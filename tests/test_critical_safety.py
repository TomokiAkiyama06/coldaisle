"""#78 Critical Safety の fault injection と合成優先順位の検証。"""

from __future__ import annotations

import math

import pytest

from coldaisle.control.config import SafetyConfig
from coldaisle.control.safety import (
    AIR_TELEMETRY_GROUP,
    CriticalSafety,
    CriticalSafetyDecision,
    compose_effective_demands,
    invalid_config_decision,
)
from coldaisle.control.schema import (
    BoundBy,
    EffectiveZoneDemand,
    Fault,
    FaultCode,
    GuardZoneOutput,
    OperatingMode,
    PerZone,
    Reason,
    SafetyState,
    SafetyZoneOutput,
    Zone,
    ZoneRequest,
)
from coldaisle.control.state import (
    ControlStateSnapshot,
    FanState,
    SnapshotSignal,
    TelemetryHealth,
    TelemetryImportance,
)
from coldaisle.store.models import Quality

PROPOSED_T_SENSOR_METRIC = "board.connector_12v2x6"


def tracked(value: object, status: str = "provisional") -> dict[str, object]:
    result: dict[str, object] = {"value": value, "status": status}
    if status == "confirmed":
        result["basis"] = "test characterization"
    return result


def safety_config(
    *,
    status: str = "provisional",
    t_sensor_enabled: bool = False,
    fault_demand: float = 1.0,
    write_limit: int = 3,
) -> SafetyConfig:
    def value(raw: object) -> dict[str, object]:
        return tracked(raw, status)

    t_sensor: dict[str, object] = {
        "enabled": tracked(
            t_sensor_enabled,
            "confirmed" if t_sensor_enabled else status,
        )
    }
    if t_sensor_enabled:
        t_sensor["stale_after_ms"] = value(1_000)
    return SafetyConfig.model_validate(
        {
            "schema_version": 1,
            "absolute_temp_ceiling_c": value(85.0),
            "zone_min_demand": {
                "front": value(0.4),
                "rear": value(0.4),
                "top": value(0.5),
            },
            "cpu_cooling_floor": [
                {"temperature_c": value(40.0), "demand": value(0.5)},
                {"temperature_c": value(80.0), "demand": value(1.0)},
            ],
            "fault_demand": value(fault_demand),
            "stall_check_min_demand": {
                "front": value(0.4),
                "rear": value(0.4),
                "top": value(0.5),
            },
            "stall_min_rpm": {
                "front": value(400),
                "rear": value(400),
                "top": value(400),
            },
            "stall_window_ms": value(2_000),
            "write_fail_emergency_after": value(write_limit),
            "telemetry": {
                "cpu_ms": value(1_000),
                "gpu_ms": value(1_000),
                "t_sensor": t_sensor,
                "air_ms": value(3_000),
                "air_sensor_period_ms": value(2_500),
            },
            "ramp_down_per_s": value(0.1),
            "startup_settle_ms": value(1_000),
            "fault_clear_hold_ms": value(2_000),
            "tick_deadline_ms": value(500),
            "overrun_consecutive_limit": value(3),
            "watchdog_timeout_ms": value(5_000),
        }
    )


def signal(
    metric: str,
    value: float | None,
    *,
    importance: TelemetryImportance = TelemetryImportance.CRITICAL,
    quality: Quality = Quality.OK,
    enabled: bool = True,
) -> SnapshotSignal:
    return SnapshotSignal(
        metric=metric,
        importance=importance,
        enabled=enabled,
        value=value,
        quality=quality,
        source_ts_ms=1_000 if enabled else None,
        last_changed_mono_ms=1_000 if enabled else None,
        age_ms=0 if enabled else None,
    )


