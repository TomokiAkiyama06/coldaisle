"""Filesystem based Control Model Registry (#104).

The registry selects and verifies model artifacts.  It deliberately never deserializes or
executes them: consumers receive an immutable byte snapshot only after checksum and schema
compatibility checks.  Fan control, fallback selection, and authority changes remain outside
this module.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_UN, flock
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    FailFast,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from coldaisle.clock import Clock, WallClock
from coldaisle.control.schema import AuthorityStage

MODEL_REGISTRY_SCHEMA_VERSION: Literal[2] = 2
MODEL_REGISTRY_CONFIG_FILENAME = "model-registry.yaml"

_STATE_FILENAME = "registry.json"
_LOCK_FILENAME = ".registry.lock"
_ARTIFACT_FILENAME = "artifact.payload"
_READ_CHUNK_BYTES = 1024 * 1024
# One JSON lexical unit relevant to structure: a string, a bracket, or a scalar run.
# The string branch always matches (possessively) up to its closing quote *or* the end
# of input, and group 1 is empty when unterminated.  Otherwise a failed string match
# would be retried at every later quote, making the scan quadratic.
# Commas, colons and whitespace are skipped; json.loads() still checks full syntax.
_JSON_STRUCTURE_TOKEN = re.compile(
    rb'"[^"\\]*+(?:\\.[^"\\]*+)*+("?)|[\[\]{}]|[^\s\[\]{},:"]++',
    re.DOTALL,
)

_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.-]*$"
_SEMVER_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _Frozen(BaseModel):
    """Reject unknown fields and keep registry records immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ArtifactKind(StrEnum):
    """Artifact roles managed independently by the registry."""

    THERMAL_MODEL = "thermal_model"
    CONFIDENCE_MODEL = "confidence_model"
    SUPERVISOR_POLICY = "supervisor_policy"
    FEATURE_TRANSFORM = "feature_transform"


class ArtifactCapability(StrEnum):
    """artifact が主張できる科学的な能力。deployment lifecycle の状態とは別（#84 / #86）。

    **登録時に申告する。** 推論器が自分で名乗る値ではなく、Registry の metadata が持ち、
    検証経路が発行する attestation に載る。#86 はこの値だけを見て内部モデルの可否を決める。
    """

    OBSERVATIONAL_REPLAY = "observational_replay"
    """観測の再生だけ。**反実仮想予測は主張しない**（決定記録 0048 §2.1）。"""
    COUNTERFACTUAL_ACTION = "counterfactual_action"
    """候補 Fan action 列に対する将来観測の予測。#86 が要求する（決定記録 0052 §2.1）。"""
    SUPERVISOR_STRATEGY = "supervisor_strategy"
    """`WorkloadRegime` から Supervisor の戦略・目的関数 weight・target band への写像（#89）。

    **thermal model の能力ではない。** 将来観測を1つも予測しないので、#86 の
    `COUNTERFACTUAL_CAPABILITIES` にも `AttestedThermalDynamics.bind` の要求にも入らず、
    この能力を申告した artifact が MPC の内部モデルや学習 dynamics として束縛される経路は
    無い。逆に #89 の束縛はこの能力だけを受け付けるので、thermal artifact を
    `supervisor_policy` として取り違えて渡す配線ミスも型で止まる（決定記録 0061 §2.1）。
    """


class ArtifactFormat(StrEnum):
    """Non-executable interchange formats accepted by the registry.

    Pickle, joblib, framework-native Python checkpoints, and binary formats without an installed
    structural validator are intentionally absent.  A future schema version may add ONNX or
    safetensors together with a non-executing validator.
    """

    JSON = "json"


class ArtifactStatus(StrEnum):
    """Persistent lifecycle state of an artifact."""

    CANDIDATE = "candidate"
    VALIDATED = "validated"
    PRODUCTION = "production"
    RETIRED = "retired"


class RegistryEventKind(StrEnum):
    """Auditable registry state transitions."""

    REGISTERED = "registered"
    VALIDATED = "validated"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    RETIRED = "retired"


class ApprovalAction(StrEnum):
    """Human decisions that cannot be reused across lifecycle operations."""

    PROMOTE = "promote"
    ROLLBACK = "rollback"


class ArtifactLoadStatus(StrEnum):
    """Read-only load outcome used by the caller to select Fallback safely."""

    LOADED = "loaded"
    NO_PRODUCTION = "no_production"
    UNKNOWN_ARTIFACT = "unknown_artifact"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    AUTHORITY_INCOMPATIBLE = "authority_incompatible"
    INVALID_REGISTRY = "invalid_registry"
    INVALID_ARTIFACT_FORMAT = "invalid_artifact_format"


HyperparameterValue = str | int | float | bool | None
_HYPERPARAMETER_TYPES = (str, int, float, bool, type(None))


class ArtifactRef(_Frozen):
    """Stable identity of one artifact version."""

    kind: ArtifactKind
    model_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=120)
    version: str = Field(pattern=_SEMVER_PATTERN, max_length=80)

    @property
    def key(self) -> str:
        """Return the canonical key used inside the registry snapshot."""
        return f"{self.kind.value}/{self.model_id}/{self.version}"


class ArtifactMetadata(_Frozen):
    """Training, compatibility, evaluation, and integrity metadata."""

    schema_version: Literal[2] = MODEL_REGISTRY_SCHEMA_VERSION
    kind: ArtifactKind
    artifact_format: ArtifactFormat
    capability: ArtifactCapability
    """この artifact が主張する能力。**登録時に申告し、既定値を持たない。**

    v1 の metadata には無かったため schema version を 2 へ上げる。既定値を補って読むと、
    能力を申告していない artifact が「反実仮想もできる」側に倒れる余地を残してしまう。
    """
    model_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=120)
    version: str = Field(pattern=_SEMVER_PATTERN, max_length=80)
    created_at: str = Field(min_length=1, max_length=64)
    training_dataset_version: str = Field(min_length=1, max_length=200)
    # Every sequence / mapping reachable from RegistrySnapshot stops at its first bad
    # member (FailFast or a fail-fast validator); see RegistrySnapshot.audit.
    source_runs: Annotated[tuple[str, ...], FailFast()] = Field(min_length=1)
    feature_schema_version: str = Field(min_length=1, max_length=120)
    target_schema_version: str = Field(min_length=1, max_length=120)
    code_commit: str | None = Field(default=None, min_length=7, max_length=100)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    model_family: str = Field(min_length=1, max_length=200)
    hyperparameters: dict[str, HyperparameterValue]
    offline_evaluation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    shadow_evaluation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    authority_compatibility: Annotated[tuple[AuthorityStage, ...], FailFast()] = Field(min_length=1)

    @field_validator("hyperparameters", mode="before")
    @classmethod
    def _hyperparameters_fail_fast(cls, value: object) -> object:
        # Pydantic reports every failing union member of every value; on a corrupt
        # snapshot that is several error objects per JSON token.  Stop at the first.
        if isinstance(value, dict):
            for item in value.values():
                if not isinstance(item, _HYPERPARAMETER_TYPES):
                    raise ValueError("hyperparameter はscalarにする")
        return value

    @field_validator("created_at")
    @classmethod
    def _created_at_is_timezone_aware_rfc3339(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at は RFC 3339 形式にする") from exc
        if parsed.utcoffset() is None:
            raise ValueError("created_at には timezone が必要")
        return value

    @model_validator(mode="after")
    def _metadata_is_unambiguous(self) -> Self:
        if len(set(self.source_runs)) != len(self.source_runs):
            raise ValueError("source_runs は重複させない")
        if any(not source or len(source) > 500 for source in self.source_runs):
            raise ValueError("source_runs の各参照は1〜500文字にする")
        if len(set(self.authority_compatibility)) != len(self.authority_compatibility):
            raise ValueError("authority_compatibility は重複させない")
        for key, value in self.hyperparameters.items():
            if not key or len(key) > 120:
                raise ValueError("hyperparameter 名は1〜120文字にする")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("hyperparameter に非有限値を入れない")
        return self

    @property
    def ref(self) -> ArtifactRef:
        """Return the stable reference represented by this metadata."""
        return ArtifactRef(kind=self.kind, model_id=self.model_id, version=self.version)


class ModelCompatibility(_Frozen):
    """Runtime contract an artifact must match before use."""

    feature_schema_version: str = Field(min_length=1, max_length=120)
    target_schema_version: str = Field(min_length=1, max_length=120)
    authority_stage: AuthorityStage
    """The authority stage the caller intends to RUN at.

    **This is the effective stage (#92 / decision record 0057 §2.2), not the configured
    ceiling.** Since `fan-policy.yaml` v9 `authority_stage` is only an upper bound; the
    stage actually granted lives in the Authority journal. Asking here with the ceiling
    rejects a SHADOW-only artifact while the journal still says SHADOW, which is exactly
    the state the first promotion has to be collected in.
    """


class HumanApproval(_Frozen):
    """Explicit human approval attached to promotion or rollback."""

    decision: Literal["approved"] = "approved"
    action: ApprovalAction
    artifact: ArtifactRef
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_revision: int = Field(ge=0)
    approver: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=120)
    approved_at_ms: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)


