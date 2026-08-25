"""取り込みデーモン（#8）。

受入基準は「24時間相当を流しても止まらない」と「不正な行があっても継続し、
破棄件数がログに出る」。どちらも**止まらないこと**が本体なので、
異常を混ぜた入力で最後まで走り切ることを確かめる。
"""

import io
import json
import logging
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

from coldaisle import logs
from coldaisle.clock import SimulatedClock
from coldaisle.ingest import Normalizer
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.daemon import Config, Daemon, build, build_parser, main
from coldaisle.ingest.protocol import RawHello, RawMessage, RawSample, RawSensor
from coldaisle.store import SqliteStore
from conftest import CALIBRATION_PATH, QUALITY_RULES_PATH, SCENARIOS_PATH

DAY_S = 24 * 60 * 60


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    logs.configure("INFO", stream)
    yield stream
    logging.getLogger().handlers = []


def log_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def config(tmp_path, **overrides) -> Config:
    return Config(
        db=tmp_path / "coldaisle.db",
        scenarios=SCENARIOS_PATH,
        quality_rules=QUALITY_RULES_PATH,
        calibration=CALIBRATION_PATH,
        **overrides,
    )


class FakeSource:
    """任意のメッセージ列を流すソース。異常の混入を組み立てるために使う。

    `stamps` を渡すとホスト受信時刻を明示できる。同じ時刻を2回渡せば、
    時計が止まったまま2件届いた状況（保存が主キー違反で落ちる）を作れる。
    """

    def __init__(
        self,
        messages: list[RawMessage],
        clock: SimulatedClock,
        stamps: list[int] | None = None,
    ) -> None:
        self._messages = messages
        self._clock = clock
        self._stamps = stamps

    @property
    def clock(self) -> SimulatedClock:
        return self._clock

    def stream(self) -> Iterator[RawMessage]:
        for index, message in enumerate(self._messages):
            at_ms = index * 2_500 if self._stamps is None else self._stamps[index]
            self._clock.advance_to_ms(at_ms)
            yield message


def daemon_with(messages, rules, tmp_path, stamps=None) -> Daemon:
    clock = SimulatedClock(0)
    return Daemon(
        source=FakeSource(messages, clock, stamps),
        store=SqliteStore(tmp_path / "fake.db", rules=rules, clock=clock),
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=clock),
    )


def sample(seq: int, up: int, **channels) -> RawSample:
    base = {"room_temp": 26.0, "gpu_intake": 28.0}
    return RawSample(seq=seq, up=up, channels={**base, **channels})


# ---------------------------------------------------------------- 合成の起点


def test_build_hands_one_clock_to_every_layer(tmp_path):
    """#42 の受入基準。**同じ値ではなく同じオブジェクト**であること。"""
    daemon = build(config(tmp_path, scenario="ramp", speed=60.0))
    try:
        assert daemon.store.clock is daemon.source.clock
        assert daemon.normalizer.clock is daemon.source.clock
    finally:
        daemon.store.close()


def test_unimplemented_sources_say_which_issue(tmp_path):
    with pytest.raises(SystemExit, match="#12"):
        build(config(tmp_path, source="serial"))


def test_replay_without_a_csv_says_so(tmp_path):
    with pytest.raises(SystemExit, match="--csv"):
        build(config(tmp_path, source="replay"))


def test_unknown_scenario_lists_the_candidates(tmp_path):
    with pytest.raises(SystemExit, match="idle"):
        build(config(tmp_path, scenario="typo"))


# ---------------------------------------------------------------- パイプライン


def test_pipeline_writes_to_the_database(tmp_path, log_stream):
    """#6 の受入基準: `--source mock --scenario ramp` でDBに書き込まれる。"""
    daemon = build(config(tmp_path, scenario="ramp", speed=1_000.0))
    try:
        stats = daemon.run(max_samples=20)
        latest = daemon.store.latest()
    finally:
        daemon.store.close()

    assert stats.samples == 20
    assert stats.hellos == 1
    assert stats.discarded == 0
    assert set(latest) == {
        "air.room",
        "air.room_humidity",
        "air.front_intake",
        "air.gpu_intake",
        "air.gpu_exhaust",
        "air.top_exhaust",
        "air.rear_exhaust",
    }


