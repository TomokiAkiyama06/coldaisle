"""Fan 制御の層間の型（#76 / 決定記録 0028 §2.3〜§2.5）。

受入基準:

- Front / Rear / Top で同一 schema を使用できる
- `requested != effective` の理由を必ず追跡できる
- Raw PWM を上位 Controller API が受け付けない
- Mock / Replay で実機なしテストが可能（純粋な型なので実機に依存しない）
- schema の互換性テストがある
"""

import ast
import hashlib
import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.control import (
    BOUND_BY_PRECEDENCE,
    SCHEMA_VERSION,
    AirBalanceRecord,
    AuthorityLimitSource,
    AuthorityRecord,
    AuthorityStage,
    BoundBy,
    ConfidenceLevel,
    ControlConfigDigest,
    ControllerKind,
    ControllerProposal,
    ControlState,
    ControlTick,
    ControlTickRuntime,
    EffectiveZoneDemand,
    Fault,
    FaultCode,
    GuardZoneOutput,
    ModeCommandRecord,
    ModelGateDecision,
    OperatingMode,
    OptimizerStatus,
    PerZone,
    Reason,
    RegistryPointerChange,
    RegistryProductionPointer,
    RegistryProvenance,
    SafetyProvenance,
    SafetyState,
    SafetyZoneOutput,
    WorkloadRegime,
    Zone,
    ZoneRecord,
    ZoneRequest,
)
from coldaisle.control.schema import (
    MODEL_GATE_ASSESSMENT_COMPONENTS,
    REGISTRY_ARTIFACT_KINDS,
    AirBalanceCoordinationRecord,
    registry_reason_sha256,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "control_tick_v1.json"
NOW_MS = 1_787_616_000_000
REASON = Reason(code="test_reason")


def passthrough(value: float = 0.4) -> EffectiveZoneDemand:
    return EffectiveZoneDemand(
        requested=value,
        effective=value,
        bound_by=BoundBy.REQUESTED,
        safety_floor=0.0,
        forced_max=False,
    )


def forced() -> EffectiveZoneDemand:
    return EffectiveZoneDemand(
        requested=0.3,
        effective=1.0,
        bound_by=BoundBy.FORCED_MAX,
        safety_floor=0.2,
        forced_max=True,
        reasons=(Reason(code="emergency"),),
    )


def record(demand: EffectiveZoneDemand) -> ZoneRecord:
    return ZoneRecord(controller_reason=REASON, demand=demand)


def zones(demand: EffectiveZoneDemand) -> PerZone[ZoneRecord]:
    return PerZone[ZoneRecord](front=record(demand), rear=record(demand), top=record(demand))


def fallback_state(**overrides) -> ControlState:
    values = {
        "operating_mode": OperatingMode.AUTO,
        "authority_stage": AuthorityStage.SHADOW,
        "active_controller": ControllerKind.FALLBACK,
        "safety_state": SafetyState.NORMAL,
        "fallback_active": True,
    }
    return ControlState(**(values | overrides))


def requests(value: float = 0.4) -> PerZone[ZoneRequest]:
    request = ZoneRequest(demand=value, reason=REASON)
    return PerZone[ZoneRequest](front=request, rear=request, top=request)


def contains_key(schema: object, key: str) -> bool:
    if isinstance(schema, dict):
        return key in schema or any(contains_key(value, key) for value in schema.values())
    if isinstance(schema, list):
        return any(contains_key(value, key) for value in schema)
    return False


# ---------------------------------------------------------------- demand の値域


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_demand_accepts_the_unit_range(value):
    assert ZoneRequest(demand=value, reason=REASON).demand == value


@pytest.mark.parametrize("value", [-0.01, 1.01, math.nan, math.inf, -math.inf])
def test_demand_rejects_out_of_range_and_non_finite(value):
    """**NaN / 無限大は拒否する**（0028 §2.1）。

    NaN は比較が常に偽になり、floor の検査をすり抜ける。
    """
    with pytest.raises(ValidationError):
        ZoneRequest(demand=value, reason=REASON)


@pytest.mark.parametrize("value", ["0.5", True])
def test_demand_is_not_coerced_from_strings_or_booleans(value):
    """設定や外部入力の取り違えを、それらしい値に変えて通さない。"""
    with pytest.raises(ValidationError):
        ZoneRequest(demand=value, reason=REASON)


# ---------------------------------------------------------------- 上位層は PWM を持たない


def test_the_controller_request_refuses_raw_pwm():
    """**Raw PWM を上位 Controller API が受け付けない**（#76 受入基準 / 0028 §2.3）。"""
    with pytest.raises(ValidationError, match="pwm"):
        ZoneRequest(demand=0.5, reason=REASON, pwm=128)


@pytest.mark.parametrize(
    "model",
    [ZoneRequest, ControllerProposal, GuardZoneOutput, SafetyZoneOutput, EffectiveZoneDemand],
)
def test_upper_layer_types_have_no_pwm_field(model):
    """制御器・Guard・Safety・合成の型のどこにも PWM の欄が無い。"""
    schema = model.model_json_schema()
    assert not contains_key(schema, "pwm")
    assert not contains_key(schema, "pwm_raw")


# ---------------------------------------------------------------- 3つの zone で同じ型


def test_every_zone_uses_the_same_type():
    per_zone = requests(0.4)
    assert [per_zone.get(zone) for zone in Zone] == [per_zone.front, per_zone.rear, per_zone.top]


def test_a_zone_cannot_be_missing():
    request = ZoneRequest(demand=0.4, reason=REASON)
    with pytest.raises(ValidationError, match="top"):
        PerZone[ZoneRequest](front=request, rear=request)


def test_no_other_zone_can_be_added():
    """AIO Pump は制御対象外（決定記録 0026）。"""
    request = ZoneRequest(demand=0.4, reason=REASON)
    with pytest.raises(ValidationError, match="pump"):
        PerZone[ZoneRequest](front=request, rear=request, top=request, pump=request)


def test_records_are_immutable():
    request = ZoneRequest(demand=0.4, reason=REASON)
    with pytest.raises(ValidationError):
        request.demand = 0.1


# ---------------------------------------- requested と effective の関係（0028 §2.4）


def test_an_unchanged_demand_needs_no_reason():
    assert passthrough(0.4).reasons == ()


def test_a_changed_demand_without_a_reason_is_rejected():
    """**requested と effective が違えば、理由が必ず残る**（#76 受入基準）。"""
    with pytest.raises(ValidationError, match="理由"):
        EffectiveZoneDemand(
            requested=0.3,
            effective=0.35,
            bound_by=BoundBy.SAFETY_FLOOR,
            safety_floor=0.35,
            forced_max=False,
        )


def test_bound_by_requested_must_equal_the_requested_value():
    with pytest.raises(ValidationError, match="requested"):
        EffectiveZoneDemand(
            requested=0.3,
            effective=0.5,
            bound_by=BoundBy.REQUESTED,
            safety_floor=0.0,
            forced_max=False,
        )


def test_effective_never_drops_below_the_safety_floor():
    with pytest.raises(ValidationError, match="Critical Safety"):
        EffectiveZoneDemand(
            requested=0.1,
            effective=0.1,
            bound_by=BoundBy.REQUESTED,
            safety_floor=0.3,
            forced_max=False,
        )


def test_the_safety_floor_wins_over_a_lower_guard_ceiling():
    """**floor は常に ceiling に勝つ**（0028 §2.4）。"""
    demand = EffectiveZoneDemand(
        requested=0.8,
        effective=0.4,
        bound_by=BoundBy.SAFETY_FLOOR,
        safety_floor=0.4,
        forced_max=False,
        guard_ceiling=0.2,
        reasons=(Reason(code="zone_min_safe_demand"),),
    )
    assert demand.effective == demand.safety_floor


def test_effective_never_drops_below_the_guard_floor():
    with pytest.raises(ValidationError, match="Guard の floor"):
        EffectiveZoneDemand(
            requested=0.8,
            effective=0.5,
            bound_by=BoundBy.GUARD_CEILING,
            safety_floor=0.2,
            forced_max=False,
            guard_floor=0.6,
            guard_ceiling=0.5,
            reasons=(REASON,),
        )


def test_bound_by_must_point_at_a_value_that_exists():
    with pytest.raises(ValidationError, match="guard_floor"):
        EffectiveZoneDemand(
            requested=0.3,
            effective=0.6,
            bound_by=BoundBy.GUARD_FLOOR,
            safety_floor=0.2,
            forced_max=False,
            reasons=(REASON,),
        )


def test_forced_max_always_means_full_demand():
    """**Max はすべてに勝つ**（0028 §2.4）。"""
    with pytest.raises(ValidationError, match=r"1\.0"):
        EffectiveZoneDemand(
            requested=0.3,
            effective=0.9,
            bound_by=BoundBy.FORCED_MAX,
            safety_floor=0.2,
            forced_max=True,
            reasons=(REASON,),
        )


def test_forced_max_and_its_bound_by_come_together():
    with pytest.raises(ValidationError, match="forced_max"):
        EffectiveZoneDemand(
            requested=0.3,
            effective=1.0,
            bound_by=BoundBy.FORCED_MAX,
            safety_floor=0.2,
            forced_max=False,
            reasons=(REASON,),
        )


def test_the_precedence_lists_the_safety_side_first():
    """同じ値のときに記録する項の順（0028 §2.4）。すべての項を1回ずつ含む。"""
    assert BOUND_BY_PRECEDENCE[0] is BoundBy.FORCED_MAX
    assert BOUND_BY_PRECEDENCE[-1] is BoundBy.REQUESTED
    assert sorted(BOUND_BY_PRECEDENCE) == sorted(BoundBy)


# ---------------------------------------------------------------- 制御器の提案


def test_a_fallback_proposal_carries_no_ml_fields():
    with pytest.raises(ValidationError, match="Fallback"):
        ControllerProposal(
            controller=ControllerKind.FALLBACK,
            seq=1,
            computed_at_ms=NOW_MS,
            requested=requests(),
            confidence=0.9,
        )


def test_a_learned_proposal_must_report_what_the_gate_needs():
    with pytest.raises(ValidationError, match="confidence"):
        ControllerProposal(
            controller=ControllerKind.LEARNED_MPC,
            seq=1,
            computed_at_ms=NOW_MS,
            requested=requests(),
            model_version="0.1.0",
        )


def test_a_complete_learned_proposal_is_accepted():
    proposal = ControllerProposal(
        controller=ControllerKind.LEARNED_MPC,
        seq=7,
        computed_at_ms=NOW_MS,
        requested=requests(0.55),
        model_version="0.1.0",
        confidence=0.82,
        ood=False,
        optimizer_status=OptimizerStatus.OK,
        latency_ms=340,
        inference_id="c" * 64,
    )
    assert proposal.requested.get(Zone.TOP).demand == 0.55


# ---------------------------------------------------------------- Guard / Safety の出力


def test_a_guard_intervention_must_say_why_and_until_when():
    with pytest.raises(ValidationError, match="hold_until_mono_ms"):
        GuardZoneOutput(floor=0.6)


def test_a_quiet_guard_carries_nothing():
    assert GuardZoneOutput() == GuardZoneOutput(floor=None, ceiling=None)
    with pytest.raises(ValidationError, match="介入していない"):
        GuardZoneOutput(reason=REASON)


def test_forced_max_from_safety_must_say_why():
    with pytest.raises(ValidationError, match="reason"):
        SafetyZoneOutput(floor=0.3, forced_max=True)


@pytest.mark.parametrize("code", [FaultCode.TACH_STALL, FaultCode.WRITE_FAILURE])
def test_fan_faults_name_their_zone(code):
    with pytest.raises(ValidationError, match="zone が要る"):
        Fault(code=code)
    assert Fault(code=code, zone=Zone.TOP).zone is Zone.TOP


@pytest.mark.parametrize("code", [FaultCode.CPU_TELEMETRY_STALE, FaultCode.CONFIG_INVALID])
def test_system_faults_do_not_name_a_zone(code):
    with pytest.raises(ValidationError, match="紐づかない"):
        Fault(code=code, zone=Zone.FRONT)


# ---------------------------------------------------------------- 制御全体の状態（0028 §2.5）


def test_fallback_in_shadow_needs_no_reason():
    """M8 の既定の運転。ML を使える状況ではないので、理由は要らない。"""
    assert fallback_state().fallback_reason is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"authority_stage": AuthorityStage.SHADOW},
        {"safety_state": SafetyState.DEGRADED},
    ],
)
def test_ml_cannot_be_active_outside_its_allowed_conditions(overrides):
    values = {
        "operating_mode": OperatingMode.AUTO,
        "authority_stage": AuthorityStage.LIMITED,
        "safety_state": SafetyState.NORMAL,
    } | overrides
    with pytest.raises(ValidationError, match="ML を使えるのは"):
        ControlState(
            **values,
            active_controller=ControllerKind.LEARNED_MPC,
            fallback_active=False,
            model_version="0.1.0",
            model_confidence=0.9,
            model_ood=False,
        )


