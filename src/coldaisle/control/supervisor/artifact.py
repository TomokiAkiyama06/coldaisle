"""RL Supervisor policy artifact の形と版（#89 / 決定記録 0061）。

**この artifact は Fan Demand を表現できない。** 中身は `WorkloadRegime` ごとの
strategy / objective weight / target band（= #88 `SupervisorOutput` の戦略部分）だけで、
`Demand` も `EffectiveZoneDemand` も PWM も鍵として受け付けない（`extra="forbid"`）。
action から demand への写像は Learned MPC（#86）と Controller Gate（#79 / #85）が持つ
（AGENTS.md ルール1 / 2、決定記録 0027 / 0028 §2.3）。

**manifest の training 欄は自称である。** 「どの episode で選んだか」「反実仮想の裏づけが
あったか」は artifact が自分で書いた値で、証拠ではない。証拠は

- Registry（#104）が発行する `ArtifactAttestation`（bytes・identity・lifecycle）
- 学習時の `EpisodeResult.promotable`（封をした `DynamicsEvidence` から環境だけが立てる。
  決定記録 0058 §2.3）
- 人の承認（promotion は #104 が `HumanApproval` を要求する）

の3つである。自称に対してこの module がやれるのは**整合の強制**だけで、次の2つを型で縛る。

1. 反実仮想の裏づけを名乗らない artifact は、**`SHADOW` 以外の authority を名乗れない**
2. 反実仮想の裏づけを名乗るには、`learned_controller_available` と
   「全 episode が promotable」の両方が要る（片方だけでは名乗れない）

いまは反実仮想能力を申告した thermal artifact が1つも無い（決定記録 0048 §2.1 / 0052 §2.1 /
0058 §3）ので、**正直に作られた policy artifact は必ず `SHADOW` だけになる。**
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coldaisle.control.config import SupervisorOutputBounds
from coldaisle.control.model.thermal import canonical_json_bytes, canonical_sha256
from coldaisle.control.model_registry import (
    MODEL_REGISTRY_SCHEMA_VERSION,
    ArtifactCapability,
)
from coldaisle.control.schema import (
    STAGE_ORDER,
    AuthorityStage,
    SupervisorObjectiveWeights,
    SupervisorTargetBand,
    WorkloadRegime,
)

POLICY_ARTIFACT_SCHEMA_VERSION: Literal[1] = 1
"""`SupervisorPolicyArtifact` の形の版。**欄の意味を変えたら上げる。**"""

POLICY_STATE_SCHEMA_VERSION: Literal["supervisor-state-v1"] = "supervisor-state-v1"
"""policy が読む state の版。#88 `SupervisorInput` の `WorkloadRegime` に対応する。

Registry の `feature_schema_version` へそのまま写す。state の作り方を変えたらここを上げ、
古い artifact が新しい runtime へ束縛されないようにする。
"""

POLICY_ACTION_SCHEMA_VERSION: Literal["supervisor-action-v1"] = "supervisor-action-v1"
"""policy が出す action の版。#88 `SupervisorOutput` の戦略部分に対応する。

Registry の `target_schema_version` へそのまま写す。
"""

POLICY_FAMILY: Literal["regime_table_v1"] = "regime_table_v1"
"""いまの唯一の policy family。**実行可能な object を含まない決定論的な表**である。

`WorkloadRegime` を鍵に strategy / weights / target band を引く。表にしているのは、

- 反実仮想モデルが無い以上、より複雑な関数近似を**測れない**（決定記録 0058 §3）
- 表なら JSON として封じられ、pickle も任意コードも読み込まずに済む（#104 の制約）
- Rule policy（#88 の `WorkloadPolicyContexts`）と同じ鍵なので、同じ episode 群で直接比べられる

