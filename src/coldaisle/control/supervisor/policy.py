"""Supervisor policy interface, deterministic RulePolicy, and active/shadow selection. #88.

RLPolicy is a worker-side interface only. The control loop passes an already received candidate
to ``SupervisorCoordinator``; it never waits for or directly invokes RL inference.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.clock import Clock
from coldaisle.control.config import RulePolicyConfig, SupervisorConfig
from coldaisle.control.schema import (
    ControlTick,
    Reason,
    SupervisorDecision,
    SupervisorOutput,
    SupervisorPolicyEvaluation,
    SupervisorPolicyKind,
)
from coldaisle.control.state import ControlStateSnapshot
from coldaisle.control.supervisor.regime import WorkloadRegimeEstimate


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SupervisorInput(_Frozen):
    """同じ state を active / shadow policy が読むための immutable input。"""

    snapshot: ControlStateSnapshot
    recent_history: tuple[ControlStateSnapshot, ...] = ()
    workload: WorkloadRegimeEstimate
    recent_performance: tuple[ControlTick, ...] = ()

    @model_validator(mode="after")
    def _all_context_is_causal_and_matches_the_current_tick(self) -> Self:
        if self.workload.as_of_tick_id != self.snapshot.tick_id:
            raise ValueError("Workload Regime と Supervisor snapshot の tick_id を揃える")
        self._validate_snapshot_history(self.recent_history, self.snapshot)
        previous_tick_id: int | None = None
        for tick in self.recent_performance:
            if tick.tick_id >= self.snapshot.tick_id:
                raise ValueError("recent control performance は現在の snapshot より過去にする")
            if previous_tick_id is not None and tick.tick_id <= previous_tick_id:
                raise ValueError("recent control performance は tick_id の昇順にする")
            previous_tick_id = tick.tick_id
        return self

    @staticmethod
    def _validate_snapshot_history(
        history: Sequence[ControlStateSnapshot], current: ControlStateSnapshot
    ) -> None:
        previous_mono_ms: int | None = None
        previous_tick_id: int | None = None
        for snapshot in history:
            if snapshot.monotonic_ms > current.monotonic_ms or snapshot.tick_id > current.tick_id:
                raise ValueError("recent history に現在より未来の snapshot を入れない")
            if previous_mono_ms is not None and snapshot.monotonic_ms <= previous_mono_ms:
                raise ValueError("recent history は monotonic_ms の昇順にする")
            if previous_tick_id is not None and snapshot.tick_id <= previous_tick_id:
                raise ValueError("recent history は tick_id の昇順にする")
            previous_mono_ms = snapshot.monotonic_ms
            previous_tick_id = snapshot.tick_id


class SupervisorOutputOrigin(StrEnum):
    """その提案を作った policy が、**どの用途で束縛されていたか**（#89 / 決定記録 0061 §2.4）。

    **出力そのものに用途を持たせる。** `SupervisorOutput` は strategy / weights / target band
    しか持たないので、shadow 用に束縛した policy の出力と active 用の出力が**値として
    見分けられない**。用途が値に付いて回らないと、shadow の提案を active slot へ渡す
    配線ミスを Coordinator が止められない。
    """

    ACTIVE_BINDING = "active_binding"
    """active slot として束縛した policy の出力。**いまこの値を発行できる経路は無い**

    （`SupervisorPolicyBinding.for_active` が閉じているため。決定記録 0061 §2.4）。
    """
    SHADOW_BINDING = "shadow_binding"
    """比較のためだけに束縛した policy の出力。**active slot では受け取らない。**"""
    UNVERIFIED = "unverified"
    """用途を名乗っていない出力。既定値で、**active slot では受け取らない**（fail closed）。"""


class ReceivedSupervisorOutput(_Frozen):
    """worker output と、control loop 自身の単調時計で記録した元 snapshot 時刻・受信時刻。

    RL 推論は非同期なので ``output`` は通常、過去の tick の snapshot から作られる。
    ``source_monotonic_ms`` は control loop が worker へ渡した snapshot の ``monotonic_ms`` で、
    鮮度（``supervisor.valid_ms``）はここから数える。worker の時計とは比べない（0028 §2.3）。
    """

    output: SupervisorOutput
    source_monotonic_ms: int = Field(ge=0)
    received_monotonic_ms: int = Field(ge=0)
    origin: SupervisorOutputOrigin = SupervisorOutputOrigin.UNVERIFIED
    """提案を作った policy の束縛用途。**既定は `unverified` で、active slot を通らない。**

    値は `RegimeTableRlPolicy.deliver()` が束縛から写す。手で組み立てて
    `active_binding` を名乗ることはできてしまうが、それは同一プロセス内の偽造という
    既知の残余リスク（決定記録 0050 §3）と同じ扱いである。狙いは**配線の誤りを型で
    止めること**で、shadow 用の policy を active slot へ繋いだ構成が黙って通らなくなる。
    """

    @model_validator(mode="after")
    def _source_precedes_receipt(self) -> Self:
        if self.source_monotonic_ms > self.received_monotonic_ms:
            raise ValueError("元 snapshot の単調時刻を受信単調時刻より後にしない")
        return self


class SupervisorPolicy(Protocol):
    """Rule / RL が同じ入力から同じ schema を返す最小 interface。"""

    @property
    def kind(self) -> SupervisorPolicyKind: ...

    @property
    def version(self) -> str: ...

    def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
        """Demandを含まない戦略 context を返す。"""
        ...


class RLPolicy(SupervisorPolicy, Protocol):
    """#89 が worker process 側で実装する RL Supervisor interface。"""


