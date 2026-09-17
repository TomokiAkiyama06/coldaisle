"""Fan control configuration contracts (#103).

値を持つ三つの YAML は、全てを検証できたときだけひとまとまりとして採用する。
このモジュールは設定値を決めず、実機への書き込み経路も持たない。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Annotated, Any, Literal, Self, cast

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle.control.schema import AuthorityStage, Demand, PerZone, Zone

CONTROL_CONFIG_VERSION: Literal[1] = 1
CONFIG_FILENAMES = {
    "fan_hardware": "fan-hardware.yaml",
    "safety": "safety.yaml",
    "policy": "fan-policy.yaml",
}

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
PositiveMilliseconds = Annotated[int, Field(gt=0)]
PositiveRpm = Annotated[int, Field(gt=0)]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


def _yaml_sequence_to_tuple(value: object) -> object:
    """YAML配列を、読み込み後は不変な tuple として保持する。"""
    return tuple(value) if isinstance(value, list) else value


def _yaml_authority_stage(value: object) -> object:
    """YAML文字列を列挙値に正規化し、未知の stage は拒否する。"""
    return AuthorityStage(value) if isinstance(value, str) else value


class _ConfigModel(BaseModel):
    """設定を欠損・余分なキー・暗黙変換から守る共通基底。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ConfigValue[T](_ConfigModel):
    """実測前後を区別する値。confirmed には根拠を必須にする。"""

    value: T
    status: Literal["provisional", "confirmed"]
    basis: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _confirmed_value_has_evidence(self) -> Self:
        if self.status == "confirmed" and self.basis is None:
            raise ValueError("confirmed の値には basis が必要")
        return self


class ConfigApproval(_ConfigModel):
    """実機への書き込みを許可するための測定・承認の記録。"""

    status: Literal["provisional", "confirmed"]
    basis: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _confirmed_approval_has_evidence(self) -> Self:
        if self.status == "confirmed" and self.basis is None:
            raise ValueError("confirmed の設定には basis が必要")
        return self


class ConfigReloadApproval(_ConfigModel):
    """安全設定の差し替えを許可した決定への参照。"""

    reference: str = Field(min_length=1, max_length=500)


class ProvisionalConfigValue(_ConfigModel):
    """起動ログへ出す、値を含めない暫定設定の位置。"""

    source: Literal["fan-hardware.yaml", "safety.yaml", "fan-policy.yaml"]
    path: str = Field(min_length=1, max_length=500)
    basis: str | None


SafetyDemand = ConfigValue[Demand]
SafetyMilliseconds = ConfigValue[PositiveMilliseconds]
SafetyRpm = ConfigValue[PositiveRpm]
SafetyFloat = ConfigValue[FiniteFloat]
PolicyDemand = ConfigValue[Demand]
PolicyMilliseconds = ConfigValue[PositiveMilliseconds]
PolicyUnitInterval = ConfigValue[UnitInterval]


class PwmRpmPoint(_ConfigModel):
    demand: Demand
    rpm: PositiveRpm


class FanProfile(_ConfigModel):
    """#75 で測る Fan profile。実測値をコードの既定値にしない。"""

    startup_demand: Demand
    minimum_stable_demand: Demand
    maximum_rpm: PositiveRpm
    pwm_to_rpm: Annotated[
        tuple[PwmRpmPoint, ...], BeforeValidator(_yaml_sequence_to_tuple), Field(min_length=2)
    ]
    airflow_index: Annotated[
        tuple[UnitInterval, ...], BeforeValidator(_yaml_sequence_to_tuple), Field(min_length=2)
    ]

    @model_validator(mode="after")
    def _profile_is_monotonic(self) -> Self:
        if self.minimum_stable_demand > self.startup_demand:
            raise ValueError("minimum_stable_demand は startup_demand 以下にする")
        if len(self.pwm_to_rpm) != len(self.airflow_index):
            raise ValueError("pwm_to_rpm と airflow_index の点数を揃える")
        previous_demand = -1.0
        previous_rpm = 0
        previous_airflow = -1.0
        for point, airflow in zip(self.pwm_to_rpm, self.airflow_index, strict=True):
            if point.demand <= previous_demand or point.rpm < previous_rpm:
                raise ValueError("Fan profile の demand と rpm は単調増加にする")
            if airflow < previous_airflow:
                raise ValueError("airflow_index は demand に対して下げない")
            previous_demand = point.demand
            previous_rpm = point.rpm
            previous_airflow = airflow
        if self.pwm_to_rpm[-1].rpm > self.maximum_rpm:
            raise ValueError("PWM→RPM 表は maximum_rpm を超えない")
        return self


