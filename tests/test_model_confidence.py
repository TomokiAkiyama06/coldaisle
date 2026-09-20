"""#85 Model Confidence / OOD 判定と Authority 制限。実機不要（合成 dataset / Replay）。"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pydantic import ValidationError

import coldaisle.control.fallback.gate as gate_module
import coldaisle.control.model.confidence as confidence_module
from coldaisle.control.fallback import (
    ControllerGate,
    ControllerSelection,
    LearnedControlStatus,
    classify_confidence,
)
from coldaisle.control.model.confidence import (
    ConfidenceAssessment,
    ConfidenceAssessor,
    ConfidenceComponent,
    ConfidenceProfileSpec,
    ModelConfidenceProfile,
    OodEvaluationCase,
    ResidualDriftMonitor,
    ResidualEvidence,
    ResidualObservation,
    SupportAxis,
    derive_inference_id,
    evaluate_ood_detection,
    fit_confidence_profile,
    inference_id,
    replay_cases,
)
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
    split_temporally,
)
from coldaisle.control.model.thermal import (
    ArtifactVerification,
    ObservedFanAction,
    ObservedThermalInput,
    ObservedWindowFrame,
    OfflineRidgeThermalModel,
)
from coldaisle.control.model.training import (
    RidgeTrainingSpec,
    train_ridge_baseline,
    verify_training_dataset_artifact,
)
from coldaisle.control.schema import (
    MODEL_GATE_ASSESSMENT_COMPONENTS,
    AuthorityLimitSource,
    AuthorityStage,
    BoundBy,
    ConfidenceLevel,
    ControllerKind,
    ControllerProposal,
    ControlState,
    ControlTick,
    EffectiveZoneDemand,
    ModelGateDecision,
    OperatingMode,
    PerZone,
    Reason,
    SafetyState,
    ZoneRecord,
)
from coldaisle.store.models import Quality
from test_fallback_controller import (
    TEST_ARTIFACT_SHA256,
    assessment_for,
    fallback_proposal,
    gate_for,
    learned_proposal,
    policy,
)

RUN_ID = "run-00000000000000000000000000000085"
SOURCE_ID = "source-00000000000000000000000000000085"
DATASET_ID = "dataset-00000000000000000000000000000085"
AIR = "air.front_intake"
GPU = "gpu.0.core"
FEATURES = (AIR, GPU)
TARGETS = (GPU,)
EXAMPLES = 40
STEP_MS = 5_000
VALIDATION_START_MS = 10_000 + 24 * STEP_MS - 2_000
TEST_START_MS = 10_000 + 32 * STEP_MS - 2_000


def cycle(index: int) -> int:
    """8周期で繰り返す負荷。test も train と同じ範囲に入る。"""
    return index % 8


def air_value(index: int) -> float:
    return 20.0 + cycle(index)


def gpu_value(index: int) -> float:
    # air と gpu は相関させる。「air が高く gpu が低い」組は学習に無い。
    return 40.0 + 2.0 * cycle(index)


def fan_value(index: int) -> float:
    return 0.3 + 0.05 * cycle(index)


def frame(ts_ms: int, index: int, *, offset: float) -> WindowFrame:
    return WindowFrame(
        ts_ms=ts_ms,
        values={AIR: air_value(index) + offset, GPU: gpu_value(index) + offset},
        source_ts_ms={AIR: ts_ms, GPU: ts_ms},
        quality={AIR: Quality.OK, GPU: Quality.OK},
        missing_mask={AIR: False, GPU: False},
        stale_mask={AIR: False, GPU: False},
    )


def example(
    index: int,
    horizons: tuple[int, ...] = (1_000,),
    targets: tuple[str, ...] = TARGETS,
) -> DatasetExample:
    action_ts = 10_000 + index * STEP_MS
    fan = fan_value(index)
    zone = ActionZone(
        requested_demand=fan,
        effective_demand=fan,
        bound_by="requested",
        controller_reason="synthetic",
        override_reasons=(),
    )
    # 決定論的な小さな揺らぎ。validation の residual を 0 にしない。
    wobble = ((index * 7) % 5 - 2) * 0.1
    label = 0.8 * gpu_value(index) + 0.3 * air_value(index) - 5.0 * fan + wobble

    def target_value(horizon: int, metric: str) -> float:
        offset = 0.0 if metric == GPU else -20.0
        return label + offset + 0.5 * (horizon / 1_000 - 1)

    return DatasetExample(
        example_id=f"{RUN_ID}:{action_ts}:{index}",
        source_run_id=RUN_ID,
        history_start_ms=action_ts - 1_000,
        action_ts_ms=action_ts,
        label_end_ms=action_ts + horizons[-1],
        control_tick_id=index,
        control_schema_version=5,
        window=(frame(action_ts - 1_000, index, offset=-0.5), frame(action_ts, index, offset=0.0)),
        action=PerZone(front=zone, rear=zone, top=zone),
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
        targets=tuple(
            TargetFrame(
                horizon_ms=horizon,
                expected_ts_ms=action_ts + horizon,
                values={metric: target_value(horizon, metric) for metric in targets},
                source_ts_ms={metric: action_ts + horizon for metric in targets},
                quality={metric: Quality.OK for metric in targets},
                missing_mask={metric: False for metric in targets},
            )
            for horizon in horizons
        ),
    )


def dataset(
    horizons: tuple[int, ...] = (1_000,),
    targets: tuple[str, ...] = TARGETS,
) -> ThermalDataset:
    items = tuple(example(index, horizons, targets) for index in range(EXAMPLES))
    return ThermalDataset(
        manifest=DatasetManifest(
            spec=DatasetSpec(
                window_ms=1_000,
                sample_period_ms=1_000,
                horizons_ms=horizons,
                target_tolerance_ms=0,
                stale_after_ms=5_000,
                feature_metrics=FEATURES,
                target_metrics=targets,
            ),
            source_runs=(
                SourceRun(
                    run_id=RUN_ID,
                    kind=DatasetSourceKind.REPLAY,
                    start_ms=0,
                    end_ms=10_000 + EXAMPLES * STEP_MS + 2_000,
                    source_refs=(SOURCE_ID,),
                    source_sha256="a" * 64,
                ),
            ),
            telemetry_sha256="a" * 64,
            control_trace_sha256="b" * 64,
            examples_sha256=examples_sha256(items),
            example_count=len(items),
        ),
        examples=items,
    )


def split(data: ThermalDataset) -> DatasetSplit:
    return split_temporally(
        data.examples, validation_start_ms=VALIDATION_START_MS, test_start_ms=TEST_START_MS
    )


def train(data: ThermalDataset, parts: DatasetSplit, *, version: str = "0.1.0"):
    with TemporaryDirectory() as temporary:
        directory = Path(temporary) / DATASET_ID
        directory.mkdir()
        manifest_path = directory / "manifest.json"
        examples_path = directory / "examples.jsonl"
        manifest_path.write_bytes((data.manifest.model_dump_json(indent=2) + "\n").encode())
        examples_path.write_bytes(examples_jsonl_bytes(data.examples))
        source = verify_training_dataset_artifact(data, manifest_path, examples_path)
        artifact = train_ridge_baseline(
            source,
            parts,
            RidgeTrainingSpec(
                model_id="rack-thermal",
                model_version=version,
                created_at="2026-09-19T10:00:00+09:00",
                ridge_lambda=0.1,
                code_commit="0123456789abcdef",
            ),
        )
    return OfflineRidgeThermalModel.from_artifact(artifact)


def profile_spec() -> ConfidenceProfileSpec:
    return ConfidenceProfileSpec(
        support_axes=(
            SupportAxis(source=AIR, edges=(22.0, 24.0, 26.0)),
            SupportAxis(source=GPU, edges=(44.0, 48.0, 52.0)),
            SupportAxis(source="fan.front", edges=(0.4, 0.5)),
        ),
        residual_scale_floor=0.01,
    )


@pytest.fixture(scope="module")
def trained():
    data = dataset()
    parts = split(data)
    assert (len(parts.train), len(parts.validation), len(parts.test)) == (24, 8, 8)
    model = train(data, parts)
    profile = fit_confidence_profile(model, data, parts, profile_spec())
    return data, parts, model, profile


def confidence_policy():
    return policy().model_confidence


def assessor(profile: ModelConfidenceProfile) -> ConfidenceAssessor:
    return ConfidenceAssessor(profile, confidence_policy())


def shifted(observed: ObservedThermalInput, **values: float) -> ObservedThermalInput:
    """最後の frame の値を差し替えた合成入力。"""
    names = {"air": AIR, "gpu": GPU}
    last = observed.window[-1]
    replaced = dict(last.values)
    for key, value in values.items():
        replaced[names[key]] = value
    frames = (*observed.window[:-1], last.model_copy(update={"values": replaced}))
    return ObservedThermalInput.model_validate(
        observed.model_copy(update={"window": frames}).model_dump(mode="python")
    )


def with_fan(observed: ObservedThermalInput, demand: float) -> ObservedThermalInput:
    action = ObservedFanAction(effective_demand=demand)
    return observed.model_copy(update={"action": PerZone(front=action, rear=action, top=action)})


def with_missing(observed: ObservedThermalInput, metric: str) -> ObservedThermalInput:
    first = observed.window[0]
    missing = ObservedWindowFrame(
        ts_ms=first.ts_ms,
        values={**first.values, metric: None},
        source_ts_ms={**first.source_ts_ms, metric: None},
        missing_mask={**first.missing_mask, metric: True},
        stale_mask={**first.stale_mask, metric: False},
        suspect_mask={**first.suspect_mask, metric: False},
    )
    return observed.model_copy(update={"window": (missing, *observed.window[1:])})


def evidence(
    profile: ModelConfidenceProfile, ratio: float, forecasts: int = 10, *, at: int
) -> ResidualEvidence:
    settings = confidence_policy()
    return ResidualEvidence(
        profile_sha256=profile.sha256(),
        residual_window=settings.residual_window.value,
        match_tolerance_ms=settings.residual_match_tolerance_ms.value,
        max_age_ms=settings.residual_max_age_ms.value,
        as_of_ts_ms=at,
        forecasts=forecasts,
        unmatched=0,
        ratio=ratio,
    )


def tuned(**values: int | float):
    """設定の一部だけを変えた model_confidence（値の検証は元の型が持つ）。"""
    base = confidence_policy()
    updates = {
        name: getattr(base, name).model_copy(update={"value": value})
        for name, value in values.items()
    }
    return base.model_copy(update=updates)


def feed(
    monitor: ResidualDriftMonitor,
    ts_ms: int,
    values: dict[str, float | None],
    quality: Quality = Quality.OK,
) -> None:
    monitor.observe(
        ts_ms,
        {
            metric: ResidualObservation(value=value, quality=quality)
            for metric, value in values.items()
        },
    )


def latest(monitor: ResidualDriftMonitor) -> ResidualEvidence:
    """最後に観測した時刻で見た証拠。"""
    clock = monitor._clock_ms
    assert clock is not None
    return monitor.evidence(clock)


def component(assessment: ConfidenceAssessment, name: ConfidenceComponent):
    return next(item for item in assessment.components if item.component is name)


# ---------------------------------------------------------------- Profile


def test_profile_is_deterministic_bound_to_the_model_and_train_only(trained) -> None:
    data, parts, model, profile = trained
    again = fit_confidence_profile(model, data, parts, profile_spec())

    assert again.canonical_bytes() == profile.canonical_bytes()
    assert ModelConfidenceProfile.model_validate_json(profile.canonical_bytes()) == profile
    assert profile.binding.model_version == "0.1.0"
    assert profile.binding.training_split_sha256 == model.manifest.training_split_sha256
    assert profile.train_example_count == len(parts.train)
    # 範囲は train だけから作る（train は index 0..23 で cycle 0..7 を全て含む）
    air = profile.feature_ranges[0]
    assert (air.minimum, air.maximum) == (19.5, 27.0)
    assert profile.missing_patterns[0].unavailable_metrics == ()
    assert profile.residual_scales[0].validation_samples == len(parts.validation)


def test_profile_rejects_a_split_or_dataset_the_model_did_not_learn(trained) -> None:
    data, parts, model, _profile = trained
    other = DatasetSplit(
        train=parts.train[:-1],
        validation=parts.validation,
        test=parts.test,
        purged=(*parts.purged, parts.train[-1]),
    )
    with pytest.raises(ValueError, match="split"):
        fit_confidence_profile(model, data, other, profile_spec())

    shorter = ThermalDataset(
        manifest=data.manifest.model_copy(
            update={
                "examples_sha256": examples_sha256(data.examples[:-1]),
                "example_count": EXAMPLES - 1,
            }
        ),
        examples=data.examples[:-1],
    )
    with pytest.raises(ValueError, match="dataset"):
        fit_confidence_profile(model, shorter, parts, profile_spec())


def test_profile_axis_must_be_a_feature_or_fan_zone(trained) -> None:
    data, parts, model, _profile = trained
    spec = ConfidenceProfileSpec(
        support_axes=(SupportAxis(source="air.room", edges=(20.0,)),),
        residual_scale_floor=0.01,
    )
    with pytest.raises(ValidationError, match="support 軸"):
        fit_confidence_profile(model, data, parts, spec)
    with pytest.raises(ValidationError, match="狭義単調増加"):
        SupportAxis(source=AIR, edges=(22.0, 22.0))


# ---------------------------------------------------------------- Assessment


def test_in_distribution_replay_is_not_ood_and_assessment_is_deterministic(trained) -> None:
    _data, parts, model, profile = trained
    judge = assessor(profile)
    observed = ObservedThermalInput.from_example(parts.test[0])
    prediction = model.predict(observed)

    first = judge.assess(observed, prediction, evidence(profile, 1.0, at=observed.action_ts_ms))
    assert first == judge.assess(
        observed, prediction, evidence(profile, 1.0, at=observed.action_ts_ms)
    )
    assert first.ood is False
    assert first.profile_sha256 == profile.sha256()
    # uncertainty が無い間は cap_without_uncertainty（0.9）で抑える
    assert first.confidence <= 0.9
    assert component(first, ConfidenceComponent.UNCERTAINTY).cap == 0.9


@pytest.mark.parametrize(
    ("case", "expected_component"),
    [
        ("air_above_range", ConfidenceComponent.FEATURE_RANGE),
        ("gpu_below_range", ConfidenceComponent.FEATURE_RANGE),
        ("fan_above_range", ConfidenceComponent.FAN_STATE_RANGE),
        ("unseen_joint_region", ConfidenceComponent.SUPPORT),
        ("unseen_missing_pattern", ConfidenceComponent.MISSING_PATTERN),
        ("support_axis_missing_at_action", ConfidenceComponent.SUPPORT),
    ],
)
def test_synthetic_out_of_training_inputs_are_ood(
    trained, case: str, expected_component: ConfidenceComponent
) -> None:
    _data, parts, model, profile = trained
    base = ObservedThermalInput.from_example(parts.test[0])
    observed = {
        "air_above_range": lambda: shifted(base, air=35.0),
        "gpu_below_range": lambda: shifted(base, gpu=20.0),
        "fan_above_range": lambda: with_fan(base, 0.95),
        # air も gpu も単独では学習範囲内。組み合わせ（室温が高く GPU が低い）が未経験
        "unseen_joint_region": lambda: shifted(base, air=27.0, gpu=40.0),
        "unseen_missing_pattern": lambda: with_missing(base, GPU),
        "support_axis_missing_at_action": lambda: _missing_at_action(base, AIR),
    }[case]()
    prediction = model.predict(observed)

    result = assessor(profile).assess(
        observed, prediction, evidence(profile, 1.0, at=observed.action_ts_ms)
    )

    assert result.ood is True
    assert result.confidence == 0.0
    assert expected_component in result.ood_components
    codes = {reason.code for reason in result.trace_reasons()}
    assert f"ood_{expected_component.value}" in codes


def _missing_at_action(observed: ObservedThermalInput, metric: str) -> ObservedThermalInput:
    last = observed.window[-1]
    missing = ObservedWindowFrame(
        ts_ms=last.ts_ms,
        values={**last.values, metric: None},
        source_ts_ms={**last.source_ts_ms, metric: None},
        missing_mask={**last.missing_mask, metric: True},
        stale_mask={**last.stale_mask, metric: False},
        suspect_mask={**last.suspect_mask, metric: False},
    )
    return observed.model_copy(update={"window": (*observed.window[:-1], missing)})


def test_small_excursion_inside_margin_lowers_confidence_without_ood(trained) -> None:
    _data, parts, model, profile = trained
    # air の学習範囲は 19.5..27.0（幅 7.5）。margin 0.1 = 0.75 まではみ出しを許す
    # test[7] は cycle 7（air 27 / gpu 54 / fan 0.65）。support cell は学習済みのまま
    observed = shifted(ObservedThermalInput.from_example(parts.test[7]), air=27.375)
    result = assessor(profile).assess(
        observed, model.predict(observed), evidence(profile, 1.0, at=observed.action_ts_ms)
    )

    assert result.ood is False
    range_score = component(result, ConfidenceComponent.FEATURE_RANGE).score
    assert range_score == pytest.approx(0.5)
    assert result.confidence == pytest.approx(0.5)


def test_model_version_mismatch_is_ood(trained) -> None:
    data, parts, _model, profile = trained
    other_model = train(data, parts, version="0.2.0")
    observed = ObservedThermalInput.from_example(parts.test[0])

    result = assessor(profile).assess(
        observed, other_model.predict(observed), evidence(profile, 1.0, at=observed.action_ts_ms)
    )

    assert result.ood is True
    detail = component(result, ConfidenceComponent.MODEL_BINDING).detail
    assert "model_version" in detail and "artifact_sha256" in detail


def test_input_that_does_not_match_the_feature_schema_fails_instead_of_guessing(trained) -> None:
    _data, parts, model, profile = trained
    observed = ObservedThermalInput.from_example(parts.test[0])
    prediction = model.predict(observed)
    shortened = observed.model_copy(update={"window": observed.window[1:]})
    with pytest.raises(ValidationError):
        assessor(profile).assess(shortened, prediction, None)


def test_confidence_is_capped_until_residual_evidence_exists(trained) -> None:
    _data, parts, model, profile = trained
    observed = ObservedThermalInput.from_example(parts.test[0])
    prediction = model.predict(observed)
    judge = assessor(profile)

    no_history = judge.assess(observed, prediction, None)
    few = judge.assess(
        observed, prediction, evidence(profile, 1.0, forecasts=4, at=observed.action_ts_ms)
    )
    enough = judge.assess(
        observed, prediction, evidence(profile, 1.0, forecasts=5, at=observed.action_ts_ms)
    )

    assert no_history.confidence == pytest.approx(0.7)
    assert few.confidence == pytest.approx(0.7)
    assert enough.confidence > 0.7


@pytest.mark.parametrize(
    ("ratio", "ood", "score"),
    [(0.5, False, 1.0), (1.0, False, 1.0), (2.0, False, 0.5), (3.0, True, 0.0), (9.0, True, 0.0)],
)
def test_residual_drift_is_scored_against_the_validation_scale(
    trained, ratio: float, ood: bool, score: float
) -> None:
    _data, parts, model, profile = trained
    observed = ObservedThermalInput.from_example(parts.test[0])
    result = assessor(profile).assess(
        observed, model.predict(observed), evidence(profile, ratio, at=observed.action_ts_ms)
    )

    drift = component(result, ConfidenceComponent.RESIDUAL_DRIFT)
    assert drift.ood is ood
    assert drift.score == pytest.approx(score)


def test_residual_drift_monitor_matches_predictions_with_later_observations(trained) -> None:
    _data, parts, model, profile = trained
    monitor = ResidualDriftMonitor(profile, confidence_policy())
    scale = profile.residual_scales[0].scale

    for example_ in parts.test[:5]:
        observed = ObservedThermalInput.from_example(example_)
        prediction = model.predict(observed)
        monitor.record(prediction)
        predicted = prediction.targets[0].values[GPU]
        # 許容幅（±500ms）の中で、validation の3倍の誤差で観測された
        feed(monitor, prediction.targets[0].expected_ts_ms + 200, {GPU: predicted + 3 * scale})

    observed = ObservedThermalInput.from_example(parts.test[5])
    state = monitor.evidence(observed.action_ts_ms)
    assert state.forecasts == 5
    assert state.ratio == pytest.approx(3.0)
    result = assessor(profile).assess(observed, model.predict(observed), state)
    assert ConfidenceComponent.RESIDUAL_DRIFT in result.ood_components

    late = model.predict(ObservedThermalInput.from_example(parts.test[6]))
    monitor.record(late)
    feed(monitor, late.targets[0].expected_ts_ms + 501, {GPU: 0.0})
    assert latest(monitor).unmatched == 1
    with pytest.raises(ValueError, match="巻き戻せない"):
        feed(monitor, 0, {GPU: 0.0})


def test_residual_drift_monitor_rejects_predictions_from_another_model(trained) -> None:
    data, parts, _model, profile = trained
    other = train(data, parts, version="0.2.0")
    monitor = ResidualDriftMonitor(profile, confidence_policy())
    with pytest.raises(ValueError, match="別のモデル"):
        monitor.record(other.predict(ObservedThermalInput.from_example(parts.test[0])))


# ---------------------------------------------------------------- Offline FP / FN


def test_false_positive_and_false_negative_are_measured_on_an_offline_dataset(trained) -> None:
    _data, parts, model, profile = trained
    in_distribution = replay_cases(parts.test, expected_ood=False)
    base = ObservedThermalInput.from_example(parts.test[0])
    synthetic = (
        OodEvaluationCase(label="hot-room", observed=shifted(base, air=35.0), expected_ood=True),
        OodEvaluationCase(
            label="joint", observed=shifted(base, air=27.0, gpu=40.0), expected_ood=True
        ),
        OodEvaluationCase(label="fan", observed=with_fan(base, 0.95), expected_ood=True),
        OodEvaluationCase(label="missing", observed=with_missing(base, AIR), expected_ood=True),
        # 評価側の誤りを数えられることを確かめる: 範囲内の入力に OOD の正解を付けた
        OodEvaluationCase(label="mislabeled", observed=base, expected_ood=True),
    )

    report = evaluate_ood_detection(model, assessor(profile), (*in_distribution, *synthetic))

    assert (report.true_negative, report.false_positive) == (len(parts.test), 0)
    assert report.false_positive_rate == 0.0
    assert (report.true_positive, report.false_negative) == (4, 1)
    assert report.false_negative_rate == pytest.approx(0.2)
    assert report.false_negative_labels == ("mislabeled",)
    assert report.ood_by_component["feature_range"] >= 1
    assert report.ood_by_component["missing_pattern"] == 1


# ---------------------------------------------------------------- Gate / Authority / trace


def test_confidence_level_follows_stage_thresholds_and_ood() -> None:
    config = policy()
    assert classify_confidence(config, confidence=0.9, ood=False, stage=AuthorityStage.FULL) is (
        ConfidenceLevel.HIGH
    )
    assert classify_confidence(config, confidence=0.8, ood=False, stage=AuthorityStage.FULL) is (
        ConfidenceLevel.MEDIUM
    )
    assert classify_confidence(
        config, confidence=0.65, ood=False, stage=AuthorityStage.EXPANDED
    ) is (ConfidenceLevel.LOW)
    assert classify_confidence(
        config, confidence=0.65, ood=False, stage=AuthorityStage.LIMITED
    ) is (ConfidenceLevel.MEDIUM)
    assert classify_confidence(config, confidence=1.0, ood=True, stage=AuthorityStage.FULL) is (
        ConfidenceLevel.LOW
    )


def _active_gate(stage: AuthorityStage, *, artifact: str = TEST_ARTIFACT_SHA256) -> ControllerGate:
    """**束縛した artifact も渡す**（#159）。Gate はこれと assessment の artifact を照らす。"""
    gate = gate_for(
        policy(authority=stage.value, recovery_hold_ms=1),
        expected_model_version="thermal-v1",
        expected_artifact_sha256=artifact,
    )
    _select(gate, 0, learned_proposal(0.7), artifact=artifact)
    return gate


