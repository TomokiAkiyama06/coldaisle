"""ヒント有りの solve の記録 ``hint_shadow_records`` と trace の突き合わせ（#288 / 0133）。

固定すること:

1. **追記専用。** UPDATE はトリガで拒む。``seq`` は追記順で、保持で消しても再利用しない
   （§2.5a）
2. **形は閉じている。** ``recorded`` だけが ``record_json`` を持ち、それ以外だけが
   ``reason`` を持つ。``note``・生の ``source``・``peer_uid`` は載せない
   （§2.5a / 0064 §2.9）。DB の CHECK でも拒む
3. **突き合わせの契約。** ``(ts_ms, tick_id) = (source_ts_ms, source_tick_id)`` で、かつ
   trace の**最上位の** ``run_id``（ControlTick v16）が等しいときだけ対応する。
   ``ts_ms`` だけ・``tick_id`` だけ・古い版の ``mode_command.run_id`` では対応させない。
   対応しない記録も捨てずに返す（§2.5a / §2.9）
4. **保持。** ``hint_shadow_days`` は ``config/retention.yaml`` にあり、
   ``control_trace_days`` と同じ値。``coldaisle-rollup`` が元の tick の時刻
   （``source_ts_ms``）で消す（§2.5a）
5. **fand は毎 tick 起動ごとの run_id を trace に保存する。** 管理ソケットを開かない構成でも載り、
   起動ごとに変わる（§2.9）
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control.schema import RUN_ID_PATTERN, ControlTick
from coldaisle.store import (
    HintShadowReason,
    HintShadowRecord,
    HintShadowStatus,
    SqliteStore,
)
from coldaisle.store.rollup import DAY_MS, RetentionRules
from coldaisle.store.rollup import run as run_rollup

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RETENTION_PATH = Path(__file__).resolve().parents[1] / "config" / "retention.yaml"

V16 = json.loads((FIXTURES / "control_tick_v16.json").read_text(encoding="utf-8"))
V15 = json.loads((FIXTURES / "control_tick_v15.json").read_text(encoding="utf-8"))
RUN_ID = V16["run_id"]
OTHER_RUN_ID = "1" * 32

PLAN_JSON = json.dumps(
    {
        "base": {"requested_demand": {"front": 0.4, "rear": 0.4, "top": 0.3}},
        "hint": {"requested_demand": {"front": 0.45, "rear": 0.4, "top": 0.3}},
        "recheck": {"monotonic": True, "in_certified_domain": True},
    }
)


@pytest.fixture
def store(tmp_path: Path, rules: Any) -> Any:
    with SqliteStore(tmp_path / "coldaisle.db", rules=rules, clock=SimulatedClock(0)) as opened:
        yield opened


def record(
    *,
    run_id: str = RUN_ID,
    tick_id: int = V16["tick_id"],
    ts_ms: int = V16["ts_ms"],
    status: HintShadowStatus = HintShadowStatus.RECORDED,
    reason: HintShadowReason | None = None,
    record_json: str | None = PLAN_JSON,
) -> HintShadowRecord:
    return HintShadowRecord(
        run_id=run_id,
        source_tick_id=tick_id,
        source_ts_ms=ts_ms,
        source_monotonic_ms=5_000,
        received_monotonic_ms=5_400,
        received_ts_ms=ts_ms + 400,
        schema_version=1,
        status=status,
        reason=reason,
        record_json=record_json,
    )


def store_trace(store: Any, body: dict[str, Any]) -> None:
    # 保存するのは検証を通る ControlTick の本文だけ（突き合わせの相手を本物の形にする）
    ControlTick.model_validate_json(json.dumps(body))
    assert store.record_control_trace(
        ts_ms=body["ts_ms"],
        tick_id=body["tick_id"],
        schema_version=body["schema_version"],
        trace_json=json.dumps(body),
    )


# ---------------------------------------------------------------- 1. 追記専用


def test_seq_follows_the_append_order_and_is_never_reused(store: Any) -> None:
    first = store.record_hint_shadow(record(ts_ms=1_000))
    second = store.record_hint_shadow(record(ts_ms=2_000))
    assert (first.seq, second.seq) == (1, 2)

    assert store.delete_hint_shadow_before(10_000) == 2
    # AUTOINCREMENT: 表が空になっても番号は巻き戻らない
    assert store.record_hint_shadow(record(ts_ms=20_000)).seq == 3


def test_a_record_with_a_seq_is_refused(store: Any) -> None:
    with pytest.raises(ValueError, match="採番前"):
        store.record_hint_shadow(record().model_copy(update={"seq": 7}))


def test_update_is_refused_by_the_trigger(store: Any) -> None:
    store.record_hint_shadow(record())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute("UPDATE hint_shadow_records SET status = 'rejected'")


# ---------------------------------------------------------------- 2. 形


def test_only_a_recorded_row_carries_the_plans() -> None:
    skipped = record(
        status=HintShadowStatus.SKIPPED,
        reason=HintShadowReason.BUDGET_EXHAUSTED,
        record_json=None,
    )
    assert skipped.reason is HintShadowReason.BUDGET_EXHAUSTED
    with pytest.raises(ValidationError, match="reason"):
        record(reason=HintShadowReason.STALE)
    with pytest.raises(ValidationError, match="record_json"):
        record(status=HintShadowStatus.REJECTED, reason=HintShadowReason.BASE_MISMATCH)
    with pytest.raises(ValidationError, match="reason"):
        record(status=HintShadowStatus.REJECTED, record_json=None)


def test_a_reason_outside_the_closed_vocabulary_is_refused() -> None:
    document = record(
        status=HintShadowStatus.SKIPPED, reason=HintShadowReason.SHADOW_BUSY, record_json=None
    ).model_dump(mode="json")
    document["reason"] = "something_else"
    with pytest.raises(ValidationError):
        HintShadowRecord.model_validate(document)


@pytest.mark.parametrize("key", ["note", "source", "peer_uid"])
def test_the_hint_note_source_and_uid_are_never_recorded(key: str) -> None:
    """ヒントの自由記述・生の出どころ・接続の uid は、どの深さにも載せない（0064 §2.9）。"""
    with pytest.raises(ValidationError, match="載せない鍵"):
        record(record_json=json.dumps({"hint": {"context": {key: "x"}}}))
    with pytest.raises(ValidationError, match="載せない鍵"):
        record(record_json=json.dumps({"items": [{key: 1}]}))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("run_id", "'0123'"),
        ("status", "'applied'"),
        ("reason", "'budget_exhausted'"),  # recorded の行は reason を持たない
        ("record_json", "'[1]'"),
        ("source_tick_id", "-1"),
    ],
)
def test_the_table_checks_the_shape_without_the_model(store: Any, column: str, value: str) -> None:
    """アプリの外からの書き込みでも、形の違う行を残さない。"""
    values = {
        "run_id": f"'{RUN_ID}'",
        "source_tick_id": "1",
        "source_ts_ms": "1",
        "source_monotonic_ms": "1",
        "received_monotonic_ms": "1",
        "received_ts_ms": "1",
        "schema_version": "1",
        "status": "'recorded'",
        "reason": "NULL",
        "record_json": "'{}'",
    } | {column: value}
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            f"INSERT INTO hint_shadow_records ({', '.join(values)}) "
            f"VALUES ({', '.join(values.values())})"
        )


# ---------------------------------------------------------------- 3. 突き合わせ


def test_a_record_matches_only_its_own_run(store: Any) -> None:
    store_trace(store, V16)
    store.record_hint_shadow(record())
    store.record_hint_shadow(record(run_id=OTHER_RUN_ID))

    matches = store.hint_shadow_matches(0, V16["ts_ms"] + 1)

    assert [(match.record.run_id, match.trace_matched) for match in matches] == [
        (RUN_ID, True),
        (OTHER_RUN_ID, False),
    ]


def test_ts_alone_or_tick_id_alone_does_not_match(store: Any) -> None:
    """再起動で tick_id が戻り、壁時計は飛ぶ。主キーの片方だけでは突き合わせない。"""
    store_trace(store, V16)
    store.record_hint_shadow(record(tick_id=V16["tick_id"] + 1))
    store.record_hint_shadow(record(ts_ms=V16["ts_ms"] + 1))

    matches = store.hint_shadow_matches(0, V16["ts_ms"] + 2)

    assert [match.trace_matched for match in matches] == [False, False]


def test_an_old_trace_is_not_matched_through_its_mode_command(store: Any) -> None:
    """v15 の ``mode_command.run_id`` を最上位の run_id として読み替えない（0133 §2.9）。"""
    assert V15["mode_command"]["run_id"] == RUN_ID
    store_trace(store, V15)
    store.record_hint_shadow(record(tick_id=V15["tick_id"], ts_ms=V15["ts_ms"]))

    (match,) = store.hint_shadow_matches(0, V15["ts_ms"] + 1)

    assert not match.trace_matched


def test_a_record_without_its_trace_is_kept_as_unmatched(store: Any) -> None:
    """trace の保存に失敗した・保持で消えた記録は捨てない（集計で「対応なし」と数える）。"""
    stored = store.record_hint_shadow(record())

    (match,) = store.hint_shadow_matches(0, V16["ts_ms"] + 1)

    assert match.record == stored
    assert not match.trace_matched


def test_the_period_is_on_the_source_tick_and_rows_come_in_seq_order(store: Any) -> None:
    store.record_hint_shadow(record(ts_ms=3_000))
    store.record_hint_shadow(record(ts_ms=1_000))
    store.record_hint_shadow(record(ts_ms=5_000))

    matches = store.hint_shadow_matches(1_000, 5_000)

    assert [match.record.source_ts_ms for match in matches] == [3_000, 1_000]
    assert [match.record.seq for match in matches] == [1, 2]
    with pytest.raises(ValueError, match="期間"):
        store.hint_shadow_matches(5_000, 1_000)


# ---------------------------------------------------------------- 4. 保持


def test_the_retention_is_configured_with_the_trace_retention() -> None:
    """突き合わせる trace より長く残しても join できない（0133 §2.5a）。"""
    rules = RetentionRules.from_yaml(RETENTION_PATH)
    assert rules.hint_shadow_days == rules.control_trace_days


def test_the_retention_has_no_default_in_code() -> None:
    with pytest.raises(ValidationError, match="hint_shadow_days"):
        RetentionRules(raw_days=30, control_trace_days=30, csv_dir="unused")  # type: ignore[call-arg]


def test_rollup_deletes_by_the_source_tick_time(store: Any) -> None:
    rules = RetentionRules.from_yaml(RETENTION_PATH)
    now_ms = 100 * DAY_MS
    cutoff = now_ms - rules.hint_shadow_retention_ms
    store.record_hint_shadow(record(ts_ms=cutoff - 1))
    # 受け取った時刻は新しくても、元の tick が古ければ消える
    old_source = record(ts_ms=cutoff - 1).model_copy(update={"received_ts_ms": now_ms})
    store.record_hint_shadow(old_source)
    kept = store.record_hint_shadow(record(ts_ms=cutoff))

    result = run_rollup(store, rules, now_ms=now_ms)

    assert result.deleted_hint_shadow == 2
    assert result.hint_shadow_cutoff_ms == cutoff
    assert [match.record for match in store.hint_shadow_matches(0, now_ms)] == [kept]


# ---------------------------------------------------------------- 5. fand の trace


def _start_and_tick(config: Any) -> ControlTick:
    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import build

    daemon = build(config, watchdog=NullWatchdog())
    try:
        tick: ControlTick = daemon.loop.tick().tick
    finally:
        daemon.close()
    return tick


def test_the_daemon_stores_its_run_id_on_every_tick_without_an_admin_entry(
    tmp_path: Path, rules: Any
) -> None:
    from test_authority_admin import daemon_config

    configs = []
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
        configs.append(daemon_config(tmp_path / name))
    assert all(config.admin_config is None for config in configs)  # `--no-admin` と同じ構成
    ticks = [_start_and_tick(config) for config in configs]
    for tick in ticks:
        assert tick.run_id is not None and re.fullmatch(RUN_ID_PATTERN, tick.run_id)
        assert tick.mode_command is not None and tick.mode_command.run_id is None
    # 起動ごとに1つ（決定記録 0077 §2.3）
    assert ticks[0].run_id != ticks[1].run_id

    with SqliteStore(configs[0].db, rules=rules, clock=SimulatedClock(0)) as opened:
        (stored,) = [json.loads(trace.trace_json) for trace in opened.control_traces(0, 2**62)]
        assert stored["schema_version"] == 16
        assert stored["run_id"] == ticks[0].run_id
        # 同じ run の記録だけが突き合う（tick_id と ts_ms が同じでも別の run は対応しない）
        opened.record_hint_shadow(
            record(run_id=ticks[0].run_id, tick_id=stored["tick_id"], ts_ms=stored["ts_ms"])
        )
        opened.record_hint_shadow(
            record(run_id=ticks[1].run_id, tick_id=stored["tick_id"], ts_ms=stored["ts_ms"])
        )
        matches = opened.hint_shadow_matches(0, 2**62)
    assert [(match.record.run_id, match.trace_matched) for match in matches] == [
        (ticks[0].run_id, True),
        (ticks[1].run_id, False),
    ]
