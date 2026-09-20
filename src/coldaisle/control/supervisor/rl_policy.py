"""RL Supervisor policy の束縛と推論（#89 / 決定記録 0061）。

`RegimeTableRlPolicy` は #88 の `RLPolicy` interface をそのまま満たす。**Demand を返す API を
持たず**、返すのは `SupervisorOutput`（strategy / weights / target band）だけである。
選択は `SupervisorCoordinator` が行い、shadow slot の出力は active になれない（#88 / 0053）。

**artifact の自称では束縛しない。** 使ってよいかどうかは

- Registry（#104）の検証経路だけが発行する `ArtifactAttestation`
- runtime の `supervisor.output_bounds`（学習時の範囲と一致するか、各欄が範囲内か）
- 設定の `supervisor.rl_version`（期待する版）

の3つで決める。同一プロセス内の悪意ある偽造までは防げない（決定記録 0050 §3）。狙いは
**配線の誤りを型で止めること**である。

**active への門は閉じている。** `for_active` は**入力を一切見ずに拒否する**。

policy artifact が「反実仮想の裏づけがあった」と書いていても、それは artifact の**自称**で
あって証拠ではない（0061 §2.3）。自称を門の条件にすると、この package がほかの場所で
閉じたのと同じ穴——自称を根拠として読む——をここで開けることになる。裏づけを**検証できる
形**（環境が封をした `DynamicsEvidence` の系譜、または Registry が反実仮想能力を申告した
artifact の attestation）で受け取る手立ては、いまの依存関係では用意できない
（`control.supervisor` は `control.rl` を import できない。循環になる）。

そこで門は開けたままにせず、**閉じたことを1か所に書く**。反実仮想能力を申告した thermal
artifact が1つも無い以上（決定記録 0048 §2.1 / 0052 §2.1 / 0058 §3）、いま通してよい
policy はそもそも存在しない。**門を開く条件は未決で、決定記録と所有者の承認が要る**
（0061 §5）。

**出力には束縛の用途が付いて回る。** `deliver()` が `SupervisorOutputOrigin` を写すので、
shadow 用に束縛した policy の出力を active slot へ繋いだ構成は Coordinator で Rule へ落ちる。
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Self

from pydantic import BaseModel

from coldaisle.clock import Clock
from coldaisle.control.config import SupervisorOutputBounds
from coldaisle.control.model.thermal import is_pickle_payload
from coldaisle.control.model_registry import (
    ArtifactAttestation,
    ArtifactCapability,
    ArtifactKind,
    VerifiedArtifact,
)
from coldaisle.control.schema import (
    AuthorityStage,
    SupervisorOutput,
    SupervisorPolicyIdentity,
    SupervisorPolicyKind,
)
from coldaisle.control.supervisor.artifact import (
    MAX_POLICY_ARTIFACT_BYTES,
    SupervisorPolicyArtifact,
    SupervisorPolicyRegistryMetadata,
    action_space_sha256,
    canonical_policy_artifact_bytes,
    policy_registry_metadata,
)
from coldaisle.control.supervisor.policy import (
    ReceivedSupervisorOutput,
    SupervisorInput,
    SupervisorOutputOrigin,
)

_BINDING_TOKEN = object()
"""`SupervisorPolicyBinding` の発行境界。

外から `RegimeTableRlPolicy` を registry 検証済みに見せないための token である。
"""


class SupervisorPolicyUnusableError(RuntimeError):
    """policy artifact を要求された用途へ束縛できない。

    **暗黙の降格をしない。** 条件を満たさない artifact は「弱い権限で使う」のではなく
    使わない（決定記録 0052 §2.1 と同じ規律）。
    """


class PolicyArtifactVerification(StrEnum):
    """policy bytes が #104 の検証境界を越えたか。"""

    REGISTRY_VERIFIED = "registry_verified"
    OFFLINE_UNVERIFIED = "offline_unverified"
    """学習・評価のために直接読んだ artifact。**運転の policy slot へ渡せない。**"""


class PolicyBindingIntent(StrEnum):
    """束縛した policy を何に使うか。**あとから広げられない。**"""

    SHADOW = "shadow"
    """比較のためだけに提案を作る。MPC の入力にならない（0053 §2.3）。"""
    ACTIVE = "active"
    """active slot として Supervisor 出力に使う。**いまこの値を持つ束は発行されない。**

    `for_active` が開かない門なので、ここへ到達する経路は存在しない。値を残してあるのは、
    `SupervisorOutputOrigin` との対応と「閉じている」という事実を1か所に置くためである。
    """