class FanHeader(_ConfigModel):
    """sysfs を番号でなく driver・label・属性名で特定する。"""

    driver: str = Field(pattern=r"^[A-Za-z0-9_.-]+$", max_length=120)
    label: str = Field(pattern=r"^[A-Za-z0-9_. -]+$", min_length=1, max_length=120)
    pwm_attribute: str = Field(pattern=r"^pwm[1-9][0-9]*$")
    tach_attribute: str = Field(pattern=r"^fan[1-9][0-9]*_input$")
    enable_attribute: str = Field(pattern=r"^pwm[1-9][0-9]*_enable$")
    profile: FanProfile

    @model_validator(mode="after")
    def _does_not_name_an_unstable_hwmon_number(self) -> Self:
        if "hwmon" in self.label.lower() or "hwmon" in self.driver.lower():
            raise ValueError("hwmonN の番号では header を特定しない")
        return self


class FanHardwareConfig(_ConfigModel):
    schema_version: Literal[1]
    approval: ConfigApproval
    zones: PerZone[FanHeader]

    @model_validator(mode="after")
    def _each_zone_targets_a_different_header(self) -> Self:
        identities = {
            (header.driver, header.label, header.pwm_attribute)
            for header in (self.zones.front, self.zones.rear, self.zones.top)
        }
        if len(identities) != len(Zone):
            raise ValueError("Front / Rear / Top は別々の header を指定する")
        return self


class TemperatureDemandPoint(_ConfigModel):
    temperature_c: SafetyFloat
    demand: SafetyDemand


class TelemetryDelays(_ConfigModel):
    cpu_ms: SafetyMilliseconds
    gpu_ms: SafetyMilliseconds
    t_sensor_ms: SafetyMilliseconds
    air_ms: SafetyMilliseconds
    air_sensor_period_ms: SafetyMilliseconds

    @model_validator(mode="after")
    def _air_delay_exceeds_sensor_period(self) -> Self:
        if self.air_ms.value <= self.air_sensor_period_ms.value:
            raise ValueError("air_ms は air_sensor_period_ms より長くする")
        return self


class SafetyConfig(_ConfigModel):
    """Critical Safety だけが所有する設定。全数値に status/basis を残す。"""

    schema_version: Literal[1]
    absolute_temp_ceiling_c: SafetyFloat
    zone_min_demand: PerZone[SafetyDemand]
    cpu_cooling_floor: Annotated[
        tuple[TemperatureDemandPoint, ...],
        BeforeValidator(_yaml_sequence_to_tuple),
        Field(min_length=2),
    ]
    fault_demand: SafetyDemand
    stall_min_rpm: PerZone[SafetyRpm]
    stall_window_ms: SafetyMilliseconds
    telemetry: TelemetryDelays
    ramp_down_per_s: SafetyFloat
    startup_settle_ms: SafetyMilliseconds
    fault_clear_hold_ms: SafetyMilliseconds
    tick_deadline_ms: SafetyMilliseconds
    overrun_consecutive_limit: ConfigValue[Annotated[int, Field(gt=0)]]
    watchdog_timeout_ms: SafetyMilliseconds

    @model_validator(mode="after")
    def _safety_values_are_consistent(self) -> Self:
        if self.fault_demand.value < max(
            self.zone_min_demand.front.value,
            self.zone_min_demand.rear.value,
            self.zone_min_demand.top.value,
        ):
            raise ValueError("fault_demand は全 zone の最低安全 demand 以上にする")
        if self.ramp_down_per_s.value < 0:
            raise ValueError("ramp_down_per_s は 0 以上にする")
        previous_temperature: float | None = None
        previous_demand: float | None = None
        for point in self.cpu_cooling_floor:
            if (
                previous_temperature is not None
                and point.temperature_c.value <= previous_temperature
            ):
                raise ValueError("cpu_cooling_floor の温度は単調増加にする")
            if previous_demand is not None and point.demand.value < previous_demand:
                raise ValueError("cpu_cooling_floor の demand は下げない")
            previous_temperature = point.temperature_c.value
            previous_demand = point.demand.value
        return self


class FallbackPoint(_ConfigModel):
    temperature_c: FiniteFloat
    demand: Demand


class ReactiveGuard(_ConfigModel):
    """Reactive Guard の閾値。値は全て安全設定と同様に追跡する。"""

    floor: PolicyDemand
    ceiling: PolicyDemand
    hold_ms: PolicyMilliseconds
    intake_rise_threshold_c: ConfigValue[FiniteFloat]
    gpu_hotspot_threshold_c: ConfigValue[FiniteFloat]

    @model_validator(mode="after")
    def _ceiling_is_not_below_floor(self) -> Self:
        if self.ceiling.value < self.floor.value:
            raise ValueError("Reactive Guard の ceiling は floor 以上にする")
        return self


