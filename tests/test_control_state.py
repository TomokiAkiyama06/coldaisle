"""#102 Runtime State Estimator の純粋ロジック検証。実機/NVMLは使わない。"""

import pytest
from pydantic import ValidationError

from coldaisle.control.schema import PerZone
from coldaisle.control.state import (
    ControlInputContract,
    ControlInputFrame,
    ControlStateEstimator,
    CriticalTelemetryGroup,
    FanState,
    SignalSpec,
    TelemetryHealth,
    TelemetryImportance,
    TelemetryReading,
)
from coldaisle.metrics import DerivedMeta, MetricCatalog, MetricMeta
from coldaisle.store.models import Quality


def catalog() -> MetricCatalog:
    return MetricCatalog(
        metrics={
            "air.front_intake": MetricMeta(unit="C", label="front"),
            "air.rear_exhaust": MetricMeta(unit="C", label="rear"),
            "cpu.package": MetricMeta(unit="C", label="cpu"),
            "gpu.0.core": MetricMeta(unit="C", label="gpu"),
        },
        derived={
            "d.case_delta": DerivedMeta(
                unit="C",
                label="case delta",
                minuend="air.rear_exhaust",
                subtrahend="air.front_intake",
            )
        },
    )


def contract() -> ControlInputContract:
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
            SignalSpec(
                metric="air.front_intake",
                importance=TelemetryImportance.DEGRADED,
                stale_after_ms=1_000,
            ),
            SignalSpec(
                metric="air.rear_exhaust",
                importance=TelemetryImportance.DEGRADED,
                stale_after_ms=1_000,
            ),
        ),
        critical_groups=(
            CriticalTelemetryGroup(
                code="air_telemetry",
                metrics=("air.front_intake", "air.rear_exhaust"),
            ),
        ),
    )


def reading(metric: str, value: float | None, quality: Quality = Quality.OK, changed: int = 9_000):
    return TelemetryReading(
        metric=metric,
        value=value,
        quality=quality,
        source_ts_ms=1_000,
        last_changed_mono_ms=changed,
    )


def frame(*readings: TelemetryReading, mono: int = 10_000, tick: int = 1) -> ControlInputFrame:
    return ControlInputFrame(tick_id=tick, ts_ms=9_999_999, monotonic_ms=mono, readings=readings)


def test_missing_input_is_explicit_never_filled_with_zero():
    snapshot = ControlStateEstimator(contract(), catalog()).build(
        frame(reading("cpu.package", 60.0))
    )

    missing = snapshot.signals_by_metric["gpu.0.core"]
    assert missing.quality is Quality.MISSING
    assert missing.value is None
    assert "gpu.0.core" in snapshot.critical_unavailable


def test_staleness_uses_monotonic_observation_not_wall_timestamp():
    estimator = ControlStateEstimator(contract(), catalog())
    snapshot = estimator.build(frame(reading("cpu.package", 60.0, changed=9_000), mono=10_001))

    cpu = snapshot.signals_by_metric["cpu.package"]
    assert cpu.age_ms == 1_001
    assert cpu.quality is Quality.STALE
    assert "cpu.package" in snapshot.critical_unavailable


def test_source_quality_is_preserved_before_stale_calculation():
    estimator = ControlStateEstimator(contract(), catalog())
    snapshot = estimator.build(frame(reading("cpu.package", None, Quality.MISSING)))

    assert snapshot.signals_by_metric["cpu.package"].quality is Quality.MISSING


def test_one_air_probe_loss_is_degraded_but_not_the_critical_group():
    estimator = ControlStateEstimator(contract(), catalog())
    snapshot = estimator.build(
        frame(
            reading("cpu.package", 60.0),
            reading("gpu.0.core", 50.0),
            reading("air.front_intake", None, Quality.MISSING),
            reading("air.rear_exhaust", 35.0),
        )
    )

    assert snapshot.telemetry_health is TelemetryHealth.DEGRADED
    assert "air_telemetry" not in snapshot.critical_unavailable


def test_all_air_probes_unavailable_marks_the_group_critical():
    estimator = ControlStateEstimator(contract(), catalog())
    snapshot = estimator.build(
        frame(
            reading("cpu.package", 60.0),
            reading("gpu.0.core", 50.0),
            reading("air.front_intake", None, Quality.MISSING),
            reading("air.rear_exhaust", None, Quality.SUSPECT),
        )
    )

    assert snapshot.critical_unavailable == ("air_telemetry",)


