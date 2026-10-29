"""``coldaisle-registry validate --thermal-evaluation`` と ``mark_validated()`` の形の検査。

#84 / 決定記録 0172 §2.7 / §2.9 / §5 の 9〜11。実機は要らない。

``coldaisle-train --apply`` で登録した candidate を ``coldaisle-thermal-evaluate`` で評価し、
その報告で validated にする。各試験は Registry の写しに対して行い、互いの状態に依らない。

- 成功: ``VALIDATED`` の event が 1 つだけ足され、ref は ``thermal-offline-v1:<報告の sha256>``。
  報告の bytes は artifact の隣（``offline-evaluation.json``）へ写る。他は何も変わらない
- 拒否（別の artifact の報告・sha256 の不一致・``blocked``・``outcome`` を手で ``pass`` に
  直した報告・設定の digest の不一致・canonical でない bytes・反実仮想 thermal model への
  ``--offline-evaluation-ref`` など）: Registry の bytes とファイルが変わらない
- ``mark_validated()`` を直接呼んで形の違う ref を渡すと拒否（その kind だけ）。他の kind の
  自由記述は今どおり通る
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from coldaisle import registry as registry_cli
from coldaisle import thermal_evaluate, train
from coldaisle.control.model_registry import (
    OFFLINE_EVALUATION_FILENAME,
    ArtifactCapability,
    ArtifactKind,
    ArtifactStatus,
    ArtifactVerificationError,
    ModelRegistry,
    RegistryEventKind,
    RegistrySnapshot,
)
from coldaisle.store import QualityRules
from coldaisle.thermal_evaluation_report import (
    ThermalEvaluationReport,
    canonical_json_bytes,
    conditions_sha256,
    offline_evaluation_ref,
    parse_report_bytes,
)
from test_model_registry import metadata as plain_metadata
from test_model_registry import payload as plain_payload
from test_train_cli import log_lines
from thermal_evaluation_world import EVALUATION_CONFIG, evaluate_argv, make_world
from train_world import World, limits

KEY = "thermal_model/rack-thermal/0.1.0"
OTHER_KEY = "thermal_model/rack-thermal/0.2.0"


@dataclass(frozen=True)
class Reports:
    """試験に使う報告と設定（Registry は module の間、candidate 2 つのまま変えない）。"""

    world: World
    passed: Path
    """0.1.0 の ``pass`` の報告。"""
    other: Path
    """0.2.0 の ``pass`` の報告（別の artifact の報告）。"""
    blocked: Path
    """0.1.0 の ``blocked``（``evidence``）の報告。"""
    config: Path
    blocked_config: Path


def _strict_config() -> str:
    """test の件数の下限を届かない値にした設定（``evidence`` で ``blocked``）。"""
    strict = EVALUATION_CONFIG.replace(
        "test_examples: {value: 10, status: provisional}",
        "test_examples: {value: 1000000, status: provisional}",
    )
    assert strict != EVALUATION_CONFIG
    return strict


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory, rules: QualityRules) -> Reports:
    world = make_world(tmp_path_factory.mktemp("thermal-registry") / "w", rules)
    assert train.main(world.apply_argv(world.outputs / "train-0.1.0")) == 0
    assert (
        train.main(world.apply_argv(world.outputs / "train-0.2.0", **{"--model-version": "0.2.0"}))
        == 0
    )
    config = world.root / "thermal-evaluation.yaml"
    blocked_config = world.root / "thermal-evaluation-strict.yaml"
    blocked_config.write_text(_strict_config(), encoding="utf-8")
    passed = world.outputs / "pass-0.1.0.json"
    other = world.outputs / "pass-0.2.0.json"
    blocked = world.outputs / "blocked-0.1.0.json"
    assert thermal_evaluate.main(evaluate_argv(world, passed)) == 0
    assert (
        thermal_evaluate.main(evaluate_argv(world, other, **{"--artifact": "rack-thermal@0.2.0"}))
        == 0
    )
    assert (
        thermal_evaluate.main(
            evaluate_argv(world, blocked, **{"--evaluation-config": str(blocked_config)})
        )
        == 0
    )
    assert parse_report_bytes(passed.read_bytes()).gate.outcome == "pass"
    assert parse_report_bytes(other.read_bytes()).gate.outcome == "pass"
    assert parse_report_bytes(blocked.read_bytes()).gate.failed_stage == "evidence"
    return Reports(world, passed, other, blocked, config, blocked_config)


@pytest.fixture
def root(reports: Reports, tmp_path: Path) -> Path:
    """module の Registry の写し（candidate 2 つ）。"""
    copied = tmp_path / "registry"
    shutil.copytree(reports.world.registry, copied, symlinks=True)
    return copied


def registry_of(root: Path) -> ModelRegistry:
    return ModelRegistry(root, limits=limits())


def state(root: Path) -> tuple[bytes, dict[str, bytes]]:
    """``registry.json`` の bytes と ``artifacts/`` の下の全ファイルの bytes。"""
    files = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "artifacts").rglob("*"))
        if path.is_file()
    }
    return (root / "registry.json").read_bytes(), files


def validate_argv(
    reports: Reports, root: Path, *, report: Path | None, **overrides: str | None
) -> list[str]:
    values: dict[str, str | None] = {
        "--root": str(root),
        "--limits": str(reports.world.limits),
        "--artifact": KEY,
        "--thermal-evaluation": None if report is None else str(report),
        "--evaluation-config": str(reports.config),
        "--actor": "evaluator",
        "--reason": "offline gates passed",
        "--expected-revision": str(registry_of(root).inspect().revision),
    }
    values.update(overrides)
    argv = ["validate"]
    for key, value in values.items():
        if value is not None:
            argv += [key, value]
    return argv


def registry_errors(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [
        line["error"]
        for line in log_lines(capsys.readouterr().err)
        if line["logger"] == "coldaisle.registry" and line["level"] == "error"
    ]


def rewritten(source: Path, target: Path, change: Callable[[dict[str, Any]], None]) -> Path:
    """報告を書き換え、``conditions_sha256`` を合わせて canonical に書き直す（取り違えの模擬）。"""
    document = json.loads(source.read_bytes())
    change(document)
    report = ThermalEvaluationReport.model_validate_json(json.dumps(document))
    document["conditions_sha256"] = conditions_sha256(report)
    target.write_bytes(canonical_json_bytes(document))
    return target


# ---------------------------------------------------------------- 成功


def test_a_passing_report_validates_the_candidate_once(
    reports: Reports, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = registry_of(root).inspect()
    before_state = state(root)
    payload = reports.passed.read_bytes()
    capsys.readouterr()
    assert registry_cli.main(validate_argv(reports, root, report=reports.passed)) == 0
    after = registry_of(root).inspect()
    ref = offline_evaluation_ref(payload)
    assert_one_validation_added(before, after, ref)
    # 報告の bytes は artifact の隣へ写る（0172 §5 の 10）。他のファイルは変わらない
    copied = Path("artifacts/thermal_model/rack-thermal/0.1.0") / OFFLINE_EVALUATION_FILENAME
    _snapshot_bytes, files = state(root)
    assert files.pop(str(copied)) == payload
    assert files == before_state[1]
    assert ref == "thermal-offline-v1:" + hashlib.sha256((root / copied).read_bytes()).hexdigest()
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == {
        "artifact": KEY,
        "status": "validated",
        "revision": after.revision,
        "offline_evaluation_ref": ref,
    }


def assert_one_validation_added(
    before: RegistrySnapshot, after: RegistrySnapshot, ref: str
) -> None:
    """0150 §2.9 と同じ定義: 足されたもの以外が1つでも変われば失敗にする。"""
    assert after.revision == before.revision + 1
    assert after.audit[:-1] == before.audit
    [event] = after.audit[len(before.audit) :]
    assert event.event is RegistryEventKind.VALIDATED
    assert event.artifact.key == KEY
    assert event.actor == "evaluator"
    assert set(after.artifacts) == set(before.artifacts)
    for key, record in after.artifacts.items():
        if key != KEY:
            assert record == before.artifacts[key]
    record = after.artifacts[KEY]
    assert record.status is ArtifactStatus.VALIDATED
    assert record.metadata.offline_evaluation_ref == ref
    assert record.metadata.model_copy(update={"offline_evaluation_ref": None}) == (
        before.artifacts[KEY].metadata
    )
    assert after.production == before.production


# ---------------------------------------------------------------- 拒否（Registry が変わらない）


def _refused(
    reports: Reports,
    root: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    report: Path | None,
    **overrides: str | None,
) -> str:
    before = state(root)
    argv = validate_argv(reports, root, report=report, **overrides)
    capsys.readouterr()
    assert registry_cli.main(argv) == 1
    assert state(root) == before
    [error] = registry_errors(capsys)
    return error


def test_a_report_of_another_artifact_is_refused(
    reports: Reports, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _refused(reports, root, capsys, report=reports.other)
    assert "--artifact と違う" in error


def test_a_report_whose_sha256_differs_from_the_registry_is_refused(
    reports: Reports, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def change(document: dict[str, Any]) -> None:
        document["artifact"]["sha256"] = "0" * 64

    forged = rewritten(reports.passed, tmp_path / "sha.json", change)
    assert "checksum と一致しない" in _refused(reports, root, capsys, report=forged)


def test_a_report_made_when_the_artifact_was_not_a_candidate_is_refused(
    reports: Reports, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def change(document: dict[str, Any]) -> None:
        document["artifact"]["registry_status"] = "validated"

    forged = rewritten(reports.passed, tmp_path / "status.json", change)
    assert "candidate のときに作った報告ではない" in _refused(reports, root, capsys, report=forged)


def test_a_blocked_report_is_refused(
    reports: Reports, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _refused(
        reports,
        root,
        capsys,
        report=reports.blocked,
        **{"--evaluation-config": str(reports.blocked_config)},
    )
    assert "gate が pass ではない" in error and "evidence" in error


def test_a_blocked_report_hand_edited_to_pass_is_refused(
    reports: Reports, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # gate は conditions_sha256 に入らないので、書き換えても parse は通る。作り直した gate と
    # 照合して止める
    def change(document: dict[str, Any]) -> None:
        document["gate"] = {"outcome": "pass", "failed_stage": None, "reasons": []}

    forged = rewritten(reports.blocked, tmp_path / "forged.json", change)
    error = _refused(
        reports,
        root,
        capsys,
        report=forged,
        **{"--evaluation-config": str(reports.blocked_config)},
    )
    assert "作り直した gate と一致しない" in error


def test_a_config_whose_digest_differs_is_refused(
    reports: Reports, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changed = tmp_path / "changed.yaml"
    changed.write_text(EVALUATION_CONFIG + "# 値は同じでも bytes が違う\n", encoding="utf-8")
    error = _refused(
        reports, root, capsys, report=reports.passed, **{"--evaluation-config": str(changed)}
    )
    assert "config_sha256 と一致しない" in error


def test_another_config_that_passes_its_own_check_is_still_refused(
    reports: Reports, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # pass の報告に、別の（正しい形の）設定を当てても digest で止まる
    error = _refused(
        reports,
        root,
        capsys,
        report=reports.passed,
        **{"--evaluation-config": str(reports.blocked_config)},
    )
    assert "config_sha256 と一致しない" in error


def test_non_canonical_report_bytes_are_refused(
    reports: Reports, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = json.loads(reports.passed.read_bytes())
    indented = tmp_path / "indented.json"
    indented.write_bytes(
        (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    )
    assert "canonical" in _refused(reports, root, capsys, report=indented)


def test_a_malformed_report_is_refused(
    reports: Reports, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_bytes(b'{"schema_version": 1}\n')
    assert "形が違う" in _refused(reports, root, capsys, report=broken)


def test_a_free_text_ref_for_a_counterfactual_thermal_model_is_refused(
    reports: Reports, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ref = offline_evaluation_ref(reports.passed.read_bytes())
    for value in ("evaluation/offline/0.1.0", ref):
        error = _refused(
            reports,
            root,
            capsys,
            report=None,
            **{"--offline-evaluation-ref": value, "--evaluation-config": None},
        )
        assert "--thermal-evaluation" in error


def test_the_evaluation_config_is_required_with_the_report(
    reports: Reports, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _refused(reports, root, capsys, report=reports.passed, **{"--evaluation-config": None})
    assert "--evaluation-config が要る" in error


def test_a_stale_expected_revision_is_refused_before_anything_is_written(
    reports: Reports, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = str(registry_of(root).inspect().revision - 1)
    error = _refused(reports, root, capsys, report=reports.passed, **{"--expected-revision": stale})
    assert "revision" in error


def test_the_ref_and_the_report_are_mutually_exclusive(
    reports: Reports, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = state(root)
    argv = validate_argv(
        reports, root, report=reports.passed, **{"--offline-evaluation-ref": "evaluation/x"}
    )
    with pytest.raises(SystemExit):
        registry_cli.main(argv)
    assert state(root) == before


# ---------------------------------------------------------------- mark_validated() の形の検査


def test_mark_validated_checks_the_ref_form_only_for_counterfactual_thermal_models(
    reports: Reports, root: Path
) -> None:
    registry = registry_of(root)
    snapshot = registry.inspect()
    record = snapshot.artifacts[KEY]
    assert record.metadata.kind is ArtifactKind.THERMAL_MODEL
    assert record.metadata.capability is ArtifactCapability.COUNTERFACTUAL_ACTION
    payload = reports.passed.read_bytes()
    before = state(root)
    attempts: list[tuple[str, bytes | None, type[Exception]]] = [
        ("evaluation/offline/0.1.0", payload, ValueError),
        ("thermal-offline-v1:" + "A" * 64, payload, ValueError),
        ("supervisor-episode:" + "0" * 64, payload, ValueError),
        (offline_evaluation_ref(payload), None, ValueError),
        (offline_evaluation_ref(payload), payload + b" ", ArtifactVerificationError),
    ]
    for ref, report, error in attempts:
        with pytest.raises(error):
            registry.mark_validated(
                record.ref,
                offline_evaluation_ref=ref,
                actor="evaluator",
                reason="offline gates passed",
                expected_revision=snapshot.revision,
                offline_evaluation_report=report,
            )
        assert state(root) == before


def test_other_kinds_keep_the_free_text_ref_and_take_no_report(tmp_path: Path) -> None:
    registry = registry_of(tmp_path / "plain")
    plain = plain_metadata("1.0.0")
    assert plain.capability is ArtifactCapability.OBSERVATIONAL_REPLAY
    registry.register_candidate(plain, plain_payload("1.0.0"), actor="trainer", reason="trained")
    revision = registry.inspect().revision
    with pytest.raises(ValueError, match="counterfactual_action だけ"):
        registry.mark_validated(
            plain.ref,
            offline_evaluation_ref="evaluation/offline/1.0.0",
            actor="evaluator",
            reason="offline gates passed",
            expected_revision=revision,
            offline_evaluation_report=b"{}\n",
        )
    assert registry.inspect().revision == revision
    registry.mark_validated(
        plain.ref,
        offline_evaluation_ref="evaluation/offline/1.0.0",
        actor="evaluator",
        reason="offline gates passed",
        expected_revision=revision,
    )
    record = registry.inspect().artifacts[plain.ref.key]
    assert record.status is ArtifactStatus.VALIDATED
    assert record.metadata.offline_evaluation_ref == "evaluation/offline/1.0.0"
    version_dir = tmp_path / "plain" / "artifacts" / "thermal_model" / "rack-thermal" / "1.0.0"
    assert not (version_dir / OFFLINE_EVALUATION_FILENAME).exists()


def test_a_snapshot_with_a_free_text_ref_on_a_counterfactual_model_is_still_readable(
    reports: Reports, root: Path
) -> None:
    # snapshot の読み込みの検証は変えない（既存の Registry を読めなくしない。0172 §5 の 11）
    path = root / "registry.json"
    document = json.loads(path.read_bytes())
    registry = registry_of(root)
    snapshot = registry.inspect()
    metadata = snapshot.artifacts[KEY].metadata.model_copy(
        update={"offline_evaluation_ref": "evaluation/offline/legacy"}
    )
    document["artifacts"][KEY] = {
        "metadata": metadata.model_dump(mode="json"),
        "status": "validated",
    }
    event = snapshot.audit[-1].model_dump(mode="json")
    event.update(
        revision=snapshot.revision + 1,
        event="validated",
        artifact=snapshot.artifacts[KEY].ref.model_dump(mode="json"),
    )
    document["audit"].append(event)
    document["revision"] = snapshot.revision + 1
    path.write_bytes(json.dumps(document).encode())
    legacy = registry.inspect().artifacts[KEY]
    assert legacy.status is ArtifactStatus.VALIDATED
    assert legacy.metadata.offline_evaluation_ref == "evaluation/offline/legacy"