class RulePolicy:
    """Workload Regime を検証済み config の context へ写像する決定論的 policy。"""

    kind = SupervisorPolicyKind.RULE

    def __init__(self, config: RulePolicyConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock

    @property
    def version(self) -> str:
        """trace と rollback に使う設定済み policy version。"""
        return self._config.version

    def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
        """現在の観測済み regime に対応する strategy / weights / target を返す。"""
        context = self._config.contexts.get(policy_input.workload.regime)
        return SupervisorOutput(
            snapshot_schema_version=policy_input.snapshot.schema_version,
            tick_id=policy_input.snapshot.tick_id,
            ts_ms=policy_input.snapshot.ts_ms,
            policy=self.kind,
            version=self.version,
            regime=policy_input.workload.regime,
            regime_confidence=policy_input.workload.confidence,
            weights=context.weights,
            strategy=context.strategy,
            target_band=context.target_band,
            computed_at_ms=self._clock.now_ms(),
        )


class ShadowRLPolicy:
    """RL proposal を比較用に生成し、active context へ選ぶ API を持たない wrapper。"""

    kind = SupervisorPolicyKind.RL

    def __init__(self, policy: RLPolicy) -> None:
        self._policy = policy

    @property
    def version(self) -> str:
        """内側の artifact version をそのまま記録する。"""
        return self._policy.version

    def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
        """比較用出力を返す。選択は Coordinator の shadow slot だけが行う。"""
        output = self._policy.propose(policy_input)
        if output.policy is not SupervisorPolicyKind.RL:
            raise ValueError("ShadowRLPolicy は RLPolicy output だけを受け付ける")
        return output


class SupervisorCoordinator:
    """inline Rule と受信済み RL candidate を選び、同じ tick の trace を作る。"""

    def __init__(
        self,
        config: SupervisorConfig,
        clock: Clock,
        *,
        rule_policy: SupervisorPolicy | None = None,
    ) -> None:
        self._config = config
        self._rule = rule_policy or RulePolicy(config.rule_policy, clock)

    def evaluate(
        self,
        policy_input: SupervisorInput,
        *,
        now_monotonic_ms: int,
        rl_candidate: ReceivedSupervisorOutput | None = None,
        rl_error: Reason | None = None,
    ) -> SupervisorDecision:
        """active/shadowを評価し、active RL失敗時は即座にRuleへfallbackする。"""
        if now_monotonic_ms < 0:
            raise ValueError("Supervisor の単調時計は負にできない")
        if rl_candidate is not None and rl_error is not None:
            raise ValueError("RL candidate と worker error を同時に渡さない")

        if self._config.active_policy is SupervisorPolicyKind.RULE:
            active = self._evaluate_rule(policy_input)
            fallback = None
        else:
            active = self._evaluate_rl(
                policy_input,
                now_monotonic_ms=now_monotonic_ms,
                candidate=rl_candidate,
                worker_error=rl_error,
                # **active slot は active 用に束縛した提案だけを受け取る**（0061 §2.4）。
                require_active_origin=True,
            )
            fallback = self._evaluate_rule(policy_input) if active.output is None else None

        shadow = None
        if self._config.shadow_policy is SupervisorPolicyKind.RL:
            # shadow は MPC にも Fan にも届かない（0053 §2.3）ので、用途を問わず記録する。
            # 問わないと、昇格前の候補を観測できず、昇格に要る証拠を集められない。
            shadow = self._evaluate_rl(
                policy_input,
                now_monotonic_ms=now_monotonic_ms,
                candidate=rl_candidate,
                worker_error=rl_error,
                require_active_origin=False,
            )

        return SupervisorDecision(
            tick_id=policy_input.snapshot.tick_id,
            ts_ms=policy_input.snapshot.ts_ms,
            snapshot_schema_version=policy_input.snapshot.schema_version,
            active=active,
            fallback=fallback,
            shadow=shadow,
        )

    def _evaluate_rule(self, policy_input: SupervisorInput) -> SupervisorPolicyEvaluation:
        try:
            if self._rule.kind is not SupervisorPolicyKind.RULE:
                raise ValueError("Rule fallback slot には RulePolicy だけを設定する")
            output = self._rule.propose(policy_input)
            self._validate_output(output, policy_input, SupervisorPolicyKind.RULE)
        except Exception as exc:
            return self._failure(
                SupervisorPolicyKind.RULE,
                "supervisor_policy_exception",
                exc,
            )
        return SupervisorPolicyEvaluation(policy=SupervisorPolicyKind.RULE, output=output)

    def _evaluate_rl(
        self,
        policy_input: SupervisorInput,
        *,
        now_monotonic_ms: int,
        candidate: ReceivedSupervisorOutput | None,
        worker_error: Reason | None,
        require_active_origin: bool,
    ) -> SupervisorPolicyEvaluation:
        if candidate is None:
            return SupervisorPolicyEvaluation(
                policy=SupervisorPolicyKind.RL,
                error=worker_error
                or Reason(code="supervisor_unavailable", detail="RLPolicy output が未受信"),
            )
        received = candidate.received_monotonic_ms
        source = candidate.source_monotonic_ms
        if require_active_origin and candidate.origin is not SupervisorOutputOrigin.ACTIVE_BINDING:
            # **用途を名乗らない提案を active にしない**（fail closed）。shadow 用に束縛した
            # policy の出力を active slot へ繋いだ構成は、ここで Rule へ落ちる。
            return SupervisorPolicyEvaluation(
                policy=SupervisorPolicyKind.RL,
                error=Reason(
                    code="supervisor_origin_not_active",
                    detail="active slot に active 用でない提案が届いた"
                    f"（origin={candidate.origin.value}）",
                ),
                received_monotonic_ms=received,
                source_monotonic_ms=source,
            )
        if received > now_monotonic_ms:
            return SupervisorPolicyEvaluation(
                policy=SupervisorPolicyKind.RL,
                error=Reason(
                    code="supervisor_clock_invalid",
                    detail="RLPolicy output の受信単調時刻が現在より未来",
                ),
                received_monotonic_ms=received,
                source_monotonic_ms=source,
            )
        # 受信時刻より厳しい元 snapshot 時刻から数える。受信が遅れた古い提案を新鮮と扱わない。
        if now_monotonic_ms - source > self._config.valid_ms:
            return SupervisorPolicyEvaluation(
                policy=SupervisorPolicyKind.RL,
                error=Reason(code="supervisor_expired", detail="RLPolicy output の有効期限切れ"),
                received_monotonic_ms=received,
                source_monotonic_ms=source,
            )
        try:
            self._validate_output(
                candidate.output,
                policy_input,
                SupervisorPolicyKind.RL,
                source_monotonic_ms=source,
            )
        except Exception as exc:
            return self._failure(
                SupervisorPolicyKind.RL,
                "supervisor_output_invalid",
                exc,
                received_monotonic_ms=received,
                source_monotonic_ms=source,
            )
        return SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RL,
            output=candidate.output,
            received_monotonic_ms=received,
            source_monotonic_ms=source,
        )

    def _validate_output(
        self,
        output: SupervisorOutput,
        policy_input: SupervisorInput,
        expected_policy: SupervisorPolicyKind,
        *,
        source_monotonic_ms: int | None = None,
    ) -> None:
        SupervisorOutput.model_validate(output.model_dump())
        if output.policy is not expected_policy:
            raise ValueError("Supervisor output の policy が configured slot と一致しない")
        current = policy_input.snapshot
        if output.snapshot_schema_version != current.schema_version:
            raise ValueError("Supervisor output の snapshot schema version が input と一致しない")
        if output.tick_id > current.tick_id:
            raise ValueError("Supervisor output の元 tick が現在より未来")
        if output.tick_id == current.tick_id:
            if output.ts_ms != current.ts_ms:
                raise ValueError("Supervisor output の ts_ms が input と一致しない")
            if source_monotonic_ms is not None and source_monotonic_ms != current.monotonic_ms:
                raise ValueError("同じ tick の元 snapshot 単調時刻が input と一致しない")
            if (output.regime, output.regime_confidence) != (
                policy_input.workload.regime,
                policy_input.workload.confidence,
            ):
                raise ValueError("Supervisor output の workload regime が input と一致しない")
        else:
            if expected_policy is SupervisorPolicyKind.RULE or source_monotonic_ms is None:
                raise ValueError("inline RulePolicy output の tick_id が input と一致しない")
            if source_monotonic_ms >= current.monotonic_ms:
                raise ValueError("過去 tick の元 snapshot 単調時刻が現在以降")
            # 過去 tick の提案でも、その後に Regime が変わっていれば古い戦略を使わず Rule へ戻す。
            if output.regime is not policy_input.workload.regime:
                raise ValueError("過去 tick の Supervisor output の workload regime が現在と異なる")
        expected_version = (
            self._config.rule_policy.version
            if expected_policy is SupervisorPolicyKind.RULE
            else self._config.rl_version
        )
        if output.version != expected_version:
            raise ValueError("Supervisor policy version が config と一致しない")
        self._config.output_bounds.validate_context(
            strategy=output.strategy,
            weights=output.weights,
            target_band=output.target_band,
        )

    @staticmethod
    def _failure(
        policy: SupervisorPolicyKind,
        code: str,
        error: Exception,
        *,
        received_monotonic_ms: int | None = None,
        source_monotonic_ms: int | None = None,
    ) -> SupervisorPolicyEvaluation:
        detail = f"{type(error).__name__}: {error}"[:500]
        return SupervisorPolicyEvaluation(
            policy=policy,
            error=Reason(code=code, detail=detail),
            received_monotonic_ms=received_monotonic_ms,
            source_monotonic_ms=source_monotonic_ms,
        )
