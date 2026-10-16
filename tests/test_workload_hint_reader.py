"""Workload Hint の読み手（#319 / 決定記録 0133 §2.8 / 0066 §2.2 / 0064 §2.6）。

実機なし。本物の SQLite（tmp）に `events` の行を書き、`SimulatedClock`（壁時計と
`CLOCK_BOOTTIME`）と
`ManualMonotonicClock` で読み手の手順（begin / poll / end）を同じスレッドから直接呼ぶ。

守りたいこと:

1. 起動時の取り込みは ``events.id`` の順に当てはめ、残った1件にだけ 0066 §2.2 の条件を当てる
2. 稼働中は ``events.id`` の順に読み進め、置き換え・取り消し・知らない版を終端として永続化する
3. loop が返した ``expired`` / ``contradicted`` を書く。書けなければ run は ``incomplete``
4. ``events`` には書かない（読み取り専用の接続）。run を始められなければヒントを扱わない
5. ``note``・生の ``source``・``peer_uid`` を loop へ渡さない
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from coldaisle.clock import ManualMonotonicClock, SimulatedClock
from coldaisle.control.config import WorkloadHintConfig
from coldaisle.control.schema import WorkloadHintKind, WorkloadHintStatus
from coldaisle.control.supervisor.workload_hint import (
    HintFeedState,
    HintObservation,
    HintTransition,
)
from coldaisle.event_entry.messages import HintWorkload, encode_workload_hint
from coldaisle.store import EventRecord, QualityRules, SqliteStore
from coldaisle.store.hint_state import PreviousRunState, RunEndStatus
from coldaisle.workload_hint_reader import (
    HintMailbox,
    WorkloadHintReader,
    open_events_reader,
    parse_hint_row,
)
from conftest import TEST_EPOCH_MS

RUN_A = "a" * 32
RUN_B = "b" * 32
OTHER_BOOT_ID = "28ffabcd-0000-4000-8000-00000000beef"
"""別の boot を表す仮の値。"""


def hint_config(**overrides: object) -> WorkloadHintConfig:
    document: dict[str, object] = {
        "stage": "observe",
        "grace_ms": 0,
        "default_ttl_ms": 600_000,
        "max_age_ms": 3_600_000,
        "contradiction_hold_ms": 60_000,
        "startup_backfill_ms": 300_000,
        "reader_poll_ms": 10,
        "reader_stale_after_ms": 2_000,
        "known_sources": ["job-launcher"],
        "context_overrides": [],
    }
    document.update(overrides)
    return WorkloadHintConfig.model_validate(document)


@dataclass
class Rig:
    db: Path
    rules: QualityRules
    clock: SimulatedClock
    monotonic: ManualMonotonicClock
    writer: SqliteStore
    config: WorkloadHintConfig

    def reader(self, run_id: str = RUN_A, mailbox: HintMailbox | None = None) -> WorkloadHintReader:
        return WorkloadHintReader(
            self.config,
            open_events=lambda: open_events_reader(self.db, rules=self.rules, clock=self.clock),
            open_state=lambda: SqliteStore(self.db, rules=self.rules, clock=self.clock),
            boot_clock=self.clock,
            clock=self.clock,
            monotonic=self.monotonic,
            run_id=run_id,
            mailbox=mailbox or HintMailbox(),
        )

    def advance(self, ms: int) -> None:
        self.clock.advance_to_ms(self.clock.now_ms() + ms)
        self.monotonic.advance_ms(ms)

    def hint(
        self,
        phase: str = "start",
        *,
        workload: str | None = "training",
        expected_duration_s: int | None = 120,
        source: str | None = "job-launcher",
        note: str | None = None,
        boot_id: str | None = None,
        boottime_ns: int | None = None,
        payload: str | None = None,
    ) -> int:
        if payload is None:
            line = encode_workload_hint(
                phase,
                workload=workload if phase == "start" else None,
                expected_duration_s=expected_duration_s if phase == "start" else None,
                source=source,
                note=note,
            )
            body = json.loads(line)
            payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
        stored = self.writer.record_event(
            EventRecord(
                ts_ms=self.clock.now_ms(),
                kind="workload_hint",
                payload_json=payload,
                peer_uid=1000,
                boot_id=boot_id or self.clock.boot_id(),
                boottime_ns=self.clock.boottime_ns() if boottime_ns is None else boottime_ns,
            )
        )
        assert stored.id is not None
        return stored.id

    def terminal(self, event_id: int) -> str | None:
        row = self.writer.connection.execute(
            "SELECT state FROM workload_hint_terminal_states WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def run_end(self, run_id: str) -> str | None:
        row = self.writer.connection.execute(
            "SELECT e.status FROM workload_hint_runs r "
            "LEFT JOIN workload_hint_run_ends e ON e.run_seq = r.run_seq WHERE r.run_id = ?",
            (run_id,),
        ).fetchone()
        return None if row is None else row[0]


@pytest.fixture
def rig(tmp_path: Path, rules: QualityRules):
    db = tmp_path / "hint.db"
    clock = SimulatedClock(TEST_EPOCH_MS)
    with SqliteStore(db, rules=rules, clock=clock) as writer:
        yield Rig(
            db=db,
            rules=rules,
            clock=clock,
            monotonic=ManualMonotonicClock(1_000),
            writer=writer,
            config=hint_config(),
        )


def current(mailbox: HintMailbox) -> HintObservation | None:
    feed = mailbox.latest()
    assert feed is not None
    assert feed.state is HintFeedState.OK
    return feed.current


def clean_previous_run(rig: Rig, run_id: str = "f" * 32) -> None:
    """境界を作らないよう、正常に終えた run を先に1つ置く。"""
    reader = rig.reader(run_id)
    reader.begin()
    reader.poll()
    reader.end()
    assert rig.run_end(run_id) == "clean"


# ---------------------------------------------------------------- 行の読み方


def test_workload_vocabulary_matches_the_entry():
    """入口の閉じた語彙と、trace / Supervisor の側の型が同じ値を持つ。"""
    assert set(HintWorkload.__args__) == {kind.value for kind in WorkloadHintKind}


def test_unknown_hint_version_is_not_guessed(rig):
    event_id = rig.hint(payload='{"hint_v":2,"phase":"start","type":"workload_hint","v":1}')
    row = rig.writer.event(event_id)
    assert row is not None
    assert parse_hint_row(row).supported is False


def test_free_text_does_not_reach_the_observation(rig):
    clean_previous_run(rig)
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.begin()
    reader.poll()
    rig.hint(source="secret-host-name", note="nightly run")
    reader.poll()
    observation = current(mailbox)
    assert observation is not None
    assert observation.source_label == "other"
    assert "nightly" not in repr(observation)
    assert "secret-host-name" not in repr(observation)


# ---------------------------------------------------------------- run の開始と起動時の取り込み


def test_first_run_makes_a_boundary_and_ignores_older_hints(rig):
    """run の記録が1件も無い初回は、それ以前のヒントを取り込まない（#318 の未決 1）。"""
    old = rig.hint()
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.begin()
    reader.poll()
    assert current(mailbox) is None
    assert rig.terminal(old) is None
    reader.end()


