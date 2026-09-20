"""3系統 Fan 制御デーモン（`coldaisle-fand`）の合成と常駐ループ。#74

決定記録 0028 §2.2 のとおり、**hwmon の PWM へ書き込むのはこのプロセスの Hardware Backend
だけ**である。M8 の時点で実機へ到達する backend は無く（#77）、ここが組み立てるのは
`SimulatedFanBackend` だけ。実機の backend は #57 の権限・引き継ぎと #75 の実測が揃ってから
同じ `FanHardwareBackend` として差し込む。

起動時の設定の扱いは 0028 §2.7 に従う。

| 状態 | 動作 |
|---|---|
| `fan-hardware.yaml` が不正 | **制御を取らない。** BIOS の制御のまま 0 以外で終了する |
| hardware は正しいが承認前（`provisional`） | 制御を取らない（0028 §2.9 の承認点 3） |
| hardware は正しく safety / policy が不正 | 制御を取り、**全 zone を Max**（`config_invalid`） |
| すべて正しい | `STARTUP` の Max から通常の control loop へ入る |

**動作中に設定を読み直さない**（0028 §2.7）。反映は再起動で行い、再起動は必ず
`STARTUP` の Max を通る。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

from coldaisle import logs
from coldaisle.clock import Clock, MonotonicClock, SystemMonotonicClock, WallClock
from coldaisle.control.config import (
    CONFIG_FILENAMES,
    ControlConfig,
    FanHardwareConfig,
    load_fan_hardware_document,
)
from coldaisle.control.fallback.controller import FallbackController
from coldaisle.control.fallback.gate import ControllerGate
from coldaisle.control.hardware.simulated import FanHardwareBackend, SimulatedFanBackend
from coldaisle.control.logging import ControlTraceLogger
from coldaisle.control.loop import (
    ControlLoop,
    ControlTickResult,
    StaticAuthority,
    StaticOperatingMode,
    TelemetrySample,
    Watchdog,
    build_input_contract,
)
from coldaisle.control.reactive.guard import ReactiveGuard
from coldaisle.control.safety.critical import (
    ControlRuntimeBinding,
    CriticalSafety,
    DemandComposer,
    create_control_runtime_binding,
    create_emergency_control_runtime,
)
from coldaisle.control.schema import Zone
from coldaisle.control.shadow.record import ShadowRecorder
from coldaisle.control.state import ControlStateEstimator
from coldaisle.control.supervisor.policy import SupervisorCoordinator
from coldaisle.control.supervisor.regime import WorkloadRegimeEstimator
from coldaisle.metrics import MetricCatalog
from coldaisle.store import QualityRules, SqliteStore

LOGGER = logging.getLogger("coldaisle.control")

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_DB = Path("var/coldaisle.db")
DEFAULT_METRICS = Path("config/metrics.yaml")
DEFAULT_QUALITY_RULES = Path("config/quality.yaml")

UNCONFIGURED_MODEL_VERSION = "unconfigured"
"""Learned MPC の worker を配線していない起動で Gate に渡す期待版。

