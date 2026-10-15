"""Workload Hint の終端状態・run の記録・取り込みの境界（L1。#318 / 決定記録 0066 §2.2）。

書き手は `coldaisle-fand`（合成の起点）だけ。`events` とは**別の接続**で、ここにある
4つの表（`0015_workload_hint_state.sql`）にだけ書く。`events` には書かない
（書き込みの入口は 0045 のソケットだけ）。`coldaisle.control` はこのモジュールを import しない。

守ること（0066 §2.2）:

- **run の順序は追記順の番号 `run_seq` で決める。** 開始時刻（壁時計・boot id・`CLOCK_BOOTTIME`）で
  並べない。「直前の run」は `run_seq` が最大の開始記録
- **run の開始記録と取り込みの境界は1つのトランザクションで書く。** 失敗したら何も書かない
  （次の起動で、異常だった run が引き続き「直前の run」に見え、境界はそこで改めて作られる）
- **確かめられない状態を「終端していない」と扱わない。** 直前の run の終了記録が無い・2件以上・
  対応する開始記録が無い・状態が既知の値以外、はどれも異常終了と同じ
- **読めないときは空と扱わない。** 照会の失敗・想定外の値は `HintStateUnreadableError` にし、
  呼び出し側はヒントを prior に使わない（`hint_prior_skipped: hint_state_unreadable`）
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from coldaisle.clock import validate_boot_id

_RUN_ID = re.compile(r"^[0-9a-f]{32}$")


class HintTerminalState(StrEnum):
    """ヒントの終端状態の閉じた集合（0066 §2.2 / 0133 §2.4）。失効は一方向。"""

    CONTRADICTED = "contradicted"
    EXPIRED = "expired"
    ENDED = "ended"
    SUPERSEDED = "superseded"
    UNSUPPORTED_VERSION = "unsupported_version"


class RunEndStatus(StrEnum):
    """run の終了記録の状態。`incomplete` は終端状態の書き込みに1回でも失敗した run。"""

    CLEAN = "clean"
    INCOMPLETE = "incomplete"


class PreviousRunState(StrEnum):
    """起動時に見た「直前の run」の状態。`clean` 以外では取り込みの境界を作る。

    `none`（run の記録が1件も無い）も確かめられない状態として境界を作る。
    この表ができる前の run が終端状態を書いていたかは分からないため。
    """

    NONE = "none"
    CLEAN = "clean"
    INCOMPLETE = "incomplete"
    ABNORMAL = "abnormal"


class HintStateError(RuntimeError):
    """ヒントの状態の表を扱えない。"""


class HintStateUnreadableError(HintStateError):
    """照会に失敗した、または想定外の値を読んだ。空（該当なし）とは扱わない。"""


class RunStartNotDurableError(HintStateError):
    """run の開始記録（と境界）を永続化できなかった。何も書いていない。"""


class HintStateWriteError(HintStateError):
    """終端状態・終了記録を書けなかった。"""


@dataclass(frozen=True)
class BootStamp:
    """書いた時点の boot id と `CLOCK_BOOTTIME`（ナノ秒）。"""

    boot_id: str
    boottime_ns: int

    def __post_init__(self) -> None:
        validate_boot_id(self.boot_id)
        if self.boottime_ns < 0:
            raise ValueError("boottime_ns は 0 以上にする")


@dataclass(frozen=True)
class RunEndRow:
    """読んだ終了記録の1行（検証前。状態の値は表の CHECK に頼らず読む側でも確かめる）。"""

    run_seq: int
    status: str


@dataclass(frozen=True)
class PreviousRun:
    """直前の run の判定。`run_seq` は `state` が `none` のときだけ ``None``。"""

    run_seq: int | None
    state: PreviousRunState


@dataclass(frozen=True)
class RunStart:
    """永続化できた run の開始。``boundary_event_id`` はこの時点の実効の境界（無ければ 0）。"""

    run_seq: int
    previous: PreviousRun
    boundary_event_id: int


@dataclass(frozen=True)
class TerminalStateRecord:
    """終端状態の1行。"""

    event_id: int
    state: HintTerminalState
    run_seq: int
    ts_ms: int
    boot: BootStamp


def classify_previous_run(latest_run_seq: int | None, end_rows: Sequence[RunEndRow]) -> PreviousRun:
    """直前の run（`run_seq` の最大）とその終了記録から、状態を決める（0066 §2.2）。

    ``end_rows`` は ``latest_run_seq`` を指す終了記録すべて。終了記録が無い・2件以上・
    別の run を指す・状態が既知の値以外、のどれも異常終了と同じに扱う。
    """
    if latest_run_seq is None:
        # 開始記録が1件も無い。終了記録だけが残っていても、確かめられない状態として境界を作る
        return PreviousRun(run_seq=None, state=PreviousRunState.NONE)
    if len(end_rows) != 1 or end_rows[0].run_seq != latest_run_seq:
        return PreviousRun(run_seq=latest_run_seq, state=PreviousRunState.ABNORMAL)
    status = end_rows[0].status
    if status == RunEndStatus.CLEAN.value:
        return PreviousRun(run_seq=latest_run_seq, state=PreviousRunState.CLEAN)
    if status == RunEndStatus.INCOMPLETE.value:
        return PreviousRun(run_seq=latest_run_seq, state=PreviousRunState.INCOMPLETE)
    return PreviousRun(run_seq=latest_run_seq, state=PreviousRunState.ABNORMAL)


class WorkloadHintStateTables:
    """終端状態・run の記録・取り込みの境界を読み書きする（1接続＝1インスタンス）。

    接続は自動コミット（`isolation_level=None`）であること。トランザクションはここで張る。
    `coldaisle-fand` は `events` を読む接続とは別の接続でこれを作る（0066 §2.2）。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def _write(self) -> Iterator[None]:
        # BEGIN IMMEDIATE: 判定（直前の run・境界）と書き込みの間に他の書き手を割り込ませない
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ run

    def start_run(self, *, run_id: str, ts_ms: int, boot: BootStamp) -> RunStart:
        """run の開始記録を書き、必要なら取り込みの境界を同じトランザクションで足す。

        直前の run が `clean` でなければ、その時点の `events.id` の最大値を境界にする
        （既存の境界より小さくはしない）。失敗したら何も書かず `RunStartNotDurableError`。
        呼び出し側はその run の間ヒントを prior に使わない（`run_start_not_durable`）。
        """
        if not _RUN_ID.match(run_id):
            raise ValueError("run_id は 32 桁の小文字の16進にする")
        if ts_ms < 0:
            raise ValueError("ts_ms は 0 以上にする")
        try:
            with self._write():
                previous = self._previous_run()
                cursor = self._conn.execute(
                    "INSERT INTO workload_hint_runs "
                    "(run_id, started_ts_ms, boot_id, started_boottime_ns) VALUES (?, ?, ?, ?)",
                    (run_id, ts_ms, boot.boot_id, boot.boottime_ns),
                )
                run_seq = cursor.lastrowid
                if run_seq is None:
                    raise RunStartNotDurableError("run_seq の採番に失敗した")
                if previous.state is not PreviousRunState.CLEAN:
                    newest_event = self._conn.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM events"
                    ).fetchone()[0]
                    through = max(int(newest_event), self._boundary())
                    self._conn.execute(
                        "INSERT INTO workload_hint_backfill_boundaries "
                        "(run_seq, previous_run_seq, previous_run_state, through_event_id) "
                        "VALUES (?, ?, ?, ?)",
                        (run_seq, previous.run_seq, previous.state.value, through),
                    )
                boundary = self._boundary()
        except RunStartNotDurableError:
            raise
        except (sqlite3.Error, HintStateUnreadableError) as exc:
            raise RunStartNotDurableError("run の開始記録を永続化できなかった") from exc
        return RunStart(run_seq=int(run_seq), previous=previous, boundary_event_id=boundary)

    def finish_run(
        self, *, run_seq: int, status: RunEndStatus, ts_ms: int, boot: BootStamp
    ) -> None:
        """run の終了記録を書く。終端状態の書き込みに1回でも失敗した run は `incomplete`。"""
        if ts_ms < 0:
            raise ValueError("ts_ms は 0 以上にする")
        try:
            with self._write():
                self._conn.execute(
                    "INSERT INTO workload_hint_run_ends "
                    "(run_seq, status, ended_ts_ms, boot_id, ended_boottime_ns) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_seq, RunEndStatus(status).value, ts_ms, boot.boot_id, boot.boottime_ns),
                )
        except sqlite3.Error as exc:
            raise HintStateWriteError("run の終了記録を書けなかった") from exc

    def previous_run(self) -> PreviousRun:
        """いまの「直前の run」（`run_seq` の最大）の状態。読めなければ例外。"""
        try:
            return self._previous_run()
        except sqlite3.Error as exc:
            raise HintStateUnreadableError("run の記録を読めない") from exc

    def _previous_run(self) -> PreviousRun:
        row = self._conn.execute("SELECT MAX(run_seq) FROM workload_hint_runs").fetchone()
        latest = _optional_int(row[0])
        if latest is None:
            # 終了記録の表も読めることを確かめる（読めない表を「空」と扱わない）
            self._conn.execute("SELECT 1 FROM workload_hint_run_ends LIMIT 1").fetchall()
            return classify_previous_run(None, ())
        ends = self._conn.execute(
            "SELECT run_seq, status FROM workload_hint_run_ends WHERE run_seq = ?", (latest,)
        ).fetchall()
        return classify_previous_run(latest, [_end_row(r) for r in ends])

    # ------------------------------------------------------------------ 境界

    def backfill_boundary(self) -> int:
        """実効の取り込みの境界（全行の最大値。無ければ 0）。これ以下の event_id は取り込まない。"""
        try:
            return self._boundary()
        except sqlite3.Error as exc:
            raise HintStateUnreadableError("取り込みの境界を読めない") from exc

    def _boundary(self) -> int:
        row = self._conn.execute(
            "SELECT MAX(through_event_id) FROM workload_hint_backfill_boundaries"
        ).fetchone()
        value = _optional_int(row[0])
        return 0 if value is None else value

    # ------------------------------------------------------------------ 終端状態

    def record_terminal_state(
        self,
        *,
        event_id: int,
        state: HintTerminalState,
        run_seq: int,
        ts_ms: int,
        boot: BootStamp,
    ) -> TerminalStateRecord:
        """ヒントの終端状態を1行書き、保存されている行を返す。

        同じ ``event_id`` が既にあれば書かずに**既存の行**を返す（失効は一方向で、先に書いた
        終端状態が残る）。書けなければ `HintStateWriteError`。呼び出し側はその run の終了記録を
        `incomplete` にする。
        """
        if ts_ms < 0:
            raise ValueError("ts_ms は 0 以上にする")
        try:
            with self._write():
                self._conn.execute(
                    "INSERT INTO workload_hint_terminal_states "
                    "(event_id, state, run_seq, ts_ms, boot_id, boottime_ns) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING",
                    (
                        event_id,
                        HintTerminalState(state).value,
                        run_seq,
                        ts_ms,
                        boot.boot_id,
                        boot.boottime_ns,
                    ),
                )
                stored = self._terminal_state(event_id)
        except (sqlite3.Error, HintStateUnreadableError) as exc:
            raise HintStateWriteError("終端状態を書けなかった") from exc
        if stored is None:
            raise HintStateWriteError("書いた終端状態を読み戻せない")
        return stored

    def terminal_state(self, event_id: int) -> TerminalStateRecord | None:
        """``event_id`` の終端状態。無ければ ``None``。

        読めないときは例外にし、``None``（該当なし）にしない。
        """
        try:
            return self._terminal_state(event_id)
        except sqlite3.Error as exc:
            raise HintStateUnreadableError("終端状態を読めない") from exc

    def _terminal_state(self, event_id: int) -> TerminalStateRecord | None:
        row = self._conn.execute(
            "SELECT event_id, state, run_seq, ts_ms, boot_id, boottime_ns "
            "FROM workload_hint_terminal_states WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return TerminalStateRecord(
                event_id=_strict_int(row[0]),
                state=HintTerminalState(row[1]),
                run_seq=_strict_int(row[2]),
                ts_ms=_strict_int(row[3]),
                boot=BootStamp(boot_id=str(row[4]), boottime_ns=_strict_int(row[5])),
            )
        except (TypeError, ValueError) as exc:
            raise HintStateUnreadableError("終端状態の行が想定外の値を持つ") from exc


def _end_row(row: sqlite3.Row | tuple[object, ...]) -> RunEndRow:
    try:
        return RunEndRow(run_seq=_strict_int(row[0]), status=str(row[1]))
    except (TypeError, ValueError) as exc:
        raise HintStateUnreadableError("終了記録の行が想定外の値を持つ") from exc


def _strict_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("整数ではない")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return _strict_int(value)
    except TypeError as exc:
        raise HintStateUnreadableError("想定外の値を読んだ") from exc
