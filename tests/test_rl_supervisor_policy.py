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
import json
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
from coldaisle.control.rl.episode import TerminationReason
from coldaisle.control.rl.training import (
    SupervisorPolicyTrainer,
    SupervisorPolicyTrainingError,
    SupervisorPolicyTrainingReport,
    candidate_rejection,
    common_matched_steps,
    mean_reward_over,
    safety_observed_steps,
    scoring_horizon,
    short_episodes,
    truncated_episodes,
)
from coldaisle.control.schema import (
    SUPERVISOR_DECISION_SCHEMA_VERSION,
    AuthorityStage,
    Reason,
    SupervisorDecision,
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyEvaluation,
    SupervisorPolicyIdentity,
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
    SupervisorPolicyRegistryMetadata,
    SupervisorPolicyUnusableError,
    SupervisorShadowConflictError,
    SupervisorShadowLedger,
    SupervisorShadowUsageError,
    action_space_sha256,
)
from coldaisle.control.supervisor.artifact import (
    _derive_policy_registry_metadata,
    _policy_artifact_bytes,
)
from coldaisle.control.supervisor.policy_config import (
    BASELINE_CANDIDATE_ID,
    MAX_CANDIDATE_TABLES,
    MAX_POLICY_MODEL_ID_LENGTH,
    POLICY_VERSION_MAX_LENGTH,
)
from coldaisle.control.supervisor.regime import (
    RegimeEvidence,
    RegimeReason,
    WorkloadRegimeEstimate,
)
from coldaisle.control.supervisor.rl_policy import ARTIFACT_DETERMINED_METADATA_EXCLUSIONS
from coldaisle.control.supervisor.shadow import SupervisorShadowSummary, shadow_summary_usable
from test_learned_mpc import REGISTRY_LIMITS, mpc_policy
from test_rl_training_environment import build_environment, episode_spec
from test_rl_training_environment import trained as _trained_fixture

# #105 の合成モデル一式をそのまま使う。pytest は module 属性名で fixture を解決するので、
# 別名で import したうえでこの名前へ束ね直す（同じ dataset を2度作らないため）。
trained = _trained_fixture