_ATTACH = object()


def _select(
    gate: ControllerGate,
    now: int,
    proposal,
    assessment: object = _ATTACH,
    *,
    artifact: str = TEST_ARTIFACT_SHA256,
):
    """提案を Gate へ渡す。既定では同じ推論の assessment を付ける。"""
    attached = (
        assessment_for(proposal, artifact_sha256=artifact) if assessment is _ATTACH else assessment
    )
    return gate.select(
        now_mono_ms=now,
        fallback=fallback_proposal(0.4),
        learned=LearnedControlStatus(
            proposal=proposal,
            received_at_mono_ms=now,
            assessment=attached,  # type: ignore[arg-type]
            binding_authority_stage=AuthorityStage.FULL,
        ),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )


def test_high_confidence_at_full_authority_is_not_narrowed() -> None:
    gate = _active_gate(AuthorityStage.FULL)
    selected = _select(gate, 1, learned_proposal(0.9, confidence=0.9))

    assert selected.active_controller is ControllerKind.LEARNED_MPC
    assert selected.proposal.requested.front.demand == 0.9
    assert selected.model_gate is not None
    assert selected.model_gate.confidence_level is ConfidenceLevel.HIGH
    assert selected.model_gate.limits == ()


def test_medium_confidence_limits_change_width_and_minimum_demand() -> None:
    gate = _active_gate(AuthorityStage.FULL)
    up = _select(gate, 1, learned_proposal(0.9, confidence=0.8))
    down = _select(gate, 2, learned_proposal(0.1, confidence=0.8))

    # Fallback 0.4 を中心に、MEDIUM 帯（上 0.1 / 下 0.05）へ収める
    assert up.proposal.requested.front.demand == pytest.approx(0.5)
    assert down.proposal.requested.front.demand == pytest.approx(0.35)
    assert "medium_confidence_band" in down.proposal.requested.front.reason.detail
    assert down.model_gate is not None
    assert down.model_gate.confidence_level is ConfidenceLevel.MEDIUM
    assert down.model_gate.limits == (AuthorityLimitSource.MEDIUM_CONFIDENCE_BAND,)


