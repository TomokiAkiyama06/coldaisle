"""ルールエンジン（#18）。

**AI は一切関与しない決定論的レイヤ。** 同じ入力からは必ず同じ判定が出る。

各ルールについて「発火する」だけでなく「**解除される**」ことと、
「閾値付近を往復してもフラッピングしない」ことを確かめる。
鳴りっぱなしのアラートは、鳴らないアラートと同じくらい役に立たない。
"""

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.metrics import MetricCatalog
from coldaisle.rules import Engine, RuleSet
from coldaisle.rules.models import RangeRule, ThresholdRule
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH

RULES_PATH = CONFIG_DIR / "rules.yaml"
METRICS_PATH = CONFIG_DIR / "metrics.yaml"
NOW_MS = 1_787_616_000_000
INTERVAL_MS = 2_500
MINUTE_MS = 60_000


@pytest.fixture
def rule_set() -> RuleSet:
    return RuleSet.from_yaml(RULES_PATH)


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "rules.db", rules=rules, clock=SimulatedClock(NOW_MS)) as opened:
        yield opened


@pytest.fixture
def engine(store, rule_set) -> Engine:
    return Engine(
        rules=rule_set,
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        store=store,
        clock=store.clock,
    )


def sample(ts_ms: int, **metrics: float | None) -> Sample:
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


def feed(engine: Engine, seconds: float, **metrics: float | None) -> list:
    """`NOW_MS` から `seconds` 秒後のサンプルを流す。"""
    return engine.on_sample(sample(NOW_MS + int(seconds * 1000), **metrics))


def alerts(store, rule_id: str) -> list:
    return [alert for alert in store.alerts(limit=100) if alert.rule_id == rule_id]


# ---------------------------------------------------------------- 閾値 + 継続時間


def test_recirculation_fires_after_five_minutes(engine, store):
    """FR-404。`d.intake_rise > 5.0℃` が5分継続。

    **成立した瞬間には発火しない。** 一時的な揺らぎで鳴らないための継続時間。
    """
    for step in range(0, 400, 10):  # 6分40秒ぶん
        feed(engine, step, **{"air.front_intake": 33.0, "air.room": 26.0})  # 差 7.0℃

    fired = alerts(store, "RECIRCULATION")
    assert len(fired) == 1
    assert fired[0].state == "firing"
    assert fired[0].started_ms == NOW_MS, "条件が成立した時刻"
    assert fired[0].fired_ms == NOW_MS + 300_000, "5分後に発火（決定記録 0002 §2.9）"
    assert fired[0].threshold == 5.0, "当時の閾値を残す"


def test_recirculation_stays_pending_before_the_duration(engine, store):
    """継続時間に届くまでは `pending`。**行は先に作る。**

    「条件はいつ成立し、継続時間の要件をいつ満たしたか」を後から検証できる
    ようにするため（決定記録 0002 §2.9）。閾値を実測で見直す #19 で
    この差分が判断材料になる。
    """
    for step in range(0, 240, 10):  # 4分（5分に足りない）
        feed(engine, step, **{"air.front_intake": 33.0, "air.room": 26.0})

    pending = alerts(store, "RECIRCULATION")
    assert len(pending) == 1
    assert pending[0].state == "pending"
    assert pending[0].fired_ms is None, "まだ発火していない"
    assert pending[0].started_ms == NOW_MS, "成立した時刻は残っている"


def test_short_excursion_leaves_no_history(engine, store):
    """継続時間に届かなかった揺らぎは履歴に残さない。**本物の発火が埋もれる。**"""
    for step in range(0, 120, 10):
        feed(engine, step, **{"air.front_intake": 33.0, "air.room": 26.0})
    feed(engine, 130, **{"air.front_intake": 27.0, "air.room": 26.0})  # 収まった
    assert store.alerts(limit=100) == ()


def test_recirculation_resolves_when_it_clears(engine, store):
    for step in range(0, 400, 10):
        feed(engine, step, **{"air.front_intake": 33.0, "air.room": 26.0})
    feed(engine, 410, **{"air.front_intake": 29.0, "air.room": 26.0})  # 差 3.0 < 解除 4.0

    fired = alerts(store, "RECIRCULATION")
    assert len(fired) == 1
    assert fired[0].state == "resolved"
    assert fired[0].resolved_ms == NOW_MS + 410_000


