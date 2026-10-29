"""連続運転テスト（soak）の集計（#47 / issues/15-soak-test.md）。

**既存の DB を読むだけ。** 取り込み・ロールアップ・通知のどれにも触らない。
DB は `SqliteStore` ではなく**読み取り専用の接続**で開く（`SoakDatabase`）。
`SqliteStore` は開くだけで WAL への切り替えとマイグレーションを当てるので、
実機から持ち帰った DB を集計した瞬間にスキーマが上がってしまう。

24時間連続運転のあとに `coldaisle-soak-report` を1回呼び、人が読む Markdown と
機械が読む JSON を残す。

集計元は**生データ**（`readings`）。理由は2つ。

1. `suspect` の件数はロールアップに残らない（決定記録 0002 §2.8 の列に無い）
2. 期間の端が分の途中でもよい。soak の開始時刻は人が決める

欠測率は「届くはずのサンプルのうち、`ok` の値として届かなかった割合」
（決定記録 0002 §2.8 と同じ分子）。**母数は期間の長さと送信周期から出す。**
行の数を母数にすると、装置ごと沈黙していた時間が欠測に数えられない。
判定の単位（最も悪いチャネル）・母数・再起動の数え方は決定記録 0070 に従う。

**DB に無いもの**: 取り込みのデーモンの RSS 推移。導入先で一時的に採取した CSV
（`deploy/soak/rss-sample.sh`）を `--rss-csv` で渡したときだけ判定する（決定記録 0173）。
**CSV も読むだけ。** 渡さなければ RSS は `NOT_COVERED` に残り、**総合の判定（`verdict`）は
合格にならない。** 不合格の項目があれば不合格、なければ判定不能。DB で判定できる項目だけの
総合は `db_verdict` に分けて出す（CSV の項目を入れない）。

シリアルの再接続回数は受入基準に無いので判定に使わず、「参考」の一覧に置く
（journal から数える。決定記録 0173 §2.5）。

**再生（replay）の DB では再起動を判定しない。** `ReplaySource` は CSV に無い
`seq` / `up` を連続した値で合成するので、再生からは seq の欠損も再起動も記録されない。
「0 件」は観測の結果ではない。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import quote
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
from coldaisle.ingest.replay import REPLAY_DEVICE
from coldaisle.store import Quality, migrations

LOGGER = logging.getLogger("coldaisle.soak")

PERIODIC_METRICS: tuple[str, ...] = tuple(CHANNEL_TO_METRIC.values())
"""欠測率を出すメトリクス。**周期的に届くデバイスのチャネルだけ**（決定記録 0008 §2.1.2）。"""

RSS_NOT_COVERED = (
    "デーモンの RSS 推移（DB に記録経路が無い。`--rss-csv` で採取の CSV を渡すと判定する）"
)

NOT_COVERED: tuple[str, ...] = (RSS_NOT_COVERED,)
"""`--rss-csv` を渡さないとき、受入基準のうち判定できない項目。**レポートに必ず書く。**

渡したときは空になる（`SoakReport.not_covered`）。
"""

REFERENCE_ONLY: tuple[str, ...] = (
    "シリアルの再接続回数（受入基準に無い。DB に記録経路が無いので journal から数える: "
    "`journalctl -u coldaisle-daemon --since ... --until ... | grep -c 'シリアルが切れた'`）",
)
"""判定に使わない参考の項目（決定記録 0173 §2.5）。`verdict` に影響しない。"""

RSS_CSV_COLUMNS: tuple[str, ...] = (
    "ts_ms",
    "unit",
    "main_pid",
    "rss_anon_kb",
    "vm_rss_kb",
    "rss_file_kb",
    "cg_memory_current",
    "cg_anon",
    "cg_file",
)
"""`deploy/soak/rss-sample.sh` が書く CSV の列（決定記録 0173 §2.2）。

**順も含めて一致を求める。**
"""


class Thresholds(BaseModel):
    """合否判定の閾値（`config/soak.yaml`）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    missing_ratio_below: float = Field(gt=0, le=1)
    """欠測率がこれ**未満**なら合格（NFR-02）。"""
    max_device_restarts: int = Field(ge=0)
    """デバイス再起動がこの回数**以下**なら合格。"""


