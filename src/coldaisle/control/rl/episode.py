"""Episode の記録型（#105 / 決定記録 0058 §2.5 / §2.6）。

**安全と reward を1つのスコアにしない。** 比較は辞書式で、安全側の違反が先に立つ
（決定記録 0054 §2.4 と同じ構造）。

**採点できなかった step を黙って落とさない。** 理由別に数え、下限に満たない episode は
比較に使えない（`usable_for_comparison=False`。fail closed）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from enum import StrEnum
from hashlib import sha256
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.model.thermal import ThermalMetricName, canonical_sha256
from coldaisle.control.rl.action import SupervisorAction
from coldaisle.control.rl.dynamics import DynamicsIdentity, DynamicsProvenance
from coldaisle.control.rl.reward import RewardBreakdown, SafetyLedgerEntry
from coldaisle.control.schema import (
    ConfidenceLevel,
    ControllerKind,
    Demand,
    OptimizerStatus,
    PerZone,
    Reason,
    SupervisorPolicyKind,
    WorkloadRegime,
)

EPISODE_SCHEMA_VERSION: Literal[1] = 1
"""`EpisodeResult` の形の版。**欄の意味を変えたら上げる。**"""

MAX_EPISODES_PER_ARM = 1_024
"""1 arm に載せる episode 数の構造上限。調整値ではない。"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def conditions_digest(conditions: Sequence[tuple[str, str]]) -> str:
    """episode ごとの条件 hash の**並び**を1つにまとめる。

    episode ごとに条件は違う（識別子も seed も違う）ので、1つの hash では表せない。
    並びまで含めて覆うことで、「同じ episode 群を同じ条件で回したか」だけを鍵にできる。
    """
    payload = json.dumps(
        [list(item) for item in conditions],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


class TrainingMode(StrEnum):
    """何から遷移を作るか。**結果の型でも混ぜない**（決定記録 0058 §2.2）。"""

    LOGGED = "logged"
    """記録済み trajectory だけ。実測の裏づけがあるが、記録と同じ action しか採点できない。"""
    LEARNED_SIMULATOR = "learned_simulator"
    """learned / 近似 simulator。任意の action を試せるが、裏づけは simulator の妥当性まで。"""
    HYBRID = "hybrid"
    """記録で説明できる step は記録から、残りは simulator から。step ごとに出どころが残る。"""


class TerminationReason(StrEnum):
    """episode が終わった理由。"""

    HORIZON = "horizon"
    SAFETY_VIOLATION = "safety_violation"
    INVALID_ACTION = "invalid_action"
    UNSUPPORTED_ACTION = "unsupported_action"
    TRAJECTORY_EXHAUSTED = "trajectory_exhausted"
    DYNAMICS_UNUSABLE = "dynamics_unusable"
    CONTROLLER_UNUSABLE = "controller_unusable"


class SafetyModel(StrEnum):
    """環境が持っている安全の表現。**Critical Safety そのものではない**（0058 §2.5）。"""

    CONFIGURED_MINIMUM_ONLY = "configured_minimum_only"
    """`safety.yaml` の最低 demand と絶対温度上限だけを screen として使う。

    Reactive Guard（#80）も Critical Safety（#78）も環境には無い。最終裁定は運転時に
    後段の2層が必ず行うので、ここで良かった policy が運転で通る保証にはならない。
    """


class StepRecord(_Frozen):
    """1 step の記録。**適用された Demand は MPC の出力であって、action ではない。**"""

    step_index: int = Field(ge=0)
    tick_id: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    action: SupervisorAction
    regime: WorkloadRegime
    requested: PerZone[Demand]
    """この step で掛けた demand。**Supervisor action からではなく MPC / Gate から来る。**"""
    active_controller: ControllerKind
    fallback_reason: Reason | None = None
    optimizer_status: OptimizerStatus | None = None
    confidence: ConfidenceLevel | None = None
    ood: bool | None = None
    provenance: DynamicsProvenance | None = None
    """遷移の出どころ。採点できなかった step では `None`。"""
    supported: bool
    unsupported_reason: Reason | None = None
    observed: dict[ThermalMetricName, float] = Field(default_factory=dict)
    reward: RewardBreakdown | None = None
    safety: SafetyLedgerEntry

    @model_validator(mode="after")
    def _unsupported_steps_have_no_outcome(self) -> Self:
        if self.supported:
            if self.unsupported_reason is not None:
                raise ValueError("採点できた step に未対応の理由を付けない")
            if self.reward is None or self.provenance is None or not self.observed:
                raise ValueError("採点できた step には観測・出どころ・reward が要る")
            return self
        if self.unsupported_reason is None:
            raise ValueError("採点できない step には理由を残す")
        if self.reward is not None or self.observed or self.provenance is not None:
            # 採点できない step に結果を作ると、「記録に無い action の成果」が生える。
            raise ValueError("採点できない step に観測・reward を作らない")
        return self


class EpisodeCoverage(_Frozen):
    """採点できた step の割合と、できなかった理由の内訳（決定記録 0054 §2.3 と同じ扱い）。"""

    steps: int = Field(ge=0)
    supported_steps: int = Field(ge=0)
    unsupported: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _counts_add_up(self) -> Self:
        if self.supported_steps > self.steps:
            raise ValueError("採点できた step 数が全体を超えている")
        total = sum(self.unsupported.values())
        if self.supported_steps + total != self.steps:
            raise ValueError("採点できた step と理由別の内訳の合計が全体と一致しない")
        if any(count < 0 for count in self.unsupported.values()):
            raise ValueError("理由別の件数を負にしない")
        return self

    @property
    def supported_fraction(self) -> float | None:
        """採点できた割合。step が1つも無ければ `None`（0 とは区別する）。"""
        if self.steps == 0:
            return None
        return self.supported_steps / self.steps


class EpisodeSafety(_Frozen):
    """episode 全体の安全側の台帳。**reward と足し合わせない。**"""

    ceiling_exceedances: int = Field(ge=0)
    floor_shortfalls: int = Field(ge=0)
    invalid_actions: int = Field(ge=0)
    minimum_margin_c: float | None = Field(default=None, allow_inf_nan=False)

    @property
    def violated(self) -> bool:
        """安全側の条件を破ったか。"""
        return self.ceiling_exceedances > 0 or self.floor_shortfalls > 0


class EpisodeResult(_Frozen):
    """1 episode の結果。**同じ条件・同じ seed からは同じ bytes になる。**"""

    schema_version: Literal[1] = EPISODE_SCHEMA_VERSION
    episode_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=120)
    seed: int = Field(ge=0)
    mode: TrainingMode
    safety_model: SafetyModel
    policy: SupervisorPolicyKind
    policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    reward_version: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=64)
    discount: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    dynamics: DynamicsIdentity
    steps: tuple[StepRecord, ...]
    termination: TerminationReason
    termination_reason: Reason
    coverage: EpisodeCoverage
    safety: EpisodeSafety
    conditions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    """policy 以外のすべての条件を覆う hash。**arm を比べる鍵になる。**"""
    usable_for_comparison: bool
    """coverage の下限を満たしたか。満たさない episode の reward は比較に使わない。"""
    learned_controller_available: bool
    """Learned MPC を束縛できたか（決定記録 0058 §2.1）。

    **`False` の episode では Supervisor action が demand に一切効かない。** 反実仮想 artifact が
    無い間はこちらが既定で、requested はすべて Fallback が作る。policy の比較を「差が出なかった」
    と読まないために、結果に必ず残す。
    """
    promotable: bool
    """この結果を昇格 / rollout の根拠にしてよいか。**環境だけが立てる欄である。**

    立つ条件は7つで、1つでも欠ければ `False`（決定記録 0058 §2.3）。

    1. coverage の下限を満たした（`usable_for_comparison`）
    2. 安全側の違反が0（絶対上限の超過・floor を下回った要求）
    3. 設定範囲外の action が0
    4. Learned MPC を束縛できた（`learned_controller_available`）
    5. dynamics が**封をした `DynamicsEvidence`** を持つ（自称の identity では立たない）
    6. その証拠の identity が dynamics の identity と一致し、`registry_attested` なら
       `ArtifactAttestation` の kind / capability / model ID / 版 / artifact hash まで一致する
    7. **すべての step の出どころが、その証拠が裏づける唯一の出どころと等しい**
    """

    @model_validator(mode="after")
    def _evidence_matches_the_claims(self) -> Self:
        if self.coverage.steps != len(self.steps):
            raise ValueError("coverage の step 数が記録した step 数と一致しない")
        if self.safety.violated and self.termination is not TerminationReason.SAFETY_VIOLATION:
            # 違反したのに続きを探索した episode を作れないようにする。
            raise ValueError("安全側の違反がある episode は SAFETY_VIOLATION で終わらせる")
        if self.safety.invalid_actions > 0 and self.termination not in {
            TerminationReason.INVALID_ACTION,
            TerminationReason.SAFETY_VIOLATION,
        }:
            raise ValueError("範囲外の action を出した episode は INVALID_ACTION で終わらせる")
        if self.promotable:
            if self.safety.violated:
                raise ValueError("安全側の違反がある episode を昇格の根拠にしない")
            if not self.usable_for_comparison:
                raise ValueError("coverage 不足の episode を昇格の根拠にしない")
            if not self.dynamics.claims_evidence:
                raise ValueError("裏づけの無い simulator の episode を昇格の根拠にしない")
            if not self.learned_controller_available:
                # action が demand に効いていない episode は、policy の根拠になりえない。
                raise ValueError("Learned MPC を束縛できなかった episode を昇格の根拠にしない")
            provenances = {step.provenance for step in self.steps if step.provenance is not None}
            if provenances - {self.dynamics.provenance}:
                # hybrid で1 step でも別の出どころが混ざれば、その episode は根拠にできない。
                # 証拠そのものとの照合は環境が行う（この型は防御的な検査だけを持つ）。
                raise ValueError("dynamics と違う出どころの step を含む episode を昇格させない")
            if DynamicsProvenance.SIMULATED_PROVISIONAL in provenances:
                raise ValueError("近似 simulator の step を含む episode を昇格の根拠にしない")
        return self

    @property
    def total_reward(self) -> float:
        """採点できた step の reward の総和。**採点できない step を 0 として足さない。**"""
        return math.fsum(step.reward.reward for step in self.steps if step.reward is not None)

    @property
    def discounted_reward(self) -> float:
        """割引後の reward。採点できた step だけを、その step の位置で割り引く。"""
        return math.fsum(
            self.discount**step.step_index * step.reward.reward
            for step in self.steps
            if step.reward is not None
        )

    @property
    def supported_steps(self) -> tuple[StepRecord, ...]:
        """採点できた step だけを、起きた順に返す。"""
        return tuple(step for step in self.steps if step.reward is not None)

    @property
    def violations(self) -> int:
        """安全側の違反の総数。"""
        return self.safety.ceiling_exceedances + self.safety.floor_shortfalls

    def discounted_reward_over(self, steps: int) -> float:
        """**最初の `steps` 個の採点できた step だけ**で割り引いた reward。

        長さの違う episode の総和を並べない（決定記録 0058 §2.6）。途中で終わった episode は
        負の reward を積む回数が少ないので、総和で比べると「早く壊れたほうが良い」になる。
        割引は step の位置ではなく**採点できた順番**で掛ける。採点できない step を挟んだ
        episode だけ割引が重くなるのを避けるためである。
        """
        if steps < 0:
            raise ValueError("比較に使う step 数を負にしない")
        usable = self.supported_steps[:steps]
        return math.fsum(
            self.discount**index * step.reward.reward
            for index, step in enumerate(usable)
            if step.reward is not None
        )

    def digest(self) -> str:
        """この結果そのものを表す SHA-256。"""
        return canonical_sha256(self)


