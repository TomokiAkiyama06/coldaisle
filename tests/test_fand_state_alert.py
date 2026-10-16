"""fand の EMERGENCY / DEGRADED と decision trace の途絶の通知。決定記録 0148

監視系（取り込みのルールエンジン）は fand が DB へ書いた `control_traces` の
**最後の 1 行**だけを読み、
`state.safety_state` と `faults` から判定する。fand・制御の経路には触らない。

実機は要らない（AGENTS.md ルール 7）。trace は DB へ直接書く（fand の代わり）。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.control.schema import ControlTick, FaultCode
from coldaisle.daemon import Daemon, build
from coldaisle.ingest import Calibration, Normalizer
from coldaisle.metrics import MetricCatalog
from coldaisle.notify import DisplayNames, Notification, NotifyConfig, Router
from coldaisle.rules import Engine, RuleSet
from coldaisle.rules.fand_trace import FandFault, describe_faults, read_fand_status
from coldaisle.store import AlertSeverity, SequencedControlTrace, SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH

RULES_PATH = CONFIG_DIR / "rules.yaml"
METRICS_PATH = CONFIG_DIR / "metrics.yaml"
NOTIFY_PATH = CONFIG_DIR / "notify.yaml"
FIXTURES = Path(__file__).parent / "fixtures"
NOON_MS = 1_787_626_800_000  # 2026-08-25T12:00:00+09:00（昼）
NIGHT_MS = 1_787_670_000_000  # 2026-08-26T00:00:00+09:00（夜）

EMERGENCY = "FAND_EMERGENCY"
DEGRADED = "FAND_DEGRADED"
SILENT = "FAND_TRACE_SILENT"

BASE = json.loads((FIXTURES / "control_tick_v15.json").read_text(encoding="utf-8"))
"""本物の v15 の trace（fand が書く形）。状態と faults だけを差し替える。"""


@pytest.fixture
def rule_set() -> RuleSet:
    """本番の rules.yaml。基板の無音（SENSOR_FAULT）だけは切る（ここでは基板を流さない）。"""
    shipped = RuleSet.from_yaml(RULES_PATH)
    return shipped.model_copy(
        update={"sensor_fault": shipped.sensor_fault.model_copy(update={"enabled": False})}
    )


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "fand.db", rules=rules, clock=SimulatedClock(NOON_MS)) as opened:
        yield opened


@pytest.fixture
def engine(store, rule_set) -> Engine:
    return Engine(
        rules=rule_set,
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        store=store,
        clock=store.clock,
    )


def at(seconds: float, base: int = NOON_MS) -> int:
    return base + int(seconds * 1000)


def body(state: str, faults: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    tick = copy.deepcopy(BASE)
    tick["state"]["safety_state"] = state
    tick["faults"] = faults or []
    return tick


class Fand:
    """fand の代わりに trace を書く。tick_id は書くたびに増やす。"""

    def __init__(self, store: SqliteStore) -> None:
        self.store = store
        self.tick_id = 0

    def write(self, ts_ms: int, trace: dict[str, Any] | str, *, version: int = 15) -> None:
        self.tick_id += 1
        text = trace if isinstance(trace, str) else json.dumps(trace)
        assert self.store.record_control_trace(
            ts_ms=ts_ms, tick_id=self.tick_id, schema_version=version, trace_json=text
        )


def run(
    engine: Engine,
    fand: Fand,
    start: float,
    stop: float,
    trace: dict[str, Any] | None,
    *,
    base: int = NOON_MS,
) -> list:
    """`start` から `stop` 秒（含まない）まで 1 s ごとに書いて評価する。

    `None` は書かない（fand が止まっている）。
    """
    transitions = []
    seconds = start
    while seconds < stop:
        if trace is not None:
            fand.write(at(seconds, base), trace)
        engine.store.clock.advance_to_ms(at(seconds, base))  # type: ignore[attr-defined]
        transitions += engine.on_tick()
        seconds += 1.0
    return transitions


def states(transitions: list, rule_id: str) -> list[str]:
    return [t.state for t in transitions if t.rule_id == rule_id]


STALL_TOP = {"code": "tach_stall", "zone": "top", "detail": "rpm=0, demand=1"}
OVER_TEMP = {
    "code": "absolute_temperature_limit",
    "zone": None,
    "detail": "absolute ceiling reached: board.connector_12v2x6=81C (ceiling 80C)",
}
GPU_STALE = {"code": "gpu_telemetry_stale", "zone": None, "detail": ""}


# ------------------------------------------------------------------ 版の読み分け（0148 §2.4）


@pytest.mark.parametrize("version", range(1, 16))
def test_every_stored_version_is_read_like_the_control_schema(version):
    """v1〜v15 の保存済みの trace から、`ControlTick` と同じ状態と fault を読む。"""
    text = (FIXTURES / f"control_tick_v{version}.json").read_text(encoding="utf-8")
    tick = ControlTick.model_validate_json(text)
    status = read_fand_status(
        SequencedControlTrace(
            seq=1, ts_ms=tick.ts_ms, tick_id=tick.tick_id, schema_version=version, trace_json=text
        )
    )
    assert status is not None
    assert status.safety_state == tick.state.safety_state.value
    assert [(f.code, f.zone) for f in status.faults] == [
        (f.code.value, None if f.zone is None else f.zone.value) for f in tick.faults
    ]


def test_a_newer_version_with_extra_fields_is_still_read():
    """v16（#332 の `run_id`）など欄が増えた版も、2 つの欄が同じ形なら読む。"""
    newer = body("emergency", [STALL_TOP])
    newer["schema_version"] = 16
    newer["run_id"] = "00000000-0000-4000-8000-000000000000"
    status = read_fand_status(
        SequencedControlTrace(
            seq=1, ts_ms=1, tick_id=1, schema_version=16, trace_json=json.dumps(newer)
        )
    )
    assert status is not None
    assert status.safety_state == "emergency"
    assert status.faults == (FandFault("tach_stall", "top"),)


