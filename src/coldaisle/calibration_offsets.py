"""較正の**実効の offset の写像**（レイヤ横断）。決定記録 0096 §2.2 / §2.3 / §5 #3。

取り込み（``ingest`` の ``Normalizer``。L0）が較正を当てるかの判定と、
反実仮想 artifact の較正の digest（``control/model``）が同じ規則を使うために、
``channels`` の隣に置く。**判定を2か所に書かない**（0096 §5 #3）。

ここは ``ingest`` を import しない。``Calibration`` 型を読むのは合成の起点で、
ここへはチャネル名 → offset（``Calibration.offsets_c``）だけが届く。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

from coldaisle.channels import METRIC_TO_CHANNEL

HUMIDITY_SUFFIX = "_humidity"
"""較正を当てない metric の末尾。較正値の単位は℃で、%RH には当てない（#13）。"""


def calibration_applies(metric: str) -> bool:
    """``Normalizer`` がこの metric に較正の offset を当てるか。

    いまは湿度（末尾 ``_humidity``）だけを除く。``Normalizer`` と digest がこの1つの述語を
    共有する（0096 §5 #3）。
    """
    return not metric.endswith(HUMIDITY_SUFFIX)


def effective_metric_offsets(offsets_c: Mapping[str, float]) -> dict[str, float]:
    """較正を当てる全 metric（``METRIC_TO_CHANNEL`` のうち湿度を除く）→ 実効の offset。

    - ``offsets_c`` に無いチャネルは 0.0（``Calibration.offset_for`` と同じ。0096 §5 #2）
    - ``-0.0`` は ``0.0`` にする（足した結果が同じなので同じ較正として扱う。0096 §2.3）
    - **丸めない**（``Normalizer`` は丸めずに足す）
    - ``METRIC_TO_CHANNEL`` に無いチャネルのキーは ``Normalizer`` が使わないので入れない

    非有限値は ``ValueError``（``Calibration`` が読み込み時に拒否する値の二重の守り）。
    """
    mapped: dict[str, float] = {}
    for metric, channel in METRIC_TO_CHANNEL.items():
        if not calibration_applies(metric):
            continue
        value = float(offsets_c.get(channel, 0.0))
        if not math.isfinite(value):
            raise ValueError(f"較正の offset が有限でない: {channel}={value!r}")
        mapped[metric] = 0.0 if value == 0.0 else value
    return mapped


def canonical_offsets_bytes(offsets: Mapping[str, float]) -> bytes:
    """``{metric 名: offset}`` の canonical JSON（0096 §2.3）。

    ``control/model/thermal.py`` の ``canonical_json_bytes`` と同じ規約。
    """
    return (
        json.dumps(
            dict(offsets),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def offsets_sha256(offsets: Mapping[str, float]) -> str:
    """:func:`canonical_offsets_bytes` の SHA-256（小文字16進64文字）。"""
    return hashlib.sha256(canonical_offsets_bytes(offsets)).hexdigest()