CREATED_AT = "2026-09-21T10:00:00+09:00"
CONDITIONS = "a" * 64
RL_IDENTITY = SupervisorPolicyIdentity(
    model_id="rl-supervisor-test", version="0.1.0", artifact_sha256="d" * 64
)


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
    metadata_overrides: dict[str, Any] | None = None,
) -> VerifiedArtifact:
    """**本物の Model Registry（#104）へ登録し、検証経路から VerifiedArtifact を受け取る。**

    試験が自分で証拠を組み立てないようにする。
    """
    # **Registry へ直接書く経路を模す。** #104 の Registry は bytes を解釈しないので、照合を
    # 通していない artifact も書けてしまう（決定記録 0061 §2.6 の残余リスク）。束縛の試験は、
    # その書き込みを束縛側が正しく扱うかを見るので、登録用の公開関数を迂回する。
    artifact_bytes = payload or _policy_artifact_bytes(policy_artifact)
    derived = _derive_policy_registry_metadata(policy_artifact, artifact_bytes)
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
    if metadata_overrides:
        # **bytes はそのまま、metadata だけ書き換える。** checksum は合うので登録は通る。
        metadata = ArtifactMetadata.model_validate(
            metadata.model_dump(mode="python") | metadata_overrides
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
    candidate = policy.deliver(
        policy_input, received_monotonic_ms=policy_input.snapshot.monotonic_ms
    )

    coordinator = SupervisorCoordinator(
        settings.supervisor, SimulatedClock(0), expected_rl_identity=policy.identity
    )
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


def test_invariant_13_f_an_unusable_summary_issues_no_evaluation_ref() -> None:
    """**下限を満たさない集計は昇格の証拠にならない。** `shadow_evaluation_ref` を出さない。"""
    ledger = ledger_for()
    ledger.observe(paired_decision(tick_id=1))
    ledger.observe(missing_rl_decision(tick_id=2))
    unusable = ledger.summary()
    assert unusable.usable is False
    with pytest.raises(SupervisorShadowUsageError, match="昇格の証拠にしない"):
        unusable.evaluation_ref()

    usable_ledger = ledger_for()
    for tick in range(4):
        usable_ledger.observe(paired_decision(tick_id=tick))
    usable = usable_ledger.summary()
    assert usable.usable is True
    assert usable.evaluation_ref() == f"supervisor-shadow:{usable.digest()}"


def test_invariant_13_b_rule_and_rl_may_share_a_version_string() -> None:
    """**Rule と RL が同じ版文字列を名乗っても台帳を作れる。**

    版は policy kind ごとに照合し、RL 側は model ID・bytes hash まで束縛するので曖昧さは無い。
    """
    config, _digest = rl_policy_config()
    ledger = SupervisorShadowLedger(
        config.shadow, rule_policy_version="0.1.0", rl_identity=RL_IDENTITY
    )
    ledger.observe(paired_decision(tick_id=1, rule_version="0.1.0"))
    summary = ledger.summary()
    assert (summary.observed_ticks, summary.paired_ticks) == (1, 1)
    assert summary.rule_policy_version == summary.rl_policy_identity.version == "0.1.0"


def test_invariant_13_c_both_unavailable_keeps_the_rl_failure() -> None:
    """**Rule と RL が同じ tick で両方落ちても、RL の欠落理由を内訳に残す。**"""
    ledger = ledger_for()
    ledger.observe(paired_decision(tick_id=1))
    ledger.observe(missing_rl_decision(tick_id=2))
    ledger.observe(both_missing_decision(tick_id=3))
    summary = ledger.summary()
    assert summary.observed_ticks == 3
    assert summary.paired_ticks == 1
    assert summary.rule_unavailable_ticks == 0
    assert summary.rl_unavailable_ticks == 1
    assert summary.both_unavailable_ticks == 1
    assert summary.rl_errors == {"supervisor_expired": 1, "supervisor_worker_down": 1}

    with pytest.raises(ValidationError, match="RL 欠落の理由の内訳"):
        type(summary).model_validate(summary.model_dump(mode="python") | {"rl_errors": {}})


# ------------------------------------- 不変条件 14 / 15 / 16 / 17: 探索と artifact


def test_invariant_13_d_an_unpaired_ledger_is_never_usable() -> None:
    """**対が1つも無い集計を読めたことにしない**（fail closed）。

    設定も集計の型も `minimum_ticks` に 0 を許さない。判定の関数そのものも、
    下限の値によらず対が 0 の集計を読めるとは言わない。
    """
    with pytest.raises(ValidationError, match="minimum_ticks"):
        rl_policy_config(shadow={"minimum_ticks": provisional(0)})
    assert (
        shadow_summary_usable(
            observed_ticks=1, paired_ticks=0, minimum_ticks=0, minimum_paired_fraction=0.0
        )
        is False
    )

    # 設定を迂回して 0 の下限を渡しても、集計の型が受け取らない（読めたことにはならない）。
    config, _digest = rl_policy_config()
    zero = config.shadow.model_copy(
        update={
            "minimum_ticks": config.shadow.minimum_ticks.model_copy(update={"value": 0}),
            "minimum_paired_fraction": config.shadow.minimum_paired_fraction.model_copy(
                update={"value": 0.0}
            ),
        }
    )
    ledger = SupervisorShadowLedger(
        zero, rule_policy_version="rule-test-v1", rl_identity=RL_IDENTITY
    )
    ledger.observe(missing_rl_decision(tick_id=1))
    with pytest.raises(ValidationError, match="minimum_ticks"):
        ledger.summary()


def test_invariant_13_e_a_summary_cannot_claim_a_usable_flag_it_does_not_derive() -> None:
    """**`usable` は導いた値だけ。** 保存した集計を読み戻しても、台帳が立てない値を名乗れない。"""
    usable_ledger = ledger_for()
    for tick in range(4):
        usable_ledger.observe(paired_decision(tick_id=tick))
    usable = usable_ledger.summary()
    unusable_ledger = ledger_for()
    unusable_ledger.observe(missing_rl_decision(tick_id=1))
    unusable = unusable_ledger.summary()
    assert (usable.usable, unusable.usable) == (True, False)

    # 台帳が作った集計はそのまま往復できる。
    for summary in (usable, unusable):
        restored = SupervisorShadowSummary.model_validate_json(summary.model_dump_json())
        assert restored == summary and restored.digest() == summary.digest()

    # 対が 0 なのに usable=true を名乗る集計（下限 0 も含む）を受け取らない。
    forged = json.loads(unusable.model_dump_json())
    for document in (
        {**forged, "usable": True},
        {**forged, "usable": True, "minimum_ticks": 0, "minimum_paired_fraction": 0.0},
    ):
        with pytest.raises(ValidationError):
            SupervisorShadowSummary.model_validate_json(json.dumps(document))

    # 導いた値と違えば、どちら向きでも受け取らない。
    with pytest.raises(ValidationError, match="usable"):
        SupervisorShadowSummary.model_validate_json(
            json.dumps({**json.loads(usable.model_dump_json()), "usable": False})
        )


def test_the_training_report_cannot_claim_promotable_it_does_not_derive(trained: Any) -> None:
    """**報告の `promotable` は比較から導いた値だけ。** 読み戻しでも名乗れない。"""
    report = report_for(trained, (episode_spec(episode_id="pr89-a", seed=3),))
    assert report.promotable is False
    restored = SupervisorPolicyTrainingReport.model_validate_json(report.model_dump_json())
    assert restored.digest() == report.digest()
    with pytest.raises(ValidationError):
        SupervisorPolicyTrainingReport.model_validate_json(
            json.dumps({**json.loads(report.model_dump_json()), "promotable": True})
        )


def test_the_training_report_cannot_claim_an_improvement_it_does_not_derive(
    trained: Any,
) -> None:
    """**改善の判定は、報告の記録から導き直した値だけ。**

    選ばれた候補・報告・artifact の3か所を揃えて「改善した」と書き換えても、記録した
    安全側の数・reward・下限から導いた値と違えば受け取らない。
    """
    environment, _config, settings, _safety = build_environment(trained, with_mpc=True)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer_for(environment, settings).train(
        specs, model_version="0.1.0", created_at=CREATED_AT
    )
    assert all(outcome.comparable for outcome in report.outcomes)
    # 本物の報告はそのまま往復できる。
    restored = SupervisorPolicyTrainingReport.model_validate_json(report.model_dump_json())
    assert restored.digest() == report.digest()

    document = json.loads(report.model_dump_json())
    selected_index = next(
        index
        for index, outcome in enumerate(document["outcomes"])
        if outcome["candidate_id"] == report.selected_candidate_id
    )
    assert report.improved_over_baseline is False
    # 3か所を揃えて改善を名乗らせる（自称どうしは整合している）。
    tampered = json.loads(json.dumps(document))
    tampered["outcomes"][selected_index]["improved"] = True
    tampered["improved_over_baseline"] = True
    tampered["artifact"]["manifest"]["training_evidence"]["improved_over_baseline"] = True
    with pytest.raises(ValidationError, match="改善判定が記録から導いた値と一致しない"):
        SupervisorPolicyTrainingReport.model_validate_json(json.dumps(tampered))

    # 選ばれていない候補に改善を名乗らせても受け取らない。
    other_index = next(
        index for index in range(len(document["outcomes"])) if index != selected_index
    )
    tampered = json.loads(json.dumps(document))
    tampered["outcomes"][other_index]["improved"] = True
    with pytest.raises(ValidationError, match="導いた"):
        SupervisorPolicyTrainingReport.model_validate_json(json.dumps(tampered))


def test_the_artifact_is_bound_to_what_was_evaluated(trained: Any) -> None:
    """**artifact が「評価したもの」を名乗る欄は、比較の記録から導いた値と一致する。**

    形や数だけを照合すると、同じ形の別の表・同じ数の別の episode を名指しした artifact が
    `promotable` な報告のまま通ってしまう。
    """
    from coldaisle.control.model.thermal import canonical_sha256

    report = report_for(trained, (episode_spec(episode_id="pr89-a", seed=3),))
    restored = SupervisorPolicyTrainingReport.model_validate_json(report.model_dump_json())
    assert restored.digest() == report.digest()
    document = json.loads(report.model_dump_json())

    # 表を、形の正しい別の表へ差し替える（payload checksum も揃えて書き換える）。
    swapped_payload = report.artifact.payload.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(
                    update={
                        "weights": entry.weights.model_copy(
                            update={"change": entry.weights.change * 0.5}
                        )
                    }
                )
                for entry in report.artifact.payload.entries
            )
        }
    )
    assert canonical_sha256(swapped_payload) != report.artifact.manifest.payload_sha256
    tampered = json.loads(json.dumps(document))
    tampered["artifact"]["payload"] = json.loads(swapped_payload.model_dump_json())
    tampered["artifact"]["manifest"]["payload_sha256"] = canonical_sha256(swapped_payload)
    with pytest.raises(ValidationError, match="評価した表と一致しない"):
        SupervisorPolicyTrainingReport.model_validate_json(json.dumps(tampered))

    # 学習 episode を、同じ数の別の識別子へ差し替える。
    tampered = json.loads(json.dumps(document))
    tampered["artifact"]["manifest"]["training_episode_ids"] = ["pr89-z"]
    with pytest.raises(ValidationError, match="学習 episode が比較で回した episode と一致しない"):
        SupervisorPolicyTrainingReport.model_validate_json(json.dumps(tampered))