def test_hysteresis_prevents_flapping(engine, store):
    """閾値ちょうどを往復してもフラッピングしない（要件 §6.4）。

    発火は 5.0、解除は 4.0。その間を往復する値では**状態が変わらない。**
    """
    for step in range(0, 400, 10):
        feed(engine, step, **{"air.front_intake": 33.0, "air.room": 26.0})
    assert alerts(store, "RECIRCULATION")[0].state == "firing"

    for index, step in enumerate(range(400, 700, 10)):
        rise = 4.5 if index % 2 else 5.5  # 発火閾値の上下を往復
        feed(engine, step, **{"air.front_intake": 26.0 + rise, "air.room": 26.0})

    fired = alerts(store, "RECIRCULATION")
    assert len(fired) == 1, "往復のたびに新しいアラートを作らない"
    assert fired[0].state == "firing", "解除の閾値を下回るまで発火したまま"


@pytest.mark.parametrize(
    ("name", "metric", "over", "under"),
    [
        ("INTAKE_HIGH", "air.gpu_intake", 42.0, 37.0),
        ("ROOM_HIGH", "air.room", 31.0, 27.0),
    ],
)
def test_simple_threshold_rules(name, metric, over, under, engine, store, rule_set):
    """FR-405 / FR-408。閾値を超えた状態が続けば発火し、下回れば解除する。"""
    rule: ThresholdRule = getattr(rule_set, name.lower())
    duration = rule.fire_after_s
    for step in range(0, int(duration) + 60, 10):
        feed(engine, step, **{metric: over})
    assert alerts(store, name)[0].state == "firing"

    feed(engine, duration + 70, **{metric: under})
    assert alerts(store, name)[0].state == "resolved"


def test_airflow_degraded_uses_a_derived_value(engine, store):
    """FR-406。`d.gpu_delta`（排気 − 吸気）が20℃を5分。派生値で判定する。"""
    for step in range(0, 400, 10):
        feed(engine, step, **{"air.gpu_exhaust": 50.0, "air.gpu_intake": 28.0})  # 差 22.0
    assert alerts(store, "AIRFLOW_DEGRADED")[0].state == "firing"


def test_derived_rule_is_silent_when_an_input_is_not_ok(engine, store):
    """**疑わしい値から作った派生値で判定しない。**

    `-127.00` から室温を引いた値は「再循環が無い」ように見え、故障を隠す。
    派生値が計算できない間は、そのルールは何も言わない。
    """
    for step in range(0, 400, 10):
        engine.on_sample(
            Sample(
                ts_ms=NOW_MS + step * 1000,
                readings=(
                    Reading(metric="air.front_intake", value=33.0, quality=Quality.SUSPECT),
                    Reading(metric="air.room", value=26.0, quality=Quality.OK),
                ),
            )
        )
    assert alerts(store, "RECIRCULATION") == []


# ---------------------------------------------------------------- 範囲（湿度）


@pytest.mark.parametrize(("value", "recovered"), [(15.0, 25.0), (75.0, 65.0)])
def test_humidity_out_of_range(value, recovered, engine, store, rule_set):
    """FR-409。低湿は静電気、高湿は結露。どちらも高額GPUの実害要因。"""
    rule: RangeRule = rule_set.humidity_out_of_range
    for step in range(0, int(rule.fire_after_s) + 60, 20):
        feed(engine, step, **{"air.room_humidity": value})
    assert alerts(store, "HUMIDITY_OUT_OF_RANGE")[0].state == "firing"

    feed(engine, rule.fire_after_s + 80, **{"air.room_humidity": recovered})
    assert alerts(store, "HUMIDITY_OUT_OF_RANGE")[0].state == "resolved"


def test_humidity_stays_firing_inside_the_hysteresis_band(engine, store, rule_set):
    """20%未満で発火し、23%までは解除しない。"""
    rule = rule_set.humidity_out_of_range
    for step in range(0, int(rule.fire_after_s) + 60, 20):
        feed(engine, step, **{"air.room_humidity": 15.0})
    feed(engine, rule.fire_after_s + 80, **{"air.room_humidity": 21.0})  # 20〜23 の間
    assert alerts(store, "HUMIDITY_OUT_OF_RANGE")[0].state == "firing"


# ---------------------------------------------------------------- 連続・無音・事象