ARTIFACT_DETERMINED_METADATA_EXCLUSIONS: frozenset[str] = frozenset(
    {"offline_evaluation_ref", "shadow_evaluation_ref"}
)
"""artifact が決めない Registry metadata の欄。

この2つは lifecycle の途中で Registry 側が書き込む参照（#104 の `mark_validated` /
`promote`）で、artifact bytes からは導けない。**これ以外はすべて artifact が決める**ので、
束縛時に1つ残らず照合する。
"""


def _comparable(value: object) -> object:
    """列挙と列を、由来の違う表現どうしで比べられる形へ落とす。"""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, list | tuple):
        return tuple(_comparable(item) for item in value)
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in sorted(value.items())}
    return value


def _registry_metadata_mismatches(
    derived: SupervisorPolicyRegistryMetadata, stored: BaseModel
) -> list[str]:
    """artifact から導いた metadata と Registry が持つ metadata の食い違いを列挙する。

    **欄を手で並べない。** 並べると、あとから足した欄が照合から漏れる。
    `SupervisorPolicyRegistryMetadata` の欄をすべて回り、artifact が決めないものだけを
    名前で除く（`ARTIFACT_DETERMINED_METADATA_EXCLUSIONS`）。
    """
    missing = object()
    mismatches: list[str] = []
    for name in SupervisorPolicyRegistryMetadata.model_fields:
        if name in ARTIFACT_DETERMINED_METADATA_EXCLUSIONS:
            continue
        actual = getattr(stored, name, missing)
        if actual is missing:
            mismatches.append(name)
            continue
        if _comparable(getattr(derived, name)) != _comparable(actual):
            mismatches.append(name)
    return mismatches


def check_action_space(artifact: SupervisorPolicyArtifact, bounds: SupervisorOutputBounds) -> None:
    """学習時の範囲と runtime の範囲が同じで、全 regime の欄が範囲内であることを確かめる。

    **丸めない。** 範囲外の欄を持つ policy は読み込まない（決定記録 0058 §2.1 と同じ規律）。
    学習時と同じ範囲であることを hash で確かめたうえで、欄ごとにも照合する。hash が
    合っていても、あとから範囲を狭めた設定で読むことはありうる。
    """
    if artifact.manifest.action_space_sha256 != action_space_sha256(bounds):
        raise SupervisorPolicyUnusableError(
            "policy を学習した action 空間が runtime の supervisor.output_bounds と違う"
        )
    for entry in artifact.payload.entries:
        try:
            bounds.validate_context(
                strategy=entry.strategy,
                weights=entry.weights,
                target_band=entry.target_band,
            )
        except ValueError as error:
            raise SupervisorPolicyUnusableError(
                f"policy table の {entry.regime.value} が設定範囲外: {error}"
            ) from error


