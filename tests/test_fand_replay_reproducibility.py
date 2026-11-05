"""同じ Replay + Clock + Config で control tick の順序を再現できる（#74 の受入基準）。

`test_control_loop.py` の invariant 18 は、試験用の Harness（FakeTelemetry）で同じ trace に
なることを見ている。ここでは**取り込みから fand までの導線**を通す。

1. 小さな CSV を `coldaisle-daemon --source replay --bulk` と同じ組み立て（`daemon.build`）で
   DB へ取り込む（決定記録 0010）。CSV には `air.*` しか無いので、Telemetry Collector（#65）
   の代わりに CPU / GPU の行を同じ時刻で書き足す
2. その DB を2つに写し、`coldaisle-fand --backend simulated --max-ticks N`（`main()`）を
   **同じ設定・同じ時計**で2回走らせる
3. 2回の `control_traces` が、時刻に依存しない中身まで一致することを確かめる

**時計の差し替え。** `control_daemon.build()` は `WallClock()` と `SystemMonotonicClock()` を
自分で作る。本体を変えずに、module の名前を `SimulatedClock` / `ManualMonotonicClock` を返す
関数へ差し替える（`test_calibrate.py` と同じ手）。tick の間の待ちは `ControlDaemon.sleep` を
「両方の時計を進める」関数へ替えて、本物の時間を待たない。

実機・ネットワーク・本物の時刻待ちは使わない（simulated backend と `tmp_path`）。
"""

from __future__ import annotations

import json
import signal
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from coldaisle import control_daemon, logs
from coldaisle import daemon as ingest_daemon
from coldaisle.clock import ManualMonotonicClock, SimulatedClock
from coldaisle.control.safety.critical import (
    CPU_POWER_METRIC,
    CPU_TEMPERATURE_METRIC,
    GPU_TEMPERATURE_METRIC,
)
from coldaisle.control_daemon import Config, ControlDaemon, main
from coldaisle.notify.notifiers import LINE_TO_ENV, LINE_TOKEN_ENV, SLACK_WEBHOOK_ENV
from coldaisle.notify.slack_bot import SLACK_BOT_TOKEN_ENV, SLACK_CHANNEL_ENV
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.models import Quality, Reading, Sample
from conftest import CALIBRATION_PATH, QUALITY_RULES_PATH, SCENARIOS_PATH
from test_control_config import provisional, valid_documents, write_documents
from test_control_loop import METRICS_PATH
from test_simulated_fan_backend import hardware_config

MAX_TICKS = 8
"""`tick_ms`（1 s）で 8 tick。`startup_settle_ms`（3 s）を抜けた後まで含む。"""

TELEMETRY_STALE_AFTER_MS = 9_000
"""この試験の safety.yaml の入力の許容遅延（CPU / GPU / CPU 電力 / 吸排気）。仮の値。

取り込みを先に終えてから fand を走らせるので、DB の最新の行は fand の間に増えない
（`latest()` は未来の行も最新として返すので、先に未来の行を入れて代わりにもできない）。
既定の試験値（1 s）のままだと 2 tick 目で全入力が stale になり、startup のまま Max が続く
trace しか比べられない。`quality.yaml` の `stale_after_ms`（10 s）より短くし、8 tick の間は
新しいまま読ませて、startup を抜けた後の Fallback → Guard → Safety の経路を通す。
"""

CSV_TEXT = (
    "timestamp,room_temp,room_humidity,front_intake,gpu_intake,gpu_exhaust,top_exhaust,"
    "rear_exhaust\n"
    "2026-08-23T20:16:30,24.5,60.0,24.31,23.94,24.00,24.06,24.19\n"
    "2026-08-23T20:16:32,24.5,60.1,24.37,24.00,24.25,24.12,24.31\n"
    "2026-08-23T20:16:35,24.6,60.1,24.44,24.06,24.50,24.25,24.44\n"
    "2026-08-23T20:16:37,24.6,60.2,24.50,24.12,24.81,24.31,24.56\n"
    "2026-08-23T20:16:40,24.6,60.2,24.56,24.19,25.06,24.44,24.69\n"
)
"""Replay の入力（仮の値。実機の記録ではない）。間隔は実機の CSV に近い 2.5 s 前後。"""

