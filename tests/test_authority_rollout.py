"""Authority Rollout（#92 / 決定記録 0057）。**実機は要らない。**

この module は不変条件ごとに1つの試験を持ち、**それを破ろうとする**。
「通る道」だけを確かめる試験は、#85 / #90 / #91 のレビューで繰り返し漏れを出した型である。

守る不変条件:

1. 既定は Shadow。壊れた journal を Shadow と読み替えない
2. stage を上げられるのは人の承認だけ
3. 承認は使い回せない（revision・遷移・時刻・設定・証拠・artifact に束縛する）
4. 昇格は1段ずつで、設定の上限を超えない
5. 昇格の証拠は**完全・新鮮・束縛済み**でなければならない
6. 降格に承認は要らず、書き残せなくても効く
7. 設定は上限として働き、上げても journal は上がらない
8. Model promotion は authority を動かさない
9. stage は model version と独立に trace へ残る
10. Critical Safety は全 stage で同一
"""

from __future__ import annotations

import json
import os
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control import (
    BASELINE_STAGE,
    STAGE_ORDER,
    ArtifactKind,
    AuthorityApprovalError,
    AuthorityChangeKind,
    AuthorityEvent,
    AuthorityEvidenceError,
    AuthorityJournal,
    AuthorityRuntime,
    AuthorityStage,
    AuthorityStateError,
    AuthorityStore,
    AuthorityStoreError,
    AuthorityTrigger,
    AutomaticCause,
    ConfidenceLevel,
    ControllerGate,
    ControllerKind,
    ControllerSelection,
    DemandComposer,
    FallbackCause,
    ModelRegistry,
    OperatingMode,
    RolloutEvidence,
    SafetyState,
    StageApproval,
    StaticAuthorityStage,
    SupervisorPolicyKind,
    lowest_stage,
    stage_above,
    stage_below,
    stage_rank,
)
from coldaisle.control.config import ControlConfig
from coldaisle.control.evaluation.model import (
    AppliedArm,
    AppliedArmReport,
    CountedReason,
    CounterfactualArm,
    CounterfactualArmReport,
    CoverageReport,
    EvaluationProvenance,
    EvaluationReport,
    GateCondition,
    GateOutcome,
    GateResult,
    GateStage,
    GroupKind,
    GroupReport,
    InterventionReport,
    ObservedVersions,
    RunProvenance,
    SegmentReport,
    SegmentRole,
    WorstCase,
    WorstCaseKind,
)
from coldaisle.control.model.confidence import ConfidenceAssessor
from coldaisle.control.mpc import LearnedMpcController, MpcModelBinding
from coldaisle.control.shadow import SHADOW_EXPORT_SCHEMA_VERSION
from test_control_config import valid_documents, write_documents
from test_critical_safety import (
    critical_safety,
    empty_guard,
    safety_config,
)
from test_critical_safety import (
    snapshot as safety_snapshot,
)
from test_fallback_controller import (
    fallback_proposal,
    healthy_status,
    learned_proposal,
    policy,
)
from test_learned_mpc import (
    ALL_STAGES,
    REGISTRY_LIMITS,
    PlanningModel,
    ScriptedClock,
    issue_attestation,
    mpc_policy,
    propose,
    trained,  # noqa: F401 （pytest fixture として使う）
)
from test_learned_mpc import (
    safety as mpc_safety,
)
from test_model_registry import (
    LIMITS,
    register_and_validate,
)
from test_model_registry import (
    promote as promote_artifact,
)

NOW_MS = 1_800_000_000_000
"""固定の壁時計。**実時計に依存させない。**"""

APPROVER = "rack-owner"
CONDITIONS_SHA = "3" * 64
EVIDENCE_END_MS = NOW_MS - 3_600_000
"""証拠の最後の観測は1時間前。既定の `evidence_max_age_ms`（7日）の中に収まる。"""


def store(tmp_path: Path, *, now_ms: int = NOW_MS) -> AuthorityStore:
    # control runtime から使う store は lock の待ち上限が要る（決定記録 0060 §2.7）。
    return AuthorityStore(tmp_path / "authority", SimulatedClock(now_ms), lock_timeout_ms=500)


_FIXTURES = TemporaryDirectory(prefix="pr92-authority-")
"""module 全体で使う既定の検証済み設定と attestation の置き場。"""


def control_config(tmp_path: Path, *, ceiling: str = "full", **rollout: int) -> ControlConfig:
    """**実ファイルから検証した** ControlConfig。checksum が中身と結び付いている。

    昇格は設定の checksum を証拠と突き合わせるので、試験でも「検証済みの束」から取る
    （承認者が sha を持ち込めないようにした側と同じ経路を通す。codex #4056903570）。
    """
    documents = valid_documents()
    policy_document = documents["fan-policy.yaml"]
    policy_document["authority_stage"] = ceiling
    for name, value in rollout.items():
        policy_document["authority_rollout"][name] = {
            "value": value,
            "status": "provisional",
            "basis": None,
        }
    root = tmp_path / f"config-{ceiling}-{'-'.join(f'{k}{v}' for k, v in sorted(rollout.items()))}"
    root.mkdir(parents=True, exist_ok=True)
    write_documents(root, documents)
    return ControlConfig.from_directory(root)


def production_registry(
    tmp_path: Path,
    *,
    version: str = "1.0.0",
    authority: tuple[AuthorityStage, ...] = ALL_STAGES,
    promoted: bool = True,
    name: str | None = None,
) -> tuple[ModelRegistry, str]:
    """**本物の Registry**（#104）と、その production artifact の sha256。

    `raise_stage()` は artifact の identity を registry から読み直すので、試験でも
    実際の registry を渡す（承認者が hash を持ち込めない側と同じ経路を通す）。
    """
    root = tmp_path / (name or f"registry-{version}")
    attestation = issue_attestation(
        root,
        model_id="rack-thermal",
        version=version,
        authority=authority,
        stage=AuthorityStage.SHADOW,
        promoted=promoted,
    )
    return ModelRegistry(root, limits=REGISTRY_LIMITS), attestation.artifact_sha256


def learned_arm(stage: AuthorityStage = AuthorityStage.SHADOW) -> CounterfactualArm:
    """Shadow で回している Learned MPC の counterfactual arm（0054 §2.1）。"""
    return CounterfactualArm(
        controller=ControllerKind.LEARNED_MPC,
        supervisor_policy=SupervisorPolicyKind.RULE,
        authority_stage=stage,
    )


def applied_fallback_arm(stage: AuthorityStage = AuthorityStage.SHADOW) -> AppliedArm:
    """実 Fan を作っていた Fallback の arm。**昇格の根拠にはできない側。**"""
    return AppliedArm(
        controller=ControllerKind.FALLBACK,
        supervisor_policy=SupervisorPolicyKind.RULE,
        authority_stage=stage,
        operating_mode=OperatingMode.AUTO,
    )


ARM = learned_arm().key
"""既定の比較対象: Shadow で回していた Learned MPC の counterfactual arm。"""

FALLBACK_ARM = applied_fallback_arm().key
"""実 Fan を作っていた Fallback の arm。**昇格の根拠にはできない。**"""


def counterfactual_report(
    arm: CounterfactualArm,
    *,
    end_ms: int,
    last_ts_ms: int | None = None,
    last_attested_ts_ms: int | None = None,
) -> CounterfactualArmReport:
    attested_ts_ms = (
        last_attested_ts_ms
        if last_attested_ts_ms is not None
        else (end_ms if last_ts_ms is None else last_ts_ms)
    )
    return CounterfactualArmReport(
        arm=arm,
        arm_key=arm.key,
        ticks=1_000,
        first_ts_ms=min(end_ms, last_ts_ms or end_ms, attested_ts_ms) - 3_600_000,
        last_ts_ms=end_ms if last_ts_ms is None else last_ts_ms,
        last_attested_ts_ms=attested_ts_ms,
        attested_ticks=1_000,
        proposals=1_000,
        safety_floor_shortfalls=0,
        maximum_floor_shortfall=0.0,
        coverage=CoverageReport(
            outcomes=1_000,
            scored=900,
            unidentifiable=100,
            identifiable_fraction=0.9,
            outputs=900,
            matched_outputs=900,
            scored_outputs=900,
            sufficient=True,
        ),
    )


def applied_report(
    arm: AppliedArm, *, end_ms: int, last_attested_ts_ms: int | None = None
) -> AppliedArmReport:
    attested = (
        last_attested_ts_ms
        if last_attested_ts_ms is not None
        else (end_ms if arm.controller is ControllerKind.LEARNED_MPC else None)
    )
    return AppliedArmReport(
        arm=arm,
        arm_key=arm.key,
        ticks=1_000,
        first_ts_ms=min(end_ms, attested if attested is not None else end_ms) - 3_600_000,
        last_ts_ms=end_ms,
        last_attested_ts_ms=attested,
        interventions=InterventionReport(
            ticks=1_000,
            safety_states=(CountedReason(code="normal", count=1_000),),
        ),
    )


DEFAULT_CONFIG = control_config(Path(_FIXTURES.name), ceiling="full")
"""既定の検証済み ControlConfig。**checksum は実ファイルから来る。**"""

POLICY_SHA = DEFAULT_CONFIG.sources.policy.sha256
SAFETY_SHA = DEFAULT_CONFIG.sources.safety.sha256


def promote_next_version(root: Path, *, version: str) -> str:
    """同じ Registry の production を別の artifact へ入れ替え、その sha256 を返す。"""
    attestation = issue_attestation(
        root,
        model_id="rack-thermal",
        version=version,
        authority=ALL_STAGES,
        stage=AuthorityStage.SHADOW,
    )
    return attestation.artifact_sha256


PRODUCTION_REGISTRY, ARTIFACT_SHA = production_registry(Path(_FIXTURES.name))
"""既定の Registry と、その production artifact の sha256。"""


