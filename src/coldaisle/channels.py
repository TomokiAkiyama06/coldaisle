"""デバイスのチャネル名とメトリクス名の対応（レイヤ横断）。#10

決定記録 0003 の対応表。デバイスは短い名前（`front_intake`）を送り、
ホストは名前空間付き（`air.front_intake`）で保存する。

`ingest`（L0）が保存の向きに、`store` の日次CSV（L1）が復元の向きに使う。
どちらの層にも置けないため、`clock` と同じくパッケージ直下に置く
（下位レイヤが上位レイヤを import しない。AGENTS.md）。

**片方向だけを直す事故を防ぐため、逆写像はここで導出する。**
"""

from __future__ import annotations

CHANNEL_TO_METRIC: dict[str, str] = {
    "room_temp": "air.room",
    "room_humidity": "air.room_humidity",
    "front_intake": "air.front_intake",
    "gpu_intake": "air.gpu_intake",
    "gpu_exhaust": "air.gpu_exhaust",
    "top_exhaust": "air.top_exhaust",
    "rear_exhaust": "air.rear_exhaust",
}
"""決定記録 0003 の対応表。**挿入順が従来のCSVの列順**（FR-205）。"""

METRIC_TO_CHANNEL: dict[str, str] = {
    metric: channel for channel, metric in CHANNEL_TO_METRIC.items()
}

SAMPLE_CHANNELS: tuple[str, ...] = tuple(CHANNEL_TO_METRIC)
"""v1 のサンプルが持つチャネル（要件 §5.2）。JSON へ出す順でもある。"""
