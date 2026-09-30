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

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal[13] = 13
"""`ControlTick` の形の版。**フィールドの名前や意味を変えたら上げる。**

#82 が保存したデータを読み違えないため。

- v2（#87 / #133）: workload regime と regime confidence
- v3（#88 / #137）: Supervisor decision
- v4（#78）: fault code `absolute_temperature_limit` を追加し、Top の `enable_reverted` を
  無条件の `EMERGENCY` にした。保存済みの v1〜v3 は v3 までの規則のまま読める
- v5（#85）: Model Confidence / OOD と authority の判断（`model_gate`）。Learned MPC を
  active にした tick には必須。保存済みの v1〜v4 はそのまま読める
- v6（#90）: Shadow Mode の counterfactual 記録（`shadow`）。**適用した demand とは別の枠**に
  置き、適用した制御器と同じ controller を counterfactual にできない。保存済みの v1〜v5 は
  そのまま読める
- v7（#159 / 決定記録 0059）: 判断を出した model artifact の hash
  （`model_gate.artifact_sha256`。`ModelGateDecision` の schema version も 2 へ上げる）。
  **裏づけのある推論にだけ付く。** 保存済みの v1〜v6 はそのまま読め、欄の無い tick は
  「artifact 不明」として扱う（昇格の証拠に使わない。fail closed）
- v8（#74 / 決定記録 0060）: control loop の実行そのものの記録（`runtime`）。tick の所要時間・
  締め切り超過・周期・snapshot schema・設定の内容ハッシュを、判断と同じ行に残す。
  保存済みの v1〜v7 はそのまま読める
- v9（#78）: Critical Safety の裁定の出どころ（`safety_provenance`）。設定で外した入力の
  理由（`disabled_inputs`）と、暫定値を含む Safety 設定で回っていたか（`config_is_provisional`）。
  **v9 には必須**で、保存済みの v1〜v8 は欄なしのまま読める（「記録が無い」であって
  「確定値で回っていた」ではない）
- v10（#104 / 決定記録 0071 §2.5）: tick が使っていた Model Registry の版（`registry`）。
  registry の `revision` と、kind ごとの production pointer・それを成立させた pointer 変更の
  `RegistryAuditEvent.trace_metadata()`（ただし自由記述の `reason` は全文でなく `reason_sha256`。
  決定記録 0075）。**v10 には必須**で、保存済みの v1〜v9 は欄なしのまま
  読める（「記録が無い」であって「production が無かった」ではない）
- v11（#81 / 決定記録 0073 §2.5）: Air Balance の記録（`air_balance`）と、zone ごとの
  `applied_demand`（Hardware Backend が PWM へ写す直前の demand）・それを曲線で写した
  `estimated_flow`。`runtime` は schema version 2（4ファイルの版と SHA-256）を要求する。
  **v11 には `air_balance` が必須**で、保存済みの v1〜v10 は欄なしのまま読める。
  決定記録 0073 は「v10」と書くが、先に #104 が v10 を使ったため次の空き番号にした（0073 §5）
- v12（#74 / 決定記録 0072 §2.2 / §2.7）: その tick に効いていたモードの出どころ（`mode_command`）。
  管理ソケットの指令の `command_id` と起動ごとの `run_id`、`MANUAL` の期限切れ、
  受付スレッドの死による `MAX`（`admin_receiver_dead`）。**v12 には必須**で、
  保存済みの v1〜v11 は欄なしのまま読める（「記録が無い」であって「入口が無かった」ではない）
- v13（#92 / 決定記録 0072 §2.6 / §2.10 段階 2）: その tick の制御権の出どころ（`authority`）。
  journal の stage と revision、設定の上限、この process が書き残せずに持っている上限、
  走行中に journal を読めなかったことによる `SHADOW`（`journal_unreadable`）、この tick の先頭で
  適用した管理ソケットの降格の `command_id`、直近の永続化の失敗。**v13 には必須**で、
  保存済みの v1〜v12 は欄なしのまま読める（「記録が無い」であって「journal が無かった」ではない）
"""

Demand = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
"""制御の内部単位（0027 §2.1）。**NaN / 無限大は拒否する**（0028 §2.1）。"""

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
"""sha256 の16進表現。**大文字や短縮形を受け取らない。**"""

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


STAGE_ORDER: tuple[AuthorityStage, ...] = (
    AuthorityStage.SHADOW,
    AuthorityStage.LIMITED,
    AuthorityStage.EXPANDED,
    AuthorityStage.FULL,
)
"""低い順。**昇格は隣へ1段ずつ**（#92 / 決定記録 0057 §2.5）。"""

BASELINE_STAGE = AuthorityStage.SHADOW
"""rollback で戻る先。実 Fan は Fallback が作る（0028 §2.5 (c)）。"""


def stage_rank(stage: AuthorityStage) -> int:
    """stage の高さ。比較にだけ使う。"""
    return STAGE_ORDER.index(stage)


def lowest_stage(*stages: AuthorityStage) -> AuthorityStage:
    """もっとも低い stage を返す。**上限を重ねるときは必ずこれを通す。**"""
    if not stages:
        raise ValueError("stage を1つ以上渡す")
    return min(stages, key=stage_rank)


def stage_above(stage: AuthorityStage) -> AuthorityStage | None:
    """1段上の stage。`FULL` の上は無い。"""
    rank = stage_rank(stage)
    return STAGE_ORDER[rank + 1] if rank + 1 < len(STAGE_ORDER) else None


def stage_below(stage: AuthorityStage) -> AuthorityStage | None:
    """1段下の stage。`SHADOW` の下は無い。"""
    rank = stage_rank(stage)
    return STAGE_ORDER[rank - 1] if rank else None


@runtime_checkable
class AuthorityStageSource(Protocol):
    """Gate へ「いまの stage」を渡す読み取り専用の窓口（#92）。

    **契約の型なのでここに置く。** journal と自動降格の実装は
    `coldaisle.control.authority` にあり、Gate はそちらを import しない。
    """

    def current_stage(self) -> AuthorityStage:
        """この tick に与えてよい制御権。"""
        ...


class StaticAuthorityStage:
    """設定の stage をそのまま使う source（journal を持たない試験・移行用）。"""

    __slots__ = ("_stage",)

    def __init__(self, stage: AuthorityStage) -> None:
        self._stage = stage

    def current_stage(self) -> AuthorityStage:
        """設定された stage を返す。"""
        return self._stage


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


class SupervisorPolicyIdentity(_Frozen):
    """RL policy artifact の**完全な**識別（#89 / 決定記録 0061）。

    semantic version だけでは、同じ版を名乗る別の model ID・別の bytes を区別できない。
    shadow の証拠がどの artifact のものかは、この3つ組で名指しする。
    """

    model_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=120)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SupervisorPolicyEvaluation(_Frozen):
    """1 policy の成功出力または構造化された失敗を decision trace に残す。"""

    policy: SupervisorPolicyKind
    output: SupervisorOutput | None = None
    policy_identity: SupervisorPolicyIdentity | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """成功した RL 出力を作った artifact の完全な識別。Rule と失敗には付かない。

    `SupervisorDecision` v2 で足した欄（#89 / 決定記録 0061 §2.6）。**無いときは書き出さない**
    ので、識別を持たない評価は v1 と同じ形で残る。
    """
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
        if self.policy_identity is not None and (
            self.policy is not SupervisorPolicyKind.RL or self.output is None
        ):
            raise ValueError("artifact の識別は成功した RL 出力にだけ付ける")
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


SUPERVISOR_DECISION_SCHEMA_VERSION: Literal[2] = 2
"""`SupervisorDecision` の形の版（`ControlTick` の中に入れ子で入る）。

- v2（#89 / 決定記録 0061 §2.6）: 評価ごとの `policy_identity`（RL artifact の完全な識別）。
  **保存済みの v1 はそのまま読め、識別を持てない。** 入れ子の版を上げないと、v1 と名乗るのに
  v1 には無かった欄を持つ記録を書いてしまい、前の版の reader（`extra="forbid"`）が
  版ではなく未知の欄で拒むことになる。`ControlTick` の版は上げない（番号は PR 間で
  確保して使うため。#159 の `model_gate` v2 と同じ入れ子の上げ方）
"""


class SupervisorDecision(_Frozen):
    """active / shadow と Rule fallback を同じ入力単位で記録する。"""

    schema_version: Literal[1, 2] = SUPERVISOR_DECISION_SCHEMA_VERSION
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
        if self.schema_version < 2 and any(
            evaluation is not None and evaluation.policy_identity is not None
            for evaluation in (self.active, self.fallback, self.shadow)
        ):
            # v1 の記録にこの欄は無かった。後から足して読ませない。
            raise ValueError(
                "policy_identity を記録する Supervisor decision は schema version 2 にする"
            )
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

MODEL_GATE_ASSESSMENT_COMPONENTS: tuple[str, ...] = (
    "model_binding",
    "feature_range",
    "fan_state_range",
    "support",
    "missing_pattern",
    "uncertainty",
    "residual_drift",
)
"""Confidence / OOD の構成要素（#85）。

`coldaisle.control.model.confidence.ConfidenceComponent` と同じ集合で、trace の理由を検証するために
ここに持つ（下位の schema から ML の module を import しないため。一致は試験で確かめる）。
"""

OOD_REASON_PREFIX = "ood_"
"""OOD と判定した構成要素の理由に付く接頭辞。"""


