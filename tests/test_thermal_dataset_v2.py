"""Thermal Dataset v2（#83 / 決定記録 0079 段 1 / 0087）。

合成の ControlTick と Telemetry だけで確かめる（実機は使わない）。既定の筋書きは anchor が
1つだけになるように作ってある（window 5000 ms で anchor は 5000 以降、run の終端で
anchor + 最大の horizon が run に収まるのは 5000 だけ）。除外の件数を1件単位で照合するためである。
"""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.calibration_offsets import effective_metric_offsets
from coldaisle.clock import SimulatedClock
from coldaisle.control import ControlTick, ControlTraceLogger
from coldaisle.control.drift.model import ChangeKind, DeclaredChange
from coldaisle.control.model.dataset import (
    DatasetExampleV2,
    DatasetManifestV2,
    DatasetSourceKind,
    DatasetSpec,
    DatasetSpecV2,
    SourceRun,
    ThermalDataset,
    ThermalDatasetV2,
    examples_sha256,
    split_temporally_v2,
)
from coldaisle.control.model.thermal import ObservedThermalInput
from coldaisle.control.mpc.plan import ActionPlan
from coldaisle.control.schema import PerZone
from coldaisle.dataset import ThermalDatasetBuilder, ThermalDatasetV2Builder, write_dataset
from coldaisle.store import Quality, QualityRules, Reading, Sample, SqliteStore, migrations
from coldaisle.store.calibration_history import (
    CalibrationActivation,
    CalibrationHistory,
    CalibrationHistoryError,
    read_calibration_history,
)
from conftest import QUALITY_RULES_PATH

CONTROL_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "control_tick_v1.json"
MODEL_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control" / "model"
SHA256 = "a" * 64
RUN_ALIAS = "run-00000000000000000000000000000001"
SOURCE_ALIAS = "source-00000000000000000000000000000001"
ARTIFACT_ONE = "dataset-00000000000000000000000000000001"
ARTIFACT_TWO = "dataset-00000000000000000000000000000002"
NO_CHANGES: tuple[DeclaredChange, ...] = ()
T0 = 100_000
"""専用 DB の時刻の起点。筋書きの時刻（tick・readings・source run）はこれからの相対で書く。

較正の記録の行は ``ts_ms >= 0`` で、期間の先頭以前に行が無ければ Dataset v2 を作らない
（決定記録 0099 §2.6 の被覆）。起点を 0 にすると、期間の先頭（0）を覆う行の区間
``[floor, ts]`` が期間に入ってしまう。
"""
COVERING_ROW_MS = 1_000
"""既定の記録の唯一の行の時刻（``T0`` より前で、区間も期間の外）。"""

Demands = tuple[float, float, float]
TickSpec = tuple[int, int, Demands]
"""``(ts_ms, tick_id, (front, rear, top) の effective)``。記録した順（seq）に並べる。"""

A: Demands = (0.5, 0.5, 0.5)
B: Demands = (0.6, 0.6, 0.6)
C: Demands = (0.7, 0.7, 0.7)
D: Demands = (0.8, 0.8, 0.8)
PRIOR: Demands = (0.1, 0.2, 0.3)

ALIGNED: tuple[TickSpec, ...] = (
    (4_000, 10, PRIOR),
    (5_000, 11, A),
    (6_000, 12, B),
    (7_000, 13, C),
    (7_500, 14, D),
)
"""tick の周期と格子が一致する既定の筋書き。値は格子の時刻でだけ変わる。"""


def spec_v2(**updates: object) -> DatasetSpecV2:
    """試験用の短い値。本番の候補ではなく、各項目を明示する（0087 §2.2 の値は実データの後）。"""
    values: dict[str, object] = {
        "window_ms": 5_000,
        "sample_period_ms": 1_000,
        "horizons_ms": (1_000, 2_000),
        "target_tolerance_ms": 100,
        "stale_after_ms": 1_500,
        "feature_metrics": ("air.room",),
        "target_metrics": ("air.gpu_exhaust",),
        "action_step_ms": 1_000,
        "action_steps": 2,
        "action_stale_after_ms": 1_500,
    }
    values.update(updates)
    return DatasetSpecV2.model_validate(values)


def source_run(*, end_ms: int) -> SourceRun:
    return SourceRun(
        run_id=RUN_ALIAS,
        kind=DatasetSourceKind.REPLAY,
        start_ms=T0,
        end_ms=T0 + end_ms,
        source_refs=(SOURCE_ALIAS,),
        source_sha256=SHA256,
    )


def tick(ts_ms: int, tick_id: int, demands: Demands) -> ControlTick:
    """effective を指定した tick（requested = effective。Guard / Safety が手を入れていない）。"""
    raw = json.loads(CONTROL_FIXTURE.read_text(encoding="utf-8"))
    raw.update(ts_ms=ts_ms, tick_id=tick_id)
    for zone, value in zip(("front", "rear", "top"), demands, strict=True):
        raw["zones"][zone]["demand"] = {
            "requested": value,
            "effective": value,
            "bound_by": "requested",
            "safety_floor": 0.0,
            "forced_max": False,
            "guard_floor": None,
            "guard_ceiling": None,
            "reasons": [],
        }
    return ControlTick.model_validate_json(json.dumps(raw))


def fixture_tick(ts_ms: int, tick_id: int) -> ControlTick:
    """requested と effective が違う zone を持つ保存済みの tick（rear 0.3 → 0.35 ほか）。"""
    raw = json.loads(CONTROL_FIXTURE.read_text(encoding="utf-8"))
    raw.update(ts_ms=ts_ms, tick_id=tick_id)
    return ControlTick.model_validate_json(json.dumps(raw))


def regular_readings(end_ms: int, *, targets: bool = True) -> list[Sample]:
    samples = []
    for ts_ms in range(0, end_ms, 500):
        values: dict[str, float] = {"air.room": 20.0 + ts_ms / 10_000}
        if targets:
            values["air.gpu_exhaust"] = 40.0 + ts_ms / 10_000
        samples.append(
            Sample(
                ts_ms=ts_ms,
                readings=tuple(
                    Reading(metric=metric, value=value, quality=Quality.OK)
                    for metric, value in values.items()
                ),
            )
        )
    return samples


