"""#79 Baseline / Fallback Controller。全て Mock Snapshot / Replay で検証する。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coldaisle.control.config import FanPolicyConfig
from coldaisle.control.fallback import (
    ControllerGate,
    FallbackController,
    LearnedControlStatus,
    LearnedFailure,
    SnapshotStatus,
)
from coldaisle.control.model.confidence import (
    ComponentResult,
    ConfidenceAssessment,
    ConfidenceComponent,
    derive_inference_id,
)
from coldaisle.control.model.thermal import (
    ArtifactVerification,
    InferenceCapability,
    PredictedTarget,
    ThermalPrediction,
)
from coldaisle.control.schema import (
    AuthorityStage,
    ControllerKind,
    ControllerProposal,
    OperatingMode,
    OptimizerStatus,
    PerZone,
    Reason,
    SafetyState,
    StaticAuthorityStage,
    Zone,
    ZoneRequest,
)
from coldaisle.control.state import (
    ControlStateSnapshot,
    SnapshotSignal,
    TelemetryHealth,
    TelemetryImportance,
)
from coldaisle.metrics import MetricCatalog, MetricMeta
from coldaisle.store.models import Quality


def provisional(value: float) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def guard_band(
    activate: float,
    clear: float,
    degraded_activate: float,
    degraded_clear: float,
) -> dict[str, object]:
    return {
        "activate_above": provisional(activate),
        "clear_at_or_below": provisional(clear),
        "degraded_activate_above": provisional(degraded_activate),
        "degraded_clear_at_or_below": provisional(degraded_clear),
    }


def supervisor_config() -> dict[str, object]:
    target_band = {
        "cpu_temperature": {"lower_c": 45.0, "upper_c": 75.0},
        "gpu_temperature": {"lower_c": 45.0, "upper_c": 78.0},
    }
    context = {
        "strategy": "balanced",
        "weights": {
            "gpu_temperature": 0.8,
            "cpu_temperature": 0.8,
            "balance": 0.5,
            "acoustic": 0.4,
            "change": 0.3,
        },
        "target_band": target_band,
    }
    return {
        "period_ms": 1_000,
        "valid_ms": 2_000,
        "active_policy": "rule_policy",
        "shadow_policy": None,
        "rl_version": None,
        "output_bounds": {
            "strategies": ["balanced"],
            "target_bands": [target_band],
            "weights": {
                name: {"minimum": 0.0, "maximum": 1.0}
                for name in (
                    "gpu_temperature",
                    "cpu_temperature",
                    "balance",
                    "acoustic",
                    "change",
                )
            },
        },
        "rule_policy": {
            "version": "rule-test-v1",
            "contexts": {
                regime: context
                for regime in (
                    "idle",
                    "transient_cpu",
                    "transient_gpu",
                    "transient_cpu_gpu",
                    "sustained_cpu",
                    "sustained_gpu",
                    "sustained_cpu_gpu",
                    "cooldown",
                    "unknown",
                )
            },
        },
    }


def mpc_optimizer_config() -> dict[str, object]:
    """#86 optimizer の暫定設定。値は実測前なのですべて provisional のままにする。"""
    return {
        "horizon_ms": provisional(60_000),
        "step_ms": provisional(10_000),
        "candidate_levels": provisional(5),
        "sweeps": provisional(2),
        "max_evaluations": provisional(64),
        "max_step_up": provisional(0.2),
        "max_step_down": provisional(0.1),
        "zone_bounds": {
            "front": {"floor": provisional(0.2), "ceiling": provisional(1.0)},
            "rear": {"floor": provisional(0.2), "ceiling": provisional(1.0)},
            "top": {"floor": provisional(0.2), "ceiling": provisional(1.0)},
        },
        "cost_scales": {
            "temperature_c": provisional(5.0),
            "balance_ratio": provisional(0.2),
            "acoustic_cost": provisional(1.0),
            "demand_change": provisional(0.1),
        },
        "cost_metrics": {
            "cpu_temperature": "cpu.package",
            "gpu_temperature": "gpu.0.core",
        },
        "unknown_balance_cost": provisional(1.0),
    }