def report_document(
    *,
    arm: str | None = None,
    outcome: GateOutcome = GateOutcome.PASS,
    artifacts: tuple[str, ...] = (ARTIFACT_SHA,),
    stages: tuple[str, ...] = (AuthorityStage.SHADOW.value,),
    end_ms: int = EVIDENCE_END_MS,
    policy_sha: str = POLICY_SHA,
    safety_sha: str = SAFETY_SHA,
    conditions_sha: str = CONDITIONS_SHA,
    arm_stage: AuthorityStage = AuthorityStage.SHADOW,
    with_applied_fallback: bool = False,
    extra_learned: CounterfactualArm | None = None,
    extra_outcome: GateOutcome = GateOutcome.PASS,
    fresh_fallback_end_ms: int | None = None,
    learned_last_ts_ms: int | None = None,
    learned_last_attested_ts_ms: int | None = None,
    start_ms: int | None = None,
) -> bytes:
    """最小の Offline Evaluation 報告（#91）。**arm の実績と gate を持つ。**"""
    learned = learned_arm(arm_stage)
    arm = arm if arm is not None else learned.key
    fallback = applied_fallback_arm(arm_stage)

    def gate(key: str, result: GateOutcome) -> GateResult:
        return GateResult(
            arm_key=key,
            outcome=result,
            blocking_stage=None if result is GateOutcome.PASS else GateStage.SAFETY,
            conditions=(
                GateCondition(
                    stage=GateStage.SAFETY,
                    name="ceiling_exceedances",
                    outcome=result,
                    limit=0.0,
                    observed=0.0 if result is GateOutcome.PASS else 1.0,
                ),
            ),
        )

    counterfactual_arms = [
        counterfactual_report(
            learned,
            end_ms=end_ms,
            last_ts_ms=learned_last_ts_ms,
            last_attested_ts_ms=learned_last_attested_ts_ms,
        )
    ]
    gates = [gate(arm, outcome)]
    if arm != learned.key:
        gates.append(gate(learned.key, GateOutcome.PASS))
    if extra_learned is not None:
        counterfactual_arms.append(counterfactual_report(extra_learned, end_ms=end_ms))
        gates.append(gate(extra_learned.key, extra_outcome))
    applied_arms = [applied_report(fallback, end_ms=end_ms)] if with_applied_fallback else []
    # **Learned の実績が無い、新しいだけの区間。** 新しさの測り方を試すために足す。
    fresh_segments: list[SegmentReport] = []
    if fresh_fallback_end_ms is not None:
        fresh_segments.append(
            SegmentReport(
                run_id="run-001",
                index=1,
                role=SegmentRole.HOLDOUT,
                start_ms=fresh_fallback_end_ms - 3_600_000,
                end_ms=fresh_fallback_end_ms,
                ticks=1_000,
                purged_outcomes=0,
                groups=(
                    GroupReport(
                        kind=GroupKind.OVERALL,
                        value="overall",
                        applied=(applied_report(fallback, end_ms=fresh_fallback_end_ms),),
                    ),
                ),
            )
        )
    report = EvaluationReport(
        provenance=EvaluationProvenance(
            evaluation_config_sha256="5" * 64,
            fan_hardware_config_sha256="6" * 64,
            safety_config_sha256=safety_sha,
            fan_policy_config_sha256=policy_sha,
            metric_catalog_sha256="7" * 64,
            absolute_temp_ceiling_c=95.0,
            outcome_match_tolerance_ms=500,
            applied_demand_tolerance=0.01,
            shadow_export_schema_version=SHADOW_EXPORT_SCHEMA_VERSION,
            runs=(
                RunProvenance(
                    run_id="run-001",
                    start_ms=end_ms - 86_400_000,
                    end_ms=end_ms if fresh_fallback_end_ms is None else fresh_fallback_end_ms,
                    traces=1_000,
                    observations=1_000,
                    trace_sha256="8" * 64,
                    observation_sha256="9" * 64,
                ),
            ),
            versions=ObservedVersions(model_artifacts=artifacts, authority_stages=stages),
            conditions_sha256=conditions_sha,
        ),
        segments=(
            SegmentReport(
                run_id="run-001",
                index=0,
                role=SegmentRole.HOLDOUT,
                start_ms=start_ms if start_ms is not None else end_ms - 3_600_000,
                end_ms=end_ms,
                ticks=1_000,
                purged_outcomes=0,
                groups=(
                    GroupReport(
                        kind=GroupKind.OVERALL,
                        value="overall",
                        applied=tuple(applied_arms),
                        counterfactual=tuple(counterfactual_arms),
                    ),
                ),
            ),
            *fresh_segments,
        ),
        worst_cases=(
            WorstCase(
                kind=WorstCaseKind.MAXIMUM_TEMPERATURE,
                run_id="run-001",
                segment_index=0,
                role=SegmentRole.HOLDOUT,
                arm_key=arm,
                metric="gpu.0.core",
                value=78.0,
            ),
        ),
        gates=tuple(gates),
    )
    return report.model_dump_json().encode("utf-8")


def evidence_for(
    document: bytes,
    *,
    arm: str = ARM,
    artifact: str = ARTIFACT_SHA,
    end_ms: int = EVIDENCE_END_MS,
    policy_sha: str = POLICY_SHA,
    safety_sha: str = SAFETY_SHA,
    conditions_sha: str = CONDITIONS_SHA,
) -> RolloutEvidence:
    return RolloutEvidence(
        report_sha256=sha256(document).hexdigest(),
        conditions_sha256=conditions_sha,
        arm_key=arm,
        artifact_sha256=artifact,
        evidence_end_ms=end_ms,
        fan_policy_config_sha256=policy_sha,
        safety_config_sha256=safety_sha,
    )


def approval_for(
    document: bytes,
    *,
    from_stage: AuthorityStage = AuthorityStage.SHADOW,
    revision: int = 0,
    approved_at_ms: int = NOW_MS,
    reason: str = "shadow で7日運転し、gate をすべて通した",
    evidence: RolloutEvidence | None = None,
) -> StageApproval:
    target = stage_above(from_stage)
    assert target is not None
    return StageApproval(
        from_stage=from_stage,
        to_stage=target,
        expected_revision=revision,
        approver=APPROVER,
        approved_at_ms=approved_at_ms,
        reason=reason,
        evidence=evidence if evidence is not None else evidence_for(document),
    )


def raise_stage(
    authority: AuthorityStore,
    *,
    approval: StageApproval,
    document: bytes,
    config: ControlConfig | None = None,
    registry: ModelRegistry | None = None,
) -> AuthorityJournal:
    return authority.raise_stage(
        approval=approval,
        evaluation_report=document,
        config=config if config is not None else DEFAULT_CONFIG,
        registry=registry if registry is not None else PRODUCTION_REGISTRY,
    )


def runtime(
    tmp_path: Path,
    *,
    stage: AuthorityStage = AuthorityStage.FULL,
    ceiling: str = "full",
    settings: Any = None,
) -> AuthorityRuntime:
    """`stage` まで上げた journal を持つ runtime。昇格はすべて承認を経由する。"""
    authority = store(tmp_path)
    document = report_document()
    current = BASELINE_STAGE
    while current is not stage:
        document = report_document(stages=(current.value,), arm_stage=current)
        raise_stage(
            authority,
            approval=approval_for(
                document,
                from_stage=current,
                revision=stage_rank(current),
                evidence=evidence_for(document, arm=learned_arm(current).key),
            ),
            document=document,
        )
        next_stage = stage_above(current)
        assert next_stage is not None
        current = next_stage
    return AuthorityRuntime(
        authority,
        settings if settings is not None else policy(authority=ceiling),
    )


class UnwritableStore(AuthorityStore):
    """読めるが書けない store。**降格の適用が永続化に依存しない**ことを確かめる。"""

    __slots__ = ()

    def lower_stage(self, **_: object) -> AuthorityJournal:  # type: ignore[override]
        raise AuthorityStoreError("disk full")


def unwritable_runtime(
    tmp_path: Path, *, stage: AuthorityStage = AuthorityStage.FULL
) -> AuthorityRuntime:
    """`stage` まで上げた journal を読み、以後は書けなくなった runtime。"""
    runtime(tmp_path, stage=stage)
    return AuthorityRuntime(
        UnwritableStore(tmp_path / "authority", SimulatedClock(NOW_MS), lock_timeout_ms=500),
        policy(authority="full"),
    )


# --- 不変条件 1: 既定は Shadow --------------------------------------------------


def test_invariant_1_a_an_absent_journal_starts_at_shadow(tmp_path: Path) -> None:
    """**記録が無いときに与える制御権は Baseline。** 初回起動で ML が実 Fan を握らない。"""
    authority = store(tmp_path)

    journal = authority.read()

    assert journal.stage is AuthorityStage.SHADOW
    assert journal.revision == 0
    assert journal.events == ()
    assert not (tmp_path / "authority").exists(), "読むだけで store を作らない"


def test_invariant_1_b_a_corrupt_journal_is_not_read_as_shadow(tmp_path: Path) -> None:
    """**壊すだけで「記録の無い状態」へ移せない。** 読み替えは静かな rollback になる。"""
    root = tmp_path / "authority"
    root.mkdir()
    (root / "authority.json").write_text('{"schema_version": 1, "revision": 99}', encoding="utf-8")

    with pytest.raises(AuthorityStateError):
        store(tmp_path).read()


def test_invariant_1_c_a_journal_that_does_not_replay_is_refused() -> None:
    """**event から再現できない stage を読まない。** 書き換えた stage だけが効かない。"""
    with pytest.raises(ValidationError, match="再現できない"):
        AuthorityJournal(revision=0, stage=AuthorityStage.FULL)


def test_invariant_1_d_the_store_refuses_a_relative_root() -> None:
    """**相対 path の store を作らない。** 作業 directory で指す先が変わる。"""
    with pytest.raises(AuthorityStoreError, match="絶対 path"):
        AuthorityStore(Path("var/authority"))


# --- 不変条件 2: 上げられるのは人だけ -------------------------------------------


def test_invariant_2_a_a_human_approval_raises_one_stage_with_a_reason_and_time(
    tmp_path: Path,
) -> None:
    """昇格は**承認者・理由・時刻**とともに残る（受入基準「理由と時刻を記録する」）。"""
    authority = store(tmp_path)
    document = report_document()

    journal = raise_stage(authority, approval=approval_for(document), document=document)

    assert journal.stage is AuthorityStage.LIMITED
    assert journal.revision == 1
    event = journal.events[-1]
    assert event.kind is AuthorityChangeKind.RAISED
    assert event.trigger is AuthorityTrigger.HUMAN
    assert event.actor == APPROVER
    assert event.reason
    assert event.occurred_at_ms == NOW_MS
    assert event.approval is not None
    assert authority.read() == journal, "journal は読み直しても同じ"


def test_invariant_2_b_the_journal_cannot_record_a_raise_without_an_approval() -> None:
    """**承認の無い昇格は、書ける形にしない。**"""
    with pytest.raises(ValidationError, match="承認が要る"):
        AuthorityEvent(
            revision=1,
            occurred_at_ms=NOW_MS,
            kind=AuthorityChangeKind.RAISED,
            trigger=AuthorityTrigger.HUMAN,
            from_stage=AuthorityStage.SHADOW,
            to_stage=AuthorityStage.LIMITED,
            actor=APPROVER,
            reason="上げたい",
        )


