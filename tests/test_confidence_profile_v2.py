"""Confidence Profile v2 の生成・step ごとの support の照合・同梱 Profile からの判定器。

#85 / 決定記録 0079 段 3 / 0084 §2.1 / §2.2。実機なしで走る（合成 Dataset v2）。
"""

from __future__ import annotations

import hashlib
import inspect
import math
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coldaisle.control.model.counterfactual_confidence as profile_v2_module
from coldaisle.clock import SimulatedClock
from coldaisle.control.model.confidence import (
    ConfidenceComponent,
    ConfidenceProfileSpec,
    OodEvaluationCase,
    ResidualObservation,
    SupportAxis,
    ValueRange,
    evaluate_ood_detection,
)
from coldaisle.control.model.counterfactual import (
    ActionCellCount,
    ActionCellTransitionCount,
    ActionSupportV2,
    ActionTrajectory,
    AnchorTransitionSupport,
    ConfidenceProfileV2,
    CounterfactualThermalModelArtifact,
    RegistryCounterfactualThermalModel,
    StepActionSupport,
    StepTransitionSupport,
    ThermalActionSchema,
    canonical_counterfactual_artifact_bytes,
    counterfactual_registry_metadata,
    counterfactual_registry_metadata_json_bytes,
)
from coldaisle.control.model.counterfactual_confidence import (
    CounterfactualConfidenceAssessor,
    StepSupportChecker,
    SupportViolationKind,
    counterfactual_residual_monitor,
    fit_confidence_profile_v2,
    seal_counterfactual_artifact,
)
from coldaisle.control.model.counterfactual_training import (
    CounterfactualTrainedModel,
    VerifiedTrainingDatasetArtifactV2,
    recorded_trajectory,
    train_counterfactual_ridge,
)
from coldaisle.control.model.dataset import (
    ActionContext,
    ActionExclusionCounts,
    ActionStepV2,
    DatasetExampleV2,
    DatasetManifestV2,
    DatasetSourceKind,
    PriorAction,
    SourceRun,
    TargetFrame,
    ThermalDatasetV2,
    examples_sha256,
    split_temporally_v2,
)
from coldaisle.control.model.thermal import ObservedThermalInput, canonical_sha256
from coldaisle.control.model_registry import (
    ApprovalAction,
    ArtifactKind,
    ArtifactMetadata,
    HumanApproval,
    ModelCompatibility,
    ModelRegistry,
    load_model_registry_limits,
)
from coldaisle.control.schema import (
    AuthorityStage,
    ControllerKind,
    OperatingMode,
    PerZone,
    SafetyState,
    Zone,
)
from coldaisle.store.models import Quality
from test_fallback_controller import gate_for, learned_proposal, policy
from test_model_confidence import _select
from test_thermal_model_v2 import (
    ANCHOR_TICKS,
    CATALOG,
    CONFIG_DIR,
    HORIZONS,
    NOW_MS,
    RUN_ID,
    SHA_A,
    SHA_B,
    SOURCE_ID,
    STEP_MS,
    STEPS,
    TARGETS,
    _approval,
    action_zone,
    dataset_spec,
    gpu_core,
    load,
    published,
    split,
    training_spec,
    verified,
    window_frame,
)

Demands = Callable[[int], PerZone[float]]
FIRST_TRAIN_TICK = ANCHOR_TICKS[0]
VALIDATION_TICK = 31
"""validation の真ん中（purge の外）。この tick の demand を学習範囲の外に置く試験で使う。"""


# ------------------------------------------------ 合成 Dataset v2（demand を選べる）


def zones(front: float, rear: float = 0.3, top: float = 0.4) -> PerZone[float]:
    return PerZone(front=front, rear=rear, top=top)


def alternating(tick: int) -> PerZone[float]:
    """0.3 と 0.7（fan.front の境界 0.5 の両側）を交互に取る。"""
    return zones(0.3 if tick % 2 == 0 else 0.7)


def low_high_high(tick: int) -> PerZone[float]:
    """0.3, 0.7, 0.7 を繰り返す。cell の組は LOW→HIGH・HIGH→HIGH・HIGH→LOW だけ。"""
    return zones(0.3 if tick % 3 == 0 else 0.7)


def base(tick: int) -> PerZone[float]:
    """0.2〜0.45 の中を動く（境界 0.5 の下だけ）。"""
    return zones(round(0.2 + 0.05 * (tick % 6), 6))


def with_override(demand_of: Demands, overrides: dict[int, PerZone[float]]) -> Demands:
    def demand(tick: int) -> PerZone[float]:
        return overrides.get(tick, demand_of(tick))

    return demand