def test_hello_is_recorded_with_its_sensors(tmp_path):
    daemon = build(config(tmp_path, scenario="idle", speed=1_000.0))
    try:
        daemon.run(max_samples=2)
        device = daemon.store.device("xiao-esp32s3-mock")
        sensors = daemon.store.sensors_for("xiao-esp32s3-mock")
    finally:
        daemon.store.close()
    assert device is not None
    assert device.interval_ms == 2_500, "expected_count の算出に要る（決定記録 0002 §2.8）"
    assert {sensor.channel for sensor in sensors} >= {"front_intake", "room"}


def test_probe_change_is_reported(tmp_path, rules, log_stream):
    """ROM が変わったら警告する。アラート化（FR-403）は #14。"""

    def hello(rom: str) -> RawHello:
        return RawHello(
            fw="1.0.0",
            dev="dev",
            interval_ms=2_500,
            sensors={"rear_exhaust": RawSensor(kind="ds18b20", gpio=7, rom=rom, res=11)},
        )

    daemon = daemon_with([hello("28FFFFFFFFFFFF01"), hello("28FFFFFFFFFFFF09")], rules, tmp_path)
    try:
        daemon.run()
    finally:
        daemon.store.close()
    warnings = [line for line in log_lines(log_stream) if line["level"] == "warning"]
    assert any("ROM" in line["msg"] for line in warnings)


# ---------------------------------------------------------------- 継続性（受入基準）


def test_duplicate_rows_are_counted_and_logged(tmp_path, rules, log_stream):
    """**黙って捨てない**（決定記録 0012 §2.4）。

    時計が止まったまま2件届くと `(metric, ts_ms)` が衝突する。無視した行は
    数えて記録しないと、完全な取り込みと取りこぼした取り込みを区別できない。
    """
    messages = [sample(index, 1_000 + index * 2_500) for index in range(4)]
    daemon = daemon_with(messages, rules, tmp_path, stamps=[0, 2_500, 2_500, 5_000])
    try:
        stats = daemon.run()
        stored = len(daemon.store.series("air.room", 0, 100_000))
    finally:
        daemon.store.close()

    assert stats.duplicates == 2, "衝突した2列（room / gpu_intake）"
    assert stats.samples == 4, "**取り込みは止まらない**"
    assert stats.discarded == 0, "重複は失敗ではない"
    assert stored == 3
    summary = log_lines(log_stream)[-1]
    assert summary["duplicates"] == 2
    assert any(line["msg"] == "既にある時刻の行を書かなかった" for line in log_lines(log_stream))


def test_non_finite_values_do_not_cost_the_whole_sample(tmp_path, rules, log_stream):
    """1チャネルが壊れても他のチャネルは保存する（決定記録 0003 §2.8）。"""
    messages = [
        sample(0, 1_000),
        sample(1, 3_500, room_temp=float("inf"), gpu_intake=28.5),
    ]
    daemon = daemon_with(messages, rules, tmp_path)
    try:
        stats = daemon.run()
        room = daemon.store.series("air.room", 0, 100_000)
        intake = daemon.store.series("air.gpu_intake", 0, 100_000)
    finally:
        daemon.store.close()

    assert stats.discarded == 0
    assert [point.value for point in room] == [26.0, None]
    assert [point.value for point in intake] == [28.0, 28.5]


