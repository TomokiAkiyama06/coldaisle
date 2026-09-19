"""Versioned observational Thermal Model contract and deterministic baseline tests (#84)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import ValidationError

import coldaisle.control.model.dataset as dataset_module
import coldaisle.control.model.training as training_module
from coldaisle.control.model.dataset import (
    ActionContext,
    ActionZone,
    DatasetExample,
    DatasetManifest,
    DatasetSourceKind,
    DatasetSpec,
    DatasetSplit,
    SourceRun,
    TargetFrame,
    ThermalDataset,
    WindowFrame,
    examples_jsonl_bytes,
    examples_sha256,
)
from coldaisle.control.model.thermal import (
    FEATURE_SCHEMA_VERSION,
    MAX_ARTIFACT_BYTES,
    MAX_FEATURE_COLUMNS,
    MAX_SOURCE_RUNS,
    MAX_WINDOW_FRAMES,
    TARGET_SCHEMA_VERSION,
    ArtifactVerification,
    InferenceCapability,
    ObservedFanAction,
    ObservedThermalInput,
    OfflineRidgeThermalModel,
    RidgeThermalModel,
    ThermalFeatureSchema,
    ThermalModelArtifact,
    ThermalRegistryMetadata,
    canonical_artifact_bytes,
    canonical_sha256,
    expectation_from_registry_metadata,
    raw_feature_values,
    registry_metadata,
    registry_metadata_json_bytes,
    replay_predictions,
)
from coldaisle.control.model.training import (
    IneffectiveRidgeLambdaError,
    RidgeTrainingSpec,
    VerifiedTrainingDatasetArtifact,
    _fit_ridge,
    train_ridge_baseline,
    verify_training_dataset_artifact,
)
from coldaisle.control.schema import (
    AuthorityStage,
    ControllerKind,
    OperatingMode,
    PerZone,
    SafetyState,
)
from coldaisle.store.models import Quality

RUN_ID = "run-00000000000000000000000000000001"
SOURCE_ID = "source-00000000000000000000000000000001"
DATASET_ID = "dataset-00000000000000000000000000000001"
SHA_A = "a" * 64
SHA_B = "b" * 64
FEATURES = ("air.front_intake", "gpu.0.core")
TARGETS = ("gpu.0.core", "cpu.package")
ACTIONS = (2_000, 6_000, 10_000, 20_000, 30_000)


def training_spec() -> RidgeTrainingSpec:
    return RidgeTrainingSpec(
        model_id="rack-thermal",
        model_version="0.1.0",
        created_at="2026-09-18T10:00:00+09:00",
        ridge_lambda=0.25,
        code_commit="0123456789abcdef",
    )


def training_source(
    dataset: ThermalDataset,
    artifact_id: str = DATASET_ID,
):
    with TemporaryDirectory() as temporary:
        artifact_dir = Path(temporary) / artifact_id
        artifact_dir.mkdir()
        manifest_path = artifact_dir / "manifest.json"
        examples_path = artifact_dir / "examples.jsonl"
        manifest_path.write_bytes(
            (dataset.manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
        )
        examples_path.write_bytes(examples_jsonl_bytes(dataset.examples))
        return verify_training_dataset_artifact(dataset, manifest_path, examples_path)


def corrupted_training_source(dataset: ThermalDataset):
    source = training_source(thermal_dataset())
    object.__setattr__(source, "_dataset", dataset)
    return source


class _TestVerifiedArtifact:
    def __init__(self, *, metadata: object, payload: bytes) -> None:
        self.metadata = metadata
        self.payload = payload


def _registry_module_for_test() -> ModuleType:
    existing = sys.modules.get("coldaisle.control.model_registry")
    if existing is not None:
        return existing
    registry_module = ModuleType("coldaisle.control.model_registry")
    registry_module.VerifiedArtifact = _TestVerifiedArtifact  # type: ignore[attr-defined]
    sys.modules[registry_module.__name__] = registry_module
    return registry_module


def load_registry_model(payload: bytes, *, expected):
    registry_module = _registry_module_for_test()
    verified_type = registry_module.VerifiedArtifact  # type: ignore[attr-defined]
    metadata = expected.registry_metadata
    registry_metadata_type = getattr(registry_module, "ArtifactMetadata", None)
    if registry_metadata_type is not None:
        metadata = registry_metadata_type.model_validate_json(metadata.model_dump_json())
    verified = verified_type(metadata=metadata, payload=payload)
    return RidgeThermalModel.from_verified_artifact(
        verified,
        authority_stage=expected.authority_stage,
    )


def source_run() -> SourceRun:
    return SourceRun(
        run_id=RUN_ID,
        kind=DatasetSourceKind.REPLAY,
        start_ms=0,
        end_ms=50_000,
        source_refs=(SOURCE_ID,),
        source_sha256=SHA_A,
    )


def dataset_spec() -> DatasetSpec:
    return DatasetSpec(
        window_ms=1_000,
        sample_period_ms=1_000,
        horizons_ms=(1_000, 2_000),
        target_tolerance_ms=0,
        stale_after_ms=5_000,
        feature_metrics=FEATURES,
        target_metrics=TARGETS,
    )


def action(value: float) -> PerZone[ActionZone]:
    return PerZone(
        front=ActionZone(
            requested_demand=value,
            effective_demand=value,
            bound_by="requested",
            controller_reason="synthetic",
            override_reasons=(),
        ),
        rear=ActionZone(
            requested_demand=value + 0.05,
            effective_demand=value + 0.05,
            bound_by="requested",
            controller_reason="synthetic",
            override_reasons=(),
        ),
        top=ActionZone(
            requested_demand=value + 0.1,
            effective_demand=value + 0.1,
            bound_by="requested",
            controller_reason="synthetic",
            override_reasons=(),
        ),
    )


def window_frame(ts_ms: int, index: int, frame_index: int) -> WindowFrame:
    values: dict[str, float | None] = {
        FEATURES[0]: 20.0 + index + frame_index,
        FEATURES[1]: 40.0 + index * 2 + frame_index,
    }
    source_times: dict[str, int | None] = {metric: ts_ms for metric in FEATURES}
    qualities: dict[str, Quality | None] = {metric: Quality.OK for metric in FEATURES}
    missing = {metric: False for metric in FEATURES}
    stale = {metric: False for metric in FEATURES}
    if frame_index == 0 and index == 0:
        values[FEATURES[1]] = None
        source_times[FEATURES[1]] = None
        qualities[FEATURES[1]] = None
        missing[FEATURES[1]] = True
    elif frame_index == 0 and index == 1:
        qualities[FEATURES[0]] = Quality.STALE
        stale[FEATURES[0]] = True
    elif frame_index == 0 and index == 2:
        source_times[FEATURES[0]] = 0
        qualities[FEATURES[0]] = Quality.SUSPECT
        stale[FEATURES[0]] = True
    elif frame_index == 0 and index == 3:
        values[FEATURES[1]] = None
        source_times[FEATURES[1]] = 0
        qualities[FEATURES[1]] = Quality.MISSING
        missing[FEATURES[1]] = True
        stale[FEATURES[1]] = True
    return WindowFrame(
        ts_ms=ts_ms,
        values=values,
        source_ts_ms=source_times,
        quality=qualities,
        missing_mask=missing,
        stale_mask=stale,
    )


def example(index: int, action_ts_ms: int) -> DatasetExample:
    targets = tuple(
        TargetFrame(
            horizon_ms=horizon,
            expected_ts_ms=action_ts_ms + horizon,
            values={
                metric: 50.0 + index * 3 + horizon / 1_000 + metric_index
                for metric_index, metric in enumerate(TARGETS)
            },
            source_ts_ms={metric: action_ts_ms + horizon for metric in TARGETS},
            quality={metric: Quality.OK for metric in TARGETS},
            missing_mask={metric: False for metric in TARGETS},
        )
        for horizon in (1_000, 2_000)
    )
    return DatasetExample(
        example_id=f"{RUN_ID}:{action_ts_ms}:{index}",
        source_run_id=RUN_ID,
        history_start_ms=action_ts_ms - 1_000,
        action_ts_ms=action_ts_ms,
        label_end_ms=action_ts_ms + 2_000,
        control_tick_id=index,
        control_schema_version=1,
        window=(
            window_frame(action_ts_ms - 1_000, index, 0),
            window_frame(action_ts_ms, index, 1),
        ),
        action=action(0.1 + index * 0.1),
        context=ActionContext(
            operating_mode=OperatingMode.AUTO,
            authority_stage=AuthorityStage.SHADOW,
            active_controller=ControllerKind.FALLBACK,
            safety_state=SafetyState.NORMAL,
            fallback_active=True,
            supervisor_policy=None,
            workload_regime=None,
            regime_confidence=None,
            fault_codes=(),
        ),
        targets=targets,
    )


def thermal_dataset(examples: tuple[DatasetExample, ...] | None = None) -> ThermalDataset:
    items = (
        tuple(example(index, action_ms) for index, action_ms in enumerate(ACTIONS))
        if examples is None
        else examples
    )
    return ThermalDataset(
        manifest=DatasetManifest(
            spec=dataset_spec(),
            source_runs=(source_run(),),
            telemetry_sha256=SHA_A,
            control_trace_sha256=SHA_B,
            examples_sha256=examples_sha256(items),
            example_count=len(items),
        ),
        examples=items,
    )


def temporal_split(dataset: ThermalDataset) -> DatasetSplit:
    return DatasetSplit(
        train=dataset.examples[:3],
        validation=dataset.examples[3:4],
        test=dataset.examples[4:],
        purged=(),
    )


def trained_artifact() -> ThermalModelArtifact:
    dataset = thermal_dataset()
    return train_ridge_baseline(training_source(dataset), temporal_split(dataset), training_spec())


def test_contract_is_multi_horizon_multi_output_and_effective_action_only():
    dataset = thermal_dataset()
    artifact = train_ridge_baseline(
        training_source(dataset), temporal_split(dataset), training_spec()
    )
    observed = ObservedThermalInput.from_example(dataset.examples[0])
    raw = raw_feature_values(observed, artifact.feature_schema)

    assert artifact.target_schema.horizons_ms == (1_000, 2_000)
    assert artifact.target_schema.metrics == TARGETS
    assert len(artifact.payload.outputs) == 4
    assert artifact.manifest.capability is InferenceCapability.OBSERVATIONAL_REPLAY
    assert artifact.manifest.authority_compatibility == (AuthorityStage.SHADOW,)
    assert artifact.feature_schema.columns[-3:] == (
        "action.front.effective_demand",
        "action.rear.effective_demand",
        "action.top.effective_demand",
    )
    assert all("requested" not in column for column in artifact.feature_schema.columns)
    assert raw[-3:] == pytest.approx((0.1, 0.15, 0.2))
    requested_only = dataset.examples[0].model_copy(
        update={
            "action": dataset.examples[0].action.model_copy(
                update={
                    "front": dataset.examples[0].action.front.model_copy(
                        update={"requested_demand": 0.9}
                    )
                }
            )
        }
    )
    assert ObservedThermalInput.from_example(requested_only) == observed
    with pytest.raises(ValidationError, match="extra"):
        ObservedFanAction.model_validate({"effective_demand": 0.5, "requested_demand": 0.5})


def test_missing_stale_and_suspect_values_are_mean_imputed_with_separate_masks():
    artifact = trained_artifact()
    columns = artifact.feature_schema.columns
    missing_value = columns.index("window[-1000].gpu.0.core.value")
    stale_value = columns.index("window[-1000].air.front_intake.value")
    suspect_mask = columns.index("window[-1000].gpu.0.core.suspect")

    assert artifact.payload.feature_observed_counts[missing_value] == 2
    assert artifact.payload.feature_observed_counts[stale_value] == 1
    assert artifact.payload.feature_observed_counts[suspect_mask] == 3
    observed = ObservedThermalInput.from_example(thermal_dataset().examples[2])
    assert observed.window[0].stale_mask[FEATURES[0]] is True
    assert observed.window[0].suspect_mask[FEATURES[0]] is True

    missing_and_stale = ObservedThermalInput.from_example(thermal_dataset().examples[3])
    assert missing_and_stale.window[0].missing_mask[FEATURES[1]] is True
    assert missing_and_stale.window[0].stale_mask[FEATURES[1]] is True


def test_valueless_suspect_cell_from_the_dataset_is_treated_as_missing():
    """datasetは`inf`等をvalue=null・quality=suspect・missing_mask=trueで持つ（決定記録 0031）。

    推論入力ではmissingとして扱い、missingとsuspectを同時に立てない。
    """
    source = thermal_dataset().examples[0]
    frame = source.window[1]
    valueless_suspect = frame.model_copy(
        update={
            "values": {**frame.values, FEATURES[0]: None},
            "quality": {**frame.quality, FEATURES[0]: Quality.SUSPECT},
            "missing_mask": {**frame.missing_mask, FEATURES[0]: True},
        }
    )
    # model_copyは検証しないので、dataset schemaとして通ることを確かめ直す
    valueless_suspect = WindowFrame.model_validate_json(valueless_suspect.model_dump_json())
    changed = source.model_copy(update={"window": (source.window[0], valueless_suspect)})

    observed = ObservedThermalInput.from_example(changed)

    assert observed.window[1].values[FEATURES[0]] is None
    assert observed.window[1].missing_mask[FEATURES[0]] is True
    assert observed.window[1].suspect_mask[FEATURES[0]] is False
    prediction = OfflineRidgeThermalModel.from_artifact(trained_artifact()).predict(observed)
    assert prediction.targets


def test_training_and_artifact_bytes_are_deterministic():
    dataset = thermal_dataset()
    split = temporal_split(dataset)

    source = training_source(dataset)
    first = train_ridge_baseline(source, split, training_spec())
    second = train_ridge_baseline(source, split, training_spec())

    assert first == second
    assert canonical_artifact_bytes(first) == canonical_artifact_bytes(second)

    reordered = split.model_copy(update={"train": tuple(reversed(split.train))})
    third = train_ridge_baseline(source, reordered, training_spec())
    assert canonical_artifact_bytes(first) == canonical_artifact_bytes(third)


def test_head_specific_ridge_centers_selected_features_for_the_intercept():
    intercept, coefficients = _fit_ridge(
        ((0.0,), (2.0,)),
        (0.0, 2.0),
        ridge_lambda=2.0,
    )

    assert coefficients == pytest.approx((0.5,))
    assert intercept == pytest.approx(0.5)


def test_ridge_lambda_below_gram_diagonal_ulp_fails_with_minimum_effective_lambda():
    # Two identical normalized columns make X^T X singular; 1e-20 rounds away on a diagonal of 2.
    rows = ((-1.0, -1.0), (1.0, 1.0))
    labels = (0.0, 2.0)

    with pytest.raises(IneffectiveRidgeLambdaError) as excinfo:
        _fit_ridge(rows, labels, ridge_lambda=1e-20)

    minimum = excinfo.value.minimum_effective_lambda
    assert excinfo.value.ridge_lambda == 1e-20
    assert 1e-20 < minimum < 1e-12
    assert "minimum_effective_lambda" in str(excinfo.value)
    # The reported minimum is actually sufficient: the same rows then solve without error.
    intercept, coefficients = _fit_ridge(rows, labels, ridge_lambda=minimum)
    assert all(map(math.isfinite, (intercept, *coefficients)))


def test_stale_threshold_is_part_of_feature_schema_identity():
    original = ThermalFeatureSchema.from_dataset_spec(dataset_spec())
    changed = ThermalFeatureSchema.from_dataset_spec(
        dataset_spec().model_copy(update={"stale_after_ms": 6_000})
    )

    assert original.stale_after_ms == 5_000
    assert canonical_sha256(original) != canonical_sha256(changed)


def test_validation_and_test_values_do_not_affect_fitted_payload():
    original = thermal_dataset()
    changed_examples = list(original.examples)
    for index in (3, 4):
        target = changed_examples[index].targets[0]
        changed_target = target.model_copy(
            update={"values": {metric: 9_000.0 for metric in TARGETS}}
        )
        latest = changed_examples[index].window[-1]
        changed_window = latest.model_copy(
            update={
                "values": {metric: 8_000.0 for metric in FEATURES},
            }
        )
        changed_action = changed_examples[index].action.model_copy(
            update={
                "front": changed_examples[index].action.front.model_copy(
                    update={"effective_demand": 0.99}
                )
            }
        )
        changed_examples[index] = changed_examples[index].model_copy(
            update={
                "window": (changed_examples[index].window[0], changed_window),
                "action": changed_action,
                "targets": (changed_target, changed_examples[index].targets[1]),
            }
        )
    changed = thermal_dataset(tuple(changed_examples))

    original_artifact = train_ridge_baseline(
        training_source(original), temporal_split(original), training_spec()
    )
    changed_artifact = train_ridge_baseline(
        training_source(changed), temporal_split(changed), training_spec()
    )

    assert changed_artifact.payload == original_artifact.payload
    assert (
        changed_artifact.manifest.training_examples_sha256
        != original_artifact.manifest.training_examples_sha256
    )


def test_reversed_or_overlapping_split_is_rejected_before_fit():
    dataset = thermal_dataset()
    broken = DatasetSplit(
        train=(dataset.examples[1],),
        validation=(dataset.examples[0],),
        test=dataset.examples[2:],
        purged=(),
    )

    with pytest.raises(ValueError, match="重なるか時系列順でない"):
        train_ridge_baseline(training_source(dataset), broken, training_spec())


def test_zero_observed_labels_for_any_head_fail_closed():
    dataset = thermal_dataset()
    changed_examples: list[DatasetExample] = []
    for item in dataset.examples:
        if item not in dataset.examples[:3]:
            changed_examples.append(item)
            continue
        first = item.targets[0]
        missing = first.model_copy(
            update={
                "values": {**first.values, TARGETS[0]: None},
                "quality": {**first.quality, TARGETS[0]: Quality.MISSING},
                "missing_mask": {**first.missing_mask, TARGETS[0]: True},
            }
        )
        changed_examples.append(item.model_copy(update={"targets": (missing, item.targets[1])}))
    changed = thermal_dataset(tuple(changed_examples))

    with pytest.raises(ValueError, match="観測済みlabelが無い"):
        train_ridge_baseline(training_source(changed), temporal_split(changed), training_spec())


def test_registry_bytes_round_trip_and_replay_are_deterministic_and_read_only():
    dataset = thermal_dataset()
    artifact = train_ridge_baseline(
        training_source(dataset), temporal_split(dataset), training_spec()
    )
    artifact_bytes = canonical_artifact_bytes(artifact)
    metadata = registry_metadata(artifact, artifact_bytes)
    model = load_registry_model(
        artifact_bytes,
        expected=expectation_from_registry_metadata(
            metadata,
            authority_stage=AuthorityStage.SHADOW,
        ),
    )

    first = replay_predictions(model, dataset.examples)
    second = replay_predictions(model, dataset.examples)

    assert first == second
    assert tuple(target.horizon_ms for target in first[0].targets) == (1_000, 2_000)
    assert all(set(target.values) == set(TARGETS) for target in first[0].targets)
    assert first[0].uncertainty is None
    assert first[0].artifact_verification is ArtifactVerification.REGISTRY_VERIFIED
    prediction_json = first[0].model_dump_json()
    assert "pwm" not in prediction_json
    assert "demand" not in prediction_json
    assert metadata.kind == "thermal_model"
    assert metadata.artifact_format == "json"
    assert metadata.training_dataset_version == DATASET_ID
    assert metadata.feature_schema_version == FEATURE_SCHEMA_VERSION
    assert metadata.target_schema_version == TARGET_SCHEMA_VERSION
    assert metadata.sha256 == hashlib.sha256(artifact_bytes).hexdigest()


def test_replay_resource_and_shape_preflight_never_calls_predict(monkeypatch):
    dataset = thermal_dataset()
    model = OfflineRidgeThermalModel.from_artifact(trained_artifact())
    calls = 0

    def unexpected_predict(_observed):
        nonlocal calls
        calls += 1
        raise AssertionError("predict must not run after Replay preflight failure")

    monkeypatch.setattr(model, "predict", unexpected_predict)
    monkeypatch.setattr("coldaisle.control.model.thermal.MAX_REPLAY_WORK_UNITS", 1)
    with pytest.raises(ValueError, match="演算量"):
        replay_predictions(model, dataset.examples)
    assert calls == 0

    monkeypatch.setattr("coldaisle.control.model.thermal.MAX_REPLAY_WORK_UNITS", 10**9)
    malformed = dataset.examples[0].model_copy(update={"window": dataset.examples[0].window[:1]})
    with pytest.raises(ValueError, match="window frame数"):
        replay_predictions(model, (malformed,))
    assert calls == 0

    huge_values = {f"metric-{index}": 1.0 for index in range(10_000)}
    huge_frame = dataset.examples[0].window[0].model_copy(update={"values": huge_values})
    huge_mapping = dataset.examples[0].model_copy(
        update={"window": (huge_frame, *dataset.examples[0].window[1:])}
    )
    with pytest.raises(ValueError, match="metric数"):
        replay_predictions(model, (huge_mapping,))
    assert calls == 0


def test_loader_rejects_registry_mismatch_pickle_and_payload_tampering():
    artifact = trained_artifact()
    artifact_bytes = canonical_artifact_bytes(artifact)
    metadata = registry_metadata(artifact, artifact_bytes)
    expected = expectation_from_registry_metadata(
        metadata,
        authority_stage=AuthorityStage.SHADOW,
    )

    with pytest.raises(ValueError, match="checksum"):
        load_registry_model(artifact_bytes + b" ", expected=expected)

    pickle = b"\x80\x04K*."
    pickle_metadata = metadata.model_copy(update={"sha256": hashlib.sha256(pickle).hexdigest()})
    pickle_expectation = expected.model_copy(update={"registry_metadata": pickle_metadata})
    with pytest.raises(ValueError, match="pickle"):
        load_registry_model(pickle, expected=pickle_expectation)

    tampered = artifact.model_dump(mode="json")
    tampered["payload"]["outputs"][0]["intercept"] += 1.0
    tampered_bytes = json.dumps(tampered, sort_keys=True).encode()
    tampered_metadata = metadata.model_copy(
        update={"sha256": hashlib.sha256(tampered_bytes).hexdigest()}
    )
    tampered_expectation = expected.model_copy(update={"registry_metadata": tampered_metadata})
    with pytest.raises(ValidationError, match="payload checksum"):
        load_registry_model(tampered_bytes, expected=tampered_expectation)


def test_loader_and_registry_adapter_require_canonical_exact_bytes():
    artifact = trained_artifact()
    canonical = canonical_artifact_bytes(artifact)
    metadata = registry_metadata(artifact, canonical)
    pretty = (json.dumps(artifact.model_dump(mode="json"), indent=2) + "\n").encode()
    pretty_metadata = metadata.model_copy(update={"sha256": hashlib.sha256(pretty).hexdigest()})
    expected = expectation_from_registry_metadata(
        pretty_metadata,
        authority_stage=AuthorityStage.SHADOW,
    )

    with pytest.raises(ValueError, match="canonical"):
        load_registry_model(pretty, expected=expected)
    with pytest.raises(ValueError, match="canonical"):
        registry_metadata(artifact, pretty)

    duplicate = canonical.replace(
        b'"schema_name":"coldaisle.thermal_model"',
        b'"schema_name":"coldaisle.thermal_model","schema_name":"coldaisle.thermal_model"',
        1,
    )
    duplicate_metadata = metadata.model_copy(
        update={"sha256": hashlib.sha256(duplicate).hexdigest()}
    )
    duplicate_expected = expectation_from_registry_metadata(
        duplicate_metadata,
        authority_stage=AuthorityStage.SHADOW,
    )
    with pytest.raises(ValueError, match="canonical"):
        load_registry_model(duplicate, expected=duplicate_expected)


def test_runtime_revalidates_mutated_or_bypassed_observations():
    dataset = thermal_dataset()
    model = OfflineRidgeThermalModel.from_artifact(trained_artifact())
    mutated = ObservedThermalInput.from_example(dataset.examples[0])
    mutated.window[0].missing_mask[FEATURES[0]] = True
    with pytest.raises(ValidationError, match="missing mask"):
        model.predict(mutated)

    aged = ObservedThermalInput.from_example(dataset.examples[2])
    first = aged.window[0]
    bad_stale = first.model_copy(update={"stale_mask": {**first.stale_mask, FEATURES[0]: False}})
    bypassed = aged.model_copy(update={"window": (bad_stale, aged.window[1])})
    with pytest.raises(ValueError, match="stale mask"):
        model.predict(bypassed)

    future = first.model_copy(
        update={"source_ts_ms": {**first.source_ts_ms, FEATURES[0]: first.ts_ms + 1}}
    )
    future_input = aged.model_copy(update={"window": (future, aged.window[1])})
    with pytest.raises(ValidationError, match="未来"):
        model.predict(future_input)


def test_artifact_and_training_resource_limits_fail_before_expansion(monkeypatch):
    huge = dataset_spec().model_copy(update={"window_ms": 10**18, "sample_period_ms": 1})
    with pytest.raises(ValueError, match="frame数"):
        ThermalFeatureSchema.from_dataset_spec(huge)

    artifact = trained_artifact()
    body = canonical_artifact_bytes(artifact)
    metadata = registry_metadata(artifact, body)
    expected = expectation_from_registry_metadata(
        metadata,
        authority_stage=AuthorityStage.SHADOW,
    )
    with pytest.raises(ValueError, match="size上限"):
        load_registry_model(b" " * (MAX_ARTIFACT_BYTES + 1), expected=expected)

    dataset = thermal_dataset()
    monkeypatch.setattr(training_module, "MAX_TRAINING_CELLS", 1)
    with pytest.raises(ValueError, match="feature cell数"):
        train_ridge_baseline(training_source(dataset), temporal_split(dataset), training_spec())


def test_ridge_work_budget_rejects_before_fit(monkeypatch):
    dataset = thermal_dataset()
    split = temporal_split(dataset)
    feature_count = len(ThermalFeatureSchema.from_dataset_spec(dataset.manifest.spec).columns)
    assert len(split.train) * feature_count <= training_module.MAX_TRAINING_CELLS

    monkeypatch.setattr(training_module, "MAX_RIDGE_WORK_UNITS", 1)

    def unexpected_fit(*args, **kwargs):
        raise AssertionError("fit must not run beyond the work budget")

    monkeypatch.setattr(training_module, "_fit_ridge", unexpected_fit)
    with pytest.raises(ValueError, match="ridge演算量"):
        train_ridge_baseline(training_source(dataset), split, training_spec())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ridge_lambda", -1.0),
        ("ridge_lambda", float("nan")),
        ("model_id", "INVALID MODEL"),
    ],
)
def test_training_revalidates_spec_and_bounds_non_train_examples(monkeypatch, field, value):
    dataset = thermal_dataset()
    split = temporal_split(dataset)
    invalid = training_spec().model_copy(update={field: value})

    with pytest.raises(ValidationError):
        train_ridge_baseline(training_source(dataset), split, invalid)

    monkeypatch.setattr(training_module, "MAX_DATASET_EXAMPLES", len(dataset.examples) - 1)
    with pytest.raises(ValueError, match="example総数"):
        train_ridge_baseline(training_source(dataset), split, training_spec())


def test_dataset_and_split_feature_and_target_cell_budgets(monkeypatch):
    dataset = thermal_dataset()
    split = temporal_split(dataset)
    feature_count = len(ThermalFeatureSchema.from_dataset_spec(dataset.manifest.spec).columns)
    output_count = len(dataset.manifest.spec.horizons_ms) * len(
        dataset.manifest.spec.target_metrics
    )
    repeated = split.model_copy(update={"purged": (*split.purged, dataset.examples[0])})

    monkeypatch.setattr(
        training_module,
        "MAX_DATASET_FEATURE_CELLS",
        len(dataset.examples) * feature_count,
    )
    with pytest.raises(ValueError, match="split feature cell総数"):
        train_ridge_baseline(training_source(dataset), repeated, training_spec())

    monkeypatch.setattr(training_module, "MAX_DATASET_FEATURE_CELLS", 10**9)
    monkeypatch.setattr(
        training_module,
        "MAX_DATASET_TARGET_CELLS",
        len(dataset.examples) * output_count,
    )
    with pytest.raises(ValueError, match="split target cell総数"):
        train_ridge_baseline(training_source(dataset), repeated, training_spec())

    monkeypatch.setattr(
        training_module,
        "MAX_DATASET_TARGET_CELLS",
        len(dataset.examples) * output_count - 1,
    )
    with pytest.raises(ValueError, match="dataset target cell総数"):
        train_ridge_baseline(training_source(dataset), split, training_spec())


@pytest.mark.parametrize("nested_field", ["override_reasons", "fault_codes"])
def test_training_bounds_nested_dataset_metadata_before_round_trip(nested_field):
    dataset = thermal_dataset()
    first = dataset.examples[0]
    oversized = ("reason",) * (training_module.MAX_METADATA_ITEMS_PER_EXAMPLE + 1)
    if nested_field == "override_reasons":
        changed_front = first.action.front.model_copy(update={nested_field: oversized})
        changed_action = first.action.model_copy(update={"front": changed_front})
        changed = first.model_copy(update={"action": changed_action})
    else:
        changed_context = first.context.model_copy(update={nested_field: oversized})
        changed = first.model_copy(update={"context": changed_context})
    changed_dataset = dataset.model_copy(update={"examples": (changed, *dataset.examples[1:])})

    with pytest.raises(ValueError, match="metadata item数"):
        train_ridge_baseline(
            corrupted_training_source(changed_dataset),
            temporal_split(dataset),
            training_spec(),
        )

    source = dataset.manifest.source_runs[0].model_copy(
        update={"source_refs": (SOURCE_ID,) * (training_module.MAX_SOURCE_REFS_PER_RUN + 1)}
    )
    manifest = dataset.manifest.model_copy(update={"source_runs": (source,)})
    changed_dataset = dataset.model_copy(update={"manifest": manifest})
    with pytest.raises(ValueError, match="source refs数"):
        train_ridge_baseline(
            corrupted_training_source(changed_dataset),
            temporal_split(dataset),
            training_spec(),
        )


def test_training_bounds_total_text_bytes_and_metric_names(monkeypatch):
    dataset = thermal_dataset()
    first = dataset.examples[0]
    shared_text = "x" * training_module.MAX_METADATA_TEXT_LENGTH
    changed_front = first.action.front.model_copy(update={"override_reasons": (shared_text,) * 4})
    changed_action = first.action.model_copy(update={"front": changed_front})
    changed = first.model_copy(update={"action": changed_action})
    repeated_examples = (changed,) * len(dataset.examples)
    repeated_dataset = dataset.model_copy(update={"examples": repeated_examples})
    repeated_split = temporal_split(dataset).model_copy(
        update={
            "train": (changed,),
            "validation": (),
            "test": (),
            "purged": (changed,) * (len(dataset.examples) - 1),
        }
    )
    monkeypatch.setattr(training_module, "MAX_DATASET_TEXT_BYTES", 1_000)
    with pytest.raises(ValueError, match="text総byte数"):
        train_ridge_baseline(
            corrupted_training_source(repeated_dataset), repeated_split, training_spec()
        )

    long_metric = f"metric.{('x' * 121)}"
    bad_spec = dataset.manifest.spec.model_copy(
        update={"feature_metrics": (long_metric, FEATURES[1])}
    )
    bad_manifest = dataset.manifest.model_copy(update={"spec": bad_spec})
    with pytest.raises(ValueError, match="metric名"):
        train_ridge_baseline(
            corrupted_training_source(dataset.model_copy(update={"manifest": bad_manifest})),
            temporal_split(dataset),
            training_spec(),
        )

    bad_values = dict(first.window[0].values)
    bad_values[long_metric] = bad_values.pop(FEATURES[0])
    bad_frame = first.window[0].model_copy(update={"values": bad_values})
    bad_example = first.model_copy(update={"window": (bad_frame, first.window[1])})
    bad_examples = (bad_example, *dataset.examples[1:])
    with pytest.raises(ValueError, match="feature metric名"):
        train_ridge_baseline(
            corrupted_training_source(dataset.model_copy(update={"examples": bad_examples})),
            temporal_split(dataset),
            training_spec(),
        )

    huge_time = 1 << 100_000
    huge_spec = dataset.manifest.spec.model_copy(update={"window_ms": huge_time})
    huge_manifest = dataset.manifest.model_copy(update={"spec": huge_spec})
    with pytest.raises(ValueError, match="spec時刻値"):
        train_ridge_baseline(
            corrupted_training_source(dataset.model_copy(update={"manifest": huge_manifest})),
            temporal_split(dataset),
            training_spec(),
        )


def test_dataset_checksum_streams_without_materializing_jsonl(monkeypatch):
    dataset = thermal_dataset()
    expected = dataset.manifest.examples_sha256

    def unexpected_join(*args, **kwargs):
        raise AssertionError("checksum must not materialize the complete JSONL artifact")

    monkeypatch.setattr(dataset_module, "examples_jsonl_bytes", unexpected_join)

    assert dataset_module.examples_sha256(dataset.examples) == expected
    assert train_ridge_baseline(training_source(dataset), temporal_split(dataset), training_spec())


def test_training_dataset_alias_is_derived_from_published_artifact(tmp_path):
    dataset = thermal_dataset()
    artifact_dir = tmp_path / DATASET_ID
    artifact_dir.mkdir()
    manifest_path = artifact_dir / "manifest.json"
    examples_path = artifact_dir / "examples.jsonl"
    manifest_path.write_bytes((dataset.manifest.model_dump_json(indent=2) + "\n").encode("utf-8"))
    examples_path.write_bytes(examples_jsonl_bytes(dataset.examples))

    source = verify_training_dataset_artifact(dataset, manifest_path, examples_path)
    artifact = train_ridge_baseline(source, temporal_split(dataset), training_spec())
    assert artifact.manifest.training_dataset_version == artifact_dir.name

    with pytest.raises(TypeError, match="published artifact"):
        VerifiedTrainingDatasetArtifact(
            artifact_id="dataset-00000000000000000000000000000002",
            dataset=dataset,
            manifest_sha256=source.manifest_sha256,
            examples_sha256=source.examples_sha256,
            _token=object(),
        )

    wrong_dir = tmp_path / "wrong-dataset-alias"
    wrong_dir.mkdir()
    wrong_manifest = wrong_dir / "manifest.json"
    wrong_examples = wrong_dir / "examples.jsonl"
    wrong_manifest.write_bytes(manifest_path.read_bytes())
    wrong_examples.write_bytes(examples_path.read_bytes())
    with pytest.raises(ValueError, match="directory名"):
        verify_training_dataset_artifact(dataset, wrong_manifest, wrong_examples)


def test_training_dataset_verifier_rejects_fifo_and_symlinks(tmp_path):
    dataset = thermal_dataset()

    fifo_dir = tmp_path / DATASET_ID
    fifo_dir.mkdir()
    os.mkfifo(fifo_dir / "manifest.json")
    (fifo_dir / "examples.jsonl").write_bytes(examples_jsonl_bytes(dataset.examples))
    with pytest.raises(ValueError, match="regular file"):
        verify_training_dataset_artifact(
            dataset,
            fifo_dir / "manifest.json",
            fifo_dir / "examples.jsonl",
        )

    (fifo_dir / "manifest.json").unlink()
    real_manifest = tmp_path / "real-manifest.json"
    real_manifest.write_bytes((dataset.manifest.model_dump_json(indent=2) + "\n").encode("utf-8"))
    (fifo_dir / "manifest.json").symlink_to(real_manifest)
    with pytest.raises(OSError):
        verify_training_dataset_artifact(
            dataset,
            fifo_dir / "manifest.json",
            fifo_dir / "examples.jsonl",
        )

    real_dir = tmp_path / "real-artifact"
    real_dir.mkdir()
    (real_dir / "manifest.json").write_bytes(real_manifest.read_bytes())
    (real_dir / "examples.jsonl").write_bytes(examples_jsonl_bytes(dataset.examples))
    symlink_root = tmp_path / "symlink-parent"
    symlink_root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        verify_training_dataset_artifact(
            dataset,
            symlink_root / DATASET_ID / "manifest.json",
            symlink_root / DATASET_ID / "examples.jsonl",
        )


def test_training_dataset_verifier_holds_one_parent_snapshot(tmp_path, monkeypatch):
    dataset = thermal_dataset()
    artifact_dir = tmp_path / DATASET_ID
    artifact_dir.mkdir()
    manifest_path = artifact_dir / "manifest.json"
    examples_path = artifact_dir / "examples.jsonl"
    manifest_path.write_bytes((dataset.manifest.model_dump_json(indent=2) + "\n").encode("utf-8"))
    examples_path.write_bytes(examples_jsonl_bytes(dataset.examples))
    original_read = training_module._read_regular_file_at

    def read_then_replace(directory_fd, name, *, max_bytes):
        result = original_read(directory_fd, name, max_bytes=max_bytes)
        moved_dir = tmp_path / "moved-artifact"
        artifact_dir.rename(moved_dir)
        artifact_dir.mkdir()
        (artifact_dir / "manifest.json").write_bytes(b"replacement")
        (artifact_dir / "examples.jsonl").write_bytes(b"replacement")
        return result

    monkeypatch.setattr(training_module, "_read_regular_file_at", read_then_replace)
    source = verify_training_dataset_artifact(dataset, manifest_path, examples_path)

    assert source.artifact_id == DATASET_ID
    assert source.examples_sha256 == dataset.manifest.examples_sha256


def test_runtime_and_artifact_containers_are_bounded_before_serialization():
    dataset = thermal_dataset()
    artifact = trained_artifact()
    oversized_payload = artifact.payload.model_copy(
        update={"feature_means": (0.0,) * (MAX_FEATURE_COLUMNS + 1)}
    )
    oversized_artifact = artifact.model_copy(update={"payload": oversized_payload})
    with pytest.raises(ValueError, match="normalization vector"):
        canonical_artifact_bytes(oversized_artifact)

    oversized_manifest = artifact.manifest.model_copy(
        update={"source_runs": artifact.manifest.source_runs * (MAX_SOURCE_RUNS + 1)}
    )
    with pytest.raises(ValueError, match="source run数"):
        OfflineRidgeThermalModel.from_artifact(
            artifact.model_copy(update={"manifest": oversized_manifest})
        )

    oversized_authority = artifact.manifest.model_copy(
        update={"authority_compatibility": (AuthorityStage.SHADOW,) * 1_000}
    )
    with pytest.raises(ValueError, match="SHADOW authority"):
        OfflineRidgeThermalModel.from_artifact(
            artifact.model_copy(update={"manifest": oversized_authority})
        )

    model = OfflineRidgeThermalModel.from_artifact(artifact)
    observed = ObservedThermalInput.from_example(dataset.examples[0])
    oversized_window = observed.model_copy(
        update={"window": (observed.window[0],) * (MAX_WINDOW_FRAMES + 1)}
    )
    with pytest.raises(ValueError, match="window frame数"):
        model.predict(oversized_window)

    extra_values = dict(observed.window[0].values)
    for index in range(65):
        extra_values[f"extra.{index}"] = 1.0
    oversized_frame = observed.window[0].model_copy(update={"values": extra_values})
    oversized_mapping = observed.model_copy(
        update={"window": (oversized_frame, observed.window[1])}
    )
    with pytest.raises(ValueError, match="metric数"):
        model.predict(oversized_mapping)

    long_metric = f"metric.{('x' * 121)}"
    long_values = dict(observed.window[0].values)
    long_values[long_metric] = long_values.pop(FEATURES[0])
    long_frame = observed.window[0].model_copy(update={"values": long_values})
    long_mapping = observed.model_copy(update={"window": (long_frame, observed.window[1])})
    with pytest.raises(ValueError, match="metric名"):
        model.predict(long_mapping)


def test_registry_constructor_is_closed_and_offline_model_is_explicit():
    artifact = trained_artifact()
    body = canonical_artifact_bytes(artifact)
    with pytest.raises(TypeError, match="VerifiedArtifact"):
        RidgeThermalModel(artifact)  # type: ignore[call-arg]
    metadata = registry_metadata(artifact, body)
    _registry_module_for_test()
    with pytest.raises(TypeError, match="nominal VerifiedArtifact"):
        RidgeThermalModel.from_verified_artifact(
            SimpleNamespace(metadata=metadata, payload=body),  # type: ignore[arg-type]
            authority_stage=AuthorityStage.SHADOW,
        )
    model = OfflineRidgeThermalModel.from_artifact(artifact)

    prediction = model.predict(ObservedThermalInput.from_example(thermal_dataset().examples[0]))

    assert prediction.artifact_sha256 == hashlib.sha256(body).hexdigest()
    assert prediction.artifact_verification is ArtifactVerification.OFFLINE_UNVERIFIED


def test_registry_metadata_rejects_huge_integer_before_serialization(monkeypatch):
    artifact = trained_artifact()
    body = canonical_artifact_bytes(artifact)
    registry_module = _registry_module_for_test()
    metadata = registry_metadata(artifact, body)
    registry_metadata_type = getattr(registry_module, "ArtifactMetadata", None)
    if registry_metadata_type is not None:
        # 実Registryの型へ正規の値で変換してから差し替える。巨大な整数を含めてJSONを経由すると、
        # 検証したいthermal側の拒否より先にint→strの桁数上限で落ちてしまう
        metadata = registry_metadata_type.model_validate_json(metadata.model_dump_json())
    metadata = metadata.model_copy(update={"hyperparameters": {"huge": 1 << 100_000}})
    verified_type = registry_module.VerifiedArtifact  # type: ignore[attr-defined]
    verified = verified_type(metadata=metadata, payload=body)

    def unexpected_serialization(*args, **kwargs):
        raise AssertionError("unbounded integer must be rejected before JSON serialization")

    monkeypatch.setattr(ThermalRegistryMetadata, "model_dump_json", unexpected_serialization)
    with pytest.raises(ValueError, match="int64"):
        RidgeThermalModel.from_verified_artifact(
            verified,
            authority_stage=AuthorityStage.SHADOW,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("training_dataset_version", "dataset-00000000000000000000000000000002"),
        ("source_runs", ("run-00000000000000000000000000000002",)),
        ("created_at", "2026-09-19T10:00:00+09:00"),
        ("code_commit", "1111111"),
        ("hyperparameters", {"ridge_lambda": 0.5}),
    ],
)
def test_loader_binds_registry_provenance_fields(field, value):
    artifact = trained_artifact()
    body = canonical_artifact_bytes(artifact)
    metadata = registry_metadata(artifact, body).model_copy(update={field: value})
    expected = expectation_from_registry_metadata(
        metadata,
        authority_stage=AuthorityStage.SHADOW,
    )

    with pytest.raises(ValueError, match="identity/provenance"):
        load_registry_model(body, expected=expected)


def test_unknown_fields_nonfinite_parameters_and_broader_authority_are_rejected():
    artifact = trained_artifact()
    unknown = artifact.model_dump(mode="json")
    unknown["python_class"] = "unsafe.Loader"
    with pytest.raises(ValidationError, match="extra"):
        ThermalModelArtifact.model_validate(unknown)

    nonfinite = artifact.model_dump(mode="python")
    nonfinite["payload"]["outputs"][0]["intercept"] = float("nan")
    with pytest.raises(ValidationError, match="finite number"):
        ThermalModelArtifact.model_validate(nonfinite)

    broader = artifact.model_dump(mode="python")
    broader["manifest"]["authority_compatibility"] = (
        AuthorityStage.SHADOW,
        AuthorityStage.LIMITED,
    )
    with pytest.raises(ValidationError):
        ThermalModelArtifact.model_validate(broader)


def test_runtime_expectation_is_bound_to_identity_schema_and_shadow():
    artifact = trained_artifact()
    artifact_bytes = canonical_artifact_bytes(artifact)
    metadata = registry_metadata(artifact, artifact_bytes)
    expected = expectation_from_registry_metadata(
        metadata,
        authority_stage=AuthorityStage.SHADOW,
    )

    wrong_metadata = metadata.model_copy(update={"version": "0.2.0"})
    wrong_identity = expected.model_copy(update={"registry_metadata": wrong_metadata})
    with pytest.raises(ValueError, match="identity/provenance"):
        load_registry_model(artifact_bytes, expected=wrong_identity)

    limited = expectation_from_registry_metadata(
        metadata,
        authority_stage=AuthorityStage.LIMITED,
    )
    with pytest.raises(ValueError, match="authority stage"):
        load_registry_model(artifact_bytes, expected=limited)

    forged_broader = metadata.model_copy(
        update={
            "authority_compatibility": (
                AuthorityStage.SHADOW,
                AuthorityStage.LIMITED,
            )
        }
    )
    forged_expectation = limited.model_copy(update={"registry_metadata": forged_broader})
    with pytest.raises(ValueError, match="authority"):
        load_registry_model(artifact_bytes, expected=forged_expectation)

    bridged = json.loads(registry_metadata_json_bytes(metadata))
    assert bridged["schema_version"] == 1
    assert bridged["kind"] == "thermal_model"