def test_startup_restores_the_last_open_hint_in_id_order(rig):
    clean_previous_run(rig)
    first = rig.hint(workload="benchmark")
    rig.advance(1_000)
    rig.hint("end")
    rig.advance(1_000)
    last = rig.hint(workload="training", expected_duration_s=600)
    rig.advance(10_000)
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.begin()
    reader.poll()
    observation = current(mailbox)
    assert observation is not None
    assert observation.event_id == last
    assert observation.status is WorkloadHintStatus.ACTIVE
    assert observation.workload is WorkloadHintKind.TRAINING
    assert observation.declared_ms == 600_000
    assert rig.terminal(first) is None  # 前の run が終えた行を書き直さない
    reader.end()


def test_startup_skips_a_hint_written_on_another_boot(rig):
    clean_previous_run(rig)
    rig.hint(boot_id=OTHER_BOOT_ID)
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.begin()
    reader.poll()
    assert current(mailbox) is None
    reader.end()


def test_startup_marks_an_expired_hint_terminal(rig):
    clean_previous_run(rig)
    event_id = rig.hint(expected_duration_s=60)
    rig.advance(61_000)
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.begin()
    reader.poll()
    assert current(mailbox) is None
    assert rig.terminal(event_id) == "expired"
    reader.end()


def test_unclean_previous_run_blocks_backfill(rig):
    clean_previous_run(rig)
    event_id = rig.hint()
    crashed = rig.reader("c" * 32)
    crashed.begin()
    crashed.poll()
    crashed.close()  # 終了記録を書かずに落ちた run
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.begin()
    assert reader._run is not None
    assert reader._run.previous.state is PreviousRunState.ABNORMAL
    reader.poll()
    assert current(mailbox) is None
    assert rig.terminal(event_id) is None
    reader.end()


