"""Compute Mode 切替アドバイザリの不変条件（#68 / 決定記録 0063）。

``src/coldaisle/api/compute_mode_advisory.py`` の I-1〜I-7 に1つずつ対応させる。
**各試験は不変条件を破ろうとする入力を与える**。壊れていれば落ちること。
"""

from __future__ import annotations

import ast
import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from coldaisle.api.app import Config, create_app
from coldaisle.api.compute_mode_advisory import (
    ComputeModeAdvisor,
    ComputeModeAdvisorySettings,
    _History,
)
from coldaisle.api.models import (
    HealthSource,
    HealthSources,
    HealthSourceStatus,
    ServerSignal,
)
from coldaisle.clock import SimulatedClock
from coldaisle.metrics import MetricCatalog
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from coldaisle.store.models import EventRecord, LatestReading
from conftest import CONFIG_DIR

NOW_MS = 1_787_616_000_000
"""2026-08-25T00:00:00Z。5分バケットの境界に揃っている。"""

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
SHIPPED_CONFIG = CONFIG_DIR / "compute-mode-advisory.yaml"

BASE_SETTINGS: dict[str, Any] = {
    "version": 1,
    "current": {
        "max_age_s": 60.0,
        "conditions": [
            {"metric": "air.room", "advisory_max": 30.0, "warn_delta": 2.0},
            {"metric": "air.room_humidity", "advisory_max": 65.0},
        ],
    },
    "history": {
        "lookback_days": 30,
        "bucket": "5m",
        "refresh_s": 300,
        "load": {
            "metric": "power.gpu.0",
            "min_value": 400.0,
            "min_ok_samples": 60,
            "min_duration_s": 900,
            "max_gap_s": 300,
        },
        "peak_metrics": ["gpu.0.core"],
    },
}

CURRENT = {"air.room": 26.4, "air.room_humidity": 48.0}


@pytest.fixture(scope="session")
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")


def _settings(
    tmp_path: Path, catalog: MetricCatalog, **overrides: Any
) -> ComputeModeAdvisorySettings:
    """本番と同じ読み込み経路を通す（検証も一緒に効かせる）。"""
    merged = json.loads(json.dumps(BASE_SETTINGS))
    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section] = _deep_merge(merged[section], values)
        else:
            merged[section] = values
    path = tmp_path / "advisory.yaml"
    path.write_text(yaml.safe_dump(merged, allow_unicode=True), encoding="utf-8")
    return ComputeModeAdvisorySettings.from_yaml(path, catalog=catalog)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _store(tmp_path: Path, rules, *, now_ms: int = NOW_MS) -> SqliteStore:
    return SqliteStore(tmp_path / "advisory.db", rules=rules, clock=SimulatedClock(now_ms))


def _sources(status: HealthSourceStatus = HealthSourceStatus.OK) -> HealthSources:
    source = HealthSource(status=status, detail="test", last_sample_ts_ms=None, last_sample_at=None)
    return HealthSources(sensor_unit=source, nvml=source, lm_sensors=source, ai_layer=source)


def _readings(
    values: dict[str, float],
    *,
    age_ms: int = 0,
    quality: Quality = Quality.OK,
    now_ms: int = NOW_MS,
) -> dict[str, LatestReading]:
    return {
        metric: LatestReading(
            metric=metric,
            ts_ms=now_ms - age_ms,
            value=value,
            quality=quality,
            age_ms=age_ms,
        )
        for metric, value in values.items()
    }


def _evaluate(
    advisor: ComputeModeAdvisor,
    store: SqliteStore,
    readings: dict[str, LatestReading] | None = None,
    *,
    signal: ServerSignal = ServerSignal.GREEN,
    sources: HealthSources | None = None,
    now_ms: int = NOW_MS,
):
    return advisor.evaluate(
        store,
        _readings(CURRENT) if readings is None else readings,
        signal=signal,
        sources=_sources() if sources is None else sources,
        alerts=[],
        firing={},
        now_ms=now_ms,
    )


