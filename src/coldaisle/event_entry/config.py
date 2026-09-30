"""書き込み入口の設定（`config/event-entry.yaml`。決定記録 0045 §2.2）。

ソケットの場所・権限・上限をコードに持たない（AGENTS.md ルール9）。
**安全側に倒せない値は読み込みの時点で拒否する。** world-writable な権限を
「設定どおり」に作ってしまうと、第一の門（ファイル権限）が黙って外れる。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from coldaisle.event_entry.messages import IMPLEMENTED_HINT_VERSIONS, WorkloadHintLimits
from coldaisle.local_socket import SOCKET_PATH_MAX_BYTES, SocketSettings

__all__ = [
    "DEFAULT_CONFIG",
    "SOCKET_PATH_MAX_BYTES",
    "AuthorizationSettings",
    "EventEntrySettings",
    "LimitSettings",
    "SocketSettings",
    "WorkloadHintSettings",
]

DEFAULT_CONFIG = Path("config/event-entry.yaml")


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
        # 上限の値は設定だけが持つ（AGENTS.md ルール 9）。コードに天井を置かない
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