def fans(
    *,
    front_rpm: int | None = 1_000,
    rear_rpm: int | None = 1_000,
    top_rpm: int | None = 1_000,
    front_demand: float = 0.6,
    rear_demand: float = 0.6,
    top_demand: float = 0.6,
) -> PerZone[FanState]:
    return PerZone(
        front=FanState(effective_demand=front_demand, rpm=front_rpm),
        rear=FanState(effective_demand=rear_demand, rpm=rear_rpm),
        top=FanState(effective_demand=top_demand, rpm=top_rpm),
    )


def snapshot(
    *,
    tick: int,
    mono: int,
    cpu: float | None = 60.0,
    gpu: float | None = 60.0,
    critical: tuple[str, ...] = (),
    fan_state: PerZone[FanState] | None = None,
    extra_signals: tuple[SnapshotSignal, ...] = (),
    telemetry_health: TelemetryHealth = TelemetryHealth.NORMAL,
) -> ControlStateSnapshot:
    signals = (
        signal(
            "cpu.package",
            cpu,
            quality=Quality.OK if cpu is not None else Quality.MISSING,
        ),
        signal(
            "gpu.0.core",
            gpu,
            quality=Quality.OK if gpu is not None else Quality.MISSING,
        ),
        *extra_signals,
    )
    return ControlStateSnapshot(
        tick_id=tick,
        ts_ms=1_700_000_000_000 + tick,
        monotonic_ms=mono,
        signals=signals,
        derived=(),
        trends=(),
        telemetry_health=telemetry_health,
        critical_unavailable=critical,
        fans=fan_state if fan_state is not None else fans(),
    )


def settle(safety: CriticalSafety) -> CriticalSafetyDecision:
    first = safety.evaluate(snapshot(tick=1, mono=0), mode=OperatingMode.AUTO)
    assert first.state is SafetyState.STARTUP
    return safety.evaluate(snapshot(tick=2, mono=1_000), mode=OperatingMode.AUTO)


def empty_guard() -> PerZone[GuardZoneOutput]:
    item = GuardZoneOutput()
    return PerZone(front=item, rear=item, top=item)


def requests(
    front: float, rear: float | None = None, top: float | None = None
) -> PerZone[ZoneRequest]:
    def item(demand: float) -> ZoneRequest:
        return ZoneRequest(demand=demand, reason=Reason(code="test_request"))

    return PerZone(
        front=item(front),
        rear=item(front if rear is None else rear),
        top=item(front if top is None else top),
    )


def decision(
    *,
    floor: float = 0.4,
    forced: bool = False,
    state: SafetyState = SafetyState.NORMAL,
) -> CriticalSafetyDecision:
    reason = Reason(code="test_safety")
    item = SafetyZoneOutput(floor=floor, forced_max=forced, reason=reason)
    faults = (Fault(code=FaultCode.CONFIG_INVALID),) if state is SafetyState.EMERGENCY else ()
    return CriticalSafetyDecision(
        state=state,
        zones=PerZone(front=item, rear=item, top=item),
        faults=faults,
        config_validated=True,
        config_is_provisional=True,
    )


def previous(demand: float) -> PerZone[EffectiveZoneDemand]:
    item = EffectiveZoneDemand(
        requested=demand,
        effective=demand,
        bound_by=BoundBy.REQUESTED,
        safety_floor=0.0,
        forced_max=False,
    )
    return PerZone(front=item, rear=item, top=item)


def test_startup_is_max_until_settle_and_all_tach_have_responded() -> None:
    safety = CriticalSafety(safety_config())

    initial = safety.evaluate(
        snapshot(tick=1, mono=0, fan_state=fans(rear_rpm=0)),
        mode=OperatingMode.AUTO,
    )
    not_all_tach = safety.evaluate(
        snapshot(tick=2, mono=1_000, fan_state=fans(rear_rpm=0)),
        mode=OperatingMode.AUTO,
    )
    normal = safety.evaluate(snapshot(tick=3, mono=2_000), mode=OperatingMode.AUTO)

    assert initial.state is SafetyState.STARTUP
    assert not_all_tach.state is SafetyState.STARTUP
    assert normal.state is SafetyState.NORMAL
    assert all(initial.zones.get(zone).forced_max for zone in Zone)
    assert not any(normal.zones.get(zone).forced_max for zone in Zone)