MODEL_GATE_SCHEMA_VERSION: Literal[2] = 2
"""`ModelGateDecision` の形の版（`ControlTick` の中に入れ子で入る。決定記録 0065 §2.2）。

- v2（#159）: `artifact_sha256`。**保存済みの v1 はそのまま読め、artifact を持てない。**
  `ControlTick` v7 以降はこの v2 を、v1〜v6 は v1 を要求する。入れ子の版を上げないと、
  v7 の trace が「v1 と名乗るのに v1 には無かった欄を持つ」記録を書いてしまう
  （codex #4057753201）。
"""


class ModelGateDecision(_Frozen):
    """Confidence / OOD Gate の1 tick の判断（決定記録 0050 §2.5）。

    **demand を持たない。** requested は ``ControllerProposal`` と zone の記録に残り、
    この型は「なぜ ML をその範囲で使った / 使わなかったか」だけを残す。
    """

    schema_version: Literal[1, 2]
    """判断の形の版。**入力では必須で、既定値を持たない**（codex #4092017585）。

    版の無い記録を最新版（v2）として読まない。作る側（Gate）は
    `MODEL_GATE_SCHEMA_VERSION` を明示して渡す。
    """
    model_version: str = Field(min_length=1, max_length=120)
    inference_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    """判定した推論（入力と予測）の識別子。提案の ``inference_id`` と同じ。"""
    artifact_sha256: Sha256Hex | None = None
    """この判断を出した **model artifact** の hash（`ControlTick` v7。#159）。

    **裏づけのある assessment からしか来ない。** 提案は artifact の欄を持たず、Gate は
    検証済み（`REGISTRY_VERIFIED`）の ``ConfidenceAssessment`` の値をそのまま写す。
    自称値や呼び出し側から受け取った値を書く経路を作らない（#85 の束縛をそのまま使う）。

    **裏づけの無い判断には付かない。** 保存済みの v1〜v6 の tick も欄を持たないので、
    どちらも **「artifact 不明」**として扱う。昇格の証拠（#92 / 決定記録 0057 §2.4）に
    使えるのは、artifact を言える tick だけである（fail closed）。
    """
    attested: bool
    """検証済み assessment に裏付けられた記録か。

    **裏付けの無い提案の数値を trace に書かない。** 後から評価（#90 / #91）や stage の判断（#92）が
    読むため、提案が自称した confidence / ood をそのまま残すと、OOD の推論が HIGH に見えてしまう。
    """
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    """検証済み assessment の confidence。``attested`` でなければ None。"""
    ood: bool | None = None
    """検証済み assessment の ood。``attested`` でなければ None。"""
    proposal_mismatch: Reason | None = None
    """assessment は検証できたが、提案の自称値がそれと違ったときの理由。"""
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
        if self.attested != (self.confidence is not None):
            raise ValueError("attested な記録だけが confidence を持つ")
        if not self.attested and self.artifact_sha256 is not None:
            # 裏づけの無い判断に artifact を書くと、別の推論の artifact が「この tick の
            # 実績」として読まれる。**束縛できていない identity は残さない。**
            raise ValueError("裏づけの無い記録に artifact_sha256 を残さない")
        if self.schema_version < 2 and self.artifact_sha256 is not None:
            # v1 の記録にこの欄は無かった。後から足して読ませない。
            raise ValueError("artifact を記録する model_gate は schema version 2 にする")
        if (self.confidence is None) != (self.ood is None):
            raise ValueError("model_gate の confidence と ood は一緒に記録する")
        if not self.attested:
            if self.confidence_level is not ConfidenceLevel.LOW:
                raise ValueError("裏付けの無い記録の confidence level は LOW にする")
            if self.proposal_mismatch is not None:
                raise ValueError("assessment を検証できていない記録に不一致の理由を付けない")
            if self.assessment:
                # 束縛できていない assessment の理由は、別の推論のものかもしれない。
                raise ValueError("裏付けの無い記録に assessment の理由を残さない")
        elif not self.assessment:
            raise ValueError("裏付けのある記録には assessment の理由が要る")
        else:
            self._check_assessment_reasons()
        if self.ood:
            if self.confidence_level is not ConfidenceLevel.LOW:
                raise ValueError("OOD の confidence level は LOW にする")
            if self.confidence != 0.0:
                raise ValueError("OOD の confidence は 0 にする（決定記録 0050 §2.2）")
        if (
            AuthorityLimitSource.STAGE_ZONE in self.limits
            and AuthorityLimitSource.STAGE_BAND not in self.limits
        ):
            raise ValueError("zone の制限は stage の帯と一緒にしか掛からない")
        if self.learned_selected:
            if not self.attested:
                raise ValueError("裏付けの無い提案を active controller にしない")
            if self.proposal_mismatch is not None:
                # 自称値が assessment と違う提案は、Gate がその tick で Fallback へ落とす。
                raise ValueError("assessment と食い違う提案を active controller にしない")
            if self.confidence_level is ConfidenceLevel.LOW:
                raise ValueError("LOW confidence の Learned MPC を選ばない（0050 §2.4）")
            if self.authority_stage is AuthorityStage.SHADOW:
                raise ValueError("SHADOW で Learned MPC を選ばない")
            if (self.confidence_level is ConfidenceLevel.MEDIUM) != (
                AuthorityLimitSource.MEDIUM_CONFIDENCE_BAND in self.limits
            ):
                raise ValueError("MEDIUM 帯を掛けるのは MEDIUM confidence のときだけ")
            if (self.authority_stage is not AuthorityStage.FULL) != (
                AuthorityLimitSource.STAGE_BAND in self.limits
            ):
                raise ValueError("FULL 未満の stage では必ず stage の帯を掛ける")
        elif self.limits:
            raise ValueError("Learned MPC を選ばない tick に authority limit を付けない")
        return self

    def _check_assessment_reasons(self) -> None:
        """理由の集合が判定と噛み合っているか（Gate は構成要素ごとに1つずつ書く）。"""
        seen: list[str] = []
        ood_components: list[str] = []
        for reason in self.assessment:
            code = reason.code
            is_ood = code.startswith(OOD_REASON_PREFIX)
            component = code.removeprefix(OOD_REASON_PREFIX) if is_ood else code
            if component not in MODEL_GATE_ASSESSMENT_COMPONENTS:
                raise ValueError(f"assessment に未知の構成要素の理由がある: {code}")
            seen.append(component)
            if is_ood:
                ood_components.append(component)
        if len(set(seen)) != len(seen):
            raise ValueError("assessment の理由は構成要素ごとに1つにする")
        if set(seen) != set(MODEL_GATE_ASSESSMENT_COMPONENTS):
            missing = sorted(set(MODEL_GATE_ASSESSMENT_COMPONENTS) - set(seen))
            raise ValueError(f"assessment の理由に足りない構成要素がある: {missing}")
        if bool(ood_components) != bool(self.ood):
            raise ValueError("assessment の ood_* の理由と ood の判定が食い違っている")


SHADOW_SCHEMA_VERSION: Literal[1] = 1
"""`ShadowRecord` の形の版（#90 / 決定記録 0053）。"""

MAX_SHADOW_COUNTERFACTUALS = len(ControllerKind)
"""1 tick に残す counterfactual の上限。

制御器ごとに高々1つなので（`ShadowRecord` が重複を拒む）、**種類の数と必ず一致する**。
任意の数を置くと、制御器が増えたときにここだけが先に詰まる。
"""

MAX_SHADOW_PLAN_STEPS = 32
"""counterfactual の候補 plan と予測に残す control step 数の上限。

**写している契約と同じ値にする。** 設定が許す plan の step 数（`MAX_MPC_HORIZON_STEPS`）と
予測が持てる horizon 数（`MAX_TARGET_HORIZONS`）はどちらも 32 で、ここだけ狭いと
「設定としては妥当な MPC が出した解を記録できない」tick が生まれる。記録の失敗は制御の
途中で起きるので、**狭い上限を後から見つけない**。下位 schema から上位 module を import
しないためここに写し、一致は試験で確かめる。
"""

MAX_SHADOW_PREDICTION_METRICS = 32
"""1 step に残す予測 metric 数の上限。`MAX_TARGET_METRICS` に合わせる（同上）。"""

MAX_SHADOW_METRIC_NAME_LENGTH = 120
"""予測 metric 名の長さの上限。`MAX_METRIC_NAME_LENGTH`（#84）に合わせる（同上）。"""

SHADOW_METRIC_NAME_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,3}$"
"""予測 metric 名の形。**`ThermalMetricName`（#84）と同じ**にする。

記録するのはモデルの target metric なので、そちらより狭い形にすると、学習に使えた metric を
記録できなくなる。一致は試験で確かめる。
"""

ShadowMetricName = Annotated[
    str,
    Field(pattern=SHADOW_METRIC_NAME_PATTERN, max_length=MAX_SHADOW_METRIC_NAME_LENGTH),
]


class ShadowPlanStep(_Frozen):
    """記録した候補 action 列の1 step。"""

    offset_ms: int = Field(gt=0)
    demands: PerZone[Demand]


