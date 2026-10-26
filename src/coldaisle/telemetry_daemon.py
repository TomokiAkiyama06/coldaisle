"""Internal Telemetry Collector の合成と常駐ループ。#65

外付けセンサーの ingest daemon と同じ SQLite、同じ Unix ms の time base へ保存する。
このプロセスだけが NVML を読み、hwmon は読み取り専用で使用する。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

import yaml

from coldaisle import logs
from coldaisle.clock import Clock, WallClock
from coldaisle.internal_telemetry import (
    SOURCE_STATE_PREFIX,
    TELEMETRY_KIND_KEY,
    CollectionCycle,
    CpuPowerAdapter,
    GpuStateClassifier,
    GpuStateConfig,
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
DEFAULT_GPU_STATE = Path("config/gpu_state.yaml")

_SQLITE_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
"""書き込みのロックを busy_timeout の間に取れなかっただけの失敗（#378 / 決定記録 0170）。"""


@dataclass(frozen=True, slots=True)
class Config:
    """CLI から組み立てるファイル位置。"""

    db: Path = DEFAULT_DB
    telemetry: Path = DEFAULT_CONFIG
    quality_rules: Path = DEFAULT_QUALITY_RULES
    metrics: Path = DEFAULT_METRICS
    gpu_state: Path = DEFAULT_GPU_STATE


@dataclass(slots=True)
class TelemetryStats:
    """1回の daemon 実行で起きたこと。"""

    cycles: int = 0
    readings: int = 0
    duplicates: int = 0
    unavailable_cycles: int = 0
    skipped_slots: int = 0
    """処理が周期を超えて飛ばした収集枠の数。追いつくために連続で収集しない。"""
    locked_cycles: int = 0
    """DB の書き込みロックを取れず、書き込みを途中で破棄した cycle の数（#378）。"""

    def as_fields(self) -> dict[str, int]:
        return {
            "cycles": self.cycles,
            "readings": self.readings,
            "duplicates": self.duplicates,
            "unavailable_cycles": self.unavailable_cycles,
            "skipped_slots": self.skipped_slots,
            "locked_cycles": self.locked_cycles,
        }


