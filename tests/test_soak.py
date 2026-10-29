"""連続運転テスト（soak）の集計（#47）。

守る線は5つ。

1. **欠測率の母数は期間と送信周期から出す**（装置ごと沈黙した時間も欠測に入る）
2. **閾値は `config/soak.yaml` から読む**（AGENTS.md ルール9）
3. **分からないものを合格と書かない**（期間が終わっていない・周期が不明）
4. **DB に書かない**（行・スキーマ・journal_mode のどれも変えない。マイグレーションも当てない）
5. **RSS は採取の CSV（`--rss-csv`）を渡したときだけ判定する**（決定記録 0173 §2.7）

DB は MockSource / ReplaySource を取り込みデーモンに通して作る。RSS の CSV は合成した値。
"""

import dataclasses
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.daemon import Daemon
from coldaisle.ingest import MockSource, Normalizer
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.replay import ReplaySource
from coldaisle.soak import (
    NOT_COVERED,
    REFERENCE_ONLY,
    RSS_CSV_COLUMNS,
    RssCsvError,
    RssSample,
    SoakConfig,
    SoakDatabase,
    SoakDatabaseError,
    SoakReport,
    Verdict,
    build,
    main,
    read_rss_csv,
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
    assert not report.replay_in_window
    assert report.db_verdict is Verdict.PASS
    # RSS と再接続回数を確かめていないので、**総合は合格にしない**
    assert report.verdict is Verdict.UNKNOWN
    assert report.as_dict()["verdict"] == "unknown"
    assert report.as_dict()["db_verdict"] == "pass"


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


def test_a_restart_without_periodic_data_still_fails(tmp_path, rules, scenarios, config):
    """再起動したまま送信が戻らなかった期間。**超過が分かっていれば判定不能にしない。**"""
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "ramp")
    later = TEST_EPOCH_MS + 10 * TEN_MINUTES_MS
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, ?)",
            ("sys.device_restarts", later - TEN_MINUTES_MS + 1_000, 1.0, "ok"),
        )

    with SoakDatabase(db) as store:
        report = build(
            store,
            start_ms=later - TEN_MINUTES_MS,
            end_ms=later,
            now_ms=later,
            config=config,
        )

    assert not report.has_data
    assert report.device_restarts == 1
    assert report.checks[0].verdict is Verdict.UNKNOWN
    assert report.checks[1].verdict is Verdict.FAIL
    assert report.verdict is Verdict.FAIL


def test_zero_restarts_without_periodic_data_is_not_a_pass(tmp_path, rules, scenarios, config):
    """0件でも、周期データが無ければ期間を観測できていたか分からない。"""
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

    assert report.checks[1].verdict is Verdict.UNKNOWN


def test_threshold_comes_from_the_config(tmp_path, rules, scenarios, config):
    """閾値を緩めれば同じ DB が合格になる。**コードに閾値が無い**ことの確認。"""
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "dropout")
    loose = config.model_copy(
        update={
            "thresholds": config.thresholds.model_copy(update={"missing_ratio_below": 0.06}),
        }
    )

    assert report_for(db, rules, loose).db_verdict is Verdict.PASS


# ---------------------------------------------------------------- Replay で作った DB


HEADER = (
    "timestamp,room_temp,room_humidity,front_intake,gpu_intake,gpu_exhaust,top_exhaust,rear_exhaust"
)
ROWS = """2026-08-24T00:00:00,24.4,56.2,24.12,24.94,23.56,23.75,23.94
2026-08-24T00:00:03,24.4,56.1,24.12,24.94,23.62,23.75,23.94
2026-08-24T00:00:06,,,24.19,25.00,23.62,23.81,24.00
2026-08-24T00:00:09,24.5,56.0,24.19,25.00,23.69,23.81,24.00
"""


def ingest_replay(db: Path, rules, config: SoakConfig, tmp_path: Path) -> None:
    csv = tmp_path / "sensors_2026-08-24.csv"
    csv.write_text(f"{HEADER}\n{ROWS}", encoding="utf-8")
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


