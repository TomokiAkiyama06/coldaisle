"""適用しなかった提案を counterfactual として記録する（#90 / 決定記録 0053）。

**この層は demand の権限を持たない。** 受け取るのは Gate（#79 / #85）が既に選び終えた結果と、
選ばれなかった提案だけで、どちらも変更しない。Reactive Guard・Critical Safety・Hardware Backend
を import しない（試験で確かめる）。

記録は3つの束縛の上に乗る。**新しい仕組みを作らない。**

1. 推論の識別子 ``inference_id``（#85）。予測も confidence もこれで束ねる
2. Registry の証拠に裏づけられた ``artifact_sha256``（#104 / 決定記録 0048）
3. 候補 plan の識別子 ``plan_digest``（決定記録 0052 §2.2）

confidence / ood は **Gate が裏づけた ``ModelGateDecision`` からだけ**取る。提案が自称した値を
記録すると、OOD の推論が後から HIGH に見える（0050 §2.5）。
"""

from __future__ import annotations

from coldaisle.control.config import ShadowConfig
from coldaisle.control.fallback.gate import ControllerSelection
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.mpc.counterfactual import PlanPrediction
from coldaisle.control.schema import (
    ControllerKind,
    ControllerProposal,
    ControlState,
    Demand,
    ModelGateDecision,
    OperatingMode,
    OptimizerStatus,
    PerZone,
    Reason,
    ShadowCounterfactual,
    ShadowPredictedTarget,
    ShadowPrediction,
    ShadowRecord,
    SupervisorOutput,
    Zone,
)


def shadow_prediction(prediction: PlanPrediction) -> ShadowPrediction:
    """optimizer が採用した候補 plan の予測を、trace に残せる形へ写す。

    **値を作り直さない。** 時刻は予測自身の ``input_action_ts_ms`` と offset から決まり、
    記録した時刻には依らない（0053 §2.3）。
    """
    return ShadowPrediction(
        model_id=prediction.model_id,
        model_version=prediction.model_version,
        artifact_sha256=prediction.artifact_sha256,
        inference_id=prediction.anchor_inference_id,
        plan_digest=prediction.plan_digest,
        input_action_ts_ms=prediction.input_action_ts_ms,
        targets=tuple(
            ShadowPredictedTarget(
                offset_ms=target.offset_ms,
                expected_ts_ms=target.expected_ts_ms,
                values=dict(target.values),
            )
            for target in prediction.targets
        ),
    )


def _demands(proposal: ControllerProposal) -> PerZone[Demand]:
    return PerZone[Demand](
        front=proposal.requested.front.demand,
        rear=proposal.requested.rear.demand,
        top=proposal.requested.top.demand,
    )


def _requested_reason(proposal: ControllerProposal) -> Reason:
    """3 zone の理由をまとめる。違う理由なら、まとめずに code を並べて残す。"""
    reasons = tuple(proposal.requested.get(zone).reason for zone in Zone)
    if all(reason == reasons[0] for reason in reasons[1:]):
        return reasons[0]
    return Reason(
        code="mixed_zone_reasons",
        detail="; ".join(f"{zone.value}={reasons[index].code}" for index, zone in enumerate(Zone)),
    )


class ShadowRecorder:
    """1 tick の counterfactual を組み立てる。**制御の状態を持たない純粋な記録器。**

    Gate が選んだ提案は counterfactual にしない。逆に counterfactual を選び直す API も無い。
    """

    def __init__(self, config: ShadowConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        """counterfactual を記録する設定か。"""
        return self._config.enabled

    def record(
        self,
        *,
        tick_id: int,
        ts_ms: int,
        state: ControlState,
        effective: PerZone[Demand],
        selection: ControllerSelection,
        baseline: ControllerProposal,
        learned: MpcProposal | None = None,
        supervisor: SupervisorOutput | None = None,
    ) -> ShadowRecord | None:
        """この tick の ``ShadowRecord`` を返す。記録するものが無ければ None。

        ``effective`` は Critical Safety が合成し終えた値をそのまま渡す。ここで作り直すと、
        比較の基準が実際に掛かった風量と別物になる。
        """
        if not self._config.enabled:
            return None
        if baseline.controller is not ControllerKind.FALLBACK:
            raise ValueError("baseline には Fallback の提案を渡す")
        if state.operating_mode in {OperatingMode.MANUAL, OperatingMode.CALIBRATION}:
            # 人が requested を決める mode には、比較すべき「適用した制御器」が無い。
            return None
        applied = state.active_controller
        if selection.active_controller is not applied:
            raise ValueError("ControllerSelection と ControlState の active controller が違う")

        counterfactuals: list[ShadowCounterfactual] = []
        if learned is not None and applied is not ControllerKind.LEARNED_MPC:
            counterfactuals.append(self._learned(learned, selection.model_gate))
        if applied is ControllerKind.LEARNED_MPC:
            # Learned MPC が active の tick では、Baseline のほうが counterfactual になる。
            counterfactuals.append(
                ShadowCounterfactual(
                    controller=ControllerKind.FALLBACK,
                    requested=_demands(baseline),
                    reason=_requested_reason(baseline),
                )
            )
        if not counterfactuals:
            return None
        return ShadowRecord(
            tick_id=tick_id,
            ts_ms=ts_ms,
            authority_stage=state.authority_stage,
            applied_controller=applied,
            applied_effective=effective,
            counterfactuals=tuple(counterfactuals),
            supervisor=supervisor,
        )

    @staticmethod
    def _learned(learned: MpcProposal, gate: ModelGateDecision | None) -> ShadowCounterfactual:
        """Learned MPC の worker 結果を counterfactual へ写す。**失敗も記録する。**"""
        if learned.proposal is None:
            assert learned.failure is not None and learned.failure_reason is not None
            detail = learned.failure_reason.detail
            return ShadowCounterfactual(
                controller=ControllerKind.LEARNED_MPC,
                failure=Reason(
                    code=learned.failure.value,
                    detail=(f"{learned.failure_reason.code}: {detail}" if detail else "")[:500],
                ),
            )
        proposal = learned.proposal
        assessment = learned.assessment
        assert assessment is not None and proposal.inference_id is not None
        assert proposal.model_version is not None
        solution = learned.solution
        solved = proposal.optimizer_status is OptimizerStatus.OK
        attested = gate is not None and gate.attested and gate.inference_id == proposal.inference_id
        return ShadowCounterfactual(
            controller=ControllerKind.LEARNED_MPC,
            requested=_demands(proposal),
            reason=_requested_reason(proposal),
            optimizer_status=proposal.optimizer_status,
            latency_ms=proposal.latency_ms,
            evaluations=None if solution is None else solution.evaluations,
            model_version=proposal.model_version,
            inference_id=proposal.inference_id,
            # 版も hash も、提案と同じ推論に束ねられた assessment から取る（#85 の束縛）。
            artifact_sha256=assessment.artifact_sha256,
            attested=attested,
            # **提案の自称値は使わない。** Gate が裏づけた値だけを残す。
            confidence=gate.confidence if attested and gate is not None else None,
            ood=gate.ood if attested and gate is not None else None,
            prediction=(
                shadow_prediction(solution.prediction) if solved and solution is not None else None
            ),
            cost_total=solution.cost.total if solved and solution is not None else None,
            baseline_cost_total=(
                solution.baseline_cost.total if solved and solution is not None else None
            ),
        )
