"""学習環境の Dynamics（#105 / 決定記録 0058 §2.2 / §2.3）。

環境が「次に何が観測されるか」を決める層で、3種類ある。**どれを使ったかは step ごとに残り、
episode の結果に必ず出る。**

| provenance | 出どころ | 昇格の根拠にできるか |
|---|---|---|
| `logged_trajectory` | 記録済みの運転（実測） | できる。ただし**記録と同じ action の区間だけ** |
| `registry_attested` | Registry が反実仮想を申告した artifact | できる。**該当 artifact は無い** |
| `simulated_provisional` | 設定した近似式 | **できない。** 実測に裏づけが無い |

`registry_attested` は `ArtifactCapability.COUNTERFACTUAL_ACTION` を要求する。現行の
artifact はすべて `observational_replay` なので（決定記録 0048 §2.1 / 0052 §2.1）、
**この経路はいまのところ決定論的にすべて拒む。** 拒否は不具合ではなく、0052 と同じ規律である。

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

from coldaisle.control.config import ShadowConfig
from coldaisle.control.model.thermal import (
    ObservedFanAction,
    ObservedThermalInput,
    ObservedWindowFrame,
    ThermalMetricName,
    canonical_sha256,
)
from coldaisle.control.model_registry import ArtifactAttestation, ArtifactCapability, ArtifactKind
from coldaisle.control.mpc import (
    ActionPlan,
    CounterfactualThermalModel,
    PlannedThermalInput,
)
from coldaisle.control.rl.config import MAX_EPISODE_STEPS, SimulatorConfig
from coldaisle.control.schema import Demand, PerZone, Reason, WorkloadRegime, Zone

Sha256 = str


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


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


class DynamicsStep(_Frozen):
    """1 step の遷移。**採点できないときは値を作らない。**"""

    provenance: DynamicsProvenance
    supported: bool
    window: ObservedThermalInput | None = None
    values: dict[ThermalMetricName, float] = Field(default_factory=dict)
    reason: Reason | None = None

    @model_validator(mode="after")
    def _unsupported_steps_carry_no_values(self) -> Self:
        if self.supported:
            if self.window is None or not self.values:
                raise ValueError("採点できる step には次の観測が要る")
            if self.reason is not None:
                raise ValueError("採点できた step に失敗の理由を付けない")
            return self
        if self.window is not None or self.values:
            # 「記録に無い action の結果」を作ってはならない（決定記録 0053 §2.3 / 0054 §2.2）。
            raise ValueError("採点できない step に観測を作らない")
        if self.reason is None:
            raise ValueError("採点できない step には理由を残す")
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


def _next_window(
    window: ObservedThermalInput,
    *,
    ts_ms: int,
    values: Mapping[ThermalMetricName, float],
    applied: PerZone[Demand],
) -> ObservedThermalInput:
    """観測 window を1 frame 進める。**長さは変えない。**

    `ObservedThermalInput` は「window が action 時刻で終わる」ことを不変条件にしているので、
    新しい frame の時刻がそのまま次の anchor 時刻になる（#84）。
    """
    metrics = tuple(window.window[-1].values)
    missing = sorted(set(metrics) - set(values))
    if missing:
        raise DynamicsUnusableError(f"dynamics が window の metric を埋めていない: {missing}")
    frame = ObservedWindowFrame(
        ts_ms=ts_ms,
        values={metric: values[metric] for metric in metrics},
        source_ts_ms=dict.fromkeys(metrics, ts_ms),
        missing_mask=dict.fromkeys(metrics, False),
        stale_mask=dict.fromkeys(metrics, False),
        suspect_mask=dict.fromkeys(metrics, False),
    )
    return ObservedThermalInput(
        action_ts_ms=ts_ms,
        window=(*window.window[1:], frame),
        action=PerZone[ObservedFanAction](
            front=ObservedFanAction(effective_demand=applied.front),
            rear=ObservedFanAction(effective_demand=applied.rear),
            top=ObservedFanAction(effective_demand=applied.top),
        ),
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
            window=_next_window(
                request.window, ts_ms=ts_ms, values=values, applied=request.applied
            ),
            values=values,
        )


class LoggedFrame(_Frozen):
    """記録済み運転の1 step。**掛かっていた demand と、その後の観測を対で持つ。**"""

    ts_ms: int = Field(ge=0)
    applied: PerZone[Demand]
    values: dict[ThermalMetricName, float] = Field(min_length=1)


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
    def provenances(self) -> frozenset[DynamicsProvenance]:
        """記録再生の step しか出さない。"""
        return frozenset({DynamicsProvenance.LOGGED_TRAJECTORY})

    @property
    def applied_demand_tolerance(self) -> float:
        """記録と同じ action とみなす幅（`fan-policy.yaml` の `shadow` から取った値）。"""
        return self._tolerance

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
            window=_next_window(
                request.window,
                ts_ms=frame.ts_ms,
                values=frame.values,
                applied=frame.applied,
            ),
            values=dict(frame.values),
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
    """Registry が反実仮想能力を申告した artifact を learned simulator として使う。

    **いまこの経路を通れる artifact は1つも無い。** 現行の #84 artifact は manifest の
    capability が `observational_replay` に固定されているため（決定記録 0048 §2.1）、
    `bind` は決定論的にすべて拒む。0052 §2.1 と同じ規律を、環境側にもそのまま置く。

    `production_active` は**要求しない**。0052 §2.1 が「Replay / offline 評価は production で
    ない attestation をそのまま使う」としているためで、ここは制御経路ではない。
    代わりに、この束は `MpcModelBinding` へ変換できない（制御へ配線する API を持たない）。
    """

    __slots__ = ("_attestation", "_evidence", "_identity", "_model")
    _model: CounterfactualThermalModel
    _attestation: ArtifactAttestation
    _identity: DynamicsIdentity
    _evidence: DynamicsEvidence

    def __init__(self) -> None:
        raise TypeError("AttestedThermalDynamics は bind からだけ作る")

    @classmethod
    def bind(
        cls,
        model: CounterfactualThermalModel,
        *,
        attestation: ArtifactAttestation,
    ) -> AttestedThermalDynamics:
        """Registry の証拠と突き合わせて束ねる。条件を1つでも欠けば拒む。"""
        identity = model.identity
        if attestation.kind is not ArtifactKind.THERMAL_MODEL:
            raise DynamicsUnusableError(
                "thermal model 以外の artifact を dynamics にしない"
                f"（kind={attestation.kind.value}）"
            )
        if attestation.capability is not ArtifactCapability.COUNTERFACTUAL_ACTION:
            # **登録時に申告された能力だけを見る。** 推論器の自称では判断しない。
            raise DynamicsUnusableError(
                "反実仮想予測を申告していない artifact を learned simulator にしない"
                f"（attested capability={attestation.capability.value}。決定記録 0048 §2.1）"
            )
        if identity.capability.value != attestation.capability.value:
            raise DynamicsUnusableError(
                "model の自称 capability が Registry の申告と食い違っている"
                f"（model={identity.capability.value}; attested={attestation.capability.value}）"
            )
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
            raise DynamicsUnusableError(
                f"model が検証済み artifact と一致しない: {','.join(mismatches)}"
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
    def evidence(self) -> DynamicsEvidence | None:
        """Registry の証拠に封をしたもの。**`bind` だけが発行している。**"""
        return self._evidence

    @property
    def provenances(self) -> frozenset[DynamicsProvenance]:
        """Registry の証拠に裏づけられた step しか出さない。"""
        return frozenset({DynamicsProvenance.REGISTRY_ATTESTED})

    def conditions(self) -> dict[str, object]:
        """条件 hash へ載せる値。Registry の証拠をそのまま覆う。"""
        return {
            "identity": self._identity.model_dump(mode="json"),
            "attestation": self._attestation.trace_metadata(),
        }

    def advance(self, request: DynamicsRequest, *, rng: Random) -> DynamicsStep:
        """1 step 分の反実仮想予測を次の観測として使う。"""
        del rng  # 学習済み simulator は決定論的。揺らぎを足すなら別の model として束ねる。
        plan = ActionPlan.held(request.applied, step_ms=request.step_ms, steps=1)
        prediction = self._model.predict_plan(
            PlannedThermalInput(observed=request.window, plan=plan)
        )
        if not prediction.matches(plan):
            raise DynamicsUnusableError("learned simulator が別の候補 plan の予測を返した")
        values = {metric: float(value) for metric, value in prediction.targets[0].values.items()}
        ts_ms = request.window.action_ts_ms + request.step_ms
        return DynamicsStep(
            provenance=DynamicsProvenance.REGISTRY_ATTESTED,
            supported=True,
            window=_next_window(
                request.window, ts_ms=ts_ms, values=values, applied=request.applied
            ),
            values=values,
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
