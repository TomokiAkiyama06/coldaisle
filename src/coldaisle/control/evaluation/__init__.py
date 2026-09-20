"""Offline Evaluation（#91 / 決定記録 0054）。Controller 構成を同じ条件で比べる。

**Fan へ届く経路を持たない。** この package は Reactive Guard・Critical Safety・
Hardware Backend を import せず、`EffectiveZoneDemand` も PWM も作らない
（AGENTS.md ルール1 / 2、0054 §2.7。試験で走査する）。

**時計を持たない。** 時刻はすべて decision trace と観測から来る。同じ入力からは同じ報告が出る。
"""

from coldaisle.control.evaluation.config import (
    EVALUATION_CONFIG_FILENAME,
    EVALUATION_CONFIG_VERSION,
    UNKNOWN_GROUP,
    EvaluationConfig,
)
from coldaisle.control.evaluation.evaluator import (
    EvaluationContext,
    EvaluationInputError,
    EvaluationRun,
    controller_kinds,
    evaluate,
)
from coldaisle.control.evaluation.gate import evaluate_gates
from coldaisle.control.evaluation.model import (
    EVALUATION_REPORT_SCHEMA_VERSION,
    AppliedArm,
    AppliedArmReport,
    CounterfactualArm,
    CounterfactualArmReport,
    CoverageReport,
    EvaluationReport,
    GateOutcome,
    GateStage,
    GroupKind,
    PredictionReport,
    SegmentRole,
    WorstCaseKind,
)
from coldaisle.control.evaluation.stats import MetricSummary, SeriesShape

__all__ = [
    "EVALUATION_CONFIG_FILENAME",
    "EVALUATION_CONFIG_VERSION",
    "EVALUATION_REPORT_SCHEMA_VERSION",
    "UNKNOWN_GROUP",
    "AppliedArm",
    "AppliedArmReport",
    "CounterfactualArm",
    "CounterfactualArmReport",
    "CoverageReport",
    "EvaluationConfig",
    "EvaluationContext",
    "EvaluationInputError",
    "EvaluationReport",
    "EvaluationRun",
    "GateOutcome",
    "GateStage",
    "GroupKind",
    "MetricSummary",
    "PredictionReport",
    "SegmentRole",
    "SeriesShape",
    "WorstCaseKind",
    "controller_kinds",
    "evaluate",
    "evaluate_gates",
]
