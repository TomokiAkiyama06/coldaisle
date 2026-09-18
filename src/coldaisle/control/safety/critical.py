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
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from coldaisle.control.config import (
    ConfigSource,
    ControlConfig,
    FanHardwareConfig,
    SafetyConfig,
    ValidatedFanHardwareDocument,
    load_fan_hardware_document,
)
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
from coldaisle.control.state import (
    STATE_SNAPSHOT_SCHEMA_VERSION,
    ControlInputContract,
    ControlStateSnapshot,
    SignalSpec,
    TelemetryImportance,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store.models import Quality, validate_metric

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
_RUNTIME_BINDING_AUTHORITY = object()


class ControlRuntimeBinding:
    """1つの validated ControlConfig から Safety と Backend を同じ session に束縛する。"""

    __slots__ = (
        "_backend_claimed",
        "_control_config_payload",
        "_emergency_only",
        "_fan_hardware_payload",
        "_lineage",
        "_safety_claimed",
        "_safety_payload",
        "_takeover_acknowledged",
    )
    _backend_claimed: bool
    _control_config_payload: str
    _emergency_only: bool
    _fan_hardware_payload: str
    _lineage: object
    _safety_claimed: bool
    _safety_payload: str | None
    _takeover_acknowledged: bool

    def __init__(self) -> None:
        raise TypeError("ControlRuntimeBinding は validated ControlConfig から作る")

    @classmethod
    def _from_control_config(
        cls,
        config: ControlConfig,
        *,
        authority: object,
    ) -> ControlRuntimeBinding:
        if authority is not _RUNTIME_BINDING_AUTHORITY:
            raise TypeError("runtime binding factory だけが binding を発行できる")
        if not config.actuation_permitted:
            raise ValueError("confirmed ではない fan hardware mapping を有効化できない")
        binding = object.__new__(cls)
        binding._control_config_payload = config.model_dump_json()
        binding._safety_payload = config.safety.model_dump_json()
        binding._fan_hardware_payload = config.fan_hardware.model_dump_json()
        binding._lineage = object()
        binding._emergency_only = False
        binding._safety_claimed = False
        binding._backend_claimed = False
        binding._takeover_acknowledged = False
        return binding

    @classmethod
    def _from_fan_hardware(
        cls,
        document: ValidatedFanHardwareDocument,
        *,
        authority: object,
    ) -> ControlRuntimeBinding:
        if authority is not _RUNTIME_BINDING_AUTHORITY:
            raise TypeError("runtime binding factory だけが binding を発行できる")
        config, source = document._binding_material()
        if config.approval.status != "confirmed":
            raise ValueError("confirmed ではない fan hardware mapping を有効化できない")
        binding = object.__new__(cls)
        binding._control_config_payload = f"{config.model_dump_json()}\n{source.model_dump_json()}"
        binding._safety_payload = None
        binding._fan_hardware_payload = config.model_dump_json()
        binding._lineage = object()
        binding._emergency_only = True
        binding._safety_claimed = False
        binding._backend_claimed = False
        binding._takeover_acknowledged = False
        return binding

    def _claim_safety(
        self,
        safety_payload: str,
        *,
        authority: object,
    ) -> tuple[str, object]:
        if authority is not _RUNTIME_BINDING_AUTHORITY:
            raise TypeError("CriticalSafety だけが runtime binding を取得できる")
        if self._emergency_only:
            raise ValueError("emergency runtime binding は通常の CriticalSafety に使えない")
        if self._safety_claimed:
            raise ValueError("同じ runtime binding に複数の CriticalSafety を作れない")
        if safety_payload != self._safety_payload:
            raise ValueError("runtime binding と SafetyConfig が一致しない")
        self._safety_claimed = True
        return self._control_config_payload, self._lineage

    def _claim_backend(
        self,
        fan_hardware_payload: str,
        *,
        authority: object,
    ) -> tuple[str, object]:
        if authority is not _RUNTIME_BINDING_AUTHORITY:
            raise TypeError("Fan Hardware Backend だけが runtime binding を取得できる")
        if self._backend_claimed:
            raise ValueError("同じ runtime binding に複数の Fan Hardware Backend を作れない")
        if fan_hardware_payload != self._fan_hardware_payload:
            raise ValueError("runtime binding と FanHardwareConfig が一致しない")
        self._backend_claimed = True
        return self._control_config_payload, self._lineage

    def _acknowledge_takeover(self, *, authority: object) -> None:
        """Backend が takeover 後の最初の全 zone Max を書いたことを Safety へ伝える。"""
        if authority is not _RUNTIME_BINDING_AUTHORITY:
            raise TypeError("Fan Hardware Backend だけが takeover を確認できる")
        if not self._backend_claimed:
            raise ValueError(
                "Fan Hardware Backend が無い runtime binding は takeover を確認できない"
            )
        self._takeover_acknowledged = True

    def _takeover_is_acknowledged(self, *, authority: object) -> bool:
        if authority is not _RUNTIME_BINDING_AUTHORITY:
            raise TypeError("CriticalSafety だけが takeover の確認を読める")
        return self._takeover_acknowledged

    def _emergency_binding(self, *, authority: object) -> tuple[str, object]:
        if authority is not _RUNTIME_BINDING_AUTHORITY:
            raise TypeError("invalid-config composer だけが emergency binding を取得できる")
        if not self._emergency_only:
            raise ValueError("通常 runtime binding では config-invalid 経路を発行できない")
        return self._control_config_payload, self._lineage


def create_control_runtime_binding(config: ControlConfig) -> ControlRuntimeBinding:
    """validated full config/source metadata に一意な control session を発行する。"""
    return ControlRuntimeBinding._from_control_config(
        config,
        authority=_RUNTIME_BINDING_AUTHORITY,
    )


@dataclass(frozen=True, slots=True)
class EmergencyControlRuntime:
    """hardware-only loader が同じ bytes から作る config / source / capability。"""

    fan_hardware: FanHardwareConfig
    source: ConfigSource
    binding: ControlRuntimeBinding


def create_emergency_control_runtime(path: Path) -> EmergencyControlRuntime:
    """Safety/Policy が不正でも、確認済み hardware へ Max だけを許す session。"""
    document = load_fan_hardware_document(path)
    fan_hardware, source = document._binding_material()
    binding = ControlRuntimeBinding._from_fan_hardware(
        document,
        authority=_RUNTIME_BINDING_AUTHORITY,
    )
    return EmergencyControlRuntime(
        fan_hardware=fan_hardware,
        source=source,
        binding=binding,
    )


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
    _runtime_payload: str | None = PrivateAttr(default=None)

    def _mark_issued(
        self,
        authority: object,
        *,
        config_payload: str | None,
        lineage: object,
        runtime_payload: str | None,
    ) -> CriticalSafetyDecision:
        if authority is not _SAFETY_DECISION_AUTHORITY:
            raise TypeError("CriticalSafety だけが Safety 裁定を発行できる")
        self._authority = authority
        self._config_payload = config_payload
        self._issued_payload = self.model_dump_json()
        self._lineage = lineage
        self._runtime_payload = runtime_payload
        return self

    def _binding(self, authority: object) -> tuple[str | None, object, str | None]:
        if (
            authority is not _SAFETY_DECISION_AUTHORITY
            or self._authority is not _SAFETY_DECISION_AUTHORITY
            or self._issued_payload != self.model_dump_json()
            or self._lineage is None
        ):
            raise ValueError("CriticalSafety が発行していない裁定は合成しない")
        return self._config_payload, self._lineage, self._runtime_payload


_COMPOSED_DEMAND_AUTHORITY = object()


class ComposedDemands:
    """DemandComposer だけが発行できる Hardware Backend 向け command。

    ``EffectiveZoneDemand`` 自体は decision trace の値 object として公開する一方、
    Backend の入力にはこの capability envelope を要求する。これにより通常の
    constructor/API から Safety 合成を省略した書込み command を作れない。
    """

    __slots__ = (
        "_authority",
        "_consumed",
        "_monotonic_ms",
        "_runtime_lineage",
        "_runtime_payload",
        "_tick_id",
        "_zones",
    )
    _authority: object
    _consumed: bool
    _monotonic_ms: int
    _runtime_lineage: object | None
    _runtime_payload: str | None
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
        runtime_lineage: object | None,
        runtime_payload: str | None,
    ) -> ComposedDemands:
        if authority is not _COMPOSED_DEMAND_AUTHORITY:
            raise TypeError("DemandComposer だけが command を発行できる")
        command = object.__new__(cls)
        object.__setattr__(command, "_zones", zones)
        object.__setattr__(command, "_authority", _COMPOSED_DEMAND_AUTHORITY)
        object.__setattr__(command, "_consumed", False)
        object.__setattr__(command, "_tick_id", tick_id)
        object.__setattr__(command, "_monotonic_ms", monotonic_ms)
        object.__setattr__(command, "_runtime_lineage", runtime_lineage)
        object.__setattr__(command, "_runtime_payload", runtime_payload)
        return command

    def _hardware_binding(
        self,
    ) -> tuple[PerZone[EffectiveZoneDemand], int, int, str | None, object | None]:
        if self._authority is not _COMPOSED_DEMAND_AUTHORITY:
            raise TypeError("DemandComposer が発行していない command は適用できない")
        if self._consumed:
            raise ValueError("同じ fan command を再適用できない")
        return (
            self._zones,
            self._tick_id,
            self._monotonic_ms,
            self._runtime_payload,
            self._runtime_lineage,
        )

    def _consume_for_hardware(self) -> None:
        if self._authority is not _COMPOSED_DEMAND_AUTHORITY:
            raise TypeError("DemandComposer が発行していない command は適用できない")
        if self._consumed:
            raise ValueError("同じ fan command を再適用できない")
        object.__setattr__(self, "_consumed", True)

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
        runtime_binding: ControlRuntimeBinding | None = None,
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
        self._runtime_binding = runtime_binding
        if runtime_binding is None:
            self._runtime_payload = None
            self._lineage = object()
        else:
            self._runtime_payload, self._lineage = runtime_binding._claim_safety(
                self._config_payload,
                authority=_RUNTIME_BINDING_AUTHORITY,
            )
        self._validate_input_contract(config, input_contract, approved_t_sensor_metric)
        self._signal_specs: dict[str, SignalSpec] = {
            spec.metric: spec for spec in input_contract.signals
        }
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
        # 絶対温度上限を超えた metric。latch が hold を経て外れるまで保持し、これらの
        # fresh な上限未満の値が揃わない tick は「解消」に数えない。
        self._over_temperature_metrics: set[str] = set()
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

    def _takeover_acknowledged(self) -> bool:
        if self._runtime_binding is None:
            return True
        return self._runtime_binding._takeover_is_acknowledged(authority=_RUNTIME_BINDING_AUTHORITY)

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
        self._validate_snapshot(snapshot)
        self._check_tick_order(snapshot)
        now_ms = snapshot.monotonic_ms
        # 0028 §2.7 / §2.5 (d): STARTUP の settle と tach 応答の確認は、Backend が
        # takeover 後の全 zone Max を実際に書いた後から数える。確認前の裁定は常に
        # STARTUP（全 zone Max）なので、先に合成しておいた command を後で渡しても
        # Max の保持を飛ばせない。binding が無い evaluator は Backend を駆動できない
        # （runtime の不一致で拒否される）ため、最初の評価から数える。
        takeover_started_this_tick = False
        if self._started_ms is None and self._takeover_acknowledged():
            self._started_ms = now_ms
            self._startup_tach_seen.clear()
            takeover_started_this_tick = True

        # tach stall は 0028 §2.7 / 0034 §2 で「stall_window_ms の間」続いたときだけの
        # fault と定義されている。Backend の TACH_STALL は1回の帰還にすぎないため直接
        # latch せず、その zone の tach が有効な応答を返していない証拠として同じ timer に渡す。
        backend_stall_zones = frozenset(
            fault.zone
            for fault in external_faults
            if fault.code is FaultCode.TACH_STALL and fault.zone is not None
        )
        external_faults = tuple(
            fault for fault in external_faults if fault.code is not FaultCode.TACH_STALL
        )
        # Backend が stall を報告した zone は同じ tick の RPM が閾値以上でも
        # startup の tach 応答確認に数えない（確認は一度付くと消えないため）。
        # takeover を確認した tick の snapshot は Max を書く前に読んだ可能性があるため、
        # tach 応答の確認には次の tick 以降だけを使う。
        if self._started_ms is not None and not takeover_started_this_tick:
            self._observe_startup_tach(snapshot, backend_stall_zones)
        self._update_write_failure_counts(external_faults)
        self._overrun_count = self._overrun_count + 1 if tick_overrun else 0

        observed: list[tuple[Fault, bool]] = []
        observed.extend((fault, False) for fault in self._telemetry_faults(snapshot))
        temperature_fault = self._absolute_temperature_fault(snapshot)
        if temperature_fault is not None:
            observed.append((temperature_fault, True))
        observed.extend(self._classify_external_fault(fault) for fault in external_faults)
        observed.extend(
            (fault, self._fault_is_emergency(fault))
            for fault in self._stall_faults(snapshot, backend_stall_zones)
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
        if (FaultCode.ABSOLUTE_TEMPERATURE_LIMIT, None) not in self._latched_faults:
            self._over_temperature_metrics.clear()
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
            runtime_payload=self._runtime_payload,
        )

    def _validate_snapshot(self, snapshot: ControlStateSnapshot) -> None:
        if snapshot.schema_version != STATE_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(
                f"Critical Safety が未対応の snapshot schema: {snapshot.schema_version}"
            )
        metrics = tuple(signal.metric for signal in snapshot.signals)
        if len(set(metrics)) != len(metrics):
            raise ValueError("Safety snapshot の signal metric が重複している")
        signals = snapshot.signals_by_metric
        missing_contract_metrics = self._required_snapshot_metrics - signals.keys()
        if missing_contract_metrics:
            raise ValueError(
                "Safety snapshot に contract の有効 signal が無い: "
                f"{sorted(missing_contract_metrics)}"
            )

        for metric in self._required_snapshot_metrics:
            signal = signals[metric]
            spec = self._signal_specs[metric]
            if not signal.enabled or signal.importance is not spec.importance:
                raise ValueError(f"Safety snapshot と input contract が一致しない: {metric}")
            if signal.quality is Quality.OK:
                if (
                    signal.value is None
                    or signal.last_changed_mono_ms is None
                    or signal.age_ms is None
                ):
                    raise ValueError(f"quality=OK の Safety signal に値・時刻が無い: {metric}")
                if signal.last_changed_mono_ms > snapshot.monotonic_ms:
                    raise ValueError(f"future の Safety signal を受理しない: {metric}")
                calculated_age = snapshot.monotonic_ms - signal.last_changed_mono_ms
                if signal.age_ms != calculated_age:
                    raise ValueError(f"Safety signal の age が単調時計と一致しない: {metric}")
                assert spec.stale_after_ms is not None
                if calculated_age > spec.stale_after_ms:
                    raise ValueError(f"期限切れなのに quality=OK の Safety signal: {metric}")

        expected_unavailable = {
            spec.metric
            for spec in self._signal_specs.values()
            if spec.enabled
            and spec.importance is TelemetryImportance.CRITICAL
            and not signals[spec.metric].available
        }
        if all(not signals[metric].available for metric in AIR_TEMPERATURE_METRICS):
            expected_unavailable.add(AIR_TELEMETRY_GROUP)
        actual_unavailable = set(snapshot.critical_unavailable)
        if len(actual_unavailable) != len(snapshot.critical_unavailable):
            raise ValueError("Safety snapshot の critical_unavailable が重複している")
        if actual_unavailable != expected_unavailable:
            raise ValueError(
                "Critical Safety snapshot の critical_unavailable と signal が矛盾する: "
                f"expected={sorted(expected_unavailable)}, actual={sorted(actual_unavailable)}"
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
        if exceeded:
            self._over_temperature_metrics.update(metric for metric, _ in exceeded)
            detail = ", ".join(f"{metric}={value:g}C" for metric, value in exceeded)
            return Fault(
                code=FaultCode.ABSOLUTE_TEMPERATURE_LIMIT,
                detail=f"absolute ceiling {ceiling:g}C reached: {detail}",
            )
        # 上限を超えた metric が stale / missing になっても、温度が下がった証拠ではない。
        # fresh な上限未満の値を観測するまで fault を観測し続け、clear hold を始めない。
        unconfirmed = sorted(
            metric
            for metric in self._over_temperature_metrics
            if not _signal_available(snapshot, metric)
            or snapshot.signals_by_metric[metric].value is None
        )
        if not unconfirmed:
            return None
        return Fault(
            code=FaultCode.ABSOLUTE_TEMPERATURE_LIMIT,
            detail=(
                f"absolute ceiling {ceiling:g}C reached; no fresh reading below the "
                f"ceiling yet: {', '.join(unconfirmed)}"
            ),
        )

    def _is_absolute_temperature_metric(self, metric: str) -> bool:
        return metric in ABSOLUTE_TEMPERATURE_METRICS or metric == self._t_sensor_metric

    def _observe_startup_tach(
        self, snapshot: ControlStateSnapshot, backend_stall_zones: frozenset[Zone]
    ) -> None:
        if snapshot.fans is None:
            return
        for zone in Zone:
            if zone in backend_stall_zones:
                continue
            rpm = snapshot.fans.get(zone).rpm
            if rpm is not None and rpm >= self._config.stall_min_rpm.get(zone).value:
                self._startup_tach_seen.add(zone)

    def _stall_faults(
        self, snapshot: ControlStateSnapshot, backend_stall_zones: frozenset[Zone]
    ) -> tuple[Fault, ...]:
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
            backend_stall = zone in backend_stall_zones
            # Backend が stall を報告した tick は、readback の RPM が閾値以上でも
            # 有効な応答とみなさない（楽観側で timer を reset しない）。
            tach_ok = (
                not backend_stall
                and rpm is not None
                and rpm >= self._config.stall_min_rpm.get(zone).value
            )
            if demand < self._config.stall_check_min_demand.get(zone).value or tach_ok:
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
                            + ("backend_tach_stall=true, " if backend_stall else "")
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
        if self._started_ms is None:
            return SafetyState.STARTUP
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
        runtime_payload=None,
    )