def _full_load(
    store: SqliteStore,
    *,
    start_ms: int,
    duration_ms: int,
    interval_ms: int = 2500,
    power: float = 520.0,
    room: float | None = 26.1,
    humidity: float | None = 45.0,
    core: float | None = 84.0,
    power_quality: Quality = Quality.OK,
) -> None:
    """観測された（＝記録された）フルロードを作る。申告は一切書かない。"""
    samples = []
    ts = start_ms
    while ts < start_ms + duration_ms:
        readings = [Reading(metric="power.gpu.0", value=power, quality=power_quality)]
        if room is not None:
            readings.append(Reading(metric="air.room", value=room, quality=Quality.OK))
        if humidity is not None:
            readings.append(Reading(metric="air.room_humidity", value=humidity, quality=Quality.OK))
        if core is not None:
            readings.append(Reading(metric="gpu.0.core", value=core, quality=Quality.OK))
        samples.append(Sample(ts_ms=ts, readings=tuple(readings)))
        ts += interval_ms
    store.insert_samples(samples)


# --- I-1 介入しない ---------------------------------------------------------


def test_the_advisory_never_blocks_whatever_the_inputs_are(tmp_path, rules, catalog):
    """**どんな条件でも切替をブロックしない**（#68 の受入基準）。"""
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=NOW_MS - 2 * HOUR_MS, duration_ms=HOUR_MS)
        cases = [
            _evaluate(advisor, store),
            _evaluate(advisor, store, _readings({"air.room": 45.0, "air.room_humidity": 99.0})),
            _evaluate(advisor, store, {}, signal=ServerSignal.RED),
            _evaluate(
                advisor,
                store,
                _readings(CURRENT, quality=Quality.SUSPECT),
                signal=ServerSignal.YELLOW,
                sources=_sources(HealthSourceStatus.UNAVAILABLE),
            ),
        ]
    for advisory in cases:
        assert advisory.blocking is False
        # 「止める」「切り替えるな」に読める指示を payload に載せない
        dumped = json.dumps(advisory.model_dump(mode="json"), ensure_ascii=False)
        assert "deny" not in dumped and "refuse" not in dumped


def test_the_advisory_module_cannot_reach_control_or_the_write_entry():
    """制御・アクチュエーション・書き込み入口へ至る import を持たない（AGENTS.md 1 / 2）。"""
    tree = ast.parse((SRC / "api" / "compute_mode_advisory.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
    forbidden = [
        name
        for name in imported
        if name.startswith(("coldaisle.control", "coldaisle.event_entry", "coldaisle.ai"))
        or name in {"subprocess", "serial"}
    ]
    assert forbidden == []


# --- I-2 欠けた判断材料を「問題なし」にしない -------------------------------


def test_a_condition_that_was_never_recorded_is_reported_and_is_not_safe(tmp_path, rules, catalog):
    """**未保存を「条件なし＝正常」にしない。** キーは残り、safe は立たない。"""
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        advisory = _evaluate(advisor, store, {})

    assert [condition.metric for condition in advisory.conditions] == [
        "air.room",
        "air.room_humidity",
    ]
    assert all(condition.quality is Quality.MISSING for condition in advisory.conditions)
    assert all(not condition.usable for condition in advisory.conditions)
    assert advisory.safe is False
    assert any("condition unavailable: air.room" in warning for warning in advisory.warnings)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"age_ms": 120_000}, "古い値を現在の条件として扱わない"),
        ({"age_ms": -5_000}, "未来の時刻の値を現在の条件として扱わない"),
        ({"quality": Quality.SUSPECT}, "疑わしい値を判断材料にしない"),
        ({"quality": Quality.STALE}, "stale を判断材料にしない"),
    ],
)
def test_questionable_current_values_are_not_usable(tmp_path, rules, catalog, kwargs, reason):
    """signal が green でも、**条件そのものが読めていなければ safe にしない。**"""
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        advisory = _evaluate(advisor, store, _readings(CURRENT, **kwargs))

    assert advisory.safe is False, reason
    assert all(not condition.usable for condition in advisory.conditions)
    assert len(advisory.warnings) == len(advisory.conditions)


def test_a_condition_above_the_advisory_maximum_is_warned_and_not_safe(tmp_path, rules, catalog):
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        advisory = _evaluate(
            advisor, store, _readings({"air.room": 31.2, "air.room_humidity": 48.0})
        )

    assert advisory.safe is False
    assert advisory.conditions[0].exceeded is True
    assert any("above the advisory maximum" in warning for warning in advisory.warnings)


