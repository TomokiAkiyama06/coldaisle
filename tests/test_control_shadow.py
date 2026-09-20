"""#90 Control Shadow Mode / Counterfactual logging。実機不要（合成 dataset / 試験用 backend）。

**ここでは「守れているか」ではなく「破れないか」を試す。** Shadow の記録が満たしていなければ
ならない不変条件を並べ、1つずつ破ろうとする試験を置く。

1. **Shadow の提案は effective demand に絶対に届かない**（経路も型も記録も）
2. 記録した予測は、**それを出した1回の推論に束ねられている**（`inference_id` / `plan_digest`）
3. 実測との突き合わせは、**記録された時刻からだけ**決まる。処理した時刻を使わない
4. 裏づけの無い confidence / ood を counterfactual に残さない（Gate の判定だけを写す）
5. optimizer の timeout・model の異常・worker の失敗も**記録する**
6. **RulePolicy active + MPC shadow** を同じ trace に残せる
7. #91 Offline Evaluation へ **決定論的に export** できる
8. 記録は設定で止められる（止めても制御は変わらない）
9. 写した構造上の上限が、元の契約と食い違わない（記録側だけが狭くならない）
10. 採点してよいのは、予測した候補 action が**実際に掛かっていた**区間だけ
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.control.config import ShadowConfig
from coldaisle.control.fallback import (
    ControllerSelection,
    LearnedControlStatus,
    LearnedFailure,
)
from coldaisle.control.model.confidence import ConfidenceAssessment, fit_confidence_profile
from coldaisle.control.model.thermal import ArtifactVerification, canonical_artifact_bytes
from coldaisle.control.mpc import MpcProposal
from coldaisle.control.schema import (
    AuthorityStage,
    BoundBy,
    ControllerKind,
    ControllerProposal,
    ControlState,
    ControlTick,
    EffectiveZoneDemand,
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
    SupervisorDecision,
    SupervisorPolicyEvaluation,
    SupervisorPolicyKind,
    WorkloadRegime,
    Zone,
    ZoneRecord,
)
from coldaisle.control.shadow import (
    AppliedActionTimeline,
    ObservationIndex,
    OutcomeObservation,
    OutcomeStatus,
    ShadowExportRow,
    ShadowObservationConflictError,
    ShadowOutcomeMatcher,
    ShadowOutcomeUnusableError,
    ShadowRecorder,
    counterfactual_controllers,
    shadow_plan,
    shadow_rows,
    write_shadow_jsonl,
)
from coldaisle.store.models import ControlTraceRecord, Quality
from test_fallback_controller import (
    assessment_for,
    fallback_proposal,
    gate_for,
    learned_proposal,
    policy,
)
from test_learned_mpc import (
    ACTION_TS_MS,
    HORIZONS,
    TARGETS,
    build_controller,
    demands,
    issue_attestation,
    mpc_policy,
    propose,
    supervisor_output,
)
from test_model_confidence import dataset, profile_spec, split, train
from test_simulated_fan_backend import runtime

GPU = "gpu.0.core"
TICK_TS_MS = 1_787_616_000_000


def _shadow_config(*, enabled: bool) -> ShadowConfig:
    return ShadowConfig.model_validate(
        {
            "enabled": enabled,
            "outcome_match_tolerance_ms": {"value": 500, "status": "provisional"},
            "applied_demand_tolerance": {"value": 0.01, "status": "provisional"},
        }
    )


SHADOW_CONFIG = _shadow_config(enabled=True)
DISABLED_SHADOW_CONFIG = _shadow_config(enabled=False)


@pytest.fixture(scope="module")
def trained_model(tmp_path_factory: pytest.TempPathFactory):
    """合成 dataset で学習した #84 モデル・Profile・Registry の証拠（#86 と同じ組み立て）。"""
    data = dataset(HORIZONS, TARGETS)
    parts = split(data)
    model = train(data, parts)
    profile = fit_confidence_profile(model, data, parts, profile_spec())
    attestation = issue_attestation(
        tmp_path_factory.mktemp("pr90-registry") / "registry",
        model_id=model.manifest.model_id,
        version=model.manifest.model_version,
        payload=canonical_artifact_bytes(model._artifact),
    )
    return model, profile, attestation


# ---------------------------------------------------------------- 組み立ての補助


def gate_selection(
    *,
    stage: str = "shadow",
    learned: LearnedControlStatus | None = None,
    baseline: float = 0.4,
    mode: OperatingMode = OperatingMode.AUTO,
):
    """設定した authority stage で選ばせる。復帰 hold を満たすため健全なまま2 tick 進める。"""
    settings = policy(authority=stage, recovery_hold_ms=1)
    gate = gate_for(settings, expected_model_version="thermal-v1")
    selection = None
    for now_mono_ms in (0, settings.recovery_hold_ms):
        selection = gate.select(
            now_mono_ms=now_mono_ms,
            fallback=fallback_proposal(baseline),
            learned=learned or LearnedControlStatus(),
            operating_mode=mode,
            safety_state=SafetyState.NORMAL,
        )
    assert selection is not None
    return selection


def learned_status(proposal: ControllerProposal, *, attested: bool = True) -> LearnedControlStatus:
    return LearnedControlStatus(
        proposal=proposal,
        received_at_mono_ms=0,
        assessment=assessment_for(proposal) if attested else None,
        binding_authority_stage=AuthorityStage.FULL,
    )


def shadow_proposal(demand: float = 0.9, **overrides) -> ControllerProposal:
    """解を持たない軽量な Learned 提案。

    ``MpcProposal`` は ``optimizer_status=ok`` の提案に解を要求する（#86）ので、合成の提案は
    必ず timeout にする。解と予測を伴う経路は実物の optimizer で試す（不変条件 2-a）。
    """
    overrides.setdefault("optimizer", OptimizerStatus.TIMEOUT)
    return learned_proposal(demand, **overrides)


def mpc_result(
    proposal: ControllerProposal | None = None,
    *,
    failure: LearnedFailure | None = None,
    assessment: ConfidenceAssessment | None = None,
) -> MpcProposal:
    """worker の結果（解を持たない軽量版）。timeout / 失敗の記録に使う。"""
    if failure is not None:
        return MpcProposal(
            failure=failure,
            failure_reason=Reason(code="model_unusable", detail="capability mismatch"),
        )
    assert proposal is not None
    return MpcProposal(
        proposal=proposal,
        assessment=assessment or assessment_for(proposal),
        binding_authority_stage=AuthorityStage.FULL,
    )


def worker_status(result: MpcProposal) -> LearnedControlStatus:
    """worker 結果から Gate へ渡す状態（**識別子ごと**運ばれる実際の経路）。"""
    return result.to_status(received_at_mono_ms=0)


def control_state(selection, *, stage: AuthorityStage = AuthorityStage.SHADOW) -> ControlState:
    gate = selection.model_gate
    return ControlState(
        operating_mode=OperatingMode.AUTO,
        authority_stage=stage,
        active_controller=selection.active_controller,
        safety_state=SafetyState.NORMAL,
        fallback_active=selection.fallback_active,
        fallback_reason=selection.fallback_reason,
        model_version=None if gate is None else gate.model_version,
        model_confidence=None if gate is None else gate.confidence,
        model_ood=None if gate is None else gate.ood,
    )


def zone_records(demand: float) -> PerZone[ZoneRecord]:
    record = ZoneRecord(
        controller_reason=Reason(code="fallback_curve"),
        demand=EffectiveZoneDemand(
            requested=demand,
            effective=demand,
            bound_by=BoundBy.REQUESTED,
            safety_floor=0.0,
            forced_max=False,
        ),
    )
    return PerZone[ZoneRecord](front=record, rear=record, top=record)


def tick_with(
    shadow: ShadowRecord | None,
    *,
    state: ControlState,
    selection=None,
    effective: float = 0.4,
    tick_id: int = 7,
    ts_ms: int = TICK_TS_MS,
    supervisor: SupervisorDecision | None = None,
) -> ControlTick:
    return ControlTick(
        tick_id=tick_id,
        ts_ms=ts_ms,
        state=state,
        zones=zone_records(effective),
        model_gate=None if selection is None else selection.model_gate,
        shadow=shadow,
        supervisor=supervisor,
    )


def recorded(
    *,
    stage: str = "shadow",
    learned: MpcProposal | None = None,
    baseline: float = 0.4,
    effective: float = 0.4,
    config: ShadowConfig = SHADOW_CONFIG,
    supervisor=None,
    ts_ms: int = TICK_TS_MS,
):
    """1 tick の Gate 選択と ShadowRecord を一緒に返す。"""
    status = LearnedControlStatus()
    if learned is not None and learned.proposal is not None:
        status = worker_status(learned)
    selection = gate_selection(stage=stage, learned=status, baseline=baseline)
    state = control_state(selection, stage=AuthorityStage(stage))
    record = ShadowRecorder(config).record(
        tick_id=7,
        ts_ms=ts_ms,
        state=state,
        effective=demands(effective),
        selection=selection,
        baseline=fallback_proposal(baseline),
        learned=learned,
        supervisor=supervisor,
    )
    return selection, state, record


