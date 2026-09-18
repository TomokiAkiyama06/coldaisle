"""同一 control tick の snapshot へ決定論的な一時 floor を適用する。#80

Reactive Guard は raw telemetry を取得せず、#102 の ``ControlStateSnapshot`` だけを
読む。絶対温度上限や telemetry fault は Critical Safety の責務であり、ここでは値を
補完せず、急な上昇を安全条件へ達する前に先回りする。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.control.config import GuardThresholdBand, ReactiveGuardConfig
from coldaisle.control.schema import GuardZoneOutput, PerZone, Reason, Zone
from coldaisle.control.state import ControlStateSnapshot, TelemetryHealth
from coldaisle.metrics import MetricCatalog

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class GuardThresholdProfile(StrEnum):
    """snapshot の入力品質に応じて選んだ閾値組。"""

    NORMAL = "normal"
    CONSERVATIVE = "conservative"


class GuardEvidenceSource(StrEnum):
    """trigger が snapshot のどの種類の値を読んだか。"""

    SIGNAL = "signal"
    DERIVED = "derived"
    TREND = "trend"


class GuardTransition(StrEnum):
    """decision trace に残す介入状態の変化。"""

    STARTED = "started"
    RELEASED = "released"


class GuardEvidence(_Frozen):
    """1 trigger の比較結果。閾値を含め、Replay で判断を説明可能にする。"""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    metric: str
    source: GuardEvidenceSource
    value: FiniteFloat
    activate_above: FiniteFloat
    clear_at_or_below: FiniteFloat
    active: bool


class GuardEvent(_Frozen):
    """介入の開始または解除。解除 tick でも理由を失わないための記録。"""

    zone: Zone
    transition: GuardTransition
    reason: Reason
    trigger_codes: tuple[str, ...] = ()


class ReactiveGuardDecision(_Frozen):
    """1 snapshot に対する Guard の出力と追跡情報。"""

    schema_version: Literal[1] = 1
    snapshot_schema_version: int = Field(ge=1)
    tick_id: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    monotonic_ms: int = Field(ge=0)
    threshold_profile: GuardThresholdProfile
    zones: PerZone[GuardZoneOutput]
    evidence: tuple[GuardEvidence, ...] = ()
    unavailable_inputs: tuple[str, ...] = ()
    events: tuple[GuardEvent, ...] = ()


@dataclass(frozen=True)
class _TriggerSpec:
    code: str
    metric: str
    source: GuardEvidenceSource
    config_field: str
    zones: frozenset[Zone]


@dataclass
class _ZoneRuntime:
    intervening: bool = False
    hold_until_mono_ms: int = 0
    origin_trigger_codes: tuple[str, ...] = ()
    release_causes: tuple[str, ...] = ()


_POWER_UNIT = "W"
"""Metric Catalog 上の電力の単位。Fallback Controller の Power 入力検証と同じ。"""

_STATIC_TRIGGERS: tuple[_TriggerSpec, ...] = (
    _TriggerSpec(
        code="cpu_temperature_rise",
        metric="cpu.package",
        source=GuardEvidenceSource.TREND,
        config_field="cpu_temperature_rate_c_per_s",
        zones=frozenset({Zone.TOP}),
    ),
    _TriggerSpec(
        code="gpu_temperature_rise",
        metric="gpu.0.core",
        source=GuardEvidenceSource.TREND,
        config_field="gpu_temperature_rate_c_per_s",
        zones=frozenset({Zone.FRONT, Zone.REAR}),
    ),
    _TriggerSpec(
        code="gpu_power_rise",
        metric="power.gpu.0",
        source=GuardEvidenceSource.TREND,
        config_field="gpu_power_rate_w_per_s",
        zones=frozenset({Zone.FRONT, Zone.REAR}),
    ),
    _TriggerSpec(
        code="intake_rise",
        metric="d.intake_rise",
        source=GuardEvidenceSource.DERIVED,
        config_field="intake_rise_c",
        zones=frozenset({Zone.FRONT, Zone.REAR}),
    ),
    _TriggerSpec(
        code="gpu_hotspot_high",
        metric="gpu.0.hotspot",
        source=GuardEvidenceSource.SIGNAL,
        config_field="gpu_hotspot_c",
        zones=frozenset({Zone.FRONT, Zone.REAR}),
    ),
)


class ReactiveGuard:
    """急な熱負荷へ floor を上げ、hysteresis と単調時計 hold で遅く解除する。"""

    def __init__(self, config: ReactiveGuardConfig, catalog: MetricCatalog) -> None:
        """Metric Catalog で設定由来の trigger metric の単位を検証してから組み立てる。

        W/s の閾値に温度など別単位の trend を比べると、Guard が誤って発火・解除する。
        Fallback Controller と同じく、名前が正しくても単位が違う設定は起動前に拒否する。
        """
        self._config = config
        triggers = list(_STATIC_TRIGGERS)
        if config.cpu_power_metric is not None:
            unit = catalog.unit_for(config.cpu_power_metric.value)
            if unit != _POWER_UNIT:
                raise ValueError(
                    "CPU Power trigger の metric は既知の電力(W)にする: "
                    f"metric={config.cpu_power_metric.value}, unit={unit}"
                )
            triggers.append(
                _TriggerSpec(
                    code="cpu_power_rise",
                    metric=config.cpu_power_metric.value,
                    source=GuardEvidenceSource.TREND,
                    config_field="cpu_power_rate_w_per_s",
                    zones=frozenset({Zone.TOP}),
                )
            )
        self._triggers = tuple(triggers)
        self._trigger_active = {trigger.code: False for trigger in self._triggers}
        self._zones = {zone: _ZoneRuntime() for zone in Zone}
        self._last_tick_id: int | None = None
        self._last_monotonic_ms: int | None = None
        self._last_snapshot: ControlStateSnapshot | None = None
        self._last_decision: ReactiveGuardDecision | None = None

    def evaluate(self, snapshot: ControlStateSnapshot) -> ReactiveGuardDecision:
        """snapshot だけを読み、同じ tick に紐づく一時 floor と理由を返す。

        同じ snapshot の再評価は副作用なしで同じ decision を返す。異なる snapshot は
        tick id と単調時計がともに前進しなければ拒否し、hold の時間を巻き戻さない。
        """
        if self._last_tick_id == snapshot.tick_id:
            if self._last_snapshot != snapshot:
                raise ValueError("同じ tick_id に異なる snapshot を渡せない")
            assert self._last_decision is not None
            return self._last_decision
        if self._last_tick_id is not None and snapshot.tick_id < self._last_tick_id:
            raise ValueError("Reactive Guard の tick_id を巻き戻せない")
        if self._last_monotonic_ms is not None and snapshot.monotonic_ms <= self._last_monotonic_ms:
            raise ValueError("Reactive Guard の単調時計は tick ごとに前進させる")

        profile = (
            GuardThresholdProfile.CONSERVATIVE
            if snapshot.telemetry_health is TelemetryHealth.DEGRADED
            else GuardThresholdProfile.NORMAL
        )
        active_by_zone: dict[Zone, list[str]] = {zone: [] for zone in Zone}
        release_causes_by_zone: dict[Zone, list[str]] = {zone: [] for zone in Zone}
        evidence: list[GuardEvidence] = []
        unavailable: set[str] = set()

        for trigger in self._triggers:
            value, is_unavailable = self._read_value(snapshot, trigger)
            was_active = self._trigger_active[trigger.code]
            if is_unavailable:
                unavailable.add(trigger.metric)
                self._trigger_active[trigger.code] = False
                if was_active:
                    for zone in trigger.zones:
                        release_causes_by_zone[zone].append("input_unavailable")
                continue
            band = self._band(trigger)
            activate, clear = self._thresholds(band, profile)
            if value is not None:
                active = value > clear if was_active else value > activate
                self._trigger_active[trigger.code] = active
                if was_active and not active:
                    for zone in trigger.zones:
                        release_causes_by_zone[zone].append("threshold_clear")
                evidence.append(
                    GuardEvidence(
                        code=trigger.code,
                        metric=trigger.metric,
                        source=trigger.source,
                        value=value,
                        activate_above=activate,
                        clear_at_or_below=clear,
                        active=active,
                    )
                )
            if self._trigger_active[trigger.code]:
                for zone in trigger.zones:
                    active_by_zone[zone].append(trigger.code)

        outputs: dict[Zone, GuardZoneOutput] = {}
        events: list[GuardEvent] = []
        for zone in Zone:
            trigger_codes = tuple(sorted(active_by_zone[zone]))
            output, zone_events = self._zone_output(
                zone,
                trigger_codes,
                tuple(sorted(set(release_causes_by_zone[zone]))),
                snapshot.monotonic_ms,
            )
            outputs[zone] = output
            events.extend(zone_events)

        decision = ReactiveGuardDecision(
            snapshot_schema_version=snapshot.schema_version,
            tick_id=snapshot.tick_id,
            ts_ms=snapshot.ts_ms,
            monotonic_ms=snapshot.monotonic_ms,
            threshold_profile=profile,
            zones=PerZone(
                front=outputs[Zone.FRONT],
                rear=outputs[Zone.REAR],
                top=outputs[Zone.TOP],
            ),
            evidence=tuple(evidence),
            unavailable_inputs=tuple(sorted(unavailable)),
            events=tuple(events),
        )
        self._last_tick_id = snapshot.tick_id
        self._last_monotonic_ms = snapshot.monotonic_ms
        self._last_snapshot = snapshot
        self._last_decision = decision
        return decision

    def _band(self, trigger: _TriggerSpec) -> GuardThresholdBand:
        bands: dict[str, GuardThresholdBand] = {
            "cpu_temperature_rate_c_per_s": self._config.cpu_temperature_rate_c_per_s,
            "gpu_temperature_rate_c_per_s": self._config.gpu_temperature_rate_c_per_s,
            "cpu_power_rate_w_per_s": self._config.cpu_power_rate_w_per_s,
            "gpu_power_rate_w_per_s": self._config.gpu_power_rate_w_per_s,
            "intake_rise_c": self._config.intake_rise_c,
            "gpu_hotspot_c": self._config.gpu_hotspot_c,
        }
        return bands[trigger.config_field]

    @staticmethod
    def _thresholds(
        band: GuardThresholdBand,
        profile: GuardThresholdProfile,
    ) -> tuple[float, float]:
        if profile is GuardThresholdProfile.CONSERVATIVE:
            return (
                band.degraded_activate_above.value,
                band.degraded_clear_at_or_below.value,
            )
        return band.activate_above.value, band.clear_at_or_below.value

    @staticmethod
    def _read_value(
        snapshot: ControlStateSnapshot,
        trigger: _TriggerSpec,
    ) -> tuple[float | None, bool]:
        if trigger.source is GuardEvidenceSource.TREND:
            signal = snapshot.signals_by_metric.get(trigger.metric)
            if signal is None or not signal.available:
                return None, True
            trend = next((item for item in snapshot.trends if item.metric == trigger.metric), None)
            # Fresh signal without a new source sample is not missing. Keep the hysteresis latch
            # until a new interval-normalized trend arrives or the signal becomes stale.
            return (None, False) if trend is None else (trend.per_second, False)
        if trigger.source is GuardEvidenceSource.DERIVED:
            value = snapshot.derived_by_metric.get(trigger.metric)
            return value, value is None
        signal = snapshot.signals_by_metric.get(trigger.metric)
        if signal is None or not signal.available:
            return None, True
        assert signal.value is not None
        return signal.value, False

    def _zone_output(
        self,
        zone: Zone,
        trigger_codes: tuple[str, ...],
        release_causes: tuple[str, ...],
        now_mono_ms: int,
    ) -> tuple[GuardZoneOutput, tuple[GuardEvent, ...]]:
        runtime = self._zones[zone]
        was_intervening = runtime.intervening
        events: list[GuardEvent] = []
        if release_causes:
            runtime.release_causes = tuple(
                sorted(set(runtime.release_causes).union(release_causes))
            )

        if trigger_codes:
            runtime.hold_until_mono_ms = now_mono_ms + self._config.hold_ms.value
            runtime.origin_trigger_codes = tuple(
                sorted(set(runtime.origin_trigger_codes).union(trigger_codes))
            )
            reason = Reason(
                code="reactive_guard_triggered",
                detail=f"zone={zone.value}; triggers={','.join(trigger_codes)}",
            )
            output = GuardZoneOutput(
                floor=self._config.floor.value,
                hold_until_mono_ms=runtime.hold_until_mono_ms,
                reason=reason,
            )
        elif now_mono_ms < runtime.hold_until_mono_ms:
            reason = Reason(
                code="reactive_guard_hold",
                detail=(
                    f"zone={zone.value}; until_mono_ms={runtime.hold_until_mono_ms}; "
                    f"triggers={','.join(runtime.origin_trigger_codes)}; "
                    f"release_causes={','.join(runtime.release_causes)}"
                ),
            )
            output = GuardZoneOutput(
                floor=self._config.floor.value,
                hold_until_mono_ms=runtime.hold_until_mono_ms,
                reason=reason,
            )
        else:
            output = GuardZoneOutput()

        runtime.intervening = output.floor is not None or output.ceiling is not None
        if runtime.intervening and not was_intervening:
            assert output.reason is not None
            events.append(
                GuardEvent(
                    zone=zone,
                    transition=GuardTransition.STARTED,
                    reason=output.reason,
                    trigger_codes=runtime.origin_trigger_codes,
                )
            )
        elif was_intervening and not runtime.intervening:
            events.append(
                GuardEvent(
                    zone=zone,
                    transition=GuardTransition.RELEASED,
                    reason=Reason(
                        code="reactive_guard_released",
                        detail=(
                            f"zone={zone.value}; "
                            f"causes={','.join(runtime.release_causes)}; "
                            f"hold_expired_mono_ms={runtime.hold_until_mono_ms}"
                        ),
                    ),
                    trigger_codes=runtime.origin_trigger_codes,
                )
            )
            runtime.origin_trigger_codes = ()
            runtime.release_causes = ()
        return output, tuple(events)
