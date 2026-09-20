"""Thermal Model の drift 検知と再学習の条件（#93 / 決定記録 0055）。

**runtime の判定はここに無い。** 1 tick の residual drift は `ConfidenceAssessor` の
構成要素として 0050 が既に持っており、confidence / authority を動かせるのはそちらだけである。
この package が出すのは**運用期間の報告と再学習の推奨**だけで、`Demand` も PWM も
authority も作らない（0055 §2.1）。

**時計を持たない。** 時刻はすべて証拠（照合に使った観測、照合期限、action 時刻）から来る。
同じ入力からは同じ報告が出る。
"""

from coldaisle.control.drift.config import (
    DRIFT_CONFIG_FILENAME,
    DRIFT_CONFIG_VERSION,
    DriftConfig,
    InputDriftGate,
    ResidualDriftGate,
)
from coldaisle.control.drift.detector import (
    DriftConfigError,
    DriftDetector,
    DriftEvidence,
    DriftInputError,
)
from coldaisle.control.drift.model import (
    DRIFT_REPORT_SCHEMA_VERSION,
    ChangeKind,
    DeclaredChange,
    DriftCoverage,
    DriftProvenance,
    DriftReasonCount,
    DriftReport,
    DriftSignalKind,
    DriftSourceCount,
    DriftTarget,
    DriftVerdict,
    InputDriftSignal,
    MetricResidual,
    ResidualDriftSignal,
    ResidualTrendBucket,
    RetrainingRecommendation,
    RetrainingTrigger,
    combine_verdicts,
)

__all__ = [
    "DRIFT_CONFIG_FILENAME",
    "DRIFT_CONFIG_VERSION",
    "DRIFT_REPORT_SCHEMA_VERSION",
    "ChangeKind",
    "DeclaredChange",
    "DriftConfig",
    "DriftConfigError",
    "DriftCoverage",
    "DriftDetector",
    "DriftEvidence",
    "DriftInputError",
    "DriftProvenance",
    "DriftReasonCount",
    "DriftReport",
    "DriftSignalKind",
    "DriftSourceCount",
    "DriftTarget",
    "DriftVerdict",
    "InputDriftGate",
    "InputDriftSignal",
    "MetricResidual",
    "ResidualDriftGate",
    "ResidualDriftSignal",
    "ResidualTrendBucket",
    "RetrainingRecommendation",
    "RetrainingTrigger",
    "combine_verdicts",
]