def plan_for(demand: float = 0.8, *, offsets: tuple[int, ...] = (1_000, 2_000)) -> ShadowActionPlan:
    """記録した候補 action 列（v1 は horizon 全体で同じ demand を保つ）。"""
    return ShadowActionPlan(
        step_ms=offsets[0],
        steps=tuple(
            ShadowPlanStep(offset_ms=offset, demands=demands(demand)) for offset in offsets
        ),
    )


def prediction(
    *,
    action_ts_ms: int = 100_000,
    offsets: tuple[int, ...] = (1_000, 2_000),
    values: tuple[float, ...] = (50.0, 51.0),
    inference: str = "c" * 64,
    plan: ShadowActionPlan | None = None,
    metric: str = GPU,
) -> ShadowPrediction:
    """候補 plan に対応する予測。plan を渡さなければ同じ offset の held plan を使う。"""
    plan = plan if plan is not None else plan_for(offsets=offsets)
    return ShadowPrediction(
        model_id="rack-thermal",
        model_version="thermal-v1",
        artifact_sha256="a" * 64,
        inference_id=inference,
        plan_digest=plan.digest(),
        input_action_ts_ms=action_ts_ms,
        targets=tuple(
            ShadowPredictedTarget(
                offset_ms=offset,
                expected_ts_ms=action_ts_ms + offset,
                values={metric: value},
            )
            for offset, value in zip(offsets, values, strict=True)
        ),
    )


def solved_counterfactual(**overrides) -> dict[str, object]:
    """解を持つ Learned MPC の counterfactual（不変条件を1つずつ壊すための素体）。"""
    plan = plan_for()
    payload: dict[str, object] = {
        "controller": ControllerKind.LEARNED_MPC,
        "requested": plan.first,
        "reason": Reason(code="optimizer_ok"),
        "optimizer_status": OptimizerStatus.OK,
        "model_version": "thermal-v1",
        "inference_id": "c" * 64,
        "artifact_sha256": "a" * 64,
        "plan": plan,
        "prediction": prediction(plan=plan),
        "cost_total": 1.0,
        "baseline_cost_total": 2.0,
    }
    return payload | overrides


def observation(ts_ms: int, value: float, *, quality: Quality = Quality.OK) -> OutcomeObservation:
    return OutcomeObservation(metric=GPU, ts_ms=ts_ms, value=value, quality=quality)


def outcome_matcher(
    *, match_tolerance_ms: int = 500, applied_demand_tolerance: float = 0.01
) -> ShadowOutcomeMatcher:
    return ShadowOutcomeMatcher(
        match_tolerance_ms=match_tolerance_ms,
        applied_demand_tolerance=applied_demand_tolerance,
    )


def applied_for(
    plan: ShadowActionPlan, *, action_ts_ms: int = 100_000, demand: float | None = None
) -> AppliedActionTimeline:
    """plan の各 step 区間に1件ずつ、適用 demand の記録がある列。

    ``demand`` を省くと **plan どおりに実行された**列になる。値を渡すと、その値が
    掛かっていた（= 採点できない）列になる。
    """
    applied = plan.first if demand is None else demands(demand)
    return AppliedActionTimeline(
        (action_ts_ms + offset_ms - plan.step_ms, applied) for offset_ms in plan.offsets_ms
    )


def match_outcome(
    matcher: ShadowOutcomeMatcher,
    observations,
    *,
    offsets: tuple[int, ...] = (1_000, 2_000),
    values: tuple[float, ...] = (50.0, 51.0),
    action_ts_ms: int = 100_000,
    metric: str = GPU,
    applied: AppliedActionTimeline | None = None,
):
    """plan どおりに実行された前提で1件照合する（時刻の規則を試すための入口）。"""
    plan = plan_for(offsets=offsets)
    predicted = prediction(
        plan=plan, offsets=offsets, values=values, action_ts_ms=action_ts_ms, metric=metric
    )
    return matcher.match(
        predicted,
        observations,
        plan=plan,
        applied=applied if applied is not None else applied_for(plan, action_ts_ms=action_ts_ms),
    )


# ------------------------------------- 不変条件 1: Shadow は effective demand に届かない


FORBIDDEN_MODULES = (
    "coldaisle.control.hardware",
    "coldaisle.control.safety",
    "coldaisle.control.reactive",
    "serial",
    "subprocess",
)
"""`control/shadow` が import してはいけない module（AGENTS.md ルール1 / 2 / 6）。"""

SHADOW_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control" / "shadow"


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def test_invariant_1_a_the_shadow_package_cannot_reach_the_actuation_path() -> None:
    """**Guard / Safety / Hardware を迂回する経路を作らない。**

    記録の層から書き込み層へ触れられるようになった瞬間、「counterfactual は届かない」が
    設計上の約束ではなくなる。
    """
    offenders = [
        f"{path.name}: {name}"
        for path in sorted(SHADOW_PACKAGE.glob("*.py"))
        for name in _imported_modules(path)
        if any(
            name == forbidden or name.startswith(f"{forbidden}.") for forbidden in FORBIDDEN_MODULES
        )
    ]
    assert offenders == []


def test_invariant_1_b_the_shadow_package_never_names_pwm_or_effective_demand() -> None:
    """Shadow の型に **PWM も `EffectiveZoneDemand` も現れない**（決定記録 0028 §2.3）。"""
    offenders = []
    for path in sorted(SHADOW_PACKAGE.glob("*.py")):
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


@pytest.mark.parametrize("shadow_demand", [0.0, 1.0])
def test_invariant_1_c_the_shadow_proposal_never_becomes_the_request(shadow_demand) -> None:
    """SHADOW stage では、Learned の要求が**何であれ** requested は Fallback のまま。"""
    proposal = learned_proposal(shadow_demand)
    selection = gate_selection(learned=learned_status(proposal))

    assert selection.active_controller is ControllerKind.FALLBACK
    for zone in Zone:
        assert selection.proposal.requested.get(zone).demand == 0.4
    # 記録は残る。**選ばれていないこと**が記録に現れる。
    assert selection.model_gate is not None
    assert selection.model_gate.learned_selected is False


def _pwm_for(shadow_demand: float | None) -> tuple[int, ...]:
    """SHADOW stage の requested を simulated backend まで通し、書いた PWM を返す。"""
    learned = (
        LearnedControlStatus()
        if shadow_demand is None
        else learned_status(learned_proposal(shadow_demand))
    )
    selection = gate_selection(learned=learned)
    requested = selection.proposal.requested
    backend, (command,) = runtime(
        requested.front.demand, requested.rear.demand, requested.top.demand
    )
    results = backend.apply(command)
    return tuple(results.get(zone).readback.pwm_raw for zone in Zone)


def test_invariant_1_d_the_written_pwm_does_not_move_with_the_shadow_output() -> None:
    """**受入基準**: Shadow 有効時に Fan PWM が Shadow 出力で変化しない。

    Gate → Critical Safety の合成 → simulated Hardware Backend まで通し、書き込んだ raw PWM を
    比べる。Shadow の要求を 0.0 と 1.0 にしても、提案が1つも無い tick と同じ値になる。
    """
    quiet = _pwm_for(0.0)
    loud = _pwm_for(1.0)
    without_any_proposal = _pwm_for(None)

    assert quiet == loud == without_any_proposal


def test_invariant_1_e_the_applied_controller_cannot_be_recorded_as_a_counterfactual() -> None:
    """適用した制御器を counterfactual に**できない**。読み手が適用値と取り違える。"""
    with pytest.raises(ValidationError, match="counterfactual として記録しない"):
        ShadowRecord(
            tick_id=7,
            ts_ms=TICK_TS_MS,
            authority_stage=AuthorityStage.SHADOW,
            applied_controller=ControllerKind.FALLBACK,
            applied_effective=demands(0.4),
            counterfactuals=(
                ShadowCounterfactual(
                    controller=ControllerKind.FALLBACK,
                    requested=demands(0.4),
                    reason=Reason(code="fallback_curve"),
                ),
            ),
        )


def test_invariant_1_f_the_applied_effective_must_match_the_zones() -> None:
    """`applied_effective` を実際の effective と**食い違わせられない**（比較の基準が壊れる）。"""
    proposal = shadow_proposal(0.9)
    selection, state, record = recorded(learned=mpc_result(proposal))
    assert record is not None

    tick_with(record, state=state, selection=selection, effective=0.4)
    forged = record.model_copy(update={"applied_effective": demands(0.9)})
    with pytest.raises(ValidationError, match="applied_effective"):
        tick_with(forged, state=state, selection=selection, effective=0.4)


