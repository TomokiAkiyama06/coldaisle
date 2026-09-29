"""連続運転テスト（soak）の集計（#47）。

守る線は4つ。

1. **欠測率の母数は期間と送信周期から出す**（装置ごと沈黙した時間も欠測に入る）
2. **閾値は `config/soak.yaml` から読む**（AGENTS.md ルール9）
3. **分からないものを合格と書かない**（期間が終わっていない・周期が不明）
4. **DB に書かない**（行・スキーマ・journal_mode のどれも変えない。マイグレーションも当てない）

DB は MockSource / ReplaySource を取り込みデーモンに通して作る。
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.daemon import Daemon
from coldaisle.ingest import MockSource, Normalizer
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.replay import ReplaySource
from coldaisle.soak import (
    SoakConfig,
    SoakDatabase,
    SoakDatabaseError,
    SoakReport,
    Verdict,
    build,
    main,
    window,
)
from coldaisle.store import Quality, SqliteStore
from conftest import CONFIG_DIR, TEST_EPOCH_MS

INTERVAL_MS = 2_500
TEN_MINUTES_MS = 600_000
"""`config/scenarios.yaml` の既定の長さ。2.5秒周期で240サンプル。"""


def no_sleep(_: float) -> None:
    """待たない。"""


@pytest.fixture
def config() -> SoakConfig:
    """**本番と同じ `config/soak.yaml`** を読む。"""
    return SoakConfig.from_yaml(CONFIG_DIR / "soak.yaml")


def ingest_mock(db: Path, rules, scenarios, name: str) -> None:
    clock = SimulatedClock(TEST_EPOCH_MS)
    # 待たずに流す。**終わりのあるシナリオだけを使う**こと。10分ぶん（241通）は
    # 待ち行列（256）に収まるが、終わりの無い `idle` は取り込みを追い越して溢れる
    source = MockSource(scenarios[name], sleep=no_sleep, clock=clock)
    daemon = Daemon(
        source=source,
        store=SqliteStore(db, rules=rules, clock=clock),
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=clock),
    )
    try:
        daemon.run()
    finally:
        daemon.store.close()


def report_for(
    db: Path, rules, config: SoakConfig, *, end_ms: int = TEST_EPOCH_MS + TEN_MINUTES_MS
) -> SoakReport:
    with SoakDatabase(db) as store:
        return build(store, start_ms=TEST_EPOCH_MS, end_ms=end_ms, now_ms=end_ms, config=config)


# ---------------------------------------------------------------- Mock で作った DB


def test_clean_run_passes(tmp_path, rules, scenarios, config):
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "ramp")

    report = report_for(db, rules, config)

    assert report.interval_ms == INTERVAL_MS
    assert all(line.expected == 240 for line in report.metrics)
    assert all(line.missing_ratio == 0.0 for line in report.metrics)
    assert (report.dropped_samples, report.device_restarts, report.queue_drops) == (0, 0, 0)
    assert report.verdict is Verdict.PASS


def test_dropout_counts_as_missing_and_as_a_seq_gap(tmp_path, rules, scenarios, config):
    """30秒の途絶 = 12サンプル。**届かなかったサンプルが欠測率に入る。**"""
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "dropout")

    report = report_for(db, rules, config)

    assert report.dropped_samples == 12
    assert all(line.missing_ratio == pytest.approx(12 / 240) for line in report.metrics)
    missing = report.checks[0]
    assert missing.verdict is Verdict.FAIL
    assert report.verdict is Verdict.FAIL


def test_a_device_restart_fails_the_run(tmp_path, rules, scenarios, config):
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "reset")

    report = report_for(db, rules, config)

    assert report.device_restarts == 1
    restart = report.checks[1]
    assert restart.verdict is Verdict.FAIL
    assert report.verdict is Verdict.FAIL


def test_suspect_rows_are_counted_and_miss(tmp_path, rules, scenarios, config):
    """-127 の生値は `suspect`。**届いていても欠測に数える**（決定記録 0002 §2.8）。"""
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "sensor_fail_raw")

    report = report_for(db, rules, config)

    rear = next(line for line in report.metrics if line.metric == "air.rear_exhaust")
    assert rear.suspect > 0
    assert report.suspect_total == rear.suspect
    assert rear.missing_ratio == pytest.approx(rear.suspect / 240)
    assert report.checks[0].note == "air.rear_exhaust", "最も悪いチャネルで判定する"
    assert report.verdict is Verdict.FAIL


def test_an_unfinished_window_is_not_judged(tmp_path, rules, scenarios, config):
    """期間の途中で集計すると、残りの時間が欠測に見える。**判定しない。**"""
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "ramp")

    with SoakDatabase(db) as store:
        report = build(
            store,
            start_ms=TEST_EPOCH_MS,
            end_ms=TEST_EPOCH_MS + TEN_MINUTES_MS,
            now_ms=TEST_EPOCH_MS + 30_000,
            config=config,
        )

    assert not report.complete
    assert {check.verdict for check in report.checks} == {Verdict.UNKNOWN}
    assert "まだ終わっていない" in report.as_markdown()


def test_an_empty_window_is_not_a_pass(tmp_path, rules, scenarios, config):
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "ramp")
    later = TEST_EPOCH_MS + 10 * TEN_MINUTES_MS

    with SoakDatabase(db) as store:
        report = build(
            store,
            start_ms=later - TEN_MINUTES_MS,
            end_ms=later,
            now_ms=later,
            config=config,
        )

    assert not report.has_data
    assert report.verdict is Verdict.UNKNOWN


def test_threshold_comes_from_the_config(tmp_path, rules, scenarios, config):
    """閾値を緩めれば同じ DB が合格になる。**コードに閾値が無い**ことの確認。"""
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "dropout")
    loose = config.model_copy(
        update={
            "thresholds": config.thresholds.model_copy(update={"missing_ratio_below": 0.06}),
        }
    )

    assert report_for(db, rules, loose).verdict is Verdict.PASS


# ---------------------------------------------------------------- Replay で作った DB


HEADER = (
    "timestamp,room_temp,room_humidity,front_intake,gpu_intake,gpu_exhaust,top_exhaust,rear_exhaust"
)
ROWS = """2026-08-24T00:00:00,24.4,56.2,24.12,24.94,23.56,23.75,23.94
2026-08-24T00:00:03,24.4,56.1,24.12,24.94,23.62,23.75,23.94
2026-08-24T00:00:06,,,24.19,25.00,23.62,23.81,24.00
2026-08-24T00:00:09,24.5,56.0,24.19,25.00,23.69,23.81,24.00
"""


def test_replayed_blank_cells_are_missing(tmp_path, rules, config):
    csv = tmp_path / "sensors_2026-08-24.csv"
    csv.write_text(f"{HEADER}\n{ROWS}", encoding="utf-8")
    db = tmp_path / "replay.db"
    source = ReplaySource(csv, tz=config.zone, bulk=True, sleep=no_sleep)
    daemon = Daemon(
        source=source,
        store=SqliteStore(db, rules=rules, clock=source.clock),
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=source.clock),
    )
    try:
        daemon.run()
    finally:
        daemon.store.close()
    start_ms, end_ms = window("2026-08-24T00:00:00", "2026-08-24T00:00:12", config)

    with SoakDatabase(db) as store:
        report = build(store, start_ms=start_ms, end_ms=end_ms, now_ms=end_ms, config=config)

    room = next(line for line in report.metrics if line.metric == "air.room")
    front = next(line for line in report.metrics if line.metric == "air.front_intake")
    assert report.interval_ms == 3_000
    assert (room.expected, room.ok, room.missing) == (4, 3, 1)
    assert room.missing_ratio == pytest.approx(0.25)
    assert front.missing_ratio == 0.0


# ---------------------------------------------------------------- CLI


def test_main_writes_markdown_and_json_without_touching_the_db(
    tmp_path, rules, scenarios, config, capsys
):
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "dropout")
    before = db_fingerprint(db)
    out_dir = tmp_path / "out"
    soak_yaml = tmp_path / "soak.yaml"
    soak_yaml.write_text(
        (CONFIG_DIR / "soak.yaml")
        .read_text(encoding="utf-8")
        .replace('output_dir: "var/soak"', f'output_dir: "{out_dir}"'),
        encoding="utf-8",
    )
    start = "2026-08-25T00:00:00+00:00"  # TEST_EPOCH_MS
    end = "2026-08-25T00:10:00+00:00"

    code = main(
        [
            "--db",
            str(db),
            "--config",
            str(soak_yaml),
            "--start",
            start,
            "--end",
            end,
            "--print",
            "json",
        ],
        clock=SimulatedClock(TEST_EPOCH_MS + 2 * TEN_MINUTES_MS),
    )

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == "fail"
    assert printed["counters"]["sys.dropped_samples"] == 12
    assert printed["not_covered"], "判定できない項目を書く"
    written = sorted(path.name for path in out_dir.iterdir())
    assert written == ["soak-20260825T090000.json", "soak-20260825T090000.md"]
    markdown = (out_dir / "soak-20260825T090000.md").read_text(encoding="utf-8")
    assert "不合格" in markdown
    assert "RSS" in markdown
    assert db_fingerprint(db) == before


def test_main_refuses_a_missing_db(tmp_path):
    missing = tmp_path / "typo.db"
    with pytest.raises(SystemExit):
        main(
            [
                "--db",
                str(missing),
                "--config",
                str(CONFIG_DIR / "soak.yaml"),
                "--start",
                "2026-08-24T00:00",
            ]
        )
    assert not missing.exists(), "空の DB を作らない"


def test_end_defaults_to_the_configured_duration(config):
    start_ms, end_ms = window("2026-08-24T09:00", None, config)
    assert end_ms - start_ms == int(config.duration_hours * 3_600_000)


# ---------------------------------------------------------------- 読み取り専用で開く


def db_fingerprint(db: Path) -> tuple[object, ...]:
    """行・スキーマ・journal_mode・適用済みの版。**どれか1つでも変われば「書いた」。**"""
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        return (
            conn.execute("PRAGMA journal_mode").fetchone()[0],
            conn.execute("PRAGMA user_version").fetchone()[0],
            conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
            tuple(conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")),
            tuple(conn.execute("SELECT * FROM readings ORDER BY metric, ts_ms")),
            tuple(conn.execute("SELECT * FROM devices ORDER BY device_id")),
        )


def test_a_non_wal_db_keeps_its_journal_mode(tmp_path, rules, scenarios, config):
    """`SqliteStore` なら WAL へ切り替えてしまう。**集計はそれをしない。**"""
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "ramp")
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
    before = db_fingerprint(db)
    assert before[0] == "delete"

    report_for(db, rules, config)

    assert db_fingerprint(db) == before


def test_an_old_schema_is_refused_not_migrated(tmp_path):
    """古い版の DB を開いてもマイグレーションを当てない。**開かずに落とす。**"""
    db = tmp_path / "old.db"
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER, applied_ms INTEGER)")
        conn.execute("INSERT INTO schema_version VALUES (1, 0)")
    before = db_fingerprint_minimal(db)

    with pytest.raises(SoakDatabaseError, match="版が合わない"):
        SoakDatabase(db)

    assert db_fingerprint_minimal(db) == before


def db_fingerprint_minimal(db: Path) -> tuple[object, ...]:
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        return (
            conn.execute("PRAGMA journal_mode").fetchone()[0],
            tuple(conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")),
            tuple(conn.execute("SELECT * FROM schema_version")),
        )


def test_main_refuses_an_old_schema(tmp_path):
    db = tmp_path / "old.db"
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER, applied_ms INTEGER)")
    with pytest.raises(SystemExit):
        main(
            [
                "--db",
                str(db),
                "--config",
                str(CONFIG_DIR / "soak.yaml"),
                "--start",
                "2026-08-24T00:00",
            ]
        )


def test_quality_counts_boundaries(tmp_path, rules):
    """値の無い `ok` 行はどこにも数えない。範囲は `[start, end)` で end ちょうどは含まない。"""
    db = tmp_path / "counts.db"
    SqliteStore(db, rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)).close()
    metric = "air.room"
    start, end = TEST_EPOCH_MS, TEST_EPOCH_MS + 10_000
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.executemany(
            "INSERT INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, ?)",
            [
                (metric, start - 1, 24.0, "ok"),  # 範囲の前
                (metric, start, 24.0, "ok"),
                (metric, start + 1_000, None, "ok"),  # 値の無い ok
                (metric, start + 2_000, None, "missing"),
                (metric, start + 3_000, -127.0, "suspect"),
                (metric, start + 4_000, 24.1, "stale"),
                (metric, end - 1, 24.2, "ok"),
                (metric, end, 24.3, "ok"),  # end ちょうど
                ("air.front_intake", start + 5_000, 24.0, "ok"),  # 別のメトリクス
            ],
        )

    with SoakDatabase(db) as database:
        counts = database.quality_counts(metric, start, end)

    assert counts == {Quality.OK: 2, Quality.MISSING: 1, Quality.SUSPECT: 1, Quality.STALE: 1}


def test_an_interval_announced_after_the_window_is_not_judged(tmp_path, rules, scenarios, config):
    """周期は起動バナーのたびに上書きされる。**期間より後の値で合否を出さない。**"""
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "ramp")
    end_ms = TEST_EPOCH_MS + TEN_MINUTES_MS
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute("UPDATE devices SET interval_ms = 5000, last_hello_ms = ?", (end_ms + 1,))

    report = report_for(db, rules, config)

    assert report.interval_after_window
    assert report.checks[0].verdict is Verdict.UNKNOWN
    assert "期間より後" in report.as_markdown()
    assert report.as_dict()["interval_after_window"] is True
