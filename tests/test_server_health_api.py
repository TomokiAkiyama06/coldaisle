"""Workspace 向け Server Health API の安全・契約テスト。#66"""

from __future__ import annotations

import ast
import importlib
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coldaisle.ai import (
    AiHealthSummarizer,
    BackgroundHealthSummarizer,
    ChatResult,
    UnavailableProvider,
)
from coldaisle.api.app import Config, create_app
from coldaisle.api.models import ComputeModeAdvisory, ServerHealthResponse
from coldaisle.api.server_health import ServerHealthSettings, server_health_state
from coldaisle.clock import SimulatedClock
from coldaisle.internal_telemetry import (
    SOURCE_STATE_PREFIX,
    ClockEventReasons,
    InternalTelemetryCollector,
    NvmlAdapter,
    NvmlConfig,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store import AlertSeverity, Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH

NOW_MS = 1_787_616_000_000
SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
GREEN_TEMPLATE = "監視対象のTelemetryと情報源は正常です。"

SENSOR_VALUES = {
    "air.room": 26.0,
    "air.room_humidity": 48.0,
    "air.front_intake": 27.0,
    "air.gpu_intake": 28.0,
    "air.gpu_exhaust": 36.0,
    "air.top_exhaust": 31.0,
    "air.rear_exhaust": 32.0,
}
INTERNAL_VALUES = {
    "gpu.0.core": 54.0,
    "gpu.0.hotspot": 64.0,
    "gpu.0.mem": 58.0,
    "gpu.0.utilization": 42.0,
    "gpu.0.vram_used": 8.0,
    "power.gpu.0": 180.0,
    "sys.cuda_processes": 2.0,
    "gpu.0.tlimit_margin": 62.0,
    "gpu.0.fan_speed": 30.0,
    "gpu.0.throttle.hw_slowdown": 0.0,
    "gpu.0.throttle.hw_thermal": 0.0,
    "gpu.0.throttle.sw_thermal": 0.0,
    "gpu.0.throttle.hw_power_brake": 0.0,
    "gpu.0.throttle.sw_power_cap": 0.0,
    "cpu.package": 49.0,
    "power.cpu.package": 72.0,
    "cpu.vrm": 46.0,
    "board.chipset": 44.0,
}

NVML_METRICS = (
    "gpu.0.core",
    "gpu.0.hotspot",
    "gpu.0.mem",
    "gpu.0.utilization",
    "gpu.0.vram_used",
    "power.gpu.0",
    "sys.cuda_processes",
    "gpu.0.tlimit_margin",
    "gpu.0.fan_speed",
    "gpu.0.throttle.hw_slowdown",
    "gpu.0.throttle.hw_thermal",
    "gpu.0.throttle.sw_thermal",
    "gpu.0.throttle.hw_power_brake",
    "gpu.0.throttle.sw_power_cap",
)


class FakeSummarizer:
    def __init__(self, summary: str | None = "監視情報と各データ源は正常です。") -> None:
        self.summary = summary
        self.facts: list[str] = []

    def summarize(self, facts: str) -> str | None:
        self.facts.append(facts)
        return self.summary


class RaisingSummarizer:
    def summarize(self, facts: str) -> str | None:
        raise RuntimeError("local model stopped")


class FakeProvider:
    def __init__(self, text: str, *, available: bool = True) -> None:
        self.text = text
        self.available = available
        self.messages = []
        self.thinking = None
        self.called = threading.Event()

    def chat(self, messages, *, thinking=None, tools=None) -> ChatResult:
        self.messages = messages
        self.thinking = thinking
        self.called.set()
        return ChatResult(available=self.available, text=self.text)

    def probe(self) -> ChatResult:
        return ChatResult(available=self.available)


def _populate(
    path: Path,
    rules,
    *,
    sensor_values: dict[str, float] | None = None,
    internal_values: dict[str, float] | None = None,
    sensor_qualities: dict[str, Quality] | None = None,
    ts_ms: int = NOW_MS,
) -> None:
    sensors = SENSOR_VALUES if sensor_values is None else sensor_values
    internals = INTERNAL_VALUES if internal_values is None else internal_values
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=ts_ms,
                readings=tuple(
                    Reading(
                        metric=name,
                        value=value,
                        quality=(sensor_qualities or {}).get(name, Quality.OK),
                    )
                    for name, value in sensors.items()
                )
                + tuple(
                    Reading(metric=name, value=value, quality=Quality.OK)
                    for name, value in internals.items()
                ),
            )
        )
        store.set_system_state("sys.ingest_source", "serial", at_ms=ts_ms)
        store.set_system_state(SOURCE_STATE_PREFIX + "nvml", "ok", at_ms=ts_ms)
        store.set_system_state(SOURCE_STATE_PREFIX + "hwmon", "ok", at_ms=ts_ms)
        store.set_system_state("sys.gpu_mode", "ai", at_ms=ts_ms)


