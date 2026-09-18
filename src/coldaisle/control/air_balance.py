"""Air Balance の評価と requested demand の協調提案（#81）。

このモジュールが扱う ``effective_flow`` は、実測 CFM ではなく #75 の
characterization が作る共通の内部尺度（EFU）である。Air Balance は制御器が
``requested`` を選ぶための材料に限り、Reactive Guard・Critical Safety・Hardware
Backend には依存しない。特に Top の提案は case auxiliary exhaust だけを表し、CPU
cooling floor は後段の Critical Safety が保持する。
"""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Self

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle.control.schema import Demand, PerZone, Reason, Zone

AIR_BALANCE_CONFIG_VERSION: Literal[1] = 1

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
EffectiveFlow = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


def _yaml_sequence_to_tuple(value: object) -> object:
    """YAML 配列を読み込み後は不変な tuple として保持する。"""
    return tuple(value) if isinstance(value, list) else value


class _Frozen(BaseModel):
    """外部設定とモデル出力を暗黙変換・余分なキーから守る基底。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class AirBalanceState(StrEnum):
    """推定風量と温度指標を合わせた Air Balance の状態。"""

    BALANCED = "balanced"
    INTAKE_HEAVY = "intake_heavy"
    EXHAUST_HEAVY = "exhaust_heavy"
    THERMALLY_LIMITED = "thermally_limited"
    UNKNOWN = "unknown"


class CharacterizationSource(_Frozen):
    """モデルが実機 characterization 済みかを明示する根拠。"""

    status: Literal["uncalibrated", "calibrated"]
    basis: str = Field(min_length=1, max_length=500)


class FlowCurvePoint(_Frozen):
    """1つの demand における Airflow Index と共通尺度の対応点。"""

    demand: Demand
    airflow_index: UnitInterval
    effective_flow: EffectiveFlow


class ZoneFlowCurve(_Frozen):
    """Zone 固有の demand → Airflow Index → EFU characterization。"""

    points: Annotated[
        tuple[FlowCurvePoint, ...],
        BeforeValidator(_yaml_sequence_to_tuple),
        Field(min_length=2),
    ]

    @model_validator(mode="after")
    def _points_are_monotonic(self) -> Self:
        first = self.points[0]
        last = self.points[-1]
        if first.demand != 0.0 or last.demand != 1.0:
            raise ValueError("風量曲線は demand=0.0 と demand=1.0 を両方含める")
        if first.airflow_index != 0.0 or last.airflow_index != 1.0:
            raise ValueError("風量曲線は airflow_index=0.0 と 1.0 を両方含める")
        if first.effective_flow != 0.0:
            raise ValueError("airflow_index=0.0 の effective_flow は 0.0 にする")
        previous: FlowCurvePoint | None = None
        for point in self.points:
            if previous is not None:
                if point.demand <= previous.demand:
                    raise ValueError("風量曲線の demand は単調増加にする")
                if point.airflow_index <= previous.airflow_index:
                    raise ValueError("airflow_index は demand に対して単調増加にする")
                if point.effective_flow < previous.effective_flow:
                    raise ValueError("effective_flow は airflow_index に対して下げない")
            previous = point
        if self.points[-1].effective_flow <= self.points[0].effective_flow:
            raise ValueError("風量曲線は利用可能な effective_flow の範囲を持たせる")
        return self

    def estimate(self, demand: Demand) -> ZoneFlowEstimate:
        """demand を Airflow Index 経由で EFU に変換する。"""
        airflow_index = _interpolate_points(
            self.points,
            input_value=demand,
            input_name="demand",
            output_name="airflow_index",
        )
        effective_flow = _interpolate_points(
            self.points,
            input_value=airflow_index,
            input_name="airflow_index",
            output_name="effective_flow",
        )
        return ZoneFlowEstimate(
            airflow_index=airflow_index,
            effective_flow=effective_flow,
        )

    def demand_for_effective_flow(self, effective_flow: float) -> float:
        """目標 EFU を満たす demand を逆補間し、曲線の端では飽和させる。"""
        return _interpolate_points(
            self.points,
            input_value=effective_flow,
            input_name="effective_flow",
            output_name="demand",
        )

    @property
    def maximum_effective_flow(self) -> float:
        """この characterization が表せる最大 EFU を返す。"""
        return self.points[-1].effective_flow


class BalanceBand(_Frozen):
    """1.0 を前提にしない、校正可能な目標比と許容帯。"""

    target_ratio: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    minimum_ratio: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    maximum_ratio: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _target_is_inside_band(self) -> Self:
        if self.minimum_ratio >= self.maximum_ratio:
            raise ValueError("Air Balance の minimum_ratio は maximum_ratio より小さくする")
        if not self.minimum_ratio <= self.target_ratio <= self.maximum_ratio:
            raise ValueError("Air Balance の target_ratio は許容帯の中に置く")
        return self


class ThermalLimits(_Frozen):
    """Air Balance の状態判定に使う非 Safety の熱制約。"""

    gpu_intake_c: FiniteFloat
    case_delta_c: FiniteFloat
    cpu_package_c: FiniteFloat
    gpu_temperature_c: FiniteFloat


class AirBalanceConfig(_Frozen):
    """未校正の初期モデルと #75 の実測モデルが共有する設定形式。"""

    schema_version: Literal[1]
    model_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=120)
    source: CharacterizationSource
    flow_unit: Literal["efu"]
    zones: PerZone[ZoneFlowCurve]
    balance: BalanceBand
    thermal_limits: ThermalLimits

    @classmethod
    def from_file(cls, path: Path) -> tuple[AirBalanceConfig, str]:
        """YAML を検証し、設定と内容ハッシュを返す。"""
        text = path.read_text(encoding="utf-8")
        loaded: Any = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"Air Balance 設定が辞書ではない: {path.name}")
        return cls.model_validate(loaded), sha256(text.encode("utf-8")).hexdigest()


