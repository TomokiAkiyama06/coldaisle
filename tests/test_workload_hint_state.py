"""events の boot id / CLOCK_BOOTTIME と、Workload Hint の終端状態・run の記録・取り込みの境界。

#318 / 決定記録 0066 §2.2。実機なし（boot id と CLOCK_BOOTTIME は SimulatedClock と値で注入する）。

守りたいこと:

1. 移行前の events の行は NULL のまま残る（現在の boot id や既定値で埋めない）。
   片方だけの行は作れない
2. events は id の昇順（追記の順）で読める。ts_ms の順では並べない
3. run の順序は run_seq（追記順）で決まり、直前の run が clean でなければ境界を作る
4. run の開始と境界は1つのトランザクション。失敗したら何も書かない
5. 確かめられない終了記録（無い・状態が未知）は異常終了と同じ
6. 終端状態は event_id ごとに1行で、先に書いたものが残る（失効は一方向）
7. 4つの表は追記のみ。読めないときは空と扱わず例外にする
"""

from __future__ import annotations

import ast
import shutil
import sqlite3
from pathlib import Path

import pytest

from coldaisle.clock import SIMULATED_BOOT_ID, SimulatedClock
from coldaisle.store import EventRecord, QualityRules, SqliteStore
from coldaisle.store import migrations as store_migrations
from coldaisle.store.hint_state import (
    BootStamp,
    HintStateUnreadableError,
    HintStateWriteError,
    HintTerminalState,
    PreviousRun,
    PreviousRunState,
    RunEndRow,
    RunEndStatus,
    RunStartNotDurableError,
    classify_previous_run,
)
from conftest import TEST_EPOCH_MS

OTHER_BOOT_ID = "28ffabcd-0000-4000-8000-00000000beef"
"""別の boot を表す仮の値。"""

BOOT = BootStamp(boot_id=SIMULATED_BOOT_ID, boottime_ns=1_000)
RUN_A = "a" * 32
RUN_B = "b" * 32
RUN_C = "c" * 32


@pytest.fixture
def store(tmp_path: Path, rules: QualityRules):
    with SqliteStore(
        tmp_path / "hint.db", rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)
    ) as opened:
        yield opened


def add_event(store: SqliteStore, *, ts_ms: int = TEST_EPOCH_MS, boottime_ns: int = 0) -> int:
    stored = store.record_event(
        EventRecord(
            ts_ms=ts_ms,
            kind="workload_hint",
            payload_json='{"phase":"start"}',
            boot_id=SIMULATED_BOOT_ID,
            boottime_ns=boottime_ns,
        )
    )
    assert stored.id is not None
    return stored.id


# ---------------------------------------------------------------- events の列（0014）


def test_existing_event_rows_stay_null_after_the_migration(tmp_path, rules):
    """移行前の行を現在の boot id や既定値で埋めない（確かめられる行として蘇らせない）。"""
    old = tmp_path / "old-migrations"
    old.mkdir()
    for migration in store_migrations.discover():
        if migration.version <= 13:
            shutil.copy(migration.path, old / migration.path.name)
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        store_migrations.apply_pending(conn, now_ms=1_000, directory=old)
        conn.execute(
            "INSERT INTO events (ts_ms, kind, payload) VALUES (2000, 'workload_hint', '{\"v\":1}')"
        )
    finally:
        conn.close()

    with SqliteStore(path, rules=rules, clock=SimulatedClock(5_000)) as store:
        event = store.event(1)
        assert event is not None
        assert event.boot_id is None
        assert event.boottime_ns is None
        assert (
            store.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 15
        )


@pytest.mark.parametrize(
    ("boot_id", "boottime_ns"),
    [
        (SIMULATED_BOOT_ID, None),
        (None, 5),
        ("too-short", 5),
        (SIMULATED_BOOT_ID, -1),
    ],
)
def test_the_table_rejects_half_or_malformed_boot_clock_rows(store, boot_id, boottime_ns):
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO events (ts_ms, kind, payload, boot_id, boottime_ns) "
            "VALUES (1, 'workload_hint', '{}', ?, ?)",
            (boot_id, boottime_ns),
        )


@pytest.mark.parametrize(
    ("boot_id", "boottime_ns"),
    [(SIMULATED_BOOT_ID, None), (None, 5), ("X" * 36, 5), (SIMULATED_BOOT_ID, -1)],
)
def test_the_record_rejects_half_or_malformed_boot_clock(boot_id, boottime_ns):
    with pytest.raises(ValueError):
        EventRecord(
            ts_ms=1, kind="gpu_mode", payload_json="{}", boot_id=boot_id, boottime_ns=boottime_ns
        )


