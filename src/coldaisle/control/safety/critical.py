"""ML 非依存の Critical Safety 判定と effective demand の合成。#78

このモジュールは検証済み :class:`SafetyConfig` だけから安全制約を作る。
モデル、optimizer、Supervisor には依存せず、それらの出力は requested
demand としてしか受け取らない。期限・stall・hold は snapshot の単調時計で
判定し、壁時計は使わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from math import isfinite

from pydantic import BaseModel, ConfigDict

from coldaisle.control.config import SafetyConfig
from coldaisle.control.schema import (
    BOUND_BY_PRECEDENCE,
    EMERGENCY_FAULTS,
    TOP_EMERGENCY_FAULTS,
    BoundBy,
    EffectiveZoneDemand,
    Fault,
    FaultCode,
    GuardZoneOutput,
    OperatingMode,
    PerZone,
    Reason,
    SafetyState,
    SafetyZoneOutput,
    Zone,
    ZoneRequest,
)
from coldaisle.control.state import ControlStateSnapshot
from coldaisle.metrics import MetricCatalog
from coldaisle.store.models import validate_metric

CPU_TEMPERATURE_METRIC = "cpu.package"
GPU_TEMPERATURE_METRIC = "gpu.0.core"
AIR_TELEMETRY_GROUP = "air_telemetry"

# これらは値ではなく、requirements と決定記録 0029 の canonical metric contract。
# Power / utilization / humidity / VRAM 容量に温度上限を誤適用しないため明示する。
ABSOLUTE_TEMPERATURE_METRICS: frozenset[str] = frozenset(
    {
        CPU_TEMPERATURE_METRIC,
        GPU_TEMPERATURE_METRIC,
        "gpu.0.hotspot",
        "gpu.0.mem",
        "cpu.vrm",
        "board.chipset",
        "air.front_intake",
        "air.gpu_intake",
        "air.gpu_exhaust",
        "air.top_exhaust",
        "air.rear_exhaust",
        "air.room",
    }
)

_WRITE_FAULTS = frozenset(
    {FaultCode.WRITE_FAILURE, FaultCode.READBACK_MISMATCH, FaultCode.ENABLE_REVERTED}
)
_FAN_FAULTS = _WRITE_FAULTS | {FaultCode.TACH_STALL}


class CriticalSafetyDecision(BaseModel):
    """1 tick の Safety 最終裁定。decision trace へそのまま記録できる。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    state: SafetyState
    zones: PerZone[SafetyZoneOutput]
    faults: tuple[Fault, ...] = ()
    disabled_inputs: tuple[Reason, ...] = ()
    config_validated: bool = True
    config_is_provisional: bool


@dataclass(slots=True)
class _LatchedFault:
    fault: Fault
    emergency: bool
    clear_since_ms: int | None = None


