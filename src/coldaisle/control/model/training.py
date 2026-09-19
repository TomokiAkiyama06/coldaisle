"""Deterministic offline ridge baseline training for Thermal Dataset v1 (#84)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from coldaisle.control.model.dataset import (
    DatasetExample,
    DatasetManifest,
    DatasetSpec,
    DatasetSplit,
    ThermalDataset,
)
from coldaisle.control.model.thermal import (
    MAX_FEATURE_COLUMNS,
    MAX_FEATURE_METRICS,
    MAX_METRIC_NAME_LENGTH,
    MAX_SOURCE_RUNS,
    MAX_TARGET_HORIZONS,
    MAX_TARGET_METRICS,
    MAX_TIME_MS,
    MAX_WINDOW_FRAMES,
    DatasetArtifactId,
    InferenceCapability,
    ModelId,
    ObservedThermalInput,
    RidgeHyperparameters,
    RidgeModelPayload,
    RidgeOutput,
    SemanticVersion,
    ThermalFeatureSchema,
    ThermalModelArtifact,
    ThermalModelManifest,
    ThermalTargetSchema,
    TrainingSourceProvenance,
    canonical_sha256,
    raw_feature_values,
)
from coldaisle.control.schema import AuthorityStage
from coldaisle.store.models import Quality

MAX_TRAIN_EXAMPLES = 100_000
MAX_TRAINING_CELLS = 1_000_000
MAX_DATASET_EXAMPLES = 100_000
MAX_DATASET_FEATURE_CELLS = 1_000_000
MAX_DATASET_TARGET_CELLS = 1_000_000
MAX_SOURCE_REFS_PER_RUN = 256
MAX_DATASET_METADATA_ITEMS = 1_000_000
MAX_METADATA_ITEMS_PER_EXAMPLE = 256
MAX_METADATA_TEXT_LENGTH = 500
MAX_DATASET_TEXT_BYTES = 8 * 1024 * 1024
MAX_RIDGE_WORK_UNITS = 50_000_000
MAX_DATASET_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_DATASET_EXAMPLES_BYTES = 256 * 1024 * 1024
_DATASET_ARTIFACT_ALIAS = re.compile(r"^dataset-[0-9a-f]{32}$")
_DATASET_MANIFEST_FILENAME = "manifest.json"
_DATASET_EXAMPLES_FILENAME = "examples.jsonl"
_DATASET_BINDING_TOKEN = object()
# Numerical-precision constant, not a tunable: one ULP of 1.0 for IEEE-754 binary64.
_FLOAT_EPSILON = sys.float_info.epsilon


class IneffectiveRidgeLambdaError(ValueError):
    """``ridge_lambda`` is too small to survive floating-point addition to the Gram diagonal.

    The user's lambda is never silently raised; callers choose a value at or above
    ``minimum_effective_lambda`` explicitly.
    """

    def __init__(self, *, ridge_lambda: float, minimum_effective_lambda: float) -> None:
        self.ridge_lambda = ridge_lambda
        self.minimum_effective_lambda = minimum_effective_lambda
        super().__init__(
            "ridge_lambdaがGram行列の対角スケールに対して小さすぎ、浮動小数点の丸めで消える: "
            f"ridge_lambda={ridge_lambda!r}, "
            f"minimum_effective_lambda={minimum_effective_lambda!r}"
        )


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class VerifiedTrainingDatasetArtifact:
    """Published #83 Dataset bytes bound to their path-derived public alias."""

    _artifact_id: DatasetArtifactId
    _dataset: ThermalDataset
    _manifest_sha256: str
    _examples_sha256: str
    _sealed: bool

    __slots__ = (
        "_artifact_id",
        "_dataset",
        "_examples_sha256",
        "_manifest_sha256",
        "_sealed",
    )

    def __init__(
        self,
        *,
        artifact_id: DatasetArtifactId,
        dataset: ThermalDataset,
        manifest_sha256: str,
        examples_sha256: str,
        _token: object,
    ) -> None:
        if _token is not _DATASET_BINDING_TOKEN:
            raise TypeError("VerifiedTrainingDatasetArtifactはpublished artifactからだけ構築できる")
        object.__setattr__(self, "_artifact_id", artifact_id)
        object.__setattr__(self, "_dataset", dataset)
        object.__setattr__(self, "_manifest_sha256", manifest_sha256)
        object.__setattr__(self, "_examples_sha256", examples_sha256)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("VerifiedTrainingDatasetArtifactはimmutable")

    @property
    def artifact_id(self) -> DatasetArtifactId:
        """Return the alias derived from the published artifact directory name."""
        return self._artifact_id

    @property
    def dataset(self) -> ThermalDataset:
        """Return the Dataset object bound to the verified manifest."""
        return self._dataset

    @property
    def manifest_sha256(self) -> str:
        """Return the exact published manifest bytes checksum."""
        return self._manifest_sha256

    @property
    def examples_sha256(self) -> str:
        """Return the exact published examples JSONL checksum."""
        return self._examples_sha256


