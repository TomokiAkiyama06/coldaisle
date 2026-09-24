"""合成の起点: Thermal Model の drift 検知（#93 / 決定記録 0056）。

保存済み decision trace と観測、Confidence Profile、直近の Dataset から、Production model に
対する drift の報告を1つ書き出す。**1コマンドで再現できる**ように、見る期間と宣言された
構成変更は manifest（YAML）で与える。

```bash
uv run coldaisle-drift --evidence config/drift-evidence.yaml \\
  --profile var/confidence-profile.json --out var/drift.json
uv run coldaisle-drift --evidence config/drift-evidence.yaml \\
  --profile var/confidence-profile.json --format markdown --out var/drift.md
```

`--format markdown` は**同じ報告**を人が読む形（residual trend の表・coverage・推奨）に
写すだけで、判定も数も足さない。保存と比較の正本は JSON である。

**書き込みも制御もしない。** Store は読み取りだけに使い、Fan へ届く経路は持たない。
出すのは報告と**再学習の推奨**だけで、Registry も設定も書き換えない（0056 §2.6）。
報告に生成時刻は入らないので、同じ入力からは同じ bytes が出る。
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle import logs
from coldaisle.control.config import ControlConfig
from coldaisle.control.drift import (
    ChangeKind,
    DeclaredChange,
    DriftConfig,
    DriftCoverage,
    DriftDetector,
    DriftEvidence,
    DriftProvenance,
    DriftReasonCount,
    DriftReport,
    DriftVerdict,
    InputDriftSignal,
    ResidualDriftSignal,
    RetrainingRecommendation,
)
from coldaisle.control.model.confidence import ConfidenceAssessor, ModelConfidenceProfile
from coldaisle.control.model.dataset import (
    DatasetExample,
    DatasetManifest,
    ThermalDataset,
    examples_sha256,
)
from coldaisle.control.model.thermal import ObservedThermalInput
from coldaisle.control.schema import ControlTick
from coldaisle.control.shadow import (
    OutcomeObservation,
    ShadowExportRow,
    ShadowOutcomeMatcher,
    read_shadow_jsonl,
    shadow_rows,
)
from coldaisle.evaluate import EvidenceDatabase
from coldaisle.store.models import ControlTraceRecord

LOGGER = logging.getLogger("coldaisle.drift")

DRIFT_EVIDENCE_MANIFEST_VERSION: Literal[1] = 1

DATASET_MANIFEST_FILENAME = "manifest.json"
DATASET_EXAMPLES_FILENAME = "examples.jsonl"


def _declared_changes(value: object) -> object:
    """YAML の変更一覧を `DeclaredChange` へ正規化する。

    記録の型は strict なので、`kind` の文字列は**ここで明示的に写す**。未知の種別は
    `ChangeKind` が拒む（黙って落とすと、宣言したはずの交換が無かったことになる）。
    """
    if not isinstance(value, list):
        return value
    normalized: list[object] = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("kind"), str):
            normalized.append({**item, "kind": ChangeKind(item["kind"])})
        else:
            normalized.append(item)
    return tuple(normalized)


class _Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class DriftEvidenceManifest(_Manifest):
    """見る期間と、宣言された構成変更。**時刻は明示する**（「直近」を置かない）。"""

    schema_version: Literal[1]
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    shadow_jsonl: str | None = Field(default=None, min_length=1, max_length=500)
    """#90 の export。省略すると同じ照合器で trace から作り直す。"""
    dataset: str | None = Field(default=None, min_length=1, max_length=500)
    """入力分布を見るための Dataset artifact ディレクトリ。省略すると入力分布は判定しない。"""
    changes: Annotated[tuple[DeclaredChange, ...], BeforeValidator(_declared_changes)] = ()

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("start_ms < end_ms にする")
        return self

    @classmethod
    def from_file(cls, path: Path) -> DriftEvidenceManifest:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"drift の証拠 manifest が辞書ではない: {path.name}")
        return cls.model_validate(loaded)


