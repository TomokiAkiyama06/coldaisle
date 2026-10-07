"""Learned worker との経路（#86 / 決定記録 0077 段階 1、`applied` は 0092）。実機不要。

**worker はまだ無い**ので、偽 worker（実際の `SOCK_SEQPACKET` の接続）と、`SO_PEERCRED` と
グループの所属を偽装した試験用の門で確かめる（0077 §2.10 の表）。

1. 設定（§2.7）: 役割ごとの別グループ・別 path、
   `worker_idle_timeout_ms >= 3 * heartbeat_interval_ms`
2. 起動時の古いソケットの検査（§2.7）: probe をソケットの種類で作り、`EPROTOTYPE` は止まる
3. 認可（§2.7）: 役割のグループだけ・もう一方のグループに属する相手は拒む・同じ uid は拒む・
   役割ごとに1接続・2つのグループの重なりは起動時に経路を開かない
4. 受信（§2.4 / §2.5）: 検証を通った最新の1件だけを置く。壊れた出力・別の `run_id`・別の `role` は
   捨てて数え、受け渡し口を変えない。切断・固まりはその時点で受け渡し口を空にする。heartbeat は
   idle を数え直すだけ
5. 送信（§2.2 / §2.3）: frame は heartbeat の後に置くだけで、送れなければ捨てる。`applied` は
   `FanHardwareResult.applied_demand` から作り、確かめられない zone は欠測（0092）
6. loop（§2.2 / §2.5）: 受付スレッドの死で Learned だけが閉じ（Max にはしない）、経路の状態が
   `learned_proposal_unavailable` の detail に入る。loop は待たない
7. 構造: `coldaisle.control` は経路の実装を import しない。経路は LLM・API・hwmon に届かない
"""

from __future__ import annotations

import ast
import dataclasses
import errno
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.clock import SystemMonotonicClock
from coldaisle.control.fallback.gate import ControllerGate, LearnedControlStatus
from coldaisle.control.hardware.simulated import SimulatedFaultPlan
from coldaisle.control.learned_handoff import (
    LearnedChannelState,
    LearnedFrame,
    LearnedRole,
)
from coldaisle.control.loop import StaticAuthority
from coldaisle.control.mpc import MpcProposal
from coldaisle.control.schema import (
    AuthorityStage,
    ControllerKind,
    OptimizerStatus,
    Reason,
    SafetyState,
    Zone,
)
from coldaisle.control_daemon import Config, build
from coldaisle.learned_channel import DisabledLearnedChannel, open_learned_channel
from coldaisle.learned_channel import server as learned_server
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.learned_channel.mailbox import LearnedMailbox
from coldaisle.learned_channel.messages import parse_inbound
from coldaisle.local_socket import SocketStartupError, prepare_path
from coldaisle.metrics import MetricCatalog
from conftest import CONFIG_DIR
from test_control_config import valid_documents, write_documents
from test_control_loop import METRICS_PATH, Harness, control_config
from test_fallback_controller import assessment_for, learned_proposal
from test_simulated_fan_backend import hardware_config

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"

RUN_ID = "0123456789abcdef0123456789abcdef"
OTHER_RUN_ID = "fedcba9876543210fedcba9876543210"

MPC_GROUP, RL_GROUP = "coldaisle-learn-mpc", "coldaisle-learn-rl"
MPC_GID, RL_GID = 61_001, 61_002
MPC_UID, RL_UID, STRANGER_UID = 51_001, 51_002, 51_003
"""偽装した資格（実在の uid / gid ではない）。"""

WAIT_S = 3.0


# ----------------------------------------------------------------- 足場


