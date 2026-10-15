"""LLM へ渡さないメトリクスとアラートの一覧（レイヤ横断）。決定記録 0131 §2.4a

**LLM へ渡すものは、この一覧を当てた後の集合だけから作る**（件数・時刻・欠けの注記など
派生した値を含む）。除く前の集合から作った値を渡すと、その差から除いたものの存在が分かる。
人が見る決定論的な出力は、除く前の集合から作る。

一覧は `config/report.yaml` の `llm_excluded_metrics` / `llm_excluded_rules` に置き
（AGENTS.md ルール 9）、日次レポート・取り込み（アラートの説明）・API（AI ツールと
Server Health の AI 要約）・案件資料が**同じ1か所**を読む。コードでメトリクス名や
rule_id を分岐しない（0017 §2.3 と同じ方針）。

AI 層（L3）と API（L2）の両方が使うので、`metrics` と同じくパッケージ直下に置く。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from coldaisle.metrics import MetricCatalog

METRICS_KEY = "llm_excluded_metrics"
RULES_KEY = "llm_excluded_rules"

_METRIC_PATTERN = re.compile(r"^[a-z0-9_*]+(\.[a-z0-9_*]+)*$")
"""メトリクス名の文法に `*` だけを足したもの。`?` や `[` は受けない（読み違いを作らない）。"""
_RULE_ID = re.compile(r"^[A-Z][A-Z0-9_]*$")


class LlmExclusionsError(ValueError):
    """一覧の形が不正。**読めなければ LLM へ渡す経路を開かない**（0131 §2.4a）。"""


@dataclass(frozen=True)
class LlmExclusions:
    """LLM へ渡さないメトリクス（パターン）と rule_id。

    メトリクスのパターンの `*` は `.` を含めて何にでも一致する（`fnmatch`）。
    **広く一致するほうが安全側**（除き損ねるより除きすぎるほうがよい）。
    """

    metric_patterns: tuple[str, ...]
    rule_ids: frozenset[str]

    @classmethod
    def from_mapping(cls, loaded: object) -> LlmExclusions:
        """`config/report.yaml` の中身から作る。**キーの省略を許さない。**

        省略を「除くものが無い」と読むと、書き漏らしが LLM へ流れる。
        """
        if not isinstance(loaded, dict):
            raise LlmExclusionsError("設定が辞書ではない")
        metrics = _string_list(loaded, METRICS_KEY)
        rules = _string_list(loaded, RULES_KEY)
        for pattern in metrics:
            if not _METRIC_PATTERN.match(pattern):
                raise LlmExclusionsError(f"{METRICS_KEY} の形が不正: {pattern!r}")
        for rule_id in rules:
            if not _RULE_ID.match(rule_id):
                raise LlmExclusionsError(f"{RULES_KEY} の形が不正: {rule_id!r}")
        return cls(metric_patterns=tuple(metrics), rule_ids=frozenset(rules))

    @classmethod
    def from_yaml(cls, path: Path) -> LlmExclusions:
        """`config/report.yaml` を読む。不正・読めないときは `LlmExclusionsError`。"""
        try:
            loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise LlmExclusionsError(f"読めない: {path}: {error}") from error
        return cls.from_mapping(loaded)

    def excludes_metric(self, metric: str) -> bool:
        """このメトリクス（保存されるものも派生値も）を LLM へ渡さないか。"""
        return any(fnmatchcase(metric, pattern) for pattern in self.metric_patterns)

    def excludes_metric_or_inputs(self, metric: str, catalog: MetricCatalog) -> bool:
        """派生値は、名前か材料（被減数・減数）のどちらかが除外なら除く。

        材料が除外の派生値を渡すと、引き算から除いた値が復元できる。
        """
        if self.excludes_metric(metric):
            return True
        derived = catalog.derived.get(metric)
        return derived is not None and (
            self.excludes_metric(derived.minuend) or self.excludes_metric(derived.subtrahend)
        )

    def excludes_rule(self, rule_id: str) -> bool:
        return rule_id in self.rule_ids

    def excludes_alert(self, rule_id: str, metric: str | None, catalog: MetricCatalog) -> bool:
        """アラートを LLM へ渡さないか。**指しているメトリクスが除外でも除く**（名前が漏れる）。

        派生値を指すアラートは、材料が除外でも除く（`excludes_metric_or_inputs`）。
        """
        return self.excludes_rule(rule_id) or (
            metric is not None and self.excludes_metric_or_inputs(metric, catalog)
        )

    def excluded_metrics(self, candidates: Iterable[str], catalog: MetricCatalog) -> frozenset[str]:
        """候補（と `catalog` の派生値）のうち除くものの集合。SQL の `NOT IN` に渡す名前を作る。

        **派生値は材料が除外でも含める。** 含めないと、除外したメトリクスから作った派生値を
        指すアラートが LLM へ渡り、件数や signal から存在が分かる。
        """
        names = {*candidates, *catalog.derived}
        return frozenset(name for name in names if self.excludes_metric_or_inputs(name, catalog))


def _string_list(loaded: dict[Any, Any], key: str) -> list[str]:
    if key not in loaded:
        raise LlmExclusionsError(f"{key} が無い")
    value = loaded[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LlmExclusionsError(f"{key} は文字列のリストにする: {value!r}")
    return list(value)
