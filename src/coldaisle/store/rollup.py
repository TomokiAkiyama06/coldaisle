"""ロールアップと保持期間（L1）。#10

決定記録 0002 §2.8 の3段（生 / 1分 / 1時間）を維持する仕事。
**集計は `quality='ok'` の行だけで行う**（0002 §2.8）。`suspect` を平均や
最大値に混ぜると、センサーの人工物がそのまま統計に乗り、
生データを消したあとは取り除けなくなる。

実行の順序が重要。**ロールアップしてから削除する。** 逆にすると、
まだ集計していない生データを消して復元できなくなる。`apply_retention()` は
自分でもその安全弁を持つ（1分ロールアップ済みの範囲しか消さない）。
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle import logs
from coldaisle.clock import WallClock
from coldaisle.store.csv_export import export_day
from coldaisle.store.db import HOUR_MS, MINUTE_MS, SqliteStore, combine_minutes_sql
from coldaisle.store.quality import QualityRules

LOGGER = logging.getLogger("coldaisle.store.rollup")

DAY_MS = 24 * 60 * 60 * 1_000


class RetentionRules(BaseModel):
    """保持とエクスポートの設定。既定値を持たない（AGENTS.md ルール6）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_days: int = Field(gt=0)
    """生データの保持日数。1分・1時間は無期限（FR-204）。"""

    csv_dir: str
    """日次CSV（FR-205）の出力先。`~` を含んでよい。"""

    @classmethod
    def from_yaml(cls, path: Path) -> RetentionRules:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"保持期間の設定が辞書ではない: {path}")
        return cls.model_validate(loaded)

    @property
    def raw_retention_ms(self) -> int:
        return self.raw_days * DAY_MS


@dataclass(frozen=True)
class Result:
    """1回の実行で動いた行数。ログと試験のために返す。"""

    minute_buckets: int = 0
    hour_buckets: int = 0
    deleted_rows: int = 0
    cutoff_ms: int | None = None
    """実際に削除の基準にした時刻。安全弁で手前に引き戻された場合はその値。"""


def rollup_minutes(store: SqliteStore) -> int:
    """生データを1分バケットへ集計する（FR-202）。書いたバケット数を返す。

    **未集計の範囲だけを見る。** 毎回すべてを数え直すと、保持期間ぶんの行を
    走査することになる。ただし前回の最終バケットは**必ず数え直す**。
    そのバケットは実行時点で途中だった可能性があり、
    その後に届いた行を取り込めていないため。
    """
    conn = store.connection
    newest = conn.execute("SELECT MAX(ts_ms) FROM readings").fetchone()[0]
    if newest is None:
        return 0
    start = conn.execute("SELECT MAX(bucket_ms) FROM readings_1m").fetchone()[0] or 0
    expected = _expected_per_minute(conn)
    with store.transaction():
        cursor = conn.execute(
            "INSERT OR REPLACE INTO readings_1m "
            "(metric, bucket_ms, min_value, max_value, mean_value, "
            " ok_value_count, row_count, expected_count) "
            f"SELECT metric, (ts_ms / {MINUTE_MS}) * {MINUTE_MS} AS bucket_ms, "
            "  MIN(CASE WHEN quality = 'ok' THEN value END), "
            "  MAX(CASE WHEN quality = 'ok' THEN value END), "
            "  AVG(CASE WHEN quality = 'ok' THEN value END), "
            "  SUM(CASE WHEN quality = 'ok' AND value IS NOT NULL THEN 1 ELSE 0 END), "
            "  COUNT(*), ? "
            "FROM readings WHERE ts_ms >= ? AND ts_ms <= ? "
            "GROUP BY metric, bucket_ms",
            (expected, start, newest),
        )
        return int(cursor.rowcount)


def rollup_hours(store: SqliteStore) -> int:
    """1分バケットを1時間へ再集計する（決定記録 0002 §2.8）。

    **生データからやり直さない。** 1分の値を `ok_value_count` で加重して合成する。
    生から数え直すと、保持期間を過ぎて消えた区間の1時間バケットが作れなくなる。
    """
    conn = store.connection
    newest = conn.execute("SELECT MAX(bucket_ms) FROM readings_1m").fetchone()[0]
    if newest is None:
        return 0
    start = conn.execute("SELECT MAX(bucket_ms) FROM readings_1h").fetchone()[0] or 0
    with store.transaction():
        cursor = conn.execute(
            "INSERT OR REPLACE INTO readings_1h "
            "(metric, bucket_ms, min_value, max_value, mean_value, "
            " ok_value_count, row_count, expected_count) "
            + combine_minutes_sql(
                HOUR_MS, "WHERE bucket_ms >= ? AND bucket_ms <= ?", with_metric=True
            ),
            (start, newest),
        )
        return int(cursor.rowcount)


