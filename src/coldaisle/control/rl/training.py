"""RL Supervisor policy の探索と評価の入口（#89 / 決定記録 0061 §2.2〜§2.4）。

**Fan へ届く経路を持たない。** この module は `control.hardware` / `control.safety` /
`control.reactive` / `serial` / `subprocess` を import せず、評価は #105 の
`SupervisorTrainingEnvironment` を通してだけ行う（試験で走査する）。

**壁時計を読まない。** `created_at` は呼び出し側が固定して渡す。同じ設定・同じ seed・
同じ episode 群からは同じ報告 bytes が出る（決定記録 0054 §2.7 と同じ規則）。

**探索が選べるのは Supervisor の戦略まで。** 候補は `supervisor.output_bounds` の
strategy / target band と `rl-policy.yaml` の weight 候補の組み合わせで、Demand を
表現できない（決定記録 0027 / 0028 §2.3 / 0058 §2.1）。

**いま何ができて、何ができないか**（決定記録 0058 §3 をそのまま引き継ぐ）。

- 記録済み trajectory を回して候補を並べ、報告と artifact を作る: **できる**
- 候補どうしを MPC を通して比べる: **できない。** 反実仮想能力を申告した artifact が
  1つも無いので、環境は `learned_controller_available=false` で回り、**すべての arm の
  requested が同一になる**。差が出ないのは「差が無かった」ではなく
  **「action が効いていない条件で回した」**である
- その結果として、**Baseline を上回る候補は原理的に選ばれない。** 報告の
  `promotable` は `False` のままで、出力する artifact は `SHADOW` 互換だけになる
"""

from __future__ import annotations

from collections.abc import Sequence
from random import Random
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.config import RulePolicyConfig, SupervisorOutputBounds
from coldaisle.control.model.thermal import canonical_sha256
from coldaisle.control.rl.environment import (
    EnvironmentUsageError,
    EpisodeSpec,
    SupervisorTrainingEnvironment,
)
from coldaisle.control.rl.episode import PolicyArm, PolicyComparison
from coldaisle.control.schema import (
    AuthorityStage,
    Reason,
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    WorkloadRegime,
)
from coldaisle.control.supervisor.artifact import (
    POLICY_FAMILY,
    PolicySearchHyperparameters,
    PolicyTrainingEvidence,
    RegimeActionEntry,
    RegimeTablePayload,
    SupervisorPolicyArtifact,
    SupervisorPolicyManifest,
    action_space_sha256,
)
from coldaisle.control.supervisor.policy import SupervisorInput, SupervisorPolicy
from coldaisle.control.supervisor.policy_config import RlPolicyConfig

TRAINING_REPORT_SCHEMA_VERSION: Literal[1] = 1

