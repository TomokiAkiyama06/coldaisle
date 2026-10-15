"""欠けの検知で発火待ちを数え直す（決定記録 0139 / #302）。

しきい値・範囲の 5 つのルール（`recirculation`・`intake_high`・`airflow_degraded`・
`room_high`・`humidity_out_of_range`）は、入力の欠けで `SENSOR_MISSING`（連続 5 サンプル）が、
またはサンプルの無音で `SENSOR_FAULT`（30 s）が**発火したときだけ**発火待ちを取り消す。

守る線は 2 つ。

1. **観測していない時間を「条件が続いた」として通知しない**（欠け・無音をまたいで発火しない）
2. **黙って発火を遅らせない**（取り消すときは必ず欠けを知らせるアラートが発火中）

実機なし。2.5 s のサンプル列と SimulatedClock で確かめる（AGENTS.md ルール 7）。
"""

from __future__ import annotations

import pytest

from coldaisle.ai import Explainer, ToolRegistry, UnavailableProvider
from coldaisle.clock import SimulatedClock
from coldaisle.llm_exclusions import LlmExclusions
from coldaisle.metrics import MetricCatalog
from coldaisle.rules import Engine, RuleSet
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR

RULES_PATH = CONFIG_DIR / "rules.yaml"
METRICS_PATH = CONFIG_DIR / "metrics.yaml"
T0 = 1_787_616_000_000
STEP_MS = 2_500
"""ファームウェアの送信周期（`INTERVAL_MS = 2500`。0139 §1.3）。"""

HOT_INTAKE = {"air.gpu_intake": 42.0}  # intake_high（40 ℃）を超える
RECIRC = {"air.front_intake": 33.0, "air.room": 26.0}  # d.intake_rise = 7.0（> 5.0）


@pytest.fixture
def rule_set() -> RuleSet:
    return RuleSet.from_yaml(RULES_PATH)


@pytest.fixture
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(METRICS_PATH)


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "gap.db", rules=rules, clock=SimulatedClock(T0)) as opened:
        yield opened


@pytest.fixture
def engine(store, rule_set, catalog) -> Engine:
    return make_engine(store, rule_set, catalog)


def make_engine(store: SqliteStore, rule_set: RuleSet, catalog: MetricCatalog) -> Engine:
    """エンジンを作る。**作るたびに DB から状態を復元する**（再起動と同じ）。"""
    return Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)


def with_disabled(rule_set: RuleSet, *names: str) -> RuleSet:
    return rule_set.model_copy(
        update={
            name: getattr(rule_set, name).model_copy(update={"enabled": False}) for name in names
        }
    )


def at(index: float) -> int:
    """`index` 番目のサンプルの時刻（2.5 s 周期）。"""
    return T0 + int(index * STEP_MS)


def sec(seconds: float) -> int:
    return T0 + int(seconds * 1000)


def sample(ts_ms: int, metrics: dict[str, float | None]) -> Sample:
    """`None` は欠け（品質 missing）。**欄を省くときは鍵ごと渡さない。**"""
    return Sample(
        ts_ms=ts_ms,
        readings=tuple(
            Reading(
                metric=metric,
                value=value,
                quality=Quality.OK if value is not None else Quality.MISSING,
            )
            for metric, value in metrics.items()
        ),
    )


def feed(engine: Engine, ts_ms: int, metrics: dict[str, float | None]) -> list:
    return engine.on_sample(sample(ts_ms, metrics))


def tick(engine: Engine, store: SqliteStore, ts_ms: int) -> list:
    store.clock.advance_to_ms(ts_ms)
    return engine.on_tick()


_ANY = object()


def rows(store: SqliteStore, rule_id: str, metric: object = _ANY) -> list:
    """`rule_id`（と `metric`）の行。`metric` を省けばメトリクスを問わない。"""
    return [
        alert
        for alert in store.alerts(limit=10_000)
        if alert.rule_id == rule_id and (metric is _ANY or alert.metric == metric)
    ]


def only(store: SqliteStore, rule_id: str, metric: object = _ANY):
    found = rows(store, rule_id, metric)
    assert len(found) == 1, found
    return found[0]


def missing(metric: str) -> dict[str, float | None]:
    return {metric: None}


# ---------------------------------------------------------------- 2.3.1 センサーの欠け


