"""RL Supervisor worker（#89 / 決定記録 0077 段階 4、§2.3 / §2.5 / §2.6 / §2.8）。実機不要。

本物の Model Registry に登録した試験用の policy artifact、loop が作った本物の frame、偽 fand
（`socketpair`）と本物の受付スレッド（実際の `SOCK_SEQPACKET`）で確かめる。

1. worker が出せるのは `SupervisorOutput`（戦略・重み・target band・regime）と artifact の識別だけ。
   Demand・束縛の用途（`origin`）を運ぶ欄は型に無い（AGENTS.md ルール2・5 / 0077 §2.8）
2. 出力は `supervisor.output_bounds`（RulePolicy と同じ枠）を越えない。枠の違う policy は束縛しない
3. 推論の前に毎周期、固定された policy が production かを照合し、違えば別の `run_id` まで止まる
   （0077 §2.6）。設定の食い違い・束縛の失敗・入力の欠けでは出力を作らない
4. fand は RL の出力を役割の枠にだけ置き、切断・閉鎖・受付の死でその tick から空にする
5. loop は経路の出力を `unverified` のまま Coordinator へ渡す。shadow slot にだけ届き、
   active slot は RulePolicy へ戻る。RL の出力が届いても effective demand は変わらない
   （Guard / Safety を迂回しない）
"""

from __future__ import annotations

import ast
import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock, SystemMonotonicClock
from coldaisle.control.config import ControlConfig
from coldaisle.control.learned_handoff import (
    LearnedChannelState,
    LearnedExpectedArtifacts,
    LearnedFrame,
    LearnedRole,
    PinnedArtifact,
)
from coldaisle.control.model_registry import ModelRegistry, VerifiedArtifact
from coldaisle.control.registry_binding import RegistryBinding
from coldaisle.control.schema import (
    AuthorityStage,
    SafetyState,
    SupervisorPolicyIdentity,
    SupervisorPolicyKind,
    WorkloadRegime,
    Zone,
)
from coldaisle.control.supervisor.policy import DeliveredSupervisorOutput
from coldaisle.learned_channel.messages import (
    FrameBody,
    HelloBody,
    OutboundEnvelope,
    encode_outbound,
    parse_inbound,
)
from coldaisle.learned_worker.cli import SupervisorWorker
from coldaisle.learned_worker.client import WorkerChannel
from coldaisle.learned_worker.registry import RegistryProductionCheck
from coldaisle.learned_worker.supervisor import (
    PolicyLoadFailure,
    PolicySource,
    RegistryPolicySource,
    SupervisorWorkerCore,
    SupervisorWorkerInputs,
)
from coldaisle.metrics import MetricCatalog
from test_control_config import supervisor_config
from test_control_loop import METRICS_PATH, Harness, control_config
from test_learned_channel import (
    MPC_UID,
    RL_UID,
    RecordingSink,
    Running,
    StaticSource,
    channel,
    connect,
    envelope,
    failure_result,
    no_file_groups,
    short_dir,
    wait_until,
)
from test_learned_mpc import REGISTRY_LIMITS
from test_rl_supervisor_policy import artifact, register_policy

__all__ = ["channel", "no_file_groups", "short_dir"]

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
RUN_ID = "0123456789abcdef0123456789abcdef"
OTHER_RUN_ID = "fedcba9876543210fedcba9876543210"
RL_VERSION = "0.1.0"
"""試験用 policy artifact（`test_rl_supervisor_policy.artifact`）の版。"""


def rl_config(**supervisor: Any) -> ControlConfig:
    """RL を shadow に置いた Control Config（`rl_version` は試験用 artifact の版）。"""
    return control_config(
        policy={"supervisor": {**supervisor_config(), "rl_version": RL_VERSION, **supervisor}}
    )


CONFIG = rl_config()
BOUNDS = CONFIG.policy.supervisor.output_bounds


@pytest.fixture(scope="module")
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(METRICS_PATH)


class Registry:
    """promote 済みの `supervisor_policy` を1つ持つ本物の registry。"""

    def __init__(self, root: Path, *, bounds: Any = BOUNDS) -> None:
        self.root = root
        register_policy(root, artifact(bounds), promoted=True)
        self.registry = ModelRegistry(root, limits=REGISTRY_LIMITS)
        self.binding = RegistryBinding.from_snapshot(self.registry.inspect())
        pins = self.binding.expected_artifacts.supervisor_policy
        assert pins is not None and self.binding.expected_rl_identity is not None
        self.pins: PinnedArtifact = pins
        self.identity: SupervisorPolicyIdentity = self.binding.expected_rl_identity

    def core(
        self,
        *,
        config: ControlConfig = CONFIG,
        policies: PolicySource | None = None,
        production: Any = None,
    ) -> SupervisorWorkerCore:
        return SupervisorWorkerCore(
            SupervisorWorkerInputs(control=config),
            policies=policies or RegistryPolicySource(self.registry),
            production=production
            or RegistryProductionCheck(self.registry, self.root, role=LearnedRole.SUPERVISOR),
            clock=SimulatedClock(1_800_000_000_000),
        )


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "registry")