def policy(
    *,
    authority: str = "full",
    recovery_hold_ms: int = 1_000,
    power_feedforward: bool = True,
    demote_after: int = 3,
    demote_window_ms: int = 60_000,
    low_confidence_after: int = 10,
    ood_after: int = 5,
    high_min_confidence: float = 0.85,
    medium_limit_up: float = 0.1,
    medium_limit_down: float = 0.05,
    mpc: dict[str, object] | None = None,
) -> FanPolicyConfig:
    document: dict[str, object] = {
        "schema_version": 9,
        "fallback_curve": [
            {"temperature_c": 20.0, "demand": 0.2},
            {"temperature_c": 80.0, "demand": 0.8},
        ],
        "fallback_temperature_inputs": {
            "front": {"metrics": ["gpu.0.core"]},
            "rear": {"metrics": ["gpu.0.core"]},
            "top": {"metrics": ["cpu.package"]},
        },
        "fallback_dynamics": {"decrease_hysteresis": 0.05, "decrease_hold_ms": 2_000},
        "reactive_guard": {
            "floor": provisional(0.2),
            "hold_ms": provisional(1_000),
            "cpu_power_metric": None,
            "cpu_temperature_rate_c_per_s": guard_band(2.0, 0.5, 1.5, 0.25),
            "gpu_temperature_rate_c_per_s": guard_band(2.0, 0.5, 1.5, 0.25),
            "cpu_power_rate_w_per_s": guard_band(100.0, 20.0, 75.0, 10.0),
            "gpu_power_rate_w_per_s": guard_band(100.0, 20.0, 75.0, 10.0),
            "intake_rise_c": guard_band(2.0, 1.0, 1.5, 0.5),
            "gpu_hotspot_c": guard_band(85.0, 80.0, 82.0, 78.0),
        },
        "mpc": mpc
        or {
            "period_ms": 1_000,
            "budget_ms": 100,
            "valid_ms": 2_000,
            "optimizer": mpc_optimizer_config(),
        },
        "supervisor": supervisor_config(),
        "workload_regime": {
            "cpu_power": {
                "metric": "power.cpu.package",
                "idle_below_w": 30.0,
                "active_above_w": 60.0,
            },
            "gpu_power": {
                "metric": "power.gpu.0",
                "idle_below_w": 40.0,
                "active_above_w": 100.0,
            },
            "activity_window_ms": 1_000,
            "history_window_ms": 120_000,
            "minimum_observation_ms": 10_000,
            "sustained_after_ms": 30_000,
            "cooldown_ms": 20_000,
            "minimum_transition_ms": 2_000,
            "confidence_full_window_ms": 60_000,
            "max_snapshot_gap_ms": 1_000,
        },
        "gate_min_confidence": {
            "limited": provisional(0.6),
            "expanded": provisional(0.7),
            "full": provisional(0.8),
        },
        "model_confidence": {
            "high_min_confidence": provisional(high_min_confidence),
            "medium_limit": {
                "limit_up": provisional(medium_limit_up),
                "limit_down": provisional(medium_limit_down),
            },
            "range_margin": provisional(0.1),
            "min_support_count": provisional(1),
            "full_support_count": provisional(5),
            "min_missing_pattern_count": provisional(1),
            "residual_window": provisional(20),
            "residual_min_samples": provisional(5),
            "residual_match_tolerance_ms": provisional(500),
            "residual_max_age_ms": provisional(60_000),
            "residual_drift_ood_ratio": provisional(3.0),
            "cap_without_uncertainty": provisional(0.9),
            "cap_before_residual_evidence": provisional(0.7),
        },
        "authority_stage": authority,
        "authority_limits": {
            "limited": {
                "permitted_zones": ["front"],
                "limit_up": 0.1,
                "limit_down": 0.1,
            },
            "expanded": {
                "permitted_zones": ["front", "rear", "top"],
                "limit_up": 0.2,
                "limit_down": 0.2,
            },
        },
        "authority_rollout": {
            "approval_max_age_ms": provisional(3_600_000),
            "evidence_max_age_ms": provisional(604_800_000),
            "unhealthy_window_ms": provisional(600_000),
            "low_confidence_after": provisional(low_confidence_after),
            "ood_after": provisional(ood_after),
        },
        "shadow": {
            "enabled": True,
            "outcome_match_tolerance_ms": provisional(500),
            "applied_demand_tolerance": provisional(0.01),
        },
        "recovery_hold_ms": recovery_hold_ms,
        "demote_window_ms": demote_window_ms,
        "demote_after": demote_after,
    }
    if power_feedforward:
        document["fallback_power_feedforward"] = {
            "front": {
                "metric": "power.gpu.0",
                "curve": [
                    {"power_w": 0.0, "demand": 0.2},
                    {"power_w": 600.0, "demand": 1.0},
                ],
            },
            "rear": {
                "metric": "power.gpu.0",
                "curve": [
                    {"power_w": 0.0, "demand": 0.2},
                    {"power_w": 600.0, "demand": 1.0},
                ],
            },
            "top": {
                "metric": "power.cpu.package",
                "curve": [
                    {"power_w": 0.0, "demand": 0.2},
                    {"power_w": 200.0, "demand": 0.8},
                ],
            },
        }
    return FanPolicyConfig.model_validate(document)


def catalog() -> MetricCatalog:
    return MetricCatalog(
        metrics={
            "gpu.0.core": MetricMeta(unit="C", label="gpu"),
            "cpu.package": MetricMeta(unit="C", label="cpu"),
            "power.gpu.0": MetricMeta(unit="W", label="gpu power"),
            "power.cpu.package": MetricMeta(unit="W", label="cpu power"),
        }
    )


def fallback_controller(config: FanPolicyConfig | None = None) -> FallbackController:
    return FallbackController(config or policy(), catalog())


def signal(
    metric: str,
    value: float | None,
    *,
    quality: Quality = Quality.OK,
    mono: int = 0,
) -> SnapshotSignal:
    return SnapshotSignal(
        metric=metric,
        importance=TelemetryImportance.DEGRADED,
        enabled=True,
        value=value,
        quality=quality,
        source_ts_ms=1_000,
        last_changed_mono_ms=mono,
        age_ms=0,
    )


def snapshot(
    *signals: SnapshotSignal,
    tick: int = 1,
    mono: int = 0,
) -> ControlStateSnapshot:
    return ControlStateSnapshot(
        tick_id=tick,
        ts_ms=10_000 + mono,
        monotonic_ms=mono,
        signals=signals,
        derived=(),
        trends=(),
        telemetry_health=TelemetryHealth.NORMAL,
        critical_unavailable=(),
    )


def requests(demand: float, code: str = "test") -> PerZone[ZoneRequest]:
    request = ZoneRequest(demand=demand, reason=Reason(code=code))
    return PerZone(front=request, rear=request, top=request)


TEST_ARTIFACT_SHA256 = "a" * 64
"""`assessment_for` が作る assessment の artifact。**仮の値である**（AGENTS.md ルール10）。"""

TEST_INPUT_SHA256 = "c" * 64
"""判定に使った入力 window の digest（試験用の仮の値）。"""

