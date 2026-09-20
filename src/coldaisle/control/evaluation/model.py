"""Offline Evaluation の報告の型（#91 / 決定記録 0054）。

**適用された構成（factual arm）と、適用されなかった提案（counterfactual arm）を
別の型にする**（0054 §2.1 / §2.2）。counterfactual の型には温度・threshold margin・
ΔT・Air Balance・RPM の欄が**無い**。実行されていない提案の下で温度がどうなったかは
記録から言えないので、書ける形にしない。

**時刻はすべて証拠から来る。** 生成時刻の欄は無い（0054 §2.7）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.evaluation.stats import MetricSummary, SeriesShape
from coldaisle.control.schema import (
    AuthorityStage,
    ControllerKind,
    OperatingMode,
    Sha256Hex,
    SupervisorPolicyKind,
    Zone,
)

EVALUATION_REPORT_SCHEMA_VERSION: Literal[1] = 1
"""報告1つの形の版。**欄の意味を変えたら上げる。**"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


LegacyPolicyName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
"""v1 / v2 の trace に残る、実装固有の Supervisor policy 名。

鍵に入れるので形を縛る。**この形に収まらない値は「識別できない」として評価が拒む**
（黙って `none` に潰すと、違う policy の区間が同じ行に混ざる）。
"""


class CountedReason(_Frozen):
    """理由ごとの件数。**理由を捨てて合計だけにしない。**"""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    count: int = Field(ge=1)


class AppliedArm(_Frozen):
    """実際に適用された構成（0054 §2.1）。

    **Reactive Guard と Critical Safety は区別に使わない。** 適用された制御に
    それらを通らない経路は無いので、arm の差にはならない（AGENTS.md ルール2）。
    Guard / Safety の効きは `interventions` と `requested_demand` との差で見る。

    **`operating_mode` は鍵に入れる。** `MANUAL` / `CALIBRATION` では人が requested を
    決め、`MAX` では AUTO の上に forced_max が重なる（0028 §2.5 (a)）。同じ制御器でも
    適用された demand の出どころが違うので、混ぜると「人が回した区間」を制御器の実績に
    数えてしまう。`CounterfactualArm` が mode を持たないのは非対称だが**意図的**である
    （0054 §2.1）。提案そのものは mode に依らず作られ、`MANUAL` / `CALIBRATION` の tick は
    そもそも counterfactual を持てない（0053 §2.1）。`MAX` 中の提案が「適用値と違う」
    ことは coverage の `applied_action_differs` に出る。
    """

    controller: ControllerKind | None
    """requested を作った制御器。`MANUAL` / `CALIBRATION` では `None`。"""
    supervisor_policy: SupervisorPolicyKind | LegacyPolicyName | None
    """**記録された** Supervisor policy（`ControlState.supervisor_policy`）。

    v1 / v2 の trace は `SupervisorDecision` を持てず、policy は実装固有の自由文字列
    だった（`ControlState` の注記）。**その値をそのまま鍵に使う。** 列挙値へ狭めると、
    違う policy で回した古い区間が1つの arm に潰れ、別々の運転が同じ行に混ざる
    （決定記録 0054 §2.1 の「trace の証拠から決める」に反する）。
    """
    authority_stage: AuthorityStage
    operating_mode: OperatingMode

    @property
    def key(self) -> str:
        """報告の中で arm を指す文字列。**counterfactual とは別の名前空間。**"""
        controller = "none" if self.controller is None else self.controller.value
        policy = "none" if self.supervisor_policy is None else str(self.supervisor_policy)
        return (
            f"applied:{controller}+{policy}"
            f"@{self.authority_stage.value}/{self.operating_mode.value}"
        )


class CounterfactualArm(_Frozen):
    """適用されなかった提案（0054 §2.1）。"""

    controller: ControllerKind
    supervisor_policy: SupervisorPolicyKind | None
    authority_stage: AuthorityStage

    @property
    def key(self) -> str:
        policy = "none" if self.supervisor_policy is None else self.supervisor_policy.value
        return f"counterfactual:{self.controller.value}+{policy}@{self.authority_stage.value}"