class ArtifactRecord(_Frozen):
    """Metadata plus its current lifecycle state."""

    metadata: ArtifactMetadata
    status: ArtifactStatus

    @property
    def ref(self) -> ArtifactRef:
        """Return this record's artifact reference."""
        return self.metadata.ref


class ProductionSlot(_Frozen):
    """Current production pointer and its one-step known-good rollback target."""

    active: ArtifactRef
    previous: ArtifactRef | None = None


class RegistryAuditEvent(_Frozen):
    """State transition record suitable for a control decision trace."""

    revision: int = Field(ge=1)
    occurred_at_ms: int = Field(ge=0)
    event: RegistryEventKind
    artifact: ArtifactRef
    previous_artifact: ArtifactRef | None = None
    # Promotion only: the artifact that passed checksum + format re-verification and
    # became the rollback target (decision record 0037). It can differ from
    # ``previous_artifact`` when the outgoing production artifact is already corrupt.
    rollback_target: ArtifactRef | None = None
    actor: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    approval: HumanApproval | None = None

    @model_validator(mode="after")
    def _previous_artifact_belongs_to_pointer_change(self) -> Self:
        # Only promotion / rollback move a production pointer, so only they can name
        # the artifact they replaced.  Anything else would be unreplayed state.
        pointer_changes = {RegistryEventKind.PROMOTED, RegistryEventKind.ROLLED_BACK}
        if self.previous_artifact is not None and self.event not in pointer_changes:
            raise ValueError("previous_artifact は promotion / rollback audit だけが持つ")
        return self

    @model_validator(mode="after")
    def _rollback_target_belongs_to_promotion(self) -> Self:
        if self.rollback_target is None:
            return self
        if self.event is not RegistryEventKind.PROMOTED:
            raise ValueError("rollback_target は promotion audit だけが持つ")
        if self.rollback_target.kind is not self.artifact.kind:
            raise ValueError("rollback_target の kind が一致しない")
        if self.rollback_target == self.artifact:
            raise ValueError("promotion 対象自身を rollback_target にできない")
        return self

    @model_validator(mode="after")
    def _human_decisions_have_approval(self) -> Self:
        requires_approval = self.event in {
            RegistryEventKind.PROMOTED,
            RegistryEventKind.ROLLED_BACK,
        }
        if requires_approval != (self.approval is not None):
            raise ValueError("promotion / rollback だけが human approval を持つ")
        if self.approval is not None:
            if self.actor != self.approval.approver or self.reason != self.approval.reason:
                raise ValueError("audit event と approval の actor / reason を一致させる")
            expected_action = {
                RegistryEventKind.PROMOTED: ApprovalAction.PROMOTE,
                RegistryEventKind.ROLLED_BACK: ApprovalAction.ROLLBACK,
            }[self.event]
            if self.approval.action is not expected_action:
                raise ValueError("audit event と approval action を一致させる")
            if self.approval.artifact != self.artifact:
                raise ValueError("audit event と approval target を一致させる")
            if self.approval.expected_revision + 1 != self.revision:
                raise ValueError("approval を対象 registry revision の直後にだけ使用する")
            if self.approval.approved_at_ms > self.occurred_at_ms:
                raise ValueError("未来の approval は記録できない")
        return self


def _validate_member[ModelT: BaseModel](model: type[ModelT], value: object) -> ModelT:
    """Validate one container member, collapsing its errors into a single ValueError."""
    if isinstance(value, model):
        return value
    try:
        return model.model_validate_json(json.dumps(value))
    except (TypeError, ValueError):
        raise ValueError(f"{model.__name__} を検証できない") from None


