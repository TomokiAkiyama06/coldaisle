"""#91 Offline Evaluation: Baseline vs MPC vs Guard vs Supervisor。実機不要。

**ここでは「守れているか」ではなく「破れないか」を試す。** 評価が満たしていなければ
ならない不変条件を並べ、1つずつ破ろうとする。

1. 評価は **Fan へ届く経路を持たず、時計も持たない**（決定記録 0054 §2.7）
2. 実測の成果は**適用された構成にしか帰属しない**。counterfactual の型に温度の欄が無い（§2.2）
3. 採点してよいのは `scored` な outcome だけ。`unidentifiable` / `unmatched` は
   **coverage として**数え、予測誤差に入れない（§2.2 / §2.3）
4. **coverage が足りなければ予測指標を出さず、gate も通さない**（§2.3。0053 §5 を閉じる）
5. **Safety 違反は、ほかの改善で相殺されない**（§2.4）
6. 時系列 split に**未来が漏れない**。境界を跨ぐ outcome は `purged`（§2.5）
7. 契約と**食い違う記録を通さない**（照合の許容幅・索引と本文・識別子。§2.6）
8. **同じ入力からは同じ bytes**。生成時刻を持たない（§2.7）
9. 設定の**既定値をコードに置かない**。写した上限が契約と食い違わない（§2.8）
10. **worst-case を必ず載せる**（平均に埋もれさせない）
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.control.config import ControlConfig
from coldaisle.control.evaluation import (
    EvaluationConfig,
    EvaluationContext,
    EvaluationInputError,
    EvaluationReport,
    EvaluationRun,
    GateOutcome,
    GateStage,
    GroupKind,
    SegmentRole,
    WorstCaseKind,
    evaluate,
)
from coldaisle.control.evaluation.model import (
    AppliedArm,
    AppliedArmReport,
    CountedReason,
    CounterfactualArm,
    CounterfactualArmReport,
    CoverageReport,
    GateCondition,
    GateResult,
    GroupReport,
    InterventionReport,
    OptimizerReport,
    PredictionReport,
    TemperatureReport,
)
from coldaisle.control.evaluation.stats import MetricSummary, shape_of, summarize
from coldaisle.control.schema import (
    MAX_SHADOW_PREDICTION_METRICS,
    MODEL_GATE_ASSESSMENT_COMPONENTS,
    AuthorityLimitSource,
    AuthorityStage,
    BoundBy,
    ConfidenceLevel,
    ControllerKind,
    ControlState,
    ControlTick,
    EffectiveZoneDemand,
    HardwareReadback,
    ModelGateDecision,
    OperatingMode,
    OptimizerStatus,
    PerZone,
    Reason,
    SafetyState,
    ShadowActionPlan,
    ShadowCounterfactual,
    ShadowPlanStep,
    ShadowPredictedTarget,
    ShadowPrediction,
    ShadowRecord,
    SupervisorPolicyKind,
    WorkloadRegime,
    Zone,
    ZoneRecord,
)
from coldaisle.control.shadow import (
    OutcomeObservation,
    OutcomeStatus,
    ShadowExportRow,
    read_shadow_jsonl,
    shadow_rows,
    write_shadow_jsonl,
)
from coldaisle.evaluate import RunsManifest, build_context, main, render
from coldaisle.metrics import MetricCatalog
from coldaisle.store.models import ControlTraceRecord, Quality
from test_control_config import valid_documents, write_documents

ROOT = Path(__file__).resolve().parents[1]
EVALUATION_PACKAGE = ROOT / "src" / "coldaisle" / "control" / "evaluation"
METRICS_YAML = ROOT / "config" / "metrics.yaml"
EVALUATION_YAML = ROOT / "config" / "evaluation.yaml"

GPU = "gpu.0.core"
ROOM = "air.room"
TEMPERATURES = ("gpu.0.core", "gpu.0.hotspot", "cpu.package")
"""`config/evaluation.yaml` の `temperature_metrics`。**全部揃わないと Safety の段は通らない。**"""
TICK_TS_MS = 1_787_616_000_000
STEP_MS = 10_000
"""`valid_documents()` の `mpc.optimizer.step_ms`。照合の許容幅より大きい。"""


# ---------------------------------------------------------------- 組み立ての補助


def demands(value: float) -> PerZone[float]:
    return PerZone[float](front=value, rear=value, top=value)


def zone_records(
    *,
    requested: float,
    effective: float,
    safety_floor: float = 0.0,
    bound_by: BoundBy = BoundBy.REQUESTED,
    guard_floor: float | None = None,
    rpm: int | None = 1200,
    flow: float | None = None,
) -> PerZone[ZoneRecord]:
    record = ZoneRecord(
        controller_reason=Reason(code="fallback_curve"),
        demand=EffectiveZoneDemand(
            requested=requested,
            effective=effective,
            bound_by=bound_by,
            safety_floor=safety_floor,
            forced_max=bound_by is BoundBy.FORCED_MAX,
            guard_floor=guard_floor,
            reasons=() if bound_by is BoundBy.REQUESTED else (Reason(code="guard_floor"),),
        ),
        estimated_flow=flow,
        hardware=(
            None
            if rpm is None
            else HardwareReadback(pwm_raw=128, rpm=rpm, write_ok=True, readback_ok=True)
        ),
    )
    return PerZone[ZoneRecord](front=record, rear=record, top=record)


def control_state(
    *,
    regime: WorkloadRegime = WorkloadRegime.SUSTAINED_GPU,
    safety_state: SafetyState = SafetyState.NORMAL,
) -> ControlState:
    """SHADOW stage の AUTO 運転。ML は requested を作れないので Fallback が active。"""
    return ControlState(
        operating_mode=OperatingMode.AUTO,
        authority_stage=AuthorityStage.SHADOW,
        active_controller=ControllerKind.FALLBACK,
        safety_state=safety_state,
        fallback_active=True,
        workload_regime=regime,
        regime_confidence=0.9,
    )


def plan_for(
    demand: float = 0.8, *, offsets: tuple[int, ...] = (STEP_MS, 2 * STEP_MS)
) -> ShadowActionPlan:
    return ShadowActionPlan(
        step_ms=offsets[0],
        steps=tuple(
            ShadowPlanStep(offset_ms=offset, demands=demands(demand)) for offset in offsets
        ),
    )


def prediction_for(
    plan: ShadowActionPlan,
    *,
    action_ts_ms: int,
    values: tuple[float, ...],
    inference: str,
    metric: str = GPU,
) -> ShadowPrediction:
    return ShadowPrediction(
        model_id="rack-thermal",
        model_version="thermal-v1",
        artifact_sha256="a" * 64,
        inference_id=inference,
        plan_digest=plan.digest(),
        input_action_ts_ms=action_ts_ms,
        targets=tuple(
            ShadowPredictedTarget(
                offset_ms=offset, expected_ts_ms=action_ts_ms + offset, values={metric: value}
            )
            for offset, value in zip(plan.offsets_ms, values, strict=True)
        ),
    )


def counterfactual(
    *,
    action_ts_ms: int,
    demand: float = 0.8,
    values: tuple[float, ...] = (50.0, 51.0),
    inference: str | None = None,
    status: OptimizerStatus = OptimizerStatus.OK,
    latency_ms: int = 120,
) -> ShadowCounterfactual:
    """解を持つ Learned MPC の counterfactual（同じ tick では適用されていない）。"""
    plan = plan_for(demand)
    identifier = inference or f"{action_ts_ms:064x}"
    if status is not OptimizerStatus.OK:
        return ShadowCounterfactual(
            controller=ControllerKind.LEARNED_MPC,
            requested=plan.first,
            reason=Reason(code="optimizer_timeout"),
            optimizer_status=status,
            latency_ms=latency_ms,
            model_version="thermal-v1",
            inference_id=identifier,
            artifact_sha256="a" * 64,
        )
    return ShadowCounterfactual(
        controller=ControllerKind.LEARNED_MPC,
        requested=plan.first,
        reason=Reason(code="optimizer_ok"),
        optimizer_status=status,
        latency_ms=latency_ms,
        evaluations=64,
        model_version="thermal-v1",
        inference_id=identifier,
        artifact_sha256="a" * 64,
        plan=plan,
        prediction=prediction_for(
            plan, action_ts_ms=action_ts_ms, values=values, inference=identifier
        ),
        cost_total=1.0,
        baseline_cost_total=2.0,
    )


def tick_at(
    ts_ms: int,
    tick_id: int,
    *,
    applied: float = 0.8,
    requested: float | None = None,
    shadow: bool = True,
    state: ControlState | None = None,
    safety_floor: float = 0.0,
    bound_by: BoundBy = BoundBy.REQUESTED,
    guard_floor: float | None = None,
    flow: float | None = 1.0,
    rpm: int | None = 1200,
    faults: tuple[Any, ...] = (),
    cf: ShadowCounterfactual | None = None,
    model_gate: ModelGateDecision | None = None,
) -> ControlTick:
    """1 tick。**適用は Fallback、counterfactual は Learned MPC**（重ならない）。"""
    zones = zone_records(
        requested=applied if requested is None else requested,
        effective=applied,
        safety_floor=safety_floor,
        bound_by=bound_by,
        guard_floor=guard_floor,
        rpm=rpm,
        flow=flow,
    )
    record = None
    if shadow:
        record = ShadowRecord(
            tick_id=tick_id,
            ts_ms=ts_ms,
            authority_stage=AuthorityStage.SHADOW,
            applied_controller=ControllerKind.FALLBACK,
            applied_effective=demands(applied),
            counterfactuals=(cf or counterfactual(action_ts_ms=ts_ms),),
        )
    return ControlTick(
        tick_id=tick_id,
        ts_ms=ts_ms,
        state=state or control_state(),
        zones=zones,
        model_gate=model_gate,
        shadow=record,
        faults=faults,
    )


def trace_of(tick: ControlTick) -> ControlTraceRecord:
    return ControlTraceRecord(
        ts_ms=tick.ts_ms,
        tick_id=tick.tick_id,
        schema_version=tick.schema_version,
        trace_json=tick.model_dump_json(),
    )


def observation(metric: str, ts_ms: int, value: float, *, quality: Quality = Quality.OK):
    return OutcomeObservation(metric=metric, ts_ms=ts_ms, value=value, quality=quality)


@pytest.fixture
def context(tmp_path: Path) -> EvaluationContext:
    """試験用の設定一式。**評価設定はリポジトリの実物を使う**（設定漏れも一緒に試す）。"""
    directory = tmp_path / "pr91-config"
    directory.mkdir()
    write_documents(directory, valid_documents())
    config, config_sha256 = EvaluationConfig.from_file(EVALUATION_YAML)
    return EvaluationContext.build(
        config=config,
        config_sha256=config_sha256,
        control=ControlConfig.from_directory(directory),
        catalog=MetricCatalog.from_yaml(METRICS_YAML),
        catalog_sha256="b" * 64,
    )


def plan_following_run(
    *, ticks: int = 6, demand: float = 0.8, start_ms: int = TICK_TS_MS
) -> tuple[list[ControlTraceRecord], list[OutcomeObservation]]:
    """**counterfactual の plan どおりに適用された** run（採点できる区間ができる）。"""
    traces = [
        trace_of(tick_at(start_ms + index * STEP_MS, index, applied=demand))
        for index in range(ticks)
    ]
    observations = [
        observation(metric, start_ms + index * STEP_MS, 50.0 + index * 0.1)
        for index in range(ticks + 4)
        for metric in TEMPERATURES
    ]
    observations.extend(
        observation(ROOM, start_ms + index * STEP_MS, 24.0) for index in range(ticks)
    )
    return traces, observations


def run_of(
    traces: list[ControlTraceRecord],
    observations: list[OutcomeObservation],
    *,
    run_id: str = "pr91-run",
    boundaries: tuple[int, ...] = (),
    shadow: tuple[ShadowExportRow, ...] | None = None,
) -> EvaluationRun:
    return EvaluationRun(
        run_id=run_id,
        traces=tuple(traces),
        observations=tuple(observations),
        split_boundaries_ms=boundaries,
        shadow=shadow,
    )


def overall(report: EvaluationReport, index: int = 0) -> GroupReport:
    group = next(item for item in report.segments[index].groups if item.kind is GroupKind.OVERALL)
    return group


# --------------------------------------- 不変条件 1: 評価は制御にも時計にも触れない


FORBIDDEN_MODULES = (
    "coldaisle.control.hardware",
    "coldaisle.control.safety",
    "coldaisle.control.reactive",
    "serial",
    "subprocess",
)
"""評価 package が import してはいけない module（AGENTS.md ルール1 / 2 / 6）。"""


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def test_invariant_1_a_the_evaluation_package_cannot_reach_the_actuation_path() -> None:
    """**Guard / Safety / Hardware へ触れられる評価を作らない。**

    触れられるようになった瞬間、「評価は読み取りだけ」が設計上の約束ではなくなる。
    """
    offenders = [
        f"{path.name}: {name}"
        for path in sorted(EVALUATION_PACKAGE.glob("*.py"))
        for name in _imported_modules(path)
        if any(
            name == forbidden or name.startswith(f"{forbidden}.") for forbidden in FORBIDDEN_MODULES
        )
    ]
    assert offenders == []


def test_invariant_1_b_the_evaluation_package_never_names_pwm_or_effective_demand() -> None:
    """評価の型に **PWM も `EffectiveZoneDemand` も現れない**（決定記録 0028 §2.3）。"""
    offenders = []
    for path in sorted(EVALUATION_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        offenders.extend(
            f"{path.name}: {name}"
            for name in sorted(names)
            if "pwm" in name.lower() or name == "EffectiveZoneDemand"
        )
    assert offenders == []


def test_invariant_1_c_the_evaluation_package_has_no_clock() -> None:
    """**処理した時刻を使わない。** 時計を持てば、あとから都合よく切り出せる。"""
    for path in sorted(EVALUATION_PACKAGE.glob("*.py")):
        imported = set(_imported_modules(path))
        assert not (imported & {"time", "datetime", "coldaisle.clock"}), path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert not ({"monotonic", "now_ms", "now", "monotonic_ms", "time_ns"} & names), path.name


def test_invariant_1_d_the_report_has_no_generation_timestamp() -> None:
    """報告に**生成時刻の欄を作らない**。入ると同じ入力から同じ bytes が出なくなる。"""
    stamped = [
        name
        for name in EvaluationReport.model_fields
        if "generated" in name or name in {"created_at", "now_ms", "at_ms"}
    ]
    assert stamped == []


# ------------------------- 不変条件 2: 実測の成果は適用された構成にしか帰属しない


def test_invariant_2_a_counterfactual_reports_cannot_carry_measured_outcomes() -> None:
    """適用されなかった提案に、温度・ΔT・Air Balance・RPM の欄を**作らない**（0054 §2.2）。"""
    forbidden = {
        "temperatures",
        "deltas",
        "air_balance",
        "rpm",
        "effective_demand",
        "interventions",
    }
    assert not (forbidden & set(CounterfactualArmReport.model_fields))


def test_invariant_2_b_a_measured_field_cannot_be_grafted_onto_a_counterfactual() -> None:
    """あとから温度の欄を足そうとしても、型が拒む（`extra="forbid"`）。"""
    with pytest.raises(ValidationError):
        CounterfactualArmReport(
            arm=CounterfactualArm(
                controller=ControllerKind.LEARNED_MPC,
                supervisor_policy=None,
                authority_stage=AuthorityStage.SHADOW,
            ),
            arm_key="counterfactual:learned_mpc+none@shadow",
            ticks=1,
            first_ts_ms=0,
            last_ts_ms=0,
            proposals=0,
            coverage=CoverageReport(outcomes=0, scored=0, unidentifiable=0, sufficient=False),
            temperatures=(),  # type: ignore[call-arg]
        )


def test_invariant_2_c_applied_and_counterfactual_arms_stay_in_separate_namespaces(
    context: EvaluationContext,
) -> None:
    """適用実績と「使わなかった提案」を**同じ行として読めない**ようにする。"""
    traces, observations = plan_following_run()
    report = evaluate([run_of(traces, observations)], context=context)
    group = overall(report)

    assert group.applied and group.counterfactual
    assert all(item.arm_key.startswith("applied:") for item in group.applied)
    assert all(item.arm_key.startswith("counterfactual:") for item in group.counterfactual)
    assert not {item.arm_key for item in group.applied} & {
        item.arm_key for item in group.counterfactual
    }


def _no_intervention(ticks: int = 1) -> InterventionReport:
    return InterventionReport(
        ticks=ticks,
        forced_max_ticks=0,
        safety_floor_ticks=0,
        guard_floor_ticks=0,
        guard_ceiling_ticks=0,
        ramp_down_ticks=0,
        guard_active_ticks=0,
        fallback_ticks=0,
        emergency_ticks=0,
        degraded_ticks=0,
        fault_ticks=0,
        safety_states=(CountedReason(code="normal", count=ticks),),
    )


def test_invariant_2_d_a_group_rejects_the_same_key_on_both_sides() -> None:
    """名前空間が重なる報告は**作れない**。"""
    applied = AppliedArmReport(
        arm=AppliedArm(
            controller=ControllerKind.FALLBACK,
            supervisor_policy=None,
            authority_stage=AuthorityStage.SHADOW,
            operating_mode=OperatingMode.AUTO,
        ),
        arm_key="applied:fallback+none@shadow/auto",
        ticks=1,
        first_ts_ms=0,
        last_ts_ms=0,
        interventions=_no_intervention(),
    )
    duplicate = applied.model_copy(update={"arm_key": applied.arm_key})
    with pytest.raises(ValidationError, match="同じ arm を1つの group に2回"):
        GroupReport(kind=GroupKind.OVERALL, value="overall", applied=(applied, duplicate))


# ------------------- 不変条件 3: 採点していない結果をモデル誤差として数えない


def test_invariant_3_a_unidentifiable_outcomes_never_become_prediction_error(
    context: EvaluationContext,
) -> None:
    """**掛かっていた action が plan と違う区間の差は、モデル誤差ではない**（0053 §2.3）。

    適用値を plan からずらすと、実測が予測から大きく外れていても予測指標は出ず、
    その分は coverage の `unidentifiable` として残る。
    """
    aside = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                applied=0.2,  # plan は 0.8。別の値が掛かっていた
                cf=counterfactual(action_ts_ms=TICK_TS_MS + index * STEP_MS),
            )
        )
        for index in range(6)
    ]
    far_off = [observation(GPU, TICK_TS_MS + index * STEP_MS, 90.0) for index in range(10)]
    far_off.extend(observation(ROOM, TICK_TS_MS + index * STEP_MS, 24.0) for index in range(6))

    report = evaluate([run_of(aside, far_off)], context=context)
    shadow_arm = overall(report).counterfactual[0]

    assert shadow_arm.coverage.outcomes > 0
    assert shadow_arm.coverage.scored == 0
    assert shadow_arm.coverage.unidentifiable == shadow_arm.coverage.outcomes
    assert shadow_arm.coverage.unidentifiable_reasons[0].code == "applied_action_differs"
    assert shadow_arm.predictions == ()
    assert shadow_arm.coverage.scored_outputs == 0


def test_invariant_3_c_a_later_failure_does_not_move_the_attested_timestamp(
    context: EvaluationContext,
) -> None:
    """**提案の無い tick で「証拠の新しさ」を更新できない**（#92 / 決定記録 0057 §2.4）。

    `last_ts_ms` は区間の最後の tick で、model を読めなかった tick も含む。それを
    新しさに使うと、いまの失敗を1つ足すだけで古い実績が「新鮮」に見えてしまう。
    裏づけのある提案が実在した時刻（`last_attested_ts_ms`）は別に持つ。
    """

    def attested_gate(index: int) -> ModelGateDecision:
        return ModelGateDecision(
            model_version="thermal-v1",
            inference_id=f"{TICK_TS_MS + index * STEP_MS:064x}",
            artifact_sha256="a" * 64,
            attested=True,
            confidence=0.9,
            ood=False,
            confidence_level=ConfidenceLevel.HIGH,
            authority_stage=AuthorityStage.SHADOW,
            learned_selected=False,
            assessment=tuple(
                Reason(code=component) for component in MODEL_GATE_ASSESSMENT_COMPONENTS
            ),
        )

    proposals = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                model_gate=attested_gate(index),
                state=control_state().model_copy(
                    update={
                        "model_version": "thermal-v1",
                        "model_confidence": 0.9,
                        "model_ood": False,
                    }
                ),
                cf=counterfactual(action_ts_ms=TICK_TS_MS + index * STEP_MS).model_copy(
                    update={"attested": True, "confidence": 0.9, "ood": False}
                ),
            )
        )
        for index in range(3)
    ]
    last_proposal_ts_ms = TICK_TS_MS + 2 * STEP_MS
    failed_ts_ms = TICK_TS_MS + 3 * STEP_MS
    failure = ShadowCounterfactual(
        controller=ControllerKind.LEARNED_MPC,
        failure=Reason(code="model_load_failure"),
    )
    proposals.append(trace_of(tick_at(failed_ts_ms, 3, cf=failure)))

    report = evaluate([run_of(proposals, [])], context=context)
    shadow_arm = overall(report).counterfactual[0]

    assert shadow_arm.last_ts_ms == failed_ts_ms, "区間の最後は失敗の tick"
    assert shadow_arm.last_attested_ts_ms == last_proposal_ts_ms
    assert shadow_arm.proposals == 3


def test_invariant_3_b_only_scored_outputs_feed_the_error_statistics() -> None:
    """採点した出力の数を、coverage が数えた採点数より多くできない。"""
    coverage = CoverageReport(
        outcomes=1,
        scored=1,
        unidentifiable=0,
        identifiable_fraction=1.0,
        outputs=2,
        matched_outputs=1,
        scored_outputs=1,
        sufficient=True,
    )
    with pytest.raises(ValidationError, match="coverage の採点数を超えている"):
        CounterfactualArmReport(
            arm=CounterfactualArm(
                controller=ControllerKind.LEARNED_MPC,
                supervisor_policy=None,
                authority_stage=AuthorityStage.SHADOW,
            ),
            arm_key="counterfactual:learned_mpc+none@shadow",
            ticks=1,
            first_ts_ms=0,
            last_ts_ms=0,
            proposals=1,
            safety_floor_shortfalls=0,
            maximum_floor_shortfall=0.0,
            coverage=coverage,
            predictions=(
                PredictionReport(
                    metric=GPU,
                    scored_outputs=5,
                    error=summarize([0.5]) or _never(),
                    absolute_error=summarize([0.5]) or _never(),
                    underprediction_outputs=5,
                    underprediction_rate=1.0,
                    maximum_underprediction=0.5,
                ),
            ),
        )


def _never() -> MetricSummary:  # pragma: no cover - 到達しない
    raise AssertionError("summarize は値があれば必ず返す")


def test_invariant_3_c_unmatched_outputs_are_counted_with_their_reason(
    context: EvaluationContext,
) -> None:
    """実測が無い出力を「誤差 0」にしない。**理由付きで数える。**"""
    traces, _observations = plan_following_run()
    room_only = [observation(ROOM, TICK_TS_MS + index * STEP_MS, 24.0) for index in range(6)]

    report = evaluate([run_of(traces, room_only)], context=context)
    shadow_arm = overall(report).counterfactual[0]

    assert shadow_arm.coverage.matched_outputs == 0
    assert shadow_arm.coverage.unmatched_reasons[0].code == "no_usable_observation"
    assert shadow_arm.predictions == ()


# ------------------- 不変条件 4: coverage は一級の出力で、足りなければ通さない


def test_invariant_4_a_a_report_cannot_show_predictions_without_enough_coverage() -> None:
    """**少数の採点区間の平均を、全体の予測精度に見せない。**"""
    coverage = CoverageReport(
        outcomes=10,
        scored=1,
        unidentifiable=9,
        identifiable_fraction=0.1,
        outputs=20,
        matched_outputs=2,
        scored_outputs=2,
        sufficient=False,
    )
    with pytest.raises(ValidationError, match="coverage が足りない"):
        CounterfactualArmReport(
            arm=CounterfactualArm(
                controller=ControllerKind.LEARNED_MPC,
                supervisor_policy=None,
                authority_stage=AuthorityStage.SHADOW,
            ),
            arm_key="counterfactual:learned_mpc+none@shadow",
            ticks=1,
            first_ts_ms=0,
            last_ts_ms=0,
            proposals=1,
            safety_floor_shortfalls=0,
            maximum_floor_shortfall=0.0,
            coverage=coverage,
            predictions=(
                PredictionReport(
                    metric=GPU,
                    scored_outputs=2,
                    error=summarize([0.5, 0.6]) or _never(),
                    absolute_error=summarize([0.5, 0.6]) or _never(),
                    underprediction_outputs=2,
                    underprediction_rate=1.0,
                    maximum_underprediction=0.6,
                ),
            ),
        )


def test_invariant_4_b_no_outcomes_means_no_identifiable_fraction() -> None:
    """outcome が1つも無いことを `1.0`（全部採点できた）に見せない。"""
    with pytest.raises(ValidationError, match="identifiable_fraction を付けない"):
        CoverageReport(
            outcomes=0,
            scored=0,
            unidentifiable=0,
            identifiable_fraction=1.0,
            sufficient=False,
        )


def test_invariant_4_c_zero_scored_outcomes_can_never_be_sufficient() -> None:
    """採点できた outcome が1つも無い coverage を「足りている」にしない。"""
    with pytest.raises(ValidationError, match="scored が 0"):
        CoverageReport(
            outcomes=5,
            scored=0,
            unidentifiable=5,
            identifiable_fraction=0.0,
            sufficient=True,
        )


def test_invariant_4_d_insufficient_coverage_blocks_the_gate(
    context: EvaluationContext,
) -> None:
    """**採点できた区間が少なければ rollout を通さない**（0053 §5 をここで閉じた）。"""
    traces, observations = plan_following_run()
    report = evaluate([run_of(traces, observations)], context=context)
    gate = next(item for item in report.gates if item.arm_key.startswith("counterfactual:"))

    assert gate.outcome is GateOutcome.BLOCKED
    assert gate.blocking_stage in {GateStage.SAFETY, GateStage.EVIDENCE}
    evidence = [item for item in gate.conditions if item.stage is GateStage.EVIDENCE]
    assert any(item.outcome is GateOutcome.BLOCKED for item in evidence)
    shadow_arm = overall(report).counterfactual[0]
    assert shadow_arm.coverage.sufficient is False
    assert any(gap.code == "insufficient_coverage" for gap in shadow_arm.gaps)


# ----------------- 不変条件 5: Safety 違反は、ほかの改善で相殺されない


def test_invariant_5_a_a_failed_condition_always_blocks_the_whole_gate() -> None:
    """cost がどれだけ良くても、Safety が落ちていれば `pass` にできない。"""
    conditions = (
        GateCondition(
            stage=GateStage.SAFETY,
            name="ceiling_exceedances",
            outcome=GateOutcome.BLOCKED,
            limit=0.0,
            observed=3.0,
        ),
        GateCondition(
            stage=GateStage.COST,
            name="underprediction_c",
            outcome=GateOutcome.PASS,
            limit=3.0,
            observed=0.0,
        ),
    )
    with pytest.raises(ValidationError, match="落ちた条件が1つでもあれば"):
        GateResult(
            arm_key="applied:fallback+none@shadow/auto",
            outcome=GateOutcome.PASS,
            blocking_stage=GateStage.SAFETY,
            conditions=conditions,
        )


def test_invariant_5_b_the_blocking_stage_is_the_earliest_failure() -> None:
    """cost の失敗を blocking として書いて、Safety の失敗を隠せない。"""
    conditions = (
        GateCondition(
            stage=GateStage.SAFETY,
            name="emergency_ticks",
            outcome=GateOutcome.BLOCKED,
            limit=0.0,
            observed=2.0,
        ),
        GateCondition(
            stage=GateStage.COST,
            name="underprediction_c",
            outcome=GateOutcome.BLOCKED,
            limit=3.0,
            observed=9.0,
        ),
    )
    with pytest.raises(ValidationError, match="blocking_stage"):
        GateResult(
            arm_key="applied:fallback+none@shadow/auto",
            outcome=GateOutcome.BLOCKED,
            blocking_stage=GateStage.COST,
            conditions=conditions,
        )


def test_invariant_5_c_an_undecidable_condition_is_blocked_not_passed() -> None:
    """**判定できないことを合格にしない**（fail closed）。"""
    with pytest.raises(ValidationError, match="観測値の無い gate 条件は blocked"):
        GateCondition(
            stage=GateStage.SAFETY,
            name="threshold_margin_c",
            outcome=GateOutcome.PASS,
            limit=5.0,
            reason=CountedReason(code="no_temperature_evidence", count=1),
        )


def test_invariant_5_d_a_safety_violation_blocks_even_with_perfect_cost_metrics(
    context: EvaluationContext,
) -> None:
    """実際の評価でも、絶対上限を超えた run は `blocked` になる。"""
    traces, observations = plan_following_run()
    ceiling = context.control.safety.absolute_temp_ceiling_c.value
    hot = [
        observation(item.metric, item.ts_ms, ceiling + 5.0) if item.metric in TEMPERATURES else item
        for item in observations
    ]
    report = evaluate([run_of(traces, hot)], context=context)
    gate = next(item for item in report.gates if item.arm_key.startswith("applied:"))

    assert gate.outcome is GateOutcome.BLOCKED
    assert gate.blocking_stage is GateStage.SAFETY
    exceeded = next(item for item in gate.conditions if item.name == "ceiling_exceedances")
    assert exceeded.observed is not None and exceeded.observed > 0
    assert exceeded.worst_case is not None


def test_invariant_5_e_a_proposal_below_the_recorded_safety_floor_is_counted(
    context: EvaluationContext,
) -> None:
    """**記録された Critical Safety floor を下回る要求**を、安全側の指標として数える。"""
    traces = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                applied=0.9,
                safety_floor=0.9,
                cf=counterfactual(action_ts_ms=TICK_TS_MS + index * STEP_MS, demand=0.2),
            )
        )
        for index in range(4)
    ]
    report = evaluate([run_of(traces, [])], context=context)
    shadow_arm = overall(report).counterfactual[0]

    assert shadow_arm.safety_floor_shortfalls == 4 * len(Zone)
    assert shadow_arm.maximum_floor_shortfall == pytest.approx(0.7)
    gate = next(item for item in report.gates if item.arm_key.startswith("counterfactual:"))
    shortfall = next(item for item in gate.conditions if item.name == "safety_floor_shortfalls")
    assert shortfall.outcome is GateOutcome.BLOCKED
    assert gate.blocking_stage is GateStage.SAFETY


# ----------------------------- 不変条件 6: 時系列 split に未来が漏れない


def test_invariant_6_a_appending_future_data_does_not_change_an_earlier_segment(
    context: EvaluationContext,
) -> None:
    """**segment の報告は `end_ms` 以降の入力に依存しない**（0054 §2.5）。"""
    traces, observations = plan_following_run(ticks=6)
    boundary = TICK_TS_MS + 3 * STEP_MS
    short_traces = traces[:4]
    short_observations = [item for item in observations if item.ts_ms <= traces[3].ts_ms]
    shorter = evaluate(
        [run_of(short_traces, short_observations, boundaries=(boundary,))], context=context
    )
    longer = evaluate([run_of(traces, observations, boundaries=(boundary,))], context=context)

    early = longer.segments[0]
    assert early.role is SegmentRole.CALIBRATION
    assert longer.segments[-1].role is SegmentRole.HOLDOUT
    assert early.start_ms == shorter.segments[0].start_ms
    assert early.end_ms == boundary
    # **あとから未来のデータを足しても、前の segment の bytes は変わらない。**
    assert early.model_dump_json() == shorter.segments[0].model_dump_json()


def test_invariant_6_b_outcomes_that_straddle_the_boundary_are_purged(
    context: EvaluationContext,
) -> None:
    """境界を跨ぐ outcome は**どの segment にも入れず、数えて残す**。"""
    traces, observations = plan_following_run(ticks=6)
    boundary = TICK_TS_MS + 3 * STEP_MS
    report = evaluate([run_of(traces, observations, boundaries=(boundary,))], context=context)

    assert sum(segment.purged_outcomes for segment in report.segments) > 0


def test_invariant_6_c_a_boundary_outside_the_run_is_rejected(
    context: EvaluationContext,
) -> None:
    """run の外の境界で、存在しない区間を作らせない。"""
    traces, observations = plan_following_run()
    with pytest.raises(EvaluationInputError, match="split の境界"):
        evaluate([run_of(traces, observations, boundaries=(1,))], context=context)


def test_invariant_6_d_the_gate_reads_only_the_holdout_segment(
    context: EvaluationContext,
) -> None:
    """gate は**最後の区間**だけで判定する（未来で較正しない）。"""
    ceiling = context.control.safety.absolute_temp_ceiling_c.value
    traces, observations = plan_following_run(ticks=6)
    boundary = TICK_TS_MS + 3 * STEP_MS
    hot_early = [
        observation(item.metric, item.ts_ms, ceiling + 5.0)
        if item.metric == GPU and item.ts_ms < boundary
        else item
        for item in observations
    ]
    report = evaluate([run_of(traces, hot_early, boundaries=(boundary,))], context=context)
    gate = next(item for item in report.gates if item.arm_key.startswith("applied:"))
    exceeded = next(item for item in gate.conditions if item.name == "ceiling_exceedances")

    # 前半の超過は holdout の判定には入らない。**worst-case 一覧には残る。**
    assert exceeded.observed == 0.0
    assert any(
        case.kind is WorstCaseKind.CEILING_EXCEEDANCES and case.value > 0
        for case in report.worst_cases
    )


# ------------------- 不変条件 7: 契約と食い違う記録を通さない


def test_invariant_7_a_a_shadow_export_matched_with_another_tolerance_is_refused(
    context: EvaluationContext,
) -> None:
    """**違う許容幅で照合された結果を、同じ coverage として並べない**（0054 §2.6）。"""
    traces, observations = plan_following_run()
    rows = tuple(shadow_rows(traces, observations=observations, matcher=context.matcher()))
    widened = tuple(
        row.model_copy(
            update={
                "outcomes": tuple(
                    outcome.model_copy(
                        update={"match_tolerance_ms": context.matcher().match_tolerance_ms + 1}
                    )
                    for outcome in row.outcomes
                )
            }
        )
        for row in rows
    )
    with pytest.raises(EvaluationInputError, match="照合許容幅が設定と違う"):
        evaluate([run_of(traces, observations, shadow=widened)], context=context)


def test_invariant_7_b_a_trace_whose_index_disagrees_with_its_body_is_refused(
    context: EvaluationContext,
) -> None:
    """索引と中身が食い違う trace を、そのまま評価へ流さない。"""
    traces, observations = plan_following_run()
    broken = traces[0].model_copy(update={"tick_id": traces[0].tick_id + 100})
    with pytest.raises(EvaluationInputError, match="index と JSON 本文"):
        evaluate([run_of([broken, *traces[1:]], observations)], context=context)


def test_invariant_7_c_a_shadow_export_row_must_match_the_stored_trace(
    context: EvaluationContext,
) -> None:
    """別の記録を貼り替えた export を、その tick の実績として読まない。"""
    traces, observations = plan_following_run()
    rows = list(shadow_rows(traces, observations=observations, matcher=context.matcher()))
    swapped = rows[0].model_copy(update={"shadow": rows[1].shadow})
    with pytest.raises(EvaluationInputError, match="trace と一致しない"):
        evaluate([run_of(traces, observations, shadow=(swapped, *rows[1:]))], context=context)


def test_invariant_7_d_an_outcome_from_another_inference_is_refused(
    context: EvaluationContext,
) -> None:
    """outcome は `inference_id` と `plan_digest` で結び直す。**結べなければ受け取らない。**

    数えないだけにすると、壊れた export が「予測が無かった」として素通りする。
    """
    traces, observations = plan_following_run()
    rows = list(shadow_rows(traces, observations=observations, matcher=context.matcher()))
    relabelled = tuple(
        row.model_copy(
            update={
                "outcomes": tuple(
                    outcome.model_copy(update={"inference_id": "f" * 64})
                    for outcome in row.outcomes
                )
            }
        )
        for row in rows
    )
    with pytest.raises(EvaluationInputError, match="結べない"):
        evaluate([run_of(traces, observations, shadow=relabelled)], context=context)


def test_invariant_7_f_a_duplicated_shadow_row_cannot_inflate_the_coverage(
    context: EvaluationContext,
) -> None:
    """**行を複製するだけで採点数と coverage の下限を満たせない。**"""
    traces, observations = plan_following_run()
    rows = tuple(shadow_rows(traces, observations=observations, matcher=context.matcher()))
    with pytest.raises(EvaluationInputError, match="同じ tick の行が複数"):
        evaluate([run_of(traces, observations, shadow=(*rows, rows[0]))], context=context)


def test_invariant_7_g_a_duplicated_outcome_cannot_inflate_the_scored_count(
    context: EvaluationContext,
) -> None:
    """同じ推論・同じ候補 plan の outcome を2つ置いて採点数を増やせない。"""
    traces, observations = plan_following_run()
    rows = list(shadow_rows(traces, observations=observations, matcher=context.matcher()))
    doubled = rows[0].model_copy(update={"outcomes": rows[0].outcomes * 2})
    with pytest.raises(EvaluationInputError, match="同じ推論・同じ候補 plan の outcome"):
        evaluate([run_of(traces, observations, shadow=(doubled, *rows[1:]))], context=context)


def test_invariant_7_h_a_shadow_tick_without_a_row_is_refused(
    context: EvaluationContext,
) -> None:
    """counterfactual を持つ tick と export の行は**1対1**。抜けを「提案が無かった」にしない。"""
    traces, observations = plan_following_run()
    rows = tuple(shadow_rows(traces, observations=observations, matcher=context.matcher()))
    with pytest.raises(EvaluationInputError, match="1対1でない"):
        evaluate([run_of(traces, observations, shadow=rows[1:])], context=context)


def test_invariant_7_e_the_shadow_export_round_trips(context: EvaluationContext) -> None:
    """#90 の export（JSON Lines）を、そのまま評価へ取り込める（受入基準）。"""
    traces, observations = plan_following_run()
    rows = tuple(shadow_rows(traces, observations=observations, matcher=context.matcher()))
    stream = _StringIO()
    write_shadow_jsonl(rows, stream)
    restored = read_shadow_jsonl(_StringIO(stream.getvalue()))

    assert restored == rows
    from_file = evaluate([run_of(traces, observations, shadow=restored)], context=context)
    computed = evaluate([run_of(traces, observations)], context=context)
    assert render(from_file) == render(computed)


