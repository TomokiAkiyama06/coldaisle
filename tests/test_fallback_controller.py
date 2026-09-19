"""#79 Baseline / Fallback Controller。全て Mock Snapshot / Replay で検証する。"""

from __future__ import annotations

import pytest

from coldaisle.control.config import FanPolicyConfig
from coldaisle.control.fallback import (
    ControllerGate,
    FallbackController,
    LearnedControlStatus,
    LearnedFailure,
    SnapshotStatus,
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


def policy(
    *,
    authority: str = "full",
    recovery_hold_ms: int = 1_000,
    power_feedforward: bool = True,
    demote_after: int = 3,
    demote_window_ms: int = 60_000,
    high_min_confidence: float = 0.85,
    medium_limit_up: float = 0.1,
    medium_limit_down: float = 0.05,
) -> FanPolicyConfig:
    document: dict[str, object] = {
        "schema_version": 6,
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
        "mpc": {"period_ms": 1_000, "budget_ms": 100, "valid_ms": 2_000},
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


def learned_proposal(
    demand: float = 0.7,
    *,
    confidence: float = 0.9,
    ood: bool = False,
    version: str = "thermal-v1",
    optimizer: OptimizerStatus = OptimizerStatus.OK,
    inference_id: str = "c" * 64,
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
        inference_id=inference_id,
    )


def fallback_proposal(demand: float = 0.4) -> ControllerProposal:
    return ControllerProposal(
        controller=ControllerKind.FALLBACK,
        seq=1,
        computed_at_ms=10_000,
        requested=requests(demand, "fallback_curve"),
    )


def healthy_status(*, received: int = 0, proposal: ControllerProposal | None = None):
    return LearnedControlStatus(
        proposal=proposal or learned_proposal(),
        received_at_mono_ms=received,
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
    gate = ControllerGate(policy(recovery_hold_ms=1_000), expected_model_version="thermal-v1")

    assert select(gate, now=0).fallback_reason.code == "ml_recovery_hold"
    assert select(gate, now=999).active_controller is ControllerKind.FALLBACK
    recovered = select(gate, now=1_000)

    assert recovered.active_controller is ControllerKind.LEARNED_MPC
    assert recovered.fallback_reason is None


def test_switch_to_fallback_is_immediate_and_does_not_lower_requested_demand() -> None:
    gate = ControllerGate(policy(recovery_hold_ms=1), expected_model_version="thermal-v1")
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
            healthy_status(proposal=learned_proposal(version="other")),
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
    gate = ControllerGate(policy(), expected_model_version="thermal-v1")
    decision = select(gate, now=now, learned=status)

    assert decision.active_controller is ControllerKind.FALLBACK
    assert decision.fallback_reason.code == expected
    assert decision.trace_metadata()["active_controller"] == "fallback"
    assert decision.trace_metadata()["fallback_reason"]["code"] == expected


def test_unavailable_proposal_and_non_normal_safety_have_structured_reasons() -> None:
    unavailable_gate = ControllerGate(policy(), expected_model_version="thermal-v1")
    unavailable = select(unavailable_gate, now=0, learned=LearnedControlStatus())
    assert unavailable.fallback_reason.code == "learned_proposal_unavailable"

    safety_gate = ControllerGate(policy(), expected_model_version="thermal-v1")
    unsafe = select(safety_gate, now=0, safety=SafetyState.DEGRADED)
    assert unsafe.fallback_reason.code == "safety_not_normal"
    assert unsafe.fallback_reason.detail == "state=degraded"


def test_an_unhealthy_tick_resets_the_recovery_hold() -> None:
    gate = ControllerGate(policy(recovery_hold_ms=1_000), expected_model_version="thermal-v1")
    select(gate, now=0)
    select(gate, now=900, learned=healthy_status(received=900, proposal=learned_proposal(ood=True)))
    select(gate, now=1_000)

    assert select(gate, now=1_999).active_controller is ControllerKind.FALLBACK
    assert select(gate, now=2_000).active_controller is ControllerKind.LEARNED_MPC


def test_limited_authority_is_bounded_around_fallback_and_by_zone() -> None:
    gate = ControllerGate(
        policy(authority=AuthorityStage.LIMITED.value, recovery_hold_ms=1),
        expected_model_version="thermal-v1",
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
    accepted_gate = ControllerGate(
        policy(authority=stage.value, recovery_hold_ms=1),
        expected_model_version="thermal-v1",
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

    rejected_gate = ControllerGate(
        policy(authority=stage.value, recovery_hold_ms=1),
        expected_model_version="thermal-v1",
    )
    rejected = select(
        rejected_gate,
        now=0,
        learned=healthy_status(proposal=learned_proposal(confidence=threshold - 0.01)),
    )
    assert rejected.fallback_reason.code == "low_confidence"
    assert f"stage={stage.value}" in rejected.fallback_reason.detail


def test_repeated_ml_to_fallback_transitions_emit_a_demotion_signal_for_issue_92() -> None:
    gate = ControllerGate(
        policy(recovery_hold_ms=1, demote_after=3),
        expected_model_version="thermal-v1",
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
    gate = ControllerGate(
        policy(recovery_hold_ms=1, demote_after=1, demote_window_ms=100),
        expected_model_version="thermal-v1",
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
    gate = ControllerGate(policy(), expected_model_version="thermal-v1")
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
    gate = ControllerGate(policy(recovery_hold_ms=1_000), expected_model_version="thermal-v1")
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
    gate = ControllerGate(
        policy(recovery_hold_ms=1_000),
        expected_model_version="thermal-v1",
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