class SupervisorPolicyBinding:
    """Registry の証拠で裏づけた policy artifact と、それを使ってよい用途の束。

    公開 constructor を持たない。`for_shadow` / `for_active` だけが発行する。
    """

    __slots__ = ("_artifact", "_artifact_sha256", "_attestation", "_authority_stage", "_intent")
    _artifact: SupervisorPolicyArtifact
    _artifact_sha256: str
    _attestation: ArtifactAttestation
    _authority_stage: AuthorityStage
    _intent: PolicyBindingIntent

    def __init__(self) -> None:
        raise TypeError("SupervisorPolicyBinding は for_shadow / for_active からだけ作る")

    def __setattr__(self, name: str, value: object) -> None:
        """発行後に差し替えられないようにする。"""
        raise AttributeError("SupervisorPolicyBinding は不変")

    @classmethod
    def for_shadow(
        cls,
        verified: VerifiedArtifact,
        *,
        expected_policy_version: str,
        bounds: SupervisorOutputBounds,
    ) -> SupervisorPolicyBinding:
        """比較のためだけに使う束を作る。**production pointer を要求しない。**

        shadow の出力は MPC にも Fan にも届かない（0053 §2.3）ので、昇格前の candidate を
        そのまま回せる。回せないと、昇格に要る証拠をそもそも集められない。
        """
        return cls._bind(
            verified,
            expected_policy_version=expected_policy_version,
            bounds=bounds,
            intent=PolicyBindingIntent.SHADOW,
            authority_stage=AuthorityStage.SHADOW,
        )

    @classmethod
    def for_active(
        cls,
        verified: VerifiedArtifact,
        *,
        expected_policy_version: str,
        bounds: SupervisorOutputBounds,
        authority_stage: AuthorityStage,
    ) -> SupervisorPolicyBinding:
        """active slot として使う束を作る。**入力を見ずに必ず拒否する。**

        **artifact の自称を門の条件にしない。** `training_evidence.counterfactual_backed` は
        artifact が自分で書いた値で、証拠ではない（0061 §2.3）。自称で開く門は、
        自称を書き換えれば開く門である。

        検証できる形で裏づけを受け取る手立ては、いまの依存関係では用意できない
        （`control.supervisor` は `control.rl` を import できないので、環境が封をした
        `DynamicsEvidence` の系譜をここで確かめられない）。そして反実仮想能力を申告した
        thermal artifact は1つも存在しないので、**いま通してよい policy もそもそも無い**
        （決定記録 0048 §2.1 / 0052 §2.1 / 0058 §3）。

        したがって、ここは**開かない門**である。開く条件——何をもって裏づけとみなし、
        誰がそれを発行するか——は未決で、決定記録と所有者の承認が要る（0061 §5）。
        """
        del verified, expected_policy_version, bounds, authority_stage
        raise SupervisorPolicyUnusableError(
            "RL Supervisor policy を active slot へ束縛できない。"
            "反実仮想の裏づけを検証できる経路がまだ無く、artifact の自称は根拠にしない"
            "（決定記録 0048 §2.1 / 0058 §3 / 0061 §2.4）"
        )

    @classmethod
    def _bind(
        cls,
        verified: VerifiedArtifact,
        *,
        expected_policy_version: str,
        bounds: SupervisorOutputBounds,
        intent: PolicyBindingIntent,
        authority_stage: AuthorityStage,
    ) -> SupervisorPolicyBinding:
        if type(verified) is not VerifiedArtifact:
            # 呼び出し側が組み立てた「検証済みに見える」object を受け取らない。
            raise SupervisorPolicyUnusableError(
                "#104 の VerifiedArtifact だけから policy を束縛できる"
            )
        attestation = verified.attestation
        if attestation.kind is not ArtifactKind.SUPERVISOR_POLICY:
            raise SupervisorPolicyUnusableError(
                f"supervisor policy 以外の artifact を Supervisor へ束縛しない"
                f"（kind={attestation.kind.value}）"
            )
        if attestation.capability is not ArtifactCapability.SUPERVISOR_STRATEGY:
            # **登録時に申告された能力だけを見る。** thermal model を取り違えて渡す経路を塞ぐ。
            raise SupervisorPolicyUnusableError(
                "Supervisor 戦略を申告していない artifact を policy にしない"
                f"（attested capability={attestation.capability.value}）"
            )
        artifact = cls._parse(verified)
        manifest = artifact.manifest
        if (attestation.model_id, attestation.version) != (
            manifest.model_id,
            manifest.model_version,
        ):
            raise SupervisorPolicyUnusableError("attestation と manifest の identity が一致しない")
        if attestation.feature_schema_version != manifest.state_schema_version:
            raise SupervisorPolicyUnusableError("state schema version が Registry と一致しない")
        if attestation.target_schema_version != manifest.action_schema_version:
            raise SupervisorPolicyUnusableError("action schema version が Registry と一致しない")
        if attestation.version != expected_policy_version:
            raise SupervisorPolicyUnusableError(
                "policy artifact の版が設定の rl_version と違う"
                f"（expected={expected_policy_version}; attested={attestation.version}）"
            )
        if authority_stage not in attestation.authority_compatibility:
            raise SupervisorPolicyUnusableError(
                "Registry が検証した artifact は要求 authority stage と互換でない"
                f"（stage={authority_stage.value}）"
            )
        check_action_space(artifact, bounds)
        binding = object.__new__(cls)
        object.__setattr__(binding, "_artifact", artifact)
        object.__setattr__(
            binding, "_artifact_sha256", hashlib.sha256(verified.payload).hexdigest()
        )
        object.__setattr__(binding, "_attestation", attestation)
        object.__setattr__(binding, "_authority_stage", authority_stage)
        object.__setattr__(binding, "_intent", intent)
        return binding

    @staticmethod
    def _parse(verified: VerifiedArtifact) -> SupervisorPolicyArtifact:
        """検証済み bytes を canonical JSON として読み、metadata と突き合わせる。"""
        payload = verified.payload
        if len(payload) > MAX_POLICY_ARTIFACT_BYTES:
            raise SupervisorPolicyUnusableError("policy artifact が安全な size 上限を超えている")
        if is_pickle_payload(payload):
            raise SupervisorPolicyUnusableError("pickle 形式の policy artifact は読み込まない")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != verified.attestation.artifact_sha256:
            raise SupervisorPolicyUnusableError("policy artifact の checksum が attestation と違う")
        try:
            artifact = SupervisorPolicyArtifact.model_validate_json(payload)
        except ValueError as error:
            raise SupervisorPolicyUnusableError(f"policy artifact を読めない: {error}") from error
        if payload != canonical_policy_artifact_bytes(artifact):
            raise SupervisorPolicyUnusableError(
                "policy artifact は canonical JSON bytes に限定する"
            )
        try:
            derived = policy_registry_metadata(artifact, payload)
        except ValueError as error:  # pragma: no cover - 上の canonical 判定で先に落ちる
            raise SupervisorPolicyUnusableError(str(error)) from error
        stored = verified.metadata
        mismatches = _registry_metadata_mismatches(derived, stored)
        if mismatches:
            # Registry が持つ metadata と artifact の中身が食い違ったまま束縛しない。
            # **artifact が決める欄は1つ残らず照合する。** 一部だけ見ていると、正しい bytes を
            # 登録しながら metadata だけ書き換えた artifact が通ってしまう。
            raise SupervisorPolicyUnusableError(
                f"policy artifact の identity が Registry metadata と一致しない: {mismatches}"
            )
        return artifact

    @property
    def artifact(self) -> SupervisorPolicyArtifact:
        """束縛した policy artifact。"""
        return self._artifact

    @property
    def attestation(self) -> ArtifactAttestation:
        """Registry が発行した証拠。"""
        return self._attestation

    @property
    def intent(self) -> PolicyBindingIntent:
        """この束縛を作った用途。"""
        return self._intent

    @property
    def authority_stage(self) -> AuthorityStage:
        """束縛時に要求した実効 stage。"""
        return self._authority_stage

    @property
    def identity(self) -> SupervisorPolicyIdentity:
        """束縛した artifact の**完全な識別**（Registry が検証した model ID・版・bytes hash）。"""
        return SupervisorPolicyIdentity(
            model_id=self._attestation.model_id,
            version=self._attestation.version,
            artifact_sha256=self._artifact_sha256,
        )

    @property
    def origin(self) -> SupervisorOutputOrigin:
        """この束から出る提案が名乗る用途。**束縛の意図をそのまま写す。**"""
        if self._intent is PolicyBindingIntent.ACTIVE:  # pragma: no cover - 門が閉じている
            return SupervisorOutputOrigin.ACTIVE_BINDING
        return SupervisorOutputOrigin.SHADOW_BINDING

    @property
    def model_version(self) -> str:
        """`ControlState` や trace に載せられる一意な値。"""
        return self._attestation.model_version

    def conditions(self) -> dict[str, object]:
        """比較・trace に載せる、path を含まない束縛条件。

        **「期待する版」だけでは policy を特定できない。** 同じ版を名乗る別の bytes も、
        別の action 空間で学習した表も、同じ入力から違う戦略を出す（決定記録 0058 §2.6）。
        """
        manifest = self._artifact.manifest
        conditions: dict[str, object] = dict(self._attestation.trace_metadata())
        conditions.update(
            {
                "binding_intent": self._intent.value,
                "authority_stage": self._authority_stage.value,
                "policy_family": manifest.policy_family,
                "policy_artifact_sha256": self._artifact_sha256,
                "action_space_sha256": manifest.action_space_sha256,
                "rl_training_config_sha256": manifest.rl_training_config_sha256,
                "rl_policy_config_sha256": manifest.rl_policy_config_sha256,
                "reward_version": manifest.reward_version,
                "training_conditions_sha256": manifest.training_evidence.conditions_sha256,
                "counterfactual_backed": manifest.training_evidence.counterfactual_backed,
                "learned_controller_available": (
                    manifest.training_evidence.learned_controller_available
                ),
            }
        )
        return conditions