def test_max_cannot_mark_a_counterfactual_learned_proposal_as_active():
    with pytest.raises(ValidationError, match="AUTO"):
        ControlState(
            operating_mode=OperatingMode.MAX,
            authority_stage=AuthorityStage.LIMITED,
            safety_state=SafetyState.NORMAL,
            active_controller=ControllerKind.LEARNED_MPC,
            fallback_active=False,
            model_version="0.1.0",
            model_confidence=0.9,
            model_ood=False,
        )

    state = fallback_state(
        operating_mode=OperatingMode.MAX,
        authority_stage=AuthorityStage.LIMITED,
    )
    assert state.active_controller is ControllerKind.FALLBACK
    assert state.fallback_reason is None


def test_fallback_while_ml_was_allowed_must_say_why():
    with pytest.raises(ValidationError, match="Fallback にした理由"):
        fallback_state(authority_stage=AuthorityStage.LIMITED)
    state = fallback_state(
        authority_stage=AuthorityStage.LIMITED,
        fallback_reason=Reason(code="low_confidence"),
        model_version="0.1.0",
        model_confidence=0.31,
        model_ood=False,
    )
    assert state.fallback_active


def test_fallback_active_cannot_contradict_the_active_controller():
    with pytest.raises(ValidationError, match="食い違って"):
        fallback_state(fallback_active=False)


def test_an_out_of_distribution_model_cannot_stay_in_control():
    """**学習不足・未知状態を通常状態として扱わない**（AGENTS.md ルール4 / 0028 §2.5 (c)）。"""
    learned = {
        "operating_mode": OperatingMode.AUTO,
        "authority_stage": AuthorityStage.LIMITED,
        "safety_state": SafetyState.NORMAL,
        "active_controller": ControllerKind.LEARNED_MPC,
        "fallback_active": False,
        "model_version": "0.1.0",
        "model_confidence": 0.9,
    }
    with pytest.raises(ValidationError, match="OOD"):
        ControlState(**learned, model_ood=True)
    assert ControlState(**learned, model_ood=False).active_controller is ControllerKind.LEARNED_MPC


@pytest.mark.parametrize("mode", [OperatingMode.MANUAL, OperatingMode.CALIBRATION])
def test_people_set_the_request_in_manual_and_calibration(mode):
    """requested を作るのは人や測定計画。**Fallback が動いたことにしない。**"""
    state = fallback_state(operating_mode=mode, active_controller=None, fallback_active=False)
    assert state.active_controller is None
    assert not state.fallback_active
    with pytest.raises(ValidationError, match="active_controller"):
        fallback_state(operating_mode=mode)


def test_auto_always_names_its_controller():
    with pytest.raises(ValidationError, match="active_controller"):
        fallback_state(active_controller=None, fallback_active=False)


def test_a_fallback_reason_belongs_only_to_fallback():
    with pytest.raises(ValidationError, match="fallback_reason"):
        fallback_state(
            operating_mode=OperatingMode.MANUAL,
            active_controller=None,
            fallback_active=False,
            fallback_reason=REASON,
        )


# ---------------------------------------- requested を下げられるのは ceiling だけ


def test_only_the_guard_ceiling_can_lower_the_request():
    with pytest.raises(ValidationError, match="下げられている"):
        EffectiveZoneDemand(
            requested=0.8,
            effective=0.5,
            bound_by=BoundBy.SAFETY_FLOOR,
            safety_floor=0.5,
            forced_max=False,
            reasons=(REASON,),
        )
    # ceiling で 0.6 まで下げたなら、それより低い floor（0.5）には下がらない
    with pytest.raises(ValidationError, match="下げられている"):
        EffectiveZoneDemand(
            requested=0.8,
            effective=0.5,
            bound_by=BoundBy.SAFETY_FLOOR,
            safety_floor=0.5,
            forced_max=False,
            guard_ceiling=0.6,
            reasons=(REASON,),
        )