def test_replayed_blank_cells_are_missing(tmp_path, rules, config):
    db = tmp_path / "replay.db"
    ingest_replay(db, rules, config, tmp_path)
    start_ms, end_ms = window("2026-08-24T00:00:00", "2026-08-24T00:00:12", config)

    with SoakDatabase(db) as store:
        report = build(store, start_ms=start_ms, end_ms=end_ms, now_ms=end_ms, config=config)

    room = next(line for line in report.metrics if line.metric == "air.room")
    front = next(line for line in report.metrics if line.metric == "air.front_intake")
    assert report.interval_ms == 3_000
    assert (room.expected, room.ok, room.missing) == (4, 3, 1)
    assert room.missing_ratio == pytest.approx(0.25)
    assert front.missing_ratio == 0.0


def test_replayed_zero_restarts_are_not_a_pass(tmp_path, rules, config):
    """再生の seq / up は合成値。**再起動 0 件は観測の結果ではない**ので判定不能。"""
    db = tmp_path / "replay.db"
    ingest_replay(db, rules, config, tmp_path)
    # 欠測の判定に引きずられないよう閾値を緩める（再起動の判定だけを見る）
    loose = config.model_copy(
        update={"thresholds": config.thresholds.model_copy(update={"missing_ratio_below": 0.5})}
    )
    start_ms, end_ms = window("2026-08-24T00:00:00", "2026-08-24T00:00:12", config)

    with SoakDatabase(db) as store:
        report = build(store, start_ms=start_ms, end_ms=end_ms, now_ms=end_ms, config=loose)

    assert report.replay_in_window
    assert (report.device_restarts, report.dropped_samples) == (0, 0)
    assert report.checks[0].verdict is Verdict.PASS
    assert report.checks[1].verdict is Verdict.UNKNOWN
    assert "再生" in report.checks[1].note
    assert report.db_verdict is Verdict.UNKNOWN
    assert report.as_dict()["replay_in_window"] is True
    assert "再生（replay）のデータを含む" in report.as_markdown()


def test_a_recorded_restart_still_fails_with_replay_data(tmp_path, rules, config):
    """再生は再起動を記録しない。**記録された超過は実機のもの**なので不合格のまま。"""
    db = tmp_path / "replay.db"
    ingest_replay(db, rules, config, tmp_path)
    start_ms, end_ms = window("2026-08-24T00:00:00", "2026-08-24T00:00:12", config)
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, ?)",
            ("sys.device_restarts", start_ms + 1_500, 1.0, "ok"),
        )

    with SoakDatabase(db) as store:
        report = build(store, start_ms=start_ms, end_ms=end_ms, now_ms=end_ms, config=config)

    assert report.replay_in_window
    assert report.checks[1].verdict is Verdict.FAIL
    assert report.verdict is Verdict.FAIL


def test_a_replay_after_the_window_does_not_mark_it(tmp_path, rules, config):
    """期間より後に始まった再生は、期間の行を持たない。"""
    db = tmp_path / "replay.db"
    ingest_replay(db, rules, config, tmp_path)
    start_ms, _ = window("2026-08-24T00:00:00", "2026-08-24T00:00:12", config)

    with SoakDatabase(db) as store:
        report = build(
            store,
            start_ms=start_ms - TEN_MINUTES_MS,
            end_ms=start_ms,
            now_ms=start_ms,
            config=config,
        )

    assert not report.replay_in_window


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
    assert printed["db_verdict"] == "fail"
    assert printed["counters"]["sys.dropped_samples"] == 12
    assert printed["not_covered"], "判定できない項目を書く"
    assert printed["rss"] is None
    assert printed["reference"] == list(REFERENCE_ONLY)
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


# ---------------------------------------------------------------- RSS（決定記録 0173）

ROOT = Path(__file__).resolve().parents[1]
SAMPLER = ROOT / "deploy" / "soak" / "rss-sample.sh"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MINUTE_MS = 60_000
INGEST = "coldaisle-daemon"
BASE_KIB = 55_000