@contextmanager
def run_store(
    path: Path,
    rules: QualityRules,
    clock: SimulatedClock,
    ticks: Sequence[TickSpec | ControlTick],
    *,
    end_ms: int,
    readings: Sequence[Sample] | None = None,
) -> Iterator[SqliteStore]:
    """1 run 専用の dataset DB を作り、tick を与えた順に記録する。"""
    with SqliteStore(path, rules=rules, clock=clock) as store:
        store.set_system_state("sys.ingest_source", "replay", at_ms=0)
        store.bind_dataset_source_run(
            run_alias=RUN_ALIAS,
            source_kind="replay",
            source_sha256=SHA256,
            at_ms=0,
            local_timezone=None,
            export_binding_sha256=None,
        )
        relative = regular_readings(end_ms) if readings is None else readings
        store.insert_samples(
            tuple(Sample(ts_ms=T0 + item.ts_ms, readings=item.readings) for item in relative)
        )
        store.complete_dataset_source_run(at_ms=end_ms)
        logger = ControlTraceLogger(store)
        for item in ticks:
            recorded = item if isinstance(item, ControlTick) else tick(*item)
            logger.record(recorded.model_copy(update={"ts_ms": T0 + recorded.ts_ms}))
        yield store


def build(
    store: SqliteStore,
    *,
    end_ms: int,
    spec: DatasetSpecV2 | None = None,
    declared_changes: tuple[DeclaredChange, ...] = NO_CHANGES,
    calibration_history: CalibrationHistory | None = None,
) -> ThermalDatasetV2:
    return ThermalDatasetV2Builder(store).build(
        source_run=source_run(end_ms=end_ms),
        spec=spec_v2() if spec is None else spec,
        declared_changes=declared_changes,
        calibration_history=(
            covering_history(store.connection.execute("PRAGMA database_list").fetchone()[2])
            if calibration_history is None
            else calibration_history
        ),
    )


def history_db(path: Path, *rows_ms: int) -> Path:
    """本番の DB の代わり。``rows_ms`` の各時刻に、写像を変えながら記録の行を足す。"""
    with SqliteStore(
        path, rules=QualityRules.from_yaml(QUALITY_RULES_PATH), clock=SimulatedClock(0)
    ) as db:
        previous: CalibrationActivation | None = None
        for index, ts_ms in enumerate(rows_ms):
            previous = CalibrationActivation.next_after(
                previous,
                ts_ms=ts_ms,
                source_kind="mock",
                offsets=effective_metric_offsets({"front_intake": 0.25 * index}),
                calibrated_at=None,
                calibration_file_sha256="f" * 64,
            )
            db.append_calibration_activation(previous)
    return path


def covering_history(dedicated_db: str) -> CalibrationHistory:
    """専用 DB の隣に、期間を覆う1行だけの記録を作って読む。"""
    path = Path(dedicated_db).with_name(Path(dedicated_db).stem + "-history.db")
    if not path.exists():
        history_db(path, COVERING_ROW_MS)
    return read_calibration_history(path)


def excluded(dataset: ThermalDatasetV2) -> dict[str, int]:
    return dataset.manifest.excluded.model_dump()


def zero_excluded(**counts: int) -> dict[str, int]:
    base = {"stale": 0, "discontinuity": 0, "in_step_change": 0, "restart": 0, "tick_id_gap": 0}
    base.update(counts)
    return base


def per_zone(values: Demands) -> PerZone[float]:
    return PerZone(front=values[0], rear=values[1], top=values[2])


@pytest.fixture
def build_ticks(tmp_path, rules, clock):
    """``build_ticks(ticks, end_ms=..., spec=...)`` で筋書きから Dataset v2 を作る。"""
    counter = iter(range(1_000))

    def factory(
        ticks: Sequence[TickSpec | ControlTick],
        *,
        end_ms: int = 7_501,
        spec: DatasetSpecV2 | None = None,
        readings: Sequence[Sample] | None = None,
    ) -> ThermalDatasetV2:
        path = tmp_path / f"run-{next(counter)}.db"
        with run_store(path, rules, clock, ticks, end_ms=end_ms, readings=readings) as store:
            return build(store, end_ms=end_ms, spec=spec)

    return factory


# ---------------------------------------------------------------- §2.1 as-of


def test_steps_take_the_as_of_effective_and_record_the_source_tick(build_ticks):
    dataset = build_ticks(ALIGNED)

    assert dataset.manifest.schema_version == 2
    (example,) = dataset.examples
    assert example.action_ts_ms == T0 + 5_000
    assert [
        (step.step, step.ts_ms, step.source_ts_ms, step.source_tick_id)
        for step in example.action_steps
    ] == [
        (0, T0 + 5_000, T0 + 5_000, 11),
        (1, T0 + 6_000, T0 + 6_000, 12),
    ]
    assert example.action_steps[0].effective_demand == per_zone(A), "step 0 は anchor の tick 自身"
    assert example.action_steps[1].effective_demand == per_zone(B)
    # 最大の horizon の時刻（7000）の tick と、その後の tick（7500）の値はどの step にも入らない
    assert len(example.action_steps) == 2
    assert excluded(dataset) == zero_excluded()


def test_offset_ticks_use_the_latest_tick_at_or_before_the_grid_time(build_ticks):
    """格子の時刻（6000）に tick が無ければ、それ以前で直近（5900）の tick を使う。"""
    dataset = build_ticks(
        (
            (4_000, 10, PRIOR),
            (5_000, 11, A),
            (5_900, 12, A),
            (6_800, 13, A),
            (7_600, 14, B),
        ),
        end_ms=7_601,
    )

    (example,) = dataset.examples
    second = example.action_steps[1]
    assert (second.ts_ms, second.source_ts_ms, second.source_tick_id) == (
        T0 + 6_000,
        T0 + 5_900,
        12,
    )
    assert second.effective_demand == per_zone(A)


def test_effective_not_requested_is_the_action_value(build_ticks):
    dataset = build_ticks(
        (
            (4_000, 10, PRIOR),
            fixture_tick(5_000, 11),
            fixture_tick(6_000, 12),
            (7_000, 13, C),
            (7_500, 14, D),
        )
    )

    (example,) = dataset.examples
    assert example.action_steps[0].effective_demand == PerZone(front=0.4, rear=0.35, top=0.6)
    assert example.action.rear.requested_demand == pytest.approx(0.3), "v1 と同じ欄は分析用に残す"


# ---------------------------------------------------------------- §2.7 prior_action


