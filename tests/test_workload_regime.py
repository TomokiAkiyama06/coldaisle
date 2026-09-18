"""Workload Regime 推定を Mock / Replay 相当の snapshot だけで検証する。#87。"""

from __future__ import annotations

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.control import WorkloadPowerBand, WorkloadRegime, WorkloadRegimeConfig
from coldaisle.control.state import (
    ControlStateSnapshot,
    SnapshotSignal,
    TelemetryHealth,
    TelemetryImportance,
)
from coldaisle.control.supervisor import RegimeReason, WorkloadRegimeEstimator
from coldaisle.metrics import MetricCatalog, MetricMeta
from coldaisle.store.models import Quality

BASE_TS_MS = 1_800_000_000_000
CPU_METRIC = "power.cpu.package"
GPU_METRIC = "power.gpu.0"


def config() -> WorkloadRegimeConfig:
    return WorkloadRegimeConfig(
        cpu_power=WorkloadPowerBand(
            metric=CPU_METRIC,
            idle_below_w=30.0,
            active_above_w=60.0,
        ),
        gpu_power=WorkloadPowerBand(
            metric=GPU_METRIC,
            idle_below_w=40.0,
            active_above_w=100.0,
        ),
        activity_window_ms=1,
        history_window_ms=30_000,
        minimum_observation_ms=2_000,
        sustained_after_ms=4_000,
        cooldown_ms=3_000,
        minimum_transition_ms=1_000,
        confidence_full_window_ms=10_000,
        max_snapshot_gap_ms=1_000,
    )


def catalog(*, cpu_unit: str = "W", gpu_unit: str = "W") -> MetricCatalog:
    return MetricCatalog(
        metrics={
            CPU_METRIC: MetricMeta(unit=cpu_unit, label="CPU package power"),
            GPU_METRIC: MetricMeta(unit=gpu_unit, label="GPU power"),
        }
    )


def signal(
    metric: str,
    value: float | None,
    quality: Quality | None = None,
) -> SnapshotSignal:
    actual_quality = quality or (Quality.OK if value is not None else Quality.MISSING)
    return SnapshotSignal(
        metric=metric,
        importance=TelemetryImportance.DEGRADED,
        enabled=True,
        value=value,
        quality=actual_quality,
        source_ts_ms=BASE_TS_MS,
        last_changed_mono_ms=0,
        age_ms=0,
    )


def snapshot(
    second: int,
    *,
    cpu_w: float | None,
    gpu_w: float | None,
    cpu_quality: Quality | None = None,
    gpu_quality: Quality | None = None,
) -> ControlStateSnapshot:
    cpu_signal = signal(CPU_METRIC, cpu_w, cpu_quality)
    gpu_signal = signal(GPU_METRIC, gpu_w, gpu_quality)
    missing = not cpu_signal.available or not gpu_signal.available
    return ControlStateSnapshot(
        tick_id=second,
        ts_ms=BASE_TS_MS + second * 1_000,
        monotonic_ms=second * 1_000,
        signals=(cpu_signal, gpu_signal),
        derived=(),
        trends=(),
        telemetry_health=TelemetryHealth.DEGRADED if missing else TelemetryHealth.NORMAL,
        critical_unavailable=(),
    )


def estimate(history: list[ControlStateSnapshot]):
    clock = SimulatedClock(history[-1].ts_ms if history else BASE_TS_MS)
    return WorkloadRegimeEstimator(config(), catalog(), clock).estimate(history)


def test_missing_or_short_history_is_unknown_instead_of_normal_load() -> None:
    assert estimate([]).regime is WorkloadRegime.UNKNOWN

    short = [snapshot(0, cpu_w=10.0, gpu_w=20.0), snapshot(1, cpu_w=10.0, gpu_w=20.0)]
    assert estimate(short).regime is WorkloadRegime.UNKNOWN

    missing = [*short, snapshot(2, cpu_w=10.0, gpu_w=None)]
    result = estimate(missing)
    assert result.regime is WorkloadRegime.UNKNOWN
    assert result.confidence == 0.0
    assert result.reason is RegimeReason.MISSING_SIGNALS


def test_idle_requires_observation_and_minimum_transition_duration() -> None:
    history = [snapshot(second, cpu_w=10.0, gpu_w=20.0) for second in range(4)]

    before_hold = estimate(history[:3])
    confirmed = estimate(history)

    assert before_hold.regime is WorkloadRegime.UNKNOWN
    assert before_hold.reason is RegimeReason.TRANSITION_PENDING
    assert confirmed.regime is WorkloadRegime.IDLE
    assert confirmed.confidence == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("cpu_w", "gpu_w", "transient", "sustained"),
    [
        (90.0, 20.0, WorkloadRegime.TRANSIENT_CPU, WorkloadRegime.SUSTAINED_CPU),
        (10.0, 180.0, WorkloadRegime.TRANSIENT_GPU, WorkloadRegime.SUSTAINED_GPU),
    ],
)
def test_replay_distinguishes_transient_burst_from_sustained_load(
    cpu_w: float,
    gpu_w: float,
    transient: WorkloadRegime,
    sustained: WorkloadRegime,
) -> None:
    history = [snapshot(second, cpu_w=cpu_w, gpu_w=gpu_w) for second in range(8)]

    assert estimate(history[:4]).regime is transient
    assert estimate(history).regime is sustained


