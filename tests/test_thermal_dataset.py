"""Thermal Dataset schema / 収集パイプライン（#83）。"""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from coldaisle.control import ControlTick, ControlTraceLogger
from coldaisle.control.model.dataset import (
    DatasetSourceKind,
    DatasetSpec,
    SourceRun,
    ThermalDataset,
    split_temporally,
)
from coldaisle.daemon import Daemon
from coldaisle.dataset import ThermalDatasetBuilder, replay_fingerprint, write_dataset
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.normalize import Normalizer
from coldaisle.ingest.replay import ReplaySource
from coldaisle.store import Quality, Reading, Sample, SqliteStore

CONTROL_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "control_tick_v1.json"
SHA256 = "a" * 64


def spec() -> DatasetSpec:
    """テスト用の短い値。本番候補ではなく、各項目を明示する。"""
    return DatasetSpec(
        window_ms=3_000,
        sample_period_ms=1_000,
        horizons_ms=(2_000, 4_000),
        target_tolerance_ms=100,
        stale_after_ms=1_500,
        feature_metrics=("air.room", "air.gpu_intake"),
        target_metrics=("air.gpu_exhaust",),
    )


def source_run(*, start_ms: int = 0, end_ms: int = 12_000) -> SourceRun:
    return SourceRun(
        run_id="replay-run-001",
        kind=DatasetSourceKind.REPLAY,
        start_ms=start_ms,
        end_ms=end_ms,
        source_refs=("sensors_example.csv",),
        source_sha256=SHA256,
    )


def fixture_tick(*, ts_ms: int, tick_id: int = 42) -> ControlTick:
    tick = ControlTick.model_validate_json(CONTROL_FIXTURE.read_text(encoding="utf-8"))
    return tick.model_copy(update={"ts_ms": ts_ms, "tick_id": tick_id})


def reading_sample(ts_ms: int, **values: float | None) -> Sample:
    return Sample(
        ts_ms=ts_ms,
        readings=tuple(
            Reading(
                metric=metric,
                value=value,
                quality=Quality.OK if value is not None else Quality.MISSING,
            )
            for metric, value in values.items()
        ),
    )


@pytest.fixture
def dataset_store(tmp_path, rules, clock):
    with SqliteStore(tmp_path / "dataset.db", rules=rules, clock=clock) as store:
        store.insert_samples(
            (
                reading_sample(
                    2_000,
                    **{"air.room": 20.0, "air.gpu_intake": 30.0, "air.gpu_exhaust": 40.0},
                ),
                reading_sample(3_000, **{"air.gpu_intake": None}),
                reading_sample(5_000, **{"air.room": 21.0, "air.gpu_intake": 31.0}),
                reading_sample(7_010, **{"air.gpu_exhaust": 42.0}),
                reading_sample(9_500, **{"air.gpu_exhaust": 44.0}),
            )
        )
        ControlTraceLogger(store).record(fixture_tick(ts_ms=5_000))
        yield store


def test_window_action_and_multi_horizon_targets_share_the_time_base(dataset_store):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())

    assert dataset.manifest.schema_version == 1
    assert dataset.manifest.source_runs[0].run_id == "replay-run-001"
    assert len(dataset.manifest.telemetry_sha256) == 64
    assert len(dataset.manifest.control_trace_sha256) == 64
    example = dataset.examples[0]
    assert [frame.ts_ms for frame in example.window] == [2_000, 3_000, 4_000, 5_000]
    assert example.window[2].source_ts_ms["air.room"] == 2_000
    assert example.window[2].stale_mask["air.room"] is True
    assert example.window[1].missing_mask["air.gpu_intake"] is True

    assert example.action.front.requested_demand == pytest.approx(0.4)
    assert example.action.rear.effective_demand == pytest.approx(0.35)
    assert example.action.rear.override_reasons == ("zone_min_safe_demand",)
    assert example.context.operating_mode == "auto"
    assert example.context.workload_regime is None

    assert [target.horizon_ms for target in example.targets] == [2_000, 4_000]
    assert example.targets[0].source_ts_ms["air.gpu_exhaust"] == 7_010
    assert example.targets[0].values["air.gpu_exhaust"] == pytest.approx(42.0)
    assert example.targets[1].missing_mask["air.gpu_exhaust"] is True
    assert example.label_end_ms == 9_100


def test_no_future_reading_enters_the_input_window(dataset_store):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    example = dataset.examples[0]
    assert all(
        source_ts is None or source_ts <= example.action_ts_ms
        for frame in example.window
        for source_ts in frame.source_ts_ms.values()
    )

    invalid = dataset.model_dump(mode="json")
    invalid["examples"][0]["window"][0]["source_ts_ms"]["air.room"] = example.action_ts_ms + 1
    with pytest.raises(ValidationError, match="frameより後"):
        ThermalDataset.model_validate_json(json.dumps(invalid))


