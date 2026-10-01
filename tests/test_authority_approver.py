"""Authority の昇格の承認者を、実行した process の uid に結びつける（#92 / 決定記録 0086 段階 3a）。

**実機も実 root も要らない。** 実行者は `ProcessIdentity` を差し替えて試す（0086 §2.9）。
試験の process 自身は authority のディレクトリの所有者（= fand の役）である。

守る性質:

1. 昇格の event は `uid.<数値>` と `approver_binding = "process_uid"` を持つ
2. root・ディレクトリの所有者・`uid != euid`・承認者の不一致・束縛の無い承認を、
   **journal を書かずに** `AuthorityStore.raise_stage()` で拒む（CLI を通さなくても）
3. 環境変数（`SUDO_UID` など）は実行者に効かない
4. 降格（rollback）は uid で拒まない
5. journal v4 の形と、v1〜v3 からの一方向の書き直し
6. journal・lock を `umask` に依らず `0660` で作る。
   CLI の store はディレクトリを作らず、形を確かめる
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control import (
    AUTHORITY_STATE_FILENAME,
    ApproverRejection,
    AuthorityApprovalError,
    AuthorityApproverError,
    AuthorityJournal,
    AuthorityStage,
    AuthorityStore,
    AuthorityStoreError,
    AutomaticCause,
    OsProcessIdentity,
    ProcessCredentials,
    StageApproval,
)
from test_authority_rollout import (
    APPROVER,
    APPROVER_IDENTITY,
    APPROVER_UID,
    NOW_MS,
    FixedIdentity,
    approval_for,
    evidence_for,
    learned_arm,
    raise_stage,
    report_document,
    store,
)

LOCK_FILENAME = ".authority.lock"
RUNS_AS_ROOT = os.geteuid() == 0
"""root で走る CI では「所有者と同じ uid」が root の拒否と区別できない（0086 §2.9）。"""


def authority_root(tmp_path: Path) -> Path:
    return tmp_path / "authority"


def journal_path(tmp_path: Path) -> Path:
    return authority_root(tmp_path) / AUTHORITY_STATE_FILENAME


def store_as(tmp_path: Path, identity: Any, **options: Any) -> AuthorityStore:
    return AuthorityStore(
        authority_root(tmp_path),
        SimulatedClock(NOW_MS),
        lock_timeout_ms=500,
        identity=identity,
        **options,
    )


def bound_approval(document: bytes, approver: str, **options: Any) -> StageApproval:
    """`approver` を差し替えた束縛済みの承認。"""
    return approval_for(document, **options).model_copy(update={"approver": approver})


def raised_once(tmp_path: Path) -> AuthorityJournal:
    """承認者として1段上げた journal（LIMITED）。"""
    document = report_document()
    return raise_stage(store(tmp_path), approval=approval_for(document), document=document)


def raise_from_limited(authority: AuthorityStore) -> AuthorityJournal:
    """LIMITED から EXPANDED へ、承認者として1段上げる。"""
    stage = AuthorityStage.LIMITED
    document = report_document(stages=(stage.value,), arm_stage=stage)
    approval = approval_for(
        document,
        from_stage=stage,
        revision=1,
        evidence=evidence_for(document, arm=learned_arm(stage).key),
    )
    return raise_stage(authority, approval=approval, document=document)


def legacy_v3_payload(journal: AuthorityJournal) -> dict[str, Any]:
    """v3 までの自己申告の承認（`approver_binding` の無い昇格）を持つ journal の形。"""
    payload: dict[str, Any] = json.loads(journal.model_dump_json())
    payload["schema_version"] = 3
    for event in payload["events"]:
        if event["approval"] is not None:
            del event["approval"]["approver_binding"]
            event["approval"]["approver"] = "rack-owner"
            event["actor"] = "rack-owner"
    return payload


@pytest.fixture
def umask_0022() -> Iterator[None]:
    """人の shell によくある `umask`。そのまま作ると `0640` になる（0086 §2.4）。"""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def shared_root(tmp_path: Path, mode: int = 0o2770) -> Path:
    """導入手順が作る形のディレクトリ（`install -d ... -m 2770`）。"""
    root = authority_root(tmp_path)
    root.mkdir()
    os.chmod(root, mode)
    if mode & stat.S_ISGID and not os.stat(root).st_mode & stat.S_ISGID:
        pytest.skip("この環境ではディレクトリに setgid を付けられない")
    return root


# ================================================================ 1. 記録の形（§2.7 / §2.8）


def test_a_raise_records_the_process_uid_as_a_bound_approver(tmp_path: Path) -> None:
    journal = raised_once(tmp_path)

    event = journal.events[-1]
    assert event.actor == APPROVER == f"uid.{APPROVER_UID}"
    assert event.approval is not None
    assert event.approval.approver == APPROVER
    assert event.approval.approver_binding == "process_uid"
    stored = json.loads(journal_path(tmp_path).read_text("utf-8"))
    assert stored["schema_version"] == 4
    assert stored["events"][0]["approval"]["approver_binding"] == "process_uid"


# ================================================================ 2. 拒否（§2.3 / §2.5）


def test_an_approver_other_than_the_process_uid_is_refused(tmp_path: Path) -> None:
    """**別の人の名前で承認を書かない。** 承認者の欄は実行者と一致しなければならない。"""
    document = report_document()
    approval = bound_approval(document, f"uid.{APPROVER_UID + 1}")

    with pytest.raises(AuthorityApproverError) as caught:
        raise_stage(store(tmp_path), approval=approval, document=document)

    assert caught.value.code is ApproverRejection.APPROVER_MISMATCH
    assert isinstance(caught.value, AuthorityApprovalError), "§2.5: AuthorityApprovalError で拒む"
    assert not journal_path(tmp_path).exists()


def test_a_self_declared_approval_is_not_written_any_more(tmp_path: Path) -> None:
    """**束縛の無い承認では昇格を書かない**（v3 までの自己申告。0086 §2.7）。"""
    document = report_document()
    approval = approval_for(document).model_copy(update={"approver_binding": None})

    with pytest.raises(AuthorityApproverError) as caught:
        raise_stage(store(tmp_path), approval=approval, document=document)

    assert caught.value.code is ApproverRejection.UNBOUND_APPROVAL
    assert not journal_path(tmp_path).exists()


def test_root_cannot_approve_and_nothing_is_touched(tmp_path: Path) -> None:
    """root は journal を直接書けるので、承認者の帰属を言えない（0086 §2.3）。"""
    document = report_document()
    approval = approval_for(document)

    with pytest.raises(AuthorityApproverError) as caught:
        raise_stage(store_as(tmp_path, FixedIdentity(0)), approval=approval, document=document)

    assert caught.value.code is ApproverRejection.ROOT
    assert not authority_root(tmp_path).exists(), "journal を読む前に拒む（何も作らない）"


def test_a_setuid_wrapper_cannot_approve(tmp_path: Path) -> None:
    """実行した人と書いた権限が食い違う process からは書かない（0086 §2.1）。"""
    document = report_document()
    identity = FixedIdentity(APPROVER_UID, euid=APPROVER_UID + 1)

    with pytest.raises(AuthorityApproverError) as caught:
        raise_stage(
            store_as(tmp_path, identity), approval=approval_for(document), document=document
        )

    assert caught.value.code is ApproverRejection.UID_EUID_MISMATCH
    assert not authority_root(tmp_path).exists()


@pytest.mark.skipif(RUNS_AS_ROOT, reason="root では所有者の拒否が root の拒否に先取りされる")
@pytest.mark.parametrize("journal_exists", [True, False])
def test_the_owner_of_the_authority_root_cannot_raise_even_without_the_cli(
    tmp_path: Path, journal_exists: bool
) -> None:
    """**fand の実行ユーザーが Python から `raise_stage()` を直接呼んでも上げられない**。

    0086 §2.3。

    承認は実行者と一致する（§2.5 の束縛は通る）ので、所有者の拒否が無いと昇格を書けてしまう。
    """
    owner = os.geteuid()
    if journal_exists:
        raised_once(tmp_path)
        before = journal_path(tmp_path).read_bytes()
        stage = AuthorityStage.LIMITED
        document = report_document(stages=(stage.value,), arm_stage=stage)
        approval = bound_approval(
            document,
            f"uid.{owner}",
            from_stage=stage,
            revision=1,
            evidence=evidence_for(document, arm=learned_arm(stage).key),
        )
    else:
        document = report_document()
        approval = bound_approval(document, f"uid.{owner}")
    authority = store_as(tmp_path, FixedIdentity(owner))

    with pytest.raises(AuthorityApproverError) as caught:
        raise_stage(authority, approval=approval, document=document)

    assert caught.value.code is ApproverRejection.AUTHORITY_ROOT_OWNER
    if journal_exists:
        assert journal_path(tmp_path).read_bytes() == before, "journal を書かない"
    else:
        assert not journal_path(tmp_path).exists()


def test_environment_variables_do_not_change_the_recorded_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`SUDO_UID` は root なら誰でも好きな値にできる。カーネルの値だけを使う（0086 §2.1）。"""
    other = str(os.getuid() + 1)
    for name in ("SUDO_UID", "SUDO_USER", "LOGNAME", "USER"):
        monkeypatch.setenv(name, other)

    credentials = OsProcessIdentity().credentials()

    assert credentials == ProcessCredentials(uid=os.getuid(), euid=os.geteuid())
    assert credentials.actor == f"uid.{os.getuid()}"