class TemperatureReport(_Frozen):
    """温度と、絶対上限までの余裕（threshold margin）。

    `threshold_c` は `safety.yaml` の `absolute_temp_ceiling_c` である。
    **評価設定に写さない**（0054 §2.6）。
    """

    metric: str = Field(min_length=1, max_length=120)
    values: MetricSummary
    ticks: int = Field(ge=1)
    """この arm の tick 数。**`values.count` と並べて、証拠の薄さが見えるようにする。**"""
    sample_coverage: float = Field(ge=0.0, allow_inf_nan=False)
    """観測が結び付いた tick の割合。1000 tick に1件の観測で温度を語らせない。"""
    threshold_c: float = Field(allow_inf_nan=False)
    margin: MetricSummary
    """`threshold_c - 観測値`。**最小値が worst-case になる。**"""
    exceedances: int = Field(ge=0)
    """絶対上限以上だった観測の数。**Safety gate の第1段。**"""


class DeltaReport(_Frozen):
    """派生 ΔT（GPU 吸気上昇・ケース内予熱など）。式は `config/metrics.yaml` から引く。"""

    metric: str = Field(min_length=1, max_length=120)
    minuend: str = Field(min_length=1, max_length=120)
    subtrahend: str = Field(min_length=1, max_length=120)
    values: MetricSummary
    ticks: int = Field(ge=1)
    sample_coverage: float = Field(ge=0.0, allow_inf_nan=False)
    """裏づけのある tick の割合。**一部だけの要約を全体の要約として読ませない。**"""


class AirBalanceReport(_Frozen):
    """推定風量から出す吸排気比（`(rear + top) / front`）。

    比は `AirBalanceEstimate.balance_ratio` と同じ定義にする。trace の
    `estimated_flow` が3 zone 揃い、front が正のときだけ数える。
    """

    ratio: MetricSummary
    ticks_with_ratio: int = Field(ge=1)
    ticks_without_ratio: int = Field(ge=0)
    """推定風量が揃わず比を出せなかった tick。**0 で埋めずに数える。**"""


class ZoneSeriesReport(_Frozen):
    """zone ごとの時系列（demand / RPM）の水準・変動量・ハンチング。"""

    zone: Zone
    shape: SeriesShape


class InterventionReport(_Frozen):
    """Safety / Reactive Guard / Fallback の介入（0054 §2.1 の「分解」）。"""

    ticks: int = Field(ge=0)
    forced_max_ticks: int = Field(default=0, ge=0)
    safety_floor_ticks: int = Field(default=0, ge=0)
    guard_floor_ticks: int = Field(default=0, ge=0)
    guard_ceiling_ticks: int = Field(default=0, ge=0)
    ramp_down_ticks: int = Field(default=0, ge=0)
    guard_active_ticks: int = Field(default=0, ge=0)
    """Guard が floor / ceiling を出した tick（効いたかどうかとは別）。"""
    fallback_ticks: int = Field(default=0, ge=0)
    fallback_reasons: tuple[CountedReason, ...] = ()
    emergency_ticks: int = Field(default=0, ge=0)
    degraded_ticks: int = Field(default=0, ge=0)
    fault_ticks: int = Field(default=0, ge=0)
    fault_codes: tuple[CountedReason, ...] = ()
    safety_states: tuple[CountedReason, ...] = ()
    bound_zone_ticks: tuple[CountedReason, ...] = ()
    """`bound_by` ごとの **(tick, zone) の総数**。

    上の `*_ticks` は「その tick でどれかの zone が縛られたか」なので、**3 zone が同時に
    縛られても1と数える**。同時に起きたことを隠さないよう、総数も並べて持つ
    （決定記録 0054 §2.4 の「metric ごとの最大で隠さない」と同じ理由）。
    """

    @model_validator(mode="after")
    def _counts_fit_inside_the_ticks(self) -> Self:
        for name in (
            "forced_max_ticks",
            "safety_floor_ticks",
            "guard_floor_ticks",
            "guard_ceiling_ticks",
            "ramp_down_ticks",
            "guard_active_ticks",
            "fallback_ticks",
            "emergency_ticks",
            "degraded_ticks",
            "fault_ticks",
        ):
            value: int = getattr(self, name)
            if value > self.ticks:
                raise ValueError(f"{name} が tick 数を超えている")
        if sum(reason.count for reason in self.safety_states) != self.ticks:
            raise ValueError("safety_states の合計を tick 数と揃える")
        return self


