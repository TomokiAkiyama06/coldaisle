"""日次レポート（FR-506 / #25）。

**統計はコードが集計する。モデルは所見の文だけを書く**（#38 と同じ）。

集計元は**1分ロールアップ**であって生データではない。理由は3つ。

1. 生データは30日で消える（`config/retention.yaml`）。ロールアップは無期限
   （FR-204）なので、**過去のどの日でもあとから作り直せる**
2. **欠測率が本物になる。** `SqliteStore.stats()` の欠測率は「届いた行のうち
   ok でない割合」で下限値だが、ロールアップは `expected_count` を持つ
   （決定記録 0002 §2.8）。届かなかったサンプルはこちらにしか出ない
3. 1メトリクスあたり 1440 行で済む（生データは 34,560 行）

`coldaisle-report` として cron から1日1回呼ぶ。取り込みデーモンの中では動かさない
（ロールアップと同じ理由。`coldaisle-rollup` のあとに走らせる）。
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from coldaisle import logs
from coldaisle.ai import provider as ai_provider
from coldaisle.ai import summary as ai_summary
from coldaisle.ai.provider import AiSettings, Provider
from coldaisle.clock import Clock, WallClock
from coldaisle.llm_exclusions import METRICS_KEY, RULES_KEY, LlmExclusions, LlmExclusionsError
from coldaisle.metrics import MetricCatalog
from coldaisle.notify.models import (
    SEVERITY_LABELS,
    Notification,
    NotifyConfig,
    ReportThread,
    ThreadImage,
    display_unit,
    is_reachable_link,
)
from coldaisle.notify.notifiers import Notifier
from coldaisle.notify.notifiers import from_env as notifiers_from_env
from coldaisle.report_table import TableRow, render_table_png
from coldaisle.rules.models import RuleSet
from coldaisle.store.csv_export import day_bounds_ms
from coldaisle.store.db import Aggregation, SqliteStore
from coldaisle.store.models import AlertRecord, AlertSeverity, RollupPoint
from coldaisle.store.quality import QualityRules

LOGGER = logging.getLogger("coldaisle.report")

MINUTES_PER_DAY = 1440
COUNTER_UNIT = "count"
"""この単位を持つメトリクスは**積み上げ**として扱う（`config/metrics.yaml`）。

`sys.queue_drops` のような数え上げに最低/平均/最高を出しても意味がない。
メトリクス名で分岐しない（AGENTS.md「絶対に守るルール」9）。
"""

ALERT_SCAN_LIMIT = 2000
"""1日ぶんのアラートを数えるときの上限。**超えたらレポートに書く。**"""


class ReportConfig(BaseModel):
    """`config/report.yaml`。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str
    to: list[str]
    """宛先の名前（`stdout` / `slack` / `line`）。**空なら送らない。**"""
    summarise: bool
    max_alerts: int = Field(ge=1)
    max_notification_chars: int = Field(ge=500)
    """Webhook・LINE・stdout の 1 通に載せる長さ。LINE は5000文字で切られる。

    ファイルには残さない（決定記録 0171 §2.4）。続きはダッシュボードで見る。
    Slack の Bot の要点（0171 §2.2）はこれで切らない。
    """
    thread_title: str
    """Slack の Bot で送るときの親の題の書式（決定記録 0171）。`{month}` と `{day}` だけを使える。

    対象日の月と日を 0 埋めせずに入れる。Webhook・LINE・stdout では使わない（今までどおり 1 通）。
    """
    highlight_metrics: list[str] = Field(min_length=1)
    """要点の「主な値」に最高値を載せる metric（0171 §2.2）。その日の値が無いものは書かない。"""
    table_font: Path
    """測定値の表の画像に使う日本語のフォントのファイル（0171 §2.3）。無ければ表を文で送る。"""
    table_font_size: int = Field(ge=8, le=96)

    @field_validator("thread_title")
    @classmethod
    def _check_thread_title(cls, value: str) -> str:
        # 綴り違いの置き場所（`{mon}` など）を毎朝の送信で初めて知ることにしない
        try:
            sample = value.format(month=1, day=1)
        except (KeyError, IndexError, ValueError) as error:
            raise ValueError(
                f"thread_title の書式が不正（使えるのは {{month}} と {{day}} だけ）: {value!r}"
            ) from error
        if not sample.strip():
            raise ValueError("thread_title が空")
        return value

    def thread_title_for(self, day: date) -> str:
        """対象日の親の題（例 `📄 10月25日の振り返り`）。"""
        return self.thread_title.format(month=day.month, day=day.day)

    @field_validator("highlight_metrics")
    @classmethod
    def _check_highlights(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("highlight_metrics に同じ metric が 2 回ある")
        return value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_yaml(cls, path: Path) -> ReportConfig:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"レポートの設定が辞書ではない: {path}")
        # LLM へ渡さない一覧は `LlmExclusions.from_yaml()` が別に読む（決定記録 0131 §2.4a）。
        # 一覧の不正でレポート全体を止めない（不正なら所見だけを作らない）
        loaded = {k: v for k, v in loaded.items() if k not in (METRICS_KEY, RULES_KEY)}
        config = cls.model_validate(loaded)
        try:
            _ = config.zone  # 綴り違いは起動時に落とす（NotifyConfig と同じ）
        except Exception as error:
            raise ValueError(f"タイムゾーンが不正: {config.timezone!r}") from error
        return config