def test_invariant_1_g_a_shadow_record_is_refused_in_modes_people_drive() -> None:
    """MANUAL / CALIBRATION には比較すべき「適用した制御器」が無い。"""
    proposal = shadow_proposal(0.9)
    selection, _state, record = recorded(learned=mpc_result(proposal))
    assert record is not None
    manual = ControlState(
        operating_mode=OperatingMode.MANUAL,
        authority_stage=AuthorityStage.SHADOW,
        active_controller=None,
        safety_state=SafetyState.NORMAL,
        fallback_active=False,
    )
    with pytest.raises(ValidationError, match="MANUAL / CALIBRATION"):
        ControlTick(
            tick_id=7,
            ts_ms=TICK_TS_MS,
            state=manual,
            zones=zone_records(0.4),
            shadow=record.model_copy(update={"applied_controller": None}),
        )
    # 記録器自身も、その mode では何も作らない。
    assert (
        ShadowRecorder(SHADOW_CONFIG).record(
            tick_id=7,
            ts_ms=TICK_TS_MS,
            state=manual,
            effective=demands(0.4),
            selection=selection,
            baseline=fallback_proposal(0.4),
            learned=mpc_result(proposal),
        )
        is None
    )


def test_invariant_1_h_a_v5_trace_cannot_carry_a_shadow_record() -> None:
    """保存済みの版に、後から意味の違う欄を足して読ませない。"""
    proposal = shadow_proposal(0.9)
    selection, state, record = recorded(learned=mpc_result(proposal))
    assert record is not None
    assert selection.model_gate is not None
    # artifact は v7 の欄なので、v5 の tick には最初から載せられない（#159）。
    # ここで見たいのは `shadow` のほうなので、v5 に載る形の model_gate で試す。
    gate = ModelGateDecision.model_validate(
        {**selection.model_gate.model_dump(mode="python"), "artifact_sha256": None}
    )
    with pytest.raises(ValidationError, match="schema version 6"):
        ControlTick(
            schema_version=5,
            tick_id=7,
            ts_ms=TICK_TS_MS,
            state=state,
            zones=zone_records(0.4),
            model_gate=gate,
            shadow=record,
        )


# ------------------------------------- 不変条件 2: 予測はその推論に束ねられている


def test_invariant_2_a_the_prediction_comes_from_the_recorded_inference(trained_model) -> None:
    """実機の経路で作った提案の記録が、**推論・artifact・候補 plan の識別子で束ねられている。**"""
    controller, _model, settings = build_controller(
        trained_model, policy_config=mpc_policy(authority="shadow"), acoustic=True
    )
    result = propose(controller, baseline=0.3)
    assert result.proposal is not None and result.solution is not None

    _selection, _state, record = recorded(learned=result)
    assert record is not None
    (counterfactual,) = record.counterfactuals
    assert counterfactual.controller is ControllerKind.LEARNED_MPC
    assert counterfactual.prediction is not None
    assert counterfactual.inference_id == result.proposal.inference_id
    assert counterfactual.prediction.inference_id == result.solution.anchor_inference_id
    assert counterfactual.prediction.plan_digest == result.solution.plan.digest()
    assert counterfactual.cost_total == result.solution.cost.total
    # 予測時刻は記録した時刻ではなく、推論の元になった action 時刻から決まる。
    assert counterfactual.prediction.input_action_ts_ms == ACTION_TS_MS
    assert counterfactual.prediction.targets[0].expected_ts_ms == (
        ACTION_TS_MS + settings.mpc.optimizer.step_ms.value
    )


def test_invariant_2_b_a_prediction_from_another_inference_cannot_be_attached() -> None:
    """別の推論の予測を counterfactual へ**貼り替えられない**。"""
    plan = plan_for()
    with pytest.raises(ValidationError, match="別の推論"):
        ShadowCounterfactual(
            **solved_counterfactual(prediction=prediction(plan=plan, inference="e" * 64))
        )


def test_invariant_2_e_a_prediction_for_another_candidate_plan_cannot_be_attached() -> None:
    """**候補 plan が違えば貼れない。** 版・推論・artifact が合っていても閉じる。

    同じ tick の候補は step の刻みが同じなので、識別子まで照らさないと plan A の要求に
    plan B の予測を貼れる（決定記録 0052 §2.2 / 0053 §2.2）。
    """
    other = plan_for(0.2)
    with pytest.raises(ValidationError, match="別の候補 plan"):
        # 予測だけを別の候補のものに差し替える（requested と plan はそのまま）。
        ShadowCounterfactual(**solved_counterfactual(prediction=prediction(plan=other)))

    with pytest.raises(ValidationError, match="最初の step と違う"):
        # plan を差し替えて digest を合わせても、要求した demand と食い違う。
        ShadowCounterfactual(**solved_counterfactual(plan=other, prediction=prediction(plan=other)))

    longer = plan_for(offsets=(1_000, 2_000, 3_000))
    with pytest.raises(ValidationError, match="候補 plan と予測の step 列"):
        # digest も要求も合うが、予測が覆う step が plan より短い。
        ShadowCounterfactual(
            **solved_counterfactual(
                plan=longer, prediction=prediction(plan=longer, offsets=(1_000, 2_000))
            )
        )


def test_invariant_2_f_the_recorded_plan_digest_matches_the_optimizer_s() -> None:
    """記録した候補 plan の digest は、**optimizer の `ActionPlan` と同じ値**になる。

    ここがずれると、trace 側で数え直した digest が予測と一致せず、正しい記録まで閉じる。
    """
    from coldaisle.control.mpc import ActionPlan

    original = ActionPlan.held(demands(0.8), step_ms=1_000, steps=2)

    assert shadow_plan(original).digest() == original.digest()
    assert plan_for(0.8, offsets=(1_000, 2_000)).digest() == original.digest()


def test_invariant_2_c_the_counterfactual_and_the_gate_must_share_one_inference() -> None:
    """trace の中で、counterfactual と Gate の判定が**別の推論を指していない**。"""
    proposal = shadow_proposal(0.9)
    selection, state, record = recorded(learned=mpc_result(proposal))
    assert record is not None
    (counterfactual,) = record.counterfactuals
    forged = record.model_copy(
        update={"counterfactuals": (counterfactual.model_copy(update={"inference_id": "f" * 64}),)}
    )
    with pytest.raises(ValidationError, match="別の推論"):
        tick_with(forged, state=state, selection=selection)


def test_invariant_2_d_a_solution_cost_cannot_beat_its_own_baseline_backwards() -> None:
    """Baseline より悪い解を「採用した解」として記録できない（#86 の不変条件を写す）。"""
    with pytest.raises(ValidationError, match="Baseline を上回っている"):
        ShadowCounterfactual(**solved_counterfactual(cost_total=3.0, baseline_cost_total=2.0))


# ------------------------------------- 不変条件 3: 照合は記録された時刻からだけ決まる