def test_medium_band_intersects_the_stage_band() -> None:
    gate = _active_gate(AuthorityStage.LIMITED)
    selected = _select(gate, 1, learned_proposal(0.0, confidence=0.7))

    # LIMITED の帯（±0.1）と MEDIUM 帯（下 0.05）の狭い方。LIMITED は front だけを許可
    assert selected.proposal.requested.front.demand == pytest.approx(0.35)
    assert selected.proposal.requested.rear.demand == 0.4
    assert selected.model_gate is not None
    assert set(selected.model_gate.limits) == {
        AuthorityLimitSource.STAGE_BAND,
        AuthorityLimitSource.MEDIUM_CONFIDENCE_BAND,
        AuthorityLimitSource.STAGE_ZONE,
    }


def test_ood_assessment_switches_to_fallback_immediately_and_is_traced(trained) -> None:
    _data, parts, model, profile = trained
    judge = assessor(profile)
    gate = _active_gate(AuthorityStage.FULL)
    active = _select(gate, 1, learned_proposal(0.7))
    assert active.active_controller is ControllerKind.LEARNED_MPC

    observed = shifted(ObservedThermalInput.from_example(parts.test[0]), air=35.0)
    assessment = judge.assess(
        observed, model.predict(observed), evidence(profile, 1.0, at=observed.action_ts_ms)
    )
    # Registry を通った artifact の判定として提案へ付ける（offline のままでは付けられない）
    deployed = _deployed(assessment)
    proposal = deployed.apply_to(learned_proposal(0.1, inference_id=deployed.inference_id))
    selected = _select(gate, 2, proposal, deployed)

    assert selected.active_controller is ControllerKind.FALLBACK
    assert selected.fallback_reason is not None and selected.fallback_reason.code == "ood"
    # ML→Fallback の切替で requested を急に下げない（#79）
    assert selected.proposal.requested.front.demand == pytest.approx(0.7)
    gate_record = selected.model_gate
    assert gate_record is not None
    assert gate_record.ood is True and gate_record.learned_selected is False
    assert gate_record.inference_id == deployed.inference_id
    assert gate_record.confidence_level is ConfidenceLevel.LOW
    assert "ood_feature_range" in {reason.code for reason in gate_record.assessment}

    tick = _trace_tick(selected)
    restored = ControlTick.model_validate_json(tick.model_dump_json())
    assert restored.schema_version == 7
    assert restored.model_gate == gate_record
    assert json.loads(tick.model_dump_json())["state"]["model_ood"] is True


