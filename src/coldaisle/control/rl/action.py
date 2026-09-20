"""RL Supervisor の action 空間（#105 / 決定記録 0058 §2.1）。

**action は Fan Demand ではない。** RL Supervisor が動かせるのは MPC の戦略・目的関数 weight・
target band までで、Demand は Learned MPC（#86）が決める（決定記録 0027 / 0028 §2.3）。
この module の型は `Demand` も `EffectiveZoneDemand` も PWM も表現できない。

許容範囲は**環境が作らない**。`config/fan-policy.yaml` の `supervisor.output_bounds`
（#103 / #88）をそのまま使う。環境が独自の範囲を持つと、学習で最適だった action が
運転時には Supervisor に拒否される、という食い違いが生まれる。
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.control.config import SupervisorOutputBounds
from coldaisle.control.schema import (
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    WorkloadRegime,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class InvalidSupervisorActionError(ValueError):
    """設定した範囲の外にある action を環境へ渡した。

    環境はこれを **terminal / invalid** として扱い、範囲内へ丸めない
    （決定記録 0058 §2.1）。丸めると、agent は範囲外を出しても罰を受けずに済み、
    運転時に Supervisor が拒否する action を学習し続ける。
    """


class SupervisorAction(_Frozen):
    """RL Supervisor の1 step の action。**Demand を表現できない。**

    `extra="forbid"` なので `front` / `demand` / `pwm` といった鍵は受け付けない。
    「action を Demand に読み替える」経路を型の段階で塞ぐ（AGENTS.md ルール1 / 2）。
    """

    strategy: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    weights: SupervisorObjectiveWeights
    target_band: SupervisorTargetBand

    @classmethod
    def from_output(cls, output: SupervisorOutput) -> Self:
        """既存の `SupervisorOutput`（Rule / RL のどちらでも）から action を取り出す。

        Rule policy と RL policy を**同じ episode 群で**比べるために使う。
        """
        return cls(
            strategy=output.strategy,
            weights=output.weights,
            target_band=output.target_band,
        )

    def to_output(
        self,
        *,
        snapshot_schema_version: int,
        tick_id: int,
        ts_ms: int,
        regime: WorkloadRegime,
        regime_confidence: float,
        policy: SupervisorPolicyKind,
        version: str,
    ) -> SupervisorOutput:
        """MPC が context として読む `SupervisorOutput` へ写す。

        `computed_at_ms` には episode 時刻をそのまま入れる。**壁時計を読まない**
        （決定記録 0054 §2.7 と同じ規則）。
        """
        return SupervisorOutput(
            snapshot_schema_version=snapshot_schema_version,
            tick_id=tick_id,
            ts_ms=ts_ms,
            policy=policy,
            version=version,
            regime=regime,
            regime_confidence=regime_confidence,
            weights=self.weights,
            strategy=self.strategy,
            target_band=self.target_band,
            computed_at_ms=ts_ms,
        )


class ActionSpace:
    """`supervisor.output_bounds` をそのまま action 空間として使う読み取り専用の窓口。"""

    __slots__ = ("_bounds",)

    def __init__(self, bounds: SupervisorOutputBounds) -> None:
        self._bounds = bounds

    @property
    def bounds(self) -> SupervisorOutputBounds:
        """検証に使っている設定済みの範囲。"""
        return self._bounds

    @property
    def strategies(self) -> tuple[str, ...]:
        """選べる戦略。"""
        return self._bounds.strategies

    @property
    def target_bands(self) -> tuple[SupervisorTargetBand, ...]:
        """選べる target band。"""
        return self._bounds.target_bands

    def validate(self, action: SupervisorAction) -> None:
        """範囲外なら `InvalidSupervisorActionError`。**丸めない。**"""
        try:
            self._bounds.validate_context(
                strategy=action.strategy,
                weights=action.weights,
                target_band=action.target_band,
            )
        except ValueError as error:
            raise InvalidSupervisorActionError(str(error)) from error

    def contains(self, action: SupervisorAction) -> bool:
        """action が設定範囲に入っているか返す。"""
        try:
            self.validate(action)
        except InvalidSupervisorActionError:
            return False
        return True