def test_invariant_3_a_the_outcome_matcher_has_no_clock() -> None:
    """**処理した時刻を使わない。** 時計を持ち込めば、あとから当たりに見せられる。"""
    source = (SHADOW_PACKAGE / "outcome.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set(_imported_modules(SHADOW_PACKAGE / "outcome.py"))
    assert not (imported & {"time", "datetime", "coldaisle.clock"})
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert not ({"monotonic", "now_ms", "now", "monotonic_ms"} & names)


def test_invariant_3_b_only_observations_inside_the_tolerance_are_evidence() -> None:
    """許容幅の外の実測を「近いから」で採らない。**理由を残して未照合にする。**"""
    outcome = match_outcome(
        outcome_matcher(), [observation(101_000, 50.5), observation(102_600, 99.0)]
    )

    first, second = outcome.matches
    assert first.observed == 50.5 and first.error == pytest.approx(0.5)
    assert second.observed is None and second.unmatched is not None
    assert second.unmatched.code == "no_usable_observation"
    assert outcome.complete is False


def test_invariant_3_c_the_earlier_observation_wins_a_tie() -> None:
    """同距離なら**過去側**を採る（決定記録 0031 §2.2 と同じ規則）。"""
    outcome = match_outcome(
        outcome_matcher(),
        [observation(100_700, 40.0), observation(101_300, 60.0)],
        offsets=(1_000,),
        values=(50.0,),
    )

    assert outcome.matches[0].observed == 40.0
    assert outcome.matches[0].observed_ts_ms == 100_700


def test_invariant_3_d_observations_before_the_action_are_not_evidence() -> None:
    """action より前の観測は、その action の効果を含まない。"""
    outcome = match_outcome(
        outcome_matcher(match_tolerance_ms=900),
        [observation(100_000, 50.0)],
        offsets=(1_000,),
        values=(50.0,),
    )

    assert outcome.matches[0].observed is None


@pytest.mark.parametrize("quality", [Quality.STALE, Quality.SUSPECT, Quality.MISSING])
def test_invariant_3_e_only_ok_observations_are_evidence(quality) -> None:
    """stale / suspect / missing の値を「予測が当たった証拠」に数えない。"""
    outcome = match_outcome(
        outcome_matcher(),
        [observation(101_000, 50.0, quality=quality)],
        offsets=(1_000,),
        values=(50.0,),
    )

    assert outcome.matches[0].observed is None


def test_invariant_3_f_the_result_does_not_depend_on_the_arrival_order() -> None:
    """観測の並び順を変えても同じ結果になる（再現できる比較のため）。"""
    matcher = outcome_matcher()
    observations = [observation(101_000, 50.5), observation(102_000, 51.5)]

    forward = match_outcome(matcher, observations)
    backward = match_outcome(matcher, list(reversed(observations)))

    assert forward == backward
    assert forward.complete is True


def test_invariant_3_g_a_tolerance_that_reaches_another_step_is_refused() -> None:
    """許容幅が1 step に届くと、別の step の実測を証拠にしてしまう。**設定で止める。**"""
    with pytest.raises(ShadowOutcomeUnusableError, match="許容幅"):
        match_outcome(outcome_matcher(match_tolerance_ms=1_000), [observation(101_000, 50.0)])

    with pytest.raises(ValidationError, match="outcome_match_tolerance_ms"):
        policy(
            mpc={
                "period_ms": 1_000,
                "budget_ms": 100,
                "valid_ms": 2_000,
                "optimizer": _optimizer_with_step(500),
            }
        )


def _optimizer_with_step(step_ms: int) -> dict[str, object]:
    from test_control_config import mpc_optimizer_config

    document = mpc_optimizer_config()
    document["step_ms"] = {"value": step_ms, "status": "provisional"}
    document["horizon_ms"] = {"value": step_ms * 2, "status": "provisional"}
    return document


def _naive_matches(
    prediction_: ShadowPrediction,
    observations: list[OutcomeObservation],
    *,
    tolerance_ms: int,
) -> list[tuple[int, str, int | None, float | None]]:
    """索引を使わない素朴な突き合わせ（最適化の前後で結果が同じことを確かめる基準）。"""
    results: list[tuple[int, str, int | None, float | None]] = []
    for target in prediction_.targets:
        for metric, _value in sorted(target.values.items()):
            best: tuple[int, int, float] | None = None
            for item in sorted(observations, key=lambda one: (one.ts_ms, one.metric)):
                value = item.usable
                if item.metric != metric or value is None:
                    continue
                if item.ts_ms <= prediction_.input_action_ts_ms:
                    continue
                distance = abs(item.ts_ms - target.expected_ts_ms)
                if distance > tolerance_ms:
                    continue
                if best is None or distance < best[0]:
                    best = (distance, item.ts_ms, value)
            found = (None, None) if best is None else (best[1], best[2])
            results.append((target.offset_ms, metric, found[0], found[1]))
    return results


def test_invariant_3_h_the_indexed_match_equals_the_naive_one() -> None:
    """**索引を使っても結果が変わらない。** 走査の順序ではなく規則で決まっていること。

    期待時刻の周りだけを二分探索で切り出す実装へ替えたので、履歴が長く metric が混ざった
    入力で、素朴な全走査と同じ照合になることを確かめる。
    """
    metrics = (GPU, "cpu.package")
    observations = [
        OutcomeObservation(
            metric=metric,
            ts_ms=ts_ms,
            value=float(ts_ms % 97) / 3.0,
            quality=Quality.OK if ts_ms % 7 else Quality.STALE,
        )
        for metric in metrics
        for ts_ms in range(99_000, 106_000, 137)
    ]
    # 期待時刻ちょうど、action より前、許容幅の外、**同じ値の重複**なども混ぜる。
    observations.append(
        OutcomeObservation(metric=GPU, ts_ms=101_000, value=1.0, quality=Quality.OK)
    )
    observations.append(
        # 生成済みの 99_000 と**まったく同じ観測**（食い違う重複は受け取らない。0055 §2.1）。
        OutcomeObservation(
            metric=GPU, ts_ms=99_000, value=float(99_000 % 97) / 3.0, quality=Quality.OK
        )
    )
    matcher = outcome_matcher(match_tolerance_ms=400)
    index = ObservationIndex(observations)

    for metric in metrics:
        predicted = prediction(metric=metric, offsets=(1_000, 2_000), values=(50.0, 51.0))
        expected = _naive_matches(predicted, observations, tolerance_ms=400)

        from_sequence = match_outcome(matcher, observations, metric=metric, values=(50.0, 51.0))
        from_index = match_outcome(matcher, index, metric=metric, values=(50.0, 51.0))

        assert from_sequence == from_index
        assert [
            (item.offset_ms, item.metric, item.observed_ts_ms, item.observed)
            for item in from_index.matches
        ] == expected


def test_invariant_3_i_contradictory_duplicate_observations_are_refused() -> None:
    """**同じ metric・同じ時刻の食い違う観測を、照合が選ばない**（決定記録 0055 §2.1）。

    誤差は `実測 - 予測` なので、小さいほうを採ると **underprediction（冷却が足りない向きの
    外し方）が実際より小さく見える**。大きいほうを採れば予測の当たりが消える。どちらを
    選んでも片方の事実が消えるので、入力の誤りとして受け取らない。
    """
    cooler = observation(101_000, 50.5)
    hotter = observation(101_000, 80.5)

    with pytest.raises(ShadowObservationConflictError, match="食い違う観測"):
        ObservationIndex([cooler, hotter])
    # 順序を入れ替えても同じ。**「先に来たほうが勝つ」規則も置かない。**
    with pytest.raises(ShadowObservationConflictError, match="食い違う観測"):
        ObservationIndex([hotter, cooler])
    # 観測を直接渡す入口も同じ契約（索引を外から作るかどうかで変わらない）。
    with pytest.raises(ShadowObservationConflictError, match="食い違う観測"):
        match_outcome(outcome_matcher(), [cooler, hotter])


def test_invariant_3_j_the_same_observation_twice_changes_nothing() -> None:
    """同じ値が2度届くのは冪等な取り込みで起きる。**それは食い違いではない。**

    1つに畳むので、件数も突き合わせ結果も「何回渡したか」に依存しない（0055 §2.1）。
    """
    matcher = outcome_matcher()
    observations = [observation(101_000, 50.5), observation(102_000, 51.5)]

    once = match_outcome(matcher, observations)
    twice = match_outcome(matcher, [*observations, observations[0], observations[1]])

    assert twice == once
    assert once.complete is True


def test_invariant_3_k_a_conflict_among_unusable_values_is_not_a_conflict() -> None:
    """証拠に使わない値（stale / suspect / missing）の食い違いでは止めない。

    索引に入らない値は誤差にも件数にも効かない。**証拠にならない値の不一致で
    照合そのものを落とすと、採点できたはずの予測まで巻き込む。**
    """
    outcome = match_outcome(
        outcome_matcher(),
        [
            observation(101_000, 50.5),
            observation(101_000, 80.5, quality=Quality.STALE),
            observation(102_000, 51.5),
        ],
    )

    assert outcome.matches[0].observed == 50.5
    assert outcome.complete is True


# ------------------------------------- 不変条件 4: 裏づけの無い数値を残さない


def test_invariant_4_a_an_unattested_proposal_records_no_confidence() -> None:
    """assessment と束ねられない提案の confidence / ood は**記録しない**。"""
    proposal = shadow_proposal(0.9, confidence=0.99)
    # Registry を通っていない判定は Gate が裏づけとして扱わない（#85）。
    offline = assessment_for(proposal).model_copy(
        update={"artifact_verification": ArtifactVerification.OFFLINE_UNVERIFIED}
    )
    result = mpc_result(proposal, assessment=offline)
    selection = gate_selection(learned=worker_status(result))
    state = control_state(selection)
    record = ShadowRecorder(SHADOW_CONFIG).record(
        tick_id=7,
        ts_ms=TICK_TS_MS,
        state=state,
        effective=demands(0.4),
        selection=selection,
        baseline=fallback_proposal(0.4),
        learned=result,
    )

    assert record is not None
    (counterfactual,) = record.counterfactuals
    assert counterfactual.attested is False
    assert counterfactual.confidence is None and counterfactual.ood is None
    # 提案が自称した値は、どこにも残っていない。
    assert "0.99" not in record.model_dump_json()


def test_invariant_4_b_the_recorded_confidence_is_the_gate_s_not_the_proposal_s() -> None:
    """裏づけのある tick では、Gate が判定した値だけが残る。"""
    proposal = shadow_proposal(0.9, confidence=0.9)
    _selection, _state, record = recorded(learned=mpc_result(proposal))

    assert record is not None
    (counterfactual,) = record.counterfactuals
    assert counterfactual.attested is True
    assert counterfactual.confidence == 0.9 and counterfactual.ood is False


def test_invariant_4_c_a_counterfactual_cannot_claim_numbers_without_attestation() -> None:
    """裏づけの無い counterfactual に数値だけを入れられない。"""
    with pytest.raises(ValidationError, match="attested"):
        ShadowCounterfactual(
            controller=ControllerKind.LEARNED_MPC,
            requested=demands(0.8),
            reason=Reason(code="optimizer_ok"),
            optimizer_status=OptimizerStatus.TIMEOUT,
            model_version="thermal-v1",
            inference_id="c" * 64,
            artifact_sha256="a" * 64,
            attested=False,
            confidence=0.99,
            ood=False,
        )


def test_invariant_4_d_an_ood_counterfactual_keeps_the_zero_confidence_rule() -> None:
    """OOD の confidence は 0（決定記録 0050 §2.2）。別の値で残せない。"""
    with pytest.raises(ValidationError, match="OOD の confidence"):
        ShadowCounterfactual(
            controller=ControllerKind.LEARNED_MPC,
            requested=demands(0.8),
            reason=Reason(code="optimizer_ok"),
            optimizer_status=OptimizerStatus.TIMEOUT,
            model_version="thermal-v1",
            inference_id="c" * 64,
            artifact_sha256="a" * 64,
            attested=True,
            confidence=0.7,
            ood=True,
        )


def test_invariant_4_e_the_fallback_counterfactual_carries_no_ml_fields() -> None:
    """Baseline の counterfactual に ML の項目を入れない（#76 と同じ線）。"""
    with pytest.raises(ValidationError, match="ML の項目"):
        ShadowCounterfactual(
            controller=ControllerKind.FALLBACK,
            requested=demands(0.4),
            reason=Reason(code="fallback_curve"),
            model_version="thermal-v1",
        )


# ------------------------------------- 不変条件 5: 失敗も記録する


def test_invariant_5_a_a_timed_out_optimizer_is_recorded_without_a_solution() -> None:
    """timeout は**記録され**、解も予測も持たない（Gate も active にしない）。"""
    proposal = shadow_proposal(0.9)
    _selection, _state, record = recorded(learned=mpc_result(proposal))

    assert record is not None
    (counterfactual,) = record.counterfactuals
    assert counterfactual.optimizer_status is OptimizerStatus.TIMEOUT
    assert counterfactual.prediction is None and counterfactual.cost_total is None
    assert counterfactual.requested is not None


def test_invariant_5_b_a_worker_failure_is_recorded_with_its_reason() -> None:
    """model 読込失敗のように**提案が作れなかった** tick も、理由付きで残る。"""
    _selection, _state, record = recorded(
        learned=mpc_result(failure=LearnedFailure.MODEL_LOAD_FAILURE)
    )

    assert record is not None
    (counterfactual,) = record.counterfactuals
    assert counterfactual.requested is None
    assert counterfactual.failure is not None
    assert counterfactual.failure.code == "model_load_failure"
    assert "capability mismatch" in counterfactual.failure.detail


def test_invariant_5_c_a_failed_counterfactual_cannot_carry_a_solution() -> None:
    """失敗した tick に「解らしきもの」を残せない。"""
    with pytest.raises(ValidationError, match="推論の記録を残さない"):
        ShadowCounterfactual(
            controller=ControllerKind.LEARNED_MPC,
            failure=Reason(code="optimizer_exception"),
            optimizer_status=OptimizerStatus.OK,
        )


def test_invariant_5_d_a_non_ok_status_cannot_carry_a_prediction() -> None:
    """timeout / error の tick に予測を付けて「当たっていた」記録を作れない。"""
    with pytest.raises(ValidationError, match="ok でない"):
        ShadowCounterfactual(
            controller=ControllerKind.LEARNED_MPC,
            requested=demands(0.8),
            reason=Reason(code="optimizer_budget_exhausted"),
            optimizer_status=OptimizerStatus.TIMEOUT,
            model_version="thermal-v1",
            inference_id="c" * 64,
            artifact_sha256="a" * 64,
            prediction=prediction(),
        )


# ------------------------------------- 不変条件 6: RulePolicy active + MPC shadow


def supervisor_decision(*, tick_id: int = 7, ts_ms: int = ACTION_TS_MS) -> SupervisorDecision:
    """RulePolicy を active、RL を shadow にした1 tick の Supervisor 判断。"""
    rule = supervisor_output()
    rl = rule.model_copy(update={"policy": SupervisorPolicyKind.RL, "version": "rl-1"})
    return SupervisorDecision(
        tick_id=tick_id,
        ts_ms=ts_ms,
        snapshot_schema_version=1,
        active=SupervisorPolicyEvaluation(policy=SupervisorPolicyKind.RULE, output=rule),
        shadow=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RL,
            output=rl,
            received_monotonic_ms=10,
            source_monotonic_ms=10,
        ),
    )