def _StringIO(text: str = ""):
    import io

    return io.StringIO(text)


# ------------------------------ 不変条件 8: 同じ入力からは同じ bytes


def test_invariant_8_a_the_same_input_produces_the_same_report(
    context: EvaluationContext,
) -> None:
    """再現できない比較は、条件の違いと制御の違いを分けられない。"""
    traces, observations = plan_following_run()
    first = evaluate([run_of(traces, observations)], context=context)
    second = evaluate([run_of(traces, observations)], context=context)

    assert render(first) == render(second)
    assert first.provenance.conditions_sha256 == second.provenance.conditions_sha256


def test_invariant_8_b_the_input_order_does_not_change_the_report(
    context: EvaluationContext,
) -> None:
    """並び順で結果が変わると、同じ条件かどうかを digest で確かめられない。"""
    traces, observations = plan_following_run()
    ordered = evaluate([run_of(traces, observations)], context=context)
    shuffled = evaluate(
        [run_of(list(reversed(traces)), list(reversed(observations)))], context=context
    )

    assert render(ordered) == render(shuffled)


def test_invariant_8_c_the_report_records_the_versions_and_configs_it_used(
    context: EvaluationContext,
) -> None:
    """**controller / model / config の版を記録する**（#91 の受入基準）。"""
    traces, observations = plan_following_run()
    report = evaluate([run_of(traces, observations)], context=context)
    provenance = report.provenance

    assert provenance.safety_config_sha256 == context.control.sources.safety.sha256
    assert provenance.fan_policy_config_sha256 == context.control.sources.policy.sha256
    assert provenance.evaluation_config_sha256 == context.config_sha256
    assert provenance.versions.model_versions == ("thermal-v1",)
    assert provenance.versions.model_artifacts == ("a" * 64,)
    assert provenance.versions.control_schema_versions == (7,)
    assert provenance.outcome_match_tolerance_ms == (
        context.control.policy.shadow.outcome_match_tolerance_ms.value
    )
    assert provenance.absolute_temp_ceiling_c == (
        context.control.safety.absolute_temp_ceiling_c.value
    )
    assert provenance.runs[0].trace_sha256 != provenance.runs[0].observation_sha256


