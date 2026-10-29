"""Workload Hint の B1 の Shadow solver（worker 側。#288 / 決定記録 0133 §2.5・0152 §2.6・
0157 §2.3・0162 §2.2 / §2.3 / §2.5 の PR-B）。

守りたいこと（実機不要。ルール 7。実時間の CPU の取り合いは ``test_hint_shadow_realtime.py``）:

1. 子の判断: 部品・起動の形・``window_ms`` / ``stale_after_ms`` の照合、worker の認証領域の判定、
   残りの予算での二重解法、2 条件。どれかに落ちれば ``skipped`` と閉じた理由。全部通れば両 plan
2. 親の受け渡し: 基準を送った後に1枠の依頼。子が途中なら ``shadow_busy``、使えなければ数だけ、
   未転送は1件・期限つき・上限つき。累積の数と ``worker_instance_id`` を毎回載せる
3. 周期のループ: 子が遅れる・例外・死ぬ・応答しない、のどれでも基準の ``MpcProposal`` の内容と
   送信時刻（注入した時計）がヒントを扱わない worker と同じ。基準の送信が依頼より先
4. 経路: Shadow の送信は lock を待たず、送信待ちが空のときだけ。詰まっても基準の送信は
   ヒントを扱わない worker と同じ時点まで通る。記録が経路に2件以上残らない
5. 実際の子プロセス: ``SCHED_IDLE`` に下げられなければ何もせず終了し作り直さない・下げた子は
   ``SCHED_IDLE``・死んだ子は間隔をあけて作り直す・親が死ねば子も終わる
6. 受け口を開いていない fand は ``hint_shadow`` を ``unsupported_body`` として捨て、生存に数えない
7. 増える経路は記録だけ: 本文は Demand の上書き・モード・authority を運べない（ルール 2）
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SystemMonotonicClock
from coldaisle.control.fallback.gate import LearnedFailure
from coldaisle.control.learned_handoff import LearnedChannelState, LearnedFrame, LearnedRole
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.schema import Reason
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.learned_channel.messages import (
    HintShadowBody,
    HintShadowSkipReason,
    HintShadowStatus,
    InboundEnvelope,
    parse_inbound,
)
from coldaisle.learned_worker.cli import MpcWorker, main
from coldaisle.learned_worker.client import Hello, ShadowSendOutcome, WorkerChannel
from coldaisle.learned_worker.mpc import StepInput
from coldaisle.learned_worker.shadow import HintShadowCoordinator
from coldaisle.learned_worker.shadow_solver import (
    ShadowChildArgs,
    ShadowOutcome,
    ShadowRequest,
    ShadowSolverProcess,
    SolverEvents,
    SolverState,
    compute_static_components,
)
from hint_shadow_world import (
    METRICS,
    REGISTRY_LIMITS_DIR,
    STATIC,
    World,
    build_world,
    demands,
    write_control,
)
from test_control_config import provisional
from test_hint_certify import certify_documents, registry_root
from test_hint_gate_spec import limits_block
from test_learned_channel import (
    MPC_UID,
    channel,
    connect,
    no_file_groups,
    short_dir,
    wait_until,
)
from test_learned_channel import (
    RUN_ID as CHANNEL_RUN_ID,
)
from test_learned_mpc import trained
from test_learned_worker import CONTROL, RUN_ID, core_for, frame, pinned_of, ticks

__all__ = ["channel", "no_file_groups", "registry_root", "short_dir", "trained"]

TESTS = Path(__file__).resolve().parent
SPAWN_WAIT_S = 60.0
"""spawn の子が import を終えて応答するまでの待ちの上限（遅い CI でも落ちない長さ）。"""


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory, registry_root: Path) -> World:
    config_dir = tmp_path_factory.mktemp("shadow-config") / "config"
    write_control(config_dir)
    return build_world(config_dir, registry_root)


# ================================================================ 1. 子の判断


def test_a_grid_point_with_every_component_matching_is_recorded(world: World) -> None:
    outcome = world.core().handle(world.request)
    assert outcome.status is HintShadowStatus.RECORDED, outcome
    assert outcome.reason is None
    assert outcome.plans is not None
    assert outcome.plans.base.requested == world.base_solution.requested
    assert outcome.plans.base.targets == world.base_solution.prediction.targets
    assert outcome.plans.hint_target_band == world.request.hint_supervisor.target_band
    assert outcome.conditions_sha256 == world.request.certification.components.conditions


@pytest.mark.parametrize(
    "component",
    [
        "source_tree",
        "lockfile",
        "environment",
        "hint_gate",
        "optimizer",
        "conditions",
        "calibration",
    ],
)
def test_one_component_off_is_a_certification_mismatch(world: World, component: str) -> None:
    """部品を1つずつ変えると、子は解かずに ``certification_mismatch``（0152 §2.6 / §2.8）。"""
    outcome = world.core().handle(world.with_components(**{component: "9" * 64}))
    assert outcome.status is HintShadowStatus.SKIPPED
    assert outcome.reason is HintShadowSkipReason.CERTIFICATION_MISMATCH
    assert outcome.plans is None


@pytest.mark.parametrize("missing", ["source_tree", "lockfile", "environment"])
def test_a_component_the_child_could_not_compute_never_matches(world: World, missing: str) -> None:
    """読めない lockfile・``other`` の起動の形（environment を計算しない）は不一致（0157 §2.3）。"""
    components = replace(STATIC, **{missing: None})
    outcome = world.core(components=components).handle(world.request)
    assert outcome.reason is HintShadowSkipReason.CERTIFICATION_MISMATCH


def test_a_window_longer_than_the_fand_lookback_is_a_certification_mismatch(world: World) -> None:
    certification = world.request.certification
    shorter = certification.model_copy(
        update={"domain_lookback_ms": certification.domain_lookback_ms - 1}
    )
    outcome = world.core().handle(world.with_request(certification=shorter))
    assert outcome.reason is HintShadowSkipReason.CERTIFICATION_MISMATCH


def test_an_artifact_stale_threshold_below_the_fand_one_is_a_certification_mismatch(
    world: World,
) -> None:
    certification = world.request.certification
    looser = certification.model_copy(
        update={"domain_stale_after_ms": certification.domain_stale_after_ms + 1}
    )
    outcome = world.core().handle(world.with_request(certification=looser))
    assert outcome.reason is HintShadowSkipReason.CERTIFICATION_MISMATCH


def test_a_gate_the_child_cannot_derive_is_a_certification_mismatch(
    world: World, tmp_path: Path
) -> None:
    docs = certify_documents()
    docs["safety.yaml"]["workload_hint"] = limits_block(max_eps=provisional(0.001))
    broken = write_control(tmp_path / "config", docs)
    core = replace(world, control=broken).core()
    # 設定の digest は揃え、ゲートの仕様だけが導けない状態にする
    request = world.with_request(config=broken.runtime_digest())
    assert core.handle(request).reason is HintShadowSkipReason.CERTIFICATION_MISMATCH


def test_a_config_or_catalog_other_than_the_frame_is_a_certification_mismatch(
    world: World, tmp_path: Path
) -> None:
    """子が親の後に読んだ設定・catalog が frame と違えば解かない（基準と別の設定で比べない）。"""
    docs = certify_documents(budget_ms=900)
    other = write_control(tmp_path / "config", docs)
    assert other.runtime_digest() != world.request.config
    core = replace(world, control=other).core()
    assert core.handle(world.request).reason is HintShadowSkipReason.CERTIFICATION_MISMATCH
    stale_catalog = world.with_request(metric_catalog_sha256="0" * 64)
    assert world.core().handle(stale_catalog).reason is (
        HintShadowSkipReason.CERTIFICATION_MISMATCH
    )


def test_one_hot_frame_in_the_window_is_out_of_the_certified_domain(world: World) -> None:
    frames = list(world.request.domain_frames)
    hot = frames[1]
    assert hot.cpu_temperature is not None
    frames[1] = replace(hot, cpu_temperature=replace(hot.cpu_temperature, value=500.0))
    outcome = world.core().handle(world.with_request(domain_frames=tuple(frames)))
    assert outcome.reason is HintShadowSkipReason.OUT_OF_CERTIFIED_DOMAIN


def test_a_gap_in_the_window_is_out_of_the_certified_domain(world: World) -> None:
    frames = list(world.request.domain_frames)
    # 送り出せなかった tick がある（tick_id の飛び）
    frames[-1] = replace(frames[-1], tick_id=frames[-1].tick_id + 1)
    outcome = world.core().handle(world.with_request(domain_frames=tuple(frames)))
    assert outcome.reason is HintShadowSkipReason.OUT_OF_CERTIFIED_DOMAIN


def test_a_hint_band_from_no_override_is_out_of_the_certified_domain(world: World) -> None:
    """frame はヒントの workload を運ばない。band がどの上書きからも来ていなければ鍵が無い。"""
    stranger = world.request.hint_supervisor.model_copy(
        update={"target_band": world.request.base_supervisor.target_band}
    )
    outcome = world.core().handle(world.with_request(hint_supervisor=stranger))
    assert outcome.reason is HintShadowSkipReason.OUT_OF_CERTIFIED_DOMAIN


def test_a_previous_demand_outside_the_grid_is_out_of_the_certified_domain(world: World) -> None:
    outcome = world.core().handle(world.with_request(previous_applied=None))
    assert outcome.reason is HintShadowSkipReason.OUT_OF_CERTIFIED_DOMAIN


def test_a_passed_deadline_is_budget_exhausted_before_any_solve(world: World) -> None:
    late = world.core(clock_ms=world.request.deadline_mono_ms)
    outcome = late.handle(world.request)
    assert outcome.reason is HintShadowSkipReason.BUDGET_EXHAUSTED
    assert outcome.conditions_sha256 is None  # 束縛の前に止まった


def test_a_remaining_budget_too_small_to_solve_is_budget_exhausted(world: World) -> None:
    outcome = world.core().handle(world.with_request(remaining_evaluations=1))
    assert outcome.reason is HintShadowSkipReason.BUDGET_EXHAUSTED


def test_a_budget_above_the_configuration_is_not_spent(world: World) -> None:
    """予算を増やす経路を作らない。設定より多い評価の回数は solve が拒み、解かない（0160 §2.4）。"""
    too_many = world.control.policy.mpc.optimizer.max_evaluations.value + 1
    outcome = world.core().handle(world.with_request(remaining_evaluations=too_many))
    assert outcome.reason is HintShadowSkipReason.BUDGET_EXHAUSTED


def test_a_base_solved_on_another_window_is_a_base_mismatch(world: World) -> None:
    outcome = world.core().handle(world.with_request(base_anchor_inference_id="e" * 64))
    assert outcome.reason is HintShadowSkipReason.BASE_MISMATCH


def test_a_hint_plan_below_the_base_by_more_than_eps_is_monotonicity_violated(
    world: World,
) -> None:
    outcome = world.core().handle(world.with_request(base_requested=demands(1.0, 1.0, 1.0)))
    assert outcome.reason is HintShadowSkipReason.MONOTONICITY_VIOLATED


def test_an_artifact_digest_that_differs_from_the_pin_is_an_artifact_mismatch(
    world: World,
) -> None:
    pinned = world.pinned.model_copy(update={"artifact_sha256": "d" * 64})
    outcome = world.core().handle(world.with_request(thermal_model=pinned))
    assert outcome.reason is HintShadowSkipReason.ARTIFACT_MISMATCH


def test_the_child_binds_once_per_artifact_calibration_and_stage(world: World) -> None:
    core = world.core()
    loads: list[Any] = []
    original = core._artifacts.load

    def counting(*args: Any) -> Any:
        loads.append(args)
        return original(*args)

    core._artifacts.load = counting  # type: ignore[method-assign]
    for _ in range(3):
        assert core.handle(world.request).status is HintShadowStatus.RECORDED
    assert len(loads) == 1


def test_static_components_follow_the_launch_form(tmp_path: Path) -> None:
    """``other`` の親・``-I`` でない子では environment を計算しない。lockfile が無ければ None。"""
    lockfile = tmp_path / "uv.lock"
    lockfile.write_bytes(b"version = 1\n")
    other = compute_static_components(lockfile=lockfile, parent_isolated=False)
    assert other.environment is None
    assert other.lockfile is not None and other.source_tree is not None
    # pytest は -I で動いていないので、親が isolated でも子自身の形で計算しない
    assert compute_static_components(lockfile=None, parent_isolated=True).environment is None
    assert compute_static_components(lockfile=None, parent_isolated=False).lockfile is None
    missing = compute_static_components(lockfile=tmp_path / "absent", parent_isolated=False)
    assert missing.lockfile is None


# ================================================================ 2. 親の受け渡し


@dataclass
class FakeSolver:
    """Shadow solver の子の偽物（状態と結果を試験が決める）。"""

    state: SolverState = SolverState.IDLE
    log: list[tuple[str, int]] | None = None
    clock: Callable[[], int] = lambda: 0
    submitted: list[ShadowRequest] = field(default_factory=list)
    started: list[int] = field(default_factory=list)
    delay_services: int = 0
    behaviour: str = "answer"
    closed: bool = False
    _waiting: int = 0

    def start(self, now_ms: int) -> None:
        self.started.append(now_ms)

    def submit(self, request: ShadowRequest) -> None:
        if self.behaviour == "raise_on_submit":
            raise RuntimeError("偽の子の失敗")
        assert self.state is SolverState.IDLE
        self.submitted.append(request)
        if self.log is not None:
            self.log.append(("submit", self.clock()))
        self.state = SolverState.BUSY
        self._waiting = self.delay_services

    def service(self, now_ms: int) -> SolverEvents:
        if self.behaviour == "raise":
            raise RuntimeError("偽の子の失敗")
        if self.state is not SolverState.BUSY or not self.submitted:
            return SolverEvents()
        request = self.submitted[-1]
        if self.behaviour == "unresponsive":
            return SolverEvents()
        if self.behaviour == "die":
            self.state = SolverState.WAITING_RESPAWN
            return SolverEvents(lost=(request.seq,))
        if self._waiting > 0:
            self._waiting -= 1
            return SolverEvents()
        self.state = SolverState.IDLE
        return SolverEvents(
            results=(
                ShadowOutcome(
                    seq=request.seq,
                    status=HintShadowStatus.SKIPPED,
                    reason=HintShadowSkipReason.MONOTONICITY_VIOLATED,
                    plans=None,
                    conditions_sha256="6" * 64,
                ),
            )
        )

    def readable_fds(self) -> list[int]:
        return []

    def writable_fds(self) -> list[int]:
        return []

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeSender:
    outcomes: list[ShadowSendOutcome] = field(default_factory=list)
    sent: list[bytes] = field(default_factory=list)
    tried: int = 0

    def try_send_shadow(self, data: bytes) -> ShadowSendOutcome:
        self.tried += 1
        outcome = self.outcomes.pop(0) if self.outcomes else ShadowSendOutcome.SENT
        if outcome is ShadowSendOutcome.SENT:
            self.sent.append(data)
        return outcome

    def bodies(self) -> list[HintShadowBody]:
        bodies = []
        for data in self.sent:
            envelope = parse_inbound(data)
            assert envelope.role is LearnedRole.MPC
            assert isinstance(envelope.body, HintShadowBody)
            bodies.append(envelope.body)
        return bodies


class Clock:
    def __init__(self, now: int = 0) -> None:
        self.now = now

    def monotonic_ms(self) -> int:
        return self.now


@dataclass
class Step:
    step: StepInput
    base: MpcProposal


@pytest.fixture(scope="module")
def hinted_step(trained: Any) -> Step:
    """本物の worker の core が、ヒントの欄を持つ frame で作った基準と入力。"""
    core = core_for(trained)
    for item in ticks(10, 3, pins=pinned_of(trained), with_hint=True):
        core.receive(RUN_ID, item)
    base = core.step()
    assert base is not None and base.solution is not None, base
    assert core.last_input is not None
    return Step(step=core.last_input, base=base)


def coordinator(
    solver: FakeSolver | None,
    sender: FakeSender,
    clock: Clock,
    *,
    isolated: bool = True,
    max_record_bytes: int = 65_536,
) -> HintShadowCoordinator:
    return HintShadowCoordinator(
        solver=solver,
        sender=sender,
        control=CONTROL,
        max_record_bytes=max_record_bytes,
        parent_isolated=isolated,
        monotonic_ms=clock.monotonic_ms,
        worker_instance_id="ab" * 16,
    )


def after_base(shadow: HintShadowCoordinator, step: Step, *, started: int = 0, due: int = 1_000):
    shadow.after_base(
        run_id=RUN_ID, step=step.step, base=step.base, base_started_ms=started, next_due_ms=due
    )


def test_the_request_carries_the_remaining_budget_and_the_shared_deadline(
    hinted_step: Step,
) -> None:
    solver, clock = FakeSolver(), Clock(100)
    shadow = coordinator(solver, FakeSender(), clock)
    after_base(shadow, hinted_step, started=50, due=800)
    (request,) = solver.submitted
    policy = CONTROL.policy
    assert hinted_step.base.solution is not None
    assert request.remaining_evaluations == (
        policy.mpc.optimizer.max_evaluations.value - hinted_step.base.solution.evaluations
    )
    # min(基準の solve の開始 + mpc.budget_ms, 次の周期の開始)
    assert request.deadline_mono_ms == min(50 + policy.mpc.budget_ms, 800)
    frame_ = hinted_step.step.frame
    assert request.hint_supervisor == frame_.supervisor_hint
    assert request.certification == frame_.hint_certification
    assert request.observed == hinted_step.step.observed
    # window を作った frame の列（基準の window と同じ並び）から軸の値だけを取り出す
    assert [item.tick_id for item in request.domain_frames] == [
        item.snapshot.tick_id for item in hinted_step.step.frames
    ]
    assert request.domain_frames[-1].tick_id == frame_.snapshot.tick_id
    assert request.base_anchor_inference_id == hinted_step.base.solution.anchor_inference_id


def test_a_result_becomes_one_record_with_the_epoch_and_counters(hinted_step: Step) -> None:
    solver, sender, clock = FakeSolver(), FakeSender(), Clock()
    shadow = coordinator(solver, sender, clock)
    after_base(shadow, hinted_step)
    shadow.service()
    (body,) = sender.bodies()
    snapshot = hinted_step.step.frame.snapshot
    assert body.worker_instance_id == "ab" * 16
    assert (body.source_tick_id, body.source_ts_ms, body.source_monotonic_ms) == (
        snapshot.tick_id,
        snapshot.ts_ms,
        snapshot.monotonic_ms,
    )
    assert body.base_result_digest == hinted_step.base.result_digest()
    assert body.reason is HintShadowSkipReason.MONOTONICITY_VIOLATED
    assert body.conditions_sha256 == "6" * 64
    assert body.counters.model_dump() == {
        "shadow_dropped_backpressure": 0,
        "shadow_record_too_large": 0,
        "shadow_unavailable": 0,
    }
    assert hinted_step.step.frame.hint_certification is not None
    assert body.certificate_sha256 == hinted_step.step.frame.hint_certification.certificate_sha256


def test_a_busy_child_drops_the_request_as_shadow_busy(hinted_step: Step) -> None:
    solver, sender, clock = FakeSolver(delay_services=10), FakeSender(), Clock()
    shadow = coordinator(solver, sender, clock)
    after_base(shadow, hinted_step)
    after_base(shadow, hinted_step, due=2_000)
    assert len(solver.submitted) == 1, "依頼を溜めない"
    (body,) = sender.bodies()
    assert body.reason is HintShadowSkipReason.SHADOW_BUSY
    assert body.conditions_sha256 is None


@pytest.mark.parametrize(
    "state",
    [
        SolverState.NOT_STARTED,
        SolverState.STARTING,
        SolverState.WAITING_RESPAWN,
        SolverState.DISABLED,
    ],
)
def test_an_unavailable_child_is_counted_without_a_record(
    hinted_step: Step, state: SolverState
) -> None:
    solver, sender, clock = FakeSolver(state=state), FakeSender(), Clock(7)
    shadow = coordinator(solver, sender, clock)
    after_base(shadow, hinted_step)
    assert not solver.submitted and not sender.sent
    assert shadow.counters.shadow_unavailable == 1
    assert solver.started == [7], "最初のヒントの依頼で子を作るよう頼む"


def test_no_solver_at_all_counts_unavailable(hinted_step: Step) -> None:
    shadow = coordinator(None, FakeSender(), Clock())
    after_base(shadow, hinted_step)
    assert shadow.counters.shadow_unavailable == 1


def test_an_other_launch_form_answers_certification_mismatch_without_a_child(
    hinted_step: Step,
) -> None:
    solver, sender = FakeSolver(), FakeSender()
    shadow = coordinator(solver, sender, Clock(), isolated=False)
    after_base(shadow, hinted_step)
    assert not solver.submitted
    (body,) = sender.bodies()
    assert body.reason is HintShadowSkipReason.CERTIFICATION_MISMATCH


def test_a_spent_budget_answers_budget_exhausted_without_a_request(hinted_step: Step) -> None:
    solver, sender = FakeSolver(), FakeSender()
    # 基準の solve の開始 + mpc.budget_ms（1000 ms）を過ぎたが、次の周期の開始の前
    shadow = coordinator(solver, sender, Clock(2_000))
    after_base(shadow, hinted_step, started=0, due=10_000)
    assert not solver.submitted
    (body,) = sender.bodies()
    assert body.reason is HintShadowSkipReason.BUDGET_EXHAUSTED


def test_a_frame_without_the_hint_or_a_base_without_a_solution_does_nothing(
    trained: Any, hinted_step: Step
) -> None:
    solver, sender = FakeSolver(), FakeSender()
    shadow = coordinator(solver, sender, Clock())
    plain = replace(hinted_step.step, frame=hinted_step.step.frame.without_hint())
    shadow.after_base(
        run_id=RUN_ID, step=plain, base=hinted_step.base, base_started_ms=0, next_due_ms=1_000
    )
    failed = MpcProposal(
        failure=LearnedFailure.MODEL_LOAD_FAILURE,
        failure_reason=Reason(code="model_unusable", detail="試験"),
    )
    shadow.after_base(
        run_id=RUN_ID, step=hinted_step.step, base=failed, base_started_ms=0, next_due_ms=1_000
    )
    assert not solver.submitted and not sender.sent
    assert shadow.counters.shadow_unavailable == 0


def test_only_one_unforwarded_record_is_kept(hinted_step: Step) -> None:
    sender = FakeSender(outcomes=[ShadowSendOutcome.QUEUE_NOT_EMPTY] * 3)
    shadow = coordinator(FakeSolver(state=SolverState.BUSY), sender, Clock())
    after_base(shadow, hinted_step)
    after_base(shadow, hinted_step)
    after_base(shadow, hinted_step)
    assert shadow.counters.shadow_dropped_backpressure == 2
    shadow.service()
    (body,) = sender.bodies()
    # 送れた記録は、捨てた数を運ぶ
    assert body.counters.shadow_dropped_backpressure == 2


def test_an_unforwarded_record_expires_at_the_next_period(hinted_step: Step) -> None:
    clock = Clock()
    sender = FakeSender(outcomes=[ShadowSendOutcome.LOCK_BUSY])
    shadow = coordinator(FakeSolver(state=SolverState.BUSY), sender, clock)
    after_base(shadow, hinted_step, due=1_000)
    clock.now = 1_000
    shadow.service()
    assert not sender.sent
    assert shadow.counters.shadow_dropped_backpressure == 1


@pytest.mark.parametrize(
    "outcome",
    [
        ShadowSendOutcome.LOCK_BUSY,
        ShadowSendOutcome.QUEUE_NOT_EMPTY,
        ShadowSendOutcome.WOULD_BLOCK,
    ],
)
def test_a_record_that_cannot_go_now_is_retried_on_the_next_wait(
    hinted_step: Step, outcome: ShadowSendOutcome
) -> None:
    sender = FakeSender(outcomes=[outcome])
    shadow = coordinator(FakeSolver(state=SolverState.BUSY), sender, Clock())
    after_base(shadow, hinted_step)
    assert not sender.sent
    shadow.service()
    assert len(sender.sent) == 1
    assert shadow.counters.shadow_dropped_backpressure == 0


def test_a_failed_send_drops_the_record(hinted_step: Step) -> None:
    sender = FakeSender(outcomes=[ShadowSendOutcome.FAILED])
    shadow = coordinator(FakeSolver(state=SolverState.BUSY), sender, Clock())
    after_base(shadow, hinted_step)
    shadow.service()
    assert not sender.sent
    assert shadow.counters.shadow_dropped_backpressure == 1


def test_a_record_over_the_limit_is_dropped_whole(hinted_step: Step) -> None:
    sender = FakeSender()
    shadow = coordinator(FakeSolver(state=SolverState.BUSY), sender, Clock(), max_record_bytes=256)
    after_base(shadow, hinted_step)
    assert sender.tried == 0 and not sender.sent
    assert shadow.counters.shadow_record_too_large == 1


def test_a_lost_request_is_counted_as_unavailable(hinted_step: Step) -> None:
    solver, sender = FakeSolver(behaviour="die"), FakeSender()
    shadow = coordinator(solver, sender, Clock())
    after_base(shadow, hinted_step)
    shadow.service()
    assert not sender.sent
    assert shadow.counters.shadow_unavailable == 1


# ================================================================ 3. 周期のループ（注入した時計）


class ScriptedChannel:
    """frame を予定の時刻に渡し、送った結果を時刻とともに記録する偽の経路。"""

    def __init__(self, clock: Clock, frames: list[tuple[int, LearnedFrame]], log: list[Any]):
        self.clock = clock
        self.pending = list(frames)
        self.log = log
        self.sent: list[tuple[int, dict[str, Any]]] = []
        self.shadow: list[tuple[int, bytes]] = []

    def receive_hello(self, *, timeout_s: float) -> Hello:
        return Hello(run_id=RUN_ID, heartbeat_interval_ms=3_600_000)

    def receive_frames(self, *, timeout_s: float) -> list[Any]:
        from coldaisle.learned_worker.client import ReceivedFrame

        target = self.clock.now + round(timeout_s * 1_000)
        if self.pending and self.pending[0][0] <= target:
            self.clock.now = max(self.clock.now, self.pending[0][0])
            delivered = []
            while self.pending and self.pending[0][0] <= self.clock.now:
                delivered.append(ReceivedFrame(run_id=RUN_ID, frame=self.pending.pop(0)[1]))
            return delivered
        self.clock.now = target
        return []

    def send_result(self, run_id: str, result: MpcProposal) -> None:
        self.sent.append((self.clock.now, result.model_dump(mode="json")))
        self.log.append(("base", self.clock.now))

    def send_heartbeat(self, run_id: str) -> None:
        pass

    def try_send_shadow(self, data: bytes) -> ShadowSendOutcome:
        self.shadow.append((self.clock.now, data))
        return ShadowSendOutcome.SENT

    def close(self) -> None:
        pass


PERIODS = 8


def run_loop(trained: Any, solver: FakeSolver | None, *, with_shadow: bool) -> ScriptedChannel:
    clock = Clock()
    log: list[Any] = []
    frames = [
        (tick * 1_000 - 600, frame(10 + tick, pins=pinned_of(trained), with_hint=True))
        for tick in range(1, PERIODS + 3)
    ]
    channel_ = ScriptedChannel(clock, frames, log)
    shadow = None
    if with_shadow:
        if solver is not None:
            solver.log = log
            solver.clock = clock.monotonic_ms
        shadow = coordinator(solver, channel_, clock)  # type: ignore[arg-type]
    worker = MpcWorker(
        core_for(trained),
        channel_,  # type: ignore[arg-type]
        period_ms=1_000,
        hello_timeout_ms=1_000,
        monotonic=clock,
        shadow=shadow,
    )
    worker.run(max_periods=PERIODS)
    channel_.log = log
    return channel_


@pytest.mark.parametrize(
    "behaviour",
    ["answer", "slow", "raise", "raise_on_submit", "die", "unresponsive"],
)
def test_the_base_proposals_and_their_times_do_not_depend_on_the_shadow_solver(
    trained: Any, behaviour: str
) -> None:
    """遅れる（周期をまたぐ）・例外・死ぬ・応答しない子でも、基準は同じ内容・同じ時刻。

    0133 §2.5 の決定論の試験。比べる相手はヒントを扱わない worker。
    """
    plain = run_loop(trained, None, with_shadow=False)
    solver = FakeSolver(behaviour=behaviour, delay_services=3 if behaviour == "slow" else 0)
    hinted = run_loop(trained, solver, with_shadow=True)
    assert plain.sent and hinted.sent == plain.sent
    if behaviour in {"answer", "slow"}:
        assert solver.submitted, "依頼は渡った"
    if behaviour == "slow":
        reasons = [json.loads(data)["body"]["reason"] for _, data in hinted.shadow]
        assert "shadow_busy" in reasons, "途中の子への依頼は捨てて記録にする（溜めない）"


def test_the_base_is_sent_before_the_request_in_every_period(trained: Any) -> None:
    solver = FakeSolver()
    hinted = run_loop(trained, solver, with_shadow=True)
    events = hinted.log
    submits = [index for index, event in enumerate(events) if event[0] == "submit"]
    assert submits
    for index in submits:
        assert events[index - 1][0] == "base" and events[index - 1][1] == events[index][1]


# ================================================================ 4. 経路（本物のソケット）


def pair(*, reserve: int | None) -> tuple[socket.socket, WorkerChannel]:
    fand, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    channel_ = WorkerChannel(worker, max_message_bytes=1 << 20)
    if reserve is not None:
        assert channel_.reserve_shadow_send_buffer(reserve)
    return fand, channel_


def shadow_bytes(size: int) -> bytes:
    return b"x" * size


def test_the_reserved_buffer_adds_the_shadow_share() -> None:
    fand, plain = pair(reserve=None)
    base = plain._sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
    _, reserved = pair(reserve=40_000)
    assert reserved._sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) >= base + 40_000
    fand.close()


def test_a_shadow_send_goes_only_when_nothing_is_waiting_on_the_wire() -> None:
    fand, channel_ = pair(reserve=None)
    assert channel_.try_send_shadow(b"first") is ShadowSendOutcome.SENT
    # fand がまだ読んでいない: 2件目は送らない（経路に残る Shadow の記録は高々1件）
    assert channel_.try_send_shadow(b"second") is ShadowSendOutcome.QUEUE_NOT_EMPTY
    assert fand.recv(64) == b"first"
    assert channel_.try_send_shadow(b"third") is ShadowSendOutcome.SENT
    fand.close()


def test_a_shadow_send_does_not_wait_for_the_heartbeat_lock() -> None:
    fand, channel_ = pair(reserve=None)
    with channel_._send_lock:
        started = time.monotonic()
        result: list[ShadowSendOutcome] = []
        thread = threading.Thread(target=lambda: result.append(channel_.try_send_shadow(b"x")))
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert time.monotonic() - started < 1.0
    assert result == [ShadowSendOutcome.LOCK_BUSY]
    fand.close()


def test_a_shadow_send_is_refused_from_the_supervisor_role() -> None:
    fand, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    channel_ = WorkerChannel(worker, max_message_bytes=1 << 20, role=LearnedRole.SUPERVISOR)
    with pytest.raises(ValueError):
        channel_.try_send_shadow(b"x")
    fand.close()


def _base_capacity(
    channel_: WorkerChannel, *, shadow: bytes | None, base: bytes
) -> tuple[int, int]:
    """fand が読まない経路で、基準の送信が詰まるまでに送れた件数と、送れた Shadow の件数。

    基準と heartbeat の送信は timeout の無いブロッキングの ``send`` で、待ち始めるのは
    ``MSG_DONTWAIT`` の ``send`` が ``EAGAIN`` を返すのと同じ条件（送信バッファの残り）。そこで
    詰まり始める点を ``MSG_DONTWAIT`` で数える（timeout 付きの ``send`` は先に poll の閾値で待つので
    使わない）。毎周期、基準の前に Shadow の記録を送ってみる。
    """
    sent = shadows = 0
    while True:
        if shadow is not None and channel_.try_send_shadow(shadow) is ShadowSendOutcome.SENT:
            shadows += 1
        try:
            channel_._sock.send(base, socket.MSG_DONTWAIT)
        except BlockingIOError:
            return sent, shadows
        sent += 1
        assert sent < 100_000


@pytest.mark.parametrize("base_bytes", [128, 6_000], ids=["heartbeat", "proposal"])
def test_a_stuck_fand_blocks_the_base_no_earlier_than_without_the_shadow(base_bytes: int) -> None:
    """記録を毎周期出させても、基準は詰まり始めるまで同じ件数を送れる（0133 §2.5）。"""
    settings = LearnedChannelSettings.from_yaml(TESTS.parent / "config" / "learned-channel.yaml")
    limits = settings.limits
    assert limits.max_shadow_record_bytes is not None and limits.packet_overhead_bytes is not None
    record = shadow_bytes(limits.max_shadow_record_bytes.value)
    base = b"b" * base_bytes
    plain_fand, plain = pair(reserve=None)
    shadow_fand, with_shadow = pair(
        reserve=limits.max_shadow_record_bytes.value + limits.packet_overhead_bytes.value
    )
    plain_count, _ = _base_capacity(plain, shadow=None, base=base)
    shadow_count, shadows = _base_capacity(with_shadow, shadow=record, base=base)
    assert shadows == 1, "fand が読まないので、経路に残る Shadow の記録は1件だけ"
    assert shadow_count >= plain_count
    plain_fand.close()
    shadow_fand.close()


def test_a_record_over_the_message_limit_is_not_sent() -> None:
    fand, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    channel_ = WorkerChannel(worker, max_message_bytes=1_024)
    assert channel_.try_send_shadow(b"x" * 1_025) is ShadowSendOutcome.FAILED
    fand.setblocking(False)
    with pytest.raises(BlockingIOError):
        fand.recv(2_048)
    fand.close()


# ================================================================ 5. 実際の子プロセス


def child_args(world: World, **overrides: Any) -> ShadowChildArgs:
    values: dict[str, Any] = {
        "config_dir": world.config_dir,
        "metrics": METRICS,
        "registry_root": world.registry_root,
        "registry_limits": REGISTRY_LIMITS_DIR,
        "lockfile": None,
        "parent_isolated": False,
        "parent_pid": os.getpid(),
        "log_level": "WARNING",
    }
    values.update(overrides)
    return ShadowChildArgs(**values)


def drive(
    solver: ShadowSolverProcess, until: Callable[[], bool], timeout_s: float = SPAWN_WAIT_S
) -> list[SolverEvents]:
    clock = SystemMonotonicClock()
    events: list[SolverEvents] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events.append(solver.service(clock.monotonic_ms()))
        if until():
            return events
        time.sleep(0.01)
    raise AssertionError(f"Shadow solver の状態が変わらない: {solver.state}")


def test_a_child_that_cannot_lower_to_sched_idle_does_nothing_and_is_not_respawned(
    world: World,
) -> None:
    from shadow_child_hooks import never_lower

    solver = ShadowSolverProcess(
        child_args(world, lower_priority=never_lower),
        respawn_interval_ms=10,
        unresponsive_after_ms=10_000,
        max_reply_bytes=1 << 20,
        monotonic_ms=SystemMonotonicClock().monotonic_ms,
    )
    try:
        solver.start(SystemMonotonicClock().monotonic_ms())
        drive(solver, lambda: solver.state is SolverState.DISABLED)
        time.sleep(0.05)
        drive(solver, lambda: True)
        assert solver.state is SolverState.DISABLED, "そのプロセスの間は作り直さない"
        assert solver.readable_fds() == []
    finally:
        solver.close()


def test_a_real_child_runs_at_sched_idle_answers_and_is_respawned_after_dying(
    world: World,
) -> None:
    clock = SystemMonotonicClock()
    solver = ShadowSolverProcess(
        child_args(world),
        respawn_interval_ms=50,
        unresponsive_after_ms=10_000,
        max_reply_bytes=1 << 20,
        monotonic_ms=clock.monotonic_ms,
    )
    try:
        assert solver.state is SolverState.NOT_STARTED
        solver.service(clock.monotonic_ms())
        assert solver.state is SolverState.NOT_STARTED, "頼まれるまで子を作らない"
        solver.start(clock.monotonic_ms())
        drive(solver, lambda: solver.state is SolverState.IDLE)
        child = solver._child
        assert child is not None and child.process.pid is not None
        assert os.sched_getscheduler(child.process.pid) == os.SCHED_IDLE
        request = world.with_request(seq=7, deadline_mono_ms=clock.monotonic_ms() + 30_000)
        solver.submit(request)
        assert solver.state is SolverState.BUSY
        events = drive(solver, lambda: solver.state is SolverState.IDLE)
        results = [result for event in events for result in event.results]
        assert [result.seq for result in results] == [7]
        # 親が other（pytest は -I でない）なので environment を計算せず、照合は不一致
        assert results[0].reason is HintShadowSkipReason.CERTIFICATION_MISMATCH
        # 子が死んだら、結果を返さずに失われた依頼として数え、間隔をあけて作り直す。
        # 先に止めてから頼む。頼んでから殺すと、速い子が殺される前に答えを返し、
        # 依頼が失われない（CI でまれに落ちた）
        os.kill(child.process.pid, signal.SIGSTOP)
        solver.submit(world.with_request(seq=8, deadline_mono_ms=clock.monotonic_ms() + 30_000))
        os.kill(child.process.pid, signal.SIGKILL)
        events = drive(solver, lambda: solver.state is not SolverState.BUSY)
        assert (8,) in [event.lost for event in events]
        drive(solver, lambda: solver.state is SolverState.IDLE)
        assert solver._child is not None and solver._child.process.pid != child.process.pid
    finally:
        solver.close()


def test_an_unresponsive_child_is_ended_and_its_request_is_lost(world: World) -> None:
    clock = SystemMonotonicClock()
    solver = ShadowSolverProcess(
        child_args(world),
        respawn_interval_ms=60_000,
        unresponsive_after_ms=1,
        max_reply_bytes=1 << 20,
        monotonic_ms=clock.monotonic_ms,
    )
    try:
        solver.start(clock.monotonic_ms())
        drive(solver, lambda: solver.state is SolverState.IDLE)
        child = solver._child
        assert child is not None and child.process.pid is not None
        os.kill(child.process.pid, signal.SIGSTOP)  # 止まった子（応答しない）
        solver.submit(world.with_request(seq=3, deadline_mono_ms=clock.monotonic_ms()))
        events = drive(solver, lambda: solver.state is SolverState.WAITING_RESPAWN)
        assert (3,) in [event.lost for event in events]
    finally:
        solver.close()


PDEATHSIG_SCRIPT = """
import os, sys, time
from pathlib import Path
from coldaisle.clock import SystemMonotonicClock
from coldaisle.learned_worker.shadow_solver import (
    ShadowChildArgs, ShadowSolverProcess, SolverState,
)
clock = SystemMonotonicClock()
solver = ShadowSolverProcess(
    ShadowChildArgs(config_dir=Path({config!r}), metrics=Path({metrics!r}),
                    registry_root=Path({root!r}), registry_limits=Path({limits!r}),
                    lockfile=None, parent_isolated=False, parent_pid=os.getpid(),
                    log_level="WARNING"),
    respawn_interval_ms=10, unresponsive_after_ms=10_000, max_reply_bytes=1 << 20,
    monotonic_ms=clock.monotonic_ms)
