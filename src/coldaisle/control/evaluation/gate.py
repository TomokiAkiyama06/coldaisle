"""rollout gate（#91 の受入基準「rollout 可否の gate 条件を定義できる」/ 決定記録 0054 §2.4）。

**Safety が先に立ち、ほかの段では覆らない。** 判定は3段（`safety` → `evidence` → `cost`）で、
上の段が1つでも落ちれば結果は `blocked` になる。cost がどれだけ良くても変わらない。

**判定できないことを合格にしない。** 欠測・未設定・coverage 不足はすべて `blocked` にする
（fail closed）。gate の結果は**助言**であり、昇格・降格の判断は #92 と人が行う。

判定は **holdout segment だけ**を見る（0054 §2.5）。Safety の条件は平均ではなく
**worst-case segment** で決める。1回の危険な run が、良い run の数で薄まらないようにする。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from coldaisle.control.evaluation.config import EvaluationConfig
from coldaisle.control.evaluation.model import (
    AppliedArmReport,
    CountedReason,
    CounterfactualArmReport,
    GateCondition,
    GateOutcome,
    GateResult,
    GateStage,
    GroupKind,
    SegmentReport,
    SegmentRole,
    WorstCase,
    WorstCaseKind,
)


@dataclass
class _AppliedEvidence:
    """holdout の中で、1つの適用 arm から集めた最悪値。"""

    segments: int = 0
    temperature_reports: int = 0
    exceedances: int = 0
    emergency_ticks: int = 0
    fault_ticks: int = 0
    minimum_margin_c: float | None = None


@dataclass
class _CounterfactualEvidence:
    """holdout の中で、1つの counterfactual arm から集めた最悪値。"""

    segments: int = 0
    outcomes: int = 0
    scored: int = 0
    minimum_identifiable_fraction: float | None = None
    maximum_underprediction_c: float | None = None
    optimizer_samples: int = 0
    maximum_timeout_rate: float | None = None
    maximum_latency_ms: float | None = None
    proposals: int = 0
    floor_shortfalls: int | None = None
    predictions: int = 0
    sufficient_segments: int = 0
    worst: dict[WorstCaseKind, WorstCase] = field(default_factory=dict)


def evaluate_gates(
    segments: Sequence[SegmentReport],
    *,
    config: EvaluationConfig,
    worst_cases: Sequence[WorstCase],
) -> tuple[GateResult, ...]:
    """arm ごとの rollout 可否を返す。**holdout の `overall` group だけ**を見る。"""
    applied: dict[str, _AppliedEvidence] = {}
    counterfactual: dict[str, _CounterfactualEvidence] = {}
    holdout = tuple(segment for segment in segments if segment.role is SegmentRole.HOLDOUT)
    for segment in holdout:
        for group in segment.groups:
            if group.kind is not GroupKind.OVERALL:
                continue
            for applied_report in group.applied:
                _collect_applied(
                    applied.setdefault(applied_report.arm_key, _AppliedEvidence()), applied_report
                )
            for shadow_report in group.counterfactual:
                _collect_counterfactual(
                    counterfactual.setdefault(shadow_report.arm_key, _CounterfactualEvidence()),
                    shadow_report,
                )
    worst_by_key = _worst_by_arm(worst_cases, holdout)
    results = [_applied_gate(key, applied[key], config, worst_by_key) for key in sorted(applied)]
    results.extend(
        _counterfactual_gate(key, counterfactual[key], config, worst_by_key)
        for key in sorted(counterfactual)
    )
    return tuple(results)


def _worst_by_arm(
    worst_cases: Sequence[WorstCase], holdout: Sequence[SegmentReport]
) -> dict[tuple[str, WorstCaseKind], WorstCase]:
    """gate に添える worst-case（**holdout のものだけ**）。"""
    allowed = {(segment.run_id, segment.index) for segment in holdout}
    found: dict[tuple[str, WorstCaseKind], WorstCase] = {}
    for case in worst_cases:
        if (case.run_id, case.segment_index) not in allowed:
            continue
        # `_worst_cases` は種類ごとに悪い順で並べるので、最初に見たものが最悪。
        found.setdefault((case.arm_key, case.kind), case)
    return found


def _collect_applied(evidence: _AppliedEvidence, report: AppliedArmReport) -> None:
    evidence.segments += 1
    evidence.temperature_reports += len(report.temperatures)
    evidence.emergency_ticks = max(evidence.emergency_ticks, report.interventions.emergency_ticks)
    evidence.fault_ticks = max(evidence.fault_ticks, report.interventions.fault_ticks)
    for temperature in report.temperatures:
        evidence.exceedances = max(evidence.exceedances, temperature.exceedances)
        margin = temperature.margin.minimum
        evidence.minimum_margin_c = (
            margin if evidence.minimum_margin_c is None else min(evidence.minimum_margin_c, margin)
        )


def _collect_counterfactual(
    evidence: _CounterfactualEvidence, report: CounterfactualArmReport
) -> None:
    evidence.segments += 1
    evidence.outcomes += report.coverage.outcomes
    evidence.scored += report.coverage.scored
    evidence.proposals += report.proposals
    evidence.predictions += len(report.predictions)
    evidence.sufficient_segments += int(report.coverage.sufficient)
    if report.safety_floor_shortfalls is not None:
        # Safety は**最悪の segment**で見る。平均で薄めない（決定記録 0054 §2.4）。
        evidence.floor_shortfalls = (
            report.safety_floor_shortfalls
            if evidence.floor_shortfalls is None
            else max(evidence.floor_shortfalls, report.safety_floor_shortfalls)
        )
    fraction = report.coverage.identifiable_fraction
    if fraction is not None:
        evidence.minimum_identifiable_fraction = (
            fraction
            if evidence.minimum_identifiable_fraction is None
            else min(evidence.minimum_identifiable_fraction, fraction)
        )
    for prediction in report.predictions:
        value = prediction.maximum_underprediction
        evidence.maximum_underprediction_c = (
            value
            if evidence.maximum_underprediction_c is None
            else max(evidence.maximum_underprediction_c, value)
        )
    optimizer = report.optimizer
    if optimizer is not None:
        evidence.optimizer_samples += optimizer.samples
        evidence.maximum_timeout_rate = (
            optimizer.timeout_rate
            if evidence.maximum_timeout_rate is None
            else max(evidence.maximum_timeout_rate, optimizer.timeout_rate)
        )
        if optimizer.latency_ms is not None:
            evidence.maximum_latency_ms = (
                optimizer.latency_ms.maximum
                if evidence.maximum_latency_ms is None
                else max(evidence.maximum_latency_ms, optimizer.latency_ms.maximum)
            )


def _applied_gate(
    arm_key: str,
    evidence: _AppliedEvidence,
    config: EvaluationConfig,
    worst: dict[tuple[str, WorstCaseKind], WorstCase],
) -> GateResult:
    """適用された構成の gate。**Safety の段だけを持つ。**

    比較の相手となる別の運転が同じ条件で存在しない以上、適用実績に対する cost の
    合否は決められない（0054 §5）。**決められないものを合格にしない**ため、条件を置かない。
    """
    safety = config.gate.safety
    conditions = [
        _at_most(
            GateStage.SAFETY,
            "ceiling_exceedances",
            observed=(None if evidence.temperature_reports == 0 else float(evidence.exceedances)),
            limit=float(safety.maximum_ceiling_exceedances.value),
            reason="no_temperature_evidence",
            worst_case=worst.get((arm_key, WorstCaseKind.CEILING_EXCEEDANCES)),
        ),
        _at_least(
            GateStage.SAFETY,
            "threshold_margin_c",
            observed=evidence.minimum_margin_c,
            limit=safety.minimum_threshold_margin_c.value,
            reason="no_temperature_evidence",
            worst_case=worst.get((arm_key, WorstCaseKind.MINIMUM_THRESHOLD_MARGIN)),
        ),
        _at_most(
            GateStage.SAFETY,
            "emergency_ticks",
            observed=float(evidence.emergency_ticks),
            limit=float(safety.maximum_emergency_ticks.value),
            reason="no_tick_evidence",
            worst_case=worst.get((arm_key, WorstCaseKind.EMERGENCY_TICKS)),
        ),
        _at_most(
            GateStage.SAFETY,
            "fault_ticks",
            observed=float(evidence.fault_ticks),
            limit=float(safety.maximum_fault_ticks.value),
            reason="no_tick_evidence",
        ),
    ]
    return _result(arm_key, tuple(conditions))


def _counterfactual_gate(
    arm_key: str,
    evidence: _CounterfactualEvidence,
    config: EvaluationConfig,
    worst: dict[tuple[str, WorstCaseKind], WorstCase],
) -> GateResult:
    """適用されなかった提案の gate。

    Safety の段で見るのは、**記録から言える**「Critical Safety が引き上げたはずの要求を
    何度出したか」である。実行されていない提案の温度実績は存在しないので、そこは見ない
    （0054 §2.2 / §2.4）。
    """
    gate = config.gate
    conditions = [
        _at_most(
            GateStage.SAFETY,
            "safety_floor_shortfalls",
            observed=(
                None if evidence.floor_shortfalls is None else float(evidence.floor_shortfalls)
            ),
            limit=float(gate.safety.maximum_floor_shortfalls.value),
            reason="no_proposal_evidence",
        ),
        _at_least(
            GateStage.EVIDENCE,
            "identifiable_fraction",
            observed=evidence.minimum_identifiable_fraction,
            limit=gate.evidence.minimum_identifiable_fraction.value,
            reason="no_outcome_evidence",
            worst_case=worst.get((arm_key, WorstCaseKind.MINIMUM_IDENTIFIABLE_FRACTION)),
        ),
        _at_least(
            GateStage.EVIDENCE,
            "scored_outcomes",
            observed=float(evidence.scored),
            limit=float(gate.evidence.minimum_scored_outcomes.value),
            reason="no_outcome_evidence",
        ),
        _at_most(
            GateStage.COST,
            "underprediction_c",
            observed=evidence.maximum_underprediction_c,
            limit=gate.cost.maximum_underprediction_c.value,
            # coverage が足りなければ予測指標そのものを出さない（0054 §2.3）。
            reason="insufficient_coverage",
            worst_case=worst.get((arm_key, WorstCaseKind.MAXIMUM_UNDERPREDICTION)),
        ),
        _at_most(
            GateStage.COST,
            "optimizer_timeout_rate",
            observed=evidence.maximum_timeout_rate,
            limit=gate.cost.maximum_optimizer_timeout_rate.value,
            reason="no_optimizer_evidence",
        ),
        _at_most(
            GateStage.COST,
            "optimizer_latency_ms",
            observed=evidence.maximum_latency_ms,
            limit=float(gate.cost.maximum_optimizer_latency_ms.value),
            reason="no_optimizer_evidence",
        ),
    ]
    return _result(arm_key, tuple(conditions))


def _at_most(
    stage: GateStage,
    name: str,
    *,
    observed: float | None,
    limit: float,
    reason: str,
    worst_case: WorstCase | None = None,
) -> GateCondition:
    """観測値が上限以下なら `pass`。**観測できなければ `blocked`。**"""
    if observed is None:
        return GateCondition(
            stage=stage,
            name=name,
            outcome=GateOutcome.BLOCKED,
            limit=limit,
            reason=CountedReason(code=reason, count=1),
        )
    return GateCondition(
        stage=stage,
        name=name,
        outcome=GateOutcome.PASS if observed <= limit else GateOutcome.BLOCKED,
        limit=limit,
        observed=observed,
        worst_case=worst_case,
    )


def _at_least(
    stage: GateStage,
    name: str,
    *,
    observed: float | None,
    limit: float,
    reason: str,
    worst_case: WorstCase | None = None,
) -> GateCondition:
    """観測値が下限以上なら `pass`。**観測できなければ `blocked`。**"""
    if observed is None:
        return GateCondition(
            stage=stage,
            name=name,
            outcome=GateOutcome.BLOCKED,
            limit=limit,
            reason=CountedReason(code=reason, count=1),
        )
    return GateCondition(
        stage=stage,
        name=name,
        outcome=GateOutcome.PASS if observed >= limit else GateOutcome.BLOCKED,
        limit=limit,
        observed=observed,
        worst_case=worst_case,
    )


def _result(arm_key: str, conditions: tuple[GateCondition, ...]) -> GateResult:
    """段の順に並べ、**1つでも落ちれば `blocked`** にする。"""
    failed = tuple(
        condition for condition in conditions if condition.outcome is GateOutcome.BLOCKED
    )
    blocking = None if not failed else failed[0].stage
    return GateResult(
        arm_key=arm_key,
        outcome=GateOutcome.PASS if not failed else GateOutcome.BLOCKED,
        blocking_stage=blocking,
        conditions=conditions,
    )
