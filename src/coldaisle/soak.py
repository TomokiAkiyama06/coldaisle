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

**この集計に無いもの**（DB に記録経路が無い。#47 の対象外）:

- シリアルの再接続回数（ログにだけ出る）
- デーモンの RSS 推移

そのため**総合の判定（`verdict`）は合格にならない。** 不合格の項目があれば不合格、
なければ判定不能。DB で判定できる項目だけの総合は `db_verdict` に分けて出す。

**再生（replay）の DB では再起動を判定しない。** `ReplaySource` は CSV に無い
`seq` / `up` を連続した値で合成するので、再生からは seq の欠損も再起動も記録されない。
「0 件」は観測の結果ではない。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
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

        受入基準の全部ではない（`NOT_COVERED` を見ない）。soak の合否には `verdict` を使う。
        """
        verdicts = {check.verdict for check in self.checks}
        if Verdict.FAIL in verdicts:
            return Verdict.FAIL
        if Verdict.UNKNOWN in verdicts or not verdicts:
            return Verdict.UNKNOWN
        return Verdict.PASS

    @property
    def verdict(self) -> Verdict:
        """受入基準**全体**の判定。不合格は確定するが、合格は出さない。

        RSS 推移と再接続回数（`NOT_COVERED`）をこの集計は確かめられない。
        DB の項目が全部合格でも、確かめていない基準が残る限り判定不能にする。
        """
        db_verdict = self.db_verdict
        if db_verdict is Verdict.FAIL:
            return Verdict.FAIL
        if NOT_COVERED:
            return Verdict.UNKNOWN
        return db_verdict

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
            f"- 送信周期: {self._interval_text()}",
            f"- 判定（総合）: **{_VERDICT_LABEL[self.verdict]}**"
            + ("" if self.verdict is Verdict.FAIL else "（「この集計に無いもの」が未確認）"),
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

    def _interval_text(self) -> str:
        if self.interval_ms is None or self.interval_hello_ms is None:
            return "不明"
        text = f"{self.interval_ms} ms（{self._local(self.interval_hello_ms)} の起動バナー）"
        if self.interval_after_window:
            text += " **期間より後の値。期間中の周期とは限らない**"
        return text


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
) -> SoakReport:
    """期間 `[start_ms, end_ms)` を集計して判定する。**DB には書かない。**"""
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

    try:
        database = SoakDatabase(args.db)
    except SoakDatabaseError as error:
        parser.error(str(error))
    with database, database.read_snapshot():
        report = build(
            database, start_ms=start_ms, end_ms=end_ms, now_ms=clock.now_ms(), config=config
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