def targets(anchor_tick: int, horizon_ms: int, demand_of: Demands) -> dict[str, float | None]:
    anchor_ms = anchor_tick * 1_000
    step0 = demand_of(anchor_tick)
    last = demand_of(anchor_tick + horizon_ms // STEP_MS - 1)
    return {
        "cpu.package": 40.0
        + 0.5 * gpu_core(anchor_ms)
        - 6.0 * step0.front
        - 3.0 * last.front
        + horizon_ms / 1_000,
        "d.gpu_rise": 10.0 - 4.0 * step0.front + 0.2 * gpu_core(anchor_ms),
    }


def make_example(
    anchor_tick: int,
    demand_of: Demands,
    *,
    target_shift: float = 0.0,
) -> DatasetExampleV2:
    anchor_ms = anchor_tick * 1_000
    current = demand_of(anchor_tick)
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
            effective_demand=demand_of(anchor_tick - 1),
        ),
        action_steps=tuple(
            ActionStepV2(
                step=step,
                ts_ms=anchor_ms + step * STEP_MS,
                source_ts_ms=anchor_ms + step * STEP_MS,
                source_tick_id=anchor_tick + step,
                effective_demand=demand_of(anchor_tick + step),
            )
            for step in range(STEPS)
        ),
        targets=tuple(
            TargetFrame(
                horizon_ms=horizon,
                expected_ts_ms=anchor_ms + horizon,
                values={
                    metric: (None if value is None else value + target_shift)
                    for metric, value in targets(anchor_tick, horizon, demand_of).items()
                },
                source_ts_ms={metric: anchor_ms + horizon for metric in TARGETS},
                quality={metric: Quality.OK for metric in TARGETS},
                missing_mask={metric: False for metric in TARGETS},
            )
            for horizon in HORIZONS
        ),
    )


def make_dataset(
    demand_of: Demands, *, target_shift: dict[int, float] | None = None
) -> ThermalDatasetV2:
    shifts = target_shift or {}
    items = tuple(
        make_example(tick, demand_of, target_shift=shifts.get(tick, 0.0)) for tick in ANCHOR_TICKS
    )
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


def profile_spec(*, floor: float = 1e-6, front_edges: tuple[float, ...] = (0.5,)) -> Any:
    return ConfidenceProfileSpec(
        support_axes=(
            SupportAxis(source="fan.front", edges=front_edges),
            SupportAxis(source="fan.rear", edges=(0.5,)),
            SupportAxis(source="fan.top", edges=(0.5,)),
        ),
        residual_scale_floor=floor,
    )


class Fitted:
    """1つの合成 dataset から学習し、Profile v2 を作って封じ、Registry の検証経路で読んだ組。"""

    def __init__(
        self,
        tmp_path: Path,
        demand_of: Demands,
        *,
        target_shift: dict[int, float] | None = None,
        spec: ConfidenceProfileSpec | None = None,
        version: str = "0.1.0",
    ) -> None:
        self.dataset = make_dataset(demand_of, target_shift=target_shift)
        self.split = split(self.dataset)
        self.source = published(self.dataset, tmp_path)
        self.trained = train_counterfactual_ridge(
            self.source,
            self.split,
            training_spec(model_version=version),
            metric_catalog=CATALOG,
        )
        self.spec = profile_spec() if spec is None else spec
        self.profile = fit_confidence_profile_v2(self.trained, self.source, self.split, self.spec)
        self.artifact = seal_counterfactual_artifact(
            self.trained, self.source, self.split, self.spec
        )
        self.payload = canonical_counterfactual_artifact_bytes(self.artifact)
        self.model = load(verified(self.payload))

    def assessor(self) -> CounterfactualConfidenceAssessor:
        return CounterfactualConfidenceAssessor.for_model(self.model, policy().model_confidence)

    def observed(self, tick: int) -> ObservedThermalInput:
        example = next(item for item in self.dataset.examples if item.control_tick_id == tick)
        return ObservedThermalInput.from_example_v2(example)


def component(assessment: Any, name: ConfidenceComponent) -> Any:
    return next(item for item in assessment.components if item.component is name)


# ---------------------------------------------------------------- 生成


def test_profile_generation_is_deterministic_and_sealed_with_the_model(tmp_path: Path) -> None:
    first = Fitted(tmp_path / "a", base)
    second = Fitted(tmp_path / "b", base)
    assert first.profile == second.profile
    assert first.profile.canonical_bytes() == second.profile.canonical_bytes()
    assert first.payload == second.payload
    # 封じた artifact の Profile は生成した Profile そのもの。manifest がその hash を持つ
    assert first.artifact.confidence_profile == first.profile
    assert first.artifact.manifest.confidence_profile_sha256 == first.profile.sha256()
    assert first.model.confidence_profile == first.profile
    assert first.profile.binding == first.trained.profile_binding()
    assert first.profile.anchor_action_rule == "hold_effective"


