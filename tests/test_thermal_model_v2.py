"""反実仮想 Thermal Model artifact v2・trainer・読み込み時の検査 L1〜L12。

#84 / 決定記録 0079 段 2 / 0084。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import coldaisle.control.model.counterfactual as counterfactual_module
import coldaisle.control.model.counterfactual_training as training_v2_module
import coldaisle.control.model_registry as registry_module
from coldaisle.clock import SimulatedClock
from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.model.confidence import (
    ConfidenceProfileSpec,
    MissingPatternCount,
    ResidualScale,
    SupportAxis,
    SupportCellCount,
    ValueRange,
)
from coldaisle.control.model.counterfactual import (
    ActionCellCount,
    ActionCellTransitionCount,
    ActionSupportV2,
    ActionTrajectory,
    AnchorTransitionSupport,
    ArtifactCheck,
    ConfidenceProfileV2,
    CounterfactualArtifactRejectedError,
    CounterfactualThermalModelArtifact,
    RegistryCounterfactualThermalModel,
    StepActionSupport,
    StepTransitionSupport,
    canonical_counterfactual_artifact_bytes,
    causal_plan_steps,
    counterfactual_registry_metadata,
    counterfactual_registry_metadata_json_bytes,
    hold_effective,
)
from coldaisle.control.model.counterfactual_training import (
    CounterfactualTrainedModel,
    CounterfactualTrainingSpec,
    VerifiedTrainingDatasetArtifactV2,
    assemble_counterfactual_artifact,
    recorded_trajectory,
    train_counterfactual_ridge,
    verify_training_dataset_artifact_v2,
)
from coldaisle.control.model.dataset import (
    ActionContext,
    ActionExclusionCounts,
    ActionStepV2,
    ActionZone,
    DatasetExampleV2,
    DatasetManifestV2,
    DatasetSourceKind,
    DatasetSpecV2,
    DatasetSplitV2,
    PriorAction,
    SourceRun,
    TargetFrame,
    ThermalDatasetV2,
    WindowFrame,
    examples_jsonl_bytes,
    examples_sha256,
    split_temporally_v2,
)
from coldaisle.control.model.thermal import (
    ArtifactVerification,
    InferenceCapability,
    ObservedThermalInput,
    RidgeThermalModel,
    ThermalTargetSchema,
    canonical_json_bytes,
    canonical_sha256,
)
from coldaisle.control.model_registry import (
    ApprovalAction,
    ArtifactAttestation,
    ArtifactKind,
    ArtifactMetadata,
    HumanApproval,
    ModelCompatibility,
    ModelRegistry,
    VerifiedArtifact,
    load_model_registry_limits,
)
from coldaisle.control.mpc.plan import ActionPlan
from coldaisle.control.schema import (
    AuthorityStage,
    ControllerKind,
    OperatingMode,
    PerZone,
    SafetyState,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store.models import Quality

RUN_ID = "run-00000000000000000000000000000002"
SOURCE_ID = "source-00000000000000000000000000000002"
DATASET_ID = "dataset-00000000000000000000000000000002"
SHA_A = "a" * 64
SHA_B = "b" * 64
CALIBRATION_OFFSETS: dict[str, float] = {
    "front_intake": 0.191,
    "room_temp": -0.31,
    "gpu_exhaust": 0.05,
}
"""学習時の較正の値。fixture の artifact で較正の掛かる metric は ``air.front_intake`` だけ。"""
RUNTIME = RuntimeCalibration.available(CALIBRATION_OFFSETS)
FIXTURE_CALIBRATION_BYTES = b'{"air.front_intake":0.191}\n'
"""fixture の artifact の digest の元（0096 §2.3）。``d.gpu_rise`` は2つに展開される。"""
FEATURES = ("air.front_intake", "gpu.0.core")
TARGETS = ("cpu.package", "d.gpu_rise")
HORIZONS = (1_000, 2_000)
STEP_MS = 1_000
STEPS = 2
ANCHOR_TICKS = range(2, 42)
VALIDATION_START_MS = 28_000
TEST_START_MS = 36_000
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
NOW_MS = 1_800_000_000_000

CATALOG = MetricCatalog.model_validate(
    {
        "metrics": {
            "air.front_intake": {"unit": "°C", "label": "Front intake"},
            "gpu.0.core": {"unit": "°C", "label": "GPU core"},
            "cpu.package": {"unit": "°C", "label": "CPU package"},
            "air.room": {"unit": "°C", "label": "Room"},
        },
        "derived": {
            "d.gpu_rise": {
                "unit": "°C",
                "label": "GPU rise",
                "minuend": "gpu.0.core",
                "subtrahend": "air.front_intake",
            }
        },
    }
)


# ---------------------------------------------------------------- 合成 Dataset v2（決定的）


def demands(tick: int) -> PerZone[float]:
    return PerZone(
        front=round(0.1 + 0.1 * ((tick * 3) % 7), 6),
        rear=round(0.2 + 0.05 * (tick % 5), 6),
        top=round(0.3 + 0.1 * ((tick * 2) % 4), 6),
    )


def gpu_core(ts_ms: int) -> float:
    return 50.0 + ((ts_ms // 1_000) % 9) * 0.7


def front_intake(ts_ms: int) -> float:
    return 20.0 + ((ts_ms // 1_000) % 5) * 0.3


def window_frame(ts_ms: int) -> WindowFrame:
    return WindowFrame(
        ts_ms=ts_ms,
        values={"air.front_intake": front_intake(ts_ms), "gpu.0.core": gpu_core(ts_ms)},
        source_ts_ms={metric: ts_ms for metric in FEATURES},
        quality={metric: Quality.OK for metric in FEATURES},
        missing_mask={metric: False for metric in FEATURES},
        stale_mask={metric: False for metric in FEATURES},
    )


def target_values(anchor_tick: int, horizon_ms: int) -> dict[str, float | None]:
    anchor_ms = anchor_tick * 1_000
    step0 = demands(anchor_tick)
    last = demands(anchor_tick + horizon_ms // STEP_MS - 1)
    return {
        "cpu.package": 40.0
        + 0.5 * gpu_core(anchor_ms)
        - 6.0 * step0.front
        - 3.0 * last.rear
        + horizon_ms / 1_000,
        "d.gpu_rise": 10.0 - 4.0 * step0.top + 0.2 * gpu_core(anchor_ms),
    }


def action_zone(value: float) -> ActionZone:
    return ActionZone(
        requested_demand=value,
        effective_demand=value,
        bound_by="requested",
        controller_reason="synthetic",
        override_reasons=(),
    )


def example(anchor_tick: int) -> DatasetExampleV2:
    anchor_ms = anchor_tick * 1_000
    current = demands(anchor_tick)
    return DatasetExampleV2(
        example_id=f"{RUN_ID}:{anchor_ms}",
        source_run_id=RUN_ID,
        history_start_ms=anchor_ms - 1_000,
        action_ts_ms=anchor_ms,
        label_end_ms=anchor_ms + HORIZONS[-1],
        control_tick_id=anchor_tick,
        control_schema_version=12,
        window=(window_frame(anchor_ms - 1_000), window_frame(anchor_ms)),
        action=PerZone(
            front=action_zone(current.front),
            rear=action_zone(current.rear),
            top=action_zone(current.top),
        ),
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
        prior_action=PriorAction(
            source_ts_ms=anchor_ms - 1_000,
            source_tick_id=anchor_tick - 1,
            effective_demand=demands(anchor_tick - 1),
        ),
        action_steps=tuple(
            ActionStepV2(
                step=step,
                ts_ms=anchor_ms + step * STEP_MS,
                source_ts_ms=anchor_ms + step * STEP_MS,
                source_tick_id=anchor_tick + step,
                effective_demand=demands(anchor_tick + step),
            )
            for step in range(STEPS)
        ),
        targets=tuple(
            TargetFrame(
                horizon_ms=horizon,
                expected_ts_ms=anchor_ms + horizon,
                values=target_values(anchor_tick, horizon),
                source_ts_ms={metric: anchor_ms + horizon for metric in TARGETS},
                quality={metric: Quality.OK for metric in TARGETS},
                missing_mask={metric: False for metric in TARGETS},
            )
            for horizon in HORIZONS
        ),
    )


def dataset_spec() -> DatasetSpecV2:
    return DatasetSpecV2(
        window_ms=1_000,
        sample_period_ms=1_000,
        horizons_ms=HORIZONS,
        target_tolerance_ms=0,
        stale_after_ms=5_000,
        feature_metrics=FEATURES,
        target_metrics=TARGETS,
        action_step_ms=STEP_MS,
        action_steps=STEPS,
        action_stale_after_ms=2_000,
    )


def thermal_dataset(examples: tuple[DatasetExampleV2, ...] | None = None) -> ThermalDatasetV2:
    items = tuple(example(tick) for tick in ANCHOR_TICKS) if examples is None else examples
    return ThermalDatasetV2(
        manifest=DatasetManifestV2(
            spec=dataset_spec(),
            source_runs=(
                SourceRun(
                    run_id=RUN_ID,
                    kind=DatasetSourceKind.REPLAY,
                    start_ms=0,
                    end_ms=100_000,
                    source_refs=(SOURCE_ID,),
                    source_sha256=SHA_A,
                ),
            ),
            telemetry_sha256=SHA_A,
            control_trace_sha256=SHA_B,
            examples_sha256=examples_sha256(items),
            example_count=len(items),
            excluded=ActionExclusionCounts(
                stale=0, discontinuity=0, in_step_change=0, restart=0, tick_id_gap=0
            ),
        ),
        examples=items,
    )


def split(dataset: ThermalDatasetV2) -> DatasetSplitV2:
    return split_temporally_v2(
        dataset.examples, validation_start_ms=VALIDATION_START_MS, test_start_ms=TEST_START_MS
    )


def published(dataset: ThermalDatasetV2, tmp_path: Path) -> VerifiedTrainingDatasetArtifactV2:
    directory = tmp_path / DATASET_ID
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_bytes(
        (dataset.manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    )
    (directory / "examples.jsonl").write_bytes(examples_jsonl_bytes(dataset.examples))
    return verify_training_dataset_artifact_v2(
        dataset, directory / "manifest.json", directory / "examples.jsonl"
    )


def training_spec(**updates: Any) -> CounterfactualTrainingSpec:
    values: dict[str, Any] = {
        "model_id": "rack-thermal-cf",
        "model_version": "0.1.0",
        "created_at": "2026-10-07T10:00:00+09:00",
        "ridge_lambda": 0.25,
        "authority_compatibility": (AuthorityStage.SHADOW,),
        "calibration_offsets_c": dict(CALIBRATION_OFFSETS),
        "code_commit": "0123456789abcdef",
    }
    values.update(updates)
    return CounterfactualTrainingSpec(**values)


def train(tmp_path: Path, **updates: Any) -> CounterfactualTrainedModel:
    dataset = thermal_dataset()
    return train_counterfactual_ridge(
        published(dataset, tmp_path),
        split(dataset),
        training_spec(**updates),
        metric_catalog=CATALOG,
    )


# ------------------------------------------------ 手で作る Profile v2（段 3 の生成の代わり）


def zone_ranges(low: float, high: float) -> PerZone[ValueRange]:
    return PerZone(
        front=ValueRange(source="fan.front", minimum=low, maximum=high, observed_count=1),
        rear=ValueRange(source="fan.rear", minimum=low, maximum=high, observed_count=1),
        top=ValueRange(source="fan.top", minimum=low, maximum=high, observed_count=1),
    )


def profile_for(trained: CounterfactualTrainedModel) -> ConfidenceProfileV2:
    cell = (0, 0, 0)
    transition = ActionCellTransitionCount(source=cell, target=cell, count=1)
    return ConfidenceProfileV2(
        binding=trained.profile_binding(),
        anchor_action_rule="hold_effective",
        spec=ConfidenceProfileSpec(
            support_axes=(
                SupportAxis(source="fan.front", edges=(0.5,)),
                SupportAxis(source="fan.rear", edges=(0.5,)),
                SupportAxis(source="fan.top", edges=(0.5,)),
            ),
            residual_scale_floor=0.1,
        ),
        train_example_count=trained.training_data.train_example_count,
        feature_ranges=tuple(
            ValueRange(source=metric, minimum=0.0, maximum=100.0, observed_count=1)
            for metric in trained.feature_schema.metrics
        ),
        fan_ranges=zone_ranges(0.0, 1.0),
        missing_patterns=(MissingPatternCount(unavailable_metrics=(), count=1),),
        support_cells=(SupportCellCount(bins=cell, count=1),),
        residual_scales=tuple(
            ResidualScale(horizon_ms=horizon, metric=metric, scale=1.0, validation_samples=1)
            for horizon in HORIZONS
            for metric in TARGETS
        ),
        residual_validation_example_count=1,
        residual_excluded_example_count=0,
        action_support=ActionSupportV2(
            steps=tuple(
                StepActionSupport(
                    step=step,
                    demand_ranges=zone_ranges(0.0, 1.0),
                    cells=(ActionCellCount(cell=cell, count=1),),
                )
                for step in range(STEPS)
            ),
            anchor_to_first=AnchorTransitionSupport(
                delta_ranges=zone_ranges(-1.0, 1.0), cells=(transition,)
            ),
            transitions=tuple(
                StepTransitionSupport(
                    from_step=step, delta_ranges=zone_ranges(-1.0, 1.0), cells=(transition,)
                )
                for step in range(STEPS - 1)
            ),
        ),
    )


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory) -> CounterfactualTrainedModel:
    return train(tmp_path_factory.mktemp("dataset"))


@pytest.fixture(scope="module")
def artifact(trained: CounterfactualTrainedModel) -> CounterfactualThermalModelArtifact:
    return assemble_counterfactual_artifact(trained, profile_for(trained))


# ---------------------------------------------------------------- Registry の検証経路の代わり


def encode(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            document, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )


def document(artifact: CounterfactualThermalModelArtifact) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(canonical_json_bytes(artifact))
    return loaded


def parse[M: BaseModel](model: type[M], value: object) -> M:
    return model.model_validate_json(json.dumps(value))


def reseal(doc: dict[str, Any]) -> dict[str, Any]:
    """改変した区画の digest を manifest へ書き戻す（狙った検査だけが外れるように）。"""
    payload = parse(counterfactual_module.RidgeModelPayload, doc["payload"])
    doc["manifest"]["payload_sha256"] = canonical_sha256(payload)
    doc["confidence_profile"]["binding"]["payload_sha256"] = doc["manifest"]["payload_sha256"]
    entries = parse(counterfactual_module.MetricBinding, doc["manifest"]["metric_binding"]).entries
    doc["manifest"]["metric_binding"]["sha256"] = counterfactual_module.metric_binding_sha256(
        entries
    )
    profile = parse(ConfidenceProfileV2, doc["confidence_profile"])
    doc["manifest"]["confidence_profile_sha256"] = profile.sha256()
    return doc


def metadata_for(payload: bytes, **overrides: Any) -> ArtifactMetadata:
    parsed = CounterfactualThermalModelArtifact.model_validate_json(payload)
    derived = counterfactual_module._metadata_from_artifact(parsed, payload)
    values = json.loads(counterfactual_registry_metadata_json_bytes(derived))
    values.update(overrides)
    return ArtifactMetadata.model_validate_json(json.dumps(values))


def verified(payload: bytes, metadata: ArtifactMetadata | None = None) -> VerifiedArtifact:
    metadata = metadata_for(payload) if metadata is None else metadata
    return VerifiedArtifact(
        metadata=metadata,
        payload=payload,
        attestation=ArtifactAttestation._issue(
            metadata, 0, _token=registry_module._ATTESTATION_ISSUE_TOKEN
        ),
    )


def load(
    verified_artifact: object,
    *,
    catalog: MetricCatalog = CATALOG,
    calibration: RuntimeCalibration = RUNTIME,
) -> RegistryCounterfactualThermalModel:
    return RegistryCounterfactualThermalModel.from_verified_artifact(
        verified_artifact,  # type: ignore[arg-type]
        metric_catalog=catalog,
        calibration=calibration,
    )


def rejected(check: ArtifactCheck, verified_artifact: object, **kwargs: Any) -> str:
    with pytest.raises(CounterfactualArtifactRejectedError) as caught:
        load(verified_artifact, **kwargs)
    assert caught.value.check is check, str(caught.value)
    return caught.value.detail


def observed_at(tick: int) -> ObservedThermalInput:
    return ObservedThermalInput.from_example_v2(example(tick))


# ---------------------------------------------------------------- trainer


def test_training_and_artifact_bytes_are_deterministic(tmp_path: Path) -> None:
    first = train(tmp_path / "a")
    second = train(tmp_path / "b")
    assert first == second
    first_bytes = canonical_counterfactual_artifact_bytes(
        assemble_counterfactual_artifact(first, profile_for(first))
    )
    second_bytes = canonical_counterfactual_artifact_bytes(
        assemble_counterfactual_artifact(second, profile_for(second))
    )
    assert first_bytes == second_bytes


def test_trainer_drops_plan_columns_after_each_horizon(
    trained: CounterfactualTrainedModel,
) -> None:
    schema = trained.feature_schema
    later_step = [schema.plan_column_index(1, zone) for zone in counterfactual_module.ZONE_ORDER]
    first_step = [schema.plan_column_index(0, zone) for zone in counterfactual_module.ZONE_ORDER]
    for output in trained.payload.outputs:
        if output.horizon_ms == 1_000:
            # step 1 は [1000, 2000) に掛かり、1000 ms の target の後である
            assert all(output.coefficients[index] == 0.0 for index in later_step)
        else:
            assert any(output.coefficients[index] != 0.0 for index in later_step)
        assert any(output.coefficients[index] != 0.0 for index in first_step)
    assert causal_plan_steps(trained.action_schema, 1_000) == 1
    assert causal_plan_steps(trained.action_schema, 2_000) == 2


def test_validation_and_test_values_do_not_affect_the_payload(tmp_path: Path) -> None:
    dataset = thermal_dataset()
    parts = split(dataset)
    baseline = train_counterfactual_ridge(
        published(dataset, tmp_path / "a"), parts, training_spec(), metric_catalog=CATALOG
    )
    changed_ticks = {
        item.action_ts_ms // 1_000 for item in (*parts.validation, *parts.test, *parts.purged)
    }

    def shifted(tick: int) -> DatasetExampleV2:
        original = example(tick)
        if tick not in changed_ticks:
            return original
        targets = tuple(
            target.model_copy(
                update={"values": {metric: 99.0 for metric in TARGETS}},
            )
            for target in original.targets
        )
        return original.model_copy(update={"targets": targets})

    other = thermal_dataset(tuple(shifted(tick) for tick in ANCHOR_TICKS))
    retrained = train_counterfactual_ridge(
        published(other, tmp_path / "b"), split(other), training_spec(), metric_catalog=CATALOG
    )
    assert retrained.payload == baseline.payload
    assert retrained.training_data.examples_sha256 != baseline.training_data.examples_sha256


def test_trainer_records_provenance_windows_and_bindings(
    trained: CounterfactualTrainedModel,
) -> None:
    data = trained.training_data
    assert data.dataset_alias == DATASET_ID
    assert data.dataset_schema_version == 2
    assert data.train_example_count == 24
    assert (data.window.train.start_ms, data.window.train.end_ms) == (1_000, 27_000)
    assert data.window.validation is not None and data.window.test is not None
    assert data.window.validation.start_ms >= VALIDATION_START_MS
    variation = data.action_variation_summary.front
    assert variation.transitions == 24 * STEPS and 0 < variation.changed <= variation.transitions
    # digest は trainer が較正の値から計算する（0096 §2.6）。湿度・未使用チャネルは入らない
    assert (
        trained.calibration_binding.sha256 == hashlib.sha256(FIXTURE_CALIBRATION_BYTES).hexdigest()
    )
    entries = {entry.metric: entry for entry in trained.metric_binding.entries}
    assert set(entries) == set(FEATURES) | set(TARGETS)
    derived = entries["d.gpu_rise"].derived
    assert derived is not None and (derived.minuend, derived.subtrahend) == (
        "gpu.0.core",
        "air.front_intake",
    )
    assert trained.target_schema.dataset_schema_version == 2
    assert trained.feature_schema.columns[-1] == "plan[1].top.effective_demand"


def test_trainer_refuses_metrics_missing_from_the_catalog(tmp_path: Path) -> None:
    dataset = thermal_dataset()
    catalog = CATALOG.model_copy(update={"derived": {}})
    with pytest.raises(ValueError, match=r"MetricCatalog に無い metric.*d\.gpu_rise"):
        train_counterfactual_ridge(
            published(dataset, tmp_path), split(dataset), training_spec(), metric_catalog=catalog
        )


@pytest.mark.parametrize(
    "stages",
    [
        (AuthorityStage.LIMITED,),
        (AuthorityStage.SHADOW, AuthorityStage.EXPANDED),
        (AuthorityStage.LIMITED, AuthorityStage.SHADOW),
    ],
)
def test_authority_compatibility_must_start_at_shadow_without_gaps(
    stages: tuple[AuthorityStage, ...],
) -> None:
    with pytest.raises(ValidationError, match="SHADOW から順に"):
        training_spec(authority_compatibility=stages)


def test_calibration_values_have_no_default() -> None:
    values = training_spec().model_dump()
    values.pop("calibration_offsets_c")
    with pytest.raises(ValidationError, match="calibration_offsets_c"):
        CounterfactualTrainingSpec(**values)


def test_trainer_no_longer_accepts_a_digest_from_the_caller() -> None:
    # 呼び出し側が digest を手で作る経路は残さない（0096 §2.6）
    with pytest.raises(ValidationError, match="calibration_sha256"):
        training_spec(calibration_sha256=SHA_A)


def test_trainer_refuses_non_finite_calibration_values() -> None:
    with pytest.raises(ValidationError):
        training_spec(calibration_offsets_c={"front_intake": float("nan")})


def test_trainer_requires_the_published_v2_dataset(tmp_path: Path) -> None:
    dataset = thermal_dataset()
    with pytest.raises(TypeError):
        train_counterfactual_ridge(
            SimpleNamespace(dataset=dataset),  # type: ignore[arg-type]
            split(dataset),
            training_spec(),
            metric_catalog=CATALOG,
        )
    other = thermal_dataset(tuple(example(tick) for tick in ANCHOR_TICKS)[:-1])
    with pytest.raises(ValueError, match="学習対象の dataset と一致しない"):
        source = published(dataset, tmp_path)
        directory = tmp_path / DATASET_ID
        verify_training_dataset_artifact_v2(
            other, directory / "manifest.json", directory / "examples.jsonl"
        )
    with pytest.raises(ValueError, match="example総数"):
        train_counterfactual_ridge(source, split(other), training_spec(), metric_catalog=CATALOG)


def test_profile_v2_requires_fan_axes_for_every_zone(trained: CounterfactualTrainedModel) -> None:
    profile = profile_for(trained)
    spec = profile.spec.model_copy(update={"support_axes": profile.spec.support_axes[:2]})
    with pytest.raises(ValidationError, match=r"fan\.top"):
        ConfidenceProfileV2.model_validate(
            profile.model_copy(update={"spec": spec}).model_dump(mode="python")
        )


# ---------------------------------------------------------------- 読み込みと推論


def test_verified_v2_artifact_loads_with_its_bundled_profile(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    model = load(verified(payload))
    assert model.artifact_sha256 == hashlib.sha256(payload).hexdigest()
    assert model.payload_sha256 == artifact.manifest.payload_sha256
    assert model.confidence_profile == artifact.confidence_profile
    assert model.artifact_verification is ArtifactVerification.REGISTRY_VERIFIED
    prediction = model.predict(observed_at(30))
    assert prediction.capability is InferenceCapability.COUNTERFACTUAL_ACTION
    assert prediction.artifact_sha256 == model.artifact_sha256
    assert tuple(target.horizon_ms for target in prediction.targets) == HORIZONS


def test_sealed_type_has_no_public_constructor() -> None:
    with pytest.raises(TypeError):
        RegistryCounterfactualThermalModel()


def test_anchor_inference_equals_the_held_plan_of_the_current_effective_demand(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    model = load(verified(canonical_counterfactual_artifact_bytes(artifact)))
    for tick in (5, 30, 39):
        observed = observed_at(tick)
        current = observed.action
        plan = ActionPlan.held(
            PerZone(
                front=current.front.effective_demand,
                rear=current.rear.effective_demand,
                top=current.top.effective_demand,
            ),
            step_ms=STEP_MS,
            steps=STEPS,
        )
        trajectory = ActionTrajectory(
            step_ms=plan.step_ms, demands=tuple(step.demands for step in plan.steps)
        )
        assert model.predict(observed) == model.predict_trajectory(observed, trajectory)
        assert hold_effective(observed, model.action_schema) == trajectory


def test_anchor_inference_does_not_use_the_recorded_action_steps(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    model = load(verified(canonical_counterfactual_artifact_bytes(artifact)))
    item = example(30)
    observed = ObservedThermalInput.from_example_v2(item)
    recorded = recorded_trajectory(item, model.action_schema)
    assert recorded != hold_effective(observed, model.action_schema)
    assert model.predict(observed) != model.predict_trajectory(observed, recorded)


@pytest.mark.parametrize(
    "trajectory",
    [
        ActionTrajectory(step_ms=500, demands=(demands(1),) * 4),
        ActionTrajectory(step_ms=STEP_MS, demands=(demands(1),)),
        ActionTrajectory(step_ms=STEP_MS, demands=(demands(1),) * 3),
    ],
)
def test_trajectory_off_the_action_grid_is_not_predicted(
    artifact: CounterfactualThermalModelArtifact, trajectory: ActionTrajectory
) -> None:
    model = load(verified(canonical_counterfactual_artifact_bytes(artifact)))
    with pytest.raises(ValueError, match="格子"):
        model.predict_trajectory(observed_at(30), trajectory)


# ---------------------------------------------------------------- L1〜L12


def test_l1_requires_the_nominal_verified_artifact_and_counterfactual_attestation(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    good = verified(payload)
    rejected(
        ArtifactCheck.VERIFIED_ARTIFACT,
        SimpleNamespace(metadata=good.metadata, payload=payload, attestation=good.attestation),
    )
    observational = metadata_for(payload, capability="observational_replay")
    rejected(ArtifactCheck.VERIFIED_ARTIFACT, verified(payload, observational))
    supervisor = metadata_for(payload, kind="supervisor_policy")
    rejected(ArtifactCheck.VERIFIED_ARTIFACT, verified(payload, supervisor))


def test_l2_rejects_oversized_and_pickle_payloads(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    for bad in (b"\x80\x04K\x01.", payload[:-1] + b" " * (8 * 1024 * 1024) + b"\n"):
        metadata = metadata_for(payload, sha256=hashlib.sha256(bad).hexdigest())
        rejected(ArtifactCheck.SIZE_AND_STRUCTURE, verified(bad, metadata))


def test_l3_rejects_bytes_that_are_not_the_canonical_serialization(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    pretty = (json.dumps(json.loads(payload), indent=2, sort_keys=True) + "\n").encode()
    unsorted = (json.dumps(json.loads(payload), separators=(",", ":")) + "\n").encode()
    for bad in (pretty, unsorted, payload.rstrip(b"\n")):
        assert bad != payload
        metadata = metadata_for(payload, sha256=hashlib.sha256(bad).hexdigest())
        rejected(ArtifactCheck.CANONICAL_BYTES, verified(bad, metadata))


def test_l4_never_reads_a_v1_artifact_as_counterfactual(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    from coldaisle.control.model.thermal import canonical_artifact_bytes
    from test_thermal_model import trained_artifact as v1_trained_artifact

    v1_bytes = canonical_artifact_bytes(v1_trained_artifact())
    payload = canonical_counterfactual_artifact_bytes(artifact)
    metadata = metadata_for(payload, sha256=hashlib.sha256(v1_bytes).hexdigest())
    detail = rejected(ArtifactCheck.SCHEMA_VERSION, verified(v1_bytes, metadata))
    assert "v2" in detail


def test_l4_rejects_a_v2_document_that_does_not_match_the_schema(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    doc = document(artifact)
    doc["manifest"]["capability"] = "observational_replay"
    bad = encode(doc)
    metadata = metadata_for(
        canonical_counterfactual_artifact_bytes(artifact), sha256=hashlib.sha256(bad).hexdigest()
    )
    rejected(ArtifactCheck.SCHEMA_VERSION, verified(bad, metadata))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_id", "other-model"),
        ("created_at", "2026-10-08T10:00:00+09:00"),
        ("training_dataset_version", "dataset-ffffffffffffffffffffffffffffffff"),
        ("source_runs", ["run-ffffffffffffffffffffffffffffffff"]),
        ("feature_schema_version", "thermal-features-v1"),
        ("target_schema_version", "thermal-targets-v2"),
        ("code_commit", "1111111"),
        ("model_family", "ridge_linear_v2"),
        ("hyperparameters", {"ridge_lambda": 0.5}),
        ("authority_compatibility", ["shadow", "limited"]),
    ],
)
def test_l5_binds_every_artifact_determined_registry_field(
    artifact: CounterfactualThermalModelArtifact, field: str, value: object
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    detail = rejected(
        ArtifactCheck.REGISTRY_METADATA, verified(payload, metadata_for(payload, **{field: value}))
    )
    assert field in detail


def test_l5_ignores_only_lifecycle_evaluation_references(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    metadata = metadata_for(
        payload,
        offline_evaluation_ref="evaluation/offline/0.1.0",
        shadow_evaluation_ref="evaluation/shadow/0.1.0",
    )
    load(verified(payload, metadata))


def tampered(
    artifact: CounterfactualThermalModelArtifact, change: Any, *, rehash: bool = True
) -> VerifiedArtifact:
    doc = document(artifact)
    change(doc)
    if rehash:
        reseal(doc)
    return verified(encode(doc))


def test_l6_recomputes_payload_profile_schema_and_binding_digests(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    def payload_only(doc: dict[str, Any]) -> None:
        doc["payload"]["outputs"][0]["intercept"] += 1.0

    def profile_only(doc: dict[str, Any]) -> None:
        doc["confidence_profile"]["residual_excluded_example_count"] = 3

    def schema_only(doc: dict[str, Any]) -> None:
        doc["manifest"]["schemas"]["action_sha256"] = SHA_A

    def binding_only(doc: dict[str, Any]) -> None:
        doc["manifest"]["metric_binding"]["entries"][0]["unit"] = "°F"

    for change, name in (
        (payload_only, "payload_sha256"),
        (profile_only, "confidence_profile_sha256"),
        (schema_only, "action_schema"),
        (binding_only, "metric_binding"),
    ):
        detail = rejected(ArtifactCheck.DIGESTS, tampered(artifact, change, rehash=False))
        assert name in detail


def test_profile_payload_hash_rewrite_is_caught_by_l6_or_l7(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    def rewrite(doc: dict[str, Any]) -> None:
        doc["confidence_profile"]["binding"]["payload_sha256"] = SHA_A

    rejected(ArtifactCheck.DIGESTS, tampered(artifact, rewrite, rehash=False))

    def rewrite_and_rehash_profile(doc: dict[str, Any]) -> None:
        doc["confidence_profile"]["binding"]["payload_sha256"] = SHA_A
        profile = parse(ConfidenceProfileV2, doc["confidence_profile"])
        doc["manifest"]["confidence_profile_sha256"] = profile.sha256()

    detail = rejected(
        ArtifactCheck.PROFILE_BINDING, tampered(artifact, rewrite_and_rehash_profile, rehash=False)
    )
    assert "payload_sha256" in detail


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("binding", "training_split_sha256"), SHA_A),
        (("binding", "model_version"), "0.2.0"),
        (("binding", "feature_schema_sha256"), SHA_B),
        (("train_example_count",), 7),
    ],
)
def test_l7_rejects_a_profile_bound_to_another_model_or_split(
    artifact: CounterfactualThermalModelArtifact, path: tuple[str, ...], value: object
) -> None:
    def change(doc: dict[str, Any]) -> None:
        target = doc["confidence_profile"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    rejected(ArtifactCheck.PROFILE_BINDING, tampered(artifact, change))


def test_l7_rejects_a_profile_whose_outputs_do_not_follow_the_target_schema(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["confidence_profile"]["residual_scales"].reverse()

    rejected(ArtifactCheck.PROFILE_BINDING, tampered(artifact, change))


def test_l8_rejects_units_or_definitions_that_differ_from_the_runtime_catalog(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    fahrenheit = CATALOG.model_copy(
        update={
            "metrics": {
                **CATALOG.metrics,
                "cpu.package": CATALOG.metrics["cpu.package"].model_copy(update={"unit": "°F"}),
            }
        }
    )
    assert "cpu.package" in rejected(
        ArtifactCheck.METRIC_BINDING, verified(payload), catalog=fahrenheit
    )
    redefined = CATALOG.model_copy(
        update={
            "derived": {
                "d.gpu_rise": CATALOG.derived["d.gpu_rise"].model_copy(
                    update={"subtrahend": "air.room"}
                )
            }
        }
    )
    assert "d.gpu_rise" in rejected(
        ArtifactCheck.METRIC_BINDING, verified(payload), catalog=redefined
    )
    missing = CATALOG.model_copy(update={"derived": {}})
    rejected(ArtifactCheck.METRIC_BINDING, verified(payload), catalog=missing)
    # 表示名だけの変更では失効しない
    relabeled = CATALOG.model_copy(
        update={
            "metrics": {
                **CATALOG.metrics,
                "cpu.package": CATALOG.metrics["cpu.package"].model_copy(
                    update={"label": "CPU パッケージ"}
                ),
            }
        }
    )
    load(verified(payload), catalog=relabeled)


def _drop_entry(doc: dict[str, Any]) -> None:
    doc["manifest"]["metric_binding"]["entries"] = [
        entry
        for entry in doc["manifest"]["metric_binding"]["entries"]
        if entry["metric"] != "cpu.package"
    ]


def _extra_entry(doc: dict[str, Any]) -> None:
    doc["manifest"]["metric_binding"]["entries"].append(
        {"metric": "zz.unused", "unit": "°C", "derived": None}
    )


def _duplicate_entry(doc: dict[str, Any]) -> None:
    entries = doc["manifest"]["metric_binding"]["entries"]
    entries.insert(0, dict(entries[0]))


@pytest.mark.parametrize("change", [_drop_entry, _extra_entry, _duplicate_entry])
def test_l8_requires_the_binding_to_cover_exactly_the_feature_and_target_metrics(
    artifact: CounterfactualThermalModelArtifact, change: Any
) -> None:
    rejected(ArtifactCheck.METRIC_BINDING, tampered(artifact, change))


@pytest.mark.parametrize("change", [_drop_entry, _extra_entry, _duplicate_entry])
def test_l8_is_also_checked_when_the_artifact_is_created(
    trained: CounterfactualTrainedModel, change: Any
) -> None:
    doc: dict[str, Any] = {
        "manifest": {"metric_binding": trained.metric_binding.model_dump(mode="json")}
    }
    change(doc)
    entries = parse(counterfactual_module.MetricBinding, doc["manifest"]["metric_binding"]).entries
    binding = counterfactual_module.MetricBinding(
        entries=entries, sha256=counterfactual_module.metric_binding_sha256(entries)
    )
    broken = trained.model_copy(update={"metric_binding": binding})
    with pytest.raises(CounterfactualArtifactRejectedError) as caught:
        assemble_counterfactual_artifact(broken, profile_for(broken))
    assert caught.value.check is ArtifactCheck.METRIC_BINDING


# ------------------------------------------------ L9（較正の digest。決定記録 0096）


def renamed_dataset(name: str) -> ThermalDatasetV2:
    """fixture の dataset の ``air.front_intake`` を ``name`` に置き換える（metric の集合だけ）。"""
    raw = thermal_dataset().model_dump_json().replace('"air.front_intake"', json.dumps(name))
    doc = json.loads(raw)
    items = tuple(DatasetExampleV2.model_validate_json(json.dumps(e)) for e in doc["examples"])
    doc["manifest"]["examples_sha256"] = examples_sha256(items)
    manifest = DatasetManifestV2.model_validate_json(json.dumps(doc["manifest"]))
    return ThermalDatasetV2(manifest=manifest, examples=items)


def renamed_catalog(name: str) -> MetricCatalog:
    values = CATALOG.model_dump(mode="python")
    values["metrics"][name] = {"unit": "°C", "label": name}
    values["derived"]["d.gpu_rise"]["subtrahend"] = name
    return MetricCatalog.model_validate(values)


def artifact_using(
    tmp_path: Path, name: str, offsets: dict[str, float] | None = None
) -> tuple[bytes, MetricCatalog]:
    """``air.front_intake`` の代わりに ``name`` を使う artifact の bytes と、それに合う catalog。"""
    dataset = renamed_dataset(name)
    catalog = renamed_catalog(name)
    trained = train_counterfactual_ridge(
        published(dataset, tmp_path),
        split(dataset),
        training_spec(
            calibration_offsets_c=dict(CALIBRATION_OFFSETS) if offsets is None else offsets
        ),
        metric_catalog=catalog,
    )
    payload = canonical_counterfactual_artifact_bytes(
        assemble_counterfactual_artifact(trained, profile_for(trained))
    )
    return payload, catalog


UNAVAILABLE = RuntimeCalibration.unavailable("config/calibration.json を読めなかった（試験）")


def test_l9_accepts_the_artifact_with_the_calibration_it_was_trained_with(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    load(verified(payload))
    # 使わないチャネルの再較正と、明示の 0.0 / 欠落の違いでは失効しない（0096 §2.9 の感度・実効値）
    load(
        verified(payload),
        calibration=RuntimeCalibration.available(
            {"front_intake": 0.191, "room_temp": 5.0, "room_humidity": 9.0, "top_exhaust": 0.0}
        ),
    )


@pytest.mark.parametrize(
    "offsets",
    [
        {**CALIBRATION_OFFSETS, "front_intake": math.nextafter(0.191, math.inf)},
        {**CALIBRATION_OFFSETS, "front_intake": math.nextafter(0.191, -math.inf)},
        {"room_temp": -0.31},
    ],
)
def test_l9_rejects_when_a_used_offset_changes(
    artifact: CounterfactualThermalModelArtifact, offsets: dict[str, float]
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    rejected(
        ArtifactCheck.CALIBRATION,
        verified(payload),
        calibration=RuntimeCalibration.available(offsets),
    )


def test_l9_rejects_a_calibrated_artifact_when_runtime_calibration_is_unavailable(
    tmp_path: Path,
) -> None:
    # 全 offset 0.0 で学習した null でない artifact。available({}) は通り、unavailable は通らない
    payload, catalog = artifact_using(tmp_path, "air.front_intake", offsets={})
    parsed = CounterfactualThermalModelArtifact.model_validate_json(payload)
    assert parsed.manifest.calibration_binding.sha256 is not None
    load(verified(payload), catalog=catalog, calibration=RuntimeCalibration.available({}))
    detail = rejected(
        ArtifactCheck.CALIBRATION, verified(payload), catalog=catalog, calibration=UNAVAILABLE
    )
    assert "読めなかった" in detail


@pytest.mark.parametrize("name", ["gpu.0.board", "air.room_humidity"])
def test_l9_accepts_null_artifacts_regardless_of_runtime_calibration(
    tmp_path: Path, name: str
) -> None:
    payload, catalog = artifact_using(tmp_path, name)
    parsed = CounterfactualThermalModelArtifact.model_validate_json(payload)
    assert parsed.manifest.calibration_binding.sha256 is None
    for runtime in (
        RUNTIME,
        RuntimeCalibration.available({}),
        RuntimeCalibration.available({"room_humidity": 3.0, "front_intake": 1.0}),
        UNAVAILABLE,
    ):
        load(verified(payload), catalog=catalog, calibration=runtime)


def _declare_null(doc: dict[str, Any]) -> None:
    doc["manifest"]["calibration_binding"]["sha256"] = None


def _declare_digest(doc: dict[str, Any]) -> None:
    doc["manifest"]["calibration_binding"]["sha256"] = SHA_A


@pytest.mark.parametrize("runtime", [RUNTIME, RuntimeCalibration.available({}), UNAVAILABLE])
def test_l9_does_not_trust_a_null_declaration_for_calibrated_metrics(
    artifact: CounterfactualThermalModelArtifact, runtime: RuntimeCalibration
) -> None:
    detail = rejected(
        ArtifactCheck.CALIBRATION, tampered(artifact, _declare_null), calibration=runtime
    )
    assert "null である" in detail


@pytest.mark.parametrize("name", ["gpu.0.board", "air.room_humidity"])
@pytest.mark.parametrize("runtime", [RUNTIME, UNAVAILABLE])
def test_l9_rejects_a_digest_declared_for_an_artifact_without_calibrated_metrics(
    tmp_path: Path, name: str, runtime: RuntimeCalibration
) -> None:
    payload, catalog = artifact_using(tmp_path, name)
    parsed = CounterfactualThermalModelArtifact.model_validate_json(payload)
    detail = rejected(
        ArtifactCheck.CALIBRATION,
        tampered(parsed, _declare_digest),
        catalog=catalog,
        calibration=runtime,
    )
    assert "null でない" in detail


def test_l9_rejects_a_nested_derived_expansion(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    # runtime の catalog では L8 が先に拒否するので、L9 だけを直接確かめる
    doc = document(artifact)
    for entry in doc["manifest"]["metric_binding"]["entries"]:
        if entry["derived"] is not None:
            entry["derived"]["subtrahend"] = "d.intake_rise"
    reseal(doc)
    parsed = CounterfactualThermalModelArtifact.model_validate_json(encode(doc))
    with pytest.raises(CounterfactualArtifactRejectedError) as caught:
        counterfactual_module.check_artifact_contents(
            parsed, metric_catalog=None, calibration=RUNTIME, check_calibration=True
        )
    assert caught.value.check is ArtifactCheck.CALIBRATION


def test_l9_requires_a_runtime_calibration_object(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    payload = canonical_counterfactual_artifact_bytes(artifact)
    for value in (None, {}, SHA_A):
        with pytest.raises(TypeError):
            load(verified(payload), calibration=value)  # type: ignore[arg-type]


def test_l10_rejects_a_nonzero_coefficient_on_a_plan_step_after_the_horizon(
    artifact: CounterfactualThermalModelArtifact, trained: CounterfactualTrainedModel
) -> None:
    index = trained.feature_schema.plan_column_index(1, counterfactual_module.ZONE_ORDER[0])

    def change(doc: dict[str, Any]) -> None:
        output = doc["payload"]["outputs"][0]
        assert output["horizon_ms"] == 1_000
        output["coefficients"][index] = 1e-12

    detail = rejected(ArtifactCheck.CAUSAL_MASK, tampered(artifact, change))
    assert "1000" in detail
    broken = trained.payload.model_copy(
        update={
            "outputs": (
                trained.payload.outputs[0].model_copy(
                    update={
                        "coefficients": tuple(
                            1e-12 if position == index else value
                            for position, value in enumerate(
                                trained.payload.outputs[0].coefficients
                            )
                        )
                    }
                ),
                *trained.payload.outputs[1:],
            )
        }
    )
    with pytest.raises(CounterfactualArtifactRejectedError) as caught:
        changed = trained.model_copy(update={"payload": broken})
        assemble_counterfactual_artifact(changed, profile_for(changed))
    assert caught.value.check is ArtifactCheck.CAUSAL_MASK


@pytest.mark.parametrize("section", ["manifest", "confidence_profile"])
def test_l11_rejects_an_anchor_action_rule_other_than_hold_effective(
    artifact: CounterfactualThermalModelArtifact, section: str
) -> None:
    doc = document(artifact)
    doc[section]["anchor_action_rule"] = "previous_plan"
    bad = encode(doc)
    metadata = metadata_for(
        canonical_counterfactual_artifact_bytes(artifact), sha256=hashlib.sha256(bad).hexdigest()
    )
    rejected(ArtifactCheck.ANCHOR_ACTION_RULE, verified(bad, metadata))


def test_l11_is_a_separate_check_even_after_the_schema_parse(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    broken = artifact.model_copy(
        update={
            "confidence_profile": artifact.confidence_profile.model_copy(
                update={"anchor_action_rule": "previous_plan"}
            )
        }
    )
    with pytest.raises(CounterfactualArtifactRejectedError) as caught:
        counterfactual_module._check_anchor_action_rule(broken)
    assert caught.value.check is ArtifactCheck.ANCHOR_ACTION_RULE


def _drop_last_step(doc: dict[str, Any]) -> None:
    doc["confidence_profile"]["action_support"]["steps"].pop()


def _renumber_steps(doc: dict[str, Any]) -> None:
    for item in doc["confidence_profile"]["action_support"]["steps"]:
        item["step"] += 1


def _drop_transitions(doc: dict[str, Any]) -> None:
    doc["confidence_profile"]["action_support"]["transitions"] = []


def _extra_transition(doc: dict[str, Any]) -> None:
    transitions = doc["confidence_profile"]["action_support"]["transitions"]
    transitions.append({**transitions[0], "from_step": 1})


@pytest.mark.parametrize(
    "change", [_drop_last_step, _renumber_steps, _drop_transitions, _extra_transition]
)
def test_l12_requires_one_support_entry_per_step_and_step_pair(
    artifact: CounterfactualThermalModelArtifact, change: Any
) -> None:
    rejected(ArtifactCheck.PROFILE_STEPS, tampered(artifact, change))


def test_l12_is_also_checked_when_the_artifact_is_created(
    trained: CounterfactualTrainedModel,
) -> None:
    profile = profile_for(trained)
    support = profile.action_support.model_copy(update={"steps": profile.action_support.steps[:1]})
    with pytest.raises(CounterfactualArtifactRejectedError) as caught:
        assemble_counterfactual_artifact(
            trained, profile.model_copy(update={"action_support": support})
        )
    assert caught.value.check is ArtifactCheck.PROFILE_STEPS


def test_creation_refuses_a_profile_bound_to_another_payload(
    trained: CounterfactualTrainedModel,
) -> None:
    profile = profile_for(trained)
    binding = profile.binding.model_copy(update={"payload_sha256": SHA_A})
    with pytest.raises(CounterfactualArtifactRejectedError) as caught:
        assemble_counterfactual_artifact(trained, profile.model_copy(update={"binding": binding}))
    assert caught.value.check is ArtifactCheck.PROFILE_BINDING


# ---------------------------------------------------------------- v1 との互換と Registry


def test_v1_artifacts_still_load_and_v1_loader_refuses_v2(
    artifact: CounterfactualThermalModelArtifact,
) -> None:
    from coldaisle.control.model.thermal import (
        canonical_artifact_bytes,
        expectation_from_registry_metadata,
        registry_metadata,
    )
    from test_thermal_model import load_registry_model
    from test_thermal_model import trained_artifact as v1_trained_artifact

    v1 = v1_trained_artifact()
    v1_bytes = canonical_artifact_bytes(v1)
    expected = expectation_from_registry_metadata(
        registry_metadata(v1, v1_bytes), authority_stage=AuthorityStage.SHADOW
    )
    model = load_registry_model(v1_bytes, expected=expected)
    assert model.manifest.capability is InferenceCapability.OBSERVATIONAL_REPLAY
    assert v1.target_schema.dataset_schema_version == 1
    payload = canonical_counterfactual_artifact_bytes(artifact)
    with pytest.raises(ValueError):
        RidgeThermalModel.from_verified_artifact(
            verified(payload), authority_stage=AuthorityStage.SHADOW
        )


def test_v1_artifact_refuses_a_dataset_v2_target_schema() -> None:
    from test_thermal_model import trained_artifact as v1_trained_artifact

    v1 = v1_trained_artifact()
    v2_target = ThermalTargetSchema.model_validate(
        {**v1.target_schema.model_dump(mode="python"), "dataset_schema_version": 2}
    )
    with pytest.raises(ValidationError, match="Dataset v1"):
        type(v1).model_validate(
            {**v1.model_dump(mode="python"), "target_schema": v2_target.model_dump(mode="python")}
        )


def _approval(metadata: ArtifactMetadata, revision: int) -> HumanApproval:
    return HumanApproval(
        action=ApprovalAction.PROMOTE,
        artifact=metadata.ref,
        artifact_sha256=metadata.sha256,
        expected_revision=revision,
        approver="model-operator",
        approved_at_ms=NOW_MS,
        reason="shadow evaluation passed",
    )


def test_promotion_and_rollback_move_model_and_profile_as_one_artifact(
    tmp_path: Path, trained: CounterfactualTrainedModel
) -> None:
    registry = ModelRegistry(
        tmp_path / "registry",
        SimulatedClock(NOW_MS),
        limits=load_model_registry_limits(CONFIG_DIR),
    )
    compatibility = ModelCompatibility(
        feature_schema_version="thermal-features-v2",
        target_schema_version="thermal-targets-v1",
        authority_stage=AuthorityStage.SHADOW,
    )
    loaded: dict[str, tuple[bytes, ConfidenceProfileV2]] = {}
    for version, floor in (("0.1.0", 0.1), ("0.2.0", 0.2)):
        changed = trained.model_copy(update={"model_version": version})
        profile = profile_for(changed)
        profile = profile.model_copy(
            update={"spec": profile.spec.model_copy(update={"residual_scale_floor": floor})}
        )
        built = assemble_counterfactual_artifact(changed, profile)
        payload = canonical_counterfactual_artifact_bytes(built)
        metadata = ArtifactMetadata.model_validate_json(
            counterfactual_registry_metadata_json_bytes(
                counterfactual_registry_metadata(built, payload)
            )
        )
        registry.register_candidate(metadata, payload, actor="trainer", reason="trained")
        registry.mark_validated(
            metadata.ref,
            offline_evaluation_ref=f"evaluation/offline/{version}",
            actor="evaluator",
            reason="offline gates passed",
            expected_revision=registry.inspect().revision,
        )
        revision = registry.inspect().revision
        registry.promote(
            metadata.ref,
            compatibility,
            shadow_evaluation_ref=f"evaluation/shadow/{version}",
            approval=_approval(metadata, revision),
            expected_revision=revision,
        )
        loaded[version] = (payload, profile)

    def production() -> RegistryCounterfactualThermalModel:
        result = registry.load_production(ArtifactKind.THERMAL_MODEL, compatibility)
        assert result.artifact is not None, result.detail
        return load(result.artifact)

    current = production()
    assert current.manifest.model_version == "0.2.0"
    assert current.confidence_profile == loaded["0.2.0"][1]
    revision = registry.inspect().revision
    previous = registry.inspect().production[ArtifactKind.THERMAL_MODEL].previous
    assert previous is not None
    registry.rollback(
        ArtifactKind.THERMAL_MODEL,
        compatibility,
        approval=HumanApproval(
            action=ApprovalAction.ROLLBACK,
            artifact=previous,
            artifact_sha256=hashlib.sha256(loaded["0.1.0"][0]).hexdigest(),
            expected_revision=revision,
            approver="model-operator",
            approved_at_ms=NOW_MS,
            reason="roll back",
        ),
        expected_revision=revision,
    )
    restored = production()
    assert restored.manifest.model_version == "0.1.0"
    assert restored.confidence_profile == loaded["0.1.0"][1]
    assert restored.artifact_sha256 == hashlib.sha256(loaded["0.1.0"][0]).hexdigest()


def test_new_model_modules_stay_upstream_of_guard_safety_and_hardware() -> None:
    forbidden = ("control.hardware", "control.safety", "control.reactive", "hwmon", "pwm")
    for module in (counterfactual_module, training_v2_module):
        assert module.__file__ is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        for token in forbidden:
            assert f"import {token}" not in source, (module.__name__, token)
            assert f"from coldaisle.{token}" not in source, (module.__name__, token)
        assert "coldaisle.ai" not in source
