"""Learned MPC の目的関数（#86）。

**Critical Safety の条件はここに入れない。** 絶対温度上限・最低安全 demand・CPU cooling floor は
コストではなく制約で、後段の #78 が決定論的に裁定する（決定記録 0027 / 0052 §2.4）。
この module が扱うのは「安全な範囲のどこを選ぶか」だけである。

項ごとの重みは Supervisor（#88）が出す ``SupervisorObjectiveWeights`` を使う。単位の違う項を
足すため、各項は設定の基準量（``mpc.optimizer.cost_scales``）で割って無単位にする。
"""

from __future__ import annotations

import math
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.control.acoustic import AcousticCostModel
from coldaisle.control.air_balance import AirBalanceModel, BalanceBand, ThermalInputs
from coldaisle.control.config import MpcOptimizerConfig
from coldaisle.control.mpc.counterfactual import PlanPrediction
from coldaisle.control.mpc.plan import ActionPlan
from coldaisle.control.schema import (
    Demand,
    PerZone,
    SupervisorObjectiveWeights,
    SupervisorTargetBand,
    TemperatureTarget,
    Zone,
)

NonNegativeFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CostTerms(_Frozen):
    """重みを掛けたあとの項ごとの寄与（無単位）。合計が ``PlanCost.total``。"""

    gpu_temperature: NonNegativeFloat
    cpu_temperature: NonNegativeFloat
    balance: NonNegativeFloat
    acoustic: NonNegativeFloat
    change: NonNegativeFloat


class PlanCost(_Frozen):
    """1つの候補 plan の総合コスト。**demand を持たない。**"""

    total: NonNegativeFloat
    terms: CostTerms
    steps: int = Field(gt=0)
    balance_known_steps: int = Field(ge=0)
    """Air Balance の比を推定できた step 数。0 なら balance 項は全 step が未知コスト。"""


class MpcCostUnusableError(ValueError):
    """内部モデルの target schema が目的関数の要求を満たしていない。

    runtime はこれを model 読込の失敗として扱い、Fallback を続ける。
    """