class OptimizerReport(_Frozen):
    """Learned MPC optimizer の実績（latency / timeout / 評価回数）。"""

    samples: int = Field(ge=1)
    ok: int = Field(ge=0)
    timeout: int = Field(ge=0)
    error: int = Field(ge=0)
    timeout_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    error_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    latency_ms: MetricSummary | None = None
    evaluations: MetricSummary | None = None
    latency_complete: bool = False
    """**すべての sample に latency の記録があるか。**

    一部にしか記録が無い latency を「この arm の latency」として読ませない。
    50 回のうち1回だけ記録があれば、その1件が最大値になり gate を通ってしまう。
    """
    evaluations_complete: bool = False
    """すべての sample に評価回数の記録があるか。"""

    @model_validator(mode="after")
    def _statuses_add_up(self) -> Self:
        if self.ok + self.timeout + self.error != self.samples:
            raise ValueError("optimizer の status の合計を samples と揃える")
        for name, complete in (
            ("latency_ms", self.latency_complete),
            ("evaluations", self.evaluations_complete),
        ):
            summary: MetricSummary | None = getattr(self, name)
            if complete and (summary is None or summary.count != self.samples):
                raise ValueError(f"{name} を complete にできるのは全 sample に記録があるときだけ")
        if self.timeout_rate != self.timeout / self.samples:
            raise ValueError("timeout_rate を実際の件数から計算する")
        if self.error_rate != self.error / self.samples:
            raise ValueError("error_rate を実際の件数から計算する")
        return self


class CoverageReport(_Frozen):
    """**どれだけが採点できたか**（決定記録 0054 §2.3。0053 §5 の未決を閉じる）。

    Shadow の間は多くの予測が `unidentifiable` になる。それは制限ではなく、
    観測から言えることの範囲そのものである。**混ぜずに区分として出す。**
    """

    outcomes: int = Field(ge=0)
    scored: int = Field(ge=0)
    unidentifiable: int = Field(ge=0)
    identifiable_fraction: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    """`scored / outcomes`。**outcome が1つも無ければ `None`**（1.0 でも 0.0 でもない）。"""
    unidentifiable_reasons: tuple[CountedReason, ...] = ()
    outputs: int = Field(default=0, ge=0)
    matched_outputs: int = Field(default=0, ge=0)
    scored_outputs: int = Field(default=0, ge=0)
    unmatched_reasons: tuple[CountedReason, ...] = ()
    predicted_metrics: tuple[str, ...] = ()
    """予測に現れた metric（記録から取る）。"""
    unscored_metrics: tuple[str, ...] = ()
    """**一度も採点できなかった** metric。

    `scored` は「掛かっていた action を識別できた」ことしか言わない。識別できた
    outcome でも、ある metric の実測が一度も照合できなければ、その metric の誤差は
    **どこにも出てこない**。それを「問題が無かった」と読ませないために、採点できて
    いない metric を名指しで残し、`sufficient` にしない（決定記録 0054 §2.3）。
    """
    sufficient: bool
    """設定の下限を満たしたか。**満たさなければ予測指標を出さない。**"""

    @model_validator(mode="after")
    def _counts_are_consistent(self) -> Self:
        if self.scored + self.unidentifiable != self.outcomes:
            raise ValueError("scored と unidentifiable の合計を outcomes と揃える")
        if self.matched_outputs > self.outputs or self.scored_outputs > self.matched_outputs:
            raise ValueError("照合・採点した出力の数が全体を超えている")
        if (self.outcomes == 0) != (self.identifiable_fraction is None):
            raise ValueError("outcome が無い coverage に identifiable_fraction を付けない")
        if (
            self.identifiable_fraction is not None
            and self.identifiable_fraction != self.scored / self.outcomes
        ):
            raise ValueError("identifiable_fraction を実際の件数から計算する")
        if self.sufficient and self.scored == 0:
            # 採点できた outcome が1つも無いのに「足りている」とは言わせない。
            raise ValueError("scored が 0 の coverage を sufficient にしない")
        if set(self.unscored_metrics) - set(self.predicted_metrics):
            raise ValueError("採点できていない metric は、予測した metric の中から挙げる")
        if self.sufficient and self.unscored_metrics:
            # 1つでも採点できていない metric があれば、残りの metric だけで
            # underprediction の gate を通せてしまう。
            raise ValueError("採点できていない metric がある coverage を sufficient にしない")
        return self


