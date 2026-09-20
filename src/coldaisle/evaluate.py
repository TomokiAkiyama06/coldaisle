"""合成の起点: Offline Evaluation（#91 / 決定記録 0054）。

保存済み decision trace と観測を読み、Controller 構成の比較レポートを1つ書き出す。
**1コマンドで再現できる**ように、比べる run は manifest（YAML）で与える。

```bash
uv run coldaisle-evaluate --runs config/evaluation-runs.yaml --out var/evaluation.json
```

**書き込みも制御もしない。** Store は読み取りだけに使い、Fan へ届く経路は持たない。
レポートに生成時刻は入らないので、同じ入力からは同じ bytes が出る（0054 §2.7）。
"""

from __future__ import annotations

import argparse
import logging
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle import logs
from coldaisle.clock import WallClock
from coldaisle.control.acoustic import ConfiguredAcousticCostModel
from coldaisle.control.config import ControlConfig
from coldaisle.control.evaluation import (
    EvaluationConfig,
    EvaluationContext,
    EvaluationReport,
    EvaluationRun,
    evaluate,
)
from coldaisle.control.schema import ControlTick
from coldaisle.control.shadow import OutcomeObservation, read_shadow_jsonl
from coldaisle.metrics import MetricCatalog
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.models import ControlTraceRecord

LOGGER = logging.getLogger("coldaisle.evaluate")

RUNS_MANIFEST_VERSION: Literal[1] = 1


def _sequence_to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class _Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RunSpec(_Manifest):
    """比較する run 1つ。**時刻は明示する**（「直近」のような相対指定を置かない）。"""

    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    split_boundaries_ms: Annotated[tuple[int, ...], BeforeValidator(_sequence_to_tuple)] = ()
    """時系列 split の境界。**最後の区間が holdout**（決定記録 0054 §2.5）。"""
    shadow_jsonl: str | None = Field(default=None, min_length=1, max_length=500)
    """#90 の export。省略すると同じ照合器で作り直す。"""

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError(f"{self.run_id}: start_ms < end_ms にする")
        boundaries = self.split_boundaries_ms
        if tuple(sorted(set(boundaries))) != boundaries:
            raise ValueError(f"{self.run_id}: split_boundaries_ms は重複なし昇順にする")
        if any(not self.start_ms < boundary < self.end_ms for boundary in boundaries):
            raise ValueError(f"{self.run_id}: split の境界を run の範囲の中に置く")
        return self


class RunsManifest(_Manifest):
    """比較する run 一式。"""

    schema_version: Literal[1]
    runs: Annotated[tuple[RunSpec, ...], BeforeValidator(_sequence_to_tuple), Field(min_length=1)]

    @model_validator(mode="after")
    def _run_ids_are_unique(self) -> Self:
        ids = tuple(run.run_id for run in self.runs)
        if len(set(ids)) != len(ids):
            raise ValueError("同じ run_id の run を2つ置かない")
        return self

    @classmethod
    def from_file(cls, path: Path) -> RunsManifest:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"run manifest が辞書ではない: {path.name}")
        return cls.model_validate(loaded)


def load_run(
    store: SqliteStore, spec: RunSpec, *, context: EvaluationContext, base: Path
) -> EvaluationRun:
    """1つの run の trace と観測を読む。**必要な metric は記録から決める。**

    予測の対象 metric は `ShadowPrediction` に記録されているものを使う。評価設定に
    写すと、モデルの target を変えたときに評価だけが古い metric を見続ける。
    """
    traces = store.control_traces(spec.start_ms, spec.end_ms)
    wanted = set(context.config.temperature_metrics)
    wanted.add(context.config.room_temperature_metric)
    for delta in context.deltas:
        wanted.update({delta.minuend, delta.subtrahend})
    wanted.update(_predicted_metrics(traces))
    observations: list[OutcomeObservation] = []
    for metric in sorted(wanted):
        observations.extend(
            OutcomeObservation(
                metric=metric, ts_ms=point.ts_ms, value=point.value, quality=point.quality
            )
            for point in store.series(metric, spec.start_ms, spec.end_ms)
        )
    shadow = None
    if spec.shadow_jsonl is not None:
        with (base / spec.shadow_jsonl).open(encoding="utf-8") as stream:
            shadow = read_shadow_jsonl(stream)
    return EvaluationRun(
        run_id=spec.run_id,
        traces=tuple(traces),
        observations=tuple(observations),
        split_boundaries_ms=spec.split_boundaries_ms,
        shadow=shadow,
    )