@dataclass(frozen=True)
class MetricLine:
    """メトリクス1つ分の1日。**すべてロールアップから機械的に出す。**"""

    metric: str
    label: str
    unit: str
    counter: bool
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    total: float | None = None
    prev_mean: float | None = None
    prev_total: float | None = None
    missing_ratio: float | None = None
    missing_minutes: int = 0
    """欠測率の母数になった分数。**`minutes` より少ないことがある。**

    起動バナーを受け取る前のバケットは期待サンプル数を持たない。混ぜると
    欠測率が実際より低く出るので母数から外し、**外したことを表に書く。**
    """
    minutes: int = 0
    """ロールアップ済みの分数。**1440 未満なら集計が未完了である。**"""


@dataclass(frozen=True)
class AlertLine:
    rule_id: str
    severity: AlertSeverity
    count: int


@dataclass(frozen=True)
class OngoingLine:
    """日の終わりに発火したまま続いていたアラート1件（決定記録 0131 §2.4）。"""

    rule_id: str
    severity: AlertSeverity
    fired_ms: int


@dataclass(frozen=True)
class DailyReport:
    day: date
    timezone: str
    metrics: tuple[MetricLine, ...]
    alerts: tuple[AlertLine, ...]
    alert_total: int
    prev_alert_total: int
    alerts_truncated: bool = False
    ongoing: tuple[OngoingLine, ...] = ()
    """日の終わりに発火したまま続いていたアラート（決定記録 0131 §2.4）。

    件数（その日に発火したもの）だけでは、人が直すまで続く状態（型番の不一致など）が
    2 日目以降に出ない。ルール名で分岐せず、全ルールに効く（0017 §2.3）。
    """
    ongoing_truncated: bool = False
    rule_labels: Mapping[str, str] = field(default_factory=dict)
    """`RULE_ID` → 人が読む名前（`config/rules.yaml` の `label`。決定記録 0142 §2.6）。

    名前が無いルールは ID だけを出す。
    """
    summary: str | None = None
    """AI の所見。**未検証**であり、無くてもレポートは成立する。"""
    llm_view: DailyReport | None = None
    """LLM へ渡す版（決定記録 0131 §2.4a）。除外の一覧を当てた後の集合だけから作り直したもの。

    件数・表・省略の注記・欠けの注記も、除いた後の値で作る。除く前の値を渡すと、
    その差から除いたものの存在が分かる。**`None` なら所見を作らない**（一覧が読めない）。
    """

    @property
    def has_data(self) -> bool:
        return any(line.minutes > 0 for line in self.metrics)

    def with_summary(self, summary: str | None) -> DailyReport:
        return replace(self, summary=summary)

    # -------------------------------------------------------------- 組み立て

    def as_markdown(self) -> str:
        """全文。**所見より先に測定値を置く**（#38 と同じ並び）。"""
        lines = [f"# 日次レポート {self.day.isoformat()}（{self.timezone}）", ""]
        if not self.has_data:
            lines += ["**この日のデータがありません。**", "", _NO_DATA_NOTE, ""]
        lines += self._facts()
        if self.summary:
            lines += ["## 所見（AI 生成・未検証）", "", self.summary, ""]
        return "\n".join(lines).rstrip() + "\n"

    def facts_markdown(self) -> str:
        """所見を除いた部分。**モデルに渡すのはこれだけ**（#25 / FR-504）。

        **除外の一覧を当てた版（`llm_view`）から作る**（決定記録 0131 §2.4a）。
        当てていないレポートからは作らない（人が読む本文をそのまま渡さない）。
        """
        if self.llm_view is None:
            raise ValueError("LLM へ渡す除外の一覧を当てていない（決定記録 0131 §2.4a）")
        return "\n".join(self.llm_view._facts()).rstrip() + "\n"

    def table_markdown(self) -> str:
        """測定値の表だけ。表の画像を描けない・上げられないときの返信（決定記録 0171 §2.3）。"""
        lines = self._gauge_table()
        if not lines:
            return "測定値はありません。\n"
        return "\n".join(lines).rstrip() + "\n"

    def table_rows(self) -> tuple[tuple[str, ...], list[TableRow]]:
        """測定値の表の画像の見出しと行。Markdown の表と同じ値・同じ書式（0171 §2.3）。

        名前は表示名だけにする（画像では metric 名を並べると読みにくい）。
        平均が無い行（届いたサンプルがすべて欠測など）は薄い色で描く。
        """
        rows = [
            TableRow(
                cells=(line.label, *_gauge_values(line)),
                muted=line.mean is None,
            )
            for line in self._gauges()
        ]
        return GAUGE_HEADER, rows

    def lead_text(self, highlights: Sequence[str], dashboard_url: str) -> str:
        """Slack のスレッドの要点（決定記録 0171 §2.2）。**切らない。**

        順は アラート → AI の所見 → 主な値（最高）→ 欠測の名前 → ダッシュボードのリンク。
        表（最低・平均・前日比・欠測率）は画像で見る。
        """
        zone = ZoneInfo(self.timezone)
        lines: list[str] = []
        if not self.has_data:
            lines += ["*この日のデータがありません。*", _NO_DATA_NOTE, ""]
        lines += ["*アラート*", f"発火 {self.alert_total} 件（前日 {self.prev_alert_total} 件）"]
        if self.alerts_truncated:
            lines += [f"{ALERT_SCAN_LIMIT} 件を超えたため打ち切った。"]
        if self.ongoing:
            lines += ["日の終わりに続いている:"]
            lines += [
                f"・{self._rule(o.rule_id)} [{SEVERITY_LABELS[o.severity]}] "
                f"{_local_time(o.fired_ms, zone)} から"
                for o in self.ongoing
            ]
            if self.ongoing_truncated:
                lines += [f"{ALERT_SCAN_LIMIT} 件を超えたため打ち切った。"]
        if self.summary:
            lines += ["", "*所見（AI 生成・未検証）*", self.summary]
        by_metric = {line.metric: line for line in self.metrics}
        peaks = [
            f"・{line.label} {_reading(line.maximum, line.unit)}"
            for name in highlights
            if (line := by_metric.get(name)) is not None and line.maximum is not None
        ]
        if peaks:
            lines += ["", "*主な値（最高）*", *peaks]
        missing = [
            line.label
            for line in self.metrics
            if line.missing_ratio is not None and line.missing_ratio > 0
        ]
        incomplete = any(0 < line.minutes < MINUTES_PER_DAY for line in self.metrics)
        if missing or incomplete:
            lines += ["", "*欠測*"]
            if missing:
                lines += ["、".join(missing)]
            if incomplete:
                lines += [
                    f"収録が {MINUTES_PER_DAY} 分に満たないメトリクスがある"
                    "（取り込みが止まっていたか、`coldaisle-rollup` がまだ走っていない）"
                ]
        if is_reachable_link(dashboard_url):
            lines += ["", dashboard_url]
        return "\n".join(lines).strip() + "\n"

    def _gauges(self) -> list[MetricLine]:
        return [line for line in self.metrics if not line.counter and line.minutes > 0]

    def _gauge_table(self) -> list[str]:
        gauges = self._gauges()
        if not gauges:
            return []
        lines = [
            "## 測定値",
            "",
            "| " + " | ".join(("メトリクス", *GAUGE_HEADER[1:])) + " |",
            "|---|---|---|---|---|---|---|",
        ]
        lines += [_gauge_row(line) for line in gauges]
        return [*lines, ""]

    def _facts(self) -> list[str]:
        lines: list[str] = self._gauge_table()
        counters = [line for line in self.metrics if line.counter and (line.total or 0) > 0]
        if counters:
            lines += ["## カウンタ", "", "| メトリクス | 合計 | 前日 |", "|---|---|---|"]
            lines += [_counter_row(line) for line in counters]
            lines += [""]
        lines += ["## アラート", ""]
        lines += [f"発火 {self.alert_total} 件（前日 {self.prev_alert_total} 件）"]
        if self.alerts_truncated:
            lines += ["", f"**{ALERT_SCAN_LIMIT} 件を超えたため打ち切った。**"]
        if self.alerts:
            lines += ["", "| ルール | 重大度 | 件数 |", "|---|---|---|"]
            lines += [
                f"| {self._rule(a.rule_id)} | {SEVERITY_LABELS[a.severity]} | {a.count} |"
                for a in self.alerts
            ]
        lines += [""]
        if self.ongoing:
            zone = ZoneInfo(self.timezone)
            lines += [
                "## 日の終わりに続いているアラート",
                "",
                "| ルール | 重大度 | 発火 |",
                "|---|---|---|",
            ]
            lines += [
                f"| {self._rule(o.rule_id)} | {SEVERITY_LABELS[o.severity]} "
                f"| {_local_time(o.fired_ms, zone)} |"
                for o in self.ongoing
            ]
            if self.ongoing_truncated:
                lines += ["", f"**{ALERT_SCAN_LIMIT} 件を超えたため打ち切った。**"]
            lines += [""]
        incomplete = [line for line in self.metrics if 0 < line.minutes < MINUTES_PER_DAY]
        if incomplete:
            # **「収録」が 1440 に満たない理由は2つある。** どちらかを断定しない
            lines += [
                "## 集計の欠け",
                "",
                f"**収録が {MINUTES_PER_DAY} 分に満たないメトリクスがある。**"
                " 取り込みが止まっていたか、`coldaisle-rollup` がまだ走っていない",
                "",
            ]
        return lines

    def _rule(self, rule_id: str) -> str:
        """`<名前>（<RULE_ID>）`。測定値の表の `<表示名>（<メトリクス>）` と揃える（0142 §2.6）。"""
        label = self.rule_labels.get(rule_id)
        return f"{label}（{rule_id}）" if label else rule_id


