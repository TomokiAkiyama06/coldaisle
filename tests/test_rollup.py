"""ロールアップと保持期間（#10）。

要点は3つ。**集計は `quality='ok'` の行だけ**（決定記録 0002 §2.8）、
**1分→1時間は件数で加重**、**集計前の生データを消さない**。
"""

from pathlib import Path

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.store import Aggregation, Quality, Reading, Sample, SqliteStore
from coldaisle.store.rollup import (
    DAY_MS,
    RetentionRules,
    apply_retention,
    main,
    rollup_hours,
    rollup_minutes,
    run,
    vacuum,
)
from conftest import QUALITY_RULES_PATH

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
RETENTION_PATH = QUALITY_RULES_PATH.parent / "retention.yaml"


@pytest.fixture
def rules_30d() -> RetentionRules:
    return RetentionRules.from_yaml(RETENTION_PATH)


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "rollup.db", rules=rules, clock=SimulatedClock(0)) as opened:
        yield opened


def write(store, metric: str, ts_ms: int, value: float | None, quality=Quality.OK) -> None:
    store.insert_sample(
        Sample(ts_ms=ts_ms, readings=(Reading(metric=metric, value=value, quality=quality),))
    )


def buckets(store, table: str) -> list[tuple]:
    return store.connection.execute(
        f"SELECT bucket_ms, min_value, max_value, mean_value, ok_value_count, row_count, "
        f"expected_count FROM {table} ORDER BY bucket_ms"
    ).fetchall()


# ---------------------------------------------------------------- 1分（FR-202）


def test_minute_bucket_aggregates_ok_values(store):
    for index, value in enumerate((25.0, 26.0, 27.0)):
        write(store, "air.room", index * 2_500, value)
    assert rollup_minutes(store) == 1

    bucket = buckets(store, "readings_1m")[0]
    assert bucket["bucket_ms"] == 0
    assert (bucket["min_value"], bucket["max_value"]) == (25.0, 27.0)
    assert bucket["mean_value"] == pytest.approx(26.0)
    assert bucket["ok_value_count"] == 3
    assert bucket["row_count"] == 3


def test_suspect_rows_are_counted_but_not_averaged(store):
    """`suspect` を平均に混ぜない。混ぜると人工物が統計に残り、

    生データを消したあとは取り除けない（決定記録 0002 §2.8）。
    品質の情報は `ok_value_count` と `row_count` の差として残る。
    """
    write(store, "air.rear_exhaust", 0, 26.0)
    write(store, "air.rear_exhaust", 2_500, 85.0, Quality.SUSPECT)
    write(store, "air.rear_exhaust", 5_000, None, Quality.MISSING)
    rollup_minutes(store)

    bucket = buckets(store, "readings_1m")[0]
    assert bucket["max_value"] == 26.0, "85.0 が最大値になっていない"
    assert bucket["mean_value"] == pytest.approx(26.0)
    assert bucket["ok_value_count"] == 1
    assert bucket["row_count"] == 3


def test_empty_bucket_has_no_statistics(store):
    write(store, "air.room", 0, None, Quality.MISSING)
    rollup_minutes(store)
    bucket = buckets(store, "readings_1m")[0]
    assert bucket["min_value"] is None
    assert bucket["mean_value"] is None
    assert bucket["ok_value_count"] == 0
    assert bucket["row_count"] == 1


def test_expected_count_comes_from_the_hello(store, tmp_path):
    """起動バナーの `interval_ms` から出す（決定記録 0002 §2.8）。"""
    from coldaisle.store import DeviceRecord

    store.record_hello(DeviceRecord(device_id="dev", interval_ms=2_500), [], at_ms=0)
    write(store, "air.room", 0, 26.0)
    rollup_minutes(store)
    assert buckets(store, "readings_1m")[0]["expected_count"] == 24


def test_expected_count_is_null_without_a_hello(store):
    """起動バナーを受け取れていなければ不明。欠測率は下限値として扱う。"""
    write(store, "air.room", 0, 26.0)
    rollup_minutes(store)
    assert buckets(store, "readings_1m")[0]["expected_count"] is None


