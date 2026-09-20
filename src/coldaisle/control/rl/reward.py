"""学習環境の reward（#105 / 決定記録 0058 §2.4）。

**安全は reward に入らない。** 絶対温度上限・最低安全 demand は重み付きの項ではなく、
`SafetyLedgerEntry` として別に数え、episode の比較では辞書式に先に立つ
（決定記録 0052 §4 (c) / 0054 §2.4 と同じ理由）。重みで表した安全は、重みの設定次第で
ほかの項の改善に相殺される。

**採点の基準は設定が所有する。** target band は `rl-training.yaml` の `reward.target_band`
から取り、agent が出した action の band は使わない。action の band で採点すると、
agent は帯を広げるだけで reward を上げられる。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.acoustic import AcousticCostModel
from coldaisle.control.model.thermal import ThermalMetricName
from coldaisle.control.rl.config import RewardConfig
from coldaisle.control.schema import Demand, PerZone, TemperatureTarget, Zone

REWARD_SCHEMA_VERSION: Literal[1] = 1
"""`RewardBreakdown` の形の版。項の意味を変えたら上げる。"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RewardUnusableError(ValueError):
    """reward を出せない入力を渡した（観測に必要な metric が無い、など）。"""


class RewardTerms(_Frozen):
    """重みを掛けたあとの項ごとの寄与（無単位）。合計が `RewardBreakdown.cost`。"""

    cpu_temperature: float = Field(ge=0.0, allow_inf_nan=False)
    gpu_temperature: float = Field(ge=0.0, allow_inf_nan=False)
    acoustic: float = Field(ge=0.0, allow_inf_nan=False)
    change: float = Field(ge=0.0, allow_inf_nan=False)


class RewardBreakdown(_Frozen):
    """1 step の reward。**安全の項を持たない。**"""

    schema_version: Literal[1] = REWARD_SCHEMA_VERSION
    version: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=64)
    terms: RewardTerms
    cost: float = Field(ge=0.0, allow_inf_nan=False)
    reward: float = Field(le=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _reward_is_the_negated_cost(self) -> Self:
        total = math.fsum(
            (
                self.terms.cpu_temperature,
                self.terms.gpu_temperature,
                self.terms.acoustic,
                self.terms.change,
            )
        )
        if not math.isclose(total, self.cost, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("reward の項の合計が cost と一致しない")
        if not math.isclose(self.reward, -self.cost, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("reward は cost の符号反転にする")
        return self


class SafetyLedgerEntry(_Frozen):
    """1 step の安全側の事実。**reward と足し合わせない。**"""

    ceiling_exceedances: int = Field(ge=0)
    """`safety.absolute_temp_ceiling_c` を超えた観測の数（metric ごとに数える）。"""
    floor_shortfalls: int = Field(ge=0)
    """設定した最低安全 demand を下回った (step, zone) の数。"""
    margin_c: float | None = Field(default=None, allow_inf_nan=False)
    """絶対上限までの最小余裕。観測が1つも無ければ `None`。"""

    @property
    def violated(self) -> bool:
        """この step が安全側の条件を破ったか。"""
        return self.ceiling_exceedances > 0 or self.floor_shortfalls > 0


class RewardFunction:
    """設定した版の reward を決定論的に出す。同じ入力からは同じ値になる。"""

    __slots__ = ("_acoustic", "_config")

    def __init__(self, config: RewardConfig, *, acoustic: AcousticCostModel | None = None) -> None:
        self._config = config
        self._acoustic = acoustic

    @property
    def version(self) -> str:
        """episode の結果に残す reward 版。"""
        return self._config.version

    @property
    def target_band_source(self) -> str:
        """採点の基準がどこから来たか。**action ではない**ことを記録に残す。"""
        return "rl-training.yaml:reward.target_band"

    @property
    def required_metrics(self) -> tuple[ThermalMetricName, str]:
        """観測に必須の metric。"""
        return (self._config.metrics.cpu_temperature, self._config.metrics.gpu_temperature)

    def evaluate(
        self,
        *,
        values: Mapping[str, float],
        demands: PerZone[Demand],
        previous: PerZone[Demand],
    ) -> RewardBreakdown:
        """実測（または simulator の観測）から reward を出す。"""
        scales = self._config.scales
        weights = self._config.weights
        band = self._config.target_band
        cpu = self._value(values, self._config.metrics.cpu_temperature)
        gpu = self._value(values, self._config.metrics.gpu_temperature)
        terms = RewardTerms(
            cpu_temperature=weights.cpu_temperature.value
            * self._band_cost(cpu, band.cpu_temperature, scales.temperature_c.value),
            gpu_temperature=weights.gpu_temperature.value
            * self._band_cost(gpu, band.gpu_temperature, scales.temperature_c.value),
            acoustic=weights.acoustic.value * self._acoustic_cost(demands),
            change=weights.change.value * self._change_cost(demands, previous),
        )
        cost = math.fsum(
            (terms.cpu_temperature, terms.gpu_temperature, terms.acoustic, terms.change)
        )
        if not math.isfinite(cost):
            raise RewardUnusableError("reward の合計が有限でない")
        return RewardBreakdown(version=self.version, terms=terms, cost=cost, reward=-cost)

    @staticmethod
    def _value(values: Mapping[str, float], metric: str) -> float:
        try:
            return values[metric]
        except KeyError as error:
            raise RewardUnusableError(f"reward に必要な metric が観測に無い: {metric}") from error

    @staticmethod
    def _band_cost(value: float, target: TemperatureTarget, scale: float) -> float:
        """target band の外へ出た分だけを二乗で数える。

        帯の中は等価に扱う。帯の中で最も低い値に報酬を与えると、常に最大風量が最適になる
        （決定記録 0052 §2.3 と同じ理由）。
        """
        excess = max(0.0, value - target.upper_c, target.lower_c - value)
        return (excess / scale) ** 2

    def _acoustic_cost(self, demands: PerZone[Demand]) -> float:
        if self._acoustic is None:
            return 0.0
        estimate = self._acoustic.estimate(demands)
        if estimate is None:
            return 0.0
        return estimate.acoustic_cost / self._config.scales.acoustic_cost.value

    def _change_cost(self, demands: PerZone[Demand], previous: PerZone[Demand]) -> float:
        scale = self._config.scales.demand_change.value
        return math.fsum(((demands.get(zone) - previous.get(zone)) / scale) ** 2 for zone in Zone)