def test_offline_or_mismatched_assessment_cannot_be_attached_to_a_control_proposal(
    trained,
) -> None:
    _data, parts, model, profile = trained
    observed = ObservedThermalInput.from_example(parts.test[0])
    assessment = assessor(profile).assess(observed, model.predict(observed), None)
    matched = assessment.inference_id
    with pytest.raises(ValueError, match="Registry"):
        assessment.apply_to(learned_proposal(inference_id=matched))
    deployed = assessment.model_copy(
        update={
            "artifact_verification": ArtifactVerification.REGISTRY_VERIFIED,
            "model_version": "thermal-v1",
        }
    )
    with pytest.raises(ValueError, match="model_version"):
        deployed.apply_to(learned_proposal(version="other", inference_id=matched))
    with pytest.raises(ValueError, match="Learned MPC"):
        deployed.apply_to(fallback_proposal())
    accepted = deployed.apply_to(learned_proposal(confidence=0.0, inference_id=matched))
    assert accepted.confidence == deployed.confidence


def test_an_assessment_cannot_be_moved_to_a_proposal_from_another_inference(trained) -> None:
    """同じ model version でも、以前の in-distribution な判定を別の入力の提案に付けられない。"""
    _data, parts, model, profile = trained
    judge = assessor(profile)
    safe_input = ObservedThermalInput.from_example(parts.test[0])
    ood_input = shifted(safe_input, air=35.0)
    safe = judge.assess(safe_input, model.predict(safe_input), None)
    ood = judge.assess(ood_input, model.predict(ood_input), None)
    assert safe.ood is False and ood.ood is True
    assert safe.inference_id != ood.inference_id
    # 同じ action 時刻・同じ model でも、入力が違えば識別子が違う
    assert safe.input_action_ts_ms == ood.input_action_ts_ms

    deployed_safe = _deployed(safe)
    proposal_for_ood_input = learned_proposal(0.1, confidence=0.0, inference_id=ood.inference_id)
    with pytest.raises(ValueError, match="別の推論"):
        deployed_safe.apply_to(proposal_for_ood_input)

    # 別の推論の assessment を Gate へ渡しても authority は得られない
    gate = _active_gate(AuthorityStage.FULL)
    selected = _select(gate, 1, proposal_for_ood_input, deployed_safe)
    assert selected.active_controller is ControllerKind.FALLBACK
    assert selected.fallback_reason is not None
    assert selected.fallback_reason.code == "confidence_unattested"
    assert selected.model_gate is not None and selected.model_gate.assessment == ()


def test_inference_id_is_deterministic_and_input_bound(trained) -> None:
    _data, parts, model, _profile = trained
    first = ObservedThermalInput.from_example(parts.test[0])
    second = ObservedThermalInput.from_example(parts.test[1])
    prediction = model.predict(first)
    assert inference_id(first, prediction) == inference_id(first, model.predict(first))
    with pytest.raises(ValueError, match="同じ入力"):
        inference_id(second, prediction)


def test_confidence_types_cannot_carry_demand_or_pwm() -> None:
    """Confidence / OOD は requested も effective も作れない（AGENTS.md ルール2）。"""
    for model_type in (ConfidenceAssessment, ModelConfidenceProfile, ModelGateDecision):
        names = set(model_type.model_fields)
        assert not any("pwm" in name or "demand" in name for name in names), model_type


def test_v5_trace_requires_consistent_model_gate_for_learned_ticks() -> None:
    gate = _active_gate(AuthorityStage.FULL)
    selected = _select(gate, 1, learned_proposal(0.7, confidence=0.9))
    tick = _trace_tick(selected)
    assert tick.model_gate is not None and tick.model_gate.learned_selected

    payload = json.loads(tick.model_dump_json())
    del payload["model_gate"]
    with pytest.raises(ValidationError, match="model_gate が要る"):
        ControlTick.model_validate_json(json.dumps(payload))

    payload = json.loads(tick.model_dump_json())
    payload["schema_version"] = 4
    with pytest.raises(ValidationError, match="schema version 5"):
        ControlTick.model_validate_json(json.dumps(payload))

    payload = json.loads(tick.model_dump_json())
    payload["model_gate"]["confidence"] = 0.5
    with pytest.raises(ValidationError, match="揃える"):
        ControlTick.model_validate_json(json.dumps(payload))

    reason = reasons(ConfidenceComponent.MODEL_BINDING)
    plain = reasons()
    with pytest.raises(ValidationError, match="LOW"):
        ModelGateDecision(
            model_version="thermal-v1",
            inference_id="c" * 64,
            attested=True,
            confidence=0.0,
            ood=True,
            confidence_level=ConfidenceLevel.MEDIUM,
            authority_stage=AuthorityStage.FULL,
            learned_selected=False,
            assessment=reason,
        )
    with pytest.raises(ValidationError, match="MEDIUM 帯"):
        ModelGateDecision(
            model_version="thermal-v1",
            inference_id="c" * 64,
            attested=True,
            confidence=0.8,
            ood=False,
            confidence_level=ConfidenceLevel.MEDIUM,
            authority_stage=AuthorityStage.FULL,
            learned_selected=True,
            assessment=plain,
        )


def _trace_tick(selection) -> ControlTick:
    gate = selection.model_gate
    assert gate is not None
    zones = {}
    for zone_name in ("front", "rear", "top"):
        request = getattr(selection.proposal.requested, zone_name)
        zones[zone_name] = ZoneRecord(
            controller_reason=request.reason,
            demand=EffectiveZoneDemand(
                requested=request.demand,
                effective=request.demand,
                bound_by=BoundBy.REQUESTED,
                safety_floor=0.0,
                forced_max=False,
            ),
        )
    return ControlTick(
        tick_id=1,
        ts_ms=1_000,
        state=ControlState(
            operating_mode=OperatingMode.AUTO,
            authority_stage=gate.authority_stage,
            active_controller=selection.active_controller,
            safety_state=SafetyState.NORMAL,
            fallback_active=selection.fallback_active,
            fallback_reason=selection.fallback_reason,
            model_version=gate.model_version,
            model_confidence=gate.confidence,
            model_ood=gate.ood,
        ),
        zones=PerZone(**zones),
        model_gate=gate,
    )


# ------------------------------------------------ Residual の数え方と照合（#149 review）


@pytest.fixture(scope="module")
def multi_output():
    """2 horizon × 2 metric = 4 出力のモデル。"""
    data = dataset(horizons=(1_000, 2_000), targets=(GPU, AIR))
    parts = split(data)
    model = train(data, parts)
    profile = fit_confidence_profile(model, data, parts, profile_spec())
    return parts, model, profile


def at_action(observed: ObservedThermalInput, action_ts_ms: int) -> ObservedThermalInput:
    """同じ観測を別の action 時刻へずらした入力（forecast を何件も作るため）。"""
    delta = action_ts_ms - observed.action_ts_ms
    frames = tuple(
        frame.model_copy(
            update={
                "ts_ms": frame.ts_ms + delta,
                "source_ts_ms": {
                    metric: None if ts is None else ts + delta
                    for metric, ts in frame.source_ts_ms.items()
                },
            }
        )
        for frame in observed.window
    )
    return ObservedThermalInput.model_validate(
        observed.model_copy(update={"window": frames, "action_ts_ms": action_ts_ms}).model_dump(
            mode="python"
        )
    )


def complete_forecast(monitor: ResidualDriftMonitor, prediction, *, error: float = 0.0) -> None:
    """全出力を期待時刻ちょうどに観測する。"""
    for target in prediction.targets:
        feed(
            monitor,
            target.expected_ts_ms,
            {metric: value + error for metric, value in target.values.items()},
        )


def test_one_multi_output_forecast_counts_once_and_keeps_the_cap(multi_output) -> None:
    parts, model, profile = multi_output
    assert len(profile.residual_scales) == 4
    policy_ = confidence_policy()
    assert policy_.residual_min_samples.value == 5
    monitor = ResidualDriftMonitor(profile, policy_)
    base = ObservedThermalInput.from_example(parts.test[0])

    prediction = model.predict(base)
    monitor.record(prediction)
    complete_forecast(monitor, prediction)
    probe = at_action(base, base.action_ts_ms + 5_000)
    state = monitor.evidence(probe.action_ts_ms)
    # 4出力を照合しても forecast は1件。5件に達しないので上限は外れない
    assert state.forecasts == 1
    result = assessor(profile).assess(probe, model.predict(probe), state)
    assert result.confidence == pytest.approx(policy_.cap_before_residual_evidence.value)
    assert component(result, ConfidenceComponent.RESIDUAL_DRIFT).cap is not None

    for step in range(1, 5):
        shifted_input = at_action(base, base.action_ts_ms + step * 10_000)
        later = model.predict(shifted_input)
        monitor.record(later)
        complete_forecast(monitor, later)
    probe = at_action(base, base.action_ts_ms + 45_000)
    state = monitor.evidence(probe.action_ts_ms)
    assert state.forecasts == 5
    lifted = assessor(profile).assess(probe, model.predict(probe), state)
    assert component(lifted, ConfidenceComponent.RESIDUAL_DRIFT).cap is None