def loop_frames(
    catalog: MetricCatalog, registry: Registry, *, ticks: int = 6, config: ControlConfig = CONFIG
) -> list[LearnedFrame]:
    """本物の loop が作った frame（その registry の production を固定している）。"""
    sink = RecordingSink()
    harness = Harness(
        catalog,
        config=config,
        registry=registry.binding.provenance,
        learned_source=StaticSource(),
        learned_health=sink,
        learned_sink=sink,
    )
    harness.run(ticks)
    assert sink.frames and all(frame.workload is not None for frame in sink.frames)
    return sink.frames


def feed(
    core: SupervisorWorkerCore, *frames: LearnedFrame, run_id: str = RUN_ID
) -> DeliveredSupervisorOutput | None:
    for item in frames:
        core.receive(run_id, item)
    return core.step()


# ================================================= 1. 経路の型は戦略しか運べない（0077 §2.8）


def test_a_healthy_frame_yields_the_pinned_policy_output(
    catalog: MetricCatalog, registry: Registry
) -> None:
    frames = loop_frames(catalog, registry)
    delivered = feed(registry.core(), *frames)

    assert delivered is not None
    latest = frames[-1]
    assert latest.workload is not None
    assert delivered.identity == registry.identity
    output = delivered.output
    assert output.policy is SupervisorPolicyKind.RL and output.version == RL_VERSION
    assert (output.tick_id, output.ts_ms) == (latest.snapshot.tick_id, latest.snapshot.ts_ms)
    assert output.regime is latest.workload.regime
    assert output.regime_confidence == latest.workload.confidence


def test_the_delivered_type_carries_no_demand_and_no_origin() -> None:
    """Demand・PWM・用途を表す欄が型に無い（AGENTS.md ルール2・5 / 0077 §2.8）。"""
    assert set(DeliveredSupervisorOutput.model_fields) == {"output", "identity"}
    from coldaisle.control.schema import SupervisorOutput

    assert not any(
        word in name
        for name in SupervisorOutput.model_fields
        for word in ("demand", "pwm", "rpm", "authority", "origin", "mode")
    )


def _delivered_document(registry: Registry, catalog: MetricCatalog) -> dict[str, Any]:
    delivered = feed(registry.core(), *loop_frames(catalog, registry))
    assert delivered is not None
    document: dict[str, Any] = json.loads(delivered.model_dump_json())
    return document


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(origin="active_binding"),
        lambda d: d.update(demand={"front": 1.0, "rear": 1.0, "top": 1.0}),
        lambda d: d["output"].update(requested_demand=0.0),
        lambda d: d["output"].update(policy="rule_policy"),
        lambda d: d["identity"].update(version="9.9.9"),
    ],
    ids=["origin", "demand", "nested_demand", "rule_policy", "other_version"],
)
def test_a_delivered_output_that_claims_more_is_refused(
    catalog: MetricCatalog, registry: Registry, mutate: Any
) -> None:
    document = _delivered_document(registry, catalog)
    mutate(document)
    with pytest.raises(ValidationError):
        DeliveredSupervisorOutput.model_validate_json(json.dumps(document))
    # 経路の封筒としても通らない（fand の受付が捨てる）
    with pytest.raises(ValueError):
        parse_inbound(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": RUN_ID,
                    "role": "supervisor",
                    "body": {"kind": "supervisor_result", "result": document},
                }
            ).encode()
        )


# ======================================== 2. RulePolicy と同じ枠を越えない（output_bounds）


def test_every_regime_stays_inside_the_rule_policy_bounds(
    catalog: MetricCatalog, registry: Registry
) -> None:
    frames = loop_frames(catalog, registry)
    latest = frames[-1]
    assert latest.workload is not None
    for regime in WorkloadRegime:
        frame = latest.model_copy(
            update={"workload": latest.workload.model_copy(update={"regime": regime})}
        )
        delivered = feed(registry.core(), frame)
        assert delivered is not None and delivered.output.regime is regime
        BOUNDS.validate_context(
            strategy=delivered.output.strategy,
            weights=delivered.output.weights,
            target_band=delivered.output.target_band,
        )


def test_a_policy_trained_on_other_bounds_is_never_bound(
    catalog: MetricCatalog, tmp_path: Path
) -> None:
    """学習時の action 空間が runtime の `output_bounds` と違えば束縛しない（丸めない）。"""
    narrow = BOUNDS.model_copy(update={"strategies": ("balanced",)})
    other = Registry(tmp_path / "registry", bounds=narrow)
    core = other.core()
    frames = loop_frames(catalog, other)
    assert feed(core, *frames) is None
    assert not core.bound
    assert feed(core, frames[-1]) is None


# =============================== 3. production の照合・停止・出力を作らない周期（0077 §2.6）


class CountingProduction:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0

    def is_production(self, pinned: PinnedArtifact) -> bool:
        self.calls += 1
        result: bool = self.inner.is_production(pinned)
        return result


class CountingPolicies:
    """読み込みを数え、``failures`` を先頭から順に返してから ``inner`` へ渡す。"""

    def __init__(
        self, inner: PolicySource | None = None, *, failures: list[PolicyLoadFailure] | None = None
    ) -> None:
        self.inner = inner
        self.failures = list(failures or [])
        self.loads = 0

    def load(self, pinned: PinnedArtifact) -> VerifiedArtifact | PolicyLoadFailure:
        self.loads += 1
        if self.failures:
            return self.failures.pop(0)
        assert self.inner is not None
        return self.inner.load(pinned)


