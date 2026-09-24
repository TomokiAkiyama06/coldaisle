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

import math
from collections.abc import Iterable, Mapping, Sequence
from random import Random
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.config import SupervisorOutputBounds
from coldaisle.control.model.thermal import canonical_sha256
from coldaisle.control.rl.environment import (
    EnvironmentUsageError,
    EpisodeSpec,
    SupervisorTrainingEnvironment,
)
from coldaisle.control.rl.episode import (
    EpisodeResult,
    PolicyArm,
    PolicyComparison,
    TerminationReason,
)
from coldaisle.control.schema import (
    AuthorityStage,
    Reason,
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    WorkloadRegime,
)
from coldaisle.control.state import ControlStateSnapshot, TelemetryHealth
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
from coldaisle.control.supervisor.policy_config import (
    BASELINE_CANDIDATE_ID,
    RlPolicyConfig,
    candidate_identifier,
)
from coldaisle.control.supervisor.regime import (
    RegimeEvidence,
    RegimeReason,
    WorkloadRegimeEstimate,
)

TRAINING_REPORT_SCHEMA_VERSION: Literal[1] = 1

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


def _probe_input(regime: WorkloadRegime) -> SupervisorInput:
    """Baseline policy に「この regime の戦略は何か」を聞くための最小の入力。

    #88 の Rule policy は regime から設定済み context への写像なので、観測の中身に依らない。
    **壁時計を読まない**（時刻はすべて 0 の固定値）。
    """
    snapshot = ControlStateSnapshot(
        tick_id=0,
        ts_ms=0,
        monotonic_ms=0,
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
            confidence=0.0,
            reason=RegimeReason.INSUFFICIENT_HISTORY,
            as_of_tick_id=snapshot.tick_id,
            computed_at_ms=snapshot.ts_ms,
            evidence=RegimeEvidence(observed_window_ms=0),
        ),
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
    """**policy どうしを実際に比べられたか。**

    条件や母集団が揃わず `PolicyComparison` を作れなかった候補だけでなく、
    **Learned MPC を束縛できていなかった run も `False`** にする。束縛できていなければ
    action が demand に効かず、全 arm の requested が同一になるので、比較そのものが
    成立していない（決定記録 0058 §2.1 / §3）。`True` で絞った読み手が
    「policy を比較した結果」だけを受け取れるようにする。
    """
    rejection: Reason | None = None
    safety_violations: int = Field(ge=0)
    invalid_actions: int = Field(ge=0)
    mean_reward_over_common_horizon: float | None = Field(default=None, allow_inf_nan=False)
    """**すべての arm が採点できた共通の長さ**で割り引いた reward の平均。

    候補ごとに別々の長さで採点すると、早く終わった候補ほど負の reward を積む回数が
    少なく、「早く壊れたほうが良い」になる（決定記録 0058 §2.6）。長さは
    `SupervisorPolicyTrainingReport.common_matched_steps` に残す。
    """
    baseline_mean_reward_over_common_horizon: float | None = Field(
        default=None, allow_inf_nan=False
    )
    improved: bool
    truncated_episodes: tuple[str, ...] = ()
    """Baseline より**安全を観測した step が少ないまま、自分の違反以外の理由で**終わった
    episode（判定は `truncated_episodes()`）。

    安全側の台帳（`safety_violations` / `invalid_actions`）は episode 全体を数えるので、
    先に終わった候補は、Baseline がその後で踏んだ違反をそもそも観測しない。それを
    「違反が少ない」と読むと、**壊れたせいで安全に見える**候補が選ばれる。1つでもあれば
    候補を `comparable=False`（理由 `candidate_truncated`）にし、共通の長さからも外す
    （fail closed）。
    """

    short_episodes: tuple[str, ...] = ()
    """Baseline と同じ区間を比べられない episode（`short_episodes()`）。観測した長さか、
    採点できた step の数のどちらかが Baseline より短い。

    打ち切り（`truncated_episodes`）と違い、自分の違反・範囲外 action で先に終わった場合も
    含む。違反は台帳に載るので比較には残す（`comparable=True`）が、**共通の長さには
    入れない**。入れると、壊れた候補の短さで健全な候補どうしが最初の数 step だけで
    並べられ、壊れた候補が「どの健全な候補が選ばれるか」を変えてしまう。
    共通の長さを満たせないので reward は書かず（`None`）、**改善扱いにしない**（fail closed）。
    """

    @model_validator(mode="after")
    def _uncomparable_candidates_never_win(self) -> Self:
        if self.short_episodes and (
            self.improved or self.mean_reward_over_common_horizon is not None
        ):
            raise ValueError("先に終わった候補に共通の長さの reward を書かず、改善扱いにしない")
        truncated_rejection = (
            self.rejection is not None and self.rejection.code == TRUNCATED_REJECTION_CODE
        )
        if bool(self.truncated_episodes) != truncated_rejection:
            # 打ち切りの事実と却下理由を食い違わせない。比べた候補に打ち切りを残さない。
            raise ValueError("打ち切られた episode と candidate_truncated の却下は対で持つ")
        if self.comparable:
            if self.rejection is not None:
                raise ValueError("比べられた候補に却下理由を付けない")
            return self
        if self.rejection is None:
            raise ValueError("比べられなかった候補には理由を残す")
        if self.improved:
            # 比べられなかった候補が「Baseline を上回った」と読めないようにする。
            raise ValueError("比べられなかった候補を改善扱いにしない")
        if (
            self.mean_reward_over_common_horizon is not None
            or self.baseline_mean_reward_over_common_horizon is not None
        ):
            raise ValueError("比べられなかった候補に reward を書かない")
        return self