# ---------------------- 不変条件 9: 設定の既定値をコードに置かない


@pytest.mark.parametrize(
    "path",
    [
        ("percentiles",),
        ("worst_case_count",),
        ("hunting",),
        ("observation_match_tolerance_ms",),
        ("room_temperature_bands",),
        ("gate",),
        ("temperature_metrics",),
        ("room_temperature_metric",),
    ],
)
def test_invariant_9_a_every_setting_is_required(path: tuple[str, ...]) -> None:
    """設定を1つ抜くと読み込みで落ちる。**コード側の既定値で埋めない**（AGENTS.md ルール9）。"""
    document = yaml.safe_load(EVALUATION_YAML.read_text(encoding="utf-8"))
    del document[path[0]]
    with pytest.raises(ValidationError):
        EvaluationConfig.model_validate(document)


def test_invariant_9_b_the_copied_metric_limit_matches_the_contract_it_copies() -> None:
    """**写した上限が、元の契約と食い違わない。** 狭いと記録できた予測を評価できない。"""
    from coldaisle.control.evaluation.config import MAX_EVALUATION_METRICS

    assert MAX_EVALUATION_METRICS == MAX_SHADOW_PREDICTION_METRICS


def test_invariant_9_c_shipped_values_are_marked_provisional() -> None:
    """実測前の値を `confirmed` として出荷しない。"""
    document = yaml.safe_load(EVALUATION_YAML.read_text(encoding="utf-8"))
    statuses = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "status" in node:
                statuses.append(node["status"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    assert statuses and set(statuses) == {"provisional"}


def test_invariant_9_d_delta_formulas_come_from_the_metric_catalog(tmp_path: Path) -> None:
    """ΔT の式を評価設定へ写させない。**`config/metrics.yaml` に無い名前は拒む。**"""
    directory = tmp_path / "pr91-config-delta"
    directory.mkdir()
    write_documents(directory, valid_documents())
    document = yaml.safe_load(EVALUATION_YAML.read_text(encoding="utf-8"))
    document["delta_metrics"] = ["d.not_in_the_catalog"]
    with pytest.raises(EvaluationInputError, match="派生値"):
        EvaluationContext.build(
            config=EvaluationConfig.model_validate(document),
            config_sha256="c" * 64,
            control=ControlConfig.from_directory(directory),
            catalog=MetricCatalog.from_yaml(METRICS_YAML),
            catalog_sha256="b" * 64,
        )


def test_invariant_9_e_a_band_without_an_upper_bound_must_come_last() -> None:
    """帯の並びが崩れると、その先の室温が黙ってどこにも入らない。"""
    document = yaml.safe_load(EVALUATION_YAML.read_text(encoding="utf-8"))
    document["room_temperature_bands"] = [
        {"name": "any"},
        {"name": "cool", "below_c": {"value": 20.0, "status": "provisional"}},
    ]
    with pytest.raises(ValidationError, match="上限のない室温帯は最後"):
        EvaluationConfig.model_validate(document)


# ------------------------------ 不変条件 10: worst-case を必ず載せる


def test_invariant_10_a_a_report_with_ticks_must_carry_worst_cases(
    context: EvaluationContext,
) -> None:
    """平均だけを見て「問題なかった」と読ませない。"""
    traces, observations = plan_following_run()
    report = evaluate([run_of(traces, observations)], context=context)

    assert report.worst_cases
    stripped = report.model_dump()
    stripped["worst_cases"] = ()
    with pytest.raises(ValidationError, match="worst-case を必ず載せる"):
        EvaluationReport.model_validate(stripped)


def test_invariant_10_b_the_worst_run_surfaces_even_next_to_good_runs(
    context: EvaluationContext,
) -> None:
    """1つ悪い run があれば、良い run の数で薄まらずに出てくる。"""
    ceiling = context.control.safety.absolute_temp_ceiling_c.value
    good_traces, good_observations = plan_following_run()
    bad_traces, bad_observations = plan_following_run(start_ms=TICK_TS_MS + 10 * STEP_MS)
    bad_observations = [
        observation(item.metric, item.ts_ms, ceiling + 9.0) if item.metric == GPU else item
        for item in bad_observations
    ]
    report = evaluate(
        [
            run_of(good_traces, good_observations, run_id="pr91-good"),
            run_of(bad_traces, bad_observations, run_id="pr91-bad"),
        ],
        context=context,
    )
    worst = next(
        case for case in report.worst_cases if case.kind is WorstCaseKind.MINIMUM_THRESHOLD_MARGIN
    )

    assert worst.run_id == "pr91-bad"
    assert worst.value < 0.0


# ------------------ 不変条件 11: Guard / Safety の効きは「分解」として出す


def test_invariant_11_a_guard_and_safety_show_up_as_interventions_not_as_arms(
    context: EvaluationContext,
) -> None:
    """**「Guard 無し MPC」という arm を作らない**（0054 §2.1）。

    Guard / Safety の効きは、同じ arm の `requested` と `effective` の差と介入回数で見る。
    """
    traces = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                applied=0.9,
                requested=0.3,
                bound_by=BoundBy.GUARD_FLOOR,
                guard_floor=0.9,
            )
        )
        for index in range(4)
    ]
    report = evaluate([run_of(traces, [])], context=context)
    applied = overall(report).applied[0]

    assert applied.interventions.guard_floor_ticks == 4
    assert applied.interventions.guard_active_ticks == 4
    assert applied.effective_demand[0].shape.level.mean == pytest.approx(0.9)
    assert applied.requested_demand[0].shape.level.mean == pytest.approx(0.3)
    assert "guard" not in applied.arm_key


