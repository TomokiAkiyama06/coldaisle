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
import json
import tempfile
from collections.abc import Sequence
from functools import cache
from hashlib import sha256
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
from coldaisle.control.config import MAX_MPC_HORIZON_STEPS, FanPolicyConfig, SafetyConfig
from coldaisle.control.fallback import (
    FallbackCause,
    LearnedControlStatus,
    LearnedFailure,
)
from coldaisle.control.model.confidence import (
    ConfidenceAssessor,
    ModelConfidenceProfile,
    ResidualEvidence,
    fit_confidence_profile,
    inference_id,
)
from coldaisle.control.model.thermal import (
    MAX_TARGET_HORIZONS,
    ArtifactVerification,
    InferenceCapability,
    ObservedThermalInput,
    ThermalFeatureSchema,
    ThermalPrediction,
    ThermalTargetSchema,
    canonical_artifact_bytes,
)
from coldaisle.control.model_registry import (
    ApprovalAction,
    ArtifactAttestation,
    ArtifactCapability,
    ArtifactFormat,
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    ArtifactStatus,
    HumanApproval,
    ModelCompatibility,
    ModelRegistry,
    load_model_registry_limits,
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
    StaticAuthorityStage,
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
from test_fallback_controller import fallback_proposal, gate_for, policy
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

type Trained = tuple[object, ModelConfidenceProfile, ArtifactAttestation]
"""学習済み #84 モデル・Confidence Profile・Registry 発行の証拠。"""


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
        model_id: str | None = None,
        model_version: str | None = None,
        verification: ArtifactVerification = ArtifactVerification.REGISTRY_VERIFIED,
        gain: float = 30.0,
        plan_error: Exception | None = None,
        forged_anchor_id: str | None = None,
        forged_offsets: tuple[int, ...] | None = None,
        forged_artifact_sha256: str | None = None,
        forged_anchor_sha256: str | None = None,
        stale_plan: ActionPlan | None = None,
    ) -> None:
        self._base = base
        self._identity = CounterfactualModelIdentity(
            model_id=model_id or base.manifest.model_id,  # type: ignore[attr-defined]
            model_version=(
                model_version or base.manifest.model_version  # type: ignore[attr-defined]
            ),
            capability=capability,
        )
        self._verification = verification
        self._gain = gain
        self._plan_error = plan_error
        self._forged_anchor_id = forged_anchor_id
        self._forged_offsets = forged_offsets
        self._forged_artifact_sha256 = forged_artifact_sha256
        self._forged_anchor_sha256 = forged_anchor_sha256
        self._stale_plan = stale_plan
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
        overrides: dict[str, object] = {"artifact_verification": self._verification}
        if self._forged_anchor_sha256 is not None:
            # 「同じ ID / 版だが別の bytes へ委譲する model」の代役。
            overrides["artifact_sha256"] = self._forged_anchor_sha256
        return ThermalPrediction.model_validate(prediction.model_dump(mode="python") | overrides)

    def predict_plan(self, planned: PlannedThermalInput) -> PlanPrediction:
        """候補 action 列に対する単調な応答を返す。"""
        self.plan_calls += 1
        if self._plan_error is not None:
            raise self._plan_error
        # 別の候補の予測を返す（キャッシュの取り違えの再現）。
        scored = (
            planned
            if self._stale_plan is None
            else PlannedThermalInput(observed=planned.observed, plan=self._stale_plan)
        )
        anchor = self.predict(scored.observed)
        anchor_mean = _mean(
            tuple(scored.observed.action.get(zone).effective_demand for zone in Zone)
        )
        by_offset = {target.horizon_ms: target.values for target in anchor.targets}
        offsets = self._forged_offsets or scored.plan.offsets_ms
        targets = []
        for index, step in enumerate(scored.plan.steps):
            offset = offsets[index]
            delta = _mean(tuple(step.demands.get(zone) for zone in Zone)) - anchor_mean
            weight = (index + 1) / len(scored.plan.steps)
            base_values = by_offset[step.offset_ms]
            targets.append(
                PlannedTarget(
                    offset_ms=offset,
                    expected_ts_ms=scored.observed.action_ts_ms + offset,
                    values={
                        metric: value - self._gain * delta * weight
                        for metric, value in base_values.items()
                    },
                )
            )
        return PlanPrediction(
            model_id=anchor.model_id,
            model_version=anchor.model_version,
            artifact_sha256=self._forged_artifact_sha256 or anchor.artifact_sha256,
            artifact_verification=anchor.artifact_verification,
            capability=InferenceCapability.COUNTERFACTUAL_ACTION,
            anchor_inference_id=(self._forged_anchor_id or inference_id(scored.observed, anchor)),
            input_action_ts_ms=anchor.input_action_ts_ms,
            # **評価した plan そのもの**の識別子を返す。取り違えれば呼び出し側が弾く。
            plan_digest=scored.plan.digest(),
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


REGISTRY_LIMITS = load_model_registry_limits(Path(__file__).resolve().parents[1] / "config")
ALL_STAGES: tuple[AuthorityStage, ...] = tuple(AuthorityStage)


def issue_attestation(
    root: Path,
    *,
    model_id: str = "rack-thermal",
    version: str = "0.1.0",
    authority: tuple[AuthorityStage, ...] = ALL_STAGES,
    feature_schema_version: str = "thermal-features-v1",
    target_schema_version: str = "thermal-targets-v1",
    kind: ArtifactKind = ArtifactKind.THERMAL_MODEL,
    stage: AuthorityStage = AuthorityStage.FULL,
    promoted: bool = True,
    payload: bytes | None = None,
    capability: ArtifactCapability = ArtifactCapability.COUNTERFACTUAL_ACTION,
) -> ArtifactAttestation:
    """**本物の Model Registry（#104）に登録し、検証経路から attestation を受け取る。**

    テストが自分で証拠を組み立てないようにする。ここで登録するのは、反実仮想 artifact 形式が
    #84 に入るまでの置き換えとしての最小の JSON payload で、`ArtifactMetadata` の identity と
    schema version だけが #86 の束縛に効く。
    """
    payload = (
        payload or json.dumps({"model": model_id, "version": version}, sort_keys=True).encode()
    )
    metadata = ArtifactMetadata(
        kind=kind,
        artifact_format=ArtifactFormat.JSON,
        capability=capability,
        model_id=model_id,
        version=version,
        created_at="2026-09-20T10:00:00+09:00",
        training_dataset_version="dataset-00000000000000000000000000000086",
        source_runs=("run-00000000000000000000000000000086",),
        feature_schema_version=feature_schema_version,
        target_schema_version=target_schema_version,
        code_commit="0123456789abcdef",
        sha256=sha256(payload).hexdigest(),
        model_family="ridge_linear_v1",
        hyperparameters={"ridge_lambda": 0.1},
        authority_compatibility=authority,
    )
    registry = ModelRegistry(root, limits=REGISTRY_LIMITS)
    registry.register_candidate(metadata, payload, actor="trainer", reason="training completed")
    registry.mark_validated(
        metadata.ref,
        offline_evaluation_ref="evaluation/offline/86",
        actor="evaluator",
        reason="offline gates passed",
        expected_revision=registry.inspect().revision,
    )
    compatibility = ModelCompatibility(
        feature_schema_version=feature_schema_version,
        target_schema_version=target_schema_version,
        authority_stage=stage,
    )
    if promoted:
        revision = registry.inspect().revision
        registry.promote(
            metadata.ref,
            compatibility,
            shadow_evaluation_ref="evaluation/shadow/86",
            approval=HumanApproval(
                action=ApprovalAction.PROMOTE,
                artifact=metadata.ref,
                artifact_sha256=metadata.sha256,
                expected_revision=revision,
                approver="model-operator",
                approved_at_ms=1_700_000_000_000,
                reason="shadow evaluation passed",
            ),
            expected_revision=revision,
        )
    result = registry.load_version(metadata.ref, compatibility)
    assert result.artifact is not None, result.detail
    return result.artifact.attestation


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory) -> Trained:
    """合成 dataset で学習した #84 モデル、その Confidence Profile、Registry 発行の証拠。"""
    data = dataset(HORIZONS, TARGETS)
    parts = split(data)
    model = train(data, parts)
    profile = fit_confidence_profile(model, data, parts, profile_spec())
    attestation = issue_attestation(
        tmp_path_factory.mktemp("pr151-registry") / "registry",
        model_id=model.manifest.model_id,
        version=model.manifest.model_version,
        # **実際の #84 artifact bytes を登録する。** attestation の artifact hash が
        # anchor 推論と Confidence Profile の hash と一致することまで試験で通す。
        payload=canonical_artifact_bytes(model._artifact),
    )
    return model, profile, attestation


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
    trained: Trained,
    *,
    model: PlanningModel | None = None,
    policy_config: FanPolicyConfig | None = None,
    clock: ScriptedClock | None = None,
    acoustic: bool = False,
    authority_stage: AuthorityStage | None = None,
) -> tuple[LearnedMpcController, PlanningModel, FanPolicyConfig]:
    """束縛済みの MPC controller を組み立てる。"""
    base, profile, attestation = trained
    planning = model or PlanningModel(base)
    settings = policy_config or mpc_policy()
    binding = MpcModelBinding.for_control(
        planning,
        attestation=attestation,
        authority_stage=settings.authority_stage,
        expected_model_version=attestation.version,
    )
    controller = LearnedMpcController(
        binding,
        settings,
        safety(),
        assessor=ConfidenceAssessor(profile, settings.model_confidence),
        monotonic_ms=clock or ScriptedClock(0),
        # #92: 実効 stage は journal が決める。試験では設定の stage をそのまま使う。
        authority=StaticAuthorityStage(authority_stage or settings.authority_stage),
        acoustic=acoustic_model() if acoustic else None,
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
    return controller.propose(
        snapshot=snapshot(ts_ms),
        observed=window,
        supervisor=supervisor_output(),
        baseline=fallback_proposal(baseline),
        safety_floor=demands(safety_floor),
        residual=residual,
    )


# ---------------------------------------------------------------- 不変条件 2: 内部モデル


def test_invariant_2_a_dataset_v1_artifact_is_refused_as_the_internal_model(trained) -> None:
    """**観測再生だけの artifact を MPC の内部モデルにしない**（決定記録 0048 §2.1）。

    いま存在する artifact は manifest の capability が `observational_replay` に固定されている。
    束縛を許すと、後続 action 列を学習していない係数を Fan action の因果効果として使ってしまう。
    """
    base, _profile, _attestation = trained
    identity = CounterfactualModelIdentity.from_manifest(base.manifest)
    assert identity.capability is InferenceCapability.OBSERVATIONAL_REPLAY

    # 実際に Registry へ登録できるのは、いまは observational_replay だけである。
    observational = issue_attestation(
        Path(tempfile.mkdtemp(prefix="pr151-observational")) / "registry",
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        capability=ArtifactCapability.OBSERVATIONAL_REPLAY,
        payload=canonical_artifact_bytes(base._artifact),
    )

    assert observational.capability is ArtifactCapability.OBSERVATIONAL_REPLAY
    with pytest.raises(MpcModelUnusableError, match="反実仮想予測を申告していない"):
        MpcModelBinding.for_control(
            PlanningModel(base, capability=identity.capability),
            attestation=observational,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=observational.version,
        )


def test_invariant_2_b_verification_comes_from_the_registry_not_from_the_model(
    trained, tmp_path: Path
) -> None:
    """**モデルの自称ではなく、Registry が発行した証拠に束縛する。**

    `ArtifactAttestation` は #104 の検証経路だけが発行する。呼び出し側が作れないので、
    検証していない artifact を取り違えて渡す配線ミスは型で止まる（決定記録 0052 §2.1）。
    """
    base, _profile, attestation = trained

    with pytest.raises(TypeError, match="検証経路"):
        ArtifactAttestation()

    # 自称の artifact_verification が OFFLINE でも、Registry の証拠があれば束縛は成立する。
    # 逆に、証拠なしで REGISTRY_VERIFIED を名乗る道はそもそも型として存在しない。
    binding = MpcModelBinding.for_control(
        PlanningModel(base, verification=ArtifactVerification.OFFLINE_UNVERIFIED),
        attestation=attestation,
        authority_stage=AuthorityStage.FULL,
        expected_model_version=attestation.version,
    )

    assert binding.artifact_verification is ArtifactVerification.REGISTRY_VERIFIED
    assert binding.model_version == attestation.version
    assert binding.artifact_sha256 == attestation.artifact_sha256
    del tmp_path


def test_invariant_2_c_a_wrapper_around_another_artifact_is_refused(
    trained, tmp_path: Path
) -> None:
    """**別の artifact を包んだ wrapper が、借りてきた証拠で authority を得られない。**"""
    base, _profile, attestation = trained

    with pytest.raises(MpcModelUnusableError, match="model_id"):
        MpcModelBinding.for_control(
            PlanningModel(base, model_id="other-thermal"),
            attestation=attestation,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=attestation.version,
        )

    # schema version が食い違う model も、証拠だけ借りて通れない。
    other = issue_attestation(
        tmp_path / "other-schema",
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        feature_schema_version="thermal-features-v2",
        target_schema_version="thermal-targets-v2",
    )
    with pytest.raises(MpcModelUnusableError, match="schema_version"):
        MpcModelBinding.for_control(
            PlanningModel(base),
            attestation=other,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=other.version,
        )


def test_invariant_2_d_the_registry_decides_the_permitted_authority(
    trained, tmp_path: Path
) -> None:
    """**authority 互換は Registry metadata の値で判断する。** 自称値では広げられない。"""
    base, _profile, _attestation = trained
    shadow_only = issue_attestation(
        tmp_path / "shadow-only",
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        authority=(AuthorityStage.SHADOW,),
        stage=AuthorityStage.SHADOW,
    )

    with pytest.raises(MpcModelUnusableError, match="authority"):
        MpcModelBinding.for_control(
            PlanningModel(base),
            attestation=shadow_only,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=shadow_only.version,
        )


def test_invariant_2_e_a_non_thermal_artifact_is_refused(trained, tmp_path: Path) -> None:
    """thermal model 以外の artifact の証拠では束縛しない。"""
    base, _profile, _attestation = trained
    other_kind = issue_attestation(
        tmp_path / "supervisor",
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        kind=ArtifactKind.SUPERVISOR_POLICY,
    )

    with pytest.raises(MpcModelUnusableError, match="thermal model"):
        MpcModelBinding.for_control(
            PlanningModel(base),
            attestation=other_kind,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=other_kind.version,
        )


def test_invariant_2_f_a_version_mismatch_is_refused_before_any_inference(trained) -> None:
    """runtime が期待する版と違うモデルで**推論すら始めない**。"""
    _base, _profile, attestation = trained
    with pytest.raises(MpcModelUnusableError, match="版"):
        MpcModelBinding.for_control(
            PlanningModel(_base),
            attestation=attestation,
            authority_stage=AuthorityStage.FULL,
            expected_model_version="9.9.9",
        )


def test_invariant_2_g_a_refused_model_degrades_to_fallback_without_stopping(trained) -> None:
    """束縛できないモデルでも**運転は止まらない**。Gate は理由付きで Fallback にする。"""
    base, _profile, _attestation = trained
    settings = mpc_policy()
    observational = issue_attestation(
        Path(tempfile.mkdtemp(prefix="pr151-degrade")) / "registry",
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        capability=ArtifactCapability.OBSERVATIONAL_REPLAY,
        payload=canonical_artifact_bytes(base._artifact),
    )
    with pytest.raises(MpcModelUnusableError) as refusal:
        MpcModelBinding.for_control(
            PlanningModel(base, capability=InferenceCapability.OBSERVATIONAL_REPLAY),
            attestation=observational,
            authority_stage=settings.authority_stage,
            expected_model_version=observational.version,
        )
    status = MpcProposal(
        failure=LearnedFailure.MODEL_LOAD_FAILURE,
        failure_reason=Reason(code="model_unusable", detail=str(refusal.value)),
    ).to_status(received_at_mono_ms=0)
    gate = gate_for(
        settings,
        expected_model_version=observational.version,
        expected_artifact_sha256=observational.artifact_sha256,
    )

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
    # 不変条件 5（理由を落とさない）: 何が起きたのかが trace に残る。
    assert "model_unusable" in selection.fallback_reason.detail
    assert "反実仮想" in selection.fallback_reason.detail
    assert selection.model_gate is None


def test_invariant_2_h_the_binding_cannot_be_swapped_after_it_is_verified(trained) -> None:
    """検証済みの束を**後から差し替えられない**。検査を1度通せば済む形にしない。"""
    base, _profile, attestation = trained
    binding = MpcModelBinding.for_control(
        PlanningModel(base),
        attestation=attestation,
        authority_stage=AuthorityStage.FULL,
        expected_model_version=attestation.version,
    )

    with pytest.raises(AttributeError):
        binding._model = PlanningModel(base)  # type: ignore[misc]
    with pytest.raises(AttributeError):
        binding.attestation._model_id = "other"  # type: ignore[misc]
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
    base, _profile, _attestation = trained
    controller, _model, settings = build_controller(
        trained, model=PlanningModel(base, forged_anchor_id="9" * 64)
    )

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR

    gate = gate_for(
        settings,
        expected_model_version=base.manifest.model_version,
        expected_artifact_sha256=_attestation.artifact_sha256,
    )
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
    base, _profile, _attestation = trained
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
                "prediction": _flat_prediction(plan),
                "evaluations": 1,
            }
        )