TEST_MODEL_VERSION = "0.1.0"
"""試験用の model version。**`ThermalPrediction` と同じ semver の形にする**（#159）。

版も識別子の導出に入る予測の中にあるので、assessment だけ別の版を名乗ることはできない。
"""

ACTION_TS_MS = 10_000


def synthetic_prediction(
    artifact_sha256: str = TEST_ARTIFACT_SHA256,
    *,
    model_version: str = TEST_MODEL_VERSION,
    verification: ArtifactVerification = ArtifactVerification.REGISTRY_VERIFIED,
) -> ThermalPrediction:
    """試験用の予測。**artifact はここにあり、推論の識別子はここから導かれる**（#159）。"""
    return ThermalPrediction(
        model_id="rack-thermal",
        model_version=model_version,
        artifact_sha256=artifact_sha256,
        artifact_verification=verification,
        capability=InferenceCapability.OBSERVATIONAL_REPLAY,
        input_action_ts_ms=ACTION_TS_MS,
        targets=(
            PredictedTarget(
                horizon_ms=30_000,
                expected_ts_ms=ACTION_TS_MS + 30_000,
                values={"gpu.0.core": 50.0},
            ),
        ),
    )


def synthetic_inference_id(
    *,
    artifact_sha256: str = TEST_ARTIFACT_SHA256,
    input_sha256: str = TEST_INPUT_SHA256,
    model_version: str = TEST_MODEL_VERSION,
) -> str:
    """その artifact・その版・その入力から**導出される**識別子。宣言した値ではない。"""
    return derive_inference_id(
        input_sha256, synthetic_prediction(artifact_sha256, model_version=model_version)
    )


def learned_proposal(
    demand: float = 0.7,
    *,
    confidence: float = 0.9,
    ood: bool = False,
    version: str = TEST_MODEL_VERSION,
    optimizer: OptimizerStatus = OptimizerStatus.OK,
    inference_id: str | None = None,
) -> ControllerProposal:
    return ControllerProposal(
        controller=ControllerKind.LEARNED_MPC,
        seq=1,
        computed_at_ms=10_000,
        requested=requests(demand, "learned"),
        model_version=version,
        confidence=confidence,
        ood=ood,
        optimizer_status=optimizer,
        latency_ms=10,
        inference_id=(
            inference_id
            if inference_id is not None
            else synthetic_inference_id(model_version=version)
        ),
    )


def fallback_proposal(demand: float = 0.4) -> ControllerProposal:
    return ControllerProposal(
        controller=ControllerKind.FALLBACK,
        seq=1,
        computed_at_ms=10_000,
        requested=requests(demand, "fallback_curve"),
    )


def assessment_for(
    proposal: ControllerProposal,
    *,
    artifact_sha256: str = TEST_ARTIFACT_SHA256,
    input_sha256: str = TEST_INPUT_SHA256,
    verification: ArtifactVerification = ArtifactVerification.REGISTRY_VERIFIED,
) -> ConfidenceAssessment:
    """その artifact で判定した、Registry 検証済みの assessment（Gate の試験用）。

    **推論の識別子は宣言せず、予測と入力 digest から導出する**（#159）。だから
    `artifact_sha256` を変えれば識別子も変わり、既定の提案とは別の推論になる。
    `input_sha256` を変えれば、同じ artifact のまま別の入力の推論になる。
    """
    assert proposal.confidence is not None and proposal.inference_id is not None
    assert proposal.model_version is not None
    prediction = synthetic_prediction(
        artifact_sha256, model_version=proposal.model_version, verification=verification
    )
    components = tuple(
        ComponentResult(component=component, score=1.0, ood=False)
        for component in ConfidenceComponent
    )
    if proposal.ood:
        components = (
            ComponentResult(component=ConfidenceComponent.MODEL_BINDING, score=0.0, ood=True),
            *components[1:],
        )
    else:
        components = (
            ComponentResult(
                component=ConfidenceComponent.MODEL_BINDING,
                score=proposal.confidence,
                ood=False,
            ),
            *components[1:],
        )
    return ConfidenceAssessment(
        model_id="rack-thermal",
        model_version=proposal.model_version,
        artifact_sha256=artifact_sha256,
        artifact_verification=verification,
        profile_sha256="d" * 64,
        input_action_ts_ms=ACTION_TS_MS,
        input_sha256=input_sha256,
        prediction=prediction,
        inference_id=derive_inference_id(input_sha256, prediction),
        confidence=0.0 if proposal.ood else proposal.confidence,
        ood=proposal.ood,
        components=components,
    )


def healthy_status(
    *,
    received: int = 0,
    proposal: ControllerProposal | None = None,
    binding_stage: AuthorityStage = AuthorityStage.FULL,
):
    """**束縛が覆う stage も添える**（#92）。既定は「どの stage でも使える artifact」。"""
    selected = proposal or learned_proposal()
    if selected.ood:
        # OOD の assessment の confidence は 0。提案も同じ値にする
        selected = selected.model_copy(update={"confidence": 0.0})
    return LearnedControlStatus(
        proposal=selected,
        received_at_mono_ms=received,
        assessment=assessment_for(selected),
        binding_authority_stage=binding_stage,
    )


def gate_for(
    settings: FanPolicyConfig,
    *,
    expected_model_version: str,
    expected_artifact_sha256: str | None = TEST_ARTIFACT_SHA256,
) -> ControllerGate:
    """**試験用**に、設定の stage をそのまま実効 stage にする Gate を作る。

    本番の配線ではない。runtime は `AuthorityRuntime` を渡し、journal が stage を決める
    （#92 / 決定記録 0057 §2.2）。`ControllerGate` が `authority` を必須にしているのは、
    配線を忘れた起動が設定の**上限**をそのまま制御権にしないためである。

    `expected_artifact_sha256` は本番では `ArtifactAttestation.artifact_sha256` から来る
    （#159 / 決定記録 0059）。ここでは `assessment_for` が作る artifact を既定にする。
    """
    return ControllerGate(
        settings,
        expected_model_version=expected_model_version,
        expected_artifact_sha256=expected_artifact_sha256,
        authority=StaticAuthorityStage(settings.authority_stage),
    )