class FlakyRegistry:
    """``load_version`` の最初の ``failures`` 回だけ例外を出す registry（一時的に読めない）。"""

    def __init__(self, inner: ModelRegistry, failures: int) -> None:
        self.inner = inner
        self.failures = failures

    def load_version(self, *args: Any, **kwargs: Any) -> Any:
        if self.failures > 0:
            self.failures -= 1
            raise OSError("一時的に読めない")
        return self.inner.load_version(*args, **kwargs)


def test_production_is_checked_every_period_before_inference(
    catalog: MetricCatalog, registry: Registry
) -> None:
    production = CountingProduction(
        RegistryProductionCheck(registry.registry, registry.root, role=LearnedRole.SUPERVISOR)
    )
    core = registry.core(production=production)
    frames = loop_frames(catalog, registry)
    for count, frame in enumerate(frames[-3:], start=1):
        assert feed(core, frame) is not None
        assert production.calls == count


def test_a_promotion_stops_the_worker_until_the_next_run(
    catalog: MetricCatalog, registry: Registry
) -> None:
    frames = loop_frames(catalog, registry)
    core = registry.core()
    assert feed(core, *frames[:-1]) is not None

    register_policy(registry.root, artifact(BOUNDS, model_version="0.2.0"), promoted=True)

    assert feed(core, frames[-1]) is None
    assert core.stopped and not core.bound
    assert feed(core, frames[-1]) is None


def test_a_broken_registry_stops_and_a_restored_one_does_not_resume_the_run(
    catalog: MetricCatalog, registry: Registry
) -> None:
    frames = loop_frames(catalog, registry)
    core = registry.core()
    state = registry.root / "registry.json"
    original = state.read_bytes()
    assert feed(core, *frames[:-1]) is not None

    state.write_text("{", "utf-8")
    assert feed(core, frames[-1]) is None and core.stopped

    state.write_bytes(original)
    assert feed(core, frames[-1]) is None, "同じ run_id の間は自分では再開しない"
    # fand の再起動（別の run_id）の frame で再開する
    resumed = feed(core, frames[-1], run_id=OTHER_RUN_ID)
    assert resumed is not None and resumed.identity == registry.identity


def test_a_config_mismatch_sends_nothing_until_it_matches(
    catalog: MetricCatalog, registry: Registry
) -> None:
    frames = loop_frames(catalog, registry)
    other = rl_config(valid_ms=3_000)
    mismatched = frames[-1].model_copy(update={"config": other.runtime_digest()})
    core = registry.core()
    assert feed(core, mismatched) is None
    assert feed(core, mismatched) is None
    assert not core.stopped
    assert feed(core, frames[-1]) is not None


def test_a_run_without_a_pinned_policy_sends_nothing(
    catalog: MetricCatalog, registry: Registry
) -> None:
    latest = loop_frames(catalog, registry)[-1]
    unpinned = latest.model_copy(
        update={
            "expected_artifacts": LearnedExpectedArtifacts(
                thermal_model=None, supervisor_policy=None
            )
        }
    )
    assert feed(registry.core(), unpinned) is None


def test_a_config_without_rl_sends_nothing(catalog: MetricCatalog, registry: Registry) -> None:
    config = rl_config(shadow_policy=None, rl_version=None)
    frames = loop_frames(catalog, registry, config=config)
    assert feed(registry.core(config=config), *frames) is None


def test_a_binding_failure_is_not_retried_until_the_next_run(
    catalog: MetricCatalog, registry: Registry
) -> None:
    """読めたうえで使えない policy は別の run_id まで読み直さない（0114 §2.1 の2）。"""
    unusable = PolicyLoadFailure(detail="checksum が合わない", transient=False)
    policies = CountingPolicies(
        RegistryPolicySource(registry.registry), failures=[unusable, unusable]
    )
    core = registry.core(policies=policies)
    frames = loop_frames(catalog, registry)
    assert feed(core, frames[-2]) is None
    assert feed(core, frames[-1]) is None
    assert policies.loads == 1
    assert feed(core, frames[-1], run_id=OTHER_RUN_ID) is None
    assert policies.loads == 2
    assert not core.stopped


def test_a_transient_read_failure_is_retried_every_period_and_recovers(
    catalog: MetricCatalog, registry: Registry
) -> None:
    """registry を**読めない**一時的な失敗だけは周期ごとに読み直す（0114 §2.1 の2）。"""
    flaky = FlakyRegistry(registry.registry, failures=2)
    core = registry.core(policies=RegistryPolicySource(flaky))  # type: ignore[arg-type]
    frames = loop_frames(catalog, registry)
    assert feed(core, frames[-3]) is None
    assert feed(core, frames[-2]) is None
    assert not core.bound and not core.stopped
    delivered = feed(core, frames[-1])
    assert delivered is not None and delivered.identity == registry.identity