class RssConfig(BaseModel):
    """取り込みのデーモンの RSS の判定（`config/soak.yaml` の `rss:`。決定記録 0173 §2.3）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ingest_unit: str = Field(min_length=1)
    """判定する unit（CSV の `unit` の列）。ほかの unit は参考として表に出すだけ。"""
    warmup_hours: float = Field(ge=0)
    """期間の始まりから判定に入れない時間。"""
    max_slope_kib_per_hour: float = Field(ge=0)
    """最小二乗の直線の傾きがこれ**以下**なら合格。"""
    max_range_mib: float = Field(ge=0)
    """最大値と最小値の差がこれ**以下**なら合格。"""
    sample_interval_s: float = Field(gt=0)
    """採取の間隔。期待の行数の母数。"""
    min_coverage: float = Field(gt=0, le=1)
    """行の数が期待の行数のこの割合**未満**なら判定不能。"""
    max_gap_minutes: float = Field(gt=0)
    """行の間隔（区間の端を含む）がこれを**超える**と判定不能。"""
    max_process_restarts: int = Field(ge=0)
    """期間での MainPID の変化（取り込みのプロセスの再起動）がこの回数**以下**なら合格。"""


class SoakConfig(BaseModel):
    """`config/soak.yaml`。**既定値をコードに置かない**（AGENTS.md ルール9）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str
    duration_hours: float = Field(gt=0)
    thresholds: Thresholds
    rss: RssConfig
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
class RssSample:
    """採取の CSV の1行（決定記録 0173 §2.2）。**読めなかった値は `None`**（推測で埋めない）。"""

    ts_ms: int
    unit: str
    main_pid: int | None
    """unit が止まっていれば `None`。"""
    rss_anon_kb: int | None
    vm_rss_kb: int | None
    rss_file_kb: int | None
    cg_memory_current: int | None
    cg_anon: int | None
    cg_file: int | None


@dataclass(frozen=True)
class UnitSummary:
    """unit ごとの期間の最初と最後の値。**参考。判定に使わない**（判定する unit も含めて出す）。"""

    unit: str
    rows: int
    pids: tuple[int, ...]
    first: RssSample | None
    last: RssSample | None


@dataclass(frozen=True)
class RssSummary:
    """取り込みのデーモンの RSS の集計（決定記録 0173 §2.3 / §2.4）。"""

    unit: str
    judge_start_ms: int
    """期間の始まりに暖機を足した時刻。判定の区間は `[judge_start_ms, judge_end_ms)`。"""
    judge_end_ms: int
    rows: int
    """判定の区間で `rss_anon_kb` を持つ行の数。"""
    expected: int
    """判定の区間の長さ ÷ 採取の間隔。"""
    max_gap_ms: int | None
    """判定の区間の行の間隔の最大（区間の端からの間隔を含む）。行が無ければ `None`。"""
    slope_kib_per_hour: float | None
    min_kib: int | None
    max_kib: int | None
    judged_pids: tuple[int, ...]
    """判定の区間に出た MainPID（出た順）。"""
    window_pids: tuple[int, ...]
    """期間（暖機を含む）に出た MainPID（出た順）。"""
    pid_changes: int
    """期間で MainPID が変わった回数。"""
    stopped_rows: int
    """期間で MainPID が空（unit が止まっていた）の行の数。"""
    edge_gap_ms: int | None
    """期間の端から、MainPID を持つ最初・最後の行までの間隔の大きい方。行が無ければ `None`。"""
    units: tuple[UnitSummary, ...]

    @property
    def range_kib(self) -> int | None:
        if self.min_kib is None or self.max_kib is None:
            return None
        return self.max_kib - self.min_kib

    @property
    def coverage(self) -> float:
        return self.rows / self.expected if self.expected else 0.0


