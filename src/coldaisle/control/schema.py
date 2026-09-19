"""Fan 制御の層間で受け渡す型。#76

決定記録 0028 §2.3 の入出力と、§2.4 の合成結果が満たす条件を**型の不変条件として**持つ。

守る線は3つ。

1. **上位層（制御器・Reactive Guard・Critical Safety）の型は PWM を持たない。**
   扱うのは demand（`0.0..1.0`）だけで、`pwm` を渡すと拒否する（`extra="forbid"`）。
   PWM の指令を作れるのは Hardware Backend（#77）だけ（0028 §2.3「型で経路を縛る」）
2. **requested と effective が違えば、理由が必ず残る**（`EffectiveZoneDemand`）
3. **Front / Rear / Top は同じ型を使う**（`PerZone`）

合成の計算そのもの（0028 §2.4）は Critical Safety（#78）が持つ。
ここは結果が満たすべき条件だけを検査する。
Supervisor の出力は #88 で本moduleに追加した。Telemetry snapshot は #102 の独立moduleが持つ。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal[5] = 5
"""`ControlTick` の形の版。**フィールドの名前や意味を変えたら上げる。**

#82 が保存したデータを読み違えないため。

- v2（#87 / #133）: workload regime と regime confidence
- v3（#88 / #137）: Supervisor decision
- v4（#78）: fault code `absolute_temperature_limit` を追加し、Top の `enable_reverted` を
  無条件の `EMERGENCY` にした。保存済みの v1〜v3 は v3 までの規則のまま読める
- v5（#85）: Model Confidence / OOD と authority の判断（`model_gate`）。Learned MPC を
  active にした tick には必須。保存済みの v1〜v4 はそのまま読める
"""

Demand = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
"""制御の内部単位（0027 §2.1）。**NaN / 無限大は拒否する**（0028 §2.1）。"""

HWMON_PWM_MAX = 255
"""hwmon の ABI が定める `pwmN` の最大値。調整する値ではない。"""


class _Frozen(BaseModel):
    # strict: 文字列の "0.5" や真偽値を demand として黙って受け入れない
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Zone(StrEnum):
    """制御する系統（決定記録 0026）。AIO Pump は含まない。"""

    FRONT = "front"
    REAR = "rear"
    TOP = "top"


class PerZone[T](_Frozen):
    """Front / Rear / Top で同じ型を持つ入れ物。**3つとも必須**で、ほかの zone は持てない。"""

    front: T
    rear: T
    top: T

    def get(self, zone: Zone) -> T:
        """zone の値を返す。"""
        return {Zone.FRONT: self.front, Zone.REAR: self.rear, Zone.TOP: self.top}[zone]


class Reason(_Frozen):
    """判断の理由。`code` は機械が集計する識別子、`detail` は人が読む説明。"""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    detail: str = Field(default="", max_length=500)


class OperatingMode(StrEnum):
    """運転モード。**人だけが変える**（0028 §2.5 (a)）。"""

    AUTO = "auto"
    MANUAL = "manual"
    MAX = "max"
    CALIBRATION = "calibration"


class AuthorityStage(StrEnum):
    """ML の提案に与える制御権の上限。**昇格は人だけ**（0028 §2.5 (b)）。"""

    SHADOW = "shadow"
    LIMITED = "limited"
    EXPANDED = "expanded"
    FULL = "full"


class ControllerKind(StrEnum):
    """requested を作る制御器（0028 §2.5 (c)）。"""

    FALLBACK = "fallback"
    LEARNED_MPC = "learned_mpc"


class SafetyState(StrEnum):
    """Critical Safety が決める状態（0028 §2.5 (d)）。"""

    STARTUP = "startup"
    NORMAL = "normal"
    DEGRADED = "degraded"
    EMERGENCY = "emergency"


class OptimizerStatus(StrEnum):
    """Learned MPC の計算の結果（0028 §2.6）。"""

    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"


class WorkloadRegime(StrEnum):
    """観測済み Telemetry から推定する現在の負荷区分。#87。"""

    IDLE = "idle"
    TRANSIENT_CPU = "transient_cpu"
    TRANSIENT_GPU = "transient_gpu"
    TRANSIENT_CPU_GPU = "transient_cpu_gpu"
    """CPU と GPU の両軸が active で、少なくとも一方が SUSTAINED 未満（決定記録 0036）。"""
    SUSTAINED_CPU = "sustained_cpu"
    SUSTAINED_GPU = "sustained_gpu"
    SUSTAINED_CPU_GPU = "sustained_cpu_gpu"
    COOLDOWN = "cooldown"
    UNKNOWN = "unknown"


