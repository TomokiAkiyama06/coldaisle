"""Learned MPC controller（#86）。

1 tick の流れは4つ。

1. いま掛かっている action に対する **anchor 推論**（#84）
2. その推論に対する Confidence / OOD の判定（#85）
3. 制約の組み立てと optimizer の探索
4. **同じ推論に束ねた** ``ControllerProposal`` を作る

出せるのは requested までで、Reactive Guard（#80）と Critical Safety（#78）は後段で常に掛かる。
timeout・実行不能・モデル異常は例外にせず、``optimizer_status`` か ``LearnedFailure`` として
Gate（#79）へ渡し、Fallback で運転を続ける。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from coldaisle.control.acoustic import AcousticCostModel
from coldaisle.control.air_balance import AirBalanceModel, BalanceBand
from coldaisle.control.config import FanPolicyConfig, SafetyConfig
from coldaisle.control.fallback.gate import LearnedControlStatus, LearnedFailure, SnapshotStatus
from coldaisle.control.model.confidence import (
    ConfidenceAssessment,
    ConfidenceAssessor,
    ResidualEvidence,
)
from coldaisle.control.model.thermal import ObservedThermalInput, ThermalPrediction
from coldaisle.control.mpc.cost import MpcCostModel, MpcCostUnusableError, PlanCost
from coldaisle.control.mpc.counterfactual import MpcModelBinding, MpcModelUnusableError
from coldaisle.control.mpc.optimizer import LearnedMpcOptimizer, MpcSolution
from coldaisle.control.mpc.plan import HardConstraintSet
from coldaisle.control.schema import (
    ControllerKind,
    ControllerProposal,
    Demand,
    OptimizerStatus,
    PerZone,
    Reason,
    SupervisorOutput,
    Zone,
    ZoneRequest,
)
from coldaisle.control.state import ControlStateSnapshot


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class MpcProposal(_Frozen):
    """worker 1回の結果。**提案とその判定を切り離せない形で持つ。**

    提案だけを取り出して別の判定と組み合わせられないよう、型の不変条件として
    ``inference_id`` と ``model_version`` の一致を要求する（#85 / 決定記録 0050 §2.5）。
    """

    proposal: ControllerProposal | None = None
    assessment: ConfidenceAssessment | None = None
    failure: LearnedFailure | None = None
    failure_reason: Reason | None = None
    solution: MpcSolution | None = None
    """採用した解。記録と評価（#90 / #91）のためで、demand の権限は持たない。"""

    @model_validator(mode="after")
    def _proposal_is_bound_to_its_own_assessment(self) -> Self:
        if (self.proposal is None) == (self.failure is None):
            raise ValueError("MPC の結果は提案か失敗のどちらか一方にする")
        if (self.proposal is None) != (self.assessment is None):
            # 判定の無い提案を Gate へ渡せば、そこで Fallback になるだけだが、
            # 「判定が付いていない提案」を worker が作れること自体を許さない。
            raise ValueError("Learned MPC の提案には必ずその推論の assessment を添える")
        if self.failure is not None:
            if self.failure_reason is None:
                raise ValueError("失敗には理由を残す")
            if self.solution is not None:
                raise ValueError("失敗した tick に解を残さない")
            return self
        if self.failure_reason is not None:
            # 提案のある tick に失敗の理由だけが残ると、trace を読んだ側が取り違える。
            raise ValueError("失敗していない結果に failure_reason を付けない")
        assert self.proposal is not None and self.assessment is not None
        if self.proposal.controller is not ControllerKind.LEARNED_MPC:
            raise ValueError("MpcProposal には Learned MPC の提案だけを入れる")
        if self.proposal.inference_id != self.assessment.inference_id:
            raise ValueError("提案と assessment が別の推論のもの")
        if self.proposal.model_version != self.assessment.model_version:
            raise ValueError("提案と assessment の model_version が一致しない")
        if (self.proposal.confidence, self.proposal.ood) != (
            self.assessment.confidence,
            self.assessment.ood,
        ):
            raise ValueError("提案の confidence / ood を assessment と一致させる")
        if self.solution is not None:
            if self.proposal.optimizer_status is not OptimizerStatus.OK:
                raise ValueError("解を持てるのは optimizer_status=ok の提案だけ")
            if self.solution.anchor_inference_id != self.proposal.inference_id:
                raise ValueError("解が提案と別の anchor 推論に属している")
            if self.solution.requested != _demands(self.proposal):
                raise ValueError("提案の requested が解の最初の step と一致しない")
        elif self.proposal.optimizer_status is OptimizerStatus.OK:
            raise ValueError("optimizer_status=ok の提案には解が要る")
        return self

    @property
    def cost(self) -> PlanCost | None:
        """採用した解の総合コスト。"""
        return None if self.solution is None else self.solution.cost

    def to_status(
        self,
        *,
        received_at_mono_ms: int,
        snapshot_status: SnapshotStatus = SnapshotStatus.AVAILABLE,
        supervisor_available: bool = True,
        control_deadline_exceeded: bool = False,
    ) -> LearnedControlStatus:
        """Gate（#79）へ渡す状態に変換する。

        受信の単調時刻は control loop 自身の時計で、worker は決めない（0028 §2.6）。
        """
        if self.proposal is None:
            return LearnedControlStatus(
                failure=self.failure,
                # **理由をここで落とさない。** Gate が trace へ書く理由が
                # `model_load_failure` だけになると、何が起きたのか後から読めない。
                failure_reason=self.failure_reason,
                supervisor_available=supervisor_available,
                control_deadline_exceeded=control_deadline_exceeded,
                snapshot_status=snapshot_status,
            )
        return LearnedControlStatus(
            proposal=self.proposal,
            received_at_mono_ms=received_at_mono_ms,
            assessment=self.assessment,
            supervisor_available=supervisor_available,
            control_deadline_exceeded=control_deadline_exceeded,
            snapshot_status=snapshot_status,
        )


