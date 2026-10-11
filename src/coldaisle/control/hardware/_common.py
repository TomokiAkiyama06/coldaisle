"""simulated / 実機の Fan Hardware Backend が共有する規則。#77 / 決定記録 0118 §2.5

- runtime binding の取得と、command の受理（別 runtime・takeover 前の通常 command・
  古い tick を拒む）
- takeover の Max を確かめてから Safety へ伝えること
- profile の写像（``stable_demand()``・未起動 zone の ``startup_demand``・
  RPM / Airflow Index の補間）

実機 backend が simulated と違う規則で command を受けたり demand を写像したりしないよう、
ここに1か所だけ置く。I/O は持たない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from coldaisle.control.config import FanHardwareConfig, FanProfile
from coldaisle.control.safety.critical import (
    _RUNTIME_BINDING_AUTHORITY,
    ComposedDemands,
    ControlRuntimeBinding,
)
from coldaisle.control.schema import HWMON_PWM_MAX, EffectiveZoneDemand, PerZone, Zone


@dataclass(slots=True)
class CommandSession:
    """1つの backend が持つ、command の受理と takeover の確認。

    ``runtime_binding`` は作った時点で1回だけ取得する（同じ binding に backend は1つ）。
    """

    config: FanHardwareConfig
    runtime_binding: ControlRuntimeBinding
    _last_tick_id: int | None = field(default=None, init=False, repr=False)
    _last_monotonic_ms: int | None = field(default=None, init=False, repr=False)
    _takeover_max_written: set[Zone] = field(default_factory=set, init=False, repr=False)
    _takeover_acknowledged: bool = field(default=False, init=False, repr=False)
    _runtime_config_payload: str = field(init=False, repr=False)
    _runtime_lineage: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime_config_payload, self._runtime_lineage = self.runtime_binding._claim_backend(
            self.config.model_dump_json(),
            authority=_RUNTIME_BINDING_AUTHORITY,
        )

    @property
    def takeover_acknowledged(self) -> bool:
        """全 zone の takeover の Max を書けたと確かめたか。"""
        return self._takeover_acknowledged

    def accept(self, demands: ComposedDemands) -> PerZone[EffectiveZoneDemand]:
        """command を検査して consume し、zone ごとの effective demand を返す。

        検査に通らない command は consume する前に ``ValueError`` で拒む。
        """
        zones, tick_id, monotonic_ms, runtime_payload, runtime_lineage = demands._hardware_binding()
        if (
            runtime_payload != self._runtime_config_payload
            or runtime_lineage is not self._runtime_lineage
        ):
            raise ValueError("active control runtime が発行していない command は適用できない")
        # 0028 §2.7: takeover 後は STARTUP（設定不正時は EMERGENCY）の全 zone Max を書く。
        # 全 zone の Max の書き込みを確認するまでは、それ以外の command を consume する前に
        # 拒否する。STARTUP を捨てて NORMAL を渡すと startup kick だけで制御を取るため。
        if not self._takeover_acknowledged and not all(zones.get(zone).forced_max for zone in Zone):
            raise ValueError(
                "Fan Hardware Backend は takeover を確認するまで全 zone forced Max"
                "（STARTUP / EMERGENCY）の command だけを受理する"
            )
        if self._last_tick_id is not None and tick_id <= self._last_tick_id:
            raise ValueError("Fan Hardware Backend に古い tick の command を適用できない")
        if self._last_monotonic_ms is not None and monotonic_ms <= self._last_monotonic_ms:
            raise ValueError("Fan Hardware Backend の command 時刻は前進させる")
        demands._consume_for_hardware()
        self._last_tick_id = tick_id
        self._last_monotonic_ms = monotonic_ms
        return zones

    def observe_takeover(
        self,
        zones: PerZone[EffectiveZoneDemand],
        confirmed: PerZone[bool],
    ) -> None:
        """takeover の Max を書けた zone を数え、全 zone が揃ったら Safety へ伝える。

        ``confirmed`` は zone ごとの「書き込みと読み戻しが成功したか」。
        """
        if self._takeover_acknowledged:
            return
        # Safety はこの確認の後から STARTUP の settle と tach 応答を数える。write や
        # readback に失敗した zone は Max が効いている保証が無いため確認に数えず、
        # 失敗した zone は確認済みから外す。失敗は fault として Safety へ渡り、Top は
        # 即 EMERGENCY、Front / Rear は連続回数で EMERGENCY へ昇格する（0028 §2.7）。
        for zone in Zone:
            if zones.get(zone).forced_max and confirmed.get(zone):
                self._takeover_max_written.add(zone)
            else:
                self._takeover_max_written.discard(zone)
        if self._takeover_max_written == set(Zone):
            self._takeover_acknowledged = True
            self.runtime_binding._acknowledge_takeover(authority=_RUNTIME_BINDING_AUTHORITY)


@dataclass(frozen=True, slots=True)
class MappedDemand:
    """1 zone の demand を profile で写した結果（PWM へ書く直前の値）。"""

    demand: float
    """``applied_demand`` の候補。``stable_demand()`` と startup の引き上げを掛けた後の値。"""
    target_rpm: int
    airflow_index: float
    pwm_raw: int


def map_demand(profile: FanProfile, effective: float, *, running: bool) -> MappedDemand:
    """effective demand を profile の制約の下で PWM の直前まで写す。

    停止を許す profile は未測定の危険な低 PWM へ落ちうるため表さない。初回（未起動の zone）
    だけ startup demand を使い、以後の最低値も profile の minimum stable demand に固定する
    （0028 §2.4 の kick の責務）。起動済みへの遷移は呼び出し側が書き込みの成功後にだけ行う。
    """
    stable_demand = profile.stable_demand(effective)
    demand = stable_demand if running else max(stable_demand, profile.startup_demand)
    return MappedDemand(
        demand=demand,
        target_rpm=interpolate_rpm(profile, demand),
        airflow_index=interpolate_airflow_index(profile, demand),
        pwm_raw=demand_to_raw_pwm(demand),
    )


def demand_to_raw_pwm(demand: float) -> int:
    """0.0..1.0 の profile demand を hwmon ABI の raw PWM へ正規化する。"""
    return round(demand * HWMON_PWM_MAX)


def interpolate_rpm(profile: FanProfile, demand: float) -> int:
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


def interpolate_airflow_index(profile: FanProfile, demand: float) -> float:
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