class SupervisorPolicyKind(StrEnum):
    """Supervisor の実装種別。旧 trace の文字列表現も維持する。"""

    RULE = "rule_policy"
    RL = "rl_policy"


SupervisorWeight = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
TemperatureC = Annotated[float, Field(allow_inf_nan=False)]


class SupervisorObjectiveWeights(_Frozen):
    """Learned MPC が任意 context として使う、多目的最適化の相対 weight。"""

    gpu_temperature: SupervisorWeight
    cpu_temperature: SupervisorWeight
    balance: SupervisorWeight
    acoustic: SupervisorWeight
    change: SupervisorWeight

    @model_validator(mode="after")
    def _at_least_one_objective_is_enabled(self) -> Self:
        if not any(
            value > 0.0
            for value in (
                self.gpu_temperature,
                self.cpu_temperature,
                self.balance,
                self.acoustic,
                self.change,
            )
        ):
            raise ValueError("Supervisor objective weight は少なくとも1つを正にする")
        return self


class TemperatureTarget(_Frozen):
    """安全上限ではない、Supervisor が選ぶ運転上の温度 target band。"""

    lower_c: TemperatureC
    upper_c: TemperatureC

    @model_validator(mode="after")
    def _lower_is_below_upper(self) -> Self:
        if self.lower_c >= self.upper_c:
            raise ValueError("target band は lower_c < upper_c にする")
        return self


class SupervisorTargetBand(_Frozen):
    """CPU / GPU 双方の運転 target。Critical Safety の閾値とは独立。"""

    cpu_temperature: TemperatureTarget
    gpu_temperature: TemperatureTarget


class SupervisorOutput(_Frozen):
    """Supervisor の versioned output。Demand / PWM / hardware 指令は表現できない。"""

    schema_version: Literal[1] = 1
    snapshot_schema_version: int = Field(ge=1)
    tick_id: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    policy: SupervisorPolicyKind
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    regime: WorkloadRegime
    regime_confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    weights: SupervisorObjectiveWeights
    strategy: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    target_band: SupervisorTargetBand
    computed_at_ms: int = Field(ge=0)


class SupervisorPolicyEvaluation(_Frozen):
    """1 policy の成功出力または構造化された失敗を decision trace に残す。"""

    policy: SupervisorPolicyKind
    output: SupervisorOutput | None = None
    error: Reason | None = None
    received_monotonic_ms: int | None = Field(default=None, ge=0)
    source_monotonic_ms: int | None = Field(default=None, ge=0)
    """RL output の元 snapshot の単調時刻（control loop 自身の時計）。鮮度はここから数える。"""

    @model_validator(mode="after")
    def _contains_exactly_one_result(self) -> Self:
        if (self.output is None) == (self.error is None):
            raise ValueError("Supervisor evaluation は output または error の片方だけを持つ")
        if self.output is not None and self.output.policy is not self.policy:
            raise ValueError("Supervisor evaluation の policy と output が一致しない")
        if self.policy is SupervisorPolicyKind.RULE and (
            self.received_monotonic_ms is not None or self.source_monotonic_ms is not None
        ):
            raise ValueError("inline RulePolicy に worker の受信時刻・元 snapshot 時刻を付けない")
        if self.policy is SupervisorPolicyKind.RL and self.output is not None:
            if self.received_monotonic_ms is None:
                raise ValueError("RLPolicy output には control loop の受信単調時刻が必要")
            if self.source_monotonic_ms is None:
                raise ValueError("RLPolicy output には元 snapshot の単調時刻が必要")
        if (
            self.received_monotonic_ms is not None
            and self.source_monotonic_ms is not None
            and self.source_monotonic_ms > self.received_monotonic_ms
        ):
            raise ValueError("元 snapshot の単調時刻を受信単調時刻より後にしない")
        return self