class RidgeTrainingSpec(_Frozen):
    """Caller-selected identity and hyperparameters; none are production defaults."""

    model_id: ModelId
    model_version: SemanticVersion
    created_at: str = Field(min_length=1, max_length=64)
    ridge_lambda: float = Field(gt=0.0, allow_inf_nan=False)
    code_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")

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


def verify_training_dataset_artifact(
    dataset: ThermalDataset,
    manifest_path: Path,
    examples_path: Path,
) -> VerifiedTrainingDatasetArtifact:
    """Bind a Dataset to exact published bytes and its directory-derived public alias."""
    if manifest_path.name != _DATASET_MANIFEST_FILENAME:
        raise ValueError("Dataset manifest filenameがv1 artifact contractと一致しない")
    if examples_path.name != _DATASET_EXAMPLES_FILENAME:
        raise ValueError("Dataset examples filenameがv1 artifact contractと一致しない")
    if manifest_path.parent != examples_path.parent:
        raise ValueError("Dataset manifestとexamplesは同じartifact directoryに置く")
    artifact_id = manifest_path.parent.name
    if _DATASET_ARTIFACT_ALIAS.fullmatch(artifact_id) is None:
        raise ValueError("Dataset artifact directory名は公開用aliasにする")

    directory_fd = _open_directory_without_symlinks(manifest_path.parent)
    try:
        manifest_bytes = _read_regular_file_at(
            directory_fd,
            _DATASET_MANIFEST_FILENAME,
            max_bytes=MAX_DATASET_MANIFEST_BYTES,
        )
        examples_sha256 = _hash_regular_file_at(
            directory_fd,
            _DATASET_EXAMPLES_FILENAME,
            max_bytes=MAX_DATASET_EXAMPLES_BYTES,
        )
    finally:
        os.close(directory_fd)
    manifest = DatasetManifest.model_validate_json(manifest_bytes)
    canonical_manifest = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    if manifest_bytes != canonical_manifest:
        raise ValueError("Dataset manifestはcanonical published bytesに限定する")
    if manifest != dataset.manifest:
        raise ValueError("published Dataset manifestが学習対象datasetと一致しない")
    if examples_sha256 != manifest.examples_sha256:
        raise ValueError("published Dataset examples checksumがmanifestと一致しない")
    return VerifiedTrainingDatasetArtifact(
        artifact_id=artifact_id,
        dataset=dataset,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        examples_sha256=examples_sha256,
        _token=_DATASET_BINDING_TOKEN,
    )