def test_missing_history_is_stated_instead_of_being_left_out(tmp_path, rules, catalog):
    """比較できないことを黙らない。``reference`` は null で、理由が残る。"""
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        advisory = _evaluate(advisor, store)

    assert advisory.reference is None
    assert advisory.reference_count == 0
    assert advisory.limitations
    assert advisory.conditions[0].reference_value is None
    assert advisory.conditions[0].delta is None


def test_a_full_load_period_without_room_evidence_reports_the_gap(tmp_path, rules, catalog):
    """電力だけ残っていて室温が無い期間を「室温は問題なかった」にしない。"""
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        _full_load(
            store, start_ms=NOW_MS - 2 * HOUR_MS, duration_ms=HOUR_MS, room=None, humidity=None
        )
        advisory = _evaluate(advisor, store)

    assert advisory.reference is not None
    assert "air.room" not in advisory.reference.conditions
    assert any("no air.room evidence" in limitation for limitation in advisory.limitations)
    assert advisory.conditions[0].delta is None


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"interval_ms": 30_000}, "5分に10サンプルしか無いバケットを根拠に数えない"),
        ({"power_quality": Quality.SUSPECT}, "quality=ok でない値を実測として数えない"),
        ({"duration_ms": 10 * MINUTE_MS}, "最低継続時間に満たない負荷を実績にしない"),
        ({"power": 120.0}, "フルロードでない電力を実績にしない"),
    ],
)
def test_thin_or_questionable_evidence_is_not_a_full_load_reference(
    tmp_path, rules, catalog, kwargs, reason
):
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        _full_load(
            store,
            **{"start_ms": NOW_MS - 2 * HOUR_MS, "duration_ms": HOUR_MS, **kwargs},
        )
        advisory = _evaluate(advisor, store)

    assert advisory.reference is None, reason
    assert advisory.reference_count == 0
    assert advisory.limitations


def test_evidence_older_than_the_lookback_window_is_not_used(tmp_path, rules, catalog):
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=NOW_MS - 40 * 24 * HOUR_MS, duration_ms=HOUR_MS)
        advisory = _evaluate(advisor, store)

    assert advisory.reference is None
    assert advisory.reference_window_days == 30


# --- I-3 自己申告を根拠にしない ---------------------------------------------


def test_declared_gpu_mode_never_creates_a_full_load_reference(tmp_path, rules, catalog):
    """**GPU Mode の申告は実績にならない**（決定記録 0045 §2.6）。

    Compute Mode の通知を何度書いても、観測された電力が無ければ比較対象は現れない。
    """
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        for index in range(50):
            store.record_event(
                EventRecord(
                    ts_ms=NOW_MS - 3 * HOUR_MS + index * MINUTE_MS,
                    kind="gpu_mode",
                    payload_json=json.dumps({"v": 1, "type": "gpu_mode", "mode": "compute"}),
                    peer_uid=1000,
                ),
                state=("sys.gpu_mode", "compute"),
            )
        advisory = _evaluate(advisor, store)

    assert advisory.reference is None
    assert advisory.reference_count == 0


# --- I-4 同じ入力の反復で水増ししない ---------------------------------------


def test_repeating_the_same_observations_does_not_inflate_the_history(tmp_path, rules, catalog):
    """同じ時刻のサンプルを繰り返し受けても、件数も継続時間も増えない。"""
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=NOW_MS - 2 * HOUR_MS, duration_ms=HOUR_MS)
        once = _evaluate(advisor, store)
        for _ in range(3):
            _full_load(store, start_ms=NOW_MS - 2 * HOUR_MS, duration_ms=HOUR_MS)
        twice = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
        repeated = _evaluate(twice, store)

    assert once.reference is not None
    assert once.reference_count == 1
    assert repeated.reference == once.reference
    assert repeated.reference_count == 1


def test_a_continuous_load_is_one_reference_and_separate_loads_are_counted_once_each(
    tmp_path, rules, catalog
):
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=NOW_MS - 20 * HOUR_MS, duration_ms=HOUR_MS, room=24.0)
        _full_load(store, start_ms=NOW_MS - 2 * HOUR_MS, duration_ms=HOUR_MS, room=26.1)
        advisory = _evaluate(advisor, store)

    assert advisory.reference_count == 2
    assert advisory.reference is not None
    # 比較に使うのは直近の期間
    assert advisory.reference.started_at_ms == NOW_MS - 2 * HOUR_MS
    assert advisory.reference.conditions["air.room"] == pytest.approx(26.1)