@pytest.mark.parametrize("uid", [-1, 4_294_967_295])
def test_a_uid_outside_the_recordable_range_is_refused(uid: int) -> None:
    with pytest.raises(AuthorityApproverError) as caught:
        ProcessCredentials(uid=uid, euid=uid)
    assert caught.value.code is ApproverRejection.INVALID_UID


# ================================================================ 3. rollback（§2.6）


def test_root_can_still_roll_back_and_is_recorded_as_uid_0(tmp_path: Path) -> None:
    """**降格は uid で拒まない。** 障害時に root しか残っていなくても戻せる（0057 §2.6）。"""
    raised_once(tmp_path)

    lowered = store_as(tmp_path, FixedIdentity(0)).rollback_to_baseline(
        actor="uid.0", reason="fand が止まっている間に戻す"
    )

    assert lowered.stage is AuthorityStage.SHADOW
    assert lowered.events[-1].actor == "uid.0"


# ================================================================ 4. journal v4（§2.7）


def test_a_bound_approval_must_be_a_non_root_uid() -> None:
    """uid.0（root）は raise_stage が作らない。journal に現れたら書き換えを疑う（0086 §2.7）。"""
    payload = json.loads(approval_for(report_document()).model_dump_json())
    for approver in ("uid.0", "rack-owner", "uid.01", "uid.4294967295"):
        with pytest.raises(ValidationError, match="束縛した承認者"):
            StageApproval.model_validate_json(json.dumps(payload | {"approver": approver}))
    # 束縛の無い（v3 までの）承認は、従来どおり自己申告の名前を読める
    legacy = {key: value for key, value in payload.items() if key != "approver_binding"}
    assert StageApproval.model_validate_json(json.dumps(legacy | {"approver": "rack-owner"}))