def rule_active_tick() -> ControlTick:
    """RulePolicy active・MPC shadow・RL shadow を1つの trace に収めた tick。"""
    proposal = shadow_proposal(0.9)
    selection, state, record = recorded(
        learned=mpc_result(proposal), supervisor=supervisor_output(), ts_ms=ACTION_TS_MS
    )
    assert record is not None
    decision = supervisor_decision()
    state = state.model_copy(
        update={
            "supervisor_policy": SupervisorPolicyKind.RULE.value,
            "workload_regime": WorkloadRegime.SUSTAINED_GPU,
            "regime_confidence": 0.9,
        }
    )
    return tick_with(
        record,
        state=state,
        selection=selection,
        ts_ms=ACTION_TS_MS,
        supervisor=decision,
    )


def test_invariant_6_a_rule_policy_active_with_mpc_and_rl_shadow_is_one_record() -> None:
    """**RulePolicy active + MPC / RL shadow** を同じ tick に記録できる（受入基準）。"""
    tick = rule_active_tick()
    restored = ControlTick.model_validate_json(tick.model_dump_json())

    assert restored.state.active_controller is ControllerKind.FALLBACK
    assert restored.supervisor is not None
    assert restored.supervisor.active.policy is SupervisorPolicyKind.RULE
    assert restored.supervisor.shadow is not None
    assert restored.supervisor.shadow.policy is SupervisorPolicyKind.RL
    assert restored.shadow is not None
    assert restored.shadow.supervisor is not None
    # 戦略と weight が counterfactual と一緒に読める。
    assert restored.shadow.supervisor.strategy == "balanced"
    assert restored.shadow.supervisor.weights.gpu_temperature == 1.0
    assert [item.controller for item in restored.shadow.counterfactuals] == [
        ControllerKind.LEARNED_MPC
    ]


def test_invariant_6_b_the_shadow_strategy_must_exist_in_this_tick() -> None:
    """counterfactual が前提にした戦略を、**その tick に無い出力**にできない。"""
    tick = rule_active_tick()
    assert tick.shadow is not None and tick.shadow.supervisor is not None
    other = tick.shadow.supervisor.model_copy(update={"strategy": "quiet"})
    forged = tick.shadow.model_copy(update={"supervisor": other})

    payload = tick.model_dump(mode="python") | {"shadow": forged.model_dump(mode="python")}
    with pytest.raises(ValidationError, match="decision に無い"):
        ControlTick.model_validate(payload)


def test_invariant_6_c_the_baseline_becomes_the_counterfactual_when_mpc_is_active() -> None:
    """Learned MPC が active な tick では、**Baseline のほうが counterfactual になる。**"""
    proposal = learned_proposal(0.5)
    selection = gate_selection(stage="full", learned=learned_status(proposal))
    assert selection.active_controller is ControllerKind.LEARNED_MPC
    state = control_state(selection, stage=AuthorityStage.FULL)

    record = ShadowRecorder(SHADOW_CONFIG).record(
        tick_id=7,
        ts_ms=TICK_TS_MS,
        state=state,
        effective=demands(0.5),
        selection=selection,
        baseline=fallback_proposal(0.4),
    )

    assert record is not None
    (counterfactual,) = record.counterfactuals
    assert counterfactual.controller is ControllerKind.FALLBACK
    assert counterfactual.requested == demands(0.4)
    tick_with(record, state=state, selection=selection, effective=0.5)


# ------------------------------------- 不変条件 7: #91 へ export できる


def trace_rows(*ticks: ControlTick) -> tuple[ControlTraceRecord, ...]:
    return tuple(
        ControlTraceRecord(
            ts_ms=tick.ts_ms,
            tick_id=tick.tick_id,
            schema_version=tick.schema_version,
            trace_json=tick.model_dump_json(),
        )
        for tick in ticks
    )


def exportable_tick(trained_model) -> ControlTick:
    controller, _model, _settings = build_controller(
        trained_model, policy_config=mpc_policy(authority="shadow"), acoustic=True
    )
    result = propose(controller, baseline=0.3)
    selection, state, record = recorded(learned=result)
    assert record is not None
    return tick_with(record, state=state, selection=selection)


def test_invariant_7_a_the_export_joins_applied_counterfactual_and_outcome(trained_model) -> None:
    """**受入基準**: 実測 future と予測 future を自動で比べられる形で export できる。

    SHADOW stage の tick なので、掛かっていたのは Fallback の demand であり、
    予測が前提にした候補 action とは違う。実測は残るが**採点はしない**（不変条件 10）。
    """
    tick = exportable_tick(trained_model)
    assert tick.shadow is not None
    predicted = tick.shadow.counterfactuals[0].prediction
    assert predicted is not None
    matcher = outcome_matcher(match_tolerance_ms=200)
    observations = [
        OutcomeObservation(
            metric=metric, ts_ms=target.expected_ts_ms, value=value + 1.0, quality=Quality.OK
        )
        for target in predicted.targets
        for metric, value in target.values.items()
    ]

    rows = list(shadow_rows(trace_rows(tick), observations=observations, matcher=matcher))

    (row,) = rows
    assert row.tick_id == tick.tick_id and row.ts_ms == tick.ts_ms
    assert row.shadow.applied_controller is ControllerKind.FALLBACK
    assert row.shadow.applied_effective == demands(0.4)
    (outcome,) = row.outcomes
    assert outcome.inference_id == predicted.inference_id
    assert outcome.status is OutcomeStatus.UNIDENTIFIABLE
    assert outcome.unidentifiable is not None
    # 実測は残す（#91 が「照合はできたが採点できない」と読めるように）。
    assert outcome.complete is True
    assert all(item.observed is not None and item.error is None for item in outcome.matches)
    assert counterfactual_controllers(rows) == frozenset({ControllerKind.LEARNED_MPC})


