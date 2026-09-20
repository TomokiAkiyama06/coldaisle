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
    SOURCE_STATE_PREFIX,
    TELEMETRY_KIND_KEY,
    HwmonAdapter,
    InternalTelemetryCollector,
    InternalTelemetryConfig,
    NvmlAdapter,
    ProcStatAdapter,
    SourceStatus,
    TelemetryAdapter,
    TelemetrySourceKind,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store import QualityRules, SqliteStore

LOGGER = logging.getLogger("coldaisle.internal_telemetry")

DEFAULT_DB = Path("var/coldaisle.db")
DEFAULT_CONFIG = Path("config/internal-telemetry.yaml")
DEFAULT_QUALITY_RULES = Path("config/quality.yaml")
DEFAULT_METRICS = Path("config/metrics.yaml")


@dataclass(frozen=True, slots=True)
class Config:
    """CLI から組み立てるファイル位置。"""

    db: Path = DEFAULT_DB
    telemetry: Path = DEFAULT_CONFIG
    quality_rules: Path = DEFAULT_QUALITY_RULES
    metrics: Path = DEFAULT_METRICS


@dataclass(slots=True)
class TelemetryStats:
    """1回の daemon 実行で起きたこと。"""

    cycles: int = 0
    readings: int = 0
    duplicates: int = 0
    unavailable_cycles: int = 0
    skipped_slots: int = 0
    """処理が周期を超えて飛ばした収集枠の数。追いつくために連続で収集しない。"""

    def as_fields(self) -> dict[str, int]:
        return {
            "cycles": self.cycles,
            "readings": self.readings,
            "duplicates": self.duplicates,
            "unavailable_cycles": self.unavailable_cycles,
            "skipped_slots": self.skipped_slots,
        }


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class InternalTelemetryDaemon:
    """poll → 同一 timestamp の Sample → Store を繰り返す。"""

    def __init__(
        self,
        *,
        collector: InternalTelemetryCollector,
        store: SqliteStore,
        interval_ms: int,
        source_kind: TelemetrySourceKind,
        sleep: Callable[[float], None] = time.sleep,
        monotonic_ms: Callable[[], int] = _monotonic_ms,
    ) -> None:
        """``monotonic_ms`` は周期の計測だけに使う。

        保存する timestamp は collector の ``Clock``（Unix ms）が決める。周期の計測に
        実時計を使うと、NTP の補正で時刻が跳んだときに待ち時間が狂うため分けている。
        試験では ``SimulatedClock.now_ms`` を渡して処理時間を再現する。
        """
        if interval_ms <= 0:
            raise ValueError("interval_ms は正にする")
        self._collector = collector
        self._store = store
        self._interval_ms = interval_ms
        # 値の出どころの種類。**既定値を持たせない**（決定記録 0049 §2.1）。
        # 既定を hardware にすると、差し込まれた模擬の adapter が黙って「実機」を名乗る
        self._source_kind = source_kind
        self._sleep = sleep
        self._monotonic_ms = monotonic_ms
        self._stop = False
        self.stats = TelemetryStats()

    @property
    def store(self) -> SqliteStore:
        return self._store

    @property
    def source_kind(self) -> TelemetrySourceKind:
        """このデーモンが書く値の出どころの種類（決定記録 0049 §2.2）。"""
        return self._source_kind

    def _next_deadline(self, previous: int) -> int:
        """次の収集予定時刻。処理時間を差し引き、周期が後ろへずれ続けないようにする。

        処理が周期を超えた場合は、過ぎた枠をまとめて飛ばして次の枠へ進める。
        遅れを取り戻すために連続で収集すると、同じ状態を短い間隔で重複記録し、
        NVML / sysfs への負荷も一時的に跳ね上がるため。
        """
        deadline = previous + self._interval_ms
        now = self._monotonic_ms()
        if now <= deadline:
            return deadline
        # 遅れを切り上げた枠数だけ進める。遅れがちょうど周期の倍数なら、進めた先の
        # 枠が現在時刻と一致し、その枠はまだ間に合うので飛ばさない
        skipped = -(-(now - deadline) // self._interval_ms)
        self.stats.skipped_slots += skipped
        LOGGER.warning(
            "Internal Telemetry の収集が周期を超えた",
            extra={
                logs.FIELDS_KEY: {
                    "interval_ms": self._interval_ms,
                    "overrun_ms": now - previous - self._interval_ms,
                    "skipped_slots": skipped,
                }
            },
        )
        return deadline + skipped * self._interval_ms

    def request_stop(self) -> None:
        """現在の poll を保存し終えてから停止する。"""
        self._stop = True

    def run(self, *, max_cycles: int | None = None) -> TelemetryStats:
        """停止要求または ``max_cycles`` まで収集する。"""
        LOGGER.info(
            "Internal Telemetry の収集を開始する",
            extra={
                logs.FIELDS_KEY: {
                    "interval_ms": self._interval_ms,
                    "source_kind": self._source_kind.value,
                }
            },
        )
        # 出どころの種類は**最初の収集の前**に、収集と同じ Clock（Unix ms）で書く。
        # 画面はこれを「いま届いている値」の札に使うので、値より後に書くと
        # 最初の周期だけ種類の分からない値が出る（決定記録 0049 §2.3）
        self._store.set_system_state(
            TELEMETRY_KIND_KEY,
            self._source_kind.value,
            at_ms=self._collector.clock.now_ms(),
        )
        deadline = self._monotonic_ms()
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
                deadline = self._next_deadline(deadline)
                remaining_ms = deadline - self._monotonic_ms()
                if remaining_ms > 0:
                    self._sleep(remaining_ms / 1_000.0)
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
        ProcStatAdapter(config.proc_stat),
    )
    return {
        metric: config.interval_ms for adapter in adapters for metric in adapter.expected_metrics
    }