def load_shadow(
    store: EvidenceDatabase,
    manifest: DriftEvidenceManifest,
    *,
    control: ControlConfig,
    base: Path,
) -> tuple[ShadowExportRow, ...]:
    """Shadow 実績を用意する。**照合の許容幅は `fan-policy.yaml` から取る**（写さない）。

    **渡された export も、同じ trace と観測から数え直した結果と照らしてから使う**
    （#91 の `_shadow_rows_for` と同じ扱い）。識別子と許容幅だけを見ても
    `status` / `observed` / `error` / 時刻は書き換えられるので、**採点していない区間を
    `scored` に、外れた予測を当たりに仕立てられる**。照合器は時計も I/O も持たず同じ
    入力から同じ結果を返すので（0053 §2.3）、数え直して閉じられる。一致しなければ
    受け取らず、一致したら**数え直したほうを使う**。
    """
    traces = store.control_traces(manifest.start_ms, manifest.end_ms)
    shadow = control.policy.shadow
    matcher = ShadowOutcomeMatcher(
        match_tolerance_ms=shadow.outcome_match_tolerance_ms.value,
        applied_demand_tolerance=shadow.applied_demand_tolerance.value,
    )
    observations: list[OutcomeObservation] = []
    for metric in sorted(_predicted_metrics(traces)):
        observations.extend(
            OutcomeObservation(
                metric=metric, ts_ms=point.ts_ms, value=point.value, quality=point.quality
            )
            for point in store.series(metric, manifest.start_ms, manifest.end_ms)
        )
    computed = tuple(
        sorted(
            shadow_rows(traces, observations=tuple(observations), matcher=matcher),
            key=_row_order,
        )
    )
    if manifest.shadow_jsonl is None:
        return computed
    with (base / manifest.shadow_jsonl).open(encoding="utf-8") as stream:
        supplied = tuple(sorted(read_shadow_jsonl(stream), key=_row_order))
    _check_supplied_shadow(supplied, computed, manifest)
    # 照らし合わせが済んだら、**数え直したほうを使う。** 1 bit も違わないことを確かめて
    # あるので値は同じで、以降の経路に外から来た object を残さない。
    return computed


def _row_order(row: ShadowExportRow) -> tuple[int, int]:
    return (row.ts_ms, row.tick_id)


def _check_supplied_shadow(
    supplied: tuple[ShadowExportRow, ...],
    computed: tuple[ShadowExportRow, ...],
    manifest: DriftEvidenceManifest,
) -> None:
    """渡された export が、宣言した期間の trace から数え直した結果と1欄ずつ一致するか。"""
    keys = [_row_order(row) for row in supplied]
    if len(set(keys)) != len(keys):
        # 行を複製するだけで outcome と scored が水増しされ、下限を満たせてしまう。
        duplicated = sorted({key for key in keys if keys.count(key) > 1})
        raise ValueError(f"Shadow export に同じ tick の行が複数ある: {duplicated}")
    outside = sorted(key for key in keys if not manifest.start_ms <= key[0] < manifest.end_ms)
    if outside:
        # **空の区間が、古い健全な証拠を借りられないようにする。**
        raise ValueError(
            f"Shadow export に宣言した期間の外の行がある: {outside}"
            f"（[{manifest.start_ms}, {manifest.end_ms}) の外）"
        )
    if supplied == computed:
        return
    for left, right in zip(supplied, computed, strict=False):
        if left == right:
            continue
        raise ValueError(
            f"Shadow export が、同じ trace と観測から数え直した結果と違う"
            f"（tick={left.tick_id}/{left.ts_ms}）。"
            f"照合の結果は記録と観測だけから決まるので、書き換えられた行は受け取らない"
        )
    raise ValueError(
        f"Shadow export の行数が、数え直した結果と違う"
        f"（export={len(supplied)}; 数え直し={len(computed)}）"
    )


def _predicted_metrics(traces: tuple[ControlTraceRecord, ...]) -> set[str]:
    """counterfactual が予測した metric（照合に要る観測を読むため）。

    **評価設定にも drift 設定にも写さない。** モデルの target を変えたときに、
    drift だけが古い metric を見続けることになる。
    """
    metrics: set[str] = set()
    for trace in traces:
        tick = ControlTick.model_validate_json(trace.trace_json)
        if tick.shadow is None:
            continue
        for counterfactual in tick.shadow.counterfactuals:
            if counterfactual.prediction is None:
                continue
            for target in counterfactual.prediction.targets:
                metrics.update(target.values)
    return metrics