def test_dataset_spec_has_no_window_or_horizon_defaults():
    with pytest.raises(ValidationError, match="window_ms"):
        DatasetSpec.model_validate(
            {
                "sample_period_ms": 1_000,
                "target_tolerance_ms": 100,
                "stale_after_ms": 1_500,
                "feature_metrics": ["air.room"],
                "target_metrics": ["air.room"],
            }
        )


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"window_ms": 3_500}, "整数倍"),
        ({"horizons_ms": (4_000, 2_000)}, "昇順"),
        ({"target_tolerance_ms": 2_000}, "最短 horizon"),
        ({"feature_metrics": ("air.room", "air.room")}, "重複"),
    ],
)
def test_dataset_spec_rejects_ambiguous_shapes(updates, match):
    with pytest.raises(ValidationError, match=match):
        DatasetSpec.model_validate({**spec().model_dump(), **updates})


def test_source_run_rejects_host_paths_and_tracks_a_digest():
    with pytest.raises(ValidationError, match="パス区切り"):
        SourceRun(
            run_id="bad",
            kind=DatasetSourceKind.REPLAY,
            start_ms=0,
            end_ms=1,
            source_refs=("/example/private.csv",),
            source_sha256=SHA256,
        )


def test_temporal_split_purges_examples_whose_window_or_label_crosses_a_boundary(
    dataset_store,
):
    base = (
        ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec()).examples[0]
    )

    def shifted(action_ts_ms: int):
        delta_ms = action_ts_ms - base.action_ts_ms
        return base.model_copy(
            update={
                "history_start_ms": base.history_start_ms + delta_ms,
                "action_ts_ms": action_ts_ms,
                "label_end_ms": base.label_end_ms + delta_ms,
                "window": tuple(
                    frame.model_copy(
                        update={
                            "ts_ms": frame.ts_ms + delta_ms,
                            "source_ts_ms": {
                                metric: None if source_ts is None else source_ts + delta_ms
                                for metric, source_ts in frame.source_ts_ms.items()
                            },
                        }
                    )
                    for frame in base.window
                ),
                "targets": tuple(
                    target.model_copy(
                        update={
                            "expected_ts_ms": target.expected_ts_ms + delta_ms,
                            "source_ts_ms": {
                                metric: None if source_ts is None else source_ts + delta_ms
                                for metric, source_ts in target.source_ts_ms.items()
                            },
                        }
                    )
                    for target in base.targets
                ),
            }
        )

    examples = (
        shifted(4_000),
        shifted(7_000),
        shifted(14_000),
        shifted(17_000),
        shifted(24_000),
    )

    split = split_temporally(examples, validation_start_ms=10_000, test_start_ms=20_000)

    assert split.train == (examples[0],)
    assert split.validation == (examples[2],)
    assert split.test == (examples[4],)
    assert split.purged == (examples[1], examples[3])


def test_artifact_writes_are_deterministic(dataset_store, tmp_path):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    first = write_dataset(dataset, tmp_path / "first")
    second = write_dataset(dataset, tmp_path / "second")

    assert first[0].read_bytes() == second[0].read_bytes()
    assert first[1].read_bytes() == second[1].read_bytes()
    manifest = json.loads(first[0].read_text(encoding="utf-8"))
    assert manifest["source_runs"][0]["source_sha256"] == SHA256


def test_replay_fingerprint_depends_on_names_boundaries_and_contents(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    first = replay_dir / "sensors_2026-09-01.csv"
    first.write_text("timestamp,room_temp\n2026-09-01T00:00:00,20\n", encoding="utf-8")

    refs_before, digest_before = replay_fingerprint(replay_dir)
    first.write_text("timestamp,room_temp\n2026-09-01T00:00:00,21\n", encoding="utf-8")
    refs_after, digest_after = replay_fingerprint(replay_dir)

    assert refs_before == refs_after == (first.name,)
    assert digest_before != digest_after


def test_replay_can_regenerate_the_same_dataset_without_hardware(tmp_path, rules):
    csv_path = tmp_path / "replay.csv"
    csv_path.write_text(
        "timestamp,room_temp,gpu_intake,gpu_exhaust\n"
        "1970-01-01T00:00:02,20,30,40\n"
        "1970-01-01T00:00:03,,31,41\n"
        "1970-01-01T00:00:05,21,32,42\n"
        "1970-01-01T00:00:07,22,33,43\n"
        "1970-01-01T00:00:09,23,34,44\n",
        encoding="utf-8",
    )
    refs, digest = replay_fingerprint(csv_path)
    run = SourceRun(
        run_id="replay-regeneration",
        kind=DatasetSourceKind.REPLAY,
        start_ms=2_000,
        end_ms=11_000,
        source_refs=refs,
        source_sha256=digest,
    )

    def regenerate(db_name: str):
        replay = ReplaySource(csv_path, tz=ZoneInfo("UTC"), bulk=True)
        with SqliteStore(tmp_path / db_name, rules=rules, clock=replay.clock) as store:
            daemon = Daemon(
                source=replay,
                store=store,
                normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
                source_name="replay",
            )
            daemon.run()
            ControlTraceLogger(store).record(fixture_tick(ts_ms=5_000))
            return ThermalDatasetBuilder(store).build(source_run=run, spec=spec())

    assert regenerate("first.db").model_dump() == regenerate("second.db").model_dump()