def test_invariant_11_b_missing_evidence_is_recorded_as_a_gap_not_as_zero(
    context: EvaluationContext,
) -> None:
    """**欄を埋め合わせない。** 出せなかった指標は理由付きで残す。"""
    traces = [
        trace_of(tick_at(TICK_TS_MS + index * STEP_MS, index, rpm=None, flow=None))
        for index in range(3)
    ]
    report = evaluate([run_of(traces, [])], context=context)
    applied = overall(report).applied[0]

    assert applied.rpm == ()
    assert applied.air_balance is None
    assert applied.acoustic_cost is None
    codes = {gap.code for gap in applied.gaps}
    assert {"no_rpm_readback", "no_estimated_flow", "no_acoustic_model"} <= codes
    assert applied.temperatures == ()


# ------------------------------ 集計の切り口と要約統計


def test_groups_cover_workload_regime_and_room_temperature_band(
    context: EvaluationContext,
) -> None:
    """**workload regime / 室温帯ごとに評価する**（Issue の評価原則）。"""
    traces = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                state=control_state(
                    regime=WorkloadRegime.IDLE if index < 2 else WorkloadRegime.SUSTAINED_GPU
                ),
            )
        )
        for index in range(4)
    ]
    observations = [
        observation(ROOM, TICK_TS_MS + index * STEP_MS, 20.0 if index < 2 else 30.0)
        for index in range(4)
    ]
    report = evaluate([run_of(traces, observations)], context=context)
    groups = {(group.kind, group.value) for group in report.segments[0].groups}

    assert (GroupKind.WORKLOAD_REGIME, "idle") in groups
    assert (GroupKind.WORKLOAD_REGIME, "sustained_gpu") in groups
    assert (GroupKind.ROOM_TEMPERATURE_BAND, "cool") in groups
    assert (GroupKind.ROOM_TEMPERATURE_BAND, "warm") in groups


def test_ticks_without_a_room_observation_land_in_the_unknown_band(
    context: EvaluationContext,
) -> None:
    """室温が読めない tick を**黙って落とさない**。"""
    traces, _observations = plan_following_run(ticks=3)
    report = evaluate([run_of(traces, [])], context=context)
    bands = {
        group.value
        for group in report.segments[0].groups
        if group.kind is GroupKind.ROOM_TEMPERATURE_BAND
    }

    assert bands == {"unknown"}


def test_percentiles_use_nearest_rank_and_never_invent_a_value() -> None:
    """percentile は入力に**実在する値**を返す（補間すると足し算の順序で揺れる）。"""
    summary = summarize([1.0, 2.0, 3.0, 4.0], quantiles=(0.95, 0.99))
    assert summary is not None
    assert [item.value for item in summary.percentiles] == [4.0, 4.0]
    assert summary.minimum == 1.0 and summary.maximum == 4.0


def test_hunting_ignores_changes_inside_the_deadband() -> None:
    """不感帯の中の揺れを「向きの反転」に数えない。"""
    noisy = [(0, 0.50), (1_000, 0.505), (2_000, 0.50), (3_000, 0.505)]
    swinging = [(0, 0.2), (1_000, 0.8), (2_000, 0.2), (3_000, 0.8)]

    assert (shape_of(noisy, deadband=0.02) or _never_shape()).reversals == 0
    assert (shape_of(swinging, deadband=0.02) or _never_shape()).reversals == 2


def _never_shape():  # pragma: no cover - 到達しない
    raise AssertionError("shape_of は点があれば必ず返す")


def test_a_single_sample_has_no_hunting_rate() -> None:
    """時間の幅が 0 のときに、0 除算を大きな数で埋め合わせない。"""
    shape = shape_of([(0, 0.5)], deadband=0.01)
    assert shape is not None
    assert shape.reversals_per_hour is None and shape.step is None