def test_certify_regenerates_the_selected_table_from_the_config(trained: Any) -> None:
    """**信頼の根は報告の外（設定と Rule policy）にある。** 登録の前に作り直して照合する。

    報告は JSON として書き換えられるので、訪れなかった regime の欄だけを変え、
    `payload_sha256` と `table_sha256` を揃えて書き換えた偽造は報告の検証を通る。
    設定から作り直した表とは食い違うので、`certify()` が拒む。
    """
    from coldaisle.control.model.thermal import canonical_sha256

    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)
    # 本物の報告は通り、作り直した表は artifact の表と同じ。
    assert trainer.certify(report).artifact == report.artifact
    assert trainer.regenerate_candidate_table(report.selected_candidate_id) == (
        report.artifact.payload
    )
    for identifier, table in trainer.candidates():
        assert trainer.regenerate_candidate_table(identifier) == table

    # 選ばれた arm が訪れなかった regime の欄だけを変える。
    visited = {
        step.regime for episode in report.comparison.arms[-1].episodes for step in episode.steps
    }
    unvisited = next(regime for regime in WorkloadRegime if regime not in visited)
    forged_payload = report.artifact.payload.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(
                    update={
                        "weights": entry.weights.model_copy(
                            update={"change": entry.weights.change * 0.5}
                        )
                    }
                )
                if entry.regime is unvisited
                else entry
                for entry in report.artifact.payload.entries
            )
        }
    )
    digest = canonical_sha256(forged_payload)
    document = json.loads(report.model_dump_json())
    document["artifact"]["payload"] = json.loads(forged_payload.model_dump_json())
    document["artifact"]["manifest"]["payload_sha256"] = digest
    for outcome in document["outcomes"]:
        if outcome["candidate_id"] == report.selected_candidate_id:
            outcome["table_sha256"] = digest
    # 報告の中の値どうしは揃っているので、報告としては読めてしまう。
    forged = SupervisorPolicyTrainingReport.model_validate_json(json.dumps(document))
    with pytest.raises(SupervisorPolicyTrainingError, match="作り直した"):
        trainer.certify(forged)

    # manifest の設定 hash と違う設定では作り直さない。
    config, _digest = rl_policy_config()
    other = SupervisorPolicyTrainer(
        environment,
        policy_config=config,
        policy_config_sha256="0" * 64,
        bounds=settings.supervisor.output_bounds,
        rule_policy=RulePolicy(settings.supervisor.rule_policy, SimulatedClock(0)),
    )
    with pytest.raises(SupervisorPolicyTrainingError, match="rl_policy_config_sha256"):
        other.certify(report)


def test_registration_accepts_only_a_certified_artifact(trained: Any) -> None:
    """**登録の道は `certify()` を通した型だけを受け取る**（慣習ではなく型で縛る）。"""
    from coldaisle.control.supervisor import (
        CertifiedPolicyArtifact,
        canonical_policy_artifact_bytes,
        policy_registry_metadata,
    )

    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    report = trainer.train(
        (episode_spec(episode_id="pr89-a", seed=3),), model_version="0.1.0", created_at=CREATED_AT
    )

    # 照合を通していない artifact は登録用の関数に渡せない。
    with pytest.raises(TypeError, match="certify"):
        canonical_policy_artifact_bytes(report.artifact)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="certify"):
        policy_registry_metadata(report.artifact, b"{}")  # type: ignore[arg-type]
    # 照合済みの型は certify() の外では作れない。
    with pytest.raises(TypeError, match="certify"):
        CertifiedPolicyArtifact(
            report.artifact,
            rl_policy_config_sha256=report.artifact.manifest.rl_policy_config_sha256,
            rl_training_config_sha256=report.artifact.manifest.rl_training_config_sha256,
            action_space_sha256=report.artifact.manifest.action_space_sha256,
        )

    # 本物の流れ: certify → bytes / metadata。
    certified = trainer.certify(report)
    artifact_bytes = canonical_policy_artifact_bytes(certified)
    metadata = policy_registry_metadata(certified, artifact_bytes)
    assert metadata.sha256 == sha256(artifact_bytes).hexdigest()
    assert metadata.source_runs == report.artifact.manifest.training_episode_ids
    with pytest.raises(AttributeError):
        certified._artifact = report.artifact  # type: ignore[misc]


def test_certify_replays_the_baseline_arm_against_the_rule_policy(trained: Any) -> None:
    """**Baseline は版ではなく、出した action で照合する。**

    同じ版を名乗る別の Rule policy の arm を比較に入れた報告は、報告としては読めても、
    この Rule policy の表が arm の action を再現しないので `certify()` が拒む。
    """
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    report = trainer.train(
        (episode_spec(episode_id="pr89-a", seed=3),), model_version="0.1.0", created_at=CREATED_AT
    )
    assert trainer.certify(report).artifact == report.artifact

    document = json.loads(report.model_dump_json())
    step = document["comparison"]["arms"][0]["episodes"][0]["steps"][0]
    step["action"]["weights"]["change"] = step["action"]["weights"]["change"] * 0.5
    forged = SupervisorPolicyTrainingReport.model_validate_json(json.dumps(document))
    with pytest.raises(SupervisorPolicyTrainingError, match="Baseline arm の action"):
        trainer.certify(forged)


def test_summary_counts_are_never_negative() -> None:
    """**件数は負にならない。** 負の内訳で合計だけを合わせた集計を受け取らない。"""
    ledger = ledger_for()
    ledger.observe(missing_rl_decision(tick_id=1))
    document = json.loads(ledger.summary().model_dump_json())
    assert document["rl_errors"] == {"supervisor_expired": 1}
    document["rl_errors"] = {"supervisor_expired": 2, "supervisor_unavailable": -1}
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        SupervisorShadowSummary.model_validate_json(json.dumps(document))