@pytest.fixture(scope="module")
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(METRICS_PATH)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """Unix ソケットのパスは 107 バイトまで。pytest の tmp_path は長くなりうる。"""
    directory = Path(tempfile.mkdtemp(prefix="clrn-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def channel_document(
    directory: Path,
    *,
    heartbeat_ms: int = 100,
    idle_ms: int = 400,
    max_message_bytes: int = 65_536,
) -> dict[str, Any]:
    return {
        "version": 1,
        "sockets": {
            "mpc": {"path": str(directory / "mpc.sock"), "mode": "0660", "group": MPC_GROUP},
            "supervisor": {
                "path": str(directory / "rl.sock"),
                "mode": "0660",
                "group": RL_GROUP,
            },
        },
        "limits": {
            "max_message_bytes": {
                "value": max_message_bytes,
                "status": "provisional",
                "basis": "test",
            },
            "listen_backlog": 4,
        },
        "heartbeat_interval_ms": {"value": heartbeat_ms, "status": "provisional", "basis": "test"},
        "worker_idle_timeout_ms": {"value": idle_ms, "status": "provisional", "basis": "test"},
    }


class FakeGroups:
    """グループの所属の偽装（`SO_PEERCRED` の門の試験用。0072 の試験と同じ考え方）。"""

    def __init__(self) -> None:
        self.gids = {MPC_GROUP: MPC_GID, RL_GROUP: RL_GID}
        self.member: dict[int, set[int]] = {MPC_GID: {MPC_UID}, RL_GID: {RL_UID}}

    def gid(self, name: str) -> int:
        try:
            return self.gids[name]
        except KeyError as exc:
            raise SocketStartupError(name) from exc

    def members(self, gid: int) -> frozenset[int]:
        return frozenset(self.member.get(gid, set()))

    def is_member(self, uid: int, gid: int) -> bool:
        return uid in self.member.get(gid, set())


class FakePeer:
    """次に受け付ける接続の uid（`SO_PEERCRED` の偽装）。"""

    def __init__(self) -> None:
        self.uid = MPC_UID

    def __call__(self, sock: socket.socket) -> int:
        return self.uid


@pytest.fixture
def no_file_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """偽の gid はファイルへ付けられない。ファイル権限の門は 0072 / 0045 の試験が持つ。"""

    def prepare_parent(parent: Path, group_gid: int | None, *, service: str) -> None:
        parent.mkdir(mode=0o750, parents=True, exist_ok=True)

    monkeypatch.setattr(learned_server, "prepare_parent", prepare_parent)
    monkeypatch.setattr(learned_server, "set_socket_group", lambda path, gid: None)


class Running:
    def __init__(self, entry: Any, groups: FakeGroups, peer: FakePeer, directory: Path) -> None:
        self.entry = entry
        self.mailbox: LearnedMailbox = entry.mailbox
        self.server = entry.server
        self.groups = groups
        self.peer = peer
        self.directory = directory

    def path(self, role: LearnedRole) -> Path:
        return self.server.socket_path(role)


@pytest.fixture
def channel(short_dir: Path, no_file_groups: None) -> Iterator[Callable[..., Running]]:
    opened: list[Any] = []

    def open_(**document_overrides: Any) -> Running:
        config = short_dir / "learned-channel.yaml"
        config.write_text(
            yaml.safe_dump(channel_document(short_dir / "run", **document_overrides)), "utf-8"
        )
        groups, peer = FakeGroups(), FakePeer()
        entry = open_learned_channel(
            config,
            run_id=RUN_ID,
            tick_deadline_ms=500,
            monotonic=SystemMonotonicClock(),
            server_uid=os.geteuid(),
            groups=groups,
            peer=peer,
        )
        assert entry is not None
        opened.append(entry)
        return Running(entry, groups, peer, short_dir)

    yield open_
    for entry in opened:
        entry.stop()


def wait_until(condition: Callable[[], bool], timeout_s: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class Worker:
    """偽 worker。実際の `SOCK_SEQPACKET` で接続する。"""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.settimeout(WAIT_S)
        self.sock.connect(str(path))

    def receive(self) -> dict[str, Any] | None:
        """1メッセージ。EOF（拒否・切断）なら None。"""
        data = self.sock.recv(1 << 20)
        return None if not data else json.loads(data)

    def send(self, payload: dict[str, Any] | bytes) -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.sock.send(data)

    def close(self) -> None:
        self.sock.close()


def connect(running: Running, role: LearnedRole, uid: int) -> tuple[Worker, dict[str, Any] | None]:
    running.peer.uid = uid
    worker = Worker(running.path(role))
    return worker, worker.receive()


def failure_result(detail: str = "capability mismatch") -> MpcProposal:
    from coldaisle.control.fallback import LearnedFailure

    return MpcProposal(
        failure=LearnedFailure.MODEL_LOAD_FAILURE,
        failure_reason=Reason(code="model_unusable", detail=detail),
    )


def proposal_result(demand: float = 0.8) -> MpcProposal:
    proposal = learned_proposal(demand, optimizer=OptimizerStatus.TIMEOUT)
    return MpcProposal(
        proposal=proposal,
        assessment=assessment_for(proposal),
        binding_authority_stage=AuthorityStage.SHADOW,
    )


def envelope(
    result: MpcProposal | None = None,
    *,
    run_id: str = RUN_ID,
    role: str = "mpc",
    version: int = 1,
) -> dict[str, Any]:
    body: dict[str, Any] = (
        {"kind": "heartbeat"}
        if result is None
        else {"kind": "mpc_result", "result": json.loads(result.model_dump_json())}
    )
    return {"schema_version": version, "run_id": run_id, "role": role, "body": body}


@pytest.fixture(scope="module")
def sample_frame(catalog: MetricCatalog) -> LearnedFrame:
    """loop が作った本物の frame（送信の試験用）。"""
    sink = RecordingSink()
    harness = Harness(
        catalog, learned_source=StaticSource(), learned_health=sink, learned_sink=sink
    )
    harness.settle()
    assert sink.frames
    return sink.frames[-1]


class StaticSource:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.polls = 0

    def poll(self) -> Any:
        self.polls += 1
        return self.result


class RecordingSink:
    """`LearnedFrameSink` と `LearnedChannelHealth` の偽物。"""

    def __init__(
        self,
        state: LearnedChannelState = LearnedChannelState.CONNECTED,
        order: list[str] | None = None,
    ) -> None:
        self.frames: list[LearnedFrame] = []
        self.current = state
        self.order = order

    def offer(self, frame: LearnedFrame) -> None:
        if self.order is not None:
            self.order.append("learned_frame")
        self.frames.append(frame)

    def state(self, role: LearnedRole) -> LearnedChannelState:
        return self.current


# ================================================================ 1. 設定（0077 §2.7）


def test_the_shipped_config_is_valid_and_names_a_group_per_role() -> None:
    settings = LearnedChannelSettings.from_yaml(CONFIG_DIR / "learned-channel.yaml")
    assert settings.sockets.mpc.group != settings.sockets.supervisor.group
    assert settings.sockets.mpc.path != settings.sockets.supervisor.path
    assert settings.worker_idle_timeout_ms.value >= 3 * settings.heartbeat_interval_ms.value


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: d.update(
                worker_idle_timeout_ms={**d["worker_idle_timeout_ms"], "value": 299}
            ),
            "3倍",
        ),
        (lambda d: d["sockets"]["supervisor"].update(group=MPC_GROUP), "グループ"),
        (lambda d: d["sockets"]["supervisor"].update(path=d["sockets"]["mpc"]["path"]), "path"),
        (lambda d: d["sockets"]["mpc"].update(group=None), "専用グループ"),
        (lambda d: d["sockets"]["mpc"].update(mode="0666"), "other"),
        (lambda d: d.update(extra=1), "extra"),
    ],
)
def test_an_invalid_config_is_refused(short_dir: Path, mutate: Any, message: str) -> None:
    document = channel_document(short_dir)
    mutate(document)
    with pytest.raises(ValidationError, match=message):
        LearnedChannelSettings.model_validate(document)


