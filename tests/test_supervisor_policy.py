"""#88 Supervisor Interface を Mock state / worker candidate だけで検証する。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control import (
    AuthorityStage,
    BoundBy,
    ControllerKind,
    ControlState,
    ControlTick,
    EffectiveZoneDemand,
    OperatingMode,
    PerZone,
    Reason,
    SafetyState,
    SupervisorDecision,
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyEvaluation,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    TemperatureTarget,
    WorkloadRegime,
    ZoneRecord,
)
from coldaisle.control.config import SupervisorConfig
from coldaisle.control.state import ControlStateSnapshot, TelemetryHealth
from coldaisle.control.supervisor import (
    ReceivedSupervisorOutput,
    RegimeEvidence,
    RegimeReason,
    RulePolicy,
    ShadowRLPolicy,
    SupervisorCoordinator,
    SupervisorInput,
    SupervisorOutputOrigin,
    WorkloadRegimeEstimate,
)

BASE_TS_MS = 1_800_000_000_000


def target_band() -> SupervisorTargetBand:
    return SupervisorTargetBand(
        cpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=75.0),
        gpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=78.0),
    )


def weights(*, acoustic: float = 0.4) -> SupervisorObjectiveWeights:
    return SupervisorObjectiveWeights(
        gpu_temperature=0.8,
        cpu_temperature=0.8,
        balance=0.5,
        acoustic=acoustic,
        change=0.3,
    )


def supervisor_document(
    *,
    active: str = "rule_policy",
    shadow: str | None = "rl_policy",
) -> dict[str, object]:
    band = target_band().model_dump(mode="json")
    balanced = {
        "strategy": "balanced",
        "weights": weights().model_dump(mode="json"),
        "target_band": band,
    }
    conservative = {
        "strategy": "conservative",
        "weights": weights(acoustic=0.1).model_dump(mode="json"),
        "target_band": band,
    }
    return {
        "period_ms": 1_000,
        "valid_ms": 2_000,
        "active_policy": active,
        "shadow_policy": shadow,
        "rl_version": "rl-test-v1",
        "output_bounds": {
            "strategies": ["balanced", "conservative"],
            "target_bands": [band],
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
                "idle": balanced,
                "transient_cpu": balanced,
                "transient_gpu": balanced,
                "transient_cpu_gpu": balanced,
                "sustained_cpu": balanced,
                "sustained_gpu": balanced,
                "sustained_cpu_gpu": balanced,
                "cooldown": balanced,
                "unknown": conservative,
            },
        },
    }


def config(*, active: str = "rule_policy", shadow: str | None = "rl_policy") -> SupervisorConfig:
    return SupervisorConfig.model_validate(supervisor_document(active=active, shadow=shadow))


def snapshot(tick: int = 7, mono: int = 10_000) -> ControlStateSnapshot:
    return ControlStateSnapshot(
        tick_id=tick,
        ts_ms=BASE_TS_MS,
        monotonic_ms=mono,
        signals=(),
        derived=(),
        trends=(),
        telemetry_health=TelemetryHealth.NORMAL,
        critical_unavailable=(),
    )


def workload(
    current: ControlStateSnapshot,
    regime: WorkloadRegime = WorkloadRegime.SUSTAINED_GPU,
    confidence: float = 0.8,
) -> WorkloadRegimeEstimate:
    return WorkloadRegimeEstimate(
        regime=regime,
        confidence=confidence,
        reason=RegimeReason.OBSERVED_HISTORY,
        as_of_tick_id=current.tick_id,
        computed_at_ms=current.ts_ms,
        evidence=RegimeEvidence(observed_window_ms=30_000),
    )


def policy_input(
    *,
    regime: WorkloadRegime = WorkloadRegime.SUSTAINED_GPU,
    tick: int = 7,
    mono: int = 10_000,
    confidence: float = 0.8,
) -> SupervisorInput:
    current = snapshot(tick=tick, mono=mono)
    return SupervisorInput(snapshot=current, workload=workload(current, regime, confidence))


def earlier_input(*, tick: int, mono: int, confidence: float = 0.6) -> SupervisorInput:
    """非同期 RL worker が推論に使った、現在より前の tick の input。"""
    return policy_input(tick=tick, mono=mono, confidence=confidence)


class FakeRLPolicy:
    kind = SupervisorPolicyKind.RL
    version = "rl-test-v1"

    def __init__(self, clock: SimulatedClock) -> None:
        self._clock = clock

    def propose(self, current: SupervisorInput) -> SupervisorOutput:
        return SupervisorOutput(
            snapshot_schema_version=current.snapshot.schema_version,
            tick_id=current.snapshot.tick_id,
            ts_ms=current.snapshot.ts_ms,
            policy=self.kind,
            version=self.version,
            regime=current.workload.regime,
            regime_confidence=current.workload.confidence,
            weights=weights(),
            strategy="balanced",
            target_band=target_band(),
            computed_at_ms=self._clock.now_ms(),
        )


def rl_candidate(
    current: SupervisorInput,
    received_mono: int = 10_000,
    *,
    source: SupervisorInput | None = None,
    binding_origin: SupervisorOutputOrigin = SupervisorOutputOrigin.ACTIVE_BINDING,
) -> ReceivedSupervisorOutput:
    """``source``（既定は現在）の snapshot から作った RL 提案を、受信済みの形にする。

    `binding_origin` の既定を `active_binding` にしているのは、鮮度や範囲の試験が
    用途の検査で先に落ちないようにするためである。用途そのものの検査は
    `test_the_active_slot_refuses_output_that_is_not_bound_for_active` が行う。
    """
    origin = source or current
    shadow = ShadowRLPolicy(FakeRLPolicy(SimulatedClock(BASE_TS_MS)))
    return ReceivedSupervisorOutput(
        output=shadow.propose(origin),
        source_monotonic_ms=origin.snapshot.monotonic_ms,
        received_monotonic_ms=received_mono,
        origin=binding_origin,
    )


def contains_key(schema: object, key: str) -> bool:
    if isinstance(schema, dict):
        return key in schema or any(contains_key(value, key) for value in schema.values())
    if isinstance(schema, list):
        return any(contains_key(value, key) for value in schema)
    return False


def test_rule_policy_maps_every_regime_from_validated_config_without_demand() -> None:
    current = policy_input(regime=WorkloadRegime.UNKNOWN)
    output = RulePolicy(config().rule_policy, SimulatedClock(BASE_TS_MS)).propose(current)

    assert output.policy is SupervisorPolicyKind.RULE
    assert output.strategy == "conservative"
    assert output.regime is WorkloadRegime.UNKNOWN
    assert output.computed_at_ms == BASE_TS_MS

    schema = SupervisorOutput.model_json_schema()
    assert not contains_key(schema, "demand")
    assert not contains_key(schema, "requested_demand")
    assert not contains_key(schema, "effective_demand")
    assert not contains_key(schema, "pwm")
    assert not contains_key(schema, "pwm_raw")
    assert not contains_key(schema, "hwmon")
    with pytest.raises(ValidationError, match="demand"):
        SupervisorOutput(**(output.model_dump() | {"demand": 0.5}))


@pytest.mark.parametrize("regime", list(WorkloadRegime))
def test_rule_policy_has_a_config_context_for_every_workload_regime(
    regime: WorkloadRegime,
) -> None:
    # 決定記録 0036 の TRANSIENT_CPU_GPU のように Regime が増えても、context 欠落で
    # RulePolicy が例外になり Fallback へ落ちることを起動前 config 検証で防ぐ。
    output = RulePolicy(config().rule_policy, SimulatedClock(BASE_TS_MS)).propose(
        policy_input(regime=regime)
    )

    assert output.regime is regime


def test_rule_active_and_rl_shadow_are_recorded_for_the_same_state() -> None:
    current = policy_input()
    decision = SupervisorCoordinator(config(), SimulatedClock(BASE_TS_MS)).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=rl_candidate(current),
    )

    assert decision.active.output is not None
    assert decision.active.output.policy is SupervisorPolicyKind.RULE
    assert decision.shadow is not None and decision.shadow.output is not None
    assert decision.shadow.output.policy is SupervisorPolicyKind.RL
    assert decision.shadow.output.tick_id == decision.active.output.tick_id
    assert decision.selected_output is decision.active.output


@pytest.mark.parametrize("age_ms", [0, 2_000])
def test_rl_candidate_is_valid_at_the_monotonic_freshness_boundary(age_ms: int) -> None:
    current = policy_input()
    source = current if age_ms == 0 else earlier_input(tick=5, mono=10_000 - age_ms)
    coordinator = SupervisorCoordinator(
        config(active="rl_policy", shadow=None),
        SimulatedClock(BASE_TS_MS),
    )
    decision = coordinator.evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=rl_candidate(current, received_mono=10_000, source=source),
    )

    assert decision.active.output is not None
    assert decision.active.output.policy is SupervisorPolicyKind.RL
    assert decision.active.source_monotonic_ms == 10_000 - age_ms
    assert decision.fallback is None


@pytest.mark.parametrize(("active", "shadow"), [("rl_policy", None), ("rule_policy", "rl_policy")])
def test_older_but_fresh_rl_proposal_keeps_its_source_identity(
    active: str, shadow: str | None
) -> None:
    # 0028 §2.2: RL は worker で非同期に推論し、ループは最新の提案を読むだけ。
    current = policy_input()
    source = earlier_input(tick=5, mono=8_500)
    decision = SupervisorCoordinator(
        config(active=active, shadow=shadow),
        SimulatedClock(BASE_TS_MS),
    ).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=rl_candidate(current, received_mono=9_900, source=source),
    )

    rl = decision.active if active == "rl_policy" else decision.shadow
    assert rl is not None and rl.output is not None
    assert rl.output.policy is SupervisorPolicyKind.RL
    assert (rl.output.tick_id, rl.output.regime_confidence) == (5, 0.6)
    assert (rl.source_monotonic_ms, rl.received_monotonic_ms) == (8_500, 9_900)
    assert decision.tick_id == current.snapshot.tick_id
    if active == "rl_policy":
        assert decision.selected_output is rl.output
        assert decision.fallback is None
    else:
        assert decision.selected_output is decision.active.output

    tick = control_tick(decision, current)
    assert tick.supervisor is decision


@pytest.mark.parametrize("active", ["rl_policy", "rule_policy"])
def test_rl_proposal_older_than_valid_ms_from_its_source_is_rejected(active: str) -> None:
    # 受信は新しくても、元 snapshot から valid_ms を超えていれば期限切れ。
    current = policy_input()
    source = earlier_input(tick=4, mono=7_999)
    decision = SupervisorCoordinator(
        config(active=active, shadow=None if active == "rl_policy" else "rl_policy"),
        SimulatedClock(BASE_TS_MS),
    ).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=rl_candidate(current, received_mono=9_999, source=source),
    )

    rl = decision.active if active == "rl_policy" else decision.shadow
    assert rl is not None and rl.output is None
    assert rl.error is not None and rl.error.code == "supervisor_expired"
    assert rl.source_monotonic_ms == 7_999
    assert decision.selected_output is not None
    assert decision.selected_output.policy is SupervisorPolicyKind.RULE


@pytest.mark.parametrize("active", ["rl_policy", "rule_policy"])
def test_rl_proposal_from_a_future_tick_is_rejected(active: str) -> None:
    current = policy_input()
    future = rl_candidate(current).output.model_copy(update={"tick_id": 8})
    decision = SupervisorCoordinator(
        config(active=active, shadow=None if active == "rl_policy" else "rl_policy"),
        SimulatedClock(BASE_TS_MS),
    ).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=ReceivedSupervisorOutput(
            output=future,
            source_monotonic_ms=10_000,
            received_monotonic_ms=10_000,
            origin=SupervisorOutputOrigin.ACTIVE_BINDING,
        ),
    )

    rl = decision.active if active == "rl_policy" else decision.shadow
    assert rl is not None and rl.output is None
    assert rl.error is not None and rl.error.code == "supervisor_output_invalid"
    assert "未来" in rl.error.detail
    assert decision.selected_output is not None
    assert decision.selected_output.policy is SupervisorPolicyKind.RULE


@pytest.mark.parametrize(
    "origin",
    [SupervisorOutputOrigin.UNVERIFIED, SupervisorOutputOrigin.SHADOW_BINDING],
)
def test_the_active_slot_refuses_output_that_is_not_bound_for_active(
    origin: SupervisorOutputOrigin,
) -> None:
    """**用途は値に付いて回る。** shadow 用・用途なしの提案を active slot で使わない。

    `SupervisorOutput` は strategy / weights / target band しか持たないので、shadow 用に
    束縛した policy の出力と active 用の出力は**値として見分けられない**。用途を
    `ReceivedSupervisorOutput` に持たせ、active slot では `active_binding` 以外を
    Rule へ落とす（fail closed。#89 / 決定記録 0061 §2.4）。
    """
    current = policy_input()
    candidate = rl_candidate(current, binding_origin=origin)

    active = SupervisorCoordinator(
        config(active="rl_policy", shadow=None), SimulatedClock(BASE_TS_MS)
    ).evaluate(current, now_monotonic_ms=10_000, rl_candidate=candidate)

    assert active.active.output is None
    assert active.active.error is not None
    assert active.active.error.code == "supervisor_origin_not_active"
    assert active.fallback is not None and active.fallback.output is not None
    assert active.selected_output is active.fallback.output
    assert active.selected_output.policy is SupervisorPolicyKind.RULE

    # shadow は MPC にも Fan にも届かないので、用途を問わず記録する。問うと、昇格前の
    # 候補を観測できず、昇格に要る証拠をそもそも集められない（決定記録 0053 §2.3）。
    shadowed = SupervisorCoordinator(config(), SimulatedClock(BASE_TS_MS)).evaluate(
        current, now_monotonic_ms=10_000, rl_candidate=candidate
    )

    assert shadowed.shadow is not None and shadowed.shadow.output is not None
    assert shadowed.active.output is shadowed.selected_output


def test_older_rl_proposal_is_rejected_when_the_regime_has_changed_since() -> None:
    current = policy_input(regime=WorkloadRegime.SUSTAINED_GPU)
    source = policy_input(regime=WorkloadRegime.IDLE, tick=5, mono=9_000)
    decision = SupervisorCoordinator(
        config(active="rl_policy", shadow=None),
        SimulatedClock(BASE_TS_MS),
    ).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=rl_candidate(current, received_mono=9_500, source=source),
    )

    assert decision.active.error is not None
    assert decision.active.error.code == "supervisor_output_invalid"
    assert decision.selected_output is not None
    assert decision.selected_output.policy is SupervisorPolicyKind.RULE


def test_source_snapshot_cannot_be_later_than_receipt() -> None:
    current = policy_input()
    with pytest.raises(ValidationError, match="受信単調時刻より後"):
        ReceivedSupervisorOutput(
            output=rl_candidate(current).output,
            source_monotonic_ms=10_001,
            received_monotonic_ms=10_000,
        )


def test_expired_or_stopped_rl_falls_back_to_rule_without_using_wall_clock() -> None:
    current = policy_input()
    coordinator = SupervisorCoordinator(
        config(active="rl_policy", shadow=None),
        SimulatedClock(BASE_TS_MS),
    )
    source = earlier_input(tick=4, mono=7_999)
    expired_output = rl_candidate(current, source=source).output.model_copy(
        update={"computed_at_ms": BASE_TS_MS + 10_000_000}
    )
    expired = coordinator.evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=ReceivedSupervisorOutput(
            output=expired_output,
            source_monotonic_ms=7_999,
            received_monotonic_ms=7_999,
            origin=SupervisorOutputOrigin.ACTIVE_BINDING,
        ),
    )
    stopped = coordinator.evaluate(current, now_monotonic_ms=10_000)

    for decision, reason in (
        (expired, "supervisor_expired"),
        (stopped, "supervisor_unavailable"),
    ):
        assert decision.active.error is not None
        assert decision.active.error.code == reason
        assert decision.fallback is not None and decision.fallback.output is not None
        assert decision.selected_output is decision.fallback.output
        assert decision.selected_output.policy is SupervisorPolicyKind.RULE


def test_invalid_shadow_output_is_recorded_but_never_selected() -> None:
    current = policy_input()
    wrong_tick = rl_candidate(current).output.model_copy(update={"tick_id": 999})
    decision = SupervisorCoordinator(config(), SimulatedClock(BASE_TS_MS)).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=ReceivedSupervisorOutput(
            output=wrong_tick,
            source_monotonic_ms=10_000,
            received_monotonic_ms=10_000,
        ),
    )

    assert decision.active.output is decision.selected_output
    assert decision.shadow is not None and decision.shadow.output is None
    assert decision.shadow.error is not None
    assert decision.shadow.error.code == "supervisor_output_invalid"


def test_future_worker_receipt_is_rejected_and_rl_success_requires_receipt_time() -> None:
    current = policy_input()
    candidate = rl_candidate(current, received_mono=10_001)
    decision = SupervisorCoordinator(config(), SimulatedClock(BASE_TS_MS)).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=candidate,
    )

    assert decision.active.output is decision.selected_output
    assert decision.shadow is not None and decision.shadow.error is not None
    assert decision.shadow.error.code == "supervisor_clock_invalid"
    with pytest.raises(ValidationError, match="受信単調時刻"):
        SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RL,
            output=candidate.output,
        )


@pytest.mark.parametrize("invalid_field", ["strategy", "weights", "target_band"])
def test_rl_output_is_limited_to_configured_strategy_weights_and_target(
    invalid_field: str,
) -> None:
    document = supervisor_document()
    document["output_bounds"]["weights"]["acoustic"]["maximum"] = 0.5
    bounded = SupervisorConfig.model_validate(document)
    current = policy_input()
    output = rl_candidate(current).output
    if invalid_field == "strategy":
        invalid = output.model_copy(update={"strategy": "unconfigured"})
    elif invalid_field == "weights":
        invalid = output.model_copy(update={"weights": weights(acoustic=0.8)})
    else:
        invalid = output.model_copy(
            update={
                "target_band": SupervisorTargetBand(
                    cpu_temperature=TemperatureTarget(lower_c=40.0, upper_c=70.0),
                    gpu_temperature=TemperatureTarget(lower_c=40.0, upper_c=72.0),
                )
            }
        )

    decision = SupervisorCoordinator(bounded, SimulatedClock(BASE_TS_MS)).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=ReceivedSupervisorOutput(
            output=invalid,
            source_monotonic_ms=10_000,
            received_monotonic_ms=10_000,
        ),
    )

    assert decision.active.output is decision.selected_output
    assert decision.shadow is not None and decision.shadow.output is None
    assert decision.shadow.error is not None
    assert decision.shadow.error.code == "supervisor_output_invalid"


def test_rule_failure_returns_no_context_instead_of_stopping_the_control_loop() -> None:
    class BrokenRule:
        kind = SupervisorPolicyKind.RULE
        version = "rule-test-v1"

        def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
            raise RuntimeError("synthetic rule failure")

    decision = SupervisorCoordinator(
        config(shadow=None),
        SimulatedClock(BASE_TS_MS),
        rule_policy=BrokenRule(),
    ).evaluate(policy_input(), now_monotonic_ms=10_000)

    assert decision.active.error is not None
    assert decision.active.error.code == "supervisor_policy_exception"
    assert decision.selected_output is None


def control_tick(decision: SupervisorDecision, current: SupervisorInput) -> ControlTick:
    """decision を現在 tick の v3 trace に載せる（元 tick の識別子は output 側に残る）。"""
    request = EffectiveZoneDemand(
        requested=0.4,
        effective=0.4,
        bound_by=BoundBy.REQUESTED,
        safety_floor=0.0,
        forced_max=False,
    )
    zone = ZoneRecord(controller_reason=Reason(code="fallback_curve"), demand=request)
    selected = decision.selected_output
    state = ControlState(
        operating_mode=OperatingMode.AUTO,
        authority_stage=AuthorityStage.SHADOW,
        active_controller=ControllerKind.FALLBACK,
        safety_state=SafetyState.NORMAL,
        fallback_active=True,
        supervisor_policy=None if selected is None else selected.policy.value,
        workload_regime=current.workload.regime,
        regime_confidence=current.workload.confidence,
    )
    return ControlTick(
        tick_id=current.snapshot.tick_id,
        ts_ms=current.snapshot.ts_ms,
        state=state,
        zones=PerZone[ZoneRecord](front=zone, rear=zone, top=zone),
        supervisor=decision,
    )


def test_decision_can_be_embedded_in_v3_control_trace_with_shadow_output() -> None:
    current = policy_input()
    decision = SupervisorCoordinator(config(), SimulatedClock(BASE_TS_MS)).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=rl_candidate(current),
    )
    request = EffectiveZoneDemand(
        requested=0.4,
        effective=0.4,
        bound_by=BoundBy.REQUESTED,
        safety_floor=0.0,
        forced_max=False,
    )
    zone = ZoneRecord(controller_reason=Reason(code="fallback_curve"), demand=request)
    state = ControlState(
        operating_mode=OperatingMode.AUTO,
        authority_stage=AuthorityStage.SHADOW,
        active_controller=ControllerKind.FALLBACK,
        safety_state=SafetyState.NORMAL,
        fallback_active=True,
        supervisor_policy=SupervisorPolicyKind.RULE.value,
        workload_regime=current.workload.regime,
        regime_confidence=current.workload.confidence,
    )

    tick = ControlTick(
        tick_id=current.snapshot.tick_id,
        ts_ms=current.snapshot.ts_ms,
        state=state,
        zones=PerZone[ZoneRecord](front=zone, rear=zone, top=zone),
        supervisor=decision,
    )

    # v3 で Supervisor decision を追加した。以後の版（v4: #78 の fault code）でもそのまま載る。
    assert tick.schema_version >= 3
    assert tick.supervisor is decision
    assert tick.supervisor.shadow is not None
    with pytest.raises(ValidationError, match="schema version 3"):
        ControlTick(
            schema_version=2,
            tick_id=tick.tick_id,
            ts_ms=tick.ts_ms,
            state=state,
            zones=tick.zones,
            supervisor=decision,
        )


def test_active_shadow_outputs_must_share_input_identity_and_regime() -> None:
    current = policy_input()
    decision = SupervisorCoordinator(config(), SimulatedClock(BASE_TS_MS)).evaluate(
        current,
        now_monotonic_ms=10_000,
        rl_candidate=rl_candidate(current),
    )
    assert decision.shadow is not None and decision.shadow.output is not None
    mismatched = decision.shadow.output.model_copy(update={"regime": WorkloadRegime.IDLE})

    with pytest.raises(ValidationError, match="同じ workload regime"):
        SupervisorDecision(
            tick_id=decision.tick_id,
            ts_ms=decision.ts_ms,
            snapshot_schema_version=decision.snapshot_schema_version,
            active=decision.active,
            shadow=decision.shadow.model_copy(update={"output": mismatched}),
        )


def test_supervisor_input_rejects_future_or_mismatched_context() -> None:
    current = snapshot()
    future = snapshot(tick=8, mono=11_000)

    with pytest.raises(ValidationError, match="未来"):
        SupervisorInput(
            snapshot=current,
            recent_history=(future,),
            workload=workload(current),
        )
    with pytest.raises(ValidationError, match="tick_id"):
        SupervisorInput(
            snapshot=current,
            workload=workload(future),
        )


def test_output_schema_version_and_policy_are_strict() -> None:
    payload = FakeRLPolicy(SimulatedClock(BASE_TS_MS)).propose(policy_input()).model_dump()
    payload["schema_version"] = 2
    with pytest.raises(ValidationError, match="schema_version"):
        SupervisorOutput.model_validate(payload)

    assert payload["tick_id"] == snapshot().tick_id