_NO_DATA_NOTE = (
    "取り込みが止まっていたか、`coldaisle-rollup` がまだ走っていない可能性がある"
    "（**このレポート自体が異常の知らせである**）。"
)


GAUGE_HEADER = ("メトリクス", "最低", "平均", "最高", "前日比(平均)", "欠測", "収録")
"""測定値の表の見出し。Markdown の表と画像で同じものを使う（決定記録 0171 §2.3）。"""


def _gauge_values(line: MetricLine) -> tuple[str, ...]:
    """`収録` は**そのメトリクスが記録されていた分数**。

    欠測率とは別物である。欠測率は「届くはずのサンプルのうち届かなかった割合」で、
    期待サンプル数を知らないと出せない（`—`）。収録はロールアップにバケットが
    あるかどうかなので、**装置ごと沈黙していた時間はこちらに出る。**
    """
    return (
        _num(line.minimum),
        _num(line.mean),
        _num(line.maximum),
        _delta(line.mean, line.prev_mean),
        _missing(line),
        f"{line.minutes}/{MINUTES_PER_DAY}",
    )


def _gauge_row(line: MetricLine) -> str:
    return "| " + " | ".join((f"{line.label}（{line.metric}）", *_gauge_values(line))) + " |"


def _reading(value: float, unit: str) -> str:
    """要点の値。アラートの本文と同じ小数 1 桁と単位（決定記録 0142 §2.4）。"""
    shown = display_unit(unit)
    return f"{value:.1f} {shown}" if shown else f"{value:.1f}"


