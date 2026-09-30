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

import logging
import os
import re
import secrets
import stat
import time
from collections import deque
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldaisle import logs
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
    AuthorityRecord,
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
    "INVALID_LOWERING_ACTOR",
    "INVALID_LOWERING_REASON",
    "MAX_JOURNAL_EVENTS",
    "MIN_EVIDENCE_REPORT_SCHEMA_VERSION",
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
    "JournalSignature",
    "RolloutEvidence",
    "StageApproval",
    "StaticAuthorityStage",
    "lowest_stage",
    "stage_above",
    "stage_below",
    "stage_rank",
]

AUTHORITY_JOURNAL_SCHEMA_VERSION: Literal[3] = 3
"""journal 1つの形の版。**欄の意味を変えたら上げる。**

- v2（#81 / 決定記録 0073 §2.6）: 昇格 event の証拠（`RolloutEvidence`）に
  `air_balance_config_sha256` と `fan_hardware_config_sha256` を足した。v2 の journal に
  新しく書く昇格は両方を必ず持つ。v1 の event は持たないまま読む（過去の記録として読むだけで、
  新しい昇格の根拠にはならない）。既に残った event は書き換えない
- v3（#92 / 決定記録 0072 §2.6）: 自動降格の理由に `authority_journal_unreadable` を足した
  （走行中に journal が読めなかったので `SHADOW` へ下げた）。**この理由を持つ event は v3 の
  journal にだけ置ける。** v2 までの reader はこの値を知らないので、版を上げて「知らない
  journal」として拒ませる（黙って読み違えさせない）。v1 / v2 の journal はそのまま読み、
  次に書くときに v3 で書く（既に残った event は書き換えない）
"""

AUTHORITY_STATE_FILENAME = "authority.json"
_LOCK_FILENAME = ".authority.lock"
_MAX_JOURNAL_BYTES = 4 * 1024 * 1024
"""journal の読み書きに許す大きさ。資源の境界であり、調整値ではない。"""

MIN_EVIDENCE_REPORT_SCHEMA_VERSION = 3
"""昇格の証拠に使える Offline Evaluation 報告の最小 version（#159 / 決定記録 0059 §2.5）。

v1 は適用 arm の `model_artifacts` / `unbound_attested_ticks` を持たない。欄の無さが
「空・0」と読めてしまい、**artifact の完全性を言えない報告が「完全に束縛できた」ように
見える**（codex #4057527950）。読むことはできるが、昇格の根拠にはしない。

v2 は `air-balance.yaml` の設定と、消費した trace の設定 hash の突き合わせを言えない
（#81 / 決定記録 0073 §2.6）。同じ理由で昇格の根拠にしない。
"""

MAX_JOURNAL_EVENTS = 4_096
"""1つの journal に残す変更の件数。超えたら**昇格を拒む**（黙って古い記録を捨てない）。"""

_ACTOR_PATTERN = r"^[a-z][a-z0-9_.-]*$"
_ACTOR_MAX_CHARS = 120
_REASON_MAX_CHARS = 1000
"""journal の event の actor / reason の上限。資源の境界であり、調整値ではない。"""

_LOGGER = logging.getLogger("coldaisle.control")


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
    AUTHORITY_JOURNAL_UNREADABLE = "authority_journal_unreadable"
    """走行中に journal が読めない・壊れている。Baseline へ戻す（0072 §2.6。journal v3）。"""


