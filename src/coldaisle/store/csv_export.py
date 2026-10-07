"""日次CSVエクスポート（FR-205）。#10

**従来の出力と同じ形式**を維持する。`~/server_sensor_logs/` に残っていた
実ファイル（2026-08-23 / 24 の記録）に合わせた。

```text
timestamp,room_temp,room_humidity,front_intake,gpu_intake,gpu_exhaust,top_exhaust,rear_exhaust
2026-08-23T20:16:40,24.5,60.0,24.31,23.94,24.0,24.06,24.19
2026-08-23T20:35:31,,,24.75,24.31,25.25,24.5,24.62
```

- 列名は**デバイスのチャネル名**（`air.` を付けない）。列順も従来どおり
- 時刻はローカル時刻の ISO8601、**秒まで・オフセット無し**
- 取得できなかった値は**空欄**。従来の出力にも `-127.00` や `85.00` は
  1件も現れておらず、異常値は空欄になっていた

表計算で開く人間向けの出力であり、機械が読む正本は SQLite のほう。
品質フラグや `suspect` の値が要るときはそちらを見る。

**日境界はローカル時刻で切る。** 「その日のCSV」の日は生活時間の日であって
UTC の日ではない。保存は UTC ミリ秒（D-05）なので、ここで変換する。
タイムゾーンは設定から受け取り、ホストの設定に依存させない。

**export ごとに manifest を CSV の横に置き、同じ内容を元の DB の ``csv_exports`` に追記する**
（決定記録 0100 §2.1 / §2.6）。書く順序は「一時ファイル → DB の commit → 古い manifest の削除 →
CSV の rename → manifest の rename」で、日ごとのプロセス間 lock の中で行う。どこで落ちても
残るのは「manifest の無い CSV」か「どの CSV とも対にならない ``csv_exports`` の行」で、
どちらも Dataset v2 には使えない側（安全側）に倒れる。
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from zoneinfo import ZoneInfo

from coldaisle import csv_export_manifest as manifest
from coldaisle.channels import METRIC_TO_CHANNEL, SAMPLE_CHANNELS
from coldaisle.csv_export_manifest import ExportRecord
from coldaisle.store.db import SqliteStore
from coldaisle.store.models import Quality

TIMESTAMP_COLUMN = "timestamp"
TIMESTAMP_FORMAT = manifest.TIMESTAMP_FORMAT
"""従来の出力に合わせる。オフセットもミリ秒も付かない。

正本は ``csv_export_manifest``（再生と同じ写像を使う。0100 §2.4）。
"""

TABLE = "csv_exports"

_LOCK_POLL_S = 0.05
"""lock を取り直す間隔。待つ上限（設定の値）より短ければよく、運用の値ではない。"""


class CsvExportError(RuntimeError):
    """export を行わなかった。CSV も manifest も ``csv_exports`` の行も書いていない。"""


class CsvExportLockTimeout(CsvExportError):
    """同じ日の export が lock を持ったまま、待つ上限を過ぎた（0100 §2.1 / §5 #17）。"""


def _timestamp_resolution_ms(timestamp_format: str) -> int:
    """書式が落とす時刻の幅（ms）。秒までの書式なら 1000、ミリ秒まで残る書式なら 1。

    再生した時刻は CSV の書式で切り捨てられている。較正の変更の記録（0099 §2.6）は、本番の
    ms の時刻を切り捨ての区間として扱うので、その幅を**書式から導く**（AGENTS.md ルール 9。
    書式と幅を別々に書くと片方だけ直る）。秒の直前の時刻を書式に通し、読み戻して失った分を測る。
    """
    probe = datetime(2000, 1, 1, 0, 0, 59, 999_999)
    lost = probe - datetime.strptime(probe.strftime(timestamp_format), timestamp_format)
    return int(lost.total_seconds() * 1000) + 1


TIMESTAMP_RESOLUTION_MS = _timestamp_resolution_ms(TIMESTAMP_FORMAT)
"""CSV の時刻の精度（ms）。決定記録 0099 §2.6 / §5 #9。"""


