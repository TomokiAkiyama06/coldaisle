"""Critical Safety の公開境界。"""

from coldaisle.control.safety.critical import (
    ABSOLUTE_TEMPERATURE_METRICS,
    AIR_TELEMETRY_GROUP,
    AIR_TEMPERATURE_METRICS,
    CPU_TEMPERATURE_METRIC,
    GPU_TEMPERATURE_METRIC,
    ComposedDemands,
    ControlRuntimeBinding,
    CriticalSafety,
    CriticalSafetyDecision,
    DemandComposer,
    EmergencyControlRuntime,
    create_control_runtime_binding,
    create_emergency_control_runtime,
    invalid_config_decision,
)
from coldaisle.safety_handoff import (
    HandoffRecordError,
    HandoffResult,
    HandoffZoneResult,
    emergency_handoff,
)

__all__ = [
    "ABSOLUTE_TEMPERATURE_METRICS",
    "AIR_TELEMETRY_GROUP",
    "AIR_TEMPERATURE_METRICS",
    "CPU_TEMPERATURE_METRIC",
    "GPU_TEMPERATURE_METRIC",
    "ComposedDemands",
    "ControlRuntimeBinding",
    "CriticalSafety",
    "CriticalSafetyDecision",
    "DemandComposer",
    "EmergencyControlRuntime",
    "HandoffRecordError",
    "HandoffResult",
    "HandoffZoneResult",
    "create_control_runtime_binding",
    "create_emergency_control_runtime",
    "emergency_handoff",
    "invalid_config_decision",
]