def train_ridge_baseline(
    source: VerifiedTrainingDatasetArtifact,
    split: DatasetSplit,
    training: RidgeTrainingSpec,
) -> ThermalModelArtifact:
    """Fit a deterministic observational baseline using only ``split.train``.

    Validation, test, and purged examples are checked for provenance and temporal isolation but
    never enter preprocessing statistics or the linear solver.
    """
    if not isinstance(source, VerifiedTrainingDatasetArtifact):
        raise TypeError("ridge baseline学習は検証済みDataset artifactを必要とする")
    dataset = source.dataset
    training = RidgeTrainingSpec.model_validate(training.model_dump(mode="python"))
    # Preflight shape before ThermalDataset's full example validation materializes frame ranges.
    _validate_dataset_spec_container_sizes(dataset.manifest.spec)
    raw_spec = DatasetSpec.model_validate(dataset.manifest.spec.model_dump(mode="python"))
    feature_schema = ThermalFeatureSchema.from_dataset_spec(raw_spec)
    target_schema = ThermalTargetSchema.from_dataset_spec(raw_spec)
    _validate_training_resource_limits(
        dataset=dataset,
        split=split,
        feature_count=len(feature_schema.columns),
        output_count=len(target_schema.outputs),
    )
    # model_copy(update=...) can bypass Pydantic validation.  Revalidate both trust boundaries.
    dataset = ThermalDataset.model_validate(dataset.model_dump(mode="python"))
    split = DatasetSplit.model_validate(split.model_dump(mode="python"))
    manifest_bytes = (dataset.manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != source.manifest_sha256:
        raise ValueError("training dataset manifest checksumがprovenanceと一致しない")
    if dataset.manifest.examples_sha256 != source.examples_sha256:
        raise ValueError("training dataset examples checksumがprovenanceと一致しない")
    _validate_split(dataset, split)
    if not split.train:
        raise ValueError("ridge baselineの学習にはtrain exampleが1件以上必要")
    train_examples = tuple(sorted(split.train, key=_example_order))
    raw_rows = tuple(
        raw_feature_values(ObservedThermalInput.from_example(example), feature_schema)
        for example in train_examples
    )
    means, scales, observed_counts, rows = _fit_preprocessing(raw_rows)

    outputs: list[RidgeOutput] = []
    for horizon in target_schema.horizons_ms:
        for metric in target_schema.metrics:
            selected_rows: list[tuple[float, ...]] = []
            labels: list[float] = []
            for example, row in zip(train_examples, rows, strict=True):
                target = next(item for item in example.targets if item.horizon_ms == horizon)
                value = target.values[metric]
                if target.quality[metric] is not Quality.OK or value is None:
                    continue
                selected_rows.append(row)
                labels.append(value)
            if not labels:
                raise ValueError(
                    f"観測済みlabelが無いため学習できない: horizon={horizon}, metric={metric}"
                )
            intercept, coefficients = _fit_ridge(
                tuple(selected_rows),
                tuple(labels),
                ridge_lambda=training.ridge_lambda,
            )
            outputs.append(
                RidgeOutput(
                    horizon_ms=horizon,
                    metric=metric,
                    intercept=intercept,
                    coefficients=coefficients,
                    training_samples=len(labels),
                )
            )

    payload = RidgeModelPayload(
        feature_means=means,
        feature_scales=scales,
        feature_observed_counts=observed_counts,
        outputs=tuple(outputs),
    )
    source_runs = tuple(
        sorted(
            (
                TrainingSourceProvenance(
                    run_id=source.run_id,
                    source_sha256=source.source_sha256,
                )
                for source in dataset.manifest.source_runs
            ),
            key=lambda source: source.run_id,
        )
    )
    manifest = ThermalModelManifest(
        model_id=training.model_id,
        model_version=training.model_version,
        created_at=training.created_at,
        training_dataset_version=source.artifact_id,
        training_dataset_manifest_sha256=manifest_sha256,
        training_examples_sha256=dataset.manifest.examples_sha256,
        source_runs=source_runs,
        training_split_sha256=split_sha256(split),
        training_example_count=len(train_examples),
        feature_schema_sha256=canonical_sha256(feature_schema),
        target_schema_sha256=canonical_sha256(target_schema),
        model_payload_sha256=canonical_sha256(payload),
        code_commit=training.code_commit,
        hyperparameters=RidgeHyperparameters(ridge_lambda=training.ridge_lambda),
        capability=InferenceCapability.OBSERVATIONAL_REPLAY,
        authority_compatibility=(AuthorityStage.SHADOW,),
    )
    return ThermalModelArtifact(
        manifest=manifest,
        feature_schema=feature_schema,
        target_schema=target_schema,
        payload=payload,
    )


def split_sha256(split: DatasetSplit) -> str:
    """Hash bucket membership and every example's complete observed time interval."""
    value = {
        bucket: [
            {
                "example_id": example.example_id,
                "source_run_id": example.source_run_id,
                "history_start_ms": example.history_start_ms,
                "action_ts_ms": example.action_ts_ms,
                "label_end_ms": example.label_end_ms,
            }
            for example in sorted(getattr(split, bucket), key=_example_order)
        ]
        for bucket in ("train", "validation", "test", "purged")
    }
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    return hashlib.sha256(encoded).hexdigest()


def _validate_split(dataset: ThermalDataset, split: DatasetSplit) -> None:
    expected = {example.example_id: example for example in dataset.examples}
    seen: set[str] = set()
    buckets = (split.train, split.validation, split.test, split.purged)
    for bucket in buckets:
        for example in bucket:
            if example.example_id in seen:
                raise ValueError("dataset split内でexampleが重複している")
            seen.add(example.example_id)
            if expected.get(example.example_id) != example:
                raise ValueError("dataset splitに元datasetと一致しないexampleがある")
    if seen != set(expected):
        raise ValueError("dataset splitは元datasetの全exampleを一度ずつ分類する必要がある")

    ordered_buckets = (split.train, split.validation, split.test)
    previous: tuple[DatasetExample, ...] = ()
    for current in ordered_buckets:
        if not current:
            continue
        if previous and max(item.label_end_ms for item in previous) >= min(
            item.history_start_ms for item in current
        ):
            raise ValueError("train / validation / testの観測期間が重なるか時系列順でない")
        previous = current


def _validate_dataset_spec_container_sizes(spec: DatasetSpec) -> None:
    if len(spec.feature_metrics) > MAX_FEATURE_METRICS:
        raise ValueError("feature metric数がbaseline trainerの安全上限を超えている")
    if len(spec.target_metrics) > MAX_TARGET_METRICS:
        raise ValueError("target metric数がbaseline trainerの安全上限を超えている")
    if len(spec.horizons_ms) > MAX_TARGET_HORIZONS:
        raise ValueError("target horizon数がbaseline trainerの安全上限を超えている")
    positive_times = (
        spec.window_ms,
        spec.sample_period_ms,
        spec.stale_after_ms,
        *spec.horizons_ms,
    )
    if any(not _is_bounded_integer(value, positive=True) for value in positive_times):
        raise ValueError("dataset spec時刻値がbaseline trainerの安全上限を超えている")
    if not _is_bounded_integer(spec.target_tolerance_ms, positive=False):
        raise ValueError("target toleranceがbaseline trainerの安全上限を超えている")
    metrics = (*spec.feature_metrics, *spec.target_metrics)
    if any(len(metric) > MAX_METRIC_NAME_LENGTH for metric in metrics):
        raise ValueError("metric名がbaseline trainerの安全上限を超えている")
    if sum(len(metric.encode("utf-8")) for metric in metrics) > MAX_DATASET_TEXT_BYTES:
        raise ValueError("metric名の総byte数がbaseline trainerの安全上限を超えている")


def _validate_training_resource_limits(
    *,
    dataset: ThermalDataset,
    split: DatasetSplit,
    feature_count: int,
    output_count: int,
) -> None:
    train_count = len(split.train)
    dataset_count = len(dataset.examples)
    split_count = sum(
        len(bucket) for bucket in (split.train, split.validation, split.test, split.purged)
    )
    if train_count > MAX_TRAIN_EXAMPLES:
        raise ValueError("train example数がbaseline trainerの安全上限を超えている")
    if dataset_count > MAX_DATASET_EXAMPLES or split_count > MAX_DATASET_EXAMPLES:
        raise ValueError("dataset / split example総数がbaseline trainerの安全上限を超えている")
    if len(dataset.manifest.source_runs) > MAX_SOURCE_RUNS:
        raise ValueError("source run数がbaseline trainerの安全上限を超えている")
    if feature_count > MAX_FEATURE_COLUMNS:
        raise ValueError("feature column数がbaseline trainerの安全上限を超えている")
    if train_count * feature_count > MAX_TRAINING_CELLS:
        raise ValueError("train feature cell数がbaseline trainerの安全上限を超えている")
    if dataset_count * feature_count > MAX_DATASET_FEATURE_CELLS:
        raise ValueError("dataset feature cell総数がbaseline trainerの安全上限を超えている")
    if split_count * feature_count > MAX_DATASET_FEATURE_CELLS:
        raise ValueError("split feature cell総数がbaseline trainerの安全上限を超えている")
    if dataset_count * output_count > MAX_DATASET_TARGET_CELLS:
        raise ValueError("dataset target cell総数がbaseline trainerの安全上限を超えている")
    if split_count * output_count > MAX_DATASET_TARGET_CELLS:
        raise ValueError("split target cell総数がbaseline trainerの安全上限を超えている")
    if split_count != dataset_count:
        raise ValueError("datasetとsplitのexample総数が一致しない")
    ridge_work = output_count * (train_count * feature_count * feature_count + feature_count**3)
    if ridge_work > MAX_RIDGE_WORK_UNITS:
        raise ValueError("ridge演算量がbaseline trainerの安全上限を超えている")
    spec = dataset.manifest.spec
    expected_frame_count = spec.window_ms // spec.sample_period_ms + 1
    _validate_source_run_container_sizes(dataset)
    _dataset_metadata_items, dataset_text_bytes = _validate_examples_container_sizes(
        dataset.examples,
        expected_frame_count=expected_frame_count,
        feature_metric_count=len(spec.feature_metrics),
        target_count=len(spec.horizons_ms),
        target_metric_count=len(spec.target_metrics),
    )
    if dataset_text_bytes > MAX_DATASET_TEXT_BYTES:
        raise ValueError("dataset text総byte数がbaseline trainerの安全上限を超えている")
    split_metadata_items = 0
    split_text_bytes = 0
    for bucket in (split.train, split.validation, split.test, split.purged):
        bucket_metadata_items, bucket_text_bytes = _validate_examples_container_sizes(
            bucket,
            expected_frame_count=expected_frame_count,
            feature_metric_count=len(spec.feature_metrics),
            target_count=len(spec.horizons_ms),
            target_metric_count=len(spec.target_metrics),
        )
        split_metadata_items += bucket_metadata_items
        split_text_bytes += bucket_text_bytes
        if split_metadata_items > MAX_DATASET_METADATA_ITEMS:
            raise ValueError("split metadata item総数がbaseline trainerの安全上限を超えている")
        if split_text_bytes > MAX_DATASET_TEXT_BYTES:
            raise ValueError("split text総byte数がbaseline trainerの安全上限を超えている")


def _validate_source_run_container_sizes(dataset: ThermalDataset) -> None:
    total_refs = 0
    total_text_bytes = 0
    for source in dataset.manifest.source_runs:
        if len(source.source_refs) > MAX_SOURCE_REFS_PER_RUN:
            raise ValueError("source refs数がbaseline trainerの安全上限を超えている")
        if not _is_bounded_integer(source.start_ms, positive=False) or not _is_bounded_integer(
            source.end_ms, positive=True
        ):
            raise ValueError("source run時刻値がbaseline trainerの安全上限を超えている")
        total_refs += len(source.source_refs)
        source_text = (source.run_id, source.source_sha256, *source.source_refs)
        if any(len(value) > MAX_METADATA_TEXT_LENGTH for value in source_text):
            raise ValueError("source run文字列がbaseline trainerの安全上限を超えている")
        total_text_bytes += sum(len(value.encode("utf-8")) for value in source_text)
    if total_refs > MAX_DATASET_METADATA_ITEMS:
        raise ValueError("source refs総数がbaseline trainerの安全上限を超えている")
    if total_text_bytes > MAX_DATASET_TEXT_BYTES:
        raise ValueError("source run text総byte数がbaseline trainerの安全上限を超えている")


def _validate_examples_container_sizes(
    examples: tuple[DatasetExample, ...],
    *,
    expected_frame_count: int,
    feature_metric_count: int,
    target_count: int,
    target_metric_count: int,
) -> tuple[int, int]:
    metadata_items = 0
    text_bytes = 0
    for example in examples:
        example_integers = (
            example.history_start_ms,
            example.action_ts_ms,
            example.label_end_ms,
            example.control_tick_id,
            example.control_schema_version,
        )
        if any(not _is_bounded_integer(value, positive=False) for value in example_integers):
            raise ValueError("example整数値がbaseline trainerの安全上限を超えている")
        if len(example.window) != expected_frame_count or len(example.window) > MAX_WINDOW_FRAMES:
            raise ValueError("dataset window frame数がspecまたは安全上限と一致しない")
        if len(example.targets) != target_count or len(example.targets) > MAX_TARGET_HORIZONS:
            raise ValueError("dataset target horizon数がspecまたは安全上限と一致しない")
        metadata_groups = (
            example.action.front.override_reasons,
            example.action.rear.override_reasons,
            example.action.top.override_reasons,
            example.context.fault_codes,
        )
        example_metadata_items = sum(len(group) for group in metadata_groups)
        if example_metadata_items > MAX_METADATA_ITEMS_PER_EXAMPLE:
            raise ValueError("example metadata item数がbaseline trainerの安全上限を超えている")
        metadata_items += example_metadata_items
        if metadata_items > MAX_DATASET_METADATA_ITEMS:
            raise ValueError("example metadata item総数がbaseline trainerの安全上限を超えている")
        metadata_text = (
            example.example_id,
            example.source_run_id,
            example.action.front.bound_by,
            example.action.front.controller_reason,
            example.action.rear.bound_by,
            example.action.rear.controller_reason,
            example.action.top.bound_by,
            example.action.top.controller_reason,
            *(reason for group in metadata_groups for reason in group),
        )
        if example.context.supervisor_policy is not None:
            metadata_text = (*metadata_text, example.context.supervisor_policy)
        if any(len(value) > MAX_METADATA_TEXT_LENGTH for value in metadata_text):
            raise ValueError("example metadata文字列がbaseline trainerの安全上限を超えている")
        text_bytes += sum(len(value.encode("utf-8")) for value in metadata_text)
        if text_bytes > MAX_DATASET_TEXT_BYTES:
            raise ValueError("example text総byte数がbaseline trainerの安全上限を超えている")
        for frame in example.window:
            if not _is_bounded_integer(frame.ts_ms, positive=False) or any(
                source_ts is not None and not _is_bounded_integer(source_ts, positive=False)
                for source_ts in frame.source_ts_ms.values()
            ):
                raise ValueError("window時刻値がbaseline trainerの安全上限を超えている")
            frame_mappings = (
                frame.values,
                frame.source_ts_ms,
                frame.quality,
                frame.missing_mask,
                frame.stale_mask,
            )
            if any(len(mapping) != feature_metric_count for mapping in frame_mappings):
                raise ValueError("dataset feature metric数がspecと一致しない")
            frame_metrics = tuple(metric for mapping in frame_mappings for metric in mapping)
            if any(len(metric) > MAX_METRIC_NAME_LENGTH for metric in frame_metrics):
                raise ValueError("dataset feature metric名が安全上限を超えている")
            text_bytes += sum(len(metric.encode("utf-8")) for metric in frame_metrics)
            if text_bytes > MAX_DATASET_TEXT_BYTES:
                raise ValueError("example text総byte数がbaseline trainerの安全上限を超えている")
        for target in example.targets:
            if not _is_bounded_integer(target.horizon_ms, positive=True) or not _is_bounded_integer(
                target.expected_ts_ms, positive=False
            ):
                raise ValueError("target時刻値がbaseline trainerの安全上限を超えている")
            if any(
                source_ts is not None and not _is_bounded_integer(source_ts, positive=False)
                for source_ts in target.source_ts_ms.values()
            ):
                raise ValueError("target source時刻値がbaseline trainerの安全上限を超えている")
            target_mappings = (
                target.values,
                target.source_ts_ms,
                target.quality,
                target.missing_mask,
            )
            if any(len(mapping) != target_metric_count for mapping in target_mappings):
                raise ValueError("dataset target metric数がspecと一致しない")
            target_metrics = tuple(metric for mapping in target_mappings for metric in mapping)
            if any(len(metric) > MAX_METRIC_NAME_LENGTH for metric in target_metrics):
                raise ValueError("dataset target metric名が安全上限を超えている")
            text_bytes += sum(len(metric.encode("utf-8")) for metric in target_metrics)
            if text_bytes > MAX_DATASET_TEXT_BYTES:
                raise ValueError("example text総byte数がbaseline trainerの安全上限を超えている")
    return metadata_items, text_bytes


def _is_bounded_integer(value: object, *, positive: bool) -> bool:
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return (0 < value <= MAX_TIME_MS) if positive else (0 <= value <= MAX_TIME_MS)


def _open_directory_without_symlinks(path: Path) -> int:
    absolute = path if path.is_absolute() else Path.cwd() / path
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _open_regular_file_at(directory_fd: int, name: str, *, max_bytes: int) -> tuple[int, int]:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    file_fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("Dataset artifact componentはregular fileに限定する")
        if file_stat.st_size > max_bytes:
            raise ValueError("Dataset artifact componentが安全なsize上限を超えている")
        return file_fd, file_stat.st_size
    except BaseException:
        os.close(file_fd)
        raise


def _read_regular_file_at(directory_fd: int, name: str, *, max_bytes: int) -> bytes:
    file_fd, expected_size = _open_regular_file_at(directory_fd, name, max_bytes=max_bytes)
    try:
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(file_fd, min(1024 * 1024, max_bytes + 1 - total)):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Dataset artifact componentが読取中にsize上限を超えた")
            chunks.append(chunk)
        if total != expected_size:
            raise ValueError("Dataset artifact componentが読取中に変更された")
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def _hash_regular_file_at(directory_fd: int, name: str, *, max_bytes: int) -> str:
    file_fd, expected_size = _open_regular_file_at(directory_fd, name, max_bytes=max_bytes)
    digest = hashlib.sha256()
    try:
        total = 0
        while chunk := os.read(file_fd, min(1024 * 1024, max_bytes + 1 - total)):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Dataset examplesが読取中にsize上限を超えた")
            digest.update(chunk)
        if total != expected_size:
            raise ValueError("Dataset examplesが読取中に変更された")
        return digest.hexdigest()
    finally:
        os.close(file_fd)


def _fit_preprocessing(
    raw_rows: tuple[tuple[float | None, ...], ...],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[int, ...], tuple[tuple[float, ...], ...]]:
    width = len(raw_rows[0])
    if any(len(row) != width for row in raw_rows):
        raise ValueError("feature rowの幅が一致しない")
    means: list[float] = []
    scales: list[float] = []
    observed_counts: list[int] = []
    for column in range(width):
        values = tuple(value for row in raw_rows if (value := row[column]) is not None)
        count = len(values)
        mean = math.fsum(values) / count if count else 0.0
        variance = math.fsum((value - mean) ** 2 for value in values) / count if count else 0.0
        scale = math.sqrt(variance) if variance > 0.0 else 1.0
        if not math.isfinite(mean) or not math.isfinite(scale):
            raise ValueError("feature normalizationが非有限になった")
        means.append(mean)
        scales.append(scale)
        observed_counts.append(count)
    normalized = tuple(
        tuple(
            0.0 if value is None else (value - means[index]) / scales[index]
            for index, value in enumerate(row)
        )
        for row in raw_rows
    )
    if any(not math.isfinite(value) for row in normalized for value in row):
        raise ValueError("normalized featureが非有限になった")
    return tuple(means), tuple(scales), tuple(observed_counts), normalized


def _fit_ridge(
    rows: tuple[tuple[float, ...], ...],
    labels: tuple[float, ...],
    *,
    ridge_lambda: float,
) -> tuple[float, tuple[float, ...]]:
    width = len(rows[0])
    label_mean = math.fsum(labels) / len(labels)
    feature_means = tuple(
        math.fsum(row[column] for row in rows) / len(rows) for column in range(width)
    )
    centered_rows = tuple(
        tuple(value - feature_means[column] for column, value in enumerate(row)) for row in rows
    )
    centered_labels = tuple(label - label_mean for label in labels)
    try:
        gram = [
            [math.fsum(row[left] * row[right] for row in centered_rows) for right in range(width)]
            for left in range(width)
        ]
        _require_effective_ridge_lambda(gram, ridge_lambda)
        for index in range(width):
            gram[index][index] += ridge_lambda
        rhs = [
            math.fsum(
                row[column] * label
                for row, label in zip(centered_rows, centered_labels, strict=True)
            )
            for column in range(width)
        ]
    except OverflowError as exc:
        raise ValueError("ridge normal equationが有限範囲を超えた") from exc
    coefficients = _solve_linear_system(gram, rhs)
    intercept = label_mean - math.fsum(
        coefficient * mean for coefficient, mean in zip(coefficients, feature_means, strict=True)
    )
    if not math.isfinite(intercept) or any(
        not math.isfinite(coefficient) for coefficient in coefficients
    ):
        raise ValueError("ridge parameterが非有限になった")
    return intercept, coefficients


def _minimum_effective_ridge_lambda(gram: list[list[float]]) -> float:
    """Smallest lambda that dominates rounding in the Gram matrix and its elimination.

    Rounding error in forming and eliminating an ``n``-column Gram matrix is bounded by roughly
    ``n * eps * max(diag)``.  A lambda below that cannot guarantee a numerically
    positive-definite system (e.g. duplicate normalized columns with ``1e-20``).  The bound is
    data-scale dependent (the diagonal grows with the number of train rows), so it is checked
    here rather than in ``RidgeTrainingSpec``.
    """
    width = len(gram)
    max_diagonal = max((abs(gram[index][index]) for index in range(width)), default=0.0)
    return width * _FLOAT_EPSILON * max_diagonal


def _require_effective_ridge_lambda(gram: list[list[float]], ridge_lambda: float) -> None:
    minimum = _minimum_effective_ridge_lambda(gram)
    if ridge_lambda <= minimum:
        raise IneffectiveRidgeLambdaError(
            ridge_lambda=ridge_lambda,
            minimum_effective_lambda=math.nextafter(minimum, math.inf),
        )


def _solve_linear_system(matrix: list[list[float]], values: list[float]) -> tuple[float, ...]:
    """Solve a positive-definite ridge system with a deterministic pivot rule."""
    size = len(values)
    augmented = [[*row, value] for row, value in zip(matrix, values, strict=True)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: (abs(augmented[row][column]), -row))
        if augmented[pivot][column] == 0.0:
            raise ValueError("ridge systemが特異で解けない")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        for index in range(column, size + 1):
            augmented[column][index] /= pivot_value
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    return tuple(augmented[row][size] for row in range(size))


def _example_order(example: DatasetExample) -> tuple[str, int, int, str]:
    return (
        example.source_run_id,
        example.action_ts_ms,
        example.control_tick_id,
        example.example_id,
    )
