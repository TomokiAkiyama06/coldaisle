"""Authority Rollout（#92 / 決定記録 0057）。

**扱うのは「制御権の大きさ」だけである。** どの artifact を Production にするかは
Model Registry（#104）が決め、この module は一切関与しない。Production の入れ替えで
authority は動かない（0057 §2.3。試験で確かめる）。

この module が持つ不変条件は7つある。

1. 既定は Shadow。journal が無ければ Shadow から始まる
2. **stage を上げられるのは人の承認だけ。** 承認には承認者・理由・時刻が要る
3. 承認は使い回せない。revision・遷移・設定・証拠・artifact に束縛する
4. 昇格は1段ずつ。Shadow から Full へ飛ばない
5. **降格に承認は要らない。** Baseline（Shadow）への rollback は常に1手で行える
6. 設定（#103 の `authority_stage`）は**上限**として働く。実効 stage がこれを超えない
7. stage は model version と独立に decision trace へ残る

**Fan へ届く経路を持たない。** demand も PWM も作らず、Reactive Guard / Critical Safety を
import しない。出せるのは「いまの stage」までで、Critical Safety は全 stage で同一である
（AGENTS.md ルール2 / 3、0028 §2.4）。
"""

from __future__ import annotations

import os
import secrets
import stat
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_UN, flock
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldaisle.clock import Clock
from coldaisle.control.config import FanPolicyConfig
from coldaisle.control.evaluation.model import EvaluationReport, GateOutcome
from coldaisle.control.schema import (
    BASELINE_STAGE,
    STAGE_ORDER,
    AuthorityStage,
    AuthorityStageSource,
    ConfidenceLevel,
    Reason,
    SafetyState,
    Sha256Hex,
    StaticAuthorityStage,
    lowest_stage,
    stage_above,
    stage_below,
    stage_rank,
)

__all__ = [
    "AUTHORITY_JOURNAL_SCHEMA_VERSION",
    "AUTHORITY_STATE_FILENAME",
    "BASELINE_STAGE",
    "MAX_JOURNAL_EVENTS",
    "STAGE_ORDER",
    "AuthorityApprovalError",
    "AuthorityChangeKind",
    "AuthorityDemotion",
    "AuthorityError",
    "AuthorityEvent",
    "AuthorityEvidenceError",
    "AuthorityJournal",
    "AuthorityRuntime",
    "AuthorityStage",
    "AuthorityStageSource",
    "AuthorityStateError",
    "AuthorityStore",
    "AuthorityStoreError",
    "AuthorityTrigger",
    "AutomaticCause",
    "RolloutEvidence",
    "StageApproval",
    "StaticAuthorityStage",
    "lowest_stage",
    "stage_above",
    "stage_below",
    "stage_rank",
]

AUTHORITY_JOURNAL_SCHEMA_VERSION: Literal[1] = 1
"""journal 1つの形の版。**欄の意味を変えたら上げる。**"""

AUTHORITY_STATE_FILENAME = "authority.json"
_LOCK_FILENAME = ".authority.lock"
_MAX_JOURNAL_BYTES = 4 * 1024 * 1024
"""journal の読み書きに許す大きさ。資源の境界であり、調整値ではない。"""

MAX_JOURNAL_EVENTS = 4_096
"""1つの journal に残す変更の件数。超えたら**昇格を拒む**（黙って古い記録を捨てない）。"""

_ACTOR_PATTERN = r"^[a-z][a-z0-9_.-]*$"


class AuthorityError(Exception):
    """Authority Rollout の失敗の基底。"""


class AuthorityApprovalError(AuthorityError):
    """承認が無い・束縛できない・期限切れである。**昇格は通さない。**"""


class AuthorityEvidenceError(AuthorityError):
    """rollout gate の証拠が欠けている・古い・別の対象のものである。"""


class AuthorityStateError(AuthorityError):
    """journal を読めない、または現在の stage を再現できない。"""


class AuthorityStoreError(AuthorityError):
    """journal を安全に読み書きできない（path・権限・I/O）。"""