def test_sensor_missing_needs_five_consecutive(engine, store):
    """FR-402。特定メトリクスが5サンプル連続で ok でない。"""
    for step in range(4):
        feed(engine, step * 2.5, **{"air.rear_exhaust": None})
    assert alerts(store, "SENSOR_MISSING") == [], "4回では鳴らない"

    feed(engine, 10.0, **{"air.rear_exhaust": None})
    fired = alerts(store, "SENSOR_MISSING")
    assert len(fired) == 1
    assert fired[0].state == "firing"
    assert fired[0].metric == "air.rear_exhaust", "どのセンサーかを残す"


def test_sensor_missing_resolves_after_good_samples(engine, store):
    for step in range(5):
        feed(engine, step * 2.5, **{"air.rear_exhaust": None})
    for step in range(5, 9):
        feed(engine, step * 2.5, **{"air.rear_exhaust": 26.0})
    assert alerts(store, "SENSOR_MISSING")[0].state == "resolved"


def test_sensor_fault_fires_without_any_sample(engine, store):
    """FR-401。**サンプルが来ないことを、来ないまま検出する。**

    サンプル受信時にしか評価しないと、止まったことに気づけるのは再開後になる。
    """
    feed(engine, 0, **{"air.room": 26.0})
    store.clock.advance_to_ms(NOW_MS + 20_000)
    engine.on_tick()
    assert alerts(store, "SENSOR_FAULT") == [], "20秒では鳴らない"

    store.clock.advance_to_ms(NOW_MS + 31_000)
    engine.on_tick()
    fired = alerts(store, "SENSOR_FAULT")
    assert len(fired) == 1
    assert fired[0].state == "firing"
    assert fired[0].severity == "critical"


def test_sensor_fault_does_not_resolve_on_the_first_sample(engine, store):
    """1件届いただけで復旧とみなさない。断続的な接続で鳴り止みを繰り返す。"""
    feed(engine, 0, **{"air.room": 26.0})
    store.clock.advance_to_ms(NOW_MS + 31_000)
    engine.on_tick()

    feed(engine, 31.5, **{"air.room": 26.0})
    assert alerts(store, "SENSOR_FAULT")[0].state == "firing"

    for step in range(32, 45):
        feed(engine, step, **{"air.room": 26.0})
    assert alerts(store, "SENSOR_FAULT")[0].state == "resolved", "届き続けたら解除"


def test_probe_changed_stays_firing_until_the_record_matches(engine, store):
    """FR-403。**「変わった瞬間」ではなく「不一致が続いている」で判定する。**

    意味するのは「較正のオフセットが、いま間違ったプローブに対応している」
    という、人が直すまで続く状態である。差し替えたまま較正し直さなければ、
    以降のすべての測定値に誤ったオフセットが乗り続ける。

    点の出来事として即座に解決すると、**静かに間違ったまま運用が続くのを
    防ぐための仕組みが、まさに静かに消える。**
    """
    recorded = {"rear_exhaust": "28FFFFFFFFFFFF05", "front_intake": "28FFFFFFFFFFFF01"}
    observed = {"rear_exhaust": "28FFFFFFFFFFFF09", "front_intake": "28FFFFFFFFFFFF01"}

    engine.on_hello(observed, recorded)
    fired = alerts(store, "PROBE_CHANGED")
    assert len(fired) == 1
    assert fired[0].state == "firing", "発生中のアラートとして残る"
    assert "rear_exhaust" in (fired[0].detail or "")

    # 再起動しても同じ不一致で行を作り直さない
    engine.on_hello(observed, recorded)
    assert len(alerts(store, "PROBE_CHANGED")) == 1

    # 人が較正をやり直して記録を更新した → 次の起動バナーで一致
    engine.on_hello(observed, observed)
    resolved = alerts(store, "PROBE_CHANGED")
    assert len(resolved) == 1
    assert resolved[0].state == "resolved"


def test_probe_changed_is_silent_for_a_new_channel(engine, store):
    """記録に無いチャネルは不一致ではない。増設で鳴らさない。"""
    engine.on_hello({"rear_exhaust": "28FFFFFFFFFFFF05"}, {})
    assert alerts(store, "PROBE_CHANGED") == []


def test_active_alert_is_adopted_after_a_restart(store, rule_set):
    """デーモンを再起動しても、1つの故障が履歴に並ばない。"""
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    recorded = {"rear_exhaust": "28FFFFFFFFFFFF05"}
    observed = {"rear_exhaust": "28FFFFFFFFFFFF09"}

    first = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    first.on_hello(observed, recorded)
    second = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    second.on_hello(observed, recorded)

    assert len(alerts(store, "PROBE_CHANGED")) == 1


