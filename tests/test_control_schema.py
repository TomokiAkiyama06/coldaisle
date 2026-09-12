"""Fan 制御の層間の型（#76 / 決定記録 0028 §2.3〜§2.5）。

受入基準:

- Front / Rear / Top で同一 schema を使用できる
- `requested != effective` の理由を必ず追跡できる
- Raw PWM を上位 Controller API が受け付けない
- Mock / Replay で実機なしテストが可能（純粋な型なので実機に依存しない）
- schema の互換性テストがある
"""

import ast
import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.control import (
    BOUND_BY_PRECEDENCE,
    SCHEMA_VERSION,
    AuthorityStage,
    BoundBy,
    ControllerKind,
    ControllerProposal,
    ControlState,
    ControlTick,
    EffectiveZoneDemand,
    Fault,
    FaultCode,
    GuardZoneOutput,
    OperatingMode,
    OptimizerStatus,
    PerZone,
    Reason,
    SafetyState,
    SafetyZoneOutput,
    Zone,
    ZoneRecord,
    ZoneRequest,
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
            model_version="thermal-v1",
        )


def test_a_complete_learned_proposal_is_accepted():
    proposal = ControllerProposal(
        controller=ControllerKind.LEARNED_MPC,
        seq=7,
        computed_at_ms=NOW_MS,
        requested=requests(0.55),
        model_version="thermal-v1",
        confidence=0.82,
        ood=False,
        optimizer_status=OptimizerStatus.OK,
        latency_ms=340,
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
        {"operating_mode": OperatingMode.MANUAL},
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
            model_version="thermal-v1",
            model_confidence=0.9,
            model_ood=False,
        )


def test_fallback_while_ml_was_allowed_must_say_why():
    with pytest.raises(ValidationError, match="Fallback にした理由"):
        fallback_state(authority_stage=AuthorityStage.LIMITED)
    state = fallback_state(
        authority_stage=AuthorityStage.LIMITED,
        fallback_reason=Reason(code="low_confidence"),
        model_version="thermal-v1",
        model_confidence=0.31,
        model_ood=False,
    )
    assert state.fallback_active


def test_fallback_active_cannot_contradict_the_active_controller():
    with pytest.raises(ValidationError, match="食い違って"):
        fallback_state(fallback_active=False)


# ---------------------------------------------------------------- 1 tick の記録


@pytest.mark.parametrize(
    "overrides",
    [
        {"safety_state": SafetyState.STARTUP},
        {"operating_mode": OperatingMode.MAX},
    ],
)
def test_all_zones_are_max_when_the_state_says_so(overrides):
    with pytest.raises(ValidationError, match="すべての zone が Max"):
        ControlTick(
            tick_id=1, ts_ms=NOW_MS, state=fallback_state(**overrides), zones=zones(passthrough())
        )
    tick = ControlTick(
        tick_id=1, ts_ms=NOW_MS, state=fallback_state(**overrides), zones=zones(forced())
    )
    assert tick.zones.get(Zone.FRONT).demand.effective == 1.0


def test_emergency_needs_a_cause_and_all_zones_at_max():
    state = fallback_state(safety_state=SafetyState.EMERGENCY)
    with pytest.raises(ValidationError, match="fault"):
        ControlTick(tick_id=1, ts_ms=NOW_MS, state=state, zones=zones(forced()))
    tick = ControlTick(
        tick_id=1,
        ts_ms=NOW_MS,
        state=state,
        zones=zones(forced()),
        faults=(Fault(code=FaultCode.TACH_STALL, zone=Zone.TOP),),
    )
    assert tick.faults[0].zone is Zone.TOP


def test_normal_cannot_carry_faults():
    with pytest.raises(ValidationError, match="NORMAL"):
        ControlTick(
            tick_id=1,
            ts_ms=NOW_MS,
            state=fallback_state(),
            zones=zones(passthrough()),
            faults=(Fault(code=FaultCode.TICK_OVERRUN),),
        )


# ---------------------------------------------------------------- 互換性（#82 の保存データ）


def test_the_stored_v1_record_still_loads_unchanged():
    """**保存済みの v1 の記録が、読めて、同じ形で書き戻せる。**

    ここが落ちたら、フィールドの名前か意味が変わっている。#82 が保存したデータを読み違えるので、
    `SCHEMA_VERSION` を上げて移行を用意してから fixture を更新する。
    """
    stored = FIXTURE.read_text(encoding="utf-8")
    tick = ControlTick.model_validate_json(stored)
    assert tick.schema_version == SCHEMA_VERSION == 1
    assert json.loads(tick.model_dump_json()) == json.loads(stored)


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
