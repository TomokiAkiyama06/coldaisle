"""Fan control configuration contracts (#103).

値を持つ四つの YAML（``fan-hardware.yaml`` / ``safety.yaml`` / ``fan-policy.yaml`` /
``air-balance.yaml``）は、全てを検証できたときだけひとまとまりとして採用する
（決定記録 0033 §2 / 0073 §2.1）。
このモジュールは設定値を決めず、実機への書き込み経路も持たない。
"""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from coldaisle.control.air_balance import AirBalanceConfig

# 協調の mode の型は trace と同じ値を指すよう schema に1つだけ置き、
# ここから再公開する（決定記録 0078 §2.7）。
from coldaisle.control.schema import AirBalanceCoordinationMode as AirBalanceCoordinationMode
from coldaisle.control.schema import (
    AuthorityStage,
    ControlConfigDigest,
    Demand,
    PerZone,
    SupervisorObjectiveWeights,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    WorkloadRegime,
    Zone,
)
from coldaisle.measurement import validate_metric

CONTROL_CONFIG_VERSION: Literal[14] = 14
"""4ファイルを束ねた Control Config の版。

- v11（#81 / 決定記録 0073 §2.1）: ``air-balance.yaml``（v2）を4つ目のファイルにした。
  各ファイルの版は変えていない
- v12（#74 / 決定記録 0080 §2.6）: ``safety.yaml`` を v4 にした（``hardware_write_fail_exit_ms``）
- v13（#81 / 決定記録 0078 §2.4）: ``fan-policy.yaml`` を v10 にした
  （``air_balance_coordination``）。
  ``mode: shadow / apply`` と ``air-balance.yaml`` の ``uncalibrated`` の組み合わせを拒否する
- v14（#86 / 決定記録 0077 §2.4 の4・§2.10 段階 3）: ``fan-policy.yaml`` を v11 にした
  （``mpc.max_source_age_ms``）。上限 ``<= mpc.valid_ms`` と、``safety.yaml`` をまたぐ下限
  ``>= 2 * safety.tick_ms + mpc.period_ms + mpc.budget_ms`` を一括検証で確かめる
"""
FAN_POLICY_CONFIG_VERSION: Literal[11] = 11
SAFETY_CONFIG_VERSION: Literal[4] = 4
CONFIG_FILENAMES = {
    "fan_hardware": "fan-hardware.yaml",
    "safety": "safety.yaml",
    "policy": "fan-policy.yaml",
    "air_balance": "air-balance.yaml",
}

ConfigFileName = Literal["fan-hardware.yaml", "safety.yaml", "fan-policy.yaml", "air-balance.yaml"]

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


def _yaml_supervisor_policy(value: object) -> object:
    """YAML文字列を Supervisor policy 列挙値へ正規化する。"""
    return SupervisorPolicyKind(value) if isinstance(value, str) else value


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

    def stable_demand(self, demand: float) -> float:
        """``minimum_stable_demand`` 未満の demand を引き上げた値（決定記録 0073 §2.3）。

        Hardware Backend が書く前に掛ける引き上げと、Learned MPC の balance 項が評価に使う
        写像を**この1箇所**に置く（2つの実装を持たない）。起動時の ``startup_demand`` の
        kick は backend の起動状態に依存する一時的な値なので含めない。
        """
        return max(demand, self.minimum_stable_demand)


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

    def stable_demands(self, demands: PerZone[Demand]) -> PerZone[Demand]:
        """3 zone の demand を各 profile の ``stable_demand()`` に通した値。

        MPC は Hardware Backend を呼ばない（決定記録 0052 の境界）。同じ設定から同じ写像を
        得るための純粋関数で、requested / effective そのものは変えない。
        """
        return PerZone[Demand](
            front=self.zones.front.profile.stable_demand(demands.front),
            rear=self.zones.rear.profile.stable_demand(demands.rear),
            top=self.zones.top.profile.stable_demand(demands.top),
        )

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


class SafetyPowerDemandPoint(_ConfigModel):
    """Critical Safety の CPU Power 曲線の1点。値ごとに provisional / confirmed を持つ。"""

    power_w: SafetyFloat
    demand: SafetyDemand


class TSensorTelemetry(_ConfigModel):
    """未設置を明示できる温度計モジュールの安全設定。"""

    enabled: ConfigValue[bool]
    stale_after_ms: SafetyMilliseconds | None = None
    absolute_ceiling_c: SafetyFloat | None = None
    """T_SENSOR 専用の絶対温度上限（決定記録 0110 §2.2）。

    T_SENSOR はこの値だけで判定し、共通の ``absolute_temp_ceiling_c`` は使わない。
    コネクタの外装温度は GPU / CPU / 空気の温度と許せる温度が違うため。
    """

    @model_validator(mode="after")
    def _enabled_sensor_is_confirmed_and_timed(self) -> Self:
        if self.enabled.value and self.enabled.status != "confirmed":
            raise ValueError("T_SENSOR を有効化するには confirmed の承認が必要")
        if self.enabled.value and self.stale_after_ms is None:
            raise ValueError("有効な T_SENSOR には stale_after_ms が必要")
        if not self.enabled.value and self.stale_after_ms is not None:
            raise ValueError("無効な T_SENSOR に stale_after_ms は指定しない")
        # 有効なのに専用の上限が無いと、どの上限で判定するかが決まらない。旧い版 4 の
        # 「共通の上限で T_SENSOR も判定する」意味で黙って読まないために必須にする
        # （決定記録 0110 §2.6）。無効なのに上限があると、効いていない値を承認済みと読み違える。
        if self.enabled.value and self.absolute_ceiling_c is None:
            raise ValueError("有効な T_SENSOR には absolute_ceiling_c が必要")
        if not self.enabled.value and self.absolute_ceiling_c is not None:
            raise ValueError("無効な T_SENSOR に absolute_ceiling_c は指定しない")
        return self