def _counter_row(line: MetricLine) -> str:
    return f"| {line.label}（{line.metric}） | {_count(line.total)} | {_count(line.prev_total)} |"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _local_time(at_ms: int, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(at_ms / 1000, tz=zone).strftime("%Y-%m-%d %H:%M")


def _count(value: float | None) -> str:
    return "—" if value is None else f"{round(value)}"


def _delta(value: float | None, previous: float | None) -> str:
    if value is None or previous is None:
        return "—"
    return f"{value - previous:+.2f}"


def _missing(line: MetricLine) -> str:
    """欠測率。**母数が1日ぶんに満たないなら、その分数も書く。**"""
    if line.missing_ratio is None:
        return "—"
    text = f"{line.missing_ratio * 100:.1f}%"
    return text if line.missing_minutes >= line.minutes else f"{text}（{line.missing_minutes}分）"


# ---------------------------------------------------------------------- 集計


def build(
    store: SqliteStore,
    *,
    day: date,
    tz: ZoneInfo,
    catalog: MetricCatalog,
    max_alerts: int,
    exclusions: LlmExclusions | None,
    rule_labels: Mapping[str, str] | None = None,
) -> DailyReport:
    """1日ぶんを集計する。**ここに推論は入らない。**

    人が読む本文は除く前の集合から作る。`exclusions` があれば、LLM へ渡す版
    （`llm_view`）を除いた後の集合から**作り直す**（決定記録 0131 §2.4a）。除く前の
    件数から引き算しない。上限（`ALERT_SCAN_LIMIT`）で打ち切る前に除かないと、
    件数と省略の注記が除く前の集合に引きずられる。
    """
    start_ms, end_ms = day_bounds_ms(day, tz)
    prev_start_ms, prev_end_ms = day_bounds_ms(day - timedelta(days=1), tz)
    names = _metric_order(store, catalog)
    lines = tuple(
        _metric_line(store, metric, catalog, (start_ms, end_ms), (prev_start_ms, prev_end_ms))
        for metric in names
    )
    window = _Window(start_ms, end_ms, prev_start_ms, prev_end_ms)
    labels = dict(rule_labels or {})
    human = replace(_view(store, day, tz, lines, window, max_alerts=max_alerts), rule_labels=labels)
    if exclusions is None:
        return human
    excluded = exclusions.excluded_metrics(names, catalog)
    llm = _view(
        store,
        day,
        tz,
        tuple(line for line in lines if line.metric not in excluded),
        window,
        max_alerts=max_alerts,
        exclude_rule_ids=exclusions.rule_ids,
        exclude_metrics=excluded,
    )
    return replace(human, llm_view=replace(llm, rule_labels=labels))


@dataclass(frozen=True)
class _Window:
    start_ms: int
    end_ms: int
    prev_start_ms: int
    prev_end_ms: int


def _view(
    store: SqliteStore,
    day: date,
    tz: ZoneInfo,
    lines: tuple[MetricLine, ...],
    window: _Window,
    *,
    max_alerts: int,
    exclude_rule_ids: frozenset[str] = frozenset(),
    exclude_metrics: frozenset[str] = frozenset(),
) -> DailyReport:
    """与えたメトリクスの行と、除外を当てたアラートの集合から1つの版を作る。"""
    fired = _fired(store, window.start_ms, window.end_ms, exclude_rule_ids, exclude_metrics)
    counted: dict[tuple[str, AlertSeverity], int] = {}
    for record in fired:
        key = (record.rule_id, record.severity)
        counted[key] = counted.get(key, 0) + 1
    ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0][0]))[:max_alerts]
    previous = _fired(
        store, window.prev_start_ms, window.prev_end_ms, exclude_rule_ids, exclude_metrics
    )
    ongoing = store.alerts_firing_at(
        window.end_ms,
        limit=ALERT_SCAN_LIMIT,
        exclude_rule_ids=exclude_rule_ids,
        exclude_metrics=exclude_metrics,
    )
    return DailyReport(
        day=day,
        timezone=str(tz),
        metrics=lines,
        alerts=tuple(
            AlertLine(rule_id=rule, severity=severity, count=count)
            for (rule, severity), count in ranked
        ),
        alert_total=len(fired),
        prev_alert_total=len(previous),
        alerts_truncated=len(fired) >= ALERT_SCAN_LIMIT,
        ongoing=tuple(
            OngoingLine(rule_id=record.rule_id, severity=record.severity, fired_ms=record.fired_ms)
            for record in ongoing
            if record.fired_ms is not None
        ),
        ongoing_truncated=len(ongoing) >= ALERT_SCAN_LIMIT,
    )