def test_an_invalid_config_disables_the_channel_without_stopping(short_dir: Path, caplog) -> None:
    document = channel_document(short_dir / "run", heartbeat_ms=200, idle_ms=500)
    config = short_dir / "learned-channel.yaml"
    config.write_text(yaml.safe_dump(document), "utf-8")
    entry = open_learned_channel(
        config, run_id=RUN_ID, tick_deadline_ms=500, monotonic=SystemMonotonicClock()
    )
    assert entry is None
    assert not (short_dir / "run" / "mpc.sock").exists()
    assert any(
        getattr(record, "fields", {}).get("state") == "channel_disabled"
        for record in caplog.records
    )


# ======================================================= 2. 起動時の古いソケット（0077 §2.7）


def _listen(path: Path, kind: socket.SocketKind) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, kind)
    sock.bind(str(path))
    sock.listen(1)
    return sock


def test_a_live_seqpacket_listener_is_not_removed(short_dir: Path) -> None:
    path = short_dir / "live.sock"
    listener = _listen(path, socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(SocketStartupError, match="待ち受けている"):
            prepare_path(path, service="test", sock_type=socket.SOCK_SEQPACKET)
        assert path.exists()
    finally:
        listener.close()


def test_a_stale_seqpacket_inode_is_removed(short_dir: Path) -> None:
    path = short_dir / "stale.sock"
    _listen(path, socket.SOCK_SEQPACKET).close()
    assert path.exists()
    prepare_path(path, service="test", sock_type=socket.SOCK_SEQPACKET)
    assert not path.exists()


def test_a_listener_of_another_kind_is_a_startup_error_not_a_raw_oserror(short_dir: Path) -> None:
    """`EPROTOTYPE` は別の種類の待ち受け。消さずに `SocketStartupError` にする。"""
    path = short_dir / "stream.sock"
    listener = _listen(path, socket.SOCK_STREAM)
    try:
        with pytest.raises(SocketStartupError, match="種類"):
            prepare_path(path, service="test", sock_type=socket.SOCK_SEQPACKET)
        assert path.exists()
    finally:
        listener.close()


def test_the_default_stream_probe_still_removes_a_stale_stream_socket(short_dir: Path) -> None:
    path = short_dir / "admin.sock"
    _listen(path, socket.SOCK_STREAM).close()
    prepare_path(path, service="test")
    assert not path.exists()


def test_a_leftover_live_socket_disables_the_channel_but_not_the_daemon(
    short_dir: Path, no_file_groups: None
) -> None:
    directory = short_dir / "run"
    directory.mkdir()
    squatter = _listen(directory / "mpc.sock", socket.SOCK_STREAM)
    try:
        config = short_dir / "learned-channel.yaml"
        config.write_text(yaml.safe_dump(channel_document(directory)), "utf-8")
        entry = open_learned_channel(
            config,
            run_id=RUN_ID,
            tick_deadline_ms=500,
            monotonic=SystemMonotonicClock(),
            groups=FakeGroups(),
            peer=FakePeer(),
        )
        assert entry is None
        assert (directory / "mpc.sock").exists(), "別の待ち受けを消さない"
        assert not (directory / "rl.sock").exists(), "途中まで作ったソケットを残さない"
    finally:
        squatter.close()


# ================================================================ 3. 認可（0077 §2.7）


def test_an_mpc_worker_is_accepted_and_told_the_heartbeat_interval(channel) -> None:
    running = channel()
    worker, hello = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        assert hello is not None
        assert hello["body"] == {"kind": "hello", "heartbeat_interval_ms": 100}
        assert hello["run_id"] == RUN_ID and hello["role"] == "mpc"
        assert wait_until(
            lambda: running.mailbox.state(LearnedRole.MPC) is LearnedChannelState.CONNECTED
        )
    finally:
        worker.close()


@pytest.mark.parametrize(
    ("role", "uid", "why"),
    [
        (LearnedRole.MPC, RL_UID, "RL のグループの相手は MPC のソケットへ接続できない"),
        (LearnedRole.SUPERVISOR, MPC_UID, "MPC のグループの相手は RL のソケットへ接続できない"),
        (LearnedRole.MPC, STRANGER_UID, "どちらのグループにも属さない"),
        (LearnedRole.MPC, 0, "root も暗黙には認めない"),
    ],
)
def test_a_peer_outside_the_role_group_is_refused(channel, role, uid, why) -> None:
    running = channel()
    worker, hello = connect(running, role, uid)
    try:
        assert hello is None, why
        assert running.mailbox.state(role) is LearnedChannelState.WORKER_DISCONNECTED
    finally:
        worker.close()


def test_the_daemon_uid_is_refused_even_as_a_member(channel) -> None:
    running = channel()
    running.groups.member[MPC_GID].add(os.geteuid())
    worker, hello = connect(running, LearnedRole.MPC, os.geteuid())
    try:
        assert hello is None
    finally:
        worker.close()


def test_a_uid_added_to_both_groups_after_startup_is_refused_on_both_sockets(channel) -> None:
    running = channel()
    running.groups.member[MPC_GID].add(STRANGER_UID)
    running.groups.member[RL_GID].add(STRANGER_UID)
    for role in LearnedRole:
        worker, hello = connect(running, role, STRANGER_UID)
        try:
            assert hello is None, role
        finally:
            worker.close()


def test_only_one_connection_per_role(channel) -> None:
    running = channel()
    first, hello = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        assert hello is not None
        second, refused = connect(running, LearnedRole.MPC, MPC_UID)
        try:
            assert refused is None
        finally:
            second.close()
        # 先の接続は生きたまま
        first.send(envelope(failure_result()))
        assert wait_until(lambda: running.mailbox.poll() is not None)
    finally:
        first.close()


@pytest.mark.parametrize(
    "overlap",
    [
        lambda groups: groups.member[RL_GID].add(MPC_UID),
        lambda groups: groups.gids.update({RL_GROUP: MPC_GID}),
    ],
    ids=["same_uid_in_both", "same_gid_under_two_names"],
)
def test_overlapping_groups_disable_the_channel_at_startup(
    short_dir: Path, no_file_groups: None, overlap: Any
) -> None:
    config = short_dir / "learned-channel.yaml"
    config.write_text(yaml.safe_dump(channel_document(short_dir / "run")), "utf-8")
    groups = FakeGroups()
    overlap(groups)
    entry = open_learned_channel(
        config,
        run_id=RUN_ID,
        tick_deadline_ms=500,
        monotonic=SystemMonotonicClock(),
        groups=groups,
        peer=FakePeer(),
    )
    assert entry is None
    assert not (short_dir / "run" / "mpc.sock").exists()


# ================================================================ 4. 受信（0077 §2.4 / §2.5）


def test_the_latest_valid_result_replaces_the_previous_one(channel) -> None:
    running = channel()
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        first, second = proposal_result(0.6), proposal_result(0.9)
        worker.send(envelope(first))
        assert wait_until(lambda: running.mailbox.poll() == first)
        worker.send(envelope(second))
        assert wait_until(lambda: running.mailbox.poll() == second)
        assert running.mailbox.poll() == second, "取り出さない（最新の1件を覗くだけ）"
    finally:
        worker.close()


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        (lambda: envelope(failure_result("x"), run_id=OTHER_RUN_ID), "run_id_mismatch"),
        (lambda: envelope(failure_result("x"), role="supervisor"), "role_mismatch"),
        (lambda: envelope(failure_result("x"), version=2), "invalid"),
        (lambda: b"{not json", "invalid"),
        (lambda: {**envelope(failure_result("x")), "authority": "full"}, "invalid"),
        (lambda: b"x" * 70_000, "too_long"),
    ],
    ids=["other_run_id", "other_role", "unknown_version", "broken_json", "extra_key", "too_long"],
)
def test_a_broken_message_is_dropped_and_counted_without_touching_the_slot(
    channel, bad: Any, code: str
) -> None:
    running = channel()
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        good = failure_result("kept")
        worker.send(envelope(good))
        assert wait_until(lambda: running.mailbox.poll() == good)
        worker.send(bad())
        assert wait_until(lambda: running.server.dropped.get(f"mpc.{code}", 0) == 1)
        assert running.mailbox.poll() == good
        assert running.mailbox.state(LearnedRole.MPC) is LearnedChannelState.CONNECTED
    finally:
        worker.close()