class TelemetryDelays(_ConfigModel):
    cpu_ms: SafetyMilliseconds
    cpu_power_ms: SafetyMilliseconds
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

    schema_version: Literal[4]
    """v4（決定記録 0080 §2.6）で ``hardware_write_fail_exit_ms`` を必須にした。

    **v3 を v4 として補完しない。** 補うと、所有者が見ていない時間で「書けないまま制御を
    持ち続ける」上限が決まる（0028 §2.8 / ``docs/control-config.md`` の移行の規則）。
    """
    absolute_temp_ceiling_c: SafetyFloat
    """T_SENSOR **以外**の温度の絶対上限（決定記録 0110 §2.2）。

    T_SENSOR は ``telemetry.t_sensor.absolute_ceiling_c`` で判定する。
    """
    zone_min_demand: PerZone[SafetyDemand]
    cpu_cooling_floor: Annotated[
        tuple[TemperatureDemandPoint, ...],
        BeforeValidator(_yaml_sequence_to_tuple),
        Field(min_length=2),
    ]
    cpu_power_cooling_floor: Annotated[
        tuple[SafetyPowerDemandPoint, ...],
        BeforeValidator(_yaml_sequence_to_tuple),
        Field(min_length=2),
    ]
    """CPU Power から Top の floor を決める曲線（0028 §2.4 の cpu_cooling_floor の Power 項）。"""
    fault_demand: SafetyDemand
    stall_check_min_demand: PerZone[SafetyDemand]
    stall_min_rpm: PerZone[SafetyRpm]
    stall_window_ms: SafetyMilliseconds
    write_fail_emergency_after: ConfigValue[Annotated[int, Field(gt=0)]]
    telemetry: TelemetryDelays
    ramp_down_per_s: SafetyFloat
    startup_settle_ms: SafetyMilliseconds
    fault_clear_hold_ms: SafetyMilliseconds
    tick_ms: SafetyMilliseconds
    """control loop の周期（決定記録 0028 §2.6 / 0060 §2.1）。

    **締め切りと同じファイルに置く。** 周期だけを別の設定へ置くと、`tick_deadline_ms`
    より短い周期が検証されないまま採用され、毎 tick が overrun になる。
    """
    tick_deadline_ms: SafetyMilliseconds
    overrun_consecutive_limit: ConfigValue[Annotated[int, Field(gt=0)]]
    watchdog_timeout_ms: SafetyMilliseconds
    hardware_write_fail_exit_ms: SafetyMilliseconds
    """takeover の後、ある zone の書き込みと読み戻しが一度も成功しないまま続いたら
    fand を終了コード 7 で終える時間（決定記録 0080 §2.6）。

    終わるときは引き継ぎ記録を消さないので、``ExecStopPost`` が root で Max を書く。
    ``write_fail_emergency_after``（fand の中で ``EMERGENCY`` へ上げる回数）の**後ろの段**である。
    """

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
        if self.tick_deadline_ms.value > self.tick_ms.value:
            # 締め切りが周期より長いと、超過を検出したときには次の tick が始まっている。
            raise ValueError("tick_deadline_ms は tick_ms 以下にする")
        # heartbeat は tick ごとにしか出ない。次の tick が始まるまで（最悪 tick_ms、
        # 記録の待ちが枠をはみ出せば tick_deadline_ms 余分に）と、その tick の処理
        # （最悪 tick_deadline_ms）を合わせた間隔が空きうる。systemd も `WatchdogSec` の
        # 半分の間隔で通知することを前提にしているので、その2倍を下限にする。
        if self.watchdog_timeout_ms.value < (self.tick_ms.value + self.tick_deadline_ms.value) * 2:
            raise ValueError("watchdog_timeout_ms は (tick_ms + tick_deadline_ms) の2倍以上にする")
        # 決定記録 0080 §2.6 の不変条件。下限は fand の中の再試行と EMERGENCY への昇格
        # （write_fail_emergency_after 回）を先に試すため、上限は「書けないまま制御を持つ」
        # 時間を hang の deadman より長くしないため（書けない fand は冷却の面で hang と同じ）。
        write_fail_floor_ms = (
            self.tick_ms.value + self.tick_deadline_ms.value
        ) * self.write_fail_emergency_after.value
        if self.hardware_write_fail_exit_ms.value < write_fail_floor_ms:
            raise ValueError(
                "hardware_write_fail_exit_ms は "
                "(tick_ms + tick_deadline_ms) * write_fail_emergency_after 以上にする"
            )
        if self.hardware_write_fail_exit_ms.value > self.watchdog_timeout_ms.value:
            raise ValueError("hardware_write_fail_exit_ms は watchdog_timeout_ms 以下にする")
        for zone in Zone:
            if self.stall_check_min_demand.get(zone).value > self.zone_min_demand.get(zone).value:
                raise ValueError(
                    f"stall_check_min_demand.{zone.value} は "
                    f"zone_min_demand.{zone.value} 以下にする"
                )
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
        previous_power: float | None = None
        previous_demand = None
        for power_point in self.cpu_power_cooling_floor:
            if previous_power is not None and power_point.power_w.value <= previous_power:
                raise ValueError("cpu_power_cooling_floor の Power は単調増加にする")
            if previous_demand is not None and power_point.demand.value < previous_demand:
                raise ValueError("cpu_power_cooling_floor の demand は下げない")
            previous_power = power_point.power_w.value
            previous_demand = power_point.demand.value
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


_POWER_DOMAIN = "power"
"""消費電力の metric domain（決定記録 0002 §2.1）。命名規約であり調整値ではない。"""


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
        metric = validate_metric(self.cpu_power_metric.value)
        # 閾値は W/s なので、温度など別ドメインの metric を黙って比較させない。
        # 単位の最終確認は Metric Catalog を持つ ReactiveGuard の生成時に行う。
        if metric.split(".", 1)[0] != _POWER_DOMAIN:
            raise ValueError(
                "CPU Power trigger の metric は power ドメイン（決定記録 0002 §2.1）にする: "
                f"{metric}"
            )
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


PositiveCount = Annotated[int, Field(gt=0)]
NonNegativeFiniteFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
NonNegativeMilliseconds = Annotated[int, Field(ge=0)]
DriftRatio = Annotated[float, Field(gt=1.0, allow_inf_nan=False)]


