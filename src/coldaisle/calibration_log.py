"""較正の変更の記録を書く・読む合成の起点の部品。#233 / 決定記録 0099。

- **取り込み**（``coldaisle-daemon`` の serial / mock）: 起動時、ソースを読む前に DB ごとの書き手の
  lock を取り、実効の offset の写像を最後の行と比べ、変わったときだけ1行追記する（0099 §2.2）。
  運転中は、覚えた最後の行の時刻より前の sample を保存しない
  （:attr:`IngestCalibrationGate.floor_ms`）
- **Dataset v2**: 記録が期間を覆うこと（被覆）と、変更の時刻（CSV の秒の切り捨ての区間の両端）
- **学習の入口**: 期間の後の変更と、較正ファイルと最後の行の食い違いを拒否する（0096 §5 #4）。
  dataset に残した export の束縛（``ReplayBindingV2``）を本番の DB の ``csv_exports`` から
  計算し直して照合し、example の期間が束縛した日に収まることを確かめる（決定記録 0100 §2.8）。
  元の再生の入力（``--replay-path``）を必須で読み直し、fingerprint と manifest の束縛も
  照合する（決定記録 0112）

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
from coldaisle.control.model.dataset import ReplayBindingV2, SourceRun, ThermalDatasetV2
from coldaisle.csv_export_manifest import (
    ExportRecord,
    export_binding_sha256,
    export_record_sha256,
    replay_binding_of_records,
)
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.replay import read_replay_export_inputs
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
    """DB に対応する取り込みの lock ファイル（実体の path の隣）。

    symlink や相対 path で同じ DB を指しても同じ lock になるよう、実体の path から導く。
    DB ファイル自体は開かない（同じプロセスで DB の別の fd を閉じると SQLite の POSIX lock が
    外れる）。
    """
    resolved = db_path.resolve()
    return resolved.with_name(resolved.name + INGEST_LOCK_SUFFIX)


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
        try:
            links = self._db_path.stat().st_nlink
        except OSError as error:
            raise CalibrationActivationRefused(
                f"DB の状態を読めない: {self._db_path}: {error}"
            ) from error
        if links != 1:
            # hard link の別名からは別の lock ファイルになり、排他が効かない
            # （PR #240 の Codex の指摘）
            raise CalibrationActivationRefused(
                f"DB に hard link の別名がある（リンク数 {links}）。"
                f"取り込みの lock を共有できないので起動しない: {self._db_path}"
            )
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
    return _interval_points(tuple(row.ts_ms for row in history.rows), resolution_ms)


def declared_calibration_change_points(
    declared_ts_ms: tuple[int, ...], *, resolution_ms: int = TIMESTAMP_RESOLUTION_MS
) -> tuple[int, ...]:
    """宣言の ``calibration_changed`` を区間 ``[floor(ts), ts]`` の両端にする（0109 §2.4）。"""
    return _interval_points(declared_ts_ms, resolution_ms)


def _interval_points(ts_values: tuple[int, ...], resolution_ms: int) -> tuple[int, ...]:
    if resolution_ms <= 0:
        raise ValueError(f"時刻の精度は正: {resolution_ms}")
    points: list[int] = []
    for ts_ms in ts_values:
        points.extend((ts_ms // resolution_ms * resolution_ms, ts_ms))
    return tuple(points)


def _overlapping(
    ts_values: tuple[int, ...], *, start_ms: int, end_ms: int, resolution_ms: int
) -> list[int]:
    """区間 ``[floor(ts), ts]`` が期間 ``[start_ms, end_ms]`` と交わる時刻。"""
    if resolution_ms <= 0:
        raise ValueError(f"時刻の精度は正: {resolution_ms}")
    return sorted(
        ts_ms
        for ts_ms in ts_values
        if ts_ms // resolution_ms * resolution_ms <= end_ms and start_ms <= ts_ms
    )


def reject_changes_overlapping(
    history: CalibrationHistory,
    *,
    start_ms: int,
    end_ms: int,
    resolution_ms: int = TIMESTAMP_RESOLUTION_MS,
) -> None:
    """区間 ``[floor(ts_ms), ts_ms]`` が期間 ``[start_ms, end_ms]`` と交われば拒否する。

    0099 §2.6。

    両端の点だけを渡す検査は、期間が区間の中にすっぽり入る（期間が CSV の精度より短い）と
    見逃す。``DatasetSpecV2`` は期間の長さの下限を持たないので、交わりを直接見る
    （PR #240 の Codex の指摘）。
    """
    overlapping = _overlapping(
        tuple(row.ts_ms for row in history.rows),
        start_ms=start_ms,
        end_ms=end_ms,
        resolution_ms=resolution_ms,
    )
    if overlapping:
        raise ValueError(
            "Dataset v2 の期間の中に較正の変更がある（記録の行。CSV の時刻の切り捨ての区間が"
            f"期間と交わる）: {overlapping}"
        )


def reject_declared_changes_overlapping(
    declared_ts_ms: tuple[int, ...],
    *,
    start_ms: int,
    end_ms: int,
    resolution_ms: int = TIMESTAMP_RESOLUTION_MS,
) -> None:
    """宣言の ``calibration_changed`` の区間 ``[floor(ts), ts]`` が期間と交われば拒否する。

    0109 §2.4 / §5 #1。宣言の時刻は本番の ms の軸で、専用 DB の時刻は CSV の書式で切り捨て
    られているので、点のままだと同じ秒の中の変更が期間の終端をすり抜ける。記録の行と同じ規則。
    """
    overlapping = _overlapping(
        declared_ts_ms, start_ms=start_ms, end_ms=end_ms, resolution_ms=resolution_ms
    )
    if overlapping:
        raise ValueError(
            "Dataset v2 の期間の中に較正の変更がある（宣言。CSV の時刻の切り捨ての区間が"
            f"期間と交わる）: {overlapping}"
        )


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


def training_export_ids(dataset: ThermalDatasetV2) -> tuple[str, ...]:
    """学習の入口が ``csv_exports`` から読む ``export_id``（dataset の ``ReplayBindingV2`` から）。

    束縛の無い dataset は ``ValueError``（0100 §2.8。v2 の学習には使わない）。
    """
    bindings = dataset.manifest.replay_bindings
    if bindings is None:
        raise ValueError(
            "export の束縛（ReplayBindingV2）の無い Dataset v2 では学習しない（決定記録 0100 §2.8）"
        )
    return tuple(sorted({export.export_id for b in bindings for export in b.exports}))


def verify_training_export_binding(
    dataset: ThermalDatasetV2,
    rows: Mapping[str, ExportRecord | None],
    *,
    replay_paths: Mapping[str, Path],
) -> None:
    """学習の入口の export の照合（決定記録 0100 §2.8 / 0112 §2.1）。

    ``rows`` は較正の記録と同じ読み取り専用の接続・同じ read transaction で読んだ
    ``csv_exports`` の行（:func:`~coldaisle.store.export_binding.read_training_records`）。
    ``replay_paths`` は source run の ``run_id`` → 元の再生の入力（日次 CSV と manifest。
    ``--replay-path``）で、**必須**（0112 §2.1。dataset の source run と過不足なく渡す）。

    - ``ReplayBindingV2`` の無い dataset は拒否する
    - 各 export の行が無い・行から計算した ``export_record_sha256`` が違う・束縛に写した欄
      （日の区間・``csv_sha256``・``row_seconds_sha256``・timezone）が行と違う、のどれでも拒否する
    - 行から ``export_binding_sha256`` を計算し直して一致しなければ拒否する
    - すべての example の期間 ``[history_start_ms, label_end_ms]`` が、その run に束縛した export の
      日の区間 ``[day_start_ms, day_end_ms)`` の和に収まらなければ拒否する（ID だけを借りた
      dataset を、その export が覆わない期間の example で見つける）

    - 各 source run の元の入力について、fingerprint が ``SourceRun.source_sha256`` と、
      manifest から計算した ``(timezone, export_binding_sha256)`` が ``ReplayBindingV2`` と
      一致しなければ拒否する。
      manifest の無い・一部にだけある入力、``csv_sha256`` の食い違う入力も拒否する（0112 §2.1。
      手で組んだ dataset を元の CSV の bytes まで遡って見つける）

    呼び出し側はこれを通ってから :func:`verify_training_calibration` へ進む。
    """
    training_export_ids(dataset)
    bindings = dataset.manifest.replay_bindings
    assert bindings is not None
    runs = {run.run_id: run for run in dataset.manifest.source_runs}
    if set(replay_paths) != set(runs):
        raise ValueError(
            "学習の入口には source run ごとに元の再生の入力（--replay-path）を過不足なく渡す"
            f"（決定記録 0112 §2.1）: 渡した {sorted(replay_paths)} / run {sorted(runs)}"
        )
    spans: dict[str, list[tuple[int, int]]] = {}
    for binding in bindings:
        bound_rows: list[ExportRecord] = []
        for export in binding.exports:
            row = rows.get(export.export_id)
            if row is None:
                raise ValueError(
                    f"csv_exports に export_id の行が無い（決定記録 0100 §2.8）: {export.export_id}"
                )
            if export_record_sha256(row) != export.export_record_sha256:
                raise ValueError(
                    f"export_record_sha256 が csv_exports の行と一致しない: {export.export_id}"
                )
            copied = (
                row.day_start_ms,
                row.day_end_ms,
                row.csv_sha256,
                row.row_seconds_sha256,
                row.timezone,
            )
            listed = (
                export.day_start_ms,
                export.day_end_ms,
                export.csv_sha256,
                export.row_seconds_sha256,
                binding.local_timezone,
            )
            if copied != listed:
                raise ValueError(
                    f"ReplayBindingV2 の欄が csv_exports の行と違う: {export.export_id}"
                )
            bound_rows.append(row)
        if export_binding_sha256(bound_rows) != binding.export_binding_sha256:
            raise ValueError(
                f"export_binding_sha256 が csv_exports の行から計算した値と違う: {binding.run_id}"
            )
        _verify_replay_input(binding, runs[binding.run_id], replay_paths[binding.run_id])
        spans[binding.run_id] = _merged_spans(
            [(export.day_start_ms, export.day_end_ms) for export in binding.exports]
        )
    for example in dataset.examples:
        start, end = example.history_start_ms, example.label_end_ms
        if not any(low <= start and end < high for low, high in spans[example.source_run_id]):
            raise ValueError(
                "example の期間が束縛した export の日の区間に収まらない"
                f"（決定記録 0100 §2.8）: {example.example_id}"
            )


def _verify_replay_input(binding: ReplayBindingV2, run: SourceRun, replay_path: Path) -> None:
    """元の再生の入力を読み直し、fingerprint と manifest の束縛を照合する（決定記録 0112 §2.1）。"""
    inputs = read_replay_export_inputs(replay_path)
    if inputs.source_sha256 != run.source_sha256:
        raise ValueError(
            f"--replay-path の fingerprint が SourceRun.source_sha256 と一致しない: {run.run_id}"
        )
    if inputs.records is None:
        raise ValueError(
            f"--replay-path に export の manifest が無い（決定記録 0112 §2.1）: {run.run_id}"
        )
    if replay_binding_of_records(inputs.records) != (
        binding.local_timezone,
        binding.export_binding_sha256,
    ):
        raise ValueError(
            "--replay-path の manifest から計算した束縛が ReplayBindingV2 と一致しない"
            f"（決定記録 0112 §2.1）: {run.run_id}"
        )


def _merged_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """``[start, end)`` の区間を、隣り合う・重なるものどうしでつなぐ。"""
    merged: list[tuple[int, int]] = []
    for low, high in sorted(spans):
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged
