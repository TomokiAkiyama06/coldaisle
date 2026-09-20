"""#105 RL Supervisor 学習環境。実機不要（合成 dataset / 試験用モデル / 近似 simulator）。

**ここでは「守れているか」ではなく「破れないか」を試す。** 学習・評価の経路で成り立って
いなければならない不変条件を並べ、1つずつ破ろうとする試験を置く。

1. action は **Supervisor の戦略まで**で、Fan Demand を表現できない
2. `control/rl` は Guard / Safety / Hardware / serial / subprocess へ **到達できない**
3. action から demand を作るのは **MPC と Gate だけ**。環境は独自に demand を作らない
4. 設定範囲外の action は **丸めず terminal / invalid**
5. Critical Safety 違反に相当する state / action は **terminal**（探索を続けない）
6. 近似 simulator は **Registry の証拠を名乗れない**。反実仮想 artifact は1つも存在しない
7. 記録済み trajectory では、**記録に無い action の結果を作らない**（coverage になる）
8. coverage が下限に満たない episode は **比較に使わない**（fail closed）
9. 同じ条件・同じ seed からは **同じ結果**。条件が変われば条件 hash が変わる
10. Rule と RL を **同じ episode 群**で比べられる。条件の違う arm は混ぜない
11. reward の採点基準は **設定が持つ**。action の target band では採点しない
12. 安全は reward の項にできない。比較は **辞書式**で安全が先に立つ
13. 環境は **壁時計を持たない**
"""

from __future__ import annotations

import ast
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control.config import FanPolicyConfig, SafetyConfig
from coldaisle.control.fallback import FallbackController
from coldaisle.control.model.thermal import (
    ArtifactVerification,
    InferenceCapability,
    ObservedThermalInput,
    canonical_artifact_bytes,
)
from coldaisle.control.model_registry import ArtifactCapability
from coldaisle.control.rl import (
    ActionSpace,
    AttestedThermalDynamics,
    DependencyIdentity,
    DynamicsIdentity,
    DynamicsProvenance,
    DynamicsStep,
    DynamicsUnusableError,
    EnvironmentUsageError,
    EpisodeResult,
    EpisodeSpec,
    HybridDynamics,
    InvalidSupervisorActionError,
    LoggedFrame,
    LoggedTrajectory,
    LoggedTrajectoryDynamics,
    PolicyArm,
    RlTrainingConfig,
    SafetyModel,
    SimulatedThermalDynamics,
    SupervisorAction,
    SupervisorTrainingEnvironment,
    TerminationReason,
    TrainingMode,
    WorkloadSample,
    WorkloadTrace,
    attested_evidence,
)
from coldaisle.control.schema import (
    ConfidenceLevel,
    ControllerKind,
    ControllerProposal,
    Demand,
    OptimizerStatus,
    PerZone,
    Reason,
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    TemperatureTarget,
    WorkloadRegime,
    Zone,
    ZoneRequest,
)
from coldaisle.control.state import ControlStateSnapshot
from coldaisle.control.supervisor import RulePolicy, SupervisorInput
from test_learned_mpc import (
    ACTION_TS_MS,
    HORIZONS,
    STEP_MS,
    TARGETS,
    PlanningModel,
    ScriptedClock,
    build_controller,
    issue_attestation,
    observed_input,
)
from test_learned_mpc import safety as mpc_safety
from test_model_confidence import AIR, GPU, dataset, fit_confidence_profile, profile_spec
from test_model_confidence import split as split_dataset
from test_model_confidence import train as train_model

RL_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control" / "rl"

WINDOW_METRICS = (AIR, GPU)
"""合成 dataset の feature metric。環境の dynamics はこの2つを進める。

`AIR` は本番の `cpu.package` に相当する位置づけで使う（合成 dataset に CPU の feature が
無いため）。**実測の意味づけではない。**
"""


# ---------------------------------------------------------------- 下ごしらえ


def provisional(value: float | int) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def rl_document(**overrides: Any) -> dict[str, Any]:
    """試験用の `rl-training.yaml` 相当。値はすべて暫定扱い。"""
    document: dict[str, Any] = {
        "schema_version": 1,
        "episode": {
            # dataset の sample_period（1秒）と揃える。window の刻みを崩さない。
            "step_ms": provisional(1_000),
            "max_steps": 6,
            "recent_history_steps": 2,
        },
        "reward": {
            "version": "reward-test-v1",
            "metrics": {"cpu_temperature": AIR, "gpu_temperature": GPU},
            "scales": {
                "temperature_c": provisional(5.0),
                "acoustic_cost": provisional(10.0),
                "demand_change": provisional(0.5),
            },
            "weights": {
                "cpu_temperature": provisional(0.2),
                "gpu_temperature": provisional(1.0),
                "acoustic": provisional(0.0),
                "change": provisional(0.1),
            },
            "target_band": {
                "cpu_temperature": {"lower_c": 18.0, "upper_c": 24.0},
                "gpu_temperature": {"lower_c": 40.0, "upper_c": 45.0},
            },
            "discount": provisional(0.99),
        },
        "coverage": {
            "minimum_supported_fraction": provisional(0.9),
            "minimum_steps": provisional(3),
        },
        "safety_screen": {"temperature_metrics": [AIR, GPU]},
        "simulator": {
            "model_id": "rl-sim-test",
            "model_version": "0.1.0",
            "responses": [
                simulator_response(AIR, ambient=20.0, gain=6.0),
                simulator_response(GPU, ambient=40.0, gain=12.0),
            ],
        },
    }
    document.update(overrides)
    return document


def simulator_response(
    metric: str,
    *,
    ambient: float,
    gain: float,
    time_constant_ms: int = 30_000,
    noise: float = 0.0,
) -> dict[str, object]:
    return {
        "metric": metric,
        "ambient_c": provisional(ambient),
        "load_gain_c": provisional(gain),
        "flow_floor": provisional(0.5),
        "flow_gain": provisional(1.0),
        "time_constant_ms": provisional(time_constant_ms),
        "noise_sigma_c": provisional(noise),
    }


def rl_config(**overrides: Any) -> tuple[RlTrainingConfig, str]:
    """設定と、その bytes の hash（条件 hash に入る）。"""
    document = rl_document(**overrides)
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return RlTrainingConfig.model_validate(document), sha256(encoded).hexdigest()


class ConstantBaseline:
    """決定論的な Fallback 代役。`BaselineProposer` を構造的に満たす。

    本物の `FallbackController` も同じ契約を満たすことは別の試験で確かめる。ここで代役を
    使うのは、**floor を下回る Baseline**という壊し方を作れるようにするためである。
    """

    def __init__(self, demand: float) -> None:
        self._demand = demand

    def propose(self, snapshot: ControlStateSnapshot) -> ControllerProposal:
        """Snapshot の tick に紐づく Fallback 提案を返す。"""
        request = ZoneRequest(demand=self._demand, reason=Reason(code="baseline_constant"))
        return ControllerProposal(
            controller=ControllerKind.FALLBACK,
            seq=snapshot.tick_id,
            computed_at_ms=snapshot.ts_ms,
            requested=PerZone[ZoneRequest](front=request, rear=request, top=request),
        )