def test_ranges_and_action_support_come_from_train_only(tmp_path: Path) -> None:
    reference = Fitted(tmp_path / "a", base)
    validation_ticks = {item.control_tick_id for item in reference.split.validation}
    test_ticks = {item.control_tick_id for item in (*reference.split.test, *reference.split.purged)}
    # test / purged の demand を学習範囲の外へ動かし、validation の label を外しても、範囲と
    # support は変わらない（validation の demand は residual の基準が残るように動かさない）
    train_ticks = {
        tick
        for item in reference.split.train
        for tick in range(item.control_tick_id - 1, item.control_tick_id + STEPS)
    }
    moved_ticks = test_ticks - validation_ticks - train_ticks
    assert moved_ticks
    moved = with_override(base, {tick: zones(0.95) for tick in moved_ticks})
    other = Fitted(tmp_path / "b", moved, target_shift={tick: 3.0 for tick in validation_ticks})
    assert other.profile.residual_scales != reference.profile.residual_scales
    for name in (
        "feature_ranges",
        "fan_ranges",
        "missing_patterns",
        "support_cells",
        "action_support",
        "train_example_count",
    ):
        assert getattr(other.profile, name) == getattr(reference.profile, name), name
    assert reference.profile.train_example_count == len(reference.split.train)
    step0 = reference.profile.action_support.steps[0]
    train_front = [item.action_steps[0].effective_demand.front for item in reference.split.train]
    assert (step0.demand_ranges.front.minimum, step0.demand_ranges.front.maximum) == (
        min(train_front),
        max(train_front),
    )
    assert step0.demand_ranges.front.observed_count == len(reference.split.train)


def test_profile_refuses_a_split_or_dataset_the_model_did_not_learn(tmp_path: Path) -> None:
    fitted = Fitted(tmp_path / "a", base)
    shifted = split_temporally_v2(
        fitted.dataset.examples, validation_start_ms=30_000, test_start_ms=36_000
    )
    with pytest.raises(ValueError, match="split"):
        fit_confidence_profile_v2(fitted.trained, fitted.source, shifted, fitted.spec)
    other = published(make_dataset(alternating), tmp_path / "b")
    with pytest.raises(ValueError, match="dataset"):
        fit_confidence_profile_v2(fitted.trained, other, fitted.split, fitted.spec)
    with pytest.raises(TypeError):
        fit_confidence_profile_v2(
            fitted.trained,
            SimpleNamespace(dataset=fitted.dataset),  # type: ignore[arg-type]
            fitted.split,
            fitted.spec,
        )


def test_profile_refuses_a_spec_without_every_fan_axis(tmp_path: Path) -> None:
    fitted = Fitted(tmp_path, base)
    spec = ConfidenceProfileSpec(
        support_axes=fitted.spec.support_axes[:2], residual_scale_floor=1e-6
    )
    with pytest.raises(ValueError, match=r"fan\.top"):
        fit_confidence_profile_v2(fitted.trained, fitted.source, fitted.split, spec)


def test_action_support_is_counted_per_step_and_per_step_pair(tmp_path: Path) -> None:
    fitted = Fitted(tmp_path, low_high_high)
    support = fitted.profile.action_support
    train = sorted(fitted.split.train, key=lambda item: item.control_tick_id)
    low, high = (0, 0, 0), (1, 0, 0)

    def cell(value: float) -> tuple[int, int, int]:
        return low if value < 0.5 else high

    # (a) step ごとの cell の件数（step 0 は anchor の tick、step 1 は次の tick）
    for step in range(STEPS):
        counts = {item.cell: item.count for item in support.steps[step].cells}
        expected: dict[tuple[int, int, int], int] = {}
        for example in train:
            key = cell(example.action_steps[step].effective_demand.front)
            expected[key] = expected.get(key, 0) + 1
        assert counts == expected
    # (b) anchor（prior_action）→ step 0、(c) 組 (0, 1)。どちらも train の記録した列から数える
    anchor_pairs = {
        (item.source, item.target): item.count for item in support.anchor_to_first.cells
    }
    expected_anchor: dict[tuple[tuple[int, int, int], tuple[int, int, int]], int] = {}
    expected_pair: dict[tuple[tuple[int, int, int], tuple[int, int, int]], int] = {}
    for example in train:
        prior = cell(example.prior_action.effective_demand.front)
        first = cell(example.action_steps[0].effective_demand.front)
        second = cell(example.action_steps[1].effective_demand.front)
        expected_anchor[(prior, first)] = expected_anchor.get((prior, first), 0) + 1
        expected_pair[(first, second)] = expected_pair.get((first, second), 0) + 1
    assert anchor_pairs == expected_anchor
    assert set(anchor_pairs) == {(low, high), (high, high), (high, low)}
    pairs = {(item.source, item.target): item.count for item in support.transitions[0].cells}
    assert pairs == expected_pair
    delta = support.transitions[0].delta_ranges.front
    assert delta.minimum == pytest.approx(-0.4) and delta.maximum == pytest.approx(0.4)
    assert delta.observed_count == len(train)
    # LOW を保つ held の列は (b) に LOW→LOW が無いので基準から除かれ、HIGH を保つ列は残る
    assert 0 < fitted.profile.residual_excluded_example_count < len(fitted.split.validation)


# ---------------------------------------------------------------- residual の基準（0084 §2.1）