def test_cpu_cooling_floor_is_interpolated_and_provisional_status_is_preserved() -> None:
    safety = CriticalSafety(safety_config())
    settled = settle(safety)

    assert settled.zones.top.floor == 0.75
    assert settled.zones.front.floor == 0.4
    assert settled.config_is_provisional is True
    assert CriticalSafety(safety_config(status="confirmed")).config_is_provisional is False


@pytest.mark.parametrize(
    ("missing", "fault", "forced_zones", "fault_floor_zones"),
    [
        ("cpu.package", FaultCode.CPU_TELEMETRY_STALE, {Zone.TOP}, set()),
        (
            "gpu.0.core",
            FaultCode.GPU_TELEMETRY_STALE,
            set(),
            {Zone.FRONT, Zone.REAR},
        ),
        (
            AIR_TELEMETRY_GROUP,
            FaultCode.AIR_TELEMETRY_STALE,
            set(),
            {Zone.FRONT, Zone.REAR},
        ),
    ],
)
def test_critical_telemetry_faults_apply_only_the_decided_response(
    missing: str,
    fault: FaultCode,
    forced_zones: set[Zone],
    fault_floor_zones: set[Zone],
) -> None:
    safety = CriticalSafety(safety_config(fault_demand=0.9))
    settle(safety)
    result = safety.evaluate(
        snapshot(
            tick=3,
            mono=2_000,
            cpu=None if missing == "cpu.package" else 60.0,
            gpu=None if missing == "gpu.0.core" else 60.0,
            critical=(missing,),
        ),
        mode=OperatingMode.AUTO,
    )

    assert result.state is SafetyState.DEGRADED
    assert {item.code for item in result.faults} == {fault}
    for zone in Zone:
        assert result.zones.get(zone).forced_max is (zone in forced_zones)
        if zone in fault_floor_zones:
            assert result.zones.get(zone).floor == 0.9


def test_partial_air_loss_changes_health_but_not_safety_state_or_demand() -> None:
    safety = CriticalSafety(safety_config(fault_demand=0.9))
    settle(safety)

    result = safety.evaluate(
        snapshot(
            tick=3,
            mono=2_000,
            telemetry_health=TelemetryHealth.DEGRADED,
        ),
        mode=OperatingMode.AUTO,
    )

    assert result.state is SafetyState.NORMAL
    assert result.faults == ()
    assert result.zones.front.floor == 0.4


def test_t_sensor_disabled_is_ignored_but_enabled_loss_is_critical() -> None:
    disabled = CriticalSafety(safety_config(t_sensor_enabled=False, fault_demand=0.9))
    settle(disabled)
    disabled_result = disabled.evaluate(
        snapshot(
            tick=3,
            mono=2_000,
            extra_signals=(signal(PROPOSED_T_SENSOR_METRIC, 100.0),),
        ),
        mode=OperatingMode.AUTO,
    )

    with pytest.raises(ValueError, match="metric contract"):
        CriticalSafety(safety_config(t_sensor_enabled=True, fault_demand=0.9))

    enabled = CriticalSafety(
        safety_config(t_sensor_enabled=True, fault_demand=0.9),
        approved_t_sensor_metric=PROPOSED_T_SENSOR_METRIC,
    )
    t_sensor = (signal(PROPOSED_T_SENSOR_METRIC, 60.0),)
    enabled.evaluate(
        snapshot(tick=1, mono=0, extra_signals=t_sensor),
        mode=OperatingMode.AUTO,
    )
    healthy = enabled.evaluate(
        snapshot(tick=2, mono=1_000, extra_signals=t_sensor),
        mode=OperatingMode.AUTO,
    )
    enabled_result = enabled.evaluate(
        snapshot(tick=3, mono=2_000, critical=(PROPOSED_T_SENSOR_METRIC,)),
        mode=OperatingMode.AUTO,
    )

    assert disabled_result.state is SafetyState.NORMAL
    assert disabled_result.faults == ()
    assert disabled_result.disabled_inputs[0].code == "t_sensor_disabled"
    assert healthy.state is SafetyState.NORMAL
    assert enabled_result.state is SafetyState.DEGRADED
    assert enabled_result.faults[0].code is FaultCode.T_SENSOR_STALE
    assert enabled_result.zones.front.floor == 0.9


