"""Filesystem Control Model Registry (#104) tests; no model runtime or hardware required."""

from __future__ import annotations

import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event
from typing import get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

import coldaisle.control.model_registry as registry_module
from coldaisle.clock import SimulatedClock
from coldaisle.control import (
    ApprovalAction,
    ArtifactFormat,
    ArtifactKind,
    ArtifactLoadStatus,
    ArtifactMetadata,
    ArtifactStatus,
    ArtifactVerificationError,
    AuthorityStage,
    ConcurrentUpdateError,
    HumanApproval,
    InvalidTransitionError,
    ModelCompatibility,
    ModelRegistry,
    ModelRegistryLimits,
    RegistryCapacityError,
    RegistryCorruptError,
    RegistryDurabilityError,
    RegistryEventKind,
    RegistrySnapshot,
    UnsafeRegistryPathError,
    load_model_registry_limits,
)

NOW_MS = 1_800_000_000_000
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
LIMITS = load_model_registry_limits(CONFIG_DIR)
ACTOR = "model-operator"
COMPATIBILITY = ModelCompatibility(
    feature_schema_version="thermal-features-v1",
    target_schema_version="thermal-targets-v1",
    authority_stage=AuthorityStage.SHADOW,
)


def payload(version: str) -> bytes:
    return json.dumps({"model": version}, sort_keys=True).encode()


def artifact_path(root: Path, version: str = "1.0.0") -> Path:
    return root / "artifacts" / "thermal_model" / "rack-thermal" / version / "artifact.payload"


def file_identity(path: Path) -> tuple[int, int]:
    status = path.stat()
    return status.st_dev, status.st_ino


def metadata(
    version: str,
    *,
    content: bytes | None = None,
    feature_schema_version: str = "thermal-features-v1",
) -> ArtifactMetadata:
    body = payload(version) if content is None else content
    return ArtifactMetadata(
        kind=ArtifactKind.THERMAL_MODEL,
        artifact_format=ArtifactFormat.JSON,
        model_id="rack-thermal",
        version=version,
        created_at="2026-09-18T10:00:00+09:00",
        training_dataset_version="dataset-v4",
        source_runs=("run-001", "run-002"),
        feature_schema_version=feature_schema_version,
        target_schema_version="thermal-targets-v1",
        code_commit="0123456789abcdef",
        sha256=sha256(body).hexdigest(),
        model_family="linear-baseline",
        hyperparameters={"alpha": 0.25, "fit_intercept": True},
        authority_compatibility=(AuthorityStage.SHADOW, AuthorityStage.LIMITED),
    )


def approval(
    version: str,
    revision: int,
    *,
    action: ApprovalAction = ApprovalAction.PROMOTE,
    reason: str = "shadow evaluation passed",
) -> HumanApproval:
    return HumanApproval(
        action=action,
        artifact=metadata(version).ref,
        artifact_sha256=metadata(version).sha256,
        expected_revision=revision,
        approver=ACTOR,
        approved_at_ms=NOW_MS,
        reason=reason,
    )


def register_and_validate(registry: ModelRegistry, version: str) -> None:
    registry.register_candidate(
        metadata(version),
        payload(version),
        actor="trainer",
        reason="training completed",
    )
    registry.mark_validated(
        metadata(version).ref,
        offline_evaluation_ref=f"evaluation/offline/{version}",
        actor="evaluator",
        reason="offline gates passed",
        expected_revision=registry.inspect().revision,
    )


def promote(registry: ModelRegistry, version: str) -> None:
    revision = registry.inspect().revision
    registry.promote(
        metadata(version).ref,
        COMPATIBILITY,
        shadow_evaluation_ref=f"evaluation/shadow/{version}",
        approval=approval(version, revision),
        expected_revision=revision,
    )


def promote_in_process(
    root: str,
    version: str,
    expected_revision: int,
    barrier,
    results,
) -> None:
    """Process worker proving that the filesystem lock and revision CAS are cross-process."""
    barrier.wait()
    registry = ModelRegistry(Path(root), SimulatedClock(NOW_MS), limits=LIMITS)
    try:
        registry.promote(
            metadata(version).ref,
            COMPATIBILITY,
            shadow_evaluation_ref=f"evaluation/shadow/{version}",
            approval=approval(version, expected_revision, reason=f"approve {version}"),
            expected_revision=expected_revision,
        )
    except ConcurrentUpdateError:
        results.put("conflict")
    else:
        results.put("promoted")


def test_no_production_returns_read_only_fallback_result(tmp_path: Path) -> None:
    root = tmp_path / "registry-does-not-exist"

    result = ModelRegistry(root, limits=LIMITS).load_production(
        ArtifactKind.THERMAL_MODEL, COMPATIBILITY
    )

    assert result.status is ArtifactLoadStatus.NO_PRODUCTION
    assert result.fallback_required is True
    assert result.artifact is None
    assert not root.exists(), "read-only load は空の registry directory を作らない"


def test_candidate_and_production_coexist_and_candidate_is_not_implicitly_loaded(
    tmp_path: Path,
) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    candidate = registry.register_candidate(
        metadata("1.1.0"),
        payload("1.1.0"),
        actor="trainer",
        reason="new training run",
    )

    loaded = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)
    pinned = registry.load_version(candidate, COMPATIBILITY)
    snapshot = registry.inspect()

    assert loaded.status is ArtifactLoadStatus.LOADED
    assert loaded.artifact is not None
    assert loaded.artifact.model_version == "rack-thermal@1.0.0"
    assert pinned.status is ArtifactLoadStatus.LOADED
    assert pinned.artifact is not None
    assert pinned.artifact.payload == payload("1.1.0")
    assert snapshot.artifacts[candidate.key].status is ArtifactStatus.CANDIDATE
    assert snapshot.production[ArtifactKind.THERMAL_MODEL].active.version == "1.0.0"