def test_a_bound_approval_cannot_sit_in_a_v3_journal(tmp_path: Path) -> None:
    payload = json.loads(raised_once(tmp_path).model_dump_json())
    payload["schema_version"] = 3

    with pytest.raises(ValidationError, match="v4"):
        AuthorityJournal.model_validate_json(json.dumps(payload))


def test_an_unbound_raise_cannot_follow_a_bound_one(tmp_path: Path) -> None:
    """**束縛は一度入ったら外せない。** 後の昇格が自己申告へ戻る記録を作らない。"""
    raised_once(tmp_path)
    journal = raise_from_limited(store(tmp_path))
    payload = json.loads(journal.model_dump_json())
    del payload["events"][1]["approval"]["approver_binding"]

    with pytest.raises(ValidationError, match="束縛の無い昇格"):
        AuthorityJournal.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("write", ["raise", "lower"])
def test_a_v3_journal_is_rewritten_as_v4_without_touching_old_events(
    tmp_path: Path, write: str
) -> None:
    """v1〜v3 はそのまま読み、次に書くときに v4 で書き直す。**既存の event は書き換えない。**"""
    legacy = legacy_v3_payload(raised_once(tmp_path))
    journal_path(tmp_path).write_text(json.dumps(legacy), encoding="utf-8")
    assert store(tmp_path).read().schema_version == 3

    if write == "raise":
        updated = raise_from_limited(store(tmp_path))
    else:
        updated = store(tmp_path).lower_stage(
            to_stage=AuthorityStage.SHADOW,
            actor="control_runtime",
            reason="試験の降格",
            cause=AutomaticCause.REPEATED_FALLBACK,
        )

    stored = json.loads(journal_path(tmp_path).read_text("utf-8"))
    assert updated.schema_version == stored["schema_version"] == 4
    assert stored["events"][0] == legacy["events"][0], "自己申告の昇格は同じ内容のまま残る"
    assert "approver_binding" not in stored["events"][0]["approval"]


