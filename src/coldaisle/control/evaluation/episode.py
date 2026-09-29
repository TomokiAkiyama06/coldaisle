"""RL episode の比較を Offline Evaluation の枠で出す報告（#105 / 決定記録 0074 §2.2）。

**`EvaluationReport` には入れない。** `EvaluationReport` の bytes は Learned MPC の authority
昇格の証拠として #92 が読む（決定記録 0057 §2.4）。episode は記録の再生か近似 simulator の
出力であって、**適用された構成の運転実績ではない**。別の型（`PolicyEpisodeReport`）にすることで、
#92 の `raise_stage()` はこの bytes を `EvaluationReport` として復号できずに拒む
（`extra="forbid"`・版の Literal）。名前空間は第3の `episode:` で、読む側は supervisor policy の
validated 化（`validate_supervisor_policy()`。0074 §2.3）だけである。

**呼び出し側が数字や hash を文字列で渡す口を作らない。** 報告の欄はすべて、
`PolicyComparison`・検証済みの `RlPolicyConfig`・Baseline の Rule policy・RL arm ごとの
`certify()` を通した artifact から導く。RL arm は**表と action の証拠**で artifact へ束縛し、
版の文字列どうしの一致では束縛しない（学習の arm の版 `<model_id>-<候補識別子>` と
artifact の意味論的な版は別の名前空間である）。

**置かないもの**: 「運転での温度実績」に見える欄（温度 percentile・ΔT・Air Balance・RPM）。
0054 §2.2 の帰属規則を第3の名前空間にも掛ける。

この module は `control.rl` / `control.supervisor` の型を**読むだけ**で import する。
`control.rl` は `control.evaluation` を import しない（向きは一方向）。循環を避けるため、
`coldaisle.control.evaluation` package の `__init__` からは読み込まない
（`coldaisle.control.evaluation.episode` を直接 import する）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, model_validator

from coldaisle.control.evaluation.model import (
    GATE_STAGE_ORDER,
    CountedReason,
    GateCondition,
    GateOutcome,
    GateResult,
    GateStage,
)
from coldaisle.control.model.thermal import canonical_sha256
from coldaisle.control.rl.dynamics import DynamicsProvenance, TrainingMode
from coldaisle.control.rl.episode import (
    EpisodeConfigDigests,
    EpisodeResult,
    EpisodeSafety,
    PolicyArm,
    PolicyComparison,
    SafetyModel,
    config_digest,
)
from coldaisle.control.rl.training import (
    candidate_improved,
    first_unreplayed_step,
    short_episodes,
    truncated_episodes,
)
from coldaisle.control.schema import Sha256Hex, SupervisorPolicyIdentity, SupervisorPolicyKind
from coldaisle.control.supervisor.artifact import (
    CertifiedPolicyArtifact,
    RegimeActionEntry,
    RegimeTablePayload,
    certified_identity,
)
from coldaisle.control.supervisor.policy import SupervisorPolicy
from coldaisle.control.supervisor.policy_config import RlPolicyConfig
from coldaisle.control.supervisor.rule_identity import (
    RulePolicyIdentity,
    RulePolicyTable,
    rule_policy_table,
)

POLICY_EPISODE_REPORT_SCHEMA_VERSION: Literal[1] = 1
"""`PolicyEpisodeReport` の形の版。**欄の意味を変えたら上げる。**"""

EPISODE_ARM_NAMESPACE = "episode"
"""arm の第3の名前空間（決定記録 0074 §2.2）。`applied:` / `counterfactual:` と混ぜない。"""

EPISODE_EVALUATION_REF_PREFIX = "supervisor-episode:"
"""`evaluation_ref()` が返す参照の接頭辞（決定記録 0074 §2.3）。"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class PolicyEpisodeReportError(ValueError):
    """入力から報告を作れない（Baseline が一意でない、artifact が arm を再現しない、など）。

    **どれかを選んで比べることはしない。** 作れない入力には報告を出さない（fail closed）。
    """


def episode_arm_key(policy: SupervisorPolicyKind, policy_version: str) -> str:
    """`episode:<policy>+<policy_version>`。**ラベルであって識別ではない。**"""
    return f"{EPISODE_ARM_NAMESPACE}:{policy.value}+{policy_version}"


