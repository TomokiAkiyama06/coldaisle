"""#86 Learned MPC optimizer と Hard Constraints の連携。実機不要（合成 dataset / 試験用モデル）。

**ここでは「守れているか」ではなく「破れないか」を試す。** MPC の提案が Reactive Guard に
届くまでに成り立っていなければならない不変条件を並べ、1つずつ破ろうとする試験を置く。

1. 提案は**必ず1回の検証済み推論に束ねられている**（提案・assessment・解の identity が一致）
2. 内部モデルは **Registry 検証済み** かつ **反実仮想を主張する** ものだけ（決定記録 0048 §2.1）
3. optimizer が出せるのは **plan の最初の step だけ**（receding horizon）
4. 要求は **Hard Constraints の中**。Critical Safety の floor を下回れない
5. 実行不能なら**当て推量を返さない**（`ERROR` → Fallback）
6. budget / 評価回数を超えたら **`TIMEOUT`**。Gate はそれを active にしない
7. モデルの異常は **例外で落ちず** Fallback になり、次 tick は通常どおり動く
8. 低 Confidence / OOD の提案は Gate で落ち、trace に**裏付けの無い数値を残さない**
9. 同じ入力・同じ時計からは**同じ提案**が出る（replay 再現性）
10. 採用する解は内部モデルの上で **Baseline 以下のコスト**
11. `control/mpc` は Guard / Safety / Hardware を **import しない**
12. 別時刻の観測 window は使わない（#102 の Snapshot と時刻が揃わなければ失敗）
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from functools import cache
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.control.acoustic import (
    AcousticCurvePoint,
    AcousticModelConfig,
    AcousticModelSource,
    ConfiguredAcousticCostModel,
    ZoneAcousticCurve,
)
from coldaisle.control.air_balance import (
    AirBalanceEstimate,
    AirBalanceMetadata,
    AirBalanceState,
    BalanceBand,
    CharacterizationSource,
    ThermalInputs,
)
from coldaisle.control.config import FanPolicyConfig, SafetyConfig
from coldaisle.control.fallback import ControllerGate, FallbackCause, LearnedFailure
from coldaisle.control.model.confidence import (
    ConfidenceAssessor,
    ModelConfidenceProfile,
    ResidualEvidence,
    fit_confidence_profile,
    inference_id,
)
from coldaisle.control.model.thermal import (
    ArtifactVerification,
    InferenceCapability,
    ObservedThermalInput,
    ThermalFeatureSchema,
    ThermalPrediction,
    ThermalTargetSchema,
)
from coldaisle.control.mpc import (
    ActionPlan,
    CounterfactualModelIdentity,
    HardConstraintSet,
    InfeasiblePlanError,
    LearnedMpcController,
    LearnedMpcOptimizer,
    MpcCostModel,
    MpcCostUnusableError,
    MpcModelBinding,
    MpcModelUnusableError,
    MpcProposal,
    PlannedTarget,
    PlannedThermalInput,
    PlanPrediction,
    PlanStep,
)
from coldaisle.control.schema import (
    AuthorityStage,
    ConfidenceLevel,
    ControllerKind,
    Demand,
    OperatingMode,
    OptimizerStatus,
    PerZone,
    Reason,
    SafetyState,
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    TemperatureTarget,
    WorkloadRegime,
    Zone,
)
from coldaisle.control.state import ControlStateSnapshot, TelemetryHealth
from test_critical_safety import safety_config
from test_fallback_controller import fallback_proposal, policy
from test_model_confidence import (
    GPU,
    dataset,
    evidence,
    profile_spec,
    shifted,
    split,
    train,
)

CPU = "cpu.package"
HORIZONS = (1_000, 2_000, 3_000)
TARGETS = (CPU, GPU)
STEP_MS = 1_000
ACTION_TS_MS = 10_000 + 3 * 5_000
"""試験に使う anchor の時刻（`test_model_confidence` の合成 dataset の刻み）。"""


# ---------------------------------------------------------------- 試験用の内部モデル


class PlanningModel:
    """試験用の反実仮想モデル。

    anchor 推論は #84 の実装（ridge baseline）に委ね、候補 action 列に対しては
    「demand を上げるほど温度が下がる」単調で決定論的な応答を返す。
    **本番の artifact ではない**。`identity` を自由に作れるのは試験だけで、
    `MpcModelBinding.for_control` が受け入れる条件そのものを検査するために置く。
    """

    def __init__(
        self,
        base: object,
        *,
        capability: InferenceCapability = InferenceCapability.COUNTERFACTUAL_ACTION,
        verification: ArtifactVerification = ArtifactVerification.REGISTRY_VERIFIED,
        authority: tuple[AuthorityStage, ...] = tuple(AuthorityStage),
        gain: float = 30.0,
        plan_error: Exception | None = None,
        forged_anchor_id: str | None = None,
        forged_offsets: tuple[int, ...] | None = None,
    ) -> None:
        self._base = base
        self._identity = CounterfactualModelIdentity(
            model_id=base.manifest.model_id,  # type: ignore[attr-defined]
            model_version=base.manifest.model_version,  # type: ignore[attr-defined]
            capability=capability,
            artifact_verification=verification,
            authority_compatibility=authority,
        )
        self._gain = gain
        self._plan_error = plan_error
        self._forged_anchor_id = forged_anchor_id
        self._forged_offsets = forged_offsets
        self.plan_calls = 0

    @property
    def identity(self) -> CounterfactualModelIdentity:
        """束縛の判断に使う identity。"""
        return self._identity

    @property
    def feature_schema(self) -> ThermalFeatureSchema:
        """入力契約。"""
        return self._base.feature_schema  # type: ignore[attr-defined,no-any-return]

    @property
    def target_schema(self) -> ThermalTargetSchema:
        """出力契約。"""
        return self._base.target_schema  # type: ignore[attr-defined,no-any-return]

    def predict(self, observed: ObservedThermalInput) -> ThermalPrediction:
        """anchor 推論。artifact の出どころだけ identity に合わせる。"""
        prediction = self._base.predict(observed)  # type: ignore[attr-defined]
        return ThermalPrediction.model_validate(
            prediction.model_dump(mode="python")
            | {"artifact_verification": self._identity.artifact_verification}
        )

    def predict_plan(self, planned: PlannedThermalInput) -> PlanPrediction:
        """候補 action 列に対する単調な応答を返す。"""
        self.plan_calls += 1
        if self._plan_error is not None:
            raise self._plan_error
        anchor = self.predict(planned.observed)
        anchor_mean = _mean(
            tuple(planned.observed.action.get(zone).effective_demand for zone in Zone)
        )
        by_offset = {target.horizon_ms: target.values for target in anchor.targets}
        offsets = self._forged_offsets or planned.plan.offsets_ms
        targets = []
        for index, step in enumerate(planned.plan.steps):
            offset = offsets[index]
            delta = _mean(tuple(step.demands.get(zone) for zone in Zone)) - anchor_mean
            weight = (index + 1) / len(planned.plan.steps)
            base_values = by_offset[step.offset_ms]
            targets.append(
                PlannedTarget(
                    offset_ms=offset,
                    expected_ts_ms=planned.observed.action_ts_ms + offset,
                    values={
                        metric: value - self._gain * delta * weight
                        for metric, value in base_values.items()
                    },
                )
            )
        return PlanPrediction(
            model_id=anchor.model_id,
            model_version=anchor.model_version,
            artifact_sha256=anchor.artifact_sha256,
            artifact_verification=anchor.artifact_verification,
            capability=InferenceCapability.COUNTERFACTUAL_ACTION,
            anchor_inference_id=(self._forged_anchor_id or inference_id(planned.observed, anchor)),
            input_action_ts_ms=anchor.input_action_ts_ms,
            targets=tuple(targets),
        )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


class ScriptedClock:
    """試験用の単調時計。値を使い切ったら最後の値を返し続ける。"""

    def __init__(self, *values: int) -> None:
        self._values = list(values) or [0]
        self._index = 0

    def __call__(self) -> int:
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


# ---------------------------------------------------------------- 下ごしらえ


@pytest.fixture(scope="module")
def trained() -> tuple[object, ModelConfidenceProfile]:
    """合成 dataset で学習した #84 モデルと、その Confidence Profile。"""
    data = dataset(HORIZONS, TARGETS)
    parts = split(data)
    model = train(data, parts)
    return model, fit_confidence_profile(model, data, parts, profile_spec())


