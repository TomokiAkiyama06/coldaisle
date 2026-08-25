"""ReplaySource（#7）。

**時刻は CSV の値をそのまま使う。** 再生で「いま」にすると、当時の推移ではなく
「いま起きたこと」として保存され、バグ再現にも回帰テストにも使えなくなる。

CSV は**テストの中で書き出す。** `.gitignore` が `sensors_*.csv` を弾いており
（実機の記録を public リポジトリへ持ち込まないため。#41）、
フィクスチャとして置くとその守りに穴を開けることになる。
体裁は実ファイル（`~/server_sensor_logs/`）に合わせてある。
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coldaisle.ingest.protocol import RawHello, RawSample
from coldaisle.ingest.replay import ReplaySource, normalize_column
from coldaisle.store import Quality, SqliteStore
from coldaisle.store.rollup import rollup_minutes

JST = ZoneInfo("Asia/Tokyo")

HEADER = (
    "timestamp,room_temp,room_humidity,front_intake,gpu_intake,gpu_exhaust,top_exhaust,rear_exhaust"
)
DAY_24_ROWS = """2026-08-24T00:00:00,24.4,56.2,24.12,24.94,23.56,23.75,23.94
2026-08-24T00:00:03,24.4,56.1,24.12,24.94,23.62,23.75,23.94
2026-08-24T00:00:06,,,24.19,25.00,23.62,23.81,24.00
2026-08-24T00:00:09,24.5,56.0,24.19,25.00,23.69,23.81,24.00
"""
DAY_25_ROWS = """2026-08-25T00:00:00,25.0,55.0,24.50,25.30,24.00,24.10,24.30
2026-08-25T00:00:03,25.1,55.1,24.56,25.31,24.06,24.12,24.31
"""
ODD_HEADER = (
    "Time,Room,Humidity, front_intake ,gpu_intake,gpu_exhaust,top_exhaust,rear_exhaust,vrm_temp"
)
ODD_ROWS = """2026-08-24T10:00:00,26.0,50.0,27.0,28.0,29.0,27.5,27.2,55.0
not-a-timestamp,26.0,50.0,27.0,28.0,29.0,27.5,27.2,55.0
2026-08-24T10:00:03,26.1,50.1,27.1,28.1,29.1,27.6,27.3,55.1
"""


@pytest.fixture
def logs(tmp_path) -> Path:
    """実ファイルと同じ体裁のCSVを2日ぶん書き出したディレクトリ。"""
    directory = tmp_path / "server_sensor_logs"
    directory.mkdir()
    (directory / "sensors_2026-08-24.csv").write_text(f"{HEADER}\n{DAY_24_ROWS}", encoding="utf-8")
    (directory / "sensors_2026-08-25.csv").write_text(f"{HEADER}\n{DAY_25_ROWS}", encoding="utf-8")
    return directory


@pytest.fixture
def day_24(logs) -> Path:
    return logs / "sensors_2026-08-24.csv"


@pytest.fixture
def odd_columns(tmp_path) -> Path:
    path = tmp_path / "odd.csv"
    path.write_text(f"{ODD_HEADER}\n{ODD_ROWS}", encoding="utf-8")
    return path


def no_sleep(_: float) -> None:
    """待たない。速度の検証は専用のテストで行う。"""


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=JST).timestamp() * 1000)


def samples(source: ReplaySource) -> list[RawSample]:
    return [message for message in source.stream() if isinstance(message, RawSample)]


def source(path: Path, **kwargs) -> ReplaySource:
    return ReplaySource(path, tz=JST, sleep=no_sleep, **kwargs)


# ---------------------------------------------------------------- 読み取り


def test_rows_become_samples(day_24):
    produced = samples(source(day_24))
    assert len(produced) == 4
    assert produced[0].channels["room_temp"] == 24.4
    assert produced[0].channels["rear_exhaust"] == 23.94


def test_blank_cells_are_missing(day_24):
    """空欄は欠測。従来の出力では異常値も空欄になっていた（決定記録 0008 §2.8）。"""
    produced = samples(source(day_24))
    assert produced[2].channels["room_temp"] is None
    assert produced[2].channels["room_humidity"] is None
    assert produced[2].channels["front_intake"] == 24.19


def test_hello_comes_first_and_estimates_the_interval(day_24):
    """`interval_ms` は最初の2行の間隔から推定する（期待サンプル数の母数）。"""
    messages = list(source(day_24).stream())
    assert isinstance(messages[0], RawHello)
    assert messages[0].interval_ms == 3_000
    assert messages[0].dev == "csv-replay", "実機やモックと取り違えない"


def test_column_names_are_tolerated(odd_columns):
    """列名の揺れに耐える（#7）。知らない列は捨てて続ける。"""
    produced = samples(source(odd_columns))
    assert len(produced) == 2, "時刻として読めない行は捨てる"
    assert produced[0].channels["room_temp"] == 26.0, "Room → room_temp"
    assert produced[0].channels["room_humidity"] == 50.0
    assert produced[0].channels["front_intake"] == 27.0, "前後の空白を無視する"
    assert "vrm_temp" not in produced[0].channels, "知らない列は捨てる"


