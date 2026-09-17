"""観測済み ``ControlStateSnapshot`` 履歴から現在の Workload Regime を推定する。#87

予測 horizon や expected duration は入力にも出力にも持たない。Power の有効な連続履歴だけを
使い、欠測・stale・大きな sampling gap の後は ``UNKNOWN`` から再評価する。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.clock import Clock
from coldaisle.control.config import WorkloadPowerBand, WorkloadRegimeConfig
from coldaisle.control.schema import WorkloadRegime
from coldaisle.control.state import ControlStateSnapshot
from coldaisle.metrics import MetricCatalog

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RegimeReason(StrEnum):
    """推定結果がその状態にある理由。decision trace の説明に利用できる。"""

    NO_HISTORY = "no_history"
    MISSING_SIGNALS = "missing_signals"
    INSUFFICIENT_HISTORY = "insufficient_history"
    AMBIGUOUS_POWER = "ambiguous_power"
    TRANSITION_PENDING = "transition_pending"
    OBSERVED_HISTORY = "observed_history"


class RegimeEvidence(_Frozen):
    """未来値を含まない、現在の平滑化済み Power と連続観測幅。"""

    cpu_power_mean_w: FiniteFloat | None = None
    gpu_power_mean_w: FiniteFloat | None = None
    observed_window_ms: int = Field(ge=0)


class WorkloadRegimeEstimate(_Frozen):
    """現在の Regime と、その根拠となる観測充足度。"""

    regime: WorkloadRegime
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    reason: RegimeReason
    as_of_tick_id: int | None = Field(default=None, ge=0)
    computed_at_ms: int = Field(ge=0)
    evidence: RegimeEvidence

    def trace_fields(self) -> dict[str, WorkloadRegime | float]:
        """#82 ``ControlState`` にそのまま渡せるフィールドを返す。"""
        return {
            "workload_regime": self.regime,
            "regime_confidence": self.confidence,
        }


class _Axis:
    """Power signal 1本の Schmitt trigger 状態。"""

    def __init__(self, band: WorkloadPowerBand) -> None:
        self._band = band
        self.active: bool | None = None

    def update(self, mean_w: float) -> bool | None:
        if mean_w >= self._band.active_above_w:
            self.active = True
        elif mean_w <= self._band.idle_below_w:
            self.active = False
        return self.active


