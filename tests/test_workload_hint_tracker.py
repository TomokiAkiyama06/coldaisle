"""Workload Hint Stage B0 の loop 側の状態の遷移と trace の塊。

#319 / 決定記録 0133 / 0064 / 0066。

実機なし。受け渡し口は偽物、`CLOCK_BOOTTIME` は `SimulatedClock`、単調時計は
`ManualMonotonicClock`。

守りたいこと:

1. 期限は行の ``boottime_ns`` を起点に ``CLOCK_BOOTTIME`` で数える。切れたら ``expired`` を
   読み手へ返す
2. 矛盾（計算負荷のヒントで Regime が ``IDLE`` / ``COOLDOWN`` のまま保持時間以上）で
   ``contradicted``。再び ``active`` にしない
3. 読み手が死んだ・値が古い・照会に失敗した・run を始められなかった tick はヒントを使わず、
   ヒントの欄を持たない記録にする（MAX にしない）
4. B1 / B2 を設定しても実効は B0 に留まり、``certification_mismatch`` を残す
5. ``would_override`` は active のヒントがある tick だけ
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.clock import ManualMonotonicClock, SimulatedClock
from coldaisle.control.config import WorkloadHintConfig
from coldaisle.control.schema import (
    ControlTick,
    HintPriorSkipped,
    WorkloadHintInfluence,
    WorkloadHintKind,
    WorkloadHintRecord,
    WorkloadHintStage,
    WorkloadHintStatus,
    WorkloadRegime,
)
from coldaisle.control.supervisor.workload_hint import (
    HintFeed,
    HintFeedState,
    HintObservation,
    HintTransition,
    WorkloadHintTracker,
    declared_ms,
    effective_stage,
    source_label,
)
from conftest import TEST_EPOCH_MS

HOLD_MS = 60_000
MAX_AGE_MS = 3_600_000
STALE_MS = 5_000


def hint_config(stage: str = "observe", **overrides: object) -> WorkloadHintConfig:
    document: dict[str, object] = {
        "stage": stage,
        "grace_ms": 60_000,
        "default_ttl_ms": 600_000,
        "max_age_ms": MAX_AGE_MS,
        "contradiction_hold_ms": HOLD_MS,
        "startup_backfill_ms": 300_000,
        "reader_poll_ms": 1_000,
        "reader_stale_after_ms": STALE_MS,
        "known_sources": ["job-launcher"],
        "context_overrides": [],
    }
    document.update(overrides)
    return WorkloadHintConfig.model_validate(document)


@dataclass
class FakeMailbox:
    feed: HintFeed | None = None
    alive: bool = True
    reports: list[HintTransition] = field(default_factory=list)
    error: Exception | None = None

    def reader_alive(self) -> bool:
        return self.alive

    def latest(self) -> HintFeed | None:
        if self.error is not None:
            raise self.error
        return self.feed

    def report(self, transition: HintTransition) -> None:
        self.reports.append(transition)


@dataclass
class Rig:
    tracker: WorkloadHintTracker
    mailbox: FakeMailbox
    boot: SimulatedClock
    monotonic: ManualMonotonicClock

    def advance(self, ms: int) -> None:
        self.boot.advance_to_ms(self.boot.now_ms() + ms)
        self.monotonic.advance_ms(ms)

    def publish(self, current: HintObservation | None, state: HintFeedState = HintFeedState.OK):
        self.mailbox.feed = HintFeed(
            state=state, read_monotonic_ms=self.monotonic.monotonic_ms(), current=current
        )

    def step(self, regime: WorkloadRegime | None = WorkloadRegime.SUSTAINED_GPU, tick_id: int = 0):
        return self.tracker.step(tick_id=tick_id, ts_ms=self.boot.now_ms(), regime=regime)


def rig(config: WorkloadHintConfig | None = None) -> Rig:
    config = config or hint_config()
    mailbox = FakeMailbox()
    boot = SimulatedClock(TEST_EPOCH_MS)
    monotonic = ManualMonotonicClock(1_000)
    tracker = WorkloadHintTracker(config, mailbox=mailbox, boot_clock=boot, monotonic=monotonic)
    return Rig(tracker=tracker, mailbox=mailbox, boot=boot, monotonic=monotonic)


def active(
    r: Rig, *, event_id: int = 7, declared: int = 600_000, boottime_ns: int | None = None
) -> HintObservation:
    return HintObservation(
        event_id=event_id,
        status=WorkloadHintStatus.ACTIVE,
        workload=WorkloadHintKind.TRAINING,
        source_label="job-launcher",
        declared_ms=declared,
        ts_ms=r.boot.now_ms(),
        boot_id=r.boot.boot_id(),
        boottime_ns=r.boot.boottime_ns() if boottime_ns is None else boottime_ns,
    )


# ---------------------------------------------------------------- 段階


def test_off_does_not_read_and_records_only_the_stage():
    config = hint_config("off")
    tracker = WorkloadHintTracker(
        config, mailbox=None, boot_clock=SimulatedClock(0), monotonic=ManualMonotonicClock()
    )
    tick = tracker.step(tick_id=0, ts_ms=0, regime=WorkloadRegime.IDLE)
    assert tick.hint is None
    assert tracker.record(tick, supervisor_available=True, would_override=True) == (
        WorkloadHintRecord.off()
    )


def test_off_must_not_be_wired_to_a_reader():
    with pytest.raises(ValueError, match="off 以外"):
        WorkloadHintTracker(
            hint_config("off"),
            mailbox=FakeMailbox(),
            boot_clock=SimulatedClock(0),
            monotonic=ManualMonotonicClock(),
        )


@pytest.mark.parametrize("configured", ["shadow_mpc", "active"])
def test_b1_and_b2_stay_at_b0_and_say_certification_mismatch(configured):
    """認証のハッシュを照合する仕組みが無いので実効は B0（0133 §2.6）。"""
    assert effective_stage(WorkloadHintStage(configured)) is WorkloadHintStage.OBSERVE
    r = rig(hint_config(configured))
    r.publish(active(r))
    tick = r.step()
    record = r.tracker.record(tick, supervisor_available=True, would_override=True)
    assert record.stage is WorkloadHintStage.OBSERVE
    assert record.influence is WorkloadHintInfluence.WOULD_OVERRIDE
    assert record.skipped is HintPriorSkipped.CERTIFICATION_MISMATCH


# ---------------------------------------------------------------- active と期限


def test_active_hint_reaches_the_supervisor_input_with_age_from_the_row():
    r = rig()
    observation = active(r)
    r.advance(10_000)
    r.publish(observation)
    tick = r.step()
    assert tick.hint is not None
    assert tick.hint.event_id == 7
    assert tick.hint.age_ms == 10_000
    assert tick.hint.remaining_ms == 590_000
    record = r.tracker.record(tick, supervisor_available=True, would_override=False)
    assert record == WorkloadHintRecord(
        stage=WorkloadHintStage.OBSERVE,
        event_id=7,
        workload=WorkloadHintKind.TRAINING,
        status=WorkloadHintStatus.ACTIVE,
        source_label="job-launcher",
        age_ms=10_000,
        influence=WorkloadHintInfluence.NONE,
    )


def test_would_override_is_recorded_only_when_the_rule_policy_says_so():
    r = rig()
    r.publish(active(r))
    tick = r.step()
    record = r.tracker.record(tick, supervisor_available=True, would_override=True)
    assert record.influence is WorkloadHintInfluence.WOULD_OVERRIDE
    assert record.skipped is None


def test_supervisor_failure_is_recorded_as_supervisor_unavailable():
    r = rig()
    r.publish(active(r))
    tick = r.step(regime=None)
    record = r.tracker.record(tick, supervisor_available=False, would_override=False)
    assert record.skipped is HintPriorSkipped.SUPERVISOR_UNAVAILABLE
    assert record.influence is WorkloadHintInfluence.NONE


def test_expiry_counts_from_the_row_boottime_and_is_reported_once():
    """期限の起点は行の boottime_ns（読んだ tick ではない。0066 §2.2）。"""
    r = rig()
    observation = active(r, declared=30_000)
    r.advance(29_999)
    r.publish(observation)
    assert r.step().hint is not None
    r.advance(1)
    r.publish(observation)
    tick = r.step()
    assert tick.hint is None
    assert tick.status is WorkloadHintStatus.EXPIRED
    assert [t.status for t in r.mailbox.reports] == [WorkloadHintStatus.EXPIRED]
    assert r.mailbox.reports[0].boottime_ns == r.boot.boottime_ns()
    # 読み手がまだ active と言っていても、再び active にしない（失効は一方向）
    r.advance(1_000)
    r.publish(observation)
    again = r.step()
    assert again.status is WorkloadHintStatus.EXPIRED
    assert len(r.mailbox.reports) == 1


def test_max_age_caps_the_declared_lifetime():
    r = rig()
    observation = active(r, declared=MAX_AGE_MS * 10)
    r.advance(MAX_AGE_MS)
    r.publish(observation)
    assert r.step().status is WorkloadHintStatus.EXPIRED


# ---------------------------------------------------------------- 矛盾


@pytest.mark.parametrize("regime", [WorkloadRegime.IDLE, WorkloadRegime.COOLDOWN])
def test_contradiction_after_the_hold_time(regime):
    r = rig()
    observation = active(r)
    r.publish(observation)
    assert r.step(regime=regime).hint is not None
    r.advance(HOLD_MS - 1)
    r.publish(observation)
    assert r.step(regime=regime).hint is not None
    r.advance(1)
    r.publish(observation)
    tick = r.step(regime=regime)
    assert tick.status is WorkloadHintStatus.CONTRADICTED
    assert [t.status for t in r.mailbox.reports] == [WorkloadHintStatus.CONTRADICTED]
    # Telemetry が一致しても再採用しない（0064 §2.7）
    r.advance(1_000)
    r.publish(observation)
    assert r.step(regime=WorkloadRegime.SUSTAINED_GPU).status is WorkloadHintStatus.CONTRADICTED


@pytest.mark.parametrize("interrupt", [WorkloadRegime.TRANSIENT_GPU, WorkloadRegime.UNKNOWN, None])
def test_a_break_in_the_idle_run_restarts_the_hold(interrupt):
    r = rig()
    observation = active(r)
    r.publish(observation)
    r.step(regime=WorkloadRegime.IDLE)
    r.advance(HOLD_MS - 1)
    r.publish(observation)
    r.step(regime=interrupt)
    r.advance(1)
    r.publish(observation)
    assert r.step(regime=WorkloadRegime.IDLE).status is WorkloadHintStatus.ACTIVE
    assert r.mailbox.reports == []


def test_cpu_regime_does_not_contradict_a_training_hint():
    """矛盾は大分類だけ（0133 §2.4）。v1 のヒントは CPU / GPU の軸を持たない。"""
    r = rig()
    observation = active(r)
    r.publish(observation)
    r.step(regime=WorkloadRegime.SUSTAINED_CPU)
    r.advance(HOLD_MS * 2)
    r.publish(observation)
    assert r.step(regime=WorkloadRegime.SUSTAINED_CPU).status is WorkloadHintStatus.ACTIVE


# ---------------------------------------------------------------- 読めないとき


def lost(r: Rig) -> WorkloadHintRecord:
    tick = r.step()
    assert tick.hint is None
    return r.tracker.record(tick, supervisor_available=True, would_override=True)


def test_no_feed_yet_is_unreadable():
    r = rig()
    record = lost(r)
    assert record == WorkloadHintRecord(
        stage=WorkloadHintStage.OBSERVE, skipped=HintPriorSkipped.HINT_STATE_UNREADABLE
    )


def test_dead_reader_is_unreadable():
    r = rig()
    r.publish(active(r))
    r.mailbox.alive = False
    assert lost(r).skipped is HintPriorSkipped.HINT_STATE_UNREADABLE


def test_stale_feed_is_unreadable():
    r = rig()
    r.publish(active(r))
    r.monotonic.advance_ms(STALE_MS + 1)
    assert lost(r).skipped is HintPriorSkipped.HINT_STATE_UNREADABLE


def test_failed_query_drops_the_hint_in_use():
    """稼働中に照会が失敗したら、使っていたヒントを外す（0066 §2.2）。"""
    r = rig()
    r.publish(active(r))
    assert r.step().hint is not None
    r.publish(None, state=HintFeedState.UNREADABLE)
    assert lost(r).skipped is HintPriorSkipped.HINT_STATE_UNREADABLE


def test_mailbox_error_is_unreadable_not_an_exception():
    r = rig()
    r.mailbox.error = RuntimeError("boom")
    assert lost(r).skipped is HintPriorSkipped.HINT_STATE_UNREADABLE


def test_run_start_not_durable_lasts_for_the_run_even_if_the_reader_exits():
    r = rig()
    r.publish(None, state=HintFeedState.RUN_START_NOT_DURABLE)
    r.mailbox.alive = False
    r.monotonic.advance_ms(STALE_MS * 10)
    assert lost(r).skipped is HintPriorSkipped.RUN_START_NOT_DURABLE


def test_terminal_status_from_the_reader_is_shown_without_age():
    r = rig()
    observation = HintObservation(
        event_id=9,
        status=WorkloadHintStatus.ENDED,
        workload=WorkloadHintKind.BENCHMARK,
        source_label="other",
    )
    r.publish(observation)
    tick = r.step()
    record = r.tracker.record(tick, supervisor_available=True, would_override=True)
    assert record.status is WorkloadHintStatus.ENDED
    assert record.age_ms is None
    assert record.influence is WorkloadHintInfluence.NONE


# ---------------------------------------------------------------- 補助の式


def test_declared_lifetime_uses_grace_or_the_default_ttl():
    config = hint_config()
    assert declared_ms(config, expected_duration_s=120) == 120_000 + 60_000
    assert declared_ms(config, expected_duration_s=None) == 600_000


def test_source_label_is_the_allowlist_or_other():
    config = hint_config()
    assert source_label(config, "job-launcher") == "job-launcher"
    assert source_label(config, "someone-else") == "other"
    assert source_label(config, None) == "other"


# ---------------------------------------------------------------- 記録の形


def test_record_rejects_hint_fields_when_state_is_lost():
    with pytest.raises(ValidationError, match="状態を読めない"):
        WorkloadHintRecord(
            stage=WorkloadHintStage.OBSERVE,
            event_id=1,
            workload=WorkloadHintKind.TRAINING,
            status=WorkloadHintStatus.ACTIVE,
            source_label="other",
            age_ms=0,
            skipped=HintPriorSkipped.HINT_STATE_UNREADABLE,
        )


def test_record_rejects_context_override_before_b2():
    with pytest.raises(ValidationError, match="context_override"):
        WorkloadHintRecord(
            stage=WorkloadHintStage.OBSERVE,
            event_id=1,
            workload=WorkloadHintKind.TRAINING,
            status=WorkloadHintStatus.ACTIVE,
            source_label="other",
            age_ms=0,
            influence=WorkloadHintInfluence.CONTEXT_OVERRIDE,
        )


def test_record_rejects_influence_without_an_active_hint():
    with pytest.raises(ValidationError, match="active"):
        WorkloadHintRecord(
            stage=WorkloadHintStage.OBSERVE,
            event_id=1,
            workload=WorkloadHintKind.TRAINING,
            status=WorkloadHintStatus.EXPIRED,
            source_label="other",
            influence=WorkloadHintInfluence.WOULD_OVERRIDE,
        )


def test_off_record_carries_nothing_else():
    with pytest.raises(ValidationError, match="stage: off"):
        WorkloadHintRecord(stage=WorkloadHintStage.OFF, skipped=HintPriorSkipped.MPC_UNAVAILABLE)


def test_unsupported_version_record_does_not_guess_the_workload():
    record = WorkloadHintRecord(
        stage=WorkloadHintStage.OBSERVE,
        event_id=3,
        status=WorkloadHintStatus.UNSUPPORTED_VERSION,
    )
    assert record.workload is None
    with pytest.raises(ValidationError, match="unsupported_version"):
        WorkloadHintRecord(
            stage=WorkloadHintStage.OBSERVE,
            event_id=3,
            status=WorkloadHintStatus.UNSUPPORTED_VERSION,
            workload=WorkloadHintKind.TRAINING,
        )


def test_trace_record_does_not_carry_free_text_fields():
    """``note``・生の ``source``・``peer_uid`` を載せない（0064 §2.9）。"""
    assert set(WorkloadHintRecord.model_fields) == {
        "schema_version",
        "stage",
        "event_id",
        "workload",
        "status",
        "source_label",
        "age_ms",
        "influence",
        "skipped",
    }


# ---------------------------------------------------------------- ControlTick v16


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(version: int) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(
        (FIXTURES / f"control_tick_v{version}.json").read_text(encoding="utf-8")
    )
    return loaded


def test_v16_requires_the_workload_hint_block():
    raw = _fixture(16)
    assert (
        ControlTick.model_validate_json(json.dumps(raw)).workload_hint == WorkloadHintRecord.off()
    )
    raw.pop("workload_hint")
    with pytest.raises(ValidationError, match="workload_hint が要る"):
        ControlTick.model_validate_json(json.dumps(raw))


def test_older_versions_cannot_carry_the_block():
    """「昔の tick はヒントが無かった」と「昔の形に概念が無かった」を取り違えない（0064 §2.9）。"""
    raw = _fixture(15)
    raw["workload_hint"] = {"schema_version": 1, "stage": "off", "influence": "none"}
    with pytest.raises(ValidationError, match="schema version 16"):
        ControlTick.model_validate_json(json.dumps(raw))