def test_prior_action_is_the_latest_tick_strictly_before_the_anchor(build_ticks):
    dataset = build_ticks(ALIGNED)

    (example,) = dataset.examples
    prior = example.prior_action
    assert (prior.source_ts_ms, prior.source_tick_id) == (T0 + 4_000, 10)
    assert prior.effective_demand == per_zone(PRIOR)
    assert prior.effective_demand != example.action_steps[0].effective_demand

    observed = ObservedThermalInput.from_example_v2(example)
    assert observed.action_ts_ms == example.action_ts_ms
    assert (
        observed.action.front.effective_demand,
        observed.action.rear.effective_demand,
        observed.action.top.effective_demand,
    ) == PRIOR, "v2 の anchor action は prior_action（step 0 ではない）"


@pytest.mark.parametrize(
    ("ticks", "stale_after"),
    [
        pytest.param(
            ((5_000, 11, A), (6_000, 12, B), (7_000, 13, C), (7_500, 14, D)), 1_500, id="no-prior"
        ),
        pytest.param(
            ((3_000, 10, PRIOR), (5_000, 11, A), (6_000, 12, B), (7_000, 13, C), (7_500, 14, D)),
            2_000,
            id="prior-exactly-at-the-limit",
        ),
    ],
)
def test_missing_or_stale_prior_action_drops_the_example(build_ticks, ticks, stale_after):
    dataset = build_ticks(ticks, spec=spec_v2(action_stale_after_ms=stale_after))

    assert dataset.examples == ()
    assert excluded(dataset) == zero_excluded(stale=1)


def test_prior_action_just_inside_the_limit_is_kept(build_ticks):
    ticks = ((3_000, 10, PRIOR), (5_000, 11, A), (6_000, 12, B), (7_000, 13, C), (7_500, 14, D))
    dataset = build_ticks(ticks, spec=spec_v2(action_stale_after_ms=2_001))

    (example,) = dataset.examples
    assert example.prior_action.source_ts_ms == T0 + 3_000


# ---------------------------------------------------------------- §2.2 鮮度


GAPPED: tuple[TickSpec, ...] = (
    (4_500, 10, A),
    (5_000, 11, A),
    (6_500, 12, A),
    (7_000, 13, A),
    (7_500, 14, A),
)
"""格子の時刻 6000 の as-of は 5000（1000 ms 前）。tick の間は 5000 → 6500 で 1500 ms 空く。"""


@pytest.mark.parametrize(
    ("stale_after", "expected"),
    [
        pytest.param(900, zero_excluded(stale=1), id="grid-older-than-limit"),
        pytest.param(1_000, zero_excluded(stale=1), id="grid-exactly-at-limit"),
        pytest.param(1_001, zero_excluded(discontinuity=1), id="fresh-grid-but-gap-between-ticks"),
        pytest.param(1_600, zero_excluded(), id="all-under-limit"),
    ],
)
def test_action_freshness_uses_the_explicit_limit(build_ticks, stale_after, expected):
    dataset = build_ticks(GAPPED, spec=spec_v2(action_stale_after_ms=stale_after))

    assert excluded(dataset) == expected
    assert len(dataset.examples) == (1 if expected == zero_excluded() else 0)


def test_spec_without_action_fields_is_not_a_type():
    """既定値は置かない（0087 §2.2 / §2.4、AGENTS.md ルール9）。"""
    full = spec_v2().model_dump()
    for field in ("action_stale_after_ms", "action_step_ms", "action_steps"):
        values = dict(full)
        del values[field]
        with pytest.raises(ValidationError, match=field):
            DatasetSpecV2.model_validate(values)
    assert all(field.is_required() for field in DatasetSpecV2.model_fields.values())


# ---------------------------------------------------------------- §2.2 tick の連続


@pytest.mark.parametrize(
    ("ticks", "end_ms", "stale_after"),
    [
        pytest.param(
            ((4_500, 10, A), (5_000, 11, A), (5_900, 12, A), (6_000, 13, A), (7_800, 14, A)),
            7_801,
            1_000,
            id="stops-in-the-last-step",
        ),
        pytest.param(
            (
                (4_500, 10, A),
                (5_000, 11, A),
                (5_600, 12, A),
                (6_000, 13, A),
                (6_900, 14, A),
                (7_000, 15, A),
                (7_500, 16, A),
            ),
            7_501,
            800,
            id="gap-inside-a-step",
        ),
    ],
)
def test_interrupted_ticks_drop_the_example(build_ticks, ticks, end_ms, stale_after):
    """格子の時刻ごとの as-of は新しいが、tick が途中で途切れている。"""
    dataset = build_ticks(ticks, end_ms=end_ms, spec=spec_v2(action_stale_after_ms=stale_after))

    assert dataset.examples == ()
    assert excluded(dataset) == zero_excluded(discontinuity=1)


def test_run_end_without_a_tick_after_the_horizon_counts_as_discontinuity(build_ticks):
    dataset = build_ticks(((4_000, 10, PRIOR), (5_000, 11, A), (6_000, 12, B), (7_000, 13, C)))

    assert dataset.examples == ()
    assert excluded(dataset) == zero_excluded(discontinuity=1)


def test_value_of_the_tick_after_the_horizon_is_never_used(build_ticks):
    """最大の horizon より後の最初の tick は tick_id の検査にだけ使い、値は数えない。"""
    dataset = build_ticks((*ALIGNED[:-1], (7_500, 14, (1.0, 0.0, 1.0))))

    assert len(dataset.examples) == 1
    assert excluded(dataset) == zero_excluded()


# ---------------------------------------------------------------- §2.3 step 内の変化


def test_change_inside_a_step_in_one_zone_drops_the_example(build_ticks):
    dataset = build_ticks(
        (
            (4_000, 10, PRIOR),
            (5_000, 11, A),
            (5_600, 12, (0.5, 0.5, 0.51)),
            (6_000, 13, A),
            (7_000, 14, A),
            (7_500, 15, A),
        )
    )

    assert dataset.examples == ()
    assert excluded(dataset) == zero_excluded(in_step_change=1)


def test_unchanged_ticks_inside_a_step_keep_the_start_value(build_ticks):
    dataset = build_ticks(
        (
            (4_000, 10, PRIOR),
            (5_000, 11, A),
            (5_600, 12, A),
            (6_000, 13, B),
            (6_600, 14, B),
            (7_000, 15, C),
            (7_500, 16, D),
        )
    )

    (example,) = dataset.examples
    assert [step.effective_demand for step in example.action_steps] == [per_zone(A), per_zone(B)]
    assert [step.source_tick_id for step in example.action_steps] == [11, 13]


