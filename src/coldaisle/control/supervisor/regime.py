"""観測済み ``ControlStateSnapshot`` 履歴から現在の Workload Regime を推定する。#87

予測 horizon や expected duration は入力にも出力にも持たない。Power の有効な連続履歴だけを
使い、欠測・stale・大きな sampling gap の後は ``UNKNOWN`` から再評価する。

推定は不変な ``WorkloadRegimeState`` を snapshot 1件ずつ進める純関数 ``step`` で行う。
状態には平滑化窓より古い sample を持たないので、1 tick あたりの計算量と状態の大きさは
稼働時間に依存しない。履歴を窓で切り詰めないため、確定済みの Regime・Schmitt latch・
遷移確認・COOLDOWN の起点も失わない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
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


@dataclass(frozen=True, slots=True)
class _AxisState:
    """Power signal 1本の平滑化窓・Schmitt latch・active 継続時間。"""

    samples: tuple[tuple[int, float], ...] = ()
    """``activity_window_ms`` 以内の (monotonic_ms, W)。これより古い sample は持たない。"""
    active: bool | None = None
    active_since_ms: int | None = None
    inactive_since_ms: int | None = None

    @property
    def mean_w(self) -> float | None:
        if not self.samples:
            return None
        return sum(value for _, value in self.samples) / len(self.samples)

    def observe(
        self, mono_ms: int, power_w: float, band: WorkloadPowerBand, activity_window_ms: int
    ) -> _AxisState:
        smoothing_cutoff = mono_ms - activity_window_ms
        appended = (*self.samples, (mono_ms, power_w))
        samples = tuple(sample for sample in appended if sample[0] >= smoothing_cutoff)
        mean_w = sum(value for _, value in samples) / len(samples)
        active = self.active
        if mean_w >= band.active_above_w:
            active = True
        elif mean_w <= band.idle_below_w:
            active = False
        return replace(self, samples=samples, active=active)


@dataclass(frozen=True, slots=True)
class WorkloadRegimeState:
    """Workload Regime 推定の checkpoint。不変で、次の snapshot だけで先へ進められる。

    欠測・gap の後は初期状態に戻る。フィールドは実装の内部表現で、保存形式ではない。
    """

    previous_mono_ms: int | None = None
    cpu: _AxisState = field(default_factory=_AxisState)
    gpu: _AxisState = field(default_factory=_AxisState)
    published: WorkloadRegime = WorkloadRegime.UNKNOWN
    candidate: WorkloadRegime | None = None
    candidate_since_ms: int | None = None
    last_active_ms: int | None = None
    valid_since_ms: int | None = None


class WorkloadRegimeEstimator:
    """snapshot を1件ずつ進め、呼出し頻度に依存しない Regime 遷移を返す。"""

    def __init__(self, config: WorkloadRegimeConfig, catalog: MetricCatalog, clock: Clock) -> None:
        self._validate_metric_units(config, catalog)
        self._config = config
        self._clock = clock

    @staticmethod
    def initial_state() -> WorkloadRegimeState:
        """履歴が無い状態。"""
        return WorkloadRegimeState()

    def step(
        self, state: WorkloadRegimeState, snapshot: ControlStateSnapshot
    ) -> tuple[WorkloadRegimeState, WorkloadRegimeEstimate]:
        """checkpoint を snapshot 1件だけ進め、次の checkpoint とその tick の推定を返す。

        同じ state と snapshot からは常に同じ結果になる（壁時計は ``computed_at_ms`` のみ）。
        """
        return self._advance(state, snapshot, self._clock.now_ms())

    def estimate(self, history: Sequence[ControlStateSnapshot]) -> WorkloadRegimeEstimate:
        """単調時刻順の snapshot 列を初期状態から ``step`` で畳み込み、最後の推定を返す。

        Replay・テスト用の簡便関数。実行時は ``step`` で checkpoint を持ち回る。
        """
        computed_at_ms = self._clock.now_ms()
        if not history:
            return self._unknown(computed_at_ms, RegimeReason.NO_HISTORY)
        state = self.initial_state()
        result: WorkloadRegimeEstimate | None = None
        for snapshot in history:
            state, result = self._advance(state, snapshot, computed_at_ms)
        assert result is not None
        return result

    def _advance(
        self,
        state: WorkloadRegimeState,
        snapshot: ControlStateSnapshot,
        computed_at_ms: int,
    ) -> tuple[WorkloadRegimeState, WorkloadRegimeEstimate]:
        mono_ms = snapshot.monotonic_ms
        if state.previous_mono_ms is not None and mono_ms <= state.previous_mono_ms:
            raise ValueError("snapshot history は monotonic_ms の昇順にする")
        cpu_power = self._power(snapshot, self._config.cpu_power.metric)
        gpu_power = self._power(snapshot, self._config.gpu_power.metric)
        gap = (
            state.previous_mono_ms is not None
            and mono_ms - state.previous_mono_ms > self._config.max_snapshot_gap_ms
        )
        if cpu_power is None or gpu_power is None or gap:
            state = WorkloadRegimeState(previous_mono_ms=mono_ms)
            if cpu_power is None or gpu_power is None:
                return state, self._estimate(
                    state, snapshot, computed_at_ms, RegimeReason.MISSING_SIGNALS
                )
        else:
            state = replace(state, previous_mono_ms=mono_ms)

        valid_since_ms = state.valid_since_ms if state.valid_since_ms is not None else mono_ms
        cpu = state.cpu.observe(
            mono_ms, cpu_power, self._config.cpu_power, self._config.activity_window_ms
        )
        gpu = state.gpu.observe(
            mono_ms, gpu_power, self._config.gpu_power, self._config.activity_window_ms
        )
        last_active_ms = state.last_active_ms
        observed_ms = mono_ms - valid_since_ms
        if cpu.active is None or gpu.active is None:
            raw = WorkloadRegime.UNKNOWN
            reason = RegimeReason.AMBIGUOUS_POWER
        elif observed_ms < self._config.minimum_observation_ms:
            raw = WorkloadRegime.UNKNOWN
            reason = RegimeReason.INSUFFICIENT_HISTORY
        else:
            cpu = self._axis_timing(mono_ms, cpu)
            gpu = self._axis_timing(mono_ms, gpu)
            raw, last_active_ms = self._raw_regime(
                mono_ms=mono_ms, cpu=cpu, gpu=gpu, last_active_ms=last_active_ms
            )
            reason = RegimeReason.OBSERVED_HISTORY

        published = state.published
        candidate = state.candidate
        candidate_since_ms = state.candidate_since_ms
        if raw is WorkloadRegime.UNKNOWN:
            published = WorkloadRegime.UNKNOWN
            candidate = None
            candidate_since_ms = None
        elif raw is published:
            candidate = None
            candidate_since_ms = None
        elif raw is not candidate:
            candidate = raw
            candidate_since_ms = mono_ms
        elif (
            candidate_since_ms is not None
            and mono_ms - candidate_since_ms >= self._config.minimum_transition_ms
        ):
            published = raw
            candidate = None
            candidate_since_ms = None

        state = replace(
            state,
            cpu=cpu,
            gpu=gpu,
            published=published,
            candidate=candidate,
            candidate_since_ms=candidate_since_ms,
            last_active_ms=last_active_ms,
            valid_since_ms=valid_since_ms,
        )
        if candidate is not None:
            reason = RegimeReason.TRANSITION_PENDING
        return state, self._estimate(state, snapshot, computed_at_ms, reason)

    def _estimate(
        self,
        state: WorkloadRegimeState,
        snapshot: ControlStateSnapshot,
        computed_at_ms: int,
        reason: RegimeReason,
    ) -> WorkloadRegimeEstimate:
        observed_window_ms = (
            0 if state.valid_since_ms is None else snapshot.monotonic_ms - state.valid_since_ms
        )
        if state.published is WorkloadRegime.UNKNOWN:
            confidence = 0.0
        else:
            confidence = min(1.0, observed_window_ms / self._config.confidence_full_window_ms)
        return WorkloadRegimeEstimate(
            regime=state.published,
            confidence=confidence,
            reason=reason,
            as_of_tick_id=snapshot.tick_id,
            computed_at_ms=computed_at_ms,
            evidence=RegimeEvidence(
                cpu_power_mean_w=state.cpu.mean_w,
                gpu_power_mean_w=state.gpu.mean_w,
                observed_window_ms=observed_window_ms,
            ),
        )

    def _raw_regime(
        self,
        *,
        mono_ms: int,
        cpu: _AxisState,
        gpu: _AxisState,
        last_active_ms: int | None,
    ) -> tuple[WorkloadRegime, int | None]:
        if cpu.active or gpu.active:
            active_since = [
                axis.active_since_ms
                for axis in (cpu, gpu)
                if axis.active and axis.active_since_ms is not None
            ]
            assert len(active_since) == [cpu.active, gpu.active].count(True)
            sustained = all(
                mono_ms - since >= self._config.sustained_after_ms for since in active_since
            )
            if cpu.active and gpu.active:
                # 片方の軸を捨てると同時 burst と単独 burst が trace で区別できなくなる
                # ため、両軸 active は常に組み合わせの値で残す（決定記録 0036）。
                regime = (
                    WorkloadRegime.SUSTAINED_CPU_GPU
                    if sustained
                    else WorkloadRegime.TRANSIENT_CPU_GPU
                )
            elif cpu.active:
                regime = WorkloadRegime.SUSTAINED_CPU if sustained else WorkloadRegime.TRANSIENT_CPU
            else:
                regime = WorkloadRegime.SUSTAINED_GPU if sustained else WorkloadRegime.TRANSIENT_GPU
            return regime, mono_ms

        if last_active_ms is not None and mono_ms - last_active_ms < self._config.cooldown_ms:
            return WorkloadRegime.COOLDOWN, last_active_ms
        return WorkloadRegime.IDLE, last_active_ms

    def _axis_timing(self, mono_ms: int, axis: _AxisState) -> _AxisState:
        """未確定の短い停止では、継続中の active duration を失わない。"""
        active_since_ms = axis.active_since_ms
        inactive_since_ms = axis.inactive_since_ms
        if axis.active:
            active_since_ms = active_since_ms if active_since_ms is not None else mono_ms
            inactive_since_ms = None
        elif active_since_ms is None:
            inactive_since_ms = None
        elif inactive_since_ms is None:
            inactive_since_ms = mono_ms
        elif mono_ms - inactive_since_ms >= self._config.minimum_transition_ms:
            active_since_ms = None
            inactive_since_ms = None
        return replace(axis, active_since_ms=active_since_ms, inactive_since_ms=inactive_since_ms)

    @staticmethod
    def _power(snapshot: ControlStateSnapshot, metric: str) -> float | None:
        signal = snapshot.signals_by_metric.get(metric)
        if signal is None or not signal.available:
            return None
        return signal.value

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
    def _unknown(computed_at_ms: int, reason: RegimeReason) -> WorkloadRegimeEstimate:
        return WorkloadRegimeEstimate(
            regime=WorkloadRegime.UNKNOWN,
            confidence=0.0,
            reason=reason,
            computed_at_ms=computed_at_ms,
            evidence=RegimeEvidence(observed_window_ms=0),
        )
