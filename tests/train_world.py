"""``coldaisle-train`` の試験の世界（本番の DB・元の CSV・公開した Dataset v2・設定）を作る。

``tests/test_training_entry.py`` の合成の作り方を、学習できる長さ（train と validation が
それぞれ1件以上）に伸ばしたもの。実機は要らない（0150 §2.9）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from coldaisle.clock import SimulatedClock
from coldaisle.control import ControlTraceLogger
from coldaisle.control.model.dataset import DatasetSourceKind, SourceRun, ThermalDatasetV2
from coldaisle.control.model_registry import ModelRegistryLimits
from coldaisle.daemon import Daemon
from coldaisle.dataset import ThermalDatasetV2Builder, write_dataset
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.normalize import Normalizer
from coldaisle.ingest.replay import ReplaySource, read_replay_export_inputs
from coldaisle.store import Quality, QualityRules, Reading, Sample, SqliteStore
from coldaisle.store.csv_export import export_day
from coldaisle.store.export_binding import read_training_production
from test_thermal_dataset_v2 import (
    COVERING_ROW_MS,
    NO_CHANGES,
    RUN_ALIAS,
    SOURCE_ALIAS,
    T0,
    history_db,
    spec_v2,
    tick,
)

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config"
UTC = ZoneInfo("UTC")
DAY = date(1970, 1, 1)
END_MS = 30_000
ALIAS = "dataset-" + "a" * 32
VALIDATION_START_MS = T0 + 15_000
TEST_START_MS = T0 + 24_000
CREATED_AT = "2026-10-20T00:00:00+00:00"
CODE_COMMIT = "0123abc"

TRAINING_SPEC = f"""\
schema_version: 1
validation_start_ms: {VALIDATION_START_MS}
test_start_ms: {TEST_START_MS}
ridge_lambda: 1.0
support_axes:
  - source: fan.front
    edges: [0.5]
  - source: fan.rear
    edges: [0.5]
  - source: fan.top
    edges: [0.5]
