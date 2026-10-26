"""Learned worker との経路の設定（`config/learned-channel.yaml`。決定記録 0077 §2.7）。

**制御の閾値ではなく経路の形だけを持つ。** `safety.yaml` には置かず、Control Config の4ファイルの束
（0073）にも入れない（束ねた版 `CONTROL_CONFIG_VERSION` は変えない）。

値はすべてここに置き、コードに既定値を置かない（AGENTS.md ルール9）。起動時に1回だけ読み、
不正なら**経路を開かず**、`coldaisle-fand` は Learned を使わずに（Fallback / RulePolicy で）
運転を続ける（冷却を入口の有無に依存させない。0077 §2.7）。
"""

from __future__ import annotations

import stat
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
        if not self.mode_bits & stat.S_IWGRP:
            # worker は別の uid で、同じ uid は拒む。グループが書けなければ誰も接続できない
            raise ValueError(f"Learned のソケットの mode はグループが書ける値にする: {self.mode}")
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
    max_shadow_record_bytes: ProvisionalBytes | None = None
    """Workload Hint の B1 の Shadow の記録1件の符号化後の上限（決定記録 0133 §2.5）。省ける。

    ``max_message_bytes`` 以下。超えた記録は切り詰めずに捨てる（worker は
    ``shadow_record_too_large`` に数える。fand の受信側も同じ上限で検査する）。
    ``packet_overhead_bytes`` と組で置く。
    """
    packet_overhead_bytes: ProvisionalBytes | None = None
    """1メッセージあたりのカーネルの管理領域の見積もり（決定記録 0133 §2.5。実測で決める）。

    worker は自分のソケットの ``SO_SNDBUF`` を、ヒントを扱わないときの値に
    ``max_shadow_record_bytes + packet_overhead_bytes`` を**足した**値にする（基準の提案と
    heartbeat の空きを減らさない）。``max_shadow_record_bytes`` と組で置く。
    """

    @model_validator(mode="after")
    def _shadow_record_limits_come_together(self) -> Self:
        record, overhead = self.max_shadow_record_bytes, self.packet_overhead_bytes
        if (record is None) != (overhead is None):
            raise ValueError("max_shadow_record_bytes と packet_overhead_bytes は組で置く")
        if record is not None and record.value > self.max_message_bytes.value:
            raise ValueError(
                "max_shadow_record_bytes は max_message_bytes 以下にする: "
                f"max_shadow_record_bytes={record.value}; "
                f"max_message_bytes={self.max_message_bytes.value}"
            )
        return self


class ProvisionalCount(_Strict):
    """`status` / `basis` 付きの件数（0077 §5「実測を待つ値」と同じ形）。"""

    value: int = Field(ge=1)
    status: Literal["provisional", "confirmed"]
    basis: str = Field(min_length=1)


class HintDomainSettings(_Strict):
    """Workload Hint の B1 で fand が行う認証領域の判定の値（決定記録 0162 §2.2）。

    fand が読み、frame v4 の ``hint_certification`` に載せて MPC worker へ渡す（worker は自分の
    設定ではなく frame の値で比べる）。**証明書には束ねない**（解の結果に効かず、判定を厳しくする
    向きにしか働かない。0162 §2.2 / §5 の 2）。
    """

    domain_lookback_ms: ProvisionalMs
    """fand が判定する区間の長さ。

    artifact の ``window_ms`` がこれ**以下**でなければ worker は解かない。
    """
    domain_stale_after_ms: ProvisionalMs
    """fand の古さの閾値。

    artifact の ``stale_after_ms`` がこれ**以上**でなければ worker は解かない。
    """
    pending_domain_result_ms: ProvisionalMs
    """送り出しの時点の判定の結果を、Shadow の記録が届くまで持つ時間（決定記録 0162 §2.2）。

    worker の Shadow の記録の期限（元の frame の次の基準の周期の開始）より長く置く。過ぎて届いた
    記録は ``source_interval_unavailable`` で範囲の外になる（危険側には倒れない）。
    """
    max_pending_domain_results: ProvisionalCount
    """保留の表の件数の上限（決定記録 0162 §2.2）。超えたら古いものから捨てる。

    同じ件数を、Shadow の記録を DB へ書く書き手の待ち行列の上限にも使う（行は保留の結果ごとに
    高々1件と、検証に落ちた記録だけ）。
    """


class HintShadowSettings(_Strict):
    """Learned MPC worker の Shadow solver の子プロセスの扱い（決定記録 0133 §2.5 / 0162 §2.5）。

    worker だけが読む。省くと worker は Shadow solver を作らない（ヒントの依頼を解かないだけで、
    基準の solve と送信は変わらない）。置くときは ``limits.max_shadow_record_bytes`` /
    ``limits.packet_overhead_bytes`` も要る。
    """

    respawn_interval_ms: ProvisionalMs
    """子が死んだ・応答しない・経路が壊れたとき、作り直すまでにあける間隔。"""
    unresponsive_after_ms: ProvisionalMs
    """依頼の打ち切り時刻からこの時間たっても結果が来ない子を「応答しない」として終わらせる。

    子は ``SCHED_IDLE`` で動くので、CPU が空かない間は遅れて当然である。短すぎると、飢えている
    だけの子を作り直し続ける。
    """


class LearnedChannelSettings(_Strict):
    """`config/learned-channel.yaml` 全体。"""

    version: Literal[1]
    sockets: RoleSockets
    limits: ChannelLimits
    heartbeat_interval_ms: ProvisionalMs
    """worker が提案・失敗を送らない間も heartbeat を送る間隔。接続を認めた最初の応答で知らせる。"""
    worker_idle_timeout_ms: ProvisionalMs
    """最後の有効なメッセージからこの時間黙った接続を閉じる（`worker_idle`）。"""
    hint_domain: HintDomainSettings | None = None
    """Workload Hint の B1 の値（決定記録 0162 §2.2）。省ける。

    省いた設定では、fand は frame にヒントの欄（``supervisor_hint`` / ``hint_certification``）を
    入れない（B1 の依頼が worker に届かないだけで、基準の経路は変わらない）。
    """

    hint_shadow: HintShadowSettings | None = None
    """Learned MPC worker の Shadow solver の値（決定記録 0133 §2.5）。

    省ける（worker だけが読む）。
    """

    @model_validator(mode="after")
    def _hint_shadow_has_its_limits(self) -> Self:
        if self.hint_shadow is not None and self.limits.max_shadow_record_bytes is None:
            raise ValueError(
                "hint_shadow を置くなら limits.max_shadow_record_bytes と "
                "limits.packet_overhead_bytes も置く"
            )
        return self

    @model_validator(mode="after")
    def _hint_domain_names_the_shadow_record_limit(self) -> Self:
        if self.hint_domain is not None and self.limits.max_shadow_record_bytes is None:
            # B1 の記録を受け取る構成で、受信側の上限を持たない構成を作らない（0133 §2.5）
            raise ValueError("hint_domain を置くなら limits.max_shadow_record_bytes も置く")
        return self

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