def test_corrupt_production_is_rejected_without_mutating_registry(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    revision = registry.inspect().revision
    artifact_path = (
        root / "artifacts" / "thermal_model" / "rack-thermal" / "1.0.0" / "artifact.payload"
    )
    artifact_path.write_bytes(b'{"model":"tampered"}')

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert result.status is ArtifactLoadStatus.CHECKSUM_MISMATCH
    assert result.fallback_required is True
    assert registry.inspect().revision == revision


def test_schema_mismatch_returns_fallback_and_trace_metadata(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    incompatible = ModelCompatibility(
        feature_schema_version="thermal-features-v2",
        target_schema_version="thermal-targets-v1",
        authority_stage=AuthorityStage.SHADOW,
    )

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, incompatible)

    assert result.status is ArtifactLoadStatus.SCHEMA_MISMATCH
    assert result.trace_metadata() == {
        "model_registry": {
            "load_status": "schema_mismatch",
            "registry_revision": 3,
            "fallback_required": True,
        }
    }


def test_authority_compatibility_is_checked_independently(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    full_authority = COMPATIBILITY.model_copy(update={"authority_stage": AuthorityStage.FULL})

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, full_authority)

    assert result.status is ArtifactLoadStatus.AUTHORITY_INCOMPATIBLE
    assert result.fallback_required is True


def test_promotion_requires_validated_state_and_does_not_raise_authority(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    ref = registry.register_candidate(
        metadata("1.0.0"),
        payload("1.0.0"),
        actor="trainer",
        reason="training completed",
    )

    with pytest.raises(InvalidTransitionError, match="validated"):
        registry.promote(
            ref,
            COMPATIBILITY,
            shadow_evaluation_ref="evaluation/shadow/1.0.0",
            approval=approval("1.0.0", registry.inspect().revision),
            expected_revision=registry.inspect().revision,
        )

    assert registry.inspect().artifacts[ref.key].status is ArtifactStatus.CANDIDATE


def test_atomic_rollback_restores_known_good_and_audits_human_reason(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    register_and_validate(registry, "2.0.0")
    promote(registry, "2.0.0")

    registry.rollback(
        ArtifactKind.THERMAL_MODEL,
        COMPATIBILITY,
        approval=approval(
            "1.0.0",
            registry.inspect().revision,
            action=ApprovalAction.ROLLBACK,
            reason="production residual regressed",
        ),
        expected_revision=registry.inspect().revision,
    )

    snapshot = registry.inspect()
    slot = snapshot.production[ArtifactKind.THERMAL_MODEL]
    loaded = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)
    event = snapshot.audit[-1]
    assert slot.active.version == "1.0.0"
    assert slot.previous is None
    assert snapshot.artifacts[metadata("1.0.0").ref.key].status is ArtifactStatus.PRODUCTION
    assert snapshot.artifacts[metadata("2.0.0").ref.key].status is ArtifactStatus.RETIRED
    assert loaded.artifact is not None
    assert loaded.artifact.model_version == "rack-thermal@1.0.0"
    assert event.event is RegistryEventKind.ROLLED_BACK
    assert event.occurred_at_ms == NOW_MS
    assert event.reason == "production residual regressed"
    assert event.approval == approval(
        "1.0.0",
        snapshot.revision - 1,
        action=ApprovalAction.ROLLBACK,
        reason="production residual regressed",
    )


def test_corrupt_known_good_cannot_replace_current_production(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    register_and_validate(registry, "2.0.0")
    promote(registry, "2.0.0")
    revision = registry.inspect().revision
    old_path = root / "artifacts" / "thermal_model" / "rack-thermal" / "1.0.0" / "artifact.payload"
    old_path.write_bytes(b"corrupt")

    with pytest.raises(ArtifactVerificationError):
        registry.rollback(
            ArtifactKind.THERMAL_MODEL,
            COMPATIBILITY,
            approval=approval(
                "1.0.0",
                revision,
                action=ApprovalAction.ROLLBACK,
                reason="attempt rollback",
            ),
            expected_revision=revision,
        )

    snapshot = registry.inspect()
    assert snapshot.revision == revision
    assert snapshot.production[ArtifactKind.THERMAL_MODEL].active.version == "2.0.0"


def rollback_approval(version: str, revision: int) -> HumanApproval:
    return approval(
        version,
        revision,
        action=ApprovalAction.ROLLBACK,
        reason="production residual regressed",
    )


def test_promotion_over_corrupt_production_keeps_verified_older_rollback_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    for version in ("1.0.0", "2.0.0"):
        register_and_validate(registry, version)
        promote(registry, version)
    assert registry.inspect().audit[-1].rollback_target == metadata("1.0.0").ref
    artifact_path(root, "2.0.0").write_bytes(b"corrupt")
    register_and_validate(registry, "3.0.0")

    promote(registry, "3.0.0")

    snapshot = registry.inspect()
    slot = snapshot.production[ArtifactKind.THERMAL_MODEL]
    event = snapshot.audit[-1]
    assert slot.active == metadata("3.0.0").ref
    assert slot.previous == metadata("1.0.0").ref
    assert event.previous_artifact == metadata("2.0.0").ref
    assert event.rollback_target == metadata("1.0.0").ref
    assert snapshot.artifacts[metadata("2.0.0").ref.key].status is ArtifactStatus.RETIRED

    revision = snapshot.revision
    registry.rollback(
        ArtifactKind.THERMAL_MODEL,
        COMPATIBILITY,
        approval=rollback_approval("1.0.0", revision),
        expected_revision=revision,
    )

    loaded = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)
    assert loaded.artifact is not None
    assert loaded.artifact.model_version == "rack-thermal@1.0.0"


def test_promotion_drops_rollback_target_when_no_candidate_verifies(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    for version in ("1.0.0", "2.0.0"):
        register_and_validate(registry, version)
        promote(registry, version)
    artifact_path(root, "1.0.0").write_bytes(b"corrupt")
    artifact_path(root, "2.0.0").unlink()
    register_and_validate(registry, "3.0.0")

    promote(registry, "3.0.0")

    snapshot = registry.inspect()
    assert snapshot.production[ArtifactKind.THERMAL_MODEL].previous is None
    assert snapshot.audit[-1].previous_artifact == metadata("2.0.0").ref
    assert snapshot.audit[-1].rollback_target is None
    with pytest.raises(InvalidTransitionError):
        registry.rollback(
            ArtifactKind.THERMAL_MODEL,
            COMPATIBILITY,
            approval=rollback_approval("2.0.0", snapshot.revision),
            expected_revision=snapshot.revision,
        )


def test_rollback_target_is_checked_for_integrity_not_runtime_compatibility(
    tmp_path: Path,
) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    old = metadata("1.0.0", feature_schema_version="thermal-features-v0")
    registry.register_candidate(old, payload("1.0.0"), actor="trainer", reason="trained")
    registry.mark_validated(
        old.ref,
        offline_evaluation_ref="evaluation/offline/1.0.0",
        actor="evaluator",
        reason="offline gates passed",
        expected_revision=registry.inspect().revision,
    )
    revision = registry.inspect().revision
    registry.promote(
        old.ref,
        COMPATIBILITY.model_copy(update={"feature_schema_version": "thermal-features-v0"}),
        shadow_evaluation_ref="evaluation/shadow/1.0.0",
        approval=approval("1.0.0", revision).model_copy(update={"artifact_sha256": old.sha256}),
        expected_revision=revision,
    )
    register_and_validate(registry, "2.0.0")

    promote(registry, "2.0.0")

    assert registry.inspect().production[ArtifactKind.THERMAL_MODEL].previous == old.ref


def test_snapshot_cannot_forge_unrelated_rollback_target(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "0.9.0")
    registry.retire(
        metadata("0.9.0").ref,
        actor="evaluator",
        reason="superseded",
        expected_revision=registry.inspect().revision,
    )
    for version in ("1.0.0", "2.0.0"):
        register_and_validate(registry, version)
        promote(registry, version)
    document = json.loads((root / "registry.json").read_text())
    forged = metadata("0.9.0").ref.model_dump(mode="json")
    document["audit"][-1]["rollback_target"] = forged
    document["production"]["thermal_model"]["previous"] = forged

    with pytest.raises(ValidationError, match="rollback_target"):
        RegistrySnapshot.model_validate_json(json.dumps(document))


def test_rollback_target_is_rejected_outside_promotion_audit() -> None:
    with pytest.raises(ValidationError, match="rollback_target"):
        registry_module.RegistryAuditEvent(
            revision=1,
            occurred_at_ms=NOW_MS,
            event=RegistryEventKind.REGISTERED,
            artifact=metadata("2.0.0").ref,
            rollback_target=metadata("1.0.0").ref,
            actor="trainer",
            reason="training completed",
        )


def test_two_concurrent_promotions_with_same_revision_cannot_clobber_each_other(
    tmp_path: Path,
) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    register_and_validate(registry, "2.0.0")
    expected_revision = registry.inspect().revision
    barrier = Barrier(2)

    def promote_concurrently(version: str) -> str:
        barrier.wait()
        try:
            registry.promote(
                metadata(version).ref,
                COMPATIBILITY,
                shadow_evaluation_ref=f"evaluation/shadow/{version}",
                approval=approval(
                    version,
                    expected_revision,
                    reason=f"approve {version}",
                ),
                expected_revision=expected_revision,
            )
        except ConcurrentUpdateError:
            return "conflict"
        return "promoted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(promote_concurrently, ("1.0.0", "2.0.0")))

    assert sorted(results) == ["conflict", "promoted"]
    snapshot = registry.inspect()
    active = snapshot.production[ArtifactKind.THERMAL_MODEL].active
    assert snapshot.artifacts[active.key].status is ArtifactStatus.PRODUCTION
    assert (
        sum(record.status is ArtifactStatus.PRODUCTION for record in snapshot.artifacts.values())
        == 1
    )
    assert sum(event.event is RegistryEventKind.PROMOTED for event in snapshot.audit) == 1


def test_processes_cannot_promote_over_the_same_revision(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    register_and_validate(registry, "2.0.0")
    expected_revision = registry.inspect().revision
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=promote_in_process,
            args=(str(root), version, expected_revision, barrier, results),
        )
        for version in ("1.0.0", "2.0.0")
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted((results.get(timeout=1), results.get(timeout=1))) == ["conflict", "promoted"]
    snapshot = registry.inspect()
    assert (
        sum(record.status is ArtifactStatus.PRODUCTION for record in snapshot.artifacts.values())
        == 1
    )


def test_readers_never_observe_unavailable_model_during_promotions(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    versions = tuple(f"{major}.0.0" for major in range(2, 12))
    for version in versions:
        register_and_validate(registry, version)

    started = Event()
    stopped = Event()
    observed: list[ArtifactLoadStatus] = []

    def read_repeatedly() -> None:
        started.set()
        while not stopped.is_set():
            observed.append(
                registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY).status
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        reader = executor.submit(read_repeatedly)
        assert started.wait(timeout=1)
        for version in versions:
            promote(registry, version)
        stopped.set()
        reader.result(timeout=2)

    assert observed
    assert set(observed) == {ArtifactLoadStatus.LOADED}


def test_pickle_and_framework_native_formats_are_not_in_the_schema() -> None:
    values = {member.value for member in ArtifactFormat}
    invalid = metadata("1.0.0").model_dump()
    invalid["artifact_format"] = "pickle"

    assert values == {"json"}
    for unsafe_format in ("pickle", "joblib", "onnx", "safetensors"):
        invalid["artifact_format"] = unsafe_format
        with pytest.raises(ValidationError):
            ArtifactMetadata.model_validate(invalid)


def test_each_created_directory_is_fsynced_in_its_parent_before_descending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int, str]] = []
    real_mkdir = os.mkdir
    real_fsync = os.fsync

    def recording_mkdir(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        assert dir_fd is not None
        real_mkdir(path, mode, dir_fd=dir_fd)
        events.append(("mkdir", os.fstat(dir_fd).st_ino, path))

    def recording_fsync(fd: int) -> None:
        real_fsync(fd)
        events.append(("fsync", os.fstat(fd).st_ino, ""))

    monkeypatch.setattr(registry_module.os, "mkdir", recording_mkdir)
    monkeypatch.setattr(registry_module.os, "fsync", recording_fsync)
    registry = ModelRegistry(
        tmp_path / "new-parent" / "registry", SimulatedClock(NOW_MS), limits=LIMITS
    )

    registry.register_candidate(
        metadata("1.0.0"),
        payload("1.0.0"),
        actor="trainer",
        reason="training completed",
    )

    created = [name for kind, _, name in events if kind == "mkdir"]
    assert created == [
        "new-parent",
        "registry",
        "artifacts",
        "thermal_model",
        "rack-thermal",
        "1.0.0",
    ]
    for index, (kind, parent_inode, _) in enumerate(events):
        if kind == "mkdir":
            # The new entry must be durable in its parent before anything is created inside.
            assert events[index + 1] == ("fsync", parent_inode, "")


def test_invalid_json_artifact_is_never_registered(tmp_path: Path) -> None:
    body = b"not-json"
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)

    with pytest.raises(ArtifactVerificationError):
        registry.register_candidate(
            metadata("1.0.0", content=body),
            body,
            actor="trainer",
            reason="training completed",
        )

    assert registry.inspect().revision == 0


def register_raw(registry: ModelRegistry, body: bytes) -> None:
    registry.register_candidate(
        metadata("1.0.0", content=body),
        body,
        actor="trainer",
        reason="training completed",
    )


def test_json_nesting_depth_is_bounded_by_config(tmp_path: Path) -> None:
    depth = LIMITS.max_json_nesting_depth
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)

    with pytest.raises(ArtifactVerificationError):
        register_raw(registry, b"[" * (depth + 1) + b"]" * (depth + 1))
    assert registry.inspect().revision == 0

    register_raw(registry, b"[" * depth + b"]" * depth)
    assert registry.inspect().revision == 1


def test_many_tiny_containers_are_rejected_before_building_the_object_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"[" + b",".join([b"[]"] * LIMITS.max_json_tokens) + b"]"
    assert len(body) <= LIMITS.max_artifact_bytes
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)

    def refuse_parse(*args: object, **kwargs: object) -> object:
        raise AssertionError("over-bound artifact must not reach json.loads")

    monkeypatch.setattr(registry_module.json, "loads", refuse_parse)

    with pytest.raises(ArtifactVerificationError):
        register_raw(registry, body)
    with pytest.raises(ArtifactVerificationError):
        register_raw(registry, b"[" + b",".join([b"{}"] * LIMITS.max_json_tokens) + b"]")
    with pytest.raises(ArtifactVerificationError):
        register_raw(registry, b"[" + b",".join([b"0"] * LIMITS.max_json_tokens) + b"]")