class ThermalInputs(_Frozen):
    """同一 tick の利用可能な温度指標。欠測値を 0 で埋めない。"""

    gpu_intake_c: FiniteFloat | None = None
    case_delta_c: FiniteFloat | None = None
    cpu_package_c: FiniteFloat | None = None
    gpu_temperature_c: FiniteFloat | None = None


class ZoneFlowEstimate(_Frozen):
    """Zone 内の相対 index と Zone 間で比較できる EFU。"""

    airflow_index: UnitInterval
    effective_flow: EffectiveFlow


class AirBalanceMetadata(_Frozen):
    """推定値を実測 CFM と誤認せず再現するための metadata。"""

    model_id: str
    source: CharacterizationSource
    flow_unit: Literal["efu"] = "efu"
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AirBalanceEstimate(_Frozen):
    """3 Zone の q、吸排気比、温度を合わせた評価結果。"""

    q_front: EffectiveFlow | None
    q_rear: EffectiveFlow | None
    q_top: EffectiveFlow | None
    estimated_intake: EffectiveFlow | None
    estimated_exhaust: EffectiveFlow | None
    balance_ratio: Annotated[float, Field(ge=0.0, allow_inf_nan=False)] | None
    state: AirBalanceState
    thermal_limited: bool
    thermal_reasons: tuple[str, ...]
    metadata: AirBalanceMetadata

    @model_validator(mode="after")
    def _components_are_consistent(self) -> Self:
        ratio_available = (
            self.q_front is not None
            and self.q_rear is not None
            and self.q_top is not None
            and self.q_front > 0.0
        )
        if ratio_available != (self.balance_ratio is not None):
            raise ValueError("balance_ratio は3つの q があり q_front > 0 のときだけ持つ")
        if ratio_available:
            assert self.q_front is not None and self.q_rear is not None and self.q_top is not None
            if self.estimated_intake != self.q_front:
                raise ValueError("estimated_intake は q_front と一致させる")
            if self.estimated_exhaust != self.q_rear + self.q_top:
                raise ValueError("estimated_exhaust は q_rear + q_top と一致させる")
        elif self.estimated_intake is not None or self.estimated_exhaust is not None:
            raise ValueError("比を計算できないときは推定吸排気量も未確定にする")
        if (self.state is AirBalanceState.UNKNOWN) == ratio_available:
            raise ValueError("q が使えるときだけ UNKNOWN 以外の状態にする")
        if self.thermal_limited != bool(self.thermal_reasons):
            raise ValueError("thermal_limited と thermal_reasons を一致させる")
        if ratio_available and self.thermal_limited != (
            self.state is AirBalanceState.THERMALLY_LIMITED
        ):
            raise ValueError("q が使えるときは熱制約と THERMALLY_LIMITED を一致させる")
        return self


class AirBalanceCoordination(_Frozen):
    """制御器が採用できる、Safety 前の requested demand 提案。"""

    candidate: PerZone[Demand]
    requested: PerZone[Demand]
    top_request_role: Literal["case_aux_exhaust"] = "case_aux_exhaust"
    before: AirBalanceEstimate
    projected: AirBalanceEstimate
    reasons: tuple[Reason, ...]

    @model_validator(mode="after")
    def _never_reduces_cooling_requests(self) -> Self:
        for zone in Zone:
            if self.requested.get(zone) < self.candidate.get(zone):
                raise ValueError("Air Balance の協調提案は既存の cooling demand を下げない")
        return self


