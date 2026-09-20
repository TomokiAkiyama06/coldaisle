"""Internal Telemetry の統合・保存を実機なしで検証する。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle import logs, rollup_job
from coldaisle.clock import SimulatedClock
from coldaisle.internal_telemetry import (
    TELEMETRY_KIND_KEY,
    AdapterResult,
    InternalTelemetryCollector,
    InternalTelemetryConfig,
    SourceStatus,
    TelemetrySourceKind,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from coldaisle.telemetry_daemon import (
    SOURCE_STATE_PREFIX,
    Config,
    InternalTelemetryDaemon,
    _log_configuration,
    build,
    main,
    periodic_metric_intervals,
)
from conftest import CONFIG_DIR

CATALOG = MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")


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
        source_kind=TelemetrySourceKind.MOCK,
        sleep=lambda _: None,
    )

    stats = daemon.run(max_cycles=1)
    latest = store.latest()

    assert stats.cycles == 1
    assert latest["air.front_intake"].ts_ms == 900
    assert latest["gpu.0.core"].ts_ms == 1_000
    assert latest["power.gpu.0"].quality is Quality.MISSING
    assert store.current_state(SOURCE_STATE_PREFIX + "nvml") == "degraded"
    assert store.current_state(TELEMETRY_KIND_KEY) == "mock"
    assert adapter.closed


# ------------------------------------------------- 値の出どころの種類（決定記録 0049）


def _kind_daemon(tmp_path: Path, rules, kind: TelemetrySourceKind):
    clock = SimulatedClock(1_000)
    store = SqliteStore(tmp_path / "kind.db", rules=rules, clock=clock)
    adapter = FakeAdapter(
        name="nvml",
        expected_metrics=("gpu.0.core",),
        result=AdapterResult(
            source="nvml",
            status=SourceStatus.OK,
            readings=(Reading(metric="gpu.0.core", value=55.0, quality=Quality.OK),),
        ),
    )
    daemon = InternalTelemetryDaemon(
        collector=InternalTelemetryCollector((adapter,), clock),
        store=store,
        interval_ms=2_500,
        source_kind=kind,
        sleep=lambda _: None,
    )
    return daemon, store, clock


@pytest.mark.parametrize("kind", list(TelemetrySourceKind))
def test_the_daemon_records_its_own_source_kind(tmp_path: Path, rules, kind):
    """デーモンが自分の出どころの種類を `sys.telemetry_kind` へ書く（決定記録 0049 §2.3）。"""
    daemon, store, _ = _kind_daemon(tmp_path, rules, kind)

    daemon.run(max_cycles=1)

    assert store.current_state(TELEMETRY_KIND_KEY) == kind.value


def test_the_source_kind_is_written_before_the_first_collection(tmp_path: Path, rules):
    """最初の収集の前に、収集と同じ Clock の時刻で書く。

    値より後に書くと、最初の周期だけ種類の分からない値が画面に出る。
    """
    daemon, store, clock = _kind_daemon(tmp_path, rules, TelemetrySourceKind.HARDWARE)

    daemon.run(max_cycles=1)

    kind_at = store.connection.execute(
        "SELECT ts_ms FROM system_state WHERE key = ?", (TELEMETRY_KIND_KEY,)
    ).fetchone()[0]
    assert kind_at == clock.now_ms() == 1_000
    assert store.latest()["gpu.0.core"].ts_ms == 1_000


def test_the_source_kind_is_written_once_while_it_does_not_change(tmp_path: Path, rules):
    """`set_system_state` の規則どおり変化時だけ書く（決定記録 0002 §2.6）。"""
    daemon, store, _ = _kind_daemon(tmp_path, rules, TelemetrySourceKind.HARDWARE)

    daemon.run(max_cycles=3)

    rows = store.connection.execute(
        "SELECT COUNT(*) FROM system_state WHERE key = ?", (TELEMETRY_KIND_KEY,)
    ).fetchone()[0]
    assert rows == 1


def test_the_source_kind_is_not_under_the_source_status_prefix():
    """adapter ごとの稼働状態の名前空間と混ぜない（Server Health が読んでいる）。"""
    assert not TELEMETRY_KIND_KEY.startswith(SOURCE_STATE_PREFIX)


def test_replay_is_not_a_source_kind_yet():
    """内部テレメトリを再生する経路が無いので `replay` は定義しない（決定記録 0049 §2.2）。"""
    assert {kind.value for kind in TelemetrySourceKind} == {"hardware", "mock"}


def _telemetry_config(tmp_path: Path) -> Config:
    telemetry = tmp_path / "internal-telemetry.yaml"
    telemetry.write_text(
        "version: 1\ninterval_ms: 2500\n"
        "nvml: {enabled: false, gpu_indices: [0]}\n"
        "proc_stat: {enabled: false, path: /proc/stat}\n"
        "hwmon: {enabled: false, root: /sys/class/hwmon, sensors: []}\n",
        encoding="utf-8",
    )
    return Config(
        db=tmp_path / "built.db",
        telemetry=telemetry,
        quality_rules=CONFIG_DIR / "quality.yaml",
        metrics=CONFIG_DIR / "metrics.yaml",
    )


def test_building_without_adapters_is_hardware(tmp_path: Path):
    """通常の起動（実 adapter を build が組む）は `hardware`（決定記録 0049 §2.1）。"""
    daemon = build(_telemetry_config(tmp_path), clock=SimulatedClock(0))
    try:
        assert daemon.source_kind is TelemetrySourceKind.HARDWARE
    finally:
        daemon.store.close()


def test_building_with_adapters_requires_an_explicit_source_kind(tmp_path: Path):
    """**既定を持たせない。** 既定が hardware だと偽 adapter が黙って実機を名乗る。"""
    adapter = FakeAdapter(
        name="fake",
        expected_metrics=("gpu.0.core",),
        result=AdapterResult(source="fake", status=SourceStatus.OK),
    )
    with pytest.raises(ValueError, match="source_kind"):
        build(_telemetry_config(tmp_path), clock=SimulatedClock(0), adapters=(adapter,))


def test_building_with_adapters_uses_the_given_source_kind(tmp_path: Path):
    adapter = FakeAdapter(
        name="fake",
        expected_metrics=("gpu.0.core",),
        result=AdapterResult(source="fake", status=SourceStatus.OK),
    )
    daemon = build(
        _telemetry_config(tmp_path),
        clock=SimulatedClock(0),
        adapters=(adapter,),
        source_kind=TelemetrySourceKind.MOCK,
    )
    try:
        assert daemon.source_kind is TelemetrySourceKind.MOCK
    finally:
        daemon.store.close()


def test_an_empty_adapter_set_is_rejected(tmp_path: Path):
    """空の tuple を「渡していない」扱いにしない（Codex P2）。

    `adapters or (...)` だと空の tuple が実 adapter へ落ち、`mock` と名乗ったまま
    実機の NVML / hwmon / /proc/stat を読んだ値を記録してしまう。
    """
    with pytest.raises(ValueError, match="空"):
        build(
            _telemetry_config(tmp_path),
            clock=SimulatedClock(0),
            adapters=(),
            source_kind=TelemetrySourceKind.MOCK,
        )


def test_an_empty_adapter_set_is_rejected_before_the_kind_is_checked(tmp_path: Path):
    """空の tuple は `source_kind` の有無に関わらず実 adapter へ落とさない。"""
    with pytest.raises(ValueError, match="source_kind"):
        build(_telemetry_config(tmp_path), clock=SimulatedClock(0), adapters=())


def test_the_source_kind_of_the_real_adapters_is_not_named_from_outside(tmp_path: Path):
    """実 adapter を組むのは build 自身。外から種類を名乗らせない。"""
    with pytest.raises(ValueError, match="source_kind"):
        build(
            _telemetry_config(tmp_path),
            clock=SimulatedClock(0),
            source_kind=TelemetrySourceKind.MOCK,
        )


def test_startup_audit_logs_disabled_reason_and_confirmation(caplog):
    config = InternalTelemetryConfig.from_yaml(
        CONFIG_DIR / "internal-telemetry.yaml", catalog=CATALOG
    )

    with caplog.at_level(logging.INFO, logger="coldaisle.internal_telemetry"):
        _log_configuration(config)

    hwmon_records = [
        record for record in caplog.records if record.message == "hwmon input configuration"
    ]
    assert len(hwmon_records) == len(config.hwmon.sensors)
    by_metric = {
        getattr(record, logs.FIELDS_KEY)["metric"]: getattr(record, logs.FIELDS_KEY)
        for record in hwmon_records
    }
    fields = by_metric["board.connector_12v2x6"]
    assert fields["metric"] == "board.connector_12v2x6"
    assert fields["enabled"] is False
    assert fields["selector"] == "none"
    assert fields["disabled_reason"].startswith("not installed")
    confirmed = by_metric["fan.front.rpm"]
    assert confirmed["enabled"] is True
    assert confirmed["confirmation_status"] == "confirmed"


def test_periodic_metric_intervals_follow_the_configured_interval():
    """ロールアップへ渡す周期は設定の interval_ms と有効な入力だけから作る。"""
    config = InternalTelemetryConfig.from_yaml(
        CONFIG_DIR / "internal-telemetry.yaml", catalog=CATALOG
    )

    intervals = periodic_metric_intervals(config)

    assert intervals["gpu.0.core"] == config.interval_ms
    assert intervals["sys.cuda_processes"] == config.interval_ms
    # 決定記録 0043 の入力も欠測を数える対象にする
    for metric in ("cpu.tctl", "cpu.ccd1", "cpu.ccd2", "gpu.0.tlimit_margin", "gpu.0.fan_speed"):
        assert intervals[metric] == config.interval_ms
    assert intervals["gpu.0.throttle.hw_thermal"] == config.interval_ms
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
        "proc_stat: {enabled: false, path: /proc/stat}\n"
        "hwmon: {enabled: false, root: /sys/class/hwmon, sensors: []}\n",
        encoding="utf-8",
    )

    code = rollup_job.main(
        [
            f"--db={database}",
            f"--retention={retention}",
            f"--quality-rules={CONFIG_DIR / 'quality.yaml'}",
            f"--internal-telemetry={telemetry}",
            f"--metrics={CONFIG_DIR / 'metrics.yaml'}",
        ],
        # ジョブの時計で6分目の途中。完了した5分目までを欠測として埋める
        clock=SimulatedClock(6 * 60_000 + 10_000),
    )

    assert code == 0
    with SqliteStore(database, rules=rules, clock=SimulatedClock(0)) as store:
        expected = store.connection.execute(
            "SELECT expected_count FROM readings_1m WHERE metric = 'gpu.0.core'"
        ).fetchall()
    assert [row[0] for row in expected] == [12] * 6


@dataclass
class TimedAdapter:
    """poll に処理時間がかかる adapter。SimulatedClock を進めて再現する。"""

    clock: SimulatedClock
    work_ms: int
    name: str = "nvml"
    expected_metrics: tuple[str, ...] = ("gpu.0.core",)
    closed: bool = False

    def poll(self) -> AdapterResult:
        self.clock.advance_to_ms(self.clock.now_ms() + self.work_ms)
        return AdapterResult(
            source=self.name,
            status=SourceStatus.OK,
            readings=(Reading(metric="gpu.0.core", value=55.0, quality=Quality.OK),),
        )

    def close(self) -> None:
        self.closed = True


def _paced_daemon(tmp_path: Path, rules, work_ms: int):
    clock = SimulatedClock(0)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance_to_ms(clock.now_ms() + round(seconds * 1_000))

    store = SqliteStore(tmp_path / "paced.db", rules=rules, clock=clock)
    daemon = InternalTelemetryDaemon(
        collector=InternalTelemetryCollector((TimedAdapter(clock, work_ms),), clock),
        store=store,
        interval_ms=2_500,
        source_kind=TelemetrySourceKind.MOCK,
        sleep=sleep,
        monotonic_ms=clock.now_ms,
    )
    return daemon, store, sleeps


def _poll_starts(store) -> list[int]:
    # collector は poll の前に timestamp を取る
    return [point.ts_ms for point in store.series("gpu.0.core", 0, 60_000)]


def test_poll_period_subtracts_the_work_time(tmp_path: Path, rules):
    """処理時間ぶん待ち時間を減らし、実周期を interval_ms に保つ。"""
    daemon, store, sleeps = _paced_daemon(tmp_path, rules, work_ms=300)

    daemon.run(max_cycles=4)

    assert _poll_starts(store) == [0, 2_500, 5_000, 7_500]
    assert sleeps == [2.2, 2.2, 2.2]
    assert daemon.stats.skipped_slots == 0


def test_work_ending_exactly_on_the_deadline_is_not_an_overrun(tmp_path: Path, rules):
    """処理が周期ちょうどで終わった場合は枠を飛ばさず、すぐ次を収集する。"""
    daemon, store, sleeps = _paced_daemon(tmp_path, rules, work_ms=2_500)

    daemon.run(max_cycles=3)

    assert _poll_starts(store) == [0, 2_500, 5_000]
    assert sleeps == []
    assert daemon.stats.skipped_slots == 0


@pytest.mark.parametrize(
    ("work_ms", "second_start", "skipped"),
    [
        (2_500, 2_500, 0),  # 周期の1倍: 間に合っている
        (5_000, 5_000, 1),  # 2倍: 1枠だけ飛ばし、倍数の境界の枠はそのまま使う
        (7_500, 7_500, 2),  # 3倍
        (3_000, 5_000, 1),  # 倍数でない: 次の枠まで待つ
    ],
)
def test_skipped_slots_round_the_delay_up(tmp_path: Path, rules, work_ms, second_start, skipped):
    """飛ばす枠数は遅れを周期で切り上げた数。周期の倍数ちょうどの境界は飛ばさない。"""
    daemon, store, _ = _paced_daemon(tmp_path, rules, work_ms=work_ms)

    daemon.run(max_cycles=2)

    assert _poll_starts(store) == [0, second_start]
    assert daemon.stats.skipped_slots == skipped


def test_overrun_skips_missed_slots_without_bursting(tmp_path: Path, rules):
    """周期を超えたら過ぎた枠を飛ばす。遅れを取り戻す連続収集をしない。"""
    daemon, store, sleeps = _paced_daemon(tmp_path, rules, work_ms=3_000)

    daemon.run(max_cycles=3)

    assert _poll_starts(store) == [0, 5_000, 10_000]
    assert all(seconds > 0 for seconds in sleeps), "待ち時間0の連続収集をしない"
    assert daemon.stats.skipped_slots == 2


def test_once_creates_the_database_directory(tmp_path: Path, monkeypatch):
    """`var/` は追跡されていない。素の checkout で `--once` が動くこと。"""
    # main() が pytest 自身の SIGINT / SIGTERM ハンドラを置き換えないようにする
    monkeypatch.setattr("coldaisle.telemetry_daemon.signal.signal", lambda *_: None)
    telemetry = tmp_path / "internal-telemetry.yaml"
    telemetry.write_text(
        "version: 1\ninterval_ms: 2500\n"
        "nvml: {enabled: false, gpu_indices: [0]}\n"
        "proc_stat: {enabled: false, path: /proc/stat}\n"
        "hwmon: {enabled: false, root: /sys/class/hwmon, sensors: []}\n",
        encoding="utf-8",
    )
    database = tmp_path / "var" / "coldaisle.db"
    assert not database.parent.exists()

    code = main(
        [
            "--once",
            f"--db={database}",
            f"--config={telemetry}",
            f"--quality-rules={CONFIG_DIR / 'quality.yaml'}",
            f"--metrics={CONFIG_DIR / 'metrics.yaml'}",
        ]
    )

    assert code == 0
    assert database.exists()


def test_interval_that_does_not_divide_a_minute_fails_at_load(tmp_path: Path):
    """1分を割り切らない周期は欠測率が負になりうるため、設定の読み込みで拒否する。"""
    telemetry = tmp_path / "internal-telemetry.yaml"
    telemetry.write_text(
        "version: 1\ninterval_ms: 7000\n"
        "nvml: {enabled: false, gpu_indices: [0]}\n"
        "proc_stat: {enabled: false, path: /proc/stat}\n"
        "hwmon: {enabled: false, root: /sys/class/hwmon, sensors: []}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="割り切る"):
        InternalTelemetryConfig.from_yaml(telemetry, catalog=CATALOG)


def _hwmon_config(tmp_path: Path, metric: str, measurement: str) -> Path:
    telemetry = tmp_path / "internal-telemetry.yaml"
    telemetry.write_text(
        "version: 1\ninterval_ms: 2500\n"
        "nvml: {enabled: false, gpu_indices: [0]}\n"
        "proc_stat: {enabled: false, path: /proc/stat}\n"
        "hwmon:\n  enabled: true\n  root: /sys/class/hwmon\n  sensors:\n"
        f"    - {{metric: {metric}, enabled: false, measurement: {measurement},\n"
        "       disabled_reason: test}\n",
        encoding="utf-8",
    )
    return telemetry


@pytest.mark.parametrize(
    ("metric", "measurement"),
    [
        ("cpu.package", "temperature"),
        ("power.cpu.package", "power"),
        ("fan.front.rpm", "rpm"),
        ("fan.front.pwm", "pwm"),
    ],
)
def test_hwmon_measurement_matching_the_catalog_unit_loads(tmp_path: Path, metric, measurement):
    config = InternalTelemetryConfig.from_yaml(
        _hwmon_config(tmp_path, metric, measurement), catalog=CATALOG
    )
    assert config.hwmon.sensors[0].metric == metric


@pytest.mark.parametrize(
    ("metric", "measurement"),
    [
        ("fan.front.rpm", "temperature"),
        ("cpu.package", "rpm"),
        ("fan.front.pwm", "power"),
        ("power.cpu.package", "pwm"),
    ],
)
def test_hwmon_measurement_with_a_different_unit_fails_at_load(tmp_path: Path, metric, measurement):
    """温度の入力を rpm の metric へ割り当てるような誤設定を読み込みで拒否する。"""
    with pytest.raises(ValueError, match="一致しない"):
        InternalTelemetryConfig.from_yaml(
            _hwmon_config(tmp_path, metric, measurement), catalog=CATALOG
        )


def test_hwmon_metric_missing_from_the_catalog_fails_at_load(tmp_path: Path):
    with pytest.raises(ValueError, match="定義の無い"):
        InternalTelemetryConfig.from_yaml(
            _hwmon_config(tmp_path, "cpu.unknown_sensor", "temperature"), catalog=CATALOG
        )
