"""Model Registry の起動時検証・監査の追跡・運用 CLI（#104 / 決定記録 0062）。

実機も推論器も要らない。ここで確かめるのは「registry が言ったこと」だけを信じる境界で、
それぞれの不変条件に対して**破ろうとする**試験を1つずつ置く。
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle import registry as cli
from coldaisle.clock import SimulatedClock
from coldaisle.control import (
    ApprovalAction,
    ArtifactCapability,
    ArtifactFormat,
    ArtifactHealth,
    ArtifactKind,
    ArtifactLoadStatus,
    ArtifactMetadata,
    ArtifactStatus,
    ArtifactVerificationError,
    AuthorityStage,
    HumanApproval,
    ModelCompatibility,
    ModelRegistry,
    ProductionHealth,
    RegistryEventKind,
    RegistryHealth,
    RegistryHealthReport,
    load_model_registry_limits,
)

NOW_MS = 1_700_000_000_000
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
LIMITS = load_model_registry_limits(CONFIG_DIR)
ACTOR = "model-operator"
FEATURE_SCHEMA = "thermal-features-v1"
TARGET_SCHEMA = "thermal-targets-v1"
COMPATIBILITY = ModelCompatibility(
    feature_schema_version=FEATURE_SCHEMA,
    target_schema_version=TARGET_SCHEMA,
    authority_stage=AuthorityStage.SHADOW,
)
CONTRACTS = {ArtifactKind.THERMAL_MODEL: COMPATIBILITY}


def payload(version: str) -> bytes:
    return json.dumps({"model": version}, sort_keys=True).encode()


def metadata(version: str, *, feature_schema_version: str = FEATURE_SCHEMA) -> ArtifactMetadata:
    return ArtifactMetadata(
        kind=ArtifactKind.THERMAL_MODEL,
        artifact_format=ArtifactFormat.JSON,
        capability=ArtifactCapability.OBSERVATIONAL_REPLAY,
        model_id="rack-thermal",
        version=version,
        created_at="2026-09-20T10:00:00+09:00",
        training_dataset_version="dataset-v4",
        source_runs=("run-001", "run-002"),
        feature_schema_version=feature_schema_version,
        target_schema_version=TARGET_SCHEMA,
        code_commit="0123456789abcdef",
        sha256=sha256(payload(version)).hexdigest(),
        model_family="linear-baseline",
        hyperparameters={"alpha": 0.25},
        authority_compatibility=(AuthorityStage.SHADOW, AuthorityStage.LIMITED),
    )


def approval_record(
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


def make_registry(root: Path) -> ModelRegistry:
    return ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)


def register_and_validate(
    registry: ModelRegistry,
    version: str,
    *,
    feature_schema_version: str = FEATURE_SCHEMA,
) -> None:
    record = metadata(version, feature_schema_version=feature_schema_version)
    registry.register_candidate(record, payload(version), actor="trainer", reason="trained")
    registry.mark_validated(
        record.ref,
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
        approval=approval_record(version, revision),
        expected_revision=revision,
    )


def production_registry(root: Path, *, versions: tuple[str, ...] = ("1.0.0",)) -> ModelRegistry:
    registry = make_registry(root)
    for version in versions:
        register_and_validate(registry, version)
        promote(registry, version)
    return registry


def artifact_file(root: Path, version: str) -> Path:
    return root / "artifacts" / "thermal_model" / "rack-thermal" / version / "artifact.payload"


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def approval_file(path: Path, approval: HumanApproval) -> Path:
    path.write_text(approval.model_dump_json(), encoding="utf-8")
    return path


def base_args(root: Path) -> list[str]:
    return ["--root", str(root), "--limits", str(CONFIG_DIR)]


def contract_args() -> list[str]:
    return [
        "--feature-schema",
        FEATURE_SCHEMA,
        "--target-schema",
        TARGET_SCHEMA,
        "--authority-stage",
        AuthorityStage.SHADOW.value,
    ]


def stdout_json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------------------
# 起動時検証: 書き換えない / 例外で起動を止めない
# --------------------------------------------------------------------------------------


def test_startup_verification_neither_writes_nor_creates_the_registry(tmp_path: Path) -> None:
    """**検証は読み取りだけ。** root が無ければ作らず、snapshot の bytes も触らない。"""
    missing = tmp_path / "absent"
    assert make_registry(missing).verify(CONTRACTS).health is RegistryHealth.NO_PRODUCTION
    assert not missing.exists()

    root = tmp_path / "registry"
    registry = production_registry(root)
    snapshot_path = root / "registry.json"
    before = (snapshot_path.read_bytes(), snapshot_path.stat().st_mtime_ns)
    report = registry.verify(CONTRACTS)
    assert (snapshot_path.read_bytes(), snapshot_path.stat().st_mtime_ns) == before
    assert report.revision == registry.inspect().revision


def test_a_candidate_is_never_reported_as_production(tmp_path: Path) -> None:
    """**候補を暗黙に production 扱いしない。** 承認の無い artifact は検証対象にも入らない。"""
    root = tmp_path / "registry"
    registry = make_registry(root)
    register_and_validate(registry, "1.0.0")

    report = registry.verify(CONTRACTS)
    assert report.health is RegistryHealth.NO_PRODUCTION
    assert report.productions == ()
    assert report.fallback_kinds() == ()


def test_corrupt_production_bytes_report_unusable_instead_of_raising(tmp_path: Path) -> None:
    """壊れた production で例外を投げない。Fallback を選べる結果として返す。"""
    root = tmp_path / "registry"
    registry = production_registry(root)
    artifact_file(root, "1.0.0").write_bytes(b'{"model": "tampered"}')

    report = registry.verify(CONTRACTS)
    assert report.health is RegistryHealth.UNUSABLE
    assert report.fallback_kinds() == (ArtifactKind.THERMAL_MODEL,)
    assert report.productions[0].active.load_status is ArtifactLoadStatus.CHECKSUM_MISMATCH


def test_feature_schema_mismatch_names_the_kind_that_must_fall_back(tmp_path: Path) -> None:
    """runtime contract と合わない production は、起動時に Fallback 対象として名指しされる。"""
    root = tmp_path / "registry"
    registry = production_registry(root)
    contracts = {
        ArtifactKind.THERMAL_MODEL: ModelCompatibility(
            feature_schema_version="thermal-features-v2",
            target_schema_version=TARGET_SCHEMA,
            authority_stage=AuthorityStage.SHADOW,
        )
    }

    report = registry.verify(contracts)
    assert report.health is RegistryHealth.UNUSABLE
    assert report.productions[0].active.load_status is ArtifactLoadStatus.SCHEMA_MISMATCH
    assert report.trace_metadata()["model_registry_health"] == {
        "health": "unusable",
        "registry_revision": report.revision,
        "supported_schema_version": 2,
        "productions": [
            {
                "artifact_kind": "thermal_model",
                "active": "thermal_model/rack-thermal/1.0.0",
                "active_load_status": "schema_mismatch",
                "compatibility_checked": True,
                "rollback_target": None,
                "rollback_available": False,
            }
        ],
        "fallback_kinds": ["thermal_model"],
        "unchecked_kinds": [],
    }


def test_missing_rollback_target_is_visible_as_degraded(tmp_path: Path) -> None:
    """戻り先が無いことを**黙って OK にしない**（決定記録 0037 §5 の未決事項）。"""
    root = tmp_path / "registry"
    report = production_registry(root).verify(CONTRACTS)
    assert report.health is RegistryHealth.DEGRADED
    assert report.productions[0].rollback_target is None
    assert report.productions[0].rollback_available is False
    assert report.fallback_kinds() == ()


def test_corrupt_rollback_target_is_degraded_even_though_production_loads(tmp_path: Path) -> None:
    """production が健全でも、戻り先が壊れていれば degraded と言う。"""
    root = tmp_path / "registry"
    registry = production_registry(root, versions=("1.0.0", "1.1.0"))
    artifact_file(root, "1.0.0").write_bytes(b'{"model": "tampered"}')

    report = registry.verify(CONTRACTS)
    assert report.health is RegistryHealth.DEGRADED
    assert report.productions[0].active.usable
    target = report.productions[0].rollback_target
    assert target is not None
    assert target.load_status is ArtifactLoadStatus.CHECKSUM_MISMATCH


def test_rollback_target_is_not_judged_by_runtime_compatibility(tmp_path: Path) -> None:
    """戻り先の互換性は rollback 実行時に見る。schema 更新で健全な戻り先を失わない（0037 §2）。"""
    root = tmp_path / "registry"
    registry = make_registry(root)
    register_and_validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    register_and_validate(registry, "1.1.0")
    promote(registry, "1.1.0")

    newer = {
        ArtifactKind.THERMAL_MODEL: ModelCompatibility(
            feature_schema_version=FEATURE_SCHEMA,
            target_schema_version=TARGET_SCHEMA,
            authority_stage=AuthorityStage.LIMITED,
        )
    }
    report = registry.verify(newer)
    target = report.productions[0].rollback_target
    assert target is not None
    assert target.usable
    assert target.compatibility_checked is False
    assert report.productions[0].active.compatibility_checked is True
    assert report.health is RegistryHealth.OK


def test_startup_verification_without_a_contract_says_so(tmp_path: Path) -> None:
    """**contract を渡さなかった検証を「互換性も確かめた」と読ませない。**"""
    root = tmp_path / "registry"
    report = production_registry(root).verify()
    assert report.productions[0].active.compatibility_checked is False


# --------------------------------------------------------------------------------------
# 報告そのものを偽れないこと
# --------------------------------------------------------------------------------------


def test_a_report_cannot_claim_ok_while_a_production_check_failed() -> None:
    """総合判定を個別の結果から切り離せない。"""
    failing = ArtifactHealth(
        artifact=metadata("1.0.0").ref,
        status=ArtifactStatus.PRODUCTION,
        load_status=ArtifactLoadStatus.CHECKSUM_MISMATCH,
        compatibility_checked=True,
        detail="checksum mismatch",
    )
    entry = ProductionHealth(kind=ArtifactKind.THERMAL_MODEL, active=failing)
    with pytest.raises(ValidationError):
        RegistryHealthReport(
            health=RegistryHealth.OK,
            revision=3,
            productions=(entry,),
            detail="claimed healthy",
        )


def test_a_report_cannot_hide_the_pointers_it_claims_to_have_checked() -> None:
    """検証していないのに `OK` と言えない。"""
    with pytest.raises(ValidationError):
        RegistryHealthReport(health=RegistryHealth.OK, revision=1, detail="nothing checked")


def test_production_health_cannot_point_at_a_non_production_artifact() -> None:
    """registry が candidate と記録した artifact を production の欄へ置けない。"""
    candidate = ArtifactHealth(
        artifact=metadata("1.0.0").ref,
        status=ArtifactStatus.CANDIDATE,
        load_status=ArtifactLoadStatus.LOADED,
        compatibility_checked=True,
        detail="loaded",
    )
    with pytest.raises(ValidationError):
        ProductionHealth(kind=ArtifactKind.THERMAL_MODEL, active=candidate)


def test_compatibility_checked_has_no_default(tmp_path: Path) -> None:
    """**欄を書かない記録を「確かめた」側へ倒さない。** 既定値を持たせない。"""
    with pytest.raises(ValidationError):
        ArtifactHealth(
            artifact=metadata("1.0.0").ref,
            status=ArtifactStatus.PRODUCTION,
            load_status=ArtifactLoadStatus.LOADED,
            detail="loaded",
        )


def test_an_older_registry_schema_version_fails_closed(tmp_path: Path) -> None:
    """schema version の古い snapshot を、欠けた欄を補って読まない。"""
    root = tmp_path / "registry"
    production_registry(root)
    snapshot_path = root / "registry.json"
    stored = json.loads(snapshot_path.read_text(encoding="utf-8"))
    stored["schema_version"] = 1
    write_json(snapshot_path, stored)

    registry = make_registry(root)
    assert registry.verify(CONTRACTS).health is RegistryHealth.INVALID
    assert (
        registry.load_production(ArtifactKind.THERMAL_MODEL, COMPATIBILITY).status
        is ArtifactLoadStatus.INVALID_REGISTRY
    )


def test_a_record_without_the_capability_field_is_not_defaulted(tmp_path: Path) -> None:
    """能力を申告していない古い記録を「反実仮想もできる」側へ倒さない（決定記録 0052 §2.1）。"""
    root = tmp_path / "registry"
    production_registry(root)
    snapshot_path = root / "registry.json"
    stored = json.loads(snapshot_path.read_text(encoding="utf-8"))
    for record in stored["artifacts"].values():
        record["metadata"].pop("capability")
    write_json(snapshot_path, stored)

    assert make_registry(root).verify(CONTRACTS).health is RegistryHealth.INVALID


# --------------------------------------------------------------------------------------
# 監査: promotion / rollback を理由と時刻ごと追える
# --------------------------------------------------------------------------------------


def test_pointer_changes_carry_the_human_reason_and_the_time(tmp_path: Path) -> None:
    """**pointer を動かした判断だけ**を、承認者・理由・時刻とともに取り出せる。"""
    root = tmp_path / "registry"
    registry = production_registry(root, versions=("1.0.0", "1.1.0"))
    revision = registry.inspect().revision
    registry.rollback(
        ArtifactKind.THERMAL_MODEL,
        COMPATIBILITY,
        approval=approval_record(
            "1.0.0",
            revision,
            action=ApprovalAction.ROLLBACK,
            reason="regression observed in shadow",
        ),
        expected_revision=revision,
    )

    changes = registry.inspect().pointer_changes
    assert [event.event for event in changes] == [
        RegistryEventKind.PROMOTED,
        RegistryEventKind.PROMOTED,
        RegistryEventKind.ROLLED_BACK,
    ]
    traced = changes[-1].trace_metadata()
    assert traced["event"] == "rolled_back"
    assert traced["model_version"] == "1.0.0"
    assert traced["previous_artifact"] == "thermal_model/rack-thermal/1.1.0"
    assert traced["approver"] == ACTOR
    assert traced["reason"] == "regression observed in shadow"
    assert traced["occurred_at_ms"] == NOW_MS
    assert traced["approval_artifact_sha256"] == metadata("1.0.0").sha256


def test_audit_trace_metadata_has_stable_keys_and_no_filesystem_path(tmp_path: Path) -> None:
    """承認の無い event でも欄を消さない。path は decision trace へ出さない。"""
    root = tmp_path / "registry"
    registry = production_registry(root)
    expected_keys = {
        "registry_revision",
        "occurred_at_ms",
        "event",
        "artifact_kind",
        "model_id",
        "model_version",
        "actor",
        "reason",
        "previous_artifact",
        "rollback_target",
        "approver",
        "approved_at_ms",
        "approval_artifact_sha256",
    }
    for event in registry.inspect().audit:
        traced = event.trace_metadata()
        assert set(traced) == expected_keys
        assert str(root) not in json.dumps(traced, ensure_ascii=False)
    registered = registry.inspect().audit[0].trace_metadata()
    assert registered["approver"] is None
    assert registered["approval_artifact_sha256"] is None


# --------------------------------------------------------------------------------------
# 運用 CLI
# --------------------------------------------------------------------------------------


def test_cli_status_is_read_only_and_does_not_create_the_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """問い合わせが registry を作らない。"""
    missing = tmp_path / "absent"
    assert cli.main(["status", *base_args(missing)]) == cli.EXIT_OK
    assert stdout_json(capsys) == {
        "schema_version": 2,
        "revision": 0,
        "production": {},
        "artifacts": [],
    }
    assert not missing.exists()


def test_cli_register_does_not_make_the_artifact_production(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """登録は候補までで、production pointer を動かさない。"""
    root = tmp_path / "registry"
    record = metadata("1.0.0")
    metadata_path = write_json(tmp_path / "metadata.json", json.loads(record.model_dump_json()))
    payload_path = tmp_path / "artifact.json"
    payload_path.write_bytes(payload("1.0.0"))

    code = cli.main(
        [
            "register",
            *base_args(root),
            "--metadata",
            str(metadata_path),
            "--payload",
            str(payload_path),
            "--actor",
            "trainer",
            "--reason",
            "training completed",
        ]
    )
    assert code == cli.EXIT_OK
    assert stdout_json(capsys)["status"] == "candidate"
    assert make_registry(root).inspect().production == {}
    assert cli.main(["verify", *base_args(root)]) == cli.EXIT_FALLBACK


def test_cli_has_no_way_to_synthesize_a_human_approval(tmp_path: Path) -> None:
    """**承認を引数から組み立てられない。** promote / rollback は承認ファイルだけを受け取る。"""
    parser = cli.build_parser()
    for command, extra in (
        ("promote", ["--shadow-evaluation-ref", "evaluation/shadow/1.0.0"]),
        ("rollback", ["--kind", ArtifactKind.THERMAL_MODEL.value]),
    ):
        with pytest.raises(SystemExit) as raised:
            parser.parse_args(
                [
                    command,
                    *base_args(tmp_path),
                    *extra,
                    *contract_args(),
                    "--expected-revision",
                    "1",
                    "--approver",
                    ACTOR,
                ]
            )
        assert raised.value.code == 2


def test_cli_rejects_an_approval_bound_to_another_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """承認は対象 artifact へ束縛されている。別の artifact の承認で昇格できない。"""
    root = tmp_path / "registry"
    registry = make_registry(root)
    register_and_validate(registry, "1.0.0")
    register_and_validate(registry, "1.1.0")
    revision = registry.inspect().revision
    # 1.1.0 を昇格させようとして、1.0.0 に対する承認を渡す。
    stolen = approval_record("1.0.0", revision).model_copy(
        update={"artifact": metadata("1.1.0").ref}
    )
    path = approval_file(tmp_path / "approval.json", stolen)

    code = cli.main(
        [
            "promote",
            *base_args(root),
            "--approval",
            str(path),
            "--shadow-evaluation-ref",
            "evaluation/shadow/1.1.0",
            *contract_args(),
            "--expected-revision",
            str(revision),
        ]
    )
    capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert registry.inspect().production == {}
    assert registry.inspect().revision == revision


def test_cli_promotion_refuses_a_stale_expected_revision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**後勝ちで上書きしない。** 古い revision を前提にした昇格は実行されない。"""
    root = tmp_path / "registry"
    registry = make_registry(root)
    register_and_validate(registry, "1.0.0")
    stale = registry.inspect().revision
    register_and_validate(registry, "1.1.0")
    current = registry.inspect().revision
    path = approval_file(tmp_path / "approval.json", approval_record("1.0.0", stale))

    code = cli.main(
        [
            "promote",
            *base_args(root),
            "--approval",
            str(path),
            "--shadow-evaluation-ref",
            "evaluation/shadow/1.0.0",
            *contract_args(),
            "--expected-revision",
            str(stale),
        ]
    )
    capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert registry.inspect().revision == current
    assert registry.inspect().production == {}


