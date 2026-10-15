"""12V-2x6 コネクタの温度の 2 段の通知。決定記録 0145

判定は取り込みのルールエンジンで、`coldaisle-telemetry` が DB へ書いた hwmon の最新値
（`board.connector_12v2x6`）を評価時刻で読んで行う。最新値が無い・ok でない
（stale・suspect を含む）周期は評価せず、発火待ちは取り消し、発火中は保つ（0131 §2.3）。

実機は要らない（AGENTS.md ルール 7）。値は DB へ直接書くか、一時ディレクトリの sysfs を
読む収集（`HwmonAdapter` → `InternalTelemetryCollector`）で書く。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from coldaisle.clock import SimulatedClock
from coldaisle.daemon import Daemon
from coldaisle.ingest import Calibration, Normalizer
from coldaisle.ingest.protocol import RawMessage, RawSample
from coldaisle.internal_telemetry import (
    HwmonAdapter,
    HwmonConfig,
    InternalTelemetryCollector,
    InternalTelemetryConfig,
)
from coldaisle.llm_exclusions import LlmExclusions
from coldaisle.metrics import MetricCatalog
from coldaisle.rules import Engine, RuleSet
from coldaisle.store import Quality, QualityRules, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH
from test_gpu_fan_service_alert import RecordingNotifier

RULES_PATH = CONFIG_DIR / "rules.yaml"
METRICS_PATH = CONFIG_DIR / "metrics.yaml"
NOW_MS = 1_787_616_000_000
STEP_S = 2.5
"""`coldaisle-telemetry` の周期（config/internal-telemetry.yaml の interval_ms と同じ）。"""

CONNECTOR = "board.connector_12v2x6"
HIGH = "CONNECTOR_TEMP_HIGH"
CRITICAL = "CONNECTOR_TEMP_CRITICAL"


@pytest.fixture
def rule_set() -> RuleSet:
    """本番の rules.yaml。基板の無音（SENSOR_FAULT）だけは切る（ここでは基板を流さない）。"""
    shipped = RuleSet.from_yaml(RULES_PATH)
    return shipped.model_copy(
        update={"sensor_fault": shipped.sensor_fault.model_copy(update={"enabled": False})}
    )


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "connector.db", rules=rules, clock=SimulatedClock(NOW_MS)) as db:
        yield db


@pytest.fixture
def engine(store, rule_set) -> Engine:
    return Engine(
        rules=rule_set,
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        store=store,
        clock=store.clock,
    )


def at(seconds: float) -> int:
    return NOW_MS + int(seconds * 1000)


def write(store: SqliteStore, seconds: float, value: float | None, quality: Quality) -> None:
    """`coldaisle-telemetry` が書く行（別プロセス）を DB へ直接入れる。"""
    store.insert_sample(
        Sample(
            ts_ms=at(seconds),
            readings=(Reading(metric=CONNECTOR, value=value, quality=quality),),
        )
    )


def tick(engine: Engine, store: SqliteStore, seconds: float) -> list:
    store.clock.advance_to_ms(at(seconds))  # type: ignore[attr-defined]
    return engine.on_tick()


def run(
    engine: Engine,
    store: SqliteStore,
    start: float,
    stop: float,
    value: float | None,
    *,
    quality: Quality = Quality.OK,
    written: bool = True,
) -> list:
    """`start` から `stop` 秒（含まない）まで、周期ごとに書いて評価する。

    `written=False` は書かない（収集が止まった）。
    """
    transitions = []
    seconds = start
    while seconds < stop:
        if written:
            write(store, seconds, value, quality if value is not None else Quality.MISSING)
        transitions += tick(engine, store, seconds)
        seconds += STEP_S
    return transitions


def rows(store: SqliteStore, rule_id: str) -> list:
    return [alert for alert in store.alerts(limit=100) if alert.rule_id == rule_id]


def states(transitions: list, rule_id: str) -> list[str]:
    return [t.state for t in transitions if t.rule_id == rule_id]


# ---------------------------------------------------------------- 設定


def test_shipped_rules_have_the_proposed_provisional_values(rule_set):
    """0145 §2.2 の値（provisional）。値は config/rules.yaml にだけ置く。"""
    high = rule_set.connector_temp_high
    assert (high.enabled, high.severity.value, high.metric) == (True, "warning", CONNECTOR)
    assert (high.threshold, high.clear, high.fire_after_s) == (65, 62, 60)
    critical = rule_set.connector_temp_critical
    assert (critical.enabled, critical.severity.value, critical.metric) == (
        True,
        "critical",
        CONNECTOR,
    )
    assert (critical.threshold, critical.clear, critical.fire_after_s) == (75, 72, 10)


def test_critical_is_below_the_critical_safety_ceiling(rule_set):
    """0110 §2.2 の 80 °C（本番の `telemetry.t_sensor.absolute_ceiling_c`）の手前で知らせる。

    `safety.yaml` は導入先にだけ置くので、ここでは 0110 の値と比べる（0145 §5 の 3）。
    """
    assert rule_set.connector_temp_high.threshold < rule_set.connector_temp_critical.threshold < 80


@pytest.mark.parametrize("name", ["connector_temp_high", "connector_temp_critical"])
@pytest.mark.parametrize("delta", [0.0, 1.0])
def test_clear_must_be_below_the_threshold(tmp_path, name, delta):
    """解除が発火の外側にあると、発火した瞬間に解除条件も満たす。"""
    loaded = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    loaded["rules"][name]["clear"] = loaded["rules"][name]["threshold"] + delta
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(loaded, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="clear は threshold より小さく"):
        RuleSet.from_yaml(path)


def test_unknown_metric_is_rejected_at_startup(tmp_path, store):
    loaded = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    loaded["rules"]["connector_temp_high"]["metric"] = "board.connector_12v2x7"
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(loaded, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match=r"board\.connector_12v2x7"):
        Engine(
            rules=RuleSet.from_yaml(path),
            catalog=MetricCatalog.from_yaml(METRICS_PATH),
            store=store,
            clock=store.clock,
        )


# ---------------------------------------------------------------- 警告（65 °C・60 s）


def test_warning_fires_after_sixty_seconds_at_or_above_65(engine, store):
    """「以上」で成立する（0145 §2.1）。65.0 °C ちょうどで数え始める。"""
    transitions = run(engine, store, 0, 60, 65.0)
    assert states(transitions, HIGH) == ["pending"]
    transitions = run(engine, store, 60, 62.5, 65.0)
    assert states(transitions, HIGH) == ["firing"]
    assert states(transitions, CRITICAL) == []
    [row] = rows(store, HIGH)
    assert (row.state.value, row.started_ms, row.fired_ms) == ("firing", at(0), at(60))
    assert (row.metric, row.threshold, row.trigger_value) == (CONNECTOR, 65.0, 65.0)
    assert row.severity.value == "warning"


def test_warning_does_not_fire_below_65(engine, store):
    """600 W の定常（48〜51 °C）や 64.9 °C では鳴らない。"""
    assert run(engine, store, 0, 300, 64.9) == []
    assert run(engine, store, 300, 600, 51.0) == []
    assert store.alerts(limit=100) == ()


def test_a_warning_spike_shorter_than_sixty_seconds_leaves_no_history(engine, store):
    transitions = run(engine, store, 0, 30, 66.0)
    transitions += run(engine, store, 30, 120, 60.0)
    assert states(transitions, HIGH) == ["pending"]  # pending は配らない（daemon が捨てる）
    assert rows(store, HIGH) == []


def test_warning_uses_hysteresis_to_clear(engine, store):
    run(engine, store, 0, 62.5, 66.0)
    # 62〜65 °C は解除しない（発火の閾値の内側）
    assert run(engine, store, 62.5, 120, 62.0) == []
    assert rows(store, HIGH)[0].state.value == "firing"
    transitions = run(engine, store, 120, 122.5, 61.9)
    assert states(transitions, HIGH) == ["resolved"]
    assert rows(store, HIGH)[0].state.value == "resolved"


# ---------------------------------------------------------------- 重大（75 °C・10 s）


def test_critical_fires_after_ten_seconds_and_the_warning_follows(engine, store):
    transitions = run(engine, store, 0, 10, 75.0)
    assert states(transitions, CRITICAL) == ["pending"]
    transitions = run(engine, store, 10, 12.5, 75.0)
    assert states(transitions, CRITICAL) == ["firing"]
    firing = next(t for t in transitions if t.rule_id == CRITICAL)
    assert (firing.severity, firing.metric, firing.value) == ("critical", CONNECTOR, 75.0)
    [row] = rows(store, CRITICAL)
    assert (row.started_ms, row.fired_ms, row.threshold) == (at(0), at(10), 75.0)

    # 警告は別のルールで、60 s で発火する（2 通。0145 §2.2）
    transitions = run(engine, store, 12.5, 62.5, 76.0)
    assert states(transitions, HIGH) == ["firing"]
    assert rows(store, HIGH)[0].fired_ms == at(60)


def test_a_single_reading_at_75_does_not_page(engine, store):
    """1 回だけの読み違いで夜間の critical を鳴らさない（0145 §2.2 の 10 s）。"""
    transitions = run(engine, store, 0, 2.5, 78.0)
    transitions += run(engine, store, 2.5, 60, 50.0)
    assert states(transitions, CRITICAL) == ["pending"]
    assert rows(store, CRITICAL) == []


def test_critical_clears_below_72_and_the_warning_stays(engine, store):
    run(engine, store, 0, 62.5, 76.0)
    assert {rows(store, HIGH)[0].state.value, rows(store, CRITICAL)[0].state.value} == {"firing"}
    assert run(engine, store, 62.5, 90, 72.0) == [], "72 °C ちょうどは解除しない"
    transitions = run(engine, store, 90, 92.5, 71.9)
    assert states(transitions, CRITICAL) == ["resolved"]
    assert states(transitions, HIGH) == [], "警告は 62 °C まで残る（まだ高い）"
    assert rows(store, HIGH)[0].state.value == "firing"


# ---------------------------------------------------------------- 欠け・stale・suspect（0145 §2.3）


def test_pending_is_cancelled_by_a_stale_gap_and_counted_again_from_recovery(engine, store):
    """成立 → 収集が止まる → 回復の直後に成立、だけで「60 s 続いた」とみなさない。"""
    transitions = run(engine, store, 0, 50, 70.0)
    assert states(transitions, HIGH) == ["pending"]
    # 収集が止まる。最後の行（47.5 s）は 10 s で stale（config/quality.yaml）
    assert run(engine, store, 50, 120, None, written=False) == []
    assert rows(store, HIGH) == [], "発火待ちは取り消して行を消す"

    transitions = run(engine, store, 120, 122.5, 70.0)
    assert states(transitions, HIGH) == ["pending"], "発火しない（空白の前を数えない）"
    assert rows(store, HIGH)[0].started_ms == at(120)
    assert states(run(engine, store, 122.5, 180, 70.0), HIGH) == []
    assert states(run(engine, store, 180, 182.5, 70.0), HIGH) == ["firing"]


def test_stale_threshold_comes_from_quality_yaml(engine, store):
    """stale の閾値はルールに持たない（Store の唯一の情報源。0004 §2.12）。"""
    stale_after_s = QualityRules.from_yaml(QUALITY_RULES_PATH).stale_after_ms / 1000
    run(engine, store, 0, 2.5, 70.0)
    tick(engine, store, stale_after_s - 0.5)
    assert rows(store, HIGH)[0].state.value == "pending"
    tick(engine, store, stale_after_s)
    assert rows(store, HIGH) == []


@pytest.mark.parametrize(
    ("value", "quality"),
    [(None, Quality.MISSING), (-40.0, Quality.SUSPECT)],
    ids=["missing", "suspect-disconnected"],
)
def test_missing_or_suspect_reading_cancels_pending(engine, store, value, quality):
    """断線（0 °C 未満は suspect。0110 §2.4）を高温と取り違えず、発火待ちも数え直す。"""
    run(engine, store, 0, 50, 76.0)
    assert {rows(store, HIGH)[0].state.value, rows(store, CRITICAL)[0].state.value} == {
        "pending",
        "firing",
    }
    run(engine, store, 50, 52.5, value, quality=quality)
    assert rows(store, HIGH) == [], "警告の発火待ちは取り消す"
    assert rows(store, CRITICAL)[0].state.value == "firing", "発火中は保つ"


def test_a_suspect_reading_alone_never_fires(engine, store):
    """値が閾値より上でも ok でなければ評価しない（品質は収集が決める）。"""
    assert run(engine, store, 0, 120, 90.0, quality=Quality.SUSPECT) == []
    assert store.alerts(limit=100) == ()


def test_firing_survives_a_gap_and_clears_after_recovery(engine, store):
    run(engine, store, 0, 12.5, 76.0)
    assert run(engine, store, 12.5, 60, None, written=False) == []
    assert rows(store, CRITICAL)[0].state.value == "firing", "stale で解除しない"
    transitions = run(engine, store, 60, 62.5, 50.0)
    assert states(transitions, CRITICAL) == ["resolved"]


def test_board_silence_does_not_stop_connector_evaluation(store, rule_set):
    """基板の SENSOR_FAULT（0139）はコネクタのルールの発火待ちを取り消さない（0145 §2.3）。"""
    with_fault = rule_set.model_copy(
        update={"sensor_fault": rule_set.sensor_fault.model_copy(update={"enabled": True})}
    )
    engine = Engine(
        rules=with_fault,
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        store=store,
        clock=store.clock,
    )
    engine.begin(at(0))
    transitions = run(engine, store, 0, 62.5, 66.0)
    assert states(transitions, "SENSOR_FAULT") == ["pending", "firing"]
    assert states(transitions, HIGH) == ["pending", "firing"]
    assert rows(store, HIGH)[0].fired_ms == at(60)


# ---------------------------------------------------------------- 再起動


def test_pending_from_before_a_restart_is_not_counted(store, rule_set):
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    first = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    run(first, store, 0, 50, 70.0)
    assert rows(store, HIGH)[0].state.value == "pending"

    restarted = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    assert rows(store, HIGH) == []
    transitions = run(restarted, store, 300, 302.5, 70.0)
    assert states(transitions, HIGH) == ["pending"]


def test_firing_from_before_a_restart_is_kept_and_resolved(store, rule_set):
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    first = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    run(first, store, 0, 12.5, 76.0)
    restarted = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    assert rows(store, CRITICAL)[0].state.value == "firing"
    transitions = run(restarted, store, 300, 302.5, 50.0)
    assert states(transitions, CRITICAL) == ["resolved"]
    assert len(rows(store, CRITICAL)) == 1


def test_disabled_rules_are_not_evaluated(store, rule_set):
    disabled = rule_set.model_copy(
        update={
            name: getattr(rule_set, name).model_copy(update={"enabled": False})
            for name in ("connector_temp_high", "connector_temp_critical")
        }
    )
    engine = Engine(
        rules=disabled,
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        store=store,
        clock=store.clock,
    )
    assert run(engine, store, 0, 120, 90.0) == []


# ---------------------------------------------------------------- 一括再生（0012 §2.2）


def test_bulk_replay_reads_the_value_at_the_evaluation_time(engine, store):
    """時計は止まったまま、評価時刻はサンプルの時刻に従う。先の時刻の行を読まない。"""
    for step in range(0, 6):
        write(store, step * STEP_S, 76.0, Quality.OK)
    write(store, 1000, 40.0, Quality.OK)
    transitions = []
    for step in range(0, 6):
        transitions += engine.on_sample(
            Sample(
                ts_ms=at(step * STEP_S),
                readings=(Reading(metric="air.room", value=26.0, quality=Quality.OK),),
            )
        )
    assert store.clock.now_ms() == NOW_MS, "時計は進めていない"
    assert states(transitions, CRITICAL) == ["pending", "firing"]
    assert rows(store, CRITICAL)[0].fired_ms == at(10)


# ---------------------------------------------------------------- 偽の sysfs から通知まで


def t_sensor_hwmon(root: Path) -> HwmonAdapter:
    """本番の設定（asusec の T_Sensor・下限 0 °C）を、一時ディレクトリの sysfs で読む。"""
    config = InternalTelemetryConfig.from_yaml(
        CONFIG_DIR / "internal-telemetry.yaml",
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
    )
    t_sensor = next(item for item in config.hwmon.sensors if item.metric == CONNECTOR)
    device = root / "hwmon0"
    device.mkdir(parents=True)
    (device / "name").write_text("asusec\n", encoding="utf-8")
    (device / "temp1_label").write_text("T_Sensor\n", encoding="utf-8")
    return HwmonAdapter(HwmonConfig(enabled=True, root=root, sensors=(t_sensor,)))


@dataclass
class BoardWithConnector:
    """基板のサンプルを流しつつ、別の接続で `coldaisle-telemetry` と同じ書き込みをする。

    `millidegrees` は周期ごとの `temp1_input`（hwmon の単位）。
    """

    db: Path
    rules: QualityRules
    clock: SimulatedClock
    sysfs: Path
    millidegrees: list[int]
    collector_clock: SimulatedClock = field(default_factory=lambda: SimulatedClock(NOW_MS))

    def stream(self) -> Iterator[RawMessage]:
        collector = InternalTelemetryCollector((t_sensor_hwmon(self.sysfs),), self.collector_clock)
        with SqliteStore(self.db, rules=self.rules, clock=self.collector_clock) as telemetry:
            for index, raw in enumerate(self.millidegrees):
                at_ms = at(index * STEP_S)
                (self.sysfs / "hwmon0" / "temp1_input").write_text(f"{raw}\n", encoding="utf-8")
                self.collector_clock.advance_to_ms(at_ms)
                telemetry.insert_sample(collector.collect().sample)
                self.clock.advance_to_ms(at_ms)
                yield RawSample(seq=index, up=index, channels={"room_temp": 26.0})


def test_hot_connector_reaches_the_notifier_as_critical(tmp_path, rules, rule_set):
    """偽の sysfs →（収集）→ DB →（取り込みのルールエンジン）→ 通知（critical）。"""
    db = tmp_path / "e2e.db"
    clock = SimulatedClock(NOW_MS)
    store = SqliteStore(db, rules=rules, clock=clock)
    notifier = RecordingNotifier()
    # 断線（負の値）が続いた後に 76 °C。断線の間は何も出さない
    readings = [-40_000] * 8 + [76_000] * 8
    daemon = Daemon(
        source=BoardWithConnector(db, rules, clock, tmp_path / "sys", readings),  # type: ignore[arg-type]
        store=store,
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=clock),
        engine=Engine(
            rules=rule_set,
            catalog=MetricCatalog.from_yaml(METRICS_PATH),
            store=store,
            clock=clock,
        ),
        notifier=notifier,  # type: ignore[arg-type]
        llm_exclusions=LlmExclusions.from_yaml(CONFIG_DIR / "report.yaml"),
    )
    daemon._request_explanation = lambda transition: None  # type: ignore[method-assign]
    try:
        daemon.run()
        sent = [(n.rule_id, n.state, n.severity.value) for n in notifier.sent]
        assert sent == [(CRITICAL, "firing", "critical")]
        [row] = rows(store, CRITICAL)
        assert (row.started_ms, row.fired_ms) == (at(8 * STEP_S), at(8 * STEP_S + 10))
        assert store.latest(at_ms=at(0))[CONNECTOR].quality is Quality.SUSPECT
    finally:
        store.close()
