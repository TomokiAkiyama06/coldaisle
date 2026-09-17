"""#77 simulated Fan Hardware Backend の検証。"""

from __future__ import annotations

from typing import cast

import pytest

from coldaisle.control.config import FanHardwareConfig, SafetyConfig
from coldaisle.control.hardware import SimulatedFanBackend, SimulatedFaultPlan
from coldaisle.control.safety import (
    AIR_TELEMETRY_GROUP,
    AIR_TEMPERATURE_METRICS,
    ComposedDemands,
    CriticalSafety,
    DemandComposer,
)
from coldaisle.control.schema import (
    FaultCode,
    GuardZoneOutput,
    OperatingMode,
    PerZone,
    Reason,
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


def hardware_config() -> FanHardwareConfig:
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
            "approval": {"status": "confirmed", "basis": "test characterization"},
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


def safety_config() -> SafetyConfig:
    def value(raw: object) -> dict[str, object]:
        return {"value": raw, "status": "provisional"}

    return SafetyConfig.model_validate(
        {
            "schema_version": 2,
            "absolute_temp_ceiling_c": value(85.0),
            "zone_min_demand": {zone.value: value(0.0) for zone in Zone},
            "cpu_cooling_floor": [
                {"temperature_c": value(40.0), "demand": value(0.0)},
                {"temperature_c": value(80.0), "demand": value(1.0)},
            ],
            "fault_demand": value(1.0),
            "stall_check_min_demand": {zone.value: value(0.0) for zone in Zone},
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


def composed_many(
    front: float,
    rear: float | None = None,
    top: float | None = None,
    *,
    count: int = 1,
) -> tuple[ComposedDemands, ...]:
    def requests(value: float, rear_value: float, top_value: float) -> PerZone[ZoneRequest]:
        return PerZone(
            front=ZoneRequest(demand=value, reason=Reason(code="hardware_test")),
            rear=ZoneRequest(demand=rear_value, reason=Reason(code="hardware_test")),
            top=ZoneRequest(demand=top_value, reason=Reason(code="hardware_test")),
        )

    guard = GuardZoneOutput()
    guards = PerZone(front=guard, rear=guard, top=guard)
    air_metrics = tuple(sorted(AIR_TEMPERATURE_METRICS))
    contract = ControlInputContract(
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

    def snapshot(tick: int, mono: int) -> ControlStateSnapshot:
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

    config = safety_config()
    safety = CriticalSafety(config, input_contract=contract)
    startup = safety.evaluate(snapshot(1, 0), mode=OperatingMode.AUTO)
    composer = DemandComposer(config)
    composer.compose(
        requested=requests(1.0, 1.0, 1.0),
        guard=guards,
        safety=startup,
        mode=OperatingMode.AUTO,
    )
    requested = requests(
        front,
        front if rear is None else rear,
        front if top is None else top,
    )
    commands = []
    for offset in range(count):
        normal = safety.evaluate(
            snapshot(2 + offset, 1_000 + offset * 1_000),
            mode=OperatingMode.AUTO,
        )
        commands.append(
            composer.compose(
                requested=requested,
                guard=guards,
                safety=normal,
                mode=OperatingMode.AUTO,
            )
        )
    return tuple(commands)


def composed(front: float, rear: float | None = None, top: float | None = None) -> ComposedDemands:
    return composed_many(front, rear, top)[0]


def test_simulated_backend_controls_three_zones_independently() -> None:
    backend = SimulatedFanBackend(hardware_config())

    results = backend.apply(composed(0.4, 0.7, 1.0))

    # 初回は kick のため Front が startup demand まで上がるが、Rear / Top は
    # それぞれの effective demand に対応する。各 zone は同じ Interface で独立する。
    assert results.front.target_rpm == 1200
    assert results.rear.target_rpm == 1350
    assert results.top.target_rpm == 1800
    assert results.front.airflow_index == 0.6
    assert results.rear.airflow_index == 0.7
    assert results.top.readback.pwm_raw == 255
    assert all(result.fault is None for result in (results.front, results.rear, results.top))


def test_first_write_kicks_and_subsequent_write_never_uses_unsafe_low_pwm() -> None:
    backend = SimulatedFanBackend(hardware_config())
    first_command, second_command = composed_many(0.0, count=2)

    first = backend.apply(first_command)
    second = backend.apply(second_command)

    # profile の 0.3（minimum stable）未満を backend が生成しない。初回は 0.6 の kick。
    assert first.front.readback.pwm_raw == 153
    assert second.front.readback.pwm_raw == 76
    assert first.front.target_rpm == 1200
    assert second.front.target_rpm == 600


def test_failed_startup_write_is_retried_with_startup_kick() -> None:
    backend = SimulatedFanBackend(
        hardware_config(),
        SimulatedFaultPlan(write_failure=frozenset({Zone.FRONT})),
    )
    failed_command, recovery_command = composed_many(0.0, count=2)

    failed = backend.apply(failed_command)
    backend.fault_plan = SimulatedFaultPlan()
    recovered = backend.apply(recovery_command)

    # write 失敗は起動確認ではない。fault を解除した再試行も minimum stable
    # demand ではなく startup kick を使うため、未起動 fan を楽観視しない。
    assert failed.front.readback.write_ok is False
    assert failed.front.readback.pwm_raw == 153
    assert recovered.front.readback.write_ok is True
    assert recovered.front.readback.pwm_raw == 153


def test_simulated_failures_are_reported_as_zone_faults_for_critical_safety() -> None:
    backend = SimulatedFanBackend(
        hardware_config(),
        SimulatedFaultPlan(
            write_failure=frozenset({Zone.FRONT}),
            readback_mismatch=frozenset({Zone.REAR}),
            tach_stall=frozenset({Zone.TOP}),
        ),
    )

    results = backend.apply(composed(0.7))

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
    backend = SimulatedFanBackend(hardware_config())

    # Protocol の唯一の入口は PerZone[EffectiveZoneDemand]。hwmon 番号・path・
    # 任意 attribute を受け取る API が無く、config で検証済みの3 header 以外へ
    # 向ける経路を作れない。
    assert not hasattr(backend, "write_header")
    assert not hasattr(backend, "write_path")


def test_backend_rejects_a_demand_that_did_not_pass_the_composer() -> None:
    backend = SimulatedFanBackend(hardware_config())

    with pytest.raises((AttributeError, TypeError)):
        backend.apply(cast(ComposedDemands, PerZone(front=0.0, rear=0.0, top=0.0)))
    with pytest.raises(TypeError, match="DemandComposer"):
        ComposedDemands()


def test_backend_rejects_replayed_and_out_of_order_composed_commands() -> None:
    older, newer = composed_many(0.2, count=2)
    backend = SimulatedFanBackend(hardware_config())

    backend.apply(newer)
    with pytest.raises(ValueError, match="古い tick"):
        backend.apply(older)

    single = composed(0.2)
    another_backend = SimulatedFanBackend(hardware_config())
    another_backend.apply(single)
    with pytest.raises(ValueError, match="再適用"):
        another_backend.apply(single)
