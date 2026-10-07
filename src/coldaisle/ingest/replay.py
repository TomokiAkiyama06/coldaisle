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
import errno
import hashlib
import io
import logging
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from coldaisle import logs
from coldaisle.channels import SAMPLE_CHANNELS
from coldaisle.clock import SimulatedClock
from coldaisle.csv_export_manifest import (
    ExportRecord,
    export_binding_sha256,
    local_time_problem,
    manifest_name_for_csv,
    parse_local,
    parse_naive,
    row_seconds_sha256,
)
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

FINGERPRINT_V2_TAG = b"coldaisle.replay_fingerprint\x00v2\x00"
"""manifest を含む入力の fingerprint の規則の版（決定記録 0100 §2.8 / §5 #7）。

v1（manifest の無い入力）は CSV の名前の長さ（8 bytes）から hash を始める。v2 はこの印から
始めるので、同じ bytes 列が v1 と v2 で同じ digest になることはない。manifest の無い入力の
値は v1 のまま変えない。
"""

MAX_MANIFEST_BYTES = 64 * 1024
"""export manifest の大きさの上限。manifest は数百 bytes で、これは形の上限（運用の値ではない）。"""


class ReplayBindingError(ValueError):
    """再生の入力が export の manifest と食い違う（決定記録 0100 §2.3）。DB には何も書いていない。

    ``file`` はどの CSV か（basename）、``check`` はどの検査か、``value`` は食い違った
    最初の1行の値。
    """

    def __init__(self, file: str, check: str, message: str, value: str | None = None) -> None:
        super().__init__(f"{file}: {check}: {message}")
        self.file = file
        self.check = check
        self.value = value


@dataclass(frozen=True)
class _ManifestInput:
    """CSV の横で読んだ manifest の bytes（CSV と同じく1回だけ開いて取り込んだもの）。"""

    name: str
    raw: bytes


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
    """Replay対象のbasename・file境界・内容を順序付きでhashする。

    manifest の無い入力は従来の規則（v1）。すべての CSV に manifest があれば、CSV ごとに
    CSV の後に manifest を basename・長さ・内容で hash する（v2。決定記録 0100 §2.8）。
    一部の CSV にだけ manifest がある入力は拒否する（0100 §2.3。照合した入力としていない入力を
    1つの run に混ぜない）。
    """
    files = csv_files(path)
    manifests = _read_manifests(files)
    _refuse_partial_manifests(files, manifests)
    snapshot = _snapshot_and_hash(files, manifests, make_snapshot=False)
    return snapshot.digest


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


@dataclass
class _Snapshot:
    """入力の bytes の写し。照合も取り込みもここから読む（0031 §2.3 / 0100 §2.3）。"""

    file: BinaryIO | None
    segments: list[tuple[int, int]]
    """各 CSV の snapshot 内の ``(開始 offset, 長さ)``。"""
    csv_sha256: list[str]
    """各 CSV の bytes の SHA-256（manifest の ``csv_sha256`` と照合する）。"""
    digest: str
    """入力全体の fingerprint（manifest があれば v2）。"""


def _read_manifests(files: list[Path]) -> list[_ManifestInput | None]:
    """各 CSV の横の manifest を1回だけ開いて読む。無ければ ``None``。

    CSV と同じく ``O_NOFOLLOW`` で開き、regular file でなければ拒否する（0100 §2.3 の 1）。
    存在の確認と読み出しを分けない（確かめた後に差し替えられた別物を読まないため）。
    """
    return [_read_manifest(csv_path) for csv_path in files]


