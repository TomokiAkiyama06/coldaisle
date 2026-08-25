"""メトリクスの単位・表示名と派生値の定義（レイヤ横断）。#9 / #18

決定記録 0004 §5 の未決1（置き場所）をここに確定した。
`config/metrics.yaml` が唯一の情報源で、コードに既定値を持たせない
（AGENTS.md ルール6）。

**API（L2）とルールエンジン（L2）の両方が使う。** ルールが HTTP 層を
import することになるのを避けるため、`clock` や `channels` と同じく
パッケージ直下に置く。

**派生値は保存しない**（決定記録 0002 §2.2）。較正オフセットを後から変えたとき、
保存済みの派生値と再計算した値が食い違うため。ここは参照のたびに計算する側。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle.store.models import DERIVED_PREFIX, Quality, validate_metric


class MetricMeta(BaseModel):
    """保存されるメトリクス1つ分の表示情報。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    label: str


class DerivedMeta(BaseModel):
    """派生値1つ分。**引き算1つ**で表せるものだけを扱う（要件 §5.1）。

    任意の式を書けるようにしない。設定ファイルが小さな言語になり、
    「この値が何を意味するか」がコードからも設定からも読めなくなる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    label: str
    minuend: str
    """引かれる側のメトリクス。"""
    subtrahend: str
    """引く側のメトリクス。"""


class MetricCatalog(BaseModel):
    """`config/metrics.yaml` 全体。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: dict[str, MetricMeta] = Field(default_factory=dict)
    derived: dict[str, DerivedMeta] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> MetricCatalog:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"メトリクス定義が辞書ではない: {path}")
        catalog = cls.model_validate(loaded)
        catalog._validate_names()
        return catalog

    def _validate_names(self) -> None:
        """命名規約（決定記録 0002 §2.1）と `d.` の予約を守らせる。"""
        for metric in self.metrics:
            validate_metric(metric)
        for name, derived in self.derived.items():
            if not name.startswith(DERIVED_PREFIX):
                raise ValueError(f"派生値の名前は `{DERIVED_PREFIX}` で始める: {name!r}")
            validate_metric(derived.minuend)
            validate_metric(derived.subtrahend)

    def unit_for(self, metric: str) -> str | None:
        if metric in self.metrics:
            return self.metrics[metric].unit
        if metric in self.derived:
            return self.derived[metric].unit
        return None


class Measured(Protocol):
    """測定値1つ分。`Reading`（取り込み側）と `LatestReading`（読み出し側）が満たす。"""

    @property
    def value(self) -> float | None: ...

    @property
    def quality(self) -> Quality: ...


def compute_derived(
    values: Mapping[str, Measured], catalog: MetricCatalog
) -> dict[str, float | None]:
    """派生値を名前つきで返す。計算できないものは `None`。

    決定記録 0002 §2.2 により**保存しない。** 較正オフセット（FR-107）を後から
    変えたとき、保存済みの派生値と再計算した値が食い違うためである。

    **両方の測定値が `ok` のときだけ計算する。** `suspect` や `stale` の値を
    引き算すると、もっともらしい数値が出てしまう。`-127.00` から室温を引いた
    `-153.4` は「再循環が無い」ように見え、故障を隠す。

    キーは常に全部返す。欠けさせると、呼び出し側は「定義が無い」のか
    「今は出せない」のかを区別できない。
    """
    computed: dict[str, float | None] = {}
    for name, definition in catalog.derived.items():
        minuend = values.get(definition.minuend)
        subtrahend = values.get(definition.subtrahend)
        if not _usable(minuend) or not _usable(subtrahend):
            computed[name] = None
            continue
        assert minuend is not None and subtrahend is not None
        assert minuend.value is not None and subtrahend.value is not None
        computed[name] = round(minuend.value - subtrahend.value, 3)
    return computed


def _usable(measured: Measured | None) -> bool:
    return measured is not None and measured.quality is Quality.OK and measured.value is not None