def test_residual_window_is_measured_in_forecasts(multi_output) -> None:
    parts, model, profile = multi_output
    # 鮮度で落ちないよう max_age を長くし、window の効果だけを見る
    policy_ = tuned(residual_max_age_ms=10_000_000)
    window = policy_.residual_window.value
    monitor = ResidualDriftMonitor(profile, policy_)
    base = ObservedThermalInput.from_example(parts.test[0])
    scale = {(item.horizon_ms, item.metric): item.scale for item in profile.residual_scales}

    for step in range(window + 5):
        prediction = model.predict(at_action(base, base.action_ts_ms + step * 10_000))
        monitor.record(prediction)
        # 古い5件だけ大きく外す。window が forecast 単位なら、その5件は押し出される
        factor = 10.0 if step < 5 else 1.0
        for target in prediction.targets:
            feed(
                monitor,
                target.expected_ts_ms,
                {
                    metric: value + factor * scale[(target.horizon_ms, metric)]
                    for metric, value in target.values.items()
                },
            )

    state = latest(monitor)
    assert state.forecasts == window
    assert state.ratio == pytest.approx(1.0)


def test_a_forecast_with_an_unmatched_output_is_not_evidence(multi_output) -> None:
    parts, model, profile = multi_output
    monitor = ResidualDriftMonitor(profile, confidence_policy())
    prediction = model.predict(ObservedThermalInput.from_example(parts.test[0]))
    monitor.record(prediction)
    first, second = prediction.targets
    feed(monitor, first.expected_ts_ms, dict(first.values))
    # 2つ目の horizon は GPU だけ観測し、AIR は許容幅を過ぎても来ない
    feed(monitor, second.expected_ts_ms, {GPU: second.values[GPU]})
    feed(monitor, second.expected_ts_ms + 501, {})

    state = latest(monitor)
    assert (state.forecasts, state.unmatched) == (0, 1)


def _single_target(trained):
    _data, parts, model, profile = trained
    monitor = ResidualDriftMonitor(profile, confidence_policy())
    prediction = model.predict(ObservedThermalInput.from_example(parts.test[0]))
    monitor.record(prediction)
    target = prediction.targets[0]
    scale = profile.residual_scales[0].scale
    return monitor, target.expected_ts_ms, target.values[GPU], scale


@pytest.mark.parametrize(
    ("observations", "expected_ratio"),
    [
        # 期待時刻の前（-100）が後（+300）より近い → 前を採る
        (((-100, 2.0), (300, 0.0)), 2.0),
        # 後（+100）が前（-400）より近い → 後を採る
        (((-400, 0.0), (100, 2.0)), 2.0),
        # 同距離（-200 / +200）→ 過去側（決定記録 0031 §2.2）
        (((-200, 2.0), (200, 0.0)), 2.0),
        # 後ろ側で値の無い観測は候補にせず、次の値のある観測を比べる
        (((-400, 0.0), (100, None), (200, 2.0)), 2.0),
    ],
)
def test_nearest_observation_within_tolerance_on_both_sides(
    trained, observations, expected_ratio: float
) -> None:
    monitor, expected_ts, predicted, scale = _single_target(trained)
    for offset, error in observations:
        feed(
            monitor,
            expected_ts + offset,
            {GPU: None if error is None else predicted + error * scale},
        )
    feed(monitor, expected_ts + 501, {})

    state = latest(monitor)
    assert state.forecasts == 1
    assert state.ratio == pytest.approx(expected_ratio)


def test_observations_outside_the_tolerance_expire_the_forecast(trained) -> None:
    monitor, expected_ts, predicted, _scale = _single_target(trained)
    feed(monitor, expected_ts - 501, {GPU: predicted})
    feed(monitor, expected_ts + 501, {GPU: predicted})

    state = latest(monitor)
    assert (state.forecasts, state.unmatched) == (0, 1)
    assert state.ratio is None


def test_the_same_action_cannot_be_counted_twice(trained) -> None:
    _data, parts, model, profile = trained
    monitor = ResidualDriftMonitor(profile, confidence_policy())
    prediction = model.predict(ObservedThermalInput.from_example(parts.test[0]))
    monitor.record(prediction)
    with pytest.raises(ValueError, match="狭義単調増加"):
        monitor.record(prediction)


def test_residual_evidence_from_another_profile_cannot_lift_the_cap(trained, multi_output) -> None:
    _data, parts, model, profile = trained
    _parts, _model, other_profile = multi_output
    observed = ObservedThermalInput.from_example(parts.test[0])
    with pytest.raises(ValueError, match="Profile と一致しない"):
        assessor(profile).assess(
            observed,
            model.predict(observed),
            evidence(other_profile, 1.0, at=observed.action_ts_ms),
        )


@pytest.mark.parametrize(("tolerance", "accepted"), [(999, True), (1_000, False), (1_500, False)])
def test_residual_tolerance_must_stay_below_the_shortest_horizon(
    trained, tolerance: int, accepted: bool
) -> None:
    """DatasetSpec と同じく、許容幅が action 時刻に届く設定を拒否する。"""
    _data, _parts, _model, profile = trained
    assert profile.target_schema.horizons_ms[0] == 1_000
    base = confidence_policy()
    window = base.residual_match_tolerance_ms.model_copy(update={"value": tolerance})
    settings = base.model_copy(update={"residual_match_tolerance_ms": window})
    if accepted:
        ResidualDriftMonitor(profile, settings)
    else:
        with pytest.raises(ValueError, match="最短 horizon"):
            ResidualDriftMonitor(profile, settings)


def test_residual_evidence_counted_with_other_settings_is_rejected(trained) -> None:
    """不変条件 R3: 証拠は同じ Profile・同じ window / 許容幅 / 鮮度で数えたものだけ。"""
    _data, parts, model, profile = trained
    observed = ObservedThermalInput.from_example(parts.test[0])
    fresh = evidence(profile, 1.0, at=observed.action_ts_ms)
    for name, value in (("residual_window", 50), ("match_tolerance_ms", 400), ("max_age_ms", 1)):
        other = fresh.model_copy(update={name: value})
        with pytest.raises(ValueError, match="window / 許容幅 / 鮮度"):
            assessor(profile).assess(observed, model.predict(observed), other)
    with pytest.raises(ValidationError, match="window を超えない"):
        ResidualEvidence(
            profile_sha256=profile.sha256(),
            residual_window=3,
            match_tolerance_ms=500,
            max_age_ms=60_000,
            as_of_ts_ms=0,
            forecasts=3,
            unmatched=1,
            ratio=1.0,
        )


# ------------------------------------------------ authority の不変条件（#149 review 3回目）
#
# Fallback より上の authority（Learned MPC の requested）には、次をすべて満たす必要がある。
# A1 提案の confidence / ood は、この推論（inference_id）の Registry 検証済み assessment と一致する
# A2 assessment は OOD でなく、confidence が stage の下限以上
# R1 HIGH（帯なし）には、鮮度のある residual 証拠（OK の観測と照合できた forecast）が
#    residual_min_samples 件以上要る。無ければ confidence は cap_before_residual_evidence
#    （< high_min_confidence）に抑えられる
# R2 window は解決した forecast を順に数える。照合できなかった forecast も枠を占める。
#    解決から residual_max_age_ms を過ぎた証拠は数えない
# R3 証拠は同じ Profile・設定で、この推論の action 時刻に見たもの
# R4 照合は OK の値だけ・±許容幅の最近傍・許容幅 < 最短 horizon・同じ action を2回数えない
# C1 設定の相互条件（high >= gate full、cap_before < high、min <= window、max_age > 許容幅）


def _good_forecasts(monitor, model, base, count: int, start_step: int = 0) -> int:
    """誤差 0 で照合できる forecast を count 件作り、最後の観測時刻を返す。"""
    last = 0
    for step in range(start_step, start_step + count):
        prediction = model.predict(at_action(base, base.action_ts_ms + step * 10_000))
        monitor.record(prediction)
        for target in prediction.targets:
            feed(monitor, target.expected_ts_ms, dict(target.values))
            last = target.expected_ts_ms
    return last


def test_r2_unmatched_forecasts_push_old_good_evidence_out_of_the_window(trained) -> None:
    _data, parts, model, profile = trained
    settings = tuned(residual_window=5, residual_max_age_ms=10_000_000)
    monitor = ResidualDriftMonitor(profile, settings)
    base = ObservedThermalInput.from_example(parts.test[0])
    last = _good_forecasts(monitor, model, base, 5)
    assert monitor.evidence(last).forecasts == 5

    # 以後の5件は target が来ない（許容幅を過ぎる）
    for step in range(5, 10):
        prediction = model.predict(at_action(base, base.action_ts_ms + step * 10_000))
        monitor.record(prediction)
        last = prediction.targets[0].expected_ts_ms + 501
        feed(monitor, last, {})
    state = monitor.evidence(last)
    assert (state.forecasts, state.unmatched, state.ratio) == (0, 5, None)