def test_invariant_7_b_the_export_is_deterministic(trained_model) -> None:
    """同じ入力からは同じ bytes。#91 の比較が再現できる。"""
    tick = exportable_tick(trained_model)
    rows = trace_rows(tick)

    first, second = io.StringIO(), io.StringIO()
    assert write_shadow_jsonl(shadow_rows(rows), first) == 1
    assert write_shadow_jsonl(shadow_rows(rows), second) == 1

    assert first.getvalue() == second.getvalue()
    assert first.getvalue().endswith("\n")


def test_invariant_7_c_ticks_without_a_counterfactual_are_not_exported() -> None:
    """counterfactual の無い tick は Shadow 実績ではない。"""
    selection = gate_selection()
    tick = tick_with(None, state=control_state(selection))

    assert list(shadow_rows(trace_rows(tick))) == []


@pytest.mark.parametrize("field", ["tick_id", "ts_ms", "schema_version"])
def test_invariant_7_d_a_trace_whose_index_disagrees_is_refused(trained_model, field) -> None:
    """索引と中身が食い違う trace を、そのまま評価へ流さない。**版も照らす。**

    版だけがずれた trace を通すと、索引の版で読み分ける側が中身と違う意味で解釈する
    （`coldaisle.dataset` の trace 検証と同じ）。
    """
    tick = exportable_tick(trained_model)
    (row,) = trace_rows(tick)
    forged = row.model_copy(update={field: getattr(row, field) + 1})

    with pytest.raises(ValueError, match="索引と中身"):
        list(shadow_rows((forged,)))


def test_invariant_7_f_a_broken_index_is_refused_even_without_a_counterfactual() -> None:
    """counterfactual を持たない行でも索引は照らす。

    素通しすると、索引の壊れた trace が「この期間には Shadow 実績が無い」に化ける。
    """
    selection = gate_selection()
    tick = tick_with(None, state=control_state(selection))
    (row,) = trace_rows(tick)
    forged = row.model_copy(update={"schema_version": row.schema_version - 1})

    with pytest.raises(ValueError, match="索引と中身"):
        list(shadow_rows((forged,)))


def test_invariant_7_e_an_unmatched_outcome_is_never_filled_in(trained_model) -> None:
    """実測が無い予測は、**埋め合わせずに**未照合として export する。"""
    tick = exportable_tick(trained_model)
    matcher = outcome_matcher(match_tolerance_ms=200)

    (row,) = list(shadow_rows(trace_rows(tick), observations=[], matcher=matcher))

    (outcome,) = row.outcomes
    assert outcome.matched == 0
    assert all(item.observed is None and item.unmatched is not None for item in outcome.matches)


# ------------------------------------- 不変条件 8: 記録は止められる


def test_invariant_8_a_disabling_the_record_changes_nothing_but_the_record() -> None:
    """設定で記録を止めても、Gate の選択（= 実際に掛かる demand）は変わらない。"""
    proposal = shadow_proposal(0.9)
    enabled_selection, _state, enabled = recorded(learned=mpc_result(proposal))
    disabled_selection, _state, disabled = recorded(
        learned=mpc_result(proposal), config=DISABLED_SHADOW_CONFIG
    )

    assert enabled is not None
    assert disabled is None
    assert enabled_selection.proposal == disabled_selection.proposal


def test_invariant_8_b_a_tick_without_a_learned_proposal_records_nothing() -> None:
    """比べる提案が1つも無ければ、空の記録を作らない。"""
    selection = gate_selection()

    record = ShadowRecorder(SHADOW_CONFIG).record(
        tick_id=7,
        ts_ms=TICK_TS_MS,
        state=control_state(selection),
        effective=demands(0.4),
        selection=selection,
        baseline=fallback_proposal(0.4),
    )

    assert record is None


def test_the_recorder_refuses_a_baseline_that_is_not_the_fallback() -> None:
    """`baseline` に Learned の提案を渡せない（比較の基準がずれる）。"""
    selection = gate_selection()
    with pytest.raises(ValueError, match="Fallback の提案"):
        ShadowRecorder(SHADOW_CONFIG).record(
            tick_id=7,
            ts_ms=TICK_TS_MS,
            state=control_state(selection),
            effective=demands(0.4),
            selection=selection,
            baseline=learned_proposal(0.9),
        )


# ------------------------------------- 不変条件 9: 写した上限が元の契約と一致する


def test_invariant_9_a_the_structural_limits_match_the_contracts_they_copy() -> None:
    """**写し元より狭い上限を置かない。**

    記録側だけが狭いと、設定としては妥当な MPC が出した解を記録できず、tick の途中で
    記録が失敗する。schema から上位 module を import しない代わりに、ここで突き合わせる。
    """
    from coldaisle.control.config import MAX_MPC_HORIZON_STEPS
    from coldaisle.control.model.thermal import (
        MAX_METRIC_NAME_LENGTH,
        MAX_TARGET_HORIZONS,
        MAX_TARGET_METRICS,
    )
    from coldaisle.control.schema import (
        MAX_SHADOW_COUNTERFACTUALS,
        MAX_SHADOW_METRIC_NAME_LENGTH,
        MAX_SHADOW_PLAN_STEPS,
        MAX_SHADOW_PREDICTION_METRICS,
    )

    assert MAX_SHADOW_PLAN_STEPS >= MAX_MPC_HORIZON_STEPS
    assert MAX_SHADOW_PLAN_STEPS >= MAX_TARGET_HORIZONS
    assert MAX_SHADOW_PREDICTION_METRICS >= MAX_TARGET_METRICS
    assert MAX_SHADOW_METRIC_NAME_LENGTH >= MAX_METRIC_NAME_LENGTH
    # 制御器ごとに高々1つしか入らないので、種類の数と一致する。
    assert len(ControllerKind) == MAX_SHADOW_COUNTERFACTUALS


def test_invariant_9_b_the_metric_name_contract_matches_the_model_s() -> None:
    """記録する metric 名の形を、モデルの target metric より狭くしない。"""
    from coldaisle.control.model.thermal import ThermalMetricName
    from coldaisle.control.schema import SHADOW_METRIC_NAME_PATTERN

    def pattern_of(annotated: object) -> str:
        found = [
            constraint.pattern
            for item in getattr(annotated, "__metadata__", ())
            for constraint in (*getattr(item, "metadata", ()), item)
            if getattr(constraint, "pattern", None) is not None
        ]
        assert found, f"pattern を持たない注釈: {annotated!r}"
        return str(found[0])

    assert pattern_of(ThermalMetricName) == SHADOW_METRIC_NAME_PATTERN


def test_invariant_9_c_a_full_horizon_solution_can_be_recorded() -> None:
    """設定が許す**最大の horizon** の解も、そのまま記録できる（途中で失敗しない）。"""
    from coldaisle.control.config import MAX_MPC_HORIZON_STEPS

    offsets = tuple(1_000 * (index + 1) for index in range(MAX_MPC_HORIZON_STEPS))
    plan = plan_for(0.8, offsets=offsets)
    values = tuple(50.0 + index for index in range(len(offsets)))

    counterfactual = ShadowCounterfactual(
        **solved_counterfactual(
            plan=plan, prediction=prediction(plan=plan, offsets=offsets, values=values)
        )
    )

    assert counterfactual.plan is not None
    assert len(counterfactual.plan.steps) == MAX_MPC_HORIZON_STEPS


# ------------------------------------- 不変条件 10: 採点は実行された区間だけ


SCORED_ACTION_TS_MS = 100_000


def solved_shadow_tick(*, applied: float, ts_ms: int = SCORED_ACTION_TS_MS) -> ControlTick:
    """解を持つ counterfactual を1件だけ持つ tick（適用値は引数で決める）。"""
    record = ShadowRecord(
        tick_id=1,
        ts_ms=ts_ms,
        authority_stage=AuthorityStage.SHADOW,
        applied_controller=ControllerKind.FALLBACK,
        applied_effective=demands(applied),
        counterfactuals=(ShadowCounterfactual(**solved_counterfactual()),),
    )
    return ControlTick(
        tick_id=1,
        ts_ms=ts_ms,
        state=ControlState(
            operating_mode=OperatingMode.AUTO,
            authority_stage=AuthorityStage.SHADOW,
            active_controller=ControllerKind.FALLBACK,
            safety_state=SafetyState.NORMAL,
            fallback_active=True,
        ),
        zones=zone_records(applied),
        shadow=record,
    )