class EpisodeWorstCase(_Frozen):
    """arm の中で安全側がいちばん悪かった episode。**平均に埋もれさせない。**"""

    episode_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=120)
    safety: EpisodeSafety


class EpisodeArmSafety(_Frozen):
    """arm の安全側の数。合計と **worst-case episode** を並べる。reward と足し合わせない。"""

    ceiling_exceedances: int = Field(ge=0)
    floor_shortfalls: int = Field(ge=0)
    invalid_actions: int = Field(ge=0)
    minimum_margin_c: float | None = Field(default=None, allow_inf_nan=False)
    """全 episode の最小 margin。どの episode にも margin が無ければ `None`。"""
    worst_case: EpisodeWorstCase


class EpisodeArmCoverage(_Frozen):
    """arm の coverage。**採点できなかった step を黙って落とさない**（0054 §2.3）。"""

    steps: int = Field(ge=0)
    supported_steps: int = Field(ge=0)
    supported_fraction: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    """step が1つも無ければ `None`（0 とは区別する）。"""
    unsupported: tuple[CountedReason, ...] = ()
    """理由別の内訳（`code` の昇順）。"""
    unusable_episodes: int = Field(ge=0)
    """`usable_for_comparison` でない episode の数。"""

    @model_validator(mode="after")
    def _counts_add_up(self) -> Self:
        if self.supported_steps + sum(item.count for item in self.unsupported) != self.steps:
            raise ValueError("採点できた step と理由別の内訳の合計が全体と一致しない")
        codes = tuple(item.code for item in self.unsupported)
        if codes != tuple(sorted(set(codes))):
            raise ValueError("理由別の内訳は code の重複なし昇順にする")
        return self


class PolicyEpisodeArm(_Frozen):
    """episode 比較の1 arm。**識別は Rule policy / certify 済み artifact からだけ取る。**"""

    arm_key: str = Field(min_length=1, max_length=200)
    policy: SupervisorPolicyKind
    policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    """比較が持つラベルをそのまま写したもの。**識別には使わない。**"""
    rule_policy_identity: RulePolicyIdentity | None = None
    """Baseline（Rule）の完全な識別（版と**全 regime の表の digest**）。"""
    rl_policy_identity: SupervisorPolicyIdentity | None = None
    """RL arm の完全な識別（`certified_identity()`）。"""
    rl_table_sha256: Sha256Hex | None = None
    """RL arm を束縛した表の digest（artifact の `manifest.payload_sha256`）。"""
    safety: EpisodeArmSafety
    coverage: EpisodeArmCoverage
    mean_matched_reward: float | None = Field(default=None, allow_inf_nan=False)
    """共通の長さ（報告の `matched_steps`）で揃えた割引 reward の平均。出せなければ `None`。"""
    short_episodes: tuple[str, ...] = ()
    """Baseline と同じ区間を比べられない episode（短い・打ち切り。0061 §2.7）。Baseline は空。"""
    promotable_episodes: int = Field(ge=0)
    total_episodes: int = Field(ge=1)

    @model_validator(mode="after")
    def _identity_matches_the_kind(self) -> Self:
        if self.arm_key != episode_arm_key(self.policy, self.policy_version):
            raise ValueError("arm_key が policy と版から作った鍵と一致しない")
        if self.promotable_episodes > self.total_episodes:
            raise ValueError("promotable な episode 数が総数を超えている")
        if self.short_episodes != tuple(sorted(set(self.short_episodes))):
            raise ValueError("short_episodes は重複なし昇順にする")
        if self.policy is SupervisorPolicyKind.RULE:
            if self.rule_policy_identity is None:
                raise ValueError("Rule arm には RulePolicyIdentity が要る")
            if self.rl_policy_identity is not None or self.rl_table_sha256 is not None:
                raise ValueError("Rule arm に RL の識別を持たせない")
            if self.rule_policy_identity.version != self.policy_version:
                raise ValueError("Rule arm の版が識別の版と一致しない")
            return self
        if self.rule_policy_identity is not None:
            raise ValueError("RL arm に Rule の識別を持たせない")
        if self.rl_policy_identity is None or self.rl_table_sha256 is None:
            raise ValueError("RL arm には certify 済み artifact の識別と表の digest が要る")
        return self