def test_four_missing_samples_keep_the_pending(engine, store):
    """1〜4 サンプルの欠けでは保つ。**最初の成立から 120 s で発火する（今と同じ）。**"""
    for index in range(0, 49):
        values = missing("air.gpu_intake") if 20 <= index <= 23 else HOT_INTAKE
        feed(engine, at(index), values)

    assert rows(store, "SENSOR_MISSING") == [], "4 回では鳴らない"
    fired = only(store, "INTAKE_HIGH")
    assert fired.state == "firing"
    assert fired.started_ms == T0
    assert fired.fired_ms == sec(120)


def test_five_missing_samples_cancel_and_restart_the_count(engine, store):
    """5 回目の欠けで `SENSOR_MISSING` が発火した**同じサンプル**で取り消す。遷移は配らない。"""
    for index in range(0, 20):
        feed(engine, at(index), HOT_INTAKE)
    for index in range(20, 24):
        feed(engine, at(index), missing("air.gpu_intake"))
    assert only(store, "INTAKE_HIGH").state == "pending"

    transitions = feed(engine, at(24), missing("air.gpu_intake"))  # 60 s。5 回目
    assert [(t.rule_id, t.state) for t in transitions] == [
        ("SENSOR_MISSING", "pending"),
        ("SENSOR_MISSING", "firing"),
    ], "取り消しは遷移を配らない"
    assert rows(store, "INTAKE_HIGH") == [], "pending の行を消す"

    for index in range(25, 73):  # 62.5 s から戻る。182.5 s の直前まで
        feed(engine, at(index), HOT_INTAKE)
    restarted = only(store, "INTAKE_HIGH")
    assert restarted.state == "pending"
    assert restarted.started_ms == sec(62.5), "回復の時刻から数え直す"

    feed(engine, at(73), HOT_INTAKE)  # 182.5 s = 62.5 + 120
    fired = only(store, "INTAKE_HIGH")
    assert fired.state == "firing"
    assert fired.fired_ms == sec(182.5)


@pytest.mark.parametrize("lost", ["air.room", "air.front_intake"])
def test_recirculation_cancels_on_either_input(lost, engine, store):
    """派生値は `minuend` / `subtrahend` のどちらの `SENSOR_MISSING` でも取り消す。"""
    for index in range(0, 10):
        feed(engine, at(index), RECIRC)
    for index in range(10, 15):
        feed(engine, at(index), {**RECIRC, lost: None})
    assert only(store, "SENSOR_MISSING", lost).state == "firing"
    assert rows(store, "RECIRCULATION") == []

    feed(engine, at(15), RECIRC)
    assert only(store, "RECIRCULATION").started_ms == at(15)


def test_recirculation_keeps_the_pending_when_inputs_alternate(engine, store):
    """2 つの入力が交互に欠け、どちらも 5 回連続にならないなら取り消さない（黙って遅らせない）。"""
    for index in range(0, 10):
        feed(engine, at(index), RECIRC)
    for index in range(10, 30):
        lost = "air.room" if index % 2 else "air.front_intake"
        feed(engine, at(index), {**RECIRC, lost: None})
    assert rows(store, "SENSOR_MISSING") == []
    for index in range(30, 121):  # 300 s まで
        feed(engine, at(index), RECIRC)

    fired = only(store, "RECIRCULATION")
    assert fired.started_ms == T0
    assert fired.fired_ms == sec(300)


def test_recovered_input_still_firing_does_not_cancel_for_the_other_input(engine, store):
    """発火中の `SENSOR_MISSING` の入力が ok に戻っていて、もう片方が 1 回欠けても取り消さない。"""
    feed(engine, at(0), RECIRC)
    for index in range(1, 6):
        feed(engine, at(index), {**RECIRC, "air.room": None})
    assert rows(store, "RECIRCULATION") == []
    feed(engine, at(6), RECIRC)  # 15 s。数え直し
    feed(engine, at(7), {**RECIRC, "air.front_intake": None})

    assert only(store, "SENSOR_MISSING", "air.room").state == "firing", "解除は 3 サンプルの ok"
    pending = only(store, "RECIRCULATION")
    assert pending.state == "pending"
    assert pending.started_ms == at(6)


def test_missing_again_while_sensor_missing_still_fires_cancels_at_once(engine, store):
    """`SENSOR_MISSING` が発火したまま再び欠けたら、その 1 サンプルで取り消す（0139 §2.3.1）。"""
    for index in range(0, 5):
        feed(engine, at(index), missing("air.gpu_intake"))
    feed(engine, at(5), HOT_INTAKE)  # SENSOR_MISSING は解除の待ち（3 サンプル）
    assert only(store, "INTAKE_HIGH").state == "pending"

    feed(engine, at(6), missing("air.gpu_intake"))
    assert rows(store, "INTAKE_HIGH") == []


