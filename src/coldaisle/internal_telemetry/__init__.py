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
    ProcStatConfig,
)
from coldaisle.internal_telemetry.hwmon import HwmonAdapter
from coldaisle.internal_telemetry.models import (
    SOURCE_STATE_PREFIX,
    TELEMETRY_KIND_KEY,
    AdapterResult,
    SourceStatus,
    TelemetryAdapter,
    TelemetrySourceKind,
)
from coldaisle.internal_telemetry.nvml import (
    THROTTLE_REASON_BITS,
    ClockEventReasons,
    NvmlAdapter,
    NvmlApi,
    PynvmlApi,
)
from coldaisle.internal_telemetry.proc_stat import (
    CPU_UTILIZATION_METRIC,
    CpuTimes,
    ProcStatAdapter,
    parse_cpu_times,
)

__all__ = [
    "CONNECTOR_TEMPERATURE_METRIC",
    "CPU_UTILIZATION_METRIC",
    "SOURCE_STATE_PREFIX",
    "TELEMETRY_KIND_KEY",
    "THROTTLE_REASON_BITS",
    "AdapterResult",
    "ClockEventReasons",
    "CollectionCycle",
    "ConfirmationEvidence",
    "ConfirmationStatus",
    "CpuTimes",
    "HwmonAdapter",
    "HwmonConfig",
    "HwmonMeasurement",
    "HwmonSensorConfig",
    "InternalTelemetryCollector",
    "InternalTelemetryConfig",
    "NvmlAdapter",
    "NvmlApi",
    "NvmlConfig",
    "ProcStatAdapter",
    "ProcStatConfig",
    "PynvmlApi",
    "SourceStatus",
    "TelemetryAdapter",
    "TelemetrySourceKind",
    "parse_cpu_times",
]