def test_invariant_2_c_an_automatic_trigger_cannot_raise_the_stage() -> None:
    """**系が自分で上げる経路を作らない。**"""
    document = report_document()
    with pytest.raises(ValidationError, match="上げられるのは人だけ"):
        AuthorityEvent(
            revision=1,
            occurred_at_ms=NOW_MS,
            kind=AuthorityChangeKind.RAISED,
            trigger=AuthorityTrigger.AUTOMATIC,
            from_stage=AuthorityStage.SHADOW,
            to_stage=AuthorityStage.LIMITED,
            actor=APPROVER,
            reason="良さそうなので",
            approval=approval_for(document),
        )


def test_invariant_2_d_the_control_runtime_has_no_way_to_raise_the_stage(tmp_path: Path) -> None:
    """**制御ループから昇格を呼べない。** 呼べる名前が生えたらここで落ちる。"""
    control = runtime(tmp_path, stage=AuthorityStage.LIMITED)

    raising = [
        name
        for name in dir(control)
        if not name.startswith("_") and any(word in name for word in ("raise", "promote", "grant"))
    ]

    assert raising == []


def test_invariant_2_e_the_approval_actor_and_reason_cannot_disagree_with_the_event() -> None:
    """**承認の名前と理由を、別の文言で記録できない。**"""
    document = report_document()
    with pytest.raises(ValidationError, match="actor / reason"):
        AuthorityEvent(
            revision=1,
            occurred_at_ms=NOW_MS,
            kind=AuthorityChangeKind.RAISED,
            trigger=AuthorityTrigger.HUMAN,
            from_stage=AuthorityStage.SHADOW,
            to_stage=AuthorityStage.LIMITED,
            actor="someone.else",
            reason="別の理由",
            approval=approval_for(document),
        )


# --- 不変条件 3: 承認は使い回せない ---------------------------------------------


def test_invariant_3_a_an_approval_cannot_be_replayed(tmp_path: Path) -> None:
    """**同じ承認で2回上げない。** revision に束縛する。"""
    authority = store(tmp_path)
    document = report_document()
    approval = approval_for(document)
    raise_stage(authority, approval=approval, document=document)

    with pytest.raises(AuthorityApprovalError, match="使い回せない"):
        raise_stage(authority, approval=approval, document=document)


def test_invariant_3_b_an_approval_is_void_once_the_journal_moved(tmp_path: Path) -> None:
    """**承認のあとに何か起きたら、その承認は使えない。**"""
    authority = store(tmp_path)
    document = report_document()
    approval = approval_for(document, from_stage=AuthorityStage.SHADOW, revision=0)
    # 承認を取ったあとに別の変更（ここでは人の rollback）が入る。
    authority.lower_stage(to_stage=AuthorityStage.SHADOW, actor="operator", reason="noop")
    raise_stage(authority, approval=approval, document=document)
    authority.rollback_to_baseline(actor="operator", reason="様子を見る")

    with pytest.raises(AuthorityApprovalError, match="使い回せない"):
        raise_stage(authority, approval=approval, document=document)


def test_invariant_3_c_a_stale_approval_is_refused(tmp_path: Path) -> None:
    """**承認を貯めて後から使えない。** 古い承認は期限切れにする。"""
    limit_ms = DEFAULT_CONFIG.policy.authority_rollout.approval_max_age_ms.value
    document = report_document()
    approval = approval_for(document, approved_at_ms=NOW_MS - limit_ms - 1)

    with pytest.raises(AuthorityApprovalError, match="承認が古い"):
        raise_stage(store(tmp_path), approval=approval, document=document)


class WaitingClock:
    """1回目の読み取りのあと、lock を待っている間に時間が進む時計。"""

    def __init__(self, first_ms: int, after_ms: int) -> None:
        self._times = [first_ms, after_ms]

    def now_ms(self) -> int:
        return self._times.pop(0) if len(self._times) > 1 else self._times[0]


def test_invariant_3_f_an_approval_that_expires_while_waiting_for_the_locks_is_refused(
    tmp_path: Path,
) -> None:
    """**lock を待っている間に切れた承認で昇格しない**（codex #4057064071）。

    `raise_stage()` は registry と authority の lock を待つ。時計を lock の前に1回だけ
    読むと、待っている間に期限が切れた承認・証拠でも通ってしまう。期限の判断は
    **両方の lock を取ったあとの時刻**で行う。
    """
    limit_ms = DEFAULT_CONFIG.policy.authority_rollout.approval_max_age_ms.value
    document = report_document()
    approval = approval_for(document, approved_at_ms=NOW_MS - limit_ms)
    authority = AuthorityStore(tmp_path / "authority", WaitingClock(NOW_MS, NOW_MS + limit_ms + 1))

    with pytest.raises(AuthorityApprovalError, match="承認が古い"):
        raise_stage(authority, approval=approval, document=document)


def test_invariant_3_g_evidence_that_expires_while_waiting_for_the_locks_is_refused(
    tmp_path: Path,
) -> None:
    """**証拠の新しさも、lock を取ったあとの時刻で判断する。**"""
    rollout = DEFAULT_CONFIG.policy.authority_rollout
    evidence_limit_ms = rollout.evidence_max_age_ms.value
    document = report_document(end_ms=NOW_MS - evidence_limit_ms)
    approval = approval_for(
        document, evidence=evidence_for(document, end_ms=NOW_MS - evidence_limit_ms)
    )
    authority = AuthorityStore(tmp_path / "authority", WaitingClock(NOW_MS, NOW_MS + 60_000))

    with pytest.raises(AuthorityEvidenceError, match="証拠が古い"):
        raise_stage(authority, approval=approval, document=document)


def test_invariant_3_d_a_future_approval_is_refused(tmp_path: Path) -> None:
    """**未来の承認を受け取らない。** 時刻をずらして期限切れを回避させない。"""
    document = report_document()
    approval = approval_for(document, approved_at_ms=NOW_MS + 1)

    with pytest.raises(AuthorityApprovalError, match="未来の承認"):
        raise_stage(store(tmp_path), approval=approval, document=document)


def test_invariant_3_e_an_approval_for_another_transition_is_refused(tmp_path: Path) -> None:
    """**別の遷移の承認を流用できない。**"""
    authority = store(tmp_path)
    document = report_document()
    raise_stage(authority, approval=approval_for(document), document=document)
    # いまは LIMITED。SHADOW→LIMITED の承認を revision だけ合わせて出し直す。
    stale = approval_for(document, from_stage=AuthorityStage.SHADOW, revision=1)

    with pytest.raises(AuthorityApprovalError, match="遷移元"):
        raise_stage(authority, approval=stale, document=document)


# --- 不変条件 4: 1段ずつ、設定の上限まで ----------------------------------------


@pytest.mark.parametrize(
    ("from_stage", "to_stage"),
    [
        (AuthorityStage.SHADOW, AuthorityStage.EXPANDED),
        (AuthorityStage.SHADOW, AuthorityStage.FULL),
        (AuthorityStage.LIMITED, AuthorityStage.FULL),
        (AuthorityStage.LIMITED, AuthorityStage.SHADOW),
        (AuthorityStage.FULL, AuthorityStage.FULL),
    ],
)
def test_invariant_4_a_an_approval_cannot_skip_or_reverse_a_stage(
    from_stage: AuthorityStage, to_stage: AuthorityStage
) -> None:
    """**Shadow から Full へ飛ばない。** 承認の型そのものが1段しか表現できない。"""
    document = report_document()
    with pytest.raises(ValidationError, match="1段ずつ"):
        StageApproval(
            from_stage=from_stage,
            to_stage=to_stage,
            expected_revision=0,
            approver=APPROVER,
            approved_at_ms=NOW_MS,
            reason="まとめて上げたい",
            evidence=evidence_for(document),
        )


def test_invariant_4_b_an_approval_above_the_configured_ceiling_is_refused(
    tmp_path: Path,
) -> None:
    """**設定が許していない stage は、承認があっても与えない**（#103 が上限を持つ）。"""
    document = report_document()

    with pytest.raises(AuthorityApprovalError, match="上限"):
        raise_stage(
            store(tmp_path),
            approval=approval_for(document),
            document=document,
            config=control_config(tmp_path, ceiling="shadow"),
        )


def test_invariant_4_c_the_stage_order_has_no_gaps() -> None:
    """stage の並びが4段で、上下が噛み合っていること。"""
    assert STAGE_ORDER == (
        AuthorityStage.SHADOW,
        AuthorityStage.LIMITED,
        AuthorityStage.EXPANDED,
        AuthorityStage.FULL,
    )
    assert stage_below(AuthorityStage.SHADOW) is None
    assert stage_above(AuthorityStage.FULL) is None
    assert lowest_stage(AuthorityStage.FULL, AuthorityStage.LIMITED) is AuthorityStage.LIMITED


# --- 不変条件 5: 証拠は完全・新鮮・束縛済み -------------------------------------


def test_invariant_5_a_a_blocked_gate_does_not_raise_the_stage(tmp_path: Path) -> None:
    """**gate を通っていない証拠では上げない**（0054 の判定をそのまま使う）。"""
    document = report_document(outcome=GateOutcome.BLOCKED)

    with pytest.raises(AuthorityEvidenceError, match="gate を通っていない"):
        raise_stage(store(tmp_path), approval=approval_for(document), document=document)


def test_invariant_5_b_evidence_pointing_at_another_report_is_refused(tmp_path: Path) -> None:
    """**承認が指した報告と、渡された報告が同じであること。**"""
    approved = report_document()
    swapped = report_document(end_ms=EVIDENCE_END_MS - 1_000)

    with pytest.raises(AuthorityEvidenceError, match="渡された報告が違う"):
        raise_stage(store(tmp_path), approval=approval_for(approved), document=swapped)


def test_invariant_5_c_evidence_for_another_artifact_is_refused(tmp_path: Path) -> None:
    """**いま Production の artifact の実績でなければ使えない。**"""
    other = "b" * 64
    document = report_document(artifacts=(other,))
    approval = approval_for(document, evidence=evidence_for(document, artifact=other))

    with pytest.raises(AuthorityEvidenceError, match="Production の artifact"):
        raise_stage(store(tmp_path), approval=approval, document=document)