class AirBalanceModel(Protocol):
    """Fallback / MPC が差し替えて読める Air Balance の契約。"""

    def evaluate(
        self,
        demands: PerZone[Demand],
        thermal: ThermalInputs,
    ) -> AirBalanceEstimate:
        """候補 demand の Air Balance を評価する。"""

    def coordinate(
        self,
        demands: PerZone[Demand],
        thermal: ThermalInputs,
    ) -> AirBalanceCoordination:
        """安全層より前の requested demand を提案する。"""


class UncalibratedAirBalanceError(ValueError):
    """未校正 characterization をproduction相当の経路で開こうとした。"""


class ConfiguredAirBalanceModel:
    """設定した characterization と閾値だけを使う純粋な初期実装。"""

    def __init__(
        self,
        config: AirBalanceConfig,
        config_sha256: str,
        *,
        allow_uncalibrated_for_testing: bool = False,
    ) -> None:
        if config.source.status == "uncalibrated" and not allow_uncalibrated_for_testing:
            raise UncalibratedAirBalanceError(
                "未校正 Air Balance は Mock/Replay test の明示的な opt-in なしに使えない"
            )
        self._config = config
        self._metadata = AirBalanceMetadata(
            model_id=config.model_id,
            source=config.source,
            flow_unit=config.flow_unit,
            config_sha256=config_sha256,
        )

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        allow_uncalibrated_for_testing: bool = False,
    ) -> ConfiguredAirBalanceModel:
        """YAMLからモデルを作る。未校正値はtest用途を明示した場合だけ許可する。"""
        config, config_sha256 = AirBalanceConfig.from_file(path)
        return cls(
            config,
            config_sha256,
            allow_uncalibrated_for_testing=allow_uncalibrated_for_testing,
        )

    def evaluate(
        self,
        demands: PerZone[Demand],
        thermal: ThermalInputs,
    ) -> AirBalanceEstimate:
        """候補 demand を q へ写像し、温度指標を含めて状態を判定する。"""
        zones = PerZone[ZoneFlowEstimate](
            front=self._config.zones.front.estimate(demands.front),
            rear=self._config.zones.rear.estimate(demands.rear),
            top=self._config.zones.top.estimate(demands.top),
        )
        thermal_reasons = self._thermal_reasons(thermal)
        q_front = zones.front.effective_flow
        q_rear = zones.rear.effective_flow
        q_top = zones.top.effective_flow
        if q_front <= 0.0:
            return self._unknown_estimate(q_front, q_rear, q_top, thermal_reasons)

        estimated_exhaust = q_rear + q_top
        ratio = estimated_exhaust / q_front
        if thermal_reasons:
            state = AirBalanceState.THERMALLY_LIMITED
        elif ratio < self._config.balance.minimum_ratio:
            state = AirBalanceState.INTAKE_HEAVY
        elif ratio > self._config.balance.maximum_ratio:
            state = AirBalanceState.EXHAUST_HEAVY
        else:
            state = AirBalanceState.BALANCED
        return AirBalanceEstimate(
            q_front=q_front,
            q_rear=q_rear,
            q_top=q_top,
            estimated_intake=q_front,
            estimated_exhaust=estimated_exhaust,
            balance_ratio=ratio,
            state=state,
            thermal_limited=bool(thermal_reasons),
            thermal_reasons=thermal_reasons,
            metadata=self._metadata,
        )

    def coordinate(
        self,
        demands: PerZone[Demand],
        thermal: ThermalInputs,
    ) -> AirBalanceCoordination:
        """Exhaust 過多なら Front、熱を伴う Intake 過多なら Rear→Top を上げる。"""
        before = self.evaluate(demands, thermal)
        requested = demands
        reasons: list[Reason] = []
        ratio = before.balance_ratio
        assert (ratio is None) == (before.state is AirBalanceState.UNKNOWN)

        zero_intake_with_exhaust = (
            before.q_front == 0.0
            and before.q_rear is not None
            and before.q_top is not None
            and before.q_rear + before.q_top > 0.0
        )
        if (ratio is not None and ratio > self._config.balance.maximum_ratio) or (
            zero_intake_with_exhaust
        ):
            assert before.q_rear is not None and before.q_top is not None
            exhaust_flow = before.q_rear + before.q_top
            target_front_flow = exhaust_flow / self._config.balance.target_ratio
            front_demand = self._config.zones.front.demand_for_effective_flow(target_front_flow)
            requested = PerZone[Demand](
                front=max(requested.front, front_demand),
                rear=requested.rear,
                top=requested.top,
            )
            if requested.front > demands.front:
                reasons.append(
                    Reason(
                        code="front_makeup_air",
                        detail=(
                            "Front 推定風量が0で排気が動作中のため make-up air を増やす"
                            if zero_intake_with_exhaust
                            else "Exhaust 過多のため Front make-up air を増やす"
                        ),
                    )
                )
        elif (
            ratio is not None
            and ratio < self._config.balance.minimum_ratio
            and before.thermal_limited
        ):
            requested, reasons = self._increase_exhaust(demands, before)

        return AirBalanceCoordination(
            candidate=demands,
            requested=requested,
            before=before,
            projected=self.evaluate(requested, thermal),
            reasons=tuple(reasons),
        )

    def _increase_exhaust(
        self,
        demands: PerZone[Demand],
        estimate: AirBalanceEstimate,
    ) -> tuple[PerZone[Demand], list[Reason]]:
        # Rear を先に使い切り、不足分だけ Top を上げる順序は決定記録 0026（FINAL）の
        # 「通常のケース換気は Front + Rear、Top の case_aux_exhaust_demand は不足時のみ」に従う。
        # response matrix による zone 選択へ変えるなら、0026 を置き換える決定記録が先に要る。
        assert estimate.q_front is not None
        assert estimate.q_rear is not None
        assert estimate.q_top is not None
        required_exhaust = estimate.q_front * self._config.balance.target_ratio
        extra_flow = max(0.0, required_exhaust - estimate.q_rear - estimate.q_top)
        rear_target = min(
            self._config.zones.rear.maximum_effective_flow,
            estimate.q_rear + extra_flow,
        )
        rear_demand = max(
            demands.rear,
            self._config.zones.rear.demand_for_effective_flow(rear_target),
        )
        requested = PerZone[Demand](
            front=demands.front,
            rear=rear_demand,
            top=demands.top,
        )
        reasons: list[Reason] = []
        if rear_demand > demands.rear:
            reasons.append(
                Reason(
                    code="rear_thermal_exhaust",
                    detail="Intake 過多と熱制約のため Rear exhaust を優先して増やす",
                )
            )

        rear_flow = self._config.zones.rear.estimate(requested.rear).effective_flow
        remaining = max(0.0, required_exhaust - rear_flow - estimate.q_top)
        if remaining > 0.0:
            top_target = estimate.q_top + remaining
            top_demand = max(
                demands.top,
                self._config.zones.top.demand_for_effective_flow(top_target),
            )
            requested = PerZone[Demand](
                front=requested.front,
                rear=requested.rear,
                top=top_demand,
            )
            if top_demand > demands.top:
                reasons.append(
                    Reason(
                        code="top_case_aux_exhaust",
                        detail="Rear の利用可能風量を超える不足分だけ Top case auxiliary を増やす",
                    )
                )
        return requested, reasons

    def _thermal_reasons(self, thermal: ThermalInputs) -> tuple[str, ...]:
        limits = self._config.thermal_limits
        comparisons = (
            ("gpu_intake", thermal.gpu_intake_c, limits.gpu_intake_c),
            ("case_delta", thermal.case_delta_c, limits.case_delta_c),
            ("cpu_package", thermal.cpu_package_c, limits.cpu_package_c),
            ("gpu_temperature", thermal.gpu_temperature_c, limits.gpu_temperature_c),
        )
        return tuple(
            name for name, value, limit in comparisons if value is not None and value >= limit
        )

    def _unknown_estimate(
        self,
        q_front: float,
        q_rear: float,
        q_top: float,
        thermal_reasons: tuple[str, ...],
    ) -> AirBalanceEstimate:
        return AirBalanceEstimate(
            q_front=q_front,
            q_rear=q_rear,
            q_top=q_top,
            estimated_intake=None,
            estimated_exhaust=None,
            balance_ratio=None,
            state=AirBalanceState.UNKNOWN,
            thermal_limited=bool(thermal_reasons),
            thermal_reasons=thermal_reasons,
            metadata=self._metadata,
        )


def _interpolate_points(
    points: tuple[FlowCurvePoint, ...],
    *,
    input_value: float,
    input_name: Literal["demand", "airflow_index", "effective_flow"],
    output_name: Literal["demand", "airflow_index", "effective_flow"],
) -> float:
    """単調な characterization 点を線形補間し、範囲外では端点へ飽和させる。"""
    first = points[0]
    last = points[-1]
    if input_value <= getattr(first, input_name):
        return float(getattr(first, output_name))
    if input_value >= getattr(last, input_name):
        return float(getattr(last, output_name))
    for lower, upper in pairwise(points):
        lower_input = float(getattr(lower, input_name))
        upper_input = float(getattr(upper, input_name))
        if input_value <= upper_input:
            lower_output = float(getattr(lower, output_name))
            upper_output = float(getattr(upper, output_name))
            if upper_input == lower_input:
                return upper_output
            proportion = (input_value - lower_input) / (upper_input - lower_input)
            return lower_output + proportion * (upper_output - lower_output)
    raise AssertionError("検証済み characterization の補間範囲に到達できなかった")
