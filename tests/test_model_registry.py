"""Filesystem Control Model Registry (#104) tests; no model runtime or hardware required."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control import (
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


def approval(reason: str = "shadow evaluation passed") -> HumanApproval:
    return HumanApproval(
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
    registry.promote(
        metadata(version).ref,
        COMPATIBILITY,
        shadow_evaluation_ref=f"evaluation/shadow/{version}",
        approval=approval(),
        expected_revision=registry.inspect().revision,
    )


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
            approval=approval(),
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
        approval=approval("production residual regressed"),
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
    assert event.approval == approval("production residual regressed")


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
            approval=approval("attempt rollback"),
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
                approval=approval(f"approve {version}"),
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


def test_pickle_and_framework_native_formats_are_not_in_the_schema() -> None:
    values = {member.value for member in ArtifactFormat}
    invalid = metadata("1.0.0").model_dump()
    invalid["artifact_format"] = "pickle"

    assert values == {"json", "onnx", "safetensors"}
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
    future = approval().model_copy(update={"approved_at_ms": NOW_MS + 1})
    revision = registry.inspect().revision

    with pytest.raises(ValueError, match="未来"):
        registry.promote(
            metadata("1.0.0").ref,
            COMPATIBILITY,
            shadow_evaluation_ref="evaluation/shadow/1.0.0",
            approval=future,
            expected_revision=revision,
        )

    assert registry.inspect().revision == revision


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