def optimizer_document(**overrides: object) -> dict[str, object]:
    """試験用の `mpc.optimizer` 設定。値はすべて暫定扱い。"""
    document: dict[str, object] = {
        "horizon_ms": _provisional(STEP_MS * len(HORIZONS)),
        "step_ms": _provisional(STEP_MS),
        "candidate_levels": _provisional(5),
        "sweeps": _provisional(2),
        "max_evaluations": _provisional(64),
        "max_step_up": _provisional(1.0),
        "max_step_down": _provisional(1.0),
        "zone_bounds": {
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(1.0)} for zone in Zone
        },
        "cost_scales": {
            "temperature_c": _provisional(5.0),
            "balance_ratio": _provisional(0.2),
            "acoustic_cost": _provisional(10.0),
            "demand_change": _provisional(0.5),
        },
        "cost_metrics": {"cpu_temperature": CPU, "gpu_temperature": GPU},
        "unknown_balance_cost": _provisional(1.0),
    }
    document.update(overrides)
    return document


def _provisional(value: float | int) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def mpc_policy(
    *,
    authority: str = "full",
    budget_ms: int = 1_000,
    **optimizer: object,
) -> FanPolicyConfig:
    """#86 の設定を差し替えた fan-policy。"""
    return policy(
        authority=authority,
        mpc={
            "period_ms": 1_000,
            "budget_ms": budget_ms,
            "valid_ms": 2_000,
            "optimizer": optimizer_document(**optimizer),
        },
    )


def supervisor_output(*, balanced: bool = True) -> SupervisorOutput:
    """MPC が context として読む Supervisor 出力。"""
    weights = SupervisorObjectiveWeights(
        gpu_temperature=1.0,
        cpu_temperature=0.2,
        balance=0.0,
        acoustic=0.2 if balanced else 0.0,
        change=0.1 if balanced else 0.0,
    )
    return SupervisorOutput(
        snapshot_schema_version=1,
        tick_id=7,
        ts_ms=ACTION_TS_MS,
        policy=SupervisorPolicyKind.RULE,
        version="rule-1",
        regime=WorkloadRegime.SUSTAINED_GPU,
        regime_confidence=0.9,
        weights=weights,
        strategy="balanced",
        # 合成 dataset の予測は target band より高い。冷却を強めるほどコストが下がる局面を作る。
        target_band=SupervisorTargetBand(
            cpu_temperature=TemperatureTarget(lower_c=10.0, upper_c=25.0),
            gpu_temperature=TemperatureTarget(lower_c=20.0, upper_c=30.0),
        ),
        computed_at_ms=ACTION_TS_MS,
    )


