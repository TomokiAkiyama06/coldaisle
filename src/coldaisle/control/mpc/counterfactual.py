"""Learned MPC の内部モデル契約（#86）。

#84 の ``ThermalModel`` は**観測の再生**だけを主張する。Dataset v1 は anchor action から
label 時刻までの後続 Fan action 列を持たないため、決定記録 0048 §2.1 は
「anchor action を任意の candidate action へ変えた反実仮想予測や、#86 optimizer の内部 model
として使えるとは宣言しない」と明記している。

そこで MPC は内部モデルに ``InferenceCapability.COUNTERFACTUAL_ACTION`` を要求する。
現行の artifact は manifest の capability が ``observational_replay`` に固定されているため、
``MpcModelBinding.for_control`` は**いまあるすべての artifact を決定論的に拒む**。
拒否は例外的な事態ではなく通常経路で、runtime はそれを
``LearnedFailure.MODEL_LOAD_FAILURE`` として Gate（#79 / #85）へ渡し、Fallback で走り続ける。
"""

from __future__ import annotations

from typing import Annotated, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.model.thermal import (
    MAX_TARGET_HORIZONS,
    MAX_TARGET_METRICS,
    ArtifactVerification,
    InferenceCapability,
    ObservedThermalInput,
    ThermalFeatureSchema,
    ThermalModelManifest,
    ThermalPrediction,
    ThermalTargetSchema,
)
from coldaisle.control.model_registry import ArtifactAttestation, ArtifactKind
from coldaisle.control.mpc.plan import ActionPlan
from coldaisle.control.schema import AuthorityStage

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
TimestampMs = Annotated[int, Field(ge=0)]
PositiveDurationMs = Annotated[int, Field(gt=0)]

COUNTERFACTUAL_CAPABILITIES: frozenset[InferenceCapability] = frozenset(
    {InferenceCapability.COUNTERFACTUAL_ACTION}
)
"""MPC の内部モデルにできる capability。

``observational_replay`` は入っていない。**現行の artifact はすべてそちら**なので、後続
action 列を持つ Dataset 版と実測評価が揃うまで、active 制御に使える内部モデルは存在しない。
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
    """#104 の metadata がまだ持たない唯一の項目（決定記録 0052 §2.1 / §5）。

    現行の artifact 形式は ``observational_replay`` しか表現できないため、いまは自称でも
    active 制御へ届かない。反実仮想 artifact を #84 が定義するときに、#104 の metadata へ
    capability を持たせ、ここも attestation 側から取る。
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
        if self.capability not in COUNTERFACTUAL_CAPABILITIES:
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


class CounterfactualThermalModel(Protocol):
    """MPC が内部モデルとして受け入れる読み取り専用の契約。

    #84 の ``ThermalModel`` に ``identity`` と ``predict_plan`` を足したもので、
    Fan Demand も PWM も返さない。Reactive Guard / Critical Safety / Hardware を呼ばない。
    """

    @property
    def identity(self) -> CounterfactualModelIdentity:
        """束縛の判断に使う model identity を返す。"""
        ...

    @property
    def feature_schema(self) -> ThermalFeatureSchema:
        """入力の順序付き契約を返す。"""
        ...

    @property
    def target_schema(self) -> ThermalTargetSchema:
        """出力の順序付き契約を返す。"""
        ...

    def predict(self, observed: ObservedThermalInput) -> ThermalPrediction:
        """いま掛かっている action に対する観測予測を返す（anchor 推論）。"""
        ...

    def predict_plan(self, planned: PlannedThermalInput) -> PlanPrediction:
        """候補 action 列に対する将来観測を返す。制御・hardware の状態は変えない。"""
        ...


class MpcModelUnusableError(RuntimeError):
    """内部モデルとして使えない model を MPC に渡した。

    runtime はこれを ``LearnedFailure.MODEL_LOAD_FAILURE`` として Gate へ渡し、Fallback を続ける。
    """


class MpcModelBinding:
    """Registry の証拠で裏づけた内部モデルと、それを使ってよい authority の束（#86）。

    optimizer と controller は **この型を通してしか** モデルに触れない。生成時に一度だけ
    検査し、以後 tick ごとに条件が変わらないようにする。

    **verification / authority / 版・schema は ``ArtifactAttestation`` から取る。**
    モデル objectの自称値は照合のためだけに読み、食い違えば束縛しない。
    """

    __slots__ = ("_attestation", "_authority_stage", "_identity", "_model")
    _model: CounterfactualThermalModel
    _identity: CounterfactualModelIdentity
    _attestation: ArtifactAttestation
    _authority_stage: AuthorityStage

    def __init__(self) -> None:
        raise TypeError("MpcModelBinding は for_control からだけ作る")

    @classmethod
    def for_control(
        cls,
        model: CounterfactualThermalModel,
        *,
        attestation: ArtifactAttestation,
        authority_stage: AuthorityStage,
        expected_model_version: str,
    ) -> MpcModelBinding:
        """制御へ提案を出すための束を作る。条件を1つでも欠けば拒む。

        ``attestation`` は Model Registry（#104）の検証経路だけが発行する。呼び出し側が
        作れないため、**検証していない artifact を取り違えて渡す配線ミスは型で止まる。**
        同一プロセス内の悪意ある偽造までは防げない（決定記録 0050 §3 / 0052 §2.1）。

        **暗黙の降格はしない。** 条件を満たせないモデルは「弱い権限で使う」のではなく使わない。
        """
        # model_copy(update=...) は検証を通らないため、ここで必ず検証し直す。
        identity = CounterfactualModelIdentity.model_validate(
            model.identity.model_dump(mode="python")
        )
        if attestation.kind is not ArtifactKind.THERMAL_MODEL:
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
        if identity.capability not in COUNTERFACTUAL_CAPABILITIES:
            raise MpcModelUnusableError(
                "反実仮想予測を主張しない model を MPC の内部モデルにしない"
                f"（capability={identity.capability.value}。決定記録 0048 §2.1）"
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
        cls._check_model_matches_attestation(model, identity, attestation)
        binding = object.__new__(cls)
        object.__setattr__(binding, "_model", model)
        object.__setattr__(binding, "_identity", identity)
        object.__setattr__(binding, "_attestation", attestation)
        object.__setattr__(binding, "_authority_stage", authority_stage)
        return binding

    @staticmethod
    def _check_model_matches_attestation(
        model: CounterfactualThermalModel,
        identity: CounterfactualModelIdentity,
        attestation: ArtifactAttestation,
    ) -> None:
        """モデル object が、検証された artifact そのものを表しているか確かめる。

        別の artifact を包んだ wrapper が attestation だけを借りて authority を得ないように、
        model ID・版・feature / target schema version を突き合わせる。
        """
        mismatches = [
            name
            for name, attested, declared in (
                ("model_id", attestation.model_id, identity.model_id),
                ("model_version", attestation.version, identity.model_version),
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
    def model(self) -> CounterfactualThermalModel:
        """検証済みの内部モデル。"""
        return self._model

    @property
    def identity(self) -> CounterfactualModelIdentity:
        """束縛したときに検証した、モデル自身の申告。"""
        return self._identity

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

    def trace_metadata(self) -> dict[str, object]:
        """#82 の decision trace へ載せられる、束縛の出どころ。"""
        return {"mpc_model_binding": self._attestation.trace_metadata()}
