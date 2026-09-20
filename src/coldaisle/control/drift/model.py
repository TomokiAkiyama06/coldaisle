"""Model Drift の報告の型（#93 / 決定記録 0056）。

**この型は制御へ届かない。** `Demand` も PWM も authority も表現できない。出せるのは
「どれだけ劣化しているか」「どれだけの証拠で言っているか」「再学習を推奨するか」だけで、
confidence を動かすのは runtime の `ConfidenceAssessor` だけである（0056 §2.1）。

**時刻はすべて証拠から来る。** 生成時刻の欄は無い（0054 §2.7 と同じ規律）。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.model.thermal import MAX_METRIC_NAME_LENGTH, MAX_TARGET_METRICS

DRIFT_REPORT_SCHEMA_VERSION: Literal[1] = 1
"""報告1つの形の版。**欄の意味を変えたら上げる。**"""

MAX_DRIFT_REASONS = 32
"""理由・出どころの内訳に並べる**種類**の上限。

**件数は落とさない。** 種類がこれを超えたら、残りを1つに集約して数え続ける
（上限を超えた瞬間に報告が作れなくなる、という形にしない）。
"""
MAX_DRIFT_SOURCE_LENGTH = 200
"""出どころの表示名の上限。**これを超える組み合わせは digest 付きに畳む**（切るだけにしない）。"""
MAX_DRIFT_TREND_BUCKETS = 4_096
MAX_DECLARED_CHANGES = 64
MAX_DRIFT_TRIGGERS = 32

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ReasonCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
"""理由の識別子。**`coldaisle.control.schema.Reason.code` と同じ形**（一致は試験で確かめる）。"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class DriftReasonCount(_Frozen):
    """理由ごとの件数。**理由を捨てて合計だけにしない。**

    `coldaisle.control.evaluation.model.CountedReason` と同じ形にする。evaluation を
    import しないために写しているので、**欄の一致は試験で確かめる**（0053 §2.5 と同じ規律）。
    """

    code: ReasonCode
    count: int = Field(ge=1)


class DriftSourceCount(_Frozen):
    """逸脱の出どころごとの件数。出どころは metric 名や欠測の組み合わせなので、
    `DriftReasonCount` の識別子より広い形を許す。"""

    source: str = Field(min_length=1, max_length=MAX_DRIFT_SOURCE_LENGTH)
    count: int = Field(ge=1)


class DriftVerdict(StrEnum):
    """1つの signal、または報告全体の判定（決定記録 0056 §2.4）。"""

    OK = "ok"
    WARNING = "warning"
    DEGRADED = "degraded"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    """判定に足りる証拠が無い。**`ok` の代わりに使わない**（fail closed）。"""


VERDICT_PRECEDENCE: tuple[DriftVerdict, ...] = (
    DriftVerdict.DEGRADED,
    DriftVerdict.INSUFFICIENT_EVIDENCE,
    DriftVerdict.WARNING,
    DriftVerdict.OK,
)
"""報告全体の判定を決める順。**先にあるものが勝つ。**

`degraded` が最優先で、次が `insufficient_evidence` である。drift があると言えるなら言い、
言えないなら「言えない」と言う。`warning` が `insufficient_evidence` を隠さない。
"""


def combine_verdicts(verdicts: tuple[DriftVerdict, ...]) -> DriftVerdict:
    """signal の判定をまとめる。**空なら「言えない」。**"""
    for candidate in VERDICT_PRECEDENCE:
        if candidate in verdicts:
            return candidate
    return DriftVerdict.INSUFFICIENT_EVIDENCE


class DriftSignalKind(StrEnum):
    """drift を見る切り口（決定記録 0056 §2.2）。"""

    RESIDUAL = "residual"
    """予測誤差の悪化。Profile の validation residual RMS を 1.0 とした比で見る。"""
    FEATURE_RANGE = "feature_range"
    """学習範囲（feature metric と Fan demand）の外で運転している割合。"""
    SUPPORT = "support"
    """学習件数の足りない support cell で運転している割合。"""
    MISSING_PATTERN = "missing_pattern"
    """学習時に無かった欠測の組み合わせの割合。"""