# ---------------------------------------------------------------- 稼働中の増分取り込み


def started(rig: Rig) -> tuple[WorkloadHintReader, HintMailbox]:
    clean_previous_run(rig)
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.begin()
    reader.poll()
    return reader, mailbox


def test_new_start_supersedes_the_active_hint(rig):
    reader, mailbox = started(rig)
    first = rig.hint(workload="benchmark")
    reader.poll()
    second = rig.hint(workload="inference_service")
    reader.poll()
    observation = current(mailbox)
    assert observation is not None and observation.event_id == second
    assert rig.terminal(first) == "superseded"
    reader.end()
    assert rig.run_end(RUN_A) == "clean"


def test_end_ends_the_active_hint(rig):
    reader, mailbox = started(rig)
    event_id = rig.hint()
    reader.poll()
    rig.hint("end")
    reader.poll()
    observation = current(mailbox)
    assert observation is not None
    assert observation.event_id == event_id
    assert observation.status is WorkloadHintStatus.ENDED
    assert rig.terminal(event_id) == "ended"
    reader.end()


def test_rows_are_ordered_by_id_not_by_wall_clock(rig):
    """壁時計が戻った後の取り消しも追記順に当てはめる（0066 §2.2）。"""
    reader, mailbox = started(rig)
    rig.clock.advance_to_ms(rig.clock.now_ms() + 100_000)
    event_id = rig.hint()
    end_row = rig.writer.record_event(
        EventRecord(
            ts_ms=TEST_EPOCH_MS,  # 開始の行より古い壁時計
            kind="workload_hint",
            payload_json='{"hint_v":1,"phase":"end","type":"workload_hint","v":1}',
            boot_id=rig.clock.boot_id(),
            boottime_ns=rig.clock.boottime_ns(),
        )
    )
    assert end_row.id is not None and end_row.id > event_id
    reader.poll()
    observation = current(mailbox)
    assert observation is not None and observation.status is WorkloadHintStatus.ENDED
    reader.end()


def test_unknown_version_supersedes_and_is_terminal(rig):
    reader, mailbox = started(rig)
    first = rig.hint()
    reader.poll()
    unknown = rig.hint(payload='{"hint_v":9,"phase":"start","type":"workload_hint","v":1}')
    reader.poll()
    observation = current(mailbox)
    assert observation is not None
    assert observation.event_id == unknown
    assert observation.status is WorkloadHintStatus.UNSUPPORTED_VERSION
    assert observation.workload is None
    assert rig.terminal(first) == "superseded"
    assert rig.terminal(unknown) == "unsupported_version"
    reader.end()


