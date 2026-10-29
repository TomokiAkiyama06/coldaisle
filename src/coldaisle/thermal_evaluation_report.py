"""反実仮想 Thermal Model の offline 評価の報告・設定の型と、gate・digest の関数。

#84 / 決定記録 0172。

評価の CLI ``coldaisle-thermal-evaluate``（書き手）と、段 2 の ``coldaisle-registry validate
--thermal-evaluation``（読み手）の2つの合成の起点が**同じ関数**を使う（0172 §2.1 / §2.7）。
どちらの合成の起点にも属さないレイヤ横断の葉の module に置く（``training_output.py`` と同じ扱い）。

- **標準ライブラリ・pydantic・yaml だけを使い、coldaisle の module を import しない**
- gate（0172 §2.6）は**報告の数字と設定だけ**で決まる純関数にする。読み手は報告の gate を信じず、
  この関数で作り直して照合する（0172 §2.7 の 4）
- 報告は canonical JSON（``sort_keys``・UTF-8・区切りの空白なし・末尾の改行 1 つ）で、壁時計の欄を
  持たない（0054 §2.7）
- 閾値の値はここに置かない。すべて設定（``config/thermal-evaluation.example.yaml`` は仮の値の例）の
  ``{value, status}`` から読む（0172 §2.5 / AGENTS.md ルール 9）
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from fractions import Fraction
from itertools import pairwise
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

REPORT_SCHEMA_VERSION: Literal[1] = 1
"""報告（``--out``）の schema の版。"""

PROCEDURE_VERSION: Literal[1] = 1
"""評価の手順（予測の作り方・baseline・指標・層・gate の構造）の版。手順を変えたら上げる。"""

CONFIG_SCHEMA_VERSION: Literal[1] = 1
"""``--evaluation-config`` の schema の版。"""

OFFLINE_EVALUATION_REF_PREFIX = "thermal-offline-v1:"
"""``offline_evaluation_ref`` の接頭辞（0172 §2.7。``supervisor-episode:<digest>`` と同じ形）。"""

UNKNOWN_STRATUM = "unknown"
"""帯を決められなかった example の層。黙って落とさずにここへ入れる（0054 §2.8 と同じ）。"""

StratumAxis = Literal[
    "room_temperature", "workload", "action_change", "workload_regime", "action_support"
]
"""層の軸。"""

AXIS_ROOM_TEMPERATURE: StratumAxis = "room_temperature"
AXIS_WORKLOAD: StratumAxis = "workload"
AXIS_ACTION_CHANGE: StratumAxis = "action_change"
AXIS_WORKLOAD_REGIME: StratumAxis = "workload_regime"
AXIS_ACTION_SUPPORT: StratumAxis = "action_support"

GATING_AXES: tuple[StratumAxis, ...] = (AXIS_ROOM_TEMPERATURE, AXIS_WORKLOAD, AXIS_ACTION_CHANGE)
"""十分な件数があれば合否の条件にする層の軸（0172 §2.4 / §2.6）。"""

REPORT_ONLY_AXES: tuple[StratumAxis, ...] = (AXIS_WORKLOAD_REGIME, AXIS_ACTION_SUPPORT)
"""報告に載せるだけの層の軸（0172 §2.4 の ``workload_regime``・§5 の 5 の support の中の集計）。"""

GateStage = Literal["evidence", "underprediction", "skill"]
"""gate の段。"""

STAGE_EVIDENCE: GateStage = "evidence"
STAGE_UNDERPREDICTION: GateStage = "underprediction"
STAGE_SKILL: GateStage = "skill"
GATE_STAGES: tuple[GateStage, ...] = (STAGE_EVIDENCE, STAGE_UNDERPREDICTION, STAGE_SKILL)
"""辞書式の段の順（0172 §2.6）。上の段が落ちたら下の段で覆らない。"""

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REF_PATTERN = re.compile(r"^thermal-offline-v1:[0-9a-f]{64}$")
_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
Quantile = Annotated[float, Field(gt=0.0, le=1.0, allow_inf_nan=False)]
UnitFraction = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
OpenUnitFraction = Annotated[float, Field(gt=0.0, lt=1.0, allow_inf_nan=False)]
ImprovementFraction = Annotated[float, Field(ge=0.0, lt=1.0, allow_inf_nan=False)]
MetricName = Annotated[str, Field(min_length=1, max_length=120)]
BandName = Annotated[str, Field(pattern=_NAME_PATTERN, max_length=32)]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ---------------------------------------------------------------- 設定（0172 §2.5）


class ConfigValue[T](_Strict):
    """実データの前後を区別する値。confirmed には根拠を要する（``control.config`` と同じ形）。"""

    value: T
    status: Literal["provisional", "confirmed"]
    basis: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _confirmed_value_has_basis(self) -> Self:
        if self.status == "confirmed" and self.basis is None:
            raise ValueError("confirmed の値には basis が要る")
        return self


class OutputConfig(_Strict):
    """合否を見る target の metric。``direction`` は v1 では ``higher_is_worse`` だけ。"""

    metric: MetricName
    direction: Literal["higher_is_worse"]


class TemperatureBand(_Strict):
    """室温の帯。``below_c`` **未満**をこの帯とし、最後の帯だけ上限を持たない（0054 §2.8）。"""

    name: BandName
    below_c: ConfigValue[FiniteFloat] | None = None


class PowerBand(_Strict):
    """電力の帯。``below_w`` **未満**をこの帯とし、最後の帯だけ上限を持たない。"""

    name: BandName
    below_w: ConfigValue[FiniteFloat] | None = None


def _check_bands(names: list[str], uppers: list[float | None], *, what: str) -> None:
    if any(upper is None for upper in uppers[:-1]) or uppers[-1] is not None:
        raise ValueError(f"{what} は最後の帯だけ上限を持たない形にする")
    finite = [upper for upper in uppers[:-1] if upper is not None]
    if any(later <= earlier for earlier, later in pairwise(finite)):
        raise ValueError(f"{what} の上限は重複なしの昇順にする")
    if len(set(names)) != len(names):
        raise ValueError(f"{what} の名前が重複している")
    if UNKNOWN_STRATUM in names:
        raise ValueError(f"{what} に {UNKNOWN_STRATUM!r} という名前は使えない")


class WorkloadBands(_Strict):
    """workload の層（0172 §2.4 / §5 の 4。0151 と同じ考え方で電力の帯の組み合わせ）。"""

    window_ms: ConfigValue[PositiveInt]
    """anchor の時点から遡るこの長さの window の frame の平均を帯に入れる。"""
    cpu_metric: MetricName
    gpu_metric: MetricName
    cpu_power: tuple[PowerBand, ...] = Field(min_length=1)
    gpu_power: tuple[PowerBand, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _bands_are_ordered(self) -> Self:
        for what, bands in (("cpu_power", self.cpu_power), ("gpu_power", self.gpu_power)):
            _check_bands(
                [band.name for band in bands],
                [None if band.below_w is None else band.below_w.value for band in bands],
                what=f"workload_bands.{what}",
            )
        return self


class MinimumConfig(_Strict):
    """件数の下限（0172 §2.6 の ``evidence`` と層の ``insufficient``）。"""

    test_examples: ConfigValue[PositiveInt]
    blocks: ConfigValue[PositiveInt]
    stratum_examples: ConfigValue[PositiveInt]
    stratum_blocks: ConfigValue[PositiveInt]
    predicted_fraction: ConfigValue[UnitFraction]


class SkillGate(_Strict):
    """``skill`` の段（0172 §2.6 の 3）。"""

    alpha: ConfigValue[OpenUnitFraction]
    minimum_mae_improvement_fraction: ConfigValue[ImprovementFraction]


class UnderpredictionGate(_Strict):
    """``underprediction`` の段（0172 §2.6 の 2）。"""

    quantile: ConfigValue[Quantile]
    maximum_c: ConfigValue[NonNegativeFloat]
    worst_case_maximum_c: ConfigValue[NonNegativeFloat]


class GateConfig(_Strict):
    """gate の値（構造は 0172 §2.6、値は実データの後に決める）。"""

    skill: SkillGate
    underprediction: UnderpredictionGate
    stratum_regression_tolerance_c: ConfigValue[NonNegativeFloat]


class ThermalEvaluationConfig(_Strict):
    """``--evaluation-config``（schema v1）。**コードに既定値を置かない**（0172 §2.5）。"""

    schema_version: Literal[1]
    outputs: tuple[OutputConfig, ...] = Field(min_length=1)
    """artifact の target schema の metric と過不足なく一致すること。"""
    quantiles: tuple[Quantile, ...] = Field(min_length=1)
    """underprediction の上側の分位（nearest-rank）。"""
    worst_case_count: PositiveInt
    block_ms: ConfigValue[PositiveInt]
    room_temperature_metric: MetricName
    room_temperature_bands: tuple[TemperatureBand, ...] = Field(min_length=1)
    workload_bands: WorkloadBands
    minimum: MinimumConfig
    gate: GateConfig

    @model_validator(mode="after")
    def _shape_is_consistent(self) -> Self:
        metrics = [output.metric for output in self.outputs]
        if len(set(metrics)) != len(metrics):
            raise ValueError("outputs の metric が重複している")
        if any(later <= earlier for earlier, later in pairwise(self.quantiles)):
            raise ValueError("quantiles は重複なしの昇順にする")
        if self.gate.underprediction.quantile.value not in self.quantiles:
            raise ValueError("gate.underprediction.quantile は quantiles のどれかにする")
        _check_bands(
            [band.name for band in self.room_temperature_bands],
            [
                None if band.below_c is None else band.below_c.value
                for band in self.room_temperature_bands
            ],
            what="room_temperature_bands",
        )
        return self

    def output_metrics(self) -> frozenset[str]:
        """合否を見る metric の集合。"""
        return frozenset(output.metric for output in self.outputs)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """mapping の重複した鍵を拒否する（``yaml.safe_load`` は黙って後の値で上書きする）。"""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    seen: set[str] = set()
    for key_node, _value in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError(f"mapping の鍵は文字列にする: {key!r}")
        if key in seen:
            raise ValueError(f"重複した鍵がある: {key!r}（行 {key_node.start_mark.line + 1}）")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _tuples(value: object) -> object:
    """YAML の list を tuple にする（strict な型は list を tuple として受けない）。"""
    if isinstance(value, list):
        return tuple(_tuples(item) for item in value)
    if isinstance(value, dict):
        return {key: _tuples(item) for key, item in value.items()}
    return value


def parse_evaluation_config(payload: bytes) -> ThermalEvaluationConfig:
    """読んだ bytes から設定を作る。形が外れれば ``ValueError``（pydantic の検証の失敗を含む）。

    評価の CLI と段 2 の validate が同じ関数で読む（0172 §2.7 の 3）。
    """
    try:
        loaded: object = yaml.load(payload.decode("utf-8"), Loader=_UniqueKeySafeLoader)
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as error:
        raise ValueError(f"評価の設定を読めない: {error}") from error
    if not isinstance(loaded, dict):
        raise ValueError("評価の設定が辞書ではない")
    try:
        return ThermalEvaluationConfig.model_validate(_tuples(loaded))
    except ValidationError as error:
        raise ValueError(f"評価の設定の形が違う: {error}") from error


def band_of(value: float, bands: tuple[tuple[str, float | None], ...]) -> str:
    """``(name, 上限)`` の列から値の入る帯の名前（上限は未満。最後の帯は上限なし）。"""
    for name, upper in bands:
        if upper is None or value < upper:
            return name
    raise ValueError("最後の帯が上限を持っている")  # 設定の検査で起きない


# ---------------------------------------------------------------- 報告（0172 §2.7）


class ArtifactSection(_Strict):
    """評価した artifact（Registry の記録の checksum と同梱 Profile の digest）。"""

    model_id: Annotated[str, Field(min_length=1, max_length=120)]
    version: Annotated[str, Field(min_length=1, max_length=80)]
    sha256: Sha256
    confidence_profile_sha256: Sha256
    registry_status: Literal["candidate", "validated", "production", "retired"]
    """評価の時点の Registry の状態。validate に使えるのは candidate の報告だけ（0172 §2.2）。"""


class DatasetSection(_Strict):
    """artifact を学習した dataset（0172 §2.2 / §5 の 7）。"""

    alias: Annotated[str, Field(pattern=r"^dataset-[0-9a-f]{32}$")]
    manifest_sha256: Sha256
    examples_sha256: Sha256


class SplitWindow(_Strict):
    """split の1集合の件数と期間（空なら期間は ``None``）。"""

    count: NonNegativeInt
    start_ms: NonNegativeInt | None
    end_ms: NonNegativeInt | None

    @model_validator(mode="after")
    def _empty_has_no_period(self) -> Self:
        if (self.count == 0) != (self.start_ms is None) or (self.start_ms is None) != (
            self.end_ms is None
        ):
            raise ValueError("空の集合だけが期間を持たない")
        return self


class SplitSection(_Strict):
    """split の境界と digest（0172 §2.2 の照合の 1）。"""

    validation_start_ms: NonNegativeInt
    test_start_ms: NonNegativeInt
    split_sha256: Sha256
    train: SplitWindow
    validation: SplitWindow
    test: SplitWindow
    purged: NonNegativeInt


class InputsSection(_Strict):
    """入力の bytes の SHA-256（parse と同じ buffer から。0150 §2.7）。"""

    config_sha256: Sha256
    metrics_config_sha256: Sha256
    calibration_sha256: Sha256


class CodeSection(_Strict):
    """報告の schema の版と評価の手順の版。"""

    report_schema_version: Literal[1]
    procedure_version: Literal[1]


class OutputKey(_Strict):
    """target schema の1出力。"""

    horizon_ms: PositiveInt
    metric: MetricName

    @property
    def label(self) -> str:
        """理由の文に使う名前。"""
        return f"{self.metric}@{self.horizon_ms}ms"


class QuantileValue(_Strict):
    """nearest-rank の分位。"""

    quantile: Quantile
    value: FiniteFloat


class WorstCase(_Strict):
    """worst-case の1件。``error`` は ``予測 − 観測``。"""

    example_id: Annotated[str, Field(min_length=1, max_length=200)]
    action_ts_ms: NonNegativeInt
    error: FiniteFloat


class ErrorSummary(_Strict):
    """1つの予測器の誤差の要約（0172 §2.4 の表の model / persistence の列）。"""

    count: PositiveInt
    mae: NonNegativeFloat
    rmse: NonNegativeFloat
    bias: FiniteFloat
    underprediction_quantiles: tuple[QuantileValue, ...]
    underprediction_max: NonNegativeFloat
    abs_error_max: NonNegativeFloat
    worst_underprediction: tuple[WorstCase, ...]
    worst_abs_error: tuple[WorstCase, ...]

    def underprediction_at(self, quantile: float) -> float | None:
        """設定の分位の値（無ければ ``None``。gate は判定できないことを合格にしない）。"""
        for item in self.underprediction_quantiles:
            if item.quantile == quantile:
                return item.value
        return None


class MeanSummary(_Strict):
    """``train_mean`` の誤差（報告だけ。合否に使わない）。"""

    count: PositiveInt
    mae: NonNegativeFloat
    rmse: NonNegativeFloat
    bias: FiniteFloat


class SignTest(_Strict):
    """時間ブロックの片側の符号検定（0172 §2.4。``d < 0`` は model が良いブロック）。"""

    model_better: NonNegativeInt
    persistence_better: NonNegativeInt
    ties: NonNegativeInt
    """``d = 0`` のブロック（検定の数から除く）。"""
    p_value: UnitFraction
    """``sign_test_p_value(model_better, model_better + persistence_better)`` の float。