def test_optimizer_latency_and_timeout_rate_come_from_the_record(
    context: EvaluationContext,
) -> None:
    """optimizer の latency / timeout を**記録から**数える（受入基準の指標）。"""
    traces = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                cf=counterfactual(
                    action_ts_ms=TICK_TS_MS + index * STEP_MS,
                    status=OptimizerStatus.TIMEOUT if index == 3 else OptimizerStatus.OK,
                    latency_ms=100 + index,
                ),
            )
        )
        for index in range(4)
    ]
    report = evaluate([run_of(traces, [])], context=context)
    optimizer = overall(report).counterfactual[0].optimizer

    assert optimizer is not None
    assert optimizer.samples == 4 and optimizer.timeout == 1
    assert optimizer.timeout_rate == pytest.approx(0.25)
    assert optimizer.latency_ms is not None and optimizer.latency_ms.maximum == 103.0


def test_air_balance_uses_the_same_ratio_as_the_air_balance_model(
    context: EvaluationContext,
) -> None:
    """吸排気比の定義を `AirBalanceEstimate.balance_ratio` と揃える。"""
    traces = [
        trace_of(tick_at(TICK_TS_MS + index * STEP_MS, index, flow=2.0)) for index in range(3)
    ]
    report = evaluate([run_of(traces, [])], context=context)
    balance = overall(report).applied[0].air_balance

    assert balance is not None
    # 3 zone とも同じ推定風量なので (rear + top) / front = 2.0
    assert balance.ratio.mean == pytest.approx(2.0)
    assert balance.ticks_without_ratio == 0


def test_underprediction_is_the_direction_where_reality_is_hotter(
    context: EvaluationContext,
) -> None:
    """`error = 実測 - 予測`。**正の側**が冷却の足りない外し方である。"""
    traces, observations = plan_following_run(ticks=6)
    hot = [
        observation(item.metric, item.ts_ms, 60.0) if item.metric == GPU else item
        for item in observations
    ]
    rows = tuple(shadow_rows(traces, observations=hot, matcher=context.matcher()))
    scored = [outcome for row in rows for outcome in row.outcomes if outcome.scored]

    assert scored, "plan どおりに適用された区間があれば採点できる"
    assert all(match.error is not None and match.error > 0 for match in scored[0].matches)


# ------------------------------------------------- 1コマンドで再現できる


def test_the_cli_writes_a_deterministic_report(tmp_path: Path) -> None:
    """**1コマンド/再現可能な設定で比較レポートを生成できる**（受入基準）。"""
    from coldaisle.clock import SimulatedClock
    from coldaisle.store import QualityRules, SqliteStore
    from coldaisle.store.models import Reading, Sample

    directory = tmp_path / "pr91-cli-config"
    directory.mkdir()
    write_documents(directory, valid_documents())
    db = tmp_path / "pr91.db"
    traces, observations = plan_following_run(ticks=6)
    clock = SimulatedClock(TICK_TS_MS + 100 * STEP_MS)
    with SqliteStore(
        db, rules=QualityRules.from_yaml(ROOT / "config" / "quality.yaml"), clock=clock
    ) as store:
        for trace in traces:
            store.record_control_trace(
                ts_ms=trace.ts_ms,
                tick_id=trace.tick_id,
                schema_version=trace.schema_version,
                trace_json=trace.trace_json,
            )
        by_time: dict[int, list[Reading]] = {}
        for item in observations:
            by_time.setdefault(item.ts_ms, []).append(
                Reading(metric=item.metric, value=item.value, quality=item.quality)
            )
        store.insert_samples(
            Sample(ts_ms=ts_ms, readings=tuple(readings))
            for ts_ms, readings in sorted(by_time.items())
        )

    manifest = tmp_path / "pr91-runs.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "runs": [
                    {
                        "run_id": "pr91-cli",
                        "start_ms": TICK_TS_MS,
                        "end_ms": TICK_TS_MS + 20 * STEP_MS,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "pr91-report.json"
    argv = [
        "--runs",
        str(manifest),
        "--db",
        str(db),
        "--config",
        str(EVALUATION_YAML),
        "--control-config",
        str(directory),
        "--metrics",
        str(METRICS_YAML),
        "--out",
        str(out),
    ]

    assert main(argv) == 0
    first = out.read_text(encoding="utf-8")
    assert main(argv) == 0
    assert out.read_text(encoding="utf-8") == first

    document = json.loads(first)
    assert document["schema_version"] == 2
    assert document["segments"][0]["run_id"] == "pr91-cli"
    assert "generated_at" not in document


def test_the_run_manifest_refuses_an_inverted_window(tmp_path: Path) -> None:
    """時刻の向きが逆の run を受け取らない。"""
    path = tmp_path / "pr91-bad-runs.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "runs": [{"run_id": "pr91", "start_ms": 10, "end_ms": 5}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="start_ms < end_ms"):
        RunsManifest.from_file(path)


def test_build_context_reads_the_shipped_configuration(tmp_path: Path) -> None:
    """出荷している `config/evaluation.yaml` と `config/metrics.yaml` で組み立てられる。"""
    directory = tmp_path / "pr91-shipped"
    directory.mkdir()
    write_documents(directory, valid_documents())
    built = build_context(
        config_path=EVALUATION_YAML,
        control_dir=directory,
        metrics_path=METRICS_YAML,
        acoustic_path=None,
    )

    assert built.deltas and all(delta.minuend for delta in built.deltas)
    assert built.config.schema_version == 1


def test_temperature_and_margin_come_from_the_safety_contract(
    context: EvaluationContext,
) -> None:
    """threshold margin の閾値は `safety.yaml` の値そのもの（評価設定に写さない）。"""
    traces, observations = plan_following_run(ticks=4)
    report = evaluate([run_of(traces, observations)], context=context)
    temperature: TemperatureReport = overall(report).applied[0].temperatures[0]
    ceiling = context.control.safety.absolute_temp_ceiling_c.value

    assert temperature.threshold_c == ceiling
    assert temperature.margin.minimum == pytest.approx(ceiling - temperature.values.maximum)
    assert temperature.exceedances == 0


# ------------- 不変条件 12: 部分的な証拠を「揃っている」として扱わない（Codex レビュー）


def test_invariant_12_a_partial_temperature_evidence_blocks_the_safety_gate(
    context: EvaluationContext,
) -> None:
    """**1つの温度 metric の証拠だけで Safety の段を通さない**（決定記録 0054 §2.3）。

    欠けた metric の超過を見ないまま合格になるのを防ぐ。「1つも無い」と「一部だけ」は
    別の理由として残す。
    """
    traces, observations = plan_following_run()
    partial = [item for item in observations if item.metric != "cpu.package"]
    report = evaluate([run_of(traces, partial)], context=context)
    gate = next(item for item in report.gates if item.arm_key.startswith("applied:"))
    exceedances = next(item for item in gate.conditions if item.name == "ceiling_exceedances")
    margin = next(item for item in gate.conditions if item.name == "threshold_margin_c")

    assert gate.outcome is GateOutcome.BLOCKED
    assert gate.blocking_stage is GateStage.SAFETY
    for condition in (exceedances, margin):
        assert condition.observed is None
        assert condition.reason is not None
        assert condition.reason.code == "incomplete_temperature_evidence"


def test_invariant_12_b_no_temperature_evidence_is_a_different_reason(
    context: EvaluationContext,
) -> None:
    """「1つも読めていない」を「一部だけ読めた」と同じ理由にしない。"""
    traces, _observations = plan_following_run()
    report = evaluate([run_of(traces, [])], context=context)
    gate = next(item for item in report.gates if item.arm_key.startswith("applied:"))
    margin = next(item for item in gate.conditions if item.name == "threshold_margin_c")

    assert margin.reason is not None and margin.reason.code == "no_temperature_evidence"


def test_invariant_12_c_one_insufficient_segment_blocks_the_whole_arm(
    context: EvaluationContext,
) -> None:
    """**coverage は評価したすべての holdout segment で足りていること**（0054 §2.3）。

    足りない segment の採点数をほかの segment と足すと、「証拠は伏せた区間から、
    指標は出した区間から」という取り合わせになる。`scored_outcomes` も最小で見る。
    """
    long_traces, long_observations = plan_following_run(ticks=60)
    short_traces, short_observations = plan_following_run(
        ticks=6, start_ms=TICK_TS_MS + 200 * STEP_MS
    )
    report = evaluate(
        [
            run_of(long_traces, long_observations, run_id="pr91-long"),
            run_of(short_traces, short_observations, run_id="pr91-short"),
        ],
        context=context,
    )
    gate = next(item for item in report.gates if item.arm_key.startswith("counterfactual:"))
    insufficient = next(
        item for item in gate.conditions if item.name == "insufficient_coverage_segments"
    )
    scored = next(item for item in gate.conditions if item.name == "scored_outcomes")

    assert insufficient.observed == 1.0 and insufficient.outcome is GateOutcome.BLOCKED
    # **合計（60 超）ではなく、足りない segment の採点数が出ている。**
    assert scored.observed is not None
    assert scored.observed < context.config.gate.evidence.minimum_scored_outcomes.value
    assert gate.outcome is GateOutcome.BLOCKED


def test_a_run_with_enough_coverage_passes_the_evidence_stage(
    context: EvaluationContext,
) -> None:
    """fail closed を入れても、**証拠が揃えば通る**（常に blocked な gate ではない）。"""
    traces, observations = plan_following_run(ticks=60)
    report = evaluate([run_of(traces, observations)], context=context)
    gate = next(item for item in report.gates if item.arm_key.startswith("counterfactual:"))
    evidence = [item for item in gate.conditions if item.stage is GateStage.EVIDENCE]

    assert all(item.outcome is GateOutcome.PASS for item in evidence), gate
    applied_gate = next(item for item in report.gates if item.arm_key.startswith("applied:"))
    assert applied_gate.outcome is GateOutcome.PASS, applied_gate


def test_invariant_12_d_an_applied_learned_mpc_arm_records_the_missing_optimizer(
    context: EvaluationContext,
) -> None:
    """**「optimizer があるのに記録が無い」を「該当しない」と区別する**（0054 §3）。"""
    gate = ModelGateDecision(
        model_version="thermal-v1",
        inference_id="c" * 64,
        artifact_sha256="a" * 64,
        attested=True,
        confidence=0.9,
        ood=False,
        confidence_level=ConfidenceLevel.HIGH,
        authority_stage=AuthorityStage.FULL,
        learned_selected=True,
        assessment=tuple(Reason(code=component) for component in MODEL_GATE_ASSESSMENT_COMPONENTS),
    )
    learned = ControlState(
        operating_mode=OperatingMode.AUTO,
        authority_stage=AuthorityStage.FULL,
        active_controller=ControllerKind.LEARNED_MPC,
        safety_state=SafetyState.NORMAL,
        fallback_active=False,
        workload_regime=WorkloadRegime.SUSTAINED_GPU,
        regime_confidence=0.9,
        model_version="thermal-v1",
        model_confidence=0.9,
        model_ood=False,
    )
    traces = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                shadow=False,
                state=learned,
                model_gate=gate,
            )
        )
        for index in range(3)
    ]
    fallback = [
        trace_of(tick_at(TICK_TS_MS + index * STEP_MS, index, shadow=False)) for index in range(3)
    ]
    learned_report = evaluate([run_of(traces, [])], context=context)
    fallback_report = evaluate([run_of(fallback, [])], context=context)

    learned_gaps = {gap.code for gap in overall(learned_report).applied[0].gaps}
    fallback_gaps = {gap.code for gap in overall(fallback_report).applied[0].gaps}
    assert "applied_optimizer_record_unavailable" in learned_gaps
    assert "applied_optimizer_record_unavailable" not in fallback_gaps


def test_invariant_12_e_partial_rpm_readback_is_recorded_as_a_gap(
    context: EvaluationContext,
) -> None:
    """一部の zone だけ RPM が読めている状態を「読めている」と読ませない。"""
    ticks = [tick_at(TICK_TS_MS + index * STEP_MS, index) for index in range(3)]
    stripped = []
    for tick in ticks:
        zones = tick.zones.model_copy(
            update={"top": tick.zones.top.model_copy(update={"hardware": None})}
        )
        stripped.append(trace_of(tick.model_copy(update={"zones": zones})))
    report = evaluate([run_of(stripped, [])], context=context)
    applied = overall(report).applied[0]

    assert len(applied.rpm) == 2
    assert "partial_rpm_readback" in {gap.code for gap in applied.gaps}