# ---------------------------------------------------------------- §2.2 再起動・欠番


@pytest.mark.parametrize(
    "tick_ids",
    [
        pytest.param((10, 11, 0, 1, 2), id="back-to-zero"),
        pytest.param((10, 11, 11, 12, 13), id="same-tick-id"),
        pytest.param((10, 11, 12, 13, 0), id="restart-right-after-the-horizon"),
        pytest.param((10, 11, 13, 0, 1), id="restart-wins-over-gap"),
    ],
)
def test_restart_inside_the_checked_range_drops_the_example(build_ticks, tick_ids):
    ticks = tuple(
        (ts_ms, tick_id, values)
        for (ts_ms, _old, values), tick_id in zip(ALIGNED, tick_ids, strict=True)
    )
    dataset = build_ticks(ticks)

    assert dataset.examples == ()
    assert excluded(dataset) == zero_excluded(restart=1)


@pytest.mark.parametrize(
    "tick_ids",
    [
        pytest.param((10, 11, 13, 14, 15), id="missing-tick-inside"),
        pytest.param((10, 11, 12, 13, 15), id="missing-tick-right-after-the-horizon"),
    ],
)
def test_tick_id_gap_inside_the_checked_range_drops_the_example(build_ticks, tick_ids):
    ticks = tuple(
        (ts_ms, tick_id, values)
        for (ts_ms, _old, values), tick_id in zip(ALIGNED, tick_ids, strict=True)
    )
    dataset = build_ticks(ticks)

    assert dataset.examples == ()
    assert excluded(dataset) == zero_excluded(tick_id_gap=1)


@pytest.mark.parametrize(
    "before",
    [pytest.param((3_000, 50, PRIOR), id="restart"), pytest.param((3_000, 8, PRIOR), id="gap")],
)
def test_restart_or_gap_before_the_prior_tick_is_ignored(build_ticks, before):
    dataset = build_ticks((before, *ALIGNED))

    assert [example.action_ts_ms for example in dataset.examples] == [T0 + 5_000]
    assert excluded(dataset) == zero_excluded()


@pytest.mark.parametrize(
    "after",
    [pytest.param((8_000, 99, D), id="restart"), pytest.param((8_000, 16, D), id="gap")],
)
def test_restart_or_gap_after_the_first_tick_past_the_horizon_is_ignored(build_ticks, after):
    """anchor 5000 の検査は最大の horizon（7000）の後の最初の tick（7500）まで。8000 は外。"""
    dataset = build_ticks((*ALIGNED, after), end_ms=8_001)

    assert [example.action_ts_ms for example in dataset.examples] == [T0 + 5_000]
    # anchor 6000 も run に収まるが、最大の horizon（8000）の後に tick が無いので「連続」
    assert excluded(dataset) == zero_excluded(discontinuity=1)


def test_first_matching_reason_is_the_only_one_counted(build_ticks):
    """前に tick が無く（鮮度）、tick_id も戻る（再起動）example は鮮度にだけ数える。"""
    dataset = build_ticks(((5_000, 11, A), (6_000, 0, B), (7_000, 1, C), (7_500, 2, D)))

    assert excluded(dataset) == zero_excluded(stale=1)


# ---------------------------------------------------------------- §2.4 格子と horizon / target


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"horizons_ms": (1_500, 2_000)}, "整数倍"),
        ({"action_steps": 3}, "最大の horizon と等しく"),
        ({"action_step_ms": 500, "action_steps": 3}, "最大の horizon と等しく"),
    ],
)
def test_grid_must_match_the_horizons(updates, match):
    with pytest.raises(ValidationError, match=match):
        spec_v2(**updates)


def test_action_columns_cover_exactly_the_grid(build_ticks):
    spec = spec_v2(
        horizons_ms=(500, 2_000), target_tolerance_ms=100, action_step_ms=500, action_steps=4
    )
    ticks = tuple((4_000 + 500 * index, 10 + index, A) for index in range(9))
    dataset = build_ticks(ticks, end_ms=8_001, spec=spec)

    (example,) = (item for item in dataset.examples if item.action_ts_ms == T0 + 5_000)
    assert len(example.action_steps) == spec.action_steps
    assert [step.ts_ms - T0 for step in example.action_steps] == [5_000, 5_500, 6_000, 6_500]


def _target_readings() -> list[Sample]:
    samples = [sample for sample in regular_readings(7_501, targets=False)]
    for ts_ms, value in ((5_950, 41.0), (6_010, 42.0), (7_050, 43.0)):
        samples.append(
            Sample(
                ts_ms=ts_ms,
                readings=(Reading(metric="air.gpu_exhaust", value=value, quality=Quality.OK),),
            )
        )
    return sorted(samples, key=lambda sample: sample.ts_ms)


def test_target_uses_only_observations_at_or_before_the_expected_time(tmp_path, rules, clock):
    with run_store(
        tmp_path / "target.db", rules, clock, ALIGNED, end_ms=7_501, readings=_target_readings()
    ) as store:
        dataset = build(store, end_ms=7_501)
        v1 = ThermalDatasetBuilder(store).build(
            source_run=source_run(end_ms=7_501),
            spec=DatasetSpec(
                window_ms=5_000,
                sample_period_ms=1_000,
                horizons_ms=(1_000, 2_000),
                target_tolerance_ms=100,
                stale_after_ms=1_500,
                feature_metrics=("air.room",),
                target_metrics=("air.gpu_exhaust",),
            ),
        )

    (example,) = dataset.examples
    first, second = example.targets
    assert first.source_ts_ms["air.gpu_exhaust"] == T0 + 5_950, (
        "後ろの 6010 のほうが近くても採らない"
    )
    assert second.missing_mask["air.gpu_exhaust"] is True, "期待時刻 7000 の後ろ（7050）にしか無い"
    assert example.label_end_ms == T0 + 7_000, "anchor + 最大の horizon（許容誤差を足さない）"

    (v1_example,) = (item for item in v1.examples if item.action_ts_ms == T0 + 5_000)
    assert v1_example.targets[0].source_ts_ms["air.gpu_exhaust"] == T0 + 6_010, (
        "v1 の採り方は変えない"
    )
    assert v1_example.targets[1].source_ts_ms["air.gpu_exhaust"] == T0 + 7_050


