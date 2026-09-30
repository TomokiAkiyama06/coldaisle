"""管理ソケットの受け渡し口から、loop が tick ごとにモードを決める（決定記録 0072 §2.2 / §2.4）。

`coldaisle.control` は**受け渡し口の Protocol（`ModeMailbox`）だけを知る。** 実装（受付スレッド・
監査・ソケット）は `coldaisle.control_admin` にあり、この package はそれを import しない
（下位層が上位を import しない。0072 §2.2）。

ここに置くのは、loop が1 tick の先頭で行う**決定論的な手順**だけである。

1. 受付スレッドが生きているかを **lock を取らずに**確かめる。死んでいたら `MANUAL` を解除し、
   `MAX`（Critical Safety の `forced_max`）に倒して、**再起動まで保つ**。以後は受け渡し口を読まない
2. 受け渡し口を**非ブロッキングで**1回だけ覗く。取れなければ直前のモードのまま進む
3. `MANUAL` の lease を**この loop の単調時計だけ**で数え、期限が来たら `AUTO` へ戻す

受け渡し口は**状態の軸ごとに1枠**（モードの枠と authority の枠）を持ち、loop は2枠を同じ lock の
中で取り出す（0072 §2.2）。authority の枠の指令（`lower_authority` / `rollback_authority`。段階 2 の
#92）は、loop がこの tick の Gate が stage を読む前に `AuthorityRuntime` へ入れる。
ここでは取り出して loop へ渡すだけで、`AuthorityRuntime` には触れない。
**authority を上げる指令の型は無い**（0072 §2.1）。

適用した指令と lease 切れは受け渡し口へ返すだけで、監査の表へは書かない（loop は DB を待たない。
0072 §2.7）。`status` のための最新の状態も、受け渡し口へ置くだけにする。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle import logs
from coldaisle.control.schema import (
    BASELINE_STAGE,
    AuthorityRecord,
    AuthorityStage,
    Demand,
    ModeCommandRecord,
    OperatingMode,
    PerZone,
    Reason,
    ZoneRequest,
    stage_rank,
)

LOGGER = logging.getLogger("coldaisle.control")

MANUAL_COMMAND_REASON = "manual_command"
"""人の `MANUAL` 指令が作った requested の理由の code。値は `command_id` だけを残す。"""


class ModeCommand(BaseModel):
    """人が決めた運転モードと、人が決めた requested（0028 §2.5 (a)）。

    `MANUAL` / `CALIBRATION` では人が requested を決めるので、その値をここで運ぶ。
    `MAX` は **requested では表さない**（Critical Safety の `forced_max` が所有する）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    mode: OperatingMode = OperatingMode.AUTO
    requested: PerZone[ZoneRequest] | None = None

    @model_validator(mode="after")
    def _people_set_demands_only_where_they_own_them(self) -> Self:
        owns_requested = self.mode in {OperatingMode.MANUAL, OperatingMode.CALIBRATION}
        if owns_requested != (self.requested is not None):
            raise ValueError("requested を持てるのは MANUAL / CALIBRATION だけ（0028 §2.5 (a)）")
        return self


