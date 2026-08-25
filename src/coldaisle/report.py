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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle import logs
from coldaisle.ai import provider as ai_provider
from coldaisle.ai import summary as ai_summary
from coldaisle.ai.provider import AiSettings, Provider
from coldaisle.clock import Clock, WallClock
from coldaisle.metrics import MetricCatalog
from coldaisle.notify.models import Notification, NotifyConfig
from coldaisle.notify.notifiers import Notifier
from coldaisle.notify.notifiers import from_env as notifiers_from_env
from coldaisle.store.csv_export import day_bounds_ms
from coldaisle.store.db import Aggregation, SqliteStore
from coldaisle.store.models import AlertRecord, AlertSeverity, RollupPoint
from coldaisle.store.quality import QualityRules

LOGGER = logging.getLogger("coldaisle.report")

MINUTES_PER_DAY = 1440
COUNTER_UNIT = "count"
"""この単位を持つメトリクスは**積み上げ**として扱う（`config/metrics.yaml`）。

`sys.queue_drops` のような数え上げに最低/平均/最高を出しても意味がない。
メトリクス名で分岐しない（AGENTS.md「絶対に守るルール」6）。
"""

ALERT_SCAN_LIMIT = 2000
"""1日ぶんのアラートを数えるときの上限。**超えたらレポートに書く。**"""


class ReportConfig(BaseModel):
    """`config/report.yaml`。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str
    output_dir: Path
    to: list[str]
    """宛先の名前（`stdout` / `slack` / `line`）。**空なら送らない。**"""
    summarise: bool
    max_alerts: int = Field(ge=1)
    max_notification_chars: int = Field(ge=500)
    """通知に載せる長さ。**全文はファイルに残る。** LINE は5000文字で切られる。"""

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_yaml(cls, path: Path) -> ReportConfig:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"レポートの設定が辞書ではない: {path}")
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
    minutes: int = 0
    """ロールアップ済みの分数。**1440 未満なら集計が未完了である。**"""


@dataclass(frozen=True)
class AlertLine:
    rule_id: str
    severity: AlertSeverity
    count: int


@dataclass(frozen=True)
class DailyReport:
    day: date
    timezone: str
    metrics: tuple[MetricLine, ...]
    alerts: tuple[AlertLine, ...]
    alert_total: int
    prev_alert_total: int
    alerts_truncated: bool = False
    summary: str | None = None
    """AI の所見。**未検証**であり、無くてもレポートは成立する。"""

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
        """所見を除いた部分。**モデルに渡すのはこれだけ**（#25 / FR-504）。"""
        return "\n".join(self._facts()).rstrip() + "\n"

    def _facts(self) -> list[str]:
        lines: list[str] = []
        gauges = [line for line in self.metrics if not line.counter and line.minutes > 0]
        if gauges:
            lines += [
                "## 測定値",
                "",
                "| メトリクス | 最低 | 平均 | 最高 | 前日比(平均) | 欠測 | 収録 |",
                "|---|---|---|---|---|---|---|",
            ]
            lines += [_gauge_row(line) for line in gauges]
            lines += [""]
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
            lines += [f"| {a.rule_id} | {a.severity.value} | {a.count} |" for a in self.alerts]
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


_NO_DATA_NOTE = (
    "取り込みが止まっていたか、`coldaisle-rollup` がまだ走っていない可能性がある"
    "（**このレポート自体が異常の知らせである**）。"
)


def _gauge_row(line: MetricLine) -> str:
    """`収録` は**そのメトリクスが記録されていた分数**。

    欠測率とは別物である。欠測率は「届くはずのサンプルのうち届かなかった割合」で、
    期待サンプル数を知らないと出せない（`—`）。収録はロールアップにバケットが
    あるかどうかなので、**装置ごと沈黙していた時間はこちらに出る。**
    """
    return (
        f"| {line.label}（{line.metric}） | {_num(line.minimum)} | {_num(line.mean)} "
        f"| {_num(line.maximum)} | {_delta(line.mean, line.prev_mean)} "
        f"| {_ratio(line.missing_ratio)} | {line.minutes}/{MINUTES_PER_DAY} |"
    )


def _counter_row(line: MetricLine) -> str:
    return f"| {line.label}（{line.metric}） | {_count(line.total)} | {_count(line.prev_total)} |"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _count(value: float | None) -> str:
    return "—" if value is None else f"{round(value)}"


def _delta(value: float | None, previous: float | None) -> str:
    if value is None or previous is None:
        return "—"
    return f"{value - previous:+.2f}"


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


# ---------------------------------------------------------------------- 集計


def build(
    store: SqliteStore,
    *,
    day: date,
    tz: ZoneInfo,
    catalog: MetricCatalog,
    max_alerts: int,
) -> DailyReport:
    """1日ぶんを集計する。**ここに推論は入らない。**"""
    start_ms, end_ms = day_bounds_ms(day, tz)
    prev_start_ms, prev_end_ms = day_bounds_ms(day - timedelta(days=1), tz)
    lines = [
        _metric_line(store, metric, catalog, (start_ms, end_ms), (prev_start_ms, prev_end_ms))
        for metric in _metric_order(store, catalog)
    ]
    fired = _fired(store, start_ms, end_ms)
    counted: dict[tuple[str, AlertSeverity], int] = {}
    for record in fired:
        key = (record.rule_id, record.severity)
        counted[key] = counted.get(key, 0) + 1
    ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0][0]))[:max_alerts]
    return DailyReport(
        day=day,
        timezone=str(tz),
        metrics=tuple(lines),
        alerts=tuple(
            AlertLine(rule_id=rule, severity=severity, count=count)
            for (rule, severity), count in ranked
        ),
        alert_total=len(fired),
        prev_alert_total=len(_fired(store, prev_start_ms, prev_end_ms)),
        alerts_truncated=len(fired) >= ALERT_SCAN_LIMIT,
    )