def test_a_ceiling_never_raises_the_request():
    with pytest.raises(ValidationError, match="上げない"):
        EffectiveZoneDemand(
            requested=0.3,
            effective=0.6,
            bound_by=BoundBy.GUARD_CEILING,
            safety_floor=0.2,
            forced_max=False,
            guard_ceiling=0.6,
            reasons=(REASON,),
        )


LEGACY_CONTROL_TICK_RUNTIME = ControlTickRuntime(
    schema_version=1,
    tick_period_ms=1_000,
    deadline_ms=500,
    duration_ms=10,
    deadline_exceeded=False,
    snapshot_schema_version=1,
    config=ControlConfigDigest(
        fan_hardware_sha256="0" * 64, safety_sha256="1" * 64, policy_sha256="2" * 64
    ),
)
"""v8〜v10 の `ControlTick` に必須の実行記録（runtime v1。#74 / 決定記録 0060 §2.4）。

**版が中身を表すことを型で縛った**ので、v8 を名乗る記録はこの欄を省けない。
値そのものはこの試験の判断に影響しない。
"""

AIR_BALANCE_SHA256 = "3" * 64
"""試験の `air-balance.yaml` の SHA-256（runtime v2 と `air_balance` で揃える）。"""

CONTROL_TICK_RUNTIME = ControlTickRuntime(
    tick_period_ms=1_000,
    deadline_ms=500,
    duration_ms=10,
    deadline_exceeded=False,
    snapshot_schema_version=1,
    config=ControlConfigDigest(
        fan_hardware_sha256="0" * 64,
        safety_sha256="1" * 64,
        policy_sha256="2" * 64,
        air_balance_sha256=AIR_BALANCE_SHA256,
        control_config_version=11,
        fan_hardware_schema_version=1,
        safety_schema_version=3,
        policy_schema_version=9,
        air_balance_schema_version=2,
    ),
)
"""v11 の `ControlTick` に必須の実行記録（runtime v2。#81 / 決定記録 0073 §2.5 (c)）。"""

AIR_BALANCE_RECORD = AirBalanceRecord.disabled(
    model_id="provisional-air-balance", config_sha256=AIR_BALANCE_SHA256
)
"""v11 の `ControlTick` に必須の Air Balance の記録（未校正で無効。決定記録 0073 §2.5 (b)）。"""

MODE_COMMAND_RECORD = ModeCommandRecord.without_entry()
"""v12 の `ControlTick` に必須のモードの出どころ（入口を開いていない構成。決定記録 0072 §2.7）。"""

COORDINATION_RECORD = AirBalanceCoordinationRecord.off()
"""v14 の `ControlTick` に必須の Air Balance の協調の記録（`mode: off`。決定記録 0078 §2.7）。"""

AUTHORITY_RECORD = AuthorityRecord(entry="static", config_ceiling=AuthorityStage.FULL)
"""v13 の `ControlTick` に必須の制御権の出どころ（journal を持たない構成。決定記録 0072 §2.6）。

設定の上限を `full` にして、どの試験の stage も上限を超えないようにする。
"""


def runtime_for(schema_version: int) -> ControlTickRuntime | None:
    """その版の `ControlTick` が持つべき runtime（v8 未満は無し、v11 からは v2）。"""
    if schema_version < 8:
        return None
    return CONTROL_TICK_RUNTIME if schema_version >= 11 else LEGACY_CONTROL_TICK_RUNTIME


SAFETY_PROVENANCE = SafetyProvenance(disabled_inputs=(), config_is_provisional=False)
"""v9 の `ControlTick` に必須の Critical Safety の裁定の前提（#78）。

v8 の runtime と同じく、v9 を名乗る記録はこの欄を省けない。値はこの試験の判断に影響しない。
"""


def registry_provenance_for(artifact: str) -> RegistryProvenance:
    """thermal_model の production を `artifact` にした v10 の registry の版（#104）。"""
    return RegistryProvenance(
        revision=3,
        production={
            "thermal_model": RegistryProductionPointer(
                artifact_sha256=artifact,
                established_by=RegistryPointerChange(
                    registry_revision=3,
                    occurred_at_ms=1_000,
                    event="promoted",
                    artifact_kind="thermal_model",
                    model_id="thermal-test",
                    model_version="0.1.0",
                    actor="operator",
                    reason_sha256=registry_reason_sha256("試験用の昇格"),
                    previous_artifact=None,
                    rollback_target=None,
                    approver="operator",
                    approved_at_ms=500,
                    approval_artifact_sha256=artifact,
                ),
            ),
        }
        | {
            kind: RegistryProductionPointer(artifact_sha256=None, established_by=None)
            for kind in ("confidence_model", "supervisor_policy", "feature_transform")
        },
    )


REGISTRY_PROVENANCE = registry_provenance_for("a" * 64)
"""v10 の `ControlTick` に必須の Model Registry の版（#104 / 決定記録 0071 §2.5）。

v9 と同じく、v10 を名乗る記録はこの欄を省けない。thermal_model の production を試験の
artifact（`"a" * 64`）にしてある。裏づけのある `model_gate` は production と一致しなければ
ならない（決定記録 0052 §2.1 の3。codex #4133594515）ため。
"""

# ---------------------------------------------------------------- 1 tick の記録


def tick(demand: EffectiveZoneDemand, faults=(), **state_overrides) -> ControlTick:
    return ControlTick(
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(**state_overrides),
        zones=zones(demand),
        faults=faults,
        runtime=CONTROL_TICK_RUNTIME,
        safety_provenance=SAFETY_PROVENANCE,
        registry=REGISTRY_PROVENANCE,
        air_balance=AIR_BALANCE_RECORD,
        mode_command=MODE_COMMAND_RECORD,
        authority=AUTHORITY_RECORD,
        air_balance_coordination=COORDINATION_RECORD,
        tach_unconfirmed_zones=(),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"safety_state": SafetyState.STARTUP},
        {"operating_mode": OperatingMode.MAX},
    ],
)
def test_all_zones_are_max_when_the_state_says_so(overrides):
    with pytest.raises(ValidationError, match="すべての zone が Max"):
        tick(passthrough(), **overrides)
    assert tick(forced(), **overrides).zones.get(Zone.FRONT).demand.effective == 1.0


def test_emergency_needs_a_cause_and_all_zones_at_max():
    with pytest.raises(ValidationError, match="fault"):
        tick(forced(), safety_state=SafetyState.EMERGENCY)
    recorded = tick(
        forced(),
        faults=(Fault(code=FaultCode.TACH_STALL, zone=Zone.TOP),),
        safety_state=SafetyState.EMERGENCY,
    )
    assert recorded.faults[0].zone is Zone.TOP


def test_normal_cannot_carry_faults():
    with pytest.raises(ValidationError, match="NORMAL"):
        tick(passthrough(), faults=(Fault(code=FaultCode.TICK_OVERRUN),))


def test_auto_must_apply_the_guard_ceiling():
    """AUTO で ceiling を無視した記録を通さない（0028 §2.4）。"""
    ignored = EffectiveZoneDemand(
        requested=0.8,
        effective=0.8,
        bound_by=BoundBy.REQUESTED,
        safety_floor=0.2,
        forced_max=False,
        guard_ceiling=0.5,
    )
    with pytest.raises(ValidationError, match="ceiling を掛けていない"):
        tick(ignored)
    applied = EffectiveZoneDemand(
        requested=0.8,
        effective=0.5,
        bound_by=BoundBy.GUARD_CEILING,
        safety_floor=0.2,
        forced_max=False,
        guard_ceiling=0.5,
        reasons=(Reason(code="hunting_suppression"),),
    )
    assert tick(applied).zones.front.demand.effective == 0.5


def test_the_guard_ceiling_is_not_applied_to_values_people_set():
    """ceiling はハンチング抑制のためのもので、人が指定した値は下げない（0028 §2.4）。"""
    manual = {
        "operating_mode": OperatingMode.MANUAL,
        "active_controller": None,
        "fallback_active": False,
    }
    lowered = EffectiveZoneDemand(
        requested=0.8,
        effective=0.5,
        bound_by=BoundBy.GUARD_CEILING,
        safety_floor=0.2,
        forced_max=False,
        guard_ceiling=0.5,
        reasons=(REASON,),
    )
    with pytest.raises(ValidationError, match="AUTO だけ"):
        tick(lowered, **manual)
    assert tick(passthrough(0.8), **manual).zones.top.demand.effective == 0.8