def test_a_gap_inside_one_period_is_not_counted_as_observed_time(tmp_path, rules, catalog):
    """欠落を跨いで1期間に繋いでも、**継続時間は観測できたぶんだけ**。"""
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=NOW_MS - 3 * HOUR_MS, duration_ms=15 * MINUTE_MS)
        # 5分の欠落（max_gap_s=300 以内なので同じ期間に繋がる）
        _full_load(
            store, start_ms=NOW_MS - 3 * HOUR_MS + 20 * MINUTE_MS, duration_ms=15 * MINUTE_MS
        )
        advisory = _evaluate(advisor, store)

    assert advisory.reference_count == 1
    assert advisory.reference is not None
    assert advisory.reference.bucket_count == 6
    assert advisory.reference.covered_s == 1800
    assert advisory.reference.ended_at_ms - advisory.reference.started_at_ms == 35 * MINUTE_MS


def test_an_observed_low_load_bucket_splits_the_period(tmp_path, rules, catalog):
    """**観測された閾値未満のバケットは欠落ではない**。期間を必ずそこで切る。

    10分の高負荷・5分の観測済み低負荷・5分の高負荷は、連続15分のフルロードではない。
    """
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    start = NOW_MS - 3 * HOUR_MS
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=start, duration_ms=10 * MINUTE_MS)
        _full_load(store, start_ms=start + 10 * MINUTE_MS, duration_ms=5 * MINUTE_MS, power=120.0)
        _full_load(store, start_ms=start + 15 * MINUTE_MS, duration_ms=5 * MINUTE_MS)
        advisory = _evaluate(advisor, store)

    assert advisory.reference is None
    assert advisory.reference_count == 0


def test_a_thin_low_bucket_is_a_gap_and_does_not_split_the_period(tmp_path, rules, catalog):
    """数サンプルしか無いバケットは、負荷が下がった証拠にもならない（欠落と同じ）。

    閾値未満の値が少しだけ届いたバケットで期間を切らない。
    """
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    start = NOW_MS - 3 * HOUR_MS
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=start, duration_ms=10 * MINUTE_MS)
        # 5分に10サンプルだけ（min_ok_samples=60 未満）の低い値
        _full_load(
            store,
            start_ms=start + 10 * MINUTE_MS,
            duration_ms=5 * MINUTE_MS,
            interval_ms=30_000,
            power=120.0,
        )
        _full_load(store, start_ms=start + 15 * MINUTE_MS, duration_ms=5 * MINUTE_MS)
        advisory = _evaluate(advisor, store)

    assert advisory.reference_count == 1
    assert advisory.reference is not None
    # 薄いバケットは繋ぐが、観測時間には数えない
    assert advisory.reference.bucket_count == 3
    assert advisory.reference.covered_s == 900


# --- I-5 時刻は証拠由来 -----------------------------------------------------


def test_reference_timestamps_come_from_the_samples_not_from_now(tmp_path, rules, catalog):
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    start = NOW_MS - 5 * HOUR_MS
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=start, duration_ms=HOUR_MS, core=91.0)
        advisory = _evaluate(advisor, store)

    assert advisory.reference is not None
    assert advisory.reference.started_at_ms == start
    assert advisory.reference.ended_at_ms == start + HOUR_MS
    assert advisory.reference.started_at.startswith("2026-08-24T19:00:00")
    assert advisory.reference.peaks["gpu.0.core"] == pytest.approx(91.0)
    # 評価時刻は現在時刻で、実績の時刻とは別のフィールドに分けて出す
    assert advisory.evaluated_at_ms == NOW_MS


def test_the_delta_against_the_last_full_load_is_reported(tmp_path, rules, catalog):
    """#68 の本題: 「その時より何度高いか」を数値で出す。"""
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=NOW_MS - 2 * HOUR_MS, duration_ms=HOUR_MS, room=26.1)
        advisory = _evaluate(
            advisor, store, _readings({"air.room": 29.2, "air.room_humidity": 48.0})
        )

    room = advisory.conditions[0]
    assert room.reference_value == pytest.approx(26.1)
    assert room.delta == pytest.approx(3.1)
    assert any("versus the last observed full load" in warning for warning in advisory.warnings)
    # 上限は超えていないので、差の警告だけが出る
    assert room.exceeded is False


# --- I-6 AI に依存しない・決定論 ---------------------------------------------