class ChangeKind(StrEnum):
    """人が宣言する構成変更（決定記録 0056 §2.5）。**検知器は推測しない。**"""

    FAN_REPLACED = "fan_replaced"
    SENSOR_REPLACED = "sensor_replaced"
    CALIBRATION_CHANGED = "calibration_changed"
    HARDWARE_CONFIG_CHANGED = "hardware_config_changed"


class DeclaredChange(_Frozen):
    """宣言された構成変更。**この時刻より前の証拠は数えない。**"""

    kind: ChangeKind
    ts_ms: int = Field(ge=0)
    detail: str = Field(default="", max_length=500)


class DriftCoverage(_Frozen):
    """**どれだけを証拠に数えられたか**（決定記録 0056 §2.4）。

    Shadow の間は多くの outcome が `unidentifiable` になる（0053 §3）。それは制限ではなく、
    記録から言えることの範囲そのものである。**混ぜずに区分として出す。**
    """

    outcomes: int = Field(ge=0)
    """対象モデルの、変更より後の outcome の数。"""
    scored: int = Field(ge=0)
    """掛かっていた action を識別できた outcome（`ShadowOutcome.status == scored`）。"""
    counted: int = Field(ge=0)
    """**全出力を照合できた** scored。比に数えるのはこれだけである。"""
    unidentifiable: int = Field(ge=0)
    identifiable_fraction: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    """`scored / outcomes`。**outcome が1つも無ければ `None`**（1.0 でも 0.0 でもない）。"""
    unidentifiable_reasons: tuple[DriftReasonCount, ...] = Field(
        default=(), max_length=MAX_DRIFT_REASONS
    )
    incomplete_scored: int = Field(default=0, ge=0)
    """採点できたが出力が揃わず、比に入れなかった outcome。**埋め合わせない。**"""
    outputs: int = Field(default=0, ge=0)
    matched_outputs: int = Field(default=0, ge=0)
    counted_outputs: int = Field(default=0, ge=0)
    unmatched_reasons: tuple[DriftReasonCount, ...] = Field(
        default=(), max_length=MAX_DRIFT_REASONS
    )
    predicted_metrics: tuple[str, ...] = Field(default=(), max_length=MAX_TARGET_METRICS)
    """予測に現れた metric。

    **写し元と同じ上限にする。** 束縛できた予測の出力は Profile の target schema の中に
    あることを検知器が確かめるので（0056 §2.3）、種類は `MAX_TARGET_METRICS` を超えない。
    ここだけ狭いと、正しい記録から報告を作れない tick が生まれる（#90 で見つかった型）。
    """
    unscored_metrics: tuple[str, ...] = Field(default=(), max_length=MAX_TARGET_METRICS)
    """**一度も比に数えられなかった** metric。

    残りの metric だけで「悪化していない」と言えてしまうので、1つでもあれば
    `sufficient` にしない（0054 §2.3 と同じ理由）。
    """
    excluded_by_change: int = Field(default=0, ge=0)
    """宣言された変更より前だったため数えなかった outcome（0056 §2.5）。"""
    purged: int = Field(default=0, ge=0)
    """証拠の時刻が**宣言した期間の外**だったため数えなかった outcome（0056 §2.3）。

    行そのものは期間の中にあっても、その予測の実測は `end_ms` を越えうる。**落とさずに
    数える**（0054 §2.5 の `purged` と同じ扱い。「予測が無かった」と「この区間では
    確かめられない」を区別する）。
    """
    foreign_model: int = Field(default=0, ge=0)
    """別のモデル / artifact の counterfactual。**混ぜない。**"""
    sufficient: bool

    @model_validator(mode="after")
    def _counts_are_consistent(self) -> Self:
        if self.scored + self.unidentifiable != self.outcomes:
            raise ValueError("scored と unidentifiable の合計を outcomes と揃える")
        if self.counted + self.incomplete_scored != self.scored:
            raise ValueError("比に数えた outcome と揃わなかった outcome の合計を scored と揃える")
        if self.matched_outputs > self.outputs or self.counted_outputs > self.matched_outputs:
            raise ValueError("照合・採点した出力の数が全体を超えている")
        if (self.outcomes == 0) != (self.identifiable_fraction is None):
            raise ValueError("outcome が無い coverage に identifiable_fraction を付けない")
        if (
            self.identifiable_fraction is not None
            and self.identifiable_fraction != self.scored / self.outcomes
        ):
            raise ValueError("identifiable_fraction を実際の件数から計算する")
        if set(self.unscored_metrics) - set(self.predicted_metrics):
            raise ValueError("採点できていない metric は、予測した metric の中から挙げる")
        if self.sufficient:
            if self.counted == 0:
                raise ValueError("比に数えた outcome が無い coverage を sufficient にしない")
            if self.unscored_metrics:
                raise ValueError("採点できていない metric がある coverage を sufficient にしない")
        return self