def candidate_improved(
    *,
    safety_violations: int,
    invalid_actions: int,
    mean_reward: float | None,
    baseline_safety_violations: int,
    baseline_invalid_actions: int,
    baseline_mean_reward: float | None,
    minimum_reward_improvement: float,
) -> bool:
    """候補が Baseline を上回ったか。**探索と報告の型が同じこの関数を使う。**

    `(安全側の違反, 範囲外 action, -reward)` の辞書式比較で厳密に小さいこと。安全側が
    同点のときだけ reward の差を見て、差は正かつ設定した下限以上であること。
    reward を出せない側（`None`）は勝てない。
    """
    key = (safety_violations, invalid_actions, _rank_value(mean_reward))
    baseline_key = (
        baseline_safety_violations,
        baseline_invalid_actions,
        _rank_value(baseline_mean_reward),
    )
    if not key < baseline_key:
        return False
    if key[:2] != baseline_key[:2]:
        return True
    return (
        mean_reward is not None
        and baseline_mean_reward is not None
        and (mean_reward - baseline_mean_reward) > 0.0
        and (mean_reward - baseline_mean_reward) >= minimum_reward_improvement
    )


def select_candidate(outcomes: Iterable[CandidateOutcome]) -> str:
    """改善した候補の中から最良を選ぶ。**1つも無ければ Baseline の表を選ぶ。**

    並び替えの鍵は `(安全側の違反, 範囲外 action, -reward, 識別子)` で、
    **評価順に依らない**。同点は識別子で決める。探索と報告の型が同じこの関数を使う。
    """
    improved = [outcome for outcome in outcomes if outcome.improved]
    if not improved:
        return BASELINE_CANDIDATE_ID
    best = min(
        improved,
        key=lambda outcome: (
            outcome.safety_violations,
            outcome.invalid_actions,
            -(outcome.mean_reward_over_common_horizon or 0.0),
            outcome.candidate_id,
        ),
    )
    return best.candidate_id