@pytest.mark.parametrize(
    ("given", "expected"),
    [("Timestamp", "timestamp"), (" Room ", "room_temp"), ("GPU-Intake", "gpu_intake")],
)
def test_normalize_column(given, expected):
    assert normalize_column(given) == expected


def test_a_directory_is_read_in_date_order(logs):
    produced = samples(source(logs))
    assert len(produced) == 6
    assert [sample.seq for sample in produced] == [0, 1, 2, 3, 4, 5]


def test_a_file_without_a_timestamp_column_is_refused(tmp_path):
    path = tmp_path / "sensors_bad.csv"
    path.write_text("room_temp,front_intake\n24.0,25.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="時刻の列"):
        ReplaySource(path, tz=JST)


def test_an_empty_file_is_refused(tmp_path):
    path = tmp_path / "sensors_empty.csv"
    path.write_text("timestamp,room_temp\n", encoding="utf-8")
    with pytest.raises(ValueError, match="読める行"):
        ReplaySource(path, tz=JST)


# ---------------------------------------------------------------- 時刻


def test_host_time_comes_from_the_csv(day_24):
    """**当時の時刻で保存する。** 「いま」にすると当時の推移が再現できない。"""
    replay = source(day_24)
    stamps = [
        replay.clock.now_ms() for message in replay.stream() if isinstance(message, RawSample)
    ]
    assert stamps[0] == ms("2026-08-24T00:00:00")
    assert stamps[-1] == ms("2026-08-24T00:00:09")


def test_timezone_is_taken_from_the_caller(day_24):
    """CSV にオフセットが無い（決定記録 0008 §2.8）。ホストの設定に依存させない。"""
    utc = ReplaySource(day_24, tz=ZoneInfo("UTC"), sleep=no_sleep)
    assert utc.clock.now_ms() == ms("2026-08-24T00:00:00") + 9 * 3_600_000


def test_synthesised_sequence_is_continuous(day_24):
    """`seq` / `up` は CSV に無いので合成する。

    連続した値にすることで、**再生では取りこぼし（FR-105）や再起動（FR-106）の
    検出が意味を持たない**ことを明示する。
    """
    produced = samples(source(day_24))
    assert [sample.seq for sample in produced] == [0, 1, 2, 3]
    assert [sample.up for sample in produced] == [1_200, 4_200, 7_200, 10_200]


# ---------------------------------------------------------------- 3つの流し方


def test_realtime_waits_for_the_gap_between_rows(day_24):
    waited: list[float] = []
    list(ReplaySource(day_24, tz=JST, sleep=waited.append).stream())
    assert waited == [3.0, 3.0, 3.0], "CSV の行間隔ぶん待つ"


def test_compressed_waits_less(day_24):
    waited: list[float] = []
    list(ReplaySource(day_24, tz=JST, speed=60.0, sleep=waited.append).stream())
    assert waited == [0.05, 0.05, 0.05]


def test_bulk_does_not_wait(day_24):
    waited: list[float] = []
    list(ReplaySource(day_24, tz=JST, bulk=True, sleep=waited.append).stream())
    assert waited == []


def test_speed_must_be_positive(day_24):
    with pytest.raises(ValueError, match="bulk"):
        ReplaySource(day_24, tz=JST, speed=0)


def test_every_mode_produces_the_same_samples(day_24):
    """速度は待ち時間にだけ効く。保存される内容は同じ。"""
    modes = [
        [sample.model_dump() for sample in samples(source(day_24))],
        [sample.model_dump() for sample in samples(source(day_24, speed=60.0))],
        [sample.model_dump() for sample in samples(source(day_24, bulk=True))],
    ]
    assert modes[0] == modes[1] == modes[2]


# ---------------------------------------------------------------- 取り込み経路との接続


def test_replayed_data_lands_in_the_store_with_its_original_time(tmp_path, rules, logs):
    """受入基準の土台: 投入すると**当時の推移**が保存される。

    ダッシュボードでの確認は #17。ここでは保存と読み出しまでを確かめる。
    """
    from coldaisle.ingest.calibration import Calibration
    from coldaisle.ingest.daemon import Daemon
    from coldaisle.ingest.normalize import Normalizer

    replay = source(logs, bulk=True)
    store = SqliteStore(tmp_path / "replay.db", rules=rules, clock=replay.clock)
    daemon = Daemon(
        source=replay,
        store=store,
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=replay.clock),
        source_name="replay",
    )
    try:
        stats = daemon.run()
        points = store.series("air.room", ms("2026-08-24T00:00:00"), ms("2026-08-26T00:00:00"))
        rollup_minutes(store)
        device = store.device("csv-replay")
        recorded_source = store.current_state("sys.ingest_source")
    finally:
        store.close()

    assert stats.samples == 6
    assert stats.discarded == 0
    assert [point.ts_ms for point in points][:2] == [
        ms("2026-08-24T00:00:00"),
        ms("2026-08-24T00:00:03"),
    ]
    assert points[2].quality is Quality.MISSING, "空欄は欠測として残る"
    assert device is not None and device.interval_ms == 3_000
    assert recorded_source == "replay", "API がソース種別を答えられる（FR-305）"


