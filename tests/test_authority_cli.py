"""`coldaisle-authority raise` / `rollback`（#92 / 決定記録 0086 段階 3b）。

**実機も実 root も要らない。**

実行者は `ProcessIdentity` を差し替えて試す（0086 §2.9）。試験の process 自身は
authority のディレクトリの所有者（= fand の役）である。

守る性質:

1. 承認者は引数でもファイルでも受け取らない（`--approver` が無い・承認ファイルの `approver` を拒む）
2. 昇格の event は実行した uid（`uid.<数値>`）に束縛される
3. store の拒否（§2.3）を終了コードと理由の code へ写す。journal は書かない
4. rollback は uid で拒まず、`uid.<数値>` を残す。環境変数は効かない
5. CLI はディレクトリを作らない
6. 構造: `raise_stage()` を呼ぶのはこの CLI だけで、どこからも import されない
"""

from __future__ import annotations

import ast
import io
import json
import logging
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from coldaisle import authority_cli, logs
from coldaisle.authority_cli import (
    EXIT_APPROVAL_REJECTED,
    EXIT_APPROVER_REJECTED,
    EXIT_AUDIT_NOT_LOGGED,
    EXIT_FAILED,
    EXIT_NOT_DURABLE,
    EXIT_OK,
    main,
)
from coldaisle.clock import SimulatedClock
from coldaisle.control import AUTHORITY_STATE_FILENAME, AuthorityStage, AutomaticCause
from test_authority_rollout import (
    _FIXTURES,
    APPROVER,
    APPROVER_IDENTITY,
    APPROVER_UID,
    NOW_MS,
    FixedIdentity,
    approval_for,
    report_document,
    store,
)

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "coldaisle"
CONFIG_DIR = Path(_FIXTURES.name) / "config-full-"
"""`test_authority_rollout.DEFAULT_CONFIG` を作ったディレクトリ。証拠の checksum が一致する。"""
REGISTRY_ROOT = Path(_FIXTURES.name) / "registry-1.0.0"
"""`test_authority_rollout.PRODUCTION_REGISTRY` の root。"""
RUNS_AS_ROOT = os.geteuid() == 0


def authority_root(tmp_path: Path) -> Path:
    return tmp_path / "authority"


def journal_path(tmp_path: Path) -> Path:
    return authority_root(tmp_path) / AUTHORITY_STATE_FILENAME


def shared_root(tmp_path: Path) -> Path:
    """導入手順が作る形（`install -d ... -m 2770`）。"""
    root = authority_root(tmp_path)
    root.mkdir()
    os.chmod(root, 0o2770)
    if not os.stat(root).st_mode & stat.S_ISGID:
        pytest.skip("この環境ではディレクトリに setgid を付けられない")
    return root


def shared_registry(tmp_path: Path) -> Path:
    """導入手順が作る形の Registry（`2770`・lock は作ってある。決定記録 0104 §2.3）。

    `REGISTRY_ROOT`（他の試験と共有する fixture）の写し。CLI は Registry を共有の root モードで
    開くので、setgid の無い fixture のままでは読めない。
    """
    root = tmp_path / "registry"
    if not root.exists():
        shutil.copytree(REGISTRY_ROOT, root)
        os.chmod(root, 0o2770)
        if not os.stat(root).st_mode & stat.S_ISGID:
            pytest.skip("この環境ではディレクトリに setgid を付けられない")
    return root


def write_inputs(tmp_path: Path, **overrides: Any) -> tuple[Path, Path]:
    """人が書く承認ファイル（approver を含まない）と、承認が指した報告。"""
    document = report_document()
    approval = json.loads(approval_for(document).model_dump_json())
    del approval["approver"]
    del approval["approver_binding"]
    approval |= overrides
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    report_path = tmp_path / "evaluation.json"
    report_path.write_bytes(document)
    return approval_path, report_path


