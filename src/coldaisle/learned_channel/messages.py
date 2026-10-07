"""Learned worker との経路の封筒（版付きの JSON。1メッセージ = 1 `SOCK_SEQPACKET` の記録）。

決定記録 0077 §2.8。

封筒は `schema_version`・`run_id`・`role`・本文（`body`）を持つ。知らない版・知らない本文は捨てる。

- worker → `coldaisle-fand`: `heartbeat`（受け渡し口も受信時刻も変えない）と `mpc_result`
  （`MpcProposal`。提案か失敗）。**段階 1 で受け取る結果は MPC だけ。** Supervisor 出力の本文は
  `SupervisorOutputSource` の形を広げる段階 4（#89）で足す（0077 §2.8 / §2.10）
- `coldaisle-fand` → worker: `hello`（接続を認めた最初の応答。`heartbeat_interval_ms` と
  `run_id`）と `frame`（毎 tick の `LearnedFrame`）

worker からの本文は `MpcProposal` の型しか運べず、Demand の上書き・モード・authority を表す型を
持たない（0077 §2.4 の 6、AGENTS.md ルール2）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.control.learned_handoff import LearnedFrame, LearnedRole
from coldaisle.control.mpc.controller import MpcProposal

ENVELOPE_SCHEMA_VERSION: Literal[1] = 1

RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
"""`run_id` の形（`control_admin.runtime.new_run_id()` と同じ。個体識別子を含まない）。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HeartbeatBody(_Strict):
    """worker が提案・失敗を送らない間も送る生存の知らせ（0077 §2.7）。"""

    kind: Literal["heartbeat"]


class MpcResultBody(_Strict):
    """Learned MPC worker の1回の結果（提案か失敗）。"""

    kind: Literal["mpc_result"]
    result: MpcProposal


InboundBody = Annotated[HeartbeatBody | MpcResultBody, Field(discriminator="kind")]


class InboundEnvelope(_Strict):
    """worker から届く封筒。`role` は照合であって認可ではない（役割はソケットで決まる）。"""

    schema_version: Literal[1]
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    role: LearnedRole
    body: InboundBody


class HelloBody(_Strict):
    """接続を認めた最初の応答（0077 §2.7）。"""

    kind: Literal["hello"] = "hello"
    heartbeat_interval_ms: int = Field(ge=1)


class FrameBody(_Strict):
    """毎 tick の worker の入力（0077 §2.3）。"""

    kind: Literal["frame"] = "frame"
    frame: LearnedFrame


class OutboundEnvelope(_Strict):
    """`coldaisle-fand` から worker へ送る封筒。"""

    schema_version: Literal[1] = ENVELOPE_SCHEMA_VERSION
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    role: LearnedRole
    body: Annotated[HelloBody | FrameBody, Field(discriminator="kind")]


def parse_inbound(data: bytes) -> InboundEnvelope:
    """worker から届いた1メッセージを検証する。

    不正なら `ValueError`（`ValidationError` を含む）。
    """
    return InboundEnvelope.model_validate_json(data)


def encode_outbound(envelope: OutboundEnvelope) -> bytes:
    """1メッセージの bytes（UTF-8 の JSON）。"""
    return envelope.model_dump_json().encode("utf-8")