def test_a_result_on_the_supervisor_socket_is_not_placed(channel) -> None:
    """段階 1 では Supervisor 出力の本文が無い。MPC の結果を送っても置かない（段階 4 の #89）。"""
    running = channel()
    worker, _ = connect(running, LearnedRole.SUPERVISOR, RL_UID)
    try:
        worker.send(envelope(proposal_result(), role="supervisor"))
        assert wait_until(lambda: running.server.dropped.get("supervisor.unsupported_body") == 1)
        assert running.mailbox.poll() is None
    finally:
        worker.close()


def test_a_disconnect_empties_the_slot_at_once(channel) -> None:
    running = channel()
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    worker.send(envelope(proposal_result()))
    assert wait_until(lambda: running.mailbox.poll() is not None)
    worker.close()
    assert wait_until(
        lambda: running.mailbox.state(LearnedRole.MPC) is LearnedChannelState.WORKER_DISCONNECTED
    )
    assert running.mailbox.poll() is None


def test_heartbeats_keep_a_quiet_worker_without_touching_the_slot(channel) -> None:
    running = channel(heartbeat_ms=50, idle_ms=200)
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        result = failure_result("before heartbeats")
        worker.send(envelope(result))
        assert wait_until(lambda: running.mailbox.poll() == result)
        deadline = time.monotonic() + 0.6  # idle の3倍
        while time.monotonic() < deadline:
            worker.send(envelope())
            time.sleep(0.04)
        assert running.mailbox.state(LearnedRole.MPC) is LearnedChannelState.CONNECTED
        assert running.mailbox.poll() == result
    finally:
        worker.close()


