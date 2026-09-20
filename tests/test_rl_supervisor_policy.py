"""#89 RL Supervisor。実機不要（合成 episode / 本物の Model Registry / 近似 simulator）。

**ここでは「守れているか」ではなく「破れないか」を試す。** 不変条件を並べ、1つずつ破ろうと
する試験を置く。

1. policy artifact は **Fan Demand を表現できない**
2. 表は **全 regime を1つずつ**覆う。未知の regime を既定の欄へ倒せない
3. manifest は **自分の payload だけ**を名指しする（表の差し替えを許さない）
4. 反実仮想の裏づけを名乗らない artifact は **SHADOW 以外の authority を名乗れない**
5. 裏づけの自称は、**片方の条件だけでは成り立たない**
6. 学習 episode の数は **名指しした識別子の数**と一致する（重複で増やせない）
7. 束縛には **Registry が発行した attestation** が要る（自作の束は通らない）
8. 束縛は **runtime の action 空間**と一致することを要求し、範囲外の欄を丸めない
9. `for_active` は **production pointer と反実仮想の裏づけ**を要求する（いまは必ず拒否）
10. `RegimeTableRlPolicy` は #88 の契約を満たし、**Coordinator の shadow slot でだけ使われる**
11. Shadow 台帳は **同じ tick を2度渡しても数が増えない**。食い違う重複は拒む
12. Shadow 台帳は **片方しか無い tick を一致として数えない**。下限未満は `usable=False`
13. Shadow 台帳は **版を名指しし**、時刻を観測した decision から取る
14. 探索は **決定論的**で、評価順（seed）に依らない
15. **比べられない候補は勝たない。** Baseline を上回らなければ Baseline の表が選ばれる
16. Learned MPC を束縛できない学習からは、**昇格の根拠にできる結果が出ない**
17. artifact から作り直した policy は、**選ばれた候補と同じ戦略**を返す
"""

from __future__ import annotations

import ast
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control.config import FanPolicyConfig, SupervisorConfig, SupervisorOutputBounds
from coldaisle.control.model_registry import (
    ApprovalAction,
    ArtifactCapability,
    ArtifactFormat,
    ArtifactKind,
    ArtifactMetadata,
    HumanApproval,
    ModelCompatibility,
    ModelRegistry,
    VerifiedArtifact,
)
from coldaisle.control.rl.training import (
    BASELINE_CANDIDATE_ID,
    SupervisorPolicyTrainer,
    SupervisorPolicyTrainingError,
)
from coldaisle.control.schema import (
    AuthorityStage,
    Reason,
    SupervisorDecision,
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyEvaluation,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    TemperatureTarget,
    WorkloadRegime,
)
from coldaisle.control.state import ControlStateSnapshot, TelemetryHealth
from coldaisle.control.supervisor import (
    PolicyArtifactVerification,
    PolicyBindingIntent,
    PolicySearchHyperparameters,
    PolicyTrainingEvidence,
    ReceivedSupervisorOutput,
    RegimeActionEntry,
    RegimeTablePayload,
    RegimeTableRlPolicy,
    RlPolicyConfig,
    RulePolicy,
    ShadowRLPolicy,
    SupervisorCoordinator,
    SupervisorInput,
    SupervisorOutputOrigin,
    SupervisorPolicyArtifact,
    SupervisorPolicyBinding,
    SupervisorPolicyManifest,
    SupervisorPolicyUnusableError,
    SupervisorShadowConflictError,
    SupervisorShadowLedger,
    SupervisorShadowUsageError,
    action_space_sha256,
    canonical_policy_artifact_bytes,
    policy_registry_metadata,
)
from coldaisle.control.supervisor.regime import (
    RegimeEvidence,
    RegimeReason,
    WorkloadRegimeEstimate,
)
from test_learned_mpc import REGISTRY_LIMITS, mpc_policy
from test_rl_training_environment import build_environment, episode_spec
from test_rl_training_environment import trained as _trained_fixture

# #105 の合成モデル一式をそのまま使う。pytest は module 属性名で fixture を解決するので、
# 別名で import したうえでこの名前へ束ね直す（同じ dataset を2度作らないため）。
trained = _trained_fixture

CREATED_AT = "2026-09-21T10:00:00+09:00"
CONDITIONS = "a" * 64


# ---------------------------------------------------------------- 下ごしらえ


def provisional(value: float | int) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def weights(**overrides: float) -> SupervisorObjectiveWeights:
    values: dict[str, float] = {
        "gpu_temperature": 0.8,
        "cpu_temperature": 0.8,
        "balance": 0.5,
        "acoustic": 0.4,
        "change": 0.3,
    }
    values.update(overrides)
    return SupervisorObjectiveWeights(**values)


def target_band() -> SupervisorTargetBand:
    return SupervisorTargetBand(
        cpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=75.0),
        gpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=78.0),
    )


def table(**per_regime: SupervisorObjectiveWeights) -> RegimeTablePayload:
    """全 regime を覆う表。指定した regime だけ weight を差し替える。"""
    return RegimeTablePayload(
        entries=tuple(
            RegimeActionEntry(
                regime=regime,
                strategy="balanced",
                weights=per_regime.get(regime.value, weights()),
                target_band=target_band(),
            )
            for regime in sorted(WorkloadRegime, key=lambda item: item.value)
        )
    )


def evidence(**overrides: Any) -> PolicyTrainingEvidence:
    document: dict[str, Any] = {
        "training_mode": "logged",
        "dynamics_provenance": "logged_trajectory",
        "learned_controller_available": False,
        "counterfactual_backed": False,
        "promotable_episodes": 0,
        "total_episodes": 2,
        "improved_over_baseline": False,
        "baseline_policy_version": "rule-test-v1",
        "conditions_sha256": CONDITIONS,
    }
    document.update(overrides)
    return PolicyTrainingEvidence.model_validate(document)