@pytest.mark.parametrize(
    "broken",
    [
        {"faults": []},
        {"state": {"safety_state": "normal"}},
        {"state": {"safety_state": "normal"}, "faults": None},
        {"state": {"safety_state": "panic"}, "faults": []},
        {"state": {"safety_state": "normal"}, "faults": {}},
        {"state": {"safety_state": "degraded"}, "faults": [{"zone": "top"}]},
        {"state": {"safety_state": "degraded"}, "faults": [{"code": "Tach Stall"}]},
        {"state": {"safety_state": "degraded"}, "faults": [{"code": "tach_stall", "zone": 3}]},
        {"state": "normal"},
    ],
)
def test_a_trace_of_an_unexpected_shape_is_unreadable(broken):
    """形が想定と違えば「異常なし」ではなく「読めない」（0148 §2.4）。"""
    trace = SequencedControlTrace(
        seq=1, ts_ms=1, tick_id=1, schema_version=99, trace_json=json.dumps(broken)
    )
    assert read_fand_status(trace) is None


def test_every_fault_code_has_a_japanese_label():
    """`FaultCode` を増やしたら `fault_labels` にも足す（見落とさない）。"""
    shipped = RuleSet.from_yaml(RULES_PATH)
    codes = {code.value for code in FaultCode}
    assert set(shipped.fand_emergency.fault_labels) == codes
    assert shipped.fand_degraded.fault_labels == shipped.fand_emergency.fault_labels


