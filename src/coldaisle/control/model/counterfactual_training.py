"""反実仮想 Thermal Model artifact v2 の決定的な ridge trainer（#84 / 決定記録 0079 段 2）。

Thermal Dataset v2（0087）の train だけから、計画 action の列（step × zone）を含む ridge 線形を
当てはめる。horizon ごとの head は、因果の mask（0079 §2.3）の外の計画 action の列を**落として**
当てはめ、その係数を厳密に 0 として置く。

- 係数の当てはめに使う計画 action の列は、Dataset v2 に記録した action 列（``action_steps``）
  そのものである（0084 §2.1 の表。``hold_effective`` は anchor 推論と Profile の residual の
  基準に使う）
- anchor action は ``prior_action``（anchor の tick より厳密に前で直近の tick の effective。
  0087 §2.7）
- window 幅・horizon・格子・ridge lambda・authority の互換・較正の値に既定値を置かない
- 較正の digest は呼び出し側から受け取らず、渡された**較正の値**と自分が作った ``metric_binding``
  から loader の L9 と同じ関数で計算する（決定記録 0096 §2.4 / §2.6）

trainer が返すのは **Profile v2 を含まない学習結果**である。Profile v2 を train / validation から
作るのは段 3（#85）で、:func:`assemble_counterfactual_artifact` が両者を1つの artifact に封じる。
"""

from __future__ import annotations

import hashlib
import os
import re
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coldaisle.control.model.calibration_digest import calibration_digest
from coldaisle.control.model.counterfactual import (
    ANCHOR_ACTION_RULE,
    DATASET_V2,
    ZONE_ORDER,
    ActionTrajectory,
    ArtifactSchemasV2,
    CalibrationBinding,
    ConfidenceProfileV2,
    CounterfactualModelManifest,
    CounterfactualThermalModelArtifact,
    MetricBinding,
    ProfileBindingV2,
    ThermalActionSchema,
    ThermalFeatureSchemaV2,
    TimeWindow,
    TrainingDataProvenanceV2,
    TrainingWindows,
    ZoneActionVariation,
    _validate_created_at,
    build_metric_binding,
    canonical_counterfactual_artifact_bytes,
    causal_plan_steps,
    feature_columns_v2,
    feature_row_v2,
    validate_authority_compatibility,
)
from coldaisle.control.model.dataset import (
    DatasetExampleV2,
    DatasetManifestV2,
    DatasetSpecV2,
    DatasetSplitV2,
    ThermalDatasetV2,
)
from coldaisle.control.model.thermal import (
    DatasetArtifactId,
    InferenceCapability,
    ModelId,
    ObservedThermalInput,
    RidgeHyperparameters,
    RidgeModelPayload,
    RidgeOutput,
    SemanticVersion,
    ThermalTargetSchema,
    TrainingSourceProvenance,
    _target_outputs,
    _validate_metric_name_lengths,
    _validate_target_shape_values,
    canonical_sha256,
)
from coldaisle.control.model.training import (
    MAX_DATASET_EXAMPLES_BYTES,
    MAX_DATASET_MANIFEST_BYTES,
    _example_order,
    _fit_preprocessing,
    _fit_ridge,
    _hash_regular_file_at,
    _open_directory_without_symlinks,
    _read_regular_file_at,
    _validate_split,
    _validate_training_resource_limits,
    split_sha256,
)
from coldaisle.control.schema import AuthorityStage, PerZone
from coldaisle.measurement import Quality
from coldaisle.metrics import MetricCatalog