def test_invariant_5_d_a_report_mixing_artifacts_is_refused(tmp_path: Path) -> None:
    """**借りた証拠で上げない。** 別 artifact が混ざった報告では帰属が決まらない。"""
    document = report_document(artifacts=(ARTIFACT_SHA, "b" * 64))

    with pytest.raises(AuthorityEvidenceError, match="Production 以外の artifact"):
        raise_stage(store(tmp_path), approval=approval_for(document), document=document)


def test_invariant_5_e_stale_evidence_is_refused(tmp_path: Path) -> None:
    """**古い証拠で上げない。** 新しさは報告の run の終了時刻で測る。"""
    limit_ms = DEFAULT_CONFIG.policy.authority_rollout.evidence_max_age_ms.value
    end_ms = NOW_MS - limit_ms - 1
    document = report_document(end_ms=end_ms)
    approval = approval_for(document, evidence=evidence_for(document, end_ms=end_ms))

    with pytest.raises(AuthorityEvidenceError, match="証拠が古い"):
        raise_stage(store(tmp_path), approval=approval, document=document)


def test_invariant_5_f_a_declared_evidence_time_that_differs_from_the_report_is_refused(
    tmp_path: Path,
) -> None:
    """**自己申告の時刻で新しさを名乗れない。** 報告の中の時刻と一致させる。"""
    document = report_document()
    approval = approval_for(document, evidence=evidence_for(document, end_ms=NOW_MS))

    with pytest.raises(AuthorityEvidenceError, match="最終観測時刻"):
        raise_stage(store(tmp_path), approval=approval, document=document)


@pytest.mark.parametrize("field", ["fan_policy_config_sha256", "safety_config_sha256"])
def test_invariant_5_g_evidence_from_another_configuration_is_refused(
    tmp_path: Path, field: str
) -> None:
    """**別の設定で取った証拠は使えない。** 閾値が違えば実績の意味も違う。"""
    other = "c" * 64
    document = report_document(
        policy_sha=other if field == "fan_policy_config_sha256" else POLICY_SHA,
        safety_sha=other if field == "safety_config_sha256" else SAFETY_SHA,
    )
    approval = approval_for(
        document,
        evidence=evidence_for(
            document,
            policy_sha=other if field == "fan_policy_config_sha256" else POLICY_SHA,
            safety_sha=other if field == "safety_config_sha256" else SAFETY_SHA,
        ),
    )

    with pytest.raises(AuthorityEvidenceError, match="いまの"):
        raise_stage(store(tmp_path), approval=approval, document=document)


def test_invariant_5_h_evidence_taken_at_a_higher_authority_is_refused(tmp_path: Path) -> None:
    """**上の stage で取った実績で、下から上げない。** 降格後の再昇格が素通りする。"""
    document = report_document(stages=(AuthorityStage.FULL.value,))

    with pytest.raises(AuthorityEvidenceError, match="高い authority"):
        raise_stage(store(tmp_path), approval=approval_for(document), document=document)


def test_invariant_5_i_evidence_without_the_current_stage_is_refused(tmp_path: Path) -> None:
    """**いまの stage で運転した実績が要る。** 昇格は1段ぶんの経験で判断する。"""
    authority = store(tmp_path)
    first = report_document()
    raise_stage(authority, approval=approval_for(first), document=first)
    # いまは LIMITED だが、証拠は SHADOW の区間しか含まない。
    document = report_document(stages=(AuthorityStage.SHADOW.value,))
    approval = approval_for(document, from_stage=AuthorityStage.LIMITED, revision=1)

    with pytest.raises(AuthorityEvidenceError, match="いまの stage で運転した証拠"):
        raise_stage(authority, approval=approval, document=document)


def test_invariant_5_j_a_missing_gate_for_the_arm_is_refused(tmp_path: Path) -> None:
    """**判定していないことを合格にしない**（0054 の fail closed と同じ向き）。

    報告に実績はあるのに gate 行が無い Learned MPC の arm が1つでもあれば通さない。
    """
    other = CounterfactualArm(
        controller=ControllerKind.LEARNED_MPC,
        supervisor_policy=SupervisorPolicyKind.RL,
        authority_stage=AuthorityStage.SHADOW,
    )
    document = report_document(extra_learned=other)
    payload = json.loads(document)
    payload["gates"] = [gate for gate in payload["gates"] if gate["arm_key"] != other.key]
    document = json.dumps(payload).encode("utf-8")
    approval = approval_for(document, evidence=evidence_for(document))

    with pytest.raises(AuthorityEvidenceError, match="gate の判定が無い"):
        raise_stage(store(tmp_path), approval=approval, document=document)


def test_invariant_5_l_a_gate_row_without_an_arm_report_is_refused(tmp_path: Path) -> None:
    """**holdout の実績に無い arm を名指せない。** 行だけの gate で昇格しない。"""
    document = report_document(arm="counterfactual:learned_mpc+rule_policy@full")
    approval = approval_for(
        document,
        evidence=evidence_for(document, arm="counterfactual:learned_mpc+rule_policy@full"),
    )

    with pytest.raises(AuthorityEvidenceError, match="holdout の報告に無い"):
        raise_stage(store(tmp_path), approval=approval, document=document)


def test_invariant_5_m_an_applied_fallback_arm_cannot_justify_a_promotion(
    tmp_path: Path,
) -> None:
    """**Fallback の arm を名指して、落ちた Learned MPC のまま上げられない**（codex #4056864033）。

    `arm_key` の一致だけで gate を読むと、実 Fan を作っていた Fallback の合格で
    昇格でき、肝心の Learned MPC が `blocked` のまま authority が増える。
    """
    document = report_document(
        arm=FALLBACK_ARM,
        outcome=GateOutcome.PASS,
        with_applied_fallback=True,
    )
    payload = json.loads(document)
    for gate in payload["gates"]:
        if gate["arm_key"] != FALLBACK_ARM:
            gate["outcome"] = "blocked"
            gate["blocking_stage"] = "safety"
            gate["conditions"][0]["outcome"] = "blocked"
            gate["conditions"][0]["observed"] = 1.0
    document = json.dumps(payload).encode("utf-8")
    approval = approval_for(document, evidence=evidence_for(document, arm=FALLBACK_ARM))

    with pytest.raises(AuthorityEvidenceError, match="Learned MPC 以外の arm"):
        raise_stage(store(tmp_path), approval=approval, document=document)


def test_invariant_5_o_an_applied_learned_arm_cannot_justify_a_promotion(
    tmp_path: Path,
) -> None:
    """**適用側の arm は artifact へ束縛できないので根拠にできない**（codex #4057064074）。

    `ModelGateDecision` は artifact の hash を持たず、適用 arm の鍵にも model の identity が
    入らない。`provenance.model_artifacts` は counterfactual の `artifact_sha256` からしか
    集まらないので、「B の適用実績 + A の counterfactual」という報告が {A} の照合を通る。
    **束縛できない証拠は使わない**（fail closed）。trace が tick ごとの artifact を記録
    するようになったら開ける（0057 §5）。
    """
    authority = store(tmp_path)
    first = report_document()
    raise_stage(authority, approval=approval_for(first), document=first)

    applied_learned = AppliedArm(
        controller=ControllerKind.LEARNED_MPC,
        supervisor_policy=SupervisorPolicyKind.RULE,
        authority_stage=AuthorityStage.LIMITED,
        operating_mode=OperatingMode.AUTO,
    )
    document = report_document(
        arm=applied_learned.key,
        stages=(AuthorityStage.LIMITED.value,),
        arm_stage=AuthorityStage.LIMITED,
    )
    payload = json.loads(document)
    groups = payload["segments"][0]["groups"][0]
    groups["applied"] = [
        json.loads(applied_report(applied_learned, end_ms=EVIDENCE_END_MS).model_dump_json())
    ]
    document = json.dumps(payload).encode("utf-8")
    approval = approval_for(
        document,
        from_stage=AuthorityStage.LIMITED,
        revision=1,
        evidence=evidence_for(document, arm=applied_learned.key),
    )

    with pytest.raises(AuthorityEvidenceError, match="適用側の arm"):
        raise_stage(authority, approval=approval, document=document)


def test_invariant_5_n_a_blocked_sibling_learned_arm_blocks_the_promotion(
    tmp_path: Path,
) -> None:
    """**良い arm だけを選んで上げられない。** 落ちた Learned MPC が残っていれば通さない。"""
    other = CounterfactualArm(
        controller=ControllerKind.LEARNED_MPC,
        supervisor_policy=SupervisorPolicyKind.RL,
        authority_stage=AuthorityStage.SHADOW,
    )
    document = report_document(extra_learned=other, extra_outcome=GateOutcome.BLOCKED)
    approval = approval_for(document, evidence=evidence_for(document))

    with pytest.raises(AuthorityEvidenceError, match="rollout gate を通っていない"):
        raise_stage(store(tmp_path), approval=approval, document=document)


def test_invariant_5_k_evidence_from_another_comparison_is_refused(tmp_path: Path) -> None:
    """**同じ条件で比べた報告であること**（`conditions_sha256`。0054 §2.7）。"""
    document = report_document(conditions_sha="d" * 64)
    approval = approval_for(document, evidence=evidence_for(document))

    with pytest.raises(AuthorityEvidenceError, match="比較条件"):
        raise_stage(store(tmp_path), approval=approval, document=document)


def test_invariant_5_p_a_fresh_fallback_only_run_does_not_refresh_stale_evidence(
    tmp_path: Path,
) -> None:
    """**Fallback だけの新しい区間を足しても、古い Learned の実績は新鮮にならない**
    （codex #4056903573）。

    新しさは報告全体の最新 run ではなく、**名指した arm の実績が実在する最後の区間**で測る。
    """
    limit_ms = DEFAULT_CONFIG.policy.authority_rollout.evidence_max_age_ms.value
    stale_end_ms = NOW_MS - limit_ms - 1
    document = report_document(end_ms=stale_end_ms, fresh_fallback_end_ms=NOW_MS - 60_000)

    # 報告全体の最新 run は新しい。それを名乗ると、arm の実績と食い違うので拒まれる。
    with pytest.raises(AuthorityEvidenceError, match="名指した arm の実績と違う"):
        raise_stage(
            store(tmp_path),
            approval=approval_for(
                document, evidence=evidence_for(document, end_ms=NOW_MS - 60_000)
            ),
            document=document,
        )

    # arm の実績の時刻を正しく名乗れば、こんどは「古い」として拒まれる。
    with pytest.raises(AuthorityEvidenceError, match="証拠が古い"):
        raise_stage(
            store(tmp_path),
            approval=approval_for(document, evidence=evidence_for(document, end_ms=stale_end_ms)),
            document=document,
        )