@pytest.mark.parametrize(("value", "recovered"), [(15.0, 15.0), (75.0, 75.0)])
def test_humidity_range_cancels_on_both_sides(value, recovered, engine, store):
    """範囲のルールも低い側・高い側で同じに動く。"""
    for index in range(0, 100):
        feed(engine, at(index), {"air.room_humidity": value})
    for index in range(100, 104):
        feed(engine, at(index), missing("air.room_humidity"))
    assert only(store, "HUMIDITY_OUT_OF_RANGE").started_ms == T0, "4 回では保つ"

    feed(engine, at(104), missing("air.room_humidity"))
    assert rows(store, "HUMIDITY_OUT_OF_RANGE") == []

    for index in range(105, 105 + 240):
        feed(engine, at(index), {"air.room_humidity": recovered})
    assert only(store, "HUMIDITY_OUT_OF_RANGE").state == "pending"
    feed(engine, at(105 + 240), {"air.room_humidity": recovered})
    fired = only(store, "HUMIDITY_OUT_OF_RANGE")
    assert fired.state == "firing"
    assert fired.started_ms == at(105)
    assert fired.fired_ms == at(105) + 600_000


def test_am2320_backoff_delays_room_high_to_the_documented_time(engine, store):
    """0139 §2.2 (3): 5 回失敗 + 12 周期休む（17 サンプル）。

    `room_high` は 1242.5 s に発火する。
    """
    room = {"air.room": 31.0, "air.room_humidity": 45.0}
    lost = {"air.room": None, "air.room_humidity": None}  # AM2320 は 2 つを同時に欠く
    for index in range(0, 240):  # 0〜597.5 s
        feed(engine, at(index), room)
    for index in range(240, 257):  # 600〜640 s の 17 サンプル
        feed(engine, at(index), lost)
    missing_alert = only(store, "SENSOR_MISSING", "air.room")
    assert missing_alert.fired_ms == sec(610), "5 回目の欠けで人に届く"
    assert rows(store, "ROOM_HIGH") == []

    index = 257  # 642.5 s から戻る
    while at(index) < sec(1242.5):
        feed(engine, at(index), room)
        index += 1
    assert only(store, "ROOM_HIGH").state == "pending"
    assert only(store, "SENSOR_MISSING", "air.room").resolved_ms == sec(647.5)

    feed(engine, sec(1242.5), room)
    fired = only(store, "ROOM_HIGH")
    assert fired.started_ms == sec(642.5)
    assert fired.fired_ms == sec(1242.5)


def test_firing_alert_is_neither_cancelled_nor_resolved_by_missing(engine, store):
    """発火中は欠けで取り消さない・解除しない。回復後に解除の閾値で解除する（0139 §2.3.4）。"""
    for index in range(0, 50):
        feed(engine, at(index), HOT_INTAKE)
    assert only(store, "INTAKE_HIGH").state == "firing"
    for index in range(50, 60):
        feed(engine, at(index), missing("air.gpu_intake"))
    assert only(store, "INTAKE_HIGH").state == "firing"
    feed(engine, at(60), {"air.gpu_intake": 39.0})  # 解除の閾値（38）より上
    assert only(store, "INTAKE_HIGH").state == "firing"
    feed(engine, at(61), {"air.gpu_intake": 37.0})
    resolved = only(store, "INTAKE_HIGH")
    assert resolved.state == "resolved"
    assert resolved.resolved_ms == at(61)


def test_explanation_counts_from_the_restarted_time(engine, store):
    """説明の「条件が N 秒継続して発火」は数え直した時刻からの秒数になる（0016 / #38）。"""
    for index in range(0, 20):
        feed(engine, at(index), HOT_INTAKE)
    for index in range(20, 25):
        feed(engine, at(index), missing("air.gpu_intake"))
    for index in range(25, 74):
        feed(engine, at(index), HOT_INTAKE)
    fired = only(store, "INTAKE_HIGH")
    assert fired.state == "firing"

    tools = ToolRegistry(
        store=store,
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        rules=RuleSet.from_yaml(RULES_PATH),
        clock=store.clock,
        exclusions=LlmExclusions.from_yaml(CONFIG_DIR / "report.yaml"),
    )
    evidence = Explainer(provider=UnavailableProvider(), tools=tools).build_evidence(fired)
    text = "\n".join(item.text for item in evidence)
    assert "条件が 120 秒継続して発火" in text