INTERNAL_VALUES: tuple[tuple[float, float, float], ...] = (
    (52.0, 41.0, 38.0),
    (55.0, 43.0, 45.0),
    (58.0, 46.0, 52.0),
    (61.0, 49.0, 60.0),
    (63.0, 52.0, 66.0),
)
"""CSV の各行と同じ時刻に書く（CPU Package ℃, GPU core ℃, CPU Package W）。仮の値。"""

NONDETERMINISTIC_TRACE_FIELDS = frozenset({"run_id"})
"""2回の起動で**違って当然**の欄。

`run_id` は起動ごとに `new_run_id()` が乱数で作る（決定記録 0077 §2.3）。同じ値になることを
求めると、2つの起動を区別するための欄の意味が無くなる。
"""


@dataclass(slots=True)
class FandRun:
    """1回の fand の起動の結果。"""

    exit_code: int
    traces: list[tuple[int, int, str]]
    """`control_traces` の `(ts_ms, tick_id, trace_json)`。`seq` の順。"""


@pytest.fixture(autouse=True)
def quiet_process_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` が process 全体に触る所を止める。

    - `logs.configure()` は root の handler を差し替える（他の試験の caplog を壊す）
    - `signal.signal()` は試験の process の SIGTERM / SIGINT を書き換える
    - 取り込み側の通知の宛先を環境から拾わない（開発機の `.env` で Slack へ送らない）
    """
    monkeypatch.setattr(logs, "configure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(signal, "signal", lambda *_args, **_kwargs: None)
    for name in (
        SLACK_WEBHOOK_ENV,
        SLACK_BOT_TOKEN_ENV,
        SLACK_CHANNEL_ENV,
        LINE_TOKEN_ENV,
        LINE_TO_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _replay_into(db: Path, csv_path: Path) -> int:
    """CSV を Replay で取り込み、最後の行の時刻（ms）を返す。"""
    daemon = ingest_daemon.build(
        ingest_daemon.Config(
            source="replay",
            csv=csv_path,
            bulk=True,
            db=db,
            scenarios=SCENARIOS_PATH,
            quality_rules=QUALITY_RULES_PATH,
            calibration=CALIBRATION_PATH,
        )
    )
    try:
        stats = daemon.run()
        ts_ms = [
            int(row[0])
            for row in daemon.store.connection.execute(
                "SELECT DISTINCT ts_ms FROM readings ORDER BY ts_ms"
            )
        ]
    finally:
        daemon.store.close()
    assert stats.samples == len(INTERNAL_VALUES)
    assert len(ts_ms) == len(INTERNAL_VALUES), "CSV の行ごとに1つの時刻になっていない"
    _write_internal_telemetry(db, ts_ms)
    return ts_ms[-1]


def _write_internal_telemetry(db: Path, ts_ms: list[int]) -> None:
    """Telemetry Collector（#65）が書くはずの CPU / GPU の行を、CSV と同じ時刻で書く。

    これが無いと CRITICAL の入力が欠け、どの tick も telemetry loss の Max になる。それでも
    再現はするが、通常の経路（Fallback → Guard → Safety）を通らない trace しか比べられない。
    """
    rules = QualityRules.from_yaml(QUALITY_RULES_PATH)
    with SqliteStore(db, rules=rules, clock=SimulatedClock(ts_ms[-1])) as store:
        written = store.insert_samples(
            Sample(
                ts_ms=at_ms,
                readings=(
                    Reading(metric=CPU_TEMPERATURE_METRIC, value=cpu, quality=Quality.OK),
                    Reading(metric=GPU_TEMPERATURE_METRIC, value=gpu, quality=Quality.OK),
                    Reading(metric=CPU_POWER_METRIC, value=power, quality=Quality.OK),
                ),
            )
            for at_ms, (cpu, gpu, power) in zip(ts_ms, INTERNAL_VALUES, strict=True)
        )
    assert written == 3 * len(ts_ms)


def _copy_db(source: Path, target: Path) -> None:
    """SQLite の backup で写す（WAL の中身も含めて、同じ状態の DB を2つ作る）。"""
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    src.close()
    dst.close()


def _run_fand(
    monkeypatch: pytest.MonkeyPatch, directory: Path, db: Path, *, start_ms: int
) -> FandRun:
    """`coldaisle-fand` を simulated backend・模擬の時計で `MAX_TICKS` だけ走らせる。"""
    wall = SimulatedClock(start_ms)
    monotonic = ManualMonotonicClock(0)

    def advance(seconds: float) -> None:
        # 壁時計と単調時計を同じだけ進める。本物の時間は待たない
        delta_ms = round(seconds * 1_000)
        assert delta_ms > 0, "進まない sleep は run() を空回りさせる"
        monotonic.advance_ms(delta_ms)
        wall.advance_to_ms(wall.now_ms() + delta_ms)

    original_build: Callable[..., ControlDaemon] = control_daemon.build

    def build_with_simulated_sleep(config: Config, **kwargs: Any) -> ControlDaemon:
        built = original_build(config, **kwargs)
        built.sleep = advance
        return built

    monkeypatch.setattr(control_daemon, "WallClock", lambda: wall)
    monkeypatch.setattr(control_daemon, "SystemMonotonicClock", lambda: monotonic)
    monkeypatch.setattr(control_daemon, "build", build_with_simulated_sleep)
    exit_code = main(
        [
            "--config-dir",
            str(directory),
            "--db",
            str(db),
            "--metrics",
            str(METRICS_PATH),
            "--quality-rules",
            str(QUALITY_RULES_PATH),
            "--no-admin",
            "--authority-root",
            str(directory / "authority"),
            "--backend",
            "simulated",
            "--max-ticks",
            str(MAX_TICKS),
        ]
    )
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT ts_ms, tick_id, trace_json FROM control_traces ORDER BY seq"
        ).fetchall()
    connection.close()
    return FandRun(exit_code=exit_code, traces=[(int(a), int(b), str(c)) for a, b, c in rows])


def _documents() -> dict[str, dict[str, object]]:
    """承認済みの hardware mapping（試験用）と、許容遅延を延ばした safety.yaml（仮の値）。"""
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    safety = documents["safety.yaml"]
    telemetry = safety["telemetry"]
    assert isinstance(telemetry, dict)
    for name in ("cpu_ms", "cpu_power_ms", "gpu_ms", "air_ms"):
        telemetry[name] = provisional(TELEMETRY_STALE_AFTER_MS)
    return documents


def _comparable(trace_json: str) -> dict[str, Any]:
    trace = json.loads(trace_json)
    assert isinstance(trace, dict)
    for name in NONDETERMINISTIC_TRACE_FIELDS:
        assert name in trace, f"trace に {name} が無い（除く欄の一覧が古い）"
        del trace[name]
    return trace


def test_replay_then_fand_reproduces_the_same_tick_order_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同じ Replay の DB・同じ時計・同じ設定で2回起動すると、tick の列と中身が一致する。"""
    csv_path = tmp_path / "sensors_2026-08-23.csv"
    csv_path.write_text(CSV_TEXT, encoding="utf-8")
    replayed = tmp_path / "replayed.db"
    last_ms = _replay_into(replayed, csv_path)

    runs: list[FandRun] = []
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        write_documents(directory, _documents())
        db = directory / "control.db"
        _copy_db(replayed, db)
        # 最後の行の直後から始める（取り込みと fand が同じ時間軸に並ぶ）
        runs.append(_run_fand(monkeypatch, directory, db, start_ms=last_ms))

    first, second = runs
    assert first.exit_code == second.exit_code == 0
    assert len(first.traces) == MAX_TICKS, "tick ごとに1行の trace になっていない"

    # tick の順序: tick_id の列と時刻の列が一致し、tick_id は欠けずに増える
    assert [tick_id for _, tick_id, _ in first.traces] == list(range(MAX_TICKS))
    assert [(ts, tick_id) for ts, tick_id, _ in first.traces] == [
        (ts, tick_id) for ts, tick_id, _ in second.traces
    ]
    # 時刻は模擬の時計から来る（本物の時刻が漏れていれば start_ms から外れる）
    assert [ts for ts, _, _ in first.traces] == [last_ms + 1_000 * i for i in range(MAX_TICKS)]

    # 比べる trace が「startup の Max が続くだけ」にならず、通常の経路まで届いている
    last = json.loads(first.traces[-1][2])
    assert last["state"]["safety_state"] == "normal"
    assert last["faults"] == []
    assert all(
        last["zones"][zone]["demand"]["effective"] < 1.0 for zone in ("front", "rear", "top")
    )

    # 中身: 起動ごとに違って当然の欄（run_id）だけを除き、残りは丸ごと一致する
    assert [_comparable(row) for _, _, row in first.traces] == [
        _comparable(row) for _, _, row in second.traces
    ]
