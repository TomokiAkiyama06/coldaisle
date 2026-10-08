"""worker の側のソケット（fand の役割ごとの `SOCK_SEQPACKET` へ接続する。0077 §2.2 / §2.7）。

役割（`mpc` / `supervisor`）は接続するソケットで決まる。封筒の `role` はその役割を名乗るだけ
（fand は照合するだけで、認可は `SO_PEERCRED` とソケットのグループ）。

- 接続を認められた最初の応答（`hello`）で `run_id` と `heartbeat_interval_ms` を受け取る
- 受け取るのは `frame` だけ。**検証に通らない frame（v2 以前・`calibration` の欠けや型の違い）は
  捨てて数える**（窓にも束縛にも使わない。0101 §2.3）
- 送るのは役割の結果（MPC は `mpc_result` の `MpcProposal`、RL Supervisor は `supervisor_result` の
  `DeliveredSupervisorOutput`）と `heartbeat` だけ。Demand の上書き・モード・authority・束縛の
  用途を表す型は封筒に無い（0077 §2.4 の 6 / §2.8）
- heartbeat は推論とは別のスレッドから送る（0107 §2.7）。送信は lock で直列にする

切断（EOF・送受信の失敗）は `ChannelClosed` で知らせる。再接続はしない（プロセスの再起動は unit の
仕事。0077 §2.10 段階 6）。
"""

from __future__ import annotations

import contextlib
import logging
import select
import socket
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from coldaisle import logs
from coldaisle.control.learned_handoff import LearnedFrame, LearnedRole
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.supervisor.policy import DeliveredSupervisorOutput
from coldaisle.learned_channel.messages import (
    ENVELOPE_SCHEMA_VERSION,
    FrameBody,
    HeartbeatBody,
    HelloBody,
    InboundEnvelope,
    MpcResultBody,
    OutboundEnvelope,
    SupervisorResultBody,
)

LOGGER = logging.getLogger("coldaisle.learned_worker.client")


class ChannelClosed(Exception):
    """fand との接続が切れた（EOF・拒否・送受信の失敗）。"""


@dataclass(frozen=True, slots=True)
class Hello:
    """接続を認めた fand の応答。"""

    run_id: str
    heartbeat_interval_ms: int


@dataclass(frozen=True, slots=True)
class ReceivedFrame:
    """検証を通った frame と、それを包んだ封筒の `run_id`。"""

    run_id: str
    frame: LearnedFrame


