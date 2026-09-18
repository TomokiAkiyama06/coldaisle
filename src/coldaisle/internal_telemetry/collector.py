"""複数の内部 Telemetry adapter を同じホスト時刻へ束ねる。#65"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from coldaisle.clock import Clock
from coldaisle.internal_telemetry.models import (
    AdapterResult,
    SourceStatus,
    TelemetryAdapter,
)
from coldaisle.store import Quality, Reading, Sample


class CollectionCycle(BaseModel):
    """1回の収集結果。全 Reading は ``sample.ts_ms`` を共有する。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sample: Sample
    sources: tuple[AdapterResult, ...]


class InternalTelemetryCollector:
    """adapter failure を source 単位に閉じ込める collector。"""

    def __init__(self, adapters: tuple[TelemetryAdapter, ...], clock: Clock) -> None:
        self._adapters = adapters
        self._clock = clock
        names = [adapter.name for adapter in adapters]
        if len(names) != len(set(names)):
            raise ValueError("Telemetry adapter の name は重複させない")
        metrics = [metric for adapter in adapters for metric in adapter.expected_metrics]
        if len(metrics) != len(set(metrics)):
            raise ValueError("Telemetry adapter 間で metric を重複させない")

    def collect(self) -> CollectionCycle:
        """時刻を1回だけ確定し、全 source を読み取る。"""
        ts_ms = self._clock.now_ms()
        results = tuple(self._poll(adapter) for adapter in self._adapters)
        readings = tuple(reading for result in results for reading in result.readings)
        return CollectionCycle(sample=Sample(ts_ms=ts_ms, readings=readings), sources=results)

    @staticmethod
    def _poll(adapter: TelemetryAdapter) -> AdapterResult:
        try:
            return adapter.poll()
        except Exception as error:
            return AdapterResult(
                source=adapter.name,
                status=SourceStatus.UNAVAILABLE,
                readings=tuple(
                    Reading(metric=metric, value=None, quality=Quality.MISSING)
                    for metric in adapter.expected_metrics
                ),
                detail=type(error).__name__,
            )

    def close(self) -> None:
        """全 adapter を閉じる。1つの失敗でも残りを閉じる。"""
        first_error: Exception | None = None
        for adapter in self._adapters:
            try:
                adapter.close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error