def _app(
    path: Path,
    clock: SimulatedClock,
    summarizer=None,
    *,
    hwmon_metrics=("cpu.package",),
    nvml_metrics=NVML_METRICS,
):
    return create_app(
        Config(
            db=path,
            quality_rules=QUALITY_RULES_PATH,
            metrics=CONFIG_DIR / "metrics.yaml",
            stream_poll_s=0.001,
        ),
        clock=clock,
        health_summarizer=summarizer,
        health_hwmon_metrics=hwmon_metrics,
        health_nvml_metrics=nvml_metrics,
    )


@pytest.fixture
def healthy_db(tmp_path, rules) -> Path:
    path = tmp_path / "server-health.db"
    _populate(path, rules)
    return path


def test_complete_template_payload_is_green_when_monitoring_sources_are_healthy(
    healthy_db,
):
    """AI停止は監視停止ではない。全フィールドを決定論的テンプレートで返す。"""
    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS))) as client:
        response = client.get("/api/v1/server-health")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["generated_at_ms"] == NOW_MS
    assert body["generated_at"].endswith("+00:00")
    assert body["signal"] == "green"
    assert body["summary_source"] == "template"
    assert body["summary"]
    assert body["gpu"]["mode"] == "ai"
    assert body["gpu"]["metrics"]["gpu.0.core"] == {
        "value": 54.0,
        "unit": "C",
        "quality": "ok",
        "age_seconds": 0.0,
    }
    assert set(body["sources"]) == {"sensor_unit", "nvml", "lm_sensors", "ai_layer"}
    assert body["sources"]["ai_layer"]["status"] == "stopped"
    assert body["compute_mode_advisory"] == {
        "safe": True,
        "warnings": [],
        "blocking": False,
    }


def test_missing_optional_gpu_values_keep_stable_keys_without_nvidia_smi(tmp_path, rules):
    """Workspace は欠測も含む固定キーだけで描画し、別コマンドを必要としない。"""
    path = tmp_path / "partial-gpu.db"
    _populate(
        path,
        rules,
        # 機種が公開しない hotspot / mem（missing_tolerated）は一度も保存されていない
        internal_values={
            name: value
            for name, value in INTERNAL_VALUES.items()
            if name not in {"gpu.0.hotspot", "gpu.0.mem"}
        },
    )
    with TestClient(_app(path, SimulatedClock(NOW_MS))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "green"
    assert body["gpu"]["metrics"]["gpu.0.hotspot"]["value"] is None
    assert body["gpu"]["metrics"]["gpu.0.hotspot"]["quality"] == "missing"
    assert body["environment"]["metrics"]["board.connector_12v2x6"]["value"] is None

    implementation = (SRC / "api" / "server_health.py").read_text(encoding="utf-8")
    assert "nvidia-smi" not in implementation
    assert "subprocess" not in implementation


def test_ai_summary_can_only_replace_the_template(healthy_db):
    summarizer = FakeSummarizer("監視情報と各データ源は正常です。")
    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS), summarizer)) as client:
        ai = client.get("/api/v1/server-health").json()
    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS))) as client:
        template = client.get("/api/v1/server-health").json()

    assert ai["summary_source"] == "ai"
    assert ai["sources"]["ai_layer"]["status"] == "ok"
    assert summarizer.facts
    for deterministic in (
        "signal",
        "gpu",
        "environment",
        "active_alerts",
        "compute_mode_advisory",
    ):
        assert ai[deterministic] == template[deterministic]


@pytest.mark.parametrize("summarizer", [FakeSummarizer(None), RaisingSummarizer()])
def test_ai_unavailable_or_failed_uses_the_complete_template(healthy_db, summarizer):
    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS), summarizer)) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "green"
    assert body["summary_source"] == "template"
    assert body["summary"]
    assert body["sources"]["ai_layer"]["status"] == "stopped"
    assert body["compute_mode_advisory"]["blocking"] is False