class ShadowActionPlan(_Frozen):
    """counterfactual が評価された候補 action 列。

    **``coldaisle.control.mpc.plan.ActionPlan`` と同じ形にする。** 同じ内容から同じ
    ``digest()`` が出なければ、記録した予測がどの候補に対するものかを後から確かめられない。
    schema から mpc を import しないためここに写し、digest の一致は試験で確かめる。
    """

    step_ms: int = Field(gt=0)
    steps: tuple[ShadowPlanStep, ...] = Field(min_length=1, max_length=MAX_SHADOW_PLAN_STEPS)

    @model_validator(mode="after")
    def _offsets_are_a_uniform_grid(self) -> Self:
        expected = tuple(self.step_ms * (index + 1) for index in range(len(self.steps)))
        if tuple(step.offset_ms for step in self.steps) != expected:
            raise ValueError("shadow plan の offset_ms は step_ms の等間隔にする")
        return self

    @property
    def first(self) -> PerZone[Demand]:
        """次の control step で要求していた demand。**これだけが requested になりうる。**"""
        return self.steps[0].demands

    @property
    def offsets_ms(self) -> tuple[int, ...]:
        """各 step の予測時刻（action からの相対）。"""
        return tuple(step.offset_ms for step in self.steps)

    def digest(self) -> str:
        """この plan を一意に表す SHA-256（``ActionPlan.digest()`` と同じ値）。"""
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class ShadowPredictedTarget(_Frozen):
    """counterfactual な候補 action に対する1 control step の予測。

    ``expected_ts_ms`` は**予測の元になった action 時刻から決まる**。あとで実測と突き合わせる
    ときに、記録した時刻ではなく処理した時刻を使わせないため、ここで固定する（0053 §2.3）。
    """

    offset_ms: int = Field(gt=0)
    expected_ts_ms: int = Field(ge=0)
    values: dict[ShadowMetricName, TemperatureC] = Field(
        min_length=1, max_length=MAX_SHADOW_PREDICTION_METRICS
    )


class ShadowPrediction(_Frozen):
    """counterfactual の予測 future。**1回の推論に束ねられている。**

    ``inference_id`` / ``plan_digest`` / ``artifact_sha256`` を持たない予測は作れない。
    別の推論の予測を後から貼り替えて「当たっていた」ことにできないようにするため
    （#85 / #86 と同じ束縛を使い、別の仕組みを作らない）。
    """

    schema_version: Literal[1] = SHADOW_SCHEMA_VERSION
    model_id: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    artifact_sha256: Sha256Hex
    inference_id: Sha256Hex
    """この予測が属する anchor 推論（#85 の判定対象）。"""
    plan_digest: Sha256Hex
    """予測した候補 plan の識別子（``ActionPlan.digest()``。決定記録 0052 §2.2）。"""
    input_action_ts_ms: int = Field(ge=0)
    targets: tuple[ShadowPredictedTarget, ...] = Field(
        min_length=1, max_length=MAX_SHADOW_PLAN_STEPS
    )

    @model_validator(mode="after")
    def _targets_follow_the_recorded_action_time(self) -> Self:
        offsets = tuple(target.offset_ms for target in self.targets)
        if tuple(sorted(set(offsets))) != offsets:
            raise ValueError("shadow prediction の offset は重複なし昇順にする")
        if any(
            target.expected_ts_ms != self.input_action_ts_ms + target.offset_ms
            for target in self.targets
        ):
            raise ValueError("shadow prediction の時刻は action 時刻 + offset にする")
        return self


class ShadowCounterfactual(_Frozen):
    """**適用しなかった**提案の記録（#90）。

    この型は effective demand も PWM も表現できない。``requested`` は「もし使っていたら
    要求していた値」であって、この tick の Fan には届いていない（0053 §2.1）。
    confidence / ood は **Gate が裏付けた値だけ**を持つ。提案の自称値は残さない（#85 と同じ規則）。
    """

    controller: ControllerKind
    requested: PerZone[Demand] | None = None
    """counterfactual な要求。worker が提案を作れなかった tick では None。"""
    reason: Reason | None = None
    """その値にした理由。提案のある counterfactual には必ず付く。"""
    optimizer_status: OptimizerStatus | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    evaluations: int | None = Field(default=None, ge=0)
    model_version: str | None = Field(default=None, min_length=1, max_length=120)
    inference_id: Sha256Hex | None = None
    artifact_sha256: Sha256Hex | None = None
    attested: bool = False
    """Gate が検証済み assessment で裏付けた記録か。"""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    ood: bool | None = None
    plan: ShadowActionPlan | None = None
    """予測した候補 action 列そのもの。``requested`` はこの plan の最初の step である。"""
    prediction: ShadowPrediction | None = None
    cost_total: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    baseline_cost_total: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    failure: Reason | None = None
    """提案を作れなかった worker の失敗（model 読込失敗・optimizer 例外）。"""

    @model_validator(mode="after")
    def _record_is_bound_to_one_inference(self) -> Self:
        if (self.requested is None) != (self.reason is None):
            raise ValueError("counterfactual の要求と理由は一緒に記録する")
        learned = (
            self.optimizer_status,
            self.latency_ms,
            self.evaluations,
            self.model_version,
            self.inference_id,
            self.artifact_sha256,
            self.plan,
            self.prediction,
            self.cost_total,
            self.baseline_cost_total,
        )
        if self.controller is not ControllerKind.LEARNED_MPC:
            if any(value is not None for value in learned) or self.attested:
                raise ValueError("Fallback の counterfactual に ML の項目を入れない")
            if self.failure is not None or self.requested is None:
                raise ValueError("Fallback の counterfactual には要求した demand が要る")
            return self._check_attestation()
        if (self.requested is None) != (self.failure is not None):
            raise ValueError("Learned MPC の counterfactual は提案か失敗のどちらか一方にする")
        if self.failure is not None:
            if any(
                value is not None
                for value in (
                    self.optimizer_status,
                    self.model_version,
                    self.inference_id,
                    self.artifact_sha256,
                    self.plan,
                    self.prediction,
                    self.cost_total,
                    self.baseline_cost_total,
                )
            ):
                raise ValueError("提案を作れなかった counterfactual に推論の記録を残さない")
            if self.attested:
                raise ValueError("提案の無い counterfactual を attested にしない")
            return self._check_attestation()
        if self.optimizer_status is None or self.model_version is None:
            raise ValueError("Learned MPC の counterfactual には optimizer_status と版が要る")
        if self.inference_id is None or self.artifact_sha256 is None:
            raise ValueError("Learned MPC の counterfactual には推論と artifact の識別子が要る")
        return self._check_solution()

    def _check_solution(self) -> Self:
        """解を持つ counterfactual の中身が、その解と噛み合っているか。"""
        solved = self.optimizer_status is OptimizerStatus.OK
        if (self.cost_total is None) != (self.baseline_cost_total is None):
            raise ValueError("counterfactual のコストは Baseline と対で記録する")
        if not solved:
            # timeout / error の tick は解を持たない。Baseline の値を「MPC の解」として残さない。
            if self.prediction is not None or self.cost_total is not None or self.plan is not None:
                raise ValueError("optimizer_status が ok でない counterfactual に解を残さない")
            return self._check_attestation()
        if self.prediction is None or self.plan is None:
            raise ValueError("ok の counterfactual には候補 plan と予測 future が要る（#90）")
        if self.cost_total is None or self.baseline_cost_total is None:
            raise ValueError("ok の counterfactual にはコストと Baseline コストが要る")
        if self.cost_total > self.baseline_cost_total:
            raise ValueError("採用した解のコストが Baseline を上回っている")
        prediction = self.prediction
        if (prediction.model_version, prediction.inference_id, prediction.artifact_sha256) != (
            self.model_version,
            self.inference_id,
            self.artifact_sha256,
        ):
            # 別の推論の予測を貼り替えて「当たっていた」記録にさせない。
            raise ValueError("counterfactual の予測が別の推論のもの")
        self._check_plan(self.plan, prediction)
        return self._check_attestation()

    def _check_plan(self, plan: ShadowActionPlan, prediction: ShadowPrediction) -> None:
        """記録した要求・候補 plan・予測が**同じ候補**を指しているか（決定記録 0052 §2.2）。

        版・推論・artifact が合っていても、それだけでは「どの候補 action に対する予測か」は
        決まらない。同じ tick の候補は step の刻みが同じなので、plan の識別子まで照らさないと
        plan A の要求に plan B の予測を貼れてしまう。**digest を数え直して閉じる。**
        """
        if plan.first != self.requested:
            raise ValueError("counterfactual の requested が候補 plan の最初の step と違う")
        if plan.offsets_ms != tuple(target.offset_ms for target in prediction.targets):
            raise ValueError("counterfactual の候補 plan と予測の step 列が違う")
        if plan.digest() != prediction.plan_digest:
            raise ValueError("counterfactual の予測が別の候補 plan のもの")

    def _check_attestation(self) -> Self:
        """裏付けのない confidence / ood を counterfactual に残さないこと（0050 §2.5 と同じ）。"""
        if self.attested != (self.confidence is not None):
            raise ValueError("attested な counterfactual だけが confidence を持つ")
        if (self.confidence is None) != (self.ood is None):
            raise ValueError("counterfactual の confidence と ood は一緒に記録する")
        if self.attested and self.inference_id is None:
            raise ValueError("attested な counterfactual には推論の識別子が要る")
        if self.ood and self.confidence != 0.0:
            raise ValueError("OOD の confidence は 0 にする（決定記録 0050 §2.2）")
        return self