class PolicyEpisodeReport(_Frozen):
    """RL episode の比較を #91 の枠（名前空間・3段 gate・digest）で出した報告。

    **同じ入力からは同じ bytes になる。** 生成時刻を持たない（0054 §2.7）。
    gate は**助言**であり、昇格の判断ではない。validated 化は `validate_supervisor_policy()`
    だけが、この報告を元の比較から作り直して照合したうえで行う（決定記録 0074 §2.3）。
    """

    schema_version: Literal[1]
    """報告の形の版。**入力では必須で、既定値を持たない**（`EvaluationReport` と同じ理由）。"""
    comparison_sha256: Sha256Hex
    """入力の `PolicyComparison` の canonical digest。**報告をその比較へ束縛する。**"""
    conditions_sha256: Sha256Hex
    """`PolicyComparison.conditions_sha256`。"""
    config_digests: EpisodeConfigDigests
    """episode の `config_digests`（arm 間・episode 間で揃っていることを確かめたもの）。"""
    rl_policy_config_sha256: Sha256Hex
    """入力の検証済み `RlPolicyConfig` の digest（`config_digest()`）。"""
    minimum_reward_improvement: float = Field(allow_inf_nan=False)
    """cost の段の閾値。入力の検証済み `RlPolicyConfig` から取る。

    `evaluation.yaml` に写さない（0054 §2.6 / 0061 §2.5）。
    """
    training_mode: TrainingMode
    dynamics_provenance: DynamicsProvenance
    safety_model: SafetyModel
    reward_version: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=64)
    applied_demand_tolerance: float | None = Field(default=None, ge=0.0, lt=1.0)
    learned_controller_available: bool
    """比較から導く（欄として受け取らない）。`False` なら policy の差を測っていない。"""
    matched_steps: dict[str, NonNegativeInt]
    """episode ごとの、**すべての arm が採点できた step 数**（最小）。reward を揃えた長さ。"""
    arms: tuple[PolicyEpisodeArm, ...] = Field(min_length=2, max_length=8)
    """`arms[0]` は Baseline（Rule）で固定。`arms[1:]` はすべて RL。"""
    gates: tuple[GateResult, ...] = Field(min_length=1, max_length=7)
    """RL arm ごとの gate（`arms[1:]` と同じ並び）。Baseline には gate を出さない。"""

    @model_validator(mode="after")
    def _baseline_is_first_and_every_rl_arm_is_gated(self) -> Self:
        baseline, *rl_arms = self.arms
        if baseline.policy is not SupervisorPolicyKind.RULE:
            raise ValueError("arms[0] は Baseline の Rule arm にする")
        if any(arm.policy is not SupervisorPolicyKind.RL for arm in rl_arms):
            raise ValueError("Baseline 以外の arm はすべて RL にする")
        keys = tuple(arm.arm_key for arm in self.arms)
        if len(set(keys)) != len(keys):
            raise ValueError("同じ arm を2つ入れない")
        tables = tuple(arm.rl_table_sha256 for arm in rl_arms)
        if len(set(tables)) != len(tables):
            raise ValueError("異なる RL arm に同じ artifact の表を当てない")
        if tuple(gate.arm_key for gate in self.gates) != tuple(arm.arm_key for arm in rl_arms):
            raise ValueError("gate は RL arm ごとに1つ、arm と同じ並びで出す")
        return self

    def arm(self, policy_version: str) -> PolicyEpisodeArm:
        """RL arm を比較の版ラベルで引く。無ければ `KeyError`。"""
        for item in self.arms[1:]:
            if item.policy_version == policy_version:
                return item
        raise KeyError(policy_version)

    def gate(self, policy_version: str) -> GateResult:
        """RL arm の gate を比較の版ラベルで引く。無ければ `KeyError`。"""
        key = self.arm(policy_version).arm_key
        for item in self.gates:
            if item.arm_key == key:
                return item
        raise KeyError(policy_version)  # pragma: no cover - validator が1対1を保証する

    def digest(self) -> str:
        """この報告そのものを表す SHA-256。"""
        return canonical_sha256(self)

    def evaluation_ref(self, policy_version: str) -> str:
        """名指した RL arm の `offline_evaluation_ref`（`supervisor-episode:<digest>`）。

        **gate が `pass` でなければ参照を出さない**（0061 §2.6 の `evaluation_ref()` と同じ
        fail closed）。`blocked` の評価を通った形で Registry の監査に残さない。
        """
        gate = self.gate(policy_version)
        if gate.outcome is not GateOutcome.PASS:
            stage = None if gate.blocking_stage is None else gate.blocking_stage.value
            raise ValueError(
                f"gate が pass でない arm の評価参照は出さない（arm={gate.arm_key}; stage={stage}）"
            )
        return f"{EPISODE_EVALUATION_REF_PREFIX}{self.digest()}"


