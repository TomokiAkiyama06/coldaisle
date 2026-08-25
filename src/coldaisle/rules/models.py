"""ルール定義（L2）。#18

**閾値をコードに埋めない。** `config/rules.yaml` が唯一の情報源で、
値はすべて暫定である（実測は #19）。AGENTS.md ルール6。

ヒステリシスのため、**発火の閾値と解除の閾値を別に持つ**（要件 §6.4）。
1つの閾値を往復するデータは、境界付近で発火と解除を繰り返す。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle.store.models import METRIC_PATTERN, AlertSeverity


class _Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    severity: AlertSeverity


class ThresholdRule(_Rule):
    """1つの値が閾値を超えた状態が続いたら発火する（FR-404 / 405 / 406 / 408）。"""

    metric: str
    threshold: float
    clear: float
    """解除の閾値。**発火より内側**に置く。同じ値にするとフラッピングする。"""
    fire_after_s: float = Field(ge=0)


class RangeRule(_Rule):
    """範囲から外れた状態が続いたら発火する（FR-409）。上下それぞれに解除値を持つ。"""

    metric: str
    low: float
    low_clear: float
    high: float
    high_clear: float
    fire_after_s: float = Field(ge=0)


class SlopeRule(_Rule):
    """変化率が閾値を超えたら発火する（FR-407）。単位は「値 / 分」。"""

    metric: str
    slope_window_s: float = Field(gt=0)
    threshold: float
    clear: float
    fire_after_s: float = Field(ge=0)


class SilenceRule(_Rule):
    """サンプルが届かない状態が続いたら発火する（FR-401）。"""

    silence_s: float = Field(gt=0)
    fire_after_s: float = Field(ge=0)
    clear_s: float = Field(ge=0)
    """再開してからこの秒数だけ届き続けたら解除する。**届いた瞬間に解除しない。**"""


class ConsecutiveRule(_Rule):
    """同じメトリクスが連続で `ok` でない状態になったら発火する（FR-402）。"""

    consecutive: int = Field(gt=0)
    clear_consecutive: int = Field(gt=0)


class EventRule(_Rule):
    """点の出来事。継続時間を持たない（FR-403）。"""


class RuleSet(BaseModel):
    """`config/rules.yaml` 全体。**すべてのルールを明示する。**

    省略を許すと、設定の書き漏らしが「そのルールが無効」として静かに通る。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_fault: SilenceRule
    sensor_missing: ConsecutiveRule
    probe_changed: EventRule
    recirculation: ThresholdRule
    intake_high: ThresholdRule
    airflow_degraded: ThresholdRule
    rapid_rise: SlopeRule
    room_high: ThresholdRule
    humidity_out_of_range: RangeRule

    @classmethod
    def from_yaml(cls, path: Path) -> RuleSet:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or "rules" not in loaded:
            raise ValueError(f"ルール定義の形が違う（`rules` が無い）: {path}")
        rules = cls.model_validate(loaded["rules"])
        rules._validate()
        return rules

    def _validate(self) -> None:
        """閾値の向きとメトリクス名を読み込み時に確かめる。

        **解除が発火の外側にあると、発火した瞬間に解除条件も満たす。**
        設定の誤りとして落とす。
        """
        for name, rule in self.model_dump().items():
            spec = getattr(self, name)
            if "metric" in rule and not METRIC_PATTERN.match(rule["metric"]):
                # 保存可否ではなく**文法**で見る。ルールは派生値（`d.` 始まり）を
                # 参照してよい。名前が実在するかはエンジンがカタログと突き合わせる
                raise ValueError(f"{name}: メトリクス名が命名規約に合わない（{rule['metric']!r}）")
            if isinstance(spec, ThresholdRule) and spec.clear >= spec.threshold:
                raise ValueError(f"{name}: clear は threshold より小さくする（{rule}）")
            if isinstance(spec, SlopeRule) and spec.clear >= spec.threshold:
                raise ValueError(f"{name}: clear は threshold より小さくする（{rule}）")
            if isinstance(spec, RangeRule) and not (
                spec.low < spec.low_clear <= spec.high_clear < spec.high
            ):
                raise ValueError(f"{name}: low < low_clear <= high_clear < high とする（{rule}）")
            if isinstance(spec, ConsecutiveRule) and spec.clear_consecutive > spec.consecutive:
                raise ValueError(f"{name}: clear_consecutive は consecutive 以下にする（{rule}）")
