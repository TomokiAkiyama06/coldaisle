"""反実仮想 Thermal Model artifact v2 と、Registry の検証経路だけが作る封をした型（#84）。

決定記録 0079 §2.9 の**段 2**（artifact v2・読み込み時の検査 L1〜L10・Registry metadata の
全欄照合）と、0084 のうち段 2 に属する点（anchor 推論の action 列の規則 ``hold_effective``、
L8 のキー集合の照合、L11 / L12）を実装する。

- artifact は 0048 §2.4 と同じ規律（strict schema の canonical UTF-8 JSON だけ。
  pickle / 任意 import / 実行可能 code を持たない）に従う
- Confidence Profile v2 は artifact の**1区画**として同梱する（0079 §2.1）。この module は
  Profile v2 の**形**と、読み込み時にその形を検査する部分（L6 / L7 / L11 / L12）だけを持つ。
  Profile v2 を train / validation から**作る**処理と、同梱 Profile から作る判定器、step ごとの
  support の照合関数は段 3（#85。``counterfactual_confidence.py``）、MPC への束縛と候補の照合は
  段 4（#86）が足す
- ここは Demand・PWM・authority を返さず、``control.hardware`` / ``control.safety`` /
  ``control.reactive`` を import しない（0079 §2.6）。LLM 層へは何も出さない

**検査に1つでも外れた artifact は型にならない。** 呼び出し側（段 4）はこの失敗を
``MODEL_LOAD_FAILURE`` として Gate へ渡し、Fallback で運転を続ける（0079 §2.6）。
暗黙の降格（Profile なしで使う・observational として使う）の経路は作らない。
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from coldaisle.control.model.calibration_digest import (
    RuntimeCalibration,
    calibrated_metrics,
    calibration_digest,
)
from coldaisle.control.model.confidence import (
    FAN_SOURCE_PREFIX,
    MAX_MISSING_PATTERNS,
    MAX_SUPPORT_CELLS,
    ConfidenceProfileSpec,
    MissingPatternCount,
    ResidualScale,
    SupportAxis,
    SupportCellCount,
    ValueRange,
)
from coldaisle.control.model.thermal import (
    MAX_ARTIFACT_BYTES,
    MAX_FEATURE_COLUMNS,
    MAX_FEATURE_METRICS,
    MAX_METADATA_TEXT_LENGTH,
    MAX_MODEL_TRAINING_EXAMPLES,
    MAX_SOURCE_RUNS,
    MAX_TARGET_METRICS,
    MAX_TARGET_OUTPUTS,
    MAX_TIME_MS,
    ArtifactVerification,
    DatasetArtifactId,
    InferenceCapability,
    ModelId,
    ObservedThermalInput,
    PositiveDurationMs,
    PredictedTarget,
    RidgeHyperparameters,
    RidgeModelPayload,
    SemanticVersion,
    Sha256,
    ThermalFeatureSchema,
    ThermalMetricName,
    ThermalPrediction,
    ThermalTargetSchema,
    TimestampMs,
    TrainingSourceProvenance,
    _feature_columns,
    _raw_feature_values_validated,
    _validate_feature_shape_values,
    _validate_metric_name_lengths,
    _validated_observed_input,
    canonical_json_bytes,
    canonical_sha256,
    is_pickle_payload,
)
from coldaisle.control.model_registry import (
    ArtifactCapability,
    ArtifactKind,
    VerifiedArtifact,
)
from coldaisle.control.schema import STAGE_ORDER, AuthorityStage, Demand, PerZone, Zone
from coldaisle.metrics import MetricCatalog

COUNTERFACTUAL_ARTIFACT_SCHEMA_VERSION: Literal[2] = 2
"""``coldaisle.thermal_model`` の v2。v1 を v2 として読み替えない（0079 §2.2）。"""
FEATURE_SCHEMA_V2_VERSION: Literal["thermal-features-v2"] = "thermal-features-v2"
TARGET_SCHEMA_V2_VERSION: Literal["thermal-targets-v1"] = "thermal-targets-v1"
"""v2 の target の layout は v1 と同じ（0079 §2.3）。"""
ACTION_SCHEMA_VERSION: Literal["thermal-actions-v1"] = "thermal-actions-v1"
CONFIDENCE_PROFILE_V2_SCHEMA_VERSION: Literal[2] = 2
ANCHOR_ACTION_RULE: Literal["hold_effective"] = "hold_effective"
"""anchor 推論の計画 action の列の規則（0084 §2.1）。v2 で取れる値はこれだけ。"""

DATASET_V2: Literal[2] = 2
ZONE_ORDER: tuple[Zone, ...] = (Zone.FRONT, Zone.REAR, Zone.TOP)
"""action schema の zone の順（0079 §2.3）。support cell の bin の順もこれに従う。"""

MAX_METRIC_BINDING_ENTRIES = MAX_FEATURE_METRICS + MAX_TARGET_METRICS
"""``metric_binding`` の entry 数の構造上限。feature と target の metric の和集合を超えない。"""
MAX_ACTION_STEPS = MAX_FEATURE_COLUMNS // len(ZONE_ORDER)
"""action 列の step 数の構造上限。計画 action の列だけで feature column の上限を超えない値。"""
MAX_UNIT_LENGTH = 64

_MODEL_FAMILY: Literal["ridge_linear_v1"] = "ridge_linear_v1"
_FAN_SOURCES: tuple[str, ...] = tuple(f"{FAN_SOURCE_PREFIX}{zone.value}" for zone in ZONE_ORDER)
_ARTIFACT_DETERMINED_EXCLUSIONS: frozenset[str] = frozenset(
    {"schema_version", "offline_evaluation_ref", "shadow_evaluation_ref"}
)
"""artifact が決めない Registry metadata の欄（0079 §2.3 / 0061 §2.4）。

評価の参照は lifecycle の途中で Registry が書く。``schema_version`` は Registry の封筒の版で、
artifact が決める値ではない（v1 の ``_registry_contract`` と同じ扱い）。
"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ArtifactCheck(StrEnum):
    """読み込み時の検査（0079 §2.4 の L1〜L10 と 0084 の L11 / L12）。"""

    VERIFIED_ARTIFACT = "L1"
    SIZE_AND_STRUCTURE = "L2"
    CANONICAL_BYTES = "L3"
    SCHEMA_VERSION = "L4"
    REGISTRY_METADATA = "L5"
    DIGESTS = "L6"
    PROFILE_BINDING = "L7"
    METRIC_BINDING = "L8"
    CALIBRATION = "L9"
    CAUSAL_MASK = "L10"
    ANCHOR_ACTION_RULE = "L11"
    PROFILE_STEPS = "L12"


class CounterfactualArtifactRejectedError(ValueError):
    """反実仮想 artifact v2 が検査に外れ、型にならなかった。

    ``check`` は外れた検査（0079 §2.4 / 0084 の表の番号）。呼び出し側は理由によらず
    ``MODEL_LOAD_FAILURE`` として Fallback にする（0079 §2.6）。
    """

    def __init__(self, check: ArtifactCheck, detail: str) -> None:
        self.check = check
        self.detail = detail
        super().__init__(f"{check.value}: {detail}")


# ---------------------------------------------------------------- schema