class AuthorityRolloutConfig(_ConfigModel):
    """Authority stage の昇格条件と自動降格の条件（#92 / 決定記録 0057）。

    **stage そのものはここに置かない。** `authority_stage` は設定が許す**上限**で、
    いま与えている制御権は `AuthorityStore` の journal が持つ（0057 §2.2）。
    この型が持つのは「人の承認と証拠をどこまで新しいものに限るか」と
    「どれだけ不健全が続いたら自動で下げるか」だけである。
    """

    approval_max_age_ms: PolicyMilliseconds
    """承認からこれを過ぎたら昇格に使えない。**承認を貯めて後から使わせない。**"""
    evidence_max_age_ms: PolicyMilliseconds
    """証拠の最後の観測時刻からこれを過ぎたら昇格に使えない。

    証拠の「新しさ」は報告書の自己申告ではなく、**中に記録された run の終了時刻**で測る
    （決定記録 0054 §2.7 が生成時刻を持たないため。0057 §2.4）。
    """
    unhealthy_window_ms: PolicyMilliseconds
    """自動降格のために、低 confidence / OOD の tick を数える直近の窓。"""
    low_confidence_after: ConfigValue[PositiveCount]
    """窓の中で LOW confidence がこの件数に達したら1段下げる。"""
    ood_after: ConfigValue[PositiveCount]
    """窓の中で OOD がこの件数に達したら1段下げる。"""

    @model_validator(mode="after")
    def _approval_does_not_outlive_its_evidence(self) -> Self:
        if self.approval_max_age_ms.value > self.evidence_max_age_ms.value:
            # 承認のほうが長生きすると、承認だけ取って書き込みを遅らせることで
            # 期限切れの証拠での昇格が通ってしまう。
            raise ValueError(
                "authority_rollout.approval_max_age_ms は evidence_max_age_ms 以下にする"
            )
        return self


class ConfidenceAuthorityBand(_ConfigModel):
    """MEDIUM confidence で Learned MPC に許す Fallback 中心の帯（決定記録 0050 §2.4）。

    ``limit_down`` は「Fallback からどこまで下げてよいか」なので、最低 demand の制限を兼ねる。
    """

    limit_up: PolicyDemand
    limit_down: PolicyDemand


class ModelConfidencePolicy(_ConfigModel):
    """Model Confidence / OOD の判定閾値と MEDIUM の authority 帯（#85 / 決定記録 0050）。

    値はすべて実データの評価前の暫定値として扱い、コードに既定値を置かない。
    """

    high_min_confidence: PolicyUnitInterval
    """これ以上を HIGH とする。MEDIUM の下限は stage ごとの ``gate_min_confidence``。"""
    medium_limit: ConfidenceAuthorityBand
    range_margin: ConfigValue[NonNegativeFiniteFloat]
    """学習範囲の幅に対する、範囲外へのはみ出しの許容比。超えたら OOD。"""
    min_support_count: ConfigValue[PositiveCount]
    """support cell（室温・Power・Fan state の組）の学習件数がこれ未満なら OOD。"""
    full_support_count: ConfigValue[PositiveCount]
    """support cell の件数がこれ以上なら support の score を満値にする。"""
    min_missing_pattern_count: ConfigValue[PositiveCount]
    """欠測の組み合わせの学習件数がこれ未満なら OOD。"""
    residual_window: ConfigValue[PositiveCount]
    """residual drift を見る直近の照合済み**予測（forecast）**の件数。出力の数ではない。"""
    residual_min_samples: ConfigValue[PositiveCount]
    """residual drift を評価に使う最低 forecast 件数。

    未満の間は ``cap_before_residual_evidence`` で confidence を抑える。
    """
    residual_match_tolerance_ms: ConfigValue[NonNegativeMilliseconds]
    """期待時刻の前後それぞれに許す照合の幅（決定記録 0031 §2.2 と同じ最近傍・同距離は過去側）。"""
    residual_max_age_ms: ConfigValue[PositiveMilliseconds]
    """解決からこれを過ぎた forecast は residual の証拠に数えない（古い証拠で上限を外さない）。"""
    residual_drift_ood_ratio: ConfigValue[DriftRatio]
    """正規化 residual の RMS が validation 基準のこの倍率以上なら OOD。"""
    cap_without_uncertainty: PolicyUnitInterval
    """モデルが uncertainty を出さないときの confidence の上限。"""
    cap_before_residual_evidence: PolicyUnitInterval
    """residual の照合件数が足りない間の confidence の上限。"""

    @model_validator(mode="after")
    def _counts_are_ordered(self) -> Self:
        if self.full_support_count.value < self.min_support_count.value:
            raise ValueError("model_confidence.full_support_count は min_support_count 以上にする")
        if self.cap_before_residual_evidence.value >= self.high_min_confidence.value:
            # 予測が当たっている証拠が無い間に HIGH（帯なしの authority）へ届かせない。
            raise ValueError(
                "model_confidence.cap_before_residual_evidence は high_min_confidence 未満にする"
            )
        if self.residual_max_age_ms.value <= self.residual_match_tolerance_ms.value:
            raise ValueError(
                "model_confidence.residual_max_age_ms は residual_match_tolerance_ms より長くする"
            )
        if self.residual_min_samples.value > self.residual_window.value:
            raise ValueError("model_confidence.residual_min_samples は residual_window 以下にする")
        return self


MAX_MPC_HORIZON_STEPS = 32
"""1 plan に置ける control step 数の構造上限（#86）。

worker 1回の計算量と trace の大きさを抑えるための境界で、調整値ではない。
horizon / step の実運用値は設定に置く。

**#84 の ``MAX_TARGET_HORIZONS`` を超えない。** 内部モデルの target schema と plan prediction が
32 horizon までしか表現できないため、それより多い step 数の設定は検証を通っても決して動かない。
設定の上限を予測の契約へ合わせる（逆に契約を広げない）。一致は試験で確かめる。
"""

MAX_MPC_EVALUATIONS = 4_096
"""1 tick に許す内部モデル評価回数の構造上限（#86）。budget_ms とは別の、決定論的な打ち切り。"""


class MpcZoneBound(_ConfigModel):
    """Learned MPC が探索してよい zone ごとの demand の範囲（#86）。

    **Critical Safety の floor はここに書かない。** 実行時に safety 側の floor を重ねて
    さらに狭める。この範囲は探索の上限であって、安全上の保証ではない。
    """

    floor: PolicyDemand
    ceiling: PolicyDemand

    @model_validator(mode="after")
    def _floor_is_below_ceiling(self) -> Self:
        if self.floor.value > self.ceiling.value:
            raise ValueError("mpc.optimizer.zone_bounds の floor は ceiling 以下にする")
        return self