def test_a_silent_worker_is_closed_as_idle(channel) -> None:
    running = channel(heartbeat_ms=50, idle_ms=200)
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        worker.send(envelope(failure_result()))
        assert wait_until(lambda: running.mailbox.poll() is not None)
        assert wait_until(
            lambda: running.mailbox.state(LearnedRole.MPC) is LearnedChannelState.WORKER_IDLE
        )
        assert running.mailbox.poll() is None
        assert worker.receive() is None, "受付が接続を閉じる"
        # 固まった接続が閉じたので、再起動した worker は締め出されない
        again, hello = connect(running, LearnedRole.MPC, MPC_UID)
        again.close()
        assert hello is not None
    finally:
        worker.close()


def test_broken_messages_do_not_count_as_liveness(channel) -> None:
    running = channel(heartbeat_ms=50, idle_ms=200)
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            try:
                worker.send(envelope(run_id=OTHER_RUN_ID))
            except BrokenPipeError:
                break  # 受付が idle として閉じた
            time.sleep(0.04)
        assert wait_until(
            lambda: running.mailbox.state(LearnedRole.MPC) is LearnedChannelState.WORKER_IDLE
        )
    finally:
        worker.close()


def test_a_dead_receiver_closes_the_channel(channel, monkeypatch) -> None:
    running = channel()

    def boom(conn: Any) -> None:
        raise RuntimeError("受付スレッドの不具合")

    monkeypatch.setattr(running.server, "_on_readable", boom)
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        worker.send(envelope())
        assert wait_until(lambda: not running.server.thread.is_alive())
        for role in LearnedRole:
            assert running.mailbox.state(role) is LearnedChannelState.CHANNEL_DEAD
    finally:
        worker.close()


# ================================================================ 5. 送信（0077 §2.2 / §2.3、0092）


def test_a_frame_reaches_every_connected_worker(channel, sample_frame) -> None:
    running = channel()
    mpc, _ = connect(running, LearnedRole.MPC, MPC_UID)
    rl, _ = connect(running, LearnedRole.SUPERVISOR, RL_UID)
    try:
        assert wait_until(
            lambda: all(
                running.mailbox.state(role) is LearnedChannelState.CONNECTED for role in LearnedRole
            )
        )
        running.mailbox.offer(sample_frame)
        for worker, role in ((mpc, "mpc"), (rl, "supervisor")):
            message = worker.receive()
            assert message is not None
            assert message["run_id"] == RUN_ID and message["role"] == role
            assert LearnedFrame.model_validate_json(json.dumps(message["body"]["frame"])) == (
                sample_frame
            )
    finally:
        mpc.close()
        rl.close()