def test_residual_base_uses_the_held_anchor_inference(tmp_path: Path) -> None:
    fitted = Fitted(tmp_path, base)
    assert fitted.profile.residual_excluded_example_count == 0
    validation = fitted.split.validation
    assert fitted.profile.residual_validation_example_count == len(validation)
    held: dict[tuple[int, str], list[float]] = {}
    recorded: dict[tuple[int, str], list[float]] = {}
    for example in validation:
        observed = ObservedThermalInput.from_example_v2(example)
        by_held = fitted.model.predict(observed)
        by_recorded = fitted.model.predict_trajectory(
            observed, recorded_trajectory(example, fitted.model.action_schema)
        )
        for target, held_target, recorded_target in zip(
            example.targets, by_held.targets, by_recorded.targets, strict=True
        ):
            for metric in TARGETS:
                actual = target.values[metric]
                assert actual is not None
                key = (target.horizon_ms, metric)
                held.setdefault(key, []).append((actual - held_target.values[metric]) ** 2)
                recorded.setdefault(key, []).append((actual - recorded_target.values[metric]) ** 2)
    scales = {(item.horizon_ms, item.metric): item for item in fitted.profile.residual_scales}
    differs = False
    for key, squared in held.items():
        expected = math.sqrt(math.fsum(squared) / len(squared))
        assert scales[key].scale == pytest.approx(max(expected, fitted.spec.residual_scale_floor))
        assert scales[key].validation_samples == len(validation)
        other = math.sqrt(math.fsum(recorded[key]) / len(recorded[key]))
        differs = differs or not math.isclose(expected, other)
    # 記録した action 列は held と違うので、別の入力の予測から基準を作れば値が変わる
    assert differs


def test_residual_base_excludes_validation_examples_outside_the_step_support(
    tmp_path: Path,
) -> None:
    # VALIDATION_TICK の demand は学習範囲の外。その次の example の held の列
    # （prior_action を保つ）が step ごとの support の外になる
    outlier = with_override(base, {VALIDATION_TICK: zones(0.95)})
    calm = Fitted(tmp_path / "a", outlier)
    # 除いた example の label だけを大きく外しても、基準は変わらない（基準に入っていない）
    noisy = Fitted(tmp_path / "b", outlier, target_shift={VALIDATION_TICK + 1: 500.0})
    excluded = calm.observed(VALIDATION_TICK + 1)
    assert calm.assessor().held_support(excluded) is not None
    assert calm.profile.residual_excluded_example_count == 1
    assert calm.profile.residual_validation_example_count == len(calm.split.validation) - 1
    assert noisy.profile.residual_scales == calm.profile.residual_scales
    assert all(
        scale.validation_samples == len(calm.split.validation) - 1
        for scale in calm.profile.residual_scales
    )
    # 対照: support の中の example の label を外せば、基準は変わる
    kept = Fitted(tmp_path / "c", outlier, target_shift={VALIDATION_TICK - 1: 500.0})
    assert kept.profile.residual_scales != calm.profile.residual_scales


def test_profile_is_refused_when_no_validation_example_is_inside_the_step_support(
    tmp_path: Path,
) -> None:
    reference = Fitted(tmp_path / "a", base)
    validation_ticks = {item.control_tick_id for item in reference.split.validation}
    # validation の全 example の prior_action を学習範囲の外へ
    moved = with_override(base, {tick - 1: zones(0.95) for tick in validation_ticks})
    dataset = make_dataset(moved)
    source = published(dataset, tmp_path / "b")
    parts = split(dataset)
    trained = train_counterfactual_ridge(
        source,
        parts,
        training_spec(),
        metric_catalog=CATALOG,
    )
    with pytest.raises(ValueError, match="Profile v2 を作らない"):
        fit_confidence_profile_v2(trained, source, parts, profile_spec())
    with pytest.raises(ValueError, match="Profile v2 を作らない"):
        seal_counterfactual_artifact(trained, source, parts, profile_spec())


# ------------------------------------------------ step ごとの support の照合（0084 §2.2）


def ranged(low: float, high: float, *, count: int = 1) -> ValueRange:
    return ValueRange(source="fan.front", minimum=low, maximum=high, observed_count=count)


def per_zone(front: ValueRange, *, low: float = 0.0, high: float = 0.45) -> PerZone[ValueRange]:
    return PerZone(
        front=front,
        rear=ValueRange(source="fan.rear", minimum=low, maximum=high, observed_count=1),
        top=ValueRange(source="fan.top", minimum=low, maximum=high, observed_count=1),
    )


LOW, HIGH = (0, 0, 0), (1, 0, 0)


