"""Rule policy の**完全な識別**（版 + 表の digest。#89 / 決定記録 0061 §2.6）。

#88 の Rule policy は regime から設定済み context への写像で、版が同じでも context の違う
設定を作れる（`RulePolicyConfig` はそれを許す）。版だけで Baseline を名指すと、古い表と
比べた shadow の証拠が、いまの表を上回った証拠として通ってしまう。

そこで、**policy そのものに regime ごとの戦略を聞いて**表を作り、その canonical digest を
識別にする。設定の形ではなく policy の振る舞いを覆うので、同じ版で context だけ違う
policy は別の識別になる。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.control.model.thermal import canonical_sha256
from coldaisle.control.schema import (
    SupervisorObjectiveWeights,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    WorkloadRegime,
)
from coldaisle.control.state import ControlStateSnapshot, TelemetryHealth
from coldaisle.control.supervisor.policy import SupervisorInput, SupervisorPolicy
from coldaisle.control.supervisor.regime import (
    RegimeEvidence,
    RegimeReason,
    WorkloadRegimeEstimate,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RulePolicyIdentity(_Frozen):
    """Rule policy の完全な識別。**版だけでは同じ版の別の表と区別できない。**"""

    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    table_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    """regime ごとに policy へ聞いた戦略（strategy・weights・target band）の表の digest。"""


class _RuleTableEntry(_Frozen):
    regime: WorkloadRegime
    strategy: str
    weights: SupervisorObjectiveWeights
    target_band: SupervisorTargetBand


class _RuleTable(_Frozen):
    entries: tuple[_RuleTableEntry, ...]


def rule_probe_input(regime: WorkloadRegime) -> SupervisorInput:
    """Rule policy に「この regime の戦略は何か」を聞くための最小の入力。

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


def rule_policy_identity(policy: SupervisorPolicy) -> RulePolicyIdentity:
    """Rule policy に regime ごとの戦略を聞き、版と表の digest から識別を作る。

    Rule 以外の policy、聞いたのと違う regime を返す policy は拒む。
    """
    if policy.kind is not SupervisorPolicyKind.RULE:
        raise TypeError("Rule policy の識別は RulePolicy からだけ作る")
    entries: list[_RuleTableEntry] = []
    for regime in sorted(WorkloadRegime, key=lambda item: item.value):
        output = policy.propose(rule_probe_input(regime))
        if output.policy is not SupervisorPolicyKind.RULE:
            raise ValueError("Rule policy が Rule 以外を名乗った")
        if output.regime is not regime:
            raise ValueError(
                f"Rule policy が聞いたのと違う regime を返した（{output.regime.value}）"
            )
        entries.append(
            _RuleTableEntry(
                regime=regime,
                strategy=output.strategy,
                weights=output.weights,
                target_band=output.target_band,
            )
        )
    return RulePolicyIdentity(
        version=policy.version,
        table_sha256=canonical_sha256(_RuleTable(entries=tuple(entries))),
    )