class RegistrySnapshot(_Frozen):
    """Atomically replaced complete registry state."""

    schema_version: Literal[2] = MODEL_REGISTRY_SCHEMA_VERSION
    revision: int = Field(ge=0)
    artifacts: dict[str, ArtifactRecord] = Field(default_factory=dict)
    production: dict[ArtifactKind, ProductionSlot] = Field(default_factory=dict)
    # FailFast / the fail-fast validators below keep a corrupt snapshot from producing
    # one ValidationError entry per element, which would cost far more memory than the
    # structure-bounded JSON itself (decision in docs/model-registry.md).
    audit: Annotated[tuple[RegistryAuditEvent, ...], FailFast()] = ()

    @field_validator("artifacts", mode="before")
    @classmethod
    def _artifacts_fail_fast(cls, value: object) -> object:
        # A "before" validator makes Pydantic hand over plain Python values, so each
        # record is validated here (with the snapshot's JSON semantics) and returned as
        # a model instance.  Stopping at the first bad record bounds the error count.
        if not isinstance(value, dict):
            return value
        return {key: _validate_member(ArtifactRecord, record) for key, record in value.items()}

    @field_validator("production", mode="before")
    @classmethod
    def _production_fail_fast(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if len(value) > len(ArtifactKind):
            raise ValueError("production pointer が artifact kind の数を超えている")
        try:
            return {
                ArtifactKind(kind): _validate_member(ProductionSlot, slot)
                for kind, slot in value.items()
            }
        except ValueError:
            raise ValueError("production pointer を検証できない") from None

    @model_validator(mode="after")
    def _pointers_and_lifecycle_agree(self) -> Self:
        for key, record in self.artifacts.items():
            if key != record.ref.key:
                raise ValueError("artifact key と metadata が一致しない")

        active_keys: set[str] = set()
        for kind, slot in self.production.items():
            if slot.active.kind is not kind:
                raise ValueError("production pointer の kind が一致しない")
            active = self.artifacts.get(slot.active.key)
            if active is None or active.status is not ArtifactStatus.PRODUCTION:
                raise ValueError("production pointer は production artifact を指す")
            active_keys.add(slot.active.key)
            if slot.previous is not None:
                if slot.previous.kind is not kind:
                    raise ValueError("rollback pointer の kind が一致しない")
                previous = self.artifacts.get(slot.previous.key)
                if previous is None or previous.status is not ArtifactStatus.RETIRED:
                    raise ValueError("rollback pointer は retired artifact を指す")

        production_records = {
            key
            for key, record in self.artifacts.items()
            if record.status is ArtifactStatus.PRODUCTION
        }
        if production_records != active_keys:
            raise ValueError("production lifecycle と pointer が一致しない")
        if len(self.audit) != self.revision:
            raise ValueError("revision と audit event 数が一致しない")
        if any(event.revision != index for index, event in enumerate(self.audit, start=1)):
            raise ValueError("audit revision が連続していない")
        self._audit_replays_to_current_state()
        return self

    def _audit_replays_to_current_state(self) -> None:
        """Reject snapshots whose lifecycle cannot be derived from their approval audit."""
        statuses: dict[str, ArtifactStatus] = {}
        production: dict[ArtifactKind, ProductionSlot] = {}
        validated: set[str] = set()
        promoted: set[str] = set()

        for event in self.audit:
            key = event.artifact.key
            record = self.artifacts.get(key)
            if record is None:
                raise ValueError("audit event が未登録 artifact を参照している")
            if event.approval is not None and (
                event.approval.artifact_sha256 != record.metadata.sha256
            ):
                raise ValueError("approval checksum が artifact metadata と一致しない")

            if event.event is RegistryEventKind.REGISTERED:
                if key in statuses or event.previous_artifact is not None:
                    raise ValueError("artifact registration audit が重複または不正")
                statuses[key] = ArtifactStatus.CANDIDATE
            elif event.event is RegistryEventKind.VALIDATED:
                if statuses.get(key) is not ArtifactStatus.CANDIDATE:
                    raise ValueError("validated audit は candidate の後にだけ置ける")
                if record.metadata.offline_evaluation_ref is None:
                    raise ValueError("validated artifact に offline evaluation がない")
                statuses[key] = ArtifactStatus.VALIDATED
                validated.add(key)
            elif event.event is RegistryEventKind.PROMOTED:
                if statuses.get(key) is not ArtifactStatus.VALIDATED:
                    raise ValueError("promotion audit は validated の後にだけ置ける")
                if (
                    record.metadata.offline_evaluation_ref is None
                    or record.metadata.shadow_evaluation_ref is None
                ):
                    raise ValueError("production artifact に evaluation refs が揃っていない")
                current = production.get(event.artifact.kind)
                expected_previous = current.active if current is not None else None
                if event.previous_artifact != expected_previous:
                    raise ValueError("promotion audit の previous production が一致しない")
                # The outgoing production (verified) or the retained older rollback
                # target (outgoing one failed verification) may become the new target;
                # None when neither verified. Nothing else can be introduced here.
                allowed_targets = {None, expected_previous}
                if current is not None:
                    allowed_targets.add(current.previous)
                if event.rollback_target not in allowed_targets:
                    raise ValueError("promotion audit の rollback_target が不正")
                if expected_previous is not None:
                    statuses[expected_previous.key] = ArtifactStatus.RETIRED
                statuses[key] = ArtifactStatus.PRODUCTION
                production[event.artifact.kind] = ProductionSlot(
                    active=event.artifact,
                    previous=event.rollback_target,
                )
                promoted.add(key)
            elif event.event is RegistryEventKind.ROLLED_BACK:
                slot = production.get(event.artifact.kind)
                if (
                    slot is None
                    or slot.previous != event.artifact
                    or event.previous_artifact != slot.active
                    or statuses.get(key) is not ArtifactStatus.RETIRED
                ):
                    raise ValueError("rollback audit が known-good pointer と一致しない")
                statuses[slot.active.key] = ArtifactStatus.RETIRED
                statuses[key] = ArtifactStatus.PRODUCTION
                production[event.artifact.kind] = ProductionSlot(active=event.artifact)
            elif event.event is RegistryEventKind.RETIRED:
                if statuses.get(key) not in {
                    ArtifactStatus.CANDIDATE,
                    ArtifactStatus.VALIDATED,
                }:
                    raise ValueError("retire audit の遷移元が不正")
                statuses[key] = ArtifactStatus.RETIRED

        actual_statuses = {key: record.status for key, record in self.artifacts.items()}
        if statuses != actual_statuses or production != self.production:
            raise ValueError("audit から現在の lifecycle / production pointer を再現できない")
        for key, record in self.artifacts.items():
            metadata = record.metadata
            if key not in validated and metadata.offline_evaluation_ref is not None:
                raise ValueError("validation audit なしで offline evaluation が設定されている")
            if key not in promoted and metadata.shadow_evaluation_ref is not None:
                raise ValueError("promotion audit なしで shadow evaluation が設定されている")


_ATTESTATION_ISSUE_TOKEN = object()


class ArtifactAttestation:
    """Registry 自身が検証を終えたときにだけ発行する、artifact を名指しする証拠。

    **公開 constructor を持たない。** ``VerifiedArtifact`` は誰でも組み立てられる形だったため、
    その型であること自体は Registry を通った証明にならなかった（決定記録 0048 §2.4 が
    「sealed / opaque な発行境界」を #85 / #86 統合前の条件として挙げている）。この値は
    ``ModelRegistry`` の検証経路だけが発行し、受け取った側は「Registry を通った」事実を
    自称ではなく型で確かめられる。

    **暗号的な保証ではない。** 同一プロセス内の悪意ある偽造を防げないことは所有者が
    受け入れている（決定記録 0050 §3）。狙いは、検証していない artifact や別の artifact を
    取り違えて制御経路へ渡す**配線の誤り**を、レビューではなく型で止めることである。
    """

    __slots__ = (
        "_artifact_sha256",
        "_authority_compatibility",
        "_capability",
        "_feature_schema_version",
        "_kind",
        "_model_id",
        "_production_active",
        "_registry_revision",
        "_status",
        "_target_schema_version",
        "_version",
    )
    _kind: ArtifactKind
    _capability: ArtifactCapability
    _model_id: str
    _version: str
    _artifact_sha256: str
    _feature_schema_version: str
    _target_schema_version: str
    _authority_compatibility: tuple[AuthorityStage, ...]
    _status: ArtifactStatus
    _production_active: bool
    _registry_revision: int

    def __init__(self) -> None:
        raise TypeError("ArtifactAttestation は Model Registry の検証経路からだけ得られる")

    def __setattr__(self, name: str, value: object) -> None:
        """発行後に差し替えられないようにする。"""
        raise AttributeError("ArtifactAttestation は不変")

    @classmethod
    def _issue(
        cls,
        metadata: ArtifactMetadata,
        registry_revision: int,
        *,
        status: ArtifactStatus = ArtifactStatus.CANDIDATE,
        production_active: bool = False,
        _token: object | None = None,
    ) -> ArtifactAttestation:
        if _token is not _ATTESTATION_ISSUE_TOKEN:
            raise TypeError("ArtifactAttestation は Registry の検証経路だけが発行できる")
        if production_active and status is not ArtifactStatus.PRODUCTION:
            raise ValueError("production pointer でない artifact を production として発行しない")
        attestation = object.__new__(cls)
        object.__setattr__(attestation, "_status", status)
        object.__setattr__(attestation, "_production_active", production_active)
        object.__setattr__(attestation, "_kind", metadata.kind)
        object.__setattr__(attestation, "_capability", metadata.capability)
        object.__setattr__(attestation, "_model_id", metadata.model_id)
        object.__setattr__(attestation, "_version", metadata.version)
        object.__setattr__(attestation, "_artifact_sha256", metadata.sha256)
        object.__setattr__(attestation, "_feature_schema_version", metadata.feature_schema_version)
        object.__setattr__(attestation, "_target_schema_version", metadata.target_schema_version)
        object.__setattr__(
            attestation, "_authority_compatibility", metadata.authority_compatibility
        )
        object.__setattr__(attestation, "_registry_revision", registry_revision)
        return attestation

    @property
    def kind(self) -> ArtifactKind:
        """検証した artifact の役割。"""
        return self._kind

    @property
    def capability(self) -> ArtifactCapability:
        """**登録時に申告した能力。** 推論器の自称ではない。

        #86 はこの値だけを見て内部モデルの可否を決める（決定記録 0052 §2.1）。
        """
        return self._capability

    @property
    def model_id(self) -> str:
        """検証した artifact の model ID。"""
        return self._model_id

    @property
    def version(self) -> str:
        """検証した artifact の version。"""
        return self._version

    @property
    def artifact_sha256(self) -> str:
        """検証した bytes の SHA-256。"""
        return self._artifact_sha256

    @property
    def feature_schema_version(self) -> str:
        """検証した artifact の feature schema version。"""
        return self._feature_schema_version

    @property
    def target_schema_version(self) -> str:
        """検証した artifact の target schema version。"""
        return self._target_schema_version

    @property
    def authority_compatibility(self) -> tuple[AuthorityStage, ...]:
        """Registry metadata が許した authority stage。"""
        return self._authority_compatibility

    @property
    def status(self) -> ArtifactStatus:
        """発行時の lifecycle 状態。"""
        return self._status

    @property
    def production_active(self) -> bool:
        """この artifact が、その kind の**いまの production pointer そのもの**か。

        明示 version を固定した読み込み（Replay / offline 評価）でも、指した先が production で
        なければ false になる。**active 制御の束縛はこれを要求する**（決定記録 0052 §2.1）。
        promotion には人の承認が要るので、承認を経ていない artifact に制御権を渡さない。
        """
        return self._production_active

    @property
    def registry_revision(self) -> int:
        """発行時の registry revision。"""
        return self._registry_revision

    @property
    def model_version(self) -> str:
        """``ControlState.model_version`` に使える一意な値。"""
        return f"{self._model_id}@{self._version}"

    def trace_metadata(self) -> dict[str, object]:
        """#82 の decision trace へ載せられる、path を含まない情報。"""
        return {
            "artifact_kind": self._kind.value,
            "artifact_capability": self._capability.value,
            "model_id": self._model_id,
            "model_version": self._version,
            "artifact_sha256": self._artifact_sha256,
            "artifact_status": self._status.value,
            "production_active": self._production_active,
            "registry_revision": self._registry_revision,
        }


class VerifiedArtifact(_Frozen):
    """Checksum-verified immutable artifact bytes; no deserialization has occurred."""

    # 外から発行できない ``ArtifactAttestation`` を必須にすることで、この型そのものが
    # Registry の検証経路の外では組み立てられなくなる（決定記録 0048 §2.4 / 0052 §2.1）。
    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True
    )

    metadata: ArtifactMetadata
    payload: bytes
    attestation: ArtifactAttestation

    @model_validator(mode="after")
    def _attestation_names_this_artifact(self) -> Self:
        if (
            self.attestation.kind is not self.metadata.kind
            or self.attestation.model_id != self.metadata.model_id
            or self.attestation.version != self.metadata.version
            or self.attestation.artifact_sha256 != self.metadata.sha256
        ):
            raise ValueError("attestation が別の artifact を指している")
        return self

    @property
    def ref(self) -> ArtifactRef:
        """Return the verified artifact reference."""
        return self.metadata.ref

    @property
    def model_version(self) -> str:
        """Return an unambiguous value for ``ControlState.model_version``."""
        return f"{self.metadata.model_id}@{self.metadata.version}"