class PolicyArm(_Frozen):
    """1 policy を同じ episode 群で回した結果。"""

    policy: SupervisorPolicyKind
    policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    episodes: tuple[EpisodeResult, ...] = Field(min_length=1, max_length=MAX_EPISODES_PER_ARM)

    @model_validator(mode="after")
    def _episodes_belong_to_this_policy(self) -> Self:
        for episode in self.episodes:
            if episode.policy is not self.policy or episode.policy_version != self.policy_version:
                raise ValueError("arm に別の policy の episode を入れない")
        identifiers = tuple(episode.episode_id for episode in self.episodes)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("同じ episode_id を1 arm に2つ入れない")
        return self

    @property
    def episode_ids(self) -> tuple[str, ...]:
        """この arm が回した episode の並び。"""
        return tuple(episode.episode_id for episode in self.episodes)

    @property
    def conditions(self) -> tuple[tuple[str, str], ...]:
        """episode ごとの (識別子, 条件 hash)。**arm を比べる鍵になる。**"""
        return tuple((episode.episode_id, episode.conditions_sha256) for episode in self.episodes)

    def conditions_sha256(self) -> str:
        """この arm が回した条件の並びを1つにまとめた hash。"""
        return conditions_digest(self.conditions)

    @property
    def safety_violations(self) -> int:
        """安全側の違反の総数。"""
        return sum(
            episode.safety.ceiling_exceedances + episode.safety.floor_shortfalls
            for episode in self.episodes
        )

    @property
    def invalid_actions(self) -> int:
        """設定範囲外の action の総数。"""
        return sum(episode.safety.invalid_actions for episode in self.episodes)

    @property
    def comparable_episode_ids(self) -> tuple[str, ...]:
        """coverage の下限を満たした episode の識別子。"""
        return tuple(
            episode.episode_id for episode in self.episodes if episode.usable_for_comparison
        )

    @property
    def learned_controller_available(self) -> bool:
        """この arm のすべての episode で Learned MPC を束縛できたか。"""
        return all(episode.learned_controller_available for episode in self.episodes)

    def episode(self, episode_id: str) -> EpisodeResult:
        """識別子で episode を引く。無ければ `KeyError`。"""
        for item in self.episodes:
            if item.episode_id == episode_id:
                return item
        raise KeyError(episode_id)