class PredictionReport(_Frozen):
    """**`scored` な outcome だけ**から出す予測誤差（0054 §2.2）。

    `error = 実測 - 予測`。正なら実測のほうが高い（**underprediction**）で、
    冷却が足りない向きの外し方である。
    """

    metric: str = Field(min_length=1, max_length=120)
    scored_outputs: int = Field(ge=1)
    error: MetricSummary
    absolute_error: MetricSummary
    underprediction_outputs: int = Field(ge=0)
    underprediction_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    maximum_underprediction: float = Field(ge=0.0, allow_inf_nan=False)
    """最大の underprediction（1つも無ければ 0.0）。**cost gate が読む。**"""

    @model_validator(mode="after")
    def _rate_matches_the_counts(self) -> Self:
        if self.underprediction_outputs > self.scored_outputs:
            raise ValueError("underprediction の数が採点した出力を超えている")
        if self.underprediction_rate != self.underprediction_outputs / self.scored_outputs:
            raise ValueError("underprediction_rate を実際の件数から計算する")
        if (self.maximum_underprediction > 0.0) != (self.underprediction_outputs > 0):
            raise ValueError("underprediction の最大値と件数が食い違っている")
        return self


class AppliedArmReport(_Frozen):
    """適用された構成の実績（0054 §2.2。**実測を帰属させてよい唯一の側**）。"""

    arm: AppliedArm
    arm_key: str = Field(min_length=1, max_length=200)
    ticks: int = Field(ge=1)
    first_ts_ms: int = Field(ge=0)
    last_ts_ms: int = Field(ge=0)
    temperatures: tuple[TemperatureReport, ...] = ()
    deltas: tuple[DeltaReport, ...] = ()
    air_balance: AirBalanceReport | None = None
    effective_demand: tuple[ZoneSeriesReport, ...] = ()
    requested_demand: tuple[ZoneSeriesReport, ...] = ()
    """制御器が要求した値。`effective_demand` との差が Guard / Safety の効きである。"""
    rpm: tuple[ZoneSeriesReport, ...] = ()
    acoustic_cost: MetricSummary | None = None
    """適用された demand の approximate Acoustic Cost（無単位。dBA ではない）。"""
    interventions: InterventionReport
    last_attested_ts_ms: int | None = Field(default=None, ge=0)
    """この arm が**裏づけのある提案**を最後に出した tick の時刻。

    `last_ts_ms` は「この arm の区間の最後の tick」で、提案を作れなかった tick
    （model の読み込み失敗など）も含む。**区間の新しさと、証拠の新しさは別物である。**
    失敗の tick を1つ足すだけで古い実績が「新しい」ことにできてしまうため、
    裏づけ（`attested`）のある提案が実在した時刻を別に持つ（#92 / 決定記録 0057 §2.4）。

    裏づけのある提案が1つも無ければ `None`。**0054 の帰属規則は変えない。**
    記録から言える事実を1つ増やしただけで、どの実測をどの arm に帰属させるかは同じ。
    """
    gaps: tuple[CountedReason, ...] = ()
    """出せなかった指標と、その理由。**欄を埋め合わせない。**"""

    @model_validator(mode="after")
    def _arm_key_matches_the_arm(self) -> Self:
        if self.arm_key != self.arm.key:
            raise ValueError("arm_key を arm から導いた値と揃える")
        if self.last_ts_ms < self.first_ts_ms:
            raise ValueError("最後の tick を最初より前にしない")
        if self.interventions.ticks != self.ticks:
            raise ValueError("介入の集計を arm の tick 数と揃える")
        if self.last_attested_ts_ms is not None and not (
            self.first_ts_ms <= self.last_attested_ts_ms <= self.last_ts_ms
        ):
            raise ValueError("裏づけのある提案の時刻を arm の区間の外に置かない")
        return self