class ThermalActionSchema(_Frozen):
    """action schema ``thermal-actions-v1``（0079 §2.3）。

    step ``k`` の action は区間 ``[k × step_ms, (k + 1) × step_ms)`` に掛かる
    （0087 §2.5。``ActionPlan.steps[k].offset_ms = step_ms × (k + 1)`` はその区間の終端）。
    """

    schema_version: Literal["thermal-actions-v1"] = ACTION_SCHEMA_VERSION
    step_ms: PositiveDurationMs
    steps: int = Field(gt=0, le=MAX_ACTION_STEPS)
    zones: tuple[Zone, ...] = ZONE_ORDER
    unit: Literal["demand"] = "demand"
    """demand（0.0..1.0）。"""
    action_source: Literal["effective_demand"] = "effective_demand"
    """action の出どころ。0031 §2 の「熱応答の action は effective_demand」に従う。"""

    @model_validator(mode="after")
    def _grid_is_bounded(self) -> Self:
        if self.zones != ZONE_ORDER:
            raise ValueError("action schema の zone は Front / Rear / Top の順にする")
        if self.step_ms * self.steps > MAX_TIME_MS:
            raise ValueError("action の格子が時刻の上限を超えている")
        return self


class ThermalFeatureSchemaV2(_Frozen):
    """feature schema ``thermal-features-v2``（0079 §2.3）。

    v1（0048 §2.1）の列（window の cell と anchor action）のあとに、計画 action の列
    ``plan[k].<zone>.effective_demand``（step × zone）を足す。anchor action は「いま掛かって
    いる effective demand」で、Dataset v2 では ``prior_action``（0087 §2.7）。
    """

    schema_version: Literal["thermal-features-v2"] = FEATURE_SCHEMA_V2_VERSION
    dataset_schema_version: Literal[2] = DATASET_V2
    window_ms: PositiveDurationMs
    sample_period_ms: PositiveDurationMs
    stale_after_ms: PositiveDurationMs
    metrics: tuple[ThermalMetricName, ...] = Field(min_length=1, max_length=MAX_FEATURE_METRICS)
    action_steps: int = Field(gt=0, le=MAX_ACTION_STEPS)
    columns: tuple[str, ...] = Field(min_length=1, max_length=MAX_FEATURE_COLUMNS)

    @model_validator(mode="after")
    def _layout_is_canonical(self) -> Self:
        _validate_metric_name_lengths(self.metrics)
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("feature metric を重複させない")
        _frames, observation_columns = _validate_feature_shape_values(
            window_ms=self.window_ms,
            sample_period_ms=self.sample_period_ms,
            metric_count=len(self.metrics),
        )
        if observation_columns + self.action_steps * len(ZONE_ORDER) > MAX_FEATURE_COLUMNS:
            raise ValueError("feature column 数が artifact 安全上限を超えている")
        if self.columns != feature_columns_v2(
            window_ms=self.window_ms,
            sample_period_ms=self.sample_period_ms,
            metrics=self.metrics,
            action_steps=self.action_steps,
        ):
            raise ValueError("feature column の順序または内容が v2 canonical layout と一致しない")
        return self

    def observation_schema(self) -> ThermalFeatureSchema:
        """window と anchor action の部分（v1 と同じ layout）だけの schema を返す。

        観測の検証と平坦化を v1 と同じ関数で行うために使う。artifact へは書かない。
        """
        return ThermalFeatureSchema(
            window_ms=self.window_ms,
            sample_period_ms=self.sample_period_ms,
            stale_after_ms=self.stale_after_ms,
            metrics=self.metrics,
            columns=_feature_columns(
                window_ms=self.window_ms,
                sample_period_ms=self.sample_period_ms,
                metrics=self.metrics,
            ),
        )

    def plan_column_index(self, step: int, zone: Zone) -> int:
        """計画 action の列 ``plan[step].<zone>`` の位置。"""
        return (
            len(self.columns)
            - self.action_steps * len(ZONE_ORDER)
            + step * len(ZONE_ORDER)
            + ZONE_ORDER.index(zone)
        )


def feature_columns_v2(
    *, window_ms: int, sample_period_ms: int, metrics: tuple[str, ...], action_steps: int
) -> tuple[str, ...]:
    """``thermal-features-v2`` の列の並び。"""
    plan = tuple(
        f"plan[{step}].{zone.value}.effective_demand"
        for step in range(action_steps)
        for zone in ZONE_ORDER
    )
    return (
        _feature_columns(window_ms=window_ms, sample_period_ms=sample_period_ms, metrics=metrics)
        + plan
    )


