"""合成の起点: Air Balance の協調の shadow 集計（#81 / 決定記録 0093 §2.1）。

保存済み decision trace（`control_traces`）の `ControlTick.air_balance_coordination` だけを読み、
`status` / `skip_reason` ごとの件数、zone ごとの引き上げ幅（`counterfactual_output - candidate`）の
分布、引き上げの有無が切り替わった回数、zone の Fan fault で `skipped` になった tick の数を
1つの報告にまとめる。

```bash
uv run coldaisle-air-balance-shadow --evidence var/air-balance-shadow-evidence.yaml \\
  --control-config var/control-config --out var/air-balance-shadow.json
uv run coldaisle-air-balance-shadow --evidence var/air-balance-shadow-evidence.yaml \\
  --control-config var/control-config --format markdown --out var/air-balance-shadow.md
```

**合否を出さない。** shadow → apply の基準は shadow の実データを見てから所有者と決める
（決定記録 0078 §5）。この報告は人の判断の材料で、昇格の証拠ではない（0093 §2.1）。

**制御へ逆流する経路を持たない。**

- 制御プロセスとは別の、人が起動する1回実行の CLI である
- 証拠の DB は `coldaisle-evaluate` の `EvidenceDatabase`（`immutable=1`）で開き、書かない。
  書くのは `--out` の1ファイルだけ
- 制御側（`control_daemon.py` / `control/`）はこの module を import しない
- LLM のツールにしない（AGENTS.md ルール1 / 8）

**同じ入力からは同じ bytes を出す。** 生成時刻を持たない（0054 §2.7）。
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import to_json

from coldaisle import logs
from coldaisle.control.config import ControlConfig
from coldaisle.control.evaluation.stats import MetricSummary, summarize
from coldaisle.control.model.thermal import canonical_json_bytes
from coldaisle.control.schema import (
    ZONE_FAULTS,
    AirBalanceCoordinationMode,
    AirBalanceCoordinationRecord,
    AirBalanceCoordinationSkipReason,
    AirBalanceCoordinationStatus,
    ControlTick,
    PerZone,
    Zone,
)
from coldaisle.evaluate import EvidenceDatabase
from coldaisle.store.models import ControlTraceRecord

LOGGER = logging.getLogger("coldaisle.air_balance_shadow")

AIR_BALANCE_SHADOW_REPORT_SCHEMA_VERSION: Literal[1] = 1
"""`AirBalanceShadowReport` の形の版。"""

AIR_BALANCE_SHADOW_MANIFEST_VERSION: Literal[1] = 1
"""`--evidence` の manifest の形の版。"""

AIR_BALANCE_SHADOW_CONFIG_VERSION: Literal[1] = 1
"""`config/air-balance-shadow.yaml` の形の版。"""

_SHA256_PATTERN = r"^[0-9a-f]{64}$"

_EVALUATED_STATUSES: frozenset[AirBalanceCoordinationStatus] = frozenset(
    {
        AirBalanceCoordinationStatus.SHADOW,
        AirBalanceCoordinationStatus.NOT_NEEDED,
        AirBalanceCoordinationStatus.APPLIED,
    }
)
"""`counterfactual_output` を記録する status（0078 §2.7）。引き上げ幅はここからだけ作る。"""

_FAULT_SKIP_REASONS: frozenset[AirBalanceCoordinationSkipReason] = frozenset(
    {AirBalanceCoordinationSkipReason.ZONE_FAN_FAULT, AirBalanceCoordinationSkipReason.SAFETY_STATE}
)
"""zone の Fan fault を数える `skip_reason`。

Top の Fan fault は無条件の `EMERGENCY` なので `safety_state` になる（0088 §2.3）。
`zone_fan_fault` だけで数えると Top を取りこぼす（0088 §3 / 0093 §2.1.2）。
"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class AirBalanceShadowInputError(ValueError):
    """run 全体を拒否した（索引の食い違い・古い trace・設定の不一致など）。

    **落とした行を除いて続けない**（0093 §2.1.1）。期間を分けて渡し直す。
    """