def test_the_cause_line_uses_closed_vocabulary_only():
    """fault の自由記述（値を含む英語）は載せない（0148 §2.6）。名前の無い code は code のまま。"""
    status = read_fand_status(
        SequencedControlTrace(
            seq=1,
            ts_ms=1,
            tick_id=1,
            schema_version=15,
            trace_json=json.dumps(
                body("emergency", [OVER_TEMP, STALL_TOP, STALL_TOP, {"code": "new_fault"}])
            ),
        )
    )
    assert status is not None
    labels = RuleSet.from_yaml(RULES_PATH).fand_emergency.fault_labels
    line = describe_faults(status.faults, labels)
    assert line == (
        "原因: 絶対温度の上限に達した（absolute_temperature_limit）、"
        "Fan の回転が止まった（tach_stall / top）、new_fault"
    )
    assert "ceiling" not in line and "rpm" not in line
    assert describe_faults((), labels) == "原因: 記録なし"


# ------------------------------------------------------------------ EMERGENCY


def test_emergency_fires_at_once_and_resolves_after_clear_s(engine, store):
    fand = Fand(store)
    transitions = run(engine, fand, 0, 5, body("normal"))
    assert states(transitions, EMERGENCY) == []
    transitions = run(engine, fand, 5, 8, body("emergency", [OVER_TEMP]))
    assert states(transitions, EMERGENCY) == ["pending", "firing"]
    fired = next(t for t in transitions if t.rule_id == EMERGENCY and t.state == "firing")
    assert fired.severity == "critical"
    assert fired.detail == "原因: 絶対温度の上限に達した（absolute_temperature_limit）"
    # 戻ってすぐには解かない（clear_s = 10）
    transitions = run(engine, fand, 8, 17, body("normal"))
    assert states(transitions, EMERGENCY) == []
    transitions = run(engine, fand, 17, 20, body("normal"))
    assert states(transitions, EMERGENCY) == ["resolved"]
    [alert] = [a for a in store.alerts(limit=10) if a.rule_id == EMERGENCY]
    assert alert.fired_ms == at(5) and alert.resolved_ms == at(18)


def test_emergency_does_not_also_raise_degraded(engine, store):
    """EMERGENCY の間は DEGRADED を数えない（2 通を同時に出さない。0148 §2.2）。"""
    fand = Fand(store)
    transitions = run(engine, fand, 0, 300, body("emergency", [STALL_TOP]))
    assert states(transitions, EMERGENCY) == ["pending", "firing"]
    assert states(transitions, DEGRADED) == []


def test_emergency_easing_to_degraded_resolves_then_counts_degraded(engine, store):
    fand = Fand(store)
    run(engine, fand, 0, 5, body("emergency", [OVER_TEMP]))
    transitions = run(engine, fand, 5, 200, body("degraded", [GPU_STALE]))
    assert states(transitions, EMERGENCY) == ["resolved"]
    assert states(transitions, DEGRADED) == ["pending", "firing"]
    [alert] = [a for a in store.alerts(limit=10) if a.rule_id == DEGRADED]
    assert alert.started_ms == at(5)
    assert alert.fired_ms == at(125)
    assert alert.detail == "原因: GPU の温度が読めない（gpu_telemetry_stale）"


# ------------------------------------------------------------------ DEGRADED


def test_short_degraded_is_not_notified(engine, store):
    """1 tick の fault でも fault_clear_hold_ms（30 s）は DEGRADED が残る。それでは鳴らさない。"""
    fand = Fand(store)
    transitions = run(engine, fand, 0, 31, body("degraded", [GPU_STALE]))
    transitions += run(engine, fand, 31, 200, body("normal"))
    assert states(transitions, DEGRADED) == ["pending"]
    assert [a for a in store.alerts(limit=10) if a.rule_id == DEGRADED] == []


def test_degraded_lasting_fire_after_s_fires_as_warning(engine, store):
    fand = Fand(store)
    transitions = run(engine, fand, 0, 121, body("degraded", [STALL_TOP]))
    assert states(transitions, DEGRADED) == ["pending", "firing"]
    fired = next(t for t in transitions if t.state == "firing")
    assert fired.severity == "warning"
    assert fired.detail == "原因: Fan の回転が止まった（tach_stall / top）"


