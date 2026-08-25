"""ハードウェア故障疑いの引き上げ（FR-509 / #39）。

**ローカルモデルに高価な機材の故障判断をさせない**（構想メモ §10）。
定型のアラート説明と日次レポートはローカルで済ませ、**初見の異常だけを人が
Claude へ持ち込む**ための案件資料をここで作る。

守っていること。

1. **判定に LLM を使わない。** 同じ入力からは必ず同じ結果が出る（受入基準）
2. **coldaisle は送信しない。** ファイルに書いて知らせるだけで、送るのは人（2.1）
3. **生の時系列を載せない。** 日ごとの平均と直近の統計だけ（FR-504 / 受入基準）

`coldaisle-escalate` として cron から1日1回、`coldaisle-rollup` のあとに呼ぶ。
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle import logs
from coldaisle.clock import Clock, WallClock
from coldaisle.metrics import MetricCatalog
from coldaisle.notify.models import Notification, NotifyConfig
from coldaisle.notify.notifiers import Notifier
from coldaisle.notify.notifiers import from_env as notifiers_from_env
from coldaisle.report import send
from coldaisle.rules.models import RangeRule, RuleSet, ThresholdRule
from coldaisle.store.csv_export import day_bounds_ms
from coldaisle.store.db import Aggregation, SqliteStore
from coldaisle.store.models import AlertSeverity, RollupPoint
from coldaisle.store.quality import QualityRules

LOGGER = logging.getLogger("coldaisle.escalate")

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


# ---------------------------------------------------------------------- 設定


class _Trigger(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    question: str
    """争点。**人が書いた文をそのまま載せる。** ここを生成させない（受入基準）。"""


class StepChangeTrigger(_Trigger):
    """段階的でない急変が続いている（例: サーマルパッド劣化）。"""

    metric: str
    delta: float = Field(gt=0)
    """隣り合う日の平均がこれ以上動いたら「急変」とみなす。"""
    hold_days: int = Field(ge=1)
    """その水準がこれだけ続いたら引き上げる。**1日で騒がない。**"""


class SustainedDeviationTrigger(_Trigger):
    """ベースラインからの持続的逸脱。"""

    metric: str
    baseline: float
    tolerance: float = Field(gt=0)
    hold_hours: int = Field(ge=1)


class RepeatedCriticalTrigger(_Trigger):
    """同じ critical が繰り返し出る。**1回で引き上げない**（対処済みかもしれない）。"""

    min_count: int = Field(ge=2)
    window_days: int = Field(ge=1)


class EscalationRules(BaseModel):
    """`config/escalation.yaml` 全体。**すべての引き金を明示する**（rules.yaml と同じ）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str
    output_dir: Path
    to: list[str]
    triggers_note: str = ""
    """設定ファイルの読み手向けの覚え書き。**評価には使わない。**"""
    course_days: int = Field(ge=2)
    """経過に載せる日数。**生の時系列は載せない**（受入基準）。"""
    max_chars: int = Field(ge=500)
    step_change: StepChangeTrigger
    sustained_deviation: SustainedDeviationTrigger
    repeated_critical: RepeatedCriticalTrigger

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_yaml(cls, path: Path) -> EscalationRules:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"引き上げの設定が辞書ではない: {path}")
        rules = cls.model_validate(loaded)
        try:
            _ = rules.zone
        except Exception as error:
            raise ValueError(f"タイムゾーンが不正: {rules.timezone!r}") from error
        return rules


# ---------------------------------------------------------------------- 判定


@dataclass(frozen=True)
class Finding:
    """引き上げの候補1件。**LLM は関与しない。**"""

    trigger: str
    problem: str
    question: str
    started: date
    """案件の起点。**同じ案件を毎日作らない**ための鍵でもある。"""
    metric: str | None = None
    rule_id: str | None = None

    @property
    def slug(self) -> str:
        target = self.metric or self.rule_id or "system"
        return f"{self.started.isoformat()}-{self.trigger}-{target.replace('.', '_')}"


def evaluate(
    store: SqliteStore,
    rules: EscalationRules,
    catalog: MetricCatalog,
    *,
    day: date,
) -> list[Finding]:
    """`day` の終わりまでを見て候補を返す。**判定は決定論的**（受入基準）。"""
    _, end_ms = day_bounds_ms(day, rules.zone)
    found = [
        _step_change(store, rules, catalog, day=day),
        _sustained_deviation(store, rules, catalog, end_ms=end_ms),
        _repeated_critical(store, rules, end_ms=end_ms),
    ]
    return [finding for finding in found if finding is not None]