def rss_rows(
    kib: Callable[[int], float],
    *,
    unit: str = INGEST,
    pid: Callable[[int], int | None] = lambda _: 4242,
    start_ms: int = TEST_EPOCH_MS,
    end_ms: int = TEST_EPOCH_MS + DAY_MS,
    step_ms: int = MINUTE_MS,
    keep: Callable[[int], bool] = lambda _: True,
) -> list[RssSample]:
    """1 分ごとの合成の行。`kib` / `pid` / `keep` は期間の始まりからの経過 ms を受ける。"""
    rows: list[RssSample] = []
    for at in range(start_ms, end_ms, step_ms):
        elapsed = at - start_ms
        if not keep(elapsed):
            continue
        main_pid = pid(elapsed)
        anon = None if main_pid is None else round(kib(elapsed))
        rows.append(RssSample(at, unit, main_pid, anon, anon, 20_000, None, None, None))
    return rows


def write_rss_csv(path: Path, rows: list[RssSample]) -> Path:
    def cell(value: object) -> str:
        return "" if value is None else str(value)

    lines = [",".join(RSS_CSV_COLUMNS)]
    lines += [",".join(cell(getattr(row, column)) for column in RSS_CSV_COLUMNS) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def empty_db(tmp_path, rules) -> Path:
    """周期のデータが無い DB。**RSS の項目だけを見る試験**に使う（DB の項目は判定不能になる）。"""
    db = tmp_path / "empty.db"
    SqliteStore(db, rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)).close()
    return db


def rss_report(
    db: Path,
    tmp_path: Path,
    config: SoakConfig,
    rows: list[RssSample],
    *,
    now_ms: int | None = None,
) -> SoakReport:
    """24 時間の期間で、CSV に書いてから読み直した行で集計する（CSV の読み手も通す）。"""
    samples = read_rss_csv(write_rss_csv(tmp_path / "rss.csv", rows))
    end_ms = TEST_EPOCH_MS + DAY_MS
    with SoakDatabase(db) as store:
        return build(
            store,
            start_ms=TEST_EPOCH_MS,
            end_ms=end_ms,
            now_ms=end_ms if now_ms is None else now_ms,
            config=config,
            rss_samples=samples,
        )


def rss_checks(report: SoakReport):
    rss_check, restart_check = report.rss_checks
    assert rss_check.name.startswith("取り込みのデーモンの RSS")
    assert restart_check.name == "取り込みのプロセスの再起動"
    return rss_check, restart_check


def test_rss_values_come_from_the_config(config):
    assert config.rss.ingest_unit == INGEST
    assert config.rss.warmup_hours == 2
    assert config.rss.max_slope_kib_per_hour == 256
    assert config.rss.max_range_mib == 16
    assert config.rss.sample_interval_s == 60
    assert config.rss.min_coverage == 0.95
    assert config.rss.max_gap_minutes == 10
    assert config.rss.max_process_restarts == 0


def test_flat_rss_passes(empty_db, tmp_path, config):
    report = rss_report(empty_db, tmp_path, config, rss_rows(lambda _: BASE_KIB))

    rss_check, restart_check = rss_checks(report)
    assert rss_check.verdict is Verdict.PASS
    assert restart_check.verdict is Verdict.PASS
    assert report.rss is not None
    assert report.rss.rows == 22 * 60
    assert report.rss.expected == 22 * 60
    assert report.rss.slope_kib_per_hour == pytest.approx(0.0)
    assert report.not_covered == ()
    # DB の項目（周期データが無い）は判定不能のまま。db_verdict に CSV の項目を入れない
    assert report.db_verdict is Verdict.UNKNOWN


def test_steady_growth_above_the_slope_fails(empty_db, tmp_path, config):
    """300 KiB/h（> 256）。22 時間で 6.4 MiB なので範囲（16 MiB）は満たす。**傾きだけで落ちる。**"""
    report = rss_report(
        empty_db, tmp_path, config, rss_rows(lambda ms: BASE_KIB + 300 * ms / HOUR_MS)
    )

    rss_check, _ = rss_checks(report)
    assert rss_check.verdict is Verdict.FAIL
    assert rss_check.observed == pytest.approx(300, abs=1)
    assert report.verdict is Verdict.FAIL