def test_degraded_worsening_to_emergency_is_not_resolved(engine, store):
    """悪化で「通常の運転に戻りました」を出さない。EMERGENCY が解けて戻ってから解く。"""
    fand = Fand(store)
    run(engine, fand, 0, 130, body("degraded", [STALL_TOP]))
    transitions = run(engine, fand, 130, 200, body("emergency", [STALL_TOP]))
    assert states(transitions, DEGRADED) == []
    assert states(transitions, EMERGENCY) == ["pending", "firing"]
    transitions = run(engine, fand, 200, 215, body("normal"))
    assert states(transitions, DEGRADED) == ["resolved"]
    assert states(transitions, EMERGENCY) == ["resolved"]


def test_pending_degraded_restarts_after_emergency(engine, store):
    """DEGRADED 100 s → EMERGENCY 30 s → DEGRADED。戻ってから 120 s で発火。"""
    fand = Fand(store)
    run(engine, fand, 0, 100, body("degraded", [GPU_STALE]))
    transitions = run(engine, fand, 100, 130, body("emergency", [OVER_TEMP]))
    assert states(transitions, DEGRADED) == []
    assert [a for a in store.alerts(limit=10) if a.rule_id == DEGRADED] == []
    transitions = run(engine, fand, 130, 249, body("degraded", [GPU_STALE]))
    assert states(transitions, DEGRADED) == ["pending"]
    transitions = run(engine, fand, 249, 252, body("degraded", [GPU_STALE]))
    assert states(transitions, DEGRADED) == ["firing"]
    [alert] = [a for a in store.alerts(limit=10) if a.rule_id == DEGRADED]
    assert alert.started_ms == at(130)
    assert alert.fired_ms == at(250)


def test_startup_is_not_an_alert(engine, store):
    """起動の takeover（全 Max）は正常な段階（0148 §2.2）。"""
    fand = Fand(store)
    transitions = run(engine, fand, 0, 300, body("startup"))
    assert transitions == []


# ------------------------------------------------------------ trace の途絶（0148 §2.2 / §2.8）


def test_no_trace_at_all_is_not_evaluated(engine, store):
    """fand を導入していない機械（trace が 1 行も無い）では鳴らさない。"""
    store.clock.advance_to_ms(at(3600))  # type: ignore[attr-defined]
    assert engine.on_tick() == []
    assert list(store.alerts(limit=10)) == []


def test_silence_fires_after_silence_s_and_resolves_after_clear_s(engine, store):
    fand = Fand(store)
    run(engine, fand, 0, 10, body("normal"))
    transitions = run(engine, fand, 10, 69, None)
    assert states(transitions, SILENT) == []
    transitions = run(engine, fand, 69, 75, None)
    assert states(transitions, SILENT) == ["pending", "firing"]
    fired = next(t for t in transitions if t.state == "firing")
    assert fired.severity == "warning"
    assert fired.detail == f"最後の decision trace から 60 秒（tick {fand.tick_id}）"
    # 1 行だけ届いても解かない。読める新しい trace が 30 s 続いたら解く
    transitions = run(engine, fand, 75, 104, body("normal"))
    assert states(transitions, SILENT) == []
    transitions = run(engine, fand, 104, 106, body("normal"))
    assert states(transitions, SILENT) == ["resolved"]


def test_an_unreadable_trace_counts_as_silence(engine, store):
    """形の違う trace（将来の版など）を黙って「異常なし」にしない（0148 §2.4）。"""
    fand = Fand(store)
    fand.write(at(0), {"state": {"safety_state": "panic"}}, version=99)
    store.clock.advance_to_ms(at(0))  # type: ignore[attr-defined]
    transitions = engine.on_tick()
    assert states(transitions, SILENT) == ["pending", "firing"]
    assert transitions[-1].detail == "最新の decision trace を読めない（schema_version 99・tick 1）"


