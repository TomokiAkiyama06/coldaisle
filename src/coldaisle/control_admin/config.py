"""管理ソケットの設定（`config/control-admin.yaml`。決定記録 0072 §2.8）。

**制御の閾値ではなく入口の形だけを持つ。** 制御の4ファイル（0028 §2.8 / 0073）には入れない。
`safety.yaml` の schema を変えると 0028 §2.9 の承認点 2 に当たるためである。

値はすべてここに置き、コードに既定値を置かない（AGENTS.md ルール9）。起動時に1回だけ読み、
不正なら**管理ソケットを開かず**、`coldaisle-fand` は `AUTO` と journal の stage で運転を続ける
（入口の設定が壊れているだけで Max を書いたり制御を手放したりしない）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.local_socket import SocketSettings

DEFAULT_CONFIG = Path("config/control-admin.yaml")

CONFIG_VERSION = 2
"""`config/control-admin.yaml` の版。

- v2（#74 / 決定記録 0076 §2.7）: `accept_backoff` を必須にした。v1 は補わずに拒否する
  （`air-balance.yaml` の v1 と同じ扱い。入口を開かず AUTO で運転を続ける）
"""


class ControlAdminConfigError(ValueError):
    """管理ソケットの設定が不正、または `safety.yaml` と両立しない（0072 §2.8）。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AdminAuthorization(_Strict):
    """接続ごとの認可（0072 §2.5）。**同じ uid を既定で認めない。**"""

    allow_same_user: bool


class AdminLimits(_Strict):
    """受付スレッドの上限（0072 §2.2 / §2.7）。"""

    max_message_bytes: int = Field(ge=64, le=65_536)
    """1行（1要求）の上限バイト数。"""
    read_timeout_s: float = Field(gt=0)
    """受信中の接続が1行を送り切るまでの上限秒数。`tick_ms` 以下（起動時に照合する）。"""
    max_connections: int = Field(ge=1)
    """同時に持つ**受信中**の接続の上限。埋まれば最も長く受信を続けている接続を閉じる。"""
    max_pending_commands: int = Field(ge=1)
    """要求を読み終えた後（監査の書き込み待ち・適用の確認待ち・応答の送信中）の接続の上限。"""
    audit_queue_max: int = Field(ge=1)
    """監査書き込みスレッドの FIFO の上限。溢れた依頼は書き込みの失敗として扱う。"""


class ProvisionalSeconds(_Strict):
    """`status` / `basis` 付きの暫定値（0072 §2.8 / §5 の 4）。"""

    value: int = Field(ge=1)
    status: Literal["provisional", "confirmed"]
    basis: str = Field(min_length=1)


class AcceptBackoff(_Strict):
    """`accept()` が失敗し続けるときに待ち受けを休む間隔と、`MAX` へ上げるまでの時間（#74）。

    EMFILE / ENFILE / ECONNABORTED などで `accept()` が失敗しても待ち受けのソケットは読める状態の
    ままなので、休まずに監視し続けると受付スレッドが空回りしてログを溢れさせる。失敗のたびに
    `initial_ms` から `multiplier` 倍にして `max_ms` で頭打ちにし、成功したら戻す。

    途切れずに失敗し続けた時間が `escalate_after_ms` に届いたら、受付スレッドを終わらせる。
    loop は毎 tick の生存の確認でそれを見て、既存の「受付スレッドの死」の経路で再起動まで
    `MAX` にする（決定記録 0076 §2.7。所有者の判断 2026-09-30）。値はすべて `status` /
    `basis` 付きの暫定値。
    """

    initial_ms: int = Field(ge=1)
    """最初の失敗のあとに待ち受けを休む時間（ミリ秒）。"""
    max_ms: int = Field(ge=1)
    """休む時間の上限（ミリ秒）。`initial_ms` 以上、`tick_ms` 以下（起動時に照合する）。"""
    multiplier: float = Field(gt=1, le=16, allow_inf_nan=False)
    """失敗のたびに休む時間を何倍にするか。1 より大きく 16 以下の有限の値。

    上限は検証の規則（inf / nan / 桁外れの値を起動時に拒否する）。休む長さは `max_ms` で
    頭打ちなので、16 倍を超える倍率に実用上の意味は無く、設定の誤りと見なす。
    """
    escalate_after_ms: int = Field(ge=1)
    """途切れずに失敗し続けたら受付スレッドを終わらせる（→ `MAX`）までの時間。`max_ms` より長い。"""
    status: Literal["provisional", "confirmed"]
    basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def _durations_are_ordered(self) -> Self:
        if self.max_ms < self.initial_ms:
            raise ValueError(
                "accept_backoff.max_ms は accept_backoff.initial_ms 以上にする: "
                f"initial_ms={self.initial_ms}; max_ms={self.max_ms}"
            )
        if self.escalate_after_ms <= self.max_ms:
            raise ValueError(
                "accept_backoff.escalate_after_ms は accept_backoff.max_ms より長くする: "
                f"max_ms={self.max_ms}; escalate_after_ms={self.escalate_after_ms}"
            )
        return self