def follow_up_tick(*, tick_id: int, ts_ms: int, applied: float) -> ControlTick:
    """counterfactual を持たない後続 tick。**その時刻に何が掛かっていたかの証拠**になる。"""
    return ControlTick(
        tick_id=tick_id,
        ts_ms=ts_ms,
        state=ControlState(
            operating_mode=OperatingMode.AUTO,
            authority_stage=AuthorityStage.SHADOW,
            active_controller=ControllerKind.FALLBACK,
            safety_state=SafetyState.NORMAL,
            fallback_active=True,
        ),
        zones=zone_records(applied),
    )


def scored_export(
    *,
    applied: float,
    follow_up: float | None = None,
    tolerance: float = 0.01,
    with_follow_up: bool = True,
):
    """適用値を変えて1件 export する。plan の demand は 0.8（`solved_counterfactual`）。"""
    ticks = [solved_shadow_tick(applied=applied)]
    if with_follow_up:
        ticks.append(
            follow_up_tick(
                tick_id=2,
                ts_ms=SCORED_ACTION_TS_MS + 1_000,
                applied=applied if follow_up is None else follow_up,
            )
        )
    observations = [
        observation(SCORED_ACTION_TS_MS + 1_000, 50.5),
        observation(SCORED_ACTION_TS_MS + 2_000, 51.5),
    ]
    rows = list(
        shadow_rows(
            trace_rows(*ticks),
            observations=observations,
            matcher=outcome_matcher(applied_demand_tolerance=tolerance),
        )
    )
    (row,) = rows
    (outcome,) = row.outcomes
    return outcome


def test_invariant_10_a_a_shadow_stage_outcome_is_unidentifiable() -> None:
    """掛かっていたのが別の action なら、**差を予測誤差として残さない。**

    Shadow の提案は実行されていない。実測との差は「制御器の違い + モデル誤差」であって、
    モデル誤差ではない（決定記録 0053 §2.3）。
    """
    outcome = scored_export(applied=0.4)

    assert outcome.status is OutcomeStatus.UNIDENTIFIABLE
    assert outcome.unidentifiable is not None
    assert outcome.unidentifiable.code == "applied_action_differs"
    assert outcome.matched == 2
    assert all(item.error is None for item in outcome.matches)


def test_invariant_10_b_an_applied_sequence_that_reproduces_the_plan_is_scored() -> None:
    """予測した候補 action が**実際に掛かっていた**区間なら、誤差を出してよい。"""
    outcome = scored_export(applied=0.8)

    assert outcome.status is OutcomeStatus.SCORED
    assert outcome.unidentifiable is None
    assert outcome.scored is True
    assert [item.error for item in outcome.matches] == [
        pytest.approx(0.5),
        pytest.approx(0.5),
    ]


def test_invariant_10_c_a_later_step_that_drifts_away_is_not_scored() -> None:
    """**horizon 全体**を見る。後半の step で別の値に変わっていれば採点しない。"""
    outcome = scored_export(applied=0.8, follow_up=0.4)

    assert outcome.status is OutcomeStatus.UNIDENTIFIABLE
    assert outcome.unidentifiable is not None
    assert outcome.unidentifiable.code == "applied_action_differs"


def test_invariant_10_d_a_horizon_without_recorded_ticks_is_not_scored() -> None:
    """記録が無い区間を「plan どおり」と決めつけない。**推定しない。**"""
    outcome = scored_export(applied=0.8, with_follow_up=False)

    assert outcome.status is OutcomeStatus.UNIDENTIFIABLE
    assert outcome.unidentifiable is not None
    assert outcome.unidentifiable.code == "applied_action_unknown"


@pytest.mark.parametrize(
    ("applied", "status"),
    [(0.805, OutcomeStatus.SCORED), (0.82, OutcomeStatus.UNIDENTIFIABLE)],
)
def test_invariant_10_e_the_identity_tolerance_comes_from_the_config(applied, status) -> None:
    """「同じ action」とみなす幅は設定値。コードに閾値を置かない（AGENTS.md ルール9）。"""
    assert scored_export(applied=applied, tolerance=0.01).status is status


def test_invariant_10_f_the_tolerance_cannot_accept_every_action() -> None:
    """許容幅で判定を骨抜きにできない（1.0 はどんな適用値も plan どおりにする）。"""
    with pytest.raises(ValueError, match="1 未満"):
        outcome_matcher(applied_demand_tolerance=1.0)
    with pytest.raises(ValidationError, match="applied_demand_tolerance"):
        _shadow_config_document(tolerance=1.0)


def _shadow_config_document(*, tolerance: float) -> ShadowConfig:
    return ShadowConfig.model_validate(
        {
            "enabled": True,
            "outcome_match_tolerance_ms": {"value": 500, "status": "provisional"},
            "applied_demand_tolerance": {"value": tolerance, "status": "provisional"},
        }
    )


def test_invariant_10_g_identity_cannot_be_judged_with_another_plan() -> None:
    """別の候補 plan で識別を判定しない（予測と plan の識別子で閉じる）。"""
    plan = plan_for()
    other = plan_for(0.2)
    with pytest.raises(ShadowOutcomeUnusableError, match="別の候補 plan"):
        outcome_matcher().match(
            prediction(plan=plan),
            [],
            plan=other,
            applied=applied_for(other),
        )


def varied_plan() -> ShadowActionPlan:
    """step ごとに demand が変わる候補 plan（持ち越しの取りこぼしを試すため）。"""
    return ShadowActionPlan(
        step_ms=1_000,
        steps=(
            ShadowPlanStep(offset_ms=1_000, demands=demands(0.8)),
            ShadowPlanStep(offset_ms=2_000, demands=demands(0.4)),
        ),
    )


def varied_export(*, follow_up_ts_ms: int):
    """step1=0.8 / step2=0.4 の plan を、後続 tick の時刻だけ変えて export する。"""
    plan = varied_plan()
    counterfactual = ShadowCounterfactual(
        **solved_counterfactual(
            plan=plan,
            requested=plan.first,
            prediction=prediction(plan=plan, action_ts_ms=SCORED_ACTION_TS_MS),
        )
    )
    anchor = solved_shadow_tick(applied=0.8)
    anchor = anchor.model_copy(
        update={"shadow": anchor.shadow.model_copy(update={"counterfactuals": (counterfactual,)})}
    )
    ticks = (
        ControlTick.model_validate(anchor.model_dump(mode="python")),
        follow_up_tick(tick_id=2, ts_ms=follow_up_ts_ms, applied=0.4),
    )
    rows = list(
        shadow_rows(
            trace_rows(*ticks),
            observations=[
                observation(SCORED_ACTION_TS_MS + 1_000, 50.5),
                observation(SCORED_ACTION_TS_MS + 2_000, 51.5),
            ],
            matcher=outcome_matcher(),
        )
    )
    (row,) = rows
    (outcome,) = row.outcomes
    return outcome


def test_invariant_10_h_the_demand_carried_into_an_interval_is_checked() -> None:
    """**demand は次の tick まで掛かり続ける。** 区間の中の記録だけを見ると取りこぼす。

    step1=0.8 / step2=0.4 の plan で、記録が 0ms(0.8) と 1500ms(0.4) しか無い場合、
    step2 の前半（1000〜1500ms）に掛かっていたのは 0.8 である。区間の中だけを見ると
    「0.4 が掛かっていた」と読めてしまうので、持ち越し分も照らす。
    """
    assert varied_export(follow_up_ts_ms=SCORED_ACTION_TS_MS + 1_500).status is (
        OutcomeStatus.UNIDENTIFIABLE
    )
    # 区間の先頭に記録があれば、その値が先頭から掛かっている（持ち越しは置き換わる）。
    assert varied_export(follow_up_ts_ms=SCORED_ACTION_TS_MS + 1_000).status is (
        OutcomeStatus.SCORED
    )


def test_invariant_2_g_the_recorded_candidate_must_be_the_one_the_gate_saw() -> None:
    """**同じ anchor 推論の別の提案**を、Gate が退けた候補として記録できない。

    推論の識別子だけで照合すると、同じ入力・同じ予測から作った別の候補 demand の提案を
    「Gate が退けたのはこれ」として残せてしまう。
    """
    evaluated = mpc_result(shadow_proposal(0.9))
    selection = gate_selection(learned=worker_status(evaluated))
    state = control_state(selection)
    # 同じ推論・同じ assessment だが、要求した demand が違う提案。
    another = mpc_result(shadow_proposal(0.5))
    assert another.proposal is not None and evaluated.proposal is not None
    assert another.proposal.inference_id == evaluated.proposal.inference_id

    with pytest.raises(ValueError, match="別の提案"):
        record_with(selection, state, learned=another)

    # Gate が見た結果そのものなら記録できる。
    assert record_with(selection, state, learned=evaluated) is not None


