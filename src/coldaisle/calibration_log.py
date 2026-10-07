"""較正の変更の記録を書く・読む合成の起点の部品。#233 / 決定記録 0099。

- **取り込み**（``coldaisle-daemon`` の serial / mock）: 起動時、ソースを読む前に DB ごとの書き手の
  lock を取り、実効の offset の写像を最後の行と比べ、変わったときだけ1行追記する（0099 §2.2）。
  運転中は、覚えた最後の行の時刻より前の sample を保存しない
  （:attr:`IngestCalibrationGate.floor_ms`）
- **Dataset v2**: 記録が期間を覆うこと（被覆）と、変更の時刻（CSV の秒の切り捨ての区間の両端）
- **学習の入口**: 期間の後の変更と、較正ファイルと最後の行の食い違いを拒否する（0096 §5 #4）

``CalibrationHistory`` は store と合成の起点だけが扱う。``control/model`` へは時刻の列だけを渡す
（0087 §2.6）。API・AI・control・``coldaisle-calibrate`` はここを使わない（書き手は取り込みだけ）。
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from coldaisle import logs
from coldaisle.calibration_offsets import effective_metric_offsets, offsets_sha256
from coldaisle.clock import Clock
from coldaisle.ingest.calibration import Calibration
from coldaisle.store import SqliteStore
from coldaisle.store.calibration_history import (
    SOURCE_KINDS,
    CalibrationActivation,
    CalibrationHistory,
    CalibrationHistoryError,
)
from coldaisle.store.csv_export import TIMESTAMP_RESOLUTION_MS
from coldaisle.store.db import DB_FILE_MODE

LOGGER = logging.getLogger("coldaisle.calibration_log")

INGEST_LOCK_SUFFIX = ".ingest.lock"
"""DB ごとの取り込みの lock ファイルの末尾（``<db>.ingest.lock``）。0099 §2.2 / §5 #12。"""

_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC


class CalibrationActivationRefused(RuntimeError):
    """取り込みを起動しない（lock が取れない・記録できない・時計が戻った）。0099 §2.7 / §5 #3。"""


def ingest_lock_path(db_path: Path) -> Path:
    """DB に対応する取り込みの lock ファイル。"""
    return db_path.with_name(db_path.name + INGEST_LOCK_SUFFIX)


class IngestCalibrationGate:
    """取り込みの起動時の較正の記録と、運転中の時刻の下限（0099 §2.2）。

    :meth:`open` が lock を取り、記録を確定し、``floor_ms`` を覚える。lock は :meth:`close`
    まで（取り込みが終わるまで）持つ。
    """

    def __init__(
        self,
        *,
        db_path: Path,
        source_kind: str,
        calibration: Calibration,
        calibration_file_sha256: str,
    ) -> None:
        if source_kind not in SOURCE_KINDS:
            # replay は較正を当てないので記録を読みも書きもしない（0010 §2.9）
            raise ValueError(f"較正の記録を書く取り込みは serial / mock だけ: {source_kind!r}")
        self._db_path = db_path
        self._source_kind = source_kind
        self._calibration = calibration
        self._calibration_file_sha256 = calibration_file_sha256
        self._lock_fd: int | None = None
        self._floor_ms: int | None = None

    @property
    def floor_ms(self) -> int:
        """最後の行（このとき書いた行を含む）の時刻。これより前の sample は保存しない。"""
        if self._floor_ms is None:
            raise RuntimeError("open() の前に floor_ms を読まない")
        return self._floor_ms

    def open(self, store: SqliteStore, clock: Clock) -> int:
        """lock を取り、記録を確定し、``floor_ms`` を返す。失敗したら lock を放して拒否する。"""
        self._acquire_lock()
        try:
            self._floor_ms = activate_calibration(
                store,
                source_kind=self._source_kind,
                offsets_c=self._calibration.offsets_c,
                calibrated_at=self._calibration.calibrated_at,
                calibration_file_sha256=self._calibration_file_sha256,
                now_ms=clock.now_ms(),
            )
        except BaseException:
            self.close()
            raise
        return self._floor_ms

    def close(self) -> None:
        """lock を放す（何度呼んでもよい）。"""
        if self._lock_fd is None:
            return
        fd, self._lock_fd = self._lock_fd, None
        os.close(fd)

    def _acquire_lock(self) -> None:
        if self._lock_fd is not None:
            raise RuntimeError("lock は取得済み")
        path = ingest_lock_path(self._db_path)
        try:
            fd = os.open(path, _LOCK_FLAGS, DB_FILE_MODE)
        except OSError as error:
            raise CalibrationActivationRefused(
                f"取り込みの lock ファイルを開けない: {path}: {error}"
            ) from error
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            # 古い取り込み（較正 A）が動いたまま B の行を足すと、A の値が B として扱われる
            raise CalibrationActivationRefused(
                f"同じ DB の取り込みが既に動いている（lock が取れない）: {path}"
            ) from error
        self._lock_fd = fd


def calibration_file_sha256(data: bytes) -> str:
    """較正ファイルの bytes の SHA-256（説明用。比べない。0099 §2.4）。"""
    return hashlib.sha256(data).hexdigest()


