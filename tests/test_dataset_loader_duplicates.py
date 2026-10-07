"""Thermal Dataset の読み込みで、同じ anchor の複製と同じ観測の食い違いを拒否する。

#224 / 決定記録 0103。

v1 / v2 とも builder の出力から始め、改ざんした後に ``example_count`` と ``examples_sha256`` を
計算し直す（hash を合わせ直した artifact でも読み込みで拒むことを確かめるため）。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control import ControlTick, ControlTraceLogger
from coldaisle.control.model.counterfactual_training import train_counterfactual_ridge
from coldaisle.control.model.dataset import (
    DatasetExample,
    DatasetExampleV2,
    DatasetSourceKind,
    DatasetSpec,
    DatasetSpecV2,
    SourceRun,
    ThermalDataset,
    ThermalDatasetV2,
    examples_jsonl_bytes,
    examples_sha256,
)
from coldaisle.dataset import ThermalDatasetBuilder, ThermalDatasetV2Builder, write_dataset
from coldaisle.drift import DATASET_EXAMPLES_FILENAME, DATASET_MANIFEST_FILENAME, load_inputs
from coldaisle.store import Quality, QualityRules, Reading, Sample, SqliteStore
from test_thermal_dataset_v2 import T0, covering_history
from test_thermal_model_v2 import (
    CATALOG,
    published,
    split,
    thermal_dataset,
    training_spec,
)

CONTROL_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "control_tick_v1.json"
SHA256 = "a" * 64
RUN_ALIAS = "run-00000000000000000000000000000224"
OTHER_RUN_ALIAS = "run-00000000000000000000000000000225"
SOURCE_ALIAS = "source-00000000000000000000000000000224"
ARTIFACT = "dataset-00000000000000000000000000000224"
START_MS = T0
"""較正の記録の行が期間の先頭を覆うように、``T0`` から始める（0099 §2.6）。"""
END_MS = START_MS + 16_000
DUPLICATE_ANCHOR = "同じ anchor"
CONFLICT = "同じ観測"

Raw = dict[str, Any]


def source_run(run_id: str = RUN_ALIAS) -> SourceRun:
    return SourceRun(
        run_id=run_id,
        kind=DatasetSourceKind.REPLAY,
        start_ms=START_MS,
        end_ms=END_MS,
        source_refs=(SOURCE_ALIAS,),
        source_sha256=SHA256,
    )


def v1_spec() -> DatasetSpec:
    """window が重なり、同じ観測が複数の frame・horizon・example に採られる短い値。

    本番の候補ではない。
    """
    return DatasetSpec(
        window_ms=3_000,
        sample_period_ms=1_000,
        horizons_ms=(1_000, 2_000),
        target_tolerance_ms=600,
        stale_after_ms=1_000,
        feature_metrics=("air.room", "air.gpu_exhaust"),
        target_metrics=("air.gpu_exhaust",),
    )


def v2_spec() -> DatasetSpecV2:
    return DatasetSpecV2(
        window_ms=3_000,
        sample_period_ms=1_000,
        horizons_ms=(1_000, 2_000),
        target_tolerance_ms=600,
        stale_after_ms=1_000,
        feature_metrics=("air.room", "air.gpu_exhaust"),
        target_metrics=("air.gpu_exhaust",),
        action_step_ms=1_000,
        action_steps=2,
        action_stale_after_ms=1_500,
    )


def readings() -> tuple[Sample, ...]:
    """1500 ms ごとの観測。1000 ms の frame は同じ観測を as-of で持ち越す（stale も混ざる）。"""
    return tuple(
        Sample(
            ts_ms=ts_ms,
            readings=(
                Reading(metric="air.room", value=20.0 + ts_ms / 1_000, quality=Quality.OK),
                Reading(metric="air.gpu_exhaust", value=40.0 + ts_ms / 500, quality=Quality.OK),
            ),
        )
        for ts_ms in range(START_MS, END_MS, 1_500)
    )


def control_tick(ts_ms: int, tick_id: int) -> ControlTick:
    raw = json.loads(CONTROL_FIXTURE.read_text(encoding="utf-8"))
    raw.update(ts_ms=ts_ms, tick_id=tick_id)
    return ControlTick.model_validate_json(json.dumps(raw))


@pytest.fixture
def make_store(
    tmp_path: Path, rules: QualityRules, clock: SimulatedClock
) -> Iterator[Callable[[tuple[tuple[int, int], ...]], SqliteStore]]:
    """``make_store(((ts_ms, tick_id), ...))`` で1 run 専用の dataset DB を作る。"""
    stores: list[SqliteStore] = []

    def factory(ticks: tuple[tuple[int, int], ...]) -> SqliteStore:
        store = SqliteStore(tmp_path / f"run-{len(stores)}.db", rules=rules, clock=clock)
        stores.append(store)
        store.set_system_state("sys.ingest_source", "replay", at_ms=0)
        store.bind_dataset_source_run(
            run_alias=RUN_ALIAS, source_kind="replay", source_sha256=SHA256, at_ms=0
        )
        store.insert_samples(readings())
        store.complete_dataset_source_run(at_ms=END_MS)
        logger = ControlTraceLogger(store)
        for ts_ms, tick_id in ticks:
            logger.record(control_tick(ts_ms, tick_id))
        return store

    yield factory
    for store in stores:
        store.close()


SEQUENTIAL_TICKS = tuple(
    (ts_ms, ts_ms // 1_000) for ts_ms in range(START_MS + 1_000, END_MS, 1_000)
)


@pytest.fixture
def v1_dataset(make_store) -> ThermalDataset:
    return ThermalDatasetBuilder(make_store(SEQUENTIAL_TICKS)).build(
        source_run=source_run(), spec=v1_spec()
    )


@pytest.fixture
def v2_dataset(make_store) -> ThermalDatasetV2:
    store = make_store(SEQUENTIAL_TICKS)
    return ThermalDatasetV2Builder(store).build(
        source_run=source_run(),
        spec=v2_spec(),
        declared_changes=(),
        calibration_history=covering_history(
            store.connection.execute("PRAGMA database_list").fetchone()[2]
        ),
    )


@pytest.fixture(params=["v1", "v2"])
def version(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def dataset(version: str, v1_dataset, v2_dataset) -> ThermalDataset | ThermalDatasetV2:
    return v1_dataset if version == "v1" else v2_dataset


def resealed(dataset: ThermalDataset | ThermalDatasetV2, raw: Raw) -> Raw:
    """改ざんした examples に合わせて ``example_count`` と ``examples_sha256`` を計算し直す。"""
    example_type: type[DatasetExample] | type[DatasetExampleV2] = (
        DatasetExampleV2 if isinstance(dataset, ThermalDatasetV2) else DatasetExample
    )
    examples = tuple(example_type.model_validate_json(json.dumps(item)) for item in raw["examples"])
    raw["manifest"]["example_count"] = len(examples)
    raw["manifest"]["examples_sha256"] = examples_sha256(examples)  # type: ignore[arg-type]
    return raw


def load(dataset: ThermalDataset | ThermalDatasetV2, raw: Raw) -> None:
    """改ざんした raw を、元の dataset と同じ版の loader で読む。"""
    payload = json.dumps(resealed(dataset, raw))
    if isinstance(dataset, ThermalDatasetV2):
        ThermalDatasetV2.model_validate_json(payload)
    else:
        ThermalDataset.model_validate_json(payload)


Cell = tuple[int, str, int, str]
"""``(example の添字, "window" | "targets", frame の添字, metric)``。"""


def shared_cells(raw: Raw) -> dict[tuple[str, str, int], list[Cell]]:
    """``(run, metric, source_ts_ms)`` ごとに、その観測を使う cell を集める。"""
    cells: dict[tuple[str, str, int], list[Cell]] = defaultdict(list)
    for index, example in enumerate(raw["examples"]):
        for kind in ("window", "targets"):
            for position, frame in enumerate(example[kind]):
                for metric, source_ts in frame["source_ts_ms"].items():
                    if source_ts is not None:
                        cells[(example["source_run_id"], metric, source_ts)].append(
                            (index, kind, position, metric)
                        )
    return cells


def pair(raw: Raw, accept: Callable[[Cell, Cell], bool]) -> tuple[Cell, Cell]:
    """同じ観測を使う2つの cell のうち、``accept`` を満たす最初の組。

    builder の出力に無ければ試験の筋書きの誤りとして落とす。
    """
    for users in shared_cells(raw).values():
        for first in users:
            for second in users:
                if first != second and accept(first, second):
                    return first, second
    raise AssertionError("builder の出力に、条件を満たす同じ観測の組が無い")


def frame_of(raw: Raw, cell: Cell) -> Raw:
    index, kind, position, _metric = cell
    frame: Raw = raw["examples"][index][kind][position]
    return frame


def same_example_windows(first: Cell, second: Cell) -> bool:
    return first[0] == second[0] and first[1] == second[1] == "window"


def windows_of_overlapping_examples(first: Cell, second: Cell) -> bool:
    return first[0] != second[0] and first[1] == second[1] == "window"


def target_and_later_window(first: Cell, second: Cell) -> bool:
    return first[0] < second[0] and (first[1], second[1]) == ("targets", "window")


def targets_of_one_example(first: Cell, second: Cell) -> bool:
    return first[0] == second[0] and first[1] == second[1] == "targets"


def targets_of_two_examples(first: Cell, second: Cell) -> bool:
    return first[0] != second[0] and first[1] == second[1] == "targets"


# ---------------------------------------------------------------- 正当な dataset は通る


def test_builder_output_has_shared_observations_and_loads(dataset) -> None:
    """builder の出力は、同じ観測を frame・example・horizon の間で共有したまま読み込める。

    0103 §2.4。
    """
    raw = dataset.model_dump(mode="json")
    for accept in (
        same_example_windows,
        windows_of_overlapping_examples,
        target_and_later_window,
        targets_of_two_examples,
    ):
        pair(raw, accept)
    load(dataset, raw)


def test_v1_targets_of_one_example_can_share_an_observation(v1_dataset) -> None:
    """v1 の最近傍は、2つの horizon に同じ観測を採りうる（0031 §2.2）。それも通る。"""
    raw = v1_dataset.model_dump(mode="json")
    pair(raw, targets_of_one_example)
    load(v1_dataset, raw)


def test_stale_mask_may_differ_between_frames_using_one_observation(dataset) -> None:
    """``stale_mask`` は frame の時刻で正当に変わるので照合しない（0103 §2.2）。"""
    raw = dataset.model_dump(mode="json")
    first, second = pair(
        raw,
        lambda a, b: (
            same_example_windows(a, b)
            and frame_of(raw, a)["stale_mask"][a[3]] != frame_of(raw, b)["stale_mask"][b[3]]
        ),
    )
    load(dataset, raw)
    # 同じ組の stale_mask を揃えると spec の鮮度と食い違う（既存の検査が拒む）
    frame_of(raw, second)["stale_mask"][second[3]] = frame_of(raw, first)["stale_mask"][first[3]]
    with pytest.raises(ValidationError, match="stale_mask"):
        load(dataset, raw)


def test_v1_ticks_at_one_time_or_with_a_reused_tick_id_are_distinct_anchors(make_store) -> None:
    """v1 は同じ時刻の別の tick も、再起動で振り直された同じ tick_id も正当に作る（0103 §2.1）。"""
    ticks = ((T0 + 5_000, 1), (T0 + 5_000, 2), (T0 + 6_000, 1), (T0 + 7_000, 0))
    built = ThermalDatasetBuilder(make_store(ticks)).build(source_run=source_run(), spec=v1_spec())
    assert [(item.action_ts_ms, item.control_tick_id) for item in built.examples] == [
        (T0 + 5_000, 1),
        (T0 + 5_000, 2),
        (T0 + 6_000, 1),
        (T0 + 7_000, 0),
    ]
    load(built, built.model_dump(mode="json"))


def test_same_observation_in_another_run_is_not_compared(dataset) -> None:
    """run が違えば別の DB の別の観測（0103 §2.2）。同じ時刻に違う値があっても通る。"""
    raw = dataset.model_dump(mode="json")
    copies = json.loads(json.dumps(raw["examples"]))
    for item in copies:
        item["source_run_id"] = OTHER_RUN_ALIAS
        item["example_id"] = f"{OTHER_RUN_ALIAS}:{item['action_ts_ms']}:{item['control_tick_id']}"
        for frame in (*item["window"], *item["targets"]):
            for metric, value in frame["values"].items():
                if value is not None:
                    frame["values"][metric] = value + 1.0
    raw["examples"].extend(copies)
    raw["manifest"]["source_runs"].append(source_run(OTHER_RUN_ALIAS).model_dump(mode="json"))
    load(dataset, raw)


# ---------------------------------------------------------------- 同じ anchor の複製


def test_copied_example_with_another_id_is_rejected(dataset) -> None:
    raw = dataset.model_dump(mode="json")
    copy = json.loads(json.dumps(raw["examples"][1]))
    copy["example_id"] = "copied"
    raw["examples"].append(copy)
    with pytest.raises(ValidationError, match=DUPLICATE_ANCHOR):
        load(dataset, raw)


def test_copied_example_with_other_content_is_rejected(dataset) -> None:
    """中身を変えても、anchor のキーが同じなら複製として拒む。"""
    raw = dataset.model_dump(mode="json")
    copy = json.loads(json.dumps(raw["examples"][1]))
    copy["example_id"] = "copied"
    copy["context"]["fault_codes"] = ["copied"]
    raw["examples"].insert(0, copy)
    with pytest.raises(ValidationError, match=DUPLICATE_ANCHOR):
        load(dataset, raw)


# ---------------------------------------------------------------- 同じ観測の食い違い


def change_value(frame: Raw, metric: str) -> None:
    frame["values"][metric] += 0.5


def change_quality(frame: Raw, metric: str) -> None:
    # 値のある suspect（範囲外など）は missing_mask=false のまま正当な cell
    frame["quality"][metric] = Quality.SUSPECT.value


def drop_value(frame: Raw, metric: str) -> None:
    # 値の無い suspect（非有限値）は missing_mask=true の正当な cell
    frame["values"][metric] = None
    frame["quality"][metric] = Quality.SUSPECT.value
    frame["missing_mask"][metric] = True


@pytest.mark.parametrize("change", [change_value, change_quality, drop_value])
@pytest.mark.parametrize(
    "where",
    [
        same_example_windows,
        windows_of_overlapping_examples,
        target_and_later_window,
        targets_of_two_examples,
    ],
)
def test_conflicting_uses_of_one_observation_are_rejected(dataset, where, change) -> None:
    raw = dataset.model_dump(mode="json")
    _first, second = pair(raw, where)
    change(frame_of(raw, second), second[3])
    with pytest.raises(ValidationError, match=CONFLICT):
        load(dataset, raw)


def test_v1_conflicting_targets_of_one_example_are_rejected(v1_dataset) -> None:
    raw = v1_dataset.model_dump(mode="json")
    _first, second = pair(raw, targets_of_one_example)
    change_value(frame_of(raw, second), second[3])
    with pytest.raises(ValidationError, match=CONFLICT):
        load(v1_dataset, raw)


def test_negative_zero_is_the_same_value(dataset) -> None:
    """``-0.0`` と ``0.0`` は同じ値とみなす（0103 §5 #5）。"""
    raw = dataset.model_dump(mode="json")
    first, second = pair(raw, windows_of_overlapping_examples)
    key = (
        raw["examples"][first[0]]["source_run_id"],
        first[3],
        frame_of(raw, first)["source_ts_ms"][first[3]],
    )
    for user in shared_cells(raw)[key]:
        frame_of(raw, user)["values"][user[3]] = 0.0
    frame_of(raw, second)["values"][second[3]] = -0.0
    load(dataset, raw)