# ---------------------------------------------------------------- アラートの出ない欠け


def test_disabled_sensor_missing_never_cancels(store, rule_set, catalog):
    """`sensor_missing.enabled: false` では長く欠けても取り消さず、今と同じ時刻に発火する。"""
    engine = make_engine(store, with_disabled(rule_set, "sensor_missing"), catalog)
    for index in range(0, 49):
        values = missing("air.gpu_intake") if 10 <= index <= 30 else HOT_INTAKE
        feed(engine, at(index), values)
    fired = only(store, "INTAKE_HIGH")
    assert fired.started_ms == T0
    assert fired.fired_ms == sec(120)


@pytest.mark.parametrize("driven_by_tick", [True, False])
def test_disabled_sensor_fault_never_cancels(driven_by_tick, store, rule_set, catalog):
    """`sensor_fault.enabled: false` では 30 s 以上の無音（実時間・一括再生）でも取り消さない。"""
    engine = make_engine(store, with_disabled(rule_set, "sensor_fault"), catalog)
    engine.begin(T0)
    for index in range(0, 9):  # 0〜20 s
        feed(engine, at(index), HOT_INTAKE)
    if driven_by_tick:
        tick(engine, store, sec(55))
    index = 24  # 60 s から戻る
    while at(index) <= sec(120):
        feed(engine, at(index), HOT_INTAKE)
        index += 1
    assert rows(store, "SENSOR_FAULT") == []
    fired = only(store, "INTAKE_HIGH")
    assert fired.started_ms == T0
    assert fired.fired_ms == sec(120)


def test_omitted_field_raises_nothing_and_keeps_the_pending(engine, store):
    """欄の省略（`null` でなく鍵ごと無い）では `SENSOR_MISSING` が出ず、取り消しもしない。"""
    for index in range(0, 49):
        values = {"air.room": 26.0} if 10 <= index <= 30 else HOT_INTAKE
        feed(engine, at(index), values)
    assert rows(store, "SENSOR_MISSING") == []
    fired = only(store, "INTAKE_HIGH")
    assert fired.started_ms == T0
    assert fired.fired_ms == sec(120)


# ---------------------------------------------------------------- 2.3.2 再起動


def test_restart_between_missing_samples_raises_nothing(store, rule_set, catalog):
    """「4 回の欠け → 再起動 → 1 回の欠け」では `SENSOR_MISSING` も取り消しも起きない。"""
    first = make_engine(store, rule_set, catalog)
    for index in range(0, 10):
        feed(first, at(index), HOT_INTAKE)
    for index in range(10, 14):
        feed(first, at(index), missing("air.gpu_intake"))

    second = make_engine(store, rule_set, catalog)
    feed(second, at(14), missing("air.gpu_intake"))
    assert rows(store, "SENSOR_MISSING") == []
    pending = only(store, "INTAKE_HIGH")
    assert pending.state == "pending"
    assert pending.started_ms == T0


def test_hydrated_sensor_missing_counts_as_firing(store, rule_set, catalog):
    """読み戻した発火中の `SENSOR_MISSING` の後の欠けでは取り消す。"""
    first = make_engine(store, rule_set, catalog)
    for index in range(0, 5):
        feed(first, at(index), missing("air.gpu_intake"))
    assert only(store, "SENSOR_MISSING").state == "firing"

    second = make_engine(store, rule_set, catalog)
    feed(second, at(5), HOT_INTAKE)
    assert only(store, "INTAKE_HIGH").state == "pending"
    feed(second, at(6), missing("air.gpu_intake"))
    assert rows(store, "INTAKE_HIGH") == []


def test_stop_gap_alone_does_not_cancel_a_hydrated_pending(store, rule_set, catalog):
    """停止の空白だけでは取り消さない（空白を知らせる信号が無い。0139 §2.3.2 / §5 の 4）。"""
    first = make_engine(store, rule_set, catalog)
    first.begin(T0)
    for index in range(0, 4):
        feed(first, at(index), HOT_INTAKE)

    store.clock.advance_to_ms(sec(300))
    second = make_engine(store, rule_set, catalog)
    second.begin(sec(300))
    pending = only(store, "INTAKE_HIGH")
    assert pending.state == "pending"
    assert pending.started_ms == T0, "読み戻した pending を引き継ぐ"

    feed(second, sec(302.5), HOT_INTAKE)
    assert rows(store, "SENSOR_FAULT") == []
    fired = only(store, "INTAKE_HIGH")
    assert fired.started_ms == T0, "今までどおり（空白をまたいだ発火が残る。§5 の 4）"