def _step_change(
    store: SqliteStore,
    rules: EscalationRules,
    catalog: MetricCatalog,
    *,
    day: date,
) -> Finding | None:
    """**急に変わって、そのまま戻らない**ものを探す。

    1日だけの跳ねは拾わない（負荷の山かもしれない）。跳ねたあと `hold_days`
    ぶん水準が戻らないことを条件にする。
    """
    trigger = rules.step_change
    if not trigger.enabled:
        return None
    series = daily_means(
        store, trigger.metric, catalog, day=day, days=rules.course_days, tz=rules.zone
    )
    values = [(at, value) for at, value in series if value is not None]
    if len(values) < trigger.hold_days + 2:
        return None
    # 直近 hold_days ぶんが「跳ねたあとの水準」で、その手前に段差があること
    tail = values[-trigger.hold_days :]
    before = values[: -trigger.hold_days]
    step = tail[0][1] - before[-1][1]
    if abs(step) < trigger.delta:
        return None
    if any(abs(value - tail[0][1]) >= trigger.delta for _, value in tail[1:]):
        return None  # 跳ねたあとも動いている。段差ではなく変動
    unit = catalog.unit_for(trigger.metric) or ""
    return Finding(
        trigger="step_change",
        metric=trigger.metric,
        started=tail[0][0],
        problem=(
            f"{trigger.metric} が {before[-1][0].isoformat()} の {before[-1][1]:.1f}{unit} から "
            f"{tail[0][0].isoformat()} に {tail[0][1]:.1f}{unit} へ変わり、"
            f"{len(tail)} 日そのまま続いている（差 {step:+.1f}{unit}）"
        ),
        question=trigger.question,
    )


def _sustained_deviation(
    store: SqliteStore,
    rules: EscalationRules,
    catalog: MetricCatalog,
    *,
    end_ms: int,
) -> Finding | None:
    """ベースラインから外れたまま戻らないものを探す。"""
    trigger = rules.sustained_deviation
    if not trigger.enabled:
        return None
    hours = _hourly_means(store, trigger.metric, catalog, end_ms=end_ms, hours=trigger.hold_hours)
    if len(hours) < trigger.hold_hours or any(value is None for _, value in hours):
        return None  # **欠けている時間を「正常」とみなさない**
    outside = [
        (at, value)
        for at, value in hours
        if value is not None and abs(value - trigger.baseline) > trigger.tolerance
    ]
    if len(outside) < trigger.hold_hours:
        return None
    unit = catalog.unit_for(trigger.metric) or ""
    worst = max(outside, key=lambda item: abs(item[1] - trigger.baseline))
    return Finding(
        trigger="sustained_deviation",
        metric=trigger.metric,
        started=datetime.fromtimestamp(outside[0][0] / 1000, tz=rules.zone).date(),
        problem=(
            f"{trigger.metric} が基準 {trigger.baseline:.1f}{unit} ± {trigger.tolerance:.1f} を"
            f"{trigger.hold_hours} 時間続けて外れている（最大 {worst[1]:.1f}{unit}）"
        ),
        question=trigger.question,
    )


def _repeated_critical(
    store: SqliteStore, rules: EscalationRules, *, end_ms: int
) -> Finding | None:
    """**同じ critical が繰り返し出る**なら、対処が効いていない。"""
    trigger = rules.repeated_critical
    if not trigger.enabled:
        return None
    start_ms = end_ms - trigger.window_days * DAY_MS
    fired = [
        record
        for record in store.fired_alerts(start_ms=start_ms, end_ms=end_ms, limit=200)
        if record.severity is AlertSeverity.CRITICAL
    ]
    counted: dict[str, list[int]] = {}
    for record in fired:
        counted.setdefault(record.rule_id, []).append(record.fired_ms or 0)
    for rule_id, times in sorted(counted.items()):
        if len(times) < trigger.min_count:
            continue
        first = min(times)
        return Finding(
            trigger="repeated_critical",
            rule_id=rule_id,
            started=datetime.fromtimestamp(first / 1000, tz=rules.zone).date(),
            problem=(
                f"{rule_id}（critical）が {trigger.window_days} 日で {len(times)} 回発火している。"
                "対処が効いていないか、原因が別にある"
            ),
            question=trigger.question,
        )
    return None