def test_dead_sensor_unit_is_never_green(tmp_path, rules):
    path = tmp_path / "dead-sensor.db"
    _populate(path, rules, ts_ms=NOW_MS - 11_000)
    with TestClient(_app(path, SimulatedClock(NOW_MS))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "red"
    assert body["sources"]["sensor_unit"]["status"] == "unavailable"
    assert "no fresh required sensor telemetry" in body["sources"]["sensor_unit"]["detail"]
    assert body["environment"]["metrics"]["air.room"]["quality"] == "stale"
    assert body["compute_mode_advisory"]["safe"] is False
    assert body["compute_mode_advisory"]["blocking"] is False


def test_one_suspect_sensor_is_yellow_not_green(tmp_path, rules):
    path = tmp_path / "suspect-sensor.db"
    _populate(path, rules, sensor_qualities={"air.gpu_intake": Quality.SUSPECT})
    with TestClient(_app(path, SimulatedClock(NOW_MS))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "yellow"
    assert body["sources"]["sensor_unit"]["status"] == "degraded"


def test_one_stale_sensor_is_yellow_not_green(tmp_path, rules):
    path = tmp_path / "one-stale-sensor.db"
    fresh = {name: value for name, value in SENSOR_VALUES.items() if name != "air.gpu_intake"}
    _populate(path, rules, sensor_values=fresh)
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS - 11_000,
                readings=(Reading(metric="air.gpu_intake", value=28.0, quality=Quality.OK),),
            )
        )

    with TestClient(_app(path, SimulatedClock(NOW_MS))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "yellow"
    assert body["sources"]["sensor_unit"]["status"] == "degraded"
    assert "partial required sensor telemetry" in body["sources"]["sensor_unit"]["detail"]


def test_lm_sensors_liveness_accepts_any_fresh_configurable_hwmon_metric(tmp_path, rules):
    """#65 の hwmon mapping は設定駆動であり、CPU package 固定ではない。"""
    path = tmp_path / "connector-only-hwmon.db"
    _populate(
        path,
        rules,
        internal_values={
            **{name: INTERNAL_VALUES[name] for name in NVML_METRICS},
            "board.connector_12v2x6": 42.0,
        },
    )
    with TestClient(
        _app(
            path,
            SimulatedClock(NOW_MS),
            hwmon_metrics=("board.connector_12v2x6",),
        )
    ) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["sources"]["lm_sensors"]["status"] == "ok"
    assert body["signal"] == "green"


def test_lm_sensors_uses_the_actual_internal_telemetry_config(tmp_path, rules):
    path = tmp_path / "configured-hwmon.db"
    _populate(
        path,
        rules,
        internal_values={
            **{name: INTERNAL_VALUES[name] for name in NVML_METRICS},
            "board.connector_12v2x6": 42.0,
        },
    )
    internal_config = tmp_path / "internal-telemetry.yaml"
    internal_config.write_text(
        """version: 1
interval_ms: 2500
nvml:
  enabled: true
  gpu_indices: [0]
hwmon:
  enabled: true
  root: /sys/class/hwmon
  sensors:
    - metric: board.connector_12v2x6
      enabled: true
      driver: fixture-driver
      label: T_SENSOR
      measurement: temperature
      required: true
      minimum: 0.0
      maximum: 125.0
      confirmation:
        status: confirmed
        basis: fixture measurement and owner approval
""",
        encoding="utf-8",
    )
    app = create_app(
        Config(
            db=path,
            quality_rules=QUALITY_RULES_PATH,
            metrics=CONFIG_DIR / "metrics.yaml",
            internal_telemetry=internal_config,
        ),
        clock=SimulatedClock(NOW_MS),
    )

    with TestClient(app) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["sources"]["lm_sensors"]["status"] == "ok"
    assert body["signal"] == "green"


@pytest.mark.parametrize(("source", "state"), [("nvml", "unavailable"), ("hwmon", "disabled")])
def test_failed_internal_source_is_red(healthy_db, rules, source, state):
    with SqliteStore(healthy_db, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.set_system_state(SOURCE_STATE_PREFIX + source, state, at_ms=NOW_MS + 1)

    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS + 1))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "red"
    api_name = "lm_sensors" if source == "hwmon" else source
    assert body["sources"][api_name]["status"] == state
    assert body["compute_mode_advisory"]["safe"] is False
    assert body["compute_mode_advisory"]["blocking"] is False