class CounterfactualArmReport(_Frozen):
    """適用されなかった提案の実績（0054 §2.2）。

    **温度・threshold margin・ΔT・Air Balance・RPM の欄を持たない。**
    掛かっていたのは別の action なので、その区間の実測をこの提案の成果にできない。
    """

    arm: CounterfactualArm
    arm_key: str = Field(min_length=1, max_length=200)
    ticks: int = Field(ge=1)
    first_ts_ms: int = Field(ge=0)
    last_ts_ms: int = Field(ge=0)
    proposals: int = Field(ge=0)
    """要求 demand を出せた tick。"""
    failures: tuple[CountedReason, ...] = ()
    """提案を作れなかった worker の失敗（理由別）。"""
    requested_demand: tuple[ZoneSeriesReport, ...] = ()
    acoustic_cost: MetricSummary | None = None
    optimizer: OptimizerReport | None = None
    cost_improvement: MetricSummary | None = None
    """`(baseline_cost - cost) / baseline_cost`。**optimizer が解を出した tick だけ。**"""
    safety_floor_shortfalls: int | None = Field(default=None, ge=0)
    """記録された Critical Safety floor を**下回った** (tick, zone) の数。

    実行されていない提案について**記録から言える安全側の指標**である（決定記録 0054 §2.4）。
    Safety が引き上げたはずの要求を何度出したかを数える。提案が1つも無ければ `None`。
    """
    maximum_floor_shortfall: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    """floor を最も大きく下回った幅（demand）。下回りが無ければ 0.0。"""
    attested_ticks: int = Field(default=0, ge=0)
    ood_ticks: int = Field(default=0, ge=0)
    confidence: MetricSummary | None = None
    """**裏づけ済み（`attested`）の counterfactual だけ**の confidence（0050 §2.5）。"""
    coverage: CoverageReport
    predictions: tuple[PredictionReport, ...] = ()
    """**coverage が足りているときだけ**入る（0054 §2.3）。"""
    last_attested_ts_ms: int | None = Field(default=None, ge=0)
    """この arm が**裏づけのある提案**を最後に出した tick の時刻。

    `last_ts_ms` は「この arm の区間の最後の tick」で、提案を作れなかった tick
    （model の読み込み失敗など）も含む。**区間の新しさと、証拠の新しさは別物である。**
    失敗の tick を1つ足すだけで古い実績が「新しい」ことにできてしまうため、
    裏づけ（`attested`）のある提案が実在した時刻を別に持つ（#92 / 決定記録 0057 §2.4）。

    裏づけのある提案が1つも無ければ `None`。**0054 の帰属規則は変えない。**
    記録から言える事実を1つ増やしただけで、どの実測をどの arm に帰属させるかは同じ。
    """
    gaps: tuple[CountedReason, ...] = ()

    @model_validator(mode="after")
    def _predictions_need_enough_identifiable_evidence(self) -> Self:
        if self.arm_key != self.arm.key:
            raise ValueError("arm_key を arm から導いた値と揃える")
        if self.last_ts_ms < self.first_ts_ms:
            raise ValueError("最後の tick を最初より前にしない")
        if self.last_attested_ts_ms is not None:
            if not (self.first_ts_ms <= self.last_attested_ts_ms <= self.last_ts_ms):
                raise ValueError("裏づけのある提案の時刻を arm の区間の外に置かない")
            if self.attested_ticks == 0:
                raise ValueError("裏づけの無い arm に裏づけのある提案の時刻を付けない")
            if self.proposals == 0:
                raise ValueError("提案の無い arm に裏づけのある提案の時刻を付けない")
        if self.predictions and not self.coverage.sufficient:
            # 少数の採点区間の平均を、全体の予測精度に見える形で並べない。
            raise ValueError("coverage が足りない counterfactual に予測指標を付けない")
        if (self.safety_floor_shortfalls is None) != (self.proposals == 0):
            # 提案があれば必ず数える。数えていない孔を「違反なし」と読ませない。
            raise ValueError("提案のある counterfactual には floor 下回りの数が要る")
        if (self.safety_floor_shortfalls is None) != (self.maximum_floor_shortfall is None):
            raise ValueError("floor 下回りの数と幅は一緒に記録する")
        if self.safety_floor_shortfalls is not None:
            assert self.maximum_floor_shortfall is not None
            if (self.maximum_floor_shortfall > 0.0) != (self.safety_floor_shortfalls > 0):
                raise ValueError("floor 下回りの数と幅が食い違っている")
        if self.ood_ticks > self.attested_ticks:
            # ood は裏づけ済みの判断からしか読めない（0050 §2.5）。
            raise ValueError("ood の tick 数が裏づけ済みの tick 数を超えている")
        if self.confidence is not None and self.confidence.count > self.attested_ticks:
            raise ValueError("confidence の件数が裏づけ済みの tick 数を超えている")
        scored_outputs = sum(item.scored_outputs for item in self.predictions)
        if scored_outputs > self.coverage.scored_outputs:
            raise ValueError("予測指標の出力数が coverage の採点数を超えている")
        return self