def test_invariant_5_q_a_fallback_only_continuation_does_not_refresh_the_same_segment(
    tmp_path: Path,
) -> None:
    """**同じ segment の続きを Fallback で回しても、Learned の実績は新鮮にならない**
    （codex #4056942799）。

    segment の `end_ms` は区間の終わりであって、その arm が最後に動いた時刻ではない。
    新しさは arm 自身の `last_ts_ms` で測る。
    """
    limit_ms = DEFAULT_CONFIG.policy.authority_rollout.evidence_max_age_ms.value
    stale_ts_ms = NOW_MS - limit_ms - 1
    document = report_document(
        end_ms=NOW_MS - 60_000,
        learned_last_ts_ms=stale_ts_ms,
        with_applied_fallback=True,
        start_ms=stale_ts_ms - 3_600_000,
    )

    # segment の終わり（新しい）を名乗ると、arm の実績と食い違う。
    with pytest.raises(AuthorityEvidenceError, match="名指した arm の実績と違う"):
        raise_stage(
            store(tmp_path),
            approval=approval_for(
                document, evidence=evidence_for(document, end_ms=NOW_MS - 60_000)
            ),
            document=document,
        )

    # arm 自身の時刻を名乗れば、こんどは「古い」として拒まれる。
    with pytest.raises(AuthorityEvidenceError, match="証拠が古い"):
        raise_stage(
            store(tmp_path),
            approval=approval_for(document, evidence=evidence_for(document, end_ms=stale_ts_ms)),
            document=document,
        )


def test_invariant_5_r_a_current_failure_does_not_refresh_stale_evidence(
    tmp_path: Path,
) -> None:
    """**いまの読み込み失敗を1つ足しても、古い実績は新鮮にならない**（codex #4057035287）。

    counterfactual の `last_ts_ms` には、提案を作れなかった tick（model の読み込み失敗など）も
    入る。そちらで測ると、gate の集計は何も変わらないまま新しさだけが更新できてしまう。
    新しさは**裏づけのある提案が実在した時刻**（`last_attested_ts_ms`）で測る。
    """
    limit_ms = DEFAULT_CONFIG.policy.authority_rollout.evidence_max_age_ms.value
    stale_ts_ms = NOW_MS - limit_ms - 1
    document = report_document(
        end_ms=NOW_MS - 60_000,
        learned_last_attested_ts_ms=stale_ts_ms,
        start_ms=stale_ts_ms - 3_600_000,
    )

    # arm の最後の tick（失敗だけの新しい tick）を名乗ると、実績と食い違う。
    with pytest.raises(AuthorityEvidenceError, match="名指した arm の実績と違う"):
        raise_stage(
            store(tmp_path),
            approval=approval_for(
                document, evidence=evidence_for(document, end_ms=NOW_MS - 60_000)
            ),
            document=document,
        )

    # 裏づけのある提案の時刻を名乗れば、こんどは「古い」として拒まれる。
    with pytest.raises(AuthorityEvidenceError, match="証拠が古い"):
        raise_stage(
            store(tmp_path),
            approval=approval_for(document, evidence=evidence_for(document, end_ms=stale_ts_ms)),
            document=document,
        )


def test_invariant_5_s_an_arm_without_any_attested_proposal_cannot_justify_a_promotion(
    tmp_path: Path,
) -> None:
    """**裏づけのある提案が1つも無い arm を根拠にできない。** 判定できないことを通さない。"""
    document = report_document()
    payload = json.loads(document)
    arm_report = payload["segments"][0]["groups"][0]["counterfactual"][0]
    arm_report["last_attested_ts_ms"] = None
    arm_report["attested_ticks"] = 0
    document = json.dumps(payload).encode("utf-8")
    approval = approval_for(document, evidence=evidence_for(document))

    with pytest.raises(AuthorityEvidenceError, match="裏づけのある提案が1つも無い"):
        raise_stage(store(tmp_path), approval=approval, document=document)


# --- 不変条件 6: 降格に承認は要らない ------------------------------------------


def test_invariant_6_a_repeated_fallback_lowers_the_stage_without_an_approval(
    tmp_path: Path,
) -> None:
    """**Gate の降格推奨（#79）を、承認なしで stage へ反映できる。**"""
    control = runtime(tmp_path, stage=AuthorityStage.FULL)

    demotion = control.observe(
        safety_state=SafetyState.NORMAL,
        demotion_recommended=True,
        now_mono_ms=1_000,
    )

    assert demotion is not None
    assert demotion.cause is AutomaticCause.REPEATED_FALLBACK
    assert demotion.to_stage is AuthorityStage.EXPANDED
    assert demotion.persisted is True
    assert control.current_stage() is AuthorityStage.EXPANDED
    event = control.journal.events[-1]
    assert event.trigger is AuthorityTrigger.AUTOMATIC
    assert event.approval is None
    assert event.reason


def test_invariant_6_b_persistent_low_confidence_and_ood_lower_the_stage(
    tmp_path: Path,
) -> None:
    """**低 confidence / OOD が続いたら自動で下げる**（Issue の原則）。"""
    settings = policy(authority="full", low_confidence_after=3, ood_after=2)
    control = runtime(tmp_path, stage=AuthorityStage.FULL, settings=settings)

    assert (
        control.observe(
            safety_state=SafetyState.NORMAL,
            confidence_level=ConfidenceLevel.LOW,
            now_mono_ms=0,
        )
        is None
    )
    assert (
        control.observe(
            safety_state=SafetyState.NORMAL,
            confidence_level=ConfidenceLevel.LOW,
            now_mono_ms=1,
        )
        is None
    )
    demotion = control.observe(
        safety_state=SafetyState.NORMAL,
        confidence_level=ConfidenceLevel.LOW,
        now_mono_ms=2,
    )

    assert demotion is not None
    assert demotion.cause is AutomaticCause.PERSISTENT_LOW_CONFIDENCE
    assert control.current_stage() is AuthorityStage.EXPANDED

    ood = control.observe(safety_state=SafetyState.NORMAL, ood=True, now_mono_ms=3)
    assert ood is None, "数え直しは降格のたびに始める"
    second = control.observe(safety_state=SafetyState.NORMAL, ood=True, now_mono_ms=4)
    assert second is not None
    assert second.cause is AutomaticCause.PERSISTENT_OOD
    assert control.current_stage() is AuthorityStage.LIMITED


def test_invariant_6_c_old_unhealthy_ticks_fall_out_of_the_window(tmp_path: Path) -> None:
    """**窓の外の不健全を数え続けない。** 何日か回せば必ず下がる、にはしない。"""
    settings = policy(authority="full", low_confidence_after=2)
    window_ms = settings.authority_rollout.unhealthy_window_ms.value
    control = runtime(tmp_path, stage=AuthorityStage.FULL, settings=settings)

    control.observe(
        safety_state=SafetyState.NORMAL, confidence_level=ConfidenceLevel.LOW, now_mono_ms=0
    )
    late = control.observe(
        safety_state=SafetyState.NORMAL,
        confidence_level=ConfidenceLevel.LOW,
        now_mono_ms=window_ms + 1,
    )

    assert late is None
    assert control.current_stage() is AuthorityStage.FULL


def test_invariant_6_d_an_emergency_rolls_back_to_baseline(tmp_path: Path) -> None:
    """**Safety の EMERGENCY は1手で Baseline へ戻す。** 段階を下りない。"""
    control = runtime(tmp_path, stage=AuthorityStage.FULL)

    demotion = control.observe(safety_state=SafetyState.EMERGENCY, now_mono_ms=0)

    assert demotion is not None
    assert demotion.cause is AutomaticCause.SAFETY_EMERGENCY
    assert demotion.to_stage is BASELINE_STAGE
    assert control.current_stage() is BASELINE_STAGE


def test_invariant_6_e_a_demotion_applies_even_when_it_cannot_be_persisted(
    tmp_path: Path,
) -> None:
    """**書き残せなくても下げる。** disk が一杯な間ほど高い authority で回らせない。"""
    control = unwritable_runtime(tmp_path)

    demotion = control.observe(safety_state=SafetyState.EMERGENCY, now_mono_ms=0)

    assert demotion is not None
    assert demotion.persisted is False
    assert demotion.persist_failure is not None
    assert control.current_stage() is BASELINE_STAGE
    assert control.persist_failure is not None


def test_invariant_6_f_reloading_does_not_undo_a_demotion(tmp_path: Path) -> None:
    """**読み直しで authority が戻らない。** 戻れば降格を「読むだけ」で取り消せる。"""
    control = unwritable_runtime(tmp_path)

    control.observe(safety_state=SafetyState.EMERGENCY, now_mono_ms=0)
    control.reload()
    assert control.journal.stage is AuthorityStage.FULL, "journal には書けていない"

    assert control.current_stage() is BASELINE_STAGE


def test_invariant_6_g_rollback_to_baseline_is_one_step(tmp_path: Path) -> None:
    """**Rollback は1手で実行できる**（受入基準「Rollback で Baseline へ戻れる」）。"""
    control = runtime(tmp_path, stage=AuthorityStage.FULL)

    demotion = control.rollback_to_baseline(actor="operator", reason="異音の切り分け")

    assert demotion is not None
    assert demotion.to_stage is BASELINE_STAGE
    assert control.current_stage() is BASELINE_STAGE
    event = control.journal.events[-1]
    assert event.trigger is AuthorityTrigger.HUMAN
    assert event.cause is None
    assert event.approval is None


def test_invariant_6_h_a_demotion_cannot_carry_an_approval() -> None:
    """**降格に承認を付けない。** 付けられると「承認が要る」実装が書けてしまう。"""
    document = report_document()
    with pytest.raises(ValidationError, match="降格に承認を付けない"):
        AuthorityEvent(
            revision=1,
            occurred_at_ms=NOW_MS,
            kind=AuthorityChangeKind.LOWERED,
            trigger=AuthorityTrigger.HUMAN,
            from_stage=AuthorityStage.LIMITED,
            to_stage=AuthorityStage.SHADOW,
            actor=APPROVER,
            reason="下げる",
            approval=approval_for(document),
        )