class MpcCostScales(_ConfigModel):
    """各コスト項を無単位へ揃える基準量（#86）。

    重み（``SupervisorObjectiveWeights``）は相対値なので、単位の違う項をそのまま足すと
    暗黙の換算係数がコードに埋まる。基準量を設定へ出し、0 除算を避けるため正に限る。
    """

    temperature_c: PolicyFloat
    balance_ratio: PolicyFloat
    acoustic_cost: PolicyFloat
    demand_change: PolicyFloat

    @model_validator(mode="after")
    def _scales_are_positive(self) -> Self:
        for name in ("temperature_c", "balance_ratio", "acoustic_cost", "demand_change"):
            scale: ConfigValue[float] = getattr(self, name)
            if scale.value <= 0.0:
                raise ValueError(f"mpc.optimizer.cost_scales.{name} は正にする")
        return self


class MpcCostMetrics(_ConfigModel):
    """コストに使う予測 metric 名（#86）。

    metric 名をコードに埋めない（AGENTS.md ルール9）。内部モデルの target schema が
    ここで指定した metric を持つことは optimizer の生成時に照合する。
    """

    cpu_temperature: str
    gpu_temperature: str

    @field_validator("cpu_temperature", "gpu_temperature")
    @classmethod
    def _metric_is_stored_telemetry(cls, value: str) -> str:
        return validate_metric(value)

    @model_validator(mode="after")
    def _metrics_are_distinct(self) -> Self:
        if self.cpu_temperature == self.gpu_temperature:
            raise ValueError("mpc.optimizer.cost_metrics の CPU / GPU は別の metric にする")
        return self


class MpcOptimizerConfig(_ConfigModel):
    """Learned MPC の horizon・探索・コストの設定（#86 / 決定記録 0052）。

    値はすべて実測前の暫定値として扱い、コードに既定値を置かない。
    """

    horizon_ms: PolicyMilliseconds
    step_ms: PolicyMilliseconds
    candidate_levels: ConfigValue[Annotated[int, Field(ge=2)]]
    """1 zone の1掃引で試す demand の格子点数。両端を含む等間隔。"""
    sweeps: ConfigValue[PositiveCount]
    """座標降下の掃引回数。増やすほど探索は良くなるが budget を食う。"""
    max_evaluations: ConfigValue[PositiveCount]
    """1 tick に許す内部モデル評価回数。budget_ms とは独立に打ち切る決定論的な上限。"""
    max_step_up: PolicyDemand
    max_step_down: PolicyDemand
    zone_bounds: PerZone[MpcZoneBound]
    cost_scales: MpcCostScales
    cost_metrics: MpcCostMetrics
    unknown_balance_cost: PolicyFloat
    """Air Balance の比を推定できない step に課す無単位コスト。"""

    @model_validator(mode="after")
    def _horizon_is_a_whole_number_of_steps(self) -> Self:
        if self.horizon_ms.value % self.step_ms.value != 0:
            raise ValueError("mpc.optimizer.horizon_ms は step_ms の整数倍にする")
        steps = self.horizon_ms.value // self.step_ms.value
        if steps > MAX_MPC_HORIZON_STEPS:
            raise ValueError(
                f"mpc.optimizer の control step 数は {MAX_MPC_HORIZON_STEPS} 以下にする"
            )
        if self.max_evaluations.value > MAX_MPC_EVALUATIONS:
            raise ValueError(f"mpc.optimizer.max_evaluations は {MAX_MPC_EVALUATIONS} 以下にする")
        if self.unknown_balance_cost.value < 0.0:
            raise ValueError("mpc.optimizer.unknown_balance_cost は 0 以上にする")
        return self

    @property
    def steps(self) -> int:
        """1 plan の control step 数。"""
        return self.horizon_ms.value // self.step_ms.value


class MpcTiming(_ConfigModel):
    period_ms: PositiveMilliseconds
    budget_ms: PositiveMilliseconds
    valid_ms: PositiveMilliseconds
    max_source_age_ms: PolicyMilliseconds
    """元 snapshot の単調時刻から数えた提案の寿命（v11。#86 / 決定記録 0077 §2.4 の4）。

    受信から ``valid_ms`` の条件（0028 §2.6）に**足す**条件で、置き換えない。worker の中で長く
    滞留した提案が、受信した瞬間に新しく見えることを塞ぐ。上限 ``<= valid_ms`` はここで、
    ``safety.yaml`` をまたぐ下限は ``ControlConfig`` の一括検証で確かめる。
    """
    optimizer: MpcOptimizerConfig

    @model_validator(mode="after")
    def _budget_fits_period(self) -> Self:
        if self.budget_ms > self.period_ms:
            raise ValueError("mpc.budget_ms は mpc.period_ms 以下にする")
        if self.valid_ms < self.period_ms:
            # 再計算の周期より短い有効期限では、健全な提案でも毎 tick 期限切れになる。
            raise ValueError("mpc.valid_ms は mpc.period_ms 以上にする")
        if self.max_source_age_ms.value > self.valid_ms:
            # 受信起点の期限より長い元 snapshot 起点の期限は、何も塞がない（0077 §2.4 の4）。
            raise ValueError(
                "mpc.max_source_age_ms は mpc.valid_ms 以下にする: "
                f"max_source_age_ms={self.max_source_age_ms.value}; valid_ms={self.valid_ms}"
            )
        return self


