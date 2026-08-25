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
from coldaisle.clock import Clock
from coldaisle.metrics import MetricCatalog, compute_derived
from coldaisle.rules.models import (
    RangeRule,
    RuleSet,
    SlopeRule,
    ThresholdRule,
)
from coldaisle.store import Quality, Reading, Sample, SqliteStore

LOGGER = logging.getLogger("coldaisle.rules")


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
    """無音から復帰した時刻（FR-401 用）。"""


@dataclass(frozen=True)
class Transition:
    """状態が変わったこと。通知（#20）はこれを見る。"""

    rule_id: str
    metric: str | None
    state: str
    value: float | None
    detail: str | None = None


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
    _states: dict[tuple[str, str | None], RuleState] = field(default_factory=dict)
    _last_sample_ms: int | None = field(default=None)
    _now_ms: int = field(default=0)
    """評価中の時刻。**サンプルの受信時刻**で評価する（`on_sample`）。

    処理が追いつくまでの間に時計が進むため、`clock.now_ms()` で評価すると
    一括再生で継続時間の条件が一瞬で満たされてしまう。
    時計を読むのはサンプルが無いとき（`on_tick`）だけ。
    """

    def __post_init__(self) -> None:
        known = set(self.catalog.metrics) | set(self.catalog.derived)
        for name in ("recirculation", "intake_high", "airflow_degraded", "rapid_rise", "room_high"):
            metric = getattr(self.rules, name).metric
            if metric not in known:
                # 綴り違いは「一度も成立しないルール」として静かに通る。読み込み時に落とす
                raise ValueError(f"{name}: 未知のメトリクス {metric!r}（config/metrics.yaml）")

    # ------------------------------------------------------------------ 入口

    def on_sample(self, sample: Sample) -> list[Transition]:
        """1サンプルぶんの評価。取り込みループから呼ぶ。

        **サンプルの受信時刻で評価する。** 時計は処理より先に進んでいることがある。
        """
        self._now_ms = sample.ts_ms
        self._last_sample_ms = sample.ts_ms
        readings = {reading.metric: reading for reading in sample.readings}
        values: dict[str, float | None] = {
            metric: (reading.value if reading.quality is Quality.OK else None)
            for metric, reading in readings.items()
        }
        values.update(compute_derived(readings, self.catalog))

        transitions: list[Transition] = []
        transitions += self._evaluate_silence()
        transitions += self._evaluate_missing(readings)
        for name in ("recirculation", "intake_high", "airflow_degraded", "room_high"):
            rule = getattr(self.rules, name)
            transitions += self._evaluate_threshold(name, rule, values.get(rule.metric))
        transitions += self._evaluate_range(
            "humidity_out_of_range",
            self.rules.humidity_out_of_range,
            values.get(self.rules.humidity_out_of_range.metric),
        )
        transitions += self._evaluate_slope("rapid_rise", self.rules.rapid_rise)
        return transitions

    def on_tick(self) -> list[Transition]:
        """サンプルが来なくても呼ぶ。

        **無音の検出（FR-401）はこれが無いと動かない。** サンプルが来た時だけ
        評価すると、止まったことに気づけるのは再開した後になる。
        """
        self._now_ms = self.clock.now_ms()
        return self._evaluate_silence()

    def on_probe_changed(self, channels: list[str]) -> list[Transition]:
        """起動バナーの ROM が前回と違った（FR-403）。

        **点の出来事なので、発火と同時に解決する。** 継続する状態ではないため、
        発生中のアラートとして残し続けると、いつまでも赤いままになる。
        記録は履歴として残り、通知（#20）は遷移を見て送る。
        """
        rule = self.rules.probe_changed
        if not rule.enabled or not channels:
            return []
        now = self._now_ms = self.clock.now_ms()
        detail = f"ROM が前回と違うチャネル: {', '.join(channels)}"
        alert_id = self.store.open_alert(
            rule_id="PROBE_CHANGED",
            severity=rule.severity.value,
            metric=None,
            started_ms=now,
            threshold=None,
            trigger_value=None,
            detail=detail,
        )
        self.store.fire_alert(alert_id, fired_ms=now, trigger_value=None)
        self.store.resolve_alert(alert_id, resolved_ms=now)
        LOGGER.warning("PROBE_CHANGED", extra={logs.FIELDS_KEY: {"channels": channels}})
        return [Transition("PROBE_CHANGED", None, "firing", None, detail)]

    # ------------------------------------------------------------------ 各ルール

    def _evaluate_silence(self) -> list[Transition]:
        rule = self.rules.sensor_fault
        if not rule.enabled or self._last_sample_ms is None:
            return []
        now = self._now_ms
        state = self._state("SENSOR_FAULT", None)
        silence_s = (now - self._last_sample_ms) / 1000

        if state.since_ms is None:
            if silence_s < rule.silence_s:
                return []
            state.resumed_ms = None
            return self._begin(
                "SENSOR_FAULT",
                None,
                rule.severity.value,
                rule.silence_s,
                silence_s,
                rule.fire_after_s,
                detail=f"{silence_s:.0f} 秒サンプルが届いていない",
            )

        if silence_s >= rule.silence_s:
            state.resumed_ms = None
            return self._continue("SENSOR_FAULT", None, silence_s, rule.fire_after_s)
        # 届き始めた。**すぐには解除しない。** 1件届いただけで復旧とみなすと、
        # 断続的な接続で発火と解除を繰り返す
        if state.resumed_ms is None:
            state.resumed_ms = now
        if (now - state.resumed_ms) / 1000 < rule.clear_s:
            return []
        return self._end("SENSOR_FAULT", None)

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
        self, name: str, rule: ThresholdRule, value: float | None
    ) -> list[Transition]:
        if not rule.enabled or value is None:
            return []
        rule_id = name.upper()
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

    def _evaluate_range(self, name: str, rule: RangeRule, value: float | None) -> list[Transition]:
        if not rule.enabled or value is None:
            return []
        rule_id = name.upper()
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

    # ------------------------------------------------------------------ 状態機械

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
        state.since_ms = now
        state.firing = False
        state.alert_id = self.store.open_alert(
            rule_id=rule_id,
            severity=severity,
            metric=metric,
            started_ms=now,
            threshold=threshold,
            trigger_value=value,
            detail=detail,
        )
        transitions = [Transition(rule_id, metric, "pending", value, detail)]
        if fire_after_s <= 0:
            transitions += self._fire(rule_id, metric, value, detail)
        return transitions

    def _continue(
        self, rule_id: str, metric: str | None, value: float | None, fire_after_s: float
    ) -> list[Transition]:
        state = self._state(rule_id, metric)
        if state.firing or state.since_ms is None:
            return []
        if (self._now_ms - state.since_ms) / 1000 < fire_after_s:
            return []
        return self._fire(rule_id, metric, value, None)

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
        return [Transition(rule_id, metric, "firing", value, detail)]

    def _end(self, rule_id: str, metric: str | None) -> list[Transition]:
        state = self._state(rule_id, metric)
        if state.alert_id is None:
            return []
        now = self._now_ms
        was_firing = state.firing
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
        return [Transition(rule_id, metric, "resolved", None)] if was_firing else []
