"""管理ソケットの受付スレッド（`control-admin`。決定記録 0072 §2.2 / §2.5 / §2.7）。

`coldaisle-fand` の中の**受付スレッド1本**が、I/O 多重化（`selectors`）で複数の接続を同時に持つ。
ある接続の受信・監査の書き込み待ち・適用の確認待ち・応答の送信の途中でも、新しい接続を受け付け、
次の指令を受け渡し口へ置ける。

接続は2つの段階に分けて別々に数える（0072 §2.2）。

- **受信中**（要求の1行を読み終えていない）: 上限 `limits.max_connections`。枠が埋まっていれば、
  新しい接続を閉じるのではなく**最も長く受信を続けている接続を閉じて**（`evicted`）新しい接続を受ける。
  `limits.read_timeout_s` でも必ず閉じる
- **受信後**（監査の書き込み待ち・適用の確認待ち・応答の送信中）:
  上限 `limits.max_pending_commands`。
  埋まっていれば**向きで分ける。** 冷却を弱めうる指令は `busy` で拒否し、安全側の指令（`MAX`・
  `lower_authority`・`rollback_authority`）は先に受け渡し口へ置いて `pending` を返す

置く順番も向きで分ける（0072 §2.7）。安全側の指令は採番したら**先に受け渡し口へ置き**、そのあとで
受付の行の書き込みを依頼する（失敗しても取り消さない）。弱めうる指令は受付の行を**書けたと分かってから**
置く（書けなければ置かずに拒否する）。

authority の降格（段階 2。#92）は authority の枠へ置く。**上げる操作は無い**（0072 §2.1）。

`accept()` が失敗し続けるとき（fd の枯渇など）は、待ち受けのソケットだけを selector から
外して `accept_backoff` の間隔で休む（失敗のたびに `multiplier` 倍、`max_ms` で頭打ち、成功で
戻す）。`sleep` はしないので、休んでいる間も接続済みの接続は読み続け、`MAX` は遅れずに受け渡し口へ
置く。途切れずに `escalate_after_ms` 失敗し続けたら受付スレッドを自分で終わらせ、loop の生存の
確認による既存の `MAX` の経路に任せる（決定記録 0076 §2.7。2つ目の `MAX` の経路は作らない）。

**受付スレッドは loop の状態に一切触れない。** 受け渡し口（`AdminMailbox`）へ置き、loop が返した
結果を読むだけである。受付スレッドの例外で `coldaisle-fand` を終わらせない。死んだら loop が
自分で `MAX` に倒す（`coldaisle.control.operating_mode`）。
"""

from __future__ import annotations

import contextlib
import errno
import logging
import math
import os
import selectors
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from coldaisle import logs
from coldaisle.clock import Clock, MonotonicClock
from coldaisle.control.operating_mode import (
    ADMIN_ACTOR_PREFIX,
    AdminAuthorityCommand,
    AdminModeCommand,
    ModeOutcomeKind,
)
from coldaisle.control.schema import (
    BASELINE_STAGE,
    AuthorityStage,
    Demand,
    OperatingMode,
    PerZone,
)
from coldaisle.control_admin.audit import AuditWriter
from coldaisle.control_admin.config import ControlAdminSettings
from coldaisle.control_admin.mailbox import AdminMailbox, Placed, Sealed, Superseded
from coldaisle.control_admin.messages import (
    AuthorityRequest,
    LowerAuthorityRequest,
    RequestError,
    SetModeRequest,
    StatusRequest,
    encode,
    parse_request,
)
from coldaisle.local_socket import (
    Authorizer,
    SocketStartupError,
    acquire_lock,
    identity,
    peer_credentials_supported,
    peer_uid,
    prepare_parent,
    prepare_path,
    resolve_group,
    umask,
    unlink_if_same,
)
from coldaisle.store import ControlAdminAuditRecord

LOGGER = logging.getLogger("coldaisle.control_admin")

SERVICE = "coldaisle-fand の管理ソケット"
"""起動時の検査のメッセージに出す入口の名前。"""

LISTEN_BACKLOG = 16
"""`listen()` の backlog。受信中の枠（`max_connections`）とは別の、カーネルの待ち行列。"""

RECV_CHUNK_BYTES = 4096

Stage = Literal["receiving", "auditing", "awaiting_ack", "sending"]