class StubRlPolicy:
    """`SupervisorPolicy` を満たす試験用の RL policy。**Demand を返さない。**"""

    kind = SupervisorPolicyKind.RL

    def __init__(self, weights: SupervisorObjectiveWeights, *, version: str = "rl-test-1") -> None:
        self._weights = weights
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
        snapshot = policy_input.snapshot
        return SupervisorOutput(
            snapshot_schema_version=snapshot.schema_version,
            tick_id=snapshot.tick_id,
            ts_ms=snapshot.ts_ms,
            policy=SupervisorPolicyKind.RL,
            version=self._version,
            regime=policy_input.workload.regime,
            regime_confidence=policy_input.workload.confidence,
            weights=self._weights,
            strategy="balanced",
            target_band=SupervisorTargetBand(
                cpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=75.0),
                gpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=78.0),
            ),
            computed_at_ms=snapshot.ts_ms,
        )


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory):
    """合成 dataset で学習した #84 モデル・Confidence Profile・Registry 発行の証拠。"""
    data = dataset(HORIZONS, TARGETS)
    parts = split_dataset(data)
    model = train_model(data, parts)
    profile = fit_confidence_profile(model, data, parts, profile_spec())
    attestation = issue_attestation(
        tmp_path_factory.mktemp("pr105-registry") / "registry",
        model_id=model.manifest.model_id,
        version=model.manifest.model_version,
        payload=canonical_artifact_bytes(model._artifact),
    )
    return model, profile, attestation


def default_action(*, strategy: str = "balanced", gpu_weight: float = 1.0) -> SupervisorAction:
    """設定範囲に収まる action。"""
    return SupervisorAction(
        strategy=strategy,
        weights=SupervisorObjectiveWeights(
            gpu_temperature=gpu_weight,
            cpu_temperature=0.2,
            balance=0.0,
            acoustic=0.2,
            change=0.1,
        ),
        target_band=SupervisorTargetBand(
            cpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=75.0),
            gpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=78.0),
        ),
    )


def workload_trace(steps: int = 8, *, load: float = 0.8) -> WorkloadTrace:
    sample = WorkloadSample(
        regime=WorkloadRegime.SUSTAINED_GPU,
        regime_confidence=0.9,
        cpu_power_w=120.0,
        gpu_power_w=320.0,
        load=load,
    )
    return WorkloadTrace(trace_id="pr105-trace", step_ms=1_000, samples=(sample,) * steps)


def episode_spec(
    *,
    episode_id: str = "pr105-a",
    seed: int = 7,
    mode: TrainingMode = TrainingMode.LEARNED_SIMULATOR,
    steps: int = 8,
    max_steps: int | None = 4,
    demand: float = 0.4,
    load: float = 0.8,
) -> EpisodeSpec:
    return EpisodeSpec(
        episode_id=episode_id,
        seed=seed,
        mode=mode,
        trace=workload_trace(steps, load=load),
        initial_window=observed_input(demand),
        initial_demands=PerZone[Demand](front=demand, rear=demand, top=demand),
        max_steps=max_steps,
    )


def baseline_identity(demand: float) -> DependencyIdentity:
    """Baseline を条件 hash へ載せるための名指し。"""
    return DependencyIdentity(
        name="constant-baseline",
        version="1",
        digest=sha256(f"{demand:.6f}".encode()).hexdigest(),
    )


def shadow_config(authority: str = "limited"):
    """記録照合の許容幅を持つ検証済み設定（**呼び出し側の写しを使わない**）。"""
    return _mpc_policy(authority).shadow


def build_environment(
    trained,
    *,
    config_overrides: Any = None,
    dynamics: Any = None,
    baseline_demand: float = 0.4,
    authority: str = "limited",
    with_mpc: bool = True,
) -> tuple[SupervisorTrainingEnvironment, RlTrainingConfig, FanPolicyConfig, SafetyConfig]:
    """束縛済みの MPC controller を含む環境一式を組み立てる。

    `with_mpc=False` にすると、**反実仮想 artifact が無くて束縛できなかった runtime** を作る。
    """
    config, config_sha = rl_config(**(config_overrides or {}))
    _model, _profile, attestation = trained
    settings = _mpc_policy(authority)
    controller = None
    unavailable = None
    if with_mpc:
        controller, _planning, settings = build_controller(
            trained, policy_config=settings, clock=ScriptedClock(0)
        )
    else:
        unavailable = Reason(
            code="counterfactual_model_unavailable",
            detail="反実仮想能力を申告した artifact が Registry に無い",
        )
    safety = mpc_safety()
    engine = dynamics or SimulatedThermalDynamics(config.simulator, config_sha256=config_sha)
    environment = SupervisorTrainingEnvironment(
        config,
        config_sha256=config_sha,
        policy=settings,
        safety=safety,
        dynamics=engine,
        mpc=controller,
        mpc_unavailable=unavailable,
        baseline=lambda: ConstantBaseline(baseline_demand),
        baseline_identity=baseline_identity(baseline_demand),
        expected_model_version=attestation.version,
    )
    return environment, config, settings, safety


def _mpc_policy(authority: str) -> FanPolicyConfig:
    from test_learned_mpc import mpc_policy

    return mpc_policy(authority=authority)


def logged_trajectory(demand: float, *, steps: int = 6) -> LoggedTrajectory:
    """`observed_input(demand)` の続きとして成立する記録。"""
    frames = []
    for index in range(steps):
        ts_ms = ACTION_TS_MS + (index + 1) * 1_000
        frames.append(
            LoggedFrame(
                ts_ms=ts_ms,
                applied=PerZone[Demand](front=demand, rear=demand, top=demand),
                values={AIR: 23.0 + 0.1 * index, GPU: 46.0 + 0.2 * index},
            )
        )
    return LoggedTrajectory(trajectory_id="pr105-log", frames=tuple(frames))


# ------------------------------------- 不変条件 1 / 2: action は Demand ではなく、経路も無い


FORBIDDEN_MODULES = (
    "coldaisle.control.hardware",
    "coldaisle.control.safety",
    "coldaisle.control.reactive",
    "serial",
    "subprocess",
)
"""学習環境が import してはいけない module（AGENTS.md ルール1 / 2 / 6）。"""