def hand_profile(
    fitted: Fitted,
    *,
    step_fronts: tuple[ValueRange, ValueRange],
    step_cells: tuple[tuple[tuple[int, int, int], ...], ...],
    anchor_front: ValueRange,
    anchor_cells: tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...],
    pair_front: ValueRange,
    pair_cells: tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...],
) -> ConfidenceProfileV2:
    def transitions(
        pairs: tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...],
    ) -> tuple[ActionCellTransitionCount, ...]:
        return tuple(
            ActionCellTransitionCount(source=source, target=target, count=1)
            for source, target in sorted(pairs)
        )

    support = ActionSupportV2(
        steps=tuple(
            StepActionSupport(
                step=step,
                demand_ranges=per_zone(step_fronts[step]),
                cells=tuple(ActionCellCount(cell=cell, count=1) for cell in sorted(cells)),
            )
            for step, cells in enumerate(step_cells)
        ),
        anchor_to_first=AnchorTransitionSupport(
            delta_ranges=per_zone(anchor_front, low=0.0, high=0.0),
            cells=transitions(anchor_cells),
        ),
        transitions=(
            StepTransitionSupport(
                from_step=0,
                delta_ranges=per_zone(pair_front, low=0.0, high=0.0),
                cells=transitions(pair_cells),
            ),
        ),
    )
    return fitted.profile.model_copy(update={"action_support": support})


def plan(*fronts: float, step_ms: int = STEP_MS) -> ActionTrajectory:
    return ActionTrajectory(step_ms=step_ms, demands=tuple(zones(front) for front in fronts))


@pytest.fixture(scope="module")
def fitted(tmp_path_factory: pytest.TempPathFactory) -> Fitted:
    return Fitted(tmp_path_factory.mktemp("fitted"), base)


def checker_for(fitted: Fitted, **kwargs: Any) -> StepSupportChecker:
    defaults: dict[str, Any] = {
        # step 0 でだけ 0.8 を観測した（step 1 は 0.1〜0.4）
        "step_fronts": (ranged(0.1, 0.8), ranged(0.1, 0.4)),
        "step_cells": ((LOW, HIGH), (LOW,)),
        "anchor_front": ranged(-0.5, 0.6),
        "anchor_cells": ((LOW, LOW), (LOW, HIGH)),
        "pair_front": ranged(-0.6, 0.3),
        "pair_cells": ((LOW, LOW), (HIGH, LOW)),
    }
    defaults.update(kwargs)
    return StepSupportChecker(hand_profile(fitted, **defaults), fitted.model.action_schema)


def test_value_seen_only_at_step_zero_is_not_accepted_at_a_later_step(fitted: Fitted) -> None:
    checker = checker_for(fitted)
    assert checker.check(zones(0.3), plan(0.8, 0.3)) is None
    violation = checker.check(zones(0.3), plan(0.3, 0.8))
    assert violation is not None
    assert (violation.kind, violation.step, violation.zone) == (
        SupportViolationKind.STEP_DEMAND,
        1,
        Zone.FRONT,
    )
    assert "step=1" in violation.describe() and "zone=front" in violation.describe()


def test_no_margin_is_applied_to_the_step_ranges(fitted: Fitted) -> None:
    # range_margin（anchor の OOD 判定の幅）の中でも、観測した範囲の外は通さない
    checker = checker_for(fitted)
    violation = checker.check(zones(0.3), plan(0.8000001, 0.3))
    assert violation is not None and violation.kind is SupportViolationKind.STEP_DEMAND


def test_a_step_without_observations_accepts_nothing(fitted: Fitted) -> None:
    empty = ValueRange(source="fan.front", minimum=None, maximum=None, observed_count=0)
    checker = checker_for(fitted, step_fronts=(ranged(0.1, 0.8), empty))
    violation = checker.check(zones(0.3), plan(0.3, 0.3))
    assert violation is not None and (violation.kind, violation.step) == (
        SupportViolationKind.STEP_DEMAND,
        1,
    )


def test_joint_cell_is_checked_per_step(fitted: Fitted) -> None:
    checker = checker_for(fitted, step_cells=((LOW,), (LOW,)))
    # 範囲には入るが、step 0 の (a) に HIGH の cell が無い
    violation = checker.check(zones(0.3), plan(0.8, 0.3))
    assert violation is not None
    assert (violation.kind, violation.step, violation.cell) == (
        SupportViolationKind.STEP_CELL,
        0,
        HIGH,
    )


def test_cross_zone_combination_outside_the_joint_cells_is_rejected(fitted: Fitted) -> None:
    # (Front, Rear) = (0.1, 0.1) と (0.9, 0.9) だけを学習した（0079 §2.7）
    both_high = (1, 1, 0)
    wide = ValueRange(source="fan.rear", minimum=0.1, maximum=0.9, observed_count=2)
    profile = hand_profile(
        fitted,
        step_fronts=(ranged(0.1, 0.9), ranged(0.1, 0.9)),
        step_cells=((LOW, both_high), (LOW, both_high)),
        anchor_front=ranged(-1.0, 1.0),
        anchor_cells=((LOW, LOW), (LOW, both_high)),
        pair_front=ranged(-1.0, 1.0),
        pair_cells=((LOW, LOW), (both_high, both_high)),
    )
    support = profile.action_support
    steps = tuple(
        item.model_copy(
            update={"demand_ranges": item.demand_ranges.model_copy(update={"rear": wide})}
        )
        for item in support.steps
    )
    anchor = support.anchor_to_first.model_copy(
        update={
            "delta_ranges": support.anchor_to_first.delta_ranges.model_copy(
                update={
                    "rear": ValueRange(
                        source="fan.rear", minimum=-1.0, maximum=1.0, observed_count=2
                    )
                }
            )
        }
    )
    profile = profile.model_copy(
        update={
            "action_support": support.model_copy(update={"steps": steps, "anchor_to_first": anchor})
        }
    )
    checker = StepSupportChecker(profile, fitted.model.action_schema)
    crossed = ActionTrajectory(
        step_ms=STEP_MS, demands=(zones(0.9, rear=0.1), zones(0.9, rear=0.1))
    )
    violation = checker.check(zones(0.1, rear=0.1), crossed)
    assert violation is not None
    assert (violation.kind, violation.step, violation.cell) == (
        SupportViolationKind.STEP_CELL,
        0,
        HIGH,
    )