class FanPolicyConfig(_ConfigModel):
    schema_version: Literal[1]
    fallback_curve: Annotated[
        tuple[FallbackPoint, ...], BeforeValidator(_yaml_sequence_to_tuple), Field(min_length=2)
    ]
    reactive_guard: ReactiveGuard
    ml_period_ms: PositiveMilliseconds
    ml_budget_ms: PositiveMilliseconds
    supervisor_period_ms: PositiveMilliseconds
    gate_min_confidence: PolicyUnitInterval
    authority_stage: Annotated[AuthorityStage, BeforeValidator(_yaml_authority_stage)]
    recovery_hold_ms: PositiveMilliseconds
    demotion_after_failures: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def _policy_periods_and_curve_are_consistent(self) -> Self:
        if self.ml_budget_ms > self.ml_period_ms:
            raise ValueError("ml_budget_ms は ml_period_ms 以下にする")
        previous_temperature: float | None = None
        previous_demand: float | None = None
        for point in self.fallback_curve:
            if previous_temperature is not None and point.temperature_c <= previous_temperature:
                raise ValueError("fallback_curve の温度は単調増加にする")
            if previous_demand is not None and point.demand < previous_demand:
                raise ValueError("fallback_curve の demand は下げない")
            previous_temperature = point.temperature_c
            previous_demand = point.demand
        return self


class ConfigSource(_ConfigModel):
    """decision trace に残せる入力の版・名前・内容ハッシュ。絶対 path は残さない。"""

    name: Literal["fan-hardware.yaml", "safety.yaml", "fan-policy.yaml"]
    schema_version: Literal[1]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConfigSources(_ConfigModel):
    fan_hardware: ConfigSource
    safety: ConfigSource
    policy: ConfigSource


class ConfigReloadEvent(_ConfigModel):
    """一括適用した設定変更を decision trace へ渡す不変イベント。"""

    previous_sources: ConfigSources
    current_sources: ConfigSources
    changed_sections: tuple[str, ...]
    approval: ConfigReloadApproval | None

    def trace_metadata(self) -> dict[str, object]:
        """#82 の trace payload に合成できる変更記録。"""
        return {"control_config_reload": self.model_dump(mode="json")}