CLOCK_MODULES = ("time", "datetime")
"""壁時計。環境は証拠から時刻を作るので、これらを読まない（決定記録 0054 §2.7）。"""


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def test_invariant_2_a_the_rl_package_cannot_reach_the_actuation_path() -> None:
    """**Guard / Safety / Hardware へ触れられる学習環境を作らない。**

    触れられるようになった瞬間、「学習は読み取りだけ」が設計上の約束ではなくなる。
    """
    offenders = [
        f"{path.name}: {name}"
        for path in sorted(RL_PACKAGE.glob("*.py"))
        for name in _imported_modules(path)
        if any(
            name == forbidden or name.startswith(f"{forbidden}.") for forbidden in FORBIDDEN_MODULES
        )
    ]
    assert offenders == []


def test_invariant_13_a_the_rl_package_has_no_wall_clock() -> None:
    """**壁時計を読まない。** 同じ条件から同じ結果が出ることの前提である。"""
    offenders = [
        f"{path.name}: {name}"
        for path in sorted(RL_PACKAGE.glob("*.py"))
        for name in _imported_modules(path)
        if name in CLOCK_MODULES or name.startswith(tuple(f"{m}." for m in CLOCK_MODULES))
    ]
    assert offenders == []


def test_invariant_1_a_the_rl_package_never_names_pwm_or_effective_demand() -> None:
    """学習環境の型に **PWM も `EffectiveZoneDemand` も現れない**（決定記録 0028 §2.3）。"""
    offenders = []
    for path in sorted(RL_PACKAGE.glob("*.py")):
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


def test_invariant_1_b_an_action_cannot_carry_a_demand() -> None:
    """**action に Demand を入れられない。** RL が探索するのは戦略までである。"""
    with pytest.raises(ValidationError):
        SupervisorAction.model_validate(default_action().model_dump(mode="python") | {"front": 0.9})
    with pytest.raises(ValidationError):
        SupervisorAction.model_validate(default_action().model_dump(mode="python") | {"pwm": 255})


def test_invariant_1_c_the_environment_step_only_accepts_an_action(trained) -> None:
    """環境の `step` は `SupervisorAction` しか受け取らない。"""
    environment, *_ = build_environment(trained)
    environment.reset(episode_spec())
    with pytest.raises(ValidationError):
        environment.step(PerZone[Demand](front=0.9, rear=0.9, top=0.9))  # type: ignore[arg-type]


# ------------------------------------- 不変条件 3: demand を作るのは MPC と Gate だけ


def test_invariant_3_a_requested_demand_comes_from_the_controller_pipeline(trained) -> None:
    """action から demand への写像は MPC / Gate が持つ。

    **環境が独自に demand を作っていないこと**を、記録された制御器と Hard Constraints で
    確かめる。Learned MPC が実際に採用された step が1つも無ければ、環境が MPC を
    通していない実装と区別が付かないので、それも条件にする。
    """
    environment, _config, settings, safety = build_environment(trained)
    result = run_all(environment, episode_spec(max_steps=5))

    bounds = settings.mpc.optimizer.zone_bounds
    for step in result.steps:
        for zone in Zone:
            demand = step.requested.get(zone)
            assert demand >= safety.zone_min_demand.get(zone).value
            assert demand <= bounds.get(zone).ceiling.value

    learned = [
        step for step in result.steps if step.active_controller is ControllerKind.LEARNED_MPC
    ]
    assert learned, "Learned MPC が1 step も requested を作っていない"
    assert all(step.optimizer_status is OptimizerStatus.OK for step in learned)


def test_invariant_3_b_uncertain_and_ood_steps_are_distinguishable(trained) -> None:
    """**低 Confidence / OOD の step を通常状態として扱わない**（AGENTS.md ルール4）。

    環境の中でも Gate は本物なので、OOD の tick は Fallback になり、その理由が残る。
    #105 の受入基準「uncertainty/OOD な episode を区別して評価できる」を、step ごとの
    記録として満たす。
    """
    environment, *_ = build_environment(trained)
    result = run_all(environment, episode_spec(max_steps=5))

    ood_steps = [step for step in result.steps if step.ood]
    assert ood_steps, "OOD の step が1つも出ていない（試験用 profile の前提が変わった）"
    for step in ood_steps:
        assert step.active_controller is ControllerKind.FALLBACK
        assert step.fallback_reason is not None
        assert step.confidence is ConfidenceLevel.LOW


def test_invariant_3_c_the_real_fallback_controller_satisfies_the_baseline_contract(
    trained,
) -> None:
    """Baseline の契約は **本物の #79 Fallback Controller** がそのまま満たす。"""
    from test_fallback_controller import catalog

    _model, _profile, attestation = trained
    config, config_sha = rl_config()
    controller, _planning, settings = build_controller(
        trained, policy_config=_mpc_policy("limited"), clock=ScriptedClock(0)
    )
    environment = SupervisorTrainingEnvironment(
        config,
        config_sha256=config_sha,
        policy=settings,
        safety=mpc_safety(),
        dynamics=SimulatedThermalDynamics(config.simulator, config_sha256=config_sha),
        mpc=controller,
        baseline=lambda: FallbackController(settings, catalog()),
        baseline_identity=DependencyIdentity(name="fallback-controller", version="1"),
        expected_model_version=attestation.version,
    )
    result = run_all(environment, episode_spec(max_steps=3))

    assert result.coverage.supported_steps == 3


# ------------------------------------- 不変条件 4: 範囲外の action は丸めない


def test_invariant_4_a_an_out_of_range_action_ends_the_episode_instead_of_being_clipped(
    trained,
) -> None:
    """**範囲外の action を丸めない。** 丸めると、運転時に拒否される action を学び続ける。"""
    environment, *_ = build_environment(trained)
    environment.reset(episode_spec())
    record = environment.step(default_action(strategy="aggressive"))

    assert not record.supported
    assert record.reward is None
    assert record.unsupported_reason is not None
    assert record.unsupported_reason.code == "action_out_of_bounds"

    result = environment.episode_result()
    assert result.termination is TerminationReason.INVALID_ACTION
    assert result.safety.invalid_actions == 1
    assert not result.promotable


def test_invariant_4_b_the_action_space_is_the_configured_supervisor_bounds(trained) -> None:
    """action 空間は `supervisor.output_bounds` そのもの。**環境が別の範囲を持たない。**"""
    environment, _config, settings, _safety = build_environment(trained)
    space = environment.action_space

    assert space.bounds is settings.supervisor.output_bounds
    with pytest.raises(InvalidSupervisorActionError):
        space.validate(default_action(strategy="aggressive"))
    assert ActionSpace(settings.supervisor.output_bounds).contains(default_action())


# ------------------------------------- 不変条件 5: Safety 違反相当は terminal


def test_invariant_5_a_a_request_below_the_configured_floor_terminates_the_episode(
    trained,
) -> None:
    """**最低安全 demand を下回った要求で探索を続けない。**

    運転時は #78 が引き上げるが、環境はそれを模さない。「Safety が直してくれる」前提の
    policy を学習させないため、その state を終端にする（決定記録 0058 §2.5）。
    """
    environment, *_ = build_environment(trained, baseline_demand=0.1, authority="shadow")
    environment.reset(episode_spec())
    record = environment.step(default_action())

    assert not record.supported
    assert record.unsupported_reason is not None
    assert record.unsupported_reason.code == "safety_floor_shortfall"

    result = environment.episode_result()
    assert result.termination is TerminationReason.SAFETY_VIOLATION
    assert result.safety.floor_shortfalls == len(Zone)
    assert not result.promotable