def apply_retention(store: SqliteStore, rules: RetentionRules, *, now_ms: int) -> tuple[int, int]:
    """保持期間を過ぎた生データを削除する（FR-203）。削除行数と基準時刻を返す。

    **1分ロールアップ済みの範囲しか消さない。** ジョブの順序を間違えても
    集計前の生データが消えないようにする安全弁。ロールアップが一度も
    走っていない DB では、何も削除しない。

    メトリクスごとにループするのは主キー `(metric, ts_ms)` を使うため
    （決定記録 0002 §2.4）。1本の `DELETE ... WHERE ts_ms < ?` は全走査になる。
    """
    conn = store.connection
    cutoff = now_ms - rules.raw_retention_ms
    rolled = conn.execute("SELECT MAX(bucket_ms) FROM readings_1m").fetchone()[0]
    if rolled is None:
        return 0, cutoff
    # **最終バケットの行は消さない。** そのバケットはまだ埋まりきっていない
    # 可能性があり、集計し直す前に生データを消すと二度と正しくならない
    cutoff = min(cutoff, int(rolled))
    deleted = 0
    with store.transaction():
        for metric in store.metrics():
            cursor = conn.execute(
                "DELETE FROM readings WHERE metric = ? AND ts_ms < ?", (metric, cutoff)
            )
            deleted += int(cursor.rowcount)
    return deleted, cutoff


def vacuum(store: SqliteStore) -> None:
    """ファイルを縮める。**定期実行しない。**

    削除で空いたページは以降の挿入が再利用するため、定常状態ではファイルは
    増え続けない。`VACUUM` が要るのは「縮めたい」ときだけで、実行中は
    書き込みを止め、一時的に元と同じだけの空き容量を要求する。
    取り込みを止められるときに手で実行する（CLI の `--vacuum`）。
    """
    store.connection.execute("VACUUM")


def run(store: SqliteStore, rules: RetentionRules, *, now_ms: int) -> Result:
    """ロールアップ → 削除の順で実行する。"""
    minutes = rollup_minutes(store)
    hours = rollup_hours(store)
    deleted, cutoff = apply_retention(store, rules, now_ms=now_ms)
    return Result(
        minute_buckets=minutes, hour_buckets=hours, deleted_rows=deleted, cutoff_ms=cutoff
    )


def _expected_per_minute(conn: sqlite3.Connection) -> int | None:
    """1分あたりの期待サンプル数（決定記録 0002 §2.8）。

    起動バナーの `interval_ms` から出す。受け取れていなければ `NULL` とし、
    そのバケットの欠測率は行数を母数にした下限値として扱う。
    """
    row = conn.execute(
        "SELECT interval_ms FROM devices WHERE interval_ms IS NOT NULL "
        "ORDER BY last_hello_ms DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return MINUTE_MS // int(row[0])


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-rollup`。cron / systemd タイマーから1日1回呼ぶ想定。

    取り込みデーモンの中では動かさない。`VACUUM` が書き込みを止めるうえ、
    集計中に取り込みが遅れる理由を増やしたくない（取り込みは止めない、が優先）。
    """
    parser = argparse.ArgumentParser(prog="coldaisle-rollup", description="ロールアップと削除")
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--retention", type=Path, default=Path("config/retention.yaml"))
    parser.add_argument("--quality-rules", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--vacuum", action="store_true", help="ファイルを縮める。書き込みを止める")
    parser.add_argument("--export-day", type=date.fromisoformat, help="YYYY-MM-DD の日次CSV")
    parser.add_argument("--timezone", default="Asia/Tokyo", help="日境界とCSVの時刻に使う")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logs.configure(args.log_level)
    rules = RetentionRules.from_yaml(args.retention)
    clock = WallClock()
    store = SqliteStore(args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=clock)
    try:
        result = run(store, rules, now_ms=clock.now_ms())
        LOGGER.info(
            "ロールアップと削除を実行した",
            extra={
                logs.FIELDS_KEY: {
                    "minute_buckets": result.minute_buckets,
                    "hour_buckets": result.hour_buckets,
                    "deleted_rows": result.deleted_rows,
                    "cutoff_ms": result.cutoff_ms,
                    "raw_days": rules.raw_days,
                }
            },
        )
        if args.export_day is not None:
            path = export_day(
                store,
                args.export_day,
                tz=ZoneInfo(args.timezone),
                out_dir=Path(rules.csv_dir).expanduser(),
            )
            LOGGER.info("日次CSVを書き出した", extra={logs.FIELDS_KEY: {"path": str(path)}})
        if args.vacuum:
            vacuum(store)
            LOGGER.info("VACUUM を実行した")
    finally:
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - `python -m coldaisle.store.rollup`
    raise SystemExit(main())
