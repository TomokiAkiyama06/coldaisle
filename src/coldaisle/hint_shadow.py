"""合成の起点: Workload Hint B1 の Shadow の集計 `coldaisle-hint-shadow`（#288 / 0162 §2.4）。

`hint_shadow_records`（fand が書いた Shadow の記録の行）と、0133 §2.5a の join の契約で対応する
`control_traces` の行のヒントの塊を読み、manifest の名前つきの区間ごとに数える。

```bash
uv run coldaisle-hint-shadow --evidence var/hint-shadow-evidence.yaml --db <DB の写し> \\
  --out var/hint-shadow.json
uv run coldaisle-hint-shadow --evidence var/hint-shadow-evidence.yaml --db <DB の写し> \\
  --format markdown --out var/hint-shadow.md
```

**合否を出さない。** 閾値も B2 へ進むかの提案も持たない（0064 §2.10・0133 §5 の 1・0162 §2.4）。
人の判断の材料で、昇格の証拠ではない。

数え方（0162 §2.4）:

- **元の tick の時刻の軸**（`source_ts_ms` / trace の `ts_ms`）: 候補になった tick → 届いた記録
  （`status` / `reason` ごと）→ `recorded` → fand の領域の判定に通った → 2 条件に通った、と
  「ヒント有りが選ばれていたら」の差分の分布。層は `run_id` × 証明書 × (Regime, workload)。
  対応する trace の無い記録は「対応なし」の層に数え、捨てない（0133 §2.5a）
- **受け取りの時刻の軸**（行の `received_ts_ms`）: 二重解法の成功率と worker が捨てた数。層は
  `run_id` × epoch（`worker_instance_id`）だけ。捨てた数は epoch ごとの累積の差（区間の中の値 −
  区間の前の値）で、数を運んだ行を受け取った時刻で区間に振り分ける。成功率は常に「上限（過大で
  ありうる）」として、分子と分母と一緒に出す
- **保持期間との整合**: 保持の削除の記録がある DB で、区間の開始が「残る最も古い記録の
  `source_ts_ms` + 余裕」より前なら、その区間は `retention_truncated` として数を出さない

**まだ出さないもの**: 0053 の台帳（基準の plan が Gate に評価されたか）と、基準の解が無い周期を
候補から除くこと（0175 §2.4）。いまの `record_json` は基準の推論の識別子を持たず、trace は
提案の元の tick を持たないので、推測でしか引けない。推測で埋めずに出さない（要否は人が決める）。

**制御へ逆流する経路を持たない。LLM へ渡さない。**

- 人が起動する1回実行の CLI。証拠の DB は `coldaisle-evaluate` の `EvidenceDatabase`
  （`immutable=1`）で開き、書かない（migration も走らせない）。書くのは `--out` の1ファイルだけ
- 制御側（`control_daemon.py` / `coldaisle.control`）・AI 層・API・`server.py`・日次レポート・
  運用メモリはこの module を import しない（0162 §2.4。試験で走査する）。出力を AI ツールにしない
  （ルール 1 / 8）

**同じ入力からは同じ bytes を出す。** 生成時刻を持たない（0054 §2.7）。
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import to_json

from coldaisle import logs
from coldaisle.control.evaluation.stats import MetricSummary, summarize
from coldaisle.control.model.thermal import canonical_json_bytes
from coldaisle.control.schema import (
    ControlTick,
    WorkloadHintInfluence,
    WorkloadHintStage,
    Zone,
)
from coldaisle.evaluate import (
    EvidenceDatabase,
    EvidenceDatabaseError,
    EvidenceOutputError,
    refuse_output_on_inputs,
)
from coldaisle.hint_shadow_record import HintShadowReason, HintShadowRecord, HintShadowStatus
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.store import migrations
from coldaisle.store.models import ControlTracePruneState, ControlTraceRecord

LOGGER = logging.getLogger("coldaisle.hint_shadow")

HINT_SHADOW_REPORT_SCHEMA_VERSION: Literal[1] = 1
"""`HintShadowReport` の形の版。"""

HINT_SHADOW_MANIFEST_VERSION: Literal[1] = 1
"""`--evidence` の manifest の形の版。"""

HINT_SHADOW_MIGRATION = "hint_shadow_records_epoch"
"""集計に要る最小の migration（行に epoch と累積の数がある形。決定記録 0175 §2.3）。"""

SUPPORTED_RECORD_SCHEMA_VERSION = 1
"""読める `record_json` の形の版（`hint_shadow_record.HINT_SHADOW_RECORD_SCHEMA_VERSION`）。"""

DIFF_QUANTILES: tuple[float, ...] = (0.5, 0.95)
"""差分の分布で出す nearest-rank percentile（中央値と p95。決定記録 0162 §2.4 が名指した要約）。"""

_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
_PERIOD_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class HintShadowInputError(ValueError):
    """run 全体を拒否した（索引の食い違い・読めない本文・知らない形の記録など）。

    **落とした行を除いて続けない。** 除いて数えると、残りの行だけの数になり、分母が黙って変わる。
    """


# ------------------------------------------------------------------ 入力


class HintShadowPeriod(_Frozen):
    """名前つきの区間 `[start_ms, end_ms)`。**「直近」のような相対指定を置かない。**"""

    name: str = Field(pattern=_PERIOD_NAME_PATTERN)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    run_ids: tuple[str, ...] | None = None
    """数える `run_id`（省けば区間のすべての run）。"""

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("区間は start_ms < end_ms にする")
        if self.run_ids is not None:
            if not self.run_ids:
                raise ValueError("run_ids は省くか 1 つ以上にする")
            if len(set(self.run_ids)) != len(self.run_ids):
                raise ValueError("run_ids に重複がある")
            for run_id in self.run_ids:
                if not _is_run_id(run_id):
                    raise ValueError(f"run_id の形が違う: {run_id!r}")
        return self

    def contains_run(self, run_id: str | None) -> bool:
        """この区間で数える run か。"""
        return self.run_ids is None or run_id in self.run_ids


def _is_run_id(value: str) -> bool:
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


class HintShadowManifest(_Frozen):
    """集計の入力（`--evidence`）。**期間の既定値を持たない**（0162 §2.4）。"""

    schema_version: Literal[1]
    periods: tuple[HintShadowPeriod, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_are_unique(self) -> Self:
        names = [period.name for period in self.periods]
        if len(set(names)) != len(names):
            raise ValueError("区間の name に重複がある")
        return self

    @classmethod
    def from_file(cls, path: Path) -> HintShadowManifest:
        """YAML を読み、検証済みの manifest を返す。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Hint の shadow 集計の manifest が辞書ではない: {path.name}")
        return cls.model_validate(loaded, strict=False)


