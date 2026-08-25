"""派生値の計算（L2）。#9

決定記録 0002 §2.2 により**保存しない。** 較正オフセット（FR-107）を後から
変えたとき、保存済みの派生値と再計算した値が食い違うためである。

**両方の測定値が `ok` のときだけ計算する。** `suspect` や `stale` の値を
引き算すると、もっともらしい数値が出てしまう。`-127.00` から室温を引いた
`-153.4` は「再循環が無い」ように見え、故障を隠す。
"""

from __future__ import annotations

from collections.abc import Mapping

from coldaisle.api.metrics_meta import MetricCatalog
from coldaisle.store.models import LatestReading, Quality


def compute(latest: Mapping[str, LatestReading], catalog: MetricCatalog) -> dict[str, float | None]:
    """派生値を名前つきで返す。計算できないものは `None`。

    キーは常に全部返す。欠けさせると、クライアントは「定義が無い」のか
    「今は出せない」のかを区別できない。
    """
    values: dict[str, float | None] = {}
    for name, definition in catalog.derived.items():
        minuend = latest.get(definition.minuend)
        subtrahend = latest.get(definition.subtrahend)
        if not _usable(minuend) or not _usable(subtrahend):
            values[name] = None
            continue
        assert minuend is not None and subtrahend is not None
        assert minuend.value is not None and subtrahend.value is not None
        values[name] = round(minuend.value - subtrahend.value, 3)
    return values


def _usable(reading: LatestReading | None) -> bool:
    return reading is not None and reading.quality is Quality.OK and reading.value is not None