def _fired(
    store: SqliteStore,
    start_ms: int,
    end_ms: int,
    exclude_rule_ids: frozenset[str],
    exclude_metrics: frozenset[str],
) -> list[AlertRecord]:
    """**その日に発火したもの**を数える。

    pending のまま消えたものは誰にも届いていない。日の境界は `fired_ms` に当てる
    （`SqliteStore.fired_alerts`）。条件の成立時刻に当てると、23:55 に成立して
    00:03 に発火したアラートが前日の件数に入ってしまう。
    """
    return list(
        store.fired_alerts(
            start_ms=start_ms,
            end_ms=end_ms,
            limit=ALERT_SCAN_LIMIT,
            exclude_rule_ids=exclude_rule_ids,
            exclude_metrics=exclude_metrics,
        )
    )


def _metric_order(store: SqliteStore, catalog: MetricCatalog) -> list[str]:
    """`config/metrics.yaml` の順。**定義に無いメトリクスも落とさない。**

    列挙元はロールアップ表。生データが消えた日でも、そこに何があったかは残る。
    """
    known = list(catalog.metrics)
    extra = sorted(set(store.rollup_metrics(Aggregation.MINUTE)) - set(known))
    return known + extra


def _metric_line(
    store: SqliteStore,
    metric: str,
    catalog: MetricCatalog,
    window: tuple[int, int],
    previous: tuple[int, int],
) -> MetricLine:
    meta = catalog.metrics.get(metric)
    unit = meta.unit if meta is not None else ""
    counter = unit == COUNTER_UNIT
    points = store.rollup(metric, window[0], window[1], Aggregation.MINUTE)
    folded = _fold(points, counter=counter)
    prior = _fold(
        store.rollup(metric, previous[0], previous[1], Aggregation.MINUTE), counter=counter
    )
    return MetricLine(
        metric=metric,
        label=meta.label if meta is not None else metric,
        unit=unit,
        counter=counter,
        minimum=folded.minimum,
        maximum=folded.maximum,
        mean=folded.mean,
        total=folded.total,
        prev_mean=prior.mean,
        prev_total=prior.total,
        missing_ratio=folded.missing_ratio,
        missing_minutes=folded.missing_minutes,
        minutes=len(points),
    )