class CriticalSafety:
    """Critical Safety の stateful な決定論的 evaluator。

    instance は1回の takeover から停止まで使う。再起動で新しい instance を
    作ると必ず ``STARTUP`` の Max から始まり、Manual / Calibration も
    Safety の floor や fault を迂回できない。
    """

    def __init__(
        self,
        config: SafetyConfig,
        *,
        approved_t_sensor_metric: str | None = None,
        metric_catalog: MetricCatalog | None = None,
    ) -> None:
        t_sensor_enabled = config.telemetry.t_sensor.enabled.value
        if t_sensor_enabled and (approved_t_sensor_metric is None or metric_catalog is None):
            raise ValueError(
                "T_SENSOR 有効化には承認済みの metric contract と MetricCatalog を注入する"
            )
        if not t_sensor_enabled and approved_t_sensor_metric is not None:
            raise ValueError("T_SENSOR が無効のときは metric contract を注入しない")
        if approved_t_sensor_metric is not None:
            validate_metric(approved_t_sensor_metric)
            if (
                approved_t_sensor_metric in ABSOLUTE_TEMPERATURE_METRICS
                or approved_t_sensor_metric == AIR_TELEMETRY_GROUP
            ):
                raise ValueError("T_SENSOR metric は既存の Safety 入力名と重複させない")
            assert metric_catalog is not None
            unit = metric_catalog.unit_for(approved_t_sensor_metric)
            if unit != "C":
                raise ValueError(
                    "T_SENSOR metric は MetricCatalog に温度(C)として定義する: "
                    f"metric={approved_t_sensor_metric}, unit={unit}"
                )
        self._config = config
        self._t_sensor_metric = approved_t_sensor_metric
        self._disabled_inputs = (
            ()
            if t_sensor_enabled
            else (
                Reason(
                    code="t_sensor_disabled",
                    detail="installation, calibration, and metric contract are not approved",
                ),
            )
        )
        self._started_ms: int | None = None
        self._last_tick_id: int | None = None
        self._last_monotonic_ms: int | None = None
        self._startup = True
        self._startup_tach_seen: set[Zone] = set()
        self._stall_started_ms: dict[Zone, int] = {}
        self._last_effective_demand: dict[Zone, float] = {}
        self._write_failure_counts = {zone: 0 for zone in Zone}
        self._overrun_count = 0
        self._latched_faults: dict[tuple[FaultCode, Zone | None], _LatchedFault] = {}
        self._config_is_provisional = _contains_provisional(config.model_dump(mode="python"))

    @property
    def config_is_provisional(self) -> bool:
        """安全値に未確定の項目があることを返す。値そのものは変えない。"""
        return self._config_is_provisional

    def evaluate(
        self,
        snapshot: ControlStateSnapshot,
        *,
        mode: OperatingMode,
        external_faults: tuple[Fault, ...] = (),
        tick_overrun: bool = False,
    ) -> CriticalSafetyDecision:
        """snapshot と actuator 帰還 fault から、この tick の最終制約を作る。

        ``external_faults`` は Hardware Backend や決定論的層が検出したものだけを
        受け取る。ML の失敗は Fallback の責務であり、Safety state を変えない。
        """
        self._check_tick_order(snapshot)
        now_ms = snapshot.monotonic_ms
        if self._started_ms is None:
            self._started_ms = now_ms

        self._observe_startup_tach(snapshot)
        self._update_write_failure_counts(external_faults)
        self._overrun_count = self._overrun_count + 1 if tick_overrun else 0

        observed: list[tuple[Fault, bool]] = []
        observed.extend((fault, False) for fault in self._telemetry_faults(snapshot))
        temperature_fault = self._absolute_temperature_fault(snapshot)
        if temperature_fault is not None:
            observed.append((temperature_fault, True))
        observed.extend(self._classify_external_fault(fault) for fault in external_faults)
        observed.extend(
            (fault, self._fault_is_emergency(fault)) for fault in self._stall_faults(snapshot)
        )

        if self._overrun_count >= self._config.overrun_consecutive_limit.value:
            observed.append(
                (
                    Fault(
                        code=FaultCode.TICK_OVERRUN,
                        detail=(
                            f"control tick deadline overrun x{self._overrun_count} "
                            f"(limit={self._config.overrun_consecutive_limit.value})"
                        ),
                    ),
                    True,
                )
            )

        self._update_latched_faults(observed, now_ms)
        faults = tuple(
            item.fault
            for _, item in sorted(
                self._latched_faults.items(),
                key=lambda entry: (entry[0][0].value, entry[0][1].value if entry[0][1] else ""),
            )
        )
        emergency = any(item.emergency for item in self._latched_faults.values())
        state = self._next_state(snapshot, emergency, bool(faults))
        return CriticalSafetyDecision(
            state=state,
            zones=self._zone_outputs(snapshot, state, mode, faults),
            faults=faults,
            disabled_inputs=self._disabled_inputs,
            config_is_provisional=self._config_is_provisional,
        )

    def _check_tick_order(self, snapshot: ControlStateSnapshot) -> None:
        if self._last_tick_id is not None and snapshot.tick_id <= self._last_tick_id:
            raise ValueError("Critical Safety にtickの重複・逆行を入力しない")
        if self._last_monotonic_ms is not None and snapshot.monotonic_ms <= self._last_monotonic_ms:
            raise ValueError("Critical Safety の単調時計は tick ごとに進める")
        self._last_tick_id = snapshot.tick_id
        self._last_monotonic_ms = snapshot.monotonic_ms

    def _telemetry_faults(self, snapshot: ControlStateSnapshot) -> tuple[Fault, ...]:
        unavailable = set(snapshot.critical_unavailable)
        known = {
            CPU_TEMPERATURE_METRIC,
            GPU_TEMPERATURE_METRIC,
            AIR_TELEMETRY_GROUP,
        }
        if self._t_sensor_metric is not None:
            known.add(self._t_sensor_metric)
        unknown = unavailable - known
        if unknown:
            # 新しい Critical 入力を黙って無視するより、上位でプロセスを終了し
            # handoff Max に移る。0028 §2.7 の「Safety 自体の例外は捕まえない」。
            raise ValueError(f"Critical Safety が分類できない Critical 入力: {sorted(unknown)}")

        faults: list[Fault] = []
        if not _signal_available(snapshot, CPU_TEMPERATURE_METRIC):
            faults.append(Fault(code=FaultCode.CPU_TELEMETRY_STALE))
        if not _signal_available(snapshot, GPU_TEMPERATURE_METRIC):
            faults.append(Fault(code=FaultCode.GPU_TELEMETRY_STALE))
        if self._t_sensor_metric is not None and not _signal_available(
            snapshot, self._t_sensor_metric
        ):
            faults.append(Fault(code=FaultCode.T_SENSOR_STALE))
        if AIR_TELEMETRY_GROUP in unavailable:
            faults.append(Fault(code=FaultCode.AIR_TELEMETRY_STALE))
        return tuple(faults)

    def _absolute_temperature_fault(self, snapshot: ControlStateSnapshot) -> Fault | None:
        ceiling = self._config.absolute_temp_ceiling_c.value
        exceeded = sorted(
            (signal.metric, signal.value)
            for signal in snapshot.signals
            if self._is_absolute_temperature_metric(signal.metric)
            and signal.available
            and signal.value is not None
            and signal.value >= ceiling
        )
        if not exceeded:
            return None
        detail = ", ".join(f"{metric}={value:g}C" for metric, value in exceeded)
        return Fault(
            code=FaultCode.ABSOLUTE_TEMPERATURE_LIMIT,
            detail=f"absolute ceiling {ceiling:g}C reached: {detail}",
        )

    def _is_absolute_temperature_metric(self, metric: str) -> bool:
        return metric in ABSOLUTE_TEMPERATURE_METRICS or metric == self._t_sensor_metric

    def _observe_startup_tach(self, snapshot: ControlStateSnapshot) -> None:
        if snapshot.fans is None:
            return
        for zone in Zone:
            rpm = snapshot.fans.get(zone).rpm
            if rpm is not None and rpm >= self._config.stall_min_rpm.get(zone).value:
                self._startup_tach_seen.add(zone)

    def _stall_faults(self, snapshot: ControlStateSnapshot) -> tuple[Fault, ...]:
        faults: list[Fault] = []
        for zone in Zone:
            fan = None if snapshot.fans is None else snapshot.fans.get(zone)
            if fan is not None and fan.effective_demand is not None:
                self._last_effective_demand[zone] = fan.effective_demand
            demand = (
                fan.effective_demand
                if fan is not None and fan.effective_demand is not None
                else self._last_effective_demand.get(zone, 1.0)
            )
            rpm = None if fan is None else fan.rpm
            if demand < self._config.stall_check_min_demand.get(zone).value or (
                rpm is not None and rpm >= self._config.stall_min_rpm.get(zone).value
            ):
                self._stall_started_ms.pop(zone, None)
                continue
            started_ms = self._stall_started_ms.setdefault(zone, snapshot.monotonic_ms)
            if snapshot.monotonic_ms - started_ms >= self._config.stall_window_ms.value:
                faults.append(
                    Fault(
                        code=FaultCode.TACH_STALL,
                        zone=zone,
                        detail=(
                            "rpm="
                            + ("unavailable" if rpm is None else str(rpm))
                            + f", demand={demand:g}, "
                            + ("fan_readback=unavailable, " if fan is None else "")
                            + f"window_ms={self._config.stall_window_ms.value}"
                        ),
                    )
                )
        return tuple(faults)

    def _update_write_failure_counts(self, faults: tuple[Fault, ...]) -> None:
        failed_zones = {
            fault.zone for fault in faults if fault.code in _WRITE_FAULTS and fault.zone is not None
        }
        for zone in Zone:
            self._write_failure_counts[zone] = (
                self._write_failure_counts[zone] + 1 if zone in failed_zones else 0
            )

    def _classify_external_fault(self, fault: Fault) -> tuple[Fault, bool]:
        emergency = self._fault_is_emergency(fault)
        if (
            fault.code in _WRITE_FAULTS
            and fault.zone is not None
            and self._write_failure_counts[fault.zone]
            >= self._config.write_fail_emergency_after.value
        ):
            emergency = True
        return fault, emergency

    @staticmethod
    def _fault_is_emergency(fault: Fault) -> bool:
        return fault.code in EMERGENCY_FAULTS or (
            fault.zone is Zone.TOP and fault.code in TOP_EMERGENCY_FAULTS
        )

    def _update_latched_faults(self, observed: list[tuple[Fault, bool]], now_ms: int) -> None:
        current: dict[tuple[FaultCode, Zone | None], tuple[Fault, bool]] = {}
        for fault, emergency in observed:
            key = (fault.code, fault.zone)
            previous = current.get(key)
            current[key] = (fault, emergency or (previous[1] if previous else False))

        for key, item in tuple(self._latched_faults.items()):
            if key in current:
                continue
            if item.clear_since_ms is None:
                item.clear_since_ms = now_ms
            elif now_ms - item.clear_since_ms >= self._config.fault_clear_hold_ms.value:
                del self._latched_faults[key]

        for key, (fault, emergency) in current.items():
            existing = self._latched_faults.get(key)
            if existing is None:
                self._latched_faults[key] = _LatchedFault(fault=fault, emergency=emergency)
            else:
                existing.fault = fault
                existing.emergency = existing.emergency or emergency
                existing.clear_since_ms = None

    def _next_state(
        self,
        snapshot: ControlStateSnapshot,
        emergency: bool,
        has_faults: bool,
    ) -> SafetyState:
        if emergency:
            return SafetyState.EMERGENCY
        assert self._started_ms is not None
        startup_ready = (
            snapshot.monotonic_ms - self._started_ms >= self._config.startup_settle_ms.value
            and _signal_available(snapshot, CPU_TEMPERATURE_METRIC)
            and self._startup_tach_seen == set(Zone)
        )
        if self._startup and not startup_ready:
            return SafetyState.STARTUP
        self._startup = False
        return SafetyState.DEGRADED if has_faults else SafetyState.NORMAL

    def _zone_outputs(
        self,
        snapshot: ControlStateSnapshot,
        state: SafetyState,
        mode: OperatingMode,
        faults: tuple[Fault, ...],
    ) -> PerZone[SafetyZoneOutput]:
        floors = {zone: self._config.zone_min_demand.get(zone).value for zone in Zone}
        floor_codes = {zone: "minimum_safe_demand" for zone in Zone}
        cpu = snapshot.signals_by_metric.get(CPU_TEMPERATURE_METRIC)
        if cpu is not None and cpu.available and cpu.value is not None:
            cpu_floor = _interpolate_cpu_floor(self._config, cpu.value)
            if cpu_floor > floors[Zone.TOP]:
                floors[Zone.TOP] = cpu_floor
                floor_codes[Zone.TOP] = "cpu_cooling_floor"

        forced_by_fault: dict[Zone, Fault] = {}
        for fault in faults:
            if fault.code in {
                FaultCode.GPU_TELEMETRY_STALE,
                FaultCode.T_SENSOR_STALE,
                FaultCode.AIR_TELEMETRY_STALE,
            }:
                for zone in (Zone.FRONT, Zone.REAR):
                    floors[zone] = max(floors[zone], self._config.fault_demand.value)
                    floor_codes[zone] = "critical_telemetry_fault_demand"
            if fault.code is FaultCode.CPU_TELEMETRY_STALE:
                forced_by_fault[Zone.TOP] = fault
            if fault.code in _FAN_FAULTS and fault.zone is not None:
                forced_by_fault[fault.zone] = fault
                for zone in Zone:
                    if zone is not fault.zone:
                        floors[zone] = max(floors[zone], self._config.fault_demand.value)
                        floor_codes[zone] = "fan_fault_demand"

        all_forced_code: str | None = None
        if state is SafetyState.STARTUP:
            all_forced_code = "startup_max"
        elif state is SafetyState.EMERGENCY:
            all_forced_code = "emergency_max"
        elif mode is OperatingMode.MAX:
            all_forced_code = "manual_max"

        def output(zone: Zone) -> SafetyZoneOutput:
            forced_fault = forced_by_fault.get(zone)
            forced_max = all_forced_code is not None or forced_fault is not None
            if all_forced_code is not None:
                reason = Reason(code=all_forced_code, detail=f"safety_state={state.value}")
            elif forced_fault is not None:
                reason = Reason(
                    code="zone_fault_max",
                    detail=f"{forced_fault.code.value}:{zone.value}",
                )
            else:
                reason = Reason(code=floor_codes[zone])
            return SafetyZoneOutput(floor=floors[zone], forced_max=forced_max, reason=reason)

        return PerZone(
            front=output(Zone.FRONT),
            rear=output(Zone.REAR),
            top=output(Zone.TOP),
        )