# ------------------------------------------------------------------ 値の読み出し


def daily_means(
    store: SqliteStore,
    metric: str,
    catalog: MetricCatalog,
    *,
    day: date,
    days: int,
    tz: ZoneInfo | None = None,
) -> list[tuple[date, float | None]]:
    """日ごとの平均。**派生値は1日平均どうしの差**として出す。

    派生値は保存されない（決定記録 0002 §2.2）ので、被減数と減数それぞれの
    1日平均を引く。生の差の平均とは厳密には一致しないが、段差を見るには足りる。
    **案件資料にその旨を書く**（`_basis`）。
    """
    zone = tz or ZoneInfo("UTC")
    if metric in catalog.derived:
        derived = catalog.derived[metric]
        left = dict(daily_means(store, derived.minuend, catalog, day=day, days=days, tz=zone))
        right = dict(daily_means(store, derived.subtrahend, catalog, day=day, days=days, tz=zone))
        days_out: list[tuple[date, float | None]] = []
        for at in sorted(left):
            minuend, subtrahend = left[at], right.get(at)
            # **片方が欠けたら差も無い。** 片側だけで代用すると、もっともらしい
            # 数字が出てしまう（決定記録 0009 §2.2 と同じ理由）
            days_out.append(
                (at, None if minuend is None or subtrahend is None else minuend - subtrahend)
            )
        return days_out
    out: list[tuple[date, float | None]] = []
    for offset in range(days - 1, -1, -1):
        at = day - timedelta(days=offset)
        start_ms, end_ms = day_bounds_ms(at, zone)
        out.append((at, _weighted(store.rollup(metric, start_ms, end_ms, Aggregation.HOUR))))
    return out


def _hourly_means(
    store: SqliteStore,
    metric: str,
    catalog: MetricCatalog,
    *,
    end_ms: int,
    hours: int,
) -> list[tuple[int, float | None]]:
    if metric in catalog.derived:
        derived = catalog.derived[metric]
        left = dict(_hourly_means(store, derived.minuend, catalog, end_ms=end_ms, hours=hours))
        right = dict(_hourly_means(store, derived.subtrahend, catalog, end_ms=end_ms, hours=hours))
        hours_out: list[tuple[int, float | None]] = []
        for at in sorted(left):
            minuend, subtrahend = left[at], right.get(at)
            hours_out.append(
                (at, None if minuend is None or subtrahend is None else minuend - subtrahend)
            )
        return hours_out
    out: list[tuple[int, float | None]] = []
    for offset in range(hours, 0, -1):
        start = end_ms - offset * HOUR_MS
        out.append(
            (start, _weighted(store.rollup(metric, start, start + HOUR_MS, Aggregation.HOUR)))
        )
    return out


def _weighted(points: Sequence[RollupPoint]) -> float | None:
    """件数で重み付けた平均（決定記録 0004 §2.9）。"""
    ok = sum(point.ok_value_count for point in points)
    if not ok:
        return None
    return sum(point.mean_value * point.ok_value_count for point in points if point.mean_value) / ok


# ---------------------------------------------------------------------- 案件資料


PREAMBLE = """> この資料は coldaisle が自動生成しました。**送信は行っていません。**
> 内容を確認したうえで、必要と判断したら人が Claude へ渡してください。
> coldaisle にファン制御・電源操作の手段はありません（AGENTS.md ルール2）。"""

DERIVED_BASIS = "（派生値のため、1日平均どうしの差から算出）"


def build_packet(
    store: SqliteStore,
    finding: Finding,
    rules: EscalationRules,
    catalog: MetricCatalog,
    *,
    day: date,
    rule_set: RuleSet | None = None,
) -> str:
    """圧縮した案件資料（構想メモ §10）。**生の時系列は入らない。**"""
    _, end_ms = day_bounds_ms(day, rules.zone)
    lines = [
        f"# エスカレーション案件 {day.isoformat()}",
        "",
        PREAMBLE,
        "",
        "## 問題",
        "",
        finding.problem,
        "",
    ]
    lines += _measurements(store, finding, catalog, end_ms=end_ms)
    lines += _course(store, finding, rules, catalog, day=day)
    lines += _checked(store, end_ms=end_ms, rule_set=rule_set)
    lines += ["## 争点", "", finding.question, ""]
    text = "\n".join(lines).rstrip() + "\n"
    if len(text) > rules.max_chars:
        # **切ったことを書く。** 黙って切ると、読む側は全部だと思う
        text = text[: rules.max_chars].rstrip() + "\n\n…（上限で切りました）\n"
    return text


