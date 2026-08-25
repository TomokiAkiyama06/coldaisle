"""SQLite ストア（L1）。スキーマは決定記録 0002 が唯一の参照先。

このクラスは**保存と照会だけ**を行う。品質の判定は取り込み側（`quality.classify`）が
済ませた状態で受け取る。唯一の例外が `stale` で、これは「前回受信からの経過」という
読み出し時にしか分からない条件のため `latest()` が付ける。
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from types import TracebackType

from coldaisle.clock import Clock
from coldaisle.store import migrations
from coldaisle.store.models import (
    DeviceRecord,
    LatestReading,
    Quality,
    RollupPoint,
    Sample,
    SensorRecord,
    SeriesPoint,
    Stats,
    validate_metric,
)
from coldaisle.store.quality import QualityRules

P95 = 0.95
"""FR-303 が要求する分位点。"""


def combine_minutes_sql(
    bucket_ms: int, where: str, *, with_metric: bool = False, having: bool = False
) -> str:
    """1分バケットを `bucket_ms` 幅へまとめる SELECT を組み立てる。

    5分の読み出し（合成）と1時間の書き込み（保存）が**同じ式**を使う。
    別々に書くと、平均の重み付けを片方だけ直す事故が起きる。

    **`mean_value` は `ok_value_count` で加重する。** 単純平均にすると、
    欠測のあるバケットが同じ重みで効いて値が狂う（決定記録 0004 §2.9）。
    """
    metric_column = "metric, " if with_metric else ""
    group_by = "metric, 2" if with_metric else "1"
    return (
        f"SELECT {metric_column}(bucket_ms / {bucket_ms}) * {bucket_ms} AS bucket_ms, "
        # 列名を付ける。合成した結果も `readings_1m` と同じ形で読めるようにする
        "MIN(min_value) AS min_value, MAX(max_value) AS max_value, "
        "CASE WHEN SUM(ok_value_count) > 0 "
        "     THEN SUM(mean_value * ok_value_count) / SUM(ok_value_count) END AS mean_value, "
        "SUM(ok_value_count) AS ok_value_count, SUM(row_count) AS row_count, "
        # いずれかのバケットで期待値が不明なら、合計も不明にする（決定記録 0002 §2.8）
        "CASE WHEN COUNT(*) = COUNT(expected_count) THEN SUM(expected_count) END "
        "AS expected_count "
        f"FROM readings_1m {where} GROUP BY {group_by} "
        # 端のバケットは**完成した状態で組み立ててから**捨てる。先に絞ると、
        # 途中までの平均が完全なバケットとして返る（下の呼び出し側を参照）。
        # HAVING では別名ではなく式を書き直す。SQLite は HAVING の `bucket_ms` を
        # **元の列**として解決するため、別名を書くと絞り込みが効かない
        + (
            f"HAVING (bucket_ms / {bucket_ms}) * {bucket_ms} >= ? "
            f"AND (bucket_ms / {bucket_ms}) * {bucket_ms} < ? "
            if having
            else ""
        )
        + "ORDER BY bucket_ms"
    )


_METRICS_SQL = """
WITH RECURSIVE metrics(metric) AS (
    SELECT MIN(metric) FROM readings
    UNION ALL
    SELECT (SELECT MIN(metric) FROM readings WHERE metric > metrics.metric)
    FROM metrics WHERE metric IS NOT NULL
)
SELECT metric FROM metrics WHERE metric IS NOT NULL
"""

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

    `5m` に対応するテーブルは無い（決定記録 0002 §2.8 の3段は生 / 1分 / 1時間）。
    **読み出しのたびに1分バケットから合成する**（決定記録 0004 §2.9）。
    """

    RAW = "raw"
    MINUTE = "1m"
    FIVE_MINUTES = "5m"
    HOUR = "1h"