def invalid_config_decision() -> CriticalSafetyDecision:
    """ハードウェア特定済みで Safety/Policy 設定が不正な場合の Max 裁定。

    hardware mapping も不正な場合はこの関数を使わず、BIOS 制御のまま
    終了する。この判定は不正な閾値を読まず、Max 以外を表現しない。
    """
    fault = Fault(code=FaultCode.CONFIG_INVALID)
    reason = Reason(code="config_invalid_max")
    zone = SafetyZoneOutput(floor=1.0, forced_max=True, reason=reason)
    return CriticalSafetyDecision(
        state=SafetyState.EMERGENCY,
        zones=PerZone(front=zone, rear=zone, top=zone),
        faults=(fault,),
        config_validated=False,
        config_is_provisional=False,
    )


class DemandComposer:
    """検証済み SafetyConfig と直前 effective を所有する唯一の合成経路。"""

    def __init__(self, config: SafetyConfig) -> None:
        self._ramp_down_per_s = config.ramp_down_per_s.value
        self._previous: PerZone[EffectiveZoneDemand] | None = None
        self._last_monotonic_ms: int | None = None

    def compose(
        self,
        *,
        requested: PerZone[ZoneRequest],
        guard: PerZone[GuardZoneOutput],
        safety: CriticalSafetyDecision,
        mode: OperatingMode,
        now_mono_ms: int,
    ) -> PerZone[EffectiveZoneDemand]:
        """0028 §2.4 の順序で合成し、次 tick 用 effective を内部保持する。"""
        if now_mono_ms < 0:
            raise ValueError("合成の単調時計は負にできない")
        if self._last_monotonic_ms is None:
            if not all(safety.zones.get(zone).forced_max for zone in Zone):
                raise ValueError("最初の合成は STARTUP / EMERGENCY の forced Max にする")
            elapsed_ms = 0
        else:
            if now_mono_ms <= self._last_monotonic_ms:
                raise ValueError("合成の単調時計は tick ごとに前進させる")
            elapsed_ms = now_mono_ms - self._last_monotonic_ms

        effective = _compose_effective_demands(
            requested=requested,
            guard=guard,
            safety=safety,
            mode=mode,
            previous=self._previous,
            elapsed_ms=elapsed_ms,
            ramp_down_per_s=self._ramp_down_per_s,
        )
        self._previous = effective
        self._last_monotonic_ms = now_mono_ms
        return effective