# ---------------------------------------------------------------- 2.3.2 復元の時の取り消し（移行）


def seed(
    store: SqliteStore,
    rule_id: str,
    metric: str | None,
    started_ms: int,
    *,
    firing: bool,
    severity: str = "warning",
) -> int:
    """更新の前のエンジンが残した行を置く。"""
    alert_id = store.open_alert(
        rule_id=rule_id,
        severity=severity,
        metric=metric,
        started_ms=started_ms,
        threshold=None,
        trigger_value=None,
    )
    if firing:
        store.fire_alert(alert_id, fired_ms=started_ms, trigger_value=None)
    return alert_id


ALL_FIVE = {
    "RECIRCULATION": "d.intake_rise",
    "INTAKE_HIGH": "air.gpu_intake",
    "AIRFLOW_DEGRADED": "d.gpu_delta",
    "ROOM_HIGH": "air.room",
    "HUMIDITY_OUT_OF_RANGE": "air.room_humidity",
}
ALL_OVER = {
    "air.front_intake": 33.0,
    "air.room": 31.0,  # room_high 超え・d.intake_rise = 2.0 なので recirculation は別に用意する
    "air.gpu_intake": 42.0,
    "air.gpu_exhaust": 65.0,  # d.gpu_delta = 23
    "air.room_humidity": 15.0,
}


def test_hydrate_cancels_pending_behind_a_firing_sensor_missing(store, rule_set, catalog):
    """移行: 発火中の `SENSOR_MISSING`（`air.room`）と、それより前からの pending を置いて起動する。

    最初のサンプルが ok で閾値を超えていても発火せず、そのサンプルの時刻から数え直す。
    """
    old = sec(-900)
    seed(store, "SENSOR_MISSING", "air.room", sec(-100), firing=True)
    seed(store, "ROOM_HIGH", "air.room", old, firing=False)
    seed(store, "RECIRCULATION", "d.intake_rise", old, firing=False)

    engine = make_engine(store, rule_set, catalog)
    assert rows(store, "ROOM_HIGH") == []
    assert rows(store, "RECIRCULATION") == []

    feed(engine, T0, {"air.room": 31.0, "air.front_intake": 37.0})  # 室温 31・差 6
    for rule_id in ("ROOM_HIGH", "RECIRCULATION"):
        restarted = only(store, rule_id)
        assert restarted.state == "pending", rule_id
        assert restarted.started_ms == T0, rule_id


def test_hydrate_cancels_every_pending_behind_a_firing_sensor_fault(store, rule_set, catalog):
    """移行: 発火中の `SENSOR_FAULT` があれば 5 つのルールの pending をすべて取り消す。"""
    seed(store, "SENSOR_FAULT", None, sec(-100), firing=True, severity="critical")
    for rule_id, metric in ALL_FIVE.items():
        seed(store, rule_id, metric, sec(-900), firing=False)

    engine = make_engine(store, rule_set, catalog)
    for rule_id in ALL_FIVE:
        assert rows(store, rule_id) == [], rule_id

    feed(engine, T0, {**ALL_OVER, "air.front_intake": 37.0})
    for rule_id in ALL_FIVE:
        restarted = only(store, rule_id)
        assert restarted.state == "pending", rule_id
        assert restarted.started_ms == T0, rule_id


def test_hydrate_keeps_pending_unrelated_to_the_firing_sensor_missing(store, rule_set, catalog):
    """入力に関係しない `SENSOR_MISSING`（`air.top_exhaust`）だけなら引き継ぐ。"""
    seed(store, "SENSOR_MISSING", "air.top_exhaust", sec(-100), firing=True)
    seed(store, "INTAKE_HIGH", "air.gpu_intake", sec(-60), firing=False)

    engine = make_engine(store, rule_set, catalog)
    feed(engine, T0, HOT_INTAKE)
    pending = only(store, "INTAKE_HIGH")
    assert pending.state == "pending"
    assert pending.started_ms == sec(-60)
    for index in range(1, 25):  # 60 s まで 2.5 s ごと
        feed(engine, at(index), HOT_INTAKE)
    fired = only(store, "INTAKE_HIGH")
    assert fired.state == "firing"
    assert fired.fired_ms == sec(60)


