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

import logging
from collections.abc import Callable
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from coldaisle import logs
from coldaisle.control.acoustic import AcousticCostModel
from coldaisle.control.air_balance import AirBalanceModel, BalanceBand
from coldaisle.control.config import FanHardwareConfig, FanPolicyConfig, SafetyConfig
from coldaisle.control.fallback.gate import LearnedControlStatus, LearnedFailure, SnapshotStatus
from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.model.confidence import ConfidenceAssessment, ResidualEvidence
from coldaisle.control.model.counterfactual_confidence import CounterfactualConfidenceAssessor
from coldaisle.control.model.thermal import (
    ObservedThermalInput,
    ThermalPrediction,
    canonical_sha256,
)
from coldaisle.control.model_registry import VerifiedArtifact
from coldaisle.control.mpc.cost import MpcCostModel, MpcCostUnusableError, PlanCost
from coldaisle.control.mpc.counterfactual import MpcModelBinding, MpcModelUnusableError
from coldaisle.control.mpc.optimizer import LearnedMpcOptimizer, MpcSolution
from coldaisle.control.mpc.plan import HardConstraintSet
from coldaisle.control.schema import (
    AuthorityStage,
    AuthorityStageSource,
    ControllerKind,
    ControllerProposal,
    Demand,
    OptimizerStatus,
    PerZone,
    Reason,
    SupervisorOutput,
    Zone,
    ZoneRequest,
    stage_rank,
)
from coldaisle.control.state import ControlStateSnapshot
from coldaisle.metrics import MetricCatalog