# ---------------------------------------------------------------- 構築


def _rule_payload(table: RulePolicyTable) -> RegimeTablePayload:
    """Rule policy の表を、action の照合に使う `RegimeTablePayload` の形へ写す。"""
    return RegimeTablePayload(
        entries=tuple(
            RegimeActionEntry(
                regime=entry.regime,
                strategy=entry.strategy,
                weights=entry.weights,
                target_band=entry.target_band,
            )
            for entry in table.entries
        )
    )


def _single[T](values: set[T], name: str) -> T:
    """arm 間・episode 間で1つに揃っている値を返す。**揃っていなければ報告を作らない。**"""
    if len(values) != 1:
        raise PolicyEpisodeReportError(f"episode の {name} が arm 間・episode 間で揃っていない")
    return next(iter(values))


def _worst_case_key(episode: EpisodeResult) -> tuple[int, int, float, str]:
    """小さいほど悪い。margin を観測できなかった episode は**悪い側**に倒す（fail closed）。"""
    margin = episode.safety.minimum_margin_c
    return (
        -episode.violations,
        -episode.safety.invalid_actions,
        -float("inf") if margin is None else margin,
        episode.episode_id,
    )


def _arm_safety(arm: PolicyArm) -> EpisodeArmSafety:
    margins = [
        episode.safety.minimum_margin_c
        for episode in arm.episodes
        if episode.safety.minimum_margin_c is not None
    ]
    worst = min(arm.episodes, key=_worst_case_key)
    return EpisodeArmSafety(
        ceiling_exceedances=sum(episode.safety.ceiling_exceedances for episode in arm.episodes),
        floor_shortfalls=sum(episode.safety.floor_shortfalls for episode in arm.episodes),
        invalid_actions=arm.invalid_actions,
        minimum_margin_c=min(margins) if margins else None,
        worst_case=EpisodeWorstCase(episode_id=worst.episode_id, safety=worst.safety),
    )


def _arm_coverage(arm: PolicyArm) -> EpisodeArmCoverage:
    steps = sum(episode.coverage.steps for episode in arm.episodes)
    supported = sum(episode.coverage.supported_steps for episode in arm.episodes)
    unsupported: dict[str, int] = {}
    for episode in arm.episodes:
        for code, count in episode.coverage.unsupported.items():
            unsupported[code] = unsupported.get(code, 0) + count
    return EpisodeArmCoverage(
        steps=steps,
        supported_steps=supported,
        supported_fraction=None if steps == 0 else supported / steps,
        unsupported=tuple(
            CountedReason(code=code, count=count)
            for code, count in sorted(unsupported.items())
            if count > 0
        ),
        unusable_episodes=sum(1 for episode in arm.episodes if not episode.usable_for_comparison),
    )


def _blocked(stage: GateStage, name: str, code: str, count: int) -> GateCondition:
    return GateCondition(
        stage=stage,
        name=name,
        outcome=GateOutcome.BLOCKED,
        reason=CountedReason(code=code, count=max(count, 1)),
    )


def _at_most(
    stage: GateStage, name: str, observed: float, limit: float, code: str
) -> GateCondition:
    passed = observed <= limit
    return GateCondition(
        stage=stage,
        name=name,
        outcome=GateOutcome.PASS if passed else GateOutcome.BLOCKED,
        limit=limit,
        observed=observed,
        reason=None if passed else CountedReason(code=code, count=max(int(observed), 1)),
    )


