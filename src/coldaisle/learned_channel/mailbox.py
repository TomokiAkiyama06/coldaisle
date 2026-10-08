"""受付スレッドと loop の受け渡し口（決定記録 0077 §2.2 / §2.5）。

- **役割ごとに1枠。** 受付スレッドは検証を通った**最新の1件だけ**を置く（新しいものが古いものを
  置き換える。積まない）。MPC の枠（`MpcProposal`）と RL Supervisor の枠
  （`DeliveredSupervisorOutput`。段階 4 / #89）
- loop は **lock を試すだけ**で覗く（`poll`）。取れなければ前回の値。**待たない**（0060 §2.3）
- 経路の状態（`state`）は **lock を取らずに**答える。受付スレッドが死んでいれば `channel_dead`
- worker が切れた・黙ったときは、**状態を先に変えてから**その役割の枠を空にする。loop は状態を
  見てから覗くので、切れた tick から古い結果を読まない（0077 §2.5 の表。RL も直前の出力を
  `supervisor.valid_ms` まで残さない）
- 送り出し用の1枠（`offer`）。loop は置くだけで、送るのは受付スレッド。置けなければ捨てる
- 固定した artifact が production でなくなった役割は、**再起動まで** `registry_superseded` を答え、
  枠を空にし、以後の結果を置かない（0077 §2.6）。接続・切断の知らせでは戻らない

loop の状態（`ControlLoop` / `ControllerGate`）には触れない。
"""

from __future__ import annotations

import socket
import threading

from coldaisle.control.learned_handoff import LearnedChannelState, LearnedFrame, LearnedRole
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.supervisor.policy import DeliveredSupervisorOutput