class WorkloadRegimeEstimator:
    """履歴を先頭から再生して、呼出し頻度に依存しない Regime 遷移を返す。"""

    def __init__(self, config: WorkloadRegimeConfig, catalog: MetricCatalog, clock: Clock) -> None:
        self._validate_metric_units(config, catalog)
        self._config = config
        self._clock = clock

    def estimate(self, history: Sequence[ControlStateSnapshot]) -> WorkloadRegimeEstimate:
        """単調時刻順の snapshot 履歴から、最後の tick の現在状態を推定する。"""
        computed_at_ms = self._clock.now_ms()
        snapshots = tuple(history)
        if not snapshots:
            return self._unknown(computed_at_ms, RegimeReason.NO_HISTORY)
        self._validate_history(snapshots)

        end_mono_ms = snapshots[-1].monotonic_ms
        cutoff_ms = max(0, end_mono_ms - self._config.history_window_ms)
        window = tuple(snapshot for snapshot in snapshots if snapshot.monotonic_ms >= cutoff_ms)
        return self._replay(window, computed_at_ms)

    def _replay(
        self,
        snapshots: tuple[ControlStateSnapshot, ...],
        computed_at_ms: int,
    ) -> WorkloadRegimeEstimate:
        cpu_axis = _Axis(self._config.cpu_power)
        gpu_axis = _Axis(self._config.gpu_power)
        cpu_window: deque[tuple[int, float]] = deque()
        gpu_window: deque[tuple[int, float]] = deque()

        published = WorkloadRegime.UNKNOWN
        candidate: WorkloadRegime | None = None
        candidate_since_ms: int | None = None
        cpu_active_since_ms: int | None = None
        gpu_active_since_ms: int | None = None
        cpu_inactive_since_ms: int | None = None
        gpu_inactive_since_ms: int | None = None
        last_active_ms: int | None = None
        valid_since_ms: int | None = None
        previous_mono_ms: int | None = None
        cpu_mean_w: float | None = None
        gpu_mean_w: float | None = None
        reason = RegimeReason.INSUFFICIENT_HISTORY

        for snapshot in snapshots:
            cpu_power = self._power(snapshot, self._config.cpu_power.metric)
            gpu_power = self._power(snapshot, self._config.gpu_power.metric)
            gap = (
                previous_mono_ms is not None
                and snapshot.monotonic_ms - previous_mono_ms > self._config.max_snapshot_gap_ms
            )
            previous_mono_ms = snapshot.monotonic_ms
            if cpu_power is None or gpu_power is None or gap:
                published = WorkloadRegime.UNKNOWN
                candidate = None
                candidate_since_ms = None
                cpu_active_since_ms = None
                gpu_active_since_ms = None
                cpu_inactive_since_ms = None
                gpu_inactive_since_ms = None
                last_active_ms = None
                valid_since_ms = None
                cpu_axis = _Axis(self._config.cpu_power)
                gpu_axis = _Axis(self._config.gpu_power)
                cpu_window.clear()
                gpu_window.clear()
                cpu_mean_w = None
                gpu_mean_w = None
                if cpu_power is None or gpu_power is None:
                    reason = RegimeReason.MISSING_SIGNALS
                    continue
                reason = RegimeReason.INSUFFICIENT_HISTORY

            mono_ms = snapshot.monotonic_ms
            if valid_since_ms is None:
                valid_since_ms = mono_ms
            cpu_window.append((mono_ms, cpu_power))
            gpu_window.append((mono_ms, gpu_power))
            smoothing_cutoff = mono_ms - self._config.activity_window_ms
            while cpu_window and cpu_window[0][0] < smoothing_cutoff:
                cpu_window.popleft()
            while gpu_window and gpu_window[0][0] < smoothing_cutoff:
                gpu_window.popleft()
            cpu_mean_w = sum(value for _, value in cpu_window) / len(cpu_window)
            gpu_mean_w = sum(value for _, value in gpu_window) / len(gpu_window)

            cpu_active = cpu_axis.update(cpu_mean_w)
            gpu_active = gpu_axis.update(gpu_mean_w)
            observed_ms = mono_ms - valid_since_ms
            if cpu_active is None or gpu_active is None:
                raw = WorkloadRegime.UNKNOWN
                reason = RegimeReason.AMBIGUOUS_POWER
            elif observed_ms < self._config.minimum_observation_ms:
                raw = WorkloadRegime.UNKNOWN
                reason = RegimeReason.INSUFFICIENT_HISTORY
            else:
                cpu_active_since_ms, cpu_inactive_since_ms = self._axis_timing(
                    mono_ms=mono_ms,
                    active=cpu_active,
                    active_since_ms=cpu_active_since_ms,
                    inactive_since_ms=cpu_inactive_since_ms,
                )
                gpu_active_since_ms, gpu_inactive_since_ms = self._axis_timing(
                    mono_ms=mono_ms,
                    active=gpu_active,
                    active_since_ms=gpu_active_since_ms,
                    inactive_since_ms=gpu_inactive_since_ms,
                )
                raw, last_active_ms = self._raw_regime(
                    mono_ms=mono_ms,
                    cpu_active=cpu_active,
                    gpu_active=gpu_active,
                    cpu_mean_w=cpu_mean_w,
                    gpu_mean_w=gpu_mean_w,
                    cpu_active_since_ms=cpu_active_since_ms,
                    gpu_active_since_ms=gpu_active_since_ms,
                    last_active_ms=last_active_ms,
                )
                reason = RegimeReason.OBSERVED_HISTORY

            if raw is WorkloadRegime.UNKNOWN:
                published = WorkloadRegime.UNKNOWN
                candidate = None
                candidate_since_ms = None
                continue
            if raw is published:
                candidate = None
                candidate_since_ms = None
                continue
            if raw is not candidate:
                candidate = raw
                candidate_since_ms = mono_ms
            elif (
                candidate_since_ms is not None
                and mono_ms - candidate_since_ms >= self._config.minimum_transition_ms
            ):
                published = raw
                candidate = None
                candidate_since_ms = None

        latest = snapshots[-1]
        observed_window_ms = 0 if valid_since_ms is None else latest.monotonic_ms - valid_since_ms
        if published is WorkloadRegime.UNKNOWN:
            confidence = 0.0
        else:
            confidence = min(1.0, observed_window_ms / self._config.confidence_full_window_ms)
        if candidate is not None:
            reason = RegimeReason.TRANSITION_PENDING
        return WorkloadRegimeEstimate(
            regime=published,
            confidence=confidence,
            reason=reason,
            as_of_tick_id=latest.tick_id,
            computed_at_ms=computed_at_ms,
            evidence=RegimeEvidence(
                cpu_power_mean_w=cpu_mean_w,
                gpu_power_mean_w=gpu_mean_w,
                observed_window_ms=observed_window_ms,
            ),
        )

    def _raw_regime(
        self,
        *,
        mono_ms: int,
        cpu_active: bool,
        gpu_active: bool,
        cpu_mean_w: float,
        gpu_mean_w: float,
        cpu_active_since_ms: int | None,
        gpu_active_since_ms: int | None,
        last_active_ms: int | None,
    ) -> tuple[WorkloadRegime, int | None]:
        combination = (cpu_active, gpu_active)
        if any(combination):
            last_active_ms = mono_ms
            active_since = tuple(
                since
                for active, since in (
                    (cpu_active, cpu_active_since_ms),
                    (gpu_active, gpu_active_since_ms),
                )
                if active
            )
            assert active_since and all(since is not None for since in active_since)
            sustained = all(
                mono_ms - since >= self._config.sustained_after_ms
                for since in active_since
                if since is not None
            )
            if cpu_active and gpu_active:
                if sustained:
                    regime = WorkloadRegime.SUSTAINED_CPU_GPU
                else:
                    cpu_activity = self._relative_activity(cpu_mean_w, self._config.cpu_power)
                    gpu_activity = self._relative_activity(gpu_mean_w, self._config.gpu_power)
                    # TRANSIENT_CPU_GPU は schema に無いため、閾値超過が大きい側を残す。
                    regime = (
                        WorkloadRegime.TRANSIENT_CPU
                        if cpu_activity >= gpu_activity
                        else WorkloadRegime.TRANSIENT_GPU
                    )
            elif cpu_active:
                regime = WorkloadRegime.SUSTAINED_CPU if sustained else WorkloadRegime.TRANSIENT_CPU
            else:
                regime = WorkloadRegime.SUSTAINED_GPU if sustained else WorkloadRegime.TRANSIENT_GPU
            return regime, last_active_ms

        if last_active_ms is not None and mono_ms - last_active_ms < self._config.cooldown_ms:
            return WorkloadRegime.COOLDOWN, last_active_ms
        return WorkloadRegime.IDLE, last_active_ms

    def _axis_timing(
        self,
        *,
        mono_ms: int,
        active: bool,
        active_since_ms: int | None,
        inactive_since_ms: int | None,
    ) -> tuple[int | None, int | None]:
        """未確定の短い停止では、継続中の active duration を失わない。"""
        if active:
            return active_since_ms if active_since_ms is not None else mono_ms, None
        if active_since_ms is None:
            return None, None
        if inactive_since_ms is None:
            return active_since_ms, mono_ms
        if mono_ms - inactive_since_ms >= self._config.minimum_transition_ms:
            return None, None
        return active_since_ms, inactive_since_ms

    @staticmethod
    def _power(snapshot: ControlStateSnapshot, metric: str) -> float | None:
        signal = snapshot.signals_by_metric.get(metric)
        if signal is None or not signal.available:
            return None
        return signal.value

    @staticmethod
    def _relative_activity(value: float, band: WorkloadPowerBand) -> float:
        return (value - band.idle_below_w) / (band.active_above_w - band.idle_below_w)

    @staticmethod
    def _validate_metric_units(config: WorkloadRegimeConfig, catalog: MetricCatalog) -> None:
        for axis, band in (("CPU", config.cpu_power), ("GPU", config.gpu_power)):
            unit = catalog.unit_for(band.metric)
            if unit != "W":
                raise ValueError(
                    f"{axis} workload Power metric は既知の電力(W)にする: "
                    f"metric={band.metric}, unit={unit}"
                )

    @staticmethod
    def _validate_history(snapshots: tuple[ControlStateSnapshot, ...]) -> None:
        previous_mono_ms: int | None = None
        for snapshot in snapshots:
            if previous_mono_ms is not None and snapshot.monotonic_ms <= previous_mono_ms:
                raise ValueError("snapshot history は monotonic_ms の昇順にする")
            previous_mono_ms = snapshot.monotonic_ms

    @staticmethod
    def _unknown(computed_at_ms: int, reason: RegimeReason) -> WorkloadRegimeEstimate:
        return WorkloadRegimeEstimate(
            regime=WorkloadRegime.UNKNOWN,
            confidence=0.0,
            reason=reason,
            computed_at_ms=computed_at_ms,
            evidence=RegimeEvidence(observed_window_ms=0),
        )