_CAUSES_ADDED_IN_V3: frozenset[AutomaticCause] = frozenset(
    {AutomaticCause.AUTHORITY_JOURNAL_UNREADABLE}
)
"""journal v3 で足した自動降格の理由。v2 までの journal には置けない。"""


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
    air_balance_config_sha256: Sha256Hex | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    fan_hardware_config_sha256: Sha256Hex | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """証拠を取ったときの ``air-balance.yaml`` / ``fan-hardware.yaml``（決定記録 0073 §2.6）。

    Learned MPC の cost は Air Balance の曲線と目標帯、profile の ``minimum_stable_demand`` で
    変わる。別の characterization・別の profile で集めた証拠で昇格させない。
    journal v1 の event は持たない（過去の記録として読むだけ）。**新しい昇格には必須**で、
    ``raise_stage`` が欠けた承認を拒む。
    """

    @model_validator(mode="after")
    def _config_bindings_come_together(self) -> Self:
        if (self.air_balance_config_sha256 is None) != (self.fan_hardware_config_sha256 is None):
            raise ValueError(
                "air_balance_config_sha256 と fan_hardware_config_sha256 は一緒に記録する"
            )
        return self

    @property
    def binds_air_balance(self) -> bool:
        """``air-balance.yaml`` と ``fan-hardware.yaml`` に束縛された証拠か（journal v2）。"""
        return self.air_balance_config_sha256 is not None


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
    actor: str = Field(pattern=_ACTOR_PATTERN, max_length=_ACTOR_MAX_CHARS)
    reason: str = Field(min_length=1, max_length=_REASON_MAX_CHARS)
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

    schema_version: Literal[1, 2, 3] = AUTHORITY_JOURNAL_SCHEMA_VERSION
    revision: int = Field(ge=0)
    stage: AuthorityStage = BASELINE_STAGE
    events: tuple[AuthorityEvent, ...] = ()

    @model_validator(mode="after")
    def _events_replay_to_the_current_stage(self) -> Self:
        if len(self.events) != self.revision:
            raise ValueError("revision と event 数が一致しない")
        bound_seen = False
        for event in self.events:
            if event.cause in _CAUSES_ADDED_IN_V3 and self.schema_version < 3:
                raise ValueError(f"{event.cause} の降格を記録する journal は v3 にする")
            if event.approval is None:
                continue
            if event.approval.evidence.binds_air_balance:
                if self.schema_version < 2:
                    raise ValueError("設定に束縛した証拠を記録する journal は v2 にする")
                bound_seen = True
            elif bound_seen:
                # 束縛は一度入ったら外せない。後の昇格が欄を空けて束縛を外す記録を作らない。
                raise ValueError("設定に束縛した昇格のあとに、束縛の無い昇格を記録しない")
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
    last_attested_ts_ms: int | None = Field(default=None, ge=0)
    """**その arm が、裏づけのある提案を最後に出した時刻**（`last_attested_ts_ms`）。

    segment の `end_ms` でも arm の `last_ts_ms` でもない。`last_ts_ms` は区間の最後の
    tick で、**提案を作れなかった tick（model の読み込み失敗など）も含む**ので、
    いまの失敗を1つ足すだけで古い実績が「新鮮」に見えてしまう（codex #4057035287）。
    裏づけ（`attested`）のある提案が実在した時刻だけを新しさに使う。
    """
    model_artifacts: tuple[str, ...] = ()
    """**その arm が実際に適用した** model artifact（#159 / 決定記録 0059）。

    適用側の arm にだけ付く（`AppliedArmReport.model_artifacts`）。counterfactual の arm は
    報告全体の `model_artifacts` の照合で束縛されるので空のままである。
    """
    unbound_attested_ticks: int = Field(default=0, ge=0)
    """その arm で、裏づけのある提案を適用したのに artifact を言えなかった tick の数。

    **1件でもあれば束縛は完全ではない。** 部分的な証拠を完全として扱わない（fail closed）。
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

    def observe(
        key: str,
        arm: AppliedArm | CounterfactualArm,
        last_attested_ts_ms: int | None,
        *,
        model_artifacts: tuple[str, ...] = (),
        unbound_attested_ticks: int = 0,
    ) -> None:
        current = arms.get(key)
        # **artifact と「不明」の数は segment をまたいで足し合わせる**（#159）。
        # 新しいほうの segment だけを残すと、別の artifact で回した区間や、artifact の
        # 欄を持たない古い trace の区間が束縛の照合から消える。
        artifacts = set(model_artifacts)
        unbound = unbound_attested_ticks
        newest = last_attested_ts_ms
        if current is not None:
            artifacts |= set(current.model_artifacts)
            unbound += current.unbound_attested_ticks
            older = current.last_attested_ts_ms
            if newest is None or (older is not None and older > newest):
                newest = older
            if last_attested_ts_ms is None or (older is not None and older >= last_attested_ts_ms):
                arm = current.arm
        arms[key] = _ArmEvidence(
            arm=arm,
            last_attested_ts_ms=newest,
            model_artifacts=tuple(sorted(artifacts)),
            unbound_attested_ticks=unbound,
        )

    for segment in report.segments:
        if segment.role is not SegmentRole.HOLDOUT:
            continue
        for group in segment.groups:
            if group.kind is not GroupKind.OVERALL:
                continue
            for applied in group.applied:
                observe(
                    applied.arm_key,
                    applied.arm,
                    applied.last_attested_ts_ms,
                    model_artifacts=applied.model_artifacts,
                    unbound_attested_ticks=applied.unbound_attested_ticks,
                )
            for counterfactual in group.counterfactual:
                observe(
                    counterfactual.arm_key,
                    counterfactual.arm,
                    counterfactual.last_attested_ts_ms,
                )
    return arms


def _check_learned_arms(
    report: EvaluationReport,
    approval: StageApproval,
    *,
    production_artifact_sha256: str,
) -> _ArmEvidence:
    """昇格の根拠を **Learned MPC の arm** に束縛する（0057 §2.4 / 決定記録 0059）。

    `arm_key` の一致だけで gate を読むと、承認者が**適用された Fallback の arm**を
    名指すだけで、肝心の Learned MPC の counterfactual arm が `blocked` のまま昇格できる。
    次のすべてを確かめる。

    - 名指した arm が holdout の報告に実在し、その制御器が Learned MPC である
    - その arm の stage が、いま上げようとしている遷移元と同じである
    - **報告に現れた Learned MPC の arm すべて**に gate があり、すべて `pass` である
      （良い arm だけを選んで、落ちた構成を残したまま上げられないようにする）
    - **報告に現れた Learned MPC の arm すべて**に「artifact を言えない適用 tick」が
      1つも無い（#159 / 決定記録 0059。名指した arm だけを見ない）
    - 名指した arm が適用側なら、その arm が適用した artifact が
      **いま Production のちょうど1つ**である

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
    if isinstance(named.arm, AppliedArm):
        # **適用側の arm は、その arm 自身が適用した artifact へ束縛してから受け入れる**
        # （#159 / 決定記録 0059 が 0057 §3 の禁止を置き換える）。
        # 報告全体の `model_artifacts` の照合（呼び出し側）と合わせて**2箇所**で見る。
        artifacts = set(named.model_artifacts)
        if not artifacts:
            # trace に artifact が無い（旧 version だけの区間）。**推測で埋めない。**
            raise AuthorityEvidenceError(
                "適用した artifact が記録されていない arm を根拠にできない"
                f"（arm={approval.evidence.arm_key}）"
            )
        if artifacts != {production_artifact_sha256}:
            raise AuthorityEvidenceError(
                "適用 arm が Production 以外の artifact で回した実績を含んでいる"
                f"（arm={approval.evidence.arm_key}）"
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
        if evidence.unbound_attested_ticks:
            # **名指した arm だけでなく、報告に現れた Learned MPC の適用 arm すべて**が
            # 束縛できていなければならない（codex #4057191724）。名指した arm だけを見ると、
            # 同じ holdout に v1〜v6 の tick を含む別の適用 arm が残っていても昇格できる。
            # 欄を持たない tick が1件でもあれば、その区間は「artifact 不明」である。
            raise AuthorityEvidenceError(
                "artifact を言えない適用 tick が混ざった arm がある"
                f"（arm={key}; unknown_ticks={evidence.unbound_attested_ticks}）"
            )
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


@dataclass(frozen=True, slots=True)
class JournalSignature:
    """`authority.json` を読み直すかを決める `stat` の写し（決定記録 0072 §2.6）。"""

    inode: int
    size: int
    mtime_ns: int


class AuthorityStore:
    """journal を1つの JSON file として持つ、排他・原子置換つきの保管庫。

    Model Registry（#104）と**別の file** に置く。同じ file に置くと、artifact の
    promotion と authority の変更が1つの revision を共有し、片方の操作がもう片方の
    状態を動かせてしまう（0057 §2.3）。
    """

    __slots__ = ("_clock", "_lock_timeout_ms", "_root")

    def __init__(
        self,
        root: Path,
        clock: Clock | None = None,
        *,
        lock_timeout_ms: int | None = None,
    ) -> None:
        """**時刻は store が持つ時計から取る。**

        操作のたびに `now_ms` を受け取ると、期限の判定に使う「いま」を呼び出し側が
        決められる（承認の期限も証拠の新しさも、渡す値ひとつで外せる）。

        ``lock_timeout_ms`` は排他 lock を待てる上限である。**制御ループから呼ぶ store
        には必ず指定する**（決定記録 0060 §2.7）。指定しないと `flock` が無期限に待ち、
        降格の書き残しが control tick のあいだに居座って heartbeat が途切れる。
        人の操作（昇格）のように deadman の無い経路では、待ち続けてよいので省略できる。
        """
        if not root.is_absolute():
            raise AuthorityStoreError("authority store の root は絶対 path にする")
        if lock_timeout_ms is not None and lock_timeout_ms <= 0:
            raise AuthorityStoreError("authority lock の待ち上限は正にする")
        self._root = root
        self._clock = clock if clock is not None else WallClock()
        self._lock_timeout_ms = lock_timeout_ms

    @property
    def lock_timeout_ms(self) -> int | None:
        """排他 lock を待てる上限。`None` は「待ち続ける」。"""
        return self._lock_timeout_ms

    def read(self) -> AuthorityJournal:
        """いまの journal。file が無ければ Baseline から始まったものとして返す。"""
        with self._open_root(create=False) as root_fd:
            if root_fd is None:
                return AuthorityJournal(revision=0)
            return self._read(root_fd)

    def journal_signature(self) -> JournalSignature | None:
        """`authority.json` の `stat`（inode・大きさ・`mtime_ns`）。無ければ None。

        **flock を取らない**（決定記録 0072 §2.6）。control loop が毎 tick、heartbeat の後に
        呼び、変わっていたら `read()` し直す。journal は原子置換で書かれるので、書き換えは
        inode の変化として必ず現れる。regular file でないものは読めないものとして扱う。
        """
        with self._open_root(create=False) as root_fd:
            if root_fd is None:
                return None
            try:
                status = os.stat(AUTHORITY_STATE_FILENAME, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise AuthorityStoreError("authority journal の stat を取れない") from error
        if not stat.S_ISREG(status.st_mode):
            raise AuthorityStoreError("authority journal が regular file ではない")
        return JournalSignature(
            inode=status.st_ino, size=status.st_size, mtime_ns=status.st_mtime_ns
        )

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
        # lock を待たせる前に、明らかに駄目なものは弾く。**判断はこれではない**（下を見る）。
        self._check_approval_freshness(approval, policy, self._clock.now_ms())
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
                # **lock を取ってから時計を読み直す**（codex #4057064071）。registry と
                # authority の lock を待っている間に期限が切れた承認・証拠で昇格させない。
                # 期限の判断は、必ずこの時刻で行う。
                now_ms = self._clock.now_ms()
                if now_ms < 0:
                    raise AuthorityStoreError("authority の時刻は負にできない")
                self._check_approval_freshness(approval, policy, now_ms)
                self._check_production(approval, production)
                self._check_evidence(
                    approval,
                    evaluation_report,
                    policy=policy,
                    fan_policy_config_sha256=config.sources.policy.sha256,
                    safety_config_sha256=config.sources.safety.sha256,
                    air_balance_config_sha256=config.sources.air_balance.sha256,
                    fan_hardware_config_sha256=config.sources.fan_hardware.sha256,
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
        air_balance_config_sha256: str,
        fan_hardware_config_sha256: str,
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
        if report.schema_version < MIN_EVIDENCE_REPORT_SCHEMA_VERSION:
            # **artifact の完全性を言えない報告で昇格しない**（codex #4057527950）。
            # v1 には適用 arm の `model_artifacts` / `unbound_attested_ticks` が無く、
            # 欄の無さが「空・0」＝「全部束縛できた」と読めてしまう。
            # **記録の無さは unknown であって completeness ではない**（決定記録 0059 §2.5）。
            raise AuthorityEvidenceError(
                "artifact の完全性を言えない古い報告では昇格できない"
                f"（schema_version={report.schema_version}; "
                f"required>={MIN_EVIDENCE_REPORT_SCHEMA_VERSION}）"
            )
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
        # **air-balance.yaml と fan-hardware.yaml も、承認・報告・いまの設定の3つで照合する**
        # （決定記録 0073 §2.6）。Air Balance の曲線と profile の写像は MPC の cost を変える。
        if (
            evidence.air_balance_config_sha256 != air_balance_config_sha256
            or provenance.air_balance_config_sha256 != air_balance_config_sha256
        ):
            raise AuthorityEvidenceError("証拠がいまの air-balance.yaml のものではない")
        if (
            evidence.fan_hardware_config_sha256 != fan_hardware_config_sha256
            or provenance.fan_hardware_config_sha256 != fan_hardware_config_sha256
        ):
            raise AuthorityEvidenceError("証拠がいまの fan-hardware.yaml のものではない")
        # **評価に使った trace が、その hash の設定で記録されたこと**を確かめる。評価時の hash を
        # 写すだけでは、別の characterization や hash を持たない古い trace から作った報告が
        # いまの設定を名乗れてしまう。1件でも不一致・欠落があれば丸ごと証拠にしない。
        for name, binding in (
            ("air-balance.yaml", provenance.air_balance_trace_binding),
            ("fan-hardware.yaml", provenance.fan_hardware_trace_binding),
        ):
            if binding is None or not binding.complete_for(provenance.consumed_traces):
                raise AuthorityEvidenceError(
                    f"評価に使った trace が、いまの {name} で記録されたと言えない"
                    + (
                        ""
                        if binding is None
                        else f"（matched={binding.matched}; mismatched={binding.mismatched}; "
                        f"missing={binding.missing}; traces={provenance.consumed_traces}）"
                    )
                )
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
        named = _check_learned_arms(
            report, approval, production_artifact_sha256=production_artifact_sha256
        )
        # **新しさは「裏づけのある提案が最後に実在した時刻」で測る**
        # （codex #4056903573 / #4056942799 / #4057035287）。報告全体の run でも、
        # segment の終わりでも、arm の最後の tick でもない。どれも、Fallback だけで回した
        # 続きや、いまの読み込み失敗を1つ足すだけで「新鮮」にできてしまう。
        #
        # **どの artifact の提案かは、報告全体で1つに絞ってある**（上の `model_artifacts`
        # の照合）。だからこの時刻は、いま production の artifact の提案の時刻である。
        end_ms = named.last_attested_ts_ms
        if end_ms is None:
            raise AuthorityEvidenceError(
                f"裏づけのある提案が1つも無い arm を根拠にできない（arm={evidence.arm_key}）"
            )
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
                self._acquire(lock_fd)
                try:
                    yield root_fd
                finally:
                    flock(lock_fd, LOCK_UN)
            finally:
                os.close(lock_fd)

    def _acquire(self, lock_fd: int) -> None:
        """排他 lock を取る。上限が指定されていれば、そこで諦める。

        **待ち続ける実装を制御ループから使わない。** `flock` は他 process が持っている
        あいだ無期限に待つので、降格の書き残しが control tick のあいだに居座り、
        heartbeat が途切れる（決定記録 0060 §2.7）。諦めた場合は `AuthorityStoreError`
        になり、呼び出し側（`AuthorityRuntime`）が **memory 上で下げたまま** 失敗を記録する。

        ここで使う時計は control loop の単調時計ではない。判断の期限ではなく
        **syscall の再試行の上限**なので、注入した時計に合わせる必要がない。
        """
        timeout_ms = self._lock_timeout_ms
        if timeout_ms is None:
            flock(lock_fd, LOCK_EX)
            return
        deadline = time.monotonic() + timeout_ms / 1_000
        interval = max(0.001, timeout_ms / 10_000)
        while True:
            try:
                flock(lock_fd, LOCK_EX | LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AuthorityStoreError(
                        f"authority lock を {timeout_ms}ms 以内に取れなかった"
                    ) from None
                time.sleep(interval)

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


@dataclass(frozen=True, slots=True)
class _PendingDemotion:
    """memory 上では効いていて、まだ journal へ書き残していない降格（0057 §2.6）。"""

    to_stage: AuthorityStage
    actor: str
    reason: str
    trigger: AuthorityTrigger
    cause: AutomaticCause | None


_SIGNATURE_UNKNOWN = object()
"""次の点検で必ず読み直す印。この process が journal を書いた直後に置く。

書いた直後の `stat` を覚えると、その間に他 process が書いた変化を見逃しうる。"""

JOURNAL_UNREADABLE_ACTOR = "control_runtime"
"""走行中に journal を読めなかったときの降格を書き残す主体（自動降格と同じ）。"""

INVALID_LOWERING_ACTOR = "control_admin"
"""actor が journal の形に合わない人の降格を書き残すときの主体（上限は形に依らず入れる）。"""
INVALID_LOWERING_REASON = "control_admin lowering (actor or reason replaced: invalid form)"
"""reason / actor が journal の形に合わない人の降格の reason。監査の行が元の値を持つ。"""


class AuthorityRuntime:
    """いまの stage を制御ループへ渡し、不健全が続いたら**その場で**下げる。

    `current_stage()` は設定の上限と journal の stage の低いほうを返す。設定を下げれば
    次の tick から効き、設定を上げても journal は上がらない（0057 §2.2）。

    **上げる経路を持たない。** 昇格は `AuthorityStore.raise_stage()` だけが行い、
    この型は runtime から呼べる範囲に置かない（AGENTS.md ルール2 / 3）。外の process
    （人の CLI）が journal を上げたときは、`maintain()` の読み直しで journal の stage が
    上がりうるが、実効 stage は設定の上限とこの process が書き残せずに持っている上限を
    超えない（0072 §2.6）。

    管理ソケット（決定記録 0072）からの降格は `apply_lowering()` が memory 上で先に効かせ、
    書き残しは heartbeat の後の `maintain()` が行う（「先に in-memory、あとで journal」。
    0057 §2.6）。
    """

    __slots__ = (
        "_demotion_consumed",
        "_journal",
        "_journal_unreadable",
        "_last_mono_ms",
        "_lock_waited",
        "_low_confidence",
        "_ood",
        "_pending",
        "_persist_failure",
        "_policy",
        "_signature",
        "_store",
        "_unpersisted_ceiling",
    )

    def __init__(self, store: AuthorityStore, policy: FanPolicyConfig) -> None:
        """**時計は持たない。** 記録の時刻は store が自分の時計で決める。

        store には**必ず lock の待ち上限を持たせる**（決定記録 0060 §2.7）。この runtime は
        control tick の中から呼ばれるので、待ち続ける store を渡すと、降格の書き残しが
        2つの heartbeat のあいだに居座って deadman が鳴る。

        起動時に journal を読めなければ `AuthorityStateError` / `AuthorityStoreError` で止まる
        （0057 §2.1。壊れた journal を「記録の無い状態」と読み替えない）。
        """
        if store.lock_timeout_ms is None:
            raise AuthorityStoreError(
                "control runtime の AuthorityStore には lock の待ち上限（lock_timeout_ms）が要る"
            )
        self._store = store
        self._policy = policy
        # **stat を先に取ってから読む。** 読んだ後に書き換わっても、覚えた stat と違うので
        # 次の点検で読み直す（逆順だと、読んでから stat までの書き換えを見逃す）。
        self._signature: object = store.journal_signature()
        self._journal = store.read()
        # **書き残せなかった降格だけ**を memory 上の上限として持つ（0057 §2.6）。
        # 書けた降格は journal がそのまま表しているので、二重に持たない。持つと、
        # あとから承認された昇格が再起動まで効かなくなる（codex #4056968495）。
        self._unpersisted_ceiling = AuthorityStage.FULL
        self._pending: list[_PendingDemotion] = []
        self._journal_unreadable = False
        # Gate の降格推奨を、立ち下がるまで1回だけ消費するための記憶。
        self._demotion_consumed = False
        self._last_mono_ms: int | None = None
        self._low_confidence: deque[int] = deque()
        self._ood: deque[int] = deque()
        self._persist_failure: Reason | None = None
        # この tick で `observe()` が既に lock を待ったか（`maintain()` で2回目を待たない）。
        self._lock_waited = False

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

    @property
    def journal_unreadable(self) -> bool:
        """走行中に journal を読めなかったので `SHADOW` に下げたまま、まだ書き残していない。"""
        return self._journal_unreadable

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

    def apply_lowering(self, *, to_stage: AuthorityStage, actor: str, reason: str) -> bool:
        """管理ソケットの降格（人の指令）を **memory 上で先に**効かせる（決定記録 0072 §2.6）。

        **古い snapshot で no-op と判断しない。** `to_stage` をいまの実効 stage と比べずに
        無条件で上限として入れ、journal への書き残しを予約する（書くのは heartbeat の後の
        `maintain()`）。外の process が直前に journal を上げていても、この上限は `reload()` で
        外れないので、後から届いた降格が捨てられて authority が上がることはない。

        返すのは、上限を入れた後の実効 stage が入れる前より下がったか（下がらなくても上限は残る）。
        disk も lock も待たない（loop は tick の先頭でこれを呼ぶ）。
        """
        before = self.current_stage()
        # **上限は形の検証より先に、無条件で入れる**（0072 §2.6）。journal へ書く形の不備を
        # 理由に、安全側の降格そのものを捨てない。
        self._unpersisted_ceiling = lowest_stage(self._unpersisted_ceiling, to_stage)
        try:
            # 書き残す記録の形は journal の event と同じ規則で先に確かめる（書く時点で落とさない）
            _check_actor_and_reason(actor, reason)
        except ValueError as error:
            # 形が合わないものは安全な固定値に置き換えて予約する（降格の記録は残す）
            _LOGGER.error(
                "authority の降格の actor / reason が journal の形に合わないので置き換えて残す",
                extra={
                    logs.FIELDS_KEY: {
                        "to_stage": to_stage.value,
                        "error": str(error)[:500],
                    }
                },
            )
            actor = INVALID_LOWERING_ACTOR
            reason = INVALID_LOWERING_REASON
        self._enqueue(
            _PendingDemotion(
                to_stage=to_stage,
                actor=actor,
                reason=reason,
                trigger=AuthorityTrigger.HUMAN,
                cause=None,
            )
        )
        return stage_rank(self.current_stage()) < stage_rank(before)

    def maintain(self) -> None:
        """heartbeat の後に1回呼ぶ。**効くのは次の tick から**（決定記録 0072 §2.6）。

        1. 予約した降格を**1件だけ**書き残す（lock の待ち上限つき。0060 §2.7）。書けたら、
           journal がその上限以下を表していれば memory 上の上限を手放す。書けなければ持ち続け、
           次の tick で書き直す
        2. `authority.json` の `stat` を見て、変わっていれば読み直す（flock を取らない）。
           読めない・壊れているときは memory 上の上限を `SHADOW` に下げ、書き残しを予約する
           （`authority_journal_unreadable`）。**読めるようになっただけでは外さない**

        **同じ tick で `observe()` が既に lock を待っていれば、1 は次の tick に回す**
        （0060 §2.7）。heartbeat の後の待ちを「自動降格の書き残し（≤ d）＋ trace の保存（≤ d）」の
        2回に抑え、`watchdog ≥ 2(t+d)` の予算（heartbeat の間隔 ≤ t+3d）を越えないためである。

        例外を外へ出さない（入口の不具合で冷却を止めない）。結果は構造化ログに残す。
        """
        waited = self._lock_waited
        self._lock_waited = False
        if not waited:
            self._flush_one()
        self._check_journal()

    def flush_pending_on_shutdown(self) -> tuple[AuthorityStage, ...]:
        """停止の直前に、予約した降格を**それぞれ1回だけ**書き残す（lock の待ち上限つき）。

        loop が止まった後に呼ぶ。書けずに残った予約の行き先を返す（呼び出し側が error に残す。
        **再起動すると journal の stage で運転が再開する**ので、黙って捨てない）。
        """
        remaining: list[_PendingDemotion] = []
        while self._pending:
            demotion = self._pending.pop(0)
            if self._write(demotion) is not None:
                remaining.append(demotion)
        self._pending = remaining
        for demotion in remaining:
            _LOGGER.error(
                "停止までに authority の降格を journal へ書き残せなかった（再起動で戻る）",
                extra={
                    logs.FIELDS_KEY: {
                        "to_stage": demotion.to_stage.value,
                        "trigger": demotion.trigger.value,
                        "actor": demotion.actor,
                        "reason": demotion.reason[:500],
                        "cause": None if demotion.cause is None else demotion.cause.value,
                    }
                },
            )
        return tuple(item.to_stage for item in remaining)

    @property
    def pending_stages(self) -> tuple[AuthorityStage, ...]:
        """まだ journal へ書き残していない降格の行き先（予約の順）。"""
        return tuple(item.to_stage for item in self._pending)

    def trace_record(self, *, command_id: int | None) -> AuthorityRecord:
        """decision trace（`ControlTick` v13）へ残す、この時点の制御権の出どころ。"""
        return AuthorityRecord(
            entry="journal",
            journal_stage=self._journal.stage,
            journal_revision=self._journal.revision,
            config_ceiling=self.configured_ceiling,
            unpersisted_ceiling=(
                None
                if self._unpersisted_ceiling is AuthorityStage.FULL
                else self._unpersisted_ceiling
            ),
            journal_unreadable=self._journal_unreadable,
            command_id=command_id,
            persist_failure=self._persist_failure,
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
        metadata["authority_journal_unreadable"] = self._journal_unreadable
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
        # この tick の lock の待ちはここで使った（`maintain()` は書き残しを次の tick に回す）
        self._lock_waited = True
        persist_failure = self._write(
            _PendingDemotion(
                to_stage=to_stage,
                actor=actor,
                reason=f"{code}: {detail}" if detail else code,
                trigger=trigger,
                cause=cause,
            )
        )
        return AuthorityDemotion(
            from_stage=from_stage,
            to_stage=to_stage,
            cause=cause,
            reason=reason,
            persisted=persist_failure is None,
            persist_failure=persist_failure,
        )

    def _write(self, demotion: _PendingDemotion) -> Reason | None:
        """1件の降格を journal へ書く。書けなければ理由を返す（上限はそのまま持ち続ける）。"""
        try:
            journal = self._store.lower_stage(
                to_stage=demotion.to_stage,
                actor=demotion.actor,
                reason=demotion.reason,
                trigger=demotion.trigger,
                cause=demotion.cause,
            )
        except (AuthorityError, OSError, ValidationError) as error:
            # 書き残せなかった。上の上限をそのまま持ち続ける（`reload()` でも外れない）。
            failure = Reason(code="authority_persist_failed", detail=str(error)[:500])
            self._persist_failure = failure
            return failure
        self._journal = journal
        self._signature = _SIGNATURE_UNKNOWN
        self._persist_failure = None
        self._release_if_represented()
        return None

    def _release_if_represented(self) -> None:
        """journal が memory 上の上限以下を表していれば上限を手放す（0057 §2.6「書けたら手放す」）。

        **予約した書き残しが残っている間は手放さない。** 手放すのは、journal の stage が
        上限以下であることを**書いた直後の journal で**確かめられたときだけなので、手放しても
        authority は上がらない。
        """
        if self._pending:
            return
        if stage_rank(self._journal.stage) <= stage_rank(self._unpersisted_ceiling):
            self._unpersisted_ceiling = AuthorityStage.FULL
            self._journal_unreadable = False

    def _enqueue(self, demotion: _PendingDemotion) -> None:
        """書き残しを予約する。**同じ主体の、同じ深さ以上へ下げる予約が先にあれば足さない。**

        主体（trigger・actor・cause）の違う予約は別に残す。自動降格が書けずに残っている間に
        届いた人の降格を、自動降格の予約に吸収させないためである（0072 §2.7）。ただし書く時点で
        journal が既にその stage 以下なら `lower_stage()` は no-op なので、**人の event が
        journal に残らない場合はある**（人の指令は管理ソケットの監査の行に残る。
        docs/control-admin.md）。予約の数は「主体 × stage の段数」で抑えられる。
        """
        if any(
            item.trigger is demotion.trigger
            and item.actor == demotion.actor
            and item.cause is demotion.cause
            and stage_rank(item.to_stage) <= stage_rank(demotion.to_stage)
            for item in self._pending
        ):
            return
        self._pending.append(demotion)

    def _flush_one(self) -> None:
        if not self._pending:
            return
        demotion = self._pending[0]
        # 書く前に予約から外す（書けたら _release_if_represented が残りの予約を見る）
        del self._pending[0]
        failure = self._write(demotion)
        if failure is None:
            _LOGGER.warning(
                "authority の降格を journal へ書き残した",
                extra={
                    logs.FIELDS_KEY: {
                        "to_stage": demotion.to_stage.value,
                        "trigger": demotion.trigger.value,
                        "cause": None if demotion.cause is None else demotion.cause.value,
                        "journal_stage": self._journal.stage.value,
                        "journal_revision": self._journal.revision,
                    }
                },
            )
            return
        # 書けなかった。予約の先頭へ戻し、次の tick で書き直す（上限は持ち続ける）
        self._pending.insert(0, demotion)
        _LOGGER.error(
            "authority の降格を journal へ書き残せなかった（memory 上では下げたまま）",
            extra={
                logs.FIELDS_KEY: {
                    "to_stage": demotion.to_stage.value,
                    "trigger": demotion.trigger.value,
                    "cause": None if demotion.cause is None else demotion.cause.value,
                    "persist_failure": failure.model_dump(mode="json"),
                }
            },
        )

    def _check_journal(self) -> None:
        """`stat` が変わっていれば読み直す。読めなければ `SHADOW` に下げる（0072 §2.6）。"""
        try:
            signature: object = self._store.journal_signature()
            if signature == self._signature:
                return
            journal = self._store.read()
        except (AuthorityError, OSError, ValidationError) as error:
            self._on_journal_unreadable(error)
            return
        previous = self._journal
        if not _extends(journal, previous):
            # **revision の後退・履歴の差し替えを信じない**（0072 §2.6 / 0057 §2.6）。古い
            # バックアップの書き戻しで、承認を経ずに高い stage が戻ってくるのを防ぐ。
            # 読めない journal と同じく SHADOW に下げ、書き残せるまで外さない。知っている
            # journal はそのまま持つ（`_signature` も更新しないので、毎 tick 確かめ直す）。
            self._on_journal_unreadable(
                AuthorityStateError(
                    "authority journal が既知の履歴を延長していない"
                    f"（known_revision={previous.revision}; read_revision={journal.revision}）"
                )
            )
            return
        self._journal = journal
        self._signature = signature
        if journal != previous:
            _LOGGER.info(
                "authority journal を読み直した（次の tick から効く）",
                extra={
                    logs.FIELDS_KEY: {
                        "journal_stage": journal.stage.value,
                        "journal_revision": journal.revision,
                        "previous_journal_stage": previous.stage.value,
                        "previous_journal_revision": previous.revision,
                        "authority_stage": self.current_stage().value,
                    }
                },
            )

    def _on_journal_unreadable(self, error: BaseException) -> None:
        """読めない間は制御権を最小にする。**止めない**（止めると冷却が止まる。0072 §4 M）。"""
        already = self._journal_unreadable
        self._journal_unreadable = True
        self._unpersisted_ceiling = BASELINE_STAGE
        self._enqueue(
            _PendingDemotion(
                to_stage=BASELINE_STAGE,
                actor=JOURNAL_UNREADABLE_ACTOR,
                reason=AutomaticCause.AUTHORITY_JOURNAL_UNREADABLE.value,
                trigger=AuthorityTrigger.AUTOMATIC,
                cause=AutomaticCause.AUTHORITY_JOURNAL_UNREADABLE,
            )
        )
        if already:
            return
        _LOGGER.error(
            "authority journal を読めないため、書き残せるまで SHADOW に下げる",
            extra={
                logs.FIELDS_KEY: {
                    "reason": AutomaticCause.AUTHORITY_JOURNAL_UNREADABLE.value,
                    "error": f"{type(error).__name__}: {error}"[:500],
                    "journal_stage": self._journal.stage.value,
                }
            },
        )

    def _expire(self, now_mono_ms: int) -> None:
        cutoff = now_mono_ms - self._policy.authority_rollout.unhealthy_window_ms.value
        for history in (self._low_confidence, self._ood):
            while history and history[0] < cutoff:
                history.popleft()


def _extends(journal: AuthorityJournal, known: AuthorityJournal) -> bool:
    """`journal` が `known` の event 列をそのまま先頭に持つ（追記だけで届いた）か。"""
    if journal.revision < known.revision:
        return False
    return journal.events[: known.revision] == known.events


def _check_actor_and_reason(actor: str, reason: str) -> None:
    """journal の event と同じ規則（`AuthorityEvent.actor` / `reason`）で先に確かめる。

    書く時点（heartbeat の後）で形の誤りが分かると、予約が永久に書けないまま残る。
    """
    if re.fullmatch(_ACTOR_PATTERN, actor) is None or len(actor) > _ACTOR_MAX_CHARS:
        raise ValueError("actor が journal の形に合わない")
    if not 1 <= len(reason) <= _REASON_MAX_CHARS:
        raise ValueError("reason の長さが journal の形に合わない")