def causal_plan_steps(action_schema: ThermalActionSchema, horizon_ms: int) -> int:
    """horizon ``h_ms`` の target が使ってよい計画 action の step の数（0079 §2.3 の因果の mask）。

    step ``k`` は ``k × step_ms < h_ms`` のときだけ使える。mask は格子から一意に導くので、
    artifact に別の欄を持たない。
    """
    return min(action_schema.steps, -(-horizon_ms // action_schema.step_ms))


# ---------------------------------------------------------------- manifest


class DerivedMetricDefinition(_Frozen):
    """派生値の定義（引き算1つ。``config/metrics.yaml``）。"""

    minuend: ThermalMetricName
    subtrahend: ThermalMetricName


class MetricBindingEntry(_Frozen):
    """学習時の ``MetricCatalog`` の単位と派生値の定義。**表示名は含めない**（0079 §2.3）。"""

    metric: ThermalMetricName
    unit: str = Field(min_length=1, max_length=MAX_UNIT_LENGTH)
    derived: DerivedMetricDefinition | None


class MetricBinding(_Frozen):
    """feature / target の metric の意味と単位の束縛（0079 §2.3 / 0084 §2.3）。"""

    entries: tuple[MetricBindingEntry, ...] = Field(
        min_length=1, max_length=MAX_METRIC_BINDING_ENTRIES
    )
    sha256: Sha256


class CalibrationBinding(_Frozen):
    """学習データに効いていた較正値のうち、使った metric に関わるものの digest。

    意味は ``calibration-digest-v1``（決定記録 0096）。較正の掛かる metric を使わなければ ``None``。
    store は較正の出どころを記録しないので、呼び出し側は**較正の値**を明示して trainer へ渡し、
    digest は trainer と loader が同じ関数（:func:`calibration_digest`）で計算する
    （0079 §2.3 を 0096 §2.6 で部分的に置き換えた。既定値を置かない）。
    """

    sha256: Sha256 | None


class TimeWindow(_Frozen):
    """split の1集合の ``[history_start_ms の最小, label_end_ms の最大]``。"""

    start_ms: TimestampMs
    end_ms: TimestampMs

    @model_validator(mode="after")
    def _is_ordered(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("学習データの時間窓は start_ms <= end_ms にする")
        return self


class TrainingWindows(_Frozen):
    """学習データの時間窓（0079 §2.3）。空の集合は ``None``。"""

    train: TimeWindow
    validation: TimeWindow | None
    test: TimeWindow | None


class ZoneActionVariation(_Frozen):
    """train の action 列で、隣り合う action（``prior_action`` → step 0、step ``k`` → ``k + 1``）の
    組の数と、そのうち値が変わった組の数。
    """

    transitions: int = Field(ge=0)
    changed: int = Field(ge=0)

    @model_validator(mode="after")
    def _changed_is_bounded(self) -> Self:
        if self.changed > self.transitions:
            raise ValueError("値が変わった組の数は組の数を超えない")
        return self


class TrainingDataProvenanceV2(_Frozen):
    """学習データの出どころ（0079 §2.3 の ``training_data``）。"""

    dataset_alias: DatasetArtifactId
    dataset_schema_version: Literal[2] = DATASET_V2
    manifest_sha256: Sha256
    examples_sha256: Sha256
    source_runs: tuple[TrainingSourceProvenance, ...] = Field(
        min_length=1, max_length=MAX_SOURCE_RUNS
    )
    split_sha256: Sha256
    window: TrainingWindows
    train_example_count: int = Field(gt=0, le=MAX_MODEL_TRAINING_EXAMPLES)
    action_variation_summary: PerZone[ZoneActionVariation]

    @model_validator(mode="after")
    def _source_runs_are_canonical(self) -> Self:
        run_ids = tuple(source.run_id for source in self.source_runs)
        if run_ids != tuple(sorted(set(run_ids))):
            raise ValueError("source run は公開 alias の重複なし昇順にする")
        return self


class ArtifactSchemasV2(_Frozen):
    """feature / target / action schema の版と canonical SHA-256。"""

    feature_version: Literal["thermal-features-v2"] = FEATURE_SCHEMA_V2_VERSION
    feature_sha256: Sha256
    target_version: Literal["thermal-targets-v1"] = TARGET_SCHEMA_V2_VERSION
    target_sha256: Sha256
    action_version: Literal["thermal-actions-v1"] = ACTION_SCHEMA_VERSION
    action_sha256: Sha256


def _validate_created_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at は RFC 3339 形式にする") from exc
    if parsed.utcoffset() is None:
        raise ValueError("created_at には timezone が必要")
    return value


def validate_authority_compatibility(stages: tuple[AuthorityStage, ...]) -> None:
    """authority の互換は SHADOW から順に並べる（0079 §2.3）。

    実測評価で裏づけた stage だけを、SHADOW から飛ばさずに持つ。
    """
    if not stages or stages != STAGE_ORDER[: len(stages)]:
        raise ValueError("authority_compatibility は SHADOW から順に飛ばさず並べる")


class CounterfactualModelManifest(_Frozen):
    """反実仮想 Thermal Model artifact v2 の manifest（0079 §2.3）。"""

    model_id: ModelId
    model_version: SemanticVersion
    created_at: str = Field(min_length=1, max_length=64)
    model_family: Literal["ridge_linear_v1"] = _MODEL_FAMILY
    capability: Literal[InferenceCapability.COUNTERFACTUAL_ACTION]
    authority_compatibility: tuple[AuthorityStage, ...] = Field(
        min_length=1, max_length=len(STAGE_ORDER)
    )
    anchor_action_rule: Literal["hold_effective"]
    training_data: TrainingDataProvenanceV2
    schemas: ArtifactSchemasV2
    metric_binding: MetricBinding
    calibration_binding: CalibrationBinding
    hyperparameters: RidgeHyperparameters
    code_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    payload_sha256: Sha256
    confidence_profile_sha256: Sha256

    @field_validator("created_at")
    @classmethod
    def _created_at_is_timezone_aware_rfc3339(cls, value: str) -> str:
        return _validate_created_at(value)

    @model_validator(mode="after")
    def _authority_starts_at_shadow(self) -> Self:
        validate_authority_compatibility(self.authority_compatibility)
        return self


# ---------------------------------------------------------------- Confidence Profile v2（形だけ）

ActionCell = tuple[int, int, int]
"""全 zone の計画 demand の組が落ちる cell（Front / Rear / Top の ``fan.<zone>`` 軸の bin）。"""


class ActionCellCount(_Frozen):
    """1つの cell と、train での件数（0079 §2.5 の (a)）。"""

    cell: ActionCell
    count: int = Field(gt=0)


class ActionCellTransitionCount(_Frozen):
    """cell の組（遷移）と、train での件数（0079 §2.5 の (b) / (c)）。"""

    source: ActionCell
    target: ActionCell
    count: int = Field(gt=0)


class StepActionSupport(_Frozen):
    """step ``k`` の計画 demand の zone ごとの範囲と、全 zone の組の cell（0084 §2.2）。"""

    step: int = Field(ge=0)
    demand_ranges: PerZone[ValueRange]
    cells: tuple[ActionCellCount, ...] = Field(max_length=MAX_SUPPORT_CELLS)


class StepTransitionSupport(_Frozen):
    """step の組 ``(k, k + 1)`` の変化量の範囲と cell の組（0084 §2.2 の (c)）。"""

    from_step: int = Field(ge=0)
    delta_ranges: PerZone[ValueRange]
    cells: tuple[ActionCellTransitionCount, ...] = Field(max_length=MAX_SUPPORT_CELLS)


class AnchorTransitionSupport(_Frozen):
    """anchor action → 最初の step の変化量の範囲と cell の組（0079 §2.5 の (b)）。"""

    delta_ranges: PerZone[ValueRange]
    cells: tuple[ActionCellTransitionCount, ...] = Field(max_length=MAX_SUPPORT_CELLS)


class ActionSupportV2(_Frozen):
    """学習した action 列の範囲（0079 §2.1 の表、0084 §2.2 で step ごと・step の組ごと）。

    step の欄の数と番号の検査は読み込み時の L12 が action schema に対して行う。
    """

    steps: tuple[StepActionSupport, ...] = Field(max_length=MAX_ACTION_STEPS)
    anchor_to_first: AnchorTransitionSupport
    transitions: tuple[StepTransitionSupport, ...] = Field(max_length=MAX_ACTION_STEPS)


class ProfileBindingV2(_Frozen):
    """Profile v2 が束縛する model（0079 §2.1）。artifact 全体の hash は持たない。

    推論ごとの照合に使う ``artifact_sha256`` は、封をした型が同じ bytes から足す（0079 §2.1）。
    """

    model_id: ModelId
    model_version: SemanticVersion
    payload_sha256: Sha256
    feature_schema_sha256: Sha256
    target_schema_sha256: Sha256
    action_schema_sha256: Sha256
    training_split_sha256: Sha256


class ConfidenceProfileV2(_Frozen):
    """Confidence Profile v2（``coldaisle.confidence_profile`` v2）の**形**。

    中身を train / validation から作るのは段 3（#85）。ここは artifact の区画として読み書きし、
    読み込み時に束縛と形を検査するために必要な部分だけを持つ。
    """

    schema_name: Literal["coldaisle.confidence_profile"] = "coldaisle.confidence_profile"
    schema_version: Literal[2] = CONFIDENCE_PROFILE_V2_SCHEMA_VERSION
    binding: ProfileBindingV2
    anchor_action_rule: Literal["hold_effective"]
    spec: ConfidenceProfileSpec
    train_example_count: int = Field(gt=0, le=MAX_MODEL_TRAINING_EXAMPLES)
    feature_ranges: tuple[ValueRange, ...] = Field(min_length=1, max_length=MAX_FEATURE_METRICS)
    fan_ranges: PerZone[ValueRange]
    missing_patterns: tuple[MissingPatternCount, ...] = Field(
        min_length=1, max_length=MAX_MISSING_PATTERNS
    )
    support_cells: tuple[SupportCellCount, ...] = Field(max_length=MAX_SUPPORT_CELLS)
    residual_scales: tuple[ResidualScale, ...] = Field(min_length=1, max_length=MAX_TARGET_OUTPUTS)
    residual_validation_example_count: int = Field(gt=0)
    """residual の基準に使った validation example の数（0084 §2.1）。"""
    residual_excluded_example_count: int = Field(ge=0)
    """held の列が step ごとの support の外にあり、基準から除いた validation example の数。"""
    action_support: ActionSupportV2

    @model_validator(mode="after")
    def _structure_is_consistent(self) -> Self:
        axes = {axis.source: axis for axis in self.spec.support_axes}
        missing_fan_axes = [source for source in _FAN_SOURCES if source not in axes]
        if missing_fan_axes:
            # 候補の同時の support は fan.<zone> の境界で分ける。明示が無ければ作らない（0079 §2.5）
            raise ValueError(f"Profile v2 には全 zone の fan.<zone> 軸が要る: {missing_fan_axes}")
        fan_axes = tuple(axes[source] for source in _FAN_SOURCES)
        for zone, source in zip(ZONE_ORDER, _FAN_SOURCES, strict=True):
            if self.fan_ranges.get(zone).source != source:
                raise ValueError("fan_ranges の source は fan.<zone> にする")
        patterns = [item.unavailable_metrics for item in self.missing_patterns]
        if len(set(patterns)) != len(patterns):
            raise ValueError("欠測の組み合わせを重複させない")
        cells = [item.bins for item in self.support_cells]
        if len(set(cells)) != len(cells):
            raise ValueError("support cell を重複させない")
        for bins in cells:
            if len(bins) != len(self.spec.support_axes):
                raise ValueError("support cell の次元が軸の数と一致しない")
            for index, axis in zip(bins, self.spec.support_axes, strict=True):
                if not 0 <= index <= len(axis.edges):
                    raise ValueError("support cell の bin が軸の範囲外")
        if any(
            scale.validation_samples > self.residual_validation_example_count
            for scale in self.residual_scales
        ):
            raise ValueError("residual の基準の件数が基準に使った validation example 数を超える")
        support = self.action_support
        for step in support.steps:
            _check_zone_ranges(step.demand_ranges)
            _check_cells(tuple(item.cell for item in step.cells), fan_axes)
        _check_zone_ranges(support.anchor_to_first.delta_ranges)
        _check_transitions(support.anchor_to_first.cells, fan_axes)
        for transition in support.transitions:
            _check_zone_ranges(transition.delta_ranges)
            _check_transitions(transition.cells, fan_axes)
        return self

    def canonical_bytes(self) -> bytes:
        """checksum と保存に使う canonical JSON。"""
        return canonical_json_bytes(self)

    def sha256(self) -> str:
        """Profile v2 の canonical SHA-256（manifest の ``confidence_profile_sha256``）。"""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _check_zone_ranges(ranges: PerZone[ValueRange]) -> None:
    for zone, source in zip(ZONE_ORDER, _FAN_SOURCES, strict=True):
        if ranges.get(zone).source != source:
            raise ValueError("action の範囲の source は fan.<zone> にする")


def _check_cells(cells: tuple[ActionCell, ...], axes: tuple[SupportAxis, ...]) -> None:
    if cells != tuple(sorted(set(cells))):
        raise ValueError("action の cell は重複なし昇順にする")
    for cell in cells:
        _check_cell(cell, axes)


def _check_transitions(
    transitions: tuple[ActionCellTransitionCount, ...], axes: tuple[SupportAxis, ...]
) -> None:
    pairs = tuple((item.source, item.target) for item in transitions)
    if pairs != tuple(sorted(set(pairs))):
        raise ValueError("action の cell の組は重複なし昇順にする")
    for source, target in pairs:
        _check_cell(source, axes)
        _check_cell(target, axes)


def _check_cell(cell: ActionCell, axes: tuple[SupportAxis, ...]) -> None:
    for index, axis in zip(cell, axes, strict=True):
        if not 0 <= index <= len(axis.edges):
            raise ValueError("action の cell の bin が fan.<zone> 軸の範囲外")


# ---------------------------------------------------------------- artifact


class CounterfactualThermalModelArtifact(_Frozen):
    """反実仮想 Thermal Model artifact v2（0079 §2.3）。

    Pydantic の検証は**形**（列数・出力の順・格子の整合）だけを見る。digest・束縛・mask・
    Profile の step の数は、作成時と読み込み時に :func:`check_artifact_contents` が
    番号つきで検査する。
    """

    schema_name: Literal["coldaisle.thermal_model"] = "coldaisle.thermal_model"
    schema_version: Literal[2] = COUNTERFACTUAL_ARTIFACT_SCHEMA_VERSION
    manifest: CounterfactualModelManifest
    feature_schema: ThermalFeatureSchemaV2
    target_schema: ThermalTargetSchema
    action_schema: ThermalActionSchema
    payload: RidgeModelPayload
    confidence_profile: ConfidenceProfileV2

    @model_validator(mode="after")
    def _shapes_match(self) -> Self:
        feature_count = len(self.feature_schema.columns)
        if self.target_schema.dataset_schema_version != DATASET_V2:
            raise ValueError("v2 artifact の target schema は Dataset v2 由来にする")
        if self.feature_schema.action_steps != self.action_schema.steps:
            raise ValueError("feature schema の計画 action の step 数が action schema と一致しない")
        if any(horizon % self.action_schema.step_ms for horizon in self.target_schema.horizons_ms):
            raise ValueError("target の horizon は action の格子の上に置く")
        if (
            self.target_schema.horizons_ms[-1]
            != self.action_schema.step_ms * self.action_schema.steps
        ):
            raise ValueError("action の格子の終端は最大の horizon と一致させる")
        if not (
            len(self.payload.feature_means)
            == len(self.payload.feature_scales)
            == len(self.payload.feature_observed_counts)
            == feature_count
        ):
            raise ValueError("normalization vector と feature column 数が一致しない")
        if any(count < 0 for count in self.payload.feature_observed_counts):
            raise ValueError("feature observed count を負にしない")
        expected_outputs = tuple(
            (horizon, metric)
            for horizon in self.target_schema.horizons_ms
            for metric in self.target_schema.metrics
        )
        if (
            tuple((output.horizon_ms, output.metric) for output in self.payload.outputs)
            != expected_outputs
        ):
            raise ValueError("ridge output の順序または集合が target schema と一致しない")
        if any(len(output.coefficients) != feature_count for output in self.payload.outputs):
            raise ValueError("ridge coefficient 数が feature column 数と一致しない")
        train_count = self.manifest.training_data.train_example_count
        if any(count > train_count for count in self.payload.feature_observed_counts):
            raise ValueError("feature observed count が train example 数を超えている")
        if any(output.training_samples > train_count for output in self.payload.outputs):
            raise ValueError("output training sample 数が train example 数を超えている")
        if self.manifest.model_family != self.payload.model_family:
            raise ValueError("manifest と payload の model family が一致しない")
        return self


def metric_binding_sha256(entries: tuple[MetricBindingEntry, ...]) -> str:
    """``metric_binding.entries`` の canonical SHA-256。"""
    encoded = (
        json.dumps(
            [entry.model_dump(mode="json") for entry in entries],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    return hashlib.sha256(encoded).hexdigest()


def build_metric_binding(metrics: tuple[str, ...], catalog: MetricCatalog) -> MetricBinding:
    """使った metric の単位（派生値は引き算の定義も）を学習時の catalog から写す。

    catalog に無い metric があれば作らない（単位を束縛できない metric で学習しない）。
    """
    entries: list[MetricBindingEntry] = []
    for metric in sorted(set(metrics)):
        if metric in catalog.metrics:
            entries.append(
                MetricBindingEntry(metric=metric, unit=catalog.metrics[metric].unit, derived=None)
            )
        elif metric in catalog.derived:
            derived = catalog.derived[metric]
            entries.append(
                MetricBindingEntry(
                    metric=metric,
                    unit=derived.unit,
                    derived=DerivedMetricDefinition(
                        minuend=derived.minuend, subtrahend=derived.subtrahend
                    ),
                )
            )
        else:
            raise ValueError(f"MetricCatalog に無い metric では artifact を作らない: {metric}")
    items = tuple(entries)
    return MetricBinding(entries=items, sha256=metric_binding_sha256(items))


def check_artifact_contents(
    artifact: CounterfactualThermalModelArtifact,
    *,
    metric_catalog: MetricCatalog | None,
    calibration: RuntimeCalibration | None,
    check_calibration: bool,
) -> None:
    """L6〜L12 を順に検査する。作成時と読み込み時で**同じ関数**を使う（0084 §2.3）。

    ``metric_catalog`` が ``None`` のときは L8 の (3)（runtime の catalog との照合）を飛ばし、
    ``check_calibration`` が偽のときは L9 を飛ばす。どちらも作成時だけの使い方で、
    読み込み時は必ず両方を渡す（``check_calibration`` が真なら ``calibration`` は必須）。
    """
    runtime: RuntimeCalibration | None = None
    if check_calibration:
        if not isinstance(calibration, RuntimeCalibration):
            raise TypeError("L9 には runtime の較正（RuntimeCalibration）を渡す")
        runtime = calibration
    _check_digests(artifact)
    _check_profile_binding(artifact)
    _check_metric_binding(artifact, metric_catalog)
    if runtime is not None:
        _check_calibration(artifact, runtime)
    _check_causal_mask(artifact)
    _check_anchor_action_rule(artifact)
    _check_profile_steps(artifact)


def _check_digests(artifact: CounterfactualThermalModelArtifact) -> None:
    manifest = artifact.manifest
    schemas = manifest.schemas
    mismatches = [
        name
        for name, expected, actual in (
            ("payload_sha256", manifest.payload_sha256, canonical_sha256(artifact.payload)),
            (
                "confidence_profile_sha256",
                manifest.confidence_profile_sha256,
                artifact.confidence_profile.sha256(),
            ),
            ("feature_schema", schemas.feature_sha256, canonical_sha256(artifact.feature_schema)),
            ("target_schema", schemas.target_sha256, canonical_sha256(artifact.target_schema)),
            ("action_schema", schemas.action_sha256, canonical_sha256(artifact.action_schema)),
            (
                "metric_binding",
                manifest.metric_binding.sha256,
                metric_binding_sha256(manifest.metric_binding.entries),
            ),
        )
        if expected != actual
    ]
    if mismatches:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.DIGESTS, f"再計算した digest が manifest と一致しない: {mismatches}"
        )


def _check_profile_binding(artifact: CounterfactualThermalModelArtifact) -> None:
    manifest = artifact.manifest
    profile = artifact.confidence_profile
    binding = profile.binding
    mismatches = [
        name
        for name, expected, actual in (
            ("model_id", manifest.model_id, binding.model_id),
            ("model_version", manifest.model_version, binding.model_version),
            ("payload_sha256", manifest.payload_sha256, binding.payload_sha256),
            (
                "feature_schema_sha256",
                manifest.schemas.feature_sha256,
                binding.feature_schema_sha256,
            ),
            ("target_schema_sha256", manifest.schemas.target_sha256, binding.target_schema_sha256),
            ("action_schema_sha256", manifest.schemas.action_sha256, binding.action_schema_sha256),
            (
                "training_split_sha256",
                manifest.training_data.split_sha256,
                binding.training_split_sha256,
            ),
            (
                "train_example_count",
                manifest.training_data.train_example_count,
                profile.train_example_count,
            ),
        )
        if expected != actual
    ]
    if mismatches:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.PROFILE_BINDING,
            f"同梱 Profile の binding が manifest と一致しない: {mismatches}",
        )
    # Profile の各欄が、同じ artifact の schema の並びに沿っていること
    if tuple(item.source for item in profile.feature_ranges) != artifact.feature_schema.metrics:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.PROFILE_BINDING, "Profile の feature_ranges が feature schema の順でない"
        )
    allowed_sources = set(artifact.feature_schema.metrics) | set(_FAN_SOURCES)
    unknown_axes = [
        axis.source for axis in profile.spec.support_axes if axis.source not in allowed_sources
    ]
    if unknown_axes:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.PROFILE_BINDING,
            f"support 軸の source が feature / fan に無い: {unknown_axes}",
        )
    known = set(artifact.feature_schema.metrics)
    if any(
        metric not in known
        for pattern in profile.missing_patterns
        for metric in pattern.unavailable_metrics
    ):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.PROFILE_BINDING, "欠測の組み合わせに feature schema 外の metric がある"
        )
    outputs = tuple((scale.horizon_ms, scale.metric) for scale in profile.residual_scales)
    expected_outputs = tuple(
        (horizon, metric)
        for horizon in artifact.target_schema.horizons_ms
        for metric in artifact.target_schema.metrics
    )
    if outputs != expected_outputs:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.PROFILE_BINDING,
            "Profile の residual_scales が target schema の出力順でない",
        )