def test_invariant_12_f_a_boundary_that_leaves_a_segment_empty_is_refused(
    context: EvaluationContext,
) -> None:
    """**tick の無い区間を作らない。** 空の holdout は何も判定しない gate になる。"""
    traces, observations = plan_following_run(ticks=4)
    between = TICK_TS_MS + STEP_MS + 1
    with pytest.raises(EvaluationInputError, match="tick の無い区間"):
        evaluate([run_of(traces, observations, boundaries=(between, between + 1))], context=context)


def test_invariant_12_g_contradictory_duplicate_observations_are_refused(
    context: EvaluationContext,
) -> None:
    """**同じ metric・同じ時刻の食い違う観測を、評価が選ばない**（決定記録 0054 §2.6）。

    小さいほうを採れば絶対上限の超過が消え、大きいほうを採れば予測の当たりが消える。
    どちらを選んでも片方の事実が消えるので、入力の誤りとして受け取らない。
    """
    traces, observations = plan_following_run(ticks=4)
    ceiling = context.control.safety.absolute_temp_ceiling_c.value
    hotter = observation(GPU, observations[0].ts_ms, ceiling + 10.0)
    with pytest.raises(EvaluationInputError, match="食い違う観測"):
        evaluate([run_of(traces, [*observations, hotter])], context=context)
    # 順序を入れ替えても同じ。**「先に来たほうが勝つ」規則も置かない。**
    with pytest.raises(EvaluationInputError, match="食い違う観測"):
        evaluate([run_of(traces, [hotter, *observations])], context=context)


def test_the_same_observation_twice_is_allowed(context: EvaluationContext) -> None:
    """同じ値が2度届くのは冪等な取り込みで起きる。**それは食い違いではない。**"""
    traces, observations = plan_following_run(ticks=4)
    report = evaluate([run_of(traces, [*observations, observations[0]])], context=context)
    once = evaluate([run_of(traces, observations)], context=context)

    assert render(report) == render(once)


def test_invariant_12_h_observations_no_tick_claims_are_counted_not_dropped(
    context: EvaluationContext,
) -> None:
    """どの tick からも離れた観測を**黙って落とさない**（帰属させないだけで、数える）。"""
    tolerance_ms = context.config.observation_match_tolerance_ms.value
    far_apart = 4 * tolerance_ms
    traces = [
        trace_of(tick_at(TICK_TS_MS, 0, shadow=False)),
        trace_of(tick_at(TICK_TS_MS + far_apart, 1, shadow=False)),
    ]
    # **どちらの tick からも許容幅の外**に落ちる観測。
    stray = observation(GPU, TICK_TS_MS + far_apart // 2, 55.0)
    near = observation(GPU, TICK_TS_MS + 1, 51.0)
    report = evaluate([run_of(traces, [near, stray])], context=context)

    assert report.segments[0].unattributed_observations == 1
    assert overall(report).applied[0].temperatures[0].values.count == 1


def test_invariant_12_i_a_report_always_carries_a_gate_result(
    context: EvaluationContext,
) -> None:
    """**条件が1つも無い結果を「何も落ちなかった」と読ませない。**"""
    traces, observations = plan_following_run(ticks=3)
    report = evaluate([run_of(traces, observations)], context=context)
    assert report.gates

    stripped = report.model_dump()
    stripped["gates"] = ()
    with pytest.raises(ValidationError, match="gate の結果を必ず載せる"):
        EvaluationReport.model_validate(stripped)


# ------------- 不変条件 13: 渡された実績は数え直して照らす（Codex レビュー第2回）


def _tampered(rows: list[ShadowExportRow], **updates: object) -> tuple[ShadowExportRow, ...]:
    """識別子と許容幅はそのままに、**採点の中身だけを書き換えた** export を作る。"""
    first = rows[0]
    outcome = first.outcomes[0]
    return (
        first.model_copy(
            update={"outcomes": (outcome.model_copy(update=updates), *first.outcomes[1:])}
        ),
        *rows[1:],
    )


def test_invariant_13_a_a_tampered_outcome_status_is_refused(
    context: EvaluationContext,
) -> None:
    """**採点していない区間を `scored` に仕立てられない。**

    識別子（`inference_id` / `plan_digest`）と許容幅は本物のままでも、`status` を
    書き換えれば「当たっていた」記録になる。同じ trace と観測から数え直して閉じる。
    """
    aside = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                applied=0.2,  # plan は 0.8。掛かっていた action が違う
                cf=counterfactual(action_ts_ms=TICK_TS_MS + index * STEP_MS),
            )
        )
        for index in range(6)
    ]
    observations = [
        observation(metric, TICK_TS_MS + index * STEP_MS, 50.0)
        for index in range(10)
        for metric in TEMPERATURES
    ]
    rows = list(shadow_rows(aside, observations=observations, matcher=context.matcher()))
    assert rows[0].outcomes[0].status is OutcomeStatus.UNIDENTIFIABLE

    forged = _tampered(
        rows,
        status=OutcomeStatus.SCORED,
        unidentifiable=None,
        matches=tuple(
            match.model_copy(update={"error": 0.0, "observed": match.predicted})
            for match in rows[0].outcomes[0].matches
        ),
    )
    with pytest.raises(EvaluationInputError, match="数え直した結果と違う"):
        evaluate([run_of(aside, observations, shadow=forged)], context=context)


def test_invariant_13_b_a_rewritten_error_is_refused(context: EvaluationContext) -> None:
    """**外れた予測を「誤差の小さい予測」に仕立てられない。**"""
    traces, observations = plan_following_run(ticks=6)
    rows = list(shadow_rows(traces, observations=observations, matcher=context.matcher()))
    scored = next(
        (index, row) for index, row in enumerate(rows) if any(o.scored for o in row.outcomes)
    )
    index, row = scored
    outcome = next(item for item in row.outcomes if item.scored)
    shrunk = outcome.model_copy(
        update={
            "matches": tuple(
                match.model_copy(update={"observed": match.predicted, "error": 0.0})
                if match.observed is not None
                else match
                for match in outcome.matches
            )
        }
    )
    forged = [*rows]
    forged[index] = row.model_copy(update={"outcomes": (shrunk,)})
    with pytest.raises(EvaluationInputError, match="数え直した結果と違う"):
        evaluate([run_of(traces, observations, shadow=tuple(forged))], context=context)


def test_invariant_13_c_a_rewritten_expected_time_is_refused(
    context: EvaluationContext,
) -> None:
    """**照合の時刻を動かして、別の観測を「当たり」に数えさせない。**"""
    traces, observations = plan_following_run(ticks=6)
    rows = list(shadow_rows(traces, observations=observations, matcher=context.matcher()))
    outcome = rows[0].outcomes[0]
    forged = _tampered(rows, input_action_ts_ms=outcome.input_action_ts_ms + 1)
    with pytest.raises(EvaluationInputError, match="数え直した結果と違う"):
        evaluate([run_of(traces, observations, shadow=forged)], context=context)


def test_invariant_13_d_exceedances_add_up_across_metrics_in_a_segment(
    context: EvaluationContext,
) -> None:
    """**同時に超えている metric の数を、metric ごとの最大で隠さない**（0054 §2.4）。"""
    traces, observations = plan_following_run(ticks=3)
    ceiling = context.control.safety.absolute_temp_ceiling_c.value
    hot = [
        observation(item.metric, item.ts_ms, ceiling + 1.0) if item.metric in TEMPERATURES else item
        for item in observations
    ]
    report = evaluate([run_of(traces, hot)], context=context)
    applied = overall(report).applied[0]
    per_metric = [item.exceedances for item in applied.temperatures]
    gate = next(item for item in report.gates if item.arm_key.startswith("applied:"))
    exceedances = next(item for item in gate.conditions if item.name == "ceiling_exceedances")

    assert len(per_metric) == len(TEMPERATURES)
    assert exceedances.observed == float(sum(per_metric))
    assert exceedances.observed > max(per_metric)


def test_invariant_13_e_the_split_is_part_of_the_conditions(
    context: EvaluationContext,
) -> None:
    """**違う切り方の比較が、同じ条件を名乗れない**（0054 §2.7）。"""
    traces, observations = plan_following_run(ticks=6)
    boundary = TICK_TS_MS + 3 * STEP_MS
    whole = evaluate([run_of(traces, observations)], context=context)
    split = evaluate([run_of(traces, observations, boundaries=(boundary,))], context=context)

    assert split.provenance.runs[0].split_boundaries_ms == (boundary,)
    assert whole.provenance.runs[0].split_boundaries_ms == ()
    assert whole.provenance.conditions_sha256 != split.provenance.conditions_sha256


def test_invariant_13_f_simultaneous_zone_interventions_are_not_hidden(
    context: EvaluationContext,
) -> None:
    """**tick 数だけだと、3 zone が同時に縛られても1に見える。** 総数も並べる。"""
    traces = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                applied=0.9,
                requested=0.3,
                bound_by=BoundBy.GUARD_FLOOR,
                guard_floor=0.9,
            )
        )
        for index in range(4)
    ]
    report = evaluate([run_of(traces, [])], context=context)
    interventions = overall(report).applied[0].interventions
    per_zone = {item.code: item.count for item in interventions.bound_zone_ticks}

    assert interventions.guard_floor_ticks == 4
    assert per_zone["guard_floor"] == 4 * len(Zone)


def test_invariant_13_g_the_run_id_is_checked_before_the_report_is_built(
    context: EvaluationContext,
) -> None:
    """報告の鍵になる名前は、**組み立てる前に**閉じる。"""
    traces, observations = plan_following_run(ticks=3)
    with pytest.raises(EvaluationInputError, match="run_id の形"):
        evaluate([run_of(traces, observations, run_id="../escape")], context=context)


def test_the_run_id_pattern_matches_the_report_contract() -> None:
    """**写した形が、報告の契約と食い違わない。**"""
    from coldaisle.control.evaluation.evaluator import RUN_ID_PATTERN
    from coldaisle.control.evaluation.model import RunProvenance

    field = RunProvenance.model_fields["run_id"]
    patterns = [item.pattern for item in field.metadata if hasattr(item, "pattern")]
    lengths = [item.max_length for item in field.metadata if hasattr(item, "max_length")]
    assert patterns == [r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"]
    assert lengths == [120]
    # 先頭の1文字を含めて 120 文字まで（報告の契約と同じ範囲）
    assert RUN_ID_PATTERN.fullmatch("a" * 120) is not None
    assert RUN_ID_PATTERN.fullmatch("a" * 121) is None
    assert RUN_ID_PATTERN.fullmatch("_leading") is None


# --------- 不変条件 14: 一部だけの要約を、全体の要約として扱わない（Codex レビュー第3回）


def test_invariant_14_a_a_never_matched_metric_blocks_the_coverage(
    context: EvaluationContext,
) -> None:
    """**`scored` は「action を識別できた」ことしか言わない**（決定記録 0054 §2.3）。

    識別できた outcome でも、ある metric の実測が一度も照合できなければ、その metric の
    誤差はどこにも出てこない。残りの metric だけで underprediction の gate を通せない。
    """
    hotspot = "gpu.0.hotspot"
    traces = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                cf=_two_metric_counterfactual(TICK_TS_MS + index * STEP_MS),
            )
        )
        for index in range(60)
    ]
    # **`gpu.0.hotspot` の実測を一度も置かない。** ほかの metric は揃っている。
    observations = [
        observation(metric, TICK_TS_MS + index * STEP_MS, 50.0)
        for index in range(64)
        for metric in TEMPERATURES
        if metric != hotspot
    ]
    report = evaluate([run_of(traces, observations)], context=context)
    shadow_arm = overall(report).counterfactual[0]

    assert hotspot in shadow_arm.coverage.predicted_metrics
    assert shadow_arm.coverage.unscored_metrics == (hotspot,)
    assert shadow_arm.coverage.scored > 0, "action は識別できている（scored は立つ）"
    assert shadow_arm.coverage.sufficient is False
    assert shadow_arm.predictions == ()

    gate = next(item for item in report.gates if item.arm_key.startswith("counterfactual:"))
    unscored = next(item for item in gate.conditions if item.name == "unscored_metrics")
    assert unscored.observed == 1.0 and unscored.outcome is GateOutcome.BLOCKED


