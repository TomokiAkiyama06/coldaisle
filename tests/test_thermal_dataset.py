"""Thermal Dataset schema / 収集パイプライン（#83）。"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

import coldaisle.daemon as daemon_module
import coldaisle.dataset as dataset_module
import coldaisle.telemetry_daemon as telemetry_daemon
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
from coldaisle.ingest.protocol import RawMessage
from coldaisle.ingest.replay import ReplaySource
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from coldaisle.store import rollup as rollup_module
from coldaisle.store.rollup import RetentionRules
from conftest import CALIBRATION_PATH, QUALITY_RULES_PATH

CONTROL_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "control_tick_v1.json"
SHA256 = "a" * 64
RUN_ALIAS = "run-00000000000000000000000000000001"
SOURCE_ALIAS = "source-00000000000000000000000000000001"
ARTIFACT_ONE = "dataset-00000000000000000000000000000001"
ARTIFACT_TWO = "dataset-00000000000000000000000000000002"


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
        run_id=RUN_ALIAS,
        kind=DatasetSourceKind.REPLAY,
        start_ms=start_ms,
        end_ms=end_ms,
        source_refs=(SOURCE_ALIAS,),
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
        store.set_system_state("sys.ingest_source", "replay", at_ms=0)
        store.bind_dataset_source_run(
            run_alias=RUN_ALIAS,
            source_kind="replay",
            source_sha256=SHA256,
            at_ms=0,
        )
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
        store.complete_dataset_source_run(at_ms=12_000)
        yield store


def test_window_action_and_multi_horizon_targets_share_the_time_base(dataset_store):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())

    assert dataset.manifest.schema_version == 1
    assert dataset.manifest.source_runs[0].run_id == RUN_ALIAS
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


def test_corrupt_masks_and_regime_context_are_rejected_when_loading_artifact(dataset_store):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())

    bad_missing = dataset.model_dump(mode="json")
    bad_missing["examples"][0]["window"][1]["missing_mask"]["air.gpu_intake"] = False
    with pytest.raises(ValidationError, match="missing_mask"):
        ThermalDataset.model_validate_json(json.dumps(bad_missing))

    bad_stale = dataset.model_dump(mode="json")
    bad_stale["examples"][0]["window"][2]["stale_mask"]["air.room"] = False
    with pytest.raises(ValidationError, match="stale_mask"):
        ThermalDataset.model_validate_json(json.dumps(bad_stale))

    bad_target = dataset.model_dump(mode="json")
    bad_target["examples"][0]["targets"][1]["missing_mask"]["air.gpu_exhaust"] = False
    with pytest.raises(ValidationError, match="未観測target"):
        ThermalDataset.model_validate_json(json.dumps(bad_target))

    unpaired_regime = dataset.model_dump(mode="json")
    unpaired_regime["examples"][0]["context"]["workload_regime"] = "idle"
    with pytest.raises(ValidationError, match="一緒に記録"):
        ThermalDataset.model_validate_json(json.dumps(unpaired_regime))

    unknown_regime = dataset.model_dump(mode="json")
    unknown_regime["examples"][0]["context"]["workload_regime"] = "future_prediction"
    unknown_regime["examples"][0]["context"]["regime_confidence"] = 0.5
    with pytest.raises(ValidationError, match="workload_regime"):
        ThermalDataset.model_validate_json(json.dumps(unknown_regime))

    value_with_missing_mask = dataset.model_dump(mode="json")
    value_with_missing_mask["examples"][0]["window"][1]["values"]["air.gpu_intake"] = 42.0
    with pytest.raises(ValidationError, match="missing_maskはvalue=null"):
        ThermalDataset.model_validate_json(json.dumps(value_with_missing_mask))

    value_with_missing_quality = dataset.model_dump(mode="json")
    window_cell = value_with_missing_quality["examples"][0]["window"][1]
    window_cell["values"]["air.gpu_intake"] = 42.0
    window_cell["missing_mask"]["air.gpu_intake"] = False
    with pytest.raises(ValidationError, match="quality=missingはvalue=null"):
        ThermalDataset.model_validate_json(json.dumps(value_with_missing_quality))

    missing_value_with_ok_quality = dataset.model_dump(mode="json")
    target_cell = missing_value_with_ok_quality["examples"][0]["targets"][0]
    target_cell["values"]["air.gpu_exhaust"] = None
    with pytest.raises(ValidationError, match="missing_maskはvalue=null"):
        ThermalDataset.model_validate_json(json.dumps(missing_value_with_ok_quality))
    target_cell["missing_mask"]["air.gpu_exhaust"] = True
    with pytest.raises(ValidationError, match="missing/suspectだけ"):
        ThermalDataset.model_validate_json(json.dumps(missing_value_with_ok_quality))

    outside_run = dataset.model_dump(mode="json")
    outside_run["examples"][0]["window"][0]["source_ts_ms"]["air.room"] = -1
    outside_run["examples"][0]["window"][0]["stale_mask"]["air.room"] = True
    with pytest.raises(ValidationError, match="source runの期間外"):
        ThermalDataset.model_validate_json(json.dumps(outside_run))

    reversed_metric_time = dataset.model_dump(mode="json")
    reversed_metric_time["examples"][0]["window"][3]["source_ts_ms"]["air.room"] = 1_000
    reversed_metric_time["examples"][0]["window"][3]["stale_mask"]["air.room"] = True
    with pytest.raises(ValidationError, match="逆行"):
        ThermalDataset.model_validate_json(json.dumps(reversed_metric_time))

    nonfinite_window = dataset.model_dump()
    nonfinite_window["examples"][0]["window"][0]["values"]["air.room"] = float("nan")
    with pytest.raises(ValidationError, match="finite number"):
        ThermalDataset.model_validate(nonfinite_window)

    nonfinite_target = dataset.model_dump()
    nonfinite_target["examples"][0]["targets"][0]["values"]["air.gpu_exhaust"] = float("inf")
    with pytest.raises(ValidationError, match="finite number"):
        ThermalDataset.model_validate(nonfinite_target)


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
    with pytest.raises(ValidationError):
        SourceRun(
            run_id=RUN_ALIAS,
            kind=DatasetSourceKind.REPLAY,
            start_ms=0,
            end_ms=1,
            source_refs=("/example/private.csv",),
            source_sha256=SHA256,
        )


@pytest.mark.parametrize(
    ("run_alias", "source_alias"),
    [
        ("coldaisle-rpi-01", SOURCE_ALIAS),
        (RUN_ALIAS, ".".join(("192", "0", "2", "1"))),
        (RUN_ALIAS, "28-00000abcdef0"),
        (RUN_ALIAS, ":".join(("aa", "bb", "cc", "dd", "ee", "ff"))),
    ],
)
def test_source_run_accepts_only_opaque_public_aliases(run_alias, source_alias):
    with pytest.raises(ValidationError):
        SourceRun(
            run_id=run_alias,
            kind=DatasetSourceKind.REPLAY,
            start_ms=0,
            end_ms=1,
            source_refs=(source_alias,),
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


def test_builder_rejects_a_database_reused_outside_the_source_run(dataset_store):
    _drop_seal_triggers(dataset_store)  # 封印より先に期間外の検査で拒否されることを見る
    dataset_store.insert_sample(reading_sample(12_000, **{"air.room": 99.0}))

    with pytest.raises(ValueError, match="専用DB"):
        ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())


def test_builder_rejects_mixed_ingest_source_provenance(dataset_store):
    dataset_store.set_system_state("sys.ingest_source", "serial", at_ms=1)

    with pytest.raises(ValueError, match="ingest source"):
        ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())


def test_builder_binds_replay_source_hash_to_database_provenance(dataset_store):
    mismatched = source_run().model_copy(update={"source_sha256": "b" * 64})

    with pytest.raises(ValueError, match="immutable provenance"):
        ThermalDatasetBuilder(dataset_store).build(source_run=mismatched, spec=spec())


def test_dataset_source_run_binding_is_immutable_even_at_the_same_timestamp(dataset_store):
    with pytest.raises(ValueError, match="既に別のsource run"):
        dataset_store.bind_dataset_source_run(
            run_alias="run-00000000000000000000000000000002",
            source_kind="replay",
            source_sha256="b" * 64,
            at_ms=0,
        )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        dataset_store.connection.execute(
            "UPDATE dataset_source_run SET source_sha256 = ? WHERE singleton = 1",
            ("b" * 64,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="already bound"):
        dataset_store.connection.execute(
            "INSERT OR REPLACE INTO dataset_source_run "
            "(singleton, run_alias, source_kind, source_sha256, bound_ms) "
            "VALUES (1, ?, 'replay', ?, 0)",
            ("run-00000000000000000000000000000002", "b" * 64),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        dataset_store.connection.execute("DELETE FROM dataset_source_run WHERE singleton = 1")


def test_dataset_source_run_must_bind_before_any_source_rows(tmp_path, rules, clock):
    with SqliteStore(tmp_path / "already-used.db", rules=rules, clock=clock) as store:
        store.insert_sample(reading_sample(0, **{"air.room": 20.0}))
        with pytest.raises(ValueError, match="空の専用DB"):
            store.bind_dataset_source_run(
                run_alias=RUN_ALIAS,
                source_kind="replay",
                source_sha256=SHA256,
                at_ms=0,
            )


def test_bound_dataset_database_rejects_daemon_without_run_alias(dataset_store, tmp_path, rules):
    csv_path = tmp_path / "second-replay.csv"
    csv_path.write_text(
        "timestamp,room_temp\n1970-01-01T00:00:00,99\n",
        encoding="utf-8",
    )
    replay = ReplaySource(csv_path, tz=ZoneInfo("UTC"), bulk=True)
    daemon = Daemon(
        source=replay,
        store=dataset_store,
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
        source_name="replay",
    )

    with pytest.raises(ValueError, match="run alias無し"):
        daemon.run()


def test_builder_reads_all_series_from_one_sqlite_snapshot(
    dataset_store, rules, clock, monkeypatch
):
    database_path = Path(
        dataset_store.connection.execute("PRAGMA database_list").fetchone()["file"]
    )
    original_series = dataset_store.series
    injected = False
    # 並行ingestを模すため封印triggerを外す。snapshot内のdigest検査は注入前の状態を見る
    _drop_seal_triggers(dataset_store)
    with SqliteStore(database_path, rules=rules, clock=clock) as writer:

        def series_with_concurrent_ingest(metric, start_ms, end_ms, *, limit=None):
            nonlocal injected
            result = original_series(metric, start_ms, end_ms, limit=limit)
            if not injected:
                injected = True
                # Storeは別writerを拒否するため、生SQLで並行書き込みを模す
                writer.connection.execute(
                    "INSERT INTO readings VALUES ('air.gpu_exhaust', 7000, 99.0, 'ok')"
                )
            return result

        monkeypatch.setattr(dataset_store, "series", series_with_concurrent_ingest)
        dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())

    assert injected is True
    assert dataset.examples[0].targets[0].source_ts_ms["air.gpu_exhaust"] == 7_010
    assert dataset.examples[0].targets[0].values["air.gpu_exhaust"] == pytest.approx(42.0)


def test_artifact_writes_are_deterministic(dataset_store, tmp_path):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    output_root = tmp_path / "artifacts"
    first = write_dataset(dataset, output_root, ARTIFACT_ONE)
    second = write_dataset(dataset, output_root, ARTIFACT_TWO)

    assert first[0].read_bytes() == second[0].read_bytes()
    assert first[1].read_bytes() == second[1].read_bytes()
    manifest = json.loads(first[0].read_text(encoding="utf-8"))
    assert manifest["source_runs"][0]["source_sha256"] == SHA256
    assert (
        manifest["examples_sha256"]
        == dataset_module.hashlib.sha256(first[1].read_bytes()).hexdigest()
    )


def test_artifact_always_refuses_overwrite(dataset_store, tmp_path):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    output_root = tmp_path / "artifacts"
    manifest_path, examples_path = write_dataset(dataset, output_root, ARTIFACT_ONE)
    original = (manifest_path.read_bytes(), examples_path.read_bytes())

    with pytest.raises(FileExistsError):
        write_dataset(dataset, output_root, ARTIFACT_ONE)
    assert (manifest_path.read_bytes(), examples_path.read_bytes()) == original


def test_artifact_writer_revalidates_model_copy_updates(dataset_store, tmp_path):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    invalid = dataset.model_copy(
        update={"manifest": dataset.manifest.model_copy(update={"example_count": 999})}
    )

    with pytest.raises(ValidationError, match="example_count"):
        write_dataset(invalid, tmp_path / "artifacts", ARTIFACT_ONE)
    assert not (tmp_path / "artifacts").exists()


def test_existing_corrupt_or_unknown_artifact_is_never_touched(dataset_store, tmp_path):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    output_root = tmp_path / "artifacts"
    manifest_path, examples_path = write_dataset(dataset, output_root, ARTIFACT_ONE)
    examples_path.write_bytes(examples_path.read_bytes() + b" ")
    corrupt = (manifest_path.read_bytes(), examples_path.read_bytes())

    with pytest.raises(FileExistsError):
        write_dataset(dataset, output_root, ARTIFACT_ONE)
    assert (manifest_path.read_bytes(), examples_path.read_bytes()) == corrupt

    extra = manifest_path.parent / "unexpected"
    extra.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_dataset(dataset, output_root, ARTIFACT_ONE)
    assert extra.read_text(encoding="utf-8") == "keep"


def test_artifact_publish_failure_removes_staging(dataset_store, tmp_path, monkeypatch):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    output_root = tmp_path / "artifacts"

    def fail_publish(_source, _target, *, src_dir_fd, dst_dir_fd):
        raise OSError("injected publish failure")

    monkeypatch.setattr(dataset_module.os, "rename", fail_publish)
    with pytest.raises(OSError, match="injected"):
        write_dataset(dataset, output_root, ARTIFACT_ONE)
    assert not (output_root / ARTIFACT_ONE).exists()
    assert all(not path.name.startswith(".coldaisle-stage-") for path in output_root.iterdir())


def test_artifact_writers_share_a_persistent_parent_lock(dataset_store, tmp_path):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    output_root = tmp_path / "artifacts"
    write_dataset(dataset, output_root, ARTIFACT_ONE)
    lock = output_root / ".coldaisle-dataset.lock"
    first_inode = lock.stat().st_ino
    write_dataset(dataset, output_root, ARTIFACT_TWO)

    assert lock.is_file()
    assert lock.stat().st_ino == first_inode


def test_staging_open_failure_removes_created_directory(dataset_store, tmp_path, monkeypatch):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    output_root = tmp_path / "artifacts"
    real_open = dataset_module.os.open

    def fail_staging_open(path, flags, mode=0o777, *, dir_fd=None):
        if isinstance(path, str) and path.startswith(".coldaisle-stage-"):
            raise OSError("injected staging open failure")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(dataset_module.os, "open", fail_staging_open)
    with pytest.raises(OSError, match="injected staging"):
        write_dataset(dataset, output_root, ARTIFACT_ONE)

    assert all(not path.name.startswith(".coldaisle-stage-") for path in output_root.iterdir())


def test_artifact_writer_never_follows_root_final_or_component_links(dataset_store, tmp_path):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        write_dataset(dataset, linked_root, ARTIFACT_ONE)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"

    output_root = tmp_path / "artifacts"
    output_root.mkdir()
    output_root.chmod(0o700)
    (output_root / ARTIFACT_ONE).symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileExistsError):
        write_dataset(dataset, output_root, ARTIFACT_ONE)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_artifact_writer_rejects_fifo_without_blocking(dataset_store, tmp_path):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    output_root = tmp_path / "artifacts"
    output_root.mkdir()
    output_root.chmod(0o700)
    os.mkfifo(output_root / ARTIFACT_ONE)

    with pytest.raises(FileExistsError):
        write_dataset(dataset, output_root, ARTIFACT_ONE)


def test_existing_artifact_components_are_not_inspected_or_touched(dataset_store, tmp_path):
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    output_root = tmp_path / "artifacts"
    artifact = output_root / ARTIFACT_ONE
    artifact.mkdir(parents=True)
    output_root.chmod(0o700)
    outside = tmp_path / "outside-manifest"
    outside.write_text("do not touch", encoding="utf-8")
    (artifact / "manifest.json").symlink_to(outside)
    os.mkfifo(artifact / "examples.jsonl")

    with pytest.raises(FileExistsError):
        write_dataset(dataset, output_root, ARTIFACT_ONE)
    assert outside.read_text(encoding="utf-8") == "do not touch"


def test_replay_fingerprint_depends_on_names_boundaries_and_contents(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    first = replay_dir / "sensors_2026-09-01.csv"
    first.write_text("timestamp,room_temp\n2026-09-01T00:00:00,20\n", encoding="utf-8")

    digest_before = replay_fingerprint(replay_dir)
    first.write_text("timestamp,room_temp\n2026-09-01T00:00:00,21\n", encoding="utf-8")
    digest_after = replay_fingerprint(replay_dir)

    assert digest_before != digest_after


def test_csv_basename_is_not_written_to_public_artifact(dataset_store, tmp_path):
    private_name = "_".join(
        (
            "coldaisle-rpi-01",
            ".".join(("192", "0", "2", "1")),
            "-".join(("28", "00000abcdef0.csv")),
        )
    )
    csv_path = tmp_path / private_name
    csv_path.write_text("timestamp,room_temp\n1970-01-01T00:00:02,20\n", encoding="utf-8")
    assert len(replay_fingerprint(csv_path)) == 64
    dataset = ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())
    paths = write_dataset(dataset, tmp_path / "artifacts", ARTIFACT_ONE)

    assert all(private_name.encode() not in path.read_bytes() for path in paths)


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
    digest = replay_fingerprint(csv_path)
    run = SourceRun(
        run_id=RUN_ALIAS,
        kind=DatasetSourceKind.REPLAY,
        start_ms=2_000,
        end_ms=11_000,
        source_refs=(SOURCE_ALIAS,),
        source_sha256=digest,
    )

    def regenerate(db_name: str):
        replay = ReplaySource(
            csv_path,
            tz=ZoneInfo("UTC"),
            bulk=True,
            dataset_provenance=True,
        )
        with SqliteStore(tmp_path / db_name, rules=rules, clock=replay.clock) as store:
            daemon = Daemon(
                source=replay,
                store=store,
                normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
                source_name="replay",
                dataset_run_alias=RUN_ALIAS,
            )
            daemon.run()
            ControlTraceLogger(store).record(fixture_tick(ts_ms=5_000))
            return ThermalDatasetBuilder(store).build(source_run=run, spec=spec())

    assert regenerate("first.db").model_dump() == regenerate("second.db").model_dump()


def _long_replay_csv(path: Path, rows: int) -> None:
    lines = ["timestamp,room_temp,gpu_intake,gpu_exhaust"]
    lines += [
        f"1970-01-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d},20,30,40" for i in range(rows)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_dataset_replay_applies_backpressure_instead_of_dropping(tmp_path, rules, monkeypatch):
    """bulkのdataset Replayで待ち行列が溢れても、1件も捨てずに全行を保存する。

    捨てるとDBはCSVの一部になり、provenanceに記録した全体のhashと食い違う。
    """
    monkeypatch.setattr(daemon_module, "QUEUE_SIZE", 1)
    rows = 500
    csv_path = tmp_path / "replay.csv"
    _long_replay_csv(csv_path, rows)
    replay = ReplaySource(csv_path, tz=ZoneInfo("UTC"), bulk=True, dataset_provenance=True)
    with SqliteStore(tmp_path / "run.db", rules=rules, clock=replay.clock) as store:
        daemon = Daemon(
            source=replay,
            store=store,
            normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
            source_name="replay",
            dataset_run_alias=RUN_ALIAS,
        )
        stats = daemon.run()

    assert stats.queue_drops == 0
    assert stats.samples == rows


def test_dataset_replay_rejects_partial_runs(tmp_path, rules):
    csv_path = tmp_path / "replay.csv"
    _long_replay_csv(csv_path, 3)
    replay = ReplaySource(csv_path, tz=ZoneInfo("UTC"), bulk=True, dataset_provenance=True)
    with SqliteStore(tmp_path / "run.db", rules=rules, clock=replay.clock) as store:
        daemon = Daemon(
            source=replay,
            store=store,
            normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
            source_name="replay",
            dataset_run_alias=RUN_ALIAS,
        )
        with pytest.raises(ValueError, match="max_samples"):
            daemon.run(max_samples=1)
        assert store.dataset_source_run() is None


class _InterruptedReplay(ReplaySource):
    """数件流したところで停止要求（SIGTERM相当）を出すReplay。"""

    on_interrupt: Callable[[], None] = staticmethod(lambda: None)

    def stream(self) -> Iterator[RawMessage]:
        for index, message in enumerate(super().stream()):
            if index == 5:
                self.on_interrupt()
            yield message


def _replay_run(tmp_path: Path, rows: int) -> SourceRun:
    csv_path = tmp_path / "replay.csv"
    _long_replay_csv(csv_path, rows)
    return SourceRun(
        run_id=RUN_ALIAS,
        kind=DatasetSourceKind.REPLAY,
        start_ms=0,
        end_ms=rows * 1_000,
        source_refs=(SOURCE_ALIAS,),
        source_sha256=replay_fingerprint(csv_path),
    )


def test_interrupted_dataset_replay_is_not_marked_complete_and_cannot_build(tmp_path, rules):
    """途中停止したDBは入力の先頭だけを持つ。全体hashのprovenanceで公開させない。"""
    run = _replay_run(tmp_path, 200)
    replay = _InterruptedReplay(
        tmp_path / "replay.csv", tz=ZoneInfo("UTC"), bulk=True, dataset_provenance=True
    )
    with SqliteStore(tmp_path / "run.db", rules=rules, clock=replay.clock) as store:
        daemon = Daemon(
            source=replay,
            store=store,
            normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
            source_name="replay",
            dataset_run_alias=RUN_ALIAS,
        )
        replay.on_interrupt = daemon.request_stop
        stats = daemon.run()
        ControlTraceLogger(store).record(fixture_tick(ts_ms=5_000))

        assert stats.dataset_incomplete
        assert stats.samples < 200
        assert not store.dataset_source_run_completed()
        with pytest.raises(ValueError, match="途中停止"):
            ThermalDatasetBuilder(store).build(source_run=run, spec=spec())


def test_complete_dataset_replay_is_marked_complete_and_builds(tmp_path, rules):
    run = _replay_run(tmp_path, 20)
    replay = ReplaySource(
        tmp_path / "replay.csv", tz=ZoneInfo("UTC"), bulk=True, dataset_provenance=True
    )
    with SqliteStore(tmp_path / "run.db", rules=rules, clock=replay.clock) as store:
        daemon = Daemon(
            source=replay,
            store=store,
            normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
            source_name="replay",
            dataset_run_alias=RUN_ALIAS,
        )
        stats = daemon.run()
        ControlTraceLogger(store).record(fixture_tick(ts_ms=5_000))

        assert not stats.dataset_incomplete
        assert store.dataset_source_run_completed()
        assert ThermalDatasetBuilder(store).build(source_run=run, spec=spec()).examples


def test_completion_marker_is_immutable_and_requires_a_bind(dataset_store, tmp_path, rules, clock):
    with pytest.raises(ValueError, match="完了"):
        dataset_store.complete_dataset_source_run(at_ms=13_000)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        dataset_store.connection.execute("DELETE FROM dataset_source_run_complete")
    with (
        SqliteStore(tmp_path / "unbound.db", rules=rules, clock=clock) as store,
        pytest.raises(ValueError, match="完了"),
    ):
        store.complete_dataset_source_run(at_ms=0)


@pytest.mark.parametrize(("interrupt", "expected_code"), [(True, 1), (False, 0)])
def test_daemon_cli_exits_non_zero_when_dataset_replay_is_interrupted(
    tmp_path, monkeypatch, interrupt, expected_code
):
    _long_replay_csv(tmp_path / "replay.csv", 20)
    original_run = Daemon.run

    def run(self: Daemon, *, max_samples: int | None = None):
        if interrupt:
            self.request_stop()  # 取り込み中のSIGTERMと同じ経路
        return original_run(self, max_samples=max_samples)

    monkeypatch.setattr(Daemon, "run", run)
    code = daemon_module.main(
        [
            "--source",
            "replay",
            "--csv",
            str(tmp_path / "replay.csv"),
            "--bulk",
            "--timezone",
            "UTC",
            "--dataset-run-alias",
            RUN_ALIAS,
            "--db",
            str(tmp_path / "cli.db"),
            "--quality-rules",
            str(QUALITY_RULES_PATH),
            "--calibration",
            str(CALIBRATION_PATH),
        ]
    )

    assert code == expected_code


def _fail_one_insert(monkeypatch: pytest.MonkeyPatch, *, at_call: int) -> None:
    """保存の失敗を1件だけ起こす。取り込みループは捨てて継続する。"""
    original = SqliteStore.insert_sample
    calls = {"n": 0}

    def insert_sample(self: SqliteStore, sample: Sample) -> int:
        calls["n"] += 1
        if calls["n"] == at_call:
            raise sqlite3.OperationalError("disk I/O error")
        return original(self, sample)

    monkeypatch.setattr(SqliteStore, "insert_sample", insert_sample)


def test_dataset_replay_with_a_discarded_sample_is_not_complete(tmp_path, rules, monkeypatch):
    """EOFまで届いても、途中で1件捨てたDBは入力の一部しか持たない。"""
    run = _replay_run(tmp_path, 20)
    _fail_one_insert(monkeypatch, at_call=3)
    replay = ReplaySource(
        tmp_path / "replay.csv", tz=ZoneInfo("UTC"), bulk=True, dataset_provenance=True
    )
    with SqliteStore(tmp_path / "run.db", rules=rules, clock=replay.clock) as store:
        daemon = Daemon(
            source=replay,
            store=store,
            normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
            source_name="replay",
            dataset_run_alias=RUN_ALIAS,
        )
        stats = daemon.run()
        ControlTraceLogger(store).record(fixture_tick(ts_ms=5_000))

        assert stats.discarded == 1
        assert stats.samples == 19, "1件の失敗で取り込みループは落ちない"
        assert stats.dataset_incomplete
        assert not store.dataset_source_run_completed()
        with pytest.raises(ValueError, match="途中停止"):
            ThermalDatasetBuilder(store).build(source_run=run, spec=spec())


def test_daemon_cli_exits_non_zero_when_a_dataset_sample_is_discarded(tmp_path, monkeypatch):
    _long_replay_csv(tmp_path / "replay.csv", 20)
    _fail_one_insert(monkeypatch, at_call=3)

    code = daemon_module.main(
        [
            "--source",
            "replay",
            "--csv",
            str(tmp_path / "replay.csv"),
            "--bulk",
            "--timezone",
            "UTC",
            "--dataset-run-alias",
            RUN_ALIAS,
            "--db",
            str(tmp_path / "cli.db"),
            "--quality-rules",
            str(QUALITY_RULES_PATH),
            "--calibration",
            str(CALIBRATION_PATH),
        ]
    )

    assert code == 1


_CLEAN_HEADER = "timestamp,room_temp,gpu_intake,gpu_exhaust"


def _clean_rows(count: int) -> list[str]:
    return [f"1970-01-01T00:00:{second:02d},20,30,40" for second in range(count)]


def _ingest_dataset_replay(tmp_path: Path, rules, lines: list[str]):
    csv_path = tmp_path / "replay.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    replay = ReplaySource(csv_path, tz=ZoneInfo("UTC"), bulk=True, dataset_provenance=True)
    store = SqliteStore(tmp_path / "run.db", rules=rules, clock=replay.clock)
    daemon = Daemon(
        source=replay,
        store=store,
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
        source_name="replay",
        dataset_run_alias=RUN_ALIAS,
    )
    return store, daemon.run()


@pytest.mark.parametrize(
    ("defect", "loss"),
    [
        ("not-a-timestamp,20,30,40", "source_dropped_rows"),
        (",20,30,40", "source_dropped_rows"),
        ("1970-01-01T00:00:30,20,30,40,99", "source_malformed_rows"),
        ("1970-01-01T00:00:30,20,30", "source_malformed_rows"),
        ("1970-01-01T00:00:30,20,ERR,40", "source_unparsed_cells"),
        ("1970-01-01T00:00:09,20,30,40", "duplicates"),
    ],
)
def test_every_ingest_loss_marks_the_dataset_replay_incomplete(tmp_path, rules, defect, loss):
    """source → normalizer → store のどこで欠けても、完了の印を付けない。

    取り込み自体は壊れた行を飛ばして最後まで進む。
    """
    lines = [_CLEAN_HEADER, *_clean_rows(10), defect, "1970-01-01T00:00:40,21,31,41"]
    store, stats = _ingest_dataset_replay(tmp_path, rules, lines)
    try:
        assert stats.samples >= 11, "壊れた行の後も取り込みを続ける"
        assert stats.dataset_incomplete, loss
        assert not store.dataset_source_run_completed()
    finally:
        store.close()


def test_dropped_timestamp_row_makes_builder_refuse(tmp_path, rules):
    lines = [_CLEAN_HEADER, *_clean_rows(10), "garbage,20,30,40"]
    store, _stats = _ingest_dataset_replay(tmp_path, rules, lines)
    try:
        ControlTraceLogger(store).record(fixture_tick(ts_ms=5_000))
        run = SourceRun(
            run_id=RUN_ALIAS,
            kind=DatasetSourceKind.REPLAY,
            start_ms=0,
            end_ms=20_000,
            source_refs=(SOURCE_ALIAS,),
            source_sha256=replay_fingerprint(tmp_path / "replay.csv"),
        )
        with pytest.raises(ValueError, match="途中停止"):
            ThermalDatasetBuilder(store).build(source_run=run, spec=spec())
    finally:
        store.close()


def test_intentional_exclusions_do_not_mark_the_replay_incomplete(tmp_path, rules):
    """空欄（欠測）、空行、対応表に無い列は取りこぼしではない。

    どれも入力hashに含まれ、同じbytesからは同じDBになる。空欄は欠測として保存され、
    空行はデータを持たず、対応表に無い列はdataset契約の外（決定記録 0010）。
    """
    lines = [
        f"{_CLEAN_HEADER},vrm_temp",
        "1970-01-01T00:00:00,20,30,40,55",
        "",
        "1970-01-01T00:00:01,,30,40,55",
        "1970-01-01T00:00:02,20, ,40,",
    ]
    store, stats = _ingest_dataset_replay(tmp_path, rules, lines)
    try:
        assert not stats.dataset_incomplete
        assert store.dataset_source_run_completed()
    finally:
        store.close()


def test_daemon_cli_exits_non_zero_when_a_replay_row_is_dropped(tmp_path):
    (tmp_path / "replay.csv").write_text(
        "\n".join([_CLEAN_HEADER, *_clean_rows(10), "garbage,20,30,40"]) + "\n",
        encoding="utf-8",
    )

    code = daemon_module.main(
        [
            "--source",
            "replay",
            "--csv",
            str(tmp_path / "replay.csv"),
            "--bulk",
            "--timezone",
            "UTC",
            "--dataset-run-alias",
            RUN_ALIAS,
            "--db",
            str(tmp_path / "cli.db"),
            "--quality-rules",
            str(QUALITY_RULES_PATH),
            "--calibration",
            str(CALIBRATION_PATH),
        ]
    )

    assert code == 1


def test_non_finite_cells_survive_replay_and_build_as_masked_suspect(tmp_path, rules):
    """`inf`は値を落として`quality=suspect`で保存される。1件でbuild全体を落とさない。

    windowでは値の無いsuspect cellとしてmaskし、targetでは欠測と同じく使えない値とする。
    """
    rows = [f"1970-01-01T00:00:{second:02d},20,30,40" for second in range(13)]
    rows[4] = "1970-01-01T00:00:04,inf,30,40"
    rows[7] = "1970-01-01T00:00:07,20,30,-inf"
    store, stats = _ingest_dataset_replay(tmp_path, rules, [_CLEAN_HEADER, *rows])
    try:
        assert not stats.dataset_incomplete, "非有限値は取りこぼしではない"
        ControlTraceLogger(store).record(fixture_tick(ts_ms=5_000))
        run = SourceRun(
            run_id=RUN_ALIAS,
            kind=DatasetSourceKind.REPLAY,
            start_ms=0,
            end_ms=13_000,
            source_refs=(SOURCE_ALIAS,),
            source_sha256=replay_fingerprint(tmp_path / "replay.csv"),
        )
        dataset = ThermalDatasetBuilder(store).build(source_run=run, spec=spec())
    finally:
        store.close()

    (example,) = dataset.examples
    window_cell = next(frame for frame in example.window if frame.ts_ms == 4_000)
    assert window_cell.values["air.room"] is None
    assert window_cell.quality["air.room"] is Quality.SUSPECT
    assert window_cell.missing_mask["air.room"] is True
    target = next(frame for frame in example.targets if frame.horizon_ms == 2_000)
    assert target.source_ts_ms["air.gpu_exhaust"] == 7_000
    assert target.values["air.gpu_exhaust"] is None
    assert target.quality["air.gpu_exhaust"] is Quality.SUSPECT
    assert target.missing_mask["air.gpu_exhaust"] is True
    # artifactとして読み戻しても同じ検証を通る
    assert ThermalDataset.model_validate_json(dataset.model_dump_json()) == dataset


def test_header_collision_marks_the_dataset_replay_incomplete(tmp_path, rules):
    """`room`と`room_temp`は同じ列になり、前の列の値を黙って失う。"""
    lines = [
        "timestamp,room,room_temp,gpu_intake,gpu_exhaust",
        *[f"1970-01-01T00:00:{second:02d},19,20,30,40" for second in range(5)],
    ]
    store, stats = _ingest_dataset_replay(tmp_path, rules, lines)
    try:
        assert stats.samples == 5, "取り込みは続ける"
        assert stats.dataset_incomplete
        assert not store.dataset_source_run_completed()
    finally:
        store.close()


def _drop_seal_triggers(store: SqliteStore) -> None:
    """triggerを外したDB（手作業・旧版・別ツール）でも、digestで変更を検出できることを試す。"""
    for action in ("insert", "update", "delete"):
        store.connection.execute(f"DROP TRIGGER readings_sealed_no_{action}")


def test_sealed_readings_refuse_writes_after_completion(dataset_store):
    """完了後に別のwriter（telemetry追記・retention削除）が黙って書き換えられない。"""
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        dataset_store.insert_sample(reading_sample(10_000, **{"air.room": 99.0}))
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        dataset_store.connection.execute("DELETE FROM readings WHERE ts_ms = 2000")
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        dataset_store.connection.execute("UPDATE readings SET value = 0 WHERE ts_ms = 2000")

    assert ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())


def test_builder_refuses_readings_appended_after_completion(dataset_store):
    _drop_seal_triggers(dataset_store)
    dataset_store.insert_sample(reading_sample(10_000, **{"air.room": 99.0}))

    with pytest.raises(ValueError, match="完了後にreadingsが変更"):
        ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())


def test_builder_refuses_readings_deleted_after_completion(dataset_store):
    _drop_seal_triggers(dataset_store)
    dataset_store.connection.execute("DELETE FROM readings WHERE ts_ms = 9500")

    with pytest.raises(ValueError, match="完了後にreadingsが変更"):
        ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())


def test_builder_refuses_readings_updated_after_completion(dataset_store):
    _drop_seal_triggers(dataset_store)
    dataset_store.connection.execute("UPDATE readings SET value = 41.0 WHERE ts_ms = 7010")

    with pytest.raises(ValueError, match="完了後にreadingsが変更"):
        ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())


def test_rollup_and_retention_are_refused_on_a_dataset_db(dataset_store):
    """保持期間でreadingsやControlTickを消すと、datasetの再生成が黙って別物になる。"""
    retention = RetentionRules.from_yaml(QUALITY_RULES_PATH.parent / "retention.yaml")
    traces_before = dataset_store.control_traces(0, 20_000)

    with pytest.raises(ValueError, match="dataset専用DB"):
        rollup_module.run(dataset_store, retention, now_ms=10**13)
    with pytest.raises(ValueError, match="dataset専用DB"):
        rollup_module.apply_retention(dataset_store, retention, now_ms=10**13)

    assert dataset_store.control_traces(0, 20_000) == traces_before
    assert ThermalDatasetBuilder(dataset_store).build(source_run=source_run(), spec=spec())


def test_foreign_writer_is_refused_while_the_dataset_run_is_bound_but_incomplete(
    tmp_path, rules, monkeypatch
):
    """取り込み中（完了の印の前）でも、並行する別writerのreadingsは入れない。

    bindしたReplay取り込みは最後まで書け、完了の印が付く。
    """
    _long_replay_csv(tmp_path / "replay.csv", 20)
    database = tmp_path / "run.db"
    refused: list[Exception] = []

    class _ConcurrentTelemetry(ReplaySource):
        def stream(self) -> Iterator[RawMessage]:
            for index, message in enumerate(super().stream()):
                if index == 5:
                    with SqliteStore(database, rules=rules, clock=self.clock) as telemetry:
                        try:
                            telemetry.insert_sample(reading_sample(5_500, **{"air.room": 99.0}))
                        except ValueError as error:
                            refused.append(error)
                yield message

    replay = _ConcurrentTelemetry(
        tmp_path / "replay.csv", tz=ZoneInfo("UTC"), bulk=True, dataset_provenance=True
    )
    with SqliteStore(database, rules=rules, clock=replay.clock) as store:
        daemon = Daemon(
            source=replay,
            store=store,
            normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
            source_name="replay",
            dataset_run_alias=RUN_ALIAS,
        )
        stats = daemon.run()

        assert len(refused) == 1
        assert "bind" in str(refused[0])
        assert stats.samples == 20
        assert not stats.dataset_incomplete
        assert store.dataset_source_run_completed()
        assert store.series("air.room", 0, 30_000)[-1].value == 20.0
        assert all(point.ts_ms != 5_500 for point in store.series("air.room", 0, 30_000))


def test_internal_telemetry_refuses_to_start_on_a_dataset_bound_db(dataset_store, tmp_path):
    database = Path(dataset_store.connection.execute("PRAGMA database_list").fetchone()["file"])

    with pytest.raises(SystemExit, match="bind済み"):
        telemetry_daemon.build(
            telemetry_daemon.Config(
                db=database,
                telemetry=QUALITY_RULES_PATH.parent / "internal-telemetry.yaml",
                quality_rules=QUALITY_RULES_PATH,
                metrics=QUALITY_RULES_PATH.parent / "metrics.yaml",
            )
        )