def test_r2_evidence_expires_by_age_even_without_new_forecasts(trained) -> None:
    _data, parts, model, profile = trained
    settings = tuned(residual_max_age_ms=60_000)
    monitor = ResidualDriftMonitor(profile, settings)
    base = ObservedThermalInput.from_example(parts.test[0])
    last = _good_forecasts(monitor, model, base, 5)
    assert monitor.evidence(last + 60_000).forecasts >= 1
    stale = monitor.evidence(last + 60_001)
    assert stale.forecasts < 5
    probe = at_action(base, last + 200_000)
    aged = monitor.evidence(probe.action_ts_ms)
    assert aged.forecasts == 0
    result = assessor(profile).assess(probe, model.predict(probe), aged)
    assert component(result, ConfidenceComponent.RESIDUAL_DRIFT).cap is not None


def test_r2_pending_forecasts_past_the_tolerance_expire_when_evidence_is_read(trained) -> None:
    _data, parts, model, profile = trained
    monitor = ResidualDriftMonitor(profile, confidence_policy())
    prediction = model.predict(ObservedThermalInput.from_example(parts.test[0]))
    monitor.record(prediction)
    # 観測が止まっても、証拠を読んだ時点で許容幅を過ぎた forecast は expired になる
    state = monitor.evidence(prediction.targets[0].expected_ts_ms + 501)
    assert (state.forecasts, state.unmatched) == (0, 1)


@pytest.mark.parametrize("quality", [Quality.STALE, Quality.SUSPECT, Quality.MISSING])
def test_r4_only_ok_observations_count_as_evidence(trained, quality: Quality) -> None:
    monitor, expected_ts, predicted, _scale = _single_target(trained)
    feed(monitor, expected_ts, {GPU: predicted}, quality=quality)
    state = monitor.evidence(expected_ts + 501)
    assert (state.forecasts, state.unmatched) == (0, 1)


def test_r3_evidence_must_be_read_at_this_inference_action_time(trained) -> None:
    _data, parts, model, profile = trained
    monitor = ResidualDriftMonitor(profile, confidence_policy())
    base = ObservedThermalInput.from_example(parts.test[0])
    last = _good_forecasts(monitor, model, base, 5)
    old_state = monitor.evidence(last)
    later = at_action(base, last + 5_000)
    with pytest.raises(ValueError, match="action 時刻"):
        assessor(profile).assess(later, model.predict(later), old_state)
    with pytest.raises(ValueError, match="巻き戻せない"):
        monitor.evidence(last - 1)


def test_r1_no_high_confidence_without_fresh_residual_evidence(trained) -> None:
    _data, parts, model, profile = trained
    config = policy()
    observed = ObservedThermalInput.from_example(parts.test[7])
    result = assessor(profile).assess(observed, model.predict(observed), None)
    assert result.confidence < config.model_confidence.high_min_confidence.value
    level = classify_confidence(
        config, confidence=result.confidence, ood=result.ood, stage=AuthorityStage.FULL
    )
    assert level is not ConfidenceLevel.HIGH


def _deployed(assessment: ConfidenceAssessment) -> ConfidenceAssessment:
    """offline で作った判定を、**配線した production の束縛のもの**として扱う。

    `artifact_sha256` も Gate が束縛した artifact に揃える（#159）。揃えないと Gate は
    `model_artifact_mismatch` で退ける。揃えるのはここが「production へ配線した」
    状況を作る helper だからで、**Gate 側の照合そのものは別の試験で破りにいく**。
    """
    # **予測ごと差し替えて、識別子を作り直す**（#159）。欄だけを書き換えたものは型が拒む。
    prediction = assessment.prediction.model_copy(
        update={
            "artifact_sha256": TEST_ARTIFACT_SHA256,
            "artifact_verification": ArtifactVerification.REGISTRY_VERIFIED,
        }
    )
    return ConfidenceAssessment.model_validate(
        {
            **assessment.model_dump(mode="python"),
            "artifact_verification": ArtifactVerification.REGISTRY_VERIFIED,
            "model_version": "thermal-v1",
            "artifact_sha256": TEST_ARTIFACT_SHA256,
            "prediction": prediction.model_dump(mode="python"),
            "inference_id": derive_inference_id(assessment.input_sha256, prediction),
        }
    )


@pytest.mark.parametrize(
    "violation",
    [
        "missing_assessment",
        "another_inference",
        "offline_artifact",
        "another_model_version",
        "proposal_confidence_raised",
        "proposal_hides_ood",
        "forged_components",
    ],
)
def test_a1_learned_confidence_requires_a_matching_verified_assessment(
    trained, violation: str
) -> None:
    """不変条件 A1: 提案の値だけでは authority を得られない。どの違反も Fallback になる。"""
    _data, parts, model, profile = trained
    base = ObservedThermalInput.from_example(parts.test[7])
    observed = base
    prediction = model.predict(observed)
    assessment = _deployed(
        assessor(profile).assess(
            observed, prediction, evidence(profile, 1.0, at=observed.action_ts_ms)
        )
    )
    assert assessment.ood is False and assessment.confidence >= 0.6
    proposal = learned_proposal(
        0.9, confidence=assessment.confidence, inference_id=assessment.inference_id
    )
    attached: object = assessment
    if violation == "missing_assessment":
        attached = None
    elif violation == "another_inference":
        other = at_action(observed, observed.action_ts_ms + 1_000)
        attached = _deployed(
            assessor(profile).assess(
                other, model.predict(other), evidence(profile, 1.0, at=other.action_ts_ms)
            )
        )
    elif violation == "offline_artifact":
        # 検証状態は予測にも入るので、予測ごと offline にして識別子を作り直す（#159）。
        offline_prediction = assessment.prediction.model_copy(
            update={"artifact_verification": ArtifactVerification.OFFLINE_UNVERIFIED}
        )
        attached = ConfidenceAssessment.model_validate(
            {
                **assessment.model_dump(mode="python"),
                "artifact_verification": ArtifactVerification.OFFLINE_UNVERIFIED,
                "prediction": offline_prediction.model_dump(mode="python"),
                "inference_id": derive_inference_id(assessment.input_sha256, offline_prediction),
            }
        )
        proposal = proposal.model_copy(update={"inference_id": attached.inference_id})
    elif violation == "another_model_version":
        proposal = proposal.model_copy(update={"model_version": "thermal-v1"})
        attached = assessment.model_copy(update={"model_version": "thermal-v0"})
    elif violation == "proposal_confidence_raised":
        proposal = proposal.model_copy(update={"confidence": 1.0})
    elif violation == "proposal_hides_ood":
        ood_input = shifted(observed, air=35.0)
        ood_assessment = _deployed(
            assessor(profile).assess(ood_input, model.predict(ood_input), None)
        )
        proposal = learned_proposal(0.9, confidence=0.95, inference_id=ood_assessment.inference_id)
        attached = ood_assessment
    elif violation == "forged_components":
        # 検証を通らない形で値だけ書き換えた assessment は、Gate へ渡す status を作れない
        forged = assessment.model_copy(update={"confidence": 1.0})
        with pytest.raises(ValidationError, match="最小値"):
            LearnedControlStatus(
                proposal=proposal.model_copy(update={"confidence": 1.0}),
                received_at_mono_ms=0,
                assessment=forged,
                binding_authority_stage=AuthorityStage.FULL,
            )
        return

    gate = gate_for(
        policy(authority="full", recovery_hold_ms=1), expected_model_version="thermal-v1"
    )
    first = _select(gate, 0, proposal, attached)
    second = _select(gate, 1, proposal, attached)
    for selected in (first, second):
        assert selected.active_controller is ControllerKind.FALLBACK, violation
        assert selected.fallback_reason is not None
        assert selected.fallback_reason.code in {"confidence_unattested", "ood"}


def test_a1_matching_assessment_is_accepted(trained) -> None:
    _data, parts, model, profile = trained
    observed = ObservedThermalInput.from_example(parts.test[7])
    assessment = _deployed(
        assessor(profile).assess(
            observed, model.predict(observed), evidence(profile, 1.0, at=observed.action_ts_ms)
        )
    )
    proposal = assessment.apply_to(
        learned_proposal(0.9, confidence=0.0, inference_id=assessment.inference_id)
    )
    gate = gate_for(
        policy(authority="limited", recovery_hold_ms=1), expected_model_version="thermal-v1"
    )
    _select(gate, 0, proposal, assessment)
    selected = _select(gate, 1, proposal, assessment)
    assert selected.active_controller is ControllerKind.LEARNED_MPC
    assert selected.model_gate is not None
    assert selected.model_gate.assessment == assessment.trace_reasons()


def test_c1_residual_age_must_exceed_the_match_tolerance(tmp_path) -> None:
    del tmp_path
    with pytest.raises(ValidationError, match="residual_max_age_ms"):
        tuned(residual_max_age_ms=500).model_validate(
            tuned(residual_max_age_ms=500).model_dump(mode="python")
        )