def training_counterfactual_backed(
    *, improved_over_baseline: bool, comparison: PolicyComparison
) -> bool:
    """学習結果を反実仮想の裏づけありと言えるか。**探索と報告の型が同じこの関数を使う。**

    選ばれた候補が Baseline を上回り、Learned MPC を束縛でき、比較した**両方の arm の
    すべての episode** が環境の立てた `promotable` を持つときだけ立つ。
    別々に書くと、探索が立てない `promotable` を、保存した報告を読み戻す側が受け取れてしまう。
    """
    return (
        improved_over_baseline
        and comparison.learned_controller_available
        and all(episode.promotable for arm in comparison.arms for episode in arm.episodes)
    )


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
    common_matched_steps: dict[str, int] = Field(default_factory=dict)
    """episode ごとの、**すべての arm が採点できた step 数**（最小）。

    候補の順位はこの長さの上だけで決める。hash では読めないので欄としても残す
    （決定記録 0056 §2.3 と同じ理由）。
    """
    minimum_reward_improvement: float = Field(allow_inf_nan=False)
    """探索で使った `search.minimum_reward_improvement`。

    **改善の判定を報告の記録だけから導き直すため**に残す。無いと、保存した報告の
    `improved` が本当に比較から出た値かを確かめられない。
    """
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
        self._check_derived_values(selected)
        if self.promotable:
            if not self.improved_over_baseline:
                raise ValueError("Baseline を上回らない結果を昇格の根拠にしない")
            if not self.learned_controller_available:
                # action が demand に効いていない条件の結果を根拠にしない（0058 §2.1）。
                raise ValueError("Learned MPC を束縛できていない結果を昇格の根拠にしない")
            if not evidence.counterfactual_backed:
                raise ValueError("反実仮想の裏づけの無い結果を昇格の根拠にしない")
        derived = training_counterfactual_backed(
            improved_over_baseline=self.improved_over_baseline, comparison=self.comparison
        )
        if self.promotable != derived or evidence.counterfactual_backed != derived:
            # **導いた値と違う判定を受け取らない。** 報告が持つ比較から導けるので、保存した
            # 報告や手で作った報告が、探索なら立てない `promotable` を名乗れないようにする。
            raise ValueError("promotable / counterfactual_backed が比較から導いた値と一致しない")
        return self

    def _check_derived_values(self, selected: CandidateOutcome) -> None:
        """**記録から導ける値は、導いた値と一致しなければ受け取らない。**

        探索と同じ関数で導き直す。自称の値どうしが揃っているだけでは足りない
        （揃えて書き換えた報告が通ってしまう）。
        """
        baseline_arm, selected_arm = self.comparison.arms[0], self.comparison.arms[-1]
        horizon = self.common_matched_steps
        baseline_mean = mean_reward_over(baseline_arm, horizon)
        if self.learned_controller_available != self.comparison.learned_controller_available:
            raise ValueError("Learned MPC の有無が比較から導いた値と一致しない")
        if self.baseline_policy_version != baseline_arm.policy_version:
            raise ValueError("Baseline の版が比較の Baseline arm と一致しない")
        # 選ばれた候補の欄は、比較に残した arm から導き直せる。
        if (selected.safety_violations, selected.invalid_actions) != (
            selected_arm.safety_violations,
            selected_arm.invalid_actions,
        ):
            raise ValueError("選ばれた候補の安全側の記録が比較の arm と一致しない")
        if selected.comparable:
            expected_short = short_episodes(selected_arm, baseline_arm)
            if selected.short_episodes != expected_short:
                raise ValueError("選ばれた候補の short_episodes が比較から導いた値と一致しない")
            expected_mean = None if expected_short else mean_reward_over(selected_arm, horizon)
            if selected.mean_reward_over_common_horizon != expected_mean:
                raise ValueError("選ばれた候補の reward が比較から導いた値と一致しない")
        for outcome in self.outcomes:
            if not outcome.comparable:
                continue
            if outcome.baseline_mean_reward_over_common_horizon != baseline_mean:
                raise ValueError("Baseline の reward が比較から導いた値と一致しない")
            expected = not outcome.short_episodes and candidate_improved(
                safety_violations=outcome.safety_violations,
                invalid_actions=outcome.invalid_actions,
                mean_reward=outcome.mean_reward_over_common_horizon,
                baseline_safety_violations=baseline_arm.safety_violations,
                baseline_invalid_actions=baseline_arm.invalid_actions,
                baseline_mean_reward=baseline_mean,
                minimum_reward_improvement=self.minimum_reward_improvement,
            )
            if outcome.improved != expected:
                raise ValueError(
                    f"候補の改善判定が記録から導いた値と一致しない（{outcome.candidate_id}）"
                )
        if self.selected_candidate_id != select_candidate(self.outcomes):
            raise ValueError("選ばれた候補が記録から導いた選択と一致しない")
        evidence = self.artifact.manifest.training_evidence
        promotable_episodes = sum(1 for episode in selected_arm.episodes if episode.promotable)
        if (evidence.promotable_episodes, evidence.total_episodes) != (
            promotable_episodes,
            len(selected_arm.episodes),
        ):
            raise ValueError("artifact の episode 数が比較から導いた値と一致しない")
        if evidence.conditions_sha256 != self.comparison.conditions_sha256:
            raise ValueError("artifact の条件 hash が比較と一致しない")
        if evidence.baseline_policy_version != self.baseline_policy_version:
            raise ValueError("artifact の Baseline の版が報告と一致しない")

    def digest(self) -> str:
        """この報告そのものを表す SHA-256。"""
        return canonical_sha256(self)


