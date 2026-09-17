"""読み取り専用 Telemetry から不変の Control State Snapshot を作る。#102

このモジュールは収集・Safety 判定・actuation を持たない。収集層 (#65) が同じ
tick で固定した入力を受け、Controller が読む状態と品質を一箇所でそろえる。
期限は壁時計でなく、収集層が ``ts_ms`` の変化を観測した単調時計で数える
（決定記録 0028 §2.6）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.schema import Demand, PerZone
from coldaisle.metrics import MetricCatalog, compute_derived
from coldaisle.store.models import Quality, validate_metric

STATE_SNAPSHOT_SCHEMA_VERSION = 1
"""``ControlStateSnapshot`` の意味を識別する版。#82 が trace に載せる。"""


FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class _Frozen(BaseModel):
    """外部入力と snapshot を後から書き換えないための共通基底。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TelemetryImportance(StrEnum):
    """欠測の影響区分（決定記録 0029 §2.1）。"""

    CRITICAL = "critical"
    DEGRADED = "degraded"
    ADVISORY = "advisory"


class TelemetryHealth(StrEnum):
    """入力の健康度。Critical Safety の ``SafetyState`` とは別の軸。"""

    NORMAL = "normal"
    DEGRADED = "degraded"


class SignalSpec(_Frozen):
    """収集済み signal を読むための契約。

    ``stale_after_ms`` は #103 が検証した safety config などから渡す値であり、
    本モジュールは既定値・安全閾値を持たない。未設置の T_SENSOR は
    ``enabled=False`` とし、``missing`` と区別する。
    """

    metric: str
    importance: TelemetryImportance
    stale_after_ms: int | None = Field(default=None, gt=0)
    enabled: bool = True

    @model_validator(mode="after")
    def _enabled_signals_have_a_stale_limit(self) -> Self:
        validate_metric(self.metric)
        if self.enabled != (self.stale_after_ms is not None):
            raise ValueError(
                "有効な signal には stale_after_ms を指定し、無効な signal には指定しない"
            )
        return self


class CriticalTelemetryGroup(_Frozen):
    """全 signal が使えないときだけ Critical になる入力群（0029 §2.2）。"""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    metrics: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _contains_unique_stored_metrics(self) -> Self:
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("Critical group の metric は重複させない")
        for metric in self.metrics:
            validate_metric(metric)
        return self


class ControlInputContract(_Frozen):
    """#65 と State Estimator の読み取り専用境界。

    ``signals`` は期待する入力の全体である。frame に無い signal は、0 などで
    埋めず ``missing`` として snapshot へ明示する。
    """

    signals: tuple[SignalSpec, ...] = Field(min_length=1)
    critical_groups: tuple[CriticalTelemetryGroup, ...] = ()

    @model_validator(mode="after")
    def _groups_reference_known_signals(self) -> Self:
        metrics = tuple(spec.metric for spec in self.signals)
        if len(set(metrics)) != len(metrics):
            raise ValueError("signal contract の metric は重複させない")
        specs_by_metric = {spec.metric: spec for spec in self.signals}
        known = set(specs_by_metric)
        for group in self.critical_groups:
            unknown = set(group.metrics) - known
            if unknown:
                raise ValueError(f"Critical group に未定義の signal がある: {sorted(unknown)}")
            disabled = [metric for metric in group.metrics if not specs_by_metric[metric].enabled]
            if disabled:
                raise ValueError(f"Critical group の signal はすべて有効にする: {sorted(disabled)}")
        return self


class TelemetryReading(_Frozen):
    """#65 が収集した 1 signal の読み取り専用値。

    ``last_changed_mono_ms`` は、この signal の ``source_ts_ms`` が最後に変化した
    ことを収集側が観測した単調時計の時刻である。source の壁時計と現在時刻を
    引かないので、時刻合わせで stale 判定は壊れない。
    """

    metric: str
    value: FiniteFloat | None = None
    quality: Quality
    source_ts_ms: int = Field(ge=0)
    last_changed_mono_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _has_a_stored_metric_name(self) -> Self:
        validate_metric(self.metric)
        return self


class FanState(_Frozen):
    """Hardware Backend が読み取った fan 状態。書き込み API ではない。"""

    effective_demand: Demand | None = None
    pwm_raw: int | None = Field(default=None, ge=0, le=255)
    rpm: int | None = Field(default=None, ge=0)
    estimated_flow: FiniteFloat | None = Field(default=None, ge=0.0)


class ControlInputFrame(_Frozen):
    """1 control tick で固定された、収集層からの入力。

    収集側は source の可変キャッシュをこの tuple にコピーしてから渡す。Estimator
    は I/O をせず、途中で source が更新されてもこの tick の判断を混ぜない。
    """

    tick_id: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    monotonic_ms: int = Field(ge=0)
    readings: tuple[TelemetryReading, ...] = ()
    fans: PerZone[FanState] | None = None

    @model_validator(mode="after")
    def _readings_are_a_consistent_cut(self) -> Self:
        metrics = tuple(reading.metric for reading in self.readings)
        if len(set(metrics)) != len(metrics):
            raise ValueError("同一 control tick に同じ metric を複数入れない")
        if any(reading.last_changed_mono_ms > self.monotonic_ms for reading in self.readings):
            raise ValueError("future の単調時計を持つ reading は snapshot に入れない")
        return self


class SnapshotSignal(_Frozen):
    """snapshot 内の signal。欠測・無効・stale を値と混ぜずに残す。"""

    metric: str
    importance: TelemetryImportance
    enabled: bool
    value: FiniteFloat | None = None
    quality: Quality
    source_ts_ms: int | None = Field(default=None, ge=0)
    last_changed_mono_ms: int | None = Field(default=None, ge=0)
    age_ms: int | None = Field(default=None, ge=0)

    @property
    def available(self) -> bool:
        """制御器が数値として利用できるかを返す。"""
        return self.enabled and self.quality is Quality.OK and self.value is not None


class DerivedSignal(_Frozen):
    """保存しない派生値（決定記録 0002 §2.2）。"""

    metric: str
    value: FiniteFloat | None = None


class Trend(_Frozen):
    """2つの snapshot 間で計算した時間あたりの変化量。"""

    metric: str
    per_second: FiniteFloat
    from_mono_ms: int = Field(ge=0)
    to_mono_ms: int = Field(ge=0)


class ControlStateSnapshot(_Frozen):
    """Controller 群が同じ tick で共有する不変状態。"""

    schema_version: int = STATE_SNAPSHOT_SCHEMA_VERSION
    tick_id: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    monotonic_ms: int = Field(ge=0)
    signals: tuple[SnapshotSignal, ...]
    derived: tuple[DerivedSignal, ...]
    trends: tuple[Trend, ...]
    telemetry_health: TelemetryHealth
    critical_unavailable: tuple[str, ...]
    fans: PerZone[FanState] | None = None

    @property
    def signals_by_metric(self) -> dict[str, SnapshotSignal]:
        """metric を key にした読み取り用のコピーを返す。"""
        return {signal.metric: signal for signal in self.signals}

    @property
    def derived_by_metric(self) -> dict[str, float | None]:
        """派生値を key にした読み取り用のコピーを返す。"""
        return {signal.metric: signal.value for signal in self.derived}


class ControlStateEstimator:
    """収集済み input frame から状態を組み立てる純粋な State Estimator。"""

    def __init__(self, contract: ControlInputContract, catalog: MetricCatalog) -> None:
        self._contract = contract
        self._catalog = catalog

    def build(
        self,
        frame: ControlInputFrame,
        previous: ControlStateSnapshot | None = None,
    ) -> ControlStateSnapshot:
        """frame を snapshot に変換する。Safety state と demand は決めない。"""
        if previous is not None and previous.monotonic_ms >= frame.monotonic_ms:
            raise ValueError("trend の比較対象は現在 tick より過去の snapshot にする")

        received = {reading.metric: reading for reading in frame.readings}
        known = {spec.metric for spec in self._contract.signals}
        unknown = set(received) - known
        if unknown:
            raise ValueError(f"input contract にない signal を受け取った: {sorted(unknown)}")

        signals = tuple(
            self._to_snapshot_signal(spec, received.get(spec.metric), frame.monotonic_ms)
            for spec in self._contract.signals
        )
        by_metric = {signal.metric: signal for signal in signals}
        derived = tuple(
            DerivedSignal(metric=metric, value=value)
            for metric, value in compute_derived(by_metric, self._catalog).items()
        )
        critical_unavailable = self._critical_unavailable(by_metric)
        telemetry_health = self._telemetry_health(signals)
        trends = self._trends(signals, previous)

        return ControlStateSnapshot(
            tick_id=frame.tick_id,
            ts_ms=frame.ts_ms,
            monotonic_ms=frame.monotonic_ms,
            signals=signals,
            derived=derived,
            trends=trends,
            telemetry_health=telemetry_health,
            critical_unavailable=critical_unavailable,
            fans=frame.fans,
        )

    @staticmethod
    def _to_snapshot_signal(
        spec: SignalSpec,
        reading: TelemetryReading | None,
        now_mono_ms: int,
    ) -> SnapshotSignal:
        if not spec.enabled:
            return SnapshotSignal(
                metric=spec.metric,
                importance=spec.importance,
                enabled=False,
                quality=Quality.MISSING,
            )
        if reading is None:
            return SnapshotSignal(
                metric=spec.metric,
                importance=spec.importance,
                enabled=True,
                quality=Quality.MISSING,
            )
        assert spec.stale_after_ms is not None
        age_ms = now_mono_ms - reading.last_changed_mono_ms
        quality = reading.quality
        if quality is Quality.OK and age_ms > spec.stale_after_ms:
            quality = Quality.STALE
        return SnapshotSignal(
            metric=spec.metric,
            importance=spec.importance,
            enabled=True,
            value=reading.value,
            quality=quality,
            source_ts_ms=reading.source_ts_ms,
            last_changed_mono_ms=reading.last_changed_mono_ms,
            age_ms=age_ms,
        )

    def _critical_unavailable(self, signals: dict[str, SnapshotSignal]) -> tuple[str, ...]:
        unavailable = [
            spec.metric
            for spec in self._contract.signals
            if spec.enabled
            and spec.importance is TelemetryImportance.CRITICAL
            and not signals[spec.metric].available
        ]
        unavailable.extend(
            group.code
            for group in self._contract.critical_groups
            if all(not signals[metric].available for metric in group.metrics)
        )
        return tuple(unavailable)

    @staticmethod
    def _telemetry_health(signals: tuple[SnapshotSignal, ...]) -> TelemetryHealth:
        if any(
            signal.enabled
            and signal.importance is not TelemetryImportance.ADVISORY
            and not signal.available
            for signal in signals
        ):
            return TelemetryHealth.DEGRADED
        return TelemetryHealth.NORMAL

    @staticmethod
    def _trends(
        current: tuple[SnapshotSignal, ...],
        previous: ControlStateSnapshot | None,
    ) -> tuple[Trend, ...]:
        if previous is None:
            return ()
        previous_by_metric = previous.signals_by_metric
        trends: list[Trend] = []
        for signal in current:
            earlier = previous_by_metric.get(signal.metric)
            if (
                earlier is None
                or not signal.available
                or not earlier.available
                or signal.last_changed_mono_ms is None
                or earlier.last_changed_mono_ms is None
                or signal.last_changed_mono_ms <= earlier.last_changed_mono_ms
            ):
                continue
            assert signal.value is not None and earlier.value is not None
            elapsed_s = (signal.last_changed_mono_ms - earlier.last_changed_mono_ms) / 1_000
            trends.append(
                Trend(
                    metric=signal.metric,
                    per_second=(signal.value - earlier.value) / elapsed_s,
                    from_mono_ms=earlier.last_changed_mono_ms,
                    to_mono_ms=signal.last_changed_mono_ms,
                )
            )
        return tuple(trends)
