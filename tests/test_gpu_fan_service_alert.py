"""GPU の Fan の曲線（coldaisle-gpu-fan）が効いていないことの判定。決定記録 0131 §2.1〜§2.3 / §2.6

判定は取り込みのルールエンジンで、`coldaisle-telemetry` が DB へ書いた NVML の最新値を
評価時刻で読んで行う。最新値が無い・ok でない（stale を含む）周期は評価せず、
発火待ちは取り消し、発火中は解除の待ちだけを数え直す。

実機は要らない（AGENTS.md ルール 7）。GPU の値は DB へ直接書くか、偽の NVML を
通した収集（`InternalTelemetryCollector`）で書く。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

from coldaisle.clock import SimulatedClock
from coldaisle.daemon import Daemon
from coldaisle.ingest import Calibration, Normalizer
from coldaisle.ingest.protocol import RawMessage, RawSample
from coldaisle.internal_telemetry import InternalTelemetryCollector, NvmlAdapter
from coldaisle.internal_telemetry.config import NvmlConfig
from coldaisle.llm_exclusions import LlmExclusions
from coldaisle.metrics import MetricCatalog
from coldaisle.notify import Notification
from coldaisle.rules import Engine, RuleSet
from coldaisle.store import Quality, QualityRules, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH
from test_internal_telemetry_nvml import FakeNvml

RULES_PATH = CONFIG_DIR / "rules.yaml"
METRICS_PATH = CONFIG_DIR / "metrics.yaml"
NOW_MS = 1_787_616_000_000
STEP_S = 2.5
"""`coldaisle-telemetry` の周期（config/internal-telemetry.yaml の interval_ms と同じ）。"""

POLICY = "gpu.0.fan_policy"
CORE = "gpu.0.core"
FAN = "gpu.0.fan_speed"
NOT_MANUAL = "GPU_FAN_NOT_MANUAL"
BELOW_CURVE = "GPU_FAN_BELOW_CURVE"


@pytest.fixture
def rule_set() -> RuleSet:
    """本番の rules.yaml。基板の無音（SENSOR_FAULT）だけは切る（ここでは基板を流さない）。"""
    shipped = RuleSet.from_yaml(RULES_PATH)
    return shipped.model_copy(
        update={"sensor_fault": shipped.sensor_fault.model_copy(update={"enabled": False})}
    )


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "gpu-fan.db", rules=rules, clock=SimulatedClock(NOW_MS)) as opened:
        yield opened


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


def gpu(store: SqliteStore, seconds: float, values: dict[str, float | None]) -> None:
    """`coldaisle-telemetry` が書く行（別プロセス）を DB へ直接入れる。"""
    store.insert_sample(
        Sample(
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
    )


def tick(engine: Engine, store: SqliteStore, seconds: float) -> list:
    store.clock.advance_to_ms(at(seconds))  # type: ignore[attr-defined]
    return engine.on_tick()


def run(
    engine: Engine,
    store: SqliteStore,
    start: float,
    stop: float,
    values: dict[str, float | None] | None,
) -> list:
    """`start` から `stop` 秒（含まない）まで、周期ごとに書いて評価する。`None` は書かない。"""
    transitions = []
    seconds = start
    while seconds < stop:
        if values is not None:
            gpu(store, seconds, values)
        transitions += tick(engine, store, seconds)
        seconds += STEP_S
    return transitions


def rows(store: SqliteStore, rule_id: str) -> list:
    return [alert for alert in store.alerts(limit=100) if alert.rule_id == rule_id]


def states(transitions: list, rule_id: str) -> list[str]:
    return [t.state for t in transitions if t.rule_id == rule_id]


# ---------------------------------------------------------------- 設定


def test_shipped_rules_have_the_approved_provisional_values(rule_set):
    """0131 §2.1 の暫定値（2026-10-14 承認）。値は config/rules.yaml にだけ置く。"""
    not_manual = rule_set.gpu_fan_not_manual
    assert (not_manual.enabled, not_manual.severity.value) == (True, "warning")
    assert (not_manual.metric, not_manual.fire_after_s, not_manual.clear_s) == (POLICY, 30, 10)
    below = rule_set.gpu_fan_below_curve
    assert (below.enabled, below.severity.value) == (True, "warning")
    assert (below.core_metric, below.fan_metric) == (CORE, FAN)
    # 決定記録 0140（0130 の曲線に合わせた値。provisional）
    assert (below.core_threshold, below.fan_below, below.fire_after_s) == (80, 70, 30)
    assert (below.core_clear, below.fan_clear) == (77, 73)


@pytest.mark.parametrize(
    ("key", "value"),
    [("core_clear", 80.0), ("core_clear", 81.0), ("fan_clear", 70.0), ("fan_clear", 65.0)],
)
def test_clear_must_be_inside_the_fire_condition(tmp_path, key, value):
    """解除が発火の外側にあると、発火した瞬間に解除条件も満たす。"""
    loaded = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    loaded["rules"]["gpu_fan_below_curve"][key] = value
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(loaded, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="core_clear < core_threshold"):
        RuleSet.from_yaml(path)


def test_unknown_metric_is_rejected_at_startup(tmp_path, store):
    loaded = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    loaded["rules"]["gpu_fan_below_curve"]["fan_metric"] = "gpu.0.fan_sped"
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(loaded, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match=r"gpu\.0\.fan_sped"):
        Engine(
            rules=RuleSet.from_yaml(path),
            catalog=MetricCatalog.from_yaml(METRICS_PATH),
            store=store,
            clock=store.clock,
        )


def test_fan_policy_is_a_flag_in_the_catalog():
    """0131 §2.2: 単位は flag（ロールアップの平均が「手動だった割合」になる）。"""
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    assert catalog.unit_for(POLICY) == "flag"


# ---------------------------------------------------------------- GPU_FAN_NOT_MANUAL


def test_not_manual_fires_after_thirty_seconds_and_resolves_after_ten_of_manual(engine, store):
    transitions = run(engine, store, 0, 40, {POLICY: 0.0})
    assert states(transitions, NOT_MANUAL) == ["pending", "firing"]
    [row] = rows(store, NOT_MANUAL)
    assert (row.state.value, row.started_ms, row.fired_ms) == ("firing", at(0), at(30))
    assert row.metric == POLICY
    firing = next(t for t in transitions if t.state == "firing")
    # 理由（型番の不一致など）は判定できない。切り分けの手順を通知の文に載せる（0131 §2.1）
    assert "systemctl status coldaisle-gpu-fan" in (firing.detail or "")
    assert "journalctl -u coldaisle-gpu-fan" in (firing.detail or "")
    assert firing.severity == "warning"

    # 手動に戻っても 10 s 続くまでは解除しない（再起動の途中の一瞬の手動で消さない）
    recovered = run(engine, store, 40, 50, {POLICY: 1.0})
    assert recovered == []
    resolved = run(engine, store, 50, 52.5, {POLICY: 1.0})
    assert states(resolved, NOT_MANUAL) == ["resolved"]


def test_a_restart_shorter_than_thirty_seconds_leaves_no_history(engine, store):
    """サービスの再起動（RestartSec=2 と起動の待ち）は拾わない。"""
    transitions = run(engine, store, 0, 7.5, {POLICY: 0.0})
    transitions += run(engine, store, 7.5, 60, {POLICY: 1.0})
    assert states(transitions, NOT_MANUAL) == ["pending"]  # pending は配らない（daemon が捨てる）
    assert rows(store, NOT_MANUAL) == []


def test_a_single_manual_reading_while_firing_does_not_resolve(engine, store):
    run(engine, store, 0, 40, {POLICY: 0.0})
    transitions = run(engine, store, 40, 45, {POLICY: 1.0})
    transitions += run(engine, store, 45, 60, {POLICY: 0.0})  # また外れた
    transitions += run(engine, store, 60, 67.5, {POLICY: 1.0})  # 7.5 s しか続かない
    assert transitions == []
    assert rows(store, NOT_MANUAL)[0].state.value == "firing"


@pytest.mark.parametrize("value", [None])
def test_missing_policy_is_not_evaluated(engine, store, value):
    """関数が無い driver・NOT_SUPPORTED（missing）の間は発火も解除もしない（0131 §2.2）。"""
    assert run(engine, store, 0, 120, {POLICY: value}) == []
    assert rows(store, NOT_MANUAL) == []


def test_no_gpu_at_all_is_not_evaluated(engine, store):
    """GPU の無い開発環境（行が1つも無い）では評価されない（0131 §2.5）。"""
    assert run(engine, store, 0, 120, None) == []
    assert store.alerts(limit=100) == ()


def test_pending_is_cancelled_by_a_stale_gap_and_counted_again_from_recovery(engine, store):
    """0131 §2.3: 成立 → 収集が止まる → 回復の直後に成立、だけで「30 s 続いた」とみなさない。"""
    transitions = run(engine, store, 0, 20, {POLICY: 0.0})  # 20 s 成立（まだ足りない）
    assert states(transitions, NOT_MANUAL) == ["pending"]
    # 収集が止まる。最後の行（17.5 s）は 10 s で stale（config/quality.yaml）
    transitions = run(engine, store, 20, 60, None)
    assert transitions == []
    assert rows(store, NOT_MANUAL) == [], "発火待ちは取り消して行を消す"

    transitions = run(engine, store, 60, 62.5, {POLICY: 0.0})  # 回復の直後に 0
    assert states(transitions, NOT_MANUAL) == ["pending"], "発火しない（空白の前を数えない）"
    assert rows(store, NOT_MANUAL)[0].started_ms == at(60)
    transitions = run(engine, store, 62.5, 90, {POLICY: 0.0})
    assert transitions == []
    transitions = run(engine, store, 90, 92.5, {POLICY: 0.0})
    assert states(transitions, NOT_MANUAL) == ["firing"], "回復から 30 s で初めて発火"


def test_stale_threshold_comes_from_quality_yaml(engine, store):
    """stale の閾値はルールに持たない（Store の唯一の情報源。0004 §2.12）。"""
    stale_after_s = QualityRules.from_yaml(QUALITY_RULES_PATH).stale_after_ms / 1000
    run(engine, store, 0, 2.5, {POLICY: 0.0})  # 0 s に1行
    # stale になる直前までは成立し続けている（pending のまま）
    tick(engine, store, stale_after_s - 0.5)
    assert rows(store, NOT_MANUAL)[0].state.value == "pending"
    tick(engine, store, stale_after_s)
    assert rows(store, NOT_MANUAL) == []


def test_missing_reading_also_cancels_pending(engine, store):
    run(engine, store, 0, 20, {POLICY: 0.0})
    run(engine, store, 20, 22.5, {POLICY: None})  # NOT_SUPPORTED などで missing
    assert rows(store, NOT_MANUAL) == []


def test_firing_survives_a_stale_gap_and_clear_waits_again_after_recovery(engine, store):
    """0131 §2.3: 発火中は欠けた入力で解除しない。解除の待ちは空白の前を数えない。"""
    run(engine, store, 0, 40, {POLICY: 0.0})
    # 40 s に手動の行が1つ。その後は収集が止まる。40 s の行は 50 s で stale になるので、
    # 手動が「続いた」のは 47.5 s までの 7.5 s（解除の 10 s に届かない）
    transitions = run(engine, store, 40, 42.5, {POLICY: 1.0})
    transitions += run(engine, store, 42.5, 70, None)
    assert transitions == []
    assert rows(store, NOT_MANUAL)[0].state.value == "firing", "stale で解除しない"

    transitions = run(engine, store, 70, 80, {POLICY: 1.0})  # 回復後 10 s 未満
    assert transitions == [], "空白の前の手動の 7.5 s を足さない"
    transitions = run(engine, store, 80, 82.5, {POLICY: 1.0})
    assert states(transitions, NOT_MANUAL) == ["resolved"]


def test_pending_from_before_a_restart_is_not_counted(store, rule_set):
    """取り込みが止まっていた間も連続とみなさない（欠けた入力と同じ扱い。0131 §2.3）。"""
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    first = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    run(first, store, 0, 20, {POLICY: 0.0})
    assert rows(store, NOT_MANUAL)[0].state.value == "pending"

    restarted = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    assert rows(store, NOT_MANUAL) == []
    transitions = run(restarted, store, 300, 302.5, {POLICY: 0.0})
    assert states(transitions, NOT_MANUAL) == ["pending"]


def test_firing_from_before_a_restart_is_kept_and_resolved(store, rule_set):
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    first = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    run(first, store, 0, 40, {POLICY: 0.0})
    restarted = Engine(rules=rule_set, catalog=catalog, store=store, clock=store.clock)
    transitions = run(restarted, store, 300, 312.5, {POLICY: 1.0})
    assert states(transitions, NOT_MANUAL) == ["resolved"]
    assert len(rows(store, NOT_MANUAL)) == 1


def test_disabled_rule_is_not_evaluated(store, rule_set):
    disabled = rule_set.model_copy(
        update={
            "gpu_fan_not_manual": rule_set.gpu_fan_not_manual.model_copy(update={"enabled": False})
        }
    )
    engine = Engine(
        rules=disabled,
        catalog=MetricCatalog.from_yaml(METRICS_PATH),
        store=store,
        clock=store.clock,
    )
    assert run(engine, store, 0, 60, {POLICY: 0.0}) == []


# ---------------------------------------------------------------- GPU_FAN_BELOW_CURVE

HOT_SLOW = {POLICY: 1.0, CORE: 82.0, FAN: 30.0}

CURVE_0130 = ((45, 30), (60, 40), (70, 50), (76, 60), (80, 75), (83, 100))
"""決定記録 0130 の曲線（°C, %）。点の間は直線で結ぶ。"""


def curve(core: float) -> float:
    for (t0, f0), (t1, f1) in pairwise(CURVE_0130):
        if t0 <= core <= t1:
            return f0 + (f1 - f0) * (core - t0) / (t1 - t0)
    return 100.0 if core > CURVE_0130[-1][0] else CURVE_0130[0][1]


def test_below_curve_fires_when_hot_and_slow_for_thirty_seconds(engine, store):
    """手動のまま低い回転で残った（0128 §2.4 の戻しの打ち切り）。"""
    transitions = run(engine, store, 0, 32.5, HOT_SLOW)
    assert states(transitions, BELOW_CURVE) == ["pending", "firing"]
    assert states(transitions, NOT_MANUAL) == [], "制御方式は手動のまま"
    [row] = rows(store, BELOW_CURVE)
    assert (row.metric, row.threshold, row.trigger_value) == (FAN, 70.0, 30.0)
    assert row.fired_ms == at(30)
    firing = next(t for t in transitions if t.state == "firing")
    assert "systemctl status coldaisle-gpu-fan" in (firing.detail or "")


@pytest.mark.parametrize(
    ("core", "fan"),
    [(79.9, 30.0), (80.0, 70.0), (90.0, 100.0)],
)
def test_below_curve_does_not_fire_outside_the_condition(engine, store, core, fan):
    """80 °C 以上で 70 % 未満のときだけ（決定記録 0140）。"""
    assert run(engine, store, 0, 60, {POLICY: 1.0, CORE: core, FAN: fan}) == []


def test_below_curve_uses_hysteresis_to_clear(engine, store):
    run(engine, store, 0, 32.5, HOT_SLOW)
    # 77 ≤ core < 80・70 ≤ fan < 73 は解除しない（発火の閾値の内側）
    assert run(engine, store, 32.5, 60, {POLICY: 1.0, CORE: 78.0, FAN: 72.0}) == []
    transitions = run(engine, store, 60, 62.5, {POLICY: 1.0, CORE: 78.0, FAN: 73.0})
    assert states(transitions, BELOW_CURVE) == ["resolved"]


def test_below_curve_clears_when_the_core_cools(engine, store):
    run(engine, store, 0, 32.5, HOT_SLOW)
    transitions = run(engine, store, 32.5, 35, {POLICY: 1.0, CORE: 76.9, FAN: 30.0})
    assert states(transitions, BELOW_CURVE) == ["resolved"]


@pytest.mark.parametrize("stopped", [CORE, FAN])
def test_one_stale_input_counts_as_a_gap_for_below_curve(engine, store, stopped):
    """0131 §2.3: 2 つの値のどちらかが欠ければ欠けとみなす。"""
    run(engine, store, 0, 20, HOT_SLOW)
    still = {k: v for k, v in HOT_SLOW.items() if k != stopped}
    run(engine, store, 20, 40, still)  # 片方だけ書かれ続ける
    assert rows(store, BELOW_CURVE) == [], "発火待ちは取り消す"
    transitions = run(engine, store, 40, 42.5, HOT_SLOW)
    assert states(transitions, BELOW_CURVE) == ["pending"]
    assert rows(store, BELOW_CURVE)[0].started_ms == at(40)


def test_below_curve_firing_survives_a_gap(engine, store):
    run(engine, store, 0, 32.5, HOT_SLOW)
    assert run(engine, store, 32.5, 60, None) == []
    assert rows(store, BELOW_CURVE)[0].state.value == "firing"


# ---------------------------------------------------------------- 既存のルールは変えない


def test_existing_threshold_rule_still_keeps_since_across_missing_values(engine, store):
    """0131 §2.3 / §5 の 4: 既存のしきい値のルールの振る舞いは変えない（#302 で別に決める）。"""

    def board(seconds: float, value: float | None) -> list:
        return engine.on_sample(
            Sample(
                ts_ms=at(seconds),
                readings=(
                    Reading(
                        metric="air.gpu_intake",
                        value=value,
                        quality=Quality.OK if value is not None else Quality.MISSING,
                    ),
                ),
            )
        )

    board(0, 45.0)
    board(60, None)  # 欠けても since_ms を保つ（今の振る舞い）
    transitions = board(120, 45.0)
    assert [t.state for t in transitions if t.rule_id == "INTAKE_HIGH"] == ["firing"]