class AdminModeCommand(BaseModel):
    """受付スレッドが検証し、受け渡し口へ置いた1件の指令（0072 §2.3）。

    **人の自由記述（`reason`）は運ばない。** それは監査の表だけに残り、decision trace と
    requested の理由には `command_id` だけが入る。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    command_id: int = Field(ge=1)
    mode: OperatingMode
    requested: PerZone[Demand] | None = None
    """`MANUAL` の zone ごとの demand（`0.0..1.0`。PWM は受け取らない。0028 §2.3）。"""
    lease_ms: int | None = Field(default=None, ge=1)
    """`MANUAL` の期限。起点は loop がこの指令を適用した tick の単調時刻（0072 §2.4）。"""

    @model_validator(mode="after")
    def _manual_carries_values_and_a_lease(self) -> Self:
        if self.mode is OperatingMode.CALIBRATION:
            # #75 の測定計画の参照形が決まるまで予約（0072 §2.3 / §2.10 段階 4）
            raise ValueError("CALIBRATION は受け渡し口に置けない")
        is_manual = self.mode is OperatingMode.MANUAL
        if is_manual != (self.requested is not None):
            raise ValueError("requested を持てるのは MANUAL だけ")
        if is_manual != (self.lease_ms is not None):
            # MANUAL には期限を必須にし、MAX には付けない（勝手に下がる経路を作らない）
            raise ValueError("lease を持てるのは MANUAL だけで、MANUAL には必須")
        return self

    def to_mode_command(self) -> ModeCommand:
        """loop が使う `ModeCommand`。`MAX` は requested では表さない（0028 §2.5 (a)）。"""
        if self.requested is None:
            return ModeCommand(mode=self.mode)
        reason = Reason(code=MANUAL_COMMAND_REASON, detail=f"command_id={self.command_id}")
        return ModeCommand(
            mode=self.mode,
            requested=PerZone[ZoneRequest](
                front=ZoneRequest(demand=self.requested.front, reason=reason),
                rear=ZoneRequest(demand=self.requested.rear, reason=reason),
                top=ZoneRequest(demand=self.requested.top, reason=reason),
            ),
        )


ADMIN_ACTOR_PREFIX = "uid."
"""管理ソケットの指令を journal に残すときの主体の形（`uid.<数値>`。0072 §2.5）。"""


class AdminAuthorityCommand(BaseModel):
    """受付スレッドが検証し、受け渡し口の authority の枠へ置いた降格（0072 §2.3 / §2.6）。

    **下げる向きしか表せない。** `rollback_authority` は `SHADOW`（Baseline）へ、
    `lower_authority` は `to_stage` へ下げる上限を入れる。いまの実効 stage とは比べない
    （受付の時点の stage は古いことがある。適用するかは loop が決める。0072 §2.3 / §2.6）。

    journal には人の変更として残るので、主体（`uid.<数値>`）と理由を運ぶ（0072 §2.7）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    command_id: int = Field(ge=1)
    op: Literal["lower_authority", "rollback_authority"]
    to_stage: AuthorityStage
    actor: str = Field(pattern=r"^uid\.[0-9]+$")
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _rollback_goes_to_the_baseline(self) -> Self:
        if self.op == "rollback_authority" and self.to_stage is not BASELINE_STAGE:
            raise ValueError("rollback_authority は Baseline（SHADOW）へ戻す")
        return self

    def deeper_than(self, other: AdminAuthorityCommand) -> bool:
        """`other` より低い行き先か（枠の合成は**最も低い行き先**を採る。0072 §2.2）。"""
        return stage_rank(self.to_stage) < stage_rank(other.to_stage)


@dataclass(frozen=True, slots=True)
class MailboxTake:
    """loop が1 tick の先頭で、受け渡し口の2枠から**同じ lock の中で**取り出したもの。"""

    mode: AdminModeCommand | None = None
    authority: AdminAuthorityCommand | None = None


class ModeOutcomeKind(StrEnum):
    """loop が受け渡し口へ返す事実（監査の表の事象の行になる。0072 §2.7）。"""

    APPLIED = "applied"
    LEASE_EXPIRED = "lease_expired"


@dataclass(frozen=True, slots=True)
class ModeOutcome:
    """loop が適用した指令・lease を切った指令と、その tick。"""

    kind: ModeOutcomeKind
    command_id: int
    tick_id: int


@dataclass(frozen=True, slots=True)
class ModeStatus:
    """`status` のために loop が毎 tick 置いていく、いまの状態（読むだけ。0072 §2.7）。

    受付スレッドは loop の状態に触れないので、loop のほうから写しを置く。
    """

    tick_id: int
    mode: OperatingMode
    command_id: int | None
    lease_deadline_mono_ms: int | None
    """`MANUAL` の期限（この process の単調時計）。残りは受付スレッドが同じ時計で数える。"""
    authority_stage: AuthorityStage
    admin_receiver_dead: bool
    authority: AuthorityRecord | None = None
    """その tick の制御権の出どころ（journal の stage・上限・永続化の失敗。0072 §2.7）。"""


