"""Learned MPC optimizer（#86）。

出せるのは ``requested_demand`` までで、実行するのは plan の最初の step だけ。
次 tick で再計算する（receding horizon）。timeout・実行不能・モデル異常では解を返さず、
呼び出し側が #79 Fallback へ落とす。

探索は決定論的な座標降下で、**出発点は必ず Fallback（Baseline）の demand**。採用する解は
必ず incumbent なので、内部モデルの上では Baseline 以下のコストにしかならない。
乱数は使わず、同じ入力・同じ単調時計の列からは同じ解を返す（決定記録 0052 §2.3）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.config import MpcOptimizerConfig
from coldaisle.control.model.thermal import ObservedThermalInput, ThermalPrediction
from coldaisle.control.mpc.cost import MpcCostModel, MpcCostUnusableError, PlanCost
from coldaisle.control.mpc.counterfactual import (
    MpcModelBinding,
    MpcModelUnusableError,
    PlannedThermalInput,
    PlanPrediction,
)
from coldaisle.control.mpc.plan import ActionPlan, HardConstraintSet, InfeasiblePlanError
from coldaisle.control.schema import (
    Demand,
    OptimizerStatus,
    PerZone,
    Reason,
    SupervisorObjectiveWeights,
    SupervisorTargetBand,
    Zone,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

_ZONE_ORDER: tuple[Zone, ...] = (Zone.FRONT, Zone.REAR, Zone.TOP)
"""座標降下の掃引順。**固定**。順序が変わると同じ入力から別の解が出る。"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class MpcSolution(_Frozen):
    """optimizer が選んだ plan と、その根拠。

    ``requested`` は plan の最初の step と**必ず一致する**。型で縛ることで、
    後段へ「探索した別の step」や「plan と無関係な値」を渡せないようにする。
    """

    plan: ActionPlan
    requested: PerZone[Demand]
    cost: PlanCost
    baseline_cost: PlanCost
    baseline_requested: PerZone[Demand]
    anchor_inference_id: Sha256
    """この解の根拠になった anchor 推論（#85 の判定対象）。"""
    prediction: PlanPrediction
    """採用した plan に対する予測 future。**同じ探索で評価したものだけ**を持つ。

    Shadow Mode（#90）が記録し、あとで実測と突き合わせる。別の候補の予測を後から
    添えられないよう、plan の識別子と anchor 推論の一致を型の不変条件にする。
    """
    evaluations: int = Field(gt=0)

    @model_validator(mode="after")
    def _is_the_first_step_and_no_worse_than_baseline(self) -> Self:
        if self.requested != self.plan.first:
            raise ValueError("requested は plan の最初の step と一致させる")
        if self.cost.total > self.baseline_cost.total:
            # incumbent から始める探索では起こらない。起きたら探索の不変条件が壊れている。
            raise ValueError("採用した解のコストが Baseline を上回っている")
        if not self.prediction.matches(self.plan):
            raise ValueError("解が別の候補 plan の予測を持っている")
        if self.prediction.anchor_inference_id != self.anchor_inference_id:
            raise ValueError("解の予測が別の anchor 推論に属している")
        return self

    @property
    def improvement(self) -> float:
        """Baseline に対するコストの改善量（0 以上）。"""
        return self.baseline_cost.total - self.cost.total


class OptimizerOutcome(_Frozen):
    """1 tick の optimizer の結果。``OK`` のときだけ解を持つ。"""

    status: OptimizerStatus
    reason: Reason
    evaluations: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    solution: MpcSolution | None = None

    @model_validator(mode="after")
    def _only_ok_carries_a_solution(self) -> Self:
        if (self.status is OptimizerStatus.OK) != (self.solution is not None):
            raise ValueError("解を持てるのは OK の結果だけ")
        return self


