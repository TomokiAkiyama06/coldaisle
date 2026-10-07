"""受付スレッドと loop の受け渡し口（決定記録 0077 §2.2 / §2.5）。

- **役割ごとに1枠。** 受付スレッドは検証を通った**最新の1件だけ**を置く（新しいものが古いものを
  置き換える。積まない）。段階 1 で結果を置くのは MPC の枠だけ
- loop は **lock を試すだけ**で覗く（`poll`）。取れなければ前回の値。**待たない**（0060 §2.3）
- 経路の状態（`state`）は **lock を取らずに**答える。受付スレッドが死んでいれば `channel_dead`
- worker が切れた・黙ったときは、**状態を先に変えてから**枠を空にする。loop は状態を見てから
  覗くので、切れた tick から古い提案を読まない（0077 §2.5 の表）
- 送り出し用の1枠（`offer`）。loop は置くだけで、送るのは受付スレッド。置けなければ捨てる

loop の状態（`ControlLoop` / `ControllerGate`）には触れない。
"""

from __future__ import annotations

import socket
import threading

from coldaisle.control.learned_handoff import LearnedChannelState, LearnedFrame, LearnedRole
from coldaisle.control.mpc.controller import MpcProposal


class LearnedMailbox:
    """MPC の結果の枠・役割ごとの経路の状態・送り出し用の1枠・受付スレッドを起こす口。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mpc: MpcProposal | None = None
        self._last_polled: MpcProposal | None = None
        self._states: dict[LearnedRole, LearnedChannelState] = {
            role: LearnedChannelState.WORKER_DISCONNECTED for role in LearnedRole
        }
        self._outgoing_lock = threading.Lock()
        self._outgoing: LearnedFrame | None = None
        self._receiver: threading.Thread | None = None
        self._stopping = False
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

    def connected(self, role: LearnedRole) -> None:
        """その役割の worker の接続を認めた。"""
        self._states[role] = LearnedChannelState.CONNECTED

    def disconnected(self, role: LearnedRole, state: LearnedChannelState) -> None:
        """その役割の接続が切れた・閉じた。**状態を先に変えてから**枠を空にする。

        ``state`` は `worker_disconnected`（EOF・送信の失敗）か `worker_idle`（黙った）。
        """
        self._states[role] = state
        if role is LearnedRole.MPC:
            with self._lock:
                self._mpc = None
                # lock を取れなかった tick に返す写しも捨てる（再接続の後に古い提案を返さない）
                self._last_polled = None

    def place_mpc(self, result: MpcProposal) -> None:
        """検証を通った MPC の結果を置く。前の結果は置き換える（積まない）。"""
        with self._lock:
            self._mpc = result

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
        if self._stopping:
            return True
        receiver = self._receiver
        return receiver is not None and receiver.is_alive()

    def state(self, role: LearnedRole) -> LearnedChannelState:
        """経路の状態（`LearnedChannelHealth`）。**lock を取らない。**"""
        if not self.receiver_alive():
            return LearnedChannelState.CHANNEL_DEAD
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
