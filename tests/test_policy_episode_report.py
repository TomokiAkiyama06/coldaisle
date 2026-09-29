"""#105 episode 結果を Offline Evaluation の枠へ出す報告と、supervisor policy の validated 化。

決定記録 0074 §2.2 / §2.3。実機不要（合成 episode / 本物の Model Registry / 近似 simulator）。

**ここでは「守れているか」ではなく「破れないか」を試す。**

1. episode の条件 hash は**2段**で、欄を書き換えた episode は作れず読み戻せない
2. 報告は **Baseline をちょうど1つの Rule arm（`arms[0]`）**として渡した Rule policy へ束縛する
3. RL arm は**表と action の証拠**で certify 済み artifact へ束縛し、版の一致では束縛しない
4. いまはすべての RL arm が evidence の段で `blocked` になり、評価参照を出さない
5. 報告の bytes は `EvaluationReport` として復号できない（#92 の昇格証拠に混ざらない）
6. `validate_supervisor_policy()` は作り直しの bytes・Registry の記録まで照合し、
   1つでも食い違えば `mark_validated()` を呼ばない
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control.evaluation.episode import (
    PolicyEpisodeReport,
    PolicyEpisodeReportError,
    build_policy_episode_report,
    episode_arm_key,
)
from coldaisle.control.evaluation.model import EvaluationReport, GateOutcome, GateStage
from coldaisle.control.model.thermal import canonical_json_bytes
from coldaisle.control.model_registry import (
    ArtifactMetadata,
    ArtifactRef,
    ArtifactStatus,
    ModelRegistry,
)
from coldaisle.control.rl.episode import (
    EPISODE_SCHEMA_VERSION,
    EpisodeConfigDigests,
    EpisodeResult,
    PolicyComparison,
    config_digest,
    episode_conditions_sha256,
)
from coldaisle.control.schema import (
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyKind,
)
from coldaisle.control.supervisor import (
    RulePolicy,
    SupervisorInput,
    canonical_policy_artifact_bytes,
    certified_identity,
    policy_registry_metadata,
    policy_registry_metadata_json_bytes,
    rule_policy_identity,
    validate_supervisor_policy,
)
from test_learned_mpc import REGISTRY_LIMITS
from test_rl_supervisor_policy import CREATED_AT, rl_policy_config, trainer_for
from test_rl_training_environment import build_environment, episode_spec, provisional, rl_config
from test_rl_training_environment import trained as _trained_fixture

trained = _trained_fixture

SPECS = (episode_spec(episode_id="pr105-a", seed=3), episode_spec(episode_id="pr105-b", seed=4))


class Setup:
    """学習1回分の入力一式（本物の trainer / certify / Rule policy）。"""

    def __init__(self, trained: Any, *, with_mpc: bool) -> None:
        environment, config, settings, safety = build_environment(trained, with_mpc=with_mpc)
        self.config = config
        self.settings = settings
        self.safety = safety
        self.trainer = trainer_for(environment, settings)
        self.training = self.trainer.train(SPECS, model_version="0.1.0", created_at=CREATED_AT)
        self.certified = self.trainer.certify(self.training)
        self.rule_policy = RulePolicy(settings.supervisor.rule_policy, SimulatedClock(0))
        self.policy_config = rl_policy_config()[0]
        self.comparison = self.training.comparison
        self.key = self.comparison.arms[1].policy_version

    def build(self, comparison: PolicyComparison | None = None, **overrides: Any) -> Any:
        arguments: dict[str, Any] = {
            "policy_config": self.policy_config,
            "rule_policy": self.rule_policy,
            "certified": {self.key: self.certified},
        }
        arguments.update(overrides)
        return build_policy_episode_report(comparison or self.comparison, **arguments)


@pytest.fixture(scope="module")
def unbacked(trained: Any) -> Setup:
    """Learned MPC を束縛できない（いまの既定の）学習。"""
    return Setup(trained, with_mpc=False)


@pytest.fixture(scope="module")
def with_mpc(trained: Any) -> Setup:
    return Setup(trained, with_mpc=True)


# ---------------------------------------------------------------- 1. 条件 hash の2段化


def test_invariant_1_episode_conditions_are_two_level_and_readable(unbacked: Setup) -> None:
    episode = unbacked.comparison.arms[0].episodes[0]
    assert episode.schema_version == EPISODE_SCHEMA_VERSION == 2
    assert episode.config_digests == EpisodeConfigDigests(
        rl_training=config_digest(unbacked.config),
        fan_policy=config_digest(unbacked.settings),
        safety=config_digest(unbacked.safety),
    )
    assert episode.conditions_sha256 == episode_conditions_sha256(
        episode.config_digests, episode.other_conditions
    )
    # 読み戻しでも同じ bytes になる。
    assert EpisodeResult.model_validate_json(episode.model_dump_json()) == episode


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reward_version", "reward-other"),
        ("discount", 0.5),
        ("seed", 999),
        ("learned_controller_available", True),
        ("applied_demand_tolerance", 0.1),
    ],
)
def test_invariant_1_b_rewriting_a_mirrored_field_is_refused(
    unbacked: Setup, field: str, value: object
) -> None:
    """欄だけ書き換えた episode は、中の条件と食い違うので作れない。"""
    document = json.loads(unbacked.comparison.arms[0].episodes[0].model_dump_json())
    document[field] = value
    with pytest.raises(ValidationError, match="条件"):
        EpisodeResult.model_validate_json(json.dumps(document))


def test_invariant_1_c_rewriting_both_sides_breaks_the_rebuilt_hash(unbacked: Setup) -> None:
    """欄と中の条件を揃えて書き換えても、作り直した条件 hash が一致しない。"""
    document = json.loads(unbacked.comparison.arms[0].episodes[0].model_dump_json())
    document["reward_version"] = "reward-other"
    document["other_conditions"]["reward_version"] = "reward-other"
    with pytest.raises(ValidationError, match="条件 hash"):
        EpisodeResult.model_validate_json(json.dumps(document))
    document = json.loads(unbacked.comparison.arms[0].episodes[0].model_dump_json())
    document["config_digests"]["safety"] = "e" * 64
    with pytest.raises(ValidationError, match="条件 hash"):
        EpisodeResult.model_validate_json(json.dumps(document))
    document = json.loads(unbacked.comparison.arms[0].episodes[0].model_dump_json())
    del document["other_conditions"]["dynamics"]
    with pytest.raises(ValidationError, match="照合する値が無い"):
        EpisodeResult.model_validate_json(json.dumps(document))


def test_invariant_1_d_a_v1_episode_is_not_read(unbacked: Setup) -> None:
    document = json.loads(unbacked.comparison.arms[0].episodes[0].model_dump_json())
    document["schema_version"] = 1
    with pytest.raises(ValidationError):
        EpisodeResult.model_validate_json(json.dumps(document))


# ---------------------------------------------------------------- 2〜4. 報告の構築


def test_invariant_3_rl_arms_bind_through_table_and_actions_not_versions(unbacked: Setup) -> None:
    """学習の arm の版と artifact の版は別の名前空間である。版が違っても束縛できる。"""
    identity = certified_identity(unbacked.certified)
    assert unbacked.key != identity.version
    report = unbacked.build()
    baseline, arm = report.arms
    assert baseline.policy is SupervisorPolicyKind.RULE
    assert baseline.rule_policy_identity == rule_policy_identity(unbacked.rule_policy)
    assert baseline.rl_policy_identity is None
    assert arm.rl_policy_identity == identity
    assert arm.rl_table_sha256 == unbacked.certified.artifact.manifest.payload_sha256
    assert arm.policy_version == unbacked.key
    assert arm.arm_key == episode_arm_key(SupervisorPolicyKind.RL, unbacked.key)
    assert report.config_digests == unbacked.comparison.arms[0].episodes[0].config_digests
    assert report.rl_policy_config_sha256 == config_digest(unbacked.policy_config)
    assert report.comparison_sha256 != report.conditions_sha256
    # 同じ入力からは同じ bytes。
    assert canonical_json_bytes(unbacked.build()) == canonical_json_bytes(report)


def test_invariant_4_every_rl_arm_is_blocked_without_a_learned_controller(
    unbacked: Setup,
) -> None:
    report = unbacked.build()
    gate = report.gate(unbacked.key)
    assert report.learned_controller_available is False
    assert gate.outcome is GateOutcome.BLOCKED
    assert gate.blocking_stage is GateStage.EVIDENCE
    learned = next(item for item in gate.conditions if item.name == "learned_controller_available")
    assert learned.reason is not None
    assert learned.reason.code == "learned_controller_unavailable"
    with pytest.raises(ValueError, match="pass でない"):
        report.evaluation_ref(unbacked.key)


def test_invariant_5_the_report_cannot_be_read_as_an_evaluation_report(unbacked: Setup) -> None:
    payload = canonical_json_bytes(unbacked.build())
    with pytest.raises(ValidationError):
        EvaluationReport.model_validate_json(payload)
    # 読み戻しは自分の型でだけ通る。
    assert PolicyEpisodeReport.model_validate_json(payload) == unbacked.build()


def _swap_policy(arm: Any, policy: SupervisorPolicyKind, version: str | None = None) -> Any:
    version = version or arm.policy_version
    episodes = tuple(
        episode.model_copy(update={"policy": policy, "policy_version": version})
        for episode in arm.episodes
    )
    return arm.model_copy(
        update={"policy": policy, "policy_version": version, "episodes": episodes}
    )


def test_invariant_2_the_baseline_is_exactly_one_rule_arm_at_the_front(unbacked: Setup) -> None:
    baseline, arm = unbacked.comparison.arms
    reversed_arms = PolicyComparison(
        conditions_sha256=unbacked.comparison.conditions_sha256, arms=(arm, baseline)
    )
    with pytest.raises(PolicyEpisodeReportError, match=r"arms\[0\]"):
        unbacked.build(reversed_arms)
    two_rules = PolicyComparison(
        conditions_sha256=unbacked.comparison.conditions_sha256,
        arms=(baseline, _swap_policy(arm, SupervisorPolicyKind.RULE)),
    )
    with pytest.raises(PolicyEpisodeReportError, match="ちょうど1つ"):
        unbacked.build(two_rules, certified={})
    no_rule = PolicyComparison(
        conditions_sha256=unbacked.comparison.conditions_sha256,
        arms=(_swap_policy(baseline, SupervisorPolicyKind.RL), arm),
    )
    with pytest.raises(PolicyEpisodeReportError, match="ちょうど1つ"):
        unbacked.build(no_rule)


def test_invariant_2_b_the_baseline_is_bound_to_the_rule_table_not_its_version(
    unbacked: Setup,
) -> None:
    rule_policy = unbacked.rule_policy

    class SameVersionOtherContexts:
        kind = SupervisorPolicyKind.RULE
        version = rule_policy.version

        def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
            output = rule_policy.propose(policy_input)
            return output.model_copy(
                update={"weights": output.weights.model_copy(update={"change": 0.0})}
            )

    class OtherVersion(SameVersionOtherContexts):
        version = f"{rule_policy.version}-other"

        def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
            output = rule_policy.propose(policy_input)
            return output.model_copy(update={"version": self.version})

    with pytest.raises(PolicyEpisodeReportError, match="再現しない"):
        unbacked.build(rule_policy=SameVersionOtherContexts())
    with pytest.raises(PolicyEpisodeReportError, match="版"):
        unbacked.build(rule_policy=OtherVersion())


def test_invariant_3_b_artifacts_map_one_to_one_onto_rl_arms(unbacked: Setup) -> None:
    with pytest.raises(PolicyEpisodeReportError, match="1対1"):
        unbacked.build(certified={})
    with pytest.raises(PolicyEpisodeReportError, match="1対1"):
        unbacked.build(certified={unbacked.key: unbacked.certified, "extra": unbacked.certified})
    baseline, arm = unbacked.comparison.arms
    twin = _swap_policy(arm, SupervisorPolicyKind.RL, f"{arm.policy_version}-twin")
    three = PolicyComparison(
        conditions_sha256=unbacked.comparison.conditions_sha256, arms=(baseline, arm, twin)
    )
    with pytest.raises(PolicyEpisodeReportError, match="同じ artifact"):
        unbacked.build(
            three,
            certified={
                arm.policy_version: unbacked.certified,
                twin.policy_version: unbacked.certified,
            },
        )


def test_invariant_3_c_an_artifact_that_does_not_replay_the_arm_is_refused(
    unbacked: Setup,
) -> None:
    baseline, arm = unbacked.comparison.arms
    episode = arm.episodes[0]
    step = episode.steps[0]
    other = SupervisorObjectiveWeights.model_validate(
        step.action.weights.model_dump() | {"change": 0.0 if step.action.weights.change else 0.5}
    )
    altered = step.model_copy(update={"action": step.action.model_copy(update={"weights": other})})
    tampered = arm.model_copy(
        update={
            "episodes": (
                episode.model_copy(update={"steps": (altered, *episode.steps[1:])}),
                *arm.episodes[1:],
            )
        }
    )
    comparison = unbacked.comparison.model_copy(update={"arms": (baseline, tampered)})
    with pytest.raises(PolicyEpisodeReportError, match="artifact の表が再現しない"):
        unbacked.build(comparison)


def test_invariant_3_d_episodes_built_under_other_configs_are_refused(unbacked: Setup) -> None:
    baseline, arm = unbacked.comparison.arms
    episode = arm.episodes[0]
    digests = episode.config_digests.model_copy(update={"safety": "e" * 64})
    tampered = arm.model_copy(
        update={
            "episodes": (episode.model_copy(update={"config_digests": digests}), *arm.episodes[1:])
        }
    )
    comparison = unbacked.comparison.model_copy(update={"arms": (baseline, tampered)})
    with pytest.raises(PolicyEpisodeReportError, match="設定"):
        unbacked.build(comparison)


# ---------------------------------------------------------------- 6. validated 化


def _forged_passing_comparison(setup: Setup) -> PolicyComparison:
    """**試験だけで作る**、gate を通る比較。

    いまは反実仮想 artifact が無いので、環境は `promotable` を立てない。validated 化の経路を
    試すために、validator を通さない `model_copy` で episode を昇格可能にし、Baseline の
    reward を下げる（RL arm の action は環境が出したまま。artifact の表で再現できる）。
    """
    baseline, arm = setup.comparison.arms

    def promotable(item: Any, *, penalty: float) -> Any:
        episodes = []
        for episode in item.episodes:
            steps = tuple(
                step
                if step.reward is None
                else step.model_copy(
                    update={
                        "reward": step.reward.model_copy(
                            update={"reward": step.reward.reward - penalty}
                        )
                    }
                )
                for step in episode.steps
            )
            episodes.append(episode.model_copy(update={"promotable": True, "steps": steps}))
        return item.model_copy(update={"episodes": tuple(episodes)})

    return setup.comparison.model_copy(
        update={"arms": (promotable(baseline, penalty=1.0), promotable(arm, penalty=0.0))}
    )


def _registered(setup: Setup, root: Path, **metadata_overrides: Any) -> tuple[ModelRegistry, Any]:
    artifact_bytes = canonical_policy_artifact_bytes(setup.certified)
    derived = policy_registry_metadata(setup.certified, artifact_bytes)
    metadata = ArtifactMetadata.model_validate_json(policy_registry_metadata_json_bytes(derived))
    if metadata_overrides:
        metadata = ArtifactMetadata.model_validate(
            metadata.model_dump(mode="python") | metadata_overrides
        )
    registry = ModelRegistry(root, limits=REGISTRY_LIMITS)
    registry.register_candidate(metadata, artifact_bytes, actor="trainer", reason="certified")
    return registry, metadata


def _validate(
    setup: Setup,
    registry: ModelRegistry,
    ref: ArtifactRef,
    report: PolicyEpisodeReport,
    comparison: PolicyComparison,
    **overrides: Any,
) -> int:
    arguments: dict[str, Any] = {
        "report": report,
        "comparison": comparison,
        "training_config": setup.config,
        "fan_policy": setup.settings,
        "safety": setup.safety,
        "policy_config": setup.policy_config,
        "certified": {setup.key: setup.certified},
        "target": setup.key,
        "baseline_rule_policy": setup.rule_policy,
        "actor": "evaluator",
        "reason": "episode gates passed",
        "expected_revision": registry.inspect().revision,
    }
    arguments.update(overrides)
    return validate_supervisor_policy(registry, ref, **arguments)


def test_invariant_6_the_current_evidence_never_validates_a_policy(
    tmp_path: Path, unbacked: Setup
) -> None:
    registry, metadata = _registered(unbacked, tmp_path / "blocked")
    before = registry.inspect()
    with pytest.raises(ValueError, match="pass でない"):
        _validate(unbacked, registry, metadata.ref, unbacked.build(), unbacked.comparison)
    assert registry.inspect() == before


def test_invariant_6_b_a_passing_report_validates_only_the_certified_record(
    tmp_path: Path, with_mpc: Setup
) -> None:
    comparison = _forged_passing_comparison(with_mpc)
    report = with_mpc.build(comparison)
    gate = report.gate(with_mpc.key)
    assert gate.outcome is GateOutcome.PASS, gate

    def refused(registry: ModelRegistry, ref: ArtifactRef, match: str, **overrides: Any) -> None:
        before = registry.inspect()
        with pytest.raises((ValueError, TypeError, PolicyEpisodeReportError), match=match):
            _validate(
                with_mpc,
                registry,
                ref,
                overrides.pop("report", report),
                overrides.pop("comparison", comparison),
                **overrides,
            )
        # **拒んだら何も書かない**（mark_validated を呼ばない）。
        assert registry.inspect() == before

    registry, metadata = _registered(with_mpc, tmp_path / "pass")
    ref = metadata.ref
    # Registry の ref / 記録が certify 済み artifact と一致しない。
    refused(registry, ref.model_copy(update={"version": "0.2.0"}), "照合済みの supervisor policy")
    refused(registry, ref, "revision", expected_revision=registry.inspect().revision + 1)
    tampered_registry, tampered = _registered(
        with_mpc, tmp_path / "tampered", source_runs=("ep-z",)
    )
    refused(tampered_registry, tampered.ref, "metadata が照合済みの artifact と一致しない")
    # report が作り直した bytes と一致しない（識別だけ・数字だけの差し替え）。
    other_identity = report.arms[1].model_copy(
        update={
            "rl_policy_identity": report.arms[1].rl_policy_identity.model_copy(  # type: ignore[union-attr]
                update={"artifact_sha256": "e" * 64}
            )
        }
    )
    refused(
        registry,
        ref,
        "bytes で一致しない",
        report=report.model_copy(update={"arms": (report.arms[0], other_identity)}),
    )
    refused(
        registry,
        ref,
        "bytes で一致しない",
        report=report.model_copy(update={"minimum_reward_improvement": -1.0}),
    )
    # 別の比較。
    refused(registry, ref, "comparison_sha256", comparison=with_mpc.comparison)
    # 別の設定（rl-policy.yaml は作り直しで、3つの設定は digest で割れる）。
    stricter = {"minimum_reward_improvement": provisional(0.5)}
    other_policy_config = rl_policy_config(search=stricter)[0]
    refused(registry, ref, "bytes で一致しない", policy_config=other_policy_config)
    other_training = rl_config(
        coverage={
            "minimum_supported_fraction": provisional(0.5),
            "minimum_steps": provisional(3),
        }
    )[0]
    refused(registry, ref, "設定の digest", training_config=other_training)
    # 対応の欠け・名指しの誤り。
    refused(registry, ref, "対応に無い", target="missing")
    refused(
        registry,
        ref,
        "1対1",
        certified={with_mpc.key: with_mpc.certified, "extra": with_mpc.certified},
    )
    refused(registry, ref, "検証済み", safety={"schema_version": 1})

    revision = _validate(with_mpc, registry, ref, report, comparison)
    record = registry.inspect().artifacts[ref.key]
    assert revision == registry.inspect().revision
    assert record.status is ArtifactStatus.VALIDATED
    assert record.metadata.offline_evaluation_ref == report.evaluation_ref(with_mpc.key)


# ---------------------------------------------------------------- import の向き


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def test_the_rl_package_never_imports_the_evaluation_package() -> None:
    """**向きは一方向。** `control.rl` は `control.evaluation` を import しない（0074 §2.2）。"""
    root = Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control" / "rl"
    offenders = [
        f"{path.name}: {name}"
        for path in sorted(root.glob("*.py"))
        for name in _imported_modules(path)
        if name.startswith("coldaisle.control.evaluation")
    ]
    assert offenders == []