def _is_lock_contention(error: sqlite3.OperationalError) -> bool:
    """SQLite の BUSY / LOCKED（拡張コードを含む）か。"""
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) in _SQLITE_BUSY_CODES


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
        self._consecutive_locked = 0
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

    def _write_cycle(self, cycle: CollectionCycle) -> None:
        """1 cycle の sample と状態を Store へ書く。ロック待ちの失敗だけはその cycle を破棄する。

        rollup などが書き込みロックを busy_timeout より長く持つと ``BEGIN IMMEDIATE`` が
        BUSY で失敗する。それで落ちると再起動までの数秒の sample が欠け、fand が入力の
        stale で Max・fault になる（#378）。取り込みの「1サンプルの失敗で落とさない」に
        合わせ、**BUSY / LOCKED のときだけ** その cycle の残りを書かずに次の周期へ進む。
        それ以外の失敗（スキーマ・ディスク・権限など）は上げて落とし、systemd に任せる。
        書けないまま動き続けると、壊れた DB に気付けないため。

        **cycle の途中で失敗したら、それ以降はその cycle では書かない。** 書き込みは
        readings → 各 source の状態の順に、それぞれ別のトランザクションで行う。先に
        コミットできた分は残し、後ろは捨てる。捨てた状態は ``set_system_state`` が
        「直近の値と違えば書く」ので、次の cycle の時刻で書き直される（1 周期遅れるだけ）。
        捨てた readings は書き直さない（その時刻の観測は欠測として残る）。
        """
        attempted = len(cycle.sample.readings)
        written: int | None = None
        try:
            written = self._store.insert_sample(cycle.sample)
            self.stats.readings += written
            self.stats.duplicates += attempted - written
            for source in cycle.sources:
                self._store.set_system_state(
                    SOURCE_STATE_PREFIX + source.source,
                    source.status.value,
                    at_ms=cycle.sample.ts_ms,
                )
                # sys.gpu_state など。値が変わったときだけ行が増える（0002 §2.6）
                for key, value in source.states:
                    self._store.set_system_state(key, value, at_ms=cycle.sample.ts_ms)
        except sqlite3.OperationalError as error:
            if not _is_lock_contention(error):
                raise
            # COMMIT が BUSY で失敗した場合はトランザクションが開いたまま残る。閉じないと
            # 次の BEGIN が「入れ子の transaction」で失敗し、ロックと無関係に落ちる
            if self._store.connection.in_transaction:
                self._store.connection.execute("ROLLBACK")
            self._consecutive_locked += 1
            self.stats.locked_cycles += 1
            # 毎 cycle 出す。ロック待ちで1回に busy_timeout（既定 5 s）かかるので、
            # 出る間隔は周期（2.5 s）より長く、件数は抑えられる
            LOGGER.warning(
                "DB の書き込みロックを取れず、この cycle の書き込みを破棄する",
                extra={
                    logs.FIELDS_KEY: {
                        "ts_ms": cycle.sample.ts_ms,
                        "stage": "readings" if written is None else "system_state",
                        "dropped_readings": attempted if written is None else 0,
                        "consecutive_locked_cycles": self._consecutive_locked,
                        "locked_cycles": self.stats.locked_cycles,
                        "sqlite_errorcode": error.sqlite_errorcode,
                        "error": str(error),
                    }
                },
            )
            return
        if self._consecutive_locked:
            # 破棄が何 cycle 続いたかを1行で残す。warning の数を数えなくて済む
            LOGGER.info(
                "DB への書き込みが回復した",
                extra={logs.FIELDS_KEY: {"consecutive_locked_cycles": self._consecutive_locked}},
            )
            self._consecutive_locked = 0

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
                self.stats.cycles += 1
                if any(source.status is SourceStatus.UNAVAILABLE for source in cycle.sources):
                    self.stats.unavailable_cycles += 1
                self._write_cycle(cycle)
                for source in cycle.sources:
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
        # 時計は expected_metrics には使わない。poll しないので実時計で足りる
        CpuPowerAdapter(config.cpu_power, clock=WallClock()),
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

    - ``adapters`` を渡さない（``None``。CLI からの通常の起動）→ ``hardware``
    - ``adapters`` を渡す → 呼び出し側が ``source_kind`` を**必ず明示する**。
      既定を ``hardware`` にすると、試験の偽 adapter が黙って「実機」を名乗る

    **``None`` と空の tuple を同じに扱わない。** 空で渡されたときに実 adapter へ
    落とすと、``source_kind="mock"`` のまま実機の値を記録してしまう。収集対象が
    無い daemon は書くものが無く、入力を止めたいなら設定側の ``enabled`` を使うので、
    空の ``adapters`` は拒否する。
    """
    if adapters is None:
        if source_kind is not None:
            # 実 adapter を組むのはこの関数自身なので、種類を外から名乗らせない
            raise ValueError("adapters を渡さないときの source_kind は build が決める")
        source_kind = TelemetrySourceKind.HARDWARE
    else:
        if source_kind is None:
            raise ValueError("adapters を渡すときは source_kind を明示する")
        if not adapters:
            raise ValueError("adapters を空で渡さない（無効化は設定の enabled で行う）")
    telemetry = InternalTelemetryConfig.from_yaml(
        config.telemetry, catalog=MetricCatalog.from_yaml(config.metrics)
    )
    _log_configuration(telemetry)
    used_clock = clock or WallClock()
    # `adapters or (...)` にしない。空の tuple が実 adapter へ落ちると、
    # 名乗った種類（mock）と実際に読む先が食い違う
    used_adapters = (
        (
            NvmlAdapter(telemetry.nvml, gpu_state=load_gpu_state_classifier(config.gpu_state)),
            HwmonAdapter(telemetry.hwmon),
            ProcStatAdapter(telemetry.proc_stat),
            # stale の判定を collector と同じ時計で行う（決定記録 0122 §2.3）
            CpuPowerAdapter(telemetry.cpu_power, clock=used_clock),
        )
        if adapters is None
        else adapters
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


def load_gpu_state_classifier(path: Path) -> GpuStateClassifier:
    """``config/gpu_state.yaml`` を読む。読めない・形が不正なら常に ``unknown`` を出す判定器にする。

    **起動を止めない**（決定記録 0163 §2.6）。``sys.gpu_state`` は観測の記録だけで、
    設定の誤りで温度・電力の収集まで止めると #63 の「Compute Mode 中も温度欠測なし」を破る。
    誤りは起動時のログと、``sys.gpu_state = unknown`` が続くことで分かる。
    """
    try:
        config = GpuStateConfig.from_yaml(path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        LOGGER.error(
            "gpu_state の設定を読めない。sys.gpu_state は unknown になる",
            extra={logs.FIELDS_KEY: {"path": str(path), "error": type(error).__name__}},
        )
        return GpuStateClassifier(
            None, config_error=f"gpu_state_config_invalid:{type(error).__name__}"
        )
    LOGGER.info(
        "gpu_state configuration",
        extra={logs.FIELDS_KEY: {"ai_units": list(config.ai_units)}},
    )
    return GpuStateClassifier(config)


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
    LOGGER.info(
        "CPU power input configuration",
        extra={
            logs.FIELDS_KEY: {
                "enabled": config.cpu_power.enabled,
                "metric": config.cpu_power.metric,
                "stale_after_ms": config.cpu_power.stale_after_ms,
                "confirmation_status": config.cpu_power.confirmation.status.value,
                "confirmation_basis": config.cpu_power.confirmation.basis,
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
    parser.add_argument(
        "--gpu-state-config",
        type=Path,
        default=DEFAULT_GPU_STATE,
        help="sys.gpu_state の識別規則（ai_units）。読めなければ sys.gpu_state は unknown",
    )
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
            gpu_state=args.gpu_state_config,
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