def manifest(
    payload: RegimeTablePayload,
    bounds: SupervisorOutputBounds,
    **overrides: Any,
) -> SupervisorPolicyManifest:
    from coldaisle.control.model.thermal import canonical_sha256

    document: dict[str, Any] = {
        "model_id": "rl-supervisor-test",
        "model_version": "0.1.0",
        "created_at": CREATED_AT,
        "action_space_sha256": action_space_sha256(bounds),
        "rl_training_config_sha256": sha256(b"rl-training").hexdigest(),
        "rl_policy_config_sha256": sha256(b"rl-policy").hexdigest(),
        "reward_version": "reward-test-v1",
        "training_episode_ids": ("ep-a", "ep-b"),
        "training_evidence": evidence(),
        "hyperparameters": PolicySearchHyperparameters(
            search_family="per_regime_coordinate_v1",
            seed=1,
            candidates_evaluated=3,
            episodes_per_candidate=2,
        ),
        "payload_sha256": canonical_sha256(payload),
        "code_commit": None,
        "authority_compatibility": (AuthorityStage.SHADOW,),
    }
    document.update(overrides)
    return SupervisorPolicyManifest.model_validate(document)


def artifact(
    bounds: SupervisorOutputBounds,
    *,
    payload: RegimeTablePayload | None = None,
    **manifest_overrides: Any,
) -> SupervisorPolicyArtifact:
    body = payload or table()
    return SupervisorPolicyArtifact(
        manifest=manifest(body, bounds, **manifest_overrides), payload=body
    )


def register_policy(
    root: Path,
    policy_artifact: SupervisorPolicyArtifact,
    *,
    authority: tuple[AuthorityStage, ...] = (AuthorityStage.SHADOW,),
    stage: AuthorityStage = AuthorityStage.SHADOW,
    promoted: bool = False,
    kind: ArtifactKind = ArtifactKind.SUPERVISOR_POLICY,
    capability: ArtifactCapability = ArtifactCapability.SUPERVISOR_STRATEGY,
    payload: bytes | None = None,
) -> VerifiedArtifact:
    """**本物の Model Registry（#104）へ登録し、検証経路から VerifiedArtifact を受け取る。**

    試験が自分で証拠を組み立てないようにする。
    """
    artifact_bytes = payload or canonical_policy_artifact_bytes(policy_artifact)
    derived = policy_registry_metadata(policy_artifact, artifact_bytes)
    metadata = ArtifactMetadata(
        kind=kind,
        artifact_format=ArtifactFormat.JSON,
        capability=capability,
        model_id=derived.model_id,
        version=derived.version,
        created_at=derived.created_at,
        training_dataset_version=derived.training_dataset_version,
        source_runs=derived.source_runs,
        feature_schema_version=derived.feature_schema_version,
        target_schema_version=derived.target_schema_version,
        code_commit=derived.code_commit,
        sha256=derived.sha256,
        model_family=derived.model_family,
        hyperparameters=derived.hyperparameters,
        authority_compatibility=authority,
    )
    registry = ModelRegistry(root, limits=REGISTRY_LIMITS)
    registry.register_candidate(
        metadata, artifact_bytes, actor="trainer", reason="policy search completed"
    )
    compatibility = ModelCompatibility(
        feature_schema_version=derived.feature_schema_version,
        target_schema_version=derived.target_schema_version,
        authority_stage=stage,
    )
    if promoted:
        registry.mark_validated(
            metadata.ref,
            offline_evaluation_ref="evaluation/offline/89",
            actor="evaluator",
            reason="offline gates passed",
            expected_revision=registry.inspect().revision,
        )
        revision = registry.inspect().revision
        registry.promote(
            metadata.ref,
            compatibility,
            shadow_evaluation_ref="supervisor-shadow:" + "c" * 64,
            approval=HumanApproval(
                action=ApprovalAction.PROMOTE,
                artifact=metadata.ref,
                artifact_sha256=metadata.sha256,
                expected_revision=revision,
                approver="model-operator",
                approved_at_ms=1_700_000_000_000,
                reason="shadow evaluation passed",
            ),
            expected_revision=revision,
        )
    result = registry.load_version(metadata.ref, compatibility)
    assert result.artifact is not None, result.detail
    return result.artifact