def select(
    gate: ControllerGate,
    *,
    now: int,
    fallback: ControllerProposal | None = None,
    learned: LearnedControlStatus | None = None,
    safety: SafetyState = SafetyState.NORMAL,
):
    return gate.select(
        now_mono_ms=now,
        fallback=fallback or fallback_proposal(),
        learned=learned or healthy_status(received=now),
        operating_mode=OperatingMode.AUTO,
        safety_state=safety,
    )


def test_temperature_feedback_power_feedforward_and_minimum_coordination() -> None:
    controller = fallback_controller()
    proposal = controller.propose(
        snapshot(
            signal("gpu.0.core", 50.0),
            signal("cpu.package", 30.0),
            signal("power.gpu.0", 300.0),
            signal("power.cpu.package", 50.0),
        )
    )

    # GPU Power の feed-forward が 0.6。共有 max により排気側だけを強くしない。
    assert proposal.controller is ControllerKind.FALLBACK
    assert proposal.requested.front.demand == pytest.approx(0.6)
    assert proposal.requested.rear.demand == pytest.approx(0.6)
    assert proposal.requested.top.demand == pytest.approx(0.6)
    assert proposal.requested.front.reason.code == "fallback_feedback_feedforward"
    assert proposal.requested.top.reason.code == "fallback_coordinated_max"


@pytest.mark.parametrize(
    ("metric", "unit", "match"),
    [
        ("gpu.0.core", "W", "temperature metric"),
        ("power.gpu.0", "C", "Power metric"),
    ],
)
def test_policy_metrics_must_exist_in_the_catalog_with_the_expected_unit(
    metric: str,
    unit: str,
    match: str,
) -> None:
    broken = catalog().model_copy(
        update={"metrics": catalog().metrics | {metric: MetricMeta(unit=unit, label="wrong unit")}}
    )

    with pytest.raises(ValueError, match=match):
        FallbackController(policy(), broken)


def test_stale_or_missing_inputs_are_not_zero_filled_and_the_reason_is_logged() -> None:
    controller = fallback_controller()
    proposal = controller.propose(
        snapshot(
            signal("gpu.0.core", 50.0),
            signal("cpu.package", 30.0, quality=Quality.STALE),
            signal("power.gpu.0", None, quality=Quality.MISSING),
            signal("power.cpu.package", None, quality=Quality.MISSING),
        )
    )

    # Temperature 欠測時は curve の保守側端点。Power 欠測は feed-forward を外して記録。
    assert proposal.requested.top.demand == 0.8
    assert proposal.requested.top.reason.code == "fallback_temperature_unavailable"
    assert "power_feedforward_disabled=power.cpu.package" in proposal.requested.top.reason.detail
    assert proposal.requested.front.demand == 0.8


def test_missing_power_uses_temperature_only_and_records_disabled_feedforward() -> None:
    proposal = fallback_controller().propose(
        snapshot(
            signal("gpu.0.core", 50.0),
            signal("cpu.package", 50.0),
        )
    )

    assert proposal.requested.front.reason.code == "fallback_power_unavailable"
    assert "power_feedforward_disabled=power.gpu.0" in proposal.requested.front.reason.detail


def test_power_degradation_remains_visible_when_coordination_raises_the_zone() -> None:
    proposal = fallback_controller().propose(
        snapshot(
            signal("gpu.0.core", 20.0),
            signal("cpu.package", 80.0),
        )
    )

    assert proposal.requested.front.reason.code == "fallback_coordinated_max"
    assert "power_feedforward_disabled=power.gpu.0" in proposal.requested.front.reason.detail


def test_ramp_up_is_immediate_and_decrease_waits_for_hysteresis_hold() -> None:
    controller = fallback_controller(policy(power_feedforward=False))
    low = controller.propose(
        snapshot(signal("gpu.0.core", 20.0), signal("cpu.package", 20.0), mono=0)
    )
    high = controller.propose(
        snapshot(signal("gpu.0.core", 80.0), signal("cpu.package", 80.0), tick=2, mono=100)
    )
    held = controller.propose(
        snapshot(signal("gpu.0.core", 20.0), signal("cpu.package", 20.0), tick=3, mono=200)
    )
    lowered = controller.propose(
        snapshot(signal("gpu.0.core", 20.0), signal("cpu.package", 20.0), tick=4, mono=2_200)
    )

    assert low.requested.front.demand == 0.2
    assert high.requested.front.demand == 0.8
    assert held.requested.front.demand == 0.8
    assert held.requested.front.reason.code == "fallback_decrease_hold"
    assert lowered.requested.front.demand == 0.2


def test_small_decrease_inside_hysteresis_is_held_without_starting_a_timer() -> None:
    controller = fallback_controller(policy(power_feedforward=False))
    baseline = controller.propose(
        snapshot(signal("gpu.0.core", 50.0), signal("cpu.package", 50.0), mono=0)
    )
    inside_band = controller.propose(
        snapshot(signal("gpu.0.core", 47.0), signal("cpu.package", 47.0), tick=2, mono=10_000)
    )

    assert baseline.requested.front.demand == pytest.approx(0.5)
    assert inside_band.requested.front.demand == pytest.approx(0.5)
    assert inside_band.requested.front.reason.code == "fallback_decrease_hold"


