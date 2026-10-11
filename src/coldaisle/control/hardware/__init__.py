"""Fan Hardware Backend の公開契約。#77

この package は、Critical Safety と DemandComposer を通った ``ComposedDemands`` だけを
PWM / RPM / Airflow Index へ写像する境界である。テストと Replay 用の
:class:`SimulatedFanBackend` と、実機の hwmon へ書く :class:`HwmonFanBackend`
（決定記録 0118 §2.5）を提供する。どちらを組むかは合成の起点（``coldaisle-fand`` の
``--backend``。既定は simulated）が決める。
"""

from coldaisle.control.hardware.hwmon import (
    HandoffRecordWriteError,
    HwmonFanBackend,
    HwmonHeaderNotIdentifiedError,
    HwmonStartupError,
    HwmonUnsupportedDriverError,
)
from coldaisle.control.hardware.simulated import (
    FanHardwareBackend,
    FanHardwareResult,
    HandsBackOnStop,
    SimulatedFanBackend,
    SimulatedFaultPlan,
)
from coldaisle.control.hardware.verified_drivers import VERIFIED_HWMON_DRIVERS

__all__ = [
    "VERIFIED_HWMON_DRIVERS",
    "FanHardwareBackend",
    "FanHardwareResult",
    "HandoffRecordWriteError",
    "HandsBackOnStop",
    "HwmonFanBackend",
    "HwmonHeaderNotIdentifiedError",
    "HwmonStartupError",
    "HwmonUnsupportedDriverError",
    "SimulatedFanBackend",
    "SimulatedFaultPlan",
]
