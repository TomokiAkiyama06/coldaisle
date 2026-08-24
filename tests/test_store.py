"""SQLite ストアの振る舞い（#5、決定記録 0002）。

受入基準は「1万サンプルの投入と範囲クエリが通る」こと。
それに加えて、**DB 側の不変条件（CHECK 制約）が実際に効いていること**を
アプリを経由せずに確かめる。制約が無くてもアプリ経由のテストは緑になるため。
"""

import sqlite3

import pytest

from coldaisle.store import (
    Aggregation,
    MigrationError,
    Quality,
    QualityRules,
    Reading,
    RollupPoint,
    Sample,
    SqliteStore,
    db,
)
from coldaisle.store import migrations as mig

INTERVAL_MS = 2_500
"""要件の送信周期。1万サンプル分の時刻を組み立てるのに使う。"""

CHANNELS = (
    "air.room",
    "air.room_humidity",
    "air.front_intake",
    "air.gpu_intake",
    "air.gpu_exhaust",
    "air.top_exhaust",
    "air.rear_exhaust",
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "coldaisle.db"


@pytest.fixture
def store(db_path):
    with SqliteStore(db_path) as opened:
        yield opened


def sample(ts_ms: int, **values: float | None) -> Sample:
    readings = tuple(
        Reading(
            metric=metric,
            value=value,
            quality=Quality.OK if value is not None else Quality.MISSING,
        )
        for metric, value in values.items()
    )
    return Sample(ts_ms=ts_ms, readings=readings)


# ---------------------------------------------------------------- マイグレーション


def test_open_applies_migrations(store):
    applied = store.connection.execute("SELECT version FROM schema_version").fetchall()
    assert [row["version"] for row in applied] == [1]


def test_reopen_does_not_reapply(db_path):
    with SqliteStore(db_path, clock=lambda: 111) as first:
        first.insert_sample(sample(1_000, **{"air.room": 26.0}))
    with SqliteStore(db_path, clock=lambda: 222) as second:
        query = "SELECT version, applied_ms FROM schema_version"
        rows = second.connection.execute(query).fetchall()
        assert [(row["version"], row["applied_ms"]) for row in rows] == [(1, 111)]
        # 適用済みの DB を開き直してもデータは残る
        assert second.latest(at_ms=1_000)["air.room"].value == 26.0


def test_wal_and_synchronous_are_enabled(store):
    """WAL でなければ読み出しが取り込みを止める。ファイル DB でのみ確認できる。"""
    assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_database_newer_than_code_is_refused(db_path, store):
    store.connection.execute("INSERT INTO schema_version VALUES (999, 0)")
    store.close()
    with pytest.raises(MigrationError, match="新しい"):
        SqliteStore(db_path)


def test_failed_migration_leaves_no_partial_schema(tmp_path):
    """途中で失敗したら丸ごと巻き戻す。中途半端なスキーマは手作業でしか直せない。"""
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_broken.sql").write_text(
        "CREATE TABLE first_half (x); CREATE TABLE first_half (x);", encoding="utf-8"
    )
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError):
            mig.apply_pending(conn, now_ms=0, directory=directory)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        assert tables == []
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("filenames", "match"),
    [
        (["0001_a.sql", "0003_c.sql"], "連番"),
        (["0002_a.sql"], "連番"),
        (["initial.sql"], "命名"),
        (["0001_Upper.sql"], "命名"),
    ],
)
def test_broken_migration_sets_are_rejected(tmp_path, filenames, match):
    directory = tmp_path / "migrations"
    directory.mkdir()
    for name in filenames:
        (directory / name).write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match=match):
        mig.discover(directory)


# ---------------------------------------------------------------- 書き込みの不変条件


def test_check_constraint_rejects_derived_metrics(store):
    """アプリを通さない経路でも派生値は入らない（決定記録 0002 §2.2）。"""
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("INSERT INTO readings VALUES ('d.intake_rise', 1000, 1.4, 'ok')")


def test_check_constraint_rejects_unknown_quality(store):
    """`quality` の綴り違いを書き込み時に落とす（決定記録 0002 §2.3）。"""
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("INSERT INTO readings VALUES ('air.room', 1000, 26.0, 'okay')")


def test_duplicate_timestamp_is_rejected(store):
    """同じ `(metric, ts_ms)` を黙って上書きしない。上書きすると観測が1つ消える。"""
    store.insert_sample(sample(1_000, **{"air.room": 26.0}))
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_sample(sample(1_000, **{"air.room": 26.5}))