class ManualSettings(_Strict):
    """`MANUAL` の期限（0072 §2.4）。"""

    max_lease_s: ProvisionalSeconds


class ControlAdminSettings(_Strict):
    """`config/control-admin.yaml` 全体。"""

    version: Literal[2]
    socket: SocketSettings
    authorization: AdminAuthorization
    limits: AdminLimits
    apply_ack_timeout_ms: int = Field(ge=1)
    """受付スレッドが適用の確認を待つ上限。`tick_ms + tick_deadline_ms` 以上（起動時に照合）。"""
    manual: ManualSettings
    accept_backoff: AcceptBackoff

    @model_validator(mode="before")
    @classmethod
    def _the_version_is_current(cls, data: object) -> object:
        # 古い版を黙って補わない（accept_backoff を既定値で埋めると、コードに既定値を置くのと同じ）
        if isinstance(data, dict) and "version" in data and data["version"] != CONFIG_VERSION:
            raise ValueError(
                f"control-admin の設定の version が違う: version={data['version']!r}; "
                f"expected={CONFIG_VERSION}。v1 からは accept_backoff を足して version: 2 にする"
                "（docs/control-admin.md）"
            )
        return data

    @model_validator(mode="after")
    def _a_production_entry_names_its_group(self) -> Self:
        # 本番は専用グループが必須。group: null は allow_same_user: true を明示した
        # 開発用の起動だけ。同じ uid を通すのは group: null のときだけ（0072 §2.5 / §2.8）
        if self.socket.group is None and not self.authorization.allow_same_user:
            raise ValueError(
                "socket.group: null は authorization.allow_same_user: true の開発用の起動だけに使う"
            )
        if self.socket.group is not None and self.authorization.allow_same_user:
            raise ValueError(
                "authorization.allow_same_user: true は socket.group: null の開発用の起動だけに使う"
            )
        return self

    def check_against(self, *, tick_ms: int, tick_deadline_ms: int) -> None:
        """`safety.yaml` の周期と締め切りに照らして、入口の約束が成り立つかを確かめる（0072 §2.8）。

        - `read_timeout_s * 1000 <= tick_ms`: 受信中の接続が1 tick を超えて枠を占めない（§2.2）
        - `apply_ack_timeout_ms >= tick_ms + tick_deadline_ms`: 次の tick の先頭で入る指令の
          適用の確認を待てる（§2.6）
        - `accept_backoff.max_ms <= tick_ms`: `accept()` の失敗で休んでいる間に届いた新しい接続
          （`MAX` を含む）を、1 tick を超えて待たせない（#74）
        """
        if self.limits.read_timeout_s * 1_000 > tick_ms:
            raise ControlAdminConfigError(
                "limits.read_timeout_s * 1000 は safety.yaml の tick_ms 以下にする: "
                f"read_timeout_s={self.limits.read_timeout_s:g}; tick_ms={tick_ms}"
            )
        required = tick_ms + tick_deadline_ms
        if self.apply_ack_timeout_ms < required:
            raise ControlAdminConfigError(
                "apply_ack_timeout_ms は safety.yaml の tick_ms + tick_deadline_ms 以上にする: "
                f"apply_ack_timeout_ms={self.apply_ack_timeout_ms}; required>={required}"
            )
        if self.accept_backoff.max_ms > tick_ms:
            raise ControlAdminConfigError(
                "accept_backoff.max_ms は safety.yaml の tick_ms 以下にする: "
                f"max_ms={self.accept_backoff.max_ms}; tick_ms={tick_ms}"
            )

    @classmethod
    def from_yaml(cls, path: Path) -> ControlAdminSettings:
        """YAML を厳格に読む。未知のキーや危険な権限は起動前に拒否する。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ControlAdminConfigError(f"管理ソケットの設定が辞書ではない: {path}")
        return cls.model_validate(loaded)