class AuthorityChangeKind(StrEnum):
    """journal に残る変更の向き。"""

    RAISED = "raised"
    LOWERED = "lowered"


class AuthorityTrigger(StrEnum):
    """変更を起こした主体。"""

    HUMAN = "human"
    AUTOMATIC = "automatic"


class AutomaticCause(StrEnum):
    """系が自分で下げた理由（0057 §2.6）。**上げる理由は無い。**"""

    REPEATED_FALLBACK = "repeated_fallback"
    """Gate の降格推奨（#79 の `demote_window_ms` / `demote_after`）。"""
    PERSISTENT_LOW_CONFIDENCE = "persistent_low_confidence"
    PERSISTENT_OOD = "persistent_ood"
    SAFETY_EMERGENCY = "safety_emergency"
    """Critical Safety が `EMERGENCY` を出した。Baseline へ戻す。"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RolloutEvidence(_Frozen):
    """昇格の根拠にした Offline Evaluation（#91 / 決定記録 0054）の**束縛**。

    数値を写さない。写した数値は元の報告と食い違いうるので、**識別子だけ**を持ち、
    判定は報告そのものを読み直して行う（0057 §2.4）。
    """

    report_sha256: Sha256Hex
    """承認が指した報告の bytes の digest。"""
    conditions_sha256: Sha256Hex
    """報告の `provenance.conditions_sha256`。**同じなら同じ条件で比べている**（0054 §2.7）。"""
    arm_key: str = Field(min_length=1, max_length=200)
    """gate を見る比較対象。"""
    artifact_sha256: Sha256Hex
    """この証拠が語っている model artifact。Production の artifact と一致させる。"""
    evidence_end_ms: int = Field(ge=0)
    """証拠の最後の観測時刻（報告の run の `end_ms` の最大）。**新しさはここで測る。**"""
    fan_policy_config_sha256: Sha256Hex
    safety_config_sha256: Sha256Hex
    """証拠を取ったときの設定。いま動いている設定と一致しなければ使わない。"""


class StageApproval(_Frozen):
    """stage を**上げる**ための人の承認。

    **下げるための承認は無い。** 型を作らないことで、「降格にも承認が要る」実装を
    書けないようにする（0057 §2.6）。
    """

    decision: Literal["approved"] = "approved"
    from_stage: AuthorityStage
    to_stage: AuthorityStage
    expected_revision: int = Field(ge=0)
    """承認した時点の journal revision。**この直後にだけ使える**（使い回しの封じ）。"""
    approver: str = Field(pattern=_ACTOR_PATTERN, max_length=120)
    approved_at_ms: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)
    evidence: RolloutEvidence

    @model_validator(mode="after")
    def _raises_exactly_one_stage(self) -> Self:
        if stage_above(self.from_stage) is not self.to_stage:
            raise ValueError("昇格は1段ずつにする（0057 §2.5）")
        return self


class AuthorityEvent(_Frozen):
    """journal に残る1つの変更。**理由と時刻を必ず持つ。**"""

    revision: int = Field(ge=1)
    occurred_at_ms: int = Field(ge=0)
    kind: AuthorityChangeKind
    trigger: AuthorityTrigger
    from_stage: AuthorityStage
    to_stage: AuthorityStage
    actor: str = Field(pattern=_ACTOR_PATTERN, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    cause: AutomaticCause | None = None
    approval: StageApproval | None = None

    @model_validator(mode="after")
    def _only_a_human_approval_raises_the_stage(self) -> Self:
        if self.from_stage is self.to_stage:
            raise ValueError("stage が変わらない event は残さない")
        raised = stage_rank(self.to_stage) > stage_rank(self.from_stage)
        if raised != (self.kind is AuthorityChangeKind.RAISED):
            raise ValueError("kind と stage の向きが食い違っている")
        if self.kind is AuthorityChangeKind.RAISED:
            if self.trigger is not AuthorityTrigger.HUMAN:
                raise ValueError("stage を上げられるのは人だけ（0057 §2.3）")
            if self.approval is None:
                raise ValueError("昇格には承認が要る")
            if self.cause is not None:
                raise ValueError("昇格に自動降格の理由を付けない")
            approval = self.approval
            if (approval.from_stage, approval.to_stage) != (self.from_stage, self.to_stage):
                raise ValueError("承認と event の遷移を一致させる")
            if approval.expected_revision + 1 != self.revision:
                raise ValueError("承認は対象 revision の直後にだけ使える")
            if approval.approver != self.actor or approval.reason != self.reason:
                raise ValueError("承認と event の actor / reason を一致させる")
            if approval.approved_at_ms > self.occurred_at_ms:
                raise ValueError("未来の承認は記録できない")
            return self
        if self.approval is not None:
            raise ValueError("降格に承認を付けない（0057 §2.6）")
        if (self.trigger is AuthorityTrigger.AUTOMATIC) != (self.cause is not None):
            raise ValueError("自動降格だけが cause を持つ")
        return self


class AuthorityJournal(_Frozen):
    """いまの stage と、そこへ至った変更のすべて。

    **stage は event から再現できなければならない。** 再現できない journal は読まない。
    """

    schema_version: Literal[1] = AUTHORITY_JOURNAL_SCHEMA_VERSION
    revision: int = Field(ge=0)
    stage: AuthorityStage = BASELINE_STAGE
    events: tuple[AuthorityEvent, ...] = ()

    @model_validator(mode="after")
    def _events_replay_to_the_current_stage(self) -> Self:
        if len(self.events) != self.revision:
            raise ValueError("revision と event 数が一致しない")
        if self.revision > MAX_JOURNAL_EVENTS:
            raise ValueError("journal の event 数が上限を超えている")
        stage = BASELINE_STAGE
        previous_ms = 0
        for index, event in enumerate(self.events, start=1):
            if event.revision != index:
                raise ValueError("event revision が連続していない")
            if event.from_stage is not stage:
                raise ValueError("event の遷移元が直前の stage と一致しない")
            if event.occurred_at_ms < previous_ms:
                raise ValueError("event の時刻が巻き戻っている")
            stage = event.to_stage
            previous_ms = event.occurred_at_ms
        if stage is not self.stage:
            raise ValueError("event から現在の stage を再現できない")
        return self

    @property
    def last_change(self) -> AuthorityEvent | None:
        """直近の変更。まだ一度も変えていなければ None。"""
        return self.events[-1] if self.events else None

    def trace_metadata(self) -> dict[str, object]:
        """#82 の decision trace へ足せる値。**model version を含めない**（0057 §2.7）。"""
        last = self.last_change
        return {
            "authority_stage": self.stage.value,
            "authority_revision": self.revision,
            "authority_last_change": (
                None
                if last is None
                else {
                    "kind": last.kind.value,
                    "trigger": last.trigger.value,
                    "from_stage": last.from_stage.value,
                    "to_stage": last.to_stage.value,
                    "actor": last.actor,
                    "reason": last.reason,
                    "cause": None if last.cause is None else last.cause.value,
                    "occurred_at_ms": last.occurred_at_ms,
                }
            ),
        }


