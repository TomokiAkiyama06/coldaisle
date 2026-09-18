"""Fan Hardware Backend の公開契約。#77

この package は、Critical Safety と DemandComposer を通った ``ComposedDemands`` だけを
PWM / RPM / Airflow Index へ写像する境界である。M8 では実機へ到達する実装を置かず、
テストと Replay 用の :class:`SimulatedFanBackend` だけを提供する。
"""

from coldaisle.control.hardware.simulated import (
    FanHardwareBackend,
    FanHardwareResult,
    SimulatedFanBackend,
    SimulatedFaultPlan,
)

__all__ = [
    "FanHardwareBackend",
    "FanHardwareResult",
    "SimulatedFanBackend",
    "SimulatedFaultPlan",
]