def _gate(
    comparison: PolicyComparison,
    arm: PolicyArm,
    baseline: PolicyArm,
    *,
    short: tuple[str, ...],
    minimum_reward_improvement: float,
) -> GateResult:
    """0054 §2.4 と同じ3段・辞書式の gate。**上の段が落ちたら下の段では覆らない。**"""
    safety = (
        # worst-case episode で判定する（合計が 0 であることと同じだが、平均では見ない）。
        _at_most(
            GateStage.SAFETY,
            "safety_violations",
            float(max(episode.violations for episode in arm.episodes)),
            0.0,
            "safety_violation",
        ),
        _at_most(
            GateStage.SAFETY,
            "invalid_actions",
            float(max(episode.safety.invalid_actions for episode in arm.episodes)),
            0.0,
            "invalid_action",
        ),
    )
    comparable = comparison.comparable_episode_ids
    if comparison.learned_controller_available:
        learned = GateCondition(
            stage=GateStage.EVIDENCE,
            name="learned_controller_available",
            outcome=GateOutcome.PASS,
            limit=1.0,
            observed=1.0,
        )
    else:
        # **いまはここで必ず止まる**（決定記録 0058 §3 / 0061 §3）。
        learned = _blocked(
            GateStage.EVIDENCE,
            "learned_controller_available",
            "learned_controller_unavailable",
            len(arm.episodes),
        )
    not_promotable = sum(1 for key in comparable if not arm.episode(key).promotable)
    promotable_fraction = (len(comparable) - not_promotable) / len(comparable)
    evidence = (
        learned,
        GateCondition(
            stage=GateStage.EVIDENCE,
            name="promotable_fraction",
            outcome=GateOutcome.PASS if not_promotable == 0 else GateOutcome.BLOCKED,
            limit=1.0,
            observed=promotable_fraction,
            reason=(
                None
                if not_promotable == 0
                else CountedReason(code="episode_not_promotable", count=not_promotable)
            ),
        ),
        # coverage の下限は環境が episode ごとに判定済み（`usable_for_comparison`）。
        # 別の `rl-training.yaml` で判定し直さない（0074 §2.2）。
        _at_most(
            GateStage.EVIDENCE,
            "unusable_episodes",
            float(sum(1 for episode in arm.episodes if not episode.usable_for_comparison)),
            0.0,
            "coverage_insufficient",
        ),
        _at_most(GateStage.EVIDENCE, "short_episodes", float(len(short)), 0.0, "short_episode"),
    )
    mean = comparison.mean_matched_reward(arm)
    baseline_mean = comparison.mean_matched_reward(baseline)
    if short:
        # 先に終わった arm は共通の長さの reward を持たない（0061 §2.7。改善扱いにしない）。
        cost = _blocked(GateStage.COST, "reward_improvement", "short_episode", len(short))
    elif mean is None or baseline_mean is None:
        cost = _blocked(GateStage.COST, "reward_improvement", "reward_unavailable", 1)
    else:
        improved = candidate_improved(
            safety_violations=arm.safety_violations,
            invalid_actions=arm.invalid_actions,
            mean_reward=mean,
            baseline_safety_violations=baseline.safety_violations,
            baseline_invalid_actions=baseline.invalid_actions,
            baseline_mean_reward=baseline_mean,
            minimum_reward_improvement=minimum_reward_improvement,
        )
        cost = GateCondition(
            stage=GateStage.COST,
            name="reward_improvement",
            outcome=GateOutcome.PASS if improved else GateOutcome.BLOCKED,
            limit=minimum_reward_improvement,
            observed=mean - baseline_mean,
            reason=None if improved else CountedReason(code="not_improved", count=1),
        )
    conditions = (*safety, *evidence, cost)
    order = {stage: index for index, stage in enumerate(GATE_STAGE_ORDER)}
    failed = [condition for condition in conditions if condition.outcome is GateOutcome.BLOCKED]
    return GateResult(
        arm_key=episode_arm_key(arm.policy, arm.policy_version),
        outcome=GateOutcome.BLOCKED if failed else GateOutcome.PASS,
        blocking_stage=(
            None if not failed else min(failed, key=lambda item: order[item.stage]).stage
        ),
        conditions=conditions,
    )