def test_invariant_3_d_a_solution_cannot_carry_another_candidates_prediction() -> None:
    """解に**別の候補 plan の予測**を添えられない（#90 が記録する予測の束縛）。"""
    from coldaisle.control.mpc.optimizer import MpcSolution

    plan = ActionPlan.held(demands(0.4), step_ms=STEP_MS, steps=2)
    other = ActionPlan.held(demands(0.9), step_ms=STEP_MS, steps=2)
    payload = {
        "plan": plan.model_dump(mode="python"),
        "requested": plan.first.model_dump(mode="python"),
        "cost": _flat_cost(1.0),
        "baseline_cost": _flat_cost(2.0),
        "baseline_requested": demands(0.4).model_dump(mode="python"),
        "anchor_inference_id": "a" * 64,
        "evaluations": 1,
    }
    with pytest.raises(ValidationError, match="別の候補 plan"):
        MpcSolution.model_validate(payload | {"prediction": _flat_prediction(other)})
    with pytest.raises(ValidationError, match="別の anchor 推論"):
        MpcSolution.model_validate(
            payload | {"prediction": _flat_prediction(plan, anchor_id="b" * 64)}
        )


def _flat_prediction(plan: ActionPlan, *, anchor_id: str = "a" * 64) -> dict[str, object]:
    return {
        "model_id": "thermal",
        "model_version": "1.0.0",
        "artifact_sha256": "c" * 64,
        "artifact_verification": ArtifactVerification.REGISTRY_VERIFIED,
        "capability": InferenceCapability.COUNTERFACTUAL_ACTION,
        "anchor_inference_id": anchor_id,
        "input_action_ts_ms": 0,
        "plan_digest": plan.digest(),
        "targets": tuple(
            PlannedTarget(
                offset_ms=step.offset_ms,
                expected_ts_ms=step.offset_ms,
                values={"gpu.0.core": 50.0},
            )
            for step in plan.steps
        ),
    }


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
    base, _profile, _attestation = trained
    controller, _model, settings = build_controller(
        trained, clock=ScriptedClock(0, 0, 10_000), policy_config=mpc_policy(budget_ms=10)
    )
    result = propose(controller)
    gate = gate_for(
        settings,
        expected_model_version=base.manifest.model_version,
        expected_artifact_sha256=_attestation.artifact_sha256,
    )

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
    base, _profile, _attestation = trained
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
    base, _profile, _attestation = trained
    controller, _model, settings = build_controller(trained)
    ood_window = shifted(observed_input(), air=200.0)

    result = propose(controller, observed=ood_window)

    assert result.proposal is not None and result.assessment is not None
    assert result.assessment.ood is True
    assert result.proposal.ood is True
    assert result.proposal.confidence == 0.0

    gate = gate_for(
        settings,
        expected_model_version=base.manifest.model_version,
        expected_artifact_sha256=_attestation.artifact_sha256,
    )
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
    base, _profile, _attestation = trained
    settings = mpc_policy(authority="shadow")
    controller, _model, _settings = build_controller(trained, policy_config=settings)
    result = propose(controller)
    gate = gate_for(
        settings,
        expected_model_version=base.manifest.model_version,
        expected_artifact_sha256=_attestation.artifact_sha256,
    )

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
    base, _profile, _attestation = trained
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
    base, _profile, attestation = trained
    settings = mpc_policy(step_ms=_provisional(7_000), horizon_ms=_provisional(7_000))
    binding = MpcModelBinding.for_control(
        PlanningModel(base),
        attestation=attestation,
        authority_stage=settings.authority_stage,
        expected_model_version=attestation.version,
    )

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
    base, _profile, attestation = trained
    settings = mpc_policy(
        cost_metrics={"cpu_temperature": "air.front_intake", "gpu_temperature": GPU}
    )
    binding = MpcModelBinding.for_control(
        PlanningModel(base),
        attestation=attestation,
        authority_stage=settings.authority_stage,
        expected_model_version=attestation.version,
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
    _base, profile, attestation = trained
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

    gate = gate_for(
        settings,
        expected_model_version=attestation.version,
        expected_artifact_sha256=attestation.artifact_sha256,
    )
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

    @property
    def metadata(self) -> AirBalanceMetadata:
        """任意依存を差し替えたことが条件 hash に出るよう、契約どおり出どころを返す。"""
        return AirBalanceMetadata(
            model_id="stub-balance",
            source=CharacterizationSource(status="uncalibrated", basis="試験用"),
            flow_unit="relative",
            config_sha256="e" * 64,
        )

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
    trained: Trained,
    settings: FanPolicyConfig,
) -> tuple[ActionPlan, PlanPrediction]:
    """コスト単体の試験に使う plan と、その plan に対する予測。"""
    base, _profile, _attestation = trained
    model = PlanningModel(base)
    plan = ActionPlan.held(
        demands(0.5),
        step_ms=settings.mpc.optimizer.step_ms.value,
        steps=settings.mpc.optimizer.steps,
    )
    return plan, model.predict_plan(PlannedThermalInput(observed=observed_input(), plan=plan))