class ModeMailbox(Protocol):
    """受付スレッドと loop の間の受け渡し口のうち、**loop が使う面**（0072 §2.2）。

    どのメソッドも**待たない**。loop は Critical Safety より前でこれを呼ぶので、
    ここで待つと安全側の裁定ごと止まる。
    """

    def receiver_alive(self) -> bool:
        """受付スレッドが生きているか。**lock を取らない。**"""
        ...

    def take(self) -> MailboxTake | None:
        """モードの枠と authority の枠を**同じ lock の中で**取り出す。

        lock が取れなければ None（その tick は直前のモード・stage のまま進む。0072 §2.2）。
        """
        ...

    def report(self, outcome: ModeOutcome) -> None:
        """適用・lease 切れを受付スレッドへ返す。**待たない。**"""
        ...

    def publish(self, status: ModeStatus) -> None:
        """`status` のための写しを置く。**待たない。**"""
        ...


@dataclass(frozen=True, slots=True)
class ModeResolution:
    """1 tick のモードと、それを decision trace に残す記録。"""

    command: ModeCommand
    record: ModeCommandRecord
    authority: AdminAuthorityCommand | None = None
    """この tick の先頭で loop が `AuthorityRuntime` へ入れる降格（0072 §2.6）。"""


class AdminModeTracker:
    """受け渡し口からこの tick のモードを決める（0072 §2.2 / §2.4）。**loop のスレッドだけが使う。**

    状態は memory 上だけで、再起動で `AUTO` に戻る（0028 §2.5 (a)）。
    """

    __slots__ = (
        "_command",
        "_command_id",
        "_dead",
        "_lease_deadline_mono_ms",
        "_mailbox",
        "_run_id",
    )

    def __init__(self, mailbox: ModeMailbox, *, run_id: str) -> None:
        self._mailbox = mailbox
        self._run_id = run_id
        self._command = ModeCommand()
        self._command_id: int | None = None
        self._lease_deadline_mono_ms: int | None = None
        self._dead = False

    @property
    def receiver_dead(self) -> bool:
        """受付スレッドの死を見て `MAX` に固定したか（再起動まで戻らない）。"""
        return self._dead

    def resolve(self, *, tick_id: int, now_mono_ms: int) -> ModeResolution:
        """この tick のモード。**例外を外へ出さない**（入口の不具合で冷却を止めない）。"""
        if self._dead or not self._receiver_alive():
            return self._hold_max_after_receiver_death(tick_id)
        authority = self._take(tick_id=tick_id, now_mono_ms=now_mono_ms)
        expired = self._expire_lease(tick_id=tick_id, now_mono_ms=now_mono_ms)
        return ModeResolution(
            authority=authority,
            command=self._command,
            record=ModeCommandRecord(
                entry="control_admin",
                run_id=self._run_id,
                command_id=self._command_id,
                manual_lease_expired_command_id=expired,
            ),
        )

    def authority_applied(self, *, command_id: int, tick_id: int) -> None:
        """loop が authority の降格を `AuthorityRuntime` へ入れたことを受付スレッドへ返す。"""
        self._report(ModeOutcome(ModeOutcomeKind.APPLIED, command_id, tick_id))

    def publish(
        self,
        *,
        tick_id: int,
        authority_stage: AuthorityStage,
        authority: AuthorityRecord | None = None,
    ) -> None:
        """`status` のための写しを受け渡し口へ置く。失敗しても制御は止めない。"""
        status = ModeStatus(
            tick_id=tick_id,
            mode=self._command.mode,
            command_id=self._command_id,
            lease_deadline_mono_ms=self._lease_deadline_mono_ms,
            authority_stage=authority_stage,
            admin_receiver_dead=self._dead,
            authority=authority,
        )
        try:
            self._mailbox.publish(status)
        except Exception:
            LOGGER.exception("control-admin の status を置けなかった")

    def _receiver_alive(self) -> bool:
        try:
            return self._mailbox.receiver_alive()
        except Exception:
            # 生きていると確かめられないなら、死んだ側として扱う（安全側）
            LOGGER.exception("control-admin の受付スレッドの生存を確かめられなかった")
            return False

    def _hold_max_after_receiver_death(self, tick_id: int) -> ModeResolution:
        """受付スレッドが死んだら `MANUAL` を解除して `MAX` にし、**再起動まで保つ**（0072 §2.2）。

        死んだあとは受け渡し口を読まない（残った指令も適用しない）。lease も付けない。
        """
        if not self._dead:
            self._dead = True
            LOGGER.error(
                "control-admin の受付スレッドが死んだため、再起動まで全 zone を Max にする",
                extra={
                    logs.FIELDS_KEY: {
                        "tick_id": tick_id,
                        "reason": "admin_receiver_dead",
                        "released_mode": self._command.mode.value,
                        "released_command_id": self._command_id,
                    }
                },
            )
        self._command = ModeCommand(mode=OperatingMode.MAX)
        self._command_id = None
        self._lease_deadline_mono_ms = None
        return ModeResolution(
            command=self._command,
            record=ModeCommandRecord(
                entry="control_admin", run_id=self._run_id, admin_receiver_dead=True
            ),
        )

    def _take(self, *, tick_id: int, now_mono_ms: int) -> AdminAuthorityCommand | None:
        """2枠を取り出し、モードを適用する。authority の降格は loop へ返す（loop が入れる）。"""
        try:
            both = self._mailbox.take()
        except Exception:
            # 読めない tick は直前のモード・stage を保つ（0060 §2.3）
            LOGGER.exception(
                "control-admin の受け渡し口を読めなかった; 直前のモードを保つ",
                extra={logs.FIELDS_KEY: {"tick_id": tick_id, "mode": self._command.mode.value}},
            )
            return None
        if both is None:
            return None
        taken = both.mode
        if taken is not None:
            self._apply_mode(taken, tick_id=tick_id, now_mono_ms=now_mono_ms)
        return both.authority

    def _apply_mode(self, taken: AdminModeCommand, *, tick_id: int, now_mono_ms: int) -> None:
        self._command = taken.to_mode_command()
        self._command_id = taken.command_id
        self._lease_deadline_mono_ms = (
            None if taken.lease_ms is None else now_mono_ms + taken.lease_ms
        )
        LOGGER.info(
            "control-admin の指令を適用した",
            extra={
                logs.FIELDS_KEY: {
                    "tick_id": tick_id,
                    "command_id": taken.command_id,
                    "mode": taken.mode.value,
                    "lease_ms": taken.lease_ms,
                }
            },
        )
        self._report(ModeOutcome(ModeOutcomeKind.APPLIED, taken.command_id, tick_id))

    def _expire_lease(self, *, tick_id: int, now_mono_ms: int) -> int | None:
        """`MANUAL` の期限を単調時計で確かめ、来ていれば `AUTO` へ戻す（0072 §2.4）。"""
        deadline = self._lease_deadline_mono_ms
        if deadline is None or now_mono_ms < deadline:
            return None
        expired = self._command_id
        assert expired is not None  # lease は MANUAL の指令にだけ付く
        self._command = ModeCommand()
        self._command_id = None
        self._lease_deadline_mono_ms = None
        LOGGER.warning(
            "MANUAL の lease が切れたため AUTO へ戻した",
            extra={
                logs.FIELDS_KEY: {
                    "tick_id": tick_id,
                    "command_id": expired,
                    "reason": "manual_lease_expired",
                }
            },
        )
        self._report(ModeOutcome(ModeOutcomeKind.LEASE_EXPIRED, expired, tick_id))
        return expired

    def _report(self, outcome: ModeOutcome) -> None:
        try:
            self._mailbox.report(outcome)
        except Exception:
            # 監査の結末の行が欠けても適用は取り消さない。事実は decision trace に残る（0072 §2.7）
            LOGGER.exception(
                "control-admin へ結果を返せなかった",
                extra={
                    logs.FIELDS_KEY: {
                        "command_id": outcome.command_id,
                        "outcome": outcome.kind.value,
                    }
                },
            )
