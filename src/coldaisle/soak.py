"""連続運転テスト（soak）の集計（#47 / issues/15-soak-test.md）。

**既存の DB を読むだけ。** 取り込み・ロールアップ・通知のどれにも触らない。
24時間連続運転のあとに `coldaisle-soak-report` を1回呼び、人が読む Markdown と
機械が読む JSON を残す。

集計元は**生データ**（`readings`）。理由は2つ。

1. `suspect` の件数はロールアップに残らない（決定記録 0002 §2.8 の列に無い）
2. 期間の端が分の途中でもよい。soak の開始時刻は人が決める

欠測率は「届くはずのサンプルのうち、`ok` の値として届かなかった割合」
（決定記録 0002 §2.8 と同じ分子）。**母数は期間の長さと送信周期から出す。**
行の数を母数にすると、装置ごと沈黙していた時間が欠測に数えられない。

**この集計に無いもの**（DB に記録経路が無い。#47 の対象外）:

- シリアルの再接続回数（ログにだけ出る）
- デーモンの RSS 推移
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle import logs
from coldaisle.channels import (
    CHANNEL_TO_METRIC,
    DEVICE_RESTART_METRIC,
    DROPPED_SAMPLES_METRIC,
    QUEUE_DROPS_METRIC,
)
from coldaisle.clock import Clock, WallClock
from coldaisle.store import Quality, SqliteStore
from coldaisle.store.quality import QualityRules

LOGGER = logging.getLogger("coldaisle.soak")

PERIODIC_METRICS: tuple[str, ...] = tuple(CHANNEL_TO_METRIC.values())
"""欠測率を出すメトリクス。**周期的に届くデバイスのチャネルだけ**（決定記録 0008 §2.1.2）。"""

NOT_COVERED: tuple[str, ...] = (
    "シリアルの再接続回数（DB に記録経路が無い。ログから数える）",
    "デーモンの RSS 推移（DB に記録経路が無い）",
)
"""受入基準のうち、この集計では判定できない項目。**レポートに必ず書く。**"""


class Thresholds(BaseModel):
    """合否判定の閾値（`config/soak.yaml`）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    missing_ratio_below: float = Field(gt=0, le=1)
    """欠測率がこれ**未満**なら合格（NFR-02）。"""
    max_device_restarts: int = Field(ge=0)
    """デバイス再起動がこの回数**以下**なら合格。"""


