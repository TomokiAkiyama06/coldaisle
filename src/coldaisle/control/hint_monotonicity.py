"""Workload Hint の単調性の 2 条件（決定記録 0066 §2.1 (b) / (d)・0133 §2.5・0152 §2.3）。

**1つの純粋な関数** :func:`monotonicity_violation` を、認証ランナー（格子の点）・Learned MPC
worker の Shadow solver（実行時の二重解法）・fand の再検査（0162 §2.5 の PR-C）が同じように呼ぶ。
規則を2か所目に書かない。

1. 各系統で ``demand_hint >= demand_base - eps``
2. 両 plan の予測の**対応するすべての offset** で、CPU / GPU を要素ごとに ``hint <= base + eps_c``

``eps`` / ``eps_c`` は呼び出し側が**自分の設定から**導いたゲートの仕様の値（証明書の値を渡さない。
0152 §2.3）。出力は破れた条件の名前（閉じた語彙。値は返さない）か None。

**制御の値を1つも作らない。** ``requested_demand`` にも Reactive Guard / Critical Safety にも
届かない（ルール 2・3）。``coldaisle.control.safety`` / ``reactive`` / ``fallback`` /
``hardware`` を import しない（0133 §2.7）。
"""

from __future__ import annotations

from collections.abc import Sequence

from coldaisle.control.mpc.counterfactual import PlannedTarget
from coldaisle.control.schema import Demand, PerZone

_ZONES = ("front", "rear", "top")


def monotonicity_violation(
    *,
    base_requested: PerZone[Demand],
    base_targets: Sequence[PlannedTarget],
    hint_requested: PerZone[Demand],
    hint_targets: Sequence[PlannedTarget],
    cpu_temperature_metric: str,
    gpu_temperature_metric: str,
    eps: float,
    eps_c: float,
) -> str | None:
    """0066 §2.1 (b) の 2 条件。破れた条件の名前か、満たせば None。

    名前は ``demand_<zone>`` / ``prediction_offsets_differ`` / ``prediction_<cpu|gpu>_missing`` /
    ``temperature_<cpu|gpu>``。条件は番号の順に調べ、最初に破れたものだけを返す。
    """
    for zone in _ZONES:
        if getattr(hint_requested, zone) < getattr(base_requested, zone) - eps:
            return f"demand_{zone}"
    if [target.offset_ms for target in base_targets] != [
        target.offset_ms for target in hint_targets
    ]:
        return "prediction_offsets_differ"
    for base_target, hint_target in zip(base_targets, hint_targets, strict=True):
        for name, metric in (("cpu", cpu_temperature_metric), ("gpu", gpu_temperature_metric)):
            base_value = base_target.values.get(metric)
            hint_value = hint_target.values.get(metric)
            if base_value is None or hint_value is None:
                return f"prediction_{name}_missing"
            if hint_value > base_value + eps_c:
                return f"temperature_{name}"
    return None