class ArtifactLoadResult(_Frozen):
    """Non-throwing operational result; every failure requires Fallback."""

    status: ArtifactLoadStatus
    artifact: VerifiedArtifact | None = None
    registry_revision: int = Field(ge=0)
    detail: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _loaded_status_has_artifact(self) -> Self:
        if (self.status is ArtifactLoadStatus.LOADED) != (self.artifact is not None):
            raise ValueError("loaded のときだけ verified artifact を返す")
        return self

    @property
    def fallback_required(self) -> bool:
        """Return whether the caller must select its deterministic Fallback path."""
        return self.status is not ArtifactLoadStatus.LOADED

    def trace_metadata(self) -> dict[str, object]:
        """Return path-free metadata for #82's decision trace integration."""
        trace: dict[str, object] = {
            "load_status": self.status.value,
            "registry_revision": self.registry_revision,
            "fallback_required": self.fallback_required,
        }
        if self.artifact is not None:
            metadata = self.artifact.metadata
            trace.update(
                {
                    "artifact_kind": metadata.kind.value,
                    "model_id": metadata.model_id,
                    "model_version": metadata.version,
                    "artifact_sha256": metadata.sha256,
                    "feature_schema_version": metadata.feature_schema_version,
                    "target_schema_version": metadata.target_schema_version,
                }
            )
        return {"model_registry": trace}


class ModelRegistryLimits(_Frozen):
    """Read / allocation bounds from ``config/model-registry.yaml`` (AGENTS.md rule 9).

    There are deliberately no code defaults: the YAML is the single source of truth.
    """

    schema_version: Literal[1]
    max_artifact_bytes: int = Field(gt=0)
    max_snapshot_bytes: int = Field(gt=0)
    max_json_nesting_depth: int = Field(gt=0)
    max_json_tokens: int = Field(gt=0)
    max_snapshot_json_nesting_depth: int = Field(gt=0)
    max_snapshot_json_tokens: int = Field(gt=0)

    @classmethod
    def from_file(cls, path: Path) -> ModelRegistryLimits:
        """Read and validate the registry limits YAML."""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"model registry 設定が辞書ではない: {path.name}")
        return cls.model_validate(loaded)


def load_model_registry_limits(directory: Path) -> ModelRegistryLimits:
    """Load the registry limits from the conventional file name in ``directory``."""
    return ModelRegistryLimits.from_file(directory / MODEL_REGISTRY_CONFIG_FILENAME)


class ModelRegistryError(Exception):
    """Base class for administrative registry operation failures."""


class RegistryCorruptError(ModelRegistryError):
    """The registry snapshot cannot be trusted."""


class UnsafeRegistryPathError(ModelRegistryError):
    """A symlink or non-regular registry path could escape the registry root."""


class ArtifactAlreadyExistsError(ModelRegistryError):
    """The artifact identity was already registered."""


class UnknownArtifactError(ModelRegistryError):
    """The requested artifact identity is not registered."""


class InvalidTransitionError(ModelRegistryError):
    """The requested lifecycle transition is not allowed."""


class ConcurrentUpdateError(ModelRegistryError):
    """The caller acted on an obsolete registry revision."""


class ArtifactVerificationError(ModelRegistryError):
    """The artifact bytes or runtime compatibility cannot be verified."""


class RegistryDurabilityError(ModelRegistryError):
    """The file was atomically replaced, but its directory entry was not fsynced.

    The new content is already visible (committed for this process and readers);
    only its durability across a crash is uncertain.  Callers must not undo it.
    """


class RegistryCapacityError(ModelRegistryError):
    """The next registry snapshot would exceed the configured snapshot bound."""


class _RegistryFileReadError(ModelRegistryError):
    """A pinned regular file changed size or exceeded its configured read bound."""


class _ArtifactUnavailableError(ArtifactVerificationError):
    pass


