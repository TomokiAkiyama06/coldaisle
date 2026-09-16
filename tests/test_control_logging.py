"""Control Logging（#82）のdecision trace保存。"""

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.control import ControlTick, ControlTraceLogger
from coldaisle.store import ControlTraceRecord, SqliteStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "control_tick_v1.json"


def control_tick() -> ControlTick:
    """既存の互換fixtureを#82の保存入力にも使う。"""
    return ControlTick.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path, rules, clock):
    with SqliteStore(tmp_path / "control.db", rules=rules, clock=clock) as opened:
        yield opened


def test_trace_logger_serializes_a_complete_tick(store):
    tick = control_tick()

    assert ControlTraceLogger(store).record(tick) is True

    records = store.control_traces(tick.ts_ms, tick.ts_ms + 1)
    assert len(records) == 1
    record = records[0]
    assert (record.ts_ms, record.tick_id, record.schema_version) == (
        tick.ts_ms,
        tick.tick_id,
        tick.schema_version,
    )
    assert json.loads(record.trace_json) == json.loads(tick.model_dump_json())


def test_trace_is_append_only_and_idempotent(store):
    tick = control_tick()
    logger = ControlTraceLogger(store)

    assert logger.record(tick) is True
    assert logger.record(tick) is False
    assert (
        store.record_control_trace(
            ts_ms=tick.ts_ms,
            tick_id=tick.tick_id,
            schema_version=tick.schema_version,
            trace_json='{"changed": true}',
        )
        is False
    )
    assert json.loads(store.control_traces(tick.ts_ms, tick.ts_ms + 1)[0].trace_json) == json.loads(
        tick.model_dump_json()
    )


def test_trace_query_is_time_ordered_and_uses_a_half_open_interval(store):
    assert store.record_control_trace(
        ts_ms=20, tick_id=1, schema_version=1, trace_json='{"at": 20}'
    )
    assert store.record_control_trace(
        ts_ms=10, tick_id=2, schema_version=1, trace_json='{"at": 10, "tick": 2}'
    )
    assert store.record_control_trace(
        ts_ms=10, tick_id=1, schema_version=1, trace_json='{"at": 10, "tick": 1}'
    )

    assert [(item.ts_ms, item.tick_id) for item in store.control_traces(10, 20)] == [
        (10, 1),
        (10, 2),
    ]
    with pytest.raises(ValueError, match="期間"):
        store.control_traces(20, 10)


@pytest.mark.parametrize("trace_json", ["not json", "[]"])
def test_trace_rejects_invalid_or_non_object_json(store, trace_json):
    with pytest.raises(ValidationError, match="decision trace"):
        store.record_control_trace(ts_ms=0, tick_id=0, schema_version=1, trace_json=trace_json)


@pytest.mark.parametrize("trace_json", ["[]", '"text"', "0", "null"])
def test_database_also_rejects_non_object_trace_json(store, trace_json):
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO control_traces (ts_ms, tick_id, schema_version, trace_json) "
            "VALUES (?, ?, ?, ?)",
            (0, 0, 1, trace_json),
        )


def test_control_trace_record_is_immutable():
    record = ControlTraceRecord(ts_ms=0, tick_id=0, schema_version=1, trace_json="{}")
    with pytest.raises(ValidationError):
        record.tick_id = 1
