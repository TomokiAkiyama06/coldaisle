"""Internal Telemetry の統合・保存を実機なしで検証する。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from coldaisle import logs, rollup_job
from coldaisle.clock import SimulatedClock
from coldaisle.internal_telemetry import (
    AdapterResult,
    InternalTelemetryCollector,
    InternalTelemetryConfig,
    SourceStatus,
)
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from coldaisle.telemetry_daemon import (
    SOURCE_STATE_PREFIX,
    InternalTelemetryDaemon,
    _log_configuration,
    periodic_metric_intervals,
)
from conftest import CONFIG_DIR


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


def test_startup_audit_logs_disabled_reason_and_confirmation(caplog):
    config = InternalTelemetryConfig.from_yaml(CONFIG_DIR / "internal-telemetry.yaml")

    with caplog.at_level(logging.INFO, logger="coldaisle.internal_telemetry"):
        _log_configuration(config)

    hwmon_records = [
        record for record in caplog.records if record.message == "hwmon input configuration"
    ]
    assert len(hwmon_records) == 1
    fields = getattr(hwmon_records[0], logs.FIELDS_KEY)
    assert fields["metric"] == "board.connector_12v2x6"
    assert fields["enabled"] is False
    assert fields["selector"] == "none"
    assert fields["disabled_reason"].startswith("not installed")


def test_periodic_metric_intervals_follow_the_configured_interval():
    """ロールアップへ渡す周期は設定の interval_ms と有効な入力だけから作る。"""
    config = InternalTelemetryConfig.from_yaml(CONFIG_DIR / "internal-telemetry.yaml")

    intervals = periodic_metric_intervals(config)

    assert intervals["gpu.0.core"] == config.interval_ms
    assert intervals["sys.cuda_processes"] == config.interval_ms
    # 無効な T_SENSOR には期待値を作らない（未設置は欠測ではない）
    assert "board.connector_12v2x6" not in intervals


def test_rollup_entry_point_registers_internal_metrics(tmp_path: Path, rules):
    """`coldaisle-rollup` が Internal Telemetry の周期を Store のロールアップへ渡す。"""
    database = tmp_path / "rollup.db"
    with SqliteStore(database, rules=rules, clock=SimulatedClock(0)) as store:
        for ts_ms in (0, 3 * 60_000):
            store.insert_sample(
                Sample(
                    ts_ms=ts_ms,
                    readings=(Reading(metric="gpu.0.core", value=55.0, quality=Quality.OK),),
                )
            )
    retention = tmp_path / "retention.yaml"
    retention.write_text(
        f"raw_days: 30\ncontrol_trace_days: 30\ncsv_dir: {tmp_path / 'csv'}\n", encoding="utf-8"
    )
    telemetry = tmp_path / "internal-telemetry.yaml"
    telemetry.write_text(
        "version: 1\ninterval_ms: 5000\n"
        "nvml: {enabled: true, gpu_indices: [0]}\n"
        "hwmon: {enabled: false, root: /sys/class/hwmon, sensors: []}\n",
        encoding="utf-8",
    )

    code = rollup_job.main(
        [
            f"--db={database}",
            f"--retention={retention}",
            f"--quality-rules={CONFIG_DIR / 'quality.yaml'}",
            f"--internal-telemetry={telemetry}",
        ]
    )

    assert code == 0
    with SqliteStore(database, rules=rules, clock=SimulatedClock(0)) as store:
        expected = store.connection.execute(
            "SELECT expected_count FROM readings_1m WHERE metric = 'gpu.0.core'"
        ).fetchall()
    assert [row[0] for row in expected] == [12, 12, 12, 12]
