"""Air Balance の推定を decision trace へ残す（#81 / 決定記録 0073 §2.2 / §2.5）。

**記録するだけで、制御へは効かない。** ここが作るのは ``ControlTick`` v11 の
``air_balance`` と zone ごとの ``estimated_flow`` だけで、requested・Guard・Critical Safety・
Hardware Backend のどれにも値を返さない（AGENTS.md ルール2 / 5）。

- 推定に使う demand は Hardware Backend が実際に書いた **applied demand**（0073 §2.3）
- ``source.status: uncalibrated`` なら ``ConfiguredAirBalanceModel`` を**作らない**。
  未校正の曲線・比・熱の閾値は記録にも入らず、``disabled`` の形だけを残す（0073 §2.2）
- 熱の指標は ``air-balance.yaml`` の ``thermal_inputs`` が指す snapshot の metric から取る。
  欠測は ``None`` のまま渡し、0 で埋めない（0073 §2.4）
"""

from __future__ import annotations

from coldaisle.control.air_balance import (
    AirBalanceConfig,
    ConfiguredAirBalanceModel,
    ThermalInputs,
)
from coldaisle.control.config import ControlConfig
from coldaisle.control.schema import (
    AirBalanceRecord,
    AirBalanceTraceState,
    PerZone,
    Zone,
)
from coldaisle.control.state import ControlStateSnapshot
from coldaisle.measurement import DERIVED_PREFIX


class AirBalanceRecorder:
    """検証済みの ``air-balance.yaml`` から、tick ごとの Air Balance の記録を作る。"""

    __slots__ = ("_config", "_config_sha256", "_model")

    def __init__(self, config: AirBalanceConfig, config_sha256: str) -> None:
        self._config = config
        self._config_sha256 = config_sha256
        # **未校正ならモデルを作らない**（0073 §2.2）。`allow_uncalibrated_for_testing` を
        # 渡す経路をこの runtime は持たない。
        self._model = (
            ConfiguredAirBalanceModel(config, config_sha256) if config.calibrated else None
        )

    @classmethod
    def from_control_config(cls, control: ControlConfig) -> AirBalanceRecorder:
        """``ControlConfig`` の air-balance.yaml と、その SHA-256 から作る。"""
        return cls(control.air_balance, control.sources.air_balance.sha256)

    @property
    def enabled(self) -> bool:
        """Air Balance を推定・記録に使うか（``source.status`` だけで決まる）。"""
        return self._model is not None

    def thermal_inputs(self, snapshot: ControlStateSnapshot) -> ThermalInputs:
        """``thermal_inputs`` が指す metric の値。**使えない値は None のまま。**"""
        bindings = self._config.thermal_inputs
        signals = snapshot.signals_by_metric
        derived = snapshot.derived_by_metric

        def value(metric: str | None) -> float | None:
            if metric is None:
                return None
            if metric.startswith(DERIVED_PREFIX):
                return derived.get(metric)
            signal = signals.get(metric)
            if signal is None or not signal.available:
                return None
            return signal.value

        return ThermalInputs(
            gpu_intake_c=value(bindings.gpu_intake_c),
            case_delta_c=value(bindings.case_delta_c),
            cpu_package_c=value(bindings.cpu_package_c),
            gpu_temperature_c=value(bindings.gpu_temperature_c),
        )

    def record(
        self,
        applied: PerZone[float | None],
        thermal: ThermalInputs,
    ) -> tuple[PerZone[float | None], AirBalanceRecord]:
        """zone ごとの ``estimated_flow`` と、tick の ``air_balance`` を返す。

        ``applied`` は風量を記録してよい zone の applied demand で、書き込み結果の無い・
        確認できていない・fault のある zone は呼び出し側が None にして渡す（0073 §2.5 (a)）。
        """
        model = self._model
        if model is None:
            empty = PerZone[float | None](front=None, rear=None, top=None)
            return empty, AirBalanceRecord.disabled(
                model_id=self._config.model_id,
                config_sha256=self._config_sha256,
            )

        flows = PerZone[float | None](
            front=_flow(model, Zone.FRONT, applied.front),
            rear=_flow(model, Zone.REAR, applied.rear),
            top=_flow(model, Zone.TOP, applied.top),
        )
        if applied.front is not None and applied.rear is not None and applied.top is not None:
            estimate = model.evaluate(
                PerZone[float](front=applied.front, rear=applied.rear, top=applied.top),
                thermal,
            )
            return flows, AirBalanceRecord(
                status="enabled",
                disabled_reason=None,
                model_id=self._config.model_id,
                source_status="calibrated",
                config_sha256=self._config_sha256,
                q_front=estimate.q_front,
                q_rear=estimate.q_rear,
                q_top=estimate.q_top,
                estimated_intake=estimate.estimated_intake,
                estimated_exhaust=estimate.estimated_exhaust,
                balance_ratio=estimate.balance_ratio,
                state=AirBalanceTraceState(estimate.state.value),
                thermal_limited=estimate.thermal_limited,
                thermal_reasons=estimate.thermal_reasons,
            )
        # **風量が不明でも温度の超過は落とさない**（0073 §2.5 (b)）。熱の理由は evaluate() と
        # 同じ1箇所の実装から取り、2つ目の分類を作らない。
        reasons = model.thermal_reasons(thermal)
        return flows, AirBalanceRecord(
            status="enabled",
            disabled_reason=None,
            model_id=self._config.model_id,
            source_status="calibrated",
            config_sha256=self._config_sha256,
            q_front=flows.front,
            q_rear=flows.rear,
            q_top=flows.top,
            estimated_intake=None,
            estimated_exhaust=None,
            balance_ratio=None,
            state=AirBalanceTraceState.UNKNOWN,
            thermal_limited=bool(reasons),
            thermal_reasons=reasons,
        )


def _flow(model: ConfiguredAirBalanceModel, zone: Zone, demand: float | None) -> float | None:
    return None if demand is None else model.estimate_flow(zone, demand)
