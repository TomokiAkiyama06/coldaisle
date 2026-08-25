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
import logging
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

from coldaisle import logs
from coldaisle.channels import SAMPLE_CHANNELS
from coldaisle.clock import SimulatedClock
from coldaisle.ingest.protocol import RawHello, RawMessage, RawSample, RawSensor

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
    ) -> None:
        if speed <= 0:
            raise ValueError(f"speed は正の数（一括投入は bulk=True）: {speed}")
        self._files = csv_files(path)
        if not self._files:
            raise ValueError(f"CSV が見つからない: {path}")
        self._tz = tz
        self._speed = speed
        self._bulk = bulk
        self._sleep = sleep
        self.dropped_rows = 0
        """時刻として読めずに捨てた行数。完全な再生かどうかの判断に使う。"""
        self._clock = SimulatedClock(self._first_timestamp_ms())

    @property
    def clock(self) -> SimulatedClock:
        """CSV の時刻で進む時計。取り込みと保存はこれを共有する（#42）。"""
        return self._clock

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
        for path in self._files:
            dropped = 0
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fields = [normalize_column(name) for name in reader.fieldnames or []]
                stamp_column = next((name for name in TIMESTAMP_COLUMNS if name in fields), None)
                if stamp_column is None:
                    raise ValueError(f"時刻の列が見つからない: {path}（候補: {TIMESTAMP_COLUMNS}）")
                for line, raw_row in enumerate(reader, start=2):
                    row = {
                        normalize_column(key): value
                        for key, value in raw_row.items()
                        if key is not None
                    }
                    parsed = self._parse_row(row, stamp_column)
                    if parsed is not None:
                        yield parsed
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
    ) -> tuple[int, dict[str, float | None]] | None:
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
        for channel in SAMPLE_CHANNELS:
            if channel not in row:
                continue
            values[channel] = _to_float(row[channel])
        return int(when.timestamp() * 1000), values

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
