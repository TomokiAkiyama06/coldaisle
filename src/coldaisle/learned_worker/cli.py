"""`coldaisle-learnd` の入口（#86 / #89、決定記録 0077 段階 3 / 4、0107 §2.6 / §2.7）。

`--role mpc`（Learned MPC worker）と `--role supervisor`（RL Supervisor worker）を受け付ける。
役割ごとに別の unit・別のユーザーで動かす（0077 §2.1）。役割は接続するソケットで決まる。

- Control Config（4ファイル）を起動時に読む。frame の `config` と照合するためで、較正ファイル・
  Telemetry・SQLite・API は読まない。MPC は Metric Catalog も読む（frame の
  `metric_catalog_sha256` との照合と L8。RL Supervisor は使わない）
- registry は `--registry-root` を読み取り専用で読み、frame が固定した artifact だけを検証して使う
- MPC は `mpc.period_ms`、RL Supervisor は `supervisor.period_ms` ごと（worker の単調時計）に、
  最後の frame に対して1回結果を作る
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
from typing import Protocol

from coldaisle import logs
from coldaisle.clock import MonotonicClock, SystemMonotonicClock, WallClock
from coldaisle.control.config import ControlConfig
from coldaisle.control.learned_handoff import LearnedFrame, LearnedRole
from coldaisle.control.model_registry import ModelRegistry, load_model_registry_limits
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.supervisor.policy import DeliveredSupervisorOutput
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.learned_worker.client import ChannelClosed, WorkerChannel
from coldaisle.learned_worker.mpc import (
    MpcWorkerCore,
    RegistryArtifactSource,
    WorkerInputs,
)
from coldaisle.learned_worker.registry import RegistryProductionCheck
from coldaisle.learned_worker.supervisor import (
    RegistryPolicySource,
    SupervisorWorkerCore,
    SupervisorWorkerInputs,
)
from coldaisle.metrics import MetricCatalog

LOGGER = logging.getLogger("coldaisle.learned_worker")

EXIT_OK = 0
EXIT_CHANNEL_CLOSED = 3
"""fand に接続できない・接続が切れた。"""
EXIT_STARTUP = 5
"""設定・Metric Catalog・registry の設定を読めない。"""

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_METRICS = Path("config/metrics.yaml")
DEFAULT_REGISTRY_LIMITS = Path("config")
"""fand（`coldaisle-fand`）と同じ既定。"""


class WorkerCore[ResultT](Protocol):
    """1周期の判断（I/O を持たない）。"""

    def receive(self, run_id: str, frame: LearnedFrame) -> None:
        """検証を通った frame を受け取る。"""

    def step(self) -> ResultT | None:
        """この周期の結果。何も送らない周期は None。"""


class PeriodicWorker[ResultT]:
    """core をソケットと時計につなぐ（役割に依らない部分）。"""

    def __init__(
        self,
        core: WorkerCore[ResultT],
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

    def close(self) -> None:
        """fand との接続を閉じる。"""
        self._channel.close()

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
                run_id = self._core_run_id()
                if result is not None and run_id is not None:
                    self._send(run_id, result)
                    self.sent += 1
                periods += 1
                if max_periods is not None and periods >= max_periods:
                    return
                # 遅れた周期を詰めて取り戻さない（最後の frame に1回だけ。0077 §2.3）。
                # 推論や送信が周期を超えたら、**終わった時刻から**次の周期を数える
                next_due += self._period_ms
                finished = self._monotonic.monotonic_ms()
                if next_due <= finished:
                    next_due = finished + self._period_ms
        finally:
            self._stop.set()
            heartbeat.join(timeout=hello.heartbeat_interval_ms / 1_000)

    def _core_run_id(self) -> str | None:
        raise NotImplementedError

    def _send(self, run_id: str, result: ResultT) -> None:
        raise NotImplementedError

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


class MpcWorker(PeriodicWorker[MpcProposal]):
    """`MpcWorkerCore` をソケットと時計につなぐ。"""

    _core: MpcWorkerCore

    def _core_run_id(self) -> str | None:
        return self._core.history.run_id

    def _send(self, run_id: str, result: MpcProposal) -> None:
        self._channel.send_result(run_id, result)


class SupervisorWorker(PeriodicWorker[DeliveredSupervisorOutput]):
    """`SupervisorWorkerCore` をソケットと時計につなぐ（#89 / 0077 段階 4）。"""

    _core: SupervisorWorkerCore

    def _core_run_id(self) -> str | None:
        return self._core.run_id

    def _send(self, run_id: str, result: DeliveredSupervisorOutput) -> None:
        self._channel.send_supervisor_result(run_id, result)


