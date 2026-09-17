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


def signal(metric: str, value: float | None) -> SnapshotSignal:
    quality = Quality.OK if value is not None else Quality.MISSING
    return SnapshotSignal(
        metric=metric,
        importance=TelemetryImportance.DEGRADED,
        enabled=True,
        value=value,
        quality=quality,
        source_ts_ms=BASE_TS_MS,
        last_changed_mono_ms=0,
        age_ms=0,
    )


def snapshot(
    second: int,
    *,
    cpu_w: float | None,
    gpu_w: float | None,
) -> ControlStateSnapshot:
    missing = cpu_w is None or gpu_w is None
    return ControlStateSnapshot(
        tick_id=second,
        ts_ms=BASE_TS_MS + second * 1_000,
        monotonic_ms=second * 1_000,
        signals=(signal(CPU_METRIC, cpu_w), signal(GPU_METRIC, gpu_w)),
        derived=(),
        trends=(),
        telemetry_health=TelemetryHealth.DEGRADED if missing else TelemetryHealth.NORMAL,
        critical_unavailable=(),
    )


def estimate(history: list[ControlStateSnapshot]):
    clock = SimulatedClock(history[-1].ts_ms if history else BASE_TS_MS)
    return WorkloadRegimeEstimator(config(), clock).estimate(history)


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


def test_same_mock_replay_and_clock_produce_the_same_transition() -> None:
    history = [snapshot(second, cpu_w=10.0, gpu_w=180.0) for second in range(8)]
    first_clock = SimulatedClock(history[-1].ts_ms)
    second_clock = SimulatedClock(history[-1].ts_ms)

    first = WorkloadRegimeEstimator(config(), first_clock).estimate(history)
    second = WorkloadRegimeEstimator(config(), second_clock).estimate(tuple(history))
    first_transitions = [
        WorkloadRegimeEstimator(config(), first_clock).estimate(history[:end]).regime
        for end in range(1, len(history) + 1)
    ]
    second_transitions = [
        WorkloadRegimeEstimator(config(), second_clock).estimate(history[:end]).regime
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

    result = WorkloadRegimeEstimator(config(), clock).estimate(history)

    assert result.regime is WorkloadRegime.IDLE
    assert result.computed_at_ms == BASE_TS_MS - 1


def test_out_of_order_monotonic_history_is_rejected() -> None:
    history = [
        snapshot(1, cpu_w=10.0, gpu_w=20.0),
        snapshot(0, cpu_w=10.0, gpu_w=20.0),
    ]
    with pytest.raises(ValueError, match="monotonic_ms"):
        WorkloadRegimeEstimator(config(), SimulatedClock(BASE_TS_MS + 1_000)).estimate(history)