def snapshot(ts_ms: int = ACTION_TS_MS) -> ControlStateSnapshot:
    """MPC が共有入力として受け取る #102 Snapshot。"""
    return ControlStateSnapshot(
        tick_id=7,
        ts_ms=ts_ms,
        monotonic_ms=ts_ms,
        signals=(),
        derived=(),
        trends=(),
        telemetry_health=TelemetryHealth.NORMAL,
        critical_unavailable=(),
    )


def safety() -> SafetyConfig:
    """floor を予測しやすくした Safety 設定（zone ごとの差はここでは見ない）。"""
    return safety_config(uniform_zone_min=0.2)


@cache
def observed_example() -> ObservedThermalInput:
    """anchor 時刻で終わる合成 window（同じ dataset を毎回作り直さない）。"""
    data = dataset(HORIZONS, TARGETS)
    example = next(item for item in data.examples if item.action_ts_ms == ACTION_TS_MS)
    return ObservedThermalInput.from_example(example)


def demands(value: float) -> PerZone[Demand]:
    """3 zone とも同じ demand。"""
    return PerZone[Demand](front=value, rear=value, top=value)


def observed_input(action: float | None = None) -> ObservedThermalInput:
    """anchor 時刻で終わる観測 window。``action`` を省くと dataset の実際の action を使う。"""
    base = observed_example()
    return base if action is None else _with_action(base, action)


def _with_action(observed: ObservedThermalInput, demand: float) -> ObservedThermalInput:
    return ObservedThermalInput.model_validate(
        observed.model_dump(mode="python")
        | {"action": {zone.value: {"effective_demand": demand} for zone in Zone}}
    )


def build_controller(
    trained: tuple[object, ModelConfidenceProfile],
    *,
    model: PlanningModel | None = None,
    policy_config: FanPolicyConfig | None = None,
    clock: ScriptedClock | None = None,
    acoustic: bool = False,
) -> tuple[LearnedMpcController, PlanningModel, FanPolicyConfig]:
    """束縛済みの MPC controller を組み立てる。"""
    base, profile = trained
    planning = model or PlanningModel(base)
    settings = policy_config or mpc_policy()
    binding = MpcModelBinding.for_control(
        planning,
        authority_stage=settings.authority_stage,
        expected_model_version=planning.identity.model_version,
    )
    cost = MpcCostModel(
        settings.mpc.optimizer,
        acoustic=acoustic_model() if acoustic else None,
    )
    controller = LearnedMpcController(
        binding,
        settings,
        safety(),
        cost_model=cost,
        assessor=ConfidenceAssessor(profile, settings.model_confidence),
        monotonic_ms=clock or ScriptedClock(0),
    )
    return controller, planning, settings


def acoustic_model() -> ConfiguredAcousticCostModel:
    """demand に対して単調に増える近似音響コスト。"""
    curve = ZoneAcousticCurve(
        points=(
            AcousticCurvePoint(demand=0.0, acoustic_cost=0.0),
            AcousticCurvePoint(demand=1.0, acoustic_cost=40.0),
        )
    )
    config = AcousticModelConfig(
        schema_version=1,
        model_id="test-acoustic",
        source=AcousticModelSource(kind="approximate", basis="試験用の近似"),
        zones=PerZone[ZoneAcousticCurve](front=curve, rear=curve, top=curve),
    )
    return ConfiguredAcousticCostModel(config, "f" * 64)


def propose(
    controller: LearnedMpcController,
    *,
    action: float | None = None,
    baseline: float = 0.4,
    safety_floor: float = 0.2,
    ts_ms: int = ACTION_TS_MS,
    observed: ObservedThermalInput | None = None,
    residual: ResidualEvidence | None = None,
) -> MpcProposal:
    """1 tick 分の提案を作る。"""
    window = observed if observed is not None else observed_input(action)
    current = _mean(tuple(window.action.get(zone).effective_demand for zone in Zone))
    return controller.propose(
        snapshot=snapshot(ts_ms),
        observed=window,
        supervisor=supervisor_output(),
        baseline=fallback_proposal(baseline),
        safety_floor=demands(safety_floor),
        current_demand=demands(current),
        residual=residual,
    )


# ---------------------------------------------------------------- 不変条件 2: 内部モデル


def test_invariant_2_a_dataset_v1_artifact_is_refused_as_the_internal_model(trained) -> None:
    """**観測再生だけの artifact を MPC の内部モデルにしない**（決定記録 0048 §2.1）。

    いま存在する artifact は manifest の capability が `observational_replay` に固定されている。
    束縛を許すと、後続 action 列を学習していない係数を Fan action の因果効果として使ってしまう。
    """
    base, _profile = trained
    identity = CounterfactualModelIdentity.from_manifest(
        base.manifest, artifact_verification=ArtifactVerification.REGISTRY_VERIFIED
    )

    assert identity.capability is InferenceCapability.OBSERVATIONAL_REPLAY
    with pytest.raises(MpcModelUnusableError, match="反実仮想"):
        MpcModelBinding.for_control(
            PlanningModel(base, capability=identity.capability),
            authority_stage=AuthorityStage.FULL,
            expected_model_version=identity.model_version,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"verification": ArtifactVerification.OFFLINE_UNVERIFIED}, "Registry"),
        ({"capability": InferenceCapability.OBSERVATIONAL_REPLAY}, "反実仮想"),
        ({"authority": (AuthorityStage.SHADOW,)}, "authority"),
    ],
)
def test_invariant_2_b_every_missing_condition_refuses_the_binding(trained, kwargs, message):
    """**条件を1つ欠いたモデルを「弱い権限で」使わない。** 暗黙の降格をしない。"""
    base, _profile = trained
    with pytest.raises(MpcModelUnusableError, match=message):
        MpcModelBinding.for_control(
            PlanningModel(base, **kwargs),
            authority_stage=AuthorityStage.FULL,
            expected_model_version=base.manifest.model_version,
        )


