"""Workspace 向け Server Health payload の決定論的な組み立て。#66

signal、source health、Compute Mode advisory は測定値・品質・アラートだけから決める。
AI は summary の文面だけを任意に差し替えられ、停止や例外で監視結果を変えない。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle.api.models import (
    ComputeModeAdvisory,
    HealthSource,
    HealthSources,
    HealthSourceStatus,
    ServerEnvironmentHealth,
    ServerGpuHealth,
    ServerHealthMetric,
    ServerHealthResponse,
    ServerSignal,
    iso,
)
from coldaisle.channels import EVENT_METRICS
from coldaisle.internal_telemetry import SOURCE_STATE_PREFIX
from coldaisle.metrics import MetricCatalog
from coldaisle.store import AlertSeverity, Quality, SqliteStore
from coldaisle.store.models import AlertRecord, LatestReading

LOGGER = logging.getLogger("coldaisle.api.server_health")

_CURRENT_QUALITIES = frozenset({Quality.OK, Quality.SUSPECT})
"""いま値が届いている quality。stale / missing / 未保存は届いていない。"""
_BAD_SOURCE_STATES = {
    HealthSourceStatus.UNAVAILABLE,
    HealthSourceStatus.DISABLED,
    HealthSourceStatus.STOPPED,
}
_TEMPLATES = {
    ServerSignal.GREEN: "監視対象のTelemetryと情報源は正常です。",
    ServerSignal.YELLOW: "一部のTelemetryまたはアラートに注意が必要です。",
    ServerSignal.RED: "監視に必要なTelemetryを取得できないか、重大なアラートがあります。",
}


class _SettingsModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RequiredMetrics(_SettingsModel):
    """1つの source が監視必須とする metric。"""

    required: tuple[str, ...] = Field(min_length=1)


class HealthSourcesSettings(_SettingsModel):
    sensor_unit: RequiredMetrics
    nvml: RequiredMetrics


class HealthPanels(_SettingsModel):
    gpu: tuple[str, ...]
    environment: tuple[str, ...]


class ServerHealthSettings(_SettingsModel):
    """``config/server-health.yaml``。監視対象 metric の宣言で、閾値は持たない。"""

    version: Literal[1]
    sources: HealthSourcesSettings
    panels: HealthPanels
    missing_tolerated: frozenset[str] = frozenset()
    """機種が公開しない metric。``missing`` だけは signal を下げない。"""
    active_alerts_limit: int = Field(gt=0)
    """``active_alerts`` に載せる件数の上限。signal の判定は上限に関係なく全件で行う。"""

    @classmethod
    def from_yaml(cls, path: Path, *, catalog: MetricCatalog) -> ServerHealthSettings:
        """YAML を厳格に読み、metrics.yaml に無い metric 名を起動前に拒否する。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Server Health 設定が辞書ではない: {path}")
        settings = cls.model_validate(loaded)
        settings.validate_metrics(catalog)
        return settings

    def required_metrics(self) -> frozenset[str]:
        """source ごとの監視必須 metric。パネルは表示用で、ここに含めない。"""
        return frozenset({*self.sources.sensor_unit.required, *self.sources.nvml.required})

    def validate_metrics(self, catalog: MetricCatalog) -> None:
        """誤記した metric は常に missing に見え、黙って監視から外れるため拒否する。"""
        names = {
            *self.sources.sensor_unit.required,
            *self.sources.nvml.required,
            *self.panels.gpu,
            *self.panels.environment,
            *self.missing_tolerated,
        }
        unknown = sorted(name for name in names if name not in catalog.metrics)
        if unknown:
            raise ValueError(f"metrics.yaml に定義の無い metric: {', '.join(unknown)}")
        required = {*self.sources.sensor_unit.required, *self.sources.nvml.required}
        overlap = sorted(required & self.missing_tolerated)
        if overlap:
            # 必須 metric の欠測を許すと、source が止まっても signal が下がらない
            raise ValueError(f"監視必須 metric を missing_tolerated にできない: {overlap}")


class HealthSummarizer(Protocol):
    """L3 の AI 要約器を L2 へ注入する最小境界。"""

    def summarize(self, facts: str) -> str | None:
        """検証済み facts の言い換えを返す。利用不能なら ``None``。"""