def test_invariant_5_b_crossing_the_absolute_ceiling_terminates_the_episode(trained) -> None:
    """**絶対温度上限を超えた state を通常状態として扱わない。**"""
    document = rl_document()
    document["simulator"] = {
        "model_id": "rl-sim-runaway",
        "model_version": "0.1.0",
        "responses": [
            # 上限を大きく超える平衡温度へ、1 step でほぼ到達する近似。
            simulator_response(AIR, ambient=200.0, gain=0.0, time_constant_ms=1),
            simulator_response(GPU, ambient=200.0, gain=0.0, time_constant_ms=1),
        ],
    }
    environment, *_ = build_environment(
        trained, config_overrides={"simulator": document["simulator"]}
    )
    environment.reset(episode_spec())
    environment.step(default_action())

    result = environment.episode_result()
    assert result.termination is TerminationReason.SAFETY_VIOLATION
    assert result.safety.ceiling_exceedances > 0
    assert not result.promotable


def test_invariant_5_c_a_violating_episode_cannot_claim_a_clean_ending(trained) -> None:
    """安全側の違反を持つ結果を、`HORIZON` で終わったことにできない（型で拒む）。"""
    environment, *_ = build_environment(trained, baseline_demand=0.1, authority="shadow")
    environment.reset(episode_spec())
    environment.step(default_action())
    result = environment.episode_result()

    with pytest.raises(ValidationError):
        result.model_validate(
            result.model_dump(mode="python") | {"termination": TerminationReason.HORIZON.value}
        )
    with pytest.raises(ValidationError):
        result.model_validate(result.model_dump(mode="python") | {"promotable": True})


def test_invariant_5_d_the_environment_says_which_safety_model_it_has(trained) -> None:
    """環境の安全は **screen だけ**であることを、結果に必ず残す。"""
    environment, *_ = build_environment(trained)
    assert environment.safety_model is SafetyModel.CONFIGURED_MINIMUM_ONLY
    environment.reset(episode_spec(max_steps=1))
    environment.step(default_action())
    assert environment.episode_result().safety_model is SafetyModel.CONFIGURED_MINIMUM_ONLY


# ------------------------------------- 不変条件 6: 近似 simulator は証拠を名乗れない


def test_invariant_6_a_a_simulator_cannot_claim_registry_evidence() -> None:
    """**近似 simulator に artifact hash を持たせない。** 検証済みに見える道を作らない。"""
    with pytest.raises(ValidationError):
        DynamicsIdentity(
            provenance=DynamicsProvenance.SIMULATED_PROVISIONAL,
            model_id="rl-sim-test",
            model_version="0.1.0",
            artifact_sha256="a" * 64,
            config_sha256="b" * 64,
        )
    with pytest.raises(ValidationError):
        DynamicsIdentity(
            provenance=DynamicsProvenance.REGISTRY_ATTESTED,
            model_id="rack-thermal",
            model_version="0.1.0",
        )


def test_invariant_6_b_the_configured_simulator_is_always_provisional() -> None:
    """設定から作った simulator は必ず `simulated_provisional`。"""
    config, config_sha = rl_config()
    simulator = SimulatedThermalDynamics(config.simulator, config_sha256=config_sha)

    assert simulator.identity.provenance is DynamicsProvenance.SIMULATED_PROVISIONAL
    assert simulator.identity.artifact_sha256 is None
    assert not simulator.identity.claims_evidence
    assert simulator.attestation is None
    assert not attested_evidence(simulator)


def test_invariant_6_c_todays_artifacts_cannot_be_a_learned_simulator(trained, tmp_path) -> None:
    """**観測再生だけの artifact を learned simulator にしない**（決定記録 0048 §2.1）。

    いま Registry へ登録できるのは `observational_replay` だけなので、この経路は
    **決定論的にすべて拒む**。0052 §2.1 の規律を環境側にもそのまま置いている。
    """
    base, _profile, _attestation = trained
    observational = issue_attestation(
        tmp_path / "observational",
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        capability=ArtifactCapability.OBSERVATIONAL_REPLAY,
        payload=canonical_artifact_bytes(base._artifact),
    )
    assert observational.capability is ArtifactCapability.OBSERVATIONAL_REPLAY

    with pytest.raises(DynamicsUnusableError, match="反実仮想予測を申告していない"):
        AttestedThermalDynamics.bind(
            PlanningModel(base, capability=InferenceCapability.OBSERVATIONAL_REPLAY),
            attestation=observational,
        )


def test_invariant_6_d_a_wrapper_around_another_artifact_is_refused(trained) -> None:
    """別の artifact を包んだ wrapper が、借りた証拠で learned simulator になれない。"""
    base, _profile, attestation = trained
    with pytest.raises(DynamicsUnusableError, match="model_id"):
        AttestedThermalDynamics.bind(
            PlanningModel(base, model_id="other-thermal"), attestation=attestation
        )
    with pytest.raises(TypeError, match="bind"):
        AttestedThermalDynamics()


def test_invariant_6_e_a_simulated_episode_is_not_promotable(trained) -> None:
    """**近似 simulator の episode を昇格の根拠にしない。**"""
    environment, *_ = build_environment(trained)
    result = run_all(environment, episode_spec())

    assert result.termination is TerminationReason.HORIZON
    assert result.usable_for_comparison
    assert not result.promotable
    assert all(step.provenance is DynamicsProvenance.SIMULATED_PROVISIONAL for step in result.steps)


def run_all(environment: SupervisorTrainingEnvironment, spec: EpisodeSpec):
    """action を固定したまま episode を最後まで回す補助。"""
    environment.reset(spec)
    while True:
        try:
            environment.step(default_action())
        except EnvironmentUsageError:
            break
    return environment.episode_result()


# ------------------------------------- 不変条件 7: 記録に無い action の結果を作らない


def test_invariant_7_a_a_logged_episode_scores_only_the_recorded_action(trained) -> None:
    """**記録と違う action の結果を作らない**（決定記録 0053 §2.3 / 0054 §2.2）。"""
    logged = LoggedTrajectoryDynamics(logged_trajectory(0.31), shadow=shadow_config())
    environment, *_ = build_environment(
        trained, dynamics=logged, baseline_demand=0.8, authority="shadow"
    )
    environment.reset(episode_spec(mode=TrainingMode.LOGGED))
    record = environment.step(default_action())

    assert not record.supported
    assert record.observed == {}
    assert record.reward is None
    assert record.unsupported_reason is not None
    assert record.unsupported_reason.code == "applied_action_differs"

    result = environment.episode_result()
    assert result.termination is TerminationReason.UNSUPPORTED_ACTION
    assert result.coverage.unsupported == {"applied_action_differs": 1}