class GroupKind(StrEnum):
    """集計の切り口（0054 §2.5 / Issue の「評価原則」）。"""

    OVERALL = "overall"
    WORKLOAD_REGIME = "workload_regime"
    ROOM_TEMPERATURE_BAND = "room_temperature_band"


class GroupReport(_Frozen):
    """1つの切り口での arm ごとの実績。"""

    kind: GroupKind
    value: str = Field(min_length=1, max_length=64)
    applied: tuple[AppliedArmReport, ...] = ()
    counterfactual: tuple[CounterfactualArmReport, ...] = ()

    @model_validator(mode="after")
    def _arms_are_unique_and_disjoint(self) -> Self:
        applied = tuple(item.arm_key for item in self.applied)
        counterfactual = tuple(item.arm_key for item in self.counterfactual)
        if len(set(applied)) != len(applied) or len(set(counterfactual)) != len(counterfactual):
            raise ValueError("同じ arm を1つの group に2回入れない")
        if set(applied) & set(counterfactual):
            # 名前空間が重なると、適用実績と使わなかった提案を同じ行として読める。
            raise ValueError("適用と counterfactual の arm を同じ鍵にしない")
        return self


class SegmentRole(StrEnum):
    """時系列 split の役割（0054 §2.5）。**最後の segment が holdout。**"""

    CALIBRATION = "calibration"
    HOLDOUT = "holdout"


class SegmentReport(_Frozen):
    """1つの run の1区間。**この区間の evidence window の中だけから決まる。**"""

    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    index: int = Field(ge=0)
    role: SegmentRole
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    """**この時刻より先の入力は、この segment の指標に一切入らない**（決定記録 0054 §2.5）。"""
    ticks: int = Field(ge=1)
    """**tick の無い segment は作らない。** 空の holdout は何も判定しない gate になる。"""
    unattributed_observations: int = Field(default=0, ge=0)
    """どの tick からも許容幅の外にあり、どの arm にも帰属させなかった観測の数。

    **黙って落とさずに数える。** 多ければ、tick と観測の周期が噛み合っていない。
    """
    purged_outcomes: int = Field(ge=0)
    """証拠が `end_ms` を越えるため、どの segment にも入れなかった outcome。

    **落とさずに数える。** 「予測が無かった」と「この区間では確かめられない」を区別する。
    """
    groups: tuple[GroupReport, ...] = ()

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("segment は start_ms < end_ms にする")
        kinds = tuple((group.kind, group.value) for group in self.groups)
        if len(set(kinds)) != len(kinds):
            raise ValueError("同じ切り口・同じ値の group を2つ入れない")
        return self


class WorstCaseKind(StrEnum):
    """必ず並べる worst-case の種類（Issue の「worst-case run を必ず確認する」）。"""

    MINIMUM_THRESHOLD_MARGIN = "minimum_threshold_margin"
    MAXIMUM_TEMPERATURE = "maximum_temperature"
    CEILING_EXCEEDANCES = "ceiling_exceedances"
    """metric ごとの超過数。**どの metric が超えたか**を示す。"""
    SEGMENT_CEILING_EXCEEDANCES = "segment_ceiling_exceedances"
    """1つの segment で**すべての metric を足した**超過数。gate が読むのはこちら。"""
    EMERGENCY_TICKS = "emergency_ticks"
    MAXIMUM_UNDERPREDICTION = "maximum_underprediction"
    MINIMUM_IDENTIFIABLE_FRACTION = "minimum_identifiable_fraction"


