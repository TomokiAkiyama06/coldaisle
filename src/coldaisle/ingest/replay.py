"""ReplaySource: 既存CSVの再生（L0）。#7

`~/server_sensor_logs/sensors_YYYY-MM-DD.csv` を読み、デバイスが送ってきたのと
同じ形（`RawSample`）に戻す。試作時の記録が回帰テストのゴールデンデータになり、
本番開始後は**当日のCSVからバグを再現**できる。

**時刻は CSV の値をそのまま使う。** 再生でホスト受信時刻を「いま」にすると、
当時の推移ではなく「いま起きたこと」として保存されてしまう。
`SimulatedClock` を行の時刻へ進めることで、取り込み経路（#8）を素通しのまま
過去の時刻で保存できる（#42）。

CSV はローカル時刻でオフセットを持たない（決定記録 0008 §2.8）。
タイムゾーンは呼び出し側が渡す。ホストの設定に依存させない。
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, TextIO
from zoneinfo import ZoneInfo

from coldaisle import logs
from coldaisle.channels import SAMPLE_CHANNELS
from coldaisle.clock import SimulatedClock
from coldaisle.ingest.protocol import RawHello, RawMessage, RawSample, RawSensor

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer

TIMESTAMP_COLUMNS = ("timestamp", "ts", "time", "datetime")
"""時刻列の呼ばれ方。**先に見つかったものを使う。**"""

COLUMN_ALIASES = {
    "room": "room_temp",
    "room_c": "room_temp",
    "room_temperature": "room_temp",
    "humidity": "room_humidity",
    "room_rh": "room_humidity",
    "intake": "front_intake",
    "front": "front_intake",
    "exhaust": "rear_exhaust",
}
"""列名の揺れを吸収する（#7）。