# ------------------------------------------- codex レビュー（PR #151）への修正の回帰試験


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("internal feature layout failure"),
        KeyError("gpu.0.core"),
        ZeroDivisionError("division by zero"),
        MemoryError("out of memory"),
    ],
)
def test_any_ordinary_exception_from_the_model_becomes_a_fallback(trained, error) -> None:
    """**種類を問わず、worker 境界の外へ例外を出さない**（codex #4055491980）。

    `ValueError` 系だけを捕まえると、`control/model/thermal.py` が内部の feature layout 異常に
    使う `RuntimeError` などが素通りして制御ループごと死ぬ。
    """
    base, _profile, _attestation = trained
    controller, _model, _settings = build_controller(
        trained, model=PlanningModel(base, plan_error=error)
    )

    result = propose(controller)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert result.failure_reason.code == _expected_reason_code(type(error).__name__)


def _expected_reason_code(name: str) -> str:
    lowered = "".join(
        f"_{character.lower()}" if character.isupper() else character for character in name
    ).strip("_")
    return lowered


@pytest.mark.parametrize("escape", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_still_propagate_from_the_worker(trained, escape) -> None:
    """**停止の合図は握りつぶさない。** `BaseException` は worker 境界を素通りする。"""
    base, _profile, _attestation = trained
    controller, _model, _settings = build_controller(
        trained, model=PlanningModel(base, plan_error=escape())
    )

    with pytest.raises(escape):
        propose(controller)


def test_the_budget_is_rechecked_after_the_baseline_evaluation(trained) -> None:
    """**Baseline の評価だけで予算を使い切った tick を OK にしない**（codex #4055491984）。

    候補が1つも無い window では、evaluation を数える打ち切りにも掛からない。
    """
    controller, _model, _settings = build_controller(
        trained,
        clock=ScriptedClock(0, 10_000),
        policy_config=mpc_policy(budget_ms=10),
    )

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.TIMEOUT


def test_a_ramp_down_bound_above_the_ceiling_is_infeasible_not_widened(trained) -> None:
    """**設定した探索 ceiling を MPC 自身が緩めない**（codex #4055491988）。

    forced Max の直後のように直前値が ceiling より上だと、1 step では ceiling まで下げきれない。
    ここを `[0.9, 0.9]` のような ceiling 超えの単元集合にすると、上限を自分で広げたことになる。
    """
    del trained
    settings = mpc_policy(
        max_step_down=_provisional(0.1),
        zone_bounds={
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(0.5)} for zone in Zone
        },
    )

    with pytest.raises(InfeasiblePlanError, match="ceiling"):
        HardConstraintSet.build(
            optimizer=settings.mpc.optimizer,
            safety=safety(),
            safety_floor=demands(0.2),
            current=demands(1.0),
        )