def _fired(store: SqliteStore, start_ms: int, end_ms: int) -> list[AlertRecord]:
    """**発火したものだけ**を数える。pending のまま消えたものは誰にも届いていない。"""
    records = store.alerts(start_ms=start_ms, end_ms=end_ms, limit=ALERT_SCAN_LIMIT)
    return [record for record in records if record.fired_ms is not None]


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
    prior = store.rollup(metric, previous[0], previous[1], Aggregation.MINUTE)
    minimum, maximum, mean, total, missing = _fold(points, counter=counter)
    _, _, prev_mean, prev_total, _ = _fold(prior, counter=counter)
    return MetricLine(
        metric=metric,
        label=meta.label if meta is not None else metric,
        unit=unit,
        counter=counter,
        minimum=minimum,
        maximum=maximum,
        mean=mean,
        total=total,
        prev_mean=prev_mean,
        prev_total=prev_total,
        missing_ratio=missing,
        minutes=len(points),
    )


def _fold(
    points: Sequence[RollupPoint], *, counter: bool
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """分バケットを1日にたたむ。

    平均は**件数で重み付けする**（決定記録 0004 §2.9）。単純平均だと、
    値の少ないバケットが値の多いバケットと同じ重さになる。
    """
    if not points:
        # **「データが無い」を「ゼロ」と書かない。** 取り込みが止まっていた日の
        # 取りこぼし件数は 0 件ではなく、分からない
        return (None, None, None, None, None)
    ok = sum(point.ok_value_count for point in points)
    minima = [point.min_value for point in points if point.min_value is not None]
    maxima = [point.max_value for point in points if point.max_value is not None]
    weighted = sum(
        point.mean_value * point.ok_value_count for point in points if point.mean_value is not None
    )
    expected = sum(point.expected_count or 0 for point in points)
    return (
        min(minima) if minima else None,
        max(maxima) if maxima else None,
        weighted / ok if ok else None,
        weighted if counter else None,
        # **カウンタに欠測率を出さない。** 事象が起きたときだけ書かれる値なので、
        # 期待サンプル数と比べる意味がない
        None if counter or not expected else max(0.0, 1.0 - ok / expected),
    )


# ---------------------------------------------------------------------- 出力


def write(report: DailyReport, out_dir: Path) -> Path:
    """`<out_dir>/YYYY-MM-DD.md` に書く。**同じ日は上書き**（再実行は冪等）。"""
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report.day.isoformat()}.md"
    path.write_text(report.as_markdown(), encoding="utf-8")
    return path


def as_notification(report: DailyReport, *, dashboard_url: str, max_chars: int) -> Notification:
    """通知1通ぶん。**長ければ切る**（全文はファイルに残っている）。"""
    text = report.as_markdown()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + f"\n\n…（全文は {report.day.isoformat()}.md）"
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
    )


def send(notification: Notification, notifiers: dict[str, Notifier], to: Sequence[str]) -> int:
    """指定された宛先へ送る。送れた数を返す。

    **`Router` は通さない。** 連投の抑制・夜間の方針・重大度の振り分けは
    どれもアラートの都合であって、決まった時刻に1通出すレポートには当たらない。
    分岐を足すと、アラートを鳴らすかどうかの判定が読みにくくなる。
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
) -> DailyReport:
    """集計して所見を付ける。**送信とファイル書き込みは呼び出し側。**"""
    report = build(store, day=day, tz=config.zone, catalog=catalog, max_alerts=config.max_alerts)
    if summariser is None:
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
    parser.add_argument("--notify", type=Path, default=Path("config/notify.yaml"))
    parser.add_argument("--ai", type=Path, default=Path("config/ai.yaml"))
    parser.add_argument("--quality-rules", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD。既定は前日")
    parser.add_argument("--no-send", action="store_true", help="ファイルに書くだけ")
    parser.add_argument("--print", action="store_true", help="全文を標準出力へ")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logs.configure(args.log_level)
    config = ReportConfig.from_yaml(args.config)
    catalog = MetricCatalog.from_yaml(args.metrics)
    notify_config = NotifyConfig.from_yaml(args.notify)
    clock = WallClock()
    day = args.date or _yesterday(clock, config.zone)

    store = SqliteStore(args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=clock)
    try:
        report = run(store, config, catalog, day=day, summariser=_summariser(args.ai, config))
    finally:
        store.close()

    path = write(report, config.output_dir)
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
            ),
            notifiers_from_env(),
            config.to,
        )
    LOGGER.info(
        "日次レポートを作成した",
        extra={
            logs.FIELDS_KEY: {
                "day": day.isoformat(),
                "path": str(path),
                "alerts": report.alert_total,
                "summary": report.summary is not None,
                "sent": sent,
            }
        },
    )
    return 0


def _summariser(ai_path: Path, config: ReportConfig) -> Provider | None:
    """**所見は付けられなくてもよい。** 表だけのレポートで成立する。"""
    if not config.summarise or not ai_path.exists():
        return None
    return ai_provider.from_env(AiSettings.from_yaml(ai_path))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