def test_brackets_and_escaped_quotes_inside_json_strings_are_not_structure(
    tmp_path: Path,
) -> None:
    depth = LIMITS.max_json_nesting_depth
    text = "[" * (depth * 4) + '\\" {{' + "]" * (depth * 4)
    body = json.dumps({"note": text, "items": [text] * 3}).encode()
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)

    register_raw(registry, body)

    assert registry.inspect().revision == 1


def test_unterminated_string_is_rejected_in_a_single_linear_pass(tmp_path: Path) -> None:
    # Many escaped quotes after an unterminated string: a naive tokenizer rescans the
    # remaining input from every quote (quadratic).  This must be rejected promptly.
    body = b'["' + b'a\\"' * 500_000
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)

    with pytest.raises(ArtifactVerificationError):
        register_raw(registry, body)
    with pytest.raises(ArtifactVerificationError):
        register_raw(registry, b'["trailing backslash\\')


def test_loaded_artifact_over_json_bounds_returns_invalid_format(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    tight = ModelRegistry(
        root,
        SimulatedClock(NOW_MS),
        limits=LIMITS.model_copy(update={"max_json_tokens": 1}),
    )

    result = tight.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert result.status is ArtifactLoadStatus.INVALID_ARTIFACT_FORMAT
    assert result.fallback_required is True


def test_oversized_artifact_is_never_registered(tmp_path: Path) -> None:
    body = b'{"model":"too-large"}'
    limits = LIMITS.model_copy(update={"max_artifact_bytes": len(body) - 1})
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=limits)

    with pytest.raises(ArtifactVerificationError, match="size"):
        registry.register_candidate(
            metadata("1.0.0", content=body),
            body,
            actor="trainer",
            reason="training completed",
        )

    assert registry.inspect().revision == 0