def test_cli_cannot_promote_an_artifact_that_was_never_validated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """offline evaluation を経ていない候補は、承認があっても production にできない。"""
    root = tmp_path / "registry"
    registry = make_registry(root)
    registry.register_candidate(
        metadata("1.0.0"), payload("1.0.0"), actor="trainer", reason="trained"
    )
    revision = registry.inspect().revision
    path = approval_file(tmp_path / "approval.json", approval_record("1.0.0", revision))

    code = cli.main(
        [
            "promote",
            *base_args(root),
            "--approval",
            str(path),
            "--shadow-evaluation-ref",
            "evaluation/shadow/1.0.0",
            *contract_args(),
            "--expected-revision",
            str(revision),
        ]
    )
    capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert registry.inspect().artifacts["thermal_model/rack-thermal/1.0.0"].status is (
        ArtifactStatus.CANDIDATE
    )


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        (
            "validate",
            [
                "--artifact",
                "thermal_model/rack-thermal/1.0.0",
                "--offline-evaluation-ref",
                "evaluation/offline/1.0.0",
                "--actor",
                ACTOR,
                "--reason",
                "offline gates passed",
            ],
        ),
        (
            "retire",
            [
                "--artifact",
                "thermal_model/rack-thermal/1.0.0",
                "--actor",
                ACTOR,
                "--reason",
                "superseded",
            ],
        ),
        (
            "promote",
            [
                "--approval",
                "approval.json",
                "--shadow-evaluation-ref",
                "evaluation/shadow/1.0.0",
                *contract_args(),
            ],
        ),
        ("rollback", ["--kind", "thermal_model", "--approval", "approval.json", *contract_args()]),
    ],
)
def test_cli_mutations_require_an_explicit_expected_revision(
    tmp_path: Path, command: str, extra: list[str]
) -> None:
    """**いまの revision を読まずに書けない。** 既定値を持たせない。"""
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args([command, *base_args(tmp_path), *extra])
    assert raised.value.code == 2


