"""Learned MPC の内部モデル契約（#86）。

MPC は内部モデルに ``InferenceCapability.COUNTERFACTUAL_ACTION`` を要求する。決定記録 0079 §2.9 の
**段 4** から、束縛は反実仮想 Thermal Model artifact v2 の封をした型
``RegistryCounterfactualThermalModel``（0079 §2.4）だけを受け取る。``MpcModelBinding`` は
Registry が発行した ``VerifiedArtifact`` から :meth:`MpcModelBinding.from_verified_artifact` の
1つの呼び出しで作り、model と同梱 Confidence Profile v2 は同じ bytes から組み立てる。

- v1 artifact（``observational_replay``）は loader の L4 と attestation の capability で拒まれる
  （0079 §2.2。v1 を v2 として読み替えない）
- 候補 plan は action schema の格子と**完全に一致**するものだけを予測する（0079 §2.3。補間・外挿・
  丸めをしない）。``ActionPlan.steps[k]`` は Dataset v2 / artifact v2 の step ``k`` と同じ区間である
  （0087 §2.5）
- 候補 plan と Fallback の requested は、同梱 Profile v2 の step ごとの support で照らす
  （0079 §2.5 / 0084 §2.2。:meth:`MpcModelBinding.plan_support_violation`）

検査に外れた artifact は ``MpcModelUnusableError`` になる。拒否は例外的な事態ではなく通常経路で、
runtime はそれを ``LearnedFailure.MODEL_LOAD_FAILURE`` として Gate（#79 / #85）へ渡し、
Fallback で走り続ける（0079 §2.6）。
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.model.confidence import inference_id
from coldaisle.control.model.counterfactual import (
    ActionTrajectory,
    CounterfactualArtifactRejectedError,
    RegistryCounterfactualThermalModel,
    ThermalActionSchema,
)
from coldaisle.control.model.counterfactual_confidence import (
    StepSupportChecker,
    StepSupportViolation,
)
from coldaisle.control.model.thermal import (
    MAX_TARGET_HORIZONS,
    MAX_TARGET_METRICS,
    ArtifactVerification,
    InferenceCapability,
    ObservedThermalInput,
    ThermalModelManifest,
    ThermalPrediction,
    ThermalTargetSchema,
)
from coldaisle.control.model_registry import (
    ArtifactAttestation,
    ArtifactCapability,
    ArtifactKind,
    VerifiedArtifact,
)
from coldaisle.control.mpc.plan import ActionPlan
from coldaisle.control.schema import AuthorityStage, PerZone
from coldaisle.metrics import MetricCatalog

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
TimestampMs = Annotated[int, Field(ge=0)]
PositiveDurationMs = Annotated[int, Field(gt=0)]

COUNTERFACTUAL_CAPABILITIES: frozenset[ArtifactCapability] = frozenset(
    {ArtifactCapability.COUNTERFACTUAL_ACTION}
)
"""MPC の内部モデルにできる、**Registry へ登録時に申告された** capability。

``observational_replay`` は入っていない。v1 artifact はすべてそちらで、MPC に束縛できるのは
Dataset v2 から学習した反実仮想 artifact v2 だけ（決定記録 0079 §2.2 / §2.3）。
"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CounterfactualModelIdentity(_Frozen):
    """内部モデルが自分について主張する事実。

    **これは自称であって証拠ではない。** ``MpcModelBinding.for_control`` は、Registry（#104）が
    発行した ``ArtifactAttestation`` と突き合わせ、食い違えば束縛しない。
    verification / authority / model 版は attestation 側を正とし、自称値は照合にだけ使う。
    """

    model_id: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    capability: InferenceCapability
    """モデル自身の申告。**可否の判断には使わない。**

    束縛は Registry が発行した attestation の capability だけを見る。この値は
    「自称が attested と食い違っていないか」の照合にだけ使う（決定記録 0052 §2.1）。
    """

    @classmethod
    def from_manifest(cls, manifest: ThermalModelManifest) -> CounterfactualModelIdentity:
        """#84 の manifest をそのまま写す。**capability を書き換えない。**

        v1 artifact は必ず ``observational_replay`` になるので、この identity で
        ``for_control`` を呼ぶと拒否される。それが正しい振る舞いである。
        """
        return cls(
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            capability=manifest.capability,
        )