def test_shadow_evidence_is_bound_to_the_promoted_artifact(tmp_path: Path, trained: Any) -> None:
    """**別の artifact の shadow 集計を、この artifact の昇格の証拠にしない。**

    shadow の証拠は文字列ではなく集計で受け取り、集計の `rl_policy_identity` が
    照合済み artifact の完全な識別と一致しなければ、metadata にも昇格にも使わない。
    """
    from coldaisle.control.supervisor import (
        canonical_policy_artifact_bytes,
        certified_identity,
        policy_registry_metadata,
        promote_supervisor_policy,
    )

    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    report = trainer.train(
        (episode_spec(episode_id="pr89-a", seed=3),), model_version="0.1.0", created_at=CREATED_AT
    )
    certified = trainer.certify(report)
    identity = certified_identity(certified)

    def usable_summary(rl_identity: SupervisorPolicyIdentity) -> SupervisorShadowSummary:
        config, _digest = rl_policy_config()
        ledger = SupervisorShadowLedger(
            config.shadow, rule_policy_version="rule-test-v1", rl_identity=rl_identity
        )
        for tick in range(4):
            ledger.observe(
                paired_decision(
                    tick_id=tick, rl_version=rl_identity.version, rl_identity=rl_identity
                )
            )
        summary = ledger.summary()
        assert summary.usable is True
        return summary

    matching = usable_summary(identity)
    other = usable_summary(identity.model_copy(update={"artifact_sha256": "e" * 64}))

    artifact_bytes = canonical_policy_artifact_bytes(certified)
    with pytest.raises(ValueError, match="shadow 集計が比べた artifact"):
        policy_registry_metadata(certified, artifact_bytes, shadow_evidence=other)
    derived = policy_registry_metadata(certified, artifact_bytes, shadow_evidence=matching)
    assert derived.shadow_evaluation_ref == matching.evaluation_ref()

    # 本物の Registry へ登録し、policy 専用の入口から昇格する。
    registry = ModelRegistry(tmp_path / "pr89-promote", limits=REGISTRY_LIMITS)
    metadata = ArtifactMetadata(
        kind=ArtifactKind.SUPERVISOR_POLICY,
        artifact_format=ArtifactFormat.JSON,
        capability=ArtifactCapability.SUPERVISOR_STRATEGY,
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
        authority_compatibility=derived.authority_compatibility,
    )
    registry.register_candidate(metadata, artifact_bytes, actor="trainer", reason="certified")
    registry.mark_validated(
        metadata.ref,
        offline_evaluation_ref="evaluation/offline/89",
        actor="evaluator",
        reason="offline gates passed",
        expected_revision=registry.inspect().revision,
    )
    compatibility = ModelCompatibility(
        feature_schema_version=derived.feature_schema_version,
        target_schema_version=derived.target_schema_version,
        authority_stage=AuthorityStage.SHADOW,
    )
    revision = registry.inspect().revision
    approval = HumanApproval(
        action=ApprovalAction.PROMOTE,
        artifact=metadata.ref,
        artifact_sha256=metadata.sha256,
        expected_revision=revision,
        approver="model-operator",
        approved_at_ms=1_700_000_000_000,
        reason="shadow evaluation passed",
    )
    with pytest.raises(ValueError, match="shadow 集計が比べた artifact"):
        promote_supervisor_policy(
            registry,
            metadata.ref,
            compatibility,
            certified=certified,
            shadow_evidence=other,
            approval=approval,
            expected_revision=revision,
        )
    promote_supervisor_policy(
        registry,
        metadata.ref,
        compatibility,
        certified=certified,
        shadow_evidence=matching,
        approval=approval,
        expected_revision=revision,
    )
    promoted = registry.inspect().artifacts[metadata.ref.key]
    assert promoted.metadata.shadow_evaluation_ref == matching.evaluation_ref()


def test_supervisor_decision_v2_carries_identity_and_v1_still_reads() -> None:
    """**識別を足した decision は版を上げる。** 保存済みの v1 はそのまま読める。"""
    decision = paired_decision(tick_id=1)
    assert decision.schema_version == SUPERVISOR_DECISION_SCHEMA_VERSION == 2
    assert decision.shadow is not None and decision.shadow.policy_identity == RL_IDENTITY

    # v1 と名乗りながら v1 に無かった欄を持つ記録は作れない。
    dumped = decision.model_dump(mode="json")
    with pytest.raises(ValidationError, match="schema version 2"):
        SupervisorDecision.model_validate_json(json.dumps({**dumped, "schema_version": 1}))

    # 識別を持たない評価は欄を書き出さないので、v1 の形のまま残り、v1 として読める。
    legacy = missing_rl_decision(tick_id=2)
    legacy_dump = legacy.model_dump(mode="json")
    assert "policy_identity" not in legacy_dump["active"]
    assert "policy_identity" not in legacy_dump["shadow"]
    restored = SupervisorDecision.model_validate_json(
        json.dumps({**legacy_dump, "schema_version": 1})
    )
    assert restored.schema_version == 1


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
    assert all(outcome.mean_reward_over_common_horizon is None for outcome in report.outcomes)


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


def test_invariant_21_shadow_evidence_carries_the_full_artifact_identity(
    tmp_path: Path, bounds: SupervisorOutputBounds
) -> None:
    """**同じ版を名乗る別の artifact を、同じ証拠として数えない**（#89 レビュー）。

    `SupervisorOutput.version` は semantic version だけである。model ID と bytes hash を
    出力に運び、Coordinator と台帳の両方で完全一致を要求する。
    """
    settings = shadow_settings(mpc_policy(), rl_version="0.1.0")
    verified = register_policy(tmp_path / "pr89-ident", artifact(bounds))
    binding = SupervisorPolicyBinding.for_shadow(
        verified, expected_policy_version="0.1.0", bounds=bounds
    )
    policy = RegimeTableRlPolicy.from_binding(binding, SimulatedClock(0))
    identity = policy.identity
    assert identity.model_id == "rl-supervisor-test"
    assert identity.version == "0.1.0"
    assert identity.artifact_sha256 == binding.conditions()["policy_artifact_sha256"]
    # offline の instance も、同じ bytes なら同じ識別になる。
    assert (
        RegimeTableRlPolicy.offline(binding.artifact, SimulatedClock(0), bounds=bounds).identity
        == identity
    )

    policy_input = supervisor_input()
    delivered = policy.deliver(
        policy_input, received_monotonic_ms=policy_input.snapshot.monotonic_ms
    )
    assert delivered.identity == identity

    # Coordinator: 期待する識別と完全一致するものだけが通る。
    def decide(expected: SupervisorPolicyIdentity, candidate: ReceivedSupervisorOutput):
        return SupervisorCoordinator(
            settings.supervisor, SimulatedClock(0), expected_rl_identity=expected
        ).evaluate(
            policy_input,
            now_monotonic_ms=policy_input.snapshot.monotonic_ms,
            rl_candidate=candidate,
        )

    ok = decide(identity, delivered)
    assert ok.shadow is not None and ok.shadow.policy_identity == identity

    for other in (
        identity.model_copy(update={"model_id": "rl-supervisor-other"}),
        identity.model_copy(update={"artifact_sha256": "e" * 64}),
    ):
        # 同じ版・別の model ID / 別の bytes。版だけの照合では通っていた。
        swapped = decide(other, delivered)
        assert swapped.shadow is not None and swapped.shadow.output is None
        assert swapped.shadow.error is not None
        assert swapped.shadow.error.code == "supervisor_identity_mismatch"
    unlabeled = delivered.model_copy(update={"identity": None})
    missing = decide(identity, unlabeled)
    assert missing.shadow is not None and missing.shadow.output is None

    # 台帳: 完全一致しない提案・識別の無い提案は数えない。
    ledger = ledger_for()
    ledger.observe(paired_decision(tick_id=1))
    for other in (
        RL_IDENTITY.model_copy(update={"model_id": "rl-supervisor-other"}),
        RL_IDENTITY.model_copy(update={"artifact_sha256": "e" * 64}),
        None,
    ):
        with pytest.raises(SupervisorShadowUsageError, match="artifact 識別"):
            ledger.observe(paired_decision(tick_id=2, rl_identity=other))
    summary = ledger.summary()
    assert summary.observed_ticks == 1
    assert summary.rl_policy_identity == RL_IDENTITY
    # 別の identity の集計は digest（= shadow_evaluation_ref）も別になる。
    other_ledger = SupervisorShadowLedger(
        rl_policy_config()[0].shadow,
        rule_policy_version="rule-test-v1",
        rl_identity=RL_IDENTITY.model_copy(update={"artifact_sha256": "e" * 64}),
    )
    assert other_ledger.summary().digest() != ledger_for().summary().digest()


