"""fand の起動時の証明書の照合（#288 / 決定記録 0152 §2.4 段 3・§2.8・0157 §2.3）。

守りたいこと（すべて実機不要。ルール 7）:

1. 認証ランナーが作った本物の証明書を本物の Registry に昇格し、すべて揃えたときだけ ``certified``
2. 0152 §2.4 の各理由ごとに1つずつ壊した入力で、その理由になり B0（起動の形が違えば
   ``environment_mismatch``。0157 §2.3）
3. 偽の証明書（認証領域だけ広げた・``eps`` だけ変えた・``grid_points`` だけ変えた・ハッシュだけ
   変えた）はどれも通らない（``certificate_sha256`` を偽の欄から計算し直しても通らない）
4. 証明書と thermal_model は**同じ1つの snapshot** から読む
5. 稼働中に証明書か thermal_model の production が変わると印が立ち、元に戻しても下りない。
   tracker はその tick から B0 に下げ、再起動まで上げない
6. 実効の段階 = min(設定, 実装の上限, 照合の結果)。設定より上がらない
7. fand は B1 を設定したときだけ証明書を読み、照合の結果を1行のログに残す。trace は
   ``certification_mismatch`` のまま、``requested_demand`` は ``stage: off`` と同じ
8. AI 層・API・eventd・管理ソケットと Safety / Guard / Fallback / hardware は照合の module を
   import しない
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

from coldaisle.control.config import ControlConfig
from coldaisle.control.learned_handoff import AvailableCalibration, UnavailableCalibration
from coldaisle.control.model_registry import (
    ApprovalAction,
    ArtifactCapability,
    ArtifactFormat,
    ArtifactKind,
    ArtifactLoadResult,
    ArtifactLoadStatus,
    ArtifactMetadata,
    HumanApproval,
    ModelCompatibility,
    ModelRegistry,
)
from coldaisle.control.schema import AuthorityStage, WorkloadHintStage, WorkloadRegime
from coldaisle.control.supervisor import workload_hint as workload_hint_module
from coldaisle.control.supervisor.workload_hint import (
    HintCertificationLatch,
    WorkloadHintTracker,
    effective_stage,
)
from coldaisle.hint_certificate import HintCertificate, certificate_sha256
from coldaisle.hint_certificate_digest import LaunchForm
from coldaisle.hint_certification import (
    CERTIFICATE_COMPATIBILITY,
    HintCertification,
    HintCertificationResult,
    verify_hint_certificate,
)
from coldaisle.hint_certify import EXIT_CERTIFIED, certify
from coldaisle.learned_channel.registry_watch import read_startup_registry
from coldaisle.startup_calibration import read_startup_calibration
from test_hint_certify import REGISTRY_LIMITS_DIR, TARGETS, inputs, write_calibration
from test_learned_mpc import ALL_STAGES, FEATURES, register_v2

APPROVED_AT_MS = 1_700_000_000_000
CERTIFICATE_MODEL_ID = "rack-hint-certificate"
SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"


def isolated(_entry: str) -> LaunchForm:
    """pytest のプロセスを ``-I -m`` の起動に見せる（起動の形の確かめ方だけを差し替える）。"""
    return LaunchForm.ISOLATED_MODULE


def other(_entry: str) -> LaunchForm:
    return LaunchForm.OTHER


# ---------------------------------------------------------------- 本物の証明書を作る


@dataclasses.dataclass(frozen=True)
class World:
    """ランナーが合格した構成。``bare`` は証明書を昇格する前の Registry の写し。"""

    root: Path
    bare: Path
    config_dir: Path
    calibration: Path
    lockfile: Path
    certificate: HintCertificate
    payload: bytes


def _limits() -> Any:
    from coldaisle.control.model_registry import load_model_registry_limits

    return load_model_registry_limits(REGISTRY_LIMITS_DIR)


def certificate_metadata(
    payload: bytes,
    certificate: HintCertificate,
    *,
    version: str = "1.0.0",
    source_run: str | None = None,
    thermal_key: str | None = None,
) -> ArtifactMetadata:
    named = certificate.thermal_model
    return ArtifactMetadata(
        kind=ArtifactKind.HINT_CERTIFICATE,
        artifact_format=ArtifactFormat.JSON,
        capability=ArtifactCapability.HINT_MONOTONICITY_CERTIFICATE,
        model_id=CERTIFICATE_MODEL_ID,
        version=version,
        created_at="2026-10-20T10:00:00+09:00",
        training_dataset_version=thermal_key or f"thermal_model/{named.model_id}/{named.version}",
        source_runs=(source_run or certificate.certificate_sha256,),
        feature_schema_version=CERTIFICATE_COMPATIBILITY.feature_schema_version,
        target_schema_version=CERTIFICATE_COMPATIBILITY.target_schema_version,
        code_commit=None,
        sha256=hashlib.sha256(payload).hexdigest(),
        model_family="hint-monotonicity-certificate",
        hyperparameters={},
        authority_compatibility=(AuthorityStage.SHADOW,),
    )


def promote(
    root: Path,
    metadata: ArtifactMetadata,
    payload: bytes,
    compatibility: ModelCompatibility = CERTIFICATE_COMPATIBILITY,
) -> None:
    registry = ModelRegistry(root, limits=_limits())
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
        approval=HumanApproval(
            action=ApprovalAction.PROMOTE,
            artifact=metadata.ref,
            artifact_sha256=metadata.sha256,
            expected_revision=revision,
            approver="model-operator",
            approved_at_ms=APPROVED_AT_MS,
            reason="RulePolicy の本番値と実時間の試験の合格を確かめた",
        ),
        expected_revision=revision,
    )


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    base = tmp_path_factory.mktemp("hint-certification")
    root = base / "registry"
    register_v2(root, features=FEATURES, targets=TARGETS, authority=ALL_STAGES)
    bare = base / "bare-registry"
    shutil.copytree(root, bare)
    given = inputs(base, root)
    assert certify(given) == EXIT_CERTIFIED
    payload = given.out.read_bytes()
    certificate = HintCertificate.model_validate_json(payload)
    promote(root, certificate_metadata(payload, certificate), payload)
    assert given.calibration is not None
    return World(
        root=root,
        bare=bare,
        config_dir=given.config_dir,
        calibration=given.calibration,
        lockfile=given.lockfile,
        certificate=certificate,
        payload=payload,
    )


def fresh_registry(world: World, tmp_path: Path) -> Path:
    """証明書を昇格する前の Registry の写し（偽の証明書を登録するため）。"""
    root = tmp_path / "registry"
    shutil.copytree(world.bare, root)
    return root


def run_verify(
    world: World,
    *,
    root: Path | None = None,
    config_dir: Path | None = None,
    calibration: Path | str | None = "world",
    lockfile: Path | str | None = "world",
    launch_form: Any = isolated,
    limits: Any = "config",
) -> HintCertificationResult:
    startup = read_startup_registry(
        world.root if root is None else root,
        limits_dir=REGISTRY_LIMITS_DIR,
        hint_latch=HintCertificationLatch(),
    )
    control = ControlConfig.from_directory(config_dir or world.config_dir)
    frame_calibration: AvailableCalibration | UnavailableCalibration = read_startup_calibration(
        world.calibration if calibration == "world" else calibration  # type: ignore[arg-type]
    )[1]
    return verify_hint_certificate(
        source=startup.hint_certificate,
        supervisor=control.policy.supervisor,
        limits=control.safety.workload_hint if limits == "config" else limits,
        lockfile=world.lockfile if lockfile == "world" else lockfile,  # type: ignore[arg-type]
        calibration=frame_calibration,
        launch_form=launch_form,
    )


def register_fake(world: World, tmp_path: Path, body: dict[str, Any]) -> Path:
    """証明書の JSON を書き換えて登録する（metadata は書き換えた JSON に合わせる）。"""
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    root = fresh_registry(world, tmp_path)
    fake = HintCertificate.model_construct(**body)
    metadata = certificate_metadata(
        payload, world.certificate, source_run=str(fake.certificate_sha256)
    )
    promote(root, metadata, payload)
    return root


def certificate_document(world: World) -> dict[str, Any]:
    return json.loads(world.payload)


# ---------------------------------------------------------------- 合格


def test_a_certificate_from_the_runner_is_certified_when_everything_matches(world: World) -> None:
    result = run_verify(world)
    assert result.certification is HintCertification.CERTIFIED
    assert result.certified
    assert result.certificate_sha256 == world.certificate.certificate_sha256
    assert result.launch_form is LaunchForm.ISOLATED_MODULE


def test_certificate_and_thermal_model_come_from_one_snapshot(world: World) -> None:
    startup = read_startup_registry(
        world.root, limits_dir=REGISTRY_LIMITS_DIR, hint_latch=HintCertificationLatch()
    )
    source = startup.hint_certificate
    assert source is not None
    assert source.load.status is ArtifactLoadStatus.LOADED
    assert source.load.registry_revision == startup.binding.provenance.revision
    assert source.thermal_model == world.certificate.thermal_model
    pinned = startup.binding.expected_artifacts.thermal_model
    assert pinned is not None
    assert pinned.artifact_sha256 == world.certificate.thermal_model.artifact_sha256


def test_without_a_latch_the_registry_read_does_not_load_the_certificate(world: World) -> None:
    startup = read_startup_registry(world.root, limits_dir=REGISTRY_LIMITS_DIR)
    assert startup.hint_certificate is None


# ---------------------------------------------------------------- 照合の手順の理由（0152 §2.4）


def test_no_registry_is_registry_unbound(world: World, tmp_path: Path) -> None:
    control = ControlConfig.from_directory(world.config_dir)
    result = verify_hint_certificate(
        source=None,
        supervisor=control.policy.supervisor,
        limits=control.safety.workload_hint,
        lockfile=world.lockfile,
        calibration=read_startup_calibration(world.calibration)[1],
        launch_form=isolated,
    )
    assert result.certification is HintCertification.REGISTRY_UNBOUND
    startup = read_startup_registry(
        None, limits_dir=REGISTRY_LIMITS_DIR, hint_latch=HintCertificationLatch()
    )
    assert startup.hint_certificate is None
    unreadable = read_startup_registry(
        world.root, limits_dir=tmp_path / "no-limits", hint_latch=HintCertificationLatch()
    )
    assert unreadable.hint_certificate is None


def test_no_certificate_production_is_certificate_missing(world: World) -> None:
    result = run_verify(world, root=world.bare)
    assert result.certification is HintCertification.CERTIFICATE_MISSING
    assert result.certificate_sha256 is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ArtifactLoadStatus.CHECKSUM_MISMATCH, HintCertification.CERTIFICATE_UNREADABLE),
        (ArtifactLoadStatus.ARTIFACT_UNAVAILABLE, HintCertification.CERTIFICATE_UNREADABLE),
        (ArtifactLoadStatus.INVALID_ARTIFACT_FORMAT, HintCertification.CERTIFICATE_UNREADABLE),
        (ArtifactLoadStatus.INVALID_REGISTRY, HintCertification.CERTIFICATE_UNREADABLE),
        (ArtifactLoadStatus.SCHEMA_MISMATCH, HintCertification.CERTIFICATE_INVALID),
        (ArtifactLoadStatus.AUTHORITY_INCOMPATIBLE, HintCertification.CERTIFICATE_INVALID),
        (ArtifactLoadStatus.NO_PRODUCTION, HintCertification.CERTIFICATE_MISSING),
    ],
)
def test_registry_load_failures_map_to_closed_reasons(
    world: World, status: ArtifactLoadStatus, expected: HintCertification
) -> None:
    startup = read_startup_registry(
        world.root, limits_dir=REGISTRY_LIMITS_DIR, hint_latch=HintCertificationLatch()
    )
    assert startup.hint_certificate is not None
    source = dataclasses.replace(
        startup.hint_certificate,
        load=ArtifactLoadResult(status=status, registry_revision=1, detail="試験"),
    )
    control = ControlConfig.from_directory(world.config_dir)
    result = verify_hint_certificate(
        source=source,
        supervisor=control.policy.supervisor,
        limits=control.safety.workload_hint,
        lockfile=world.lockfile,
        calibration=read_startup_calibration(world.calibration)[1],
        launch_form=isolated,
    )
    assert result.certification is expected


def test_a_corrupted_certificate_file_is_certificate_unreadable(
    world: World, tmp_path: Path
) -> None:
    root = tmp_path / "registry"
    shutil.copytree(world.root, root)
    stored = [
        path for path in root.rglob("*") if path.is_file() and path.read_bytes() == world.payload
    ]
    assert len(stored) == 1
    stored[0].write_bytes(world.payload.replace(b"passed", b"passeD"))
    assert run_verify(world, root=root).certification is HintCertification.CERTIFICATE_UNREADABLE


def test_a_certificate_too_large_for_the_registry_limit_is_unreadable(world: World) -> None:
    startup = read_startup_registry(
        world.root, limits_dir=REGISTRY_LIMITS_DIR, hint_latch=HintCertificationLatch()
    )
    assert startup.hint_certificate is not None
    source = dataclasses.replace(startup.hint_certificate, max_artifact_bytes=16)
    control = ControlConfig.from_directory(world.config_dir)
    result = verify_hint_certificate(
        source=source,
        supervisor=control.policy.supervisor,
        limits=control.safety.workload_hint,
        lockfile=world.lockfile,
        calibration=read_startup_calibration(world.calibration)[1],
        launch_form=isolated,
    )
    assert result.certification is HintCertification.CERTIFICATE_UNREADABLE


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda body: body.update(schema_version="hint-certificate-v9"), id="schema"),
        pytest.param(lambda body: body.update(result="failed"), id="result"),
        pytest.param(lambda body: body.update(grid_points=body["grid_points"] + 1), id="grid"),
    ],
)
def test_an_invalid_certificate_is_certificate_invalid(
    world: World, tmp_path: Path, edit: Any
) -> None:
    body = certificate_document(world)
    edit(body)
    root = register_fake(world, tmp_path, body)
    assert run_verify(world, root=root).certification is HintCertification.CERTIFICATE_INVALID


@pytest.mark.parametrize(
    "metadata_edit",
    [
        pytest.param({"source_run": "cd" * 32}, id="source-runs"),
        pytest.param({"thermal_key": "thermal_model/other-model/9.9.9"}, id="thermal-key"),
    ],
)
def test_metadata_that_disagrees_with_the_certificate_is_invalid(
    world: World, tmp_path: Path, metadata_edit: dict[str, str]
) -> None:
    root = fresh_registry(world, tmp_path)
    promote(
        root, certificate_metadata(world.payload, world.certificate, **metadata_edit), world.payload
    )
    assert run_verify(world, root=root).certification is HintCertification.CERTIFICATE_INVALID


def test_missing_safety_limits_is_eps_bounds_invalid(world: World) -> None:
    result = run_verify(world, limits=None)
    assert result.certification is HintCertification.EPS_BOUNDS_INVALID


def test_an_underivable_gate_is_hint_gate_invalid(world: World, tmp_path: Path) -> None:
    from test_control_config import write_documents
    from test_hint_certify import certify_documents

    docs = certify_documents()
    docs["fan-policy.yaml"]["supervisor"]["workload_hint"].pop("monotonicity_gate")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    write_documents(config_dir, docs)  # type: ignore[arg-type]
    assert (
        run_verify(world, config_dir=config_dir).certification
        is HintCertification.HINT_GATE_INVALID
    )


def test_another_thermal_production_is_thermal_model_mismatch(world: World, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    shutil.copytree(world.root, root)
    register_v2(root, version="0.2.0", features=FEATURES, targets=TARGETS, authority=ALL_STAGES)
    assert run_verify(world, root=root).certification is HintCertification.THERMAL_MODEL_MISMATCH


def test_a_different_or_missing_lockfile_is_lockfile_mismatch(world: World, tmp_path: Path) -> None:
    other_lock = tmp_path / "uv.lock"
    other_lock.write_bytes(b"version = 2\n")
    assert run_verify(world, lockfile=other_lock).certification is (
        HintCertification.LOCKFILE_MISMATCH
    )
    assert run_verify(world, lockfile=None).certification is HintCertification.LOCKFILE_MISMATCH
    assert run_verify(world, lockfile=tmp_path / "absent.lock").certification is (
        HintCertification.LOCKFILE_MISMATCH
    )


def test_a_launch_form_other_than_isolated_module_is_environment_mismatch(world: World) -> None:
    """0157 §2.3: 指紋を計算せず、既存の語彙に倒す（新しい理由を作らない）。"""
    result = run_verify(world, launch_form=other)
    assert result.certification is HintCertification.ENVIRONMENT_MISMATCH
    assert result.launch_form is LaunchForm.OTHER


def test_the_real_pytest_process_is_not_isolated_module(world: World) -> None:
    from coldaisle.hint_certificate_digest import current_launch_form

    result = run_verify(world, launch_form=current_launch_form)
    assert result.certification is HintCertification.ENVIRONMENT_MISMATCH


def test_a_changed_hint_policy_is_hint_policy_mismatch(world: World, tmp_path: Path) -> None:
    from test_control_config import write_documents
    from test_hint_certify import certify_documents

    docs = certify_documents()
    contexts = docs["fan-policy.yaml"]["supervisor"]["rule_policy"]["contexts"]
    # ゲートの仕様（hint_gate）に入らない context の値を1つだけ変える
    idle = copy.deepcopy(contexts["idle"])
    contexts["idle"] = idle
    first = sorted(idle["weights"])[0]
    idle["weights"][first] = idle["weights"][first] * 0.5
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    write_documents(config_dir, docs)  # type: ignore[arg-type]
    assert (
        run_verify(world, config_dir=config_dir).certification
        is HintCertification.HINT_POLICY_MISMATCH
    )


def test_a_changed_gate_is_hint_gate_mismatch(world: World, tmp_path: Path) -> None:
    from test_control_config import provisional, write_documents
    from test_hint_certify import certify_documents, small_gate

    gate = small_gate(eps=provisional(0.02))
    docs = certify_documents(gate=gate)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    write_documents(config_dir, docs)  # type: ignore[arg-type]
    assert (
        run_verify(world, config_dir=config_dir).certification
        is HintCertification.HINT_GATE_MISMATCH
    )


def test_a_changed_or_unavailable_calibration_is_calibration_mismatch(
    world: World, tmp_path: Path
) -> None:
    offsets = json.loads(world.calibration.read_text(encoding="utf-8"))["offsets_c"]
    changed = {key: value + 0.25 for key, value in offsets.items()}
    other_calibration = write_calibration(tmp_path / "calibration.json", changed)
    assert run_verify(world, calibration=other_calibration).certification is (
        HintCertification.CALIBRATION_MISMATCH
    )
    assert run_verify(world, calibration=None).certification is (
        HintCertification.CALIBRATION_MISMATCH
    )


# ---------------------------------------------------------------- 偽の証明書（0152 §2.8）


def _widened(body: dict[str, Any]) -> None:
    axis = body["certified_domain"]["temperature_cpu"]
    axis["points"] = [*axis["points"], axis["points"][-1] + 10.0]
    axis["maximum"] = axis["points"][-1]
    body["grid_points"] = body["grid_points"] // 2 * 3


def _eps(body: dict[str, Any]) -> None:
    body["eps"] = body["eps"] + 0.01


def _eps_c(body: dict[str, Any]) -> None:
    body["eps_c"] = body["eps_c"] + 0.5


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        pytest.param(_widened, HintCertification.CERTIFIED_DOMAIN_MISMATCH, id="domain"),
        pytest.param(_eps, HintCertification.EPS_MISMATCH, id="eps"),
        pytest.param(_eps_c, HintCertification.EPS_MISMATCH, id="eps_c"),
    ],
)
@pytest.mark.parametrize("rehash", [True, False], ids=["rehashed", "original-hash"])
def test_a_forged_certificate_is_never_certified(
    world: World, tmp_path: Path, edit: Any, expected: HintCertification, rehash: bool
) -> None:
    body = certificate_document(world)
    edit(body)
    if rehash:
        forged = HintCertificate.model_validate_json(json.dumps(body))
        body["certificate_sha256"] = certificate_sha256(forged.body())
    root = register_fake(world, tmp_path, body)
    result = run_verify(world, root=root)
    assert not result.certified
    assert result.certification is expected


def test_a_hint_gate_digest_matching_the_config_with_another_domain_is_not_certified(
    world: World, tmp_path: Path
) -> None:
    """``hint_gate`` は今の設定に合わせ、``certified_domain`` だけ別の格子にした証明書。"""
    body = certificate_document(world)
    _widened(body)
    forged = HintCertificate.model_validate_json(json.dumps(body))
    assert forged.components.hint_gate == world.certificate.components.hint_gate
    body["certificate_sha256"] = certificate_sha256(forged.body())
    root = register_fake(world, tmp_path, body)
    assert run_verify(world, root=root).certification is (
        HintCertification.CERTIFIED_DOMAIN_MISMATCH
    )


def test_a_certificate_with_another_hash_is_certificate_hash_mismatch(
    world: World, tmp_path: Path
) -> None:
    body = certificate_document(world)
    body["certificate_sha256"] = "ef" * 32
    root = register_fake(world, tmp_path, body)
    assert run_verify(world, root=root).certification is (
        HintCertification.CERTIFICATE_HASH_MISMATCH
    )


@pytest.mark.parametrize("component", ["optimizer", "conditions"])
def test_worker_side_components_are_taken_from_the_certificate(
    world: World, tmp_path: Path, component: str
) -> None:
    """fand は worker 側の部品を計算しない（0152 §2.4 の 5）。変えても fand の照合は本体で通る。

    worker の照合（0152 §2.6）は段 4。ここでは fand が証明書の値を置くことだけを固定する。
    """
    body = certificate_document(world)
    body["components"][component] = "12" * 32
    forged = HintCertificate.model_validate_json(json.dumps(body))
    body["certificate_sha256"] = certificate_sha256(forged.body())
    root = register_fake(world, tmp_path, body)
    assert run_verify(world, root=root).certification is HintCertification.CERTIFIED


# ---------------------------------------------------------------- 稼働中の production の変化


def test_a_new_certificate_production_revokes_and_stays_revoked(
    world: World, tmp_path: Path
) -> None:
    root = tmp_path / "registry"
    shutil.copytree(world.root, root)
    latch = HintCertificationLatch()
    startup = read_startup_registry(root, limits_dir=REGISTRY_LIMITS_DIR, hint_latch=latch)
    assert startup.watch is not None
    assert startup.watch.check() == frozenset()
    assert not latch.revoked
    promote(
        root,
        certificate_metadata(world.payload, world.certificate, version="2.0.0"),
        world.payload,
    )
    startup.watch.check()
    assert latch.revoked
    # 元の証明書へ戻しても下ろさない（再起動まで上げない）
    registry = ModelRegistry(root, limits=_limits())
    revision = registry.inspect().revision
    first = certificate_metadata(world.payload, world.certificate)
    registry.rollback(
        ArtifactKind.HINT_CERTIFICATE,
        CERTIFICATE_COMPATIBILITY,
        approval=HumanApproval(
            action=ApprovalAction.ROLLBACK,
            artifact=first.ref,
            artifact_sha256=first.sha256,
            expected_revision=revision,
            approver="model-operator",
            approved_at_ms=APPROVED_AT_MS,
            reason="1つ前の証明書へ戻す",
        ),
        expected_revision=revision,
    )
    startup.watch.check()
    assert latch.revoked


def test_a_new_thermal_production_revokes(world: World, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    shutil.copytree(world.root, root)
    latch = HintCertificationLatch()
    startup = read_startup_registry(root, limits_dir=REGISTRY_LIMITS_DIR, hint_latch=latch)
    assert startup.watch is not None
    register_v2(root, version="0.3.0", features=FEATURES, targets=TARGETS, authority=ALL_STAGES)
    startup.watch.check()
    assert latch.revoked


def test_an_unrelated_registry_write_does_not_revoke(world: World, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    shutil.copytree(world.root, root)
    latch = HintCertificationLatch()
    startup = read_startup_registry(root, limits_dir=REGISTRY_LIMITS_DIR, hint_latch=latch)
    assert startup.watch is not None
    registry = ModelRegistry(root, limits=_limits())
    candidate = certificate_metadata(world.payload, world.certificate, version="3.0.0")
    registry.register_candidate(candidate, world.payload, actor="trainer", reason="候補だけ")
    startup.watch.check()
    assert not latch.revoked


def test_an_unreadable_registry_while_running_revokes(world: World, tmp_path: Path) -> None:
    root = tmp_path / "registry"
    shutil.copytree(world.root, root)
    latch = HintCertificationLatch()
    startup = read_startup_registry(root, limits_dir=REGISTRY_LIMITS_DIR, hint_latch=latch)
    assert startup.watch is not None
    (root / "registry.json").write_text("{", encoding="utf-8")
    startup.watch.check()
    assert latch.revoked


# ---------------------------------------------------------------- 実効の段階（min）


def _hint_config(stage: str) -> Any:
    from test_workload_hint_tracker import hint_config

    return hint_config(stage)


@pytest.mark.parametrize("configured", ["shadow_mpc", "active"])
def test_the_implementation_ceiling_keeps_b0_even_when_certified(configured: str) -> None:
    """B1 の Shadow solver はまだ無い（段 4）ので、照合が通っても B0。"""
    stage = WorkloadHintStage(configured)
    assert effective_stage(stage, certified=True) is WorkloadHintStage.OBSERVE
    assert effective_stage(stage, certified=False) is WorkloadHintStage.OBSERVE
    assert effective_stage(stage) is WorkloadHintStage.OBSERVE


def test_effective_stage_is_the_minimum_of_config_ceiling_and_certification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workload_hint_module, "IMPLEMENTED_STAGE", WorkloadHintStage.SHADOW_MPC)
    shadow = WorkloadHintStage.SHADOW_MPC
    assert effective_stage(shadow, certified=True) is shadow
    assert effective_stage(shadow, certified=False) is WorkloadHintStage.OBSERVE
    assert effective_stage(WorkloadHintStage.ACTIVE, certified=True) is shadow
    # 設定より上げない
    assert effective_stage(WorkloadHintStage.OBSERVE, certified=True) is WorkloadHintStage.OBSERVE
    assert effective_stage(WorkloadHintStage.OFF, certified=True) is WorkloadHintStage.OFF


def test_the_tracker_lowers_to_b0_from_the_tick_the_latch_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coldaisle.clock import ManualMonotonicClock, SimulatedClock
    from test_workload_hint_tracker import FakeMailbox

    monkeypatch.setattr(workload_hint_module, "IMPLEMENTED_STAGE", WorkloadHintStage.SHADOW_MPC)
    latch = HintCertificationLatch()
    tracker = WorkloadHintTracker(
        _hint_config("shadow_mpc"),
        mailbox=FakeMailbox(),
        boot_clock=SimulatedClock(0),
        monotonic=ManualMonotonicClock(1_000),
        certified=True,
        revocation=latch,
    )
    assert tracker.step(tick_id=0, ts_ms=0, regime=WorkloadRegime.IDLE).stage is (
        WorkloadHintStage.SHADOW_MPC
    )
    latch.revoke()
    assert tracker.step(tick_id=1, ts_ms=1, regime=WorkloadRegime.IDLE).stage is (
        WorkloadHintStage.OBSERVE
    )
    assert tracker.stage is WorkloadHintStage.OBSERVE
    assert tracker.configured_stage is WorkloadHintStage.SHADOW_MPC


def test_an_uncertified_tracker_stays_at_b0(monkeypatch: pytest.MonkeyPatch) -> None:
    from coldaisle.clock import ManualMonotonicClock, SimulatedClock
    from test_workload_hint_tracker import FakeMailbox

    monkeypatch.setattr(workload_hint_module, "IMPLEMENTED_STAGE", WorkloadHintStage.SHADOW_MPC)
    tracker = WorkloadHintTracker(
        _hint_config("shadow_mpc"),
        mailbox=FakeMailbox(),
        boot_clock=SimulatedClock(0),
        monotonic=ManualMonotonicClock(1_000),
    )
    assert tracker.stage is WorkloadHintStage.OBSERVE


# ---------------------------------------------------------------- fand の組み立て


def _fand_config_dir(tmp_path: Path, *, stage: str = "shadow_mpc") -> Path:
    from test_control_config import write_documents
    from test_hint_certify import certify_documents
    from test_simulated_fan_backend import hardware_config

    docs = certify_documents()
    docs["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    docs["fan-policy.yaml"]["supervisor"]["workload_hint"]["stage"] = stage
    config_dir = tmp_path / "control"
    config_dir.mkdir()
    write_documents(config_dir, docs)  # type: ignore[arg-type]
    return config_dir


def _certification_logs(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        dict(getattr(record, "fields", {}))
        for record in caplog.records
        if "certification" in getattr(record, "fields", {})
    ]


def test_fand_verifies_once_at_startup_and_logs_one_line(
    world: World, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import Config, build
    from test_control_loop import METRICS_PATH

    caplog.set_level(logging.INFO)
    daemon = build(
        Config(
            config_dir=_fand_config_dir(tmp_path),
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            admin_config=tmp_path / "missing.yaml",
            registry_root=world.root,
            registry_limits=REGISTRY_LIMITS_DIR,
            calibration=world.calibration,
            hint_lockfile=world.lockfile,
        ),
        watchdog=NullWatchdog(),
    )
    try:
        tick = daemon.loop.tick().tick
    finally:
        daemon.close()
    lines = _certification_logs(caplog)
    assert len(lines) == 1
    line = lines[0]
    # pytest のプロセスは `-I -m` で起動していないので environment_mismatch（0157 §2.4）。
    # それより前の手順（registry・証明書・ゲートの仕様・thermal_model・source_tree・lockfile）は通る
    assert line == {
        "configured_stage": "shadow_mpc",
        "effective_stage": "observe",
        "certification": "environment_mismatch",
        "certificate_sha256": world.certificate.certificate_sha256,
        "launch_form": "other",
    }
    assert tick.workload_hint.stage is WorkloadHintStage.OBSERVE


def test_fand_without_a_registry_logs_registry_unbound(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import Config, build
    from test_control_loop import METRICS_PATH

    caplog.set_level(logging.INFO)
    daemon = build(
        Config(
            config_dir=_fand_config_dir(tmp_path),
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            admin_config=tmp_path / "missing.yaml",
        ),
        watchdog=NullWatchdog(),
    )
    daemon.close()
    lines = _certification_logs(caplog)
    assert [line["certification"] for line in lines] == ["registry_unbound"]
    assert lines[0]["certificate_sha256"] is None


def test_fand_at_observe_does_not_read_the_certificate(
    world: World, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """設定が B0 なら照合しても実効は上がらない（min）ので、証明書を読まない・照合しない。"""
    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import Config, build
    from test_control_loop import METRICS_PATH

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("observe の fand は証明書を読まない")

    monkeypatch.setattr(ModelRegistry, "inspect_with_production", refuse)
    caplog.set_level(logging.INFO)
    daemon = build(
        Config(
            config_dir=_fand_config_dir(tmp_path, stage="observe"),
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            admin_config=tmp_path / "missing.yaml",
            registry_root=world.root,
            registry_limits=REGISTRY_LIMITS_DIR,
            calibration=world.calibration,
            hint_lockfile=world.lockfile,
        ),
        watchdog=NullWatchdog(),
    )
    daemon.close()
    assert _certification_logs(caplog) == []


def test_the_cli_takes_the_hint_lockfile(tmp_path: Path) -> None:
    from coldaisle.control_daemon import build_parser

    args = build_parser().parse_args(["--hint-lockfile", str(tmp_path / "uv.lock")])
    assert args.hint_lockfile == tmp_path / "uv.lock"
    assert build_parser().parse_args([]).hint_lockfile is None


# ---------------------------------------------------------------- 境界


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "package",
    [
        "ai",
        "api",
        "event_entry",
        "control_admin",
        "control/safety",
        "control/reactive",
        "control/fallback",
        "control/hardware",
        "control",
    ],
)
def test_llm_api_eventd_admin_and_control_do_not_import_the_verification(package: str) -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted((SRC / package).rglob("*.py"))
        if "coldaisle.hint_certification" in _imports(path)
    ]
    assert offenders == []


def test_the_verification_does_not_import_safety_guard_fallback_hardware_or_llm() -> None:
    forbidden = (
        "coldaisle.control.safety",
        "coldaisle.control.reactive",
        "coldaisle.control.fallback",
        "coldaisle.control.hardware",
        "coldaisle.ai",
        "coldaisle.api",
    )
    imported = _imports(SRC / "hint_certification.py")
    assert not [name for name in imported if name.startswith(forbidden)]


def test_the_verification_never_writes_the_registry() -> None:
    source = (SRC / "hint_certification.py").read_text(encoding="utf-8")
    for method in ("register_candidate", "mark_validated", "promote(", "rollback(", "retire("):
        assert method not in source