def test_a_frame_over_the_limit_is_dropped(channel, sample_frame) -> None:
    running = channel(max_message_bytes=1_024)
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        running.mailbox.offer(sample_frame)
        assert wait_until(lambda: running.server.dropped.get("mpc.frame_too_long") == 1)
    finally:
        worker.close()


def test_a_worker_that_never_reads_does_not_block_the_offer(channel, sample_frame) -> None:
    running = channel(heartbeat_ms=1_000, idle_ms=5_000)
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        started = time.monotonic()
        for _ in range(400):
            running.mailbox.offer(sample_frame)
            time.sleep(0.001)
        assert time.monotonic() - started < WAIT_S
        assert wait_until(lambda: running.server.dropped.get("mpc.frame_send_busy", 0) >= 1)
        assert running.server.thread.is_alive()
    finally:
        worker.close()


def test_offer_and_poll_never_wait_for_the_receiver() -> None:
    """受付スレッドが lock を握っていても、loop は待たない（前回の値・捨てる）。"""
    mailbox = LearnedMailbox()
    thread = threading.Thread(target=lambda: None)
    mailbox.attach_receiver(thread)
    mailbox.mark_stopping()  # 生きているとみなす
    mailbox.connected(LearnedRole.MPC)
    result = failure_result()
    mailbox.place_mpc(result)
    assert mailbox.poll() == result
    with mailbox._lock:
        assert mailbox.poll() == result, "前回の値"
    with mailbox._outgoing_lock:
        mailbox.offer(object())  # type: ignore[arg-type]
    assert mailbox.take_outgoing() is None, "置けなかった frame は捨てる"
    mailbox.close()


def test_poll_returns_nothing_once_the_worker_is_gone_even_without_the_lock() -> None:
    mailbox = LearnedMailbox()
    mailbox.mark_stopping()
    mailbox.connected(LearnedRole.MPC)
    mailbox.place_mpc(failure_result())
    assert mailbox.poll() is not None
    mailbox.disconnected(LearnedRole.MPC, LearnedChannelState.WORKER_DISCONNECTED)
    with mailbox._lock:
        assert mailbox.poll() is None
    mailbox.close()


def test_the_frame_is_built_from_the_same_tick_after_the_heartbeat(catalog) -> None:
    sink = RecordingSink()
    harness = Harness(
        catalog, learned_source=StaticSource(), learned_health=sink, learned_sink=sink
    )
    sink.order = harness.order
    result = harness.settle()
    frame = sink.frames[-1]
    tick = result.tick
    assert frame.snapshot.tick_id == tick.tick_id and frame.snapshot.ts_ms == tick.ts_ms
    assert frame.authority_stage is tick.state.authority_stage
    assert frame.config == tick.runtime.config
    for zone in Zone:
        assert frame.safety_floor.get(zone) == tick.zones.get(zone).demand.safety_floor
    assert frame.baseline is not None and frame.baseline.controller is ControllerKind.FALLBACK
    last = harness.order[harness.order.index("watchdog", len(harness.order) - 6) :]
    assert last.index("watchdog") < last.index("learned_frame"), "frame は heartbeat の後"


class OverridingBackend:
    """本物の simulated backend の結果のうち、front の `applied_demand` だけを差し替える。

    最低安定の引き上げなどで、効いた値が effective と違う tick を作る（0092 §1）。
    """

    def __init__(self, inner: Any, *, front: float) -> None:
        self.inner = inner
        self.front = front

    def apply(self, demands: Any) -> Any:
        results = self.inner.apply(demands)
        front = results.front
        if front.applied_demand is None:
            return results
        return results.model_copy(
            update={"front": dataclasses.replace(front, applied_demand=self.front)}
        )


def test_applied_comes_from_the_hardware_result_not_the_effective_demand(catalog) -> None:
    """0092 §2.1: frame の `applied` は Backend が確かめた値。effective ではない。"""
    sink = RecordingSink()
    harness = Harness(
        catalog, learned_source=StaticSource(), learned_health=sink, learned_sink=sink
    )
    harness.loop._backend = OverridingBackend(harness.backend, front=0.97)
    result = harness.settle()
    frame = sink.frames[-1]
    assert result.tick.zones.front.demand.effective != 0.97
    assert frame.applied.front == 0.97
    for zone in (Zone.REAR, Zone.TOP):
        assert frame.applied.get(zone) == result.hardware.get(zone).applied_demand