@dataclass(frozen=True)
class _Folded:
    """分バケットを1日にたたんだ結果。"""

    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    total: float | None = None
    missing_ratio: float | None = None
    missing_minutes: int = 0
    """欠測率の母数になった分数。**1日ぶんとは限らない**（下記）。"""


def _fold(points: Sequence[RollupPoint], *, counter: bool) -> _Folded:
    """分バケットを1日にたたむ。

    平均は**件数で重み付けする**（決定記録 0004 §2.9）。単純平均だと、
    値の少ないバケットが値の多いバケットと同じ重さになる。
    """
    if not points:
        # **「データが無い」を「ゼロ」と書かない。** 取り込みが止まっていた日の
        # 取りこぼし件数は 0 件ではなく、分からない
        return _Folded()
    ok = sum(point.ok_value_count for point in points)
    minima = [point.min_value for point in points if point.min_value is not None]
    maxima = [point.max_value for point in points if point.max_value is not None]
    weighted = sum(
        point.mean_value * point.ok_value_count for point in points if point.mean_value is not None
    )
    # **期待サンプル数を知らないバケットを母数に混ぜない。** 起動バナーを受け取る
    # 前のバケットは `expected_count` が NULL で、これを 0 として足すと、その分の
    # `ok` だけが分子に残って欠測率が実際より低く出る（0% に張り付く）
    known = [point for point in points if point.expected_count is not None]
    expected = sum(point.expected_count or 0 for point in known)
    known_ok = sum(point.ok_value_count for point in known)
    return _Folded(
        minimum=min(minima) if minima else None,
        maximum=max(maxima) if maxima else None,
        mean=weighted / ok if ok else None,
        total=weighted if counter else None,
        # **カウンタに欠測率を出さない。** 事象が起きたときだけ書かれる値なので、
        # 期待サンプル数と比べる意味がない
        missing_ratio=(None if counter or not expected else max(0.0, 1.0 - known_ok / expected)),
        missing_minutes=0 if counter else len(known),
    )