def test_rapid_rise_uses_the_stored_slope(engine, store):
    """FR-407。`air.gpu_intake` の上昇率が 5℃/分 超。"""
    for step in range(48):  # 2分ぶん、1分あたり +10℃
        ts = NOW_MS + step * INTERVAL_MS
        store.insert_sample(sample(ts, **{"air.gpu_intake": 20.0 + step * 0.4166}))
    engine.on_sample(sample(NOW_MS + 48 * INTERVAL_MS, **{"air.gpu_intake": 40.0}))

    fired = alerts(store, "RAPID_RISE")
    assert len(fired) == 1
    assert fired[0].state == "firing", "即時発火（継続時間 0）"
    assert fired[0].trigger_value > 5.0


# ---------------------------------------------------------------- 設定の検証


def test_config_marks_thresholds_as_provisional():
    """受入基準: 閾値が暫定であることが `rules.yaml` に書かれている。"""
    text = RULES_PATH.read_text(encoding="utf-8")
    assert "暫定" in text
    assert "#19" in text, "実測で確定させる先が書かれている"


def test_every_required_rule_is_defined(rule_set):
    """FR-401〜409 の9つすべて。"""
    assert len(RuleSet.model_fields) == 9


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ("    clear: 6.0\n", "clear は threshold"),
        ("    metric: Air.Room\n", "命名規約"),
    ],
)
def test_broken_thresholds_are_rejected(patch, match, tmp_path):
    """**解除が発火の外側にあると、発火した瞬間に解除条件も満たす。**"""
    text = RULES_PATH.read_text(encoding="utf-8")
    if "clear" in patch:
        text = text.replace("    clear: 4.0\n", patch, 1)
    else:
        text = text.replace("    metric: d.intake_rise\n", patch, 1)
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        RuleSet.from_yaml(path)


def test_unknown_metric_is_rejected_at_construction(store, rule_set):
    """綴り違いは「一度も成立しないルール」として静かに通る。組み立て時に落とす。"""
    broken = rule_set.model_copy(
        update={"room_high": rule_set.room_high.model_copy(update={"metric": "air.roomm"})}
    )
    with pytest.raises(ValueError, match="未知のメトリクス"):
        Engine(
            rules=broken,
            catalog=MetricCatalog.from_yaml(METRICS_PATH),
            store=store,
            clock=store.clock,
        )


def test_disabled_rules_are_not_evaluated(store, rule_set):
    off = rule_set.model_copy(
        update={"room_high": rule_set.room_high.model_copy(update={"enabled": False})}
    )
    engine = Engine(
        rules=off, catalog=MetricCatalog.from_yaml(METRICS_PATH), store=store, clock=store.clock
    )
    for step in range(0, 700, 20):
        feed(engine, step, **{"air.room": 35.0})
    assert alerts(store, "ROOM_HIGH") == []


# ---------------------------------------------------------------- 通し（#42 の受入基準）


def _run_scenario(tmp_path, scenario: str, *, speed: float, max_samples: int, tick_s: float = 0.05):
    """デーモンを通してシナリオを流し、DB を返す。"""
    from coldaisle.daemon import Config, build
    from conftest import CALIBRATION_PATH, SCENARIOS_PATH

    daemon = build(
        Config(
            source="mock",
            scenario=scenario,
            speed=speed,
            db=tmp_path / f"{scenario}.db",
            scenarios=SCENARIOS_PATH,
            quality_rules=QUALITY_RULES_PATH,
            calibration=CALIBRATION_PATH,
            rules=RULES_PATH,
            metrics=METRICS_PATH,
            tick_s=tick_s,
        )
    )
    try:
        daemon.run(max_samples=max_samples)
        return list(daemon.store.alerts(limit=100)), daemon.stats
    finally:
        daemon.store.close()


@pytest.mark.slow
def test_recirculation_fires_in_compressed_replay(tmp_path):
    """#42 の受入基準: `--speed 60` で `recirculation` を流すと発火する。

    **ホスト時刻がシナリオ時間で進むからこそ、5分の継続が成立する。**
    実時計で判定していたら、実時間15秒では一度も鳴らない。
    """
    found, stats = _run_scenario(tmp_path, "recirculation", speed=60.0, max_samples=360)

    fired = [alert for alert in found if alert.rule_id == "RECIRCULATION"]
    assert fired, "圧縮再生で発火していない"
    assert fired[0].state == "firing"
    assert fired[0].fired_ms - fired[0].started_ms >= 300_000, "5分の継続を満たしている"
    assert stats.alerts_fired >= 1