def _measurements(
    store: SqliteStore, finding: Finding, catalog: MetricCatalog, *, end_ms: int
) -> list[str]:
    """直近24時間の統計。**関係するメトリクスだけ**を載せる。"""
    metrics = _related(finding, catalog)
    if not metrics:
        return []
    lines = [
        "## 測定値（直近24時間）",
        "",
        "| メトリクス | 平均 | p95 | 最高 | 欠測 |",
        "|---|---|---|---|---|",
    ]
    for metric in metrics:
        stats = store.stats(metric, end_ms - DAY_MS, end_ms)
        lines.append(
            f"| {metric} | {_num(stats.mean_value)} | {_num(stats.p95_value)} "
            f"| {_num(stats.max_value)} | {_pct(stats.missing_ratio)} |"
        )
    return [*lines, ""]


def _related(finding: Finding, catalog: MetricCatalog) -> list[str]:
    """案件に関係する**測定**メトリクス。派生値はその材料に展開する。"""
    if finding.metric is None:
        return []
    if finding.metric in catalog.derived:
        derived = catalog.derived[finding.metric]
        return [derived.minuend, derived.subtrahend]
    return [finding.metric]


def _course(
    store: SqliteStore,
    finding: Finding,
    rules: EscalationRules,
    catalog: MetricCatalog,
    *,
    day: date,
) -> list[str]:
    """経過。**日ごとの平均だけ**（生の点は載せない。受入基準）。"""
    if finding.metric is None:
        return []
    series = daily_means(
        store, finding.metric, catalog, day=day, days=rules.course_days, tz=rules.zone
    )
    basis = DERIVED_BASIS if finding.metric in catalog.derived else ""
    lines = [f"## 経過（{finding.metric} の日平均）{basis}", "", "| 日 | 平均 |", "|---|---|"]
    lines += [f"| {at.isoformat()} | {_num(value)} |" for at, value in series]
    return [*lines, ""]


def _checked(store: SqliteStore, *, end_ms: int, rule_set: RuleSet | None = None) -> list[str]:
    """**自動で確認したこと**。人が「それは見た」と言えるようにする。

    ここに書くのは**測って分かること**だけ。「負荷パターンに変化なし」のような、
    測っていないことは書かない（それは争点であって、確認済みではない）。
    """
    latest = store.latest(at_ms=end_ms)
    bad = sorted(
        f"{metric}={reading.quality.value}"
        for metric, reading in latest.items()
        if reading.quality.value != "ok"
    )
    lines = ["## 試行済み（coldaisle が自動で確認したこと）", ""]
    lines.append(
        "- センサーの品質: すべて ok"
        if not bad
        else f"- **ok でないセンサーがある**: {', '.join(bad[:5])}"
    )
    counts = {
        metric: store.stats(metric, end_ms - DAY_MS, end_ms)
        for metric in ("sys.dropped_samples", "sys.queue_drops")
        if metric in latest
    }
    for metric, stats in counts.items():
        total = (stats.mean_value or 0) * stats.ok_value_count
        lines.append(f"- {metric}（24時間の合計）: {round(total)}")
    if not counts:
        lines.append("- 取りこぼしの記録なし（24時間）")
    lines += _within_thresholds(store, latest, end_ms=end_ms, rule_set=rule_set)
    return [*lines, ""]


