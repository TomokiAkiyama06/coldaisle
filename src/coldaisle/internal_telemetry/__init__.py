"""NVML / hwmon Internal Telemetry の読み取り専用実装。#65"""

from coldaisle.internal_telemetry.collector import CollectionCycle, InternalTelemetryCollector
from coldaisle.internal_telemetry.config import (
    CONNECTOR_TEMPERATURE_METRIC,
    ConfirmationEvidence,
    ConfirmationStatus,
    HwmonConfig,
    HwmonMeasurement,
    HwmonSensorConfig,
    InternalTelemetryConfig,
    NvmlConfig,
)
from coldaisle.internal_telemetry.hwmon import HwmonAdapter
from coldaisle.internal_telemetry.models import (
    SOURCE_STATE_PREFIX,
    AdapterResult,
    SourceStatus,
    TelemetryAdapter,
)
from coldaisle.internal_telemetry.nvml import (
    THROTTLE_REASON_BITS,
    ClockEventReasons,
    NvmlAdapter,
    NvmlApi,
    PynvmlApi,
)

__all__ = [
    "CONNECTOR_TEMPERATURE_METRIC",
    "SOURCE_STATE_PREFIX",
    "THROTTLE_REASON_BITS",
    "AdapterResult",
    "ClockEventReasons",
    "CollectionCycle",
    "ConfirmationEvidence",
    "ConfirmationStatus",
    "HwmonAdapter",
    "HwmonConfig",
    "HwmonMeasurement",
    "HwmonSensorConfig",
    "InternalTelemetryCollector",
    "InternalTelemetryConfig",
    "NvmlAdapter",
    "NvmlApi",
    "NvmlConfig",
    "PynvmlApi",
    "SourceStatus",
    "TelemetryAdapter",
]