def test_loaded_v2_target_after_the_expected_time_is_rejected(build_ticks):
    dataset = build_ticks(ALIGNED)
    raw = dataset.model_dump(mode="json")
    target = raw["examples"][0]["targets"][0]
    target["source_ts_ms"]["air.gpu_exhaust"] = target["expected_ts_ms"] + 1
    with pytest.raises(ValidationError, match="期待時刻より後"):
        ThermalDatasetV2.model_validate_json(json.dumps(raw))


# ---------------------------------------------------------------- §2.5 step の番号


def test_step_k_is_the_same_interval_as_action_plan_step_k(build_ticks):
    spec = spec_v2()
    dataset = build_ticks(ALIGNED, spec=spec)
    (example,) = dataset.examples
    plan = ActionPlan.held(
        PerZone(front=0.5, rear=0.5, top=0.5), step_ms=spec.action_step_ms, steps=spec.action_steps
    )

    assert len(plan.steps) == len(example.action_steps)
    for step, planned in zip(example.action_steps, plan.steps, strict=True):
        # PlanStep.offset_ms は区間の終端。始端は offset_ms − step_ms
        assert step.ts_ms - example.action_ts_ms == planned.offset_ms - spec.action_step_ms


# ---------------------------------------------------------------- §2.1 時刻が単調でない run


@pytest.mark.parametrize(
    "ticks",
    [
        pytest.param(
            ((4_000, 10, PRIOR), (6_000, 11, A), (5_000, 12, B), (7_000, 13, C), (7_500, 14, D)),
            id="clock-went-back",
        ),
        pytest.param(
            (
                (4_000, 10, PRIOR),
                (5_000, 11, A),
                (5_000, 12, A),
                (6_000, 13, B),
                (7_000, 14, C),
                (7_500, 15, D),
            ),
            id="same-ts",
        ),
    ],
)
def test_runs_whose_ticks_are_not_strictly_increasing_are_refused(build_ticks, ticks):
    with pytest.raises(ValueError, match="狭義単調増加でない"):
        build_ticks(ticks)


def test_runs_with_pre_migration_rows_are_refused(tmp_path, rules, clock):
    with run_store(tmp_path / "legacy.db", rules, clock, ALIGNED, end_ms=7_501) as store:
        # 移行前の行の上限を最初の行まで上げた DB（0007 の前に記録した行を持つ DB と同じ）
        store.connection.execute("UPDATE control_trace_prune SET legacy_through_seq = 1")
        with pytest.raises(ValueError, match="移行前"):
            build(store, end_ms=7_501)


def test_new_db_whose_first_tick_equals_legacy_until_ms_is_not_refused(tmp_path, rules):
    """空の DB でも legacy_until_ms はストアの時計で書かれる。時刻で見分けると正しい run を拒む。"""
    clock = SimulatedClock(T0 + 5_000)
    with run_store(tmp_path / "sim.db", rules, clock, ALIGNED, end_ms=7_501) as store:
        assert store.control_trace_prune_state().legacy_until_ms == T0 + 5_000
        assert store.control_trace_legacy_through_seq() == 0
        dataset = build(store, end_ms=7_501)

    assert [example.action_ts_ms for example in dataset.examples] == [T0 + 5_000]


# ---------------------------------------------------------------- §2.1 legacy_through_seq


def _migrations_through(directory: Path, version: int) -> Path:
    directory.mkdir()
    for migration in migrations.discover():
        if migration.version <= version:
            shutil.copy(migration.path, directory / migration.path.name)
    return directory


def _trace_row(seq: int, ts_ms: int, tick_id: int) -> tuple[int, int, int, int, str]:
    trace = tick(ts_ms, tick_id, A)
    return (seq, ts_ms, tick_id, trace.schema_version, trace.model_dump_json())


def test_migration_on_an_empty_db_sets_zero(tmp_path, rules, clock):
    with SqliteStore(tmp_path / "empty.db", rules=rules, clock=clock) as store:
        assert store.control_trace_legacy_through_seq() == 0


def test_migration_fills_the_max_seq_of_legacy_rows_and_keeps_the_read_api(tmp_path, rules):
    """0007 を適用済みの DB: ``ts_ms ≤ legacy_until_ms`` の行の ``MAX(seq)``。他は変えない。"""
    path = tmp_path / "v9.db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        # 0006 までの DB に行があり、0007 を壁時計 10_000 で適用した
        migrations.apply_pending(conn, now_ms=0, directory=_migrations_through(tmp_path / "m6", 6))
        conn.executemany(
            "INSERT INTO control_traces (ts_ms, tick_id, schema_version, trace_json) "
            "VALUES (?, ?, ?, ?)",
            [row[1:] for row in (_trace_row(0, 3_000, 2), _trace_row(0, 2_000, 1))],
        )
        migrations.apply_pending(
            conn, now_ms=10_000, directory=_migrations_through(tmp_path / "m9", 9)
        )
        # 0007 の後に記録した行（legacy_until_ms と同じ時刻の行と、それより後の行）
        conn.executemany(
            "INSERT INTO control_traces (seq, ts_ms, tick_id, schema_version, trace_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [_trace_row(3, 10_000, 3), _trace_row(4, 11_000, 4)],
        )
        before_prune = conn.execute(
            "SELECT pruned_through_seq, pruned_before_ms, legacy_until_ms FROM control_trace_prune"
        ).fetchone()
    finally:
        conn.close()

    with SqliteStore(path, rules=rules, clock=SimulatedClock(20_000)) as store:
        page = store.control_trace_page(0, 100_000, after_seq=None, limit=10)
        state = store.control_trace_prune_state()
        assert (
            state.pruned_through_seq,
            state.pruned_before_ms,
            state.legacy_until_ms,
        ) == before_prune
        assert state.legacy_until_ms == 10_000
        assert [(row.seq, row.ts_ms, row.tick_id) for row in page.traces] == [
            (1, 2_000, 1),
            (2, 3_000, 2),
            (3, 10_000, 3),
            (4, 11_000, 4),
        ]
        assert page.retained_from_ms == 10_000
        # 0007 の後に legacy_until_ms と同じ時刻で記録した行（seq 3）も含む安全側の上限
        assert store.control_trace_legacy_through_seq() == 3

    # 冪等: 開き直しても再適用せず、値も変わらない
    with SqliteStore(path, rules=rules, clock=SimulatedClock(30_000)) as again:
        assert again.control_trace_legacy_through_seq() == 3
        assert again.control_trace_page(0, 100_000, after_seq=None, limit=10) == page
        versions = [
            row[0] for row in again.connection.execute("SELECT version FROM schema_version")
        ]
        assert versions == list(range(1, len(migrations.discover()) + 1))