def raise_argv(tmp_path: Path, approval: Path, report: Path) -> list[str]:
    return [
        "raise",
        "--authority-root",
        str(authority_root(tmp_path)),
        "--approval",
        str(approval),
        "--report",
        str(report),
        "--config-dir",
        str(CONFIG_DIR),
        "--registry-root",
        str(shared_registry(tmp_path)),
        "--registry-limits",
        str(REPO / "config"),
    ]


def rollback_argv(tmp_path: Path) -> list[str]:
    return ["rollback", "--authority-root", str(authority_root(tmp_path)), "--reason", "戻す"]


def run(argv: list[str], identity: Any = APPROVER_IDENTITY) -> int:
    return main(argv, identity=identity, clock=SimulatedClock(NOW_MS))


def log_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


# ================================================================ 1. 承認者を受け取らない（§2.5）


@pytest.mark.parametrize("command", ["raise", "rollback"])
def test_there_is_no_approver_option(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main([command, "--help"])
    assert caught.value.code == 0
    assert "--approver" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as caught:
        main([command, "--approver", "rack-owner"])
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "field", [{"approver": APPROVER}, {"approver_binding": "process_uid"}, {"approver": "x"}]
)
def test_an_approval_file_that_names_the_approver_is_refused(
    tmp_path: Path, field: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """承認ファイルに承認者を書かせない。合っていても自己申告の欄を残さない（0086 §2.5）。"""
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path, **field)

    assert run(raise_argv(tmp_path, approval, report)) == EXIT_APPROVAL_REJECTED

    assert not journal_path(tmp_path).exists()
    [line] = log_lines(capsys.readouterr().err)
    assert line["code"] == "invalid_approval"


# ================================================================ 2. 昇格（§2.1 / §2.8）


def test_raise_records_the_executing_uid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)

    assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK

    journal = store(tmp_path).read()
    event = journal.events[-1]
    assert journal.stage is AuthorityStage.LIMITED
    assert event.actor == APPROVER
    assert event.approval is not None and event.approval.approver_binding == "process_uid"
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["event"] == "raised"
    assert result["actor"].startswith(APPROVER)
    assert (result["from_stage"], result["to_stage"]) == ("shadow", "limited")
    [line] = log_lines(captured.err)
    assert line["event"] == "raised"
    assert (line["uid"], line["euid"], line["revision"]) == (APPROVER_UID, APPROVER_UID, 1)
    assert line["report_sha256"] == result["report_sha256"]
    for name in (AUTHORITY_STATE_FILENAME, ".authority.lock"):
        assert stat.S_IMODE(os.stat(authority_root(tmp_path) / name).st_mode) == 0o660


def test_the_name_is_shown_but_never_written_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """名前は表示のときだけ引く。journal とログには `uid.<数値>` だけ（0086 §2.8）。"""

    class Entry:
        pw_name = "rack-operator"

    monkeypatch.setattr(authority_cli.pwd, "getpwuid", lambda uid: Entry())
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)

    assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK

    captured = capsys.readouterr()
    assert json.loads(captured.out)["actor"] == f"{APPROVER}（rack-operator）"
    assert "rack-operator" not in journal_path(tmp_path).read_text("utf-8")
    assert "rack-operator" not in captured.err


# ================================================================ 3. 拒否の写し（§2.3）


@pytest.mark.parametrize(
    ("identity", "code"),
    [
        (FixedIdentity(0), "approver_is_root"),
        (FixedIdentity(4242, euid=0), "uid_differs_from_euid"),
    ],
)
def test_store_rejections_map_to_an_exit_code_and_a_reason(
    tmp_path: Path, identity: Any, code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)

    assert run(raise_argv(tmp_path, approval, report), identity) == EXIT_APPROVER_REJECTED

    assert not journal_path(tmp_path).exists()
    [line] = log_lines(capsys.readouterr().err)
    assert line["event"] == "raise_failed"
    assert line["code"] == code