class WorstCase(_Frozen):
    """1件の worst-case。**平均に埋もれさせない。**"""

    kind: WorstCaseKind
    run_id: str = Field(min_length=1, max_length=120)
    segment_index: int = Field(ge=0)
    role: SegmentRole
    arm_key: str = Field(min_length=1, max_length=200)
    metric: str | None = Field(default=None, min_length=1, max_length=120)
    value: float = Field(allow_inf_nan=False)


class RunProvenance(_Frozen):
    """1つの run の入力の素性。**同じ条件で比べたことを、あとから確かめられるように。**"""

    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    split_boundaries_ms: tuple[int, ...] = ()
    """時系列 split の境界。**条件の一部である。**

    同じ run を違う位置で切れば、holdout の中身も gate の判定も変わる。digest に
    入れないと、**違う切り方の比較が同じ `conditions_sha256` を名乗れてしまう**。
    """
    traces: int = Field(ge=0)
    observations: int = Field(ge=0)
    trace_sha256: Sha256Hex
    observation_sha256: Sha256Hex


class ObservedVersions(_Frozen):
    """入力に現れた版と識別子（Issue の受入基準「version / config を記録する」）。

    **記録されていたものだけを並べる。** 走らせた側の自己申告は入れない。
    """

    control_schema_versions: tuple[int, ...] = ()
    shadow_schema_versions: tuple[int, ...] = ()
    supervisor_policies: tuple[str, ...] = ()
    """`<policy>:<version>`。"""
    model_versions: tuple[str, ...] = ()
    model_artifacts: tuple[Sha256Hex, ...] = ()
    authority_stages: tuple[str, ...] = ()
    operating_modes: tuple[str, ...] = ()


class EvaluationProvenance(_Frozen):
    """再現に要る入力の素性。**絶対 path も生成時刻も持たない**（0021 / 0054 §2.7）。"""

    evaluation_config_sha256: Sha256Hex
    fan_hardware_config_sha256: Sha256Hex
    safety_config_sha256: Sha256Hex
    fan_policy_config_sha256: Sha256Hex
    metric_catalog_sha256: Sha256Hex
    acoustic_config_sha256: Sha256Hex | None = None
    absolute_temp_ceiling_c: float = Field(allow_inf_nan=False)
    outcome_match_tolerance_ms: int = Field(ge=0)
    applied_demand_tolerance: float = Field(ge=0.0, lt=1.0, allow_inf_nan=False)
    shadow_export_schema_version: int = Field(ge=1)
    runs: tuple[RunProvenance, ...] = Field(min_length=1)
    versions: ObservedVersions
    conditions_sha256: Sha256Hex
    """入力と設定をまとめた識別子。**これが同じなら同じ条件で比べている。**"""


class GateStage(StrEnum):
    """gate の段（0054 §2.4）。**上の段が落ちたら下の段では覆らない。**"""

    SAFETY = "safety"
    EVIDENCE = "evidence"
    COST = "cost"


GATE_STAGE_ORDER: tuple[GateStage, ...] = (GateStage.SAFETY, GateStage.EVIDENCE, GateStage.COST)
"""判定の順。**Safety が必ず先。**"""


class GateOutcome(StrEnum):
    """1つの条件、または arm 全体の判定。"""

    PASS = "pass"
    BLOCKED = "blocked"
    """条件を満たさない、または**判定できない**。判定できないことを合格にしない。"""


class GateCondition(_Frozen):
    """1つの gate 条件の判定。"""

    stage: GateStage
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    outcome: GateOutcome
    limit: float | None = Field(default=None, allow_inf_nan=False)
    observed: float | None = Field(default=None, allow_inf_nan=False)
    reason: CountedReason | None = None
    """判定できなかった理由。`observed` が無いときは必ず付く。"""
    worst_case: WorstCase | None = None
    """Safety の条件が見た worst-case（0054 §2.4）。"""

    @model_validator(mode="after")
    def _undecidable_conditions_are_blocked(self) -> Self:
        if self.observed is None:
            if self.outcome is not GateOutcome.BLOCKED:
                # 欠測・未設定・coverage 不足を合格にしない（fail closed）。
                raise ValueError("観測値の無い gate 条件は blocked にする")
            if self.reason is None:
                raise ValueError("判定できなかった gate 条件には理由が要る")
        return self


