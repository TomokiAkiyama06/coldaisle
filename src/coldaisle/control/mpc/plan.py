"""Candidate action plan と Hard Constraints（#86）。

**この module は requested までしか表現できない。** ``EffectiveZoneDemand`` も PWM も持たず、
Reactive Guard（#80）と Critical Safety（#78）を呼ぶ経路も作らない。ここでいう
「Hard Constraints」は**探索を狭めるための写し**であって、安全上の保証ではない。
最終裁定は後段の決定論的な2層が常に行う（決定記録 0028 §2.4 / 0052 §2.4）。
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.config import (
    MAX_MPC_HORIZON_STEPS,
    MpcOptimizerConfig,
    SafetyConfig,
)
from coldaisle.control.schema import Demand, PerZone, Zone

PositiveMilliseconds = Annotated[int, Field(gt=0)]


class _Frozen(BaseModel):
    """暗黙変換と余分なキーを拒む共通基底。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class InfeasiblePlanError(ValueError):
    """満たせる demand が1つも無い制約集合を作ろうとした。

    optimizer はこれを ``OptimizerStatus.ERROR`` として扱い、Fallback へ渡す。
    """


class PlanStep(_Frozen):
    """1 control step の候補 demand。``offset_ms`` は action 時刻からの相対時間。"""

    offset_ms: PositiveMilliseconds
    demands: PerZone[Demand]


class ActionPlan(_Frozen):
    """receding horizon の候補 action 列。

    **実行してよいのは最初の step だけ**（``first``）。それ以外の step は予測のためだけに
    存在し、``ControllerProposal`` へは出さない。
    """

    step_ms: PositiveMilliseconds
    steps: Annotated[tuple[PlanStep, ...], Field(min_length=1, max_length=MAX_MPC_HORIZON_STEPS)]

    @model_validator(mode="after")
    def _offsets_are_a_uniform_grid(self) -> Self:
        expected = tuple(self.step_ms * (index + 1) for index in range(len(self.steps)))
        if tuple(step.offset_ms for step in self.steps) != expected:
            raise ValueError("plan の offset_ms は step_ms の等間隔にする")
        return self

    @property
    def first(self) -> PerZone[Demand]:
        """次の control step で要求する demand。**これだけが requested になる。**"""
        return self.steps[0].demands

    @property
    def horizon_ms(self) -> int:
        """plan 全体が覆う予測 horizon。"""
        return self.step_ms * len(self.steps)

    @property
    def offsets_ms(self) -> tuple[int, ...]:
        """各 step の予測時刻（action からの相対）。"""
        return tuple(step.offset_ms for step in self.steps)

    @classmethod
    def held(cls, demands: PerZone[Demand], *, step_ms: int, steps: int) -> ActionPlan:
        """1つの demand を horizon 全体で保持する plan を作る（move blocking = 1）。

        次 tick で必ず再計算するため、v1 は step ごとに別の値を探索しない
        （決定記録 0052 §2.3）。
        """
        return cls(
            step_ms=step_ms,
            steps=tuple(
                PlanStep(offset_ms=step_ms * (index + 1), demands=demands) for index in range(steps)
            ),
        )


class ZoneBound(_Frozen):
    """1 zone の探索範囲。"""

    floor: Demand
    ceiling: Demand

    @model_validator(mode="after")
    def _floor_is_below_ceiling(self) -> Self:
        if self.floor > self.ceiling:
            raise ValueError("zone bound の floor は ceiling 以下にする")
        return self


class HardConstraintSet(_Frozen):
    """optimizer が候補を作るときに従う制約（#86）。

    構成は3つで、**どれも範囲を狭める向きにしか働かない**。

    1. 設定した zone ごとの探索範囲（``mpc.optimizer.zone_bounds``）
    2. Critical Safety の最低 demand（設定の ``zone_min_demand`` と、この tick の floor）
    3. 直前の effective demand からの変化幅（下げる速さは ``safety.ramp_down_per_s``）

    **2 を緩められない。** floor は常に ceiling と上げ幅に勝ち、下げる向きの制限にも勝つ。
    ここで守っても後段の Critical Safety は必ず掛かる。狭めるのは
    「どうせ潰される候補を評価して budget を使わない」ためで、安全の根拠ではない。
    """

    bounds: PerZone[ZoneBound]
    current: PerZone[Demand]
    """直前の effective demand。変化幅の起点。"""
    max_step_up: Demand
    max_step_down: Demand

    @classmethod
    def build(
        cls,
        *,
        optimizer: MpcOptimizerConfig,
        safety: SafetyConfig,
        safety_floor: PerZone[Demand],
        current: PerZone[Demand],
    ) -> HardConstraintSet:
        """設定とこの tick の Safety floor を重ねて最も狭い制約を作る。

        満たせる demand が無ければ ``InfeasiblePlanError``。**勝手に緩めない。**
        """
        bounds: dict[Zone, ZoneBound] = {}
        for zone in Zone:
            configured = optimizer.zone_bounds.get(zone)
            floor = max(
                configured.floor.value,
                safety.zone_min_demand.get(zone).value,
                safety_floor.get(zone),
            )
            ceiling = configured.ceiling.value
            if floor > ceiling:
                raise InfeasiblePlanError(
                    f"{zone.value}: safety floor={floor:.6f} が探索 ceiling={ceiling:.6f} を超える"
                )
            bounds[zone] = ZoneBound(floor=floor, ceiling=ceiling)
        step_s = optimizer.step_ms.value / 1000.0
        max_step_down = min(optimizer.max_step_down.value, safety.ramp_down_per_s.value * step_s)
        return cls(
            bounds=PerZone[ZoneBound](
                front=bounds[Zone.FRONT], rear=bounds[Zone.REAR], top=bounds[Zone.TOP]
            ),
            current=current,
            max_step_up=optimizer.max_step_up.value,
            max_step_down=max(0.0, min(1.0, max_step_down)),
        )

    def window(self, zone: Zone) -> tuple[float, float]:
        """その zone で許される demand の下限・上限を返す。

        Safety floor が直前の値より上にある tick では、**上げる向きの制限を外す**。
        制限を優先すると、冷却を強める必要があるときに弱いまま留まってしまう。
        """
        bound = self.bounds.get(zone)
        previous = self.current.get(zone)
        lower = max(bound.floor, previous - self.max_step_down)
        upper = min(bound.ceiling, previous + self.max_step_up)
        if upper < lower:
            # 起点が floor より下（Safety floor が上がった直後）。floor に合わせる。
            upper = lower
        return lower, upper

    def clamp(self, zone: Zone, demand: float) -> Demand:
        """候補をその zone の許容範囲へ収める。"""
        lower, upper = self.window(zone)
        return min(max(demand, lower), upper)

    def violations(self, demands: PerZone[Demand]) -> tuple[str, ...]:
        """制約を外れている zone の説明を返す。空なら実行可能。"""
        problems: list[str] = []
        for zone in Zone:
            lower, upper = self.window(zone)
            value = demands.get(zone)
            if value < lower or value > upper:
                problems.append(
                    f"{zone.value}: demand={value:.6f} が [{lower:.6f}, {upper:.6f}] の外"
                )
        return tuple(problems)

    def contains(self, demands: PerZone[Demand]) -> bool:
        """すべての zone が許容範囲に入っているか返す。"""
        return not self.violations(demands)