class AirBalanceShadowPeriod(_Frozen):
    """集計した期間 `[start_ms, end_ms)`。**「直近」のような相対指定を置かない。**"""

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("period は start_ms < end_ms にする")
        return self


class AirBalanceShadowManifest(_Frozen):
    """1回の集計の入力（`--evidence`）。**期間は1つ。**"""

    schema_version: Literal[1]
    period: AirBalanceShadowPeriod

    @classmethod
    def from_file(cls, path: Path) -> AirBalanceShadowManifest:
        """YAML を読み、検証済みの manifest を返す。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Air Balance の shadow 集計の manifest が辞書ではない: {path.name}")
        return cls.model_validate(loaded)


class AirBalanceShadowConfig(_Frozen):
    """集計の表し方（`config/air-balance-shadow.yaml`）。**合否の閾値は持たない。**"""

    schema_version: Literal[1]
    percentiles: tuple[float, ...]
    """引き上げ幅の分布で出す nearest-rank percentile（重複なし昇順、0 < q <= 1）。"""

    @model_validator(mode="after")
    def _percentiles_are_ordered(self) -> Self:
        quantiles = self.percentiles
        if any(not 0.0 < quantile <= 1.0 for quantile in quantiles):
            raise ValueError("percentiles は 0 より大きく 1 以下の値にする")
        if list(quantiles) != sorted(set(quantiles)):
            raise ValueError("percentiles は重複なし昇順にする")
        return self

    @classmethod
    def from_file(cls, path: Path) -> AirBalanceShadowConfig:
        """YAML を読み、検証済みの設定を返す。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Air Balance の shadow 集計の設定が辞書ではない: {path.name}")
        return cls.model_validate(loaded, strict=False)


class AirBalanceShadowConfigBinding(_Frozen):
    """照合した Control Config の hash（trace の `runtime.config` と一致した値）。"""

    fan_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    air_balance_sha256: str = Field(pattern=_SHA256_PATTERN)
    fan_hardware_sha256: str = Field(pattern=_SHA256_PATTERN)


class StatusCounts(_Frozen):
    """`status` ごとの tick の数。**6つの鍵を常に全部出す**（0 でも省かない）。"""

    off: int = Field(ge=0)
    skipped: int = Field(ge=0)
    not_needed: int = Field(ge=0)
    shadow: int = Field(ge=0)
    applied: int = Field(ge=0)
    failed: int = Field(ge=0)

    def total(self) -> int:
        """全 tick の数。"""
        return self.off + self.skipped + self.not_needed + self.shadow + self.applied + self.failed


class SkipReasonCounts(_Frozen):
    """`skip_reason` ごとの `skipped` の tick の数。**7つの鍵を常に全部出す。**"""

    air_balance_disabled: int = Field(ge=0)
    operating_mode: int = Field(ge=0)
    snapshot_unavailable: int = Field(ge=0)
    baseline_unavailable: int = Field(ge=0)
    safety_state: int = Field(ge=0)
    zone_fan_fault: int = Field(ge=0)
    tach_unconfirmed: int = Field(ge=0)

    def total(self) -> int:
        """`skipped` の tick の数。"""
        return sum(self.model_dump().values())


class ZoneRaise(_Frozen):
    """1つの zone の引き上げ幅（`counterfactual_output - candidate`）。"""

    raised_ticks: int = Field(ge=0)
    """`counterfactual_output > candidate` だった tick の数（厳密な不等号。0078 §2.7）。"""
    toggles: int = Field(ge=0)
    """引き上げの有無が、`counterfactual_output` を記録した隣り合う tick で変わった回数。"""
    all_ticks: MetricSummary | None
    """`counterfactual_output` を記録したすべての tick の幅（0 を含む）。無ければ `None`。"""
    raised_only: MetricSummary | None
    """上げた tick だけの幅。無ければ `None`（0 で埋めない）。"""