試作中のスクリプトは列名が揺れていた可能性がある。**知らない列は捨てて続ける**
（決定記録 0003 §2.7 と同じ態度）。1列の名前違いで再生が止まるほうが困る。
"""

REPLAY_DEVICE = "csv-replay"
"""`dev`。実機やモックと取り違えないための名前。"""

BOOT_UP_MS = 1_200

NOMINAL_INTERVAL_MS = 2_500
"""行の間隔を測れないときの想定周期（要件 §5.2）。"""

MAX_LOGGED_DROPS = 10
"""1ファイルあたり、個別に記録する破棄行の上限。総数は別に出す。"""

LOGGER = logging.getLogger("coldaisle.ingest.replay")
COPY_CHUNK_BYTES = 1024 * 1024
"""dataset provenance用snapshotを定数memoryで作るchunk size。"""


def normalize_column(name: str) -> str:
    """列名を正規化する。大文字・空白・BOM・別名を吸収する。"""
    cleaned = name.strip().lstrip("﻿").lower().replace(" ", "_").replace("-", "_")
    return COLUMN_ALIASES.get(cleaned, cleaned)


def csv_files(path: Path) -> list[Path]:
    """ファイルなら1つ、ディレクトリなら `sensors_*.csv` を日付順に返す。

    存在しないパスはここで落とす。開くまで気づかないと、
    **打ち間違いが生の `FileNotFoundError` として出る。**
    """
    if path.is_dir():
        return sorted(path.glob("sensors_*.csv"))
    if not path.exists():
        raise ValueError(f"CSV が見つからない: {path}")
    return [path]


def replay_sha256(path: Path) -> str:
    """Replay対象のbasename・file境界・内容を順序付きでhashする。"""
    _snapshot, _segments, digest = _snapshot_and_hash(csv_files(path), make_snapshot=False)
    return digest


class _SnapshotSegment(io.RawIOBase):
    """共有snapshotの1区間だけを`pread`で読むview。

    CSVごとにfileを持つとdirectory replayでdescriptorがCSV数だけ増えEMFILEになり得る。
    1つのspool fileを位置非依存の`pread`で読めば、何本CSVがあっても保持するfdは1つで済む。
    """

    def __init__(self, fd: int, start: int, length: int) -> None:
        super().__init__()
        self._fd = fd
        self._position = start
        self._end = start + length

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: WriteableBuffer) -> int:
        view = memoryview(buffer).cast("B")
        size = min(len(view), self._end - self._position)
        if size <= 0:
            return 0
        chunk = os.pread(self._fd, size, self._position)
        view[: len(chunk)] = chunk
        self._position += len(chunk)
        return len(chunk)


def _snapshot_and_hash(
    files: list[Path], *, make_snapshot: bool
) -> tuple[BinaryIO | None, list[tuple[int, int]], str]:
    """CSVをchunk単位でhashし、指定時は同じbytesを1つのprivate snapshotへ連結して書く。

    返す区間は各CSVのsnapshot内`(開始offset, 長さ)`。
    """
    digest = hashlib.sha256()
    segments: list[tuple[int, int]] = []
    # dataset sourceの寿命まで保持し、各CSVは区間viewで読む。
    snapshot = tempfile.TemporaryFile(mode="w+b") if make_snapshot else None  # noqa: SIM115
    offset = 0
    try:
        for csv_path in files:
            encoded_name = csv_path.name.encode("utf-8")
            source_fd = os.open(
                csv_path,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            with os.fdopen(source_fd, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError(f"Replay入力はregular fileでなければならない: {csv_path}")
                digest.update(len(encoded_name).to_bytes(8, "big"))
                digest.update(encoded_name)
                digest.update(before.st_size.to_bytes(8, "big"))
                copied = 0
                while chunk := source.read(COPY_CHUNK_BYTES):
                    copied += len(chunk)
                    digest.update(chunk)
                    if snapshot is not None:
                        snapshot.write(chunk)
                after = os.fstat(source.fileno())
            identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity_before != identity_after or copied != before.st_size:
                raise ValueError(f"hash中にReplay CSVが変更された: {csv_path}")
            segments.append((offset, copied))
            offset += copied
        if snapshot is not None:
            snapshot.flush()
            os.fsync(snapshot.fileno())
    except BaseException:
        if snapshot is not None:
            snapshot.close()
        raise
    return snapshot, segments, digest.hexdigest()


class ReplaySource:
    """CSV から `RawMessage` を流す `Source` 実装（FR-101）。

    3つの流し方がある。

    | 速度 | 挙動 |
    |---|---|
    | `speed=1.0` | 実時間再生。CSV の行間隔ぶん待つ |
    | `speed=60.0` | 時間圧縮再生。1分を1秒で流す |
    | `bulk=True` | 一括投入。待たない |

    どの流し方でも**保存される時刻は CSV の値**であり、結果は同じになる。
    速度は待ち時間にだけ効く（決定記録 0009 と同じ原則）。
    """

    def __init__(
        self,
        path: Path,
        *,
        tz: ZoneInfo,
        speed: float = 1.0,
        bulk: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        dataset_provenance: bool = False,
    ) -> None:
        if speed <= 0:
            raise ValueError(f"speed は正の数（一括投入は bulk=True）: {speed}")
        source_files = csv_files(path)
        if not source_files:
            raise ValueError(f"CSV が見つからない: {path}")
        self._snapshot: BinaryIO | None = None
        self._snapshot_segments: list[tuple[int, int]] = []
        self._source_sha256: str | None = None
        if dataset_provenance:
            self._snapshot, self._snapshot_segments, self._source_sha256 = _snapshot_and_hash(
                source_files,
                make_snapshot=True,
            )
        self._files = source_files
        self._tz = tz
        self._speed = speed
        self._bulk = bulk
        self._sleep = sleep
        self.dropped_rows = 0
        """時刻として読めずに捨てた行数。完全な再生かどうかの判断に使う。"""
        self.malformed_rows = 0
        """列数がheaderと合わない行数。**行は流す**が、欠けた列は欠測・余った列は捨てている。"""
        self.unparsed_cells = 0
        """空欄ではないのに数値として読めず欠測にしたcell数。"""
        self._clock = SimulatedClock(self._first_timestamp_ms())

    @property
    def clock(self) -> SimulatedClock:
        """CSV の時刻で進む時計。取り込みと保存はこれを共有する（#42）。"""
        return self._clock

    @property
    def source_sha256(self) -> str | None:
        """dataset snapshot有効時だけ、その同一bytesのSHA-256を返す。"""
        return self._source_sha256

    @property
    def losses(self) -> dict[str, int]:
        """CSVにあったのにsampleへ届かなかったものの件数（#83 dataset完了判定）。

        どれも取り込みは止めずに続ける（1行の書式違いで再生全体を止めない）。
        dataset用Replayでは1件でもあればDBは入力の一部しか持たないため、
        daemonは完了の印を付けない。数えないもの（header行、空行、対応表に無い列）
        は理由を`docs/thermal-dataset.md`に記す。
        """
        return {
            "dropped_rows": self.dropped_rows,
            "malformed_rows": self.malformed_rows,
            "unparsed_cells": self.unparsed_cells,
        }

    @property
    def hello(self) -> RawHello:
        """再生用の起動バナー。

        `interval_ms` は**最初の2行の間隔**から推定する。期待サンプル数
        （決定記録 0002 §2.8）の母数になるので、実測に近い値を入れる。
        """
        return RawHello(
            fw="0.0.0-replay",
            dev=REPLAY_DEVICE,
            interval_ms=self._estimate_interval_ms(),
            sensors={channel: RawSensor(kind="csv") for channel in SAMPLE_CHANNELS},
        )

    def stream(self) -> Iterator[RawMessage]:
        yield self.hello
        previous_ms: int | None = None
        first_ms = self._clock.now_ms()
        for seq, (row_ms, values) in enumerate(self._rows(report=True)):
            if previous_ms is not None and not self._bulk:
                self._sleep(max(row_ms - previous_ms, 0) / 1000 / self._speed)
            previous_ms = row_ms
            self._clock.advance_to_ms(row_ms)
            # seq / up は CSV に無いので合成する。**取りこぼしや再起動の検出
            # （FR-105 / FR-106）は再生では意味を持たない**ことを、
            # 連続した値を入れることで明示する
            yield RawSample(seq=seq, up=BOOT_UP_MS + (row_ms - first_ms), channels=values)

    def _rows(self, *, report: bool = False) -> Iterator[tuple[int, dict[str, float | None]]]:
        """全ファイルを時刻順に読む。**壊れた行は捨てて続ける。**

        取り込みループと同じ態度（AGENTS.md）。1行の書式違いで
        1日ぶんの再生が止まるほうが困る。

        ただし**黙って捨てない。** 数えて記録しないと、完全な再生と
        取りこぼした再生を運用者が区別できない。`report=True` のときだけ
        記録する（起動バナーのための先読みで二重に数えないため）。
        """
        for index, path in enumerate(self._files):
            dropped = 0
            handle_context: TextIO
            if self._snapshot is not None:
                segment_start, segment_length = self._snapshot_segments[index]
                handle_context = io.TextIOWrapper(
                    io.BufferedReader(
                        _SnapshotSegment(self._snapshot.fileno(), segment_start, segment_length)
                    ),
                    encoding="utf-8-sig",
                    newline="",
                )
            else:
                handle_context = path.open(encoding="utf-8-sig", newline="")
            with handle_context as handle:
                reader = csv.DictReader(handle)
                fields = [normalize_column(name) for name in reader.fieldnames or []]
                stamp_column = next((name for name in TIMESTAMP_COLUMNS if name in fields), None)
                if stamp_column is None:
                    raise ValueError(f"時刻の列が見つからない: {path}（候補: {TIMESTAMP_COLUMNS}）")
                for line, raw_row in enumerate(reader, start=2):
                    # DictReaderは余った列をkey None、足りない列をvalue Noneで表す。
                    # 空欄（""）とは区別できるので、書式の壊れた行として数える
                    malformed = None in raw_row or any(value is None for value in raw_row.values())
                    row = {
                        normalize_column(key): value
                        for key, value in raw_row.items()
                        if key is not None
                    }
                    parsed = self._parse_row(row, stamp_column)
                    if parsed is not None:
                        row_ms, values, unparsed = parsed
                        if report:
                            self.malformed_rows += int(malformed)
                            self.unparsed_cells += unparsed
                        yield row_ms, values
                        continue
                    dropped += 1
                    if report:
                        self.dropped_rows += 1
                        if dropped <= MAX_LOGGED_DROPS:
                            # 壊れたファイルでログを埋めない。総数は最後に出す
                            LOGGER.warning(
                                "時刻として読めない行を捨てた",
                                extra={
                                    logs.FIELDS_KEY: {
                                        "file": path.name,
                                        "line": line,
                                        "value": row.get(stamp_column),
                                    }
                                },
                            )
            if report and dropped:
                LOGGER.warning(
                    "再生で行を捨てた",
                    extra={logs.FIELDS_KEY: {"file": path.name, "dropped": dropped}},
                )

    def _parse_row(
        self, row: dict[str, str | None], stamp_column: str
    ) -> tuple[int, dict[str, float | None], int] | None:
        """行を`(時刻, 値, 数値として読めなかった非空cell数)`にする。時刻が無ければ`None`。"""
        stamp = row.get(stamp_column)
        if not stamp:
            return None
        try:
            when = datetime.fromisoformat(stamp.strip())
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=self._tz)
        values: dict[str, float | None] = {}
        unparsed = 0
        for channel in SAMPLE_CHANNELS:
            if channel not in row:
                continue
            raw_value = row[channel]
            values[channel] = _to_float(raw_value)
            if values[channel] is None and raw_value is not None and raw_value.strip():
                unparsed += 1
        return int(when.timestamp() * 1000), values, unparsed

    def _first_timestamp_ms(self) -> int:
        for row_ms, _ in self._rows():
            return row_ms
        raise ValueError(f"読める行が1つも無い: {self._files[0]}")

    def _estimate_interval_ms(self) -> int:
        # **2行で打ち切る。** 条件で絞るだけだと生成器を最後まで回し、
        # 起動バナーを作るためだけに書庫全体を読むことになる
        stamps = [row_ms for row_ms, _ in islice(self._rows(), 2)]
        if len(stamps) < 2 or stamps[1] <= stamps[0]:
            return NOMINAL_INTERVAL_MS
        return stamps[1] - stamps[0]


def _to_float(value: str | None) -> float | None:
    """空欄は欠測。数値にできない値も欠測として扱う。"""
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None