# ---------------------------------------------------------------- 入口


def test_write_dataset_rejects_a_resealed_copy(dataset, tmp_path: Path) -> None:
    raw = dataset.model_dump(mode="json")
    copy = json.loads(json.dumps(raw["examples"][1]))
    copy["example_id"] = "copied"
    raw["examples"].append(copy)
    resealed(dataset, raw)
    # validation を通らない instance を作り、書き出しの境界の再検証で拒むことを確かめる
    forged = type(dataset).model_construct(
        manifest=type(dataset.manifest).model_validate_json(json.dumps(raw["manifest"])),
        examples=tuple(
            type(dataset.examples[0]).model_validate_json(json.dumps(item))
            for item in raw["examples"]
        ),
    )
    with pytest.raises(ValidationError, match=DUPLICATE_ANCHOR):
        write_dataset(forged, tmp_path / "out", ARTIFACT)


def test_drift_inputs_reject_a_v1_artifact_with_conflicting_observations(
    v1_dataset, tmp_path: Path
) -> None:
    """``coldaisle-drift`` の ``load_inputs`` は v1 だけを読む（0103 §2.3）。"""
    raw = v1_dataset.model_dump(mode="json")
    _first, second = pair(raw, windows_of_overlapping_examples)
    change_value(frame_of(raw, second), second[3])
    resealed(v1_dataset, raw)
    examples = tuple(
        DatasetExample.model_validate_json(json.dumps(item)) for item in raw["examples"]
    )
    directory = tmp_path / ARTIFACT
    directory.mkdir()
    (directory / DATASET_MANIFEST_FILENAME).write_text(json.dumps(raw["manifest"]))
    (directory / DATASET_EXAMPLES_FILENAME).write_bytes(examples_jsonl_bytes(examples))
    with pytest.raises(ValidationError, match=CONFLICT):
        load_inputs(directory, start_ms=START_MS, end_ms=END_MS)


def test_counterfactual_trainer_rejects_a_v2_dataset_with_a_copied_anchor(tmp_path: Path) -> None:
    """反実仮想の trainer は Dataset v2 を検証し直す（0103 §2.3）。"""
    valid = thermal_dataset()
    copy = valid.examples[0].model_copy(update={"example_id": "copied"})
    items = (*valid.examples, copy)
    forged = ThermalDatasetV2.model_construct(
        manifest=valid.manifest.model_copy(
            update={"example_count": len(items), "examples_sha256": examples_sha256(items)}
        ),
        examples=items,
    )
    with pytest.raises(ValidationError, match=DUPLICATE_ANCHOR):
        train_counterfactual_ridge(
            published(forged, tmp_path), split(forged), training_spec(), metric_catalog=CATALOG
        )