def test_growth_just_below_the_slope_passes(empty_db, tmp_path, config):
    report = rss_report(
        empty_db, tmp_path, config, rss_rows(lambda ms: BASE_KIB + 200 * ms / HOUR_MS)
    )

    assert rss_checks(report)[0].verdict is Verdict.PASS


def test_a_tall_spike_fails_on_the_range(empty_db, tmp_path, config):
    """区間の真ん中の 20 MiB の山。傾きはほぼ 0 だが、**範囲（16 MiB）で落ちる。**"""

    def spike(ms: int) -> float:
        return BASE_KIB + (20 * 1024 if 12 * HOUR_MS <= ms < 12 * HOUR_MS + 30 * MINUTE_MS else 0)

    report = rss_report(empty_db, tmp_path, config, rss_rows(spike))

    rss_check, _ = rss_checks(report)
    assert abs(rss_check.observed or 0) < 256
    assert report.rss is not None and report.rss.range_kib == 20 * 1024
    assert rss_check.verdict is Verdict.FAIL


def test_growth_only_during_the_warmup_passes(empty_db, tmp_path, config):
    """暖機の 2 時間で 30 MiB 増え、あとは平ら。**暖機は傾向に入れない。**"""

    def warmup(ms: int) -> float:
        return BASE_KIB + 30 * 1024 * min(ms, 2 * HOUR_MS) / (2 * HOUR_MS)

    report = rss_report(empty_db, tmp_path, config, rss_rows(warmup))

    rss_check, _ = rss_checks(report)
    assert rss_check.verdict is Verdict.PASS
    assert report.rss is not None
    assert report.rss.judge_start_ms == TEST_EPOCH_MS + 2 * HOUR_MS


def test_a_pid_change_in_the_judged_window(empty_db, tmp_path, config):
    """区間の途中で MainPID が変わる → RSS は判定不能、プロセスの再起動は不合格。"""
    rows = rss_rows(lambda _: BASE_KIB, pid=lambda ms: 4242 if ms < 12 * HOUR_MS else 4343)

    report = rss_report(empty_db, tmp_path, config, rows)

    rss_check, restart_check = rss_checks(report)
    assert rss_check.verdict is Verdict.UNKNOWN
    assert "MainPID" in rss_check.note
    assert restart_check.verdict is Verdict.FAIL
    assert restart_check.observed == 1
    assert report.verdict is Verdict.FAIL


def test_a_pid_without_a_readable_rss_still_counts_in_the_judged_window(empty_db, tmp_path, config):
    """区間の最初の行の PID は RSS が読めず、残りは別の PID で平ら → **それでも RSS は判定不能**。

    RSS の値が無い行を PID の一覧から落とすと、プロセスが1つに見えて合格になる（0173 §2.3）。
    """
    judge_start = 2 * HOUR_MS
    rows = [
        sample
        if sample.ts_ms != TEST_EPOCH_MS + judge_start
        else dataclasses.replace(sample, rss_anon_kb=None)
        for sample in rss_rows(
            lambda _: BASE_KIB, pid=lambda ms: 4242 if ms <= judge_start else 4343
        )
    ]

    report = rss_report(empty_db, tmp_path, config, rows)

    rss_check, restart_check = rss_checks(report)
    assert report.rss is not None and report.rss.judged_pids == (4242, 4343)
    assert rss_check.verdict is Verdict.UNKNOWN
    assert restart_check.verdict is Verdict.FAIL


def test_a_pid_change_during_the_warmup_still_counts_as_a_restart(empty_db, tmp_path, config):
    """再起動は**暖機を含む期間**で数える（0173 §2.4）。RSS は区間の1つのプロセスで判定できる。"""
    rows = rss_rows(lambda _: BASE_KIB, pid=lambda ms: 4242 if ms < HOUR_MS else 4343)

    report = rss_report(empty_db, tmp_path, config, rows)

    rss_check, restart_check = rss_checks(report)
    assert rss_check.verdict is Verdict.PASS
    assert restart_check.verdict is Verdict.FAIL
    assert report.rss is not None and report.rss.window_pids == (4242, 4343)