class ShadowRecord(_Frozen):
    """1 tick の Shadow Mode の記録（#90 / 決定記録 0053）。

    **適用した値と counterfactual を同じ枠に入れない。** ``applied_*`` は実際に Fan へ届いた
    結果、``counterfactuals`` は届かなかった提案である。同じ制御器が両方に現れることはない。
    """

    schema_version: Literal[1] = SHADOW_SCHEMA_VERSION
    tick_id: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    authority_stage: AuthorityStage
    applied_controller: ControllerKind | None
    """この tick の requested を作った制御器。``ControlState`` と揃える。"""
    applied_effective: PerZone[Demand]
    """実際に Fan へ渡った effective demand。比較の基準になる。"""
    counterfactuals: tuple[ShadowCounterfactual, ...] = Field(
        min_length=1, max_length=MAX_SHADOW_COUNTERFACTUALS
    )
    supervisor: SupervisorOutput | None = None
    """counterfactual が前提にした運転戦略（strategy / weights / target band）。"""

    @model_validator(mode="after")
    def _counterfactuals_are_never_the_applied_decision(self) -> Self:
        controllers = tuple(item.controller for item in self.counterfactuals)
        if len(set(controllers)) != len(controllers):
            raise ValueError("同じ制御器の counterfactual を1 tick に2つ残さない")
        if self.applied_controller is not None and self.applied_controller in controllers:
            # ここが破れると、適用した demand を「使わなかった提案」として読める記録になる。
            raise ValueError("適用した制御器を counterfactual として記録しない")
        if self.supervisor is not None:
            if self.supervisor.tick_id > self.tick_id:
                raise ValueError("shadow の Supervisor output を未来の tick にしない")
            if self.supervisor.tick_id == self.tick_id and self.supervisor.ts_ms != self.ts_ms:
                raise ValueError("同じ tick の Supervisor output の ts_ms を揃える")
        return self


class ControlConfigDigest(_Frozen):
    """この tick が使っていた検証済み設定（#103）の内容ハッシュ。

    **絶対 path も個体識別子も残さない**（AGENTS.md ルール10）。ここにあるのは、あとから
    「どの設定で回っていた tick か」を言えるだけの hash である。

    runtime v2（#81 / 決定記録 0073 §2.5 (c)）で ``air-balance.yaml`` の hash と、束ねた版・
    4ファイルそれぞれの schema version を足した。**v2 ではすべて必須、v1 ではどれも持たない**
    （検証は ``ControlTickRuntime`` が版を見て行う）。値は ``ControlConfig.sources`` から写す。
    """

    fan_hardware_sha256: Sha256Hex
    safety_sha256: Sha256Hex
    policy_sha256: Sha256Hex
    air_balance_sha256: Sha256Hex | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    control_config_version: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )
    fan_hardware_schema_version: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )
    safety_schema_version: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )
    policy_schema_version: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )
    air_balance_schema_version: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )

    @property
    def _v2_fields(self) -> tuple[object, ...]:
        return (
            self.air_balance_sha256,
            self.control_config_version,
            self.fan_hardware_schema_version,
            self.safety_schema_version,
            self.policy_schema_version,
            self.air_balance_schema_version,
        )

    @property
    def complete(self) -> bool:
        """runtime v2 の欄（4ファイルの版と SHA-256）をすべて持つか。"""
        return all(value is not None for value in self._v2_fields)

    @property
    def legacy(self) -> bool:
        """runtime v1 の形（3ファイルの SHA-256 だけ）か。"""
        return all(value is None for value in self._v2_fields)


CONTROL_TICK_RUNTIME_SCHEMA_VERSION: Literal[2] = 2
"""`ControlTickRuntime` の形の版。

- v2（#81 / 決定記録 0073 §2.5 (c)）: `config` に `air-balance.yaml` の SHA-256 と、束ねた版・
  4ファイルの schema version を足した。`ControlTick` v11 は v2 を要求する
"""


class ControlTickRuntime(_Frozen):
    """1 tick の**実行そのもの**の記録（#74 / 決定記録 0060 §2.4）。

    判断ではなく、判断を出した実行の条件を残す。別のログと突き合わせずに
    「この tick は締め切りに間に合っていたか」「どの設定と snapshot の形で回っていたか」を
    言えるようにするための欄である。

    ``duration_ms`` は**書き込みと検証まで**の所要時間で、decision trace の保存時間を含まない
    （0028 §2.6 の watchdog が数える区間と同じ）。
    """

    schema_version: Literal[1, 2] = CONTROL_TICK_RUNTIME_SCHEMA_VERSION
    tick_period_ms: int = Field(gt=0)
    deadline_ms: int = Field(gt=0)
    duration_ms: int = Field(ge=0)
    deadline_exceeded: bool
    snapshot_schema_version: int = Field(ge=1)
    config: ControlConfigDigest

    @model_validator(mode="after")
    def _overrun_matches_the_recorded_duration(self) -> Self:
        if self.schema_version >= 2 and not self.config.complete:
            # 版が中身を表さない記録を作らない（0060 §2.4）。v2 を名乗りながら
            # 4ファイルの版と hash が欠けていると、どの設定で回っていたかを言えない。
            raise ValueError("runtime v2 の config には4ファイルの版と SHA-256 が要る")
        if self.schema_version < 2 and not self.config.legacy:
            raise ValueError("4ファイルの版と hash を記録する runtime は schema version 2 にする")
        if self.deadline_exceeded != (self.duration_ms > self.deadline_ms):
            # 超過の有無を、同じ行に残した所要時間と食い違わせない。片方だけを書き換えて
            # 「遅れていない tick」に見せられる欄を作らないため。
            raise ValueError("deadline_exceeded は記録した duration と締め切りから決まる")
        if self.deadline_ms > self.tick_period_ms:
            raise ValueError("tick の締め切りを周期より長くしない（0028 §2.6）")
        return self


class SafetyProvenance(_Frozen):
    """Critical Safety の裁定が**どの前提で出たか**の記録（`ControlTick` v9。#78）。

    `CriticalSafetyDecision` の `disabled_inputs` と `config_is_provisional` をそのまま写す。
    判断そのもの（floor / forced Max / fault）は `zones` と `faults` が持ち、ここは
    「その判断が確定値で出たか」「どの入力を設定で外していたか」だけを残す。
    これが無いと、T_SENSOR を外していた期間や暫定の閾値で回っていた期間の trace を、
    確定値で全入力を見ていた trace と区別できない（docs/critical-safety.md）。

    ``config_is_provisional=false`` は「暫定値を1つも含まない Safety 設定で裁定した」ことだけを
    表す。設定不正時の Max の経路は Safety 設定を読まず、この記録も作らない
    （docs/critical-safety.md）。
    """

    schema_version: Literal[1] = 1
    disabled_inputs: tuple[Reason, ...]
    """設定で Critical の対象から外した入力と理由。外していなければ空。"""
    config_is_provisional: bool
    """Safety 設定に `status: provisional` の値が1つでもあったか（0028 §2.8）。"""


REGISTRY_ARTIFACT_KINDS: frozenset[str] = frozenset(
    {"thermal_model", "confidence_model", "supervisor_policy", "feature_transform"}
)
"""registry が production pointer を持つ artifact kind（`RegistryProvenance.production` の鍵）。

`coldaisle.control.model_registry.ArtifactKind` の値。schema は registry を import しないので
値で持ち、食い違いは tests/test_model_registry.py で止める。
"""

REGISTRY_POINTER_CHANGE_EVENTS: frozenset[str] = frozenset({"promoted", "rolled_back"})
"""production pointer を動かす registry event（決定記録 0062 §2.5 の `pointer_changes`）。

`coldaisle.control.model_registry.RegistryEventKind` の値のうち2つ。schema は registry を
import しない（registry が schema を import する向きだけにする）ので、値で持つ。
一致は試験が確かめる。
"""

_REGISTRY_THERMAL_MODEL_KIND = "thermal_model"
"""Learned MPC が制御に束縛する artifact の kind（決定記録 0052 §2.1 の2）。"""

_REGISTRY_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.-]*$"
"""Registry の kind・model id・actor の形（`model_registry._IDENTIFIER_PATTERN` と同じ）。"""


_REGISTRY_REF_LABEL_MAX_LENGTH = 240
"""`ArtifactRef.key`（`<kind>/<model_id>/<version>`）の長さの上限。

各部の上限（kind は語彙・model id 120・version 80）と区切りの和を下回らない値。
毎 tick 載る欄なので、識別子であっても長さを schema で閉じる（決定記録 0075 §2.2）。
"""


def registry_reason_sha256(reason: str) -> str:
    """registry の audit の `reason` を、毎 tick の塊に載せる digest にする（決定記録 0075）。

    UTF-8 の bytes の SHA-256（小文字16進）。全文の正本は registry の audit であり
    （`coldaisle-registry audit --pointer-changes`）、digest はそれと照合するためだけに置く。
    """
    return hashlib.sha256(reason.encode("utf-8")).hexdigest()


