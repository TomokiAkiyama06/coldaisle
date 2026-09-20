"""Control Shadow Mode（#90）。適用しなかった提案と、その予測の当たり外れを記録する。

**Fan へ届く経路を持たない。** この package は Reactive Guard・Critical Safety・Hardware Backend
を import せず、``EffectiveZoneDemand`` も PWM も扱わない（AGENTS.md ルール1 / 2、
決定記録 0053 §2.1）。記録の型そのものは ``coldaisle.control.schema`` にあり、
ここにあるのは「作る側」と「あとで突き合わせる側」だけである。
"""

from coldaisle.control.shadow.export import (
    SHADOW_EXPORT_SCHEMA_VERSION,
    ControlTraceRow,
    ShadowExportRow,
    counterfactual_controllers,
    shadow_rows,
    write_shadow_jsonl,
)
from coldaisle.control.shadow.outcome import (
    OutcomeMatch,
    OutcomeObservation,
    ShadowOutcome,
    ShadowOutcomeMatcher,
    ShadowOutcomeUnusableError,
)
from coldaisle.control.shadow.record import ShadowRecorder, shadow_prediction

__all__ = [
    "SHADOW_EXPORT_SCHEMA_VERSION",
    "ControlTraceRow",
    "OutcomeMatch",
    "OutcomeObservation",
    "ShadowExportRow",
    "ShadowOutcome",
    "ShadowOutcomeMatcher",
    "ShadowOutcomeUnusableError",
    "ShadowRecorder",
    "counterfactual_controllers",
    "shadow_prediction",
    "shadow_rows",
    "write_shadow_jsonl",
]
