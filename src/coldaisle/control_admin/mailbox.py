"""受付スレッドと loop の受け渡し口（mailbox。決定記録 0072 §2.2）。

- **状態の軸ごとに1枠。** モードの枠（段階 1 の #74）と authority の枠（段階 2 の #92）。
  **別の軸の指令は互いに消さない**（`MAX` のあとの rollback は両方が同じ tick で効く）
- モードの枠は **`command_id` が最大の1件**を採る。これまでに置いた最大の `command_id` を
  覚え（loop が取り出したあとも保つ）、それより小さい指令は置かずに `superseded` とする。
  安全側の `MAX` は監査を待たずに置き、弱めうる指令は監査を書けてから置くので、先に届いた
  弱めうる指令が後から届いて先に置かれた `MAX` を上書きしうるためである
- authority の枠は置き換えずに**合成する。** `lower_authority` と `rollback_authority` はどちらも
  下げる向きなので、届いた指令のうち**最も低い行き先**を採る（後から来た浅い降格が先の rollback を
  打ち消さない）。同じ深さなら先に置いた指令を残す。採らなかった指令は `superseded`
- loop は **非ブロッキングで**覗き、2枠を**同じ lock の中で**取り出す（`take`）。lock が取れなければ
  その tick は直前のモード・stage のまま
- loop が返す結果（適用・lease 切れ）は lock の要らない FIFO に積み、受付スレッドを起こす
- 受付スレッドの生存は **lock を取らずに**答える（`receiver_alive`）

loop の状態（`ControlLoop` / `ControllerGate` / `AuthorityRuntime`）には触れない。
"""

from __future__ import annotations

import queue
import socket
import threading
from dataclasses import dataclass

from coldaisle.control.operating_mode import (
    AdminAuthorityCommand,
    AdminModeCommand,
    MailboxTake,
    ModeOutcome,
    ModeStatus,
)


@dataclass(frozen=True, slots=True)
class Placed:
    """枠へ置けた。``replaced`` は取り出される前に置き換えた指令（採らなかった側）。"""

    replaced: AdminModeCommand | AdminAuthorityCommand | None


@dataclass(frozen=True, slots=True)
class Superseded:
    """置かなかった。``by`` はすでに枠にある、採られた指令の `command_id`。

    モードの枠では、より新しい指令。authority の枠では、同じかより低い行き先の指令
    （先に届いた指令のこともある。0072 §2.2 の合成）。
    """

    by: int


class AdminMailbox:
    """モードの枠と authority の枠、loop からの結果の FIFO、`status` の写し。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode: AdminModeCommand | None = None
        self._authority: AdminAuthorityCommand | None = None
        self._max_mode_command_id = 0
        self._outcomes: queue.SimpleQueue[ModeOutcome] = queue.SimpleQueue()
        self._status: ModeStatus | None = None
        self._receiver: threading.Thread | None = None
        self._stopping = False
        # loop・監査スレッドから受付スレッドを起こす（selectors で待っている）。
        # 書き込みは非ブロッキングで、詰まっていれば捨てる（起こす必要はもう満たされている）
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
        """`coldaisle-fand` 自身の停止の手順で受付スレッドを止める（死んだとは扱わない）。

        loop が止まった後に呼ぶ（0072 §2.2）。
        """
        self._stopping = True

    def place_mode(self, command: AdminModeCommand) -> Placed | Superseded:
        """モードの枠へ置く。**`command_id` が最大の1件**だけを残す（0072 §2.2）。"""
        with self._lock:
            if command.command_id <= self._max_mode_command_id:
                return Superseded(by=self._max_mode_command_id)
            replaced = self._mode
            self._mode = command
            self._max_mode_command_id = command.command_id
            return Placed(replaced=replaced)

    def place_authority(self, command: AdminAuthorityCommand) -> Placed | Superseded:
        """authority の枠へ置く。**最も低い行き先**の1件を残す（0072 §2.2）。

        受付の時点の実効 stage とは比べない（適用するかは loop が決める。0072 §2.3）。
        """
        with self._lock:
            current = self._authority
            if current is not None and not command.deeper_than(current):
                return Superseded(by=current.command_id)
            self._authority = command
            return Placed(replaced=current)

    def drain_outcomes(self) -> list[ModeOutcome]:
        """loop が返した結果を、返された順に取り出す。"""
        drained: list[ModeOutcome] = []
        while True:
            try:
                drained.append(self._outcomes.get_nowait())
            except queue.Empty:
                return drained

    def latest_status(self) -> ModeStatus | None:
        """loop が最後に置いた写し。まだ1 tick も回っていなければ None。"""
        return self._status

    def wake(self) -> None:
        """受付スレッドを起こす。**待たない。**"""
        try:
            self._wake_writer.send(b"\0")
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            # 閉じた後。起こす相手がいない
            pass

    def close(self) -> None:
        """起こすための socket を閉じる。"""
        self._wake_reader.close()
        self._wake_writer.close()

    # ---------------------------------------------------------------- loop の側（待たない）

    def receiver_alive(self) -> bool:
        """受付スレッドが生きているか。**lock を取らない。**

        まだ受付スレッドを渡されていない mailbox は「生きていない」と答える（安全側）。
        停止の手順の途中は、死んだとは扱わない。
        """
        if self._stopping:
            return True
        receiver = self._receiver
        return receiver is not None and receiver.is_alive()

    def take(self) -> MailboxTake | None:
        """2枠を**同じ lock の中で**取り出す。lock を取れなければ None（直前のまま）。"""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            taken = MailboxTake(mode=self._mode, authority=self._authority)
            self._mode = None
            self._authority = None
            return taken
        finally:
            self._lock.release()

    def take_mode(self) -> AdminModeCommand | None:
        """モードの枠だけを取り出す（authority の枠は残す）。lock を取れなければ None。"""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            taken, self._mode = self._mode, None
            return taken
        finally:
            self._lock.release()

    def report(self, outcome: ModeOutcome) -> None:
        """適用・lease 切れを返す。**待たない**（`SimpleQueue.put` は待たない）。"""
        self._outcomes.put(outcome)
        self.wake()

    def publish(self, status: ModeStatus) -> None:
        """`status` の写しを置く（参照の差し替えだけ）。"""
        self._status = status