class RegistryPointerChange(_Frozen):
    """production pointer を成立させた変更の、path を含まない記録（決定記録 0071 §2.5 / 0075）。

    `RegistryAuditEvent.trace_metadata()` の欄を**全部**持つ。欄を落とすと、読む側が
    「記録されていない」と「起きていない」を区別できない（決定記録 0062 §2.5）。
    ただし**自由記述の `reason` だけは全文を持たず、`reason_sha256` に置き換える**
    （決定記録 0075。毎 tick 載るため、最大1000字の全文は保存量を押し上げる）。
    誰が・なぜ・いつ承認したかの正本は `registry.json` の audit であり、ここは写しである。
    `RegistryAuditEvent.tick_trace_metadata()` がこの形を作る。
    """

    registry_revision: int = Field(ge=1)
    occurred_at_ms: int = Field(ge=0)
    event: str
    artifact_kind: str = Field(pattern=_REGISTRY_IDENTIFIER_PATTERN, max_length=120)
    model_id: str = Field(pattern=_REGISTRY_IDENTIFIER_PATTERN, max_length=120)
    model_version: str = Field(min_length=1, max_length=80)
    actor: str = Field(pattern=_REGISTRY_IDENTIFIER_PATTERN, max_length=120)
    reason_sha256: Sha256Hex
    """audit の `reason` の UTF-8 の SHA-256（`registry_reason_sha256`）。全文は audit で引く。"""
    previous_artifact: str | None = Field(max_length=_REGISTRY_REF_LABEL_MAX_LENGTH)
    rollback_target: str | None = Field(max_length=_REGISTRY_REF_LABEL_MAX_LENGTH)
    approver: str | None
    approved_at_ms: int | None = Field(ge=0)
    approval_artifact_sha256: Sha256Hex | None

    @model_validator(mode="after")
    def _is_an_approved_pointer_change(self) -> Self:
        if self.event not in REGISTRY_POINTER_CHANGE_EVENTS:
            # 登録・検証・retire は production pointer を動かさない。pointer を成立させた
            # 変更として載せられるのは promotion / rollback だけ（決定記録 0062 §2.5）。
            raise ValueError("production pointer を成立させるのは promotion / rollback だけ")
        if self.approver is None or self.approved_at_ms is None:
            raise ValueError("promotion / rollback の記録には human approval が要る")
        if self.approval_artifact_sha256 is None:
            raise ValueError("承認した artifact の checksum が要る")
        if self.approver != self.actor:
            raise ValueError("pointer 変更の actor と approver を揃える")
        if self.approved_at_ms > self.occurred_at_ms:
            raise ValueError("未来の approval は記録できない")
        if self.rollback_target is not None and self.event != "promoted":
            raise ValueError("rollback_target は promotion の記録だけが持つ")
        return self


class RegistryProductionPointer(_Frozen):
    """1つの artifact kind について、tick が使っていた production pointer（決定記録 0071 §2.5）。

    production が無ければ両方 `None`。**欄は常に揃える**（値が無いときも欄を消さない）。
    """

    artifact_sha256: Sha256Hex | None
    """production artifact の checksum。production が無ければ `None`。"""
    established_by: RegistryPointerChange | None
    """その production を成立させた pointer 変更（最後の promotion / rollback）。"""

    @model_validator(mode="after")
    def _pointer_and_its_change_agree(self) -> Self:
        change = self.established_by
        if (self.artifact_sha256 is None) != (change is None):
            raise ValueError("production の checksum と、それを成立させた変更は一緒に記録する")
        if change is not None and change.approval_artifact_sha256 != self.artifact_sha256:
            # 承認は artifact の checksum に束縛されている（0062）。食い違う pointer は、
            # 承認していない bytes の下で判断したと読めてしまう。
            raise ValueError("production の checksum を承認した checksum と揃える")
        return self


class RegistryProvenance(_Frozen):
    """この tick の判断が**どの Model Registry の版の下で出たか**（`ControlTick` v10。#104）。

    決定記録 0071 §2.5。制御デーモンが起動時に持っていた registry の状態を**毎 tick** 写す。
    起動した tick だけに載せると、保持期間より長く動いたデーモンでは「どの昇格の下で
    動いているか」が trace から言えなくなる。

    - `revision`: registry の revision（audit の件数と一致し 1 から連続する）。
      **`None` は「この制御デーモンは registry を読んでいない」**（artifact を束縛していない構成）
      であり、`production` は空にする。`0` は「読んだが1件も記録が無い」で、両者を混ぜない
    - `production`: kind ごとの pointer。registry を読んだなら**全 kind を載せる**
      （production が無い kind も `None` の欄で残す）

    隣り合う trace の `revision` が飛んでいれば、その間の pointer 変更は
    `coldaisle-registry audit --pointer-changes` で引く。**trace は tick が使っていた pointer
    だけを持ち、変更の全件は持たない**（決定記録 0071 §2.5 / §5 #7）。
    """

    schema_version: Literal[1] = 1
    revision: int | None = Field(ge=0)
    production: dict[
        Annotated[str, Field(pattern=_REGISTRY_IDENTIFIER_PATTERN, max_length=120)],
        RegistryProductionPointer,
    ]

    @model_validator(mode="after")
    def _pointers_belong_to_this_revision(self) -> Self:
        if self.revision is None:
            if self.production:
                raise ValueError("registry を読んでいない tick に production pointer を載せない")
            return self
        if set(self.production) != REGISTRY_ARTIFACT_KINDS:
            # 欄を欠くと「production が無かった」と「記録されていない」を区別できない（0062 §2.5）。
            raise ValueError("registry を読んだ tick は全 kind の production pointer を載せる")
        change_revisions: list[int] = []
        for kind, pointer in self.production.items():
            change = pointer.established_by
            if change is None:
                continue
            if change.artifact_kind != kind:
                raise ValueError("production pointer の kind と、それを成立させた変更を揃える")
            if change.registry_revision > self.revision:
                raise ValueError("registry の revision より後の変更を production に載せない")
            change_revisions.append(change.registry_revision)
        if len(change_revisions) != len(set(change_revisions)):
            # 1つの audit event が動かす pointer は1つだけ。
            raise ValueError("1つの pointer 変更を複数の kind に載せない")
        return self

    @classmethod
    def unbound(cls) -> RegistryProvenance:
        """registry を読んでいない制御デーモンの記録（artifact を束縛していない構成）。"""
        return cls(revision=None, production={})


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
    """zone をまたいで比べる推定量（#75 / #81）。単位は EFU で、CFM ではない。

    v11 では ``applied_demand`` を ``air-balance.yaml`` の曲線で写した値（決定記録 0073
    §2.5 (a)）。Air Balance が無効・書き込み結果が無い・確認できていない・その tick の backend
    fault がある・有効な stall の zone は None（0 にしない）。
    """
    applied_demand: Demand | None = Field(default=None, exclude_if=lambda value: value is None)
    """Hardware Backend が PWM へ写す直前の demand（v11。決定記録 0073 §2.5 (a)）。

    ``effective`` に profile の制約（``minimum_stable_demand`` への引き上げ、未起動 zone の
    ``startup_demand``）を掛けた後の値。書き込み結果の無い tick と、``write_ok`` /
    ``readback_ok`` のどちらかが偽の tick は None。
    """
    hardware: HardwareReadback | None = None
    """まだ書き込んでいない tick（`STARTUP` の最初など）は None。"""

    @model_validator(mode="after")
    def _applied_demand_was_confirmed(self) -> Self:
        if self.applied_demand is not None and (
            self.hardware is None or not (self.hardware.write_ok and self.hardware.readback_ok)
        ):
            # 書けていない・確かめられていない指令を「適用した demand」として残さない。
            raise ValueError("applied_demand は write と readback を確認できた zone にだけ残す")
        return self


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


AIR_BALANCE_RECORD_SCHEMA_VERSION: Literal[1] = 1
"""`AirBalanceRecord` の形の版（決定記録 0073 §2.5 (b)）。"""

EffectiveFlowUnit = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
"""zone をまたいで比べる推定風量（EFU）。実測 CFM ではない。"""


class AirBalanceTraceState(StrEnum):
    """trace に残す Air Balance の状態。

    ``disabled`` 以外は ``coldaisle.control.air_balance.AirBalanceState`` と同じ値である。
    **``disabled`` は trace 側の型だけ**が持つ（決定記録 0073 §2.5 (b)）。モデルは無効のとき
    呼ばれないので、モデルの出力に ``disabled`` を返す経路は作らない。schema は
    air_balance を import しない（向きを一方にする）ので値で持ち、食い違いは試験で止める。
    """

    BALANCED = "balanced"
    INTAKE_HEAVY = "intake_heavy"
    EXHAUST_HEAVY = "exhaust_heavy"
    THERMALLY_LIMITED = "thermally_limited"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


