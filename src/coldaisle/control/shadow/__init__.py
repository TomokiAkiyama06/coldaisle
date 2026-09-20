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
    applied_action_timeline,
    counterfactual_controllers,
    shadow_rows,
    write_shadow_jsonl,
)
from coldaisle.control.shadow.outcome import (
    AppliedActionTimeline,
    ObservationIndex,
    OutcomeMatch,
    OutcomeObservation,
    OutcomeStatus,
    ShadowOutcome,
    ShadowOutcomeMatcher,
    ShadowOutcomeUnusableError,
)
from coldaisle.control.shadow.record import ShadowRecorder, shadow_plan, shadow_prediction

__all__ = [
    "SHADOW_EXPORT_SCHEMA_VERSION",
    "AppliedActionTimeline",
    "ControlTraceRow",
    "ObservationIndex",
    "OutcomeMatch",
    "OutcomeObservation",
    "OutcomeStatus",
    "ShadowExportRow",
    "ShadowOutcome",
    "ShadowOutcomeMatcher",
    "ShadowOutcomeUnusableError",
    "ShadowRecorder",
    "applied_action_timeline",
    "counterfactual_controllers",
    "shadow_plan",
    "shadow_prediction",
    "shadow_rows",
    "write_shadow_jsonl",
]