def _check_metric_binding(
    artifact: CounterfactualThermalModelArtifact, catalog: MetricCatalog | None
) -> None:
    entries = artifact.manifest.metric_binding.entries
    names = tuple(entry.metric for entry in entries)
    if len(set(names)) != len(names):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.METRIC_BINDING, "metric_binding に同じ metric の entry が複数ある"
        )
    if names != tuple(sorted(names)):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.METRIC_BINDING, "metric_binding の entry は metric 名の昇順にする"
        )
    expected = set(artifact.feature_schema.metrics) | set(artifact.target_schema.metrics)
    if set(names) != expected:
        # 不足も余分も認めない（0084 §2.3）
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.METRIC_BINDING,
            "metric_binding の metric が feature と target の metric の和集合と一致しない"
            f"（不足={sorted(expected - set(names))}; 余分={sorted(set(names) - expected)}）",
        )
    if catalog is None:
        return
    mismatched: list[str] = []
    for entry in entries:
        if entry.derived is None:
            meta = catalog.metrics.get(entry.metric)
            if meta is None or meta.unit != entry.unit:
                mismatched.append(entry.metric)
            continue
        derived = catalog.derived.get(entry.metric)
        if (
            derived is None
            or derived.unit != entry.unit
            or derived.minuend != entry.derived.minuend
            or derived.subtrahend != entry.derived.subtrahend
        ):
            mismatched.append(entry.metric)
    if mismatched:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.METRIC_BINDING,
            f"runtime の MetricCatalog に同じ単位・定義で存在しない metric がある: {mismatched}",
        )