def test_the_same_database_and_clock_give_the_same_advisory(tmp_path, rules, catalog):
    settings = _settings(tmp_path, catalog)
    with _store(tmp_path, rules) as store:
        _full_load(store, start_ms=NOW_MS - 2 * HOUR_MS, duration_ms=HOUR_MS)
        first = _evaluate(ComputeModeAdvisor(settings, catalog), store)
        second = _evaluate(ComputeModeAdvisor(settings, catalog), store)

    assert first == second


def test_history_is_evaluated_once_per_refresh_window_and_says_when(tmp_path, rules, catalog):
    """窓の中では同じ結果を返すが、**いつ時点かを隠さない**。"""
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules) as store:
        first = _evaluate(advisor, store)
        assert first.reference is None

        _full_load(store, start_ms=NOW_MS - 2 * HOUR_MS, duration_ms=HOUR_MS)
        within = _evaluate(advisor, store, now_ms=NOW_MS + 60_000)
        assert within.reference is None
        assert within.evaluated_at_ms == NOW_MS

        after = _evaluate(advisor, store, now_ms=NOW_MS + 300_000)

    assert after.reference is not None
    assert after.evaluated_at_ms == NOW_MS + 300_000


def test_the_first_history_evaluation_in_a_window_runs_only_once(
    tmp_path, rules, catalog, monkeypatch
):
    """REST と WS が同じ advisor を同時に呼んでも、**窓ごとの評価は1回だけ**。

    2本が別々のスナップショットで評価すると、同じ窓で異なる reference を返し、
    どちらがキャッシュに残るかが実行順で変わってしまう。
    """
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    calls: list[int] = []
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()

    def counting(store, now_ms):
        calls.append(now_ms)
        if len(calls) == 1:
            first_entered.set()
            assert release.wait(timeout=10)
        else:
            second_entered.set()
        # 呼び出しごとに別の結果を返し、どちらがキャッシュに残ったかを見分ける
        return _History(None, len(calls), now_ms, ())

    monkeypatch.setattr(advisor, "_evaluate_history", counting)
    results: dict[str, Any] = {}

    def run(name: str, now_ms: int) -> None:
        # SQLite の接続はスレッドをまたげないため、本番と同じく呼び出し側ごとに開く
        with _store(tmp_path, rules, now_ms=now_ms) as store:
            results[name] = advisor._history(store, now_ms)

    first = threading.Thread(target=run, args=("first", NOW_MS))
    second = threading.Thread(target=run, args=("second", NOW_MS + 1_000))
    first.start()
    assert first_entered.wait(timeout=10)
    second.start()
    # 修正前は2本目もすぐ評価に入る。修正後は1本目の完了まで待たされる
    entered_concurrently = second_entered.wait(timeout=0.5)
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not entered_concurrently
    assert calls == [NOW_MS]
    assert results["first"] is results["second"]


def test_a_late_call_for_an_older_window_reuses_the_newest_result(
    tmp_path, rules, catalog, monkeypatch
):
    """新しい窓を評価したあとに遅れて届いた古い窓の呼び出しは、**評価しない**。

    履歴はすでにより新しい窓で評価済みなので、その結果をそのまま返す（どの時点の
    評価かは `evaluated_at` が示す）。これで遅れがどれだけ大きくても、どの窓も
    2回評価されない。
    """
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    calls: list[int] = []

    def counting(store, now_ms):
        calls.append(now_ms)
        return _History(None, len(calls), now_ms, ())

    monkeypatch.setattr(advisor, "_evaluate_history", counting)
    refresh_ms = BASE_SETTINGS["history"]["refresh_s"] * 1000
    newer_ms = NOW_MS + 3 * refresh_ms
    with _store(tmp_path, rules) as store:
        newer = advisor._history(store, newer_ms)
        # 直前の窓と、refresh_s の数倍遅れた窓
        previous = advisor._history(store, newer_ms - 1_000)
        much_older = advisor._history(store, newer_ms - 2 * refresh_ms)
        much_older_again = advisor._history(store, newer_ms - 2 * refresh_ms)
        newer_again = advisor._history(store, newer_ms + 1_000)

    assert calls == [newer_ms]
    assert previous is newer
    assert much_older is newer
    assert much_older_again is newer
    assert newer_again is newer
    assert newer.evaluated_at_ms == newer_ms