class AnyZoneRaise(_Frozen):
    """「どれかの zone で上がっている」の有無。"""

    raised_ticks: int = Field(ge=0)
    toggles: int = Field(ge=0)


class AirBalanceShadowReport(_Frozen):
    """Air Balance の協調の shadow 集計（schema v1。決定記録 0093 §2.1）。

    **合否・推奨・閾値を持たない。** 数え方は 0093 §2.1.2。
    """

    schema_version: Literal[1] = AIR_BALANCE_SHADOW_REPORT_SCHEMA_VERSION
    period: AirBalanceShadowPeriod
    config: AirBalanceShadowConfigBinding
    percentiles: tuple[float, ...]
    mode: AirBalanceCoordinationMode | None
    """trace に記録された協調の `mode`。tick が無ければ `None`。"""
    ticks: int = Field(ge=0)
    first_ts_ms: int | None = Field(ge=0)
    last_ts_ms: int | None = Field(ge=0)
    status: StatusCounts
    skip_reason: SkipReasonCounts
    evaluated_ticks: int = Field(ge=0)
    """`counterfactual_output` を記録した tick（`shadow` / `not_needed` / `applied`）の数。"""
    any_zone: AnyZoneRaise
    zones: PerZone[ZoneRaise]
    fan_fault_skips: PerZone[int]
    """zone の Fan fault で `skipped` になった tick の数。

    `safety_state` と同じ tick の `faults` を含む（0088 §3）。
    """

    @model_validator(mode="after")
    def _counts_agree(self) -> Self:
        if self.status.total() != self.ticks:
            raise ValueError("status の件数の合計が ticks と一致しない")
        if self.skip_reason.total() != self.status.skipped:
            raise ValueError("skip_reason の件数の合計が skipped と一致しない")
        evaluated = self.status.shadow + self.status.not_needed + self.status.applied
        if evaluated != self.evaluated_ticks:
            raise ValueError("evaluated_ticks が shadow / not_needed / applied の合計と一致しない")
        if (self.ticks == 0) != (self.first_ts_ms is None) or (self.ticks == 0) != (
            self.last_ts_ms is None
        ):
            raise ValueError("first_ts_ms / last_ts_ms は tick があるときだけ記録する")
        if (self.ticks == 0) != (self.mode is None):
            raise ValueError("mode は tick があるときだけ記録する")
        if self.any_zone.raised_ticks > self.evaluated_ticks:
            raise ValueError("引き上げた tick が evaluated_ticks を超えている")
        for zone in Zone:
            item = self.zones.get(zone)
            if item.raised_ticks > self.evaluated_ticks:
                raise ValueError(f"{zone.value} の引き上げた tick が evaluated_ticks を超えている")
            if (item.all_ticks is None) != (self.evaluated_ticks == 0):
                raise ValueError(f"{zone.value} の分布の有無が evaluated_ticks と一致しない")
            if (item.raised_only is None) != (item.raised_ticks == 0):
                raise ValueError(f"{zone.value} の引き上げた tick の分布の有無が件数と一致しない")
        return self


def render(report: AirBalanceShadowReport) -> str:
    """報告の canonical JSON。**同じ入力からは同じ bytes になる。**"""
    return canonical_json_bytes(report).decode("utf-8")


def read_report(path: Path) -> AirBalanceShadowReport:
    """書き出した JSON の報告を読み戻す。"""
    return AirBalanceShadowReport.model_validate_json(path.read_bytes(), strict=False)


