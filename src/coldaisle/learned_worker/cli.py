"""`coldaisle-learnd` の入口（#86 / 決定記録 0077 段階 3、0107 §2.6 / §2.7）。

`--role mpc` だけを受け付ける。`--role supervisor`（RL worker）は段階 4（#89）まで起動を拒む。

- Control Config（4ファイル）と Metric Catalog を起動時に読む。frame の `config` /
  `metric_catalog_sha256` と照合するためで、較正ファイル・Telemetry・SQLite・API は読まない
- registry は `--registry-root` を読み取り専用で読み、frame が固定した artifact だけを検証して使う
- `mpc.period_ms` ごと（worker の単調時計）に最後の frame に対して1回 `propose()` を呼ぶ
- heartbeat は別のスレッドから `heartbeat_interval_ms`（fand の `hello`）ごとに送る
- fand との接続が切れたら終了する（再接続はしない。再起動は unit の仕事。0077 §2.10 段階 6）
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from coldaisle import logs
from coldaisle.clock import MonotonicClock, SystemMonotonicClock
from coldaisle.control.config import ControlConfig
from coldaisle.control.model_registry import ModelRegistry, load_model_registry_limits
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.learned_worker.client import ChannelClosed, WorkerChannel
from coldaisle.learned_worker.mpc import (
    MpcWorkerCore,
    RegistryArtifactSource,
    RegistryProductionCheck,
    WorkerInputs,
)
from coldaisle.metrics import MetricCatalog

LOGGER = logging.getLogger("coldaisle.learned_worker")

EXIT_OK = 0
EXIT_ROLE_NOT_SUPPORTED = 2
"""`--role supervisor`（段階 4 まで無い）。"""
EXIT_CHANNEL_CLOSED = 3
"""fand に接続できない・接続が切れた。"""
EXIT_STARTUP = 5
"""設定・Metric Catalog・registry の設定を読めない。"""

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_METRICS = Path("config/metrics.yaml")
DEFAULT_REGISTRY_LIMITS = Path("config")
"""fand（`coldaisle-fand`）と同じ既定。"""


class MpcWorker:
    """`MpcWorkerCore` をソケットと時計につなぐ。"""

    def __init__(
        self,
        core: MpcWorkerCore,
        channel: WorkerChannel,
        *,
        period_ms: int,
        hello_timeout_ms: int,
        monotonic: MonotonicClock,
    ) -> None:
        self._core = core
        self._channel = channel
        self._period_ms = period_ms
        self._hello_timeout_ms = hello_timeout_ms
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._run_id: str | None = None
        self.sent = 0

    def request_stop(self) -> None:
        """次の待ちで終わる。"""
        self._stop.set()

    def run(self, *, max_periods: int | None = None) -> None:
        """切断・停止まで回す。切断は `ChannelClosed` で返す。"""
        hello = self._channel.receive_hello(timeout_s=self._hello_timeout_ms / 1_000)
        self._run_id = hello.run_id
        failure: list[BaseException] = []
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(hello.heartbeat_interval_ms, failure),
            name="learned-worker-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        periods = 0
        next_due = self._monotonic.monotonic_ms()
        try:
            while not self._stop.is_set():
                if failure:
                    raise ChannelClosed("heartbeat を送れない")
                wait_ms = max(0, next_due - self._monotonic.monotonic_ms())
                for received in self._channel.receive_frames(timeout_s=wait_ms / 1_000):
                    self._core.receive(received.run_id, received.frame)
                    self._run_id = received.run_id
                now = self._monotonic.monotonic_ms()
                if now < next_due:
                    continue
                result = self._core.step()
                run_id = self._core.history.run_id
                if result is not None and run_id is not None:
                    self._channel.send_result(run_id, result)
                    self.sent += 1
                periods += 1
                if max_periods is not None and periods >= max_periods:
                    return
                # 遅れた周期を詰めて取り戻さない（最後の frame に1回だけ。0077 §2.3）
                next_due = max(next_due + self._period_ms, now)
        finally:
            self._stop.set()
            heartbeat.join(timeout=hello.heartbeat_interval_ms / 1_000)

    def _heartbeat(self, interval_ms: int, failure: list[BaseException]) -> None:
        while not self._stop.wait(interval_ms / 1_000):
            run_id = self._run_id
            if run_id is None:
                continue
            try:
                self._channel.send_heartbeat(run_id)
            except ChannelClosed as error:
                failure.append(error)
                return


def build_parser() -> argparse.ArgumentParser:
    """CLI。**閾値は受け取らない**（設定ファイルが持つ）。較正の path は持たない（0101 §2.1）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-learnd",
        description="Learned MPC worker（提案を coldaisle-fand へ送るだけ。hwmon には触れない）",
    )
    parser.add_argument("--role", choices=("mpc", "supervisor"), required=True)
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument("--registry-limits", type=Path, default=DEFAULT_REGISTRY_LIMITS)
    parser.add_argument("--learned-channel-config", type=Path, required=True)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-learnd` の入口。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    if args.role != "mpc":
        LOGGER.error(
            "RL Supervisor worker は 0077 段階 4（#89）まで無い。起動しない",
            extra={logs.FIELDS_KEY: {"reason": "role_not_supported", "role": args.role}},
        )
        return EXIT_ROLE_NOT_SUPPORTED
    monotonic: MonotonicClock = SystemMonotonicClock()
    try:
        control = ControlConfig.from_directory(args.config_dir)
        catalog, catalog_sha256 = MetricCatalog.from_yaml_with_sha256(args.metrics)
        settings = LearnedChannelSettings.from_yaml(args.learned_channel_config)
        registry = ModelRegistry(
            args.registry_root, limits=load_model_registry_limits(args.registry_limits)
        )
    except Exception:
        LOGGER.exception("worker の設定を読めないため起動しない")
        return EXIT_STARTUP
    core = MpcWorkerCore(
        WorkerInputs(control=control, catalog=catalog, catalog_sha256=catalog_sha256),
        artifacts=RegistryArtifactSource(registry),
        production=RegistryProductionCheck(registry, args.registry_root),
        monotonic_ms=monotonic.monotonic_ms,
    )
    try:
        channel = WorkerChannel.connect(
            settings.sockets.mpc.path,
            max_message_bytes=settings.limits.max_message_bytes.value,
        )
    except ChannelClosed:
        LOGGER.exception("coldaisle-fand の MPC のソケットに接続できない")
        return EXIT_CHANNEL_CLOSED
    worker = MpcWorker(
        core,
        channel,
        period_ms=control.policy.mpc.period_ms,
        hello_timeout_ms=settings.worker_idle_timeout_ms.value,
        monotonic=monotonic,
    )

    def _stop(signum: int, frame: FrameType | None) -> None:
        worker.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        worker.run()
    except ChannelClosed:
        LOGGER.exception("coldaisle-fand との接続が切れたため終了する")
        return EXIT_CHANNEL_CLOSED
    finally:
        channel.close()
    return EXIT_OK