class ControlConfig(_ConfigModel):
    """一括で検証済みの制御設定と、再現用の入力情報。"""

    fan_hardware: FanHardwareConfig
    safety: SafetyConfig
    policy: FanPolicyConfig
    sources: ConfigSources

    @classmethod
    def from_directory(cls, directory: Path) -> ControlConfig:
        """三つ全てを読んでから生成する。1つでも不正なら返さない。"""
        contents: dict[str, tuple[dict[str, Any], ConfigSource]] = {}
        for key, filename in CONFIG_FILENAMES.items():
            path = directory / filename
            text = path.read_text(encoding="utf-8")
            loaded: Any = yaml.safe_load(text)
            if not isinstance(loaded, dict):
                raise ValueError(f"制御設定が辞書ではない: {filename}")
            schema_version = loaded.get("schema_version")
            if type(schema_version) is not int:
                raise ValueError(f"schema_version が整数ではない: {filename}")
            contents[key] = (
                loaded,
                ConfigSource(
                    name=cast(
                        Literal["fan-hardware.yaml", "safety.yaml", "fan-policy.yaml"], filename
                    ),
                    schema_version=cast(Literal[1], schema_version),
                    sha256=sha256(text.encode("utf-8")).hexdigest(),
                ),
            )
        return cls(
            fan_hardware=FanHardwareConfig.model_validate(contents["fan_hardware"][0]),
            safety=SafetyConfig.model_validate(contents["safety"][0]),
            policy=FanPolicyConfig.model_validate(contents["policy"][0]),
            sources=ConfigSources(
                fan_hardware=contents["fan_hardware"][1],
                safety=contents["safety"][1],
                policy=contents["policy"][1],
            ),
        )

    def trace_metadata(self) -> dict[str, object]:
        """#82 の decision trace payload へ足せる再現情報。"""
        return {"control_config": self.sources.model_dump(mode="json")}

    def provisional_values(self) -> tuple[ProvisionalConfigValue, ...]:
        """起動ログ用に、確認前の設定位置だけを返す。"""
        values: list[ProvisionalConfigValue] = []

        def append(
            source: Literal["fan-hardware.yaml", "safety.yaml", "fan-policy.yaml"],
            path: str,
            value: ConfigApproval | ConfigValue[Any],
        ) -> None:
            if value.status == "provisional":
                values.append(ProvisionalConfigValue(source=source, path=path, basis=value.basis))

        safety = self.safety
        append("fan-hardware.yaml", "approval", self.fan_hardware.approval)
        append("safety.yaml", "absolute_temp_ceiling_c", safety.absolute_temp_ceiling_c)
        append("safety.yaml", "fault_demand", safety.fault_demand)
        append("safety.yaml", "stall_window_ms", safety.stall_window_ms)
        append("safety.yaml", "ramp_down_per_s", safety.ramp_down_per_s)
        append("safety.yaml", "startup_settle_ms", safety.startup_settle_ms)
        append("safety.yaml", "fault_clear_hold_ms", safety.fault_clear_hold_ms)
        append("safety.yaml", "tick_deadline_ms", safety.tick_deadline_ms)
        append("safety.yaml", "overrun_consecutive_limit", safety.overrun_consecutive_limit)
        append("safety.yaml", "watchdog_timeout_ms", safety.watchdog_timeout_ms)
        for zone in Zone:
            append(
                "safety.yaml",
                f"zone_min_demand.{zone.value}",
                safety.zone_min_demand.get(zone),
            )
            append(
                "safety.yaml",
                f"stall_min_rpm.{zone.value}",
                safety.stall_min_rpm.get(zone),
            )
        for index, point in enumerate(safety.cpu_cooling_floor):
            append("safety.yaml", f"cpu_cooling_floor[{index}].temperature_c", point.temperature_c)
            append("safety.yaml", f"cpu_cooling_floor[{index}].demand", point.demand)
        for name in ("cpu_ms", "gpu_ms", "t_sensor_ms", "air_ms", "air_sensor_period_ms"):
            append("safety.yaml", f"telemetry.{name}", getattr(safety.telemetry, name))

        guard = self.policy.reactive_guard
        append("fan-policy.yaml", "reactive_guard.floor", guard.floor)
        append("fan-policy.yaml", "reactive_guard.ceiling", guard.ceiling)
        append("fan-policy.yaml", "reactive_guard.hold_ms", guard.hold_ms)
        append(
            "fan-policy.yaml",
            "reactive_guard.intake_rise_threshold_c",
            guard.intake_rise_threshold_c,
        )
        append(
            "fan-policy.yaml",
            "reactive_guard.gpu_hotspot_threshold_c",
            guard.gpu_hotspot_threshold_c,
        )
        append("fan-policy.yaml", "gate_min_confidence", self.policy.gate_min_confidence)
        return tuple(values)

    @property
    def actuation_permitted(self) -> bool:
        """実測で確認済みの hardware mapping だけを後続の書込み層へ渡す。"""
        return self.fan_hardware.approval.status == "confirmed"

    def approval_required_changes(self, candidate: Self) -> tuple[str, ...]:
        """自動昇格できない変更を、監査可能な区分で返す。"""
        changes: list[str] = []
        if self.fan_hardware != candidate.fan_hardware:
            changes.append("fan_hardware")
        if self.safety != candidate.safety:
            changes.append("safety")
        if self.policy.reactive_guard != candidate.policy.reactive_guard:
            changes.append("reactive_guard")
        if self.policy.authority_stage != candidate.policy.authority_stage:
            changes.append("authority_stage")
        return tuple(changes)


class ConfigApprovalRequiredError(ValueError):
    """人の承認が要る設定変更を自動適用しようとした。"""


class ControlConfigManager:
    """有効設定を候補全体と原子的に入れ替える。Hot-reload の監視は持たない。"""

    def __init__(self, active: ControlConfig) -> None:
        self._active = active
        self._last_reload_event: ConfigReloadEvent | None = None
        self._lock = RLock()

    @property
    def active(self) -> ControlConfig:
        with self._lock:
            return self._active

    @property
    def last_reload_event(self) -> ConfigReloadEvent | None:
        """直近の成功した差し替えを decision trace へ合成する。"""
        with self._lock:
            return self._last_reload_event

    def reload_from_directory(
        self, directory: Path, *, approval: ConfigReloadApproval | None = None
    ) -> ControlConfig:
        """候補を完全検証してから差し替え、監査イベントを残す。"""
        candidate = ControlConfig.from_directory(directory)
        with self._lock:
            previous = self._active
            changes = previous.approval_required_changes(candidate)
            if changes and approval is None:
                names = ", ".join(changes)
                raise ConfigApprovalRequiredError(f"承認が必要な Control Config の変更: {names}")
            self._active = candidate
            self._last_reload_event = ConfigReloadEvent(
                previous_sources=previous.sources,
                current_sources=candidate.sources,
                changed_sections=changes,
                approval=approval,
            )
            return candidate
