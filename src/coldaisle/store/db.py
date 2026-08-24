"""SQLite ストア（L1）。スキーマは決定記録 0002 が唯一の参照先。

このクラスは**保存と照会だけ**を行う。品質の判定は取り込み側（`quality.classify`）が
済ませた状態で受け取る。唯一の例外が `stale` で、これは「前回受信からの経過」という
読み出し時にしか分からない条件のため `latest()` が付ける。
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from types import TracebackType

from coldaisle.store import migrations
from coldaisle.store.models import (
    LatestReading,
    Quality,
    RollupPoint,
    Sample,
    SeriesPoint,
    Stats,
    validate_metric,
)
from coldaisle.store.quality import QualityRules

P95 = 0.95
"""FR-303 が要求する分位点。"""

_LATEST_SQL = """
WITH RECURSIVE metrics(metric) AS (
    SELECT MIN(metric) FROM readings
    UNION ALL
    SELECT (SELECT MIN(metric) FROM readings WHERE metric > metrics.metric)
    FROM metrics WHERE metric IS NOT NULL
)
SELECT r.metric, r.ts_ms, r.value, r.quality
FROM metrics
JOIN readings r
  ON r.metric = metrics.metric
 AND r.ts_ms = (SELECT MAX(ts_ms) FROM readings WHERE metric = metrics.metric)
WHERE metrics.metric IS NOT NULL
"""
"""最新値を主キーのシークだけで引く（決定記録 0004 §2.11）。

`v_latest`（0002 §2.12）は `GROUP BY metric` のため `SCAN readings` になり、
**保持期間に比例して遅くなる**。実測で 105万行のとき 44.8ms、
30日保持（約730万行）では 300ms 台に達する。`latest()` は
`/api/v1/health/summary` から毎秒叩かれるため、ここは行数に依存させない。