def _arm_report(
    comparison: PolicyComparison,
    arm: PolicyArm,
    *,
    short: tuple[str, ...],
    rule_identity: RulePolicyIdentity | None = None,
    certified: CertifiedPolicyArtifact | None = None,
) -> PolicyEpisodeArm:
    return PolicyEpisodeArm(
        arm_key=episode_arm_key(arm.policy, arm.policy_version),
        policy=arm.policy,
        policy_version=arm.policy_version,
        rule_policy_identity=rule_identity,
        rl_policy_identity=None if certified is None else certified_identity(certified),
        rl_table_sha256=None if certified is None else certified.artifact.manifest.payload_sha256,
        safety=_arm_safety(arm),
        coverage=_arm_coverage(arm),
        mean_matched_reward=comparison.mean_matched_reward(arm),
        short_episodes=short,
        promotable_episodes=sum(1 for episode in arm.episodes if episode.promotable),
        total_episodes=len(arm.episodes),
    )


def _bind_baseline(
    comparison: PolicyComparison, rule_policy: SupervisorPolicy
) -> RulePolicyIdentity:
    """Baseline を**ちょうど1つの Rule arm（`arms[0]`）**として、渡した Rule policy へ束縛する。"""
    rule_arms = [arm for arm in comparison.arms if arm.policy is SupervisorPolicyKind.RULE]
    if len(rule_arms) != 1:
        # Baseline を一意に決められない比較は、どれかを選んで比べない。
        raise PolicyEpisodeReportError(
            f"Baseline の Rule arm がちょうど1つではない（{len(rule_arms)}）"
        )
    baseline = comparison.arms[0]
    if baseline.policy is not SupervisorPolicyKind.RULE:
        raise PolicyEpisodeReportError("arms[0] が Baseline の Rule arm ではない")
    if rule_policy.kind is not SupervisorPolicyKind.RULE:
        raise PolicyEpisodeReportError("Baseline には RulePolicy を渡す")
    try:
        table = rule_policy_table(rule_policy)
    except (TypeError, ValueError) as error:
        raise PolicyEpisodeReportError(
            f"Baseline の Rule policy の表を作れない: {error}"
        ) from error
    identity = table.identity
    if identity.version != baseline.policy_version:
        raise PolicyEpisodeReportError(
            "Baseline arm の版が渡した Rule policy の版と一致しない"
            f"（arm={baseline.policy_version}; rule={identity.version}）"
        )
    mismatch = first_unreplayed_step(_rule_payload(table), baseline)
    if mismatch is not None:
        # **版だけでは Baseline を特定できない。** 同じ版の別の表の arm を通さない。
        raise PolicyEpisodeReportError(
            f"Baseline arm の action を、渡した Rule policy の表が再現しない（{mismatch}）"
        )
    return identity


def _bind_rl_arms(
    comparison: PolicyComparison, certified: Mapping[str, CertifiedPolicyArtifact]
) -> dict[str, CertifiedPolicyArtifact]:
    """RL arm を**表と action の証拠**で certify 済み artifact へ束縛する（版の一致ではなく）。"""
    rl_arms = comparison.arms[1:]
    labels = {arm.policy_version for arm in rl_arms}
    if set(certified) != labels:
        missing = sorted(labels - set(certified))
        extra = sorted(set(certified) - labels)
        raise PolicyEpisodeReportError(
            f"RL arm と artifact の対応が1対1でない（欠け={missing}; 余り={extra}）"
        )
    tables: dict[str, str] = {}
    bound: dict[str, CertifiedPolicyArtifact] = {}
    for arm in rl_arms:
        item = certified[arm.policy_version]
        if not isinstance(item, CertifiedPolicyArtifact):
            raise PolicyEpisodeReportError(
                "RL arm には SupervisorPolicyTrainer.certify() を通した artifact を渡す"
            )
        artifact = item.artifact
        digest = artifact.manifest.payload_sha256
        if digest in tables:
            raise PolicyEpisodeReportError(
                "異なる RL arm に同じ artifact を当てない"
                f"（{tables[digest]} と {arm.policy_version}）"
            )
        mismatch = first_unreplayed_step(artifact.payload, arm)
        if mismatch is not None:
            raise PolicyEpisodeReportError(
                "RL arm の action を artifact の表が再現しない"
                f"（arm={arm.policy_version}; {mismatch}）"
            )
        tables[digest] = arm.policy_version
        bound[arm.policy_version] = item
    return bound


