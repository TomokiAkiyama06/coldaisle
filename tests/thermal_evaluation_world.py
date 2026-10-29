"""``coldaisle-thermal-evaluate`` の試験の世界（決定記録 0172 §2.9）。実機は要らない。

``tests/train_world.py`` の作り方を、**未来が action に依存する**合成の系に替えたもの:

- GPU 排気（``air.gpu_exhaust``）は 1 s 前に掛かっていた front の effective demand で決まる
  （``45 + 10 × (0.5 − demand)``）。demand は 2 tick ごとに 0.3 / 0.7 を往復する
- 排気を feature にも target にも入れる（``persistence`` を作れるように）
- test が数ブロックに分かれる長さにする

この系では、実際に適用された action 列を使う model は排気をほぼ当て、``persistence``（anchor の
時点の値）は demand が変わるたびに 4 ℃ 外れる。
"""

from __future__ import annotations

import json
from pathlib import Path

from coldaisle.clock import SimulatedClock
from coldaisle.control import ControlTraceLogger
from coldaisle.control.model.dataset import DatasetSourceKind, SourceRun, ThermalDatasetV2
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
from train_world import ALIAS, CONFIG, DAY, UTC, World

END_MS = 120_000
FIRST_TICK_MS = 4_000
VALIDATION_START_MS = T0 + 60_000
TEST_START_MS = T0 + 90_000
PATTERN = (0.3, 0.3, 0.7, 0.7)
FEATURES = ("air.room", "air.gpu_exhaust")
TARGET = "air.gpu_exhaust"

TRAINING_SPEC = f"""\
schema_version: 1
validation_start_ms: {VALIDATION_START_MS}
test_start_ms: {TEST_START_MS}
ridge_lambda: 0.001
support_axes:
  - source: fan.front
    edges: [0.5]
  - source: fan.rear
    edges: [0.5]
  - source: fan.top
    edges: [0.5]
residual_scale_floor: 0.01
"""

EVALUATION_CONFIG = """\
schema_version: 1
outputs:
  - {metric: air.gpu_exhaust, direction: higher_is_worse}
quantiles: [0.5, 0.9]
worst_case_count: 3
block_ms: {value: 4000, status: provisional}
room_temperature_metric: air.room
room_temperature_bands:
  - name: cool
    below_c: {value: 20.5, status: provisional}
  - name: warm
workload_bands:
  window_ms: {value: 3000, status: provisional}
  cpu_metric: power.cpu.package
  gpu_metric: power.gpu.0
  cpu_power:
    - name: idle
      below_w: {value: 50.0, status: provisional}
    - name: busy
  gpu_power:
    - name: idle
      below_w: {value: 30.0, status: provisional}
    - name: busy
minimum:
  test_examples: {value: 10, status: provisional}
  blocks: {value: 3, status: provisional}
  stratum_examples: {value: 5, status: provisional}
  stratum_blocks: {value: 2, status: provisional}
  predicted_fraction: {value: 0.9, status: provisional}
gate:
  skill:
    alpha: {value: 0.1, status: provisional}
    minimum_mae_improvement_fraction: {value: 0.5, status: provisional}
  underprediction:
    quantile: {value: 0.9, status: provisional}
    maximum_c: {value: 1.0, status: provisional}
    worst_case_maximum_c: {value: 2.0, status: provisional}
  stratum_regression_tolerance_c: {value: 0.1, status: provisional}
"""


def demand_at(offset_ms: int) -> float | None:
    """``T0 + offset_ms`` に掛かっていた front の effective（最初の tick の前は ``None``）。"""
    if offset_ms < FIRST_TICK_MS:
        return None
    index = (min(offset_ms, END_MS) - FIRST_TICK_MS) // 1_000
    return PATTERN[index % len(PATTERN)]


def exhaust_at(offset_ms: int) -> float:
    """1 s 前に掛かっていた demand で決まる排気温度。"""
    demand = demand_at(offset_ms - 1_000)
    return 45.0 if demand is None else 45.0 + 10.0 * (0.5 - demand)


def _write_readings(prod: Path, rules: QualityRules) -> None:
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        for offset in range(0, END_MS + 1, 1_000):
            store.insert_sample(
                Sample(
                    ts_ms=T0 + offset,
                    readings=(
                        Reading(
                            metric="air.room", value=20.0 + offset / 100_000, quality=Quality.OK
                        ),
                        Reading(metric=TARGET, value=exhaust_at(offset), quality=Quality.OK),
                    ),
                )
            )


def _record_ticks(prod: Path, rules: QualityRules) -> None:
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        logger = ControlTraceLogger(store)
        for index, ts_ms in enumerate(range(FIRST_TICK_MS, END_MS + 1, 1_000)):
            value = PATTERN[index % len(PATTERN)]
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
            spec=spec_v2(feature_metrics=FEATURES, target_metrics=(TARGET,)),
            declared_changes=NO_CHANGES,
            calibration_history=production.history,
            export_binding=production.binding,
        )


def make_world(root: Path, rules: QualityRules) -> World:
    """本番の DB・その日の export・公開した Dataset v2・設定ファイルを作る（学習はしない）。"""
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
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
    (root / "thermal-evaluation.yaml").write_text(EVALUATION_CONFIG, encoding="utf-8")
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


def evaluation_config(world: World) -> Path:
    """試験の評価の設定（仮の値）。"""
    return world.root / "thermal-evaluation.yaml"


def evaluate_argv(world: World, out: Path, **overrides: str | None) -> list[str]:
    """``coldaisle-thermal-evaluate`` の引数（``overrides`` の値 ``None`` はその引数を外す）。"""
    values: dict[str, str | None] = {
        "--registry-root": str(world.registry),
        "--registry-limits": str(world.limits),
        "--artifact": "rack-thermal@0.1.0",
        "--dataset": str(world.dataset),
        "--validation-start-ms": str(VALIDATION_START_MS),
        "--test-start-ms": str(TEST_START_MS),
        "--evaluation-config": str(evaluation_config(world)),
        "--metrics-config": str(world.metrics),
        "--calibration": str(world.calibration),
        "--out": str(out),
    }
    values.update(overrides)
    argv: list[str] = []
    for key, value in values.items():
        if value is not None:
            argv += [key, value]
    return argv