def activate_calibration(
    store: SqliteStore,
    *,
    source_kind: str,
    offsets_c: Mapping[str, float],
    calibrated_at: str | None,
    calibration_file_sha256: str,
    now_ms: int,
) -> int:
    """実効の写像を最後の行と比べ、変わったときだけ1行追記し、最後の行の時刻を返す（0099 §2.2）。

    比較と追記は1つの書き込みトランザクションで行う。次のときは記録を書かず
    :class:`CalibrationActivationRefused`（取り込みを起動しない。0099 §2.7 / §5 #3）。

    - 既存の行が 0099 §2.5 の検証に外れる・DB に書けない
    - ``now_ms`` が最後の行の時刻**以下**（較正が変わらない起動でも。§2.2 の5）
    - 較正が変わる起動で、``now_ms`` が readings の最大の時刻**以下**（§2.2 の4）
    """
    offsets = effective_metric_offsets(offsets_c)
    digest = offsets_sha256(offsets)
    try:
        with store.transaction():
            rows = store.calibration_activations()
            last = rows[-1] if rows else None
            if last is not None and now_ms <= last.ts_ms:
                raise CalibrationActivationRefused(
                    f"時計（{now_ms}）が較正の記録の最後の行の時刻（{last.ts_ms}）以下。"
                    "時刻が追いつくまで取り込みを起動しない（決定記録 0099 §2.2）"
                )
            if last is not None and last.offsets_sha256 == digest:
                return last.ts_ms
            newest_reading = store.max_reading_ts_ms()
            if newest_reading is not None and now_ms <= newest_reading:
                raise CalibrationActivationRefused(
                    f"較正が変わったが、時計（{now_ms}）が保存済みの readings の最大の時刻"
                    f"（{newest_reading}）以下。記録を書かず取り込みを起動しない"
                    "（決定記録 0099 §2.2）"
                )
            row = CalibrationActivation.next_after(
                last,
                ts_ms=now_ms,
                source_kind=source_kind,
                offsets=offsets,
                calibrated_at=calibrated_at,
                calibration_file_sha256=calibration_file_sha256,
            )
            store.append_calibration_activation(row)
    except CalibrationHistoryError as error:
        raise CalibrationActivationRefused(f"較正の記録が壊れている: {error}") from error
    except sqlite3.Error as error:
        raise CalibrationActivationRefused(f"較正の記録を書けない: {error}") from error
    LOGGER.info(
        "較正の変更を記録した",
        extra={
            logs.FIELDS_KEY: {
                "ts_ms": row.ts_ms,
                "source_kind": source_kind,
                "offsets_sha256": row.offsets_sha256,
                "previous_row_sha256": row.previous_row_sha256,
                "calibrated_at": calibrated_at,
            }
        },
    )
    return row.ts_ms


# ---------------------------------------------------------------- Dataset v2 / 学習の入口


def require_history_covers(history: CalibrationHistory, start_ms: int) -> None:
    """期間の先頭（``start_ms``）以前に少なくとも1行があること（0099 §2.6 の被覆）。"""
    if not any(row.ts_ms <= start_ms for row in history.rows):
        raise ValueError(
            f"較正の履歴が期間を覆っていない（期間の先頭 {start_ms} 以前に記録の行が無い）。"
            "記録が無いことを変更なしと扱わない（決定記録 0099 §2.6 / §2.7）"
        )


def calibration_change_points(
    history: CalibrationHistory, *, resolution_ms: int = TIMESTAMP_RESOLUTION_MS
) -> tuple[int, ...]:
    """各行を区間 ``[floor(ts_ms), ts_ms]`` とし、両端の時刻を返す（0099 §2.6 / §5 #9）。

    再生した専用 DB の時刻は CSV の書式で切り捨てられている。幅は CSV の書式から導いた値。
    """
    if resolution_ms <= 0:
        raise ValueError(f"時刻の精度は正: {resolution_ms}")
    points: list[int] = []
    for row in history.rows:
        points.extend((row.ts_ms // resolution_ms * resolution_ms, row.ts_ms))
    return tuple(points)


def verify_training_calibration(
    history: CalibrationHistory,
    *,
    period_end_ms: int,
    calibration_offsets_c: Mapping[str, float],
) -> CalibrationActivation:
    """学習の入口の検査（0099 §2.6 / 0096 §5 #4）。期間に効いていた最後の行を返す。

    ``period_end_ms`` は学習に使う dataset の期間の終わり（全 example の ``label_end_ms`` の最大。
    0087 §2.6 の期間と同じ）。

    - 期間の終わりより後に行が1つでもあれば拒否する
    - 明示して読んだ較正ファイルの写像が最後の行と一致しなければ拒否する（ファイルを
      書き換えたが取り込みをまだ再起動していない、または取り違えたファイル）
    """
    if not isinstance(history, CalibrationHistory):
        raise TypeError("較正の記録（CalibrationHistory）を明示して渡す")
    last = history.last
    if last is None:
        raise ValueError("較正の記録が無い（記録が無いことを変更なしと扱わない）")
    after = [row.ts_ms for row in history.rows if row.ts_ms > period_end_ms]
    if after:
        raise ValueError(
            f"dataset の期間の終わり（{period_end_ms}）より後に較正の変更がある: {after}"
            "（決定記録 0096 §5 #4）"
        )
    if offsets_sha256(effective_metric_offsets(calibration_offsets_c)) != last.offsets_sha256:
        raise ValueError(
            "較正ファイルの実効の写像が記録の最後の行と一致しない"
            "（取り込みを再起動していない、またはファイルの取り違え。決定記録 0099 §2.6）"
        )
    return last
