"""#77 simulated Fan Hardware Backend の検証。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.control.config import (
    ConfigSource,
    ConfigSources,
    ControlConfig,
    FanHardwareConfig,
    FanPolicyConfig,
    SafetyConfig,
)
from coldaisle.control.hardware import SimulatedFanBackend, SimulatedFaultPlan
from coldaisle.control.safety import (
    AIR_TELEMETRY_GROUP,
    AIR_TEMPERATURE_METRICS,
    ComposedDemands,
    CriticalSafety,
    DemandComposer,
    create_control_runtime_binding,
    create_emergency_control_runtime,
)
from coldaisle.control.schema import (
    FaultCode,
    GuardZoneOutput,
    OperatingMode,
    PerZone,
    Reason,
    SafetyState,
    Zone,
    ZoneRequest,
)
from coldaisle.control.state import (
    ControlInputContract,
    ControlStateSnapshot,
    CriticalTelemetryGroup,
    FanState,
    SignalSpec,
    SnapshotSignal,
    TelemetryHealth,
    TelemetryImportance,
)
from coldaisle.store.models import Quality


def hardware_config(*, confirmed: bool = True) -> FanHardwareConfig:
    """実機の識別子を含まない、測定済み profile のテスト設定を作る。"""
    profile = {
        "startup_demand": 0.6,
        "minimum_stable_demand": 0.3,
        "maximum_rpm": 1800,
        "pwm_to_rpm": [
            {"demand": 0.3, "rpm": 600},
            {"demand": 0.6, "rpm": 1200},
            {"demand": 1.0, "rpm": 1800},
        ],
        "airflow_index": [0.2, 0.6, 1.0],
    }
    return FanHardwareConfig.model_validate(
        {
            "schema_version": 1,
            "approval": (
                {"status": "confirmed", "basis": "test characterization"}
                if confirmed
                else {"status": "provisional"}
            ),
            "zones": {
                "front": {
                    "driver": "test-superio",
                    "label": "front-header",
                    "pwm_attribute": "pwm1",
                    "tach_attribute": "fan1_input",
                    "enable_attribute": "pwm1_enable",
                    "profile": profile,
                },
                "rear": {
                    "driver": "test-superio",
                    "label": "rear-header",
                    "pwm_attribute": "pwm2",
                    "tach_attribute": "fan2_input",
                    "enable_attribute": "pwm2_enable",
                    "profile": profile,
                },
                "top": {
                    "driver": "test-superio",
                    "label": "top-header",
                    "pwm_attribute": "pwm3",
                    "tach_attribute": "fan3_input",
                    "enable_attribute": "pwm3_enable",
                    "profile": profile,
                },
            },
        }
    )


def safety_config(*, zone_min: float = 0.0) -> SafetyConfig:
    def value(raw: object) -> dict[str, object]:
        return {"value": raw, "status": "provisional"}

    return SafetyConfig.model_validate(
        {
            "schema_version": 2,
            "absolute_temp_ceiling_c": value(85.0),
            "zone_min_demand": {zone.value: value(zone_min) for zone in Zone},
            "cpu_cooling_floor": [
                {"temperature_c": value(40.0), "demand": value(zone_min)},
                {"temperature_c": value(80.0), "demand": value(1.0)},
            ],
            "fault_demand": value(1.0),
            "stall_check_min_demand": {zone.value: value(zone_min) for zone in Zone},
            "stall_min_rpm": {zone.value: value(400) for zone in Zone},
            "stall_window_ms": value(2_000),
            "write_fail_emergency_after": value(3),
            "telemetry": {
                "cpu_ms": value(1_000),
                "gpu_ms": value(1_000),
                "t_sensor": {"enabled": value(False)},
                "air_ms": value(3_000),
                "air_sensor_period_ms": value(2_500),
            },
            "ramp_down_per_s": value(1.0),
            "startup_settle_ms": value(1_000),
            "fault_clear_hold_ms": value(2_000),
            "tick_deadline_ms": value(500),
            "overrun_consecutive_limit": value(3),
            "watchdog_timeout_ms": value(5_000),
        }
    )


def control_config(
    *,
    safety: SafetyConfig | None = None,
    fan_hardware: FanHardwareConfig | None = None,
) -> ControlConfig:
    policy = FanPolicyConfig.model_validate(
        {
            "schema_version": 1,
            "fallback_curve": [
                {"temperature_c": 25.0, "demand": 0.3},
                {"temperature_c": 80.0, "demand": 1.0},
            ],
            "reactive_guard": {
                "floor": {"value": 0.4, "status": "provisional"},
                "ceiling": {"value": 1.0, "status": "provisional"},
                "hold_ms": {"value": 1_000, "status": "provisional"},
                "intake_rise_threshold_c": {"value": 2.0, "status": "provisional"},
                "gpu_hotspot_threshold_c": {"value": 85.0, "status": "provisional"},
            },
            "mpc": {"period_ms": 1_000, "budget_ms": 100, "valid_ms": 2_000},
            "supervisor": {"period_ms": 1_000, "valid_ms": 2_000},
            "gate_min_confidence": {"value": 0.0, "status": "provisional"},
            "authority_stage": "shadow",
            "authority_limits": {
                "limited": {
                    "permitted_zones": ["front"],
                    "limit_up": 0.1,
                    "limit_down": 0.1,
                },
                "expanded": {
                    "permitted_zones": ["front", "rear", "top"],
                    "limit_up": 0.2,
                    "limit_down": 0.2,
                },
            },
            "recovery_hold_ms": 1_000,
            "demote_window_ms": 60_000,
            "demote_after": 3,
        }
    )
    sources = ConfigSources(
        fan_hardware=ConfigSource(name="fan-hardware.yaml", schema_version=1, sha256="1" * 64),
        safety=ConfigSource(name="safety.yaml", schema_version=2, sha256="2" * 64),
        policy=ConfigSource(name="fan-policy.yaml", schema_version=1, sha256="3" * 64),
    )
    return ControlConfig(
        fan_hardware=fan_hardware or hardware_config(),
        safety=safety or safety_config(),
        policy=policy,
        sources=sources,
    )


def input_contract() -> ControlInputContract:
    air_metrics = tuple(sorted(AIR_TEMPERATURE_METRICS))
    return ControlInputContract(
        signals=(
            SignalSpec(
                metric="cpu.package",
                importance=TelemetryImportance.CRITICAL,
                stale_after_ms=1_000,
            ),
            SignalSpec(
                metric="gpu.0.core",
                importance=TelemetryImportance.CRITICAL,
                stale_after_ms=1_000,
            ),
            *(
                SignalSpec(
                    metric=metric,
                    importance=TelemetryImportance.DEGRADED,
                    stale_after_ms=3_000,
                )
                for metric in air_metrics
            ),
        ),
        critical_groups=(CriticalTelemetryGroup(code=AIR_TELEMETRY_GROUP, metrics=air_metrics),),
    )


def ready_snapshot(contract: ControlInputContract, tick: int, mono: int) -> ControlStateSnapshot:
    """全 signal が fresh で、全 fan の tach が応答している snapshot。"""
    signals = tuple(
        SnapshotSignal(
            metric=spec.metric,
            importance=spec.importance,
            enabled=True,
            value=40.0 if spec.metric == "cpu.package" else 25.0,
            quality=Quality.OK,
            source_ts_ms=mono,
            last_changed_mono_ms=mono,
            age_ms=0,
        )
        for spec in contract.signals
    )
    fan = FanState(effective_demand=1.0, rpm=1_000)
    return ControlStateSnapshot(
        tick_id=tick,
        ts_ms=tick,
        monotonic_ms=mono,
        signals=signals,
        derived=(),
        trends=(),
        telemetry_health=TelemetryHealth.NORMAL,
        critical_unavailable=(),
        fans=PerZone(front=fan, rear=fan, top=fan),
    )


def write_hardware_document(path: Path, config: FanHardwareConfig) -> bytes:
    payload = yaml.safe_dump(config.model_dump(mode="json")).encode("utf-8")
    path.write_bytes(payload)
    return payload


def runtime(
    front: float,
    rear: float | None = None,
    top: float | None = None,
    *,
    count: int = 1,
    config: ControlConfig | None = None,
    startup_fault_plan: SimulatedFaultPlan | None = None,
) -> tuple[SimulatedFanBackend, tuple[ComposedDemands, ...]]:
    """STARTUP の全 zone Max を backend に適用した後の、未適用の通常 command 列を返す。

    STARTUP を捨てて NORMAL だけを backend へ渡すと takeover の Max を飛ばしてしまう
    （0028 §2.7）ため、fixture も STARTUP の command を必ず backend へ適用する。
    ``startup_fault_plan`` は STARTUP の間の書き込みだけに適用する。
    """

    def requests(value: float, rear_value: float, top_value: float) -> PerZone[ZoneRequest]:
        return PerZone(
            front=ZoneRequest(demand=value, reason=Reason(code="hardware_test")),
            rear=ZoneRequest(demand=rear_value, reason=Reason(code="hardware_test")),
            top=ZoneRequest(demand=top_value, reason=Reason(code="hardware_test")),
        )

    guard = GuardZoneOutput()
    guards = PerZone(front=guard, rear=guard, top=guard)
    contract = input_contract()

    active_config = config or control_config()
    binding = create_control_runtime_binding(active_config)
    safety = CriticalSafety(
        active_config.safety,
        input_contract=contract,
        runtime_binding=binding,
    )
    backend = SimulatedFanBackend(active_config.fan_hardware, binding)
    startup = safety.evaluate(ready_snapshot(contract, 1, 0), mode=OperatingMode.AUTO)
    composer = DemandComposer(active_config.safety)
    startup_command = composer.compose(
        requested=requests(1.0, 1.0, 1.0),
        guard=guards,
        safety=startup,
        mode=OperatingMode.AUTO,
    )
    # 実際の loop と同じく evaluate → compose → apply を順に行う。Safety は Backend が
    # takeover の Max を書いたことを確認してから settle と tach 応答を数えるため、
    # STARTUP の間の command もすべて Backend に適用する。
    backend.fault_plan = startup_fault_plan or SimulatedFaultPlan()
    backend.apply(startup_command)
    requested = requests(
        front,
        front if rear is None else rear,
        front if top is None else top,
    )
    commands: list[ComposedDemands] = []
    tick = 2
    while len(commands) < count:
        decision = safety.evaluate(
            ready_snapshot(contract, tick, (tick - 1) * 1_000),
            mode=OperatingMode.AUTO,
        )
        command = composer.compose(
            requested=requested,
            guard=guards,
            safety=decision,
            mode=OperatingMode.AUTO,
        )
        if decision.state is SafetyState.STARTUP:
            assert not commands
            backend.apply(command)
        else:
            commands.append(command)
        tick += 1
    backend.fault_plan = SimulatedFaultPlan()
    return backend, tuple(commands)


def test_simulated_backend_controls_three_zones_independently() -> None:
    backend, (command,) = runtime(0.4, 0.7, 1.0)

    results = backend.apply(command)

    # STARTUP の Max の後は各 zone がそれぞれの effective demand に対応する。
    # 各 zone は同じ Interface で独立する。
    assert results.front.target_rpm == 800
    assert results.front.readback.pwm_raw == 102
    assert results.rear.target_rpm == 1350
    assert results.top.target_rpm == 1800
    assert results.front.airflow_index == pytest.approx(1 / 3)
    assert results.rear.airflow_index == 0.7
    assert results.top.readback.pwm_raw == 255
    assert all(result.fault is None for result in (results.front, results.rear, results.top))


def test_startup_max_then_normal_write_never_uses_unsafe_low_pwm() -> None:
    backend, (command,) = runtime(0.0)

    result = backend.apply(command)

    # STARTUP の Max の後は起動済み。profile の 0.3（minimum stable）未満は生成しない。
    assert result.front.readback.pwm_raw == 76
    assert result.front.target_rpm == 600


def test_delayed_startup_command_cannot_be_followed_by_a_precomposed_normal() -> None:
    # 0028 §2.7 / §2.5 (d): takeover の Max を Backend が書くまで Safety は STARTUP の
    # settle も tach 確認も始めない。STARTUP の command を遅らせても、その間に合成した
    # command は Max のままで、Max を書いてから settle と tach 応答を経て初めて下げられる。
    active_config = control_config()
    binding = create_control_runtime_binding(active_config)
    contract = input_contract()
    safety = CriticalSafety(active_config.safety, input_contract=contract, runtime_binding=binding)
    backend = SimulatedFanBackend(active_config.fan_hardware, binding)
    composer = DemandComposer(active_config.safety)
    guard = GuardZoneOutput()
    guards = PerZone(front=guard, rear=guard, top=guard)
    low = PerZone(
        front=ZoneRequest(demand=0.2, reason=Reason(code="hardware_test")),
        rear=ZoneRequest(demand=0.2, reason=Reason(code="hardware_test")),
        top=ZoneRequest(demand=0.2, reason=Reason(code="hardware_test")),
    )

    def step(tick: int, mono: int) -> tuple[SafetyState, ComposedDemands]:
        decision = safety.evaluate(ready_snapshot(contract, tick, mono), mode=OperatingMode.AUTO)
        command = composer.compose(
            requested=low, guard=guards, safety=decision, mode=OperatingMode.AUTO
        )
        return decision.state, command

    _, delayed_startup = step(1, 0)
    # settle 時間が過ぎ tach も見えているが、Backend はまだ Max を書いていない。
    precomposed = [step(tick, (tick - 1) * 1_000) for tick in (2, 3, 4)]
    assert all(state is SafetyState.STARTUP for state, _ in precomposed)

    startup = backend.apply(delayed_startup)
    assert all(startup.get(zone).readback.pwm_raw == 255 for zone in Zone)
    for _, command in precomposed:
        applied = backend.apply(command)
        assert all(applied.get(zone).readback.pwm_raw == 255 for zone in Zone)

    # Max を書いた後の最初の tick で settle を始め、その tick の tach は数えない。
    ack_tick_state, ack_tick_command = step(5, 4_000)
    assert ack_tick_state is SafetyState.STARTUP
    backend.apply(ack_tick_command)
    before_settle_state, before_settle_command = step(6, 4_999)
    assert before_settle_state is SafetyState.STARTUP
    backend.apply(before_settle_command)
    normal_state, normal_command = step(7, 5_000)
    assert normal_state is SafetyState.NORMAL
    # ramp-down で少しずつ下がる。Max を書いてから settle を経た後で初めて Max を外れる。
    assert not any(normal_command.get(zone).forced_max for zone in Zone)
    assert normal_command.front.effective < 1.0
    assert backend.apply(normal_command).front.readback.write_ok is True


def test_failed_startup_write_is_retried_with_startup_kick() -> None:
    backend, (recovery_command,) = runtime(
        0.0,
        startup_fault_plan=SimulatedFaultPlan(write_failure=frozenset({Zone.FRONT})),
    )

    recovered = backend.apply(recovery_command)

    # STARTUP の Max の write 失敗は起動確認ではない。次の command も minimum stable
    # demand ではなく startup kick を使うため、未起動 fan を楽観視しない。
    assert recovered.front.readback.write_ok is True
    assert recovered.front.readback.pwm_raw == 153
    assert recovered.rear.readback.pwm_raw == 76


def test_simulated_failures_are_reported_as_zone_faults_for_critical_safety() -> None:
    backend, (command,) = runtime(0.7)
    backend.fault_plan = SimulatedFaultPlan(
        write_failure=frozenset({Zone.FRONT}),
        readback_mismatch=frozenset({Zone.REAR}),
        tach_stall=frozenset({Zone.TOP}),
    )

    results = backend.apply(command)

    assert results.front.fault is not None
    assert results.front.fault.code is FaultCode.WRITE_FAILURE
    assert results.front.readback.write_ok is False
    assert results.rear.fault is not None
    assert results.rear.fault.code is FaultCode.READBACK_MISMATCH
    assert results.rear.readback.readback_ok is False
    assert results.top.fault is not None
    assert results.top.fault.code is FaultCode.TACH_STALL
    assert results.top.readback.rpm == 0


def test_backend_exposes_no_arbitrary_header_or_hwmon_write_api() -> None:
    backend, _ = runtime(0.5)

    # Protocol の唯一の入口は runtime-bound ComposedDemands。hwmon 番号・path・
    # 任意 attribute を受け取る API が無く、config で検証済みの3 header 以外へ
    # 向ける経路を作れない。
    assert not hasattr(backend, "write_header")
    assert not hasattr(backend, "write_path")


def test_backend_rejects_a_demand_that_did_not_pass_the_composer() -> None:
    backend, _ = runtime(0.5)

    with pytest.raises((AttributeError, TypeError)):
        backend.apply(cast(ComposedDemands, PerZone(front=0.0, rear=0.0, top=0.0)))
    with pytest.raises(TypeError, match="DemandComposer"):
        ComposedDemands()


def test_backend_rejects_replayed_and_out_of_order_composed_commands() -> None:
    backend, (older, newer) = runtime(0.2, count=2)

    backend.apply(newer)
    with pytest.raises(ValueError, match="古い tick"):
        backend.apply(older)

    another_backend, (single,) = runtime(0.2)
    another_backend.apply(single)
    with pytest.raises(ValueError, match="再適用"):
        another_backend.apply(single)


def test_backend_rejects_commands_from_other_config_and_runtime_before_consuming() -> None:
    active_config = control_config(safety=safety_config(zone_min=0.7))
    permissive_config = control_config(safety=safety_config(zone_min=0.0))
    active_backend, _ = runtime(0.7, config=active_config)
    permissive_backend, (permissive_command,) = runtime(0.0, config=permissive_config)

    with pytest.raises(ValueError, match="active control runtime"):
        active_backend.apply(permissive_command)
    permissive_result = permissive_backend.apply(permissive_command)
    assert permissive_result.front.readback.write_ok is True

    other_backend, (same_config_other_runtime_command,) = runtime(0.7, config=active_config)
    with pytest.raises(ValueError, match="active control runtime"):
        active_backend.apply(same_config_other_runtime_command)
    assert other_backend.apply(same_config_other_runtime_command).front.readback.write_ok is True


def test_invalid_config_forced_max_can_reach_the_bound_backend(tmp_path: Path) -> None:
    path = tmp_path / "fan-hardware.yaml"
    payload = write_hardware_document(path, hardware_config())
    runtime = create_emergency_control_runtime(path)
    backend = SimulatedFanBackend(runtime.fan_hardware, runtime.binding)
    composer = DemandComposer.for_invalid_config(runtime.binding)

    command = composer.compose_invalid_config(tick_id=1, monotonic_ms=0)
    result = backend.apply(command)

    assert all(result.get(zone).readback.pwm_raw == 255 for zone in Zone)
    assert runtime.source.sha256 == sha256(payload).hexdigest()


def test_emergency_runtime_rejects_provisional_mapping_and_normal_safety(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fan-hardware.yaml"
    write_hardware_document(path, hardware_config(confirmed=False))
    with pytest.raises(ValueError, match="confirmed"):
        create_emergency_control_runtime(path)

    write_hardware_document(path, hardware_config())
    runtime = create_emergency_control_runtime(path)
    with pytest.raises(ValueError, match="emergency runtime binding"):
        CriticalSafety(
            safety_config(),
            input_contract=input_contract(),
            runtime_binding=runtime.binding,
        )


def test_emergency_runtime_rejects_a_self_asserted_source_hash(tmp_path: Path) -> None:
    path = tmp_path / "fan-hardware.yaml"
    document = hardware_config().model_dump(mode="json")
    document["source_sha256"] = "0" * 64
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        create_emergency_control_runtime(path)


def test_normal_runtime_binding_cannot_switch_to_invalid_config_mode() -> None:
    active_config = control_config()
    binding = create_control_runtime_binding(active_config)

    with pytest.raises(ValueError, match="通常 runtime binding"):
        DemandComposer.for_invalid_config(binding)


def test_normal_runtime_rejects_provisional_fan_mapping() -> None:
    provisional = control_config(fan_hardware=hardware_config(confirmed=False))

    with pytest.raises(ValueError, match="confirmed"):
        create_control_runtime_binding(provisional)