def build_server_health(
    store: SqliteStore,
    catalog: MetricCatalog,
    summarizer: HealthSummarizer | None = None,
    *,
    settings: ServerHealthSettings,
    hwmon_metrics: tuple[str, ...],
    nvml_metrics: tuple[str, ...],
) -> ServerHealthResponse:
    """DB の同じ current view から REST / WS 共通 payload を作る。"""
    # payload 全体を DB の1時点から作る。文ごとに読むと、間に resolve や新しい
    # サンプルが入ったとき一覧・件数・値・source 状態が食い違う。AI 要約は
    # スナップショットの外で行い、読み取りトランザクションを長く保持しない
    with store.read_snapshot():
        readings = store.latest()
        # 一覧は新しい順に打ち切るため、重大度は件数上限の無い集計から判定する
        alerts = list(store.alerts(state="firing", limit=settings.active_alerts_limit))
        firing = store.alert_severity_counts(state="firing")
        sources = _monitoring_sources(store, readings, settings, hwmon_metrics)
        gpu_mode = store.current_state("sys.gpu_mode") or "unknown"
    # 無効化・撤去した入力の最後の行は store.latest() に残り続け、やがて stale になる。
    # 監視していない metric で signal を下げないよう、必須 metric と現在有効な入力だけを
    # 見る。パネルは表示専用で、入力が無効なら値が古くても signal に影響させない
    monitored = settings.required_metrics() | frozenset((*hwmon_metrics, *nvml_metrics))
    signal = _signal(sources, readings, firing, settings.missing_tolerated, sorted(monitored))
    gpu = ServerGpuHealth(
        mode=gpu_mode,
        metrics=_metrics(settings.panels.gpu, readings, catalog),
    )
    environment = ServerEnvironmentHealth(
        metrics=_metrics(settings.panels.environment, readings, catalog)
    )
    advisory = _compute_mode_advisory(signal, sources, alerts, firing)

    summary = None
    if summarizer is not None:
        try:
            summary = summarizer.summarize(_TEMPLATES[signal])
        except Exception:
            # AI は任意の文面生成だけを担う。失敗を signal や API availability へ伝播させない。
            LOGGER.warning("Server Health のAI要約に失敗した", exc_info=True)

    if summary is not None and summary.strip():
        summary = summary.strip()
        ai_source = HealthSource(
            status=HealthSourceStatus.OK,
            detail="AI summary generated",
            last_sample_ts_ms=None,
            last_sample_at=None,
        )
        summary_source: Literal["ai", "template"] = "ai"
    else:
        ai_source = HealthSource(
            status=HealthSourceStatus.STOPPED,
            detail="deterministic template used",
            last_sample_ts_ms=None,
            last_sample_at=None,
        )
        summary = _TEMPLATES[signal]
        summary_source = "template"

    now_ms = store.clock.now_ms()
    return ServerHealthResponse(
        generated_at_ms=now_ms,
        generated_at=iso(now_ms),
        signal=signal,
        summary=summary,
        summary_source=summary_source,
        gpu=gpu,
        environment=environment,
        active_alerts=alerts,
        sources=HealthSources(
            sensor_unit=sources.sensor_unit,
            nvml=sources.nvml,
            lm_sensors=sources.lm_sensors,
            ai_layer=ai_source,
        ),
        compute_mode_advisory=advisory,
    )


def _monitoring_sources(
    store: SqliteStore,
    readings: Mapping[str, LatestReading],
    settings: ServerHealthSettings,
    hwmon_metrics: tuple[str, ...],
) -> HealthSources:
    ingest_source = store.current_state("sys.ingest_source")
    sensor_unit = _source_from_metrics(
        HealthSourceStatus.OK if ingest_source else None,
        settings.sources.sensor_unit.required,
        readings,
        detail=f"ingest_source={ingest_source}" if ingest_source else "ingest source not observed",
        metric_role="required sensor",
    )
    nvml_state = _source_status(store.current_state(SOURCE_STATE_PREFIX + "nvml"))
    nvml = _source_from_metrics(
        nvml_state,
        settings.sources.nvml.required,
        readings,
        detail=f"collector_state={nvml_state.value}" if nvml_state else "collector state missing",
        metric_role="required NVML",
    )
    hwmon_state = _source_status(store.current_state(SOURCE_STATE_PREFIX + "hwmon"))
    lm_sensors = _source_from_metrics(
        hwmon_state,
        hwmon_metrics,
        readings,
        detail=f"collector_state={hwmon_state.value}" if hwmon_state else "collector state missing",
        require_all=False,
        metric_role="configured hwmon",
    )
    stopped_ai = HealthSource(
        status=HealthSourceStatus.STOPPED,
        detail="summary has not been attempted",
        last_sample_ts_ms=None,
        last_sample_at=None,
    )
    return HealthSources(
        sensor_unit=sensor_unit,
        nvml=nvml,
        lm_sensors=lm_sensors,
        ai_layer=stopped_ai,
    )


def _source_status(raw: str | None) -> HealthSourceStatus | None:
    if raw is None:
        return None
    try:
        return HealthSourceStatus(raw)
    except ValueError:
        return HealthSourceStatus.UNAVAILABLE