class _Counter:
    """trace の tick を1つずつ受け取って数える。"""

    def __init__(self) -> None:
        self.ticks = 0
        self.first_ts_ms: int | None = None
        self.last_ts_ms: int | None = None
        self.modes: set[AirBalanceCoordinationMode] = set()
        self.status = dict.fromkeys(AirBalanceCoordinationStatus, 0)
        self.skip_reason = dict.fromkeys(AirBalanceCoordinationSkipReason, 0)
        self.raises: dict[Zone, list[float]] = {zone: [] for zone in Zone}
        self.toggles = dict.fromkeys(Zone, 0)
        self.any_raised = 0
        self.any_toggles = 0
        self.previous: tuple[dict[Zone, bool], bool] | None = None
        self.fault_skips = dict.fromkeys(Zone, 0)

    def add(self, tick: ControlTick, record: AirBalanceCoordinationRecord) -> None:
        self.ticks += 1
        if self.first_ts_ms is None:
            self.first_ts_ms = tick.ts_ms
        self.last_ts_ms = tick.ts_ms
        self.modes.add(record.mode)
        self.status[record.status] += 1
        if record.skip_reason is not None:
            self.skip_reason[record.skip_reason] += 1
            if record.skip_reason in _FAULT_SKIP_REASONS:
                faulted = {
                    fault.zone
                    for fault in tick.faults
                    if fault.code in ZONE_FAULTS and fault.zone is not None
                }
                for zone in faulted:
                    self.fault_skips[zone] += 1
        if record.status in _EVALUATED_STATUSES:
            self._add_raise(tick, record)

    def _add_raise(self, tick: ControlTick, record: AirBalanceCoordinationRecord) -> None:
        candidate, counterfactual = record.candidate, record.counterfactual_output
        if candidate is None or counterfactual is None:
            # 0078 §2.7 の検証で起きない形。起きたら記録が壊れているので数えずに止める。
            raise AirBalanceShadowInputError(
                "counterfactual_output を記録するはずの tick に"
                " candidate か counterfactual_output が"
                f" 無い（ts_ms={tick.ts_ms}; tick_id={tick.tick_id}）"
            )
        raised: dict[Zone, bool] = {}
        for zone in Zone:
            width = counterfactual.get(zone) - candidate.get(zone)
            self.raises[zone].append(width)
            raised[zone] = counterfactual.get(zone) > candidate.get(zone)
        any_raised = any(raised.values())
        self.any_raised += any_raised
        if self.previous is not None:
            previous_zones, previous_any = self.previous
            for zone in Zone:
                self.toggles[zone] += raised[zone] != previous_zones[zone]
            self.any_toggles += any_raised != previous_any
        self.previous = (raised, any_raised)

    def report(
        self,
        *,
        period: AirBalanceShadowPeriod,
        config: AirBalanceShadowConfigBinding,
        percentiles: tuple[float, ...],
    ) -> AirBalanceShadowReport:
        if len(self.modes) > 1:
            # 同じ fan-policy.yaml の hash の下では起きない。起きたら混ぜずに止める。
            raise AirBalanceShadowInputError(
                "期間の中で協調の mode が変わっている。期間を分けて渡す"
            )
        mode = next(iter(self.modes)) if self.modes else None
        status = self.status
        evaluated = sum(status[item] for item in _EVALUATED_STATUSES)

        def zone_raise(zone: Zone) -> ZoneRaise:
            widths = self.raises[zone]
            positive = [width for width in widths if width > 0.0]
            return ZoneRaise(
                raised_ticks=len(positive),
                toggles=self.toggles[zone],
                all_ticks=summarize(widths, quantiles=percentiles),
                raised_only=summarize(positive, quantiles=percentiles),
            )

        return AirBalanceShadowReport(
            period=period,
            config=config,
            percentiles=percentiles,
            mode=mode,
            ticks=self.ticks,
            first_ts_ms=self.first_ts_ms,
            last_ts_ms=self.last_ts_ms,
            status=StatusCounts(**{item.value: count for item, count in status.items()}),
            skip_reason=SkipReasonCounts(
                **{item.value: count for item, count in self.skip_reason.items()}
            ),
            evaluated_ticks=evaluated,
            any_zone=AnyZoneRaise(raised_ticks=self.any_raised, toggles=self.any_toggles),
            zones=PerZone[ZoneRaise](
                front=zone_raise(Zone.FRONT), rear=zone_raise(Zone.REAR), top=zone_raise(Zone.TOP)
            ),
            fan_fault_skips=PerZone[int](
                front=self.fault_skips[Zone.FRONT],
                rear=self.fault_skips[Zone.REAR],
                top=self.fault_skips[Zone.TOP],
            ),
        )


