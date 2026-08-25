"""日次CSVエクスポート（FR-205、#10）。

**従来の出力と同じ形式**であることが要件。`~/server_sensor_logs/` に残っていた
実ファイル（2026-08-23 / 24）のヘッダと書式に合わせている。

```text
timestamp,room_temp,room_humidity,front_intake,gpu_intake,gpu_exhaust,top_exhaust,rear_exhaust
2026-08-23T20:16:40,24.5,60.0,24.31,23.94,24.0,24.06,24.19
2026-08-23T20:35:31,,,24.75,24.31,25.25,24.5,24.62
```
"""

import csv
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from coldaisle.store.csv_export import day_bounds_ms, export_day

JST = ZoneInfo("Asia/Tokyo")
HISTORICAL_HEADER = [
    "timestamp",
    "room_temp",
    "room_humidity",
    "front_intake",
    "gpu_intake",
    "gpu_exhaust",
    "top_exhaust",
    "rear_exhaust",
]
DAY = date(2026, 8, 25)
NOON_JST_MS = 1_787_626_800_000  # 2026-08-25T12:00:00+09:00


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "csv.db", rules=rules, clock=SimulatedClock(0)) as opened:
        yield opened


def write(store, ts_ms: int, **channels) -> None:
    readings = tuple(
        Reading(
            metric=metric,
            value=value,
            quality=Quality.OK if value is not None else Quality.MISSING,
        )
        for metric, value in channels.items()
    )
    store.insert_sample(Sample(ts_ms=ts_ms, readings=readings))


def rows_of(path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_header_matches_the_historical_format(store, tmp_path):
    """列名は**デバイスのチャネル名**。`air.` を付けない。列順も従来どおり。"""
    write(store, NOON_JST_MS, **{"air.room": 26.0})
    path = export_day(store, DAY, tz=JST, out_dir=tmp_path / "out")
    assert path.name == "sensors_2026-08-25.csv"
    assert rows_of(path)[0] == HISTORICAL_HEADER


def test_timestamp_has_no_offset_and_second_resolution(store, tmp_path):
    write(store, NOON_JST_MS, **{"air.room": 26.0})
    path = export_day(store, DAY, tz=JST, out_dir=tmp_path / "out")
    assert rows_of(path)[1][0] == "2026-08-25T12:00:00"


def test_values_land_in_their_channel_columns(store, tmp_path):
    write(
        store,
        NOON_JST_MS,
        **{"air.room": 24.5, "air.room_humidity": 60.0, "air.rear_exhaust": 24.19},
    )
    path = export_day(store, DAY, tz=JST, out_dir=tmp_path / "out")
    row = dict(zip(HISTORICAL_HEADER, rows_of(path)[1], strict=True))
    assert row["room_temp"] == "24.5"
    assert row["room_humidity"] == "60.0"
    assert row["rear_exhaust"] == "24.19"
    assert row["front_intake"] == "", "届かなかった列は空欄"


def test_non_ok_values_are_blank(store, tmp_path):
    """従来の出力にも `-127.00` や ちょうど `85.00` は1件も無く、空欄になっていた。

    数値として書くと、表計算のグラフと平均にセンサーの人工物がそのまま乗る。
    """
    store.insert_sample(
        Sample(
            ts_ms=NOON_JST_MS,
            readings=(
                Reading(metric="air.rear_exhaust", value=-127.0, quality=Quality.SUSPECT),
                Reading(metric="air.gpu_exhaust", value=85.0, quality=Quality.SUSPECT),
                Reading(metric="air.room", value=None, quality=Quality.MISSING),
                Reading(metric="air.front_intake", value=24.75, quality=Quality.OK),
            ),
        )
    )
    path = export_day(store, DAY, tz=JST, out_dir=tmp_path / "out")
    row = dict(zip(HISTORICAL_HEADER, rows_of(path)[1], strict=True))
    assert row["rear_exhaust"] == ""
    assert row["gpu_exhaust"] == ""
    assert row["room_temp"] == ""
    assert row["front_intake"] == "24.75"


def test_device_derived_metrics_only(store, tmp_path):
    """`sys.*` は書かない。従来の列だけを保つ。"""
    write(store, NOON_JST_MS, **{"air.room": 26.0, "sys.dropped_samples": 4.0})
    path = export_day(store, DAY, tz=JST, out_dir=tmp_path / "out")
    assert rows_of(path)[0] == HISTORICAL_HEADER


def test_day_is_cut_in_local_time(store, tmp_path):
    """「その日」は生活時間の日。UTC の日ではない。"""
    start_ms, end_ms = day_bounds_ms(DAY, JST)
    write(store, start_ms, **{"air.room": 1.0})
    write(store, end_ms - 1, **{"air.room": 2.0})
    write(store, start_ms - 1, **{"air.room": 3.0})  # 前日の23:59:59.999
    write(store, end_ms, **{"air.room": 4.0})  # 翌日の00:00

    path = export_day(store, DAY, tz=JST, out_dir=tmp_path / "out")
    values = [row[1] for row in rows_of(path)[1:]]
    assert values == ["1.0", "2.0"]


def test_empty_day_still_writes_a_header(store, tmp_path):
    path = export_day(store, DAY, tz=JST, out_dir=tmp_path / "out")
    assert rows_of(path) == [HISTORICAL_HEADER]


def test_one_row_per_sample_time(store, tmp_path):
    """1サンプルの全メトリクスは同じ `ts_ms`（決定記録 0002 §2.3）。横に並ぶ。"""
    write(store, NOON_JST_MS, **{"air.room": 26.0, "air.front_intake": 27.0})
    write(store, NOON_JST_MS + 2_500, **{"air.room": 26.1, "air.front_intake": 27.1})
    path = export_day(store, DAY, tz=JST, out_dir=tmp_path / "out")
    assert len(rows_of(path)) == 3
