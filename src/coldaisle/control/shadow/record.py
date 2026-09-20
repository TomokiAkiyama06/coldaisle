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
from coldaisle.control.mpc.plan import ActionPlan
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
    ShadowActionPlan,
    ShadowCounterfactual,
    ShadowPlanStep,
    ShadowPredictedTarget,
    ShadowPrediction,
    ShadowRecord,
    SupervisorOutput,
    Zone,
)


def shadow_plan(plan: ActionPlan) -> ShadowActionPlan:
    """optimizer が採用した候補 action 列を、trace に残せる形へ写す。

    **形を変えない。** 同じ内容から同じ ``digest()`` が出なければ、記録した予測がどの候補に
    対するものかを後から確かめられない（決定記録 0052 §2.2 / 0053 §2.2）。
    """
    return ShadowActionPlan(
        step_ms=plan.step_ms,
        steps=tuple(
            ShadowPlanStep(offset_ms=step.offset_ms, demands=step.demands) for step in plan.steps
        ),
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

    **束縛が壊れていれば記録を作らず例外にする（fail closed）。** 食い違ったまま残した記録は、
    あとの評価（#91）と昇格の判断（#92）を誤らせる。記録は制御の入力ではないので、制御ループは
    この失敗で運転を止めない（#83 の配線。記録の失敗と制御の失敗を混ぜない）。
    記録側の構造上限は写し元の契約と同じ値にしてあるので、**設定として妥当な解がここで弾かれる
    ことはない**（決定記録 0053 §2.5。一致は試験で突き合わせる）。
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
            self._check_candidate(learned, selection)
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
    def _check_candidate(learned: MpcProposal, selection: ControllerSelection) -> None:
        """記録しようとしている結果が、**Gate が評価した worker 結果そのもの**か確かめる。

        推論の識別子では足りない（同じ入力・同じ予測から別の候補 demand を作れる）し、提案
        だけの識別子でも足りない（同じ提案のまま別の解＝別の予測を抱えられる）。記録側が書く
        値をすべて覆う ``MpcProposal.result_digest()`` で照らす（決定記録 0053 §2.2）。
        """
        if learned.proposal is None:
            if selection.candidate_digest is not None:
                raise ValueError("Gate は候補を評価しているのに、記録側は提案を持っていない")
            return
        if selection.candidate_digest is None:
            raise ValueError("Gate が評価した候補の識別子が無い提案は記録しない")
        if learned.result_digest() != selection.candidate_digest:
            raise ValueError("Gate が評価した候補と別の提案を counterfactual にしようとしている")

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
            plan=shadow_plan(solution.plan) if solved and solution is not None else None,
            prediction=(
                shadow_prediction(solution.prediction) if solved and solution is not None else None
            ),
            cost_total=solution.cost.total if solved and solution is not None else None,
            baseline_cost_total=(
                solution.baseline_cost.total if solved and solution is not None else None
            ),
        )