class GateResult(_Frozen):
    """1つの arm の rollout 可否。**助言であり、昇格の判断ではない**（#92）。"""

    arm_key: str = Field(min_length=1, max_length=200)
    outcome: GateOutcome
    blocking_stage: GateStage | None = None
    conditions: tuple[GateCondition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _safety_is_never_offset_by_other_stages(self) -> Self:
        stages = tuple(condition.stage for condition in self.conditions)
        order = {stage: index for index, stage in enumerate(GATE_STAGE_ORDER)}
        if list(stages) != sorted(stages, key=lambda stage: order[stage]):
            raise ValueError("gate の条件を段の順に並べる")
        failed = tuple(
            condition for condition in self.conditions if condition.outcome is GateOutcome.BLOCKED
        )
        expected_blocking = (
            None if not failed else min(failed, key=lambda item: order[item.stage]).stage
        )
        if self.blocking_stage is not expected_blocking:
            raise ValueError("blocking_stage を最初に落ちた段と揃える")
        expected = GateOutcome.PASS if not failed else GateOutcome.BLOCKED
        if self.outcome is not expected:
            # ここが破れると、cost の改善で Safety 違反を相殺できてしまう。
            raise ValueError("落ちた条件が1つでもあれば blocked にする")
        return self


class EvaluationReport(_Frozen):
    """比較レポート全体（#91 の受入基準）。

    **同じ入力からは同じ bytes になる。** 生成時刻を持たず、時刻はすべて証拠から来る。
    """

    schema_version: Literal[1] = EVALUATION_REPORT_SCHEMA_VERSION
    provenance: EvaluationProvenance
    segments: tuple[SegmentReport, ...] = Field(min_length=1)
    worst_cases: tuple[WorstCase, ...] = ()
    gates: tuple[GateResult, ...] = ()
    """**holdout segment だけ**で判定した結果（0054 §2.5）。"""

    @model_validator(mode="after")
    def _segments_are_ordered_and_worst_cases_are_present(self) -> Self:
        previous: tuple[str, int] | None = None
        for segment in self.segments:
            current = (segment.run_id, segment.index)
            if previous is not None and current <= previous:
                raise ValueError("segment は run_id と index の昇順に並べる")
            previous = current
        if not self.worst_cases:
            # 平均だけを見て「問題なかった」と読ませない（Issue の評価原則）。
            # segment には必ず tick があるので、worst-case は必ず作れる。
            raise ValueError("tick のある報告には worst-case を必ず載せる")
        if not self.gates:
            # **条件が1つも無い結果を「何も落ちなかった」と読ませない。**
            # holdout には必ず tick があるので、適用 arm の gate は必ず作れる。
            raise ValueError("holdout のある報告には gate の結果を必ず載せる")
        keys = tuple(gate.arm_key for gate in self.gates)
        if len(set(keys)) != len(keys):
            raise ValueError("同じ arm の gate 結果を2つ入れない")
        return self


__all__ = [
    "EVALUATION_REPORT_SCHEMA_VERSION",
    "GATE_STAGE_ORDER",
    "AirBalanceReport",
    "AppliedArm",
    "AppliedArmReport",
    "CountedReason",
    "CounterfactualArm",
    "CounterfactualArmReport",
    "CoverageReport",
    "DeltaReport",
    "EvaluationProvenance",
    "EvaluationReport",
    "GateCondition",
    "GateOutcome",
    "GateResult",
    "GateStage",
    "GroupKind",
    "GroupReport",
    "InterventionReport",
    "ObservedVersions",
    "OptimizerReport",
    "PredictionReport",
    "RunProvenance",
    "SegmentReport",
    "SegmentRole",
    "TemperatureReport",
    "WorstCase",
    "WorstCaseKind",
    "ZoneSeriesReport",
]