class SupervisorDecision(_Frozen):
    """active / shadow と Rule fallback を同じ入力単位で記録する。"""

    schema_version: Literal[1] = 1
    tick_id: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    snapshot_schema_version: int = Field(ge=1)
    active: SupervisorPolicyEvaluation
    fallback: SupervisorPolicyEvaluation | None = None
    shadow: SupervisorPolicyEvaluation | None = None

    @property
    def selected_output(self) -> SupervisorOutput | None:
        """MPC が optional context として使える active または Rule fallback 出力。"""
        if self.active.output is not None:
            return self.active.output
        if self.fallback is not None:
            return self.fallback.output
        return None

    @model_validator(mode="after")
    def _runs_share_one_state_and_shadow_never_becomes_active(self) -> Self:
        active_failed = self.active.output is None
        if self.active.policy is SupervisorPolicyKind.RL and active_failed:
            if self.fallback is None or self.fallback.policy is not SupervisorPolicyKind.RULE:
                raise ValueError("active RLPolicy の失敗時は RulePolicy fallback を記録する")
        elif self.fallback is not None:
            raise ValueError("Rule fallback は active RLPolicy が失敗したときだけ記録する")
        if self.shadow is not None and self.shadow.policy is self.active.policy:
            raise ValueError("active と shadow に同じ Supervisor policy を指定しない")

        outputs = tuple(
            evaluation.output
            for evaluation in (self.active, self.fallback, self.shadow)
            if evaluation is not None and evaluation.output is not None
        )
        for output in outputs:
            # RL は worker で非同期に推論するため、元 snapshot が過去の tick でもよい（0028 §2.2）。
            # 元 tick の識別子はそのまま残し、未来の tick だけを拒否する。
            if output.tick_id > self.tick_id:
                raise ValueError("Supervisor output の tick_id を decision より未来にしない")
            if output.policy is SupervisorPolicyKind.RULE and output.tick_id != self.tick_id:
                raise ValueError("inline RulePolicy output の tick_id を decision と揃える")
            if output.tick_id == self.tick_id and output.ts_ms != self.ts_ms:
                raise ValueError("Supervisor output の ts_ms を decision と揃える")
            if output.snapshot_schema_version != self.snapshot_schema_version:
                raise ValueError("Supervisor output の snapshot schema version を揃える")
        if outputs:
            first = outputs[0]
            if any(output.regime is not first.regime for output in outputs[1:]):
                raise ValueError("active / fallback / shadow は同じ workload regime を使う")
            current = [output for output in outputs if output.tick_id == self.tick_id]
            if any(
                output.regime_confidence != current[0].regime_confidence for output in current[1:]
            ):
                raise ValueError("同じ tick の Supervisor output は同じ workload regime を使う")
        return self


class BoundBy(StrEnum):
    """effective を決めた項（0028 §2.4）。"""

    FORCED_MAX = "forced_max"
    SAFETY_FLOOR = "safety_floor"
    RAMP_DOWN = "ramp_down"
    GUARD_FLOOR = "guard_floor"
    GUARD_CEILING = "guard_ceiling"
    REQUESTED = "requested"


BOUND_BY_PRECEDENCE: tuple[BoundBy, ...] = (
    BoundBy.FORCED_MAX,
    BoundBy.SAFETY_FLOOR,
    BoundBy.RAMP_DOWN,
    BoundBy.GUARD_FLOOR,
    BoundBy.GUARD_CEILING,
    BoundBy.REQUESTED,
)
"""複数の項が同じ値になったとき、記録する項の優先順。**安全側が先**（0028 §2.4）。"""


class FaultCode(StrEnum):
    """Critical Safety が扱う故障（0028 §2.7）。"""

    CPU_TELEMETRY_STALE = "cpu_telemetry_stale"
    GPU_TELEMETRY_STALE = "gpu_telemetry_stale"
    T_SENSOR_STALE = "t_sensor_stale"
    AIR_TELEMETRY_STALE = "air_telemetry_stale"
    ABSOLUTE_TEMPERATURE_LIMIT = "absolute_temperature_limit"
    TACH_STALL = "tach_stall"
    WRITE_FAILURE = "write_failure"
    READBACK_MISMATCH = "readback_mismatch"
    ENABLE_REVERTED = "enable_reverted"
    FALLBACK_EXCEPTION = "fallback_exception"
    GUARD_EXCEPTION = "guard_exception"
    TICK_OVERRUN = "tick_overrun"
    CONFIG_INVALID = "config_invalid"