def test_unknown_critical_input_is_not_silently_ignored() -> None:
    safety = CriticalSafety(safety_config())

    with pytest.raises(ValueError, match="Critical Safety"):
        safety.evaluate(
            snapshot(tick=1, mono=0, critical=("future.critical",)),
            mode=OperatingMode.AUTO,
        )


def test_absolute_temperature_limit_forces_emergency_max() -> None:
    safety = CriticalSafety(safety_config())
    settle(safety)
    hotspot = signal(
        "gpu.0.hotspot",
        85.0,
        importance=TelemetryImportance.ADVISORY,
    )

    result = safety.evaluate(
        snapshot(tick=3, mono=2_000, extra_signals=(hotspot,)),
        mode=OperatingMode.AUTO,
    )

    assert result.state is SafetyState.EMERGENCY
    assert result.faults[0].code is FaultCode.ABSOLUTE_TEMPERATURE_LIMIT
    assert all(result.zones.get(zone).forced_max for zone in Zone)


def test_deprecated_bare_chipset_name_is_not_treated_as_temperature() -> None:
    safety = CriticalSafety(safety_config())
    settle(safety)

    result = safety.evaluate(
        snapshot(
            tick=3,
            mono=2_000,
            extra_signals=(signal("chipset", 100.0),),
        ),
        mode=OperatingMode.AUTO,
    )

    assert result.state is SafetyState.NORMAL
    assert result.faults == ()


def test_front_stall_uses_monotonic_window_then_maxes_zone_and_raises_others() -> None:
    safety = CriticalSafety(safety_config(fault_demand=0.9))
    settle(safety)

    first_low = safety.evaluate(
        snapshot(tick=3, mono=2_000, fan_state=fans(front_rpm=0)),
        mode=OperatingMode.AUTO,
    )
    before_window = safety.evaluate(
        snapshot(tick=4, mono=3_999, fan_state=fans(front_rpm=0)),
        mode=OperatingMode.AUTO,
    )
    stalled = safety.evaluate(
        snapshot(tick=5, mono=4_000, fan_state=fans(front_rpm=0)),
        mode=OperatingMode.AUTO,
    )

    assert first_low.state is SafetyState.NORMAL
    assert before_window.state is SafetyState.NORMAL
    assert stalled.state is SafetyState.DEGRADED
    assert stalled.faults[0].code is FaultCode.TACH_STALL
    assert stalled.zones.front.forced_max
    assert stalled.zones.rear.floor == 0.9
    assert stalled.zones.top.floor == 0.9


def test_stall_is_not_counted_below_configured_demand() -> None:
    safety = CriticalSafety(safety_config())
    settle(safety)

    safety.evaluate(
        snapshot(
            tick=3,
            mono=2_000,
            fan_state=fans(front_rpm=0, front_demand=0.3),
        ),
        mode=OperatingMode.AUTO,
    )
    low_demand = safety.evaluate(
        snapshot(
            tick=4,
            mono=5_000,
            fan_state=fans(front_rpm=0, front_demand=0.3),
        ),
        mode=OperatingMode.AUTO,
    )
    assert low_demand.state is SafetyState.NORMAL


