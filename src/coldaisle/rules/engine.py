"""ルールエンジン（L2）。#18

**AI は一切関与しない決定論的レイヤ。** 同じ入力からは必ず同じ判定が出る。

状態機械は `OK → PENDING → FIRING → RESOLVED`（要件 §6.4）。
条件が成立した時点で `pending` の行を作り、継続時間を満たしたら `firing` にする。
**発火してから行を作らない。** 「条件はいつ成立し、継続時間の要件をいつ満たしたか」を
後から検証できるようにするため（決定記録 0002 §2.9）。閾値を実測で見直す #19 で
この差分が判断材料になる。

**ヒステリシス。** いったん成立したら、解除の閾値を越えるまで成立し続ける。
1つの閾値で判定すると、境界付近を往復するデータで発火と解除を繰り返す。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from coldaisle import logs
from coldaisle.clock import Clock, MonotonicClock, SystemMonotonicClock
from coldaisle.metrics import MetricCatalog, compute_derived
from coldaisle.rules.fand_trace import FandStatus, describe_faults, read_fand_status
from coldaisle.rules.models import (
    ControlSilenceRule,
    ControlStateRule,
    FlagOffRule,
    HotAndSlowRule,
    LatestThresholdRule,
    RangeRule,
    RuleSet,
    SlopeRule,
    ThresholdRule,
    rule_metrics,
)
from coldaisle.store import LatestReading, Quality, Reading, Sample, SqliteStore

LOGGER = logging.getLogger("coldaisle.rules")


GAP_RESET_RULES: tuple[str, ...] = (
    "recirculation",
    "intake_high",
    "airflow_degraded",
    "room_high",
    "humidity_out_of_range",
)
"""欠けの検知（`SENSOR_MISSING` / `SENSOR_FAULT`）で発火待ちを数え直すルール（決定記録 0139 §2.3）。

`rapid_rise`・`sensor_*`・`probe_changed` と、DB の最新値を読むルール
（GPU の Fan の 2 つと 12V-2x6 コネクタの 2 つ。0131 §2.3 / 0145 §2.3。
1 回の欠けで取り消す）は含めない。
"""


@dataclass
class RuleState:
    """1ルール（メトリクス別のものはメトリクスごと）の進行状況。"""

    since_ms: int | None = None
    """条件が成立した時刻。`None` なら成立していない。"""
    alert_id: int | None = None
    firing: bool = False
    streak: int = 0
    """連続回数（FR-402 用）。"""
    resumed_ms: int | None = None
    """無音から復帰した時刻（FR-401 用）。

    fand の trace を読むルール（決定記録 0148）では、解除の条件を満たした最初の行を**読んだ
    ときの単調時計**（`Engine.monotonic`）を入れる。trace の `ts_ms` は壁時計で前後に跳ぶため。
    """
    trace_since_mono_ms: int | None = None
    """fand の trace を読むルールで、条件が成立した最初の行を読んだときの単調時計（0148）。"""
    severity: str = "warning"
    """このアラートの重大度。**解除したあとに DB から引けない**ので持っておく。

    引こうとすると、行は既に `resolved` になっていて未解決の検索に掛からず、
    既定値に落ちる。critical の解除が warning の経路へ流れることになる。
    """


@dataclass(frozen=True)
class Transition:
    """状態が変わったこと。通知（#20）はこれを見る。

    **状態ではなく遷移を配る。** 発生中のあいだ送り続けると、
    受け取る側が「新しく起きたこと」を見分けられなくなる。
    """

    rule_id: str
    metric: str | None
    state: str
    value: float | None
    detail: str | None = None
    severity: str = "warning"


_ABSENT = object()
"""「そのチャネルが hello に出てこなかった」ことを表す番人。