def common_matched_steps(arms: Sequence[PolicyArm]) -> dict[str, int]:
    """episode ごとに、**すべての arm が採点できた step 数**（最小）を返す。

    `PolicyComparison.matched_steps()` と同じ考え方だが、対象が2 arm ではなく
    **候補すべて**である。2 arm ごとに別々の長さで採点すると、候補 A を4 step、候補 B を
    2 step で割り引いた平均を同じ表に並べることになる（決定記録 0058 §2.6）。

    比較に使える episode は arm 間で一致している（`compare()` が要求する）ので、
    先頭の arm の並びを基準にする。
    """
    if not arms:
        return {}
    return {
        episode_id: min(len(arm.episode(episode_id).supported_steps) for arm in arms)
        for episode_id in arms[0].comparable_episode_ids
    }


def mean_reward_over(arm: PolicyArm, horizon: Mapping[str, int]) -> float | None:
    """揃えた長さで割り引いた reward の、episode にわたる平均。

    長さが 0 の episode が1つでもあれば `None`。**判定できないことを合格にしない**
    （`PolicyComparison.mean_matched_reward` と同じ規則）。
    """
    if not horizon or any(steps == 0 for steps in horizon.values()):
        return None
    return math.fsum(
        arm.episode(episode_id).discounted_reward_over(steps)
        for episode_id, steps in horizon.items()
    ) / len(horizon)


_OWN_FAILURE_TERMINATIONS = frozenset(
    {TerminationReason.SAFETY_VIOLATION, TerminationReason.INVALID_ACTION}
)
"""候補自身の違反で終わった理由。これで短くなった episode は、その違反が台帳に載る。"""

TRUNCATED_REJECTION_CODE = "candidate_truncated"
"""Baseline より先に、自分の違反以外の理由で打ち切られた候補の却下理由。"""


def safety_observed_steps(episode: EpisodeResult) -> int:
    """episode の**観測した長さ**: 安全側の結果を観測した step の数。

    採点できた step（成果と安全の両方を観測した）と、採点はできないが安全側の違反を
    記録した step（`safety_floor_shortfall` の終端など）を数える。`dynamics_unusable` や
    `controller_unusable` で積まれた終端記録は、その step の安全を観測していないので数えない。
    環境は採点できない終端も記録に積むので、記録の数（`steps`）とは一致しない。

    **安全側の台帳を Baseline と同じ区間で比べられるか**を決める長さである（打ち切りの判定）。
    reward を同じ区間で比べられるかは、採点できた step の数（`supported_steps`）で決める。
    2つは測っているものが違うので、目的ごとに意図して使い分ける（決定記録 0061 §2.7）。
    """
    return sum(1 for step in episode.steps if step.supported or step.safety.violated)


def short_episodes(arm: PolicyArm, baseline: PolicyArm) -> tuple[str, ...]:
    """`arm` が Baseline と**同じ区間を比べられない** episode。終わった理由を問わない。

    次のどちらかを満たせば短い。

    - 観測した長さ（`safety_observed_steps()`）が Baseline より短い。Baseline が後で観測した
      安全側の結果を観測していないので、違反の数を同じ区間で比べられない
    - 採点できた step の数（`supported_steps`）が Baseline より少ない。最後の step で
      `safety_floor_shortfall` を記録して終わった候補は観測した長さが Baseline と等しくても、
      reward を持つ step が1つ少ない。共通の長さに入れると、健全な候補すべての採点区間を縮める

    短い arm は共通の長さに入れず、reward を書かず、改善扱いにしない（`_score`）。
    """
    short: list[str] = []
    for episode in arm.episodes:
        reference = baseline.episode(episode.episode_id)
        if safety_observed_steps(episode) < safety_observed_steps(reference) or len(
            episode.supported_steps
        ) < len(reference.supported_steps):
            short.append(episode.episode_id)
    return tuple(sorted(short))