class PlannedThermalInput(_Frozen):
    """観測 window と、これから採る候補 action 列。

    ``observed`` の window は action 時刻で終わり、その action は**いま実際に掛かっている**
    effective demand である（#84）。``plan`` はその後の反実仮想。
    """

    observed: ObservedThermalInput
    plan: ActionPlan


class PlannedTarget(_Frozen):
    """1 control step の予測値。"""

    offset_ms: PositiveDurationMs
    expected_ts_ms: TimestampMs
    values: dict[str, FiniteFloat] = Field(min_length=1, max_length=MAX_TARGET_METRICS)


class PlanPrediction(_Frozen):
    """候補 action 列に対する読み取り専用の予測。**Demand も authority も返さない。**"""

    model_id: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    artifact_sha256: Sha256
    artifact_verification: ArtifactVerification
    capability: InferenceCapability
    anchor_inference_id: Sha256
    """この予測が属する anchor 推論（入力と予測）の識別子（#85）。

    Confidence / OOD の判定は anchor 推論に対して行う。別の推論の判定を借りて authority を
    得ないよう、optimizer は最後まで同じ識別子で束ねる。
    """
    input_action_ts_ms: TimestampMs
    plan_digest: Sha256
    """予測した候補 plan の識別子（``ActionPlan.digest()``）。

    **どの候補に対する予測かを、時刻ではなく plan そのもので突き合わせる。** 同じ tick の候補は
    step の刻みが同じなので、offset だけでは別の候補（別の demand）の予測を見分けられない。
    取り違えたまま採点すると、別の温度予測に対して今の音響・風量・変化コストを足してしまう。
    """
    targets: Annotated[
        tuple[PlannedTarget, ...], Field(min_length=1, max_length=MAX_TARGET_HORIZONS)
    ]

    @model_validator(mode="after")
    def _targets_follow_the_plan(self) -> Self:
        if self.capability is not InferenceCapability.COUNTERFACTUAL_ACTION:
            raise ValueError("反実仮想を主張しない capability で plan prediction を作らない")
        offsets = tuple(target.offset_ms for target in self.targets)
        if tuple(sorted(set(offsets))) != offsets:
            raise ValueError("plan prediction の offset は重複なし昇順にする")
        if any(
            target.expected_ts_ms != self.input_action_ts_ms + target.offset_ms
            for target in self.targets
        ):
            raise ValueError("plan prediction の時刻は action 時刻 + offset にする")
        return self

    def matches(self, plan: ActionPlan) -> bool:
        """予測がこの候補 plan そのものに対応しているか返す。

        step の刻みだけでなく **plan の識別子**（zone ごとの demand を含む）を突き合わせる。
        """
        if self.plan_digest != plan.digest():
            return False
        return tuple(target.offset_ms for target in self.targets) == plan.offsets_ms


class MpcModelUnusableError(RuntimeError):
    """内部モデルとして使えない model を MPC に渡した。

    runtime はこれを ``LearnedFailure.MODEL_LOAD_FAILURE`` として Gate へ渡し、Fallback を続ける。
    """