def test_simultaneous_cpu_gpu_load_becomes_sustained_combined() -> None:
    history = [snapshot(second, cpu_w=90.0, gpu_w=180.0) for second in range(8)]

    assert estimate(history).regime is WorkloadRegime.SUSTAINED_CPU_GPU


@pytest.mark.parametrize(
    ("cpu_w", "gpu_w"),
    [
        (200.0, 101.0),  # CPU の閾値超過が大きくても片方に丸めない
        (61.0, 180.0),  # GPU の閾値超過が大きくても片方に丸めない
    ],
)
def test_simultaneous_burst_keeps_both_axes_as_transient_combined(
    cpu_w: float, gpu_w: float
) -> None:
    combined = [snapshot(second, cpu_w=cpu_w, gpu_w=gpu_w) for second in range(4)]

    result = estimate(combined)

    assert result.regime is WorkloadRegime.TRANSIENT_CPU_GPU
    assert result.trace_fields()["workload_regime"] is WorkloadRegime.TRANSIENT_CPU_GPU


def test_simultaneous_burst_becomes_sustained_combined_then_single_axis() -> None:
    combined = [snapshot(second, cpu_w=90.0, gpu_w=180.0) for second in range(8)]
    cpu_only = [snapshot(second, cpu_w=90.0, gpu_w=20.0) for second in range(8, 11)]

    assert estimate(combined[:4]).regime is WorkloadRegime.TRANSIENT_CPU_GPU
    assert estimate(combined).regime is WorkloadRegime.SUSTAINED_CPU_GPU
    assert estimate(combined + cpu_only).regime is WorkloadRegime.SUSTAINED_CPU


def test_short_combined_burst_can_become_single_axis_transient() -> None:
    combined = [snapshot(second, cpu_w=90.0, gpu_w=180.0) for second in range(4)]
    cpu_only = [snapshot(second, cpu_w=90.0, gpu_w=20.0) for second in range(4, 7)]

    assert estimate(combined).regime is WorkloadRegime.TRANSIENT_CPU_GPU
    assert estimate(combined + cpu_only).regime is WorkloadRegime.TRANSIENT_CPU


def test_finished_burst_enters_cooldown_then_idle_without_predicting_its_future() -> None:
    burst = [snapshot(second, cpu_w=90.0, gpu_w=20.0) for second in range(4)]
    cooling = [snapshot(second, cpu_w=10.0, gpu_w=20.0) for second in range(4, 9)]

    assert estimate(burst).regime is WorkloadRegime.TRANSIENT_CPU
    assert estimate(burst + cooling[:3]).regime is WorkloadRegime.COOLDOWN
    assert estimate(burst + cooling).regime is WorkloadRegime.IDLE


def test_deadband_hysteresis_does_not_flip_an_active_axis_to_idle() -> None:
    active = [snapshot(second, cpu_w=90.0, gpu_w=20.0) for second in range(4)]
    deadband = [snapshot(second, cpu_w=45.0, gpu_w=20.0) for second in range(4, 6)]

    result = estimate(active + deadband)

    assert result.regime is WorkloadRegime.TRANSIENT_CPU
    assert result.reason is RegimeReason.OBSERVED_HISTORY


def test_unconfirmed_second_axis_spike_does_not_reset_sustained_cpu_age() -> None:
    cpu_load = [snapshot(second, cpu_w=90.0, gpu_w=20.0) for second in range(8)]
    gpu_spike = snapshot(8, cpu_w=90.0, gpu_w=180.0)
    cpu_again = [snapshot(second, cpu_w=90.0, gpu_w=20.0) for second in range(9, 12)]

    assert estimate(cpu_load).regime is WorkloadRegime.SUSTAINED_CPU
    assert estimate([*cpu_load, gpu_spike]).regime is WorkloadRegime.SUSTAINED_CPU
    assert all(
        estimate([*cpu_load, gpu_spike, *cpu_again[:end]]).regime is WorkloadRegime.SUSTAINED_CPU
        for end in range(1, len(cpu_again) + 1)
    )


def test_unconfirmed_idle_dip_does_not_reset_sustained_cpu_age() -> None:
    cpu_load = [snapshot(second, cpu_w=90.0, gpu_w=20.0) for second in range(8)]
    idle_dip = snapshot(8, cpu_w=10.0, gpu_w=20.0)
    cpu_again = [snapshot(second, cpu_w=90.0, gpu_w=20.0) for second in range(9, 12)]

    assert estimate([*cpu_load, idle_dip]).regime is WorkloadRegime.SUSTAINED_CPU
    assert all(
        estimate([*cpu_load, idle_dip, *cpu_again[:end]]).regime is WorkloadRegime.SUSTAINED_CPU
        for end in range(1, len(cpu_again) + 1)
    )