def test_a_stale_trace_keeps_a_firing_emergency_and_drops_a_pending_degraded(engine, store):
    """trace が古い・読めない間は、発火中は保ち、発火待ちは取り消す（0148 §2.3）。"""
    fand = Fand(store)
    run(engine, fand, 0, 5, body("emergency", [OVER_TEMP]))
    transitions = run(engine, fand, 5, 600, None)
    assert states(transitions, EMERGENCY) == []
    assert states(transitions, SILENT) == ["pending", "firing"]
    assert [a.state.value for a in store.alerts(limit=10) if a.rule_id == EMERGENCY] == ["firing"]

    other = Fand(store)
    other.tick_id = fand.tick_id
    # 50 s の DEGRADED の後に止まる。最後の trace が古くなる 709 s で、720 s の発火の前に取り消す
    run(engine, other, 600, 650, body("degraded", [GPU_STALE]))
    transitions = run(engine, other, 650, 900, None)
    assert [a for a in store.alerts(limit=10) if a.rule_id == DEGRADED] == [], "発火待ちは取り消す"
    assert states(transitions, DEGRADED) == []


def test_a_trace_without_faults_does_not_resolve_a_firing_emergency(engine, store):
    """`faults` の無い trace は読めない。`normal` でも EMERGENCY を解かず、途絶とする。"""
    fand = Fand(store)
    run(engine, fand, 0, 5, body("emergency", [OVER_TEMP]))
    broken = body("normal")
    del broken["faults"]
    transitions = run(engine, fand, 5, 60, broken)
    assert states(transitions, EMERGENCY) == []
    assert states(transitions, SILENT) == ["pending", "firing"]
    assert [a.state.value for a in store.alerts(limit=10) if a.rule_id == EMERGENCY] == ["firing"]


def test_one_trace_then_stop_does_not_resolve_silence(engine, store):
    """fand が 1 行だけ書いて止まる。同じ行を読み続けても途絶の解除は進まない（0148）。"""
    fand = Fand(store)
    run(engine, fand, 0, 5, body("normal"))
    assert states(run(engine, fand, 5, 70, None), SILENT) == ["pending", "firing"]
    fand.write(at(70), body("normal"))
    transitions = run(engine, fand, 70, 130, None)
    assert SILENT not in [t.rule_id for t in transitions if t.state == "resolved"]
    assert [a.state.value for a in store.alerts(limit=10) if a.rule_id == SILENT] == ["firing"]


def test_one_normal_trace_then_stop_does_not_resolve_emergency(engine, store):
    """非 EMERGENCY の 1 行だけで、発火中の EMERGENCY を解かない（同じ行の繰り返し）。"""
    fand = Fand(store)
    run(engine, fand, 0, 5, body("emergency", [OVER_TEMP]))
    fand.write(at(5), body("normal"))
    transitions = run(engine, fand, 5, 120, None)
    assert states(transitions, EMERGENCY) == []
    assert [a.state.value for a in store.alerts(limit=10) if a.rule_id == EMERGENCY] == ["firing"]


def test_advancing_traces_resolve_after_clear_s_of_trace_time(engine, store):
    """新しい行が clear_s の幅で届き続けたら解く（trace の時刻で測る）。"""
    fand = Fand(store)
    run(engine, fand, 0, 5, body("emergency", [OVER_TEMP]))
    # 評価は 1 s ごとだが、trace は 5 s ごとにしか来ない。解除は trace の時刻で 10 s の幅
    transitions = []
    for seconds in range(5, 40):
        if seconds % 5 == 0:
            fand.write(at(seconds), body("normal"))
        store.clock.advance_to_ms(at(seconds))  # type: ignore[attr-defined]
        transitions += engine.on_tick()
    resolved = [t for t in transitions if t.rule_id == EMERGENCY and t.state == "resolved"]
    assert len(resolved) == 1
    [alert] = [a for a in store.alerts(limit=10) if a.rule_id == EMERGENCY]
    assert alert.resolved_ms == at(15)