# ---------------------------------------------------------------- 評価時刻（0012 §2.2）


def test_bulk_replay_reads_the_value_at_the_evaluation_time(engine, store):
    """一括再生: 時計は止まったまま、評価時刻はサンプルの時刻に従う。

    DB には先の時刻（手動に戻った 1000 s）の行が既にある。評価時刻より後の行を
    読むと「手動」に見えて発火しない。
    """
    for step in range(0, 17):
        gpu(store, step * STEP_S, {POLICY: 0.0})
    gpu(store, 1000, {POLICY: 1.0})
    transitions = []
    for step in range(0, 17):
        transitions += engine.on_sample(
            Sample(
                ts_ms=at(step * STEP_S),
                readings=(Reading(metric="air.room", value=26.0, quality=Quality.OK),),
            )
        )
    assert store.clock.now_ms() == NOW_MS, "時計は進めていない"
    assert states(transitions, NOT_MANUAL) == ["pending", "firing"]
    assert rows(store, NOT_MANUAL)[0].fired_ms == at(30)


# ---------------------------------------------------------------- 偽の NVML から通知まで


@dataclass
class StoppedServiceNvml(FakeNvml):
    """「サービスが止まった」シナリオ（0131 §2.6）。

    `stop_after` 回目より後の読み取りから手動でなくなる（0 が続く）。
    """

    stop_after: int = 4
    reads: int = 0

    def fan_policy_manual(self, handle: object) -> float | None:
        self.reads += 1
        return 1.0 if self.reads <= self.stop_after else 0.0