@pytest.mark.parametrize("slot", ["shadow", "active"])
def test_invariant_21_b_an_unbound_coordinator_refuses_every_rl_proposal(
    tmp_path: Path, bounds: SupervisorOutputBounds, slot: str
) -> None:
    """**照合する識別を持たない Coordinator は RL 提案を通さない**（fail closed。0061 §2.6）。

    照合を省けば、同じ版を名乗る別の artifact や識別の無い提案が trace に入る。
    active slot なら Rule へ落ちる。
    """
    settings = shadow_settings(mpc_policy(), rl_version="0.1.0")
    supervisor = settings.supervisor
    if slot == "active":
        supervisor = SupervisorConfig.model_validate(
            supervisor.model_dump(mode="python")
            | {"active_policy": SupervisorPolicyKind.RL, "shadow_policy": None}
        )
    verified = register_policy(tmp_path / f"pr89-unbound-{slot}", artifact(bounds))
    binding = SupervisorPolicyBinding.for_shadow(
        verified, expected_policy_version="0.1.0", bounds=supervisor.output_bounds
    )
    policy = RegimeTableRlPolicy.from_binding(binding, SimulatedClock(0))
    policy_input = supervisor_input()
    delivered = policy.deliver(
        policy_input, received_monotonic_ms=policy_input.snapshot.monotonic_ms
    )
    # active slot の用途検査より先で落ちないよう、用途だけは active と名乗らせる。
    delivered = delivered.model_copy(update={"origin": SupervisorOutputOrigin.ACTIVE_BINDING})

    for candidate in (delivered, delivered.model_copy(update={"identity": None})):
        decision = SupervisorCoordinator(supervisor, SimulatedClock(0)).evaluate(
            policy_input,
            now_monotonic_ms=policy_input.snapshot.monotonic_ms,
            rl_candidate=candidate,
        )
        rl = decision.shadow if slot == "shadow" else decision.active
        assert rl is not None and rl.output is None
        assert rl.error is not None and rl.error.code == "supervisor_identity_mismatch"
        assert decision.selected_output is not None
        assert decision.selected_output.policy is SupervisorPolicyKind.RULE


def test_invariant_18_the_baseline_table_comes_from_the_policy_that_was_evaluated(
    trained: Any,
) -> None:
    """**報告に載る Baseline と `baseline` 候補の表が別物にならない。**

    設定と policy を別々に受け取っていた頃は、版だけ同じで context の違う policy を渡すと、
    `baseline` 候補の表（設定から）と Baseline arm（policy から）が食い違った。
    表は**評価する policy そのものに聞いて**作る。
    """
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    divergent_weights = weights(acoustic=0.1, change=0.9)

    class DivergentRulePolicy:
        """設定と同じ版を名乗りながら、違う context を返す Rule policy。"""

        kind = SupervisorPolicyKind.RULE

        @property
        def version(self) -> str:
            return settings.supervisor.rule_policy.version

        def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
            snapshot = policy_input.snapshot
            return SupervisorOutput(
                snapshot_schema_version=snapshot.schema_version,
                tick_id=snapshot.tick_id,
                ts_ms=snapshot.ts_ms,
                policy=SupervisorPolicyKind.RULE,
                version=self.version,
                regime=policy_input.workload.regime,
                regime_confidence=policy_input.workload.confidence,
                weights=divergent_weights,
                strategy="balanced",
                target_band=target_band(),
                computed_at_ms=snapshot.ts_ms,
            )

    config, digest = rl_policy_config()
    trainer = SupervisorPolicyTrainer(
        environment,
        policy_config=config,
        policy_config_sha256=digest,
        bounds=settings.supervisor.output_bounds,
        rule_policy=DivergentRulePolicy(),
    )

    table_from_policy = trainer.baseline_table()
    assert all(entry.weights == divergent_weights for entry in table_from_policy.entries)
    # 設定の context（acoustic=0.4 / change=0.3）ではなく、policy が返した値になっている。
    configured = settings.supervisor.rule_policy.contexts.get(WorkloadRegime.IDLE)
    assert table_from_policy.entry(WorkloadRegime.IDLE).weights != configured.weights

    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)
    assert report.selected_candidate_id == BASELINE_CANDIDATE_ID
    assert report.artifact.payload == table_from_policy


def test_invariant_18_b_a_baseline_outside_the_action_space_is_refused(trained: Any) -> None:
    """**Baseline の欄も丸めない。** 範囲外を返す policy を起点にしない。"""
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)

    class OutOfRangeRulePolicy:
        kind = SupervisorPolicyKind.RULE
        version = "rule-test-v1"

        def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
            snapshot = policy_input.snapshot
            return SupervisorOutput(
                snapshot_schema_version=snapshot.schema_version,
                tick_id=snapshot.tick_id,
                ts_ms=snapshot.ts_ms,
                policy=SupervisorPolicyKind.RULE,
                version=self.version,
                regime=policy_input.workload.regime,
                regime_confidence=policy_input.workload.confidence,
                weights=weights(),
                strategy="unconfigured",
                target_band=target_band(),
                computed_at_ms=snapshot.ts_ms,
            )

    config, digest = rl_policy_config()
    trainer = SupervisorPolicyTrainer(
        environment,
        policy_config=config,
        policy_config_sha256=digest,
        bounds=settings.supervisor.output_bounds,
        rule_policy=OutOfRangeRulePolicy(),
    )
    with pytest.raises(SupervisorPolicyTrainingError, match="output_bounds の外"):
        trainer.baseline_table()


def test_invariant_19_candidates_are_ranked_on_one_common_horizon(trained: Any) -> None:
    """**長さの違う候補を、別々の長さで採点した平均で並べない**（決定記録 0058 §2.6）。

    途中で終わった候補は負の reward を積む回数が少ないので、生の平均で比べると
    「早く壊れたほうが良い」になる。共通の長さへ揃えてから採点する。
    """
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)

    # 報告は揃えた長さを欄として残す（hash では読めないため）。
    assert set(report.common_matched_steps) == {"pr89-a"}
    full = report.comparison.arms[0].episode("pr89-a")
    assert report.common_matched_steps["pr89-a"] == len(full.supported_steps)

    # **短く終わった arm は、生の平均が高くても共通の長さでは勝てない。**
    short_episode = full.model_copy(update={"steps": full.steps[:1]})
    short_arm = report.comparison.arms[0].model_copy(update={"episodes": (short_episode,)})
    long_arm = report.comparison.arms[0]

    horizon = common_matched_steps((long_arm, short_arm))
    assert horizon["pr89-a"] == 1

    # 生の総和では step 数の少ない側が有利になる（負の reward を積む回数が少ない）。
    assert short_episode.discounted_reward > full.discounted_reward
    # 揃えた長さでは同じ step だけを見るので、その有利が消える。
    assert mean_reward_over(short_arm, horizon) == mean_reward_over(long_arm, horizon)