def load_inputs(directory: Path, *, start_ms: int, end_ms: int) -> tuple[ObservedThermalInput, ...]:
    """Dataset artifact から推論入力を起こす。

    **bytes と中身の両方を検証する。** checksum だけでは、manifest ごと差し替えた
    dataset が通る。`ThermalDataset` を組み立てて manifest との整合（件数・
    example_id の重複・window / horizon が spec と合うか・source run の期間内か）を
    すべて通す。

    **宣言した期間の外の example は使わない。** 期間を絞って「証拠が無い」はずの区間を
    見ているのに、古い健全な example が入力分布の判定を埋めてしまう。
    """
    manifest = DatasetManifest.model_validate_json(
        (directory / DATASET_MANIFEST_FILENAME).read_bytes()
    )
    examples = tuple(
        DatasetExample.model_validate_json(line)
        for line in (directory / DATASET_EXAMPLES_FILENAME).read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if examples_sha256(examples) != manifest.examples_sha256:
        # 書き換えられた example を「学習範囲の外で運転していた証拠」にしない。
        raise ValueError("Dataset の examples が manifest の checksum と一致しない")
    dataset = ThermalDataset(manifest=manifest, examples=examples)
    return tuple(
        ObservedThermalInput.from_example(example)
        for example in dataset.examples
        if start_ms <= example.action_ts_ms < end_ms
    )


def render(report: DriftReport) -> str:
    """報告を JSON へ。**欄を落とさない**（`canonical_bytes` と同じ内容を読みやすく）。"""
    return report.model_dump_json(indent=2) + "\n"


# ---------------------------------------------------------------- Markdown（人が読む版）

ReportFormat = Literal["json", "markdown"]
REPORT_FORMATS: tuple[ReportFormat, ...] = ("json", "markdown")
DEFAULT_OUT: dict[ReportFormat, Path] = {
    "json": Path("var/drift.json"),
    "markdown": Path("var/drift.md"),
}

_ABSENT = "—"
_WITHHELD = f"{_ABSENT}（証拠不足のため出さない）"

_VERDICT_NOTES: dict[DriftVerdict, str] = {
    DriftVerdict.OK: "閾値に届いていない（証拠は下限を満たしている）",
    DriftVerdict.WARNING: "warning の閾値以上",
    DriftVerdict.DEGRADED: "degraded の閾値以上",
    DriftVerdict.INSUFFICIENT_EVIDENCE: "**証拠が足りず判定できない。`ok` ではない**",
}
"""判定の読み方。**`insufficient_evidence` を「問題なし」と読ませない**（0056 §2.4）。"""


def _number(value: float | None) -> str:
    """数値を**報告の JSON と同じ字面**で出す。丸めない（丸めると報告に無い数が生まれる）。"""
    if value is None:
        return _ABSENT
    return json.dumps(value)


def _text(value: str) -> str:
    """自由記述を表のセル1つに収める。`|` と改行で表の形を壊させない。"""
    escaped = value.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")
    return " ".join(escaped.splitlines()) or _ABSENT


def _code(value: str) -> str:
    """形の決まった識別子（enum・理由コード・digest・metric 名）だけを code span にする。"""
    return f"`{value}`"


def _verdict(verdict: DriftVerdict) -> str:
    return f"{_code(verdict.value)} — {_VERDICT_NOTES[verdict]}"


def _table(header: tuple[str, ...], body: Sequence[tuple[str, ...]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(cells) + " |" for cells in body)
    return lines


def _reasons(counts: tuple[DriftReasonCount, ...]) -> str:
    if not counts:
        return _ABSENT
    return ", ".join(f"{_code(item.code)} × {_number(item.count)}" for item in counts)


def _codes(values: tuple[str, ...]) -> str:
    return ", ".join(_code(value) for value in values) if values else _ABSENT


def _recommendation_summary(recommendation: RetrainingRecommendation) -> str:
    if recommendation.required:
        summary = "**推奨する**（人の承認が要る。この報告は Registry も設定も書き換えない）"
        if recommendation.recharacterization_required:
            summary += "。学習の前に**再 characterization が要る**"
        return summary
    if recommendation.inconclusive_signals:
        # 「推奨しない」を「再学習は不要」と読ませない（0056 §2.6）。
        return (
            "推奨する理由は見つかっていない。"
            "**ただし判定できなかった signal があり、「再学習は不要」とは言えない**"
        )
    return "推奨しない"


def _headline(report: DriftReport) -> list[str]:
    """冒頭の判定。**証拠不足を、報告全体の判定と同じ強さで出す**（0056 §2.4）。"""
    recommendation = report.recommendation
    lines = [f"> **判定: {_verdict(report.verdict)}**"]
    if recommendation.inconclusive_signals:
        lines += [
            ">",
            "> **判定できなかった signal**: "
            + _codes(tuple(kind.value for kind in recommendation.inconclusive_signals))
            + "。これらについては、劣化しているともしていないとも言えない。",
        ]
    coverage = "下限を満たしている" if report.residual.coverage.sufficient else "**足りない**"
    return [
        *lines,
        ">",
        f"> **Residual の coverage**: {coverage}",
        ">",
        f"> **再学習の推奨**: {_recommendation_summary(recommendation)}",
    ]


def _signal_rows(report: DriftReport) -> list[tuple[str, ...]]:
    residual = report.residual
    body: list[tuple[str, ...]] = [
        (
            _code(residual.kind.value),
            _code(residual.verdict.value),
            "ratio",
            _WITHHELD if residual.ratio is None else _number(residual.ratio),
            _number(residual.warning_ratio),
            _number(residual.degraded_ratio),
        )
    ]
    body.extend(
        (
            _code(signal.kind.value),
            _code(signal.verdict.value),
            "fraction",
            _WITHHELD if signal.fraction is None else _number(signal.fraction),
            _number(signal.warning_fraction),
            _number(signal.degraded_fraction),
        )
        for signal in report.inputs
    )
    return body


def _coverage_lines(coverage: DriftCoverage) -> list[str]:
    fraction = (
        f"{_ABSENT}（outcome が無い）"
        if coverage.identifiable_fraction is None
        else _number(coverage.identifiable_fraction)
    )
    body = [
        ("sufficient", "足りている" if coverage.sufficient else "**足りない**"),
        ("outcomes", _number(coverage.outcomes)),
        ("scored", _number(coverage.scored)),
        ("counted（比に数えた）", _number(coverage.counted)),
        ("incomplete_scored（出力が揃わず比に入れない）", _number(coverage.incomplete_scored)),
        ("unidentifiable", _number(coverage.unidentifiable)),
        ("unidentifiable の理由", _reasons(coverage.unidentifiable_reasons)),
        ("identifiable_fraction", fraction),
        ("outputs", _number(coverage.outputs)),
        ("matched_outputs", _number(coverage.matched_outputs)),
        ("counted_outputs", _number(coverage.counted_outputs)),
        ("unmatched の理由", _reasons(coverage.unmatched_reasons)),
        ("predicted_metrics", _codes(coverage.predicted_metrics)),
        ("unscored_metrics（一度も比に数えられなかった）", _codes(coverage.unscored_metrics)),
        ("excluded_by_change", _number(coverage.excluded_by_change)),
        ("purged（期間の外）", _number(coverage.purged)),
        ("foreign_model", _number(coverage.foreign_model)),
    ]
    return _table(("項目", "値"), body)


def _trend_lines(residual: ResidualDriftSignal) -> list[str]:
    """residual trend（#93「residual trend を可視化・保存できる」）。

    **bucket を1つも落とさない。** 比の無い bucket も行として残す。
    """
    if not residual.trend:
        # 比を伏せた signal には trend を付けない（0056 §2.4）。空を「平坦」と読ませない。
        return [
            "**trend は無い。** residual の coverage が足りないため、比も trend も出していない"
            "（証拠不足であって、推移が平坦という意味ではない）。"
        ]
    body = [
        (
            _number(bucket.index),
            _number(bucket.start_ts_ms),
            _number(bucket.end_ts_ms),
            _number(bucket.outcomes),
            f"{_ABSENT}（証拠不足）" if bucket.ratio is None else _number(bucket.ratio),
            _verdict(bucket.verdict),
        )
        for bucket in residual.trend
    ]
    return [
        f"bucket の大きさ（outcome 数）: {_number(residual.trend_bucket_outcomes)}。"
        "各 bucket は**自分の件数だけ**で判定し、足りない bucket は比を持たない。",
        "",
        f"証拠の期間（ms）: {_number(residual.first_evidence_ts_ms)} 〜 "
        f"{_number(residual.last_evidence_ts_ms)}",
        "",
        *_table(("bucket", "start_ts_ms", "end_ts_ms", "outcomes", "ratio", "判定"), body),
    ]


def _per_metric_lines(residual: ResidualDriftSignal) -> list[str]:
    if not residual.per_metric:
        return ["無し（比を出していない signal には metric 別も付けない）。"]
    return _table(
        ("metric", "counted_outputs", "ratio"),
        [
            (_code(item.metric), _number(item.counted_outputs), _number(item.ratio))
            for item in residual.per_metric
        ],
    )


def _input_lines(signal: InputDriftSignal) -> list[str]:
    sources = ", ".join(f"{_text(item.source)} × {_number(item.count)}" for item in signal.sources)
    body = [
        ("判定", _verdict(signal.verdict)),
        ("inputs", _number(signal.inputs)),
        ("considered", _number(signal.considered)),
        ("excluded_by_change", _number(signal.excluded_by_change)),
        ("affected", _number(signal.affected)),
        ("fraction", _WITHHELD if signal.fraction is None else _number(signal.fraction)),
        (
            "入力の期間（ms）",
            f"{_number(signal.first_input_ts_ms)} 〜 {_number(signal.last_input_ts_ms)}",
        ),
        ("出どころ", sources or _ABSENT),
    ]
    return [f"### {_code(signal.kind.value)}", "", *_table(("項目", "値"), body), ""]


def _changes_lines(report: DriftReport) -> list[str]:
    if not report.declared_changes:
        return ["無し。"]
    return _table(
        ("kind", "ts_ms", "detail"),
        [
            (_code(change.kind.value), _number(change.ts_ms), _text(change.detail))
            for change in report.declared_changes
        ],
    )


def _recommendation_lines(recommendation: RetrainingRecommendation) -> list[str]:
    if recommendation.required:
        required = "要"
    elif recommendation.inconclusive_signals:
        required = "推奨しない（判定できなかった signal があり、「不要」とは言えない）"
    else:
        required = "推奨しない"
    lines = _table(
        ("項目", "値"),
        [
            ("required", required),
            (
                "recharacterization_required",
                "要" if recommendation.recharacterization_required else "不要",
            ),
            ("human_approval_required", "要（常に）"),
            (
                "inconclusive_signals（判定できなかった）",
                _codes(tuple(kind.value for kind in recommendation.inconclusive_signals)),
            ),
        ],
    )
    lines.append("")
    if not recommendation.triggers:
        return [*lines, "理由: 無し。"]
    return [
        *lines,
        *_table(
            ("理由", "signal", "evidence_ts_ms", "detail"),
            [
                (
                    _code(trigger.code),
                    _ABSENT if trigger.signal is None else _code(trigger.signal.value),
                    _number(trigger.evidence_ts_ms),
                    _text(trigger.detail),
                )
                for trigger in recommendation.triggers
            ],
        ),
    ]


def _provenance_lines(provenance: DriftProvenance) -> list[str]:
    return _table(
        ("項目", "値"),
        [
            ("drift_config_sha256", _code(provenance.drift_config_sha256)),
            ("profile_sha256", _code(provenance.profile_sha256)),
            ("evidence_sha256", _code(provenance.evidence_sha256)),
            ("shadow_export_schema_version", _number(provenance.shadow_export_schema_version)),
            ("shadow_rows", _number(provenance.shadow_rows)),
            ("window_start_ms", _number(provenance.window_start_ms)),
            ("window_end_ms", _number(provenance.window_end_ms)),
            ("residual_drift_ood_ratio", _number(provenance.residual_drift_ood_ratio)),
            ("range_margin", _number(provenance.range_margin)),
            ("min_support_count", _number(provenance.min_support_count)),
            ("min_missing_pattern_count", _number(provenance.min_missing_pattern_count)),
            ("outcome_match_tolerance_ms", _number(provenance.outcome_match_tolerance_ms)),
            ("applied_demand_tolerance", _number(provenance.applied_demand_tolerance)),
        ],
    )


def render_markdown(report: DriftReport) -> str:
    """報告を Markdown へ。**報告から写すだけで、新しい計算をしない。**

    数値は JSON と同じ字面で出し、丸めない（報告に無い数を作らない）。例外は冒頭の
    「報告の sha256」だけで、これは同じ報告の JSON（`canonical_bytes`）の digest である。
    証拠不足（`insufficient_evidence`・coverage の不足・判定できなかった signal）は
    **冒頭で**判定と同じ強さで出す（0056 §2.4）。保存と比較の正本は JSON（`render`）である。
    """
    target = report.target
    residual = report.residual
    header = _table(
        ("対象", "値"),
        [
            ("model_id", _text(target.model_id)),
            ("model_version", _text(target.model_version)),
            ("artifact_sha256", _code(target.artifact_sha256)),
            ("profile_sha256", _code(target.profile_sha256)),
            ("報告の sha256（JSON の canonical bytes）", _code(report.sha256())),
            ("schema_version", _number(report.schema_version)),
        ],
    )
    signals = _table(("signal", "判定", "指標", "値", "warning", "degraded"), _signal_rows(report))
    lines = [
        "# Model drift 報告",
        "",
        *header,
        "",
        *_headline(report),
        "",
        "## Signal ごとの判定",
        "",
        *signals,
        "",
        "## Residual の coverage",
        "",
        *_coverage_lines(residual.coverage),
        "",
        "## Residual trend",
        "",
        *_trend_lines(residual),
        "",
        "## Metric 別の residual",
        "",
        *_per_metric_lines(residual),
        "",
        "## 入力分布",
        "",
        *(line for signal in report.inputs for line in _input_lines(signal)),
        "## 宣言された構成変更",
        "",
        *_changes_lines(report),
        "",
        "## 再学習の推奨",
        "",
        *_recommendation_lines(report.recommendation),
        "",
        "## 素性",
        "",
        *_provenance_lines(report.provenance),
    ]
    return "\n".join(lines) + "\n"


_RENDERERS: dict[ReportFormat, Callable[[DriftReport], str]] = {
    "json": render,
    "markdown": render_markdown,
}
_RENDERERS = {"json": render, "markdown": render_markdown}


def write(report: DriftReport, path: Path, *, report_format: ReportFormat = "json") -> Path:
    """報告を書き出す。同じ入力からは同じ bytes になる（どちらの形式でも）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_RENDERERS[report_format](report), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coldaisle-drift",
        description="Thermal Model の drift 検知と再学習の推奨（#93）。**書き込みはしない**",
    )
    parser.add_argument("--evidence", type=Path, required=True, help="見る期間と変更の manifest")
    parser.add_argument("--profile", type=Path, required=True, help="Confidence Profile の JSON")
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--config", type=Path, default=Path("config/drift.yaml"))
    parser.add_argument(
        "--control-config",
        type=Path,
        default=Path("config"),
        help="fan-hardware.yaml / safety.yaml / fan-policy.yaml のあるディレクトリ",
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
        help="既定は json なら var/drift.json、markdown なら var/drift.md",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)

    manifest = DriftEvidenceManifest.from_file(args.evidence)
    config, config_sha256 = DriftConfig.from_file(args.config)
    control = ControlConfig.from_directory(args.control_config)
    profile = ModelConfidenceProfile.model_validate_json(args.profile.read_bytes())
    detector = DriftDetector(
        ConfidenceAssessor(profile, control.policy.model_confidence),
        config,
        shadow=control.policy.shadow,
        config_sha256=config_sha256,
    )
    # **証拠の DB は読み取り専用で開く**（#91 と同じ入口を使う。規則を写さない）。
    with EvidenceDatabase(args.db) as store, store.snapshot():
        shadow = load_shadow(store, manifest, control=control, base=args.evidence.parent)
    inputs = (
        ()
        if manifest.dataset is None
        else load_inputs(
            args.evidence.parent / manifest.dataset,
            start_ms=manifest.start_ms,
            end_ms=manifest.end_ms,
        )
    )
    report = detector.detect(
        DriftEvidence(
            shadow=shadow,
            inputs=inputs,
            changes=manifest.changes,
            window_start_ms=manifest.start_ms,
            window_end_ms=manifest.end_ms,
        )
    )
    report_format: ReportFormat = args.report_format
    out: Path = args.out if args.out is not None else DEFAULT_OUT[report_format]
    path = write(report, out, report_format=report_format)
    LOGGER.info(
        "Model drift の報告を書き出した",
        extra={
            logs.FIELDS_KEY: {
                "path": str(path),
                "format": report_format,
                "verdict": report.verdict.value,
                "model_version": report.target.model_version,
                "retraining_required": report.recommendation.required,
                "triggers": [item.code for item in report.recommendation.triggers],
                "report_sha256": report.sha256(),
            }
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
