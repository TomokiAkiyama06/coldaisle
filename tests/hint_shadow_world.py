"""Shadow solver の子の判断（#288 / 決定記録 0162 §2.5 の PR-B）の試験で共有する世界。

認証ランナーの試験（``test_hint_certify``）と同じ小さな格子の設定・同じ試験用 artifact を使い、
格子の点の1つを「実行時の1 tick」として依頼に組み立てる。digest の仮の値は明らかな仮の値
（AGENTS.md ルール10）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coldaisle.control.config import ControlConfig
from coldaisle.control.hint_domain import DomainFrame, DomainSignal
from coldaisle.control.hint_gate import calibration_sha256, derive_hint_gate_spec, optimizer_sha256
from coldaisle.control.learned_handoff import (
    AvailableCalibration,
    LearnedHintCertification,
    LearnedHintComponents,
    PinnedArtifact,
    calibration_to_runtime,
)
from coldaisle.control.model.thermal import ObservedFanAction
from coldaisle.control.model_registry import ModelRegistry, load_model_registry_limits
from coldaisle.control.mpc.controller import LearnedMpcRuntime, MpcCostDependencies
from coldaisle.control.mpc.optimizer import MpcSolution
from coldaisle.control.schema import (
    AuthorityStage,
    Demand,
    PerZone,
    StaticAuthorityStage,
    SupervisorOutput,
    WorkloadRegime,
)
from coldaisle.hint_certificate import HintGateSpec
from coldaisle.hint_certificate_digest import conditions_sha256
from coldaisle.hint_certify import flat_window
from coldaisle.learned_worker.mpc import RegistryArtifactSource
from coldaisle.learned_worker.shadow_solver import (
    ShadowRequest,
    ShadowSolverCore,
    WorkerStaticComponents,
)
from coldaisle.measurement import Quality
from coldaisle.metrics import MetricCatalog
from test_control_config import write_documents
from test_hint_certify import _artifact, certify_documents
from test_learned_mpc import CALIBRATION_OFFSETS, CATALOG, GPU, supervisor_output
from test_model_confidence import AIR

REPO = Path(__file__).resolve().parents[1]
REGISTRY_LIMITS_DIR = REPO / "config"
REGISTRY_LIMITS = load_model_registry_limits(REGISTRY_LIMITS_DIR)
METRICS = REPO / "config" / "metrics.yaml"
CATALOG_SHA256 = MetricCatalog.from_yaml_with_sha256(METRICS)[1]
"""``test_learned_mpc.CATALOG`` と同じファイルの SHA-256（子は自分で読んで照らす）。"""
STATIC = WorkerStaticComponents(source_tree="1" * 64, lockfile="2" * 64, environment="3" * 64)
"""子が起動時に計算する部品の仮の値（試験では frame の期待値と同じにする）。"""
CALIBRATION = AvailableCalibration(offsets_c=dict(CALIBRATION_OFFSETS))
BASE_REGIME = WorkloadRegime.TRANSIENT_GPU
"""``test_hint_certify.OVERRIDES`` の鍵の Regime。上書き先は ``sustained_gpu``。"""
CLOCK_MS = 0
"""子の単調時計（固定）。打ち切り時刻はこれ + ``mpc.budget_ms`` にする。"""


def write_control(directory: Path, docs: dict[str, dict[str, Any]] | None = None) -> ControlConfig:
    """小さな格子の設定（``test_hint_certify.certify_documents``）を書いて読む。"""
    directory.mkdir(parents=True, exist_ok=True)
    write_documents(directory, docs or certify_documents())  # type: ignore[arg-type]
    return ControlConfig.from_directory(directory)


@dataclass(frozen=True)
class World:
    """1つの格子の点を実行時の 1 tick として組み立てた依頼と、それを作った材料。"""

    control: ControlConfig
    config_dir: Path
    registry_root: Path
    pinned: PinnedArtifact
    spec: HintGateSpec
    request: ShadowRequest
    base_solution: MpcSolution

    def core(
        self,
        *,
        components: WorkerStaticComponents = STATIC,
        clock_ms: int = CLOCK_MS,
    ) -> ShadowSolverCore:
        return ShadowSolverCore(
            control=self.control,
            catalog=CATALOG,
            catalog_sha256=CATALOG_SHA256,
            artifacts=RegistryArtifactSource(
                ModelRegistry(self.registry_root, limits=REGISTRY_LIMITS)
            ),
            components=components,
            monotonic_ms=lambda: clock_ms,
        )

    def with_request(self, **changes: Any) -> ShadowRequest:
        return self.request.model_copy(update=changes)

    def with_components(self, **changes: str) -> ShadowRequest:
        certification = self.request.certification
        components = certification.components.model_copy(update=changes)
        return self.request.model_copy(
            update={"certification": certification.model_copy(update={"components": components})}
        )


def demands(front: float, rear: float, top: float) -> PerZone[Demand]:
    return PerZone[Demand](front=front, rear=rear, top=top)


def build_world(config_dir: Path, registry_root: Path) -> World:
    """格子の点の1つを1 tick にする。

    直前 Demand 0.5・温度は基準の upper_c − 5・floor の1行目・baseline の1行目。
    """
    control = ControlConfig.from_directory(config_dir)
    attestation = _artifact(registry_root).verified.attestation
    pinned = PinnedArtifact(
        model_id=attestation.model_id,
        version=attestation.version,
        artifact_sha256=attestation.artifact_sha256,
    )
    return _build(control, config_dir, registry_root, pinned)


def _build(
    control: ControlConfig, config_dir: Path, registry_root: Path, pinned: PinnedArtifact
) -> World:
    policy = control.policy
    spec = derive_hint_gate_spec(policy.supervisor.workload_hint, control.safety.workload_hint)
    contexts = policy.supervisor.rule_policy.contexts
    base_context = contexts.get(BASE_REGIME)
    hint_context = contexts.get(WorkloadRegime.SUSTAINED_GPU)
    loaded = RegistryArtifactSource(ModelRegistry(registry_root, limits=REGISTRY_LIMITS)).load(
        pinned, AuthorityStage.SHADOW
    )
    assert not isinstance(loaded, str), loaded
    dependencies = MpcCostDependencies.from_control(control)
    runtime = LearnedMpcRuntime.load(
        loaded,
        policy,
        control.safety,
        metric_catalog=CATALOG,
        calibration=calibration_to_runtime(CALIBRATION),
        expected_model_version=pinned.version,
        monotonic_ms=lambda: CLOCK_MS,
        authority=StaticAuthorityStage(AuthorityStage.SHADOW),
        air_balance=dependencies.air_balance,
        balance_band=dependencies.balance_band,
        fan_hardware=dependencies.fan_hardware,
    )
    controller = runtime.controller
    assert controller is not None, runtime.failure_reason
    schema = controller.binding.model.feature_schema
    cpu_upper = base_context.target_band.cpu_temperature.upper_c
    gpu_upper = base_context.target_band.gpu_temperature.upper_c
    values = {AIR: cpu_upper - 5.0, GPU: gpu_upper - 5.0}
    previous = 0.5
    action = PerZone[ObservedFanAction](
        front=ObservedFanAction(effective_demand=previous),
        rear=ObservedFanAction(effective_demand=previous),
        top=ObservedFanAction(effective_demand=previous),
    )
    observed = flat_window(schema, values, action)
    floor = demands(0.4, 0.4, 0.5)
    baseline = demands(0.6, 0.6, 0.6)
    base_supervisor = _supervisor(base_context)
    hint_supervisor = base_supervisor.model_copy(update={"target_band": hint_context.target_band})
    budget = policy.mpc.optimizer.max_evaluations.value
    base = controller.solve(
        observed=observed,
        safety_floor=floor,
        weights=base_supervisor.weights,
        target_band=base_supervisor.target_band,
        baseline=baseline,
        max_evaluations=budget,
        deadline_mono_ms=CLOCK_MS + policy.mpc.budget_ms,
    )
    assert base.solution is not None, base.reason
    frames = tuple(
        DomainFrame(
            tick_id=index,
            ts_ms=ts_ms,
            cpu_temperature=DomainSignal(value=values[AIR], quality=Quality.OK, source_ts_ms=ts_ms),
            gpu_temperature=DomainSignal(value=values[GPU], quality=Quality.OK, source_ts_ms=ts_ms),
            features={},
        )
        for index, ts_ms in enumerate(range(0, schema.window_ms + 1, schema.sample_period_ms))
    )
    certification = LearnedHintCertification(
        certificate_sha256="c" * 64,
        components=LearnedHintComponents(
            source_tree=STATIC.source_tree or "",
            lockfile=STATIC.lockfile or "",
            environment=STATIC.environment or "",
            hint_gate=spec.sha256(),
            optimizer=optimizer_sha256(policy, control.safety),
            conditions=conditions_sha256(controller.conditions()),
            calibration=calibration_sha256(CALIBRATION),
        ),
        domain_lookback_ms=schema.window_ms,
        domain_stale_after_ms=schema.stale_after_ms,
    )
    request = ShadowRequest(
        seq=1,
        observed=observed,
        domain_frames=frames,
        previous_applied=PerZone[Demand | None](front=previous, rear=previous, top=previous),
        safety_floor=floor,
        baseline=baseline,
        base_supervisor=base_supervisor,
        hint_supervisor=hint_supervisor,
        base_requested=base.solution.requested,
        base_targets=base.solution.prediction.targets,
        base_anchor_inference_id=base.solution.anchor_inference_id,
        remaining_evaluations=budget,
        deadline_mono_ms=CLOCK_MS + policy.mpc.budget_ms,
        certification=certification,
        calibration=CALIBRATION,
        thermal_model=pinned,
        authority_stage=AuthorityStage.SHADOW,
        config=control.runtime_digest(),
        metric_catalog_sha256=CATALOG_SHA256,
    )
    return World(
        control=control,
        config_dir=config_dir,
        registry_root=registry_root,
        pinned=pinned,
        spec=spec,
        request=request,
        base_solution=base.solution,
    )


def _supervisor(context: Any) -> SupervisorOutput:
    return supervisor_output().model_copy(
        update={
            "regime": BASE_REGIME,
            "weights": context.weights,
            "strategy": context.strategy,
            "target_band": context.target_band,
        }
    )