def test_load_failures_are_classified_as_unreadable_or_unusable(registry: Registry) -> None:
    source = RegistryPolicySource(FlakyRegistry(registry.registry, failures=1))  # type: ignore[arg-type]
    raised = source.load(registry.pins)
    assert isinstance(raised, PolicyLoadFailure) and raised.transient

    real = RegistryPolicySource(registry.registry)
    unknown = real.load(registry.pins.model_copy(update={"version": "9.9.9"}))
    assert isinstance(unknown, PolicyLoadFailure) and not unknown.transient
    other_sha = real.load(registry.pins.model_copy(update={"artifact_sha256": "e" * 64}))
    assert isinstance(other_sha, PolicyLoadFailure) and not other_sha.transient

    # registry の snapshot を読めない（壊れた registry.json）は読み直す側
    (registry.root / "registry.json").write_text("{", "utf-8")
    broken = real.load(registry.pins)
    assert isinstance(broken, PolicyLoadFailure) and broken.transient


def test_an_unreadable_artifact_is_retried_but_a_moved_production_still_stops(
    catalog: MetricCatalog, registry: Registry
) -> None:
    """読み直すのは読み込みだけ。production でなくなれば従来どおり別の run_id まで止まる。"""
    transient = PolicyLoadFailure(detail="artifact bytes を読み取れない", transient=True)
    policies = CountingPolicies(RegistryPolicySource(registry.registry), failures=[transient])
    core = registry.core(policies=policies)
    frames = loop_frames(catalog, registry)
    assert feed(core, frames[-3]) is None
    assert feed(core, frames[-2]) is not None
    assert policies.loads == 2

    register_policy(registry.root, artifact(BOUNDS, model_version="0.2.0"), promoted=True)
    assert feed(core, frames[-1]) is None and core.stopped
    assert feed(core, frames[-1]) is None


def test_another_sha_under_the_pinned_version_is_not_bound(
    catalog: MetricCatalog, registry: Registry
) -> None:
    """同じ版を名乗る別の bytes を使わない（frame の固定は3つ組。0077 §2.6）。"""
    latest = loop_frames(catalog, registry)[-1]
    forged = registry.pins.model_copy(update={"artifact_sha256": "e" * 64})
    loaded = RegistryPolicySource(registry.registry).load(forged)
    assert isinstance(loaded, PolicyLoadFailure) and not loaded.transient
    assert latest.expected_artifacts.supervisor_policy == registry.pins


def test_a_frame_without_a_consistent_workload_sends_nothing(
    catalog: MetricCatalog, registry: Registry
) -> None:
    latest = loop_frames(catalog, registry)[-1]
    assert latest.workload is not None
    core = registry.core()
    assert feed(core, latest.model_copy(update={"workload": None})) is None
    misaligned = latest.workload.model_copy(update={"as_of_tick_id": latest.snapshot.tick_id - 1})
    assert feed(core, latest.model_copy(update={"workload": misaligned})) is None
    assert feed(core, latest) is not None


def test_the_shadow_binding_does_not_follow_the_authority_stage(
    catalog: MetricCatalog, registry: Registry
) -> None:
    """RL の束縛は shadow 用だけ（`for_active` は閉じたまま。0061 §2.4 / 0077 §2.10 段階 4）。"""
    latest = loop_frames(catalog, registry)[-1]
    core = registry.core()
    for stage in (AuthorityStage.SHADOW, AuthorityStage.LIMITED, AuthorityStage.FULL):
        delivered = feed(core, latest.model_copy(update={"authority_stage": stage}))
        assert delivered is not None and delivered.identity == registry.identity


# ================================================================ ソケット（偽 fand）


class FakeFand:
    """`socketpair` の片側。RL の役割の hello と frame を送り、出力と heartbeat を受け取る。"""

    def __init__(self) -> None:
        self.fand, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.fand.settimeout(3.0)
        self.channel = WorkerChannel(worker, max_message_bytes=1 << 20, role=LearnedRole.SUPERVISOR)

    def send(self, body: Any, *, role: LearnedRole = LearnedRole.SUPERVISOR) -> None:
        self.fand.send(encode_outbound(OutboundEnvelope(run_id=RUN_ID, role=role, body=body)))

    def receive(self) -> Any:
        return parse_inbound(self.fand.recv(1 << 20))


def test_the_worker_sends_heartbeats_and_outputs_over_the_socket(
    catalog: MetricCatalog, registry: Registry
) -> None:
    fake = FakeFand()
    fake.send(HelloBody(heartbeat_interval_ms=20))
    frames = loop_frames(catalog, registry)
    # MPC の役割の封筒は捨てる（役割はソケットで決まる）
    fake.send(FrameBody(frame=frames[-2]), role=LearnedRole.MPC)
    fake.send(FrameBody(frame=frames[-1]))
    worker = SupervisorWorker(
        registry.core(),
        fake.channel,
        period_ms=50,
        hello_timeout_ms=3_000,
        monotonic=SystemMonotonicClock(),
    )
    thread = threading.Thread(target=worker.run, kwargs={"max_periods": 6}, daemon=True)
    thread.start()
    kinds: list[str] = []
    outputs: list[DeliveredSupervisorOutput] = []
    while not outputs or "heartbeat" not in kinds:
        received = fake.receive()
        assert received.run_id == RUN_ID and received.role is LearnedRole.SUPERVISOR
        kinds.append(received.body.kind)
        if received.body.kind == "supervisor_result":
            outputs.append(received.body.result)
    thread.join(timeout=3.0)
    assert set(kinds) <= {"heartbeat", "supervisor_result"}
    assert outputs[0].identity == registry.identity
    assert outputs[0].output.tick_id == frames[-1].snapshot.tick_id
    assert fake.channel.dropped == {"role_mismatch": 1}