def config_binding(control: ControlConfig) -> AirBalanceShadowConfigBinding:
    """照合に使う Control Config の hash（PR #220 の束縛と同じ3つ。0093 §2.1.1）。"""
    sources = control.sources
    return AirBalanceShadowConfigBinding(
        fan_policy_sha256=sources.policy.sha256,
        air_balance_sha256=sources.air_balance.sha256,
        fan_hardware_sha256=sources.fan_hardware.sha256,
    )


def build_report(
    traces: Iterable[ControlTraceRecord],
    *,
    binding: AirBalanceShadowConfigBinding,
    period: AirBalanceShadowPeriod,
    settings: AirBalanceShadowConfig,
) -> AirBalanceShadowReport:
    """trace の行を数えて報告を作る（0093 §2.1）。

    次のどれかがあれば **run 全体を拒否する**（`AirBalanceShadowInputError`）。

    - period の外の行、索引（`ts_ms` / `tick_id` / `schema_version`）と本文の食い違い、
      本文を読めない
    - `runtime` か `air_balance_coordination` の無い trace（v14 未満）
    - `fan-policy.yaml` / `air-balance.yaml` / `fan-hardware.yaml` の hash が渡した設定と違う tick
    """
    counter = _Counter()
    for row in sorted(traces, key=lambda item: (item.ts_ms, item.tick_id)):
        where = f"ts_ms={row.ts_ms}; tick_id={row.tick_id}"
        if not period.start_ms <= row.ts_ms < period.end_ms:
            raise AirBalanceShadowInputError(f"period の外の trace が渡された（{where}）")
        try:
            tick = ControlTick.model_validate_json(row.trace_json)
        except ValidationError as error:
            raise AirBalanceShadowInputError(
                f"decision trace の本文を読めない（{where}）"
            ) from error
        if (tick.ts_ms, tick.tick_id, tick.schema_version) != (
            row.ts_ms,
            row.tick_id,
            row.schema_version,
        ):
            raise AirBalanceShadowInputError(f"decision trace の索引と中身が一致しない（{where}）")
        record = tick.air_balance_coordination
        if tick.runtime is None or record is None:
            raise AirBalanceShadowInputError(
                "協調の記録の無い（v14 未満の）decision trace は集計できない"
                f"（{where}; version={tick.schema_version}）"
            )
        config = tick.runtime.config
        recorded = (config.policy_sha256, config.air_balance_sha256, config.fan_hardware_sha256)
        expected = (
            binding.fan_policy_sha256,
            binding.air_balance_sha256,
            binding.fan_hardware_sha256,
        )
        if recorded != expected:
            # max_raise・mode・較正・stable demand の違う区間を1つの分布に混ぜない。
            raise AirBalanceShadowInputError(
                "decision trace の Control Config の hash"
                "（fan-policy / air-balance / fan-hardware）"
                f"が渡した設定と違う（{where}）。期間を分けて渡す"
            )
        counter.add(tick, record)
    return counter.report(period=period, config=binding, percentiles=settings.percentiles)


# ------------------------------------------------------------------ Markdown

_ABSENT = "—"


def _number(value: float | None) -> str:
    """数値を**報告の JSON と同じ字面**で出す。丸めない。"""
    if value is None:
        return _ABSENT
    return to_json(value).decode()


def _code(value: str) -> str:
    return f"`{value}`"


