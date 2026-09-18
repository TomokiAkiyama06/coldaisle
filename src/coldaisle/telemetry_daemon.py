"""Internal Telemetry Collector の合成と常駐ループ。#65

外付けセンサーの ingest daemon と同じ SQLite、同じ Unix ms の time base へ保存する。
このプロセスだけが NVML を読み、hwmon は読み取り専用で使用する。
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from coldaisle import logs
from coldaisle.clock import Clock, WallClock
from coldaisle.internal_telemetry import (
    HwmonAdapter,
    InternalTelemetryCollector,
    InternalTelemetryConfig,
    NvmlAdapter,
    SourceStatus,
    TelemetryAdapter,
)
from coldaisle.store import QualityRules, SqliteStore

LOGGER = logging.getLogger("coldaisle.internal_telemetry")

DEFAULT_DB = Path("var/coldaisle.db")
DEFAULT_CONFIG = Path("config/internal-telemetry.yaml")
DEFAULT_QUALITY_RULES = Path("config/quality.yaml")
SOURCE_STATE_PREFIX = "sys.telemetry_source."


@dataclass(frozen=True, slots=True)
class Config:
    """CLI から組み立てるファイル位置。"""

    db: Path = DEFAULT_DB
    telemetry: Path = DEFAULT_CONFIG
    quality_rules: Path = DEFAULT_QUALITY_RULES


@dataclass(slots=True)
class TelemetryStats:
    """1回の daemon 実行で起きたこと。"""

    cycles: int = 0
    readings: int = 0
    duplicates: int = 0
    unavailable_cycles: int = 0

    def as_fields(self) -> dict[str, int]:
        return {
            "cycles": self.cycles,
            "readings": self.readings,
            "duplicates": self.duplicates,
            "unavailable_cycles": self.unavailable_cycles,
        }


class InternalTelemetryDaemon:
    """poll → 同一 timestamp の Sample → Store を繰り返す。"""

    def __init__(
        self,
        *,
        collector: InternalTelemetryCollector,
        store: SqliteStore,
        interval_ms: int,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval_ms <= 0:
            raise ValueError("interval_ms は正にする")
        self._collector = collector
        self._store = store
        self._interval_ms = interval_ms
        self._sleep = sleep
        self._stop = False
        self.stats = TelemetryStats()

    @property
    def store(self) -> SqliteStore:
        return self._store

    def request_stop(self) -> None:
        """現在の poll を保存し終えてから停止する。"""
        self._stop = True

    def run(self, *, max_cycles: int | None = None) -> TelemetryStats:
        """停止要求または ``max_cycles`` まで収集する。"""
        LOGGER.info(
            "Internal Telemetry の収集を開始する",
            extra={logs.FIELDS_KEY: {"interval_ms": self._interval_ms}},
        )
        try:
            while not self._stop:
                cycle = self._collector.collect()
                attempted = len(cycle.sample.readings)
                written = self._store.insert_sample(cycle.sample)
                self.stats.cycles += 1
                self.stats.readings += written
                self.stats.duplicates += attempted - written
                if any(source.status is SourceStatus.UNAVAILABLE for source in cycle.sources):
                    self.stats.unavailable_cycles += 1
                for source in cycle.sources:
                    self._store.set_system_state(
                        SOURCE_STATE_PREFIX + source.source,
                        source.status.value,
                        at_ms=cycle.sample.ts_ms,
                    )
                    if source.status in (SourceStatus.DEGRADED, SourceStatus.UNAVAILABLE):
                        LOGGER.warning(
                            "Internal Telemetry source の取得が不完全",
                            extra={
                                logs.FIELDS_KEY: {
                                    "source": source.source,
                                    "status": source.status.value,
                                    "detail": source.detail,
                                }
                            },
                        )
                if max_cycles is not None and self.stats.cycles >= max_cycles:
                    break
                self._sleep(self._interval_ms / 1_000.0)
        finally:
            self._collector.close()
        LOGGER.info(
            "Internal Telemetry の収集を終了する",
            extra={logs.FIELDS_KEY: self.stats.as_fields()},
        )
        return self.stats


def periodic_metric_intervals(config: InternalTelemetryConfig) -> dict[str, int]:
    """有効な入力ごとの収集周期。ロールアップの期待サンプル数に使う。

    daemon が止まった区間を、生データの保持期間を過ぎても欠測として残すため
    （``coldaisle.store.rollup.rollup_minutes``）。対象は build() と同じ adapter の
    ``expected_metrics`` から出し、metric 一覧を二重管理しない。adapter は構築時に
    NVML / sysfs を開かない。
    """
    adapters: tuple[TelemetryAdapter, ...] = (
        NvmlAdapter(config.nvml),
        HwmonAdapter(config.hwmon),
    )
    return {
        metric: config.interval_ms for adapter in adapters for metric in adapter.expected_metrics
    }


def build(
    config: Config,
    *,
    clock: Clock | None = None,
    adapters: tuple[TelemetryAdapter, ...] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> InternalTelemetryDaemon:
    """設定を読み、実 adapter と既存 Store を1つの clock で束ねる。"""
    telemetry = InternalTelemetryConfig.from_yaml(config.telemetry)
    _log_configuration(telemetry)
    used_clock = clock or WallClock()
    used_adapters = adapters or (
        NvmlAdapter(telemetry.nvml),
        HwmonAdapter(telemetry.hwmon),
    )
    rules = QualityRules.from_yaml(config.quality_rules)
    store = SqliteStore(config.db, rules=rules, clock=used_clock)
    collector = InternalTelemetryCollector(used_adapters, used_clock)
    return InternalTelemetryDaemon(
        collector=collector,
        store=store,
        interval_ms=telemetry.interval_ms,
        sleep=sleep,
    )


def _log_configuration(config: InternalTelemetryConfig) -> None:
    """入力の有効状態と実機確認根拠を起動時の監査ログへ残す。"""
    LOGGER.info(
        "NVML input configuration",
        extra={
            logs.FIELDS_KEY: {
                "enabled": config.nvml.enabled,
                "logical_gpu_indices": config.nvml.gpu_indices,
                "identity_contract": "single_physical_gpu",
            }
        },
    )
    for sensor in config.hwmon.sensors:
        if sensor.label is not None:
            selector = "label"
        elif sensor.channel is not None:
            selector = "channel"
        else:
            selector = "none"
        LOGGER.info(
            "hwmon input configuration",
            extra={
                logs.FIELDS_KEY: {
                    "metric": sensor.metric,
                    "enabled": sensor.enabled,
                    "selector": selector,
                    "confirmation_status": (
                        sensor.confirmation.status.value
                        if sensor.confirmation is not None
                        else None
                    ),
                    "confirmation_basis": (
                        sensor.confirmation.basis if sensor.confirmation is not None else None
                    ),
                    "disabled_reason": sensor.disabled_reason,
                }
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NVML / hwmon Internal Telemetry Collector")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--quality-rules", type=Path, default=DEFAULT_QUALITY_RULES)
    parser.add_argument("--once", action="store_true", help="1回収集して終了する")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """``coldaisle-telemetry`` の入口。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    daemon = build(
        Config(
            db=args.db,
            telemetry=args.config,
            quality_rules=args.quality_rules,
        )
    )

    def stop(_signum: int, _frame: FrameType | None) -> None:
        daemon.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        daemon.run(max_cycles=1 if args.once else None)
    finally:
        daemon.store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