@pytest.mark.skipif(RUNS_AS_ROOT, reason="root では所有者の拒否が root の拒否に先取りされる")
def test_the_fand_user_cannot_raise_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)

    exit_code = run(raise_argv(tmp_path, approval, report), FixedIdentity(os.geteuid()))

    assert exit_code == EXIT_APPROVER_REJECTED
    [line] = log_lines(capsys.readouterr().err)
    assert line["code"] == "approver_owns_authority_root"
    assert not journal_path(tmp_path).exists()


def test_an_approval_refused_by_the_store_is_exit_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path, expected_revision=5)

    assert run(raise_argv(tmp_path, approval, report)) == EXIT_APPROVAL_REJECTED
    [line] = log_lines(capsys.readouterr().err)
    assert line["code"] == "approval_rejected"


# ================================================================ 4. rollback（§2.6）


def test_root_can_roll_back_and_is_recorded_as_uid_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK
    capsys.readouterr()

    assert run(rollback_argv(tmp_path), FixedIdentity(0)) == EXIT_OK

    journal = store(tmp_path).read()
    assert journal.stage is AuthorityStage.SHADOW
    assert journal.events[-1].actor == "uid.0"
    result = json.loads(capsys.readouterr().out)
    assert (result["event"], result["from_stage"], result["to_stage"]) == (
        "rolled_back",
        "limited",
        "shadow",
    )


def test_rollback_ignores_sudo_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """既定の実行者（`os.getuid()`）を使い、`SUDO_UID` などは読まない（0086 §2.1）。"""
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK
    for name in ("SUDO_UID", "SUDO_USER", "LOGNAME", "USER"):
        monkeypatch.setenv(name, "12345")

    assert main(rollback_argv(tmp_path), clock=SimulatedClock(NOW_MS)) == EXIT_OK

    assert store(tmp_path).read().events[-1].actor == f"uid.{os.getuid()}"


def test_rollback_at_baseline_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared_root(tmp_path)

    assert run(rollback_argv(tmp_path)) == EXIT_OK

    assert not journal_path(tmp_path).exists()
    assert json.loads(capsys.readouterr().out)["event"] == "already_baseline"


# ================================================================ 5. ディレクトリ（§2.4）


@pytest.mark.parametrize("command", ["raise", "rollback"])
def test_the_cli_does_not_create_the_authority_directory(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    approval, report = write_inputs(tmp_path)
    argv = raise_argv(tmp_path, approval, report) if command == "raise" else rollback_argv(tmp_path)

    assert run(argv) == EXIT_FAILED

    assert not authority_root(tmp_path).exists()
    [line] = log_lines(capsys.readouterr().err)
    assert line["code"] == "store_error"


# ================================================================ 6. 構造（0072 §2.1）


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_only_this_cli_calls_raise_stage() -> None:
    """**昇格は人が実行するこの CLI だけ。** fand・control・API・AI・eventd から届かない。"""
    callers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        # 定義している module は除く（docstring で名前を挙げるだけ）
        if path != SRC / "control" / "authority.py"
        and ".raise_stage(" in path.read_text(encoding="utf-8")
    )
    assert callers == ["authority_cli.py"]


def test_nothing_imports_the_cli() -> None:
    importers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name != "authority_cli.py"
        and any(name.startswith("coldaisle.authority_cli") for name in _imports(path))
    )
    assert importers == []
    assert authority_cli.__name__ == "coldaisle.authority_cli"


# ================================================================ 7. 境界の失敗（codex）