def test_a_supervisor_channel_cannot_send_an_mpc_result() -> None:
    fake = FakeFand()
    with pytest.raises(ValueError):
        fake.channel.send_result(RUN_ID, failure_result())


# ========================================== 4. fand の受付（本物の `SOCK_SEQPACKET`）


def supervisor_envelope(delivered: DeliveredSupervisorOutput | dict[str, Any]) -> dict[str, Any]:
    result = delivered if isinstance(delivered, dict) else json.loads(delivered.model_dump_json())
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "role": "supervisor",
        "body": {"kind": "supervisor_result", "result": result},
    }


@pytest.fixture
def delivered(catalog: MetricCatalog, registry: Registry) -> DeliveredSupervisorOutput:
    result = feed(registry.core(), *loop_frames(catalog, registry))
    assert result is not None
    return result


def test_an_output_is_placed_only_on_the_supervisor_slot(
    channel: Any, delivered: DeliveredSupervisorOutput
) -> None:
    running: Running = channel()
    worker, _ = connect(running, LearnedRole.SUPERVISOR, RL_UID)
    try:
        worker.send(supervisor_envelope(delivered))
        assert wait_until(lambda: running.mailbox.supervisor_source.poll() == delivered)
        assert running.mailbox.poll() is None, "MPC の枠には置かない"
    finally:
        worker.close()


def test_an_output_on_the_mpc_socket_is_dropped(
    channel: Any, delivered: DeliveredSupervisorOutput
) -> None:
    running: Running = channel()
    worker, _ = connect(running, LearnedRole.MPC, MPC_UID)
    try:
        document = supervisor_envelope(delivered) | {"role": "mpc"}
        worker.send(document)
        assert wait_until(lambda: running.server.dropped.get("mpc.unsupported_body") == 1)
        assert running.mailbox.supervisor_source.poll() is None
        assert running.mailbox.poll() is None
    finally:
        worker.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(origin="active_binding"),
        lambda d: d["output"].update(requested_demand=0.0),
    ],
    ids=["origin", "demand"],
)
def test_an_output_that_claims_more_is_dropped_without_touching_the_slot(
    channel: Any, delivered: DeliveredSupervisorOutput, mutate: Any
) -> None:
    running: Running = channel()
    worker, _ = connect(running, LearnedRole.SUPERVISOR, RL_UID)
    try:
        worker.send(supervisor_envelope(delivered))
        assert wait_until(lambda: running.mailbox.supervisor_source.poll() == delivered)
        document = json.loads(delivered.model_dump_json())
        mutate(document)
        worker.send(supervisor_envelope(document))
        assert wait_until(lambda: running.server.dropped.get("supervisor.invalid") == 1)
        assert running.mailbox.supervisor_source.poll() == delivered
    finally:
        worker.close()


def test_a_disconnect_empties_the_supervisor_slot_at_once(
    channel: Any, delivered: DeliveredSupervisorOutput
) -> None:
    """直前の RL 出力を `supervisor.valid_ms` まで残さない（0077 §2.5 の RL の行・§5 の決定 4）。"""
    running: Running = channel()
    worker, _ = connect(running, LearnedRole.SUPERVISOR, RL_UID)
    worker.send(supervisor_envelope(delivered))
    assert wait_until(lambda: running.mailbox.supervisor_source.poll() == delivered)
    worker.close()
    assert wait_until(
        lambda: (
            running.mailbox.state(LearnedRole.SUPERVISOR) is LearnedChannelState.WORKER_DISCONNECTED
        )
    )
    assert running.mailbox.supervisor_source.poll() is None


def test_a_superseded_or_dead_channel_holds_no_supervisor_output(
    delivered: DeliveredSupervisorOutput,
) -> None:
    from coldaisle.learned_channel.mailbox import LearnedMailbox

    mailbox = LearnedMailbox()
    mailbox.mark_stopping()
    mailbox.connected(LearnedRole.SUPERVISOR)
    mailbox.place_supervisor(delivered)
    assert mailbox.supervisor_source.poll() == delivered

    mailbox.supersede(LearnedRole.SUPERVISOR)
    assert mailbox.supervisor_source.poll() is None
    mailbox.place_supervisor(delivered)
    assert mailbox.supervisor_source.poll() is None, "閉じた役割には置かない"

    other = LearnedMailbox()
    other.mark_stopping()
    other.connected(LearnedRole.SUPERVISOR)
    other.place_supervisor(delivered)
    other.receiver_failed()
    assert other.state(LearnedRole.SUPERVISOR) is LearnedChannelState.CHANNEL_DEAD
    assert other.supervisor_source.poll() is None
    other.close()
    mailbox.close()