def test_a_floor_above_the_step_up_limit_is_still_allowed(trained) -> None:
    """対になる向き（floor が上げ幅に勝つ）は**実行不能ではない**。floor に合わせる。"""
    del trained
    settings = mpc_policy(max_step_up=_provisional(0.01), max_step_down=_provisional(0.5))
    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.9),
        current=demands(0.3),
    )

    assert constraints.window(Zone.FRONT) == (pytest.approx(0.9), pytest.approx(0.9))


def test_the_worker_failure_detail_reaches_the_fallback_trace(trained) -> None:
    """**理由を trace の手前で落とさない**（codex #4055491991）。"""
    base, _profile, attestation = trained
    settings = mpc_policy()
    controller, _model, _ = build_controller(
        trained,
        model=PlanningModel(base, plan_error=RuntimeError("feature layout broken")),
        policy_config=settings,
    )
    result = propose(controller)
    gate = gate_for(
        settings,
        expected_model_version=attestation.version,
        expected_artifact_sha256=attestation.artifact_sha256,
    )

    selection = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=result.to_status(received_at_mono_ms=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OPTIMIZER_EXCEPTION.value
    assert "runtime_error" in selection.fallback_reason.detail
    assert "feature layout broken" in selection.fallback_reason.detail
    metadata = selection.trace_metadata()["fallback_reason"]
    assert isinstance(metadata, dict)
    assert "feature layout broken" in str(metadata["detail"])


def test_a_status_without_a_failure_cannot_carry_a_failure_reason(trained) -> None:
    """失敗していない状態に理由だけを付けられない（trace の読み違いを防ぐ）。"""
    del trained
    with pytest.raises(ValidationError, match="failure_reason"):
        LearnedControlStatus(failure_reason=Reason(code="made_up"))


def test_a_plan_prediction_from_another_artifact_is_rejected(trained) -> None:
    """**anchor と別の artifact の予測でコストを測らない。**

    版が同じでも、別の bytes の予測を混ぜれば #85 の判定は別の推論に付け替わる。
    """
    base, _profile, _attestation = trained
    controller, _model, _settings = build_controller(
        trained, model=PlanningModel(base, forged_artifact_sha256="b" * 64)
    )

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR


@pytest.mark.parametrize("promoted", [False, True])
def test_only_the_production_pointer_can_be_bound_for_active_control(
    trained, tmp_path: Path, promoted: bool
) -> None:
    """**promotion を経ていない artifact に制御権を渡さない**（codex #4055572072）。

    `load_version()` は Replay / offline 評価のために候補・検証済み・引退も返す。その結果を
    そのまま controller へ配線できると、人の承認を経ずに active authority を得てしまう。
    """
    base, _profile, _attestation = trained
    attestation = issue_attestation(
        tmp_path / ("promoted" if promoted else "pinned"),
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        promoted=promoted,
    )

    assert attestation.production_active is promoted
    if promoted:
        assert attestation.status is ArtifactStatus.PRODUCTION
        binding = MpcModelBinding.for_control(
            PlanningModel(base),
            attestation=attestation,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=attestation.version,
        )
        assert binding.attestation.production_active is True
        return

    assert attestation.status is ArtifactStatus.VALIDATED
    with pytest.raises(MpcModelUnusableError, match="production pointer"):
        MpcModelBinding.for_control(
            PlanningModel(base),
            attestation=attestation,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=attestation.version,
        )


def test_a_retired_artifact_pinned_by_version_is_refused(trained, tmp_path: Path) -> None:
    """rollback などで引退した artifact も、version 固定で制御へ戻せない。"""
    base, _profile, _attestation = trained
    root = tmp_path / "retired"
    old = issue_attestation(
        root,
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
    )
    assert old.production_active is True

    # 次の版を promote すると、前の版は production pointer ではなくなる。
    newer = issue_attestation(
        root,
        model_id=base.manifest.model_id,
        version="0.2.0",
    )
    registry = ModelRegistry(root, limits=REGISTRY_LIMITS)
    stale = registry.load_version(
        ArtifactRef(
            kind=ArtifactKind.THERMAL_MODEL,
            model_id=base.manifest.model_id,
            version=base.manifest.model_version,
        ),
        ModelCompatibility(
            feature_schema_version="thermal-features-v1",
            target_schema_version="thermal-targets-v1",
            authority_stage=AuthorityStage.FULL,
        ),
    )

    assert newer.production_active is True
    assert stale.artifact is not None
    assert stale.artifact.attestation.production_active is False
    with pytest.raises(MpcModelUnusableError, match="production pointer"):
        MpcModelBinding.for_control(
            PlanningModel(base),
            attestation=stale.artifact.attestation,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=base.manifest.model_version,
        )


def test_a_prediction_for_another_candidate_is_rejected(trained) -> None:
    """**別の候補の予測を今の候補のコストに使わせない**（codex #4055572075）。

    同じ tick の候補は step の刻みが同じなので、時刻だけでは見分けられない。別の demand の
    温度予測に、今の候補の音響・風量・変化コストを足して選ばせてしまう。
    """
    base, _profile, _attestation = trained
    stale = ActionPlan.held(demands(0.95), step_ms=STEP_MS, steps=len(HORIZONS))
    controller, _model, settings = build_controller(
        trained, model=PlanningModel(base, stale_plan=stale)
    )

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR

    gate = gate_for(
        settings,
        expected_model_version=_attestation.version,
        expected_artifact_sha256=_attestation.artifact_sha256,
    )
    selection = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=result.to_status(received_at_mono_ms=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    assert selection.active_controller is ControllerKind.FALLBACK


def test_the_plan_digest_covers_the_per_zone_demands() -> None:
    """plan の識別子は zone ごとの demand まで含む（offset だけでは足りない）。"""
    first = ActionPlan.held(demands(0.4), step_ms=STEP_MS, steps=2)
    second = ActionPlan.held(demands(0.5), step_ms=STEP_MS, steps=2)
    zoned = ActionPlan(
        step_ms=STEP_MS,
        steps=tuple(
            PlanStep(
                offset_ms=STEP_MS * (index + 1),
                demands=PerZone[Demand](front=0.4, rear=0.4, top=0.5),
            )
            for index in range(2)
        ),
    )

    assert first.offsets_ms == second.offsets_ms == zoned.offsets_ms
    assert len({first.digest(), second.digest(), zoned.digest()}) == 3
    assert first.digest() == ActionPlan.held(demands(0.4), step_ms=STEP_MS, steps=2).digest()


def test_an_expired_tick_never_spends_another_model_evaluation(trained) -> None:
    """**予算を使い切った tick で、さらに1回モデルを回さない**（codex #4055572079）。

    anchor 推論と Confidence 判定だけで予算を越えた場合、`predict_plan()` を1度も呼ばずに
    `TIMEOUT` を返す。
    """
    base, _profile, _attestation = trained
    model = PlanningModel(base)
    controller, _model, _settings = build_controller(
        trained,
        model=model,
        clock=ScriptedClock(0, 10_000),
        policy_config=mpc_policy(budget_ms=10),
    )

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.TIMEOUT
    assert model.plan_calls == 0


def test_a_model_delegating_to_other_bytes_is_refused(trained) -> None:
    """**ID と版が同じでも、別の bytes へ委譲する model の推論は使わない**（codex #4055635586）。

    Registry が承認したのは特定の bytes である。ID と版と schema version だけを合わせた
    別 artifact が、production の証拠の下で提案を出せてはならない。
    """
    base, _profile, attestation = trained
    controller, _model, settings = build_controller(
        trained, model=PlanningModel(base, forged_anchor_sha256="c" * 64)
    )

    result = propose(controller)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "artifact_sha256" in result.failure_reason.detail

    gate = gate_for(
        settings,
        expected_model_version=attestation.version,
        expected_artifact_sha256=attestation.artifact_sha256,
    )
    selection = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=result.to_status(received_at_mono_ms=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    assert selection.active_controller is ControllerKind.FALLBACK
    assert "artifact_sha256" in selection.fallback_reason.detail  # type: ignore[union-attr]


def test_an_anchor_from_another_model_version_is_refused(trained, tmp_path: Path) -> None:
    """anchor の model 版が Registry の証拠と違えば、判定を付ける前に落とす。"""
    base, profile, _attestation = trained
    attestation = issue_attestation(
        tmp_path / "other-version",
        model_id=base.manifest.model_id,
        version="0.2.0",
        payload=canonical_artifact_bytes(base._artifact),
    )
    binding = MpcModelBinding.for_control(
        PlanningModel(base, model_version="0.2.0"),
        attestation=attestation,
        authority_stage=AuthorityStage.FULL,
        expected_model_version=attestation.version,
    )
    settings = mpc_policy()
    # Profile は 0.1.0 に対して作ったので、生成時の照合で落ちる（tick ごとに失敗させない）。
    with pytest.raises(MpcModelUnusableError, match="Confidence Profile"):
        LearnedMpcController(
            binding,
            settings,
            safety(),
            assessor=ConfidenceAssessor(profile, settings.model_confidence),
            monotonic_ms=ScriptedClock(0),
            authority=StaticAuthorityStage(AuthorityStage.FULL),
        )


def test_the_binding_must_cover_the_effective_authority_stage(trained) -> None:
    """**Registry が SHADOW だけを許した artifact を、より高い実効 stage で動かさない。**

    照合の相手は設定の `authority_stage` ではなく**実効 stage**（#92 / 0057 §2.2、
    codex #4056864031）。設定は v9 から上限なので、上限と照合すると
    「journal は SHADOW、上限は LIMITED」の初回昇格で SHADOW 互換の artifact が拒まれ、
    新しい設定での証拠を1件も集められなくなる。
    """
    base, profile, attestation = trained
    shadow_binding = MpcModelBinding.for_control(
        PlanningModel(base),
        attestation=attestation,
        authority_stage=AuthorityStage.SHADOW,
        expected_model_version=attestation.version,
    )
    full_policy = mpc_policy(authority="full")

    with pytest.raises(MpcModelUnusableError, match="authority stage"):
        LearnedMpcController(
            shadow_binding,
            full_policy,
            safety(),
            assessor=ConfidenceAssessor(profile, full_policy.model_confidence),
            monotonic_ms=ScriptedClock(0),
            authority=StaticAuthorityStage(AuthorityStage.FULL),
        )

    # **上限が LIMITED でも、journal が SHADOW なら SHADOW 互換の artifact は動く。**
    # ここが通らないと、昇格に要る証拠を集める運転そのものが始められない。
    limited_ceiling = mpc_policy(authority="limited")
    controller = LearnedMpcController(
        shadow_binding,
        limited_ceiling,
        safety(),
        assessor=ConfidenceAssessor(profile, limited_ceiling.model_confidence),
        monotonic_ms=ScriptedClock(0),
        authority=StaticAuthorityStage(AuthorityStage.SHADOW),
    )
    assert propose(controller).proposal is not None


def test_a_raised_stage_stops_a_binding_that_no_longer_covers_it(trained) -> None:
    """**昇格のあと、束縛の覆っていない stage で提案を出し続けない。**

    worker は生成時にしか照合しないと、`AuthorityStore` が stage を上げた瞬間から
    「検証していない authority で作られた提案」を Gate へ渡すことになる。tick ごとに見る。
    """
    base, profile, attestation = trained
    binding = MpcModelBinding.for_control(
        PlanningModel(base),
        attestation=attestation,
        authority_stage=AuthorityStage.SHADOW,
        expected_model_version=attestation.version,
    )
    settings = mpc_policy(authority="limited")

    class Rising:
        """途中で昇格した journal を模す。"""

        def __init__(self) -> None:
            self.stage = AuthorityStage.SHADOW

        def current_stage(self) -> AuthorityStage:
            return self.stage

    authority = Rising()
    controller = LearnedMpcController(
        binding,
        settings,
        safety(),
        assessor=ConfidenceAssessor(profile, settings.model_confidence),
        monotonic_ms=ScriptedClock(0),
        authority=authority,
    )
    assert propose(controller).proposal is not None

    authority.stage = AuthorityStage.LIMITED
    result = propose(controller)

    assert result.proposal is None
    assert result.failure is LearnedFailure.MODEL_LOAD_FAILURE
    assert result.failure_reason is not None
    assert "authority" in result.failure_reason.detail


def test_a_confidence_assessor_from_another_policy_is_refused(trained) -> None:
    """閾値だけがすり替わった判定器を受け取らない（同じ種類の取り違え）。"""
    base, profile, attestation = trained
    settings = mpc_policy()
    other = settings.model_confidence.model_copy(
        update={
            "high_min_confidence": settings.model_confidence.high_min_confidence.model_copy(
                update={"value": 0.99}
            )
        }
    )
    binding = MpcModelBinding.for_control(
        PlanningModel(base),
        attestation=attestation,
        authority_stage=settings.authority_stage,
        expected_model_version=attestation.version,
    )

    with pytest.raises(MpcModelUnusableError, match="Confidence 判定器"):
        LearnedMpcController(
            binding,
            settings,
            safety(),
            assessor=ConfidenceAssessor(profile, other),
            monotonic_ms=ScriptedClock(0),
            authority=StaticAuthorityStage(settings.authority_stage),
        )


ATTESTATION_FIELD_CHECKS: dict[str, str] = {
    "kind": "for_control: thermal_model 以外を拒む",
    "capability": "for_control: 反実仮想を申告していない artifact を拒む / 自称と照合",
    "model_id": "for_control: identity と照合 / _check_anchor: anchor と照合",
    "version": "for_control: identity と期待版 / _check_anchor / _check_prediction",
    "artifact_sha256": "_check_anchor: anchor と照合 / 生成時: Confidence Profile と照合",
    "feature_schema_version": "for_control: model.feature_schema と照合",
    "target_schema_version": "for_control: model.target_schema と照合",
    "authority_compatibility": "for_control: 要求 stage が含まれるか",
    "status": "for_control: production_active と一緒に判断",
    "production_active": "for_control: production pointer 以外を拒む",
    "model_version": "trace 用の派生値（model_id@version）。個別の照合は上の2つ",
    "registry_revision": "trace のみ。推論時に対応する申告が無い",
    "trace_metadata": "trace 出力（#82）",
}
"""attestation が持つ値ごとに、**どこで模型の申告と突き合わせているか**。

新しい値を #104 が足したときに、照合を書き忘れたまま通らないようにする。
"""


def test_every_attested_value_has_a_place_where_it_is_compared() -> None:
    """**証拠に載っている値を、照合しないまま増やさない。**

    attestation の公開項目が増えたらこの試験が落ちる。落ちたら、その値を推論時の申告と
    突き合わせる場所を決めてから表に足す（決定記録 0052 §2.1）。
    """
    public = {name for name in dir(ArtifactAttestation) if not name.startswith("_")}

    assert public == set(ATTESTATION_FIELD_CHECKS)


def test_the_policy_values_with_a_binding_counterpart_are_compared(trained) -> None:
    """運転設定と束縛の対応を、生成時にすべて突き合わせていること。

    - **実効 authority stage**（#92。設定の `authority_stage` は上限）↔ `binding.authority_stage`
    - `model_confidence` ↔ 判定器の設定
    - `mpc.optimizer` の horizon / step / cost_metrics ↔ model の target schema
    """
    base, profile, attestation = trained
    settings = mpc_policy()
    binding = MpcModelBinding.for_control(
        PlanningModel(base),
        attestation=attestation,
        authority_stage=settings.authority_stage,
        expected_model_version=attestation.version,
    )

    def build(
        policy_config: FanPolicyConfig,
        *,
        stage: AuthorityStage | None = None,
    ) -> LearnedMpcController:
        return LearnedMpcController(
            binding,
            policy_config,
            safety(),
            assessor=ConfidenceAssessor(profile, policy_config.model_confidence),
            monotonic_ms=ScriptedClock(0),
            authority=StaticAuthorityStage(stage or policy_config.authority_stage),
        )

    assert build(settings) is not None
    # 束縛は settings の stage（full）。低い実効 stage は通り、覆えない stage は拒む。
    assert build(mpc_policy(authority="shadow"), stage=AuthorityStage.SHADOW) is not None
    shadow_binding = MpcModelBinding.for_control(
        PlanningModel(base),
        attestation=attestation,
        authority_stage=AuthorityStage.SHADOW,
        expected_model_version=attestation.version,
    )
    with pytest.raises(MpcModelUnusableError, match="authority stage"):
        LearnedMpcController(
            shadow_binding,
            settings,
            safety(),
            assessor=ConfidenceAssessor(profile, settings.model_confidence),
            monotonic_ms=ScriptedClock(0),
            authority=StaticAuthorityStage(AuthorityStage.EXPANDED),
        )
    with pytest.raises(MpcModelUnusableError, match="horizon"):
        build(mpc_policy(step_ms=_provisional(7_000), horizon_ms=_provisional(7_000)))
    with pytest.raises(MpcModelUnusableError, match="metric"):
        build(
            mpc_policy(cost_metrics={"cpu_temperature": "air.front_intake", "gpu_temperature": GPU})
        )


def test_the_capability_comes_from_the_registry_not_from_the_model(trained, tmp_path: Path) -> None:
    """**自称の capability では束縛できない**（codex #4055686513）。

    #104 の metadata と attestation が capability を持つようになったので、推論器がいくら
    `counterfactual_action` を名乗っても、登録時の申告が `observational_replay` なら拒否される。
    現行の #84 artifact はすべてこちらなので、**いまは常に拒否されるのが期待どおりの結果**である。
    """
    base, _profile, _attestation = trained
    observational = issue_attestation(
        tmp_path / "observational",
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        capability=ArtifactCapability.OBSERVATIONAL_REPLAY,
        payload=canonical_artifact_bytes(base._artifact),
    )

    # 自称だけ counterfactual に書き換えても通らない。
    with pytest.raises(MpcModelUnusableError, match="反実仮想予測を申告していない"):
        MpcModelBinding.for_control(
            PlanningModel(base, capability=InferenceCapability.COUNTERFACTUAL_ACTION),
            attestation=observational,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=observational.version,
        )


def test_a_model_contradicting_the_attested_capability_is_refused(trained, tmp_path: Path) -> None:
    """attested と自称が食い違う model も拒む（どちらが正かを黙って決めない）。"""
    base, _profile, _attestation = trained
    counterfactual = issue_attestation(
        tmp_path / "counterfactual",
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        capability=ArtifactCapability.COUNTERFACTUAL_ACTION,
        payload=canonical_artifact_bytes(base._artifact),
    )

    with pytest.raises(MpcModelUnusableError, match="食い違っている"):
        MpcModelBinding.for_control(
            PlanningModel(base, capability=InferenceCapability.OBSERVATIONAL_REPLAY),
            attestation=counterfactual,
            authority_stage=AuthorityStage.FULL,
            expected_model_version=counterfactual.version,
        )


def test_the_configured_step_limit_cannot_exceed_the_prediction_contract() -> None:
    """**設定の上限を、予測の契約より大きくしない**（codex #4055686520）。

    内部モデルの target schema と plan prediction は 32 horizon までしか表現できない。
    設定だけが 64 step を通すと、検証に通っても決して動かない組み合わせを作れてしまう。
    """
    assert MAX_MPC_HORIZON_STEPS <= MAX_TARGET_HORIZONS
    assert MAX_MPC_HORIZON_STEPS == 32

    at_limit = mpc_policy(
        step_ms=_provisional(1_000),
        horizon_ms=_provisional(1_000 * MAX_MPC_HORIZON_STEPS),
    )
    assert at_limit.mpc.optimizer.steps == MAX_MPC_HORIZON_STEPS
    assert len(ActionPlan.held(demands(0.4), step_ms=1_000, steps=MAX_MPC_HORIZON_STEPS).steps) == (
        MAX_MPC_HORIZON_STEPS
    )

    with pytest.raises(ValidationError, match="control step"):
        mpc_policy(
            step_ms=_provisional(1_000),
            horizon_ms=_provisional(1_000 * (MAX_MPC_HORIZON_STEPS + 1)),
        )


def test_the_rate_limit_origin_comes_from_the_observed_action(trained) -> None:
    """**変化幅の起点を呼び出し側から受け取らない**（codex #4055749781）。

    anchor が見ている action と違う値を渡せると、rate limit と変化コストだけが別の前提で
    計算される。起点は観測 window の action（= いま実際に掛かっている effective demand）から取る。
    """
    import inspect

    controller, _model, settings = build_controller(trained)
    signature = inspect.signature(controller.propose)

    # 渡す口が無いこと自体を固定する（将来また受け取り始めたら落ちる）。
    assert "current_demand" not in signature.parameters

    applied = 0.45
    window = observed_input(applied)
    result = propose(controller, observed=window, safety_floor=0.2)
    assert result.solution is not None

    # 起点が window の action なら、許される範囲は applied を中心にした帯になる。
    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.2),
        current=demands(applied),
    )
    for zone in Zone:
        lower, upper = constraints.window(zone)
        assert lower <= result.solution.requested.get(zone) <= upper
    assert constraints.window(Zone.FRONT)[0] == pytest.approx(applied - 0.1)


def test_a_window_action_outside_the_search_bounds_fails_closed(trained) -> None:
    """window の action が探索 ceiling より上なら、勝手に広げず実行不能にする。

    起点を window から取るので、`HardConstraintSet` の交わりの規則がそのまま効く。
    """
    base, _profile, _attestation = trained
    settings = mpc_policy(
        max_step_down=_provisional(0.1),
        zone_bounds={
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(0.5)} for zone in Zone
        },
    )
    controller, _model, _ = build_controller(trained, policy_config=settings)
    del base

    result = propose(controller, observed=observed_input(1.0), safety_floor=0.2)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "ceiling" in result.failure_reason.detail