def build_parser() -> argparse.ArgumentParser:
    """CLI。**閾値は受け取らない**（設定ファイルが持つ）。較正の path は持たない（0101 §2.1）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-learnd",
        description=(
            "Learned MPC / RL Supervisor worker（結果を coldaisle-fand へ送るだけ。"
            "hwmon には触れない）"
        ),
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
    role = LearnedRole(args.role)
    monotonic: MonotonicClock = SystemMonotonicClock()
    try:
        control = ControlConfig.from_directory(args.config_dir)
        settings = LearnedChannelSettings.from_yaml(args.learned_channel_config)
        registry = ModelRegistry(
            args.registry_root, limits=load_model_registry_limits(args.registry_limits)
        )
        worker = _build_worker(role, args, control, settings, registry, monotonic)
    except ChannelClosed:
        LOGGER.exception(
            "coldaisle-fand の役割のソケットに接続できない",
            extra={logs.FIELDS_KEY: {"role": role.value}},
        )
        return EXIT_CHANNEL_CLOSED
    except Exception:
        LOGGER.exception("worker の設定を読めないため起動しない")
        return EXIT_STARTUP

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
        worker.close()
    return EXIT_OK


def _build_worker(
    role: LearnedRole,
    args: argparse.Namespace,
    control: ControlConfig,
    settings: LearnedChannelSettings,
    registry: ModelRegistry,
    monotonic: MonotonicClock,
) -> MpcWorker | SupervisorWorker:
    """役割の core を作り、その役割のソケットへ接続する（接続は最後。設定の誤りを先に見せる）。"""
    production = RegistryProductionCheck(registry, args.registry_root, role=role)
    if role is LearnedRole.MPC:
        catalog, catalog_sha256 = MetricCatalog.from_yaml_with_sha256(args.metrics)
        mpc_core = MpcWorkerCore(
            WorkerInputs(control=control, catalog=catalog, catalog_sha256=catalog_sha256),
            artifacts=RegistryArtifactSource(registry),
            production=production,
            monotonic_ms=monotonic.monotonic_ms,
        )
        return MpcWorker(
            mpc_core,
            _connect(role, settings),
            period_ms=control.policy.mpc.period_ms,
            hello_timeout_ms=settings.worker_idle_timeout_ms.value,
            monotonic=monotonic,
        )
    supervisor_core = SupervisorWorkerCore(
        SupervisorWorkerInputs(control=control),
        policies=RegistryPolicySource(registry),
        production=production,
        # `SupervisorOutput.computed_at_ms` の壁時計。期限は fand が元 snapshot の単調時刻から数える
        clock=WallClock(),
    )
    return SupervisorWorker(
        supervisor_core,
        _connect(role, settings),
        period_ms=control.policy.supervisor.period_ms,
        hello_timeout_ms=settings.worker_idle_timeout_ms.value,
        monotonic=monotonic,
    )


def _connect(role: LearnedRole, settings: LearnedChannelSettings) -> WorkerChannel:
    return WorkerChannel.connect(
        settings.sockets.for_role(role).path,
        max_message_bytes=settings.limits.max_message_bytes.value,
        role=role,
    )