class LearnedMpcOptimizer:
    """候補 demand を決定論的に探索する receding horizon optimizer。

    hardware も Guard も Safety も呼ばない。呼べるのは束縛済みの内部モデルとコスト関数だけ。
    """

    def __init__(
        self,
        binding: MpcModelBinding,
        config: MpcOptimizerConfig,
        cost_model: MpcCostModel,
        *,
        budget_ms: int,
        monotonic_ms: Callable[[], int],
    ) -> None:
        if budget_ms <= 0:
            raise ValueError("optimizer の budget_ms は正にする")
        self._binding = binding
        self._config = config
        self._cost_model = cost_model
        self._budget_ms = budget_ms
        self._monotonic_ms = monotonic_ms
        self._check_model_covers_the_horizon()

    def _check_model_covers_the_horizon(self) -> None:
        """設定した horizon と目的関数を、内部モデルの target schema と突き合わせる。

        合わない組み合わせは tick ごとに失敗させず、生成時に拒む。runtime はこれを
        model 読込の失敗として Fallback にする。
        """
        schema = self._binding.model.target_schema
        available = set(schema.horizons_ms)
        needed = {self._config.step_ms.value * (index + 1) for index in range(self._config.steps)}
        missing = sorted(needed - available)
        if missing:
            raise MpcModelUnusableError(
                f"内部モデルの target horizon が control step を覆っていない: {missing}"
            )
        metrics = set(schema.metrics)
        missing_metrics = sorted(set(self._cost_model.required_metrics) - metrics)
        if missing_metrics:
            raise MpcModelUnusableError(
                f"内部モデルの target metric に目的関数の必須項目が無い: {missing_metrics}"
            )

    def solve(
        self,
        *,
        observed: ObservedThermalInput,
        anchor: ThermalPrediction,
        anchor_inference_id: str,
        constraints: HardConstraintSet,
        weights: SupervisorObjectiveWeights,
        target_band: SupervisorTargetBand,
        baseline: PerZone[Demand],
        budget_started_mono_ms: int | None = None,
    ) -> OptimizerOutcome:
        """この tick の requested を探す。**解けなければ解を返さない。**

        ``budget_started_mono_ms`` を渡すと、その時刻から ``budget_ms`` を数える。
        呼び出し側は anchor 推論と Confidence 判定に使った時間も予算に含められる。
        """
        started_ms = (
            self._monotonic_ms() if budget_started_mono_ms is None else budget_started_mono_ms
        )
        evaluations = 0
        # 入口で見る。anchor 推論と Confidence 判定だけで予算を使い切った tick に、
        # さらに1回まるごとモデルを回させない（予測は高価になりうる）。
        if self._out_of_budget(started_ms):
            return self._timed_out(started_ms, evaluations)
        try:
            # 格子の算出も制約の交わりを読むので、実行不能はここで捕まえる。
            levels = {zone: self._levels(constraints, zone) for zone in _ZONE_ORDER}
            baseline_requested = self._clamped(constraints, baseline)
            if self._out_of_budget(started_ms):
                # 制約の組み立てで越えた場合も、モデルを呼ぶ前に止める。
                return self._timed_out(started_ms, evaluations)
            baseline_cost, baseline_prediction = self._evaluate(
                baseline_requested,
                observed,
                anchor,
                anchor_inference_id,
                constraints,
                weights,
                target_band,
            )
        except InfeasiblePlanError as error:
            return self._failed(OptimizerStatus.ERROR, "infeasible", str(error), started_ms, 0)
        except MpcCostUnusableError as error:
            return self._failed(OptimizerStatus.ERROR, "cost_unusable", str(error), started_ms, 0)
        evaluations += 1
        incumbent = baseline_requested
        best_cost = baseline_cost
        best_prediction = baseline_prediction
        # Baseline の評価だけで予算を使い切ることもある。**評価のあとにも必ず見る。**
        if self._out_of_budget(started_ms):
            return self._timed_out(started_ms, evaluations)

        max_evaluations = self._config.max_evaluations.value
        for _sweep in range(self._config.sweeps.value):
            improved = False
            for zone in _ZONE_ORDER:
                for candidate_demand in levels[zone]:
                    candidate = self._with_zone(incumbent, zone, candidate_demand)
                    if candidate == incumbent:
                        continue
                    if evaluations >= max_evaluations or self._out_of_budget(started_ms):
                        return self._timed_out(started_ms, evaluations)
                    try:
                        cost, prediction = self._evaluate(
                            candidate,
                            observed,
                            anchor,
                            anchor_inference_id,
                            constraints,
                            weights,
                            target_band,
                        )
                    except MpcCostUnusableError as error:
                        return self._failed(
                            OptimizerStatus.ERROR,
                            "cost_unusable",
                            str(error),
                            started_ms,
                            evaluations,
                        )
                    except InfeasiblePlanError as error:
                        # 格子は window から作るので通常は起きない。起きたら探索を止める。
                        return self._failed(
                            OptimizerStatus.ERROR,
                            "infeasible",
                            str(error),
                            started_ms,
                            evaluations,
                        )
                    evaluations += 1
                    # 厳密な改善だけを採る。等コストで乗り換えると、同じ入力でも
                    # 評価順によって解が変わる（再現性が落ちる）。
                    if cost.total < best_cost.total:
                        best_cost = cost
                        # 予測もコストと一緒に持ち替える。あとで採用した plan の予測を
                        # 引き直すと、探索に使った予測と別のものを記録しうる。
                        best_prediction = prediction
                        incumbent = candidate
                        improved = True
                    # 最後の候補で予算を越えたまま OK を返さない。
                    if self._out_of_budget(started_ms):
                        return self._timed_out(started_ms, evaluations)
            if not improved:
                break

        plan = self._plan(incumbent)
        solution = MpcSolution(
            plan=plan,
            requested=plan.first,
            cost=best_cost,
            baseline_cost=baseline_cost,
            baseline_requested=baseline_requested,
            anchor_inference_id=anchor_inference_id,
            prediction=best_prediction,
            evaluations=evaluations,
        )
        return OptimizerOutcome(
            status=OptimizerStatus.OK,
            reason=Reason(
                code="optimizer_ok",
                detail=(
                    f"evaluations={evaluations}; cost={best_cost.total:.6f}; "
                    f"baseline_cost={baseline_cost.total:.6f}"
                ),
            ),
            evaluations=evaluations,
            elapsed_ms=self._elapsed_ms(started_ms),
            solution=solution,
        )

    def _levels(self, constraints: HardConstraintSet, zone: Zone) -> tuple[float, ...]:
        """その zone で試す demand の格子。両端を含む等間隔で、順序は固定。"""
        lower, upper = constraints.window(zone)
        count = self._config.candidate_levels.value
        if upper <= lower:
            return (lower,)
        return tuple(lower + (upper - lower) * index / (count - 1) for index in range(count))

    def _plan(self, demands: PerZone[Demand]) -> ActionPlan:
        return ActionPlan.held(
            demands, step_ms=self._config.step_ms.value, steps=self._config.steps
        )

    def _timed_out(self, started_ms: int, evaluations: int) -> OptimizerOutcome:
        """予算切れの結果を返す。**解は付けない。**"""
        return self._failed(
            OptimizerStatus.TIMEOUT,
            "optimizer_budget_exhausted",
            f"evaluations={evaluations}; budget_ms={self._budget_ms}",
            started_ms,
            evaluations,
        )

    def _evaluate(
        self,
        demands: PerZone[Demand],
        observed: ObservedThermalInput,
        anchor: ThermalPrediction,
        anchor_inference_id: str,
        constraints: HardConstraintSet,
        weights: SupervisorObjectiveWeights,
        target_band: SupervisorTargetBand,
    ) -> tuple[PlanCost, PlanPrediction]:
        """候補のコストと、その根拠になった予測を**対で**返す（#90 が記録する）。"""
        violations = constraints.violations(demands)
        if violations:
            raise InfeasiblePlanError("; ".join(violations))
        plan = self._plan(demands)
        prediction = self._binding.model.predict_plan(
            PlannedThermalInput(observed=observed, plan=plan)
        )
        self._check_prediction(prediction, anchor, anchor_inference_id)
        cost = self._cost_model.evaluate(
            plan=plan,
            prediction=prediction,
            weights=weights,
            target_band=target_band,
            previous=constraints.current,
        )
        return cost, prediction

    def _check_prediction(
        self,
        prediction: PlanPrediction,
        anchor: ThermalPrediction,
        anchor_inference_id: str,
    ) -> None:
        """予測が束縛した artifact と anchor 推論に属しているか確かめる。

        別の推論・別の artifact の予測を混ぜると、#85 の判定が別の入力に付け替わる。
        版と artifact hash は Registry の証拠（``MpcModelBinding``）を正として照合する。
        """
        if prediction.anchor_inference_id != anchor_inference_id:
            raise MpcCostUnusableError("予測が別の anchor 推論に属している")
        if prediction.model_version != self._binding.model_version:
            raise MpcCostUnusableError("予測が別の model 版に属している")
        if prediction.artifact_sha256 != anchor.artifact_sha256:
            raise MpcCostUnusableError("予測が anchor と別の artifact に属している")
        if prediction.input_action_ts_ms != anchor.input_action_ts_ms:
            raise MpcCostUnusableError("予測が anchor と別の時刻の入力に属している")
        if prediction.artifact_verification is not self._binding.artifact_verification:
            raise MpcCostUnusableError("予測の artifact 検証状態が束縛と食い違っている")

    @staticmethod
    def _with_zone(demands: PerZone[Demand], zone: Zone, value: float) -> PerZone[Demand]:
        updated = {item.value: demands.get(item) for item in _ZONE_ORDER}
        updated[zone.value] = value
        return PerZone[Demand](**updated)

    @staticmethod
    def _clamped(constraints: HardConstraintSet, demands: PerZone[Demand]) -> PerZone[Demand]:
        return PerZone[Demand](
            front=constraints.clamp(Zone.FRONT, demands.front),
            rear=constraints.clamp(Zone.REAR, demands.rear),
            top=constraints.clamp(Zone.TOP, demands.top),
        )

    def _out_of_budget(self, started_ms: int) -> bool:
        return self._elapsed_ms(started_ms) >= self._budget_ms

    def _elapsed_ms(self, started_ms: int) -> int:
        # 単調時計なので戻らないが、注入された時計が壊れていても負の経過にしない。
        return max(0, self._monotonic_ms() - started_ms)

    def _failed(
        self,
        status: OptimizerStatus,
        code: str,
        detail: str,
        started_ms: int,
        evaluations: int,
    ) -> OptimizerOutcome:
        return OptimizerOutcome(
            status=status,
            reason=Reason(code=code, detail=detail[:500]),
            evaluations=evaluations,
            elapsed_ms=self._elapsed_ms(started_ms),
            solution=None,
        )