@pytest.mark.parametrize(
    "fault",
    [
        Fault(code=FaultCode.TACH_STALL, zone=Zone.TOP),
        Fault(code=FaultCode.WRITE_FAILURE, zone=Zone.TOP),
        Fault(code=FaultCode.READBACK_MISMATCH, zone=Zone.TOP),
        Fault(code=FaultCode.FALLBACK_EXCEPTION),
        Fault(code=FaultCode.GUARD_EXCEPTION),
        Fault(code=FaultCode.CONFIG_INVALID),
    ],
    ids=lambda fault: f"{fault.code.value}-{fault.zone}",
)
def test_unconditional_emergency_faults_cannot_be_recorded_as_degraded(fault):
    """0028 §2.7 で `EMERGENCY` と決めた故障。Top は CPU の冷却を担う。"""
    with pytest.raises(ValidationError, match="EMERGENCY"):
        tick(forced(), faults=(fault,), safety_state=SafetyState.DEGRADED)
    recorded = tick(forced(), faults=(fault,), safety_state=SafetyState.EMERGENCY)
    assert recorded.state.safety_state is SafetyState.EMERGENCY


def test_a_front_or_rear_stall_can_stay_degraded():
    front_at_max = PerZone[ZoneRecord](
        front=record(forced()), rear=record(passthrough()), top=record(passthrough())
    )
    recorded = ControlTick(
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(safety_state=SafetyState.DEGRADED),
        zones=front_at_max,
        faults=(Fault(code=FaultCode.TACH_STALL, zone=Zone.FRONT),),
        runtime=CONTROL_TICK_RUNTIME,
        safety_provenance=SAFETY_PROVENANCE,
        registry=REGISTRY_PROVENANCE,
        air_balance=AIR_BALANCE_RECORD,
        mode_command=MODE_COMMAND_RECORD,
        authority=AUTHORITY_RECORD,
        air_balance_coordination=COORDINATION_RECORD,
        tach_unconfirmed_zones=(),
    )
    assert recorded.state.safety_state is SafetyState.DEGRADED


def test_a_stalled_fan_is_driven_to_max():
    with pytest.raises(ValidationError, match="tach stall"):
        tick(
            passthrough(),
            faults=(Fault(code=FaultCode.TACH_STALL, zone=Zone.REAR),),
            safety_state=SafetyState.DEGRADED,
        )


def test_stale_cpu_temperature_drives_top_to_max():
    faults = (Fault(code=FaultCode.CPU_TELEMETRY_STALE),)
    with pytest.raises(ValidationError, match="CPU 温度"):
        tick(passthrough(), faults=faults, safety_state=SafetyState.DEGRADED)
    top_at_max = PerZone[ZoneRecord](
        front=record(passthrough()), rear=record(passthrough()), top=record(forced())
    )
    recorded = ControlTick(
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(safety_state=SafetyState.DEGRADED),
        zones=top_at_max,
        faults=faults,
        runtime=CONTROL_TICK_RUNTIME,
        safety_provenance=SAFETY_PROVENANCE,
        registry=REGISTRY_PROVENANCE,
        air_balance=AIR_BALANCE_RECORD,
        mode_command=MODE_COMMAND_RECORD,
        authority=AUTHORITY_RECORD,
        air_balance_coordination=COORDINATION_RECORD,
        tach_unconfirmed_zones=(),
    )
    assert recorded.zones.top.demand.forced_max


# ---------------------------------------------------------------- 互換性（#82 の保存データ）


def test_the_stored_v1_record_still_loads_unchanged():
    """**保存済みの v1 の記録が、読めて、同じ形で書き戻せる。**

    ここが落ちたら、フィールドの名前か意味が変わっている。#82 が保存したデータを読み違えるので、
    `SCHEMA_VERSION` を上げて移行を用意してから fixture を更新する。
    """
    stored = FIXTURE.read_text(encoding="utf-8")
    tick = ControlTick.model_validate_json(stored)
    assert tick.schema_version == 1
    assert SCHEMA_VERSION == 14
    assert json.loads(tick.model_dump_json()) == json.loads(stored)


def test_v4_trace_records_the_absolute_temperature_limit():
    recorded = tick(
        forced(),
        faults=(Fault(code=FaultCode.ABSOLUTE_TEMPERATURE_LIMIT),),
        safety_state=SafetyState.EMERGENCY,
    )

    assert recorded.schema_version == SCHEMA_VERSION
    restored = ControlTick.model_validate_json(recorded.model_dump_json())
    assert restored.faults[0].code is FaultCode.ABSOLUTE_TEMPERATURE_LIMIT
    with pytest.raises(ValidationError, match="EMERGENCY"):
        tick(
            forced(),
            faults=(Fault(code=FaultCode.ABSOLUTE_TEMPERATURE_LIMIT),),
            safety_state=SafetyState.DEGRADED,
        )


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_legacy_trace_cannot_carry_a_fault_code_added_in_v4(schema_version: int):
    with pytest.raises(ValidationError, match="schema version 4"):
        ControlTick(
            schema_version=schema_version,
            tick_id=1,
            ts_ms=NOW_MS,
            state=fallback_state(safety_state=SafetyState.EMERGENCY),
            zones=zones(forced()),
            faults=(Fault(code=FaultCode.ABSOLUTE_TEMPERATURE_LIMIT),),
        )


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_top_enable_revert_is_emergency_in_v4_but_legacy_records_still_load(
    schema_version: int,
):
    top_revert = (Fault(code=FaultCode.ENABLE_REVERTED, zone=Zone.TOP),)
    with pytest.raises(ValidationError, match="EMERGENCY"):
        tick(forced(), faults=top_revert, safety_state=SafetyState.DEGRADED)

    stored = ControlTick(
        schema_version=schema_version,
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(safety_state=SafetyState.DEGRADED),
        zones=zones(forced()),
        faults=top_revert,
    )
    restored = ControlTick.model_validate_json(stored.model_dump_json())
    assert restored.schema_version == schema_version
    assert restored.state.safety_state is SafetyState.DEGRADED


def test_current_trace_stores_workload_regime_and_confidence_together():
    state = fallback_state(
        workload_regime=WorkloadRegime.SUSTAINED_GPU,
        regime_confidence=0.85,
    )
    recorded = ControlTick(
        tick_id=1,
        ts_ms=NOW_MS,
        state=state,
        zones=zones(passthrough()),
        runtime=CONTROL_TICK_RUNTIME,
        safety_provenance=SAFETY_PROVENANCE,
        registry=REGISTRY_PROVENANCE,
        air_balance=AIR_BALANCE_RECORD,
        mode_command=MODE_COMMAND_RECORD,
        authority=AUTHORITY_RECORD,
        air_balance_coordination=COORDINATION_RECORD,
        tach_unconfirmed_zones=(),
    )

    payload = json.loads(recorded.model_dump_json())
    assert recorded.schema_version == SCHEMA_VERSION
    assert payload["state"]["workload_regime"] == "sustained_gpu"
    assert payload["state"]["regime_confidence"] == 0.85

    with pytest.raises(ValidationError, match="一緒に記録"):
        fallback_state(workload_regime=WorkloadRegime.UNKNOWN)
    with pytest.raises(ValidationError, match="schema version 2"):
        ControlTick(
            schema_version=1,
            tick_id=1,
            ts_ms=NOW_MS,
            state=state,
            zones=zones(passthrough()),
        )


def test_v2_trace_keeps_simultaneous_transient_load_distinct():
    state = fallback_state(
        workload_regime=WorkloadRegime.TRANSIENT_CPU_GPU,
        regime_confidence=0.4,
    )
    recorded = ControlTick(
        tick_id=1,
        ts_ms=NOW_MS,
        state=state,
        zones=zones(passthrough()),
        runtime=CONTROL_TICK_RUNTIME,
        safety_provenance=SAFETY_PROVENANCE,
        registry=REGISTRY_PROVENANCE,
        air_balance=AIR_BALANCE_RECORD,
        mode_command=MODE_COMMAND_RECORD,
        authority=AUTHORITY_RECORD,
        air_balance_coordination=COORDINATION_RECORD,
        tach_unconfirmed_zones=(),
    )

    payload = json.loads(recorded.model_dump_json())
    assert payload["state"]["workload_regime"] == "transient_cpu_gpu"
    restored = ControlTick.model_validate_json(recorded.model_dump_json())
    assert restored.state.workload_regime is WorkloadRegime.TRANSIENT_CPU_GPU