def _two_metric_counterfactual(action_ts_ms: int) -> ShadowCounterfactual:
    """2つの metric を予測する counterfactual（片方だけ実測が来る状況を作る）。"""
    plan = plan_for(0.8)
    identifier = f"{action_ts_ms:064x}"
    prediction = ShadowPrediction(
        model_id="rack-thermal",
        model_version="thermal-v1",
        artifact_sha256="a" * 64,
        inference_id=identifier,
        plan_digest=plan.digest(),
        input_action_ts_ms=action_ts_ms,
        targets=tuple(
            ShadowPredictedTarget(
                offset_ms=offset,
                expected_ts_ms=action_ts_ms + offset,
                values={GPU: 50.0, "gpu.0.hotspot": 60.0},
            )
            for offset in plan.offsets_ms
        ),
    )
    return ShadowCounterfactual(
        controller=ControllerKind.LEARNED_MPC,
        requested=plan.first,
        reason=Reason(code="optimizer_ok"),
        optimizer_status=OptimizerStatus.OK,
        latency_ms=120,
        evaluations=64,
        model_version="thermal-v1",
        inference_id=identifier,
        artifact_sha256="a" * 64,
        plan=plan,
        prediction=prediction,
        cost_total=1.0,
        baseline_cost_total=2.0,
    )


def test_invariant_14_b_a_coverage_cannot_claim_sufficiency_with_an_unscored_metric() -> None:
    """型でも閉じる。**採点できていない metric があれば `sufficient` にできない。**"""
    with pytest.raises(ValidationError, match="採点できていない metric"):
        CoverageReport(
            outcomes=60,
            scored=60,
            unidentifiable=0,
            identifiable_fraction=1.0,
            outputs=240,
            matched_outputs=120,
            scored_outputs=120,
            predicted_metrics=(GPU, "gpu.0.hotspot"),
            unscored_metrics=("gpu.0.hotspot",),
            sufficient=True,
        )


def test_invariant_14_c_a_partly_recorded_latency_cannot_pass_the_gate(
    context: EvaluationContext,
) -> None:
    """**50 回のうち1回の記録で latency の gate を通さない**（0054 §2.3）。"""
    traces = [
        trace_of(
            tick_at(
                TICK_TS_MS + index * STEP_MS,
                index,
                cf=counterfactual(
                    action_ts_ms=TICK_TS_MS + index * STEP_MS,
                    status=OptimizerStatus.OK,
                    latency_ms=10,
                ),
            )
        )
        for index in range(60)
    ]
    # 1件だけ latency を落とす（`ShadowCounterfactual` は latency を必須にしない）。
    first = ControlTick.model_validate_json(traces[0].trace_json)
    assert first.shadow is not None
    stripped = first.shadow.counterfactuals[0].model_copy(update={"latency_ms": None})
    traces[0] = trace_of(
        first.model_copy(
            update={"shadow": first.shadow.model_copy(update={"counterfactuals": (stripped,)})}
        )
    )
    report = evaluate([run_of(traces, [])], context=context)
    optimizer = overall(report).counterfactual[0].optimizer

    assert optimizer is not None
    assert optimizer.samples == 60 and optimizer.latency_ms is not None
    assert optimizer.latency_ms.count == 59
    assert optimizer.latency_complete is False
    assert "partial_optimizer_latency" in {
        gap.code for gap in overall(report).counterfactual[0].gaps
    }

    gate = next(item for item in report.gates if item.arm_key.startswith("counterfactual:"))
    latency = next(item for item in gate.conditions if item.name == "optimizer_latency_ms")
    assert latency.observed is None and latency.outcome is GateOutcome.BLOCKED
    assert latency.reason is not None
    assert latency.reason.code == "incomplete_optimizer_latency"


def test_invariant_14_d_an_incomplete_summary_cannot_claim_completeness() -> None:
    """型でも閉じる。**全 sample に記録が無ければ `complete` にできない。**"""
    with pytest.raises(ValidationError, match="complete にできるのは"):
        OptimizerReport(
            samples=50,
            ok=50,
            timeout=0,
            error=0,
            timeout_rate=0.0,
            error_rate=0.0,
            latency_ms=summarize([10.0]) or _never(),
            latency_complete=True,
        )


def test_invariant_14_e_thin_temperature_evidence_blocks_the_gate(
    context: EvaluationContext,
) -> None:
    """**1000 tick に1件の観測で温度を語らせない**（0054 §2.3）。"""
    traces = [
        trace_of(tick_at(TICK_TS_MS + index * STEP_MS, index, shadow=False)) for index in range(40)
    ]
    # 各 metric の観測は**最初の tick の分だけ**。平均も margin も出せてしまう。
    thin = [observation(metric, TICK_TS_MS, 50.0) for metric in TEMPERATURES]
    report = evaluate([run_of(traces, thin)], context=context)
    applied = overall(report).applied[0]
    temperature = applied.temperatures[0]

    assert temperature.ticks == 40
    assert temperature.values.count == 1
    assert temperature.sample_coverage == pytest.approx(1 / 40)

    gate = next(item for item in report.gates if item.arm_key.startswith("applied:"))
    coverage = next(item for item in gate.conditions if item.name == "temperature_sample_coverage")
    assert coverage.outcome is GateOutcome.BLOCKED
    assert gate.blocking_stage is GateStage.EVIDENCE


# ------- 不変条件 15: 証拠の無い run と、証拠の DB の書き換え（Codex レビュー第4回）


def test_invariant_15_a_a_run_without_any_tick_is_refused(
    context: EvaluationContext,
) -> None:
    """**decision trace が1つも無い run を黙って通さない**（決定記録 0054 §2.5）。

    segment も gate も作られないので、その run は報告のどこにも現れず、ほかの run
    だけで `pass` が出てしまう（tick の無い segment を拒むのと同じ理由）。
    """
    traces, observations = plan_following_run(ticks=3)
    with pytest.raises(EvaluationInputError, match="decision trace が1つも無い"):
        evaluate(
            [
                run_of(traces, observations, run_id="pr91-has-ticks"),
                run_of([], [], run_id="pr91-empty"),
            ],
            context=context,
        )


def _evidence_db(path: Path, *, versions: int | None = None) -> None:
    """指定した版まで migration を当てた DB を作る（評価の入口を試すため）。"""
    import shutil
    import sqlite3

    from coldaisle.store import migrations

    directory = path.parent / f"pr91-migrations-{path.stem}"
    directory.mkdir(parents=True, exist_ok=True)
    for migration in migrations.discover():
        if versions is not None and migration.version > versions:
            break
        shutil.copy(migration.path, directory / migration.path.name)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        migrations.apply_pending(conn, now_ms=TICK_TS_MS, directory=directory)
    finally:
        conn.close()


def test_invariant_15_b_an_old_schema_evidence_db_is_not_migrated(tmp_path: Path) -> None:
    """**証拠の DB を開くだけで書き換えない。** 古ければ理由を言って落とす。"""
    import sqlite3

    from coldaisle.evaluate import (
        EvidenceDatabase,
        EvidenceDatabaseError,
        required_schema_version,
    )
    from coldaisle.store import migrations

    db = tmp_path / "pr91-old.db"
    _evidence_db(db, versions=required_schema_version() - 1)
    before = db.read_bytes()

    with pytest.raises(EvidenceDatabaseError, match="スキーマが古い"):
        EvidenceDatabase(db)

    conn = sqlite3.connect(db)
    try:
        assert migrations.current_version(conn) == required_schema_version() - 1
    finally:
        conn.close()
    assert db.read_bytes() == before, "開いただけで DB を書き換えない"


def test_invariant_15_c_a_missing_evidence_db_is_not_created(tmp_path: Path) -> None:
    """**path が無ければ作らずに落とす。** 空の DB を作って「証拠が無い」にしない。"""
    from coldaisle.evaluate import EvidenceDatabase, EvidenceDatabaseError

    missing = tmp_path / "pr91-missing.db"
    with pytest.raises(EvidenceDatabaseError, match="証拠の DB が無い"):
        EvidenceDatabase(missing)
    assert not missing.exists()


def test_invariant_15_d_the_evidence_database_cannot_write(tmp_path: Path) -> None:
    """読み取り専用で開く。**書き込みは sqlite が拒む。**"""
    import sqlite3

    from coldaisle.evaluate import EvidenceDatabase

    db = tmp_path / "pr91-ro.db"
    _evidence_db(db)
    with EvidenceDatabase(db) as store, pytest.raises(sqlite3.OperationalError):
        store._conn.execute(
            "INSERT INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, ?)",
            ("air.room", TICK_TS_MS, 24.0, "ok"),
        )


def test_the_required_schema_version_is_read_from_the_migrations(tmp_path: Path) -> None:
    """**必要な版を数字で写さない。** migration の名前から引く。"""
    from coldaisle.evaluate import EVIDENCE_MIGRATION, required_schema_version
    from coldaisle.store import migrations

    named = [
        migration
        for migration in migrations.discover()
        if migration.path.stem.endswith(EVIDENCE_MIGRATION)
    ]
    assert len(named) == 1
    assert required_schema_version() == named[0].version
    del tmp_path


# ---- 不変条件 16: 添え file を作らない / 記録された policy を潰さない（第5回レビュー）


def test_invariant_16_a_opening_the_evidence_db_creates_no_sidecar(tmp_path: Path) -> None:
    """**WAL の DB を `mode=ro` で開くと `-shm` / `-wal` が作られる。**

    「何も変えない」が守れず、読み取り専用の媒体では開けもしない。`immutable=1` は
    添え file を作らない。
    """
    import sqlite3

    from coldaisle.evaluate import EvidenceDatabase

    db = tmp_path / "pr91-wal.db"
    _evidence_db(db)
    # 書き手と同じように WAL にしてから、きちんと閉じる（＝静止した DB）。
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()
    before = sorted(item.name for item in tmp_path.iterdir())

    with EvidenceDatabase(db) as store, store.snapshot():
        assert store.control_traces(0, TICK_TS_MS) == ()

    after = sorted(item.name for item in tmp_path.iterdir())
    assert after == before
    assert not (tmp_path / "pr91-wal.db-wal").exists()
    assert not (tmp_path / "pr91-wal.db-shm").exists()


def test_invariant_16_b_a_database_that_is_not_quiescent_is_refused(tmp_path: Path) -> None:
    """**未 checkpoint の WAL を黙って無視して古い断面を読まない。**

    `immutable=1` は WAL を読まないので、書きかけの DB を開くと table が丸ごと
    見えないことさえある。証拠を読み違えるくらいなら読まない。
    """
    from coldaisle.evaluate import EvidenceDatabase, EvidenceDatabaseError

    db = tmp_path / "pr91-live.db"
    _evidence_db(db)
    (tmp_path / "pr91-live.db-wal").write_bytes(b"\x00" * 32)

    with pytest.raises(EvidenceDatabaseError, match="静止していない"):
        EvidenceDatabase(db)


def test_invariant_16_c_a_file_that_is_not_sqlite_is_refused(tmp_path: Path) -> None:
    """SQLite でない file を証拠として開かない。"""
    from coldaisle.evaluate import EvidenceDatabase, EvidenceDatabaseError

    fake = tmp_path / "pr91-fake.db"
    fake.write_text("not a database", encoding="utf-8")
    with pytest.raises(EvidenceDatabaseError, match="SQLite の file ではない"):
        EvidenceDatabase(fake)


def _legacy_tick(ts_ms: int, tick_id: int, policy: str) -> ControlTick:
    """`SupervisorDecision` を持てない v2 の trace（policy は自由文字列）。"""
    return ControlTick(
        schema_version=2,
        tick_id=tick_id,
        ts_ms=ts_ms,
        state=ControlState(
            operating_mode=OperatingMode.AUTO,
            authority_stage=AuthorityStage.SHADOW,
            active_controller=ControllerKind.FALLBACK,
            safety_state=SafetyState.NORMAL,
            fallback_active=True,
            supervisor_policy=policy,
            workload_regime=WorkloadRegime.IDLE,
            regime_confidence=0.5,
        ),
        zones=zone_records(requested=0.4, effective=0.4),
    )


def test_invariant_16_d_legacy_supervisor_policies_stay_separate_arms(
    context: EvaluationContext,
) -> None:
    """**違う policy で回した古い区間を、1つの arm に潰さない**（決定記録 0054 §2.1）。

    v1 / v2 の trace は `SupervisorDecision` を持てず、policy は実装固有の自由文字列
    だった。そこを見ずに `None` にすると、別々の運転が同じ行に混ざる。
    """
    traces = [
        trace_of(_legacy_tick(TICK_TS_MS, 0, "legacy_rule_v1")),
        trace_of(_legacy_tick(TICK_TS_MS + STEP_MS, 1, "legacy_rule_v2")),
    ]
    report = evaluate([run_of(traces, [])], context=context)
    keys = {item.arm_key for item in overall(report).applied}

    assert len(keys) == 2
    assert any("legacy_rule_v1" in key for key in keys)
    assert any("legacy_rule_v2" in key for key in keys)


