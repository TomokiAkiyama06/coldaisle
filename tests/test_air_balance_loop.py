"""Air Balance を Control Config と control loop へつなぐ（#81 / 決定記録 0073）。

確かめること:

- 未校正の ``air-balance.yaml`` では Air Balance を無効にして起動し、毎 tick ``disabled`` を残す
- 校正済みなら applied demand から ``estimated_flow`` と ``air_balance`` を毎 tick 残す
- 回っていない・確かめられていない Fan に風量を書かない
- Air Balance の有無で Critical Safety と Reactive Guard の結果は変わらない
- ``thermal_inputs`` の metric は入力契約へ加わり、検証できない metric は起動時に拒否する
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.control.air_balance import AirBalanceState, ConfiguredAirBalanceModel
from coldaisle.control.config import CONTROL_CONFIG_VERSION, ControlConfig
from coldaisle.control.hardware import SimulatedFaultPlan
from coldaisle.control.loop import air_balance_input_metrics, build_input_contract
from coldaisle.control.schema import (
    AirBalanceRecord,
    AirBalanceTraceState,
    ControlTick,
    SafetyState,
    Zone,
)
from coldaisle.control.state import TelemetryImportance
from coldaisle.control_daemon import (
    Config,
    ControlConfigInvalidError,
    StartupEnvironmentError,
    build,
)
from coldaisle.metrics import MetricCatalog
from conftest import CONFIG_DIR, QUALITY_RULES_PATH
from test_control_config import (
    calibrated_air_balance_document,
    valid_documents,
    write_documents,
)
from test_control_loop import (
    T_SENSOR_METRIC,
    Harness,
    confirmed_safety_document,
    control_config,
)

METRICS_PATH = CONFIG_DIR / "metrics.yaml"

THERMAL_INPUTS = {
    "gpu_intake_c": "air.gpu_intake",
    "case_delta_c": "d.case_delta",
    "cpu_package_c": "cpu.package",
    "gpu_temperature_c": "gpu.0.mem",
}
"""試験用の束縛。``gpu.0.mem`` は Safety にも fan-policy にも出てこない metric である。"""


@pytest.fixture(scope="module")
def catalog() -> MetricCatalog:
    """**本番と同じ Metric Catalog** を読む。"""
    return MetricCatalog.from_yaml(METRICS_PATH)


def calibrated(thermal_inputs: dict[str, str | None] | None = None) -> ControlConfig:
    return control_config(air_balance=calibrated_air_balance_document(thermal_inputs))


def model_of(config: ControlConfig) -> ConfiguredAirBalanceModel:
    return ConfiguredAirBalanceModel(config.air_balance, config.sources.air_balance.sha256)


# ---------------------------------------------------------------- 起動時の扱い（0073 §2.2）


def test_an_uncalibrated_file_disables_air_balance_on_every_tick(catalog: MetricCatalog) -> None:
    """未校正なら無効で起動し、**推定を1つも記録しない**。起動そのものは止めない。"""
    harness = Harness(catalog)
    results = [harness.settle(), *harness.run(3)]

    for result in results:
        tick = result.tick
        assert tick.schema_version == 12
        record = tick.air_balance
        assert record is not None
        assert record.status == "disabled"
        assert record.disabled_reason == "uncalibrated"
        assert record.state is AirBalanceTraceState.DISABLED
        assert record.config_sha256 == harness.config.sources.air_balance.sha256
        assert record.model_id == harness.config.air_balance.model_id
        for zone in Zone:
            assert tick.zones.get(zone).estimated_flow is None
    # 無効でも applied demand そのものは記録する（backend が実際に書いた値）。
    last = results[-1].tick
    assert all(last.zones.get(zone).applied_demand is not None for zone in Zone)


def test_the_runtime_digest_names_all_four_files_with_their_versions(
    catalog: MetricCatalog,
) -> None:
    """保存された tick だけで4ファイルの版と SHA-256 が言える（0033 §2 / 0073 §2.5 (c)）。"""
    harness = Harness(catalog)
    tick = harness.settle().tick
    runtime = tick.runtime
    assert runtime is not None and runtime.schema_version == 2
    digest = runtime.config
    sources = harness.config.sources
    assert digest.control_config_version == CONTROL_CONFIG_VERSION == 11
    assert (digest.fan_hardware_sha256, digest.fan_hardware_schema_version) == (
        sources.fan_hardware.sha256,
        sources.fan_hardware.schema_version,
    )
    assert (digest.safety_sha256, digest.safety_schema_version) == (
        sources.safety.sha256,
        sources.safety.schema_version,
    )
    assert (digest.policy_sha256, digest.policy_schema_version) == (
        sources.policy.sha256,
        sources.policy.schema_version,
    )
    assert (digest.air_balance_sha256, digest.air_balance_schema_version) == (
        sources.air_balance.sha256,
        2,
    )


def test_a_calibrated_file_records_the_estimate_from_the_applied_demand(
    catalog: MetricCatalog,
) -> None:
    """推定は requested でも effective でもなく **applied demand** から作る（0073 §2.3）。"""
    config = calibrated()
    harness = Harness(catalog, config=config)
    tick = harness.settle().tick
    tick = harness.tick().tick
    record = tick.air_balance
    assert record is not None and record.status == "enabled"
    model = model_of(config)
    for zone in Zone:
        zone_record = tick.zones.get(zone)
        applied = zone_record.applied_demand
        assert applied is not None
        minimum = config.fan_hardware.zones.get(zone).profile.minimum_stable_demand
        assert applied >= minimum
        assert applied >= zone_record.demand.effective
        assert zone_record.estimated_flow == pytest.approx(model.estimate_flow(zone, applied))
        assert zone_record.estimated_flow == record.flows.get(zone)
    assert record.state is not AirBalanceTraceState.UNKNOWN
    assert record.balance_ratio == pytest.approx(
        (record.flows.rear + record.flows.top) / record.flows.front  # type: ignore[operator]
    )


def test_air_balance_never_changes_what_safety_and_guard_decide(catalog: MetricCatalog) -> None:
    """Air Balance は Safety ではない。**有効でも無効でも effective は同じ**（0073 §2.2）。"""
    disabled = Harness(catalog)
    enabled = Harness(catalog, config=calibrated())
    for _ in range(12):
        left = disabled.tick().tick
        right = enabled.tick().tick
        assert left.state.safety_state is right.state.safety_state
        assert left.faults == right.faults
        for zone in Zone:
            assert left.zones.get(zone).demand == right.zones.get(zone).demand


# ------------------------------------------------ 風量を書かない zone（0073 §2.5 (a)）


def test_a_write_failure_leaves_the_flow_unknown_without_dropping_the_record(
    catalog: MetricCatalog,
) -> None:
    harness = Harness(
        catalog,
        config=calibrated(),
        fault_plan=SimulatedFaultPlan(write_failure=frozenset({Zone.FRONT})),
    )
    tick = harness.tick().tick
    front = tick.zones.front
    assert front.hardware is not None and not front.hardware.write_ok
    assert front.applied_demand is None
    assert front.estimated_flow is None
    record = tick.air_balance
    assert record is not None and record.status == "enabled"
    assert record.state is AirBalanceTraceState.UNKNOWN
    assert record.balance_ratio is None
    assert record.q_rear is not None and record.q_top is not None


def test_a_stalled_fan_gets_no_flow_from_the_first_tick(catalog: MetricCatalog) -> None:
    """stall の窓が満ちる前の tick も、**その tick の backend fault**を見て風量を書かない。"""
    harness = Harness(
        catalog,
        config=calibrated(),
        fault_plan=SimulatedFaultPlan(tach_stall=frozenset({Zone.REAR})),
    )
    for _ in range(6):
        tick = harness.tick().tick
        rear = tick.zones.rear
        assert rear.hardware is not None and rear.hardware.write_ok and rear.hardware.readback_ok
        # 書き込みと読み戻しは成功しているので applied demand は残るが、風量は書かない。
        assert rear.applied_demand is not None
        assert rear.estimated_flow is None
        assert tick.air_balance is not None
        assert tick.air_balance.q_rear is None


def test_heat_is_recorded_even_when_the_flow_is_unknown(catalog: MetricCatalog) -> None:
    """**風量が不明という理由で温度の超過を記録から落とさない**（0073 §2.5 (b)）。"""
    config = calibrated(THERMAL_INPUTS)
    harness = Harness(
        catalog,
        config=config,
        fault_plan=SimulatedFaultPlan(write_failure=frozenset({Zone.TOP})),
    )
    limit = config.air_balance.thermal_limits.gpu_intake_c
    harness.telemetry.values["air.gpu_intake"] = limit + 1.0
    record = harness.tick().tick.air_balance
    assert record is not None
    assert record.state is AirBalanceTraceState.UNKNOWN
    assert record.thermal_limited is True
    assert "gpu_intake" in record.thermal_reasons


def test_thermal_inputs_come_from_the_bound_snapshot_metrics(catalog: MetricCatalog) -> None:
    config = calibrated(THERMAL_INPUTS)
    harness = Harness(catalog, config=config)
    harness.settle()
    limit = config.air_balance.thermal_limits.gpu_temperature_c
    harness.telemetry.values["gpu.0.mem"] = limit + 2.0
    record = harness.tick().tick.air_balance
    assert record is not None
    assert record.state is AirBalanceTraceState.THERMALLY_LIMITED
    assert record.thermal_reasons == ("gpu_temperature",)


# ---------------------------------------------------------------- 入力契約（0073 §2.4）


def test_bound_metrics_join_the_input_contract_as_advisory(catalog: MetricCatalog) -> None:
    """契約に無い metric は取り込まれない。**派生値は材料へ展開して加える。**"""
    config = calibrated(THERMAL_INPUTS)
    baseline = {
        spec.metric: spec for spec in build_input_contract(control_config(), catalog).signals
    }
    contract = {spec.metric: spec for spec in build_input_contract(config, catalog).signals}

    assert "gpu.0.mem" not in baseline
    added = contract["gpu.0.mem"]
    assert added.importance is TelemetryImportance.ADVISORY
    assert added.stale_after_ms == config.safety.telemetry.gpu_ms.value
    # 既に契約にある metric は、その重要度・許容遅延のまま使う。
    assert contract["cpu.package"] == baseline["cpu.package"]
    # d.case_delta は材料の metric で契約に入る。
    assert {"air.rear_exhaust", "air.front_intake"} <= set(contract)
    assert "d.case_delta" not in contract


def test_bound_metrics_join_the_contract_even_while_disabled(catalog: MetricCatalog) -> None:
    """無効の間も加える。校正後に初めて拒否が見つかる事態を作らない。"""
    document = valid_documents()["air-balance.yaml"]
    document["thermal_inputs"] = THERMAL_INPUTS
    config = control_config(air_balance=document)
    assert not config.air_balance_enabled
    assert "gpu.0.mem" in {spec.metric for spec in build_input_contract(config, catalog).signals}


@pytest.mark.parametrize(
    ("metric", "message"),
    [
        ("power.gpu.0", "温度"),
        ("air.not_in_catalog", "温度"),
        ("board.chipset", "許容遅延"),
    ],
)
def test_unverifiable_bindings_are_rejected(
    catalog: MetricCatalog, metric: str, message: str
) -> None:
    config = calibrated(THERMAL_INPUTS | {"gpu_intake_c": metric})
    with pytest.raises(ValueError, match=message):
        air_balance_input_metrics(config, catalog)


def test_a_bound_t_sensor_keeps_its_critical_contract(catalog: MetricCatalog) -> None:
    """承認済みの T_SENSOR を束縛しても拒否しない（0073 §2.4: 既存の metric はそのまま使う）。

    T_SENSOR には源の決まらない ``board.*`` も承認できる。契約に新しく加わる metric だけに
    許容遅延を求めないと、契約では CRITICAL として扱える設定を起動時に拒否してしまう。
    """
    config = control_config(
        safety=confirmed_safety_document(),
        air_balance=calibrated_air_balance_document(
            THERMAL_INPUTS | {"gpu_intake_c": T_SENSOR_METRIC}
        ),
    )
    assert T_SENSOR_METRIC in air_balance_input_metrics(
        config, catalog, t_sensor_metric=T_SENSOR_METRIC
    )
    contract = {
        spec.metric: spec
        for spec in build_input_contract(config, catalog, t_sensor_metric=T_SENSOR_METRIC).signals
    }
    assert contract[T_SENSOR_METRIC].importance is TelemetryImportance.CRITICAL
    # T_SENSOR として承認していない ``board.*`` は、契約に新しく加わるので従来どおり拒否する。
    with pytest.raises(ValueError, match="許容遅延"):
        air_balance_input_metrics(config, catalog)


# ---------------------------------------------------------------- coldaisle-fand の起動


def daemon_config(directory: Path, tmp_path: Path) -> Config:
    return Config(
        config_dir=directory,
        db=tmp_path / "control.db",
        metrics=METRICS_PATH,
        quality_rules=QUALITY_RULES_PATH,
        t_sensor_metric=None,
        record_trace=False,
        require_watchdog=False,
    )


def confirmed_documents() -> dict[str, dict[str, Any]]:
    documents = valid_documents()
    documents["fan-hardware.yaml"]["approval"] = {
        "status": "confirmed",
        "basis": "試験用の承認。実機の測定記録ではない",
    }
    return documents


def test_the_daemon_treats_a_missing_air_balance_file_as_invalid_config(tmp_path: Path) -> None:
    """**無いことを「無効」と読まない。** 全 zone Max（`config_invalid`）の条件にする。"""
    directory = tmp_path / "config"
    directory.mkdir()
    documents = confirmed_documents()
    del documents["air-balance.yaml"]
    write_documents(directory, documents)
    with pytest.raises(ControlConfigInvalidError):
        build(daemon_config(directory, tmp_path))


def test_the_daemon_treats_an_unverifiable_binding_as_invalid_config(tmp_path: Path) -> None:
    """Catalog で確かめられない束縛は起動環境ではなく設定の不正（0073 §2.2 / §2.4）。"""
    directory = tmp_path / "config"
    directory.mkdir()
    documents = confirmed_documents()
    documents["air-balance.yaml"]["thermal_inputs"]["gpu_intake_c"] = "board.chipset"
    write_documents(directory, documents)
    with pytest.raises(ControlConfigInvalidError) as raised:
        build(daemon_config(directory, tmp_path))
    assert not isinstance(raised.value, StartupEnvironmentError)


def test_the_daemon_starts_with_a_t_sensor_bound_to_thermal_inputs(tmp_path: Path) -> None:
    """T_SENSOR の metric を ``thermal_inputs`` に束縛しても `config_invalid` にしない。"""
    directory = tmp_path / "config"
    directory.mkdir()
    documents = confirmed_documents()
    documents["safety.yaml"] = confirmed_safety_document()
    documents["air-balance.yaml"] = calibrated_air_balance_document(
        THERMAL_INPUTS | {"gpu_intake_c": T_SENSOR_METRIC}
    )
    write_documents(directory, documents)
    config = daemon_config(directory, tmp_path)
    config = Config(
        config_dir=config.config_dir,
        db=config.db,
        metrics=config.metrics,
        quality_rules=config.quality_rules,
        t_sensor_metric=T_SENSOR_METRIC,
        record_trace=False,
        require_watchdog=False,
    )
    daemon = build(config, watchdog=_NoWatchdog())
    try:
        daemon.run(max_ticks=1)
    finally:
        assert daemon.store is not None
        daemon.store.close()
    assert daemon.stats.ticks == 1


def test_the_daemon_starts_with_air_balance_disabled_when_uncalibrated(tmp_path: Path) -> None:
    directory = tmp_path / "config"
    directory.mkdir()
    write_documents(directory, confirmed_documents())
    daemon = build(daemon_config(directory, tmp_path), watchdog=_NoWatchdog())
    try:
        daemon.run(max_ticks=1)
    finally:
        assert daemon.store is not None
        daemon.store.close()
    assert daemon.stats.ticks == 1


class _NoWatchdog:
    def notify(self) -> None:
        """試験では deadman を持たない。"""


# ---------------------------------------------------------------- 記録の不変条件（0073 §2.5 (b)）


def _enabled_tick() -> dict[str, Any]:
    fixture = Path(__file__).parent / "fixtures" / "control_tick_v11.json"
    loaded = json.loads(fixture.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_v11_fixture_is_a_consistent_enabled_record() -> None:
    tick = ControlTick.model_validate_json(json.dumps(_enabled_tick()))
    assert tick.air_balance is not None and tick.air_balance.status == "enabled"


def test_zone_flows_must_match_the_record_zone_by_zone() -> None:
    """Rear と Top の入れ替わりも検出できる。"""
    payload = _enabled_tick()
    rear = payload["zones"]["rear"]["estimated_flow"]
    top = payload["zones"]["top"]["estimated_flow"]
    payload["zones"]["rear"]["estimated_flow"] = top
    payload["zones"]["top"]["estimated_flow"] = rear
    with pytest.raises(ValidationError, match="estimated_flow"):
        ControlTick.model_validate_json(json.dumps(payload))


def test_the_record_must_name_the_same_air_balance_file_as_the_runtime() -> None:
    payload = _enabled_tick()
    payload["air_balance"]["config_sha256"] = "9" * 64
    with pytest.raises(ValidationError, match="config_sha256"):
        ControlTick.model_validate_json(json.dumps(payload))


def test_v11_requires_the_record_and_a_v2_runtime() -> None:
    payload = _enabled_tick()
    del payload["air_balance"]
    with pytest.raises(ValidationError, match="air_balance が要る"):
        ControlTick.model_validate_json(json.dumps(payload))

    payload = _enabled_tick()
    payload["runtime"]["schema_version"] = 1
    with pytest.raises(ValidationError, match="schema version 2"):
        ControlTick.model_validate_json(json.dumps(payload))


def test_a_legacy_tick_cannot_carry_the_fields_added_in_v11() -> None:
    payload = _enabled_tick()
    payload["schema_version"] = 10
    with pytest.raises(ValidationError, match="schema version 11"):
        ControlTick.model_validate_json(json.dumps(payload))


def test_an_unconfirmed_write_cannot_carry_an_applied_demand() -> None:
    payload = _enabled_tick()
    payload["zones"]["front"]["hardware"]["readback_ok"] = False
    with pytest.raises(ValidationError, match="applied_demand"):
        ControlTick.model_validate_json(json.dumps(payload))


def _disabled_tick() -> dict[str, Any]:
    """未校正で起動した v11（Air Balance を使わず、zone の風量も持たない）。"""
    payload = _enabled_tick()
    record = AirBalanceRecord.disabled(
        model_id=payload["air_balance"]["model_id"],
        config_sha256=payload["air_balance"]["config_sha256"],
    )
    payload["air_balance"] = json.loads(record.model_dump_json())
    for zone in ("front", "rear", "top"):
        payload["zones"][zone]["estimated_flow"] = None
    return payload


@pytest.mark.parametrize("make", [_enabled_tick, _disabled_tick])
@pytest.mark.parametrize("zone", ["front", "rear", "top"])
def test_a_confirmed_write_in_v11_must_carry_an_applied_demand(make: Any, zone: str) -> None:
    """書けて読み戻せた v11 の zone は applied_demand を必ず持つ（codex #4134968263）。

    FanHardwareResult と同じ同値関係。Air Balance が無効でも applied は記録する。
    """
    payload = make()
    ControlTick.model_validate_json(json.dumps(payload))
    payload["zones"][zone].pop("applied_demand")
    payload["zones"][zone]["estimated_flow"] = None
    if payload["air_balance"]["status"] == "enabled":
        # 風量の欠けは Air Balance の記録にも写す（別の検査に落ちないように）
        payload["air_balance"].update(
            {f"q_{zone}": None},
            estimated_intake=None,
            estimated_exhaust=None,
            balance_ratio=None,
            state="unknown",
        )
    with pytest.raises(ValidationError, match=f"{zone}: .*applied_demand が要る"):
        ControlTick.model_validate_json(json.dumps(payload))

    # 確認できなかった zone なら、applied が無いのが正しい形
    payload["zones"][zone]["hardware"]["readback_ok"] = False
    tick = ControlTick.model_validate_json(json.dumps(payload))
    assert tick.zones.get(Zone(zone)).applied_demand is None


@pytest.mark.parametrize("version", [1, 8, 10])
def test_a_legacy_tick_with_a_confirmed_write_needs_no_applied_demand(version: int) -> None:
    """v1〜v10 には applied_demand の欄が無い。**保存済みの記録はそのまま読める。**"""
    fixture = Path(__file__).parent / "fixtures" / f"control_tick_v{version}.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    for zone in payload["zones"].values():
        zone["hardware"] = {"pwm_raw": 99, "rpm": 690, "write_ok": True, "readback_ok": True}
        assert "applied_demand" not in zone
    tick = ControlTick.model_validate_json(json.dumps(payload))
    assert all(tick.zones.get(zone).applied_demand is None for zone in Zone)


def test_a_disabled_record_carries_no_estimate() -> None:
    record = AirBalanceRecord.disabled(model_id="provisional-air-balance", config_sha256="3" * 64)
    payload = json.loads(record.model_dump_json())
    assert payload["thermal_reasons"] == []
    for key in ("q_front", "q_rear", "q_top", "balance_ratio"):
        assert payload[key] is None
    payload["q_front"] = 1.0
    with pytest.raises(ValidationError, match="disabled"):
        AirBalanceRecord.model_validate_json(json.dumps(payload))
    payload = json.loads(record.model_dump_json()) | {"thermal_limited": True}
    with pytest.raises(ValidationError, match="disabled"):
        AirBalanceRecord.model_validate_json(json.dumps(payload))


def test_an_enabled_record_must_come_from_a_calibrated_file() -> None:
    payload = _enabled_tick()["air_balance"] | {"source_status": "uncalibrated"}
    with pytest.raises(ValidationError, match="calibrated"):
        AirBalanceRecord.model_validate_json(json.dumps(payload))


def test_the_trace_state_matches_the_model_state_plus_disabled() -> None:
    """``disabled`` は trace 側の型だけが持つ（0073 §2.5 (b)）。"""
    assert {state.value for state in AirBalanceTraceState} == {
        state.value for state in AirBalanceState
    } | {"disabled"}
    assert "disabled" not in {state.value for state in AirBalanceState}


def test_stalled_zone_in_the_active_faults_cannot_carry_a_flow() -> None:
    payload = _enabled_tick()
    payload["state"]["safety_state"] = "emergency"
    for zone in ("front", "rear", "top"):
        demand = payload["zones"][zone]["demand"]
        demand.update(
            requested=demand["requested"],
            effective=1.0,
            bound_by="forced_max",
            forced_max=True,
            reasons=[{"code": "forced_max", "detail": ""}],
        )
    payload["faults"] = [{"code": "tach_stall", "zone": "top", "detail": ""}]
    with pytest.raises(ValidationError, match="stall"):
        ControlTick.model_validate_json(json.dumps(payload))


def test_safety_state_enum_is_unchanged() -> None:
    """Air Balance の統合で Safety の状態を増やしていない。"""
    assert {state.value for state in SafetyState} == {"startup", "normal", "degraded", "emergency"}