def test_cli_cannot_retire_the_production_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """運転中の production を retire で外せない（戻すのは rollback か新しい promotion）。"""
    root = tmp_path / "registry"
    registry = production_registry(root)
    revision = registry.inspect().revision

    code = cli.main(
        [
            "retire",
            *base_args(root),
            "--artifact",
            "thermal_model/rack-thermal/1.0.0",
            "--actor",
            ACTOR,
            "--reason",
            "no longer needed",
            "--expected-revision",
            str(revision),
        ]
    )
    capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert registry.inspect().production[ArtifactKind.THERMAL_MODEL].active.version == "1.0.0"


def test_cli_rollback_restores_the_known_good_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """承認された rollback が pointer を戻し、理由と時刻を監査へ残す。"""
    root = tmp_path / "registry"
    registry = production_registry(root, versions=("1.0.0", "1.1.0"))
    revision = registry.inspect().revision
    path = approval_file(
        tmp_path / "rollback.json",
        approval_record(
            "1.0.0", revision, action=ApprovalAction.ROLLBACK, reason="regression observed"
        ),
    )

    code = cli.main(
        [
            "rollback",
            *base_args(root),
            "--kind",
            "thermal_model",
            "--approval",
            str(path),
            *contract_args(),
            "--expected-revision",
            str(revision),
        ]
    )
    assert code == cli.EXIT_OK
    assert stdout_json(capsys)["artifact"] == "thermal_model/rack-thermal/1.0.0"
    snapshot = registry.inspect()
    assert snapshot.production[ArtifactKind.THERMAL_MODEL].active.version == "1.0.0"
    assert snapshot.pointer_changes[-1].trace_metadata()["reason"] == "regression observed"