def test_invariant_7_b_a_logged_step_cannot_carry_fabricated_values() -> None:
    """採点できない step に観測を付けられない（型で拒む）。"""
    with pytest.raises(ValidationError):
        DynamicsStep(
            provenance=DynamicsProvenance.LOGGED_TRAJECTORY,
            supported=False,
            values={GPU: 45.0},
            reason=Reason(code="applied_action_differs"),
        )
    with pytest.raises(ValidationError):
        DynamicsStep(provenance=DynamicsProvenance.LOGGED_TRAJECTORY, supported=False)


def test_invariant_7_c_a_matching_logged_action_is_scored(trained) -> None:
    """記録と同じ action が掛かった区間は、実測として採点できる。"""
    logged = LoggedTrajectoryDynamics(logged_trajectory(0.8), shadow=shadow_config())
    environment, *_ = build_environment(
        trained, dynamics=logged, baseline_demand=0.8, authority="shadow"
    )
    environment.reset(episode_spec(mode=TrainingMode.LOGGED, demand=0.8))
    record = environment.step(default_action())

    assert record.supported
    assert record.provenance is DynamicsProvenance.LOGGED_TRAJECTORY
    assert record.reward is not None


def test_invariant_7_d_hybrid_keeps_the_origin_of_every_step(trained) -> None:
    """hybrid では step ごとに出どころが残り、近似が混ざれば昇格の根拠にしない。"""
    config, config_sha = rl_config()
    hybrid = HybridDynamics(
        LoggedTrajectoryDynamics(logged_trajectory(0.31), shadow=shadow_config()),
        SimulatedThermalDynamics(config.simulator, config_sha256=config_sha),
    )
    environment, *_ = build_environment(
        trained, dynamics=hybrid, baseline_demand=0.8, authority="shadow"
    )
    result = run_all(environment, episode_spec(mode=TrainingMode.HYBRID, demand=0.8))

    provenances = {step.provenance for step in result.steps if step.supported}
    assert DynamicsProvenance.SIMULATED_PROVISIONAL in provenances
    assert not result.promotable


# ------------------------------------- 不変条件 8: coverage 不足は比較に使わない


def test_invariant_8_a_an_episode_below_the_coverage_floor_is_not_comparable(trained) -> None:
    """**採点できた step が少ない episode の reward を比較に使わない**（fail closed）。"""
    logged = LoggedTrajectoryDynamics(logged_trajectory(0.31), shadow=shadow_config())
    environment, *_ = build_environment(
        trained, dynamics=logged, baseline_demand=0.8, authority="shadow"
    )
    result = run_all(environment, episode_spec(mode=TrainingMode.LOGGED, demand=0.8))

    assert result.coverage.supported_steps == 0
    assert result.coverage.supported_fraction == 0.0
    assert not result.usable_for_comparison
    assert not result.promotable


def test_invariant_8_b_coverage_counts_must_add_up(trained) -> None:
    """採点できた数と理由別の内訳の合計は、必ず step 数に一致する。"""
    environment, *_ = build_environment(trained)
    result = run_all(environment, episode_spec())
    coverage = result.coverage

    with pytest.raises(ValidationError):
        coverage.model_validate(coverage.model_dump(mode="python") | {"supported_steps": 99})


# ------------------------------------- 不変条件 9: 再現性


def test_invariant_9_a_the_same_conditions_and_seed_give_the_same_bytes(trained) -> None:
    """**同じ設定・同じ seed・同じ trace からは同じ結果が出る。**"""
    first = run_all(build_environment(trained)[0], episode_spec())
    second = run_all(build_environment(trained)[0], episode_spec())

    assert first.digest() == second.digest()
    assert first.conditions_sha256 == second.conditions_sha256


def test_invariant_9_b_a_different_seed_changes_the_conditions(trained) -> None:
    """seed は条件の一部。**違う seed の結果を同じ条件として並べない。**"""
    first = run_all(build_environment(trained)[0], episode_spec(seed=7))
    second = run_all(build_environment(trained)[0], episode_spec(seed=8))

    assert first.conditions_sha256 != second.conditions_sha256


def test_invariant_9_c_changing_the_reward_or_dynamics_changes_the_conditions(trained) -> None:
    """reward 版・dynamics・workload trace のどれを変えても条件 hash が変わる。"""
    base = run_all(build_environment(trained)[0], episode_spec())

    other_reward = rl_document()["reward"]
    assert isinstance(other_reward, dict)
    other_reward = dict(other_reward) | {"version": "reward-test-v2"}
    changed_reward = run_all(
        build_environment(trained, config_overrides={"reward": other_reward})[0], episode_spec()
    )
    assert changed_reward.conditions_sha256 != base.conditions_sha256

    config, config_sha = rl_config()
    logged = LoggedTrajectoryDynamics(logged_trajectory(0.4), shadow=shadow_config())
    changed_dynamics = run_all(
        build_environment(trained, dynamics=logged, authority="shadow")[0],
        episode_spec(mode=TrainingMode.LOGGED),
    )
    assert changed_dynamics.conditions_sha256 != base.conditions_sha256
    del config, config_sha

    changed_trace = run_all(build_environment(trained)[0], episode_spec(load=0.5))
    assert changed_trace.conditions_sha256 != base.conditions_sha256


def test_invariant_9_d_the_policy_is_not_part_of_the_conditions(trained) -> None:
    """**policy は条件に入れない。** 同じ条件で Rule と RL を比べるためである。"""
    environment, _config, settings, _safety = build_environment(trained)
    rule = RulePolicy(settings.supervisor.rule_policy, SimulatedClock(ACTION_TS_MS))
    rl = StubRlPolicy(
        SupervisorObjectiveWeights(
            gpu_temperature=0.2, cpu_temperature=0.8, balance=0.5, acoustic=0.4, change=0.3
        )
    )
    spec = episode_spec()

    assert (
        environment.run_episode(rule, spec).conditions_sha256
        == environment.run_episode(rl, spec).conditions_sha256
    )


# ------------------------------------- 不変条件 10 / 12: 比較は同じ条件で、安全が先