def test_jump_from_the_anchor_is_checked_even_for_a_held_plan(fitted: Fitted) -> None:
    checker = checker_for(
        fitted,
        step_fronts=(ranged(0.1, 0.8), ranged(0.1, 0.8)),
        step_cells=((LOW, HIGH), (LOW, HIGH)),
        pair_cells=((LOW, LOW), (HIGH, LOW), (HIGH, HIGH)),
        anchor_front=ranged(-0.1, 0.1),
    )
    # 各 step の値と step 間の変化量（0）は範囲内。anchor 0.2 → 0.8 の跳びだけが範囲外
    violation = checker.check(zones(0.2), plan(0.8, 0.8))
    assert violation is not None
    assert (violation.kind, violation.zone) == (SupportViolationKind.ANCHOR_DELTA, Zone.FRONT)
    assert "steps=(anchor,0)" in violation.describe()
    # cell の組 (b) にだけ無い遷移
    checker = checker_for(fitted, anchor_cells=((LOW, LOW),))
    violation = checker.check(zones(0.3), plan(0.8, 0.3))
    assert violation is not None and violation.kind is SupportViolationKind.ANCHOR_CELL
    assert (violation.source_cell, violation.cell) == (LOW, HIGH)


def test_transitions_are_checked_per_step_pair(fitted: Fitted) -> None:
    checker = checker_for(fitted, pair_cells=((LOW, LOW),))
    violation = checker.check(zones(0.3), plan(0.8, 0.3))
    assert violation is not None
    assert (violation.kind, violation.step, violation.to_step) == (
        SupportViolationKind.TRANSITION_CELL,
        0,
        1,
    )
    assert "steps=(0,1)" in violation.describe()
    checker = checker_for(fitted, pair_front=ranged(-0.1, 0.3))
    violation = checker.check(zones(0.3), plan(0.8, 0.3))
    assert violation is not None and violation.kind is SupportViolationKind.TRANSITION_DELTA


def test_off_grid_trajectories_are_not_checked(fitted: Fitted) -> None:
    checker = checker_for(fitted)
    with pytest.raises(ValueError, match="格子"):
        checker.check(zones(0.3), plan(0.3, 0.3, 0.3))
    with pytest.raises(ValueError, match="格子"):
        checker.check(zones(0.3), plan(0.3, 0.3, step_ms=500))


def test_checker_is_bound_to_the_profile_action_schema(fitted: Fitted) -> None:
    other = ThermalActionSchema(step_ms=STEP_MS * 2, steps=STEPS)
    with pytest.raises(ValueError, match="action schema"):
        StepSupportChecker(fitted.profile, other)


# ---------------------------------------------------------------- 同梱 Profile からの判定器


def test_assessor_is_built_only_from_the_sealed_model(fitted: Fitted) -> None:
    with pytest.raises(TypeError):
        CounterfactualConfidenceAssessor()
    with pytest.raises(TypeError):
        CounterfactualConfidenceAssessor.for_model(
            SimpleNamespace(confidence_profile=fitted.profile),  # type: ignore[arg-type]
            policy().model_confidence,
        )
    # 別の Profile を渡す引数を持たない（0079 §2.4）
    assert list(inspect.signature(CounterfactualConfidenceAssessor.for_model).parameters) == [
        "model",
        "policy",
    ]
    judge = fitted.assessor()
    assert judge.profile == fitted.model.confidence_profile
    with pytest.raises(AttributeError):
        judge._core = None  # type: ignore[misc]


def held_out_dataset() -> Demands:
    # 最初の train の anchor の tick でだけ 0.8 を使う。step 0 では観測するが、step 1 では観測しない
    return with_override(base, {FIRST_TRAIN_TICK: zones(0.8)})


@pytest.fixture(scope="module")
def held_out(tmp_path_factory: pytest.TempPathFactory) -> Fitted:
    return Fitted(tmp_path_factory.mktemp("held"), held_out_dataset())