def _check_calibration(
    artifact: CounterfactualThermalModelArtifact, calibration: RuntimeCalibration
) -> None:
    """L9（0096 §2.4 / §2.6）。L8 の後に artifact の ``metric_binding`` から計算して照合する。

    ``null`` は manifest の申告だけで信じない。先に較正の掛かる metric の集合を導き、
    ``null`` / 非 ``null`` がそれと合うかを見る。runtime の較正を読めなかったときは、
    集合が空の artifact だけを通し、空でなければ digest を比べずに拒否する（0096 §5 #8）。
    """
    entries = artifact.manifest.metric_binding.entries
    declared = artifact.manifest.calibration_binding.sha256
    try:
        metrics = calibrated_metrics(entries)
    except ValueError as error:
        raise CounterfactualArtifactRejectedError(ArtifactCheck.CALIBRATION, str(error)) from error
    if not metrics:
        if declared is not None:
            raise CounterfactualArtifactRejectedError(
                ArtifactCheck.CALIBRATION,
                "較正の掛かる metric を使わないのに calibration_binding が null でない",
            )
        return
    if declared is None:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.CALIBRATION,
            f"較正の掛かる metric を使うのに calibration_binding が null である: {sorted(metrics)}",
        )
    if calibration.offsets_c is None:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.CALIBRATION,
            "runtime の較正を読めなかったので、較正の掛かる metric を使う artifact を使わない"
            f"（{calibration.unavailable_reason}）",
        )
    if calibration_digest(entries, calibration.offsets_c) != declared:
        # 不一致は拒否して Fallback（0079 §6 の質問 4）
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.CALIBRATION, "較正の digest が runtime の較正と一致しない"
        )