ZONE_FAULTS: frozenset[FaultCode] = frozenset(
    {
        FaultCode.TACH_STALL,
        FaultCode.WRITE_FAILURE,
        FaultCode.READBACK_MISMATCH,
        FaultCode.ENABLE_REVERTED,
    }
)
"""特定の zone のファンで起きる故障。これ以外は zone に紐づかない（Telemetry・設定・ループ）。"""


class Fault(_Frozen):
    """1つの故障。"""

    code: FaultCode
    zone: Zone | None = None
    detail: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _zone_matches_code(self) -> Self:
        if (self.code in ZONE_FAULTS) != (self.zone is not None):
            where = "zone が要る" if self.code in ZONE_FAULTS else "zone に紐づかない"
            raise ValueError(f"{self.code.value} は{where}")
        return self


class ZoneRequest(_Frozen):
    """制御器が1つの zone に求める値。**PWM は持たない。**"""

    demand: Demand
    reason: Reason


class ControllerProposal(_Frozen):
    """制御器（Fallback / Learned MPC）の出力（0028 §2.3）。

    **出せるのは requested まで**（0027 §2.4）。
    effective を決めるのは Reactive Guard と Critical Safety。
    """

    controller: ControllerKind
    seq: int = Field(ge=0)
    computed_at_ms: int = Field(ge=0)
    """記録用の壁時計。**期限の判定には使わない**（受け取った側の単調時計で数える。0028 §2.6）。"""
    requested: PerZone[ZoneRequest]
    model_version: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    ood: bool | None = None
    optimizer_status: OptimizerStatus | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    inference_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    """提案の元になった1回の推論（入力と予測）の識別子（#85）。

    confidence / ood はこの推論に対する判定でなければならない。別の入力の判定を付け替えて
    authority を得ないよう、assessment 側も同じ識別子を持ち、一致しなければ付けられない。
    """

    @model_validator(mode="after")
    def _learned_fields_match_controller(self) -> Self:
        learned = (
            self.model_version,
            self.confidence,
            self.ood,
            self.optimizer_status,
            self.latency_ms,
            self.inference_id,
        )
        if self.controller is ControllerKind.LEARNED_MPC:
            if any(value is None for value in learned):
                raise ValueError(
                    "Learned MPC の提案には model_version / confidence / ood / "
                    "optimizer_status / latency_ms / inference_id が要る（Gate が判定に使う）"
                )
        elif any(value is not None for value in learned):
            raise ValueError("Fallback の提案に ML の項目を入れない")
        return self


class ConfidenceLevel(StrEnum):
    """Gate が confidence から決める tick ごとの authority 区分（決定記録 0050 §2.4）。"""

    HIGH = "high"
    """設定上の authority stage の範囲で Learned MPC を使う。"""
    MEDIUM = "medium"
    """stage の範囲に加え、Fallback を中心とした MEDIUM 帯で変更幅と最低 demand を絞る。"""
    LOW = "low"
    """Fallback へ退避する。OOD は常にここに入る。"""


class AuthorityLimitSource(StrEnum):
    """Learned MPC の requested を狭めた設定上の根拠。"""

    STAGE_BAND = "stage_band"
    """LIMITED / EXPANDED の `authority_limits` 帯。"""
    STAGE_ZONE = "stage_zone"
    """stage が許可していない zone を Fallback の値にした。"""
    MEDIUM_CONFIDENCE_BAND = "medium_confidence_band"
    """MEDIUM confidence の `model_confidence.medium_limit` 帯。"""


MAX_MODEL_GATE_REASONS = 16
"""1 tick に残す assessment の理由の上限。trace の大きさを抑える構造上の上限で、調整値ではない。"""