def test_loop_transitions_are_persisted_with_their_boot_stamp(rig):
    reader, mailbox = started(rig)
    event_id = rig.hint()
    reader.poll()
    mailbox.report(
        HintTransition(
            event_id=event_id,
            status=WorkloadHintStatus.CONTRADICTED,
            ts_ms=rig.clock.now_ms(),
            boot_id=rig.clock.boot_id(),
            boottime_ns=12_345,
        )
    )
    reader.poll()
    assert rig.terminal(event_id) == "contradicted"
    observation = current(mailbox)
    assert observation is not None and observation.status is WorkloadHintStatus.CONTRADICTED
    stored = rig.writer.connection.execute(
        "SELECT boottime_ns FROM workload_hint_terminal_states WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert stored[0] == 12_345
    reader.end()


def test_failed_terminal_write_makes_the_run_incomplete(rig):
    reader, mailbox = started(rig)
    mailbox.report(
        HintTransition(
            event_id=999_999,  # events に無い（トリガが拒む）
            status=WorkloadHintStatus.EXPIRED,
            ts_ms=rig.clock.now_ms(),
            boot_id=rig.clock.boot_id(),
            boottime_ns=rig.clock.boottime_ns(),
        )
    )
    reader.poll()
    reader.end()
    assert rig.run_end(RUN_A) == RunEndStatus.INCOMPLETE.value


def test_events_connection_is_read_only(rig):
    store = open_events_reader(rig.db, rules=rig.rules, clock=rig.clock)
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.connection.execute(
                "INSERT INTO events (ts_ms, kind, payload) VALUES (1, 'workload_hint', '{}')"
            )
    finally:
        store.close()


def test_run_start_failure_means_no_hints_for_the_run(rig):
    def broken() -> SqliteStore:
        raise sqlite3.OperationalError("database is locked")

    mailbox = HintMailbox()
    reader = WorkloadHintReader(
        rig.config,
        open_events=lambda: open_events_reader(rig.db, rules=rig.rules, clock=rig.clock),
        open_state=broken,
        boot_clock=rig.clock,
        clock=rig.clock,
        monotonic=rig.monotonic,
        run_id=RUN_A,
        mailbox=mailbox,
    )
    reader.begin()
    reader.poll()
    feed = mailbox.latest()
    assert feed is not None and feed.state is HintFeedState.RUN_START_NOT_DURABLE
    reader.end()


def test_unreadable_events_are_reported_and_recovered(rig):
    calls = {"n": 0}

    def flaky() -> SqliteStore:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return open_events_reader(rig.db, rules=rig.rules, clock=rig.clock)

    clean_previous_run(rig)
    mailbox = HintMailbox()
    reader = WorkloadHintReader(
        rig.config,
        open_events=flaky,
        open_state=lambda: SqliteStore(rig.db, rules=rig.rules, clock=rig.clock),
        boot_clock=rig.clock,
        clock=rig.clock,
        monotonic=rig.monotonic,
        run_id=RUN_A,
        mailbox=mailbox,
    )
    reader.begin()
    reader.poll()
    feed = mailbox.latest()
    assert feed is not None and feed.state is HintFeedState.UNREADABLE
    reader.poll()
    feed = mailbox.latest()
    assert feed is not None and feed.state is HintFeedState.OK
    reader.end()


def test_thread_starts_restores_and_ends_the_run_cleanly(rig):
    """スレッドで動かしても、取り込みを終えてから start が返り、stop で終了記録を書く。"""
    clean_previous_run(rig)
    event_id = rig.hint()
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.start()
    assert reader.wait_ready(5.0)
    assert mailbox.reader_alive()
    observation = current(mailbox)
    assert observation is not None and observation.event_id == event_id
    reader.stop()
    assert not mailbox.reader_alive()
    assert rig.run_end(RUN_A) == "clean"


def test_unknown_version_inside_the_boundary_is_not_backfilled(rig):
    """版を知らない行も境界で拒む（書かない・出さない。Codex P2）。"""
    old = rig.hint(payload='{"hint_v":9,"phase":"start","type":"workload_hint","v":1}')
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.begin()  # 初回の run なので境界ができる
    reader.poll()
    assert current(mailbox) is None
    assert rig.terminal(old) is None
    reader.end()


def test_unknown_version_after_a_clean_run_is_shown_as_unsupported(rig):
    clean_previous_run(rig)
    unknown = rig.hint(payload='{"hint_v":9,"phase":"start","type":"workload_hint","v":1}')
    mailbox = HintMailbox()
    reader = rig.reader(RUN_A, mailbox)
    reader.begin()
    reader.poll()
    observation = current(mailbox)
    assert observation is not None
    assert observation.status is WorkloadHintStatus.UNSUPPORTED_VERSION
    assert rig.terminal(unknown) == "unsupported_version"
    reader.end()
