"""``coldaisle-cpu-power`` の出力から CPU Package の消費電力を読む読み取り専用 adapter。

#278 / 決定記録 0122 §2.3。``energy_uj`` は root だけが読めるため、ここでは root の
publisher（``coldaisle.cpu_power_publisher``）が書いた 1 s 平均・1 W 単位の JSON だけを読む。
publisher とは JSON の形だけを共有し、モジュールは import しない（publisher は
``coldaisle`` パッケージに依存しない）。
"""

from __future__ import annotations

import json
from typing import Any

from coldaisle.clock import Clock
from coldaisle.internal_telemetry.config import CpuPowerConfig
from coldaisle.internal_telemetry.models import AdapterResult, SourceStatus
from coldaisle.store import Quality, Reading

CPU_POWER_SCHEMA_VERSION = 1
"""読める publisher の出力の版。``cpu_power_publisher.SCHEMA_VERSION`` と対になる。"""
_MAX_FILE_BYTES = 4 * 1024
"""publisher の出力は1行の小さな JSON。これを超えるものは読まない（形の上限。運用の値ではない）。"""


class CpuPowerAdapter:
    """publisher の JSON を poll ごとに読み、``power.cpu.package``（W）にする。

    - 読めない・JSON が壊れている・版が違う・``watts`` / ``ts_ms`` が整数でない・負 → ``missing``
    - ``ts_ms`` が collector の時計より ``max_future_skew_ms`` を超えて未来 → ``missing``
    - ``ts_ms`` が collector の時計より ``stale_after_ms`` を超えて古い → ``stale``
      （値は残す。publisher が止まってもファイルは残るため、古さで見分ける）
    - それ以外 → ``ok``
    """

    name = "cpu_power"

    def __init__(self, config: CpuPowerConfig, *, clock: Clock) -> None:
        self._config = config
        self._clock = clock

    @property
    def expected_metrics(self) -> tuple[str, ...]:
        return (self._config.metric,) if self._config.enabled else ()

    def poll(self) -> AdapterResult:
        """出力ファイルを1回読む。書き込みは行わない。"""
        if not self._config.enabled:
            return AdapterResult(source=self.name, status=SourceStatus.DISABLED)
        try:
            with self._config.path.open("rb") as handle:
                payload = handle.read(_MAX_FILE_BYTES + 1)
        except OSError as error:
            return self._unavailable(type(error).__name__)
        if len(payload) > _MAX_FILE_BYTES:
            return self._unavailable("file_too_large")
        try:
            loaded: Any = json.loads(payload.decode("utf-8"))
        except ValueError:
            return self._unavailable("invalid_json")
        if not isinstance(loaded, dict):
            return self._unavailable("invalid_json")
        if _as_int(loaded.get("schema_version")) != CPU_POWER_SCHEMA_VERSION:
            return self._unavailable("unsupported_schema_version")
        watts = _as_int(loaded.get("watts"))
        ts_ms = _as_int(loaded.get("ts_ms"))
        if watts is None or watts < 0:
            return self._unavailable("invalid_watts")
        if ts_ms is None:
            return self._unavailable("invalid_ts_ms")
        age_ms = self._clock.now_ms() - ts_ms
        if -age_ms > self._config.max_future_skew_ms:
            # 時計の巻き戻しの後、止まった値を新しいまま扱い続けないため（0122 §2.3）
            return self._unavailable("future_ts_ms")
        if age_ms > self._config.stale_after_ms:
            return AdapterResult(
                source=self.name,
                status=SourceStatus.DEGRADED,
                readings=(
                    Reading(metric=self._config.metric, value=float(watts), quality=Quality.STALE),
                ),
                detail="stale",
            )
        return AdapterResult(
            source=self.name,
            status=SourceStatus.OK,
            readings=(Reading(metric=self._config.metric, value=float(watts), quality=Quality.OK),),
        )

    def _unavailable(self, detail: str) -> AdapterResult:
        return AdapterResult(
            source=self.name,
            status=SourceStatus.UNAVAILABLE,
            readings=(Reading(metric=self._config.metric, value=None, quality=Quality.MISSING),),
            detail=detail,
        )

    def close(self) -> None:
        """poll ごとに開くため、保持 resource はない。"""


def _as_int(value: object) -> int | None:
    """JSON の整数だけを返す。小数・文字列・真偽値は ``None``。"""
    # JSON の true / false は Python で int の部分型になるため除く
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
