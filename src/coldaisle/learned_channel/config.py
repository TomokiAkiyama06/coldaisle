"""Learned worker との経路の設定（`config/learned-channel.yaml`。決定記録 0077 §2.7）。

**制御の閾値ではなく経路の形だけを持つ。** `safety.yaml` には置かず、Control Config の4ファイルの束
（0073）にも入れない（束ねた版 `CONTROL_CONFIG_VERSION` は変えない）。

値はすべてここに置き、コードに既定値を置かない（AGENTS.md ルール9）。起動時に1回だけ読み、
不正なら**経路を開かず**、`coldaisle-fand` は Learned を使わずに（Fallback / RulePolicy で）
運転を続ける（冷却を入口の有無に依存させない。0077 §2.7）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.learned_handoff import LearnedRole
from coldaisle.local_socket import SocketSettings

CONFIG_VERSION = 1
"""`config/learned-channel.yaml` の版。"""


class LearnedChannelConfigError(ValueError):
    """経路の設定が不正（0077 §2.7）。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProvisionalMs(_Strict):
    """`status` / `basis` 付きの値（0077 §5「実測を待つ値」）。"""

    value: int = Field(ge=1)
    status: Literal["provisional", "confirmed"]
    basis: str = Field(min_length=1)


class ProvisionalBytes(_Strict):
    """`status` / `basis` 付きのバイト数（0077 §5「実測を待つ値」）。"""

    value: int = Field(ge=256)
    status: Literal["provisional", "confirmed"]
    basis: str = Field(min_length=1)


class RoleSocket(SocketSettings):
    """役割ごとのソケット。**役割の専用グループが必須**（0077 §2.7）。

    同じ uid を認める開発用の設定は無い。
    """

    @model_validator(mode="after")
    def _a_role_names_its_group(self) -> Self:
        if self.group is None:
            raise ValueError("Learned のソケットには役割の専用グループ（group）を必ず設定する")
        return self


class RoleSockets(_Strict):
    """`mpc` と `supervisor` のソケット。**path もグループも重ねない**（0077 §2.7）。"""

    mpc: RoleSocket
    supervisor: RoleSocket

    @model_validator(mode="after")
    def _roles_do_not_share_a_socket_or_a_group(self) -> Self:
        if self.mpc.path == self.supervisor.path:
            raise ValueError("mpc と supervisor のソケットは別の path にする")
        if self.mpc.group == self.supervisor.group:
            raise ValueError("mpc と supervisor のグループは別のグループにする")
        return self

    def for_role(self, role: LearnedRole) -> RoleSocket:
        """役割のソケットの設定。"""
        return self.mpc if role is LearnedRole.MPC else self.supervisor


class ChannelLimits(_Strict):
    """受付スレッドの上限（0077 §2.7）。"""

    max_message_bytes: ProvisionalBytes
    """1メッセージ（両方向）の上限バイト数。超えたものは受け取らず・送らずに捨てて数える。"""
    listen_backlog: int = Field(ge=1)
    """`listen()` の待ち行列。**同時に持つ接続は役割ごとに1つ**で、これはその手前の待ち行列。"""


class LearnedChannelSettings(_Strict):
    """`config/learned-channel.yaml` 全体。"""

    version: Literal[1]
    sockets: RoleSockets
    limits: ChannelLimits
    heartbeat_interval_ms: ProvisionalMs
    """worker が提案・失敗を送らない間も heartbeat を送る間隔。接続を認めた最初の応答で知らせる。"""
    worker_idle_timeout_ms: ProvisionalMs
    """最後の有効なメッセージからこの時間黙った接続を閉じる（`worker_idle`）。"""

    @model_validator(mode="after")
    def _idle_covers_three_heartbeats(self) -> Self:
        # 正当に黙っている worker と固まった worker を区別する（0077 §2.7 / §5 の決定 13）
        required = 3 * self.heartbeat_interval_ms.value
        if self.worker_idle_timeout_ms.value < required:
            raise ValueError(
                "worker_idle_timeout_ms は heartbeat_interval_ms の3倍以上にする: "
                f"worker_idle_timeout_ms={self.worker_idle_timeout_ms.value}; required>={required}"
            )
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> LearnedChannelSettings:
        """YAML を厳格に読む。未知のキーや危険な権限は起動前に拒否する。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise LearnedChannelConfigError(f"Learned の経路の設定が辞書ではない: {path}")
        return cls.model_validate(loaded)
