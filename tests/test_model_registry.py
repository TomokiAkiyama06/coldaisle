"""Filesystem Control Model Registry (#104) tests; no model runtime or hardware required."""

from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event

import pytest
from pydantic import ValidationError

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
    RegistryEventKind,
    RegistrySnapshot,
    UnsafeRegistryPathError,
)

NOW_MS = 1_800_000_000_000
ACTOR = "model-operator"
COMPATIBILITY = ModelCompatibility(
    feature_schema_version="thermal-features-v1",
    target_schema_version="thermal-targets-v1",
    authority_stage=AuthorityStage.SHADOW,
)


def payload(version: str) -> bytes:
    return json.dumps({"model": version}, sort_keys=True).encode()


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
    registry = ModelRegistry(Path(root), SimulatedClock(NOW_MS))
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

    result = ModelRegistry(root).load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert result.status is ArtifactLoadStatus.NO_PRODUCTION
    assert result.fallback_required is True
    assert result.artifact is None
    assert not root.exists(), "read-only load は空の registry directory を作らない"


def test_candidate_and_production_coexist_and_candidate_is_not_implicitly_loaded(
    tmp_path: Path,
) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(root, SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    full_authority = COMPATIBILITY.model_copy(update={"authority_stage": AuthorityStage.FULL})

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, full_authority)

    assert result.status is ArtifactLoadStatus.AUTHORITY_INCOMPATIBLE
    assert result.fallback_required is True


def test_promotion_requires_validated_state_and_does_not_raise_authority(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(root, SimulatedClock(NOW_MS))
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


def test_two_concurrent_promotions_with_same_revision_cannot_clobber_each_other(
    tmp_path: Path,
) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(root, SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
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


def test_invalid_json_artifact_is_never_registered(tmp_path: Path) -> None:
    body = b"not-json"
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))

    with pytest.raises(ArtifactVerificationError):
        registry.register_candidate(
            metadata("1.0.0", content=body),
            body,
            actor="trainer",
            reason="training completed",
        )

    assert registry.inspect().revision == 0


def test_malformed_registry_state_returns_fallback_instead_of_loading(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    (root / "registry.json").write_text('{"schema_version": 999}', encoding="utf-8")

    result = ModelRegistry(root).load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert result.status is ArtifactLoadStatus.INVALID_REGISTRY
    assert result.fallback_required is True


def test_human_approval_cannot_be_postdated(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
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
    registry = ModelRegistry(root, SimulatedClock(NOW_MS))

    with pytest.raises(UnsafeRegistryPathError, match="symlink"):
        registry.register_candidate(
            metadata("1.0.0"),
            payload("1.0.0"),
            actor="trainer",
            reason="training completed",
        )

    assert tuple(outside.iterdir()) == ()
    assert not (root / "registry.json").exists()


def test_symlink_payload_cannot_overwrite_file_outside_registry(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    artifact_directory = root / "artifacts" / "thermal_model" / "rack-thermal" / "1.0.0"
    artifact_directory.mkdir(parents=True)
    outside = tmp_path / "outside.payload"
    outside.write_bytes(b"keep-me")
    (artifact_directory / "artifact.payload").symlink_to(outside)
    registry = ModelRegistry(root, SimulatedClock(NOW_MS))

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
    registry = ModelRegistry(root, SimulatedClock(NOW_MS))
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


def test_symlink_registry_snapshot_is_not_trusted(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema_version": 1, "revision": 0}', encoding="utf-8")
    (root / "registry.json").symlink_to(outside)

    result = ModelRegistry(root).load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    assert result.status is ArtifactLoadStatus.INVALID_REGISTRY


def test_loaded_trace_metadata_has_version_checksum_and_schema(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS))
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")

    result = registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY)

    trace = result.trace_metadata()["model_registry"]
    assert isinstance(trace, dict)
    assert trace["model_version"] == "1.0.0"
    assert trace["artifact_sha256"] == metadata("1.0.0").sha256
    assert trace["feature_schema_version"] == "thermal-features-v1"
    assert "authority_stage" not in trace, "Model Promotion と Authority Rollout を混同しない"