def test_held_column_outside_the_step_support_is_support_ood(held_out: Fitted) -> None:
    judge = held_out.assessor()
    # prior_action が 0.8 の example（train）。anchor action の fan range と support cell には入る
    observed = held_out.observed(FIRST_TRAIN_TICK + 1)
    assert observed.action.front.effective_demand == 0.8
    assessment = judge.assess(observed, held_out.model.predict(observed), None)
    support = component(assessment, ConfidenceComponent.SUPPORT)
    assert support.ood is True and support.score == 0.0
    assert "step=1" in support.detail and "zone=front" in support.detail
    assert assessment.ood is True and assessment.confidence == 0.0
    assert assessment.ood_components == (ConfidenceComponent.SUPPORT,)
    fan = component(assessment, ConfidenceComponent.FAN_STATE_RANGE)
    assert fan.ood is False and fan.score == 1.0
    # 0050 の既存の判定（v1 と同じ材料）だけなら通る入力である
    assert "cell=" in support.detail and "count=" in support.detail


def test_held_column_inside_the_step_support_is_not_ood(held_out: Fitted) -> None:
    judge = held_out.assessor()
    observed = held_out.observed(FIRST_TRAIN_TICK + 5)
    assert judge.held_support(observed) is None
    assessment = judge.assess(observed, held_out.model.predict(observed), None)
    assert component(assessment, ConfidenceComponent.SUPPORT).ood is False
    assert assessment.ood is False and assessment.confidence > 0.0


def test_transition_missing_only_in_the_step_pair_is_support_ood(tmp_path: Path) -> None:
    # (b) にある LOW → LOW（最初の train の anchor の prior → step 0）が、組 (0, 1) の (c) には無い
    demand_of = with_override(
        low_high_high,
        {
            FIRST_TRAIN_TICK - 1: zones(0.3),
            FIRST_TRAIN_TICK: zones(0.3),
            FIRST_TRAIN_TICK + 1: zones(0.7),
        },
    )
    fitted = Fitted(tmp_path, demand_of)
    observed = next(
        fitted.observed(item.control_tick_id)
        for item in sorted(fitted.split.train, key=lambda item: item.control_tick_id)
        if item.prior_action.effective_demand.front < 0.5
    )
    violation = fitted.assessor().held_support(observed)
    assert violation is not None
    assert (violation.kind, violation.step, violation.to_step) == (
        SupportViolationKind.TRANSITION_CELL,
        0,
        1,
    )
    assessment = fitted.assessor().assess(observed, fitted.model.predict(observed), None)
    assert component(assessment, ConfidenceComponent.SUPPORT).ood is True
    assert "steps=(0,1)" in component(assessment, ConfidenceComponent.SUPPORT).detail


def test_support_ood_switches_the_gate_to_fallback(held_out: Fitted) -> None:
    judge = held_out.assessor()
    model = held_out.model
    # 証拠が無い間の上限（0.7）でも LIMITED の下限（0.6）は越える。support の外だけが差になる
    gate = gate_for(
        policy(authority="limited", recovery_hold_ms=1),
        expected_model_version=model.manifest.model_version,
        expected_artifact_sha256=model.artifact_sha256,
    )

    inside = held_out.observed(FIRST_TRAIN_TICK + 5)
    calm = judge.assess(inside, model.predict(inside), None)
    for now in (0, 2):
        accepted = _select(
            gate,
            now,
            calm.apply_to(learned_proposal(0.5, inference_id=calm.inference_id)),
            calm,
            artifact=model.artifact_sha256,
        )
    assert accepted.active_controller is ControllerKind.LEARNED_MPC, accepted.fallback_reason

    outside = held_out.observed(FIRST_TRAIN_TICK + 1)
    ood = judge.assess(outside, model.predict(outside), None)
    selected = _select(
        gate,
        3,
        ood.apply_to(learned_proposal(0.1, inference_id=ood.inference_id)),
        ood,
        artifact=model.artifact_sha256,
    )
    assert selected.active_controller is ControllerKind.FALLBACK
    assert selected.fallback_reason is not None and selected.fallback_reason.code == "ood"
    assert selected.model_gate is not None
    assert "ood_support" in {reason.code for reason in selected.model_gate.assessment}


def test_prediction_from_other_bytes_with_the_same_identity_is_model_binding_ood(
    tmp_path: Path, fitted: Fitted
) -> None:
    other = Fitted(tmp_path, base, spec=profile_spec(floor=0.5))
    assert other.model.manifest.model_version == fitted.model.manifest.model_version
    assert other.model.artifact_sha256 != fitted.model.artifact_sha256
    observed = fitted.observed(FIRST_TRAIN_TICK + 5)
    assessment = fitted.assessor().assess(observed, other.model.predict(observed), None)
    binding = component(assessment, ConfidenceComponent.MODEL_BINDING)
    assert binding.ood is True and "artifact_sha256" in binding.detail