class AuthorityStore:
    """journal を1つの JSON file として持つ、排他・原子置換つきの保管庫。

    Model Registry（#104）と**別の file** に置く。同じ file に置くと、artifact の
    promotion と authority の変更が1つの revision を共有し、片方の操作がもう片方の
    状態を動かせてしまう（0057 §2.3）。
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise AuthorityStoreError("authority store の root は絶対 path にする")
        self._root = root

    def read(self) -> AuthorityJournal:
        """いまの journal。file が無ければ Baseline から始まったものとして返す。"""
        with self._open_root(create=False) as root_fd:
            if root_fd is None:
                return AuthorityJournal(revision=0)
            return self._read(root_fd)

    def raise_stage(
        self,
        *,
        approval: StageApproval,
        evaluation_report: bytes,
        policy: FanPolicyConfig,
        fan_policy_config_sha256: str,
        safety_config_sha256: str,
        production_artifact_sha256: str,
        now_ms: int,
    ) -> AuthorityJournal:
        """人の承認と rollout gate の証拠を検証してから、stage を1段上げる。

        **判定できないことを合格にしない。** 欠けている・古い・別の対象を指す証拠は
        すべて拒む（0054 の gate と同じ向き。0057 §2.4）。
        """
        if now_ms < 0:
            raise AuthorityStoreError("authority の時刻は負にできない")
        self._check_approval_freshness(approval, policy, now_ms)
        if stage_rank(approval.to_stage) > stage_rank(policy.authority_stage):
            raise AuthorityApprovalError(
                "設定が許す上限を超える stage は承認できない"
                f"（ceiling={policy.authority_stage.value}; to={approval.to_stage.value}）"
            )
        self._check_evidence(
            approval,
            evaluation_report,
            policy=policy,
            fan_policy_config_sha256=fan_policy_config_sha256,
            safety_config_sha256=safety_config_sha256,
            production_artifact_sha256=production_artifact_sha256,
            now_ms=now_ms,
        )
        with self._exclusive_lock() as root_fd:
            journal = self._read(root_fd)
            if approval.expected_revision != journal.revision:
                raise AuthorityApprovalError(
                    "承認した時点の revision と一致しない（承認は使い回せない）"
                    f"（approved_for={approval.expected_revision}; now={journal.revision}）"
                )
            if approval.from_stage is not journal.stage:
                raise AuthorityApprovalError(
                    "承認した遷移元といまの stage が違う"
                    f"（approved_from={approval.from_stage.value}; now={journal.stage.value}）"
                )
            event = AuthorityEvent(
                revision=journal.revision + 1,
                occurred_at_ms=max(now_ms, self._last_ms(journal)),
                kind=AuthorityChangeKind.RAISED,
                trigger=AuthorityTrigger.HUMAN,
                from_stage=journal.stage,
                to_stage=approval.to_stage,
                actor=approval.approver,
                reason=approval.reason,
                approval=approval,
            )
            return self._append(root_fd, journal, event)

    def lower_stage(
        self,
        *,
        to_stage: AuthorityStage,
        actor: str,
        reason: str,
        now_ms: int,
        trigger: AuthorityTrigger = AuthorityTrigger.AUTOMATIC,
        cause: AutomaticCause | None = None,
    ) -> AuthorityJournal:
        """stage を下げる。**承認を要求しない**（0057 §2.6）。

        すでに `to_stage` 以下なら何もせず、いまの journal をそのまま返す。
        """
        if now_ms < 0:
            raise AuthorityStoreError("authority の時刻は負にできない")
        with self._exclusive_lock() as root_fd:
            journal = self._read(root_fd)
            if stage_rank(to_stage) >= stage_rank(journal.stage):
                return journal
            event = AuthorityEvent(
                revision=journal.revision + 1,
                occurred_at_ms=max(now_ms, self._last_ms(journal)),
                kind=AuthorityChangeKind.LOWERED,
                trigger=trigger,
                from_stage=journal.stage,
                to_stage=to_stage,
                actor=actor,
                reason=reason,
                cause=cause,
            )
            return self._append(root_fd, journal, event)

    def rollback_to_baseline(self, *, actor: str, reason: str, now_ms: int) -> AuthorityJournal:
        """1手で Baseline（Shadow）へ戻す。**承認も段階も経由しない。**"""
        return self.lower_stage(
            to_stage=BASELINE_STAGE,
            actor=actor,
            reason=reason,
            now_ms=now_ms,
            trigger=AuthorityTrigger.HUMAN,
        )

    @staticmethod
    def _last_ms(journal: AuthorityJournal) -> int:
        """直前の変更の時刻。**壁時計が巻き戻っても降格を落とさないため**に使う。

        journal は時刻の巻き戻りを拒むので、巻き戻った壁時計をそのまま書くと
        `raise` ではなく**降格が書けなくなる**。安全側の壊れ方ではないので、
        直前の時刻まで引き上げて記録する（承認の期限は壁時計で別に見ている）。
        """
        last = journal.last_change
        return 0 if last is None else last.occurred_at_ms

    @staticmethod
    def _check_approval_freshness(
        approval: StageApproval, policy: FanPolicyConfig, now_ms: int
    ) -> None:
        if approval.approved_at_ms > now_ms:
            raise AuthorityApprovalError("未来の承認は使えない")
        age_ms = now_ms - approval.approved_at_ms
        limit_ms = policy.authority_rollout.approval_max_age_ms.value
        if age_ms > limit_ms:
            raise AuthorityApprovalError(f"承認が古い（age_ms={age_ms}; max_ms={limit_ms}）")

    @staticmethod
    def _check_evidence(
        approval: StageApproval,
        evaluation_report: bytes,
        *,
        policy: FanPolicyConfig,
        fan_policy_config_sha256: str,
        safety_config_sha256: str,
        production_artifact_sha256: str,
        now_ms: int,
    ) -> None:
        """報告そのものを読み直し、承認が指した証拠が**完全・新鮮・束縛済み**かを見る。"""
        evidence = approval.evidence
        if sha256(evaluation_report).hexdigest() != evidence.report_sha256:
            raise AuthorityEvidenceError("承認が指した報告と、渡された報告が違う")
        try:
            report = EvaluationReport.model_validate_json(evaluation_report)
        except ValidationError as error:
            raise AuthorityEvidenceError("Offline Evaluation の報告を検証できない") from error
        provenance = report.provenance
        if provenance.conditions_sha256 != evidence.conditions_sha256:
            raise AuthorityEvidenceError("報告の比較条件が承認と違う")
        if (
            evidence.fan_policy_config_sha256 != fan_policy_config_sha256
            or provenance.fan_policy_config_sha256 != fan_policy_config_sha256
        ):
            raise AuthorityEvidenceError("証拠がいまの fan-policy.yaml のものではない")
        if (
            evidence.safety_config_sha256 != safety_config_sha256
            or provenance.safety_config_sha256 != safety_config_sha256
        ):
            raise AuthorityEvidenceError("証拠がいまの safety.yaml のものではない")
        if evidence.artifact_sha256 != production_artifact_sha256:
            raise AuthorityEvidenceError("証拠が Production の artifact のものではない")
        artifacts = set(provenance.versions.model_artifacts)
        if artifacts != {production_artifact_sha256}:
            # 別の artifact が混ざった報告では、どの artifact の実績なのか言えない。
            raise AuthorityEvidenceError(
                "報告に Production 以外の artifact が含まれている（借りた証拠で上げない）"
            )
        stages = provenance.versions.authority_stages
        if not stages:
            raise AuthorityEvidenceError("証拠に authority stage の記録が無い")
        try:
            observed = tuple(AuthorityStage(value) for value in stages)
        except ValueError as error:
            raise AuthorityEvidenceError("証拠の authority stage を解釈できない") from error
        if any(stage_rank(stage) > stage_rank(approval.from_stage) for stage in observed):
            raise AuthorityEvidenceError("いまより高い authority で取った証拠は使えない")
        if approval.from_stage not in observed:
            raise AuthorityEvidenceError("いまの stage で運転した証拠が無い")
        end_ms = max(run.end_ms for run in provenance.runs)
        if evidence.evidence_end_ms != end_ms:
            raise AuthorityEvidenceError("証拠の最終観測時刻が報告と違う")
        if end_ms > now_ms:
            raise AuthorityEvidenceError("未来の観測を証拠にできない")
        age_ms = now_ms - end_ms
        limit_ms = policy.authority_rollout.evidence_max_age_ms.value
        if age_ms > limit_ms:
            raise AuthorityEvidenceError(f"証拠が古い（age_ms={age_ms}; max_ms={limit_ms}）")
        gates = tuple(gate for gate in report.gates if gate.arm_key == evidence.arm_key)
        if not gates:
            raise AuthorityEvidenceError(f"gate の判定が無い（arm={evidence.arm_key}）")
        blocked = tuple(gate for gate in gates if gate.outcome is not GateOutcome.PASS)
        if blocked:
            stages_text = ",".join(
                gate.blocking_stage.value for gate in blocked if gate.blocking_stage is not None
            )
            raise AuthorityEvidenceError(f"rollout gate を通っていない（blocked={stages_text}）")

    def _append(
        self, root_fd: int, journal: AuthorityJournal, event: AuthorityEvent
    ) -> AuthorityJournal:
        if journal.revision >= MAX_JOURNAL_EVENTS:
            raise AuthorityStateError("journal の event 数が上限に達している")
        updated = AuthorityJournal(
            revision=event.revision,
            stage=event.to_stage,
            events=(*journal.events, event),
        )
        payload = updated.model_dump_json(indent=2).encode("utf-8") + b"\n"
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise AuthorityStateError("journal が size 上限を超える")
        self._atomic_write(root_fd, AUTHORITY_STATE_FILENAME, payload)
        return updated

    def _read(self, root_fd: int) -> AuthorityJournal:
        try:
            payload = self._read_regular_file(root_fd, AUTHORITY_STATE_FILENAME)
        except OSError as error:
            raise AuthorityStoreError("authority journal を読めない") from error
        if payload is None:
            return AuthorityJournal(revision=0)
        try:
            return AuthorityJournal.model_validate_json(payload)
        except ValidationError as error:
            # 壊れた journal を「Baseline」と読み替えない。読み替えると、壊すだけで
            # 記録の無い状態へ移せてしまう。運用者が直すまで止める。
            raise AuthorityStateError("authority journal を検証できない") from error

    @contextmanager
    def _open_root(self, *, create: bool) -> Iterator[int | None]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            anchor_fd = os.open(self._root.anchor, flags)
        except OSError as error:
            raise AuthorityStoreError("authority store の anchor を開けない") from error
        try:
            try:
                root_fd = self._open_directory_chain(
                    anchor_fd, tuple(self._root.parts[1:]), create=create
                )
            except FileNotFoundError:
                if create:
                    raise AuthorityStoreError("authority store の root を作成できない") from None
                yield None
                return
            except OSError as error:
                raise AuthorityStoreError(
                    "authority store の component が symlink または directory ではない"
                ) from error
        finally:
            os.close(anchor_fd)
        try:
            yield root_fd
        finally:
            os.close(root_fd)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[int]:
        with self._open_root(create=True) as root_fd:
            assert root_fd is not None
            try:
                lock_fd = os.open(
                    _LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root_fd
                )
            except OSError as error:
                raise AuthorityStoreError("authority lock が symlink である") from error
            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise AuthorityStoreError("authority lock が regular file ではない")
                flock(lock_fd, LOCK_EX)
                try:
                    yield root_fd
                finally:
                    flock(lock_fd, LOCK_UN)
            finally:
                os.close(lock_fd)

    @staticmethod
    def _open_directory_chain(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
        current_fd = os.dup(root_fd)
        try:
            for part in parts:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                try:
                    child_fd = os.open(part, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=current_fd)
                    child_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = child_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    @staticmethod
    def _read_regular_file(directory_fd: int, name: str) -> bytes | None:
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise AuthorityStoreError(f"authority file が symlink である: {name}") from error
        try:
            file_status = os.fstat(file_fd)
            if not stat.S_ISREG(file_status.st_mode):
                raise AuthorityStoreError(f"authority file が regular file ではない: {name}")
            if file_status.st_size > _MAX_JOURNAL_BYTES:
                raise AuthorityStateError(f"authority file が size 上限を超えている: {name}")
            payload = bytearray()
            remaining = file_status.st_size
            while remaining:
                chunk = os.read(file_fd, remaining)
                if not chunk:
                    raise AuthorityStateError(f"authority file が読取中に短縮された: {name}")
                payload.extend(chunk)
                remaining -= len(chunk)
            return bytes(payload)
        finally:
            os.close(file_fd)

    @staticmethod
    def _atomic_write(directory_fd: int, name: str, payload: bytes) -> None:
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise AuthorityStoreError(f"authority の書込先が regular file ではない: {name}")
        temporary_name = f".{name}.{secrets.token_hex(12)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        try:
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(temporary_fd, remaining)
                    remaining = remaining[written:]
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
            os.replace(temporary_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)


class AuthorityDemotion(_Frozen):
    """自動または人の操作で下げた結果。**下げたことは永続化の成否に依らない。**"""

    from_stage: AuthorityStage
    to_stage: AuthorityStage
    cause: AutomaticCause | None
    reason: Reason
    persisted: bool
    persist_failure: Reason | None = None

    @model_validator(mode="after")
    def _failure_is_explained(self) -> Self:
        if self.persisted != (self.persist_failure is None):
            raise ValueError("永続化に失敗した降格だけが persist_failure を持つ")
        if stage_rank(self.to_stage) >= stage_rank(self.from_stage):
            raise ValueError("降格は stage を下げる")
        return self


class AuthorityRuntime:
    """いまの stage を制御ループへ渡し、不健全が続いたら**その場で**下げる。

    `current_stage()` は設定の上限と journal の stage の低いほうを返す。設定を下げれば
    次の tick から効き、設定を上げても journal は上がらない（0057 §2.2）。

    **上げる経路を持たない。** 昇格は `AuthorityStore.raise_stage()` だけが行い、
    この型は runtime から呼べる範囲に置かない（AGENTS.md ルール2 / 3）。
    """

    __slots__ = (
        "_applied_ceiling",
        "_clock",
        "_journal",
        "_low_confidence",
        "_ood",
        "_persist_failure",
        "_policy",
        "_store",
    )

    def __init__(self, store: AuthorityStore, policy: FanPolicyConfig, *, clock: Clock) -> None:
        self._store = store
        self._policy = policy
        self._clock = clock
        self._journal = store.read()
        # この process が下げた上限。**永続化できなくても保持する**（0057 §2.6）。
        self._applied_ceiling = AuthorityStage.FULL
        self._low_confidence: deque[int] = deque()
        self._ood: deque[int] = deque()
        self._persist_failure: Reason | None = None

    @property
    def configured_ceiling(self) -> AuthorityStage:
        """検証済み設定が許す上限（#103 の `authority_stage`）。"""
        return self._policy.authority_stage

    @property
    def journal(self) -> AuthorityJournal:
        """読み込んだ journal。"""
        return self._journal

    @property
    def persist_failure(self) -> Reason | None:
        """直近の降格を書き残せなかった理由。**運用者が見て直す。**"""
        return self._persist_failure

    def current_stage(self) -> AuthorityStage:
        """この tick に与えてよい制御権。**上限を超えることはない。**"""
        return lowest_stage(self._journal.stage, self.configured_ceiling, self._applied_ceiling)

    def reload(self) -> None:
        """外（管理操作）で変わった journal を読み直す。

        **この process が下げた上限は外れない。** 読み直しで authority が戻ると、
        書き残せなかった降格を「読み直すだけ」で取り消せてしまう。
        """
        self._journal = self._store.read()

    def observe(
        self,
        *,
        safety_state: SafetyState,
        demotion_recommended: bool = False,
        confidence_level: ConfidenceLevel | None = None,
        ood: bool | None = None,
        now_mono_ms: int,
    ) -> AuthorityDemotion | None:
        """この tick の健全性を見て、必要なら**承認を待たずに**下げる。

        返すのは下げたときだけ。`None` は「この tick では下げない」であって、
        「健全である」ではない。1 tick ごとの退避は Gate（#79 / #85）が別に行う。
        """
        if now_mono_ms < 0:
            raise ValueError("単調時計は負にできない")
        self._expire(now_mono_ms)
        if confidence_level is ConfidenceLevel.LOW:
            self._low_confidence.append(now_mono_ms)
        if ood:
            self._ood.append(now_mono_ms)

        stage = self.current_stage()
        if stage is BASELINE_STAGE:
            # すでに Baseline。下げ先が無いので数えた履歴も持たない。
            self._low_confidence.clear()
            self._ood.clear()
            return None

        rollout = self._policy.authority_rollout
        if safety_state is SafetyState.EMERGENCY:
            return self._demote(stage, BASELINE_STAGE, AutomaticCause.SAFETY_EMERGENCY, "")
        if demotion_recommended:
            return self._demote(
                stage,
                self._one_below(stage),
                AutomaticCause.REPEATED_FALLBACK,
                f"demote_after={self._policy.demote_after}",
            )
        if len(self._ood) >= rollout.ood_after.value:
            return self._demote(
                stage,
                self._one_below(stage),
                AutomaticCause.PERSISTENT_OOD,
                f"count={len(self._ood)}; required={rollout.ood_after.value}",
            )
        if len(self._low_confidence) >= rollout.low_confidence_after.value:
            return self._demote(
                stage,
                self._one_below(stage),
                AutomaticCause.PERSISTENT_LOW_CONFIDENCE,
                f"count={len(self._low_confidence)}; required={rollout.low_confidence_after.value}",
            )
        return None

    def rollback_to_baseline(self, *, actor: str, reason: str) -> AuthorityDemotion | None:
        """人の操作で即座に Baseline へ戻す。**承認を待たない。**"""
        stage = self.current_stage()
        if stage is BASELINE_STAGE:
            return None
        return self._demote(
            stage,
            BASELINE_STAGE,
            None,
            reason,
            actor=actor,
            trigger=AuthorityTrigger.HUMAN,
        )

    def trace_metadata(self) -> dict[str, object]:
        """#82 へ渡す stage の記録。**model version を含めない**（0057 §2.7）。"""
        metadata = self._journal.trace_metadata()
        metadata["authority_stage"] = self.current_stage().value
        metadata["authority_journal_stage"] = self._journal.stage.value
        metadata["authority_config_ceiling"] = self.configured_ceiling.value
        metadata["authority_persist_failure"] = (
            None if self._persist_failure is None else self._persist_failure.model_dump(mode="json")
        )
        return metadata

    @staticmethod
    def _one_below(stage: AuthorityStage) -> AuthorityStage:
        below = stage_below(stage)
        assert below is not None, "Baseline より下は呼び出し側で除いている"
        return below

    def _demote(
        self,
        from_stage: AuthorityStage,
        to_stage: AuthorityStage,
        cause: AutomaticCause | None,
        detail: str,
        *,
        actor: str = "control_runtime",
        trigger: AuthorityTrigger = AuthorityTrigger.AUTOMATIC,
    ) -> AuthorityDemotion:
        """先に memory 上で下げ、そのあとで書き残す。

        **書き残せなくても下げたままにする。** 永続化に失敗したら元へ戻す実装だと、
        disk が一杯な間ほど高い authority で回り続けることになる。

        ``from_stage`` は**実効 stage**（設定の上限を掛けたあと）で、journal に残る
        event の遷移元は journal の stage である。設定で上限を掛けている間は両者が
        食い違うが、journal は journal の履歴を、返り値はこの tick の制御権の変化を表す。
        """
        code = cause.value if cause is not None else "manual_rollback"
        reason = Reason(code=code, detail=detail[:500])
        # **先に下げる。** 書き込みの成否を待たない。
        self._applied_ceiling = lowest_stage(self._applied_ceiling, to_stage)
        self._low_confidence.clear()
        self._ood.clear()
        persist_failure: Reason | None = None
        try:
            self._journal = self._store.lower_stage(
                to_stage=to_stage,
                actor=actor,
                reason=f"{code}: {detail}" if detail else code,
                now_ms=self._clock.now_ms(),
                trigger=trigger,
                cause=cause,
            )
            self._persist_failure = None
        except (AuthorityError, OSError) as error:
            persist_failure = Reason(code="authority_persist_failed", detail=str(error)[:500])
            self._persist_failure = persist_failure
        return AuthorityDemotion(
            from_stage=from_stage,
            to_stage=to_stage,
            cause=cause,
            reason=reason,
            persisted=persist_failure is None,
            persist_failure=persist_failure,
        )

    def _expire(self, now_mono_ms: int) -> None:
        cutoff = now_mono_ms - self._policy.authority_rollout.unhealthy_window_ms.value
        for history in (self._low_confidence, self._ood):
            while history and history[0] < cutoff:
                history.popleft()