class _ChecksumMismatchError(ArtifactVerificationError):
    pass


class _SchemaMismatchError(ArtifactVerificationError):
    pass


class _AuthorityIncompatibleError(ArtifactVerificationError):
    pass


class _InvalidArtifactFormatError(ArtifactVerificationError):
    pass


class _JsonStructureError(ValueError):
    """JSON bytes exceed a configured structure bound or are lexically unbalanced."""


def _scan_json_structure(payload: bytes, *, max_depth: int, max_tokens: int) -> None:
    """Single linear pass over JSON bytes enforcing depth / token bounds.

    Runs before any parser materializes the object graph, so a small file made of
    millions of tiny containers is rejected without allocating per-element objects.
    """
    depth = 0
    tokens = 0
    for match in _JSON_STRUCTURE_TOKEN.finditer(payload):
        first = match.group()[:1]
        if first == b'"' and not match.group(1):
            raise _JsonStructureError("文字列が閉じていない")
        if first in (b"]", b"}"):
            depth -= 1
            if depth < 0:
                raise _JsonStructureError("括弧が対応していない")
            continue
        tokens += 1
        if tokens > max_tokens:
            raise _JsonStructureError("token 数が上限を超えている")
        if first in (b"[", b"{"):
            depth += 1
            if depth > max_depth:
                raise _JsonStructureError("nesting が上限を超えている")