def test_hydrate_never_cancels_a_firing_threshold_alert(store, rule_set, catalog):
    seed(store, "SENSOR_FAULT", None, sec(-100), firing=True, severity="critical")
    seed(store, "SENSOR_MISSING", "air.gpu_intake", sec(-100), firing=True)
    seed(store, "INTAKE_HIGH", "air.gpu_intake", sec(-900), firing=True)

    make_engine(store, rule_set, catalog)
    assert only(store, "INTAKE_HIGH").state == "firing"


@pytest.mark.parametrize(
    ("disabled", "fault_rule", "fault_metric"),
    [
        ("sensor_missing", "SENSOR_MISSING", "air.gpu_intake"),
        ("sensor_fault", "SENSOR_FAULT", None),
    ],
)
def test_hydrate_ignores_rows_of_a_disabled_fault_rule(
    disabled, fault_rule, fault_metric, store, rule_set, catalog
):
    """無効にした後に DB に残った発火中の故障アラートでは取り消さず、pending を引き継ぐ。"""
    seed(store, fault_rule, fault_metric, sec(-100), firing=True)
    seed(store, "INTAKE_HIGH", "air.gpu_intake", sec(-60), firing=False)

    engine = make_engine(store, with_disabled(rule_set, disabled), catalog)
    pending = only(store, "INTAKE_HIGH")
    assert pending.state == "pending"
    assert pending.started_ms == sec(-60)

    feed(engine, sec(60), HOT_INTAKE)
    fired = only(store, "INTAKE_HIGH")
    assert fired.state == "firing"
    assert fired.started_ms == sec(-60)
    assert only(store, fault_rule).state == "firing", "残った行そのものの扱いは今のまま"


def test_leftover_row_of_disabled_sensor_missing_does_not_cancel_per_sample(
    store, rule_set, catalog
):
    """サンプルごとの経路でも、無効にした `sensor_missing` の残った行を根拠にしない。"""
    seed(store, "SENSOR_MISSING", "air.gpu_intake", sec(-100), firing=True)
    engine = make_engine(store, with_disabled(rule_set, "sensor_missing"), catalog)

    feed(engine, T0, HOT_INTAKE)
    assert only(store, "INTAKE_HIGH").state == "pending"
    feed(engine, at(1), missing("air.gpu_intake"))
    pending = only(store, "INTAKE_HIGH")
    assert pending.state == "pending"
    assert pending.started_ms == T0


def test_hydrate_reads_active_alerts_beyond_a_thousand_resolved(store, rule_set, catalog):
    """復元の窓: 新しい解決済みが 1000 件を超えても、有効な行をすべて読み戻す。"""
    seed(store, "SENSOR_MISSING", "air.room", sec(-3000), firing=True)
    seed(store, "ROOM_HIGH", "air.room", sec(-2900), firing=False)
    seed(store, "INTAKE_HIGH", "air.gpu_intake", sec(-2900), firing=False)
    with store.transaction():
        for index in range(1_100):
            store._conn.execute(  # 件数だけを作る。1件ずつの書き込みは遅い
                "INSERT INTO alerts (rule_id, severity, state, metric, started_ms, fired_ms, "
                "resolved_ms) VALUES ('PROBE_CHANGED', 'info', 'resolved', NULL, ?, ?, ?)",
                (sec(-2000 + index), sec(-2000 + index), sec(-1999 + index)),
            )
    recent = {(alert.rule_id, alert.metric) for alert in store.alerts(limit=1_000)}
    assert ("SENSOR_MISSING", "air.room") not in recent, "前提: 旧来の 1000 件の窓の外"

    engine = make_engine(store, rule_set, catalog)
    assert rows(store, "ROOM_HIGH") == [], "復元の時の取り消しが働く"
    pending = only(store, "INTAKE_HIGH")
    assert pending.state == "pending"
    assert pending.started_ms == sec(-2900), "故障アラートに依らない pending は引き継ぐ"

    feed(engine, T0, {"air.room": None})  # 読み戻した発火中の SENSOR_MISSING が続いている
    assert only(store, "SENSOR_MISSING", "air.room").state == "firing"


