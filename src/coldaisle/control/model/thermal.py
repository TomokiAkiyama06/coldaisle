"""Versioned read-only Learned Thermal Model contract (#84).

Dataset v1 does not contain the future fan-action trajectory between its anchor action and
labels.  This module therefore exposes an observational Replay model only.  It cannot return
Fan Demand or claim counterfactual MPC capability.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coldaisle.control.model.dataset import (
    DATASET_SCHEMA_VERSION,
    DatasetExample,
    DatasetSpec,
)
from coldaisle.control.schema import AuthorityStage, PerZone, Zone
from coldaisle.store.models import Quality

MODEL_ARTIFACT_SCHEMA_VERSION: Literal[1] = 1
FEATURE_SCHEMA_VERSION: Literal["thermal-features-v1"] = "thermal-features-v1"
TARGET_SCHEMA_VERSION: Literal["thermal-targets-v1"] = "thermal-targets-v1"
INFERENCE_SCHEMA_VERSION: Literal[1] = 1
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_WINDOW_FRAMES = 512
MAX_FEATURE_METRICS = 64
MAX_FEATURE_COLUMNS = 512
MAX_TARGET_HORIZONS = 32
MAX_TARGET_METRICS = 32
MAX_TARGET_OUTPUTS = 128
MAX_SOURCE_RUNS = 1_024
MAX_REGISTRY_HYPERPARAMETERS = 16
MAX_REPLAY_EXAMPLES = 100_000
MAX_REPLAY_FEATURE_CELLS = 1_000_000
MAX_REPLAY_OUTPUT_VALUES = 1_000_000
MAX_REPLAY_WORK_UNITS = 50_000_000
MAX_METRIC_NAME_LENGTH = 120
MAX_SCHEMA_COLUMN_LENGTH = 256
MAX_INFERENCE_TEXT_BYTES = 1 * 1024 * 1024
MAX_MODEL_TRAINING_EXAMPLES = 100_000
MAX_METADATA_TEXT_LENGTH = 500
MAX_TIME_MS = (1 << 63) - 1
MIN_INT64 = -(1 << 63)

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
PositiveFiniteFloat = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
TimestampMs = Annotated[int, Field(ge=0, le=MAX_TIME_MS)]
PositiveDurationMs = Annotated[int, Field(gt=0, le=MAX_TIME_MS)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DatasetArtifactId = Annotated[str, Field(pattern=r"^dataset-[0-9a-f]{32}$")]
RunAlias = Annotated[str, Field(pattern=r"^run-[0-9a-f]{32}$")]
ModelId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=120)]
SemanticVersion = Annotated[
    str,
    Field(
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
        ),
        max_length=80,
    ),
]
ThermalMetricName = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,3}$",
        max_length=MAX_METRIC_NAME_LENGTH,
    ),
]

_ZONE_ORDER: tuple[Zone, ...] = (Zone.FRONT, Zone.REAR, Zone.TOP)
_MODEL_FAMILY: Literal["ridge_linear_v1"] = "ridge_linear_v1"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class InferenceCapability(StrEnum):
    """Scientific claim supported by an artifact, not its deployment lifecycle state."""

    OBSERVATIONAL_REPLAY = "observational_replay"
    COUNTERFACTUAL_ACTION = "counterfactual_action"
    """Prediction under a candidate future fan-action trajectory (#86).

    No Dataset v1 artifact can declare it: ``ThermalModelManifest.capability`` and
    ``ThermalPrediction.capability`` are pinned to ``OBSERVATIONAL_REPLAY`` because v1 has no
    post-anchor action trajectory (decision record 0048 §2.1).  The member exists so that #86
    can name the capability it requires and refuse every current artifact deterministically.
    """


class ArtifactVerification(StrEnum):
    """Whether inference bytes crossed the #104 checksum-verified load boundary."""

    REGISTRY_VERIFIED = "registry_verified"
    OFFLINE_UNVERIFIED = "offline_unverified"


class ThermalFeatureSchema(_Frozen):
    """Ordered v1 feature layout shared by training and inference."""

    schema_version: Literal["thermal-features-v1"] = FEATURE_SCHEMA_VERSION
    dataset_schema_version: Literal[1] = DATASET_SCHEMA_VERSION
    window_ms: PositiveDurationMs
    sample_period_ms: PositiveDurationMs
    stale_after_ms: PositiveDurationMs
    metrics: tuple[ThermalMetricName, ...] = Field(min_length=1, max_length=MAX_FEATURE_METRICS)
    columns: tuple[str, ...] = Field(min_length=1, max_length=MAX_FEATURE_COLUMNS)

    @classmethod
    def from_dataset_spec(cls, spec: DatasetSpec) -> ThermalFeatureSchema:
        """Derive the exact layout without introducing window or metric defaults."""
        _validate_metric_name_lengths(spec.feature_metrics)
        _validate_feature_shape_values(
            window_ms=spec.window_ms,
            sample_period_ms=spec.sample_period_ms,
            metric_count=len(spec.feature_metrics),
        )
        return cls(
            window_ms=spec.window_ms,
            sample_period_ms=spec.sample_period_ms,
            stale_after_ms=spec.stale_after_ms,
            metrics=spec.feature_metrics,
            columns=_feature_columns(
                window_ms=spec.window_ms,
                sample_period_ms=spec.sample_period_ms,
                metrics=spec.feature_metrics,
            ),
        )

    @model_validator(mode="after")
    def _layout_is_canonical(self) -> Self:
        _validate_metric_name_lengths(self.metrics)
        if any(len(column) > MAX_SCHEMA_COLUMN_LENGTH for column in self.columns):
            raise ValueError("feature column名がartifact安全上限を超えている")
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("feature metricを重複させない")
        _frame_count, column_count = _validate_feature_shape_values(
            window_ms=self.window_ms,
            sample_period_ms=self.sample_period_ms,
            metric_count=len(self.metrics),
        )
        if len(self.columns) != column_count:
            raise ValueError("feature column数がwindow / metric layoutと一致しない")
        expected = _feature_columns(
            window_ms=self.window_ms,
            sample_period_ms=self.sample_period_ms,
            metrics=self.metrics,
        )
        if self.columns != expected:
            raise ValueError("feature columnの順序または内容がv1 canonical layoutと一致しない")
        return self


class ThermalTargetSchema(_Frozen):
    """Ordered multi-horizon / multi-output target layout."""

    schema_version: Literal["thermal-targets-v1"] = TARGET_SCHEMA_VERSION
    dataset_schema_version: Literal[1] = DATASET_SCHEMA_VERSION
    horizons_ms: tuple[PositiveDurationMs, ...] = Field(
        min_length=1, max_length=MAX_TARGET_HORIZONS
    )
    metrics: tuple[ThermalMetricName, ...] = Field(min_length=1, max_length=MAX_TARGET_METRICS)
    outputs: tuple[str, ...] = Field(min_length=1, max_length=MAX_TARGET_OUTPUTS)

    @classmethod
    def from_dataset_spec(cls, spec: DatasetSpec) -> ThermalTargetSchema:
        """Derive target order from the versioned Dataset spec."""
        _validate_metric_name_lengths(spec.target_metrics)
        if any(not _is_bounded_time_value(horizon, positive=True) for horizon in spec.horizons_ms):
            raise ValueError("target horizonがartifact安全上限を超えている")
        _validate_target_shape_values(
            horizon_count=len(spec.horizons_ms),
            metric_count=len(spec.target_metrics),
        )
        return cls(
            horizons_ms=spec.horizons_ms,
            metrics=spec.target_metrics,
            outputs=_target_outputs(spec.horizons_ms, spec.target_metrics),
        )

    @model_validator(mode="after")
    def _layout_is_canonical(self) -> Self:
        _validate_metric_name_lengths(self.metrics)
        if any(len(output) > MAX_SCHEMA_COLUMN_LENGTH for output in self.outputs):
            raise ValueError("target output名がartifact安全上限を超えている")
        if tuple(sorted(set(self.horizons_ms))) != self.horizons_ms:
            raise ValueError("target horizonは正の値を重複なし昇順にする")
        if any(horizon <= 0 for horizon in self.horizons_ms):
            raise ValueError("target horizonは正にする")
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("target metricを重複させない")
        output_count = _validate_target_shape_values(
            horizon_count=len(self.horizons_ms),
            metric_count=len(self.metrics),
        )
        if len(self.outputs) != output_count:
            raise ValueError("target output数がhorizon / metric layoutと一致しない")
        if self.outputs != _target_outputs(self.horizons_ms, self.metrics):
            raise ValueError("target outputの順序または内容がv1 canonical layoutと一致しない")
        return self


class ObservedWindowFrame(_Frozen):
    """One historical frame with explicit unusable-value masks."""

    ts_ms: TimestampMs
    values: dict[ThermalMetricName, FiniteFloat | None] = Field(
        min_length=1, max_length=MAX_FEATURE_METRICS
    )
    source_ts_ms: dict[ThermalMetricName, TimestampMs | None] = Field(
        min_length=1, max_length=MAX_FEATURE_METRICS
    )
    missing_mask: dict[ThermalMetricName, bool] = Field(
        min_length=1, max_length=MAX_FEATURE_METRICS
    )
    stale_mask: dict[ThermalMetricName, bool] = Field(min_length=1, max_length=MAX_FEATURE_METRICS)
    suspect_mask: dict[ThermalMetricName, bool] = Field(
        min_length=1, max_length=MAX_FEATURE_METRICS
    )

    @model_validator(mode="after")
    def _cell_states_are_unambiguous(self) -> Self:
        keys = set(self.values)
        if any(
            set(mapping) != keys
            for mapping in (
                self.source_ts_ms,
                self.missing_mask,
                self.stale_mask,
                self.suspect_mask,
            )
        ):
            raise ValueError("window frameのmetric集合が一致しない")
        for metric, value in self.values.items():
            missing = self.missing_mask[metric]
            suspect = self.suspect_mask[metric]
            source_ts = self.source_ts_ms[metric]
            if missing != (value is None):
                raise ValueError("missing maskとvalue=Noneを一致させる")
            if missing and suspect:
                raise ValueError("missingとsuspectは同時に立てない")
            if source_ts is None:
                if not missing or self.stale_mask[metric] or suspect:
                    raise ValueError("未観測cellはmissingだけを立てる")
            elif source_ts > self.ts_ms:
                raise ValueError("window frameに未来のsource時刻を入れない")
        return self


class ObservedFanAction(_Frozen):
    """Fan action that actually affected the observed thermal response."""

    effective_demand: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class ObservedThermalInput(_Frozen):
    """Historical observation accepted by v1; not a counterfactual candidate action plan."""

    schema_version: Literal[1] = INFERENCE_SCHEMA_VERSION
    action_ts_ms: TimestampMs
    window: tuple[ObservedWindowFrame, ...] = Field(min_length=2, max_length=MAX_WINDOW_FRAMES)
    action: PerZone[ObservedFanAction]

    @classmethod
    def from_example(cls, example: DatasetExample) -> ObservedThermalInput:
        """Drop labels and training-only context from a validated Dataset example."""
        _validate_example_input_container_sizes(example)
        return cls(
            action_ts_ms=example.action_ts_ms,
            window=tuple(
                ObservedWindowFrame(
                    ts_ms=frame.ts_ms,
                    values=frame.values,
                    source_ts_ms=frame.source_ts_ms,
                    missing_mask=frame.missing_mask,
                    stale_mask=frame.stale_mask,
                    # suspect maskは「値はあるが疑わしい」を表す。値の無いsuspect
                    # （inf等。決定記録 0031 §2.1）はdataset側でmissing_maskが立つため、
                    # ここではmissingとして扱い、missingとsuspectを同時に立てない
                    suspect_mask={
                        metric: frame.quality[metric] is Quality.SUSPECT
                        and frame.values[metric] is not None
                        for metric in frame.values
                    },
                )
                for frame in example.window
            ),
            action=PerZone(
                front=ObservedFanAction(effective_demand=example.action.front.effective_demand),
                rear=ObservedFanAction(effective_demand=example.action.rear.effective_demand),
                top=ObservedFanAction(effective_demand=example.action.top.effective_demand),
            ),
        )

    @model_validator(mode="after")
    def _window_ends_at_action(self) -> Self:
        timestamps = tuple(frame.ts_ms for frame in self.window)
        if tuple(sorted(set(timestamps))) != timestamps:
            raise ValueError("observed windowの時刻は重複なし昇順にする")
        if timestamps[-1] != self.action_ts_ms:
            raise ValueError("observed windowはaction時刻で終わらなければならない")
        return self


class PredictedTarget(_Frozen):
    """Predicted values for one configured horizon."""

    horizon_ms: PositiveDurationMs
    expected_ts_ms: TimestampMs
    values: dict[ThermalMetricName, FiniteFloat] = Field(
        min_length=1, max_length=MAX_TARGET_METRICS
    )


class ThermalPrediction(_Frozen):
    """Read-only prediction result; it deliberately has no Demand or PWM output."""

    schema_version: Literal[1] = INFERENCE_SCHEMA_VERSION
    model_id: ModelId
    model_version: SemanticVersion
    artifact_sha256: Sha256
    artifact_verification: ArtifactVerification
    capability: Literal[InferenceCapability.OBSERVATIONAL_REPLAY]
    input_action_ts_ms: TimestampMs
    targets: tuple[PredictedTarget, ...] = Field(min_length=1, max_length=MAX_TARGET_HORIZONS)
    uncertainty: None = None

    @model_validator(mode="after")
    def _targets_follow_input(self) -> Self:
        horizons = tuple(target.horizon_ms for target in self.targets)
        if tuple(sorted(set(horizons))) != horizons:
            raise ValueError("prediction horizonは重複なし昇順にする")
        if any(
            target.expected_ts_ms != self.input_action_ts_ms + target.horizon_ms
            for target in self.targets
        ):
            raise ValueError("prediction時刻はinput action + horizonにする")
        return self


class TrainingSourceProvenance(_Frozen):
    """Public aliases and content hash for one source run."""

    run_id: RunAlias
    source_sha256: Sha256


class RidgeHyperparameters(_Frozen):
    """Required training choices; no production default is provided."""

    ridge_lambda: PositiveFiniteFloat


class ThermalModelManifest(_Frozen):
    """Registry-compatible identity, provenance, compatibility, and payload integrity."""

    model_id: ModelId
    model_version: SemanticVersion
    created_at: str = Field(min_length=1, max_length=64)
    training_dataset_version: DatasetArtifactId
    training_dataset_schema_version: Literal[1] = DATASET_SCHEMA_VERSION
    training_dataset_manifest_sha256: Sha256
    training_examples_sha256: Sha256
    source_runs: tuple[TrainingSourceProvenance, ...] = Field(
        min_length=1, max_length=MAX_SOURCE_RUNS
    )
    training_split_sha256: Sha256
    training_example_count: int = Field(gt=0, le=MAX_MODEL_TRAINING_EXAMPLES)
    feature_schema_version: Literal["thermal-features-v1"] = FEATURE_SCHEMA_VERSION
    target_schema_version: Literal["thermal-targets-v1"] = TARGET_SCHEMA_VERSION
    feature_schema_sha256: Sha256
    target_schema_sha256: Sha256
    model_payload_sha256: Sha256
    code_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    model_family: Literal["ridge_linear_v1"] = _MODEL_FAMILY
    hyperparameters: RidgeHyperparameters
    capability: Literal[InferenceCapability.OBSERVATIONAL_REPLAY]
    authority_compatibility: tuple[AuthorityStage, ...] = Field(min_length=1, max_length=1)

    @field_validator("created_at")
    @classmethod
    def _created_at_is_timezone_aware_rfc3339(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_atはRFC 3339形式にする") from exc
        if parsed.utcoffset() is None:
            raise ValueError("created_atにはtimezoneが必要")
        return value

    @model_validator(mode="after")
    def _is_shadow_only_and_public(self) -> Self:
        if self.authority_compatibility != (AuthorityStage.SHADOW,):
            raise ValueError("Dataset v1 baselineはSHADOW authorityだけに限定する")
        run_ids = tuple(source.run_id for source in self.source_runs)
        if run_ids != tuple(sorted(set(run_ids))):
            raise ValueError("source runは公開aliasの重複なし昇順にする")
        return self


class RidgeOutput(_Frozen):
    """One independently fitted horizon/metric head."""

    horizon_ms: PositiveDurationMs
    metric: ThermalMetricName
    intercept: FiniteFloat
    coefficients: tuple[FiniteFloat, ...] = Field(min_length=1, max_length=MAX_FEATURE_COLUMNS)
    training_samples: int = Field(gt=0)


class RidgeModelPayload(_Frozen):
    """Portable numeric payload; contains no executable Python object."""

    model_family: Literal["ridge_linear_v1"] = _MODEL_FAMILY
    feature_means: tuple[FiniteFloat, ...] = Field(min_length=1, max_length=MAX_FEATURE_COLUMNS)
    feature_scales: tuple[PositiveFiniteFloat, ...] = Field(
        min_length=1, max_length=MAX_FEATURE_COLUMNS
    )
    feature_observed_counts: tuple[int, ...] = Field(min_length=1, max_length=MAX_FEATURE_COLUMNS)
    outputs: tuple[RidgeOutput, ...] = Field(min_length=1, max_length=MAX_TARGET_OUTPUTS)


class ThermalModelArtifact(_Frozen):
    """Complete non-executable JSON model artifact."""

    schema_name: Literal["coldaisle.thermal_model"] = "coldaisle.thermal_model"
    schema_version: Literal[1] = MODEL_ARTIFACT_SCHEMA_VERSION
    manifest: ThermalModelManifest
    feature_schema: ThermalFeatureSchema
    target_schema: ThermalTargetSchema
    payload: RidgeModelPayload

    @model_validator(mode="after")
    def _manifest_and_payload_match(self) -> Self:
        feature_count = len(self.feature_schema.columns)
        if not (
            len(self.payload.feature_means)
            == len(self.payload.feature_scales)
            == len(self.payload.feature_observed_counts)
            == feature_count
        ):
            raise ValueError("normalization vectorとfeature column数が一致しない")
        if any(count < 0 for count in self.payload.feature_observed_counts):
            raise ValueError("feature observed countを負にしない")
        expected_outputs = tuple(
            (horizon, metric)
            for horizon in self.target_schema.horizons_ms
            for metric in self.target_schema.metrics
        )
        actual_outputs = tuple(
            (output.horizon_ms, output.metric) for output in self.payload.outputs
        )
        if actual_outputs != expected_outputs:
            raise ValueError("ridge outputの順序または集合がtarget schemaと一致しない")
        if any(len(output.coefficients) != feature_count for output in self.payload.outputs):
            raise ValueError("ridge coefficient数がfeature column数と一致しない")
        if any(
            count > self.manifest.training_example_count
            for count in self.payload.feature_observed_counts
        ):
            raise ValueError("feature observed countがtrain example数を超えている")
        if any(
            output.training_samples > self.manifest.training_example_count
            for output in self.payload.outputs
        ):
            raise ValueError("output training sample数がtrain example数を超えている")
        if self.manifest.feature_schema_version != self.feature_schema.schema_version:
            raise ValueError("manifestとfeature schema versionが一致しない")
        if self.manifest.target_schema_version != self.target_schema.schema_version:
            raise ValueError("manifestとtarget schema versionが一致しない")
        if self.manifest.feature_schema_sha256 != canonical_sha256(self.feature_schema):
            raise ValueError("feature schema checksumが一致しない")
        if self.manifest.target_schema_sha256 != canonical_sha256(self.target_schema):
            raise ValueError("target schema checksumが一致しない")
        if self.manifest.model_payload_sha256 != canonical_sha256(self.payload):
            raise ValueError("model payload checksumが一致しない")
        if self.manifest.model_family != self.payload.model_family:
            raise ValueError("manifestとpayloadのmodel familyが一致しない")
        return self


class ThermalRegistryMetadata(_Frozen):
    """Values that map one-to-one to #104 ``ArtifactMetadata``."""

    schema_version: Literal[2, 3] = 3
    """#104 ``ArtifactMetadata`` の版。**書き出しは v3、v2 で登録済みの記録も読む。**

    v3 は `ArtifactCapability` に `supervisor_strategy` が増えただけで（#89）、thermal model
    側の欄は何も変わっていない。v2 で登録済みの production artifact を版の違いだけで
    読めなくしないため、両方を受け付ける。版は Registry の封筒の値で artifact が決める値では
    ないので、artifact との照合（`_registry_contract`）にも入れない。
    """
    kind: Literal["thermal_model"] = "thermal_model"
    artifact_format: Literal["json"] = "json"
    capability: InferenceCapability
    """登録時に #104 へ申告する能力。**manifest の capability をそのまま写す。**

    #86 はこの値（Registry の metadata と attestation 側）だけを見て内部モデルの可否を決める。
    推論器が自分で名乗った値では判断しない（決定記録 0052 §2.1）。
    """
    model_id: ModelId
    version: SemanticVersion
    created_at: str
    training_dataset_version: DatasetArtifactId
    source_runs: tuple[RunAlias, ...] = Field(min_length=1, max_length=MAX_SOURCE_RUNS)
    feature_schema_version: Literal["thermal-features-v1"] = FEATURE_SCHEMA_VERSION
    target_schema_version: Literal["thermal-targets-v1"] = TARGET_SCHEMA_VERSION
    code_commit: str | None
    sha256: Sha256
    model_family: Literal["ridge_linear_v1"] = _MODEL_FAMILY
    hyperparameters: dict[str, str | int | float | bool | None] = Field(
        max_length=MAX_REGISTRY_HYPERPARAMETERS
    )
    offline_evaluation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    shadow_evaluation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    authority_compatibility: tuple[AuthorityStage, ...]

    @field_validator("created_at")
    @classmethod
    def _created_at_is_timezone_aware_rfc3339(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_atはRFC 3339形式にする") from exc
        if parsed.utcoffset() is None:
            raise ValueError("created_atにはtimezoneが必要")
        return value

    @model_validator(mode="after")
    def _is_unique_and_shadow_only(self) -> Self:
        if self.source_runs != tuple(sorted(set(self.source_runs))):
            raise ValueError("Registry source runは重複なし昇順にする")
        if self.authority_compatibility != (AuthorityStage.SHADOW,):
            raise ValueError("Dataset v1 Registry metadataはSHADOWだけに限定する")
        return self


class ThermalArtifactExpectation(_Frozen):
    """Verified #104 metadata and the actual authority requested by its caller."""

    registry_metadata: ThermalRegistryMetadata
    authority_stage: AuthorityStage


class ThermalModel(Protocol):
    """Read-only interface consumed by Replay, #85, and future #86 integration."""

    @property
    def manifest(self) -> ThermalModelManifest:
        """Return immutable model identity and provenance."""
        ...

    @property
    def artifact_verification(self) -> ArtifactVerification:
        """Expose whether #104 verified the loaded bytes."""
        ...

    @property
    def feature_schema(self) -> ThermalFeatureSchema:
        """Return the exact ordered input contract."""
        ...

    @property
    def target_schema(self) -> ThermalTargetSchema:
        """Return the exact ordered output contract."""
        ...

    def predict(self, observed: ObservedThermalInput) -> ThermalPrediction:
        """Predict future observations without changing control or hardware state."""
        ...


class RegistryThermalModel(ThermalModel, Protocol):
    """Deployment-facing model that crossed the #104 VerifiedArtifact boundary."""

    @property
    def artifact_verification(self) -> Literal[ArtifactVerification.REGISTRY_VERIFIED]:
        """Always identify Registry-verified model bytes."""
        ...


class RegistryVerifiedArtifact(Protocol):
    """Static shape for #104; runtime loading still requires its exact nominal type."""

    @property
    def metadata(self) -> BaseModel:
        """Return checksum-verified #104 metadata."""
        ...

    @property
    def payload(self) -> bytes:
        """Return the exact bytes verified by #104."""
        ...


class _RidgeThermalModelBase:
    """Shared deterministic implementation; concrete types identify provenance."""

    def __init__(
        self,
        artifact: ThermalModelArtifact,
        *,
        artifact_verification: ArtifactVerification,
    ) -> None:
        self._artifact = _validated_artifact(artifact)
        artifact_bytes = canonical_json_bytes(self._artifact)
        if len(artifact_bytes) > MAX_ARTIFACT_BYTES:
            raise ValueError("thermal model artifactが安全なsize上限を超えている")
        self._artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        self._artifact_verification = artifact_verification

    @property
    def manifest(self) -> ThermalModelManifest:
        """Return immutable model identity and provenance."""
        return self._artifact.manifest

    @property
    def artifact_verification(self) -> ArtifactVerification:
        """Return the explicit Registry/offline provenance state."""
        return self._artifact_verification

    @property
    def feature_schema(self) -> ThermalFeatureSchema:
        """Return the exact ordered input contract."""
        return self._artifact.feature_schema

    @property
    def target_schema(self) -> ThermalTargetSchema:
        """Return the exact ordered output contract."""
        return self._artifact.target_schema

    def predict(self, observed: ObservedThermalInput) -> ThermalPrediction:
        """Return deterministic observational forecasts for all configured heads."""
        observed = _validated_observed_input(observed)
        raw = _raw_feature_values_validated(observed, self.feature_schema)
        normalized = tuple(
            0.0 if value is None else (value - mean) / scale
            for value, mean, scale in zip(
                raw,
                self._artifact.payload.feature_means,
                self._artifact.payload.feature_scales,
                strict=True,
            )
        )
        by_horizon: dict[int, dict[str, float]] = {
            horizon: {} for horizon in self.target_schema.horizons_ms
        }
        for output in self._artifact.payload.outputs:
            try:
                value = output.intercept + math.fsum(
                    coefficient * feature
                    for coefficient, feature in zip(output.coefficients, normalized, strict=True)
                )
            except OverflowError as exc:
                raise ValueError("thermal predictionが有限範囲を超えた") from exc
            if not math.isfinite(value):
                raise ValueError("thermal predictionが非有限になった")
            by_horizon[output.horizon_ms][output.metric] = value
        targets = tuple(
            PredictedTarget(
                horizon_ms=horizon,
                expected_ts_ms=observed.action_ts_ms + horizon,
                values=by_horizon[horizon],
            )
            for horizon in self.target_schema.horizons_ms
        )
        return ThermalPrediction(
            model_id=self.manifest.model_id,
            model_version=self.manifest.model_version,
            artifact_sha256=self._artifact_sha256,
            artifact_verification=self.artifact_verification,
            capability=self.manifest.capability,
            input_action_ts_ms=observed.action_ts_ms,
            targets=targets,
        )


class OfflineRidgeThermalModel(_RidgeThermalModelBase):
    """Explicitly unverified model for offline evaluation and deterministic tests only."""

    def __init__(self, artifact: ThermalModelArtifact) -> None:
        super().__init__(
            artifact,
            artifact_verification=ArtifactVerification.OFFLINE_UNVERIFIED,
        )

    @classmethod
    def from_artifact(cls, artifact: ThermalModelArtifact) -> OfflineRidgeThermalModel:
        """Build a model that is visibly outside the #104 Registry trust boundary."""
        return cls(artifact)

    @property
    def artifact_verification(self) -> Literal[ArtifactVerification.OFFLINE_UNVERIFIED]:
        """Always mark offline artifacts as unverified."""
        return ArtifactVerification.OFFLINE_UNVERIFIED


_REGISTRY_LOAD_TOKEN = object()


class RidgeThermalModel(_RidgeThermalModelBase):
    """Registry-verified deterministic inference implementation for deployment consumers."""

    def __init__(
        self,
        artifact: ThermalModelArtifact,
        *,
        _load_token: object | None = None,
    ) -> None:
        if _load_token is not _REGISTRY_LOAD_TOKEN:
            raise TypeError("RidgeThermalModelは#104 VerifiedArtifactからだけ構築できる")
        super().__init__(
            artifact,
            artifact_verification=ArtifactVerification.REGISTRY_VERIFIED,
        )

    @classmethod
    def from_verified_artifact(
        cls,
        verified: RegistryVerifiedArtifact,
        *,
        authority_stage: AuthorityStage,
    ) -> RidgeThermalModel:
        """Load the exact payload and metadata returned by #104 ``VerifiedArtifact``.

        ``authority_stage`` is the **effective** stage the caller will run at
        (#92 / decision record 0057 §2.2), not the configured ceiling.
        """
        try:
            registry_module = importlib.import_module("coldaisle.control.model_registry")
            verified_artifact_type = registry_module.VerifiedArtifact
        except (ImportError, AttributeError) as exc:
            raise RuntimeError("#104 Model Registryが利用できない") from exc
        if type(verified) is not verified_artifact_type:
            raise TypeError("#104 nominal VerifiedArtifactだけをruntime loadできる")
        if not isinstance(verified.payload, bytes):
            raise TypeError("#104 VerifiedArtifact payloadはbytesでなければならない")
        if len(verified.payload) > MAX_ARTIFACT_BYTES:
            raise ValueError("thermal model artifactが安全なsize上限を超えている")
        _validate_external_registry_metadata_sizes(verified.metadata)
        metadata = ThermalRegistryMetadata.model_validate_json(verified.metadata.model_dump_json())
        expected = ThermalArtifactExpectation(
            registry_metadata=metadata,
            authority_stage=authority_stage,
        )
        return cls._from_verified_bytes(verified.payload, expected=expected)

    @classmethod
    def _from_verified_bytes(
        cls,
        artifact_bytes: bytes,
        *,
        expected: ThermalArtifactExpectation,
    ) -> RidgeThermalModel:
        if len(artifact_bytes) > MAX_ARTIFACT_BYTES:
            raise ValueError("thermal model artifactが安全なsize上限を超えている")
        expected = _validated_expectation(expected)
        metadata = expected.registry_metadata
        actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        if actual_sha256 != metadata.sha256:
            raise ValueError("thermal model artifact checksumがRegistry metadataと一致しない")
        if is_pickle_payload(artifact_bytes):
            raise ValueError("pickle形式のthermal model artifactは読み込まない")
        artifact = ThermalModelArtifact.model_validate_json(artifact_bytes)
        if artifact_bytes != canonical_artifact_bytes(artifact):
            raise ValueError("thermal model artifactはcanonical JSON bytesに限定する")
        manifest = artifact.manifest
        actual_metadata = _registry_metadata_from_artifact(artifact, artifact_bytes)
        if _registry_contract(actual_metadata) != _registry_contract(metadata):
            raise ValueError("thermal model artifactのidentity/provenanceがRegistryと一致しない")
        if expected.authority_stage not in manifest.authority_compatibility:
            raise ValueError("thermal model artifactは要求authority stageと互換でない")
        model = cls(artifact, _load_token=_REGISTRY_LOAD_TOKEN)
        if model._artifact_sha256 != actual_sha256:
            raise ValueError("thermal model artifact checksumがcanonical bytesと一致しない")
        return model

    @property
    def artifact_verification(self) -> Literal[ArtifactVerification.REGISTRY_VERIFIED]:
        """Always mark this deployment-facing type as Registry verified."""
        return ArtifactVerification.REGISTRY_VERIFIED


def replay_predictions(
    model: ThermalModel,
    examples: tuple[DatasetExample, ...],
) -> tuple[ThermalPrediction, ...]:
    """Replay saved observations through a pinned read-only model in dataset order."""
    example_count = len(examples)
    feature_count = len(model.feature_schema.columns)
    output_count = len(model.target_schema.outputs)
    if example_count > MAX_REPLAY_EXAMPLES:
        raise ValueError("Replay example数が安全上限を超えている")
    if example_count * feature_count > MAX_REPLAY_FEATURE_CELLS:
        raise ValueError("Replay feature cell総数が安全上限を超えている")
    if example_count * output_count > MAX_REPLAY_OUTPUT_VALUES:
        raise ValueError("Replay output value総数が安全上限を超えている")
    if example_count * feature_count * output_count > MAX_REPLAY_WORK_UNITS:
        raise ValueError("Replay推論演算量が安全上限を超えている")
    for example in examples:
        _validate_replay_example_shape(example, model.feature_schema)
    return tuple(model.predict(ObservedThermalInput.from_example(example)) for example in examples)


def _validate_replay_example_shape(
    example: DatasetExample,
    schema: ThermalFeatureSchema,
) -> None:
    _validate_example_input_container_sizes(example)
    expected_frames = schema.window_ms // schema.sample_period_ms + 1
    if len(example.window) != expected_frames:
        raise ValueError("Replay exampleのwindow frame数がmodel schemaと一致しない")
    expected_metrics = set(schema.metrics)
    for frame in example.window:
        mappings = (
            frame.values,
            frame.source_ts_ms,
            frame.quality,
            frame.missing_mask,
            frame.stale_mask,
        )
        if any(len(mapping) != len(schema.metrics) for mapping in mappings):
            raise ValueError("Replay exampleのfeature metric数がmodel schemaと一致しない")
        if any(set(mapping) != expected_metrics for mapping in mappings):
            raise ValueError("Replay exampleのfeature metric集合がmodel schemaと一致しない")


def canonical_json_bytes(model: BaseModel) -> bytes:
    """Return portable canonical JSON bytes for schema/payload hashing."""
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def canonical_sha256(model: BaseModel) -> str:
    """Hash one canonical schema or payload value."""
    return hashlib.sha256(canonical_json_bytes(model)).hexdigest()


def canonical_artifact_bytes(artifact: ThermalModelArtifact) -> bytes:
    """Serialize the entire executable-free artifact for #104 storage."""
    return canonical_json_bytes(_validated_artifact(artifact))


def registry_metadata(
    artifact: ThermalModelArtifact, artifact_bytes: bytes
) -> ThermalRegistryMetadata:
    """Map exact artifact bytes to #104 candidate registration metadata."""
    _validate_artifact_container_sizes(artifact)
    if len(artifact_bytes) > MAX_ARTIFACT_BYTES:
        raise ValueError("thermal model artifactが安全なsize上限を超えている")
    parsed = ThermalModelArtifact.model_validate_json(artifact_bytes)
    if parsed != artifact:
        raise ValueError("Registryへ渡すbytesが指定artifactと一致しない")
    if artifact_bytes != canonical_artifact_bytes(parsed):
        raise ValueError("Registryへ渡すartifactはcanonical JSON bytesに限定する")
    return _registry_metadata_from_artifact(parsed, artifact_bytes)


def registry_metadata_json_bytes(metadata: ThermalRegistryMetadata) -> bytes:
    """Return the strict-JSON bridge consumed by #104 ``ArtifactMetadata``."""
    return canonical_json_bytes(_validated_registry_metadata(metadata))


def expectation_from_registry_metadata(
    metadata: ThermalRegistryMetadata,
    *,
    authority_stage: AuthorityStage,
) -> ThermalArtifactExpectation:
    """Build the strict runtime expectation passed beside VerifiedArtifact bytes."""
    return ThermalArtifactExpectation(
        registry_metadata=metadata,
        authority_stage=authority_stage,
    )


def raw_feature_values(
    observed: ObservedThermalInput,
    schema: ThermalFeatureSchema,
) -> tuple[float | None, ...]:
    """Validate one observation and flatten it without using labels or future state."""
    observed = _validated_observed_input(observed)
    _validate_feature_schema_container_sizes(schema)
    schema = ThermalFeatureSchema.model_validate(schema.model_dump(mode="python"))
    return _raw_feature_values_validated(observed, schema)


def _raw_feature_values_validated(
    observed: ObservedThermalInput,
    schema: ThermalFeatureSchema,
) -> tuple[float | None, ...]:
    """Flatten already revalidated and bounded runtime contracts."""
    expected_times = tuple(
        range(
            observed.action_ts_ms - schema.window_ms,
            observed.action_ts_ms + 1,
            schema.sample_period_ms,
        )
    )
    if tuple(frame.ts_ms for frame in observed.window) != expected_times:
        raise ValueError("observed windowの幅・periodがfeature schemaと一致しない")
    expected_metrics = set(schema.metrics)
    flattened: list[float | None] = []
    for frame in observed.window:
        mappings = (
            frame.values,
            frame.source_ts_ms,
            frame.missing_mask,
            frame.stale_mask,
            frame.suspect_mask,
        )
        if any(set(mapping) != expected_metrics for mapping in mappings):
            raise ValueError("observed windowのmetric集合がfeature schemaと一致しない")
        for metric in schema.metrics:
            source_ts = frame.source_ts_ms[metric]
            if (
                source_ts is not None
                and frame.ts_ms - source_ts >= schema.stale_after_ms
                and not frame.stale_mask[metric]
            ):
                raise ValueError("observed windowのstale maskがfeature schemaと一致しない")
            unavailable = (
                frame.missing_mask[metric] or frame.stale_mask[metric] or frame.suspect_mask[metric]
            )
            flattened.extend(
                (
                    None if unavailable else frame.values[metric],
                    float(frame.missing_mask[metric]),
                    float(frame.stale_mask[metric]),
                    float(frame.suspect_mask[metric]),
                )
            )
    for metric in schema.metrics:
        source_times = tuple(
            source_ts
            for frame in observed.window
            if (source_ts := frame.source_ts_ms[metric]) is not None
        )
        if tuple(sorted(source_times)) != source_times:
            raise ValueError("observed windowのsource時刻がmetric内で逆行している")
    for zone in _ZONE_ORDER:
        flattened.append(observed.action.get(zone).effective_demand)
    if len(flattened) != len(schema.columns):
        raise RuntimeError("internal feature layout mismatch")
    return tuple(flattened)


def _validate_feature_schema_container_sizes(schema: ThermalFeatureSchema) -> None:
    if len(schema.metrics) > MAX_FEATURE_METRICS:
        raise ValueError("feature metric数がartifact安全上限を超えている")
    if len(schema.columns) > MAX_FEATURE_COLUMNS:
        raise ValueError("feature column数がartifact安全上限を超えている")
    _validate_metric_name_lengths(schema.metrics)
    if any(len(column) > MAX_SCHEMA_COLUMN_LENGTH for column in schema.columns):
        raise ValueError("feature column名がartifact安全上限を超えている")


def _validate_example_input_container_sizes(example: DatasetExample) -> None:
    if len(example.window) > MAX_WINDOW_FRAMES:
        raise ValueError("observed window frame数が安全上限を超えている")
    metric_text_bytes = 0
    for frame in example.window:
        mappings = (
            frame.values,
            frame.source_ts_ms,
            frame.missing_mask,
            frame.stale_mask,
            frame.quality,
        )
        if any(len(mapping) > MAX_FEATURE_METRICS for mapping in mappings):
            raise ValueError("observed window metric数が安全上限を超えている")
        if any(len(metric) > MAX_METRIC_NAME_LENGTH for mapping in mappings for metric in mapping):
            raise ValueError("observed window metric名が安全上限を超えている")
        metric_text_bytes += sum(
            len(metric.encode("utf-8")) for mapping in mappings for metric in mapping
        )
        if metric_text_bytes > MAX_INFERENCE_TEXT_BYTES:
            raise ValueError("observed window metric名の総byte数が安全上限を超えている")


def _validated_observed_input(observed: ObservedThermalInput) -> ObservedThermalInput:
    if len(observed.window) > MAX_WINDOW_FRAMES:
        raise ValueError("observed window frame数が安全上限を超えている")
    metric_text_bytes = 0
    for frame in observed.window:
        mappings = (
            frame.values,
            frame.source_ts_ms,
            frame.missing_mask,
            frame.stale_mask,
            frame.suspect_mask,
        )
        if any(len(mapping) > MAX_FEATURE_METRICS for mapping in mappings):
            raise ValueError("observed window metric数が安全上限を超えている")
        if any(len(metric) > MAX_METRIC_NAME_LENGTH for mapping in mappings for metric in mapping):
            raise ValueError("observed window metric名が安全上限を超えている")
        metric_text_bytes += sum(
            len(metric.encode("utf-8")) for mapping in mappings for metric in mapping
        )
        if metric_text_bytes > MAX_INFERENCE_TEXT_BYTES:
            raise ValueError("observed window metric名の総byte数が安全上限を超えている")
    return ObservedThermalInput.model_validate(observed.model_dump(mode="python"))


def _validate_artifact_container_sizes(artifact: ThermalModelArtifact) -> None:
    if len(artifact.manifest.source_runs) > MAX_SOURCE_RUNS:
        raise ValueError("source run数がartifact安全上限を超えている")
    if len(artifact.manifest.authority_compatibility) > 1:
        raise ValueError("Dataset v1 artifactはSHADOW authorityだけに限定する")
    _validate_feature_schema_container_sizes(artifact.feature_schema)
    if len(artifact.target_schema.horizons_ms) > MAX_TARGET_HORIZONS:
        raise ValueError("target horizon数がartifact安全上限を超えている")
    if len(artifact.target_schema.metrics) > MAX_TARGET_METRICS:
        raise ValueError("target metric数がartifact安全上限を超えている")
    if len(artifact.target_schema.outputs) > MAX_TARGET_OUTPUTS:
        raise ValueError("target output数がartifact安全上限を超えている")
    _validate_metric_name_lengths(artifact.target_schema.metrics)
    if any(len(output) > MAX_SCHEMA_COLUMN_LENGTH for output in artifact.target_schema.outputs):
        raise ValueError("target output名がartifact安全上限を超えている")
    payload = artifact.payload
    vectors = (
        payload.feature_means,
        payload.feature_scales,
        payload.feature_observed_counts,
    )
    if any(len(vector) > MAX_FEATURE_COLUMNS for vector in vectors):
        raise ValueError("normalization vectorがartifact安全上限を超えている")
    if len(payload.outputs) > MAX_TARGET_OUTPUTS:
        raise ValueError("ridge output数がartifact安全上限を超えている")
    if any(len(output.coefficients) > MAX_FEATURE_COLUMNS for output in payload.outputs):
        raise ValueError("ridge coefficient数がartifact安全上限を超えている")
    _validate_metric_name_lengths(tuple(output.metric for output in payload.outputs))


def _validated_artifact(artifact: ThermalModelArtifact) -> ThermalModelArtifact:
    _validate_artifact_container_sizes(artifact)
    return ThermalModelArtifact.model_validate(artifact.model_dump(mode="python"))


def _validated_registry_metadata(metadata: ThermalRegistryMetadata) -> ThermalRegistryMetadata:
    if len(metadata.source_runs) > MAX_SOURCE_RUNS:
        raise ValueError("Registry source run数が安全上限を超えている")
    if len(metadata.hyperparameters) > MAX_REGISTRY_HYPERPARAMETERS:
        raise ValueError("Registry hyperparameter数が安全上限を超えている")
    if len(metadata.authority_compatibility) > 1:
        raise ValueError("Dataset v1 Registry metadataはSHADOWだけに限定する")
    for key, value in metadata.hyperparameters.items():
        if len(key) > MAX_METADATA_TEXT_LENGTH or (
            isinstance(value, str) and len(value) > MAX_METADATA_TEXT_LENGTH
        ):
            raise ValueError("Registry hyperparameter文字列が安全上限を超えている")
        _validate_hyperparameter_scalar(value, source="Registry")
    return ThermalRegistryMetadata.model_validate(metadata.model_dump(mode="python"))


def _validate_external_registry_metadata_sizes(metadata: BaseModel) -> None:
    source_runs = getattr(metadata, "source_runs", ())
    hyperparameters = getattr(metadata, "hyperparameters", {})
    authority = getattr(metadata, "authority_compatibility", ())
    if not isinstance(source_runs, tuple) or len(source_runs) > MAX_SOURCE_RUNS:
        raise ValueError("#104 metadata source run数が安全上限を超えている")
    if not isinstance(hyperparameters, dict) or len(hyperparameters) > MAX_REGISTRY_HYPERPARAMETERS:
        raise ValueError("#104 metadata hyperparameter数が安全上限を超えている")
    for value in hyperparameters.values():
        _validate_hyperparameter_scalar(value, source="#104 metadata")
    if not isinstance(authority, tuple) or len(authority) > 1:
        raise ValueError("#104 metadata authority数が安全上限を超えている")
    text_values = (
        getattr(metadata, "kind", ""),
        getattr(metadata, "artifact_format", ""),
        getattr(metadata, "model_id", ""),
        getattr(metadata, "version", ""),
        getattr(metadata, "created_at", ""),
        getattr(metadata, "training_dataset_version", ""),
        *source_runs,
        getattr(metadata, "feature_schema_version", ""),
        getattr(metadata, "target_schema_version", ""),
        getattr(metadata, "model_family", ""),
        getattr(metadata, "sha256", ""),
    )
    optional_text = (
        getattr(metadata, "code_commit", None),
        getattr(metadata, "offline_evaluation_ref", None),
        getattr(metadata, "shadow_evaluation_ref", None),
    )
    hyperparameter_text = tuple(hyperparameters) + tuple(
        value for value in hyperparameters.values() if isinstance(value, str)
    )
    all_text = (
        *text_values,
        *(value for value in optional_text if value is not None),
        *hyperparameter_text,
    )
    if any(
        not isinstance(value, str) or len(value) > MAX_METADATA_TEXT_LENGTH for value in all_text
    ):
        raise ValueError("#104 metadata文字列が安全上限を超えている")


def _validate_hyperparameter_scalar(value: object, *, source: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if not MIN_INT64 <= value <= MAX_TIME_MS:
            raise ValueError(f"{source} hyperparameter整数がint64上限を超えている")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{source} hyperparameterはfinite値に限定する")
        return
    raise ValueError(f"{source} hyperparameter型が契約外である")


def _validated_expectation(
    expected: ThermalArtifactExpectation,
) -> ThermalArtifactExpectation:
    metadata = _validated_registry_metadata(expected.registry_metadata)
    return ThermalArtifactExpectation.model_validate(
        {
            "registry_metadata": metadata,
            "authority_stage": expected.authority_stage,
        }
    )


def _validate_metric_name_lengths(metrics: tuple[str, ...]) -> None:
    if any(len(metric) > MAX_METRIC_NAME_LENGTH for metric in metrics):
        raise ValueError("metric名がartifact安全上限を超えている")


def _is_bounded_time_value(value: object, *, positive: bool = False) -> bool:
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return (0 < value <= MAX_TIME_MS) if positive else (0 <= value <= MAX_TIME_MS)


def _feature_columns(
    *, window_ms: int, sample_period_ms: int, metrics: tuple[str, ...]
) -> tuple[str, ...]:
    columns: list[str] = []
    for offset_ms in range(-window_ms, 1, sample_period_ms):
        for metric in metrics:
            columns.extend(
                (
                    f"window[{offset_ms}].{metric}.value",
                    f"window[{offset_ms}].{metric}.missing",
                    f"window[{offset_ms}].{metric}.stale",
                    f"window[{offset_ms}].{metric}.suspect",
                )
            )
    columns.extend(f"action.{zone.value}.effective_demand" for zone in _ZONE_ORDER)
    return tuple(columns)


def _target_outputs(horizons_ms: tuple[int, ...], metrics: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"target[{horizon}].{metric}" for horizon in horizons_ms for metric in metrics)


def _validate_feature_shape_values(
    *, window_ms: int, sample_period_ms: int, metric_count: int
) -> tuple[int, int]:
    if window_ms <= 0 or sample_period_ms <= 0 or metric_count <= 0:
        raise ValueError("feature shapeは正のwindow / period / metric数を必要とする")
    if window_ms > MAX_TIME_MS or sample_period_ms > MAX_TIME_MS:
        raise ValueError("feature時刻値がartifact安全上限を超えている")
    if window_ms % sample_period_ms:
        raise ValueError("feature windowはsample periodの整数倍にする")
    if metric_count > MAX_FEATURE_METRICS:
        raise ValueError("feature metric数がartifact安全上限を超えている")
    frame_count = window_ms // sample_period_ms + 1
    if frame_count > MAX_WINDOW_FRAMES:
        raise ValueError("feature window frame数がartifact安全上限を超えている")
    column_count = frame_count * metric_count * 4 + len(_ZONE_ORDER)
    if column_count > MAX_FEATURE_COLUMNS:
        raise ValueError("feature column数がartifact安全上限を超えている")
    return frame_count, column_count


def _validate_target_shape_values(*, horizon_count: int, metric_count: int) -> int:
    if horizon_count <= 0 or metric_count <= 0:
        raise ValueError("target shapeは正のhorizon / metric数を必要とする")
    if horizon_count > MAX_TARGET_HORIZONS:
        raise ValueError("target horizon数がartifact安全上限を超えている")
    if metric_count > MAX_TARGET_METRICS:
        raise ValueError("target metric数がartifact安全上限を超えている")
    output_count = horizon_count * metric_count
    if output_count > MAX_TARGET_OUTPUTS:
        raise ValueError("target output数がartifact安全上限を超えている")
    return output_count


def _registry_metadata_from_artifact(
    artifact: ThermalModelArtifact, artifact_bytes: bytes
) -> ThermalRegistryMetadata:
    manifest = artifact.manifest
    return ThermalRegistryMetadata(
        capability=manifest.capability,
        model_id=manifest.model_id,
        version=manifest.model_version,
        created_at=manifest.created_at,
        training_dataset_version=manifest.training_dataset_version,
        source_runs=tuple(source.run_id for source in manifest.source_runs),
        code_commit=manifest.code_commit,
        sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        hyperparameters={"ridge_lambda": manifest.hyperparameters.ridge_lambda},
        authority_compatibility=manifest.authority_compatibility,
    )


def _registry_contract(metadata: ThermalRegistryMetadata) -> tuple[object, ...]:
    """Exclude lifecycle evaluation refs while binding every artifact-owned field."""
    # `schema_version` は Registry の封筒の版で artifact が決める値ではない。v2 で登録済みの
    # production artifact を、版が違うだけで読めなくしない（#89 レビュー）。
    return (
        metadata.kind,
        metadata.artifact_format,
        metadata.capability,
        metadata.model_id,
        metadata.version,
        metadata.created_at,
        metadata.training_dataset_version,
        metadata.source_runs,
        metadata.feature_schema_version,
        metadata.target_schema_version,
        metadata.code_commit,
        metadata.sha256,
        metadata.model_family,
        tuple(sorted(metadata.hyperparameters.items())),
        metadata.authority_compatibility,
    )


def is_pickle_payload(payload: bytes) -> bool:
    """Conservative helper used only to give unsafe legacy formats a clear rejection reason."""
    return bool(payload) and (payload[0] == 0x80 or re.match(rb"\(c[^\n]+\n", payload) is not None)