@pytest.mark.parametrize(
    ("zone", "expected_state"),
    [(Zone.FRONT, SafetyState.DEGRADED), (Zone.TOP, SafetyState.EMERGENCY)],
)
def test_unavailable_rpm_uses_the_stall_window_and_safe_response(
    zone: Zone, expected_state: SafetyState
) -> None:
    safety = CriticalSafety(safety_config(fault_demand=0.9))
    settle(safety)

    def unavailable() -> PerZone[FanState]:
        return fans(
            front_rpm=None if zone is Zone.FRONT else 1_000,
            top_rpm=None if zone is Zone.TOP else 1_000,
        )

    first = safety.evaluate(
        snapshot(tick=3, mono=2_000, fan_state=unavailable()),
        mode=OperatingMode.AUTO,
    )
    before_window = safety.evaluate(
        snapshot(tick=4, mono=3_999, fan_state=unavailable()),
        mode=OperatingMode.AUTO,
    )
    faulted = safety.evaluate(
        snapshot(tick=5, mono=4_000, fan_state=unavailable()),
        mode=OperatingMode.AUTO,
    )

    assert first.state is SafetyState.NORMAL
    assert before_window.state is SafetyState.NORMAL
    assert faulted.state is expected_state
    assert faulted.faults[0].code is FaultCode.TACH_STALL
    assert "rpm=unavailable" in faulted.faults[0].detail
    assert faulted.zones.get(zone).forced_max


def test_low_and_unavailable_rpm_share_one_continuous_stall_timer() -> None:
    safety = CriticalSafety(safety_config())
    settle(safety)
    safety.evaluate(
        snapshot(tick=3, mono=2_000, fan_state=fans(front_rpm=0)),
        mode=OperatingMode.AUTO,
    )
    faulted = safety.evaluate(
        snapshot(tick=4, mono=4_000, fan_state=fans(front_rpm=None)),
        mode=OperatingMode.AUTO,
    )

    assert faulted.state is SafetyState.DEGRADED
    assert faulted.faults[0].code is FaultCode.TACH_STALL


def test_top_stall_is_immediate_emergency_after_the_stall_window() -> None:
    safety = CriticalSafety(safety_config())
    settle(safety)
    safety.evaluate(
        snapshot(tick=3, mono=2_000, fan_state=fans(top_rpm=0)),
        mode=OperatingMode.AUTO,
    )

    result = safety.evaluate(
        snapshot(tick=4, mono=4_000, fan_state=fans(top_rpm=0)),
        mode=OperatingMode.AUTO,
    )

    assert result.state is SafetyState.EMERGENCY
    assert all(result.zones.get(zone).forced_max for zone in Zone)


@pytest.mark.parametrize(
    "code",
    [FaultCode.WRITE_FAILURE, FaultCode.READBACK_MISMATCH, FaultCode.ENABLE_REVERTED],
)
def test_front_hardware_fault_retries_at_max_and_escalates_after_configured_count(
    code: FaultCode,
) -> None:
    safety = CriticalSafety(safety_config(fault_demand=0.9, write_limit=3))
    settle(safety)
    fault = Fault(code=code, zone=Zone.FRONT)

    first = safety.evaluate(
        snapshot(tick=3, mono=2_000),
        mode=OperatingMode.AUTO,
        external_faults=(fault,),
    )
    second = safety.evaluate(
        snapshot(tick=4, mono=3_000),
        mode=OperatingMode.AUTO,
        external_faults=(fault,),
    )
    third = safety.evaluate(
        snapshot(tick=5, mono=4_000),
        mode=OperatingMode.AUTO,
        external_faults=(fault,),
    )

    assert first.state is SafetyState.DEGRADED
    assert second.state is SafetyState.DEGRADED
    assert first.zones.front.forced_max
    assert first.zones.rear.floor == 0.9
    assert third.state is SafetyState.EMERGENCY
    assert all(third.zones.get(zone).forced_max for zone in Zone)


@pytest.mark.parametrize("code", [FaultCode.WRITE_FAILURE, FaultCode.READBACK_MISMATCH])
def test_top_write_or_readback_failure_is_immediate_emergency(code: FaultCode) -> None:
    safety = CriticalSafety(safety_config())
    settle(safety)

    result = safety.evaluate(
        snapshot(tick=3, mono=2_000),
        mode=OperatingMode.AUTO,
        external_faults=(Fault(code=code, zone=Zone.TOP),),
    )

    assert result.state is SafetyState.EMERGENCY
    assert all(result.zones.get(zone).forced_max for zone in Zone)