def test_invariant_10_a_rule_and_rl_can_be_compared_on_the_same_episodes(trained) -> None:
    """**同じ episode 群で RulePolicy と RLPolicy を比べられる**（#105 受入基準）。"""
    environment, _config, settings, _safety = build_environment(trained)
    rule = RulePolicy(settings.supervisor.rule_policy, SimulatedClock(ACTION_TS_MS))
    rl = StubRlPolicy(
        SupervisorObjectiveWeights(
            gpu_temperature=1.0, cpu_temperature=0.1, balance=0.0, acoustic=0.0, change=0.0
        )
    )
    specs = (episode_spec(episode_id="pr105-a"), episode_spec(episode_id="pr105-b", seed=9))

    comparison = SupervisorTrainingEnvironment.compare(
        [environment.run_policy(rule, specs), environment.run_policy(rl, specs)]
    )

    assert {arm.policy for arm in comparison.arms} == {
        SupervisorPolicyKind.RULE,
        SupervisorPolicyKind.RL,
    }
    assert all(arm.episode_ids == ("pr105-a", "pr105-b") for arm in comparison.arms)
    assert len(comparison.ranked) == 2


def test_invariant_10_b_arms_built_under_different_conditions_are_refused(trained) -> None:
    """**条件の違う arm を同じ表に並べない。** 差が policy の差に見えてしまう。"""
    environment, _config, settings, _safety = build_environment(trained)
    rule = RulePolicy(settings.supervisor.rule_policy, SimulatedClock(ACTION_TS_MS))
    rl = StubRlPolicy(
        SupervisorObjectiveWeights(
            gpu_temperature=1.0, cpu_temperature=0.1, balance=0.0, acoustic=0.0, change=0.0
        )
    )
    left = environment.run_policy(rule, (episode_spec(episode_id="pr105-a", seed=1),))
    right = environment.run_policy(rl, (episode_spec(episode_id="pr105-a", seed=2),))

    with pytest.raises(EnvironmentUsageError, match="条件 hash"):
        SupervisorTrainingEnvironment.compare([left, right])


def test_invariant_12_a_safety_ranks_before_reward(trained) -> None:
    """**安全側の違反は reward で覆らない**（辞書式。決定記録 0054 §2.4 と同じ構造）。

    reward で勝っている arm に、同じ条件のまま安全違反だけを持たせて比べる。
    """
    environment, _config, settings, _safety = build_environment(trained)
    rule = RulePolicy(settings.supervisor.rule_policy, SimulatedClock(ACTION_TS_MS))
    clean = environment.run_policy(rule, (episode_spec(episode_id="pr105-a"),))

    # **条件は同じまま**、安全側の台帳と終端理由だけを違反へ差し替えた arm を作る。
    violating = PolicyArm(
        policy=SupervisorPolicyKind.RL,
        policy_version="rl-test-1",
        episodes=(
            EpisodeResult.model_validate(
                clean.episodes[0].model_dump(mode="python")
                | {
                    "policy": SupervisorPolicyKind.RL,
                    "policy_version": "rl-test-1",
                    "termination": TerminationReason.SAFETY_VIOLATION,
                    "termination_reason": Reason(code="absolute_ceiling_exceeded"),
                    "safety": clean.episodes[0].safety.model_dump(mode="python")
                    | {"ceiling_exceedances": 1},
                    "promotable": False,
                }
            ),
        ),
    )

    assert violating.safety_violations > 0
    assert clean.safety_violations == 0

    comparison = SupervisorTrainingEnvironment.compare([violating, clean])
    # reward がどれだけ良くても、違反のある arm が先には来ない。
    assert comparison.ranked[0] is clean
    assert comparison.ranking_key(violating)[0] > comparison.ranking_key(clean)[0]


def test_invariant_12_b_reward_cannot_carry_a_safety_term() -> None:
    """**安全を reward の重みにできない。** 重みにすると、ほかの項で相殺できる。"""
    document = rl_document()
    reward = dict(document["reward"])
    reward["weights"] = dict(reward["weights"]) | {"safety": provisional(1.0)}
    document["reward"] = reward

    with pytest.raises(ValidationError):
        RlTrainingConfig.model_validate(document)


# ------------------------------------- 不変条件 11: 採点基準は設定が持つ


def test_invariant_11_a_the_reward_band_comes_from_config_not_from_the_action(trained) -> None:
    """**agent が target band を広げても reward の採点基準は動かない。**"""
    environment, config, *_ = build_environment(trained)
    wide = SupervisorAction(
        strategy="balanced",
        weights=default_action().weights,
        # 設定の候補にある band はこれだけなので、action 側では帯を変えられない。
        target_band=default_action().target_band,
    )
    environment.reset(episode_spec())
    record = environment.step(wide)

    assert record.reward is not None
    assert record.reward.version == config.reward.version
    # 設定の帯（GPU 40..45）で採点している。action の帯（45..78）なら超過は 0 になる。
    assert record.observed[GPU] > config.reward.target_band.gpu_temperature.upper_c
    assert record.reward.terms.gpu_temperature > 0.0


# ------------------------------------- 使い方の誤りは例外のまま


def test_usage_errors_are_not_episode_outcomes(trained) -> None:
    """reset していない / 終わった episode を進める誤りは、終端理由にしない。"""
    environment, *_ = build_environment(trained)
    with pytest.raises(EnvironmentUsageError, match="reset"):
        environment.step(default_action())

    environment.reset(episode_spec(max_steps=1))
    environment.step(default_action())
    with pytest.raises(EnvironmentUsageError, match="終わった"):
        environment.step(default_action())


def test_the_packaged_config_file_loads(trained) -> None:
    """`config/rl-training.yaml` が検証を通ることを確かめる（既定値はコードに無い）。"""
    del trained
    path = Path(__file__).resolve().parents[1] / "config" / "rl-training.yaml"
    config, digest = RlTrainingConfig.from_file(path)

    assert config.schema_version == 1
    assert len(digest) == 64
    assert config.reward.version == "reward-v1"


def test_the_initial_window_is_unchanged_by_running_an_episode(trained) -> None:
    """**入力を書き換えない。** 同じ spec を何度でも回せる。"""
    environment, *_ = build_environment(trained)
    spec = episode_spec()
    before = spec.initial_window.model_dump(mode="json")
    run_all(environment, spec)

    assert spec.initial_window.model_dump(mode="json") == before
    assert isinstance(spec.initial_window, ObservedThermalInput)
    assert ArtifactVerification.REGISTRY_VERIFIED.value == "registry_verified"
    assert STEP_MS == 1_000


# ---------------- codex レビュー（PR #158）で塞いだ穴。**同じ形を作れないことを試す。**


def hot_window(value: float) -> ObservedThermalInput:
    """全 frame の温度を差し替えた観測 window（絶対上限を超える初期状態を作る）。"""
    base = observed_input(0.4)
    frames = tuple(
        frame.model_copy(update={"values": dict.fromkeys(frame.values, value)})
        for frame in base.window
    )
    return ObservedThermalInput.model_validate(
        base.model_copy(update={"window": frames}).model_dump(mode="python")
    )