def test_snapshot_unavailable_uses_configured_conservative_endpoint_without_decreasing() -> None:
    controller = fallback_controller(policy(power_feedforward=False))
    hot = controller.propose(
        snapshot(signal("gpu.0.core", 80.0), signal("cpu.package", 80.0), mono=0)
    )
    unavailable = controller.propose_without_snapshot(tick_id=2, ts_ms=11_000, monotonic_ms=1_000)

    assert unavailable.requested.front.demand == hot.requested.front.demand == 0.8
    assert unavailable.requested.front.reason.code == "fallback_temperature_unavailable"


def test_mock_replay_is_deterministic() -> None:
    replay = (
        snapshot(signal("gpu.0.core", 20.0), signal("cpu.package", 30.0), mono=0),
        snapshot(signal("gpu.0.core", 70.0), signal("cpu.package", 50.0), tick=2, mono=1_000),
        snapshot(signal("gpu.0.core", 30.0), signal("cpu.package", 30.0), tick=3, mono=2_000),
        snapshot(signal("gpu.0.core", 30.0), signal("cpu.package", 30.0), tick=4, mono=4_000),
    )

    first_controller = fallback_controller(policy(power_feedforward=False))
    second_controller = fallback_controller(policy(power_feedforward=False))
    first = [first_controller.propose(item).model_dump() for item in replay]
    second = [second_controller.propose(item).model_dump() for item in replay]

    assert first == second


def test_ml_recovery_requires_a_continuously_healthy_hold() -> None:
    gate = gate_for(policy(recovery_hold_ms=1_000), expected_model_version="0.1.0")

    assert select(gate, now=0).fallback_reason.code == "ml_recovery_hold"
    assert select(gate, now=999).active_controller is ControllerKind.FALLBACK
    recovered = select(gate, now=1_000)

    assert recovered.active_controller is ControllerKind.LEARNED_MPC
    assert recovered.fallback_reason is None


def test_switch_to_fallback_is_immediate_and_does_not_lower_requested_demand() -> None:
    gate = gate_for(policy(recovery_hold_ms=1), expected_model_version="0.1.0")
    select(gate, now=0, learned=healthy_status(received=0, proposal=learned_proposal(0.9)))
    assert (
        select(
            gate, now=1, learned=healthy_status(received=1, proposal=learned_proposal(0.9))
        ).active_controller
        is ControllerKind.LEARNED_MPC
    )

    failed = select(
        gate,
        now=2,
        fallback=fallback_proposal(0.3),
        learned=healthy_status(
            received=2,
            proposal=learned_proposal(0.2, confidence=0.1),
        ),
    )

    assert failed.active_controller is ControllerKind.FALLBACK
    assert failed.fallback_reason.code == "low_confidence"
    assert all(failed.proposal.requested.get(zone).demand == 0.9 for zone in Zone)
    assert failed.proposal.requested.front.reason.code == "fallback_transition_floor"


@pytest.mark.parametrize(
    ("status", "now", "expected"),
    [
        (
            LearnedControlStatus(failure=LearnedFailure.MODEL_LOAD_FAILURE),
            0,
            "model_load_failure",
        ),
        (
            LearnedControlStatus(failure=LearnedFailure.OPTIMIZER_EXCEPTION),
            0,
            "optimizer_exception",
        ),
        (
            healthy_status(proposal=learned_proposal(optimizer=OptimizerStatus.TIMEOUT)),
            0,
            "optimizer_timeout",
        ),
        (
            healthy_status(proposal=learned_proposal(optimizer=OptimizerStatus.ERROR)),
            0,
            "optimizer_error",
        ),
        (healthy_status(proposal=learned_proposal(ood=True)), 0, "ood"),
        (
            LearnedControlStatus(supervisor_available=False),
            0,
            "supervisor_failure",
        ),
        (
            healthy_status(proposal=learned_proposal(version="9.9.9")),
            0,
            "model_version_mismatch",
        ),
        (
            LearnedControlStatus(control_deadline_exceeded=True),
            0,
            "control_deadline_exceeded",
        ),
        (
            LearnedControlStatus(snapshot_status=SnapshotStatus.UNAVAILABLE),
            0,
            "state_snapshot_unavailable",
        ),
        (
            LearnedControlStatus(snapshot_status=SnapshotStatus.INVALID),
            0,
            "state_snapshot_invalid",
        ),
        (healthy_status(received=0), 2_001, "learned_proposal_expired"),
    ],
)
def test_every_fallback_condition_has_a_structured_reason(
    status: LearnedControlStatus,
    now: int,
    expected: str,
) -> None:
    gate = gate_for(policy(), expected_model_version="0.1.0")
    decision = select(gate, now=now, learned=status)

    assert decision.active_controller is ControllerKind.FALLBACK
    assert decision.fallback_reason.code == expected
    assert decision.trace_metadata()["active_controller"] == "fallback"
    assert decision.trace_metadata()["fallback_reason"]["code"] == expected


def test_unavailable_proposal_and_non_normal_safety_have_structured_reasons() -> None:
    unavailable_gate = gate_for(policy(), expected_model_version="0.1.0")
    unavailable = select(unavailable_gate, now=0, learned=LearnedControlStatus())
    assert unavailable.fallback_reason.code == "learned_proposal_unavailable"

    safety_gate = gate_for(policy(), expected_model_version="0.1.0")
    unsafe = select(safety_gate, now=0, safety=SafetyState.DEGRADED)
    assert unsafe.fallback_reason.code == "safety_not_normal"
    assert unsafe.fallback_reason.detail == "state=degraded"