MINUTE_MS = 60_000
FIVE_MINUTES_MS = 5 * MINUTE_MS
HOUR_MS = 60 * MINUTE_MS

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
        clock: Clock,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        # `rules` と `clock` は必須。既定へ黙って落ちると、設定やソースと違う
        # しきい値・時刻で `stale` が判定される（AGENTS.md ルール6 / #42）。
        # 特に時計は、取り込みが SimulatedClock で保存が実時計、という
        # 組み合わせが静かに成立すると、圧縮再生の結果が説明できなくなる
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
        migrations.apply_pending(self._conn, self._clock.now_ms())

    # ------------------------------------------------------------------ 基本

    @property
    def clock(self) -> Clock:
        """この接続が `stale` 判定に使う時計。

        **合成の起点が同じインスタンスを配れたか**を呼び出し側が検査できるように
        公開する。`Clock` は型しか縛らないため、取り込みと保存で別の時計を
        持っていても型検査は通ってしまう（#42）。
        """
        return self._clock

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
    def transaction(self) -> Iterator[None]:
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
        with self.transaction():
            self._conn.executemany(
                "INSERT INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def record_hello(
        self, device: DeviceRecord, sensors: Sequence[SensorRecord], *, at_ms: int
    ) -> None:
        """起動バナーを `devices` / `device_sensors` へ反映する（決定記録 0002 §2.10）。

        センサーの集合は**丸ごと置き換える。** 起動バナーはその時点の全構成を
        申告するため、差分更新にすると外したセンサーの行が残り続ける。
        置き換える前の ROM が要る場合は `sensors_for()` で先に読む（FR-403 / #14）。

        `first_seen_ms` は初回だけ書く。上書きすると「いつから見ているか」が消える。
        """
        with self.transaction():
            self._conn.execute(
                "INSERT INTO devices "
                "(device_id, fw, schema_v, interval_ms, first_seen_ms, last_seen_ms, last_hello_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET "
                "  fw = excluded.fw, schema_v = excluded.schema_v,"
                "  interval_ms = excluded.interval_ms,"
                "  last_seen_ms = excluded.last_seen_ms, last_hello_ms = excluded.last_hello_ms",
                (
                    device.device_id,
                    device.fw,
                    device.schema_v,
                    device.interval_ms,
                    at_ms,
                    at_ms,
                    at_ms,
                ),
            )
            self._conn.execute(
                "DELETE FROM device_sensors WHERE device_id = ?", (device.device_id,)
            )
            self._conn.executemany(
                "INSERT INTO device_sensors "
                "(device_id, channel, kind, gpio, rom, resolution, updated_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        device.device_id,
                        sensor.channel,
                        sensor.kind,
                        sensor.gpio,
                        sensor.rom,
                        sensor.resolution,
                        at_ms,
                    )
                    for sensor in sensors
                ],
            )

    # ------------------------------------------------------------------ 読み出し

    def device(self, device_id: str) -> DeviceRecord | None:
        row = self._conn.execute(
            "SELECT device_id, fw, schema_v, interval_ms FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if row is None:
            return None
        return DeviceRecord(
            device_id=row["device_id"],
            fw=row["fw"],
            schema_v=row["schema_v"],
            interval_ms=row["interval_ms"],
        )

    def sensors_for(self, device_id: str) -> tuple[SensorRecord, ...]:
        """申告済みのセンサー構成。ROM の突き合わせ（FR-403）に使う。"""
        rows = self._conn.execute(
            "SELECT channel, kind, gpio, rom, resolution FROM device_sensors "
            "WHERE device_id = ? ORDER BY channel",
            (device_id,),
        ).fetchall()
        return tuple(
            SensorRecord(
                channel=row["channel"],
                kind=row["kind"],
                gpio=row["gpio"],
                rom=row["rom"],
                resolution=row["resolution"],
            )
            for row in rows
        )

    def latest(self, *, at_ms: int | None = None) -> dict[str, LatestReading]:
        """メトリクスごとの最新値（FR-301）。

        `at_ms` から `stale_after_ms` 以上離れた値は `stale` に落とす。
        **保存時の品質は上書きする。** 古い値を `ok` のまま返すと、
        止まった時計を正常表示する（api-contract §3 が禁じている失敗）。

        `v_latest` ビューは使わない。理由は `_LATEST_SQL` を参照。
        """
        now = self._clock.now_ms() if at_ms is None else at_ms
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

    def metrics(self) -> tuple[str, ...]:
        """保存済みのメトリクス名。走査量はメトリクス数に比例する（決定記録 0004 §2.11）。

        `SELECT DISTINCT metric` は全走査になる。保持期間ぶんの行を舐めるので、
        削除ジョブ（FR-203）のたびに数百万行を読むことになってしまう。
        """
        rows = self._conn.execute(_METRICS_SQL).fetchall()
        return tuple(row["metric"] for row in rows)

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
        if agg is Aggregation.FIVE_MINUTES:
            # 表を持たず、1分バケットから合成する（決定記録 0004 §2.9）。
            # **入力はバケット境界まで広げ、出力を要求範囲で絞る。**
            # 先に絞ると、12:02 の要求が 12:02〜12:04 だけの平均を
            # 「12:00 のバケット」として返してしまう
            aligned_start = (start_ms // FIVE_MINUTES_MS) * FIVE_MINUTES_MS
            aligned_end = -(-end_ms // FIVE_MINUTES_MS) * FIVE_MINUTES_MS
            rows = self._conn.execute(
                combine_minutes_sql(
                    FIVE_MINUTES_MS,
                    "WHERE metric = ? AND bucket_ms >= ? AND bucket_ms < ?",
                    having=True,
                ),
                (metric, aligned_start, aligned_end, start_ms, end_ms),
            ).fetchall()
        else:
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
