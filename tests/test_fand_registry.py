"""`coldaisle-fand` の registry の束縛（#104 / 決定記録 0077 段階 2・§2.6）。実機不要。

1. 起動時に1回読む snapshot から provenance・Gate の期待値・`expected_rl_identity`・frame の
   `expected_artifacts` を作り、すべて同じ `revision` の production から来る
2. 読めない・壊れているときは Learned を無効にして起動を続ける（番兵・None・`unbound()`）
3. `--registry-root` を省いた起動はいまと同じ
4. 受付スレッドが `safety.tick_ms` ごとに production を確かめ、移動・消失・破損した役割を
   再起動まで閉じる（`registry_superseded`）。Gate の期待値・trace の provenance は書き換えない
5. authority の束縛（0089）: 起動時の production の artifact で上げた journal だけが有効になる
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from coldaisle.clock import SimulatedClock, SystemMonotonicClock
from coldaisle.control import (
    ApprovalAction,
    ArtifactCapability,
    ArtifactKind,
    ModelRegistry,
)
from coldaisle.control.fallback.gate import LearnedControlStatus
from coldaisle.control.learned_handoff import (
    LEARNED_FRAME_SCHEMA_VERSION,
    LearnedChannelState,
    LearnedExpectedArtifacts,
    LearnedFrame,
    LearnedRole,
    PinnedArtifact,
)
from coldaisle.control.registry_binding import UNCONFIGURED_MODEL_VERSION, RegistryBinding
from coldaisle.control.schema import AuthorityStage, RegistryProvenance, SupervisorPolicyIdentity
from coldaisle.control_daemon import Config, build, build_parser, open_authority_runtime
from coldaisle.learned_channel import open_learned_channel
from coldaisle.learned_channel.mailbox import LearnedMailbox
from coldaisle.learned_channel.registry_watch import (
    RegistryWatch,
    read_startup_registry,
    snapshot_identity,
)
from coldaisle.metrics import MetricCatalog
from conftest import CONFIG_DIR
from test_authority_rollout import DEFAULT_CONFIG, PRODUCTION_REGISTRY
from test_authority_rollout import NOW_MS as AUTHORITY_NOW_MS
from test_authority_rollout import runtime as raised_runtime
from test_control_config import valid_documents, write_documents
from test_control_loop import METRICS_PATH, Harness
from test_fallback_controller import (
    TEST_MODEL_VERSION,
    assessment_for,
    gate_for,
    learned_proposal,
    select,
    synthetic_inference_id,
)
from test_learned_channel import (
    MPC_UID,
    RL_UID,
    RUN_ID,
    FakeGroups,
    FakePeer,
    RecordingSink,
    StaticSource,
    Worker,
    channel_document,
    envelope,
    failure_result,
    limited_harness,
    wait_until,
)
from test_learned_channel import no_file_groups as no_file_groups
from test_learned_channel import short_dir as short_dir
from test_learned_mpc import issue_attestation
from test_model_registry_lifecycle import (
    COMPATIBILITY,
    approval_record,
    make_registry,
    promote,
    register_and_validate,
)
from test_model_registry_lifecycle import production_registry as thermal_registry
from test_simulated_fan_backend import hardware_config

RL_MODEL_ID, RL_VERSION = "rack-rl", "0.2.0"


# ----------------------------------------------------------------- 足場


@pytest.fixture(scope="module")
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(METRICS_PATH)


def registry_with_both(root: Path) -> ModelRegistry:
    """`thermal_model`（rack-thermal 1.0.0）と `supervisor_policy`（rack-rl 0.2.0）を置く。"""
    registry = thermal_registry(root)
    issue_attestation(
        root,
        kind=ArtifactKind.SUPERVISOR_POLICY,
        capability=ArtifactCapability.SUPERVISOR_STRATEGY,
        model_id=RL_MODEL_ID,
        version=RL_VERSION,
    )
    return registry


def thermal_sha(registry: ModelRegistry) -> str:
    snapshot = registry.inspect()
    slot = snapshot.production[ArtifactKind.THERMAL_MODEL]
    return snapshot.artifacts[slot.active.key].metadata.sha256


def rollback_thermal(registry: ModelRegistry, to_version: str) -> None:
    revision = registry.inspect().revision
    registry.rollback(
        ArtifactKind.THERMAL_MODEL,
        COMPATIBILITY,
        approval=approval_record(to_version, revision, action=ApprovalAction.ROLLBACK),
        expected_revision=revision,
    )


def promote_next(registry: ModelRegistry, version: str) -> None:
    register_and_validate(registry, version)
    promote(registry, version)


def daemon_config(tmp_path: Path, **overrides: Any) -> Config:
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    config_dir = tmp_path / "control-config"
    config_dir.mkdir(exist_ok=True)
    write_documents(config_dir, documents)
    return Config(
        config_dir=config_dir,
        db=tmp_path / "control.db",
        metrics=METRICS_PATH,
        quality_rules=CONFIG_DIR / "quality.yaml",
        authority_root=tmp_path / "authority",
        **overrides,
    )


# ================================================================ 1. 1つの snapshot から作る


def test_every_expectation_comes_from_one_snapshot(tmp_path: Path) -> None:
    """provenance・Gate の期待値・RL の識別・frame の固定が、同じ `revision` から来る。"""
    registry = registry_with_both(tmp_path / "registry")
    snapshot = registry.inspect()

    binding = RegistryBinding.from_snapshot(snapshot)

    assert binding.provenance == snapshot.trace_provenance()
    assert binding.provenance.revision == snapshot.revision
    thermal = snapshot.production[ArtifactKind.THERMAL_MODEL].active
    rl = snapshot.production[ArtifactKind.SUPERVISOR_POLICY].active
    rl_sha = snapshot.artifacts[rl.key].metadata.sha256
    assert binding.expected_model_version == thermal.version == "1.0.0"
    assert binding.expected_artifact_sha256 == thermal_sha(registry)
    assert binding.expected_rl_identity == SupervisorPolicyIdentity(
        model_id=RL_MODEL_ID, version=RL_VERSION, artifact_sha256=rl_sha
    )
    assert binding.expected_artifacts == LearnedExpectedArtifacts(
        thermal_model=PinnedArtifact(
            model_id=thermal.model_id, version="1.0.0", artifact_sha256=thermal_sha(registry)
        ),
        supervisor_policy=PinnedArtifact(
            model_id=RL_MODEL_ID, version=RL_VERSION, artifact_sha256=rl_sha
        ),
    )
    # frame の固定は trace の provenance からも同じ値になる（loop が作る経路）
    assert (
        LearnedExpectedArtifacts.from_provenance(binding.provenance) == binding.expected_artifacts
    )


def test_a_registry_without_production_offers_nothing_to_learned(tmp_path: Path) -> None:
    """production が無ければ、Gate は番兵と None（どの提案も採らない。0077 §2.6）。"""
    registry = make_registry(tmp_path / "registry")
    register_and_validate(registry, "1.0.0")

    binding = RegistryBinding.from_snapshot(registry.inspect())

    assert binding.provenance.revision == registry.inspect().revision
    assert binding.expected_model_version == UNCONFIGURED_MODEL_VERSION
    assert binding.expected_artifact_sha256 is None
    assert binding.expected_rl_identity is None
    assert binding.expected_artifacts == LearnedExpectedArtifacts(
        thermal_model=None, supervisor_policy=None
    )
    # Gate の契約は変えない（空の期待版は拒まれる）。番兵なら組み立てられる
    gate_for(
        DEFAULT_CONFIG.policy,
        expected_model_version=binding.expected_model_version,
        expected_artifact_sha256=binding.expected_artifact_sha256,
    )


def test_the_unbound_binding_is_what_fand_used_before() -> None:
    binding = RegistryBinding.unbound()

    assert binding.provenance == RegistryProvenance.unbound()
    assert binding.expected_model_version == UNCONFIGURED_MODEL_VERSION == "unconfigured"
    assert binding.expected_artifact_sha256 is None
    assert binding.expected_rl_identity is None


def test_the_gate_accepts_only_the_pinned_production_artifact(tmp_path: Path) -> None:
    """Gate の期待値は registry の production。別の artifact・別の版の提案は Fallback。"""
    # 試験用の提案と assessment が名乗る版（TEST_MODEL_VERSION）で production を置く
    root = tmp_path / "registry"
    issue_attestation(root, version=TEST_MODEL_VERSION)
    registry = ModelRegistry(root, limits=_limits())
    binding = RegistryBinding.from_snapshot(registry.inspect())
    assert binding.expected_model_version == TEST_MODEL_VERSION
    gate = gate_for(
        DEFAULT_CONFIG.policy,
        expected_model_version=binding.expected_model_version,
        expected_artifact_sha256=binding.expected_artifact_sha256,
    )
    sha = thermal_sha(registry)

    def decide(*, version: str, artifact: str) -> str | None:
        # worker の提案は、その artifact で作った推論の識別子を持つ（識別子は artifact から導出）
        proposal = learned_proposal(
            version=version,
            inference_id=synthetic_inference_id(artifact_sha256=artifact, model_version=version),
        )
        status = LearnedControlStatus(
            proposal=proposal,
            received_at_mono_ms=0,
            source_snapshot_mono_ms=0,
            assessment=assessment_for(proposal, artifact_sha256=artifact),
            binding_authority_stage=AuthorityStage.FULL,
        )
        reason = select(gate, now=0, learned=status).fallback_reason
        return None if reason is None else reason.code

    # 照合を通った提案は、復帰の hold（安全でない側への復帰だけ待つ）までは進む
    assert decide(version=TEST_MODEL_VERSION, artifact=sha) in {None, "ml_recovery_hold"}
    assert decide(version=TEST_MODEL_VERSION, artifact="b" * 64) == "model_artifact_mismatch"
    assert decide(version="9.9.9", artifact=sha) == "model_version_mismatch"


# ================================================================ 2. 起動時の読み込み


def test_read_startup_registry_without_a_root_does_not_read(caplog) -> None:
    startup = read_startup_registry(None, limits_dir=CONFIG_DIR)

    assert startup.binding == RegistryBinding.unbound()
    assert startup.watch is None


def test_a_corrupt_registry_disables_learned_without_stopping(tmp_path: Path, caplog) -> None:
    root = tmp_path / "registry"
    thermal_registry(root)
    (root / "registry.json").write_text("{ not json", "utf-8")

    startup = read_startup_registry(root, limits_dir=CONFIG_DIR)

    assert startup.binding == RegistryBinding.unbound()
    assert startup.watch is None
    assert any(
        getattr(record, "fields", {}).get("reason") == "registry_unreadable"
        or "registry_unreadable" in record.getMessage()
        or "registry を読めない" in record.getMessage()
        for record in caplog.records
    )


def test_unreadable_limits_disable_learned_without_stopping(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    thermal_registry(root)

    startup = read_startup_registry(root, limits_dir=tmp_path / "no-such-config")

    assert startup.binding == RegistryBinding.unbound()
    assert startup.watch is None


def test_a_missing_root_reads_as_an_empty_registry(tmp_path: Path) -> None:
    """root が無いのは registry 自身の規則どおり「記録の無い registry」（revision 0）。"""
    startup = read_startup_registry(tmp_path / "absent", limits_dir=CONFIG_DIR)

    assert startup.binding.provenance.revision == 0
    assert startup.binding.expected_model_version == UNCONFIGURED_MODEL_VERSION
    assert startup.watch is not None
    assert startup.watch.check() == frozenset()


def test_fand_binds_the_gate_the_trace_the_coordinator_and_authority_to_the_registry(
    tmp_path: Path,
) -> None:
    registry = registry_with_both(tmp_path / "registry")
    snapshot = registry.inspect()
    expected = RegistryBinding.from_snapshot(snapshot)

    daemon = build(daemon_config(tmp_path, registry_root=tmp_path / "registry"))
    try:
        loop = daemon.loop
        assert loop._gate._expected_model_version == "1.0.0"
        assert loop._gate._expected_artifact_sha256 == thermal_sha(registry)
        assert loop._registry == snapshot.trace_provenance()
        assert loop._expected_artifacts == expected.expected_artifacts
        assert loop._supervisor is not None
        assert loop._supervisor._expected_rl_identity == expected.expected_rl_identity
        assert daemon.authority.loaded_artifact_sha256 == thermal_sha(registry)
        stats = daemon.run(max_ticks=3)
        assert stats.ticks == 3
    finally:
        daemon.close()


def test_fand_with_a_corrupt_registry_runs_with_learned_disabled(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    thermal_registry(root)
    (root / "registry.json").write_bytes(b"\x00broken")

    daemon = build(daemon_config(tmp_path, registry_root=root))
    try:
        loop = daemon.loop
        assert loop._gate._expected_model_version == UNCONFIGURED_MODEL_VERSION
        assert loop._gate._expected_artifact_sha256 is None
        assert loop._registry == RegistryProvenance.unbound()
        assert loop._supervisor is not None
        assert loop._supervisor._expected_rl_identity is None
        assert daemon.authority.loaded_artifact_sha256 is None
        assert daemon.run(max_ticks=3).ticks == 3
    finally:
        daemon.close()


def test_fand_without_a_registry_root_runs_as_before(tmp_path: Path) -> None:
    """`--registry-root` を省けばいまと同じ（後方互換）。"""
    daemon = build(daemon_config(tmp_path))
    try:
        loop = daemon.loop
        assert loop._gate._expected_model_version == UNCONFIGURED_MODEL_VERSION
        assert loop._gate._expected_artifact_sha256 is None
        assert loop._registry == RegistryProvenance.unbound()
        assert loop._supervisor is not None
        assert loop._supervisor._expected_rl_identity is None
        assert daemon.authority.loaded_artifact_sha256 is None
    finally:
        daemon.close()


def test_the_cli_takes_the_registry_root_and_defaults_to_none() -> None:
    parser = build_parser()
    assert parser.parse_args([]).registry_root is None
    assert parser.parse_args(["--registry-root", "var/model-registry"]).registry_root == Path(
        "var/model-registry"
    )


# ================================================================ 3. frame の固定


def test_the_frame_carries_the_pinned_artifacts_from_the_trace_provenance(
    tmp_path: Path, catalog: MetricCatalog
) -> None:
    registry = registry_with_both(tmp_path / "registry")
    provenance = registry.inspect().trace_provenance()
    sink = RecordingSink()
    harness = Harness(
        catalog,
        registry=provenance,
        learned_source=StaticSource(),
        learned_health=sink,
        learned_sink=sink,
    )
    harness.settle()

    frame = sink.frames[-1]
    assert frame.schema_version == LEARNED_FRAME_SCHEMA_VERSION == 3
    assert frame.expected_artifacts == LearnedExpectedArtifacts.from_provenance(provenance)
    assert frame.expected_artifacts.thermal_model is not None
    assert frame.expected_artifacts.thermal_model.artifact_sha256 == thermal_sha(registry)
    # 封筒に載る形で往復できる（path を含まない）
    assert LearnedFrame.model_validate_json(frame.model_dump_json()) == frame
    assert str(tmp_path) not in frame.model_dump_json()


def test_an_unbound_loop_sends_no_pinned_artifact(catalog: MetricCatalog) -> None:
    sink = RecordingSink()
    harness = Harness(
        catalog, learned_source=StaticSource(), learned_health=sink, learned_sink=sink
    )
    harness.settle()

    assert sink.frames[-1].expected_artifacts == LearnedExpectedArtifacts(
        thermal_model=None, supervisor_policy=None
    )


def test_a_v1_frame_is_not_accepted_as_v2(catalog: MetricCatalog) -> None:
    sink = RecordingSink()
    harness = Harness(
        catalog, learned_source=StaticSource(), learned_health=sink, learned_sink=sink
    )
    harness.settle()
    document = json.loads(sink.frames[-1].model_dump_json())
    document["schema_version"] = 1
    del document["expected_artifacts"]

    with pytest.raises(ValueError):
        LearnedFrame.model_validate_json(json.dumps(document))


# ================================================================ 4. 走行中の production の移動


def watch_for(root: Path) -> RegistryWatch:
    startup = read_startup_registry(root, limits_dir=CONFIG_DIR)
    assert startup.watch is not None
    return startup.watch


def test_an_untouched_registry_is_not_read_again(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "registry"
    registry_with_both(root)
    watch = watch_for(root)
    reads: list[int] = []
    original = ModelRegistry.inspect

    def counting(self: ModelRegistry) -> Any:
        reads.append(1)
        return original(self)

    monkeypatch.setattr(ModelRegistry, "inspect", counting)

    assert watch.check() == frozenset()
    assert watch.check() == frozenset()
    assert reads == []


@pytest.mark.parametrize("change", ["promotion", "rollback"])
def test_moving_the_thermal_production_closes_only_mpc(tmp_path: Path, change: str) -> None:
    root = tmp_path / "registry"
    registry = registry_with_both(root)
    if change == "rollback":
        promote_next(registry, "1.1.0")
    watch = watch_for(root)

    if change == "promotion":
        promote_next(registry, "1.2.0")
    else:
        rollback_thermal(registry, "1.0.0")

    assert watch.check() == frozenset({LearnedRole.MPC})


def test_an_unrelated_registry_write_closes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = registry_with_both(root)
    watch = watch_for(root)

    register_and_validate(registry, "1.3.0")  # 候補の登録は production を動かさない

    assert watch.check() == frozenset()


def test_moving_the_supervisor_production_closes_only_the_supervisor(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry_with_both(root)
    watch = watch_for(root)

    issue_attestation(
        root,
        kind=ArtifactKind.SUPERVISOR_POLICY,
        capability=ArtifactCapability.SUPERVISOR_STRATEGY,
        model_id=RL_MODEL_ID,
        version="0.3.0",
    )

    assert watch.check() == frozenset({LearnedRole.SUPERVISOR})


@pytest.mark.parametrize("breakage", ["corrupt", "removed", "unreadable"])
def test_a_registry_that_cannot_be_confirmed_closes_every_role(
    tmp_path: Path, breakage: str, monkeypatch
) -> None:
    root = tmp_path / "registry"
    registry_with_both(root)
    watch = watch_for(root)
    state = root / "registry.json"

    if breakage == "corrupt":
        state.write_text("{ broken", "utf-8")
    elif breakage == "removed":
        # 消えた registry は production の消失（固定した両方が production でなくなる）
        state.unlink()
    else:

        def denied(path: Path) -> Any:
            raise PermissionError(13, "denied")

        monkeypatch.setattr(watch, "_identity_of", denied)

    assert watch.check() == frozenset(LearnedRole)


def test_snapshot_identity_does_not_follow_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text("{}", "utf-8")
    link = tmp_path / "registry.json"
    link.symlink_to(target)

    assert snapshot_identity(link) == (
        os.lstat(link).st_dev,
        os.lstat(link).st_ino,
        os.lstat(link).st_mtime_ns,
        os.lstat(link).st_size,
    )
    assert snapshot_identity(tmp_path / "absent.json") is None


# ---------------------------------------------------------------- 受付スレッドでの閉鎖


class Opened:
    def __init__(self, entry: Any, peer: FakePeer) -> None:
        self.entry = entry
        self.mailbox: LearnedMailbox = entry.mailbox
        self.server = entry.server
        self.peer = peer

    def connect(self, role: LearnedRole, uid: int) -> Worker:
        self.peer.uid = uid
        worker = Worker(self.server.socket_path(role))
        hello = worker.receive()
        assert hello is not None and hello["body"]["kind"] == "hello"
        return worker


@pytest.fixture
def watched_channel(
    short_dir: Path,
    no_file_groups: None,
) -> Iterator[Callable[[Path], Opened]]:
    opened: list[Any] = []

    def open_(registry_root: Path) -> Opened:
        config = short_dir / "learned-channel.yaml"
        # idle は試験の間に切れない長さにする（閉鎖の判定を idle と混ぜない）
        config.write_text(
            yaml.safe_dump(channel_document(short_dir / "run", heartbeat_ms=1_000, idle_ms=30_000)),
            "utf-8",
        )
        startup = read_startup_registry(registry_root, limits_dir=CONFIG_DIR)
        peer = FakePeer()
        entry = open_learned_channel(
            config,
            run_id=RUN_ID,
            tick_deadline_ms=500,
            monotonic=SystemMonotonicClock(),
            server_uid=os.geteuid(),
            groups=FakeGroups(),
            peer=peer,
            registry_watch=startup.watch,
            registry_check_interval_ms=20,
        )
        assert entry is not None
        opened.append(entry)
        return Opened(entry, peer)

    yield open_
    for entry in opened:
        entry.stop()


def test_a_promotion_closes_the_mpc_role_until_restart(tmp_path: Path, watched_channel) -> None:
    """移動の後は、古い artifact に一致する結果が届いても受け渡し口へ置かない（0077 §2.6）。"""
    root = tmp_path / "registry"
    registry = registry_with_both(root)
    opened = watched_channel(root)
    mpc = opened.connect(LearnedRole.MPC, MPC_UID)
    rl = opened.connect(LearnedRole.SUPERVISOR, RL_UID)
    try:
        mpc.send(envelope(failure_result("before")))
        assert wait_until(lambda: opened.mailbox.poll() is not None)

        promote_next(registry, "1.1.0")

        assert wait_until(
            lambda: opened.mailbox.state(LearnedRole.MPC) is LearnedChannelState.REGISTRY_SUPERSEDED
        )
        assert opened.mailbox.poll() is None
        # 止まり損ねた worker が古い artifact の結果を送り続けても置かれない
        mpc.send(envelope(failure_result("after")))
        assert wait_until(lambda: opened.server.dropped.get("mpc.registry_superseded", 0) >= 1)
        assert opened.mailbox.poll() is None
        # もう一方の役割は閉じない
        assert opened.mailbox.state(LearnedRole.SUPERVISOR) is LearnedChannelState.CONNECTED

        # registry を元の版へ戻しても、再起動まで閉じたまま
        rollback_thermal(registry, "1.0.0")
        assert thermal_sha(registry) == opened_pin_sha(root)
        mpc.send(envelope(failure_result("restored")))
        assert wait_until(lambda: opened.server.dropped.get("mpc.registry_superseded", 0) >= 2)
        assert opened.mailbox.state(LearnedRole.MPC) is LearnedChannelState.REGISTRY_SUPERSEDED
        assert opened.mailbox.poll() is None
    finally:
        mpc.close()
        rl.close()
    # 切断の知らせでも閉鎖は解けない
    assert wait_until(
        lambda: (
            opened.mailbox.state(LearnedRole.SUPERVISOR) is LearnedChannelState.WORKER_DISCONNECTED
        )
    )
    assert opened.mailbox.state(LearnedRole.MPC) is LearnedChannelState.REGISTRY_SUPERSEDED


def opened_pin_sha(root: Path) -> str:
    """戻した後の production（1.0.0）の sha。起動時の固定と同じ値になる。"""
    return thermal_sha(ModelRegistry(root, limits=_limits()))


def _limits() -> Any:
    from coldaisle.control import load_model_registry_limits

    return load_model_registry_limits(CONFIG_DIR)


def test_a_corrupted_registry_closes_both_roles(tmp_path: Path, watched_channel) -> None:
    root = tmp_path / "registry"
    registry_with_both(root)
    opened = watched_channel(root)

    (root / "registry.json").write_text("{ broken", "utf-8")

    assert wait_until(
        lambda: all(
            opened.mailbox.state(role) is LearnedChannelState.REGISTRY_SUPERSEDED
            for role in LearnedRole
        )
    )


def test_an_unchanged_registry_keeps_the_channel_open(tmp_path: Path, watched_channel) -> None:
    root = tmp_path / "registry"
    registry_with_both(root)
    opened = watched_channel(root)
    worker = opened.connect(LearnedRole.MPC, MPC_UID)
    try:
        worker.send(envelope(failure_result()))
        assert wait_until(lambda: opened.mailbox.poll() is not None)
        # 監視は何周も回るが、production が動かなければ閉じない
        assert not wait_until(
            lambda: (
                opened.mailbox.state(LearnedRole.MPC) is LearnedChannelState.REGISTRY_SUPERSEDED
            ),
            timeout_s=0.3,
        )
        assert opened.mailbox.poll() is not None
    finally:
        worker.close()


def test_the_loop_reports_registry_superseded_and_keeps_the_trace_registry(
    tmp_path: Path, catalog: MetricCatalog
) -> None:
    """`learned_proposal_unavailable`（detail `registry_superseded`）。provenance は不変。"""
    registry = registry_with_both(tmp_path / "registry")
    provenance = registry.inspect().trace_provenance()
    mailbox = LearnedMailbox()
    mailbox.attach_receiver(_AliveThread())
    mailbox.connected(LearnedRole.MPC)
    harness = limited_harness(
        catalog,
        registry=provenance,
        learned_source=mailbox,
        learned_health=mailbox,
        learned_sink=RecordingSink(),
    )
    harness.settle()

    mailbox.supersede(LearnedRole.MPC)
    mailbox.place_mpc(failure_result("late"))  # 閉じた役割には置かれない
    result = harness.tick()

    reason = result.tick.state.fallback_reason
    assert reason is not None
    assert (reason.code, reason.detail) == ("learned_proposal_unavailable", "registry_superseded")
    assert result.tick.registry == provenance
    # 接続の知らせでも戻らない
    mailbox.connected(LearnedRole.MPC)
    assert mailbox.state(LearnedRole.MPC) is LearnedChannelState.REGISTRY_SUPERSEDED


class _AliveThread:
    """受付スレッドが生きているふり（loop の側だけを試す）。"""

    def is_alive(self) -> bool:
        return True


# ================================================================ 5. authority の束縛（0089）


def _registry_root_of(registry: ModelRegistry) -> Path:
    return Path(registry._root)


def test_authority_is_effective_only_for_the_registry_production_artifact(tmp_path: Path) -> None:
    """起動時の production で上げた journal は有効、registry を読まなければ Baseline。"""
    raised_runtime(tmp_path, stage=AuthorityStage.LIMITED)
    startup = read_startup_registry(_registry_root_of(PRODUCTION_REGISTRY), limits_dir=CONFIG_DIR)
    assert startup.binding.expected_artifact_sha256 is not None

    bound = open_authority_runtime(
        tmp_path / "authority",
        DEFAULT_CONFIG,
        clock=SimulatedClock(AUTHORITY_NOW_MS),
        loaded_artifact_sha256=startup.binding.expected_artifact_sha256,
    )
    unbound = open_authority_runtime(
        tmp_path / "authority",
        DEFAULT_CONFIG,
        clock=SimulatedClock(AUTHORITY_NOW_MS),
        loaded_artifact_sha256=RegistryBinding.unbound().expected_artifact_sha256,
    )

    assert bound.journal.stage is AuthorityStage.LIMITED
    assert bound.current_stage() is AuthorityStage.LIMITED
    assert unbound.current_stage() is AuthorityStage.SHADOW


def test_authority_for_another_production_artifact_stays_at_baseline(tmp_path: Path) -> None:
    """journal を上げた artifact と違う production で起動した fand は Baseline（0089 §2.2）。"""
    raised_runtime(tmp_path, stage=AuthorityStage.LIMITED)
    other = thermal_registry(tmp_path / "other-registry")
    startup = read_startup_registry(tmp_path / "other-registry", limits_dir=CONFIG_DIR)
    assert startup.binding.expected_artifact_sha256 == thermal_sha(other)

    runtime = open_authority_runtime(
        tmp_path / "authority",
        DEFAULT_CONFIG,
        clock=SimulatedClock(AUTHORITY_NOW_MS),
        loaded_artifact_sha256=startup.binding.expected_artifact_sha256,
    )

    assert runtime.journal.stage is AuthorityStage.LIMITED
    assert runtime.current_stage() is AuthorityStage.SHADOW


# ================================================================ 6. 構造


def test_fand_does_not_write_the_registry() -> None:
    """fand の registry の経路は読むだけ（#104 と #92 の境界）。書く API を呼ばない。"""
    sources = [
        Path("src/coldaisle/control_daemon.py"),
        Path("src/coldaisle/control/registry_binding.py"),
        *sorted(Path("src/coldaisle/learned_channel").rglob("*.py")),
    ]
    writers = (
        ".register_candidate(",
        ".mark_validated(",
        ".promote(",
        ".rollback(",
        ".retire(",
        ".pinned(",
        ".load_production(",
        ".load_version(",
        ".verify(",
    )
    hits = [
        f"{path}: {writer}"
        for path in sources
        for writer in writers
        if writer in path.read_text(encoding="utf-8")
    ]
    assert hits == []