def _applied_demands(observed: ObservedThermalInput) -> PerZone[Demand]:
    """いま実際に掛かっている effective demand（#84 の観測 window の action）。"""
    return PerZone[Demand](
        front=observed.action.front.effective_demand,
        rear=observed.action.rear.effective_demand,
        top=observed.action.top.effective_demand,
    )


def _demands(proposal: ControllerProposal) -> PerZone[Demand]:
    return PerZone[Demand](
        front=proposal.requested.front.demand,
        rear=proposal.requested.rear.demand,
        top=proposal.requested.top.demand,
    )


class LearnedMpcController:
    """1 tick 分の Learned MPC 提案を作る worker。

    **Snapshot を自分で取り直さない。** 観測 window は同じ tick の入力から作られたものを
    受け取り、action 時刻が Snapshot と揃っていることを確かめる（#102 / #86 の入力）。
    """

    def __init__(
        self,
        binding: MpcModelBinding,
        policy: FanPolicyConfig,
        safety: SafetyConfig,
        *,
        assessor: ConfidenceAssessor,
        monotonic_ms: Callable[[], int],
        acoustic: AcousticCostModel | None = None,
        air_balance: AirBalanceModel | None = None,
        balance_band: BalanceBand | None = None,
    ) -> None:
        """設定とモデルが噛み合わなければ ``MpcModelUnusableError`` で**生成時に**失敗する。

        tick ごとに失敗させない。呼び出し側（runtime）はこれを
        ``LearnedFailure.MODEL_LOAD_FAILURE`` として Gate へ渡し、Fallback で運転を続ける。
        """
        self._check_binding_matches_policy(binding, policy, assessor)
        # **目的関数を外から受け取らない。** 別の設定で作った cost model を渡されると、
        # 重みや基準量だけが運転設定とずれる。任意依存（#94 / #81）だけを受け取る。
        cost_model = MpcCostModel(
            policy.mpc.optimizer,
            acoustic=acoustic,
            air_balance=air_balance,
            balance_band=balance_band,
        )
        self._binding = binding
        self._policy = policy
        self._safety = safety
        self._assessor = assessor
        self._monotonic_ms = monotonic_ms
        self._optimizer = LearnedMpcOptimizer(
            binding,
            policy.mpc.optimizer,
            cost_model,
            budget_ms=policy.mpc.budget_ms,
            monotonic_ms=monotonic_ms,
        )

    @staticmethod
    def _check_binding_matches_policy(
        binding: MpcModelBinding,
        policy: FanPolicyConfig,
        assessor: ConfidenceAssessor,
    ) -> None:
        """束縛・判定器・運転設定が**同じ前提で作られているか**を生成時に確かめる。

        束縛を検証したときの authority stage と、いま動かす policy の stage が違うと、
        Registry が SHADOW だけを許した artifact が FULL の経路へ入ってしまう。
        `MpcModelBinding.authority_stage` が誰にも読まれないままだと、この食い違いは
        「復帰 hold のあとに authority を得る」形で表に出る。
        """
        if binding.authority_stage is not policy.authority_stage:
            raise MpcModelUnusableError(
                "束縛時と運転中の authority stage が違う"
                f"（binding={binding.authority_stage.value}; "
                f"policy={policy.authority_stage.value}）"
            )
        if assessor.policy != policy.model_confidence:
            # 別の設定で作った判定器を渡されると、閾値だけがすり替わる。
            raise MpcModelUnusableError("Confidence 判定器が runtime と別の設定で作られている")
        attestation = binding.attestation
        profile = assessor.profile.binding
        mismatches = [
            name
            for name, attested, fitted in (
                ("model_id", attestation.model_id, profile.model_id),
                ("model_version", attestation.version, profile.model_version),
                ("artifact_sha256", attestation.artifact_sha256, profile.artifact_sha256),
            )
            if attested != fitted
        ]
        if mismatches:
            # Profile は特定の artifact に対して作る。別の artifact のものを使うと、
            # 学習範囲も residual の基準も違う値で confidence を出してしまう。
            raise MpcModelUnusableError(
                f"Confidence Profile が束縛した artifact のものではない: {','.join(mismatches)}"
            )

    def propose(
        self,
        *,
        snapshot: ControlStateSnapshot,
        observed: ObservedThermalInput,
        supervisor: SupervisorOutput,
        baseline: ControllerProposal,
        safety_floor: PerZone[Demand],
        residual: ResidualEvidence | None = None,
    ) -> MpcProposal:
        """この tick の提案を作る。**失敗しても例外を外へ出さない。**

        ここが worker の境界である。推論・判定・最適化・依存 model のどれが何を投げても、
        制御ループを落とさずに Fallback へ渡す（AGENTS.md ルール4）。取り込みループと同じく、
        ここだけは例外を握らずに**構造化した失敗へ翻訳して**記録する。
        ``KeyboardInterrupt`` / ``SystemExit`` などの ``BaseException`` は素通しする。
        """
        started_ms = self._monotonic_ms()
        try:
            if baseline.controller is not ControllerKind.FALLBACK:
                raise ValueError("baseline には Fallback の提案を渡す")
            return self._propose(
                snapshot=snapshot,
                observed=observed,
                supervisor=supervisor,
                baseline=baseline,
                safety_floor=safety_floor,
                residual=residual,
                started_ms=started_ms,
            )
        except MpcModelUnusableError as error:
            return self._failed(LearnedFailure.MODEL_LOAD_FAILURE, "model_unusable", str(error))
        except Exception as error:
            # 想定した ValueError 系だけを捕まえると、例えば #84 が内部の feature layout 異常に
            # 使う RuntimeError が素通りして制御ループごと死ぬ。種類で選ばず、翻訳する。
            return self._failed(
                LearnedFailure.OPTIMIZER_EXCEPTION, type(error).__name__, str(error)
            )

    def _propose(
        self,
        *,
        snapshot: ControlStateSnapshot,
        observed: ObservedThermalInput,
        supervisor: SupervisorOutput,
        baseline: ControllerProposal,
        safety_floor: PerZone[Demand],
        residual: ResidualEvidence | None,
        started_ms: int,
    ) -> MpcProposal:
        if observed.action_ts_ms != snapshot.ts_ms:
            raise ValueError(
                "観測 window の action 時刻が Snapshot と違う"
                f"（window={observed.action_ts_ms}; snapshot={snapshot.ts_ms}）"
            )
        anchor = self._binding.model.predict(observed)
        self._check_anchor(anchor, observed)
        assessment = self._assessor.assess(observed, anchor, residual)
        constraints = HardConstraintSet.build(
            optimizer=self._policy.mpc.optimizer,
            safety=self._safety,
            safety_floor=safety_floor,
            # **変化幅の起点は観測 window の action から取る。** 呼び出し側から別に受け取ると、
            # anchor が見ている action と違う値で rate limit と変化コストを計算できてしまう。
            current=_applied_demands(observed),
        )
        outcome = self._optimizer.solve(
            observed=observed,
            anchor=anchor,
            anchor_inference_id=assessment.inference_id,
            constraints=constraints,
            weights=supervisor.weights,
            target_band=supervisor.target_band,
            baseline=_demands(baseline),
            # anchor 推論と Confidence 判定に使った時間も予算に含める。
            budget_started_mono_ms=started_ms,
        )
        if outcome.solution is None:
            # timeout / 実行不能でも、anchor 推論とその判定は残っている。理由の付いた提案を
            # 作って Gate に判断させる（trace に「なぜ使わなかったか」を残すため）。
            requested = self._requests(_demands(baseline), outcome.reason)
        else:
            requested = self._requests(outcome.solution.requested, outcome.reason)
        proposal = ControllerProposal(
            controller=ControllerKind.LEARNED_MPC,
            seq=snapshot.tick_id,
            computed_at_ms=snapshot.ts_ms,
            requested=requested,
            model_version=self._binding.model_version,
            confidence=assessment.confidence,
            ood=assessment.ood,
            optimizer_status=outcome.status,
            latency_ms=max(0, self._monotonic_ms() - started_ms),
            inference_id=assessment.inference_id,
        )
        # apply_to は Registry 検証済みでない判定を弾く（決定記録 0048 §2.4）。
        # 値は既に入れてあるが、束縛の検査をここで必ず通す。
        proposal = assessment.apply_to(proposal)
        return MpcProposal(
            proposal=proposal,
            assessment=assessment,
            solution=outcome.solution,
        )

    def _check_anchor(self, anchor: ThermalPrediction, observed: ObservedThermalInput) -> None:
        """anchor 推論が、束縛した artifact とこの tick の入力に属しているか確かめる。

        #85 は予測を Profile の binding と照合するが、**Registry の証拠との一致は見ない**。
        ここで見ないと、別の model が返した予測に今の tick の判定を付けてしまう。
        """
        attestation = self._binding.attestation
        mismatches = [
            name
            for name, expected, actual in (
                ("model_id", attestation.model_id, anchor.model_id),
                ("model_version", attestation.version, anchor.model_version),
                # **中身の hash まで見る。** ID と版が同じでも別の bytes へ委譲していれば、
                # Registry が承認していない artifact が production の証拠の下で提案を出せる。
                ("artifact_sha256", attestation.artifact_sha256, anchor.artifact_sha256),
                ("input_action_ts_ms", observed.action_ts_ms, anchor.input_action_ts_ms),
            )
            if expected != actual
        ]
        if mismatches:
            raise MpcCostUnusableError(
                f"anchor 推論が束縛した artifact / 入力と一致しない: {','.join(mismatches)}"
            )

    @staticmethod
    def _requests(demands: PerZone[Demand], reason: Reason) -> PerZone[ZoneRequest]:
        requests = {
            zone.value: ZoneRequest(demand=demands.get(zone), reason=reason) for zone in Zone
        }
        return PerZone[ZoneRequest](**requests)

    @staticmethod
    def _failed(failure: LearnedFailure, code: str, detail: str) -> MpcProposal:
        return MpcProposal(
            failure=failure,
            failure_reason=Reason(code=_reason_code(code), detail=detail[:500]),
        )


def _reason_code(name: str) -> str:
    """例外名などを ``Reason.code`` の形（小文字と下線）へ落とす。"""
    lowered = "".join(
        f"_{character.lower()}" if character.isupper() else character for character in name
    ).strip("_")
    cleaned = "".join(
        character if character.isalnum() or character == "_" else "_" for character in lowered
    ).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"mpc_{cleaned}" if cleaned else "mpc_failure"
    return cleaned[:64]