class ModelGateDecision(_Frozen):
    """Confidence / OOD Gate の1 tick の判断（決定記録 0050 §2.5）。

    **demand を持たない。** requested は ``ControllerProposal`` と zone の記録に残り、
    この型は「なぜ ML をその範囲で使った / 使わなかったか」だけを残す。
    """

    schema_version: Literal[1] = 1
    model_version: str = Field(min_length=1, max_length=120)
    inference_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    """判定した推論（入力と予測）の識別子。提案の ``inference_id`` と同じ。"""
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    ood: bool
    confidence_level: ConfidenceLevel
    authority_stage: AuthorityStage
    learned_selected: bool
    """この tick の requested を Learned MPC が作ったか。"""
    limits: tuple[AuthorityLimitSource, ...] = ()
    """Learned MPC の requested を狭めた根拠。選ばれなかった tick では空。"""
    assessment: tuple[Reason, ...] = Field(default=(), max_length=MAX_MODEL_GATE_REASONS)
    """Confidence / OOD の構成要素ごとの理由（worker の assessment）。"""

    @model_validator(mode="after")
    def _authority_matches_confidence(self) -> Self:
        if len(set(self.limits)) != len(self.limits):
            raise ValueError("authority limit の根拠を重複させない")
        if self.ood and self.confidence_level is not ConfidenceLevel.LOW:
            raise ValueError("OOD の confidence level は LOW にする")
        if self.learned_selected:
            if self.confidence_level is ConfidenceLevel.LOW:
                raise ValueError("LOW confidence の Learned MPC を選ばない（0050 §2.4）")
            if self.authority_stage is AuthorityStage.SHADOW:
                raise ValueError("SHADOW で Learned MPC を選ばない")
            if (
                self.confidence_level is ConfidenceLevel.MEDIUM
                and AuthorityLimitSource.MEDIUM_CONFIDENCE_BAND not in self.limits
            ):
                raise ValueError("MEDIUM confidence では MEDIUM 帯を掛ける")
        elif self.limits:
            raise ValueError("Learned MPC を選ばない tick に authority limit を付けない")
        return self


class GuardZoneOutput(_Frozen):
    """Reactive Guard の zone ごとの出力（0028 §2.3）。介入していなければすべて None。

    ceiling が Critical Safety の floor を下回ってもよい。**floor が勝つ**（0028 §2.4）。
    """

    floor: Demand | None = None
    ceiling: Demand | None = None
    hold_until_mono_ms: int | None = Field(default=None, ge=0)
    """解除してよい時刻。単調時計（0028 §2.6）。"""
    reason: Reason | None = None

    @model_validator(mode="after")
    def _intervention_is_explained(self) -> Self:
        intervening = self.floor is not None or self.ceiling is not None
        if intervening and (self.hold_until_mono_ms is None or self.reason is None):
            raise ValueError("Guard が介入するなら hold_until_mono_ms と reason が要る")
        if not intervening and (self.hold_until_mono_ms is not None or self.reason is not None):
            raise ValueError("介入していない Guard に hold や reason を付けない")
        return self


class SafetyZoneOutput(_Frozen):
    """Critical Safety の zone ごとの出力（0028 §2.3）。"""

    floor: Demand
    """最低安全 demand。Top は CPU cooling floor を含む（0028 §2.4）。"""
    forced_max: bool
    """`STARTUP` / `EMERGENCY` / zone の fault / 運転モード `MAX`（0028 §2.4）。"""
    reason: Reason | None = None

    @model_validator(mode="after")
    def _forced_max_is_explained(self) -> Self:
        if self.forced_max and self.reason is None:
            raise ValueError("Max にするなら reason が要る")
        return self


class EffectiveZoneDemand(_Frozen):
    """合成（0028 §2.4）の結果。**requested と effective が違えば、理由が必ず残る。**"""

    requested: Demand
    effective: Demand
    bound_by: BoundBy
    safety_floor: Demand
    forced_max: bool
    guard_floor: Demand | None = None
    guard_ceiling: Demand | None = None
    reasons: tuple[Reason, ...] = ()
    """requested から変えた理由。Guard / Safety / 下げる速さの制限のそれぞれが足す。"""

    @model_validator(mode="after")
    def _matches_the_combination_rules(self) -> Self:
        if self.forced_max != (self.bound_by is BoundBy.FORCED_MAX):
            raise ValueError("forced_max と bound_by=forced_max は必ず一緒に立つ")
        if self.forced_max and self.effective != 1.0:
            raise ValueError("Max はすべてに勝つ。forced_max なら effective は 1.0")
        if self.effective < self.safety_floor:
            raise ValueError("effective が Critical Safety の floor を下回っている")
        if self.guard_floor is not None and self.effective < self.guard_floor:
            raise ValueError(
                "effective が Guard の floor を下回っている（floor は ceiling に勝つ）"
            )
        # requested から下げられる項は Guard の ceiling だけ。ほかの項（floor・下げる速さの
        # 制限・Max）は上げる向きにしか働かない（0028 §2.4）
        lowest = (
            self.requested
            if self.guard_ceiling is None
            else min(self.requested, self.guard_ceiling)
        )
        if self.effective < lowest:
            raise ValueError("effective が requested / ceiling より下げられている")
        if self.effective > self.requested and self.bound_by is BoundBy.GUARD_CEILING:
            raise ValueError("ceiling は requested を上げない")

        determined_by: dict[BoundBy, float | None] = {
            BoundBy.SAFETY_FLOOR: self.safety_floor,
            BoundBy.GUARD_FLOOR: self.guard_floor,
            BoundBy.GUARD_CEILING: self.guard_ceiling,
            BoundBy.REQUESTED: self.requested,
        }
        if self.bound_by in determined_by and self.effective != determined_by[self.bound_by]:
            raise ValueError(f"bound_by={self.bound_by.value} なのに effective がその値と違う")

        if self.bound_by is not BoundBy.REQUESTED and not self.reasons:
            raise ValueError("requested から変えた理由（reasons）が無い")
        return self