**値そのものに意味は無い。** worker が無い構成では提案が1件も来ないので Gate は必ず
Fallback を選ぶ。worker を配線するときは、Registry の production 版を明示的に渡す。
"""

EXIT_HARDWARE_CONFIG_INVALID = 2
"""`fan-hardware.yaml` が不正で、制御を取らずに終了した（0028 §2.7）。"""

EXIT_ACTUATION_NOT_APPROVED = 3
"""hardware mapping が `provisional` で、実機の制御を取る承認が無い（0028 §2.9）。"""

EXIT_WATCHDOG_UNAVAILABLE = 4
"""`--require-watchdog` を指定したのに deadman へ通知できない（0028 §2.6）。"""

NOTIFY_SOCKET_ENV = "NOTIFY_SOCKET"
"""systemd が `Type=notify` のサービスへ渡す通知先。**この名前は ABI で、調整値ではない。**"""

WATCHDOG_DATAGRAM = b"WATCHDOG=1"
READY_DATAGRAM = b"READY=1"
"""sd_notify の ABI。値を組み立てる余地を残さない。"""


class WatchdogUnavailableError(RuntimeError):
    """外部の deadman へ通知できないのに、通知を必須にして起動しようとした。"""


class SystemdWatchdog:
    """`NOTIFY_SOCKET` へ `WATCHDOG=1` を送る deadman（0028 §2.6）。

    **書けるのはこの2つの固定 datagram だけ**で、値を引数から組み立てない。
    送信に失敗したら例外にする。握りつぶすと、heartbeat が届いていないのに
    「通知した」ことになり、deadman が無いのと同じになる。
    """

    __slots__ = ("_address", "_socket")

    def __init__(self, address: str) -> None:
        if not address:
            raise WatchdogUnavailableError(f"{NOTIFY_SOCKET_ENV} が空")
        # systemd の abstract namespace（先頭が `@`）を AF_UNIX の表現へ直す。
        self._address = "\0" + address[1:] if address.startswith("@") else address
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)

    def ready(self) -> None:
        """起動を伝える（`Type=notify` の service が待っている）。"""
        self._send(READY_DATAGRAM)

    def notify(self) -> None:
        """1 tick を書き終えたことを伝える。"""
        self._send(WATCHDOG_DATAGRAM)

    def close(self) -> None:
        """socket を閉じる。"""
        self._socket.close()

    def _send(self, payload: bytes) -> None:
        self._socket.sendto(payload, self._address)


class UnsupervisedWatchdog:
    """外部の deadman が無い環境（手元実行・試験）の代わり。**黙って何もしない訳ではない。**

    `NullWatchdog` をそのまま本番に置くと、hang しても誰も気づかない。この実装は
    起動時に「deadman が無い」ことを error として残し、heartbeat の間隔が
    `watchdog_timeout_ms` を超えたらそのつど記録する。**プロセスを殺す力は無い**ので、
    本番の service では `--require-watchdog` で `SystemdWatchdog` を必須にする。
    """

    __slots__ = ("_last_mono_ms", "_monotonic", "_timeout_ms")

    def __init__(self, *, timeout_ms: int, monotonic: MonotonicClock) -> None:
        self._timeout_ms = timeout_ms
        self._monotonic = monotonic
        self._last_mono_ms: int | None = None
        LOGGER.error(
            "外部の deadman が無い状態で起動する（hang しても停止させられない）",
            extra={
                logs.FIELDS_KEY: {
                    "notify_socket": None,
                    "watchdog_timeout_ms": timeout_ms,
                    "hint": "systemd の Type=notify と WatchdogSec、または --require-watchdog",
                }
            },
        )

    def notify(self) -> None:
        """heartbeat の間隔だけを見る。**遅れを黙って捨てない。**"""
        now_ms = self._monotonic.monotonic_ms()
        previous = self._last_mono_ms
        self._last_mono_ms = now_ms
        if previous is not None and now_ms - previous > self._timeout_ms:
            LOGGER.error(
                "control tick の heartbeat が deadman の時間切れを超えた",
                extra={
                    logs.FIELDS_KEY: {
                        "gap_ms": now_ms - previous,
                        "watchdog_timeout_ms": self._timeout_ms,
                    }
                },
            )


def create_watchdog(
    *,
    timeout_ms: int,
    monotonic: MonotonicClock,
    require: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Watchdog:
    """環境に応じた deadman を作る。**既定を無音の no-op にしない。**"""
    address = (os.environ if environ is None else environ).get(NOTIFY_SOCKET_ENV, "")
    if address:
        watchdog = SystemdWatchdog(address)
        watchdog.ready()
        LOGGER.info(
            "systemd の deadman へ heartbeat を送る",
            extra={logs.FIELDS_KEY: {"watchdog_timeout_ms": timeout_ms}},
        )
        return watchdog
    if require:
        raise WatchdogUnavailableError(
            f"{NOTIFY_SOCKET_ENV} が無いのに --require-watchdog が指定されている"
        )
    return UnsupervisedWatchdog(timeout_ms=timeout_ms, monotonic=monotonic)


@dataclass(frozen=True, slots=True)
class Config:
    """CLI から組み立てるファイル位置と配線の選択。"""

    config_dir: Path = DEFAULT_CONFIG_DIR
    db: Path = DEFAULT_DB
    metrics: Path = DEFAULT_METRICS
    quality_rules: Path = DEFAULT_QUALITY_RULES
    t_sensor_metric: str | None = None
    record_trace: bool = True
    require_watchdog: bool = False
    """外部の deadman へ通知できないときに起動を拒むか（本番の service では真にする）。"""


@dataclass(slots=True)
class ControlStats:
    """1回の実行で起きたこと。"""

    ticks: int = 0
    overruns: int = 0
    skipped_slots: int = 0
    """処理が周期を超えて飛ばした control tick の枠。追いつくために連続実行しない。"""
    recorded: int = 0
    emergency_max: bool = False

    def as_fields(self) -> dict[str, int | bool]:
        """構造化ログへそのまま載せる形。"""
        return {
            "ticks": self.ticks,
            "overruns": self.overruns,
            "skipped_slots": self.skipped_slots,
            "recorded": self.recorded,
            "emergency_max": self.emergency_max,
        }


class StoreTelemetrySource:
    """読み取り専用のストアから、制御が読む metric の最新値を取り出す（決定記録 0060 §2.2）。

    **シリアルも NVML も触らない**（AGENTS.md ルール6 / 決定記録 0001 D-07）。取り込み
    デーモン（`air.*`）と Telemetry Collector（#65）が同じ SQLite へ書いた行を読むだけである。

    ストアは `quality.yaml` のしきい値で古い行を `stale` に落とすが、制御はそのうえで
    **自分の許容遅延**（`safety.yaml`）を重ねる。2つの判定は重なるほど保守側にしか動かない
    ので、制御が甘い側へ倒れることはない。
    """

    __slots__ = ("_metrics", "_store")

    def __init__(self, store: SqliteStore, metrics: frozenset[str]) -> None:
        self._store = store
        self._metrics = metrics

    def read(self) -> tuple[TelemetrySample, ...]:
        """契約にある metric の最新値だけを返す。**待たない。**"""
        latest = self._store.latest()
        return tuple(
            TelemetrySample(
                metric=metric,
                value=reading.value,
                quality=reading.quality,
                source_ts_ms=reading.ts_ms,
            )
            for metric, reading in sorted(latest.items())
            if metric in self._metrics
        )


BackendFactory = Callable[[FanHardwareConfig, ControlRuntimeBinding], FanHardwareBackend]


def simulated_backend(
    config: FanHardwareConfig, binding: ControlRuntimeBinding
) -> FanHardwareBackend:
    """M8 の唯一の backend。実機の backend は #57 / #75 のあとで同じ形で差し込む。"""
    return SimulatedFanBackend(config=config, runtime_binding=binding)


