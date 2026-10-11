"""Fan Hardware Backend の契約と、実機へ書き込まない simulated backend。#77

ここで扱う入力は Critical Safety が合成した ``ComposedDemands`` だけである。
PWM の raw 値は backend の中でしか作らず、simulated backend は sysfs、
subprocess、ネットワークを一切使わない。実機 backend（``hwmon.HwmonFanBackend``。
決定記録 0118 §2.5）も同じ Protocol を実装し、command の受理・takeover の確認・写像の
規則を ``_common`` で共有する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from coldaisle.control.config import FanHardwareConfig
from coldaisle.control.hardware._common import CommandSession, map_demand
from coldaisle.control.safety.critical import ComposedDemands, ControlRuntimeBinding
from coldaisle.control.schema import (
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

    ``applied_demand`` は profile の制約（``minimum_stable_demand`` への引き上げと、未起動
    zone の ``startup_demand``）を掛けた後の、PWM へ写す直前の demand（決定記録 0073 §2.5 (a)）。
    **``write_ok`` と ``readback_ok`` の両方が真のときだけ値を持つ。** 書けていない・
    確かめられていない指令を「適用した demand」として返さない。PWM の raw 値から逆算しない。
    実機 backend も同じ規則で返す。
    """

    target_rpm: int
    airflow_index: float
    readback: HardwareReadback
    applied_demand: float | None
    fault: Fault | None = None

    def __post_init__(self) -> None:
        confirmed = self.readback.write_ok and self.readback.readback_ok
        if confirmed != (self.applied_demand is not None):
            raise ValueError("applied_demand は write と readback が確認できたときだけ持つ")
        if self.applied_demand is not None and not 0.0 <= self.applied_demand <= 1.0:
            raise ValueError("applied_demand は 0.0..1.0 にする")


class FanHardwareBackend(Protocol):
    """実機・simulated backend が共有する、唯一の書込み境界。"""

    def apply(self, demands: ComposedDemands) -> PerZone[FanHardwareResult]:
        """effective demand を3 zone 独立に反映し、readback と fault を返す。"""


@runtime_checkable
class HandsBackOnStop(Protocol):
    """正常停止で制御を返す backend（実機 backend。決定記録 0080 §2.8）。

    書込み境界（:class:`FanHardwareBackend`）とは分ける。制御を取らない simulated backend や
    試験の偽物に、返す手順を持たせないためである。合成の起点は **loop が止まり、正常に
    終えるときだけ** 呼ぶ（例外・終了コード 7 の経路では呼ばず、引き継ぎ記録を残して
    ``ExecStopPost`` の Max に任せる）。
    """

    def hand_back(self) -> bool:
        """元の制御へ返す。引き継ぎ記録を消せたときだけ真。"""


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
    未測定の低 PWM を simulated 経路でも表現しない。command の受理・takeover の確認・
    写像の規則は実機 backend と共有する（``_common``）。
    """

    config: FanHardwareConfig
    runtime_binding: ControlRuntimeBinding
    fault_plan: SimulatedFaultPlan = field(default_factory=SimulatedFaultPlan)
    _running: set[Zone] = field(default_factory=set, init=False, repr=False)
    _session: CommandSession = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._session = CommandSession(self.config, self.runtime_binding)

    def apply(self, demands: ComposedDemands) -> PerZone[FanHardwareResult]:
        """3 zone の effective demand を個別に map する。実機 I/O は行わない。"""
        zones = self._session.accept(demands)
        results = PerZone(
            front=self._apply_zone(Zone.FRONT, zones.front.effective),
            rear=self._apply_zone(Zone.REAR, zones.rear.effective),
            top=self._apply_zone(Zone.TOP, zones.top.effective),
        )
        self._session.observe_takeover(zones, confirmed_writes(results))
        return results

    def _apply_zone(self, zone: Zone, demand: float) -> FanHardwareResult:
        profile = self.config.zones.get(zone).profile
        mapped = map_demand(profile, demand, running=zone in self._running)
        mapped_demand = mapped.demand
        target_rpm = mapped.target_rpm
        airflow_index = mapped.airflow_index
        pwm_raw = mapped.pwm_raw

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
                applied_demand=None,
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
                applied_demand=None,
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
                # 書き込みと読み戻しは成功している。回っていないことは fault が表し、
                # 風量の記録から外すのは呼び出し側（決定記録 0073 §2.5 (a)）。
                applied_demand=mapped_demand,
                fault=Fault(code=FaultCode.TACH_STALL, zone=zone),
            )

        # write/readback/tach の全てが成功して初めて、kick 済みとして扱う。失敗した
        # write を起動済みにすると、復旧時に必要な startup demand を省略してしまう。
        self._running.add(zone)
        return FanHardwareResult(
            target_rpm=target_rpm,
            airflow_index=airflow_index,
            readback=HardwareReadback(
                pwm_raw=pwm_raw,
                rpm=target_rpm,
                write_ok=True,
                readback_ok=True,
            ),
            applied_demand=mapped_demand,
        )


def confirmed_writes(results: PerZone[FanHardwareResult]) -> PerZone[bool]:
    """zone ごとの「書き込みと読み戻しの両方が成功したか」（takeover の確認に数える条件）。"""
    return PerZone(
        front=results.front.readback.write_ok and results.front.readback.readback_ok,
        rear=results.rear.readback.write_ok and results.rear.readback.readback_ok,
        top=results.top.readback.write_ok and results.top.readback.readback_ok,
    )


def _different_pwm(expected: int) -> int:
    """readback mismatch 用に、範囲内で必ず異なる raw PWM を返す。"""
    return expected - 1 if expected > 0 else 1