def test_cli_verify_exit_codes_tell_the_operator_what_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """OK / 戻り先なし / 使えない を、終了コードで区別できる。"""
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        "schema_version: 1\n"
        "contracts:\n"
        "  thermal_model:\n"
        f"    feature_schema_version: {FEATURE_SCHEMA}\n"
        f"    target_schema_version: {TARGET_SCHEMA}\n"
        "    authority_stage: shadow\n",
        encoding="utf-8",
    )
    verify = ["verify", "--contract", str(contract)]

    empty = tmp_path / "empty"
    assert cli.main([*verify, *base_args(empty)]) == cli.EXIT_FALLBACK
    assert stdout_json(capsys)["model_registry_health"]["health"] == "no_production"

    degraded = tmp_path / "degraded"
    production_registry(degraded)
    assert cli.main([*verify, *base_args(degraded)]) == cli.EXIT_DEGRADED
    assert stdout_json(capsys)["model_registry_health"]["health"] == "degraded"

    healthy = tmp_path / "healthy"
    production_registry(healthy, versions=("1.0.0", "1.1.0"))
    assert cli.main([*verify, *base_args(healthy)]) == cli.EXIT_OK
    assert stdout_json(capsys)["model_registry_health"]["health"] == "ok"

    artifact_file(healthy, "1.1.0").write_bytes(b'{"model": "tampered"}')
    assert cli.main([*verify, *base_args(healthy)]) == cli.EXIT_FALLBACK
    assert stdout_json(capsys)["model_registry_health"]["health"] == "unusable"