ためである。より豊かな state を使う family を足すときは、この値を増やして版で分ける。
"""

MAX_POLICY_ARTIFACT_BYTES = 1 * 1024 * 1024
"""artifact bytes の構造上限。未信頼な入力による資源枯渇を防ぐ境界で、調整値ではない。"""

MAX_TRAINING_EPISODES = 1_024
"""manifest が名指しできる学習 episode の構造上限。"""

_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.-]*$"
_SEMVER_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

ModelId = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN, max_length=120)]
SemanticVersion = Annotated[str, Field(pattern=_SEMVER_PATTERN, max_length=80)]
Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]
LabelName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
EpisodeId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=120)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _canonical_regimes() -> tuple[WorkloadRegime, ...]:
    """すべての regime を、値の昇順という1つの並びで返す。"""
    return tuple(sorted(WorkloadRegime, key=lambda regime: regime.value))


class RegimeActionEntry(_Frozen):
    """1 regime に対する Supervisor の戦略。**Demand の欄を持たない。**"""

    regime: WorkloadRegime
    strategy: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    weights: SupervisorObjectiveWeights
    target_band: SupervisorTargetBand


class RegimeTablePayload(_Frozen):
    """`WorkloadRegime` → 戦略の決定論的な表。**実行可能な内容を持たない。**"""

    policy_family: Literal["regime_table_v1"] = POLICY_FAMILY
    entries: tuple[RegimeActionEntry, ...] = Field(
        min_length=len(WorkloadRegime), max_length=len(WorkloadRegime)
    )

    @model_validator(mode="after")
    def _table_covers_every_regime_exactly_once(self) -> Self:
        """**未知の regime を既定の欄へ倒さない**（AGENTS.md ルール4）。

        既定値や部分的な表を許すと、`UNKNOWN` や新しい regime が「通常時の戦略」に落ちる。
        並びも1つに固定する。同じ表が2通りの bytes を持つと、hash が policy を特定できない。
        """
        regimes = tuple(entry.regime for entry in self.entries)
        if regimes != _canonical_regimes():
            raise ValueError("policy table は全 regime を値の昇順で1つずつ持つ")
        return self

    def entry(self, regime: WorkloadRegime) -> RegimeActionEntry:
        """regime に対応する欄を返す。表は全 regime を覆うので必ず見つかる。"""
        for item in self.entries:
            if item.regime is regime:
                return item
        raise KeyError(regime)  # pragma: no cover - 上の不変条件で到達しない


class PolicySearchHyperparameters(_Frozen):
    """policy を選んだ探索の再現に要る値。**乱数の種を必ず持つ。**"""

    search_family: LabelName
    seed: int = Field(ge=0)
    candidates_evaluated: int = Field(ge=1)
    episodes_per_candidate: int = Field(ge=1)


class PolicyTrainingEvidence(_Frozen):
    """学習時に何が成り立っていたかの**自称**。証拠そのものではない。

    ここにある値は artifact が自分で書いたもので、読む側は Registry の attestation と
    学習報告（`SupervisorPolicyTrainingReport`）で裏を取る。この型が縛れるのは
    **自称どうしの整合**だけである（決定記録 0058 §2.3 と同じ考え方）。
    """

    training_mode: LabelName
    """`TrainingMode` の値をそのまま写した**ラベル**（`logged` / `learned_simulator` / `hybrid`）。

    `control.rl` を import すると supervisor → rl → supervisor の循環になるので、列挙では
    なく文字列で持つ。**照合にも昇格判断にも使わない。** 読む側への表示のためだけにある。
    """
    dynamics_provenance: LabelName
    """`DynamicsProvenance` の値を写したラベル。上と同じく表示のためだけにある。"""
    learned_controller_available: bool
    """学習中に Learned MPC を束縛できていたか（決定記録 0058 §2.1）。

    **`False` なら、その学習で Supervisor action は demand に一切効いていない。**
    """
    counterfactual_backed: bool
    """反実仮想の裏づけのある episode だけで選ばれたか。

    **`False` の artifact は `SHADOW` 以外の authority を名乗れない**（下の不変条件）。
    """
    promotable_episodes: int = Field(ge=0)
    total_episodes: int = Field(ge=1)
    improved_over_baseline: bool
    baseline_policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    conditions_sha256: Sha256
    """学習に使った episode 群の条件 hash（`PolicyComparison.conditions_sha256`）。

    policy は条件に入らない（決定記録 0058 §2.6）ので、この値は
    **「どの条件で Rule と比べたか」**を指す。
    """

    @model_validator(mode="after")
    def _claims_do_not_contradict_each_other(self) -> Self:
        if self.promotable_episodes > self.total_episodes:
            raise ValueError("昇格可能 episode 数が総数を超えている")
        if not self.counterfactual_backed:
            return self
        if not self.learned_controller_available:
            # action が demand に効いていない学習を「反実仮想の裏づけあり」と書かせない。
            raise ValueError("Learned MPC を束縛できていない学習を反実仮想の裏づけありにしない")
        if self.promotable_episodes != self.total_episodes:
            # 一部だけ裏づけのある episode 群を「全部裏づけあり」と読まない（0058 §2.3 条件7）。
            raise ValueError("昇格可能でない episode を含む学習を反実仮想の裏づけありにしない")
        if not self.improved_over_baseline:
            raise ValueError("Baseline を上回っていない学習を反実仮想の裏づけありにしない")
        return self


class SupervisorPolicyManifest(_Frozen):
    """artifact の identity・互換性・出どころ。**Registry metadata と1対1に写せる。**"""

    model_id: ModelId
    model_version: SemanticVersion
    created_at: str = Field(min_length=1, max_length=64)
    capability: Literal[ArtifactCapability.SUPERVISOR_STRATEGY] = (
        ArtifactCapability.SUPERVISOR_STRATEGY
    )
    """Registry へ申告する能力。**thermal model の能力を名乗れない。**

    `Literal` で固定しているので、この artifact が `counterfactual_action` を名乗って
    Learned MPC の内部モデルとして束縛される経路は型の段階で無い（決定記録 0052 §2.1）。
    """
    policy_family: Literal["regime_table_v1"] = POLICY_FAMILY
    state_schema_version: Literal["supervisor-state-v1"] = POLICY_STATE_SCHEMA_VERSION
    action_schema_version: Literal["supervisor-action-v1"] = POLICY_ACTION_SCHEMA_VERSION
    action_space_sha256: Sha256
    """学習時の `supervisor.output_bounds` の hash（`action_space_sha256()`）。

    **束縛時に runtime の範囲と照合する。** 別の範囲で学習した policy を読み込むと、
    学習で最適だった action が運転時に Supervisor へ拒否される（決定記録 0058 §2.1）。
    """
    rl_training_config_sha256: Sha256
    """学習環境の設定（`config/rl-training.yaml`）の bytes hash。"""
    rl_policy_config_sha256: Sha256
    """探索の設定（`config/rl-policy.yaml`）の bytes hash。"""
    reward_version: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=64)
    training_episode_ids: tuple[EpisodeId, ...] = Field(
        min_length=1, max_length=MAX_TRAINING_EPISODES
    )
    training_evidence: PolicyTrainingEvidence
    hyperparameters: PolicySearchHyperparameters
    payload_sha256: Sha256
    code_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    authority_compatibility: tuple[AuthorityStage, ...] = Field(
        min_length=1, max_length=len(STAGE_ORDER)
    )

    @field_validator("created_at")
    @classmethod
    def _created_at_is_timezone_aware_rfc3339(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("created_at は RFC 3339 形式にする") from error
        if parsed.utcoffset() is None:
            raise ValueError("created_at には timezone が必要")
        return value

    @model_validator(mode="after")
    def _identity_and_authority_match_the_evidence(self) -> Self:
        identifiers = self.training_episode_ids
        if identifiers != tuple(sorted(set(identifiers))):
            # **数を入力の回数で増やせないようにする。** 同じ episode を2度名指しして
            # `total_episodes` を膨らませる経路を、識別子の一意性で塞ぐ。
            raise ValueError("training_episode_ids は重複なし昇順にする")
        if self.training_evidence.total_episodes != len(identifiers):
            raise ValueError("学習 episode 数が名指しした識別子の数と一致しない")
        stages = self.authority_compatibility
        if len(set(stages)) != len(stages):
            raise ValueError("authority_compatibility は重複させない")
        if stages != tuple(stage for stage in STAGE_ORDER if stage in set(stages)):
            raise ValueError("authority_compatibility は低い stage から順に並べる")
        if not self.training_evidence.counterfactual_backed and stages != (AuthorityStage.SHADOW,):
            # 反実仮想の裏づけが無い policy に SHADOW より上を名乗らせない。
            # いまは裏づけのある artifact が存在しないので、**ここが常に効く**（0058 §3）。
            raise ValueError("反実仮想の裏づけの無い policy artifact は SHADOW 互換だけを名乗る")
        return self


class SupervisorPolicyArtifact(_Frozen):
    """完全な非実行 JSON policy artifact。**PWM も Demand も表現できない。**"""

    schema_name: Literal["coldaisle.supervisor_policy"] = "coldaisle.supervisor_policy"
    schema_version: Literal[1] = POLICY_ARTIFACT_SCHEMA_VERSION
    manifest: SupervisorPolicyManifest
    payload: RegimeTablePayload

    @model_validator(mode="after")
    def _manifest_names_this_payload(self) -> Self:
        if self.manifest.policy_family != self.payload.policy_family:
            raise ValueError("manifest と payload の policy family が一致しない")
        if self.manifest.payload_sha256 != canonical_sha256(self.payload):
            # 表だけ差し替えた artifact を、同じ identity のまま読み込ませない。
            raise ValueError("payload checksum が manifest と一致しない")
        return self

    def entry(self, regime: WorkloadRegime) -> RegimeActionEntry:
        """regime に対応する戦略を返す。"""
        return self.payload.entry(regime)


class SupervisorPolicyRegistryMetadata(_Frozen):
    """#104 `ArtifactMetadata` へ1対1で写せる値。**artifact bytes から導く。**"""

    schema_version: Literal[3] = MODEL_REGISTRY_SCHEMA_VERSION
    kind: Literal["supervisor_policy"] = "supervisor_policy"
    artifact_format: Literal["json"] = "json"
    capability: Literal[ArtifactCapability.SUPERVISOR_STRATEGY] = (
        ArtifactCapability.SUPERVISOR_STRATEGY
    )
    model_id: ModelId
    version: SemanticVersion
    created_at: str
    training_dataset_version: str = Field(min_length=1, max_length=200)
    """学習に使った episode 群を指す値。`rl-episodes:<条件 hash>` を入れる。

    policy の学習データは Thermal Dataset ではなく **episode 群**なので、
    その条件 hash（policy を含まない。決定記録 0058 §2.6）で指す。
    """
    source_runs: tuple[EpisodeId, ...] = Field(min_length=1, max_length=MAX_TRAINING_EPISODES)
    feature_schema_version: Literal["supervisor-state-v1"] = POLICY_STATE_SCHEMA_VERSION
    target_schema_version: Literal["supervisor-action-v1"] = POLICY_ACTION_SCHEMA_VERSION
    code_commit: str | None
    sha256: Sha256
    model_family: Literal["regime_table_v1"] = POLICY_FAMILY
    hyperparameters: dict[str, str | int | float | bool | None] = Field(max_length=16)
    offline_evaluation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    shadow_evaluation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    authority_compatibility: tuple[AuthorityStage, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _source_runs_are_unique(self) -> Self:
        if self.source_runs != tuple(sorted(set(self.source_runs))):
            raise ValueError("Registry source run は重複なし昇順にする")
        return self


def action_space_sha256(bounds: SupervisorOutputBounds) -> str:
    """`supervisor.output_bounds` そのものの SHA-256。

    **欄を数え上げずに範囲全体を覆う。** 数え上げると、あとから足した欄が hash に入らず、
    違う範囲で学習した policy が同じ hash を名乗れてしまう。
    """
    return canonical_sha256(bounds)


_CERTIFICATION_TOKEN = object()


class CertifiedPolicyArtifact:
    """**設定から作り直して照合した** policy artifact（`SupervisorPolicyTrainer.certify()`）。

    登録用の関数（`canonical_policy_artifact_bytes` / `policy_registry_metadata`）は
    **この型だけ**を受け取る。学習報告は JSON として書き換えられるので、報告の中の値だけで
    照合した artifact を登録させない（決定記録 0061 §2.6）。

    `__init__` は構築用の token が無ければ拒む。手で token を持ち出す偽造は同一プロセス内では
    可能で、0050 §3 と同じ残余リスクである。狙いは配線の誤りを型で止めることである。
    """

    __slots__ = (
        "_action_space_sha256",
        "_artifact",
        "_rl_policy_config_sha256",
        "_rl_training_config_sha256",
    )
    _artifact: SupervisorPolicyArtifact
    _rl_policy_config_sha256: str
    _rl_training_config_sha256: str
    _action_space_sha256: str

    def __init__(
        self,
        artifact: SupervisorPolicyArtifact,
        *,
        rl_policy_config_sha256: str,
        rl_training_config_sha256: str,
        action_space_sha256: str,
        _token: object | None = None,
    ) -> None:
        """`SupervisorPolicyTrainer.certify()` からだけ作る。"""
        if _token is not _CERTIFICATION_TOKEN:
            raise TypeError(
                "CertifiedPolicyArtifact は SupervisorPolicyTrainer.certify() からだけ作る"
            )
        manifest = artifact.manifest
        if (
            manifest.rl_policy_config_sha256,
            manifest.rl_training_config_sha256,
            manifest.action_space_sha256,
        ) != (rl_policy_config_sha256, rl_training_config_sha256, action_space_sha256):
            raise ValueError("認証に使った設定の hash が artifact の manifest と一致しない")
        object.__setattr__(self, "_artifact", artifact)
        object.__setattr__(self, "_rl_policy_config_sha256", rl_policy_config_sha256)
        object.__setattr__(self, "_rl_training_config_sha256", rl_training_config_sha256)
        object.__setattr__(self, "_action_space_sha256", action_space_sha256)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("CertifiedPolicyArtifact は不変")

    @property
    def artifact(self) -> SupervisorPolicyArtifact:
        """照合済みの artifact。"""
        return self._artifact

    @property
    def rl_policy_config_sha256(self) -> str:
        """照合に使った `rl-policy.yaml` の hash。"""
        return self._rl_policy_config_sha256

    @property
    def rl_training_config_sha256(self) -> str:
        """照合に使った `rl-training.yaml` の hash。"""
        return self._rl_training_config_sha256

    @property
    def action_space_sha256(self) -> str:
        """照合に使った `supervisor.output_bounds` の hash。"""
        return self._action_space_sha256


def _issue_certified_policy_artifact(
    artifact: SupervisorPolicyArtifact,
    *,
    rl_policy_config_sha256: str,
    rl_training_config_sha256: str,
    action_space_sha256: str,
) -> CertifiedPolicyArtifact:
    """`SupervisorPolicyTrainer.certify()` 専用の発行口。**ほかから呼ばない。**"""
    return CertifiedPolicyArtifact(
        artifact,
        rl_policy_config_sha256=rl_policy_config_sha256,
        rl_training_config_sha256=rl_training_config_sha256,
        action_space_sha256=action_space_sha256,
        _token=_CERTIFICATION_TOKEN,
    )


def _require_certified(certified: object) -> SupervisorPolicyArtifact:
    if not isinstance(certified, CertifiedPolicyArtifact):
        # **照合していない artifact を登録の道へ入れない。**
        raise TypeError(
            "登録には SupervisorPolicyTrainer.certify() を通した CertifiedPolicyArtifact を渡す"
        )
    return certified.artifact


def canonical_policy_artifact_bytes(certified: CertifiedPolicyArtifact) -> bytes:
    """#104 へ保存する canonical JSON bytes を返す。**照合済みの artifact だけ。**"""
    return _policy_artifact_bytes(_require_certified(certified))


def policy_registry_metadata(
    certified: CertifiedPolicyArtifact,
    artifact_bytes: bytes,
    *,
    offline_evaluation_ref: str | None = None,
    shadow_evaluation_ref: str | None = None,
) -> SupervisorPolicyRegistryMetadata:
    """#104 登録用 metadata を作る。**照合済みの artifact だけ。**"""
    return _derive_policy_registry_metadata(
        _require_certified(certified),
        artifact_bytes,
        offline_evaluation_ref=offline_evaluation_ref,
        shadow_evaluation_ref=shadow_evaluation_ref,
    )


def _policy_artifact_bytes(artifact: SupervisorPolicyArtifact) -> bytes:
    """canonical JSON bytes（登録済み artifact の**検証**にも使う。登録の道ではない）。"""
    validated = SupervisorPolicyArtifact.model_validate(artifact.model_dump(mode="python"))
    return canonical_json_bytes(validated)


def _derive_policy_registry_metadata(
    artifact: SupervisorPolicyArtifact,
    artifact_bytes: bytes,
    *,
    offline_evaluation_ref: str | None = None,
    shadow_evaluation_ref: str | None = None,
) -> SupervisorPolicyRegistryMetadata:
    """**渡された bytes そのもの**から metadata を導く（束縛時の照合にも使う）。

    bytes と artifact が食い違っていれば作らない。食い違いを許すと、登録される bytes と
    metadata が別物になり、attestation が別の内容を名指すことになる。
    """
    if len(artifact_bytes) > MAX_POLICY_ARTIFACT_BYTES:
        raise ValueError("policy artifact が安全な size 上限を超えている")
    parsed = SupervisorPolicyArtifact.model_validate_json(artifact_bytes)
    if parsed != artifact:
        raise ValueError("Registry へ渡す bytes が指定 artifact と一致しない")
    if artifact_bytes != _policy_artifact_bytes(parsed):
        raise ValueError("Registry へ渡す artifact は canonical JSON bytes に限定する")
    manifest = parsed.manifest
    evidence = manifest.training_evidence
    return SupervisorPolicyRegistryMetadata(
        model_id=manifest.model_id,
        version=manifest.model_version,
        created_at=manifest.created_at,
        training_dataset_version=f"rl-episodes:{evidence.conditions_sha256}",
        source_runs=manifest.training_episode_ids,
        code_commit=manifest.code_commit,
        sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        hyperparameters={
            "search_family": manifest.hyperparameters.search_family,
            "seed": manifest.hyperparameters.seed,
            "candidates_evaluated": manifest.hyperparameters.candidates_evaluated,
            "episodes_per_candidate": manifest.hyperparameters.episodes_per_candidate,
            "reward_version": manifest.reward_version,
            "training_mode": evidence.training_mode,
            "learned_controller_available": evidence.learned_controller_available,
            "counterfactual_backed": evidence.counterfactual_backed,
        },
        offline_evaluation_ref=offline_evaluation_ref,
        shadow_evaluation_ref=shadow_evaluation_ref,
        authority_compatibility=manifest.authority_compatibility,
    )


def policy_registry_metadata_json_bytes(metadata: SupervisorPolicyRegistryMetadata) -> bytes:
    """#104 `ArtifactMetadata` へ渡す strict JSON の橋渡しを返す。"""
    validated = SupervisorPolicyRegistryMetadata.model_validate(metadata.model_dump(mode="python"))
    return canonical_json_bytes(validated)
