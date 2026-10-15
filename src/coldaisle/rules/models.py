"""ルール定義（L2）。#18

**閾値をコードに埋めない。** `config/rules.yaml` が唯一の情報源で、
値はすべて暫定である（実測は #19）。AGENTS.md ルール9。

ヒステリシスのため、**発火の閾値と解除の閾値を別に持つ**（要件 §6.4）。
1つの閾値を往復するデータは、境界付近で発火と解除を繰り返す。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from coldaisle.store.models import METRIC_PATTERN, AlertSeverity

METRIC_KEYS = ("metric", "core_metric", "fan_metric")
"""ルールがメトリクス名を持つ項目。名前の検査と、LLM への除外の判定（0131 §2.4a）で使う。"""


def rule_metrics(rule: BaseModel) -> tuple[str, ...]:
    """ルールが参照するメトリクス名。"""
    return tuple(value for key in METRIC_KEYS if isinstance(value := getattr(rule, key, None), str))


TEXT_KEYS = ("label", "title", "resolved_title", "hint")
"""人が読む文の項目（決定記録 0142）。閾値ではないので、AI ツールと運用メモリの事実には出さない。"""

ValueKind = Literal["threshold", "range", "slope", "below", "flag", "seconds", "samples", "event"]
"""遷移の `value` が何を表すか（決定記録 0142 §2.4）。通知の値の行の書き方を決める。

ルールごとに値の意味が違う（`SENSOR_FAULT` は秒数、`RAPID_RISE` は 1 分あたりの変化）。
メトリクスの単位を機械的に付けると、秒数に `°C` が付く。
"""


class _Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    severity: AlertSeverity
    label: str
    """短い名前（決定記録 0142 §2.3）。日次レポートの表に出る。"""
    title: str
    """発火の通知の見出しの文（例「GPU の吸気温度が急上昇しています」）。"""
    resolved_title: str
    """解除の通知の見出しの文（例「GPU の吸気温度の急上昇が収まりました」）。"""
    hint: str
    """発火の通知に載せる「確かめること」の一文。

    4 つとも**必須**。省略を許すと、書き漏らしが ID だけの通知・手掛かりの無い通知として静かに通る。
    """

    @field_validator("label", "title", "resolved_title", "hint")
    @classmethod
    def _text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("空の文")
        return value


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


class FlagOffRule(_Rule):
    """0/1 の flag が 1 でない状態が続いたら発火する（決定記録 0131 §2.1）。

    値は取り込みのサンプルではなく、**DB の最新値**（別プロセスの収集が書いたもの）を
    評価のたびに読む（0131 §2.3）。最新値が無い・ok でない（stale を含む）周期は
    評価しない。発火待ちは取り消し、発火中は解除の待ちだけを数え直す。
    """

    metric: str
    fire_after_s: float = Field(ge=0)
    clear_s: float = Field(ge=0)
    """flag が 1 の状態がこの秒数だけ続いたら解除する。**1 回の 1 で解除しない。**"""


class HotAndSlowRule(_Rule):
    """温度が高いのに回転が低い状態が続いたら発火する（決定記録 0131 §2.1）。

    `FlagOffRule` と同じく DB の最新値を読み、2 つの値のどちらかが無い・ok でない
    周期は評価しない（0131 §2.3）。
    """

    core_metric: str
    fan_metric: str
    core_threshold: float
    """`core_metric` がこれ以上で、"""
    fan_below: float
    """`fan_metric` がこれ未満なら成立。"""
    core_clear: float
    """解除: `core_metric` がこれ未満になる。**`core_threshold` より小さくする。**"""
    fan_clear: float
    """解除: `fan_metric` がこれ以上になる。**`fan_below` より大きくする。**"""
    fire_after_s: float = Field(ge=0)


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
    gpu_fan_not_manual: FlagOffRule
    gpu_fan_below_curve: HotAndSlowRule

    def labels(self) -> dict[str, str]:
        """`RULE_ID`（ルールの名前の大文字）→ 人が読む名前（決定記録 0142 §2.3）。"""
        return {name.upper(): rule.label for name, rule in self}

    def value_kinds(self) -> dict[str, ValueKind]:
        """`RULE_ID` → 遷移の `value` の意味（決定記録 0142 §2.4）。**ルールの型で決める。**"""
        return {name.upper(): _value_kind(rule) for name, rule in self}

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
            for key in METRIC_KEYS:
                if key in rule and not METRIC_PATTERN.match(rule[key]):
                    # 保存可否ではなく**文法**で見る。ルールは派生値（`d.` 始まり）を
                    # 参照してよい。名前が実在するかはエンジンがカタログと突き合わせる
                    raise ValueError(f"{name}: メトリクス名が命名規約に合わない（{rule[key]!r}）")
            if isinstance(spec, HotAndSlowRule) and not (
                spec.core_clear < spec.core_threshold and spec.fan_below < spec.fan_clear
            ):
                raise ValueError(
                    f"{name}: core_clear < core_threshold かつ "
                    f"fan_below < fan_clear とする（{rule}）"
                )
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


def _value_kind(rule: BaseModel) -> ValueKind:
    """ルールの型から値の意味を決める。ルールの名前（ID）で分岐しない。"""
    kinds: tuple[tuple[type[BaseModel], ValueKind], ...] = (
        (ThresholdRule, "threshold"),
        (RangeRule, "range"),
        (SlopeRule, "slope"),
        (HotAndSlowRule, "below"),
        (FlagOffRule, "flag"),
        (SilenceRule, "seconds"),
        (ConsecutiveRule, "samples"),
        (EventRule, "event"),
    )
    for model, kind in kinds:
        if isinstance(rule, model):
            return kind
    raise TypeError(f"値の意味が決まっていないルールの型: {type(rule).__name__}")