def test_a_closed_mpc_role_does_not_touch_the_supervisor_slot(
    delivered: DeliveredSupervisorOutput,
) -> None:
    from coldaisle.learned_channel.mailbox import LearnedMailbox

    mailbox = LearnedMailbox()
    mailbox.mark_stopping()
    mailbox.connected(LearnedRole.SUPERVISOR)
    mailbox.place_supervisor(delivered)
    mailbox.supersede(LearnedRole.MPC)
    mailbox.disconnected(LearnedRole.MPC, LearnedChannelState.WORKER_IDLE)
    assert mailbox.supervisor_source.poll() == delivered
    mailbox.close()


# ================================ 5. loop と Coordinator（本物の受付スレッドと本物の worker）


class Pipeline:
    """本物の受付スレッドと loop に、本物の RL worker（socket・core）をつないだ足場。"""

    def __init__(
        self,
        catalog: MetricCatalog,
        running: Running,
        registry: Registry,
        *,
        config: ControlConfig = CONFIG,
        connect_worker: bool = True,
    ) -> None:
        self.running = running
        self.harness = Harness(
            catalog,
            config=config,
            registry=registry.binding.provenance,
            learned_source=running.mailbox,
            learned_health=running.mailbox,
            learned_sink=running.mailbox,
            rl_supervisor_source=running.mailbox.supervisor_source,
            expected_rl_identity=registry.identity,
        )
        self.core = registry.core(config=config)
        self.client: WorkerChannel | None = None
        if connect_worker:
            running.peer.uid = RL_UID
            self.client = WorkerChannel.connect(
                running.path(LearnedRole.SUPERVISOR),
                max_message_bytes=65_536,
                role=LearnedRole.SUPERVISOR,
            )
            self.hello = self.client.receive_hello(timeout_s=3.0)

    def tick_and_answer(self) -> Any:
        """1 tick 回し、その tick の frame を worker が受け取って1周期回し、出力を送る。"""
        result = self.harness.tick()
        client = self.client
        assert client is not None
        tick_id = result.tick.tick_id
        while (latest := self.core.latest) is None or latest.snapshot.tick_id < tick_id:
            for received in client.receive_frames(timeout_s=3.0):
                self.core.receive(received.run_id, received.frame)
        answer = self.core.step()
        if answer is not None:
            client.send_supervisor_result(self.hello.run_id, answer)
            assert wait_until(lambda: self.running.mailbox.supervisor_source.poll() == answer)
        return result

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


def test_an_rl_output_reaches_only_the_shadow_slot_and_leaves_control_unchanged(
    catalog: MetricCatalog, channel: Any, registry: Registry
) -> None:
    pipeline = Pipeline(catalog, channel(), registry)
    try:
        results = [pipeline.tick_and_answer() for _ in range(10)]
    finally:
        pipeline.close()
    shadows = [
        result.tick.supervisor.shadow
        for result in results
        if result.tick.supervisor.shadow.output is not None
    ]
    assert shadows, [result.tick.supervisor.shadow.error for result in results]
    for shadow in shadows:
        assert shadow.policy is SupervisorPolicyKind.RL
        assert shadow.policy_identity == registry.identity
    for result in results:
        assert result.tick.supervisor.active.policy is SupervisorPolicyKind.RULE

    # 同じ入力で RL の経路が無い loop と、effective demand も active の Supervisor 出力も同じ
    sink = RecordingSink(state=LearnedChannelState.WORKER_DISCONNECTED)
    baseline = Harness(
        catalog,
        config=CONFIG,
        registry=registry.binding.provenance,
        learned_source=StaticSource(),
        learned_health=sink,
        learned_sink=sink,
    )
    for result in results:
        expected = baseline.tick()
        assert result.tick.supervisor.active == expected.tick.supervisor.active
        for zone in Zone:
            with_rl = result.tick.zones.get(zone).demand
            without = expected.tick.zones.get(zone).demand
            assert (with_rl.requested, with_rl.effective) == (
                without.requested,
                without.effective,
            )


def test_an_active_rl_slot_still_falls_back_to_the_rule_policy(
    catalog: MetricCatalog, channel: Any, registry: Registry
) -> None:
    """経路の出力は `unverified`。active slot を通らず RulePolicy へ戻る。

    0061 §2.4 / 0077 §2.8。
    """
    config = rl_config(active_policy="rl_policy", shadow_policy=None)
    pipeline = Pipeline(catalog, channel(), registry, config=config)
    try:
        results = [pipeline.tick_and_answer() for _ in range(10)]
    finally:
        pipeline.close()
    codes = {
        result.tick.supervisor.active.error.code
        for result in results
        if result.tick.supervisor.active.error is not None
    }
    assert "supervisor_origin_not_active" in codes
    for result in results:
        decision = result.tick.supervisor
        assert decision.active.output is None
        assert decision.fallback is not None and decision.fallback.output is not None
        assert decision.fallback.output.policy is SupervisorPolicyKind.RULE