def pending_domain_result_ms(path: Path) -> int:
    """`config/learned-channel.yaml` の `hint_domain.pending_domain_result_ms`（保持の検査の余裕）。

    fand と同じ設定から読む。`hint_domain` が無い設定では B1 の記録が作られないので、集計を拒む。
    """
    settings = LearnedChannelSettings.from_yaml(path)
    if settings.hint_domain is None:
        raise HintShadowInputError(
            f"learned-channel の設定に hint_domain が無い（B1 の記録を作らない設定）: {path.name}"
        )
    return settings.hint_domain.pending_domain_result_ms.value


# ------------------------------------------------------------------ 証拠の DB


@dataclass(frozen=True, slots=True)
class ShadowRow:
    """`hint_shadow_records` の1行と、0133 §2.5a の契約で対応した trace の本文（無ければ None）。"""

    record: HintShadowRecord
    trace_json: str | None


def _required_hint_shadow_version() -> int:
    for migration in migrations.discover():
        if migration.path.stem.endswith(HINT_SHADOW_MIGRATION):
            return migration.version
    raise RuntimeError(f"{HINT_SHADOW_MIGRATION} の migration が見つからない")


class HintShadowEvidenceDatabase(EvidenceDatabase):
    """証拠の DB（`immutable=1`）。`EvidenceDatabase` の開き方に、行の epoch の列がある版を要る。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        required = _required_hint_shadow_version()
        if self.schema_version < required:
            self.close()
            raise EvidenceDatabaseError(
                f"証拠の DB のスキーマが古い（version={self.schema_version}; "
                f"Hint の shadow 集計には {required} 以上が要る）: {path}"
            )

    def __enter__(self) -> Self:
        return self

    def hint_shadow_rows(self) -> tuple[ShadowRow, ...]:
        """すべての行（`seq` の順）と、対応する trace の本文。

        区間の前の行（epoch の基準値）・保持の検査（最も古い行・最大の遅れ）・epoch の切れ目に
        全体が要るので、区間で絞らずに読む（行は保持期間で抑えられている）。
        """
        rows = self._conn.execute(
            "SELECT h.seq, h.run_id, h.source_tick_id, h.source_ts_ms, h.source_monotonic_ms,"
            " h.received_monotonic_ms, h.received_ts_ms, h.schema_version, h.status, h.reason,"
            " h.record_json, h.worker_instance_id, h.shadow_dropped_backpressure,"
            " h.shadow_record_too_large, h.shadow_unavailable,"
            " (SELECT c.trace_json FROM control_traces AS c"
            "   WHERE c.ts_ms = h.source_ts_ms AND c.tick_id = h.source_tick_id"
            "     AND json_extract(c.trace_json, '$.run_id') = h.run_id) AS trace_json"
            " FROM hint_shadow_records AS h ORDER BY h.seq"
        ).fetchall()
        found: list[ShadowRow] = []
        for row in rows:
            try:
                record = HintShadowRecord(
                    seq=row["seq"],
                    run_id=row["run_id"],
                    source_tick_id=row["source_tick_id"],
                    source_ts_ms=row["source_ts_ms"],
                    source_monotonic_ms=row["source_monotonic_ms"],
                    received_monotonic_ms=row["received_monotonic_ms"],
                    received_ts_ms=row["received_ts_ms"],
                    schema_version=row["schema_version"],
                    status=row["status"],
                    reason=row["reason"],
                    record_json=row["record_json"],
                    worker_instance_id=row["worker_instance_id"],
                    shadow_dropped_backpressure=row["shadow_dropped_backpressure"],
                    shadow_record_too_large=row["shadow_record_too_large"],
                    shadow_unavailable=row["shadow_unavailable"],
                )
            except ValidationError as error:
                raise HintShadowInputError(
                    f"hint_shadow_records の行を読めない（seq={row['seq']}）"
                ) from error
            found.append(ShadowRow(record=record, trace_json=row["trace_json"]))
        return tuple(found)

    def control_trace_prune_state(self) -> ControlTracePruneState:
        """保持期間の削除の境界（`control_trace_prune`。決定記録 0071 §2.2a）。"""
        row = self._conn.execute(
            "SELECT pruned_through_seq, pruned_before_ms, legacy_until_ms FROM control_trace_prune"
        ).fetchone()
        if row is None:
            raise HintShadowInputError("control_trace_prune の行が無い")
        return ControlTracePruneState(
            pruned_through_seq=row["pruned_through_seq"],
            pruned_before_ms=row["pruned_before_ms"],
            legacy_until_ms=row["legacy_until_ms"],
        )


# ------------------------------------------------------------------ 報告の形


class Fraction(_Frozen):
    """分子と分母と値。**率だけを出さない**（0162 §2.4）。分母が 0 なら値は None。"""

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None

    @classmethod
    def of(cls, numerator: int, denominator: int) -> Fraction:
        """分子と分母から作る。"""
        return cls(
            numerator=numerator,
            denominator=denominator,
            value=None if denominator == 0 else numerator / denominator,
        )


class NamedCount(_Frozen):
    """閉じた語彙の値ごとの件数。"""

    name: str
    count: int = Field(ge=1)


class StatusReasonCount(_Frozen):
    """`status` / `reason` ごとの届いた記録の数。"""

    status: HintShadowStatus
    reason: HintShadowReason | None
    rows: int = Field(ge=1)


class ZoneDiffs(_Frozen):
    """系統ごとの `requested_demand` の差（ヒント有り − 基準）の分布。"""

    front: MetricSummary | None
    rear: MetricSummary | None
    top: MetricSummary | None


class MetricDiffs(_Frozen):
    """1つの予測の metric の差（ヒント有り − 基準）の分布。"""

    metric: str
    max_over_offsets: MetricSummary | None
    """記録ごとの全 offset での最大の差。"""
    final_offset: MetricSummary | None
    """記録ごとの最終 offset の差。"""


class DiffDistribution(_Frozen):
    """「ヒント有りが選ばれていたら」の差（`requested_demand` の段の値とモデルの予測の差）。

    **実測でも `effective_demand` でもない**（Gate・Guard・Safety の後の値を模擬しない。
    0162 §2.4）。
    """

    records: int = Field(ge=0)
    requested_demand: ZoneDiffs
    prediction: tuple[MetricDiffs, ...]


class LayerKey(_Frozen):
    """元の tick の時刻の軸の層（`run_id` × 証明書 × (Regime, workload)。0162 §2.4）。

    `trace_matched` が偽の層は、対応する trace の無い記録（「対応なし」）。Regime と workload は
    trace から引くので、対応なしの層では None。
    """

    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    certificate_sha256: str | None
    regime: str | None
    workload: str | None
    trace_matched: bool

    def sort_key(self) -> tuple[str, str, str, str, bool]:
        """出力の並び（同じ入力から同じ順）。"""
        return (
            self.run_id,
            self.certificate_sha256 or "",
            self.regime or "",
            self.workload or "",
            not self.trace_matched,
        )


class SourceAxisLayer(_Frozen):
    """元の tick の時刻の軸の段の数と差分（1つの層）。"""

    layer: LayerKey
    candidate_ticks: int = Field(ge=0)
    """`stage: shadow_mpc` かつ `influence: would_override` の tick。"""
    trace_skipped: tuple[NamedCount, ...]
    """`stage: shadow_mpc` の tick の trace の `skipped`（その tick に fand が知っていた理由）。"""
    received_records: int = Field(ge=0)
    by_status: tuple[StatusReasonCount, ...]
    recorded: int = Field(ge=0)
    domain_passed: int = Field(ge=0)
    """`recorded` のうち fand の領域の判定に通った記録。"""
    both_passed: int = Field(ge=0)
    """`recorded` のうち領域の判定と 2 条件の両方に通った記録。"""
    domain_failed: tuple[NamedCount, ...]
    """領域の判定の最初の外れの項目ごとの数。"""
    monotonicity_failed: tuple[NamedCount, ...]
    """2 条件の最初の外れの項目ごとの数（領域の判定の結果を問わない）。"""
    both_passed_of_received: Fraction
    """**届いた記録のうち**通った割合。

    worker が捨てた数を含まないので成功率ではない（0162 §2.4）。
    """
    diffs_recheck_passed: DiffDistribution
    diffs_recheck_failed: DiffDistribution


class DroppedCounts(_Frozen):
    """worker が送らずに捨てた数（観測の時刻で数えた数。0162 §2.3 / §2.4）。"""

    shadow_dropped_backpressure: int = Field(ge=0)
    shadow_record_too_large: int = Field(ge=0)
    shadow_unavailable: int = Field(ge=0)

    def total(self) -> int:
        """3つの数の和。"""
        return (
            self.shadow_dropped_backpressure
            + self.shadow_record_too_large
            + (self.shadow_unavailable)
        )


RateBound = Literal["upper_bound", "direction_undetermined"]


class EpochRate(_Frozen):
    """`run_id` × epoch の層の成功率（受け取りの時刻の軸。0162 §2.4）。"""

    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    worker_instance_id: str = Field(pattern=_RUN_ID_PATTERN)
    received_records: int = Field(ge=0)
    """`received_ts_ms` が区間の中の行（`status` を問わない）。"""
    both_passed: int = Field(ge=0)
    dropped: DroppedCounts
    """区間の中の値 − 区間の前の値（受け入れた行の累積の最大。`counters_decreased` の行を除く）。"""
    success: Fraction
    """2 条件まで通った記録 ÷（届いた記録 + 捨てた数）。"""
    rate_bound: RateBound
    """常に上限（過大でありうる）。最初の記録より前の分を含みうる epoch では向きが決まらない。"""
    may_include_before_first_record: bool
    """区間の前にこの epoch の受け入れた行が無い（基準値を 0 にした。捨てた数が多めでありうる）。"""
    ended_in_period: bool
    """区間の中で終わった（区間の中に後続の epoch の行がある）。終了で失われうる試みがある。"""
    end_unknown: bool
    """その run の最後の行が区間の中にあるだけの epoch（終わりの不明な epoch）。"""
    first_observation_lead_ms: int | None = Field(ge=0)
    """区間の開始から、区間の中で数を最初に観測した行までの時間（ずれうる幅）。"""
    last_observation_tail_ms: int | None = Field(ge=1)
    """区間の中で数を最後に観測した行から、区間の終わりまでの時間（ずれうる幅）。"""
    trace_last_counters: DroppedCounts | None
    """区間の trace の `shadow_counters` のうち、この epoch の最後の値（表示と検算だけ）。"""


class ReceiveAxis(_Frozen):
    """受け取りの時刻の軸の集計（1つの区間）。"""

    epochs: tuple[EpochRate, ...]
    dropped_total: DroppedCounts
    """区間の捨てた数の合計（epoch の和。epoch を跨いで差を取らない）。"""
    epoch_switches: int = Field(ge=0)
    """同じ `run_id` の中で `worker_instance_id` が切り替わった回数（後の行が区間の中のもの）。"""
    ended_epochs: int = Field(ge=0)
    end_unknown_epochs: int = Field(ge=0)
    epochs_with_records_before_first_observation: int = Field(ge=0)
    """`may_include_before_first_record` の epoch の数。"""


class RetentionCheck(_Frozen):
    """保持期間との整合の検査の材料（0162 §2.4）。"""

    pruned_before_ms: int | None = Field(ge=0)
    """`control_trace_prune` の削除の境界。削除の記録が無ければ None（検査しない）。"""
    oldest_source_ts_ms: int | None = Field(ge=0)
    margin_ms: int = Field(ge=1)
    """`pending_domain_result_ms` と、DB の行の最大の遅れ（受け取り − 元の tick）の大きいほう。"""
    max_observed_delay_ms: int | None
    earliest_start_ms: int | None = Field(ge=0)
    """これより前に始まる区間は `retention_truncated`。検査しないときは None。"""


class PeriodReport(_Frozen):
    """1つの区間の集計。`retention_truncated` の区間は数を出さない。"""

    name: str = Field(pattern=_PERIOD_NAME_PATTERN)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    run_ids: tuple[str, ...] | None
    status: Literal["summarized", "retention_truncated"]
    retention: RetentionCheck
    source_axis: tuple[SourceAxisLayer, ...] | None
    receive_axis: ReceiveAxis | None

    @model_validator(mode="after")
    def _truncated_period_has_no_counts(self) -> Self:
        summarized = self.status == "summarized"
        if summarized != (self.source_axis is not None) or summarized != (
            self.receive_axis is not None
        ):
            raise ValueError("数を出すのは summarized の区間だけ")
        return self


class HintShadowReport(_Frozen):
    """Workload Hint B1 の Shadow の集計（schema v1。決定記録 0162 §2.4）。

    **合否・推奨・閾値を持たない。**
    """

    schema_version: Literal[1] = HINT_SHADOW_REPORT_SCHEMA_VERSION
    pending_domain_result_ms: int = Field(ge=1)
    diff_quantiles: tuple[float, ...]
    periods: tuple[PeriodReport, ...]


def render(report: HintShadowReport) -> str:
    """報告の canonical JSON。**同じ入力からは同じ bytes になる。**"""
    return canonical_json_bytes(report).decode("utf-8")


def read_report(path: Path) -> HintShadowReport:
    """書き出した JSON の報告を読み戻す。"""
    return HintShadowReport.model_validate_json(path.read_bytes(), strict=False)


# ------------------------------------------------------------------ record_json の読み方


class _Loose(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _TargetJson(_Loose):
    offset_ms: int
    values: dict[str, float]


class _PlanJson(_Loose):
    requested_demand: dict[str, float]
    prediction: tuple[_TargetJson, ...]


class _JudgementJson(_Loose):
    passed: bool
    failed: str | None


class _RecheckJson(_Loose):
    domain: _JudgementJson
    monotonicity: _JudgementJson


class _RecordJson(_Loose):
    """`record_json` のうち集計が読む欄（`learned_channel.hint_shadow` が書く形の v1）。"""

    base: _PlanJson
    hint: _PlanJson
    recheck: _RecheckJson
    certificate_sha256: str


@dataclass(frozen=True, slots=True)
class _Diff:
    demand: dict[Zone, float]
    max_over_offsets: dict[str, float]
    final_offset: dict[str, float]


def _parse_record(record: HintShadowRecord) -> _RecordJson:
    where = f"seq={record.seq}"
    if record.schema_version != SUPPORTED_RECORD_SCHEMA_VERSION:
        raise HintShadowInputError(
            f"知らない形の record_json（{where}; schema_version={record.schema_version}）"
        )
    assert record.record_json is not None
    try:
        return _RecordJson.model_validate_json(record.record_json)
    except ValidationError as error:
        raise HintShadowInputError(f"record_json を読めない（{where}）") from error


def _diff(document: _RecordJson, *, seq: int | None) -> _Diff:
    """ヒント有り − 基準。

    予測は同じ offset どうしで比べる（fand が offset の一致を確かめてある）。
    """
    where = f"seq={seq}"
    demand: dict[Zone, float] = {}
    for zone in Zone:
        try:
            demand[zone] = (
                document.hint.requested_demand[zone.value]
                - document.base.requested_demand[zone.value]
            )
        except KeyError as error:
            raise HintShadowInputError(
                f"record_json の requested_demand が欠けている（{where}）"
            ) from error
    base_offsets = [target.offset_ms for target in document.base.prediction]
    hint_offsets = [target.offset_ms for target in document.hint.prediction]
    if base_offsets != hint_offsets or not base_offsets:
        raise HintShadowInputError(f"record_json の両 plan の予測の offset が違う（{where}）")
    metrics = sorted(
        set.intersection(
            *(
                set(target.values)
                for target in (*document.base.prediction, *document.hint.prediction)
            )
        )
    )
    max_over: dict[str, float] = {}
    final: dict[str, float] = {}
    for metric in metrics:
        diffs = [
            hint.values[metric] - base.values[metric]
            for base, hint in zip(document.base.prediction, document.hint.prediction, strict=True)
        ]
        max_over[metric] = max(diffs)
        final[metric] = diffs[-1]
    return _Diff(demand=demand, max_over_offsets=max_over, final_offset=final)


def _distribution(diffs: Sequence[_Diff]) -> DiffDistribution:
    metrics = sorted({metric for diff in diffs for metric in diff.max_over_offsets})

    def zone(z: Zone) -> MetricSummary | None:
        return summarize((diff.demand[z] for diff in diffs), quantiles=DIFF_QUANTILES)

    return DiffDistribution(
        records=len(diffs),
        requested_demand=ZoneDiffs(
            front=zone(Zone.FRONT), rear=zone(Zone.REAR), top=zone(Zone.TOP)
        ),
        prediction=tuple(
            MetricDiffs(
                metric=metric,
                max_over_offsets=summarize(
                    (
                        diff.max_over_offsets[metric]
                        for diff in diffs
                        if metric in diff.max_over_offsets
                    ),
                    quantiles=DIFF_QUANTILES,
                ),
                final_offset=summarize(
                    (diff.final_offset[metric] for diff in diffs if metric in diff.final_offset),
                    quantiles=DIFF_QUANTILES,
                ),
            )
            for metric in metrics
        ),
    )


# ------------------------------------------------------------------ trace の読み方


def _parse_tick(
    trace_json: str, *, ts_ms: int, tick_id: int, schema_version: int | None
) -> ControlTick:
    where = f"ts_ms={ts_ms}; tick_id={tick_id}"
    try:
        tick = ControlTick.model_validate_json(trace_json)
    except ValidationError as error:
        raise HintShadowInputError(f"decision trace の本文を読めない（{where}）") from error
    if (tick.ts_ms, tick.tick_id) != (ts_ms, tick_id) or (
        schema_version is not None and tick.schema_version != schema_version
    ):
        # 0053 §2.4 の trace 検証と同じ。索引の壊れた行を集計へ流さない
        raise HintShadowInputError(f"decision trace の索引と中身が一致しない（{where}）")
    return tick


@dataclass(frozen=True, slots=True)
class _TickLayer:
    certificate_sha256: str | None
    regime: str | None
    workload: str | None


def _tick_layer(tick: ControlTick) -> _TickLayer:
    hint = tick.workload_hint
    certificate = None if hint is None or hint.certificate is None else hint.certificate
    return _TickLayer(
        certificate_sha256=None if certificate is None else certificate.certificate_sha256,
        regime=None if tick.state.workload_regime is None else tick.state.workload_regime.value,
        workload=None if hint is None or hint.workload is None else hint.workload.value,
    )


# ------------------------------------------------------------------ 元の tick の時刻の軸


class _LayerCounter:
    def __init__(self) -> None:
        self.candidate_ticks = 0
        self.trace_skipped: Counter[str] = Counter()
        self.by_status: Counter[tuple[HintShadowStatus, HintShadowReason | None]] = Counter()
        self.recorded = 0
        self.domain_passed = 0
        self.both_passed = 0
        self.domain_failed: Counter[str] = Counter()
        self.monotonicity_failed: Counter[str] = Counter()
        self.passed_diffs: list[_Diff] = []
        self.failed_diffs: list[_Diff] = []

    def add_record(self, record: HintShadowRecord, document: _RecordJson | None) -> None:
        self.by_status[(record.status, record.reason)] += 1
        if document is None:
            return
        self.recorded += 1
        domain, monotonicity = document.recheck.domain, document.recheck.monotonicity
        if domain.passed:
            self.domain_passed += 1
        elif domain.failed is not None:
            self.domain_failed[domain.failed] += 1
        if not monotonicity.passed and monotonicity.failed is not None:
            self.monotonicity_failed[monotonicity.failed] += 1
        diff = _diff(document, seq=record.seq)
        if domain.passed and monotonicity.passed:
            self.both_passed += 1
            self.passed_diffs.append(diff)
        else:
            self.failed_diffs.append(diff)

    def report(self, key: LayerKey) -> SourceAxisLayer:
        received = sum(self.by_status.values())
        return SourceAxisLayer(
            layer=key,
            candidate_ticks=self.candidate_ticks,
            trace_skipped=_named(self.trace_skipped),
            received_records=received,
            by_status=tuple(
                StatusReasonCount(status=status, reason=reason, rows=count)
                for (status, reason), count in sorted(
                    self.by_status.items(),
                    key=lambda item: (
                        item[0][0].value,
                        "" if item[0][1] is None else item[0][1].value,
                    ),
                )
            ),
            recorded=self.recorded,
            domain_passed=self.domain_passed,
            both_passed=self.both_passed,
            domain_failed=_named(self.domain_failed),
            monotonicity_failed=_named(self.monotonicity_failed),
            both_passed_of_received=Fraction.of(self.both_passed, received),
            diffs_recheck_passed=_distribution(self.passed_diffs),
            diffs_recheck_failed=_distribution(self.failed_diffs),
        )


def _named(counter: Mapping[str, int]) -> tuple[NamedCount, ...]:
    return tuple(
        NamedCount(name=name, count=count) for name, count in sorted(counter.items()) if count
    )


def _source_axis(
    period: HintShadowPeriod,
    *,
    traces: Iterable[ControlTraceRecord],
    rows: Sequence[ShadowRow],
    documents: Mapping[int, _RecordJson],
) -> tuple[SourceAxisLayer, ...]:
    layers: dict[LayerKey, _LayerCounter] = {}

    def counter(key: LayerKey) -> _LayerCounter:
        return layers.setdefault(key, _LayerCounter())

    for row in traces:
        if not period.start_ms <= row.ts_ms < period.end_ms:
            raise HintShadowInputError(
                f"区間の外の trace が渡された（ts_ms={row.ts_ms}; tick_id={row.tick_id}）"
            )
        tick = _parse_tick(
            row.trace_json, ts_ms=row.ts_ms, tick_id=row.tick_id, schema_version=row.schema_version
        )
        hint = tick.workload_hint
        if tick.run_id is None or hint is None or hint.stage is not WorkloadHintStage.SHADOW_MPC:
            continue
        if not period.contains_run(tick.run_id):
            continue
        layer = _tick_layer(tick)
        key = LayerKey(
            run_id=tick.run_id,
            certificate_sha256=layer.certificate_sha256,
            regime=layer.regime,
            workload=layer.workload,
            trace_matched=True,
        )
        if hint.influence is WorkloadHintInfluence.WOULD_OVERRIDE:
            counter(key).candidate_ticks += 1
        if hint.skipped is not None:
            counter(key).trace_skipped[hint.skipped.value] += 1

    for shadow in rows:
        record = shadow.record
        if not period.start_ms <= record.source_ts_ms < period.end_ms:
            continue
        if not period.contains_run(record.run_id):
            continue
        document = None if record.seq is None else documents.get(record.seq)
        if shadow.trace_json is not None:
            tick = _parse_tick(
                shadow.trace_json,
                ts_ms=record.source_ts_ms,
                tick_id=record.source_tick_id,
                schema_version=None,
            )
            layer = _tick_layer(tick)
            key = LayerKey(
                run_id=record.run_id,
                certificate_sha256=layer.certificate_sha256,
                regime=layer.regime,
                workload=layer.workload,
                trace_matched=True,
            )
        else:
            key = LayerKey(
                run_id=record.run_id,
                certificate_sha256=None if document is None else document.certificate_sha256,
                regime=None,
                workload=None,
                trace_matched=False,
            )
        counter(key).add_record(record, document)
    return tuple(
        layers[key].report(key) for key in sorted(layers, key=lambda item: item.sort_key())
    )


# ------------------------------------------------------------------ 受け取りの時刻の軸


Epoch = tuple[str, str]
"""(`run_id`, `worker_instance_id`)。"""

_ZERO = DroppedCounts(
    shadow_dropped_backpressure=0, shadow_record_too_large=0, shadow_unavailable=0
)


def _counts(record: HintShadowRecord) -> DroppedCounts:
    return DroppedCounts(
        shadow_dropped_backpressure=record.shadow_dropped_backpressure,
        shadow_record_too_large=record.shadow_record_too_large,
        shadow_unavailable=record.shadow_unavailable,
    )


def _max(left: DroppedCounts, right: DroppedCounts) -> DroppedCounts:
    return DroppedCounts(
        shadow_dropped_backpressure=max(
            left.shadow_dropped_backpressure, right.shadow_dropped_backpressure
        ),
        shadow_record_too_large=max(left.shadow_record_too_large, right.shadow_record_too_large),
        shadow_unavailable=max(left.shadow_unavailable, right.shadow_unavailable),
    )


def _minus(left: DroppedCounts, right: DroppedCounts) -> DroppedCounts:
    return DroppedCounts(
        shadow_dropped_backpressure=left.shadow_dropped_backpressure
        - right.shadow_dropped_backpressure,
        shadow_record_too_large=left.shadow_record_too_large - right.shadow_record_too_large,
        shadow_unavailable=left.shadow_unavailable - right.shadow_unavailable,
    )


def _plus(left: DroppedCounts, right: DroppedCounts) -> DroppedCounts:
    return DroppedCounts(
        shadow_dropped_backpressure=left.shadow_dropped_backpressure
        + right.shadow_dropped_backpressure,
        shadow_record_too_large=left.shadow_record_too_large + right.shadow_record_too_large,
        shadow_unavailable=left.shadow_unavailable + right.shadow_unavailable,
    )


def _both_passed(record: HintShadowRecord, documents: Mapping[int, _RecordJson]) -> bool:
    document = None if record.seq is None else documents.get(record.seq)
    return (
        document is not None
        and document.recheck.domain.passed
        and document.recheck.monotonicity.passed
    )


def _receive_axis(
    period: HintShadowPeriod,
    *,
    rows: Sequence[ShadowRow],
    documents: Mapping[int, _RecordJson],
    trace_counters: Mapping[Epoch, DroppedCounts],
) -> ReceiveAxis:
    start, end = period.start_ms, period.end_ms
    by_run: dict[str, list[HintShadowRecord]] = {}
    for shadow in rows:
        if period.contains_run(shadow.record.run_id):
            by_run.setdefault(shadow.record.run_id, []).append(shadow.record)
    epochs: list[EpochRate] = []
    switches = 0
    for run_id in sorted(by_run):
        ordered = sorted(by_run[run_id], key=lambda item: (item.received_ts_ms, item.seq or 0))
        # 切れ目: 同じ run の中で、ある epoch の行の次に別の epoch の行が来た（後の行が区間の中）
        for previous, current in pairwise(ordered):
            if (
                previous.worker_instance_id != current.worker_instance_id
                and start <= current.received_ts_ms < end
            ):
                switches += 1
        in_period_epochs = sorted(
            {
                record.worker_instance_id
                for record in ordered
                if start <= record.received_ts_ms < end
            }
        )
        last_of_run = ordered[-1]
        for worker in in_period_epochs:
            epochs.append(
                _epoch_rate(
                    period,
                    run_id=run_id,
                    worker=worker,
                    ordered=ordered,
                    last_of_run=last_of_run,
                    documents=documents,
                    trace_counters=trace_counters,
                )
            )
    total = _ZERO
    for epoch in epochs:
        total = _plus(total, epoch.dropped)
    return ReceiveAxis(
        epochs=tuple(epochs),
        dropped_total=total,
        epoch_switches=switches,
        ended_epochs=sum(epoch.ended_in_period for epoch in epochs),
        end_unknown_epochs=sum(epoch.end_unknown for epoch in epochs),
        epochs_with_records_before_first_observation=sum(
            epoch.may_include_before_first_record for epoch in epochs
        ),
    )


def _epoch_rate(
    period: HintShadowPeriod,
    *,
    run_id: str,
    worker: str,
    ordered: Sequence[HintShadowRecord],
    last_of_run: HintShadowRecord,
    documents: Mapping[int, _RecordJson],
    trace_counters: Mapping[Epoch, DroppedCounts],
) -> EpochRate:
    start, end = period.start_ms, period.end_ms
    mine = [record for record in ordered if record.worker_instance_id == worker]
    # 端点に使うのは fand が数を受け入れた行だけ（0162 §2.4。PR #365 の Codex P2）
    accepted = [
        record for record in mine if record.reason is not HintShadowReason.COUNTERS_DECREASED
    ]
    before = [record for record in accepted if record.received_ts_ms < start]
    until_end = [record for record in accepted if record.received_ts_ms < end]
    base = _ZERO
    for record in before:
        base = _max(base, _counts(record))
    value = base
    for record in until_end:
        value = _max(value, _counts(record))
    dropped = _minus(value, base)
    in_period = [record for record in mine if start <= record.received_ts_ms < end]
    observed = [record for record in accepted if start <= record.received_ts_ms < end]
    received = len(in_period)
    passed = sum(_both_passed(record, documents) for record in in_period)
    last_of_epoch = mine[-1]
    later_in_period = any(
        record.worker_instance_id != worker
        and start <= record.received_ts_ms < end
        and (record.received_ts_ms, record.seq or 0)
        > (last_of_epoch.received_ts_ms, last_of_epoch.seq or 0)
        for record in ordered
    )
    last_in_period = start <= last_of_epoch.received_ts_ms < end
    ended = last_in_period and later_in_period
    end_unknown = last_in_period and last_of_epoch is last_of_run
    may_include_before = not before
    return EpochRate(
        run_id=run_id,
        worker_instance_id=worker,
        received_records=received,
        both_passed=passed,
        dropped=dropped,
        success=Fraction.of(passed, received + dropped.total()),
        rate_bound="direction_undetermined" if may_include_before else "upper_bound",
        may_include_before_first_record=may_include_before,
        ended_in_period=ended,
        end_unknown=end_unknown,
        first_observation_lead_ms=None if not observed else observed[0].received_ts_ms - start,
        last_observation_tail_ms=None if not observed else end - observed[-1].received_ts_ms,
        trace_last_counters=trace_counters.get((run_id, worker)),
    )


def _trace_counters(
    period: HintShadowPeriod, traces: Iterable[ControlTraceRecord]
) -> dict[Epoch, DroppedCounts]:
    """区間の trace の `shadow_counters` のうち、epoch ごとの最後の値（表示と検算だけ）。"""
    found: dict[Epoch, DroppedCounts] = {}
    for row in traces:
        tick = _parse_tick(
            row.trace_json, ts_ms=row.ts_ms, tick_id=row.tick_id, schema_version=row.schema_version
        )
        hint = tick.workload_hint
        if tick.run_id is None or hint is None or hint.shadow_counters is None:
            continue
        if not period.contains_run(tick.run_id):
            continue
        counters = hint.shadow_counters
        found[(tick.run_id, counters.worker_instance_id)] = DroppedCounts(
            shadow_dropped_backpressure=counters.shadow_dropped_backpressure,
            shadow_record_too_large=counters.shadow_record_too_large,
            shadow_unavailable=counters.shadow_unavailable,
        )
    return found


# ------------------------------------------------------------------ 保持期間


def _retention(
    *,
    prune: ControlTracePruneState,
    rows: Sequence[ShadowRow],
    pending_ms: int,
) -> RetentionCheck:
    """保持期間との整合の検査の材料（0162 §2.4。PR #365 の Codex P2）。

    保持の削除は `source_ts_ms` で行を消すので、境界の直前の tick を元にし区間の中で受け取った
    記録が消えうる。削除の記録（`control_trace_prune.pruned_before_ms`。`hint_shadow_records` は
    同じ rollup・同じ保持日数で消える）がある DB でだけ、「残る最も古い `source_ts_ms` と削除の
    境界の遅いほう + 余裕」より前に始まる区間を拒む。余裕は `pending_domain_result_ms` と、DB の
    行の最大の遅れの大きいほう（遅れの上限を記録の外から仮定しない）。
    """
    delays = [shadow.record.received_ts_ms - shadow.record.source_ts_ms for shadow in rows]
    max_delay = max(delays) if delays else None
    margin = max(pending_ms, max_delay if max_delay is not None else 0)
    oldest = min((shadow.record.source_ts_ms for shadow in rows), default=None)
    earliest: int | None = None
    if prune.pruned_before_ms is not None:
        boundary = prune.retained_from_ms if oldest is None else max(prune.retained_from_ms, oldest)
        earliest = boundary + margin
    return RetentionCheck(
        pruned_before_ms=prune.pruned_before_ms,
        oldest_source_ts_ms=oldest,
        margin_ms=margin,
        max_observed_delay_ms=max_delay,
        earliest_start_ms=earliest,
    )


# ------------------------------------------------------------------ 組み立て


def build_report(
    *,
    manifest: HintShadowManifest,
    rows: Sequence[ShadowRow],
    traces_by_period: Mapping[str, Sequence[ControlTraceRecord]],
    prune: ControlTracePruneState,
    pending_ms: int,
) -> HintShadowReport:
    """読んだ行から報告を作る。**合否を出さない。**

    `traces_by_period` は区間の名前ごとの `[start_ms, end_ms)` の trace。壊れた行・知らない形の
    記録があれば run 全体を拒否する（`HintShadowInputError`）。
    """
    documents: dict[int, _RecordJson] = {}
    for shadow in rows:
        record = shadow.record
        if record.status is HintShadowStatus.RECORDED:
            assert record.seq is not None
            documents[record.seq] = _parse_record(record)
    retention = _retention(prune=prune, rows=rows, pending_ms=pending_ms)
    periods: list[PeriodReport] = []
    for period in manifest.periods:
        traces = traces_by_period[period.name]
        truncated = retention.earliest_start_ms is not None and (
            period.start_ms < retention.earliest_start_ms
        )
        if truncated:
            periods.append(
                PeriodReport(
                    name=period.name,
                    start_ms=period.start_ms,
                    end_ms=period.end_ms,
                    run_ids=period.run_ids,
                    status="retention_truncated",
                    retention=retention,
                    source_axis=None,
                    receive_axis=None,
                )
            )
            continue
        periods.append(
            PeriodReport(
                name=period.name,
                start_ms=period.start_ms,
                end_ms=period.end_ms,
                run_ids=period.run_ids,
                status="summarized",
                retention=retention,
                source_axis=_source_axis(period, traces=traces, rows=rows, documents=documents),
                receive_axis=_receive_axis(
                    period,
                    rows=rows,
                    documents=documents,
                    trace_counters=_trace_counters(period, traces),
                ),
            )
        )
    return HintShadowReport(
        pending_domain_result_ms=pending_ms,
        diff_quantiles=DIFF_QUANTILES,
        periods=tuple(periods),
    )


# ------------------------------------------------------------------ Markdown

_ABSENT = "—"


def _number(value: float | None) -> str:
    """数値を**報告の JSON と同じ字面**で出す。丸めない。"""
    if value is None:
        return _ABSENT
    return to_json(value).decode()


def _code(value: str | None) -> str:
    return _ABSENT if value is None else f"`{value}`"


def _table(header: tuple[str, ...], body: Sequence[tuple[str, ...]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(cells) + " |" for cells in body)
    return lines


def _fraction(value: Fraction) -> str:
    return f"{value.numerator} / {value.denominator}（{_number(value.value)}）"


def _counts_cell(items: Sequence[NamedCount]) -> str:
    if not items:
        return _ABSENT
    return "<br>".join(f"`{item.name}`: {item.count}" for item in items)


def _summary_cells(summary: MetricSummary | None) -> tuple[str, ...]:
    if summary is None:
        return (_ABSENT,) * (3 + len(DIFF_QUANTILES))
    values = {item.quantile: item.value for item in summary.percentiles}
    return (
        _number(summary.count),
        _number(summary.minimum),
        *(_number(values.get(quantile)) for quantile in DIFF_QUANTILES),
        _number(summary.maximum),
    )


def _layer_name(key: LayerKey) -> str:
    if not key.trace_matched:
        return f"`{key.run_id[:8]}` 対応なし"
    certificate = _ABSENT if key.certificate_sha256 is None else f"`{key.certificate_sha256[:12]}`"
    return f"`{key.run_id[:8]}` / {certificate} / {_code(key.regime)} / {_code(key.workload)}"


def _source_lines(layers: Sequence[SourceAxisLayer]) -> list[str]:
    lines = [
        "### 段の数（元の tick の時刻の軸）",
        "",
        "層は `run_id` × 証明書 × (Regime, workload)。"
        "対応する trace の無い記録は「対応なし」の層に数える。"
        "「届いた記録のうち」は worker が捨てた数を含まないので**成功率ではない**。",
        "",
    ]
    lines.extend(
        _table(
            (
                "層",
                "候補 tick",
                "trace の skipped",
                "届いた記録",
                "status / reason",
                "recorded",
                "領域の判定に通った",
                "2 条件まで通った",
                "領域の外れ",
                "2 条件の外れ",
                "届いた記録のうち",
            ),
            [
                (
                    _layer_name(layer.layer),
                    _number(layer.candidate_ticks),
                    _counts_cell(layer.trace_skipped),
                    _number(layer.received_records),
                    "<br>".join(
                        f"`{item.status.value}`"
                        + ("" if item.reason is None else f" / `{item.reason.value}`")
                        + f": {item.rows}"
                        for item in layer.by_status
                    )
                    or _ABSENT,
                    _number(layer.recorded),
                    _number(layer.domain_passed),
                    _number(layer.both_passed),
                    _counts_cell(layer.domain_failed),
                    _counts_cell(layer.monotonicity_failed),
                    _fraction(layer.both_passed_of_received),
                )
                for layer in layers
            ],
        )
    )
    quantile_header = tuple(f"p{_number(quantile)}" for quantile in DIFF_QUANTILES)
    rows: list[tuple[str, ...]] = []
    for layer in layers:
        for label, distribution in (
            ("再検査に通った", layer.diffs_recheck_passed),
            ("再検査に落ちた", layer.diffs_recheck_failed),
        ):
            for zone in Zone:
                rows.append(
                    (
                        _layer_name(layer.layer),
                        label,
                        f"requested_demand `{zone.value}`",
                        *_summary_cells(getattr(distribution.requested_demand, zone.value)),
                    )
                )
            for metric in distribution.prediction:
                rows.append(
                    (
                        _layer_name(layer.layer),
                        label,
                        f"予測 `{metric.metric}`（全 offset の最大）",
                        *_summary_cells(metric.max_over_offsets),
                    )
                )
                rows.append(
                    (
                        _layer_name(layer.layer),
                        label,
                        f"予測 `{metric.metric}`（最終 offset）",
                        *_summary_cells(metric.final_offset),
                    )
                )
    lines.extend(
        [
            "",
            "### 「ヒント有りが選ばれていたら」の差（ヒント有り − 基準）",
            "",
            "**差は `requested_demand` の段の値と、モデルの予測の差であり、"
            "実測でも `effective_demand` でもない**"
            "（Gate・Reactive Guard・Critical Safety の後の値を模擬しない。決定記録 0162 §2.4）。",
            "",
            *_table(("層", "再検査", "量", "count", "min", *quantile_header, "max"), rows),
        ]
    )
    return lines


def _receive_lines(axis: ReceiveAxis) -> list[str]:
    total = axis.dropped_total
    lines = [
        "### 二重解法の成功率（受け取りの時刻の軸。`run_id` × epoch の層だけ）",
        "",
        "成功率 = 2 条件まで通った記録 ÷（届いた記録 + worker が捨てた数）。"
        "分子・分母のすべての項を行の `received_ts_ms` で区間に振り分ける。"
        "捨てた数は**観測の時刻で数えた数**（数を運んだ行を"
        "fand が受け取った時刻）で、区間の前の値との差（epoch ごと。受け入れた行の累積の最大）。"
        "**成功率は常に上限（過大でありうる）**: "
        "最後の記録の後の試みと、worker の終了で途中だった試みは"
        "見えない。区間の前にその epoch の記録が無いときは基準値を 0 にし、捨てた数が多めでありうる"
        "（率は低めに出る）ので、向きの決まらない値になる。ずれうる幅は区間の開始から最初の観測まで・"
        "最後の観測から区間の終わりまでの時間。",
        "",
    ]
    lines.extend(
        _table(
            (
                "run / epoch",
                "届いた記録",
                "2 条件まで通った",
                "捨てた数（backpressure / too_large / unavailable）",
                "成功率",
                "向き",
                "最初の記録より前の分を含みうる",
                "区間の中で終わった",
                "終わりの不明な epoch",
                "ずれうる幅（先頭 / 末尾 ms）",
                "trace の shadow_counters（表示だけ）",
            ),
            [
                (
                    f"`{epoch.run_id[:8]}` / `{epoch.worker_instance_id[:8]}`",
                    _number(epoch.received_records),
                    _number(epoch.both_passed),
                    f"{epoch.dropped.shadow_dropped_backpressure} / "
                    f"{epoch.dropped.shadow_record_too_large} / {epoch.dropped.shadow_unavailable}",
                    _fraction(epoch.success),
                    "上限" if epoch.rate_bound == "upper_bound" else "向きが決まらない",
                    "はい" if epoch.may_include_before_first_record else "いいえ",
                    "はい" if epoch.ended_in_period else "いいえ",
                    "はい" if epoch.end_unknown else "いいえ",
                    f"{_number(epoch.first_observation_lead_ms)} / "
                    f"{_number(epoch.last_observation_tail_ms)}",
                    _ABSENT
                    if epoch.trace_last_counters is None
                    else f"{epoch.trace_last_counters.shadow_dropped_backpressure} / "
                    f"{epoch.trace_last_counters.shadow_record_too_large} / "
                    f"{epoch.trace_last_counters.shadow_unavailable}",
                )
                for epoch in axis.epochs
            ],
        )
    )
    lines.extend(
        [
            "",
            *_table(
                ("項目", "値"),
                [
                    (
                        "捨てた数の合計（少なくともこれだけ）",
                        f"{total.shadow_dropped_backpressure} / {total.shadow_record_too_large} / "
                        f"{total.shadow_unavailable}",
                    ),
                    ("epoch の切れ目", _number(axis.epoch_switches)),
                    ("区間の中で終わった epoch", _number(axis.ended_epochs)),
                    ("終わりの不明な epoch", _number(axis.end_unknown_epochs)),
                    (
                        "最初の記録より前の分を含みうる epoch",
                        _number(axis.epochs_with_records_before_first_observation),
                    ),
                ],
            ),
        ]
    )
    return lines


def render_markdown(report: HintShadowReport) -> str:
    """報告を Markdown へ。**報告から写すだけで、新しい計算も判定もしない。**"""
    lines = [
        "# Workload Hint B1 の Shadow の集計",
        "",
        "**合否は出さない。** 閾値も B2 へ進むかの提案も持たない"
        "（決定記録 0162 §2.4 / 0133 §5 の 1）。"
        "人の判断の材料で、昇格の証拠ではない。",
        "",
        *_table(
            ("項目", "値"),
            [
                ("pending_domain_result_ms", _number(report.pending_domain_result_ms)),
                ("schema_version", _number(report.schema_version)),
            ],
        ),
    ]
    for period in report.periods:
        retention = period.retention
        lines.extend(
            [
                "",
                f"## 区間 `{period.name}`",
                "",
                *_table(
                    ("項目", "値"),
                    [
                        ("区間", f"[{period.start_ms}, {period.end_ms})"),
                        (
                            "run_id",
                            "すべて"
                            if period.run_ids is None
                            else "<br>".join(f"`{run_id}`" for run_id in period.run_ids),
                        ),
                        ("status", _code(period.status)),
                        ("保持の削除の境界", _number(retention.pruned_before_ms)),
                        ("残る最も古い source_ts_ms", _number(retention.oldest_source_ts_ms)),
                        ("余裕（ms）", _number(retention.margin_ms)),
                        ("最大の遅れ（ms）", _number(retention.max_observed_delay_ms)),
                        ("これより前に始まる区間は数えない", _number(retention.earliest_start_ms)),
                    ],
                ),
            ]
        )
        if period.status == "retention_truncated":
            lines.extend(
                [
                    "",
                    "**`retention_truncated`**: 区間の開始が保持の削除で消えうる範囲にかかるので、"
                    "数を出さない。区間を後ろへずらして渡し直す。",
                ]
            )
            continue
        assert period.source_axis is not None and period.receive_axis is not None
        lines.extend(
            ["", *_source_lines(period.source_axis), "", *_receive_lines(period.receive_axis)]
        )
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ CLI

ReportFormat = Literal["json", "markdown"]
REPORT_FORMATS: tuple[ReportFormat, ...] = ("json", "markdown")
DEFAULT_OUT: dict[ReportFormat, Path] = {
    "json": Path("var/hint-shadow.json"),
    "markdown": Path("var/hint-shadow.md"),
}
_RENDERERS: dict[ReportFormat, Callable[[HintShadowReport], str]] = {
    "json": render,
    "markdown": render_markdown,
}


def write(report: HintShadowReport, path: Path, *, report_format: ReportFormat = "json") -> Path:
    """報告を書き出す。**書くのはこの path だけ**（DB・設定・trace には書かない）。

    同じディレクトリの一時ファイルへ書いてから置き換える（`coldaisle-supervisor-shadow` と同じ）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_RENDERERS[report_format](report).encode("utf-8"))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def build_parser() -> argparse.ArgumentParser:
    """CLI の引数。**合否の閾値・期間の既定値を受け取る口は作らない**（0162 §2.4）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-hint-shadow",
        description=(
            "Workload Hint B1 の Shadow の記録を集計する（#288 / 決定記録 0162 §2.4）。"
            "**読むだけで、合否は出さない**"
        ),
    )
    parser.add_argument(
        "--evidence", type=Path, required=True, help="名前つきの区間（と任意の run_id）の manifest"
    )
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument(
        "--learned-channel-config",
        type=Path,
        default=Path("config/learned-channel.yaml"),
        help="pending_domain_result_ms を読む learned-channel.yaml（fand と同じ設定）",
    )
    parser.add_argument(
        "--format",
        dest="report_format",
        choices=REPORT_FORMATS,
        default="json",
        help="json（保存・比較の正本）/ markdown（人が読む版。同じ報告から写すだけ）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="既定は json なら var/hint-shadow.json、markdown なら var/hint-shadow.md",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI の入口。拒否した run では何も書かずに 1 を返す。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)

    report_format: ReportFormat = args.report_format
    out: Path = args.out if args.out is not None else DEFAULT_OUT[report_format]
    try:
        # **どの入力を開くより前に**確かめる（#227。`--out` が DB を指すと証拠を上書きする）
        refuse_output_on_inputs(
            out, db=args.db, inputs=(args.evidence, args.learned_channel_config)
        )
    except EvidenceOutputError as error:
        LOGGER.error(
            "Hint の shadow 集計を拒否した（何も書かない）",
            extra={logs.FIELDS_KEY: {"reason": str(error), "out": str(out)}},
        )
        return 1
    manifest = HintShadowManifest.from_file(args.evidence)
    try:
        pending_ms = pending_domain_result_ms(args.learned_channel_config)
        # **証拠の DB は読み取り専用で開く**（`immutable=1`。添え file に中身があれば開かない）
        with HintShadowEvidenceDatabase(args.db) as store, store.snapshot():
            rows = store.hint_shadow_rows()
            prune = store.control_trace_prune_state()
            traces_by_period = {
                period.name: store.control_traces(period.start_ms, period.end_ms)
                for period in manifest.periods
            }
        report = build_report(
            manifest=manifest,
            rows=rows,
            traces_by_period=traces_by_period,
            prune=prune,
            pending_ms=pending_ms,
        )
    except (HintShadowInputError, EvidenceDatabaseError) as error:
        LOGGER.error(
            "Hint の shadow 集計を拒否した（何も書かない）",
            extra={
                logs.FIELDS_KEY: {
                    "reason": str(error),
                    "out": str(out),
                    "stale_out_exists": out.exists(),
                }
            },
        )
        return 1
    path = write(report, out, report_format=report_format)
    LOGGER.info(
        "Hint の shadow 集計を書き出した",
        extra={
            logs.FIELDS_KEY: {
                "path": str(path),
                "format": report_format,
                "periods": [
                    {"name": period.name, "status": period.status} for period in report.periods
                ],
            }
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
