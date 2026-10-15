"""Workload Hint の経過・期限・起動時の取り込みの条件式（#318 / 決定記録 0066 §2.2）。

実機なし。boot id と CLOCK_BOOTTIME は値で渡す。守りたいこと:

1. 壁時計が戻っても、古いヒントが満額以上で蘇らない（残り時間は元の期限を超えない）
2. 経過は同じ boot の CLOCK_BOOTTIME の差だけで数える。boot が違う・列が無い行は採らない
3. 壁時計の差は追加の検査（負なら future_ts、上限超えなら too_old）
4. 境界以下・終端済みの行は採らない
5. 行の順序は events.id で、最後に残った未完了のヒントを1つ選ぶ
"""

from __future__ import annotations

import itertools

import pytest

from coldaisle.clock import SIMULATED_BOOT_ID
from coldaisle.control.supervisor.hint_clock import (
    BackfillRejection,
    BootReading,
    HintAge,
    HintPhaseRow,
    HintRowClock,
    evaluate_running_hint,
    evaluate_startup_backfill,
    select_open_hint,
)

OTHER_BOOT_ID = "28ffabcd-0000-4000-8000-00000000beef"
MS = 1_000_000
WALL = 1_787_616_000_000
START_NS = 50_000 * MS
"""起動時の CLOCK_BOOTTIME（50 秒）。"""

LIMITS = {"declared_ms": 3_600_000, "max_age_ms": 7_200_000, "startup_backfill_ms": 600_000}


def row(
    *,
    event_id: int = 10,
    ts_ms: int = WALL,
    boot_id: str | None = SIMULATED_BOOT_ID,
    boottime_ns: int | None = START_NS,
) -> HintRowClock:
    return HintRowClock(event_id=event_id, ts_ms=ts_ms, boot_id=boot_id, boottime_ns=boottime_ns)


def backfill(
    hint: HintRowClock,
    *,
    elapsed_ms: int,
    wall_elapsed_ms: int | None = None,
    boundary: int = 0,
    terminated: bool = False,
    **overrides: int,
) -> HintAge | BackfillRejection:
    limits = {**LIMITS, **overrides}
    return evaluate_startup_backfill(
        hint,
        startup=BootReading(SIMULATED_BOOT_ID, START_NS + elapsed_ms * MS),
        startup_wall_ms=hint.ts_ms + (elapsed_ms if wall_elapsed_ms is None else wall_elapsed_ms),
        boundary_event_id=boundary,
        already_terminated=terminated,
        **limits,
    )


# ---------------------------------------------------------------- 起動時の取り込み


def test_a_recent_hint_is_backfilled_with_the_elapsed_time_subtracted():
    assert backfill(row(), elapsed_ms=60_000) == HintAge(age_ms=60_000, remaining_ms=3_540_000)


def test_the_remaining_time_never_exceeds_the_original_lifetime():
    """壁時計が戻った場合も含め、残り時間は min(declared_ms, max_age_ms) を超えない。"""
    for elapsed, wall in itertools.product([0, 1, 59_999, 600_000], [0, 1, 300_000, 600_000]):
        result = backfill(row(), elapsed_ms=elapsed, wall_elapsed_ms=wall)
        if isinstance(result, HintAge):
            assert result.remaining_ms <= min(LIMITS["declared_ms"], LIMITS["max_age_ms"])
            assert result.remaining_ms == LIMITS["declared_ms"] - elapsed, "壁時計の差で数えない"


def test_max_age_caps_the_declared_duration():
    result = backfill(row(), elapsed_ms=1_000, declared_ms=10_000_000, max_age_ms=5_000)
    assert result == HintAge(age_ms=1_000, remaining_ms=4_000)


def test_a_future_wall_clock_row_is_not_taken():
    """壁時計の逆行（行の時刻が起動時より未来）は経過が分からない。0 に丸めない。"""
    assert backfill(row(), elapsed_ms=1_000, wall_elapsed_ms=-1) is BackfillRejection.FUTURE_TS


def test_an_underestimated_wall_clock_does_not_extend_the_hint():
    """壁時計が戻って ts_ms を少しだけ追い越した。

    壁時計の差は小さくても、経過は CLOCK_BOOTTIME で数える。
    """
    result = backfill(
        row(), elapsed_ms=3_599_000, wall_elapsed_ms=10, startup_backfill_ms=4_000_000
    )
    assert result == HintAge(age_ms=3_599_000, remaining_ms=1_000)
    assert (
        backfill(row(), elapsed_ms=3_600_000, wall_elapsed_ms=10, startup_backfill_ms=4_000_000)
        is BackfillRejection.EXPIRED
    )


