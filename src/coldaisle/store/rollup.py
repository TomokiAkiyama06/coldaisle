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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle import logs
from coldaisle.channels import METRIC_TO_CHANNEL
from coldaisle.clock import Clock, WallClock
from coldaisle.store.csv_export import export_day
from coldaisle.store.db import HOUR_MS, MINUTE_MS, SqliteStore, combine_minutes_sql
from coldaisle.store.quality import QualityRules

LOGGER = logging.getLogger("coldaisle.store.rollup")

DAY_MS = 24 * 60 * 60 * 1_000


class RetentionRules(BaseModel):
    """保持とエクスポートの設定。既定値を持たない（AGENTS.md ルール9）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_days: int = Field(gt=0)
    """生データの保持日数。1分・1時間は無期限（FR-204）。"""

    control_trace_days: int = Field(gt=0)
    """Control decision trace の保持日数。保存容量を無制限に増やさない。"""

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

    @property
    def control_trace_retention_ms(self) -> int:
        return self.control_trace_days * DAY_MS


@dataclass(frozen=True)
class Result:
    """1回の実行で動いた行数。ログと試験のために返す。"""

    minute_buckets: int = 0
    hour_buckets: int = 0
    deleted_rows: int = 0
    cutoff_ms: int | None = None
    """実際に削除の基準にした時刻。安全弁で手前に引き戻された場合はその値。"""
    deleted_control_traces: int = 0
    control_trace_cutoff_ms: int | None = None


def rollup_minutes(
    store: SqliteStore,
    *,
    periodic_intervals_ms: Mapping[str, int] | None = None,
    now_ms: int | None = None,
) -> int:
    """生データを1分バケットへ集計する（FR-202）。書いたバケット数を返す。

    ``periodic_intervals_ms`` は外付けデバイス以外で**周期的に届く**メトリクスと
    その収集周期（Internal Telemetry など。#65）。ここに載ったメトリクスも
    期待サンプル数を持ち、1件も届かなかった分が0行のバケットとして残る。
    Store は上位の設定を読まない（レイヤは一方向）ので、周期は呼び出し側が渡す。

    ``now_ms`` を渡すと、周期メトリクスの穴埋めを**ジョブの時計で最後に完了した分**まで
    進める。生データの最新時刻を上限にすると、collector と ingest がともに止まった
    区間が記録されないため。進行中の分は欠測に数えない。外付けデバイスの ``air.*``
    には適用しない（決定記録 0008 §2.1.1「停止後にまで期待値を作らない」。デバイスの
    撤去と通信断をロールアップからは区別できない）。周期メトリクスは設定で有効な
    間だけ登録されるので、登録中の停止は欠測として扱い、無効だった期間は欠測に
    しない。登録状態は ``periodic_metric_registrations`` に保存する（決定記録 0038）。

    **未集計の範囲だけを見る。** 毎回すべてを数え直すと、保持期間ぶんの行を
    走査することになる。ただし前回の最終バケットは**必ず数え直す**。
    そのバケットは実行時点で途中だった可能性があり、
    その後に届いた行を取り込めていないため。

    **メトリクスごとにループする。** `WHERE ts_ms >= ?` の1本にすると主キー
    `(metric, ts_ms)` を使えず、`SCAN readings` で保持期間ぶんを毎回舐める
    （決定記録 0002 §2.4 と同じ理由）。トランザクションもメトリクス単位にして、
    取り込みの書き込みロック待ちを短く保つ。
    """
    conn = store.connection
    _periodic_expected_per_minute(periodic_intervals_ms or {})  # 周期の検証だけ先に行う
    newest = conn.execute("SELECT MAX(ts_ms) FROM readings").fetchone()[0]
    start = conn.execute("SELECT MAX(bucket_ms) FROM readings_1m").fetchone()[0]
    run_ms = now_ms if now_ms is not None else int(newest) if newest is not None else 0
    with store.transaction():
        registrations = _sync_registrations(conn, periodic_intervals_ms or {}, run_ms)
    # 今回外れた metric（前回は登録中）も、この実行では前回の周期で埋める。
    # 前回から今回までの停止は、無効にされる前の停止かもしれないため（決定記録 0038）
    retiring = {
        metric
        for metric, state in registrations.items()
        if metric not in (periodic_intervals_ms or {})
    }
    periodic = _periodic_expected_per_minute(
        {metric: state.interval_ms for metric, state in registrations.items()}
    )
    try:
        return _rollup_minutes(store, periodic, registrations, newest, start, now_ms)
    finally:
        # 穴埋めのあとで外す。途中で失敗しても、次の実行で同じ区間を埋め直せる
        if retiring:
            with store.transaction():
                conn.executemany(
                    "UPDATE periodic_metric_registrations SET registered = 0, changed_ms = ? "
                    "WHERE metric = ?",
                    [(run_ms, metric) for metric in sorted(retiring)],
                )


def _rollup_minutes(
    store: SqliteStore,
    periodic: Mapping[str, int],
    registrations: Mapping[str, _Registration],
    newest: int | None,
    start: int | None,
    now_ms: int | None,
) -> int:
    conn = store.connection
    # 最後に完了した分。進行中の分はまだ届く途中なので欠測に数えない
    completed_bucket = (
        _floor(now_ms, MINUTE_MS) - MINUTE_MS if now_ms is not None and periodic else None
    )
    if newest is None:
        # 生データが全部消えても、観測済みの周期メトリクスの欠測は埋め続ける
        if start is None or completed_bucket is None:
            return 0
        newest = -1
    elif start is None:
        # 初回。エポックから数えると欠測の穴埋めが天文学的な件数になる
        oldest = conn.execute("SELECT MIN(ts_ms) FROM readings").fetchone()[0]
        start = _floor(int(oldest), MINUTE_MS)
    newest_bucket = _floor(int(newest), MINUTE_MS)
    periodic_until = (
        newest_bucket if completed_bucket is None else max(newest_bucket, completed_bucket)
    )
    expected = _expected_per_minute(conn)

    written = 0
    # 登録済みの周期メトリクスは、生データが1行も残っていなくても回す。
    # 停止した collector の最後の生データが保持期間で消えると `store.metrics()`
    # から外れ、以降の日に0行バケットが作られなくなるため。穴埋めは保存した
    # 登録状態の「欠測を数え始める時刻」以降に限る（決定記録 0038）
    metrics = sorted(set(store.metrics()) | periodic.keys())
    for metric in metrics:
        # 期待サンプル数は**周期的に届くメトリクスにだけ**意味がある。
        # `sys.dropped_samples` は起きたときしか書かない（決定記録 0007 §2.4）ので、
        # 期待値を持たせると欠測率が無意味な値になる
        metric_expected = expected if metric in METRIC_TO_CHANNEL else periodic.get(metric)
        with store.transaction():
            cursor = conn.execute(
                "INSERT OR REPLACE INTO readings_1m "
                "(metric, bucket_ms, min_value, max_value, mean_value, "
                " ok_value_count, row_count, expected_count) "
                f"SELECT ?, (ts_ms / {MINUTE_MS}) * {MINUTE_MS} AS bucket_ms, "
                "  MIN(CASE WHEN quality = 'ok' THEN value END), "
                "  MAX(CASE WHEN quality = 'ok' THEN value END), "
                "  AVG(CASE WHEN quality = 'ok' THEN value END), "
                "  SUM(CASE WHEN quality = 'ok' AND value IS NOT NULL THEN 1 ELSE 0 END), "
                "  COUNT(*), ? "
                "FROM readings WHERE metric = ? AND ts_ms >= ? AND ts_ms <= ? "
                "GROUP BY bucket_ms",
                (metric, metric_expected, metric, start, newest),
            )
            written += int(cursor.rowcount)
            if metric in periodic:
                metric_from = registrations[metric].active_from_ms
                if metric_from is not None:
                    written += _fill_absent_minutes(
                        conn,
                        metric,
                        max(start, _floor(metric_from, MINUTE_MS)),
                        periodic_until,
                        metric_expected,
                    )
            else:
                written += _fill_absent_minutes(conn, metric, start, newest_bucket, metric_expected)
    return written


@dataclass(frozen=True)
class _Registration:
    """この実行で周期メトリクスとして扱う metric の状態。"""

    interval_ms: int
    active_from_ms: int | None
    """欠測を数え始める時刻。再開後にまだ観測が無ければ ``None``。"""


def _sync_registrations(
    conn: sqlite3.Connection, intervals_ms: Mapping[str, int], run_ms: int
) -> dict[str, _Registration]:
    """周期メトリクスの登録状態を保存し、この実行で扱う metric を返す（決定記録 0038）。

    - 初めて登録された、または無効から戻ったメトリクスは、その区間の
      **最初の観測**から欠測を数える。無効だった期間と、再開後の最初の観測より前は
      欠測にしない。まだ観測が無ければ ``active_from_ms = None``（今回は埋めない）
    - 前回登録中で今回外れた metric も、保存した周期で返す。呼び出し側はこの実行の
      上限まで埋めてから ``registered = 0`` にする。設定を変えた正確な時刻は分からない
      ため、前回の実行から今回の検出までの停止は欠測として残す

    登録状態を readings から推測しないのは、全 source が静かな期間には痕跡が残らず、
    無効期間と停止を区別できないため。登録はロールアップの実行時にしか見えないので、
    2回の実行の間に無効化と再有効化の両方が起きた場合、その期間は欠測として残る。
    """
    result: dict[str, _Registration] = {}
    for row in conn.execute(
        "SELECT metric, interval_ms, active_from_ms FROM periodic_metric_registrations "
        "WHERE registered = 1"
    ).fetchall():
        if row[0] not in intervals_ms:
            result[str(row[0])] = _Registration(
                interval_ms=int(row[1]),
                active_from_ms=None if row[2] is None else int(row[2]),
            )
    for metric in sorted(intervals_ms):
        row = conn.execute(
            "SELECT registered, since_ms, active_from_ms, changed_ms "
            "FROM periodic_metric_registrations WHERE metric = ?",
            (metric,),
        ).fetchone()
        if row is None:
            since, changed = 0, run_ms
            active: int | None = _first_observation_since(conn, metric, since)
        elif int(row[0]) == 0:
            # 無効から戻った。無効にしたと記録した時刻より後の観測だけを見る
            since, changed = int(row[3]), run_ms
            active = _first_observation_since(conn, metric, since)
        else:
            since, changed = int(row[1]), int(row[3])
            active = (
                int(row[2]) if row[2] is not None else _first_observation_since(conn, metric, since)
            )
        conn.execute(
            "INSERT OR REPLACE INTO periodic_metric_registrations "
            "(metric, registered, interval_ms, since_ms, active_from_ms, changed_ms) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (metric, intervals_ms[metric], since, active, changed),
        )
        result[metric] = _Registration(interval_ms=intervals_ms[metric], active_from_ms=active)
    return result


def _first_observation_since(conn: sqlite3.Connection, metric: str, since_ms: int) -> int | None:
    """``since_ms`` 以降の最初の観測時刻。生データが消えた範囲は1分ロールアップで見る。"""
    raw = conn.execute(
        "SELECT MIN(ts_ms) FROM readings WHERE metric = ? AND ts_ms >= ?", (metric, since_ms)
    ).fetchone()[0]
    rolled = conn.execute(
        "SELECT MIN(bucket_ms) FROM readings_1m "
        "WHERE metric = ? AND bucket_ms >= ? AND row_count > 0",
        (metric, since_ms),
    ).fetchone()[0]
    candidates = [int(value) for value in (raw, rolled) if value is not None]
    return min(candidates) if candidates else None


def _fill_absent_minutes(
    conn: sqlite3.Connection, metric: str, start: int, newest_bucket: int, expected: int | None
) -> int:
    """1件も届かなかった分を「0行のバケット」として作る。

    **これが無いと通信断が記録から消える。** 行が無い分は集約時に数えられず、
    `SUM(expected_count)` が届いた分だけの合計になるため、
    1分まるごと落ちた1時間が「欠測ゼロ」として残ってしまう。
    生データを30日で消したあとは復元できない（決定記録 0002 §2.8 が
    分けようとした「センサー異常」と「サンプル不着」の区別が失われる）。

    埋めるのは**そのメトリクスを観測し始めてから**の範囲だけ。
    設置前や停止後にまで期待値を作らない。
    """
    if expected is None:
        return 0
    first = conn.execute(
        "SELECT MIN(bucket_ms) FROM readings_1m WHERE metric = ?", (metric,)
    ).fetchone()[0]
    if first is None:
        return 0
    fill_from = max(start, int(first))
    if fill_from > newest_bucket:
        return 0
    cursor = conn.execute(
        "INSERT OR IGNORE INTO readings_1m "
        "(metric, bucket_ms, min_value, max_value, mean_value, "
        " ok_value_count, row_count, expected_count) "
        "WITH RECURSIVE minutes(bucket_ms) AS ("
        "  SELECT ? UNION ALL "
        f"  SELECT bucket_ms + {MINUTE_MS} FROM minutes WHERE bucket_ms + {MINUTE_MS} <= ?"
        ") SELECT ?, bucket_ms, NULL, NULL, NULL, 0, 0, ? FROM minutes",
        (fill_from, newest_bucket, metric, expected),
    )
    return int(cursor.rowcount)


def _periodic_expected_per_minute(intervals_ms: Mapping[str, int]) -> dict[str, int]:
    """呼び出し側が登録した周期から、1分あたりの期待サンプル数を出す。

    外付けデバイスのチャネルは起動バナーの周期が正本なので、二重登録を拒否する。
    周期は1分を割り切る値に限る。割り切れない周期（例: 7000 ms）では1分に届く件数が
    8件と9件で揺れ、固定の期待値では欠測率が負になりうる。1分より長い周期も
    期待値が0になり欠測率が定義できないので、同じ条件で拒否される。
    """
    result: dict[str, int] = {}
    for metric, interval_ms in intervals_ms.items():
        if metric in METRIC_TO_CHANNEL:
            raise ValueError(f"デバイスのチャネルは周期を登録できない: {metric}")
        if interval_ms <= 0 or MINUTE_MS % interval_ms != 0:
            raise ValueError(f"周期は {MINUTE_MS} ms を割り切る値にする: {metric}={interval_ms}")
        result[metric] = MINUTE_MS // interval_ms
    return result


def _floor(value: int, unit: int) -> int:
    return (value // unit) * unit


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
    _refuse_dataset_db(store)
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


def _refuse_dataset_db(store: SqliteStore) -> None:
    """dataset専用DB（#83）には保持期間を適用しない。

    dataset DBはsource run 1本の凍結された記録で、readingsは完了時に封印される。
    保持期間で生データやControlTickを消すと、datasetの再生成が黙って別物になる。
    削除0件でもControlTickは消え得るため、triggerに任せず入口で拒否する。
    """
    if store.dataset_source_run() is not None:
        raise ValueError("dataset専用DBにはロールアップ・保持期間を適用しない（#83）")


def vacuum(store: SqliteStore) -> None:
    """ファイルを縮める。**定期実行しない。**

    削除で空いたページは以降の挿入が再利用するため、定常状態ではファイルは
    増え続けない。`VACUUM` が要るのは「縮めたい」ときだけで、実行中は
    書き込みを止め、一時的に元と同じだけの空き容量を要求する。
    取り込みを止められるときに手で実行する（CLI の `--vacuum`）。
    """
    store.connection.execute("VACUUM")


def run(
    store: SqliteStore,
    rules: RetentionRules,
    *,
    now_ms: int,
    periodic_intervals_ms: Mapping[str, int] | None = None,
) -> Result:
    """ロールアップ → 削除の順で実行する。"""
    _refuse_dataset_db(store)
    minutes = rollup_minutes(store, periodic_intervals_ms=periodic_intervals_ms, now_ms=now_ms)
    hours = rollup_hours(store)
    deleted, cutoff = apply_retention(store, rules, now_ms=now_ms)
    trace_cutoff = max(0, now_ms - rules.control_trace_retention_ms)
    deleted_traces = store.delete_control_traces_before(trace_cutoff)
    return Result(
        minute_buckets=minutes,
        hour_buckets=hours,
        deleted_rows=deleted,
        cutoff_ms=cutoff,
        deleted_control_traces=deleted_traces,
        control_trace_cutoff_ms=trace_cutoff,
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


def main(
    argv: Sequence[str] | None = None,
    *,
    periodic_intervals_ms: Mapping[str, int] | None = None,
    clock: Clock | None = None,
) -> int:
    """ロールアップの CLI 本体。cron / systemd タイマーから1日1回呼ぶ想定。

    入口の `coldaisle-rollup` は `coldaisle.rollup_job` で、Internal Telemetry の設定から
    周期メトリクスを組み立ててここへ渡す（Store は上位の設定を import しない）。

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
    used_clock: Clock = clock or WallClock()
    # 既定の `var/` は追跡されていない。デーモンと同じく、無ければ作る
    args.db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=used_clock)
    try:
        result = run(
            store, rules, now_ms=used_clock.now_ms(), periodic_intervals_ms=periodic_intervals_ms
        )
        LOGGER.info(
            "ロールアップと削除を実行した",
            extra={
                logs.FIELDS_KEY: {
                    "minute_buckets": result.minute_buckets,
                    "hour_buckets": result.hour_buckets,
                    "deleted_rows": result.deleted_rows,
                    "cutoff_ms": result.cutoff_ms,
                    "raw_days": rules.raw_days,
                    "deleted_control_traces": result.deleted_control_traces,
                    "control_trace_cutoff_ms": result.control_trace_cutoff_ms,
                    "control_trace_days": rules.control_trace_days,
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
