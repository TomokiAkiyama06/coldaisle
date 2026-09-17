"""ML に依存しない Baseline / Fallback Controller。#79

``ControlStateSnapshot`` だけを読み、``requested_demand`` までを生成する。
Reactive Guard、Critical Safety、PWM mapping はこの層の責務ではない。
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from coldaisle.control.config import FallbackPowerCurve, FanPolicyConfig
from coldaisle.control.schema import (
    ControllerKind,
    ControllerProposal,
    PerZone,
    Reason,
    Zone,
    ZoneRequest,
)
from coldaisle.control.state import ControlStateSnapshot, SnapshotSignal
from coldaisle.metrics import MetricCatalog


class FallbackController:
    """設定済み curve を Snapshot に適用する決定論的 Controller。

    各 zone は temperature feedback と、設定されていれば Power feed-forward の
    大きい方を使う。最後に3 zoneの最大値を共有し、実測 Air Balance Model が無い
    段階でも排気側だけを相対的に強くしない。絶対風量の釣り合いは #81 の責務で、
    この調整を真の風量比とは扱わない。
    """

    def __init__(self, policy: FanPolicyConfig, catalog: MetricCatalog) -> None:
        self._validate_metric_units(policy, catalog)
        self._policy = policy
        self._last_demand: float | None = None
        self._decrease_since_mono_ms: int | None = None
        self._last_mono_ms: int | None = None

    def propose(self, snapshot: ControlStateSnapshot) -> ControllerProposal:
        """1 tick の Snapshot から Fallback 提案を作る。I/O は行わない。"""
        return self._propose(
            tick_id=snapshot.tick_id,
            ts_ms=snapshot.ts_ms,
            monotonic_ms=snapshot.monotonic_ms,
            signals=snapshot.signals_by_metric,
            snapshot_available=True,
        )

    def propose_without_snapshot(
        self, *, tick_id: int, ts_ms: int, monotonic_ms: int
    ) -> ControllerProposal:
        """Snapshot 自体を取得できない tick に保守側の設定値を提案する。

        使えない入力を 0 とみなさず、設定された temperature curve の最大 demand を
        全 zone に使う。Critical な欠測の最終裁定は後段の #78 が行う。
        """
        return self._propose(
            tick_id=tick_id,
            ts_ms=ts_ms,
            monotonic_ms=monotonic_ms,
            signals={},
            snapshot_available=False,
        )

    def _propose(
        self,
        *,
        tick_id: int,
        ts_ms: int,
        monotonic_ms: int,
        signals: dict[str, SnapshotSignal],
        snapshot_available: bool,
    ) -> ControllerProposal:
        self._check_monotonic(monotonic_ms)
        local: dict[Zone, tuple[float, Reason, bool]] = {}
        for zone in Zone:
            local[zone] = self._zone_demand(zone, signals, snapshot_available=snapshot_available)

        desired = max(item[0] for item in local.values())
        inputs_available = all(item[2] for item in local.values())
        demand = self._apply_decrease_hold(
            desired,
            monotonic_ms=monotonic_ms,
            permit_decrease=inputs_available,
        )

        requested: dict[Zone, ZoneRequest] = {}
        for zone in Zone:
            local_demand, local_reason, _ = local[zone]
            reason = local_reason
            if demand > desired:
                reason = Reason(
                    code="fallback_decrease_hold",
                    detail=self._detail(
                        f"candidate={desired:.6f}",
                        f"held={demand:.6f}",
                        f"local_reason={local_reason.code}",
                        f"local_detail={local_reason.detail}",
                    ),
                )
            elif demand > local_demand:
                reason = Reason(
                    code="fallback_coordinated_max",
                    detail=self._detail(
                        f"local={local_demand:.6f}",
                        f"shared={demand:.6f}",
                        f"local_reason={local_reason.code}",
                        f"local_detail={local_reason.detail}",
                    ),
                )
            requested[zone] = ZoneRequest(demand=demand, reason=reason)

        return ControllerProposal(
            controller=ControllerKind.FALLBACK,
            seq=tick_id,
            computed_at_ms=ts_ms,
            requested=PerZone(
                front=requested[Zone.FRONT],
                rear=requested[Zone.REAR],
                top=requested[Zone.TOP],
            ),
        )

    def _zone_demand(
        self,
        zone: Zone,
        signals: dict[str, SnapshotSignal],
        *,
        snapshot_available: bool,
    ) -> tuple[float, Reason, bool]:
        temperature_inputs = self._policy.fallback_temperature_inputs.get(zone).metrics
        available_temperatures = self._available_values(temperature_inputs, signals)
        missing_temperatures = tuple(
            metric for metric in temperature_inputs if metric not in available_temperatures
        )

        if available_temperatures:
            feedback_temperature = max(available_temperatures.values())
            temperature_demand = self._interpolate(
                tuple((point.temperature_c, point.demand) for point in self._policy.fallback_curve),
                feedback_temperature,
            )
        else:
            feedback_temperature = None
            temperature_demand = self._policy.fallback_curve[-1].demand

        power_config = (
            None
            if self._policy.fallback_power_feedforward is None
            else self._policy.fallback_power_feedforward.get(zone)
        )
        power_demand: float | None = None
        power_value: float | None = None
        if power_config is not None:
            power_signal = signals.get(power_config.metric)
            if power_signal is not None and power_signal.available:
                assert power_signal.value is not None
                power_value = power_signal.value
                power_demand = self._power_demand(power_config, power_value)

        demand = max(
            temperature_demand,
            power_demand if power_demand is not None else temperature_demand,
        )
        all_temperature_inputs_available = not missing_temperatures and snapshot_available

        if feedback_temperature is None:
            code = "fallback_temperature_unavailable"
        elif missing_temperatures:
            code = "fallback_temperature_degraded"
        elif power_config is not None and power_value is None:
            code = "fallback_power_unavailable"
        elif power_demand is not None:
            code = "fallback_feedback_feedforward"
        else:
            code = "fallback_temperature_feedback"

        details = [
            f"zone={zone.value}",
            "temperature="
            + ("unavailable" if feedback_temperature is None else f"{feedback_temperature:.6f}"),
            f"temperature_demand={temperature_demand:.6f}",
        ]
        if missing_temperatures:
            details.append(f"temperature_inputs_unavailable={','.join(missing_temperatures)}")
        if power_config is not None:
            if power_value is None:
                details.append(f"power_feedforward_disabled={power_config.metric}")
            else:
                assert power_demand is not None
                details.extend(
                    (
                        f"power={power_config.metric}:{power_value:.6f}",
                        f"power_demand={power_demand:.6f}",
                    )
                )
        details.append(f"requested={demand:.6f}")
        return (
            demand,
            Reason(code=code, detail=self._detail(*details)),
            all_temperature_inputs_available,
        )

    def _apply_decrease_hold(
        self,
        desired: float,
        *,
        monotonic_ms: int,
        permit_decrease: bool,
    ) -> float:
        previous = self._last_demand
        if previous is None or desired >= previous:
            self._last_demand = desired
            self._decrease_since_mono_ms = None
            return desired

        hysteresis = self._policy.fallback_dynamics.decrease_hysteresis
        if not permit_decrease or previous - desired <= hysteresis:
            self._decrease_since_mono_ms = None
            return previous

        if self._decrease_since_mono_ms is None:
            self._decrease_since_mono_ms = monotonic_ms
            return previous
        held_ms = monotonic_ms - self._decrease_since_mono_ms
        if held_ms < self._policy.fallback_dynamics.decrease_hold_ms:
            return previous

        self._last_demand = desired
        self._decrease_since_mono_ms = None
        return desired

    def _check_monotonic(self, monotonic_ms: int) -> None:
        if monotonic_ms < 0:
            raise ValueError("Fallback Controller の単調時計は負にできない")
        if self._last_mono_ms is not None and monotonic_ms < self._last_mono_ms:
            raise ValueError("Fallback Controller の単調時計は巻き戻せない")
        self._last_mono_ms = monotonic_ms

    @staticmethod
    def _validate_metric_units(policy: FanPolicyConfig, catalog: MetricCatalog) -> None:
        for zone in Zone:
            for metric in policy.fallback_temperature_inputs.get(zone).metrics:
                unit = catalog.unit_for(metric)
                if unit != "C":
                    raise ValueError(
                        f"Fallback temperature metric は既知の温度(C)にする: "
                        f"zone={zone.value}, metric={metric}, unit={unit}"
                    )
            if policy.fallback_power_feedforward is None:
                continue
            power_metric = policy.fallback_power_feedforward.get(zone).metric
            unit = catalog.unit_for(power_metric)
            if unit != "W":
                raise ValueError(
                    f"Fallback Power metric は既知の電力(W)にする: "
                    f"zone={zone.value}, metric={power_metric}, unit={unit}"
                )

    @staticmethod
    def _available_values(
        metrics: tuple[str, ...], signals: dict[str, SnapshotSignal]
    ) -> dict[str, float]:
        available: dict[str, float] = {}
        for metric in metrics:
            signal = signals.get(metric)
            if signal is not None and signal.available:
                assert signal.value is not None
                available[metric] = signal.value
        return available

    @classmethod
    def _power_demand(cls, config: FallbackPowerCurve, value: float) -> float:
        return cls._interpolate(
            tuple((point.power_w, point.demand) for point in config.curve),
            value,
        )

    @staticmethod
    def _interpolate(points: Sequence[tuple[float, float]], value: float) -> float:
        if value <= points[0][0]:
            return points[0][1]
        for (lower_x, lower_y), (upper_x, upper_y) in pairwise(points):
            if value <= upper_x:
                fraction = (value - lower_x) / (upper_x - lower_x)
                return lower_y + fraction * (upper_y - lower_y)
        return points[-1][1]

    @staticmethod
    def _detail(*parts: str) -> str:
        # Reason.detail の上限を、将来設定される metric 数に依存させない。
        return "; ".join(parts)[:500]
