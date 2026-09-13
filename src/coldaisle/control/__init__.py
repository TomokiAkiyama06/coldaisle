"""Fan 制御（Supervisor + Learned MPC + Reactive Guard + Critical Safety）。

構成は決定記録 0027、層間の契約は決定記録 0028。

**LLM はここへ到達できない。** `ai/` は `control/` を import しない（AGENTS.md ルール1）。
Fan Demand を変えられるのは、定義済みの Control Pipeline を通った値だけ（AGENTS.md ルール2）。
"""

from coldaisle.control.schema import (
    BOUND_BY_PRECEDENCE,
    EMERGENCY_FAULTS,
    SCHEMA_VERSION,
    TOP_EMERGENCY_FAULTS,
    ZONE_FAULTS,
    AuthorityStage,
    BoundBy,
    ControllerKind,
    ControllerProposal,
    ControlState,
    ControlTick,
    Demand,
    EffectiveZoneDemand,
    Fault,
    FaultCode,
    GuardZoneOutput,
    HardwareReadback,
    OperatingMode,
    OptimizerStatus,
    PerZone,
    Reason,
    SafetyState,
    SafetyZoneOutput,
    Zone,
    ZoneRecord,
    ZoneRequest,
)

__all__ = [
    "BOUND_BY_PRECEDENCE",
    "EMERGENCY_FAULTS",
    "SCHEMA_VERSION",
    "TOP_EMERGENCY_FAULTS",
    "ZONE_FAULTS",
    "AuthorityStage",
    "BoundBy",
    "ControlState",
    "ControlTick",
    "ControllerKind",
    "ControllerProposal",
    "Demand",
    "EffectiveZoneDemand",
    "Fault",
    "FaultCode",
    "GuardZoneOutput",
    "HardwareReadback",
    "OperatingMode",
    "OptimizerStatus",
    "PerZone",
    "Reason",
    "SafetyState",
    "SafetyZoneOutput",
    "Zone",
    "ZoneRecord",
    "ZoneRequest",
]