class SupervisorWeightRange(_ConfigModel):
    """RL action を validated config 内へ閉じ込める weight の範囲。"""

    minimum: UnitInterval
    maximum: UnitInterval

    @model_validator(mode="after")
    def _minimum_does_not_exceed_maximum(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("Supervisor weight range は minimum <= maximum にする")
        return self

    def contains(self, value: float) -> bool:
        """weight が設定済みの閉区間にあるか返す。"""
        return self.minimum <= value <= self.maximum


class SupervisorWeightBounds(_ConfigModel):
    """#89 が変更できる各 objective weight の範囲。"""

    gpu_temperature: SupervisorWeightRange
    cpu_temperature: SupervisorWeightRange
    balance: SupervisorWeightRange
    acoustic: SupervisorWeightRange
    change: SupervisorWeightRange


class SupervisorOutputBounds(_ConfigModel):
    """Rule / RL の出力を検証済み戦略・target・weightへ制限する。"""

    strategies: Annotated[
        tuple[str, ...], BeforeValidator(_yaml_sequence_to_tuple), Field(min_length=1)
    ]
    target_bands: Annotated[
        tuple[SupervisorTargetBand, ...],
        BeforeValidator(_yaml_sequence_to_tuple),
        Field(min_length=1),
    ]
    weights: SupervisorWeightBounds

    @model_validator(mode="after")
    def _choices_are_unique_and_well_formed(self) -> Self:
        if len(set(self.strategies)) != len(self.strategies):
            raise ValueError("Supervisor strategy は重複させない")
        for strategy in self.strategies:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", strategy):
                raise ValueError(f"Supervisor strategy の形式が不正: {strategy!r}")
        serialized_bands = [band.model_dump_json() for band in self.target_bands]
        if len(set(serialized_bands)) != len(serialized_bands):
            raise ValueError("Supervisor target band は重複させない")
        return self

    def validate_context(
        self,
        *,
        strategy: str,
        weights: SupervisorObjectiveWeights,
        target_band: SupervisorTargetBand,
    ) -> None:
        """policy context が config の許可範囲外なら拒否する。"""
        if strategy not in self.strategies:
            raise ValueError(f"Supervisor strategy は設定済み候補から選ぶ: {strategy}")
        if target_band not in self.target_bands:
            raise ValueError("Supervisor target band は設定済み候補から選ぶ")
        for name in ("gpu_temperature", "cpu_temperature", "balance", "acoustic", "change"):
            if not getattr(self.weights, name).contains(getattr(weights, name)):
                raise ValueError(f"Supervisor weight が設定範囲外: {name}")


class SupervisorPolicyContext(_ConfigModel):
    """RulePolicy が1 workload regimeに対応づける運転戦略。"""

    strategy: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    weights: SupervisorObjectiveWeights
    target_band: SupervisorTargetBand


class WorkloadPolicyContexts(_ConfigModel):
    """UNKNOWNを通常状態に倒さず、全 regime の Rule context を必須にする。"""

    idle: SupervisorPolicyContext
    transient_cpu: SupervisorPolicyContext
    transient_gpu: SupervisorPolicyContext
    transient_cpu_gpu: SupervisorPolicyContext
    """CPU / GPU 同時 burst（決定記録 0036）。値は config で与え、既定値は持たない。"""
    sustained_cpu: SupervisorPolicyContext
    sustained_gpu: SupervisorPolicyContext
    sustained_cpu_gpu: SupervisorPolicyContext
    cooldown: SupervisorPolicyContext
    unknown: SupervisorPolicyContext

    def get(self, regime: WorkloadRegime) -> SupervisorPolicyContext:
        """列挙値に対応する設定を返す。"""
        return cast(SupervisorPolicyContext, getattr(self, regime.value))


class RulePolicyConfig(_ConfigModel):
    """決定論的な初期 active / RL fallback policy。"""

    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    contexts: WorkloadPolicyContexts


class SupervisorConfig(_ConfigModel):
    """Supervisor timing・selection・出力範囲を一括で検証する。"""

    period_ms: PositiveMilliseconds
    valid_ms: PositiveMilliseconds
    active_policy: Annotated[SupervisorPolicyKind, BeforeValidator(_yaml_supervisor_policy)]
    shadow_policy: Annotated[SupervisorPolicyKind | None, BeforeValidator(_yaml_supervisor_policy)]
    rl_version: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        max_length=120,
    )
    output_bounds: SupervisorOutputBounds
    rule_policy: RulePolicyConfig

    @model_validator(mode="after")
    def _selection_and_contexts_are_consistent(self) -> Self:
        if self.valid_ms < self.period_ms:
            raise ValueError("supervisor.valid_ms は period_ms 以上にする")
        if self.shadow_policy is not None and self.shadow_policy is not SupervisorPolicyKind.RL:
            raise ValueError("shadow policy に指定できるのは RLPolicy だけ")
        if self.shadow_policy is self.active_policy:
            raise ValueError("active と shadow に同じ Supervisor policy を指定しない")
        uses_rl = self.active_policy is SupervisorPolicyKind.RL or self.shadow_policy is not None
        if uses_rl and self.rl_version is None:
            raise ValueError("RLPolicy を使う設定には rl_version が必要")
        for regime in WorkloadRegime:
            context = self.rule_policy.contexts.get(regime)
            self.output_bounds.validate_context(
                strategy=context.strategy,
                weights=context.weights,
                target_band=context.target_band,
            )
        return self


class WorkloadPowerBand(_ConfigModel):
    """Workload activity の Schmitt trigger を signal ごとに設定する。"""

    metric: str
    idle_below_w: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    active_above_w: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]

    @field_validator("metric")
    @classmethod
    def _metric_is_stored_telemetry(cls, value: str) -> str:
        return validate_metric(value)

    @model_validator(mode="after")
    def _activity_has_a_deadband(self) -> Self:
        if self.active_above_w <= self.idle_below_w:
            raise ValueError("active_above_w は idle_below_w より大きくする")
        return self


class WorkloadRegimeConfig(_ConfigModel):
    """#87 の観測窓・遷移時間・Power 閾値。将来時間の予測値は持たない。"""

    cpu_power: WorkloadPowerBand
    gpu_power: WorkloadPowerBand
    activity_window_ms: PositiveMilliseconds
    history_window_ms: PositiveMilliseconds
    minimum_observation_ms: PositiveMilliseconds
    sustained_after_ms: PositiveMilliseconds
    cooldown_ms: PositiveMilliseconds
    minimum_transition_ms: PositiveMilliseconds
    confidence_full_window_ms: PositiveMilliseconds
    max_snapshot_gap_ms: PositiveMilliseconds

    @model_validator(mode="after")
    def _windows_support_every_duration(self) -> Self:
        if self.cpu_power.metric == self.gpu_power.metric:
            raise ValueError("CPU / GPU workload Power metric は別々にする")
        bounded = {
            "activity_window_ms": self.activity_window_ms,
            "minimum_observation_ms": self.minimum_observation_ms,
            "sustained_after_ms": self.sustained_after_ms,
            "cooldown_ms": self.cooldown_ms,
            "minimum_transition_ms": self.minimum_transition_ms,
            "confidence_full_window_ms": self.confidence_full_window_ms,
        }
        too_long = [name for name, value in bounded.items() if value > self.history_window_ms]
        if too_long:
            raise ValueError(
                f"workload regime の期間は history_window_ms 以下にする: {sorted(too_long)}"
            )
        if self.max_snapshot_gap_ms > self.minimum_observation_ms:
            raise ValueError("max_snapshot_gap_ms は minimum_observation_ms 以下にする")
        required_for_sustained = (
            self.minimum_observation_ms + self.sustained_after_ms + self.minimum_transition_ms
        )
        if required_for_sustained > self.history_window_ms:
            raise ValueError("history_window_ms は観測・SUSTAINED判定・遷移確認の合計以上にする")
        required_for_cooldown = (
            self.minimum_observation_ms + self.cooldown_ms + self.minimum_transition_ms
        )
        if required_for_cooldown > self.history_window_ms:
            raise ValueError("history_window_ms は観測・COOLDOWN判定・遷移確認の合計以上にする")
        return self


