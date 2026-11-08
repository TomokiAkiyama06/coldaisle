"""`coldaisle-dataset` の v2 は本番の DB の trace を `seq` ごと写してから作る（0116。#83）。

- CLI（再生 → dataset）で作った dataset が、そのまま学習の入口（0112 §2.3）を通る
- 専用 DB に本番と同じ trace が既にあれば作り、違えば作らない
- 本番の trace が保持期間で消えた期間からは作らない
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coldaisle.daemon as daemon_module
import coldaisle.dataset as dataset_module
from coldaisle.clock import SimulatedClock
from coldaisle.control import ControlTraceLogger
from coldaisle.control.model.dataset import ThermalDatasetV2
from coldaisle.ingest.replay import read_replay_export_inputs
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.export_binding import read_training_production
from coldaisle.store.models import SequencedControlTrace
from coldaisle.training_entry import verify_training_dataset_v2
from conftest import CALIBRATION_PATH, QUALITY_RULES_PATH
from test_dataset_cli_v2 import ARTIFACT_ONE, log_lines, v2_args
from test_thermal_dataset_v2 import (
    ALIGNED,
    COVERING_ROW_MS,
    NO_CHANGES,
    RUN_ALIAS,
    T0,
    history_db,
    tick,
)
from test_training_entry import export_to, record_ticks, write_readings

ALL = 2**62


@pytest.fixture(autouse=True)
def _restore_root_logger():
    import logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level


@pytest.fixture
def env(tmp_path: Path, rules: QualityRules) -> dict[str, Path]:
    """本番の DB（較正の記録・readings・trace）→ export → 再生（CLI）で専用 DB（trace 無し）。"""
    prod = Path(history_db(tmp_path / "prod.db", COVERING_ROW_MS))
    write_readings(prod, rules, range(0, 7_001, 1_000))
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


def traces_of(path: Path, rules: QualityRules):
    with SqliteStore(path, rules=rules, clock=SimulatedClock(0)) as store:
        return store.control_traces_in_seq_order(0, ALL)


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


def test_a_dataset_made_by_the_cli_passes_the_training_entry(env, rules):
    """結合試験: 再生（CLI）→ coldaisle-dataset（CLI）→ 学習の入口。"""
    assert traces_of(env["db"], rules) == (), "試験の前提: 再生した専用 DB に trace は無い"
    assert dataset_module.main(v2_args(env)) == 0
    assert traces_of(env["db"], rules) == traces_of(env["history"], rules), "seq ごと写す"
    dataset = load(env)
    assert dataset.examples
    history = verify_training_dataset_v2(
        dataset,
        production_db=env["history"],
        replay_path=env["replay"],
        declared_changes=NO_CHANGES,
        quality_rules=rules,
    )
    assert history.rows


def test_identical_traces_already_in_the_dedicated_db_are_used(env, rules):
    with SqliteStore(env["db"], rules=rules, clock=SimulatedClock(0)) as store:
        store.copy_control_traces(traces_of(env["history"], rules))
    assert dataset_module.main(v2_args(env)) == 0
    verify_training_dataset_v2(
        load(env),
        production_db=env["history"],
        replay_path=env["replay"],
        declared_changes=NO_CHANGES,
        quality_rules=rules,
    )


def test_different_traces_in_the_dedicated_db_refuse_generation(env, rules, capsys):
    """同じ内容の tick でも、記録し直して seq が違えば作らない（上書きも混ぜることもしない）。"""
    with SqliteStore(env["db"], rules=rules, clock=SimulatedClock(0)) as store:
        logger = ControlTraceLogger(store)
        logger.record(tick(T0 - 10_000 + 1, 1, (0.1, 0.1, 0.1)))  # seq をずらす
        for ts_ms, tick_id, demands in ALIGNED:
            logger.record(tick(T0 + ts_ms, tick_id, demands))
    before = traces_of(env["db"], rules)
    assert dataset_module.main(v2_args(env)) == 1
    refused = [line for line in log_lines(capsys.readouterr().err) if line.get("level") == "error"]
    assert refused and "一致しない" in str(refused[0]["reason"])
    assert traces_of(env["db"], rules) == before, "専用 DB の trace を書き換えない"
    assert not (env["out"] / ARTIFACT_ONE).exists()


def test_a_period_whose_production_traces_were_pruned_is_refused(env, rules, capsys):
    with SqliteStore(env["history"], rules=rules, clock=SimulatedClock(10**13)) as store:
        store.delete_control_traces_before(T0 + 1)
    assert dataset_module.main(v2_args(env)) == 1
    refused = [line for line in log_lines(capsys.readouterr().err) if line.get("level") == "error"]
    assert refused and "保持期間" in str(refused[0]["reason"])
    assert traces_of(env["db"], rules) == (), "作らない期間では写さない"
    assert not (env["out"] / ARTIFACT_ONE).exists()


def test_a_period_with_legacy_production_traces_is_refused(env, rules):
    with SqliteStore(env["history"], rules=rules, clock=SimulatedClock(10**13)) as store:
        store.connection.execute("UPDATE control_trace_prune SET legacy_through_seq = 1")
    assert dataset_module.main(v2_args(env)) == 1
    assert traces_of(env["db"], rules) == ()


# ---------------------------------------------------------------- #416 trace を持たずに写す


def test_extra_traces_in_the_dedicated_db_refuse_generation(env, rules, capsys):
    """本番の trace に一致する行の後に専用 DB だけの行が余っても作らない（0116 §2.2 の 1）。"""
    production = traces_of(env["history"], rules)
    with SqliteStore(env["db"], rules=rules, clock=SimulatedClock(0)) as store:
        store.copy_control_traces(production)
        ControlTraceLogger(store).record(tick(T0 + 7_900, 99, (0.1, 0.1, 0.1)))
    before = traces_of(env["db"], rules)
    assert len(before) == len(production) + 1
    assert dataset_module.main(v2_args(env)) == 1
    refused = [line for line in log_lines(capsys.readouterr().err) if line.get("level") == "error"]
    assert refused and "一致しない" in str(refused[0]["reason"])
    assert traces_of(env["db"], rules) == before
    assert not (env["out"] / ARTIFACT_ONE).exists()


def test_read_training_production_streams_traces_in_seq_order(env, rules):
    """trace は持たずに記録した順で trace_sink へ渡し、件数と移行前の行の有無だけを残す。"""
    received: list[SequencedControlTrace] = []
    inputs = read_replay_export_inputs(env["replay"])
    assert inputs.records is not None
    production = read_training_production(
        env["history"], inputs.records, start_ms=0, end_ms=ALL, trace_sink=received.append
    )
    assert tuple(received) == traces_of(env["history"], rules)
    assert production.trace_count == len(received)
    assert production.has_legacy_traces is False
    assert not hasattr(production, "traces")


def test_streaming_copy_writes_in_one_transaction(tmp_path, rules):
    source = traces_of_store(tmp_path / "source.db", rules)
    with SqliteStore(tmp_path / "copy.db", rules=rules, clock=SimulatedClock(0)) as store:
        with store.control_trace_copy() as append:
            for trace in source:
                append(trace)
        assert store.control_traces_in_seq_order(0, ALL) == source


def test_streaming_copy_rolls_back_when_the_block_fails(tmp_path, rules):
    """ブロックが例外で抜けたら、途中まで写した trace を残さない（#416）。"""
    source = traces_of_store(tmp_path / "source.db", rules)
    with SqliteStore(tmp_path / "copy.db", rules=rules, clock=SimulatedClock(0)) as store:
        with pytest.raises(RuntimeError, match="拒否"), store.control_trace_copy() as append:
            append(source[0])
            raise RuntimeError("検査で拒否")
        assert store.control_traces_in_seq_order(0, ALL) == ()


def test_streaming_copy_refuses_out_of_order_seq_and_a_db_with_traces(tmp_path, rules):
    source = traces_of_store(tmp_path / "source.db", rules)
    with SqliteStore(tmp_path / "copy.db", rules=rules, clock=SimulatedClock(0)) as store:
        with pytest.raises(ValueError, match="seq の順"), store.control_trace_copy() as append:
            append(source[1])
            append(source[0])
        assert store.control_traces_in_seq_order(0, ALL) == ()
        store.copy_control_traces(source)
        with pytest.raises(ValueError, match="trace のある DB"), store.control_trace_copy():
            pass


def traces_of_store(path: Path, rules: QualityRules) -> tuple[SequencedControlTrace, ...]:
    """seq が 1 から並ぶ 3 件の trace を記録した DB を作り、その trace を返す。"""
    with SqliteStore(path, rules=rules, clock=SimulatedClock(10**13)) as store:
        logger = ControlTraceLogger(store)
        for index in range(3):
            logger.record(tick(T0 + 1_000 * index, 1 + index, (0.5, 0.5, 0.5)))
        traces = store.control_traces_in_seq_order(0, ALL)
    assert len(traces) == 3
    return traces