@dataclass(slots=True)
class ControlDaemon:
    """control tick を単調時計の締め切りで刻む常駐ループ。

    **遅れた tick を後から取り戻して連続実行しない**（0028 §2.6）。連続実行すると、
    遅れているときほど1 tick あたりの入力の新しさが揃わなくなる。
    """

    loop: ControlLoop
    monotonic: MonotonicClock
    store: SqliteStore | None = None
    sleep: Callable[[float], None] = time.sleep
    stats: ControlStats = field(default_factory=ControlStats)
    _stop: bool = field(default=False, init=False, repr=False)

    def request_stop(self) -> None:
        """次の tick の前に止める。**tick の途中では止めない。**"""
        self._stop = True

    def run(self, *, max_ticks: int | None = None) -> ControlStats:
        """止めるまで tick を回す。`max_ticks` は試験と Replay のための上限。"""
        period_ms = self.loop.tick_period_ms
        deadline_ms = self.monotonic.monotonic_ms()
        while not self._stop and (max_ticks is None or self.stats.ticks < max_ticks):
            now_ms = self.monotonic.monotonic_ms()
            if now_ms < deadline_ms:
                self.sleep((deadline_ms - now_ms) / 1_000)
                continue
            self._record(self.loop.tick())
            deadline_ms += period_ms
            after_ms = self.monotonic.monotonic_ms()
            while deadline_ms <= after_ms:
                deadline_ms += period_ms
                self.stats.skipped_slots += 1
        return self.stats

    def _record(self, result: ControlTickResult) -> None:
        self.stats.ticks += 1
        if result.deadline_exceeded:
            self.stats.overruns += 1
        if result.recorded:
            self.stats.recorded += 1
        state = result.tick.state
        LOGGER.info(
            "control tick",
            extra={
                logs.FIELDS_KEY: {
                    "tick_id": result.tick.tick_id,
                    "ts_ms": result.tick.ts_ms,
                    "duration_ms": result.duration_ms,
                    "deadline_exceeded": result.deadline_exceeded,
                    "operating_mode": state.operating_mode.value,
                    "safety_state": state.safety_state.value,
                    "authority_stage": state.authority_stage.value,
                    "active_controller": (
                        None if state.active_controller is None else state.active_controller.value
                    ),
                    "faults": [fault.code.value for fault in result.tick.faults],
                    "effective": {
                        zone.value: result.tick.zones.get(zone).demand.effective for zone in Zone
                    },
                }
            },
        )