再帰 CTE で「次に大きい metric」を主キーで辿り、各メトリクスの最新行を
1回のシークで取る。走査量はメトリクス数に比例する（実測 0.01ms）。
"""


def now_ms() -> int:
    """現在時刻（Unix ミリ秒、UTC）。テストで差し替えられるよう関数にしている。"""
    return time.time_ns() // 1_000_000


def _enable_wal(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    """WAL へ切り替える。切り替え済みなら何もしない。

    **`PRAGMA busy_timeout` はこの切り替えを待ってくれない。**
    journal_mode の変更は排他ロックを要求するが、SQLite の busy ハンドラは
    ここでは呼ばれず、即座に `database is locked` を返す。
    新しい DB を取り込みデーモンと API が同時に開くと、片方が
    マイグレーション中のロックを持っているため、これに当たる。
    """
    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        return
    deadline = time.monotonic() + busy_timeout_ms / 1000
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
        else:
            return


class Aggregation(StrEnum):
    """`GET /api/v1/series` の `agg`（FR-302）。

    `5m` は v1 のテーブルに対応するものが無い（決定記録 0002 §2.8 の3段は
    生 / 1分 / 1時間）。1分から合成するのは #10 の担当で、ここでは扱わない。
    """

    RAW = "raw"
    MINUTE = "1m"
    HOUR = "1h"


_ROLLUP_TABLES = {
    Aggregation.MINUTE: "readings_1m",
    Aggregation.HOUR: "readings_1h",
}


class SqliteStore:
    """1接続＝1インスタンス。**インスタンスをスレッド間で共有しない。**

    WAL では読み手と書き手が互いをブロックしないため、取り込みデーモンと API は
    それぞれ自分の接続を持てばよい。共有して排他を書くより単純で、
    片方が長い読み出しをしても取り込みが止まらない。
    """

    def __init__(
        self,
        path: Path | str,
        *,
        rules: QualityRules,
        clock: Callable[[], int] = now_ms,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        # `rules` は必須。既定値へ黙って落ちると、設定を読めていないことに
        # 気づかないまま `stale` の判定だけが別のしきい値で動く（AGENTS.md ルール6）
        self._rules = rules
        self._clock = clock
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # busy_timeout を最初に設定する。WAL への切り替えは一瞬だけ排他ロックを取るため、
        # 取り込みデーモンが動いている最中に API が開くと、これが無いと即座に失敗する
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        # WAL: 読み出し中も取り込みが書ける（NFR-02 の「取りこぼさない」の前提）
        # synchronous=NORMAL: WAL と組み合わせた場合、電源断で失われるのは
        # 直近のチェックポイント以降のみ。2.5秒周期の観測データにはこれで足りる
        _enable_wal(self._conn, busy_timeout_ms)
        self._conn.execute("PRAGMA synchronous = NORMAL")
        migrations.apply_pending(self._conn, self._clock())

    # ------------------------------------------------------------------ 基本

    @property
    def connection(self) -> sqlite3.Connection:
        """生の接続。ロールアップ（#10）など、この層の外で SQL を足すため。"""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        # BEGIN IMMEDIATE で最初から書き込みロックを取る。DEFERRED だと
        # 読んでから書く途中で昇格に失敗し、busy_timeout を待たずに落ちる
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ 書き込み

    def insert_sample(self, sample: Sample) -> int:
        """1サンプルを保存し、書いた行数を返す（FR-201）。

        同じ `(metric, ts_ms)` が既にあると `sqlite3.IntegrityError` を送出する。
        黙って上書きすると、同じ時刻に2つの値が観測された事実が消える。
        取り込みループはこれを1サンプルのパース失敗と同様に**記録して継続**する。
        """
        return self.insert_samples((sample,))

    def insert_samples(self, samples: Iterable[Sample]) -> int:
        """複数サンプルを1トランザクションで保存する。

        リプレイと試験のための一括投入。COMMIT は1回で済むため、
        1件ずつ `insert_sample` を呼ぶより桁で速い。
        """
        rows = [
            (reading.metric, sample.ts_ms, reading.value, reading.quality.value)
            for sample in samples
            for reading in sample.readings
        ]
        if not rows:
            return 0
        with self._transaction():
            self._conn.executemany(
                "INSERT INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    # ------------------------------------------------------------------ 読み出し

    def latest(self, *, at_ms: int | None = None) -> dict[str, LatestReading]:
        """メトリクスごとの最新値（FR-301）。

        `at_ms` から `stale_after_ms` 以上離れた値は `stale` に落とす。
        **保存時の品質は上書きする。** 古い値を `ok` のまま返すと、
        止まった時計を正常表示する（api-contract §3 が禁じている失敗）。

        `v_latest` ビューは使わない。理由は `_LATEST_SQL` を参照。
        """
        now = self._clock() if at_ms is None else at_ms
        rows = self._conn.execute(_LATEST_SQL).fetchall()
        latest: dict[str, LatestReading] = {}
        for row in rows:
            age_ms = now - int(row["ts_ms"])
            quality = Quality(row["quality"])
            if age_ms >= self._rules.stale_after_ms:
                quality = Quality.STALE
            latest[row["metric"]] = LatestReading(
                metric=row["metric"],
                ts_ms=int(row["ts_ms"]),
                value=row["value"],
                quality=quality,
                age_ms=age_ms,
            )
        return latest

    def series(
        self, metric: str, start_ms: int, end_ms: int, *, limit: int | None = None
    ) -> tuple[SeriesPoint, ...]:
        """生データの時系列（FR-302 の `agg=raw`）。範囲は `[start_ms, end_ms)`。

        `limit` を超える場合は**古い側を落とす**。監視で欠けてはならないのは
        直近であり、先頭から打ち切ると最新の異常がグラフから消える。
        返す並びは常に時刻の昇順。
        """
        validate_metric(metric)
        self._check_range(start_ms, end_ms)
        if limit is None:
            rows = self._conn.execute(
                "SELECT ts_ms, value, quality FROM readings "
                "WHERE metric = ? AND ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms",
                (metric, start_ms, end_ms),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT ts_ms, value, quality FROM ("
                "  SELECT ts_ms, value, quality FROM readings"
                "  WHERE metric = ? AND ts_ms >= ? AND ts_ms < ?"
                "  ORDER BY ts_ms DESC LIMIT ?"
                ") ORDER BY ts_ms",
                (metric, start_ms, end_ms, limit),
            ).fetchall()
        return tuple(
            SeriesPoint(
                ts_ms=int(row["ts_ms"]), value=row["value"], quality=Quality(row["quality"])
            )
            for row in rows
        )

    def rollup(
        self, metric: str, start_ms: int, end_ms: int, agg: Aggregation
    ) -> tuple[RollupPoint, ...]:
        """ロールアップ済みの時系列（FR-302 の `agg=1m` / `1h`）。

        バケットを書くのは #10。ここは読み出しのみで、`bucket_ms` は
        `[start_ms, end_ms)` に入るものを昇順で返す。
        """
        if agg is Aggregation.RAW:
            raise ValueError("agg=raw は series() を使う")
        validate_metric(metric)
        self._check_range(start_ms, end_ms)
        rows = self._conn.execute(
            # テーブル名は _ROLLUP_TABLES の値だけを取り、呼び出し側の文字列は入らない
            "SELECT bucket_ms, min_value, max_value, mean_value, ok_value_count, "
            f"row_count, expected_count FROM {_ROLLUP_TABLES[agg]} "
            "WHERE metric = ? AND bucket_ms >= ? AND bucket_ms < ? ORDER BY bucket_ms",
            (metric, start_ms, end_ms),
        ).fetchall()
        return tuple(
            RollupPoint(
                bucket_ms=int(row["bucket_ms"]),
                min_value=row["min_value"],
                max_value=row["max_value"],
                mean_value=row["mean_value"],
                ok_value_count=int(row["ok_value_count"]),
                row_count=int(row["row_count"]),
                expected_count=row["expected_count"],
            )
            for row in rows
        )

    def stats(self, metric: str, start_ms: int, end_ms: int) -> Stats:
        """窓 `[start_ms, end_ms)` の統計量（FR-303）。

        min / max / mean / p95 / 傾きは `quality='ok'` かつ値を持つ行だけから出す。
        欠測率の母数は**窓に入った行数**であり、届かなかったサンプルは数に入らない。
        したがってここで返す `missing_ratio` は下限値である
        （期待サンプル数を使った本来の欠測率はロールアップ側が持つ。決定記録 0002 §2.8）。
        """
        validate_metric(metric)
        self._check_range(start_ms, end_ms)
        window = (metric, start_ms, end_ms)

        row_count = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM readings WHERE metric = ? AND ts_ms >= ? AND ts_ms < ?",
                window,
            ).fetchone()[0]
        )
        # 傾きは最小二乗法。x の原点を窓の先頭に取り、単位を分にそろえる。
        # ts_ms のままだと x が 1e12 台になり、x*x の総和で精度が落ちる
        aggregates = self._conn.execute(
            "SELECT COUNT(*) AS n, MIN(value) AS min_value, MAX(value) AS max_value, "
            "AVG(value) AS mean_value, SUM(x) AS sx, SUM(value) AS sy, "
            "SUM(x * value) AS sxy, SUM(x * x) AS sxx FROM ("
            "  SELECT value, (ts_ms - ?) / 60000.0 AS x FROM readings"
            "  WHERE metric = ? AND ts_ms >= ? AND ts_ms < ? AND quality = 'ok'"
            "    AND value IS NOT NULL"
            ")",
            (start_ms, metric, start_ms, end_ms),
        ).fetchone()
        ok_count = int(aggregates["n"])

        return Stats(
            metric=metric,
            start_ms=start_ms,
            end_ms=end_ms,
            row_count=row_count,
            ok_value_count=ok_count,
            min_value=aggregates["min_value"],
            max_value=aggregates["max_value"],
            mean_value=aggregates["mean_value"],
            p95_value=self._percentile(window, ok_count, P95),
            slope_per_min=_slope(
                ok_count,
                aggregates["sx"],
                aggregates["sy"],
                aggregates["sxy"],
                aggregates["sxx"],
            ),
            missing_ratio=None if row_count == 0 else 1.0 - ok_count / row_count,
        )

    # ------------------------------------------------------------------ 内部

    def _percentile(
        self, window: tuple[str, int, int], ok_count: int, ratio: float
    ) -> float | None:
        """nearest-rank 法。**測定していない値を補間で作らない。**

        線形補間だと、センサーが出したことのない温度が p95 として API に出る。
        SQLite に分位関数が無いため、順位を計算して1行だけ取り出す。
        """
        if ok_count == 0:
            return None
        offset = math.ceil(ratio * ok_count) - 1
        row = self._conn.execute(
            "SELECT value FROM readings "
            "WHERE metric = ? AND ts_ms >= ? AND ts_ms < ? "
            "  AND quality = 'ok' AND value IS NOT NULL "
            "ORDER BY value LIMIT 1 OFFSET ?",
            (*window, offset),
        ).fetchone()
        return None if row is None else float(row["value"])

    @staticmethod
    def _check_range(start_ms: int, end_ms: int) -> None:
        if start_ms > end_ms:
            raise ValueError(f"範囲が逆転している: start_ms={start_ms} > end_ms={end_ms}")


def _slope(
    n: int, sx: float | None, sy: float | None, sxy: float | None, sxx: float | None
) -> float | None:
    """最小二乗法の傾き。点が1つ以下、または時刻が全て同じなら None。"""
    if n < 2 or sx is None or sy is None or sxy is None or sxx is None:
        return None
    denominator = n * sxx - sx * sx
    if denominator == 0:
        return None
    return (n * sxy - sx * sy) / denominator