class MpcCostModel:
    """候補 plan と予測から無単位の総合コストを決定論的に出す。

    Acoustic（#94）と Air Balance（#81）は任意依存で、無ければその項は 0 になる。
    **無い依存を「良い」と読み替えない**ため、Air Balance は比を推定できなかった step に
    設定した ``unknown_balance_cost`` を課す。
    """

    def __init__(
        self,
        optimizer: MpcOptimizerConfig,
        *,
        acoustic: AcousticCostModel | None = None,
        air_balance: AirBalanceModel | None = None,
        balance_band: BalanceBand | None = None,
    ) -> None:
        if (air_balance is None) != (balance_band is None):
            # 目標比は #81 の設定が持つ。MPC 側に写すと同じ定数が2箇所になる。
            raise MpcCostUnusableError("Air Balance Model と BalanceBand は一緒に渡す")
        self._optimizer = optimizer
        self._acoustic = acoustic
        self._air_balance = air_balance
        self._balance_band = balance_band
        self._cpu_metric = optimizer.cost_metrics.cpu_temperature
        self._gpu_metric = optimizer.cost_metrics.gpu_temperature

    @property
    def required_metrics(self) -> tuple[str, ...]:
        """内部モデルの target schema に必須の metric。"""
        return (self._cpu_metric, self._gpu_metric)

    def evaluate(
        self,
        *,
        plan: ActionPlan,
        prediction: PlanPrediction,
        weights: SupervisorObjectiveWeights,
        target_band: SupervisorTargetBand,
        previous: PerZone[Demand],
    ) -> PlanCost:
        """plan のコストを返す。同じ入力からは必ず同じ値になる。"""
        if not prediction.matches(plan):
            raise MpcCostUnusableError("予測の step 列が候補 plan と一致しない")
        scales = self._optimizer.cost_scales
        gpu_terms: list[float] = []
        cpu_terms: list[float] = []
        balance_terms: list[float] = []
        acoustic_terms: list[float] = []
        change_terms: list[float] = []
        balance_known = 0

        last = previous
        for step, target in zip(plan.steps, prediction.targets, strict=True):
            cpu_c = self._value(target.values, self._cpu_metric)
            gpu_c = self._value(target.values, self._gpu_metric)
            cpu_terms.append(
                self._band_cost(cpu_c, target_band.cpu_temperature, scales.temperature_c.value)
            )
            gpu_terms.append(
                self._band_cost(gpu_c, target_band.gpu_temperature, scales.temperature_c.value)
            )
            balance_cost, known = self._balance_cost(step.demands, cpu_c=cpu_c, gpu_c=gpu_c)
            balance_terms.append(balance_cost)
            balance_known += int(known)
            acoustic_terms.append(self._acoustic_cost(step.demands))
            change_terms.append(self._change_cost(step.demands, last))
            last = step.demands

        steps = len(plan.steps)
        terms = CostTerms(
            gpu_temperature=weights.gpu_temperature * math.fsum(gpu_terms) / steps,
            cpu_temperature=weights.cpu_temperature * math.fsum(cpu_terms) / steps,
            balance=weights.balance * math.fsum(balance_terms) / steps,
            acoustic=weights.acoustic * math.fsum(acoustic_terms) / steps,
            change=weights.change * math.fsum(change_terms) / steps,
        )
        total = math.fsum(
            (
                terms.gpu_temperature,
                terms.cpu_temperature,
                terms.balance,
                terms.acoustic,
                terms.change,
            )
        )
        if not math.isfinite(total):
            raise MpcCostUnusableError("総合コストが有限でない")
        return PlanCost(total=total, terms=terms, steps=steps, balance_known_steps=balance_known)

    def _value(self, values: dict[str, float], metric: str) -> float:
        try:
            return values[metric]
        except KeyError as exc:
            raise MpcCostUnusableError(f"予測に必要な metric が無い: {metric}") from exc

    @staticmethod
    def _band_cost(value: float, target: TemperatureTarget, scale: float) -> float:
        """target band の外へ出た分だけを二乗で数える。

        band の中は等価に扱う。帯の中で最も低い値を選ばせると、常に最大風量が最適になる。
        """
        excess = max(0.0, value - target.upper_c, target.lower_c - value)
        return (excess / scale) ** 2

    def _balance_cost(
        self, demands: PerZone[Demand], *, cpu_c: float, gpu_c: float
    ) -> tuple[float, bool]:
        if self._air_balance is None or self._balance_band is None:
            # Model を繋いでいない（項が無い）ことと、繋いだ上で比が不明なことは区別する。
            # 前者は 0、後者は設定した ``unknown_balance_cost`` を課す。
            return 0.0, False
        estimate = self._air_balance.evaluate(
            demands,
            ThermalInputs(cpu_package_c=cpu_c, gpu_temperature_c=gpu_c),
        )
        ratio = estimate.balance_ratio
        if ratio is None:
            return self._optimizer.unknown_balance_cost.value, False
        deviation = ratio - self._balance_band.target_ratio
        return (deviation / self._optimizer.cost_scales.balance_ratio.value) ** 2, True

    def _acoustic_cost(self, demands: PerZone[Demand]) -> float:
        if self._acoustic is None:
            return 0.0
        estimate = self._acoustic.estimate(demands)
        if estimate is None:
            return 0.0
        return estimate.acoustic_cost / self._optimizer.cost_scales.acoustic_cost.value

    def _change_cost(self, demands: PerZone[Demand], previous: PerZone[Demand]) -> float:
        scale = self._optimizer.cost_scales.demand_change.value
        return math.fsum(((demands.get(zone) - previous.get(zone)) / scale) ** 2 for zone in Zone)