def test_residual_monitor_counts_evidence_against_the_bundled_profile(
    tmp_path: Path, fitted: Fitted
) -> None:
    settings = policy().model_confidence
    monitor = counterfactual_residual_monitor(fitted.model, settings)
    observed = fitted.observed(FIRST_TRAIN_TICK + 5)
    prediction = fitted.model.predict(observed)
    monitor.record(prediction)
    for target in prediction.targets:
        monitor.observe(
            target.expected_ts_ms,
            {
                metric: ResidualObservation(value=value, quality=Quality.OK)
                for metric, value in target.values.items()
            },
        )
    evidence = monitor.evidence(observed.action_ts_ms + HORIZONS[-1] + 5_000)
    assert evidence.profile_sha256 == fitted.profile.sha256()
    assert evidence.forecasts == 1 and evidence.ratio == 0.0
    other = Fitted(tmp_path, base, spec=profile_spec(floor=0.5))
    with pytest.raises(ValueError, match="別のモデル"):
        monitor.record(other.model.predict(fitted.observed(FIRST_TRAIN_TICK + 6)))
    with pytest.raises(TypeError):
        counterfactual_residual_monitor(
            SimpleNamespace(confidence_profile=fitted.profile),  # type: ignore[arg-type]
            settings,
        )


def test_offline_evaluation_counts_held_support_ood(held_out: Fitted) -> None:
    cases = (
        OodEvaluationCase(
            label="held-outside",
            observed=held_out.observed(FIRST_TRAIN_TICK + 1),
            expected_ood=True,
        ),
        OodEvaluationCase(
            label="held-inside",
            observed=held_out.observed(FIRST_TRAIN_TICK + 5),
            expected_ood=False,
        ),
    )
    report = evaluate_ood_detection(held_out.model, held_out.assessor(), cases)
    assert (report.true_positive, report.true_negative) == (1, 1)
    assert report.false_positive == report.false_negative == 0
    assert report.ood_by_component["support"] == 1


# ---------------------------------------------------------------- 組で昇格・rollback


def test_promotion_and_rollback_move_the_fitted_profile_with_its_model(tmp_path: Path) -> None:
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
    fitted_by_version: dict[str, Fitted] = {}
    for version, demand_of in (("0.1.0", base), ("0.2.0", held_out_dataset())):
        fitted = Fitted(tmp_path / version, demand_of, version=version)
        artifact: CounterfactualThermalModelArtifact = fitted.artifact
        metadata = ArtifactMetadata.model_validate_json(
            counterfactual_registry_metadata_json_bytes(
                counterfactual_registry_metadata(artifact, fitted.payload)
            )
        )
        registry.register_candidate(metadata, fitted.payload, actor="trainer", reason="trained")
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
        fitted_by_version[version] = fitted

    def production() -> RegistryCounterfactualThermalModel:
        result = registry.load_production(ArtifactKind.THERMAL_MODEL, compatibility)
        assert result.artifact is not None, result.detail
        return load(result.artifact)

    settings = policy().model_confidence
    current = production()
    newest = fitted_by_version["0.2.0"]
    judge = CounterfactualConfidenceAssessor.for_model(current, settings)
    assert judge.profile == newest.profile
    observed = newest.observed(FIRST_TRAIN_TICK + 1)
    assert judge.assess(observed, current.predict(observed), None).ood is True

    revision = registry.inspect().revision
    previous = registry.inspect().production[ArtifactKind.THERMAL_MODEL].previous
    assert previous is not None
    oldest = fitted_by_version["0.1.0"]
    registry.rollback(
        ArtifactKind.THERMAL_MODEL,
        compatibility,
        approval=HumanApproval(
            action=ApprovalAction.ROLLBACK,
            artifact=previous,
            artifact_sha256=hashlib.sha256(oldest.payload).hexdigest(),
            expected_revision=revision,
            approver="model-operator",
            approved_at_ms=NOW_MS,
            reason="roll back",
        ),
        expected_revision=revision,
    )
    restored = production()
    judge = CounterfactualConfidenceAssessor.for_model(restored, settings)
    assert restored.manifest.model_version == "0.1.0"
    assert judge.profile == oldest.profile
    assessment = judge.assess(observed, restored.predict(observed), None)
    assert assessment.profile_sha256 == oldest.profile.sha256()
    assert assessment.artifact_sha256 == hashlib.sha256(oldest.payload).hexdigest()
    # 新しい model の予測を古い Profile の判定器へ渡すと、束縛の照合で OOD になる
    mixed = judge.assess(observed, current.predict(observed), None)
    assert component(mixed, ConfidenceComponent.MODEL_BINDING).ood is True


def test_profile_v2_module_stays_upstream_of_guard_safety_and_hardware() -> None:
    forbidden = ("control.hardware", "control.safety", "control.reactive", "hwmon", "pwm")
    assert profile_v2_module.__file__ is not None
    source = Path(profile_v2_module.__file__).read_text(encoding="utf-8")
    for token in forbidden:
        assert f"import {token}" not in source, token
        assert f"from coldaisle.{token}" not in source, token
    assert "coldaisle.ai" not in source


def test_profile_digest_is_canonical(fitted: Fitted) -> None:
    assert fitted.profile.sha256() == canonical_sha256(fitted.profile)
    assert isinstance(fitted.source, VerifiedTrainingDatasetArtifactV2)
    assert isinstance(fitted.trained, CounterfactualTrainedModel)