class ResidualTrendBucket(_Frozen):
    """residual trend の1区切り（#93 の「residual trend を可視化・保存できる」）。

    **bucket は自分の件数だけで判定する。** 足りなければ比を持たず
    `insufficient_evidence` になる。隣の bucket から証拠を借りない（0056 §2.4）。
    """

    index: int = Field(ge=0)
    start_ts_ms: int = Field(ge=0)
    end_ts_ms: int = Field(ge=0)
    """**証拠から来る時刻**（照合に使った観測の時刻、または照合期限）。"""
    outcomes: int = Field(ge=1)
    ratio: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    verdict: DriftVerdict

    @model_validator(mode="after")
    def _ratio_and_verdict_agree(self) -> Self:
        if self.end_ts_ms < self.start_ts_ms:
            raise ValueError("bucket の終わりを始まりより前にしない")
        if (self.ratio is None) != (self.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE):
            raise ValueError("比の無い bucket は insufficient_evidence にする")
        return self


class MetricResidual(_Frozen):
    """metric ごとの正規化 residual（Profile の validation RMS を 1.0 とした比）。"""

    metric: str = Field(min_length=1, max_length=MAX_METRIC_NAME_LENGTH)
    """**写し元と同じ上限にする**（`ThermalMetricName`）。狭いと正しい metric を書けない。"""
    counted_outputs: int = Field(ge=1)
    ratio: float = Field(ge=0.0, allow_inf_nan=False)


