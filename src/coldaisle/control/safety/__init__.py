"""Critical Safety の公開境界。"""

from coldaisle.control.safety.critical import (
    ABSOLUTE_TEMPERATURE_METRICS,
    AIR_TELEMETRY_GROUP,
    CPU_TEMPERATURE_METRIC,
    GPU_TEMPERATURE_METRIC,
    CriticalSafety,
    CriticalSafetyDecision,
    compose_effective_demands,
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
    "CPU_TEMPERATURE_METRIC",
    "GPU_TEMPERATURE_METRIC",
    "CriticalSafety",
    "CriticalSafetyDecision",
    "HandoffRecordError",
    "HandoffResult",
    "HandoffZoneResult",
    "compose_effective_demands",
    "emergency_handoff",
    "invalid_config_decision",
]