class ForgedAttestedDynamics:
    """`registry_attested` を**自称するだけ**の dynamics。証拠 object を持たない。

    provenance も artifact hash もただの値なので、この形は誰でも組み立てられる。
    **それが昇格の根拠にならないこと**を試すために置く。
    """

    def __init__(self, inner: SimulatedThermalDynamics) -> None:
        self._inner = inner
        self._identity = DynamicsIdentity(
            provenance=DynamicsProvenance.REGISTRY_ATTESTED,
            model_id="rack-thermal",
            model_version="0.1.0",
            artifact_sha256="a" * 64,
        )

    @property
    def identity(self) -> DynamicsIdentity:
        return self._identity

    @property
    def attestation(self) -> None:
        return None

    @property
    def provenances(self) -> frozenset[DynamicsProvenance]:
        return frozenset({DynamicsProvenance.REGISTRY_ATTESTED})

    def conditions(self) -> dict[str, object]:
        return {"identity": self._identity.model_dump(mode="json")}

    def advance(self, request, *, rng):
        step = self._inner.advance(request, rng=rng)
        return DynamicsStep.model_validate(
            step.model_dump(mode="python") | {"provenance": DynamicsProvenance.REGISTRY_ATTESTED}
        )


def test_invariant_14_a_the_environment_runs_without_a_counterfactual_model(trained) -> None:
    """**反実仮想 artifact が無くても環境は回る**（偽の attestation を作らない）。

    束縛できなかった事実は運転時と同じ `LearnedFailure.MODEL_LOAD_FAILURE` として Gate へ渡り、
    requested はすべて Fallback が作る（決定記録 0058 §2.1）。
    """
    environment, *_ = build_environment(trained, with_mpc=False)
    assert not environment.learned_controller_available
    result = run_all(environment, episode_spec(max_steps=4))

    assert result.termination is TerminationReason.HORIZON
    assert result.coverage.supported_steps == 4
    assert all(step.active_controller is ControllerKind.FALLBACK for step in result.steps)
    assert all(step.fallback_reason is not None for step in result.steps)
    assert not result.learned_controller_available
    assert not result.promotable


def test_invariant_14_b_an_episode_without_a_controller_cannot_be_promotable(trained) -> None:
    """action が demand に効いていない episode を、昇格の根拠にできない（型で拒む）。"""
    environment, *_ = build_environment(trained, with_mpc=False)
    result = run_all(environment, episode_spec(max_steps=4))

    with pytest.raises(ValidationError):
        EpisodeResult.model_validate(result.model_dump(mode="python") | {"promotable": True})


def test_invariant_14_c_the_controller_state_must_be_stated_exactly_once(trained) -> None:
    """controller と「使えない理由」を両方 / どちらも渡さない、は受け取らない。"""
    config, config_sha = rl_config()
    _model, _profile, attestation = trained
    controller, _planning, settings = build_controller(
        trained, policy_config=_mpc_policy("limited"), clock=ScriptedClock(0)
    )
    common: dict[str, Any] = {
        "config_sha256": config_sha,
        "policy": settings,
        "safety": mpc_safety(),
        "dynamics": SimulatedThermalDynamics(config.simulator, config_sha256=config_sha),
        "baseline": lambda: ConstantBaseline(0.4),
        "baseline_identity": baseline_identity(0.4),
        "expected_model_version": attestation.version,
    }
    with pytest.raises(EnvironmentUsageError, match="どちらか一方"):
        SupervisorTrainingEnvironment(config, **common)
    with pytest.raises(EnvironmentUsageError, match="どちらか一方"):
        SupervisorTrainingEnvironment(
            config, mpc=controller, mpc_unavailable=Reason(code="x"), **common
        )


def test_invariant_6_f_a_self_declared_registry_provenance_grants_nothing(trained) -> None:
    """**自称の `registry_attested` では裏づけにならない。**

    昇格の判断は `ArtifactAttestation` object そのものを見る。Registry の検証経路だけが
    発行するので、近似 simulator はこれを用意できない（決定記録 0058 §2.3）。
    """
    config, config_sha = rl_config()
    forged = ForgedAttestedDynamics(
        SimulatedThermalDynamics(config.simulator, config_sha256=config_sha)
    )

    assert forged.identity.claims_evidence  # 自称はできてしまう
    assert not attested_evidence(forged)  # 証拠が無いので裏づけにならない

    environment, *_ = build_environment(trained, dynamics=forged)
    result = run_all(environment, episode_spec(max_steps=3))
    assert not result.promotable


def test_invariant_6_g_an_attested_binding_is_the_only_source_of_evidence(trained) -> None:
    """Registry の証拠に裏づけられた dynamics だけが `attested_evidence` を満たす。"""
    _model, _profile, attestation = trained
    base, _p, _a = trained
    dynamics = AttestedThermalDynamics.bind(PlanningModel(base), attestation=attestation)

    assert dynamics.attestation is attestation
    assert attested_evidence(dynamics)
    assert dynamics.identity.artifact_sha256 == attestation.artifact_sha256


def test_invariant_7_e_the_logged_tolerance_comes_from_the_validated_policy() -> None:
    """**照合の許容幅を呼び出し側から受け取らない**（決定記録 0054 §2.6 と同じ規則）。"""
    shadow = shadow_config()
    dynamics = LoggedTrajectoryDynamics(logged_trajectory(0.4), shadow=shadow)

    assert dynamics.applied_demand_tolerance == shadow.applied_demand_tolerance.value
    with pytest.raises(TypeError):
        LoggedTrajectoryDynamics(  # type: ignore[call-arg]
            logged_trajectory(0.4), applied_demand_tolerance=0.5
        )


def test_invariant_9_e_conditions_cover_every_injected_dependency(trained) -> None:
    """**注入した依存が変われば条件 hash が変わる。**

    変わらないと、`compare()` が依存の差を policy の差として並べてしまう。
    """
    base = run_all(build_environment(trained)[0], episode_spec())

    # Baseline の中身が変われば条件も変わる。
    other_baseline = run_all(build_environment(trained, baseline_demand=0.5)[0], episode_spec())
    assert other_baseline.conditions_sha256 != base.conditions_sha256

    # 制御器の設定（authority stage / mpc.optimizer / gate 閾値）が変われば条件も変わる。
    other_policy = run_all(build_environment(trained, authority="expanded")[0], episode_spec())
    assert other_policy.conditions_sha256 != base.conditions_sha256

    # Learned MPC の有無も条件の一部。
    without = run_all(build_environment(trained, with_mpc=False)[0], episode_spec())
    assert without.conditions_sha256 != base.conditions_sha256