def test_the_bucket_still_in_progress_is_not_counted_as_a_full_bucket(tmp_path, rules, catalog):
    """進行中のバケットを5分ぶんの観測として数えない（0063 §2.3）。

    12.5分の実負荷が、進行中バケットを含めて 3 バケット＝15分に化けてはいけない。
    終了時刻が未来になることもあってはならない。
    """
    now_ms = NOW_MS + 150_000
    advisor = ComputeModeAdvisor(_settings(tmp_path, catalog), catalog)
    with _store(tmp_path, rules, now_ms=now_ms) as store:
        _full_load(store, start_ms=NOW_MS - 10 * MINUTE_MS, duration_ms=12 * MINUTE_MS + 30_000)
        advisory = _evaluate(advisor, store, now_ms=now_ms)

    assert advisory.reference is None
    assert advisory.reference_count == 0


# --- I-7 しきい値・metric 名は設定から ---------------------------------------


def test_the_shipped_configuration_loads(catalog):
    """本番の設定が読めること。コード側に既定値は持たせない（AGENTS.md ルール9）。"""
    settings = ComputeModeAdvisorySettings.from_yaml(SHIPPED_CONFIG, catalog=catalog)
    assert settings.version == 1
    assert settings.current.conditions
    assert settings.history.load.metric == "power.gpu.0"


@pytest.mark.parametrize(
    "overrides",
    [
        {"current": {"conditions": [{"metric": "air.no_such_probe"}]}},
        {"history": {"load": {"metric": "power.no_such_rail"}}},
        {"history": {"peak_metrics": ["gpu.0.no_such_sensor"]}},
    ],
)
def test_metric_names_missing_from_the_catalog_are_rejected(tmp_path, catalog, overrides):
    """誤記は常に欠測に見える。起動時に落として、黙って助言から消さない。"""
    with pytest.raises(ValueError, match=re.escape("metrics.yaml")):
        _settings(tmp_path, catalog, **overrides)


def test_the_same_condition_cannot_be_listed_twice(tmp_path, catalog):
    """同じ条件を2度並べると、1つの事実で警告が2件に増える（I-4）。"""
    with pytest.raises(ValueError, match="同じ metric"):
        _settings(
            tmp_path,
            catalog,
            current={
                "conditions": [
                    {"metric": "air.room", "advisory_max": 30.0},
                    {"metric": "air.room", "advisory_max": 28.0},
                ]
            },
        )


def test_unknown_configuration_keys_are_rejected(tmp_path, catalog):
    with pytest.raises(ValueError):
        _settings(tmp_path, catalog, history={"load": {"block_above": 500.0}})


def test_no_advisory_threshold_is_hardcoded():
    """しきい値は設定ファイルだけに置く（AGENTS.md ルール9）。"""
    source = (SRC / "api" / "compute_mode_advisory.py").read_text(encoding="utf-8")
    for literal in ("30.0", "32.0", "65.0", "400.0"):
        assert literal not in source


def test_the_advisory_is_identical_whether_the_ai_answers_or_fails(tmp_path, rules):
    """**AI 停止中でもアドバイザリが機能する**（#68 の受入基準）。

    要約器が答えても例外を投げても、advisory は1ビットも変わらない。
    """

    class Answering:
        def summarize(self, facts: str) -> str:
            return "監視情報と各データ源は正常です。"

    class Raising:
        def summarize(self, facts: str) -> str:
            raise RuntimeError("local model stopped")

    path = tmp_path / "server-health.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        _full_load(store, start_ms=NOW_MS - 2 * HOUR_MS, duration_ms=HOUR_MS)
        store.set_system_state("sys.ingest_source", "serial", at_ms=NOW_MS)

    bodies = []
    for summarizer in (None, Answering(), Raising()):
        app = create_app(
            Config(
                db=path,
                quality_rules=CONFIG_DIR / "quality.yaml",
                metrics=CONFIG_DIR / "metrics.yaml",
                compute_mode_advisory=SHIPPED_CONFIG,
            ),
            clock=SimulatedClock(NOW_MS),
            health_summarizer=summarizer,
            health_hwmon_metrics=(),
            health_nvml_metrics=(),
        )
        with TestClient(app) as client:
            bodies.append(client.get("/api/v1/server-health").json()["compute_mode_advisory"])

    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["blocking"] is False
    assert bodies[0]["reference"] is not None