def test_active_alerts_has_no_limit_and_skips_resolved(store):
    for index in range(3):
        seed(store, "SENSOR_MISSING", f"air.x{index}", sec(index), firing=index % 2 == 0)
    resolved = seed(store, "ROOM_HIGH", "air.room", sec(10), firing=True)
    store.resolve_alert(resolved, resolved_ms=sec(20))

    active = store.active_alerts()
    assert {alert.metric for alert in active} == {"air.x0", "air.x1", "air.x2"}
    assert [alert.started_ms for alert in active] == sorted(
        (alert.started_ms for alert in active), reverse=True
    ), "新しい順（alerts() と同じ）"


# ---------------------------------------------------------------- 2.3.3 無音


def test_short_silence_keeps_the_pending(engine, store):
    """20 s の無音では保つ（`sensor_fault` は鳴らない）。"""
    engine.begin(T0)
    for index in range(0, 5):  # 0〜10 s
        feed(engine, at(index), HOT_INTAKE)
    tick(engine, store, sec(30))  # 20 s の無音
    index = 12  # 30 s から戻る
    while at(index) <= sec(120):
        feed(engine, at(index), HOT_INTAKE)
        index += 1
    assert rows(store, "SENSOR_FAULT") == []
    fired = only(store, "INTAKE_HIGH")
    assert fired.started_ms == T0
    assert fired.fired_ms == sec(120)


def test_realtime_silence_cancels_when_sensor_fault_fires(engine, store):
    """30 s の無音で `on_tick()` が `SENSOR_FAULT` を発火させた評価で取り消す。

    再開の後に数え直す。
    """
    engine.begin(T0)
    for index in range(0, 5):  # 0〜10 s
        feed(engine, at(index), HOT_INTAKE)
    transitions = tick(engine, store, sec(40))
    assert [(t.rule_id, t.state) for t in transitions] == [
        ("SENSOR_FAULT", "pending"),
        ("SENSOR_FAULT", "firing"),
    ]
    assert rows(store, "INTAKE_HIGH") == []

    index = 18  # 45 s から戻る
    while at(index) < sec(165):
        feed(engine, at(index), HOT_INTAKE)
        index += 1
    fault = only(store, "SENSOR_FAULT")
    assert fault.resolved_ms == sec(55), "解除は今と同じ（再開から clear_s）"
    assert only(store, "INTAKE_HIGH").started_ms == sec(45)
    assert only(store, "INTAKE_HIGH").state == "pending"
    feed(engine, sec(165), HOT_INTAKE)
    assert only(store, "INTAKE_HIGH").fired_ms == sec(165)


def test_bulk_gap_raises_sensor_fault_and_cancels(engine, store):
    """一括再生: `on_tick()` 無しでサンプルの時刻に 30 s 以上の空白があれば、空白の後の最初の
    サンプルの時刻で `SENSOR_FAULT` が発火し、同じ評価で取り消し、`clear_s` の後に解除する。"""
    engine.begin(T0)
    for index in range(0, 5):  # 0〜10 s
        feed(engine, at(index), HOT_INTAKE)
    transitions = feed(engine, sec(45), HOT_INTAKE)  # 35 s の空白
    assert [(t.rule_id, t.state) for t in transitions] == [
        ("SENSOR_FAULT", "pending"),
        ("SENSOR_FAULT", "firing"),
        ("INTAKE_HIGH", "pending"),
    ], "取り消しは遷移を配らず、同じサンプルから数え直す"
    fault = only(store, "SENSOR_FAULT")
    assert fault.fired_ms == sec(45)
    assert fault.severity == "critical"
    assert fault.detail == "35 秒サンプルが届いていない"
    assert only(store, "INTAKE_HIGH").started_ms == sec(45)

    for seconds in (47.5, 50.0, 52.5):
        feed(engine, sec(seconds), HOT_INTAKE)
    assert only(store, "SENSOR_FAULT").state == "firing"
    feed(engine, sec(55), HOT_INTAKE)
    assert only(store, "SENSOR_FAULT").resolved_ms == sec(55)


def test_bulk_gap_shorter_than_silence_raises_nothing(engine, store):
    engine.begin(T0)
    for index in range(0, 5):  # 0〜10 s
        feed(engine, at(index), HOT_INTAKE)
    assert feed(engine, sec(37.5), HOT_INTAKE) == []  # 27.5 s の空白
    assert rows(store, "SENSOR_FAULT") == []
    assert only(store, "INTAKE_HIGH").started_ms == T0