class ModelRegistry:
    """Manage immutable local artifacts through an atomic registry snapshot."""

    def __init__(
        self,
        root: Path,
        clock: Clock | None = None,
        *,
        limits: ModelRegistryLimits,
    ) -> None:
        # Bind relative roots to the construction-time working directory without resolving
        # symlinks.  Each component is opened with O_NOFOLLOW below, so an ancestor symlink
        # cannot silently move the registry outside the configured path.
        self._root = Path(os.path.abspath(root))
        self._clock = clock or WallClock()
        self._limits = limits

    def inspect(self) -> RegistrySnapshot:
        """Read the current snapshot without changing filesystem state."""
        return self._read_snapshot()

    @contextmanager
    def pinned(self) -> Iterator[RegistrySnapshot]:
        """Hold the registry lock while the caller acts on the snapshot it read.

        ``inspect()`` releases the lock before it returns, so a caller that decides
        something from the snapshot and then commits elsewhere races with any
        concurrent promotion.  Authority Rollout (#92) uses this to make
        "this artifact is production" true **at the moment it writes**.

        **Lock order: the registry lock first, then the caller's own lock.**  The
        registry never acquires another component's lock while holding this one, so
        the ordering is total and cannot cycle.  Callers must not take this lock
        while already holding theirs.
        """
        with self._exclusive_lock() as root_fd:
            yield self._read_snapshot(root_fd)

    def register_candidate(
        self,
        metadata: ArtifactMetadata,
        payload: bytes,
        *,
        actor: str,
        reason: str,
    ) -> ArtifactRef:
        """Persist a checksum-matching candidate without interpreting its model contents."""
        self._validate_actor_reason(actor, reason)
        if len(payload) > self._limits.max_artifact_bytes:
            raise ArtifactVerificationError("artifact がsize上限を超えている")
        digest = sha256(payload).hexdigest()
        if digest != metadata.sha256:
            raise ArtifactVerificationError("artifact checksum が metadata と一致しない")
        self._validate_format(payload)

        with self._exclusive_lock() as root_fd:
            snapshot = self._read_snapshot(root_fd)
            ref = metadata.ref
            if ref.key in snapshot.artifacts:
                raise ArtifactAlreadyExistsError(f"artifact は登録済み: {ref.key}")

            artifacts = dict(snapshot.artifacts)
            artifacts[ref.key] = ArtifactRecord(
                metadata=metadata,
                status=ArtifactStatus.CANDIDATE,
            )
            updated = self._append_event(
                snapshot,
                artifacts=artifacts,
                production=dict(snapshot.production),
                event=RegistryEventKind.REGISTERED,
                artifact=ref,
                actor=actor,
                reason=reason,
            )
            # Check the capacity bounds before touching the filesystem, so a full
            # registry never leaves an unreferenced artifact behind.  The write order
            # stays artifact first, snapshot second: a crash in between leaves only
            # an orphan, never a snapshot pointing at missing bytes.
            snapshot_payload = self._encode_snapshot(updated)
            snapshot_attempted = False
            try:
                self._write_artifact(root_fd, ref, payload)
                snapshot_attempted = True
                self._atomic_write(root_fd, _STATE_FILENAME, snapshot_payload)
            except BaseException as exc:
                # A durability error after the snapshot replace means the registration
                # is committed and the artifact is referenced: never delete it.
                if not (snapshot_attempted and isinstance(exc, RegistryDurabilityError)):
                    self._remove_artifact_if_unreferenced(root_fd, ref)
                raise
            return ref

    def mark_validated(
        self,
        ref: ArtifactRef,
        *,
        offline_evaluation_ref: str,
        actor: str,
        reason: str,
        expected_revision: int,
    ) -> int:
        """Record successful offline evaluation and move candidate to validated."""
        self._validate_actor_reason(actor, reason)
        self._validate_reference(offline_evaluation_ref, "offline_evaluation_ref")
        with self._exclusive_lock() as root_fd:
            snapshot = self._read_snapshot(root_fd)
            self._check_revision(snapshot, expected_revision)
            record = self._record(snapshot, ref)
            if record.status is not ArtifactStatus.CANDIDATE:
                raise InvalidTransitionError("candidate だけを validated にできる")
            self._verify(root_fd, record, compatibility=None)

            metadata = record.metadata.model_copy(
                update={"offline_evaluation_ref": offline_evaluation_ref}
            )
            artifacts = dict(snapshot.artifacts)
            artifacts[ref.key] = ArtifactRecord(
                metadata=metadata,
                status=ArtifactStatus.VALIDATED,
            )
            updated = self._append_event(
                snapshot,
                artifacts=artifacts,
                production=dict(snapshot.production),
                event=RegistryEventKind.VALIDATED,
                artifact=ref,
                actor=actor,
                reason=reason,
            )
            self._write_snapshot(root_fd, updated)
            return updated.revision

    def promote(
        self,
        ref: ArtifactRef,
        compatibility: ModelCompatibility,
        *,
        shadow_evaluation_ref: str,
        approval: HumanApproval,
        expected_revision: int,
    ) -> int:
        """Atomically promote one validated artifact after explicit human approval."""
        self._validate_reference(shadow_evaluation_ref, "shadow_evaluation_ref")
        with self._exclusive_lock() as root_fd:
            snapshot = self._read_snapshot(root_fd)
            self._check_revision(snapshot, expected_revision)
            record = self._record(snapshot, ref)
            if record.status is not ArtifactStatus.VALIDATED:
                raise InvalidTransitionError("validated artifact だけを production にできる")
            if record.metadata.offline_evaluation_ref is None:
                raise InvalidTransitionError("offline evaluation の記録がない")
            self._verify(root_fd, record, compatibility)

            now_ms = self._clock.now_ms()
            self._validate_approval(
                approval,
                action=ApprovalAction.PROMOTE,
                artifact=ref,
                artifact_sha256=record.metadata.sha256,
                expected_revision=expected_revision,
                occurred_at_ms=now_ms,
            )
            previous_slot = snapshot.production.get(ref.kind)
            previous_ref = previous_slot.active if previous_slot is not None else None
            rollback_target = self._select_rollback_target(root_fd, snapshot, previous_slot)
            artifacts = dict(snapshot.artifacts)
            if previous_ref is not None:
                previous_record = self._record(snapshot, previous_ref)
                artifacts[previous_ref.key] = ArtifactRecord(
                    metadata=previous_record.metadata,
                    status=ArtifactStatus.RETIRED,
                )
            promoted_metadata = record.metadata.model_copy(
                update={"shadow_evaluation_ref": shadow_evaluation_ref}
            )
            artifacts[ref.key] = ArtifactRecord(
                metadata=promoted_metadata,
                status=ArtifactStatus.PRODUCTION,
            )
            production = dict(snapshot.production)
            production[ref.kind] = ProductionSlot(active=ref, previous=rollback_target)
            updated = self._append_event(
                snapshot,
                artifacts=artifacts,
                production=production,
                event=RegistryEventKind.PROMOTED,
                artifact=ref,
                previous_artifact=previous_ref,
                rollback_target=rollback_target,
                actor=approval.approver,
                reason=approval.reason,
                approval=approval,
                occurred_at_ms=now_ms,
            )
            self._write_snapshot(root_fd, updated)
            return updated.revision

    def _select_rollback_target(
        self,
        root_fd: int,
        snapshot: RegistrySnapshot,
        slot: ProductionSlot | None,
    ) -> ArtifactRef | None:
        """Pick the rollback target that survives a promotion (decision record 0037).

        Only checksum + format are re-verified here: compatibility is judged against
        the runtime contract at rollback time, so a schema change must not silently
        discard an intact known-good artifact.
        """
        if slot is None:
            return None
        for candidate in (slot.active, slot.previous):
            if candidate is None:
                continue
            try:
                self._verify(root_fd, self._record(snapshot, candidate), None)
            except ArtifactVerificationError:
                continue
            return candidate
        return None

    def rollback(
        self,
        kind: ArtifactKind,
        compatibility: ModelCompatibility,
        *,
        approval: HumanApproval,
        expected_revision: int,
    ) -> int:
        """Atomically restore the previous verified production artifact."""
        with self._exclusive_lock() as root_fd:
            snapshot = self._read_snapshot(root_fd)
            self._check_revision(snapshot, expected_revision)
            slot = snapshot.production.get(kind)
            if slot is None or slot.previous is None:
                raise InvalidTransitionError("rollback できる known-good artifact がない")
            current = self._record(snapshot, slot.active)
            target = self._record(snapshot, slot.previous)
            if target.status is not ArtifactStatus.RETIRED:
                raise InvalidTransitionError("rollback target が retired ではない")
            self._verify(root_fd, target, compatibility)

            now_ms = self._clock.now_ms()
            self._validate_approval(
                approval,
                action=ApprovalAction.ROLLBACK,
                artifact=target.ref,
                artifact_sha256=target.metadata.sha256,
                expected_revision=expected_revision,
                occurred_at_ms=now_ms,
            )
            artifacts = dict(snapshot.artifacts)
            artifacts[current.ref.key] = ArtifactRecord(
                metadata=current.metadata,
                status=ArtifactStatus.RETIRED,
            )
            artifacts[target.ref.key] = ArtifactRecord(
                metadata=target.metadata,
                status=ArtifactStatus.PRODUCTION,
            )
            production = dict(snapshot.production)
            production[kind] = ProductionSlot(active=target.ref)
            updated = self._append_event(
                snapshot,
                artifacts=artifacts,
                production=production,
                event=RegistryEventKind.ROLLED_BACK,
                artifact=target.ref,
                previous_artifact=current.ref,
                actor=approval.approver,
                reason=approval.reason,
                approval=approval,
                occurred_at_ms=now_ms,
            )
            self._write_snapshot(root_fd, updated)
            return updated.revision

    def retire(
        self,
        ref: ArtifactRef,
        *,
        actor: str,
        reason: str,
        expected_revision: int,
    ) -> int:
        """Retire a non-production candidate or validated artifact."""
        self._validate_actor_reason(actor, reason)
        with self._exclusive_lock() as root_fd:
            snapshot = self._read_snapshot(root_fd)
            self._check_revision(snapshot, expected_revision)
            record = self._record(snapshot, ref)
            if record.status not in {ArtifactStatus.CANDIDATE, ArtifactStatus.VALIDATED}:
                raise InvalidTransitionError("candidate / validated だけを明示的に retire できる")
            artifacts = dict(snapshot.artifacts)
            artifacts[ref.key] = ArtifactRecord(
                metadata=record.metadata,
                status=ArtifactStatus.RETIRED,
            )
            updated = self._append_event(
                snapshot,
                artifacts=artifacts,
                production=dict(snapshot.production),
                event=RegistryEventKind.RETIRED,
                artifact=ref,
                actor=actor,
                reason=reason,
            )
            self._write_snapshot(root_fd, updated)
            return updated.revision

    def load_production(
        self,
        kind: ArtifactKind,
        compatibility: ModelCompatibility,
    ) -> ArtifactLoadResult:
        """Read and verify production, returning a Fallback result instead of raising."""
        try:
            with self._open_root(create=False) as root_fd:
                if root_fd is None:
                    snapshot = RegistrySnapshot(revision=0)
                else:
                    snapshot = self._read_snapshot(root_fd)
                slot = snapshot.production.get(kind)
                if slot is None:
                    return self._load_failure(
                        ArtifactLoadStatus.NO_PRODUCTION,
                        snapshot.revision,
                        "production artifact が登録されていない",
                    )
                assert root_fd is not None
                return self._load_record(root_fd, snapshot, slot.active, compatibility)
        except (RegistryCorruptError, UnsafeRegistryPathError):
            return self._load_failure(
                ArtifactLoadStatus.INVALID_REGISTRY,
                0,
                "registry snapshot を検証できない",
            )

    def load_version(
        self,
        ref: ArtifactRef,
        compatibility: ModelCompatibility,
    ) -> ArtifactLoadResult:
        """Load an explicitly pinned version for Replay or Offline Evaluation."""
        try:
            with self._open_root(create=False) as root_fd:
                if root_fd is None:
                    snapshot = RegistrySnapshot(revision=0)
                else:
                    snapshot = self._read_snapshot(root_fd)
                if ref.key not in snapshot.artifacts:
                    return self._load_failure(
                        ArtifactLoadStatus.UNKNOWN_ARTIFACT,
                        snapshot.revision,
                        "指定された artifact version が登録されていない",
                    )
                assert root_fd is not None
                return self._load_record(root_fd, snapshot, ref, compatibility)
        except (RegistryCorruptError, UnsafeRegistryPathError):
            return self._load_failure(
                ArtifactLoadStatus.INVALID_REGISTRY,
                0,
                "registry snapshot を検証できない",
            )

    def _load_record(
        self,
        root_fd: int,
        snapshot: RegistrySnapshot,
        ref: ArtifactRef,
        compatibility: ModelCompatibility,
    ) -> ArtifactLoadResult:
        record = snapshot.artifacts[ref.key]
        slot = snapshot.production.get(ref.kind)
        # 明示 version を固定した読み込みでも、production pointer そのものなら production と
        # して発行する。指した先が候補・検証済み・引退なら false のままにする。
        production_active = slot is not None and slot.active == ref
        try:
            artifact = self._verify(
                root_fd,
                record,
                compatibility,
                snapshot.revision,
                production_active=production_active,
            )
        except _ArtifactUnavailableError:
            return self._load_failure(
                ArtifactLoadStatus.ARTIFACT_UNAVAILABLE,
                snapshot.revision,
                "artifact bytes を読み取れない",
            )
        except _ChecksumMismatchError:
            return self._load_failure(
                ArtifactLoadStatus.CHECKSUM_MISMATCH,
                snapshot.revision,
                "artifact checksum が metadata と一致しない",
            )
        except _SchemaMismatchError:
            return self._load_failure(
                ArtifactLoadStatus.SCHEMA_MISMATCH,
                snapshot.revision,
                "artifact schema が runtime contract と一致しない",
            )
        except _AuthorityIncompatibleError:
            return self._load_failure(
                ArtifactLoadStatus.AUTHORITY_INCOMPATIBLE,
                snapshot.revision,
                "artifact は現在の authority stage と互換性がない",
            )
        except _InvalidArtifactFormatError:
            return self._load_failure(
                ArtifactLoadStatus.INVALID_ARTIFACT_FORMAT,
                snapshot.revision,
                "artifact format の安全な検証に失敗した",
            )
        return ArtifactLoadResult(
            status=ArtifactLoadStatus.LOADED,
            artifact=artifact,
            registry_revision=snapshot.revision,
            detail="artifact の checksum と互換性を検証した",
        )

    def _verify(
        self,
        root_fd: int,
        record: ArtifactRecord,
        compatibility: ModelCompatibility | None,
        registry_revision: int = 0,
        production_active: bool = False,
    ) -> VerifiedArtifact:
        try:
            payload = self._read_artifact(root_fd, record.ref)
        except (OSError, UnsafeRegistryPathError, _RegistryFileReadError) as exc:
            raise _ArtifactUnavailableError("artifact bytes を読み取れない") from exc
        if sha256(payload).hexdigest() != record.metadata.sha256:
            raise _ChecksumMismatchError("artifact checksum mismatch")
        self._validate_format(payload)
        if compatibility is not None:
            if (
                record.metadata.feature_schema_version != compatibility.feature_schema_version
                or record.metadata.target_schema_version != compatibility.target_schema_version
            ):
                raise _SchemaMismatchError("artifact schema mismatch")
            if compatibility.authority_stage not in record.metadata.authority_compatibility:
                raise _AuthorityIncompatibleError("authority stage incompatible")
        # attestation はこの検証経路だけが発行する。呼び出し側が「Registry を通った」ことを
        # 自称ではなく型で示せるようにする（決定記録 0052 §2.1）。
        return VerifiedArtifact(
            metadata=record.metadata,
            payload=payload,
            attestation=ArtifactAttestation._issue(
                record.metadata,
                registry_revision,
                status=record.status,
                production_active=production_active,
                _token=_ATTESTATION_ISSUE_TOKEN,
            ),
        )

    def _validate_format(self, payload: bytes) -> None:
        # Bound the object graph *before* json.loads(): a payload under the byte limit
        # can still hold millions of tiny containers whose Python objects would take
        # hundreds of MB, and a MemoryError must not take down the control process.
        self._check_json_structure_bounds(payload)

        def reject_nonfinite(value: str) -> None:
            raise ValueError(f"非有限値はJSON artifactに使用できない: {value}")

        try:
            decoded = json.loads(payload, parse_constant=reject_nonfinite)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _InvalidArtifactFormatError("JSON artifact が不正") from exc
        if not isinstance(decoded, (dict, list)):
            raise _InvalidArtifactFormatError("JSON artifact は object または array にする")

    def _check_json_structure_bounds(self, payload: bytes) -> None:
        try:
            _scan_json_structure(
                payload,
                max_depth=self._limits.max_json_nesting_depth,
                max_tokens=self._limits.max_json_tokens,
            )
        except _JsonStructureError as exc:
            raise _InvalidArtifactFormatError(f"JSON artifact: {exc}") from exc

    def _check_snapshot_structure_bounds(self, payload: bytes) -> None:
        _scan_json_structure(
            payload,
            max_depth=self._limits.max_snapshot_json_nesting_depth,
            max_tokens=self._limits.max_snapshot_json_tokens,
        )

    def _read_snapshot(self, root_fd: int | None = None) -> RegistrySnapshot:
        if root_fd is None:
            try:
                with self._open_root(create=False) as opened_root_fd:
                    if opened_root_fd is None:
                        return RegistrySnapshot(revision=0)
                    return self._read_snapshot(opened_root_fd)
            except UnsafeRegistryPathError as exc:
                raise RegistryCorruptError("registry root を安全に読み取れない") from exc
        try:
            payload = self._read_regular_file(
                root_fd,
                _STATE_FILENAME,
                missing_ok=True,
                max_bytes=self._limits.max_snapshot_bytes,
            )
            if payload is None:
                return RegistrySnapshot(revision=0)
            # Bound the parsed graph before Pydantic materializes it (same as artifacts).
            self._check_snapshot_structure_bounds(payload)
            return RegistrySnapshot.model_validate_json(payload)
        except (
            OSError,
            UnsafeRegistryPathError,
            _RegistryFileReadError,
            ValidationError,
            ValueError,
        ) as exc:
            raise RegistryCorruptError("registry snapshot を検証できない") from exc

    def _write_snapshot(self, root_fd: int, snapshot: RegistrySnapshot) -> None:
        self._atomic_write(root_fd, _STATE_FILENAME, self._encode_snapshot(snapshot))

    def _encode_snapshot(self, snapshot: RegistrySnapshot) -> bytes:
        """Serialize a snapshot, refusing one that later reads would reject."""
        payload = snapshot.model_dump_json(indent=2).encode("utf-8") + b"\n"
        if len(payload) > self._limits.max_snapshot_bytes:
            # Writing it would make every later read fail as INVALID_REGISTRY.
            raise RegistryCapacityError("registry snapshot がsize上限を超える")
        try:
            self._check_snapshot_structure_bounds(payload)
        except _JsonStructureError as exc:
            raise RegistryCapacityError(f"registry snapshot が構造上限を超える: {exc}") from exc
        return payload

    def _remove_artifact_if_unreferenced(self, root_fd: int, ref: ArtifactRef) -> None:
        """Best-effort removal of an artifact whose registration was not committed.

        The failure may have happened on either side of the snapshot replace (e.g. an
        interrupt right after the rename), so the snapshot is re-read under the still
        held lock and the artifact is removed only when it is provably unreferenced.
        An unreadable snapshot is ambiguous and keeps the artifact.  Every component
        is opened with O_NOFOLLOW relative to the pinned root, and
        unlink / rmdir act on names inside those fds, so a swapped-in symlink makes the
        cleanup fail closed instead of deleting anything outside the registry.
        """
        try:
            if ref.key in self._read_snapshot(root_fd).artifacts:
                return
        except RegistryCorruptError:
            return
        with suppress(OSError, UnsafeRegistryPathError):
            parent_fd = self._open_directory_chain(
                root_fd,
                ("artifacts", ref.kind.value, ref.model_id),
                create=False,
            )
            try:
                version_fd = self._open_directory_chain(parent_fd, (ref.version,), create=False)
                try:
                    with suppress(FileNotFoundError):
                        os.unlink(_ARTIFACT_FILENAME, dir_fd=version_fd)
                finally:
                    os.close(version_fd)
                # Fails (and is ignored) if anything else is left in the directory.
                os.rmdir(ref.version, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    def _read_artifact(self, root_fd: int, ref: ArtifactRef) -> bytes:
        directory_fd = self._open_directory_chain(
            root_fd,
            ("artifacts", ref.kind.value, ref.model_id, ref.version),
            create=False,
        )
        try:
            payload = self._read_regular_file(
                directory_fd,
                _ARTIFACT_FILENAME,
                max_bytes=self._limits.max_artifact_bytes,
            )
            assert payload is not None
            return payload
        finally:
            os.close(directory_fd)

    def _write_artifact(self, root_fd: int, ref: ArtifactRef, payload: bytes) -> None:
        directory_fd = self._open_directory_chain(
            root_fd,
            ("artifacts", ref.kind.value, ref.model_id, ref.version),
            create=True,
        )
        try:
            self._atomic_write(directory_fd, _ARTIFACT_FILENAME, payload)
        finally:
            os.close(directory_fd)

    @contextmanager
    def _open_root(self, *, create: bool) -> Iterator[int | None]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            anchor_fd = os.open(self._root.anchor, flags)
        except OSError as exc:
            raise UnsafeRegistryPathError("registry root のanchorを開けない") from exc
        try:
            try:
                root_fd = self._open_directory_chain(
                    anchor_fd,
                    tuple(self._root.parts[1:]),
                    create=create,
                )
            except FileNotFoundError:
                if create:
                    raise UnsafeRegistryPathError("registry root を作成できない") from None
                yield None
                return
            except OSError as exc:
                raise UnsafeRegistryPathError(
                    "registry root のcomponentがsymlinkまたはdirectoryではない"
                ) from exc
        finally:
            os.close(anchor_fd)
        try:
            yield root_fd
        finally:
            os.close(root_fd)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[int]:
        with self._open_root(create=True) as root_fd:
            assert root_fd is not None
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
            try:
                lock_fd = os.open(_LOCK_FILENAME, flags, 0o600, dir_fd=root_fd)
            except OSError as exc:
                raise UnsafeRegistryPathError("registry lock がsymlinkである") from exc
            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise UnsafeRegistryPathError("registry lock がregular fileではない")
                flock(lock_fd, LOCK_EX)
                try:
                    yield root_fd
                finally:
                    flock(lock_fd, LOCK_UN)
            finally:
                os.close(lock_fd)

    @staticmethod
    def _open_directory_chain(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
        current_fd = os.dup(root_fd)
        try:
            for part in parts:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                try:
                    child_fd = os.open(part, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    # Another registry process may have created this component.  The
                    # no-follow open below still decides whether it is safe to use.
                    try:
                        os.mkdir(part, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    else:
                        # The directory entry lives in the parent; without this fsync a
                        # crash can drop it even after an artifact / snapshot fsync inside.
                        os.fsync(current_fd)
                    child_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as exc:
                    raise UnsafeRegistryPathError(
                        f"registry path component がsymlinkまたはdirectoryではない: {part}"
                    ) from exc
                os.close(current_fd)
                current_fd = child_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    @staticmethod
    def _read_regular_file(
        directory_fd: int,
        name: str,
        *,
        missing_ok: bool = False,
        max_bytes: int | None = None,
    ) -> bytes | None:
        try:
            file_fd = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        except OSError as exc:
            raise UnsafeRegistryPathError(f"registry file がsymlinkである: {name}") from exc
        try:
            file_status = os.fstat(file_fd)
            if not stat.S_ISREG(file_status.st_mode):
                raise UnsafeRegistryPathError(f"registry file がregular fileではない: {name}")
            expected_size = file_status.st_size
            if max_bytes is not None and expected_size > max_bytes:
                raise _RegistryFileReadError(f"registry file がsize上限を超えている: {name}")

            payload = bytearray()
            remaining = expected_size
            while remaining:
                chunk = os.read(file_fd, min(_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _RegistryFileReadError(f"registry file が読取中に短縮された: {name}")
                payload.extend(chunk)
                remaining -= len(chunk)
            if os.read(file_fd, 1):
                raise _RegistryFileReadError(f"registry file が読取中に拡張された: {name}")
            return bytes(payload)
        finally:
            os.close(file_fd)

    @staticmethod
    def _atomic_write(directory_fd: int, name: str, payload: bytes) -> None:
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise UnsafeRegistryPathError(f"registry destination がregular fileではない: {name}")

        temporary_name = f".{name}.{secrets.token_hex(12)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        try:
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(temporary_fd, remaining)
                    remaining = remaining[written:]
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
            os.replace(
                temporary_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                # Report that the replace already happened, so callers do not treat
                # this like a failure before the new content became visible.
                raise RegistryDurabilityError(
                    f"registry file は置換済みだがdirectoryをfsyncできない: {name}"
                ) from exc
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)

    def _append_event(
        self,
        snapshot: RegistrySnapshot,
        *,
        artifacts: dict[str, ArtifactRecord],
        production: dict[ArtifactKind, ProductionSlot],
        event: RegistryEventKind,
        artifact: ArtifactRef,
        actor: str,
        reason: str,
        previous_artifact: ArtifactRef | None = None,
        rollback_target: ArtifactRef | None = None,
        approval: HumanApproval | None = None,
        occurred_at_ms: int | None = None,
    ) -> RegistrySnapshot:
        revision = snapshot.revision + 1
        audit_event = RegistryAuditEvent(
            revision=revision,
            occurred_at_ms=self._clock.now_ms() if occurred_at_ms is None else occurred_at_ms,
            event=event,
            artifact=artifact,
            previous_artifact=previous_artifact,
            rollback_target=rollback_target,
            actor=actor,
            reason=reason,
            approval=approval,
        )
        return RegistrySnapshot(
            revision=revision,
            artifacts=artifacts,
            production=production,
            audit=(*snapshot.audit, audit_event),
        )

    @staticmethod
    def _record(snapshot: RegistrySnapshot, ref: ArtifactRef) -> ArtifactRecord:
        try:
            return snapshot.artifacts[ref.key]
        except KeyError as exc:
            raise UnknownArtifactError(f"artifact が登録されていない: {ref.key}") from exc

    @staticmethod
    def _check_revision(snapshot: RegistrySnapshot, expected_revision: int) -> None:
        if snapshot.revision != expected_revision:
            raise ConcurrentUpdateError(
                f"registry revision が更新された: expected={expected_revision}, "
                f"actual={snapshot.revision}"
            )

    @staticmethod
    def _validate_actor_reason(actor: str, reason: str) -> None:
        if not actor or len(actor) > 120:
            raise ValueError("actor は1〜120文字にする")
        if not reason or len(reason) > 1000:
            raise ValueError("reason は1〜1000文字にする")
        if re.fullmatch(_IDENTIFIER_PATTERN, actor) is None:
            raise ValueError("actor は小文字の識別子にする")

    @staticmethod
    def _validate_reference(value: str, field_name: str) -> None:
        if not value or len(value) > 500:
            raise ValueError(f"{field_name} は1〜500文字にする")

    @staticmethod
    def _validate_approval(
        approval: HumanApproval,
        *,
        action: ApprovalAction,
        artifact: ArtifactRef,
        artifact_sha256: str,
        expected_revision: int,
        occurred_at_ms: int,
    ) -> None:
        if approval.action is not action:
            raise ValueError("human approval action が操作と一致しない")
        if approval.artifact != artifact:
            raise ValueError("human approval target がartifactと一致しない")
        if approval.artifact_sha256 != artifact_sha256:
            raise ValueError("human approval checksum がartifactと一致しない")
        if approval.expected_revision != expected_revision:
            raise ValueError("human approval revision が操作対象と一致しない")
        if approval.approved_at_ms > occurred_at_ms:
            raise ValueError("未来の human approval は使用できない")

    @staticmethod
    def _load_failure(
        status: ArtifactLoadStatus,
        revision: int,
        detail: str,
    ) -> ArtifactLoadResult:
        return ArtifactLoadResult(
            status=status,
            registry_revision=revision,
            detail=detail,
        )