def test_confidence_layer_stays_upstream_of_guard_and_safety() -> None:
    """Confidence / OOD は requested までで、Guard・Critical Safety を迂回しない。

    危険温度への対応は confidence に関係なく Reactive Guard（#80）と Critical Safety（#78）が
    決定論的に行う（決定記録 0050 §2.3 / 0028 §2.4）。ここでは静的な依存で確かめる。
    """
    sources = {
        name: (Path(module.__file__).read_text(encoding="utf-8"))
        for name, module in (
            ("confidence", confidence_module),
            ("gate", gate_module),
        )
        if module.__file__ is not None
    }
    forbidden = ("control.hardware", "control.safety", "control.reactive", "hwmon", "pwm")
    for name, source in sources.items():
        for token in forbidden:
            assert f"import {token}" not in source, (name, token)
            assert f"from coldaisle.{token}" not in source, (name, token)

    # Gate が返せるのは requested までで、effective / PWM を表す型を持たない
    assert "effective" not in set(ControllerSelection.model_fields)
    assert set(ControllerProposal.model_fields) & {"effective", "pwm", "pwm_raw"} == set()


def _assessed(trained, *, ood_input: bool):
    """Registry 検証済みの assessment と、その推論に対する素の提案を作る。"""
    _data, parts, model, profile = trained
    base = ObservedThermalInput.from_example(parts.test[7])
    observed = shifted(base, air=35.0) if ood_input else base
    assessment = _deployed(
        assessor(profile).assess(
            observed,
            model.predict(observed),
            evidence(profile, 1.0, at=observed.action_ts_ms),
        )
    )
    assert assessment.ood is ood_input
    return assessment


def test_trace_records_the_verified_assessment_not_the_proposal_claim(trained) -> None:
    """不変条件 T1: trace には検証できた値だけを残す（OOD を HIGH に見せない）。"""
    assessment = _assessed(trained, ood_input=True)
    # 提案は「OOD ではない・高 confidence」と自称する
    liar = learned_proposal(0.9, confidence=0.99, ood=False, inference_id=assessment.inference_id)
    gate = _active_gate(AuthorityStage.FULL)
    selected = _select(gate, 1, liar, assessment)

    assert selected.active_controller is ControllerKind.FALLBACK
    # 自称値の食い違いを先に見るので、理由は OOD ではなく不一致になる（trace と一貫させる）
    assert selected.fallback_reason is not None
    assert selected.fallback_reason.code == "confidence_unattested"
    record = selected.model_gate
    assert record is not None
    assert record.attested is True
    assert (record.confidence, record.ood) == (assessment.confidence, assessment.ood)
    assert record.confidence_level is ConfidenceLevel.LOW
    assert record.proposal_mismatch is not None
    assert "proposal_confidence=0.99" in record.proposal_mismatch.detail
    assert record.assessment == assessment.trace_reasons()


def test_trace_keeps_the_assessed_confidence_when_the_proposal_inflates_it(trained) -> None:
    assessment = _assessed(trained, ood_input=False)
    inflated = learned_proposal(0.9, confidence=1.0, inference_id=assessment.inference_id)
    gate = _active_gate(AuthorityStage.FULL)
    selected = _select(gate, 1, inflated, assessment)

    assert selected.fallback_reason is not None
    assert selected.fallback_reason.code == "confidence_unattested"
    record = selected.model_gate
    assert record is not None
    assert record.confidence == assessment.confidence != 1.0
    assert record.proposal_mismatch is not None
    assert record.learned_selected is False


def test_trace_marks_a_proposal_without_an_assessment_as_unattested(trained) -> None:
    assessment = _assessed(trained, ood_input=False)
    proposal = learned_proposal(0.9, confidence=0.99, inference_id=assessment.inference_id)
    gate = _active_gate(AuthorityStage.FULL)
    selected = _select(gate, 1, proposal, None)

    record = selected.model_gate
    assert record is not None
    assert record.attested is False
    assert (record.confidence, record.ood, record.assessment) == (None, None, ())
    assert record.confidence_level is ConfidenceLevel.LOW
    assert record.proposal_mismatch is None
    # 裏付けの無い tick では ControlState にも数値を残さない
    tick = _trace_tick(selected)
    payload = json.loads(tick.model_dump_json())
    assert payload["state"]["model_confidence"] is None
    assert payload["model_gate"]["attested"] is False
    payload["state"]["model_confidence"] = 0.99
    with pytest.raises(ValidationError, match="揃える"):
        ControlTick.model_validate_json(json.dumps(payload))


def test_trace_rejects_recording_numbers_without_attestation() -> None:
    with pytest.raises(ValidationError, match="attested"):
        ModelGateDecision(
            model_version="thermal-v1",
            inference_id="c" * 64,
            attested=False,
            confidence=0.9,
            ood=False,
            confidence_level=ConfidenceLevel.LOW,
            authority_stage=AuthorityStage.FULL,
            learned_selected=False,
        )
    with pytest.raises(ValidationError, match="裏付けの無い提案"):
        ModelGateDecision(
            model_version="thermal-v1",
            inference_id="c" * 64,
            attested=False,
            confidence_level=ConfidenceLevel.LOW,
            authority_stage=AuthorityStage.FULL,
            learned_selected=True,
        )


# --------------------------- Gate が作れない組み合わせは schema が拒む（#149 review 5回目）
#
# trace は Gate の外（保存済み JSON の読み込み、将来の別実装）からも組み立てられる。
# schema を最後の防波堤として、Gate が決して出さない組み合わせを型で拒む。


def reasons(*ood_components: ConfidenceComponent) -> tuple[Reason, ...]:
    """Gate が書く形（構成要素ごとに1つ）の理由。指定した構成要素だけ OOD にする。"""
    return tuple(
        Reason(
            code=(f"ood_{component.value}" if component in ood_components else component.value),
            detail="score=1.000000",
        )
        for component in ConfidenceComponent
    )


REASONS = reasons()


def gate_record(**changes: object) -> ModelGateDecision:
    """Gate が実際に出す形の記録（既定は裏付けのある HIGH / FULL）。"""
    fields: dict[str, object] = {
        "model_version": "thermal-v1",
        "inference_id": "c" * 64,
        "attested": True,
        "confidence": 0.9,
        "ood": False,
        "confidence_level": ConfidenceLevel.HIGH,
        "authority_stage": AuthorityStage.FULL,
        "learned_selected": False,
        "assessment": REASONS,
    }
    fields.update(changes)
    return ModelGateDecision.model_validate(fields)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        # 裏付けが無いのに、別の推論の理由が残っている
        (
            {
                "attested": False,
                "confidence": None,
                "ood": None,
                "confidence_level": ConfidenceLevel.LOW,
                "assessment": REASONS,
            },
            "assessment の理由を残さない",
        ),
        # 裏付けがあるのに理由が無い（Gate は必ず構成要素ごとの理由を書く）
        ({"assessment": ()}, "assessment の理由が要る"),
        # OOD なのに confidence が 0 でない
        (
            {
                "confidence": 0.5,
                "ood": True,
                "confidence_level": ConfidenceLevel.LOW,
                "assessment": "ood_feature_range",
            },
            "confidence は 0",
        ),
        # zone の制限だけが立っている（stage の帯なしには起きない）
        (
            {"learned_selected": True, "limits": (AuthorityLimitSource.STAGE_ZONE,)},
            "stage の帯と一緒",
        ),
        # FULL なのに stage の帯が掛かっている / FULL 未満なのに掛かっていない
        ({"learned_selected": True, "limits": (AuthorityLimitSource.STAGE_BAND,)}, "stage の帯"),
        (
            {"authority_stage": AuthorityStage.LIMITED, "learned_selected": True, "limits": ()},
            "stage の帯",
        ),
        # HIGH なのに MEDIUM 帯が掛かっている
        (
            {"learned_selected": True, "limits": (AuthorityLimitSource.MEDIUM_CONFIDENCE_BAND,)},
            "MEDIUM 帯",
        ),
        # 裏付けが無いのに Learned MPC を選んだ / 数値だけが残っている
        (
            {
                "attested": False,
                "confidence": None,
                "ood": None,
                "confidence_level": ConfidenceLevel.LOW,
                "assessment": (),
                "learned_selected": True,
            },
            "裏付けの無い提案",
        ),
        (
            {"attested": False, "confidence_level": ConfidenceLevel.LOW, "assessment": ()},
            "attested",
        ),
        # 自称値が assessment と違う提案は、Gate がその tick で Fallback へ落とす
        (
            {
                "learned_selected": True,
                "proposal_mismatch": Reason(code="proposal_mismatch", detail="claimed 1.0"),
            },
            "食い違う提案",
        ),
        # 裏付けがあるのに数値が欠けている / confidence と ood が揃っていない
        ({"confidence": None}, "attested"),
        ({"ood": None}, "一緒に記録"),
        # 同じ制限の根拠を2回書く
        (
            {
                "learned_selected": True,
                "confidence": 0.7,
                "confidence_level": ConfidenceLevel.MEDIUM,
                "limits": (
                    AuthorityLimitSource.MEDIUM_CONFIDENCE_BAND,
                    AuthorityLimitSource.MEDIUM_CONFIDENCE_BAND,
                ),
            },
            "重複させない",
        ),
        # MEDIUM なのに MEDIUM 帯が無い
        (
            {
                "learned_selected": True,
                "confidence": 0.7,
                "confidence_level": ConfidenceLevel.MEDIUM,
                "limits": (),
            },
            "MEDIUM 帯",
        ),
        # SHADOW で Learned MPC を選ぶ
        ({"authority_stage": AuthorityStage.SHADOW, "learned_selected": True}, "SHADOW"),
    ],
)
def test_model_gate_rejects_combinations_the_gate_never_produces(
    changes: dict[str, object], match: str
) -> None:
    prepared = dict(changes)
    marker = prepared.get("assessment")
    if marker == "ood_feature_range":
        prepared["assessment"] = reasons(ConfidenceComponent.FEATURE_RANGE)
    with pytest.raises(ValidationError, match=match):
        gate_record(**prepared)


