"""fand の Shadow の記録の 2 条件の再検査の結果（0133 §2.5・0162 §2.2）。

規則そのものは ``coldaisle.control.hint_monotonicity.monotonicity_violation``（認証ランナー・
worker の Shadow solver と同じ1つの関数）で、ここはその結果を ``record_json`` に載せる閉じた語彙
``demand`` / ``cpu`` / ``gpu``（0162 §2.2「`record_json` に載せる fand の再検査の結果」）へ写す
だけ。予測そのものは worker の出力で、fand は予測を作り直さない（モデルを評価しない。0133 §2.5）。

**B1 は shadow だけで、適用しない**（0162 §2.1）。``coldaisle.control.safety`` / ``reactive`` /
``fallback`` / ``hardware`` はこの module を import しない（0133 §2.7。試験で固定する）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from coldaisle.control.hint_monotonicity import monotonicity_violation
from coldaisle.control.mpc.counterfactual import PlannedTarget
from coldaisle.control.schema import Demand, PerZone


class MonotonicityCheck(StrEnum):
    """2 条件の再検査で最初に外れた項目（決定記録 0162 §2.2 の閉じた語彙）。"""

    DEMAND = "demand"
    """系統のどれかで ``demand_hint < demand_base - eps``。"""
    CPU = "cpu"
    """ある offset で CPU 温度の予測が ``hint > base + eps_c``（または予測に metric が無い）。"""
    GPU = "gpu"
    """ある offset で GPU 温度の予測が ``hint > base + eps_c``（または予測に metric が無い）。"""


@dataclass(frozen=True, slots=True)
class MonotonicityJudgement:
    """2 条件の再検査の結果。合格なら ``failed`` は None。"""

    failed: MonotonicityCheck | None

    @property
    def passed(self) -> bool:
        """2 条件とも通ったか。"""
        return self.failed is None


_VIOLATIONS: dict[str, MonotonicityCheck] = {
    "demand_front": MonotonicityCheck.DEMAND,
    "demand_rear": MonotonicityCheck.DEMAND,
    "demand_top": MonotonicityCheck.DEMAND,
    "prediction_cpu_missing": MonotonicityCheck.CPU,
    "temperature_cpu": MonotonicityCheck.CPU,
    "prediction_gpu_missing": MonotonicityCheck.GPU,
    "temperature_gpu": MonotonicityCheck.GPU,
}


def recheck_monotonicity(
    *,
    base_requested: PerZone[Demand],
    base_targets: Sequence[PlannedTarget],
    hint_requested: PerZone[Demand],
    hint_targets: Sequence[PlannedTarget],
    cpu_metric: str,
    gpu_metric: str,
    eps: float,
    eps_c: float,
) -> MonotonicityJudgement:
    """0066 §2.1 (d) の 2 条件を、fand が自分の設定から導いた ``eps`` / ``eps_c`` で比べる。

    ``eps`` / ``eps_c`` は呼び出し側が**自分の設定から**導いたゲートの仕様の値（証明書の値を
    渡さない。0152 §2.3）。両 plan の offset の列が違う（比べられない）ときは ``ValueError``
    （呼び出し側が記録の検証で先に ``base_mismatch`` として拒む）。
    """
    violation = monotonicity_violation(
        base_requested=base_requested,
        base_targets=base_targets,
        hint_requested=hint_requested,
        hint_targets=hint_targets,
        cpu_temperature_metric=cpu_metric,
        gpu_temperature_metric=gpu_metric,
        eps=eps,
        eps_c=eps_c,
    )
    if violation is None:
        return MonotonicityJudgement(failed=None)
    check = _VIOLATIONS.get(violation)
    if check is None:
        raise ValueError(f"比べられない 2 つの plan: {violation}")
    return MonotonicityJudgement(failed=check)