# ================================================================ 5. mode と共有ディレクトリ


def test_journal_and_lock_are_0660_regardless_of_umask(tmp_path: Path, umask_0022: None) -> None:
    """`0640` だと fand が lock を `O_RDWR` で開けず、降格を書き残せなくなる。"""
    shared_root(tmp_path)

    document = report_document()
    raise_stage(
        store_as(tmp_path, APPROVER_IDENTITY, require_shared_root=True),
        approval=approval_for(document),
        document=document,
    )

    for name in (AUTHORITY_STATE_FILENAME, LOCK_FILENAME):
        mode = stat.S_IMODE(os.stat(authority_root(tmp_path) / name).st_mode)
        assert mode == 0o660, f"{name}: {mode:04o}"


def test_the_fand_store_also_writes_0660(tmp_path: Path, umask_0022: None) -> None:
    raised_once(tmp_path)

    for name in (AUTHORITY_STATE_FILENAME, LOCK_FILENAME):
        mode = stat.S_IMODE(os.stat(authority_root(tmp_path) / name).st_mode)
        assert mode == 0o660, f"{name}: {mode:04o}"


def test_an_existing_lock_keeps_its_mode(tmp_path: Path) -> None:
    """他人の lock の mode を変えようとして `EPERM` で止まらないよう、既にある lock は触らない。"""
    root = authority_root(tmp_path)
    root.mkdir()
    lock = root / LOCK_FILENAME
    lock.touch()
    os.chmod(lock, 0o600)

    store(tmp_path).lower_stage(to_stage=AuthorityStage.SHADOW, actor="uid.1000", reason="x")

    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600


def test_the_cli_store_does_not_create_the_directory(tmp_path: Path) -> None:
    """CLI が作ると、人の uid が所有する fand の読めないディレクトリができる（0086 §2.4）。"""
    authority = store_as(tmp_path, APPROVER_IDENTITY, require_shared_root=True)

    with pytest.raises(AuthorityStoreError, match="ディレクトリが無い"):
        authority.rollback_to_baseline(actor=APPROVER, reason="x")
    document = report_document()
    with pytest.raises(AuthorityStoreError, match="ディレクトリが無い"):
        raise_stage(authority, approval=approval_for(document), document=document)

    assert not authority_root(tmp_path).exists()


@pytest.mark.parametrize(
    ("mode", "message"),
    [(0o0770, "setgid が無い"), (0o2775, "other の権限"), (0o2707, "other の権限")],
)
def test_the_cli_store_refuses_a_misconfigured_directory_before_writing(
    tmp_path: Path, mode: int, message: str
) -> None:
    root = shared_root(tmp_path, mode)
    document = report_document()

    with pytest.raises(AuthorityStoreError, match=message):
        raise_stage(
            store_as(tmp_path, APPROVER_IDENTITY, require_shared_root=True),
            approval=approval_for(document),
            document=document,
        )

    assert sorted(path.name for path in root.iterdir()) == [], "lock も journal も作らない"