def test_registry_limits_come_from_config_without_code_defaults(tmp_path: Path) -> None:
    assert LIMITS.max_artifact_bytes > 0
    assert LIMITS.max_snapshot_bytes > 0
    (tmp_path / "model-registry.yaml").write_text("schema_version: 1\nmax_artifact_bytes: 10\n")

    with pytest.raises(ValidationError, match="max_snapshot_bytes"):
        load_model_registry_limits(tmp_path)
    with pytest.raises(TypeError):
        ModelRegistry(tmp_path / "registry")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ModelRegistryLimits.model_validate(LIMITS.model_dump() | {"max_json_nesting_depth": 0})


def test_oversized_snapshot_is_rejected_by_fstat_as_invalid_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    state = root / "registry.json"
    with state.open("r+b") as handle:
        handle.truncate(LIMITS.max_snapshot_bytes + 1)
    state_identity = file_identity(state)
    original_read = registry_module.os.read

    def reject_state_read(file_fd: int, count: int) -> bytes:
        status = os.fstat(file_fd)
        if (status.st_dev, status.st_ino) == state_identity:
            raise AssertionError("oversized snapshot must be rejected before read")
        return original_read(file_fd, count)

    monkeypatch.setattr(registry_module.os, "read", reject_state_read)

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert result.status is ArtifactLoadStatus.INVALID_REGISTRY
    assert result.fallback_required is True
    with pytest.raises(RegistryCorruptError):
        registry.inspect()


def snapshot_document(**overrides: object) -> bytes:
    document: dict[str, object] = {
        "schema_version": 1,
        "revision": 0,
        "artifacts": {},
        "production": {},
        "audit": [],
    }
    document.update(overrides)
    return json.dumps(document).encode()


def json_token_count(payload: bytes) -> int:
    closers = (b"]", b"}")
    return sum(
        1
        for match in registry_module._JSON_STRUCTURE_TOKEN.finditer(payload)
        if match.group()[:1] not in closers
    )


def test_snapshot_with_many_tiny_containers_is_rejected_before_pydantic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    tiny = b"[" + b",".join([b"[]"] * LIMITS.max_snapshot_json_tokens) + b"]"
    assert len(tiny) <= LIMITS.max_snapshot_bytes
    deep = b"[" * (LIMITS.max_snapshot_json_nesting_depth + 1)
    deep += b"]" * (LIMITS.max_snapshot_json_nesting_depth + 1)

    def refuse_parse(*args: object, **kwargs: object) -> object:
        raise AssertionError("over-bound snapshot must not reach Pydantic")

    monkeypatch.setattr(RegistrySnapshot, "model_validate_json", refuse_parse)
    for body in (tiny, deep):
        (root / "registry.json").write_bytes(body)

        result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

        assert result.status is ArtifactLoadStatus.INVALID_REGISTRY
        assert result.fallback_required is True
        with pytest.raises(RegistryCorruptError):
            registry.inspect()


def metadata_document(**overrides: object) -> bytes:
    return json.dumps(metadata("1.0.0").model_dump(mode="json") | overrides).encode()


