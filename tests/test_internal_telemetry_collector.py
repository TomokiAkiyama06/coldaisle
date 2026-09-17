"""Internal Telemetry の統合・保存を実機なしで検証する。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coldaisle.clock import SimulatedClock
from coldaisle.internal_telemetry import (
    AdapterResult,
    InternalTelemetryCollector,
    SourceStatus,
)
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from coldaisle.telemetry_daemon import SOURCE_STATE_PREFIX, InternalTelemetryDaemon


@dataclass
class FakeAdapter:
    name: str
    expected_metrics: tuple[str, ...]
    result: AdapterResult | None = None
    error: Exception | None = None
    closed: bool = False

    def poll(self) -> AdapterResult:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def close(self) -> None:
        self.closed = True


def test_collect_uses_one_host_timestamp_and_isolates_source_failure():
    clock = SimulatedClock(123_456)
    gpu = FakeAdapter(
        name="nvml",
        expected_metrics=("gpu.0.core",),
        result=AdapterResult(
            source="nvml",
            status=SourceStatus.OK,
            readings=(Reading(metric="gpu.0.core", value=55.0, quality=Quality.OK),),
        ),
    )
    board = FakeAdapter(
        name="hwmon",
        expected_metrics=("cpu.package",),
        error=RuntimeError("read failed"),
    )

    cycle = InternalTelemetryCollector((gpu, board), clock).collect()
    readings = {reading.metric: reading for reading in cycle.sample.readings}

    assert cycle.sample.ts_ms == 123_456
    assert readings["gpu.0.core"].value == 55.0
    assert readings["cpu.package"].quality is Quality.MISSING
    assert cycle.sources[1].status is SourceStatus.UNAVAILABLE
    assert cycle.sources[1].detail == "RuntimeError"


def test_daemon_adds_internal_values_to_the_existing_air_timeline(tmp_path: Path, rules):
    clock = SimulatedClock(1_000)
    store = SqliteStore(tmp_path / "timeline.db", rules=rules, clock=clock)
    store.insert_sample(
        Sample(
            ts_ms=900,
            readings=(Reading(metric="air.front_intake", value=26.0, quality=Quality.OK),),
        )
    )
    adapter = FakeAdapter(
        name="nvml",
        expected_metrics=("gpu.0.core", "power.gpu.0"),
        result=AdapterResult(
            source="nvml",
            status=SourceStatus.DEGRADED,
            readings=(
                Reading(metric="gpu.0.core", value=58.0, quality=Quality.OK),
                Reading(metric="power.gpu.0", value=None, quality=Quality.MISSING),
            ),
        ),
    )
    collector = InternalTelemetryCollector((adapter,), clock)
    daemon = InternalTelemetryDaemon(
        collector=collector,
        store=store,
        interval_ms=2_500,
        sleep=lambda _: None,
    )

    stats = daemon.run(max_cycles=1)
    latest = store.latest()

    assert stats.cycles == 1
    assert latest["air.front_intake"].ts_ms == 900
    assert latest["gpu.0.core"].ts_ms == 1_000
    assert latest["power.gpu.0"].quality is Quality.MISSING
    assert store.current_state(SOURCE_STATE_PREFIX + "nvml") == "degraded"
    assert adapter.closed