residual_scale_floor: 0.01
"""


@dataclass(frozen=True)
class World:
    """1回の学習に要る入力の置き場所。"""

    root: Path
    prod: Path
    replay: Path
    dataset: Path
    declared: Path
    quality: Path
    calibration: Path
    metrics: Path
    spec: Path
    outputs: Path
    registry: Path
    limits: Path

    def argv(self, out: Path | None = None, **overrides: str | None) -> list[str]:
        """``coldaisle-train`` の引数（``overrides`` の値 ``None`` はその引数を外す）。"""
        values: dict[str, str | None] = {
            "--dataset": str(self.dataset),
            "--replay-path": str(self.replay),
            "--calibration-history-db": str(self.prod),
            "--declared-changes": str(self.declared),
            "--quality-config": str(self.quality),
            "--calibration": str(self.calibration),
            "--metrics-config": str(self.metrics),
            "--training-spec": str(self.spec),
            "--model-id": "rack-thermal",
            "--model-version": "0.1.0",
            "--created-at": CREATED_AT,
            "--out": str(out if out is not None else self.outputs / "run"),
        }
        values.update(overrides)
        argv: list[str] = []
        for key, value in values.items():
            if value is not None:
                argv += [key, value]
        return argv

    def apply_argv(self, out: Path | None = None, **overrides: str | None) -> list[str]:
        """``--apply`` つきの引数。"""
        extra: dict[str, str | None] = {
            "--registry-root": str(self.registry),
            "--registry-limits": str(self.limits),
            "--actor": "trainer",
            "--reason": "training completed",
            "--code-commit": CODE_COMMIT,
        }
        extra.update(overrides)
        return [*self.argv(out), "--apply", *_flatten(extra)]


def _flatten(values: dict[str, str | None]) -> list[str]:
    flat: list[str] = []
    for key, value in values.items():
        if value is not None:
            flat += [key, value]
    return flat


def limits() -> ModelRegistryLimits:
    """本番と同じ ``config/model-registry.yaml``。"""
    return ModelRegistryLimits.from_file(CONFIG / "model-registry.yaml")


def _write_readings(prod: Path, rules: QualityRules) -> None:
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        for offset in range(0, END_MS + 1, 1_000):
            store.insert_sample(
                Sample(
                    ts_ms=T0 + offset,
                    readings=(
                        Reading(metric="air.room", value=20.0 + offset / 1_000, quality=Quality.OK),
                        Reading(
                            metric="air.gpu_exhaust",
                            value=40.0 + offset / 2_000,
                            quality=Quality.OK,
                        ),
                    ),
                )
            )


def _record_ticks(prod: Path, rules: QualityRules) -> None:
    # 値は2 tick ごとに変える（同じ値が続く step の組も、変わる組も train に入る）
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        logger = ControlTraceLogger(store)
        for index, ts_ms in enumerate(range(4_000, END_MS + 1, 1_000)):
            value = (0.3, 0.3, 0.7, 0.7)[index % 4]
            logger.record(tick(T0 + ts_ms, 10 + index, (value, 1.0 - value, value)))


def _build(out: Path, prod: Path, rules: QualityRules, dedicated: Path) -> ThermalDatasetV2:
    replay = ReplaySource(out, tz=UTC, timezone_explicit=False, bulk=True, dataset_provenance=True)
    inputs = read_replay_export_inputs(out)
    assert inputs.records is not None
    production = read_training_production(prod, inputs.records, start_ms=T0, end_ms=T0 + END_MS + 1)
    with SqliteStore(dedicated, rules=rules, clock=replay.clock) as store:
        Daemon(
            source=replay,
            store=store,
            normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
            source_name="replay",
            dataset_run_alias=RUN_ALIAS,
        ).run()
        store.copy_control_traces(production.traces)
        return ThermalDatasetV2Builder(store).build(
            source_run=SourceRun(
                run_id=RUN_ALIAS,
                kind=DatasetSourceKind.REPLAY,
                start_ms=T0,
                end_ms=T0 + END_MS + 1,
                source_refs=(SOURCE_ALIAS,),
                source_sha256=inputs.source_sha256,
            ),
            spec=spec_v2(),
            declared_changes=NO_CHANGES,
            calibration_history=production.history,
            export_binding=production.binding,
        )


def make_world(root: Path, rules: QualityRules) -> World:
    """本番の DB・その日の export・公開した Dataset v2・設定ファイルを作る。"""
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)  # umask に依らず group / other が書けない祖先にする
    prod = Path(history_db(root / "prod.db", COVERING_ROW_MS))
    _write_readings(prod, rules)
    _record_ticks(prod, rules)
    replay = root / "csv"
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        export_day(store, DAY, tz=UTC, out_dir=replay, lock_timeout_s=5.0)
    dataset = _build(replay, prod, rules, root / "dedicated.db")
    manifest, _examples = write_dataset(dataset, root / "datasets", ALIAS)
    declared = root / "declared-changes.yaml"
    declared.write_text("schema_version: 1\nchanges: []\n", encoding="utf-8")
    calibration = root / "calibration.json"
    calibration.write_text(json.dumps({"offsets_c": {}}), encoding="utf-8")
    spec = root / "training.yaml"
    spec.write_text(TRAINING_SPEC, encoding="utf-8")
    outputs = root / "outputs"
    outputs.mkdir(mode=0o755)
    outputs.chmod(0o755)
    return World(
        root=root,
        prod=prod,
        replay=replay,
        dataset=manifest.parent,
        declared=declared,
        quality=CONFIG / "quality.yaml",
        calibration=calibration,
        metrics=CONFIG / "metrics.yaml",
        spec=spec,
        outputs=outputs,
        registry=root / "registry",
        limits=CONFIG,
    )