def test_a_disconnected_worker_is_not_read_from_the_next_tick(
    catalog: MetricCatalog, channel: Any, registry: Registry
) -> None:
    pipeline = Pipeline(catalog, channel(), registry)
    try:
        for _ in range(8):
            pipeline.tick_and_answer()
    finally:
        pipeline.close()
    assert wait_until(
        lambda: (
            pipeline.running.mailbox.state(LearnedRole.SUPERVISOR)
            is LearnedChannelState.WORKER_DISCONNECTED
        )
    )
    shadow = pipeline.harness.tick().tick.supervisor.shadow
    assert shadow.output is None
    assert shadow.error is not None and shadow.error.code == "supervisor_unavailable"


def test_an_output_outside_the_bounds_or_of_another_artifact_is_refused(
    catalog: MetricCatalog, channel: Any, registry: Registry
) -> None:
    """worker が枠の外の戦略・別の artifact の識別を送っても Coordinator が拒む（fail closed）。"""
    running: Running = channel()
    pipeline = Pipeline(catalog, running, registry)
    try:
        for _ in range(6):
            pipeline.tick_and_answer()
        latest = pipeline.core.latest
        assert latest is not None
        honest = pipeline.core.step()
        assert honest is not None
        client = pipeline.client
        assert client is not None
        forged_bounds = honest.model_copy(
            update={"output": honest.output.model_copy(update={"strategy": "aggressive"})}
        )
        client.send_supervisor_result(pipeline.hello.run_id, forged_bounds)
        assert wait_until(lambda: running.mailbox.supervisor_source.poll() == forged_bounds)
        shadow = pipeline.harness.tick().tick.supervisor.shadow
        assert shadow.error is not None and shadow.error.code == "supervisor_output_invalid"

        forged_identity = DeliveredSupervisorOutput(
            output=honest.output,
            identity=registry.identity.model_copy(update={"artifact_sha256": "e" * 64}),
        )
        client.send_supervisor_result(pipeline.hello.run_id, forged_identity)
        assert wait_until(lambda: running.mailbox.supervisor_source.poll() == forged_identity)
        shadow = pipeline.harness.tick().tick.supervisor.shadow
        assert shadow.error is not None and shadow.error.code == "supervisor_identity_mismatch"
    finally:
        pipeline.close()


def test_a_dead_receiver_closes_the_rl_slot_without_max(
    catalog: MetricCatalog, channel: Any, registry: Registry
) -> None:
    running: Running = channel()
    pipeline = Pipeline(catalog, running, registry)
    try:
        for _ in range(6):
            pipeline.tick_and_answer()
        running.mailbox.receiver_failed()
        result = pipeline.harness.tick()
    finally:
        pipeline.close()
    shadow = result.tick.supervisor.shadow
    assert shadow.output is None and shadow.error is not None
    assert result.tick.state.safety_state is SafetyState.NORMAL
    assert not any(result.tick.zones.get(zone).demand.forced_max for zone in Zone)
    assert pipeline.harness.loop._learned_channel_dead


def test_the_daemon_wires_the_rl_slot_of_the_channel(tmp_path: Path, short_dir: Path) -> None:
    """`coldaisle-fand` は RL の口を経路の受け渡し口から渡す（経路が無ければ配線しない）。"""
    from coldaisle.control_daemon import build
    from test_fand_registry import daemon_config

    daemon = build(daemon_config(tmp_path))
    try:
        assert daemon.loop._rl_supervisor_source is None
    finally:
        daemon.close()

    broken = short_dir / "learned-channel.yaml"
    broken.write_text("version: 99\n", "utf-8")
    other = tmp_path / "other"
    other.mkdir()
    daemon = build(daemon_config(other, learned_channel_config=broken))
    try:
        source = daemon.loop._rl_supervisor_source
        assert source is not None and source.poll() is None
    finally:
        daemon.close()


# ================================================================ 境界