def test_invariant_9_f_hybrid_conditions_include_the_logged_side(trained) -> None:
    """hybrid の identity は近似側だが、**条件には記録側と許容幅も入る。**"""
    config, config_sha = rl_config()
    simulated = SimulatedThermalDynamics(config.simulator, config_sha256=config_sha)
    left = HybridDynamics(
        LoggedTrajectoryDynamics(logged_trajectory(0.4), shadow=shadow_config()), simulated
    )
    right = HybridDynamics(
        LoggedTrajectoryDynamics(logged_trajectory(0.6), shadow=shadow_config()), simulated
    )

    assert left.identity == right.identity  # identity だけでは見分けられない
    assert left.conditions() != right.conditions()  # 条件は見分けられる

    first = run_all(
        build_environment(trained, dynamics=left, baseline_demand=0.8, authority="shadow")[0],
        episode_spec(mode=TrainingMode.HYBRID, demand=0.8),
    )
    second = run_all(
        build_environment(trained, dynamics=right, baseline_demand=0.8, authority="shadow")[0],
        episode_spec(mode=TrainingMode.HYBRID, demand=0.8),
    )
    assert first.conditions_sha256 != second.conditions_sha256


def test_invariant_5_e_the_initial_state_is_screened_before_any_step(trained) -> None:
    """**最初の state にも screen を掛ける。** 上限超えの初期 window から探索を始めない。"""
    environment, *_ = build_environment(trained)
    spec = EpisodeSpec(
        episode_id="pr105-hot",
        seed=1,
        mode=TrainingMode.LEARNED_SIMULATOR,
        trace=workload_trace(),
        initial_window=hot_window(120.0),
        initial_demands=PerZone[Demand](front=0.4, rear=0.4, top=0.4),
        max_steps=4,
    )
    environment.reset(spec)
    result = environment.episode_result()

    assert result.termination is TerminationReason.SAFETY_VIOLATION
    assert result.termination_reason.code == "initial_ceiling_exceeded"
    assert result.steps == ()
    assert not result.promotable
    with pytest.raises(EnvironmentUsageError, match="終わった"):
        environment.step(default_action())


def test_invariant_5_f_initial_demands_below_the_floor_are_screened(trained) -> None:
    """初期 demand が最低安全 demand を下回っていれば、1 step も進めない。"""
    environment, *_ = build_environment(trained)
    environment.reset(episode_spec(demand=0.1))
    result = environment.episode_result()

    assert result.termination is TerminationReason.SAFETY_VIOLATION
    assert result.termination_reason.code == "initial_floor_shortfall"
    assert result.safety.floor_shortfalls == len(Zone)


def test_invariant_10_c_arms_with_different_comparable_episodes_are_refused(trained) -> None:
    """**arm ごとに落ちた episode を捨てない。** 捨てると母集団が arm ごとに変わる。"""
    environment, _config, settings, _safety = build_environment(trained)
    rule = RulePolicy(settings.supervisor.rule_policy, SimulatedClock(ACTION_TS_MS))
    clean = environment.run_policy(rule, (episode_spec(episode_id="pr105-a"),))

    dropped = PolicyArm(
        policy=SupervisorPolicyKind.RL,
        policy_version="rl-test-1",
        episodes=(
            EpisodeResult.model_validate(
                clean.episodes[0].model_dump(mode="python")
                | {
                    "policy": SupervisorPolicyKind.RL,
                    "policy_version": "rl-test-1",
                    "usable_for_comparison": False,
                    "promotable": False,
                }
            ),
        ),
    )

    with pytest.raises(EnvironmentUsageError, match="比較できる episode 群"):
        SupervisorTrainingEnvironment.compare([clean, dropped])


def test_invariant_10_d_a_truncated_episode_cannot_win_on_reward(trained) -> None:
    """**長さの違う episode の総和を並べない。** 早く終わった arm が勝ってしまう。"""
    environment, _config, settings, _safety = build_environment(trained)
    rule = RulePolicy(settings.supervisor.rule_policy, SimulatedClock(ACTION_TS_MS))
    full = environment.run_policy(rule, (episode_spec(episode_id="pr105-a", max_steps=6),))
    long_episode = full.episodes[0]
    assert len(long_episode.supported_steps) == 6

    truncated_steps = long_episode.steps[:4]
    truncated = PolicyArm(
        policy=SupervisorPolicyKind.RL,
        policy_version="rl-test-1",
        episodes=(
            EpisodeResult.model_validate(
                long_episode.model_dump(mode="python")
                | {
                    "policy": SupervisorPolicyKind.RL,
                    "policy_version": "rl-test-1",
                    "steps": tuple(step.model_dump(mode="python") for step in truncated_steps),
                    "coverage": {"steps": 4, "supported_steps": 4, "unsupported": {}},
                }
            ),
        ),
    )

    # 総和で比べれば、短いほうが「良い」ことになってしまう。
    assert truncated.episodes[0].discounted_reward > long_episode.discounted_reward

    comparison = SupervisorTrainingEnvironment.compare([full, truncated])
    assert comparison.matched_steps() == {"pr105-a": 4}
    # 揃えた長さの上では同じ episode なので、差は付かない。
    assert comparison.mean_matched_reward(full) == comparison.mean_matched_reward(truncated)
    assert comparison.ranking_key(full)[2] == comparison.ranking_key(truncated)[2]


def test_invariant_10_e_a_comparison_records_whether_a_controller_was_present(trained) -> None:
    """**Learned MPC の有無が違う arm を混ぜない。** 混ぜると policy の差を測れない。"""
    environment, _config, settings, _safety = build_environment(trained)
    without, _c, settings_without, _s = build_environment(trained, with_mpc=False)
    rule = RulePolicy(settings.supervisor.rule_policy, SimulatedClock(ACTION_TS_MS))
    rule_without = RulePolicy(settings_without.supervisor.rule_policy, SimulatedClock(ACTION_TS_MS))
    spec = episode_spec()

    with_controller = environment.run_policy(rule, (spec,))
    no_controller = without.run_policy(rule_without, (spec,))

    assert with_controller.learned_controller_available
    assert not no_controller.learned_controller_available
    # 条件 hash が既に違うので、compare はそこで落ちる（混ぜられない）。
    with pytest.raises(EnvironmentUsageError, match="条件 hash"):
        SupervisorTrainingEnvironment.compare([with_controller, no_controller])


def test_invariant_6_h_a_step_cannot_claim_an_uncontracted_provenance(trained) -> None:
    """**step ごとの provenance も自称である。** 契約した出どころの外を受け取らない。

    近似 simulator が「記録から来た」と名乗る step を返せると、実測の裏づけが無い遷移が
    `promotable` な側へ紛れ込む。
    """

    class MislabelingDynamics(SimulatedThermalDynamics):
        def advance(self, request, *, rng):
            step = super().advance(request, rng=rng)
            return DynamicsStep.model_validate(
                step.model_dump(mode="python")
                | {"provenance": DynamicsProvenance.LOGGED_TRAJECTORY}
            )

    config, config_sha = rl_config()
    environment, *_ = build_environment(
        trained,
        dynamics=MislabelingDynamics(config.simulator, config_sha256=config_sha),
    )
    result = run_all(environment, episode_spec(max_steps=3))

    assert result.termination is TerminationReason.DYNAMICS_UNUSABLE
    assert result.termination_reason.code == "provenance_unexpected"
    assert not result.promotable