def test_tick_overrun_escalates_only_after_configured_consecutive_count() -> None:
    safety = CriticalSafety(safety_config())
    settle(safety)

    first = safety.evaluate(
        snapshot(tick=3, mono=2_000), mode=OperatingMode.AUTO, tick_overrun=True
    )
    second = safety.evaluate(
        snapshot(tick=4, mono=3_000), mode=OperatingMode.AUTO, tick_overrun=True
    )
    third = safety.evaluate(
        snapshot(tick=5, mono=4_000), mode=OperatingMode.AUTO, tick_overrun=True
    )

    assert first.state is SafetyState.NORMAL
    assert second.state is SafetyState.NORMAL
    assert third.state is SafetyState.EMERGENCY
    assert third.faults[0].code is FaultCode.TICK_OVERRUN


def test_fault_clear_hold_keeps_the_safer_state_until_full_monotonic_hold() -> None:
    safety = CriticalSafety(safety_config(fault_demand=0.9))
    settle(safety)
    missing = safety.evaluate(
        snapshot(tick=3, mono=2_000, gpu=None, critical=("gpu.0.core",)),
        mode=OperatingMode.AUTO,
    )
    first_clear = safety.evaluate(snapshot(tick=4, mono=3_000), mode=OperatingMode.AUTO)
    before_hold = safety.evaluate(snapshot(tick=5, mono=4_999), mode=OperatingMode.AUTO)
    held = safety.evaluate(snapshot(tick=6, mono=5_000), mode=OperatingMode.AUTO)

    assert missing.state is SafetyState.DEGRADED
    assert first_clear.state is SafetyState.DEGRADED
    assert before_hold.state is SafetyState.DEGRADED
    assert held.state is SafetyState.NORMAL


def test_manual_max_and_invalid_config_cannot_express_a_lower_demand() -> None:
    safety = CriticalSafety(safety_config())
    settle(safety)
    manual_max = safety.evaluate(snapshot(tick=3, mono=2_000), mode=OperatingMode.MAX)
    invalid = invalid_config_decision()

    assert all(manual_max.zones.get(zone).forced_max for zone in Zone)
    assert invalid.state is SafetyState.EMERGENCY
    assert invalid.faults[0].code is FaultCode.CONFIG_INVALID
    assert invalid.config_validated is False
    assert all(invalid.zones.get(zone).floor == 1.0 for zone in Zone)


def test_floor_wins_over_auto_guard_ceiling_and_tie_uses_safety_precedence() -> None:
    guard_reason = Reason(code="guard_test")
    constrained_guard = PerZone(
        front=GuardZoneOutput(
            floor=0.5,
            ceiling=0.3,
            hold_until_mono_ms=10_000,
            reason=guard_reason,
        ),
        rear=GuardZoneOutput(),
        top=GuardZoneOutput(),
    )

    result = compose_effective_demands(
        requested=requests(0.8),
        guard=constrained_guard,
        safety=decision(floor=0.6),
        mode=OperatingMode.AUTO,
        previous=None,
        elapsed_ms=1_000,
        ramp_down_per_s=0.1,
    )
    tie = compose_effective_demands(
        requested=requests(0.6),
        guard=empty_guard(),
        safety=decision(floor=0.6),
        mode=OperatingMode.AUTO,
        previous=None,
        elapsed_ms=1_000,
        ramp_down_per_s=0.1,
    )

    assert result.front.effective == 0.6
    assert result.front.bound_by is BoundBy.SAFETY_FLOOR
    assert result.front.guard_ceiling == 0.3
    assert tie.front.bound_by is BoundBy.SAFETY_FLOOR


