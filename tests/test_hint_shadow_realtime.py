"""Shadow solver の実時間の CPU の取り合いを含む試験（#288 / 0133 §2.5・0162 §2.5 の PR-B）。

実際の子プロセスを ``SCHED_IDLE`` で起動し、論理 CPU の数だけの計算ループ（``SCHED_IDLE``）で CPU を
使い切った状態と、子を起動しない状態とで、同じ frame の列を実時間で流す（fand 側の受信を遅らせる
条件も含める）。各周期の「``next_due`` から基準の ``MpcProposal`` を送り終えるまでの時間」の分布の
p50 / p99 / 最大の差が、``config/hint-shadow-realtime.yaml`` の許容幅に収まることを確かめる。

**実時間に依存するので通常の CI からは外す**（``hardware`` と ``realtime`` の marker。CI は
``-m "not hardware"``）。リリースゲートと B1 の着手の前に本番のホストで走らせる（0133 §2.6）::

    uv run pytest -m realtime tests/test_hint_shadow_realtime.py
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from coldaisle.clock import SystemMonotonicClock
from coldaisle.control.learned_handoff import LearnedRole
from coldaisle.control.model_registry import ModelRegistry
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.learned_channel.messages import (
    FrameBody,
    HelloBody,
    OutboundEnvelope,
    encode_outbound,
    parse_inbound,
)
from coldaisle.learned_worker.cli import MpcWorker
from coldaisle.learned_worker.client import WorkerChannel
from coldaisle.learned_worker.mpc import MpcWorkerCore, RegistryArtifactSource, WorkerInputs
from coldaisle.learned_worker.registry import RegistryProductionCheck
from coldaisle.learned_worker.shadow import HintShadowCoordinator
from coldaisle.learned_worker.shadow_solver import (
    ShadowChildArgs,
    ShadowSolverProcess,
    SolverState,
)
from hint_shadow_world import (
    METRICS,
    REGISTRY_LIMITS,
    REGISTRY_LIMITS_DIR,
    World,
    build_world,
    write_control,
)
from test_hint_certify import registry_root
from test_learned_mpc import CATALOG
from test_learned_worker import CATALOG_SHA256, RUN_ID, frame

__all__ = ["registry_root"]

pytestmark = [pytest.mark.hardware, pytest.mark.realtime]

REPO = Path(__file__).resolve().parents[1]
SETTINGS = yaml.safe_load((REPO / "config" / "hint-shadow-realtime.yaml").read_text("utf-8"))
CHANNEL = LearnedChannelSettings.from_yaml(REPO / "config" / "learned-channel.yaml")


def setting(*path: str) -> int:
    node: Any = SETTINGS
    for key in path:
        node = node[key]
    return int(node["value"])


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory, registry_root: Path) -> World:
    config_dir = tmp_path_factory.mktemp("shadow-realtime") / "config"
    write_control(config_dir)
    return build_world(config_dir, registry_root)


@dataclass
class Run:
    lags_ms: list[int]
    shadow_records: int


class RealtimeFand:
    """``socketpair`` の fand の側。frame を実時間の周期で送り、届いたものを読む。"""

    def __init__(self, world: World, *, periods: int, period_ms: int, read_delay_ms: int):
        self.sock, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.channel = WorkerChannel(
            worker, max_message_bytes=CHANNEL.limits.max_message_bytes.value
        )
        self.world = world
        self.periods = periods
        self.period_ms = period_ms
        self.read_delay_s = read_delay_ms / 1_000
        self.stop = threading.Event()
        self.shadow_records = 0
        self.sock.send(
            encode_outbound(
                OutboundEnvelope(
                    run_id=RUN_ID, role=LearnedRole.MPC, body=HelloBody(heartbeat_interval_ms=1_000)
                )
            )
        )
        self.frames = [
            encode_outbound(
                OutboundEnvelope(
                    run_id=RUN_ID,
                    role=LearnedRole.MPC,
                    body=FrameBody(
                        frame=frame(tick, pins=world.pinned, config=world.control, with_hint=True)
                    ),
                )
            )
            for tick in range(10, 10 + periods + 5)
        ]
        self.threads = [
            threading.Thread(target=self._send, daemon=True),
            threading.Thread(target=self._read, daemon=True),
        ]

    def start(self) -> None:
        for thread in self.threads:
            thread.start()

    def close(self) -> None:
        self.stop.set()
        self.sock.close()

    def _send(self) -> None:
        for data in self.frames:
            if self.stop.wait(self.period_ms / 1_000):
                return
            try:
                self.sock.send(data)
            except OSError:
                return

    def _read(self) -> None:
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                data = self.sock.recv(1 << 20)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            if parse_inbound(data).body.kind == "hint_shadow":
                self.shadow_records += 1
            if self.read_delay_s:
                time.sleep(self.read_delay_s)


def core_for(world: World) -> MpcWorkerCore:
    registry = ModelRegistry(world.registry_root, limits=REGISTRY_LIMITS)
    return MpcWorkerCore(
        WorkerInputs(control=world.control, catalog=CATALOG, catalog_sha256=CATALOG_SHA256),
        artifacts=RegistryArtifactSource(registry),
        production=RegistryProductionCheck(registry, world.registry_root),
        monotonic_ms=SystemMonotonicClock().monotonic_ms,
    )


def start_solver(world: World) -> ShadowSolverProcess:
    clock = SystemMonotonicClock()
    solver = ShadowSolverProcess(
        ShadowChildArgs(
            config_dir=world.config_dir,
            metrics=METRICS,
            registry_root=world.registry_root,
            registry_limits=REGISTRY_LIMITS_DIR,
            lockfile=None,
            parent_isolated=True,
            parent_pid=os.getpid(),
            log_level="WARNING",
        ),
        respawn_interval_ms=1_000,
        unresponsive_after_ms=60_000,
        max_reply_bytes=CHANNEL.limits.max_message_bytes.value,
        monotonic_ms=clock.monotonic_ms,
    )
    solver.start(clock.monotonic_ms())
    deadline = time.monotonic() + 60
    while solver.state is not SolverState.IDLE:
        assert time.monotonic() < deadline, solver.state
        solver.service(clock.monotonic_ms())
        time.sleep(0.01)
    return solver


def run(world: World, *, with_shadow: bool, read_delay_ms: int) -> Run:
    periods, period_ms = setting("periods"), setting("period_ms")
    fand = RealtimeFand(world, periods=periods, period_ms=period_ms, read_delay_ms=read_delay_ms)
    clock = SystemMonotonicClock()
    shadow = None
    if with_shadow:
        limits = CHANNEL.limits
        assert limits.max_shadow_record_bytes is not None
        assert limits.packet_overhead_bytes is not None
        assert fand.channel.reserve_shadow_send_buffer(
            limits.max_shadow_record_bytes.value + limits.packet_overhead_bytes.value
        )
        shadow = HintShadowCoordinator(
            solver=start_solver(world),
            sender=fand.channel,
            control=world.control,
            max_record_bytes=limits.max_shadow_record_bytes.value,
            parent_isolated=True,
            monotonic_ms=clock.monotonic_ms,
        )
    worker = MpcWorker(
        core_for(world),
        fand.channel,
        period_ms=period_ms,
        hello_timeout_ms=5_000,
        monotonic=clock,
        shadow=shadow,
    )
    lags: list[int] = []
    worker.on_sent = lambda due, sent: lags.append(sent - due)
    fand.start()
    try:
        worker.run(max_periods=periods)
    finally:
        worker.close()
        fand.close()
    return Run(lags_ms=lags, shadow_records=fand.shadow_records)


def percentile(values: list[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


@pytest.mark.parametrize("read_delay", [False, True], ids=["fand-reads", "fand-slow"])
def test_the_base_send_time_is_indistinguishable_under_a_saturating_shadow_load(
    world: World, read_delay: bool
) -> None:
    delay_ms = setting("slow_reader_delay_ms") if read_delay else 0
    baseline = run(world, with_shadow=False, read_delay_ms=delay_ms)
    context = multiprocessing.get_context("spawn")
    stop, ready = context.Event(), context.Semaphore(0)
    from shadow_child_hooks import burn_at_sched_idle

    burners = [
        context.Process(target=burn_at_sched_idle, args=(stop, ready), daemon=True)
        for _ in range(os.cpu_count() or 1)
    ]
    for burner in burners:
        burner.start()
    try:
        for _ in burners:
            assert ready.acquire(timeout=60), "計算ループを SCHED_IDLE に下げられない"
        loaded = run(world, with_shadow=True, read_delay_ms=delay_ms)
    finally:
        stop.set()
        for burner in burners:
            burner.join(timeout=5)
            if burner.is_alive():
                burner.kill()
    assert baseline.lags_ms and loaded.lags_ms
    assert loaded.shadow_records > 0, "子への依頼と記録の転送が実際に起きていること"
    for name, q in (("p50", 0.5), ("p99", 0.99)):
        difference = percentile(loaded.lags_ms, q) - percentile(baseline.lags_ms, q)
        assert difference <= setting("tolerance_ms", name), (name, difference)
    difference = max(loaded.lags_ms) - max(baseline.lags_ms)
    assert difference <= setting("tolerance_ms", "max"), ("max", difference)