def test_invariant_2_c_a_version_mismatch_is_refused_before_any_inference(trained) -> None:
    """runtime が期待する版と違うモデルで**推論すら始めない**。"""
    base, _profile = trained
    with pytest.raises(MpcModelUnusableError, match="版"):
        MpcModelBinding.for_control(
            PlanningModel(base),
            authority_stage=AuthorityStage.FULL,
            expected_model_version="9.9.9",
        )


def test_invariant_2_d_a_refused_model_degrades_to_fallback_without_stopping(trained) -> None:
    """束縛できないモデルでも**運転は止まらない**。Gate は Fallback にする。"""
    base, _profile = trained
    settings = mpc_policy()
    with pytest.raises(MpcModelUnusableError):
        MpcModelBinding.for_control(
            PlanningModel(base, capability=InferenceCapability.OBSERVATIONAL_REPLAY),
            authority_stage=settings.authority_stage,
            expected_model_version=base.manifest.model_version,
        )
    status = MpcProposal(
        failure=LearnedFailure.MODEL_LOAD_FAILURE,
        failure_reason=Reason(code="model_unusable"),
    ).to_status(received_at_mono_ms=0)
    gate = ControllerGate(settings, expected_model_version=base.manifest.model_version)

    selection = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=status,
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.MODEL_LOAD_FAILURE.value
    assert selection.model_gate is None


def test_invariant_2_e_the_binding_cannot_be_swapped_after_it_is_verified(trained) -> None:
    """検証済みの束を**後から差し替えられない**。検査を1度通せば済む形にしない。"""
    base, _profile = trained
    binding = MpcModelBinding.for_control(
        PlanningModel(base),
        authority_stage=AuthorityStage.FULL,
        expected_model_version=base.manifest.model_version,
    )

    with pytest.raises(AttributeError):
        binding._model = PlanningModel(base)  # type: ignore[misc]
    with pytest.raises(TypeError):
        MpcModelBinding()


# ---------------------------------------------------------------- 不変条件 1: 推論への束縛


def test_invariant_1_a_the_proposal_carries_its_own_assessment(trained) -> None:
    """提案の `inference_id` は、判定した assessment と**同じ推論**を指す。"""
    controller, _model, _settings = build_controller(trained)

    result = propose(controller)

    assert result.proposal is not None and result.assessment is not None
    assert result.proposal.inference_id == result.assessment.inference_id
    assert result.proposal.confidence == result.assessment.confidence
    assert result.proposal.ood == result.assessment.ood
    assert result.solution is not None
    assert result.solution.anchor_inference_id == result.proposal.inference_id


def test_invariant_1_b_a_proposal_cannot_be_paired_with_another_inference(trained) -> None:
    """**別の推論の判定を付け替えられない。** OOD の入力が in-distribution に見えてしまう。"""
    controller, _model, _settings = build_controller(trained)
    first = propose(controller, action=0.4)
    second = propose(controller, action=0.5)
    assert first.assessment is not None and second.proposal is not None
    assert first.assessment.inference_id != second.assessment.inference_id  # type: ignore[union-attr]

    with pytest.raises(ValidationError, match="別の推論"):
        MpcProposal(proposal=second.proposal, assessment=first.assessment)


def test_invariant_1_c_a_proposal_without_an_assessment_cannot_be_built(trained) -> None:
    """判定の付いていない提案を worker が**作れない**ようにする。"""
    controller, _model, _settings = build_controller(trained)
    result = propose(controller)

    with pytest.raises(ValidationError, match="assessment"):
        MpcProposal(proposal=result.proposal)