def test_model_gate_accepts_the_shapes_the_gate_produces() -> None:
    assert gate_record().attested is True
    assert (
        gate_record(
            attested=False,
            confidence=None,
            ood=None,
            confidence_level=ConfidenceLevel.LOW,
            assessment=(),
        ).confidence
        is None
    )
    assert gate_record(learned_selected=True).learned_selected is True
    assert (
        gate_record(
            authority_stage=AuthorityStage.LIMITED,
            confidence_level=ConfidenceLevel.MEDIUM,
            confidence=0.7,
            learned_selected=True,
            limits=(
                AuthorityLimitSource.STAGE_BAND,
                AuthorityLimitSource.MEDIUM_CONFIDENCE_BAND,
                AuthorityLimitSource.STAGE_ZONE,
            ),
        ).learned_selected
        is True
    )


def _fallback_tick(**state_changes: object) -> dict[str, object]:
    gate = _active_gate(AuthorityStage.FULL)
    selection = _select(gate, 1, learned_proposal(0.7, confidence=0.9), None)
    tick = _trace_tick(selection)
    payload = json.loads(tick.model_dump_json())
    payload["state"].update(state_changes)
    return payload


@pytest.mark.parametrize(
    ("state_changes", "match"),
    [
        ({"model_confidence": 0.99, "model_ood": False}, "残さない"),
        ({"model_version": "thermal-v1"}, "残さない"),
    ],
)
def test_v5_tick_without_a_model_gate_cannot_keep_ml_numbers(
    state_changes: dict[str, object], match: str
) -> None:
    payload = _fallback_tick(**state_changes)
    del payload["model_gate"]
    with pytest.raises(ValidationError, match=match):
        ControlTick.model_validate_json(json.dumps(payload))

    without = _fallback_tick()
    del without["model_gate"]
    without["state"]["model_version"] = None
    without["state"]["model_confidence"] = None
    without["state"]["model_ood"] = None
    assert ControlTick.model_validate_json(json.dumps(without)).model_gate is None


def test_v5_tick_in_a_human_mode_cannot_carry_a_model_gate() -> None:
    payload = _fallback_tick()
    payload["state"]["operating_mode"] = "manual"
    payload["state"]["active_controller"] = None
    payload["state"]["fallback_active"] = False
    payload["state"]["fallback_reason"] = None
    with pytest.raises(ValidationError, match="MANUAL / CALIBRATION"):
        ControlTick.model_validate_json(json.dumps(payload))


def test_gate_never_emits_a_selected_proposal_with_a_mismatch(trained) -> None:
    """Gate 側でも、不一致のある提案は必ず Fallback になる（表の 8 行目の裏取り）。"""
    assessment = _assessed(trained, ood_input=False)
    inflated = learned_proposal(0.9, confidence=1.0, inference_id=assessment.inference_id)
    gate = _active_gate(AuthorityStage.FULL)
    selected = _select(gate, 1, inflated, assessment)
    record = selected.model_gate
    assert record is not None
    assert record.proposal_mismatch is not None
    assert record.learned_selected is False
    assert selected.active_controller is ControllerKind.FALLBACK


# ----------------- assessment の理由の集合を判定と突き合わせる（#149 review 6回目）


def test_schema_component_names_match_the_assessment_components() -> None:
    """schema 側の構成要素名は ConfidenceComponent と同じ集合にする。"""
    assert set(MODEL_GATE_ASSESSMENT_COMPONENTS) == {
        component.value for component in ConfidenceComponent
    }
    assert len(MODEL_GATE_ASSESSMENT_COMPONENTS) == len(ConfidenceComponent)


@pytest.mark.parametrize(
    ("assessment", "ood", "match"),
    [
        # 構成要素が足りない
        (reasons()[:-1], False, "足りない構成要素"),
        # 同じ構成要素を2回書く
        ((*reasons()[:-1], reasons()[0]), False, "構成要素ごとに1つ"),
        # 知らない code
        (
            (*reasons()[:-1], Reason(code="made_up_check", detail="")),
            False,
            "未知の構成要素",
        ),
        # ood_ の理由があるのに ood ではない
        (reasons(ConfidenceComponent.SUPPORT), False, "食い違って"),
        # ood なのに ood_ の理由が無い
        (reasons(), True, "食い違って"),
    ],
)
def test_attested_trace_rejects_reason_sets_that_do_not_match_the_verdict(
    assessment: tuple[Reason, ...], ood: bool, match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        gate_record(
            assessment=assessment,
            ood=ood,
            confidence=0.0 if ood else 0.9,
            confidence_level=ConfidenceLevel.LOW if ood else ConfidenceLevel.HIGH,
        )


def test_attested_trace_accepts_the_reason_set_the_gate_writes(trained) -> None:
    """Gate が実際に書く理由の集合は通る（OOD のときは ood_* を含む）。"""
    assert gate_record(assessment=reasons()).ood is False
    ood_record = gate_record(
        assessment=reasons(ConfidenceComponent.FEATURE_RANGE),
        ood=True,
        confidence=0.0,
        confidence_level=ConfidenceLevel.LOW,
    )
    assert ood_record.ood is True

    # Gate が本物の assessment から書いた記録も同じ規則を満たす
    assessment = _assessed(trained, ood_input=True)
    liar = learned_proposal(0.9, confidence=0.99, ood=False, inference_id=assessment.inference_id)
    gate = _active_gate(AuthorityStage.FULL)
    record = _select(gate, 1, liar, assessment).model_gate
    assert record is not None
    codes = {reason.code for reason in record.assessment}
    assert codes == {
        f"ood_{component.value}" if component in assessment.ood_components else component.value
        for component in ConfidenceComponent
    }


def test_a_proposal_only_ood_claim_is_recorded_as_a_mismatch_not_as_ood(trained) -> None:
    """assessment が OOD でないのに提案だけが OOD を名乗る tick（#149 review 7回目）。"""
    assessment = _assessed(trained, ood_input=False)
    claims_ood = learned_proposal(
        0.7, confidence=assessment.confidence, ood=True, inference_id=assessment.inference_id
    )
    gate = _active_gate(AuthorityStage.FULL)
    selected = _select(gate, 1, claims_ood, assessment)

    assert selected.active_controller is ControllerKind.FALLBACK
    assert selected.fallback_reason is not None
    assert selected.fallback_reason.code == "confidence_unattested"
    record = selected.model_gate
    assert record is not None
    # trace の判定は assessment 側（OOD ではない）。理由と食い違わない
    assert record.ood is False
    assert record.proposal_mismatch is not None
    assert "proposal_ood=True" in record.proposal_mismatch.detail
    assert not any(reason.code.startswith("ood_") for reason in record.assessment)


def test_matching_the_verdict_of_an_ood_assessment_is_still_reported_as_ood(trained) -> None:
    assessment = _assessed(trained, ood_input=True)
    honest = learned_proposal(
        0.7, confidence=assessment.confidence, ood=True, inference_id=assessment.inference_id
    )
    gate = _active_gate(AuthorityStage.FULL)
    selected = _select(gate, 1, honest, assessment)

    assert selected.fallback_reason is not None and selected.fallback_reason.code == "ood"
    record = selected.model_gate
    assert record is not None
    assert (record.ood, record.proposal_mismatch) == (True, None)


def test_a_forecast_resolved_long_after_its_deadline_is_not_fresh_evidence(trained) -> None:
    """照合は期限内でも、解決の処理が遅れた forecast を「新しい証拠」にしない。"""
    _data, parts, model, profile = trained
    settings = tuned(residual_max_age_ms=60_000)
    monitor = ResidualDriftMonitor(profile, settings)
    base = ObservedThermalInput.from_example(parts.test[0])
    prediction = model.predict(base)
    monitor.record(prediction)
    target = prediction.targets[0]

    # 期待時刻の手前（許容幅の中）に観測が来たが、次の観測はずっと後だった
    feed(monitor, target.expected_ts_ms - 100, {GPU: target.values[GPU]})
    late = target.expected_ts_ms + 10_000_000
    feed(monitor, late, {GPU: 0.0})

    state = monitor.evidence(late)
    # 照合自体はできているが、証拠の時刻は観測時刻（期限内）なので鮮度切れになる
    assert (state.forecasts, state.unmatched) == (0, 1)
    fresh = monitor.evidence(late)
    assert fresh.ratio is None


def test_an_expired_forecast_is_timestamped_at_its_deadline(trained) -> None:
    _data, parts, model, profile = trained
    settings = tuned(residual_max_age_ms=60_000)
    monitor = ResidualDriftMonitor(profile, settings)
    base = ObservedThermalInput.from_example(parts.test[0])
    prediction = model.predict(base)
    monitor.record(prediction)
    deadline = prediction.targets[0].expected_ts_ms + 500

    # 期限を大きく過ぎてから読んでも、その forecast は「いま解決した」扱いにしない
    state = monitor.evidence(deadline + 10_000_000)
    assert (state.forecasts, state.unmatched) == (0, 1)