class ResidualDriftSignal(_Frozen):
    """prediction residual の悪化（決定記録 0056 §2.2 / §2.3）。

    **`status: scored` で、かつ全出力が照合できた outcome だけ**から作る。
    `unidentifiable` の差は制御器の違いとモデル誤差の混合であって、drift ではない。
    """

    kind: Literal[DriftSignalKind.RESIDUAL] = DriftSignalKind.RESIDUAL
    verdict: DriftVerdict
    coverage: DriftCoverage
    ratio: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    """正規化 residual の RMS。**coverage が足りているときだけ**入る。"""
    warning_ratio: float = Field(gt=1.0, allow_inf_nan=False)
    degraded_ratio: float = Field(gt=1.0, allow_inf_nan=False)
    per_metric: tuple[MetricResidual, ...] = ()
    trend: tuple[ResidualTrendBucket, ...] = Field(default=(), max_length=MAX_DRIFT_TREND_BUCKETS)
    trend_bucket_outcomes: int | None = Field(default=None, gt=0)
    """trend を切った実際の bucket の大きさ。

    証拠が多すぎて構造上限（`MAX_DRIFT_TREND_BUCKETS`）を超えるときは、設定値の**整数倍**へ
    粗くする。**報告が作れなくなる形にしない。** 粗くしても bucket は自分の件数だけで
    判定するので、隣から証拠を借りることにはならない（0056 §2.4）。
    """
    first_evidence_ts_ms: int | None = Field(default=None, ge=0)
    last_evidence_ts_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _thin_evidence_publishes_nothing(self) -> Self:
        if (self.ratio is None) != (not self.coverage.sufficient):
            # 足りない証拠から比を出さない。出したなら足りている。
            raise ValueError("比の有無と coverage の充足を一致させる")
        if (self.ratio is None) != (self.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE):
            raise ValueError("比の無い residual signal は insufficient_evidence にする")
        if (self.trend_bucket_outcomes is None) != (not self.trend):
            raise ValueError("trend がある signal には bucket の大きさを付ける")
        if self.ratio is None and (self.trend or self.per_metric):
            # trend も metric 別も、伏せた比と同じ証拠から出る。
            raise ValueError("比を出さない residual signal に trend / metric 別を付けない")
        if self.warning_ratio >= self.degraded_ratio:
            raise ValueError("warning_ratio は degraded_ratio 未満にする")
        if self.ratio is not None:
            expected = DriftVerdict.OK
            if self.ratio >= self.degraded_ratio:
                expected = DriftVerdict.DEGRADED
            elif self.ratio >= self.warning_ratio:
                expected = DriftVerdict.WARNING
            if self.verdict is not expected:
                raise ValueError("residual の判定を比と閾値から導いた値と揃える")
        counted_outputs = sum(item.counted_outputs for item in self.per_metric)
        if counted_outputs > self.coverage.counted_outputs:
            raise ValueError("metric 別の出力数が coverage の採点数を超えている")
        if sum(bucket.outcomes for bucket in self.trend) > self.coverage.counted:
            raise ValueError("trend の件数が比に数えた outcome を超えている")
        if (self.first_evidence_ts_ms is None) != (self.last_evidence_ts_ms is None):
            raise ValueError("証拠の期間は両端を揃えて記録する")
        if (
            self.first_evidence_ts_ms is not None
            and self.last_evidence_ts_ms is not None
            and self.last_evidence_ts_ms < self.first_evidence_ts_ms
        ):
            raise ValueError("証拠の期間を逆順にしない")
        return self


class InputDriftSignal(_Frozen):
    """入力分布の逸脱（feature range / support / 欠測の組み合わせ）。

    何を逸脱と数えるかは **runtime の `model_confidence`** が決める。ここは割合だけを見る。
    """

    kind: DriftSignalKind
    """feature range / support / 欠測のどれか。**`residual` はここに置かない**（証拠が別）。"""
    verdict: DriftVerdict
    inputs: int = Field(ge=0)
    """受け取った入力の数。"""
    considered: int = Field(ge=0)
    """宣言された変更より後の入力（判定に使った数）。"""
    excluded_by_change: int = Field(default=0, ge=0)
    affected: int = Field(ge=0)
    fraction: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    """`affected / considered`。**下限に満たなければ出さない。**"""
    warning_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    degraded_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    sources: tuple[DriftSourceCount, ...] = Field(default=(), max_length=MAX_DRIFT_REASONS)
    """逸脱の出どころ（metric / zone / 欠測の組み合わせ）ごとの件数。"""
    first_input_ts_ms: int | None = Field(default=None, ge=0)
    last_input_ts_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _thin_evidence_publishes_nothing(self) -> Self:
        if self.kind is DriftSignalKind.RESIDUAL:
            # residual は outcome の証拠から作る。入力分布の signal として作らせない。
            raise ValueError("residual を入力分布の signal にしない")
        if self.considered + self.excluded_by_change != self.inputs:
            raise ValueError("判定に使った入力と除いた入力の合計を inputs と揃える")
        if self.affected > self.considered:
            raise ValueError("逸脱した入力の数が判定に使った入力を超えている")
        if self.warning_fraction >= self.degraded_fraction:
            raise ValueError("warning_fraction は degraded_fraction 未満にする")
        if (self.fraction is None) != (self.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE):
            raise ValueError("割合の無い input signal は insufficient_evidence にする")
        if self.fraction is None:
            if self.sources:
                raise ValueError("割合を出さない signal に内訳を付けない")
            return self
        if self.considered == 0 or self.fraction != self.affected / self.considered:
            raise ValueError("割合を実際の件数から計算する")
        expected = DriftVerdict.OK
        if self.fraction >= self.degraded_fraction:
            expected = DriftVerdict.DEGRADED
        elif self.fraction >= self.warning_fraction:
            expected = DriftVerdict.WARNING
        if self.verdict is not expected:
            raise ValueError("入力分布の判定を割合と閾値から導いた値と揃える")
        if sum(item.count for item in self.sources) < self.affected:
            raise ValueError("逸脱の内訳の合計が件数に足りない")
        return self


