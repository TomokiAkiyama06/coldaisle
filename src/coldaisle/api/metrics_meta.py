"""メトリクスの単位・表示名と派生値の定義（L2）。#9

決定記録 0004 §5 の未決1（置き場所）をここに確定した。
`config/metrics.yaml` が唯一の情報源で、コードに既定値を持たせない
（AGENTS.md ルール6）。

**派生値は保存しない**（決定記録 0002 §2.2）。較正オフセットを後から変えたとき、
保存済みの派生値と再計算した値が食い違うため。ここは参照のたびに計算する側。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle.store.models import DERIVED_PREFIX, validate_metric


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