def test_invariant_6_i_recovering_after_an_automatic_demotion_needs_a_new_approval(
    tmp_path: Path,
) -> None:
    """**自動で下がったあとに、自動では戻らない。** 戻すのは人の承認だけ。"""
    authority = store(tmp_path)
    document = report_document()
    raise_stage(authority, approval=approval_for(document), document=document)
    control = AuthorityRuntime(authority, policy(authority="full"))
    control.observe(safety_state=SafetyState.EMERGENCY, now_mono_ms=0)
    assert control.current_stage() is BASELINE_STAGE

    for tick in range(100):
        assert control.observe(safety_state=SafetyState.NORMAL, now_mono_ms=tick + 1) is None
    control.reload()

    assert control.current_stage() is BASELINE_STAGE
    assert authority.read().stage is BASELINE_STAGE


def test_invariant_6_j_a_rewound_clock_does_not_block_a_demotion(tmp_path: Path) -> None:
    """**壁時計が巻き戻っても降格は書ける。** 書けなくなるのは安全側の壊れ方ではない。"""
    authority = store(tmp_path)
    document = report_document()
    raise_stage(authority, approval=approval_for(document), document=document)

    journal = store(tmp_path, now_ms=NOW_MS - 60_000).rollback_to_baseline(
        actor="operator", reason="時計が巻き戻った"
    )

    assert journal.stage is BASELINE_STAGE
    assert journal.events[-1].occurred_at_ms == NOW_MS


def test_invariant_6_k_one_threshold_crossing_lowers_exactly_one_stage(tmp_path: Path) -> None:
    """**1回の閾値超えで1段だけ下げる**（codex #4056903574）。

    Gate の降格推奨は自分の窓の間ずっと立ったままなので、毎 tick 消費すると
    FULL → EXPANDED → LIMITED → SHADOW と連鎖してしまう。立ち上がりだけを消費する。
    """
    control = runtime(tmp_path, stage=AuthorityStage.FULL)

    first = control.observe(
        safety_state=SafetyState.NORMAL, demotion_recommended=True, now_mono_ms=0
    )
    assert first is not None
    assert control.current_stage() is AuthorityStage.EXPANDED

    for tick in range(1, 6):
        assert (
            control.observe(
                safety_state=SafetyState.NORMAL, demotion_recommended=True, now_mono_ms=tick
            )
            is None
        ), "推奨が立ったままの間は下げ続けない"
    assert control.current_stage() is AuthorityStage.EXPANDED

    # いちど収まり、もういちど超えたら、もう1段だけ下がる。
    assert (
        control.observe(safety_state=SafetyState.NORMAL, demotion_recommended=False, now_mono_ms=6)
        is None
    )
    second = control.observe(
        safety_state=SafetyState.NORMAL, demotion_recommended=True, now_mono_ms=7
    )

    assert second is not None
    assert control.current_stage() is AuthorityStage.LIMITED


def test_invariant_6_m_an_approved_promotion_after_a_persisted_demotion_takes_effect(
    tmp_path: Path,
) -> None:
    """**書き残せた降格は journal が表す。** 承認された昇格が再起動を待たない
    （codex #4056968495）。

    memory 上の上限を二重に持つと、記録に残った降格のあとで承認された昇格が、
    process を作り直すまで効かなくなる。
    """
    authority = store(tmp_path)
    control = runtime(tmp_path, stage=AuthorityStage.FULL)
    demotion = control.observe(safety_state=SafetyState.EMERGENCY, now_mono_ms=0)

    assert demotion is not None
    assert demotion.persisted is True
    assert control.current_stage() is BASELINE_STAGE

    # 人が承認して上げる（別の管理操作）。
    document = report_document()
    journal = raise_stage(
        authority,
        approval=approval_for(document, revision=control.journal.revision),
        document=document,
    )
    assert journal.stage is AuthorityStage.LIMITED

    control.reload()

    assert control.current_stage() is AuthorityStage.LIMITED
    assert control.trace_metadata()["authority_unpersisted_ceiling"] is None


def test_invariant_6_n_an_unpersisted_demotion_still_survives_a_promotion(
    tmp_path: Path,
) -> None:
    """**書き残せなかった降格は、読み直しでも昇格でも外れない。**"""
    control = unwritable_runtime(tmp_path, stage=AuthorityStage.LIMITED)
    control.observe(safety_state=SafetyState.EMERGENCY, now_mono_ms=0)
    assert control.current_stage() is BASELINE_STAGE
    assert control.trace_metadata()["authority_unpersisted_ceiling"] == BASELINE_STAGE.value

    authority = store(tmp_path)
    document = report_document(
        stages=(AuthorityStage.LIMITED.value,), arm_stage=AuthorityStage.LIMITED
    )
    raise_stage(
        authority,
        approval=approval_for(
            document,
            from_stage=AuthorityStage.LIMITED,
            revision=authority.read().revision,
            evidence=evidence_for(document, arm=learned_arm(AuthorityStage.LIMITED).key),
        ),
        document=document,
    )
    control.reload()

    assert control.current_stage() is BASELINE_STAGE, "記録の無い降格は読み直しで外れない"


def test_invariant_6_o_a_demotion_takes_effect_before_it_is_persisted(tmp_path: Path) -> None:
    """**安全側の変更は、disk も他 process の lock も待たない**（codex #4056992239）。

    `lower_stage()` は flock と fsync を待つ。別 process が昇格で lock を握っていれば
    その間ずっと待つ。**書き始める前にもう下がっている**ことを確かめる。
    """
    seen: list[AuthorityStage] = []
    holder: list[AuthorityRuntime] = []

    class WatchingStore(AuthorityStore):
        """書き始める瞬間の実効 stage を記録する store。"""

        __slots__ = ()

        def lower_stage(self, **kwargs: object) -> AuthorityJournal:  # type: ignore[override]
            seen.append(holder[0].current_stage())
            return super().lower_stage(**kwargs)  # type: ignore[arg-type]

    runtime(tmp_path, stage=AuthorityStage.FULL)
    control = AuthorityRuntime(
        WatchingStore(tmp_path / "authority", SimulatedClock(NOW_MS), lock_timeout_ms=500),
        policy(authority="full"),
    )
    holder.append(control)
    assert control.current_stage() is AuthorityStage.FULL

    demotion = control.observe(safety_state=SafetyState.EMERGENCY, now_mono_ms=0)

    assert demotion is not None
    assert demotion.persisted is True
    assert seen == [BASELINE_STAGE], "書き始める前にもう下がっている"


def test_invariant_6_p_an_unexpected_persist_failure_still_lowers(tmp_path: Path) -> None:
    """**下げたことを、例外の種類に依存させない。** 想定外の失敗でも下がったままにする。"""

    class ExplodingStore(AuthorityStore):
        __slots__ = ()

        def lower_stage(self, **kwargs: object) -> AuthorityJournal:  # type: ignore[override]
            raise RuntimeError("想定していない失敗")

    runtime(tmp_path, stage=AuthorityStage.FULL)
    control = AuthorityRuntime(
        ExplodingStore(tmp_path / "authority", SimulatedClock(NOW_MS), lock_timeout_ms=500),
        policy(authority="full"),
    )

    with pytest.raises(RuntimeError):
        control.observe(safety_state=SafetyState.EMERGENCY, now_mono_ms=0)

    assert control.current_stage() is BASELINE_STAGE


# --- 不変条件 7: 設定は上限 ----------------------------------------------------


def test_invariant_6_l_a_rewound_monotonic_clock_is_refused(tmp_path: Path) -> None:
    """**巻き戻った単調時刻を受け取らない。** 窓の外へ出た不健全が数え直される。"""
    control = runtime(tmp_path, stage=AuthorityStage.FULL)
    control.observe(safety_state=SafetyState.NORMAL, now_mono_ms=1_000)

    with pytest.raises(ValueError, match="巻き戻せない"):
        control.observe(safety_state=SafetyState.NORMAL, now_mono_ms=999)


def test_invariant_7_a_the_configured_ceiling_caps_the_effective_stage(tmp_path: Path) -> None:
    """**設定を下げれば、journal が高くても実効 stage が下がる。**"""
    control = runtime(tmp_path, stage=AuthorityStage.FULL, ceiling="limited")

    assert control.journal.stage is AuthorityStage.FULL
    assert control.configured_ceiling is AuthorityStage.LIMITED
    assert control.current_stage() is AuthorityStage.LIMITED


def test_invariant_7_b_raising_the_configured_ceiling_does_not_raise_the_journal(
    tmp_path: Path,
) -> None:
    """**設定を上げただけでは制御権は増えない。** 昇格は承認の記録が要る。"""
    authority = store(tmp_path)

    control = AuthorityRuntime(authority, policy(authority="full"))

    assert control.configured_ceiling is AuthorityStage.FULL
    assert control.current_stage() is BASELINE_STAGE
    assert authority.read().revision == 0


def test_invariant_7_c_the_gate_never_uses_a_stage_above_the_configured_ceiling() -> None:
    """**Gate 側でも上限を掛ける。** 嘘の stage を渡しても設定を超えない。"""

    class Lying:
        def current_stage(self) -> AuthorityStage:
            return AuthorityStage.FULL

    settings = policy(authority="limited", recovery_hold_ms=1)
    gate = ControllerGate(settings, expected_model_version="thermal-v1", authority=Lying())

    first = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=0, proposal=learned_proposal(0.9)),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    selection = gate.select(
        now_mono_ms=1,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=1, proposal=learned_proposal(0.9)),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert first.authority_stage is AuthorityStage.LIMITED
    assert selection.authority_stage is AuthorityStage.LIMITED
    assert selection.active_controller is ControllerKind.LEARNED_MPC
    # LIMITED は front だけを許し、Fallback から limit_up=0.1 までしか上げられない。
    assert selection.proposal.requested.front.demand == pytest.approx(0.5)
    assert selection.proposal.requested.rear.demand == pytest.approx(0.4)


def test_invariant_7_d_a_lowered_stage_puts_the_gate_back_on_fallback() -> None:
    """**stage を Shadow へ下げたら、次の tick から実 Fan は Fallback が作る。**"""
    settings = policy(authority="full", recovery_hold_ms=1)
    source = StaticAuthorityStage(AuthorityStage.FULL)
    gate = ControllerGate(settings, expected_model_version="thermal-v1", authority=source)
    gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    using_ml = gate.select(
        now_mono_ms=1,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=1),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    assert using_ml.active_controller is ControllerKind.LEARNED_MPC

    lowered = ControllerGate(
        settings,
        expected_model_version="thermal-v1",
        authority=StaticAuthorityStage(AuthorityStage.SHADOW),
    )
    selection = lowered.select(
        now_mono_ms=2,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=2),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.authority_stage is AuthorityStage.SHADOW