def rl_policy_config(**overrides: Any) -> tuple[RlPolicyConfig, str]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "artifact": {"model_id": "rl-supervisor-test"},
        "search": {
            "family": "per_regime_coordinate_v1",
            "seed": provisional(11),
            "max_candidate_tables": 512,
            "minimum_reward_improvement": provisional(0.0),
            "weight_candidates": [
                {
                    "gpu_temperature": 1.0,
                    "cpu_temperature": 0.2,
                    "balance": 0.2,
                    "acoustic": 0.2,
                    "change": 0.1,
                },
                {
                    "gpu_temperature": 0.6,
                    "cpu_temperature": 0.6,
                    "balance": 0.3,
                    "acoustic": 0.2,
                    "change": 0.6,
                },
            ],
        },
        "shadow": {
            "minimum_ticks": provisional(2),
            "minimum_paired_fraction": provisional(0.9),
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            document[key] = {**document[key], **value}
        else:
            document[key] = value
    return RlPolicyConfig.model_validate(document), sha256(b"rl-policy-test").hexdigest()


def trainer_for(
    environment: Any, settings: FanPolicyConfig, **config_overrides: Any
) -> SupervisorPolicyTrainer:
    config, digest = rl_policy_config(**config_overrides)
    return SupervisorPolicyTrainer(
        environment,
        policy_config=config,
        policy_config_sha256=digest,
        bounds=settings.supervisor.output_bounds,
        rule_config=settings.supervisor.rule_policy,
        rule_policy=RulePolicy(settings.supervisor.rule_policy, SimulatedClock(0)),
    )


@pytest.fixture
def bounds() -> SupervisorOutputBounds:
    return mpc_policy().supervisor.output_bounds


# ------------------------------------- 不変条件 1 / 2 / 3: artifact は戦略しか表せない


def test_invariant_1_the_policy_artifact_cannot_express_fan_demand(
    bounds: SupervisorOutputBounds,
) -> None:
    """**action を Demand に読み替える経路を型で塞ぐ**（AGENTS.md ルール1 / 2）。"""
    entry = table().entries[0].model_dump(mode="python")
    entry["demand"] = 0.8
    with pytest.raises(ValidationError):
        RegimeActionEntry.model_validate(entry)

    document = artifact(bounds).model_dump(mode="python")
    document["payload"]["pwm"] = 128
    with pytest.raises(ValidationError):
        SupervisorPolicyArtifact.model_validate(document)


def test_invariant_2_the_table_covers_every_regime_exactly_once() -> None:
    """**未知の regime を既定の欄へ倒せない。** 欠けた表も重複した表も受け取らない。"""
    entries = table().entries
    with pytest.raises(ValidationError, match="at least 9"):
        RegimeTablePayload.model_validate({"entries": entries[:-1]})
    with pytest.raises(ValidationError, match="全 regime"):
        RegimeTablePayload.model_validate({"entries": (entries[0], *entries[:-1])})
    # 並びを崩した表も、同じ内容で2通りの bytes を持たせないために拒む。
    with pytest.raises(ValidationError, match="全 regime"):
        RegimeTablePayload.model_validate({"entries": tuple(reversed(entries))})
    assert any(entry.regime is WorkloadRegime.UNKNOWN for entry in entries)


def test_invariant_3_the_manifest_names_only_its_own_payload(
    bounds: SupervisorOutputBounds,
) -> None:
    """**表だけ差し替えた artifact を、同じ identity のまま読み込ませない。**"""
    original = artifact(bounds)
    swapped = table(sustained_gpu=weights(gpu_temperature=0.1))
    with pytest.raises(ValidationError, match="payload checksum"):
        SupervisorPolicyArtifact.model_validate(
            {"manifest": original.manifest.model_dump(mode="python"), "payload": swapped}
        )


# ------------------------------------- 不変条件 4 / 5 / 6: 自称どうしの整合


def test_invariant_4_unbacked_policies_cannot_claim_more_than_shadow(
    bounds: SupervisorOutputBounds,
) -> None:
    """**反実仮想の裏づけの無い policy に SHADOW より上を名乗らせない。**

    いまは裏づけのある artifact が1つも無いので、この条件が常に効く（決定記録 0058 §3）。
    """
    with pytest.raises(ValidationError, match="SHADOW 互換"):
        artifact(
            bounds,
            authority_compatibility=(AuthorityStage.SHADOW, AuthorityStage.LIMITED),
        )
    with pytest.raises(ValidationError, match="SHADOW 互換"):
        artifact(bounds, authority_compatibility=(AuthorityStage.LIMITED,))


def test_invariant_5_counterfactual_backing_needs_every_condition() -> None:
    """**片方の条件だけで「反実仮想の裏づけあり」と書けない。**"""
    with pytest.raises(ValidationError, match="Learned MPC"):
        evidence(counterfactual_backed=True, promotable_episodes=2, improved_over_baseline=True)
    with pytest.raises(ValidationError, match="昇格可能でない"):
        evidence(
            counterfactual_backed=True,
            learned_controller_available=True,
            promotable_episodes=1,
            improved_over_baseline=True,
        )
    with pytest.raises(ValidationError, match="Baseline を上回って"):
        evidence(
            counterfactual_backed=True,
            learned_controller_available=True,
            promotable_episodes=2,
            improved_over_baseline=False,
        )
    backed = evidence(
        counterfactual_backed=True,
        learned_controller_available=True,
        promotable_episodes=2,
        improved_over_baseline=True,
    )
    assert backed.counterfactual_backed


def test_invariant_6_episode_counts_cannot_be_inflated_by_repeating_input(
    bounds: SupervisorOutputBounds,
) -> None:
    """**同じ episode を2度名指ししても総数が増えない。**"""
    with pytest.raises(ValidationError, match="重複なし昇順"):
        artifact(
            bounds,
            training_episode_ids=("ep-a", "ep-a"),
            training_evidence=evidence(total_episodes=2),
        )
    with pytest.raises(ValidationError, match="名指しした識別子の数"):
        artifact(
            bounds,
            training_episode_ids=("ep-a", "ep-b"),
            training_evidence=evidence(total_episodes=5),
        )


# ------------------------------------- 不変条件 7 / 8 / 9: 束縛は Registry の証拠を要求する


def test_invariant_7_binding_requires_a_registry_attestation(
    tmp_path: Path, bounds: SupervisorOutputBounds
) -> None:
    """**自作の「検証済みに見える」束では policy を束縛できない。**"""

    class FakeVerified:
        metadata = None
        payload = b"{}"
        attestation = None

    with pytest.raises(SupervisorPolicyUnusableError, match="VerifiedArtifact"):
        SupervisorPolicyBinding.for_shadow(
            FakeVerified(),  # type: ignore[arg-type]
            expected_policy_version="0.1.0",
            bounds=bounds,
        )

    thermal_kind = register_policy(
        tmp_path / "pr89-kind",
        artifact(bounds),
        kind=ArtifactKind.THERMAL_MODEL,
    )
    with pytest.raises(SupervisorPolicyUnusableError, match="supervisor policy 以外"):
        SupervisorPolicyBinding.for_shadow(
            thermal_kind, expected_policy_version="0.1.0", bounds=bounds
        )

    wrong_capability = register_policy(
        tmp_path / "pr89-capability",
        artifact(bounds),
        capability=ArtifactCapability.COUNTERFACTUAL_ACTION,
    )
    with pytest.raises(SupervisorPolicyUnusableError, match="Supervisor 戦略を申告して"):
        SupervisorPolicyBinding.for_shadow(
            wrong_capability, expected_policy_version="0.1.0", bounds=bounds
        )


def test_invariant_7_b_binding_refuses_a_version_the_runtime_did_not_expect(
    tmp_path: Path, bounds: SupervisorOutputBounds
) -> None:
    """**設定の `rl_version` と違う policy を読み込まない。**"""
    verified = register_policy(tmp_path / "pr89-version", artifact(bounds))
    with pytest.raises(SupervisorPolicyUnusableError, match="rl_version"):
        SupervisorPolicyBinding.for_shadow(verified, expected_policy_version="0.2.0", bounds=bounds)


def test_invariant_8_binding_requires_the_same_action_space(
    tmp_path: Path, bounds: SupervisorOutputBounds
) -> None:
    """**別の範囲で学習した policy を読み込まない。範囲外の欄も丸めない。**"""
    verified = register_policy(tmp_path / "pr89-space", artifact(bounds))
    narrowed = SupervisorOutputBounds.model_validate(
        {
            **bounds.model_dump(mode="python"),
            "strategies": ("balanced", "conservative"),
        }
    )
    with pytest.raises(SupervisorPolicyUnusableError, match="action 空間"):
        SupervisorPolicyBinding.for_shadow(
            verified, expected_policy_version="0.1.0", bounds=narrowed
        )

    # **hash が合っていても欄ごとに照合する。** 学習時と同じ範囲を名乗ったまま、
    # その範囲に収まらない表を持つ artifact を読み込ませない。
    tightened = SupervisorOutputBounds.model_validate(
        {
            **bounds.model_dump(mode="python"),
            "weights": {
                **bounds.weights.model_dump(mode="python"),
                "gpu_temperature": {"minimum": 0.7, "maximum": 1.0},
            },
        }
    )
    out_of_range = artifact(tightened, payload=table(idle=weights(gpu_temperature=0.5)))
    verified_range = register_policy(tmp_path / "pr89-range", out_of_range)
    with pytest.raises(SupervisorPolicyUnusableError, match="設定範囲外"):
        SupervisorPolicyBinding.for_shadow(
            verified_range, expected_policy_version="0.1.0", bounds=tightened
        )


def test_invariant_9_the_active_gate_is_closed_and_never_reads_the_self_claim(
    tmp_path: Path, bounds: SupervisorOutputBounds
) -> None:
    """**active への門は開かない。** artifact の自称で開く門を作らない。

    `counterfactual_backed` は artifact が自分で書いた値である。自称を門の条件にすると、
    自称を書き換えれば開く門になる。だから門は入力を見ずに閉じる（決定記録 0061 §2.4）。
    """
    candidate = register_policy(tmp_path / "pr89-candidate", artifact(bounds))
    with pytest.raises(SupervisorPolicyUnusableError, match="active slot へ束縛できない"):
        SupervisorPolicyBinding.for_active(
            candidate,
            expected_policy_version="0.1.0",
            bounds=bounds,
            authority_stage=AuthorityStage.SHADOW,
        )

    # **昇格済みでも、裏づけを自称していても開かない。** 自称を読んでいない証拠として、
    # 「通りそうな」artifact を2通り用意しても同じ理由で拒まれることを確かめる。
    promoted = register_policy(tmp_path / "pr89-promoted", artifact(bounds), promoted=True)
    claiming = artifact(
        bounds,
        training_evidence=evidence(
            counterfactual_backed=True,
            learned_controller_available=True,
            promotable_episodes=2,
            improved_over_baseline=True,
        ),
        authority_compatibility=(AuthorityStage.SHADOW, AuthorityStage.LIMITED),
    )
    claimed = register_policy(
        tmp_path / "pr89-claimed",
        claiming,
        authority=(AuthorityStage.SHADOW, AuthorityStage.LIMITED),
        stage=AuthorityStage.LIMITED,
        promoted=True,
    )
    for verified in (promoted, claimed):
        with pytest.raises(SupervisorPolicyUnusableError, match="自称は根拠にしない"):
            SupervisorPolicyBinding.for_active(
                verified,
                expected_policy_version="0.1.0",
                bounds=bounds,
                authority_stage=AuthorityStage.LIMITED,
            )

    # shadow は昇格前の candidate をそのまま回せる。回せないと証拠を集められない。
    binding = SupervisorPolicyBinding.for_shadow(
        candidate, expected_policy_version="0.1.0", bounds=bounds
    )
    assert binding.intent is PolicyBindingIntent.SHADOW
    assert binding.origin is SupervisorOutputOrigin.SHADOW_BINDING


def test_invariant_9_b_a_shadow_bound_policy_cannot_reach_the_active_slot(
    tmp_path: Path, bounds: SupervisorOutputBounds
) -> None:
    """**用途が値に付いて回る。** shadow 用の policy の提案は active slot を通らない。"""
    settings = shadow_settings(mpc_policy(), rl_version="0.1.0")
    verified = register_policy(tmp_path / "pr89-origin", artifact(bounds))
    policy = RegimeTableRlPolicy.from_binding(
        SupervisorPolicyBinding.for_shadow(
            verified, expected_policy_version="0.1.0", bounds=bounds
        ),
        SimulatedClock(0),
    )
    assert policy.origin is SupervisorOutputOrigin.SHADOW_BINDING

    policy_input = supervisor_input()
    candidate = policy.deliver(
        policy_input, received_monotonic_ms=policy_input.snapshot.monotonic_ms
    )
    assert candidate.origin is SupervisorOutputOrigin.SHADOW_BINDING

    active_settings = SupervisorConfig.model_validate(
        settings.supervisor.model_dump(mode="python")
        | {"active_policy": SupervisorPolicyKind.RL, "shadow_policy": None}
    )
    decision = SupervisorCoordinator(active_settings, SimulatedClock(0)).evaluate(
        policy_input,
        now_monotonic_ms=policy_input.snapshot.monotonic_ms,
        rl_candidate=candidate,
    )

    assert decision.active.output is None
    assert decision.active.error is not None
    assert decision.active.error.code == "supervisor_origin_not_active"
    assert decision.selected_output is not None
    assert decision.selected_output.policy is SupervisorPolicyKind.RULE

    # Registry を通さない offline の instance は、用途すら名乗れない。
    offline = RegimeTableRlPolicy.offline(artifact(bounds), SimulatedClock(0), bounds=bounds)
    assert offline.origin is SupervisorOutputOrigin.UNVERIFIED


# ------------------------------------- 不変条件 10: #88 の interface を満たす


def test_invariant_10_the_policy_satisfies_the_supervisor_interface(
    tmp_path: Path, bounds: SupervisorOutputBounds
) -> None:
    """**Rule / RL が同じ入力から同じ schema を返す**（#88）。出力は範囲内に収まる。"""
    verified = register_policy(tmp_path / "pr89-interface", artifact(bounds))
    binding = SupervisorPolicyBinding.for_shadow(
        verified, expected_policy_version="0.1.0", bounds=bounds
    )
    policy = RegimeTableRlPolicy.from_binding(binding, SimulatedClock(1_000))
    assert policy.kind is SupervisorPolicyKind.RL
    assert policy.version == "0.1.0"
    assert policy.verification is PolicyArtifactVerification.REGISTRY_VERIFIED

    settings = mpc_policy()
    policy_input = supervisor_input()
    output = policy.propose(policy_input)
    assert output.policy is SupervisorPolicyKind.RL
    assert output.regime is policy_input.workload.regime
    settings.supervisor.output_bounds.validate_context(
        strategy=output.strategy, weights=output.weights, target_band=output.target_band
    )
    # ShadowRLPolicy は RL の出力しか通さない（#88）。
    assert ShadowRLPolicy(policy).propose(policy_input).policy is SupervisorPolicyKind.RL

    offline = RegimeTableRlPolicy.offline(binding.artifact, SimulatedClock(0), bounds=bounds)
    assert offline.verification is PolicyArtifactVerification.OFFLINE_UNVERIFIED
    assert offline.binding is None
    with pytest.raises(TypeError, match="from_binding"):
        RegimeTableRlPolicy(binding.artifact, SimulatedClock(0))


def test_invariant_10_b_the_shadow_slot_never_becomes_active(
    tmp_path: Path, bounds: SupervisorOutputBounds
) -> None:
    """**shadow の提案は active にならない。** Rule が active のまま trace に両方残る。"""
    settings = shadow_settings(mpc_policy(), rl_version="0.1.0")
    verified = register_policy(tmp_path / "pr89-shadow-slot", artifact(bounds))
    binding = SupervisorPolicyBinding.for_shadow(
        verified, expected_policy_version="0.1.0", bounds=settings.supervisor.output_bounds
    )
    policy = RegimeTableRlPolicy.from_binding(binding, SimulatedClock(0))
    policy_input = supervisor_input()
    candidate = received(policy.propose(policy_input))

    coordinator = SupervisorCoordinator(settings.supervisor, SimulatedClock(0))
    decision = coordinator.evaluate(
        policy_input, now_monotonic_ms=policy_input.snapshot.monotonic_ms, rl_candidate=candidate
    )
    assert decision.active.policy is SupervisorPolicyKind.RULE
    assert decision.shadow is not None and decision.shadow.output is not None
    assert decision.selected_output is not None
    assert decision.selected_output.policy is SupervisorPolicyKind.RULE


# ------------------------------------- 不変条件 11 / 12 / 13: shadow 台帳


def test_invariant_11_repeating_the_same_tick_does_not_inflate_the_counts() -> None:
    """**冪等な取り込みで数が増えない。食い違う重複は受け取らない**（決定記録 0055 §2.1）。"""
    ledger = ledger_for()
    decision = paired_decision(tick_id=1)
    ledger.observe(decision)
    ledger.observe(decision)
    summary = ledger.summary()
    assert (summary.observed_ticks, summary.paired_ticks) == (1, 1)

    conflicting = paired_decision(tick_id=1, rl_strategy="conservative")
    with pytest.raises(SupervisorShadowConflictError, match="食い違う"):
        ledger.observe(conflicting)


def test_invariant_12_partial_evidence_is_never_counted_as_agreement() -> None:
    """**片方しか無い tick を一致として数えない。** 下限未満の集計は `usable=False`。"""
    ledger = ledger_for()
    ledger.observe(paired_decision(tick_id=1))
    ledger.observe(missing_rl_decision(tick_id=2))
    summary = ledger.summary()
    assert summary.observed_ticks == 2
    assert summary.paired_ticks == 1
    assert summary.rl_unavailable_ticks == 1
    assert summary.strategy_matches == 1
    assert summary.rl_errors == {"supervisor_expired": 1}
    assert summary.paired_fraction == 0.5
    # minimum_paired_fraction=0.9 を下回るので、比較の証拠として読めない。
    assert summary.usable is False

    full = ledger_for()
    for tick in range(4):
        full.observe(paired_decision(tick_id=tick))
    assert full.summary().usable is True


def test_invariant_12_b_a_regime_mismatch_never_reaches_the_ledger() -> None:
    """**違う前提の提案どうしを比べない。**

    同じ tick の active / shadow は同じ regime を使う、という #88 の不変条件が先に効く。
    台帳が「違う regime の対」を一致として数える余地は、そもそも作れない。
    """
    with pytest.raises(ValidationError, match="同じ workload regime"):
        paired_decision(tick_id=1, rl_regime=WorkloadRegime.IDLE)
    assert ledger_for().summary().strategy_agreement_fraction is None


def test_invariant_13_the_ledger_binds_versions_and_reads_time_from_evidence() -> None:
    """**版を名指しし、時刻は観測した decision から取る**（壁時計を読まない）。"""
    ledger = ledger_for()
    with pytest.raises(SupervisorShadowUsageError, match="RL policy の版"):
        ledger.observe(paired_decision(tick_id=1, rl_version="9.9.9"))
    with pytest.raises(SupervisorShadowUsageError, match="Rule policy の版"):
        ledger.observe(paired_decision(tick_id=2, rule_version="rule-other"))
    with pytest.raises(SupervisorShadowUsageError, match="shadow slot の無い"):
        ledger.observe(rule_only_decision(tick_id=3))

    ledger.observe(paired_decision(tick_id=4, ts_ms=5_000))
    ledger.observe(paired_decision(tick_id=5, ts_ms=1_000))
    summary = ledger.summary()
    assert (summary.first_ts_ms, summary.last_ts_ms) == (1_000, 5_000)
    assert summary.evaluation_ref().startswith("supervisor-shadow:")
    assert summary.digest() == ledger.summary().digest()

    empty = ledger_for().summary()
    assert empty.first_ts_ms is None and empty.paired_fraction is None and empty.usable is False


# ------------------------------------- 不変条件 14 / 15 / 16 / 17: 探索と artifact


def test_invariant_14_the_search_is_deterministic_and_order_independent(trained: Any) -> None:
    """**同じ入力からは同じ報告。** 評価順（seed）を変えても選ばれる policy は同じ。"""
    specs = (episode_spec(episode_id="pr89-a", seed=3), episode_spec(episode_id="pr89-b", seed=4))
    first = report_for(trained, specs)
    again = report_for(trained, specs)
    assert first.digest() == again.digest()

    shuffled = report_for(trained, specs, search={"seed": provisional(999)})
    assert shuffled.candidate_order != first.candidate_order
    assert shuffled.selected_candidate_id == first.selected_candidate_id
    assert shuffled.artifact.payload == first.artifact.payload


def test_invariant_15_without_an_improvement_the_baseline_table_is_selected(
    trained: Any,
) -> None:
    """**Baseline を上回らなければ Baseline の表が選ばれる。**

    Learned MPC を束縛できない間は全 arm の requested が同一になるので、
    どの候補も差を作れない（決定記録 0058 §3）。
    """
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = report_for(trained, specs)
    assert report.learned_controller_available is False
    assert report.selected_candidate_id == BASELINE_CANDIDATE_ID
    assert report.improved_over_baseline is False
    assert all(not outcome.improved for outcome in report.outcomes)
    assert report.comparison.learned_controller_available is False

    # **「比べた」と読ませない。** action が demand に効かない run では、候補は
    # `comparable=False` になり、理由が残る。この欄で絞った読み手が
    # 「policy を比較した結果」として受け取ることがない。
    assert all(not outcome.comparable for outcome in report.outcomes)
    assert {outcome.rejection.code for outcome in report.outcomes if outcome.rejection} == {
        "learned_controller_unavailable"
    }
    assert all(outcome.mean_matched_reward is None for outcome in report.outcomes)


def test_invariant_16_an_unbacked_search_can_never_be_promotable(trained: Any) -> None:
    """**昇格の根拠にできる結果が出ない。** artifact は SHADOW 互換だけになる。"""
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = report_for(trained, specs)
    assert report.promotable is False
    evidence_claims = report.artifact.manifest.training_evidence
    assert evidence_claims.counterfactual_backed is False
    assert evidence_claims.learned_controller_available is False
    assert report.artifact.manifest.authority_compatibility == (AuthorityStage.SHADOW,)

    # SHADOW より上を要求しても、artifact の不変条件が先に落とす。
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    with pytest.raises(ValidationError, match="SHADOW 互換"):
        trainer_for(environment, settings).train(
            specs,
            model_version="0.1.0",
            created_at=CREATED_AT,
            authority_compatibility=(AuthorityStage.SHADOW, AuthorityStage.LIMITED),
        )


def test_invariant_16_b_an_approximate_simulator_is_not_promotable_even_with_an_mpc(
    trained: Any,
) -> None:
    """**近似 simulator の上では、MPC を束縛できても昇格の根拠にならない**（0058 §2.3）。"""
    environment, _config, settings, _safety = build_environment(trained, with_mpc=True)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer_for(environment, settings).train(
        specs, model_version="0.1.0", created_at=CREATED_AT
    )
    assert report.learned_controller_available is True
    # MPC を束縛できた run では候補を実際に比べている（`comparable=True`）。
    # それでも近似 simulator の episode は promotable にならない。
    assert all(outcome.comparable for outcome in report.outcomes)
    assert all(not episode.promotable for arm in report.comparison.arms for episode in arm.episodes)
    assert report.promotable is False
    assert report.artifact.manifest.authority_compatibility == (AuthorityStage.SHADOW,)


def test_invariant_17_the_artifact_reproduces_the_selected_strategy(
    tmp_path: Path, trained: Any
) -> None:
    """**artifact から作り直した policy が、選ばれた表と同じ戦略を返す。**"""
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    trainer = trainer_for(environment, settings)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)

    policy = RegimeTableRlPolicy.offline(
        report.artifact, SimulatedClock(0), bounds=settings.supervisor.output_bounds
    )
    baseline_table = trainer.baseline_table()
    for regime in WorkloadRegime:
        output = policy.propose(supervisor_input(regime=regime))
        expected = baseline_table.entry(regime)
        assert (output.strategy, output.weights, output.target_band) == (
            expected.strategy,
            expected.weights,
            expected.target_band,
        )

    # 登録 → 束縛まで往復できる（#89 受入基準「#104 へ登録可能な metadata を出力する」）。
    verified = register_policy(tmp_path / "pr89-roundtrip", report.artifact)
    binding = SupervisorPolicyBinding.for_shadow(
        verified,
        expected_policy_version="0.1.0",
        bounds=settings.supervisor.output_bounds,
    )
    assert binding.artifact == report.artifact