# ---------------------------------------------------------------- 揺れへの耐性


def test_an_empty_directory_is_refused(tmp_path):
    with pytest.raises(ValueError, match="見つからない"):
        ReplaySource(tmp_path, tz=JST)


def test_rows_without_a_timestamp_are_skipped(tmp_path):
    path = tmp_path / "sensors_gap.csv"
    path.write_text(
        "timestamp,room_temp\n2026-08-24T00:00:00,24.0\n,24.1\n2026-08-24T00:00:03,24.2\n",
        encoding="utf-8",
    )
    produced = samples(ReplaySource(path, tz=JST, sleep=no_sleep))
    assert [sample.channels["room_temp"] for sample in produced] == [24.0, 24.2]


def test_an_offset_in_the_file_wins_over_the_argument(tmp_path):
    """CSV がオフセットを持つなら、それを尊重する。上書きしない。"""
    path = tmp_path / "sensors_tz.csv"
    path.write_text("timestamp,room_temp\n2026-08-24T00:00:00+00:00,24.0\n", encoding="utf-8")
    replay = ReplaySource(path, tz=JST, sleep=no_sleep)
    assert replay.clock.now_ms() == ms("2026-08-24T00:00:00") + 9 * 3_600_000


def test_a_single_row_falls_back_to_the_nominal_interval(tmp_path):
    """行が1つでは間隔を測れない。要件の周期（2.5秒）とみなす。"""
    path = tmp_path / "sensors_one.csv"
    path.write_text("timestamp,room_temp\n2026-08-24T00:00:00,24.0\n", encoding="utf-8")
    assert ReplaySource(path, tz=JST, sleep=no_sleep).hello.interval_ms == 2_500


def test_non_numeric_values_become_missing(tmp_path):
    """`n/a` のような値も欠測として扱う。1セルで再生を止めない。"""
    path = tmp_path / "sensors_text.csv"
    path.write_text(
        "timestamp,room_temp,front_intake\n2026-08-24T00:00:00,n/a,24.1\n", encoding="utf-8"
    )
    produced = samples(ReplaySource(path, tz=JST, sleep=no_sleep))
    assert produced[0].channels["room_temp"] is None
    assert produced[0].channels["front_intake"] == 24.1


def test_daemon_builds_a_replay_source(tmp_path, day_24):
    """`--source replay --csv ...` でデーモンが組み上がること。"""
    from coldaisle.ingest.daemon import Config, build
    from conftest import CALIBRATION_PATH, QUALITY_RULES_PATH, SCENARIOS_PATH

    daemon = build(
        Config(
            source="replay",
            csv=day_24,
            bulk=True,
            db=tmp_path / "replay.db",
            scenarios=SCENARIOS_PATH,
            quality_rules=QUALITY_RULES_PATH,
            calibration=CALIBRATION_PATH,
        )
    )
    try:
        stats = daemon.run()
        assert daemon.store.clock is daemon.source.clock, "時計は1つ（#42）"
    finally:
        daemon.store.close()
    assert stats.samples == 4


def test_daemon_reports_a_missing_csv(tmp_path):
    """打ち間違いを生の `FileNotFoundError` にしない。"""
    from coldaisle.ingest.daemon import Config, build
    from conftest import CALIBRATION_PATH, QUALITY_RULES_PATH, SCENARIOS_PATH

    with pytest.raises(SystemExit, match="見つからない"):
        build(
            Config(
                source="replay",
                csv=tmp_path / "typo.csv",
                db=tmp_path / "replay.db",
                scenarios=SCENARIOS_PATH,
                quality_rules=QUALITY_RULES_PATH,
                calibration=CALIBRATION_PATH,
            )
        )
