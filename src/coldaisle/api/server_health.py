"""Workspace 向け Server Health payload の決定論的な組み立て。#66

signal、source health、Compute Mode advisory は測定値・品質・アラートだけから決める。
AI は summary の文面だけを任意に差し替えられ、停止や例外で監視結果を変えない。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Literal, Protocol

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

SENSOR_METRICS = (
    "air.room",
    "air.room_humidity",
    "air.front_intake",
    "air.gpu_intake",
    "air.gpu_exhaust",
    "air.top_exhaust",
    "air.rear_exhaust",
)
GPU_METRICS = (
    "gpu.0.core",
    "gpu.0.hotspot",
    "gpu.0.mem",
    "gpu.0.utilization",
    "gpu.0.vram_used",
    "power.gpu.0",
    "sys.cuda_processes",
)
ENVIRONMENT_METRICS = (
    *SENSOR_METRICS,
    "cpu.package",
    "power.cpu.package",
    "cpu.vrm",
    "board.chipset",
    "board.connector_12v2x6",
)
_NVML_REQUIRED = ("gpu.0.core", "power.gpu.0")
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


class HealthSummarizer(Protocol):
    """L3 の AI 要約器を L2 へ注入する最小境界。"""

    def summarize(self, facts: str) -> str | None:
        """検証済み facts の言い換えを返す。利用不能なら ``None``。"""


def build_server_health(
    store: SqliteStore,
    catalog: MetricCatalog,
    summarizer: HealthSummarizer | None = None,
    *,
    hwmon_metrics: tuple[str, ...],
) -> ServerHealthResponse:
    """DB の同じ current view から REST / WS 共通 payload を作る。"""
    readings = store.latest()
    alerts = list(store.alerts(state="firing", limit=100))
    sources = _monitoring_sources(store, readings, hwmon_metrics)
    signal = _signal(sources, readings, alerts)
    gpu = ServerGpuHealth(
        mode=store.current_state("sys.gpu_mode") or "unknown",
        metrics=_metrics(GPU_METRICS, readings, catalog),
    )
    environment = ServerEnvironmentHealth(metrics=_metrics(ENVIRONMENT_METRICS, readings, catalog))
    advisory = _compute_mode_advisory(signal, sources, alerts)

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
    hwmon_metrics: tuple[str, ...],
) -> HealthSources:
    ingest_source = store.current_state("sys.ingest_source")
    sensor_unit = _source_from_metrics(
        HealthSourceStatus.OK if ingest_source else None,
        SENSOR_METRICS,
        readings,
        detail=f"ingest_source={ingest_source}" if ingest_source else "ingest source not observed",
        metric_role="required sensor",
    )
    nvml_state = _source_status(store.current_state(SOURCE_STATE_PREFIX + "nvml"))
    nvml = _source_from_metrics(
        nvml_state,
        _NVML_REQUIRED,
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
    else:
        available = sum(
            metric in readings and readings[metric].quality is Quality.OK for metric in required
        )
        partial_required = require_all and available != len(required)
        if available == 0:
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
    alerts: list[AlertRecord],
) -> ServerSignal:
    monitoring = (sources.sensor_unit, sources.nvml, sources.lm_sensors)
    if any(source.status in _BAD_SOURCE_STATES for source in monitoring):
        return ServerSignal.RED
    if any(alert.severity is AlertSeverity.CRITICAL for alert in alerts):
        return ServerSignal.RED
    if any(source.status is HealthSourceStatus.DEGRADED for source in monitoring):
        return ServerSignal.YELLOW
    periodic = (reading for metric, reading in readings.items() if metric not in EVENT_METRICS)
    if alerts or any(reading.quality is not Quality.OK for reading in periodic):
        return ServerSignal.YELLOW
    return ServerSignal.GREEN


def _compute_mode_advisory(
    signal: ServerSignal,
    sources: HealthSources,
    alerts: list[AlertRecord],
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