def test_partial_bucket_is_recomputed_on_the_next_run(store):
    """前回の最終バケットは必ず数え直す。**途中だった可能性がある。**"""
    write(store, "air.room", 0, 25.0)
    rollup_minutes(store)
    assert buckets(store, "readings_1m")[0]["ok_value_count"] == 1

    write(store, "air.room", 30_000, 27.0)  # 同じ分の後半に届いた
    rollup_minutes(store)
    bucket = buckets(store, "readings_1m")[0]
    assert bucket["ok_value_count"] == 2
    assert bucket["max_value"] == 27.0


def test_rollup_is_idempotent(store):
    for index in range(5):
        write(store, "air.room", index * 2_500, 26.0)
    rollup_minutes(store)
    before = buckets(store, "readings_1m")
    rollup_minutes(store)
    assert buckets(store, "readings_1m") == before


def test_rollup_of_an_empty_database_does_nothing(store):
    assert rollup_minutes(store) == 0
    assert rollup_hours(store) == 0


# ---------------------------------------------------------------- 1時間


def test_hour_mean_is_weighted_by_sample_count(store):
    """**単純平均にしない。** 欠測のあるバケットが同じ重みで効くと値が狂う。

    決定記録 0004 §2.9 の例をそのまま検算する。
    24件×27.0 + 24件×27.0 + 3件×35.0 の平均は 29.67 ではなく 27.47。
    """
    for minute, (value, count) in enumerate(((27.0, 24), (27.0, 24), (35.0, 3))):
        for index in range(count):
            write(store, "air.gpu_exhaust", minute * MINUTE_MS + index * 2_500, value)
    rollup_minutes(store)
    assert rollup_hours(store) == 1

    bucket = buckets(store, "readings_1h")[0]
    assert bucket["mean_value"] == pytest.approx((27 * 24 + 27 * 24 + 35 * 3) / 51)
    assert bucket["mean_value"] != pytest.approx((27.0 + 27.0 + 35.0) / 3)
    assert bucket["ok_value_count"] == 51
    assert (bucket["min_value"], bucket["max_value"]) == (27.0, 35.0)


def test_hour_expected_count_is_null_if_any_minute_is_unknown(store):
    """1分のどれかが不明なら1時間も不明（決定記録 0002 §2.8）。"""
    from coldaisle.store import DeviceRecord

    write(store, "air.room", 0, 26.0)
    write(store, "air.room", MINUTE_MS, 26.0)
    rollup_minutes(store)  # 起動バナー前。両方 NULL

    store.record_hello(DeviceRecord(device_id="dev", interval_ms=2_500), [], at_ms=0)
    write(store, "air.room", 2 * MINUTE_MS, 26.0)
    rollup_minutes(store)
    rollup_hours(store)

    minutes = buckets(store, "readings_1m")
    assert [bucket["expected_count"] for bucket in minutes] == [None, 24, 24], (
        "数え直すのは前回の最終バケットまで。それより古い期待値は当時のまま残す"
    )
    assert buckets(store, "readings_1h")[0]["expected_count"] is None


def test_five_minute_series_is_synthesised_from_minutes(store):
    """表を持たず読み出しのたびに合成する（決定記録 0004 §2.9）。"""
    for minute in range(10):
        for index in range(4):
            write(store, "air.room", minute * MINUTE_MS + index * 2_500, 20.0 + minute)
    rollup_minutes(store)

    points = store.rollup("air.room", 0, 10 * MINUTE_MS, Aggregation.FIVE_MINUTES)
    assert [point.bucket_ms for point in points] == [0, 5 * MINUTE_MS]
    assert points[0].mean_value == pytest.approx(22.0)  # 20..24 の平均
    assert points[0].ok_value_count == 20
    assert points[1].min_value == 25.0


def test_five_minute_and_hour_use_the_same_weighting(store):
    """同じ式であることを、値を突き合わせて確かめる。"""
    for minute in range(60):
        count = 24 if minute % 2 == 0 else 3
        for index in range(count):
            write(store, "air.room", minute * MINUTE_MS + index * 2_500, float(minute))
    rollup_minutes(store)
    rollup_hours(store)

    five = store.rollup("air.room", 0, HOUR_MS, Aggregation.FIVE_MINUTES)
    hour = store.rollup("air.room", 0, HOUR_MS, Aggregation.HOUR)[0]
    weighted = sum(p.mean_value * p.ok_value_count for p in five) / sum(
        p.ok_value_count for p in five
    )
    assert hour.mean_value == pytest.approx(weighted)


