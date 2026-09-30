"""`AuthorityRuntime` を `coldaisle-fand` へ配線する（#92 / 決定記録 0072 §2.10 段階 2）。

1. runtime（§2.6）: 管理ソケットの降格は **memory 上で先に**効き、journal へは heartbeat の後に
   書く。古い snapshot で no-op と判断しない。書けなければ上限を持ち続け、次の tick で書き直す
2. 外の process の変更（§2.6）: 毎 tick の `stat` で journal の変化を知り、次の tick から効く。
   走行中に読めなければ `SHADOW` に下げ、**読めるようになっただけでは戻さない**
3. journal v3: `authority_journal_unreadable` は v3 の journal にだけ置ける
4. 受け渡し口（§2.2）: authority の枠は最も低い行き先を採る。モードの枠とは互いに消さない
5. loop（§2.6 / §2.7）: 降格は Gate が stage を読む前に効き、`ControlTick` v13 の `authority` に残る
6. 管理ソケット（§2.3 / §2.7）: 安全側の指令として監査を待たずに置く。`raise_authority` は無い
7. `coldaisle-fand`: journal を読んで起動する。起動時に読めなければ制御を取らない（0057 §2.1）
8. 構造（§2.9）: LLM・読み取り API・eventd は authority の書き込みの経路を import しない

**authority を自動で上げる経路は無い。** どの試験でも、系が自分で stage を上げないことを確かめる。
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock, SystemMonotonicClock
from coldaisle.control.authority import (
    AUTHORITY_STATE_FILENAME,
    AuthorityChangeKind,
    AuthorityJournal,
    AuthorityRuntime,
    AuthorityStore,
    AuthorityTrigger,
    AutomaticCause,
)
from coldaisle.control.fallback.gate import ControllerGate
from coldaisle.control.operating_mode import (
    AdminAuthorityCommand,
    AdminModeCommand,
    AdminModeTracker,
    MailboxTake,
    ModeOutcomeKind,
)
from coldaisle.control.schema import (
    AuthorityRecord,
    AuthorityStage,
    ControlTick,
    OperatingMode,
)
from coldaisle.control_admin.mailbox import AdminMailbox, Placed, Superseded
from coldaisle.control_admin.messages import (
    LowerAuthorityRequest,
    RollbackAuthorityRequest,
    parse_request,
)
from coldaisle.metrics import MetricCatalog
from test_authority_rollout import NOW_MS
from test_authority_rollout import runtime as raised_runtime
from test_control_admin import (
    RUN_ID,
    FakeMailbox,
    RunningEntry,
    needs_peercred,
    wait_until,
)
from test_control_loop import METRICS_PATH, Harness, control_config

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
ACTOR = "uid.1000"


@pytest.fixture(scope="session")
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(METRICS_PATH)


def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "authority" / AUTHORITY_STATE_FILENAME


def other_store(tmp_path: Path) -> AuthorityStore:
    """外の process（人の CLI）の代わり。同じ root を別の store で読み書きする。"""
    return AuthorityStore(tmp_path / "authority", SimulatedClock(NOW_MS + 1), lock_timeout_ms=500)


def full_journal_bytes(tmp_path: Path) -> bytes:
    """承認を経て FULL まで上げた journal の bytes（別の root で作る）。"""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    raised_runtime(elsewhere, stage=AuthorityStage.FULL)
    return journal_path(elsewhere).read_bytes()


@pytest.fixture
def held_lock(tmp_path: Path) -> Iterator[Any]:
    """authority の排他 lock を別の持ち主として握る（昇格の途中の CLI の代わり）。"""
    root = tmp_path / "authority"
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / ".authority.lock", os.O_RDWR | os.O_CREAT, 0o600)

    class Lock:
        def hold(self) -> None:
            fcntl.flock(fd, fcntl.LOCK_EX)

        def release(self) -> None:
            fcntl.flock(fd, fcntl.LOCK_UN)

    try:
        yield Lock()
    finally:
        os.close(fd)


# ================================================================ 1. runtime（0072 §2.6）


def test_a_lowering_takes_effect_in_memory_before_it_is_written(tmp_path):
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.FULL)

    changed = runtime.apply_lowering(
        to_stage=AuthorityStage.LIMITED, actor=ACTOR, reason="夜間の OOD が多い"
    )

    assert changed is True
    assert runtime.current_stage() is AuthorityStage.LIMITED
    # まだ書いていない（書くのは heartbeat の後の maintain）
    assert other_store(tmp_path).read().stage is AuthorityStage.FULL
    assert runtime.trace_record(command_id=None).unpersisted_ceiling is AuthorityStage.LIMITED

    runtime.maintain()

    journal = other_store(tmp_path).read()
    assert journal.stage is AuthorityStage.LIMITED
    event = journal.events[-1]
    assert event.kind is AuthorityChangeKind.LOWERED
    assert event.trigger is AuthorityTrigger.HUMAN
    assert event.actor == ACTOR
    assert event.cause is None
    assert "夜間の OOD が多い" in event.reason
    # journal が表したので memory 上の上限は手放す。authority は上がらない
    record = runtime.trace_record(command_id=None)
    assert record.unpersisted_ceiling is None
    assert runtime.current_stage() is AuthorityStage.LIMITED


def test_a_lowering_is_not_judged_a_no_op_against_a_stale_journal(tmp_path):
    """外の process が journal を上げた直後でも、後から届いた降格は捨てられない（0072 §2.6）。"""
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.LIMITED)
    # この runtime がまだ気づいていない昇格（CLI）で journal が FULL になった
    journal_path(tmp_path).write_bytes(full_journal_bytes(tmp_path))

    changed = runtime.apply_lowering(
        to_stage=AuthorityStage.EXPANDED, actor=ACTOR, reason="昇格を見直す"
    )
    assert changed is False, "古い snapshot では下がって見えないが、上限は入る"
    runtime.maintain()
    runtime.maintain()

    assert other_store(tmp_path).read().stage is AuthorityStage.EXPANDED
    assert runtime.current_stage() is AuthorityStage.EXPANDED


def test_an_unwritten_lowering_survives_reload_and_is_retried(tmp_path, held_lock):
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.FULL)
    held_lock.hold()
    runtime.apply_lowering(to_stage=AuthorityStage.SHADOW, actor=ACTOR, reason="rollback")

    runtime.maintain()

    assert runtime.persist_failure is not None
    assert runtime.persist_failure.code == "authority_persist_failed"
    runtime.reload()
    assert runtime.current_stage() is AuthorityStage.SHADOW, "読み直しでは外れない"
    record = runtime.trace_record(command_id=None)
    assert record.unpersisted_ceiling is AuthorityStage.SHADOW
    assert record.persist_failure is not None

    held_lock.release()
    runtime.maintain()

    assert runtime.persist_failure is None
    assert other_store(tmp_path).read().stage is AuthorityStage.SHADOW
    assert runtime.trace_record(command_id=None).unpersisted_ceiling is None
    assert runtime.current_stage() is AuthorityStage.SHADOW


def test_repeated_lowerings_do_not_pile_up_unbounded(tmp_path, held_lock):
    """同じ深さ以上へ下げる予約が先にあれば足さない（予約は stage の段数で抑えられる）。"""
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.FULL)
    held_lock.hold()
    for _ in range(50):
        runtime.apply_lowering(to_stage=AuthorityStage.LIMITED, actor=ACTOR, reason="x")
        runtime.apply_lowering(to_stage=AuthorityStage.EXPANDED, actor=ACTOR, reason="x")
        runtime.maintain()
    held_lock.release()
    runtime.maintain()

    journal = other_store(tmp_path).read()
    assert journal.stage is AuthorityStage.LIMITED
    lowered = [event for event in journal.events if event.kind is AuthorityChangeKind.LOWERED]
    assert len(lowered) == 1


def test_a_malformed_actor_is_refused_before_anything_changes(tmp_path):
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.FULL)
    with pytest.raises(ValueError, match="actor"):
        runtime.apply_lowering(to_stage=AuthorityStage.SHADOW, actor="Root", reason="x")
    assert runtime.current_stage() is AuthorityStage.FULL


# ================================================================ 2. 外の process（0072 §2.6）


def test_an_external_rollback_is_seen_on_the_next_tick(tmp_path):
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.FULL)

    other_store(tmp_path).rollback_to_baseline(actor="rack-owner", reason="CLI の rollback")
    assert runtime.current_stage() is AuthorityStage.FULL, "stat を見るまでは前の journal"

    runtime.maintain()

    assert runtime.current_stage() is AuthorityStage.SHADOW
    assert runtime.journal.stage is AuthorityStage.SHADOW


def test_an_unchanged_journal_is_not_read_again(tmp_path, monkeypatch):
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.FULL)
    runtime.maintain()
    reads: list[int] = []
    original = AuthorityStore.read

    def counting_read(self: AuthorityStore) -> AuthorityJournal:
        reads.append(1)
        return original(self)

    monkeypatch.setattr(AuthorityStore, "read", counting_read)
    for _ in range(5):
        runtime.maintain()
    assert reads == []


def test_an_unreadable_journal_drops_to_shadow_and_reading_again_does_not_restore(
    tmp_path, held_lock
):
    """走行中に壊れたら止めずに SHADOW。**読めるようになっただけでは戻さない**（0072 §2.6）。"""
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.FULL)
    good = journal_path(tmp_path).read_bytes()
    journal_path(tmp_path).write_bytes(b"{broken")

    runtime.maintain()

    assert runtime.current_stage() is AuthorityStage.SHADOW
    assert runtime.journal_unreadable is True
    record = runtime.trace_record(command_id=None)
    assert record.journal_unreadable is True
    assert record.unpersisted_ceiling is AuthorityStage.SHADOW

    # 読めるようになったが、書き残せない間は戻さない
    journal_path(tmp_path).write_bytes(good)
    held_lock.hold()
    runtime.maintain()
    runtime.maintain()
    assert runtime.journal.stage is AuthorityStage.FULL, "読めた journal は FULL を表す"
    assert runtime.current_stage() is AuthorityStage.SHADOW, "それを理由に戻さない"

    held_lock.release()
    runtime.maintain()

    journal = other_store(tmp_path).read()
    assert journal.stage is AuthorityStage.SHADOW
    assert journal.schema_version == 3
    last = journal.events[-1]
    assert last.cause is AutomaticCause.AUTHORITY_JOURNAL_UNREADABLE
    assert last.trigger is AuthorityTrigger.AUTOMATIC
    assert runtime.journal_unreadable is False
    assert runtime.current_stage() is AuthorityStage.SHADOW
    # 戻すには 0057 §2.3 の承認による昇格（SHADOW から1段ずつ）が要る


def test_a_journal_that_disappears_is_not_a_reason_to_raise(tmp_path):
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.LIMITED)
    journal_path(tmp_path).unlink()

    runtime.maintain()

    # 記録の無い journal は SHADOW から始まる（0057 §2.1）。上がる向きには動かない
    assert runtime.current_stage() is AuthorityStage.SHADOW


# ================================================================ 3. journal v3


def test_the_unreadable_cause_needs_a_v3_journal(tmp_path):
    runtime = raised_runtime(tmp_path, stage=AuthorityStage.LIMITED)
    good = journal_path(tmp_path).read_bytes()
    journal_path(tmp_path).write_bytes(b"not json")
    runtime.maintain()
    journal_path(tmp_path).write_bytes(good)
    runtime.maintain()
    payload = json.loads(journal_path(tmp_path).read_text("utf-8"))
    assert payload["schema_version"] == 3
    payload["schema_version"] = 2
    with pytest.raises(ValidationError, match="v3"):
        AuthorityJournal.model_validate_json(json.dumps(payload))


# ================================================================ 4. 受け渡し口（0072 §2.2）


def authority_command(command_id: int, to_stage: AuthorityStage) -> AdminAuthorityCommand:
    rollback = to_stage is AuthorityStage.SHADOW
    return AdminAuthorityCommand(
        command_id=command_id,
        op="rollback_authority" if rollback else "lower_authority",
        to_stage=to_stage,
        actor=ACTOR,
        reason="x",
    )


def test_the_authority_slot_keeps_the_lowest_destination():
    mailbox = AdminMailbox()
    try:
        assert mailbox.place_authority(authority_command(1, AuthorityStage.SHADOW)) == Placed(
            replaced=None
        )
        # 後から来た浅い降格は、先の rollback を打ち消さない
        assert mailbox.place_authority(authority_command(2, AuthorityStage.LIMITED)) == (
            Superseded(by=1)
        )
        taken = mailbox.take()
        assert taken is not None and taken.authority is not None
        assert taken.authority.command_id == 1

        mailbox.place_authority(authority_command(3, AuthorityStage.EXPANDED))
        placed = mailbox.place_authority(authority_command(4, AuthorityStage.SHADOW))
        assert isinstance(placed, Placed)
        assert placed.replaced is not None and placed.replaced.command_id == 3
    finally:
        mailbox.close()


def test_the_two_slots_do_not_erase_each_other():
    """`MAX` のあとに rollback が来ても、両方が同じ tick で効く（0072 §2.2）。"""
    mailbox = AdminMailbox()
    try:
        mailbox.place_mode(AdminModeCommand(command_id=1, mode=OperatingMode.MAX))
        mailbox.place_authority(authority_command(2, AuthorityStage.SHADOW))
        taken = mailbox.take()
        assert taken is not None
        assert taken.mode is not None and taken.mode.command_id == 1
        assert taken.authority is not None and taken.authority.command_id == 2
        assert mailbox.take() == MailboxTake()
    finally:
        mailbox.close()


def test_the_loop_never_waits_for_the_two_slots():
    mailbox = AdminMailbox()
    try:
        mailbox.place_authority(authority_command(1, AuthorityStage.SHADOW))
        with mailbox._lock:
            assert mailbox.take() is None
        taken = mailbox.take()
        assert taken is not None and taken.authority is not None
    finally:
        mailbox.close()


def test_there_is_no_command_that_raises_authority():
    """降格の型は下げる向きしか表せない。rollback は必ず Baseline へ戻す。"""
    with pytest.raises(ValidationError, match="Baseline"):
        AdminAuthorityCommand(
            command_id=1,
            op="rollback_authority",
            to_stage=AuthorityStage.FULL,
            actor=ACTOR,
            reason="x",
        )
    with pytest.raises(ValidationError):
        AdminAuthorityCommand(
            command_id=1,
            op="raise_authority",  # type: ignore[arg-type]
            to_stage=AuthorityStage.FULL,
            actor=ACTOR,
            reason="x",
        )


# ================================================================ 5. loop（0072 §2.6 / §2.7）


def journal_harness(
    catalog: MetricCatalog, tmp_path: Path, *, stage: AuthorityStage = AuthorityStage.FULL
) -> tuple[Harness, FakeMailbox, AuthorityRuntime]:
    config = control_config(policy={"authority_stage": "full"})
    runtime = raised_runtime(tmp_path, stage=stage)
    mailbox = FakeMailbox()
    harness = Harness(
        catalog,
        config=config,
        authority=runtime,
        gate=ControllerGate(
            config.policy,
            expected_model_version="thermal-vtest",
            expected_artifact_sha256=None,
            authority=runtime,
        ),
        admin_mode=AdminModeTracker(mailbox, run_id=RUN_ID),
    )
    harness.settle()
    return harness, mailbox, runtime


def test_a_socket_rollback_applies_before_the_gate_reads_the_stage(catalog, tmp_path):
    harness, mailbox, _runtime = journal_harness(catalog, tmp_path)
    before = harness.tick().tick
    assert before.state.authority_stage is AuthorityStage.FULL
    assert before.authority is not None
    assert before.authority.entry == "journal"
    assert before.authority.journal_stage is AuthorityStage.FULL

    mailbox.pending_authority = authority_command(7, AuthorityStage.SHADOW)
    tick = harness.tick().tick

    assert tick.schema_version == 13
    assert tick.state.authority_stage is AuthorityStage.SHADOW, "同じ tick の Gate から効く"
    assert tick.authority is not None
    assert tick.authority.command_id == 7
    assert tick.authority.unpersisted_ceiling is AuthorityStage.SHADOW
    assert (ModeOutcomeKind.APPLIED, 7, tick.tick_id) in [
        (outcome.kind, outcome.command_id, outcome.tick_id) for outcome in mailbox.outcomes
    ]
    # journal へは heartbeat と trace の保存の後に書いた
    journal = other_store(tmp_path).read()
    assert journal.stage is AuthorityStage.SHADOW
    assert journal.events[-1].actor == ACTOR
    assert "command_id=7" in journal.events[-1].reason

    after = harness.tick().tick
    assert after.authority is not None
    assert after.authority.command_id is None
    assert after.authority.unpersisted_ceiling is None
    assert after.state.authority_stage is AuthorityStage.SHADOW
    ControlTick.model_validate_json(after.model_dump_json())


def test_max_and_a_rollback_in_the_same_tick_both_apply(catalog, tmp_path):
    harness, mailbox, _runtime = journal_harness(catalog, tmp_path)
    mailbox.pending = AdminModeCommand(command_id=1, mode=OperatingMode.MAX)
    mailbox.pending_authority = authority_command(2, AuthorityStage.SHADOW)

    tick = harness.tick().tick

    assert tick.state.operating_mode is OperatingMode.MAX
    assert tick.state.authority_stage is AuthorityStage.SHADOW
    assert tick.mode_command is not None and tick.mode_command.command_id == 1
    assert tick.authority is not None and tick.authority.command_id == 2


def test_an_external_change_takes_effect_from_the_next_tick(catalog, tmp_path):
    harness, _mailbox, _runtime = journal_harness(catalog, tmp_path)
    other_store(tmp_path).lower_stage(
        to_stage=AuthorityStage.LIMITED,
        actor="rack-owner",
        reason="CLI",
        trigger=AuthorityTrigger.HUMAN,
    )
    first = harness.tick().tick
    second = harness.tick().tick

    assert first.state.authority_stage is AuthorityStage.FULL, "stat は heartbeat の後"
    assert second.state.authority_stage is AuthorityStage.LIMITED
    assert second.authority is not None
    assert second.authority.journal_stage is AuthorityStage.LIMITED


def test_a_broken_journal_does_not_stop_the_loop(catalog, tmp_path):
    harness, _mailbox, _runtime = journal_harness(catalog, tmp_path)
    journal_path(tmp_path).write_bytes(b"\x00" * 16)

    harness.tick()
    result = harness.tick()

    assert result.hardware is not None, "冷却は止めない"
    tick = result.tick
    assert tick.state.authority_stage is AuthorityStage.SHADOW
    assert tick.authority is not None
    assert tick.authority.journal_unreadable is True


def test_the_trace_cannot_claim_a_stage_above_its_ceilings():
    body = json.loads(
        (Path(__file__).parent / "fixtures" / "control_tick_v13.json").read_text("utf-8")
    )
    body["state"]["authority_stage"] = "limited"
    body["state"]["fallback_reason"] = {"code": "no_proposal", "detail": "worker なし"}
    body["authority"]["journal_stage"] = "limited"
    ControlTick.model_validate_json(json.dumps(body))  # journal と設定の上限（limited）の中
    body["authority"]["unpersisted_ceiling"] = "shadow"
    with pytest.raises(ValidationError, match="上限を超えている"):
        ControlTick.model_validate_json(json.dumps(body))
    body["authority"]["unpersisted_ceiling"] = None
    body["authority"]["journal_stage"] = "shadow"
    body["state"]["authority_stage"] = "shadow"
    body["state"]["fallback_reason"] = None
    body["schema_version"] = 12
    with pytest.raises(ValidationError, match="schema version 13"):
        ControlTick.model_validate_json(json.dumps(body))
    del body["authority"]
    ControlTick.model_validate_json(json.dumps(body))
    body["schema_version"] = 13
    with pytest.raises(ValidationError, match="authority が要る"):
        ControlTick.model_validate_json(json.dumps(body))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"entry": "static", "journal_stage": "full", "journal_revision": 1}, "journal の欄"),
        ({"journal_revision": None}, "一緒に記録する"),
        ({"unpersisted_ceiling": "full"}, "None で表す"),
        ({"journal_unreadable": True}, "SHADOW の上限"),
    ],
)
def test_an_authority_record_describes_one_source(changes, message):
    base: dict[str, Any] = {
        "entry": "journal",
        "journal_stage": "full",
        "journal_revision": 3,
        "config_ceiling": "full",
    }
    with pytest.raises(ValidationError, match=message):
        AuthorityRecord.model_validate(base | changes, strict=False)


# ======================================================== 6. 管理ソケット（0072 §2.3 / §2.7）


def test_the_protocol_accepts_both_demotions():
    lower = parse_request(
        b'{"v": 1, "op": "lower_authority", "to_stage": "limited", "reason": "OOD"}\n'
    )
    assert isinstance(lower, LowerAuthorityRequest)
    rollback = parse_request(b'{"v": 1, "op": "rollback_authority", "reason": "\xe8\xa6\x8b"}\n')
    assert isinstance(rollback, RollbackAuthorityRequest)


def socket_tick(entry: RunningEntry) -> Any:
    """loop の tick の先頭の代わり。authority の降格を入れたことを受付スレッドへ返す。"""
    entry.tick_id += 1
    resolution = entry.tracker.resolve(
        tick_id=entry.tick_id, now_mono_ms=SystemMonotonicClock().monotonic_ms()
    )
    if resolution.authority is not None:
        entry.tracker.authority_applied(
            command_id=resolution.authority.command_id, tick_id=entry.tick_id
        )
    entry.tracker.publish(
        tick_id=entry.tick_id,
        authority_stage=AuthorityStage.SHADOW,
        authority=AuthorityRecord(
            entry="journal",
            journal_stage=AuthorityStage.LIMITED,
            journal_revision=4,
            config_ceiling=AuthorityStage.FULL,
            unpersisted_ceiling=AuthorityStage.SHADOW,
        ),
    )
    return resolution


def tick_until_authority(entry: RunningEntry) -> AdminAuthorityCommand:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        resolution = socket_tick(entry)
        if resolution.authority is not None:
            command: AdminAuthorityCommand = resolution.authority
            return command
        time.sleep(0.005)
    raise AssertionError("降格が受け渡し口に届かなかった")


@pytest.fixture
def entry(short_dir: Path, rules: Any) -> Iterator[RunningEntry]:
    running = RunningEntry(short_dir, rules)
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
def short_dir() -> Iterator[Path]:
    import shutil
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="cauth-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def rules() -> Any:
    from coldaisle.store import QualityRules
    from conftest import QUALITY_RULES_PATH

    return QualityRules.from_yaml(QUALITY_RULES_PATH)


ROLLBACK = {"v": 1, "op": "rollback_authority", "reason": "新しい artifact の挙動を見直す"}
LOWER = {"v": 1, "op": "lower_authority", "to_stage": "limited", "reason": "夜間の OOD が多い"}


@needs_peercred
def test_a_rollback_is_applied_and_acknowledged_with_its_tick(entry):
    reply = entry.send_async(ROLLBACK)
    command = tick_until_authority(entry)

    response = reply.result(timeout=5)
    assert response["ok"] is True
    assert response["applied"] is True
    assert response["command_id"] == command.command_id
    assert command.to_stage is AuthorityStage.SHADOW
    assert command.actor == f"uid.{os.getuid()}", "操作者は peer credential から決める"
    wait_until(
        lambda: (
            [row[:2] for row in entry.rows()]
            == [("accepted", command.command_id), ("applied", command.command_id)]
        )
    )


@needs_peercred
def test_a_shallower_lowering_after_a_rollback_is_superseded_by_the_earlier_one(entry):
    first = entry.send_async(ROLLBACK)
    wait_until(lambda: entry.mailbox._authority is not None)
    second = entry.send(LOWER)

    assert second["ok"] is True
    assert second["superseded_by"] == 1
    command = tick_until_authority(entry)
    assert command.command_id == 1
    assert first.result(timeout=5)["applied"] is True
    wait_until(lambda: ("superseded", 2, None, 1) in entry.rows())


@needs_peercred
def test_a_demotion_does_not_wait_for_the_audit_database(short_dir, rules):
    """安全側の指令は監査の書き込みを待たずに受け渡し口へ置く（0072 §2.7）。"""
    running = RunningEntry(short_dir, rules)
    try:
        running.gate.clear()  # 監査の DB が lock されている
        reply = running.send_async(ROLLBACK)
        command = tick_until_authority(running)
        assert reply.result(timeout=5)["applied"] is True
        assert command.to_stage is AuthorityStage.SHADOW
    finally:
        running.stop()


@needs_peercred
def test_status_reports_the_journal_and_the_unpersisted_ceiling(entry):
    socket_tick(entry)
    status = entry.send({"v": 1, "op": "status"})
    assert status["ok"] is True
    assert status["authority_journal_stage"] == "limited"
    assert status["authority_journal_revision"] == 4
    assert status["authority_ceiling"] == "full"
    assert status["authority_unpersisted_ceiling"] == "shadow"
    assert status["authority_journal_unreadable"] is False
    assert status["persist_failure"] is None


# ================================================================ 7. coldaisle-fand


def daemon_config(tmp_path: Path, **changes: Any) -> Any:
    from coldaisle.control_daemon import Config
    from test_control_config import valid_documents, write_documents
    from test_simulated_fan_backend import hardware_config

    config_dir = tmp_path / "control"
    config_dir.mkdir(exist_ok=True)
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(config_dir, documents)
    return Config(
        config_dir=config_dir,
        db=tmp_path / "control.db",
        metrics=METRICS_PATH,
        admin_config=None,
        authority_root=tmp_path / "authority",
        **changes,
    )


def test_the_daemon_reads_the_journal_and_records_it_every_tick(tmp_path):
    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import build

    daemon = build(daemon_config(tmp_path), watchdog=NullWatchdog())
    try:
        tick = daemon.loop.tick().tick
        assert tick.authority is not None
        assert tick.authority.entry == "journal"
        assert tick.authority.journal_stage is AuthorityStage.SHADOW
        assert tick.authority.journal_revision == 0
        assert tick.state.authority_stage is AuthorityStage.SHADOW
    finally:
        daemon.close()
    assert not (tmp_path / "authority").exists(), "読むだけの起動は journal を作らない"


def test_the_daemon_does_not_take_control_over_a_broken_journal(tmp_path):
    """起動時に読めない journal は「記録の無い状態」と読み替えない（0057 §2.1）。"""
    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import StartupEnvironmentError, build

    (tmp_path / "authority").mkdir()
    journal_path(tmp_path).write_text('{"schema_version": 3, "revision": 9}', encoding="utf-8")
    with pytest.raises(StartupEnvironmentError, match="Authority"):
        build(daemon_config(tmp_path), watchdog=NullWatchdog())


def test_the_daemon_takes_the_authority_root_from_the_command_line():
    from coldaisle.control_daemon import DEFAULT_AUTHORITY_ROOT, build_parser

    args = build_parser().parse_args(["--authority-root", "var/test-authority"])
    assert args.authority_root == Path("var/test-authority")
    assert build_parser().parse_args([]).authority_root == DEFAULT_AUTHORITY_ROOT


# ================================================================ 8. 構造（0072 §2.9）


def _imports(path: Path) -> set[str]:
    import ast

    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_llm_read_api_and_event_entry_do_not_reach_the_authority_write_path():
    """AI 層・読み取り API・ツール窓口・eventd は authority の journal を書く経路を持たない。"""
    paths = [SRC / "server.py"]
    for package in ("ai", "api", "event_entry", "rules", "notify"):
        paths.extend(sorted((SRC / package).rglob("*.py")))
    forbidden = ("coldaisle.control.authority", "coldaisle.control_admin")
    hits = [
        f"{path.relative_to(SRC)}: {name}"
        for path in paths
        for name in _imports(path)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]
    assert hits == []


def test_the_admin_entry_never_touches_the_authority_runtime():
    """受付スレッドは `AuthorityRuntime` に触れない（受け渡し口へ置くだけ。0072 §2.6）。"""
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((SRC / "control_admin").rglob("*.py"))
    )
    for word in ("AuthorityStore", "raise_stage", "lower_stage(", "apply_lowering("):
        assert word not in source


# ================================================================ 9. クライアント


def test_the_client_can_lower_but_never_raise():
    from coldaisle.control_admin import client as admin_client

    parser = admin_client.build_parser()
    for argv in (
        ["raise-authority", "--reason", "x"],
        ["lower-authority", "--to-stage", "full", "--reason", "x"],
        ["lower-authority", "--reason", "x"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    args = parser.parse_args(["lower-authority", "--to-stage", "limited", "--reason", "OOD"])
    assert admin_client._body(args) == {
        "v": 1,
        "op": "lower_authority",
        "to_stage": "limited",
        "reason": "OOD",
    }
    args = parser.parse_args(["rollback-authority", "--reason", "見直す"])
    assert admin_client._body(args) == {"v": 1, "op": "rollback_authority", "reason": "見直す"}


@needs_peercred
def test_the_client_rollback_round_trips(entry, capsys):
    from coldaisle.control_admin import client as admin_client

    socket_args = ["--socket", str(entry.path), "--timeout", "5"]
    assert admin_client.main([*socket_args, "rollback-authority", "--reason", "試験"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["ok"] is True
    assert response["pending"] is True
    command = tick_until_authority(entry)
    assert command.command_id == response["command_id"]