def _read_manifest(csv_path: Path) -> _ManifestInput | None:
    name = manifest_name_for_csv(csv_path.name)
    if name is None:
        return None
    path = csv_path.with_name(name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ReplayBindingError(
                csv_path.name, "manifest_regular_file", "manifest が symlink"
            ) from exc
        raise
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ReplayBindingError(
                csv_path.name, "manifest_regular_file", "manifest が regular file でない"
            )
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ReplayBindingError(csv_path.name, "manifest_format", "manifest が大きすぎる")
    return _ManifestInput(name=name, raw=raw)


def _refuse_partial_manifests(files: list[Path], manifests: list[_ManifestInput | None]) -> None:
    """一部の CSV にだけ manifest がある入力を拒否する（dataset 用の再生と fingerprint）。"""
    present = [manifest is not None for manifest in manifests]
    if any(present) and not all(present):
        missing = next(path.name for path, has in zip(files, present, strict=True) if not has)
        raise ReplayBindingError(
            missing,
            "manifest_partial",
            "manifest のある CSV と無い CSV を1つの run に混ぜない（決定記録 0100 §2.3）",
        )


def _snapshot_and_hash(
    files: list[Path], manifests: list[_ManifestInput | None], *, make_snapshot: bool
) -> _Snapshot:
    """CSVをchunk単位でhashし、指定時は同じbytesを1つのprivate snapshotへ連結して書く。

    fingerprint は manifest が1つも無ければ v1、すべてにあれば v2（決定記録 0100 §2.8）。
    一部にだけある入力の fingerprint は使わない（呼び出し側が拒否する）。
    """
    with_manifests = all(manifest is not None for manifest in manifests) and bool(manifests)
    digest = hashlib.sha256()
    if with_manifests:
        digest.update(FINGERPRINT_V2_TAG)
    segments: list[tuple[int, int]] = []
    csv_digests: list[str] = []
    # dataset sourceの寿命まで保持し、各CSVは区間viewで読む。
    snapshot = tempfile.TemporaryFile(mode="w+b") if make_snapshot else None  # noqa: SIM115
    offset = 0
    try:
        for csv_path, manifest in zip(files, manifests, strict=True):
            encoded_name = csv_path.name.encode("utf-8")
            file_digest = hashlib.sha256()
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
                    file_digest.update(chunk)
                    if snapshot is not None:
                        snapshot.write(chunk)
                after = os.fstat(source.fileno())
            identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity_before != identity_after or copied != before.st_size:
                raise ValueError(f"hash中にReplay CSVが変更された: {csv_path}")
            if with_manifests and manifest is not None:
                encoded_manifest = manifest.name.encode("utf-8")
                digest.update(len(encoded_manifest).to_bytes(8, "big"))
                digest.update(encoded_manifest)
                digest.update(len(manifest.raw).to_bytes(8, "big"))
                digest.update(manifest.raw)
            segments.append((offset, copied))
            csv_digests.append(file_digest.hexdigest())
            offset += copied
        if snapshot is not None:
            snapshot.flush()
            os.fsync(snapshot.fileno())
    except BaseException:
        if snapshot is not None:
            snapshot.close()
        raise
    return _Snapshot(
        file=snapshot, segments=segments, csv_sha256=csv_digests, digest=digest.hexdigest()
    )


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
        timezone_explicit: bool = True,
        speed: float = 1.0,
        bulk: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        dataset_provenance: bool = False,
    ) -> None:
        """``tz`` は manifest の無い CSV に当てる timezone。

        ``timezone_explicit`` は ``tz`` を人が明示したか（``--timezone``）。manifest のある
        入力では manifest の timezone を使い、明示した ``tz`` と文字列で違えば拒否する
        （決定記録 0100 §2.3）。
        manifest のある入力は、照合も取り込みも同じ snapshot から読む。食い違いは
        :class:`ReplayBindingError`（何も取り込まない）。
        """
        if speed <= 0:
            raise ValueError(f"speed は正の数（一括投入は bulk=True）: {speed}")
        source_files = csv_files(path)
        if not source_files:
            raise ValueError(f"CSV が見つからない: {path}")
        try:
            manifests = _read_manifests(source_files)
            if dataset_provenance:
                _refuse_partial_manifests(source_files, manifests)
            verified = [manifest is not None for manifest in manifests]
            self._snapshot: BinaryIO | None = None
            self._snapshot_segments: list[tuple[int, int]] = []
            self._source_sha256: str | None = None
            if dataset_provenance or any(verified):
                snapshot = _snapshot_and_hash(source_files, manifests, make_snapshot=True)
                self._snapshot = snapshot.file
                self._snapshot_segments = snapshot.segments
                if dataset_provenance:
                    self._source_sha256 = snapshot.digest
            self._files = source_files
            self._verified = verified
            self._tz = tz
            self._local_timezone: str | None = None
            self._export_binding_sha256: str | None = None
            if any(verified):
                records = self._verify_manifests(
                    manifests,
                    snapshot.csv_sha256,
                    timezone_explicit=timezone_explicit,
                    dataset_provenance=dataset_provenance,
                )
                if dataset_provenance:
                    self._local_timezone = self._tz.key
                    self._export_binding_sha256 = export_binding_sha256(records)
        except ReplayBindingError as error:
            # どの CSV の、どの検査か（行の値は最初の1行だけ。決定記録 0100 §2.3）
            LOGGER.error(
                "再生の入力が export の manifest と食い違う。取り込まない",
                extra={
                    logs.FIELDS_KEY: {
                        "file": error.file,
                        "check": error.check,
                        "value": error.value,
                        "reason": str(error),
                    }
                },
            )
            self._close_snapshot()
            raise
        if not all(verified):
            LOGGER.warning(
                "manifest の無い CSV の timezone を照合していない（決定記録 0100 §2.3 / §2.7）",
                extra={
                    logs.FIELDS_KEY: {
                        "files": sum(1 for has in verified if not has),
                        "timezone": tz.key,
                    }
                },
            )
        self._speed = speed
        self._bulk = bulk
        self._sleep = sleep
        self.dropped_rows = 0
        """時刻として読めずに捨てた行数。完全な再生かどうかの判断に使う。"""
        self.malformed_rows = 0
        """列数がheaderと合わない行数。**行は流す**が、欠けた列は欠測・余った列は捨てている。"""
        self.unparsed_cells = 0
        """空欄ではないのに数値として読めず欠測にしたcell数。"""
        self.header_collisions = 0
        """同じ列名に正規化される見出しの重複数（fileごとに1回数える）。後の列だけが残る。"""
        self._clock = SimulatedClock(self._first_timestamp_ms())

    def _close_snapshot(self) -> None:
        snapshot = getattr(self, "_snapshot", None)
        if snapshot is not None:
            snapshot.close()

    def _verify_manifests(
        self,
        manifests: list[_ManifestInput | None],
        csv_sha256: list[str],
        *,
        timezone_explicit: bool,
        dataset_provenance: bool,
    ) -> list[ExportRecord]:
        """決定記録 0100 §2.3 の 1〜7 を、取り込みの前に snapshot から確かめる。

        通過したら ``self._tz`` を manifest の timezone にする。1つでも外れれば
        :class:`ReplayBindingError`。7（``export_id`` の重複）は dataset 用の再生だけで見る。
        """
        records: list[tuple[int, ExportRecord]] = []
        for index, manifest in enumerate(manifests):
            if manifest is None:
                continue
            name = self._files[index].name
            try:
                record = ExportRecord.from_manifest_bytes(manifest.raw)
            except ValueError as exc:
                raise ReplayBindingError(name, "manifest_format", str(exc)) from exc
            if record.csv_name != name:
                raise ReplayBindingError(
                    name, "csv_name", f"manifest の csv_name が違う: {record.csv_name}"
                )
            if record.csv_sha256 != csv_sha256[index]:
                raise ReplayBindingError(name, "csv_sha256", "CSV の bytes が manifest と違う")
            records.append((index, record))

        timezones = sorted({record.timezone for _, record in records})
        if len(timezones) != 1:
            raise ReplayBindingError(
                self._files[records[0][0]].name,
                "timezone_mixed",
                f"1 run に timezone の違う manifest が混ざる: {timezones}",
            )
        manifest_timezone = timezones[0]
        partial = len(records) != len(manifests)
        # 明示した --timezone、または manifest の無い CSV に当てる実効の timezone（省略時は
        # 既定値）が manifest と文字列で違えば拒否する。2つの時刻の写像を1つの DB に混ぜない
        if (timezone_explicit or partial) and self._tz.key != manifest_timezone:
            raise ReplayBindingError(
                self._files[records[0][0]].name,
                "timezone_flag",
                f"--timezone {self._tz.key!r} が manifest の {manifest_timezone!r} と違う",
            )
        try:
            zone = ZoneInfo(manifest_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ReplayBindingError(
                self._files[records[0][0]].name,
                "timezone_unreadable",
                f"manifest の timezone を読めない: {manifest_timezone!r}",
            ) from exc
        for index, record in records:
            self._verify_rows(index, record, zone)
        if dataset_provenance:
            seen: set[str] = set()
            for index, record in records:
                if record.export_id in seen:
                    raise ReplayBindingError(
                        self._files[index].name, "export_id_duplicate", "export_id が重複している"
                    )
                seen.add(record.export_id)
        self._tz = zone
        return [record for _, record in records]

    def _verify_rows(self, index: int, record: ExportRecord, zone: ZoneInfo) -> None:
        """全行を manifest の timezone で絶対時刻へ写し、秒の列の hash・行数・日の区間を照合する。

        DST の曖昧な時刻・存在しない時刻が1行でもあれば拒否する（0100 §2.5）。snapshot を
        1回読み通すだけで、行は持たない（定数メモリ）。
        """
        name = self._files[index].name
        count = 0

        def seconds(handle: TextIO) -> Iterator[int]:
            nonlocal count
            reader = csv.DictReader(handle)
            fields = [normalize_column(field) for field in reader.fieldnames or []]
            stamp_column = next((field for field in TIMESTAMP_COLUMNS if field in fields), None)
            if stamp_column is None:
                raise ReplayBindingError(name, "row_time", "時刻の列が無い")
            for raw_row in reader:
                row = {normalize_column(k): v for k, v in raw_row.items() if k is not None}
                stamp = (row.get(stamp_column) or "").strip()
                try:
                    naive = parse_naive(stamp)
                except ValueError as exc:
                    raise ReplayBindingError(
                        name, "row_time", "時刻として読めない行", stamp
                    ) from exc
                problem = local_time_problem(naive, zone)
                if problem is not None:
                    raise ReplayBindingError(
                        name, f"dst_{problem}", "DST の曖昧な時刻か存在しない時刻", stamp
                    )
                second = parse_local(stamp, zone)
                if not record.day_start_ms <= second * 1000 < record.day_end_ms:
                    raise ReplayBindingError(name, "day_range", "行の時刻がその日の外", stamp)
                count += 1
                yield second

        with self._open_snapshot_segment(index) as handle:
            digest = row_seconds_sha256(seconds(handle))
        if count != record.row_count:
            raise ReplayBindingError(
                name, "row_count", f"行数が manifest と違う: {count} != {record.row_count}"
            )
        if digest != record.row_seconds_sha256:
            raise ReplayBindingError(
                name, "row_seconds_sha256", "全行の絶対時刻が export と一致しない"
            )

    def _open_snapshot_segment(self, index: int) -> TextIO:
        assert self._snapshot is not None
        segment_start, segment_length = self._snapshot_segments[index]
        return io.TextIOWrapper(
            io.BufferedReader(
                _SnapshotSegment(self._snapshot.fileno(), segment_start, segment_length)
            ),
            encoding="utf-8-sig",
            newline="",
        )

    @property
    def local_timezone(self) -> str | None:
        """dataset 用の再生で manifest と照合した timezone の名前。照合していなければ ``None``。"""
        return self._local_timezone

    @property
    def export_binding_sha256(self) -> str | None:
        """dataset 用の再生で照合した入力の export の束縛の digest（0100 §2.8）。

        照合していなければ ``None``。
        """
        return self._export_binding_sha256

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
            "header_collisions": self.header_collisions,
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
                handle_context = self._open_snapshot_segment(index)
            else:
                handle_context = path.open(encoding="utf-8-sig", newline="")
            with handle_context as handle:
                reader = csv.DictReader(handle)
                fields = [normalize_column(name) for name in reader.fieldnames or []]
                stamp_column = next((name for name in TIMESTAMP_COLUMNS if name in fields), None)
                if stamp_column is None:
                    raise ValueError(f"時刻の列が見つからない: {path}（候補: {TIMESTAMP_COLUMNS}）")
                collisions = _header_collisions(fields)
                if report and collisions:
                    # `room`と`room_temp`のように同じ列へ正規化される見出しは、dictにすると
                    # 後の列だけが残り前の列の値を黙って失う。取り込みは続け、数えて記録する
                    self.header_collisions += collisions
                    LOGGER.warning(
                        "同じ列に正規化される見出しが重複している。後の列だけを使う",
                        extra={logs.FIELDS_KEY: {"file": path.name, "collisions": collisions}},
                    )
                for line, raw_row in enumerate(reader, start=2):
                    # DictReaderは余った列をkey None、足りない列をvalue Noneで表す。
                    # 空欄（""）とは区別できるので、書式の壊れた行として数える
                    malformed = None in raw_row or any(value is None for value in raw_row.values())
                    row = {
                        normalize_column(key): value
                        for key, value in raw_row.items()
                        if key is not None
                    }
                    parsed = self._parse_row(row, stamp_column, verified=self._verified[index])
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
        self, row: dict[str, str | None], stamp_column: str, *, verified: bool
    ) -> tuple[int, dict[str, float | None], int] | None:
        """行を`(時刻, 値, 数値として読めなかった非空cell数)`にする。時刻が無ければ`None`。

        manifest と照合した CSV（``verified``）は、照合と同じ写像（``parse_local``）で読む
        （決定記録 0100 §2.4）。照合していない CSV は従来どおり（0010 §2.7）。
        """
        stamp = row.get(stamp_column)
        if not stamp:
            return None
        if verified:
            row_ms = parse_local(stamp.strip(), self._tz) * 1000
        else:
            try:
                when = datetime.fromisoformat(stamp.strip())
            except ValueError:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=self._tz)
            row_ms = int(when.timestamp() * 1000)
        values: dict[str, float | None] = {}
        unparsed = 0
        for channel in SAMPLE_CHANNELS:
            if channel not in row:
                continue
            raw_value = row[channel]
            values[channel] = _to_float(raw_value)
            if values[channel] is None and raw_value is not None and raw_value.strip():
                unparsed += 1
        return row_ms, values, unparsed

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


def _header_collisions(fields: list[str]) -> int:
    """読む列（channelと時刻）のうち、1つの値へ潰れる見出しの余剰数。

    同じ名前へ正規化される見出しに加え、`timestamp,ts`のように時刻の別名が複数ある
    場合も数える。時刻は先に見つかった1列だけを使うため、残りの列は黙って失われる。
    対応表に無い列同士の重複は値を読まないため失うものが無く、数えない。
    """
    channels = [name for name in fields if name in SAMPLE_CHANNELS]
    stamps = [name for name in fields if name in TIMESTAMP_COLUMNS]
    return (len(channels) - len(set(channels))) + max(len(stamps) - 1, 0)


def _to_float(value: str | None) -> float | None:
    """空欄は欠測。数値にできない値も欠測として扱う。"""
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None