def build(
    config: Config,
    *,
    backend_factory: BackendFactory = simulated_backend,
    watchdog: Watchdog | None = None,
) -> ControlDaemon:
    """検証済み設定から control loop 一式を組み立てる。

    **1つでも検証に失敗したら組み立てない。** 途中まで配線した状態で走らせると、どの層が
    設定を持っていないのかが運転中にしか分からなくなる。
    """
    control = ControlConfig.from_directory(config.config_dir)
    if not control.actuation_permitted:
        raise ActuationNotApprovedError(
            "fan-hardware.yaml の approval が confirmed ではないため制御を取らない"
        )
    catalog = MetricCatalog.from_yaml(config.metrics)
    contract = build_input_contract(control, catalog, t_sensor_metric=config.t_sensor_metric)
    clock: Clock = WallClock()
    monotonic: MonotonicClock = SystemMonotonicClock()
    binding = create_control_runtime_binding(control)
    store = SqliteStore(
        config.db,
        rules=QualityRules.from_yaml(config.quality_rules),
        clock=clock,
    )
    authority = StaticAuthority()
    # **deadman を必ず配線する。** ここを省くと hang しても heartbeat の欠落が起きず、
    # `watchdog_timeout_ms` が一度も効かない（0028 §2.6 / 決定記録 0060 §2.7）。
    deadman = (
        watchdog
        if watchdog is not None
        else create_watchdog(
            timeout_ms=control.safety.watchdog_timeout_ms.value,
            monotonic=monotonic,
            require=config.require_watchdog,
        )
    )
    loop = ControlLoop(
        config=control,
        estimator=ControlStateEstimator(contract, catalog),
        fallback=FallbackController(control.policy, catalog),
        gate=ControllerGate(
            control.policy,
            expected_model_version=UNCONFIGURED_MODEL_VERSION,
            authority=authority,
        ),
        guard=ReactiveGuard(control.policy.reactive_guard, catalog),
        safety=CriticalSafety(
            control.safety,
            input_contract=contract,
            runtime_binding=binding,
            approved_t_sensor_metric=config.t_sensor_metric,
            metric_catalog=catalog if config.t_sensor_metric is not None else None,
        ),
        composer=DemandComposer(control.safety),
        backend=backend_factory(control.fan_hardware, binding),
        telemetry=StoreTelemetrySource(store, frozenset(spec.metric for spec in contract.signals)),
        clock=clock,
        monotonic=monotonic,
        mode_source=StaticOperatingMode(),
        supervisor=SupervisorCoordinator(control.policy.supervisor, clock),
        regime=WorkloadRegimeEstimator(control.policy.workload_regime, catalog, clock),
        shadow=ShadowRecorder(control.policy.shadow),
        trace=ControlTraceLogger(store) if config.record_trace else None,
        authority=authority,
        watchdog=deadman,
    )
    _log_configuration(control)
    return ControlDaemon(loop=loop, monotonic=monotonic, store=store)


class ActuationNotApprovedError(RuntimeError):
    """実機の制御を取る承認（0028 §2.9 の承認点 3）がまだ無い。"""


def run_config_invalid_max(
    config: Config,
    *,
    monotonic: MonotonicClock,
    backend_factory: BackendFactory = simulated_backend,
) -> ControlStats:
    """`safety.yaml` / `fan-policy.yaml` が不正なときの、全 zone Max だけの経路（0028 §2.7）。

    **不正な閾値を読まない。** 確認済みの hardware mapping だけを使い、`config_invalid` の
    `EMERGENCY` を1回書く。以後は正しい設定で再起動するまで解除しない。
    """
    runtime = create_emergency_control_runtime(config.config_dir / CONFIG_FILENAMES["fan_hardware"])
    if runtime.fan_hardware.approval.status != "confirmed":
        raise ActuationNotApprovedError(
            "fan-hardware.yaml の approval が confirmed ではないため制御を取らない"
        )
    backend = backend_factory(runtime.fan_hardware, runtime.binding)
    composer = DemandComposer.for_invalid_config(runtime.binding)
    backend.apply(composer.compose_invalid_config(tick_id=0, monotonic_ms=monotonic.monotonic_ms()))
    LOGGER.error(
        "設定が不正なため全 zone を Max に固定した（正しい設定で再起動するまで解除しない）",
        extra={logs.FIELDS_KEY: {"reason": "config_invalid"}},
    )
    return ControlStats(ticks=1, emergency_max=True)