def test_degraded_fire_after_s_is_measured_in_trace_time(engine, store):
    """DEGRADED の 1 行を書いて止まっても、同じ行の繰り返しで 120 s の待ちは満ちない。"""
    fand = Fand(store)
    run(engine, fand, 0, 70, body("degraded", [GPU_STALE]))
    transitions = run(engine, fand, 70, 129, None)
    assert states(transitions, DEGRADED) == [], "壁時計で 120 s を過ぎても発火しない"
    [pending] = [a for a in store.alerts(limit=10) if a.rule_id == DEGRADED]
    assert pending.fired_ms is None
    run(engine, fand, 129, 140, None)
    assert [a for a in store.alerts(limit=10) if a.rule_id == DEGRADED] == [], "古くなって取り消す"


def test_a_trace_from_the_future_counts_as_fresh(engine, store):
    """時計が戻って trace の時刻が評価時刻より先にある（年齢が負）。新しいとみなす。"""
    fand = Fand(store)
    fand.write(at(120), body("emergency", [OVER_TEMP]))
    store.clock.advance_to_ms(at(0))  # type: ignore[attr-defined]
    transitions = engine.on_tick()
    assert states(transitions, EMERGENCY) == ["pending", "firing"]
    assert states(transitions, SILENT) == []


def test_a_restart_keeps_the_firing_alert_without_notifying_again(store, rule_set):
    """取り込みを再起動しても、発火中のアラートを復元して出し直さない（0148 §2.8）。"""
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    first = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    fand = Fand(store)
    run(first, fand, 0, 5, body("normal"))
    assert states(run(first, fand, 5, 70, None), SILENT) == ["pending", "firing"]
    restarted = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    transitions = run(restarted, fand, 70, 100, None)
    assert transitions == []
    transitions = run(restarted, fand, 100, 140, body("normal"))
    assert states(transitions, SILENT) == ["resolved"]


def test_disabled_silence_rule_is_quiet_while_fand_is_stopped(store, rule_set):
    """意図して止めるときは `fand_trace_silent.enabled: false`（0148 §2.8）。"""
    quiet = rule_set.model_copy(
        update={
            "fand_trace_silent": rule_set.fand_trace_silent.model_copy(update={"enabled": False})
        }
    )
    engine = Engine(
        rules=quiet, catalog=MetricCatalog.from_yaml(METRICS_PATH), store=store, clock=store.clock
    )
    fand = Fand(store)
    run(engine, fand, 0, 5, body("normal"))
    assert run(engine, fand, 5, 600, None) == []


def test_traces_are_not_read_unless_live(store, rule_set):
    """再生・時間圧縮では trace を読まない（0148 §2.1）。"""
    engine = Engine(
        rules=rule_set,
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        store=store,
        clock=store.clock,
        read_control_traces=False,
    )
    fand = Fand(store)
    assert run(engine, fand, 0, 10, body("emergency", [OVER_TEMP])) == []
    assert run(engine, fand, 10, 200, None) == []


@pytest.mark.parametrize(
    ("overrides", "live"),
    [
        ({"scenario": "ramp"}, True),
        ({"scenario": "ramp", "speed": 60.0}, False),
        ({"source": "serial"}, True),
    ],
)
def test_build_reads_traces_only_in_real_time(tmp_path, overrides, live):
    from test_daemon import config

    daemon = build(config(tmp_path, **overrides))
    try:
        assert daemon._engine is not None
        assert daemon._engine.read_control_traces is live
    finally:
        daemon.store.close()


# ------------------------------------------------------------------ 通知（0142 / 0013 / 0135）


class Recorder:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)


