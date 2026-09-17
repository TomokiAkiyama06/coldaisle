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
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from coldaisle.clock import Clock, WallClock
from coldaisle.control.schema import AuthorityStage

MODEL_REGISTRY_SCHEMA_VERSION: Literal[1] = 1
_STATE_FILENAME = "registry.json"
_LOCK_FILENAME = ".registry.lock"
_ARTIFACT_FILENAME = "artifact.payload"

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

    schema_version: Literal[1] = MODEL_REGISTRY_SCHEMA_VERSION
    kind: ArtifactKind
    artifact_format: ArtifactFormat
    model_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=120)
    version: str = Field(pattern=_SEMVER_PATTERN, max_length=80)
    created_at: str = Field(min_length=1, max_length=64)
    training_dataset_version: str = Field(min_length=1, max_length=200)
    source_runs: tuple[str, ...] = Field(min_length=1)
    feature_schema_version: str = Field(min_length=1, max_length=120)
    target_schema_version: str = Field(min_length=1, max_length=120)
    code_commit: str | None = Field(default=None, min_length=7, max_length=100)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    model_family: str = Field(min_length=1, max_length=200)
    hyperparameters: dict[str, HyperparameterValue]
    offline_evaluation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    shadow_evaluation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    authority_compatibility: tuple[AuthorityStage, ...] = Field(min_length=1)

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
    actor: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    approval: HumanApproval | None = None

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


class RegistrySnapshot(_Frozen):
    """Atomically replaced complete registry state."""

    schema_version: Literal[1] = MODEL_REGISTRY_SCHEMA_VERSION
    revision: int = Field(ge=0)
    artifacts: dict[str, ArtifactRecord] = Field(default_factory=dict)
    production: dict[ArtifactKind, ProductionSlot] = Field(default_factory=dict)
    audit: tuple[RegistryAuditEvent, ...] = ()

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
                if expected_previous is not None:
                    statuses[expected_previous.key] = ArtifactStatus.RETIRED
                statuses[key] = ArtifactStatus.PRODUCTION
                production[event.artifact.kind] = ProductionSlot(
                    active=event.artifact,
                    previous=expected_previous,
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


class VerifiedArtifact(_Frozen):
    """Checksum-verified immutable artifact bytes; no deserialization has occurred."""

    metadata: ArtifactMetadata
    payload: bytes

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


class ModelRegistry:
    """Manage immutable local artifacts through an atomic registry snapshot."""

    def __init__(self, root: Path, clock: Clock | None = None) -> None:
        # Bind relative roots to the construction-time working directory without resolving
        # symlinks.  Each component is opened with O_NOFOLLOW below, so an ancestor symlink
        # cannot silently move the registry outside the configured path.
        self._root = Path(os.path.abspath(root))
        self._clock = clock or WallClock()

    def inspect(self) -> RegistrySnapshot:
        """Read the current snapshot without changing filesystem state."""
        return self._read_snapshot()

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
        digest = sha256(payload).hexdigest()
        if digest != metadata.sha256:
            raise ArtifactVerificationError("artifact checksum が metadata と一致しない")
        self._validate_format(payload)

        with self._exclusive_lock() as root_fd:
            snapshot = self._read_snapshot(root_fd)
            ref = metadata.ref
            if ref.key in snapshot.artifacts:
                raise ArtifactAlreadyExistsError(f"artifact は登録済み: {ref.key}")

            self._write_artifact(root_fd, ref, payload)
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
            self._write_snapshot(root_fd, updated)
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
            production[ref.kind] = ProductionSlot(active=ref, previous=previous_ref)
            updated = self._append_event(
                snapshot,
                artifacts=artifacts,
                production=production,
                event=RegistryEventKind.PROMOTED,
                artifact=ref,
                previous_artifact=previous_ref,
                actor=approval.approver,
                reason=approval.reason,
                approval=approval,
                occurred_at_ms=now_ms,
            )
            self._write_snapshot(root_fd, updated)
            return updated.revision

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
        try:
            artifact = self._verify(root_fd, record, compatibility)
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
    ) -> VerifiedArtifact:
        try:
            payload = self._read_artifact(root_fd, record.ref)
        except (OSError, UnsafeRegistryPathError) as exc:
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
        return VerifiedArtifact(metadata=record.metadata, payload=payload)

    @staticmethod
    def _validate_format(payload: bytes) -> None:
        def reject_nonfinite(value: str) -> None:
            raise ValueError(f"非有限値はJSON artifactに使用できない: {value}")

        try:
            decoded = json.loads(payload, parse_constant=reject_nonfinite)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _InvalidArtifactFormatError("JSON artifact が不正") from exc
        if not isinstance(decoded, (dict, list)):
            raise _InvalidArtifactFormatError("JSON artifact は object または array にする")

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
            payload = self._read_regular_file(root_fd, _STATE_FILENAME, missing_ok=True)
            if payload is None:
                return RegistrySnapshot(revision=0)
            return RegistrySnapshot.model_validate_json(payload)
        except (OSError, UnsafeRegistryPathError, ValidationError, ValueError) as exc:
            raise RegistryCorruptError("registry snapshot を検証できない") from exc

    def _write_snapshot(self, root_fd: int, snapshot: RegistrySnapshot) -> None:
        payload = snapshot.model_dump_json(indent=2).encode("utf-8") + b"\n"
        self._atomic_write(root_fd, _STATE_FILENAME, payload)

    def _read_artifact(self, root_fd: int, ref: ArtifactRef) -> bytes:
        directory_fd = self._open_directory_chain(
            root_fd,
            ("artifacts", ref.kind.value, ref.model_id, ref.version),
            create=False,
        )
        try:
            payload = self._read_regular_file(directory_fd, _ARTIFACT_FILENAME)
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
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=current_fd)
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
    ) -> bytes | None:
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        except OSError as exc:
            raise UnsafeRegistryPathError(f"registry file がsymlinkである: {name}") from exc
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise UnsafeRegistryPathError(f"registry file がregular fileではない: {name}")
            chunks: list[bytes] = []
            while chunk := os.read(file_fd, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
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
            os.fsync(directory_fd)
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