def test_candidates_are_bounded_and_never_silently_truncated(trained: Any) -> None:
    """**候補が上限を超えたら切り詰めず落とす。** 報告に出ない候補を作らない。"""
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings, search={"max_candidate_tables": 2})
    with pytest.raises(SupervisorPolicyTrainingError, match="上限を超えた"):
        trainer.candidates()


def test_the_trainer_refuses_a_baseline_that_is_not_the_rule_policy(trained: Any) -> None:
    """**比較の起点を取り違えない。** RL policy を Baseline slot に置かせない。"""
    from test_rl_training_environment import StubRlPolicy

    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    config, digest = rl_policy_config()
    with pytest.raises(SupervisorPolicyTrainingError, match="RulePolicy"):
        SupervisorPolicyTrainer(
            environment,
            policy_config=config,
            policy_config_sha256=digest,
            bounds=settings.supervisor.output_bounds,
            rule_config=settings.supervisor.rule_policy,
            rule_policy=StubRlPolicy(weights()),
        )


def test_the_evaluate_entry_point_compares_one_policy_against_the_rule_baseline(
    trained: Any,
) -> None:
    """**評価の入口は #105 の環境を通る。** 条件も母集団も arm 間で揃う。"""
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    policy = RegimeTableRlPolicy.offline(
        artifact(settings.supervisor.output_bounds),
        SimulatedClock(0),
        bounds=settings.supervisor.output_bounds,
    )
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    comparison = trainer.evaluate(policy, specs)
    assert {arm.policy for arm in comparison.arms} == {
        SupervisorPolicyKind.RULE,
        SupervisorPolicyKind.RL,
    }
    assert comparison.comparable_episode_ids == ("pr89-a",)
    with pytest.raises(SupervisorPolicyTrainingError, match="episode の無い"):
        trainer.evaluate(policy, ())