def test_an_unconfirmed_zone_is_missing_not_filled(catalog) -> None:
    """0092 §2.2: 書き込みを確かめられない zone は欠測。他の zone は渡す。"""
    sink = RecordingSink()
    harness = Harness(
        catalog,
        learned_source=StaticSource(),
        learned_health=sink,
        learned_sink=sink,
        fault_plan=SimulatedFaultPlan(write_failure=frozenset({Zone.TOP})),
    )
    result = harness.tick()
    frame = sink.frames[-1]
    assert result.tick.zones.top.demand.effective is not None
    assert frame.applied.top is None
    assert frame.applied.front == result.hardware.front.applied_demand is not None


def test_a_tick_without_hardware_results_sends_every_zone_as_missing(catalog) -> None:
    class Broken:
        def apply(self, demands: Any) -> Any:
            raise OSError("hwmon が消えた")

    sink = RecordingSink()
    harness = Harness(
        catalog,
        learned_source=StaticSource(),
        learned_health=sink,
        learned_sink=sink,
        backend=Broken(),
    )
    result = harness.tick()
    assert result.hardware is None
    frame = sink.frames[-1]
    assert (frame.applied.front, frame.applied.rear, frame.applied.top) == (None, None, None)


def test_a_failing_sink_does_not_stop_control(catalog) -> None:
    class FailingSink(RecordingSink):
        def offer(self, frame: LearnedFrame) -> None:
            raise RuntimeError("送り出しの不具合")

    sink = FailingSink()
    harness = Harness(
        catalog, learned_source=StaticSource(), learned_health=sink, learned_sink=sink
    )
    harness.settle()
    assert harness.watchdog.beats > 0


# ================================================================ 6. loop（0077 §2.2 / §2.5）


def limited_harness(catalog: MetricCatalog, **kwargs: Any) -> Harness:
    """Gate が Fallback の理由を trace に残す loop（実効 stage が LIMITED）。

    SHADOW の tick には Fallback の理由が残らない。
    """
    config = control_config(policy={"authority_stage": "limited"})
    authority = StaticAuthority(AuthorityStage.LIMITED, config_ceiling=AuthorityStage.LIMITED)
    gate = ControllerGate(
        config.policy,
        expected_model_version="thermal-vtest",
        expected_artifact_sha256=None,
        authority=authority,
    )
    return Harness(catalog, config=config, gate=gate, authority=authority, **kwargs)


@pytest.mark.parametrize(
    "state",
    [
        LearnedChannelState.WORKER_DISCONNECTED,
        LearnedChannelState.WORKER_IDLE,
        LearnedChannelState.CHANNEL_DISABLED,
    ],
)
def test_the_channel_state_is_the_detail_and_the_slot_is_not_read(catalog, state) -> None:
    source = StaticSource(result=failure_result())
    sink = RecordingSink(state)
    harness = limited_harness(
        catalog, learned_source=source, learned_health=sink, learned_sink=sink
    )
    result = harness.settle()
    reason = result.tick.state.fallback_reason
    assert reason is not None
    assert (reason.code, reason.detail) == ("learned_proposal_unavailable", state.value)
    assert source.polls == 0, "経路が切れている tick では受け渡し口を読まない"


def test_a_dead_receiver_closes_learned_until_restart_without_forcing_max(catalog) -> None:
    source = StaticSource(result=failure_result())
    sink = RecordingSink(LearnedChannelState.CONNECTED)
    harness = limited_harness(
        catalog, learned_source=source, learned_health=sink, learned_sink=sink
    )
    result = harness.settle()
    assert result.tick.state.fallback_reason.code == "model_load_failure"
    polls = source.polls
    sink.current = LearnedChannelState.CHANNEL_DEAD
    dead = harness.tick()
    assert dead.tick.state.fallback_reason.detail == "channel_dead"
    assert dead.tick.state.safety_state is SafetyState.NORMAL
    assert not any(zone.demand.forced_max for zone in (dead.tick.zones.get(z) for z in Zone))
    sink.current = LearnedChannelState.CONNECTED  # 戻っても再起動まで読まない
    later = harness.tick()
    assert later.tick.state.fallback_reason.detail == "channel_dead"
    assert source.polls == polls


def test_a_connected_channel_still_reaches_the_gate(catalog) -> None:
    """経路が健全なら、worker の失敗は既存どおり Gate へ届く（detail は経路の値ではない）。"""
    sink = RecordingSink()
    harness = limited_harness(
        catalog,
        learned_source=StaticSource(result=failure_result("from worker")),
        learned_health=sink,
        learned_sink=sink,
    )
    result = harness.settle()
    assert result.tick.state.fallback_reason.code == "model_load_failure"
    assert "from worker" in result.tick.state.fallback_reason.detail