@dataclass(eq=False)
class _Connection:
    sock: socket.socket
    uid: int
    opened_mono_ms: int
    deadline_mono_ms: int | None
    """次の締め切り。監査の書き込み待ちは None（書き込みスレッドが必ず結果を返す）。"""
    stage: Stage = "receiving"
    buffer: bytearray = field(default_factory=bytearray)
    op: str | None = None
    command_id: int | None = None
    outgoing: bytes = b""


class ControlAdminServer:
    """管理ソケットで待ち受け、検証済みの指令を受け渡し口へ置く。"""

    def __init__(
        self,
        *,
        settings: ControlAdminSettings,
        mailbox: AdminMailbox,
        audit: AuditWriter,
        clock: Clock,
        monotonic: MonotonicClock,
        run_id: str,
        authority_ceiling: AuthorityStage,
        server_uid: int | None = None,
    ) -> None:
        if not peer_credentials_supported():
            # 0072 §2.5: SO_PEERCRED が無いプラットフォームでは管理ソケットを開かない
            raise SocketStartupError("SO_PEERCRED が無いプラットフォームでは管理ソケットを開かない")
        self._settings = settings
        self._mailbox = mailbox
        self._audit = audit
        self._clock = clock
        self._monotonic = monotonic
        self._run_id = run_id
        self._authority_ceiling = authority_ceiling
        self._authorizer = Authorizer(
            server_uid=os.geteuid() if server_uid is None else server_uid,
            allow_same_user=settings.authorization.allow_same_user,
            group_gid=resolve_group(settings.socket.group),
        )
        self._path = settings.socket.path
        self._listener: socket.socket | None = None
        self._bound: tuple[int, int] | None = None
        self._lock_fd: int | None = None
        self._selector: selectors.BaseSelector | None = None
        self._thread = threading.Thread(
            target=self._run, name="control-admin-receiver", daemon=True
        )
        self._stop = False
        self._next_command_id = 0
        self._connections: set[_Connection] = set()
        self._auditing: dict[int, tuple[_Connection, AdminModeCommand]] = {}
        self._awaiting_ack: dict[int, _Connection] = {}
        self._accept_backoff_ms: int | None = None
        """いまの休みの長さ。None は失敗が続いていない（成功で戻す）。"""
        self._accept_resume_mono_ms: int | None = None
        """待ち受けを selector へ戻す時刻。None は待ち受けを監視している。"""
        self._accept_failures = 0
        """最後の成功から数えた `accept()` の失敗の回数。"""
        self._accept_failures_logged = 0
        """そのうちログに出した時点の回数（出さなかった分を次のログでまとめて数える）。"""
        self._accept_first_failure_mono_ms: int | None = None
        """途切れずに続いている失敗の最初の時刻。成功で None に戻す。"""
        self._accept_exhausted = False
        """失敗が `escalate_after_ms` 続いた。受付スレッドを終わらせる（→ loop が `MAX`）。"""

    @property
    def path(self) -> Path:
        """待ち受けているソケットの場所。"""
        return self._path

    @property
    def thread(self) -> threading.Thread:
        """受付スレッド（受け渡し口が生存を答える対象）。"""
        return self._thread

    # ---------------------------------------------------------------- 起動と停止

    def bind(self) -> None:
        """ソケットを作り、権限を設定して待ち受けを始める（0045 §2.2 と同じ検査）。"""
        prepare_parent(self._path.parent, self._authorizer.group_gid, service=SERVICE)
        lock_fd = acquire_lock(self._path, service=SERVICE)
        try:
            self._bind_locked()
        except BaseException:
            os.close(lock_fd)
            raise
        self._lock_fd = lock_fd

    def _bind_locked(self) -> None:
        prepare_path(self._path, service=SERVICE)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound: tuple[int, int] | None = None
        try:
            # 作った瞬間から所有者以外に開かない。権限は直後に設定値へ広げる
            with umask(0o177):
                listener.bind(str(self._path))
            bound = identity(os.lstat(self._path))
            if self._authorizer.group_gid is not None:
                os.chown(self._path, -1, self._authorizer.group_gid)
            os.chmod(self._path, self._settings.socket.mode_bits)
            listener.listen(LISTEN_BACKLOG)
            listener.setblocking(False)
        except BaseException:
            listener.close()
            unlink_if_same(self._path, bound)
            raise
        self._listener = listener
        self._bound = bound
        LOGGER.info(
            "管理ソケットで待ち受ける",
            extra={
                logs.FIELDS_KEY: {
                    "socket": str(self._path),
                    "mode": self._settings.socket.mode,
                    "group": self._settings.socket.group,
                    "run_id": self._run_id,
                }
            },
        )

    def start(self) -> None:
        """受付スレッドを起動する。受け渡し口に生存を答える対象として渡してから起動する。"""
        if self._listener is None:
            raise RuntimeError("bind() の前に start() を呼んだ")
        self._mailbox.attach_receiver(self._thread)
        self._thread.start()

    def request_stop(self) -> None:
        """受付スレッドを止める（停止の手順。死んだとは扱わない）。"""
        self._stop = True
        self._mailbox.wake()

    def join(self, *, timeout_s: float) -> None:
        """受付スレッドの終了を待つ。"""
        if self._thread.is_alive():
            self._thread.join(timeout=timeout_s)

    def close(self) -> None:
        """待ち受けを閉じ、**自分が作ったソケットだけ**を消す。"""
        for conn in list(self._connections):
            self._drop(conn)
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        unlink_if_same(self._path, self._bound)
        self._bound = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    # ---------------------------------------------------------------- 受付スレッド

    def _run(self) -> None:
        try:
            self._serve()
        except BaseException:
            # 受付スレッドの例外で coldaisle-fand を終わらせない。loop が生存の確認で気づき、
            # 再起動まで MAX に倒す（0072 §2.2）
            LOGGER.exception(
                "管理ソケットの受付スレッドが止まった",
                extra={logs.FIELDS_KEY: {"reason": "admin_receiver_dead"}},
            )
        finally:
            if self._selector is not None:
                self._selector.close()
                self._selector = None

    def _serve(self) -> None:
        listener = self._listener
        assert listener is not None
        selector = selectors.DefaultSelector()
        self._selector = selector
        selector.register(listener, selectors.EVENT_READ, "accept")
        selector.register(self._mailbox.wake_reader, selectors.EVENT_READ, "wake")
        while not self._stop and not self._accept_exhausted:
            for key, _events in selector.select(self._select_timeout_s()):
                if key.data == "accept":
                    self._accept(listener)
                elif key.data == "wake":
                    self._drain_wake()
                elif isinstance(key.data, _Connection):
                    self._on_ready(key.data)
            self._on_audit_completions()
            self._on_loop_outcomes()
            self._expire()
            self._resume_accept_if_due()
        if self._accept_exhausted:
            self._close_after_exhaustion()

    def _select_timeout_s(self) -> float | None:
        """次の締め切りまでの秒数。締め切りの無い間は起こされるまで待つ。"""
        deadlines = [
            conn.deadline_mono_ms for conn in self._connections if conn.deadline_mono_ms is not None
        ]
        if self._accept_resume_mono_ms is not None:
            deadlines.append(self._accept_resume_mono_ms)
        if not deadlines:
            return None
        return max(0.0, (min(deadlines) - self._monotonic.monotonic_ms()) / 1_000)

    def _drain_wake(self) -> None:
        try:
            while self._mailbox.wake_reader.recv(RECV_CHUNK_BYTES):
                pass
        except (BlockingIOError, InterruptedError):
            return

    # ---------------------------------------------------------------- 受け付けと受信

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                sock, _ = listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                # EMFILE / ENFILE / ECONNABORTED などは一時的で回復しうる。まずは受付を死なせず、
                # 待ち受けだけを少し休む（待ち受けは読める状態のままなので、休まないと空回りする）。
                # 途切れずに escalate_after_ms 続いたら受付スレッドを終わらせる
                if not self._escalate_if_exhausted(exc):
                    self._pause_accept(exc)
                return
            self._on_accept_succeeded()
            sock.setblocking(False)
            try:
                uid = peer_uid(sock)
            except OSError:
                LOGGER.warning("接続相手の uid を取れないため拒否した", exc_info=True)
                self._reply_and_close(sock, {"ok": False, "error": "unauthorized"})
                continue
            if not self._authorizer.allows(uid):
                # 認可されなかった接続はすぐ閉じ、受信中の枠に数えず、追い出しの理由にもしない
                self._log_connection(uid, None, "rejected", "unauthorized")
                self._reply_and_close(sock, {"ok": False, "error": "unauthorized"})
                continue
            receiving = [conn for conn in self._connections if conn.stage == "receiving"]
            if len(receiving) >= self._settings.limits.max_connections:
                oldest = min(receiving, key=lambda conn: conn.opened_mono_ms)
                self._log_connection(oldest.uid, None, "closed", "evicted")
                self._drop(oldest)
            now_ms = self._monotonic.monotonic_ms()
            conn = _Connection(
                sock=sock,
                uid=uid,
                opened_mono_ms=now_ms,
                deadline_mono_ms=now_ms + self._read_timeout_ms(),
            )
            self._connections.add(conn)
            self._register(conn, selectors.EVENT_READ)

    def _pause_accept(self, exc: OSError) -> None:
        """待ち受けを selector から外し、次の休みの長さを決める。**`sleep` はしない。**

        接続済みの接続は selector に残るので、休んでいる間も読み続ける。ログは最初の失敗と、
        休みが伸びたとき・頭打ちの後は失敗の回数が 2 の冪に届いたときだけ出す（出さなかった
        回数は次のログにまとめる）。
        """
        backoff = self._settings.accept_backoff
        previous = self._accept_backoff_ms
        current = backoff.initial_ms if previous is None else self._grow(previous)
        self._accept_backoff_ms = current
        self._accept_failures += 1
        self._accept_resume_mono_ms = self._monotonic.monotonic_ms() + current
        self._unwatch_listener()
        failures = self._accept_failures
        if current != previous or failures & (failures - 1) == 0:
            LOGGER.warning(
                "接続の受け付けに失敗したため待ち受けを休む",
                exc_info=failures == 1,
                extra={
                    logs.FIELDS_KEY: {
                        "reason": "admin_accept_failed",
                        "errno": None if exc.errno is None else errno.errorcode.get(exc.errno),
                        "consecutive_failures": failures,
                        "suppressed_since_last_log": failures - self._accept_failures_logged - 1,
                        "backoff_ms": current,
                        "run_id": self._run_id,
                    }
                },
            )
            self._accept_failures_logged = failures

    def _escalate_if_exhausted(self, exc: OSError) -> bool:
        """途切れずに続く失敗が `escalate_after_ms` に届いたら、受付スレッドを終わらせる印を立てる。

        終わった受付スレッドは loop が毎 tick の生存の確認で見つけ、`MANUAL` を解除して
        再起動まで `MAX`（`forced_max`）にし、decision trace に `admin_receiver_dead` を残す
        （0072 §2.2 の経路をそのまま使う。0076 §2.7）。
        """
        now_ms = self._monotonic.monotonic_ms()
        if self._accept_first_failure_mono_ms is None:
            self._accept_first_failure_mono_ms = now_ms
        failing_for_ms = now_ms - self._accept_first_failure_mono_ms
        escalate_after_ms = self._settings.accept_backoff.escalate_after_ms
        if failing_for_ms < escalate_after_ms:
            return False
        self._accept_failures += 1
        self._accept_exhausted = True
        self._accept_resume_mono_ms = None
        self._unwatch_listener()
        LOGGER.error(
            "接続の受け付けが失敗し続けたため受付スレッドを終わらせる（再起動まで MAX）",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                logs.FIELDS_KEY: {
                    "reason": "admin_accept_exhausted",
                    "errno": None if exc.errno is None else errno.errorcode.get(exc.errno),
                    "consecutive_failures": self._accept_failures,
                    "failing_for_ms": failing_for_ms,
                    "escalate_after_ms": escalate_after_ms,
                    "run_id": self._run_id,
                }
            },
        )
        return True

    def _close_after_exhaustion(self) -> None:
        """受付スレッドを終わらせる前に、接続と待ち受けを閉じる。

        ソケットのファイルとロックは残し、停止の手順の `close()` が消す（自分が作ったものだけを
        消す規則を1か所に保つ）。監査書き込みスレッドは受付スレッドの死と同じく触らない。
        """
        for conn in list(self._connections):
            self._drop(conn)
        self._auditing.clear()
        self._awaiting_ack.clear()
        listener = self._listener
        self._listener = None
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.close()

    def _grow(self, previous: int) -> int:
        """次の休みの長さ。`max_ms` で頭打ちにし、桁あふれを受付の外へ出さない。

        倍率は起動時に有限・上限付きで検証するが、ここでも float の積が inf / nan になる経路で
        `math.ceil` の OverflowError / ValueError を起こさない（受付スレッドを落とさない）。
        """
        backoff = self._settings.accept_backoff
        grown = previous * backoff.multiplier
        if not math.isfinite(grown) or grown >= backoff.max_ms:
            return backoff.max_ms
        return min(math.ceil(grown), backoff.max_ms)

    def _on_accept_succeeded(self) -> None:
        """失敗が続いた後の成功で休みの長さと時間の起点を戻し、回復を1行だけ残す。"""
        self._accept_first_failure_mono_ms = None
        if self._accept_failures == 0:
            return
        LOGGER.info(
            "接続の受け付けが回復した",
            extra={
                logs.FIELDS_KEY: {
                    "reason": "admin_accept_recovered",
                    "consecutive_failures": self._accept_failures,
                    "run_id": self._run_id,
                }
            },
        )
        self._accept_backoff_ms = None
        self._accept_failures = 0
        self._accept_failures_logged = 0

    def _resume_accept_if_due(self) -> None:
        """休みの期限（単調時計）が来たら待ち受けを selector へ戻す。休みの長さは成功まで保つ。"""
        resume = self._accept_resume_mono_ms
        if resume is None or self._monotonic.monotonic_ms() < resume:
            return
        self._accept_resume_mono_ms = None
        if self._selector is not None and self._listener is not None:
            with contextlib.suppress(KeyError):
                self._selector.register(self._listener, selectors.EVENT_READ, "accept")

    def _unwatch_listener(self) -> None:
        if self._selector is None or self._listener is None:
            return
        with contextlib.suppress(KeyError, ValueError):
            self._selector.unregister(self._listener)

    def _on_ready(self, conn: _Connection) -> None:
        if conn.stage == "receiving":
            self._receive(conn)
        elif conn.stage == "sending":
            self._send(conn)

    def _receive(self, conn: _Connection) -> None:
        limit = self._settings.limits.max_message_bytes
        try:
            chunk = conn.sock.recv(min(RECV_CHUNK_BYTES, limit + 1 - len(conn.buffer)))
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._reject(conn, "connection_error")
            return
        if not chunk:
            self._reject(conn, "incomplete_message")
            return
        conn.buffer += chunk
        line, newline, _ = bytes(conn.buffer).partition(b"\n")
        if len(line) > limit:
            self._reject(conn, "message_too_large")
            return
        if not newline:
            return
        # 要求を読み終えた。受信中の枠から外す（受信後の枠で数える）
        self._unregister(conn)
        self._connections.discard(conn)
        conn.deadline_mono_ms = None
        self._handle(conn, line)

    # ---------------------------------------------------------------- 要求の処理

    def _handle(self, conn: _Connection, line: bytes) -> None:
        try:
            request = parse_request(line)
        except RequestError as exc:
            self._reject(conn, exc.code)
            return
        conn.op = request.op
        if isinstance(request, StatusRequest):
            self._finish(conn, self._status_body(), result="accepted", code=None)
            return
        if isinstance(request, SetModeRequest):
            self._handle_set_mode(conn, request)
            return
        self._handle_authority(conn, request)

    def _handle_authority(self, conn: _Connection, request: AuthorityRequest) -> None:
        """authority の降格（0072 §2.6 / §2.7）。**安全側の指令**として監査を待たずに置く。

        受付の時点の実効 stage とは比べない（古いことがある）。適用するかは loop が決める。
        """
        pending_full = self._pending_count() >= self._settings.limits.max_pending_commands
        command_id = self._take_command_id()
        conn.command_id = command_id
        to_stage = (
            AuthorityStage(request.to_stage)
            if isinstance(request, LowerAuthorityRequest)
            else BASELINE_STAGE
        )
        command = AdminAuthorityCommand(
            command_id=command_id,
            op=request.op,
            to_stage=to_stage,
            # 操作者は peer credential から決める（名前解決の結果を記録に使わない。0072 §2.5）
            actor=f"{ADMIN_ACTOR_PREFIX}{conn.uid}",
            reason=request.reason,
        )
        accepted = ControlAdminAuditRecord(
            run_id=self._run_id,
            command_id=command_id,
            event="accepted",
            ts_ms=self._clock.now_ms(),
            peer_uid=conn.uid,
            op=request.op,
            body_json=encode(request.body()).decode("utf-8").rstrip("\n"),
        )
        # **安全側は記録を待たない。** 先に置き、そのあとで受付の行を依頼する。結末の行は
        # 受付の行の後に依頼する（同じ指令の受付の行が結末の行より先に書かれる）
        placement = self._mailbox.place_authority(command)
        if isinstance(placement, Sealed):
            # 停止の手順が枠を取り出し終えている。置いていないので `pending` も受付の行も
            # 残さず、停止の手順が残りの接続を閉じるのと同じく応答せずに閉じる（0072 §2.6）
            LOGGER.warning(
                "停止の手順の途中に届いた authority の降格を置かずに閉じた",
                extra={logs.FIELDS_KEY: {"run_id": self._run_id, "command_id": command_id}},
            )
            self._drop(conn)
            return
        if not self._audit.submit(accepted):
            self._log_audit_failure(command_id, "accepted")
        if isinstance(placement, Superseded):
            # すでに枠にある同じかより低い行き先の降格が効く（0072 §2.2 の合成）
            self._submit_outcome(command_id, "superseded", superseded_by=placement.by, tick_id=None)
            self._finish(
                conn,
                self._superseded_body(command_id, placement.by),
                result="accepted",
                code="superseded",
            )
            return
        if placement.replaced is not None:
            self._supersede(placement.replaced.command_id, by=command_id)
        if pending_full:
            self._finish(conn, self._pending_body(command_id), result="accepted", code="pending")
            return
        self._await_ack(conn, command_id)

    def _handle_set_mode(self, conn: _Connection, request: SetModeRequest) -> None:
        max_lease_s = self._settings.manual.max_lease_s.value
        if request.lease_s is not None and request.lease_s > max_lease_s:
            self._reject(conn, "lease_too_long")
            return
        pending_full = self._pending_count() >= self._settings.limits.max_pending_commands
        if request.weakens_cooling and pending_full:
            # 弱めうる指令は受け渡し口へ置かず、監査も依頼しない
            self._reject(conn, "busy")
            return
        command_id = self._take_command_id()
        conn.command_id = command_id
        command = AdminModeCommand(
            command_id=command_id,
            mode=OperatingMode(request.mode),
            requested=(
                None
                if request.requested is None
                else PerZone[Demand](
                    front=request.requested.front,
                    rear=request.requested.rear,
                    top=request.requested.top,
                )
            ),
            lease_ms=None if request.lease_s is None else request.lease_s * 1_000,
        )
        accepted = ControlAdminAuditRecord(
            run_id=self._run_id,
            command_id=command_id,
            event="accepted",
            ts_ms=self._clock.now_ms(),
            peer_uid=conn.uid,
            op=request.op,
            body_json=encode(request.body()).decode("utf-8").rstrip("\n"),
        )
        if not request.weakens_cooling:
            # **安全側は記録を待たない。** 先に置き、そのあとで受付の行を依頼する
            self._place(command)
            if not self._audit.submit(accepted):
                self._log_audit_failure(command_id, "accepted")
            if pending_full:
                self._finish(
                    conn, self._pending_body(command_id), result="accepted", code="pending"
                )
                return
            self._await_ack(conn, command_id)
            return
        if not self._audit.submit(accepted, ticket=command_id):
            # 記録の無い弱化を作らない
            self._log_audit_failure(command_id, "accepted")
            self._reject(conn, "audit_unavailable")
            return
        conn.stage = "auditing"
        conn.deadline_mono_ms = None
        self._connections.add(conn)
        self._auditing[command_id] = (conn, command)

    def _on_audit_completions(self) -> None:
        for completion in self._audit.drain_completions():
            entry = self._auditing.pop(completion.ticket, None)
            if entry is None:
                continue
            conn, command = entry
            if not completion.ok:
                self._reject(conn, "audit_unavailable")
                continue
            placement = self._place(command)
            if isinstance(placement, Superseded):
                self._finish(
                    conn,
                    self._superseded_body(command.command_id, placement.by),
                    result="accepted",
                    code="superseded",
                )
                continue
            self._await_ack(conn, command.command_id)
        if not self._audit.alive:
            # 書き込みスレッドが死んだ。完了の知らせは来ないので、待っている弱めうる指令を拒否する
            for conn, command in list(self._auditing.values()):
                self._auditing.pop(command.command_id, None)
                self._reject(conn, "audit_unavailable")

    def _place(self, command: AdminModeCommand) -> Placed | Superseded:
        """受け渡し口へ置き、置き換えた古い指令・置けなかった指令を `superseded` として残す。"""
        placement = self._mailbox.place_mode(command)
        if isinstance(placement, Superseded):
            self._submit_outcome(
                command.command_id, "superseded", superseded_by=placement.by, tick_id=None
            )
            return placement
        replaced = placement.replaced
        if replaced is not None:
            self._supersede(replaced.command_id, by=command.command_id)
        return placement

    def _supersede(self, command_id: int, *, by: int) -> None:
        """取り出される前に置き換えられた指令を `superseded` として残し、待っている接続へ返す。"""
        self._submit_outcome(command_id, "superseded", superseded_by=by, tick_id=None)
        waiting = self._awaiting_ack.pop(command_id, None)
        if waiting is not None:
            self._finish(
                waiting,
                self._superseded_body(command_id, by),
                result="accepted",
                code="superseded",
            )

    def _await_ack(self, conn: _Connection, command_id: int) -> None:
        conn.stage = "awaiting_ack"
        conn.deadline_mono_ms = self._monotonic.monotonic_ms() + self._settings.apply_ack_timeout_ms
        self._connections.add(conn)
        self._awaiting_ack[command_id] = conn

    def _on_loop_outcomes(self) -> None:
        for outcome in self._mailbox.drain_outcomes():
            if outcome.kind is ModeOutcomeKind.APPLIED:
                self._submit_outcome(
                    outcome.command_id, "applied", superseded_by=None, tick_id=outcome.tick_id
                )
                conn = self._awaiting_ack.pop(outcome.command_id, None)
                if conn is not None:
                    self._finish(
                        conn,
                        {
                            "ok": True,
                            "command_id": outcome.command_id,
                            "applied": True,
                            "applied_tick_id": outcome.tick_id,
                        },
                        result="accepted",
                        code="applied",
                    )
            else:
                self._submit_outcome(
                    outcome.command_id,
                    "lease_expired",
                    superseded_by=None,
                    tick_id=outcome.tick_id,
                )

    def _submit_outcome(
        self,
        command_id: int,
        event: Literal["applied", "superseded", "lease_expired"],
        *,
        superseded_by: int | None,
        tick_id: int | None,
    ) -> None:
        """結末・lease 切れの行を依頼する。書けなくても適用は取り消さない（0072 §2.7）。"""
        record = ControlAdminAuditRecord(
            run_id=self._run_id,
            command_id=command_id,
            event=event,
            ts_ms=self._clock.now_ms(),
            tick_id=tick_id,
            superseded_by=superseded_by,
        )
        if not self._audit.submit(record):
            self._log_audit_failure(command_id, event)

    def _expire(self) -> None:
        now_ms = self._monotonic.monotonic_ms()
        expired = [
            conn
            for conn in self._connections
            if conn.deadline_mono_ms is not None and conn.deadline_mono_ms <= now_ms
        ]
        for conn in expired:
            if conn.stage == "receiving":
                self._reject(conn, "read_timeout")
            elif conn.stage == "awaiting_ack":
                command_id = conn.command_id
                assert command_id is not None
                self._awaiting_ack.pop(command_id, None)
                # 適用は取り消さない（遅れて効いたことは監査と decision trace に残る）
                self._finish(
                    conn, self._pending_body(command_id), result="accepted", code="pending"
                )
            elif conn.stage == "sending":
                self._log_connection(conn.uid, conn.op, "closed", "send_timeout")
                self._drop(conn)

    # ---------------------------------------------------------------- 応答

    def _reject(self, conn: _Connection, code: str) -> None:
        self._finish(conn, {"ok": False, "error": code}, result="rejected", code=code)

    def _finish(
        self, conn: _Connection, body: dict[str, Any], *, result: str, code: str | None
    ) -> None:
        """応答を1行返して閉じる。送り切れなければ送信中として残す。"""
        self._log_connection(conn.uid, conn.op, result, code, command_id=conn.command_id)
        self._unregister(conn)
        conn.outgoing = encode(body)
        conn.stage = "sending"
        conn.deadline_mono_ms = self._monotonic.monotonic_ms() + self._read_timeout_ms()
        self._connections.add(conn)
        self._send(conn)
        if conn in self._connections and conn.stage == "sending":
            self._register(conn, selectors.EVENT_WRITE)

    def _send(self, conn: _Connection) -> None:
        try:
            sent = conn.sock.send(conn.outgoing)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            # 相手が先に切った。受付・拒否の記録は済んでいる
            self._drop(conn)
            return
        conn.outgoing = conn.outgoing[sent:]
        if not conn.outgoing:
            self._drop(conn)

    def _reply_and_close(self, sock: socket.socket, body: dict[str, Any]) -> None:
        """認可前の拒否。**待たない**（1回だけ送り、送れなくても閉じる）。"""
        try:
            sock.send(encode(body))
        except OSError:
            pass
        finally:
            sock.close()

    def _status_body(self) -> dict[str, Any]:
        """いまの状態（0072 §2.7）。**読み取り API には出さない。**"""
        status = self._mailbox.latest_status()
        remaining: int | None = None
        if status is not None and status.lease_deadline_mono_ms is not None:
            remaining = max(0, status.lease_deadline_mono_ms - self._monotonic.monotonic_ms())
        authority = None if status is None else status.authority
        return {
            "ok": True,
            "run_id": self._run_id,
            "tick_id": None if status is None else status.tick_id,
            "mode": None if status is None else status.mode.value,
            "command_id": None if status is None else status.command_id,
            "manual_lease_remaining_ms": remaining,
            "authority_stage": None if status is None else status.authority_stage.value,
            # 最後に loop が置いた写し（0072 §2.7）。journal を持たない構成では null
            "authority_journal_stage": (
                None
                if authority is None or authority.journal_stage is None
                else authority.journal_stage.value
            ),
            "authority_journal_revision": None if authority is None else authority.journal_revision,
            "authority_ceiling": (
                self._authority_ceiling.value
                if authority is None
                else authority.config_ceiling.value
            ),
            "authority_unpersisted_ceiling": (
                None
                if authority is None or authority.unpersisted_ceiling is None
                else authority.unpersisted_ceiling.value
            ),
            "authority_journal_unreadable": (
                None if authority is None else authority.journal_unreadable
            ),
            "persist_failure": (
                None
                if authority is None or authority.persist_failure is None
                else authority.persist_failure.model_dump(mode="json")
            ),
            "entry": "open",
            "audit_failures": self._audit.failures,
        }

    @staticmethod
    def _pending_body(command_id: int) -> dict[str, Any]:
        return {
            "ok": True,
            "command_id": command_id,
            "applied": False,
            "applied_tick_id": None,
            "pending": True,
        }

    @staticmethod
    def _superseded_body(command_id: int, by: int) -> dict[str, Any]:
        return {
            "ok": True,
            "command_id": command_id,
            "applied": False,
            "applied_tick_id": None,
            "superseded_by": by,
        }

    # ---------------------------------------------------------------- 補助

    def _take_command_id(self) -> int:
        """受け渡し口へ置く前に採番する。プロセスの中で単調に増え、DB を待たない（0072 §2.7）。"""
        self._next_command_id += 1
        return self._next_command_id

    def _pending_count(self) -> int:
        return sum(1 for conn in self._connections if conn.stage != "receiving")

    def _read_timeout_ms(self) -> int:
        return int(self._settings.limits.read_timeout_s * 1_000)

    def _register(self, conn: _Connection, events: int) -> None:
        if self._selector is not None:
            self._selector.register(conn.sock, events, conn)

    def _unregister(self, conn: _Connection) -> None:
        if self._selector is None:
            return
        with contextlib.suppress(KeyError, ValueError):
            self._selector.unregister(conn.sock)

    def _drop(self, conn: _Connection) -> None:
        self._unregister(conn)
        self._connections.discard(conn)
        if conn.command_id is not None:
            if self._awaiting_ack.get(conn.command_id) is conn:
                del self._awaiting_ack[conn.command_id]
            entry = self._auditing.get(conn.command_id)
            if entry is not None and entry[0] is conn:
                del self._auditing[conn.command_id]
        with contextlib.suppress(OSError):
            conn.sock.close()

    def _log_audit_failure(self, command_id: int, event: str) -> None:
        LOGGER.error(
            "監査の書き込みを依頼できなかった",
            extra={
                logs.FIELDS_KEY: {
                    "run_id": self._run_id,
                    "command_id": command_id,
                    "event": event,
                    "audit_failures": self._audit.failures,
                }
            },
        )

    @staticmethod
    def _log_connection(
        uid: int,
        op: str | None,
        result: str,
        code: str | None,
        *,
        command_id: int | None = None,
    ) -> None:
        """接続ごとのログ（0072 §2.7）。**入力の値そのものは残さない。**"""
        LOGGER.info(
            "control-admin connection",
            extra={
                logs.FIELDS_KEY: {
                    "peer_uid": uid,
                    "op": op,
                    "result": result,
                    "code": code,
                    "command_id": command_id,
                }
            },
        )