def test_invariant_1_d_a_plan_prediction_from_another_inference_is_rejected(trained) -> None:
    """optimizer は**別の anchor に属する予測**でコストを測らない。

    解を返さず `ERROR` にするので、Gate は Fallback へ落とす。
    """
    base, _profile = trained
    controller, _model, settings = build_controller(
        trained, model=PlanningModel(base, forged_anchor_id="9" * 64)
    )

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR

    gate = ControllerGate(settings, expected_model_version=base.manifest.model_version)
    selection = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=result.to_status(received_at_mono_ms=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OPTIMIZER_ERROR.value


def test_invariant_1_e_a_prediction_for_other_steps_is_rejected(trained) -> None:
    """**候補と違う step 列の予測**を受け取ったまま最適化しない。"""
    base, _profile = trained
    forged = PlanningModel(base, forged_offsets=(1_000, 2_000, 4_000))
    controller, _model, _settings = build_controller(trained, model=forged)

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR


# ---------------------------------------------------------------- 不変条件 3: 最初の step だけ


def test_invariant_3_a_only_the_first_step_becomes_the_request(trained) -> None:
    """**実行するのは plan の最初の step だけ**（receding horizon）。"""
    controller, _model, settings = build_controller(trained)

    result = propose(controller)

    assert result.solution is not None and result.proposal is not None
    plan = result.solution.plan
    assert len(plan.steps) == settings.mpc.optimizer.steps == len(HORIZONS)
    for zone in Zone:
        assert result.proposal.requested.get(zone).demand == plan.first.get(zone)


def test_invariant_3_b_a_solution_cannot_claim_a_request_outside_its_plan() -> None:
    """解の `requested` を plan の最初の step と**食い違わせられない**。"""
    from coldaisle.control.mpc.optimizer import MpcSolution

    plan = ActionPlan.held(demands(0.4), step_ms=STEP_MS, steps=2)
    with pytest.raises(ValidationError, match="最初の step"):
        MpcSolution.model_validate(
            {
                "plan": plan.model_dump(mode="python"),
                "requested": demands(0.9).model_dump(mode="python"),
                "cost": _flat_cost(1.0),
                "baseline_cost": _flat_cost(2.0),
                "baseline_requested": demands(0.4).model_dump(mode="python"),
                "anchor_inference_id": "a" * 64,
                "evaluations": 1,
            }
        )


def _flat_cost(total: float) -> dict[str, object]:
    return {
        "total": total,
        "terms": {
            "gpu_temperature": total,
            "cpu_temperature": 0.0,
            "balance": 0.0,
            "acoustic": 0.0,
            "change": 0.0,
        },
        "steps": 2,
        "balance_known_steps": 0,
    }


def test_invariant_3_c_a_plan_cannot_have_an_irregular_step_grid() -> None:
    """plan の step 幅を**不揃いにできない**。予測の時刻と対応が崩れる。"""
    with pytest.raises(ValidationError, match="等間隔"):
        ActionPlan(
            step_ms=1_000,
            steps=(
                PlanStep(offset_ms=1_000, demands=demands(0.4)),
                PlanStep(offset_ms=2_500, demands=demands(0.4)),
            ),
        )


# ---------------------------------------------------------------- 不変条件 4 / 5: 制約


def test_invariant_4_a_the_request_never_goes_below_the_safety_floor(trained) -> None:
    """**Critical Safety の floor を下回る要求を作らない。**

    後段の #78 が必ず引き上げるが、そもそも下回る候補を評価しない。
    """
    controller, _model, _settings = build_controller(trained)

    for floor in (0.2, 0.5, 0.75, 0.95):
        result = propose(controller, baseline=0.3, safety_floor=floor)
        assert result.proposal is not None
        for zone in Zone:
            assert result.proposal.requested.get(zone).demand >= floor


def test_invariant_4_b_the_constraint_set_only_narrows(trained) -> None:
    """設定の探索範囲・Safety floor・変化幅は**狭める向きにしか働かない**。"""
    settings = mpc_policy(max_step_up=_provisional(0.05), max_step_down=_provisional(0.05))
    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.2),
        current=demands(0.5),
    )

    lower, upper = constraints.window(Zone.FRONT)
    assert (lower, upper) == (pytest.approx(0.45), pytest.approx(0.55))
    assert constraints.clamp(Zone.FRONT, 1.0) == pytest.approx(0.55)
    assert constraints.clamp(Zone.FRONT, 0.0) == pytest.approx(0.45)


def test_invariant_4_c_a_rising_safety_floor_beats_the_rate_limit(trained) -> None:
    """Safety floor が上がった tick では**上げ幅の制限より floor を優先する**。

    制限を優先すると、冷却を強めるべき瞬間に弱いまま留まる。
    """
    settings = mpc_policy(max_step_up=_provisional(0.01))
    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.9),
        current=demands(0.3),
    )

    lower, upper = constraints.window(Zone.TOP)
    assert lower == pytest.approx(0.9)
    assert upper >= lower


def test_invariant_5_a_an_infeasible_constraint_set_is_refused(trained) -> None:
    """満たせる値が無い制約を**勝手に緩めない**。"""
    settings = mpc_policy(
        zone_bounds={
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(0.5)} for zone in Zone
        }
    )

    with pytest.raises(InfeasiblePlanError, match="ceiling"):
        HardConstraintSet.build(
            optimizer=settings.mpc.optimizer,
            safety=safety(),
            safety_floor=demands(0.8),
            current=demands(0.4),
        )


def test_invariant_5_b_an_infeasible_tick_degrades_to_fallback(trained) -> None:
    """実行不能な tick は当て推量を返さず、`ERROR` として Fallback へ渡る。"""
    settings = mpc_policy(
        zone_bounds={
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(0.5)} for zone in Zone
        }
    )
    controller, _model, _settings = build_controller(trained, policy_config=settings)

    result = propose(controller, safety_floor=0.9)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "ceiling" in result.failure_reason.detail


# ---------------------------------------------------------------- 不変条件 6: timeout


def test_invariant_6_a_an_exhausted_budget_yields_no_solution(trained) -> None:
    """budget を使い切ったら**解を返さない**。中途半端な探索結果を制御に使わない。"""
    controller, _model, _settings = build_controller(
        trained, clock=ScriptedClock(0, 0, 10_000), policy_config=mpc_policy(budget_ms=10)
    )

    result = propose(controller)

    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.TIMEOUT
    assert result.solution is None


def test_invariant_6_b_an_evaluation_limit_stops_the_search_deterministically(trained) -> None:
    """時計に頼らない**決定論的な打ち切り**も効く（replay で同じ結果になる）。"""
    controller, _model, _settings = build_controller(
        trained, policy_config=mpc_policy(max_evaluations=_provisional(2))
    )

    result = propose(controller)

    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.TIMEOUT
    assert result.solution is None