def test_invariant_7_e_a_proposal_made_before_a_promotion_is_not_used_after_it(
    tmp_path: Path,
) -> None:
    """**worker の照合を、提案が使われる瞬間まで運ぶ**（codex #4056903566）。

    worker は自分が走った時刻の実効 stage しか見られない。そこから Gate が選ぶまでの間に
    昇格が起きると、SHADOW だけ検証された束縛の提案が LIMITED で採られてしまう。
    """
    settings = policy(authority="full", recovery_hold_ms=1)
    source = StaticAuthorityStage(AuthorityStage.SHADOW)
    gate = ControllerGate(settings, expected_model_version="thermal-v1", authority=source)
    shadow_status = healthy_status(received=0, binding_stage=AuthorityStage.SHADOW)

    raised = ControllerGate(
        settings,
        expected_model_version="thermal-v1",
        authority=StaticAuthorityStage(AuthorityStage.LIMITED),
    )
    raised.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=shadow_status,
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    selection = raised.select(
        now_mono_ms=1,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=1, binding_stage=AuthorityStage.SHADOW),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.AUTHORITY_NOT_COVERED.value

    # 同じ提案でも、束縛が覆っている stage なら使える。
    gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=shadow_status,
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    assert gate is not None


# --- 不変条件 8: Model promotion は authority を動かさない ----------------------


def test_invariant_8_a_promoting_a_model_does_not_change_the_authority_stage(
    tmp_path: Path,
) -> None:
    """**Production への昇格で制御権は増えない**（受入基準・#104 との境界）。"""
    authority = store(tmp_path)
    document = report_document()
    raise_stage(authority, approval=approval_for(document), document=document)
    before = authority.read()

    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote_artifact(registry, "1.0.0")

    after = authority.read()
    assert after == before
    assert after.stage is AuthorityStage.LIMITED
    assert registry.inspect().production[ArtifactKind.THERMAL_MODEL].active.version == "1.0.0"
    control = AuthorityRuntime(authority, policy(authority="full"))
    assert control.current_stage() is AuthorityStage.LIMITED


def test_invariant_8_b_an_approval_for_the_retired_artifact_is_refused_after_promotion(
    tmp_path: Path,
) -> None:
    """**artifact が入れ替わったら、前の artifact の証拠は使えない。**

    承認者は artifact の hash を持ち込めない（codex #4056903570）し、発行済みの
    attestation も渡せない（codex #4056942797）。Production の identity は
    **書き込む直前に Registry から読み直す**ので、「A の実績で承認し、B に制御権を
    与える」が成立しない。
    """
    registry, old_sha = production_registry(tmp_path, version="1.0.0", name="registry")
    document = report_document(artifacts=(old_sha,))
    approval = approval_for(document, evidence=evidence_for(document, artifact=old_sha))

    # 同じ registry の production を入れ替える。承認と報告はそのまま。
    new_sha = promote_next_version(tmp_path / "registry", version="1.1.0")

    assert new_sha != old_sha
    with pytest.raises(AuthorityEvidenceError, match="Production の artifact"):
        raise_stage(store(tmp_path), approval=approval, document=document, registry=registry)


def test_invariant_8_e_the_production_identity_is_read_at_promotion_time(
    tmp_path: Path,
) -> None:
    """**production を読むのは承認の時ではなく、昇格を書く時である**（codex #4056942797）。

    `production_active` のような「発行した瞬間の写し」を受け取ると、A の証拠を持ったまま
    B が production になったあとに昇格できる。`raise_stage()` は artifact を引数に取らず、
    **その場で Registry を読む**ので、同じ承認・同じ報告でも結果が変わる。
    """
    registry, sha = production_registry(tmp_path, version="1.0.0", name="registry")
    document = report_document(artifacts=(sha,))
    approval = approval_for(document, evidence=evidence_for(document, artifact=sha))

    # まだ A が production。同じ承認は通る。
    journal = raise_stage(store(tmp_path), approval=approval, document=document, registry=registry)
    assert journal.stage is AuthorityStage.LIMITED

    # production を B へ入れ替えると、**同じ承認・同じ報告**がもう通らない。
    promote_next_version(tmp_path / "registry", version="2.0.0")
    with pytest.raises(AuthorityEvidenceError, match="Production の artifact"):
        raise_stage(
            store(tmp_path / "second"),
            approval=approval,
            document=document,
            registry=registry,
        )


def test_invariant_8_g_a_registry_without_a_production_pointer_is_refused(
    tmp_path: Path,
) -> None:
    """**production が無い Registry では上げられない。** 「判定できない」を合格にしない。"""
    registry, sha = production_registry(
        tmp_path, version="1.0.0", promoted=False, name="registry-candidate"
    )
    document = report_document(artifacts=(sha,))
    approval = approval_for(document, evidence=evidence_for(document, artifact=sha))

    with pytest.raises(AuthorityEvidenceError, match="Production の artifact が無い"):
        raise_stage(store(tmp_path), approval=approval, document=document, registry=registry)


def test_invariant_8_f_a_stage_the_registry_did_not_allow_is_refused(tmp_path: Path) -> None:
    """**Registry が許していない stage へ、authority の側から上げない**（#104 の境界）。"""
    registry, sha = production_registry(
        tmp_path,
        version="3.0.0",
        authority=(AuthorityStage.SHADOW,),
        name="registry-shadow-only",
    )
    document = report_document(artifacts=(sha,))
    approval = approval_for(document, evidence=evidence_for(document, artifact=sha))

    with pytest.raises(AuthorityApprovalError, match="Registry が許していない"):
        raise_stage(store(tmp_path), approval=approval, document=document, registry=registry)


def registry_lock_is_held(root: Path) -> bool:
    """別の fd から non-blocking で取れなければ、その lock は誰かが握っている。"""
    lock_fd = os.open(root / ".registry.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            flock(lock_fd, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            return True
        flock(lock_fd, LOCK_UN)
        return False
    finally:
        os.close(lock_fd)


def test_invariant_8_h_the_registry_is_pinned_until_the_authority_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**読んでから書くまでの間に production を動かされない**（codex #4056968492）。

    `inspect()` は戻るときに lock を手放すので、それだけでは別 process が B へ promote
    できる。`raise_stage()` は Registry を pin したまま authority journal を書く。
    """
    registry, sha = production_registry(tmp_path, version="1.0.0", name="registry")
    document = report_document(artifacts=(sha,))
    approval = approval_for(document, evidence=evidence_for(document, artifact=sha))
    observed: list[bool] = []
    original = AuthorityStore._append

    def watching_append(self, root_fd, journal, event):  # type: ignore[no-untyped-def]
        # journal を書くまさにその瞬間に、Registry の lock が握られていること。
        observed.append(registry_lock_is_held(tmp_path / "registry"))
        return original(self, root_fd, journal, event)

    monkeypatch.setattr(AuthorityStore, "_append", watching_append)
    journal = raise_stage(store(tmp_path), approval=approval, document=document, registry=registry)

    assert journal.stage is AuthorityStage.LIMITED
    assert observed == [True]


def test_invariant_8_i_lowering_never_waits_for_the_registry(tmp_path: Path) -> None:
    """**降格は Registry の lock を取らない。** 安全側へは常に動ける（0057 §2.3 / §2.6）。"""
    registry, _sha = production_registry(tmp_path, version="1.0.0", name="registry")
    authority = store(tmp_path)
    first = report_document()
    raise_stage(authority, approval=approval_for(first), document=first, registry=registry)

    # 別の誰かが Registry を握っている間でも、Baseline へ戻せる。
    with registry.pinned():
        assert registry_lock_is_held(tmp_path / "registry")
        journal = authority.rollback_to_baseline(actor="operator", reason="異音の切り分け")

    assert journal.stage is BASELINE_STAGE


def test_invariant_8_j_an_authority_failure_is_not_reported_as_a_registry_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**どの部品が落ちたのかを、その部品の理由で残す**（codex #4056992240）。

    Registry を pin する context manager の `yield` を `try` の中に置くと、
    authority 側の I/O 失敗まで「Registry を読めない」として報告してしまう。
    """
    registry, sha = production_registry(tmp_path, version="1.0.0", name="registry")
    document = report_document(artifacts=(sha,))
    approval = approval_for(document, evidence=evidence_for(document, artifact=sha))

    def exploding_append(self, root_fd, journal, event):  # type: ignore[no-untyped-def]
        raise OSError("authority journal を書けない")

    monkeypatch.setattr(AuthorityStore, "_append", exploding_append)
    with pytest.raises(OSError) as refusal:
        raise_stage(store(tmp_path), approval=approval, document=document, registry=registry)

    assert not isinstance(refusal.value, AuthorityEvidenceError)
    assert "Registry" not in str(refusal.value)


def test_invariant_8_c_the_registry_cannot_write_the_authority_journal() -> None:
    """**Model Registry は authority の状態を触らない。** 名前でも参照しない。"""
    source = Path("src/coldaisle/control/model_registry.py").read_text(encoding="utf-8")

    assert "AuthorityStore" not in source
    assert "authority.json" not in source
    assert "AuthorityJournal" not in source


def test_invariant_8_d_the_two_states_live_in_separate_files(tmp_path: Path) -> None:
    """**同じ file に同居させない。** revision を共有すると片方が片方を動かす。"""
    authority = store(tmp_path)
    document = report_document()
    raise_stage(authority, approval=approval_for(document), document=document)
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")

    written = sorted(path.name for path in (tmp_path / "authority").iterdir())

    assert "authority.json" in written
    assert not (tmp_path / "authority" / "registry.json").exists()
    assert not (tmp_path / "registry" / "authority.json").exists()


# --- 不変条件 9: stage は model version と独立に残る ---------------------------


def test_invariant_9_a_the_stage_is_recorded_on_a_tick_without_any_model() -> None:
    """**model が無い tick にも stage を残す**（受入基準「独立して #82 へ記録できる」）。"""
    gate = ControllerGate(
        policy(authority="full"),
        expected_model_version="thermal-v1",
        authority=StaticAuthorityStage(AuthorityStage.EXPANDED),
    )

    selection = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=0).model_copy(
            update={"proposal": None, "received_at_mono_ms": None, "assessment": None}
        ),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    trace = selection.trace_metadata()
    assert selection.model_gate is None
    assert trace["authority_stage"] == AuthorityStage.EXPANDED.value
    assert "model_version" not in trace