# ---------------------------------------------------------------------- 出力


TRUNCATED_SUFFIX = "\n\n…（続きはダッシュボードで）"
"""1 通を切ったときの末尾。ファイルには残さない（決定記録 0171 §2.4）。"""

TABLE_IMAGE_COMMENT = "測定値"
"""スレッドへ上げる表の画像に添える文。"""


def as_notification(
    report: DailyReport,
    *,
    dashboard_url: str,
    max_chars: int,
    thread: ReportThread | None = None,
) -> Notification:
    """通知1通ぶん。**長ければ切る**（続きはダッシュボードで見る。決定記録 0171 §2.4）。

    `thread` は Slack の Bot だけが使う形（0171 §2.1）。Webhook・LINE・stdout の本文
    （`as_text()`）と長さの上限はこれに関係なく今と同じ。
    """
    text = report.as_markdown()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + TRUNCATED_SUFFIX
    return Notification(
        rule_id=f"日次レポート {report.day.isoformat()}",
        severity=AlertSeverity.INFO,
        state="report",
        metric=None,
        value=None,
        detail=None,
        dashboard_url=dashboard_url,
        kind="report",
        body=text,
        thread=thread,
    )


def as_thread(report: DailyReport, config: ReportConfig, *, dashboard_url: str) -> ReportThread:
    """Slack の Bot で送る形（決定記録 0171 §2.1〜§2.3）。表の画像は描けなければ付けない。"""
    header, rows = report.table_rows()
    png = render_table_png(
        header, rows, font_path=config.table_font.expanduser(), font_size=config.table_font_size
    )
    image = (
        None
        if png is None
        else ThreadImage(
            filename=f"report-{report.day.isoformat()}.png",
            content=png,
            title=f"測定値 {report.day.isoformat()}",
            comment=TABLE_IMAGE_COMMENT,
        )
    )
    return ReportThread(
        title=config.thread_title_for(report.day),
        lead=report.lead_text(config.highlight_metrics, dashboard_url),
        table_text=report.table_markdown(),
        table_image=image,
    )


def send(notification: Notification, notifiers: dict[str, Notifier], to: Sequence[str]) -> int:
    """指定された宛先へ送る。送れた数を返す。

    **`Router` は通さない。** 連投の抑制・夜間の方針・重大度の振り分けは
    どれもアラートの都合であって、決まった時刻に1通出すレポートには当たらない。
    分岐を足すと、アラートを鳴らすかどうかの判定が読みにくくなる。

    **数えるのは本文が届いた宛先。** Slack の Bot では要点（返信 1）がスレッドか
    チャンネルに届いたときだけ数える。題だけの親が届いても、要点が届かなければ
    `SlackBotNotifier` が例外を投げるので数えない（決定記録 0171 §2.5）。
    """
    sent = 0
    for name in to:
        notifier = notifiers.get(name)
        if notifier is None:
            LOGGER.warning("宛先が設定されていない", extra={logs.FIELDS_KEY: {"target": name}})
            continue
        try:
            notifier.send(notification)
        except Exception:
            # **1つ落ちても残りへ送る。** レポートは1日1回しか出ない
            LOGGER.warning(
                "レポートの送信に失敗した", exc_info=True, extra={logs.FIELDS_KEY: {"target": name}}
            )
            continue
        sent += 1
    return sent


# ---------------------------------------------------------------------- CLI


