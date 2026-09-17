"""実機へ書き込まない Fan Hardware Backend。#77

ここで扱う入力は Critical Safety が合成した ``EffectiveZoneDemand`` だけである。
PWM の raw 値はこのモジュール内でしか作らず、simulated backend は sysfs、
subprocess、ネットワークを一切使わない。実機 backend は #57 の OS 権限・
handoff 条件と #75 の測定結果が揃ってから、この Protocol を実装する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Protocol

from coldaisle.control.config import FanHardwareConfig, FanProfile
from coldaisle.control.schema import (
    HWMON_PWM_MAX,
    EffectiveZoneDemand,
    Fault,
    FaultCode,
    HardwareReadback,
    PerZone,
    Zone,
)


@dataclass(frozen=True, slots=True)
class FanHardwareResult:
    """1 zone の写像と simulated readback。

    ``fault`` は Critical Safety へそのまま渡せる ``Fault`` であり、この層が
    safety state を決めることはない。
    """

    target_rpm: int
    airflow_index: float
    readback: HardwareReadback
    fault: Fault | None = None


class FanHardwareBackend(Protocol):
    """実機・simulated backend が共有する、唯一の書込み境界。"""

    def apply(self, demands: PerZone[EffectiveZoneDemand]) -> PerZone[FanHardwareResult]:
        """effective demand を3 zone 独立に反映し、readback と fault を返す。"""


@dataclass(frozen=True, slots=True)
class SimulatedFaultPlan:
    """実機なしで失敗経路を再現するための fault injection。

    これはテスト用の入力であり、実機の header 名や write path を受け取らない。
    """

    write_failure: frozenset[Zone] = frozenset()
    readback_mismatch: frozenset[Zone] = frozenset()
    tach_stall: frozenset[Zone] = frozenset()


@dataclass(slots=True)
class SimulatedFanBackend:
    """profile に従って demand を写像する、メモリ内だけの backend。

    ``FanHardwareConfig`` が検証済みなので zone / header の対応は固定であり、
    呼び出し側が任意の ``hwmonX`` や属性を渡す入口はない。初回は startup
    demand を使い、その後も minimum stable demand 未満へは下げない。これにより
    未測定の低 PWM を simulated 経路でも表現しない。
    """

    config: FanHardwareConfig
    fault_plan: SimulatedFaultPlan = field(default_factory=SimulatedFaultPlan)
    _running: set[Zone] = field(default_factory=set, init=False, repr=False)

    def apply(self, demands: PerZone[EffectiveZoneDemand]) -> PerZone[FanHardwareResult]:
        """3 zone の effective demand を個別に map する。実機 I/O は行わない。"""
        return PerZone(
            front=self._apply_zone(Zone.FRONT, demands.front),
            rear=self._apply_zone(Zone.REAR, demands.rear),
            top=self._apply_zone(Zone.TOP, demands.top),
        )

    def _apply_zone(self, zone: Zone, demand: EffectiveZoneDemand) -> FanHardwareResult:
        profile = self.config.zones.get(zone).profile
        mapped_demand = self._safe_demand(zone, demand.effective, profile)
        target_rpm = _interpolate_rpm(profile, mapped_demand)
        airflow_index = _interpolate_airflow_index(profile, mapped_demand)
        pwm_raw = _demand_to_raw_pwm(mapped_demand)

        if zone in self.fault_plan.write_failure:
            return FanHardwareResult(
                target_rpm=target_rpm,
                airflow_index=airflow_index,
                readback=HardwareReadback(
                    pwm_raw=pwm_raw,
                    rpm=None,
                    write_ok=False,
                    readback_ok=False,
                ),
                fault=Fault(code=FaultCode.WRITE_FAILURE, zone=zone),
            )

        if zone in self.fault_plan.readback_mismatch:
            return FanHardwareResult(
                target_rpm=target_rpm,
                airflow_index=airflow_index,
                readback=HardwareReadback(
                    pwm_raw=_different_pwm(pwm_raw),
                    rpm=target_rpm,
                    write_ok=True,
                    readback_ok=False,
                ),
                fault=Fault(code=FaultCode.READBACK_MISMATCH, zone=zone),
            )

        if zone in self.fault_plan.tach_stall:
            return FanHardwareResult(
                target_rpm=target_rpm,
                airflow_index=airflow_index,
                readback=HardwareReadback(
                    pwm_raw=pwm_raw,
                    rpm=0,
                    write_ok=True,
                    readback_ok=True,
                ),
                fault=Fault(code=FaultCode.TACH_STALL, zone=zone),
            )

        return FanHardwareResult(
            target_rpm=target_rpm,
            airflow_index=airflow_index,
            readback=HardwareReadback(
                pwm_raw=pwm_raw,
                rpm=target_rpm,
                write_ok=True,
                readback_ok=True,
            ),
        )

    def _safe_demand(self, zone: Zone, effective: float, profile: FanProfile) -> float:
        # 停止を許す profile は未測定の危険な低 PWM へ落ちうるため、この backend
        # では表さない。初回だけ startup demand を使い、以後の最低値も profile の
        # minimum stable demand に固定する（0028 §2.4 の kick の責務）。
        stable_demand = max(effective, profile.minimum_stable_demand)
        if zone not in self._running:
            self._running.add(zone)
            return max(stable_demand, profile.startup_demand)
        return stable_demand


def _demand_to_raw_pwm(demand: float) -> int:
    """0.0..1.0 の profile demand を hwmon ABI の raw PWM へ正規化する。"""
    return round(demand * HWMON_PWM_MAX)


def _different_pwm(expected: int) -> int:
    """readback mismatch 用に、範囲内で必ず異なる raw PWM を返す。"""
    return expected - 1 if expected > 0 else 1


def _interpolate_rpm(profile: FanProfile, demand: float) -> int:
    """#75 の demand→RPM 表を線形補間する。表の外側は端点で飽和する。"""
    points = profile.pwm_to_rpm
    if demand <= points[0].demand:
        return points[0].rpm
    if demand >= points[-1].demand:
        return points[-1].rpm
    for lower, upper in pairwise(points):
        if demand <= upper.demand:
            return round(
                lower.rpm
                + (upper.rpm - lower.rpm) * (demand - lower.demand) / (upper.demand - lower.demand)
            )
    raise AssertionError("検証済み profile の補間範囲に到達できなかった")


def _interpolate_airflow_index(profile: FanProfile, demand: float) -> float:
    """profile の対応点から zone 内の Airflow Index を線形補間する。"""
    points = profile.pwm_to_rpm
    values = profile.airflow_index
    if demand <= points[0].demand:
        return values[0]
    if demand >= points[-1].demand:
        return values[-1]
    for index, (lower, upper) in enumerate(pairwise(points)):
        if demand <= upper.demand:
            return values[index] + (values[index + 1] - values[index]) * (demand - lower.demand) / (
                upper.demand - lower.demand
            )
    raise AssertionError("検証済み profile の補間範囲に到達できなかった")