def daemon_for(tmp_path, rule_set, start_ms: int) -> tuple[Daemon, SqliteStore, Recorder]:
    from coldaisle.store import QualityRules

    rules = QualityRules.from_yaml(QUALITY_RULES_PATH)
    clock = SimulatedClock(start_ms)
    store = SqliteStore(tmp_path / "e2e.db", rules=rules, clock=clock)
    recorder = Recorder()
    daemon = Daemon(
        source=object(),  # type: ignore[arg-type]
        store=store,
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=clock),
        engine=Engine(
            rules=rule_set,
            catalog=MetricCatalog.from_yaml(METRICS_PATH),
            store=store,
            clock=clock,
        ),
        notifier=Router(
            config=NotifyConfig.from_yaml(NOTIFY_PATH, environ={}),
            notifiers={"slack": recorder, "stdout": Recorder()},  # type: ignore[dict-item]
            clock=clock,
        ),
    )
    return daemon, store, recorder


def drive(daemon: Daemon, store: SqliteStore, fand: Fand, start: float, stop: float, trace, base):
    seconds = start
    while seconds < stop:
        if trace is not None:
            fand.write(at(seconds, base), trace)
        store.clock.advance_to_ms(at(seconds, base))  # type: ignore[attr-defined]
        daemon._tick()
        seconds += 1.0


def test_emergency_reaches_slack_with_japanese_text(tmp_path, rule_set):
    daemon, store, recorder = daemon_for(tmp_path, rule_set, NOON_MS)
    try:
        fand = Fand(store)
        drive(daemon, store, fand, 0, 5, body("emergency", [OVER_TEMP, STALL_TOP]), NOON_MS)
        drive(daemon, store, fand, 5, 20, body("normal"), NOON_MS)
        [fired, resolved] = recorder.sent
        assert fired.severity is AlertSeverity.CRITICAL
        lines = fired.as_text().splitlines()
        assert lines[0] == "🔴 [重大] Fan 制御が緊急状態です（すべての Fan を最大で回しています）"
        assert lines[1] == (
            "原因: 絶対温度の上限に達した（absolute_temperature_limit）、"
            "Fan の回転が止まった（tach_stall / top）"
        )
        assert "journalctl -u coldaisle-fand" in lines[2]
        assert lines[-1] == "FAND_EMERGENCY"
        assert resolved.as_text().splitlines()[0] == "✅ [重大 解除] Fan 制御が緊急状態を抜けました"
    finally:
        store.close()


def test_at_night_only_emergency_is_sent(tmp_path, rule_set):
    """夜間は critical だけ（0001 D-04 / 0135 §2.3）。DEGRADED・途絶の warning は送らない。"""
    daemon, store, recorder = daemon_for(tmp_path, rule_set, NIGHT_MS)
    try:
        fand = Fand(store)
        drive(daemon, store, fand, 0, 200, body("degraded", [STALL_TOP]), NIGHT_MS)
        drive(daemon, store, fand, 200, 205, body("emergency", [STALL_TOP]), NIGHT_MS)
        drive(daemon, store, fand, 205, 300, None, NIGHT_MS)
        assert [(n.rule_id, n.state) for n in recorder.sent] == [(EMERGENCY, "firing")]
        fired_rules = {a.rule_id for a in store.alerts(limit=10) if a.fired_ms is not None}
        assert fired_rules == {EMERGENCY, DEGRADED, SILENT}, "発火は DB に残る（送らないだけ）"
    finally:
        store.close()


def test_display_names_use_the_detail_as_the_value_line():
    names = DisplayNames.build(
        rules=RuleSet.from_yaml(RULES_PATH),
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        context_always=("air.room",),
    )
    notification = Notification(
        rule_id=SILENT,
        severity=AlertSeverity.WARNING,
        state="firing",
        metric=None,
        value=61.0,
        detail="最後の decision trace から 61 秒（tick 42）",
        dashboard_url="http://127.0.0.1:8000/",
        context={"air.room": 26.1},
        names=names,
    )
    assert notification.as_text().splitlines()[:2] == [
        "🟠 [警告] Fan 制御（fand）の記録が途絶えています",
        "最後の decision trace から 61 秒（tick 42）・室温 26.1 °C",
    ]