def test_fallback_trace_remains_valid_when_regime_is_not_available():
    recorded = ControlTick(
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(),
        zones=zones(passthrough()),
        runtime=CONTROL_TICK_RUNTIME,
        safety_provenance=SAFETY_PROVENANCE,
        registry=REGISTRY_PROVENANCE,
        air_balance=AIR_BALANCE_RECORD,
        mode_command=MODE_COMMAND_RECORD,
        authority=AUTHORITY_RECORD,
        air_balance_coordination=COORDINATION_RECORD,
        tach_unconfirmed_zones=(),
    )

    assert recorded.schema_version == SCHEMA_VERSION
    assert recorded.state.workload_regime is None


def test_stored_v2_trace_without_supervisor_decision_remains_valid():
    recorded = ControlTick(
        schema_version=2,
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(
            workload_regime=WorkloadRegime.SUSTAINED_GPU,
            regime_confidence=0.85,
        ),
        zones=zones(passthrough()),
    )

    payload = json.loads(recorded.model_dump_json())
    assert payload["schema_version"] == 2
    assert "supervisor" not in payload


@pytest.mark.parametrize("schema_version", [1, 2])
def test_legacy_trace_preserves_free_form_supervisor_policy(schema_version: int):
    stored = json.loads(FIXTURE.read_text(encoding="utf-8"))
    stored["schema_version"] = schema_version
    stored["state"]["supervisor_policy"] = "legacy_rule_v1"

    tick = ControlTick.model_validate_json(json.dumps(stored))

    assert tick.state.supervisor_policy == "legacy_rule_v1"
    assert json.loads(tick.model_dump_json()) == stored


def test_v3_supervisor_policy_requires_a_matching_decision():
    with pytest.raises(ValidationError, match="Supervisor decision"):
        ControlTick(
            tick_id=1,
            ts_ms=NOW_MS,
            state=fallback_state(supervisor_policy="legacy_rule_v1"),
            zones=zones(passthrough()),
            runtime=CONTROL_TICK_RUNTIME,
            safety_provenance=SAFETY_PROVENANCE,
            registry=REGISTRY_PROVENANCE,
            air_balance=AIR_BALANCE_RECORD,
            mode_command=MODE_COMMAND_RECORD,
            authority=AUTHORITY_RECORD,
            air_balance_coordination=COORDINATION_RECORD,
            tach_unconfirmed_zones=(),
        )


def test_a_v8_tick_cannot_claim_the_version_without_its_payload():
    """**版が中身を表す。** v8 を名乗りながら v8 を定義する欄が無い記録を作れない。

    ここが破れると、読む側は `schema_version` を見ても何が入っているか言えない
    （#82 の保存データと #91 の評価が、無い欄を「古い記録」と区別できなくなる）。
    """
    with pytest.raises(ValidationError, match="runtime が要る"):
        ControlTick(
            tick_id=1,
            ts_ms=NOW_MS,
            state=fallback_state(),
            zones=zones(passthrough()),
        )
    # 保存済みの v1〜v7 は runtime を持たないまま読める。
    stored = ControlTick(
        schema_version=6,
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(),
        zones=zones(passthrough()),
    )
    assert stored.runtime is None
    with pytest.raises(ValidationError, match="schema version 8"):
        ControlTick(
            schema_version=6,
            tick_id=1,
            ts_ms=NOW_MS,
            state=fallback_state(),
            zones=zones(passthrough()),
            runtime=LEGACY_CONTROL_TICK_RUNTIME,
        )


def test_a_v9_tick_cannot_claim_the_version_without_the_safety_provenance():
    """**v9 は Critical Safety の裁定の前提を省けない**（#78）。

    無いと、T_SENSOR を外していた期間や暫定値で回っていた期間の trace を、
    確定値で全入力を見ていた trace と区別できない（docs/critical-safety.md）。
    """
    with pytest.raises(ValidationError, match="safety_provenance が要る"):
        ControlTick(
            tick_id=1,
            ts_ms=NOW_MS,
            state=fallback_state(),
            zones=zones(passthrough()),
            runtime=CONTROL_TICK_RUNTIME,
        )


@pytest.mark.parametrize("schema_version", [1, 5, 7])
def test_a_stored_trace_before_v9_loads_without_the_safety_provenance(schema_version: int):
    """保存済みの v1〜v8 は欄を持たないまま読め、書き戻しても欄が現れない（決定記録 0030）。"""
    stored = ControlTick(
        schema_version=schema_version,
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(),
        zones=zones(passthrough()),
    )
    assert stored.safety_provenance is None
    assert "safety_provenance" not in json.loads(stored.model_dump_json())


def test_a_stored_v8_trace_loads_without_the_safety_provenance():
    stored = ControlTick(
        schema_version=8,
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(),
        zones=zones(passthrough()),
        runtime=LEGACY_CONTROL_TICK_RUNTIME,
    )
    restored = ControlTick.model_validate_json(stored.model_dump_json())
    assert restored.schema_version == 8
    assert restored.safety_provenance is None


@pytest.mark.parametrize("schema_version", [7, 8])
def test_a_legacy_trace_cannot_carry_the_safety_provenance_added_in_v9(schema_version: int):
    """v9 の欄を持つ記録に旧い版を名乗らせない。旧い reader は欄を知らない。"""
    with pytest.raises(ValidationError, match="schema version 9"):
        ControlTick(
            schema_version=schema_version,
            tick_id=1,
            ts_ms=NOW_MS,
            state=fallback_state(),
            zones=zones(passthrough()),
            runtime=runtime_for(schema_version),
            safety_provenance=SAFETY_PROVENANCE,
        )


def test_the_v9_trace_keeps_disabled_inputs_and_the_provisional_flag():
    """設定で外した入力の理由と暫定値の有無が、保存と読み戻しで失われない。"""
    provenance = SafetyProvenance(
        disabled_inputs=(Reason(code="t_sensor_disabled", detail="not approved"),),
        config_is_provisional=True,
    )
    recorded = ControlTick(
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(),
        zones=zones(passthrough()),
        runtime=CONTROL_TICK_RUNTIME,
        safety_provenance=provenance,
        registry=REGISTRY_PROVENANCE,
        air_balance=AIR_BALANCE_RECORD,
        mode_command=MODE_COMMAND_RECORD,
        authority=AUTHORITY_RECORD,
        air_balance_coordination=COORDINATION_RECORD,
        tach_unconfirmed_zones=(),
    )
    payload = json.loads(recorded.model_dump_json())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["safety_provenance"] == {
        "schema_version": 1,
        "disabled_inputs": [{"code": "t_sensor_disabled", "detail": "not approved"}],
        "config_is_provisional": True,
    }
    assert ControlTick.model_validate_json(recorded.model_dump_json()) == recorded


@pytest.mark.parametrize("missing", ["disabled_inputs", "config_is_provisional"])
def test_the_safety_provenance_requires_both_fields(missing: str):
    """片方だけの記録を作らない。**既定値で「外していない」「確定値」と読ませない。**"""
    values: dict[str, object] = {"disabled_inputs": [], "config_is_provisional": False}
    del values[missing]
    with pytest.raises(ValidationError):
        SafetyProvenance.model_validate(values)


# ---------------------------------------------------------------- v10: registry の版（#104）

REGISTRY_SHA = "a" * 64


def pointer_change(**overrides: object) -> RegistryPointerChange:
    """`RegistryAuditEvent.tick_trace_metadata()` と同じ欄を持つ promotion の記録。"""
    values: dict[str, object] = {
        "registry_revision": 3,
        "occurred_at_ms": NOW_MS,
        "event": "promoted",
        "artifact_kind": "thermal_model",
        "model_id": "rack-thermal",
        "model_version": "1.0.0",
        "actor": "model-operator",
        "reason_sha256": registry_reason_sha256("shadow evaluation passed"),
        "previous_artifact": None,
        "rollback_target": None,
        "approver": "model-operator",
        "approved_at_ms": NOW_MS - 1_000,
        "approval_artifact_sha256": REGISTRY_SHA,
    }
    return RegistryPointerChange.model_validate(values | overrides)


EMPTY_POINTER = RegistryProductionPointer(artifact_sha256=None, established_by=None)


def bound_registry(**overrides: object) -> RegistryProvenance:
    production = {
        "thermal_model": RegistryProductionPointer(
            artifact_sha256=REGISTRY_SHA, established_by=pointer_change()
        ),
    } | {
        kind: EMPTY_POINTER
        for kind in ("confidence_model", "supervisor_policy", "feature_transform")
    }
    values: dict[str, object] = {"revision": 3, "production": production}
    return RegistryProvenance(**(values | overrides))