def test_one_bad_sample_does_not_stop_the_process(tmp_path, rules, log_stream):
    """**1サンプルのパース失敗でプロセスを落とさない**（AGENTS.md）。

    ここでの失敗は正規化で落ちるもの（メトリクス名が規約に合わないなど）。
    重複は失敗ではなく、無視して続ける（決定記録 0012 §2.4）。
    """
    messages: list = [sample(index, 1_000 + index * 2_500) for index in range(10)]
    messages[4] = RawSample(seq=4, up=11_000, channels={"room_temp": float("inf")})
    daemon = daemon_with(messages, rules, tmp_path)
    try:
        stats = daemon.run()
    finally:
        daemon.store.close()
    assert stats.samples == 10, "非有限値は値だけ落として行は残る（決定記録 0007 §2.3）"
    assert stats.discarded == 0


def test_sequence_gap_and_restart_are_logged_and_stored(tmp_path, rules, log_stream):
    messages = [
        sample(10, 25_000),
        sample(15, 37_500),  # 4件飛び（FR-105）
        sample(0, 1_200),  # 再起動（FR-106）
    ]
    daemon = daemon_with(messages, rules, tmp_path)
    try:
        stats = daemon.run()
        dropped = daemon.store.series("sys.dropped_samples", 0, 100_000)
        restarts = daemon.store.series("sys.device_restarts", 0, 100_000)
    finally:
        daemon.store.close()

    assert stats.dropped_samples == 4
    assert stats.restarts == 1
    assert [point.value for point in dropped] == [4.0]
    assert [point.value for point in restarts] == [1.0]
    messages_logged = [line["msg"] for line in log_lines(log_stream)]
    assert "サンプルの取りこぼしを検出した" in messages_logged
    assert "デバイスの再起動を検出した" in messages_logged


@pytest.mark.slow
def test_runs_a_full_day_of_compressed_time(tmp_path, log_stream):
    """受入基準: 24時間相当（時間圧縮）を流しても停止しない。"""
    daemon = build(config(tmp_path, scenario="idle", speed=100_000.0))
    expected = DAY_S * 1_000 // 2_500
    try:
        stats = daemon.run(max_samples=expected)
        span = daemon.store.clock.now_ms() - daemon.store.series("air.room", 0, 2**62)[0].ts_ms
    finally:
        daemon.store.close()

    assert stats.samples == expected
    assert stats.discarded == 0
    assert span >= DAY_S * 1_000 - 2_500, "ホスト時刻で24時間ぶん進んでいる"


# ---------------------------------------------------------------- 停止


def test_stop_request_ends_the_loop(tmp_path, rules, log_stream):
    """SIGTERM は次のメッセージの手前で効く。処理中のサンプルは書き切る。"""
    daemon = daemon_with(
        [sample(index, 1_000 + index * 2_500) for index in range(50)], rules, tmp_path
    )
    try:
        daemon.request_stop()
        stats = daemon.run()
    finally:
        daemon.store.close()
    assert stats.samples == 0
    assert "停止要求を受けた" in [line["msg"] for line in log_lines(log_stream)]


# ---------------------------------------------------------------- CLI


def test_parser_defaults_are_the_documented_ones():
    args = build_parser().parse_args([])
    assert args.source == "mock"
    assert args.speed == 1.0


def test_main_writes_and_returns_zero(tmp_path):
    database = tmp_path / "cli.db"
    code = main(
        [
            "--source",
            "mock",
            "--scenario",
            "ramp",
            "--speed",
            "1000",
            "--db",
            str(database),
            "--scenarios",
            str(SCENARIOS_PATH),
            "--quality-rules",
            str(QUALITY_RULES_PATH),
            "--calibration",
            str(CALIBRATION_PATH),
            "--max-samples",
            "5",
        ]
    )
    assert code == 0
    assert database.exists()