@pytest.mark.parametrize(
    ("severity", "expected_signal"), [("warning", "yellow"), ("critical", "red")]
)
def test_active_alert_changes_signal_deterministically(
    healthy_db, rules, severity, expected_signal
):
    with SqliteStore(healthy_db, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        alert_id = store.open_alert(
            rule_id="TEST_ALERT",
            severity=severity,
            metric="air.room",
            started_ms=NOW_MS - 1_000,
            threshold=30.0,
            trigger_value=31.0,
        )
        store.fire_alert(alert_id, fired_ms=NOW_MS, trigger_value=31.0)

    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == expected_signal
    assert body["active_alerts"][0]["rule_id"] == "TEST_ALERT"
    assert f"active alert: TEST_ALERT ({severity})" in body["compute_mode_advisory"]["warnings"]


def test_empty_database_is_complete_and_red(tmp_path):
    path = tmp_path / "empty.db"
    with TestClient(_app(path, SimulatedClock(NOW_MS))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "red"
    assert body["summary_source"] == "template"
    assert all(
        source["status"] == "stopped"
        for name, source in body["sources"].items()
        if name != "ai_layer"
    )
    assert body["gpu"]["mode"] == "unknown"
    assert body["gpu"]["metrics"]["gpu.0.core"]["quality"] == "missing"
    assert body["environment"]["metrics"]["air.room"]["quality"] == "missing"


def test_event_metric_staleness_does_not_change_a_green_signal(healthy_db, rules):
    """事象メトリクスは周期データではないため鮮度判定から外す（DR0009 §2.12）。"""
    with SqliteStore(healthy_db, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS - 60_000,
                readings=(Reading(metric="sys.dropped_samples", value=1.0, quality=Quality.OK),),
            )
        )

    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "green"


def test_websocket_pushes_the_exact_rest_payload(healthy_db):
    app = _app(healthy_db, SimulatedClock(NOW_MS))
    with TestClient(app) as client:
        rest = client.get("/api/v1/server-health").json()
        with client.websocket_connect("/api/v1/server-health/stream") as websocket:
            pushed = websocket.receive_json()

    assert pushed == rest


def test_websocket_state_ignores_clock_and_age_only_changes(healthy_db):
    clock = SimulatedClock(NOW_MS)
    with TestClient(_app(healthy_db, clock)) as client:
        first = ServerHealthResponse.model_validate(client.get("/api/v1/server-health").json())
        clock.advance_to_ms(NOW_MS + 1_000)
        second = ServerHealthResponse.model_validate(client.get("/api/v1/server-health").json())

    assert second.generated_at_ms != first.generated_at_ms
    assert (
        second.gpu.metrics["gpu.0.core"].age_seconds != first.gpu.metrics["gpu.0.core"].age_seconds
    )
    assert server_health_state(second) == server_health_state(first)


def test_websocket_detects_disconnect_while_health_state_is_stable(healthy_db, monkeypatch):
    api_module = importlib.import_module("coldaisle.api.app")

    original = api_module.build_server_health
    calls = 0

    def counting_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(api_module, "build_server_health", counting_build)
    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS))) as client:
        with client.websocket_connect("/api/v1/server-health/stream") as websocket:
            websocket.receive_json()
        calls_at_disconnect = calls
        time.sleep(0.02)

    assert calls <= calls_at_disconnect + 1


def test_openapi_exposes_a_typed_read_only_contract(healthy_db):
    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS))) as client:
        document = client.get("/openapi.json").json()

    operation = document["paths"]["/api/v1/server-health"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("ServerHealthResponse")
    methods = {method for operations in document["paths"].values() for method in operations}
    assert methods == {"get"}


def test_api_layer_still_does_not_import_the_ai_layer():
    offending = []
    for path in (SRC / "api").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                names = []
            offending += [
                f"{path.name}: {name}" for name in names if name.startswith("coldaisle.ai")
            ]
    assert offending == []


def test_compute_mode_advisory_cannot_become_blocking():
    with pytest.raises(ValueError):
        ComputeModeAdvisory(safe=False, warnings=("hot",), blocking=True)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "GPU温度は54度で正常です。",
        "ファン制御を変更しました。",
        "正常なのでCompute Modeへの切替を推奨します。",
        "正常なので処理を開始しても安全です。",
        "監視対象は正常ではありません。",
        "確認しましたが問題ありません。",
        "異常はありません。",
        "1行目\n2行目",
        "x" * 10_000,
        "   ",
    ],
)
def test_ai_summary_rejects_numbers_actions_multiline_and_empty_text(text):
    provider = FakeProvider(text)
    assert AiHealthSummarizer(provider).summarize(GREEN_TEMPLATE) is None
    assert provider.messages[0].role == "system"


def test_ai_summary_accepts_only_an_allowed_paraphrase():
    provider = FakeProvider("監視情報と各データ源は正常です。")
    summary = AiHealthSummarizer(provider).summarize(GREEN_TEMPLATE)
    assert summary == "監視情報と各データ源は正常です。"
    assert provider.thinking is False


def test_composition_root_injects_the_ai_summarizer(healthy_db, tmp_path, monkeypatch):
    """L2→L3 の逆依存を作らず、server.py だけが AI 実体を合成する。"""
    import coldaisle.server as server

    # リポジトリの internal-telemetry.yaml に依存させない。有効な入力が変わると
    # signal（= AI に渡すテンプレート）が変わり、固定の言い換えが不正出力になる
    internal_config = tmp_path / "internal-telemetry.yaml"
    internal_config.write_text(
        """version: 1
interval_ms: 2500
nvml:
  enabled: true
  gpu_indices: [0]
hwmon:
  enabled: true
  root: /sys/class/hwmon
  sensors:
    - metric: cpu.package
      enabled: true
      driver: fixture-driver
      label: Tctl
      measurement: temperature
      confirmation:
        status: confirmed
        basis: fixture
""",
        encoding="utf-8",
    )
    provider = FakeProvider("監視情報と各データ源は正常です。")
    monkeypatch.setattr(server, "provider_from_env", lambda settings: provider)
    app = server.create_server(
        Config(
            db=healthy_db,
            quality_rules=QUALITY_RULES_PATH,
            metrics=CONFIG_DIR / "metrics.yaml",
            internal_telemetry=internal_config,
        ),
        clock=SimulatedClock(NOW_MS),
    )

    with TestClient(app) as client:
        first = client.get("/api/v1/server-health").json()
        # provider の呼び出しではなく、cache された要約が API に現れるまでを待つ
        deadline = time.monotonic() + 5.0
        body = first
        while body["summary_source"] != "ai" and time.monotonic() < deadline:
            time.sleep(0.01)
            body = client.get("/api/v1/server-health").json()

    assert first["signal"] == "green"
    assert first["summary_source"] == "template", "AI の完了を待たない"
    assert body["summary_source"] == "ai"
    assert body["summary"] == "監視情報と各データ源は正常です。"
    assert body["sources"]["ai_layer"]["status"] == "ok"
    assert provider.messages