class RecordingNotifier:
    """`Router` の代わり。送った通知を残す。"""

    @dataclass
    class _Config:
        dashboard_url: str = "http://127.0.0.1:8000/"
        context_always: tuple[str, ...] = ("air.room",)

    def __init__(self) -> None:
        self.config = self._Config()
        self.sent: list[Notification] = []

    def start(self) -> None:
        pass

    def stop(self, *, timeout_s: float = 30.0) -> None:
        pass

    def notify(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return True


@dataclass
class BoardWithTelemetry:
    """基板のサンプルを流しつつ、別の接続で `coldaisle-telemetry` と同じ書き込みをする。

    取り込みと収集は別プロセスで同じ DB を使う。ここでは読み取りスレッドの中で
    別の接続を開いて模す（接続はスレッド間で共有しない。0004 §2.8）。
    """

    db: Path
    rules: QualityRules
    clock: SimulatedClock
    nvml: FakeNvml
    count: int
    collector_clock: SimulatedClock = field(default_factory=lambda: SimulatedClock(NOW_MS))

    def stream(self) -> Iterator[RawMessage]:
        collector = InternalTelemetryCollector(
            (NvmlAdapter(NvmlConfig(enabled=True, gpu_indices=(0,)), self.nvml),),
            self.collector_clock,
        )
        with SqliteStore(self.db, rules=self.rules, clock=self.collector_clock) as telemetry:
            for index in range(self.count):
                at_ms = at(index * STEP_S)
                self.collector_clock.advance_to_ms(at_ms)
                telemetry.insert_sample(collector.collect().sample)
                self.clock.advance_to_ms(at_ms)
                yield RawSample(seq=index, up=index, channels={"room_temp": 26.0})


def test_stopped_service_reaches_the_notifier_without_asking_the_llm(tmp_path, rules, rule_set):
    """偽の NVML →（収集）→ DB →（取り込みのルールエンジン）→ 通知。説明は求めない（§2.4a）。"""
    db = tmp_path / "e2e.db"
    clock = SimulatedClock(NOW_MS)
    store = SqliteStore(db, rules=rules, clock=clock)
    notifier = RecordingNotifier()
    nvml = StoppedServiceNvml()
    daemon = Daemon(
        source=BoardWithTelemetry(db, rules, clock, nvml, count=20),  # type: ignore[arg-type]
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
    asked: list[str] = []
    daemon._request_explanation = lambda transition: asked.append(transition.rule_id)  # type: ignore[method-assign]
    try:
        daemon.run()
        fired = [n for n in notifier.sent if n.rule_id == NOT_MANUAL]
        assert [(n.state, n.severity.value) for n in fired] == [("firing", "warning")]
        assert "systemctl status coldaisle-gpu-fan" in (fired[0].detail or "")
        assert asked == [], "除外の一覧の rule_id は LLM に説明を求めない"
        # 停止は 4 回目の読み取りの後（7.5 s）。そこから 30 s で発火
        assert rows(store, NOT_MANUAL)[0].started_ms == at(4 * STEP_S)
        assert rows(store, NOT_MANUAL)[0].fired_ms == at(4 * STEP_S + 30)
        latest = store.latest(at_ms=at(19 * STEP_S))
        assert latest[POLICY].value == 0.0
    finally:
        store.close()


# ------------------------------------------------ 0130 の曲線との整合（決定記録 0140）


@pytest.mark.parametrize("core", [70, 72, 74, 76, 77, 78, 79, 80, 81])
def test_normal_rotation_on_the_0130_curve_does_not_fire(engine, store, core):
    """0130 の曲線どおりに回っていれば、70〜81 °C のどこでも発火しない（高い負荷で誤発火しない）。

    0131 の値（70 °C / 90 %）ではここで発火していた。
    """
    fan = float(int(curve(core)))  # NVML は整数の %。切り捨てて低い側で確かめる
    assert run(engine, store, 0, 120, {POLICY: 1.0, CORE: float(core), FAN: fan}) == []


@pytest.mark.parametrize(("core", "fan"), [(78.0, 68.0), (79.0, 72.0), (79.0, 68.0)])
def test_measured_600w_steady_state_does_not_fire(engine, store, core, fan):
    """600 W の定常は 78〜79 °C・68〜72 %（docs/fan-header-mapping.md の T5・T7）。"""
    assert run(engine, store, 0, 120, {POLICY: 1.0, CORE: core, FAN: fan}) == []


def test_gpu_default_control_at_80c_fires(engine, store):
    """GPU 自身の既定の制御は 80 °C で約 47 %（0128 §1 の測定）。30 s で捉える。"""
    transitions = run(engine, store, 0, 32.5, {POLICY: 0.0, CORE: 80.0, FAN: 47.0})
    assert states(transitions, BELOW_CURVE) == ["pending", "firing"]