# ---------------------------------------------------------------- 保持（FR-203）


def test_old_raw_rows_are_deleted(store, rules_30d):
    now = 40 * DAY_MS
    write(store, "air.room", 1 * DAY_MS, 26.0)
    write(store, "air.room", 39 * DAY_MS, 26.5)
    rollup_minutes(store)

    deleted, cutoff = apply_retention(store, rules_30d, now_ms=now)
    assert deleted == 1
    assert cutoff == now - 30 * DAY_MS
    assert [point.ts_ms for point in store.series("air.room", 0, 50 * DAY_MS)] == [39 * DAY_MS]


def test_rollups_are_kept_forever(store, rules_30d):
    """FR-204。生を消してもロールアップは残る。"""
    write(store, "air.room", 1 * DAY_MS, 26.0)
    write(store, "air.room", 2 * DAY_MS, 26.5)
    rollup_minutes(store)
    rollup_hours(store)
    apply_retention(store, rules_30d, now_ms=40 * DAY_MS)

    remaining = [point.ts_ms for point in store.series("air.room", 0, 50 * DAY_MS)]
    assert remaining == [2 * DAY_MS], "最終バケットの行だけが残る"
    assert len(buckets(store, "readings_1m")) == 2
    assert len(buckets(store, "readings_1h")) == 2
    assert buckets(store, "readings_1m")[0]["mean_value"] == 26.0, "消した区間の集計は残る"


def test_nothing_is_deleted_before_the_rollup_has_run(store, rules_30d):
    """**集計していない生データを消さない。** ジョブの順序を間違えたときの安全弁。"""
    write(store, "air.room", 1 * DAY_MS, 26.0)
    deleted, _ = apply_retention(store, rules_30d, now_ms=40 * DAY_MS)
    assert deleted == 0
    assert len(store.series("air.room", 0, 50 * DAY_MS)) == 1


def test_deletion_stops_at_the_rolled_up_watermark(store, rules_30d):
    """ロールアップ済みより新しい生データは、保持期間を過ぎていても残す。

    最終バケット自身も消さない。まだ埋まりきっていない可能性があるため。
    """
    write(store, "air.room", 1 * DAY_MS, 26.0)
    write(store, "air.room", 2 * DAY_MS, 26.5)
    rollup_minutes(store)
    write(store, "air.room", 3 * DAY_MS, 27.0)  # まだ集計していない

    deleted, cutoff = apply_retention(store, rules_30d, now_ms=40 * DAY_MS)
    assert deleted == 1
    assert cutoff == 2 * DAY_MS
    remaining = [point.ts_ms for point in store.series("air.room", 0, 50 * DAY_MS)]
    assert remaining == [2 * DAY_MS, 3 * DAY_MS]


def test_every_metric_is_swept(store, rules_30d):
    """メトリクスごとにループする（決定記録 0002 §2.4）。1本も取り残さない。"""
    for metric in ("air.room", "air.gpu_intake", "sys.dropped_samples"):
        write(store, metric, 1 * DAY_MS, 1.0)
        write(store, metric, 2 * DAY_MS, 1.0)
    rollup_minutes(store)
    deleted, _ = apply_retention(store, rules_30d, now_ms=40 * DAY_MS)
    assert deleted == 3


# ---------------------------------------------------------------- 設定と CLI


def test_config_file_covers_every_field(rules_30d):
    import yaml

    loaded = yaml.safe_load(RETENTION_PATH.read_text(encoding="utf-8"))
    assert set(loaded) == set(RetentionRules.model_fields)
    assert rules_30d.raw_days == 30, "決定記録 0001 D-02"


def test_unknown_key_is_rejected(tmp_path):
    path = tmp_path / "retention.yaml"
    path.write_text("raw_days: 30\ncsv_dir: x\nraw_day: 14\n", encoding="utf-8")
    with pytest.raises(ValueError, match="raw_day"):
        RetentionRules.from_yaml(path)


