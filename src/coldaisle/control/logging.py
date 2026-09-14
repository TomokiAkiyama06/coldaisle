"""ControlTick を永続化する decision trace logger。#82。

この層は制御判断を JSON に固定するだけで、PWM・hwmon・NVML へは到達しない。
保存先は注入し、テストでは実機や SQLite を必要としない。
"""

from __future__ import annotations

from typing import Protocol

from coldaisle.control.schema import ControlTick


class ControlTraceSink(Protocol):
    """1 tick の不変な判断記録を受け取る保存先。"""

    def record_control_trace(
        self, *, ts_ms: int, tick_id: int, schema_version: int, trace_json: str
    ) -> bool:
        """新規に保存できたときだけ ``True`` を返す。"""
        ...


class ControlTraceLogger:
    """ControlTick をschema version付きの完全なdecision traceとして保存する。"""

    def __init__(self, sink: ControlTraceSink) -> None:
        self._sink = sink

    def record(self, tick: ControlTick) -> bool:
        """1 tick を保存する。重複した trace は保存先の冪等規則に従い無視する。"""
        return self._sink.record_control_trace(
            ts_ms=tick.ts_ms,
            tick_id=tick.tick_id,
            schema_version=tick.schema_version,
            trace_json=tick.model_dump_json(),
        )