_DATASET_ARTIFACT_ALIAS = re.compile(r"^dataset-[0-9a-f]{32}$")
_DATASET_MANIFEST_FILENAME = "manifest.json"
_DATASET_EXAMPLES_FILENAME = "examples.jsonl"
_DATASET_V2_BINDING_TOKEN = object()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class VerifiedTrainingDatasetArtifactV2:
    """公開済みの Dataset v2 の bytes と、directory 名から導いた公開 alias の束。"""

    _artifact_id: DatasetArtifactId
    _dataset: ThermalDatasetV2
    _manifest_sha256: str
    _examples_sha256: str

    __slots__ = ("_artifact_id", "_dataset", "_examples_sha256", "_manifest_sha256")

    def __init__(
        self,
        *,
        artifact_id: DatasetArtifactId,
        dataset: ThermalDatasetV2,
        manifest_sha256: str,
        examples_sha256: str,
        _token: object,
    ) -> None:
        if _token is not _DATASET_V2_BINDING_TOKEN:
            raise TypeError("VerifiedTrainingDatasetArtifactV2 は公開済みの artifact からだけ作る")
        object.__setattr__(self, "_artifact_id", artifact_id)
        object.__setattr__(self, "_dataset", dataset)
        object.__setattr__(self, "_manifest_sha256", manifest_sha256)
        object.__setattr__(self, "_examples_sha256", examples_sha256)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("VerifiedTrainingDatasetArtifactV2 は不変")

    @property
    def artifact_id(self) -> DatasetArtifactId:
        """公開 directory 名から導いた alias。"""
        return self._artifact_id

    @property
    def dataset(self) -> ThermalDatasetV2:
        """検証した manifest に束縛した Dataset v2。"""
        return self._dataset

    @property
    def manifest_sha256(self) -> str:
        """公開した manifest の bytes の SHA-256。"""
        return self._manifest_sha256

    @property
    def examples_sha256(self) -> str:
        """公開した examples JSONL の SHA-256。"""
        return self._examples_sha256


def verify_training_dataset_artifact_v2(
    dataset: ThermalDatasetV2,
    manifest_path: Path,
    examples_path: Path,
) -> VerifiedTrainingDatasetArtifactV2:
    """Dataset v2 を公開済みの bytes と directory 名の alias に束縛する（v1 と同じ手順）。"""
    manifest_file = manifest_path
    examples_file = examples_path
    if manifest_file.name != _DATASET_MANIFEST_FILENAME:
        raise ValueError("Dataset manifest の file 名が artifact の契約と一致しない")
    if examples_file.name != _DATASET_EXAMPLES_FILENAME:
        raise ValueError("Dataset examples の file 名が artifact の契約と一致しない")
    if manifest_file.parent != examples_file.parent:
        raise ValueError("Dataset の manifest と examples は同じ directory に置く")
    artifact_id = manifest_file.parent.name
    if _DATASET_ARTIFACT_ALIAS.fullmatch(artifact_id) is None:
        raise ValueError("Dataset artifact の directory 名は公開用 alias にする")
    directory_fd = _open_directory_without_symlinks(manifest_file.parent)
    try:
        manifest_bytes = _read_regular_file_at(
            directory_fd, _DATASET_MANIFEST_FILENAME, max_bytes=MAX_DATASET_MANIFEST_BYTES
        )
        examples_sha256 = _hash_regular_file_at(
            directory_fd, _DATASET_EXAMPLES_FILENAME, max_bytes=MAX_DATASET_EXAMPLES_BYTES
        )
    finally:
        os.close(directory_fd)
    manifest = DatasetManifestV2.model_validate_json(manifest_bytes)
    if manifest_bytes != (manifest.model_dump_json(indent=2) + "\n").encode("utf-8"):
        raise ValueError("Dataset manifest は canonical な公開 bytes に限る")
    if manifest != dataset.manifest:
        raise ValueError("公開した Dataset manifest が学習対象の dataset と一致しない")
    if examples_sha256 != manifest.examples_sha256:
        raise ValueError("公開した Dataset examples の checksum が manifest と一致しない")
    return VerifiedTrainingDatasetArtifactV2(
        artifact_id=artifact_id,
        dataset=dataset,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        examples_sha256=examples_sha256,
        _token=_DATASET_V2_BINDING_TOKEN,
    )


