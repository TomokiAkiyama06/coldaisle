"""#80 Reactive Guard。ControlStateSnapshot だけを使う非 hardware 試験。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coldaisle.control.config import ReactiveGuardConfig
from coldaisle.control.reactive import (
    GuardThresholdProfile,
    GuardTransition,
    ReactiveGuard,
)
from coldaisle.control.schema import (
    BoundBy,
    EffectiveZoneDemand,
    GuardZoneOutput,
    PerZone,
    Reason,
)
from coldaisle.control.state import (
    ControlInputContract,
    ControlInputFrame,
    ControlStateEstimator,
    ControlStateSnapshot,
    DerivedSignal,
    SignalSpec,
    SnapshotSignal,
    TelemetryHealth,
    TelemetryImportance,
    TelemetryReading,
    Trend,
)
from coldaisle.metrics import MetricCatalog, MetricMeta
from coldaisle.store.models import Quality


def provisional(value: float | int) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def confirmed(value: str) -> dict[str, object]:
    return {"value": value, "status": "confirmed", "basis": "approved metric contract"}


def band(
    activate: float,
    clear: float,
    degraded_activate: float,
    degraded_clear: float,
) -> dict[str, object]:
    return {
        "activate_above": provisional(activate),
        "clear_at_or_below": provisional(clear),
        "degraded_activate_above": provisional(degraded_activate),
        "degraded_clear_at_or_below": provisional(degraded_clear),
    }


def config(
    *,
    hold_ms: int = 5_000,
    cpu_power_metric: str | None = None,
) -> ReactiveGuardConfig:
    return ReactiveGuardConfig.model_validate(
        {
            "floor": provisional(0.6),
            "hold_ms": provisional(hold_ms),
            "cpu_power_metric": (None if cpu_power_metric is None else confirmed(cpu_power_metric)),
            "cpu_temperature_rate_c_per_s": band(2.0, 1.0, 1.5, 0.5),
            "gpu_temperature_rate_c_per_s": band(2.0, 1.0, 1.5, 0.5),
            "cpu_power_rate_w_per_s": band(15.0, 5.0, 10.0, 2.5),
            "gpu_power_rate_w_per_s": band(15.0, 5.0, 10.0, 2.5),
            "intake_rise_c": band(3.0, 2.0, 2.5, 1.5),
            "gpu_hotspot_c": band(85.0, 80.0, 82.0, 78.0),
        }
    )


def catalog() -> MetricCatalog:
    return MetricCatalog(
        metrics={
            "power.cpu.package": MetricMeta(unit="W", label="cpu power"),
            "power.cpu.cores": MetricMeta(unit="C", label="wrong unit"),
        }
    )


def signal(
    metric: str,
    value: float | None,
    *,
    quality: Quality = Quality.OK,
    changed: int = 1_000,
) -> SnapshotSignal:
    return SnapshotSignal(
        metric=metric,
        importance=TelemetryImportance.DEGRADED,
        enabled=True,
        value=value,
        quality=quality,
        source_ts_ms=1_000,
        last_changed_mono_ms=changed,
        age_ms=0,
    )


def snapshot(
    *,
    tick: int,
    mono: int,
    signals: tuple[SnapshotSignal, ...] = (),
    trends: tuple[Trend, ...] = (),
    derived: tuple[DerivedSignal, ...] = (),
    health: TelemetryHealth = TelemetryHealth.NORMAL,
    ts_ms: int = 10_000,
) -> ControlStateSnapshot:
    return ControlStateSnapshot(
        tick_id=tick,
        ts_ms=ts_ms,
        monotonic_ms=mono,
        signals=signals,
        derived=derived,
        trends=trends,
        telemetry_health=health,
        critical_unavailable=(),
    )


def temperature_snapshot(
    *,
    tick: int,
    mono: int,
    rate: float | None,
    quality: Quality = Quality.OK,
    health: TelemetryHealth = TelemetryHealth.NORMAL,
    ts_ms: int = 10_000,
) -> ControlStateSnapshot:
    trends = (
        ()
        if rate is None
        else (
            Trend(
                metric="gpu.0.core",
                per_second=rate,
                from_mono_ms=max(0, mono - 1_000),
                to_mono_ms=mono,
            ),
        )
    )
    return snapshot(
        tick=tick,
        mono=mono,
        signals=(signal("gpu.0.core", 60.0, quality=quality, changed=mono),),
        trends=trends,
        health=health,
        ts_ms=ts_ms,
    )


def test_gpu_temperature_rise_raises_front_and_rear_in_the_same_snapshot_tick() -> None:
    decision = ReactiveGuard(config(), catalog()).evaluate(
        temperature_snapshot(tick=7, mono=10_000, rate=2.5)
    )

    assert decision.tick_id == 7
    assert decision.monotonic_ms == 10_000
    assert decision.zones.front.floor == pytest.approx(0.6)
    assert decision.zones.rear.floor == pytest.approx(0.6)
    assert decision.zones.top == GuardZoneOutput()
    assert decision.zones.front.ceiling is None, "上昇時に冷却を ceiling で抑えない"
    assert {event.zone.value for event in decision.events} == {"front", "rear"}
    assert all(event.transition is GuardTransition.STARTED for event in decision.events)


def test_cpu_temperature_rise_only_raises_the_top_floor() -> None:
    decision = ReactiveGuard(config(), catalog()).evaluate(
        snapshot(
            tick=1,
            mono=2_000,
            signals=(signal("cpu.package", 70.0, changed=2_000),),
            trends=(
                Trend(
                    metric="cpu.package",
                    per_second=3.0,
                    from_mono_ms=1_000,
                    to_mono_ms=2_000,
                ),
            ),
        )
    )

    assert decision.zones.front == GuardZoneOutput()
    assert decision.zones.rear == GuardZoneOutput()
    assert decision.zones.top.floor == pytest.approx(0.6)


def test_cpu_power_step_raises_top_without_waiting_for_a_temperature_limit() -> None:
    decision = ReactiveGuard(config(cpu_power_metric="power.cpu.package"), catalog()).evaluate(
        snapshot(
            tick=1,
            mono=2_000,
            signals=(signal("power.cpu.package", 180.0, changed=2_000),),
            trends=(
                Trend(
                    metric="power.cpu.package",
                    per_second=20.0,
                    from_mono_ms=1_000,
                    to_mono_ms=2_000,
                ),
            ),
        )
    )

    assert decision.zones.top.floor == pytest.approx(0.6)
    assert decision.zones.front == GuardZoneOutput()
    assert any(item.code == "cpu_power_rise" for item in decision.evidence)


def test_unapproved_cpu_power_metric_keeps_the_trigger_disabled() -> None:
    decision = ReactiveGuard(config(), catalog()).evaluate(
        snapshot(
            tick=1,
            mono=2_000,
            signals=(signal("power.cpu.package", 180.0, changed=2_000),),
            trends=(
                Trend(
                    metric="power.cpu.package",
                    per_second=20.0,
                    from_mono_ms=1_000,
                    to_mono_ms=2_000,
                ),
            ),
        )
    )

    assert decision.zones.top == GuardZoneOutput()
    assert all(item.code != "cpu_power_rise" for item in decision.evidence)
    assert "power.cpu.package" not in decision.unavailable_inputs


@pytest.mark.parametrize(
    ("signals", "derived", "evidence_code"),
    [
        ((), (DerivedSignal(metric="d.intake_rise", value=3.5),), "intake_rise"),
        ((signal("gpu.0.hotspot", 86.0),), (), "gpu_hotspot_high"),
    ],
)
def test_absolute_snapshot_triggers_raise_the_gpu_airflow_zones(
    signals: tuple[SnapshotSignal, ...],
    derived: tuple[DerivedSignal, ...],
    evidence_code: str,
) -> None:
    decision = ReactiveGuard(config(), catalog()).evaluate(
        snapshot(tick=1, mono=1_000, signals=signals, derived=derived)
    )

    assert decision.zones.front.floor == pytest.approx(0.6)
    assert decision.zones.rear.floor == pytest.approx(0.6)
    assert any(item.code == evidence_code and item.active for item in decision.evidence)


def test_power_slope_uses_the_actual_sample_interval_not_the_control_tick_interval() -> None:
    contract = ControlInputContract(
        signals=(
            SignalSpec(
                metric="power.gpu.0",
                importance=TelemetryImportance.DEGRADED,
                stale_after_ms=20_000,
            ),
        )
    )
    power_catalog = MetricCatalog(metrics={"power.gpu.0": MetricMeta(unit="W", label="GPU power")})
    estimator = ControlStateEstimator(contract, power_catalog)
    previous = estimator.build(
        ControlInputFrame(
            tick_id=1,
            ts_ms=1_000,
            monotonic_ms=1_000,
            readings=(
                TelemetryReading(
                    metric="power.gpu.0",
                    value=100.0,
                    quality=Quality.OK,
                    source_ts_ms=1_000,
                    last_changed_mono_ms=1_000,
                ),
            ),
        )
    )
    current = estimator.build(
        ControlInputFrame(
            tick_id=2,
            ts_ms=50_000,
            monotonic_ms=11_000,
            readings=(
                TelemetryReading(
                    metric="power.gpu.0",
                    value=140.0,
                    quality=Quality.OK,
                    source_ts_ms=3_000,
                    last_changed_mono_ms=3_000,
                ),
            ),
        ),
        previous=previous,
    )

    assert current.trends[0].per_second == pytest.approx(20.0)
    decision = ReactiveGuard(config(), catalog()).evaluate(current)
    assert decision.zones.front.floor == pytest.approx(0.6)
    assert next(item for item in decision.evidence if item.code == "gpu_power_rise").value == 20


def test_hysteresis_and_hold_prevent_hunting_and_record_release_reason() -> None:
    guard = ReactiveGuard(config(hold_ms=5_000), catalog())

    started = guard.evaluate(temperature_snapshot(tick=1, mono=1_000, rate=3.0))
    hysteresis = guard.evaluate(temperature_snapshot(tick=2, mono=2_000, rate=1.5))
    holding = guard.evaluate(temperature_snapshot(tick=3, mono=3_000, rate=0.5))
    released = guard.evaluate(temperature_snapshot(tick=4, mono=7_000, rate=0.5))

    assert started.zones.front.floor == pytest.approx(0.6)
    assert hysteresis.zones.front.reason is not None
    assert hysteresis.zones.front.reason.code == "reactive_guard_triggered"
    assert holding.zones.front.floor == pytest.approx(0.6)
    assert holding.zones.front.reason is not None
    assert holding.zones.front.reason.code == "reactive_guard_hold"
    assert released.zones.front == GuardZoneOutput()
    release_event = next(event for event in released.events if event.zone.value == "front")
    assert release_event.transition is GuardTransition.RELEASED
    assert release_event.reason.code == "reactive_guard_released"
    assert "causes=threshold_clear" in release_event.reason.detail
    assert release_event.trigger_codes == ("gpu_temperature_rise",)


def test_overlapping_triggers_keep_all_origins_and_release_causes() -> None:
    guard = ReactiveGuard(config(hold_ms=1_000), catalog())
    first = guard.evaluate(temperature_snapshot(tick=1, mono=1_000, rate=3.0))
    overlap = guard.evaluate(
        snapshot(
            tick=2,
            mono=2_000,
            signals=(
                signal("gpu.0.core", None, quality=Quality.MISSING, changed=2_000),
                signal("power.gpu.0", 200.0, changed=2_000),
            ),
            trends=(
                Trend(
                    metric="power.gpu.0",
                    per_second=20.0,
                    from_mono_ms=1_000,
                    to_mono_ms=2_000,
                ),
            ),
            health=TelemetryHealth.DEGRADED,
        )
    )
    holding = guard.evaluate(
        snapshot(
            tick=3,
            mono=2_500,
            signals=(
                signal("gpu.0.core", None, quality=Quality.MISSING, changed=2_500),
                signal("power.gpu.0", 200.0, changed=2_500),
            ),
            trends=(
                Trend(
                    metric="power.gpu.0",
                    per_second=1.0,
                    from_mono_ms=2_000,
                    to_mono_ms=2_500,
                ),
            ),
            health=TelemetryHealth.DEGRADED,
        )
    )
    released = guard.evaluate(
        snapshot(
            tick=4,
            mono=3_000,
            signals=(
                signal("gpu.0.core", None, quality=Quality.MISSING, changed=3_000),
                signal("power.gpu.0", 200.0, changed=3_000),
            ),
            trends=(
                Trend(
                    metric="power.gpu.0",
                    per_second=1.0,
                    from_mono_ms=2_500,
                    to_mono_ms=3_000,
                ),
            ),
            health=TelemetryHealth.DEGRADED,
        )
    )

    assert first.events[0].trigger_codes == ("gpu_temperature_rise",)
    assert overlap.zones.front.floor == pytest.approx(0.6)
    assert holding.zones.front.reason is not None
    assert "release_causes=input_unavailable,threshold_clear" in holding.zones.front.reason.detail
    release_event = next(event for event in released.events if event.zone.value == "front")
    assert release_event.trigger_codes == ("gpu_power_rise", "gpu_temperature_rise")
    assert "causes=input_unavailable,threshold_clear" in release_event.reason.detail


def test_fresh_signal_without_a_new_sample_keeps_the_latch_until_the_next_trend() -> None:
    guard = ReactiveGuard(config(hold_ms=1_000), catalog())
    started = guard.evaluate(temperature_snapshot(tick=1, mono=1_000, rate=3.0))
    no_new_sample = guard.evaluate(temperature_snapshot(tick=2, mono=1_500, rate=None))

    assert started.zones.front.hold_until_mono_ms == 2_000
    assert no_new_sample.zones.front.floor == pytest.approx(0.6)
    assert no_new_sample.zones.front.hold_until_mono_ms == 2_500
    assert "gpu.0.core" not in no_new_sample.unavailable_inputs
    assert all(item.metric != "gpu.0.core" for item in no_new_sample.evidence)


def test_missing_or_stale_input_is_not_treated_as_a_number_and_releases_after_hold() -> None:
    guard = ReactiveGuard(config(hold_ms=1_000), catalog())
    guard.evaluate(temperature_snapshot(tick=1, mono=1_000, rate=3.0))

    holding = guard.evaluate(
        temperature_snapshot(
            tick=2,
            mono=1_500,
            rate=None,
            quality=Quality.STALE,
            health=TelemetryHealth.DEGRADED,
        )
    )
    released = guard.evaluate(
        temperature_snapshot(
            tick=3,
            mono=2_000,
            rate=None,
            quality=Quality.MISSING,
            health=TelemetryHealth.DEGRADED,
        )
    )

    assert holding.zones.front.floor == pytest.approx(0.6)
    assert "gpu.0.core" in holding.unavailable_inputs
    assert released.zones.front == GuardZoneOutput()
    assert all(item.metric != "gpu.0.core" for item in released.evidence)
    release_event = next(event for event in released.events if event.zone.value == "front")
    assert "causes=input_unavailable" in release_event.reason.detail


def test_degraded_snapshot_uses_the_configured_conservative_threshold_set() -> None:
    normal = ReactiveGuard(config(), catalog()).evaluate(
        temperature_snapshot(tick=1, mono=1_000, rate=1.6)
    )
    conservative = ReactiveGuard(config(), catalog()).evaluate(
        temperature_snapshot(
            tick=1,
            mono=1_000,
            rate=1.6,
            health=TelemetryHealth.DEGRADED,
        )
    )

    assert normal.zones.front == GuardZoneOutput()
    assert conservative.threshold_profile is GuardThresholdProfile.CONSERVATIVE
    assert conservative.zones.front.floor == pytest.approx(0.6)
    gpu_evidence = next(
        item for item in conservative.evidence if item.code == "gpu_temperature_rise"
    )
    assert gpu_evidence.activate_above == pytest.approx(1.5)


def test_activation_requires_strictly_exceeding_the_configured_threshold() -> None:
    at_threshold = ReactiveGuard(config(), catalog()).evaluate(
        temperature_snapshot(tick=1, mono=1_000, rate=2.0)
    )
    above_threshold = ReactiveGuard(config(), catalog()).evaluate(
        temperature_snapshot(tick=1, mono=1_000, rate=2.001)
    )

    assert at_threshold.zones.front == GuardZoneOutput()
    assert above_threshold.zones.front.floor == pytest.approx(0.6)


def test_hold_uses_monotonic_time_and_same_snapshot_evaluation_is_idempotent() -> None:
    guard = ReactiveGuard(config(hold_ms=1_000), catalog())
    first_snapshot = temperature_snapshot(
        tick=1,
        mono=1_000,
        rate=3.0,
        ts_ms=50_000,
    )
    first = guard.evaluate(first_snapshot)

    assert guard.evaluate(first_snapshot) is first
    with pytest.raises(ValueError, match="異なる snapshot"):
        guard.evaluate(temperature_snapshot(tick=1, mono=1_000, rate=2.5, ts_ms=50_000))
    # wall timestamp が戻っても、hold は単調時計だけで解除される。
    released = guard.evaluate(temperature_snapshot(tick=2, mono=2_000, rate=0.5, ts_ms=10_000))
    assert released.zones.front == GuardZoneOutput()


def test_guard_output_preserves_the_critical_safety_priority_protocol() -> None:
    """#78 の compose 境界: safety floor と forced Max は Guard より強い。"""
    guard_output: PerZone[GuardZoneOutput] = (
        ReactiveGuard(config(), catalog())
        .evaluate(temperature_snapshot(tick=1, mono=1_000, rate=3.0))
        .zones
    )
    front = guard_output.front
    assert front.floor == pytest.approx(0.6)

    safety_wins = EffectiveZoneDemand(
        requested=0.2,
        effective=0.8,
        bound_by=BoundBy.SAFETY_FLOOR,
        safety_floor=0.8,
        forced_max=False,
        guard_floor=front.floor,
        reasons=(Reason(code="safety_floor"),),
    )
    forced_max_wins = EffectiveZoneDemand(
        requested=0.2,
        effective=1.0,
        bound_by=BoundBy.FORCED_MAX,
        safety_floor=0.8,
        forced_max=True,
        guard_floor=front.floor,
        reasons=(Reason(code="emergency"),),
    )

    assert safety_wins.effective == pytest.approx(0.8)
    assert forced_max_wins.effective == pytest.approx(1.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"effective": 0.5, "safety_floor": 0.8},
        {"effective": 0.5, "guard_floor": 0.6},
        {
            "effective": 0.9,
            "forced_max": True,
            "bound_by": BoundBy.FORCED_MAX,
        },
    ],
)
def test_schema_rejects_any_effective_demand_that_bypasses_guard_or_safety(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "requested": 0.2,
        "effective": 0.8,
        "bound_by": BoundBy.SAFETY_FLOOR,
        "safety_floor": 0.8,
        "forced_max": False,
        "guard_floor": 0.6,
        "reasons": (Reason(code="safety_floor"),),
    }
    values.update(overrides)

    with pytest.raises(ValidationError):
        EffectiveZoneDemand.model_validate(values)


def test_out_of_order_snapshot_cannot_rewind_hold_state() -> None:
    guard = ReactiveGuard(config(), catalog())
    guard.evaluate(temperature_snapshot(tick=2, mono=2_000, rate=3.0))

    with pytest.raises(ValueError, match="tick_id"):
        guard.evaluate(temperature_snapshot(tick=1, mono=3_000, rate=3.0))
    with pytest.raises(ValueError, match="単調時計"):
        guard.evaluate(temperature_snapshot(tick=3, mono=2_000, rate=3.0))


@pytest.mark.parametrize("metric", ["power.cpu.cores", "power.cpu.unknown"])
def test_cpu_power_metric_must_be_a_known_watt_metric_in_the_catalog(metric: str) -> None:
    with pytest.raises(ValueError, match="電力"):
        ReactiveGuard(config(cpu_power_metric=metric), catalog())


def test_cpu_power_metric_outside_the_power_domain_is_rejected_by_config() -> None:
    with pytest.raises(ValidationError, match="power ドメイン"):
        config(cpu_power_metric="gpu.0.core")