def test_migration_keeps_the_read_api_result_unchanged(tmp_path, rules):
    """0071 の読み取り API の結果は migration の前後で同じ。"""
    path = tmp_path / "api.db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        migrations.apply_pending(
            conn, now_ms=1_000, directory=_migrations_through(tmp_path / "m9", 9)
        )
        conn.executemany(
            "INSERT INTO control_traces (seq, ts_ms, tick_id, schema_version, trace_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [_trace_row(1, 2_000, 1), _trace_row(2, 3_000, 2)],
        )
    finally:
        conn.close()
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    try:
        before_rows = [
            tuple(row) for row in raw.execute("SELECT * FROM control_traces ORDER BY seq")
        ]
        before_prune = tuple(raw.execute("SELECT * FROM control_trace_prune").fetchone())
    finally:
        raw.close()

    with SqliteStore(path, rules=rules, clock=SimulatedClock(5_000)) as store:
        after_rows = [
            tuple(row)
            for row in store.connection.execute("SELECT * FROM control_traces ORDER BY seq")
        ]
        after_prune = tuple(
            store.connection.execute(
                "SELECT singleton, pruned_through_seq, pruned_before_ms, legacy_until_ms "
                "FROM control_trace_prune"
            ).fetchone()
        )
        assert store.control_trace_legacy_through_seq() == 0, "移行前の行は無い（0007 の時点で空）"
    assert after_rows == before_rows
    assert after_prune == before_prune


# ---------------------------------------------------------------- §2.6 較正


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        pytest.param(
            DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=T0),
            True,
            id="at-history-start",
        ),
        pytest.param(
            DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=T0 + 6_000), True, id="inside"
        ),
        pytest.param(
            DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=T0 + 7_000),
            True,
            id="at-label-end",
        ),
        # 宣言も記録の行と同じ区間 [floor(ts), ts] で見る（0109 §2.4 / §5 #1）。
        # 下端 T0 + 7000 が期間の終端
        pytest.param(
            DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=T0 + 7_001),
            True,
            id="same-second-after-label-end",
        ),
        pytest.param(
            DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=T0 + 7_999),
            True,
            id="only-the-floor-inside",
        ),
        pytest.param(
            DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=T0 + 8_000), False, id="after"
        ),
        # 区間の上端が期間の先頭の直前（下端 T0 - 1000 も前）
        pytest.param(
            DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=T0 - 1),
            False,
            id="just-before",
        ),
        pytest.param(
            DeclaredChange(kind=ChangeKind.FAN_REPLACED, ts_ms=T0 + 6_000),
            False,
            id="not-calibration",
        ),
    ],
)
def test_calibration_change_inside_the_dataset_period_is_refused(
    tmp_path, rules, clock, change, refused
):
    with run_store(tmp_path / "cal.db", rules, clock, ALIGNED, end_ms=7_501) as store:
        if refused:
            with pytest.raises(ValueError, match="較正の変更"):
                build(store, end_ms=7_501, declared_changes=(change,))
        else:
            assert len(build(store, end_ms=7_501, declared_changes=(change,)).examples) == 1


def test_declared_changes_must_be_passed_explicitly(tmp_path, rules, clock):
    history = read_calibration_history(history_db(tmp_path / "history.db", COVERING_ROW_MS))
    with run_store(tmp_path / "decl.db", rules, clock, ALIGNED, end_ms=7_501) as store:
        builder = ThermalDatasetV2Builder(store)
        with pytest.raises(TypeError):
            builder.build(  # type: ignore[call-arg]
                source_run=source_run(end_ms=7_501), spec=spec_v2(), calibration_history=history
            )
        with pytest.raises(TypeError, match="DeclaredChange"):
            builder.build(
                source_run=source_run(end_ms=7_501),
                spec=spec_v2(),
                declared_changes=[],  # type: ignore[arg-type]
                calibration_history=history,
            )


# ---------------------------------------------------------------- 0099 §2.6 較正の変更の記録

ALIGNED_PERIOD = (T0, T0 + 7_000)
"""ALIGNED の唯一の example の期間 ``[history_start, label_end]``。"""


def build_with_history(tmp_path, rules, clock, *rows_ms, ticks=ALIGNED, spec=None):
    history = read_calibration_history(history_db(tmp_path / "history.db", *rows_ms))
    with run_store(tmp_path / "run.db", rules, clock, ticks, end_ms=7_501) as store:
        return build(store, end_ms=7_501, spec=spec, calibration_history=history)


def test_alignment_of_the_default_period():
    """以降の試験の前提（ALIGNED の example の期間）を固定する。"""
    assert ALIGNED_PERIOD == (T0 + 5_000 - 5_000, T0 + 5_000 + 2_000)


@pytest.mark.parametrize(
    "rows_ms",
    [
        pytest.param((COVERING_ROW_MS, T0 + 3_000), id="row-inside"),
        pytest.param((COVERING_ROW_MS, T0), id="row-at-the-period-start"),
        pytest.param((COVERING_ROW_MS, T0 + 7_000), id="row-at-the-period-end"),
        # 区間 [floor(ts), ts] の下端（T0 + 7000）だけが期間に入る（秒の切り捨て。0099 §5 #9）
        pytest.param((COVERING_ROW_MS, T0 + 7_999), id="only-the-floor-inside"),
    ],
)
def test_recorded_calibration_change_inside_the_period_is_refused(tmp_path, rules, clock, rows_ms):
    """``declared_changes`` が空でも記録の行は効く（宣言との和集合。0099 §2.6 / §5 #6）。"""
    with pytest.raises(ValueError, match="較正の変更"):
        build_with_history(tmp_path, rules, clock, *rows_ms)


@pytest.mark.parametrize(
    "rows_ms",
    [
        pytest.param((COVERING_ROW_MS,), id="one-covering-row"),
        pytest.param((COVERING_ROW_MS, T0 + 8_000), id="row-after-the-period"),
        # 区間の上端だけが期間の前（T0 - 1）。下端（T0 - 1000）も期間の前
        pytest.param((COVERING_ROW_MS, T0 - 1), id="row-just-before-the-period"),
    ],
)
def test_rows_only_outside_the_period_build(tmp_path, rules, clock, rows_ms):
    dataset = build_with_history(tmp_path, rules, clock, *rows_ms)
    assert [example.action_ts_ms for example in dataset.examples] == [T0 + 5_000]