class CounterfactualTrainingSpec(_Frozen):
    """呼び出し側が明示する identity と学習の選択。**どれも本番の既定値を持たない。**"""

    model_id: ModelId
    model_version: SemanticVersion
    created_at: str = Field(min_length=1, max_length=64)
    ridge_lambda: float = Field(gt=0.0, allow_inf_nan=False)
    authority_compatibility: tuple[AuthorityStage, ...] = Field(min_length=1)
    """実測評価で裏づけた stage だけを SHADOW から順に（0079 §2.3）。"""
    calibration_offsets_c: dict[str, Annotated[float, Field(allow_inf_nan=False)]]
    """学習データの期間に効いていた較正の値（``Calibration.offsets_c``。チャネル名 → ℃）。

    合成の起点がその期間の較正ファイルを明示して読んで渡す（既定の path も既定値も置かない。
    0079 §2.3 / 0096 §2.4）。digest は trainer が計算する（0096 §2.6）。
    """
    code_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")

    @field_validator("created_at")
    @classmethod
    def _created_at_is_timezone_aware_rfc3339(cls, value: str) -> str:
        return _validate_created_at(value)

    @model_validator(mode="after")
    def _authority_starts_at_shadow(self) -> Self:
        validate_authority_compatibility(self.authority_compatibility)
        return self


class CounterfactualTrainedModel(_Frozen):
    """trainer の結果。**Profile v2 を含まない**ので、このままでは登録も推論もできない。

    段 3 の Profile v2 の生成が :meth:`profile_binding` に束縛した Profile を作り、
    :func:`assemble_counterfactual_artifact` が1つの artifact に封じる。
    """

    model_id: ModelId
    model_version: SemanticVersion
    created_at: str
    authority_compatibility: tuple[AuthorityStage, ...]
    training_data: TrainingDataProvenanceV2
    metric_binding: MetricBinding
    calibration_binding: CalibrationBinding
    hyperparameters: RidgeHyperparameters
    code_commit: str | None
    feature_schema: ThermalFeatureSchemaV2
    target_schema: ThermalTargetSchema
    action_schema: ThermalActionSchema
    payload: RidgeModelPayload

    def schemas(self) -> ArtifactSchemasV2:
        """3つの schema の canonical SHA-256。"""
        return ArtifactSchemasV2(
            feature_sha256=canonical_sha256(self.feature_schema),
            target_sha256=canonical_sha256(self.target_schema),
            action_sha256=canonical_sha256(self.action_schema),
        )

    def profile_binding(self) -> ProfileBindingV2:
        """この学習結果に Profile v2 を束縛する値（0079 §2.1。payload の hash に束縛する）。"""
        schemas = self.schemas()
        return ProfileBindingV2(
            model_id=self.model_id,
            model_version=self.model_version,
            payload_sha256=canonical_sha256(self.payload),
            feature_schema_sha256=schemas.feature_sha256,
            target_schema_sha256=schemas.target_sha256,
            action_schema_sha256=schemas.action_sha256,
            training_split_sha256=self.training_data.split_sha256,
        )


def assemble_counterfactual_artifact(
    trained: CounterfactualTrainedModel, profile: ConfidenceProfileV2
) -> CounterfactualThermalModelArtifact:
    """学習結果と同梱 Profile v2 を1つの artifact に封じる（0079 §2.1）。

    読み込み時と同じ検査（L6〜L8 の (1)(2)・L10〜L12）を作成時にも行い、満たさない artifact を
    作らない（0084 §2.3）。
    """
    trained = CounterfactualTrainedModel.model_validate(trained.model_dump(mode="python"))
    profile = ConfidenceProfileV2.model_validate(profile.model_dump(mode="python"))
    artifact = CounterfactualThermalModelArtifact(
        manifest=CounterfactualModelManifest(
            model_id=trained.model_id,
            model_version=trained.model_version,
            created_at=trained.created_at,
            capability=InferenceCapability.COUNTERFACTUAL_ACTION,
            authority_compatibility=trained.authority_compatibility,
            anchor_action_rule=ANCHOR_ACTION_RULE,
            training_data=trained.training_data,
            schemas=trained.schemas(),
            metric_binding=trained.metric_binding,
            calibration_binding=trained.calibration_binding,
            hyperparameters=trained.hyperparameters,
            code_commit=trained.code_commit,
            payload_sha256=canonical_sha256(trained.payload),
            confidence_profile_sha256=profile.sha256(),
        ),
        feature_schema=trained.feature_schema,
        target_schema=trained.target_schema,
        action_schema=trained.action_schema,
        payload=trained.payload,
        confidence_profile=profile,
    )
    canonical_counterfactual_artifact_bytes(artifact)
    return artifact