class RetrainingTrigger(_Frozen):
    """再学習を推奨する理由1つ（#93 の「retraining trigger の理由を記録できる」）。"""

    code: ReasonCode
    signal: DriftSignalKind | None = None
    """由来した signal。宣言された変更による trigger では `None`。"""
    detail: str = Field(default="", max_length=500)
    evidence_ts_ms: int | None = Field(default=None, ge=0)
    """**証拠から来る時刻**。処理した時刻ではない。"""


class RetrainingRecommendation(_Frozen):
    """再学習の推奨。**Registry も設定も書き換えない**（決定記録 0056 §2.6）。

    `human_approval_required` は型の上で常に真である。「承認不要の再学習」を
    表現できないようにするための構造上の制約で、設定値ではない。
    """

    required: bool
    """`degraded` な signal があるか、構成変更が宣言されたときだけ真。"""
    recharacterization_required: bool
    """Fan / sensor / 較正が変わったので、学習前に再 characterization が要る。"""
    human_approval_required: Literal[True] = True
    triggers: tuple[RetrainingTrigger, ...] = Field(default=(), max_length=MAX_DRIFT_TRIGGERS)
    inconclusive_signals: tuple[DriftSignalKind, ...] = ()
    """証拠が足りず判定できなかった signal。**「再学習は不要」と読ませないために残す。**"""

    @model_validator(mode="after")
    def _requirement_follows_the_triggers(self) -> Self:
        if self.required != bool(self.triggers):
            raise ValueError("推奨の要否と理由の有無を一致させる")
        if self.recharacterization_required and not self.required:
            raise ValueError("再 characterization が要るなら再学習も要る")
        codes = tuple(item.code for item in self.triggers)
        if len(set(codes)) != len(codes):
            raise ValueError("同じ理由を2回並べない")
        return self


class DriftProvenance(_Frozen):
    """再現に要る入力の素性。**絶対 path も生成時刻も持たない**（0021 / 0054 §2.7）。"""

    drift_config_sha256: Sha256Hex
    profile_sha256: Sha256Hex
    shadow_export_schema_version: int = Field(ge=1)
    residual_drift_ood_ratio: float = Field(gt=1.0, allow_inf_nan=False)
    """照合した runtime の契約（`model_confidence`）。**写しではなく、照合した値の記録。**"""
    range_margin: float = Field(ge=0.0, allow_inf_nan=False)
    min_support_count: int = Field(gt=0)
    min_missing_pattern_count: int = Field(gt=0)
    shadow_rows: int = Field(ge=0)
    outcome_match_tolerance_ms: int = Field(ge=0)
    """照合に使った許容幅（`fan-policy.yaml` の `shadow`）。**写しではなく、照合した値の記録。**

    記録された幅がこれと違う outcome は数えない（0054 §2.6 と同じ規則）。
    """
    applied_demand_tolerance: float = Field(ge=0.0, lt=1.0, allow_inf_nan=False)
    """識別に使った許容幅（`fan-policy.yaml` の `shadow`）。**照合した値の記録。**"""
    window_start_ms: int | None = Field(default=None, ge=0)
    window_end_ms: int | None = Field(default=None, ge=0)
    """**証拠として見た期間**（0056 §2.7）。呼び出し側が期間を宣言したときだけ入る。

    これが無いと、**違う期間を見た2つの報告が同じ bytes を名乗れる**（同じ証拠しか
    入っていなければ、どこを見たのかが報告から消える）。
    """
    evidence_sha256: Sha256Hex
    """数えた証拠そのものの digest。**同じ証拠なら同じ値**になる。

    宣言された変更は**1つに畳んでから**数えるので、同じ宣言を2度渡しても値は変わらない。
    """

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if (self.window_start_ms is None) != (self.window_end_ms is None):
            raise ValueError("証拠の期間は両端を揃えて記録する")
        if (
            self.window_start_ms is not None
            and self.window_end_ms is not None
            and self.window_end_ms <= self.window_start_ms
        ):
            raise ValueError("証拠の期間は start < end にする")
        return self