def test_boot_clock_round_trips(store):
    event_id = add_event(store, boottime_ns=123_456_789)
    event = store.event(event_id)
    assert event is not None
    assert (event.boot_id, event.boottime_ns) == (SIMULATED_BOOT_ID, 123_456_789)


def test_events_after_id_follows_append_order_not_wall_clock(store):
    """時計が戻った後に追記された行も、追記順に並ぶ（0066 §2.2）。"""
    first = add_event(store, ts_ms=TEST_EPOCH_MS + 10_000)
    second = add_event(store, ts_ms=TEST_EPOCH_MS)  # 壁時計が戻った後の行
    third = add_event(store, ts_ms=TEST_EPOCH_MS + 5_000)
    assert [e.id for e in store.events_after_id(0)] == [first, second, third]
    assert [e.id for e in store.events_after_id(first)] == [second, third]
    # limit を超えるときは古い側を残す（読み進めた位置で続きを取りこぼさない）
    assert [e.id for e in store.events_after_id(0, limit=2)] == [first, second]
    assert store.events_after_id(0, kinds=["gpu_mode"]) == ()
    assert store.events_after_id(0, kinds=[]) == ()


# ---------------------------------------------------------------- run の記録と境界


def test_the_first_run_cannot_vouch_for_earlier_hints(store):
    """run の記録が1件も無いときも、確かめられない状態として境界を作る。"""
    add_event(store)
    newest = add_event(store)
    tables = store.workload_hint_state()
    started = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    assert started.previous == PreviousRun(run_seq=None, state=PreviousRunState.NONE)
    assert started.boundary_event_id == newest
    assert tables.backfill_boundary() == newest