@dataclass(frozen=True)
class SoakReport:
    start_ms: int
    end_ms: int
    timezone: str
    interval_ms: int | None
    interval_hello_ms: int | None
    """`interval_ms` を申告した起動バナーの受信時刻。**どの時点の周期か**を示す。"""
    complete: bool
    """期間の終わりが集計時刻より前か。**終わっていない期間は判定しない。**"""
    metrics: tuple[MetricLine, ...]
    dropped_samples: int
    device_restarts: int
    queue_drops: int
    replay_in_window: bool
    """期間に再生（replay）のデータが入りうるか。**seq の欠損と再起動は合成値で数えられない。**"""
    checks: tuple[Check, ...]
    """**DB で判定する項目だけ**（`db_verdict` の対象）。"""
    rss: RssSummary | None = None
    """`--rss-csv` を渡したときだけ。"""
    rss_checks: tuple[Check, ...] = ()
    """採取の CSV で判定する項目（RSS・取り込みのプロセスの再起動）。`db_verdict` に入れない。"""

    @property
    def all_checks(self) -> tuple[Check, ...]:
        return self.checks + self.rss_checks

    @property
    def not_covered(self) -> tuple[str, ...]:
        """受入基準のうち判定できないもの。CSV を渡せば RSS も判定するので空になる。"""
        return () if self.rss is not None else NOT_COVERED

    @property
    def interval_after_window(self) -> bool:
        """周期を申告した起動バナーが期間より後か。**期間中の周期と違うかもしれない。**"""
        return self.interval_hello_ms is not None and self.interval_hello_ms >= self.end_ms

    @property
    def has_data(self) -> bool:
        return any(line.rows > 0 for line in self.metrics)

    @property
    def suspect_total(self) -> int:
        return sum(line.suspect for line in self.metrics)

    @property
    def db_verdict(self) -> Verdict:
        """**DB で判定できる項目だけ**の総合。1つでも不合格なら不合格、判定不能が残れば判定不能。

        受入基準の全部ではない（`NOT_COVERED` と CSV の項目を見ない）。
        soak の合否には `verdict` を使う。
        """
        return _combine(self.checks)

    @property
    def verdict(self) -> Verdict:
        """受入基準**全体**の判定。

        確かめていない基準（`not_covered`）が残る限り合格は出さない。CSV を渡して
        RSS も判定したときだけ、DB と CSV の項目がすべて合格なら合格になる。
        """
        combined = _combine(self.all_checks)
        if combined is Verdict.FAIL:
            return Verdict.FAIL
        if self.not_covered:
            return Verdict.UNKNOWN
        return combined

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
            "interval_hello": (
                None if self.interval_hello_ms is None else self._local(self.interval_hello_ms)
            ),
            "interval_hello_ms": self.interval_hello_ms,
            "interval_after_window": self.interval_after_window,
            "verdict": self.verdict.value,
            "db_verdict": self.db_verdict.value,
            "replay_in_window": self.replay_in_window,
            "checks": [
                {
                    "name": check.name,
                    "source": source,
                    "observed": check.observed,
                    "threshold": check.threshold,
                    "verdict": check.verdict.value,
                    "note": check.note,
                }
                for source, checks in (("db", self.checks), ("rss_csv", self.rss_checks))
                for check in checks
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
            "rss": None if self.rss is None else self._rss_dict(self.rss),
            "not_covered": list(self.not_covered),
            "reference": list(REFERENCE_ONLY),
        }

    def _rss_dict(self, rss: RssSummary) -> dict[str, Any]:
        return {
            "unit": rss.unit,
            "judge_window": {
                "start": self._local(rss.judge_start_ms),
                "end": self._local(rss.judge_end_ms),
                "start_ms": rss.judge_start_ms,
                "end_ms": rss.judge_end_ms,
            },
            "rows": rss.rows,
            "expected": rss.expected,
            "coverage": rss.coverage,
            "max_gap_ms": rss.max_gap_ms,
            "slope_kib_per_hour": rss.slope_kib_per_hour,
            "min_kib": rss.min_kib,
            "max_kib": rss.max_kib,
            "range_kib": rss.range_kib,
            "judged_pids": list(rss.judged_pids),
            "window_pids": list(rss.window_pids),
            "pid_changes": rss.pid_changes,
            "stopped_rows": rss.stopped_rows,
            "edge_gap_ms": rss.edge_gap_ms,
            "units": [
                {
                    "unit": unit.unit,
                    "rows": unit.rows,
                    "pids": list(unit.pids),
                    "first": None if unit.first is None else _sample_dict(unit.first),
                    "last": None if unit.last is None else _sample_dict(unit.last),
                }
                for unit in rss.units
            ],
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n"

    def as_markdown(self) -> str:
        """人が読む全文。**判定を先に、根拠の表をあとに置く。**"""
        lines = [
            "# 連続運転テスト（soak）集計",
            "",
            f"- 期間: {self._local(self.start_ms)} 〜 {self._local(self.end_ms)}",
            f"- 送信周期: {self._interval_text()}",
            f"- 判定（総合）: **{_VERDICT_LABEL[self.verdict]}**"
            + (
                "（「この集計に無いもの」が未確認）"
                if self.verdict is not Verdict.FAIL and self.not_covered
                else ""
            ),
            f"- 判定（DB で判定できる項目のみ）: **{_VERDICT_LABEL[self.db_verdict]}**",
            "",
        ]
        if not self.complete:
            lines += ["**期間がまだ終わっていないため判定しない。**", ""]
        if not self.has_data:
            lines += ["**この期間のデータがありません。**", ""]
        if self.replay_in_window:
            lines += [
                "**期間に再生（replay）のデータを含む。** 再生の seq / up は合成値のため、"
                "seq の欠損と再起動は記録されない（0 件は観測の結果ではない）。",
                "",
            ]
        lines += ["## 判定", "", "| 項目 | 観測値 | 基準 | 判定 | 備考 |", "|---|---|---|---|---|"]
        lines += [
            f"| {check.name} | {check.display} | {check.threshold} "
            f"| {_VERDICT_LABEL[check.verdict]} | {check.note} |"
            for check in self.all_checks
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
        if self.rss is not None:
            lines += self._rss_markdown(self.rss)
        lines += ["", "## この集計に無いもの", ""]
        lines += [f"- {item}" for item in self.not_covered] or ["- なし"]
        lines += ["", "## 参考（判定に使わない）", ""]
        lines += [f"- {item}" for item in REFERENCE_ONLY]
        return "\n".join(lines).rstrip() + "\n"

    def _rss_markdown(self, rss: RssSummary) -> list[str]:
        lines = [
            "",
            f"## 取り込みのデーモンの RSS（`{rss.unit}` の RssAnon）",
            "",
            f"- 判定の区間: {self._local(rss.judge_start_ms)} 〜 {self._local(rss.judge_end_ms)}"
            "（期間の始まりから暖機を除く）",
            f"- 行数: {rss.rows} / 期待 {rss.expected}（{rss.coverage * 100:.1f}%）",
            f"- 行の間隔の最大（区間の端を含む）: {_minutes(rss.max_gap_ms)}",
            f"- 傾き（最小二乗）: {_slope(rss.slope_kib_per_hour)}",
            f"- 最小 / 最大: {_mib(rss.min_kib)} / {_mib(rss.max_kib)}（差 {_mib(rss.range_kib)}）",
            f"- MainPID（判定の区間）: {_pids(rss.judged_pids)}",
            f"- MainPID（期間。暖機を含む）: {_pids(rss.window_pids)}"
            f"（変化 {rss.pid_changes} 回・止まっていた行 {rss.stopped_rows}）",
            "",
            "### unit ごとの最初と最後の値（MiB。参考。判定に使わない）",
            "",
            "| unit | 行 | MainPID | RssAnon | VmRSS | RssFile "
            "| memory.current | cgroup anon | cgroup file |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for unit in rss.units:
            first, last = unit.first, unit.last
            lines.append(
                f"| {unit.unit} | {unit.rows} | {_pids(unit.pids)} "
                f"| {_span(first, last, 'rss_anon_kb', 1024)} "
                f"| {_span(first, last, 'vm_rss_kb', 1024)} "
                f"| {_span(first, last, 'rss_file_kb', 1024)} "
                f"| {_span(first, last, 'cg_memory_current', 1024 * 1024)} "
                f"| {_span(first, last, 'cg_anon', 1024 * 1024)} "
                f"| {_span(first, last, 'cg_file', 1024 * 1024)} |"
            )
        return lines

    def _interval_text(self) -> str:
        if self.interval_ms is None or self.interval_hello_ms is None:
            return "不明"
        text = f"{self.interval_ms} ms（{self._local(self.interval_hello_ms)} の起動バナー）"
        if self.interval_after_window:
            text += " **期間より後の値。期間中の周期とは限らない**"
        return text


def _combine(checks: Sequence[Check]) -> Verdict:
    """1つでも不合格なら不合格、判定不能が残れば（項目が無ければ）判定不能。"""
    verdicts = {check.verdict for check in checks}
    if Verdict.FAIL in verdicts:
        return Verdict.FAIL
    if Verdict.UNKNOWN in verdicts or not verdicts:
        return Verdict.UNKNOWN
    return Verdict.PASS


def _sample_dict(sample: RssSample) -> dict[str, Any]:
    return {column: getattr(sample, column) for column in RSS_CSV_COLUMNS}


def _mib(kib: int | None) -> str:
    return "—" if kib is None else f"{kib / 1024:.1f} MiB"


def _slope(kib_per_hour: float | None) -> str:
    return "—" if kib_per_hour is None else f"{kib_per_hour:+.1f} KiB/h"


def _minutes(ms: int | None) -> str:
    return "—" if ms is None else f"{ms / 60_000:.1f} 分"


def _pids(pids: Sequence[int]) -> str:
    return "—" if not pids else ", ".join(str(pid) for pid in pids)


def _span(first: RssSample | None, last: RssSample | None, column: str, per_mib: int) -> str:
    """最初と最後の値を MiB で。

    `per_mib` は列の単位から MiB への割り数（kB は 1024、bytes は 1024²）。
    """

    def one(sample: RssSample | None) -> str:
        value = None if sample is None else getattr(sample, column)
        return "—" if value is None else f"{value / per_mib:.1f}"

    return f"{one(first)} → {one(last)}"


def _count(value: int | None) -> str:
    return "—" if value is None else str(value)


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.3f}%"


# ---------------------------------------------------------------------- 集計


class SoakDatabaseError(RuntimeError):
    """集計する DB を開けない（SQLite ではない・スキーマの版が合わない）。"""


class SoakDatabase:
    """soak の集計が読む DB。**開いても行もスキーマも変えない。**

    `mode=ro` の URI で開く。soak はデーモンが書いている最中にも走らせるので、
    静止した DB を前提にする `immutable=1`（evaluate.py の `EvidenceDatabase`）は
    使えない。WAL の DB では SQLite が `-shm` / `-wal` の添え file を作ることがあるが、
    DB の中身（行・スキーマ・journal_mode）は変わらない。

    **マイグレーションは当てない。** 版が合わなければ開かずに落とす。
    """

    __slots__ = ("_conn",)

    def __init__(self, path: Path) -> None:
        uri = f"file:{quote(str(path.resolve()))}?mode=ro"
        try:
            self._conn = sqlite3.connect(uri, uri=True, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            version = migrations.current_version(self._conn)
        except sqlite3.Error as error:
            raise SoakDatabaseError(f"DB を読み取り専用で開けない: {path}") from error
        known = len(migrations.discover())
        if version != known:
            self.close()
            raise SoakDatabaseError(
                f"DB のスキーマの版が合わない（DB={version}; コード={known}）: {path}。"
                "**集計は DB を書き換えないので移行もしない。** 取り込みデーモンで開いて"
                "移行するか、同じ版のコードで集計する"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SoakDatabase:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """ブロック内の読み出しを1つのスナップショットにそろえる（`SqliteStore` と同じ）。"""
        self._conn.execute("BEGIN DEFERRED")
        try:
            yield
        finally:
            self._conn.execute("COMMIT")

    def latest_interval(self) -> tuple[int, int] | None:
        """直近の起動バナーが申告した `(interval_ms, last_hello_ms)`（決定記録 0002 §2.8）。

        `devices` は起動バナーのたびに上書きされ、過去の周期は残らない。
        そのため**いつの値か**を一緒に返し、期間より後なら呼び出し側が判定しない。
        """
        row = self._conn.execute(
            "SELECT interval_ms, last_hello_ms FROM devices "
            "WHERE interval_ms IS NOT NULL AND last_hello_ms IS NOT NULL "
            "ORDER BY last_hello_ms DESC LIMIT 1"
        ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]))

    def replay_before(self, end_ms: int) -> bool:
        """`end_ms` より前に再生（replay）の取り込みが始まっていたか。

        `readings` には取り込み元の列が無いので、起動バナー（`devices`）と
        dataset の来歴（`dataset_source_run`）から判断する。再生の起動バナーは
        CSV の最初の行の時刻で記録され、行はそれ以降に並ぶ（`ReplaySource`）。
        `first_seen_ms` が期間の終わりより前なら、期間に再生の行が入りうる。
        **期間より前に終わった再生も含むが、甘い側には倒れない**（判定不能になるだけ）。
        """
        device = self._conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ? AND first_seen_ms < ? LIMIT 1",
            (REPLAY_DEVICE, end_ms),
        ).fetchone()
        dataset = self._conn.execute(
            "SELECT 1 FROM dataset_source_run WHERE source_kind = 'replay' LIMIT 1"
        ).fetchone()
        return device is not None or dataset is not None

    def quality_counts(self, metric: str, start_ms: int, end_ms: int) -> dict[Quality, int]:
        """窓 `[start_ms, end_ms)` の生データを品質ごとに数える。

        `ok` は**値を持つ行だけ**を数える（`Stats.ok_value_count` と同じ母数。
        決定記録 0002 §2.8）。値の無い `ok` 行はどの品質にも数えない。
        ロールアップは `suspect` の件数を持たないため、生データから数える。
        """
        rows = self._conn.execute(
            "SELECT quality, COUNT(*) AS n FROM readings "
            "WHERE metric = ? AND ts_ms >= ? AND ts_ms < ? "
            "  AND (quality != 'ok' OR value IS NOT NULL) "
            "GROUP BY quality",
            (metric, start_ms, end_ms),
        ).fetchall()
        counts = dict.fromkeys(Quality, 0)
        for row in rows:
            counts[Quality(row["quality"])] = int(row["n"])
        return counts

    def event_total(self, metric: str, start_ms: int, end_ms: int) -> int:
        """事象メトリクスの合計。**起きたときしか書かれない**ので、行が無ければ 0 件。"""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(value), 0) FROM readings "
            "WHERE metric = ? AND ts_ms >= ? AND ts_ms < ?",
            (metric, start_ms, end_ms),
        ).fetchone()
        return round(float(row[0]))


