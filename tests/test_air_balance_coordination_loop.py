"""Air Balance の協調を control loop へ配線する（#81 / 決定記録 0078 §2.11 の段 3 / 0085）。

確かめること（0078 §2.10 / 0085 §2.4）:

- 位置: Fallback の後・Critical Safety の評価の後・Gate の前・合成の前。合成は迂回しない
- 段の独立性: ``shadow`` は Backend に渡る effective を変えない
- 条件（0078 §2.3）が欠けた tick は ``skipped`` で raw baseline のまま
- 風量は合成の下限（Safety の floor・forced_max・Guard の floor・ramp_down）込みで見積もる
- 確定前の tach 無応答（``tach_unconfirmed_zones``）の tick は協調しない。Safety の判定は変えない
- 失敗: ``apply`` は ``fallback_exception`` へ翻訳して Gate を迂回し raw baseline、
  ``shadow`` は記録だけ。``try`` は協調の部品の呼び出しだけを囲む
- Gate: 帯の中心は coordinated baseline、FULL の Learned MPC には掛けない、遷移の floor は後
- trace（``ControlTick`` v14）の不変条件

校正済みの値は試験用に ``calibrated`` を名乗らせた characterization で、実機の値ではない。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.control.air_balance import ConfiguredAirBalanceModel, ThermalInputs
from coldaisle.control.air_balance_coordination import AirBalanceCoordinator
from coldaisle.control.config import ControlConfig
from coldaisle.control.fallback.controller import FallbackController
from coldaisle.control.fallback.gate import ControllerGate, LearnedControlStatus, SnapshotStatus
from coldaisle.control.hardware.simulated import SimulatedFanBackend, SimulatedFaultPlan
from coldaisle.control.loop import ModeCommand, StaticAuthority
from coldaisle.control.reactive.guard import ReactiveGuard
from coldaisle.control.safety.critical import (
    CriticalSafety,
    DemandComposer,
    create_control_runtime_binding,
)
from coldaisle.control.schema import (
    AirBalanceCoordinationRecord,
    AirBalanceCoordinationSkipReason,
    AirBalanceCoordinationStatus,
    AuthorityStage,
    BoundBy,
    ControllerKind,
    ControlTick,
    Demand,
    FaultCode,
    GuardZoneOutput,
    OperatingMode,
    PerZone,
    ProjectedFloorBasis,
    Reason,
    SafetyState,
    StaticAuthorityStage,
    Zone,
    ZoneRequest,
)
from coldaisle.metrics import MetricCatalog
from test_air_balance_coordination import ScriptedModel
from test_control_config import air_balance_coordination, calibrated_air_balance_document
from test_control_loop import METRICS_PATH, Harness, control_config, manual
from test_control_schema import registry_provenance_for
from test_fallback_controller import (
    TEST_ARTIFACT_SHA256,
    TEST_MODEL_VERSION,
    healthy_status,
    learned_proposal,
)

LEARNED_DEMAND = 0.95
"""試験の Learned MPC が毎 tick 出す requested（3 zone 同じ）。"""


@pytest.fixture(scope="module")
def catalog() -> MetricCatalog:
    """**本番と同じ Metric Catalog** を読む。"""
    return MetricCatalog.from_yaml(METRICS_PATH)


def coordination_config(
    mode: str = "apply",
    *,
    authority_stage: str = "shadow",
    policy: dict[str, Any] | None = None,
) -> ControlConfig:
    """校正済みの ``air-balance.yaml`` と、``mode`` の協調を持つ設定（値は試験用の仮の値）。

    ``max_raise`` は Front / Rear 0.2・Top 0、``release_hold_ms`` は 3000。
    """
    return control_config(
        policy={
            "air_balance_coordination": air_balance_coordination(mode),
            "authority_stage": authority_stage,
            "recovery_hold_ms": 1,
            **(policy or {}),
        },
        air_balance=calibrated_air_balance_document(),
    )


def scripted_coordinator(config: ControlConfig) -> tuple[AirBalanceCoordinator, ScriptedModel]:
    """``coordinate()`` を差し替えられる偽の model を持つ協調の部品。"""
    model = ScriptedModel(
        ConfiguredAirBalanceModel(config.air_balance, config.sources.air_balance.sha256)
    )
    return (
        AirBalanceCoordinator(
            settings=config.policy.air_balance_coordination,
            model=model,
            fan_hardware=config.fan_hardware,
        ),
        model,
    )


class LearnedGate:
    """本物の ``ControllerGate`` に、健全で選ばれるはずの Learned MPC の提案を毎 tick 渡す。

    ``select()`` が受け取った Fallback を記録する（呼ばれたかの spy）。``healthy`` を偽にすると
    提案の無い tick（worker の失敗）になり、Gate は Fallback へ退避する。
    """

    def __init__(self, config: ControlConfig, stage: AuthorityStage) -> None:
        self.inner = ControllerGate(
            config.policy,
            expected_model_version=TEST_MODEL_VERSION,
            expected_artifact_sha256=TEST_ARTIFACT_SHA256,
            authority=StaticAuthorityStage(stage),
        )
        self.calls: list[Any] = []
        self.healthy = True

    def set_operating_mode(self, operating_mode: OperatingMode, *, now_mono_ms: int) -> None:
        self.inner.set_operating_mode(operating_mode, now_mono_ms=now_mono_ms)

    def select(self, *, now_mono_ms: int, fallback: Any, learned: Any, **kwargs: Any) -> Any:
        self.calls.append(fallback)
        status = (
            healthy_status(received=now_mono_ms, proposal=learned_proposal(LEARNED_DEMAND))
            if self.healthy
            else LearnedControlStatus(
                supervisor_available=True,
                control_deadline_exceeded=False,
                snapshot_status=SnapshotStatus.AVAILABLE,
            )
        )
        return self.inner.select(
            now_mono_ms=now_mono_ms, fallback=fallback, learned=status, **kwargs
        )


def learned_harness(
    catalog: MetricCatalog,
    mode: str,
    stage: AuthorityStage,
    **kwargs: Any,
) -> tuple[Harness, LearnedGate]:
    """Learned MPC が選ばれうる loop（設定の上限は full、実効 stage は ``stage``）。"""
    config = coordination_config(mode, authority_stage="full")
    gate = LearnedGate(config, stage)
    harness = Harness(
        catalog,
        config=config,
        gate=gate,
        registry=registry_provenance_for(TEST_ARTIFACT_SHA256),
        authority=StaticAuthority(stage, config_ceiling=AuthorityStage.FULL),
        **kwargs,
    )
    return harness, gate


def record_of(tick: ControlTick) -> AirBalanceCoordinationRecord:
    record = tick.air_balance_coordination
    assert record is not None
    return record


def requested_of(tick: ControlTick) -> PerZone[Demand]:
    return PerZone[Demand](
        front=tick.zones.front.demand.requested,
        rear=tick.zones.rear.demand.requested,
        top=tick.zones.top.demand.requested,
    )


def settle_until(harness: Harness, status: AirBalanceCoordinationStatus) -> ControlTick:
    """``NORMAL`` にしてから、協調の結果が ``status`` になるまで回す。"""
    for _ in range(12):
        tick = harness.tick().tick
        if tick.state.safety_state is SafetyState.NORMAL and record_of(tick).status is status:
            return tick
    raise AssertionError(f"{status.value} にならなかった")


def round_trip(tick: ControlTick) -> ControlTick:
    """保存した JSON から読み直しても同じ記録（writer と reader が同じ不変条件を守る）。"""
    loaded = ControlTick.model_validate_json(tick.model_dump_json())
    assert loaded == tick
    return loaded


# ------------------------------------------------------------- mode: off（いまの main と同じ）


def test_off_records_off_and_copies_the_unconfirmed_tach(catalog: MetricCatalog) -> None:
    harness = Harness(catalog, config=coordination_config("off"))
    first = harness.tick().tick
    assert first.schema_version == 14
    assert record_of(first) == AirBalanceCoordinationRecord.off()
    # 引き継ぎ直後の STARTUP は tach の応答をまだ見ていない（timer が動いている）。
    assert first.tach_unconfirmed_zones == (Zone.FRONT, Zone.REAR, Zone.TOP)
    settled = harness.settle().tick
    assert record_of(settled) == AirBalanceCoordinationRecord.off()
    assert settled.tach_unconfirmed_zones == ()
    for zone in Zone:
        assert settled.zones.get(zone).controller_reason.code.startswith("fallback")


# ------------------------------------------------------------- 段の独立性（0078 §2.10）


def test_shadow_never_changes_what_reaches_the_backend(catalog: MetricCatalog) -> None:
    off = Harness(catalog, config=coordination_config("off"))
    shadow = Harness(catalog, config=coordination_config("shadow"))
    seen_shadow = False
    for _ in range(14):
        left = round_trip(off.tick().tick)
        right = round_trip(shadow.tick().tick)
        assert left.state.safety_state is right.state.safety_state
        assert left.faults == right.faults
        for zone in Zone:
            assert left.zones.get(zone).demand == right.zones.get(zone).demand
            assert left.zones.get(zone).controller_reason == right.zones.get(zone).controller_reason
        record = record_of(right)
        if record.status is AirBalanceCoordinationStatus.SHADOW:
            seen_shadow = True
            assert record.output == record.candidate
            assert record.counterfactual_output is not None
            assert record.counterfactual_output.front > record.candidate.front  # type: ignore[union-attr]
    assert seen_shadow


# ------------------------------------------------------------- apply（上げるだけ・上限・合成は後）


def test_apply_raises_the_requested_within_max_raise(catalog: MetricCatalog) -> None:
    harness = Harness(catalog, config=coordination_config("apply"))
    tick = round_trip(settle_until(harness, AirBalanceCoordinationStatus.APPLIED))
    record = record_of(tick)
    assert record.candidate is not None and record.output is not None
    assert record.max_raise == PerZone[Demand](front=0.2, rear=0.2, top=0.0)
    assert record.reasons == ("front_makeup_air",)

    front = tick.zones.front
    assert front.demand.requested == record.output.front > record.candidate.front
    assert record.output.front <= record.candidate.front + 0.2
    assert front.controller_reason.code == "air_balance_front_makeup_air"
    assert "baseline_reason=fallback" in front.controller_reason.detail
    # Top の max_raise は 0（最初の apply は Front / Rear だけ。0078 §2.8）。
    assert tick.zones.top.demand.requested == record.candidate.top
    # 合成は協調の後にいまと同じく掛かる（Safety の floor・ramp_down を迂回しない）。
    for zone in Zone:
        demand = tick.zones.get(zone).demand
        assert demand.effective >= demand.safety_floor
        assert demand.effective >= demand.requested or demand.bound_by is BoundBy.GUARD_CEILING


def test_the_top_cpu_floor_does_not_depend_on_coordination(catalog: MetricCatalog) -> None:
    off = Harness(catalog, config=coordination_config("off"))
    applied = Harness(catalog, config=coordination_config("apply"))
    for _ in range(12):
        left = off.tick().tick
        right = applied.tick().tick
        assert left.zones.top.demand.safety_floor == right.zones.top.demand.safety_floor
        assert left.state.safety_state is right.state.safety_state


class CeilingGuard(ReactiveGuard):
    """Front に ceiling を掛ける Guard（介入の形は本物の Guard の出力のまま差し替える）。"""

    ceiling: float | None = None

    def evaluate(self, snapshot):  # type: ignore[no-untyped-def]
        decision = super().evaluate(snapshot)
        if self.ceiling is None:
            return decision
        front = GuardZoneOutput(
            ceiling=self.ceiling,
            hold_until_mono_ms=snapshot.monotonic_ms + 1_000,
            reason=Reason(code="test_ceiling"),
        )
        return decision.model_copy(
            update={
                "zones": PerZone[GuardZoneOutput](
                    front=front, rear=decision.zones.rear, top=decision.zones.top
                )
            }
        )


def test_the_guard_ceiling_still_applies_to_a_coordinated_request(catalog: MetricCatalog) -> None:
    """協調は requested を作る側。Guard の ceiling は協調の後にも掛かる（0078 §2.2）。"""
    config = coordination_config("apply")
    guard = CeilingGuard(config.policy.reactive_guard, catalog)
    harness = Harness(catalog, config=config, guard=guard)
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    guard.ceiling = 0.75
    ticks = harness.run(6)
    last = ticks[-1].tick
    record = record_of(last)
    assert record.output is not None and record.output.front > 0.75
    assert last.zones.front.demand.requested == record.output.front
    assert last.zones.front.demand.effective == 0.75
    assert last.zones.front.demand.bound_by is BoundBy.GUARD_CEILING


# ------------------------------------------------------------- 条件の欠け（0078 §2.3）


@pytest.mark.parametrize(
    "command",
    [
        manual(0.6, 0.6, 0.6),
        ModeCommand(
            mode=OperatingMode.CALIBRATION,
            requested=PerZone[ZoneRequest](
                front=ZoneRequest(demand=0.6, reason=Reason(code="sweep")),
                rear=ZoneRequest(demand=0.6, reason=Reason(code="sweep")),
                top=ZoneRequest(demand=0.6, reason=Reason(code="sweep")),
            ),
        ),
        ModeCommand(mode=OperatingMode.MAX),
    ],
    ids=["manual", "calibration", "max"],
)
def test_a_human_mode_skips_coordination(catalog: MetricCatalog, command: ModeCommand) -> None:
    harness = Harness(catalog, config=coordination_config("apply"))
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    harness.mode.command = command
    tick = round_trip(harness.tick().tick)
    record = record_of(tick)
    assert record.status is AirBalanceCoordinationStatus.SKIPPED
    assert record.skip_reason is AirBalanceCoordinationSkipReason.OPERATING_MODE
    assert record.output == record.candidate
    assert record.held == PerZone[bool](front=False, rear=False, top=False)
    assert all(
        not tick.zones.get(zone).controller_reason.code.startswith("air_balance") for zone in Zone
    )


def test_an_unavailable_snapshot_skips_coordination(catalog: MetricCatalog) -> None:
    harness = Harness(catalog, config=coordination_config("apply"))
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    harness.telemetry.error = RuntimeError("収集層が止まった")
    record = record_of(round_trip(harness.tick().tick))
    assert record.skip_reason is AirBalanceCoordinationSkipReason.SNAPSHOT_UNAVAILABLE
    assert record.output == record.candidate


class FailingFallback(FallbackController):
    fail = False

    def propose(self, snapshot):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("Fallback の不具合")
        return super().propose(snapshot)


def test_a_fallback_exception_skips_then_emergency_skips(catalog: MetricCatalog) -> None:
    config = coordination_config("apply")
    fallback = FailingFallback(config.policy, catalog)
    harness = Harness(catalog, config=config, fallback=fallback)
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    fallback.fail = True
    first = record_of(round_trip(harness.tick().tick))
    assert first.skip_reason is AirBalanceCoordinationSkipReason.BASELINE_UNAVAILABLE
    assert first.candidate is None and first.output is None
    fallback.fail = False
    tick = round_trip(harness.tick().tick)
    assert tick.state.safety_state is SafetyState.EMERGENCY
    assert record_of(tick).skip_reason is AirBalanceCoordinationSkipReason.SAFETY_STATE


def test_startup_skips_coordination(catalog: MetricCatalog) -> None:
    harness = Harness(catalog, config=coordination_config("apply"))
    tick = round_trip(harness.tick().tick)
    assert tick.state.safety_state is SafetyState.STARTUP
    record = record_of(tick)
    assert record.skip_reason is AirBalanceCoordinationSkipReason.SAFETY_STATE
    assert record.output == record.candidate


@pytest.mark.parametrize("zone", list(Zone))
def test_a_write_failure_in_any_zone_skips_coordination(catalog: MetricCatalog, zone: Zone) -> None:
    harness = Harness(catalog, config=coordination_config("apply"))
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    harness.backend.fault_plan = SimulatedFaultPlan(write_failure=frozenset({zone}))
    harness.tick()  # この tick の書き込みが失敗し、次の tick の Safety に渡る
    tick = round_trip(harness.tick().tick)
    assert any(fault.code is FaultCode.WRITE_FAILURE for fault in tick.faults)
    record = record_of(tick)
    # Top の Fan fault は無条件の EMERGENCY（0028 §2.7）なので、表の上の行が先に当たる。
    expected = (
        AirBalanceCoordinationSkipReason.SAFETY_STATE
        if zone is Zone.TOP
        else AirBalanceCoordinationSkipReason.ZONE_FAN_FAULT
    )
    assert record.skip_reason is expected
    assert record.output == record.candidate


@pytest.mark.parametrize("zone", list(Zone))
def test_an_unconfirmed_tach_skips_from_the_first_report_until_the_fault(
    catalog: MetricCatalog, zone: Zone
) -> None:
    """backend の ``TACH_STALL`` の最初の tick から、確定するまで ``tach_unconfirmed``。"""
    harness = Harness(catalog, config=coordination_config("apply"))
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    harness.backend.fault_plan = SimulatedFaultPlan(tach_stall=frozenset({zone}))
    harness.tick()  # この tick の書き込みで backend が TACH_STALL を返す
    reasons: list[AirBalanceCoordinationSkipReason | None] = []
    for _ in range(4):
        tick = round_trip(harness.tick().tick)
        record = record_of(tick)
        assert record.status is AirBalanceCoordinationStatus.SKIPPED
        assert record.output == record.candidate
        assert zone in tick.tach_unconfirmed_zones
        reasons.append(record.skip_reason)
        if any(fault.code is FaultCode.TACH_STALL for fault in tick.faults):
            break
    assert reasons[0] is AirBalanceCoordinationSkipReason.TACH_UNCONFIRMED
    # 窓が満ちて faults に確定した tick からは Fan fault として扱う（表の上の行が先）。
    # Top の stall は無条件の EMERGENCY なので Safety の状態の行が先に当たる。
    assert reasons[-1] is (
        AirBalanceCoordinationSkipReason.SAFETY_STATE
        if zone is Zone.TOP
        else AirBalanceCoordinationSkipReason.ZONE_FAN_FAULT
    )
    assert set(reasons[:-1]) == {AirBalanceCoordinationSkipReason.TACH_UNCONFIRMED}


class RecordingSafety(CriticalSafety):
    """発行した裁定を残す Critical Safety（判定は変えない）。"""

    decisions: list[Any]

    def evaluate(self, snapshot, **kwargs):  # type: ignore[no-untyped-def]
        decision = super().evaluate(snapshot, **kwargs)
        self.decisions.append(decision)
        return decision


def test_unconfirmed_tach_is_copied_by_the_writer_and_changes_no_safety_judgement(
    catalog: MetricCatalog,
) -> None:
    """裁定の欄は外へ見せるだけ。``off`` と ``shadow`` で Safety の結果は同じ（0078 §2.3）。"""
    results: dict[str, list[ControlTick]] = {}
    for mode in ("off", "shadow"):
        config = coordination_config(mode)
        # Backend は Safety と同じ binding を使う（別の runtime の裁定を混ぜない）。
        binding = create_control_runtime_binding(config)
        safety = RecordingSafety(
            config.safety,
            input_contract=Harness(catalog, config=config).contract,
            runtime_binding=binding,
        )
        safety.decisions = []
        harness = Harness(
            catalog,
            config=config,
            safety=safety,
            backend=SimulatedFanBackend(config=config.fan_hardware, runtime_binding=binding),
        )
        harness.settle()
        harness.backend.fault_plan = SimulatedFaultPlan(tach_stall=frozenset({Zone.REAR}))
        ticks = [harness.tick().tick for _ in range(5)]
        for tick, decision in zip(ticks, safety.decisions[-5:], strict=True):
            assert tick.tach_unconfirmed_zones == tuple(
                sorted(decision.tach_unconfirmed_zones, key=lambda zone: zone.value)
            )
        results[mode] = ticks
    for left, right in zip(results["off"], results["shadow"], strict=True):
        assert left.state.safety_state is right.state.safety_state
        assert left.faults == right.faults
        assert left.tach_unconfirmed_zones == right.tach_unconfirmed_zones
        for zone in Zone:
            assert left.zones.get(zone).demand == right.zones.get(zone).demand


def test_a_top_fan_fault_with_stale_cpu_telemetry_skips(catalog: MetricCatalog) -> None:
    harness = Harness(catalog, config=coordination_config("apply"))
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    harness.backend.fault_plan = SimulatedFaultPlan(write_failure=frozenset({Zone.TOP}))
    del harness.telemetry.values["cpu.package"]
    harness.tick()
    tick = round_trip(harness.tick().tick)
    codes = {fault.code for fault in tick.faults}
    assert {FaultCode.WRITE_FAILURE, FaultCode.CPU_TELEMETRY_STALE} <= codes
    assert record_of(tick).skip_reason in {
        AirBalanceCoordinationSkipReason.ZONE_FAN_FAULT,
        AirBalanceCoordinationSkipReason.SAFETY_STATE,
    }
    assert record_of(tick).output == record_of(tick).candidate


# ------------------------------------------------------------- 下限の見込み（0078 §2.2）


def test_stale_cpu_telemetry_counts_the_forced_top_as_full_exhaust(catalog: MetricCatalog) -> None:
    """CPU Telemetry の stale で Top だけ Max の tick は ``projected_floors.top == 1.0``。"""
    config = coordination_config("apply")
    harness = Harness(catalog, config=config)
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    del harness.telemetry.values["cpu.package"]
    tick = round_trip(harness.tick().tick)
    assert tick.state.safety_state is SafetyState.DEGRADED
    assert tick.zones.top.demand.forced_max
    record = record_of(tick)
    assert record.status in {
        AirBalanceCoordinationStatus.APPLIED,
        AirBalanceCoordinationStatus.NOT_NEEDED,
    }
    assert record.projected_floors is not None and record.projected_floor_basis is not None
    assert record.projected_floors.top == 1.0
    assert record.projected_floor_basis.top is ProjectedFloorBasis.FORCED_MAX
    assert record.proposed is not None and record.candidate is not None

    # Top の floor だけを渡した場合より Front make-up air が小さくならない。
    model = ConfiguredAirBalanceModel(config.air_balance, config.sources.air_balance.sha256)
    stable = config.fan_hardware.stable_demands(record.candidate)
    floors = record.projected_floors
    floor_only = model.coordinate(
        stable,
        ThermalInputs(),
        projected_floors=PerZone[Demand](
            front=floors.front, rear=floors.rear, top=tick.zones.top.demand.safety_floor
        ),
    )
    assert record.proposed.front >= floor_only.requested.front


def test_a_safety_floor_on_front_and_rear_is_projected(catalog: MetricCatalog) -> None:
    """GPU Telemetry の stale で Front / Rear の Safety の floor が上がる tick（0078 §2.2）。"""
    harness = Harness(catalog, config=coordination_config("apply"))
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    del harness.telemetry.values["gpu.0.core"]
    tick = round_trip(harness.tick().tick)
    assert tick.state.safety_state is SafetyState.DEGRADED
    record = record_of(tick)
    assert record.projected_floors is not None and record.projected_floor_basis is not None
    for zone in (Zone.FRONT, Zone.REAR):
        demand = tick.zones.get(zone).demand
        assert record.projected_floor_basis.get(zone) is ProjectedFloorBasis.SAFETY_FLOOR
        assert record.projected_floors.get(zone) == demand.safety_floor
        # 下限が既に目標の風量を出している zone は上げない（floor を requested に写さない）。
        assert record.output is not None and record.candidate is not None
        assert demand.requested == record.output.get(zone)


def test_the_ramp_down_floor_is_the_one_the_composer_applies(catalog: MetricCatalog) -> None:
    """STARTUP の Max から下がる途中の tick の見込みは、合成の ramp_down の下限と同じ値。"""
    harness = Harness(catalog, config=coordination_config("apply"))
    seen = 0
    for _ in range(10):
        tick = round_trip(harness.tick().tick)
        record = record_of(tick)
        if record.projected_floors is None or record.projected_floor_basis is None:
            continue
        for zone in Zone:
            demand = tick.zones.get(zone).demand
            if demand.bound_by is not BoundBy.RAMP_DOWN:
                continue
            if record.projected_floor_basis.get(zone) is ProjectedFloorBasis.RAMP_DOWN:
                assert record.projected_floors.get(zone) == demand.effective
                seen += 1
    assert seen > 0


def test_the_composer_ramp_floor_matches_the_composition(catalog: MetricCatalog) -> None:
    """``ramp_floor()`` は合成と同じ式・同じ前 tick・同じ経過時間。前 tick が無ければ None。"""
    config = coordination_config("off")
    composer = DemandComposer(config.safety)
    assert composer.ramp_floor(Zone.TOP, 1_000) is None
    harness = Harness(catalog, config=config, composer=composer)
    harness.tick()
    previous = harness.tick().tick
    upcoming_ms = harness.monotonic.monotonic_ms() + config.safety.tick_ms.value
    floor = composer.ramp_floor(Zone.TOP, upcoming_ms)
    rate = config.safety.ramp_down_per_s.value
    assert floor == pytest.approx(
        max(0.0, previous.zones.top.demand.effective - rate * config.safety.tick_ms.value / 1_000)
    )
    with pytest.raises(ValueError, match="単調時計"):
        composer.ramp_floor(Zone.TOP, harness.monotonic.monotonic_ms())


# ------------------------------------------------------------- 保持（0078 §2.4）


def test_a_skipped_tick_releases_the_hold(catalog: MetricCatalog) -> None:
    config = coordination_config("apply")
    coordinator, model = scripted_coordinator(config)
    harness = Harness(catalog, config=config, air_balance_coordinator=coordinator)
    harness.settle()
    model.requested = PerZone[Demand](front=1.0, rear=0.0, top=0.0)
    applied = record_of(harness.tick().tick)
    assert applied.status is AirBalanceCoordinationStatus.APPLIED
    model.requested = None  # 引き上げ幅が消えた
    held = round_trip(harness.tick().tick)
    assert record_of(held).held == PerZone[bool](front=True, rear=False, top=False)
    assert held.zones.front.controller_reason.code == "air_balance_release_hold"
    harness.mode.command = manual(0.6, 0.6, 0.6)
    harness.tick()  # skipped（operating_mode）で保持を解く
    harness.mode.command = ModeCommand()
    after = record_of(round_trip(harness.tick().tick))
    assert after.status is AirBalanceCoordinationStatus.NOT_NEEDED
    assert after.output == after.candidate


# ------------------------------------------------------------- 失敗（0078 §2.6 / 0085）


@pytest.mark.parametrize("lowering", [False, True], ids=["exception", "lowering"])
def test_an_apply_failure_uses_the_raw_baseline_and_becomes_an_emergency(
    catalog: MetricCatalog, caplog: pytest.LogCaptureFixture, lowering: bool
) -> None:
    config = coordination_config("apply")
    coordinator, model = scripted_coordinator(config)
    harness = Harness(catalog, config=config, air_balance_coordinator=coordinator)
    harness.settle()
    if lowering:
        model.lower = True
    else:
        model.error = RuntimeError("coordinate の不具合")
    with caplog.at_level(logging.ERROR, logger="coldaisle.control"):
        failed = round_trip(harness.tick().tick)
    record = record_of(failed)
    assert record.status is AirBalanceCoordinationStatus.FAILED
    assert record.failure is not None
    assert record.output == record.candidate == requested_of(failed)
    assert failed.model_gate is None
    # authority が SHADOW の tick は ML を使えないので fallback_reason を付けない（0085 §2.2）。
    assert failed.state.fallback_reason is None
    assert any(
        entry.levelno == logging.ERROR and entry.getMessage() == "air balance coordination failed"
        for entry in caplog.records
    )

    model.error = None
    model.lower = False
    emergency = round_trip(harness.tick().tick)
    assert emergency.state.safety_state is SafetyState.EMERGENCY
    exceptions = [fault for fault in emergency.faults if fault.code is FaultCode.FALLBACK_EXCEPTION]
    assert len(exceptions) == 1
    assert exceptions[0].detail.startswith("air_balance_coordination: ")


def test_a_shadow_failure_is_only_recorded(catalog: MetricCatalog) -> None:
    config = coordination_config("shadow")
    coordinator, model = scripted_coordinator(config)
    shadow = Harness(catalog, config=config, air_balance_coordinator=coordinator)
    off = Harness(catalog, config=coordination_config("off"))
    model.error = RuntimeError("coordinate の不具合")
    for _ in range(10):
        left = off.tick().tick
        right = round_trip(shadow.tick().tick)
        assert left.state.safety_state is right.state.safety_state
        assert left.faults == right.faults
        for zone in Zone:
            assert left.zones.get(zone).demand == right.zones.get(zone).demand
    assert record_of(right).status is AirBalanceCoordinationStatus.FAILED
    assert right.state.safety_state is SafetyState.NORMAL


class ExplodingSafety(CriticalSafety):
    explode = False

    def evaluate(self, snapshot, **kwargs):  # type: ignore[no-untyped-def]
        if self.explode:
            raise RuntimeError("Safety の不具合")
        return super().evaluate(snapshot, **kwargs)


class ExplodingComposer(DemandComposer):
    explode = False

    def compose(self, **kwargs):  # type: ignore[no-untyped-def]
        if self.explode:
            raise RuntimeError("合成の不具合")
        return super().compose(**kwargs)


def test_safety_and_composition_exceptions_are_still_not_caught(catalog: MetricCatalog) -> None:
    """協調の ``try`` は協調の部品だけを囲む（0078 §2.6 / 0028 §2.7）。"""
    config = coordination_config("apply")
    base = Harness(catalog, config=config)
    binding = create_control_runtime_binding(config)
    safety = ExplodingSafety(config.safety, input_contract=base.contract, runtime_binding=binding)
    composer = ExplodingComposer(config.safety)
    harness = Harness(
        catalog,
        config=config,
        safety=safety,
        composer=composer,
        backend=SimulatedFanBackend(config=config.fan_hardware, runtime_binding=binding),
    )
    settle_until(harness, AirBalanceCoordinationStatus.APPLIED)
    composer.explode = True
    with pytest.raises(RuntimeError, match="合成の不具合"):
        harness.tick()
    composer.explode = False
    safety.explode = True
    with pytest.raises(RuntimeError, match="Safety の不具合"):
        harness.tick()


def test_an_injected_coordinator_must_match_the_configured_mode(catalog: MetricCatalog) -> None:
    config = coordination_config("apply")
    coordinator, _ = scripted_coordinator(coordination_config("shadow"))
    with pytest.raises(ValueError, match="同じ mode"):
        Harness(catalog, config=config, air_balance_coordinator=coordinator)


# ------------------------------------------------------------- Gate（0078 §2.5 / §2.7）


def test_the_limited_band_is_centred_on_the_coordinated_baseline(catalog: MetricCatalog) -> None:
    harness, _ = learned_harness(catalog, "apply", AuthorityStage.LIMITED)
    for _ in range(12):
        tick = round_trip(harness.tick().tick)
        record = record_of(tick)
        if (
            tick.state.active_controller is ControllerKind.LEARNED_MPC
            and record.status is AirBalanceCoordinationStatus.APPLIED
        ):
            break
    else:
        raise AssertionError("Learned MPC と協調が同じ tick に揃わなかった")
    assert record.output is not None and record.candidate is not None
    limit_up = harness.config.policy.authority_limits.limited.limit_up
    front = tick.zones.front.demand.requested
    assert front == pytest.approx(min(LEARNED_DEMAND, record.output.front + limit_up))
    # raw baseline を中心にしていたら、もっと低く切り詰めていた。
    assert front > record.candidate.front + limit_up


def test_full_authority_leaves_the_learned_request_untouched(catalog: MetricCatalog) -> None:
    harness, _ = learned_harness(catalog, "apply", AuthorityStage.FULL)
    for _ in range(12):
        tick = round_trip(harness.tick().tick)
        if tick.state.active_controller is ControllerKind.LEARNED_MPC:
            break
    else:
        raise AssertionError("Learned MPC が選ばれなかった")
    assert record_of(tick).status is AirBalanceCoordinationStatus.APPLIED
    for zone in Zone:
        assert tick.zones.get(zone).demand.requested == LEARNED_DEMAND
        assert tick.zones.get(zone).controller_reason.code == "learned"


def test_the_transition_floor_sits_on_top_of_the_coordinated_baseline(
    catalog: MetricCatalog,
) -> None:
    harness, gate = learned_harness(catalog, "apply", AuthorityStage.FULL)
    for _ in range(12):
        if harness.tick().tick.state.active_controller is ControllerKind.LEARNED_MPC:
            break
    gate.healthy = False
    tick = round_trip(harness.tick().tick)
    assert tick.state.active_controller is ControllerKind.FALLBACK
    record = record_of(tick)
    assert record.output is not None
    front = tick.zones.front
    assert front.demand.requested == LEARNED_DEMAND >= record.output.front
    assert front.controller_reason.code == "fallback_transition_floor"
    assert "baseline_reason=air_balance_front_makeup_air" in front.controller_reason.detail


# ------------------------------------------------------------- 迂回（決定記録 0085 §2.4）


def run_until_learned(harness: Harness) -> ControlTick:
    for _ in range(12):
        tick = harness.tick().tick
        if tick.state.active_controller is ControllerKind.LEARNED_MPC:
            return tick
    raise AssertionError("Learned MPC が選ばれなかった")


@pytest.mark.parametrize(
    "stage", [AuthorityStage.LIMITED, AuthorityStage.EXPANDED, AuthorityStage.FULL]
)
@pytest.mark.parametrize("lowering", [False, True], ids=["exception", "lowering"])
def test_an_apply_failure_bypasses_the_gate(
    catalog: MetricCatalog, stage: AuthorityStage, lowering: bool
) -> None:
    config = coordination_config("apply", authority_stage="full")
    coordinator, model = scripted_coordinator(config)
    harness, gate = learned_harness(catalog, "apply", stage, air_balance_coordinator=coordinator)
    previous = run_until_learned(harness)
    calls = len(gate.calls)
    if lowering:
        model.lower = True
    else:
        model.error = RuntimeError("coordinate の不具合")
    tick = round_trip(harness.tick().tick)

    assert len(gate.calls) == calls  # Gate を呼ばない
    record = record_of(tick)
    assert record.status is AirBalanceCoordinationStatus.FAILED
    assert requested_of(tick) == record.candidate == record.output
    assert tick.state.active_controller is ControllerKind.FALLBACK
    assert tick.model_gate is None and tick.shadow is None
    assert tick.state.fallback_reason is not None
    assert tick.state.fallback_reason.code == "air_balance_coordination_failed"
    # 直前に Learned MPC が高く回していた zone も raw baseline まで下がりうるが、
    # effective の下げは ramp_down が制限する（合成は迂回しない）。
    rate = harness.config.safety.ramp_down_per_s.value
    step = harness.config.safety.tick_ms.value / 1_000
    for zone in Zone:
        before = previous.zones.get(zone).demand.effective
        after = tick.zones.get(zone).demand.effective
        assert after >= before - rate * step - 1e-9

    model.error = None
    model.lower = False
    emergency = round_trip(harness.tick().tick)
    assert emergency.state.safety_state is SafetyState.EMERGENCY
    exceptions = [fault for fault in emergency.faults if fault.code is FaultCode.FALLBACK_EXCEPTION]
    assert len(exceptions) == 1
    assert exceptions[0].detail.startswith("air_balance_coordination: ")


def test_a_shadow_failure_does_not_bypass_the_gate(catalog: MetricCatalog) -> None:
    config = coordination_config("shadow", authority_stage="full")
    coordinator, model = scripted_coordinator(config)
    harness, gate = learned_harness(
        catalog, "shadow", AuthorityStage.FULL, air_balance_coordinator=coordinator
    )
    reference, _ = learned_harness(catalog, "off", AuthorityStage.FULL)
    model.error = RuntimeError("coordinate の不具合")
    for _ in range(10):
        calls = len(gate.calls)
        left = reference.tick().tick
        right = round_trip(harness.tick().tick)
        assert left.state.safety_state is right.state.safety_state
        for zone in Zone:
            assert left.zones.get(zone).demand == right.zones.get(zone).demand
        if record_of(right).status is AirBalanceCoordinationStatus.FAILED:
            assert len(gate.calls) == calls + 1
    assert right.state.active_controller is ControllerKind.LEARNED_MPC


def test_the_bypass_reason_is_null_when_ml_could_not_run(catalog: MetricCatalog) -> None:
    """SHADOW の stage と DEGRADED の tick は ``fallback_reason`` を付けない（0085 §2.2）。"""
    config = coordination_config("apply")
    coordinator, model = scripted_coordinator(config)
    harness = Harness(catalog, config=config, air_balance_coordinator=coordinator)
    harness.settle()
    del harness.telemetry.values["cpu.package"]  # DEGRADED（Top だけ Max）
    model.error = RuntimeError("coordinate の不具合")
    tick = round_trip(harness.tick().tick)
    assert tick.state.safety_state is SafetyState.DEGRADED
    assert record_of(tick).status is AirBalanceCoordinationStatus.FAILED
    assert tick.state.fallback_reason is None


def test_a_persistent_failure_cycles_between_emergency_and_a_bypassed_tick(
    catalog: MetricCatalog,
) -> None:
    """解除と再試行の繰り返し（0085 §2.6）。Learned MPC はどの tick でも選ばれない。"""
    config = coordination_config("apply", authority_stage="full")
    coordinator, model = scripted_coordinator(config)
    harness, _ = learned_harness(
        catalog, "apply", AuthorityStage.FULL, air_balance_coordinator=coordinator
    )
    learned = run_until_learned(harness)
    model.error = RuntimeError("直らない不具合")
    statuses: list[tuple[SafetyState, AirBalanceCoordinationStatus, int]] = []
    previous_effective = learned.zones.front.demand.effective
    for _ in range(14):
        calls = len(model.calls)
        tick = round_trip(harness.tick().tick)
        record = record_of(tick)
        statuses.append((tick.state.safety_state, record.status, len(model.calls) - calls))
        assert tick.state.active_controller is not ControllerKind.LEARNED_MPC
        if tick.state.safety_state is SafetyState.EMERGENCY:
            # EMERGENCY の間は協調を呼ばない（fault は新たに観測されない）。
            assert record.status is AirBalanceCoordinationStatus.SKIPPED
            assert len(model.calls) == calls
        else:
            assert record.status is AirBalanceCoordinationStatus.FAILED
            # 離れる tick の Fan は Max の近くに留まる（ramp_down の分しか下がらない）。
            rate = config.safety.ramp_down_per_s.value
            step = config.safety.tick_ms.value / 1_000
            assert tick.zones.front.demand.effective >= previous_effective - rate * step - 1e-9
        previous_effective = tick.zones.front.demand.effective
    bypassed = [
        index
        for index, item in enumerate(statuses)
        if item[1] is AirBalanceCoordinationStatus.FAILED
    ]
    # 最初の失敗と、fault_clear_hold_ms の後の再試行が両方起きる。
    assert len(bypassed) >= 2
    for index in bypassed:
        if index + 1 < len(statuses):
            assert statuses[index + 1][0] is SafetyState.EMERGENCY


# ----------------------------------------------- trace の不変条件（0078 §2.7 / 0085 §2.3）


def _applied_document(catalog: MetricCatalog) -> dict[str, Any]:
    harness = Harness(catalog, config=coordination_config("apply"))
    document: dict[str, Any] = json.loads(
        settle_until(harness, AirBalanceCoordinationStatus.APPLIED).model_dump_json()
    )
    return document


def _bypassed_document(catalog: MetricCatalog) -> dict[str, Any]:
    config = coordination_config("apply", authority_stage="full")
    coordinator, model = scripted_coordinator(config)
    harness, _ = learned_harness(
        catalog, "apply", AuthorityStage.FULL, air_balance_coordinator=coordinator
    )
    learned = run_until_learned(harness)
    model.error = RuntimeError("coordinate の不具合")
    document: dict[str, Any] = json.loads(harness.tick().tick.model_dump_json())
    document["_learned_model_gate"] = json.loads(learned.model_dump_json())["model_gate"]
    return document


def _validate(document: dict[str, Any]) -> ControlTick:
    return ControlTick.model_validate_json(json.dumps(document))


def test_an_output_above_the_recorded_max_raise_is_refused(catalog: MetricCatalog) -> None:
    document = _applied_document(catalog)
    block = document["air_balance_coordination"]
    block["max_raise"]["front"] = 0.05
    with pytest.raises(ValidationError, match="max_raise"):
        _validate(document)


def test_a_requested_below_the_output_is_refused(catalog: MetricCatalog) -> None:
    document = _applied_document(catalog)
    block = document["air_balance_coordination"]
    block["max_raise"]["front"] = 0.5
    raised = block["output"]["front"] + 0.01
    block["output"]["front"] = raised
    block["counterfactual_output"]["front"] = raised
    with pytest.raises(ValidationError, match="output を下回る"):
        _validate(document)


def test_a_coordination_failed_reason_on_another_tick_is_refused(catalog: MetricCatalog) -> None:
    document = _applied_document(catalog)
    document["state"]["fallback_reason"] = {
        "code": "air_balance_coordination_failed",
        "detail": "x",
    }
    with pytest.raises(ValidationError, match="air_balance_coordination_failed"):
        _validate(document)


def test_the_bypassed_tick_invariants(catalog: MetricCatalog) -> None:
    document = _bypassed_document(catalog)
    learned_gate = document.pop("_learned_model_gate")
    _validate(document)

    with_gate = json.loads(json.dumps(document))
    with_gate["model_gate"] = learned_gate
    with pytest.raises(ValidationError, match="model_gate / shadow"):
        _validate(with_gate)

    wrong_reason = json.loads(json.dumps(document))
    wrong_reason["state"]["fallback_reason"]["code"] = "controller_gate_unavailable"
    with pytest.raises(ValidationError, match="air_balance_coordination_failed"):
        _validate(wrong_reason)

    other_request = json.loads(json.dumps(document))
    block = other_request["air_balance_coordination"]
    block["candidate"]["rear"] = block["output"]["rear"] = 0.0
    with pytest.raises(ValidationError, match="raw baseline"):
        _validate(other_request)


def test_unconfirmed_tach_must_be_sorted_and_explain_the_skip(catalog: MetricCatalog) -> None:
    document = _applied_document(catalog)
    with_tach = json.loads(json.dumps(document))
    with_tach["tach_unconfirmed_zones"] = ["rear"]
    with pytest.raises(ValidationError, match="skipped"):
        _validate(with_tach)

    unsorted = json.loads(json.dumps(document))
    unsorted["tach_unconfirmed_zones"] = ["top", "front"]
    with pytest.raises(ValidationError, match="昇順"):
        _validate(unsorted)

    block = document["air_balance_coordination"]
    candidate = block["candidate"]
    document["air_balance_coordination"] = {
        "schema_version": 1,
        "mode": "apply",
        "status": "skipped",
        "skip_reason": "tach_unconfirmed",
        "candidate": candidate,
        "output": candidate,
        "max_raise": block["max_raise"],
        "held": {"front": False, "rear": False, "top": False},
    }
    for zone in ("front", "rear", "top"):
        document["zones"][zone]["controller_reason"] = {"code": "fallback_baseline", "detail": ""}
        document["zones"][zone]["demand"]["requested"] = candidate[zone]
    document["zones"]["front"]["demand"]["bound_by"] = "ramp_down"
    with pytest.raises(ValidationError, match="未確認の zone がある"):
        _validate(document)


def test_a_v13_tick_cannot_carry_the_coordination(catalog: MetricCatalog) -> None:
    document = _applied_document(catalog)
    document["schema_version"] = 13
    with pytest.raises(ValidationError, match="schema version 14"):
        _validate(document)
    del document["air_balance_coordination"]
    with pytest.raises(ValidationError, match="schema version 14"):
        _validate(document)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"mode": "off", "status": "off"}, "mode: off"),
        ({"status": "shadow"}, "status: shadow"),
        ({"projected_floor_basis": {"front": "forced_max", "rear": "none", "top": "none"}}, "1.0"),
        ({"counterfactual_output": None}, "保持込み"),
    ],
)
def test_record_invariants_are_enforced(
    catalog: MetricCatalog, change: dict[str, Any], message: str
) -> None:
    document = _applied_document(catalog)
    document["air_balance_coordination"].update(change)
    with pytest.raises(ValidationError, match=message):
        _validate(document)


@pytest.mark.parametrize("held", [None, {"front": True, "rear": False, "top": False}])
def test_a_skipped_tick_must_record_an_explicit_release(
    catalog: MetricCatalog, held: dict[str, bool] | None
) -> None:
    """``skipped`` / ``failed`` の ``held`` は null を許さず、3 zone とも偽（0088 §2.2）。"""
    harness = Harness(catalog, config=coordination_config("apply"))
    document: dict[str, Any] = json.loads(harness.tick().tick.model_dump_json())
    block = document["air_balance_coordination"]
    assert block["status"] == "skipped"
    assert block["held"] == {"front": False, "rear": False, "top": False}
    block["held"] = held
    with pytest.raises(ValidationError, match="保持を解く"):
        _validate(document)


def _skipped_document(catalog: MetricCatalog) -> dict[str, Any]:
    harness = Harness(catalog, config=coordination_config("apply"))
    document: dict[str, Any] = json.loads(harness.tick().tick.model_dump_json())
    assert document["air_balance_coordination"]["status"] == "skipped"
    return document


def _failed_document(catalog: MetricCatalog) -> dict[str, Any]:
    config = coordination_config("apply")
    coordinator, model = scripted_coordinator(config)
    harness = Harness(catalog, config=config, air_balance_coordinator=coordinator)
    harness.settle()
    model.error = RuntimeError("coordinate の不具合")
    document: dict[str, Any] = json.loads(harness.tick().tick.model_dump_json())
    assert document["air_balance_coordination"]["status"] == "failed"
    return document


FLOORS = {"front": 0.4, "rear": 0.4, "top": 0.5}
BASIS = {"front": "safety_floor", "rear": "safety_floor", "top": "safety_floor"}


@pytest.mark.parametrize("kind", ["skipped", "failed"])
@pytest.mark.parametrize(
    "change",
    [
        {"proposed": FLOORS},
        {"bounded_by_max_raise": {"front": False, "rear": False, "top": False}},
        {"before_state": "balanced"},
        {"before_ratio": 0.9},
        {"projected_state": "balanced"},
        {"projected_ratio": 0.9},
        {"reasons": ["front_makeup_air"]},
    ],
)
def test_records_without_a_result_cannot_carry_result_fields(
    catalog: MetricCatalog, kind: str, change: dict[str, Any]
) -> None:
    """``skipped`` / ``failed`` は協調の結果の欄を持たない（0088 §2.2）。"""
    document = _skipped_document(catalog) if kind == "skipped" else _failed_document(catalog)
    _validate(document)
    document["air_balance_coordination"].update(change)
    with pytest.raises(ValidationError, match="結果の欄"):
        _validate(document)


def test_projected_floors_are_null_when_skipped_and_kept_when_failed(
    catalog: MetricCatalog,
) -> None:
    skipped = _skipped_document(catalog)
    skipped["air_balance_coordination"].update(
        {"projected_floors": FLOORS, "projected_floor_basis": BASIS}
    )
    with pytest.raises(ValidationError, match="下限を見込まない"):
        _validate(skipped)
    failed = _failed_document(catalog)
    assert failed["air_balance_coordination"]["projected_floors"] is not None
    failed["air_balance_coordination"].update(
        {"projected_floors": None, "projected_floor_basis": None}
    )
    with pytest.raises(ValidationError, match="projected_floors を記録する"):
        _validate(failed)


def test_a_skip_reason_after_the_baseline_check_needs_the_baseline(catalog: MetricCatalog) -> None:
    """表の順（0078 §2.3）と raw baseline の有無を突き合わせる（0088 §2.2）。"""
    document = _skipped_document(catalog)
    block = document["air_balance_coordination"]
    assert block["skip_reason"] == "safety_state"
    without = json.loads(json.dumps(document))
    without["air_balance_coordination"].update({"candidate": None, "output": None})
    with pytest.raises(ValidationError, match="baseline_unavailable"):
        _validate(without)
    fabricated = json.loads(json.dumps(document))
    fabricated["air_balance_coordination"]["skip_reason"] = "baseline_unavailable"
    with pytest.raises(ValidationError, match="baseline_unavailable"):
        _validate(fabricated)


@pytest.mark.parametrize(
    "change",
    [
        {"before_state": None, "before_ratio": None},
        {"projected_state": None, "projected_ratio": None},
        {"before_state": "unknown"},
        {"projected_ratio": None},
    ],
)
def test_a_coordinated_record_carries_both_estimates(
    catalog: MetricCatalog, change: dict[str, Any]
) -> None:
    document = _applied_document(catalog)
    _validate(document)
    document["air_balance_coordination"].update(change)
    with pytest.raises(ValidationError, match=r"_state|_ratio"):
        _validate(document)