def record_with(selection, state, *, learned: MpcProposal):
    """同じ tick の記録を、worker 結果だけ差し替えて試す。"""
    return ShadowRecorder(SHADOW_CONFIG).record(
        tick_id=7,
        ts_ms=TICK_TS_MS,
        state=state,
        effective=demands(0.4),
        selection=selection,
        baseline=fallback_proposal(0.4),
        learned=learned,
    )


def test_invariant_2_h_a_selection_without_the_candidate_identity_is_refused() -> None:
    """Gate の識別子を落とした選択結果では記録しない（fail closed）。"""
    evaluated = mpc_result(shadow_proposal(0.9))
    selection = gate_selection(learned=worker_status(evaluated))
    state = control_state(selection)
    stripped = ControllerSelection.model_validate(
        selection.model_dump(mode="python") | {"candidate_digest": None}
    )

    with pytest.raises(ValueError, match="識別子が無い"):
        record_with(stripped, state, learned=evaluated)


def test_invariant_7_g_the_jsonl_omits_the_fields_that_have_no_value() -> None:
    """**採点していない結果に `"error": null` を書かない。** 値があることが意味になる形にする。

    scored / unidentifiable / unmatched の3状態が、欄の有無で読み分けられることを確かめる。
    """
    scored = _jsonl_outcome(applied=0.8)
    assert scored["status"] == "scored"
    assert "unidentifiable" not in scored
    assert all("error" in match and "unmatched" not in match for match in scored["matches"])

    unidentifiable = _jsonl_outcome(applied=0.4)
    assert unidentifiable["status"] == "unidentifiable"
    assert unidentifiable["unidentifiable"]["code"] == "applied_action_differs"
    assert all("observed" in match and "error" not in match for match in unidentifiable["matches"])

    unmatched = _jsonl_outcome(applied=0.8, observations=[])
    assert all(
        "observed" not in match and "error" not in match and "unmatched" in match
        for match in unmatched["matches"]
    )


def _jsonl_outcome(*, applied: float, observations=None) -> dict:
    """1行だけ書き出し、その行の outcome を JSON として返す（読み戻しも確かめる）。"""
    ticks = (
        solved_shadow_tick(applied=applied),
        follow_up_tick(tick_id=2, ts_ms=SCORED_ACTION_TS_MS + 1_000, applied=applied),
    )
    rows = list(
        shadow_rows(
            trace_rows(*ticks),
            observations=[
                observation(SCORED_ACTION_TS_MS + 1_000, 50.5),
                observation(SCORED_ACTION_TS_MS + 2_000, 51.5),
            ]
            if observations is None
            else observations,
            matcher=outcome_matcher(),
        )
    )
    stream = io.StringIO()
    assert write_shadow_jsonl(rows, stream) == 1
    line = stream.getvalue().strip()
    # 省略した欄は既定値として読み戻せる（意味を失わない）。
    assert ShadowExportRow.model_validate_json(line) == rows[0]
    payload = json.loads(line)
    (outcome,) = payload["outcomes"]
    return outcome


def swapped_solution(result: MpcProposal) -> MpcProposal:
    """提案も assessment もそのままに、**解（候補 plan と予測）だけ**を別物にした結果。

    同じ held plan に対する別の妥当な予測は、いくらでも作れる。検証を通さない ``model_copy``
    で作るのは、「壊れた値」ではなく「別の正しい結果」を渡したときに閉じることを試すため。
    """
    assert result.solution is not None
    prediction_ = result.solution.prediction
    other = prediction_.model_copy(
        update={
            "targets": tuple(
                target.model_copy(
                    update={
                        "values": {metric: value + 1.0 for metric, value in target.values.items()}
                    }
                )
                for target in prediction_.targets
            )
        }
    )
    return result.model_copy(
        update={"solution": result.solution.model_copy(update={"prediction": other})}
    )


def test_invariant_2_i_a_result_with_another_solution_is_refused(trained_model) -> None:
    """**解だけを差し替えた結果**を、Gate の判断に属する記録にできない。

    提案と assessment が同じなら、解（= 予測）を入れ替えても提案の識別子は変わらない。
    それだけで照合すると、Gate が見たのとは別の予測を「その判断の予測」として残せてしまう。
    """
    controller, _model, _settings = build_controller(
        trained_model, policy_config=mpc_policy(authority="shadow"), acoustic=True
    )
    evaluated = propose(controller, baseline=0.3)
    assert evaluated.proposal is not None and evaluated.solution is not None
    forged = swapped_solution(evaluated)
    assert forged.proposal == evaluated.proposal
    assert forged.assessment == evaluated.assessment
    assert forged.solution != evaluated.solution

    selection = gate_selection(learned=worker_status(evaluated))
    state = control_state(selection)

    with pytest.raises(ValueError, match="別の提案"):
        record_with(selection, state, learned=forged)
    assert record_with(selection, state, learned=evaluated) is not None


def test_invariant_2_j_the_digest_covers_every_recorded_part_of_the_result(trained_model) -> None:
    """**記録が書く値は、すべて識別子に覆われている。**

    counterfactual が worker 結果から写すのは、要求 demand・理由・optimizer の結果・latency・
    評価回数・model 版・推論 id・artifact hash・候補 plan・予測・コスト・失敗の理由である。
    そのどれを変えても識別子が変わること（= 覆われていること）を、1つずつ確かめる。
    confidence / ood は worker 結果ではなく Gate の判断（`model_gate`）から取るため、ここには
    含まれない（trace 側で `model_gate` と突き合わせる。不変条件 2-c）。
    """
    controller, _model, _settings = build_controller(
        trained_model, policy_config=mpc_policy(authority="shadow"), acoustic=True
    )
    base = propose(controller, baseline=0.3)
    assert base.proposal is not None and base.solution is not None and base.assessment is not None
    digest = base.result_digest()

    variants = {
        "requested": base.model_copy(
            update={
                "proposal": base.proposal.model_copy(
                    update={"requested": requests_of(base.proposal, 0.55)}
                )
            }
        ),
        "optimizer_status": base.model_copy(
            update={
                "proposal": base.proposal.model_copy(
                    update={"optimizer_status": OptimizerStatus.TIMEOUT}
                )
            }
        ),
        "latency_ms": base.model_copy(
            update={"proposal": base.proposal.model_copy(update={"latency_ms": 999})}
        ),
        "model_version": base.model_copy(
            update={"proposal": base.proposal.model_copy(update={"model_version": "other-v9"})}
        ),
        "inference_id": base.model_copy(
            update={"proposal": base.proposal.model_copy(update={"inference_id": "e" * 64})}
        ),
        "artifact_sha256": base.model_copy(
            update={"assessment": base.assessment.model_copy(update={"artifact_sha256": "f" * 64})}
        ),
        "evaluations": base.model_copy(
            update={"solution": base.solution.model_copy(update={"evaluations": 999})}
        ),
        "plan": base.model_copy(
            update={
                "solution": base.solution.model_copy(
                    update={"plan": other_plan(base.solution.plan)}
                )
            }
        ),
        "prediction": swapped_solution(base),
        "cost_total": base.model_copy(
            update={
                "solution": base.solution.model_copy(update={"cost": zero_cost(base.solution.cost)})
            }
        ),
        "failure_reason": base.model_copy(update={"failure_reason": Reason(code="other_failure")}),
    }
    unchanged = sorted(
        name for name, variant in variants.items() if variant.result_digest() == digest
    )
    assert unchanged == []


def requests_of(proposal: ControllerProposal, demand: float):
    request = proposal.requested.front.model_copy(update={"demand": demand})
    return PerZone(front=request, rear=request, top=request)


def other_plan(plan):
    return plan.model_copy(
        update={
            "steps": tuple(
                step.model_copy(update={"demands": demands(0.11)}) for step in plan.steps
            )
        }
    )


def zero_cost(cost):
    return cost.model_copy(update={"total": cost.total + 1.0})


def test_the_counterfactual_artifact_comes_from_the_gate_not_the_assessment() -> None:
    """**counterfactual の artifact も、Gate が照合し終えた値にする**（#159 / 決定記録 0059）。

    assessment の欄をそのまま写すと、Gate が束縛した attestation と照らしていない値が
    counterfactual 側にだけ残る（codex #4057191721 と同じ型の穴）。同じ tick の
    `model_gate` と食い違う artifact を `ControlTick` は拒むので、trace 全体で1つになる。
    """
    proposal = shadow_proposal(0.9)
    selection, state, record = recorded(learned=mpc_result(proposal))

    assert record is not None
    assert selection.model_gate is not None
    assert selection.model_gate.attested is True
    item = next(
        counterfactual
        for counterfactual in record.counterfactuals
        if counterfactual.controller is ControllerKind.LEARNED_MPC
    )
    assert item.attested is True
    assert item.artifact_sha256 == selection.model_gate.artifact_sha256

    # 同じ tick に2つの artifact を書けないことを、schema の側でも確かめる。
    tick = ControlTick(
        tick_id=7,
        ts_ms=TICK_TS_MS,
        state=state,
        zones=zone_records(0.4),
        model_gate=selection.model_gate,
        shadow=record,
    )
    assert tick.model_gate is not None
    assert tick.model_gate.artifact_sha256 == item.artifact_sha256
