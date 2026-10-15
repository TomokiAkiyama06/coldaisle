"""`AIRFLOW_DEGRADED` を GPU の電力から求めた正常の上限と比べて判定する。決定記録 0144

`d.gpu_delta`（排気 − 吸気）から、`power.gpu.0` の直近 `power_window_s` の最大で引いた
期待値（`config/rules.yaml` の点の表）を引いた超過で判定する。電力は取り込みのサンプルに
載らず、`coldaisle-telemetry` が DB へ書く行を評価時刻で読む。

実機は要らない（AGENTS.md ルール 7）。値は 0144 §1.2 の材料の要約の数字で合成する:

- 定常の最大: アイドル 5.0・300 W 26.3・450 W 33.8・600 W（0130 の曲線）32.6 °C
- 600 W で GPU の Fan が既定の制御（約 47 %）: 推定 46〜50 °C
- 600 W を止めた後の減り方: 6 °C 未満まで 340〜380 s
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from coldaisle.clock import SimulatedClock
from coldaisle.metrics import MetricCatalog
from coldaisle.rules import Engine, RuleSet
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR

RULES_PATH = CONFIG_DIR / "rules.yaml"
METRICS_PATH = CONFIG_DIR / "metrics.yaml"
NOW_MS = 1_787_616_000_000
STEP_S = 2.5
"""基板の送信周期と `coldaisle-telemetry` の周期（どちらも 2.5 s）。"""

RULE_ID = "AIRFLOW_DEGRADED"
POWER = "power.gpu.0"
INTAKE_C = 21.0


@pytest.fixture
def rule_set() -> RuleSet:
    return RuleSet.from_yaml(RULES_PATH)


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "airflow.db", rules=rules, clock=SimulatedClock(NOW_MS)) as opened:
        yield opened


def make_engine(store: SqliteStore, rule_set: RuleSet) -> Engine:
    return Engine(
        rules=rule_set,
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        store=store,
        clock=store.clock,
    )


@pytest.fixture
def engine(store, rule_set) -> Engine:
    return make_engine(store, rule_set)


def at(seconds: float) -> int:
    return NOW_MS + int(seconds * 1000)


def _sample(seconds: float, values: dict[str, float | None]) -> Sample:
    return Sample(
        ts_ms=at(seconds),
        readings=tuple(
            Reading(
                metric=metric,
                value=value,
                quality=Quality.OK if value is not None else Quality.MISSING,
            )
            for metric, value in values.items()
        ),
    )


def step(
    engine: Engine,
    store: SqliteStore,
    seconds: float,
    *,
    power: float | None,
    delta: float | None,
    exhaust_missing: bool = False,
) -> list:
    """1 周期。取り込みと同じく、DB へ書いてから評価する（`daemon._on_sample`）。

    `power` が `None` の周期は電力の行を書かない（収集が止まった）。
    """
    if power is not None:
        store.insert_sample(_sample(seconds, {POWER: power}))
    exhaust = None if exhaust_missing or delta is None else INTAKE_C + delta
    board = _sample(seconds, {"air.gpu_intake": INTAKE_C, "air.gpu_exhaust": exhaust})
    store.insert_sample(board)
    return engine.on_sample(board)


def run(
    engine: Engine,
    store: SqliteStore,
    start: float,
    stop: float,
    *,
    power: float | None,
    delta: float,
) -> list:
    """`start` から `stop` 秒（含まない）まで、同じ電力・同じ差を流す。"""
    transitions = []
    seconds = start
    while seconds < stop:
        transitions += step(engine, store, seconds, power=power, delta=delta)
        seconds += STEP_S
    return transitions


def rows(store: SqliteStore) -> list:
    return [alert for alert in store.alerts(limit=100) if alert.rule_id == RULE_ID]


def ours(transitions: list) -> list:
    return [transition for transition in transitions if transition.rule_id == RULE_ID]


# ---------------------------------------------------------------- 正常の定常では鳴らない


@pytest.mark.parametrize(
    ("power", "delta"),
    [
        (600.0, 32.6),  # 0130 の曲線の 600 W の最大（T5・T7）
        (600.0, 36.0),  # 600 W で GPU の Fan を 60 % に固定（0144 §2.2: 鳴らない）
        (450.0, 33.8),  # 450 W の最大（T5、既定の制御）
        (300.0, 26.3),  # 300 W の最大（T4・T5、既定の制御）
        (7.0, 5.0),  # アイドルの最大（T7）
    ],
)
def test_steady_normal_does_not_fire(engine, store, power, delta):
    """0144 §2.2 の表。`fire_after_s`（300 s）を十分に越えても、発火の待ちにも入らない。"""
    transitions = run(engine, store, 0, 1200, power=power, delta=delta)
    assert ours(transitions) == []
    assert rows(store) == []


def test_t1_values_do_not_fire_at_300_w_or_more(engine, store):
    """T1 で鳴った 26〜30 °C は、参照電力が 300 W 以上なら閾値に届かない（0144 §2.2）。"""
    for index, power in enumerate((300.0, 450.0, 600.0)):
        start = index * 1200
        run(engine, store, start, start + 1200, power=power, delta=30.0)
    assert rows(store) == []


def test_old_fixed_threshold_would_have_fired(rule_set):
    """前提の確認: 600 W の正常（30〜32 °C）は FR-406 の旧い閾値 20 °C を超える（0144 §1.1）。"""
    rule = rule_set.airflow_degraded
    assert rule.expected_at(600.0) + rule.threshold > 32.6 > 20.0


# ---------------------------------------------------------------- 風量が落ちたら鳴る


def test_default_gpu_fan_at_600_w_fires_and_resolves(engine, store):
    """600 W で GPU の Fan が既定の制御（約 47 %、推定 46〜50 °C）なら 300 s で発火する。"""
    run(engine, store, 0, 400, power=600.0, delta=47.0)
    [alert] = rows(store)
    assert alert.state == "firing"
    assert alert.started_ms == at(0)
    assert alert.fired_ms == at(300)
    assert alert.threshold == pytest.approx(40.0), "その時の発火の d.gpu_delta（期待値 + 超過）"
    assert alert.trigger_value == pytest.approx(47.0), "d.gpu_delta そのもの"
    assert alert.detail is not None
    assert "600 W" in alert.detail

    transitions = run(engine, store, 400, 410, power=600.0, delta=32.0)  # 超過 −2 < 解除 4
    assert [t.state for t in ours(transitions)] == ["resolved"]
    assert rows(store)[0].state == "resolved"


def test_hysteresis_between_clear_and_threshold(engine, store):
    """発火の後は、超過が解除の 4.0 を下回るまで発火したまま（38.0〜40.0 の間を往復しても）。"""
    run(engine, store, 0, 400, power=600.0, delta=47.0)
    seconds = 400.0
    for index in range(100):
        delta = 39.0 if index % 2 else 40.5
        step(engine, store, seconds, power=600.0, delta=delta)
        seconds += STEP_S
    [alert] = rows(store)
    assert alert.state == "firing"


def test_excess_exactly_at_threshold_does_not_start(engine, store):
    """成立は「超過 > threshold」（0144 §2.1）。差 40.0（超過ちょうど 6.0）で待たない。"""
    run(engine, store, 0, 600, power=600.0, delta=40.0)
    assert rows(store) == []


def test_excess_exactly_at_clear_keeps_firing(engine, store):
    """解除は「超過 < clear」（0144 §2.1）。差 38.0（超過ちょうど 4.0）では発火を保つ。"""
    run(engine, store, 0, 400, power=600.0, delta=47.0)
    run(engine, store, 400, 600, power=600.0, delta=38.0)
    assert rows(store)[0].state == "firing"
    run(engine, store, 600, 610, power=600.0, delta=37.9)
    assert rows(store)[0].state == "resolved"


def test_excess_exactly_at_clear_keeps_pending(engine, store):
    """発火待ちの間も、超過ちょうど clear では取り消さず、継続時間で発火する。"""
    run(engine, store, 0, 100, power=600.0, delta=47.0)
    run(engine, store, 100, 400, power=600.0, delta=38.0)
    [alert] = rows(store)
    assert alert.state == "firing"
    assert alert.started_ms == at(0)


def test_threshold_follows_power(engine, store):
    """300 W では 32.5 °C 超で鳴る（450 W・600 W なら正常の 33 °C でも）。"""
    run(engine, store, 0, 400, power=300.0, delta=33.0)
    [alert] = rows(store)
    assert alert.state == "firing"
    assert alert.threshold == pytest.approx(32.5)


def test_fired_alert_records_the_threshold_applied_at_firing(engine, store):
    """発火待ちの間に電力が上がったら、行の閾値は発火の時点で当てた値（0144 §2.6）。

    300 W で 41 °C（限度 32.5）から待ちに入り、600 W に上がっても 41 °C は限度 40.0 を超えて続く。
    記録は開いたときの 32.5 ではなく 40.0 にする（説明が誤った限度を書かないため）。
    """
    run(engine, store, 0, 100, power=300.0, delta=41.0)
    assert rows(store)[0].threshold == pytest.approx(32.5)
    run(engine, store, 100, 400, power=600.0, delta=41.0)
    [alert] = rows(store)
    assert alert.state == "firing"
    assert alert.started_ms == at(0)
    assert alert.threshold == pytest.approx(40.0)
    assert alert.detail is not None
    assert "600 W" in alert.detail

    # 解除はその周期の参照電力で判定し、発火の時点の閾値の記録は変えない
    run(engine, store, 400, 1100, power=300.0, delta=39.0)  # 窓の間は 600 W の期待値で超過 5 > 4
    assert rows(store)[0].state == "firing"
    run(engine, store, 1100, 1110, power=300.0, delta=30.0)  # 300 W の期待値 26.5 で超過 3.5 < 4
    [alert] = rows(store)
    assert alert.state == "resolved"
    assert alert.threshold == pytest.approx(40.0)


# ---------------------------------------------------------------- 電力の変化の直後


def _decay(seconds: float) -> float:
    """600 W を止めた後の差。材料（6 °C 未満まで 340〜380 s）より遅く減る指数（τ = 170 s）。"""
    return 3.5 + 28.5 * math.exp(-seconds / 170.0)


def _drop_from_600(engine: Engine, store: SqliteStore) -> list:
    transitions = run(engine, store, 0, 600, power=600.0, delta=32.0)
    seconds = 600.0
    while seconds < 2400:
        transitions += step(engine, store, seconds, power=7.0, delta=_decay(seconds - 600))
        seconds += STEP_S
    return transitions


def test_residual_heat_after_power_drop_does_not_fire(engine, store):
    """0144 §2.3。下げてから 600 s は下げる前の期待値と比べるので、余熱で発火の待ちに入らない。"""
    assert _decay(380) > 6.0, "合成の減り方は材料（380 s までに 6 °C 未満）より遅い"
    transitions = _drop_from_600(engine, store)
    assert ours(transitions) == []
    assert rows(store) == []


def test_without_the_window_residual_heat_would_start_pending(store, rule_set):
    """窓が無いと（今の電力だけで比べると）、余熱で発火の待ちに入る。窓が要る理由（0144 §2.3）。"""
    rule = rule_set.airflow_degraded.model_copy(update={"power_window_s": STEP_S})
    engine = make_engine(store, rule_set.model_copy(update={"airflow_degraded": rule}))
    transitions = _drop_from_600(engine, store)
    assert "pending" in [t.state for t in ours(transitions)]


def test_power_rise_raises_the_expectation_at_once(engine, store):
    """上げた直後は、新しい電力の期待値ですぐ比べる（差が定常に近づいても鳴らない）。"""
    run(engine, store, 0, 600, power=7.0, delta=4.0)
    run(engine, store, 600, 1800, power=600.0, delta=32.6)
    assert rows(store) == []


def test_window_keeps_the_previous_high_power_only_for_its_length(engine, store):
    """窓を過ぎたら、下げた後の電力の期待値で比べる（下げた後も風量の低下を捉える）。"""
    run(engine, store, 0, 600, power=600.0, delta=32.0)
    # アイドルで差 15（期待値 5 を 10 超える）。窓の 600 s の間は 600 W の期待値（34）と比べる
    run(engine, store, 600, 1200, power=7.0, delta=15.0)
    assert rows(store) == []
    run(engine, store, 1200, 1600, power=7.0, delta=15.0)
    [alert] = rows(store)
    assert alert.state == "firing"


# ---------------------------------------------------------------- 電力が無い・stale（0144 §2.4）


def test_missing_power_uses_the_largest_expectation(engine, store):
    """電力の行が無いときは表の最大（34.0）と比べる。600 W と同じく 40.0 超で鳴る。"""
    run(engine, store, 0, 600, power=None, delta=36.0)
    assert rows(store) == []

    run(engine, store, 600, 1000, power=None, delta=47.0)
    [alert] = rows(store)
    assert alert.state == "firing"
    assert alert.threshold == pytest.approx(40.0)
    assert alert.detail is not None
    assert "stale" in alert.detail


def test_stale_power_is_not_used(engine, store):
    """収集が止まった後に、窓の中の古い ok の値（アイドル）で推測しない（0144 §2.4）。

    アイドルの期待値（5.0）なら差 20 は超過 15 で鳴るが、電力が stale なので最大の期待値と比べる。
    """
    run(engine, store, 0, 60, power=7.0, delta=4.0)
    run(engine, store, 60, 700, power=None, delta=20.0)
    assert rows(store) == []


def test_power_lost_while_pending_keeps_evaluating(engine, store):
    """発火の待ちの間に電力が分からなくなっても、取り消さずに最大の期待値で評価を続ける。"""
    run(engine, store, 0, 100, power=600.0, delta=47.0)
    run(engine, store, 100, 400, power=None, delta=47.0)
    [alert] = rows(store)
    assert alert.state == "firing"
    assert alert.started_ms == at(0), "電力が分からなくなっても数え直さない（0144 §2.4）"


# ---------------------------------------------------------------- 0139 はそのまま


def test_sensor_missing_on_exhaust_still_cancels_pending(engine, store):
    """0139: `air.gpu_exhaust` の `SENSOR_MISSING` が発火中なら、発火の待ちを取り消して数え直す。"""
    run(engine, store, 0, 100, power=600.0, delta=47.0)
    assert rows(store)[0].state == "pending"
    seconds = 100.0
    for _ in range(6):  # sensor_missing.consecutive（5）を越える
        step(engine, store, seconds, power=600.0, delta=47.0, exhaust_missing=True)
        seconds += STEP_S
    assert rows(store) == []

    run(engine, store, seconds, seconds + 400, power=600.0, delta=47.0)
    [alert] = rows(store)
    assert alert.started_ms == at(seconds), "回復してから数え直す"
    assert alert.state == "firing"


# ---------------------------------------------------------------- 設定の検査


def test_expected_is_interpolated_linearly(rule_set):
    rule = rule_set.airflow_degraded
    assert rule.expected_at(0.0) == pytest.approx(5.0)
    assert rule.expected_at(150.0) == pytest.approx(15.75)
    assert rule.expected_at(375.0) == pytest.approx(30.25)
    assert rule.expected_at(700.0) == pytest.approx(34.0), "表の外は端の値"
    assert rule.expected_at(None) == pytest.approx(34.0), "電力が分からなければ表の最大"


def _write_rules(tmp_path: Path, **airflow: object) -> Path:
    loaded = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    loaded["rules"]["airflow_degraded"].update(airflow)
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(loaded, allow_unicode=True), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "airflow",
    [
        {"expected": [{"power_w": 0.0, "value": 5.0}]},  # 点が 1 つ
        {
            "expected": [
                {"power_w": 0.0, "value": 5.0},
                {"power_w": 450.0, "value": 34.0},
                {"power_w": 600.0, "value": 32.6},  # 減る
            ]
        },
        {
            "expected": [
                {"power_w": 300.0, "value": 26.5},
                {"power_w": 300.0, "value": 30.0},  # 電力が増えない
            ]
        },
        {"clear": 6.0},  # clear >= threshold
        {"power_window_s": 0.0},
        {"power_metric": "Power GPU"},  # 命名規約
    ],
)
def test_invalid_settings_are_rejected(tmp_path, airflow):
    with pytest.raises(ValueError):
        RuleSet.from_yaml(_write_rules(tmp_path, **airflow))


def test_unknown_power_metric_is_rejected(tmp_path, store):
    """綴り違いは「一度も電力が分からないルール」として静かに通る。組み立て時に落とす。"""
    rules = RuleSet.from_yaml(_write_rules(tmp_path, power_metric="power.gpu.9"))
    with pytest.raises(ValueError, match=r"power\.gpu\.9"):
        make_engine(store, rules)