def test_cli_verify_rejects_an_unknown_artifact_kind_in_the_contract(tmp_path: Path) -> None:
    """検査したつもりの kind が黙って落ちないようにする。"""
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        "schema_version: 1\n"
        "contracts:\n"
        "  thermal_modell:\n"
        f"    feature_schema_version: {FEATURE_SCHEMA}\n"
        f"    target_schema_version: {TARGET_SCHEMA}\n"
        "    authority_stage: shadow\n",
        encoding="utf-8",
    )
    root = tmp_path / "registry"
    production_registry(root)
    assert cli.main(["verify", "--contract", str(contract), *base_args(root)]) == cli.EXIT_FAILED


def test_cli_audit_can_be_narrowed_to_pointer_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """登録・検証の記録と、production を動かした判断を分けて読める。"""
    root = tmp_path / "registry"
    production_registry(root)

    assert cli.main(["audit", *base_args(root), "--pointer-changes"]) == cli.EXIT_OK
    narrowed = stdout_json(capsys)
    assert [event["event"] for event in narrowed] == ["promoted"]

    assert cli.main(["audit", *base_args(root)]) == cli.EXIT_OK
    assert [event["event"] for event in stdout_json(capsys)] == [
        "registered",
        "validated",
        "promoted",
    ]


# --------------------------------------------------------------------------------------
# Codex review (PR #162): contract の取りこぼし・壊れた YAML・上限より先の確保
# --------------------------------------------------------------------------------------


