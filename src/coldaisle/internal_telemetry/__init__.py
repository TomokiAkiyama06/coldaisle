"""Internal Telemetry（NVML / hwmon / /proc/stat / CPU Package 電力）の読み取り専用実装。

#65 / #278。``sys.gpu_state`` の識別（NVML の PID と ``/proc/<pid>/cgroup``）は #63
"""

from coldaisle.internal_telemetry.collector import CollectionCycle, InternalTelemetryCollector
from coldaisle.internal_telemetry.config import (
    CONNECTOR_TEMPERATURE_METRIC,
    ConfirmationEvidence,
    ConfirmationStatus,
    CpuPowerConfig,
    HwmonConfig,
    HwmonMeasurement,
    HwmonSensorConfig,
    InternalTelemetryConfig,
    NvmlConfig,
    ProcStatConfig,
)
from coldaisle.internal_telemetry.cpu_power import CPU_POWER_SCHEMA_VERSION, CpuPowerAdapter
from coldaisle.internal_telemetry.gpu_state import (
    GPU_STATE_KEY,
    GpuState,
    GpuStateClassifier,
    GpuStateConfig,
    ProcCgroupReader,
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
    "CPU_POWER_SCHEMA_VERSION",
    "CPU_UTILIZATION_METRIC",
    "GPU_STATE_KEY",
    "SOURCE_STATE_PREFIX",
    "TELEMETRY_KIND_KEY",
    "THROTTLE_REASON_BITS",
    "AdapterResult",
    "ClockEventReasons",
    "CollectionCycle",
    "ConfirmationEvidence",
    "ConfirmationStatus",
    "CpuPowerAdapter",
    "CpuPowerConfig",
    "CpuTimes",
    "GpuState",
    "GpuStateClassifier",
    "GpuStateConfig",
    "HwmonAdapter",
    "HwmonConfig",
    "HwmonMeasurement",
    "HwmonSensorConfig",
    "InternalTelemetryCollector",
    "InternalTelemetryConfig",
    "NvmlAdapter",
    "NvmlApi",
    "NvmlConfig",
    "ProcCgroupReader",
    "ProcStatAdapter",
    "ProcStatConfig",
    "PynvmlApi",
    "SourceStatus",
    "TelemetryAdapter",
    "TelemetrySourceKind",
    "parse_cpu_times",
]
