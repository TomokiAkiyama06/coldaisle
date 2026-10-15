"""Workload Hint の経過・期限・起動時の取り込みの判定（純粋な関数。#318 / 決定記録 0066 §2.2）。

DB も時計も読まない。読み手（B0 の読み手のスレッド。#319）が `events` の行・終端状態・
取り込みの境界・いまの `BootClock` の値を集めて渡し、ここは条件式だけを持つ。

**経過は同じ boot の中の `CLOCK_BOOTTIME` の差だけで数える。** 壁時計は時刻合わせで戻り、
`CLOCK_MONOTONIC` は suspend 中に進まないので、どちらで数えても経過を過小に見積もり、
期限切れのヒントが蘇る。壁時計の差は起動時の取り込みの**追加の検査**にだけ使い、
残り時間の計算には使わない。

**行の順序は `events.id`（追記の順）で決める。** `ts_ms` の順は使わない。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

NS_PER_MS = 1_000_000


class BackfillRejection(StrEnum):
    """起動時の取り込み・稼働中の初回の取り込みで採らなかった理由（0066 §2.2 の閉じた語彙）。"""

    FUTURE_TS = "future_ts"
    TOO_OLD = "too_old"
    EXPIRED = "expired"
    UNVERIFIABLE_ELAPSED = "unverifiable_elapsed"
    ALREADY_TERMINATED = "already_terminated"
    UNCLEAN_PREVIOUS_RUN = "unclean_previous_run"


@dataclass(frozen=True)
class BootReading:
    """いまの boot id と `CLOCK_BOOTTIME`（ナノ秒）。"""

    boot_id: str
    boottime_ns: int

    def __post_init__(self) -> None:
        if self.boottime_ns < 0:
            raise ValueError("boottime_ns は 0 以上にする")


@dataclass(frozen=True)
class HintRowClock:
    """ヒントの行の時刻の欄。``boot_id`` / ``boottime_ns`` は移行前の行で ``None``。"""

    event_id: int
    ts_ms: int
    boot_id: str | None
    boottime_ns: int | None


@dataclass(frozen=True)
class HintAge:
    """採ったヒントの経過と残り時間（ミリ秒）。``remaining_ms`` は常に正。"""

    age_ms: int
    remaining_ms: int


@dataclass(frozen=True)
class HintPhaseRow:
    """順序づけに要る行の欄。``phase`` は Workload Hint の `start` / `end`。"""

    event_id: int
    phase: Literal["start", "end"]


def select_open_hint(rows: Iterable[HintPhaseRow]) -> int | None:
    """`events.id` の昇順の行に終了・置き換えを当てはめ、最後に残った未完了のヒントを返す。

    有効なヒントは高々1件（0064 §2.6）。`start` は前のヒントを置き換え、`end` は取り消す。
    ``rows`` が `events.id` の厳密な昇順でなければ `ValueError`（時刻の順に並べた列を渡させない）。
    """
    open_event: int | None = None
    previous_id: int | None = None
    for row in rows:
        if previous_id is not None and row.event_id <= previous_id:
            raise ValueError("行は events.id の厳密な昇順で渡す")
        previous_id = row.event_id
        open_event = row.event_id if row.phase == "start" else None
    return open_event


def _elapsed_ms(row: HintRowClock, now: BootReading) -> int | None:
    """同じ boot の `CLOCK_BOOTTIME` の差（ミリ秒、切り上げ）。確かめられなければ ``None``。

    切り上げるのは経過を過小に見積もらないため（残り時間は短くなる側に丸める）。
    """
    if row.boot_id is None or row.boottime_ns is None:
        return None
    if row.boot_id != now.boot_id:
        # 再起動をまたいだ経過は確かめられない
        return None
    elapsed_ns = now.boottime_ns - row.boottime_ns
    if elapsed_ns < 0:
        # 同じ boot で行のほうが新しい。書き手の時計を信じない
        return None
    return -(-elapsed_ns // NS_PER_MS)


def _lifetime_ms(declared_ms: int, max_age_ms: int) -> int:
    if declared_ms <= 0 or max_age_ms <= 0:
        raise ValueError("declared_ms と max_age_ms は正にする")
    return min(declared_ms, max_age_ms)


def evaluate_running_hint(
    row: HintRowClock, *, now: BootReading, declared_ms: int, max_age_ms: int
) -> HintAge | BackfillRejection:
    """稼働中のヒントの経過と残り時間（0066 §2.2）。

    0064 §2.6 の `expires_at` / `age_ms` の式を置き換える。

    期限の起点は読んだ tick ではなく**行の `boottime_ns`**。稼働中に初めて読む行にも、
    以後の tick にも同じ式を使う::

        age_ms       = (いまの CLOCK_BOOTTIME - 行の boottime_ns) / 1e6
        remaining_ms = min(declared_ms, max_age_ms) - age_ms      （0 以下なら expired）

    行の boot id が違う・列が無い・行のほうが新しい場合は `unverifiable_elapsed`。
    """
    lifetime = _lifetime_ms(declared_ms, max_age_ms)
    age_ms = _elapsed_ms(row, now)
    if age_ms is None:
        return BackfillRejection.UNVERIFIABLE_ELAPSED
    remaining_ms = lifetime - age_ms
    if remaining_ms <= 0:
        return BackfillRejection.EXPIRED
    return HintAge(age_ms=age_ms, remaining_ms=remaining_ms)


def evaluate_startup_backfill(
    row: HintRowClock,
    *,
    startup: BootReading,
    startup_wall_ms: int,
    declared_ms: int,
    max_age_ms: int,
    startup_backfill_ms: int,
    boundary_event_id: int,
    already_terminated: bool,
) -> HintAge | BackfillRejection:
    """起動時に選んだ1件のヒントを採るか（0066 §2.2。0064 §2.6 の条件を置き換える）::

        前提                = 起動時の boot_id == 行の boot_id
        boottime_elapsed_ms = startup_boottime_ms - row_boottime_ms
        wall_elapsed_ms     = startup_wall_ms - ts_ms          （追加の検査だけに使う）
        採る条件            = 0 <= boottime_elapsed_ms <= startup_backfill_ms
                              かつ 0 <= wall_elapsed_ms <= startup_backfill_ms
        残り時間            = min(declared_ms, max_age_ms) - boottime_elapsed_ms
                              （0 以下なら採らない）

    残り時間は元の期限（``min(declared_ms, max_age_ms)``）を超えない。
    ``boundary_event_id`` 以下の行は直前の run を確かめられないので採らない。
    """
    if startup_backfill_ms < 0:
        raise ValueError("startup_backfill_ms は 0 以上にする")
    lifetime = _lifetime_ms(declared_ms, max_age_ms)
    if row.event_id <= boundary_event_id:
        return BackfillRejection.UNCLEAN_PREVIOUS_RUN
    if already_terminated:
        return BackfillRejection.ALREADY_TERMINATED
    boottime_elapsed_ms = _elapsed_ms(row, startup)
    if boottime_elapsed_ms is None:
        return BackfillRejection.UNVERIFIABLE_ELAPSED
    wall_elapsed_ms = startup_wall_ms - row.ts_ms
    if wall_elapsed_ms < 0:
        # 壁時計の逆行・書き手の時計のずれ・改ざんのどれでも、経過の分からない行は採らない
        return BackfillRejection.FUTURE_TS
    if boottime_elapsed_ms > startup_backfill_ms or wall_elapsed_ms > startup_backfill_ms:
        return BackfillRejection.TOO_OLD
    remaining_ms = lifetime - boottime_elapsed_ms
    if remaining_ms <= 0:
        return BackfillRejection.EXPIRED
    return HintAge(age_ms=boottime_elapsed_ms, remaining_ms=remaining_ms)