def _check_causal_mask(artifact: CounterfactualThermalModelArtifact) -> None:
    schema = artifact.feature_schema
    plan_start = len(schema.columns) - schema.action_steps * len(ZONE_ORDER)
    for output in artifact.payload.outputs:
        allowed = causal_plan_steps(artifact.action_schema, output.horizon_ms)
        masked = output.coefficients[plan_start + allowed * len(ZONE_ORDER) :]
        if any(coefficient != 0.0 for coefficient in masked):
            raise CounterfactualArtifactRejectedError(
                ArtifactCheck.CAUSAL_MASK,
                f"horizon {output.horizon_ms} ms の {output.metric} に、horizon より後の step の"
                "計画 action の係数が 0 でない",
            )


def _check_anchor_action_rule(artifact: CounterfactualThermalModelArtifact) -> None:
    rules = (artifact.manifest.anchor_action_rule, artifact.confidence_profile.anchor_action_rule)
    if rules != (ANCHOR_ACTION_RULE, ANCHOR_ACTION_RULE):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.ANCHOR_ACTION_RULE,
            f"manifest と Profile の anchor_action_rule が hold_effective で一致しない: {rules}",
        )


def _check_profile_steps(artifact: CounterfactualThermalModelArtifact) -> None:
    support = artifact.confidence_profile.action_support
    steps = artifact.action_schema.steps
    step_numbers = tuple(item.step for item in support.steps)
    pair_numbers = tuple(item.from_step for item in support.transitions)
    if step_numbers != tuple(range(steps)):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.PROFILE_STEPS,
            f"Profile の step ごとの欄が action schema の step 0..{steps - 1} と一致しない",
        )
    if pair_numbers != tuple(range(steps - 1)):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.PROFILE_STEPS,
            f"Profile の step の組ごとの欄が (0, 1)..({steps - 2}, {steps - 1}) と一致しない",
        )


# ---------------------------------------------------------------- Registry metadata


class CounterfactualRegistryMetadata(_Frozen):
    """#104 ``ArtifactMetadata`` へ1対1で写せる値。**manifest と bytes から導く**（0079 §2.3）。"""

    schema_version: Literal[3] = 3
    kind: Literal["thermal_model"] = "thermal_model"
    artifact_format: Literal["json"] = "json"
    capability: Literal[ArtifactCapability.COUNTERFACTUAL_ACTION] = (
        ArtifactCapability.COUNTERFACTUAL_ACTION
    )
    model_id: ModelId
    version: SemanticVersion
    created_at: str
    training_dataset_version: DatasetArtifactId
    source_runs: tuple[str, ...] = Field(min_length=1, max_length=MAX_SOURCE_RUNS)
    feature_schema_version: Literal["thermal-features-v2"] = FEATURE_SCHEMA_V2_VERSION
    target_schema_version: Literal["thermal-targets-v1"] = TARGET_SCHEMA_V2_VERSION
    code_commit: str | None
    sha256: Sha256
    model_family: Literal["ridge_linear_v1"] = _MODEL_FAMILY
    hyperparameters: dict[str, str | int | float | bool | None] = Field(max_length=16)
    offline_evaluation_ref: str | None = Field(
        default=None, min_length=1, max_length=MAX_METADATA_TEXT_LENGTH
    )
    shadow_evaluation_ref: str | None = Field(
        default=None, min_length=1, max_length=MAX_METADATA_TEXT_LENGTH
    )
    authority_compatibility: tuple[AuthorityStage, ...] = Field(
        min_length=1, max_length=len(STAGE_ORDER)
    )


def _metadata_from_artifact(
    artifact: CounterfactualThermalModelArtifact, artifact_bytes: bytes
) -> CounterfactualRegistryMetadata:
    manifest = artifact.manifest
    return CounterfactualRegistryMetadata(
        model_id=manifest.model_id,
        version=manifest.model_version,
        created_at=manifest.created_at,
        training_dataset_version=manifest.training_data.dataset_alias,
        source_runs=tuple(source.run_id for source in manifest.training_data.source_runs),
        code_commit=manifest.code_commit,
        sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        hyperparameters={"ridge_lambda": manifest.hyperparameters.ridge_lambda},
        authority_compatibility=manifest.authority_compatibility,
    )


def _comparable(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, list | tuple):
        return tuple(_comparable(item) for item in value)
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in sorted(value.items())}
    return value


def registry_metadata_mismatches(
    derived: CounterfactualRegistryMetadata, stored: BaseModel
) -> list[str]:
    """artifact から導いた metadata と Registry の metadata の食い違いを列挙する。

    **欄を手で並べない。** ``CounterfactualRegistryMetadata`` の欄をすべて回り、artifact が
    決めないものだけを名前で除く（0079 §2.3 / 0061 §2.4）。
    """
    missing = object()
    mismatches: list[str] = []
    for name in CounterfactualRegistryMetadata.model_fields:
        if name in _ARTIFACT_DETERMINED_EXCLUSIONS:
            continue
        actual = getattr(stored, name, missing)
        if actual is missing or _comparable(getattr(derived, name)) != _comparable(actual):
            mismatches.append(name)
    return mismatches


def canonical_counterfactual_artifact_bytes(artifact: CounterfactualThermalModelArtifact) -> bytes:
    """Registry へ登録する bytes（canonical 直列化そのもの。0079 §2.3）。

    作成時の検査（L6〜L8 の (1)(2)・L10〜L12）を通らない artifact は直列化しない。
    """
    artifact = CounterfactualThermalModelArtifact.model_validate(artifact.model_dump(mode="python"))
    check_artifact_contents(
        artifact, metric_catalog=None, calibration=None, check_calibration=False
    )
    encoded = canonical_json_bytes(artifact)
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ValueError("反実仮想 thermal model artifact が安全な size 上限を超えている")
    return encoded