class AirBalanceRecord(_Frozen):
    """1 tick の Air Balance の記録（`ControlTick` v11。#81 / 決定記録 0073 §2.5 (b)）。

    **「推定できなかった」（``enabled`` + ``unknown``）と「使っていなかった」（``disabled``）を
    同じ形にしない。** ``disabled`` は ``source.status: uncalibrated`` の起動だけが作り、
    q・比・熱の理由はすべて空である（未校正の曲線と閾値を記録に入れない）。

    ``enabled`` の q・吸排気量・比・状態は ``AirBalanceEstimate`` の不変条件をそのまま守る。
    熱の理由は風量と独立に残す（q が欠けた tick でも温度の超過を落とさない）。
    """

    schema_version: Literal[1] = AIR_BALANCE_RECORD_SCHEMA_VERSION
    status: Literal["enabled", "disabled"]
    disabled_reason: Literal["uncalibrated"] | None
    demand_basis: Literal["applied"] = "applied"
    """何の demand で評価したか。requested でも effective でもなく applied（0073 §2.3）。"""
    model_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=120)
    source_status: Literal["uncalibrated", "calibrated"]
    config_sha256: Sha256Hex
    q_front: EffectiveFlowUnit | None
    q_rear: EffectiveFlowUnit | None
    q_top: EffectiveFlowUnit | None
    estimated_intake: EffectiveFlowUnit | None
    estimated_exhaust: EffectiveFlowUnit | None
    balance_ratio: EffectiveFlowUnit | None
    state: AirBalanceTraceState
    thermal_limited: bool
    thermal_reasons: tuple[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)], ...]

    @classmethod
    def disabled(cls, *, model_id: str, config_sha256: str) -> AirBalanceRecord:
        """未校正で無効にして起動した tick の記録。どの未校正ファイルかだけを残す。"""
        return cls(
            status="disabled",
            disabled_reason="uncalibrated",
            model_id=model_id,
            source_status="uncalibrated",
            config_sha256=config_sha256,
            q_front=None,
            q_rear=None,
            q_top=None,
            estimated_intake=None,
            estimated_exhaust=None,
            balance_ratio=None,
            state=AirBalanceTraceState.DISABLED,
            thermal_limited=False,
            thermal_reasons=(),
        )

    @property
    def flows(self) -> PerZone[float | None]:
        """zone ごとの q（``ZoneRecord.estimated_flow`` と突き合わせる値）。"""
        return PerZone[float | None](front=self.q_front, rear=self.q_rear, top=self.q_top)

    @model_validator(mode="after")
    def _enabled_and_disabled_have_distinct_shapes(self) -> Self:
        if self.status == "disabled":
            if self.disabled_reason is None or self.source_status != "uncalibrated":
                raise ValueError("disabled の Air Balance は uncalibrated の理由と出どころを持つ")
            if any(
                value is not None
                for value in (
                    self.q_front,
                    self.q_rear,
                    self.q_top,
                    self.estimated_intake,
                    self.estimated_exhaust,
                    self.balance_ratio,
                )
            ):
                raise ValueError("disabled の Air Balance に q・吸排気量・比を残さない")
            if self.state is not AirBalanceTraceState.DISABLED:
                raise ValueError("disabled の Air Balance の state は disabled にする")
            if self.thermal_limited or self.thermal_reasons:
                raise ValueError("disabled の Air Balance に熱の理由を残さない")
            return self
        if self.disabled_reason is not None:
            raise ValueError("enabled の Air Balance に disabled_reason を付けない")
        if self.source_status != "calibrated":
            raise ValueError("enabled の Air Balance は calibrated の設定からだけ作る")
        if self.state is AirBalanceTraceState.DISABLED:
            raise ValueError("enabled の Air Balance の state を disabled にしない")
        # 以下は AirBalanceEstimate の不変条件と同じ（schema は air_balance を import しない）。
        ratio_available = (
            self.q_front is not None
            and self.q_rear is not None
            and self.q_top is not None
            and self.q_front > 0.0
        )
        if ratio_available != (self.balance_ratio is not None):
            raise ValueError("balance_ratio は3つの q があり q_front > 0 のときだけ持つ")
        if ratio_available:
            assert self.q_front is not None and self.q_rear is not None and self.q_top is not None
            if self.estimated_intake != self.q_front:
                raise ValueError("estimated_intake は q_front と一致させる")
            if self.estimated_exhaust != self.q_rear + self.q_top:
                raise ValueError("estimated_exhaust は q_rear + q_top と一致させる")
            assert self.balance_ratio is not None
            if self.balance_ratio != self.estimated_exhaust / self.q_front:
                raise ValueError("balance_ratio は estimated_exhaust / estimated_intake にする")
        elif self.estimated_intake is not None or self.estimated_exhaust is not None:
            raise ValueError("比を計算できないときは推定吸排気量も未確定にする")
        if (self.state is AirBalanceTraceState.UNKNOWN) == ratio_available:
            raise ValueError("q が使えるときだけ unknown 以外の状態にする")
        if self.thermal_limited != bool(self.thermal_reasons):
            raise ValueError("thermal_limited と thermal_reasons を一致させる")
        if ratio_available and self.thermal_limited != (
            self.state is AirBalanceTraceState.THERMALLY_LIMITED
        ):
            raise ValueError("q が使えるときは熱制約と thermally_limited を一致させる")
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


MODE_COMMAND_RECORD_SCHEMA_VERSION: Literal[1] = 1
"""`ModeCommandRecord` の形の版。"""

RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
"""`coldaisle-fand` の起動ごとの識別子の形（決定記録 0072 §2.7）。個体識別子を含まない乱数。"""


class ModeCommandRecord(_Frozen):
    """その tick に効いていた運転モードの出どころ（`ControlTick` v12。#74 / 決定記録 0072 §2.7）。

    - ``entry``: ``control_admin`` は管理ソケットの受け渡し口からモードを読む構成。
      ``none`` は入口を開いていない構成（入口の設定が不正・`SO_PEERCRED` が無い・試験の入口）で、
      ``command_id`` を持たない
    - ``command_id``: この tick のモードを決めた指令（``run_id`` との組で監査の表の行と対応する）。
      起動直後の ``AUTO``・lease 切れで戻った ``AUTO``・受付スレッドの死による ``MAX`` では None
    - ``manual_lease_expired_command_id``: **この tick で** lease が切れて ``AUTO`` へ戻した
      ``MANUAL`` の指令（0072 §2.4）
    - ``admin_receiver_dead``: 受付スレッドの死で ``MAX`` に倒している（0072 §2.2）。
      再起動まで毎 tick 真のまま
    """

    schema_version: Literal[1] = MODE_COMMAND_RECORD_SCHEMA_VERSION
    entry: Literal["none", "control_admin"]
    run_id: str | None = Field(default=None, pattern=RUN_ID_PATTERN)
    command_id: int | None = Field(default=None, ge=1)
    manual_lease_expired_command_id: int | None = Field(default=None, ge=1)
    admin_receiver_dead: bool = False

    @classmethod
    def without_entry(cls) -> ModeCommandRecord:
        """管理ソケットを開いていない構成の記録。"""
        return cls(entry="none")

    @model_validator(mode="after")
    def _one_source_per_tick(self) -> Self:
        if self.entry == "none":
            if (
                self.run_id is not None
                or self.command_id is not None
                or self.manual_lease_expired_command_id is not None
                or self.admin_receiver_dead
            ):
                raise ValueError("入口の無い構成は command_id・lease・受付スレッドの状態を持たない")
            return self
        if self.run_id is None:
            raise ValueError("control_admin の記録には run_id が要る（監査の行と突き合わせる）")
        if self.admin_receiver_dead and (
            self.command_id is not None or self.manual_lease_expired_command_id is not None
        ):
            # 死んだあとは受け渡し口を読まず、MANUAL も解除している（0072 §2.2）
            raise ValueError("受付スレッドの死による MAX は指令にも lease にも由来しない")
        if self.manual_lease_expired_command_id is not None and self.command_id is not None:
            raise ValueError("lease 切れで戻した AUTO は指令に由来しない")
        return self


AUTHORITY_RECORD_SCHEMA_VERSION: Literal[1] = 1
"""`AuthorityRecord` の形の版。"""


class AuthorityRecord(_Frozen):
    """その tick の制御権の出どころ（`ControlTick` v13。#92 / 決定記録 0072 §2.6 / 0057 §2.7）。

    **この tick の Gate が stage を読んだ時点**の写しである（tick の先頭で管理ソケットの降格を
    入れた後）。heartbeat の後に行う journal の読み直し・書き残しの結果は、次の tick の記録に現れる
    （効くのが次の tick からなので）。

    - ``entry``: ``journal`` は `authority.json`（`AuthorityRuntime`）から読む構成。``static`` は
      journal を持たない固定 stage の構成（試験・journal を配線しない入口）で、
      journal の欄を持たない
    - ``journal_stage`` / ``journal_revision``: 最後に読めた journal
    - ``config_ceiling``: 検証済み設定の上限（`fan-policy.yaml` の `authority_stage`）
    - ``unpersisted_ceiling``: この process が下げて、まだ journal が表していない上限。無ければ None
    - ``journal_unreadable``: 走行中に journal を読めなかったので ``SHADOW`` に下げている
      （書き残せるまで外さない。0072 §2.6）
    - ``command_id``: **この tick の先頭で**適用した管理ソケットの降格（`lower_authority` /
      `rollback_authority`）。``ModeCommandRecord.run_id`` との組で監査の表の行と対応する
    - ``persist_failure``: 直近の降格を書き残せなかった理由（0057 §2.6）

    **model version を含めない**（0057 §2.7。stage と model は独立に残す）。
    """

    schema_version: Literal[1] = AUTHORITY_RECORD_SCHEMA_VERSION
    entry: Literal["static", "journal"]
    journal_stage: AuthorityStage | None = None
    journal_revision: int | None = Field(default=None, ge=0)
    config_ceiling: AuthorityStage
    unpersisted_ceiling: AuthorityStage | None = None
    journal_unreadable: bool = False
    command_id: int | None = Field(default=None, ge=1)
    persist_failure: Reason | None = None

    @model_validator(mode="after")
    def _describes_one_source(self) -> Self:
        if (self.journal_stage is None) != (self.journal_revision is None):
            raise ValueError("journal の stage と revision は一緒に記録する")
        if self.entry == "static":
            if self.journal_stage is not None or self.journal_unreadable:
                raise ValueError("journal を持たない構成は journal の欄を持たない")
            if self.persist_failure is not None:
                raise ValueError("journal を持たない構成は書き残しに失敗しない")
        elif self.journal_stage is None:
            raise ValueError("journal の構成は journal の stage を記録する")
        if self.unpersisted_ceiling is AuthorityStage.FULL:
            # FULL の上限は「上限なし」と同じ事実。2通りの書き方を作らない
            raise ValueError("上限の無い状態は None で表す")
        if self.journal_unreadable and self.unpersisted_ceiling is not BASELINE_STAGE:
            raise ValueError("journal を読めない間は SHADOW の上限を持つ（0072 §2.6）")
        return self

    def ceiling(self) -> AuthorityStage:
        """記録した上限のうち最も低いもの。実効 stage はこれを超えない（0057 §2.2）。"""
        stages = [self.config_ceiling]
        if self.journal_stage is not None:
            stages.append(self.journal_stage)
        if self.unpersisted_ceiling is not None:
            stages.append(self.unpersisted_ceiling)
        return lowest_stage(*stages)