def registry_tick(registry: RegistryProvenance, **overrides: object) -> ControlTick:
    values: dict[str, object] = {
        "tick_id": 1,
        "ts_ms": NOW_MS,
        "state": fallback_state(),
        "zones": zones(passthrough()),
        "runtime": CONTROL_TICK_RUNTIME,
        "safety_provenance": SAFETY_PROVENANCE,
        "registry": registry,
        "air_balance": AIR_BALANCE_RECORD,
        "mode_command": MODE_COMMAND_RECORD,
        "authority": AUTHORITY_RECORD,
        "air_balance_coordination": COORDINATION_RECORD,
        "tach_unconfirmed_zones": (),
    }
    return ControlTick(**(values | overrides))


def test_a_v10_tick_round_trips_the_registry_pointer_it_used():
    """**tick が使っていた pointer と revision が、保存と読み戻しで失われない**（0071 §2.5）。"""
    recorded = registry_tick(bound_registry())

    payload = json.loads(recorded.model_dump_json())
    assert payload["schema_version"] == SCHEMA_VERSION == 14
    assert payload["registry"]["revision"] == 3
    thermal = payload["registry"]["production"]["thermal_model"]
    assert thermal["artifact_sha256"] == REGISTRY_SHA
    assert thermal["established_by"]["registry_revision"] == 3
    assert thermal["established_by"]["approver"] == "model-operator"
    # 欄は常に揃える。production の無い kind も None の欄で残す（0062 §2.5）。
    assert payload["registry"]["production"]["confidence_model"] == {
        "artifact_sha256": None,
        "established_by": None,
    }
    assert set(thermal["established_by"]) == {
        "registry_revision",
        "occurred_at_ms",
        "event",
        "artifact_kind",
        "model_id",
        "model_version",
        "actor",
        "reason_sha256",
        "previous_artifact",
        "rollback_target",
        "approver",
        "approved_at_ms",
        "approval_artifact_sha256",
    }
    assert ControlTick.model_validate_json(recorded.model_dump_json()) == recorded


def test_a_v10_tick_carries_only_the_reason_digest():
    """**毎 tick の塊に `reason` の全文を載せない。** digest だけを置く（決定記録 0075）。"""
    reason = "昇格の理由。" * 150  # audit が許す上限（1000字）に近い全文
    change = pointer_change(reason_sha256=registry_reason_sha256(reason))
    recorded = registry_tick(
        bound_registry(
            production={
                "thermal_model": RegistryProductionPointer(
                    artifact_sha256=REGISTRY_SHA, established_by=change
                ),
            }
            | {
                kind: EMPTY_POINTER
                for kind in ("confidence_model", "supervisor_policy", "feature_transform")
            }
        )
    )

    stored = recorded.model_dump_json()
    thermal = json.loads(stored)["registry"]["production"]["thermal_model"]["established_by"]
    assert "reason" not in thermal
    assert thermal["reason_sha256"] == registry_reason_sha256(reason)
    assert "昇格の理由" not in stored
    assert ControlTick.model_validate_json(stored) == recorded


def test_the_reason_digest_is_the_sha256_of_the_utf8_reason():
    """audit の全文から誰でも同じ digest を計算して照合できる（決定記録 0075）。"""
    assert registry_reason_sha256("shadow evaluation passed") == (
        hashlib.sha256(b"shadow evaluation passed").hexdigest()
    )
    assert registry_reason_sha256("回帰を観測") == (
        hashlib.sha256("回帰を観測".encode()).hexdigest()
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "shadow evaluation passed"},
        {"reason_sha256": "not-a-digest"},
        {"previous_artifact": "x" * 241},
        {"rollback_target": "x" * 241},
    ],
)
def test_a_pointer_change_rejects_free_text_and_unbounded_labels(overrides: dict[str, object]):
    """全文の `reason` は受け取らず、識別子の欄も長さを閉じる（決定記録 0075 §2.2）。"""
    with pytest.raises(ValidationError):
        pointer_change(**overrides)


def test_a_v10_tick_without_a_registry_read_records_it_explicitly():
    """registry を読んでいない構成は `revision=None` の塊を持つ。**欄を省いて表さない。**"""
    recorded = registry_tick(RegistryProvenance.unbound())

    payload = json.loads(recorded.model_dump_json())
    assert payload["registry"] == {"schema_version": 1, "revision": None, "production": {}}
    assert ControlTick.model_validate_json(recorded.model_dump_json()) == recorded


def test_a_v10_tick_cannot_claim_the_version_without_the_registry():
    """**v10 は registry の版を省けない**（#104）。無いと、どの pointer の下の判断か言えない。"""
    with pytest.raises(ValidationError, match="registry が要る"):
        ControlTick(
            tick_id=1,
            ts_ms=NOW_MS,
            state=fallback_state(),
            zones=zones(passthrough()),
            runtime=CONTROL_TICK_RUNTIME,
            safety_provenance=SAFETY_PROVENANCE,
        )


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_a_stored_trace_before_v10_loads_without_the_registry(schema_version: int):
    """保存済みの v1〜v9 は欄を持たないまま読め、書き戻しても欄が現れない（決定記録 0030）。"""
    stored = ControlTick(
        schema_version=schema_version,
        tick_id=1,
        ts_ms=NOW_MS,
        state=fallback_state(),
        zones=zones(passthrough()),
        runtime=runtime_for(schema_version),
        safety_provenance=SAFETY_PROVENANCE if schema_version >= 9 else None,
    )
    restored = ControlTick.model_validate_json(stored.model_dump_json())
    assert restored.schema_version == schema_version
    assert restored.registry is None
    assert "registry" not in json.loads(stored.model_dump_json())


@pytest.mark.parametrize("schema_version", [1, 5, 8, 9])
def test_a_legacy_trace_cannot_carry_the_registry_added_in_v10(schema_version: int):
    """v10 の欄を持つ記録に旧い版を名乗らせない。旧い reader は欄を知らない。"""
    with pytest.raises(ValidationError, match="schema version 10"):
        ControlTick(
            schema_version=schema_version,
            tick_id=1,
            ts_ms=NOW_MS,
            state=fallback_state(),
            zones=zones(passthrough()),
            runtime=runtime_for(schema_version),
            safety_provenance=SAFETY_PROVENANCE if schema_version >= 9 else None,
            registry=bound_registry(),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"event": "registered"}, "promotion / rollback だけ"),
        ({"event": "retired"}, "promotion / rollback だけ"),
        ({"approver": None}, "human approval"),
        ({"approved_at_ms": None}, "human approval"),
        ({"approval_artifact_sha256": None}, "checksum"),
        ({"approver": "someone-else"}, "approver"),
        ({"approved_at_ms": NOW_MS + 1}, "未来"),
        ({"event": "rolled_back", "rollback_target": "thermal_model/x/1.0.0"}, "rollback_target"),
    ],
)
def test_a_pointer_change_is_an_approved_promotion_or_rollback(
    overrides: dict[str, object], message: str
):
    with pytest.raises(ValidationError, match=message):
        pointer_change(**overrides)


def test_a_pointer_change_keeps_every_field_of_the_audit_metadata():
    """欄を1つでも落とした記録は受け付けない（「記録されていない」と「起きていない」を混ぜない）。"""
    values = pointer_change().model_dump()
    del values["rollback_target"]
    with pytest.raises(ValidationError):
        RegistryPointerChange.model_validate(values)


def test_a_production_pointer_and_its_change_are_recorded_together():
    with pytest.raises(ValidationError, match="一緒に記録"):
        RegistryProductionPointer(artifact_sha256=REGISTRY_SHA, established_by=None)
    with pytest.raises(ValidationError, match="一緒に記録"):
        RegistryProductionPointer(artifact_sha256=None, established_by=pointer_change())
    with pytest.raises(ValidationError, match="承認した checksum"):
        RegistryProductionPointer(artifact_sha256="b" * 64, established_by=pointer_change())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        # registry を読んでいないのに pointer を持つ
        ({"revision": None}, "読んでいない"),
        # snapshot の revision より後の変更
        ({"revision": 2}, "revision より後"),
        # 別 kind の変更を載せる
        (
            {
                "production": {
                    "thermal_model": EMPTY_POINTER,
                    "confidence_model": RegistryProductionPointer(
                        artifact_sha256=REGISTRY_SHA, established_by=pointer_change()
                    ),
                    "supervisor_policy": EMPTY_POINTER,
                    "feature_transform": EMPTY_POINTER,
                }
            },
            "変更を揃える",
        ),
        # 1つの変更を2つの kind に載せる
        (
            {
                "production": {
                    "thermal_model": RegistryProductionPointer(
                        artifact_sha256=REGISTRY_SHA, established_by=pointer_change()
                    ),
                    "confidence_model": RegistryProductionPointer(
                        artifact_sha256=REGISTRY_SHA,
                        established_by=pointer_change(artifact_kind="confidence_model"),
                    ),
                    "supervisor_policy": EMPTY_POINTER,
                    "feature_transform": EMPTY_POINTER,
                }
            },
            "複数の kind",
        ),
    ],
)
def test_registry_pointers_belong_to_the_recorded_revision(
    overrides: dict[str, object], message: str
):
    with pytest.raises(ValidationError, match=message):
        bound_registry(**overrides)


