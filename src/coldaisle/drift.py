"""合成の起点: Thermal Model の drift 検知（#93 / 決定記録 0055）。

保存済み decision trace と観測、Confidence Profile、直近の Dataset から、Production model に
対する drift の報告を1つ書き出す。**1コマンドで再現できる**ように、見る期間と宣言された
構成変更は manifest（YAML）で与える。

```bash
uv run coldaisle-drift --evidence config/drift-evidence.yaml \\
  --profile var/confidence-profile.json --out var/drift.json
```

**書き込みも制御もしない。** Store は読み取りだけに使い、Fan へ届く経路は持たない。
出すのは報告と**再学習の推奨**だけで、Registry も設定も書き換えない（0055 §2.6）。
報告に生成時刻は入らないので、同じ入力からは同じ bytes が出る。
"""

from __future__ import annotations

import argparse
import logging
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
    DriftDetector,
    DriftEvidence,
    DriftReport,
)
from coldaisle.control.model.confidence import ConfidenceAssessor, ModelConfidenceProfile
from coldaisle.control.model.dataset import DatasetExample, DatasetManifest, examples_sha256
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
    """Shadow 実績を読む。**照合の許容幅は `fan-policy.yaml` から取る**（写さない）。"""
    if manifest.shadow_jsonl is not None:
        with (base / manifest.shadow_jsonl).open(encoding="utf-8") as stream:
            return read_shadow_jsonl(stream)
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
    return tuple(shadow_rows(traces, observations=tuple(observations), matcher=matcher))


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


def load_inputs(directory: Path) -> tuple[ObservedThermalInput, ...]:
    """Dataset artifact から推論入力を起こす。**bytes を manifest と照らしてから使う。**"""
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
    return tuple(ObservedThermalInput.from_example(example) for example in examples)


def render(report: DriftReport) -> str:
    """報告を JSON へ。**欄を落とさない**（`canonical_bytes` と同じ内容を読みやすく）。"""
    return report.model_dump_json(indent=2) + "\n"


def write(report: DriftReport, path: Path) -> Path:
    """報告を書き出す。同じ入力からは同じ bytes になる。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report), encoding="utf-8")
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
    parser.add_argument("--out", type=Path, default=Path("var/drift.json"))
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
        config_sha256=config_sha256,
    )
    # **証拠の DB は読み取り専用で開く**（#91 と同じ入口を使う。規則を写さない）。
    with EvidenceDatabase(args.db) as store, store.snapshot():
        shadow = load_shadow(store, manifest, control=control, base=args.evidence.parent)
    inputs = (
        () if manifest.dataset is None else load_inputs(args.evidence.parent / manifest.dataset)
    )
    report = detector.detect(DriftEvidence(shadow=shadow, inputs=inputs, changes=manifest.changes))
    path = write(report, args.out)
    LOGGER.info(
        "Model drift の報告を書き出した",
        extra={
            logs.FIELDS_KEY: {
                "path": str(path),
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