@pytest.mark.parametrize(("elapsed", "wall"), [(600_001, 0), (0, 600_001), (600_001, 600_001)])
def test_either_clock_beyond_the_backfill_window_is_too_old(elapsed, wall):
    assert backfill(row(), elapsed_ms=elapsed, wall_elapsed_ms=wall) is BackfillRejection.TOO_OLD


def test_the_backfill_window_is_inclusive():
    assert isinstance(backfill(row(), elapsed_ms=600_000), HintAge)


def test_an_expired_hint_is_not_taken():
    result = backfill(row(), elapsed_ms=500_000, declared_ms=500_000)
    assert result is BackfillRejection.EXPIRED


@pytest.mark.parametrize(
    "hint",
    [
        row(boot_id=None, boottime_ns=None),  # 移行前の行
        row(boot_id=OTHER_BOOT_ID),  # 再起動をまたいだ
        row(boottime_ns=START_NS + 1),  # 同じ boot で行のほうが新しい
    ],
)
def test_unverifiable_elapsed_is_not_taken(hint):
    assert backfill(hint, elapsed_ms=0) is BackfillRejection.UNVERIFIABLE_ELAPSED


def test_rows_at_or_below_the_boundary_are_not_taken():
    assert backfill(row(event_id=10), elapsed_ms=0, boundary=10) is (
        BackfillRejection.UNCLEAN_PREVIOUS_RUN
    )
    assert isinstance(backfill(row(event_id=11), elapsed_ms=0, boundary=10), HintAge)


def test_terminated_rows_are_not_taken():
    assert backfill(row(), elapsed_ms=0, terminated=True) is BackfillRejection.ALREADY_TERMINATED


def test_sub_millisecond_elapsed_is_rounded_up():
    """経過を過小に見積もらない（残り時間は短くなる側に丸める）。"""
    result = evaluate_running_hint(
        row(), now=BootReading(SIMULATED_BOOT_ID, START_NS + 1), declared_ms=10, max_age_ms=10
    )
    assert result == HintAge(age_ms=1, remaining_ms=9)


@pytest.mark.parametrize(
    "bad", [{"declared_ms": 0}, {"max_age_ms": -1}, {"startup_backfill_ms": -1}]
)
def test_invalid_limits_are_rejected(bad):
    with pytest.raises(ValueError):
        backfill(row(), elapsed_ms=0, **bad)


# ---------------------------------------------------------------- 稼働中


def test_running_age_starts_at_the_row_not_at_the_read():
    """読まれるまでの間（suspend を含む）の経過も差し引く。"""
    now = BootReading(SIMULATED_BOOT_ID, START_NS + 3_000_000 * MS)
    assert evaluate_running_hint(row(), now=now, declared_ms=3_600_000, max_age_ms=7_200_000) == (
        HintAge(age_ms=3_000_000, remaining_ms=600_000)
    )
    late = BootReading(SIMULATED_BOOT_ID, START_NS + 3_600_000 * MS)
    assert (
        evaluate_running_hint(row(), now=late, declared_ms=3_600_000, max_age_ms=7_200_000)
        is BackfillRejection.EXPIRED
    )


@pytest.mark.parametrize("hint", [row(boot_id=None, boottime_ns=None), row(boot_id=OTHER_BOOT_ID)])
def test_running_rows_from_another_boot_are_unverifiable(hint):
    now = BootReading(SIMULATED_BOOT_ID, START_NS)
    assert (
        evaluate_running_hint(hint, now=now, declared_ms=1_000, max_age_ms=1_000)
        is BackfillRejection.UNVERIFIABLE_ELAPSED
    )


# ---------------------------------------------------------------- 行の順序


def test_select_open_hint_applies_end_and_supersede_in_append_order():
    rows = [HintPhaseRow(1, "start"), HintPhaseRow(2, "start"), HintPhaseRow(3, "end")]
    assert select_open_hint(rows) is None
    assert select_open_hint(rows[:2]) == 2, "新しい start が古いものを置き換える"
    assert select_open_hint([*rows, HintPhaseRow(5, "start")]) == 5
    assert select_open_hint([]) is None


@pytest.mark.parametrize(
    "rows",
    [
        [HintPhaseRow(2, "start"), HintPhaseRow(1, "end")],
        [HintPhaseRow(2, "start"), HintPhaseRow(2, "end")],
    ],
)
def test_select_open_hint_refuses_non_append_order(rows):
    with pytest.raises(ValueError, match="昇順"):
        select_open_hint(rows)


def test_rejection_vocabulary_is_closed():
    assert {r.value for r in BackfillRejection} == {
        "future_ts",
        "too_old",
        "expired",
        "unverifiable_elapsed",
        "already_terminated",
        "unclean_previous_run",
    }