def counterfactual_registry_metadata(
    artifact: CounterfactualThermalModelArtifact, artifact_bytes: bytes
) -> CounterfactualRegistryMetadata:
    """登録する bytes から #104 の metadata を導く。bytes は canonical 直列化に限る。"""
    if artifact_bytes != canonical_counterfactual_artifact_bytes(artifact):
        raise ValueError("Registry へ渡す bytes は artifact の canonical 直列化に限る")
    return _metadata_from_artifact(artifact, artifact_bytes)


def counterfactual_registry_metadata_json_bytes(metadata: CounterfactualRegistryMetadata) -> bytes:
    """#104 ``ArtifactMetadata`` へ渡す strict JSON。"""
    return canonical_json_bytes(
        CounterfactualRegistryMetadata.model_validate(metadata.model_dump(mode="python"))
    )


# ---------------------------------------------------------------- 推論


class ActionTrajectory(_Frozen):
    """候補の action 列。``demands[k]`` は区間 ``[k × step_ms, (k + 1) × step_ms)`` に掛かる。

    ``ActionPlan.steps[k].demands`` と同じ区間である（0087 §2.5）。値は「その値が effective
    として掛かった」仮定として model へ渡す（0079 §2.3）。格子は action schema と**完全に一致**
    しなければ予測しない（補間・外挿・丸めをしない）。
    """

    step_ms: PositiveDurationMs
    demands: tuple[PerZone[Demand], ...] = Field(min_length=1, max_length=MAX_ACTION_STEPS)


def hold_effective(
    observed: ObservedThermalInput, action_schema: ThermalActionSchema
) -> ActionTrajectory:
    """anchor 推論の計画 action の列（規則 ``hold_effective``。0084 §2.1）。

    すべての step に、観測の anchor action（いま掛かっている effective demand）をそのまま入れる。
    丸め・補間・clamp をしない。``ActionPlan.held(現在の effective demand, step_ms=..., steps=...)``
    と同じ列である。runtime の anchor 推論、Profile v2 の residual の基準、offline 評価の
    anchor 予測は**すべてこの関数**で列を作る（別の実装を持たない）。
    """
    held = PerZone(
        front=observed.action.front.effective_demand,
        rear=observed.action.rear.effective_demand,
        top=observed.action.top.effective_demand,
    )
    return ActionTrajectory(
        step_ms=action_schema.step_ms,
        demands=tuple(held for _ in range(action_schema.steps)),
    )


def feature_row_v2(
    observed: ObservedThermalInput,
    trajectory: ActionTrajectory,
    feature_schema: ThermalFeatureSchemaV2,
    action_schema: ThermalActionSchema,
) -> tuple[float | None, ...]:
    """観測と action 列を ``thermal-features-v2`` の並びへ平坦化する（学習と推論で共通）。"""
    trajectory = ActionTrajectory.model_validate(trajectory.model_dump(mode="python"))
    if (
        trajectory.step_ms != action_schema.step_ms
        or len(trajectory.demands) != action_schema.steps
    ):
        raise ValueError("action 列の格子が action schema と一致しない（補間・外挿はしない）")
    observed = _validated_observed_input(observed)
    row = _raw_feature_values_validated(observed, feature_schema.observation_schema())
    plan = tuple(demands.get(zone) for demands in trajectory.demands for zone in ZONE_ORDER)
    flattened = row + plan
    if len(flattened) != len(feature_schema.columns):
        raise RuntimeError("internal feature layout mismatch")
    return flattened


def _predict_targets(
    artifact: CounterfactualThermalModelArtifact,
    observed: ObservedThermalInput,
    trajectory: ActionTrajectory,
) -> tuple[PredictedTarget, ...]:
    return predict_targets(
        payload=artifact.payload,
        feature_schema=artifact.feature_schema,
        target_schema=artifact.target_schema,
        action_schema=artifact.action_schema,
        observed=observed,
        trajectory=trajectory,
    )


def predict_targets(
    *,
    payload: RidgeModelPayload,
    feature_schema: ThermalFeatureSchemaV2,
    target_schema: ThermalTargetSchema,
    action_schema: ThermalActionSchema,
    observed: ObservedThermalInput,
    trajectory: ActionTrajectory,
) -> tuple[PredictedTarget, ...]:
    """payload と schema から、観測と action 列に対する target を計算する（推論の本体）。

    封をした型の推論と、artifact へ封じる前の Profile v2 の residual の基準（段 3。0084 §2.1）が
    **同じ関数**を使う。制御へ渡す予測（``ThermalPrediction``）は封をした型だけが作る。
    """
    raw = feature_row_v2(observed, trajectory, feature_schema, action_schema)
    normalized = tuple(
        0.0 if value is None else (value - mean) / scale
        for value, mean, scale in zip(
            raw, payload.feature_means, payload.feature_scales, strict=True
        )
    )
    by_horizon: dict[int, dict[str, float]] = {horizon: {} for horizon in target_schema.horizons_ms}
    for output in payload.outputs:
        try:
            value = output.intercept + math.fsum(
                coefficient * feature
                for coefficient, feature in zip(output.coefficients, normalized, strict=True)
            )
        except OverflowError as exc:
            raise ValueError("thermal prediction が有限範囲を超えた") from exc
        if not math.isfinite(value):
            raise ValueError("thermal prediction が非有限になった")
        by_horizon[output.horizon_ms][output.metric] = value
    return tuple(
        PredictedTarget(
            horizon_ms=horizon,
            expected_ts_ms=observed.action_ts_ms + horizon,
            values=by_horizon[horizon],
        )
        for horizon in target_schema.horizons_ms
    )