def build(
    store: SoakDatabase,
    *,
    start_ms: int,
    end_ms: int,
    now_ms: int,
    config: SoakConfig,
    rss_samples: Sequence[RssSample] | None = None,
) -> SoakReport:
    """期間 `[start_ms, end_ms)` を集計して判定する。**DB には書かない。**

    `rss_samples`（`--rss-csv` の行）を渡したときだけ、RSS と取り込みのプロセスの再起動も
    判定する（決定記録 0173）。渡さなければ今までと同じ結果になる。
    """
    if start_ms >= end_ms:
        raise ValueError(f"期間が空か逆転している: start_ms={start_ms} end_ms={end_ms}")
    interval = store.latest_interval()
    interval_ms = None if interval is None else interval[0]
    expected = None if interval_ms is None else (end_ms - start_ms) // interval_ms
    metrics = tuple(
        _metric_line(store, metric, start_ms, end_ms, expected) for metric in PERIODIC_METRICS
    )
    counters = {
        metric: store.event_total(metric, start_ms, end_ms)
        for metric in (DROPPED_SAMPLES_METRIC, DEVICE_RESTART_METRIC, QUEUE_DROPS_METRIC)
    }
    complete = end_ms <= now_ms
    has_data = any(line.rows > 0 for line in metrics)
    replay_in_window = store.replay_before(end_ms)
    interval_after_window = interval is not None and interval[1] >= end_ms
    rss = (
        None
        if rss_samples is None
        else summarize_rss(rss_samples, start_ms=start_ms, end_ms=end_ms, config=config.rss)
    )
    rss_checks = (
        ()
        if rss is None
        else (
            _rss_check(rss, config.rss, complete=complete),
            _process_restart_check(rss, config.rss, complete=complete),
        )
    )
    return SoakReport(
        start_ms=start_ms,
        end_ms=end_ms,
        timezone=config.timezone,
        interval_ms=interval_ms,
        interval_hello_ms=None if interval is None else interval[1],
        complete=complete,
        metrics=metrics,
        dropped_samples=counters[DROPPED_SAMPLES_METRIC],
        device_restarts=counters[DEVICE_RESTART_METRIC],
        queue_drops=counters[QUEUE_DROPS_METRIC],
        replay_in_window=replay_in_window,
        checks=(
            _missing_check(
                metrics,
                config.thresholds,
                complete=complete,
                has_data=has_data,
                interval_after_window=interval_after_window,
            ),
            _restart_check(
                counters[DEVICE_RESTART_METRIC],
                config.thresholds,
                complete=complete,
                has_data=has_data,
                replay_in_window=replay_in_window,
            ),
        ),
        rss=rss,
        rss_checks=rss_checks,
    )


