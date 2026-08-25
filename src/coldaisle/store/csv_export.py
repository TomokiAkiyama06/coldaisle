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
"""

from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from coldaisle.channels import METRIC_TO_CHANNEL, SAMPLE_CHANNELS
from coldaisle.store.db import SqliteStore
from coldaisle.store.models import Quality

TIMESTAMP_COLUMN = "timestamp"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
"""従来の出力に合わせる。オフセットもミリ秒も付かない。"""


def day_bounds_ms(day: date, tz: ZoneInfo) -> tuple[int, int]:
    """その日の `[開始, 終了)` を Unix ミリ秒で返す。"""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def export_day(store: SqliteStore, day: date, *, tz: ZoneInfo, out_dir: Path) -> Path:
    """1日ぶんを1ファイルへ書き出す。書いたパスを返す。

    行は同一時刻のサンプル。決定記録 0002 §2.3 により1サンプルの全メトリクスは
    同じ `ts_ms` を持つので、そのまま横に並ぶ。

    デバイス由来でないメトリクス（`sys.dropped_samples` など）は**書かない。**
    従来の列だけを保つ。欠測や取りこぼしの分析は SQLite 側で行う。
    """
    start_ms, end_ms = day_bounds_ms(day, tz)
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

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"sensors_{day.isoformat()}.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([TIMESTAMP_COLUMN, *SAMPLE_CHANNELS])
        for ts_ms in sorted(pivoted):
            stamp = datetime.fromtimestamp(ts_ms / 1000, tz=tz).strftime(TIMESTAMP_FORMAT)
            values = pivoted[ts_ms]
            writer.writerow(
                [stamp, *("" if values.get(c) is None else values[c] for c in SAMPLE_CHANNELS)]
            )
    return path