class MpcModelBinding:
    """Registry の証拠で裏づけた反実仮想 artifact v2 と、それを使ってよい authority の束（#86）。

    optimizer と controller は **この型を通してしか** モデルに触れない。生成時に一度だけ
    検査し、以後 tick ごとに条件が変わらないようにする。

    **作れるのは :meth:`from_verified_artifact` だけ**（決定記録 0079 §2.4 / §2.9 の段 4）。
    Registry が発行した ``VerifiedArtifact`` を1つ受け取り、同じ bytes から封をした型
    ``RegistryCounterfactualThermalModel``（model と同梱 Profile v2）と、step ごとの support の
    照合（0084 §2.2）を作る。``VerifiedArtifact`` と別に組み立てた model / Profile を並べて
    受け取る経路は持たない。

    **verification / authority / 版・schema は ``ArtifactAttestation`` から取る。**
    """

    __slots__ = ("_attestation", "_authority_stage", "_checker", "_model")
    _model: RegistryCounterfactualThermalModel
    _attestation: ArtifactAttestation
    _authority_stage: AuthorityStage
    _checker: StepSupportChecker

    def __init__(self) -> None:
        raise TypeError("MpcModelBinding は from_verified_artifact からだけ作る")

    @classmethod
    def from_verified_artifact(
        cls,
        verified: VerifiedArtifact,
        *,
        metric_catalog: MetricCatalog,
        calibration: RuntimeCalibration,
        authority_stage: AuthorityStage,
        expected_model_version: str,
    ) -> MpcModelBinding:
        """``VerifiedArtifact`` から制御へ提案を出すための束を作る。条件を1つでも欠けば拒む。

        読み込み時の検査（0079 §2.4 の L1〜L10、0084 の L11 / L12）は封をした型の loader が行う。
        ``metric_catalog`` は runtime の ``config/metrics.yaml``、``calibration`` は runtime の較正
        （起動時に1回だけ読んだ値。読めなければ ``RuntimeCalibration.unavailable``。0096 §5 #8）。
        どちらも既定値を持たない。検査に外れた artifact は ``MpcModelUnusableError`` になり、
        runtime はそれを ``MODEL_LOAD_FAILURE`` として Fallback にする（0079 §2.6）。

        ``authority_stage`` は**いま与えられている実効 stage**（#92 / 決定記録 0057 §2.2）で、
        設定の `authority_stage`（v9 からは上限）ではない。

        **暗黙の降格はしない。** 条件を満たせない artifact は「弱い権限で使う」「Profile なしで
        使う」のではなく使わない（0079 §2.6）。
        """
        try:
            model = RegistryCounterfactualThermalModel.from_verified_artifact(
                verified, metric_catalog=metric_catalog, calibration=calibration
            )
        except CounterfactualArtifactRejectedError as error:
            # **検査の番号を理由に残す。** 何に外れたのか（較正・単位・格子など）を後から読める。
            raise MpcModelUnusableError(
                f"反実仮想 artifact v2 の読み込み時の検査に外れた（{error.check.value}）: "
                f"{error.detail}"
            ) from error
        attestation = verified.attestation
        if attestation.kind is not ArtifactKind.THERMAL_MODEL:
            # L1 でも拒むが、束縛の条件として独立に見る（0052 §2.1 の表）。
            raise MpcModelUnusableError(
                f"thermal model 以外の artifact を MPC の内部モデルにしない"
                f"（kind={attestation.kind.value}）"
            )
        if not attestation.production_active:
            # 明示 version を固定した読み込み（Replay / offline 評価）の結果を、そのまま
            # 制御へ配線できないようにする。promotion には人の承認が要る（#104 / 0052 §2.1）。
            raise MpcModelUnusableError(
                "production pointer でない artifact に制御権を渡さない"
                f"（status={attestation.status.value}）"
            )
        if attestation.capability not in COUNTERFACTUAL_CAPABILITIES:
            # **登録時に申告された能力だけを見る。** L1 と同じ条件を束縛の側でも持つ。
            raise MpcModelUnusableError(
                "反実仮想予測を申告していない artifact を MPC の内部モデルにしない"
                f"（attested capability={attestation.capability.value}。決定記録 0048 §2.1）"
            )
        if authority_stage not in attestation.authority_compatibility:
            raise MpcModelUnusableError(
                "Registry が検証した artifact は要求 authority stage と互換でない"
                f"（stage={authority_stage.value}）"
            )
        if attestation.version != expected_model_version:
            # Gate も照合するが、版違いの提案を作る前に止める（無駄な推論と誤配を避ける）。
            raise MpcModelUnusableError(
                "内部モデルの版が runtime の期待と違う"
                f"（expected={expected_model_version}; attested={attestation.version}）"
            )
        cls._check_model_matches_attestation(model, attestation)
        binding = object.__new__(cls)
        object.__setattr__(binding, "_model", model)
        object.__setattr__(binding, "_attestation", attestation)
        object.__setattr__(binding, "_authority_stage", authority_stage)
        object.__setattr__(
            binding,
            "_checker",
            # 同梱 Profile からだけ作る。held の列・residual の基準と**同じ照合**（0084 §2.2）。
            StepSupportChecker(model.confidence_profile, model.action_schema),
        )
        return binding

    @staticmethod
    def _check_model_matches_attestation(
        model: RegistryCounterfactualThermalModel, attestation: ArtifactAttestation
    ) -> None:
        """封をした型が、attestation の示す artifact そのものから作られたかを確かめる。

        loader の L5 が同じ bytes から導いた metadata を照合済みだが、束縛の条件として
        独立にもう一度見る（0052 §2.1 の表を v2 でも同じ強さで残す）。
        """
        manifest = model.manifest
        mismatches = [
            name
            for name, attested, declared in (
                ("model_id", attestation.model_id, manifest.model_id),
                ("model_version", attestation.version, manifest.model_version),
                ("artifact_sha256", attestation.artifact_sha256, model.artifact_sha256),
                (
                    "feature_schema_version",
                    attestation.feature_schema_version,
                    model.feature_schema.schema_version,
                ),
                (
                    "target_schema_version",
                    attestation.target_schema_version,
                    model.target_schema.schema_version,
                ),
            )
            if attested != declared
        ]
        if mismatches:
            raise MpcModelUnusableError(
                f"model が検証済み artifact と一致しない: {','.join(mismatches)}"
            )

    def __setattr__(self, name: str, value: object) -> None:
        """束を後から差し替えられないようにする。"""
        raise AttributeError("MpcModelBinding は不変")

    @property
    def model(self) -> RegistryCounterfactualThermalModel:
        """検証済みの封をした型（model と同梱 Profile v2）。判定器はここから作る。"""
        return self._model

    @property
    def identity(self) -> CounterfactualModelIdentity:
        """束縛した artifact の manifest が申告する identity。"""
        manifest = self._model.manifest
        return CounterfactualModelIdentity(
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            capability=manifest.capability,
        )

    @property
    def target_schema(self) -> ThermalTargetSchema:
        """出力の順序付き契約。"""
        return self._model.target_schema

    @property
    def action_schema(self) -> ThermalActionSchema:
        """action の格子（``step_ms``・step 数・zone の順）。plan はこの格子と完全に一致させる。"""
        return self._model.action_schema

    @property
    def attestation(self) -> ArtifactAttestation:
        """Registry が発行した証拠。"""
        return self._attestation

    @property
    def authority_stage(self) -> AuthorityStage:
        """この束を検証したときの authority stage。"""
        return self._authority_stage

    @property
    def model_version(self) -> str:
        """提案に載せる model 版。**attestation 側の値を使う。**"""
        return self._attestation.version

    @property
    def artifact_sha256(self) -> str:
        """検証された artifact bytes の SHA-256。予測の出どころの照合に使う。"""
        return self._attestation.artifact_sha256

    @property
    def artifact_verification(self) -> ArtifactVerification:
        """Registry の証拠に裏づけられた検証状態。**自称値は使わない。**"""
        return ArtifactVerification.REGISTRY_VERIFIED

    @property
    def capability(self) -> ArtifactCapability:
        """Registry へ登録時に申告された能力。"""
        return self._attestation.capability

    def predict(self, observed: ObservedThermalInput) -> ThermalPrediction:
        """anchor 推論。計画 action の列は ``hold_effective``（0084 §2.1）で封をした型が作る。"""
        return self._model.predict(observed)

    def predict_plan(
        self, planned: PlannedThermalInput, *, anchor: ThermalPrediction
    ) -> PlanPrediction:
        """候補 plan に対する予測（0079 §2.3 / 0087 §2.5）。Demand も authority も返さない。

        plan の ``steps[k]`` は action schema の step ``k``
        （区間 ``[k × step_ms, (k + 1) × step_ms)``）の action で、その値が effective として
        掛かった仮定として model へ渡す（0079 §2.3）。
        **plan の格子が action schema と違えば予測しない**（補間・外挿・丸めをしない）。
        予測の horizon も plan の offset 列と完全に一致しなければならない。

        ``anchor`` はこの tick に :meth:`predict` で1回だけ作った anchor 推論で、
        ``anchor_inference_id`` はそこから作る。候補ごとに anchor 推論をやり直さない（予算を
        候補の評価に使う）。束縛した artifact とこの観測の anchor 推論でなければ予測しない。
        """
        plan = planned.plan
        trajectory = self._trajectory(plan)
        if (anchor.artifact_sha256, anchor.input_action_ts_ms) != (
            self._model.artifact_sha256,
            planned.observed.action_ts_ms,
        ):
            raise ValueError("anchor 推論が束縛した artifact とこの観測のものではない")
        prediction = self._model.predict_trajectory(planned.observed, trajectory)
        horizons = tuple(target.horizon_ms for target in prediction.targets)
        if horizons != plan.offsets_ms:
            raise ValueError(
                "予測の horizon が plan の offset 列と一致しない"
                f"（horizons={list(horizons)}; offsets={list(plan.offsets_ms)}）"
            )
        return PlanPrediction(
            model_id=prediction.model_id,
            model_version=prediction.model_version,
            artifact_sha256=prediction.artifact_sha256,
            artifact_verification=prediction.artifact_verification,
            capability=prediction.capability,
            anchor_inference_id=inference_id(planned.observed, anchor),
            input_action_ts_ms=prediction.input_action_ts_ms,
            plan_digest=plan.digest(),
            targets=tuple(
                PlannedTarget(
                    offset_ms=target.horizon_ms,
                    expected_ts_ms=target.expected_ts_ms,
                    values=dict(target.values),
                )
                for target in prediction.targets
            ),
        )

    def plan_support_violation(
        self, observed: ObservedThermalInput, plan: ActionPlan
    ) -> StepSupportViolation | None:
        """候補 plan を同梱 Profile v2 の step ごとの support に照らす（0079 §2.5 / 0084 §2.2）。

        anchor は観測 window の action（いま掛かっている effective demand）。外れた最初の点を返す。
        **margin も件数の下限も掛けない**（観測した値・cell・組の外は評価しない）。
        held の列（anchor 推論）と residual の基準の除外と同じ ``StepSupportChecker`` を使う。
        """
        anchor = PerZone[float](
            front=observed.action.front.effective_demand,
            rear=observed.action.rear.effective_demand,
            top=observed.action.top.effective_demand,
        )
        return self._checker.check(anchor, self._trajectory(plan))

    def _trajectory(self, plan: ActionPlan) -> ActionTrajectory:
        schema = self._model.action_schema
        if plan.step_ms != schema.step_ms or len(plan.steps) != schema.steps:
            raise ValueError(
                "plan の格子が action schema と一致しない（補間・外挿・丸めはしない）"
                f"（plan={plan.step_ms}ms×{len(plan.steps)}; "
                f"action={schema.step_ms}ms×{schema.steps}）"
            )
        return ActionTrajectory(
            step_ms=plan.step_ms, demands=tuple(step.demands for step in plan.steps)
        )

    def trace_metadata(self) -> dict[str, object]:
        """#82 の decision trace へ載せられる、束縛の出どころ。"""
        return {"mpc_model_binding": self._attestation.trace_metadata()}
