"""Dataset v2 の内部 Telemetry は本番の DB から読む（決定記録 0184。#83 / #84）。

- 日次 CSV の列に無い metric（``power.gpu.0``）は本番の DB から、``air.*`` は専用 DB（再生）から
  読み、どちらも同じ規則で cell になる
- 学習の入口は本番の DB から読み直し、系列が1点でも違えば作り直した bytes が合わず拒否する
- 期間の先頭より前の raw が無い metric は拒否する（保持期間で消えた期間）
- 内部 Telemetry を使わない spec では ``production_metrics`` は空の列
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import coldaisle.daemon as daemon_module
import coldaisle.dataset as dataset_module
from coldaisle.clock import SimulatedClock
from coldaisle.control.model.dataset import (
    DatasetManifestV2,
    DatasetSourceKind,
    SourceRun,
    ThermalDatasetV2,
    production_metrics_of,
)
from coldaisle.dataset import ThermalDatasetV2Builder, production_trace_sink
from coldaisle.ingest.replay import read_replay_export_inputs
from coldaisle.store import Quality, QualityRules, Reading, Sample, SeriesPoint, SqliteStore
from coldaisle.store.export_binding import ProductionTelemetryError, read_training_production
from coldaisle.training_entry import TrainingDatasetRefused, verify_training_dataset_v2
from conftest import CALIBRATION_PATH, QUALITY_RULES_PATH
from test_dataset_cli_v2 import ARTIFACT_ONE, END_MS, log_lines, v2_args
from test_thermal_dataset_v2 import (
    COVERING_ROW_MS,
    NO_CHANGES,
    RUN_ALIAS,
    SOURCE_ALIAS,
    T0,
    history_db,
    spec_v2,
)
from test_training_entry import export_to, record_ticks, write_readings

POWER = "power.gpu.0"


@pytest.fixture(autouse=True)
def _restore_root_logger():
    import logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level


def power_at(offset_ms: int) -> float:
    """GPU の電力（試験用。時刻ごとに違う値で、どの点から引いたかが分かる）。"""
    return 100.0 + offset_ms / 100


def write_power(prod: Path, rules: QualityRules, offsets: range) -> None:
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        for offset in offsets:
            store.insert_sample(
                Sample(
                    ts_ms=T0 + offset,
                    readings=(Reading(metric=POWER, value=power_at(offset), quality=Quality.OK),),
                )
            )


def make_env(tmp_path: Path, rules: QualityRules, *, power_from_ms: int) -> dict[str, Path]:
    """本番の DB（readings・電力・trace）→ export → 再生（CLI）で専用 DB。"""
    prod = Path(history_db(tmp_path / "prod.db", COVERING_ROW_MS))
    write_readings(prod, rules, range(0, 7_001, 1_000))
    write_power(prod, rules, range(power_from_ms, 7_001, 500))
    record_ticks(prod, rules)
    replay = export_to(prod, rules, tmp_path / "csv")
    dedicated = tmp_path / "dedicated.db"
    assert (
        daemon_module.main(
            [
                "--source",
                "replay",
                "--csv",
                str(replay),
                "--bulk",
                "--dataset-run-alias",
                RUN_ALIAS,
                "--db",
                str(dedicated),
                "--quality-rules",
                str(QUALITY_RULES_PATH),
                "--calibration",
                str(CALIBRATION_PATH),
            ]
        )
        == 0
    )
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    out.chmod(0o700)
    declared = tmp_path / "declared.yaml"
    declared.write_text("schema_version: 1\nchanges: []\n", encoding="utf-8")
    return {"db": dedicated, "history": prod, "replay": replay, "out": out, "declared": declared}


@pytest.fixture
def env(tmp_path: Path, rules: QualityRules) -> dict[str, Path]:
    """電力は期間の先頭（T0）より 1 s 前から記録してある。"""
    return make_env(tmp_path, rules, power_from_ms=-1_000)


def power_args(env: dict[str, Path]) -> list[str]:
    return [*v2_args(env), "--feature-metric", POWER]


def load(env: dict[str, Path]) -> ThermalDatasetV2:
    directory = env["out"] / ARTIFACT_ONE
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    examples = [
        json.loads(line)
        for line in (directory / "examples.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    return ThermalDatasetV2.model_validate_json(
        json.dumps({"manifest": manifest, "examples": examples})
    )


def verify(env: dict[str, Path], dataset: ThermalDatasetV2, rules: QualityRules) -> None:
    verify_training_dataset_v2(
        dataset,
        production_db=env["history"],
        replay_path=env["replay"],
        declared_changes=NO_CHANGES,
        quality_rules=rules,
    )


def test_production_metrics_are_the_spec_metrics_without_a_csv_column():
    spec = spec_v2(
        feature_metrics=("air.room", "power.gpu.0", "power.cpu.package"),
        target_metrics=("air.gpu_exhaust", "gpu.0.core"),
    )
    assert production_metrics_of(spec) == ("gpu.0.core", "power.cpu.package", "power.gpu.0")
    assert production_metrics_of(spec_v2()) == ()


def test_internal_telemetry_comes_from_the_production_db_and_passes_the_training_entry(env, rules):
    """結合試験: 再生（CLI）→ coldaisle-dataset（CLI・電力を feature に）→ 学習の入口。"""
    with SqliteStore(env["db"], rules=rules, clock=SimulatedClock(0)) as store:
        assert store.series(POWER, 0, 2**62) == (), "試験の前提: 専用 DB に電力は無い"
    assert dataset_module.main(power_args(env)) == 0
    dataset = load(env)
    assert dataset.manifest.production_metrics == (POWER,)
    assert dataset.examples
    for example in dataset.examples:
        for frame in example.window:
            # as-of: その時刻以前の直近の点（500 ms ごと）。air.* と同じ規則で cell になる
            offset = frame.ts_ms - T0
            assert frame.values[POWER] == power_at(offset - offset % 500)
            assert frame.quality[POWER] is Quality.OK
            assert frame.values["air.room"] is not None
    verify(env, dataset, rules)
    with SqliteStore(env["db"], rules=rules, clock=SimulatedClock(0)) as store:
        assert store.series(POWER, 0, 2**62) == (), "専用 DB へは写さない（封印を変えない）"


def test_a_changed_production_point_refuses_the_training_entry(env, rules):
    assert dataset_module.main(power_args(env)) == 0
    dataset = load(env)
    with SqliteStore(env["history"], rules=rules, clock=SimulatedClock(10**13)) as store:
        store.connection.execute(
            "UPDATE readings SET value = value + 1 WHERE metric = ? AND ts_ms = ?",
            (POWER, T0 + 3_000),
        )
    with pytest.raises(TrainingDatasetRefused, match="一致しない"):
        verify(env, dataset, rules)


def test_a_period_without_raw_before_its_start_is_refused(tmp_path, rules, capsys):
    """電力の記録が期間の先頭から始まる（先頭より前が無い）なら作らない（0184 §2.3）。"""
    env = make_env(tmp_path, rules, power_from_ms=0)
    assert dataset_module.main(power_args(env)) == 1
    refused = [line for line in log_lines(capsys.readouterr().err) if line.get("level") == "error"]
    assert refused and "保持期間" in str(refused[0]["reason"])
    assert not (env["out"] / ARTIFACT_ONE).exists()
    with SqliteStore(env["db"], rules=rules, clock=SimulatedClock(0)) as store:
        assert store.control_traces_in_seq_order(0, 2**62) == (), "作らない期間では写さない"


def test_the_training_entry_refuses_once_the_raw_before_the_start_is_pruned(env, rules):
    assert dataset_module.main(power_args(env)) == 0
    dataset = load(env)
    with SqliteStore(env["history"], rules=rules, clock=SimulatedClock(10**13)) as store:
        store.connection.execute("DELETE FROM readings WHERE metric = ? AND ts_ms < ?", (POWER, T0))
    with pytest.raises(TrainingDatasetRefused, match="保持期間"):
        verify(env, dataset, rules)


def test_a_csv_only_spec_writes_an_empty_production_metrics(env, rules):
    assert dataset_module.main(v2_args(env)) == 0
    manifest = json.loads((env["out"] / ARTIFACT_ONE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["production_metrics"] == []
    verify(env, load(env), rules)


def test_read_training_production_reads_the_requested_series_in_the_run(env):
    inputs = read_replay_export_inputs(env["replay"])
    assert inputs.records is not None
    production = read_training_production(
        env["history"],
        inputs.records,
        start_ms=T0,
        end_ms=T0 + 2_000,
        trace_sink=lambda _trace: None,
        production_metrics=(POWER,),
    )
    assert production.production_series == {
        POWER: (
            SeriesPoint(ts_ms=T0, value=power_at(0), quality=Quality.OK),
            SeriesPoint(ts_ms=T0 + 500, value=power_at(500), quality=Quality.OK),
            SeriesPoint(ts_ms=T0 + 1_000, value=power_at(1_000), quality=Quality.OK),
            SeriesPoint(ts_ms=T0 + 1_500, value=power_at(1_500), quality=Quality.OK),
        )
    }
    with pytest.raises(ProductionTelemetryError, match="保持期間"):
        read_training_production(
            env["history"],
            inputs.records,
            start_ms=T0 - 1_000,
            end_ms=T0 + 2_000,
            trace_sink=lambda _trace: None,
            production_metrics=(POWER,),
        )
    with pytest.raises(ValueError, match="昇順"):
        read_training_production(
            env["history"],
            inputs.records,
            start_ms=T0,
            end_ms=T0 + 2_000,
            trace_sink=lambda _trace: None,
            production_metrics=("power.gpu.0", "power.cpu.package"),
        )


@pytest.mark.parametrize(
    "series",
    [
        pytest.param({}, id="missing"),
        pytest.param({POWER: (), "power.cpu.package": ()}, id="extra"),
        pytest.param(
            {
                POWER: (
                    SeriesPoint(ts_ms=T0 + 1_000, value=1.0, quality=Quality.OK),
                    SeriesPoint(ts_ms=T0, value=1.0, quality=Quality.OK),
                )
            },
            id="unsorted",
        ),
        pytest.param(
            {POWER: (SeriesPoint(ts_ms=T0 + END_MS, value=1.0, quality=Quality.OK),)},
            id="outside",
        ),
    ],
)
def test_the_builder_refuses_series_that_do_not_match_the_spec(env, rules, series):
    inputs = read_replay_export_inputs(env["replay"])
    assert inputs.records is not None
    with SqliteStore(env["db"], rules=rules, clock=SimulatedClock(0)) as store:
        with production_trace_sink(store) as sink:
            production = read_training_production(
                env["history"], inputs.records, start_ms=T0, end_ms=T0 + END_MS, trace_sink=sink
            )
        with pytest.raises(ValueError, match="本番の DB"):
            ThermalDatasetV2Builder(store).build(
                source_run=SourceRun(
                    run_id=RUN_ALIAS,
                    kind=DatasetSourceKind.REPLAY,
                    start_ms=T0,
                    end_ms=T0 + END_MS,
                    source_refs=(SOURCE_ALIAS,),
                    source_sha256=inputs.source_sha256,
                ),
                spec=spec_v2(feature_metrics=("air.room", POWER)),
                declared_changes=NO_CHANGES,
                calibration_history=production.history,
                export_binding=production.binding,
                production_series=series,
            )


@pytest.mark.parametrize(
    ("production_metrics", "match"),
    [
        pytest.param(["air.room"], "CSV に無い", id="csv-metric"),
        pytest.param(["power.gpu.0", "gpu.0.core"], "昇順", id="unsorted"),
    ],
)
def test_the_manifest_refuses_wrong_production_metrics(env, rules, production_metrics, match):
    assert dataset_module.main(power_args(env)) == 0
    manifest = json.loads((env["out"] / ARTIFACT_ONE / "manifest.json").read_text(encoding="utf-8"))
    manifest["spec"]["feature_metrics"] = ["air.room", "gpu.0.core", "power.gpu.0"]
    manifest["production_metrics"] = production_metrics
    with pytest.raises(ValidationError, match=match):
        DatasetManifestV2.model_validate_json(json.dumps(manifest))