def test_a_clean_previous_run_adds_no_boundary(store):
    tables = store.workload_hint_state()
    first = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    tables.finish_run(
        run_seq=first.run_seq, status=RunEndStatus.CLEAN, ts_ms=TEST_EPOCH_MS, boot=BOOT
    )
    add_event(store)
    second = tables.start_run(run_id=RUN_B, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    assert second.previous == PreviousRun(run_seq=first.run_seq, state=PreviousRunState.CLEAN)
    assert second.boundary_event_id == 0, "止まっている間に届いたヒントは取り込める"
    assert second.run_seq > first.run_seq


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, PreviousRunState.ABNORMAL),
        (RunEndStatus.INCOMPLETE, PreviousRunState.INCOMPLETE),
    ],
)
def test_an_unclean_previous_run_moves_the_boundary(store, status, expected):
    tables = store.workload_hint_state()
    first = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    if status is not None:
        tables.finish_run(run_seq=first.run_seq, status=status, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    newest = add_event(store)
    second = tables.start_run(run_id=RUN_B, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    assert second.previous == PreviousRun(run_seq=first.run_seq, state=expected)
    assert second.boundary_event_id == newest


def test_the_boundary_survives_later_clean_runs(store):
    """異常終了の後の run が正常に終わっても、境界は消えない（戻さない）。"""
    tables = store.workload_hint_state()
    tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)  # 終了記録なし
    boundary = add_event(store)
    second = tables.start_run(run_id=RUN_B, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    tables.finish_run(
        run_seq=second.run_seq, status=RunEndStatus.CLEAN, ts_ms=TEST_EPOCH_MS, boot=BOOT
    )
    add_event(store)
    third = tables.start_run(run_id=RUN_C, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    assert third.previous.state is PreviousRunState.CLEAN
    assert third.boundary_event_id == boundary


def test_run_order_ignores_the_wall_clock(store):
    """開始時刻が戻っても、直前の run は run_seq の最大で決まる。"""
    tables = store.workload_hint_state()
    first = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS + 60_000, boot=BOOT)
    tables.finish_run(
        run_seq=first.run_seq, status=RunEndStatus.CLEAN, ts_ms=TEST_EPOCH_MS, boot=BOOT
    )
    second = tables.start_run(run_id=RUN_B, ts_ms=TEST_EPOCH_MS, boot=BOOT)  # 終了記録なし
    assert tables.previous_run() == PreviousRun(
        run_seq=second.run_seq, state=PreviousRunState.ABNORMAL
    )


def test_a_failed_run_start_writes_nothing(store):
    """開始記録だけが残って境界が欠ける、という状態を作らない。"""
    add_event(store)
    store.connection.execute(
        "CREATE TEMP TRIGGER fail_boundary BEFORE INSERT ON workload_hint_backfill_boundaries "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    tables = store.workload_hint_state()
    with pytest.raises(RunStartNotDurableError):
        tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    assert store.connection.execute("SELECT COUNT(*) FROM workload_hint_runs").fetchone()[0] == 0
    store.connection.execute("DROP TRIGGER temp.fail_boundary")
    # 次の起動で境界が改めて作られる
    retried = tables.start_run(run_id=RUN_B, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    assert retried.boundary_event_id == 1


def test_run_ids_are_unique_and_shaped(store):
    tables = store.workload_hint_state()
    tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    with pytest.raises(RunStartNotDurableError):
        tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    with pytest.raises(ValueError):
        tables.start_run(run_id="A" * 32, ts_ms=TEST_EPOCH_MS, boot=BOOT)


def test_a_run_ends_at_most_once(store):
    tables = store.workload_hint_state()
    started = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    tables.finish_run(
        run_seq=started.run_seq, status=RunEndStatus.CLEAN, ts_ms=TEST_EPOCH_MS, boot=BOOT
    )
    with pytest.raises(HintStateWriteError):
        tables.finish_run(
            run_seq=started.run_seq, status=RunEndStatus.INCOMPLETE, ts_ms=TEST_EPOCH_MS, boot=BOOT
        )
    with pytest.raises(HintStateWriteError):
        tables.finish_run(run_seq=999, status=RunEndStatus.CLEAN, ts_ms=TEST_EPOCH_MS, boot=BOOT)


@pytest.mark.parametrize(
    ("ends", "expected"),
    [
        ((), PreviousRunState.ABNORMAL),
        ((RunEndRow(7, "clean"), RunEndRow(7, "clean")), PreviousRunState.ABNORMAL),
        ((RunEndRow(6, "clean"),), PreviousRunState.ABNORMAL),
        ((RunEndRow(7, "finished"),), PreviousRunState.ABNORMAL),
        ((RunEndRow(7, "CLEAN"),), PreviousRunState.ABNORMAL),
        ((RunEndRow(7, "incomplete"),), PreviousRunState.INCOMPLETE),
        ((RunEndRow(7, "clean"),), PreviousRunState.CLEAN),
    ],
)
def test_unverifiable_lifecycles_are_not_clean(ends, expected):
    """無い・2件以上・別の run を指す・未知の状態は、どれも異常終了と同じ（0066 §2.2）。"""
    assert classify_previous_run(7, ends).state is expected


def test_no_start_record_means_none_even_with_orphan_ends():
    assert classify_previous_run(None, (RunEndRow(3, "clean"),)).state is PreviousRunState.NONE


# ---------------------------------------------------------------- 終端状態


def test_terminal_state_is_one_way_and_first_wins(store):
    event_id = add_event(store)
    tables = store.workload_hint_state()
    run = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    assert tables.terminal_state(event_id) is None
    first = tables.record_terminal_state(
        event_id=event_id,
        state=HintTerminalState.CONTRADICTED,
        run_seq=run.run_seq,
        ts_ms=TEST_EPOCH_MS,
        boot=BOOT,
    )
    again = tables.record_terminal_state(
        event_id=event_id,
        state=HintTerminalState.EXPIRED,
        run_seq=run.run_seq,
        ts_ms=TEST_EPOCH_MS + 1,
        boot=BootStamp(boot_id=OTHER_BOOT_ID, boottime_ns=9),
    )
    assert again == first
    assert tables.terminal_state(event_id) == first
    assert first.state is HintTerminalState.CONTRADICTED
    assert first.boot == BOOT


def test_terminal_states_cover_the_closed_set(store):
    tables = store.workload_hint_state()
    run = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    for state in HintTerminalState:
        event_id = add_event(store)
        stored = tables.record_terminal_state(
            event_id=event_id, state=state, run_seq=run.run_seq, ts_ms=TEST_EPOCH_MS, boot=BOOT
        )
        assert stored.state is state
    assert {s.value for s in HintTerminalState} == {
        "contradicted",
        "expired",
        "ended",
        "superseded",
        "unsupported_version",
    }


def test_terminal_states_must_refer_to_an_event_and_a_run(store):
    tables = store.workload_hint_state()
    run = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    with pytest.raises(HintStateWriteError):
        tables.record_terminal_state(
            event_id=999,
            state=HintTerminalState.ENDED,
            run_seq=run.run_seq,
            ts_ms=TEST_EPOCH_MS,
            boot=BOOT,
        )
    event_id = add_event(store)
    with pytest.raises(HintStateWriteError):
        tables.record_terminal_state(
            event_id=event_id,
            state=HintTerminalState.ENDED,
            run_seq=999,
            ts_ms=TEST_EPOCH_MS,
            boot=BOOT,
        )


# ---------------------------------------------------------------- 追記のみ・読めないとき


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE workload_hint_runs SET started_ts_ms = 0",
        "DELETE FROM workload_hint_runs",
        "UPDATE workload_hint_run_ends SET status = 'clean'",
        "DELETE FROM workload_hint_run_ends",
        "UPDATE workload_hint_backfill_boundaries SET through_event_id = 0",
        "DELETE FROM workload_hint_backfill_boundaries",
        "UPDATE workload_hint_terminal_states SET state = 'ended'",
        "DELETE FROM workload_hint_terminal_states",
    ],
)
def test_the_tables_are_append_only(store, statement):
    event_id = add_event(store)
    tables = store.workload_hint_state()
    run = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    tables.record_terminal_state(
        event_id=event_id,
        state=HintTerminalState.EXPIRED,
        run_seq=run.run_seq,
        ts_ms=TEST_EPOCH_MS,
        boot=BOOT,
    )
    tables.finish_run(
        run_seq=run.run_seq, status=RunEndStatus.INCOMPLETE, ts_ms=TEST_EPOCH_MS, boot=BOOT
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute(statement)


def test_the_boundary_only_increases_in_the_table(store):
    add_event(store)
    add_event(store)
    tables = store.workload_hint_state()
    run = tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)
    assert run.boundary_event_id == 2
    second = store.connection.execute(
        "INSERT INTO workload_hint_runs (run_id, started_ts_ms, boot_id, started_boottime_ns) "
        "VALUES (?, 0, ?, 0)",
        (RUN_B, SIMULATED_BOOT_ID),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="only increases"):
        store.connection.execute(
            "INSERT INTO workload_hint_backfill_boundaries "
            "(run_seq, previous_run_seq, previous_run_state, through_event_id) "
            "VALUES (?, ?, 'abnormal', 1)",
            (second, run.run_seq),
        )


def test_run_seq_is_never_reused(store):
    """AUTOINCREMENT: 最大の行が消えても番号は戻らない（表は消せないが、番号の規則を固定する）。"""
    sql = store.connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'workload_hint_runs'"
    ).fetchone()[0]
    assert "AUTOINCREMENT" in sql


@pytest.mark.parametrize(
    "table",
    [
        "workload_hint_runs",
        "workload_hint_run_ends",
        "workload_hint_backfill_boundaries",
        "workload_hint_terminal_states",
    ],
)
def test_unreadable_state_is_an_error_not_empty(tmp_path, rules, table):
    """表が無い（migration の不整合など）を「該当なし」と扱わない（hint_state_unreadable）。"""
    with SqliteStore(
        tmp_path / "broken.db", rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)
    ) as store:
        store.connection.execute("PRAGMA writable_schema = ON")
        store.connection.execute("DELETE FROM sqlite_master WHERE tbl_name = ?", (table,))
        store.connection.execute("PRAGMA writable_schema = OFF")
    with SqliteStore(
        tmp_path / "broken.db", rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)
    ) as store:
        tables = store.workload_hint_state()
        reads = {
            "workload_hint_runs": tables.previous_run,
            "workload_hint_run_ends": tables.previous_run,
            "workload_hint_backfill_boundaries": tables.backfill_boundary,
            "workload_hint_terminal_states": lambda: tables.terminal_state(1),
        }
        with pytest.raises(HintStateUnreadableError):
            reads[table]()
        if table != "workload_hint_terminal_states":
            # run の開始は直前の run と境界を読むので、どれかが読めなければ何も書かない
            with pytest.raises(RunStartNotDurableError):
                tables.start_run(run_id=RUN_A, ts_ms=TEST_EPOCH_MS, boot=BOOT)


def test_control_does_not_import_the_hint_state_tables():
    """`coldaisle.control` は DB を開かない（0064 §2.5 / 0066 §2.2）。書くのは合成の起点だけ。"""
    src = Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control"
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            offenders += [
                f"{path.name}: {name}"
                for name in names
                if name.startswith(("coldaisle.store.hint_state", "coldaisle.store.db"))
            ]
    assert offenders == []
