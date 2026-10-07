"""学習環境の Dynamics（#105 / 決定記録 0058 §2.2 / §2.3）。

環境が「次に何が観測されるか」を決める層で、3種類ある。**どれを使ったかは step ごとに残り、
episode の結果に必ず出る。**

| provenance | 出どころ | 昇格の根拠にできるか |
|---|---|---|
| `logged_trajectory` | 記録済みの運転（実測） | できる。ただし**記録と同じ action の区間だけ** |
| `registry_attested` | Registry が反実仮想を申告した artifact | できる。**該当 artifact は無い** |
| `simulated_provisional` | 設定した近似式 | **できない。** 実測に裏づけが無い |

`registry_attested` は反実仮想 Thermal Model artifact v2 の封をした型
`RegistryCounterfactualThermalModel`（決定記録 0079 §2.4）**だけ**を受け取る（0079 §2.9 の段 6）。
v1 artifact（`observational_replay`）は loader の L1 / L4 で型にならず、学習 dynamics にも
決定論的に束縛できない（0079 §2.2 / 0058 §2.3）。拒否は不具合ではなく、0052 と同じ規律である。

learned simulator は各 step で、同梱 Confidence Profile v2 による判定（anchor 推論の OOD と、
その step で掛けた action 列の step ごとの support。0050 / 0084）を **記録する**
（`LearnedStepAssessment`）。**記録だけで、遷移・採点・`promotable` の条件は変えない**
（0079 §2.9 段 6 / §5 #8）。

近似 simulator は `simulated_provisional` としか名乗れず、`DynamicsIdentity` の不変条件が
Registry の証拠（artifact hash）を持たせない。**「検証済みの学習 simulator」に見える道は無い。**
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from random import Random
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.config import ModelConfidencePolicy, ShadowConfig
from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.model.confidence import ComponentResult, ConfidenceComponent
from coldaisle.control.model.counterfactual import (
    ActionTrajectory,
    CounterfactualArtifactRejectedError,
    RegistryCounterfactualThermalModel,
    ThermalActionSchema,
)
from coldaisle.control.model.counterfactual_confidence import (
    CounterfactualConfidenceAssessor,
    StepSupportChecker,
    StepSupportViolation,
)
from coldaisle.control.model.thermal import (
    InferenceCapability,
    ObservedFanAction,
    ObservedThermalInput,
    ObservedWindowFrame,
    ThermalMetricName,
    canonical_sha256,
)
from coldaisle.control.model_registry import (
    ArtifactAttestation,
    ArtifactCapability,
    ArtifactKind,
    VerifiedArtifact,
)
from coldaisle.control.rl.config import MAX_EPISODE_STEPS, SimulatorConfig
from coldaisle.control.schema import Demand, PerZone, Reason, WorkloadRegime, Zone
from coldaisle.metrics import MetricCatalog

Sha256 = str


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TrainingMode(StrEnum):
    """何から遷移を作るか。**結果の型でも混ぜない**（決定記録 0058 §2.2）。

    **`EpisodeSpec` の欄ではなく dynamics が名乗る。** 呼び出し側が別に持つと、記録再生を
    `learned_simulator` と書いた結果を作れてしまう（0058 §2.2）。
    """

    LOGGED = "logged"
    """記録済み trajectory だけ。実測の裏づけがあるが、記録と同じ action しか採点できない。"""
    LEARNED_SIMULATOR = "learned_simulator"
    """learned / 近似 simulator。任意の action を試せるが、裏づけは simulator の妥当性まで。"""
    HYBRID = "hybrid"
    """記録で説明できる step は記録から、残りは simulator から。step ごとに出どころが残る。"""


class DynamicsProvenance(StrEnum):
    """1 step の遷移がどこから来たか。**混ぜて集計しない。**"""

    LOGGED_TRAJECTORY = "logged_trajectory"
    REGISTRY_ATTESTED = "registry_attested"
    SIMULATED_PROVISIONAL = "simulated_provisional"


class DynamicsUnusableError(RuntimeError):
    """環境の dynamics として使えないものを渡した。

    環境はこれを例外のまま外へ出さず、episode の終端理由へ翻訳する
    （AGENTS.md ルール4 と同じ扱い）。ただし**組み立て時の拒否は例外のまま**にする。
    """


class DynamicsIdentity(_Frozen):
    """この dynamics が何であるかを、証拠の有無まで含めて名指しする。

    **自称と証拠を取り違えられないようにする。** `registry_attested` を名乗るには Registry が
    発行した artifact hash が要り、近似 simulator はそれを持てない（決定記録 0058 §2.3）。
    """

    provenance: DynamicsProvenance
    model_id: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    artifact_sha256: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_sha256: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trace_digest: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _evidence_matches_the_claim(self) -> Self:
        if self.provenance is DynamicsProvenance.REGISTRY_ATTESTED:
            if self.artifact_sha256 is None:
                raise ValueError("registry_attested を名乗る dynamics には artifact hash が要る")
            if self.config_sha256 is not None or self.trace_digest is not None:
                raise ValueError("registry_attested の identity に設定 / trace の hash を混ぜない")
        elif self.provenance is DynamicsProvenance.SIMULATED_PROVISIONAL:
            if self.artifact_sha256 is not None:
                # ここを許すと、近似 simulator が Registry の証拠を持っているように読める。
                raise ValueError("近似 simulator に artifact hash を持たせない")
            if self.config_sha256 is None:
                raise ValueError("近似 simulator には設定 bytes の hash が要る")
        else:
            if self.artifact_sha256 is not None:
                raise ValueError("記録再生の identity に artifact hash を持たせない")
            if self.trace_digest is None:
                raise ValueError("記録再生には元になった記録の digest が要る")
        return self

    @property
    def claims_evidence(self) -> bool:
        """**自称**として実測 / Registry の裏づけを名乗っているか。

        **これを昇格の判断に使わない。** provenance も hash も、ただの値なので
        `registry_attested` を名乗る identity は誰でも組み立てられる。裏づけの判断は
        `attested_evidence()` が `ArtifactAttestation` そのものを見て行う
        （決定記録 0058 §2.3）。
        """
        return self.provenance is not DynamicsProvenance.SIMULATED_PROVISIONAL


_EVIDENCE_ISSUE_TOKEN = object()
"""`DynamicsEvidence` の発行経路を閉じるための番兵。**この module の外へ出さない。**"""


class DynamicsEvidence:
    """昇格の根拠にできる遷移の出どころを表す、**封をした**証拠。

    **公開 constructor を持たない。** この module の検証経路だけが発行する。

    - `logged_trajectory`: `LoggedTrajectoryDynamics` の構築
      （検証済みの記録 + 検証済み設定の許容幅）
    - `registry_attested`: `AttestedThermalDynamics.bind`
      （Registry が発行した `ArtifactAttestation`）

    `DynamicsIdentity` は pydantic model で、`registry_attested` も `logged_trajectory` も
    値として組み立てられる。**identity は自称で、これは証拠である**（決定記録 0058 §2.3）。
    `EnvironmentDynamics.provenances` も実装の自称なので、昇格の判断には使わない。

    **同一プロセス内の悪意ある偽造までは防げない**（決定記録 0050 §3）。狙いは、裏づけの無い
    dynamics が「検証済み」を名乗って昇格の根拠へ混ざる**配線の誤り**を型で止めることである。
    """

    __slots__ = ("_attestation", "_identity", "_provenance")
    _provenance: DynamicsProvenance
    _identity: DynamicsIdentity
    _attestation: ArtifactAttestation | None

    def __init__(self) -> None:
        raise TypeError("DynamicsEvidence は dynamics の検証経路からだけ得られる")

    def __setattr__(self, name: str, value: object) -> None:
        """発行後に差し替えられないようにする。"""
        raise AttributeError("DynamicsEvidence は不変")

    @classmethod
    def _issue(
        cls,
        provenance: DynamicsProvenance,
        identity: DynamicsIdentity,
        *,
        attestation: ArtifactAttestation | None = None,
        _token: object | None = None,
    ) -> DynamicsEvidence:
        if _token is not _EVIDENCE_ISSUE_TOKEN:
            raise TypeError("DynamicsEvidence は dynamics の検証経路だけが発行できる")
        if provenance is DynamicsProvenance.SIMULATED_PROVISIONAL:
            raise ValueError("近似 simulator に証拠は発行しない")
        if provenance is not identity.provenance:
            raise ValueError("証拠の出どころを identity と食い違わせない")
        if (provenance is DynamicsProvenance.REGISTRY_ATTESTED) != (attestation is not None):
            raise ValueError("registry_attested の証拠にだけ ArtifactAttestation を添える")
        evidence = object.__new__(cls)
        object.__setattr__(evidence, "_provenance", provenance)
        object.__setattr__(evidence, "_identity", identity)
        object.__setattr__(evidence, "_attestation", attestation)
        return evidence

    @property
    def provenance(self) -> DynamicsProvenance:
        """**この証拠が裏づける唯一の出どころ。** step はこれと一致しなければ根拠にならない。"""
        return self._provenance

    @property
    def identity(self) -> DynamicsIdentity:
        """証拠を発行したときの identity。借りた証拠を別の identity に付けさせない。"""
        return self._identity

    @property
    def attestation(self) -> ArtifactAttestation | None:
        """Registry が発行した artifact の証拠（`registry_attested` のときだけ）。"""
        return self._attestation


class WorkloadSample(_Frozen):
    """1 step の負荷擾乱。**将来の継続時間を断定しない**（AGENTS.md 制御アーキテクチャ）。"""

    regime: WorkloadRegime
    regime_confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    cpu_power_w: float = Field(ge=0.0, allow_inf_nan=False)
    gpu_power_w: float = Field(ge=0.0, allow_inf_nan=False)
    load: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    """近似 simulator が発熱の強さとして読む無単位の値。"""


class WorkloadTrace(_Frozen):
    """episode ごとに固定する負荷擾乱の列（再現性のため）。"""

    trace_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=120)
    step_ms: int = Field(gt=0)
    samples: tuple[WorkloadSample, ...] = Field(min_length=1, max_length=MAX_EPISODE_STEPS + 1)

    def at(self, index: int) -> WorkloadSample | None:
        """`index` 番目の擾乱。尽きていれば `None`（環境は episode を終端する）。"""
        if 0 <= index < len(self.samples):
            return self.samples[index]
        return None

    def digest(self) -> str:
        """この trace を一意に表す SHA-256。条件 hash に入れる。"""
        return canonical_sha256(self)


class DynamicsRequest(_Frozen):
    """次の観測を求めるための入力。**Demand は「掛かった action」としてのみ現れる。**"""

    window: ObservedThermalInput
    applied: PerZone[Demand]
    workload: WorkloadSample
    step_ms: int = Field(gt=0)
    step_index: int = Field(ge=0)


class LearnedStepAssessment(_Frozen):
    """learned simulator が1 step を作ったときの、同梱 Profile v2 による判定の**記録**。

    決定記録 0079 §2.9 の段 6（「同梱 Profile で step の OOD を記録する」）。**記録だけ**で、
    遷移も採点も `promotable` の条件も変えない（0079 §5 #8。条件に入れるなら新しい記録）。

    - `confidence` / `ood` / `ood_components`: その step の入力 window に対する anchor 推論
      （規則 `hold_effective`。0084 §2.1）を、運転時と同じ判定器
      `CounterfactualConfidenceAssessor`（同梱 Profile v2 と `fan-policy.yaml` の
      `model_confidence`）で判定した結果（0050 §2.2 の構成要素の形のまま）。simulator の中には
      実測が無いので residual の証拠は渡さない（residual drift は判定できない、として扱う）
    - `plan_support`: その step で掛けた demand を action schema の格子の間保った列を、anchor
      （window の action）から同梱 Profile v2 の step ごとの support に照らし、外れた最初の点
      （0079 §2.5 / 0084 §2.2。候補 plan の照合と**同じ関数**・margin なし）。外れていれば、
      その step の次の観測は学習した action 列の外の外挿である
    """

    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    ood: bool
    ood_components: tuple[ComponentResult, ...] = Field(max_length=len(ConfidenceComponent))
    """OOD と判定された構成要素（detail 付き）。OOD でなければ空。"""
    plan_support: StepSupportViolation | None
    """掛けた action 列が step ごとの support の外なら、外れた最初の点。中なら `None`。"""

    @model_validator(mode="after")
    def _ood_matches_the_components(self) -> Self:
        if any(not component.ood for component in self.ood_components):
            raise ValueError("OOD でない構成要素を ood_components に入れない")
        if self.ood != bool(self.ood_components):
            raise ValueError("ood と OOD の構成要素の有無を食い違わせない")
        if self.ood and self.confidence != 0.0:
            # 0050 §2.2「1つでも OOD なら confidence は 0」。
            raise ValueError("OOD の step の confidence は 0 にする")
        return self

    @property
    def outside_support(self) -> bool:
        """掛けた action 列が step ごとの support の外だったか。"""
        return self.plan_support is not None


class DynamicsStep(_Frozen):
    """1 step の遷移。**採点できないときは観測を作らない。**

    **観測の出どころは `window` ひとつだけ。** 値を別の欄でも返せると、window の品質 mask と
    食い違う値を渡せてしまう。環境は `window` の最後の frame から、**使える cell だけ**を
    読む（決定記録 0058 §2.2）。

    `registry_attested` の採点できる step は、同梱 Profile v2 による判定の記録 `assessment` を
    **必ず**持つ（0079 段 6）。それ以外の step は持たない（近似 simulator や記録再生に
    Profile の判定を名乗らせない）。
    """

    provenance: DynamicsProvenance
    supported: bool
    window: ObservedThermalInput | None = None
    reason: Reason | None = None
    assessment: LearnedStepAssessment | None = None

    @model_validator(mode="after")
    def _unsupported_steps_carry_no_observation(self) -> Self:
        if self.supported:
            if self.window is None:
                raise ValueError("採点できる step には次の観測が要る")
            if self.reason is not None:
                raise ValueError("採点できた step に失敗の理由を付けない")
            attested = self.provenance is DynamicsProvenance.REGISTRY_ATTESTED
            if attested and self.assessment is None:
                raise ValueError("learned simulator の step には同梱 Profile の判定の記録が要る")
            if not attested and self.assessment is not None:
                raise ValueError("learned simulator 以外の step に Profile の判定を付けない")
            return self
        if self.window is not None:
            # 「記録に無い action の結果」を作ってはならない（決定記録 0053 §2.3 / 0054 §2.2）。
            raise ValueError("採点できない step に観測を作らない")
        if self.reason is None:
            raise ValueError("採点できない step には理由を残す")
        if self.assessment is not None:
            raise ValueError("採点できない step に Profile の判定を付けない")
        return self


class EnvironmentDynamics(Protocol):
    """環境が次の観測を得るための読み取り専用契約。

    **Fan Hardware を持たない。** 実装は `control.hardware` / `control.safety` /
    `control.reactive` を import しない（試験で走査する）。
    """

    @property
    def identity(self) -> DynamicsIdentity:
        """この dynamics の出どころ。**自称であって証拠ではない。**"""
        ...

    @property
    def evidence(self) -> DynamicsEvidence | None:
        """封をした証拠。持たない dynamics は `None` を返す。

        **昇格の判断はこの object だけを見る。** `identity` も `provenances` も実装の自称で、
        値としてなら誰でも組み立てられる（決定記録 0058 §2.3）。`DynamicsEvidence` は
        この module の検証経路だけが発行するので、自称では用意できない。
        """
        ...

    @property
    def mode(self) -> TrainingMode:
        """この dynamics が表す学習 mode。**episode 側からは指定できない。**"""
        ...

    @property
    def applied_demand_tolerance(self) -> float | None:
        """記録と同じ action とみなした幅。記録を再生しない dynamics は `None`。

        **識別に使った幅を結果に残す**（決定記録 0056 §2.3）。広い幅で識別すれば、別の
        action が掛かっていた区間まで採点できてしまうので、読む側が確かめられるようにする。
        """
        ...

    @property
    def provenances(self) -> frozenset[DynamicsProvenance]:
        """この dynamics が出しうる step の出どころ。

        **step ごとの provenance も自称である。** 環境はこの集合に無い出どころの step を
        受け取らず、`DYNAMICS_UNUSABLE` として episode を終端する。hybrid だけが2つを持つ。
        """
        ...

    def conditions(self) -> dict[str, object]:
        """条件 hash へ載せる、この dynamics の**結果に効くすべて**。

        identity だけでは足りない。照合の許容幅や、hybrid が内側に持つ記録は
        identity に現れないのに結果を変える（決定記録 0058 §2.6）。
        """
        ...

    def advance(self, request: DynamicsRequest, *, rng: Random) -> DynamicsStep:
        """次の観測を返す。`rng` は environment が seed から作った決定論的な乱数源。"""
        ...


def _mean_demand(demands: PerZone[Demand]) -> float:
    return sum(demands.get(zone) for zone in Zone) / len(Zone)


def _advance_window(
    window: ObservedThermalInput,
    *,
    frame: ObservedWindowFrame,
    applied: PerZone[Demand],
) -> ObservedThermalInput:
    """観測 window を1 frame 進める。**長さは変えない。**

    `ObservedThermalInput` は「window が action 時刻で終わる」ことを不変条件にしているので、
    新しい frame の時刻がそのまま次の anchor 時刻になる（#84）。
    """
    metrics = set(window.window[-1].values)
    if set(frame.values) != metrics:
        raise DynamicsUnusableError(
            f"dynamics の frame が window の metric 集合と一致しない: {sorted(frame.values)}"
        )
    if frame.ts_ms <= window.window[-1].ts_ms:
        raise DynamicsUnusableError("dynamics の frame 時刻が window より過去になっている")
    return ObservedThermalInput(
        action_ts_ms=frame.ts_ms,
        window=(*window.window[1:], frame),
        action=PerZone[ObservedFanAction](
            front=ObservedFanAction(effective_demand=applied.front),
            rear=ObservedFanAction(effective_demand=applied.rear),
            top=ObservedFanAction(effective_demand=applied.top),
        ),
    )


def _synthesized_frame(
    window: ObservedThermalInput, *, ts_ms: int, values: Mapping[ThermalMetricName, float]
) -> ObservedWindowFrame:
    """**生成した**観測の frame。品質 mask はすべて偽で、source 時刻は生成時刻である。

    近似 simulator と learned simulator は、値を**その場で作る**。欠測も stale も無いのは
    偽装ではなく事実である。記録を再生するときはこの関数を使わない（決定記録 0058 §2.2）。
    """
    metrics = tuple(window.window[-1].values)
    missing = sorted(set(metrics) - set(values))
    if missing:
        raise DynamicsUnusableError(f"dynamics が window の metric を埋めていない: {missing}")
    return ObservedWindowFrame(
        ts_ms=ts_ms,
        values={metric: values[metric] for metric in metrics},
        source_ts_ms=dict.fromkeys(metrics, ts_ms),
        missing_mask=dict.fromkeys(metrics, False),
        stale_mask=dict.fromkeys(metrics, False),
        suspect_mask=dict.fromkeys(metrics, False),
    )


class SimulatedThermalDynamics:
    """設定した1次遅れ応答だけで動く近似 simulator。

    **実測に裏づけられていない。** ここから出た episode は `promotable=False` になり、
    #91 の gate や #92 の昇格判断の根拠にできない（決定記録 0058 §2.3）。
    `DynamicsIdentity` の不変条件により、この dynamics は Registry の証拠を名乗れない。
    """

    __slots__ = ("_config", "_identity", "_responses")

    def __init__(self, config: SimulatorConfig, *, config_sha256: str) -> None:
        self._config = config
        self._responses = {response.metric: response for response in config.responses}
        self._identity = DynamicsIdentity(
            provenance=DynamicsProvenance.SIMULATED_PROVISIONAL,
            model_id=config.model_id,
            model_version=config.model_version,
            config_sha256=config_sha256,
        )

    @property
    def identity(self) -> DynamicsIdentity:
        """近似 simulator であることを明示する identity。"""
        return self._identity

    @property
    def evidence(self) -> DynamicsEvidence | None:
        """**常に `None`。** 近似 simulator に証拠は無い。"""
        return None

    @property
    def mode(self) -> TrainingMode:
        """この dynamics が表す学習 mode。"""
        return TrainingMode.LEARNED_SIMULATOR

    @property
    def applied_demand_tolerance(self) -> float | None:
        """記録を再生しないので識別の幅を持たない。"""
        return None

    @property
    def provenances(self) -> frozenset[DynamicsProvenance]:
        """近似の step しか出さない。"""
        return frozenset({DynamicsProvenance.SIMULATED_PROVISIONAL})

    def conditions(self) -> dict[str, object]:
        """条件 hash へ載せる値。設定 bytes の hash が応答の中身を覆う。"""
        return {"identity": self._identity.model_dump(mode="json")}

    def advance(self, request: DynamicsRequest, *, rng: Random) -> DynamicsStep:
        """設定した平衡温度へ1次遅れで近づける。同じ seed からは同じ列になる。"""
        last = request.window.window[-1]
        flow_basis = _mean_demand(request.applied)
        values: dict[ThermalMetricName, float] = {}
        for metric in last.values:
            response = self._responses.get(metric)
            if response is None:
                raise DynamicsUnusableError(f"simulator に応答の無い metric がある: {metric}")
            previous = last.values[metric]
            if previous is None:
                raise DynamicsUnusableError(f"欠測の frame から simulator を進めない: {metric}")
            flow = response.flow_floor.value + response.flow_gain.value * flow_basis
            equilibrium = response.ambient_c.value + (
                response.load_gain_c.value * request.workload.load / flow
            )
            alpha = 1.0 - math.exp(-request.step_ms / response.time_constant_ms.value)
            noise = (
                rng.gauss(0.0, response.noise_sigma_c.value)
                if response.noise_sigma_c.value > 0.0
                else 0.0
            )
            value = previous + (equilibrium - previous) * alpha + noise
            if not math.isfinite(value):
                raise DynamicsUnusableError(f"simulator の出力が有限でない: {metric}")
            values[metric] = value
        ts_ms = last.ts_ms + request.step_ms
        return DynamicsStep(
            provenance=DynamicsProvenance.SIMULATED_PROVISIONAL,
            supported=True,
            window=_advance_window(
                request.window,
                frame=_synthesized_frame(request.window, ts_ms=ts_ms, values=values),
                applied=request.applied,
            ),
        )


class LoggedFrame(_Frozen):
    """記録済み運転の1 step。**掛かっていた demand と、その後の観測を対で持つ。**

    観測は `ObservedWindowFrame` をそのまま持つ。値だけでなく **source 時刻と
    missing / stale / suspect の mask も記録から来る**（決定記録 0058 §2.2）。
    どれも必須で、既定値を持たない。**品質の分からない記録は受け取らない**（`OK` に倒さない）。
    """

    applied: PerZone[Demand]
    observed: ObservedWindowFrame

    @property
    def ts_ms(self) -> int:
        """この記録の観測時刻。"""
        return self.observed.ts_ms


class LoggedTrajectory(_Frozen):
    """offline RL に使う記録済み trajectory。"""

    trajectory_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=120)
    frames: tuple[LoggedFrame, ...] = Field(min_length=1, max_length=MAX_EPISODE_STEPS)

    @model_validator(mode="after")
    def _frames_are_ordered(self) -> Self:
        timestamps = tuple(frame.ts_ms for frame in self.frames)
        if tuple(sorted(set(timestamps))) != timestamps:
            raise ValueError("記録 trajectory の時刻は重複なし昇順にする")
        return self

    def digest(self) -> str:
        """この記録を一意に表す SHA-256。"""
        return canonical_sha256(self)


class LoggedTrajectoryDynamics:
    """記録済み運転だけで遷移を作る（logged trajectory からの offline RL）。

    **記録と違う action の結果を作らない。** 要求 demand が記録と `applied_demand_tolerance`
    の範囲で一致しない step は `supported=False` として数え、観測を返さない
    （決定記録 0053 §2.3 / 0054 §2.2 と同じ帰属規則）。これが coverage になる。
    """

    __slots__ = ("_evidence", "_identity", "_tolerance", "_trajectory")

    def __init__(self, trajectory: LoggedTrajectory, *, shadow: ShadowConfig) -> None:
        """照合の許容幅は**検証済み設定から取る**（呼び出し側の写しを受け取らない）。

        評価側が独自の幅を持たないのと同じ理由である（決定記録 0054 §2.6）。別の幅で
        照合した結果を、同じ coverage として並べられないようにする。
        """
        self._trajectory = trajectory
        self._tolerance = shadow.applied_demand_tolerance.value
        self._identity = DynamicsIdentity(
            provenance=DynamicsProvenance.LOGGED_TRAJECTORY,
            model_id=trajectory.trajectory_id,
            model_version=str(len(trajectory.frames)),
            trace_digest=trajectory.digest(),
        )
        # 検証済みの記録と検証済み設定の許容幅から作ったことを、封をした証拠として残す。
        self._evidence = DynamicsEvidence._issue(
            DynamicsProvenance.LOGGED_TRAJECTORY,
            self._identity,
            _token=_EVIDENCE_ISSUE_TOKEN,
        )

    @property
    def identity(self) -> DynamicsIdentity:
        """記録再生であることを明示する identity。"""
        return self._identity

    @property
    def evidence(self) -> DynamicsEvidence | None:
        """検証済みの記録に裏づけられた証拠。**構築経路だけが発行している。**"""
        return self._evidence

    @property
    def mode(self) -> TrainingMode:
        """この dynamics が表す学習 mode。"""
        return TrainingMode.LOGGED

    @property
    def applied_demand_tolerance(self) -> float | None:
        """記録と同じ action とみなした幅（`fan-policy.yaml` の `shadow` から取った値）。"""
        return self._tolerance

    @property
    def provenances(self) -> frozenset[DynamicsProvenance]:
        """記録再生の step しか出さない。"""
        return frozenset({DynamicsProvenance.LOGGED_TRAJECTORY})

    def conditions(self) -> dict[str, object]:
        """条件 hash へ載せる値。**許容幅も入れる**（coverage が変わるため）。"""
        return {
            "identity": self._identity.model_dump(mode="json"),
            "applied_demand_tolerance": self._tolerance,
        }

    def advance(self, request: DynamicsRequest, *, rng: Random) -> DynamicsStep:
        """記録された次の観測を返す。記録と違う action なら観測を作らない。"""
        del rng  # 記録再生に乱数は無い。seed を変えても同じ列になる。
        if request.step_index >= len(self._trajectory.frames):
            return self._unsupported("logged_trajectory_exhausted", "記録の frame が尽きた")
        frame = self._trajectory.frames[request.step_index]
        deviations = [
            f"{zone.value}: requested={request.applied.get(zone):.6f}; "
            f"logged={frame.applied.get(zone):.6f}"
            for zone in Zone
            if abs(request.applied.get(zone) - frame.applied.get(zone)) > self._tolerance
        ]
        if deviations:
            return self._unsupported("applied_action_differs", "; ".join(deviations))
        return DynamicsStep(
            provenance=DynamicsProvenance.LOGGED_TRAJECTORY,
            supported=True,
            # **記録した frame をそのまま置く。** 時刻も品質 mask も作り直さない。
            window=_advance_window(request.window, frame=frame.observed, applied=frame.applied),
        )

    @staticmethod
    def _unsupported(code: str, detail: str) -> DynamicsStep:
        return DynamicsStep(
            provenance=DynamicsProvenance.LOGGED_TRAJECTORY,
            supported=False,
            reason=Reason(code=code, detail=detail[:500]),
        )


class HybridDynamics:
    """記録で説明できる step は記録から、説明できない step は近似 simulator から。

    **どちらから来たかを step ごとに残す。** 混ぜたまま平均すると、実測の裏づけが
    近似の外挿で薄まる。episode に simulated な step が1つでも混ざれば、
    その episode は昇格の根拠にできない（決定記録 0058 §2.2）。
    """

    __slots__ = ("_identity", "_logged", "_simulated")

    def __init__(
        self, logged: LoggedTrajectoryDynamics, simulated: SimulatedThermalDynamics
    ) -> None:
        self._logged = logged
        self._simulated = simulated
        # hybrid は「記録の裏づけがある step だけ」を証拠にする。identity は近似側を名乗り、
        # 実測から来た step は step ごとの provenance で区別する。
        self._identity = simulated.identity

    @property
    def identity(self) -> DynamicsIdentity:
        """近似を含むことを隠さない identity。"""
        return self._identity

    @property
    def evidence(self) -> DynamicsEvidence | None:
        """**常に `None`。** 近似を含む以上、証拠は名乗れない。

        記録から来た step も、この dynamics の下では昇格の根拠にしない。step ごとに
        分けて数え直すより、`promotable=False` で閉じるほうが取り違えが起きない。
        """
        return None

    @property
    def mode(self) -> TrainingMode:
        """この dynamics が表す学習 mode。"""
        return TrainingMode.HYBRID

    @property
    def applied_demand_tolerance(self) -> float | None:
        """記録側が使った識別の幅。"""
        return self._logged.applied_demand_tolerance

    @property
    def provenances(self) -> frozenset[DynamicsProvenance]:
        """記録と近似の両方を出す。**step ごとにどちらかが残る。**"""
        return self._logged.provenances | self._simulated.provenances

    @property
    def logged_identity(self) -> DynamicsIdentity:
        """記録側の identity（条件 hash に入れる）。"""
        return self._logged.identity

    def conditions(self) -> dict[str, object]:
        """**内側の2つを両方載せる。** identity だけでは記録側が条件から消える。"""
        return {
            "logged": self._logged.conditions(),
            "simulated": self._simulated.conditions(),
        }

    def advance(self, request: DynamicsRequest, *, rng: Random) -> DynamicsStep:
        """記録で説明できればそちらを、できなければ近似を使う。"""
        recorded = self._logged.advance(request, rng=rng)
        if recorded.supported:
            return recorded
        return self._simulated.advance(request, rng=rng)


class AttestedThermalDynamics:
    """Registry が検証した反実仮想 Thermal Model artifact v2 を learned simulator として使う。

    **受け取るのは封をした型 `RegistryCounterfactualThermalModel` だけ**（決定記録 0079 §2.4 /
    §2.9 の段 6）。この型は Registry の検証経路が発行した `VerifiedArtifact` から、読み込み時の
    検査（L1〜L12。較正の L9 を含む）をすべて通ったときだけ作られ、model と同梱 Confidence
    Profile v2 を同じ bytes から持つ。v1 artifact（`observational_replay`）はこの型にならない
    ので、学習 dynamics にも束縛できない（0079 §2.2）。

    `production_active` は**要求しない**。0052 §2.1 が「Replay / offline 評価は production で
    ない attestation をそのまま使う」としているためで、ここは制御経路ではない（0058 §2.3）。
    代わりに、この束は `MpcModelBinding` へ変換できない（制御へ配線する API を持たない）。

    **各 step で同梱 Profile v2 による判定を記録する**（`LearnedStepAssessment`）。記録だけで、
    遷移・採点・`promotable` の条件は変えない（0079 §5 #8）。
    """

    __slots__ = (
        "_assessor",
        "_attestation",
        "_checker",
        "_evidence",
        "_identity",
        "_model",
    )
    _model: RegistryCounterfactualThermalModel
    _attestation: ArtifactAttestation
    _assessor: CounterfactualConfidenceAssessor
    _checker: StepSupportChecker
    _identity: DynamicsIdentity
    _evidence: DynamicsEvidence

    def __init__(self) -> None:
        raise TypeError("AttestedThermalDynamics は bind からだけ作る")

    @classmethod
    def from_verified_artifact(
        cls,
        verified: VerifiedArtifact,
        *,
        metric_catalog: MetricCatalog,
        calibration: RuntimeCalibration,
        confidence_policy: ModelConfidencePolicy,
    ) -> AttestedThermalDynamics:
        """`VerifiedArtifact` から封をした型を作り、同じ証拠と束ねる（1つの呼び出し）。

        `metric_catalog` は `config/metrics.yaml`、`calibration` は較正の値（読めなければ
        `RuntimeCalibration.unavailable`）。どちらも既定値を持たない（0079 §2.4 / 0096）。
        読み込み時の検査に外れた artifact は `DynamicsUnusableError`（検査の番号つき）になる。
        """
        try:
            model = RegistryCounterfactualThermalModel.from_verified_artifact(
                verified, metric_catalog=metric_catalog, calibration=calibration
            )
        except CounterfactualArtifactRejectedError as error:
            raise DynamicsUnusableError(
                f"反実仮想 artifact v2 の読み込み時の検査に外れた（{error.check.value}）: "
                f"{error.detail}"
            ) from error
        return cls.bind(
            model, attestation=verified.attestation, confidence_policy=confidence_policy
        )

    @classmethod
    def bind(
        cls,
        model: RegistryCounterfactualThermalModel,
        *,
        attestation: ArtifactAttestation,
        confidence_policy: ModelConfidencePolicy,
    ) -> AttestedThermalDynamics:
        """Registry の証拠と突き合わせて束ねる。条件を1つでも欠けば拒む。

        `confidence_policy` は `fan-policy.yaml` の `model_confidence`（運転時と同じ判定の設定）。
        各 step の判定の記録にだけ使う。
        """
        if type(model) is not RegistryCounterfactualThermalModel:
            # **v1 の model や、それを包んだ wrapper を受け取らない**（0079 §2.4 / §2.2）。
            raise DynamicsUnusableError(
                "learned simulator は Registry の検証経路が作った反実仮想 artifact v2 の"
                f"封をした型だけから作る（type={type(model).__name__}）"
            )
        if attestation.kind is not ArtifactKind.THERMAL_MODEL:
            raise DynamicsUnusableError(
                "thermal model 以外の artifact を dynamics にしない"
                f"（kind={attestation.kind.value}）"
            )
        if attestation.capability is not ArtifactCapability.COUNTERFACTUAL_ACTION:
            # **登録時に申告された能力だけを見る。** 封をした型の L1 と同じ条件を束縛でも持つ。
            raise DynamicsUnusableError(
                "反実仮想予測を申告していない artifact を learned simulator にしない"
                f"（attested capability={attestation.capability.value}。決定記録 0048 §2.1）"
            )
        manifest = model.manifest
        if manifest.capability is not InferenceCapability.COUNTERFACTUAL_ACTION:
            raise DynamicsUnusableError(
                "model の capability が Registry の申告と食い違っている"
                f"（model={manifest.capability.value}; attested={attestation.capability.value}）"
            )
        mismatches = [
            name
            for name, attested, declared in (
                ("model_id", attestation.model_id, manifest.model_id),
                ("model_version", attestation.version, manifest.model_version),
                # **借りた証拠を別の bytes の model に付けさせない。** 封をした型は同じ bytes
                # から artifact hash を持つので、一致しなければ別の artifact である。
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
            raise DynamicsUnusableError(
                f"model が検証済み artifact と一致しない: {','.join(mismatches)}"
            )
        uncovered = sorted(set(model.feature_schema.metrics) - set(model.target_schema.metrics))
        if uncovered:
            # 次の window は最初の horizon の予測だけで作る（観測を作り直さない。0058 §2.2）。
            # 予測しない feature の metric は埋められないので、step で落ちる前に束縛で拒む。
            raise DynamicsUnusableError(
                "learned simulator は window の全 metric を予測する artifact に限る"
                f"（target に無い feature の metric: {uncovered}）"
            )
        sample_period_ms = model.feature_schema.sample_period_ms
        if sample_period_ms != model.action_schema.step_ms:
            # 1 step で window を1 frame（action の刻み）だけ進めるので、window の刻みが違うと
            # 次の step の推論で window が feature schema に合わなくなる。再標本化はしない
            # （補間・外挿をしない。0079 §2.3）。step で落ちる前に束縛で拒む。
            raise DynamicsUnusableError(
                "learned simulator は window の刻みが action の刻みと等しい artifact に限る"
                f"（sample_period={sample_period_ms}ms; action={model.action_schema.step_ms}ms）"
            )
        bound = object.__new__(cls)
        bound_identity = DynamicsIdentity(
            provenance=DynamicsProvenance.REGISTRY_ATTESTED,
            model_id=attestation.model_id,
            model_version=attestation.version,
            artifact_sha256=attestation.artifact_sha256,
        )
        object.__setattr__(bound, "_model", model)
        object.__setattr__(bound, "_attestation", attestation)
        # 判定器も support の照合も**同梱 Profile からだけ**作る（別の Profile を渡す口が無い）。
        object.__setattr__(
            bound,
            "_assessor",
            CounterfactualConfidenceAssessor.for_model(model, confidence_policy),
        )
        object.__setattr__(
            bound,
            "_checker",
            StepSupportChecker(model.confidence_profile, model.action_schema),
        )
        object.__setattr__(bound, "_identity", bound_identity)
        object.__setattr__(
            bound,
            "_evidence",
            DynamicsEvidence._issue(
                DynamicsProvenance.REGISTRY_ATTESTED,
                bound_identity,
                attestation=attestation,
                _token=_EVIDENCE_ISSUE_TOKEN,
            ),
        )
        return bound

    def __setattr__(self, name: str, value: object) -> None:
        """束を後から差し替えられないようにする。"""
        raise AttributeError("AttestedThermalDynamics は不変")

    @property
    def identity(self) -> DynamicsIdentity:
        """Registry の証拠に裏づけられた identity。"""
        return self._identity

    @property
    def attestation(self) -> ArtifactAttestation:
        """束ねたときの Registry の証拠。"""
        return self._attestation

    @property
    def model(self) -> RegistryCounterfactualThermalModel:
        """束ねた封をした型（model と同梱 Profile v2）。"""
        return self._model

    @property
    def action_schema(self) -> ThermalActionSchema:
        """artifact の action の格子。環境の刻みはこの `step_ms` と一致しなければならない。"""
        return self._model.action_schema

    @property
    def confidence_policy(self) -> ModelConfidencePolicy:
        """判定の記録に使っている `model_confidence` の設定。"""
        return self._assessor.policy

    @property
    def evidence(self) -> DynamicsEvidence | None:
        """Registry の証拠に封をしたもの。**`bind` だけが発行している。**"""
        return self._evidence

    @property
    def mode(self) -> TrainingMode:
        """この dynamics が表す学習 mode。"""
        return TrainingMode.LEARNED_SIMULATOR

    @property
    def applied_demand_tolerance(self) -> float | None:
        """記録を再生しないので識別の幅を持たない。"""
        return None

    @property
    def provenances(self) -> frozenset[DynamicsProvenance]:
        """Registry の証拠に裏づけられた step しか出さない。"""
        return frozenset({DynamicsProvenance.REGISTRY_ATTESTED})

    def conditions(self) -> dict[str, object]:
        """条件 hash へ載せる値。Registry の証拠と、判定の記録に効く Profile と設定を覆う。"""
        return {
            "identity": self._identity.model_dump(mode="json"),
            "attestation": self._attestation.trace_metadata(),
            "confidence_profile_sha256": self._model.confidence_profile.sha256(),
            "confidence_policy": self._assessor.policy.model_dump(mode="json"),
        }

    def advance(self, request: DynamicsRequest, *, rng: Random) -> DynamicsStep:
        """1 step 分の反実仮想予測を次の観測として使い、同梱 Profile での判定を記録する。

        掛けた demand を action schema の格子の間保った列（`ActionPlan.held` と同じ形）を
        model へ渡し、最初の horizon（= 1 step 後）の予測を次の frame にする。因果の mask
        （0079 §2.3）により、最初の horizon は step 0 の action しか使わない。
        **格子が環境の刻みと違えば予測しない**（補間・外挿・丸めをしない。0079 §2.3）。
        """
        del rng  # 学習済み simulator は決定論的。揺らぎを足すなら別の model として束ねる。
        schema = self._model.action_schema
        horizons = self._model.target_schema.horizons_ms
        if request.step_ms != schema.step_ms or horizons[0] != request.step_ms:
            raise DynamicsUnusableError(
                "環境の刻みが artifact の action の格子と一致しない（補間・外挿・丸めはしない）"
                f"（step_ms={request.step_ms}; action={schema.step_ms}ms×{schema.steps}; "
                f"first_horizon={horizons[0]}ms）"
            )
        window = request.window
        trajectory = ActionTrajectory(
            step_ms=schema.step_ms,
            demands=tuple(
                PerZone[Demand](
                    front=request.applied.front,
                    rear=request.applied.rear,
                    top=request.applied.top,
                )
                for _ in range(schema.steps)
            ),
        )
        anchor = PerZone[float](
            front=window.action.front.effective_demand,
            rear=window.action.rear.effective_demand,
            top=window.action.top.effective_demand,
        )
        # **運転時と同じ判定器で、入力 window の anchor 推論を判定する。** simulator の中に実測は
        # 無いので residual の証拠は渡さない（自分の予測と照らしても drift は測れない）。
        assessment = self._assessor.assess(window, self._model.predict(window), None)
        prediction = self._model.predict_trajectory(window, trajectory)
        if prediction.artifact_sha256 != self._model.artifact_sha256:
            raise DynamicsUnusableError("learned simulator が束ねた artifact と別の予測を返した")
        first = prediction.targets[0]
        if first.horizon_ms != request.step_ms:
            raise DynamicsUnusableError("learned simulator の最初の horizon が環境の刻みと違う")
        values = {metric: float(value) for metric, value in first.values.items()}
        ts_ms = window.action_ts_ms + request.step_ms
        return DynamicsStep(
            provenance=DynamicsProvenance.REGISTRY_ATTESTED,
            supported=True,
            window=_advance_window(
                window,
                frame=_synthesized_frame(window, ts_ms=ts_ms, values=values),
                applied=request.applied,
            ),
            assessment=LearnedStepAssessment(
                confidence=assessment.confidence,
                ood=assessment.ood,
                ood_components=tuple(
                    component for component in assessment.components if component.ood
                ),
                # 候補 plan・held の列と**同じ照合**（0084 §2.2）。margin も件数の下限も掛けない。
                plan_support=self._checker.check(anchor, trajectory),
            ),
        )


def attested_evidence(dynamics: EnvironmentDynamics) -> DynamicsEvidence | None:
    """昇格の根拠にできる**封をした証拠**を返す。無ければ `None`。

    **自称は一切見ない。** `identity.provenance` も `provenances` も、実装が返すただの値で、
    近似 simulator でも `logged_trajectory` / `registry_attested` を名乗れる。判断するのは
    `DynamicsEvidence` object の有無と中身だけで、これは `LoggedTrajectoryDynamics` の構築と
    `AttestedThermalDynamics.bind` しか発行できない（決定記録 0058 §2.3）。

    確かめるのは3つ。

    1. 証拠があること（= この module の検証経路を通ったこと）
    2. 証拠の identity が、いま dynamics が名乗っている identity と一致すること
       （借りた証拠を別の identity に付けさせない）
    3. `registry_attested` なら、`ArtifactAttestation` の kind / capability / model ID / 版 /
       artifact hash が identity と一致すること

    **同一プロセス内の悪意ある偽造までは防げない**（決定記録 0050 §3）。狙いは、裏づけの無い
    dynamics が「検証済み」を名乗って昇格の根拠へ混ざる**配線の誤り**を型で止めることである。
    """
    evidence = dynamics.evidence
    if evidence is None:
        return None
    if evidence.identity != dynamics.identity:
        return None
    if evidence.provenance is DynamicsProvenance.LOGGED_TRAJECTORY:
        # 発行経路が閉じているので、ここに来た時点で検証済みの記録から来ている。
        return evidence if evidence.attestation is None else None
    attestation = evidence.attestation
    if attestation is None:
        return None
    matches = (
        attestation.kind is ArtifactKind.THERMAL_MODEL
        and attestation.capability is ArtifactCapability.COUNTERFACTUAL_ACTION
        and attestation.model_id == evidence.identity.model_id
        and attestation.version == evidence.identity.model_version
        and attestation.artifact_sha256 == evidence.identity.artifact_sha256
    )
    return evidence if matches else None