@pytest.mark.slow
def test_sensor_fault_fires_during_a_dropout(tmp_path):
    """#42 の受入基準: `dropout` で `SENSOR_FAULT` が発火する。

    サンプルが来ない間も評価する仕組み（`on_tick`）が要る。
    """
    # **ティック間隔は圧縮後の無音より十分短くする。** 30秒の無音は
    # 速度30なら実時間1秒。閾値を超えてから復帰するまでの猶予は
    # シナリオ時間で2.5秒（＝実時間83ms）しかないため、10msごとに評価する。
    # 実運用は速度1・ティック1秒なので、この比率は問題にならない
    found, _ = _run_scenario(tmp_path, "dropout", speed=30.0, max_samples=110, tick_s=0.01)

    fired = [alert for alert in found if alert.rule_id == "SENSOR_FAULT"]
    assert fired, "無音の30秒を検出できていない"
    assert fired[0].severity == "critical"


@pytest.mark.slow
def test_sensor_fail_scenario_raises_sensor_missing(tmp_path):
    """`sensor_fail` の欠測が5サンプル続けば `SENSOR_MISSING`（FR-402）。"""
    found, _ = _run_scenario(tmp_path, "sensor_fail", speed=200.0, max_samples=240)

    fired = [alert for alert in found if alert.rule_id == "SENSOR_MISSING"]
    assert fired, "欠測が続いても鳴っていない"
    assert fired[0].metric == "air.rear_exhaust"


@pytest.mark.slow
def test_idle_scenario_raises_nothing(tmp_path):
    """**正常なデータで鳴らない。** 鳴りっぱなしのアラートは無視される。"""
    found, _ = _run_scenario(tmp_path, "idle", speed=200.0, max_samples=400)
    assert found == [], f"正常なのに鳴った: {[a.rule_id for a in found]}"


# ---------------------------------------------------------------- レビュー指摘の退行防止


def test_sensor_fault_fires_when_nothing_ever_arrives(engine, store):
    """**起動時からデバイスが無い場合も鳴る。**

    最初のサンプルを待って基準を立てる実装だと、いちばん重要な場合
    （最初から死んでいる）で永久に鳴らない。「いつから届いていないか」は
    「いつから見ているか」で決まる。
    """
    engine.begin(NOW_MS)
    store.clock.advance_to_ms(NOW_MS + 20_000)
    engine.on_tick()
    assert alerts(store, "SENSOR_FAULT") == []

    store.clock.advance_to_ms(NOW_MS + 31_000)
    engine.on_tick()
    assert alerts(store, "SENSOR_FAULT")[0].state == "firing"


def test_restart_resolves_an_alert_whose_condition_has_cleared(store, rule_set):
    """**再起動したデーモンが古いアラートを解除できること。**

    状態を DB から取り込まないと、条件が解けていても `firing` のまま永久に残る。
    """
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    first = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    for step in range(0, 700, 20):
        first.on_sample(sample(NOW_MS + step * 1000, **{"air.room": 32.0}))
    assert alerts(store, "ROOM_HIGH")[0].state == "firing"

    # デーモンを再起動し、室温が下がった状態で受け取る
    second = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    second.on_sample(sample(NOW_MS + 800_000, **{"air.room": 26.0}))

    resolved = alerts(store, "ROOM_HIGH")
    assert len(resolved) == 1, "同じ事象の行を増やさない"
    assert resolved[0].state == "resolved"


def test_restart_does_not_duplicate_a_still_active_alert(store, rule_set):
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    first = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    for step in range(0, 700, 20):
        first.on_sample(sample(NOW_MS + step * 1000, **{"air.room": 32.0}))

    second = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    second.on_sample(sample(NOW_MS + 800_000, **{"air.room": 33.0}))

    assert len(alerts(store, "ROOM_HIGH")) == 1
    assert alerts(store, "ROOM_HIGH")[0].state == "firing"


def test_humidity_metric_is_validated_too(store, rule_set):
    """検証の列挙から漏れると、そのルールだけ静かに無効になる。"""
    broken = rule_set.model_copy(
        update={
            "humidity_out_of_range": rule_set.humidity_out_of_range.model_copy(
                update={"metric": "air.room_humidityy"}
            )
        }
    )
    with pytest.raises(ValueError, match="未知のメトリクス"):
        Engine(
            rules=broken,
            catalog=MetricCatalog.from_yaml(METRICS_PATH),
            store=store,
            clock=store.clock,
        )