@pytest.mark.parametrize(
    "rows_ms",
    [
        pytest.param((), id="empty-table"),
        pytest.param((T0 + 1,), id="first-row-after-the-period-start"),
        pytest.param((T0 + 9_000,), id="only-a-row-after-the-period"),
    ],
)
def test_history_that_does_not_cover_the_period_is_refused(tmp_path, rules, clock, rows_ms):
    with pytest.raises(ValueError, match="期間を覆っていない"):
        build_with_history(tmp_path, rules, clock, *rows_ms)


def test_declared_calibration_change_is_still_refused_with_a_covering_history(
    tmp_path, rules, clock
):
    history = read_calibration_history(history_db(tmp_path / "history.db", COVERING_ROW_MS))
    change = DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=T0 + 6_000)
    with (
        run_store(tmp_path / "run.db", rules, clock, ALIGNED, end_ms=7_501) as store,
        pytest.raises(ValueError, match="較正の変更"),
    ):
        build(store, end_ms=7_501, declared_changes=(change,), calibration_history=history)


def test_zero_example_dataset_skips_coverage_and_change_checks(tmp_path, rules, clock):
    """action の規則ですべて除外された dataset（0094 §2.1）は、被覆が無くても0件で返る。"""
    no_prior = ((5_000, 11, A), (6_000, 12, B), (7_000, 13, C), (7_500, 14, D))
    dataset = build_with_history(tmp_path, rules, clock, ticks=no_prior)
    assert dataset.examples == ()
    assert excluded(dataset) == zero_excluded(stale=1)


def test_zero_example_dataset_still_needs_a_readable_history(tmp_path, rules, clock):
    """表が無い DB（migration 前）は、0件の dataset でも読み込みで拒否する（0099 §2.7）。"""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy, isolation_level=None)
    try:
        migrations.apply_pending(conn, now_ms=0, directory=_migrations_through(tmp_path / "m", 10))
    finally:
        conn.close()
    with pytest.raises(CalibrationHistoryError, match="calibration_activations が無い"):
        read_calibration_history(legacy)


def test_calibration_history_must_be_passed_explicitly(tmp_path, rules, clock):
    with run_store(tmp_path / "run.db", rules, clock, ALIGNED, end_ms=7_501) as store:
        builder = ThermalDatasetV2Builder(store)
        with pytest.raises(TypeError, match="calibration_history"):
            builder.build(  # type: ignore[call-arg]
                source_run=source_run(end_ms=7_501), spec=spec_v2(), declared_changes=NO_CHANGES
            )
        with pytest.raises(TypeError, match="calibration_history"):
            builder.build(
                source_run=source_run(end_ms=7_501),
                spec=spec_v2(),
                declared_changes=NO_CHANGES,
                calibration_history=(),  # type: ignore[arg-type]
            )


def test_control_model_does_not_import_control_drift():
    """``control/drift`` は ``control/model`` を import する。逆向きは循環する（0087 §2.6）。"""
    found: dict[str, set[str]] = {}
    for path in sorted(MODEL_PACKAGE.glob("*.py")):
        modules: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.add(node.module)
        offending = {name for name in modules if name.startswith("coldaisle.control.drift")}
        if offending:
            found[path.name] = offending
    assert found == {}


# ---------------------------------------------------------------- 除外の件数・決定性・版


def test_exclusion_counts_add_up_over_many_anchors(build_ticks):
    """anchor ごとに理由が違う run。理由ごとの件数と、作った example の和が anchor の数になる。"""
    spec = spec_v2(window_ms=1_000, action_stale_after_ms=1_100)
    ticks: tuple[TickSpec, ...] = (
        (1_000, 1, A),  # 前に tick が無い → 鮮度
        (2_000, 2, A),  # 作る
        (3_000, 3, A),  # 作る
        (4_000, 4, A),  # 区間 [5000, 6000) の 5500 が違う → 区間内の変化
        (5_000, 5, A),  # 区間 [5000, 6000) の 5500 が違う → 区間内の変化
        (5_500, 6, B),  # 作る
        (6_000, 7, B),  # 9 → 11 → 欠番
        (7_000, 8, B),  # 欠番
        (8_000, 9, B),  # 欠番
        (9_000, 11, B),  # 欠番と再起動（13 → 0）の両方 → 再起動
        (10_000, 12, B),  # 再起動
        (11_000, 13, B),  # 最大の horizon（13000）の後に tick が無い → 連続
        (12_000, 0, B),
        (13_000, 1, B),
    )
    dataset = build_ticks(ticks, end_ms=13_500, spec=spec)

    counts = excluded(dataset)
    eligible = [ts for ts, _id, _values in ticks if ts >= 1_000 and ts + 2_000 < 13_500]
    assert sum(counts.values()) + len(dataset.examples) == len(eligible)
    assert [example.action_ts_ms - T0 for example in dataset.examples] == [2_000, 3_000, 5_500]
    assert counts == zero_excluded(
        stale=1, discontinuity=1, in_step_change=2, restart=2, tick_id_gap=3
    )


def test_same_db_and_spec_give_the_same_bytes(tmp_path, rules, clock):
    with run_store(tmp_path / "det.db", rules, clock, ALIGNED, end_ms=7_501) as store:
        first = build(store, end_ms=7_501)
        second = build(store, end_ms=7_501)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.manifest.examples_sha256 == second.manifest.examples_sha256
    assert first.manifest.excluded == second.manifest.excluded

    one = write_dataset(first, tmp_path / "out", ARTIFACT_ONE)
    two = write_dataset(second, tmp_path / "out", ARTIFACT_TWO)
    assert [path.read_bytes() for path in one] == [path.read_bytes() for path in two]
    loaded = ThermalDatasetV2.model_validate_json(
        json.dumps(
            {
                "manifest": json.loads(one[0].read_text(encoding="utf-8")),
                "examples": [
                    json.loads(line) for line in one[1].read_text(encoding="utf-8").splitlines()
                ],
            }
        )
    )
    assert loaded == first


