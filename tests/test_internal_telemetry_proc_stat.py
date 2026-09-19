"""/proc/stat adapter を一時ファイルで検証する。実機の /proc は読まない。"""

from __future__ import annotations

from pathlib import Path

import pytest

from coldaisle.internal_telemetry import (
    CPU_UTILIZATION_METRIC,
    CpuTimes,
    InternalTelemetryConfig,
    ProcStatAdapter,
    ProcStatConfig,
    SourceStatus,
    parse_cpu_times,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store import Quality
from coldaisle.telemetry_daemon import periodic_metric_intervals
from conftest import CONFIG_DIR


def stat(user: int, nice: int, system: int, idle: int, iowait: int, *rest: int) -> str:
    # 集計行のあとに CPU ごとの行と他の行が続く、実際の /proc/stat と同じ形
    tail = " ".join(str(value) for value in (rest or (0, 0, 0, 0, 0, 0)))
    return (
        f"cpu  {user} {nice} {system} {idle} {iowait} {tail}\n"
        "cpu0 1 2 3 4 5 0 0 0 0 0\n"
        "intr 12345 0 0\nctxt 999\nbtime 1700000000\n"
    )


def adapter(path: Path, *, enabled: bool = True, platform: str = "linux") -> ProcStatAdapter:
    return ProcStatAdapter(ProcStatConfig(enabled=enabled, path=path), platform=platform)


def value_of(result) -> tuple[float | None, Quality]:
    (reading,) = result.readings
    assert reading.metric == CPU_UTILIZATION_METRIC
    return reading.value, reading.quality


def test_parse_counts_iowait_as_idle_and_ignores_guest_columns():
    # guest / guest_nice（末尾2列）は user / nice に含まれているので足さない
    times = parse_cpu_times(stat(100, 10, 50, 800, 40, 5, 5, 0, 999, 999))

    assert times == CpuTimes(total=100 + 10 + 50 + 800 + 40 + 5 + 5 + 0, idle=840)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "intr 1 2\n",
        "cpu  1 2 3\n",
        "cpu  1 2 3 4 5\n",
        "cpu  1 2 3 4 5 6 7\n",
        "cpu  a b c d e f g h\n",
        "cpu  1 2 3 -4 5 0 0 0\n",
    ],
    ids=[
        "empty",
        "no-cpu-line",
        "too-few-columns",
        "truncated-5-columns",
        "truncated-7-columns",
        "not-numbers",
        "negative",
    ],
)
def test_parse_rejects_malformed_content(text: str):
    with pytest.raises(ValueError):
        parse_cpu_times(text)


def test_parse_accepts_exactly_eight_columns():
    # 古い kernel は guest 列を出さない。8列あれば 0047 の total が作れる
    times = parse_cpu_times("cpu  100 10 50 800 40 5 5 0\n")

    assert times == CpuTimes(total=1010, idle=840)


def test_truncated_cpu_line_is_a_read_failure(tmp_path: Path):
    path = tmp_path / "stat"
    path.write_text(stat(100, 0, 0, 900, 0), encoding="utf-8")
    source = adapter(path)
    source.poll()
    path.write_text("cpu  200 0 0 1800 0 0 0\n", encoding="utf-8")

    result = source.poll()

    assert result.status is SourceStatus.UNAVAILABLE
    assert result.detail == "ValueError"
    assert value_of(result) == (None, Quality.MISSING)


def test_first_poll_is_missing_not_zero(tmp_path: Path):
    path = tmp_path / "stat"
    path.write_text(stat(100, 0, 100, 800, 0), encoding="utf-8")

    result = adapter(path).poll()

    assert value_of(result) == (None, Quality.MISSING)
    assert result.status is SourceStatus.OK
    assert result.detail == "no_previous_sample"


def test_second_poll_is_the_busy_share_of_the_delta(tmp_path: Path):
    path = tmp_path / "stat"
    path.write_text(stat(100, 0, 100, 800, 0), encoding="utf-8")
    reader = adapter(path)
    reader.poll()
    # 差分: busy = 30 + 10 + 20 = 60、idle = 30 + 10 = 40 → 60 %
    path.write_text(stat(130, 10, 120, 830, 10), encoding="utf-8")

    result = reader.poll()

    assert value_of(result) == (pytest.approx(60.0), Quality.OK)
    assert result.status is SourceStatus.OK


def test_counter_that_does_not_advance_is_missing(tmp_path: Path):
    path = tmp_path / "stat"
    path.write_text(stat(100, 0, 100, 800, 0), encoding="utf-8")
    reader = adapter(path)
    reader.poll()

    assert value_of(reader.poll()) == (None, Quality.MISSING)


def test_counter_going_backwards_is_missing_then_recovers(tmp_path: Path):
    path = tmp_path / "stat"
    path.write_text(stat(500, 0, 500, 5000, 0), encoding="utf-8")
    reader = adapter(path)
    reader.poll()
    path.write_text(stat(100, 0, 100, 800, 0), encoding="utf-8")
    assert value_of(reader.poll()) == (None, Quality.MISSING)

    path.write_text(stat(150, 0, 100, 850, 0), encoding="utf-8")

    assert value_of(reader.poll()) == (pytest.approx(50.0), Quality.OK)


def test_read_failure_is_unavailable_and_does_not_bridge_the_gap(tmp_path: Path):
    path = tmp_path / "stat"
    path.write_text(stat(100, 0, 100, 800, 0), encoding="utf-8")
    reader = adapter(path)
    reader.poll()
    path.unlink()

    failed = reader.poll()
    assert failed.status is SourceStatus.UNAVAILABLE
    assert value_of(failed) == (None, Quality.MISSING)

    # 失敗をまたいだ差分は使わない。復帰後の1回目は再び前回値待ち
    path.write_text(stat(200, 0, 200, 900, 0), encoding="utf-8")
    assert value_of(reader.poll()) == (None, Quality.MISSING)


def test_non_linux_is_missing_without_reading(tmp_path: Path):
    result = adapter(tmp_path / "absent", platform="darwin").poll()

    assert result.status is SourceStatus.UNAVAILABLE
    assert result.detail == "unsupported_platform"
    assert value_of(result) == (None, Quality.MISSING)


def test_disabled_has_no_expected_metrics(tmp_path: Path):
    reader = adapter(tmp_path / "stat", enabled=False)

    assert reader.expected_metrics == ()
    assert reader.poll().status is SourceStatus.DISABLED


def test_production_config_enables_it_and_registers_the_rollup_period():
    config = InternalTelemetryConfig.from_yaml(
        CONFIG_DIR / "internal-telemetry.yaml",
        catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
    )

    assert config.proc_stat.enabled
    assert periodic_metric_intervals(config)[CPU_UTILIZATION_METRIC] == config.interval_ms
    catalog = MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")
    assert catalog.unit_for(CPU_UTILIZATION_METRIC) == "%"


@pytest.mark.parametrize(
    ("enabled", "platform", "measures"),
    [(True, "linux", True), (False, "linux", False), (True, "darwin", False)],
)
def test_measures_requires_enabled_and_linux(tmp_path, enabled, platform, measures):
    """エアフロー画面の「未計測」の根拠（#145）。missing の行が保存される場合と区別する。"""
    assert adapter(tmp_path / "stat", enabled=enabled, platform=platform).measures is measures