class RegistryCounterfactualThermalModel:
    """Registry の検証経路が発行した ``VerifiedArtifact`` からだけ作れる封をした型（0079 §2.4）。

    **公開 constructor を持たない。** 同じ bytes から model と Profile v2 を**両方**組み立てて持ち、
    別々に渡す API を作らない。L1〜L12 のすべてを通ったときだけ型ができる。
    """

    __slots__ = ("_artifact", "_artifact_sha256")
    _artifact: CounterfactualThermalModelArtifact
    _artifact_sha256: str

    def __init__(self) -> None:
        raise TypeError("RegistryCounterfactualThermalModel は from_verified_artifact からだけ作る")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("RegistryCounterfactualThermalModel は不変")

    @classmethod
    def from_verified_artifact(
        cls,
        verified: VerifiedArtifact,
        *,
        metric_catalog: MetricCatalog,
        calibration: RuntimeCalibration,
    ) -> RegistryCounterfactualThermalModel:
        """``VerifiedArtifact`` を L1〜L12 の順に検査し、すべて通ったときだけ型を作る。

        ``metric_catalog`` は runtime の ``config/metrics.yaml``、``calibration`` は runtime の較正
        （起動時に1回だけ読んだ値。読めなければ :meth:`RuntimeCalibration.unavailable`）。
        L9 は artifact の ``metric_binding`` とこの値から digest を計算して照合する
        （決定記録 0096）。
        どちらも既定値を持たない。外れたら
        :class:`CounterfactualArtifactRejectedError`（``check`` に番号）。
        """
        artifact, digest = _load_checked(
            verified, metric_catalog=metric_catalog, calibration=calibration
        )
        model = object.__new__(cls)
        object.__setattr__(model, "_artifact", artifact)
        object.__setattr__(model, "_artifact_sha256", digest)
        return model

    @property
    def manifest(self) -> CounterfactualModelManifest:
        """model の identity と出どころ。"""
        return self._artifact.manifest

    @property
    def feature_schema(self) -> ThermalFeatureSchemaV2:
        """入力の順序付き契約。"""
        return self._artifact.feature_schema

    @property
    def target_schema(self) -> ThermalTargetSchema:
        """出力の順序付き契約。"""
        return self._artifact.target_schema

    @property
    def action_schema(self) -> ThermalActionSchema:
        """action の格子・zone の順・出どころ。"""
        return self._artifact.action_schema

    @property
    def confidence_profile(self) -> ConfidenceProfileV2:
        """同じ bytes から作った同梱 Profile v2。"""
        return self._artifact.confidence_profile

    @property
    def artifact_sha256(self) -> str:
        """Registry が検証した bytes 全体の SHA-256。"""
        return self._artifact_sha256

    @property
    def payload_sha256(self) -> str:
        """model payload の canonical SHA-256（Profile v2 の束縛先）。"""
        return self._artifact.manifest.payload_sha256

    @property
    def artifact_verification(self) -> Literal[ArtifactVerification.REGISTRY_VERIFIED]:
        """常に Registry 検証済み。"""
        return ArtifactVerification.REGISTRY_VERIFIED

    def predict(self, observed: ObservedThermalInput) -> ThermalPrediction:
        """anchor 推論。計画 action の列は ``hold_effective`` で作る（0084 §2.1）。

        呼び出し側が計画 action の列を渡す引数は持たない。
        """
        return self._prediction(observed, hold_effective(observed, self.action_schema))

    def predict_trajectory(
        self, observed: ObservedThermalInput, trajectory: ActionTrajectory
    ) -> ThermalPrediction:
        """候補の action 列に対する予測。格子が action schema と違えば予測しない。"""
        return self._prediction(observed, trajectory)

    def _prediction(
        self, observed: ObservedThermalInput, trajectory: ActionTrajectory
    ) -> ThermalPrediction:
        targets = _predict_targets(self._artifact, observed, trajectory)
        return ThermalPrediction(
            model_id=self.manifest.model_id,
            model_version=self.manifest.model_version,
            artifact_sha256=self._artifact_sha256,
            artifact_verification=ArtifactVerification.REGISTRY_VERIFIED,
            capability=InferenceCapability.COUNTERFACTUAL_ACTION,
            input_action_ts_ms=observed.action_ts_ms,
            targets=targets,
        )


def _load_checked(
    verified: VerifiedArtifact,
    *,
    metric_catalog: MetricCatalog,
    calibration: RuntimeCalibration,
) -> tuple[CounterfactualThermalModelArtifact, str]:
    # L1: Registry の検証経路が発行した VerifiedArtifact と attestation
    if type(verified) is not VerifiedArtifact:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.VERIFIED_ARTIFACT, "#104 の VerifiedArtifact だけから型を作る"
        )
    attestation = verified.attestation
    payload = verified.payload
    if attestation.kind is not ArtifactKind.THERMAL_MODEL:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.VERIFIED_ARTIFACT,
            f"thermal model 以外の artifact から型を作らない（kind={attestation.kind.value}）",
        )
    if attestation.capability is not ArtifactCapability.COUNTERFACTUAL_ACTION:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.VERIFIED_ARTIFACT,
            "反実仮想を申告していない artifact から型を作らない"
            f"（attested capability={attestation.capability.value}）",
        )
    if not isinstance(payload, bytes):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.VERIFIED_ARTIFACT, "VerifiedArtifact の payload は bytes に限る"
        )
    # L2: 大きさと構造。JSON の入れ子と token 数は Registry が展開前に検査済み（_validate_format）
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.SIZE_AND_STRUCTURE, "artifact が安全な size 上限を超えている"
        )
    if is_pickle_payload(payload):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.SIZE_AND_STRUCTURE, "pickle 形式の artifact は読み込まない"
        )
    digest = hashlib.sha256(payload).hexdigest()
    if digest != attestation.artifact_sha256:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.VERIFIED_ARTIFACT, "bytes の SHA-256 が attestation と一致しない"
        )
    # L3: canonical 直列化と完全一致（schema を読む前に、JSON の値として確かめる）
    try:
        decoded = json.loads(payload, parse_constant=_reject_nonfinite)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.CANONICAL_BYTES, "artifact が JSON として読めない"
        ) from exc
    if not isinstance(decoded, dict):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.CANONICAL_BYTES, "artifact は JSON object に限る"
        )
    if payload != _canonical_value_bytes(decoded):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.CANONICAL_BYTES, "artifact は canonical JSON bytes に限る"
        )
    # L4: schema_name / schema_version が v2。v1 は反実仮想の型にならない
    header = (decoded.get("schema_name"), decoded.get("schema_version"))
    if header != ("coldaisle.thermal_model", COUNTERFACTUAL_ARTIFACT_SCHEMA_VERSION):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.SCHEMA_VERSION,
            f"反実仮想の型は coldaisle.thermal_model v2 だけから作る（{header}）",
        )
    # L11 の Literal 外の値は schema の読み込みより前に見分ける（理由を L4 と混ぜない）
    rules = (_section_value(decoded, "manifest"), _section_value(decoded, "confidence_profile"))
    if rules != (ANCHOR_ACTION_RULE, ANCHOR_ACTION_RULE):
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.ANCHOR_ACTION_RULE,
            f"manifest と Profile の anchor_action_rule が hold_effective で一致しない: {rules}",
        )
    try:
        artifact = CounterfactualThermalModelArtifact.model_validate_json(payload)
    except ValidationError as exc:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.SCHEMA_VERSION, f"artifact が v2 の schema に合わない: {exc}"
        ) from exc
    if canonical_json_bytes(artifact) != payload:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.CANONICAL_BYTES, "artifact の bytes が schema の canonical 直列化と違う"
        )
    # L5: manifest から導いた Registry metadata の全欄が attestation / metadata と一致
    derived = _metadata_from_artifact(artifact, payload)
    mismatches = registry_metadata_mismatches(derived, verified.metadata)
    attested = (
        ("kind", attestation.kind.value, derived.kind),
        ("capability", attestation.capability.value, derived.capability.value),
        ("model_id", attestation.model_id, derived.model_id),
        ("version", attestation.version, derived.version),
        ("sha256", attestation.artifact_sha256, derived.sha256),
        (
            "feature_schema_version",
            attestation.feature_schema_version,
            derived.feature_schema_version,
        ),
        ("target_schema_version", attestation.target_schema_version, derived.target_schema_version),
        (
            "authority_compatibility",
            _comparable(attestation.authority_compatibility),
            _comparable(derived.authority_compatibility),
        ),
    )
    mismatches += [f"attestation.{name}" for name, left, right in attested if left != right]
    if mismatches:
        raise CounterfactualArtifactRejectedError(
            ArtifactCheck.REGISTRY_METADATA,
            f"manifest から導いた Registry metadata が一致しない: {mismatches}",
        )
    # L6〜L12
    check_artifact_contents(
        artifact,
        metric_catalog=metric_catalog,
        calibration=calibration,
        check_calibration=True,
    )
    return artifact, digest


def _section_value(document: dict[str, object], section: str) -> object:
    value = document.get(section)
    return value.get("anchor_action_rule") if isinstance(value, dict) else None


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"非有限値は JSON artifact に使えない: {value}")


def _canonical_value_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