def build(
    config: Config,
    *,
    clock: Clock | None = None,
    adapters: tuple[TelemetryAdapter, ...] | None = None,
    source_kind: TelemetrySourceKind | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> InternalTelemetryDaemon:
    """設定を読み、実 adapter と既存 Store を1つの clock で束ねる。

    **出どころの種類を決めるのはこの合成の起点**（決定記録 0049 §2.1）。adapter 自身や
    設定ファイルには申告させない。``NvmlAdapter(api=...)`` のように実クラスのまま中身を
    差し替えられるため、クラスでは判別できないため。

    - ``adapters`` を渡さない（CLI からの通常の起動）→ ``hardware``
    - ``adapters`` を渡す → 呼び出し側が ``source_kind`` を**必ず明示する**。
      既定を ``hardware`` にすると、試験の偽 adapter が黙って「実機」を名乗る
    """
    if adapters is None:
        if source_kind is not None:
            # 実 adapter を組むのはこの関数自身なので、種類を外から名乗らせない
            raise ValueError("adapters を渡さないときの source_kind は build が決める")
        source_kind = TelemetrySourceKind.HARDWARE
    elif source_kind is None:
        raise ValueError("adapters を渡すときは source_kind を明示する")
    telemetry = InternalTelemetryConfig.from_yaml(
        config.telemetry, catalog=MetricCatalog.from_yaml(config.metrics)
    )
    _log_configuration(telemetry)
    used_clock = clock or WallClock()
    used_adapters = adapters or (
        NvmlAdapter(telemetry.nvml),
        HwmonAdapter(telemetry.hwmon),
        ProcStatAdapter(telemetry.proc_stat),
    )
    rules = QualityRules.from_yaml(config.quality_rules)
    # 既定の `var/` は追跡されていない。ingest daemon / rollup と同じく、無ければ作る
    config.db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(config.db, rules=rules, clock=used_clock)
    if store.dataset_source_run() is not None:
        # dataset専用DBは1本のReplay取り込みだけの記録。Storeも書き込みを拒否するが、
        # 周期ごとに失敗させるより起動時に止めるほうが原因が分かりやすい（#83）
        store.close()
        raise SystemExit("dataset source runへbind済みのDBにはInternal Telemetryを書かない")
    collector = InternalTelemetryCollector(used_adapters, used_clock)
    return InternalTelemetryDaemon(
        collector=collector,
        store=store,
        interval_ms=telemetry.interval_ms,
        source_kind=source_kind,
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
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
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
            metrics=args.metrics,
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