class DemandComposer:
    """検証済み SafetyConfig と直前 effective を所有する唯一の合成経路。"""

    def __init__(self, config: SafetyConfig) -> None:
        self._config_payload: str | None = config.model_dump_json()
        self._lineage: object | None = None
        self._runtime_payload: str | None = None
        self._ramp_down_per_s = config.ramp_down_per_s.value
        self._previous: PerZone[EffectiveZoneDemand] | None = None
        self._last_tick_id: int | None = None
        self._last_monotonic_ms: int | None = None
        self._invalid_config_only = False

    @classmethod
    def for_invalid_config(
        cls,
        runtime_binding: ControlRuntimeBinding | None = None,
    ) -> DemandComposer:
        """設定値を一切使わず、config-invalid Max だけを出せる composer。"""
        composer = cls.__new__(cls)
        # invalid-config 経路は forced Max だけなので、この値は需要を決めない。
        composer._config_payload = None
        if runtime_binding is None:
            composer._lineage = object()
            composer._runtime_payload = None
        else:
            composer._runtime_payload, composer._lineage = runtime_binding._emergency_binding(
                authority=_RUNTIME_BINDING_AUTHORITY
            )
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
        config_payload, lineage, runtime_payload = safety._binding(_SAFETY_DECISION_AUTHORITY)
        if not safety.config_validated:
            raise ValueError("config-invalid 裁定は専用 composer で合成する")
        if config_payload != self._config_payload:
            raise ValueError("Safety evaluator と DemandComposer の設定が一致しない")
        if self._lineage is None:
            self._lineage = lineage
            self._runtime_payload = runtime_payload
        elif lineage is not self._lineage:
            raise ValueError("異なる CriticalSafety instance の裁定を混ぜない")
        elif runtime_payload != self._runtime_payload:
            raise ValueError("異なる control runtime の裁定を混ぜない")
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
            runtime_lineage=self._lineage if self._runtime_payload is not None else None,
            runtime_payload=self._runtime_payload,
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