BAD_MEMBERS = 1000
# Every sequence / mapping field reachable from RegistrySnapshot, each filled with
# BAD_MEMBERS invalid members and validated through the model that owns it.  Owning
# models are validated directly: a parent that collapses a member's errors would still
# have materialized all of them first.
CONTAINER_FIELD_CASES: dict[str, tuple[type[BaseModel], bytes]] = {
    "RegistrySnapshot.audit": (RegistrySnapshot, snapshot_document(audit=[{}] * BAD_MEMBERS)),
    "RegistrySnapshot.artifacts": (
        RegistrySnapshot,
        snapshot_document(artifacts={f"k{i}": {} for i in range(BAD_MEMBERS)}),
    ),
    "RegistrySnapshot.production": (
        RegistrySnapshot,
        snapshot_document(production={f"k{i}": {} for i in range(BAD_MEMBERS)}),
    ),
    "ArtifactMetadata.source_runs": (
        ArtifactMetadata,
        metadata_document(source_runs=[1] * BAD_MEMBERS),
    ),
    "ArtifactMetadata.hyperparameters": (
        ArtifactMetadata,
        metadata_document(hyperparameters={f"k{i}": {} for i in range(BAD_MEMBERS)}),
    ),
    "ArtifactMetadata.authority_compatibility": (
        ArtifactMetadata,
        metadata_document(authority_compatibility=["bogus"] * BAD_MEMBERS),
    ),
}


def reachable_container_fields() -> set[str]:
    """Walk the snapshot model tree and name every sequence / mapping field."""
    found: set[str] = set()
    seen: set[type[BaseModel]] = set()
    pending: list[type[BaseModel]] = [RegistrySnapshot]
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for name, field in model.model_fields.items():
            for annotation in (field.annotation, *get_args(field.annotation)):
                for candidate in (annotation, *get_args(annotation)):
                    if get_origin(candidate) in (tuple, list, dict, set, frozenset):
                        found.add(f"{model.__name__}.{name}")
                    for inner in (candidate, *get_args(candidate)):
                        if isinstance(inner, type) and issubclass(inner, BaseModel):
                            pending.append(inner)
    return found


def test_fail_fast_cases_cover_every_reachable_container_field() -> None:
    assert reachable_container_fields() == set(CONTAINER_FIELD_CASES)


@pytest.mark.parametrize("field_path", sorted(CONTAINER_FIELD_CASES))
def test_corrupt_container_reports_a_bounded_number_of_errors(field_path: str) -> None:
    # Error objects cost far more than the JSON tokens; the count must not scale with them.
    model, document = CONTAINER_FIELD_CASES[field_path]

    with pytest.raises(ValidationError) as caught:
        model.model_validate_json(document)

    assert caught.value.error_count() <= 20


def test_legitimate_history_reaches_the_byte_bound_before_the_token_bound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    for index in range(20):
        version = f"1.0.{index}"
        register_and_validate(registry, version)
        promote(registry, version)
    payload = (root / "registry.json").read_bytes()
    tokens_per_byte = json_token_count(payload) / len(payload)

    assert tokens_per_byte * LIMITS.max_snapshot_bytes < LIMITS.max_snapshot_json_tokens


def version_dir(root: Path, version: str) -> Path:
    return root / "artifacts" / "thermal_model" / "rack-thermal" / version