class HardwareReadback(_Frozen):
    """Hardware Backend が読み戻した値。**記録用で、指令ではない。**

    PWM の指令の型は Hardware Backend（#77）の中に置き、上位層からは作れないようにする。
    """

    pwm_raw: int = Field(ge=0, le=HWMON_PWM_MAX)
    rpm: int | None = Field(default=None, ge=0)
    write_ok: bool
    readback_ok: bool


class ZoneRecord(_Frozen):
    """1 tick の zone ごとの記録（#76 の必須フィールド。#82 が保存する）。"""

    controller_reason: Reason
    """制御器がその requested にした理由。"""
    demand: EffectiveZoneDemand
    """requested / effective / Guard の floor・ceiling / Safety の floor と、変えた理由。"""
    airflow_index: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    """zone 自身の能力比（#75）。zone をまたいで比べない。"""
    estimated_flow: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    """zone をまたいで比べる推定量（#75 / #81）。単位は #75 で決める。"""
    hardware: HardwareReadback | None = None
    """まだ書き込んでいない tick（`STARTUP` の最初など）は None。"""


class ControlState(_Frozen):
    """1 tick の制御全体の状態（0028 §2.5）。"""

    operating_mode: OperatingMode
    authority_stage: AuthorityStage
    active_controller: ControllerKind | None
    """requested を作った制御器。

    `MANUAL` / `CALIBRATION` では人や測定計画が requested を作るので None（0028 §2.5 (a)）。
    `MAX` は AUTO の経路に重ねる override なので、下で動く制御器を記録する
    （MAX を抜けたときに戻る先を追える）。
    """
    safety_state: SafetyState
    fallback_active: bool
    fallback_reason: Reason | None = None
    """ML を使えたはずの状況で Fallback にした理由（0028 §2.5 (c)）。"""
    # v1/v2 traceでは実装固有の自由文字列だったため、外側のControlTick versionを見ずに
    # enumへ狭めると保存済みrecordを読めなくなる。v3の許可値・decision整合はControlTickで検証する。
    supervisor_policy: str | None = None
    workload_regime: WorkloadRegime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    regime_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        exclude_if=lambda value: value is None,
    )
    model_version: str | None = None
    model_confidence: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    model_ood: bool | None = None

    @model_validator(mode="after")
    def _ml_is_used_only_when_allowed(self) -> Self:
        if (self.workload_regime is None) != (self.regime_confidence is None):
            raise ValueError("workload_regime と regime_confidence は一緒に記録する")
        set_by_people = self.operating_mode in {OperatingMode.MANUAL, OperatingMode.CALIBRATION}
        if set_by_people != (self.active_controller is None):
            raise ValueError("active_controller を持たないのは MANUAL / CALIBRATION のときだけ")
        if self.fallback_active != (self.active_controller is ControllerKind.FALLBACK):
            raise ValueError("fallback_active と active_controller が食い違っている")
        if self.fallback_reason is not None and not self.fallback_active:
            raise ValueError("Fallback でない tick に fallback_reason を付けない")

        # MAX は AUTO の上に重ねる forced_max overrideだが、0028 §2.5(c)で
        # Learned MPCをactiveにできるmodeはAUTOだけ。MAX中のcounterfactualな
        # Learned提案はactive controllerとは別に記録する。
        ml_could_run = (
            self.operating_mode is OperatingMode.AUTO
            and self.authority_stage is not AuthorityStage.SHADOW
            and self.safety_state is SafetyState.NORMAL
        )
        if self.active_controller is ControllerKind.LEARNED_MPC:
            if not ml_could_run:
                raise ValueError("ML を使えるのは AUTO・LIMITED 以上・NORMAL のときだけ")
            if (
                self.model_version is None
                or self.model_confidence is None
                or self.model_ood is None
            ):
                raise ValueError("ML を使った tick には model_version / confidence / ood が要る")
            if self.model_ood:
                raise ValueError("OOD のモデルに制御させない（0028 §2.5 (c)）")
        elif ml_could_run and self.fallback_reason is None:
            raise ValueError("ML を使えたはずなのに Fallback にした理由が無い")
        return self