`None` は「ROM を持たないセンサー」（AM2320）に使われているので、
**欠けていることと区別できる値が要る。**
"""


def probe_mismatch(
    observed: Mapping[str, str | None], recorded: Mapping[str, str | None]
) -> list[str]:
    """記録と食い違うチャネル。**記録側を基準に数える。**

    含めるもの:

    - ROM が変わった（差し替え）
    - **記録にあるのに hello に出てこない**（起動時に繋がっていなかった）

    後者を見落とすと、ホストは「そのチャネルは無い」として記録を消す。
    あとで別のプローブを挿したときに**比べる相手が無く、黙って受け入れる。**
    較正のオフセットが間違ったプローブに対応したまま運用が続く
    （FR-403 がまさに防ごうとしている状態）。

    含めないもの: **hello にあって記録に無いチャネル**（新設）。
    較正された値がまだ無いので、間違った対応にはならない。初回起動で
    全チャネルが「不一致」になってしまうのも避ける。
    """
    return sorted(
        channel for channel, rom in recorded.items() if observed.get(channel, _ABSENT) != rom
    )


@dataclass
class Engine:
    """サンプルごと・時刻ごとに評価する。

    **保存と同じ時計を使う**（#42）。継続時間の判定が実時間に依存すると、
    圧縮再生でルールを検証できない（決定記録 0007 §2.11）。
    """

    rules: RuleSet
    catalog: MetricCatalog
    store: SqliteStore
    clock: Clock
    read_control_traces: bool = True
    """fand の decision trace を読むルール（決定記録 0148）を評価するか。

    **実時間の取り込みでだけ True にする**（`daemon.build()`）。再生・時間圧縮では評価時刻が
    CSV・シナリオの時刻で、別プロセスが壁時計で書いた trace と比べられない（0148 §2.1）。
    """
    monotonic: MonotonicClock = field(default_factory=SystemMonotonicClock)
    """fand の trace を読むルールの発火と解除の待ちを測る単調時計（決定記録 0148 §2.2）。

    trace の `ts_ms` は fand が壁時計で付けるので、NTP の補正で前へ跳ぶと 2 行で待ちが満ち、
    後ろへ跳ぶと壁時計が追いつくまで待ちが止まる。試験では手で進める時計に差し替える。
    """
    _trace_seq: int | None = field(default=None)
    """最後に読んだ trace の `seq`（記録の順の番号。再利用しない。0071 §2.2a）。"""
    _trace_seen_mono_ms: int = field(default=0)
    """`_trace_seq` の行を**初めて読んだ**ときの単調時計。同じ行を読み直しても変えない。"""
    _states: dict[tuple[str, str | None], RuleState] = field(default_factory=dict)
    _last_sample_ms: int | None = field(default=None)
    _now_ms: int = field(default=0)
    """評価中の時刻（決定記録 0012 §2.2）。**巻き戻さない。**

    駆動する時刻は2つの出どころを持つ。サンプルがあるときはその受信時刻、
    無いときは `clock.now_ms()`。どちらも同じ「ホスト受信時刻」の軸だが、
    処理が滞ると前者が後者より古くなる。単調にしておかないと、
    経過時間が負になって継続時間の判定が動かなくなる。
    """

    def __post_init__(self) -> None:
        known = set(self.catalog.metrics) | set(self.catalog.derived)
        for name, rule in self.rules:
            for metric in rule_metrics(rule):
                if metric not in known:
                    # 綴り違いは「一度も成立しないルール」として静かに通る。読み込み時に落とす
                    raise ValueError(f"{name}: 未知のメトリクス {metric!r}（config/metrics.yaml）")
        self._hydrate()

    def _hydrate(self) -> None:
        """DB に残っている未解決のアラートを状態として取り込む。

        **これが無いと、再起動したデーモンは古いアラートを解除できない。**
        条件が解けていても `firing` のまま永久に残り、逆にまだ異常なら
        同じ事象の行がもう1つ増える。
        """
        # **件数の上限なしに、有効な行だけを読む**（決定記録 0139 §2.3.2）。解決済みを含む
        # 新しい N 件で読むと、古い発火中の故障アラートや pending が窓の外に落ちる
        for alert in self.store.active_alerts():
            state = self._state(alert.rule_id, alert.metric)
            state.since_ms = alert.started_ms
            state.alert_id = alert.id
            state.firing = alert.state.value == "firing"
            state.severity = alert.severity.value
            self._advance(alert.started_ms)
            if not state.firing and alert.rule_id in self._latest_rule_ids():
                # DB の最新値を読むルールの発火待ちは、止まっていた間を連続とみなさない
                # （決定記録 0131 §2.3。欠けた入力で発火待ちを取り消すのと同じ扱い）。
                # 残すと、再起動の直後に 1 回成立しただけで継続時間を満たしたことになる
                self._cancel_pending(alert.rule_id, alert.metric)
        self._cancel_hydrated_gaps()

    def _cancel_hydrated_gaps(self) -> None:
        """読み戻した発火待ちのうち、発火中の故障アラートに依るものを取り消す（0139 §2.3.2）。

        更新の前のエンジンが取り消さずに残した状態（移行）も、ここで同じに閉じる。
        発火中の故障アラートは既に人に届いているので、黙った遅れにならない。
        **いまの設定で無効の故障ルールの行は根拠にしない**（通知の経路として働いていない）。
        発火の時刻の前後は比べない（観測の切れ目をまたいだ継続を数えない）。
        停止の空白だけでは取り消さない（空白を知らせる信号が無く、黙った遅れになる。0139 §5 の 4）。
        """
        fault_firing = self._fault_firing()
        for name in GAP_RESET_RULES:
            rule = getattr(self.rules, name)
            rule_id = name.upper()
            state = self._states.get((rule_id, rule.metric))
            if state is None or state.since_ms is None or state.firing:
                continue
            if fault_firing or any(
                self._missing_firing(metric) for metric in self._input_metrics(rule.metric)
            ):
                self._cancel_pending(rule_id, rule.metric)

    def begin(self, at_ms: int) -> None:
        """監視を始めた時刻を基準にする。

        **これが無いと、起動時からデバイスが無い場合に `SENSOR_FAULT` が
        永久に鳴らない。** 最初のサンプルが来るまで無音を測る基準が無いため。
        「いつから届いていないか」は「いつから見ているか」で決まる。
        """
        self._advance(at_ms)
        if self._last_sample_ms is None:
            self._last_sample_ms = at_ms

    # ------------------------------------------------------------------ 入口

    def on_sample(self, sample: Sample) -> list[Transition]:
        """1サンプルぶんの評価。取り込みループから呼ぶ。

        **サンプルの受信時刻で評価する。** 時計は処理より先に進んでいることがある。
        """
        previous_ms = self._last_sample_ms
        self._advance(sample.ts_ms)
        readings = {reading.metric: reading for reading in sample.readings}
        values: dict[str, float | None] = {
            metric: (reading.value if reading.quality is Quality.OK else None)
            for metric, reading in readings.items()
        }
        values.update(compute_derived(readings, self.catalog))

        transitions: list[Transition] = []
        # **`_last_sample_ms` を更新する前に**、直前のサンプルとの空白を無音として評価する
        # （決定記録 0139 §2.3.3）。一括再生では `on_tick()` が呼ばれず、
        # 更新した後では空白が見えない
        transitions += self._evaluate_sample_gap(previous_ms, sample.ts_ms)
        self._last_sample_ms = sample.ts_ms
        transitions += self._evaluate_silence()
        transitions += self._evaluate_missing(readings)
        for name in ("recirculation", "intake_high", "airflow_degraded", "room_high"):
            rule = getattr(self.rules, name)
            transitions += self._evaluate_threshold(name, rule, values.get(rule.metric), readings)
        transitions += self._evaluate_range(
            "humidity_out_of_range",
            self.rules.humidity_out_of_range,
            values.get(self.rules.humidity_out_of_range.metric),
            readings,
        )
        transitions += self._evaluate_slope("rapid_rise", self.rules.rapid_rise)
        transitions += self._evaluate_latest()
        transitions += self._evaluate_control()
        return transitions

    def on_tick(self) -> list[Transition]:
        """サンプルが来なくても呼ぶ。

        **無音の検出（FR-401）はこれが無いと動かない。** サンプルが来た時だけ
        評価すると、止まったことに気づけるのは再開した後になる。

        DB の最新値を読むルール（決定記録 0131 §2.3）もここで評価する。基板のサンプルが
        止まっていても、GPU の値は別プロセスが書き続けている。
        """
        self._advance(self.clock.now_ms())
        return self._evaluate_silence() + self._evaluate_latest() + self._evaluate_control()

    def on_hello(
        self,
        observed: dict[str, str | None],
        recorded: dict[str, str | None],
        *,
        at_ms: int | None = None,
    ) -> list[Transition]:
        """起動バナーを受けた（FR-403）。**ROM の不一致は「続く状態」として扱う。**

        `PROBE_CHANGED` が意味するのは「ROM が変わった瞬間」ではない。
        **「較正のオフセットが、いま間違ったプローブに対応している」という、
        人が直すまで続く状態**である。差し替えたまま較正し直さなければ、
        以降のすべての測定値に誤ったオフセットが乗り続ける。ΔT の判定
        （`d.intake_rise` など）は数℃の話なので、実害になる。

        点の出来事として即座に解決すると、**静かに間違ったまま運用が続くのを
        防ぐための仕組みが、まさに静かに消える。**

        解除は、人が較正をやり直して記録側の ROM を更新し、
        次の起動バナーで一致したとき。
        """
        rule = self.rules.probe_changed
        if not rule.enabled:
            return []
        self._advance(self.clock.now_ms() if at_ms is None else at_ms)
        changed = probe_mismatch(observed, recorded)
        state = self._state("PROBE_CHANGED", None)
        if changed:
            if state.since_ms is None:
                detail = f"較正が対応していないプローブ: {', '.join(changed)}"
                return self._begin(
                    "PROBE_CHANGED", None, rule.severity.value, None, None, 0.0, detail=detail
                )
            return []
        return self._end("PROBE_CHANGED", None) if state.since_ms is not None else []

    # ------------------------------------------------------------------ 各ルール

    def _evaluate_silence(self) -> list[Transition]:
        rule = self.rules.sensor_fault
        if not rule.enabled:
            return []
        if self._last_sample_ms is None:
            # 監視を始めた時刻が基準（`begin`）。呼ばれていなければここで立てる
            self._last_sample_ms = self._now_ms
            return []
        now = self._now_ms
        state = self._state("SENSOR_FAULT", None)
        silence_s = (now - self._last_sample_ms) / 1000

        if silence_s >= rule.silence_s:
            state.resumed_ms = None
            return self._fault_silence(silence_s)
        if state.since_ms is None:
            return []
        # 届き始めた。**すぐには解除しない。** 1件届いただけで復旧とみなすと、
        # 断続的な接続で発火と解除を繰り返す
        if state.resumed_ms is None:
            state.resumed_ms = now
        if (now - state.resumed_ms) / 1000 < rule.clear_s:
            return []
        return self._end("SENSOR_FAULT", None)

    def _evaluate_sample_gap(self, previous_ms: int | None, sample_ms: int) -> list[Transition]:
        """新しいサンプルと直前のサンプルの時刻の差を無音として評価する（決定記録 0139 §2.3.3）。

        一括再生・処理の滞りでは `on_tick()` が無音を見ないので、CSV の空白を
        ここで `SENSOR_FAULT` にする。差が `silence_s` 以上なら、発火していなければ
        このサンプルの時刻で開いて発火させ、5 つのルールの発火待ちを取り消す。
        そのサンプルは届いているので、再開の時刻をこのサンプルの時刻にし直す
        （前の再開からの数えは捨てる）。実時間の運転では `on_tick()` が先に発火させて
        いるので、発火中のまま再開の扱いへ進むだけになる。
        """
        rule = self.rules.sensor_fault
        if not rule.enabled or previous_ms is None:
            return []
        gap_s = (sample_ms - previous_ms) / 1000
        if gap_s < rule.silence_s:
            return []
        transitions = self._fault_silence(gap_s)
        state = self._state("SENSOR_FAULT", None)
        if state.since_ms is not None:
            state.resumed_ms = self._now_ms
        return transitions

    def _fault_silence(self, silence_s: float) -> list[Transition]:
        """無音の条件（`silence_s` 以上）を満たした評価。**`sensor_fault` が有効なときだけ呼ぶ。**

        `SENSOR_FAULT` を開く・続ける。発火中なら（新しく発火したか既に発火中かによらず）、
        5 つのルールの発火待ちを取り消す（決定記録 0139 §2.3.3）。「戻ってすぐまた空白」も
        2 回目の空白で取り消す。発火中の `SENSOR_FAULT` は人に届いているので、黙った遅れにならない。
        """
        rule = self.rules.sensor_fault
        state = self._state("SENSOR_FAULT", None)
        if state.since_ms is None:
            transitions = self._begin(
                "SENSOR_FAULT",
                None,
                rule.severity.value,
                rule.silence_s,
                silence_s,
                rule.fire_after_s,
                detail=f"{silence_s:.0f} 秒サンプルが届いていない",
            )
        else:
            transitions = self._continue("SENSOR_FAULT", None, silence_s, rule.fire_after_s)
        if state.firing:
            for name in GAP_RESET_RULES:
                self._cancel_pending(name.upper(), getattr(self.rules, name).metric)
        return transitions

    def _evaluate_missing(self, readings: Mapping[str, Reading]) -> list[Transition]:
        rule = self.rules.sensor_missing
        if not rule.enabled:
            return []
        transitions: list[Transition] = []
        for metric, reading in readings.items():
            quality = reading.quality
            if metric.startswith("sys."):
                continue  # 事象メトリクスは毎回来るものではない（決定記録 0008 §2.1.2）
            state = self._state("SENSOR_MISSING", metric)
            if quality is not Quality.OK:
                state.streak = max(state.streak, 0) + 1
                if state.since_ms is None and state.streak >= rule.consecutive:
                    transitions += self._begin(
                        "SENSOR_MISSING",
                        metric,
                        rule.severity.value,
                        float(rule.consecutive),
                        float(state.streak),
                        0.0,
                        detail=f"{state.streak} サンプル連続で ok でない",
                    )
                elif state.since_ms is not None:
                    transitions += self._continue(
                        "SENSOR_MISSING", metric, float(state.streak), 0.0
                    )
            else:
                state.streak = min(state.streak, 0) - 1
                if state.since_ms is not None and -state.streak >= rule.clear_consecutive:
                    transitions += self._end("SENSOR_MISSING", metric)
        return transitions

    def _evaluate_threshold(
        self,
        name: str,
        rule: ThresholdRule,
        value: float | None,
        readings: Mapping[str, Reading],
    ) -> list[Transition]:
        if not rule.enabled:
            return []
        rule_id = name.upper()
        if value is None:
            self._reset_on_missing(rule_id, rule.metric, readings)
            return []
        state = self._state(rule_id, rule.metric)
        # ヒステリシス: 成立中は解除の閾値まで下がるのを待つ
        limit = rule.clear if state.since_ms is not None else rule.threshold
        if value > limit:
            if state.since_ms is None:
                return self._begin(
                    rule_id,
                    rule.metric,
                    rule.severity.value,
                    rule.threshold,
                    value,
                    rule.fire_after_s,
                )
            return self._continue(rule_id, rule.metric, value, rule.fire_after_s)
        return self._end(rule_id, rule.metric) if state.since_ms is not None else []

    def _evaluate_range(
        self,
        name: str,
        rule: RangeRule,
        value: float | None,
        readings: Mapping[str, Reading],
    ) -> list[Transition]:
        if not rule.enabled:
            return []
        rule_id = name.upper()
        if value is None:
            self._reset_on_missing(rule_id, rule.metric, readings)
            return []
        state = self._state(rule_id, rule.metric)
        if state.since_ms is None:
            outside = value < rule.low or value > rule.high
            threshold = rule.low if value < rule.low else rule.high
        else:
            outside = value < rule.low_clear or value > rule.high_clear
            threshold = rule.low if value < rule.low_clear else rule.high
        if outside:
            if state.since_ms is None:
                return self._begin(
                    rule_id, rule.metric, rule.severity.value, threshold, value, rule.fire_after_s
                )
            return self._continue(rule_id, rule.metric, value, rule.fire_after_s)
        return self._end(rule_id, rule.metric) if state.since_ms is not None else []

    def _evaluate_slope(self, name: str, rule: SlopeRule) -> list[Transition]:
        if not rule.enabled:
            return []
        now = self._now_ms
        window_ms = int(rule.slope_window_s * 1000)
        slope = self.store.stats(rule.metric, now - window_ms, now + 1).slope_per_min
        if slope is None:
            return []
        rule_id = name.upper()
        state = self._state(rule_id, rule.metric)
        limit = rule.clear if state.since_ms is not None else rule.threshold
        if slope > limit:
            if state.since_ms is None:
                return self._begin(
                    rule_id,
                    rule.metric,
                    rule.severity.value,
                    rule.threshold,
                    slope,
                    rule.fire_after_s,
                    detail=f"{slope:.2f} ℃/分で上昇している",
                )
            return self._continue(rule_id, rule.metric, slope, rule.fire_after_s)
        return self._end(rule_id, rule.metric) if state.since_ms is not None else []

    def _latest_rule_ids(self) -> frozenset[str]:
        """DB の最新値を読むルールの rule_id（決定記録 0131 §2.3）。"""
        return frozenset(
            name.upper()
            for name, rule in self.rules
            if isinstance(
                rule,
                FlagOffRule
                | HotAndSlowRule
                | LatestThresholdRule
                | ControlStateRule
                | ControlSilenceRule,
            )
        )

    def _evaluate_latest(self) -> list[Transition]:
        """DB の最新値を評価時刻で読んで評価するルール（決定記録 0131 §2.3）。

        **`at_ms` は評価時刻**（0012 §2.2）。圧縮再生・一括再生でも実運用と同じ判定になる。
        stale は `SqliteStore.latest()` が `config/quality.yaml` の閾値で落とした品質を
        そのまま使う（閾値を 2 か所に持たない）。
        """
        not_manual = self.rules.gpu_fan_not_manual
        below_curve = self.rules.gpu_fan_below_curve
        connector = (
            ("connector_temp_high", self.rules.connector_temp_high),
            ("connector_temp_critical", self.rules.connector_temp_critical),
        )
        if not (
            not_manual.enabled or below_curve.enabled or any(rule.enabled for _, rule in connector)
        ):
            return []
        latest = self.store.latest(at_ms=self._now_ms)
        transitions: list[Transition] = []
        transitions += self._evaluate_flag_off("gpu_fan_not_manual", not_manual, latest)
        transitions += self._evaluate_hot_and_slow("gpu_fan_below_curve", below_curve, latest)
        for name, rule in connector:
            transitions += self._evaluate_latest_threshold(name, rule, latest)
        return transitions

    def _evaluate_flag_off(
        self, name: str, rule: FlagOffRule, latest: Mapping[str, LatestReading]
    ) -> list[Transition]:
        if not rule.enabled:
            return []
        rule_id = name.upper()
        value = _current(latest, rule.metric)
        if value is None:
            self._reset_on_gap(rule_id, rule.metric)
            return []
        state = self._state(rule_id, rule.metric)
        if value != 1.0:
            # 手動でない。解除の待ちは途切れた
            state.resumed_ms = None
            detail = (
                f"{rule.metric} = {value:g}（1 でない）。"
                "GPU の Fan を曲線のサービスが握っていない。"
                f"{_GPU_FAN_SERVICE_HINT}"
            )
            if state.since_ms is None:
                return self._begin(
                    rule_id,
                    rule.metric,
                    rule.severity.value,
                    None,
                    value,
                    rule.fire_after_s,
                    detail=detail,
                )
            return self._continue(rule_id, rule.metric, value, rule.fire_after_s, detail=detail)
        if state.since_ms is None:
            return []
        if not state.firing:
            return self._end(rule_id, rule.metric)  # 発火待ちの揺らぎは消す（0012 §2.4）
        # **1 回の 1 で解除しない。** 再起動を繰り返している間も一瞬は手動になる
        if state.resumed_ms is None:
            state.resumed_ms = self._now_ms
        if (self._now_ms - state.resumed_ms) / 1000 < rule.clear_s:
            return []
        return self._end(rule_id, rule.metric)

    def _evaluate_hot_and_slow(
        self, name: str, rule: HotAndSlowRule, latest: Mapping[str, LatestReading]
    ) -> list[Transition]:
        if not rule.enabled:
            return []
        rule_id = name.upper()
        core = _current(latest, rule.core_metric)
        fan = _current(latest, rule.fan_metric)
        if core is None or fan is None:
            # 片方だけが欠けても欠けとみなす（0131 §2.3）
            self._reset_on_gap(rule_id, rule.fan_metric)
            return []
        state = self._state(rule_id, rule.fan_metric)
        if state.since_ms is None:
            holds = core >= rule.core_threshold and fan < rule.fan_below
        else:
            # ヒステリシス: 成立中は解除の閾値を越えるまで成立し続ける
            holds = not (core < rule.core_clear or fan >= rule.fan_clear)
        if not holds:
            return self._end(rule_id, rule.fan_metric) if state.since_ms is not None else []
        detail = (
            f"{rule.core_metric} = {core:.1f} で {rule.fan_metric} = {fan:.0f}"
            f"（{rule.fan_below:g} 未満）。GPU の Fan が曲線どおりに回っていない。"
            f"{_GPU_FAN_SERVICE_HINT}"
        )
        if state.since_ms is None:
            return self._begin(
                rule_id,
                rule.fan_metric,
                rule.severity.value,
                rule.fan_below,
                fan,
                rule.fire_after_s,
                detail=detail,
            )
        return self._continue(rule_id, rule.fan_metric, fan, rule.fire_after_s, detail=detail)

    def _evaluate_latest_threshold(
        self, name: str, rule: LatestThresholdRule, latest: Mapping[str, LatestReading]
    ) -> list[Transition]:
        """DB の最新値のしきい値（決定記録 0145 §2.1）。成立は `>= threshold`、解除は `< clear`。

        欠け・stale・suspect（0 °C 未満の断線など）は高温と取り違えない。発火待ちは取り消し、
        発火中は保つ（0131 §2.3。0145 §2.3）。
        """
        if not rule.enabled:
            return []
        rule_id = name.upper()
        value = _current(latest, rule.metric)
        if value is None:
            self._reset_on_gap(rule_id, rule.metric)
            return []
        state = self._state(rule_id, rule.metric)
        # ヒステリシス: 成立中は解除の閾値を下回るまで成立し続ける
        limit = rule.clear if state.since_ms is not None else rule.threshold
        if value < limit:
            return self._end(rule_id, rule.metric) if state.since_ms is not None else []
        if state.since_ms is None:
            return self._begin(
                rule_id,
                rule.metric,
                rule.severity.value,
                rule.threshold,
                value,
                rule.fire_after_s,
            )
        return self._continue(rule_id, rule.metric, value, rule.fire_after_s)

    def _evaluate_control(self) -> list[Transition]:
        """fand の最後の decision trace を読んで評価するルール（決定記録 0148）。

        **最後に記録した 1 行**（`latest_control_trace()`。0071 §2.2）の `safety_state` と
        `faults` だけを見る。fand は `fault_clear_hold_ms` の間状態を保つので、評価の間隔
        （約 1 s）で見落とさない（0148 §3）。
        trace が 1 行も無い DB（fand を導入していない機械）では何もしない（0148 §2.8）。

        **発火の待ちと解除の待ちは、新しい行を初めて読んだときの単調時計で測る。**
        評価は約 1 s ごとに同じ行を何度も読む。評価の時刻で測ると、fand が 1 行書いて
        止まったときに、その 1 行だけで待ちが満ちて発火中のアラートを解いてしまう。
        trace の `ts_ms` で測ると、壁時計が前へ跳んだとき隣り合う 2 行で満ち、
        後ろへ跳んだとき止まる。新しい行かどうかは `seq`（記録の順の番号）が変わったかで決め、
        壁時計の大小に頼らない。途絶の 60 s だけは
        `ts_ms` と評価時刻の差で測る（0148 §2.2）。
        """
        emergency = self.rules.fand_emergency
        degraded = self.rules.fand_degraded
        silent = self.rules.fand_trace_silent
        if not self.read_control_traces or not (
            emergency.enabled or degraded.enabled or silent.enabled
        ):
            return []
        trace = self.store.latest_control_trace()
        if trace is None:
            self._reset_on_gap("FAND_EMERGENCY", None)
            self._reset_on_gap("FAND_DEGRADED", None)
            return []
        if trace.seq != self._trace_seq:
            self._trace_seq = trace.seq
            self._trace_seen_mono_ms = self.monotonic.monotonic_ms()
        seen_ms = self._trace_seen_mono_ms
        status = read_fand_status(trace)
        # 古さは**いまの壁時計**と比べる。巻き戻さない評価時刻（`_now_ms`）と比べると、
        # ホストの時計が silence_s より大きく戻ったとき、両方のプロセスが直った時刻で
        # 書いた新しい行まで古く見え、途絶を誤って鳴らし、時計が追いつくまで EMERGENCY を
        # 解けない。fand も同じ壁時計で書く（0148 §2.2）。時計が戻って trace が先にある
        # ときは新しいとみなす（年齢は負）
        age_s = (self.clock.now_ms() - trace.ts_ms) / 1000
        fresh = status is not None and age_s < silent.silence_s
        transitions: list[Transition] = []
        if silent.enabled:
            if status is None:
                detail = (
                    f"最新の decision trace を読めない（schema_version {trace.schema_version}"
                    f"・tick {trace.tick_id}）"
                )
            else:
                detail = f"最後の decision trace から {age_s:.0f} 秒（tick {trace.tick_id}）"
            transitions += self._evaluate_trace_silence(silent, fresh, age_s, seen_ms, detail)
        current = status if fresh else None
        transitions += self._evaluate_control_state(
            "fand_emergency",
            emergency,
            current,
            seen_ms,
            target="emergency",
            worse=frozenset(),
        )
        transitions += self._evaluate_control_state(
            "fand_degraded",
            degraded,
            current,
            seen_ms,
            target="degraded",
            worse=frozenset({"emergency"}),
        )
        return transitions

    def _evaluate_trace_silence(
        self, rule: ControlSilenceRule, fresh: bool, age_s: float, seen_ms: int, detail: str
    ) -> list[Transition]:
        rule_id = "FAND_TRACE_SILENT"
        state = self._state(rule_id, None)
        if not fresh:
            state.resumed_ms = None
            if state.since_ms is None:
                return self._begin(
                    rule_id,
                    None,
                    rule.severity.value,
                    rule.silence_s,
                    age_s,
                    rule.fire_after_s,
                    detail=detail,
                )
            return self._continue(rule_id, None, age_s, rule.fire_after_s, detail=detail)
        if state.since_ms is None:
            return []
        if not state.firing:
            return self._end(rule_id, None)
        # 1 行届いただけで復旧とみなさない。読める新しい行が clear_s の幅（単調時計）で
        # 届き続けたら解く（同じ行を読み続けても進まない）
        return self._trace_clear(rule_id, state, seen_ms, rule.clear_s)

    def _evaluate_control_state(
        self,
        name: str,
        rule: ControlStateRule,
        status: FandStatus | None,
        seen_ms: int,
        *,
        target: str,
        worse: frozenset[str],
    ) -> list[Transition]:
        """`safety_state` が `target` の間を成立とみなす（0148 §2.2）。

        `worse`（DEGRADED に対する EMERGENCY）の間は始めも発火も解除もしない。悪化で
        「解けました」を出さず、2 通を同時に出さないため。trace が古い・読めない（`status` が
        `None`）周期は評価しない（発火待ちは取り消し、発火中は保つ。0148 §2.3）。
        発火と解除の待ちは行を初めて読んだときの単調時計（`seen_ms`）で測る（`_evaluate_control`）。
        """
        if not rule.enabled:
            return []
        rule_id = name.upper()
        if status is None:
            self._reset_on_gap(rule_id, None)
            return []
        state = self._state(rule_id, None)
        if status.safety_state == target:
            state.resumed_ms = None
            detail = describe_faults(status.faults, rule.fault_labels)
            if state.since_ms is None:
                state.trace_since_mono_ms = seen_ms
                # 発火は下で単調時計で判定する。ここでは即時の発火だけを任せる
                return self._begin(
                    rule_id,
                    None,
                    rule.severity.value,
                    None,
                    None,
                    rule.fire_after_s,
                    detail=detail,
                )
            if state.firing:
                return []
            if state.trace_since_mono_ms is None:
                state.trace_since_mono_ms = seen_ms
            if (seen_ms - state.trace_since_mono_ms) / 1000 < rule.fire_after_s:
                return []
            return self._fire(rule_id, None, None, detail)
        if status.safety_state in worse:
            # 発火中は保つ（悪化で「解けました」を出さない）。発火待ちは取り消し、悪い側の状態が
            # 終わってから数え直す（0148 §2.2）。残すと、その間の時間まで数えて戻った直後に発火する
            state.resumed_ms = None
            if not state.firing:
                self._cancel_pending(rule_id, None)
            return []
        if state.since_ms is None:
            return []
        if not state.firing:
            return self._end(rule_id, None)  # 発火待ちの揺らぎは消す（0012 §2.4）
        return self._trace_clear(rule_id, state, seen_ms, rule.clear_s)

    def _trace_clear(
        self, rule_id: str, state: RuleState, seen_ms: int, clear_s: float
    ) -> list[Transition]:
        """解除の待ちを、行を初めて読んだときの単調時計で測る（0148）。

        **同じ行の繰り返しでは進まない**（`seen_ms` は新しい行が来たときだけ変わる）。
        """
        if state.resumed_ms is None:
            state.resumed_ms = seen_ms
        if (seen_ms - state.resumed_ms) / 1000 < clear_s:
            return []
        return self._end(rule_id, None)

    def _reset_on_gap(self, rule_id: str, metric: str | None) -> None:
        """欠けた・stale の入力を受けた（決定記録 0131 §2.3）。**連続の時間を数え直す。**

        - 発火待ちは取り消す（行を消す。遷移は配らない）。回復後に成立したら、その時刻から数え直す
        - 発火中は**解除しない**。解除の待ちの起点だけを戻し、回復後にもう一度待つ

        **DB の最新値を読むルールだけが呼ぶ。** しきい値・範囲のルールは、欠けを知らせる
        `SENSOR_MISSING` / `SENSOR_FAULT` が発火したときだけ取り消す（`_reset_on_missing()`。
        決定記録 0139。境界が違うのは 0139 §3 のとおり）。
        """
        state = self._state(rule_id, metric)
        if state.since_ms is None:
            return
        if state.firing:
            state.resumed_ms = None
            return
        self._cancel_pending(rule_id, metric)

    def _reset_on_missing(self, rule_id: str, metric: str, readings: Mapping[str, Reading]) -> None:
        """値が無いサンプル。欠けを知らせる `SENSOR_MISSING` が発火中なら発火待ちを取り消す。

        決定記録 0139 §2.3.1。入力のメトリクスのうち、**このサンプルで ok でなく**、
        その `SENSOR_MISSING` が発火中（いまの設定で有効）のものが 1 つでもあれば取り消す。
        独立の欠けの数を持たないので、取り消しの条件と故障アラートの条件が食い違わない。
        1〜4 サンプルの欠け・欄の省略・`sensor_missing` の無効化では取り消さない
        （黙って発火を遅らせない）。発火中のルールは何もしない（回復後に解除の閾値で解く）。
        """
        for name in self._input_metrics(metric):
            reading = readings.get(name)
            if reading is None or reading.quality is Quality.OK:
                continue
            if self._missing_firing(name):
                self._cancel_pending(rule_id, metric)
                return

    def _input_metrics(self, metric: str) -> tuple[str, ...]:
        """ルールの入力のメトリクス。派生値なら `minuend` / `subtrahend` の 2 つ。"""
        derived = self.catalog.derived.get(metric)
        if derived is None:
            return (metric,)
        return (derived.minuend, derived.subtrahend)

    def _missing_firing(self, metric: str) -> bool:
        """`metric` の `SENSOR_MISSING` が発火中で、いまの設定で有効か（0139 §2.3）。"""
        if not self.rules.sensor_missing.enabled:
            return False
        state = self._states.get(("SENSOR_MISSING", metric))
        return state is not None and state.firing

    def _fault_firing(self) -> bool:
        """`SENSOR_FAULT` が発火中で、いまの設定で有効か（0139 §2.3）。"""
        if not self.rules.sensor_fault.enabled:
            return False
        state = self._states.get(("SENSOR_FAULT", None))
        return state is not None and state.firing

    def _cancel_pending(self, rule_id: str, metric: str | None) -> None:
        """発火待ちを取り消す。継続時間に届かなかった揺らぎと同じく行を消す（0012 §2.4）。"""
        state = self._state(rule_id, metric)
        if state.firing:
            return
        if state.alert_id is not None:
            self.store.delete_alert(state.alert_id)
        state.since_ms = None
        state.alert_id = None
        state.resumed_ms = None

    # ------------------------------------------------------------------ 状態機械

    def _advance(self, proposed_ms: int) -> int:
        """評価時刻を進める。**巻き戻さない**（決定記録 0012 §2.2）。"""
        self._now_ms = max(self._now_ms, proposed_ms)
        return self._now_ms

    def _state(self, rule_id: str, metric: str | None) -> RuleState:
        return self._states.setdefault((rule_id, metric), RuleState())

    def _begin(
        self,
        rule_id: str,
        metric: str | None,
        severity: str,
        threshold: float | None,
        value: float | None,
        fire_after_s: float,
        *,
        detail: str | None = None,
    ) -> list[Transition]:
        now = self._now_ms
        state = self._state(rule_id, metric)
        existing = self.store.active_alert(rule_id, metric)
        if existing is not None:
            # デーモンを再起動しても同じ事象で行を作り直さない。
            # 作り直すと、1つの故障が再起動の回数だけ履歴に並ぶ
            state.since_ms = existing.started_ms
            state.alert_id = existing.id
            state.firing = existing.state.value == "firing"
            state.severity = existing.severity.value
            if state.firing:
                return []
            return self._continue(rule_id, metric, value, fire_after_s)
        state.since_ms = now
        state.firing = False
        state.severity = severity
        state.alert_id = self.store.open_alert(
            rule_id=rule_id,
            severity=severity,
            metric=metric,
            started_ms=now,
            threshold=threshold,
            trigger_value=value,
            detail=detail,
        )
        transitions = [Transition(rule_id, metric, "pending", value, detail, severity)]
        if fire_after_s <= 0:
            transitions += self._fire(rule_id, metric, value, detail)
        return transitions

    def _continue(
        self,
        rule_id: str,
        metric: str | None,
        value: float | None,
        fire_after_s: float,
        *,
        detail: str | None = None,
    ) -> list[Transition]:
        state = self._state(rule_id, metric)
        if state.firing or state.since_ms is None:
            return []
        if (self._now_ms - state.since_ms) / 1000 < fire_after_s:
            return []
        return self._fire(rule_id, metric, value, detail)

    def _fire(
        self, rule_id: str, metric: str | None, value: float | None, detail: str | None
    ) -> list[Transition]:
        state = self._state(rule_id, metric)
        if state.alert_id is None:
            return []
        now = self._now_ms
        self.store.fire_alert(state.alert_id, fired_ms=now, trigger_value=value, detail=detail)
        state.firing = True
        LOGGER.warning(
            "アラートが発火した",
            extra={logs.FIELDS_KEY: {"rule": rule_id, "metric": metric, "value": value}},
        )
        return [Transition(rule_id, metric, "firing", value, detail, state.severity)]

    def _end(self, rule_id: str, metric: str | None) -> list[Transition]:
        state = self._state(rule_id, metric)
        if state.alert_id is None:
            return []
        now = self._now_ms
        was_firing = state.firing
        severity = state.severity  # 解除するとDBから引けなくなる。先に控える
        if was_firing:
            self.store.resolve_alert(state.alert_id, resolved_ms=now)
            LOGGER.info(
                "アラートが解除された", extra={logs.FIELDS_KEY: {"rule": rule_id, "metric": metric}}
            )
        else:
            # 継続時間に届かなかった揺らぎは履歴に残さない。残すと本物が埋もれる
            self.store.delete_alert(state.alert_id)
        state.since_ms = None
        state.alert_id = None
        state.firing = False
        state.resumed_ms = None
        if not was_firing:
            return []
        return [Transition(rule_id, metric, "resolved", None, None, severity)]


_GPU_FAN_SERVICE_HINT = (
    "理由は `systemctl status coldaisle-gpu-fan` と `journalctl -u coldaisle-gpu-fan`"
    "（gpu_fan_model_mismatch などの event）で確かめる"
)
"""通知の文に載せる切り分けの案内（決定記録 0131 §2.1）。監視系は systemd も journal も読まない。"""


def _current(latest: Mapping[str, LatestReading], metric: str) -> float | None:
    """評価に使える最新値。**無い・ok でない（stale を含む）なら `None`**（0131 §2.3）。"""
    reading = latest.get(metric)
    if reading is None or reading.quality is not Quality.OK or reading.value is None:
        return None
    return reading.value
