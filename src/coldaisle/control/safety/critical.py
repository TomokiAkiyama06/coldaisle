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

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

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
from coldaisle.control.state import ControlInputContract, ControlStateSnapshot, TelemetryImportance
from coldaisle.metrics import MetricCatalog
from coldaisle.store.models import validate_metric

CPU_TEMPERATURE_METRIC = "cpu.package"
GPU_TEMPERATURE_METRIC = "gpu.0.core"
AIR_TELEMETRY_GROUP = "air_telemetry"
AIR_TEMPERATURE_METRICS: frozenset[str] = frozenset(
    {
        "air.front_intake",
        "air.gpu_intake",
        "air.gpu_exhaust",
        "air.top_exhaust",
        "air.rear_exhaust",
    }
)

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


_SAFETY_DECISION_AUTHORITY = object()


class CriticalSafetyDecision(BaseModel):
    """1 tick の Safety 最終裁定。decision trace へそのまま記録できる。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tick_id: int = Field(ge=0)
    monotonic_ms: int = Field(ge=0)
    state: SafetyState
    zones: PerZone[SafetyZoneOutput]
    faults: tuple[Fault, ...] = ()
    disabled_inputs: tuple[Reason, ...] = ()
    config_validated: bool = True
    config_is_provisional: bool
    _authority: object | None = PrivateAttr(default=None)
    _config_payload: str | None = PrivateAttr(default=None)
    _issued_payload: str | None = PrivateAttr(default=None)
    _lineage: object | None = PrivateAttr(default=None)

    def _mark_issued(
        self,
        authority: object,
        *,
        config_payload: str | None,
        lineage: object,
    ) -> CriticalSafetyDecision:
        if authority is not _SAFETY_DECISION_AUTHORITY:
            raise TypeError("CriticalSafety だけが Safety 裁定を発行できる")
        self._authority = authority
        self._config_payload = config_payload
        self._issued_payload = self.model_dump_json()
        self._lineage = lineage
        return self

    def _binding(self, authority: object) -> tuple[str | None, object]:
        if (
            authority is not _SAFETY_DECISION_AUTHORITY
            or self._authority is not _SAFETY_DECISION_AUTHORITY
            or self._issued_payload != self.model_dump_json()
            or self._lineage is None
        ):
            raise ValueError("CriticalSafety が発行していない裁定は合成しない")
        return self._config_payload, self._lineage


_COMPOSED_DEMAND_AUTHORITY = object()


class ComposedDemands:
    """DemandComposer だけが発行できる Hardware Backend 向け command。

    ``EffectiveZoneDemand`` 自体は decision trace の値 object として公開する一方、
    Backend の入力にはこの capability envelope を要求する。これにより通常の
    constructor/API から Safety 合成を省略した書込み command を作れない。
    """

    __slots__ = ("_authority", "_consumed", "_monotonic_ms", "_tick_id", "_zones")
    _authority: object
    _consumed: bool
    _monotonic_ms: int
    _tick_id: int
    _zones: PerZone[EffectiveZoneDemand]

    def __init__(self) -> None:
        raise TypeError("ComposedDemands は DemandComposer からだけ取得する")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ComposedDemands は不変")

    @classmethod
    def _from_composer(
        cls,
        zones: PerZone[EffectiveZoneDemand],
        *,
        authority: object,
        tick_id: int,
        monotonic_ms: int,
    ) -> ComposedDemands:
        if authority is not _COMPOSED_DEMAND_AUTHORITY:
            raise TypeError("DemandComposer だけが command を発行できる")
        command = object.__new__(cls)
        object.__setattr__(command, "_zones", zones)
        object.__setattr__(command, "_authority", _COMPOSED_DEMAND_AUTHORITY)
        object.__setattr__(command, "_consumed", False)
        object.__setattr__(command, "_tick_id", tick_id)
        object.__setattr__(command, "_monotonic_ms", monotonic_ms)
        return command

    def _consume_for_hardware(self) -> tuple[PerZone[EffectiveZoneDemand], int, int]:
        if self._authority is not _COMPOSED_DEMAND_AUTHORITY:
            raise TypeError("DemandComposer が発行していない command は適用できない")
        if self._consumed:
            raise ValueError("同じ fan command を再適用できない")
        object.__setattr__(self, "_consumed", True)
        return self._zones, self._tick_id, self._monotonic_ms

    @property
    def front(self) -> EffectiveZoneDemand:
        return self._zones.front

    @property
    def rear(self) -> EffectiveZoneDemand:
        return self._zones.rear

    @property
    def top(self) -> EffectiveZoneDemand:
        return self._zones.top

    def get(self, zone: Zone) -> EffectiveZoneDemand:
        return self._zones.get(zone)


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
        input_contract: ControlInputContract,
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
        self._config_payload = config.model_dump_json()
        self._lineage = object()
        self._validate_input_contract(config, input_contract, approved_t_sensor_metric)
        self._required_snapshot_metrics = frozenset(
            spec.metric for spec in input_contract.signals if spec.enabled
        )
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

    @staticmethod
    def _validate_input_contract(
        config: SafetyConfig,
        contract: ControlInputContract,
        approved_t_sensor_metric: str | None,
    ) -> None:
        specs = {spec.metric: spec for spec in contract.signals}
        critical_metrics = {
            spec.metric
            for spec in contract.signals
            if spec.enabled and spec.importance is TelemetryImportance.CRITICAL
        }
        expected_critical = {CPU_TEMPERATURE_METRIC, GPU_TEMPERATURE_METRIC}
        if approved_t_sensor_metric is not None:
            expected_critical.add(approved_t_sensor_metric)
        if critical_metrics != expected_critical:
            raise ValueError(
                "Critical Safety の CRITICAL signal contract が一致しない: "
                f"expected={sorted(expected_critical)}, actual={sorted(critical_metrics)}"
            )
        expected_stale_ms = {
            CPU_TEMPERATURE_METRIC: config.telemetry.cpu_ms.value,
            GPU_TEMPERATURE_METRIC: config.telemetry.gpu_ms.value,
            **{metric: config.telemetry.air_ms.value for metric in AIR_TEMPERATURE_METRICS},
        }
        if approved_t_sensor_metric is not None:
            stale_after_ms = config.telemetry.t_sensor.stale_after_ms
            assert stale_after_ms is not None
            expected_stale_ms[approved_t_sensor_metric] = stale_after_ms.value
        stale_mismatch = sorted(
            metric
            for metric, expected in expected_stale_ms.items()
            if metric not in specs or specs[metric].stale_after_ms != expected
        )
        if stale_mismatch:
            raise ValueError(
                "Safety signal contract の stale limit が SafetyConfig と一致しない: "
                f"{stale_mismatch}"
            )
        invalid_air = sorted(
            metric
            for metric in AIR_TEMPERATURE_METRICS
            if metric not in specs
            or not specs[metric].enabled
            or specs[metric].importance is not TelemetryImportance.DEGRADED
        )
        if invalid_air:
            raise ValueError(f"air telemetry contract が不正: {invalid_air}")
        if len(contract.critical_groups) != 1:
            raise ValueError("air_telemetry Critical group は承認済み5 signal全体に固定する")
        group = contract.critical_groups[0]
        if group.code != AIR_TELEMETRY_GROUP or frozenset(group.metrics) != AIR_TEMPERATURE_METRICS:
            raise ValueError("air_telemetry Critical group は承認済み5 signal全体に固定する")

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
        missing_contract_metrics = (
            self._required_snapshot_metrics - snapshot.signals_by_metric.keys()
        )
        if missing_contract_metrics:
            raise ValueError(
                "Safety snapshot に contract の有効 signal が無い: "
                f"{sorted(missing_contract_metrics)}"
            )
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
            tick_id=snapshot.tick_id,
            monotonic_ms=snapshot.monotonic_ms,
            state=state,
            zones=self._zone_outputs(snapshot, state, mode, faults),
            faults=faults,
            disabled_inputs=self._disabled_inputs,
            config_is_provisional=self._config_is_provisional,
        )._mark_issued(
            _SAFETY_DECISION_AUTHORITY,
            config_payload=self._config_payload,
            lineage=self._lineage,
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
        if AIR_TELEMETRY_GROUP in unavailable or all(
            not _signal_available(snapshot, metric) for metric in AIR_TEMPERATURE_METRICS
        ):
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


def invalid_config_decision(*, tick_id: int, monotonic_ms: int) -> CriticalSafetyDecision:
    """ハードウェア特定済みで Safety/Policy 設定が不正な場合の Max 裁定。

    hardware mapping も不正な場合はこの関数を使わず、BIOS 制御のまま
    終了する。この判定は不正な閾値を読まず、Max 以外を表現しない。
    """
    fault = Fault(code=FaultCode.CONFIG_INVALID)
    reason = Reason(code="config_invalid_max")
    zone = SafetyZoneOutput(floor=1.0, forced_max=True, reason=reason)
    return CriticalSafetyDecision(
        tick_id=tick_id,
        monotonic_ms=monotonic_ms,
        state=SafetyState.EMERGENCY,
        zones=PerZone(front=zone, rear=zone, top=zone),
        faults=(fault,),
        config_validated=False,
        config_is_provisional=False,
    )._mark_issued(
        _SAFETY_DECISION_AUTHORITY,
        config_payload=None,
        lineage=object(),
    )


class DemandComposer:
    """検証済み SafetyConfig と直前 effective を所有する唯一の合成経路。"""

    def __init__(self, config: SafetyConfig) -> None:
        self._config_payload: str | None = config.model_dump_json()
        self._lineage: object | None = None
        self._ramp_down_per_s = config.ramp_down_per_s.value
        self._previous: PerZone[EffectiveZoneDemand] | None = None
        self._last_tick_id: int | None = None
        self._last_monotonic_ms: int | None = None
        self._invalid_config_only = False

    @classmethod
    def for_invalid_config(cls) -> DemandComposer:
        """設定値を一切使わず、config-invalid Max だけを出せる composer。"""
        composer = cls.__new__(cls)
        # invalid-config 経路は forced Max だけなので、この値は需要を決めない。
        composer._config_payload = None
        composer._lineage = object()
        composer._ramp_down_per_s = 0.0
        composer._previous = None
        composer._last_tick_id = None
        composer._last_monotonic_ms = None
        composer._invalid_config_only = True
        return composer

    def compose(
        self,
        *,
        requested: PerZone[ZoneRequest],
        guard: PerZone[GuardZoneOutput],
        safety: CriticalSafetyDecision,
        mode: OperatingMode,
    ) -> ComposedDemands:
        """Safety 裁定の時刻だけを使って合成し、次 tick 用 effective を保持する。"""
        if self._invalid_config_only:
            raise ValueError("設定不正時は compose_invalid_config() だけを使う")
        config_payload, lineage = safety._binding(_SAFETY_DECISION_AUTHORITY)
        if not safety.config_validated:
            raise ValueError("config-invalid 裁定は専用 composer で合成する")
        if config_payload != self._config_payload:
            raise ValueError("Safety evaluator と DemandComposer の設定が一致しない")
        if self._lineage is None:
            self._lineage = lineage
        elif lineage is not self._lineage:
            raise ValueError("異なる CriticalSafety instance の裁定を混ぜない")
        return self._compose_and_remember(
            requested=requested,
            guard=guard,
            safety=safety,
            mode=mode,
        )

    def compose_invalid_config(
        self,
        *,
        tick_id: int,
        monotonic_ms: int,
    ) -> ComposedDemands:
        """設定が壊れていても表現できる、全 zone forced Max の専用経路。"""
        if not self._invalid_config_only:
            raise ValueError("検証済み設定の composer では config-invalid 経路を使わない")
        request = ZoneRequest(demand=1.0, reason=Reason(code="config_invalid_max"))
        guard = GuardZoneOutput()
        return self._compose_and_remember(
            requested=PerZone(front=request, rear=request, top=request),
            guard=PerZone(front=guard, rear=guard, top=guard),
            safety=invalid_config_decision(tick_id=tick_id, monotonic_ms=monotonic_ms),
            mode=OperatingMode.AUTO,
        )

    def _compose_and_remember(
        self,
        *,
        requested: PerZone[ZoneRequest],
        guard: PerZone[GuardZoneOutput],
        safety: CriticalSafetyDecision,
        mode: OperatingMode,
    ) -> ComposedDemands:
        now_mono_ms = safety.monotonic_ms
        if self._last_monotonic_ms is None:
            if safety.state not in {SafetyState.STARTUP, SafetyState.EMERGENCY} or not all(
                safety.zones.get(zone).forced_max for zone in Zone
            ):
                raise ValueError("最初の合成は STARTUP / EMERGENCY の forced Max にする")
            elapsed_ms = 0
        else:
            assert self._last_tick_id is not None
            if safety.tick_id <= self._last_tick_id:
                raise ValueError("Safety 裁定の tick は合成ごとに前進させる")
            if now_mono_ms <= self._last_monotonic_ms:
                raise ValueError("Safety 裁定の単調時計は合成ごとに前進させる")
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
        self._last_tick_id = safety.tick_id
        self._last_monotonic_ms = now_mono_ms
        return ComposedDemands._from_composer(
            effective,
            authority=_COMPOSED_DEMAND_AUTHORITY,
            tick_id=safety.tick_id,
            monotonic_ms=safety.monotonic_ms,
        )


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