class WorkerChannel:
    """1つの役割のソケットへの1接続。"""

    def __init__(
        self,
        sock: socket.socket,
        *,
        max_message_bytes: int,
        role: LearnedRole = LearnedRole.MPC,
    ) -> None:
        self._sock = sock
        self._max = max_message_bytes
        self._role = role
        self._send_lock = threading.Lock()
        self._dropped: Counter[str] = Counter()

    @classmethod
    def connect(
        cls, path: Path, *, max_message_bytes: int, role: LearnedRole = LearnedRole.MPC
    ) -> WorkerChannel:
        """接続する。拒否・未起動は `ChannelClosed`。"""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            sock.connect(str(path))
        except OSError as error:
            sock.close()
            raise ChannelClosed(f"接続できない: {type(error).__name__}") from error
        return cls(sock, max_message_bytes=max_message_bytes, role=role)

    @property
    def dropped(self) -> dict[str, int]:
        """捨てたメッセージの件数（理由の code ごと）。"""
        return dict(self._dropped)

    def close(self) -> None:
        """接続を閉じる。"""
        with contextlib.suppress(OSError):
            self._sock.close()

    def receive_hello(self, *, timeout_s: float) -> Hello:
        """最初の応答を待つ。来なければ・拒否されたら `ChannelClosed`。"""
        ready, _, _ = select.select([self._sock], [], [], timeout_s)
        if not ready:
            raise ChannelClosed("hello が来ない")
        envelope = self._parse(self._recv())
        if envelope is None or not isinstance(envelope.body, HelloBody):
            raise ChannelClosed("最初のメッセージが hello ではない")
        return Hello(
            run_id=envelope.run_id, heartbeat_interval_ms=envelope.body.heartbeat_interval_ms
        )

    def receive_frames(self, *, timeout_s: float) -> list[ReceivedFrame]:
        """``timeout_s`` まで待ち、届いているメッセージをすべて読む。"""
        frames: list[ReceivedFrame] = []
        wait = max(0.0, timeout_s)
        while True:
            ready, _, _ = select.select([self._sock], [], [], wait)
            if not ready:
                return frames
            wait = 0.0
            envelope = self._parse(self._recv())
            if envelope is None:
                continue
            if not isinstance(envelope.body, FrameBody):
                self._drop("unexpected_body")
                continue
            frames.append(ReceivedFrame(run_id=envelope.run_id, frame=envelope.body.frame))

    @property
    def role(self) -> LearnedRole:
        """この接続の役割。"""
        return self._role

    def send_result(self, run_id: str, result: MpcProposal) -> None:
        """MPC の1回の結果を送る（提案か失敗）。"""
        if self._role is not LearnedRole.MPC:
            raise ValueError("MPC の結果は MPC の役割の接続からだけ送る")
        self._send(
            InboundEnvelope(
                schema_version=ENVELOPE_SCHEMA_VERSION,
                run_id=run_id,
                role=LearnedRole.MPC,
                body=MpcResultBody(kind="mpc_result", result=result),
            )
        )

    def send_supervisor_result(self, run_id: str, result: DeliveredSupervisorOutput) -> None:
        """RL Supervisor の1回の出力を送る（0077 §2.8。失敗の本文は無い）。"""
        if self._role is not LearnedRole.SUPERVISOR:
            raise ValueError("Supervisor の出力は supervisor の役割の接続からだけ送る")
        self._send(
            InboundEnvelope(
                schema_version=ENVELOPE_SCHEMA_VERSION,
                run_id=run_id,
                role=LearnedRole.SUPERVISOR,
                body=SupervisorResultBody(kind="supervisor_result", result=result),
            )
        )

    def send_heartbeat(self, run_id: str) -> None:
        """生存の知らせ（受け渡し口も受信時刻も変えない。0077 §2.7）。"""
        self._send(
            InboundEnvelope(
                schema_version=ENVELOPE_SCHEMA_VERSION,
                run_id=run_id,
                role=self._role,
                body=HeartbeatBody(kind="heartbeat"),
            )
        )

    # ------------------------------------------------------------------ 内部

    def _recv(self) -> bytes:
        try:
            data, _, flags, _ = self._sock.recvmsg(self._max + 1)
        except OSError as error:
            raise ChannelClosed(f"受信に失敗: {type(error).__name__}") from error
        if not data and not flags & socket.MSG_TRUNC:
            raise ChannelClosed("fand が接続を閉じた")
        if len(data) > self._max or flags & socket.MSG_TRUNC:
            self._drop("too_long")
            return b""
        return data

    def _parse(self, data: bytes) -> OutboundEnvelope | None:
        if not data:
            return None
        try:
            envelope = OutboundEnvelope.model_validate_json(data)
        except (ValidationError, ValueError):
            # v2 以前の frame・calibration の欠けた / 型の違う frame もここで落ちる（0101 §2.3）
            self._drop("invalid")
            return None
        if envelope.role is not self._role:
            self._drop("role_mismatch")
            return None
        return envelope

    def _send(self, envelope: InboundEnvelope) -> None:
        data = envelope.model_dump_json().encode("utf-8")
        if len(data) > self._max:
            self._drop("result_too_long")
            return
        with self._send_lock:
            try:
                self._sock.send(data)
            except OSError as error:
                raise ChannelClosed(f"送信に失敗: {type(error).__name__}") from error

    def _drop(self, code: str) -> None:
        self._dropped[code] += 1
        count = self._dropped[code]
        if count & (count - 1) == 0:
            LOGGER.warning(
                "fand からのメッセージを捨てた",
                extra={logs.FIELDS_KEY: {"code": code, "count": count}},
            )