def test_derived_values_are_configured_and_skip_unusable_inputs():
    estimator = ControlStateEstimator(contract(), catalog())
    usable = estimator.build(
        frame(reading("air.front_intake", 20.0), reading("air.rear_exhaust", 30.0))
    )
    unusable = estimator.build(
        frame(reading("air.front_intake", 20.0), reading("air.rear_exhaust", None, Quality.MISSING))
    )

    assert usable.derived_by_metric["d.case_delta"] == 10.0
    assert unusable.derived_by_metric["d.case_delta"] is None


def test_trend_uses_monotonic_observation_time_so_tick_jitter_does_not_change_rate():
    estimator = ControlStateEstimator(contract(), catalog())
    prior = estimator.build(frame(reading("cpu.package", 50.0), mono=10_000, tick=1))
    current_reading = TelemetryReading(
        metric="cpu.package",
        value=56.0,
        quality=Quality.OK,
        source_ts_ms=0,
        last_changed_mono_ms=11_000,
    )
    current = estimator.build(frame(current_reading, mono=11_500, tick=2), previous=prior)

    assert len(current.trends) == 1
    assert current.trends[0].per_second == 3.0
    assert current.trends[0].from_mono_ms == 9_000
    assert current.trends[0].to_mono_ms == 11_000


def test_trend_never_reuses_the_same_monotonic_observation():
    estimator = ControlStateEstimator(contract(), catalog())
    prior = estimator.build(frame(reading("cpu.package", 50.0), mono=10_000, tick=1))
    current = estimator.build(
        frame(reading("cpu.package", 55.0), mono=11_000, tick=2), previous=prior
    )

    assert current.trends == ()


def test_frame_rejects_future_monotonic_observation_and_unknown_inputs_are_rejected():
    with pytest.raises(ValidationError, match="future"):
        frame(reading("cpu.package", 60.0, changed=10_001), mono=10_000)

    estimator = ControlStateEstimator(contract(), catalog())
    with pytest.raises(ValueError, match="contract"):
        estimator.build(frame(reading("air.room", 20.0)))


def test_disabled_signal_is_distinct_from_missing_and_does_not_degrade_health():
    disabled_contract = ControlInputContract(
        signals=(
            SignalSpec(
                metric="cpu.package",
                importance=TelemetryImportance.CRITICAL,
                stale_after_ms=1_000,
            ),
            SignalSpec(
                metric="gpu.0.core",
                importance=TelemetryImportance.CRITICAL,
                enabled=False,
            ),
        )
    )
    snapshot = ControlStateEstimator(disabled_contract, catalog()).build(
        frame(reading("cpu.package", 60.0))
    )

    gpu = snapshot.signals_by_metric["gpu.0.core"]
    assert not gpu.enabled
    assert gpu.quality is Quality.MISSING
    assert snapshot.telemetry_health is TelemetryHealth.NORMAL
    assert snapshot.critical_unavailable == ()


def test_critical_group_rejects_an_intentionally_disabled_member():
    with pytest.raises(ValidationError, match="すべて有効"):
        ControlInputContract(
            signals=(
                SignalSpec(
                    metric="air.front_intake",
                    importance=TelemetryImportance.DEGRADED,
                    stale_after_ms=1_000,
                ),
                SignalSpec(
                    metric="air.rear_exhaust",
                    importance=TelemetryImportance.DEGRADED,
                    enabled=False,
                ),
            ),
            critical_groups=(
                CriticalTelemetryGroup(
                    code="air_telemetry",
                    metrics=("air.front_intake", "air.rear_exhaust"),
                ),
            ),
        )


def test_fan_hardware_state_is_passed_through_without_transformation():
    fans = PerZone(
        front=FanState(effective_demand=0.4, pwm_raw=102, rpm=1_200, estimated_flow=2.5),
        rear=FanState(effective_demand=0.5, pwm_raw=128, rpm=1_300, estimated_flow=2.7),
        top=FanState(effective_demand=0.6, pwm_raw=153, rpm=1_400, estimated_flow=2.9),
    )

    snapshot = ControlStateEstimator(contract(), catalog()).build(
        ControlInputFrame(
            tick_id=1,
            ts_ms=9_999_999,
            monotonic_ms=10_000,
            fans=fans,
        )
    )

    assert snapshot.fans == fans
