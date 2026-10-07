"""Learned worker との経路の受付スレッド（決定記録 0077 §2.2 / §2.5 / §2.7）。

`coldaisle-fand` の中の**受付スレッド1本**が、役割ごとの `SOCK_SEQPACKET` の待ち受け2つと、
役割ごとに高々1つの接続を I/O 多重化（`selectors`）で持つ。

- **役割は OS の資格で決める。** 各ソケットは `SO_PEERCRED` の相手が**そのソケットの役割の
  グループ**に属し、**もう一方の役割のグループに属さない**ときだけ接続を認める。`coldaisle-fand` と
  同じ uid は認めない。root も暗黙には認めない（グループに入っていなければ拒む）
- 役割ごとに**同時に1接続**。生きた接続がある役割への新しい接続は拒む
- 受信・長さの上限の確認・JSON の解釈・pydantic の検証・`run_id` と `role` の照合までをここで行い、
  検証を通った MPC の結果だけを受け渡し口へ置く。壊れたメッセージは捨てて数える（受け渡し口は
  変えない。最後の有効なメッセージの時刻も変えない）
- 最後の**有効な**メッセージ（結果・heartbeat）から `worker_idle_timeout_ms` 黙った接続は閉じる
  （`worker_idle`）。EOF は `worker_disconnected`。どちらも**その時点で**受け渡し口を空にする
- loop が置いた frame を、接続している役割へ非ブロッキングで送る。送れなければ捨てる（再送も待ちも
  しない）
- `registry_watch` を渡されたら、`registry_check_interval_ms`（`safety.tick_ms`）ごとに registry の
  production を起動時の固定と比べ、移動・消失・読めない役割を**再起動まで閉じる**
  （`registry_superseded`。0077 §2.6）。閉じた役割に届いたメッセージは検証の前に捨てる。
  I/O はこのスレッドで行い、loop の tick に入れない（0060 §2.3）

**受付スレッドは loop の状態に一切触れない。** 受付スレッドの例外で `coldaisle-fand` を
終わらせない。死んだら loop が毎 tick の確認でそれを見て、再起動まで Learned を読まない
（0077 §2.2。Max にはしない）。
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import selectors
import socket
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from coldaisle import logs
from coldaisle.clock import MonotonicClock
from coldaisle.control.learned_handoff import LearnedChannelState, LearnedFrame, LearnedRole
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.learned_channel.mailbox import LearnedMailbox
from coldaisle.learned_channel.messages import (
    FrameBody,
    HelloBody,
    MpcResultBody,
    OutboundEnvelope,
    encode_outbound,
    parse_inbound,
)
from coldaisle.learned_channel.registry_watch import RegistryWatch
from coldaisle.local_socket import (
    SocketStartupError,
    acquire_lock,
    group_member_uids,
    identity,
    peer_credentials_supported,
    peer_uid,
    prepare_parent,
    prepare_path,
    resolve_group,
    uid_in_group,
    umask,
    unlink_if_same,
)

LOGGER = logging.getLogger("coldaisle.learned_channel")

SERVICE = "coldaisle-fand の Learned の経路"
"""起動時の検査のメッセージに出す入口の名前。"""


class GroupDirectory(Protocol):
    """グループの解決と所属の判定（試験で `SO_PEERCRED` と所属を偽装するための口）。"""

    def gid(self, name: str) -> int:
        """グループ名の gid。解決できなければ `SocketStartupError`。"""

    def members(self, gid: int) -> frozenset[int]:
        """そのグループに属する uid の集合（起動時の重なりの検査）。"""

    def is_member(self, uid: int, gid: int) -> bool:
        """uid がそのグループに属するか（接続のたびに引き直す）。"""


class SystemGroupDirectory:
    """OS のグループ情報（`local_socket` の門と同じ規則。0072 §2.5）。"""

    def gid(self, name: str) -> int:
        """グループ名の gid。"""
        gid = resolve_group(name)
        assert gid is not None
        return gid

    def members(self, gid: int) -> frozenset[int]:
        """補助グループの `gr_mem` と、主グループがそのグループであるユーザーの両方。"""
        return group_member_uids(gid)

    def is_member(self, uid: int, gid: int) -> bool:
        """uid がそのグループに属するか。"""
        return uid_in_group(uid, gid)


def set_socket_group(path: Path, gid: int) -> None:
    """作ったソケットのグループを役割のグループにする。"""
    os.chown(path, -1, gid)


@dataclass(eq=False)
class _Connection:
    role: LearnedRole
    sock: socket.socket
    uid: int
    last_valid_mono_ms: int


@dataclass(frozen=True, slots=True)
class _Listener:
    role: LearnedRole
    sock: socket.socket
    path: Path
    bound: tuple[int, int]
    gid: int


class LearnedChannelServer:
    """役割ごとのソケットで待ち受け、検証済みの結果を受け渡し口へ置き、frame を送る。"""

    def __init__(
        self,
        *,
        settings: LearnedChannelSettings,
        mailbox: LearnedMailbox,
        monotonic: MonotonicClock,
        run_id: str,
        server_uid: int | None = None,
        groups: GroupDirectory | None = None,
        peer: Callable[[socket.socket], int] = peer_uid,
        registry_watch: RegistryWatch | None = None,
        registry_check_interval_ms: int | None = None,
    ) -> None:
        if registry_watch is not None and (
            registry_check_interval_ms is None or registry_check_interval_ms < 1
        ):
            raise ValueError("registry を監視するなら確認の間隔（safety.tick_ms）を渡す")
        if not peer_credentials_supported():
            raise SocketStartupError(
                "SO_PEERCRED が無いプラットフォームでは Learned の経路を開かない"
            )
        self._settings = settings
        self._mailbox = mailbox
        self._monotonic = monotonic
        self._run_id = run_id
        self._server_uid = os.geteuid() if server_uid is None else server_uid
        self._groups: GroupDirectory = groups if groups is not None else SystemGroupDirectory()
        self._peer = peer
        self._gids = {role: self._groups.gid(_group_name(settings, role)) for role in LearnedRole}
        self._check_groups_do_not_overlap()
        self._listeners: dict[LearnedRole, _Listener] = {}
        self._lock_fds: list[int] = []
        self._connections: dict[LearnedRole, _Connection] = {}
        self._dropped: Counter[str] = Counter()
        self._selector: selectors.BaseSelector | None = None
        self._thread = threading.Thread(target=self._run, name="learned-channel", daemon=True)
        self._stop = False
        self._registry_watch = registry_watch
        self._registry_interval_ms = registry_check_interval_ms or 0
        # 起動直後に1回確かめる（起動時の読み込みから受付の開始までの間の移動も拾う）
        self._next_registry_check_ms: int | None = (
            None if registry_watch is None else self._monotonic.monotonic_ms()
        )

    @property
    def thread(self) -> threading.Thread:
        """受付スレッド（受け渡し口が生存を答える対象）。"""
        return self._thread

    @property
    def dropped(self) -> dict[str, int]:
        """捨てたメッセージ・frame の件数（理由の code ごと）。"""
        return dict(self._dropped)

    def socket_path(self, role: LearnedRole) -> Path:
        """役割のソケットの場所。"""
        return self._settings.sockets.for_role(role).path

    # ---------------------------------------------------------------- 起動と停止

    def _check_groups_do_not_overlap(self) -> None:
        """2つのグループが同じ gid・同じ uid を含むなら開かない（0077 §2.7）。

        名前が違っても同じ uid を含む2つのグループは、その uid に両方の役割を与えてしまう。
        """
        mpc, rl = self._gids[LearnedRole.MPC], self._gids[LearnedRole.SUPERVISOR]
        if mpc == rl:
            raise SocketStartupError("mpc と supervisor のグループが同じ gid を指している")
        overlap = self._groups.members(mpc) & self._groups.members(rl)
        if overlap:
            raise SocketStartupError(
                f"mpc と supervisor のグループに同じ uid が属している（{len(overlap)} 件）"
            )

    def bind(self) -> None:
        """役割ごとにソケットを作り、権限を設定して待ち受けを始める（0072 §2.5 と同じ検査）。"""
        try:
            for role in LearnedRole:
                self._bind_role(role)
        except BaseException:
            self.close()
            raise

    def _bind_role(self, role: LearnedRole) -> None:
        settings = self._settings.sockets.for_role(role)
        gid = self._gids[role]
        prepare_parent(settings.path.parent, gid, service=SERVICE)
        lock_fd = acquire_lock(settings.path, service=SERVICE)
        self._lock_fds.append(lock_fd)
        prepare_path(settings.path, service=SERVICE, sock_type=socket.SOCK_SEQPACKET)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        bound: tuple[int, int] | None = None
        try:
            # 作った瞬間から所有者以外に開かない。権限は直後に設定値へ広げる
            with umask(0o177):
                listener.bind(str(settings.path))
            bound = identity(os.lstat(settings.path))
            set_socket_group(settings.path, gid)
            os.chmod(settings.path, settings.mode_bits)
            listener.listen(self._settings.limits.listen_backlog)
            listener.setblocking(False)
        except BaseException:
            listener.close()
            unlink_if_same(settings.path, bound)
            raise
        assert bound is not None
        self._listeners[role] = _Listener(
            role=role, sock=listener, path=settings.path, bound=bound, gid=gid
        )
        LOGGER.info(
            "Learned の経路で待ち受ける",
            extra={
                logs.FIELDS_KEY: {
                    "role": role.value,
                    "socket": str(settings.path),
                    "mode": settings.mode,
                    "group": settings.group,
                    "run_id": self._run_id,
                }
            },
        )

    def start(self) -> None:
        """受付スレッドを起動する。受け渡し口に生存を答える対象として渡してから起動する。"""
        if len(self._listeners) != len(LearnedRole):
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
        """待ち受けを閉じ、**自分が作ったソケットだけ**を消す。**待たない。**

        受付スレッドがまだ生きていれば、selector に載った fd は閉じずに受付スレッドへ任せる
        （daemon thread なので process の終了は妨げない）。ソケットのファイルとロックは片付ける。
        """
        if not self._thread.is_alive():
            for conn in list(self._connections.values()):
                with contextlib.suppress(OSError):
                    conn.sock.close()
            self._connections.clear()
            for listener in self._listeners.values():
                with contextlib.suppress(OSError):
                    listener.sock.close()
        for listener in self._listeners.values():
            unlink_if_same(listener.path, listener.bound)
        for fd in self._lock_fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        self._lock_fds.clear()

    # ---------------------------------------------------------------- 受付スレッド

    def _run(self) -> None:
        try:
            self._serve()
        except BaseException:
            # 受付スレッドの例外で coldaisle-fand を終わらせない。loop が毎 tick の確認で気づき、
            # 再起動まで Learned を読まない（0077 §2.2。Max にはしない）。**後片付けの前に**
            # channel_dead を公開する（片付けの間の tick に古い提案を読ませない）
            self._mailbox.receiver_failed()
            LOGGER.exception(
                "Learned の経路の受付スレッドが止まった",
                extra={logs.FIELDS_KEY: {"reason": "learned_channel_dead"}},
            )
        finally:
            selector = self._selector
            self._selector = None
            if selector is not None:
                selector.close()
            for conn in list(self._connections.values()):
                with contextlib.suppress(OSError):
                    conn.sock.close()
            self._connections.clear()

    def _serve(self) -> None:
        selector = selectors.DefaultSelector()
        self._selector = selector
        for listener in self._listeners.values():
            selector.register(listener.sock, selectors.EVENT_READ, listener)
        selector.register(self._mailbox.wake_reader, selectors.EVENT_READ, "wake")
        while not self._stop:
            for key, _events in selector.select(self._select_timeout_s()):
                if key.data == "wake":
                    self._mailbox.drain_wake()
                elif isinstance(key.data, _Listener):
                    self._accept(key.data)
                elif isinstance(key.data, _Connection):
                    self._on_readable(key.data)
            self._check_registry()
            self._expire_idle()
            self._send_outgoing()

    def _select_timeout_s(self) -> float | None:
        """最も早い締め切り（idle・registry の確認）までの秒数。無ければ起こされるまで待つ。"""
        deadlines: list[int] = []
        if self._connections:
            timeout_ms = self._settings.worker_idle_timeout_ms.value
            earliest = min(conn.last_valid_mono_ms for conn in self._connections.values())
            deadlines.append(earliest + timeout_ms)
        if self._next_registry_check_ms is not None:
            deadlines.append(self._next_registry_check_ms)
        if not deadlines:
            return None
        return max(0.0, (min(deadlines) - self._monotonic.monotonic_ms()) / 1_000)

    # ---------------------------------------------------------------- registry の監視

    def _check_registry(self) -> None:
        """`safety.tick_ms` ごとに、固定した artifact がいまも production か確かめる（0077 §2.6）。

        移動・消失・読めない役割を再起動まで閉じる。**Gate の期待値も trace の provenance も
        変えない**（ここでは古い期待値に一致する結果が届かないようにするだけ）。
        """
        watch, due = self._registry_watch, self._next_registry_check_ms
        if watch is None or due is None:
            return
        now_ms = self._monotonic.monotonic_ms()
        if now_ms < due:
            return
        self._next_registry_check_ms = now_ms + self._registry_interval_ms
        try:
            moved = watch.check()
        except Exception:
            # check() は例外を出さない約束だが、出たら読めなかったのと同じに扱う（保守側）
            LOGGER.exception("registry の確認に失敗した。Learned の役割を再起動まで閉じる")
            moved = frozenset(LearnedRole)
        for role in moved:
            if self._mailbox.superseded(role):
                continue
            self._mailbox.supersede(role)
            pinned = watch.pins.for_role(role)
            LOGGER.error(
                "固定した artifact が production でなくなったため、再起動まで役割を閉じる",
                extra={
                    logs.FIELDS_KEY: {
                        "reason": LearnedChannelState.REGISTRY_SUPERSEDED.value,
                        "role": role.value,
                        "pinned": None if pinned is None else pinned.model_dump(),
                        "run_id": self._run_id,
                    }
                },
            )
        if all(self._mailbox.superseded(role) for role in LearnedRole):
            # すべて閉じたら確かめ続ける理由は無い（再起動まで戻さない）
            self._next_registry_check_ms = None

    # ---------------------------------------------------------------- 受け付けと認可

    def _accept(self, listener: _Listener) -> None:
        while True:
            try:
                sock, _ = listener.sock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                if exc.errno == errno.ECONNABORTED:
                    # 相手が accept の前に切った。その接続だけの事情
                    continue
                # fd の枯渇などは受付スレッドの死として扱う（Learned を閉じる。Max にはしない）
                raise
            self._admit(listener, sock)

    def _admit(self, listener: _Listener, sock: socket.socket) -> None:
        role = listener.role
        try:
            uid = self._peer(sock)
        except OSError:
            LOGGER.warning("接続相手の uid を取れないため拒否した", exc_info=True)
            sock.close()
            return
        refusal = self._refusal(role, uid)
        if refusal is not None:
            self._log_connection(role, uid, "rejected", refusal)
            sock.close()
            return
        sock.setblocking(False)
        conn = _Connection(
            role=role, sock=sock, uid=uid, last_valid_mono_ms=self._monotonic.monotonic_ms()
        )
        hello = encode_outbound(
            OutboundEnvelope(
                run_id=self._run_id,
                role=role,
                body=HelloBody(heartbeat_interval_ms=self._settings.heartbeat_interval_ms.value),
            )
        )
        try:
            sock.send(hello)
        except OSError:
            self._log_connection(role, uid, "closed", "hello_failed")
            sock.close()
            return
        self._connections[role] = conn
        assert self._selector is not None
        self._selector.register(sock, selectors.EVENT_READ, conn)
        self._mailbox.connected(role)
        self._log_connection(role, uid, "accepted", None)

    def _refusal(self, role: LearnedRole, uid: int) -> str | None:
        """この接続を拒む理由の code。認めるなら None（0077 §2.7）。"""
        if uid == self._server_uid:
            return "same_user"
        if not self._groups.is_member(uid, self._gids[role]):
            return "unauthorized"
        # 起動後にグループの所属は変わりうる。もう一方の役割の資格も持つ相手は拒む
        other = LearnedRole.SUPERVISOR if role is LearnedRole.MPC else LearnedRole.MPC
        if self._groups.is_member(uid, self._gids[other]):
            return "both_roles"
        if role in self._connections:
            return "role_busy"
        return None

    # ---------------------------------------------------------------- 受信

    def _on_readable(self, conn: _Connection) -> None:
        limit = self._settings.limits.max_message_bytes.value
        try:
            data, _, flags, _ = conn.sock.recvmsg(limit + 1)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._disconnect(conn, LearnedChannelState.WORKER_DISCONNECTED, "recv_failed")
            return
        if not data and not flags & socket.MSG_TRUNC:
            self._disconnect(conn, LearnedChannelState.WORKER_DISCONNECTED, "eof")
            return
        if len(data) > limit or flags & socket.MSG_TRUNC:
            self._drop(conn.role, "too_long")
            return
        if self._mailbox.superseded(conn.role):
            # 閉じた役割に届いたものは**検証の前に**捨てる（0077 §2.6）。生存の知らせにも数えない
            self._drop(conn.role, "registry_superseded")
            return
        try:
            envelope = parse_inbound(data)
        except (ValidationError, ValueError):
            self._drop(conn.role, "invalid")
            return
        if envelope.run_id != self._run_id:
            # 再起動をまたいだ取り違え（0077 §2.4 の1）
            self._drop(conn.role, "run_id_mismatch")
            return
        if envelope.role is not conn.role:
            # 照合であって認可ではない。役割はソケットで決まっている
            self._drop(conn.role, "role_mismatch")
            return
        body = envelope.body
        if isinstance(body, MpcResultBody):
            if conn.role is not LearnedRole.MPC:
                self._drop(conn.role, "unsupported_body")
                return
            self._mailbox.place_mpc(body.result)
        # heartbeat は idle を数え直すだけで、受け渡し口の値も受信時刻も変えない
        conn.last_valid_mono_ms = self._monotonic.monotonic_ms()

    def _expire_idle(self) -> None:
        timeout_ms = self._settings.worker_idle_timeout_ms.value
        now_ms = self._monotonic.monotonic_ms()
        for conn in list(self._connections.values()):
            if now_ms - conn.last_valid_mono_ms >= timeout_ms:
                self._disconnect(conn, LearnedChannelState.WORKER_IDLE, "idle")

    def _disconnect(self, conn: _Connection, state: LearnedChannelState, reason: str) -> None:
        """接続を閉じ、**その時点で**受け渡し口を空にする（0077 §2.5）。

        **状態の変更と受け渡し口の消去を先に行う。** ソケットの後片付けの間に回った tick が
        `connected` を見て古い提案を読まないため。
        """
        self._mailbox.disconnected(conn.role, state)
        if self._connections.get(conn.role) is conn:
            del self._connections[conn.role]
        if self._selector is not None:
            with contextlib.suppress(KeyError, ValueError):
                self._selector.unregister(conn.sock)
        with contextlib.suppress(OSError):
            conn.sock.close()
        self._log_connection(conn.role, conn.uid, "closed", reason)

    # ---------------------------------------------------------------- 送信

    def _send_outgoing(self) -> None:
        frame = self._mailbox.take_outgoing()
        if frame is None or not self._connections:
            return
        for conn in list(self._connections.values()):
            self._send_frame(conn, frame)

    def _send_frame(self, conn: _Connection, frame: LearnedFrame) -> None:
        data = encode_outbound(
            OutboundEnvelope(run_id=self._run_id, role=conn.role, body=FrameBody(frame=frame))
        )
        if len(data) > self._settings.limits.max_message_bytes.value:
            self._drop(conn.role, "frame_too_long")
            return
        try:
            conn.sock.send(data)
        except (BlockingIOError, InterruptedError):
            # 送信バッファが一杯。捨てる（再送も待ちもしない。0077 §2.2）
            self._drop(conn.role, "frame_send_busy")
        except OSError as exc:
            if exc.errno == errno.EMSGSIZE:
                self._drop(conn.role, "frame_too_long")
                return
            self._disconnect(conn, LearnedChannelState.WORKER_DISCONNECTED, "send_failed")

    # ---------------------------------------------------------------- 記録

    def _drop(self, role: LearnedRole, code: str) -> None:
        """捨てて数える。件数は2の冪に届いたときだけログに出す（溢れさせない）。"""
        key = f"{role.value}.{code}"
        self._dropped[key] += 1
        count = self._dropped[key]
        if count & (count - 1) == 0:
            LOGGER.warning(
                "Learned の経路でメッセージを捨てた",
                extra={logs.FIELDS_KEY: {"role": role.value, "code": code, "count": count}},
            )

    def _log_connection(self, role: LearnedRole, uid: int, event: str, reason: str | None) -> None:
        LOGGER.log(
            logging.INFO if event == "accepted" else logging.WARNING,
            "Learned の経路の接続",
            extra={
                logs.FIELDS_KEY: {
                    "role": role.value,
                    "peer_uid": uid,
                    "event": event,
                    "reason": reason,
                    "run_id": self._run_id,
                }
            },
        )


def _group_name(settings: LearnedChannelSettings, role: LearnedRole) -> str:
    group = settings.sockets.for_role(role).group
    assert group is not None  # 設定の検証で必須にしてある
    return group