def _unobserved(step: Any, code: str, *, floor_shortfalls: int = 0) -> Any:
    """採点できない終端記録（環境の `_unsupported_record` と同じ形）を作る。"""
    return step.model_copy(
        update={
            "supported": False,
            "unsupported_reason": Reason(code=code, detail="試験"),
            "applied": None,
            "observed": {},
            "provenance": None,
            "reward": None,
            "safety": step.safety.model_copy(
                update={
                    "ceiling_exceedances": 0,
                    "floor_shortfalls": floor_shortfalls,
                    "margin_c": None,
                }
            ),
        }
    )


def _arm_of(base: Any, episode: Any, version: str) -> Any:
    """1 episode の arm を作る。episode も arm と同じ policy の版を名乗る。"""
    return base.model_copy(
        update={
            "episodes": (episode.model_copy(update={"policy_version": version}),),
            "policy_version": version,
        }
    )


def test_invariant_19_b_a_candidate_cut_short_cannot_look_safer(trained: Any) -> None:
    """**壊れたせいで安全に見える候補を比べない**（fail closed）。

    安全側の台帳は episode 全体を数えるので、候補が自分の違反以外の理由で先に終わると、
    Baseline がその後で踏んだ違反を観測しないまま `safety_violations=0` になる。
    判定は記録の数ではなく、終わり方と**安全を観測した step の数**で行う。
    """
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)
    base = report.comparison.arms[0]
    # Learned MPC を束縛できた run として扱う（比べられる条件を作るため。台帳は直に作る）。
    full = base.episode("pr89-a").model_copy(update={"learned_controller_available": True})
    assert len(full.steps) >= 3 and all(step.supported for step in full.steps)
    last = full.steps[-1]

    def arm(episode: Any, version: str) -> Any:
        return _arm_of(base, episode, version)

    # Baseline 1: 最後の採点できた step で温度上限を超えた。
    ceiling = full.model_copy(
        update={
            "safety": full.safety.model_copy(update={"ceiling_exceedances": 1}),
            "termination": TerminationReason.SAFETY_VIOLATION,
        }
    )
    # Baseline 2: 最後の step で floor を下回り、**採点できない終端記録**に違反が載った。
    floor = full.model_copy(
        update={
            "steps": (
                *full.steps[:-1],
                _unobserved(last, "safety_floor_shortfall", floor_shortfalls=1),
            ),
            "safety": full.safety.model_copy(update={"floor_shortfalls": 1}),
            "termination": TerminationReason.SAFETY_VIOLATION,
        }
    )
    # 候補: 同じ位置で dynamics が使えなくなった。
    # 記録の数も採点できた step 数も Baseline 2 と同じ。
    same_point = full.model_copy(
        update={
            "steps": (*full.steps[:-1], _unobserved(last, "dynamics_unusable")),
            "termination": TerminationReason.DYNAMICS_UNUSABLE,
        }
    )
    assert len(same_point.steps) == len(floor.steps)
    assert len(same_point.supported_steps) == len(floor.supported_steps)
    # 候補: 自分の違反で先に終わった（違反は台帳に載る）。
    own = full.model_copy(
        update={
            "steps": full.steps[:-1],
            "safety": full.safety.model_copy(update={"ceiling_exceedances": 1}),
            "termination": TerminationReason.SAFETY_VIOLATION,
        }
    )

    for baseline_episode in (ceiling, floor):
        baseline_arm = arm(baseline_episode, base.policy_version)
        cut = arm(same_point, "cand-same-point")
        own_arm = arm(own, "cand-own")
        clean = arm(full, "cand-clean")
        assert truncated_episodes(cut, baseline_arm) == ("pr89-a",)
        assert truncated_episodes(own_arm, baseline_arm) == ()
        assert truncated_episodes(clean, baseline_arm) == ()

        arms = {"cut": cut, "own": own_arm, "clean": clean}
        rejections = {
            key: reason
            for key, value in arms.items()
            if (reason := candidate_rejection(value, baseline_arm)) is not None
        }
        assert set(rejections) == {"cut"}
        assert rejections["cut"].code == "candidate_truncated"
        horizon = scoring_horizon(baseline_arm, arms, rejections)
        outcomes = trainer._score(
            tables={key: trainer.baseline_table() for key in arms},
            arms=arms,
            rejections=rejections,
            baseline_arm=baseline_arm,
            horizon=horizon,
        )
        assert outcomes["cut"].comparable is False
        assert outcomes["cut"].truncated_episodes == ("pr89-a",)
        assert outcomes["cut"].improved is False
        assert outcomes["cut"].mean_reward_over_common_horizon is None
        assert outcomes["own"].truncated_episodes == ()
        assert outcomes["clean"].improved is True
        assert trainer._select(outcomes) == "clean"


def test_invariant_19_c_a_truncated_candidate_never_shortens_the_common_horizon(
    trained: Any,
) -> None:
    """**打ち切った候補は共通の長さを縮めない。** 比べてよいかは採点の前に1度だけ決める。

    後で落とす形だと、coverage を満たしたまま早く終わった候補が最小を取り、
    健全な候補すべてがその短い区間で採点される。
    """
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)
    base = report.comparison.arms[0]
    full = base.episode("pr89-a").model_copy(update={"learned_controller_available": True})
    assert len(full.steps) >= 3

    early = full.model_copy(
        update={
            "steps": (*full.steps[:1], _unobserved(full.steps[1], "dynamics_unusable")),
            "termination": TerminationReason.DYNAMICS_UNUSABLE,
        }
    )
    baseline_arm = _arm_of(base, full, base.policy_version)
    arms = {"early": _arm_of(base, early, "cand-early"), "clean": _arm_of(base, full, "cand-clean")}
    rejections = {
        key: reason
        for key, value in arms.items()
        if (reason := candidate_rejection(value, baseline_arm)) is not None
    }
    assert set(rejections) == {"early"}
    horizon = scoring_horizon(baseline_arm, arms, rejections)
    # 打ち切った候補を入れていれば 1 になっていた。
    assert common_matched_steps((baseline_arm, *arms.values()))["pr89-a"] == 1
    assert horizon == {"pr89-a": len(full.supported_steps)}


