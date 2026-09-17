"""#79 Baseline / Fallback Controller。全て Mock Snapshot / Replay で検証する。"""

from __future__ import annotations

import pytest

from coldaisle.control.config import FanPolicyConfig
from coldaisle.control.fallback import (
    ControllerGate,
    FallbackController,
    LearnedControlStatus,
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
from coldaisle.store.models import Quality


def provisional(value: float) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def policy(
    *,
    authority: str = "full",
    recovery_hold_ms: int = 1_000,
    power_feedforward: bool = True,
) -> FanPolicyConfig:
    document: dict[str, object] = {
        "schema_version": 2,
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
            "ceiling": provisional(1.0),
            "hold_ms": provisional(1_000),
            "intake_rise_threshold_c": provisional(2.0),
            "gpu_hotspot_threshold_c": provisional(85.0),
        },
        "mpc": {"period_ms": 1_000, "budget_ms": 100, "valid_ms": 2_000},
        "supervisor": {"period_ms": 1_000, "valid_ms": 2_000},
        "gate_min_confidence": provisional(0.7),
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
        "demote_window_ms": 60_000,
        "demote_after": 3,
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
    controller = FallbackController(policy())
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


def test_stale_or_missing_inputs_are_not_zero_filled_and_the_reason_is_logged() -> None:
    controller = FallbackController(policy())
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
    proposal = FallbackController(policy()).propose(
        snapshot(
            signal("gpu.0.core", 50.0),
            signal("cpu.package", 50.0),
        )
    )

    assert proposal.requested.front.reason.code == "fallback_power_unavailable"
    assert "power_feedforward_disabled=power.gpu.0" in proposal.requested.front.reason.detail


def test_power_degradation_remains_visible_when_coordination_raises_the_zone() -> None:
    proposal = FallbackController(policy()).propose(
        snapshot(
            signal("gpu.0.core", 20.0),
            signal("cpu.package", 80.0),
        )
    )

    assert proposal.requested.front.reason.code == "fallback_coordinated_max"
    assert "power_feedforward_disabled=power.gpu.0" in proposal.requested.front.reason.detail


def test_ramp_up_is_immediate_and_decrease_waits_for_hysteresis_hold() -> None:
    controller = FallbackController(policy(power_feedforward=False))
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
    controller = FallbackController(policy(power_feedforward=False))
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
    controller = FallbackController(policy(power_feedforward=False))
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

    first_controller = FallbackController(policy(power_feedforward=False))
    second_controller = FallbackController(policy(power_feedforward=False))
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
        (LearnedControlStatus(model_loaded=False), 0, "model_load_failure"),
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