def _log_configuration(control: ControlConfig) -> None:
    """起動時に暫定値の位置を出す（0028 §2.8）。**値そのものは出さない。**"""
    provisional = control.provisional_values()
    LOGGER.info(
        "制御設定を読み込んだ",
        extra={
            logs.FIELDS_KEY: {
                **control.trace_metadata(),
                "tick_ms": control.safety.tick_ms.value,
                "tick_deadline_ms": control.safety.tick_deadline_ms.value,
                "authority_stage_ceiling": control.policy.authority_stage.value,
                "provisional_values": [f"{item.source}:{item.path}" for item in provisional],
            }
        },
    )


def build_parser() -> argparse.ArgumentParser:
    """CLI を組み立てる。**閾値は受け取らない**（設定ファイルが持つ）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-fand",
        description="3系統 Fan 制御デーモン（Supervisor + MPC + Guard + Critical Safety）",
    )
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--quality-rules", type=Path, default=DEFAULT_QUALITY_RULES)
    parser.add_argument(
        "--t-sensor-metric",
        default=None,
        help="承認済みの T_SENSOR metric（safety.yaml で有効にしたときだけ指定する）",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="この tick 数で終了する（試験・Replay 用）",
    )
    parser.add_argument("--no-trace", action="store_true", help="decision trace を保存しない")
    parser.add_argument(
        "--require-watchdog",
        action="store_true",
        help=f"{NOTIFY_SOCKET_ENV} が無ければ起動しない（systemd の Type=notify で使う）",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-fand` の入口。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    config = Config(
        config_dir=args.config_dir,
        db=args.db,
        metrics=args.metrics,
        quality_rules=args.quality_rules,
        t_sensor_metric=args.t_sensor_metric,
        record_trace=not args.no_trace,
        require_watchdog=args.require_watchdog,
    )
    monotonic: MonotonicClock = SystemMonotonicClock()

    try:
        load_fan_hardware_document(config.config_dir / CONFIG_FILENAMES["fan_hardware"])
    except Exception:
        # 対象の header を特定できない設定で Max を書くと、対象外の header へ書き込む。
        LOGGER.exception("fan-hardware.yaml が不正なため制御を取らない（BIOS の制御のまま）")
        return EXIT_HARDWARE_CONFIG_INVALID

    try:
        daemon = build(config)
    except ActuationNotApprovedError:
        LOGGER.exception("実機の制御を取る承認が無いため起動しない（決定記録 0028 §2.9）")
        return EXIT_ACTUATION_NOT_APPROVED
    except WatchdogUnavailableError:
        LOGGER.exception("外部の deadman へ通知できないため起動しない（決定記録 0028 §2.6）")
        return EXIT_WATCHDOG_UNAVAILABLE
    except Exception:
        LOGGER.exception("safety.yaml / fan-policy.yaml が不正なため全 zone を Max にする")
        try:
            stats = run_config_invalid_max(config, monotonic=monotonic)
        except ActuationNotApprovedError:
            LOGGER.exception("実機の制御を取る承認が無いため Max も書かない")
            return EXIT_ACTUATION_NOT_APPROVED
        LOGGER.error("config_invalid で停止", extra={logs.FIELDS_KEY: stats.as_fields()})
        return 1

    def _stop(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("シグナルを受けた", extra={logs.FIELDS_KEY: {"signal": signum}})
        daemon.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        stats = daemon.run(max_ticks=args.max_ticks)
    finally:
        if daemon.store is not None:
            daemon.store.close()
    LOGGER.info("control daemon を終了する", extra={logs.FIELDS_KEY: stats.as_fields()})
    return 0


if __name__ == "__main__":  # pragma: no cover - `python -m coldaisle.control_daemon`
    raise SystemExit(main())