class SoakConfig(BaseModel):
    """`config/soak.yaml`。**既定値をコードに置かない**（AGENTS.md ルール9）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str
    duration_hours: float = Field(gt=0)
    thresholds: Thresholds
    output_dir: Path

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_yaml(cls, path: Path) -> SoakConfig:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"soak の設定が辞書ではない: {path}")
        config = cls.model_validate(loaded)
        try:
            _ = config.zone  # 綴り違いは起動時に落とす（ReportConfig と同じ）
        except Exception as error:
            raise ValueError(f"タイムゾーンが不正: {config.timezone!r}") from error
        return config


class Verdict(StrEnum):
    """判定。**分からないものを合格と書かない。**"""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


_VERDICT_LABEL = {Verdict.PASS: "合格", Verdict.FAIL: "不合格", Verdict.UNKNOWN: "判定不能"}


@dataclass(frozen=True)
class MetricLine:
    """周期メトリクス1つ分の期間。"""

    metric: str
    expected: int | None
    """期待サンプル数。送信周期が分からなければ `None`。"""
    ok: int
    suspect: int
    missing: int
    stale: int

    @property
    def rows(self) -> int:
        return self.ok + self.suspect + self.missing + self.stale

    @property
    def missing_ratio(self) -> float | None:
        """`1 - ok / expected`。**`suspect` も欠測に入る**（決定記録 0002 §2.8 と同じ分子）。

        受信時刻の揺れで `ok` が期待数をわずかに超えることがあるので 0 で止める。
        """
        if not self.expected:
            return None
        return max(0.0, 1.0 - self.ok / self.expected)


@dataclass(frozen=True)
class Check:
    """受入基準1項目の判定。"""

    name: str
    observed: float | None
    display: str
    """`observed` を人が読む形にしたもの（Markdown 用）。"""
    threshold: str
    verdict: Verdict
    note: str = ""


@dataclass(frozen=True)
class SoakReport:
    start_ms: int
    end_ms: int
    timezone: str
    interval_ms: int | None
    complete: bool
    """期間の終わりが集計時刻より前か。**終わっていない期間は判定しない。**"""
    metrics: tuple[MetricLine, ...]
    dropped_samples: int
    device_restarts: int
    queue_drops: int
    checks: tuple[Check, ...]

    @property
    def has_data(self) -> bool:
        return any(line.rows > 0 for line in self.metrics)

    @property
    def suspect_total(self) -> int:
        return sum(line.suspect for line in self.metrics)

    @property
    def verdict(self) -> Verdict:
        """判定した項目の総合。1つでも不合格なら不合格、判定不能が残れば判定不能。"""
        verdicts = {check.verdict for check in self.checks}
        if Verdict.FAIL in verdicts:
            return Verdict.FAIL
        if Verdict.UNKNOWN in verdicts or not verdicts:
            return Verdict.UNKNOWN
        return Verdict.PASS

    # ---------------------------------------------------------------- 出力

    def _local(self, at_ms: int) -> str:
        return datetime.fromtimestamp(at_ms / 1000, tz=ZoneInfo(self.timezone)).isoformat()

    def as_dict(self) -> dict[str, Any]:
        """JSON の形。**数値は丸めない**（丸めは Markdown の側だけ）。"""
        return {
            "window": {
                "start": self._local(self.start_ms),
                "end": self._local(self.end_ms),
                "start_ms": self.start_ms,
                "end_ms": self.end_ms,
                "timezone": self.timezone,
                "complete": self.complete,
            },
            "interval_ms": self.interval_ms,
            "verdict": self.verdict.value,
            "checks": [
                {
                    "name": check.name,
                    "observed": check.observed,
                    "threshold": check.threshold,
                    "verdict": check.verdict.value,
                    "note": check.note,
                }
                for check in self.checks
            ],
            "counters": {
                DROPPED_SAMPLES_METRIC: self.dropped_samples,
                DEVICE_RESTART_METRIC: self.device_restarts,
                QUEUE_DROPS_METRIC: self.queue_drops,
                "suspect": self.suspect_total,
            },
            "metrics": [
                {
                    "metric": line.metric,
                    "expected": line.expected,
                    "rows": line.rows,
                    "ok": line.ok,
                    "suspect": line.suspect,
                    "missing": line.missing,
                    "stale": line.stale,
                    "missing_ratio": line.missing_ratio,
                }
                for line in self.metrics
            ],
            "not_covered": list(NOT_COVERED),
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n"

    def as_markdown(self) -> str:
        """人が読む全文。**判定を先に、根拠の表をあとに置く。**"""
        lines = [
            "# 連続運転テスト（soak）集計",
            "",
            f"- 期間: {self._local(self.start_ms)} 〜 {self._local(self.end_ms)}",
            f"- 送信周期: {'不明' if self.interval_ms is None else f'{self.interval_ms} ms'}",
            f"- 判定（DB で判定できる項目）: **{_VERDICT_LABEL[self.verdict]}**",
            "",
        ]
        if not self.complete:
            lines += ["**期間がまだ終わっていないため判定しない。**", ""]
        if not self.has_data:
            lines += ["**この期間のデータがありません。**", ""]
        lines += ["## 判定", "", "| 項目 | 観測値 | 基準 | 判定 | 備考 |", "|---|---|---|---|---|"]
        lines += [
            f"| {check.name} | {check.display} | {check.threshold} "
            f"| {_VERDICT_LABEL[check.verdict]} | {check.note} |"
            for check in self.checks
        ]
        lines += [
            "",
            "## 件数",
            "",
            "| 項目 | 件数 |",
            "|---|---|",
            f"| seq の欠損（`{DROPPED_SAMPLES_METRIC}` の合計） | {self.dropped_samples} |",
            f"| デバイス再起動（`{DEVICE_RESTART_METRIC}`） | {self.device_restarts} |",
            f"| 待ち行列の溢れ（`{QUEUE_DROPS_METRIC}` の合計） | {self.queue_drops} |",
            f"| `suspect`（周期メトリクスの合計） | {self.suspect_total} |",
            "",
            "## チャネル別",
            "",
            "| メトリクス | 期待 | 受信行 | ok | suspect | missing | 欠測率 |",
            "|---|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {line.metric} | {_count(line.expected)} | {line.rows} | {line.ok} "
            f"| {line.suspect} | {line.missing} | {_ratio(line.missing_ratio)} |"
            for line in self.metrics
        ]
        lines += ["", "## この集計に無いもの", ""]
        lines += [f"- {item}" for item in NOT_COVERED]
        return "\n".join(lines).rstrip() + "\n"


def _count(value: int | None) -> str:
    return "—" if value is None else str(value)


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.3f}%"


# ---------------------------------------------------------------------- 集計


def build(
    store: SqliteStore,
    *,
    start_ms: int,
    end_ms: int,
    now_ms: int,
    config: SoakConfig,
) -> SoakReport:
    """期間 `[start_ms, end_ms)` を集計して判定する。**DB には書かない。**"""
    if start_ms >= end_ms:
        raise ValueError(f"期間が空か逆転している: start_ms={start_ms} end_ms={end_ms}")
    interval_ms = store.latest_interval_ms()
    expected = None if interval_ms is None else (end_ms - start_ms) // interval_ms
    metrics = tuple(
        _metric_line(store, metric, start_ms, end_ms, expected) for metric in PERIODIC_METRICS
    )
    counters = {
        metric: _event_total(store, metric, start_ms, end_ms)
        for metric in (DROPPED_SAMPLES_METRIC, DEVICE_RESTART_METRIC, QUEUE_DROPS_METRIC)
    }
    complete = end_ms <= now_ms
    has_data = any(line.rows > 0 for line in metrics)
    return SoakReport(
        start_ms=start_ms,
        end_ms=end_ms,
        timezone=config.timezone,
        interval_ms=interval_ms,
        complete=complete,
        metrics=metrics,
        dropped_samples=counters[DROPPED_SAMPLES_METRIC],
        device_restarts=counters[DEVICE_RESTART_METRIC],
        queue_drops=counters[QUEUE_DROPS_METRIC],
        checks=(
            _missing_check(metrics, config.thresholds, complete=complete, has_data=has_data),
            _restart_check(
                counters[DEVICE_RESTART_METRIC],
                config.thresholds,
                complete=complete,
                has_data=has_data,
            ),
        ),
    )


def _metric_line(
    store: SqliteStore, metric: str, start_ms: int, end_ms: int, expected: int | None
) -> MetricLine:
    counts = store.quality_counts(metric, start_ms, end_ms)
    return MetricLine(
        metric=metric,
        expected=expected,
        ok=counts[Quality.OK],
        suspect=counts[Quality.SUSPECT],
        missing=counts[Quality.MISSING],
        stale=counts[Quality.STALE],
    )


def _event_total(store: SqliteStore, metric: str, start_ms: int, end_ms: int) -> int:
    """事象メトリクスの合計。**起きたときしか書かれない**ので、行が無ければ 0 件。"""
    return round(
        sum(point.value for point in store.series(metric, start_ms, end_ms) if point.value)
    )


def _missing_check(
    metrics: Sequence[MetricLine], thresholds: Thresholds, *, complete: bool, has_data: bool
) -> Check:
    """**最も悪いチャネル**で判定する。平均すると、1本だけ死んだプローブが薄まる。"""
    name = "欠測率（最も悪いチャネル）"
    limit = f"< {thresholds.missing_ratio_below * 100:g}%"
    ratios = [(line.missing_ratio, line.metric) for line in metrics]
    known = [(ratio, metric) for ratio, metric in ratios if ratio is not None]
    if not known:
        return Check(
            name, None, "—", limit, Verdict.UNKNOWN, "送信周期が分からない（起動バナー無し）"
        )
    worst, metric = max(known)
    display = _ratio(worst)
    if not complete or not has_data:
        return Check(name, worst, display, limit, Verdict.UNKNOWN, _why_unknown(complete))
    verdict = Verdict.PASS if worst < thresholds.missing_ratio_below else Verdict.FAIL
    return Check(name, worst, display, limit, verdict, metric)


def _restart_check(
    restarts: int, thresholds: Thresholds, *, complete: bool, has_data: bool
) -> Check:
    name = "デバイス再起動"
    limit = f"≤ {thresholds.max_device_restarts} 回"
    note = "意図した再起動かどうかは DB から区別できない"
    display = f"{restarts} 回"
    if not complete or not has_data:
        return Check(name, float(restarts), display, limit, Verdict.UNKNOWN, _why_unknown(complete))
    verdict = Verdict.PASS if restarts <= thresholds.max_device_restarts else Verdict.FAIL
    return Check(name, float(restarts), display, limit, verdict, note)


def _why_unknown(complete: bool) -> str:
    return "期間がまだ終わっていない" if not complete else "期間にデータが無い"


# ---------------------------------------------------------------------- 出力


def write(report: SoakReport, out_dir: Path) -> tuple[Path, Path]:
    """`<out_dir>/soak-<開始時刻>.md` と `.json` に書く。**同じ開始時刻は上書き**（冪等）。"""
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.fromtimestamp(report.start_ms / 1000, tz=ZoneInfo(report.timezone))
    stem = f"soak-{started.strftime('%Y%m%dT%H%M%S')}"
    markdown = out_dir / f"{stem}.md"
    data = out_dir / f"{stem}.json"
    markdown.write_text(report.as_markdown(), encoding="utf-8")
    data.write_text(report.as_json(), encoding="utf-8")
    return markdown, data


# ---------------------------------------------------------------------- CLI


def parse_time(text: str, zone: ZoneInfo) -> int:
    """ISO 8601 を Unix ms にする。**タイムゾーンが無ければ設定のものとみなす。**"""
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return int(parsed.timestamp() * 1000)


def window(start: str, end: str | None, config: SoakConfig) -> tuple[int, int]:
    """`--start` / `--end` から期間を出す。`--end` が無ければ `duration_hours` ぶん。"""
    start_ms = parse_time(start, config.zone)
    if end is not None:
        return start_ms, parse_time(end, config.zone)
    return start_ms, start_ms + int(timedelta(hours=config.duration_hours).total_seconds() * 1000)


def main(argv: Sequence[str] | None = None, *, clock: Clock | None = None) -> int:
    """`coldaisle-soak-report`。連続運転のあとに1回呼ぶ。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-soak-report", description="連続運転テスト（soak）の集計"
    )
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--config", type=Path, default=Path("config/soak.yaml"))
    parser.add_argument("--quality-rules", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--start", required=True, help="期間の始まり（ISO 8601）")
    parser.add_argument("--end", help="期間の終わり（ISO 8601）。既定は duration_hours 後")
    parser.add_argument("--no-write", action="store_true", help="ファイルに書かない")
    parser.add_argument(
        "--print", choices=["markdown", "json"], help="全文を標準出力へ（markdown / json）"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logs.configure(args.log_level)
    config = SoakConfig.from_yaml(args.config)
    if not args.db.exists():
        # **開かない。** 開くと空の DB が作られ、「データが無い」と区別できなくなる
        parser.error(f"DB が見つからない: {args.db}")
    start_ms, end_ms = window(args.start, args.end, config)
    clock = clock or WallClock()

    store = SqliteStore(args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=clock)
    try:
        with store.read_snapshot():
            report = build(
                store, start_ms=start_ms, end_ms=end_ms, now_ms=clock.now_ms(), config=config
            )
    finally:
        store.close()

    paths: tuple[Path, Path] | None = None
    if not args.no_write:
        paths = write(report, config.output_dir)
    if args.print == "markdown":
        # 人が読むもの。**`--print` のときだけ**標準出力へ出す（report.py と同じ）
        print(report.as_markdown())  # noqa: T201
    elif args.print == "json":
        print(report.as_json(), end="")  # noqa: T201
    LOGGER.info(
        "soak の集計を作成した",
        extra={
            logs.FIELDS_KEY: {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "verdict": report.verdict.value,
                "paths": None if paths is None else [str(path) for path in paths],
            }
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