def test_invariant_6_c_a_timed_out_proposal_is_never_made_active(trained) -> None:
    """`TIMEOUT` の提案を Gate が active controller にしない。"""
    base, _profile = trained
    controller, _model, settings = build_controller(
        trained, clock=ScriptedClock(0, 0, 10_000), policy_config=mpc_policy(budget_ms=10)
    )
    result = propose(controller)
    gate = ControllerGate(settings, expected_model_version=base.manifest.model_version)

    selection = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=result.to_status(received_at_mono_ms=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OPTIMIZER_TIMEOUT.value
    assert selection.model_gate is not None
    assert selection.model_gate.learned_selected is False


# ---------------------------------------------------------------- 不変条件 7 / 12: 失敗と入力


def test_invariant_7_a_a_model_exception_becomes_a_failure_not_a_crash(trained) -> None:
    """内部モデルが落ちても**制御ループを落とさない**（AGENTS.md ルール4）。"""
    base, _profile = trained
    broken = PlanningModel(base, plan_error=ValueError("model exploded"))
    controller, _model, _settings = build_controller(trained, model=broken)

    result = propose(controller)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "model exploded" in result.failure_reason.detail


def test_invariant_7_b_the_worker_keeps_running_after_a_failed_tick(trained) -> None:
    """失敗した次の tick は**通常どおり提案できる**。"""
    controller, _model, _settings = build_controller(trained)
    broken = propose(controller, ts_ms=0)
    assert broken.failure is LearnedFailure.OPTIMIZER_EXCEPTION

    healthy = propose(controller)

    assert healthy.proposal is not None


def test_invariant_12_a_a_window_from_another_time_is_refused(trained) -> None:
    """**MPC は別時刻の Telemetry を使わない**（#102 の共通入力を守る）。"""
    controller, _model, _settings = build_controller(trained)

    result = propose(controller, ts_ms=ACTION_TS_MS + 1_000)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "Snapshot" in result.failure_reason.detail


# ---------------------------------------------------------------- 不変条件 8: Confidence / OOD


def test_invariant_8_a_an_ood_input_is_handed_to_the_gate_as_ood(trained) -> None:
    """学習範囲の外の入力は **OOD として Gate へ渡る**。提案の数値も判定と揃う。"""
    base, _profile = trained
    controller, _model, settings = build_controller(trained)
    ood_window = shifted(observed_input(), air=200.0)

    result = propose(controller, observed=ood_window)

    assert result.proposal is not None and result.assessment is not None
    assert result.assessment.ood is True
    assert result.proposal.ood is True
    assert result.proposal.confidence == 0.0

    gate = ControllerGate(settings, expected_model_version=base.manifest.model_version)
    selection = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=result.to_status(received_at_mono_ms=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OOD.value
    assert selection.model_gate is not None
    assert selection.model_gate.attested is True
    assert selection.model_gate.ood is True
    assert selection.model_gate.confidence_level is ConfidenceLevel.LOW
    assert selection.model_gate.learned_selected is False


def test_invariant_8_b_an_inflated_confidence_cannot_be_written_into_the_proposal(
    trained,
) -> None:
    """**提案が判定より高い confidence を名乗れない。** 名乗れば型が拒む。"""
    controller, _model, _settings = build_controller(trained)
    result = propose(controller)
    assert result.proposal is not None

    inflated = result.proposal.model_copy(update={"confidence": 1.0})
    with pytest.raises(ValidationError, match="confidence"):
        MpcProposal(proposal=inflated, assessment=result.assessment, solution=result.solution)


def test_invariant_8_c_a_shadow_stage_records_the_proposal_without_selecting_it(trained) -> None:
    """Shadow Mode は**実機を操作せず提案だけを記録する**。"""
    base, _profile = trained
    settings = mpc_policy(authority="shadow")
    controller, _model, _settings = build_controller(trained, policy_config=settings)
    result = propose(controller)
    gate = ControllerGate(settings, expected_model_version=base.manifest.model_version)

    selection = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=result.to_status(received_at_mono_ms=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.model_gate is not None
    assert selection.model_gate.learned_selected is False
    assert selection.model_gate.inference_id == result.proposal.inference_id  # type: ignore[union-attr]
    assert selection.model_gate.limits == ()


# ---------------------------------------------------------------- 不変条件 9 / 10: 再現性と改善


def test_invariant_9_a_the_same_inputs_give_the_same_proposal(trained) -> None:
    """同じ Snapshot / window / model / 設定 / 時計から**同じ提案**が出る（replay 再現性）。"""
    base, _profile = trained
    first_controller, _first_model, _ = build_controller(trained, model=PlanningModel(base))
    second_controller, _second_model, _ = build_controller(trained, model=PlanningModel(base))

    first = propose(first_controller)
    second = propose(second_controller)

    assert first.proposal is not None and second.proposal is not None
    assert first.proposal.model_dump_json() == second.proposal.model_dump_json()
    assert first.solution is not None and second.solution is not None
    assert first.solution.model_dump_json() == second.solution.model_dump_json()


def test_invariant_10_a_the_chosen_plan_is_never_worse_than_the_baseline(trained) -> None:
    """採用する解は内部モデルの上で **Baseline 以下のコスト**。探索は Baseline から始める。"""
    controller, _model, _settings = build_controller(trained, acoustic=True)

    result = propose(controller, baseline=0.3)

    assert result.solution is not None
    assert result.solution.cost.total <= result.solution.baseline_cost.total
    # Baseline 自身も制約に収める。下げる速さは safety.ramp_down_per_s（0.1/s）× step（1s）。
    assert result.solution.baseline_requested == demands(0.35)


def test_invariant_10_b_the_optimizer_improves_the_total_cost_over_the_baseline(trained) -> None:
    """予測温度が target band を超えている状況では、Baseline より**総合コストを下げる**。"""
    controller, _model, _settings = build_controller(trained, acoustic=True)

    result = propose(controller, baseline=0.3)

    assert result.solution is not None
    assert result.solution.improvement > 0.0
    # 温度が高い局面なので、より強い冷却を選ぶ（音響と変化のコストを払ってでも）。
    assert result.solution.requested.front > 0.3
    assert result.solution.cost.terms.gpu_temperature < (
        result.solution.baseline_cost.terms.gpu_temperature
    )


def test_invariant_10_c_the_acoustic_term_pulls_the_solution_back(trained) -> None:
    """音響コストを入れると**同じ入力でもより静かな解**を選ぶ。項が効いていることを示す。"""
    loud_controller, _loud_model, _ = build_controller(trained, acoustic=False)
    quiet_controller, _quiet_model, _ = build_controller(trained, acoustic=True)

    loud = propose(loud_controller, baseline=0.3)
    quiet = propose(quiet_controller, baseline=0.3)

    assert loud.solution is not None and quiet.solution is not None
    assert quiet.solution.requested.front <= loud.solution.requested.front


# ---------------------------------------------------------------- 不変条件 11: 迂回路を作らない


FORBIDDEN_MODULES = (
    "coldaisle.control.hardware",
    "coldaisle.control.safety",
    "coldaisle.control.reactive",
    "serial",
    "subprocess",
)
"""`control/mpc` が import してはいけない module（AGENTS.md ルール1 / 2 / 6）。"""


def test_invariant_11_a_the_mpc_package_cannot_reach_the_actuation_path() -> None:
    """**Guard / Safety / Hardware を迂回する経路を作らない。**

    MPC から書き込み層へ直接触れられるようになった瞬間、`requested` までという線が消える。
    """
    package = Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control" / "mpc"
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                if any(
                    name == forbidden or name.startswith(f"{forbidden}.")
                    for forbidden in FORBIDDEN_MODULES
                ):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_invariant_11_b_the_mpc_package_never_names_pwm_or_effective_demand() -> None:
    """MPC の型に **PWM も effective demand も現れない**（決定記録 0028 §2.3）。"""
    package = Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control" / "mpc"
    offenders = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        for name in sorted(names):
            if "pwm" in name.lower() or name == "EffectiveZoneDemand":
                offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_the_optimizer_refuses_a_model_whose_horizons_miss_the_control_steps(trained) -> None:
    """設定した control step を**覆えないモデル**は生成時に拒む（tick ごとに失敗させない）。"""
    base, profile = trained
    settings = mpc_policy(step_ms=_provisional(7_000), horizon_ms=_provisional(7_000))
    binding = MpcModelBinding.for_control(
        PlanningModel(base),
        authority_stage=settings.authority_stage,
        expected_model_version=base.manifest.model_version,
    )
    del profile

    with pytest.raises(MpcModelUnusableError, match="horizon"):
        LearnedMpcOptimizer(
            binding,
            settings.mpc.optimizer,
            MpcCostModel(settings.mpc.optimizer),
            budget_ms=settings.mpc.budget_ms,
            monotonic_ms=ScriptedClock(0),
        )


def test_the_optimizer_refuses_a_model_that_cannot_predict_the_cost_metrics(trained) -> None:
    """目的関数が必要とする metric を**予測できないモデル**も生成時に拒む。"""
    base, _profile = trained
    settings = mpc_policy(
        cost_metrics={"cpu_temperature": "air.front_intake", "gpu_temperature": GPU}
    )
    binding = MpcModelBinding.for_control(
        PlanningModel(base),
        authority_stage=settings.authority_stage,
        expected_model_version=base.manifest.model_version,
    )

    with pytest.raises(MpcModelUnusableError, match="metric"):
        LearnedMpcOptimizer(
            binding,
            settings.mpc.optimizer,
            MpcCostModel(settings.mpc.optimizer),
            budget_ms=settings.mpc.budget_ms,
            monotonic_ms=ScriptedClock(0),
        )


def test_the_whole_chain_hands_a_bounded_request_to_the_guard(trained) -> None:
    """健全な tick では **Gate が Learned MPC を選び、要求は制約の中に収まる**。

    ここで作れるのは `requested` までで、この先の Reactive Guard（#80）と
    Critical Safety（#78）は毎 tick 必ず掛かる。
    """
    base, profile = trained
    settings = mpc_policy(max_step_up=_provisional(0.1))
    controller, _model, _ = build_controller(trained, policy_config=settings, acoustic=True)
    result = propose(
        controller,
        residual=evidence(profile, 0.5, 10, at=ACTION_TS_MS),
        safety_floor=0.3,
    )
    assert result.proposal is not None
    assert result.assessment is not None
    assert result.assessment.ood is False

    gate = ControllerGate(settings, expected_model_version=base.manifest.model_version)
    # 復帰 hold を満たすため、健全なまま2 tick 進める（#79）。
    for now_mono_ms in (0, settings.recovery_hold_ms):
        selection = gate.select(
            now_mono_ms=now_mono_ms,
            fallback=fallback_proposal(0.4),
            learned=result.to_status(received_at_mono_ms=now_mono_ms),
            operating_mode=OperatingMode.AUTO,
            safety_state=SafetyState.NORMAL,
        )

    assert selection.active_controller is ControllerKind.LEARNED_MPC
    assert selection.model_gate is not None
    assert selection.model_gate.learned_selected is True

    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.3),
        current=demands(
            _mean(tuple(observed_input().action.get(zone).effective_demand for zone in Zone))
        ),
    )
    for zone in Zone:
        demand = selection.proposal.requested.get(zone).demand
        assert demand >= 0.3
        lower, upper = constraints.window(zone)
        assert lower <= demand <= upper


# ---------------------------------------------------------------- 目的関数の項


class StubAirBalance:
    """比を返す / 返さないだけを切り替える試験用 Air Balance Model。"""

    def __init__(self, ratio: float | None) -> None:
        self._ratio = ratio
        self.calls = 0

    def evaluate(self, demands: PerZone[Demand], thermal: ThermalInputs) -> AirBalanceEstimate:
        """比と状態だけを持つ最小の評価結果を返す。"""
        del demands, thermal
        self.calls += 1
        known = self._ratio is not None
        return AirBalanceEstimate(
            q_front=1.0 if known else None,
            q_rear=0.5 if known else None,
            q_top=(self._ratio - 0.5) if self._ratio is not None else None,
            estimated_intake=1.0 if known else None,
            estimated_exhaust=self._ratio if known else None,
            balance_ratio=self._ratio,
            state=AirBalanceState.BALANCED if known else AirBalanceState.UNKNOWN,
            thermal_limited=False,
            thermal_reasons=(),
            metadata=AirBalanceMetadata(
                model_id="stub",
                source=CharacterizationSource(status="uncalibrated", basis="試験用"),
                config_sha256="e" * 64,
            ),
        )

    def coordinate(self, demands, thermal, *, projected_top_floor=None):  # type: ignore[no-untyped-def]
        """MPC は使わない。"""
        raise NotImplementedError


def _balance_band() -> BalanceBand:
    return BalanceBand(target_ratio=1.0, minimum_ratio=0.8, maximum_ratio=1.2)


def test_an_unknown_air_balance_ratio_is_not_treated_as_a_good_one(trained) -> None:
    """**比を推定できない step を「釣り合っている」と読み替えない。**

    設定した `unknown_balance_cost` を課し、未知が最適解に見えないようにする。
    """
    settings = mpc_policy()
    prediction = _plan_prediction(trained, settings)
    weights = supervisor_output().weights.model_copy(update={"balance": 1.0})

    unknown = MpcCostModel(
        settings.mpc.optimizer,
        air_balance=StubAirBalance(None),
        balance_band=_balance_band(),
    ).evaluate(
        plan=prediction[0],
        prediction=prediction[1],
        weights=weights,
        target_band=supervisor_output().target_band,
        previous=demands(0.45),
    )
    on_target = MpcCostModel(
        settings.mpc.optimizer,
        air_balance=StubAirBalance(1.0),
        balance_band=_balance_band(),
    ).evaluate(
        plan=prediction[0],
        prediction=prediction[1],
        weights=weights,
        target_band=supervisor_output().target_band,
        previous=demands(0.45),
    )

    assert unknown.balance_known_steps == 0
    assert on_target.balance_known_steps == len(prediction[0].steps)
    assert unknown.terms.balance == pytest.approx(settings.mpc.optimizer.unknown_balance_cost.value)
    assert on_target.terms.balance == pytest.approx(0.0)


def test_the_air_balance_target_band_must_come_from_its_own_config(trained) -> None:
    """目標比は #81 の設定が持つ。**MPC 側に写させない。**"""
    del trained
    settings = mpc_policy()
    with pytest.raises(MpcCostUnusableError, match="BalanceBand"):
        MpcCostModel(settings.mpc.optimizer, air_balance=StubAirBalance(1.0))


def test_a_prediction_without_the_cost_metrics_is_refused(trained) -> None:
    """コストに必要な metric が予測に無ければ、**0 で埋めずに失敗する**。"""
    settings = mpc_policy()
    plan, prediction = _plan_prediction(trained, settings)
    stripped = PlanPrediction.model_validate(
        prediction.model_dump(mode="python")
        | {
            "targets": tuple(
                target.model_dump(mode="python") | {"values": {GPU: target.values[GPU]}}
                for target in prediction.targets
            )
        }
    )

    with pytest.raises(MpcCostUnusableError, match=CPU):
        MpcCostModel(settings.mpc.optimizer).evaluate(
            plan=plan,
            prediction=stripped,
            weights=supervisor_output().weights,
            target_band=supervisor_output().target_band,
            previous=demands(0.45),
        )


def _plan_prediction(
    trained: tuple[object, ModelConfidenceProfile],
    settings: FanPolicyConfig,
) -> tuple[ActionPlan, PlanPrediction]:
    """コスト単体の試験に使う plan と、その plan に対する予測。"""
    base, _profile = trained
    model = PlanningModel(base)
    plan = ActionPlan.held(
        demands(0.5),
        step_ms=settings.mpc.optimizer.step_ms.value,
        steps=settings.mpc.optimizer.steps,
    )
    return plan, model.predict_plan(PlannedThermalInput(observed=observed_input(), plan=plan))