def _source_from_metrics(
    reported: HealthSourceStatus | None,
    required: tuple[str, ...],
    readings: Mapping[str, LatestReading],
    *,
    detail: str,
    require_all: bool = True,
    metric_role: str,
) -> HealthSource:
    present = [readings[metric] for metric in required if metric in readings]
    newest = max((reading.ts_ms for reading in present), default=None)
    if reported is None:
        status = HealthSourceStatus.STOPPED
    elif reported in _BAD_SOURCE_STATES:
        status = reported
    elif not required:
        # 有効な入力が1つも無い source には、取得不能になりうる必須データが無い
        status = reported
    else:
        qualities = [_current_quality(readings, metric) for metric in required]
        # suspect は「値は届いているが疑わしい」。取得できている以上、情報源の停止
        # （unavailable / red）ではなく劣化（degraded / yellow）として扱う
        current = sum(quality in _CURRENT_QUALITIES for quality in qualities)
        partial_required = require_all and any(quality is not Quality.OK for quality in qualities)
        if current == 0:
            status = HealthSourceStatus.UNAVAILABLE
            detail = f"{detail}; no fresh {metric_role} telemetry"
        elif partial_required or reported is HealthSourceStatus.DEGRADED:
            status = HealthSourceStatus.DEGRADED
            reason = (
                f"partial {metric_role} telemetry"
                if partial_required
                else "source reported degraded"
            )
            detail = f"{detail}; {reason}"
        else:
            status = HealthSourceStatus.OK
    return HealthSource(
        status=status,
        detail=detail,
        last_sample_ts_ms=newest,
        last_sample_at=None if newest is None else iso(newest),
    )


def _metrics(
    names: tuple[str, ...],
    readings: Mapping[str, LatestReading],
    catalog: MetricCatalog,
) -> dict[str, ServerHealthMetric]:
    result: dict[str, ServerHealthMetric] = {}
    for name in names:
        reading = readings.get(name)
        result[name] = ServerHealthMetric(
            value=None if reading is None else reading.value,
            unit=catalog.unit_for(name),
            quality=Quality.MISSING if reading is None else reading.quality,
            age_seconds=None if reading is None else round(reading.age_ms / 1000, 3),
        )
    return result


def _signal(
    sources: HealthSources,
    readings: Mapping[str, LatestReading],
    firing: Mapping[AlertSeverity, int],
    missing_tolerated: frozenset[str],
    monitored: list[str],
) -> ServerSignal:
    monitoring = (sources.sensor_unit, sources.nvml, sources.lm_sensors)
    if any(source.status in _BAD_SOURCE_STATES for source in monitoring):
        return ServerSignal.RED
    if firing.get(AlertSeverity.CRITICAL, 0) > 0:
        return ServerSignal.RED
    if any(source.status is HealthSourceStatus.DEGRADED for source in monitoring):
        return ServerSignal.YELLOW
    if sum(firing.values()) > 0 or any(
        _degrades_signal(metric, _current_quality(readings, metric), missing_tolerated)
        for metric in monitored
    ):
        return ServerSignal.YELLOW
    return ServerSignal.GREEN


def _current_quality(readings: Mapping[str, LatestReading], metric: str) -> Quality:
    """一度も保存されていない監視対象は、保存済みの missing と同じに扱う。"""
    reading = readings.get(metric)
    return Quality.MISSING if reading is None else reading.quality


def _degrades_signal(metric: str, quality: Quality, missing_tolerated: frozenset[str]) -> bool:
    if metric in EVENT_METRICS:
        # 発生時だけ記録する metric は鮮度で判定しない（決定記録 0009 §2.12）
        return False
    if metric in missing_tolerated and quality is Quality.MISSING:
        # 機種が公開しない値は collector が毎回 missing で保存する。これで yellow に
        # すると signal が恒常的に下がり、本当の劣化と区別できなくなる。
        # suspect / stale は「値はあるが疑わしい・古い」なので従来どおり下げる
        return False
    return quality is not Quality.OK


def _compute_mode_advisory(
    signal: ServerSignal,
    sources: HealthSources,
    alerts: list[AlertRecord],
    firing: Mapping[AlertSeverity, int],
) -> ComputeModeAdvisory:
    warnings = [
        f"{name} source is {source.status.value}"
        for name, source in (
            ("sensor_unit", sources.sensor_unit),
            ("nvml", sources.nvml),
            ("lm_sensors", sources.lm_sensors),
        )
        if source.status is not HealthSourceStatus.OK
    ]
    warnings.extend(f"active alert: {alert.rule_id} ({alert.severity.value})" for alert in alerts)
    listed = Counter(alert.severity for alert in alerts)
    unlisted = {
        severity: firing.get(severity, 0) - listed.get(severity, 0) for severity in AlertSeverity
    }
    if any(count > 0 for count in unlisted.values()):
        # 一覧から外れた古いアラートも、件数と重大度だけは警告に残す
        detail = ", ".join(
            f"{severity.value}={count}" for severity, count in unlisted.items() if count > 0
        )
        warnings.append(f"more active alerts not listed: {detail}")
    if signal is not ServerSignal.GREEN and not warnings:
        warnings.append("one or more telemetry values are not quality=ok")
    return ComputeModeAdvisory(
        safe=signal is ServerSignal.GREEN,
        warnings=tuple(warnings),
        blocking=False,
    )


def server_health_state(payload: ServerHealthResponse) -> str:
    """WS の変更検出用。時計と連続的な age だけの変化では push しない。"""
    state = payload.model_dump(mode="json")
    state.pop("generated_at_ms")
    state.pop("generated_at")
    for section in (state["gpu"], state["environment"]):
        for metric in section["metrics"].values():
            metric.pop("age_seconds")
    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