class PolicyComparison(_Frozen):
    """同じ条件・同じ episode 群で複数の policy を比べた結果。"""

    schema_version: Literal[1] = EPISODE_SCHEMA_VERSION
    conditions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arms: tuple[PolicyArm, ...] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def _arms_share_the_same_conditions_and_evidence(self) -> Self:
        expected = self.arms[0].conditions
        comparable = self.arms[0].comparable_episode_ids
        learned = self.arms[0].learned_controller_available
        for arm in self.arms:
            if arm.conditions != expected:
                # 条件の違う結果を同じ表に並べると、差が policy の差に見えてしまう。
                raise ValueError("条件の違う episode 群を同じ比較に入れない")
            if arm.comparable_episode_ids != comparable:
                # **arm ごとに落ちた episode を捨てない。** 捨てると、arm ごとに違う
                # 母集団の平均を並べることになり、都合の悪い episode が消えた arm が勝つ。
                raise ValueError("arm ごとに比較できる episode 群が違う")
            if arm.learned_controller_available != learned:
                # 片方だけ Learned MPC が居る比較は、policy の差を測っていない。
                raise ValueError("arm ごとに Learned MPC の有無が違う")
        if not comparable:
            # 比較できる episode が1つも無い表は、「差が無かった」と読めてしまう。
            raise ValueError("比較できる episode が1つも無い比較を作らない")
        if conditions_digest(expected) != self.conditions_sha256:
            raise ValueError("比較の条件 hash が arm の条件と一致しない")
        policies = tuple((arm.policy, arm.policy_version) for arm in self.arms)
        if len(set(policies)) != len(policies):
            raise ValueError("同じ policy / 版を2つの arm にしない")
        return self

    @property
    def comparable_episode_ids(self) -> tuple[str, ...]:
        """すべての arm で比較に使える episode の識別子（arm 間で同一であることは検証済み）。"""
        return self.arms[0].comparable_episode_ids

    @property
    def learned_controller_available(self) -> bool:
        """比較した episode 群で Learned MPC を束縛できたか。

        **`False` なら、この比較は policy の差を測っていない**（requested はすべて Fallback）。
        読む側がそれを見落とさないよう、表そのものに載せる。
        """
        return self.arms[0].learned_controller_available

    def matched_steps(self) -> dict[str, int]:
        """episode ごとに、**すべての arm が採点できた step 数**（最小）を返す。

        長さの違う episode の総和を並べると、早く終わった arm が「良い」ことになる。
        揃えた長さの上でだけ reward を比べる（決定記録 0058 §2.6）。
        """
        return {
            episode_id: min(len(arm.episode(episode_id).supported_steps) for arm in self.arms)
            for episode_id in self.comparable_episode_ids
        }

    def mean_matched_reward(self, arm: PolicyArm) -> float | None:
        """揃えた長さで割り引いた reward の、比較できる episode にわたる平均。

        1つでも揃えた長さが 0 の episode があれば `None`（**判定できないことを合格にしない**）。
        """
        matched = self.matched_steps()
        if not matched or any(steps == 0 for steps in matched.values()):
            return None
        return math.fsum(
            arm.episode(episode_id).discounted_reward_over(steps)
            for episode_id, steps in matched.items()
        ) / len(matched)

    def ranking_key(self, arm: PolicyArm) -> tuple[int, int, float]:
        """arm の辞書式比較鍵。小さいほど良い。

        **安全が先に立ち、reward では覆らない。** reward を出せない arm は
        `inf` を置いて勝てないようにする。
        """
        mean = self.mean_matched_reward(arm)
        return (
            arm.safety_violations,
            arm.invalid_actions,
            math.inf if mean is None else -mean,
        )

    @property
    def ranked(self) -> tuple[PolicyArm, ...]:
        """辞書式に並べた arm。**安全が先、次に範囲外 action、最後に reward。**"""
        return tuple(sorted(self.arms, key=self.ranking_key))
