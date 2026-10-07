"""学習の入口で、元の入力と本番の trace から dataset を作り直して比べる（0112 §2.3。#237）。

- 正当な dataset（元の入力を再生し、本番の trace を ``seq`` ごと写して作ったもの）は通る
- export A の example に、別の正当な export B の fingerprint と束縛を組み合わせた dataset は拒否する
  （PR #264 の Codex の指摘）
- example の値だけを書き換えた dataset、本番の trace と違う trace で作った dataset は拒否する
- 本番の DB の trace が保持期間で消えた期間、移行前の行を含む期間の dataset は拒否する
- 一時の専用 DB は成否にかかわらず消す
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.control import ControlTraceLogger
from coldaisle.control.model.dataset import (
    DatasetExampleV2,
    DatasetSourceKind,
    SourceRun,
    ThermalDatasetV2,
    examples_sha256,
)
from coldaisle.daemon import Daemon
from coldaisle.dataset import ThermalDatasetV2Builder
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.normalize import Normalizer
from coldaisle.ingest.replay import ReplaySource, read_replay_export_inputs
from coldaisle.store import Quality, QualityRules, Reading, Sample, SqliteStore
from coldaisle.store.csv_export import export_day
from coldaisle.store.export_binding import read_training_production
from coldaisle.store.models import SequencedControlTrace
from coldaisle.training_entry import (
    TrainingDatasetRefused,
    published_bytes,
    verify_training_dataset_v2,
)
from test_thermal_dataset_v2 import (
    ALIGNED,
    COVERING_ROW_MS,
    NO_CHANGES,
    RUN_ALIAS,
    SOURCE_ALIAS,
    T0,
    history_db,
    spec_v2,
    tick,
)

UTC = ZoneInfo("UTC")
DAY = date(1970, 1, 1)
END_MS = 7_501


def write_readings(prod: Path, rules: QualityRules, offsets: range) -> None:
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        for offset in offsets:
            store.insert_sample(
                Sample(
                    ts_ms=T0 + offset,
                    readings=(
                        Reading(metric="air.room", value=20.0 + offset / 1_000, quality=Quality.OK),
                        Reading(
                            metric="air.gpu_exhaust",
                            value=40.0 + offset / 1_000,
                            quality=Quality.OK,
                        ),
                    ),
                )
            )


def record_ticks(prod: Path, rules: QualityRules) -> None:
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        logger = ControlTraceLogger(store)
        for ts_ms, tick_id, demands in ALIGNED:
            logger.record(tick(T0 + ts_ms, tick_id, demands))


def export_to(prod: Path, rules: QualityRules, out: Path) -> Path:
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        export_day(store, DAY, tz=UTC, out_dir=out, lock_timeout_s=5.0)
    return out


def build_from(out: Path, prod: Path, rules: QualityRules, dedicated: Path) -> ThermalDatasetV2:
    """元の入力を専用 DB へ再生し、本番の trace を写して Dataset v2 を作る（正当な経路）。"""
    replay = ReplaySource(out, tz=UTC, timezone_explicit=False, bulk=True, dataset_provenance=True)
    inputs = read_replay_export_inputs(out)
    assert inputs.records is not None
    production = read_training_production(prod, inputs.records, start_ms=T0, end_ms=T0 + END_MS)
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
                end_ms=T0 + END_MS,
                source_refs=(SOURCE_ALIAS,),
                source_sha256=inputs.source_sha256,
            ),
            spec=spec_v2(),
            declared_changes=NO_CHANGES,
            calibration_history=production.history,
            export_binding=production.binding,
        )


@pytest.fixture
def world(tmp_path: Path, rules: QualityRules) -> tuple[Path, Path, ThermalDatasetV2]:
    """本番の DB（較正の記録・readings・trace）と、その日の export と、正当な dataset。"""
    prod = Path(history_db(tmp_path / "prod.db", COVERING_ROW_MS))
    write_readings(prod, rules, range(0, 7_001, 1_000))
    record_ticks(prod, rules)
    out = export_to(prod, rules, tmp_path / "csv")
    dataset = build_from(out, prod, rules, tmp_path / "dedicated.db")
    assert dataset.examples, "試験の前提: example がある"
    return prod, out, dataset


def verify(dataset: ThermalDatasetV2, prod: Path, out: Path, rules: QualityRules) -> object:
    return verify_training_dataset_v2(
        dataset,
        production_db=prod,
        replay_path=out,
        declared_changes=NO_CHANGES,
        quality_rules=rules,
    )


def edited(dataset: ThermalDatasetV2, edit: Callable[[dict], None]) -> ThermalDatasetV2:
    raw = json.loads(dataset.model_dump_json())
    edit(raw)
    return ThermalDatasetV2.model_validate_json(json.dumps(raw))


# ---------------------------------------------------------------- 通る


def test_a_dataset_rebuilt_from_the_original_input_passes(world, rules):
    prod, out, dataset = world
    history = verify(dataset, prod, out, rules)
    assert history.rows, "較正の記録を返す（呼び出し側が verify_training_calibration へ進む）"


def test_published_bytes_are_compared_in_full(world):
    _, _, dataset = world
    manifest, examples = published_bytes(dataset)
    assert manifest.endswith(b"}\n") and b'"replay_bindings"' in manifest
    assert examples.count(b"\n") == len(dataset.examples)


# ---------------------------------------------------------------- PR #264 の Codex の指摘


def test_examples_of_export_a_with_the_binding_of_export_b_are_refused(tmp_path, rules):
    """A の example に、同じ日の別の正当な export B の fingerprint と束縛を組み合わせた dataset。

    fingerprint・manifest・束縛・csv_exports の行はどれも B と一致するので、0112 §2.1 の照合は通る。
    元の入力（B）から作り直した dataset と比べて初めて、example が B のものでないと分かる。
    """
    prod = Path(history_db(tmp_path / "prod.db", COVERING_ROW_MS))
    record_ticks(prod, rules)
    write_readings(prod, rules, range(0, 5_001, 1_000))
    out_a = export_to(prod, rules, tmp_path / "a")
    dataset_a = build_from(out_a, prod, rules, tmp_path / "a.db")
    write_readings(prod, rules, range(6_000, 7_001, 1_000))  # その後に届いた readings
    out_b = export_to(prod, rules, tmp_path / "b")
    dataset_b = build_from(out_b, prod, rules, tmp_path / "b.db")
    assert dataset_a.examples != dataset_b.examples, "試験の前提: A と B の example が違う"

    def borrow_b(raw: dict) -> None:
        raw["manifest"]["source_runs"] = json.loads(
            json.dumps([run.model_dump(mode="json") for run in dataset_b.manifest.source_runs])
        )
        raw["manifest"]["replay_bindings"] = [
            binding.model_dump(mode="json") for binding in dataset_b.manifest.replay_bindings or ()
        ]

    forged = edited(dataset_a, borrow_b)
    with pytest.raises(TrainingDatasetRefused, match="作り直した dataset"):
        verify(forged, prod, out_b, rules)
    # 正当な A と B は、それぞれの元の入力で通る
    verify(dataset_a, prod, out_a, rules)
    verify(dataset_b, prod, out_b, rules)
    # A の dataset に B の入力を渡せば、fingerprint で拒否する
    with pytest.raises(TrainingDatasetRefused, match="fingerprint"):
        verify(dataset_a, prod, out_b, rules)


def test_a_rewritten_example_value_is_refused(world, rules):
    prod, out, dataset = world

    def rewrite(raw: dict) -> None:
        frame = raw["examples"][0]["window"][-1]
        frame["values"]["air.room"] += 1.0
        examples = tuple(
            DatasetExampleV2.model_validate_json(json.dumps(item)) for item in raw["examples"]
        )
        raw["manifest"]["examples_sha256"] = examples_sha256(examples)

    with pytest.raises(TrainingDatasetRefused, match="作り直した dataset"):
        verify(edited(dataset, rewrite), prod, out, rules)


def test_a_dataset_whose_traces_were_not_copied_from_production_is_refused(tmp_path, rules):
    """ControlTick の出どころは本番の DB の trace（0112 §2.3 の 2）。

    同じ内容の tick でも、専用 DB で記録し直すと ``seq`` が本番と違い、
    ``control_trace_sha256`` が合わない。
    """
    prod = Path(history_db(tmp_path / "prod.db", COVERING_ROW_MS))
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        # run の期間の外の trace が先にあるので、本番の seq は 2 から始まる
        ControlTraceLogger(store).record(tick(T0 - 10_000, 1, (0.1, 0.1, 0.1)))
    record_ticks(prod, rules)
    write_readings(prod, rules, range(0, 7_001, 1_000))
    out = export_to(prod, rules, tmp_path / "csv")
    honest = build_from(out, prod, rules, tmp_path / "honest.db")
    verify(honest, prod, out, rules)

    def record_locally(store: SqliteStore, _traces: object) -> None:
        logger = ControlTraceLogger(store)
        for ts_ms, tick_id, demands in ALIGNED:
            logger.record(tick(T0 + ts_ms, tick_id, demands))

    original = SqliteStore.copy_control_traces
    SqliteStore.copy_control_traces = record_locally  # type: ignore[method-assign]
    try:
        local = build_from(out, prod, rules, tmp_path / "local.db")
    finally:
        SqliteStore.copy_control_traces = original  # type: ignore[method-assign]
    assert local.examples == honest.examples, "試験の前提: example は同じで trace の seq だけが違う"
    with pytest.raises(TrainingDatasetRefused, match="作り直した dataset"):
        verify(local, prod, out, rules)


# ---------------------------------------------------------------- trace の保持期間と移行前の行


def test_a_period_whose_traces_were_pruned_is_refused(world, rules):
    prod, out, dataset = world
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        store.delete_control_traces_before(T0 + 1)
    with pytest.raises(TrainingDatasetRefused, match="保持期間"):
        verify(dataset, prod, out, rules)


def test_pruning_before_the_run_does_not_refuse(world, rules):
    prod, out, dataset = world
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        store.delete_control_traces_before(T0)
    verify(dataset, prod, out, rules)


def test_a_period_with_legacy_traces_is_refused(world, rules):
    prod, out, dataset = world
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        store.connection.execute("UPDATE control_trace_prune SET legacy_through_seq = 1")
    with pytest.raises(TrainingDatasetRefused, match="移行前"):
        verify(dataset, prod, out, rules)


# ---------------------------------------------------------------- 入口の前提


def test_a_dataset_without_a_binding_is_refused(world, rules):
    prod, out, dataset = world
    bare = dataset.model_copy(
        update={"manifest": dataset.manifest.model_copy(update={"replay_bindings": None})}
    )
    with pytest.raises(TrainingDatasetRefused, match="ReplayBindingV2"):
        verify(bare, prod, out, rules)


def test_an_input_without_manifests_is_refused(world, rules, tmp_path):
    prod, out, dataset = world
    copied = tmp_path / "copied"
    shutil.copytree(out, copied)
    (copied / "sensors_1970-01-01.export.json").unlink()
    with pytest.raises(TrainingDatasetRefused):
        verify(dataset, prod, copied, rules)


def test_a_production_db_without_the_rows_is_refused(world, rules, tmp_path):
    _, out, dataset = world
    other = Path(history_db(tmp_path / "other.db", COVERING_ROW_MS))
    with pytest.raises(ValueError, match="行が無い"):
        verify(dataset, other, out, rules)


def test_declared_changes_must_be_a_tuple(world, rules):
    prod, out, dataset = world
    with pytest.raises(TypeError):
        verify_training_dataset_v2(
            dataset,
            production_db=prod,
            replay_path=out,
            declared_changes=[],  # type: ignore[arg-type]
            quality_rules=rules,
        )


# ---------------------------------------------------------------- 一時の専用 DB


@pytest.mark.parametrize("passes", [True, False])
def test_the_temporary_db_is_removed(world, rules, monkeypatch, passes):
    prod, out, dataset = world
    created: list[Path] = []
    original = tempfile.TemporaryDirectory

    class Recording(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            created.append(Path(self.name))

    monkeypatch.setattr(tempfile, "TemporaryDirectory", Recording)
    target = (
        dataset
        if passes
        else edited(
            dataset,
            lambda raw: raw["manifest"].update(
                excluded={
                    **raw["manifest"]["excluded"],
                    "stale": raw["manifest"]["excluded"]["stale"] + 1,
                }
            ),
        )
    )
    if passes:
        verify(target, prod, out, rules)
    else:
        with pytest.raises(TrainingDatasetRefused):
            verify(target, prod, out, rules)
    assert created and not any(path.exists() for path in created)


def test_copy_control_traces_keeps_seq_and_refuses_a_db_with_traces(tmp_path, rules):
    traces = (
        SequencedControlTrace(seq=5, ts_ms=1, tick_id=1, schema_version=1, trace_json="{}"),
        SequencedControlTrace(seq=9, ts_ms=2, tick_id=2, schema_version=1, trace_json="{}"),
    )
    with SqliteStore(tmp_path / "d.db", rules=rules, clock=SimulatedClock(0)) as store:
        store.copy_control_traces(traces)
        assert [t.seq for t in store.control_traces_in_seq_order(0, 10)] == [5, 9]
        with pytest.raises(ValueError, match="trace のある DB"):
            store.copy_control_traces(traces)
    with (
        SqliteStore(tmp_path / "e.db", rules=rules, clock=SimulatedClock(0)) as store,
        pytest.raises(ValueError, match="seq の順"),
    ):
        store.copy_control_traces(tuple(reversed(traces)))


def test_rows_are_checked_before_the_rebuild(world, rules):
    """0100 §2.8 の行の検査は作り直しの前に行う（写した欄の食い違いは行の検査で分かる）。"""
    prod, out, dataset = world

    def shift(raw: dict) -> None:
        raw["manifest"]["replay_bindings"][0]["exports"][0]["day_end_ms"] -= 1

    with pytest.raises(ValueError, match="欄が csv_exports の行と違う"):
        verify(edited(dataset, shift), prod, out, rules)