class ShadowConfig(_ConfigModel):
    """Shadow Mode の記録設定（#90 / 決定記録 0053）。

    記録の**量**と**照合の幅**だけを持つ。何を counterfactual にするかは authority stage と
    Gate の判断で決まるので、ここには置かない。値は実測前の暫定値として扱う。
    """

    enabled: bool
    """counterfactual を decision trace へ残すか。制御の挙動は変えない。"""
    outcome_match_tolerance_ms: PolicyMilliseconds
    """予測時刻と実測時刻のずれの許容幅。Dataset の target 選択（0031 §2.2）と同じ規則で使う。"""
    applied_demand_tolerance: PolicyDemand
    """予測した候補 action が「実際に掛かっていた」とみなす zone ごとの demand の許容幅。

    counterfactual の予測を**採点してよいのは、その plan が実際に実行された区間だけ**である
    （決定記録 0053 §2.3）。別の値が掛かっていた区間の実測と引き算しても、出てくるのは
    制御器の違いとモデル誤差が混ざった量になる。
    """

    @model_validator(mode="after")
    def _tolerance_keeps_the_check_meaningful(self) -> Self:
        if self.applied_demand_tolerance.value >= 1.0:
            # demand の全域を許すと、どんな適用値も「plan どおり」になり判定が意味を失う。
            raise ValueError("shadow.applied_demand_tolerance は 1.0 未満にする")
        return self


def _yaml_coordination_mode(value: object) -> object:
    """YAML の ``mode`` を列挙値に正規化する。

    YAML 1.1 は引用符の無い ``off`` を真偽値の偽として読む（``yaml.safe_load``）。
    0078 §2.4 の雛形どおり ``mode: off`` と書いた設定を拒否しないよう、**偽だけ**を ``off`` と読む。
    真（``on`` / ``yes`` / ``true``）は ``shadow`` / ``apply`` のどちらか決められないので拒否する
    （協調を開く向きの値を推測しない）。
    """
    if value is False:
        return AirBalanceCoordinationMode.OFF
    return AirBalanceCoordinationMode(value) if isinstance(value, str) else value


class AirBalanceCoordinationConfig(_ConfigModel):
    """Baseline（Fallback）の requested への Air Balance の協調の方針（#81 / 決定記録 0078 §2.4）。

    有効化は ``mode`` を人が ``off`` → ``shadow`` → ``apply`` と設定して再起動するだけで進む
    （0078 §2.8。authority journal を経ない）。Air Balance そのものの有効・無効は
    ``air-balance.yaml`` の ``source.status`` が決め、ここでは決めない（0073 §2.2）。
    """

    mode: Annotated[AirBalanceCoordinationMode, BeforeValidator(_yaml_coordination_mode)]
    max_raise: PerZone[PolicyDemand]
    """zone ごとに協調が raw baseline から上げてよい幅（demand）。0 はその zone を動かさない。"""
    release_hold_ms: ConfigValue[NonNegativeMilliseconds]
    """引き上げ幅が消えた・縮んだ後も、直近の最大の幅を保つ時間（単調時計）。0 は保持しない。"""


class FanPolicyConfig(_ConfigModel):
    schema_version: Literal[11]
    fallback_curve: Annotated[
        tuple[FallbackPoint, ...], BeforeValidator(_yaml_sequence_to_tuple), Field(min_length=2)
    ]
    fallback_temperature_inputs: PerZone[FallbackTemperatureInputs]
    fallback_power_feedforward: PerZone[FallbackPowerCurve] | None = None
    fallback_dynamics: FallbackDynamics
    reactive_guard: ReactiveGuardConfig
    mpc: MpcTiming
    supervisor: SupervisorConfig
    workload_regime: WorkloadRegimeConfig
    gate_min_confidence: GateConfidenceThresholds
    model_confidence: ModelConfidencePolicy
    authority_stage: Annotated[AuthorityStage, BeforeValidator(_yaml_authority_stage)]
    """設定が許す authority の**上限**（#92 / 決定記録 0057 §2.2）。

    v9 より前は「いまの stage」だったが、実効 stage は journal が持つようになった。
    ここを下げれば実効 stage は次の tick から下がり、上げても journal は上がらない。
    """
    authority_limits: AuthorityLimits
    authority_rollout: AuthorityRolloutConfig
    shadow: ShadowConfig
    air_balance_coordination: AirBalanceCoordinationConfig
    """v10（#81 / 決定記録 0078 §2.4）。**必須。** v9 以前を補完しない。

    v11（#86 / 決定記録 0077 §2.4）は ``mpc.max_source_age_ms`` を足した。v10 以前を補完しない。
    """
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
        if self.model_confidence.high_min_confidence.value < self.gate_min_confidence.full.value:
            # HIGH の下限が stage の最低 confidence より低いと、MEDIUM を経ずに
            # Fallback 境界の直上で帯なしの authority を得てしまう。
            raise ValueError(
                "model_confidence.high_min_confidence は gate_min_confidence.full 以上にする"
            )
        if self.shadow.outcome_match_tolerance_ms.value >= self.mpc.optimizer.step_ms.value:
            # 許容幅が1 step に届くと、別の step の実測を「その予測が当たった証拠」に数える。
            # ResidualDriftMonitor が最短 horizon に課す条件（0050 §2.2）と同じ理由。
            raise ValueError(
                "shadow.outcome_match_tolerance_ms は mpc.optimizer.step_ms より小さくする"
            )
        return self