@pytest.mark.slow
def test_sigterm_shuts_down_gracefully(tmp_path):
    """SIGTERM で終了コード0。**書いたものは残る**（AGENTS.md / #8）。

    別プロセスで実際にシグナルを送る。ハンドラを登録しただけで
    ループが止まらない実装は、この形でしか捕まらない。
    """
    database = tmp_path / "sigterm.db"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "coldaisle.ingest.daemon",
            "--source=mock",
            "--scenario=idle",
            "--speed=100",
            f"--db={database}",
            f"--scenarios={SCENARIOS_PATH}",
            f"--quality-rules={QUALITY_RULES_PATH}",
            f"--calibration={CALIBRATION_PATH}",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # 起動バナーと数サンプルが出るまで待つ
        for _ in range(200):
            if database.exists() and database.stat().st_size > 0:
                break
            time.sleep(0.05)
        time.sleep(0.5)
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover - 落ちなかったときの後始末
            process.kill()

    assert process.returncode == 0, stderr
    assert "取り込みを終了する" in stderr
    # ログは JSON Lines（AGENTS.md）。1行でも素の文字列が混ざると集約側が壊れる。
    # `python -m` の二重 import が `runpy` の警告を出していたのが実例
    for line in stderr.splitlines():
        json.loads(line)
    with sqlite3.connect(database) as connection:
        stored = connection.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    assert stored > 0, "終了までに書いたものが残っていない"


def test_out_of_order_and_unknown_channels_are_logged_once(tmp_path, rules, log_stream):
    """未知チャネルは初出だけ記録する。毎サンプル出すとログが埋まる。"""
    messages = [
        sample(5, 1_000, vrm_temp=55.0),
        sample(5, 3_500, vrm_temp=56.0),  # seq が進んでいない
    ]
    daemon = daemon_with(messages, rules, tmp_path)
    try:
        stats = daemon.run()
    finally:
        daemon.store.close()

    assert stats.unknown_channels == {"vrm_temp"}
    lines = [line["msg"] for line in log_lines(log_stream)]
    assert lines.count("未知のチャネルを無視した") == 1
    assert "seq が進んでいない" in lines


def test_speed_warns_about_the_process_boundary(tmp_path, capsys):
    """圧縮中の仮想時刻はこのプロセスの中にしか無い（#42）。起動時に言う。

    `main()` は自分でログを設定し直すため、ここでは標準エラーを見る。
    """
    main(
        [
            "--source=mock",
            "--scenario=ramp",
            "--speed=1000",
            f"--db={tmp_path / 'warn.db'}",
            f"--scenarios={SCENARIOS_PATH}",
            f"--quality-rules={QUALITY_RULES_PATH}",
            f"--calibration={CALIBRATION_PATH}",
            "--max-samples=2",
        ]
    )
    logged = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert any("同時に使わないこと" in line["msg"] for line in logged)
    logging.getLogger().handlers = []


def test_unknown_device_reads_as_none(tmp_path, rules):
    daemon = daemon_with([], rules, tmp_path)
    try:
        assert daemon.store.device("never-seen") is None
        assert daemon.store.sensors_for("never-seen") == ()
    finally:
        daemon.store.close()


def test_queue_drops_are_recorded_for_the_api(tmp_path, rules, log_stream):
    """待ち行列が溢れたら**残して数える**（決定記録 0012 §2.3）。

    捨てるのは読み取りスレッドだが、DB を触るのは取り込みスレッドだけ。
    API は別プロセスなので、`/api/v1/health` から見えるようにするには残すしかない。
    """
    from coldaisle.channels import QUEUE_DROPS_METRIC

    daemon = daemon_with(
        [sample(index, 1_000 + index * 2_500) for index in range(3)], rules, tmp_path
    )
    daemon.stats.queue_drops = 4  # 読み取りスレッドが4件捨てた状態を作る
    try:
        daemon.run()
        drops = daemon.store.series(QUEUE_DROPS_METRIC, 0, 100_000)
    finally:
        daemon.store.close()

    assert [point.value for point in drops] == [4.0], "1回だけ、まとめて記録する"
    assert any(line["msg"] == "待ち行列が溢れた" for line in log_lines(log_stream))


def test_queue_drops_do_not_affect_freshness(tmp_path, rules):
    """事象メトリクスなので鮮度の判定に混ぜない（決定記録 0009 §2.12）。"""
    from coldaisle.channels import EVENT_METRICS, QUEUE_DROPS_METRIC

    assert QUEUE_DROPS_METRIC in EVENT_METRICS