def test_unavailable_provider_causes_template_fallback_directly():
    assert AiHealthSummarizer(UnavailableProvider()).summarize(GREEN_TEMPLATE) is None


def test_background_ai_never_blocks_and_limits_concurrency():
    class BlockingSummarizer:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()
            self.calls = 0

        def summarize(self, template):
            self.calls += 1
            self.entered.set()
            self.release.wait(timeout=1)
            self.finished.set()
            return "監視情報は正常です。"

    delegate = BlockingSummarizer()
    background = BackgroundHealthSummarizer(delegate, retry_s=60.0)

    assert background.summarize(GREEN_TEMPLATE) is None
    assert delegate.entered.wait(timeout=1)
    assert background.summarize(GREEN_TEMPLATE) is None
    assert delegate.calls == 1
    delegate.release.set()
    assert delegate.finished.wait(timeout=1)
    for _ in range(100):
        result = background.summarize(GREEN_TEMPLATE)
        if result is not None:
            break
        time.sleep(0.001)
    assert result == "監視情報は正常です。"


class UnsupportedTemperaturesNvml:
    """hotspot / memory 温度を公開しない GPU を模した NVML API（実機で観測した形）。"""

    def initialize(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def device_count(self) -> int:
        return 1

    def handle(self, index: int) -> object:
        return index

    def core_temperature_c(self, handle: object) -> float:
        return 54.0

    def hotspot_temperature_c(self, handle: object) -> float | None:
        return None

    def memory_temperature_c(self, handle: object) -> float | None:
        return None

    def power_w(self, handle: object) -> float:
        return 180.0

    def utilization_pct(self, handle: object) -> float:
        return 42.0

    def vram_used_gb(self, handle: object) -> float:
        return 8.0

    def compute_process_ids(self, handle: object) -> tuple[int, ...]:
        return (10, 20)

    # margin / reason / fan speed は同じ GPU が公開する（2026-09-18 に実機で確認）
    def tlimit_margin_c(self, handle: object) -> float | None:
        return 62.0

    def clock_event_reasons(self, handle: object) -> ClockEventReasons | None:
        return ClockEventReasons(active=0, supported=0x1FF)

    def fan_speed_pct(self, handle: object) -> float | None:
        return 30.0


def _store_collector_cycle(path: Path, rules, api) -> None:
    """telemetry daemon と同じ経路（collector → insert_sample → source state）で保存する。"""
    clock = SimulatedClock(NOW_MS)
    collector = InternalTelemetryCollector(
        (NvmlAdapter(NvmlConfig(enabled=True, gpu_indices=(0,)), api),), clock
    )
    cycle = collector.collect()
    with SqliteStore(path, rules=rules, clock=clock) as store:
        store.insert_sample(cycle.sample)
        for source in cycle.sources:
            store.set_system_state(
                SOURCE_STATE_PREFIX + source.source, source.status.value, at_ms=NOW_MS
            )


def test_hardware_unsupported_nvml_temperatures_saved_as_missing_stay_green(tmp_path, rules):
    """RTX PRO 6000 は hotspot / mem を返さず、collector は毎回 missing を保存する。

    これで yellow に固定されると本当の劣化と見分けられないため、設定で宣言した
    metric の missing だけは signal を下げない（決定記録 0040）。
    """
    path = tmp_path / "unsupported-nvml.db"
    _populate(path, rules, internal_values={"cpu.package": 49.0})
    _store_collector_cycle(path, rules, UnsupportedTemperaturesNvml())

    with TestClient(_app(path, SimulatedClock(NOW_MS))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["gpu"]["metrics"]["gpu.0.hotspot"]["quality"] == "missing"
    assert body["gpu"]["metrics"]["gpu.0.hotspot"]["age_seconds"] == 0.0
    assert body["gpu"]["metrics"]["gpu.0.mem"]["quality"] == "missing"
    assert body["sources"]["nvml"]["status"] == "ok"
    assert body["signal"] == "green"
    assert body["compute_mode_advisory"]["safe"] is True


@pytest.mark.parametrize("quality", [Quality.SUSPECT, Quality.STALE])
def test_tolerated_metric_that_is_suspect_or_stale_still_degrades(tmp_path, rules, quality):
    """許容するのは missing だけ。値があるのに疑わしい・古いなら従来どおり下げる。"""
    path = tmp_path / "suspect-hotspot.db"
    _populate(path, rules)
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS + 1,
                readings=(Reading(metric="gpu.0.hotspot", value=64.0, quality=quality),),
            )
        )

    with TestClient(_app(path, SimulatedClock(NOW_MS + 1))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "yellow"


def test_missing_metric_that_is_not_tolerated_still_degrades(tmp_path, rules):
    path = tmp_path / "missing-utilization.db"
    _populate(path, rules)
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS + 1,
                readings=(
                    Reading(metric="gpu.0.utilization", value=None, quality=Quality.MISSING),
                ),
            )
        )

    with TestClient(_app(path, SimulatedClock(NOW_MS + 1))) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == "yellow"


