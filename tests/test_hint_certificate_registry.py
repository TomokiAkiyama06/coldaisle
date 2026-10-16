"""Registry の kind `hint_certificate` と snapshot v4（#288 / 決定記録 0152 §2.2 / §2.7a / §2.8）。

実機・モデルの実行は要らない。証明書の中身は Registry が読まない（0062 §2.3）ので、ここでは
metadata の形・lifecycle・snapshot の版・trace の provenance だけを確かめる。
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control import (
    COUNTERFACTUAL_CAPABILITIES,
    ApprovalAction,
    ArtifactCapability,
    ArtifactFormat,
    ArtifactKind,
    ArtifactLoadStatus,
    ArtifactMetadata,
    AuthorityStage,
    HumanApproval,
    ModelCompatibility,
    ModelRegistry,
    RegistrySnapshot,
    load_model_registry_limits,
)
from coldaisle.control.model_registry import (
    HINT_CERTIFICATE_MODEL_FAMILY,
    MODEL_REGISTRY_SCHEMA_VERSION,
    certified_thermal_model_ref,
)
from coldaisle.control.schema import REGISTRY_ARTIFACT_KINDS, ControlTick

NOW_MS = 1_800_000_000_000
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIMITS = load_model_registry_limits(CONFIG_DIR)
ACTOR = "model-operator"
THERMAL_KEY = "thermal_model/rack-thermal/1.0.0"
CERTIFICATE_SHA256 = "ab" * 32
THERMAL_COMPATIBILITY = ModelCompatibility(
    feature_schema_version="thermal-features-v1",
    target_schema_version="thermal-targets-v1",
    authority_stage=AuthorityStage.SHADOW,
)
# 証明書の feature / target の版の値は 0152 が決めていない（後の段で決める）。ここでは仮の値。
CERTIFICATE_COMPATIBILITY = ModelCompatibility(
    feature_schema_version="hint-certificate-test",
    target_schema_version="hint-certificate-test",
    authority_stage=AuthorityStage.SHADOW,
)


def thermal_payload(version: str) -> bytes:
    return json.dumps({"model": version}, sort_keys=True).encode()


def certificate_payload(version: str) -> bytes:
    return json.dumps({"certificate": version}, sort_keys=True).encode()


def thermal_metadata(version: str) -> ArtifactMetadata:
    return ArtifactMetadata(
        kind=ArtifactKind.THERMAL_MODEL,
        artifact_format=ArtifactFormat.JSON,
        capability=ArtifactCapability.OBSERVATIONAL_REPLAY,
        model_id="rack-thermal",
        version=version,
        created_at="2026-10-16T10:00:00+09:00",
        training_dataset_version="dataset-v4",
        source_runs=("run-001",),
        feature_schema_version="thermal-features-v1",
        target_schema_version="thermal-targets-v1",
        code_commit="0123456789abcdef",
        sha256=sha256(thermal_payload(version)).hexdigest(),
        model_family="linear-baseline",
        hyperparameters={},
        authority_compatibility=(AuthorityStage.SHADOW,),
    )


def certificate_fields(version: str = "1.0.0") -> dict[str, object]:
    return {
        "kind": ArtifactKind.HINT_CERTIFICATE,
        "artifact_format": ArtifactFormat.JSON,
        "capability": ArtifactCapability.HINT_MONOTONICITY_CERTIFICATE,
        "model_id": "rack-hint-certificate",
        "version": version,
        "created_at": "2026-10-16T10:00:00+09:00",
        "training_dataset_version": THERMAL_KEY,
        "source_runs": (CERTIFICATE_SHA256,),
        "feature_schema_version": "hint-certificate-test",
        "target_schema_version": "hint-certificate-test",
        "code_commit": None,
        "sha256": sha256(certificate_payload(version)).hexdigest(),
        "model_family": HINT_CERTIFICATE_MODEL_FAMILY,
        "hyperparameters": {},
        "authority_compatibility": (AuthorityStage.SHADOW,),
    }


def certificate_metadata(version: str = "1.0.0", **overrides: object) -> ArtifactMetadata:
    return ArtifactMetadata(**(certificate_fields(version) | overrides))  # type: ignore[arg-type]


def approval(metadata: ArtifactMetadata, revision: int) -> HumanApproval:
    return HumanApproval(
        action=ApprovalAction.PROMOTE,
        artifact=metadata.ref,
        artifact_sha256=metadata.sha256,
        expected_revision=revision,
        approver=ACTOR,
        approved_at_ms=NOW_MS,
        reason="RulePolicy の本番値と実時間の試験の合格を確かめた",
    )


def make_registry(root: Path) -> ModelRegistry:
    return ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)


def register_validate_promote(
    registry: ModelRegistry,
    metadata: ArtifactMetadata,
    payload: bytes,
    compatibility: ModelCompatibility,
) -> None:
    registry.register_candidate(metadata, payload, actor="trainer", reason="registered")
    registry.mark_validated(
        metadata.ref,
        offline_evaluation_ref=f"evaluation/offline/{metadata.version}",
        actor="evaluator",
        reason="validated",
        expected_revision=registry.inspect().revision,
    )
    revision = registry.inspect().revision
    registry.promote(
        metadata.ref,
        compatibility,
        shadow_evaluation_ref=f"evaluation/shadow/{metadata.version}",
        approval=approval(metadata, revision),
        expected_revision=revision,
    )


def registry_with_both(root: Path) -> ModelRegistry:
    registry = make_registry(root)
    register_validate_promote(
        registry, thermal_metadata("1.0.0"), thermal_payload("1.0.0"), THERMAL_COMPATIBILITY
    )
    register_validate_promote(
        registry, certificate_metadata(), certificate_payload("1.0.0"), CERTIFICATE_COMPATIBILITY
    )
    return registry


# ---------------------------------------------------------------- metadata の形（0152 §5 の 2 (a)）


def test_a_certificate_metadata_fixes_the_meaning_of_the_existing_fields() -> None:
    metadata = certificate_metadata()
    assert metadata.schema_version == MODEL_REGISTRY_SCHEMA_VERSION == 4
    assert metadata.ref.key == "hint_certificate/rack-hint-certificate/1.0.0"
    certified = certified_thermal_model_ref(metadata.training_dataset_version)
    assert certified is not None and certified.key == THERMAL_KEY


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"capability": ArtifactCapability.OBSERVATIONAL_REPLAY}, "組でだけ", id="kind-only"
        ),
        pytest.param({"schema_version": 3}, "v4 より前", id="old-schema"),
        pytest.param({"model_family": "linear-baseline"}, "model_family", id="family"),
        pytest.param({"training_dataset_version": "dataset-v4"}, "thermal_model", id="dataset"),
        pytest.param(
            {"training_dataset_version": "supervisor_policy/rack-thermal/1.0.0"},
            "thermal_model",
            id="other-kind-key",
        ),
        pytest.param(
            {"training_dataset_version": "thermal_model/rack-thermal/not-semver"},
            "thermal_model",
            id="bad-version",
        ),
        pytest.param(
            {"training_dataset_version": "thermal_model/rack-thermal/1.0.0/extra"},
            "thermal_model",
            id="extra-part",
        ),
        pytest.param(
            {"source_runs": (CERTIFICATE_SHA256, "cd" * 32)}, "source_runs", id="two-runs"
        ),
        pytest.param({"source_runs": ("run-001",)}, "source_runs", id="not-a-digest"),
        pytest.param({"source_runs": ("AB" * 32,)}, "source_runs", id="upper-hex"),
    ],
)
def test_a_certificate_metadata_outside_the_fixed_meaning_is_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        certificate_metadata(**overrides)


def test_the_certificate_capability_is_not_accepted_on_another_kind() -> None:
    fields = thermal_metadata("1.0.0").model_dump(mode="python") | {
        "capability": ArtifactCapability.HINT_MONOTONICITY_CERTIFICATE
    }
    with pytest.raises(ValidationError, match="組でだけ"):
        ArtifactMetadata.model_validate(fields)


def test_the_certificate_capability_cannot_be_bound_as_an_internal_model() -> None:
    """MPC の内部モデル・RL の policy として束縛される能力の集合に入らない（0152 §2.2）。"""
    assert ArtifactCapability.HINT_MONOTONICITY_CERTIFICATE not in COUNTERFACTUAL_CAPABILITIES
    assert (
        ArtifactCapability.HINT_MONOTONICITY_CERTIFICATE
        is not ArtifactCapability.SUPERVISOR_STRATEGY
    )


# ---------------------------------------------------------------- lifecycle（0152 §2.2）


def test_a_certificate_goes_through_the_thermal_model_lifecycle(tmp_path: Path) -> None:
    registry = registry_with_both(tmp_path / "registry")
    snapshot = registry.inspect()

    assert snapshot.schema_version == 4
    assert set(snapshot.production) == {ArtifactKind.THERMAL_MODEL, ArtifactKind.HINT_CERTIFICATE}
    loaded = registry.load_production(ArtifactKind.HINT_CERTIFICATE, CERTIFICATE_COMPATIBILITY)
    assert loaded.status is ArtifactLoadStatus.LOADED
    assert loaded.artifact is not None
    assert loaded.artifact.payload == certificate_payload("1.0.0")
    stored = json.loads((tmp_path / "registry" / "registry.json").read_text(encoding="utf-8"))
    assert stored["schema_version"] == 4


def test_a_certificate_promotion_needs_a_human_approval(tmp_path: Path) -> None:
    """承認の無い promotion を snapshot に書き込めない（0062 §2.2 / 0152 §2.2）。"""
    registry = registry_with_both(tmp_path / "registry")
    document = registry.inspect().model_dump(mode="json")
    promotion = document["audit"][-1]
    assert promotion["event"] == "promoted"
    assert promotion["artifact"]["kind"] == "hint_certificate"
    promotion["approval"] = None

    with pytest.raises(ValidationError, match="approval"):
        RegistrySnapshot.model_validate_json(json.dumps(document))


def test_a_certificate_can_be_rolled_back_to_the_previous_one(tmp_path: Path) -> None:
    registry = registry_with_both(tmp_path / "registry")
    second = certificate_metadata("2.0.0")
    register_validate_promote(
        registry, second, certificate_payload("2.0.0"), CERTIFICATE_COMPATIBILITY
    )
    revision = registry.inspect().revision
    first = certificate_metadata("1.0.0")
    registry.rollback(
        ArtifactKind.HINT_CERTIFICATE,
        CERTIFICATE_COMPATIBILITY,
        approval=HumanApproval(
            action=ApprovalAction.ROLLBACK,
            artifact=first.ref,
            artifact_sha256=first.sha256,
            expected_revision=revision,
            approver=ACTOR,
            approved_at_ms=NOW_MS,
            reason="1つ前の証明書へ戻す",
        ),
        expected_revision=revision,
    )
    snapshot = registry.inspect()
    assert snapshot.production[ArtifactKind.HINT_CERTIFICATE].active == first.ref
    # thermal_model の production は動かない（独立の slot）。
    assert snapshot.production[ArtifactKind.THERMAL_MODEL].active == thermal_metadata("1.0.0").ref


# ---------------------------------------------------------------- snapshot の版（0152 §2.2 / §2.8）


def test_an_older_snapshot_cannot_carry_a_certificate(tmp_path: Path) -> None:
    registry = registry_with_both(tmp_path / "registry")
    document = registry.inspect().model_dump(mode="json")
    document["schema_version"] = 3

    with pytest.raises(ValidationError, match="v4 より前"):
        RegistrySnapshot.model_validate_json(json.dumps(document))


def test_a_v3_snapshot_is_read_and_upgraded_to_v4_on_write(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = make_registry(root)
    register_validate_promote(
        registry, thermal_metadata("1.0.0"), thermal_payload("1.0.0"), THERMAL_COMPATIBILITY
    )
    path = root / "registry.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["schema_version"] = 3
    for record in stored["artifacts"].values():
        record["metadata"]["schema_version"] = 3
    path.write_text(json.dumps(stored), encoding="utf-8")

    reader = make_registry(root)
    loaded = reader.load_production(ArtifactKind.THERMAL_MODEL, THERMAL_COMPATIBILITY)
    assert loaded.status is ArtifactLoadStatus.LOADED
    # 読むだけでは書き換えない。
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 3

    reader.register_candidate(
        certificate_metadata(), certificate_payload("1.0.0"), actor="trainer", reason="registered"
    )
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 4


# ---------------------------------------------------------------- trace の provenance（0152 §2.7a）


def test_the_trace_provenance_keeps_the_four_bound_kinds(tmp_path: Path) -> None:
    provenance = registry_with_both(tmp_path / "registry").inspect().trace_provenance()

    assert set(provenance.production) == REGISTRY_ARTIFACT_KINDS
    assert "hint_certificate" not in provenance.production
    assert provenance.production["thermal_model"].artifact_sha256 == (
        thermal_metadata("1.0.0").sha256
    )


@pytest.mark.parametrize("version", range(10, 17))
def test_stored_traces_with_a_registry_still_validate(version: int) -> None:
    """保存済みの v10〜v16 の trace は、kind を足した後も再検証で落ちない（0152 §2.7a）。"""
    body = (FIXTURES / f"control_tick_v{version}.json").read_text(encoding="utf-8")
    tick = ControlTick.model_validate_json(body)
    assert tick.registry is not None
    assert set(tick.registry.production) == REGISTRY_ARTIFACT_KINDS