def truncated_episodes(arm: PolicyArm, baseline: PolicyArm) -> tuple[str, ...]:
    """観測した長さが Baseline より短く、**自分の違反・範囲外 action 以外の理由で**終わった
    episode。

    判定は観測した長さ（`safety_observed_steps()`）だけで行う。reward を持つ step が少ない
    だけの候補は、安全側の台帳は Baseline と同じ区間を見ているので却下しない（短い扱いで
    共通の長さから外す）。自分の違反で終わった episode は、その違反が台帳に載るので比較には
    残す。それ以外の理由（`dynamics_unusable` / `controller_unusable` /
    `unsupported_action` / trace の枯渇など）で観測が足りない episode は、違反を観測しなかった
    ことが「違反が少ない」に見えるので、候補ごと比較から外す（`candidate_rejection()`）。
    """
    return tuple(
        sorted(
            episode.episode_id
            for episode in arm.episodes
            if episode.termination not in _OWN_FAILURE_TERMINATIONS
            and safety_observed_steps(episode)
            < safety_observed_steps(baseline.episode(episode.episode_id))
        )
    )


def candidate_rejection(arm: PolicyArm, baseline: PolicyArm) -> Reason | None:
    """候補を Baseline と**比べてよいか**を1度だけ決める。比べてよければ `None`。

    採点（共通の長さ・reward・改善）より**前に**決める。後で落とすと、落とす候補が
    共通の長さを縮め、健全な候補まで短い区間で採点されてしまう。
    """
    try:
        comparison = SupervisorTrainingEnvironment.compare([baseline, arm])
    except (EnvironmentUsageError, ValueError) as error:
        # **比べられない候補は勝たせない。** 母集団や条件が揃わない結果を、
        # 「差が出た」として採らない（決定記録 0058 §2.6）。
        return Reason(
            code="candidate_not_comparable",
            detail=f"{type(error).__name__}: {error}"[:500],
        )
    if not comparison.learned_controller_available:
        # **policy を比べていない。** Learned MPC を束縛できていない episode 群では
        # action が demand に効かず、全 arm の requested が同一になる（0058 §2.1 / §3）。
        return Reason(
            code="learned_controller_unavailable",
            detail=(
                "Learned MPC を束縛できていないので action が demand に効かない。"
                "候補間の差を測っていない（決定記録 0058 §3）"
            ),
        )
    truncated = truncated_episodes(arm, baseline)
    if truncated:
        # **壊れたせいで安全に見える候補を比べない。** 先に終わった候補は、Baseline が
        # その後で踏んだ違反を観測していない（fail closed）。
        return Reason(
            code=TRUNCATED_REJECTION_CODE,
            detail=(
                "Baseline より安全を観測した step が少ないまま、自分の違反以外の理由で"
                f"終わった episode がある（{', '.join(truncated)}）"
            )[:500],
        )
    return None


def scoring_horizon(
    baseline: PolicyArm,
    arms: Mapping[str, PolicyArm],
    rejections: Mapping[str, Reason],
) -> dict[str, int]:
    """Baseline と、**比べてよく、かつ Baseline より先に終わっていない候補だけ**から
    共通の長さを決める。

    却下した候補（打ち切りを含む）や、自分の違反で先に終わった候補を入れると、その候補の
    短さが健全な候補すべての採点区間を縮め、健全な候補どうしを最初の数 step だけで
    並べることになる。先に終わった候補は安全側の台帳で比べ、reward は書かない（`_score`）。
    """
    return common_matched_steps(
        (
            baseline,
            *(
                arms[key]
                for key in sorted(arms)
                if key not in rejections and not short_episodes(arms[key], baseline)
            ),
        )
    )