def test_an_unhealthy_tick_resets_the_recovery_hold() -> None:
    gate = gate_for(policy(recovery_hold_ms=1_000), expected_model_version="0.1.0")
    select(gate, now=0)
    select(gate, now=900, learned=healthy_status(received=900, proposal=learned_proposal(ood=True)))
    select(gate, now=1_000)

    assert select(gate, now=1_999).active_controller is ControllerKind.FALLBACK
    assert select(gate, now=2_000).active_controller is ControllerKind.LEARNED_MPC


def test_limited_authority_is_bounded_around_fallback_and_by_zone() -> None:
    gate = gate_for(
        policy(authority=AuthorityStage.LIMITED.value, recovery_hold_ms=1),
        expected_model_version="0.1.0",
    )
    select(gate, now=0)
    selected = select(
        gate,
        now=1,
        learned=healthy_status(received=1, proposal=learned_proposal(0.9)),
    )

    assert selected.proposal.requested.front.demand == 0.5
    assert selected.proposal.requested.rear.demand == 0.4
    assert selected.proposal.requested.top.demand == 0.4


@pytest.mark.parametrize(
    ("stage", "threshold"),
    [
        (AuthorityStage.LIMITED, 0.6),
        (AuthorityStage.EXPANDED, 0.7),
        (AuthorityStage.FULL, 0.8),
    ],
)
def test_confidence_boundary_is_selected_by_authority_stage(
    stage: AuthorityStage,
    threshold: float,
) -> None:
    accepted_gate = gate_for(
        policy(authority=stage.value, recovery_hold_ms=1),
        expected_model_version="0.1.0",
    )
    select(
        accepted_gate,
        now=0,
        learned=healthy_status(proposal=learned_proposal(confidence=threshold)),
    )
    accepted = select(
        accepted_gate,
        now=1,
        learned=healthy_status(received=1, proposal=learned_proposal(confidence=threshold)),
    )
    assert accepted.active_controller is ControllerKind.LEARNED_MPC

    rejected_gate = gate_for(
        policy(authority=stage.value, recovery_hold_ms=1),
        expected_model_version="0.1.0",
    )
    rejected = select(
        rejected_gate,
        now=0,
        learned=healthy_status(proposal=learned_proposal(confidence=threshold - 0.01)),
    )
    assert rejected.fallback_reason.code == "low_confidence"
    assert f"stage={stage.value}" in rejected.fallback_reason.detail


def test_repeated_ml_to_fallback_transitions_emit_a_demotion_signal_for_issue_92() -> None:
    gate = gate_for(
        policy(recovery_hold_ms=1, demote_after=3),
        expected_model_version="0.1.0",
    )
    bad = learned_proposal(confidence=0.1)

    select(gate, now=0)
    select(gate, now=1)
    first = select(gate, now=2, learned=healthy_status(received=2, proposal=bad))
    select(gate, now=3)
    select(gate, now=4)
    second = select(gate, now=5, learned=healthy_status(received=5, proposal=bad))
    select(gate, now=6)
    select(gate, now=7)
    third = select(gate, now=8, learned=healthy_status(received=8, proposal=bad))

    assert first.fallback_transitions_in_window == 1
    assert second.fallback_transitions_in_window == 2
    assert not second.demotion_recommended
    assert third.fallback_transitions_in_window == 3
    assert third.demotion_recommended
    assert third.trace_metadata()["demotion_recommended"] is True


def test_demotion_signal_clears_after_the_configured_window_expires() -> None:
    gate = gate_for(
        policy(recovery_hold_ms=1, demote_after=1, demote_window_ms=100),
        expected_model_version="0.1.0",
    )
    select(gate, now=0)
    select(gate, now=1)
    triggered = select(
        gate,
        now=2,
        learned=healthy_status(received=2, proposal=learned_proposal(confidence=0.1)),
    )
    expired = select(gate, now=103)

    assert triggered.demotion_recommended
    assert expired.fallback_transitions_in_window == 0
    assert not expired.demotion_recommended


def test_gate_does_not_own_manual_or_calibration_requests() -> None:
    gate = gate_for(policy(), expected_model_version="0.1.0")
    with pytest.raises(ValueError, match="MANUAL"):
        gate.select(
            now_mono_ms=0,
            fallback=fallback_proposal(),
            learned=healthy_status(),
            operating_mode=OperatingMode.MANUAL,
            safety_state=SafetyState.NORMAL,
        )


@pytest.mark.parametrize("human_mode", [OperatingMode.MANUAL, OperatingMode.CALIBRATION])
def test_human_mode_round_trip_resets_learned_state_and_requires_recovery_hold_again(
    human_mode: OperatingMode,
) -> None:
    gate = gate_for(policy(recovery_hold_ms=1_000), expected_model_version="0.1.0")
    select(gate, now=0)
    assert select(gate, now=1_000).active_controller is ControllerKind.LEARNED_MPC

    gate.set_operating_mode(human_mode, now_mono_ms=1_001)
    gate.set_operating_mode(OperatingMode.AUTO, now_mono_ms=2_000)
    returned = select(gate, now=2_000)

    assert returned.active_controller is ControllerKind.FALLBACK
    assert returned.fallback_reason.code == "ml_recovery_hold"
    assert select(gate, now=3_000).active_controller is ControllerKind.LEARNED_MPC


