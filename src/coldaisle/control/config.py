"""Fan control configuration contracts (#103).

値を持つ三つの YAML は、全てを検証できたときだけひとまとまりとして採用する。
このモジュールは設定値を決めず、実機への書き込み経路も持たない。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle.control.schema import AuthorityStage, Demand, PerZone, Zone
from coldaisle.store.models import validate_metric

CONTROL_CONFIG_VERSION: Literal[3] = 3
FAN_POLICY_CONFIG_VERSION: Literal[3] = 3
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


def _yaml_zones_to_frozenset(value: object) -> object:
    """YAMLのzone配列を不変のZone集合に正規化する。"""
    if isinstance(value, list):
        return frozenset(Zone(item) if isinstance(item, str) else item for item in value)
    return value


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
PolicyFloat = ConfigValue[FiniteFloat]
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
    def _targets_one_header_channel(self) -> Self:
        if "hwmon" in self.label.lower() or "hwmon" in self.driver.lower():
            raise ValueError("hwmonN の番号では header を特定しない")
        if self.enable_attribute != f"{self.pwm_attribute}_enable":
            raise ValueError("enable_attribute は pwm_attribute と同じ channel を指定する")
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


class TSensorTelemetry(_ConfigModel):
    """未設置を明示できる温度計モジュールの安全設定。"""

    enabled: ConfigValue[bool]
    stale_after_ms: SafetyMilliseconds | None = None

    @model_validator(mode="after")
    def _enabled_sensor_is_confirmed_and_timed(self) -> Self:
        if self.enabled.value and self.enabled.status != "confirmed":
            raise ValueError("T_SENSOR を有効化するには confirmed の承認が必要")
        if self.enabled.value and self.stale_after_ms is None:
            raise ValueError("有効な T_SENSOR には stale_after_ms が必要")
        if not self.enabled.value and self.stale_after_ms is not None:
            raise ValueError("無効な T_SENSOR に stale_after_ms は指定しない")
        return self


class TelemetryDelays(_ConfigModel):
    cpu_ms: SafetyMilliseconds
    gpu_ms: SafetyMilliseconds
    t_sensor: TSensorTelemetry
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


class FallbackTemperatureInputs(_ConfigModel):
    """1 zone の temperature feedback に使う Snapshot signal。"""

    metrics: Annotated[
        tuple[str, ...], BeforeValidator(_yaml_sequence_to_tuple), Field(min_length=1)
    ]

    @model_validator(mode="after")
    def _metrics_are_unique_and_valid(self) -> Self:
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("Fallback temperature metric は重複させない")
        for metric in self.metrics:
            validate_metric(metric)
        return self


class PowerDemandPoint(_ConfigModel):
    """Power feed-forward の入力値と要求 demand。"""

    power_w: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    demand: Demand


class FallbackPowerCurve(_ConfigModel):
    """1 zone の Power signal と demand curve。未設定なら feed-forward を使わない。"""

    metric: str
    curve: Annotated[
        tuple[PowerDemandPoint, ...],
        BeforeValidator(_yaml_sequence_to_tuple),
        Field(min_length=2),
    ]

    @model_validator(mode="after")
    def _curve_is_monotonic(self) -> Self:
        validate_metric(self.metric)
        previous_power: float | None = None
        previous_demand: float | None = None
        for point in self.curve:
            if previous_power is not None and point.power_w <= previous_power:
                raise ValueError("Fallback Power curve の power_w は単調増加にする")
            if previous_demand is not None and point.demand < previous_demand:
                raise ValueError("Fallback Power curve の demand は下げない")
            previous_power = point.power_w
            previous_demand = point.demand
        return self


class FallbackDynamics(_ConfigModel):
    """Fallback 内で demand を下げる前の hysteresis / hold。"""

    decrease_hysteresis: Demand
    decrease_hold_ms: PositiveMilliseconds


class GateConfidenceThresholds(_ConfigModel):
    """Authority stage ごとに Learned MPC へ要求する最低 confidence。"""

    limited: PolicyUnitInterval
    expanded: PolicyUnitInterval
    full: PolicyUnitInterval

    @model_validator(mode="after")
    def _higher_authority_needs_at_least_as_much_confidence(self) -> Self:
        if not (self.limited.value <= self.expanded.value <= self.full.value):
            raise ValueError("gate_min_confidence は limited <= expanded <= full にする")
        return self


class GuardThresholdBand(_ConfigModel):
    """1つの Guard trigger の発火・解除閾値。

    ``degraded_*`` は決定記録 0029 が求める保守側の閾値組である。
    実測前の値をコードに隠さないよう、4値とも追跡可能にする。
    """

    activate_above: PolicyFloat
    clear_at_or_below: PolicyFloat
    degraded_activate_above: PolicyFloat
    degraded_clear_at_or_below: PolicyFloat

    @model_validator(mode="after")
    def _has_hysteresis_and_a_conservative_profile(self) -> Self:
        if self.clear_at_or_below.value >= self.activate_above.value:
            raise ValueError("Guard の解除閾値は発火閾値より低くする")
        if self.degraded_clear_at_or_below.value >= self.degraded_activate_above.value:
            raise ValueError("Degraded Guard の解除閾値は発火閾値より低くする")
        if self.degraded_activate_above.value > self.activate_above.value:
            raise ValueError("Degraded Guard の発火閾値は通常より保守側にする")
        if self.degraded_clear_at_or_below.value > self.clear_at_or_below.value:
            raise ValueError("Degraded Guard の解除閾値は通常より保守側にする")
        return self


class ReactiveGuardConfig(_ConfigModel):
    """Reactive Guard の暫定 floor・hold・trigger 閾値。"""

    floor: PolicyDemand
    hold_ms: PolicyMilliseconds
    cpu_power_metric: ConfigValue[str] | None
    cpu_temperature_rate_c_per_s: GuardThresholdBand
    gpu_temperature_rate_c_per_s: GuardThresholdBand
    cpu_power_rate_w_per_s: GuardThresholdBand
    gpu_power_rate_w_per_s: GuardThresholdBand
    intake_rise_c: GuardThresholdBand
    gpu_hotspot_c: GuardThresholdBand

    @model_validator(mode="after")
    def _cpu_power_metric_requires_approval(self) -> Self:
        if self.cpu_power_metric is None:
            return self
        validate_metric(self.cpu_power_metric.value)
        if self.cpu_power_metric.status != "confirmed":
            raise ValueError("CPU Power trigger の metric は confirmed 承認を必須にする")
        return self


class AuthorityLimit(_ConfigModel):
    """Fallbackからの変化量と、MLを使ってよいzoneをstageごとに制限する。"""

    permitted_zones: Annotated[
        frozenset[Zone], BeforeValidator(_yaml_zones_to_frozenset), Field(min_length=1)
    ]
    limit_up: Demand
    limit_down: Demand


class AuthorityLimits(_ConfigModel):
    """LIMITED / EXPANDED にだけ適用する、設定上のauthority上限。"""

    limited: AuthorityLimit
    expanded: AuthorityLimit


class MpcTiming(_ConfigModel):
    period_ms: PositiveMilliseconds
    budget_ms: PositiveMilliseconds
    valid_ms: PositiveMilliseconds

    @model_validator(mode="after")
    def _budget_fits_period(self) -> Self:
        if self.budget_ms > self.period_ms:
            raise ValueError("mpc.budget_ms は mpc.period_ms 以下にする")
        return self


class SupervisorTiming(_ConfigModel):
    period_ms: PositiveMilliseconds
    valid_ms: PositiveMilliseconds


class FanPolicyConfig(_ConfigModel):
    schema_version: Literal[3]
    fallback_curve: Annotated[
        tuple[FallbackPoint, ...], BeforeValidator(_yaml_sequence_to_tuple), Field(min_length=2)
    ]
    fallback_temperature_inputs: PerZone[FallbackTemperatureInputs]
    fallback_power_feedforward: PerZone[FallbackPowerCurve] | None = None
    fallback_dynamics: FallbackDynamics
    reactive_guard: ReactiveGuardConfig
    mpc: MpcTiming
    supervisor: SupervisorTiming
    gate_min_confidence: GateConfidenceThresholds
    authority_stage: Annotated[AuthorityStage, BeforeValidator(_yaml_authority_stage)]
    authority_limits: AuthorityLimits
    recovery_hold_ms: PositiveMilliseconds
    demote_window_ms: PositiveMilliseconds
    demote_after: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def _policy_curve_is_consistent(self) -> Self:
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
    schema_version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConfigSources(_ConfigModel):
    fan_hardware: ConfigSource
    safety: ConfigSource
    policy: ConfigSource


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
                    schema_version=schema_version,
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
        for name in ("cpu_ms", "gpu_ms", "air_ms", "air_sensor_period_ms"):
            append("safety.yaml", f"telemetry.{name}", getattr(safety.telemetry, name))
        append("safety.yaml", "telemetry.t_sensor.enabled", safety.telemetry.t_sensor.enabled)
        if safety.telemetry.t_sensor.stale_after_ms is not None:
            append(
                "safety.yaml",
                "telemetry.t_sensor.stale_after_ms",
                safety.telemetry.t_sensor.stale_after_ms,
            )

        guard = self.policy.reactive_guard
        append("fan-policy.yaml", "reactive_guard.floor", guard.floor)
        append("fan-policy.yaml", "reactive_guard.hold_ms", guard.hold_ms)
        for trigger_name in (
            "cpu_temperature_rate_c_per_s",
            "gpu_temperature_rate_c_per_s",
            "cpu_power_rate_w_per_s",
            "gpu_power_rate_w_per_s",
            "intake_rise_c",
            "gpu_hotspot_c",
        ):
            band = getattr(guard, trigger_name)
            for threshold_name in (
                "activate_above",
                "clear_at_or_below",
                "degraded_activate_above",
                "degraded_clear_at_or_below",
            ):
                append(
                    "fan-policy.yaml",
                    f"reactive_guard.{trigger_name}.{threshold_name}",
                    getattr(band, threshold_name),
                )
        for stage in ("limited", "expanded", "full"):
            append(
                "fan-policy.yaml",
                f"gate_min_confidence.{stage}",
                getattr(self.policy.gate_min_confidence, stage),
            )
        return tuple(values)

    @property
    def actuation_permitted(self) -> bool:
        """実測で確認済みの hardware mapping だけを後続の書込み層へ渡す。"""
        return self.fan_hardware.approval.status == "confirmed"
