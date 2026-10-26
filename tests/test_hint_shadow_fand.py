"""fand の Workload Hint B1 の受信・検証・記録（#288 / 決定記録 0162 §2.2 / §2.3 / §2.5 の PR-C）。

実機なし（ルール 7）。固定すること:

1. **送り出しの時点の判定を保留し、記録が届いたらそれを使う。** 区間の長さを超えて後の frame を
   送った後に届いた記録も、保留の表の結果で判定される。保留が切れた・無い記録は
   ``source_interval_unavailable`` で範囲の外として ``recorded`` のまま残す（0162 §2.2）
2. **区間は fand が自分で送った frame で決まる。** worker が window に使わない古い frame の外れでも
   外になり（厳しい側）、送り出せなかった tick（``tick_id`` の飛び）があれば外
3. **記録の検証**: 新しさ（``mpc.max_source_age_ms``）・artifact・証明書と ``conditions`` の
   digest・同じ元の tick の2件目・対の基準。どれかに落ちた記録は ``rejected`` で、その記録だけを
   捨てる
4. **epoch ごとの数**: 同じ ``worker_instance_id`` の中で数が減った記録だけを ``counters_decreased``
   で捨て、epoch の最後の値を更新しない。新しい epoch は前の値と比べない（0162 §2.3）
5. **2 条件の再検査**は fand が設定から導いた ``eps`` / ``eps_c`` で行い、領域の外でも残す
6. **受付**: ``hint_shadow`` は MPC のソケットで受け口を渡されたときだけ受け取り、生存の知らせに
   数えない。受け口の失敗で受付スレッドを止めない。送り出した frame を受け口の列へ渡す
7. **組み立て**: fand は照合が通り ``hint_domain`` がある B1 の run だけ受け口を開いて受付に渡す。
   trace の塊 v2 は証明書の識別と最後に受け入れた数を持つ
8. **境界**: Safety / Guard / Fallback / hardware は新しい部品を import しない。worker からの本文は
   Demand・モード・authority を運べない
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from coldaisle.clock import SimulatedClock, SystemMonotonicClock
from coldaisle.control.hint_domain import DomainCheck
from coldaisle.control.hint_shadow import MonotonicityCheck, recheck_monotonicity
from coldaisle.control.learned_handoff import LearnedRole, PinnedArtifact
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.schema import Demand, HintShadowCounters, PerZone
from coldaisle.hint_certificate import HintGateSpec
from coldaisle.hint_shadow_record import HintShadowReason, HintShadowRecord, HintShadowStatus
from coldaisle.learned_channel import open_learned_channel
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.learned_channel.hint_shadow import (
    SOURCE_INTERVAL_UNAVAILABLE,
    HintAxes,
    HintDomainTracker,
    HintShadowExpectations,
    HintShadowIntake,
    HintShadowWriter,
    SentFrameAxes,
)
from coldaisle.learned_channel.messages import (
    HintShadowBody,
    HintShadowPlan,
    HintShadowPlans,
    HintShadowSkipReason,
    parse_inbound,
)
from coldaisle.learned_channel.messages import HintShadowCounters as WorkerCounters
from coldaisle.learned_channel.messages import HintShadowStatus as WorkerShadowStatus
from coldaisle.store import SqliteStore
from test_hint_certification import world as _world_fixture
from test_hint_domain import CPU_UPPER, GPU_UPPER, LOOKBACK_MS, STALE_MS, good_frame, ok, ts
from test_hint_domain import spec as _spec_fixture
from test_learned_channel import (
    RUN_ID,
    FakeGroups,
    FakePeer,
    Worker,
    channel_document,
    wait_until,
)
from test_learned_channel import catalog as _catalog_fixture
from test_learned_channel import no_file_groups as _no_file_groups_fixture
from test_learned_channel import sample_frame as _sample_frame_fixture
from test_learned_channel import short_dir as _short_dir_fixture
from test_learned_mpc import build_controller, propose
from test_learned_mpc import trained as _trained_fixture

# 他の試験 module の fixture を、この module でも同じ名前で使う
spec = _spec_fixture
catalog = _catalog_fixture
no_file_groups = _no_file_groups_fixture
sample_frame = _sample_frame_fixture
short_dir = _short_dir_fixture
trained = _trained_fixture
world = _world_fixture

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
MONO_BASE = 50_000
"""送り出しの単調時計の起点（tick ``t`` の frame は ``MONO_BASE + t * 1000`` で送る）。"""
PENDING_MS = 30_000
MAX_PENDING = 8
MAX_SOURCE_AGE_MS = 10_000
N = 10
CERTIFICATE = "c" * 64
CONDITIONS = "6" * 64
EPOCH_A = "a" * 32
EPOCH_B = "b" * 32
CPU_METRIC = "cpu.temp"
GPU_METRIC = "gpu.temp"


def mono(tick: int) -> int:
    return MONO_BASE + tick * 1_000


def hint_axes() -> HintAxes:
    return HintAxes(
        key=("transient_gpu", "training"),
        safety_floor=PerZone[Demand](front=0.2, rear=0.2, top=0.2),
        baseline=PerZone[Demand](front=0.4, rear=0.5, top=0.3),
        cpu_upper_c=CPU_UPPER,
        gpu_upper_c=GPU_UPPER,
    )


def sent(tick: int, *, hint: bool = False, domain: Any = None) -> SentFrameAxes:
    return SentFrameAxes(
        domain=good_frame(tick) if domain is None else domain,
        monotonic_ms=mono(tick),
        applied=PerZone[Demand | None](front=0.5, rear=0.5, top=0.5),
        hint=hint_axes() if hint else None,
    )


def make_tracker(spec: HintGateSpec) -> HintDomainTracker:
    return HintDomainTracker(
        spec=spec,
        cpu_temperature_metric=CPU_METRIC,
        gpu_temperature_metric=GPU_METRIC,
        lookback_ms=LOOKBACK_MS,
        stale_after_ms=STALE_MS,
        pending_ms=PENDING_MS,
        max_pending=MAX_PENDING,
        hint_keys={},
    )


def send_ticks(
    tracker: HintDomainTracker, ticks: range, *, hint_at: int | None = N, **replace: Any
) -> None:
    for tick in ticks:
        domain = replace.get(f"t{tick}")
        tracker.record_sent(
            sent(tick, hint=tick == hint_at, domain=domain), sent_mono_ms=mono(tick)
        )


# ---------------------------------------------------------------- 基準と記録の組み立て


@pytest.fixture(scope="module")
def base(trained: Any) -> MpcProposal:
    """本物の optimizer の解を持つ基準の ``MpcProposal``（元の tick を ``N`` に置き直す）。"""
    controller, _binding, _settings = build_controller(trained)
    result = propose(controller)
    assert result.proposal is not None and result.solution is not None
    proposal = result.proposal.model_copy(update={"seq": N, "computed_at_ms": ts(N)})
    return result.model_copy(update={"proposal": proposal})


@pytest.fixture(scope="module")
def metrics(trained: Any) -> tuple[str, str]:
    _controller, _binding, settings = build_controller(trained)
    names = settings.mpc.optimizer.cost_metrics
    return names.cpu_temperature, names.gpu_temperature


def pinned(base: MpcProposal) -> PinnedArtifact:
    assert base.solution is not None
    prediction = base.solution.prediction
    return PinnedArtifact(
        model_id=prediction.model_id,
        version=prediction.model_version,
        artifact_sha256=prediction.artifact_sha256,
    )


def counters(
    epoch: str = EPOCH_A, dropped: int = 2, too_large: int = 0, unavailable: int = 1
) -> HintShadowCounters:
    return HintShadowCounters(
        worker_instance_id=epoch,
        shadow_dropped_backpressure=dropped,
        shadow_record_too_large=too_large,
        shadow_unavailable=unavailable,
    )


def hint_plan(
    base: MpcProposal,
    metrics: tuple[str, str],
    *,
    demand: float = 0.0,
    cpu: float = -1.0,
    gpu: float = -1.0,
) -> HintShadowPlan:
    """基準の解から作るヒント有りの plan（``demand`` / ``cpu`` / ``gpu`` は基準からの差）。"""
    assert base.solution is not None
    solution = base.solution
    requested = PerZone[Demand](
        front=min(1.0, max(0.0, solution.requested.front + demand)),
        rear=solution.requested.rear,
        top=solution.requested.top,
    )
    cpu_metric, gpu_metric = metrics
    targets = tuple(
        target.model_copy(
            update={
                "values": {
                    **target.values,
                    cpu_metric: target.values[cpu_metric] + cpu,
                    gpu_metric: target.values[gpu_metric] + gpu,
                }
            }
        )
        for target in solution.prediction.targets
    )
    return HintShadowPlan(requested=requested, targets=targets)


def base_plan(base: MpcProposal) -> HintShadowPlan:
    assert base.solution is not None
    return HintShadowPlan(
        requested=base.solution.requested, targets=base.solution.prediction.targets
    )


def result(base: MpcProposal, metrics: tuple[str, str], **overrides: Any) -> HintShadowBody:
    """PR-B の本文 ``hint_shadow`` を組み立てる（``counters`` は trace の形で渡す）。"""
    assert base.solution is not None
    fields: dict[str, Any] = {
        "source_tick_id": N,
        "source_ts_ms": ts(N),
        "source_monotonic_ms": mono(N),
        "base_result_digest": base.result_digest(),
        "thermal_model": pinned(base),
        "config": _config_digest(),
        "certificate_sha256": CERTIFICATE,
        "conditions_sha256": CONDITIONS,
        "counters": counters(),
        "status": "recorded",
        "hint_plan": hint_plan(base, metrics),
        "base_plan": base_plan(base),
    }
    fields.update(overrides)
    trace_counters: HintShadowCounters = fields.pop("counters")
    status = WorkerShadowStatus(fields.pop("status"))
    reason = fields.pop("skip_reason", None)
    hint, based = fields.pop("hint_plan"), fields.pop("base_plan")
    if status is WorkerShadowStatus.SKIPPED:
        reason = reason or HintShadowSkipReason.BUDGET_EXHAUSTED
        plans = None
    else:
        plans = HintShadowPlans(base=based, hint=hint, hint_target_band=_band())
    return HintShadowBody(
        kind="hint_shadow",
        record_schema_version=1,
        worker_instance_id=trace_counters.worker_instance_id,
        counters=WorkerCounters(
            shadow_dropped_backpressure=trace_counters.shadow_dropped_backpressure,
            shadow_record_too_large=trace_counters.shadow_record_too_large,
            shadow_unavailable=trace_counters.shadow_unavailable,
        ),
        status=status,
        reason=reason,
        plans=plans,
        **fields,
    )


def _band() -> Any:
    from coldaisle.control.schema import SupervisorTargetBand, TemperatureTarget

    return SupervisorTargetBand(
        cpu_temperature=TemperatureTarget(lower_c=55.0, upper_c=65.0),
        gpu_temperature=TemperatureTarget(lower_c=57.0, upper_c=67.0),
    )


class Sink:
    def __init__(self) -> None:
        self.records: list[HintShadowRecord] = []

    def submit(self, record: HintShadowRecord) -> bool:
        self.records.append(record)
        return True


@dataclasses.dataclass
class Rig:
    tracker: HintDomainTracker
    intake: HintShadowIntake
    sink: Sink


def rig(
    spec: HintGateSpec,
    base: MpcProposal,
    metrics: tuple[str, str],
    *,
    eps: float = 0.02,
    eps_c: float = 0.5,
) -> Rig:
    tracker = HintDomainTracker(
        spec=spec,
        cpu_temperature_metric=CPU_METRIC,
        gpu_temperature_metric=GPU_METRIC,
        lookback_ms=LOOKBACK_MS,
        stale_after_ms=STALE_MS,
        pending_ms=PENDING_MS,
        max_pending=MAX_PENDING,
        hint_keys={},
    )
    sink = Sink()
    intake = HintShadowIntake(
        run_id=RUN_ID,
        tracker=tracker,
        expectations=HintShadowExpectations(
            thermal_model=pinned(base),
            certificate_sha256=CERTIFICATE,
            conditions_sha256=CONDITIONS,
            config=_config_digest(),
            max_source_age_ms=MAX_SOURCE_AGE_MS,
            eps=eps,
            eps_c=eps_c,
            cpu_temperature_metric=metrics[0],
            gpu_temperature_metric=metrics[1],
        ),
        wall=SimulatedClock(ts(N) + 500),
        sink=sink,
    )
    return Rig(tracker=tracker, intake=intake, sink=sink)


def _config_digest() -> Any:
    from coldaisle.control.schema import ControlConfigDigest

    return ControlConfigDigest(
        fan_hardware_sha256="1" * 64, safety_sha256="2" * 64, policy_sha256="3" * 64
    )


def receive(
    r: Rig, record: HintShadowBody, base: MpcProposal | None, *, at_tick: float = N + 0.5
) -> HintShadowRecord:
    return r.intake.receive(record, base=base, received_mono_ms=MONO_BASE + int(at_tick * 1_000))


def recheck_of(row: HintShadowRecord) -> dict[str, Any]:
    assert row.record_json is not None
    document: dict[str, Any] = json.loads(row.record_json)
    recheck: dict[str, Any] = document["recheck"]
    return recheck


# ---------------------------------------------------------------- 1. 送り出しの時点の判定と保留


def test_a_record_inside_the_domain_passes_both_rechecks(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    row = receive(r, result(base, metrics), base)
    assert row.status is HintShadowStatus.RECORDED and row.reason is None
    assert recheck_of(row) == {
        "domain": {"passed": True, "failed": None},
        "monotonicity": {"passed": True, "failed": None},
    }
    assert r.sink.records == [row]


def test_the_judgement_is_kept_from_the_send_even_after_the_interval_has_moved_on(
    spec, base, metrics
) -> None:
    """記録が届く前に区間の長さを超えて frame を送っても、送り出しの時点の結果で判定する。"""
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    send_ticks(r.tracker, range(N + 1, N + 3 * LOOKBACK_MS // 1_000), hint_at=None)
    row = receive(r, result(base, metrics), base, at_tick=N + 5)
    assert recheck_of(row)["domain"] == {"passed": True, "failed": None}


def test_a_record_after_the_pending_expiry_is_out_but_kept_as_recorded(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    late = N + PENDING_MS // 1_000 + 1
    record = result(base, metrics)
    # 新しさの検査（max_source_age_ms）には通るよう、受け取りの時刻だけを進める
    row = r.intake.receive(
        record.model_copy(update={"source_monotonic_ms": mono(late) - 1}),
        base=base,
        received_mono_ms=mono(late),
    )
    assert row.status is HintShadowStatus.RECORDED
    assert recheck_of(row)["domain"] == {"passed": False, "failed": SOURCE_INTERVAL_UNAVAILABLE}
    # 2 条件の再検査は行って残す
    assert recheck_of(row)["monotonicity"]["passed"] is True


def test_a_record_for_a_tick_that_was_never_sent_with_a_hint_is_out(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1), hint_at=None)
    row = receive(r, result(base, metrics), base)
    assert recheck_of(row)["domain"]["failed"] == SOURCE_INTERVAL_UNAVAILABLE


def test_the_pending_table_is_bounded_by_count(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    for tick in range(N + 1, N + 1 + MAX_PENDING):
        r.tracker.record_sent(sent(tick, hint=True), sent_mono_ms=mono(tick))
    row = receive(r, result(base, metrics), base, at_tick=N + MAX_PENDING + 1)
    assert recheck_of(row)["domain"]["failed"] == SOURCE_INTERVAL_UNAVAILABLE


def test_a_key_that_disagrees_with_the_sent_frame_is_out(spec, base, metrics) -> None:
    """記録の (tick, ts, monotonic) が保留の鍵と一致しなければ範囲の外。"""
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    row = receive(r, result(base, metrics, source_monotonic_ms=mono(N) + 1), base)
    assert recheck_of(row)["domain"]["failed"] == SOURCE_INTERVAL_UNAVAILABLE


# ---------------------------------------------------------------- 2. 区間は fand の frame で決まる


def test_an_old_frame_in_the_interval_out_of_range_makes_it_out(spec, base, metrics) -> None:
    """worker が window に使わない古い frame でも外になる（厳しい側）。"""
    r = rig(spec, base, metrics)
    oldest_in_interval = N - LOOKBACK_MS // 1_000
    hot = dataclasses.replace(
        good_frame(oldest_in_interval), cpu_temperature=ok(CPU_UPPER + 20.0, oldest_in_interval)
    )
    send_ticks(r.tracker, range(0, N + 1), **{f"t{oldest_in_interval}": hot})
    row = receive(r, result(base, metrics), base)
    assert recheck_of(row)["domain"] == {
        "passed": False,
        "failed": DomainCheck.TEMPERATURE_CPU.value,
    }


def test_a_frame_that_could_not_be_sent_makes_the_interval_out(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    for tick in range(0, N + 1):
        if tick == N - 2:
            continue  # 送り出せなかった tick
        r.tracker.record_sent(sent(tick, hint=tick == N), sent_mono_ms=mono(tick))
    row = receive(r, result(base, metrics), base)
    assert recheck_of(row)["domain"]["failed"] == DomainCheck.INTERVAL_GAP.value


def test_a_signal_older_than_the_fand_threshold_is_out_even_if_ok(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    stale = dataclasses.replace(
        good_frame(N - 1), cpu_temperature=ok(CPU_UPPER - 2.0, N - 1 - STALE_MS // 1_000)
    )
    send_ticks(r.tracker, range(0, N + 1), **{f"t{N - 1}": stale})
    row = receive(r, result(base, metrics), base)
    assert recheck_of(row)["domain"]["failed"] == DomainCheck.SIGNAL_STALE.value


def test_the_record_carries_no_domain_claim_for_fand_to_read() -> None:
    """fand の判定は worker の申告に依らない: 記録は判定の結果も window も運ばない。"""
    assert not {"domain", "window", "certified_domain", "eps", "eps_c", "judgement"} & set(
        HintShadowBody.model_fields
    )


# ---------------------------------------------------------------- 3. 記録の検証


def test_a_stale_record_is_rejected(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    row = r.intake.receive(
        result(base, metrics), base=base, received_mono_ms=mono(N) + MAX_SOURCE_AGE_MS + 1
    )
    assert (row.status, row.reason) == (HintShadowStatus.REJECTED, HintShadowReason.STALE)
    assert row.record_json is None


def test_another_artifact_is_rejected(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    other = pinned(base).model_copy(update={"artifact_sha256": "9" * 64})
    row = receive(r, result(base, metrics, thermal_model=other), base)
    assert row.reason is HintShadowReason.ARTIFACT_MISMATCH


@pytest.mark.parametrize("field", ["certificate_sha256", "conditions_sha256"])
def test_a_forged_certificate_or_conditions_digest_is_rejected(
    spec, base, metrics, field: str
) -> None:
    """0152 §2.6 / §2.8: 証明書・``conditions`` の digest を偽った記録を捨てる。"""
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    row = receive(r, result(base, metrics, **{field: "d" * 64}), base)
    assert (row.status, row.reason) == (
        HintShadowStatus.REJECTED,
        HintShadowReason.CERTIFICATION_MISMATCH,
    )


def test_the_second_record_for_the_same_source_is_rejected(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    assert receive(r, result(base, metrics), base).status is HintShadowStatus.RECORDED
    again = receive(r, result(base, metrics), base, at_tick=N + 0.6)
    assert (again.status, again.reason) == (
        HintShadowStatus.REJECTED,
        HintShadowReason.DUPLICATE_SOURCE,
    )


def test_a_record_without_its_paired_base_is_rejected(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    assert receive(r, result(base, metrics), None).reason is HintShadowReason.BASE_MISMATCH
    other = result(base, metrics, base_result_digest="0" * 64)
    assert receive(r, other, base).reason is HintShadowReason.BASE_MISMATCH
    assert base.proposal is not None
    moved = base.model_copy(update={"proposal": base.proposal.model_copy(update={"seq": N + 1})})
    shifted = result(base, metrics, base_result_digest=moved.result_digest())
    assert receive(r, shifted, moved).reason is HintShadowReason.BASE_MISMATCH


def test_a_hint_plan_over_another_horizon_is_rejected(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    plan = hint_plan(base, metrics)
    shorter = plan.model_copy(update={"targets": plan.targets[:1]})
    if len(plan.targets) == 1:
        pytest.skip("horizon が 1 step の解では offset を減らせない")
    assert receive(r, result(base, metrics, hint_plan=shorter), base).reason is (
        HintShadowReason.BASE_MISMATCH
    )


def test_a_skipped_record_keeps_the_worker_reason_and_no_plans(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    row = receive(
        r,
        result(
            base, metrics, status="skipped", skip_reason=HintShadowSkipReason.CERTIFICATION_MISMATCH
        ),
        base,
    )
    assert (row.status, row.reason, row.record_json) == (
        HintShadowStatus.SKIPPED,
        HintShadowReason.CERTIFICATION_MISMATCH,
        None,
    )


def test_every_worker_skip_reason_is_in_the_row_vocabulary() -> None:
    assert {reason.value for reason in HintShadowSkipReason} <= {
        reason.value for reason in HintShadowReason
    }


# ---------------------------------------------------------------- 4. epoch ごとの数


def test_counters_that_decrease_within_an_epoch_are_rejected_without_updating(
    spec, base, metrics
) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    first = receive(r, result(base, metrics, status="skipped"), base)
    assert first.status is HintShadowStatus.SKIPPED
    assert r.intake.latest_counters() == counters()

    decreased = result(base, metrics, status="skipped", counters=counters(dropped=1))
    row = receive(r, decreased, base, at_tick=N + 0.6)
    assert (row.status, row.reason) == (
        HintShadowStatus.REJECTED,
        HintShadowReason.COUNTERS_DECREASED,
    )
    # 行には運んだ数をそのまま残し、epoch の最後の値は更新しない
    assert row.shadow_dropped_backpressure == 1
    assert r.intake.latest_counters() == counters()


def test_a_new_epoch_is_not_compared_with_the_previous_one(spec, base, metrics) -> None:
    """worker の再起動（新しい ``worker_instance_id``）の数は 0 から始まる。"""
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    receive(r, result(base, metrics, status="skipped", counters=counters(dropped=5)), base)
    restarted = counters(EPOCH_B, dropped=0, too_large=0, unavailable=0)
    row = receive(r, result(base, metrics, counters=restarted), base, at_tick=N + 0.6)
    assert row.reason is not HintShadowReason.COUNTERS_DECREASED
    assert row.worker_instance_id == EPOCH_B
    assert r.intake.latest_counters() == restarted


def test_every_row_carries_the_epoch_and_the_counters_it_brought(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    row = receive(r, result(base, metrics, base_result_digest="0" * 64), base)
    assert row.status is HintShadowStatus.REJECTED
    assert (row.worker_instance_id, row.shadow_dropped_backpressure, row.shadow_unavailable) == (
        EPOCH_A,
        2,
        1,
    )


# ---------------------------------------------------------------- 5. 2 条件の再検査


@pytest.mark.parametrize(
    ("change", "failed"),
    [
        ({"demand": -0.1}, MonotonicityCheck.DEMAND),
        ({"cpu": 1.0}, MonotonicityCheck.CPU),
        ({"gpu": 1.0}, MonotonicityCheck.GPU),
    ],
)
def test_a_hint_plan_that_weakens_cooling_fails_the_recheck(
    spec, base, metrics, change: dict[str, float], failed: MonotonicityCheck
) -> None:
    r = rig(spec, base, metrics, eps=0.02, eps_c=0.5)
    send_ticks(r.tracker, range(0, N + 1))
    assert base.solution is not None
    if change.get("demand") and base.solution.requested.front < 0.1:
        pytest.skip("基準の front が小さく、demand を下げられない")
    row = receive(r, result(base, metrics, hint_plan=hint_plan(base, metrics, **change)), base)
    assert row.status is HintShadowStatus.RECORDED
    assert recheck_of(row)["monotonicity"] == {"passed": False, "failed": failed.value}


def test_the_recheck_uses_the_given_eps_not_the_record() -> None:
    from coldaisle.control.mpc.counterfactual import PlannedTarget

    base_requested = PerZone[Demand](front=0.5, rear=0.5, top=0.5)
    hint_requested = PerZone[Demand](front=0.49, rear=0.5, top=0.5)
    prediction = _prediction_with(PlannedTarget, cpu=60.0, gpu=60.0)
    hotter = _prediction_with(PlannedTarget, cpu=60.4, gpu=60.0)
    common = {"cpu_metric": "cpu.t", "gpu_metric": "gpu.t"}
    assert recheck_monotonicity(
        base_requested=base_requested,
        base_targets=prediction.targets,
        hint_requested=hint_requested,
        hint_targets=hotter.targets,
        eps=0.02,
        eps_c=0.5,
        **common,
    ).passed
    assert (
        recheck_monotonicity(
            base_requested=base_requested,
            base_targets=prediction.targets,
            hint_requested=hint_requested,
            hint_targets=hotter.targets,
            eps=0.005,
            eps_c=0.5,
            **common,
        ).failed
        is MonotonicityCheck.DEMAND
    )
    assert (
        recheck_monotonicity(
            base_requested=base_requested,
            base_targets=prediction.targets,
            hint_requested=base_requested,
            hint_targets=hotter.targets,
            eps=0.02,
            eps_c=0.3,
            **common,
        ).failed
        is MonotonicityCheck.CPU
    )


def _prediction_with(planned_target: Any, *, cpu: float, gpu: float) -> Any:
    from coldaisle.control.mpc.counterfactual import PlanPrediction

    return PlanPrediction.model_construct(
        targets=(
            planned_target.model_construct(
                offset_ms=5_000, expected_ts_ms=5_000, values={"cpu.t": cpu, "gpu.t": gpu}
            ),
        )
    )


def test_the_recheck_refuses_plans_over_different_offsets() -> None:
    from coldaisle.control.mpc.counterfactual import PlannedTarget, PlanPrediction

    one = _prediction_with(PlannedTarget, cpu=60.0, gpu=60.0)
    two = PlanPrediction.model_construct(
        targets=(*one.targets, one.targets[0].model_copy(update={"offset_ms": 10_000}))
    )
    demand = PerZone[Demand](front=0.5, rear=0.5, top=0.5)
    with pytest.raises(ValueError, match="比べられない"):
        recheck_monotonicity(
            base_requested=demand,
            base_targets=one.targets,
            hint_requested=demand,
            hint_targets=two.targets,
            cpu_metric="cpu.t",
            gpu_metric="gpu.t",
            eps=0.0,
            eps_c=0.0,
        )


def test_the_record_json_has_both_plans_and_fand_values_only(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    row = receive(r, result(base, metrics), base)
    assert row.record_json is not None
    document = json.loads(row.record_json)
    assert set(document) == {
        "base",
        "hint",
        "hint_band",
        "recheck",
        "thermal_model",
        "config",
        "certificate_sha256",
        "base_result_digest",
    }
    assert base.solution is not None
    # 基準の plan は受け渡し口の基準から写す（記録の申告ではない）
    assert document["base"]["requested_demand"] == base.solution.requested.model_dump(mode="json")
    assert len(document["base"]["prediction"]) == len(base.solution.prediction.targets)
    assert len(document["hint"]["prediction"]) == len(base.solution.prediction.targets)
    assert document["certificate_sha256"] == CERTIFICATE
    assert document["config"] == _config_digest().model_dump(mode="json")


# ---------------------------------------------------------------- 書き手


class FakeStore:
    def __init__(self, fail: int = 0) -> None:
        self.rows: list[HintShadowRecord] = []
        self.fail = fail
        self.closed = 0

    def record_hint_shadow(self, record: HintShadowRecord) -> HintShadowRecord:
        if self.fail:
            self.fail -= 1
            raise OSError("disk full")
        self.rows.append(record)
        return record

    def close(self) -> None:
        self.closed += 1


def test_the_writer_writes_on_its_own_thread_and_survives_a_failure(spec, base, metrics) -> None:
    store = FakeStore(fail=1)
    opened: list[str] = []

    def open_store() -> FakeStore:
        opened.append(threading.current_thread().name)
        return store

    writer = HintShadowWriter(open_store=open_store, max_queue=4)
    writer.start()
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    row = receive(r, result(base, metrics), base)
    assert writer.submit(row) and writer.submit(row)
    writer.stop(timeout_s=5.0)
    assert writer.failures == {"write_failed": 1}
    assert store.rows == [row]
    assert set(opened) == {"hint-shadow-writer"}


def test_a_full_writer_queue_drops_without_waiting() -> None:
    writer = HintShadowWriter(open_store=FakeStore, max_queue=1)  # 起動しない（読まれない）
    record = _row()
    assert writer.submit(record)
    assert not writer.submit(record)
    assert writer.failures == {"queue_full": 1}


def _row() -> HintShadowRecord:
    return HintShadowRecord(
        run_id=RUN_ID,
        source_tick_id=N,
        source_ts_ms=ts(N),
        source_monotonic_ms=mono(N),
        received_monotonic_ms=mono(N) + 1,
        received_ts_ms=ts(N) + 1,
        schema_version=1,
        status=HintShadowStatus.SKIPPED,
        reason=HintShadowReason.SHADOW_BUSY,
        worker_instance_id=EPOCH_A,
        shadow_dropped_backpressure=0,
        shadow_record_too_large=0,
        shadow_unavailable=0,
    )


def test_the_writer_writes_real_rows(tmp_path: Path, rules: Any) -> None:
    db = tmp_path / "coldaisle.db"
    writer = HintShadowWriter(
        open_store=lambda: SqliteStore(db, rules=rules, clock=SimulatedClock(0)), max_queue=4
    )
    writer.start()
    assert writer.submit(_row())
    writer.stop(timeout_s=5.0)
    with SqliteStore(db, rules=rules, clock=SimulatedClock(0)) as store:
        (match,) = store.hint_shadow_matches(0, ts(N) + 1)
    assert match.record.reason is HintShadowReason.SHADOW_BUSY
    assert match.record.worker_instance_id == EPOCH_A


# ---------------------------------------------------------------- 6. 受付


def _envelope(record: HintShadowBody, *, role: str = "mpc") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "role": role,
        "body": json.loads(record.model_dump_json()),
    }


class FakeIntake:
    def __init__(self, *, fail: bool = False) -> None:
        self.received: list[tuple[HintShadowBody, MpcProposal | None]] = []
        self.frames: list[int] = []
        self.fail = fail

    def receive(
        self, record: HintShadowBody, *, base: MpcProposal | None, received_mono_ms: int
    ) -> None:
        if self.fail:
            raise RuntimeError("壊れた受け口")
        self.received.append((record, base))

    def on_frame_sent(self, frame: Any, *, sent_mono_ms: int) -> None:
        self.frames.append(frame.snapshot.tick_id)

    def latest_counters(self) -> None:
        return None


def _open(short_dir: Path, *, idle_ms: int = 400, shadow_limit: int | None = 32_768) -> Any:
    document = channel_document(short_dir / "run", idle_ms=idle_ms)
    if shadow_limit is not None:
        document["limits"]["max_shadow_record_bytes"] = {
            "value": shadow_limit,
            "status": "provisional",
            "basis": "test",
        }
        document["limits"]["packet_overhead_bytes"] = {
            "value": 4_096,
            "status": "provisional",
            "basis": "test",
        }
    config = short_dir / "learned-channel.yaml"
    config.write_text(yaml.safe_dump(document), "utf-8")
    entry = open_learned_channel(
        config,
        run_id=RUN_ID,
        tick_deadline_ms=500,
        monotonic=SystemMonotonicClock(),
        server_uid=os.geteuid(),
        groups=FakeGroups(),
        peer=FakePeer(),
    )
    assert entry is not None
    return entry


@pytest.fixture
def skipped_record(base, metrics) -> HintShadowBody:
    return result(base, metrics, status="skipped", skip_reason=HintShadowSkipReason.SHADOW_BUSY)


def test_the_body_parses_as_a_hint_shadow_envelope(skipped_record: HintShadowBody) -> None:
    envelope = parse_inbound(json.dumps(_envelope(skipped_record)).encode())
    assert envelope.body.kind == "hint_shadow"


def test_without_an_intake_the_record_is_dropped_as_unsupported(
    short_dir: Path, no_file_groups: None, skipped_record: HintShadowBody
) -> None:
    entry = _open(short_dir)
    try:
        worker = Worker(entry.server.socket_path(LearnedRole.MPC))
        assert worker.receive() is not None
        worker.send(_envelope(skipped_record))
        assert wait_until(lambda: entry.server.dropped.get("mpc.unsupported_body") == 1)
        worker.close()
    finally:
        entry.stop()


def test_the_record_reaches_the_intake_with_the_current_base(
    short_dir: Path, no_file_groups: None, skipped_record: HintShadowBody
) -> None:
    entry = _open(short_dir)
    intake = FakeIntake()
    entry.server.attach_hint_shadow(intake)
    try:
        worker = Worker(entry.server.socket_path(LearnedRole.MPC))
        assert worker.receive() is not None
        worker.send(_envelope(skipped_record))
        assert wait_until(lambda: len(intake.received) == 1)
        assert intake.received[0] == (skipped_record, None)
        # 受け渡し口には何も置かない（B1 は shadow だけ。0162 §2.1）
        assert entry.mailbox.poll() is None
        worker.close()
    finally:
        entry.stop()


def test_the_record_is_not_a_liveness_signal(
    short_dir: Path, no_file_groups: None, skipped_record: HintShadowBody
) -> None:
    """Shadow の記録だけを送り続ける worker は ``worker_idle`` で閉じられる（0162 §2.3）。"""
    from coldaisle.control.learned_handoff import LearnedChannelState

    entry = _open(short_dir, idle_ms=400)
    intake = FakeIntake()
    entry.server.attach_hint_shadow(intake)
    try:
        worker = Worker(entry.server.socket_path(LearnedRole.MPC))
        assert worker.receive() is not None
        deadline = time.monotonic() + 1.5
        closed = False
        while time.monotonic() < deadline:
            try:
                worker.send(_envelope(skipped_record))
            except OSError:
                closed = True
                break
            if entry.mailbox.state(LearnedRole.MPC) is LearnedChannelState.WORKER_IDLE:
                closed = True
                break
            time.sleep(0.05)
        assert closed
        assert intake.received
        worker.close()
    finally:
        entry.stop()


def test_a_record_on_the_supervisor_socket_is_unsupported(
    short_dir: Path, no_file_groups: None, skipped_record: HintShadowBody
) -> None:
    from test_learned_channel import RL_UID

    entry = _open(short_dir)
    intake = FakeIntake()
    entry.server.attach_hint_shadow(intake)
    peer = FakePeer()
    peer.uid = RL_UID
    try:
        entry.server._peer = peer
        worker = Worker(entry.server.socket_path(LearnedRole.SUPERVISOR))
        assert worker.receive() is not None
        worker.send(_envelope(skipped_record, role="supervisor"))
        assert wait_until(lambda: entry.server.dropped.get("supervisor.unsupported_body") == 1)
        assert intake.received == []
        worker.close()
    finally:
        entry.stop()


def test_a_record_over_the_shadow_limit_is_dropped(
    short_dir: Path, no_file_groups: None, skipped_record: HintShadowBody
) -> None:
    entry = _open(short_dir, shadow_limit=256)
    intake = FakeIntake()
    entry.server.attach_hint_shadow(intake)
    try:
        worker = Worker(entry.server.socket_path(LearnedRole.MPC))
        assert worker.receive() is not None
        worker.send(_envelope(skipped_record))
        assert wait_until(lambda: entry.server.dropped.get("mpc.shadow_record_too_large") == 1)
        assert intake.received == []
        worker.close()
    finally:
        entry.stop()


def test_a_failing_intake_does_not_stop_the_receiver(
    short_dir: Path, no_file_groups: None, skipped_record: HintShadowBody
) -> None:
    entry = _open(short_dir)
    entry.server.attach_hint_shadow(FakeIntake(fail=True))
    try:
        worker = Worker(entry.server.socket_path(LearnedRole.MPC))
        assert worker.receive() is not None
        worker.send(_envelope(skipped_record))
        assert wait_until(lambda: entry.server.dropped.get("mpc.hint_shadow_failed") == 1)
        assert entry.server.thread.is_alive()
        worker.send(
            {"schema_version": 1, "run_id": RUN_ID, "role": "mpc", "body": {"kind": "heartbeat"}}
        )
        worker.close()
    finally:
        entry.stop()


def test_frames_sent_to_the_mpc_worker_reach_the_intake(
    short_dir: Path, no_file_groups: None, sample_frame: Any
) -> None:
    entry = _open(short_dir)
    intake = FakeIntake()
    entry.server.attach_hint_shadow(intake)
    try:
        worker = Worker(entry.server.socket_path(LearnedRole.MPC))
        assert worker.receive() is not None
        assert wait_until(lambda: entry.mailbox.state(LearnedRole.MPC).value == "connected")
        entry.mailbox.offer(sample_frame)
        assert worker.receive() is not None
        assert wait_until(lambda: intake.frames == [sample_frame.snapshot.tick_id])
        worker.close()
    finally:
        entry.stop()


def test_the_intake_can_be_attached_only_once(short_dir: Path, no_file_groups: None) -> None:
    entry = _open(short_dir)
    try:
        entry.server.attach_hint_shadow(FakeIntake())
        with pytest.raises(RuntimeError):
            entry.server.attach_hint_shadow(FakeIntake())
    finally:
        entry.stop()


# ---------------------------------------------------------------- 設定


def test_the_channel_config_carries_the_b1_values() -> None:
    settings = LearnedChannelSettings.from_yaml(CONFIG_DIR / "learned-channel.yaml")
    domain = settings.hint_domain
    assert domain is not None
    assert domain.pending_domain_result_ms.value > 0
    assert domain.max_pending_domain_results.value > 0
    limit = settings.limits.max_shadow_record_bytes
    assert limit is not None and limit.value <= settings.limits.max_message_bytes.value


def test_the_hint_domain_requires_the_shadow_record_limit() -> None:
    document = yaml.safe_load((CONFIG_DIR / "learned-channel.yaml").read_text(encoding="utf-8"))
    limits = dict(document["limits"])
    document["limits"].pop("max_shadow_record_bytes")
    document["limits"].pop("packet_overhead_bytes")
    document.pop("hint_shadow")
    with pytest.raises(ValueError, match="hint_domain を置くなら"):
        LearnedChannelSettings.model_validate(document)
    document["limits"] = limits
    document["limits"]["max_shadow_record_bytes"] = {
        "value": document["limits"]["max_message_bytes"]["value"] + 1,
        "status": "provisional",
        "basis": "test",
    }
    with pytest.raises(ValueError, match="max_message_bytes 以下"):
        LearnedChannelSettings.model_validate(document)


# ---------------------------------------------------------------- 7. 境界


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


NEW_PARTS = (
    "coldaisle.control.hint_shadow",
    "coldaisle.learned_channel.hint_shadow",
    "coldaisle.hint_shadow_record",
    "coldaisle.control.schema.HintShadowCounters",
    "coldaisle.control.schema.HintCertificateRef",
)


@pytest.mark.parametrize("package", ["safety", "reactive", "fallback", "hardware"])
def test_safety_guard_fallback_and_hardware_do_not_import_the_shadow_parts(package: str) -> None:
    offenders = [
        f"{path.name}: {name}"
        for path in sorted((SRC / "control" / package).rglob("*.py"))
        for name in _imports(path)
        if name.startswith(NEW_PARTS)
    ]
    assert offenders == []


@pytest.mark.parametrize("package", ["ai", "api", "event_entry", "control_admin", "slackbot"])
def test_the_llm_and_the_read_api_do_not_reach_the_shadow_records(package: str) -> None:
    offenders = [
        f"{path.name}: {name}"
        for path in sorted((SRC / package).rglob("*.py"))
        for name in _imports(path)
        if name.startswith(NEW_PARTS)
    ]
    assert offenders == []


def test_the_control_package_does_not_import_the_channel_side() -> None:
    offenders = [
        f"{path.name}: {name}"
        for path in sorted((SRC / "control").rglob("*.py"))
        for name in _imports(path)
        if name.startswith(("coldaisle.learned_channel", "coldaisle.hint_shadow_record"))
    ]
    assert offenders == []


def test_the_worker_body_cannot_carry_a_demand_override_or_authority() -> None:
    names = set(HintShadowBody.model_fields)
    assert not {"demand", "effective_demand", "mode", "authority", "authority_stage", "pwm"} & names


# ---------------------------------------------------------------- trace の塊 v2（ControlTick v17）


CERTIFICATE_REF_FIELDS = {
    "model_id": "hint-cert-example",
    "version": "1",
    "artifact_sha256": "e" * 64,
    "registry_revision": 5,
    "certificate_sha256": CERTIFICATE,
}


class CountersBox:
    def __init__(self, value: HintShadowCounters | None = None, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail

    def latest_counters(self) -> HintShadowCounters | None:
        if self.fail:
            raise RuntimeError("壊れた口")
        return self.value


def _tracker(**kwargs: Any) -> Any:
    from coldaisle.clock import ManualMonotonicClock
    from coldaisle.control.supervisor.workload_hint import WorkloadHintTracker
    from test_workload_hint_tracker import TEST_EPOCH_MS, FakeMailbox, hint_config

    return WorkloadHintTracker(
        hint_config("observe"),
        mailbox=FakeMailbox(),
        boot_clock=SimulatedClock(TEST_EPOCH_MS),
        monotonic=ManualMonotonicClock(1_000),
        **kwargs,
    )


def _record(tracker: Any, tick: int) -> Any:
    step = tracker.step(tick_id=tick, ts_ms=tick, regime=None)
    return tracker.record(step, supervisor_available=True, would_override=False)


def test_the_block_carries_the_certificate_and_the_latest_counters() -> None:
    from coldaisle.control.schema import HintCertificateRef

    ref = HintCertificateRef(**CERTIFICATE_REF_FIELDS)
    box = CountersBox()
    tracker = _tracker(certified=True, certificate=ref, shadow_counters=box)
    first = _record(tracker, 0)
    assert first.schema_version == 2
    assert (first.certificate, first.shadow_counters) == (ref, None)
    box.value = counters()
    second = _record(tracker, 1)
    assert second.shadow_counters == counters()
    dumped = json.loads(second.model_dump_json())
    assert dumped["certificate"]["certificate_sha256"] == CERTIFICATE
    assert dumped["shadow_counters"]["worker_instance_id"] == EPOCH_A


def test_a_broken_counters_source_leaves_the_counters_null() -> None:
    tracker = _tracker(shadow_counters=CountersBox(fail=True))
    assert _record(tracker, 0).shadow_counters is None


def test_a_certificate_is_only_given_to_a_certified_tracker() -> None:
    from coldaisle.control.schema import HintCertificateRef

    with pytest.raises(ValueError, match="certificate"):
        _tracker(certificate=HintCertificateRef(**CERTIFICATE_REF_FIELDS))


def test_v2_always_writes_the_fields_and_v1_never_does() -> None:
    from coldaisle.control.schema import WorkloadHintRecord, WorkloadHintStage

    v2 = json.loads(WorkloadHintRecord(stage=WorkloadHintStage.OBSERVE).model_dump_json())
    assert v2["certificate"] is None and v2["shadow_counters"] is None
    v1 = json.loads(
        WorkloadHintRecord(stage=WorkloadHintStage.OBSERVE, schema_version=1).model_dump_json()
    )
    assert "certificate" not in v1 and "shadow_counters" not in v1
    with pytest.raises(ValueError, match="schema version 2"):
        WorkloadHintRecord(
            stage=WorkloadHintStage.OBSERVE, schema_version=1, shadow_counters=counters()
        )
    with pytest.raises(ValueError, match="stage: off"):
        WorkloadHintRecord(stage=WorkloadHintStage.OFF, shadow_counters=counters())


def test_the_tick_version_decides_the_block_version() -> None:
    from coldaisle.control.schema import ControlTick

    fixtures = Path(__file__).resolve().parent / "fixtures"
    v17 = json.loads((fixtures / "control_tick_v17.json").read_text(encoding="utf-8"))
    assert ControlTick.model_validate_json(json.dumps(v17)).workload_hint is not None
    v1_block = {"schema_version": 1, "stage": "off", "influence": "none"}
    with pytest.raises(ValueError, match="v2 にする"):
        ControlTick.model_validate_json(json.dumps(dict(v17, workload_hint=v1_block)))
    v16 = json.loads((fixtures / "control_tick_v16.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="v1 にする"):
        ControlTick.model_validate_json(json.dumps(dict(v16, workload_hint=v17["workload_hint"])))
    # 保存済みの v16 の塊は v1 のまま読め、v2 の欄を持たずに書き戻る
    reread = json.loads(ControlTick.model_validate_json(json.dumps(v16)).model_dump_json())
    assert reread["workload_hint"]["schema_version"] == 1
    assert not {"certificate", "shadow_counters"} & set(reread["workload_hint"])


# ---------------------------------------------------------------- PR-B の本文の形に固有の検証


def test_a_base_plan_copy_that_is_not_the_slot_base_is_rejected(spec, base, metrics) -> None:
    """記録が写した基準の plan は、受け渡し口の基準の解そのものでなければならない。"""
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    forged = hint_plan(base, metrics, cpu=-5.0, gpu=-5.0)
    row = receive(r, result(base, metrics, base_plan=forged), base)
    assert (row.status, row.reason) == (HintShadowStatus.REJECTED, HintShadowReason.BASE_MISMATCH)


def test_a_record_under_another_config_is_rejected(spec, base, metrics) -> None:
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    other = _config_digest().model_copy(update={"policy_sha256": "4" * 64})
    row = receive(r, result(base, metrics, config=other), base)
    assert row.reason is HintShadowReason.BASE_MISMATCH


def test_a_skipped_record_without_conditions_is_kept(spec, base, metrics) -> None:
    """子が束縛を作る前に止まった ``skipped`` の記録は ``conditions`` を持たない（PR-B の形）。"""
    r = rig(spec, base, metrics)
    send_ticks(r.tracker, range(0, N + 1))
    record = result(
        base,
        metrics,
        status="skipped",
        skip_reason=HintShadowSkipReason.CERTIFICATION_MISMATCH,
        conditions_sha256=None,
    )
    row = receive(r, record, base)
    assert (row.status, row.reason) == (
        HintShadowStatus.SKIPPED,
        HintShadowReason.CERTIFICATION_MISMATCH,
    )


# ---------------------------------------------------------------- fand の組み立て（B1 の受け口）


def _open_with_hint_domain(short_dir: Path) -> Any:
    document = channel_document(short_dir / "run")
    provisional = {"status": "provisional", "basis": "test"}
    document["limits"]["max_shadow_record_bytes"] = {"value": 32_768, **provisional}
    document["limits"]["packet_overhead_bytes"] = {"value": 4_096, **provisional}
    document["hint_domain"] = {
        "domain_lookback_ms": {"value": LOOKBACK_MS, **provisional},
        "domain_stale_after_ms": {"value": STALE_MS, **provisional},
        "pending_domain_result_ms": {"value": PENDING_MS, **provisional},
        "max_pending_domain_results": {"value": MAX_PENDING, **provisional},
    }
    config = short_dir / "learned-channel.yaml"
    config.write_text(yaml.safe_dump(document), "utf-8")
    entry = open_learned_channel(
        config,
        run_id=RUN_ID,
        tick_deadline_ms=500,
        monotonic=SystemMonotonicClock(),
        server_uid=os.geteuid(),
        groups=FakeGroups(),
        peer=FakePeer(),
    )
    assert entry is not None
    return entry


def test_fand_attaches_the_intake_only_for_a_certified_b1_run(
    short_dir: Path, no_file_groups: None, tmp_path: Path, rules: Any, world: Any
) -> None:
    """照合が通り ``hint_domain`` があれば、fand は受け口と書き手を開いて受付に渡す。"""
    from types import SimpleNamespace

    from coldaisle.control.config import ControlConfig
    from coldaisle.control.supervisor.workload_hint import HintCertificationLatch
    from coldaisle.control_daemon import _learned_hint_certification, _open_hint_shadow
    from coldaisle.learned_channel.registry_watch import read_startup_registry
    from test_hint_certification import run_verify
    from test_hint_certify import REGISTRY_LIMITS_DIR

    control = ControlConfig.from_directory(world.config_dir)
    startup = read_startup_registry(
        world.root, limits_dir=REGISTRY_LIMITS_DIR, hint_latch=HintCertificationLatch()
    )
    certified = run_verify(world)
    assert certified.certified
    entry = _open_with_hint_domain(short_dir)
    try:
        learned_hint = _learned_hint_certification(entry, certified)
        assert learned_hint is not None
        common: dict[str, Any] = {
            "learned": entry,
            "registry_binding": startup.binding,
            "rules": rules,
            "wall": SimulatedClock(0),
            "run_id": RUN_ID,
        }
        config = SimpleNamespace(db=tmp_path / "coldaisle.db")
        # 照合が通らない run は開かない（受付は hint_shadow を unsupported_body で捨てる）
        failed = run_verify(world, lockfile=None)
        assert (
            _open_hint_shadow(
                config,  # type: ignore[arg-type]
                control,
                certification=failed,
                learned_hint_certification=_learned_hint_certification(entry, failed),
                **common,
            )
            is None
        )
        opened = _open_hint_shadow(
            config,  # type: ignore[arg-type]
            control,
            certification=certified,
            learned_hint_certification=learned_hint,
            **common,
        )
        assert opened is not None
        intake, writer = opened
        assert entry.server._hint_shadow is intake
        assert intake.latest_counters() is None
        writer.stop(timeout_s=5.0)
    finally:
        entry.stop()


def test_a_sent_frame_is_reduced_to_its_axes_and_the_hint_key(
    spec: HintGateSpec, sample_frame: Any
) -> None:
    """frame はヒントの workload を運ばないので、band から設定の対応表で鍵を引き直す。"""
    from coldaisle.control.schema import WorkloadHintKind
    from hint_frame_world import with_hint

    frame = with_hint(sample_frame)
    hint = frame.supervisor_hint
    assert hint is not None and frame.supervisor is not None
    tracker = HintDomainTracker(
        spec=spec,
        cpu_temperature_metric=CPU_METRIC,
        gpu_temperature_metric=GPU_METRIC,
        lookback_ms=LOOKBACK_MS,
        stale_after_ms=STALE_MS,
        pending_ms=PENDING_MS,
        max_pending=MAX_PENDING,
        hint_keys={(hint.regime, hint.target_band): (WorkloadHintKind.TRAINING,)},
    )
    axes = tracker.axes(frame)
    assert axes.key == (
        frame.snapshot.tick_id,
        frame.snapshot.ts_ms,
        frame.snapshot.monotonic_ms,
    )
    assert axes.applied == frame.applied
    assert axes.hint is not None
    assert axes.hint.key == (hint.regime.value, "training")
    # 区間の全 frame は**基準の** upper_c で測る（0156 §2.4）
    assert axes.hint.cpu_upper_c == frame.supervisor.target_band.cpu_temperature.upper_c
    assert axes.hint.safety_floor == frame.safety_floor
    # 設定の対応表に無い band の鍵は引けない（判定 1 で外）
    other = HintDomainTracker(
        spec=spec,
        cpu_temperature_metric=CPU_METRIC,
        gpu_temperature_metric=GPU_METRIC,
        lookback_ms=LOOKBACK_MS,
        stale_after_ms=STALE_MS,
        pending_ms=PENDING_MS,
        max_pending=MAX_PENDING,
        hint_keys={},
    )
    reduced = other.axes(frame)
    assert reduced.hint is not None and reduced.hint.key is None
    assert other.axes(sample_frame).hint is None
