"""#78 Critical Safety の fault injection と合成優先順位の検証。"""

from __future__ import annotations

import pytest

from coldaisle.control.config import SafetyConfig
from coldaisle.control.safety import (
    AIR_TELEMETRY_GROUP,
    CriticalSafety,
    CriticalSafetyDecision,
    DemandComposer,
    invalid_config_decision,
)
from coldaisle.control.schema import (
    BoundBy,
    Fault,
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
from coldaisle.metrics import MetricCatalog, MetricMeta
from coldaisle.store.models import Quality

PROPOSED_T_SENSOR_METRIC = "board.connector_12v2x6"
AIR_METRICS = (
    "air.front_intake",
    "air.gpu_intake",
    "air.gpu_exhaust",
    "air.top_exhaust",
    "air.rear_exhaust",
)


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
    ramp_down_per_s: float = 0.1,
    uniform_zone_min: float | None = None,
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
    zone_min = (
        {"front": 0.4, "rear": 0.4, "top": 0.5}
        if uniform_zone_min is None
        else {zone.value: uniform_zone_min for zone in Zone}
    )
    stall_check = (
        {"front": 0.4, "rear": 0.4, "top": 0.5}
        if uniform_zone_min is None
        else {zone.value: min(0.4, uniform_zone_min) for zone in Zone}
    )
    curve_floor = 0.5 if uniform_zone_min is None else uniform_zone_min
    return SafetyConfig.model_validate(
        {
            "schema_version": 2,
            "absolute_temp_ceiling_c": value(85.0),
            "zone_min_demand": {zone: value(demand) for zone, demand in zone_min.items()},
            "cpu_cooling_floor": [
                {"temperature_c": value(40.0), "demand": value(curve_floor)},
                {"temperature_c": value(80.0), "demand": value(1.0)},
            ],
            "cpu_power_cooling_floor": [
                {"power_w": value(65.0), "demand": value(curve_floor)},
                {"power_w": value(250.0), "demand": value(1.0)},
            ],
            "fault_demand": value(fault_demand),
            "stall_check_min_demand": {zone: value(demand) for zone, demand in stall_check.items()},
            "stall_min_rpm": {
                "front": value(400),
                "rear": value(400),
                "top": value(400),
            },
            "stall_window_ms": value(2_000),
            "write_fail_emergency_after": value(write_limit),
            "telemetry": {
                "cpu_ms": value(1_000),
                "cpu_power_ms": value(1_000),
                "gpu_ms": value(1_000),
                "t_sensor": t_sensor,
                "air_ms": value(3_000),
                "air_sensor_period_ms": value(2_500),
            },
            "ramp_down_per_s": value(ramp_down_per_s),
            "startup_settle_ms": value(1_000),
            "fault_clear_hold_ms": value(2_000),
            "tick_deadline_ms": value(500),
            "overrun_consecutive_limit": value(3),
            "watchdog_timeout_ms": value(5_000),
        }
    )


def input_contract(*, t_sensor_metric: str | None = None) -> ControlInputContract:
    specs = [
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
        SignalSpec(
            metric="power.cpu.package",
            importance=TelemetryImportance.DEGRADED,
            stale_after_ms=1_000,
        ),
        *(
            [
                SignalSpec(
                    metric=t_sensor_metric,
                    importance=TelemetryImportance.CRITICAL,
                    stale_after_ms=1_000,
                )
            ]
            if t_sensor_metric is not None
            else []
        ),
        *(
            SignalSpec(
                metric=metric,
                importance=TelemetryImportance.DEGRADED,
                stale_after_ms=3_000,
            )
            for metric in AIR_METRICS
        ),
    ]
    return ControlInputContract(
        signals=tuple(specs),
        critical_groups=(CriticalTelemetryGroup(code=AIR_TELEMETRY_GROUP, metrics=AIR_METRICS),),
    )


def critical_safety(
    config: SafetyConfig,
    *,
    approved_t_sensor_metric: str | None = None,
    metric_catalog: MetricCatalog | None = None,
) -> CriticalSafety:
    return CriticalSafety(
        config,
        input_contract=input_contract(t_sensor_metric=approved_t_sensor_metric),
        approved_t_sensor_metric=approved_t_sensor_metric,
        metric_catalog=metric_catalog,
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
    cpu_power: float | None = 50.0,
    critical: tuple[str, ...] = (),
    fan_state: PerZone[FanState] | None = None,
    extra_signals: tuple[SnapshotSignal, ...] = (),
    missing_air: tuple[str, ...] = (),
    telemetry_health: TelemetryHealth = TelemetryHealth.NORMAL,
) -> ControlStateSnapshot:
    unavailable_air = set(AIR_METRICS if AIR_TELEMETRY_GROUP in critical else missing_air)
    raw_signals = (
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
        signal(
            "power.cpu.package",
            cpu_power,
            importance=TelemetryImportance.DEGRADED,
            quality=Quality.OK if cpu_power is not None else Quality.MISSING,
        ),
        *(
            signal(
                metric,
                None if metric in unavailable_air else 25.0,
                importance=TelemetryImportance.DEGRADED,
                quality=Quality.MISSING if metric in unavailable_air else Quality.OK,
            )
            for metric in AIR_METRICS
        ),
        *extra_signals,
    )
    signals = tuple(
        item.model_copy(
            update={
                "last_changed_mono_ms": mono - (item.age_ms or 0),
                "source_ts_ms": mono,
            }
        )
        if item.enabled and item.last_changed_mono_ms is not None
        else item
        for item in raw_signals
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


def t_sensor_catalog(metric: str = PROPOSED_T_SENSOR_METRIC, unit: str = "C") -> MetricCatalog:
    return MetricCatalog(metrics={metric: MetricMeta(unit=unit, label="T_SENSOR")})


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
    tick: int,
    mono: int,
    floor: float = 0.4,
    forced: bool = False,
    state: SafetyState = SafetyState.NORMAL,
) -> CriticalSafetyDecision:
    safety = critical_safety(safety_config(uniform_zone_min=floor))
    if state is SafetyState.EMERGENCY:
        return safety.evaluate(
            snapshot(tick=tick, mono=mono, cpu=85.0),
            mode=OperatingMode.AUTO,
        )
    if tick < 2 or mono < 1_000:
        raise ValueError("NORMAL test decision は先行 STARTUP tick を表現できる時刻にする")
    safety.evaluate(
        snapshot(tick=tick - 1, mono=mono - 1_000, cpu=40.0),
        mode=OperatingMode.AUTO,
    )
    return safety.evaluate(
        snapshot(tick=tick, mono=mono, cpu=40.0),
        mode=OperatingMode.MAX if forced else OperatingMode.AUTO,
    )


def issued_sequence(
    config: SafetyConfig,
    *points: tuple[int, int, OperatingMode],
) -> tuple[CriticalSafetyDecision, ...]:
    safety = critical_safety(config)
    return tuple(
        safety.evaluate(
            snapshot(tick=tick, mono=mono, cpu=40.0),
            mode=mode,
        )
        for tick, mono, mode in points
    )


def test_startup_is_max_until_settle_and_all_tach_have_responded() -> None:
    safety = critical_safety(safety_config())

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


def test_backend_tach_stall_does_not_confirm_startup_tach_even_with_normal_rpm() -> None:
    # readback の RPM が閾値以上でも Backend が stall を報告した tick は tach 応答の
    # 確認に数えない。確認は消えないため、ここで数えると STARTUP を抜けてしまう。
    safety = critical_safety(safety_config())
    backend_fault = (Fault(code=FaultCode.TACH_STALL, zone=Zone.REAR),)

    initial = safety.evaluate(
        snapshot(tick=1, mono=0),
        mode=OperatingMode.AUTO,
        external_faults=backend_fault,
    )
    after_settle = safety.evaluate(
        snapshot(tick=2, mono=1_000, fan_state=fans(rear_rpm=0)),
        mode=OperatingMode.AUTO,
    )
    confirmed = safety.evaluate(snapshot(tick=3, mono=2_000), mode=OperatingMode.AUTO)

    assert initial.state is SafetyState.STARTUP
    assert after_settle.state is SafetyState.STARTUP
    assert all(after_settle.zones.get(zone).forced_max for zone in Zone)
    assert confirmed.state is SafetyState.NORMAL


def test_cpu_cooling_floor_is_interpolated_and_provisional_status_is_preserved() -> None:
    safety = critical_safety(safety_config())
    settled = settle(safety)

    assert settled.zones.top.floor == 0.75
    assert settled.zones.front.floor == 0.4
    assert settled.config_is_provisional is True
    assert critical_safety(safety_config(status="confirmed")).config_is_provisional is False


@pytest.mark.parametrize(("cpu_power", "expected_top_floor"), [(157.5, 0.75), (250.0, 1.0)])
def test_cpu_power_jump_raises_the_top_floor_even_when_the_cpu_is_cool(
    cpu_power: float, expected_top_floor: float
) -> None:
    # 0028 §2.4: cpu_cooling_floor(CPU 温度, CPU Power)。温度が上がる前の Power の立ち上がりで
    # Top の floor を上げる。温度曲線（40C → 0.5）と Power 曲線の大きい方を取る。
    safety = critical_safety(safety_config())
    settle(safety)

    cool_idle = safety.evaluate(
        snapshot(tick=3, mono=2_000, cpu=40.0, cpu_power=50.0), mode=OperatingMode.AUTO
    )
    cool_loaded = safety.evaluate(
        snapshot(tick=4, mono=3_000, cpu=40.0, cpu_power=cpu_power), mode=OperatingMode.AUTO
    )

    assert cool_idle.zones.top.floor == 0.5
    assert cool_loaded.zones.top.floor == pytest.approx(expected_top_floor)
    assert cool_loaded.zones.top.reason is not None
    assert cool_loaded.zones.top.reason.code == "cpu_cooling_floor"
    assert cool_loaded.state is SafetyState.NORMAL
    assert cool_loaded.zones.front.floor == 0.4


def test_hot_cpu_keeps_the_temperature_floor_when_power_is_low() -> None:
    safety = critical_safety(safety_config())
    settle(safety)

    result = safety.evaluate(
        snapshot(tick=3, mono=2_000, cpu=80.0, cpu_power=10.0), mode=OperatingMode.AUTO
    )

    assert result.zones.top.floor == 1.0


@pytest.mark.parametrize("quality", [Quality.MISSING, Quality.STALE, Quality.SUSPECT])
def test_unavailable_cpu_power_is_degraded_and_only_drops_the_power_term(
    quality: Quality,
) -> None:
    # 0029 §2.2 / §2.3: CPU Power の欠測は Degraded。fault_demand にせず safety state も
    # 変えず、Power 項を外して温度の曲線だけで Top の floor を決める。
    safety = critical_safety(safety_config())
    settle(safety)
    unavailable_power = signal(
        "power.cpu.package",
        None,
        importance=TelemetryImportance.DEGRADED,
        quality=quality,
    )
    base = snapshot(
        tick=3,
        mono=2_000,
        cpu=60.0,
        telemetry_health=TelemetryHealth.DEGRADED,
    )
    degraded = base.model_copy(
        update={
            "signals": tuple(
                unavailable_power if item.metric == "power.cpu.package" else item
                for item in base.signals
            )
        }
    )

    result = safety.evaluate(degraded, mode=OperatingMode.AUTO)

    assert result.state is SafetyState.NORMAL
    assert not result.faults
    assert result.zones.top.floor == 0.75
    assert not result.zones.top.forced_max
    assert result.zones.front.floor == 0.4
    assert result.zones.top.reason is not None
    assert "cpu_power_unavailable" in result.zones.top.reason.detail


@pytest.mark.parametrize(
    "importance", [None, TelemetryImportance.CRITICAL, TelemetryImportance.ADVISORY]
)
def test_cpu_power_must_be_a_degraded_signal_in_the_input_contract(
    importance: TelemetryImportance | None,
) -> None:
    contract = input_contract()
    signals = tuple(
        spec
        if spec.metric != "power.cpu.package"
        else spec.model_copy(update={"importance": importance})
        for spec in contract.signals
        if importance is not None or spec.metric != "power.cpu.package"
    )
    with pytest.raises(ValueError, match=r"CRITICAL signal contract|power\.cpu\.package"):
        CriticalSafety(
            safety_config(), input_contract=contract.model_copy(update={"signals": signals})
        )


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
    safety = critical_safety(safety_config(fault_demand=0.9))
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
    safety = critical_safety(safety_config(fault_demand=0.9))
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
    disabled = critical_safety(safety_config(t_sensor_enabled=False, fault_demand=0.9))
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
        critical_safety(safety_config(t_sensor_enabled=True, fault_demand=0.9))

    enabled = critical_safety(
        safety_config(t_sensor_enabled=True, fault_demand=0.9),
        approved_t_sensor_metric=PROPOSED_T_SENSOR_METRIC,
        metric_catalog=t_sensor_catalog(),
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
        snapshot(
            tick=3,
            mono=2_000,
            critical=(PROPOSED_T_SENSOR_METRIC,),
            extra_signals=(signal(PROPOSED_T_SENSOR_METRIC, None, quality=Quality.MISSING),),
        ),
        mode=OperatingMode.AUTO,
    )

    assert disabled_result.state is SafetyState.NORMAL
    assert disabled_result.faults == ()
    assert disabled_result.disabled_inputs[0].code == "t_sensor_disabled"
    assert healthy.state is SafetyState.NORMAL
    assert enabled_result.state is SafetyState.DEGRADED
    assert enabled_result.faults[0].code is FaultCode.T_SENSOR_STALE
    assert enabled_result.zones.front.floor == 0.9


@pytest.mark.parametrize(
    "metric",
    ["cpu.package", "gpu.0.hotspot", AIR_TELEMETRY_GROUP, "not a metric"],
)
def test_t_sensor_contract_rejects_reserved_or_noncanonical_metric(metric: str) -> None:
    with pytest.raises(ValueError):
        critical_safety(
            safety_config(t_sensor_enabled=True),
            approved_t_sensor_metric=metric,
            metric_catalog=t_sensor_catalog(metric),
        )


@pytest.mark.parametrize(("unit", "catalog"), [("W", True), ("C", False)])
def test_t_sensor_contract_requires_a_catalogued_celsius_metric(unit: str, catalog: bool) -> None:
    with pytest.raises(ValueError, match=r"MetricCatalog|温度"):
        critical_safety(
            safety_config(t_sensor_enabled=True),
            approved_t_sensor_metric=PROPOSED_T_SENSOR_METRIC,
            metric_catalog=t_sensor_catalog(unit=unit) if catalog else None,
        )


def test_unknown_critical_input_is_not_silently_ignored() -> None:
    safety = critical_safety(safety_config())

    with pytest.raises(ValueError, match="Critical Safety"):
        safety.evaluate(
            snapshot(tick=1, mono=0, critical=("future.critical",)),
            mode=OperatingMode.AUTO,
        )


def test_snapshot_schema_signal_identity_and_critical_markers_are_revalidated() -> None:
    valid = snapshot(tick=1, mono=0)

    invalid_snapshots = (
        valid.model_copy(update={"schema_version": 999}),
        valid.model_copy(update={"signals": (*valid.signals, valid.signals[0])}),
        valid.model_copy(update={"critical_unavailable": ("cpu.package",)}),
        snapshot(tick=1, mono=0, cpu=None),
        valid.model_copy(update={"critical_unavailable": (AIR_TELEMETRY_GROUP,)}),
    )

    for invalid in invalid_snapshots:
        with pytest.raises(ValueError):
            critical_safety(safety_config()).evaluate(invalid, mode=OperatingMode.AUTO)


def test_quality_ok_signal_cannot_hide_stale_future_or_inconsistent_age() -> None:
    valid = snapshot(tick=1, mono=2_000)
    cpu = valid.signals[0]
    invalid_cpu_signals = (
        cpu.model_copy(update={"age_ms": 1_001, "last_changed_mono_ms": 999}),
        cpu.model_copy(update={"age_ms": 0, "last_changed_mono_ms": 2_001}),
        cpu.model_copy(update={"age_ms": 1, "last_changed_mono_ms": 2_000}),
    )

    for invalid_cpu in invalid_cpu_signals:
        malformed = valid.model_copy(update={"signals": (invalid_cpu, *valid.signals[1:])})
        with pytest.raises(ValueError):
            critical_safety(safety_config()).evaluate(malformed, mode=OperatingMode.AUTO)


def test_air_group_contract_and_snapshot_marker_must_be_consistent() -> None:
    contract_without_group = input_contract().model_copy(update={"critical_groups": ()})
    with pytest.raises(ValueError, match="air_telemetry"):
        CriticalSafety(safety_config(), input_contract=contract_without_group)

    safety = critical_safety(safety_config(fault_demand=0.9))
    settle(safety)
    with pytest.raises(ValueError, match="critical_unavailable"):
        safety.evaluate(
            snapshot(tick=3, mono=2_000, missing_air=AIR_METRICS),
            mode=OperatingMode.AUTO,
        )


def test_input_contract_stale_limits_must_come_from_safety_config() -> None:
    contract = input_contract()
    mismatched_cpu = contract.signals[0].model_copy(update={"stale_after_ms": 60_000})
    mismatched = contract.model_copy(update={"signals": (mismatched_cpu, *contract.signals[1:])})

    with pytest.raises(ValueError, match="stale limit"):
        CriticalSafety(safety_config(), input_contract=mismatched)


def test_absolute_temperature_limit_forces_emergency_max() -> None:
    safety = critical_safety(safety_config())
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


@pytest.mark.parametrize("quality", [Quality.STALE, Quality.MISSING, None])
def test_absolute_temperature_limit_does_not_clear_while_the_reading_is_unavailable(
    quality: Quality | None,
) -> None:
    # 上限を超えた metric が stale / missing / 消失になっても解消とみなさない。
    # fresh な上限未満の値が fault_clear_hold_ms の間続いてから解除する。
    safety = critical_safety(safety_config())
    settle(safety)

    def hotspot(value: float | None, q: Quality = Quality.OK) -> tuple[SnapshotSignal, ...]:
        return (signal("gpu.0.hotspot", value, importance=TelemetryImportance.ADVISORY, quality=q),)

    unavailable = () if quality is None else hotspot(None, quality)
    hot = safety.evaluate(
        snapshot(tick=3, mono=2_000, extra_signals=hotspot(85.0)),
        mode=OperatingMode.AUTO,
    )
    held = [
        safety.evaluate(
            snapshot(tick=tick, mono=mono, extra_signals=unavailable),
            mode=OperatingMode.AUTO,
        )
        for tick, mono in ((4, 3_000), (5, 10_000), (6, 20_000))
    ]
    fresh_below = safety.evaluate(
        snapshot(tick=7, mono=21_000, extra_signals=hotspot(60.0)),
        mode=OperatingMode.AUTO,
    )
    before_hold = safety.evaluate(
        snapshot(tick=8, mono=22_999, extra_signals=hotspot(60.0)),
        mode=OperatingMode.AUTO,
    )
    cleared = safety.evaluate(
        snapshot(tick=9, mono=23_000, extra_signals=hotspot(60.0)),
        mode=OperatingMode.AUTO,
    )

    assert hot.state is SafetyState.EMERGENCY
    for decision in held:
        assert decision.state is SafetyState.EMERGENCY
        assert decision.faults[0].code is FaultCode.ABSOLUTE_TEMPERATURE_LIMIT
        assert "gpu.0.hotspot" in decision.faults[0].detail
    assert fresh_below.state is SafetyState.EMERGENCY
    assert before_hold.state is SafetyState.EMERGENCY
    assert cleared.state is SafetyState.NORMAL
    assert not cleared.faults


def test_absolute_temperature_clear_hold_restarts_if_the_reading_becomes_unavailable() -> None:
    safety = critical_safety(safety_config())
    settle(safety)

    def hotspot(value: float | None, q: Quality = Quality.OK) -> tuple[SnapshotSignal, ...]:
        return (signal("gpu.0.hotspot", value, importance=TelemetryImportance.ADVISORY, quality=q),)

    safety.evaluate(
        snapshot(tick=3, mono=2_000, extra_signals=hotspot(85.0)), mode=OperatingMode.AUTO
    )
    safety.evaluate(
        snapshot(tick=4, mono=3_000, extra_signals=hotspot(60.0)), mode=OperatingMode.AUTO
    )
    safety.evaluate(
        snapshot(tick=5, mono=4_000, extra_signals=hotspot(None, Quality.STALE)),
        mode=OperatingMode.AUTO,
    )
    still = safety.evaluate(
        snapshot(tick=6, mono=5_000, extra_signals=hotspot(60.0)), mode=OperatingMode.AUTO
    )
    cleared = safety.evaluate(
        snapshot(tick=7, mono=7_000, extra_signals=hotspot(60.0)), mode=OperatingMode.AUTO
    )

    # tick 4 から hold を数えると tick 6 で解除されるが、tick 5 の stale で hold が
    # やり直しになるため、tick 6 の fresh 値から fault_clear_hold_ms 後に解除する。
    assert still.state is SafetyState.EMERGENCY
    assert cleared.state is SafetyState.NORMAL


def test_deprecated_bare_chipset_name_is_not_treated_as_temperature() -> None:
    safety = critical_safety(safety_config())
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
    safety = critical_safety(safety_config(fault_demand=0.9))
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
    safety = critical_safety(safety_config())
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
    safety = critical_safety(safety_config(fault_demand=0.9))
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


@pytest.mark.parametrize(
    ("zone", "expected_state"),
    [(Zone.FRONT, SafetyState.DEGRADED), (Zone.TOP, SafetyState.EMERGENCY)],
)
def test_backend_tach_stall_fault_waits_for_the_stall_window(
    zone: Zone, expected_state: SafetyState
) -> None:
    # 0028 §2.7 / 0034 §2: stall は window の間続いたときだけ fault。Backend の
    # TACH_STALL 1回で即 DEGRADED / EMERGENCY にしない。
    safety = critical_safety(safety_config(fault_demand=0.9))
    settle(safety)
    backend_fault = (Fault(code=FaultCode.TACH_STALL, zone=zone),)
    rpm_zero = fans(
        front_rpm=0 if zone is Zone.FRONT else 1_000,
        top_rpm=0 if zone is Zone.TOP else 1_000,
    )

    first = safety.evaluate(
        snapshot(tick=3, mono=2_000, fan_state=rpm_zero),
        mode=OperatingMode.AUTO,
        external_faults=backend_fault,
    )
    before_window = safety.evaluate(
        snapshot(tick=4, mono=3_999, fan_state=rpm_zero),
        mode=OperatingMode.AUTO,
        external_faults=backend_fault,
    )
    faulted = safety.evaluate(
        snapshot(tick=5, mono=4_000, fan_state=rpm_zero),
        mode=OperatingMode.AUTO,
        external_faults=backend_fault,
    )

    assert first.state is SafetyState.NORMAL
    assert not first.faults
    assert before_window.state is SafetyState.NORMAL
    assert not before_window.faults
    assert faulted.state is expected_state
    assert [fault.code for fault in faulted.faults] == [FaultCode.TACH_STALL]
    assert "backend_tach_stall=true" in faulted.faults[0].detail
    assert faulted.zones.get(zone).forced_max


def test_backend_tach_stall_keeps_the_timer_even_if_readback_rpm_looks_normal() -> None:
    # Backend が stall を報告した tick は readback RPM を楽観的に信じて timer を消さない。
    safety = critical_safety(safety_config())
    settle(safety)
    backend_fault = (Fault(code=FaultCode.TACH_STALL, zone=Zone.FRONT),)

    safety.evaluate(
        snapshot(tick=3, mono=2_000, fan_state=fans(front_rpm=0)),
        mode=OperatingMode.AUTO,
    )
    safety.evaluate(
        snapshot(tick=4, mono=3_000, fan_state=fans(front_rpm=1_000)),
        mode=OperatingMode.AUTO,
        external_faults=backend_fault,
    )
    faulted = safety.evaluate(
        snapshot(tick=5, mono=4_000, fan_state=fans(front_rpm=0)),
        mode=OperatingMode.AUTO,
    )

    assert faulted.state is SafetyState.DEGRADED
    assert faulted.faults[0].code is FaultCode.TACH_STALL


def test_backend_tach_stall_below_check_demand_is_not_a_fault() -> None:
    safety = critical_safety(safety_config())
    settle(safety)
    backend_fault = (Fault(code=FaultCode.TACH_STALL, zone=Zone.FRONT),)

    for tick, mono in ((3, 2_000), (4, 5_000)):
        decision = safety.evaluate(
            snapshot(tick=tick, mono=mono, fan_state=fans(front_rpm=0, front_demand=0.3)),
            mode=OperatingMode.AUTO,
            external_faults=backend_fault,
        )
    assert decision.state is SafetyState.NORMAL
    assert not decision.faults


def test_low_and_unavailable_rpm_share_one_continuous_stall_timer() -> None:
    safety = critical_safety(safety_config())
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


def test_missing_effective_demand_keeps_the_existing_stall_timer() -> None:
    safety = critical_safety(safety_config())
    settle(safety)
    safety.evaluate(
        snapshot(tick=3, mono=2_000, fan_state=fans(front_rpm=0)),
        mode=OperatingMode.AUTO,
    )
    missing_demand = fans(front_rpm=0).model_copy(
        update={"front": FanState(effective_demand=None, rpm=0)}
    )

    faulted = safety.evaluate(
        snapshot(tick=4, mono=4_000, fan_state=missing_demand),
        mode=OperatingMode.AUTO,
    )

    assert faulted.state is SafetyState.DEGRADED
    assert faulted.faults[0].code is FaultCode.TACH_STALL
    assert "demand=0.6" in faulted.faults[0].detail


def test_all_fan_readback_loss_uses_last_effective_demand_and_stalls() -> None:
    safety = critical_safety(safety_config())
    settle(safety)

    started = safety.evaluate(
        snapshot(tick=3, mono=2_000).model_copy(update={"fans": None}),
        mode=OperatingMode.AUTO,
    )
    before_window = safety.evaluate(
        snapshot(tick=4, mono=3_999).model_copy(update={"fans": None}),
        mode=OperatingMode.AUTO,
    )
    stalled = safety.evaluate(
        snapshot(tick=5, mono=4_000).model_copy(update={"fans": None}),
        mode=OperatingMode.AUTO,
    )

    assert started.state is SafetyState.NORMAL
    assert before_window.state is SafetyState.NORMAL
    assert stalled.state is SafetyState.EMERGENCY
    assert {(fault.code, fault.zone) for fault in stalled.faults} == {
        (FaultCode.TACH_STALL, zone) for zone in Zone
    }
    assert all("fan_readback=unavailable" in fault.detail for fault in stalled.faults)


def test_all_fan_readback_loss_does_not_invent_a_higher_last_demand() -> None:
    safety = critical_safety(safety_config())
    settle(safety)
    safety.evaluate(
        snapshot(
            tick=3,
            mono=2_000,
            fan_state=fans(front_demand=0.3),
        ),
        mode=OperatingMode.AUTO,
    )
    safety.evaluate(
        snapshot(tick=4, mono=3_000).model_copy(update={"fans": None}),
        mode=OperatingMode.AUTO,
    )
    stalled = safety.evaluate(
        snapshot(tick=5, mono=5_000).model_copy(update={"fans": None}),
        mode=OperatingMode.AUTO,
    )

    tach_zones = {fault.zone for fault in stalled.faults if fault.code is FaultCode.TACH_STALL}
    assert tach_zones == {Zone.REAR, Zone.TOP}


def test_top_stall_is_immediate_emergency_after_the_stall_window() -> None:
    safety = critical_safety(safety_config())
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
def test_front_backend_fault_retries_at_max_and_escalates_after_configured_count(
    code: FaultCode,
) -> None:
    safety = critical_safety(safety_config(fault_demand=0.9, write_limit=3))
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


@pytest.mark.parametrize(
    "code",
    [FaultCode.WRITE_FAILURE, FaultCode.READBACK_MISMATCH, FaultCode.ENABLE_REVERTED],
)
def test_top_write_or_readback_failure_is_immediate_emergency(code: FaultCode) -> None:
    safety = critical_safety(safety_config())
    settle(safety)

    result = safety.evaluate(
        snapshot(tick=3, mono=2_000),
        mode=OperatingMode.AUTO,
        external_faults=(Fault(code=code, zone=Zone.TOP),),
    )

    assert result.state is SafetyState.EMERGENCY
    assert all(result.zones.get(zone).forced_max for zone in Zone)


def test_tick_overrun_escalates_only_after_configured_consecutive_count() -> None:
    safety = critical_safety(safety_config())
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
    safety = critical_safety(safety_config(fault_demand=0.9))
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
    safety = critical_safety(safety_config())
    settle(safety)
    manual_max = safety.evaluate(snapshot(tick=3, mono=2_000), mode=OperatingMode.MAX)
    invalid = invalid_config_decision(tick_id=3, monotonic_ms=2_000)

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

    config = safety_config(uniform_zone_min=0.6)
    startup, normal, next_normal = issued_sequence(
        config,
        (1, 0, OperatingMode.AUTO),
        (2, 10_000, OperatingMode.AUTO),
        (3, 20_000, OperatingMode.AUTO),
    )
    composer = DemandComposer(config)
    composer.compose(
        requested=requests(0.2),
        guard=empty_guard(),
        safety=startup,
        mode=OperatingMode.AUTO,
    )
    result = composer.compose(
        requested=requests(0.8),
        guard=constrained_guard,
        safety=normal,
        mode=OperatingMode.AUTO,
    )
    tie = composer.compose(
        requested=requests(0.6),
        guard=empty_guard(),
        safety=next_normal,
        mode=OperatingMode.AUTO,
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

    config = safety_config(uniform_zone_min=0.6)
    startup, normal, next_normal = issued_sequence(
        config,
        (1, 0, mode),
        (2, 10_000, mode),
        (3, 20_000, mode),
    )
    composer = DemandComposer(config)
    composer.compose(
        requested=requests(0.2),
        guard=empty_guard(),
        safety=startup,
        mode=mode,
    )
    high = composer.compose(
        requested=requests(0.8),
        guard=guard,
        safety=normal,
        mode=mode,
    )
    low = composer.compose(
        requested=requests(0.1),
        guard=guard,
        safety=next_normal,
        mode=mode,
    )

    assert high.front.effective == 0.8
    assert high.front.guard_ceiling is None
    assert low.front.effective == 0.6
    assert low.front.bound_by is BoundBy.SAFETY_FLOOR


def test_ramp_down_is_limited_but_ramp_up_is_not() -> None:
    config = safety_config(uniform_zone_min=0.0)
    startup, normal, next_normal, final_normal = issued_sequence(
        config,
        (1, 0, OperatingMode.AUTO),
        (2, 2_000, OperatingMode.AUTO),
        (3, 3_000, OperatingMode.AUTO),
        (4, 4_000, OperatingMode.AUTO),
    )
    composer = DemandComposer(config)
    composer.compose(
        requested=requests(0.2),
        guard=empty_guard(),
        safety=startup,
        mode=OperatingMode.AUTO,
    )
    baseline = composer.compose(
        requested=requests(0.8),
        guard=empty_guard(),
        safety=normal,
        mode=OperatingMode.AUTO,
    )
    down = composer.compose(
        requested=requests(0.2),
        guard=empty_guard(),
        safety=next_normal,
        mode=OperatingMode.AUTO,
    )
    up = composer.compose(
        requested=requests(0.9),
        guard=empty_guard(),
        safety=final_normal,
        mode=OperatingMode.AUTO,
    )

    assert baseline.front.effective == pytest.approx(0.8)
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

    config = safety_config()
    (startup,) = issued_sequence(config, (1, 0, OperatingMode.AUTO))
    result = DemandComposer(config).compose(
        requested=requests(0.2),
        guard=guard,
        safety=startup,
        mode=OperatingMode.AUTO,
    )

    assert result.front.effective == 1.0
    assert result.front.bound_by is BoundBy.FORCED_MAX
    assert result.front.reasons


def test_composer_owns_previous_state_and_rejects_bypass_or_time_reuse() -> None:
    config = safety_config()
    startup, normal = issued_sequence(
        config,
        (1, 0, OperatingMode.AUTO),
        (2, 1_000, OperatingMode.AUTO),
    )
    with pytest.raises(ValueError, match="forced Max"):
        DemandComposer(config).compose(
            requested=requests(0.5),
            guard=empty_guard(),
            safety=normal,
            mode=OperatingMode.AUTO,
        )
    _, manual_max = issued_sequence(
        config,
        (1, 0, OperatingMode.AUTO),
        (2, 1_000, OperatingMode.MAX),
    )
    with pytest.raises(ValueError, match="STARTUP / EMERGENCY"):
        DemandComposer(config).compose(
            requested=requests(0.5),
            guard=empty_guard(),
            safety=manual_max,
            mode=OperatingMode.AUTO,
        )
    composer = DemandComposer(config)
    composer.compose(
        requested=requests(0.5),
        guard=empty_guard(),
        safety=startup,
        mode=OperatingMode.AUTO,
    )
    with pytest.raises(ValueError, match="tick"):
        composer.compose(
            requested=requests(0.5),
            guard=empty_guard(),
            safety=startup,
            mode=OperatingMode.AUTO,
        )


def test_serialized_or_directly_constructed_safety_decision_cannot_authorize_backend() -> None:
    issued = decision(tick=1, mono=0, forced=True, state=SafetyState.EMERGENCY)
    untrusted = CriticalSafetyDecision.model_validate(issued.model_dump(mode="python"))
    tampered = issued.model_copy(update={"tick_id": 99, "monotonic_ms": 99_000})

    for rejected in (untrusted, tampered):
        with pytest.raises(ValueError, match="発行していない"):
            DemandComposer(safety_config()).compose(
                requested=requests(0.0),
                guard=empty_guard(),
                safety=rejected,
                mode=OperatingMode.AUTO,
            )


def test_composer_rejects_cross_config_and_cross_evaluator_decisions() -> None:
    config = safety_config()
    startup_a, _ = issued_sequence(
        config,
        (1, 0, OperatingMode.AUTO),
        (2, 1_000, OperatingMode.AUTO),
    )
    _, normal_b = issued_sequence(
        config,
        (1, 0, OperatingMode.AUTO),
        (2, 1_000, OperatingMode.AUTO),
    )
    composer = DemandComposer(config)
    composer.compose(
        requested=requests(0.5),
        guard=empty_guard(),
        safety=startup_a,
        mode=OperatingMode.AUTO,
    )
    with pytest.raises(ValueError, match="異なる CriticalSafety"):
        composer.compose(
            requested=requests(0.5),
            guard=empty_guard(),
            safety=normal_b,
            mode=OperatingMode.AUTO,
        )

    other_config = safety_config(ramp_down_per_s=0.9)
    (other_startup,) = issued_sequence(other_config, (1, 0, OperatingMode.AUTO))
    with pytest.raises(ValueError, match="設定が一致しない"):
        DemandComposer(config).compose(
            requested=requests(0.5),
            guard=empty_guard(),
            safety=other_startup,
            mode=OperatingMode.AUTO,
        )


def test_invalid_config_composer_needs_no_config_and_can_only_repeat_forced_max() -> None:
    composer = DemandComposer.for_invalid_config()

    first = composer.compose_invalid_config(tick_id=1, monotonic_ms=0)
    repeated = composer.compose_invalid_config(tick_id=2, monotonic_ms=100_000)

    for output in (first, repeated):
        assert all(output.get(zone).effective == 1.0 for zone in Zone)
        assert all(output.get(zone).bound_by is BoundBy.FORCED_MAX for zone in Zone)
    with pytest.raises(ValueError, match="compose_invalid_config"):
        composer.compose(
            requested=requests(0.0),
            guard=empty_guard(),
            safety=decision(tick=3, mono=100_001, forced=True, state=SafetyState.EMERGENCY),
            mode=OperatingMode.AUTO,
        )
    with pytest.raises(ValueError, match="config-invalid"):
        DemandComposer(safety_config()).compose(
            requested=requests(0.0),
            guard=empty_guard(),
            safety=invalid_config_decision(tick_id=1, monotonic_ms=0),
            mode=OperatingMode.AUTO,
        )
    with pytest.raises(ValueError, match="検証済み設定"):
        DemandComposer(safety_config()).compose_invalid_config(tick_id=1, monotonic_ms=0)


def test_bare_stateless_composition_is_not_part_of_the_public_safety_api() -> None:
    import coldaisle.control.safety as safety_api

    assert not hasattr(safety_api, "compose_effective_demands")


def test_tick_order_cannot_move_backwards_or_repeat() -> None:
    safety = critical_safety(safety_config())
    safety.evaluate(snapshot(tick=1, mono=1_000), mode=OperatingMode.AUTO)

    with pytest.raises(ValueError, match="tick"):
        safety.evaluate(snapshot(tick=1, mono=2_000), mode=OperatingMode.AUTO)