@pytest.mark.parametrize("target", ["registry_limits", "control_config"])
def test_malformed_yaml_is_a_configuration_failure_not_a_traceback(
    tmp_path: Path, target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`yaml.YAMLError` は ValueError ではない。終了コード 1 と構造化ログで止める。"""
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    broken = tmp_path / "broken"
    if target == "registry_limits":
        broken.mkdir()
        (broken / "model-registry.yaml").write_text("a: [1, 2\n", encoding="utf-8")
        argv = raise_argv(tmp_path, approval, report)
        argv[argv.index("--registry-limits") + 1] = str(broken)
    else:
        shutil.copytree(CONFIG_DIR, broken)
        (broken / "safety.yaml").write_text("a: [1, 2\n", encoding="utf-8")
        argv = raise_argv(tmp_path, approval, report)
        argv[argv.index("--config-dir") + 1] = str(broken)

    assert run(argv) == EXIT_FAILED

    assert not journal_path(tmp_path).exists()
    [line] = log_lines(capsys.readouterr().err)
    assert (line["event"], line["code"]) == ("raise_failed", "io_or_config_error")


@pytest.mark.parametrize("breakage", ["no_lock", "no_setgid", "other_permissions", "no_root"])
def test_an_unreadable_registry_is_exit_1_registry_error_not_an_evidence_rejection(
    tmp_path: Path, breakage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Registry を読めないのは導入の誤りで、証拠の拒否（4）ではない（決定記録 0104 §5 の 11）。

    承認者の側は root も lock も作らない（0104 §2.4）。
    """
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    argv = raise_argv(tmp_path, approval, report)
    registry = shared_registry(tmp_path)
    if breakage == "no_lock":
        (registry / ".registry.lock").unlink()
    elif breakage == "no_setgid":
        os.chmod(registry, 0o770)
    elif breakage == "other_permissions":
        os.chmod(registry, 0o2775)
    else:
        argv[argv.index("--registry-root") + 1] = str(tmp_path / "missing")
    before = sorted(os.listdir(registry))

    assert run(argv) == EXIT_FAILED

    assert not journal_path(tmp_path).exists()
    assert sorted(os.listdir(registry)) == before
    assert not (tmp_path / "missing").exists()
    [line] = log_lines(capsys.readouterr().err)
    assert (line["event"], line["code"]) == ("raise_failed", "registry_error")


def test_a_rollback_already_done_by_someone_else_is_not_claimed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**別の書き手が先に下げた変更を、自分の rollback として記録しない**（codex）。

    追記したかは store が lock の中で決める。CLI が lock の外で読んだ journal と比べると、
    間に入った自動降格の event を自分の `rolled_back` として出してしまう。
    """
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK
    store(tmp_path).lower_stage(
        to_stage=AuthorityStage.SHADOW,
        actor="control_runtime",
        reason="自動降格",
        cause=AutomaticCause.SAFETY_EMERGENCY,
    )
    capsys.readouterr()

    assert run(rollback_argv(tmp_path)) == EXIT_OK

    journal = store(tmp_path).read()
    assert journal.revision == 2 and journal.events[-1].actor == "control_runtime"
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert (result["event"], result["reason"]) == ("already_baseline", None)
    [line] = log_lines(captured.err)
    assert line["event"] == "already_baseline"


def test_the_store_reports_whether_this_call_appended(tmp_path: Path) -> None:
    authority = store(tmp_path)
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK

    first, appended = authority.rollback_to_baseline_with_outcome(actor="uid.1", reason="戻す")
    again, none = authority.rollback_to_baseline_with_outcome(actor="uid.2", reason="戻す")

    assert appended is not None and appended.actor == "uid.1"
    assert appended == first.events[-1]
    assert none is None and again == first


# ======================================================= 8. 書いた後の出力と監査ログ（codex）


class ClosedStdout(io.StringIO):
    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


class AsciiStdout(io.StringIO):
    """`PYTHONIOENCODING=ascii` の stdout。日本語の理由を書けない。"""

    def write(self, text: str) -> int:
        text.encode("ascii")
        return super().write(text)


@pytest.mark.parametrize("stdout", [ClosedStdout, AsciiStdout])
@pytest.mark.parametrize("command", ["raise", "rollback"])
def test_a_closed_stdout_after_the_commit_is_still_success(
    tmp_path: Path,
    command: str,
    stdout: type[io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**書いた変更を「失敗」と伝えない。** stdout が閉じていても終了コード 0（codex P1）。"""
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    if command == "rollback":
        assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK
        argv = rollback_argv(tmp_path)
    else:
        argv = raise_argv(tmp_path, approval, report)
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdout", stdout())

    assert run(argv) == EXIT_OK

    expected = AuthorityStage.SHADOW if command == "rollback" else AuthorityStage.LIMITED
    assert store(tmp_path).read().stage is expected
    events = [line["event"] for line in log_lines(capsys.readouterr().err)]
    assert events == ["rolled_back" if command == "rollback" else "raised", "result_not_written"]


@pytest.mark.parametrize("second_fsync_fails", [False, True])
def test_a_repeated_rollback_resyncs_the_directory_before_claiming_durable(
    tmp_path: Path,
    second_fsync_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**やり直した rollback は、既に Baseline でも fsync し直してから durable と言う**。

    1回目の rollback が置き換えの後の fsync に失敗した（終了コード 5）。手順どおりやり直すと
    journal は既に SHADOW なので何も書かないが、fsync せずに成功を返すと、1回目の置き換えを
    誰も永続化しない。
    """
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK
    real = os.fsync
    directory_calls: list[bool] = []
    failing = [True]

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls.append(failing[0])
            if failing[0]:
                raise OSError(5, "Input/output error")
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    assert run(rollback_argv(tmp_path)) == EXIT_NOT_DURABLE
    capsys.readouterr()
    failing[0] = second_fsync_fails
    before = len(directory_calls)

    exit_code = run(rollback_argv(tmp_path))

    assert len(directory_calls) > before, "no-op の分岐でもディレクトリを fsync し直す"
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["event"] == "already_baseline"
    [line] = log_lines(captured.err)
    assert line["event"] == "already_baseline"
    if second_fsync_fails:
        assert exit_code == EXIT_NOT_DURABLE
        assert result["durable"] is False
        assert line["durable"] is False
    else:
        assert exit_code == EXIT_OK
        assert result["durable"] is True
    monkeypatch.setattr(os, "fsync", real)
    assert store(tmp_path).read().stage is AuthorityStage.SHADOW


@pytest.mark.parametrize("command", ["raise", "rollback"])
def test_the_audit_line_survives_an_ascii_only_stderr(
    tmp_path: Path, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PYTHONIOENCODING=ascii` の stderr でも監査の1行（0086 §2.8）が欠けない（codex P2）。

    `StreamHandler` は encode の失敗を内部で握るので、日本語のままだと journal だけが変わる。
    """
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    if command == "rollback":
        assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK
        argv = rollback_argv(tmp_path)
    else:
        argv = raise_argv(tmp_path, approval, report)
    raw = io.BytesIO()
    stderr = io.TextIOWrapper(raw, encoding="ascii", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", AsciiStdout())

    assert run(argv) == EXIT_OK

    text = raw.getvalue().decode("ascii")
    assert "Traceback" not in text, "logging が encode の失敗を握っていない"
    lines = log_lines(text)
    expected = "rolled_back" if command == "rollback" else "raised"
    assert [line["event"] for line in lines] == [expected, "result_not_written"]
    assert "authority stage" in lines[0]["msg"], "日本語の msg は JSON の escape で元に戻る"


def test_the_audit_log_level_cannot_be_lowered(capsys: pytest.CaptureFixture[str]) -> None:
    """監査の1行（0086 §2.8）を `--log-level` で消せない（codex P2）。"""
    with pytest.raises(SystemExit) as caught:
        main(["--log-level", "CRITICAL", "rollback", "--authority-root", "/x", "--reason", "r"])
    assert caught.value.code == 2
    capsys.readouterr()


def test_a_failing_name_service_falls_back_to_the_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """LDAP・SSSD が使えず `getpwuid` が OSError でも、書いた変更を失敗にしない（codex P1）。"""

    def unavailable(uid: int) -> object:
        raise OSError(5, "nss backend unavailable")

    monkeypatch.setattr(authority_cli.pwd, "getpwuid", unavailable)
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)

    assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK

    assert json.loads(capsys.readouterr().out)["actor"] == APPROVER


@pytest.mark.parametrize("command", ["raise", "rollback"])
def test_a_failed_directory_fsync_after_replace_is_not_reported_as_no_change(
    tmp_path: Path,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**置き換えた journal は見えている。** 「行わなかった」と伝えない（codex P1）。"""
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    if command == "rollback":
        assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK
        argv = rollback_argv(tmp_path)
    else:
        argv = raise_argv(tmp_path, approval, report)
    capsys.readouterr()
    real = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)

    # 成功（0）とも失敗（1）とも分ける。失われた rollback は上げた authority を黙って戻すので、
    # 終了コードで気づけるようにする（2026-10-01 所有者の決定）。
    assert run(argv) == EXIT_NOT_DURABLE

    monkeypatch.setattr(os, "fsync", real)
    expected = AuthorityStage.SHADOW if command == "rollback" else AuthorityStage.LIMITED
    journal = store(tmp_path).read()
    assert journal.stage is expected
    assert journal.events[-1].actor == APPROVER
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["durable"] is False
    assert result["event"] == ("rolled_back" if command == "rollback" else "raised")
    [line] = log_lines(captured.err)
    assert (line["level"], line["event"], line["durable"]) == ("warning", result["event"], False)


# ============================================ 9. 監査ログを書けない（#218 / 決定記録 0091）


class BrokenPipeStderr(io.StringIO):
    """閉じたパイプ・切れた journald の stream。"""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


class FullDiskStderr(io.StringIO):
    """`ENOSPC` の出力先。"""

    def write(self, text: str) -> int:
        raise OSError(28, "No space left on device")


class FailingFlushStderr(io.StringIO):
    """書き込みは受けるが、flush で失敗する（buffer を書き出せない）。"""

    def flush(self) -> None:
        raise OSError(5, "Input/output error")


class FailingFinalFlushStderr(io.StringIO):
    """操作の行の flush（1回目）は通るが、終了直前の flush で失敗する。"""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        if self.flushes > 1:
            raise OSError(5, "Input/output error")


def _argv_for(tmp_path: Path, command: str) -> list[str]:
    """``rollback`` は LIMITED から戻す。``noop`` は既に Baseline の rollback。"""
    shared_root(tmp_path)
    approval, report = write_inputs(tmp_path)
    if command == "raise":
        return raise_argv(tmp_path, approval, report)
    if command == "rollback":
        assert run(raise_argv(tmp_path, approval, report)) == EXIT_OK
    return rollback_argv(tmp_path)


_EXPECTED_EVENT = {"raise": "raised", "rollback": "rolled_back", "noop": "already_baseline"}
_EXPECTED_STAGE = {
    "raise": AuthorityStage.LIMITED,
    "rollback": AuthorityStage.SHADOW,
    "noop": AuthorityStage.SHADOW,
}


@pytest.mark.parametrize("command", ["raise", "rollback", "noop"])
def test_a_successful_operation_reports_audit_logged(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """結果の `audit_logged` は常に出る。書けたら `true`・終了コード 0。"""
    argv = _argv_for(tmp_path, command)
    capsys.readouterr()

    assert run(argv) == EXIT_OK

    result = json.loads(capsys.readouterr().out)
    assert result["event"] == _EXPECTED_EVENT[command]
    assert result["audit_logged"] is True


@pytest.mark.parametrize("stderr", [BrokenPipeStderr, FullDiskStderr, FailingFlushStderr])
@pytest.mark.parametrize("command", ["raise", "rollback", "noop"])
def test_an_unwritable_audit_sink_is_exit_6_not_success(
    tmp_path: Path,
    command: str,
    stderr: type[io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**監査の行を書けなかったことを握りつぶさない**（0091 §2.2 / §2.3）。

    journal の変更は確定しているので 1 にはしない。raise と rollback（no-op を含む）を分けない。
    """
    argv = _argv_for(tmp_path, command)
    capsys.readouterr()
    monkeypatch.setattr(sys, "stderr", stderr())

    assert run(argv) == EXIT_AUDIT_NOT_LOGGED

    assert store(tmp_path).read().stage is _EXPECTED_STAGE[command]
    result = json.loads(capsys.readouterr().out)
    assert result["event"] == _EXPECTED_EVENT[command]
    assert (result["durable"], result["audit_logged"]) == (True, False)


def test_a_failure_of_the_final_flush_is_exit_6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """判定は終了直前の flush まで含める（0091 §2.2）。終了コードを正とする。"""
    argv = _argv_for(tmp_path, "rollback")
    capsys.readouterr()
    sink = FailingFinalFlushStderr()
    monkeypatch.setattr(sys, "stderr", sink)

    assert run(argv) == EXIT_AUDIT_NOT_LOGGED

    assert sink.flushes >= 2
    assert "rolled_back" in sink.getvalue(), "操作の行そのものは書けている"
    assert store(tmp_path).read().stage is AuthorityStage.SHADOW


@pytest.mark.parametrize("command", ["raise", "rollback"])
def test_a_failed_fsync_takes_precedence_over_a_failed_audit_line(
    tmp_path: Path,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """5 と 6 が重なったら 5。結果には `audit_logged: false` を出す（0091 §2.2）。"""
    argv = _argv_for(tmp_path, command)
    capsys.readouterr()
    real = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(sys, "stderr", BrokenPipeStderr())

    assert run(argv) == EXIT_NOT_DURABLE

    monkeypatch.setattr(os, "fsync", real)
    assert store(tmp_path).read().stage is _EXPECTED_STAGE[command]
    result = json.loads(capsys.readouterr().out)
    assert (result["durable"], result["audit_logged"]) == (False, False)


@pytest.mark.parametrize("command", ["raise", "rollback"])
def test_an_operation_not_performed_keeps_its_exit_code(
    tmp_path: Path, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """行われなかった失敗（1）は、監査の行を書けなくても 1 のまま（0091 §2.2）。"""
    approval, report = write_inputs(tmp_path)
    argv = raise_argv(tmp_path, approval, report) if command == "raise" else rollback_argv(tmp_path)
    monkeypatch.setattr(sys, "stderr", BrokenPipeStderr())

    assert run(argv) == EXIT_FAILED

    assert not journal_path(tmp_path).exists()


def test_other_commands_keep_the_plain_stream_handler() -> None:
    """handler を差し替えるのは `coldaisle-authority` だけ（0091 §2.1）。"""
    stream = io.StringIO()
    logs.configure("INFO", stream)
    try:
        [handler] = logging.getLogger().handlers
        assert type(handler) is logging.StreamHandler
    finally:
        logs.configure("INFO")


@pytest.mark.parametrize("command", ["raise", "rollback"])
def test_the_final_flush_is_checked_even_when_fsync_failed(
    tmp_path: Path,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """5 の経路でも終了直前の flush を行い、失敗を検出する（codex P2。0091 §5 の 3）。

    終了コードは 5 のまま。結果は flush より先に出すので `audit_logged` は真のままになる。
    """
    argv = _argv_for(tmp_path, command)
    capsys.readouterr()
    real = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    sink = FailingFinalFlushStderr()
    monkeypatch.setattr(sys, "stderr", sink)

    assert run(argv) == EXIT_NOT_DURABLE

    monkeypatch.setattr(os, "fsync", real)
    assert sink.flushes >= 2, "5 の経路でも終了直前の flush を行う"
    assert store(tmp_path).read().stage is _EXPECTED_STAGE[command]
    result = json.loads(capsys.readouterr().out)
    assert (result["durable"], result["audit_logged"]) == (False, True)
