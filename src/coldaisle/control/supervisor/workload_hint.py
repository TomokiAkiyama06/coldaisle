"""Workload Hint Stage B0: 受け渡し口から読んだヒントの状態を tick ごとに進め、trace の塊を作る。

#319 / 決定記録 0133 §2.3 / §2.4 / §2.6 / §2.8 / §2.9、0064 §2.6 / §2.7、0066 §2.2。

**ここは control 層で、DB を開かない。** ``events`` と終端状態の表を読むのは合成の起点
（`coldaisle.workload_hint_reader`）の読み手のスレッドで、検証済みの不変の値（`HintFeed`）を
受け渡し口に置く。loop は tick ごとに受け渡し口を**待たずに**覗くだけ
（`operating_mode.py` と同じ形）。

役割の分担:

- **読み手のスレッド**: 行の順序（``events.id``）で決まる遷移（``superseded`` / ``ended`` /
  ``unsupported_version``、初めて読んだ時点での ``expired``）と、起動時の取り込み（0066 §2.2）。
  終端状態の永続化も読み手が行う
- **このモジュール（loop のスレッドだけ）**: 時間と Telemetry で決まる遷移（``expired`` /
  ``contradicted``）と、tick ごとの ``age_ms``。遷移は受け渡し口へ返し、永続化は読み手に任せる
  （loop は DB を待たない）

**B0 は ``requested_demand`` を1ビットも変えない。** `SupervisorInput.workload_hint` に入れるが、
RulePolicy の ``propose`` はこれを読まず、「効かせていたら選択が変わったか」（``would_override``）を
trace に残すだけ（0133 §2.6）。Safety / Guard / Fallback / hardware はこのモジュールの型を
import しない（0133 §2.7。試験で固定する）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from coldaisle import logs
from coldaisle.clock import BootClock, MonotonicClock
from coldaisle.control.config import WorkloadHintConfig
from coldaisle.control.schema import (
    HINT_SOURCE_LABEL_OTHER,
    HintPriorSkipped,
    WorkloadHintInfluence,
    WorkloadHintKind,
    WorkloadHintRecord,
    WorkloadHintStage,
    WorkloadHintStatus,
    WorkloadRegime,
    workload_hint_stage_rank,
)
from coldaisle.control.supervisor.hint_clock import (
    NS_PER_MS,
    BackfillRejection,
    BootReading,
    HintAge,
    HintRowClock,
    evaluate_running_hint,
)

LOGGER = logging.getLogger("coldaisle.control")

IMPLEMENTED_STAGE = WorkloadHintStage.OBSERVE
"""この実装が動かせる最も高い段階（B0）。

B1 / B2 を設定しても、認証のハッシュ（0066 §2.1 (c)）を照合する仕組みがまだ無いので、
実効の段階は B0 に留まる（0133 §2.6「ハッシュが無い・合わないときは B0 に留めて起動する」）。
"""

CONTRADICTING_REGIMES = frozenset({WorkloadRegime.IDLE, WorkloadRegime.COOLDOWN})
"""計算負荷のヒントと食い違う Regime（0064 §2.7。v1 のヒントは3種類とも計算負荷）。"""


class WorkloadHint(BaseModel):
    """`SupervisorInput.workload_hint` に入る、その tick で ``active`` なヒント（0064 §2.5）。

    **閉じた語彙と数だけを持つ。** ``note``・生の ``source``・``peer_uid`` は持たない（0064 §2.9）。
    ``remaining_ms`` は期限までの残りで、band の補間には使わない（0133 §2.3。効くか
    効かないかの2値）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    event_id: int = Field(ge=1)
    workload: WorkloadHintKind
    source_label: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    age_ms: int = Field(ge=0)
    remaining_ms: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class HintObservation:
    """読み手が追っている最新のヒント（行の順序で決まる状態まで）。

    ``status`` が ``active`` のものだけが時間の遷移の対象で、そのとき ``workload`` /
    ``source_label`` / ``declared_ms`` / 時刻の欄がそろう。``unsupported_version`` の行は
    ``workload`` / ``source_label`` を持たない（知らない版を推測で読まない。0064 §2.3）。
    """

    event_id: int
    status: WorkloadHintStatus
    workload: WorkloadHintKind | None = None
    source_label: str | None = None
    declared_ms: int | None = None
    """``expected_duration_s * 1000 + grace_ms``（無ければ ``default_ttl_ms``）。0064 §2.6。"""
    ts_ms: int | None = None
    boot_id: str | None = None
    boottime_ns: int | None = None

    def __post_init__(self) -> None:
        if self.event_id < 1:
            raise ValueError("event_id は 1 以上")
        if self.status is WorkloadHintStatus.ACTIVE and (
            self.workload is None
            or self.source_label is None
            or self.declared_ms is None
            or self.declared_ms <= 0
            or self.ts_ms is None
            or self.boot_id is None
            or self.boottime_ns is None
        ):
            raise ValueError("active のヒントは workload・source・期限・行の時刻をそろえて持つ")

    def row_clock(self) -> HintRowClock:
        """期限の計算に使う行の時刻（0066 §2.2。起点は行の ``boottime_ns``）。"""
        assert self.ts_ms is not None
        return HintRowClock(
            event_id=self.event_id,
            ts_ms=self.ts_ms,
            boot_id=self.boot_id,
            boottime_ns=self.boottime_ns,
        )


