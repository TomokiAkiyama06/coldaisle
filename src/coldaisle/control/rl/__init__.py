"""RL Supervisor の学習基盤（#105 / 決定記録 0058）。

**Fan へ届く経路を持たない。** この package は `control.hardware` / `control.safety` /
`control.reactive` を import せず、`EffectiveZoneDemand` も PWM も扱わない
（AGENTS.md ルール1 / 2、0058 §2.5。試験で走査する）。

**agent が探索するのは Supervisor の戦略であって Fan Demand ではない**（0027 / 0028 §2.3）。
action から demand への写像は Learned MPC（#86）と Controller Gate（#79 / #85）が持つ。

**いま何が学習・評価できるか**（決定記録 0058 §3）。

- 記録済み trajectory からの offline RL: **できる。** ただし記録と同じ action の区間だけ
- Registry 検証済みの learned simulator: **できない。** 反実仮想能力を申告した artifact が
  1つも無い（決定記録 0048 §2.1 / 0052 §2.1）。この経路は決定論的にすべて拒む
- 近似 simulator: **研究用途に限り回せる。** 結果は `promotable=False` になり、
  #91 の gate や #92 の昇格判断の根拠にできない
"""

from coldaisle.control.rl.action import (
    ActionSpace,
    InvalidSupervisorActionError,
    SupervisorAction,
)
from coldaisle.control.rl.config import (
    MAX_EPISODE_STEPS,
    RL_TRAINING_CONFIG_FILENAME,
    RL_TRAINING_CONFIG_VERSION,
    CoverageConfig,
    EpisodeConfig,
    RewardConfig,
    RewardMetrics,
    RewardScales,
    RewardWeights,
    RlTrainingConfig,
    SafetyScreenConfig,
    SimulatorConfig,
    SimulatorResponse,
)
from coldaisle.control.rl.dynamics import (
    AttestedThermalDynamics,
    DynamicsIdentity,
    DynamicsProvenance,
    DynamicsRequest,
    DynamicsStep,
    DynamicsUnusableError,
    EnvironmentDynamics,
    HybridDynamics,
    LoggedFrame,
    LoggedTrajectory,
    LoggedTrajectoryDynamics,
    SimulatedThermalDynamics,
    WorkloadSample,
    WorkloadTrace,
)
from coldaisle.control.rl.environment import (
    BaselineProposer,
    EnvironmentUsageError,
    EpisodeSpec,
    SupervisorTrainingEnvironment,
)
from coldaisle.control.rl.episode import (
    EPISODE_SCHEMA_VERSION,
    EpisodeCoverage,
    EpisodeResult,
    EpisodeSafety,
    PolicyArm,
    PolicyComparison,
    SafetyModel,
    StepRecord,
    TerminationReason,
    TrainingMode,
)
from coldaisle.control.rl.reward import (
    REWARD_SCHEMA_VERSION,
    RewardBreakdown,
    RewardFunction,
    RewardTerms,
    RewardUnusableError,
    SafetyLedgerEntry,
)

__all__ = [
    "EPISODE_SCHEMA_VERSION",
    "MAX_EPISODE_STEPS",
    "REWARD_SCHEMA_VERSION",
    "RL_TRAINING_CONFIG_FILENAME",
    "RL_TRAINING_CONFIG_VERSION",
    "ActionSpace",
    "AttestedThermalDynamics",
    "BaselineProposer",
    "CoverageConfig",
    "DynamicsIdentity",
    "DynamicsProvenance",
    "DynamicsRequest",
    "DynamicsStep",
    "DynamicsUnusableError",
    "EnvironmentDynamics",
    "EnvironmentUsageError",
    "EpisodeConfig",
    "EpisodeCoverage",
    "EpisodeResult",
    "EpisodeSafety",
    "EpisodeSpec",
    "HybridDynamics",
    "InvalidSupervisorActionError",
    "LoggedFrame",
    "LoggedTrajectory",
    "LoggedTrajectoryDynamics",
    "PolicyArm",
    "PolicyComparison",
    "RewardBreakdown",
    "RewardConfig",
    "RewardFunction",
    "RewardMetrics",
    "RewardScales",
    "RewardTerms",
    "RewardUnusableError",
    "RewardWeights",
    "RlTrainingConfig",
    "SafetyLedgerEntry",
    "SafetyModel",
    "SafetyScreenConfig",
    "SimulatedThermalDynamics",
    "SimulatorConfig",
    "SimulatorResponse",
    "StepRecord",
    "SupervisorAction",
    "SupervisorTrainingEnvironment",
    "TerminationReason",
    "TrainingMode",
    "WorkloadSample",
    "WorkloadTrace",
]