def _table(header: tuple[str, ...], body: Sequence[tuple[str, ...]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(cells) + " |" for cells in body)
    return lines


def _summary_cells(summary: MetricSummary | None, quantiles: tuple[float, ...]) -> tuple[str, ...]:
    if summary is None:
        return (_ABSENT,) * (4 + len(quantiles))
    values = {item.quantile: item.value for item in summary.percentiles}
    return (
        _number(summary.count),
        _number(summary.mean),
        _number(summary.minimum),
        *(_number(values.get(quantile)) for quantile in quantiles),
        _number(summary.maximum),
    )


def render_markdown(report: AirBalanceShadowReport) -> str:
    """報告を Markdown へ。**報告から写すだけで、新しい計算も判定もしない。**"""
    quantiles = report.percentiles
    distribution_header = (
        "zone",
        "対象",
        "count",
        "mean",
        "min",
        *(f"p{_number(quantile)}" for quantile in quantiles),
        "max",
    )
    distribution: list[tuple[str, ...]] = []
    for zone in Zone:
        item = report.zones.get(zone)
        distribution.append((_code(zone.value), "全件", *_summary_cells(item.all_ticks, quantiles)))
        distribution.append(
            (_code(zone.value), "上げた tick", *_summary_cells(item.raised_only, quantiles))
        )
    lines = [
        "# Air Balance の協調の shadow 集計",
        "",
        "**合否は出さない。** shadow → apply の基準は所有者と決める（決定記録 0078 §5 / 0093）。",
        "",
        *_table(
            ("項目", "値"),
            [
                ("mode", _ABSENT if report.mode is None else _code(report.mode.value)),
                ("period", f"[{report.period.start_ms}, {report.period.end_ms})"),
                ("ticks", _number(report.ticks)),
                ("first_ts_ms", _number(report.first_ts_ms)),
                ("last_ts_ms", _number(report.last_ts_ms)),
                ("evaluated_ticks", _number(report.evaluated_ticks)),
                ("fan_policy_sha256", _code(report.config.fan_policy_sha256)),
                ("air_balance_sha256", _code(report.config.air_balance_sha256)),
                ("fan_hardware_sha256", _code(report.config.fan_hardware_sha256)),
                ("schema_version", _number(report.schema_version)),
            ],
        ),
        "",
        "## status ごとの件数",
        "",
        *_table(
            ("status", "ticks"),
            [(_code(key), _number(value)) for key, value in report.status.model_dump().items()],
        ),
        "",
        "## skip_reason ごとの件数",
        "",
        *_table(
            ("skip_reason", "ticks"),
            [
                (_code(key), _number(value))
                for key, value in report.skip_reason.model_dump().items()
            ],
        ),
        "",
        "## zone の Fan fault で skipped になった tick",
        "",
        "`zone_fan_fault` と、同じ tick の `faults` にその zone の Fan fault がある"
        " `safety_state` を数える"
        "（Top の Fan fault は `safety_state` になる。決定記録 0088 §3）。",
        "",
        *_table(
            ("zone", "ticks"),
            [(_code(zone.value), _number(report.fan_fault_skips.get(zone))) for zone in Zone],
        ),
        "",
        "## 引き上げの有無と切り替わり",
        "",
        *_table(
            ("zone", "上げた tick", "切り替わり"),
            [
                (
                    _code(zone.value),
                    _number(report.zones.get(zone).raised_ticks),
                    _number(report.zones.get(zone).toggles),
                )
                for zone in Zone
            ]
            + [
                (
                    "どれか",
                    _number(report.any_zone.raised_ticks),
                    _number(report.any_zone.toggles),
                )
            ],
        ),
        "",
        "## 引き上げ幅（counterfactual_output - candidate）の分布",
        "",
        *_table(distribution_header, distribution),
    ]
    return "\n".join(lines) + "\n"


ReportFormat = Literal["json", "markdown"]
REPORT_FORMATS: tuple[ReportFormat, ...] = ("json", "markdown")
DEFAULT_OUT: dict[ReportFormat, Path] = {
    "json": Path("var/air-balance-shadow.json"),
    "markdown": Path("var/air-balance-shadow.md"),
}
_RENDERERS: dict[ReportFormat, Callable[[AirBalanceShadowReport], str]] = {
    "json": render,
    "markdown": render_markdown,
}


def write(
    report: AirBalanceShadowReport, path: Path, *, report_format: ReportFormat = "json"
) -> Path:
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


_SQLITE_SIDECARS = ("-wal", "-journal", "-shm")
"""証拠の DB の添え file。`--out` がこれらを指しても DB を壊しうる。"""


def control_config_files(directory: Path) -> tuple[Path, ...]:
    """`--control-config` の4ファイル（`--out` で上書きさせない入力）。"""
    return tuple(
        directory / name
        for name in ("fan-hardware.yaml", "safety.yaml", "fan-policy.yaml", "air-balance.yaml")
    )


def _same_file(left: Path, right: Path) -> bool:
    if left.exists() and right.exists():
        return os.path.samefile(left, right)
    return left.resolve() == right.resolve()


def check_out_is_not_an_input(out: Path, *, db: Path, inputs: Iterable[Path]) -> None:
    """`--out` が入力（証拠の DB とその添え file・manifest・設定）を指していれば拒む。

    `write()` は `os.replace()` で置き換えるので、`--out` が DB を指すと**証拠を報告で
    上書きする**。読むだけの CLI を破壊的な操作にしないため、読む前に拒む。
    """
    candidates = [
        db,
        *(db.with_name(db.name + suffix) for suffix in _SQLITE_SIDECARS),
        *inputs,
    ]
    for path in candidates:
        if _same_file(out, path):
            raise AirBalanceShadowInputError(f"--out が入力のファイルを指している: {path}")


def build_parser() -> argparse.ArgumentParser:
    """CLI の引数。**合否の閾値を受け取る口は作らない**（0093 §2.1）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-air-balance-shadow",
        description=(
            "保存済み decision trace から Air Balance の協調の shadow 集計を作る"
            "（#81 / 決定記録 0093）。**読むだけで、合否は出さない**"
        ),
    )
    parser.add_argument("--evidence", type=Path, required=True, help="期間を名指す manifest")
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument(
        "--control-config",
        type=Path,
        default=Path("config"),
        help=(
            "fan-hardware.yaml / safety.yaml / fan-policy.yaml / air-balance.yaml"
            " のあるディレクトリ（4ファイルとも必須。trace の hash と照合する）"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/air-balance-shadow.yaml"),
        help="集計の表し方（percentile）",
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
        help=(
            "既定は json なら var/air-balance-shadow.json、markdown なら var/air-balance-shadow.md"
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI の入口。拒否した run では何も書かずに 1 を返す。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)

    manifest = AirBalanceShadowManifest.from_file(args.evidence)
    settings = AirBalanceShadowConfig.from_file(args.config)
    control = ControlConfig.from_directory(args.control_config)
    report_format: ReportFormat = args.report_format
    out: Path = args.out if args.out is not None else DEFAULT_OUT[report_format]
    period = manifest.period
    try:
        check_out_is_not_an_input(
            out,
            db=args.db,
            inputs=(args.evidence, args.config, *control_config_files(args.control_config)),
        )
        # **証拠の DB は読み取り専用で開く**（`immutable=1`。添え file に中身があれば開かない）。
        with EvidenceDatabase(args.db) as store, store.snapshot():
            traces = store.control_traces(period.start_ms, period.end_ms)
        report = build_report(
            traces, binding=config_binding(control), period=period, settings=settings
        )
    except AirBalanceShadowInputError as error:
        LOGGER.error(
            "Air Balance の shadow 集計を拒否した（何も書かない）",
            extra={
                logs.FIELDS_KEY: {
                    "reason": str(error),
                    "out": str(out),
                    "stale_out_exists": out.exists(),
                    "start_ms": period.start_ms,
                    "end_ms": period.end_ms,
                }
            },
        )
        return 1
    path = write(report, out, report_format=report_format)
    LOGGER.info(
        "Air Balance の shadow 集計を書き出した",
        extra={
            logs.FIELDS_KEY: {
                "path": str(path),
                "format": report_format,
                "mode": None if report.mode is None else report.mode.value,
                "ticks": report.ticks,
                "evaluated_ticks": report.evaluated_ticks,
                "fan_policy_sha256": report.config.fan_policy_sha256,
            }
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