def tiny_limits(directory: Path, *, max_artifact_bytes: int) -> Path:
    """artifact の上限だけを小さくした設定ディレクトリを作る。"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model-registry.yaml").write_text(
        "schema_version: 1\n"
        f"max_artifact_bytes: {max_artifact_bytes}\n"
        "max_snapshot_bytes: 4194304\n"
        "max_json_nesting_depth: 32\n"
        "max_json_tokens: 250000\n"
        "max_snapshot_json_nesting_depth: 16\n"
        "max_snapshot_json_tokens: 250000\n",
        encoding="utf-8",
    )
    return directory


def contract_file(path: Path, kinds: tuple[str, ...]) -> Path:
    body = ["schema_version: 1", "contracts:"]
    if not kinds:
        body = ["schema_version: 1", "contracts: {}"]
    for kind in kinds:
        body += [
            f"  {kind}:",
            f"    feature_schema_version: {FEATURE_SCHEMA}",
            f"    target_schema_version: {TARGET_SCHEMA}",
            "    authority_stage: shadow",
        ]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def test_the_report_names_the_kinds_that_were_checked_without_a_contract(tmp_path: Path) -> None:
    """**contract を当てなかった kind を `ok` の陰に隠さない。**"""
    root = tmp_path / "registry"
    registry = production_registry(root)
    assert registry.verify().unchecked_kinds() == (ArtifactKind.THERMAL_MODEL,)
    assert registry.verify(CONTRACTS).unchecked_kinds() == ()
    traced: Any = registry.verify().trace_metadata()["model_registry_health"]
    assert traced["unchecked_kinds"] == ["thermal_model"]


@pytest.mark.parametrize("covered", [(), ("supervisor_policy",)])
def test_cli_verify_fails_closed_when_a_contract_misses_a_production_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], covered: tuple[str, ...]
) -> None:
    """contract を渡したのに覆っていない production kind があれば、判定を出さずに失敗する。

    checksum と format だけで `loaded` になり、schema や authority が合っていなくても
    総合判定が `ok` になってしまうため。
    """
    root = tmp_path / "registry"
    production_registry(root, versions=("1.0.0", "1.1.0"))
    contract = contract_file(tmp_path / "contract.yaml", covered)

    code = cli.main(["verify", "--contract", str(contract), *base_args(root)])
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert captured.out == ""
    assert "thermal_model" in captured.err


def test_cli_verify_accepts_a_contract_that_covers_every_production_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """覆っていれば通る。取りこぼしの検査が正常系を塞がないことを確かめる。"""
    root = tmp_path / "registry"
    production_registry(root, versions=("1.0.0", "1.1.0"))
    contract = contract_file(tmp_path / "contract.yaml", ("thermal_model",))

    assert cli.main(["verify", "--contract", str(contract), *base_args(root)]) == cli.EXIT_OK
    assert stdout_json(capsys)["model_registry_health"]["unchecked_kinds"] == []


@pytest.mark.parametrize("target", ["contract", "limits"])
def test_cli_reports_a_malformed_yaml_as_a_failed_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    """壊れた YAML で traceback を出さない。**記録された失敗**として終了コードで返す。"""
    root = tmp_path / "registry"
    production_registry(root)
    broken = "schema_version: 1\ncontracts: [unbalanced\n"

    if target == "contract":
        path = tmp_path / "contract.yaml"
        path.write_text(broken, encoding="utf-8")
        argv = ["verify", "--contract", str(path), *base_args(root)]
    else:
        limits = tmp_path / "limits"
        limits.mkdir()
        (limits / "model-registry.yaml").write_text(broken, encoding="utf-8")
        argv = ["verify", "--root", str(root), "--limits", str(limits)]

    assert cli.main(argv) == cli.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Model Registry の操作に失敗した" in captured.err


def test_bounded_read_never_asks_for_more_than_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**読んでから大きさを判断しない。** 上限＋1 byte しか要求しない。"""
    path = tmp_path / "oversized.json"
    path.write_bytes(b"x" * 5000)
    requested: list[int] = []
    real_open = Path.open

    class _Spy:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def read(self, size: int = -1) -> bytes:
            requested.append(size)
            result: bytes = self._handle.read(size)
            return result

        def __enter__(self) -> _Spy:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        return _Spy(real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", spy_open)
    with pytest.raises(ArtifactVerificationError):
        cli.read_bounded(path, 64)
    assert requested == [65]


def test_cli_register_refuses_an_oversized_payload_before_reading_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """上限を超える artifact で管理 process を落とさない。registry も作らない。"""
    root = tmp_path / "registry"
    record = metadata("1.0.0")
    metadata_path = write_json(tmp_path / "metadata.json", json.loads(record.model_dump_json()))
    payload_path = tmp_path / "artifact.json"
    payload_path.write_bytes(b"x" * 4096)
    limits = tiny_limits(tmp_path / "limits", max_artifact_bytes=64)

    code = cli.main(
        [
            "register",
            "--root",
            str(root),
            "--limits",
            str(limits),
            "--metadata",
            str(metadata_path),
            "--payload",
            str(payload_path),
            "--actor",
            "trainer",
            "--reason",
            "training completed",
        ]
    )
    capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert not root.exists()


def test_cli_status_reports_a_corrupt_registry_as_a_failed_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """読み取りの問い合わせも、壊れた snapshot で traceback を出さない。"""
    root = tmp_path / "registry"
    production_registry(root)
    (root / "registry.json").write_bytes(b"{ broken")

    for command in ("status", "audit"):
        assert cli.main([command, *base_args(root)]) == cli.EXIT_FAILED
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Model Registry の操作に失敗した" in captured.err


def deep_flow_yaml(depth: int) -> str:
    """byte 数は小さいまま、入れ子だけを深くした YAML。"""
    return "schema_version: 1\ncontracts: " + "[" * depth + "]" * depth + "\n"


@pytest.mark.parametrize("target", ["contract", "limits"])
def test_cli_reports_deeply_nested_yaml_as_a_failed_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    """**小さくても深い YAML で traceback を出さない。**

    byte 上限に収まっていても PyYAML の再帰を尽くす。`RecursionError` は `ValueError`
    でも `yaml.YAMLError` でもないため、寄せておかないと終了コード 1 と構造化ログという
    約束が破れる（決定記録 0062 §2.1）。
    """
    root = tmp_path / "registry"
    production_registry(root)
    body = deep_flow_yaml(60_000)
    assert len(body.encode()) < LIMITS.max_snapshot_bytes

    if target == "contract":
        path = tmp_path / "contract.yaml"
        path.write_text(body, encoding="utf-8")
        argv = ["verify", "--contract", str(path), *base_args(root)]
    else:
        limits = tmp_path / "limits"
        limits.mkdir()
        (limits / "model-registry.yaml").write_text(body, encoding="utf-8")
        argv = ["verify", "--root", str(root), "--limits", str(limits)]

    assert cli.main(argv) == cli.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "入れ子が深すぎる" in captured.err


def test_cli_reports_a_recursive_yaml_alias_as_a_failed_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """自分を指す alias が作る循環構造も、設定の誤りとして返す。"""
    root = tmp_path / "registry"
    production_registry(root)
    contract = tmp_path / "contract.yaml"
    contract.write_text("schema_version: 1\ncontracts: &loop\n  thermal_model: *loop\n", "utf-8")

    assert cli.main(["verify", "--contract", str(contract), *base_args(root)]) == cli.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Model Registry の操作に失敗した" in captured.err


@pytest.mark.parametrize("argument", ["metadata", "payload"])
def test_cli_reports_undecodable_input_as_a_failed_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argument: str
) -> None:
    """UTF-8 として読めない入力も traceback にしない（`register` の2つの入口）。"""
    root = tmp_path / "registry"
    record = metadata("1.0.0")
    metadata_path = write_json(tmp_path / "metadata.json", json.loads(record.model_dump_json()))
    payload_path = tmp_path / "artifact.json"
    payload_path.write_bytes(payload("1.0.0"))
    {"metadata": metadata_path, "payload": payload_path}[argument].write_bytes(b"\xff\xfe\x00")

    code = cli.main(
        [
            "register",
            *base_args(root),
            "--metadata",
            str(metadata_path),
            "--payload",
            str(payload_path),
            "--actor",
            "trainer",
            "--reason",
            "training completed",
        ]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert captured.out == ""
    assert "Model Registry の操作に失敗した" in captured.err


@pytest.mark.parametrize("argument", ["metadata", "approval"])
def test_cli_reports_deeply_nested_json_as_a_failed_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argument: str
) -> None:
    """深く入れ子にした JSON（metadata / 承認）も、記録される失敗として返す。"""
    root = tmp_path / "registry"
    registry = make_registry(root)
    register_and_validate(registry, "1.0.0")
    revision = registry.inspect().revision
    deep = ("[" * 100_000 + "]" * 100_000).encode()

    if argument == "metadata":
        metadata_path = tmp_path / "metadata.json"
        metadata_path.write_bytes(deep)
        payload_path = tmp_path / "artifact.json"
        payload_path.write_bytes(payload("1.1.0"))
        argv = [
            "register",
            *base_args(root),
            "--metadata",
            str(metadata_path),
            "--payload",
            str(payload_path),
            "--actor",
            "trainer",
            "--reason",
            "training completed",
        ]
    else:
        approval_path = tmp_path / "approval.json"
        approval_path.write_bytes(deep)
        argv = [
            "promote",
            *base_args(root),
            "--approval",
            str(approval_path),
            "--shadow-evaluation-ref",
            "evaluation/shadow/1.0.0",
            *contract_args(),
            "--expected-revision",
            str(revision),
        ]

    assert cli.main(argv) == cli.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Model Registry の操作に失敗した" in captured.err
    assert registry.inspect().revision == revision