def train_counterfactual_ridge(
    source: VerifiedTrainingDatasetArtifactV2,
    split: DatasetSplitV2,
    training: CounterfactualTrainingSpec,
    *,
    metric_catalog: MetricCatalog,
) -> CounterfactualTrainedModel:
    """``split.train`` だけから、計画 action の列を含む ridge 線形を決定的に当てはめる。

    validation / test / purged は出どころと時間の分離の検査にだけ使い、正規化にも当てはめにも
    入れない。``metric_catalog`` は学習時の ``config/metrics.yaml`` で、使った metric の単位を
    ``metric_binding`` へ写す（無い metric があれば作らない）。
    """
    if not isinstance(source, VerifiedTrainingDatasetArtifactV2):
        raise TypeError("反実仮想 model の学習は検証済みの Dataset v2 artifact を要る")
    training = CounterfactualTrainingSpec.model_validate(training.model_dump(mode="python"))
    dataset = source.dataset
    raw_spec = DatasetSpecV2.model_validate(dataset.manifest.spec.model_dump(mode="python"))
    _validate_metric_name_lengths((*raw_spec.feature_metrics, *raw_spec.target_metrics))
    feature_schema = ThermalFeatureSchemaV2(
        window_ms=raw_spec.window_ms,
        sample_period_ms=raw_spec.sample_period_ms,
        stale_after_ms=raw_spec.stale_after_ms,
        metrics=raw_spec.feature_metrics,
        action_steps=raw_spec.action_steps,
        columns=feature_columns_v2(
            window_ms=raw_spec.window_ms,
            sample_period_ms=raw_spec.sample_period_ms,
            metrics=raw_spec.feature_metrics,
            action_steps=raw_spec.action_steps,
        ),
    )
    _validate_target_shape_values(
        horizon_count=len(raw_spec.horizons_ms), metric_count=len(raw_spec.target_metrics)
    )
    target_schema = ThermalTargetSchema(
        dataset_schema_version=DATASET_V2,
        horizons_ms=raw_spec.horizons_ms,
        metrics=raw_spec.target_metrics,
        outputs=_target_outputs(raw_spec.horizons_ms, raw_spec.target_metrics),
    )
    action_schema = ThermalActionSchema(
        step_ms=raw_spec.action_step_ms, steps=raw_spec.action_steps
    )
    _validate_training_resource_limits(
        dataset=dataset,
        split=split,
        feature_count=len(feature_schema.columns),
        output_count=len(target_schema.outputs),
    )
    # model_copy(update=...) は検証を通らない。両方の信頼境界を検証し直す
    dataset = ThermalDatasetV2.model_validate(dataset.model_dump(mode="python"))
    split = DatasetSplitV2.model_validate(split.model_dump(mode="python"))
    manifest_bytes = (dataset.manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != source.manifest_sha256:
        raise ValueError("学習 dataset の manifest checksum が出どころと一致しない")
    if dataset.manifest.examples_sha256 != source.examples_sha256:
        raise ValueError("学習 dataset の examples checksum が出どころと一致しない")
    _validate_split(dataset, split)
    if not split.train:
        raise ValueError("反実仮想 model の学習には train example が1件以上必要")
    metric_binding = build_metric_binding(
        (*feature_schema.metrics, *target_schema.metrics), metric_catalog
    )

    train_examples = tuple(sorted(split.train, key=_example_order))
    raw_rows = tuple(
        feature_row_v2(
            ObservedThermalInput.from_example_v2(example),
            recorded_trajectory(example, action_schema),
            feature_schema,
            action_schema,
        )
        for example in train_examples
    )
    means, scales, observed_counts, rows = _fit_preprocessing(raw_rows)
    plan_start = len(feature_schema.columns) - action_schema.steps * len(ZONE_ORDER)

    outputs: list[RidgeOutput] = []
    for horizon in target_schema.horizons_ms:
        # 因果の mask: horizon より後の step の計画 action の列は落として当てはめる（0079 §2.3）
        kept = plan_start + causal_plan_steps(action_schema, horizon) * len(ZONE_ORDER)
        for metric in target_schema.metrics:
            selected_rows: list[tuple[float, ...]] = []
            labels: list[float] = []
            for example, row in zip(train_examples, rows, strict=True):
                target = next(item for item in example.targets if item.horizon_ms == horizon)
                value = target.values[metric]
                if target.quality[metric] is not Quality.OK or value is None:
                    continue
                selected_rows.append(row[:kept])
                labels.append(value)
            if not labels:
                raise ValueError(
                    f"観測済み label が無いため学習できない: horizon={horizon}, metric={metric}"
                )
            intercept, coefficients = _fit_ridge(
                tuple(selected_rows), tuple(labels), ridge_lambda=training.ridge_lambda
            )
            outputs.append(
                RidgeOutput(
                    horizon_ms=horizon,
                    metric=metric,
                    intercept=intercept,
                    coefficients=coefficients + (0.0,) * (len(feature_schema.columns) - kept),
                    training_samples=len(labels),
                )
            )

    payload = RidgeModelPayload(
        feature_means=means,
        feature_scales=scales,
        feature_observed_counts=observed_counts,
        outputs=tuple(outputs),
    )
    training_data = TrainingDataProvenanceV2(
        dataset_alias=source.artifact_id,
        manifest_sha256=manifest_sha256,
        examples_sha256=dataset.manifest.examples_sha256,
        source_runs=tuple(
            sorted(
                (
                    TrainingSourceProvenance(run_id=run.run_id, source_sha256=run.source_sha256)
                    for run in dataset.manifest.source_runs
                ),
                key=lambda run: run.run_id,
            )
        ),
        split_sha256=split_sha256(split),
        window=TrainingWindows(
            train=_window(split.train),
            validation=_window(split.validation) if split.validation else None,
            test=_window(split.test) if split.test else None,
        ),
        train_example_count=len(train_examples),
        action_variation_summary=_action_variation(train_examples),
    )
    return CounterfactualTrainedModel(
        model_id=training.model_id,
        model_version=training.model_version,
        created_at=training.created_at,
        authority_compatibility=training.authority_compatibility,
        training_data=training_data,
        metric_binding=metric_binding,
        calibration_binding=CalibrationBinding(
            sha256=calibration_digest(metric_binding.entries, training.calibration_offsets_c)
        ),
        hyperparameters=RidgeHyperparameters(ridge_lambda=training.ridge_lambda),
        code_commit=training.code_commit,
        feature_schema=feature_schema,
        target_schema=target_schema,
        action_schema=action_schema,
        payload=payload,
    )


def recorded_trajectory(
    example: DatasetExampleV2, action_schema: ThermalActionSchema
) -> ActionTrajectory:
    """Dataset v2 に記録した action 列（係数の当てはめに使う。0084 §2.1 の表）。"""
    return ActionTrajectory(
        step_ms=action_schema.step_ms,
        demands=tuple(step.effective_demand for step in example.action_steps),
    )


def _window(examples: tuple[DatasetExampleV2, ...]) -> TimeWindow:
    return TimeWindow(
        start_ms=min(example.history_start_ms for example in examples),
        end_ms=max(example.label_end_ms for example in examples),
    )


def _action_variation(
    examples: tuple[DatasetExampleV2, ...],
) -> PerZone[ZoneActionVariation]:
    counts = {zone: [0, 0] for zone in ZONE_ORDER}
    for example in examples:
        sequence = (
            example.prior_action.effective_demand,
            *(step.effective_demand for step in example.action_steps),
        )
        for before, after in pairwise(sequence):
            for zone in ZONE_ORDER:
                counts[zone][0] += 1
                if before.get(zone) != after.get(zone):
                    counts[zone][1] += 1
    variation = {
        zone: ZoneActionVariation(transitions=total, changed=changed)
        for zone, (total, changed) in counts.items()
    }
    return PerZone(
        front=variation[ZONE_ORDER[0]],
        rear=variation[ZONE_ORDER[1]],
        top=variation[ZONE_ORDER[2]],
    )