def run(
    store: SqliteStore,
    config: ReportConfig,
    catalog: MetricCatalog,
    *,
    day: date,
    summariser: Provider | None = None,
    exclusions: LlmExclusions | None = None,
    rule_labels: Mapping[str, str] | None = None,
) -> DailyReport:
    """集計して所見を付ける。**送信とファイル書き込みは呼び出し側。**

    `exclusions` が無ければ所見を作らない（決定記録 0131 §2.4a。一覧が読めないときの安全側）。
    """
    report = build(
        store,
        day=day,
        tz=config.zone,
        catalog=catalog,
        max_alerts=config.max_alerts,
        exclusions=exclusions,
        rule_labels=rule_labels,
    )
    if summariser is None or report.llm_view is None:
        return report
    return report.with_summary(ai_summary.summarise(summariser, report.facts_markdown()))


def _yesterday(clock: Clock, tz: ZoneInfo) -> date:
    return (datetime.fromtimestamp(clock.now_ms() / 1000, tz=tz) - timedelta(days=1)).date()


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-report`。cron から毎朝呼ぶ（`coldaisle-rollup` のあと）。"""
    parser = argparse.ArgumentParser(prog="coldaisle-report", description="日次レポート")
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--config", type=Path, default=Path("config/report.yaml"))
    parser.add_argument("--metrics", type=Path, default=Path("config/metrics.yaml"))
    parser.add_argument(
        "--rules",
        type=Path,
        default=Path("config/rules.yaml"),
        help="アラートの表のルールの名前（label）を読む（決定記録 0142 §2.6）",
    )
    parser.add_argument("--notify", type=Path, default=Path("config/notify.yaml"))
    parser.add_argument("--ai", type=Path, default=Path("config/ai.yaml"))
    parser.add_argument("--quality-rules", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD。既定は前日")
    parser.add_argument(
        "--no-send", action="store_true", help="送らない（`--print` と合わせて読む）"
    )
    parser.add_argument("--print", action="store_true", help="全文を標準出力へ")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logs.configure(args.log_level)
    config = ReportConfig.from_yaml(args.config)
    catalog = MetricCatalog.from_yaml(args.metrics)
    unknown = sorted(set(config.highlight_metrics) - set(catalog.metrics))
    if unknown:
        # 綴り違いで「主な値」から黙って消えないよう、起動時に落とす
        raise ValueError(f"highlight_metrics に定義の無い metric がある: {unknown}")
    rule_labels = RuleSet.from_yaml(args.rules).labels()
    notify_config = NotifyConfig.from_yaml(args.notify)
    clock = WallClock()
    day = args.date or _yesterday(clock, config.zone)

    exclusions = _exclusions(args.config)
    store = SqliteStore(args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=clock)
    try:
        report = run(
            store,
            config,
            catalog,
            day=day,
            summariser=_summariser(args.ai, config) if exclusions is not None else None,
            exclusions=exclusions,
            rule_labels=rule_labels,
        )
    finally:
        store.close()

    # ファイルには書かない（決定記録 0171 §2.4）。読みたいときは `--print`
    if args.print:
        # レポートは人が読むもの。**`--print` のときだけ**標準出力へ出す
        print(report.as_markdown())  # noqa: T201
    sent = 0
    if not args.no_send and notify_config.enabled:
        sent = send(
            as_notification(
                report,
                dashboard_url=notify_config.dashboard_url,
                max_chars=config.max_notification_chars,
                thread=as_thread(report, config, dashboard_url=notify_config.dashboard_url),
            ),
            notifiers_from_env(),
            config.to,
        )
    LOGGER.info(
        "日次レポートを作成した",
        extra={
            logs.FIELDS_KEY: {
                "day": day.isoformat(),
                "alerts": report.alert_total,
                "summary": report.summary is not None,
                "sent": sent,
            }
        },
    )
    return 0


def _exclusions(path: Path) -> LlmExclusions | None:
    """LLM へ渡さない一覧。**読めなければ所見を作らない**（決定記録 0131 §2.4a）。"""
    try:
        return LlmExclusions.from_yaml(path)
    except LlmExclusionsError as error:
        LOGGER.error(
            "LLM へ渡さない一覧が不正なので所見を作らない",
            extra={logs.FIELDS_KEY: {"config": str(path), "reason": str(error)}},
        )
        return None


def _summariser(ai_path: Path, config: ReportConfig) -> Provider | None:
    """**所見は付けられなくてもよい。** 表だけのレポートで成立する。"""
    if not config.summarise or not ai_path.exists():
        return None
    return ai_provider.from_env(AiSettings.from_yaml(ai_path))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