EMERGENCY_FAULTS: frozenset[FaultCode] = frozenset(
    {
        FaultCode.ABSOLUTE_TEMPERATURE_LIMIT,
        FaultCode.CONFIG_INVALID,
        FaultCode.FALLBACK_EXCEPTION,
        FaultCode.GUARD_EXCEPTION,
    }
)
"""起きたら無条件に `EMERGENCY` にする故障（設定不正・決定論的な層の例外。0028 §2.7）。"""

TOP_EMERGENCY_FAULTS: frozenset[FaultCode] = frozenset(
    {
        FaultCode.TACH_STALL,
        FaultCode.WRITE_FAILURE,
        FaultCode.READBACK_MISMATCH,
        FaultCode.ENABLE_REVERTED,
    }
)
"""Top で起きたら無条件に `EMERGENCY` にする故障。Top は CPU の冷却を担う（0028 §2.7）。

回数で決まる対応（overrun の連続、Front / Rear の書き込み失敗の繰り返し）は
Critical Safety（#78）が数えるので、ここでは検査しない。
"""


_FAULTS_ADDED_IN_V4: frozenset[FaultCode] = frozenset({FaultCode.ABSOLUTE_TEMPERATURE_LIMIT})
"""schema version 4 で追加した fault code。

v1〜v3 の reader は知らないため v3 以前には記録しない。
"""