def test_invariant_19_d_a_self_failing_candidate_never_shortens_the_common_horizon(
    trained: Any,
) -> None:
    """**自分の違反で先に終わった候補も、共通の長さを縮めない。**

    その候補は打ち切りではない（違反は台帳に載る）ので比較には残るが、共通の長さに入れると
    健全な候補どうしが最初の数 step だけで並べられ、後半の差が無視される。壊れた候補が
    「どの健全な候補が選ばれるか」を変えてはならない。
    """
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)
    base = report.comparison.arms[0]
    full = base.episode("pr89-a").model_copy(update={"learned_controller_available": True})
    assert len(full.steps) >= 3 and all(step.supported for step in full.steps)

    # A: 1 step 目の後、2 step 目で自分の違反を踏んで終わった（違反を観測した記録は残す）。
    violating = full.steps[1].model_copy(
        update={"safety": full.steps[1].safety.model_copy(update={"ceiling_exceedances": 1})}
    )
    self_failed = full.model_copy(
        update={
            "steps": (full.steps[0], violating),
            "safety": full.safety.model_copy(update={"ceiling_exceedances": 1}),
            "termination": TerminationReason.SAFETY_VIOLATION,
        }
    )
    # B / C: 走り切る。**違いは最後の step の reward だけ**（A が終わった後の区間）。
    last = full.steps[-1]
    assert last.reward is not None
    better_last = last.model_copy(
        update={"reward": last.reward.model_copy(update={"reward": last.reward.reward + 1.0})}
    )
    better = full.model_copy(update={"steps": (*full.steps[:-1], better_last)})

    baseline_arm = _arm_of(base, full, base.policy_version)
    arms = {
        "a-self-failed": _arm_of(base, self_failed, "cand-a"),
        "b-same": _arm_of(base, full, "cand-b"),
        "c-better": _arm_of(base, better, "cand-c"),
    }
    rejections = {
        key: reason
        for key, value in arms.items()
        if (reason := candidate_rejection(value, baseline_arm)) is not None
    }
    # A は打ち切りではない（自分の違反で終わった）ので却下されない。
    assert rejections == {}

    horizon = scoring_horizon(baseline_arm, arms, rejections)
    assert horizon == {"pr89-a": len(full.supported_steps)}

    outcomes = trainer._score(
        tables={key: trainer.baseline_table() for key in arms},
        arms=arms,
        rejections=rejections,
        baseline_arm=baseline_arm,
        horizon=horizon,
    )
    assert outcomes["a-self-failed"].comparable is True
    assert outcomes["a-self-failed"].short_episodes == ("pr89-a",)
    assert outcomes["a-self-failed"].safety_violations == 1
    assert outcomes["a-self-failed"].mean_reward_over_common_horizon is None
    assert outcomes["a-self-failed"].improved is False
    assert outcomes["b-same"].improved is False
    assert outcomes["c-better"].improved is True
    assert trainer._select(outcomes) == "c-better"


def test_invariant_19_e_short_is_judged_by_observed_length_not_supported_steps(
    trained: Any,
) -> None:
    """**長さの定義は1つ（観測した長さ）。** 採点できた step 数が同じでも短い候補を見落とさない。

    候補は採点できた step で温度上限を超えて終わり、Baseline は同じ数の採点できた step の後、
    採点できない終端で floor 不足を記録した。採点できた step 数は等しいが、Baseline は
    候補が観測していない最後の安全側の結果を観測している。
    """
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)
    base = report.comparison.arms[0]
    full = base.episode("pr89-a").model_copy(update={"learned_controller_available": True})
    assert len(full.steps) >= 3 and all(step.supported for step in full.steps)
    kept = full.steps[:-1]

    # Baseline: 最後から2つ目までは採点でき、最後の step で floor を下回った（採点できない終端）。
    baseline_episode = full.model_copy(
        update={
            "steps": (
                *kept,
                _unobserved(full.steps[-1], "safety_floor_shortfall", floor_shortfalls=1),
            ),
            "safety": full.safety.model_copy(update={"floor_shortfalls": 1}),
            "termination": TerminationReason.SAFETY_VIOLATION,
        }
    )
    # 候補: 同じ数の採点できた step で、最後の採点できた step が温度上限を超えて終わった。
    # reward は Baseline より良くしておく（違反の数が同点なら reward で改善に見えてしまう）。
    tail = kept[-1]
    assert tail.reward is not None
    violating_tail = tail.model_copy(
        update={
            "safety": tail.safety.model_copy(update={"ceiling_exceedances": 1}),
            "reward": tail.reward.model_copy(update={"reward": tail.reward.reward + 1.0}),
        }
    )
    candidate_episode = full.model_copy(
        update={
            "steps": (*kept[:-1], violating_tail),
            "safety": full.safety.model_copy(update={"ceiling_exceedances": 1}),
            "termination": TerminationReason.SAFETY_VIOLATION,
        }
    )
    assert len(candidate_episode.supported_steps) == len(baseline_episode.supported_steps)

    baseline_arm = _arm_of(base, baseline_episode, base.policy_version)
    arms = {"early": _arm_of(base, candidate_episode, "cand-early")}
    assert safety_observed_steps(candidate_episode) < safety_observed_steps(baseline_episode)
    assert short_episodes(arms["early"], baseline_arm) == ("pr89-a",)
    # 自分の違反で終わったので打ち切りではない（比較には残る）。
    assert truncated_episodes(arms["early"], baseline_arm) == ()
    assert candidate_rejection(arms["early"], baseline_arm) is None

    horizon = scoring_horizon(baseline_arm, arms, {})
    outcomes = trainer._score(
        tables={key: trainer.baseline_table() for key in arms},
        arms=arms,
        rejections={},
        baseline_arm=baseline_arm,
        horizon=horizon,
    )
    early = outcomes["early"]
    assert early.comparable is True
    assert early.safety_violations == baseline_arm.safety_violations == 1
    assert early.short_episodes == ("pr89-a",)
    assert early.mean_reward_over_common_horizon is None
    assert early.improved is False
    assert trainer._select(outcomes) == BASELINE_CANDIDATE_ID