class ConfigSource(_ConfigModel):
    """decision trace に残せる入力の版・名前・内容ハッシュ。絶対 path は残さない。"""

    name: ConfigFileName
    schema_version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ValidatedFanHardwareDocument:
    """同じ ``fan-hardware.yaml`` bytes から検証値と provenance を作った束。"""

    __slots__ = ("_config", "_source")
    _config: FanHardwareConfig
    _source: ConfigSource

    def __init__(self) -> None:
        raise TypeError("ValidatedFanHardwareDocument は trusted loader からだけ取得する")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ValidatedFanHardwareDocument は不変")

    @classmethod
    def _from_bytes(cls, payload: bytes) -> ValidatedFanHardwareDocument:
        try:
            text = payload.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("fan-hardware.yaml はUTF-8にする") from exc
        loaded: Any = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError("制御設定が辞書ではない: fan-hardware.yaml")
        config = FanHardwareConfig.model_validate(loaded)
        source = ConfigSource(
            name="fan-hardware.yaml",
            schema_version=config.schema_version,
            sha256=sha256(payload).hexdigest(),
        )
        document = object.__new__(cls)
        object.__setattr__(document, "_config", config)
        object.__setattr__(document, "_source", source)
        return document

    def _binding_material(self) -> tuple[FanHardwareConfig, ConfigSource]:
        return self._config, self._source


def load_fan_hardware_document(path: Path) -> ValidatedFanHardwareDocument:
    """hardware-only fail-safe 起動用に、1回読んだ bytes を検証してhashする。"""
    if path.name != CONFIG_FILENAMES["fan_hardware"]:
        raise ValueError("emergency hardware source は fan-hardware.yaml に固定する")
    return ValidatedFanHardwareDocument._from_bytes(path.read_bytes())


class ConfigSources(_ConfigModel):
    fan_hardware: ConfigSource
    safety: ConfigSource
    policy: ConfigSource
    air_balance: ConfigSource