@pytest.mark.parametrize(
    "production",
    [
        {},
        {"thermal_model": EMPTY_POINTER},
        {kind: EMPTY_POINTER for kind in REGISTRY_ARTIFACT_KINDS} | {"extra_model": EMPTY_POINTER},
    ],
)
def test_a_registry_read_records_every_kind(production: dict[str, RegistryProductionPointer]):
    """registry を読んだなら**全 kind の欄を揃える**（0062 §2.5）。欠けた記録は受け付けない。

    欠けていると「その kind は production が無かった」のか「記録されていない」のかを
    読む側が区別できない。
    """
    with pytest.raises(ValidationError, match="全 kind"):
        RegistryProvenance(revision=3, production=production)


def test_the_registry_revision_cannot_be_negative():
    with pytest.raises(ValidationError):
        RegistryProvenance(revision=-1, production={})


def test_the_stored_record_explains_every_changed_zone():
    tick = ControlTick.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    for zone in Zone:
        demand = tick.zones.get(zone).demand
        if demand.effective != demand.requested:
            assert demand.bound_by is not BoundBy.REQUESTED
            assert demand.reasons


# ---------------------------------------------------------------- レイヤの境界


def imported_modules(package: Path) -> list[tuple[str, str]]:
    found = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.append((path.name, node.module))
            elif isinstance(node, ast.Import):
                found += [(path.name, alias.name) for alias in node.names]
    return found


def test_the_ai_layer_cannot_reach_fan_control():
    """**LLM から Fan Demand へ到達する経路を作らない**（AGENTS.md ルール1）。"""
    offending = [
        f"{name}: {module}"
        for name, module in imported_modules(SRC / "ai")
        if module.startswith("coldaisle.control")
    ]
    assert offending == []


def test_fan_control_does_not_depend_on_the_ai_api_or_serial():
    """制御は AI・API に依存しない（0027 §2.7）。

    シリアルを開くのは取り込みデーモンだけ（AGENTS.md ルール6）。
    """
    forbidden = ("coldaisle.ai", "coldaisle.api", "coldaisle.server", "serial")
    offending = [
        f"{name}: {module}"
        for name, module in imported_modules(SRC / "control")
        if module.startswith(forbidden)
    ]
    assert offending == []


# ------------------------------------------- v7: 判断を出した model artifact（#159）

ARTIFACT = "a" * 64
"""試験用の artifact hash。**実機の値ではない**（AGENTS.md ルール10）。"""


def learned_state(**overrides) -> ControlState:
    """Learned MPC が requested を作った tick の状態。"""
    values = {
        "operating_mode": OperatingMode.AUTO,
        "authority_stage": AuthorityStage.LIMITED,
        "active_controller": ControllerKind.LEARNED_MPC,
        "safety_state": SafetyState.NORMAL,
        "fallback_active": False,
        "model_version": "0.1.0",
        "model_confidence": 0.9,
        "model_ood": False,
    }
    return ControlState(**(values | overrides))


def model_gate(**overrides) -> ModelGateDecision:
    """Learned MPC を採った tick の裏づけのある判断。"""
    values = {
        "schema_version": 2,
        "model_version": "0.1.0",
        "inference_id": "b" * 64,
        "artifact_sha256": ARTIFACT,
        "attested": True,
        "confidence": 0.9,
        "ood": False,
        "confidence_level": ConfidenceLevel.HIGH,
        "authority_stage": AuthorityStage.LIMITED,
        "learned_selected": True,
        "limits": (AuthorityLimitSource.STAGE_BAND,),
        "assessment": tuple(
            Reason(code=component) for component in MODEL_GATE_ASSESSMENT_COMPONENTS
        ),
    }
    return ModelGateDecision(**(values | overrides))


def learned_tick(**overrides) -> ControlTick:
    values = {
        "tick_id": 1,
        "ts_ms": NOW_MS,
        "state": learned_state(),
        "zones": zones(passthrough()),
        "model_gate": model_gate(),
        # v8 を名乗る記録は実行記録を省けない（#74 / 決定記録 0060 §2.4）。
        "runtime": CONTROL_TICK_RUNTIME,
        # v9 を名乗る記録は Safety の裁定の前提を省けない（#78）。
        "safety_provenance": SAFETY_PROVENANCE,
        # v10 を名乗る記録は registry の版を省けない（#104）。
        "registry": REGISTRY_PROVENANCE,
        # v11 を名乗る記録は Air Balance の記録を省けない（#81）。
        "air_balance": AIR_BALANCE_RECORD,
        "mode_command": MODE_COMMAND_RECORD,
        "authority": AUTHORITY_RECORD,
        "air_balance_coordination": COORDINATION_RECORD,
        "tach_unconfirmed_zones": (),
    }
    version = overrides.get("schema_version", SCHEMA_VERSION)
    assert isinstance(version, int)
    values["runtime"] = runtime_for(version)
    if version < 14:
        values.pop("air_balance_coordination")
        values.pop("tach_unconfirmed_zones")
    if version < 13:
        values.pop("authority")
    if version < 12:
        values.pop("mode_command")
    if version < 11:
        values.pop("air_balance")
    if overrides.get("schema_version", SCHEMA_VERSION) < 10:
        values.pop("registry")
    if overrides.get("schema_version", SCHEMA_VERSION) < 9:
        values.pop("safety_provenance")
    if overrides.get("schema_version", SCHEMA_VERSION) < 8:
        values.pop("runtime")
    return ControlTick(**(values | overrides))


def test_v7_trace_records_the_artifact_that_produced_the_applied_proposal():
    """**適用した tick の artifact が decision trace に残る**（#159 の受入基準）。"""
    recorded = learned_tick()

    assert recorded.schema_version == SCHEMA_VERSION >= 7
    restored = ControlTick.model_validate_json(recorded.model_dump_json())
    assert restored.model_gate is not None
    assert restored.model_gate.artifact_sha256 == ARTIFACT
    assert restored.applied_model_artifact == ARTIFACT
    assert restored.applied_artifact_unknown is False


def test_an_attested_artifact_needs_the_registry_that_bound_it():
    """**registry を読んでいない記録に、裏づけのある artifact を載せない**（codex #4133594515）。

    Learned MPC を制御へ束縛できるのは production pointer だけ（決定記録 0052 §2.1 の3）で、
    その pointer は registry を読まなければ得られない。
    """
    with pytest.raises(ValidationError, match="registry を読んでいる"):
        learned_tick(registry=RegistryProvenance.unbound())


def test_an_attested_artifact_must_be_the_recorded_production():
    """**別の artifact を production に持つ registry の版と組み合わせない**（codex #4133594515）。

    組み合わせられると、artifact B の判断を artifact A の昇格の下で出たものとして記録できる。
    """
    with pytest.raises(ValidationError, match="thermal_model の production と揃える"):
        learned_tick(registry=registry_provenance_for("f" * 64))

    other = {
        kind: pointer
        for kind, pointer in REGISTRY_PROVENANCE.production.items()
        if kind != "thermal_model"
    }
    with pytest.raises(ValidationError, match="thermal_model の production と揃える"):
        learned_tick(
            registry=RegistryProvenance(
                revision=3,
                production=other
                | {
                    "thermal_model": RegistryProductionPointer(
                        artifact_sha256=None, established_by=None
                    )
                },
            )
        )


def test_an_unattested_gate_does_not_need_a_registry_read():
    """裏づけの無い判断は artifact を持たないので、registry を読んでいない記録でもよい。"""
    recorded = learned_tick(
        state=fallback_state(
            authority_stage=AuthorityStage.LIMITED,
            fallback_reason=Reason(code="low_confidence"),
            model_version="0.1.0",
        ),
        model_gate=model_gate(
            artifact_sha256=None,
            attested=False,
            confidence=None,
            ood=None,
            confidence_level=ConfidenceLevel.LOW,
            learned_selected=False,
            limits=(),
            assessment=(),
        ),
        registry=RegistryProvenance.unbound(),
    )

    assert recorded.registry == RegistryProvenance.unbound()