solver.start(clock.monotonic_ms())
deadline = time.monotonic() + 60
while solver.state is not SolverState.IDLE and time.monotonic() < deadline:
    solver.service(clock.monotonic_ms())
    time.sleep(0.01)
print(solver._child.process.pid, flush=True)
os._exit(0)
"""


def test_the_child_ends_when_the_parent_dies(world: World, tmp_path: Path) -> None:
    """``PR_SET_PDEATHSIG``: 親が後始末をせずに終わっても、子は残らない（0133 §2.5）。"""
    script = PDEATHSIG_SCRIPT.format(
        config=str(world.config_dir),
        metrics=str(METRICS),
        root=str(world.registry_root),
        limits=str(REGISTRY_LIMITS_DIR),
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    pid = int(done.stdout.strip().splitlines()[-1])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    raise AssertionError("親の死後も子が残った")


# ================================================================ 6. fand（受け口が無ければ捨てる）


def hint_shadow_envelope(run_id: str = CHANNEL_RUN_ID) -> dict[str, Any]:
    body = HintShadowBody(
        kind="hint_shadow",
        record_schema_version=1,
        worker_instance_id="ab" * 16,
        counters={
            "shadow_dropped_backpressure": 0,
            "shadow_record_too_large": 0,
            "shadow_unavailable": 0,
        },  # type: ignore[arg-type]
        source_tick_id=10,
        source_ts_ms=1_800_000_000_000,
        source_monotonic_ms=10_000,
        base_result_digest="b" * 64,
        thermal_model={"model_id": "rack-thermal", "version": "0.1.0", "artifact_sha256": "a" * 64},  # type: ignore[arg-type]
        config=CONTROL.runtime_digest(),
        certificate_sha256="c" * 64,
        conditions_sha256=None,
        status=HintShadowStatus.SKIPPED,
        reason=HintShadowSkipReason.SHADOW_BUSY,
        plans=None,
    )
    return {
        "schema_version": 1,
        "run_id": run_id,
        "role": "mpc",
        "body": json.loads(body.model_dump_json()),
    }


def test_fand_drops_hint_shadow_until_pr_c_and_does_not_count_it_as_liveness(channel) -> None:
    running = channel(heartbeat_ms=50, idle_ms=200)
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            try:
                worker.send(hint_shadow_envelope())
            except (BrokenPipeError, ConnectionResetError):
                break  # 受付が idle として閉じた
            time.sleep(0.04)
        assert wait_until(lambda: running.server.dropped.get("mpc.unsupported_body", 0) >= 1)
        assert wait_until(
            lambda: running.mailbox.state(LearnedRole.MPC) is LearnedChannelState.WORKER_IDLE
        )
        assert running.mailbox.poll() is None
    finally:
        worker.close()


# ================================================================ 7. 本文の形と境界


def test_the_record_body_is_a_closed_shape() -> None:
    document = hint_shadow_envelope()
    parsed = parse_inbound(json.dumps(document).encode())
    assert isinstance(parsed.body, HintShadowBody)
    for change in (
        {"reason": None},  # skipped には理由が要る
        {"status": "recorded"},  # recorded には plans と conditions が要る
        {"status": "rejected"},  # rejected は fand だけが付ける
        {"reason": "hint_record_rejected"},  # 閉じた語彙の外
        {"requested_demand": {"front": 1.0, "rear": 1.0, "top": 1.0}},  # 上書きの欄は無い
        {"mode": "manual"},
        {"authority_stage": "full"},
    ):
        broken = json.loads(json.dumps(document))
        broken["body"].update(change)
        with pytest.raises(ValidationError):
            parse_inbound(json.dumps(broken).encode())


def test_the_counters_never_go_negative() -> None:
    document = hint_shadow_envelope()
    document["body"]["counters"]["shadow_unavailable"] = -1
    with pytest.raises(ValidationError):
        InboundEnvelope.model_validate_json(json.dumps(document))


def test_the_lockfile_is_for_the_mpc_worker_only(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exited:
        main(
            [
                "--role",
                "supervisor",
                "--registry-root",
                str(tmp_path),
                "--learned-channel-config",
                str(tmp_path / "x.yaml"),
                "--hint-lockfile",
                str(tmp_path / "uv.lock"),
            ]
        )
    assert exited.value.code == 2


def test_the_shadow_modules_do_not_reach_safety_guard_fallback_hardware_or_llm() -> None:
    """Shadow solver の経路から Reactive Guard / Critical Safety / hardware へ届かない。

    0133 §2.7 / 0162 §2.1。LLM 層・API・store・fand の照合も import しない。
    """
    import ast

    forbidden = (
        "coldaisle.control.safety",
        "coldaisle.control.reactive",
        "coldaisle.control.fallback",
        "coldaisle.control.hardware",
        "coldaisle.ai",
        "coldaisle.api",
        "coldaisle.server",
        "coldaisle.store",
        "coldaisle.control_daemon",
        "coldaisle.hint_certification",
        "coldaisle.hint_certify",
    )
    src = TESTS.parent / "src" / "coldaisle"
    for path in (
        src / "learned_worker" / "shadow.py",
        src / "learned_worker" / "shadow_solver.py",
        src / "control" / "hint_monotonicity.py",
    ):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                assert not any(name == item or name.startswith(f"{item}.") for item in forbidden), (
                    f"{path.name} imports {name}"
                )


def test_nothing_on_the_control_side_imports_the_shadow_solver() -> None:
    """fand・control・Safety は worker の Shadow solver を import しない（経路を増やさない）。"""
    import ast

    src = TESTS.parent / "src" / "coldaisle"
    paths = [*sorted((src / "control").rglob("*.py")), src / "control_daemon.py"]
    for path in paths:
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith("coldaisle.learned_worker"), path