def test_the_packaged_policy_config_loads_and_stays_provisional() -> None:
    """`config/rl-policy.yaml` が検証を通る（**既定値はコードに無い**。AGENTS.md ルール9）。

    実測前の値がすべて `provisional` のままであることも確かめる。confirmed へ変わるのは
    基準となる測定が済んだときで、その判断は人が行う。
    """
    path = Path(__file__).resolve().parents[1] / "config" / "rl-policy.yaml"
    config, digest = RlPolicyConfig.from_file(path)

    assert config.schema_version == 1
    assert len(digest) == 64
    assert config.search.family == "per_regime_coordinate_v1"
    assert config.search.seed.status == "provisional"
    assert config.shadow.minimum_ticks.status == "provisional"
    assert config.shadow.minimum_paired_fraction.status == "provisional"


def test_duplicate_weight_candidates_are_refused() -> None:
    """**同じ候補を2度並べない。** 候補数だけが増えて探索の内容が変わらない。"""
    candidate = {
        "gpu_temperature": 1.0,
        "cpu_temperature": 0.2,
        "balance": 0.2,
        "acoustic": 0.2,
        "change": 0.1,
    }
    with pytest.raises(ValidationError, match="重複させない"):
        rl_policy_config(search={"weight_candidates": [candidate, dict(candidate)]})