def test_manifest_without_exclusion_counts_is_not_a_type(build_ticks):
    raw = build_ticks(ALIGNED).manifest.model_dump(mode="json")
    del raw["excluded"]
    with pytest.raises(ValidationError, match="excluded"):
        DatasetManifestV2.model_validate_json(json.dumps(raw))
    partial = build_ticks(ALIGNED).manifest.model_dump(mode="json")
    del partial["excluded"]["tick_id_gap"]
    with pytest.raises(ValidationError, match="tick_id_gap"):
        DatasetManifestV2.model_validate_json(json.dumps(partial))


def test_v1_is_not_read_as_v2_and_v2_is_not_read_as_v1(tmp_path, rules, clock, build_ticks):
    v2 = build_ticks(ALIGNED)
    with run_store(tmp_path / "v1.db", rules, clock, ALIGNED, end_ms=7_501) as store:
        v1 = ThermalDatasetBuilder(store).build(
            source_run=source_run(end_ms=7_501),
            spec=DatasetSpec(
                window_ms=5_000,
                sample_period_ms=1_000,
                horizons_ms=(1_000, 2_000),
                target_tolerance_ms=100,
                stale_after_ms=1_500,
                feature_metrics=("air.room",),
                target_metrics=("air.gpu_exhaust",),
            ),
        )
    assert v1.manifest.schema_version == 1
    with pytest.raises(ValidationError):
        ThermalDatasetV2.model_validate_json(v1.model_dump_json())
    with pytest.raises(ValidationError):
        DatasetExampleV2.model_validate_json(v1.examples[0].model_dump_json())
    with pytest.raises(ValidationError):
        ThermalDataset.model_validate_json(v2.model_dump_json())


def test_loaded_v2_steps_must_stay_on_the_grid_and_fresh(build_ticks):
    dataset = build_ticks(ALIGNED)
    raw = dataset.model_dump(mode="json")
    raw["examples"][0]["action_steps"][1]["ts_ms"] = T0 + 6_500
    with pytest.raises(ValidationError, match="格子"):
        ThermalDatasetV2.model_validate_json(json.dumps(raw))

    raw = dataset.model_dump(mode="json")
    raw["examples"][0]["action_steps"] = raw["examples"][0]["action_steps"][:1]
    with pytest.raises(ValidationError, match="action_steps と一致しない"):
        ThermalDatasetV2.model_validate_json(json.dumps(raw))

    raw = dataset.model_dump(mode="json")
    raw["examples"][0]["prior_action"]["source_ts_ms"] = T0 + 5_000
    with pytest.raises(ValidationError, match="厳密に前"):
        ThermalDatasetV2.model_validate_json(json.dumps(raw))


def test_v2_split_uses_the_v2_label_end(build_ticks):
    (example,) = build_ticks(ALIGNED).examples
    assert example.label_end_ms == T0 + 7_000
    # v1 なら label_end は 7100 で validation 境界 7050 を跨ぐが、v2 は 7000 で train に入る
    split = split_temporally_v2(
        (example,), validation_start_ms=T0 + 7_050, test_start_ms=T0 + 9_000
    )
    assert split.train == (example,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("source_tick_id", 99, id="tick-id"),
        pytest.param("effective_demand", {"front": 0.9, "rear": 0.5, "top": 0.5}, id="value"),
    ],
)
def test_steps_sharing_a_source_tick_must_agree(build_ticks, field, value):
    """複数の step が同じ tick を使うとき、tick_id と値は一致する（PR #221 の Codex P2）。"""
    dataset = build_ticks(
        ((4_500, 10, PRIOR), (5_000, 11, A), (6_800, 12, A), (7_500, 13, B)),
        spec=spec_v2(action_stale_after_ms=1_900),
    )
    (example,) = dataset.examples
    first, second = example.action_steps
    assert second.source_ts_ms == first.source_ts_ms == T0 + 5_000
    assert second.source_tick_id == first.source_tick_id

    raw = dataset.model_dump(mode="json")
    raw["examples"][0]["action_steps"][1][field] = value
    with pytest.raises(ValidationError, match=r"同じ元の tick|時刻の順と一致しない"):
        DatasetExampleV2.model_validate_json(json.dumps(raw["examples"][0]))


@pytest.mark.parametrize(
    ("where", "field", "value"),
    [
        pytest.param("action_steps", "source_tick_id", 99, id="step-tick-id"),
        pytest.param(
            "action_steps",
            "effective_demand",
            {"front": 0.9, "rear": 0.6, "top": 0.6},
            id="step-value",
        ),
        pytest.param(
            "prior_action",
            "effective_demand",
            {"front": 0.9, "rear": 0.5, "top": 0.5},
            id="prior-value",
        ),
    ],
)
def test_source_ticks_must_agree_across_examples(build_ticks, where, field, value):
    """重なる example が同じ tick を使うとき、example の間でも tick_id と値が一致する。"""
    ticks = tuple((4_000 + 1_000 * index, 10 + index, A if index % 2 else B) for index in range(6))
    dataset = build_ticks(ticks, end_ms=9_001, spec=spec_v2(window_ms=1_000))
    first, second = dataset.examples[:2]
    assert second.prior_action.source_ts_ms == first.action_ts_ms
    assert second.action_steps[0].source_ts_ms == first.action_steps[1].source_ts_ms

    raw = dataset.model_dump(mode="json")
    # 1つ目の step 1（6000）は2つ目の anchor、2つ目の prior_action（5000）は1つ目の anchor
    target = (
        raw["examples"][0]["action_steps"][1]
        if where == "action_steps"
        else raw["examples"][1][where]
    )
    target[field] = value
    examples = tuple(
        DatasetExampleV2.model_validate_json(json.dumps(item)) for item in raw["examples"]
    )
    raw["manifest"]["examples_sha256"] = examples_sha256(examples)
    with pytest.raises(ValidationError, match="example の間で食い違っている"):
        ThermalDatasetV2.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        pytest.param(("prior_action", "source_tick_id"), 3, "anchor の tick_id − 1", id="prior"),
        pytest.param(("action_steps", 1, "source_tick_id"), 11, "時刻の順と一致しない", id="step"),
    ],
)
def test_loaded_source_tick_ids_follow_the_consecutive_rule(build_ticks, path, value, match):
    """作られた example では tick_id がちょうど 1 ずつ増える（PR #221 の Codex P2 の3件目）。"""
    dataset = build_ticks(ALIGNED)
    raw = dataset.model_dump(mode="json")["examples"][0]
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError, match=match):
        DatasetExampleV2.model_validate_json(json.dumps(raw))