def _predicted_metrics(traces: tuple[ControlTraceRecord, ...]) -> set[str]:
    """counterfactual が予測した metric（照合に要る観測を読むため）。"""
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


def build_context(
    *,
    config_path: Path,
    control_dir: Path,
    metrics_path: Path,
    acoustic_path: Path | None,
) -> EvaluationContext:
    """設定一式を読み、素性（hash）とともに束ねる。"""
    config, config_sha256 = EvaluationConfig.from_file(config_path)
    catalog = MetricCatalog.from_yaml(metrics_path)
    catalog_sha256 = sha256(metrics_path.read_bytes()).hexdigest()
    acoustic = None
    acoustic_sha256 = None
    if acoustic_path is not None:
        acoustic = ConfiguredAcousticCostModel.from_file(acoustic_path)
        acoustic_sha256 = sha256(acoustic_path.read_bytes()).hexdigest()
    return EvaluationContext.build(
        config=config,
        config_sha256=config_sha256,
        control=ControlConfig.from_directory(control_dir),
        catalog=catalog,
        catalog_sha256=catalog_sha256,
        acoustic=acoustic,
        acoustic_sha256=acoustic_sha256,
    )


def render(report: EvaluationReport) -> str:
    """レポートを JSON へ。**無い値は書かない**（0 と区別できる形にする。0053 §2.4）。"""
    return report.model_dump_json(exclude_none=True, indent=2) + "\n"


def write(report: EvaluationReport, path: Path) -> Path:
    """レポートを書き出す。同じ入力からは同じ bytes になる。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coldaisle-evaluate",
        description="Offline Evaluation: Controller 構成の比較レポート（#91）",
    )
    parser.add_argument("--runs", type=Path, required=True, help="比較する run の manifest")
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--config", type=Path, default=Path("config/evaluation.yaml"))
    parser.add_argument(
        "--control-config",
        type=Path,
        default=Path("config"),
        help="fan-hardware.yaml / safety.yaml / fan-policy.yaml のあるディレクトリ",
    )
    parser.add_argument("--metrics", type=Path, default=Path("config/metrics.yaml"))
    parser.add_argument("--quality-rules", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument(
        "--acoustic",
        type=Path,
        default=None,
        help="acoustic.yaml。省略すると Acoustic Cost は出さない（欠測として残す）",
    )
    parser.add_argument("--out", type=Path, default=Path("var/evaluation.json"))
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)

    manifest = RunsManifest.from_file(args.runs)
    context = build_context(
        config_path=args.config,
        control_dir=args.control_config,
        metrics_path=args.metrics,
        acoustic_path=args.acoustic,
    )
    store = SqliteStore(
        args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=WallClock()
    )
    try:
        with store.read_snapshot():
            runs = [
                load_run(store, spec, context=context, base=args.runs.parent)
                for spec in manifest.runs
            ]
    finally:
        store.close()

    report = evaluate(runs, context=context)
    path = write(report, args.out)
    LOGGER.info(
        "Offline Evaluation のレポートを書き出した",
        extra={
            logs.FIELDS_KEY: {
                "path": str(path),
                "runs": len(report.provenance.runs),
                "segments": len(report.segments),
                "conditions_sha256": report.provenance.conditions_sha256,
                "gates": {gate.arm_key: gate.outcome.value for gate in report.gates},
            }
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