def test_max_keeps_fallback_active_and_auto_return_requires_a_fresh_recovery_hold() -> None:
    """MAXの実Fanは#78 forced_max。MLのcounterfactualはactive/holdに使わない。"""
    gate = gate_for(
        policy(recovery_hold_ms=1_000),
        expected_model_version="0.1.0",
    )
    in_max = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.2),
        learned=healthy_status(),
        operating_mode=OperatingMode.MAX,
        safety_state=SafetyState.NORMAL,
    )
    still_max = gate.select(
        now_mono_ms=1_000,
        fallback=fallback_proposal(0.2),
        learned=healthy_status(received=1_000, proposal=learned_proposal(0.9)),
        operating_mode=OperatingMode.MAX,
        safety_state=SafetyState.NORMAL,
    )
    returned = gate.select(
        now_mono_ms=1_001,
        fallback=fallback_proposal(0.2),
        learned=healthy_status(received=1_001, proposal=learned_proposal(0.9)),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    before_hold = gate.select(
        now_mono_ms=2_000,
        fallback=fallback_proposal(0.2),
        learned=healthy_status(received=2_000, proposal=learned_proposal(0.9)),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    recovered = gate.select(
        now_mono_ms=2_001,
        fallback=fallback_proposal(0.2),
        learned=healthy_status(received=2_001, proposal=learned_proposal(0.9)),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert in_max.active_controller is ControllerKind.FALLBACK
    assert in_max.fallback_reason is None
    assert still_max.active_controller is ControllerKind.FALLBACK
    assert still_max.recovery_healthy_since_mono_ms is None
    assert returned.active_controller is ControllerKind.FALLBACK
    assert returned.fallback_reason.code == "ml_recovery_hold"
    assert before_hold.active_controller is ControllerKind.FALLBACK
    assert recovered.active_controller is ControllerKind.LEARNED_MPC


# ------------------------------- 判断を出した model artifact を trace へ残す（#159）


def test_the_gate_records_the_artifact_of_the_attested_inference() -> None:
    """**適用した tick の artifact が `model_gate` に残る**（#159 / 決定記録 0059）。

    値は Registry 検証済みの assessment から来る。提案は artifact の欄を持たないので、
    worker が自称する経路は型として存在しない。
    """
    gate = gate_for(policy(recovery_hold_ms=1), expected_model_version="0.1.0")
    gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    selected = gate.select(
        now_mono_ms=1_000,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=1_000),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selected.active_controller is ControllerKind.LEARNED_MPC
    assert selected.model_gate is not None
    assert selected.model_gate.attested is True
    assert selected.model_gate.artifact_sha256 == "a" * 64


def test_an_assessment_the_registry_did_not_verify_leaves_the_artifact_unknown() -> None:
    """**束縛できない assessment の artifact は残さない**（#85 の裏づけと同じ向き）。

    Registry を通っていない判定の artifact を書くと、検証していない artifact の実績が
    昇格の根拠に使えてしまう。裏づけが無ければ「artifact 不明」にする（fail closed）。
    """
    gate = gate_for(policy(), expected_model_version="0.1.0")
    offline = assessment_for(
        learned_proposal(), verification=ArtifactVerification.OFFLINE_UNVERIFIED
    )
    # 検証状態も識別子の導出に入るので、提案の側も同じ推論を指す。
    proposal = learned_proposal(inference_id=offline.inference_id)
    selected = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=LearnedControlStatus(
            proposal=proposal,
            received_at_mono_ms=0,
            assessment=offline,
            binding_authority_stage=AuthorityStage.FULL,
        ),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selected.active_controller is ControllerKind.FALLBACK
    assert selected.model_gate is not None
    assert selected.model_gate.attested is False
    assert selected.model_gate.artifact_sha256 is None


def test_an_assessment_from_another_artifact_cannot_be_relabelled_as_the_bound_one() -> None:
    """**artifact B の判定を A と名乗らせられない**（codex #4057241944）。

    前の版は「写した欄同士」を比べていたので、A を期待する Gate に対して
    `model_copy(update={"artifact_sha256": A})` した B の assessment が素通りし、
    **B の提案がそのまま採られた**（推論 ID は B のままなので提案とは一致する）。

    いまは識別子を `prediction`（artifact を含む）から**導出**して検証するので、

    1. 欄だけ書き換えたものは **型が受け取らない**（導出が合わない）
    2. 予測ごと A に差し替えれば識別子が変わり、**B の提案とは別の推論**になる
    3. B の assessment をそのまま出せば、A を期待する Gate が artifact で退ける
    """
    other = "b" * 64
    # B で作った、完全に正しい assessment と、その推論に属する B の提案。
    b_assessment = assessment_for(learned_proposal(), artifact_sha256=other)
    b_proposal = learned_proposal(inference_id=b_assessment.inference_id)
    gate = gate_for(
        policy(), expected_model_version="0.1.0", expected_artifact_sha256=TEST_ARTIFACT_SHA256
    )

    # 1. 欄だけ A に書き換えたものは、そもそも型が受け取らない。
    relabelled = b_assessment.model_copy(update={"artifact_sha256": TEST_ARTIFACT_SHA256})
    with pytest.raises(ValidationError, match="判定した予測の artifact と違う"):
        ConfidenceAssessment.model_validate(relabelled.model_dump(mode="python"))
    with pytest.raises(ValidationError, match="判定した予測の artifact と違う"):
        LearnedControlStatus(
            proposal=b_proposal,
            received_at_mono_ms=0,
            assessment=relabelled,
            binding_authority_stage=AuthorityStage.FULL,
        )

    # 1b. 予測の中の artifact まで書き換えても、**識別子の導出が合わなくなる**。
    deeper = b_assessment.model_copy(
        update={
            "artifact_sha256": TEST_ARTIFACT_SHA256,
            "prediction": b_assessment.prediction.model_copy(
                update={"artifact_sha256": TEST_ARTIFACT_SHA256}
            ),
        }
    )
    with pytest.raises(ValidationError, match="入力の digest と予測から導けない"):
        ConfidenceAssessment.model_validate(deeper.model_dump(mode="python"))

    # 2. 予測ごと A にすれば識別子が変わり、B の提案の推論ではなくなる。
    a_assessment = assessment_for(learned_proposal(), artifact_sha256=TEST_ARTIFACT_SHA256)
    assert a_assessment.inference_id != b_assessment.inference_id
    rebuilt = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=LearnedControlStatus(
            proposal=b_proposal,
            received_at_mono_ms=0,
            assessment=a_assessment,
            binding_authority_stage=AuthorityStage.FULL,
        ),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    assert rebuilt.active_controller is ControllerKind.FALLBACK
    assert rebuilt.fallback_reason is not None
    assert rebuilt.fallback_reason.code == "confidence_unattested"
    assert "another inference" in rebuilt.fallback_reason.detail
    assert rebuilt.model_gate is not None
    assert rebuilt.model_gate.artifact_sha256 is None

    # 3. B の assessment をそのまま出せば、artifact で退ける。
    honest = gate.select(
        now_mono_ms=1,
        fallback=fallback_proposal(0.4),
        learned=LearnedControlStatus(
            proposal=b_proposal,
            received_at_mono_ms=1,
            assessment=b_assessment,
            binding_authority_stage=AuthorityStage.FULL,
        ),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    assert honest.active_controller is ControllerKind.FALLBACK
    assert honest.fallback_reason is not None
    assert honest.fallback_reason.code == "model_artifact_mismatch"
    assert honest.model_gate is not None
    assert honest.model_gate.attested is False
    assert honest.model_gate.artifact_sha256 is None, "束縛できない artifact を残さない"


def test_a_gate_without_a_bound_artifact_never_records_one() -> None:
    """**Learned MPC を束縛できていない runtime は、どの提案も採らない**（#159）。

    `expected_artifact_sha256=None` は「照らす相手が無い」という明示であって、
    「何でも通す」ではない。fail closed にする。
    """
    gate = gate_for(policy(), expected_model_version="0.1.0", expected_artifact_sha256=None)
    selected = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selected.active_controller is ControllerKind.FALLBACK
    assert selected.fallback_reason is not None
    assert selected.fallback_reason.code == "model_artifact_mismatch"
    assert selected.model_gate is not None
    assert selected.model_gate.artifact_sha256 is None


def test_the_gate_requires_an_explicit_expected_artifact() -> None:
    """**配線の抜けが「何にも照らさない」Gate を作らない**（0057 §2.2 と同じ理由）。

    既定値を置かず、必須の引数にする。形の違う値も受け取らない。
    """
    with pytest.raises(TypeError):
        ControllerGate(  # type: ignore[call-arg]
            policy(),
            expected_model_version="0.1.0",
            authority=StaticAuthorityStage(AuthorityStage.FULL),
        )
    with pytest.raises(ValueError, match="sha256 の16進表現"):
        ControllerGate(
            policy(),
            expected_model_version="0.1.0",
            expected_artifact_sha256="NOT-A-HASH",
            authority=StaticAuthorityStage(AuthorityStage.FULL),
        )


def test_an_assessment_for_another_inference_cannot_lend_its_artifact() -> None:
    """**artifact は、その提案を出した推論の assessment からしか来ない。**

    別の推論の assessment を付け替えても、Gate はそれを退け、artifact も残さない。
    付け替えが通ると、「A の提案の実績」を B の artifact の実績にできる。
    """
    gate = gate_for(policy(), expected_model_version="0.1.0")
    proposal = learned_proposal()
    # 同じ artifact だが**別の入力**の推論。識別子が違うので提案には付けられない。
    other = assessment_for(proposal, input_sha256="e" * 64)
    assert other.inference_id != proposal.inference_id
    selected = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=LearnedControlStatus(
            proposal=proposal,
            received_at_mono_ms=0,
            assessment=other,
            binding_authority_stage=AuthorityStage.FULL,
        ),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selected.active_controller is ControllerKind.FALLBACK
    assert selected.fallback_reason is not None
    assert selected.fallback_reason.code == "confidence_unattested"
    assert selected.model_gate is not None
    assert selected.model_gate.artifact_sha256 is None


def test_a_model_version_cannot_be_restated_independently_of_the_prediction() -> None:
    """**同じ artifact bytes を指す登録が2つあると、版だけを書き換えられた**
    （codex #4057753197）。

    artifact が同じなら artifact の照合は通り、推論 ID は予測から導出されたまま提案と
    一致する。**版だけを top-level で書き換えれば、B の判定が A の実績になる。**
    版も予測（識別子の導出に入っている側）と一致していなければならない。
    """
    b_assessment = assessment_for(learned_proposal(version="9.9.9"))
    assert b_assessment.prediction.model_version == "9.9.9"

    relabelled = b_assessment.model_copy(update={"model_version": TEST_MODEL_VERSION})
    with pytest.raises(ValidationError, match="予測の model_version が違う"):
        ConfidenceAssessment.model_validate(relabelled.model_dump(mode="python"))

    # 提案も同じように書き換えても、型の段で止まるので Gate まで届かない。
    with pytest.raises(ValidationError, match="予測の model_version が違う"):
        LearnedControlStatus(
            proposal=learned_proposal(
                version=TEST_MODEL_VERSION, inference_id=b_assessment.inference_id
            ),
            received_at_mono_ms=0,
            assessment=relabelled,
            binding_authority_stage=AuthorityStage.FULL,
        )

    # 予測ごと版を変えれば識別子が変わり、**もう同じ推論ではない**。
    a_assessment = assessment_for(learned_proposal(version=TEST_MODEL_VERSION))
    assert a_assessment.inference_id != b_assessment.inference_id