def test_non_mapping_file_is_rejected(tmp_path):
    path = tmp_path / "retention.yaml"
    path.write_text("- 30\n", encoding="utf-8")
    with pytest.raises(ValueError, match="辞書ではない"):
        RetentionRules.from_yaml(path)


def test_run_reports_what_it_did(store, rules_30d):
    for index in range(3):
        write(store, "air.room", index * 2_500, 26.0)
    result = run(store, rules_30d, now_ms=DAY_MS)
    assert result.minute_buckets == 1
    assert result.hour_buckets == 1
    assert result.deleted_rows == 0


def test_vacuum_keeps_the_data(store, rules_30d):
    write(store, "air.room", 0, 26.0)
    vacuum(store)
    assert len(store.series("air.room", 0, MINUTE_MS)) == 1


def test_cli_runs_and_writes_csv(tmp_path, rules):
    database = tmp_path / "cli.db"
    with SqliteStore(database, rules=rules, clock=SimulatedClock(0)) as store:
        write(store, "air.room", 1_787_616_000_000, 26.0)  # 2026-08-25 09:00 JST

    retention = tmp_path / "retention.yaml"
    retention.write_text(f"raw_days: 30\ncsv_dir: {tmp_path / 'csv'}\n", encoding="utf-8")
    code = main(
        [
            f"--db={database}",
            f"--retention={retention}",
            f"--quality-rules={QUALITY_RULES_PATH}",
            "--export-day=2026-08-25",
            "--vacuum",
        ]
    )
    assert code == 0
    assert (tmp_path / "csv" / "sensors_2026-08-25.csv").exists()
    assert Path(database).exists()


# ---------------------------------------------------------------- 受入基準


def _fill_days(store, days: float, metrics: tuple[str, ...]) -> int:
    """2.5秒周期の生データを流し込む。返すのは行数。"""
    interval = 2_500
    samples = int(days * 24 * 60 * 60 * 1000 / interval)
    rows = [
        (metric, index * interval, 20.0 + (index % 100) / 10, "ok")
        for index in range(samples)
        for metric in metrics
    ]
    with store.transaction():
        store.connection.executemany(
            "INSERT INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, ?)", rows
        )
    return len(rows)


@pytest.mark.slow
def test_storage_per_row_stays_within_the_estimate(store, tmp_path):
    """受入基準: 30日分を入れてもDBサイズが想定内に収まる。

    30日ぶん（約730万行）を実際に入れるとテストが数分になるため、
    **1行あたりのバイト数**を実測して外挿する。決定記録 0002 §3 の見積りは
    1行 約40バイト・30日で約290MB。
    """
    metrics = ("air.room", "air.room_humidity", "air.front_intake")
    rows = _fill_days(store, days=0.5, metrics=metrics)
    store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = (tmp_path / "rollup.db").stat().st_size
    per_row = size / rows

    assert per_row < 60, f"1行 {per_row:.1f} バイト。見積り（約40）から外れている"
    thirty_days = per_row * 241_920 * 30  # 7メトリクス × 30日（決定記録 0002 §3）
    assert thirty_days < 1_000_000_000, f"30日で {thirty_days / 1e6:.0f}MB"


@pytest.mark.slow
def test_minute_query_does_far_less_work_than_raw(store):
    """受入基準: `agg=1m` のクエリが raw より明確に速い。

    実時間の計測は実行環境で揺れるため、**SQLite の VM ステップ数**を数える。
    1分バケットは行数が 1/24 になるので、走査量もその桁で減る。
    """
    rows = _fill_days(store, days=1.0, metrics=("air.room",))
    rollup_minutes(store)
    window = (0, DAY_MS)

    def steps(call) -> int:
        counted = 0

        def handler() -> int:
            nonlocal counted
            counted += 1
            return 0

        store.connection.set_progress_handler(handler, 100)
        try:
            call()
        finally:
            store.connection.set_progress_handler(None, 0)
        return counted

    raw_steps = steps(lambda: store.series("air.room", *window))
    minute_steps = steps(lambda: store.rollup("air.room", *window, Aggregation.MINUTE))

    assert rows == 34_560
    assert minute_steps * 10 < raw_steps, f"raw={raw_steps} 1m={minute_steps}"