def test_invariant_9_b_the_recorded_stage_does_not_follow_the_model_version() -> None:
    """**model version が変わっても stage は動かない。**"""
    settings = policy(authority="full", recovery_hold_ms=1)
    stages = []
    for version in ("thermal-v1", "thermal-v1"):
        gate = ControllerGate(
            settings,
            expected_model_version=version,
            authority=StaticAuthorityStage(AuthorityStage.LIMITED),
        )
        gate.select(
            now_mono_ms=0,
            fallback=fallback_proposal(0.4),
            learned=healthy_status(received=0, proposal=learned_proposal(version=version)),
            operating_mode=OperatingMode.AUTO,
            safety_state=SafetyState.NORMAL,
        )
        selection = gate.select(
            now_mono_ms=1,
            fallback=fallback_proposal(0.4),
            learned=healthy_status(received=1, proposal=learned_proposal(version=version)),
            operating_mode=OperatingMode.AUTO,
            safety_state=SafetyState.NORMAL,
        )
        assert selection.model_gate is not None
        stages.append(selection.authority_stage)

    assert stages == [AuthorityStage.LIMITED, AuthorityStage.LIMITED]


def test_invariant_9_c_a_selection_cannot_claim_two_different_stages() -> None:
    """**2つの欄が別の stage を名乗れない。** 読む側がどちらを信じるか決められなくなる。"""
    gate = ControllerGate(
        policy(authority="full", recovery_hold_ms=1),
        expected_model_version="thermal-v1",
        authority=StaticAuthorityStage(AuthorityStage.LIMITED),
    )
    gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    selection = gate.select(
        now_mono_ms=1,
        fallback=fallback_proposal(0.4),
        learned=healthy_status(received=1),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    assert selection.model_gate is not None

    with pytest.raises(ValidationError, match="authority stage を一致させる"):
        ControllerSelection(
            proposal=selection.proposal,
            authority_stage=AuthorityStage.FULL,
            transitioned=False,
            model_gate=selection.model_gate,
        )


def test_invariant_9_d_the_runtime_trace_carries_the_stage_but_no_model_version(
    tmp_path: Path,
) -> None:
    """runtime の trace は stage・上限・直近の変更を残し、**model を名指さない**。"""
    control = runtime(tmp_path, stage=AuthorityStage.LIMITED, ceiling="full")

    trace = control.trace_metadata()

    assert trace["authority_stage"] == AuthorityStage.LIMITED.value
    assert trace["authority_config_ceiling"] == AuthorityStage.FULL.value
    assert trace["authority_revision"] == 1
    last = trace["authority_last_change"]
    assert isinstance(last, dict)
    assert last["actor"] == APPROVER
    assert last["occurred_at_ms"] == NOW_MS
    serialized = json.dumps(trace, ensure_ascii=False)
    assert "model" not in serialized, "stage の記録は model version を持ち込まない"


# --- 不変条件 10: Critical Safety は全 stage で同一 ----------------------------


def test_invariant_10_a_critical_safety_never_reads_the_authority_stage() -> None:
    """**Safety は stage を読まない。** 読めば「stage ごとの安全」が生まれる。"""
    for path in sorted(Path("src/coldaisle/control/safety").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "AuthorityStage" not in source, path
        assert "authority_stage" not in source, path
        assert "AuthorityStore" not in source, path


def test_invariant_10_b_the_safety_floor_is_identical_at_every_stage() -> None:
    """**どの stage でも同じ floor・同じ最終裁定になる。**

    stage は `requested` の幅だけを変える。Learned MPC が floor を下回る値を出しても、
    4つの stage すべてで同じ effective demand に収束する（0028 §2.4、AGENTS.md ルール3）。
    """
    settings = policy(authority="full", recovery_hold_ms=1)
    floors: dict[AuthorityStage, tuple[float, ...]] = {}
    effective: dict[AuthorityStage, tuple[float, ...]] = {}
    requested: dict[AuthorityStage, float] = {}
    for stage in AuthorityStage:
        gate = ControllerGate(
            settings,
            expected_model_version="thermal-v1",
            authority=StaticAuthorityStage(stage),
        )
        for tick in (0, 1):
            selection = gate.select(
                now_mono_ms=tick,
                fallback=fallback_proposal(0.2),
                learned=healthy_status(received=tick, proposal=learned_proposal(0.1)),
                operating_mode=OperatingMode.AUTO,
                safety_state=SafetyState.NORMAL,
            )

        config = safety_config(uniform_zone_min=0.6)
        safety = critical_safety(config)
        composer = DemandComposer(config)
        startup = safety.evaluate(safety_snapshot(tick=1, mono=0), mode=OperatingMode.AUTO)
        composer.compose(
            requested=selection.proposal.requested,
            guard=empty_guard(),
            safety=startup,
            mode=OperatingMode.AUTO,
        )
        normal = safety.evaluate(safety_snapshot(tick=2, mono=10_000), mode=OperatingMode.AUTO)
        composed = composer.compose(
            requested=selection.proposal.requested,
            guard=empty_guard(),
            safety=normal,
            mode=OperatingMode.AUTO,
        )

        assert normal.state is SafetyState.NORMAL
        requested[stage] = selection.proposal.requested.front.demand
        floors[stage] = tuple(
            zone.safety_floor for zone in (composed.front, composed.rear, composed.top)
        )
        effective[stage] = tuple(
            zone.effective for zone in (composed.front, composed.rear, composed.top)
        )

    assert len(set(floors.values())) == 1, floors
    assert len(set(effective.values())) == 1, effective
    assert effective[AuthorityStage.FULL] == (0.6, 0.6, 0.8)
    # stage は requested の幅を変える。変わらないなら、この試験は何も言っていない。
    assert requested[AuthorityStage.SHADOW] != requested[AuthorityStage.FULL]


# --- 初回昇格を実際に歩けること -------------------------------------------------


def test_the_first_promotion_can_actually_be_walked(tmp_path: Path, trained) -> None:  # noqa: F811
    """**SHADOW → LIMITED を、いまの設定で集めた証拠で最後まで歩ける**（codex #4056864031）。

    初日の形はこうである。journal はまだ無い（＝SHADOW）、設定の上限は LIMITED、
    Registry の artifact は SHADOW 互換だけ。どこかが「上限」と「実効 stage」を
    取り違えていると、**worker を作れず、証拠を1件も集められず、最初の昇格が永久に来ない。**

    1. SHADOW 互換の束縛で worker を作れる
    2. Gate は SHADOW なので実 Fan を Fallback に置く（提案は counterfactual）
    3. その区間の証拠で LIMITED へ上げられる
    4. **昇格前に作った提案は、そのままでは LIMITED で使えない**（codex #4056903566）
    5. LIMITED を覆う束縛で worker を作り直すと、帯の中で Learned MPC を採る
    """
    base, profile, attestation = trained
    settings = mpc_policy(authority="limited")
    authority = store(tmp_path)
    control = AuthorityRuntime(authority, settings)

    assert control.configured_ceiling is AuthorityStage.LIMITED
    assert control.current_stage() is AuthorityStage.SHADOW, "journal が無ければ Baseline"

    # 1. 上限が LIMITED でも、実効 stage が SHADOW なら SHADOW 互換の artifact で動かせる。
    shadow_binding = MpcModelBinding.for_control(
        PlanningModel(base),
        attestation=attestation,
        authority_stage=AuthorityStage.SHADOW,
        expected_model_version=attestation.version,
    )
    shadow_worker = LearnedMpcController(
        shadow_binding,
        settings,
        mpc_safety(),
        assessor=ConfidenceAssessor(profile, settings.model_confidence),
        monotonic_ms=ScriptedClock(0),
        authority=control,
    )
    shadow_result = propose(shadow_worker)
    assert shadow_result.proposal is not None, "Shadow 期間の提案が作れないと証拠が貯まらない"

    # 2. Gate は SHADOW。実 Fan は Fallback が作る。
    gate = ControllerGate(settings, expected_model_version=attestation.version, authority=control)
    shadow_tick = gate.select(
        now_mono_ms=0,
        fallback=fallback_proposal(0.4),
        learned=shadow_result.to_status(received_at_mono_ms=0),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    assert shadow_tick.authority_stage is AuthorityStage.SHADOW
    assert shadow_tick.active_controller is ControllerKind.FALLBACK

    # 3. その区間の証拠で昇格する。証拠はいまの設定・いまの artifact のものである。
    config = control_config(tmp_path, ceiling="limited")
    registry, production_sha = production_registry(tmp_path, version="9.0.0", name="registry-e2e")
    document = report_document(
        artifacts=(production_sha,),
        policy_sha=config.sources.policy.sha256,
        safety_sha=config.sources.safety.sha256,
    )
    journal = raise_stage(
        authority,
        approval=approval_for(
            document,
            evidence=evidence_for(
                document,
                artifact=production_sha,
                policy_sha=config.sources.policy.sha256,
                safety_sha=config.sources.safety.sha256,
            ),
        ),
        document=document,
        config=config,
        registry=registry,
    )
    assert journal.stage is AuthorityStage.LIMITED
    control.reload()
    assert control.current_stage() is AuthorityStage.LIMITED

    # 4. **昇格前の提案は、そのまま LIMITED では使えない。** 束縛が覆っていない。
    stale_tick = gate.select(
        now_mono_ms=1_000,
        fallback=fallback_proposal(0.4),
        learned=shadow_result.to_status(received_at_mono_ms=1_000),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    assert stale_tick.active_controller is ControllerKind.FALLBACK
    assert stale_tick.fallback_reason is not None
    assert stale_tick.fallback_reason.code == FallbackCause.AUTHORITY_NOT_COVERED.value

    # 5. LIMITED を覆う束縛で作り直せば、帯の中で Learned MPC を採る。
    limited_worker = LearnedMpcController(
        MpcModelBinding.for_control(
            PlanningModel(base),
            attestation=attestation,
            authority_stage=AuthorityStage.LIMITED,
            expected_model_version=attestation.version,
        ),
        settings,
        mpc_safety(),
        assessor=ConfidenceAssessor(profile, settings.model_confidence),
        monotonic_ms=ScriptedClock(0),
        authority=control,
    )
    limited_result = propose(limited_worker)
    assert limited_result.proposal is not None
    gate.select(
        now_mono_ms=2_000,
        fallback=fallback_proposal(0.4),
        learned=limited_result.to_status(received_at_mono_ms=2_000),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )
    limited_tick = gate.select(
        now_mono_ms=3_000,
        fallback=fallback_proposal(0.4),
        learned=limited_result.to_status(received_at_mono_ms=3_000),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )

    assert limited_tick.authority_stage is AuthorityStage.LIMITED
    assert limited_tick.active_controller is ControllerKind.LEARNED_MPC