@pytest.mark.parametrize("mode", [OperatingMode.MANUAL, OperatingMode.CALIBRATION])
def test_manual_and_calibration_ignore_ceiling_but_keep_guard_and_safety_floors(
    mode: OperatingMode,
) -> None:
    guard_reason = Reason(code="guard_test")
    guard = PerZone(
        front=GuardZoneOutput(
            floor=0.5,
            ceiling=0.2,
            hold_until_mono_ms=10_000,
            reason=guard_reason,
        ),
        rear=GuardZoneOutput(),
        top=GuardZoneOutput(),
    )

    high = compose_effective_demands(
        requested=requests(0.8),
        guard=guard,
        safety=decision(floor=0.4),
        mode=mode,
        previous=None,
        elapsed_ms=1_000,
        ramp_down_per_s=0.1,
    )
    low = compose_effective_demands(
        requested=requests(0.1),
        guard=guard,
        safety=decision(floor=0.6),
        mode=mode,
        previous=None,
        elapsed_ms=1_000,
        ramp_down_per_s=0.1,
    )

    assert high.front.effective == 0.8
    assert high.front.guard_ceiling is None
    assert low.front.effective == 0.6
    assert low.front.bound_by is BoundBy.SAFETY_FLOOR


def test_ramp_down_is_limited_but_ramp_up_is_not() -> None:
    down = compose_effective_demands(
        requested=requests(0.2),
        guard=empty_guard(),
        safety=decision(floor=0.0),
        mode=OperatingMode.AUTO,
        previous=previous(0.8),
        elapsed_ms=1_000,
        ramp_down_per_s=0.1,
    )
    up = compose_effective_demands(
        requested=requests(0.9),
        guard=empty_guard(),
        safety=decision(floor=0.0),
        mode=OperatingMode.AUTO,
        previous=previous(0.4),
        elapsed_ms=1_000,
        ramp_down_per_s=0.1,
    )

    assert down.front.effective == pytest.approx(0.7)
    assert down.front.bound_by is BoundBy.RAMP_DOWN
    assert up.front.effective == 0.9
    assert up.front.bound_by is BoundBy.REQUESTED


def test_forced_max_wins_over_every_constraint_and_is_structured() -> None:
    guard_reason = Reason(code="guard_test")
    guard = PerZone(
        front=GuardZoneOutput(
            ceiling=0.1,
            hold_until_mono_ms=10_000,
            reason=guard_reason,
        ),
        rear=GuardZoneOutput(),
        top=GuardZoneOutput(),
    )

    result = compose_effective_demands(
        requested=requests(0.2),
        guard=guard,
        safety=decision(floor=0.4, forced=True, state=SafetyState.EMERGENCY),
        mode=OperatingMode.AUTO,
        previous=previous(0.5),
        elapsed_ms=1_000,
        ramp_down_per_s=0.1,
    )

    assert result.front.effective == 1.0
    assert result.front.bound_by is BoundBy.FORCED_MAX
    assert result.front.reasons


def test_composition_rejects_invalid_timing_values() -> None:
    with pytest.raises(ValueError, match="elapsed_ms"):
        compose_effective_demands(
            requested=requests(0.5),
            guard=empty_guard(),
            safety=decision(),
            mode=OperatingMode.AUTO,
            previous=None,
            elapsed_ms=-1,
            ramp_down_per_s=0.1,
        )
    with pytest.raises(ValueError, match="ramp_down_per_s"):
        compose_effective_demands(
            requested=requests(0.5),
            guard=empty_guard(),
            safety=decision(),
            mode=OperatingMode.AUTO,
            previous=None,
            elapsed_ms=1,
            ramp_down_per_s=math.inf,
        )


def test_tick_order_cannot_move_backwards_or_repeat() -> None:
    safety = CriticalSafety(safety_config())
    safety.evaluate(snapshot(tick=1, mono=1_000), mode=OperatingMode.AUTO)

    with pytest.raises(ValueError, match="tick"):
        safety.evaluate(snapshot(tick=1, mono=2_000), mode=OperatingMode.AUTO)