SUPERVISOR_PACKAGE = (
    Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control" / "supervisor"
)

FORBIDDEN_MODULES = (
    "coldaisle.control.hardware",
    "serial",
    "subprocess",
)
"""Supervisor 層が import してはいけない module（AGENTS.md ルール1 / 2 / 6）。"""


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def test_the_supervisor_package_cannot_reach_the_actuation_path() -> None:
    """**Supervisor から Fan へ届く経路を作らない。** 出せるのは戦略までである。"""
    offenders = [
        f"{path.name}: {name}"
        for path in sorted(SUPERVISOR_PACKAGE.glob("*.py"))
        for name in _imported_modules(path)
        if any(
            name == forbidden or name.startswith(f"{forbidden}.") for forbidden in FORBIDDEN_MODULES
        )
    ]
    assert offenders == []


def test_the_policy_modules_never_name_pwm_or_effective_demand() -> None:
    """**policy の型に PWM も `EffectiveZoneDemand` も現れない**（決定記録 0028 §2.3）。

    走査するのは識別子だけである（散文で「表現できない」と書くのは禁じない）。
    """
    offenders = []
    for path in sorted(SUPERVISOR_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        offenders.extend(
            f"{path.name}: {name}"
            for name in sorted(names)
            if "pwm" in name.lower() or name == "EffectiveZoneDemand"
        )
    assert offenders == []


# ---------------------------------------------------------------- 小道具


def supervisor_input(*, regime: WorkloadRegime = WorkloadRegime.SUSTAINED_GPU) -> SupervisorInput:
    """policy が読む #88 の入力。**Demand の欄を持たない。**"""
    snapshot = ControlStateSnapshot(
        tick_id=7,
        ts_ms=1_700_000_000_000,
        monotonic_ms=1_700_000_000_000,
        signals=(),
        derived=(),
        trends=(),
        telemetry_health=TelemetryHealth.NORMAL,
        critical_unavailable=(),
    )
    return SupervisorInput(
        snapshot=snapshot,
        workload=WorkloadRegimeEstimate(
            regime=regime,
            confidence=0.9,
            reason=RegimeReason.OBSERVED_HISTORY,
            as_of_tick_id=snapshot.tick_id,
            computed_at_ms=snapshot.ts_ms,
            evidence=RegimeEvidence(
                cpu_power_mean_w=120.0,
                gpu_power_mean_w=320.0,
                observed_window_ms=60_000,
            ),
        ),
    )


def shadow_settings(settings: FanPolicyConfig, *, rl_version: str) -> FanPolicyConfig:
    """shadow slot に RL を置いた設定を作る（`SupervisorConfig` の検証をそのまま通す）。"""
    document = settings.supervisor.model_dump(mode="python")
    document["shadow_policy"] = SupervisorPolicyKind.RL
    document["rl_version"] = rl_version
    supervisor = SupervisorConfig.model_validate(document)
    policy_document = settings.model_dump(mode="python")
    policy_document["supervisor"] = supervisor
    return FanPolicyConfig.model_validate(policy_document)


def received(output: SupervisorOutput) -> ReceivedSupervisorOutput:
    return ReceivedSupervisorOutput(
        output=output,
        source_monotonic_ms=output.ts_ms,
        received_monotonic_ms=output.ts_ms,
    )


def ledger_for() -> SupervisorShadowLedger:
    config, _digest = rl_policy_config()
    return SupervisorShadowLedger(
        config.shadow, rule_policy_version="rule-test-v1", rl_policy_version="0.1.0"
    )


def supervisor_output(
    *,
    tick_id: int,
    ts_ms: int,
    policy: SupervisorPolicyKind,
    version: str,
    regime: WorkloadRegime,
    strategy: str,
) -> SupervisorOutput:
    return SupervisorOutput(
        snapshot_schema_version=1,
        tick_id=tick_id,
        ts_ms=ts_ms,
        policy=policy,
        version=version,
        regime=regime,
        regime_confidence=0.9,
        weights=weights(),
        strategy=strategy,
        target_band=target_band(),
        computed_at_ms=ts_ms,
    )


def paired_decision(
    *,
    tick_id: int,
    ts_ms: int = 1_000,
    rule_version: str = "rule-test-v1",
    rl_version: str = "0.1.0",
    rl_strategy: str = "balanced",
    rl_regime: WorkloadRegime = WorkloadRegime.SUSTAINED_GPU,
) -> SupervisorDecision:
    return SupervisorDecision(
        tick_id=tick_id,
        ts_ms=ts_ms,
        snapshot_schema_version=1,
        active=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RULE,
            output=supervisor_output(
                tick_id=tick_id,
                ts_ms=ts_ms,
                policy=SupervisorPolicyKind.RULE,
                version=rule_version,
                regime=WorkloadRegime.SUSTAINED_GPU,
                strategy="balanced",
            ),
        ),
        shadow=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RL,
            output=supervisor_output(
                tick_id=tick_id,
                ts_ms=ts_ms,
                policy=SupervisorPolicyKind.RL,
                version=rl_version,
                regime=rl_regime,
                strategy=rl_strategy,
            ),
            received_monotonic_ms=ts_ms,
            source_monotonic_ms=ts_ms,
        ),
    )