LOGGER = logging.getLogger("coldaisle.control.mpc")


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
    binding_authority_stage: AuthorityStage | None = None
    """この結果を作った束縛が Registry に検証された authority stage（#92 / 0057 §2.2）。

    **識別子（`result_digest`）に覆われる。** worker の照合結果を Gate まで運び、
    worker が走ってから選ばれるまでの間に昇格が起きた提案を採らせない。
    """

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
        if self.binding_authority_stage is None:
            # 束縛の stage を持たない結果は、どの authority まで検証されたのか言えない。
            raise ValueError("Learned MPC の提案には束縛の authority stage を添える")
        return self

    def result_digest(self) -> str:
        """**この worker 結果そのもの**を表す SHA-256。

        提案だけでは足りない。同じ ``ControllerProposal`` と assessment を持ったまま、
        別の解（別の予測）を抱えた結果をいくつでも作れるので、提案の識別子だけで照らすと、
        Gate が見たのとは別の予測を「その判断に属する予測」として記録できてしまう
        （#90 / 決定記録 0053 §2.2）。

        canonical JSON はこの型の**全フィールド**（提案・assessment・解・失敗の理由）を含むので、
        記録側が書く値はすべてこの識別子に覆われる。
        """
        return canonical_sha256(self)

    @property
    def cost(self) -> PlanCost | None:
        """採用した解の総合コスト。"""
        return None if self.solution is None else self.solution.cost

    def to_status(
        self,
        *,
        received_at_mono_ms: int,
        source_snapshot_mono_ms: int | None,
        snapshot_status: SnapshotStatus = SnapshotStatus.AVAILABLE,
        supervisor_available: bool = True,
        control_deadline_exceeded: bool = False,
    ) -> LearnedControlStatus:
        """Gate（#79）へ渡す状態に変換する。

        受信の単調時刻は control loop 自身の時計で、worker は決めない（0028 §2.6）。
        元 snapshot の単調時刻も loop が自分の出した snapshot の記録から取る（0077 §2.4 の4）。
        提案の無い結果（失敗）では使わないので None でよい。提案があるのに None なら
        ``LearnedControlStatus`` の検証で落ちる。
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
            source_snapshot_mono_ms=source_snapshot_mono_ms,
            assessment=self.assessment,
            supervisor_available=supervisor_available,
            control_deadline_exceeded=control_deadline_exceeded,
            snapshot_status=snapshot_status,
            binding_authority_stage=self.binding_authority_stage,
            # Gate はこの識別子をそのまま選択結果へ残す。記録側（#90）は、自分が持っている
            # worker 結果を数え直して照らし、別の結果を記録しない。
            result_digest=self.result_digest(),
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
        monotonic_ms: Callable[[], int],
        authority: AuthorityStageSource,
        acoustic: AcousticCostModel | None = None,
        air_balance: AirBalanceModel | None = None,
        balance_band: BalanceBand | None = None,
        fan_hardware: FanHardwareConfig | None = None,
    ) -> None:
        """設定とモデルが噛み合わなければ ``MpcModelUnusableError`` で**生成時に**失敗する。

        Air Balance を渡すなら、同じ ``ControlConfig`` の ``fan_hardware`` も渡す
        （決定記録 0073 §2.3。欠ければ ``MpcCostUnusableError``）。

        tick ごとに失敗させない。呼び出し側（runtime）はこれを
        ``LearnedFailure.MODEL_LOAD_FAILURE`` として Gate へ渡し、Fallback で運転を続ける。

        ``authority`` は**いま与えている制御権**（#92 / 決定記録 0057 §2.2）。設定の
        ``authority_stage`` は v9 から**上限**なので、そちらとは照合しない。

        **Confidence 判定器は外から受け取らない**（決定記録 0079 §2.4）。束縛した封をした型の
        同梱 Profile v2 と、runtime の ``model_confidence`` から作る。別の Profile・別の設定の
        判定器を渡す引数を制御側に持たせない。
        """
        self._authority = authority
        self._check_binding_covers_authority(binding, authority.current_stage())
        try:
            assessor = CounterfactualConfidenceAssessor.for_model(
                binding.model, policy.model_confidence
            )
        except (TypeError, ValueError) as error:
            raise MpcModelUnusableError(
                f"同梱 Profile v2 から Confidence 判定器を作れない: {error}"
            ) from error
        # **目的関数を外から受け取らない。** 別の設定で作った cost model を渡されると、
        # 重みや基準量だけが運転設定とずれる。任意依存（#94 / #81）だけを受け取る。
        cost_model = MpcCostModel(
            policy.mpc.optimizer,
            acoustic=acoustic,
            air_balance=air_balance,
            balance_band=balance_band,
            fan_hardware=fan_hardware,
        )
        self._binding = binding
        self._policy = policy
        self._safety = safety
        self._assessor = assessor
        self._monotonic_ms = monotonic_ms
        # 任意依存を差し替えたことが offline 評価（#91 / #105）の条件へ出るように保持する。
        self._acoustic = acoustic
        self._air_balance = air_balance
        self._balance_band = balance_band
        self._fan_hardware = fan_hardware
        self._optimizer = LearnedMpcOptimizer(
            binding,
            policy.mpc.optimizer,
            cost_model,
            budget_ms=policy.mpc.budget_ms,
            monotonic_ms=monotonic_ms,
        )

    @property
    def binding(self) -> MpcModelBinding:
        """この controller が束縛した内部モデル（読み取り専用）。

        offline 評価（#105）が「どの artifact で回したか」を条件へ入れるために読む。
        束縛は不変で、ここから制御へ配線する経路は増えない。
        """
        return self._binding

    def conditions(self) -> dict[str, object]:
        """条件 hash へ載せる、この controller の**結果に効くすべて**。

        `expected_model_version` だけでは足りない。同じ版を名乗る別の artifact、別の
        Confidence Profile、別の任意依存（#94 / #81）は、どれも同じ入力から違う提案を作る。
        """
        return {
            "binding": self._binding.attestation.trace_metadata(),
            "authority_stage": self._binding.authority_stage.value,
            "confidence_profile_sha256": self._assessor.profile.sha256(),
            "model_confidence": self._assessor.policy.model_dump(mode="json"),
            "acoustic": (
                None if self._acoustic is None else _optional_metadata(self._acoustic.metadata)
            ),
            "air_balance": (
                None
                if self._air_balance is None
                else self._air_balance.metadata.model_dump(mode="json")
            ),
            "balance_band": (
                None if self._balance_band is None else self._balance_band.model_dump(mode="json")
            ),
            # balance の項は profile の写像を通した demand で評価する（決定記録 0073 §2.3）。
            # 写像が変われば同じ入力から違う提案になるので、条件に入れる。
            "balance_minimum_stable_demand": (
                None
                if self._fan_hardware is None or self._air_balance is None
                else {
                    zone.value: self._fan_hardware.zones.get(zone).profile.minimum_stable_demand
                    for zone in Zone
                }
            ),
        }

    @staticmethod
    def _check_binding_covers_authority(
        binding: MpcModelBinding,
        effective_stage: AuthorityStage,
    ) -> None:
        """**束縛が、いま与えている制御権を覆っているか**を確かめる（#92 / 0057 §2.2）。

        `MpcModelBinding.authority_stage` は Registry が「この stage で使ってよい」と
        検証した stage である（#104 の `authority_compatibility`）。それより**高い**
        実効 stage で動かすと、SHADOW だけを許された artifact が LIMITED 以上の経路へ入る。

        **低いぶんには通す。** 設定の上限（v9 の `authority_stage`）や journal で
        実効 stage が下がっているだけなら、与えている制御権は検証済みの範囲に収まる。
        ここを「一致」にすると、journal が SHADOW・上限が LIMITED の初回昇格で
        SHADOW 互換の artifact が拒まれ、**新しい設定での証拠を1件も集められなくなる**
        （codex #4056864031）。
        """
        if stage_rank(effective_stage) > stage_rank(binding.authority_stage):
            raise MpcModelUnusableError(
                "束縛が実効 authority stage を覆っていない"
                f"（binding={binding.authority_stage.value}; "
                f"effective={effective_stage.value}）"
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
        # 制御権は運転中に動く（#92）。生成時の照合だけだと、昇格のあとに作り直されなかった
        # worker が、束縛の覆っていない stage で提案を出し続ける。tick ごとに見る。
        self._check_binding_covers_authority(self._binding, self._authority.current_stage())
        anchor = self._binding.predict(observed)
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
            # **照合した stage を結果に結び付ける。** Gate が選ぶ瞬間まで運ぶ（#92）。
            binding_authority_stage=self._binding.authority_stage,
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


class LearnedMpcRuntime:
    """artifact の読み込みから毎 tick の提案までを1つにまとめた worker の本体（#86）。

    **読み込みに失敗しても例外で止まらない**（決定記録 0079 §2.6 / 0052 §2.1）。
    :meth:`load` は ``VerifiedArtifact`` を ``MpcModelBinding.from_verified_artifact`` と
    ``LearnedMpcController`` に通し、どこかで ``MpcModelUnusableError``（L1〜L12 の検査・
    較正の不一致や読めなかった較正・production でない・authority・版・格子・目的関数の metric）
    になれば（目的関数の任意依存の組み立ての ``MpcCostUnusableError`` も同じ）、
    以後の :meth:`propose` は毎回 ``LearnedFailure.MODEL_LOAD_FAILURE``
    （``failure_reason`` 付き）を返す。Gate はそれを Fallback にする。

    暗黙の降格はしない（Profile なしで使う・observational として使う経路を持たない）。
    MPC worker のプロセス（決定記録 0077 段階 3）はこの型を使う想定で、ここは worker の外で
    単体に試験できるようにした境界である。
    """

    __slots__ = ("_controller", "_failure_reason")

    def __init__(
        self,
        *,
        controller: LearnedMpcController | None,
        failure_reason: Reason | None,
    ) -> None:
        """:meth:`load` を使う。直接作るときも、どちらか一方だけを持たせる。"""
        if (controller is None) == (failure_reason is None):
            raise ValueError("LearnedMpcRuntime は controller か読み込みの失敗のどちらか一方")
        self._controller = controller
        self._failure_reason = failure_reason

    @classmethod
    def load(
        cls,
        verified: VerifiedArtifact,
        policy: FanPolicyConfig,
        safety: SafetyConfig,
        *,
        metric_catalog: MetricCatalog,
        calibration: RuntimeCalibration,
        expected_model_version: str,
        monotonic_ms: Callable[[], int],
        authority: AuthorityStageSource,
        acoustic: AcousticCostModel | None = None,
        air_balance: AirBalanceModel | None = None,
        balance_band: BalanceBand | None = None,
        fan_hardware: FanHardwareConfig | None = None,
    ) -> LearnedMpcRuntime:
        """artifact を束縛して controller を作る。使えなければ失敗を持った runtime を返す。

        ``calibration`` は runtime の較正（起動時に1回だけ読んだ値）。読めなかったときは
        ``RuntimeCalibration.unavailable`` を渡す。較正の掛かる metric を使う artifact は L9 で
        拒まれ、使わない（``null`` の）artifact だけが通る（決定記録 0096 §5 #8）。
        束縛の authority stage は**いまの実効 stage**（``authority.current_stage()``）。
        """
        try:
            binding = MpcModelBinding.from_verified_artifact(
                verified,
                metric_catalog=metric_catalog,
                calibration=calibration,
                authority_stage=authority.current_stage(),
                expected_model_version=expected_model_version,
            )
            controller = LearnedMpcController(
                binding,
                policy,
                safety,
                monotonic_ms=monotonic_ms,
                authority=authority,
                acoustic=acoustic,
                air_balance=air_balance,
                balance_band=balance_band,
                fan_hardware=fan_hardware,
            )
        except (MpcModelUnusableError, MpcCostUnusableError) as error:
            # 目的関数の任意依存の組み立ての失敗（Air Balance に profile が無いなど）も、
            # 起動を止めずに読み込みの失敗として Fallback にする（0079 §2.6 / AGENTS.md ルール4）。
            reason = Reason(code="model_unusable", detail=str(error)[:500])
            LOGGER.error(
                "Learned MPC の内部モデルを使えないため、提案を出さず Fallback にする",
                extra={
                    logs.FIELDS_KEY: {
                        "reason": "model_load_failure",
                        "detail": reason.detail,
                        "calibration_available": calibration.is_available,
                    }
                },
            )
            return cls(controller=None, failure_reason=reason)
        return cls(controller=controller, failure_reason=None)

    @property
    def controller(self) -> LearnedMpcController | None:
        """束縛できたときの controller。読み込みに失敗したときは None。"""
        return self._controller

    @property
    def failure_reason(self) -> Reason | None:
        """読み込みに失敗した理由。使えるときは None。"""
        return self._failure_reason

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
        """この tick の結果。読み込みに失敗していれば ``MODEL_LOAD_FAILURE`` を返す。"""
        if self._controller is None:
            assert self._failure_reason is not None
            return MpcProposal(
                failure=LearnedFailure.MODEL_LOAD_FAILURE,
                failure_reason=self._failure_reason,
            )
        return self._controller.propose(
            snapshot=snapshot,
            observed=observed,
            supervisor=supervisor,
            baseline=baseline,
            safety_floor=safety_floor,
            residual=residual,
        )


def _optional_metadata(metadata: BaseModel | None) -> dict[str, object] | None:
    """任意依存の出どころを条件 hash へ載せる形にする。"""
    return None if metadata is None else metadata.model_dump(mode="json")


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