class ControlTick(_Frozen):
    """1 tick の判断の記録（decision trace。0028 §2.3）。#82 が保存し、#90 / #91 が読む。"""

    schema_version: Literal[1, 2, 3, 4, 5] = SCHEMA_VERSION
    tick_id: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    """記録の時刻（壁時計。0028 §2.6）。"""
    state: ControlState
    zones: PerZone[ZoneRecord]
    supervisor: SupervisorDecision | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    model_gate: ModelGateDecision | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """Confidence / OOD Gate の判断（v5。#85）。Learned MPC の提案が無い tick では None。"""
    faults: tuple[Fault, ...] = ()
    """いま有効な故障。

    **解消しても `fault_clear_hold_ms` の間は残す。** その間は状態を安全側に保つため
    （0028 §2.5 (d)）、残さないと hold 中の `EMERGENCY` を原因の無い記録にしてしまう。
    """

    @model_validator(mode="after")
    def _state_matches_zones_and_faults(self) -> Self:
        state = self.state
        if self.schema_version < 4 and any(
            fault.code in _FAULTS_ADDED_IN_V4 for fault in self.faults
        ):
            raise ValueError(
                "absolute_temperature_limit を記録する ControlTick は schema version 4 にする"
            )
        if self.schema_version == 1 and state.workload_regime is not None:
            raise ValueError("workload regime を記録する ControlTick は schema version 2 にする")
        if self.schema_version < 3 and self.supervisor is not None:
            raise ValueError(
                "Supervisor decision を記録する ControlTick は schema version 3 にする"
            )
        if (
            self.schema_version >= 3
            and self.supervisor is None
            and state.supervisor_policy is not None
        ):
            raise ValueError("v3 以降の supervisor_policy には Supervisor decision が必要")
        self._check_model_gate()
        if self.supervisor is not None:
            if self.supervisor.tick_id != self.tick_id:
                raise ValueError("Supervisor decision の tick_id を ControlTick と揃える")
            if self.supervisor.ts_ms != self.ts_ms:
                raise ValueError("Supervisor decision の ts_ms を ControlTick と揃える")
            selected = self.supervisor.selected_output
            selected_policy = None if selected is None else selected.policy.value
            if state.supervisor_policy != selected_policy:
                raise ValueError("ControlState の supervisor_policy を選択された出力と揃える")
            successful_outputs = tuple(
                evaluation.output
                for evaluation in (
                    self.supervisor.active,
                    self.supervisor.fallback,
                    self.supervisor.shadow,
                )
                if evaluation is not None and evaluation.output is not None
            )
            if any(
                state.workload_regime is not output.regime
                or (
                    output.tick_id == self.tick_id
                    and state.regime_confidence != output.regime_confidence
                )
                for output in successful_outputs
            ):
                raise ValueError("ControlState と Supervisor output の workload regime を揃える")
        all_max = state.safety_state in {SafetyState.STARTUP, SafetyState.EMERGENCY} or (
            state.operating_mode is OperatingMode.MAX
        )
        if all_max and not all(self.zones.get(zone).demand.forced_max for zone in Zone):
            raise ValueError("STARTUP / EMERGENCY / MAX モードでは、すべての zone が Max")

        if state.safety_state is SafetyState.NORMAL and self.faults:
            raise ValueError("NORMAL なのに fault がある")
        if state.safety_state in {SafetyState.DEGRADED, SafetyState.EMERGENCY} and not self.faults:
            raise ValueError(f"{state.safety_state.value} の原因（fault）が無い")

        for fault in self.faults:
            self._check_fault_response(fault)

        for zone in Zone:
            demand = self.zones.get(zone).demand
            if demand.guard_ceiling is None:
                continue
            if state.operating_mode is not OperatingMode.AUTO:
                raise ValueError("Guard の ceiling を掛けるのは AUTO だけ（0028 §2.4）")
            if demand.bound_by is BoundBy.REQUESTED and demand.requested > demand.guard_ceiling:
                raise ValueError(f"{zone.value}: AUTO なのに Guard の ceiling を掛けていない")
        return self

    def _check_model_gate(self) -> None:
        """v5 の ``model_gate`` が ControlState と同じ判断を指しているか。"""
        state = self.state
        gate = self.model_gate
        if gate is None:
            if self.schema_version >= 5 and state.active_controller is ControllerKind.LEARNED_MPC:
                raise ValueError("v5 以降で Learned MPC を使った tick には model_gate が要る")
            return
        if self.schema_version < 5:
            raise ValueError("model_gate を記録する ControlTick は schema version 5 にする")
        if (state.model_version, state.model_confidence, state.model_ood) != (
            gate.model_version,
            gate.confidence,
            gate.ood,
        ):
            raise ValueError(
                "ControlState の model_version / confidence / ood を model_gate と揃える"
            )
        if state.authority_stage is not gate.authority_stage:
            raise ValueError("ControlState と model_gate の authority stage を揃える")
        if gate.learned_selected != (state.active_controller is ControllerKind.LEARNED_MPC):
            raise ValueError("model_gate.learned_selected と active_controller が食い違っている")

    def _check_fault_response(self, fault: Fault) -> None:
        """0028 §2.7 の無条件の対応を満たしているか。"""
        # v1〜v3 の記録は当時の規則で読む。v3 までは Top の enable_reverted は DEGRADED でも
        # 有効だったため、v4 の規則で検証すると保存済みの記録を読めなくなる。
        top_emergency = (
            TOP_EMERGENCY_FAULTS
            if self.schema_version >= 4
            else TOP_EMERGENCY_FAULTS - {FaultCode.ENABLE_REVERTED}
        )
        emergency = fault.code in EMERGENCY_FAULTS or (
            fault.zone is Zone.TOP and fault.code in top_emergency
        )
        if emergency and self.state.safety_state is not SafetyState.EMERGENCY:
            raise ValueError(f"{fault.code.value} は EMERGENCY にする（0028 §2.7）")
        if (
            fault.code is FaultCode.TACH_STALL
            and fault.zone is not None
            and not self.zones.get(fault.zone).demand.forced_max
        ):
            raise ValueError(f"tach stall の {fault.zone.value} は Max にする（再始動を試みる）")
        if fault.code is FaultCode.CPU_TELEMETRY_STALE and not self.zones.top.demand.forced_max:
            raise ValueError("CPU 温度が stale なら Top は Max にする（0028 §2.7）")