def _settings_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "server-health.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_BASE_SETTINGS = """version: 1
sources:
  sensor_unit: {required: [air.room]}
  nvml: {required: [gpu.0.core]}
panels:
  gpu: [gpu.0.core]
  environment: [air.room]
active_alerts_limit: 100
"""


def test_server_health_settings_reject_unknown_metrics(tmp_path):
    catalog = MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")
    path = _settings_yaml(tmp_path, _BASE_SETTINGS + "missing_tolerated: [gpu.0.hotpsot]\n")
    with pytest.raises(ValueError, match=r"gpu\.0\.hotpsot"):
        ServerHealthSettings.from_yaml(path, catalog=catalog)


def test_server_health_settings_reject_tolerating_a_required_metric(tmp_path):
    catalog = MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")
    path = _settings_yaml(tmp_path, _BASE_SETTINGS + "missing_tolerated: [gpu.0.core]\n")
    with pytest.raises(ValueError, match="missing_tolerated"):
        ServerHealthSettings.from_yaml(path, catalog=catalog)


def test_repository_server_health_settings_load():
    catalog = MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")
    settings = ServerHealthSettings.from_yaml(CONFIG_DIR / "server-health.yaml", catalog=catalog)
    assert settings.missing_tolerated == {"gpu.0.hotspot", "gpu.0.mem"}


def test_stale_row_of_a_removed_input_does_not_degrade_the_signal(tmp_path, rules):
    """撤去した入力（例: 外したファン）の最後の行は残って stale になるが、監視対象外。"""
    path = tmp_path / "removed-input.db"
    # 撤去前に fan.vrm.rpm も hwmon から保存されていた
    _populate(path, rules, internal_values={**INTERNAL_VALUES, "fan.vrm.rpm": 900.0})
    later = NOW_MS + 3_600_000
    # 撤去後: 設定から外れた fan.vrm.rpm 以外は新しい値が届き続けている
    _populate(path, rules, ts_ms=later)

    with SqliteStore(path, rules=rules, clock=SimulatedClock(later)) as store:
        assert store.latest()["fan.vrm.rpm"].quality is Quality.STALE
    with TestClient(_app(path, SimulatedClock(later), hwmon_metrics=("cpu.package",))) as client:
        removed = client.get("/api/v1/server-health").json()
    with TestClient(
        _app(path, SimulatedClock(later), hwmon_metrics=("cpu.package", "fan.vrm.rpm"))
    ) as client:
        still_configured = client.get("/api/v1/server-health").json()

    assert removed["signal"] == "green"
    assert removed["compute_mode_advisory"]["safe"] is True
    # 同じ行でも、まだ有効な入力なら従来どおり signal を下げる
    assert still_configured["signal"] == "yellow"


@pytest.mark.parametrize(
    ("enabled_hwmon", "expected"),
    [
        # 実機の現状: board.chipset は 0 °C を返すため無効。cpu.vrm も未有効化
        (("cpu.package",), "green"),
        # cpu.package / cpu.vrm を有効化した後も、無効のままの board.chipset は影響しない
        (("cpu.package", "cpu.vrm"), "green"),
        # 入力を有効にしたパネル metric が古くなれば、従来どおり signal を下げる
        (("cpu.package", "cpu.vrm", "board.chipset"), "yellow"),
    ],
)
def test_panel_metric_with_disabled_input_is_display_only(tmp_path, rules, enabled_hwmon, expected):
    """パネルは表示専用。入力が無効な metric の古い行で signal を下げない。"""
    path = tmp_path / "panel-disabled.db"
    _populate(path, rules)  # cpu.vrm / board.chipset も一度は保存されていた
    later = NOW_MS + 3_600_000
    # board.chipset は以後届かない（無効化、または有効なのに止まった）。cpu.vrm は
    # 有効なときだけ届き続ける
    current = {
        name: value
        for name, value in INTERNAL_VALUES.items()
        if name != "board.chipset" and (name != "cpu.vrm" or name in enabled_hwmon)
    }
    _populate(path, rules, internal_values=current, ts_ms=later)

    with SqliteStore(path, rules=rules, clock=SimulatedClock(later)) as store:
        assert store.latest()["board.chipset"].quality is Quality.STALE
    with TestClient(_app(path, SimulatedClock(later), hwmon_metrics=enabled_hwmon)) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["environment"]["metrics"]["board.chipset"]["quality"] == "stale"
    assert body["signal"] == expected


# --- signal の真理値表（docs/api-contract.md §3 / 決定記録 0040 §2.4〜§2.6）---
#
# 各 metric の状態: ok / suspect / missing（missing として保存）/ stale（古い行だけ）/
# absent（一度も保存されていない）。suspect は「値は届いている」、stale / missing /
# absent は「届いていない」。
STATES = ("ok", "suspect", "missing", "stale", "absent")
TABLE_HWMON = ("cpu.package", "cpu.vrm")
_ALL_TABLE_METRICS = {**SENSOR_VALUES, **INTERNAL_VALUES, "board.chipset": 44.0}


def _table_db(path: Path, rules, states: dict[str, str], *, nvml_state: str | None = "ok"):
    """``states`` 以外の metric は現在時刻に ok で届いている DB を作る。"""
    old = NOW_MS - 60_000  # stale の閾値より十分古い
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=old,
                readings=tuple(
                    Reading(metric=name, value=value, quality=Quality.OK)
                    for name, value in _ALL_TABLE_METRICS.items()
                    if states.get(name) != "absent"
                ),
            )
        )
        current = []
        for name, value in _ALL_TABLE_METRICS.items():
            state = states.get(name, "ok")
            if name == "board.chipset" and name not in states:
                continue  # 入力が無効な panel metric。古い行だけが残る
            if state in {"stale", "absent"}:
                continue
            if state == "missing":
                current.append(Reading(metric=name, value=None, quality=Quality.MISSING))
            else:
                current.append(Reading(metric=name, value=value, quality=Quality(state)))
        store.insert_sample(Sample(ts_ms=NOW_MS, readings=tuple(current)))
        store.set_system_state("sys.ingest_source", "serial", at_ms=NOW_MS)
        if nvml_state is not None:
            store.set_system_state(SOURCE_STATE_PREFIX + "nvml", nvml_state, at_ms=NOW_MS)
        store.set_system_state(SOURCE_STATE_PREFIX + "hwmon", "ok", at_ms=NOW_MS)


# (metric 群, その区分, 状態ごとの期待 signal: ok, suspect, missing, stale, absent)
TRUTH_TABLE = [
    # 監視必須（sensor_unit）の1本だけ: 一部欠ければ source degraded
    (("air.room",), "required-one", ("green", "yellow", "yellow", "yellow", "yellow")),
    # 監視必須のすべて: 値が1本も届かなければ unavailable / red。suspect は届いている
    (tuple(SENSOR_VALUES), "required-all", ("green", "yellow", "red", "red", "red")),
    # NVML の監視必須（core / power）
    (("gpu.0.core",), "nvml-required-one", ("green", "yellow", "yellow", "yellow", "yellow")),
    (("gpu.0.core", "power.gpu.0"), "nvml-required-all", ("green", "yellow", "red", "red", "red")),
    # 有効な入力だが必須ではない（NVML の utilization）
    (("gpu.0.utilization",), "enabled-optional", ("green", "yellow", "yellow", "yellow", "yellow")),
    # 機種が公開しない値（missing_tolerated）: missing / 未保存だけは下げない
    (("gpu.0.hotspot",), "tolerated", ("green", "yellow", "green", "yellow", "green")),
    # 有効な hwmon 入力の1本: lm_sensors はいずれか1本届けば ok、metric 単位で yellow
    (("cpu.vrm",), "hwmon-one", ("green", "yellow", "yellow", "yellow", "yellow")),
    # 有効な hwmon 入力のすべて: 1本も届かなければ lm_sensors unavailable / red
    (TABLE_HWMON, "hwmon-all", ("green", "yellow", "red", "red", "red")),
    # 入力が無効な panel metric: 表示専用で signal に影響しない
    (("board.chipset",), "panel-disabled", ("green", "green", "green", "green", "green")),
    # 事象メトリクスは鮮度判定から外す（0009 §2.12）。監視対象でもない
    (("sys.dropped_samples",), "event", ("green", "green", "green", "green", "green")),
]


@pytest.mark.parametrize(
    ("metrics", "state", "expected"),
    [
        pytest.param(metrics, state, expected[index], id=f"{kind}-{state}")
        for metrics, kind, expected in TRUTH_TABLE
        for index, state in enumerate(STATES)
    ],
)
def test_signal_truth_table(tmp_path, rules, metrics, state, expected):
    path = tmp_path / "truth-table.db"
    if metrics == ("sys.dropped_samples",):
        _table_db(path, rules, {})
        if state != "absent":
            with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
                ts = NOW_MS - 60_000 if state == "stale" else NOW_MS
                quality = Quality.OK if state == "stale" else Quality(state)
                value = None if quality is Quality.MISSING else 1.0
                store.insert_sample(
                    Sample(
                        ts_ms=ts,
                        readings=(
                            Reading(metric="sys.dropped_samples", value=value, quality=quality),
                        ),
                    )
                )
    else:
        _table_db(path, rules, dict.fromkeys(metrics, state))
    with TestClient(_app(path, SimulatedClock(NOW_MS), hwmon_metrics=TABLE_HWMON)) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["signal"] == expected
    assert body["compute_mode_advisory"]["safe"] is (expected == "green")


@pytest.mark.parametrize(
    ("reported", "expected_status", "expected_signal"),
    [
        ("ok", "ok", "green"),
        ("degraded", "degraded", "yellow"),
        ("unavailable", "unavailable", "red"),
        ("disabled", "disabled", "red"),
        ("stopped", "stopped", "red"),
        ("not-a-status", "unavailable", "red"),
        (None, "stopped", "red"),  # collector の状態が一度も記録されていない
    ],
)
def test_reported_source_state_truth_table(
    tmp_path, rules, reported, expected_status, expected_signal
):
    """collector が報告した source 状態は、metric が揃っていても優先する。"""
    path = tmp_path / "reported-state.db"
    _table_db(path, rules, {}, nvml_state=reported)
    with TestClient(_app(path, SimulatedClock(NOW_MS), hwmon_metrics=TABLE_HWMON)) as client:
        body = client.get("/api/v1/server-health").json()

    assert body["sources"]["nvml"]["status"] == expected_status
    assert body["signal"] == expected_signal


def test_old_critical_alert_beyond_the_list_limit_is_still_red(healthy_db, rules):
    """一覧は新しい 100 件で打ち切るが、重大度の判定は発生中の全件で行う。"""
    with SqliteStore(healthy_db, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        for index in range(101):
            alert_id = store.open_alert(
                rule_id="OLD_CRITICAL" if index == 0 else f"WARNING_{index}",
                severity="critical" if index == 0 else "warning",
                metric="air.room",
                # index 0 がいちばん古い
                started_ms=NOW_MS - 1_000_000 + index,
                threshold=30.0,
                trigger_value=31.0,
            )
            store.fire_alert(alert_id, fired_ms=NOW_MS, trigger_value=31.0)
        assert store.alert_severity_counts(state="firing") == {
            AlertSeverity.CRITICAL: 1,
            AlertSeverity.WARNING: 100,
        }

    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS))) as client:
        body = client.get("/api/v1/server-health").json()

    assert len(body["active_alerts"]) == 100
    assert "OLD_CRITICAL" not in {alert["rule_id"] for alert in body["active_alerts"]}
    assert body["signal"] == "red"
    assert body["compute_mode_advisory"]["safe"] is False
    assert "more active alerts not listed: critical=1" in body["compute_mode_advisory"]["warnings"]


def test_alert_resolved_between_reads_does_not_split_the_payload(healthy_db, rules, monkeypatch):
    """一覧を読んだ直後に別プロセスが resolve しても、件数は同じ時点のものを使う。"""
    with SqliteStore(healthy_db, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        alert_id = store.open_alert(
            rule_id="RESOLVED_MEANWHILE",
            severity="critical",
            metric="air.room",
            started_ms=NOW_MS - 1_000,
            threshold=30.0,
            trigger_value=31.0,
        )
        store.fire_alert(alert_id, fired_ms=NOW_MS, trigger_value=31.0)

    original = SqliteStore.alerts
    resolved = []

    def alerts_then_resolve_elsewhere(self, **kwargs):
        listed = original(self, **kwargs)
        if not resolved:
            # ルールエンジン（別接続）が、一覧と件数の読み出しの間に resolve する
            with SqliteStore(healthy_db, rules=rules, clock=SimulatedClock(NOW_MS)) as writer:
                writer.resolve_alert(alert_id, resolved_ms=NOW_MS)
            resolved.append(alert_id)
        return listed

    monkeypatch.setattr(SqliteStore, "alerts", alerts_then_resolve_elsewhere)
    with TestClient(_app(healthy_db, SimulatedClock(NOW_MS))) as client:
        during = client.get("/api/v1/server-health").json()
        after = client.get("/api/v1/server-health").json()

    assert resolved == [alert_id]
    # resolve 前の1時点: 一覧に載る critical が signal にも反映される
    assert [alert["rule_id"] for alert in during["active_alerts"]] == ["RESOLVED_MEANWHILE"]
    assert during["signal"] == "red"
    assert "more active alerts not listed" not in " ".join(
        during["compute_mode_advisory"]["warnings"]
    )
    # 次の取得では resolve 後の1時点になる
    assert after["active_alerts"] == []
    assert after["signal"] == "green"


def test_read_snapshot_is_read_only_and_closes_on_error(healthy_db, rules):
    with SqliteStore(healthy_db, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        with pytest.raises(RuntimeError), store.read_snapshot():
            store.latest()
            raise RuntimeError("boom")
        # トランザクションが閉じていれば、続けて書き込める
        store.set_system_state("sys.gpu_mode", "compute", at_ms=NOW_MS + 1)
        assert store.current_state("sys.gpu_mode") == "compute"