def test_failed_insert_writes_nothing(store):
    """サンプル単位で原子的。半分だけ書かれた時刻があると横串が壊れる。"""
    store.insert_sample(sample(1_000, **{"air.room": 26.0}))
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_sample(sample(1_000, **{"air.gpu_intake": 28.0, "air.room": 26.5}))
    assert store.series("air.gpu_intake", 0, 2_000) == ()


def test_insert_empty_sample_is_a_noop(store):
    assert store.insert_samples([]) == 0


# ---------------------------------------------------------------- 受入基準


def test_ten_thousand_samples_and_range_query(store):
    """受入基準: 1万サンプルの投入と範囲クエリ。"""
    count = 10_000
    samples = [
        sample(
            ts_ms=i * INTERVAL_MS,
            **{channel: 20.0 + (i % 100) / 10 for channel in CHANNELS},
        )
        for i in range(count)
    ]
    assert store.insert_samples(samples) == count * len(CHANNELS)

    # 100サンプル分だけを切り出す。範囲は [start, end) なので end 自身は入らない
    start = 1_000 * INTERVAL_MS
    end = 1_100 * INTERVAL_MS
    points = store.series("air.gpu_intake", start, end)
    assert len(points) == 100
    assert points[0].ts_ms == start
    assert points[-1].ts_ms == end - INTERVAL_MS
    assert [point.ts_ms for point in points] == sorted(point.ts_ms for point in points)

    stats = store.stats("air.gpu_intake", start, end)
    assert stats.row_count == 100
    assert stats.ok_value_count == 100
    assert stats.min_value == pytest.approx(20.0)
    assert stats.max_value == pytest.approx(29.9)


# ---------------------------------------------------------------- latest


def test_latest_returns_the_newest_row_per_metric(store):
    store.insert_samples(
        [
            sample(1_000, **{"air.room": 26.0, "air.gpu_intake": 28.0}),
            sample(2_000, **{"air.room": 26.5}),
        ]
    )
    latest = store.latest(at_ms=2_000)
    assert latest["air.room"].ts_ms == 2_000
    assert latest["air.room"].value == 26.5
    assert latest["air.gpu_intake"].ts_ms == 1_000


def test_latest_marks_old_values_stale(db_path):
    """要件 §5.3「前サンプルから10秒以上更新なし」。境界を含む。"""
    with SqliteStore(db_path, rules=QualityRules(stale_after_ms=10_000)) as store:
        store.insert_sample(sample(1_000, **{"air.room": 26.0}))

        assert store.latest(at_ms=1_000 + 9_999)["air.room"].quality is Quality.OK
        stale = store.latest(at_ms=1_000 + 10_000)["air.room"]
        assert stale.quality is Quality.STALE
        assert stale.value == 26.0, "古くても値は返す。判断材料を消さない"
        assert stale.age_ms == 10_000


def test_latest_uses_the_clock_when_no_time_is_given(db_path):
    with SqliteStore(db_path, clock=lambda: 60_000) as store:
        store.insert_sample(sample(1_000, **{"air.room": 26.0}))
        assert store.latest()["air.room"].quality is Quality.STALE


def test_latest_keeps_the_stored_quality_when_fresh(store):
    store.insert_sample(sample(1_000, **{"air.rear_exhaust": None}))
    assert store.latest(at_ms=1_500)["air.rear_exhaust"].quality is Quality.MISSING


def test_latest_on_empty_database(store):
    assert store.latest(at_ms=1_000) == {}


# ---------------------------------------------------------------- series


def test_series_range_is_half_open(store):
    store.insert_samples([sample(ts, **{"air.room": 26.0}) for ts in (999, 1_000, 1_999, 2_000)])
    points = store.series("air.room", 1_000, 2_000)
    assert [point.ts_ms for point in points] == [1_000, 1_999]


def test_series_keeps_quality(store):
    store.insert_sample(sample(1_000, **{"air.rear_exhaust": None}))
    assert store.series("air.rear_exhaust", 0, 2_000)[0].quality is Quality.MISSING


def test_series_limit_drops_the_oldest(store):
    """打ち切るなら古い側。直近が消えると監視の意味が無くなる。"""
    store.insert_samples([sample(ts * 1_000, **{"air.room": float(ts)}) for ts in range(10)])
    points = store.series("air.room", 0, 10_000, limit=3)
    assert [point.ts_ms for point in points] == [7_000, 8_000, 9_000]


def test_series_rejects_unknown_metric_names(store):
    with pytest.raises(ValueError, match="命名規約"):
        store.series("Air.Room", 0, 1_000)