def missing_rl_decision(*, tick_id: int, ts_ms: int = 2_000) -> SupervisorDecision:
    return SupervisorDecision(
        tick_id=tick_id,
        ts_ms=ts_ms,
        snapshot_schema_version=1,
        active=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RULE,
            output=supervisor_output(
                tick_id=tick_id,
                ts_ms=ts_ms,
                policy=SupervisorPolicyKind.RULE,
                version="rule-test-v1",
                regime=WorkloadRegime.SUSTAINED_GPU,
                strategy="balanced",
            ),
        ),
        shadow=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RL,
            error=Reason(code="supervisor_expired", detail="RLPolicy output の有効期限切れ"),
        ),
    )


def rule_only_decision(*, tick_id: int, ts_ms: int = 3_000) -> SupervisorDecision:
    return SupervisorDecision(
        tick_id=tick_id,
        ts_ms=ts_ms,
        snapshot_schema_version=1,
        active=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RULE,
            output=supervisor_output(
                tick_id=tick_id,
                ts_ms=ts_ms,
                policy=SupervisorPolicyKind.RULE,
                version="rule-test-v1",
                regime=WorkloadRegime.SUSTAINED_GPU,
                strategy="balanced",
            ),
        ),
    )


def report_for(models: Any, specs: Any, **config_overrides: Any) -> Any:
    environment, _config, settings, _safety = build_environment(models, with_mpc=False)
    trainer = trainer_for(environment, settings, **config_overrides)
    return trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)