class LearnedMailbox:
    """役割ごとの結果の枠・役割ごとの経路の状態・送り出し用の1枠・受付スレッドを起こす口。

    `poll()` が MPC の枠（`LearnedProposalSource`）、`supervisor_source.poll()` が RL の枠
    （`SupervisorOutputSource`）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mpc: MpcProposal | None = None
        self._last_polled: MpcProposal | None = None
        self._supervisor: DeliveredSupervisorOutput | None = None
        self._last_polled_supervisor: DeliveredSupervisorOutput | None = None
        self.supervisor_source = SupervisorSlot(self)
        """RL Supervisor の枠を覗く口（`SupervisorOutputSource`）。"""
        self._states: dict[LearnedRole, LearnedChannelState] = {
            role: LearnedChannelState.WORKER_DISCONNECTED for role in LearnedRole
        }
        # 受付スレッドだけが置き換え、loop は lock を取らずに読む（frozenset の差し替えは原子的）
        self._superseded: frozenset[LearnedRole] = frozenset()
        self._outgoing_lock = threading.Lock()
        self._outgoing: LearnedFrame | None = None
        self._receiver: threading.Thread | None = None
        self._stopping = False
        self._failed = False
        self._wake_reader, self._wake_writer = socket.socketpair()
        self._wake_reader.setblocking(False)
        self._wake_writer.setblocking(False)

    # ---------------------------------------------------------------- 受付スレッドの側

    @property
    def wake_reader(self) -> socket.socket:
        """受付スレッドが selector に載せる、起こされる側の端。"""
        return self._wake_reader

    def attach_receiver(self, thread: threading.Thread) -> None:
        """生存を答える対象の受付スレッド。**起動する前に**渡す。"""
        self._receiver = thread

    def mark_stopping(self) -> None:
        """`coldaisle-fand` 自身の停止の手順（死んだとは扱わない）。"""
        self._stopping = True

    def receiver_failed(self) -> None:
        """受付スレッドが致命的な例外で止まる。**後片付けの前に**呼ぶ（Codex P1）。

        スレッドが生きている間の後片付けで、loop が `connected` を見て古い提案を読まないよう、
        先に `channel_dead` を答えるようにしてから枠を空にする。
        """
        self._failed = True
        with self._lock:
            self._clear(LearnedRole.MPC)
            self._clear(LearnedRole.SUPERVISOR)

    def connected(self, role: LearnedRole) -> None:
        """その役割の worker の接続を認めた。"""
        self._states[role] = LearnedChannelState.CONNECTED

    def disconnected(self, role: LearnedRole, state: LearnedChannelState) -> None:
        """その役割の接続が切れた・閉じた。**状態を先に変えてから**枠を空にする。

        ``state`` は `worker_disconnected`（EOF・送信の失敗）か `worker_idle`（黙った）。
        """
        self._states[role] = state
        with self._lock:
            self._clear(role)

    def supersede(self, role: LearnedRole) -> None:
        """その役割を `coldaisle-fand` の再起動まで閉じる（0077 §2.6 の `registry_superseded`）。

        **状態を先に変えてから**枠を空にする（`disconnected` と同じ順。loop は状態を見てから覗く）。
        """
        self._superseded = self._superseded | {role}
        with self._lock:
            self._clear(role)

    def superseded(self, role: LearnedRole) -> bool:
        """その役割を registry の移動で閉じたか。**lock を取らない。**"""
        return role in self._superseded

    def place_mpc(self, result: MpcProposal) -> None:
        """検証を通った MPC の結果を置く。前の結果は置き換える（積まない）。

        閉じた役割（`registry_superseded`）には置かない（受付が検証の前に捨てるのに加えた二重の守り）。
        """
        with self._lock:
            if LearnedRole.MPC in self._superseded:
                return
            self._mpc = result

    def place_supervisor(self, result: DeliveredSupervisorOutput) -> None:
        """検証を通った RL Supervisor の出力を置く。前の出力は置き換える（積まない）。

        閉じた役割（`registry_superseded`）には置かない（`place_mpc` と同じ二重の守り）。
        """
        with self._lock:
            if LearnedRole.SUPERVISOR in self._superseded:
                return
            self._supervisor = result

    def _clear(self, role: LearnedRole) -> None:
        """その役割の枠と、lock を取れなかった tick に返す写しを捨てる。**lock の中で呼ぶ。**

        写しも捨てるのは、再接続の後に古い結果を返さないため。
        """
        if role is LearnedRole.MPC:
            self._mpc = None
            self._last_polled = None
        else:
            self._supervisor = None
            self._last_polled_supervisor = None

    def take_outgoing(self) -> LearnedFrame | None:
        """送り出し用の枠から取り出す。"""
        with self._outgoing_lock:
            frame, self._outgoing = self._outgoing, None
            return frame

    def drain_wake(self) -> None:
        """起こしの bytes を読み捨てる。"""
        try:
            while self._wake_reader.recv(4096):
                pass
        except (BlockingIOError, InterruptedError):
            return

    def close(self) -> None:
        """起こすための socket を閉じる。

        受付スレッドがまだ生きていれば、起こされる側の端は閉じない（selector に載ったままの fd を
        別のスレッドから閉じない。daemon thread なので process の終了は妨げない）。
        """
        self._wake_writer.close()
        receiver = self._receiver
        if receiver is None or not receiver.is_alive():
            self._wake_reader.close()

    # ---------------------------------------------------------------- loop の側（待たない）

    def receiver_alive(self) -> bool:
        """受付スレッドが生きているか。**lock を取らない。**

        停止の手順の途中は生きているとみなす。
        """
        if self._failed:
            return False
        if self._stopping:
            return True
        receiver = self._receiver
        return receiver is not None and receiver.is_alive()

    def state(self, role: LearnedRole) -> LearnedChannelState:
        """経路の状態（`LearnedChannelHealth`）。**lock を取らない。**"""
        if not self.receiver_alive():
            return LearnedChannelState.CHANNEL_DEAD
        if role in self._superseded:
            return LearnedChannelState.REGISTRY_SUPERSEDED
        return self._states[role]

    def poll(self) -> MpcProposal | None:
        """MPC の枠を覗く（`LearnedProposalSource`）。**lock を試すだけ。** 取り出さない。

        lock を取れなければ前回の値を返す。ただしその間に接続が切れていれば何も返さない。
        """
        if not self._lock.acquire(blocking=False):
            if self.state(LearnedRole.MPC) is not LearnedChannelState.CONNECTED:
                return None
            return self._last_polled
        try:
            # **lock の中でも状態を確かめ直す。** 受付スレッドは状態を先に変えてから lock を
            # 待つので、その間に lock を取った tick が切断済みの古い提案を返さないため
            if self.state(LearnedRole.MPC) is not LearnedChannelState.CONNECTED:
                self._last_polled = None
                return None
            self._last_polled = self._mpc
            return self._mpc
        finally:
            self._lock.release()

    def poll_supervisor(self) -> DeliveredSupervisorOutput | None:
        """RL Supervisor の枠を覗く。**lock を試すだけ。** 取り出さない（`poll` と同じ規則）。"""
        if not self._lock.acquire(blocking=False):
            if self.state(LearnedRole.SUPERVISOR) is not LearnedChannelState.CONNECTED:
                return None
            return self._last_polled_supervisor
        try:
            if self.state(LearnedRole.SUPERVISOR) is not LearnedChannelState.CONNECTED:
                self._last_polled_supervisor = None
                return None
            self._last_polled_supervisor = self._supervisor
            return self._supervisor
        finally:
            self._lock.release()

    def offer(self, frame: LearnedFrame) -> None:
        """送り出し用の1枠へ置き、受付スレッドを起こす（`LearnedFrameSink`）。**待たない。**

        lock を取れなければ（受付スレッドが取り出している最中）この frame は捨てる。
        """
        if not self._outgoing_lock.acquire(blocking=False):
            return
        try:
            self._outgoing = frame
        finally:
            self._outgoing_lock.release()
        self.wake()

    def wake(self) -> None:
        """受付スレッドを起こす。**待たない。**"""
        try:
            self._wake_writer.send(b"\0")
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            # 閉じた後。起こす相手がいない
            pass


class SupervisorSlot:
    """`LearnedMailbox` の RL Supervisor の枠を `SupervisorOutputSource` として見せる口。"""

    __slots__ = ("_mailbox",)

    def __init__(self, mailbox: LearnedMailbox) -> None:
        self._mailbox = mailbox

    def poll(self) -> DeliveredSupervisorOutput | None:
        """最新の RL 出力。**待たない。**"""
        return self._mailbox.poll_supervisor()