def test_invariant_19_f_a_reward_short_failure_never_shortens_the_common_horizon(
    trained: Any,
) -> None:
    """**観測した長さが等しくても、reward を持つ step が少ない候補は共通の長さに入れない。**

    候補は Baseline の最後の step まで来て、そこで floor を下回って終わった（採点できない終端）。
    違反は観測しているので観測した長さは Baseline と等しいが、reward を持つ step は1つ少ない。
    共通の長さに入れると、健全な候補どうしが最後の step の差を無視して並べられる。
    """
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings)
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)
    base = report.comparison.arms[0]
    full = base.episode("pr89-a").model_copy(update={"learned_controller_available": True})
    assert len(full.steps) >= 3 and all(step.supported for step in full.steps)
    last = full.steps[-1]
    assert last.reward is not None

    floor_at_end = full.model_copy(
        update={
            "steps": (
                *full.steps[:-1],
                _unobserved(last, "safety_floor_shortfall", floor_shortfalls=1),
            ),
            "safety": full.safety.model_copy(update={"floor_shortfalls": 1}),
            "termination": TerminationReason.SAFETY_VIOLATION,
        }
    )
    better_last = last.model_copy(
        update={"reward": last.reward.model_copy(update={"reward": last.reward.reward + 1.0})}
    )
    better = full.model_copy(update={"steps": (*full.steps[:-1], better_last)})

    baseline_arm = _arm_of(base, full, base.policy_version)
    arms = {
        "a-floor-at-end": _arm_of(base, floor_at_end, "cand-a"),
        "b-same": _arm_of(base, full, "cand-b"),
        "c-better": _arm_of(base, better, "cand-c"),
    }
    assert safety_observed_steps(floor_at_end) == safety_observed_steps(full)
    assert len(floor_at_end.supported_steps) == len(full.supported_steps) - 1

    assert short_episodes(arms["a-floor-at-end"], baseline_arm) == ("pr89-a",)
    assert truncated_episodes(arms["a-floor-at-end"], baseline_arm) == ()
    rejections = {
        key: reason
        for key, value in arms.items()
        if (reason := candidate_rejection(value, baseline_arm)) is not None
    }
    assert rejections == {}

    horizon = scoring_horizon(baseline_arm, arms, rejections)
    assert horizon == {"pr89-a": len(full.supported_steps)}

    outcomes = trainer._score(
        tables={key: trainer.baseline_table() for key in arms},
        arms=arms,
        rejections=rejections,
        baseline_arm=baseline_arm,
        horizon=horizon,
    )
    failed = outcomes["a-floor-at-end"]
    assert failed.comparable is True
    assert failed.short_episodes == ("pr89-a",)
    assert failed.truncated_episodes == ()
    assert failed.mean_reward_over_common_horizon is None
    assert failed.improved is False
    assert outcomes["b-same"].improved is False
    assert outcomes["c-better"].improved is True
    assert trainer._select(outcomes) == "c-better"


def test_invariant_20_binding_compares_every_artifact_determined_metadata_field(
    tmp_path: Path, bounds: SupervisorOutputBounds
) -> None:
    """**正しい bytes を登録しながら metadata だけ書き換えた artifact を通さない。**

    `policy_registry_metadata()` が artifact から導く欄は1つ残らず照合する。
    一部だけ見ていると、見ていない欄を書き換えた登録が通る。
    """
    policy_artifact = artifact(bounds)
    for field, value in (
        ("code_commit", "0123456789abcdef"),
        ("hyperparameters", {"search_family": "tampered", "seed": 999}),
        ("training_dataset_version", "rl-episodes:" + "f" * 64),
        ("source_runs", ("ep-z",)),
    ):
        verified = register_policy(
            tmp_path / f"pr89-meta-{field}",
            policy_artifact,
            metadata_overrides={field: value},
        )
        with pytest.raises(SupervisorPolicyUnusableError, match="Registry metadata と一致しない"):
            SupervisorPolicyBinding.for_shadow(
                verified, expected_policy_version="0.1.0", bounds=bounds
            )

    # 照合の網は欄を手で並べず、model の欄から作る（あとから足した欄が漏れないように）。
    assert {
        "offline_evaluation_ref",
        "shadow_evaluation_ref",
    } == ARTIFACT_DETERMINED_METADATA_EXCLUSIONS
    checked = set(SupervisorPolicyRegistryMetadata.model_fields) - (
        ARTIFACT_DETERMINED_METADATA_EXCLUSIONS
    )
    assert {"code_commit", "hyperparameters", "sha256", "authority_compatibility"} <= checked


def test_candidates_are_bounded_and_never_silently_truncated(trained: Any) -> None:
    """**候補が上限を超えたら切り詰めず落とす。** 報告に出ない候補を作らない。"""
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings, search={"max_candidate_tables": 2})
    with pytest.raises(SupervisorPolicyTrainingError, match="上限を超えた"):
        trainer.candidates()


def test_candidates_are_counted_before_they_are_materialized(
    trained: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**上限は作る前に数えて検査する。** 直積や表を先に作ると、大きい設定で資源を使い切る。"""
    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    trainer = trainer_for(environment, settings, search={"max_candidate_tables": 2})

    def forbidden(self: Any) -> Any:
        raise AssertionError("上限を超える設定で action の直積を作った")

    monkeypatch.setattr(SupervisorPolicyTrainer, "_action_pool", forbidden)
    with pytest.raises(SupervisorPolicyTrainingError, match="上限を超えた"):
        trainer.candidates()

    # 事前の数は、実際に作った数と一致する（上限の中では作ってから照合している）。
    monkeypatch.undo()
    roomy = trainer_for(
        environment, settings, search={"max_candidate_tables": MAX_CANDIDATE_TABLES}
    )
    baseline = roomy.baseline_table()
    assert roomy._projected_candidate_count(baseline) == len(roomy.candidates())


def test_a_maximal_model_id_leaves_room_for_every_candidate_version(trained: Any) -> None:
    """**候補の版 `<model_id>-<識別子>` は上限に収まる。**

    収まらない model_id は、探索の途中ではなく設定の読み込みで落とす。
    """
    with pytest.raises(ValidationError, match="model_id"):
        rl_policy_config(artifact={"model_id": "m" * (MAX_POLICY_MODEL_ID_LENGTH + 1)})

    environment, _config, settings, _safety = build_environment(trained, with_mpc=False)
    model_id = "m" * MAX_POLICY_MODEL_ID_LENGTH
    trainer = trainer_for(environment, settings, artifact={"model_id": model_id})
    identifiers = [identifier for identifier, _ in trainer.candidates()]
    assert max(len(f"{model_id}-{identifier}") for identifier in identifiers) <= (
        POLICY_VERSION_MAX_LENGTH
    )
    # 学習を最後まで回しても、候補の版が検証に落ちない。
    specs = (episode_spec(episode_id="pr89-a", seed=3),)
    report = trainer.train(specs, model_version="0.1.0", created_at=CREATED_AT)
    assert report.artifact.manifest.model_id == model_id


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


def ledger_for() -> SupervisorShadowLedger:
    config, _digest = rl_policy_config()
    return SupervisorShadowLedger(
        config.shadow, rule_policy_version="rule-test-v1", rl_identity=RL_IDENTITY
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
    rl_identity: SupervisorPolicyIdentity | None = RL_IDENTITY,
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
            policy_identity=rl_identity,
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


def both_missing_decision(*, tick_id: int, ts_ms: int = 4_000) -> SupervisorDecision:
    return SupervisorDecision(
        tick_id=tick_id,
        ts_ms=ts_ms,
        snapshot_schema_version=1,
        active=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RULE,
            error=Reason(code="supervisor_rule_failed", detail="RulePolicy の評価に失敗"),
        ),
        shadow=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RL,
            error=Reason(code="supervisor_worker_down", detail="RLPolicy worker が応答しない"),
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