class HintFeedState(StrEnum):
    """読み手が置いた値の状態。"""

    OK = "ok"
    UNREADABLE = "unreadable"
    """最後の照会に失敗した（0066 §2.2）。成功するまでヒントを使わない。"""
    RUN_START_NOT_DURABLE = "run_start_not_durable"
    """run の開始記録を永続化できなかった。この run の間はヒントを扱わない（0066 §2.2）。"""


@dataclass(frozen=True, slots=True)
class HintFeed:
    """読み手のスレッドが受け渡し口に置く、検証済みの不変の値。

    ``read_monotonic_ms`` は読み手が最後に照会に成功した時刻（loop と同じ単調時計）。
    loop はこれが ``reader_stale_after_ms`` より古ければ ``hint_state_unreadable`` に倒す。
    """

    state: HintFeedState
    read_monotonic_ms: int
    current: HintObservation | None = None


@dataclass(frozen=True, slots=True)
class HintTransition:
    """loop が決めた終端の遷移（``expired`` / ``contradicted``）。読み手が永続化する。"""

    event_id: int
    status: WorkloadHintStatus
    ts_ms: int
    boot_id: str
    boottime_ns: int


class WorkloadHintMailbox(Protocol):
    """読み手のスレッドと loop の間の受け渡し口のうち、**loop が使う面**（0133 §2.8）。

    どのメソッドも**待たない**。
    """

    def reader_alive(self) -> bool:
        """読み手のスレッドが生きているか。**lock を取らない。**"""
        ...

    def latest(self) -> HintFeed | None:
        """最後に置かれた値。まだ無ければ None。"""
        ...

    def report(self, transition: HintTransition) -> None:
        """終端の遷移を読み手へ返す。**待たない。**"""
        ...


@dataclass(frozen=True, slots=True)
class HintTick:
    """1 tick のヒントの状態。`SupervisorInput` へ渡す値と、trace の塊の材料。"""

    stage: WorkloadHintStage
    hint: WorkloadHint | None = None
    observation: HintObservation | None = None
    status: WorkloadHintStatus | None = None
    age_ms: int | None = None
    lost: HintPriorSkipped | None = None
    """``hint_state_unreadable`` / ``run_start_not_durable``。ヒントの欄を持たない tick。"""