def test_the_worker_package_never_reaches_actuation_or_the_llm() -> None:
    """worker の package は hwmon・シリアル・LLM 層・SQLite を import しない（0077 §2.7）。"""
    forbidden = (
        "sqlite3",
        "serial",
        "subprocess",
        "coldaisle.store",
        "coldaisle.ai",
        "coldaisle.api",
        "coldaisle.server",
        "coldaisle.control.hardware",
        "coldaisle.control.safety",
        "coldaisle.control.reactive",
        "coldaisle.control_daemon",
        "coldaisle.control_admin",
        "coldaisle.event_entry",
    )
    for path in sorted((SRC / "learned_worker").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                assert not any(name == item or name.startswith(f"{item}.") for item in forbidden), (
                    f"{path.name} imports {name}"
                )


def test_the_rl_worker_never_calls_for_active() -> None:
    """active への門は閉じたまま（0061 §2.4 / 0077 §2.10 段階 4）。"""
    text = (SRC / "learned_worker" / "supervisor.py").read_text("utf-8")
    assert "for_active" not in text
    assert "for_shadow" in text


def test_envelope_mpc_heartbeat_still_parses() -> None:
    """MPC の封筒（段階 1〜3）は変わらない。"""
    parsed = parse_inbound(json.dumps(envelope()).encode())
    assert parsed.body.kind == "heartbeat"


@pytest.mark.parametrize(
    "state",
    [
        LearnedChannelState.WORKER_DISCONNECTED,
        LearnedChannelState.WORKER_IDLE,
        LearnedChannelState.REGISTRY_SUPERSEDED,
        LearnedChannelState.CHANNEL_DEAD,
        LearnedChannelState.CHANNEL_DISABLED,
    ],
)
def test_the_loop_reads_no_rl_output_unless_the_role_is_connected(
    catalog: MetricCatalog, registry: Registry, state: LearnedChannelState
) -> None:
    """受け渡し口が出力を返しても、経路の状態が `connected` でなければ loop は読まない。

    受け渡し口の側の確かめとは別の、loop の側の守り（0077 §2.5 の RL の行）。
    """
    frames = loop_frames(catalog, registry)

    class RoleHealth:
        def __init__(self) -> None:
            self.current = LearnedChannelState.CONNECTED

        def state(self, role: LearnedRole) -> LearnedChannelState:
            return self.current if role is LearnedRole.SUPERVISOR else LearnedChannelState.CONNECTED

    class Answering:
        """loop が直前に出した frame から本物の worker core で出力を作って返す。"""

        def __init__(self) -> None:
            self.core = registry.core()
            self.sink = RecordingSink()

        def poll(self) -> DeliveredSupervisorOutput | None:
            if not self.sink.frames:
                return None
            return feed(self.core, self.sink.frames[-1])

    health = RoleHealth()
    answering = Answering()
    harness = Harness(
        catalog,
        config=CONFIG,
        registry=registry.binding.provenance,
        learned_source=StaticSource(),
        learned_health=health,
        learned_sink=answering.sink,
        rl_supervisor_source=answering,
        expected_rl_identity=registry.identity,
    )
    shadows = [harness.tick().tick.supervisor.shadow for _ in range(len(frames))]
    assert any(shadow.output is not None for shadow in shadows)

    health.current = state
    shadow = harness.tick().tick.supervisor.shadow
    assert shadow.output is None
    assert shadow.error is not None and shadow.error.code == "supervisor_unavailable"


def test_a_reconnected_worker_does_not_resurrect_the_previous_output(
    delivered: DeliveredSupervisorOutput,
) -> None:
    """切断で枠を空にする。再接続した直後に前の接続の出力を返さない（0077 §2.5）。"""
    from coldaisle.learned_channel.mailbox import LearnedMailbox

    for close in (
        lambda m: m.disconnected(LearnedRole.SUPERVISOR, LearnedChannelState.WORKER_DISCONNECTED),
        lambda m: m.disconnected(LearnedRole.SUPERVISOR, LearnedChannelState.WORKER_IDLE),
    ):
        mailbox = LearnedMailbox()
        mailbox.mark_stopping()
        mailbox.connected(LearnedRole.SUPERVISOR)
        mailbox.place_supervisor(delivered)
        assert mailbox.supervisor_source.poll() == delivered
        close(mailbox)
        mailbox.connected(LearnedRole.SUPERVISOR)
        assert mailbox.supervisor_source.poll() is None
        mailbox.close()


def test_a_superseded_role_keeps_its_slot_empty_even_if_offered(
    delivered: DeliveredSupervisorOutput,
) -> None:
    """閉じた役割には受付が置こうとしても置かない（検証の前に捨てるのに加えた二重の守り）。"""
    from coldaisle.learned_channel.mailbox import LearnedMailbox

    mailbox = LearnedMailbox()
    mailbox.supersede(LearnedRole.SUPERVISOR)
    mailbox.place_supervisor(delivered)
    assert mailbox._supervisor is None
    mailbox.close()


def test_a_dead_receiver_thread_is_never_read_even_with_an_output_in_the_slot(
    delivered: DeliveredSupervisorOutput,
) -> None:
    """受付スレッドが（後片付けの前に）死んでいれば、枠に残った出力も返さない（0077 §2.2）。"""
    from coldaisle.learned_channel.mailbox import LearnedMailbox

    mailbox = LearnedMailbox()
    dead = threading.Thread(target=lambda: None)
    mailbox.attach_receiver(dead)
    dead.start()
    dead.join()
    mailbox.connected(LearnedRole.SUPERVISOR)
    mailbox.place_supervisor(delivered)
    assert mailbox.state(LearnedRole.SUPERVISOR) is LearnedChannelState.CHANNEL_DEAD
    assert mailbox.supervisor_source.poll() is None
    mailbox.close()


def test_the_supervisor_role_starts_and_exits_when_fand_is_not_listening(
    tmp_path: Path, short_dir: Path, registry: Registry
) -> None:
    """`--role supervisor` は設定を読んでから RL のソケットへ接続し、fand がいなければ終了する。"""
    import yaml

    from coldaisle.learned_worker.cli import EXIT_CHANNEL_CLOSED, main
    from test_fand_registry import daemon_config
    from test_learned_channel import channel_document

    config = daemon_config(tmp_path)
    channel_config = short_dir / "learned-channel.yaml"
    channel_config.write_text(yaml.safe_dump(channel_document(short_dir / "run")), "utf-8")
    assert (
        main(
            [
                "--role",
                "supervisor",
                "--config-dir",
                str(config.config_dir),
                "--registry-root",
                str(registry.root),
                "--learned-channel-config",
                str(channel_config),
            ]
        )
        == EXIT_CHANNEL_CLOSED
    )