def build_policy_episode_report(
    comparison: PolicyComparison,
    *,
    policy_config: RlPolicyConfig,
    rule_policy: SupervisorPolicy,
    certified: Mapping[str, CertifiedPolicyArtifact],
) -> PolicyEpisodeReport:
    """`PolicyComparison` 1つから `PolicyEpisodeReport` を作る（決定記録 0074 §2.2）。

    `certified` は比較の**すべての RL arm** について、arm の `policy_version` をキーにした
    certify 済み artifact の対応である。キーは**対応を引くためだけ**に使い、識別は
    `certified_identity()` から、束縛は表の digest と action の再現から取る。
    `validate_supervisor_policy()` も同じこの関数で作り直して bytes を照合する。
    """
    if not isinstance(comparison, PolicyComparison):
        raise TypeError("比較は PolicyComparison で渡す")
    if not isinstance(policy_config, RlPolicyConfig):
        raise TypeError("rl-policy.yaml は検証済みの RlPolicyConfig で渡す")
    rule_identity = _bind_baseline(comparison, rule_policy)
    if any(arm.policy is not SupervisorPolicyKind.RL for arm in comparison.arms[1:]):
        raise PolicyEpisodeReportError("Baseline 以外の arm はすべて RL にする")
    bound = _bind_rl_arms(comparison, certified)

    episodes = [episode for arm in comparison.arms for episode in arm.episodes]
    digests = _single({episode.config_digests.model_dump_json() for episode in episodes}, "設定")
    tolerance = _single({episode.applied_demand_tolerance for episode in episodes}, "許容幅")
    baseline = comparison.arms[0]
    minimum = policy_config.search.minimum_reward_improvement.value
    arms = [_arm_report(comparison, baseline, short=(), rule_identity=rule_identity)]
    gates: list[GateResult] = []
    for arm in comparison.arms[1:]:
        short = tuple(
            sorted(set(short_episodes(arm, baseline)) | set(truncated_episodes(arm, baseline)))
        )
        arms.append(_arm_report(comparison, arm, short=short, certified=bound[arm.policy_version]))
        gates.append(
            _gate(comparison, arm, baseline, short=short, minimum_reward_improvement=minimum)
        )
    return PolicyEpisodeReport(
        schema_version=POLICY_EPISODE_REPORT_SCHEMA_VERSION,
        comparison_sha256=canonical_sha256(comparison),
        conditions_sha256=comparison.conditions_sha256,
        config_digests=EpisodeConfigDigests.model_validate_json(digests),
        rl_policy_config_sha256=config_digest(policy_config),
        minimum_reward_improvement=minimum,
        training_mode=_single({episode.mode for episode in episodes}, "学習 mode"),
        dynamics_provenance=_single(
            {episode.dynamics.provenance for episode in episodes}, "dynamics の出どころ"
        ),
        safety_model=_single({episode.safety_model for episode in episodes}, "safety_model"),
        reward_version=_single({episode.reward_version for episode in episodes}, "reward 版"),
        applied_demand_tolerance=tolerance,
        learned_controller_available=comparison.learned_controller_available,
        matched_steps=dict(sorted(comparison.matched_steps().items())),
        arms=tuple(arms),
        gates=tuple(gates),
    )


__all__ = [
    "EPISODE_ARM_NAMESPACE",
    "EPISODE_EVALUATION_REF_PREFIX",
    "POLICY_EPISODE_REPORT_SCHEMA_VERSION",
    "EpisodeArmCoverage",
    "EpisodeArmSafety",
    "EpisodeWorstCase",
    "PolicyEpisodeArm",
    "PolicyEpisodeReport",
    "PolicyEpisodeReportError",
    "build_policy_episode_report",
    "episode_arm_key",
]