class ControlTick(_Frozen):
    """1 tick の判断の記録（decision trace。0028 §2.3）。#82 が保存し、#90 / #91 が読む。"""

    schema_version: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13] = SCHEMA_VERSION
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
    shadow: ShadowRecord | None = Field(default=None, exclude_if=lambda value: value is None)
    """適用しなかった提案の記録（v6。#90）。counterfactual が無い tick では None。"""
    runtime: ControlTickRuntime | None = Field(default=None, exclude_if=lambda value: value is None)
    """control loop の実行そのものの記録（v8。#74）。保存済みの v1〜v7 では None。"""
    safety_provenance: SafetyProvenance | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """Critical Safety の裁定の前提（v9。#78）。保存済みの v1〜v8 では None。"""
    registry: RegistryProvenance | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """tick が使っていた Model Registry の版（v10。#104 / 決定記録 0071 §2.5）。

    保存済みの v1〜v9 では None。registry を読んでいない構成の v10 は `None` ではなく
    `RegistryProvenance.unbound()`（`revision=None`）を持つ。
    """
    air_balance: AirBalanceRecord | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """Air Balance の記録（v11。#81 / 決定記録 0073 §2.5 (b)）。保存済みの v1〜v10 では None。

    無効（未校正）の起動も ``None`` ではなく ``AirBalanceRecord.disabled()`` を持つ。
    """
    mode_command: ModeCommandRecord | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """その tick に効いていたモードの出どころ（v12。#74 / 決定記録 0072 §2.7）。

    保存済みの v1〜v11 では None。入口を開いていない構成の v12 も ``None`` ではなく
    ``ModeCommandRecord.without_entry()`` を持つ。
    """
    authority: AuthorityRecord | None = Field(default=None, exclude_if=lambda value: value is None)
    """その tick の制御権の出どころ（v13。#92 / 決定記録 0072 §2.6）。保存済みの v1〜v12 では None。

    journal を持たない構成の v13 も ``None`` ではなく ``entry="static"`` の記録を持つ。
    """
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
        if self.runtime is None:
            if self.schema_version >= 8:
                # **版が中身を表さない記録を作らない。** v8 を名乗りながら v8 を定義する欄が
                # 無いと、読む側は版を見ても何が入っているか言えない。
                raise ValueError("v8 の ControlTick には runtime が要る")
        elif self.schema_version < 8:
            raise ValueError("runtime を記録する ControlTick は schema version 8 にする")
        if self.safety_provenance is None:
            if self.schema_version >= 9:
                # v8 の runtime と同じく、版が中身を表さない記録を作らない。欄が無いと
                # 「T_SENSOR を外していたか」「暫定値で回っていたか」を言えない。
                raise ValueError("v9 の ControlTick には safety_provenance が要る")
        elif self.schema_version < 9:
            raise ValueError("safety_provenance を記録する ControlTick は schema version 9 にする")
        if self.registry is None:
            if self.schema_version >= 10:
                # v9 と同じく、版が中身を表さない記録を作らない。欄が無いと「どの production
                # pointer の下で出た判断か」を言えない（決定記録 0071 §2.5）。
                raise ValueError("v10 の ControlTick には registry が要る")
        elif self.schema_version < 10:
            raise ValueError("registry を記録する ControlTick は schema version 10 にする")
        self._check_air_balance()
        self._check_mode_command()
        self._check_authority()
        self._check_model_gate()
        self._check_registry_binds_model()
        self._check_shadow()
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

    @property
    def applied_model_artifact(self) -> str | None:
        """**この tick の requested を実際に作った** model artifact（#159）。

        返すのは、裏づけのある判断（`attested`）で Learned MPC が選ばれ、かつ artifact を
        記録していた tick の hash だけである。次のどれかなら `None`、すなわち
        **「artifact 不明」**として扱う（昇格の証拠に使わない。fail closed）。

        - `model_gate` の無い tick（提案が無かった / Fallback で回していた /
          `model_gate` を持たない v1〜v4 の trace）
        - 裏づけの無い判断（`attested` でない）
        - Learned MPC を選ばなかった tick（提案は counterfactual に残る）
        - artifact の欄を持たない保存済みの v1〜v6 の trace

        **「言えない」と「そうではなかった」は別物である。** 適用した tick かどうかは
        `applied_artifact_unknown` が別に答える。
        """
        gate = self.model_gate
        if gate is None or not gate.attested or not gate.learned_selected:
            return None
        return gate.artifact_sha256

    @property
    def applied_artifact_unknown(self) -> bool:
        """Learned MPC を**適用したのに** artifact を言えない tick か。

        保存済みの v1〜v6 がこれに当たる。**「記録が無いだけ」と「束縛できた」を混ぜない**
        ために、数えられる形で分けて持つ（#91 / #92 が部分的な証拠を完全として扱わないため）。

        **判断の起点は `model_gate` ではなく `ControlState` である**（codex #4057527947）。
        `model_gate` を必須にしたのは v5 からで、**v1〜v4 は Learned MPC を適用した tick でも
        `model_gate` を持たない。** gate の有無から数えると、そういう tick が
        「artifact 不明」にも「束縛できた」にも数えられず、**欠けていること自体が消える。**
        `#92` の「適用 arm すべてに不明が無いこと」が、記録の無い区間を素通りしてしまう。

        `active_controller` と `model_gate.learned_selected` の一致は v5 以降で schema が
        要求しているので、起点を `ControlState` にしても v5 以降の意味は変わらない。
        """
        if self.state.active_controller is not ControllerKind.LEARNED_MPC:
            return False
        return self.applied_model_artifact is None

    def _check_air_balance(self) -> None:
        """v11 の ``air_balance`` と zone の風量・runtime の設定 hash が同じ事実を指すか。"""
        record = self.air_balance
        runtime = self.runtime
        if self.schema_version < 11:
            if record is not None:
                raise ValueError("air_balance を記録する ControlTick は schema version 11 にする")
            if runtime is not None and runtime.schema_version >= 2:
                raise ValueError("runtime v2 を記録する ControlTick は schema version 11 にする")
            if any(self.zones.get(zone).applied_demand is not None for zone in Zone):
                raise ValueError("applied_demand を記録する ControlTick は v11 にする")
            return
        if record is None:
            # 版が中身を表さない記録を作らない（0060 §2.4）。
            raise ValueError("v11 の ControlTick には air_balance が要る")
        assert runtime is not None  # v8 以降は必須（上で検証済み）
        if runtime.schema_version < 2:
            raise ValueError("v11 の ControlTick には runtime v2 が要る")
        if record.config_sha256 != runtime.config.air_balance_sha256:
            raise ValueError("air_balance.config_sha256 を runtime の air-balance.yaml と揃える")
        for zone in Zone:
            zone_record = self.zones.get(zone)
            flow = record.flows.get(zone)
            if zone_record.estimated_flow != flow:
                # zone ごとに突き合わせるので、Rear と Top の入れ替わりも検出できる。
                raise ValueError(f"{zone.value}: estimated_flow を air_balance の q と揃える")
            hardware = zone_record.hardware
            confirmed = hardware is not None and hardware.write_ok and hardware.readback_ok
            if confirmed and zone_record.applied_demand is None:
                # FanHardwareResult と同じ同値関係（書けて読み戻せた ⇔ applied がある）を
                # v11 の trace でも守る（codex #4134968263）。欠けを許すと、確認済みの zone を
                # 「書き込みを確認できていない」と読ませてしまう。v10 以前は欄が無いので見ない。
                raise ValueError(
                    f"{zone.value}: write と readback を確認できた v11 の zone には"
                    " applied_demand が要る"
                )
            if zone_record.applied_demand is None and flow is not None:
                raise ValueError(f"{zone.value}: applied demand の無い zone に風量を残さない")
            if flow is not None and any(
                fault.code is FaultCode.TACH_STALL and fault.zone is zone for fault in self.faults
            ):
                # 回っていない Fan に風量を書かない（0073 §2.5 (a)）。
                raise ValueError(f"{zone.value}: stall の zone に風量を残さない")

    def _check_mode_command(self) -> None:
        """v12 の ``mode_command`` が ``ControlState`` のモードと同じ事実を指すか。"""
        record = self.mode_command
        if record is None:
            if self.schema_version >= 12:
                # 版が中身を表さない記録を作らない（0060 §2.4）。
                raise ValueError("v12 の ControlTick には mode_command が要る")
            return
        if self.schema_version < 12:
            raise ValueError("mode_command を記録する ControlTick は schema version 12 にする")
        mode = self.state.operating_mode
        if record.admin_receiver_dead and mode is not OperatingMode.MAX:
            raise ValueError("受付スレッドの死を記録する tick は MAX にする（0072 §2.2）")
        if record.manual_lease_expired_command_id is not None and mode is not OperatingMode.AUTO:
            raise ValueError("lease 切れを記録する tick は AUTO へ戻す（0072 §2.4）")
        if (
            record.entry == "control_admin"
            and record.command_id is None
            and not record.admin_receiver_dead
            and mode is not OperatingMode.AUTO
        ):
            # 指令に由来しないモードは、起動直後と lease 切れの AUTO だけ（0028 §2.5 (a)）
            raise ValueError("指令に由来しない AUTO 以外のモードは記録できない")

    def _check_authority(self) -> None:
        """v13 の ``authority`` が ``ControlState`` の stage を超えない上限を指すか。"""
        record = self.authority
        if record is None:
            if self.schema_version >= 13:
                # 版が中身を表さない記録を作らない（0060 §2.4）。
                raise ValueError("v13 の ControlTick には authority が要る")
            return
        if self.schema_version < 13:
            raise ValueError("authority を記録する ControlTick は schema version 13 にする")
        if stage_rank(self.state.authority_stage) > stage_rank(record.ceiling()):
            # 実効 stage は journal・設定・この process の上限の最小を超えない（0057 §2.2）
            raise ValueError("authority_stage が記録した上限を超えている")

    def _check_model_gate(self) -> None:
        """v5 の ``model_gate`` が ControlState と同じ判断を指しているか。"""
        state = self.state
        gate = self.model_gate
        if gate is None:
            if self.schema_version >= 5:
                if state.active_controller is ControllerKind.LEARNED_MPC:
                    raise ValueError("v5 以降で Learned MPC を使った tick には model_gate が要る")
                if (state.model_version, state.model_confidence, state.model_ood) != (
                    None,
                    None,
                    None,
                ):
                    # 裏付けの記録が無いのに ML の数値だけが残ると、評価（#90 / #91）と
                    # stage の判断（#92）が根拠の無い値を読む。
                    raise ValueError(
                        "v5 で model_gate の無い tick に ML の model_version / confidence / ood "
                        "を残さない"
                    )
            return
        if self.schema_version < 5:
            raise ValueError("model_gate を記録する ControlTick は schema version 5 にする")
        if self.schema_version < 7:
            if gate.schema_version >= 2:
                raise ValueError("artifact を記録する ControlTick は schema version 7 にする")
        elif gate.schema_version < 2:
            # v7 の tick が v1 の判断を抱えられると、「artifact の欄が無い」記録を
            # **新しい trace でも**作れてしまう。
            raise ValueError("v7 の ControlTick には schema version 2 の model_gate が要る")
        elif gate.attested and gate.artifact_sha256 is None:
            # **新しい trace で「artifact 不明」を作れないようにする。** 作れると、
            # 束縛できる形に直したあとも、欄を空けるだけで束縛を外せてしまう。
            # 保存済みの v1〜v6 だけが「artifact 不明」でありうる。
            raise ValueError("v7 の裏づけのある model_gate には artifact_sha256 が要る")
        if (state.model_version, state.model_confidence, state.model_ood) != (
            gate.model_version,
            gate.confidence,
            gate.ood,
        ):
            # 裏付けの無い tick では gate 側が None なので、ControlState にも数値を残さない。
            raise ValueError(
                "ControlState の model_version / confidence / ood を model_gate と揃える"
            )
        if state.authority_stage is not gate.authority_stage:
            raise ValueError("ControlState と model_gate の authority stage を揃える")
        if gate.learned_selected != (state.active_controller is ControllerKind.LEARNED_MPC):
            raise ValueError("model_gate.learned_selected と active_controller が食い違っている")
        if state.operating_mode in {OperatingMode.MANUAL, OperatingMode.CALIBRATION}:
            # 人が requested を決める mode では Gate が動かない（0028 §2.5 (a)）。
            raise ValueError("MANUAL / CALIBRATION の tick に model_gate を残さない")

    def _check_registry_binds_model(self) -> None:
        """v10 の ``registry`` が、この tick の裏づけのある model artifact を production に持つか。

        Learned MPC を制御へ束縛できるのは thermal_model の**production pointer そのもの**
        だけである（決定記録 0052 §2.1 の3）。制御デーモンは起動時に読んだ registry の版を
        毎 tick 載せる（0071 §2.5）ので、裏づけのある `model_gate` の artifact は、その版の
        thermal_model の production と一致していなければならない。
        一致を求めないと、registry を読んでいない記録（`unbound`）や別の版の snapshot と
        組み合わさり、**判断を別の昇格の下で出たものとして記録できてしまう**（codex #4133594515）。

        RL Supervisor の識別はここで照合しない。shadow は production でない candidate を
        回す（0061 §2.4 の `for_shadow`）ので、production との一致は要件にならない。
        """
        registry = self.registry
        gate = self.model_gate
        if registry is None or gate is None or not gate.attested or gate.artifact_sha256 is None:
            return
        if registry.revision is None:
            raise ValueError("裏づけのある model artifact を記録する tick は registry を読んでいる")
        pointer = registry.production[_REGISTRY_THERMAL_MODEL_KIND]
        if pointer.artifact_sha256 != gate.artifact_sha256:
            raise ValueError(
                "model_gate の artifact を registry の thermal_model の production と揃える"
            )

    def _check_shadow(self) -> None:
        """v6 の ``shadow`` が、同じ tick の適用結果と同じ判断を指しているか（#90）。"""
        shadow = self.shadow
        if shadow is None:
            return
        if self.schema_version < 6:
            raise ValueError("shadow を記録する ControlTick は schema version 6 にする")
        state = self.state
        if (shadow.tick_id, shadow.ts_ms) != (self.tick_id, self.ts_ms):
            raise ValueError("ShadowRecord の tick_id / ts_ms を ControlTick と揃える")
        if shadow.authority_stage is not state.authority_stage:
            raise ValueError("ShadowRecord と ControlState の authority stage を揃える")
        if state.operating_mode in {OperatingMode.MANUAL, OperatingMode.CALIBRATION}:
            # 人が requested を決める mode では「使わなかった提案」の比較対象が無い。
            raise ValueError("MANUAL / CALIBRATION の tick に shadow を残さない")
        if shadow.applied_controller is not state.active_controller:
            raise ValueError("ShadowRecord の applied_controller と active_controller を揃える")
        for zone in Zone:
            if shadow.applied_effective.get(zone) != self.zones.get(zone).demand.effective:
                # ここが揃っていないと、比較の基準が実際に掛かった風量と別物になる。
                raise ValueError(
                    f"{zone.value}: ShadowRecord の applied_effective が effective と違う"
                )
        self._check_shadow_supervisor(shadow)
        self._check_shadow_learned(shadow)

    def _check_shadow_supervisor(self, shadow: ShadowRecord) -> None:
        """counterfactual が前提にした戦略が、この tick に実在した出力か。"""
        if shadow.supervisor is None:
            return
        if self.supervisor is None:
            raise ValueError("Supervisor decision の無い tick に shadow の戦略を残さない")
        decision = self.supervisor
        outputs = tuple(
            evaluation.output
            for evaluation in (decision.active, decision.fallback, decision.shadow)
            if evaluation is not None and evaluation.output is not None
        )
        if all(output != shadow.supervisor for output in outputs):
            raise ValueError("shadow の Supervisor output がこの tick の decision に無い")

    def _check_shadow_learned(self, shadow: ShadowRecord) -> None:
        """Learned MPC の counterfactual が、Gate の裏付けと同じ推論を指しているか。"""
        learned = [
            item for item in shadow.counterfactuals if item.controller is ControllerKind.LEARNED_MPC
        ]
        if not learned:
            return
        item = learned[0]
        gate = self.model_gate
        if gate is None:
            if item.attested:
                # 裏付けの記録が無い tick に attested な counterfactual があると、
                # 評価（#91）が Gate の判定を経ていない confidence を読む。
                raise ValueError("model_gate の無い tick に attested な counterfactual を残さない")
            return
        if item.inference_id is not None and item.inference_id != gate.inference_id:
            raise ValueError("counterfactual と model_gate が別の推論を指している")
        if item.attested != gate.attested:
            raise ValueError("counterfactual の attested を model_gate と揃える")
        if item.attested and (item.confidence, item.ood) != (gate.confidence, gate.ood):
            raise ValueError("counterfactual の confidence / ood を model_gate と揃える")
        if item.model_version is not None and item.model_version != gate.model_version:
            raise ValueError("counterfactual の model_version を model_gate と揃える")
        if (
            gate.artifact_sha256 is not None
            and item.artifact_sha256 is not None
            and item.artifact_sha256 != gate.artifact_sha256
        ):
            # 同じ tick の2つの記録が別の artifact を名乗ると、あとから読む側が
            # 「どの artifact の提案か」を決められない。
            raise ValueError("counterfactual と model_gate が別の artifact を指している")

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