読み手用。gate は数から作り直す。
"""


class PairedComparison(_Strict):
    """model と persistence の**両方が値を持つ** (example, 出力) の対だけの比較。"""

    count: PositiveInt
    model: ErrorSummary
    persistence: ErrorSummary
    mae_improvement_fraction: FiniteFloat | None
    """``1 − MAE_model / MAE_persistence``。persistence の MAE が 0 なら ``None``。"""
    sign_test: SignTest


class NormalizedError(_Strict):
    """Profile の ``residual_scales`` で割った ``|e|`` の分位。

    #85 の閾値の材料で、合否に使わない。
    """

    residual_scale: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    abs_error_quantiles: tuple[QuantileValue, ...]
    abs_error_max: NonNegativeFloat


class OutputResult(_Strict):
    """1出力 (horizon_ms, metric) の指標（0172 §2.4）。"""

    horizon_ms: PositiveInt
    metric: MetricName
    excluded: dict[str, NonNegativeInt]
    """この出力で除外した (example, 出力) の数を理由ごとに。

target の欠測・persistence が無い 等。
"""
    model: ErrorSummary | None
    train_mean: MeanSummary | None
    paired: PairedComparison | None
    normalized: NormalizedError | None

    @property
    def key(self) -> OutputKey:
        """この出力の key。"""
        return OutputKey(horizon_ms=self.horizon_ms, metric=self.metric)


class GroupResult(_Strict):
    """全体の結果。"""

    examples: NonNegativeInt
    """予測できた example の数。"""
    blocks: NonNegativeInt
    """予測できた example が入る時間ブロックの数。"""
    outputs: tuple[OutputResult, ...]


class StratumResult(_Strict):
    """1つの層の結果。"""

    axis: StratumAxis
    name: Annotated[str, Field(min_length=1, max_length=80)]
    gating: bool
    """十分な件数があれば合否の条件にする軸か（``GATING_AXES`` と一致すること）。"""
    status: Literal["sufficient", "insufficient"]
    examples: NonNegativeInt
    blocks: NonNegativeInt
    outputs: tuple[OutputResult, ...]

    @model_validator(mode="after")
    def _gating_matches_axis(self) -> Self:
        if self.gating != (self.axis in GATING_AXES):
            raise ValueError("gating は軸から決まる")
        return self


class ActionEffectOutput(_Strict):
    """``action_changed`` の example で、実際の action 列と ``hold_effective`` の予測の MAE。"""

    horizon_ms: PositiveInt
    metric: MetricName
    count: NonNegativeInt
    recorded_mae: NonNegativeFloat | None
    held_mae: NonNegativeFloat | None


class ActionEffect(_Strict):
    """action の効き目の確認（0172 §2.3 / §5 の 6。**報告だけで gate の段に入れない**）。"""

    changed_examples: NonNegativeInt
    held_prediction_unavailable: NonNegativeInt
    outputs: tuple[ActionEffectOutput, ...]


class EvaluationSection(_Strict):
    """test の評価の結果（0172 §2.3 / §2.4）。"""

    block_origin_ms: NonNegativeInt
    """時間ブロックの起点（split の ``test_start_ms``）。