class DriftTarget(_Frozen):
    """この報告が対象にする Production artifact（#93 の「model/version と対応付ける」）。"""

    model_id: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    artifact_sha256: Sha256Hex
    profile_sha256: Sha256Hex


class DriftReport(_Frozen):
    """1回の drift 判定。**同じ入力からは同じ bytes になる。**"""

    schema_version: Literal[1] = DRIFT_REPORT_SCHEMA_VERSION
    target: DriftTarget
    verdict: DriftVerdict
    residual: ResidualDriftSignal
    inputs: tuple[InputDriftSignal, ...] = Field(min_length=3, max_length=3)
    """feature range / support / 欠測の3つを**必ず**並べる（欠けを「問題なし」と読ませない）。"""
    declared_changes: tuple[DeclaredChange, ...] = Field(
        default=(), max_length=MAX_DECLARED_CHANGES
    )
    recommendation: RetrainingRecommendation
    provenance: DriftProvenance

    @model_validator(mode="after")
    def _verdict_and_recommendation_follow_the_signals(self) -> Self:
        kinds = tuple(item.kind for item in self.inputs)
        expected_kinds = (
            DriftSignalKind.FEATURE_RANGE,
            DriftSignalKind.SUPPORT,
            DriftSignalKind.MISSING_PATTERN,
        )
        if kinds != expected_kinds:
            raise ValueError("入力分布の signal は定義順に1つずつ並べる")
        verdicts = (self.residual.verdict, *(item.verdict for item in self.inputs))
        if self.verdict is not combine_verdicts(verdicts):
            raise ValueError("報告全体の判定を signal から導いた値と揃える")
        if (
            self.verdict is DriftVerdict.OK
            and self.recommendation.required
            and not self.declared_changes
        ):
            # signal 由来の要求は degraded のときだけ。**宣言された変更だけが例外**で、
            # 証拠が足りていても Fan / sensor / 較正が変われば再 characterization が要る。
            raise ValueError("すべて ok で変更も無い報告から再学習を要求しない")
        if self.verdict is DriftVerdict.DEGRADED and not self.recommendation.required:
            raise ValueError("degraded な報告では再学習を推奨する")
        inconclusive = tuple(
            kind
            for kind, verdict in zip(
                (DriftSignalKind.RESIDUAL, *expected_kinds), verdicts, strict=True
            )
            if verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
        )
        if self.recommendation.inconclusive_signals != inconclusive:
            # 「判定できなかった」を落とすと、残りが ok なだけの報告を
            # 「drift は無かった」と読めてしまう。
            raise ValueError("判定できなかった signal を推奨へそのまま残す")
        if self.recommendation.recharacterization_required != bool(self.declared_changes):
            raise ValueError("宣言された変更の有無と再 characterization の要否を一致させる")
        if self.target.profile_sha256 != self.provenance.profile_sha256:
            raise ValueError("報告の対象と素性の Profile を揃える")
        return self

    def canonical_bytes(self) -> bytes:
        """保存と digest に使う canonical JSON。**無い値も欄として残す。**"""
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    def sha256(self) -> str:
        """報告の canonical SHA-256。並べれば drift の履歴になる。"""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


__all__ = [
    "DRIFT_REPORT_SCHEMA_VERSION",
    "VERDICT_PRECEDENCE",
    "ChangeKind",
    "DeclaredChange",
    "DriftCoverage",
    "DriftProvenance",
    "DriftReasonCount",
    "DriftReport",
    "DriftSignalKind",
    "DriftSourceCount",
    "DriftTarget",
    "DriftVerdict",
    "InputDriftSignal",
    "MetricResidual",
    "ResidualDriftSignal",
    "ResidualTrendBucket",
    "RetrainingRecommendation",
    "RetrainingTrigger",
    "combine_verdicts",
]