def test_the_detail_is_only_for_a_missing_proposal() -> None:
    with pytest.raises(ValidationError):
        LearnedControlStatus(
            failure=failure_result().failure,
            failure_reason=Reason(code="x"),
            unavailable_detail=LearnedChannelState.WORKER_IDLE,
        )
    with pytest.raises(ValidationError):
        LearnedControlStatus(unavailable_detail=LearnedChannelState.CONNECTED)


def test_a_worker_over_a_real_socket_reaches_the_gate_and_then_falls_back(catalog, channel) -> None:
    """偽 worker → 受付スレッド → 受け渡し口 → loop → Gate の通しの経路。"""
    running = channel()
    harness = limited_harness(
        catalog,
        learned_source=running.mailbox,
        learned_health=running.mailbox,
        learned_sink=running.mailbox,
    )
    harness.settle()
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        worker.send(envelope(failure_result("over the wire")))
        assert wait_until(lambda: running.mailbox.poll() is not None)
        result = harness.tick()
        assert result.tick.state.fallback_reason.code == "model_load_failure"
        frame = worker.receive()
        assert frame is not None and frame["body"]["kind"] == "frame"
        assert frame["body"]["frame"]["snapshot"]["tick_id"] == result.tick.tick_id
    finally:
        worker.close()
    assert wait_until(
        lambda: running.mailbox.state(LearnedRole.MPC) is LearnedChannelState.WORKER_DISCONNECTED
    )
    after = harness.tick()
    assert (after.tick.state.fallback_reason.code, after.tick.state.fallback_reason.detail) == (
        "learned_proposal_unavailable",
        "worker_disconnected",
    )


def test_the_daemon_runs_with_a_broken_channel_config(tmp_path: Path) -> None:
    """設定が不正なら経路を開かず、Learned だけを無効にして運転する（0077 §2.7）。"""
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(tmp_path, documents)
    broken = tmp_path / "learned-channel.yaml"
    broken.write_text("version: 99\n", "utf-8")
    daemon = build(
        Config(
            config_dir=tmp_path,
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            quality_rules=CONFIG_DIR / "quality.yaml",
            authority_root=tmp_path / "authority",
            learned_channel_config=broken,
        )
    )
    try:
        assert daemon.learned is None
        assert isinstance(daemon.loop._learned_health, DisabledLearnedChannel)
        stats = daemon.run(max_ticks=6)
        assert stats.ticks == 6
    finally:
        daemon.close()


def test_the_daemon_does_not_wire_a_channel_without_a_config(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(tmp_path, documents)
    daemon = build(
        Config(
            config_dir=tmp_path,
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            quality_rules=CONFIG_DIR / "quality.yaml",
            authority_root=tmp_path / "authority",
        )
    )
    try:
        assert daemon.loop._learned_source is None
        assert daemon.loop._learned_health is None
        assert daemon.loop._learned_sink is None
    finally:
        daemon.close()


# ================================================================ 7. 構造


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _hits(paths: list[Path], prefixes: tuple[str, ...]) -> list[str]:
    return [
        f"{path.relative_to(SRC)}: {name}"
        for path in paths
        for name in _imports(path)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
    ]


def test_control_and_the_llm_side_do_not_import_the_channel() -> None:
    paths = [SRC / "server.py"]
    for package in ("ai", "api", "event_entry", "control_admin", "control", "rules", "notify"):
        paths.extend(sorted((SRC / package).rglob("*.py")))
    assert _hits(paths, ("coldaisle.learned_channel",)) == []


def test_the_channel_does_not_reach_the_llm_the_api_or_hwmon() -> None:
    paths = sorted((SRC / "learned_channel").rglob("*.py"))
    forbidden = (
        "coldaisle.ai",
        "coldaisle.api",
        "coldaisle.server",
        "coldaisle.event_entry",
        "coldaisle.control_admin",
        "coldaisle.control.hardware",
        "coldaisle.control.safety",
        "coldaisle.control.loop",
        "coldaisle.control.authority",
        "coldaisle.store",
        "serial",
        "sqlite3",
        "subprocess",
    )
    assert _hits(paths, forbidden) == []


def test_the_worker_body_cannot_carry_a_demand_override_or_authority() -> None:
    """worker から運べる本文は heartbeat と `MpcProposal` だけ（0077 §2.4 の 6）。"""
    with pytest.raises(ValidationError):
        parse_inbound(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": RUN_ID,
                    "role": "mpc",
                    "body": {"kind": "set_mode", "mode": "manual"},
                }
            ).encode()
        )


def test_eprototype_is_what_linux_reports_for_a_kind_mismatch(short_dir: Path) -> None:
    """前提の確認: 同じ path の別の種類の待ち受けへの connect は `EPROTOTYPE`。"""
    path = short_dir / "k.sock"
    listener = _listen(path, socket.SOCK_STREAM)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(OSError) as raised:
            probe.connect(str(path))
        assert raised.value.errno == errno.EPROTOTYPE
    finally:
        probe.close()
        listener.close()