def test_too_few_rows_is_unknown(empty_db, tmp_path, config):
    """10 行に 1 行が欠ける（90%）。**95% 未満は判定不能**（平らな値でも合格にしない）。"""
    rows = rss_rows(lambda _: BASE_KIB, keep=lambda ms: (ms // MINUTE_MS) % 10 != 0)

    report = rss_report(empty_db, tmp_path, config, rows)

    rss_check, _ = rss_checks(report)
    assert rss_check.verdict is Verdict.UNKNOWN
    assert "95%" in rss_check.note


def test_a_long_gap_is_unknown(empty_db, tmp_path, config):
    """15 分の隙間。行の数は 98% あるが、**10 分を超える隙間**で判定不能。"""
    rows = rss_rows(
        lambda _: BASE_KIB,
        keep=lambda ms: not 10 * HOUR_MS <= ms < 10 * HOUR_MS + 15 * MINUTE_MS,
    )

    report = rss_report(empty_db, tmp_path, config, rows)

    rss_check, restart_check = rss_checks(report)
    assert report.rss is not None and report.rss.coverage > 0.95
    assert rss_check.verdict is Verdict.UNKNOWN
    assert "10 分" in rss_check.note
    # 隙間の前後で MainPID が同じなら、再起動は無かった（変われば数えられる）
    assert restart_check.verdict is Verdict.PASS


def test_a_csv_that_stops_early_is_unknown(empty_db, tmp_path, config):
    """CSV が期間の終わりまで覆っていない（採取の止め忘れの逆。始め忘れも同じ）。"""
    rows = rss_rows(lambda _: BASE_KIB, end_ms=TEST_EPOCH_MS + 20 * HOUR_MS)

    report = rss_report(empty_db, tmp_path, config, rows)

    rss_check, restart_check = rss_checks(report)
    assert rss_check.verdict is Verdict.UNKNOWN
    assert restart_check.verdict is Verdict.UNKNOWN


def test_a_stopped_daemon_is_not_zero_restarts(empty_db, tmp_path, config):
    """MainPID が空の行（止まっていた）。戻らなければ MainPID は変わらないので、0 回と言わない。"""
    rows = rss_rows(lambda _: BASE_KIB, pid=lambda ms: None if ms >= 23 * HOUR_MS else 4242)

    report = rss_report(empty_db, tmp_path, config, rows)

    _, restart_check = rss_checks(report)
    assert restart_check.verdict is Verdict.UNKNOWN
    assert report.rss is not None and report.rss.stopped_rows == 60


def test_an_unfinished_window_is_not_judged_from_the_csv(empty_db, tmp_path, config):
    report = rss_report(
        empty_db,
        tmp_path,
        config,
        rss_rows(lambda _: BASE_KIB),
        now_ms=TEST_EPOCH_MS + 12 * HOUR_MS,
    )

    assert {check.verdict for check in report.rss_checks} == {Verdict.UNKNOWN}


def test_other_units_do_not_change_the_verdict(empty_db, tmp_path, config):
    """fand など参考の unit がどれだけ増えても、判定は取り込みの行だけで決まる。"""
    ingest = rss_rows(lambda _: BASE_KIB)
    growing = rss_rows(
        lambda ms: 40_000 + 10_000 * ms / HOUR_MS, unit="coldaisle-fand", pid=lambda _: 99
    )

    alone = rss_report(empty_db, tmp_path, config, ingest)
    mixed = rss_report(empty_db, tmp_path, config, ingest + growing)

    assert [check.verdict for check in mixed.rss_checks] == [
        check.verdict for check in alone.rss_checks
    ]
    assert rss_checks(mixed)[0].verdict is Verdict.PASS
    assert mixed.rss is not None
    fand = next(unit for unit in mixed.rss.units if unit.unit == "coldaisle-fand")
    assert fand.first is not None and fand.last is not None
    assert fand.last.rss_anon_kb - fand.first.rss_anon_kb > 200_000
    assert "coldaisle-fand" in mixed.as_markdown()


def test_without_rss_csv_nothing_changes(empty_db, config):
    """`--rss-csv` を渡さない → RSS は `NOT_COVERED`、再接続回数は参考の一覧（0173 §2.5）。"""
    end_ms = TEST_EPOCH_MS + DAY_MS
    with SoakDatabase(empty_db) as store:
        report = build(store, start_ms=TEST_EPOCH_MS, end_ms=end_ms, now_ms=end_ms, config=config)

    assert report.rss is None
    assert report.rss_checks == ()
    assert report.not_covered == NOT_COVERED
    assert all("RSS" in item for item in NOT_COVERED)
    assert not any("再接続" in item for item in NOT_COVERED)
    assert any("再接続" in item for item in REFERENCE_ONLY)
    markdown = report.as_markdown()
    assert "## 参考（判定に使わない）" in markdown
    assert "「この集計に無いもの」が未確認" in markdown


def test_everything_passing_gives_an_overall_pass(tmp_path, rules, scenarios, config):
    """DB の項目と CSV の項目がすべて合格 → `verdict` が合格（`NOT_COVERED` が空になる）。

    Mock の DB は 10 分なので、RSS の区間と間隔だけを縮めた設定で判定する。
    """
    db = tmp_path / "soak.db"
    ingest_mock(db, rules, scenarios, "ramp")
    short = config.model_copy(
        update={
            "rss": config.rss.model_copy(
                update={"warmup_hours": 0, "sample_interval_s": 10, "max_gap_minutes": 1}
            )
        }
    )
    end_ms = TEST_EPOCH_MS + TEN_MINUTES_MS
    rows = rss_rows(lambda _: BASE_KIB, end_ms=end_ms, step_ms=10_000)

    with SoakDatabase(db) as store:
        report = build(
            store,
            start_ms=TEST_EPOCH_MS,
            end_ms=end_ms,
            now_ms=end_ms,
            config=short,
            rss_samples=rows,
        )

    assert report.db_verdict is Verdict.PASS
    assert {check.verdict for check in report.rss_checks} == {Verdict.PASS}
    assert report.not_covered == ()
    assert report.verdict is Verdict.PASS
    document = report.as_dict()
    assert document["verdict"] == "pass"
    assert document["not_covered"] == []
    assert {check["source"] for check in document["checks"]} == {"db", "rss_csv"}
    assert "未確認" not in report.as_markdown()


def test_main_reads_the_rss_csv(tmp_path, empty_db, capsys):
    csv_path = write_rss_csv(tmp_path / "rss.csv", rss_rows(lambda _: BASE_KIB))

    code = main(
        [
            "--db",
            str(empty_db),
            "--config",
            str(CONFIG_DIR / "soak.yaml"),
            "--start",
            "2026-08-25T00:00:00+00:00",  # TEST_EPOCH_MS
            "--rss-csv",
            str(csv_path),
            "--no-write",
            "--print",
            "json",
        ],
        clock=SimulatedClock(TEST_EPOCH_MS + 2 * DAY_MS),
    )

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["not_covered"] == []
    assert printed["rss"]["rows"] == 22 * 60
    names = {check["name"]: check["verdict"] for check in printed["checks"]}
    assert names["取り込みのプロセスの再起動"] == "pass"


def test_main_refuses_a_malformed_rss_csv(tmp_path, empty_db):
    bad = tmp_path / "rss.csv"
    bad.write_text("ts_ms,unit,rss\n1,coldaisle-daemon,3\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        main(
            [
                "--db",
                str(empty_db),
                "--config",
                str(CONFIG_DIR / "soak.yaml"),
                "--start",
                "2026-08-25T00:00:00+00:00",
                "--rss-csv",
                str(bad),
                "--no-write",
            ]
        )


@pytest.mark.parametrize(
    "row",
    [
        "x,coldaisle-daemon,1,2,3,4,5,6,7",  # 時刻が数でない
        "1,coldaisle-daemon,1,2,3",  # 列が足りない
        "1,,1,2,3,4,5,6,7",  # unit が空
        "1,coldaisle-daemon,1,-2,3,4,5,6,7",  # 負の値
    ],
)
def test_malformed_rss_rows_are_refused(tmp_path, row):
    path = tmp_path / "rss.csv"
    path.write_text(",".join(RSS_CSV_COLUMNS) + "\n" + row + "\n", encoding="utf-8")

    with pytest.raises(RssCsvError):
        read_rss_csv(path)


def test_blank_cells_are_none(tmp_path):
    path = tmp_path / "rss.csv"
    path.write_text(",".join(RSS_CSV_COLUMNS) + "\n1,coldaisle-daemon,,,,,,,\n", encoding="utf-8")

    (sample,) = read_rss_csv(path)

    assert sample == RssSample(1, "coldaisle-daemon", None, None, None, None, None, None, None)


# ---------------------------------------------------------------- 採取のスクリプト


def test_sampler_is_valid_bash():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash が無い")
    subprocess.run([bash, "-n", str(SAMPLER)], check=True)
    assert os.access(SAMPLER, os.X_OK), "timer から直接起動できるよう実行権限を付ける"


@pytest.mark.skipif(sys.platform != "linux", reason="GNU date（%3N）が要る")
def test_sampler_writes_the_columns_from_fake_proc_and_cgroup(tmp_path):
    """偽の `/proc` / cgroup / `systemctl` を渡す。**実機の `/proc` を読まない。**"""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash が無い")
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    (proc / "4242" / "status").write_text(
        "Name:\tcoldaisle-daemon\nVmRSS:\t   75396 kB\nRssAnon:\t   55204 kB\n"
        "RssFile:\t   20192 kB\nRssShmem:\t       0 kB\n",
        encoding="utf-8",
    )
    cgroup = tmp_path / "cgroup" / "system.slice" / "coldaisle-daemon.service"
    cgroup.mkdir(parents=True)
    (cgroup / "memory.current").write_text("122683392\n", encoding="utf-8")
    (cgroup / "memory.stat").write_text(
        "anon 56528896\nfile 63963136\nkernel 1048576\n", encoding="utf-8"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    # `systemctl show <unit> -p MainPID --value`。止まっている unit は 0 を返す
    systemctl.write_text(
        '#!/bin/sh\ncase "$2" in\n  coldaisle-daemon.service) echo 4242 ;;\n  *) echo 0 ;;\nesac\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "COLDAISLE_SOAK_PROC_ROOT": str(proc),
        "COLDAISLE_SOAK_CGROUP_ROOT": str(tmp_path / "cgroup"),
    }
    out = tmp_path / "rss.csv"

    for _ in range(2):
        subprocess.run(
            [bash, str(SAMPLER), str(out), "coldaisle-daemon", "coldaisle-fand"],
            check=True,
            env=env,
        )

    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(RSS_CSV_COLUMNS), "見出しは最初の1回だけ"
    assert len(lines) == 1 + 2 * 2
    ingest = lines[1].split(",")
    assert ingest[0].isdigit()
    assert ingest[1:] == [
        "coldaisle-daemon",
        "4242",
        "55204",
        "75396",
        "20192",
        "122683392",
        "56528896",
        "63963136",
    ]
    # 止まっている unit（MainPID 0）・cgroup の無い unit は空欄（推測で埋めない）
    assert lines[2].split(",")[1:] == ["coldaisle-fand", "", "", "", "", "", "", ""]
    # soak の集計がそのまま読める形
    samples = read_rss_csv(out)
    assert [sample.unit for sample in samples] == ["coldaisle-daemon", "coldaisle-fand"] * 2
    assert samples[0].rss_anon_kb == 55204


def test_sampler_refuses_a_unit_name_that_breaks_the_csv(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash が無い")
    out = tmp_path / "rss.csv"

    result = subprocess.run(
        [bash, str(SAMPLER), str(out), "bad,unit"], capture_output=True, check=False
    )

    assert result.returncode == 2
    assert not out.exists()
