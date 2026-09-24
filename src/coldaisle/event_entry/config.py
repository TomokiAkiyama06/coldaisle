"""書き込み入口の設定（`config/event-entry.yaml`。決定記録 0045 §2.2）。

ソケットの場所・権限・上限をコードに持たない（AGENTS.md ルール9）。
**安全側に倒せない値は読み込みの時点で拒否する。** world-writable な権限を
「設定どおり」に作ってしまうと、第一の門（ファイル権限）が黙って外れる。
"""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from coldaisle.event_entry.messages import IMPLEMENTED_HINT_VERSIONS, WorkloadHintLimits

DEFAULT_CONFIG = Path("config/event-entry.yaml")

SOCKET_PATH_MAX_BYTES = 107
"""Linux の `sun_path` は 108 バイトで、終端の NUL を含む。"""

MAX_EXPECTED_DURATION_CEILING_S = 31 * 24 * 3600
"""`limits.max_expected_duration_s` に書ける値の天井（31日）。

書き間違い（ミリ秒で書く等）で実質無期限のヒントを受理させないための型の上限であり、
運用の上限は設定ファイルの値が持つ。
"""

_MODE_PATTERN = re.compile(r"^0[0-7]{3}$")
_GROUP_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class SocketSettings(BaseModel):
    """ソケットの場所と権限。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    mode: str = Field(description='8進の文字列（例: "0660"）')
    group: str | None = None

    @field_validator("path")
    @classmethod
    def _path_fits_sun_path(cls, value: Path) -> Path:
        if not str(value):
            raise ValueError("socket.path が空")
        if len(bytes(value)) > SOCKET_PATH_MAX_BYTES:
            raise ValueError(f"socket.path が長すぎる（{SOCKET_PATH_MAX_BYTES} バイトまで）")
        return value

    @field_validator("mode")
    @classmethod
    def _mode_is_not_world_accessible(cls, value: str) -> str:
        if not _MODE_PATTERN.match(value):
            raise ValueError('socket.mode は "0660" のような4桁の8進文字列で書く')
        bits = int(value, 8)
        if bits & stat.S_IRWXO:
            # other に1ビットでも開けると、同じホストの全利用者が門の手前まで来られる
            raise ValueError(f"socket.mode に other の権限を含めない: {value}")
        if bits & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise ValueError(f"socket.mode に setuid / setgid / sticky を含めない: {value}")
        if not bits & stat.S_IWUSR:
            raise ValueError(f"socket.mode は所有者が書ける値にする: {value}")
        return value

    @field_validator("group")
    @classmethod
    def _group_is_a_plain_name(cls, value: str | None) -> str | None:
        if value is not None and not _GROUP_PATTERN.match(value):
            raise ValueError(f"socket.group の書式が不正: {value!r}")
        return value

    @property
    def mode_bits(self) -> int:
        return int(self.mode, 8)


class AuthorizationSettings(BaseModel):
    """接続ごとの認可（決定記録 0045 §2.3）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow_same_user: bool


class LimitSettings(BaseModel):
    """1接続あたりの上限。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_message_bytes: int = Field(ge=64, le=65_536)
    read_timeout_s: float = Field(gt=0, le=60)
    max_expected_duration_s: int = Field(
        ge=1,
        le=MAX_EXPECTED_DURATION_CEILING_S,
        description="Workload Hint の expected_duration_s の上限（決定記録 0064 §2.3）",
    )


class WorkloadHintSettings(BaseModel):
    """Workload Hint の受理条件（#107 / 決定記録 0064 §2.3）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted_hint_versions: tuple[int, ...]

    @field_validator("accepted_hint_versions")
    @classmethod
    def _only_versions_the_code_knows(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        # 空は「ヒントを1件も受理しない」。拒否する側なので許す
        if len(value) != len(set(value)):
            raise ValueError("workload_hint.accepted_hint_versions に重複がある")
        unknown = sorted(set(value) - IMPLEMENTED_HINT_VERSIONS)
        if unknown:
            # 形を知らない版を設定だけで受理させない（古い形の検証で新しい意味を通す）
            raise ValueError(f"workload_hint.accepted_hint_versions に未実装の版がある: {unknown}")
        return value


class EventEntrySettings(BaseModel):
    """`config/event-entry.yaml` 全体。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    socket: SocketSettings
    authorization: AuthorizationSettings
    limits: LimitSettings
    workload_hint: WorkloadHintSettings

    @property
    def hint_limits(self) -> WorkloadHintLimits:
        """`parse_message` へ渡す、設定で決まる Workload Hint の上限。"""
        return WorkloadHintLimits(
            accepted_hint_versions=frozenset(self.workload_hint.accepted_hint_versions),
            max_expected_duration_s=self.limits.max_expected_duration_s,
        )

    @classmethod
    def from_yaml(cls, path: Path) -> EventEntrySettings:
        """YAML を厳格に読む。未知のキーや危険な権限は起動前に拒否する。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"書き込み入口の設定が辞書ではない: {path}")
        return cls.model_validate(loaded)
