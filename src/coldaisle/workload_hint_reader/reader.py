"""Workload Hint の読み手のスレッド（#319 / 決定記録 0133 §2.4 / §2.8、0066 §2.2、0064 §2.6）。

**DB に触れるのはこのスレッドだけ**（`sqlite3` の接続はスレッドをまたいで使えないので、接続も
このスレッドの中で開く）。run の開始・起動時の取り込み・稼働中の増分取り込み・終端状態の永続化・
run の終了記録をすべてここで行い、loop へは不変の値（`HintFeed`）だけを渡す。

守ること:

- **行の順序は ``events.id``**（追記の順）。``ts_ms`` は順序にも読み進めた位置にも使わない
- **期限の起点は行の ``boottime_ns``**。経過は同じ boot の ``CLOCK_BOOTTIME`` の差だけで数える
- **run の開始記録が永続化できなければ、その run の間はヒントを扱わない**
  （``run_start_not_durable``）
- **照会に失敗したら空と扱わない**（``hint_state_unreadable``）。成功するまでヒントを使わせない
- **終端状態を1回でも書けなかった run は、終了記録を ``incomplete`` にする**
- ``events`` には書かない。``note``・生の ``source``・``peer_uid`` を loop へ渡さない（0064 §2.9）
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from coldaisle import logs
from coldaisle.clock import BootClock, BootClockUnavailableError, Clock, MonotonicClock
from coldaisle.control.config import WorkloadHintConfig
from coldaisle.control.schema import WorkloadHintKind, WorkloadHintStatus
from coldaisle.control.supervisor.hint_clock import (
    BackfillRejection,
    BootReading,
    HintAge,
    HintRowClock,
    evaluate_running_hint,
    evaluate_startup_backfill,
)
from coldaisle.control.supervisor.workload_hint import (
    HintFeed,
    HintFeedState,
    HintObservation,
    declared_ms,
    source_label,
)
from coldaisle.event_entry.messages import IMPLEMENTED_HINT_VERSIONS, WorkloadHintMessage
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.hint_state import (
    BootStamp,
    HintStateUnreadableError,
    HintStateWriteError,
    HintTerminalState,
    RunEndStatus,
    RunStart,
    RunStartNotDurableError,
    WorkloadHintStateTables,
)
from coldaisle.store.models import EventRecord
from coldaisle.workload_hint_reader.mailbox import HintMailbox

LOGGER = logging.getLogger("coldaisle.workload_hint_reader")

HINT_EVENT_KIND = "workload_hint"
"""``events.kind`` の値（0064 §2.4）。"""


@dataclass(frozen=True, slots=True)
class ParsedHint:
    """``events`` の1行を読んだ結果。``supported`` が False の行は中身を推測で読まない。"""

    event_id: int
    supported: bool
    phase: Literal["start", "end"] | None = None
    workload: WorkloadHintKind | None = None
    expected_duration_s: int | None = None
    source: str | None = None


def parse_hint_row(row: EventRecord) -> ParsedHint:
    """``events`` の ``workload_hint`` の行を読む（0064 §2.3）。

    知らない ``hint_v``・形の検証に落ちる行は ``supported=False``（「ヒント無し」として扱い、
    推測で解釈しない。fail closed）。入口（`coldaisle-eventd`）が検証した形をもう一度確かめる。
    """
    try:
        payload = json.loads(row.payload_json)
    except (json.JSONDecodeError, RecursionError):
        return ParsedHint(event_id=row.id or 0, supported=False)
    if not isinstance(payload, dict):
        return ParsedHint(event_id=row.id or 0, supported=False)
    hint_v = payload.get("hint_v")
    if type(hint_v) is not int or hint_v not in IMPLEMENTED_HINT_VERSIONS:
        return ParsedHint(event_id=row.id or 0, supported=False)
    try:
        message = WorkloadHintMessage.model_validate(payload)
    except ValidationError:
        return ParsedHint(event_id=row.id or 0, supported=False)
    return ParsedHint(
        event_id=row.id or 0,
        supported=True,
        phase=message.phase,
        workload=None if message.workload is None else WorkloadHintKind(message.workload),
        expected_duration_s=message.expected_duration_s,
        source=message.source,
    )


class WorkloadHintReader:
    """``events`` を読み、ヒントの状態を受け渡し口に置くスレッド。

    ``open_events`` は ``events`` を読む接続（呼び出し側が読み取り専用にする）、``open_state`` は
    自分の表に書く接続を開く。どちらもこのスレッドの中で呼ぶ（0066 §2.2 の「別の接続」）。
    """

    def __init__(
        self,
        config: WorkloadHintConfig,
        *,
        open_events: Callable[[], SqliteStore],
        open_state: Callable[[], SqliteStore],
        boot_clock: BootClock,
        clock: Clock,
        monotonic: MonotonicClock,
        run_id: str,
        mailbox: HintMailbox,
    ) -> None:
        if (
            config.reader_poll_ms is None
            or config.reader_stale_after_ms is None
            or config.max_age_ms is None
            or config.startup_backfill_ms is None
        ):
            raise ValueError("読み手は stage が off 以外の設定でだけ作る")
        self._config = config
        self._poll_s = config.reader_poll_ms / 1_000
        self._open_events = open_events
        self._open_state = open_state
        self._boot_clock = boot_clock
        self._clock = clock
        self._monotonic = monotonic
        self._run_id = run_id
        self._mailbox = mailbox
        self._stop = threading.Event()
        self._initialized = threading.Event()
        self._thread = threading.Thread(target=self._main, name="workload-hint-reader", daemon=True)
        mailbox.bind_thread(self._thread)
        # 以下はこのスレッドだけが触る
        self._events: SqliteStore | None = None
        self._state_store: SqliteStore | None = None
        self._state: WorkloadHintStateTables | None = None
        self._run: RunStart | None = None
        self._restored = False
        self._position = 0
        self._current: HintObservation | None = None
        self._incomplete = False

    # ------------------------------------------------------------ 合成の起点が使う面

    def start(self) -> None:
        """スレッドを起こす。**待たない。**

        run の開始と起動時の取り込みはスレッドの中で行う。fand の起動の経路（`READY=1` と
        最初の heartbeat の間）でヒントのために待つと、DB の busy timeout や大きな ``events`` の
        復元で systemd の watchdog を越えうる。ヒントは任意の機能なので、取り込みが終わるまでの
        tick は受け渡し口が空で ``hint_state_unreadable`` になるだけにする（0133 §2.8）。
        """
        self._thread.start()

    def wait_ready(self, timeout_s: float) -> bool:
        """最初の取り込み（または run を始められなかったこと）が終わるまで最大 ``timeout_s`` 待つ。

        試験と診断のためだけ。fand の起動の経路では呼ばない。
        """
        return self._initialized.wait(timeout=timeout_s)

    def stop(self, *, drain: bool = True) -> None:
        """止めて、run の終了記録を書かせる。

        待つのは最大 ``reader_stale_after_ms``（設定。新しい定数を置かない）。``drain=False`` は
        loop が例外で抜けた経路で、待たない（終了を遅らせない）。終了記録を書けなかった run は、
        次の起動で異常終了と扱われ、取り込みの境界ができる（安全側。0066 §2.2）。
        """
        self._stop.set()
        if not drain or not self._thread.is_alive():
            return
        assert self._config.reader_stale_after_ms is not None
        timeout_s = self._config.reader_stale_after_ms / 1_000
        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():
            LOGGER.error(
                "workload hint の読み手が止まらなかった（run の終了記録は書かれない）",
                extra={logs.FIELDS_KEY: {"timeout_s": timeout_s}},
            )

    # ------------------------------------------------------------ スレッド

    def _main(self) -> None:
        try:
            self.begin()
            while not self._stop.is_set():
                self.poll()
                self._initialized.set()
                self._stop.wait(self._poll_s)
            self.end()
        except Exception:
            # 落ちたら loop は reader_alive() で気づき、ヒントを使わない（MAX にしない）
            LOGGER.exception("workload hint の読み手が止まった")
            self.close()
        finally:
            self._initialized.set()

    # 以下の begin / poll / end は**同じ1つのスレッドから**呼ぶ（接続をスレッドにまたがせない）。
    # 本番はこのクラスのスレッドが呼び、試験はスレッドを起こさずに直接呼んで手順を固定する。

    def begin(self) -> None:
        """run の開始記録（と取り込みの境界）を1つのトランザクションで書く（0066 §2.2）。"""
        try:
            store = self._open_state()
            self._state_store = store
            self._state = store.workload_hint_state()
            boot = BootStamp(
                boot_id=self._boot_clock.boot_id(), boottime_ns=self._boot_clock.boottime_ns()
            )
            self._run = self._state.start_run(
                run_id=self._run_id, ts_ms=self._clock.now_ms(), boot=boot
            )
        except (RunStartNotDurableError, BootClockUnavailableError, sqlite3.Error, ValueError):
            LOGGER.exception(
                "workload hint の run の開始を永続化できなかった; この run ではヒントを扱わない",
                extra={logs.FIELDS_KEY: {"reason": "run_start_not_durable"}},
            )
            self._run = None
            self._publish(HintFeedState.RUN_START_NOT_DURABLE)
            return
        LOGGER.info(
            "workload hint の run を始めた",
            extra={
                logs.FIELDS_KEY: {
                    "run_seq": self._run.run_seq,
                    "previous_run_state": self._run.previous.state.value,
                    "backfill_boundary_event_id": self._run.boundary_event_id,
                }
            },
        )

    def poll(self) -> None:
        """1周期: loop が返した終端を書き、``events`` の新しい行を読み、値を置く。"""
        if self._run is None:
            return
        self._persist_reports()
        try:
            events = self._events_store()
            if not self._restored:
                self._restore(events)
            else:
                self._read_new_rows(events)
        except (sqlite3.Error, HintStateUnreadableError, BootClockUnavailableError):
            LOGGER.exception(
                "workload hint の状態を読めなかった; 読めるまでヒントを使わない",
                extra={logs.FIELDS_KEY: {"reason": "hint_state_unreadable"}},
            )
            self._drop_events_store()
            self._publish(HintFeedState.UNREADABLE)
            return
        self._publish(HintFeedState.OK)

    def _events_store(self) -> SqliteStore:
        if self._events is None:
            self._events = self._open_events()
        return self._events

    def _drop_events_store(self) -> None:
        # 壊れた接続を使い回さない（次の周期で開き直す）
        store, self._events = self._events, None
        if store is not None:
            try:
                store.close()
            except sqlite3.Error:
                LOGGER.exception("events を読む接続を閉じられなかった")

    def _rows_after(self, events: SqliteStore, after_id: int) -> Iterator[EventRecord]:
        """``after_id`` より後の ``workload_hint`` の行を ``events.id`` の昇順にすべて返す。"""
        position = after_id
        while True:
            page = events.events_after_id(position, kinds=[HINT_EVENT_KIND])
            if not page:
                return
            for row in page:
                assert row.id is not None
                position = row.id
                yield row

    # ------------------------------------------------------------ 起動時の取り込み

    def _restore(self, events: SqliteStore) -> None:
        """`events.id` の昇順に終了・置き換えを当てはめ、残った1件にだけ条件を当てる。

        0066 §2.2。
        """
        assert self._run is not None and self._state is not None
        open_row: EventRecord | None = None
        position = self._position
        for row in self._rows_after(events, 0):
            assert row.id is not None
            position = row.id
            parsed = parse_hint_row(row)
            # start と、知らない版の行は、それより前のヒントを置き換える
            open_row = None if parsed.supported and parsed.phase == "end" else row
        # 読み終えてから「いま」を採る。壁時計を先に、CLOCK_BOOTTIME を後に（BootReading の順序）
        startup_wall_ms = self._clock.now_ms()
        now = self._boot_now()
        self._current = (
            None
            if open_row is None
            else self._adopt_at_startup(open_row, startup_wall_ms=startup_wall_ms, now=now)
        )
        self._position = position
        self._restored = True

    def _adopt_at_startup(
        self, row: EventRecord, *, startup_wall_ms: int, now: BootReading
    ) -> HintObservation | None:
        assert self._run is not None and self._state is not None
        parsed = parse_hint_row(row)
        # **版を見る前に境界と終端済みで拒む。** 確かめられない run の前の行・既に終端の行は、
        # 版を知らない行でも取り込まない（書き直さず、trace にも出さない。0066 §2.2）
        if parsed.event_id <= self._run.boundary_event_id:
            self._log_rejected(
                parsed.event_id, BackfillRejection.UNCLEAN_PREVIOUS_RUN, at_startup=True
            )
            return None
        if not parsed.supported and self._state.terminal_state(parsed.event_id) is not None:
            self._log_rejected(
                parsed.event_id, BackfillRejection.ALREADY_TERMINATED, at_startup=True
            )
            return None
        if not parsed.supported:
            self._write_terminal(parsed.event_id, WorkloadHintStatus.UNSUPPORTED_VERSION)
            return HintObservation(
                event_id=parsed.event_id, status=WorkloadHintStatus.UNSUPPORTED_VERSION
            )
        declared = declared_ms(self._config, expected_duration_s=parsed.expected_duration_s)
        assert self._config.max_age_ms is not None
        assert self._config.startup_backfill_ms is not None
        terminated = self._state.terminal_state(parsed.event_id) is not None
        result = evaluate_startup_backfill(
            _row_clock(row),
            startup=now,
            startup_wall_ms=startup_wall_ms,
            declared_ms=declared,
            max_age_ms=self._config.max_age_ms,
            startup_backfill_ms=self._config.startup_backfill_ms,
            boundary_event_id=self._run.boundary_event_id,
            already_terminated=terminated,
        )
        if isinstance(result, HintAge):
            LOGGER.info(
                "起動時に workload hint を取り込んだ",
                extra={
                    logs.FIELDS_KEY: {
                        "event_id": parsed.event_id,
                        "age_ms": result.age_ms,
                        "remaining_ms": result.remaining_ms,
                    }
                },
            )
            return self._active(row, parsed, declared)
        self._log_rejected(parsed.event_id, result, at_startup=True)
        if result is BackfillRejection.EXPIRED:
            self._write_terminal(parsed.event_id, WorkloadHintStatus.EXPIRED)
        return None

    # ------------------------------------------------------------ 稼働中の増分取り込み

    def _read_new_rows(self, events: SqliteStore) -> None:
        for row in self._rows_after(events, self._position):
            assert row.id is not None
            self._apply(row)
            # 読み進めた位置は最後に処理した events.id（時刻ではない。0066 §2.2）
            self._position = row.id

    def _apply(self, row: EventRecord) -> None:
        parsed = parse_hint_row(row)
        if not parsed.supported:
            self._supersede_current()
            self._write_terminal(parsed.event_id, WorkloadHintStatus.UNSUPPORTED_VERSION)
            self._current = HintObservation(
                event_id=parsed.event_id, status=WorkloadHintStatus.UNSUPPORTED_VERSION
            )
            return
        if parsed.phase == "end":
            current = self._current
            if current is not None and current.status is WorkloadHintStatus.ACTIVE:
                self._write_terminal(current.event_id, WorkloadHintStatus.ENDED)
                self._current = replace(current, status=WorkloadHintStatus.ENDED)
            return
        self._supersede_current()
        declared = declared_ms(self._config, expected_duration_s=parsed.expected_duration_s)
        assert self._config.max_age_ms is not None
        result = evaluate_running_hint(
            _row_clock(row),
            now=self._boot_now(),
            declared_ms=declared,
            max_age_ms=self._config.max_age_ms,
        )
        if isinstance(result, HintAge):
            self._current = self._active(row, parsed, declared)
            return
        self._log_rejected(parsed.event_id, result, at_startup=False)
        if result is BackfillRejection.EXPIRED:
            self._write_terminal(parsed.event_id, WorkloadHintStatus.EXPIRED)
            self._current = replace(
                self._active(row, parsed, declared), status=WorkloadHintStatus.EXPIRED
            )
            return
        # 経過を確かめられない行は採らない（終端にもしない）
        self._current = None

    def _supersede_current(self) -> None:
        current = self._current
        if current is not None and current.status is WorkloadHintStatus.ACTIVE:
            self._write_terminal(current.event_id, WorkloadHintStatus.SUPERSEDED)
            self._current = replace(current, status=WorkloadHintStatus.SUPERSEDED)

    # ------------------------------------------------------------ 終端状態と run の終了

    def _persist_reports(self) -> None:
        """loop が決めた ``expired`` / ``contradicted`` を書く（loop は DB を待たない）。"""
        for transition in self._mailbox.drain():
            self._write_terminal(
                transition.event_id,
                transition.status,
                ts_ms=transition.ts_ms,
                boot=BootStamp(boot_id=transition.boot_id, boottime_ns=transition.boottime_ns),
            )
            current = self._current
            if (
                current is not None
                and current.event_id == transition.event_id
                and (current.status is WorkloadHintStatus.ACTIVE)
            ):
                self._current = replace(current, status=transition.status)

    def _write_terminal(
        self,
        event_id: int,
        status: WorkloadHintStatus,
        *,
        ts_ms: int | None = None,
        boot: BootStamp | None = None,
    ) -> None:
        """終端状態を1行書く。書けなければ run を ``incomplete`` にする（例外は出さない）。"""
        assert self._run is not None and self._state is not None
        try:
            stamp = (
                boot
                if boot is not None
                else BootStamp(
                    boot_id=self._boot_clock.boot_id(), boottime_ns=self._boot_clock.boottime_ns()
                )
            )
            self._state.record_terminal_state(
                event_id=event_id,
                state=HintTerminalState(status.value),
                run_seq=self._run.run_seq,
                ts_ms=self._clock.now_ms() if ts_ms is None else ts_ms,
                boot=stamp,
            )
        except (HintStateWriteError, BootClockUnavailableError, ValueError):
            self._incomplete = True
            LOGGER.exception(
                "workload hint の終端状態を書けなかった（この run の終了記録は incomplete）",
                extra={logs.FIELDS_KEY: {"event_id": event_id, "status": status.value}},
            )
            return
        LOGGER.info(
            "workload hint の終端状態を書いた",
            extra={logs.FIELDS_KEY: {"event_id": event_id, "status": status.value}},
        )

    def end(self) -> None:
        """loop が返した残りの終端を書き、run の終了記録を書いて接続を閉じる。"""
        try:
            if self._run is not None:
                self._persist_reports()
                self._finish()
        finally:
            self.close()

    def _finish(self) -> None:
        assert self._run is not None and self._state is not None
        status = RunEndStatus.INCOMPLETE if self._incomplete else RunEndStatus.CLEAN
        try:
            self._state.finish_run(
                run_seq=self._run.run_seq,
                status=status,
                ts_ms=self._clock.now_ms(),
                boot=BootStamp(
                    boot_id=self._boot_clock.boot_id(), boottime_ns=self._boot_clock.boottime_ns()
                ),
            )
        except (HintStateWriteError, BootClockUnavailableError, ValueError):
            # 書けなければ次の起動で異常終了と扱われ、取り込みの境界ができる（安全側）
            LOGGER.exception("workload hint の run の終了記録を書けなかった")
            return
        LOGGER.info(
            "workload hint の run を終えた",
            extra={logs.FIELDS_KEY: {"run_seq": self._run.run_seq, "status": status.value}},
        )

    def close(self) -> None:
        """接続を閉じる（run の終了記録は書かない）。"""
        self._drop_events_store()
        store, self._state_store = self._state_store, None
        if store is not None:
            try:
                store.close()
            except sqlite3.Error:
                LOGGER.exception("workload hint の状態の接続を閉じられなかった")

    # ------------------------------------------------------------ 補助

    def _boot_now(self) -> BootReading:
        return BootReading(
            boot_id=self._boot_clock.boot_id(), boottime_ns=self._boot_clock.boottime_ns()
        )

    def _active(self, row: EventRecord, parsed: ParsedHint, declared: int) -> HintObservation:
        assert parsed.workload is not None
        return HintObservation(
            event_id=parsed.event_id,
            status=WorkloadHintStatus.ACTIVE,
            workload=parsed.workload,
            source_label=source_label(self._config, parsed.source),
            declared_ms=declared,
            ts_ms=row.ts_ms,
            boot_id=row.boot_id,
            boottime_ns=row.boottime_ns,
        )

    def _publish(self, state: HintFeedState) -> None:
        self._mailbox.publish(
            HintFeed(
                state=state,
                read_monotonic_ms=self._monotonic.monotonic_ms(),
                current=self._current if state is HintFeedState.OK else None,
            )
        )

    @staticmethod
    def _log_rejected(event_id: int, reason: BackfillRejection, *, at_startup: bool) -> None:
        # ts_ms の値そのものは載せない（0066 §2.2 / 0064 §2.9）
        LOGGER.warning(
            "workload hint を取り込まなかった",
            extra={
                logs.FIELDS_KEY: {
                    "event_id": event_id,
                    "backfill_rejected": reason.value,
                    "at_startup": at_startup,
                }
            },
        )


def open_events_reader(db: Path, *, rules: QualityRules, clock: Clock) -> SqliteStore:
    """``events`` を読む接続を開き、**読み取り専用**にする（0064 §2.5 / 0066 §2.2）。

    ``PRAGMA query_only`` で、この接続からの書き込みを SQLite が拒む（書く接続は別に開く）。
    """
    store = SqliteStore(db, rules=rules, clock=clock)
    try:
        store.connection.execute("PRAGMA query_only = ON")
    except BaseException:
        store.close()
        raise
    return store


def _row_clock(row: EventRecord) -> HintRowClock:
    assert row.id is not None
    return HintRowClock(
        event_id=row.id, ts_ms=row.ts_ms, boot_id=row.boot_id, boottime_ns=row.boottime_ns
    )
