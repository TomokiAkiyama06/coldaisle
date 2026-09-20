"""Internal Telemetry の読み取り専用 adapter 契約。#65

adapter は観測値を ``Reading`` に変換するだけで、保存・制御・hwmon への書き込みを
行わない。取得不能値は 0 に置き換えず ``missing`` として返す。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from coldaisle.store import Reading

SOURCE_STATE_PREFIX = "sys.telemetry_source."
"""Internal Telemetry source の現在状態を保存する ``system_state`` key prefix。"""

TELEMETRY_KIND_KEY = "sys.telemetry_kind"
"""値の出どころの種類を保存する ``system_state`` key（決定記録 0049 §2.3）。

``SOURCE_STATE_PREFIX`` の下には置かない。あちらは adapter 名ごとの**稼働状態**の
名前空間で、Server Health（決定記録 0040）が読んでいる。混ぜると
「adapter 名 = 種類」と区別が付かなくなる。
"""


class TelemetrySourceKind(StrEnum):
    """内部テレメトリの値の出どころの種類（決定記録 0049 §2.2）。

    運用で変える値ではなく実装の識別なので、設定ファイルには出さない。
    ``replay`` は内部テレメトリを再生する経路が無いため、今は定義しない。
    """

    HARDWARE = "hardware"
    """実 adapter が、設定された OS の読み取り口（NVML / hwmon / ``/proc/stat``）を読んでいる。

    **センサーと物理部位の対応が正しいことまでは表さない**（それは hwmon の
    ``confirmation``。決定記録 0029 §2.4）。
    """

    MOCK = "mock"
    """差し込まれた模擬の adapter による読み取り。"""


class SourceStatus(StrEnum):
    """1回の poll における情報源の状態。"""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class AdapterResult(BaseModel):
    """adapter 1つの immutable な poll 結果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    status: SourceStatus
    readings: tuple[Reading, ...] = ()
    detail: str | None = None


class TelemetryAdapter(Protocol):
    """実機 adapter と fake が共有する読み取り専用境界。"""

    @property
    def name(self) -> str:
        """source health に使う安定名。"""
        ...

    @property
    def expected_metrics(self) -> tuple[str, ...]:
        """source 全体の失敗時にも ``missing`` を作る対象。"""
        ...

    def poll(self) -> AdapterResult:
        """現在値を1回読む。write や actuation は行わない。"""
        ...

    def close(self) -> None:
        """adapter が保持する読み取り専用 resource を解放する。"""
        ...