BASELINE_CANDIDATE_ID = "baseline"
"""Rule policy の表をそのまま RL artifact として回す候補の識別子。

**必ず並べる。** どの候補も Baseline を上回らないとき、選ばれるのはこれである。
「上回らなかったのに別の表を出す」ことを避けるための既定の受け皿でもある。
"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SupervisorPolicyTrainingError(RuntimeError):
    """探索の入力が噛み合っていない（候補が上限を超えた、範囲外の候補がある、など）。"""


class _TablePolicy:
    """候補表をそのまま提案する、評価専用の `SupervisorPolicy`。

    **Registry を通っていない。** 運転の policy slot へは渡せず、環境の中だけで使う。
    `computed_at_ms` は snapshot の時刻を使う（**壁時計を読まない**）。
    """

    kind = SupervisorPolicyKind.RL

    __slots__ = ("_table", "_version")

    def __init__(self, table: RegimeTablePayload, version: str) -> None:
        self._table = table
        self._version = version

    @property
    def version(self) -> str:
        """この候補の識別に使う版ラベル。"""
        return self._version

    @property
    def table(self) -> RegimeTablePayload:
        """提案の元になる表。"""
        return self._table

    def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
        """観測した regime に対応する戦略を返す。**Demand を含まない。**"""
        entry = self._table.entry(policy_input.workload.regime)
        snapshot = policy_input.snapshot
        return SupervisorOutput(
            snapshot_schema_version=snapshot.schema_version,
            tick_id=snapshot.tick_id,
            ts_ms=snapshot.ts_ms,
            policy=self.kind,
            version=self._version,
            regime=policy_input.workload.regime,
            regime_confidence=policy_input.workload.confidence,
            weights=entry.weights,
            strategy=entry.strategy,
            target_band=entry.target_band,
            computed_at_ms=snapshot.ts_ms,
        )


class _CandidateAction(_Frozen):
    """1 regime へ差し替える候補。**regime を持たない**（どの regime にも当てられる）。"""

    strategy: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    weights: SupervisorObjectiveWeights
    target_band: SupervisorTargetBand


class CandidateOutcome(_Frozen):
    """1候補の評価結果。**比べられなかった候補も理由付きで残す。**"""

    candidate_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=120)
    policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    comparable: bool
    rejection: Reason | None = None
    safety_violations: int = Field(ge=0)
    invalid_actions: int = Field(ge=0)
    mean_matched_reward: float | None = Field(default=None, allow_inf_nan=False)
    baseline_mean_matched_reward: float | None = Field(default=None, allow_inf_nan=False)
    improved: bool

    @model_validator(mode="after")
    def _uncomparable_candidates_never_win(self) -> Self:
        if self.comparable:
            if self.rejection is not None:
                raise ValueError("比べられた候補に却下理由を付けない")
            return self
        if self.rejection is None:
            raise ValueError("比べられなかった候補には理由を残す")
        if self.improved:
            # 比べられなかった候補が「Baseline を上回った」と読めないようにする。
            raise ValueError("比べられなかった候補を改善扱いにしない")
        if self.mean_matched_reward is not None or self.baseline_mean_matched_reward is not None:
            raise ValueError("比べられなかった候補に reward を書かない")
        return self


class SupervisorPolicyTrainingReport(_Frozen):
    """探索1回の結果。**同じ入力からは同じ bytes になる。**"""

    schema_version: Literal[1] = TRAINING_REPORT_SCHEMA_VERSION
    search_family: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    seed: int = Field(ge=0)
    candidate_order: tuple[str, ...] = Field(min_length=1)
    """seed で決めた評価の順序。**結果はこの順序に依らない**（試験で確かめる）。"""
    outcomes: tuple[CandidateOutcome, ...] = Field(min_length=1)
    baseline_policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    selected_candidate_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=120)
    improved_over_baseline: bool
    learned_controller_available: bool
    """学習中に Learned MPC を束縛できていたか（決定記録 0058 §2.1）。

    **`False` なら、この報告は policy の差を測っていない。**
    """
    comparison: PolicyComparison
    """Rule baseline と選ばれた候補の2 arm 比較。条件・母集団は arm 間で揃っている。"""
    artifact: SupervisorPolicyArtifact
    promotable: bool
    """この結果を昇格の根拠にしてよいか。**反実仮想の裏づけが無ければ立たない。**"""

    @model_validator(mode="after")
    def _selection_is_present_and_consistent(self) -> Self:
        identifiers = tuple(outcome.candidate_id for outcome in self.outcomes)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("同じ候補を2つ記録しない")
        if set(self.candidate_order) != set(identifiers):
            raise ValueError("評価順に無い候補、または記録の無い候補がある")
        if self.selected_candidate_id not in identifiers:
            raise ValueError("選ばれた候補が記録に無い")
        selected = next(
            outcome
            for outcome in self.outcomes
            if outcome.candidate_id == self.selected_candidate_id
        )
        if self.improved_over_baseline != selected.improved:
            raise ValueError("選ばれた候補の改善判定と報告が一致しない")
        evidence = self.artifact.manifest.training_evidence
        if evidence.improved_over_baseline != self.improved_over_baseline:
            raise ValueError("artifact の改善判定が報告と一致しない")
        if evidence.learned_controller_available != self.learned_controller_available:
            raise ValueError("artifact の Learned MPC の有無が報告と一致しない")
        if self.promotable:
            if not self.improved_over_baseline:
                raise ValueError("Baseline を上回らない結果を昇格の根拠にしない")
            if not self.learned_controller_available:
                # action が demand に効いていない条件の結果を根拠にしない（0058 §2.1）。
                raise ValueError("Learned MPC を束縛できていない結果を昇格の根拠にしない")
            if not evidence.counterfactual_backed:
                raise ValueError("反実仮想の裏づけの無い結果を昇格の根拠にしない")
        return self

    def digest(self) -> str:
        """この報告そのものを表す SHA-256。"""
        return canonical_sha256(self)


class SupervisorPolicyTrainer:
    """Rule baseline を起点に候補表を並べ、同じ episode 群で比べて artifact を作る。

    **環境を通さずに policy を採点する API を持たない。** reward も安全の台帳も
    `SupervisorTrainingEnvironment` が作る（決定記録 0058 §2.4 / §2.5）。
    """

    __slots__ = (
        "_bounds",
        "_config",
        "_config_sha256",
        "_environment",
        "_rule_config",
        "_rule_policy",
    )

    def __init__(
        self,
        environment: SupervisorTrainingEnvironment,
        *,
        policy_config: RlPolicyConfig,
        policy_config_sha256: str,
        bounds: SupervisorOutputBounds,
        rule_config: RulePolicyConfig,
        rule_policy: SupervisorPolicy,
    ) -> None:
        """探索の設定と Baseline を束ねる。**噛み合わなければ生成時に落とす。**"""
        if rule_policy.kind is not SupervisorPolicyKind.RULE:
            raise SupervisorPolicyTrainingError("Baseline には RulePolicy を渡す")
        if rule_policy.version != rule_config.version:
            # 表の出どころと Baseline arm の版がずれると、比較の起点が特定できなくなる。
            raise SupervisorPolicyTrainingError("RulePolicy の版が rule_policy 設定と違う")
        self._environment = environment
        self._config = policy_config
        self._config_sha256 = policy_config_sha256
        self._bounds = bounds
        self._rule_config = rule_config
        self._rule_policy = rule_policy

    # ------------------------------------------------------------------ 候補の並べ方

    def baseline_table(self) -> RegimeTablePayload:
        """Rule policy の設定をそのまま表にした起点。"""
        entries = tuple(
            RegimeActionEntry(
                regime=regime,
                strategy=self._rule_config.contexts.get(regime).strategy,
                weights=self._rule_config.contexts.get(regime).weights,
                target_band=self._rule_config.contexts.get(regime).target_band,
            )
            for regime in sorted(WorkloadRegime, key=lambda item: item.value)
        )
        return RegimeTablePayload(entries=entries)

    def candidates(self) -> tuple[tuple[str, RegimeTablePayload], ...]:
        """評価する候補を `(識別子, 表)` の決定論的な並びで返す。

        `per_regime_coordinate_v1` は、Baseline の表の **regime を1つだけ**候補 action へ
        差し替えた表をすべて並べる。Baseline と同じになる差し替えは並べない
        （同じ表を2度評価しても何も分からない）。

        **上限を超えたら切り詰めず落とす。** 黙って切ると、報告に出ない候補が生まれ、
        「全候補を比べた」と読めてしまう。
        """
        baseline = self.baseline_table()
        pool = self._action_pool()
        candidates: list[tuple[str, RegimeTablePayload]] = [(BASELINE_CANDIDATE_ID, baseline)]
        for regime in sorted(WorkloadRegime, key=lambda item: item.value):
            current = baseline.entry(regime)
            for index, action in enumerate(pool):
                if (action.strategy, action.weights, action.target_band) == (
                    current.strategy,
                    current.weights,
                    current.target_band,
                ):
                    continue
                entries = tuple(
                    RegimeActionEntry(
                        regime=entry.regime,
                        strategy=action.strategy if entry.regime is regime else entry.strategy,
                        weights=action.weights if entry.regime is regime else entry.weights,
                        target_band=(
                            action.target_band if entry.regime is regime else entry.target_band
                        ),
                    )
                    for entry in baseline.entries
                )
                candidates.append(
                    (f"{regime.value}-a{index:04d}", RegimeTablePayload(entries=entries))
                )
        limit = self._config.search.max_candidate_tables
        if len(candidates) > limit:
            raise SupervisorPolicyTrainingError(
                f"候補表が設定の上限を超えた（candidates={len(candidates)}; limit={limit}）。"
                "切り詰めずに設定を見直す"
            )
        return tuple(candidates)

    def _action_pool(self) -> tuple[_CandidateAction, ...]:
        """strategy × target band × weight 候補を、設定の並びのまま展開する。

        **範囲外の候補は落とさず拒む。** 黙って落とすと、設定した候補が評価されていない
        ことに気づけない。
        """
        pool: list[_CandidateAction] = []
        for strategy in self._bounds.strategies:
            for band in self._bounds.target_bands:
                for weights in self._config.search.weight_candidates:
                    try:
                        self._bounds.validate_context(
                            strategy=strategy, weights=weights, target_band=band
                        )
                    except ValueError as error:
                        raise SupervisorPolicyTrainingError(
                            f"設定した weight 候補が supervisor.output_bounds の外: {error}"
                        ) from error
                    pool.append(
                        _CandidateAction(strategy=strategy, weights=weights, target_band=band)
                    )
        return tuple(pool)

    # ------------------------------------------------------------------ 評価の入口

    def evaluate(self, policy: SupervisorPolicy, specs: Sequence[EpisodeSpec]) -> PolicyComparison:
        """1つの policy を Rule baseline と**同じ episode 群**で比べる。

        `RulePolicy` も Registry で束縛した `RegimeTableRlPolicy` も、#88 の同じ契約なので
        そのまま渡せる（#89 受入基準）。
        """
        if not specs:
            raise SupervisorPolicyTrainingError("episode の無い評価はしない")
        baseline = self._environment.run_policy(self._rule_policy, specs)
        arm = self._environment.run_policy(policy, specs)
        return SupervisorTrainingEnvironment.compare([baseline, arm])

    def train(
        self,
        specs: Sequence[EpisodeSpec],
        *,
        model_version: str,
        created_at: str,
        code_commit: str | None = None,
        authority_compatibility: tuple[AuthorityStage, ...] = (AuthorityStage.SHADOW,),
    ) -> SupervisorPolicyTrainingReport:
        """候補を並べて評価し、選ばれた表を policy artifact にして返す。

        `created_at` は**呼び出し側が固定する**（壁時計を読まない）。
        `authority_compatibility` の既定が `SHADOW` だけなのは、authority の拡大が
        人の判断（#92 / 決定記録 0057）であり、探索の結果として自動で広がってはならない
        ためである。反実仮想の裏づけが無い artifact は、そもそも `SHADOW` 以外を名乗れない。
        """
        if not specs:
            raise SupervisorPolicyTrainingError("episode の無い探索はしない")
        baseline_arm = self._environment.run_policy(self._rule_policy, specs)
        candidates = self.candidates()
        order = self._evaluation_order(tuple(identifier for identifier, _ in candidates))
        tables = dict(candidates)

        arms: dict[str, PolicyArm] = {}
        outcomes: dict[str, CandidateOutcome] = {}
        for identifier in order:
            outcome, arm = self._evaluate_candidate(
                identifier, tables[identifier], specs, baseline_arm
            )
            outcomes[identifier] = outcome
            if arm is not None:
                arms[identifier] = arm

        selected_id = self._select(outcomes)
        selected_arm = arms.get(selected_id)
        if selected_arm is None:  # pragma: no cover - baseline 候補は常に比較できる
            raise SupervisorPolicyTrainingError("選ばれた候補の arm が無い")
        comparison = SupervisorTrainingEnvironment.compare([baseline_arm, selected_arm])
        selected = outcomes[selected_id]

        episode_ids = tuple(sorted({spec.episode_id for spec in specs}))
        if len(episode_ids) != len(specs):
            raise SupervisorPolicyTrainingError("同じ episode_id を2度並べない")
        promotable_episodes = sum(1 for episode in selected_arm.episodes if episode.promotable)
        counterfactual_backed = (
            selected.improved
            and comparison.learned_controller_available
            and promotable_episodes == len(episode_ids)
            and all(episode.promotable for episode in baseline_arm.episodes)
        )
        artifact = self._build_artifact(
            table=tables[selected_id],
            model_version=model_version,
            created_at=created_at,
            code_commit=code_commit,
            authority_compatibility=authority_compatibility,
            episode_ids=episode_ids,
            candidates_evaluated=len(candidates),
            evidence=PolicyTrainingEvidence(
                training_mode=self._environment.dynamics.mode.value,
                dynamics_provenance=self._environment.dynamics.identity.provenance.value,
                learned_controller_available=self._environment.learned_controller_available,
                counterfactual_backed=counterfactual_backed,
                promotable_episodes=promotable_episodes,
                total_episodes=len(episode_ids),
                improved_over_baseline=selected.improved,
                baseline_policy_version=self._rule_policy.version,
                conditions_sha256=comparison.conditions_sha256,
            ),
        )
        return SupervisorPolicyTrainingReport(
            search_family=self._config.search.family,
            seed=self._config.search.seed.value,
            candidate_order=order,
            outcomes=tuple(outcomes[identifier] for identifier, _ in candidates),
            baseline_policy_version=self._rule_policy.version,
            selected_candidate_id=selected_id,
            improved_over_baseline=selected.improved,
            learned_controller_available=self._environment.learned_controller_available,
            comparison=comparison,
            artifact=artifact,
            promotable=counterfactual_backed,
        )

    # ------------------------------------------------------------------ 補助

    def _evaluation_order(self, identifiers: tuple[str, ...]) -> tuple[str, ...]:
        """seed から評価順を決める。**選択はこの順序に依らない。**

        順序を振ることで「評価順が結果を変えていないか」を試験で確かめられる。
        """
        order = list(identifiers)
        Random(self._config.search.seed.value).shuffle(order)
        return tuple(order)

    def _evaluate_candidate(
        self,
        identifier: str,
        table: RegimeTablePayload,
        specs: Sequence[EpisodeSpec],
        baseline_arm: PolicyArm,
    ) -> tuple[CandidateOutcome, PolicyArm | None]:
        version = f"{self._config.artifact.model_id}-{identifier}"
        policy = _TablePolicy(table, version)
        arm = self._environment.run_policy(policy, specs)
        try:
            comparison = SupervisorTrainingEnvironment.compare([baseline_arm, arm])
        except (EnvironmentUsageError, ValueError) as error:
            # **比べられない候補は勝たせない。** 母集団や条件が揃わない結果を、
            # 「差が出た」として採らない（決定記録 0058 §2.6）。
            return (
                CandidateOutcome(
                    candidate_id=identifier,
                    policy_version=version,
                    comparable=False,
                    rejection=Reason(
                        code="candidate_not_comparable",
                        detail=f"{type(error).__name__}: {error}"[:500],
                    ),
                    safety_violations=arm.safety_violations,
                    invalid_actions=arm.invalid_actions,
                    improved=False,
                ),
                None,
            )
        key = comparison.ranking_key(arm)
        baseline_key = comparison.ranking_key(baseline_arm)
        mean = comparison.mean_matched_reward(arm)
        baseline_mean = comparison.mean_matched_reward(baseline_arm)
        improved = key < baseline_key
        if improved and key[:2] == baseline_key[:2]:
            # 安全側が同点のときだけ reward の差を見る。差は設定した下限を満たすこと。
            minimum = self._config.search.minimum_reward_improvement.value
            improved = (
                mean is not None
                and baseline_mean is not None
                and (mean - baseline_mean) > 0.0
                and (mean - baseline_mean) >= minimum
            )
        return (
            CandidateOutcome(
                candidate_id=identifier,
                policy_version=version,
                comparable=True,
                safety_violations=arm.safety_violations,
                invalid_actions=arm.invalid_actions,
                mean_matched_reward=mean,
                baseline_mean_matched_reward=baseline_mean,
                improved=improved,
            ),
            arm,
        )

    @staticmethod
    def _select(outcomes: dict[str, CandidateOutcome]) -> str:
        """改善した候補の中から最良を選ぶ。**1つも無ければ Baseline の表を選ぶ。**

        並び替えの鍵は `(安全側の違反, 範囲外 action, -reward, 識別子)` で、
        **評価順に依らない**。同点は識別子で決める。
        """
        improved = [outcome for outcome in outcomes.values() if outcome.improved]
        if not improved:
            return BASELINE_CANDIDATE_ID
        best = min(
            improved,
            key=lambda outcome: (
                outcome.safety_violations,
                outcome.invalid_actions,
                -(outcome.mean_matched_reward or 0.0),
                outcome.candidate_id,
            ),
        )
        return best.candidate_id

    def _build_artifact(
        self,
        *,
        table: RegimeTablePayload,
        model_version: str,
        created_at: str,
        code_commit: str | None,
        authority_compatibility: tuple[AuthorityStage, ...],
        episode_ids: tuple[str, ...],
        candidates_evaluated: int,
        evidence: PolicyTrainingEvidence,
    ) -> SupervisorPolicyArtifact:
        manifest = SupervisorPolicyManifest(
            model_id=self._config.artifact.model_id,
            model_version=model_version,
            created_at=created_at,
            action_space_sha256=action_space_sha256(self._bounds),
            rl_training_config_sha256=self._environment.config_sha256,
            rl_policy_config_sha256=self._config_sha256,
            reward_version=self._environment.reward_version,
            training_episode_ids=episode_ids,
            training_evidence=evidence,
            hyperparameters=PolicySearchHyperparameters(
                search_family=self._config.search.family,
                seed=self._config.search.seed.value,
                candidates_evaluated=candidates_evaluated,
                episodes_per_candidate=len(episode_ids),
            ),
            payload_sha256=canonical_sha256(table),
            code_commit=code_commit,
            authority_compatibility=authority_compatibility,
        )
        if manifest.policy_family != POLICY_FAMILY:  # pragma: no cover - Literal で固定済み
            raise SupervisorPolicyTrainingError("policy family が想定と違う")
        return SupervisorPolicyArtifact(manifest=manifest, payload=table)