def test_gap_resets_history_and_requires_a_new_contiguous_observation() -> None:
    old = [snapshot(second, cpu_w=90.0, gpu_w=20.0) for second in range(4)]
    after_gap = snapshot(6, cpu_w=90.0, gpu_w=20.0)

    result = estimate([*old, after_gap])

    assert result.regime is WorkloadRegime.UNKNOWN
    assert result.evidence.observed_window_ms == 0

    recovered = [
        *old,
        after_gap,
        snapshot(7, cpu_w=90.0, gpu_w=20.0),
        snapshot(8, cpu_w=90.0, gpu_w=20.0),
        snapshot(9, cpu_w=90.0, gpu_w=20.0),
    ]
    assert estimate(recovered).regime is WorkloadRegime.TRANSIENT_CPU


def test_missing_and_stale_inputs_recover_only_after_a_new_contiguous_history() -> None:
    missing = [
        snapshot(0, cpu_w=10.0, gpu_w=20.0),
        snapshot(1, cpu_w=10.0, gpu_w=None),
    ]
    recovered = [
        *missing,
        *(snapshot(second, cpu_w=10.0, gpu_w=20.0) for second in range(2, 6)),
    ]
    stale = snapshot(
        6,
        cpu_w=10.0,
        gpu_w=20.0,
        gpu_quality=Quality.STALE,
    )

    assert estimate(recovered).regime is WorkloadRegime.IDLE
    assert estimate([*recovered, stale]).regime is WorkloadRegime.UNKNOWN


def test_threshold_and_gap_boundaries_are_inclusive() -> None:
    exactly_idle = [snapshot(second, cpu_w=30.0, gpu_w=40.0) for second in range(4)]
    exactly_active = [snapshot(second, cpu_w=60.0, gpu_w=40.0) for second in range(4)]
    gap_at_limit = [
        snapshot(0, cpu_w=30.0, gpu_w=40.0),
        snapshot(1, cpu_w=30.0, gpu_w=40.0),
        snapshot(2, cpu_w=30.0, gpu_w=40.0),
        snapshot(3, cpu_w=30.0, gpu_w=40.0),
    ]

    assert estimate(exactly_idle).regime is WorkloadRegime.IDLE
    assert estimate(exactly_active).regime is WorkloadRegime.TRANSIENT_CPU
    assert estimate(gap_at_limit).regime is WorkloadRegime.IDLE


def test_same_mock_replay_and_clock_produce_the_same_transition() -> None:
    history = [snapshot(second, cpu_w=10.0, gpu_w=180.0) for second in range(8)]
    first_clock = SimulatedClock(history[-1].ts_ms)
    second_clock = SimulatedClock(history[-1].ts_ms)

    first = WorkloadRegimeEstimator(config(), catalog(), first_clock).estimate(history)
    second = WorkloadRegimeEstimator(config(), catalog(), second_clock).estimate(tuple(history))
    first_transitions = [
        WorkloadRegimeEstimator(config(), catalog(), first_clock).estimate(history[:end]).regime
        for end in range(1, len(history) + 1)
    ]
    second_transitions = [
        WorkloadRegimeEstimator(config(), catalog(), second_clock).estimate(history[:end]).regime
        for end in range(1, len(history) + 1)
    ]

    assert first == second
    assert first_transitions == second_transitions
    assert first.computed_at_ms == history[-1].ts_ms
    assert first.trace_fields() == {
        "workload_regime": WorkloadRegime.SUSTAINED_GPU,
        "regime_confidence": 0.7,
    }


def test_wall_clock_is_only_recorded_and_never_used_for_elapsed_time() -> None:
    history = [snapshot(second, cpu_w=10.0, gpu_w=20.0) for second in range(4)]
    clock = SimulatedClock(BASE_TS_MS - 1)

    result = WorkloadRegimeEstimator(config(), catalog(), clock).estimate(history)

    assert result.regime is WorkloadRegime.IDLE
    assert result.computed_at_ms == BASE_TS_MS - 1


def test_out_of_order_monotonic_history_is_rejected() -> None:
    history = [
        snapshot(1, cpu_w=10.0, gpu_w=20.0),
        snapshot(0, cpu_w=10.0, gpu_w=20.0),
    ]
    with pytest.raises(ValueError, match="monotonic_ms"):
        WorkloadRegimeEstimator(config(), catalog(), SimulatedClock(BASE_TS_MS + 1_000)).estimate(
            history
        )


def test_power_metrics_must_be_distinct_known_watt_signals() -> None:
    with pytest.raises(ValueError, match=r"CPU workload Power metric.*電力\(W\)"):
        WorkloadRegimeEstimator(
            config(),
            catalog(cpu_unit="C"),
            SimulatedClock(BASE_TS_MS),
        )

    missing_cpu = MetricCatalog(metrics={GPU_METRIC: MetricMeta(unit="W", label="GPU power")})
    with pytest.raises(ValueError, match=r"CPU workload Power metric.*unit=None"):
        WorkloadRegimeEstimator(
            config(),
            missing_cpu,
            SimulatedClock(BASE_TS_MS),
        )