def _metric_line(
    store: SoakDatabase, metric: str, start_ms: int, end_ms: int, expected: int | None
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


def _missing_check(
    metrics: Sequence[MetricLine],
    thresholds: Thresholds,
    *,
    complete: bool,
    has_data: bool,
    interval_after_window: bool,
) -> Check:
    """**最も悪いチャネル**で判定する（決定記録 0070 §2.1）。

    平均すると、1本だけ死んだプローブが薄まる。
    """
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
    if interval_after_window:
        # 周期は起動バナーのたびに上書きされる。期間より後のバナーの値は、
        # 期間中の周期とは限らない（決定記録 0070 §2.2）
        note = "送信周期が期間より後の起動バナーの値（期間中の周期とは限らない）"
        return Check(name, worst, display, limit, Verdict.UNKNOWN, note)
    verdict = Verdict.PASS if worst < thresholds.missing_ratio_below else Verdict.FAIL
    return Check(name, worst, display, limit, verdict, metric)


def _restart_check(
    restarts: int,
    thresholds: Thresholds,
    *,
    complete: bool,
    has_data: bool,
    replay_in_window: bool,
) -> Check:
    """決定記録 0070 §2.3。**再生のデータを含む期間では閾値以下を合格と言わない。**

    再生は `seq` / `up` を合成するので再起動を記録しない。記録された再起動は
    再生以外（実機）から来たものなので、閾値超過はそのまま不合格にする。
    """
    name = "デバイス再起動"
    limit = f"≤ {thresholds.max_device_restarts} 回"
    note = "意図した再起動かどうかは DB から区別できない"
    display = f"{restarts} 回"
    if not complete:
        return Check(name, float(restarts), display, limit, Verdict.UNKNOWN, _why_unknown(complete))
    # 超過は周期データが無くても確定している（再起動したまま送信が戻らなかった場合など）。
    # 周期データで期間のカバーを確かめる必要があるのは「閾値以下」と言うときだけ
    if restarts > thresholds.max_device_restarts:
        return Check(name, float(restarts), display, limit, Verdict.FAIL, note)
    if not has_data:
        return Check(name, float(restarts), display, limit, Verdict.UNKNOWN, _why_unknown(complete))
    if replay_in_window:
        replay_note = (
            "期間に再生（replay）のデータを含む。再生の seq / up は合成値で再起動を記録しない"
        )
        return Check(name, float(restarts), display, limit, Verdict.UNKNOWN, replay_note)
    return Check(name, float(restarts), display, limit, Verdict.PASS, note)


def _why_unknown(complete: bool) -> str:
    return "期間がまだ終わっていない" if not complete else "期間にデータが無い"


# ---------------------------------------------------------------------- RSS（決定記録 0173）


class RssCsvError(ValueError):
    """採取の CSV の形が違う。**読める行だけで判定しない**（黙って欠けた判定を出さない）。"""


def read_rss_csv(path: Path) -> tuple[RssSample, ...]:
    """`deploy/soak/rss-sample.sh` の CSV を読む。**読むだけ。**

    見出しは `RSS_CSV_COLUMNS` と順も含めて一致すること。空欄は `None`（採取で読めなかった値）。
    形の違う行が1つでもあれば `RssCsvError` で落とす。
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None or tuple(header) != RSS_CSV_COLUMNS:
            raise RssCsvError(
                f"RSS の CSV の見出しが違う: {path}（期待: {','.join(RSS_CSV_COLUMNS)}）"
            )
        samples: list[RssSample] = []
        for row in reader:
            if not row:
                continue
            try:
                samples.append(_parse_rss_row(row))
            except ValueError as error:
                raise RssCsvError(
                    f"RSS の CSV の {reader.line_num} 行目が読めない: {path}: {error}"
                ) from error
    return tuple(samples)


def _parse_rss_row(row: Sequence[str]) -> RssSample:
    if len(row) != len(RSS_CSV_COLUMNS):
        raise ValueError(f"列の数が {len(row)}（期待 {len(RSS_CSV_COLUMNS)}）")
    ts_ms = int(row[0])
    unit = row[1]
    if not unit:
        raise ValueError("unit が空")
    values = [_optional_count(cell) for cell in row[2:]]
    return RssSample(ts_ms, unit, *values)


def _optional_count(cell: str) -> int | None:
    """空欄は `None`。負の値は採取の値としてありえないので落とす。"""
    if cell == "":
        return None
    value = int(cell)
    if value < 0:
        raise ValueError(f"負の値: {cell}")
    return value


def summarize_rss(
    samples: Sequence[RssSample], *, start_ms: int, end_ms: int, config: RssConfig
) -> RssSummary:
    """期間 `[start_ms, end_ms)` の行を集計する。

    判定はしない（`_rss_check` / `_process_restart_check`）。

    判定の区間は期間の始まりから `warmup_hours` を除いた残り。MainPID の変化は
    暖機を含む期間全体で数える（決定記録 0173 §2.3 / §2.4）。
    """
    in_window = sorted(
        (sample for sample in samples if start_ms <= sample.ts_ms < end_ms),
        key=lambda sample: sample.ts_ms,
    )
    ingest = [sample for sample in in_window if sample.unit == config.ingest_unit]
    judge_start_ms = min(end_ms, start_ms + round(config.warmup_hours * 3_600_000))
    in_judge = [sample for sample in ingest if sample.ts_ms >= judge_start_ms]
    judged = [sample for sample in in_judge if sample.rss_anon_kb is not None]
    points = [(sample.ts_ms, sample.rss_anon_kb) for sample in judged]
    values = [value for _, value in points if value is not None]
    expected = int((end_ms - judge_start_ms) // round(config.sample_interval_s * 1000))

    running = [sample for sample in ingest if sample.main_pid is not None]
    pid_changes = sum(1 for before, after in pairwise(running) if before.main_pid != after.main_pid)
    edge_gap_ms = (
        None if not running else max(running[0].ts_ms - start_ms, end_ms - running[-1].ts_ms)
    )
    return RssSummary(
        unit=config.ingest_unit,
        judge_start_ms=judge_start_ms,
        judge_end_ms=end_ms,
        rows=len(judged),
        expected=expected,
        max_gap_ms=_max_gap([ts for ts, _ in points], judge_start_ms, end_ms),
        slope_kib_per_hour=_slope_per_hour(points),
        min_kib=min(values) if values else None,
        max_kib=max(values) if values else None,
        # RSS が読めなかった行の MainPID も数える。落とすと、入れ替わる前のプロセスが
        # 見えず「判定不能」を合格にしてしまう（決定記録 0173 §2.3）
        judged_pids=_distinct_pids(in_judge),
        window_pids=_distinct_pids(running),
        pid_changes=pid_changes,
        stopped_rows=sum(1 for sample in ingest if sample.main_pid is None),
        edge_gap_ms=edge_gap_ms,
        units=_unit_summaries(in_window),
    )


def _distinct_pids(samples: Iterable[RssSample]) -> tuple[int, ...]:
    seen: dict[int, None] = {}
    for sample in samples:
        if sample.main_pid is not None:
            seen.setdefault(sample.main_pid, None)
    return tuple(seen)


def _max_gap(timestamps: Sequence[int], start_ms: int, end_ms: int) -> int | None:
    """区間の端から最初・最後の行までを含めた、行の間隔の最大。

    **CSV が区間を覆っているか**の確かめも兼ねる。
    """
    if not timestamps:
        return None
    edges = [start_ms, *timestamps, end_ms]
    return max(after - before for before, after in pairwise(edges))


def _slope_per_hour(points: Sequence[tuple[int, int | None]]) -> float | None:
    """最小二乗の直線の傾き（KiB/h）。

    **全点を使う**（端の揺れに引っ張られにくい。0173 §4 の G）。
    """
    pairs = [(ts / 3_600_000, float(value)) for ts, value in points if value is not None]
    if len(pairs) < 2:
        return None
    mean_x = sum(x for x, _ in pairs) / len(pairs)
    mean_y = sum(y for _, y in pairs) / len(pairs)
    sxx = sum((x - mean_x) ** 2 for x, _ in pairs)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    return sxy / sxx


def _unit_summaries(samples: Sequence[RssSample]) -> tuple[UnitSummary, ...]:
    by_unit: dict[str, list[RssSample]] = {}
    for sample in samples:
        by_unit.setdefault(sample.unit, []).append(sample)
    return tuple(
        UnitSummary(
            unit=unit,
            rows=len(rows),
            pids=_distinct_pids(rows),
            first=rows[0] if rows else None,
            last=rows[-1] if rows else None,
        )
        for unit, rows in sorted(by_unit.items())
    )


def _rss_check(rss: RssSummary, config: RssConfig, *, complete: bool) -> Check:
    """決定記録 0173 §2.3。**傾きと範囲の両方**を満たして合格。区間が足りなければ判定不能。"""
    name = "取り込みのデーモンの RSS（RssAnon）"
    limit = (
        f"暖機 {config.warmup_hours:g} 時間を除き、傾き ≤ {config.max_slope_kib_per_hour:g} KiB/h"
        f" かつ 最大−最小 ≤ {config.max_range_mib:g} MiB"
    )
    slope = rss.slope_kib_per_hour
    display = f"傾き {_slope(slope)}・最大−最小 {_mib(rss.range_kib)}"

    def unknown(note: str) -> Check:
        return Check(name, slope, display, limit, Verdict.UNKNOWN, note)

    if not complete:
        return unknown(_why_unknown(complete))
    if len(rss.judged_pids) >= 2:
        return unknown(
            f"判定の区間で MainPID が入れ替わった（{_pids(rss.judged_pids)}）。"
            "別のプロセスの値を1本の傾きにしない"
        )
    if rss.rows < 2 or slope is None or rss.range_kib is None or rss.max_gap_ms is None:
        return unknown("判定の区間に行が足りない（CSV が期間を覆っていない）")
    if rss.coverage < config.min_coverage:
        return unknown(
            f"行が期待の {rss.coverage * 100:.1f}%（{config.min_coverage * 100:g}% 未満）"
        )
    if rss.max_gap_ms > config.max_gap_minutes * 60_000:
        return unknown(
            f"行の間隔（区間の端を含む）が {_minutes(rss.max_gap_ms)}"
            f"（{config.max_gap_minutes:g} 分を超える）"
        )
    passed = slope <= config.max_slope_kib_per_hour and rss.range_kib <= config.max_range_mib * 1024
    note = (
        ""
        if passed
        else "リークの断定ではない。48 時間の測り直しか tracemalloc で調べる（0173 §2.3）"
    )
    return Check(name, slope, display, limit, Verdict.PASS if passed else Verdict.FAIL, note)


def _process_restart_check(rss: RssSummary, config: RssConfig, *, complete: bool) -> Check:
    """決定記録 0173 §2.4。期間（暖機を含む）の MainPID の変化を取り込みのプロセスの再起動に数える。

    超過は CSV が期間を覆っていなくても確定している。「閾値以下」と言うのは、CSV が期間の
    両端を覆い、止まっていた行が無いときだけ（止まったまま戻らなければ MainPID は変わらない）。
    """
    name = "取り込みのプロセスの再起動"
    limit = f"≤ {config.max_process_restarts} 回"
    display = f"{rss.pid_changes} 回"
    observed = float(rss.pid_changes)
    note = f"`{rss.unit}` の MainPID の変化（採取の CSV）"
    if not complete:
        return Check(name, observed, display, limit, Verdict.UNKNOWN, _why_unknown(complete))
    if rss.pid_changes > config.max_process_restarts:
        return Check(name, observed, display, limit, Verdict.FAIL, note)
    if rss.edge_gap_ms is None or rss.edge_gap_ms > config.max_gap_minutes * 60_000:
        return Check(
            name, observed, display, limit, Verdict.UNKNOWN, "CSV が期間の両端を覆っていない"
        )
    if rss.stopped_rows:
        return Check(
            name,
            observed,
            display,
            limit,
            Verdict.UNKNOWN,
            f"MainPID が空の行が {rss.stopped_rows} 行ある（unit が止まっていた）",
        )
    return Check(name, observed, display, limit, Verdict.PASS, note)


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
    parser.add_argument("--start", required=True, help="期間の始まり（ISO 8601）")
    parser.add_argument("--end", help="期間の終わり（ISO 8601）。既定は duration_hours 後")
    parser.add_argument(
        "--rss-csv",
        type=Path,
        help="取り込みの RSS の採取の CSV（deploy/soak/rss-sample.sh。決定記録 0173）。"
        "渡したときだけ RSS と取り込みのプロセスの再起動を判定する",
    )
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
    rss_samples: tuple[RssSample, ...] | None = None
    if args.rss_csv is not None:
        try:
            rss_samples = read_rss_csv(args.rss_csv)
        except (OSError, RssCsvError) as error:
            parser.error(str(error))

    try:
        database = SoakDatabase(args.db)
    except SoakDatabaseError as error:
        parser.error(str(error))
    with database, database.read_snapshot():
        report = build(
            database,
            start_ms=start_ms,
            end_ms=end_ms,
            now_ms=clock.now_ms(),
            config=config,
            rss_samples=rss_samples,
        )

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
                "db_verdict": report.db_verdict.value,
                "paths": None if paths is None else [str(path) for path in paths],
            }
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
