"""Learned MPC（#86）。

Supervisor（#88）の戦略と Learned Thermal Model（#84）の予測から、数十秒〜数分先を見た
Fan Demand 列を決定論的に探索する。**出せるのは ``requested_demand`` まで**で、実行するのは
plan の最初の step だけ。Reactive Guard（#80）と Critical Safety（#78）は後段で常に掛かり、
この package からそれらを迂回する経路は作らない（AGENTS.md ルール2 / 決定記録 0028 §2.4）。

timeout・実行不能・モデル読込失敗・低 Confidence / OOD では提案を通さず、#79 Fallback で
運転を続ける（AGENTS.md ルール4）。
"""

from coldaisle.control.mpc.controller import LearnedMpcController, MpcProposal
from coldaisle.control.mpc.cost import CostTerms, MpcCostModel, MpcCostUnusableError, PlanCost
from coldaisle.control.mpc.counterfactual import (
    COUNTERFACTUAL_CAPABILITIES,
    CounterfactualModelIdentity,
    CounterfactualThermalModel,
    MpcModelBinding,
    MpcModelUnusableError,
    PlannedTarget,
    PlannedThermalInput,
    PlanPrediction,
)
from coldaisle.control.mpc.optimizer import LearnedMpcOptimizer, MpcSolution, OptimizerOutcome
from coldaisle.control.mpc.plan import (
    ActionPlan,
    HardConstraintSet,
    InfeasiblePlanError,
    PlanStep,
    ZoneBound,
)

__all__ = [
    "COUNTERFACTUAL_CAPABILITIES",
    "ActionPlan",
    "CostTerms",
    "CounterfactualModelIdentity",
    "CounterfactualThermalModel",
    "HardConstraintSet",
    "InfeasiblePlanError",
    "LearnedMpcController",
    "LearnedMpcOptimizer",
    "MpcCostModel",
    "MpcCostUnusableError",
    "MpcModelBinding",
    "MpcModelUnusableError",
    "MpcProposal",
    "MpcSolution",
    "OptimizerOutcome",
    "PlanCost",
    "PlanPrediction",
    "PlanStep",
    "PlannedTarget",
    "PlannedThermalInput",
    "ZoneBound",
]