def test_return_then_gap_again_realtime(engine, store):
    """戻ってすぐまた空白（実時間）: 2 回目の空白で取り消す。

    空白の前の `started_ms` で発火しない。
    """
    engine.begin(T0)
    feed(engine, T0, {"air.gpu_intake": 30.0})
    tick(engine, store, sec(31))
    assert only(store, "SENSOR_FAULT").state == "firing"

    for seconds in (40.0, 42.5, 45.0, 47.5):  # clear_s（10 s）より短く戻る
        feed(engine, sec(seconds), HOT_INTAKE)
    assert only(store, "INTAKE_HIGH").started_ms == sec(40)

    transitions = tick(engine, store, sec(77.5))  # 2 回目の 30 s の無音
    assert transitions == [], "SENSOR_FAULT は発火中のまま"
    assert only(store, "SENSOR_FAULT").state == "firing"
    assert rows(store, "INTAKE_HIGH") == []

    feed(engine, sec(170), HOT_INTAKE)  # 40 + 120 を過ぎている
    restarted = only(store, "INTAKE_HIGH")
    assert restarted.state == "pending", "空白の前の started_ms で発火しない"
    assert restarted.started_ms == sec(170)
    for seconds in (172.5, 175.0, 177.5):
        feed(engine, sec(seconds), HOT_INTAKE)
    assert only(store, "SENSOR_FAULT").state == "firing"
    feed(engine, sec(180), HOT_INTAKE)
    fault = only(store, "SENSOR_FAULT")
    assert fault.resolved_ms == sec(180), "2 回目の再開から clear_s の後"


def test_return_then_gap_again_bulk(engine, store):
    """戻ってすぐまた空白（一括再生）: サンプルの時刻の差で同じに動く。"""
    engine.begin(T0)
    feed(engine, T0, {"air.gpu_intake": 30.0})
    for seconds in (40.0, 42.5, 45.0, 47.5):
        feed(engine, sec(seconds), HOT_INTAKE)
    assert only(store, "SENSOR_FAULT").fired_ms == sec(40)
    assert only(store, "INTAKE_HIGH").started_ms == sec(40)

    transitions = feed(engine, sec(170), HOT_INTAKE)
    assert [(t.rule_id, t.state) for t in transitions] == [("INTAKE_HIGH", "pending")]
    assert only(store, "SENSOR_FAULT").state == "firing"
    restarted = only(store, "INTAKE_HIGH")
    assert restarted.state == "pending"
    assert restarted.started_ms == sec(170)

    for seconds in (172.5, 175.0, 177.5):
        feed(engine, sec(seconds), HOT_INTAKE)
    assert only(store, "SENSOR_FAULT").state == "firing"
    feed(engine, sec(180), HOT_INTAKE)
    assert only(store, "SENSOR_FAULT").resolved_ms == sec(180)


# ---------------------------------------------------------------- 変わらないこと


def test_realtime_sensor_fault_transitions_are_unchanged(engine, store):
    """実時間の `sensor_fault` の遷移の列は今と同じ（tick で発火 → 再開から clear_s で解除）。"""
    engine.begin(T0)
    seen: list[tuple[str, str, int]] = []

    def record(transitions: list, ts_ms: int) -> None:
        seen.extend((t.rule_id, t.state, ts_ms - T0) for t in transitions)

    record(feed(engine, T0, {"air.room": 26.0}), T0)
    for seconds in (10, 20, 30, 31, 40):
        record(tick(engine, store, sec(seconds)), sec(seconds))
    for seconds in (41.0, 43.5, 46.0, 48.5, 51.0):
        record(feed(engine, sec(seconds), {"air.room": 26.0}), sec(seconds))
        record(tick(engine, store, sec(seconds)), sec(seconds))
    assert seen == [
        ("SENSOR_FAULT", "pending", 30_000),
        ("SENSOR_FAULT", "firing", 30_000),
        ("SENSOR_FAULT", "resolved", 51_000),
    ]


def test_sensor_missing_transitions_are_unchanged(engine, store):
    seen = []
    for index in range(0, 9):
        value = None if index < 5 else 26.0
        seen += [(t.rule_id, t.state) for t in feed(engine, at(index), {"air.rear_exhaust": value})]
    assert seen == [
        ("SENSOR_MISSING", "pending"),
        ("SENSOR_MISSING", "firing"),
        ("SENSOR_MISSING", "resolved"),
    ]
