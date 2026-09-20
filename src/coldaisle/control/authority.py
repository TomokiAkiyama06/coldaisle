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
from contextlib import ExitStack, contextmanager, suppress
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_UN, flock
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldaisle.clock import Clock, WallClock
from coldaisle.control.config import ControlConfig, FanPolicyConfig
from coldaisle.control.evaluation.model import (
    AppliedArm,
    CounterfactualArm,
    EvaluationReport,
    GateOutcome,
    GateResult,
    GroupKind,
    SegmentRole,
)
from coldaisle.control.model_registry import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactStatus,
    ModelRegistry,
    ModelRegistryError,
    RegistrySnapshot,
)
from coldaisle.control.schema import (
    BASELINE_STAGE,
    STAGE_ORDER,
    AuthorityStage,
    AuthorityStageSource,
    ConfidenceLevel,
    ControllerKind,
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


class _ArmEvidence(_Frozen):
    """1つの arm が holdout に現れた事実と、**その arm が現れた最後の時刻**。"""

    arm: AppliedArm | CounterfactualArm
    last_ts_ms: int = Field(ge=0)
    """**その arm 自身の最後の tick の時刻**（`AppliedArmReport` / `CounterfactualArmReport`）。

    segment の `end_ms` ではない。同じ segment の続きを Fallback だけで回しても
    segment は伸びるので、そちらで測ると古い Learned MPC の実績が新鮮に見える
    （codex #4056942799）。
    """


def _holdout_arms(report: EvaluationReport) -> dict[str, _ArmEvidence]:
    """holdout の `overall` group に**実際に現れた** arm を、鍵から引けるようにする。

    gate の行は `arm_key` という文字列しか持たない。文字列だけで照合すると、
    「どの制御器の実績か」を確かめないまま合格を読むことになる（codex #4056864033）。
    報告の中の arm object へ結び直してから判断する。

    **その arm 自身の最後の tick の時刻も持つ。** 証拠の新しさは報告全体の run でも
    segment の終わりでもなく、**その arm が最後に動いた時刻**で測る
    （codex #4056903573 / #4056942799）。報告全体や segment で測ると、Fallback だけで
    回した続きを足すだけで、古い Learned MPC の実績が「新鮮」に見えてしまう。
    """
    arms: dict[str, _ArmEvidence] = {}

    def observe(key: str, arm: AppliedArm | CounterfactualArm, last_ts_ms: int) -> None:
        current = arms.get(key)
        if current is None or last_ts_ms > current.last_ts_ms:
            arms[key] = _ArmEvidence(arm=arm, last_ts_ms=last_ts_ms)

    for segment in report.segments:
        if segment.role is not SegmentRole.HOLDOUT:
            continue
        for group in segment.groups:
            if group.kind is not GroupKind.OVERALL:
                continue
            for applied in group.applied:
                observe(applied.arm_key, applied.arm, applied.last_ts_ms)
            for counterfactual in group.counterfactual:
                observe(counterfactual.arm_key, counterfactual.arm, counterfactual.last_ts_ms)
    return arms


def _check_learned_arms(report: EvaluationReport, approval: StageApproval) -> _ArmEvidence:
    """昇格の根拠を **Learned MPC の arm** に束縛する（0057 §2.4）。

    `arm_key` の一致だけで gate を読むと、承認者が**適用された Fallback の arm**を
    名指すだけで、肝心の Learned MPC の counterfactual arm が `blocked` のまま昇格できる。
    次のすべてを確かめる。

    - 名指した arm が holdout の報告に実在し、その制御器が Learned MPC である
    - その arm の stage が、いま上げようとしている遷移元と同じである
    - **報告に現れた Learned MPC の arm すべて**に gate があり、すべて `pass` である
      （良い arm だけを選んで、落ちた構成を残したまま上げられないようにする）

    返すのは名指した arm の実績で、呼び出し側が**その arm の新しさ**を測るのに使う。
    """
    arms = _holdout_arms(report)
    if not arms:
        raise AuthorityEvidenceError("holdout の arm 実績が無い報告では昇格できない")
    named = arms.get(approval.evidence.arm_key)
    if named is None:
        raise AuthorityEvidenceError(
            f"名指した arm が holdout の報告に無い（arm={approval.evidence.arm_key}）"
        )
    if named.arm.controller is not ControllerKind.LEARNED_MPC:
        controller = named.arm.controller
        raise AuthorityEvidenceError(
            "Learned MPC 以外の arm を昇格の根拠にできない"
            f"（arm={approval.evidence.arm_key}; "
            f"controller={'none' if controller is None else controller.value}）"
        )
    if named.arm.authority_stage is not approval.from_stage:
        raise AuthorityEvidenceError(
            "名指した arm の authority stage が、いまの stage と違う"
            f"（arm={named.arm.authority_stage.value}; now={approval.from_stage.value}）"
        )
    outcomes: dict[str, list[GateResult]] = {}
    for gate in report.gates:
        outcomes.setdefault(gate.arm_key, []).append(gate)
    for key, evidence in sorted(arms.items()):
        if evidence.arm.controller is not ControllerKind.LEARNED_MPC:
            continue
        results = outcomes.get(key, [])
        if not results:
            # 判定していないことを合格にしない（0054 の fail closed と同じ向き）。
            raise AuthorityEvidenceError(f"gate の判定が無い（arm={key}）")
        blocked = tuple(item for item in results if item.outcome is not GateOutcome.PASS)
        if blocked:
            stages_text = ",".join(
                item.blocking_stage.value for item in blocked if item.blocking_stage is not None
            )
            raise AuthorityEvidenceError(
                f"rollout gate を通っていない（arm={key}; blocked={stages_text}）"
            )
    return named


class AuthorityStore:
    """journal を1つの JSON file として持つ、排他・原子置換つきの保管庫。

    Model Registry（#104）と**別の file** に置く。同じ file に置くと、artifact の
    promotion と authority の変更が1つの revision を共有し、片方の操作がもう片方の
    状態を動かせてしまう（0057 §2.3）。
    """

    __slots__ = ("_clock", "_root")

    def __init__(self, root: Path, clock: Clock | None = None) -> None:
        """**時刻は store が持つ時計から取る。**

        操作のたびに `now_ms` を受け取ると、期限の判定に使う「いま」を呼び出し側が
        決められる（承認の期限も証拠の新しさも、渡す値ひとつで外せる）。
        """
        if not root.is_absolute():
            raise AuthorityStoreError("authority store の root は絶対 path にする")
        self._root = root
        self._clock = clock if clock is not None else WallClock()

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
        config: ControlConfig,
        registry: ModelRegistry,
        artifact_kind: ArtifactKind = ArtifactKind.THERMAL_MODEL,
    ) -> AuthorityJournal:
        """人の承認と rollout gate の証拠を検証してから、stage を1段上げる。

        **判定できないことを合格にしない。** 欠けている・古い・別の対象を指す証拠は
        すべて拒む（0054 の gate と同じ向き。0057 §2.4）。

        **承認者が値を持ち込む余地を残さない**（codex #4056903570）。設定の checksum は
        検証済みの `ControlConfig` から、artifact の identity は Model Registry の
        **いまの production pointer** から取る。

        **Registry の lock を、authority の commit が終わるまで握る**
        （codex #4056942797 / #4056968492）。発行済みの attestation は「発行した時点で
        production だった」としか言わないし、`inspect()` は戻るときに lock を手放すので、
        読んでから書くまでの間に別 process が B へ promote できてしまう。
        **lock は必ず Registry → Authority の順**で取る（逆順で取る経路は無いので、
        この順序は全順序であり deadlock しない）。読むのは #104 の state で、
        **書くことはない**（境界は 0057 §2.3）。

        **降格は Registry の lock を取らない。** registry が壊れていても、使えなくても、
        安全側（stage を下げる）へは常に動ける。
        """
        policy = config.policy
        now_ms = self._clock.now_ms()
        if now_ms < 0:
            raise AuthorityStoreError("authority の時刻は負にできない")
        self._check_approval_freshness(approval, policy, now_ms)
        if stage_rank(approval.to_stage) > stage_rank(policy.authority_stage):
            raise AuthorityApprovalError(
                "設定が許す上限を超える stage は承認できない"
                f"（ceiling={policy.authority_stage.value}; to={approval.to_stage.value}）"
            )
        # **Registry を先に pin する。** この with を抜けるまで production は動かない。
        with self._pinned_production(registry, artifact_kind) as pinned:
            production, registry_revision = pinned
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
                self._check_production(approval, production)
                self._check_evidence(
                    approval,
                    evaluation_report,
                    policy=policy,
                    fan_policy_config_sha256=config.sources.policy.sha256,
                    safety_config_sha256=config.sources.safety.sha256,
                    production_artifact_sha256=production.sha256,
                    now_ms=now_ms,
                )
                # **lock を握ったまま読み直す。** 値が動いていたら lock が効いていない
                # ということなので、昇格を書かずに止める。
                current, current_revision = self._read_production(registry, artifact_kind)
                if current_revision != registry_revision or current.sha256 != production.sha256:
                    raise AuthorityEvidenceError(
                        "検証中に Model Registry の production が動いた（やり直す）"
                        f"（revision={registry_revision}→{current_revision}）"
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
        trigger: AuthorityTrigger = AuthorityTrigger.AUTOMATIC,
        cause: AutomaticCause | None = None,
    ) -> AuthorityJournal:
        """stage を下げる。**承認を要求しない**（0057 §2.6）。

        すでに `to_stage` 以下なら何もせず、いまの journal をそのまま返す。
        """
        now_ms = self._clock.now_ms()
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

    def rollback_to_baseline(self, *, actor: str, reason: str) -> AuthorityJournal:
        """1手で Baseline（Shadow）へ戻す。**承認も段階も経由しない。**"""
        return self.lower_stage(
            to_stage=BASELINE_STAGE,
            actor=actor,
            reason=reason,
            trigger=AuthorityTrigger.HUMAN,
        )

    @contextmanager
    def _pinned_production(
        self, registry: ModelRegistry, artifact_kind: ArtifactKind
    ) -> Iterator[tuple[ArtifactMetadata, int]]:
        """Registry の lock を握ったまま、いまの production を返す。

        **lock の順序は Registry → Authority で固定する。** この with の中でだけ
        authority の lock を取り、逆順で取る経路はどこにも無いので deadlock しない。
        降格（`lower_stage`）はこの経路を通らないため、registry が使えなくても
        安全側へは常に動ける。
        """
        with ExitStack() as pin:
            try:
                snapshot = pin.enter_context(registry.pinned())
                production = self._production_of(snapshot, artifact_kind)
            except (OSError, ValueError, ModelRegistryError) as error:
                # Registry を読めないことを「production が無い」と読み替えない。止める。
                raise AuthorityEvidenceError("Model Registry の状態を読めない") from error
            # **yield を try の外に置く。** 中に入れると、authority 側の OSError まで
            # 「Registry を読めない」として報告してしまう（codex #4056992240）。
            # どの部品が落ちたのかを、その部品の理由で残す。
            yield production

    @staticmethod
    def _read_production(
        registry: ModelRegistry, artifact_kind: ArtifactKind
    ) -> tuple[ArtifactMetadata, int]:
        """lock を握ったまま読み直すための、lock を取らない読み取り。"""
        try:
            snapshot = registry.inspect()
        except (OSError, ValueError, ModelRegistryError) as error:
            raise AuthorityEvidenceError("Model Registry の状態を読めない") from error
        return AuthorityStore._production_of(snapshot, artifact_kind)

    @staticmethod
    def _production_of(
        snapshot: RegistrySnapshot, artifact_kind: ArtifactKind
    ) -> tuple[ArtifactMetadata, int]:
        """production pointer が指している artifact の metadata と registry revision。

        #92 は #104 の state を**読むだけ**である（書き込む経路を持たない。0057 §2.3）。
        読む向きは Issue #92 が引いた境界そのもので、「#104 がどの artifact を Production に
        するか決め、#92 がその Production artifact へどこまで制御権を渡すか決める」に従う。
        """
        slot = snapshot.production.get(artifact_kind)
        if slot is None:
            raise AuthorityEvidenceError(
                f"Production の artifact が無い（kind={artifact_kind.value}）"
            )
        record = snapshot.artifacts.get(slot.active.key)
        if record is None or record.status is not ArtifactStatus.PRODUCTION:
            raise AuthorityEvidenceError("Production pointer が production artifact を指していない")
        return record.metadata, snapshot.revision

    @staticmethod
    def _check_production(approval: StageApproval, production: ArtifactMetadata) -> None:
        """**いま Production である artifact そのもの**へ制御権を渡すことを確かめる。"""
        if approval.to_stage not in production.authority_compatibility:
            # Registry が許していない stage を、authority の側から与えない（#104 の境界）。
            raise AuthorityApprovalError(
                "Registry が許していない stage へ上げようとしている"
                f"（to={approval.to_stage.value}; "
                f"compatibility={[stage.value for stage in production.authority_compatibility]}）"
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
        named = _check_learned_arms(report, approval)
        # **新しさは「その arm が最後に動いた時刻」で測る**（codex #4056903573 / #4056942799）。
        # 報告全体の run でも segment の終わりでもない。どちらも、Fallback だけで回した
        # 続きを足すだけで、古い Learned MPC の実績を「新鮮」にできてしまう。
        end_ms = named.last_ts_ms
        if evidence.evidence_end_ms != end_ms:
            raise AuthorityEvidenceError(
                "証拠の最終観測時刻が、名指した arm の実績と違う"
                f"（declared={evidence.evidence_end_ms}; arm={end_ms}）"
            )
        if end_ms > now_ms:
            raise AuthorityEvidenceError("未来の観測を証拠にできない")
        age_ms = now_ms - end_ms
        limit_ms = policy.authority_rollout.evidence_max_age_ms.value
        if age_ms > limit_ms:
            raise AuthorityEvidenceError(f"証拠が古い（age_ms={age_ms}; max_ms={limit_ms}）")

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
        "_demotion_consumed",
        "_journal",
        "_last_mono_ms",
        "_low_confidence",
        "_ood",
        "_persist_failure",
        "_policy",
        "_store",
        "_unpersisted_ceiling",
    )

    def __init__(self, store: AuthorityStore, policy: FanPolicyConfig) -> None:
        """**時計は持たない。** 記録の時刻は store が自分の時計で決める。"""
        self._store = store
        self._policy = policy
        self._journal = store.read()
        # **書き残せなかった降格だけ**を memory 上の上限として持つ（0057 §2.6）。
        # 書けた降格は journal がそのまま表しているので、二重に持たない。持つと、
        # あとから承認された昇格が再起動まで効かなくなる（codex #4056968495）。
        self._unpersisted_ceiling = AuthorityStage.FULL
        # Gate の降格推奨を、立ち下がるまで1回だけ消費するための記憶。
        self._demotion_consumed = False
        self._last_mono_ms: int | None = None
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
        return lowest_stage(self._journal.stage, self.configured_ceiling, self._unpersisted_ceiling)

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
        if self._last_mono_ms is not None and now_mono_ms < self._last_mono_ms:
            # 巻き戻った時刻を受け取ると、窓の外へ出たはずの不健全が数え直されてしまう。
            raise ValueError("Authority Runtime の単調時計は巻き戻せない")
        self._last_mono_ms = now_mono_ms
        self._expire(now_mono_ms)
        if confidence_level is ConfidenceLevel.LOW:
            self._low_confidence.append(now_mono_ms)
        if ood:
            self._ood.append(now_mono_ms)
        # **推奨は「立ち上がり」だけを消費する**（codex #4056903574）。Gate の推奨は
        # 自分の窓（`demote_window_ms`）の間ずっと立ったままなので、毎 tick 消費すると
        # 1回の閾値超えが FULL → EXPANDED → LIMITED → SHADOW と連鎖する。
        rising_edge = demotion_recommended and not self._demotion_consumed
        self._demotion_consumed = demotion_recommended

        stage = self.current_stage()
        if stage is BASELINE_STAGE:
            # すでに Baseline。下げ先が無いので数えた履歴も持たない。
            self._low_confidence.clear()
            self._ood.clear()
            return None

        rollout = self._policy.authority_rollout
        if safety_state is SafetyState.EMERGENCY:
            return self._demote(stage, BASELINE_STAGE, AutomaticCause.SAFETY_EMERGENCY, "")
        if rising_edge:
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
        metadata["authority_unpersisted_ceiling"] = (
            None
            if self._unpersisted_ceiling is AuthorityStage.FULL
            else self._unpersisted_ceiling.value
        )
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
        # **先に下げる。** 安全側の変更を、disk にも他 process の lock にも待たせない
        # （codex #4056992239）。`lower_stage()` は flock と fsync を待つので、ここを
        # 例外の枝に置くと、待っている間ずっと前の stage のまま回ってしまう。
        # 例外の種類にも依存させない（`BaseException` で抜けても下がったままにする）。
        self._unpersisted_ceiling = lowest_stage(self._unpersisted_ceiling, to_stage)
        self._low_confidence.clear()
        self._ood.clear()
        persist_failure: Reason | None = None
        try:
            self._journal = self._store.lower_stage(
                to_stage=to_stage,
                actor=actor,
                reason=f"{code}: {detail}" if detail else code,
                trigger=trigger,
                cause=cause,
            )
        except (AuthorityError, OSError) as error:
            # 書き残せなかった。上の上限をそのまま持ち続ける（`reload()` でも外れない）。
            persist_failure = Reason(code="authority_persist_failed", detail=str(error)[:500])
            self._persist_failure = persist_failure
        else:
            self._persist_failure = None
            # 書けた降格は journal が表すので、memory 上の上限は手放す。持ち続けると、
            # あとから承認された昇格が再起動まで効かない（codex #4056968495）。
            # `to_stage` は必ずいまの上限以下なので、手放しても authority は上がらない。
            self._unpersisted_ceiling = AuthorityStage.FULL
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