@pytest.mark.parametrize("bound", ["max_snapshot_bytes", "max_snapshot_json_tokens"])
def test_full_registry_rejects_registration_before_writing_the_artifact(
    tmp_path: Path,
    bound: str,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    before = (root / "registry.json").read_bytes()
    current = len(before) if bound == "max_snapshot_bytes" else json_token_count(before)
    full = ModelRegistry(
        root,
        SimulatedClock(NOW_MS),
        limits=LIMITS.model_copy(update={bound: current}),
    )

    with pytest.raises(RegistryCapacityError):
        register_raw_version(full, "2.0.0")

    assert not version_dir(root, "2.0.0").exists()
    assert (root / "registry.json").read_bytes() == before


def register_raw_version(registry: ModelRegistry, version: str) -> None:
    registry.register_candidate(
        metadata(version),
        payload(version),
        actor="trainer",
        reason="training completed",
    )


def test_failed_snapshot_commit_removes_the_orphaned_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    before = (root / "registry.json").read_bytes()
    original_write = ModelRegistry._atomic_write

    def fail_snapshot_write(directory_fd: int, name: str, body: bytes) -> None:
        if name == "registry.json":
            assert artifact_path(root, "2.0.0").exists()  # artifact is written first
            raise OSError("disk full")
        original_write(directory_fd, name, body)

    monkeypatch.setattr(ModelRegistry, "_atomic_write", staticmethod(fail_snapshot_write))

    with pytest.raises(OSError, match="disk full"):
        register_raw_version(registry, "2.0.0")

    assert not version_dir(root, "2.0.0").exists()
    assert artifact_path(root, "1.0.0").exists()
    assert (root / "registry.json").read_bytes() == before


def fail_after_replace(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    failure: BaseException | None = None,
) -> None:
    """Let ``os.replace`` onto ``target`` succeed, then fail its directory fsync.

    With ``failure`` the exception is raised right after the rename instead, like
    an interrupt arriving between the replace and the fsync.
    """
    original_replace = os.replace
    original_fsync = os.fsync
    replaced = False

    def replace(src: str, dst: str, **kwargs: int) -> None:
        nonlocal replaced
        original_replace(src, dst, **kwargs)
        if dst == target:
            if failure is not None:
                raise failure
            replaced = True

    def fsync(fd: int) -> None:
        nonlocal replaced
        if replaced:
            replaced = False
            raise OSError("fsync failed after replace")
        original_fsync(fd)

    monkeypatch.setattr(registry_module.os, "replace", replace)
    monkeypatch.setattr(registry_module.os, "fsync", fsync)


def test_artifact_directory_fsync_failure_after_replace_leaves_no_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    before = (root / "registry.json").read_bytes()
    fail_after_replace(monkeypatch, "artifact.payload")

    with pytest.raises(RegistryDurabilityError):
        register_raw_version(registry, "2.0.0")

    assert not version_dir(root, "2.0.0").exists()
    assert (root / "registry.json").read_bytes() == before


def test_snapshot_fsync_failure_after_replace_keeps_the_committed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    fail_after_replace(monkeypatch, "registry.json")

    with pytest.raises(RegistryDurabilityError):
        register_raw_version(registry, "1.0.0")
    monkeypatch.undo()

    assert metadata("1.0.0").ref.key in registry.inspect().artifacts
    loaded = registry.load_version(metadata("1.0.0").ref, COMPATIBILITY)
    assert loaded.status is ArtifactLoadStatus.LOADED


def test_interrupt_after_snapshot_replace_keeps_the_committed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    fail_after_replace(monkeypatch, "registry.json", KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        register_raw_version(registry, "1.0.0")
    monkeypatch.undo()

    assert metadata("1.0.0").ref.key in registry.inspect().artifacts
    assert artifact_path(root, "1.0.0").exists()


def test_interrupt_before_snapshot_replace_removes_the_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    original_replace = os.replace

    def interrupt_snapshot_replace(src: str, dst: str, **kwargs: int) -> None:
        if dst == "registry.json":
            raise KeyboardInterrupt
        original_replace(src, dst, **kwargs)

    monkeypatch.setattr(registry_module.os, "replace", interrupt_snapshot_replace)

    with pytest.raises(KeyboardInterrupt):
        register_raw_version(registry, "1.0.0")
    monkeypatch.undo()

    assert not version_dir(root, "1.0.0").exists()
    assert registry.inspect().revision == 0


def test_orphan_cleanup_does_not_follow_a_swapped_in_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "artifact.payload").write_bytes(b"do not delete")
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    original_write = ModelRegistry._atomic_write

    def swap_then_fail(directory_fd: int, name: str, body: bytes) -> None:
        if name == "registry.json":
            target = version_dir(root, "1.0.0")
            (target / "artifact.payload").unlink()
            target.rmdir()
            target.symlink_to(outside, target_is_directory=True)
            raise OSError("disk full")
        original_write(directory_fd, name, body)

    monkeypatch.setattr(ModelRegistry, "_atomic_write", staticmethod(swap_then_fail))

    with pytest.raises(OSError, match="disk full"):
        register_raw_version(registry, "1.0.0")

    assert (outside / "artifact.payload").read_bytes() == b"do not delete"
    assert outside.is_dir()


def test_snapshot_exceeding_the_token_bound_is_never_written(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    before = (root / "registry.json").read_bytes()
    tight = ModelRegistry(
        root,
        SimulatedClock(NOW_MS),
        limits=LIMITS.model_copy(update={"max_snapshot_json_tokens": json_token_count(before)}),
    )

    with pytest.raises(RegistryCapacityError):
        tight.retire(
            metadata("1.0.0").ref,
            actor="evaluator",
            reason="superseded",
            expected_revision=tight.inspect().revision,
        )

    assert (root / "registry.json").read_bytes() == before


@pytest.mark.parametrize(
    "event",
    [RegistryEventKind.REGISTERED, RegistryEventKind.VALIDATED, RegistryEventKind.RETIRED],
)
def test_previous_artifact_is_rejected_outside_pointer_changes(
    event: RegistryEventKind,
) -> None:
    with pytest.raises(ValidationError, match="previous_artifact"):
        registry_module.RegistryAuditEvent(
            revision=1,
            occurred_at_ms=NOW_MS,
            event=event,
            artifact=metadata("2.0.0").ref,
            previous_artifact=metadata("1.0.0").ref,
            actor="trainer",
            reason="lifecycle change",
        )


def test_snapshot_cannot_smuggle_previous_artifact_into_validation_audit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    register_and_validate(registry, "2.0.0")
    registry.retire(
        metadata("2.0.0").ref,
        actor="evaluator",
        reason="superseded",
        expected_revision=registry.inspect().revision,
    )
    document = json.loads((root / "registry.json").read_text())
    for index in (1, 4):  # VALIDATED 1.0.0, RETIRED 2.0.0
        forged = json.loads(json.dumps(document))
        forged["audit"][index]["previous_artifact"] = metadata("1.0.0").ref.model_dump(mode="json")

        with pytest.raises(ValidationError, match="previous_artifact"):
            RegistrySnapshot.model_validate_json(json.dumps(forged))


def test_snapshot_exceeding_the_bound_is_never_written(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    size = (root / "registry.json").stat().st_size
    tight = ModelRegistry(
        root,
        SimulatedClock(NOW_MS),
        limits=LIMITS.model_copy(update={"max_snapshot_bytes": size}),
    )

    with pytest.raises(RegistryCapacityError):
        tight.retire(
            metadata("1.0.0").ref,
            actor="evaluator",
            reason="superseded",
            expected_revision=tight.inspect().revision,
        )

    assert tight.inspect().revision == 2
    assert (root / "registry.json").stat().st_size == size


def test_malformed_registry_state_returns_fallback_instead_of_loading(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    (root / "registry.json").write_text('{"schema_version": 999}', encoding="utf-8")

    result = ModelRegistry(root, limits=LIMITS).load_production(
        ArtifactKind.THERMAL_MODEL, COMPATIBILITY
    )

    assert result.status is ArtifactLoadStatus.INVALID_REGISTRY
    assert result.fallback_required is True


def test_human_approval_cannot_be_postdated(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    revision = registry.inspect().revision
    future = approval("1.0.0", revision).model_copy(update={"approved_at_ms": NOW_MS + 1})

    with pytest.raises(ValueError, match="未来"):
        registry.promote(
            metadata("1.0.0").ref,
            COMPATIBILITY,
            shadow_evaluation_ref="evaluation/shadow/1.0.0",
            approval=future,
            expected_revision=revision,
        )

    assert registry.inspect().revision == revision


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("action", ApprovalAction.ROLLBACK, "action"),
        ("artifact", metadata("2.0.0").ref, "target"),
        ("artifact_sha256", "0" * 64, "checksum"),
        ("expected_revision", 0, "revision"),
    ],
)
def test_human_approval_is_bound_to_exact_operation_artifact_and_revision(
    field: str,
    value: object,
    message: str,
    tmp_path: Path,
) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    revision = registry.inspect().revision
    mismatched = approval("1.0.0", revision).model_copy(update={field: value})

    with pytest.raises(ValueError, match=message):
        registry.promote(
            metadata("1.0.0").ref,
            COMPATIBILITY,
            shadow_evaluation_ref="evaluation/shadow/1.0.0",
            approval=mismatched,
            expected_revision=revision,
        )

    assert registry.inspect().revision == revision


def test_snapshot_cannot_forge_production_without_promotion_approval(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    document = registry.inspect().model_dump(mode="json")
    document["revision"] = 1
    document["audit"] = document["audit"][:1]

    with pytest.raises(ValidationError, match="audit"):
        RegistrySnapshot.model_validate_json(json.dumps(document))


def test_symlink_artifact_component_cannot_escape_registry_root(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "artifacts").symlink_to(outside, target_is_directory=True)
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)

    with pytest.raises(UnsafeRegistryPathError, match="symlink"):
        registry.register_candidate(
            metadata("1.0.0"),
            payload("1.0.0"),
            actor="trainer",
            reason="training completed",
        )

    assert tuple(outside.iterdir()) == ()
    assert not (root / "registry.json").exists()


def test_symlink_registry_ancestor_cannot_escape_trusted_anchor(tmp_path: Path) -> None:
    safe = tmp_path / "safe"
    outside = tmp_path / "outside"
    safe.mkdir()
    outside.mkdir()
    (safe / "linked-parent").symlink_to(outside, target_is_directory=True)
    registry = ModelRegistry(
        safe / "linked-parent" / "registry", SimulatedClock(NOW_MS), limits=LIMITS
    )

    with pytest.raises(UnsafeRegistryPathError, match="symlink"):
        registry.register_candidate(
            metadata("1.0.0"),
            payload("1.0.0"),
            actor="trainer",
            reason="training completed",
        )

    assert tuple(outside.iterdir()) == ()


def test_symlink_registry_ancestor_is_not_read(tmp_path: Path) -> None:
    safe = tmp_path / "safe"
    outside = tmp_path / "outside"
    safe.mkdir()
    outside_registry = ModelRegistry(outside / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(outside_registry, "1.0.0")
    promote(outside_registry, "1.0.0")
    revision = outside_registry.inspect().revision
    (safe / "linked-parent").symlink_to(outside, target_is_directory=True)

    result = ModelRegistry(safe / "linked-parent" / "registry", limits=LIMITS).load_production(
        ArtifactKind.THERMAL_MODEL,
        COMPATIBILITY,
    )

    assert result.status is ArtifactLoadStatus.INVALID_REGISTRY
    assert outside_registry.inspect().revision == revision


def test_symlink_payload_cannot_overwrite_file_outside_registry(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    artifact_directory = root / "artifacts" / "thermal_model" / "rack-thermal" / "1.0.0"
    artifact_directory.mkdir(parents=True)
    outside = tmp_path / "outside.payload"
    outside.write_bytes(b"keep-me")
    (artifact_directory / "artifact.payload").symlink_to(outside)
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)

    with pytest.raises(UnsafeRegistryPathError, match="regular file"):
        registry.register_candidate(
            metadata("1.0.0"),
            payload("1.0.0"),
            actor="trainer",
            reason="training completed",
        )

    assert outside.read_bytes() == b"keep-me"
    assert not (root / "registry.json").exists()


def test_symlink_or_nonregular_payload_is_not_read(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    artifact_path = (
        root / "artifacts" / "thermal_model" / "rack-thermal" / "1.0.0" / "artifact.payload"
    )
    outside = tmp_path / "outside.payload"
    outside.write_bytes(payload("1.0.0"))
    artifact_path.unlink()
    artifact_path.symlink_to(outside)

    linked = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)
    artifact_path.unlink()
    artifact_path.mkdir()
    nonregular = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert linked.status is ArtifactLoadStatus.ARTIFACT_UNAVAILABLE
    assert nonregular.status is ArtifactLoadStatus.ARTIFACT_UNAVAILABLE
    assert outside.read_bytes() == payload("1.0.0")


def test_fifo_payload_is_rejected_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    stored = artifact_path(root)
    stored.unlink()
    os.mkfifo(stored)

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert result.status is ArtifactLoadStatus.ARTIFACT_UNAVAILABLE


def test_oversized_payload_is_rejected_by_fstat_before_any_artifact_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    stored = artifact_path(root)
    with stored.open("r+b") as handle:
        handle.truncate(LIMITS.max_artifact_bytes + 1)
    target_identity = file_identity(stored)
    original_read = registry_module.os.read
    artifact_read = False

    def reject_artifact_read(file_fd: int, count: int) -> bytes:
        nonlocal artifact_read
        status = os.fstat(file_fd)
        if (status.st_dev, status.st_ino) == target_identity:
            artifact_read = True
            raise AssertionError("oversized artifact must be rejected before read")
        return original_read(file_fd, count)

    monkeypatch.setattr(registry_module.os, "read", reject_artifact_read)

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert result.status is ArtifactLoadStatus.ARTIFACT_UNAVAILABLE
    assert artifact_read is False


def test_truncated_payload_is_rejected_against_pinned_fstat_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    stored = artifact_path(root)
    target_identity = file_identity(stored)
    original_read = registry_module.os.read
    truncated = False

    def truncate_before_read(file_fd: int, count: int) -> bytes:
        nonlocal truncated
        status = os.fstat(file_fd)
        if not truncated and (status.st_dev, status.st_ino) == target_identity:
            with stored.open("r+b") as handle:
                handle.truncate(1)
            truncated = True
        return original_read(file_fd, count)

    monkeypatch.setattr(registry_module.os, "read", truncate_before_read)

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert truncated is True
    assert result.status is ArtifactLoadStatus.ARTIFACT_UNAVAILABLE


def test_growing_payload_read_is_bounded_to_preflight_size_plus_one_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    stored = artifact_path(root)
    target_identity = file_identity(stored)
    preflight_size = stored.stat().st_size
    original_read = registry_module.os.read
    artifact_read_requests: list[int] = []
    grown = False

    def grow_before_read(file_fd: int, count: int) -> bytes:
        nonlocal grown
        status = os.fstat(file_fd)
        if (status.st_dev, status.st_ino) == target_identity:
            artifact_read_requests.append(count)
            if not grown:
                with stored.open("r+b") as handle:
                    handle.truncate(LIMITS.max_artifact_bytes + 1)
                grown = True
        return original_read(file_fd, count)

    monkeypatch.setattr(registry_module.os, "read", grow_before_read)

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert grown is True
    assert result.status is ArtifactLoadStatus.ARTIFACT_UNAVAILABLE
    assert sum(artifact_read_requests) == preflight_size + 1


def test_payload_fd_remains_pinned_when_path_is_replaced_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    stored = artifact_path(root)
    displaced = tmp_path / "displaced.payload"
    replacement = b'{"model":"replacement"}'
    target_identity = file_identity(stored)
    original_read = registry_module.os.read
    replaced = False

    def replace_path_before_read(file_fd: int, count: int) -> bytes:
        nonlocal replaced
        status = os.fstat(file_fd)
        if not replaced and (status.st_dev, status.st_ino) == target_identity:
            stored.rename(displaced)
            stored.write_bytes(replacement)
            replaced = True
        return original_read(file_fd, count)

    monkeypatch.setattr(registry_module.os, "read", replace_path_before_read)

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert replaced is True
    assert result.status is ArtifactLoadStatus.LOADED
    assert result.artifact is not None
    assert result.artifact.payload == payload("1.0.0")
    assert stored.read_bytes() == replacement


def test_symlink_registry_snapshot_is_not_trusted(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema_version": 1, "revision": 0}', encoding="utf-8")
    (root / "registry.json").symlink_to(outside)

    result = ModelRegistry(root, limits=LIMITS).load_production(
        ArtifactKind.THERMAL_MODEL, COMPATIBILITY
    )

    assert result.status is ArtifactLoadStatus.INVALID_REGISTRY


def test_load_pins_root_across_path_replacement(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "registry"
    displaced = tmp_path / "displaced-registry"
    outside = tmp_path / "outside"
    outside.mkdir()
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    original_read = registry._read_regular_file
    replaced = False

    def read_then_replace_root(
        directory_fd: int,
        name: str,
        *,
        missing_ok: bool = False,
        max_bytes: int | None = None,
    ) -> bytes | None:
        nonlocal replaced
        content = original_read(
            directory_fd,
            name,
            missing_ok=missing_ok,
            max_bytes=max_bytes,
        )
        if name == "registry.json" and not replaced:
            root.rename(displaced)
            root.symlink_to(outside, target_is_directory=True)
            replaced = True
        return content

    monkeypatch.setattr(registry, "_read_regular_file", read_then_replace_root)

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert replaced is True
    assert result.status is ArtifactLoadStatus.LOADED
    assert result.artifact is not None
    assert result.artifact.payload == payload("1.0.0")
    assert tuple(outside.iterdir()) == ()


def test_loaded_trace_metadata_has_version_checksum_and_schema(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    trace = result.trace_metadata()["model_registry"]
    assert isinstance(trace, dict)
    assert trace["model_version"] == "1.0.0"
    assert trace["artifact_sha256"] == metadata("1.0.0").sha256
    assert trace["feature_schema_version"] == "thermal-features-v1"
    assert "authority_stage" not in trace, "Model Promotion と Authority Rollout を混同しない"


def test_only_the_verification_path_issues_an_artifact_attestation(tmp_path: Path) -> None:
    """**Registry を通った事実を、型で示せるようにする**（決定記録 0048 §2.4 / 0052 §2.1）。

    `VerifiedArtifact` は誰でも組み立てられたため、その型であること自体は証明にならなかった。
    発行できない `ArtifactAttestation` を必須にして、検証していない artifact を取り違えて
    制御経路へ渡す配線ミスを止める。暗号的な保証ではない（決定記録 0050 §3）。
    """
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")

    loaded = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert loaded.artifact is not None
    attestation = loaded.artifact.attestation
    assert attestation.model_id == "rack-thermal"
    assert attestation.version == "1.0.0"
    assert attestation.artifact_sha256 == metadata("1.0.0").sha256
    assert attestation.kind is ArtifactKind.THERMAL_MODEL
    assert attestation.authority_compatibility == (AuthorityStage.SHADOW, AuthorityStage.LIMITED)
    assert attestation.registry_revision == registry.inspect().revision
    assert attestation.trace_metadata()["artifact_sha256"] == metadata("1.0.0").sha256

    with pytest.raises(TypeError, match="検証経路"):
        registry_module.ArtifactAttestation()
    with pytest.raises(TypeError, match="Registry の検証経路"):
        registry_module.ArtifactAttestation._issue(metadata("1.0.0"), 0)
    with pytest.raises(AttributeError, match="不変"):
        attestation._model_id = "other"


def test_a_verified_artifact_cannot_be_assembled_without_an_attestation(tmp_path: Path) -> None:
    """証拠なしでは `VerifiedArtifact` を作れず、別 artifact の証拠も付け替えられない。"""
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    register_and_validate(registry, "1.1.0")
    promote(registry, "1.0.0")
    loaded = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)
    assert loaded.artifact is not None

    with pytest.raises(ValidationError, match="attestation"):
        registry_module.VerifiedArtifact(metadata=metadata("1.0.0"), payload=payload("1.0.0"))
    with pytest.raises(ValidationError, match="別の artifact"):
        registry_module.VerifiedArtifact(
            metadata=metadata("1.1.0"),
            payload=payload("1.1.0"),
            attestation=loaded.artifact.attestation,
        )


def test_the_attestation_records_lifecycle_and_production_provenance(tmp_path: Path) -> None:
    """**明示 version の読み込みを、そのまま制御へ配線できないようにする**（#86 / 0052 §2.1）。

    `load_version()` は Replay / offline 評価のために候補・検証済み・引退も返す。attestation に
    lifecycle と production pointer を載せ、active 制御側（#86）が要求できるようにする。
    """
    root = tmp_path / "registry"
    registry = ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)
    register_and_validate(registry, "1.0.0")
    register_and_validate(registry, "1.1.0")
    promote(registry, "1.0.0")

    production = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)
    pinned_production = registry.load_version(metadata("1.0.0").ref, COMPATIBILITY)
    pinned_validated = registry.load_version(metadata("1.1.0").ref, COMPATIBILITY)

    assert production.artifact is not None
    assert production.artifact.attestation.status is ArtifactStatus.PRODUCTION
    assert production.artifact.attestation.production_active is True
    # production pointer そのものを version 指定で読んだ場合も production として扱う。
    assert pinned_production.artifact is not None
    assert pinned_production.artifact.attestation.production_active is True
    # promotion を経ていない artifact は、version を固定しても production にならない。
    assert pinned_validated.artifact is not None
    assert pinned_validated.artifact.attestation.status is ArtifactStatus.VALIDATED
    assert pinned_validated.artifact.attestation.production_active is False
    trace = pinned_validated.artifact.attestation.trace_metadata()
    assert trace["artifact_status"] == "validated"
    assert trace["production_active"] is False

    with pytest.raises(ValueError, match="production pointer"):
        registry_module.ArtifactAttestation._issue(
            metadata("1.1.0"),
            0,
            status=ArtifactStatus.VALIDATED,
            production_active=True,
            _token=registry_module._ATTESTATION_ISSUE_TOKEN,
        )