class WorkloadHintTracker:
    """受け渡し口からこの tick のヒントの状態を決める。**loop のスレッドだけが使う。**

    例外を外へ出さない（ヒントは任意の機能で、失敗はヒント無しに倒すだけ。MAX にしない。
    0133 §2.8）。
    """

    __slots__ = (
        "_boot_clock",
        "_config",
        "_configured",
        "_contradiction_event",
        "_contradiction_since_ns",
        "_effective",
        "_mailbox",
        "_monotonic",
        "_terminal",
    )

    def __init__(
        self,
        config: WorkloadHintConfig,
        *,
        mailbox: WorkloadHintMailbox | None,
        boot_clock: BootClock,
        monotonic: MonotonicClock,
    ) -> None:
        """``mailbox`` が None なのは ``stage: off`` の構成だけ（読み手を作らない）。"""
        self._config = config
        self._configured = config.stage
        self._effective = effective_stage(config.stage)
        if (self._effective is WorkloadHintStage.OFF) != (mailbox is None):
            raise ValueError("読み手の受け渡し口は stage が off 以外のときだけ配線する")
        self._mailbox = mailbox
        self._boot_clock = boot_clock
        self._monotonic = monotonic
        # loop が決めた終端（読み手が永続化して値に反映するまでの間も、再び active にしない）
        self._terminal: dict[int, WorkloadHintStatus] = {}
        self._contradiction_event: int | None = None
        self._contradiction_since_ns: int | None = None

    @property
    def stage(self) -> WorkloadHintStage:
        """実効の段階。"""
        return self._effective

    @property
    def configured_stage(self) -> WorkloadHintStage:
        """設定の上限。"""
        return self._configured

    def step(self, *, tick_id: int, ts_ms: int, regime: WorkloadRegime | None) -> HintTick:
        """この tick のヒントの状態を進める。``regime`` は Telemetry 由来（無ければ None）。"""
        if self._effective is WorkloadHintStage.OFF:
            return HintTick(stage=WorkloadHintStage.OFF)
        try:
            return self._step(tick_id=tick_id, ts_ms=ts_ms, regime=regime)
        except Exception:
            # 不具合でも制御は止めない。ヒント無しに倒す（0133 §2.8）
            LOGGER.exception(
                "workload hint の状態を進められなかった; ヒントを使わない",
                extra={logs.FIELDS_KEY: {"tick_id": tick_id, "reason": "hint_state_unreadable"}},
            )
            self._reset_contradiction()
            return self._lost(HintPriorSkipped.HINT_STATE_UNREADABLE)

    def record(
        self, tick: HintTick, *, supervisor_available: bool, would_override: bool
    ) -> WorkloadHintRecord:
        """trace の塊（0133 §2.9）。``would_override`` は RulePolicy が判定した値。"""
        if tick.stage is WorkloadHintStage.OFF:
            return WorkloadHintRecord.off()
        if tick.lost is not None:
            return WorkloadHintRecord(stage=tick.stage, skipped=tick.lost)
        observation = tick.observation
        if observation is None:
            return WorkloadHintRecord(stage=tick.stage)
        influence = WorkloadHintInfluence.NONE
        skipped: HintPriorSkipped | None = None
        if tick.hint is not None:
            if not supervisor_available:
                skipped = HintPriorSkipped.SUPERVISOR_UNAVAILABLE
            else:
                if would_override:
                    influence = WorkloadHintInfluence.WOULD_OVERRIDE
                if workload_hint_stage_rank(self._configured) > workload_hint_stage_rank(
                    self._effective
                ):
                    # 設定は B1 / B2 だが、認証のハッシュが無いので B0 に留めた（0133 §2.6）
                    skipped = HintPriorSkipped.CERTIFICATION_MISMATCH
        return WorkloadHintRecord(
            stage=tick.stage,
            event_id=observation.event_id,
            workload=observation.workload,
            status=tick.status,
            source_label=observation.source_label,
            age_ms=tick.age_ms,
            influence=influence,
            skipped=skipped,
        )

    # ------------------------------------------------------------------ 内部

    def _lost(self, reason: HintPriorSkipped) -> HintTick:
        return HintTick(stage=self._effective, lost=reason)

    def _reset_contradiction(self) -> None:
        self._contradiction_event = None
        self._contradiction_since_ns = None

    def _step(self, *, tick_id: int, ts_ms: int, regime: WorkloadRegime | None) -> HintTick:
        mailbox = self._mailbox
        assert mailbox is not None
        feed = self._feed(mailbox)
        if feed is None:
            self._reset_contradiction()
            return self._lost(HintPriorSkipped.HINT_STATE_UNREADABLE)
        if feed.state is HintFeedState.RUN_START_NOT_DURABLE:
            self._reset_contradiction()
            return self._lost(HintPriorSkipped.RUN_START_NOT_DURABLE)
        observation = feed.current
        if observation is None:
            self._reset_contradiction()
            return HintTick(stage=self._effective)
        status = self._terminal.get(observation.event_id, observation.status)
        if status is not WorkloadHintStatus.ACTIVE:
            self._reset_contradiction()
            return HintTick(stage=self._effective, observation=observation, status=status)
        boot = BootReading(
            boot_id=self._boot_clock.boot_id(), boottime_ns=self._boot_clock.boottime_ns()
        )
        assert observation.declared_ms is not None
        assert self._config.max_age_ms is not None
        age = evaluate_running_hint(
            observation.row_clock(),
            now=boot,
            declared_ms=observation.declared_ms,
            max_age_ms=self._config.max_age_ms,
        )
        if age is BackfillRejection.EXPIRED:
            return self._terminate(
                observation, WorkloadHintStatus.EXPIRED, tick_id=tick_id, ts_ms=ts_ms, boot=boot
            )
        if not isinstance(age, HintAge):
            # 同じ boot の中で行の時刻が「いま」より新しい（ありえないが改ざん等）。経過を
            # 確かめられない行を使わない。終端にはしない（読めないのと同じ扱い。0066 §2.2）
            LOGGER.warning(
                "workload hint の経過を確かめられない; ヒントを使わない",
                extra={
                    logs.FIELDS_KEY: {
                        "tick_id": tick_id,
                        "event_id": observation.event_id,
                        "reason": age.value,
                    }
                },
            )
            self._reset_contradiction()
            return self._lost(HintPriorSkipped.HINT_STATE_UNREADABLE)
        if self._contradicted(observation.event_id, regime, boot):
            return self._terminate(
                observation,
                WorkloadHintStatus.CONTRADICTED,
                tick_id=tick_id,
                ts_ms=ts_ms,
                boot=boot,
            )
        assert observation.workload is not None and observation.source_label is not None
        hint = WorkloadHint(
            event_id=observation.event_id,
            workload=observation.workload,
            source_label=observation.source_label,
            age_ms=age.age_ms,
            remaining_ms=age.remaining_ms,
        )
        return HintTick(
            stage=self._effective,
            hint=hint,
            observation=observation,
            status=WorkloadHintStatus.ACTIVE,
            age_ms=age.age_ms,
        )

    def _feed(self, mailbox: WorkloadHintMailbox) -> HintFeed | None:
        """受け渡し口の値。読み手が死んだ・値が古い・照会に失敗した、なら None。"""
        try:
            alive = mailbox.reader_alive()
            feed = mailbox.latest()
        except Exception:
            LOGGER.exception("workload hint の受け渡し口を読めなかった")
            return None
        if feed is None:
            return None
        if feed.state is HintFeedState.RUN_START_NOT_DURABLE:
            # run の開始を永続化できなかった run では、読み手の生死に関わらずヒントを扱わない
            return feed
        if not alive or feed.state is HintFeedState.UNREADABLE:
            return None
        assert self._config.reader_stale_after_ms is not None
        if self._monotonic.monotonic_ms() - feed.read_monotonic_ms > (
            self._config.reader_stale_after_ms
        ):
            return None
        return feed

    def _contradicted(
        self, event_id: int, regime: WorkloadRegime | None, boot: BootReading
    ) -> bool:
        """Regime が ``IDLE`` / ``COOLDOWN`` のまま ``contradiction_hold_ms`` 以上続いたか。

        0064 §2.7。

        保持時間は ``CLOCK_BOOTTIME`` で数える（0066 §2.2）。Regime がそれ以外（``UNKNOWN`` と
        推定できなかった tick を含む）になったら数え直す。**大分類だけで判定する**（0133 §2.4）。
        """
        if regime not in CONTRADICTING_REGIMES:
            self._reset_contradiction()
            return False
        if self._contradiction_event != event_id or self._contradiction_since_ns is None:
            self._contradiction_event = event_id
            self._contradiction_since_ns = boot.boottime_ns
        assert self._config.contradiction_hold_ms is not None
        held_ns = boot.boottime_ns - self._contradiction_since_ns
        return held_ns >= self._config.contradiction_hold_ms * NS_PER_MS

    def _terminate(
        self,
        observation: HintObservation,
        status: WorkloadHintStatus,
        *,
        tick_id: int,
        ts_ms: int,
        boot: BootReading,
    ) -> HintTick:
        self._terminal[observation.event_id] = status
        self._reset_contradiction()
        LOGGER.info(
            "workload hint が終端になった",
            extra={
                logs.FIELDS_KEY: {
                    "tick_id": tick_id,
                    "event_id": observation.event_id,
                    "status": status.value,
                }
            },
        )
        assert self._mailbox is not None
        try:
            self._mailbox.report(
                HintTransition(
                    event_id=observation.event_id,
                    status=status,
                    ts_ms=ts_ms,
                    boot_id=boot.boot_id,
                    boottime_ns=boot.boottime_ns,
                )
            )
        except Exception:
            # 永続化を頼めなくても、この process の中では終端のまま（再び active にしない）
            LOGGER.exception(
                "workload hint の終端を読み手へ返せなかった",
                extra={logs.FIELDS_KEY: {"event_id": observation.event_id}},
            )
        return HintTick(stage=self._effective, observation=observation, status=status)


def effective_stage(configured: WorkloadHintStage) -> WorkloadHintStage:
    """設定の上限から、この実装で動かせる実効の段階（0133 §2.6）。"""
    if workload_hint_stage_rank(configured) > workload_hint_stage_rank(IMPLEMENTED_STAGE):
        return IMPLEMENTED_STAGE
    return configured


def declared_ms(config: WorkloadHintConfig, *, expected_duration_s: int | None) -> int:
    """ヒントの申告した期限（0064 §2.6）。上限 ``max_age_ms`` は期限の計算の側で掛ける。"""
    if expected_duration_s is None:
        assert config.default_ttl_ms is not None
        return config.default_ttl_ms
    assert config.grace_ms is not None
    return expected_duration_s * 1000 + config.grace_ms


def source_label(config: WorkloadHintConfig, source: str | None) -> str:
    """trace に出す ``source_label``。許可リストに無ければ ``other``（0064 §2.9）。"""
    if source is not None and source in (config.known_sources or ()):
        return source
    return HINT_SOURCE_LABEL_OTHER