class RegimeTableRlPolicy:
    """`WorkloadRegime` から戦略を引く決定論的な RL Supervisor policy（#88 `RLPolicy`）。

    **Demand を返さない。** 返すのは `SupervisorOutput` だけで、demand を作るのは
    Learned MPC と Controller Gate である。
    """

    kind = SupervisorPolicyKind.RL

    __slots__ = ("_artifact", "_binding", "_clock", "_verification")

    def __init__(
        self,
        artifact: SupervisorPolicyArtifact,
        clock: Clock,
        *,
        binding: SupervisorPolicyBinding | None = None,
        _token: object | None = None,
    ) -> None:
        if _token is not _BINDING_TOKEN:
            raise TypeError("RegimeTableRlPolicy は from_binding / offline からだけ作る")
        self._artifact = artifact
        self._clock = clock
        self._binding = binding
        self._verification = (
            PolicyArtifactVerification.REGISTRY_VERIFIED
            if binding is not None
            else PolicyArtifactVerification.OFFLINE_UNVERIFIED
        )

    @classmethod
    def from_binding(cls, binding: SupervisorPolicyBinding, clock: Clock) -> Self:
        """Registry の証拠で裏づけた束から作る。"""
        return cls(binding.artifact, clock, binding=binding, _token=_BINDING_TOKEN)

    @classmethod
    def offline(
        cls,
        artifact: SupervisorPolicyArtifact,
        clock: Clock,
        *,
        bounds: SupervisorOutputBounds,
    ) -> Self:
        """学習・offline 評価のために、Registry を通さず artifact から作る。

        **運転の policy slot へ渡さない。** `verification` は `offline_unverified` のままで、
        `binding` を持たないので `SupervisorPolicyBinding` を要求する経路には入れない。
        範囲の照合だけは束縛時と同じ規則で行う（範囲外の表を評価に使わない）。
        """
        check_action_space(artifact, bounds)
        return cls(artifact, clock, binding=None, _token=_BINDING_TOKEN)

    @property
    def version(self) -> str:
        """trace と rollback に使う policy version（artifact の版）。"""
        return self._artifact.manifest.model_version

    @property
    def verification(self) -> PolicyArtifactVerification:
        """bytes が #104 の検証境界を越えたか。"""
        return self._verification

    @property
    def binding(self) -> SupervisorPolicyBinding | None:
        """Registry の証拠。offline の instance では `None`。"""
        return self._binding

    @property
    def artifact(self) -> SupervisorPolicyArtifact:
        """束縛した policy artifact。"""
        return self._artifact

    @property
    def identity(self) -> SupervisorPolicyIdentity:
        """この policy の artifact の**完全な識別**。version だけでは同じ版の別物と区別できない。

        Registry を通した instance は attestation の値、`offline` は artifact の canonical
        bytes から自分で導いた値（bytes hash は同じ規則で作るので、束縛した同じ artifact と
        同じ値になる）。
        """
        if self._binding is not None:
            return self._binding.identity
        manifest = self._artifact.manifest
        return SupervisorPolicyIdentity(
            model_id=manifest.model_id,
            version=manifest.model_version,
            artifact_sha256=hashlib.sha256(
                canonical_policy_artifact_bytes(self._artifact)
            ).hexdigest(),
        )

    @property
    def origin(self) -> SupervisorOutputOrigin:
        """この policy の出力が名乗る用途。

        Registry を通さずに作った instance（`offline`）は `unverified` で、
        **Coordinator の active slot を通らない**（決定記録 0061 §2.4）。
        """
        if self._binding is None:
            return SupervisorOutputOrigin.UNVERIFIED
        return self._binding.origin

    def deliver(
        self,
        policy_input: SupervisorInput,
        *,
        received_monotonic_ms: int,
    ) -> ReceivedSupervisorOutput:
        """提案を、**用途を付けた**受信済みの形で返す。

        worker から control loop へ渡す値をこの1か所で組み立てる。呼び出し側が
        `origin` を選べないので、shadow 用に束縛した policy の提案が
        `active_binding` を名乗って active slot へ届くことがない。
        """
        return ReceivedSupervisorOutput(
            output=self.propose(policy_input),
            source_monotonic_ms=policy_input.snapshot.monotonic_ms,
            received_monotonic_ms=received_monotonic_ms,
            identity=self.identity,
            origin=self.origin,
        )

    def propose(self, policy_input: SupervisorInput) -> SupervisorOutput:
        """観測した regime に対応する戦略 context を返す。**Demand を含まない。**"""
        workload = policy_input.workload
        entry = self._artifact.entry(workload.regime)
        snapshot = policy_input.snapshot
        return SupervisorOutput(
            snapshot_schema_version=snapshot.schema_version,
            tick_id=snapshot.tick_id,
            ts_ms=snapshot.ts_ms,
            policy=self.kind,
            version=self.version,
            regime=workload.regime,
            regime_confidence=workload.confidence,
            weights=entry.weights,
            strategy=entry.strategy,
            target_band=entry.target_band,
            computed_at_ms=self._clock.now_ms(),
        )