def test_invariant_16_e_a_policy_name_that_cannot_be_keyed_is_refused(
    context: EvaluationContext,
) -> None:
    """鍵に入れられない policy 名を、黙って `none` に潰さない。"""
    traces = [trace_of(_legacy_tick(TICK_TS_MS, 0, "bad policy@name"))]
    with pytest.raises(EvaluationInputError, match="arm の鍵にできない"):
        evaluate([run_of(traces, [])], context=context)


def test_the_policy_pattern_matches_the_report_contract() -> None:
    """**写した形が、報告の契約と食い違わない。**"""
    from coldaisle.control.evaluation.evaluator import POLICY_NAME_PATTERN

    assert POLICY_NAME_PATTERN.fullmatch("a" * 64) is not None
    assert POLICY_NAME_PATTERN.fullmatch("a" * 65) is None
    assert POLICY_NAME_PATTERN.fullmatch("_leading") is None
    # 現行の列挙値はそのまま通る（v3 以降の鍵は今までと同じ）。
    for policy in SupervisorPolicyKind:
        assert POLICY_NAME_PATTERN.fullmatch(policy.value) is not None
        arm = AppliedArm(
            controller=ControllerKind.FALLBACK,
            supervisor_policy=policy,
            authority_stage=AuthorityStage.SHADOW,
            operating_mode=OperatingMode.AUTO,
        )
        assert arm.key.endswith("@shadow/auto")
        assert f"+{policy.value}@" in arm.key


# ------------------- 不変条件 17: 適用側の arm を artifact へ束縛する（#159 / 0059）


def _applied_learned_state() -> ControlState:
    """Learned MPC が実 Fan の requested を作っていた tick の状態（LIMITED 以降）。"""
    return ControlState(
        operating_mode=OperatingMode.AUTO,
        authority_stage=AuthorityStage.LIMITED,
        active_controller=ControllerKind.LEARNED_MPC,
        safety_state=SafetyState.NORMAL,
        fallback_active=False,
        workload_regime=WorkloadRegime.SUSTAINED_GPU,
        regime_confidence=0.9,
        model_version="thermal-v1",
        model_confidence=0.9,
        model_ood=False,
    )


def _applied_gate(*, artifact: str | None = "a" * 64) -> ModelGateDecision:
    return ModelGateDecision(
        model_version="thermal-v1",
        inference_id="c" * 64,
        artifact_sha256=artifact,
        attested=True,
        confidence=0.9,
        ood=False,
        confidence_level=ConfidenceLevel.HIGH,
        authority_stage=AuthorityStage.LIMITED,
        learned_selected=True,
        limits=(AuthorityLimitSource.STAGE_BAND,),
        assessment=tuple(Reason(code=component) for component in MODEL_GATE_ASSESSMENT_COMPONENTS),
    )


def _applied_learned_run(*, artifacts: tuple[str | None, ...]) -> list[ControlTraceRecord]:
    """Learned MPC が適用された tick の列。`None` は artifact の欄を持たない v6 の trace。"""
    traces: list[ControlTraceRecord] = []
    for index, artifact in enumerate(artifacts):
        tick = tick_at(
            TICK_TS_MS + index * STEP_MS,
            index,
            shadow=False,
            state=_applied_learned_state(),
            model_gate=_applied_gate(artifact="a" * 64),
        )
        if artifact is None:
            # 保存済みの v6（artifact の欄が無い）。読めなければならない（決定記録 0030）。
            document = tick.model_dump(mode="python")
            document["schema_version"] = 6
            document["model_gate"]["artifact_sha256"] = None
            tick = ControlTick.model_validate(document)
        elif artifact != "a" * 64:
            document = tick.model_dump(mode="python")
            document["model_gate"]["artifact_sha256"] = artifact
            tick = ControlTick.model_validate(document)
        traces.append(trace_of(tick))
    return traces


def test_invariant_17_a_an_applied_learned_arm_is_bound_to_its_artifact(
    context: EvaluationContext,
) -> None:
    """**適用した tick の artifact が、適用側の arm の実績として報告に残る**（#159）。"""
    report = evaluate(
        [run_of(_applied_learned_run(artifacts=("a" * 64,) * 3), [])], context=context
    )
    applied = overall(report).applied[0]

    assert applied.arm.controller is ControllerKind.LEARNED_MPC
    assert applied.model_artifacts == ("a" * 64,)
    assert applied.unbound_attested_ticks == 0
    assert applied.last_attested_ts_ms is not None
    assert "applied_artifact_unknown" not in {gap.code for gap in applied.gaps}


def test_invariant_17_b_a_tick_without_the_field_is_counted_as_artifact_unknown(
    context: EvaluationContext,
) -> None:
    """**「artifact 不明」を黙って落とさない**（部分的な束縛を完全として扱わない）。

    落とすと、残った tick の artifact が区間全体の実績に見え、#92 が欠けた区間を
    見ないまま昇格できてしまう。
    """
    report = evaluate(
        [run_of(_applied_learned_run(artifacts=("a" * 64, None, "a" * 64)), [])],
        context=context,
    )
    applied = overall(report).applied[0]

    assert applied.model_artifacts == ("a" * 64,)
    assert applied.unbound_attested_ticks == 1
    assert "applied_artifact_unknown" in {gap.code for gap in applied.gaps}


def test_invariant_17_c_the_provenance_collects_artifacts_from_the_applied_side(
    context: EvaluationContext,
) -> None:
    """**適用側からも artifact を集める**（#159 / 決定記録 0059）。

    counterfactual からしか集めないと、「artifact B の適用実績 + artifact A の
    counterfactual」という報告が `{A}` の照合を素通りする（決定記録 0057 §3）。
    """
    report = evaluate(
        [run_of(_applied_learned_run(artifacts=("b" * 64,) * 3), [])], context=context
    )

    assert report.provenance.versions.model_artifacts == ("b" * 64,)


def test_invariant_17_d_an_arm_cannot_name_an_artifact_the_run_never_saw() -> None:
    """**run に現れていない artifact を arm の実績に書けない。**

    書けると、報告全体の照合（#92）を通る artifact を arm 側にだけ足して、別の
    artifact の実績を昇格の根拠にできる。
    """
    from coldaisle.control.evaluation.model import AppliedArmReport, InterventionReport

    arm = AppliedArm(
        controller=ControllerKind.LEARNED_MPC,
        supervisor_policy=SupervisorPolicyKind.RULE,
        authority_stage=AuthorityStage.LIMITED,
        operating_mode=OperatingMode.AUTO,
    )

    with pytest.raises(ValidationError, match="Learned MPC 以外の適用 arm"):
        AppliedArmReport(
            arm=arm.model_copy(update={"controller": ControllerKind.FALLBACK}),
            arm_key=arm.model_copy(update={"controller": ControllerKind.FALLBACK}).key,
            ticks=1,
            first_ts_ms=TICK_TS_MS,
            last_ts_ms=TICK_TS_MS,
            last_attested_ts_ms=TICK_TS_MS,
            model_artifacts=("a" * 64,),
            interventions=InterventionReport(
                ticks=1, safety_states=(CountedReason(code="normal", count=1),)
            ),
        )

    with pytest.raises(ValidationError, match="裏づけの無い適用 arm"):
        AppliedArmReport(
            arm=arm,
            arm_key=arm.key,
            ticks=1,
            first_ts_ms=TICK_TS_MS,
            last_ts_ms=TICK_TS_MS,
            model_artifacts=("a" * 64,),
            bound_attested_ticks=1,
            interventions=InterventionReport(
                ticks=1, safety_states=(CountedReason(code="normal", count=1),)
            ),
        )

    with pytest.raises(ValidationError, match="束縛できた tick の数と artifact の有無"):
        AppliedArmReport(
            arm=arm,
            arm_key=arm.key,
            ticks=1,
            first_ts_ms=TICK_TS_MS,
            last_ts_ms=TICK_TS_MS,
            last_attested_ts_ms=TICK_TS_MS,
            model_artifacts=("a" * 64,),
            interventions=InterventionReport(
                ticks=1, safety_states=(CountedReason(code="normal", count=1),)
            ),
        )


def test_invariant_17_e_a_learned_tick_without_a_model_gate_counts_as_unknown(
    context: EvaluationContext,
) -> None:
    """**`model_gate` を持たない v1〜v4 の適用 tick も「不明」に数える**（codex #4057527947）。

    数えないと、その区間は artifact を1つも挙げないまま `unbound_attested_ticks == 0` に
    なり、`#92` の「適用 arm すべてに不明が無いこと」を素通りする。
    """
    traces = []
    for index in range(3):
        tick = tick_at(
            TICK_TS_MS + index * STEP_MS,
            index,
            shadow=False,
            state=_applied_learned_state(),
            model_gate=_applied_gate(),
        )
        document = tick.model_dump(mode="python")
        # v4 の trace。Learned MPC を適用しているが `model_gate` を持たない。
        document["schema_version"] = 4
        document["model_gate"] = None
        traces.append(trace_of(ControlTick.model_validate(document)))

    report = evaluate([run_of(traces, [])], context=context)
    applied = overall(report).applied[0]

    assert applied.arm.controller is ControllerKind.LEARNED_MPC
    assert applied.model_artifacts == ()
    assert applied.unbound_attested_ticks == 3, "記録の無さを「不明なし」に落とさない"
    assert applied.last_attested_ts_ms is None
    assert "applied_artifact_unknown" in {gap.code for gap in applied.gaps}


def test_invariant_17_f_an_applied_learned_arm_must_account_for_every_tick(
    context: EvaluationContext,
) -> None:
    """**一部の tick しか束縛できていない arm を「完全」と読ませない**（codex #4057573941）。

    `model_artifacts` は集合なので「どの artifact か」しか言わない。1 tick だけ束縛できた
    100 tick の arm でも `model_artifacts == (production,)` / `unbound == 0` になりうる。
    **すべての tick を勘定する。**
    """
    report = evaluate(
        [run_of(_applied_learned_run(artifacts=("a" * 64,) * 3), [])], context=context
    )
    applied = overall(report).applied[0]
    assert (applied.bound_attested_ticks, applied.unbound_attested_ticks) == (3, 0)
    assert applied.ticks == 3

    document = json.loads(report.model_dump_json())
    # 3 tick のうち1 tick しか勘定していない報告。artifact も「不明」も食い違わないが、
    # **区間の何割を束縛できたのかを言えていない。**
    for group in document["segments"][0]["groups"]:
        for item in group["applied"]:
            item["bound_attested_ticks"] = 1

    with pytest.raises(ValidationError, match="すべての tick の artifact を勘定する"):
        EvaluationReport.model_validate_json(json.dumps(document))


def test_invariant_17_h_a_stored_v1_report_still_loads(context: EvaluationContext) -> None:
    """**保存済みの v1 の報告は、そのまま読める**（codex #4057573943）。

    完全性を型の段で要求すると、v1 が**読めなくなる。** v1 は読めたうえで、
    `#92` が「artifact の完全性を言えない報告」として昇格の証拠から外す
    （決定記録 0059 §2.5）。読めなくするのと、根拠にしないのは別である。
    """
    report = evaluate(
        [run_of(_applied_learned_run(artifacts=("a" * 64,) * 3), [])], context=context
    )
    document = json.loads(report.model_dump_json())
    # v1 の報告の形（この3欄は当時まだ無かった）。
    document["schema_version"] = 1
    for segment in document["segments"]:
        for group in segment["groups"]:
            for item in group["applied"]:
                del item["model_artifacts"]
                del item["bound_attested_ticks"]
                del item["unbound_attested_ticks"]

    stored = EvaluationReport.model_validate_json(json.dumps(document))

    assert stored.schema_version == 1
    applied = overall(stored).applied[0]
    assert applied.arm.controller is ControllerKind.LEARNED_MPC
    assert applied.model_artifacts == ()
    assert (applied.bound_attested_ticks, applied.unbound_attested_ticks) == (0, 0)


def test_invariant_17_g_a_v1_report_cannot_carry_the_fields_added_in_v2(
    context: EvaluationContext,
) -> None:
    """**古い version に、後から意味の違う欄を足して読ませない**（決定記録 0030 と同じ向き）。"""
    report = evaluate(
        [run_of(_applied_learned_run(artifacts=("a" * 64,) * 3), [])], context=context
    )
    document = json.loads(report.model_dump_json())
    assert document["schema_version"] == 2
    document["schema_version"] = 1

    with pytest.raises(ValidationError, match="schema version 2"):
        EvaluationReport.model_validate_json(json.dumps(document))
