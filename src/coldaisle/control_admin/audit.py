"""監査書き込みスレッド（決定記録 0072 §2.7）。

**監査の表へ書くのはこのスレッド1本だけ。** 受付スレッドは依頼を FIFO の queue へ入れるだけで、
自分では DB を開かない。書き込みは依頼の順に1件ずつ行う（同じ指令の受付の行が結末の行より
先に書かれる）。queue は上限（`limits.audit_queue_max`）つきで、溢れた依頼は書き込みの失敗として
扱う。監査の DB が lock されていても待つのはこのスレッドだけで、受付スレッドも loop も止まらない。

接続（`SqliteStore`）は**このスレッドの中で**開く。SQLite の接続をスレッド間で共有しない
（0004 §2.8）。制御デーモンが自分の表を別の接続で書く前例は 0066。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from coldaisle import logs
from coldaisle.store import ControlAdminAuditRecord

LOGGER = logging.getLogger("coldaisle.control_admin")


class AuditSink(Protocol):
    """監査の行を書く先（`SqliteStore` が満たす）。"""

    def record_control_admin_audit(
        self, record: ControlAdminAuditRecord
    ) -> ControlAdminAuditRecord:
        """1行追記する。失敗は例外で返す。"""
        ...

    def close(self) -> None:
        """接続を閉じる。"""
        ...


@dataclass(frozen=True, slots=True)
class AuditCompletion:
    """完了を知らせてほしいと頼まれた書き込み（`ticket`）の結果。"""

    ticket: int
    ok: bool


@dataclass(frozen=True, slots=True)
class _Job:
    record: ControlAdminAuditRecord
    ticket: int | None


_STOP = object()
"""停止の合図。queue の中で依頼の後ろに並ぶので、先に入った依頼は書いてから止まる。"""


class AuditWriter:
    """FIFO の queue から1件ずつ監査の表へ書くスレッド。"""

    def __init__(
        self,
        *,
        open_sink: Callable[[], AuditSink],
        queue_max: int,
        on_done: Callable[[], None],
    ) -> None:
        self._open_sink = open_sink
        self._queue: queue.Queue[_Job | object] = queue.Queue(maxsize=queue_max)
        self._completions: queue.SimpleQueue[AuditCompletion] = queue.SimpleQueue()
        self._on_done = on_done
        self._thread = threading.Thread(target=self._run, name="control-admin-audit", daemon=True)
        # 受付スレッドだけが数える失敗（queue が溢れた・スレッドが死んでいた）と、
        # このスレッドだけが数える失敗（書けなかった）を別に持つ。同じ整数を2つのスレッドで
        # 書き換えない
        self._submit_failures = 0
        self._write_failures = 0

    @property
    def alive(self) -> bool:
        """書き込みスレッドが生きているか。"""
        return self._thread.is_alive()

    @property
    def failures(self) -> int:
        """監査の失敗数（`status` に出す。0072 §2.7）。"""
        return self._submit_failures + self._write_failures

    def start(self) -> None:
        """書き込みスレッドを起動する。"""
        self._thread.start()

    def submit(self, record: ControlAdminAuditRecord, *, ticket: int | None = None) -> bool:
        """書き込みを依頼する。**待たない。** 依頼できなければ False（失敗として数える）。

        ``ticket`` を渡した依頼だけ、書けたかどうかを `drain_completions()` で返す。
        """
        if not self.alive:
            self._submit_failures += 1
            return False
        try:
            self._queue.put_nowait(_Job(record=record, ticket=ticket))
        except queue.Full:
            self._submit_failures += 1
            return False
        return True

    def drain_completions(self) -> list[AuditCompletion]:
        """完了を知らせる依頼の結果を、書いた順に取り出す。"""
        drained: list[AuditCompletion] = []
        while True:
            try:
                drained.append(self._completions.get_nowait())
            except queue.Empty:
                return drained

    def stop(self, *, timeout_s: float) -> None:
        """先に入った依頼を書いてから止める。止まるまで最大 ``timeout_s`` 待つ。"""
        if not self._thread.is_alive():
            return
        try:
            self._queue.put(_STOP, timeout=timeout_s)
        except queue.Full:
            LOGGER.error("監査書き込みスレッドへ停止を伝えられなかった（queue が空かない）")
            return
        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():
            LOGGER.error("監査書き込みスレッドが時間内に止まらなかった")

    def _run(self) -> None:
        try:
            sink = self._open_sink()
        except Exception:
            LOGGER.exception("監査の表を開けないため、監査書き込みスレッドを止める")
            self._fail_remaining()
            return
        try:
            while True:
                job = self._queue.get()
                if job is _STOP:
                    return
                assert isinstance(job, _Job)
                self._write(sink, job)
        except BaseException:
            LOGGER.exception("監査書き込みスレッドが止まった")
            self._fail_remaining()
            raise
        finally:
            sink.close()

    def _write(self, sink: AuditSink, job: _Job) -> None:
        ok = True
        try:
            sink.record_control_admin_audit(job.record)
        except Exception:
            ok = False
            self._write_failures += 1
            LOGGER.exception(
                "監査の行を書けなかった",
                extra={
                    logs.FIELDS_KEY: {
                        "run_id": job.record.run_id,
                        "command_id": job.record.command_id,
                        "event": job.record.event,
                    }
                },
            )
        if job.ticket is not None:
            self._completions.put(AuditCompletion(ticket=job.ticket, ok=ok))
            self._on_done()

    def _fail_remaining(self) -> None:
        """残った依頼を失敗として返す。待っている受付スレッドを置き去りにしない。"""
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(job, _Job):
                self._write_failures += 1
                if job.ticket is not None:
                    self._completions.put(AuditCompletion(ticket=job.ticket, ok=False))
                    self._on_done()