def _rank_value(mean: float | None) -> float:
    """reward を出せない arm を勝たせないための順位値。"""
    return math.inf if mean is None else -mean


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
        "_rule_policy",
    )

    def __init__(
        self,
        environment: SupervisorTrainingEnvironment,
        *,
        policy_config: RlPolicyConfig,
        policy_config_sha256: str,
        bounds: SupervisorOutputBounds,
        rule_policy: SupervisorPolicy,
    ) -> None:
        """探索の設定と Baseline を束ねる。**噛み合わなければ生成時に落とす。**

        **Baseline の表は設定からではなく、渡された `rule_policy` そのものから作る**
        （`baseline_table()`）。設定と policy を別々に受け取ると、版だけ同じで中身の違う
        2つの Baseline——報告に載る arm と、`baseline` 候補の表——を作れてしまう。
        """
        if rule_policy.kind is not SupervisorPolicyKind.RULE:
            raise SupervisorPolicyTrainingError("Baseline には RulePolicy を渡す")
        self._environment = environment
        self._config = policy_config
        self._config_sha256 = policy_config_sha256
        self._bounds = bounds
        self._rule_policy = rule_policy

    # ------------------------------------------------------------------ 候補の並べ方

    def baseline_table(self) -> RegimeTablePayload:
        """**評価する Rule policy そのものに聞いて**起点の表を作る。

        設定（`rule_policy.contexts`）から作ると、版だけ同じで中身の違う policy を渡された
        ときに、報告に載る Baseline arm と `baseline` 候補の表が別物になる。#88 の Rule policy
        は regime から context への写像なので、regime ごとに1度聞けば表が取れる。

        範囲外の欄を返す policy は**丸めず拒む**（決定記録 0058 §2.1 と同じ規律）。
        """
        entries: list[RegimeActionEntry] = []
        for regime in sorted(WorkloadRegime, key=lambda item: item.value):
            try:
                output = self._rule_policy.propose(_probe_input(regime))
            except Exception as error:
                raise SupervisorPolicyTrainingError(
                    f"Baseline policy が {regime.value} の戦略を返せない: {error}"
                ) from error
            if output.policy is not SupervisorPolicyKind.RULE:
                raise SupervisorPolicyTrainingError("Baseline policy が Rule 以外を名乗った")
            if output.regime is not regime:
                raise SupervisorPolicyTrainingError(
                    f"Baseline policy が聞いたのと違う regime を返した（{output.regime.value}）"
                )
            try:
                self._bounds.validate_context(
                    strategy=output.strategy,
                    weights=output.weights,
                    target_band=output.target_band,
                )
            except ValueError as error:
                raise SupervisorPolicyTrainingError(
                    f"Baseline policy の {regime.value} が supervisor.output_bounds の外: {error}"
                ) from error
            entries.append(
                RegimeActionEntry(
                    regime=regime,
                    strategy=output.strategy,
                    weights=output.weights,
                    target_band=output.target_band,
                )
            )
        return RegimeTablePayload(entries=tuple(entries))

    def candidates(self) -> tuple[tuple[str, RegimeTablePayload], ...]:
        """評価する候補を `(識別子, 表)` の決定論的な並びで返す。

        `per_regime_coordinate_v1` は、Baseline の表の **regime を1つだけ**候補 action へ
        差し替えた表をすべて並べる。Baseline と同じになる差し替えは並べない
        （同じ表を2度評価しても何も分からない）。

        **上限を超えたら切り詰めず落とす。** 黙って切ると、報告に出ない候補が生まれ、
        「全候補を比べた」と読めてしまう。
        """
        baseline = self.baseline_table()
        # **数えてから作る。** 直積と表を先に作ってから上限を見ると、大きいが妥当な設定で
        # 上限の検査に届く前に資源を使い切る。
        projected = self._projected_candidate_count(baseline)
        limit = self._config.search.max_candidate_tables
        if projected > limit:
            raise SupervisorPolicyTrainingError(
                f"候補表が設定の上限を超えた（candidates={projected}; limit={limit}）。"
                "切り詰めずに設定を見直す"
            )
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
                    (candidate_identifier(regime, index), RegimeTablePayload(entries=entries))
                )
        if len(candidates) != projected:  # pragma: no cover - 数え方と作り方の食い違い
            raise SupervisorPolicyTrainingError("候補表の数が事前に数えた数と一致しない")
        return tuple(candidates)

    def _projected_candidate_count(self, baseline: RegimeTablePayload) -> int:
        """候補表の数を**作らずに**数える。

        action の候補は strategy × target band × weight の直積で、各軸は重複しない
        （`output_bounds` と `weight_candidates` の検証）ので、直積の要素も重複しない。
        regime ごとに、Baseline と同じ action になる差し替えが直積に含まれていれば1つ除く。
        """
        strategies = self._bounds.strategies
        bands = self._bounds.target_bands
        weights = self._config.search.weight_candidates
        pool_size = len(strategies) * len(bands) * len(weights)
        count = 1
        for regime in WorkloadRegime:
            current = baseline.entry(regime)
            in_pool = (
                current.strategy in strategies
                and current.target_band in bands
                and current.weights in weights
            )
            count += pool_size - (1 if in_pool else 0)
        return count

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

        # 1巡目: 候補を回し、Baseline と**並べられるか**だけを判定する。
        arms: dict[str, PolicyArm] = {}
        rejections: dict[str, Reason] = {}
        for identifier in order:
            arm = self._run_candidate(identifier, tables[identifier], specs)
            arms[identifier] = arm
            rejection = candidate_rejection(arm, baseline_arm)
            if rejection is not None:
                rejections[identifier] = rejection

        # 2巡目: **すべての arm に共通の長さ**を決めてから採点する。候補ごとに別々の長さで
        # 割り引いた reward を並べると、早く終わった候補ほど負の reward を積む回数が少なく、
        # 「早く壊れたほうが良い」になる（決定記録 0058 §2.6）。
        horizon = scoring_horizon(baseline_arm, arms, rejections)
        outcomes = self._score(
            arms=arms,
            rejections=rejections,
            baseline_arm=baseline_arm,
            horizon=horizon,
        )

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
        counterfactual_backed = training_counterfactual_backed(
            improved_over_baseline=selected.improved, comparison=comparison
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
            common_matched_steps=dict(sorted(horizon.items())),
            minimum_reward_improvement=self._config.search.minimum_reward_improvement.value,
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

    def _run_candidate(
        self,
        identifier: str,
        table: RegimeTablePayload,
        specs: Sequence[EpisodeSpec],
    ) -> PolicyArm:
        """候補を回す。比べてよいかは `candidate_rejection()`、採点は `_score()` が決める。

        採点を分けるのは、共通の長さが**すべての候補を回し終えるまで決まらない**ためである。
        """
        version = f"{self._config.artifact.model_id}-{identifier}"
        return self._environment.run_policy(_TablePolicy(table, version), specs)

    def _score(
        self,
        *,
        arms: Mapping[str, PolicyArm],
        rejections: Mapping[str, Reason],
        baseline_arm: PolicyArm,
        horizon: Mapping[str, int],
    ) -> dict[str, CandidateOutcome]:
        """**共通の長さ**で採点し、Baseline を上回ったかを判定する。"""
        baseline_mean = mean_reward_over(baseline_arm, horizon)
        minimum = self._config.search.minimum_reward_improvement.value
        outcomes: dict[str, CandidateOutcome] = {}
        for identifier, arm in arms.items():
            rejection = rejections.get(identifier)
            if rejection is not None:
                outcomes[identifier] = CandidateOutcome(
                    candidate_id=identifier,
                    policy_version=f"{self._config.artifact.model_id}-{identifier}",
                    comparable=False,
                    rejection=rejection,
                    safety_violations=arm.safety_violations,
                    invalid_actions=arm.invalid_actions,
                    improved=False,
                    truncated_episodes=(
                        truncated_episodes(arm, baseline_arm)
                        if rejection.code == TRUNCATED_REJECTION_CODE
                        else ()
                    ),
                )
                continue
            short = short_episodes(arm, baseline_arm)
            if short:
                # **先に終わった候補は共通の長さの reward を持たない。** 違反は台帳に載るので
                # 比較には残すが、改善扱いにしない（fail closed。0061 §2.7）。
                outcomes[identifier] = CandidateOutcome(
                    candidate_id=identifier,
                    policy_version=f"{self._config.artifact.model_id}-{identifier}",
                    comparable=True,
                    safety_violations=arm.safety_violations,
                    invalid_actions=arm.invalid_actions,
                    baseline_mean_reward_over_common_horizon=baseline_mean,
                    improved=False,
                    short_episodes=short,
                )
                continue
            mean = mean_reward_over(arm, horizon)
            improved = candidate_improved(
                safety_violations=arm.safety_violations,
                invalid_actions=arm.invalid_actions,
                mean_reward=mean,
                baseline_safety_violations=baseline_arm.safety_violations,
                baseline_invalid_actions=baseline_arm.invalid_actions,
                baseline_mean_reward=baseline_mean,
                minimum_reward_improvement=minimum,
            )
            outcomes[identifier] = CandidateOutcome(
                candidate_id=identifier,
                policy_version=f"{self._config.artifact.model_id}-{identifier}",
                comparable=True,
                safety_violations=arm.safety_violations,
                invalid_actions=arm.invalid_actions,
                mean_reward_over_common_horizon=mean,
                baseline_mean_reward_over_common_horizon=baseline_mean,
                improved=improved,
            )
        return outcomes

    @staticmethod
    def _select(outcomes: Mapping[str, CandidateOutcome]) -> str:
        """改善した候補の中から最良を選ぶ（`select_candidate()`）。"""
        return select_candidate(outcomes.values())

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