def day_bounds_ms(day: date, tz: ZoneInfo) -> tuple[int, int]:
    """その日の `[開始, 終了)` を Unix ミリ秒で返す。"""
    start = datetime.combine(day, day_time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def export_day(
    store: SqliteStore,
    day: date,
    *,
    tz: ZoneInfo,
    out_dir: Path,
    lock_timeout_s: float,
) -> Path:
    """1日ぶんを1ファイルへ書き出し、manifest と ``csv_exports`` の行を残す。CSV のパスを返す。

    行は同一時刻のサンプル。決定記録 0002 §2.3 により1サンプルの全メトリクスは
    同じ `ts_ms` を持つので、そのまま横に並ぶ。

    デバイス由来でないメトリクス（`sys.dropped_samples` など）は**書かない。**
    従来の列だけを保つ。欠測や取りこぼしの分析は SQLite 側で行う。

    ``tz`` は名前（``ZoneInfo.key``）を持つこと。manifest と ``csv_exports`` にはその名前を
    そのまま書く（0100 §2.2）。``lock_timeout_s`` は同じ日の lock を待つ上限
    （設定の値。0100 §2.1）。
    取れなければ :class:`CsvExportLockTimeout` で、何も書かない。dataset 専用 DB（bind 済み）では
    :class:`CsvExportError`（0100 §2.6）。
    """
    if not tz.key:
        raise CsvExportError("名前の無い timezone では export しない（manifest に書けない）")
    if not lock_timeout_s > 0:
        raise CsvExportError(f"lock を待つ上限が正でない: {lock_timeout_s!r}")
    if store.dataset_source_run() is not None:
        raise CsvExportError("dataset専用DBからは export しない（決定記録 0100 §2.6）")

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / manifest.csv_name_for(day)
    manifest_path = out_dir / manifest.manifest_name_for(day)
    with _day_lock(out_dir / manifest.lock_name_for(day), lock_timeout_s):
        start_ms, end_ms = day_bounds_ms(day, tz)
        csv_bytes, seconds = _render(store, start_ms, end_ms, tz)
        record = ExportRecord(
            export_id=f"export-{secrets.token_hex(16)}",
            csv_name=csv_path.name,
            csv_sha256=_sha256(csv_bytes),
            day=day.isoformat(),
            timezone=tz.key,
            day_start_ms=start_ms,
            day_end_ms=end_ms,
            timestamp_format=TIMESTAMP_FORMAT,
            row_count=len(seconds),
            row_seconds_sha256=manifest.row_seconds_sha256(seconds),
        )
        temporaries: list[Path] = []
        try:
            # (1) 一時ファイルへ書いて fsync。名前は `.` で始め、glob に掛からない
            temporaries.append(_write_temporary(out_dir, csv_path.name, csv_bytes))
            temporaries.append(
                _write_temporary(out_dir, manifest_path.name, record.manifest_bytes())
            )
            # (2) 元の DB へ追記して commit
            _append_record(store, record)
            # (3) 古い manifest を消してから、CSV → manifest の順に rename する。
            # 古い manifest が新しい CSV と対になる瞬間を作らない（0100 §2.1）
            if manifest_path.exists() or manifest_path.is_symlink():
                manifest_path.unlink()
                _fsync_directory(out_dir)
            os.replace(temporaries[0], csv_path)
            _fsync_directory(out_dir)
            os.replace(temporaries[1], manifest_path)
            _fsync_directory(out_dir)
            temporaries.clear()
        finally:
            for leftover in temporaries:
                leftover.unlink(missing_ok=True)
    return csv_path


def manifest_path_for(csv_path: Path) -> Path:
    """export した CSV の横の manifest のパス。"""
    day = date.fromisoformat(csv_path.name.removeprefix(manifest.CSV_PREFIX).removesuffix(".csv"))
    return csv_path.with_name(manifest.manifest_name_for(day))


def read_csv_export(connection: sqlite3.Connection, export_id: str) -> ExportRecord | None:
    """``csv_exports`` の1行を :class:`ExportRecord` として読む。無ければ ``None``。

    ``export_record_sha256``（0100 §2.8）は manifest からも、この行からも同じ関数で計算する。
    """
    columns = ", ".join(manifest.EXPORT_FIELDS)
    row = connection.execute(
        f"SELECT {columns} FROM {TABLE} WHERE export_id = ?", (export_id,)
    ).fetchone()
    if row is None:
        return None
    return ExportRecord.model_validate(dict(zip(manifest.EXPORT_FIELDS, tuple(row), strict=True)))


def _render(
    store: SqliteStore, start_ms: int, end_ms: int, tz: ZoneInfo
) -> tuple[bytes, list[int]]:
    """CSV の bytes と、各行の絶対時刻（UTC の Unix 秒）の列を作る。

    hash を取る bytes と書く bytes を同じものにするため、先にメモリ上で組み立てる
    （1日ぶんは数 MB に収まる）。
    """
    rows = store.connection.execute(
        "SELECT ts_ms, metric, value, quality FROM readings "
        "WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms",
        (start_ms, end_ms),
    ).fetchall()

    pivoted: dict[int, dict[str, float | None]] = {}
    for row in rows:
        channel = METRIC_TO_CHANNEL.get(row["metric"])
        if channel is None:
            continue
        value = row["value"] if row["quality"] == Quality.OK.value else None
        pivoted.setdefault(int(row["ts_ms"]), {})[channel] = value

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([TIMESTAMP_COLUMN, *SAMPLE_CHANNELS])
    seconds: list[int] = []
    for ts_ms in sorted(pivoted):
        second = manifest.export_second(ts_ms)
        seconds.append(second)
        values = pivoted[ts_ms]
        writer.writerow(
            [
                manifest.format_local(second, tz),
                *("" if values.get(c) is None else values[c] for c in SAMPLE_CHANNELS),
            ]
        )
    return buffer.getvalue().encode("utf-8"), seconds


def _append_record(store: SqliteStore, record: ExportRecord) -> None:
    """``csv_exports`` に1行を追記して commit する。bind 済みの DB には書かない（0100 §2.6）。"""
    values = record.model_dump()
    columns = (*manifest.EXPORT_FIELDS, "exported_ms")
    placeholders = ", ".join("?" for _ in columns)
    with store.transaction():
        # 入口の検査の後に bind されていないかを、書く transaction の中で確かめ直す
        if store.dataset_source_run() is not None:
            raise CsvExportError("dataset専用DBからは export しない（決定記録 0100 §2.6）")
        store.connection.execute(
            f"INSERT INTO {TABLE} ({', '.join(columns)}) VALUES ({placeholders})",
            (*(values[name] for name in manifest.EXPORT_FIELDS), store.clock.now_ms()),
        )


@contextmanager
def _day_lock(path: Path, timeout_s: float) -> Iterator[None]:
    """その日の export のプロセス間 lock（``flock`` の排他）。上限を過ぎたら何もせず拒否する。

    同じ日の export が並行すると、SQLite は commit を順に並べても rename の順序は守らないので、
    CSV B と manifest A が対になって残りうる（0100 §2.1 / §5 #17）。lock ファイルは消さない
    （消すと、消した後に別のプロセスが別の inode で lock を取れてしまう）。
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CsvExportLockTimeout(
                        f"同じ日の export が lock を持っている（{timeout_s} 秒待った）: {path.name}"
                    ) from None
                time.sleep(min(_LOCK_POLL_S, remaining))
        yield
    finally:
        # close で flock も外れる
        os.close(fd)


def _write_temporary(directory: Path, final_name: str, payload: bytes) -> Path:
    """同じディレクトリの一時ファイルへ書いて fsync する。

    mode は従来の ``open("w")`` と同じ（``0o666`` から umask を引いたもの）にする。
    ``mkstemp`` の ``0o600`` にすると、CSV を読む人の権限が変わる。
    """
    path = directory / f".{final_name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    return path


def _fsync_directory(directory: Path) -> None:
    """rename と unlink を電源断の後にも残す。

    順序（古い manifest の削除 → CSV → manifest）を電源断の後にも保つため。
    """
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