@pytest.mark.parametrize("schema_version", [5, 6])
def test_a_stored_tick_without_the_field_is_read_as_artifact_unknown(schema_version: int):
    """**欄の無い古い tick は「artifact 不明」**。昇格の証拠に使えない（fail closed）。

    保存済みの v5 / v6 はそのまま読めなければならない（決定記録 0030）。読めたうえで、
    `applied_model_artifact` は `None`、`applied_artifact_unknown` は `True` になる。
    """
    stored = ControlTick(
        schema_version=schema_version,
        tick_id=1,
        ts_ms=NOW_MS,
        state=learned_state(),
        zones=zones(passthrough()),
        # 保存済みの v5 / v6 は、入れ子の判断も v1（artifact の欄を持たない）。
        model_gate=model_gate(schema_version=1, artifact_sha256=None),
    )

    restored = ControlTick.model_validate_json(stored.model_dump_json())
    assert restored.schema_version == schema_version
    assert restored.model_gate is not None
    assert restored.model_gate.schema_version == 1
    assert restored.state.active_controller is ControllerKind.LEARNED_MPC
    assert restored.applied_model_artifact is None, "推測で埋めない"
    assert restored.applied_artifact_unknown is True, "「記録が無いだけ」に見せない"


@pytest.mark.parametrize("schema_version", [5, 6])
def test_a_legacy_trace_cannot_carry_the_artifact_added_in_v7(schema_version: int):
    """**古い version に、後から意味の違う欄を足して読ませない**（決定記録 0030 §2）。"""
    with pytest.raises(ValidationError, match="schema version 7"):
        learned_tick(schema_version=schema_version)


def test_a_v7_tick_cannot_hide_the_artifact_of_an_attested_judgement():
    """**新しい trace で「artifact 不明」を作れない。**

    作れると、束縛できる形に直したあとも、欄を空けるだけで束縛を外せてしまう。
    「artifact 不明」でありうるのは保存済みの v1〜v6 だけである。
    """
    with pytest.raises(ValidationError, match="artifact_sha256 が要る"):
        learned_tick(model_gate=model_gate(artifact_sha256=None))


def test_an_unattested_judgement_cannot_name_an_artifact():
    """**束縛できていない identity を残さない**（#85 の裏づけと同じ向き）。

    裏づけの無い判断に artifact を書けると、別の推論の artifact が「この tick の実績」
    として読まれる。
    """
    with pytest.raises(ValidationError, match="裏づけの無い記録に artifact_sha256"):
        model_gate(
            artifact_sha256=ARTIFACT,
            attested=False,
            confidence=None,
            ood=None,
            confidence_level=ConfidenceLevel.LOW,
            learned_selected=False,
            limits=(),
            assessment=(),
        )


def test_a_proposal_cannot_declare_its_own_artifact():
    """**artifact は提案の自称値にできない**（#159 の不変条件）。

    `ControllerProposal` に欄を作らないことで、worker が「この提案はこの artifact が
    出した」と名乗る経路そのものを無くす。Gate は検証済み assessment からだけ写す。
    """
    with pytest.raises(ValidationError):
        ControllerProposal(
            controller=ControllerKind.LEARNED_MPC,
            seq=0,
            computed_at_ms=NOW_MS,
            requested=requests(),
            model_version="0.1.0",
            confidence=0.9,
            ood=False,
            optimizer_status=OptimizerStatus.OK,
            latency_ms=10,
            inference_id="b" * 64,
            artifact_sha256=ARTIFACT,
        )


def test_a_tick_cannot_claim_two_different_artifacts():
    """**同じ tick の2つの記録が別の artifact を名乗らない。**

    名乗れると、あとから読む側が「どの artifact の提案か」を決められない。
    """
    from coldaisle.control.schema import ShadowCounterfactual, ShadowRecord

    learned_counterfactual = ShadowCounterfactual(
        controller=ControllerKind.LEARNED_MPC,
        requested=PerZone[float](front=0.5, rear=0.5, top=0.5),
        reason=Reason(code="optimizer_timeout"),
        optimizer_status=OptimizerStatus.TIMEOUT,
        latency_ms=10,
        model_version="0.1.0",
        inference_id="b" * 64,
        # **同じ tick の model_gate とは別の artifact。**
        artifact_sha256="c" * 64,
        attested=True,
        confidence=0.9,
        ood=False,
    )

    with pytest.raises(ValidationError, match="別の artifact"):
        ControlTick(
            tick_id=1,
            ts_ms=NOW_MS,
            runtime=CONTROL_TICK_RUNTIME,
            safety_provenance=SAFETY_PROVENANCE,
            registry=REGISTRY_PROVENANCE,
            air_balance=AIR_BALANCE_RECORD,
            mode_command=MODE_COMMAND_RECORD,
            authority=AUTHORITY_RECORD,
            air_balance_coordination=COORDINATION_RECORD,
            tach_unconfirmed_zones=(),
            state=fallback_state(
                authority_stage=AuthorityStage.LIMITED,
                fallback_reason=Reason(code="low_confidence"),
                model_version="0.1.0",
                model_confidence=0.9,
                model_ood=False,
            ),
            zones=zones(passthrough()),
            model_gate=model_gate(learned_selected=False, limits=()),
            shadow=ShadowRecord(
                tick_id=1,
                ts_ms=NOW_MS,
                authority_stage=AuthorityStage.LIMITED,
                applied_controller=ControllerKind.FALLBACK,
                applied_effective=PerZone[float](front=0.4, rear=0.4, top=0.4),
                counterfactuals=(learned_counterfactual,),
            ),
        )


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4])
def test_a_learned_tick_without_a_model_gate_is_artifact_unknown(schema_version: int):
    """**`model_gate` を必須にしたのは v5 から**（codex #4057527947）。

    v1〜v4 は Learned MPC を適用した tick でも `model_gate` を持たない。gate の有無から
    数えると、そういう tick が「artifact 不明」にも「束縛できた」にも数えられず、
    **欠けていること自体が記録から消える。** 起点は `ControlState` にする。
    """
    stored = ControlTick(
        schema_version=schema_version,
        tick_id=1,
        ts_ms=NOW_MS,
        state=learned_state(),
        zones=zones(passthrough()),
    )

    restored = ControlTick.model_validate_json(stored.model_dump_json())
    assert restored.model_gate is None
    assert restored.state.active_controller is ControllerKind.LEARNED_MPC
    assert restored.applied_model_artifact is None
    assert restored.applied_artifact_unknown is True, "記録の無さを「不明なし」に落とさない"


def test_a_fallback_tick_is_not_counted_as_artifact_unknown():
    """**「言えない」と「そうではなかった」を混ぜない。**

    Fallback で回していた tick は artifact を持たないのが正しい姿で、「不明」ではない。
    ここを混ぜると、Fallback 区間の多い報告がすべて不明で埋まり、昇格が永久に来ない。
    """
    recorded = tick(passthrough())

    assert recorded.state.active_controller is ControllerKind.FALLBACK
    assert recorded.applied_model_artifact is None
    assert recorded.applied_artifact_unknown is False


def test_the_nested_model_gate_carries_its_own_schema_version():
    """**入れ子の判断にも版を持たせる**（codex #4057753201）。

    入れ子の版を上げないと、v7 の trace が「v1 と名乗るのに v1 には無かった欄を持つ」
    記録を書いてしまう。v1 の判断は artifact を持てず、v7 の tick は v2 を要求する。
    """
    recorded = learned_tick()
    assert recorded.model_gate is not None
    assert recorded.model_gate.schema_version == 2

    with pytest.raises(ValidationError, match="schema version 2 にする"):
        model_gate(schema_version=1)

    with pytest.raises(ValidationError, match="schema version 2 の model_gate が要る"):
        learned_tick(model_gate=model_gate(schema_version=1, artifact_sha256=None))

    # 保存済みの v1 の判断はそのまま読める。
    stored = model_gate(schema_version=1, artifact_sha256=None)
    assert ModelGateDecision.model_validate_json(stored.model_dump_json()) == stored


def test_a_model_gate_without_a_schema_version_is_refused():
    """**版の書かれていない判断を最新版として読まない**（codex #4092017585）。

    既定値を省いて書き出した v1 の判断が v2 と読まれないよう、版は入力で必須にする。
    """
    document = json.loads(learned_tick().model_dump_json())
    assert document["model_gate"]["schema_version"] == 2
    del document["model_gate"]["schema_version"]

    with pytest.raises(ValidationError, match="schema_version"):
        ControlTick.model_validate_json(json.dumps(document))