ブロックは ``(action_ts_ms − 起点) // block_ms``。
"""
    test_examples: NonNegativeInt
    predicted_examples: NonNegativeInt
    excluded_examples: dict[str, NonNegativeInt]
    """予測できなかった example の数を理由ごとに（補わない。0172 §2.3）。"""
    overall: GroupResult
    strata: tuple[StratumResult, ...]
    action_effect: ActionEffect


class GateResult(_Strict):
    """gate の結果（0172 §2.6）。``blocked`` のときだけ落ちた最初の段と理由を持つ。"""

    outcome: Literal["pass", "blocked"]
    failed_stage: GateStage | None
    reasons: tuple[str, ...]

    @model_validator(mode="after")
    def _blocked_has_a_stage(self) -> Self:
        if (self.outcome == "blocked") != (self.failed_stage is not None):
            raise ValueError("blocked のときだけ failed_stage を持つ")
        if (self.outcome == "blocked") != bool(self.reasons):
            raise ValueError("blocked のときだけ理由を持つ")
        return self


class ThermalEvaluationReport(_Strict):
    """``--out`` の報告（schema v1）。壁時計の欄を持たない（0054 §2.7）。"""

    schema_version: Literal[1]
    artifact: ArtifactSection
    dataset: DatasetSection
    split: SplitSection
    inputs: InputsSection
    code: CodeSection
    outputs: tuple[OutputKey, ...] = Field(min_length=1)
    """artifact の target schema の出力の順。"""
    evaluation: EvaluationSection
    gate: GateResult
    conditions_sha256: Sha256


def canonical_json_bytes(document: object) -> bytes:
    """canonical JSON（``sort_keys``・UTF-8・区切りの空白なし・末尾の改行 1 つ）。"""
    return (
        json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def canonical_report_bytes(report: ThermalEvaluationReport) -> bytes:
    """報告の canonical bytes（``--out`` に書く bytes そのもの）。"""
    return canonical_json_bytes(report.model_dump(mode="json"))


def parse_report_bytes(payload: bytes) -> ThermalEvaluationReport:
    """報告を strict な型で検証し、canonical な bytes であることを確かめる（0172 §2.7 の 1）。"""
    try:
        report = ThermalEvaluationReport.model_validate_json(payload)
    except ValidationError as error:
        raise ValueError(f"評価の報告の形が違う: {error}") from error
    if canonical_report_bytes(report) != payload:
        raise ValueError("評価の報告が canonical な bytes ではない")
    if report.conditions_sha256 != conditions_sha256(report):
        raise ValueError("評価の報告の conditions_sha256 が中身と一致しない")
    return report


def conditions_sha256(report: ThermalEvaluationReport) -> str:
    """評価の条件（識別・設定・split の境界・版）をすべて覆う digest（0172 §2.7 / 0054 §2.7）。"""
    document = {
        "artifact": report.artifact.model_dump(mode="json"),
        "dataset": report.dataset.model_dump(mode="json"),
        "split": {
            "validation_start_ms": report.split.validation_start_ms,
            "test_start_ms": report.split.test_start_ms,
            "split_sha256": report.split.split_sha256,
        },
        "inputs": report.inputs.model_dump(mode="json"),
        "code": report.code.model_dump(mode="json"),
        "outputs": [output.model_dump(mode="json") for output in report.outputs],
    }
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def offline_evaluation_ref(report_bytes: bytes) -> str:
    """``offline_evaluation_ref`` の値（``thermal-offline-v1:<報告の bytes の SHA-256>``）。"""
    return OFFLINE_EVALUATION_REF_PREFIX + hashlib.sha256(report_bytes).hexdigest()


def is_offline_evaluation_ref(value: str) -> bool:
    """``thermal-offline-v1:`` + 64 hex の形か（段 2 の ``mark_validated()`` の検査が使う）。"""
    return _REF_PATTERN.fullmatch(value) is not None


# ---------------------------------------------------------------- 検定（0172 §2.4）


def sign_test_p_value(model_better: int, nonzero_blocks: int) -> Fraction:
    """片側の符号検定の正確な p 値 ``P(X ≥ model_better)``（``X ~ Binomial(n, 1/2)``）。

    整数と分数で計算し、乱数も浮動小数も使わない。ブロックが無ければ 1（何も言えない）。
    """
    if nonzero_blocks < 0 or not 0 <= model_better <= nonzero_blocks:
        raise ValueError("符号検定の数が範囲の外")
    if nonzero_blocks == 0:
        return Fraction(1)
    tail = sum(math.comb(nonzero_blocks, k) for k in range(model_better, nonzero_blocks + 1))
    return Fraction(tail, 2**nonzero_blocks)


def stratum_status(
    examples: int, blocks: int, minimum: MinimumConfig
) -> Literal["sufficient", "insufficient"]:
    """層の件数・ブロック数が下限に届くか（届かない層は合否の条件にしない。0172 §2.4）。"""
    if examples >= minimum.stratum_examples.value and blocks >= minimum.stratum_blocks.value:
        return "sufficient"
    return "insufficient"


# ---------------------------------------------------------------- gate（0172 §2.6）


def _evidence(report: ThermalEvaluationReport, config: ThermalEvaluationConfig) -> list[str]:
    reasons: list[str] = []
    evaluation = report.evaluation
    minimum = config.minimum
    report_metrics = frozenset(output.metric for output in report.outputs)
    if report_metrics != config.output_metrics():
        reasons.append(
            "設定の outputs が target schema と一致しない"
            f"（設定: {sorted(config.output_metrics())}・target: {sorted(report_metrics)}）"
        )
    if evaluation.test_examples < minimum.test_examples.value:
        reasons.append(
            f"test の example が下限に届かない（{evaluation.test_examples} < "
            f"{minimum.test_examples.value}）"
        )
    if evaluation.overall.blocks < minimum.blocks.value:
        reasons.append(
            f"時間ブロックが下限に届かない（{evaluation.overall.blocks} < {minimum.blocks.value}）"
        )
    if evaluation.test_examples == 0 or Fraction(evaluation.predicted_examples) < Fraction(
        evaluation.test_examples
    ) * Fraction(minimum.predicted_fraction.value):
        reasons.append(
            f"予測できた割合が下限に届かない（{evaluation.predicted_examples} / "
            f"{evaluation.test_examples} < {minimum.predicted_fraction.value}）"
        )
    keys = tuple(output.key for output in evaluation.overall.outputs)
    if keys != report.outputs:
        reasons.append("全体の結果の出力が target schema の出力と一致しない")
    for output in evaluation.overall.outputs:
        if output.paired is None:
            reasons.append(f"{output.key.label} に persistence と対になる値が無い")
    for stratum in evaluation.strata:
        if stratum.status != stratum_status(stratum.examples, stratum.blocks, minimum):
            reasons.append(f"層 {stratum.axis}={stratum.name} の status が設定の下限と一致しない")
    return reasons


def _gating_strata(
    report: ThermalEvaluationReport, config: ThermalEvaluationConfig
) -> list[StratumResult]:
    return [
        stratum
        for stratum in report.evaluation.strata
        if stratum.axis in GATING_AXES
        and stratum_status(stratum.examples, stratum.blocks, config.minimum) == "sufficient"
    ]


def _underprediction(report: ThermalEvaluationReport, config: ThermalEvaluationConfig) -> list[str]:
    gate = config.gate.underprediction
    quantile = gate.quantile.value
    reasons: list[str] = []
    groups: list[tuple[str, tuple[OutputResult, ...]]] = [
        ("全体", report.evaluation.overall.outputs)
    ]
    groups.extend(
        (f"層 {stratum.axis}={stratum.name}", stratum.outputs)
        for stratum in _gating_strata(report, config)
    )
    for where, outputs in groups:
        for output in outputs:
            summary = output.model
            if summary is None:
                reasons.append(f"{where} の {output.key.label} に model の誤差が無い")
                continue
            value = summary.underprediction_at(quantile)
            if value is None:
                reasons.append(f"{where} の {output.key.label} に分位 {quantile} の値が無い")
            elif value > gate.maximum_c.value:
                reasons.append(
                    f"{where} の {output.key.label} の underprediction の分位 {quantile} が上限を"
                    f"超える（{value} > {gate.maximum_c.value}）"
                )
    # worst-case は層で薄めない（全体の最大は各層の最大以上）
    for output in report.evaluation.overall.outputs:
        summary = output.model
        if summary is not None and summary.underprediction_max > gate.worst_case_maximum_c.value:
            reasons.append(
                f"{output.key.label} の underprediction の最大が上限を超える"
                f"（{summary.underprediction_max} > {gate.worst_case_maximum_c.value}）"
            )
    return reasons


def _skill(report: ThermalEvaluationReport, config: ThermalEvaluationConfig) -> list[str]:
    gate = config.gate.skill
    alpha = Fraction(gate.alpha.value)
    reasons: list[str] = []
    for output in report.evaluation.overall.outputs:
        paired = output.paired
        if paired is None:
            reasons.append(f"{output.key.label} に persistence と対になる値が無い")
            continue
        persistence_mae = paired.persistence.mae
        if persistence_mae <= 0.0:
            reasons.append(f"{output.key.label} は persistence の MAE が 0 で、改善を示せない")
        else:
            improvement = 1.0 - paired.model.mae / persistence_mae
            if improvement < gate.minimum_mae_improvement_fraction.value:
                reasons.append(
                    f"{output.key.label} の MAE の改善の割合が下限に届かない"
                    f"（{improvement} < {gate.minimum_mae_improvement_fraction.value}）"
                )
        test = paired.sign_test
        p_value = sign_test_p_value(test.model_better, test.model_better + test.persistence_better)
        if p_value > alpha:
            reasons.append(
                f"{output.key.label} のブロックの符号検定の p 値が alpha を超える"
                f"（{float(p_value)} > {gate.alpha.value}。model が良いブロック "
                f"{test.model_better} / {test.model_better + test.persistence_better}）"
            )
    tolerance = config.gate.stratum_regression_tolerance_c.value
    for stratum in _gating_strata(report, config):
        for output in stratum.outputs:
            where = f"層 {stratum.axis}={stratum.name} の {output.key.label}"
            paired = output.paired
            if paired is None:
                reasons.append(f"{where} に persistence と対になる値が無い")
                continue
            excess = paired.model.mae - paired.persistence.mae
            if excess > tolerance:
                reasons.append(
                    f"{where} で model の MAE が persistence を許容より上回る"
                    f"（{excess} > {tolerance}）"
                )
    return reasons


def evaluate_gate(report: ThermalEvaluationReport, config: ThermalEvaluationConfig) -> GateResult:
    """報告の数字と設定だけから gate を作る（0172 §2.6 / §2.7 の 4）。

    報告の ``gate`` の欄は読まない。

        段は辞書式で、落ちた最初の段の理由だけを返す。欠測・下限未満は ``blocked``。
    """
    checks: tuple[
        tuple[GateStage, Callable[[ThermalEvaluationReport, ThermalEvaluationConfig], list[str]]],
        ...,
    ] = (
        (STAGE_EVIDENCE, _evidence),
        (STAGE_UNDERPREDICTION, _underprediction),
        (STAGE_SKILL, _skill),
    )
    for stage, check in checks:
        reasons = check(report, config)
        if reasons:
            return GateResult(outcome="blocked", failed_stage=stage, reasons=tuple(reasons))
    return GateResult(outcome="pass", failed_stage=None, reasons=())
