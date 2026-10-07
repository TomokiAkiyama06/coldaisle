"""`coldaisle-dataset` の v2 と宣言された変更のファイル（決定記録 0109）。実機なし。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import pytest

import coldaisle.dataset as dataset_module
from coldaisle.control.drift.model import MAX_DECLARED_CHANGES, ChangeKind, DeclaredChange
from coldaisle.declared_changes import DeclaredChangesError, read_declared_changes
from coldaisle.drift import DriftEvidenceManifest
from conftest import QUALITY_RULES_PATH
from test_thermal_dataset_v2 import (
    ALIGNED,
    ARTIFACT_ONE,
    COVERING_ROW_MS,
    RUN_ALIAS,
    SHA256,
    SOURCE_ALIAS,
    T0,
    history_db,
    run_store,
)

END_MS = 7_501


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """CLI の ``logs.configure`` がルートの handler を差し替えるので、試験の後に戻す。"""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 宣言のファイル（0109 §2.1〜§2.3）


def test_declared_changes_file_is_read_and_normalized(tmp_path):
    text = (
        "schema_version: 1\n"
        "changes:\n"
        "  - kind: fan_replaced\n"
        "    ts_ms: 2000\n"
        "  - kind: calibration_changed\n"
        "    ts_ms: 1000\n"
        '    detail: "再較正"\n'
        "  - kind: fan_replaced\n"
        "    ts_ms: 2000\n"
    )
    path = write(tmp_path / "declared.yaml", text)
    read = read_declared_changes(path)
    assert read.changes == (
        DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=1000, detail="再較正"),
        DeclaredChange(kind=ChangeKind.FAN_REPLACED, ts_ms=2000),
    )
    assert read.file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_empty_declaration_is_explicit(tmp_path):
    read = read_declared_changes(write(tmp_path / "d.yaml", "schema_version: 1\nchanges: []\n"))
    assert read.changes == ()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty-file"),
        pytest.param("null\n", id="null"),
        pytest.param("- kind: fan_replaced\n  ts_ms: 1\n", id="top-level-list"),
        pytest.param("schema_version: 1\n", id="changes-missing"),
        pytest.param("schema_version: 1\nchanges:\n", id="changes-null"),
        pytest.param("schema_version: 1\nchanges: {}\n", id="changes-not-a-list"),
        pytest.param("changes: []\n", id="schema-version-missing"),
        pytest.param("schema_version: 2\nchanges: []\n", id="schema-version-2"),
        pytest.param("schema_version: true\nchanges: []\n", id="schema-version-bool"),
        pytest.param("schema_version: 1\nchanges: []\nnote: x\n", id="extra-top-key"),
        pytest.param(
            "schema_version: 1\nchanges:\n  - kind: calibration_change\n    ts_ms: 1\n",
            id="unknown-kind",
        ),
        pytest.param(
            "schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: '1000'\n",
            id="ts-string",
        ),
        pytest.param(
            "schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: 2026-10-01T12:00:00\n",
            id="ts-timestamp",
        ),
        pytest.param(
            "schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: 1.5\n",
            id="ts-float",
        ),
        pytest.param(
            "schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: true\n",
            id="ts-bool",
        ),
        pytest.param(
            "schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: -1\n",
            id="ts-negative",
        ),
        pytest.param(
            "schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: 1\n    who: x\n",
            id="extra-item-key",
        ),
        pytest.param(
            "schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: 1\n"
            f"    detail: {'x' * 501}\n",
            id="detail-too-long",
        ),
        pytest.param("schema_version: 1\nschema_version: 1\nchanges: []\n", id="duplicate-top-key"),
        pytest.param(
            "schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: 1\n    ts_ms: 2\n",
            id="duplicate-item-key",
        ),
        pytest.param("schema_version: 1\nchanges: [\n", id="not-yaml"),
        pytest.param("schema_version: 1\nchanges: []\n? [a, b]\n: 1\n", id="unhashable-key"),
        pytest.param("schema_version: 1\nchanges: []\n1: x\n", id="non-string-key"),
    ],
)
def test_invalid_declaration_files_are_refused(tmp_path, text):
    with pytest.raises(DeclaredChangesError):
        read_declared_changes(write(tmp_path / "d.yaml", text))


def test_non_utf8_declaration_is_refused(tmp_path):
    path = tmp_path / "d.yaml"
    path.write_bytes(b"schema_version: 1\nchanges: []\n# \xff\n")
    with pytest.raises(DeclaredChangesError, match="UTF-8"):
        read_declared_changes(path)


def test_symlink_and_directory_are_refused(tmp_path):
    target = write(tmp_path / "real.yaml", "schema_version: 1\nchanges: []\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(target)
    with pytest.raises(DeclaredChangesError):
        read_declared_changes(link)
    with pytest.raises(DeclaredChangesError, match="regular file"):
        read_declared_changes(tmp_path)


def _changes_yaml(ts_values: list[int]) -> str:
    items = "".join(f"  - kind: fan_replaced\n    ts_ms: {ts}\n" for ts in ts_values)
    return f"schema_version: 1\nchanges:\n{items}"


def test_the_limit_applies_after_deduplication(tmp_path):
    """同じ件の繰り返しは上限に数えない（drift の検知器と同じ順。0109 §2.3 の 5 / 6）。"""
    repeated = [7] * (MAX_DECLARED_CHANGES + 1)
    read = read_declared_changes(write(tmp_path / "same.yaml", _changes_yaml(repeated)))
    assert len(read.changes) == 1
    distinct = list(range(MAX_DECLARED_CHANGES + 1))
    with pytest.raises(DeclaredChangesError, match="構造上限"):
        read_declared_changes(write(tmp_path / "many.yaml", _changes_yaml(distinct)))
    at_limit = list(range(MAX_DECLARED_CHANGES))
    read = read_declared_changes(write(tmp_path / "limit.yaml", _changes_yaml(at_limit)))
    assert len(read.changes) == MAX_DECLARED_CHANGES


def test_drift_evidence_shares_the_item_shape():
    """drift の証拠 YAML の ``changes:`` も同じ写しを使う（0109 §2.1）。"""
    manifest = DriftEvidenceManifest.model_validate(
        {
            "schema_version": 1,
            "start_ms": 0,
            "end_ms": 10,
            "changes": [{"kind": "calibration_changed", "ts_ms": 5}],
        }
    )
    assert manifest.changes == (DeclaredChange(kind=ChangeKind.CALIBRATION_CHANGED, ts_ms=5),)
    with pytest.raises(ValueError, match="calibration_change"):
        DriftEvidenceManifest.model_validate(
            {
                "schema_version": 1,
                "start_ms": 0,
                "end_ms": 10,
                "changes": [{"kind": "calibration_change", "ts_ms": 5}],
            }
        )


# ---------------------------------------------------------------- CLI（0109 §5 #6 / §7）


@pytest.fixture
def cli_env(tmp_path, rules, clock, monkeypatch):
    """専用 DB・本番の DB（記録）・宣言のファイル・出力先を用意する。"""
    db = tmp_path / "run.db"
    with run_store(db, rules, clock, ALIGNED, end_ms=END_MS):
        pass
    history = history_db(tmp_path / "history.db", COVERING_ROW_MS)
    replay = tmp_path / "replay"
    replay.mkdir()
    # 専用 DB は試験の fixture が仮の fingerprint で bind している
    monkeypatch.setattr(dataset_module, "replay_fingerprint", lambda path: SHA256)
    output = tmp_path / "out"
    output.mkdir(mode=0o700)
    output.chmod(0o700)
    declared = write(tmp_path / "declared.yaml", "schema_version: 1\nchanges: []\n")
    return {"db": db, "history": history, "replay": replay, "out": output, "declared": declared}


def common_args(env: dict[str, Path]) -> list[str]:
    return [
        "--db",
        str(env["db"]),
        "--quality-config",
        str(QUALITY_RULES_PATH),
        "--output-root",
        str(env["out"]),
        "--artifact-name",
        ARTIFACT_ONE,
        "--run-alias",
        RUN_ALIAS,
        "--source-kind",
        "replay",
        "--replay-path",
        str(env["replay"]),
        "--source-alias",
        SOURCE_ALIAS,
        "--start-ms",
        str(T0),
        "--end-ms",
        str(T0 + END_MS),
        "--window-ms",
        "5000",
        "--sample-period-ms",
        "1000",
        "--horizon-ms",
        "1000",
        "--horizon-ms",
        "2000",
        "--target-tolerance-ms",
        "100",
        "--stale-after-ms",
        "1500",
        "--feature-metric",
        "air.room",
        "--target-metric",
        "air.gpu_exhaust",
    ]


def v2_args(env: dict[str, Path], **overrides: str | None) -> list[str]:
    options: dict[str, str | None] = {
        "--action-step-ms": "1000",
        "--action-steps": "2",
        "--action-stale-after-ms": "1500",
        "--declared-changes": str(env["declared"]),
        "--calibration-history-db": str(env["history"]),
    }
    options.update({"--" + key.replace("_", "-"): value for key, value in overrides.items()})
    args = ["--dataset-version", "2", *common_args(env)]
    for key, value in options.items():
        if value is not None:
            args += [key, value]
    return args


def log_lines(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.splitlines() if line.startswith("{")]


def manifest_of(env: dict[str, Path]) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(
        (env["out"] / ARTIFACT_ONE / "manifest.json").read_text(encoding="utf-8")
    )
    return loaded


def test_v2_cli_writes_a_dataset(cli_env, capsys):
    assert dataset_module.main(v2_args(cli_env)) == 0
    manifest = manifest_of(cli_env)
    assert manifest["schema_version"] == 2
    assert manifest["example_count"] == 1
    written = [line for line in log_lines(capsys.readouterr().err) if "書き出した" in line["msg"]]
    assert written, "書き出しの構造化ログが無い"
    expected = hashlib.sha256(cli_env["declared"].read_bytes()).hexdigest()
    assert written[0]["declared_changes_sha256"] == expected
    assert written[0]["declared_changes"] == 0


def test_v1_cli_still_works_with_an_explicit_version(cli_env):
    assert dataset_module.main(["--dataset-version", "1", *common_args(cli_env)]) == 0
    assert manifest_of(cli_env)["schema_version"] == 1


def test_dataset_version_has_no_default(cli_env):
    with pytest.raises(SystemExit) as raised:
        dataset_module.main(common_args(cli_env))
    assert raised.value.code == 2


@pytest.mark.parametrize(
    "option",
    [
        "action_step_ms",
        "action_steps",
        "action_stale_after_ms",
        "declared_changes",
        "calibration_history_db",
    ],
)
def test_v2_requires_each_v2_option(cli_env, capsys, option):
    with pytest.raises(SystemExit) as raised:
        dataset_module.main(v2_args(cli_env, **{option: None}))
    assert raised.value.code == 2
    assert "--" + option.replace("_", "-") in capsys.readouterr().err
    assert not (cli_env["out"] / ARTIFACT_ONE).exists()


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--action-step-ms", "1000"),
        ("--action-steps", "2"),
        ("--action-stale-after-ms", "1500"),
        ("--declared-changes", "declared.yaml"),
        ("--calibration-history-db", "history.db"),
    ],
)
def test_v1_refuses_v2_options(cli_env, option, value):
    with pytest.raises(SystemExit) as raised:
        dataset_module.main(["--dataset-version", "1", *common_args(cli_env), option, value])
    assert raised.value.code == 2


def test_calibration_history_db_must_not_be_the_dedicated_db(cli_env, tmp_path):
    with pytest.raises(SystemExit) as raised:
        dataset_module.main(v2_args(cli_env, calibration_history_db=str(cli_env["db"])))
    assert raised.value.code == 2
    alias = tmp_path / "alias.db"
    alias.symlink_to(cli_env["db"])
    with pytest.raises(SystemExit):
        dataset_module.main(v2_args(cli_env, calibration_history_db=str(alias)))
    hard = tmp_path / "hard.db"
    os.link(cli_env["db"], hard)
    with pytest.raises(SystemExit):
        dataset_module.main(v2_args(cli_env, calibration_history_db=str(hard)))
    assert not (cli_env["out"] / ARTIFACT_ONE).exists()


def test_invalid_declaration_is_refused_before_opening_any_db(cli_env, tmp_path, monkeypatch):
    bad = write(tmp_path / "bad.yaml", "schema_version: 1\n")

    def must_not_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("宣言の検証より前に DB を開いた")

    monkeypatch.setattr(dataset_module, "read_calibration_history", must_not_open)
    monkeypatch.setattr(dataset_module, "SqliteStore", must_not_open)
    assert dataset_module.main(v2_args(cli_env, declared_changes=str(bad))) == 1
    assert not (cli_env["out"] / ARTIFACT_ONE).exists()


def test_declared_calibration_change_in_the_period_refuses_generation(cli_env, tmp_path, capsys):
    # 期間 [T0, T0 + 7000] の終端と同じ秒の中（区間の下端だけが期間に入る。0109 §5 #1）
    declared = write(
        tmp_path / "cal.yaml",
        f"schema_version: 1\nchanges:\n  - kind: calibration_changed\n    ts_ms: {T0 + 7_999}\n",
    )
    assert dataset_module.main(v2_args(cli_env, declared_changes=str(declared))) == 1
    assert not (cli_env["out"] / ARTIFACT_ONE).exists()
    refused = [line for line in log_lines(capsys.readouterr().err) if "拒否" in line["msg"]]
    assert refused and "宣言" in str(refused[0]["reason"])


def test_recorded_change_is_reported_as_a_record(cli_env, tmp_path, capsys):
    history = history_db(tmp_path / "changed.db", COVERING_ROW_MS, T0 + 3_000)
    assert dataset_module.main(v2_args(cli_env, calibration_history_db=str(history))) == 1
    refused = [line for line in log_lines(capsys.readouterr().err) if "拒否" in line["msg"]]
    assert refused and "記録の行" in str(refused[0]["reason"])


def test_other_kinds_in_the_period_only_warn(cli_env, tmp_path, capsys):
    declared = write(
        tmp_path / "fan.yaml",
        f"schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: {T0 + 6_000}\n",
    )
    assert dataset_module.main(v2_args(cli_env, declared_changes=str(declared))) == 0
    lines = log_lines(capsys.readouterr().err)
    warnings = [line for line in lines if line.get("level") == "warning"]
    assert warnings and warnings[0]["changes"] == [{"kind": "fan_replaced", "ts_ms": T0 + 6_000}]
    written = [line for line in lines if "書き出した" in line["msg"]]
    assert written[0]["declared_changes_in_period"] == 1
    assert written[0]["declared_changes_by_kind"] == {"fan_replaced": 1}