class ControlConfig(_ConfigModel):
    """一括で検証済みの制御設定と、再現用の入力情報。"""

    fan_hardware: FanHardwareConfig
    safety: SafetyConfig
    policy: FanPolicyConfig
    air_balance: AirBalanceConfig
    """Air Balance の characterization（決定記録 0033 / 0073）。

    **``source.status: uncalibrated`` でも検証は通す。** そのときは Air Balance を無効にして
    起動し、未校正の曲線・比・熱の閾値を requested にも trace の推定値にも入れない
    （0073 §2.2。``air_balance_enabled``）。
    """
    sources: ConfigSources

    @model_validator(mode="after")
    def _source_metadata_matches_validated_documents(self) -> Self:
        expected = (
            (self.sources.fan_hardware, "fan-hardware.yaml", self.fan_hardware.schema_version),
            (self.sources.safety, "safety.yaml", self.safety.schema_version),
            (self.sources.policy, "fan-policy.yaml", self.policy.schema_version),
            (self.sources.air_balance, "air-balance.yaml", self.air_balance.schema_version),
        )
        for source, name, version in expected:
            if source.name != name or source.schema_version != version:
                raise ValueError(f"ConfigSource が検証済み設定と一致しない: {name}")
        return self

    @model_validator(mode="after")
    def _coordination_requires_calibrated_air_balance(self) -> Self:
        # 協調を掛けたつもりで掛かっていない構成を、黙って off と読まない（決定記録 0078 §2.4）。
        # 不正として全 zone Max（config_invalid）に倒し、置き忘れと同じく
        # 大きな音で気付ける側にする。
        mode = self.policy.air_balance_coordination.mode
        if mode is not AirBalanceCoordinationMode.OFF and not self.air_balance.calibrated:
            raise ValueError(
                "air_balance_coordination.mode が shadow / apply のときは "
                "air-balance.yaml の source.status を calibrated にする（止めるなら mode: off）"
            )
        return self

    @model_validator(mode="after")
    def _mpc_source_age_covers_a_healthy_round_trip(self) -> Self:
        # 往き（元 snapshot から frame が worker へ届くまでの最大1 tick・worker が最後の frame を
        # 拾うまでの最大1周期・optimizer の予算）と還り（poll の直後に届いた結果が次の tick の
        # poll まで待つ最大1 tick）を足した値より短いと、健全な worker の提案も期限切れになりうる。
        # Learned が黙って一度も使われない構成を起動時に見せる（決定記録 0077 §2.4 の4）。
        mpc = self.policy.mpc
        required = 2 * self.safety.tick_ms.value + mpc.period_ms + mpc.budget_ms
        if mpc.max_source_age_ms.value < required:
            raise ValueError(
                "mpc.max_source_age_ms は 2 * safety.tick_ms + mpc.period_ms + mpc.budget_ms "
                f"以上にする: max_source_age_ms={mpc.max_source_age_ms.value}; "
                f"required>={required}"
            )
        return self

    @property
    def air_balance_enabled(self) -> bool:
        """Air Balance を推定・記録に使うか（決定記録 0073 §2.2）。

        ``source.status`` だけで決まる。
        """
        return self.air_balance.calibrated

    @classmethod
    def from_directory(cls, directory: Path) -> ControlConfig:
        """四つ全てを読んでから生成する。1つでも無い・不正なら返さない。

        ``air-balance.yaml`` が無いことを「Air Balance 無し」と読み替えない
        （決定記録 0073 §2.2。置き忘れを黙って部分適用にしない）。
        """
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
                    name=cast(ConfigFileName, filename),
                    schema_version=schema_version,
                    sha256=sha256(text.encode("utf-8")).hexdigest(),
                ),
            )
        return cls(
            fan_hardware=FanHardwareConfig.model_validate(contents["fan_hardware"][0]),
            safety=SafetyConfig.model_validate(contents["safety"][0]),
            policy=FanPolicyConfig.model_validate(contents["policy"][0]),
            air_balance=AirBalanceConfig.model_validate(contents["air_balance"][0]),
            sources=ConfigSources(
                fan_hardware=contents["fan_hardware"][1],
                safety=contents["safety"][1],
                policy=contents["policy"][1],
                air_balance=contents["air_balance"][1],
            ),
        )

    def runtime_digest(self) -> ControlConfigDigest:
        """trace の `runtime.config` と frame の `config` に載せる値（決定記録 0073 §2.5 (c)）。

        4ファイルの版と SHA-256 を `sources` から写す（手で書かない）。fand の loop と Learned MPC
        worker が**同じ関数**で作り、worker は frame の値と照合する
        （0077 §2.3 の `config_mismatch`）。
        """
        sources = self.sources
        return ControlConfigDigest(
            fan_hardware_sha256=sources.fan_hardware.sha256,
            safety_sha256=sources.safety.sha256,
            policy_sha256=sources.policy.sha256,
            air_balance_sha256=sources.air_balance.sha256,
            control_config_version=CONTROL_CONFIG_VERSION,
            fan_hardware_schema_version=sources.fan_hardware.schema_version,
            safety_schema_version=sources.safety.schema_version,
            policy_schema_version=sources.policy.schema_version,
            air_balance_schema_version=sources.air_balance.schema_version,
        )

    def trace_metadata(self) -> dict[str, object]:
        """#82 の decision trace payload へ足せる再現情報（4ファイルの名前・版・SHA-256）。"""
        return {
            "control_config_version": CONTROL_CONFIG_VERSION,
            "control_config": self.sources.model_dump(mode="json"),
        }

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
        append(
            "safety.yaml",
            "write_fail_emergency_after",
            safety.write_fail_emergency_after,
        )
        append("safety.yaml", "ramp_down_per_s", safety.ramp_down_per_s)
        append("safety.yaml", "startup_settle_ms", safety.startup_settle_ms)
        append("safety.yaml", "fault_clear_hold_ms", safety.fault_clear_hold_ms)
        append("safety.yaml", "tick_ms", safety.tick_ms)
        append("safety.yaml", "tick_deadline_ms", safety.tick_deadline_ms)
        append("safety.yaml", "overrun_consecutive_limit", safety.overrun_consecutive_limit)
        append("safety.yaml", "watchdog_timeout_ms", safety.watchdog_timeout_ms)
        append("safety.yaml", "hardware_write_fail_exit_ms", safety.hardware_write_fail_exit_ms)
        for zone in Zone:
            append(
                "safety.yaml",
                f"zone_min_demand.{zone.value}",
                safety.zone_min_demand.get(zone),
            )
            append(
                "safety.yaml",
                f"stall_check_min_demand.{zone.value}",
                safety.stall_check_min_demand.get(zone),
            )
            append(
                "safety.yaml",
                f"stall_min_rpm.{zone.value}",
                safety.stall_min_rpm.get(zone),
            )
        for index, point in enumerate(safety.cpu_cooling_floor):
            append("safety.yaml", f"cpu_cooling_floor[{index}].temperature_c", point.temperature_c)
            append("safety.yaml", f"cpu_cooling_floor[{index}].demand", point.demand)
        for index, power_point in enumerate(safety.cpu_power_cooling_floor):
            append("safety.yaml", f"cpu_power_cooling_floor[{index}].power_w", power_point.power_w)
            append("safety.yaml", f"cpu_power_cooling_floor[{index}].demand", power_point.demand)
        for name in ("cpu_ms", "cpu_power_ms", "gpu_ms", "air_ms", "air_sensor_period_ms"):
            append("safety.yaml", f"telemetry.{name}", getattr(safety.telemetry, name))
        append("safety.yaml", "telemetry.t_sensor.enabled", safety.telemetry.t_sensor.enabled)
        if safety.telemetry.t_sensor.stale_after_ms is not None:
            append(
                "safety.yaml",
                "telemetry.t_sensor.stale_after_ms",
                safety.telemetry.t_sensor.stale_after_ms,
            )
        if safety.telemetry.t_sensor.absolute_ceiling_c is not None:
            append(
                "safety.yaml",
                "telemetry.t_sensor.absolute_ceiling_c",
                safety.telemetry.t_sensor.absolute_ceiling_c,
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
        confidence = self.policy.model_confidence
        for name in (
            "high_min_confidence",
            "range_margin",
            "min_support_count",
            "full_support_count",
            "min_missing_pattern_count",
            "residual_window",
            "residual_min_samples",
            "residual_match_tolerance_ms",
            "residual_max_age_ms",
            "residual_drift_ood_ratio",
            "cap_without_uncertainty",
            "cap_before_residual_evidence",
        ):
            append("fan-policy.yaml", f"model_confidence.{name}", getattr(confidence, name))
        append(
            "fan-policy.yaml",
            "model_confidence.medium_limit.limit_up",
            confidence.medium_limit.limit_up,
        )
        append(
            "fan-policy.yaml",
            "model_confidence.medium_limit.limit_down",
            confidence.medium_limit.limit_down,
        )
        append("fan-policy.yaml", "mpc.max_source_age_ms", self.policy.mpc.max_source_age_ms)
        optimizer = self.policy.mpc.optimizer
        for name in (
            "horizon_ms",
            "step_ms",
            "candidate_levels",
            "sweeps",
            "max_evaluations",
            "max_step_up",
            "max_step_down",
            "unknown_balance_cost",
        ):
            append("fan-policy.yaml", f"mpc.optimizer.{name}", getattr(optimizer, name))
        for name in ("temperature_c", "balance_ratio", "acoustic_cost", "demand_change"):
            append(
                "fan-policy.yaml",
                f"mpc.optimizer.cost_scales.{name}",
                getattr(optimizer.cost_scales, name),
            )
        rollout = self.policy.authority_rollout
        for name in (
            "approval_max_age_ms",
            "evidence_max_age_ms",
            "unhealthy_window_ms",
            "low_confidence_after",
            "ood_after",
        ):
            append("fan-policy.yaml", f"authority_rollout.{name}", getattr(rollout, name))
        append(
            "fan-policy.yaml",
            "shadow.outcome_match_tolerance_ms",
            self.policy.shadow.outcome_match_tolerance_ms,
        )
        append(
            "fan-policy.yaml",
            "shadow.applied_demand_tolerance",
            self.policy.shadow.applied_demand_tolerance,
        )
        coordination = self.policy.air_balance_coordination
        for zone in Zone:
            append(
                "fan-policy.yaml",
                f"air_balance_coordination.max_raise.{zone.value}",
                coordination.max_raise.get(zone),
            )
        append(
            "fan-policy.yaml",
            "air_balance_coordination.release_hold_ms",
            coordination.release_hold_ms,
        )
        for zone in Zone:
            bound = optimizer.zone_bounds.get(zone)
            append("fan-policy.yaml", f"mpc.optimizer.zone_bounds.{zone.value}.floor", bound.floor)
            append(
                "fan-policy.yaml",
                f"mpc.optimizer.zone_bounds.{zone.value}.ceiling",
                bound.ceiling,
            )
        return tuple(values)

    @property
    def actuation_permitted(self) -> bool:
        """実測で確認済みの hardware mapping だけを後続の書込み層へ渡す。"""
        return self.fan_hardware.approval.status == "confirmed"