def _within_thresholds(
    store: SqliteStore,
    latest: dict[str, Any],
    *,
    end_ms: int,
    rule_set: RuleSet | None,
) -> list[str]:
    """閾値を持つルールについて、**24時間の p95 が中に収まっているか**を書く。

    「吸気温は正常であることを確認」（Issue の例）がこれ。判定は `config/rules.yaml`
    の閾値をそのまま使う。**別の基準を持たない。**
    """
    if rule_set is None:
        return []
    lines: list[str] = []
    for name, rule in sorted(rule_set):
        if not isinstance(rule, ThresholdRule | RangeRule) or not rule.enabled:
            continue
        # 派生値は保存されないので統計を引けない（決定記録 0002 §2.2）。
        # **無い値を推測してまで1行増やさない**
        if rule.metric not in latest:
            continue
        p95 = store.stats(rule.metric, end_ms - DAY_MS, end_ms).p95_value
        if p95 is None:
            continue
        if isinstance(rule, ThresholdRule):
            inside = p95 < rule.threshold
            bound = f"閾値 {rule.threshold:.1f} 未満"
        else:
            inside = rule.low < p95 < rule.high
            bound = f"範囲 {rule.low:.1f}〜{rule.high:.1f}"
        mark = "正常範囲" if inside else "**外れている**"
        lines.append(f"- {rule.metric}（24時間 p95 {p95:.1f}）: {mark}（{name} の{bound}）")
    return lines


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def write_packet(text: str, finding: Finding, out_dir: Path, *, force: bool = False) -> Path | None:
    """書けたパスを返す。**同じ案件が既にあるなら書かない**（毎日作らない）。"""
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{finding.slug}.md"
    if path.exists() and not force:
        LOGGER.info("同じ案件は作成済み", extra={logs.FIELDS_KEY: {"path": str(path)}})
        return None
    path.write_text(text, encoding="utf-8")
    return path


def as_notification(finding: Finding, path: Path, *, dashboard_url: str) -> Notification:
    """**「資料ができた」と知らせるだけ。** 中身も送らない（受入基準）。"""
    return Notification(
        rule_id=f"エスカレーション候補 {finding.trigger}",
        severity=AlertSeverity.WARNING,
        state="escalation",
        metric=finding.metric,
        value=None,
        detail=None,
        dashboard_url=dashboard_url,
        kind="report",
        body=(
            f"{finding.problem}\n\n"
            f"案件資料: {path}\n"
            "**送信していません。** 内容を確認してから、必要なら人が Claude へ渡してください。"
        ),
    )


# ---------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-escalate`。cron から1日1回、`coldaisle-rollup` のあとに呼ぶ。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-escalate",
        description="ハードウェア故障疑いの案件資料を作る（送信はしない）",
    )
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--config", type=Path, default=Path("config/escalation.yaml"))
    parser.add_argument("--metrics", type=Path, default=Path("config/metrics.yaml"))
    parser.add_argument("--notify", type=Path, default=Path("config/notify.yaml"))
    parser.add_argument("--quality-rules", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--rules", type=Path, default=Path("config/rules.yaml"))
    parser.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD。既定は前日")
    parser.add_argument("--force", action="store_true", help="同じ案件でも作り直す")
    parser.add_argument("--print", action="store_true", help="全文を標準出力へ")
    parser.add_argument("--no-send", action="store_true", help="知らせを出さない")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logs.configure(args.log_level)
    rules = EscalationRules.from_yaml(args.config)
    catalog = MetricCatalog.from_yaml(args.metrics)
    notify_config = NotifyConfig.from_yaml(args.notify)
    rule_set = RuleSet.from_yaml(args.rules)
    clock: Clock = WallClock()
    day = args.date or _yesterday(clock, rules.zone)

    store = SqliteStore(args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=clock)
    try:
        findings = evaluate(store, rules, catalog, day=day)
        written = [
            (finding, path)
            for finding in findings
            for path in [
                write_packet(
                    build_packet(store, finding, rules, catalog, day=day, rule_set=rule_set),
                    finding,
                    rules.output_dir,
                    force=args.force,
                )
            ]
            if path is not None
        ]
        if args.print:
            for _, path in written:
                print(path.read_text(encoding="utf-8"))  # noqa: T201
    finally:
        store.close()

    if not args.no_send and notify_config.enabled:
        notifiers: dict[str, Notifier] = notifiers_from_env()
        for finding, path in written:
            send(
                as_notification(finding, path, dashboard_url=notify_config.dashboard_url),
                notifiers,
                rules.to,
            )
    LOGGER.info(
        "引き上げの判定を実行した",
        extra={
            logs.FIELDS_KEY: {
                "day": day.isoformat(),
                "found": len(findings),
                "written": len(written),
            }
        },
    )
    return 0


def _yesterday(clock: Clock, tz: ZoneInfo) -> date:
    return (datetime.fromtimestamp(clock.now_ms() / 1000, tz=tz) - timedelta(days=1)).date()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