def _compose_effective_demands(
    *,
    requested: PerZone[ZoneRequest],
    guard: PerZone[GuardZoneOutput],
    safety: CriticalSafetyDecision,
    mode: OperatingMode,
    previous: PerZone[EffectiveZoneDemand] | None,
    elapsed_ms: int,
    ramp_down_per_s: float,
) -> PerZone[EffectiveZoneDemand]:
    """0028 §2.4 の優先順位で requested / Guard / Safety を合成する。

    上げる速さは制限せず、下げる速さだけを ``ramp_down_per_s`` で制限する。
    値は検証済み Safety Config から渡すことを前提に、ここでは既定値を
    持たない。
    """
    if elapsed_ms < 0:
        raise ValueError("elapsed_ms は 0 以上にする")
    if ramp_down_per_s < 0.0 or not isfinite(ramp_down_per_s):
        raise ValueError("ramp_down_per_s は有限の 0 以上にする")

    def compose(zone: Zone) -> EffectiveZoneDemand:
        zone_request = requested.get(zone)
        zone_guard = guard.get(zone)
        zone_safety = safety.zones.get(zone)
        request_value = zone_request.demand
        applied_ceiling = zone_guard.ceiling if mode is OperatingMode.AUTO else None
        x1 = request_value if applied_ceiling is None else min(request_value, applied_ceiling)
        x2 = x1 if zone_guard.floor is None else max(x1, zone_guard.floor)
        x3 = max(x2, zone_safety.floor)
        ramp_floor: float | None = None
        if previous is not None:
            ramp_floor = max(
                0.0,
                previous.get(zone).effective - ramp_down_per_s * elapsed_ms / 1_000,
            )
        x4 = x3 if ramp_floor is None else max(x3, ramp_floor)
        forced_max = zone_safety.forced_max or mode is OperatingMode.MAX
        effective = 1.0 if forced_max else x4

        candidates: dict[BoundBy, float | None] = {
            BoundBy.FORCED_MAX: 1.0 if forced_max else None,
            BoundBy.SAFETY_FLOOR: zone_safety.floor,
            BoundBy.RAMP_DOWN: ramp_floor,
            BoundBy.GUARD_FLOOR: zone_guard.floor,
            BoundBy.GUARD_CEILING: (
                applied_ceiling
                if applied_ceiling is not None and applied_ceiling <= request_value
                else None
            ),
            BoundBy.REQUESTED: request_value,
        }
        bound_by = next(
            bound
            for bound in BOUND_BY_PRECEDENCE
            if candidates[bound] is not None and candidates[bound] == effective
        )

        reasons: list[Reason] = []
        if applied_ceiling is not None and (
            x1 != request_value or bound_by is BoundBy.GUARD_CEILING
        ):
            assert zone_guard.reason is not None
            reasons.append(zone_guard.reason)
        if zone_guard.floor is not None and (x2 != x1 or bound_by is BoundBy.GUARD_FLOOR):
            assert zone_guard.reason is not None
            if zone_guard.reason not in reasons:
                reasons.append(zone_guard.reason)
        if x3 != x2 or bound_by is BoundBy.SAFETY_FLOOR or forced_max:
            assert zone_safety.reason is not None
            reasons.append(zone_safety.reason)
        if ramp_floor is not None and (x4 != x3 or bound_by is BoundBy.RAMP_DOWN):
            reasons.append(
                Reason(
                    code="ramp_down_limit",
                    detail=f"elapsed_ms={elapsed_ms}, rate={ramp_down_per_s:g}/s",
                )
            )
        if mode is OperatingMode.MAX and not zone_safety.forced_max:
            reasons.append(Reason(code="manual_max"))

        return EffectiveZoneDemand(
            requested=request_value,
            effective=effective,
            bound_by=bound_by,
            safety_floor=zone_safety.floor,
            forced_max=forced_max,
            guard_floor=zone_guard.floor,
            guard_ceiling=applied_ceiling,
            reasons=tuple(reasons),
        )

    return PerZone(
        front=compose(Zone.FRONT),
        rear=compose(Zone.REAR),
        top=compose(Zone.TOP),
    )


def _signal_available(snapshot: ControlStateSnapshot, metric: str) -> bool:
    signal = snapshot.signals_by_metric.get(metric)
    return signal is not None and signal.available


def _interpolate_cpu_floor(config: SafetyConfig, temperature_c: float) -> float:
    points = config.cpu_cooling_floor
    if temperature_c <= points[0].temperature_c.value:
        return points[0].demand.value
    if temperature_c >= points[-1].temperature_c.value:
        return points[-1].demand.value
    for lower, upper in pairwise(points):
        if temperature_c <= upper.temperature_c.value:
            fraction = (temperature_c - lower.temperature_c.value) / (
                upper.temperature_c.value - lower.temperature_c.value
            )
            return lower.demand.value + fraction * (upper.demand.value - lower.demand.value)
    raise AssertionError("検証済み CPU cooling floor の補間範囲に到達できなかった")


def _contains_provisional(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("status") == "provisional":
            return True
        return any(_contains_provisional(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_provisional(item) for item in value)
    return False