def test_series_rejects_reversed_range(store):
    with pytest.raises(ValueError, match="逆転"):
        store.series("air.room", 2_000, 1_000)


# ---------------------------------------------------------------- rollup


def test_rollup_reads_minute_buckets(store):
    """バケットを書くのは #10。ここは読み出しの形だけを固定する。"""
    store.connection.executemany(
        "INSERT INTO readings_1m VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("air.room", 0, 25.0, 27.0, 26.0, 20, 24, 24),
            ("air.room", 60_000, None, None, None, 0, 3, 24),
        ],
    )
    points = store.rollup("air.room", 0, 120_000, Aggregation.MINUTE)
    assert [point.bucket_ms for point in points] == [0, 60_000]
    assert points[0].mean_value == 26.0
    # 24 期待して 20 が ok。届いた 24 行のうち 4 行が ok ではなかった
    assert points[0].missing_ratio == pytest.approx(1 - 20 / 24)
    # サンプル自体が 3 行しか届いていない。母数は期待値のまま
    assert points[1].missing_ratio == pytest.approx(1.0)


def test_rollup_missing_ratio_falls_back_to_row_count():
    """起動バナー未受信なら期待値が無い。行数を母数にした下限値になる。"""
    point = RollupPoint(
        bucket_ms=0,
        min_value=25.0,
        max_value=26.0,
        mean_value=25.5,
        ok_value_count=12,
        row_count=24,
        expected_count=None,
    )
    assert point.missing_ratio == pytest.approx(0.5)


def test_rollup_rejects_raw(store):
    with pytest.raises(ValueError, match="series"):
        store.rollup("air.room", 0, 1_000, Aggregation.RAW)


# ---------------------------------------------------------------- stats


def test_stats_ignores_non_ok_rows(store):
    """suspect を平均に混ぜない（決定記録 0002 §2.8）。"""
    store.insert_samples([sample(ts * 1_000, **{"air.room": 26.0}) for ts in range(3)])
    store.connection.execute("INSERT INTO readings VALUES ('air.room', 3000, 85.0, 'suspect')")
    store.connection.execute("INSERT INTO readings VALUES ('air.room', 4000, NULL, 'missing')")

    stats = store.stats("air.room", 0, 5_000)
    assert stats.row_count == 5
    assert stats.ok_value_count == 3
    assert stats.max_value == 26.0
    assert stats.mean_value == pytest.approx(26.0)
    assert stats.missing_ratio == pytest.approx(0.4)


def test_stats_p95_is_nearest_rank(store):
    """補間しない。測定していない値を統計に出さない。"""
    store.insert_samples([sample(ts * 1_000, **{"air.room": float(ts + 1)}) for ts in range(100)])
    assert store.stats("air.room", 0, 100_000).p95_value == pytest.approx(95.0)


def test_stats_slope_is_per_minute(store):
    """FR-407 が「°C/分」で閾値を持つため、傾きの単位を分にそろえる。"""
    store.insert_samples(
        [sample(minute * 60_000, **{"air.gpu_intake": 20.0 + minute}) for minute in range(10)]
    )
    stats = store.stats("air.gpu_intake", 0, 600_000)
    assert stats.slope_per_min == pytest.approx(1.0)


def test_stats_slope_needs_two_points(store):
    store.insert_sample(sample(0, **{"air.room": 26.0}))
    assert store.stats("air.room", 0, 1_000).slope_per_min is None


def test_stats_on_empty_window(store):
    stats = store.stats("air.room", 0, 1_000)
    assert stats.row_count == 0
    assert stats.ok_value_count == 0
    assert stats.min_value is None
    assert stats.p95_value is None
    assert stats.missing_ratio is None


def test_slope_of_simultaneous_points_is_none():
    """時刻が同一の点しか無ければ傾きは定義できない。

    `readings` の主キーがこの状態を防ぐため公開経路からは起こらないが、
    ゼロ除算を式の側で止めていることを直接確かめる。
    """
    assert db._slope(n=2, sx=0.0, sy=3.0, sxy=0.0, sxx=0.0) is None


def test_rollup_missing_ratio_is_none_for_an_empty_bucket():
    """1行も無いバケットの欠測率は 0% でも 100% でもない。母数が無い。"""
    point = RollupPoint(
        bucket_ms=0,
        min_value=None,
        max_value=None,
        mean_value=None,
        ok_value_count=0,
        row_count=0,
        expected_count=None,
    )
    assert point.missing_ratio is None
