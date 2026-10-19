"""学習の CLI ``coldaisle-train``（#84 / 決定記録 0150 §2.9）。実機は要らない。

- 自己一致（同じ入力・同じ ``--created-at`` から同じ bytes）・壁時計を読まない
- ``--apply`` の無い実行は Registry を書かない。``--apply`` は candidate を1つだけ足す
- 検査・学習の前の拒否では ``--out`` を作らず Registry も変わらない
- 公開の手順（mkdir の排他・印の link・後始末・公開の後の失敗）と、path の照合・中身の再検証
- 登録の ``RegistryDurabilityError`` の2つの意味と、登録の後の検査
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from coldaisle import train, training_output
from coldaisle.calibration_offsets import effective_metric_offsets
from coldaisle.clock import SimulatedClock
from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.model_registry import (
    ModelRegistry,
    RegistryDurabilityError,
    RegistrySnapshot,
)
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.calibration_history import CalibrationActivation, read_calibration_history
from test_thermal_dataset_v2 import T0
from train_world import END_MS, World, limits, make_world

# ---------------------------------------------------------------- 道具


@pytest.fixture
def world(tmp_path: Path, rules: QualityRules) -> World:
    return make_world(tmp_path / "w", rules)


def log_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def train_errors(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [
        line
        for line in log_lines(capsys.readouterr().err)
        if line["logger"] == "coldaisle.train" and line["level"] == "error"
    ]


def stages_of(capsys: pytest.CaptureFixture[str]) -> list[str]:
    errors = train_errors(capsys)
    assert len(errors) == 1, errors
    stages: list[str] = errors[0]["stages"]
    assert errors[0]["stage"] == stages[0]
    return stages


def registry_state(world: World) -> tuple[bytes | None, list[str]]:
    """``registry.json`` の bytes と ``artifacts/`` の下のファイル（Registry が変わらないこと）。"""
    state = world.registry / "registry.json"
    files = (
        sorted(
            str(p.relative_to(world.registry))
            for p in (world.registry / "artifacts").rglob("*")
            if p.is_file()
        )
        if (world.registry / "artifacts").exists()
        else []
    )
    return (state.read_bytes() if state.exists() else None), files


def snapshot(world: World) -> RegistrySnapshot:
    return ModelRegistry(world.registry, limits=limits()).inspect()


def assert_one_candidate_added(
    before: RegistrySnapshot, after: RegistrySnapshot, out: Path, *, actor: str = "trainer"
) -> str:
    """(1)〜(4)（0150 §2.9）: 足されたもの以外が1つでも変われば失敗にする。"""
    assert after.revision == before.revision + 1
    added = set(after.artifacts) - set(before.artifacts)
    assert len(added) == 1
    [key] = added
    record = after.artifacts[key]
    assert record.status.value == "candidate"
    metadata = json.loads((out / "metadata.json").read_bytes())
    assert json.loads(record.metadata.model_dump_json()) == metadata
    assert len(after.audit) == len(before.audit) + 1
    event = after.audit[-1]
    assert event.event.value == "registered"
    assert event.revision == after.revision
    assert event.artifact.key == key
    assert event.actor == actor
    assert after.production == before.production
    assert after.audit[: len(before.audit)] == before.audit
    for name, record_before in before.artifacts.items():
        assert after.artifacts[name] == record_before
    return key


def files_of(out: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(out.iterdir())}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 成功・自己一致


def test_training_writes_the_three_files_and_a_valid_marker(world: World) -> None:
    out = world.outputs / "run"
    assert train.main(world.argv(out)) == 0
    assert sorted(p.name for p in out.iterdir()) == [
        "COMPLETE.json",
        "artifact.json",
        "metadata.json",
        "training-report.json",
    ]
    fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        verified = training_output.read_training_output(
            fd, max_artifact_bytes=10**7, max_other_bytes=10**6
        )
    finally:
        os.close(fd)
    metadata = json.loads(verified.metadata)
    assert metadata["sha256"] == sha(verified.artifact)
    assert metadata["capability"] == "counterfactual_action"
    assert metadata["authority_compatibility"] == ["shadow"]
    report = json.loads(verified.report)
    assert report["artifact"]["sha256"] == sha(verified.artifact)
    assert report["split"]["train"]["count"] > 0 and report["split"]["validation"]["count"] > 0
    assert report["inputs"]["training_spec_sha256"] == sha(world.spec.read_bytes())
    assert report["inputs"]["calibration_sha256"] == sha(world.calibration.read_bytes())
    assert "created_at" not in report, "報告に生成時刻の欄を置かない（0150 §2.7）"
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_same_inputs_give_the_same_bytes_and_created_at_is_used(world: World) -> None:
    first, second, third = (world.outputs / name for name in ("a", "b", "c"))
    assert train.main(world.argv(first)) == 0
    assert train.main(world.argv(second)) == 0
    assert train.main(world.argv(third, **{"--created-at": "2026-10-21T00:00:00+00:00"})) == 0
    for name in ("artifact.json", "metadata.json", "training-report.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert (first / "artifact.json").read_bytes() != (third / "artifact.json").read_bytes()


def test_without_apply_the_registry_is_neither_created_nor_changed(world: World) -> None:
    assert train.main(world.argv(world.outputs / "a")) == 0
    assert not world.registry.exists()
    assert train.main(world.apply_argv(world.outputs / "b")) == 0
    before = registry_state(world)
    assert train.main(world.argv(world.outputs / "c")) == 0
    assert registry_state(world) == before


def test_apply_adds_exactly_one_candidate_and_the_same_version_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    ModelRegistry(world.registry, limits=limits())  # 空の Registry
    assert train.main(world.apply_argv(world.outputs / "x")) == 0
    before = snapshot(world)
    out = world.outputs / "a"
    assert train.main(world.apply_argv(out, **{"--model-version": "0.2.0"})) == 0
    key = assert_one_candidate_added(before, snapshot(world), out)
    assert key == "thermal_model/rack-thermal/0.2.0"
    stored = ModelRegistry(world.registry, limits=limits())
    assert stored.inspect().artifacts[key].metadata.code_commit == "0123abc"
    capsys.readouterr()
    state = registry_state(world)
    assert train.main(world.apply_argv(world.outputs / "b", **{"--model-version": "0.2.0"})) == 1
    assert stages_of(capsys) == ["register"]
    assert registry_state(world) == state


# ---------------------------------------------------------------- 学習の前の拒否


def _assert_refused_before_publish(
    world: World, argv: list[str], capsys: pytest.CaptureFixture[str], stage: str = "before_publish"
) -> None:
    capsys.readouterr()
    state = registry_state(world)
    assert train.main(argv) == 1
    assert stages_of(capsys) == [stage]
    assert not (world.outputs / "run").exists()
    assert registry_state(world) == state


@pytest.mark.parametrize("apply", [False, True])
def test_a_tampered_dataset_is_refused(
    world: World, capsys: pytest.CaptureFixture[str], apply: bool
) -> None:
    examples = world.dataset / "examples.jsonl"
    examples.write_bytes(examples.read_bytes().replace(b'"air.room":2', b'"air.room":3', 1))
    argv = world.apply_argv() if apply else world.argv()
    _assert_refused_before_publish(world, argv, capsys)


@pytest.mark.parametrize("apply", [False, True])
def test_the_wrong_replay_path_is_refused(
    world: World, capsys: pytest.CaptureFixture[str], apply: bool
) -> None:
    other = world.root / "other-csv"
    other.mkdir()
    for path in world.replay.iterdir():
        (other / path.name).write_bytes(path.read_bytes())
    csv = next(other.glob("*.csv"))
    csv.write_bytes(csv.read_bytes() + b"\n")
    argv = (world.apply_argv if apply else world.argv)(**{"--replay-path": str(other)})
    _assert_refused_before_publish(world, argv, capsys)


def test_a_calibration_row_after_the_period_is_refused(
    world: World, capsys: pytest.CaptureFixture[str], rules: QualityRules
) -> None:
    last = read_calibration_history(world.prod).last
    with SqliteStore(world.prod, rules=rules, clock=SimulatedClock(0)) as store:
        store.append_calibration_activation(
            CalibrationActivation.next_after(
                last,
                ts_ms=T0 + END_MS + 60_000,
                source_kind="mock",
                offsets=effective_metric_offsets({"front_intake": 0.5}),
                calibrated_at=None,
                calibration_file_sha256="e" * 64,
            )
        )
    _assert_refused_before_publish(world, world.apply_argv(), capsys)


def test_a_declared_calibration_change_after_the_period_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.declared.write_text(
        "schema_version: 1\nchanges:\n"
        f"  - kind: calibration_changed\n    ts_ms: {T0 + END_MS + 60_000}\n",
        encoding="utf-8",
    )
    _assert_refused_before_publish(world, world.argv(), capsys)


def test_the_wrong_calibration_file_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.calibration.write_text(json.dumps({"offsets_c": {"front_intake": 0.5}}), encoding="utf-8")
    _assert_refused_before_publish(world, world.apply_argv(), capsys)


def test_support_axes_without_every_fan_zone_are_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    text = world.spec.read_text(encoding="utf-8")
    world.spec.write_text(text.replace("  - source: fan.top\n    edges: [0.5]\n", ""), "utf-8")
    _assert_refused_before_publish(world, world.apply_argv(), capsys)


def test_an_existing_empty_out_is_refused_and_kept(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = world.outputs / "run"
    out.mkdir(mode=0o700)
    identity = (out.stat().st_dev, out.stat().st_ino)
    assert train.main(world.argv(out)) == 1
    [error] = train_errors(capsys)
    assert error["stage"] == "before_publish" and error["complete_marker_valid"] is False
    assert (out.stat().st_dev, out.stat().st_ino) == identity and not list(out.iterdir())


def test_the_mkdir_eexist_refuses_an_out_created_after_the_early_check(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = world.outputs / "run"
    real = train.train_artifact

    def then_another_process_creates_out(*args: Any) -> train.TrainedOutput:
        trained = real(*args)
        out.mkdir(mode=0o700)
        return trained

    monkeypatch.setattr(train, "train_artifact", then_another_process_creates_out)
    assert train.main(world.argv(out)) == 1
    assert stages_of(capsys) == ["before_publish"]
    assert out.is_dir() and not list(out.iterdir())


def test_an_artifact_that_fails_the_loader_checks_is_not_written(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Other:
        @staticmethod
        def available(_offsets: object) -> RuntimeCalibration:
            return RuntimeCalibration.available({"room_temp": 9.0})

    monkeypatch.setattr(train, "RuntimeCalibration", Other)
    _assert_refused_before_publish(world, world.argv(), capsys)


@pytest.mark.parametrize(
    "missing", ["--registry-limits", "--registry-root", "--actor", "--reason", "--code-commit"]
)
def test_apply_requires_the_registry_arguments(
    world: World, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    argv = world.apply_argv(**{missing: None})
    _assert_refused_before_publish(world, argv, capsys)


def test_invalid_registry_limits_are_refused_before_training(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = world.root / "limits"
    bad.mkdir()
    (bad / "model-registry.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    called: list[object] = []
    monkeypatch.setattr(train, "train_artifact", lambda *a: called.append(a))
    _assert_refused_before_publish(
        world, world.apply_argv(**{"--registry-limits": str(bad)}), capsys
    )
    assert called == []


def test_registry_arguments_without_apply_are_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [*world.argv(), "--registry-root", str(world.registry)]
    _assert_refused_before_publish(world, argv, capsys)


# ---------------------------------------------------------------- 設定の上限と1回の読み

_CONFIGS = (
    "--training-spec",
    "--quality-config",
    "--metrics-config",
    "--declared-changes",
    "--calibration",
    "--registry-limits",
)


def _config_path(world: World, flag: str, root: Path) -> tuple[dict[str, str], Path]:
    """``flag`` に渡す path と、実際に読まれるファイル。"""
    if flag == "--registry-limits":
        directory = root / "limits-dir"
        directory.mkdir()
        return {flag: str(directory)}, directory / "model-registry.yaml"
    return {flag: str(root / "config")}, root / "config"


@pytest.mark.parametrize("flag", _CONFIGS)
def test_each_config_is_read_at_most_one_byte_past_the_bound(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    override, target = _config_path(world, flag, world.root)
    target.write_bytes(b"#" * (train.MAX_TRAINING_CONFIG_BYTES * 3))
    paths: dict[int, str] = {}
    counts: dict[str, int] = {}
    real_open, real_read = os.open, os.read

    def recording_open(path: Any, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, *args, **kwargs)
        paths[fd] = str(path)
        return fd

    def counting_read(fd: int, size: int) -> bytes:
        data = real_read(fd, size)
        name = paths.get(fd)
        if name is not None:
            counts[name] = counts.get(name, 0) + len(data)
        return data

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "read", counting_read)
    argv = world.apply_argv(**override)
    _assert_refused_before_publish(world, argv, capsys)
    assert counts[str(target)] == train.MAX_TRAINING_CONFIG_BYTES + 1


@pytest.mark.parametrize("flag", ["--training-spec", "--calibration", "--declared-changes"])
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_a_symlink_or_fifo_config_is_refused(
    world: World, capsys: pytest.CaptureFixture[str], flag: str, kind: str
) -> None:
    special = world.root / "special"
    if kind == "symlink":
        special.symlink_to(world.spec)
    else:
        os.mkfifo(special)
    _assert_refused_before_publish(world, world.argv(**{flag: str(special)}), capsys)


def test_each_config_is_opened_once(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    watched = {
        str(world.spec),
        str(world.quality),
        str(world.metrics),
        str(world.declared),
        str(world.calibration),
        str(world.limits / "model-registry.yaml"),
    }
    opened: dict[str, int] = {}
    real_os_open, real_io_open = os.open, io.open

    def count(path: Any) -> None:
        if str(path) in watched:
            opened[str(path)] = opened.get(str(path), 0) + 1

    def os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        count(path)
        return real_os_open(path, *args, **kwargs)

    def io_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        count(path)
        return real_io_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(io, "open", io_open)
    monkeypatch.setattr("builtins.open", io_open)
    assert train.main(world.apply_argv()) == 0
    assert opened == dict.fromkeys(watched, 1)


# ---------------------------------------------------------------- 親と祖先の検査


def test_a_missing_parent_is_refused_and_not_created(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = world.root / "missing" / "deeper" / "run"
    capsys.readouterr()
    assert train.main(world.argv(out)) == 1
    assert stages_of(capsys) == ["before_publish"]
    assert not (world.root / "missing").exists()


def test_a_dotdot_out_is_refused(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    _assert_refused_before_publish(
        world, world.argv(world.outputs / ".." / "outputs" / "run"), capsys, "untrusted_path"
    )


@pytest.mark.parametrize(
    ("target", "mode"),
    [("parent", 0o775), ("parent", 0o777), ("ancestor", 0o775), ("ancestor", 0o777)],
)
def test_writable_parents_and_ancestors_are_refused(
    world: World, capsys: pytest.CaptureFixture[str], target: str, mode: int
) -> None:
    (world.outputs if target == "parent" else world.root).chmod(mode)
    stage = "untrusted_path"
    _assert_refused_before_publish(world, world.argv(), capsys, stage)


def _fake_stat(
    monkeypatch: pytest.MonkeyPatch, changes: dict[tuple[int, int], dict[str, int]]
) -> None:
    real = os.fstat

    def fstat(fd: int) -> os.stat_result:
        result = real(fd)
        change = changes.get((result.st_dev, result.st_ino))
        if change is None:
            return result
        values = list(result[:10])
        if "uid" in change:
            values[4] = change["uid"]
        if "mode" in change:
            values[0] = change["mode"]
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", fstat)


def _key(path: Path) -> tuple[int, int]:
    result = path.stat()
    return (result.st_dev, result.st_ino)


OTHER_UID = os.geteuid() + 4242


@pytest.mark.parametrize(
    "case",
    [
        "parent_owned_by_other",
        "ancestor_owned_by_other",
        "sticky_owned_by_other",
        "sticky_child_owned_by_other",
    ],
)
def test_ancestors_owned_by_other_users_are_refused(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    sticky = world.root / "sticky"
    sticky.mkdir()
    sticky.chmod(0o1777)
    child = sticky / "mine"
    child.mkdir(mode=0o755)
    child.chmod(0o755)
    out = child / "run"
    changes = {
        "parent_owned_by_other": {_key(world.outputs): {"uid": OTHER_UID}},
        "ancestor_owned_by_other": {_key(world.root): {"uid": OTHER_UID}},
        "sticky_owned_by_other": {_key(sticky): {"uid": OTHER_UID}},
        "sticky_child_owned_by_other": {_key(child): {"uid": OTHER_UID}},
    }[case]
    _fake_stat(monkeypatch, changes)
    target = (
        world.outputs / "run" if case.startswith("parent") or case.startswith("ancestor") else out
    )
    capsys.readouterr()
    assert train.main(world.argv(target)) == 1
    stages = stages_of(capsys)
    assert stages in (["untrusted_path"], ["before_publish"])
    if case != "parent_owned_by_other":
        assert stages == ["untrusted_path"]
    assert not target.exists()


def test_a_sticky_ancestor_owned_by_the_runner_is_accepted(world: World) -> None:
    sticky = world.root / "sticky"
    sticky.mkdir()
    sticky.chmod(0o1777)
    child = sticky / "mine"
    child.mkdir()
    child.chmod(0o755)
    assert train.main(world.argv(child / "run")) == 0


def test_an_ancestor_symlink_is_refused_with_a_hint(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    link = world.root / "link"
    link.symlink_to(world.outputs)
    capsys.readouterr()
    assert train.main(world.argv(link / "run")) == 1
    [error] = train_errors(capsys)
    assert error["stage"] == "untrusted_path"
    assert error["symlink"] == str(link) and "実の path" in error["msg"]
    assert not (world.outputs / "run").exists()


def test_a_posix_acl_on_an_ancestor_is_refused(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root_key = _key(world.root)
    real = getattr(os, "listxattr", None)

    def listxattr(fd: int) -> list[str]:
        if _key_fd(fd) == root_key:
            return ["system.posix_acl_default"]
        return [] if real is None else list(real(fd))

    monkeypatch.setattr(os, "listxattr", listxattr, raising=False)
    _assert_refused_before_publish(world, world.argv(), capsys, "untrusted_path")


def _key_fd(fd: int) -> tuple[int, int]:
    result = os.fstat(fd)
    return (result.st_dev, result.st_ino)


def test_without_listxattr_only_the_mode_rules_apply(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "listxattr", raising=False)
    assert train.main(world.argv()) == 0


def test_loosening_an_ancestor_before_publish_leaves_an_empty_out(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir, rmdirs = os.mkdir, []

    def mkdir_then_loosen(path: Any, *args: Any, **kwargs: Any) -> None:
        real_mkdir(path, *args, **kwargs)
        if str(path) == "run":
            world.root.chmod(0o777)

    monkeypatch.setattr(os, "mkdir", mkdir_then_loosen)
    real_rmdir = os.rmdir

    def rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path) == "run":
            rmdirs.append(path)
        real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", rmdir)
    capsys.readouterr()
    assert train.main(world.argv()) == 1
    [error] = train_errors(capsys)
    assert error["stage"] == "untrusted_path"
    assert rmdirs == []
    assert (world.outputs / "run").is_dir() and not list((world.outputs / "run").iterdir())


# ---------------------------------------------------------------- 公開の手順


def test_a_failing_link_is_cleaned_up(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def link(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.EIO, "link failed")

    monkeypatch.setattr(os, "link", link)
    _assert_refused_before_publish(world, world.argv(), capsys)


@pytest.mark.parametrize("at", ["artifact.json", "metadata.json", "marker"])
def test_a_failure_while_writing_is_cleaned_up(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, at: str
) -> None:
    real = training_output._write_new_file

    def failing(directory_fd: int, name: str, payload: bytes) -> None:
        if name == at or (at == "marker" and name.startswith(".COMPLETE.json.tmp-")):
            real(directory_fd, name, payload[: len(payload) // 2])
            raise OSError(errno.ENOSPC, "no space")
        real(directory_fd, name, payload)

    monkeypatch.setattr(training_output, "_write_new_file", failing)
    _assert_refused_before_publish(world, world.argv(), capsys)


def test_complete_is_not_visible_before_the_link(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real, seen = os.link, []

    def checking_link(src: Any, dst: Any, **kwargs: Any) -> None:
        with pytest.raises(training_output.TrainingOutputIncomplete):
            training_output.read_training_output(
                kwargs["dst_dir_fd"], max_artifact_bytes=10**7, max_other_bytes=10**6
            )
        seen.append(dst)
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", checking_link)
    assert train.main(world.argv()) == 0
    assert seen == ["COMPLETE.json"]


def test_a_foreign_complete_makes_the_link_fail_and_cleanup_report_it(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.link

    def another_process_first(src: Any, dst: Any, **kwargs: Any) -> None:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=kwargs["dst_dir_fd"])
        os.write(fd, b"{}")
        os.close(fd)
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", another_process_first)
    capsys.readouterr()
    assert train.main(world.argv()) == 1
    [error] = train_errors(capsys)
    assert error["stage"] == "cleanup_failed"
    assert error["remaining_names"] == ["COMPLETE.json"]
    assert error["cleanup_errors"][0]["errno"] == errno.ENOTEMPTY
    out = world.outputs / "run"
    assert sorted(p.name for p in out.iterdir()) == ["COMPLETE.json"]
    assert (out / "COMPLETE.json").read_bytes() == b"{}"


def test_a_failing_unlink_in_cleanup_is_reported(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_unlink = os.unlink

    def link(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.EIO, "link failed")

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path) == "artifact.json":
            raise OSError(errno.EACCES, "denied")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "unlink", unlink)
    capsys.readouterr()
    assert train.main(world.argv()) == 1
    [error] = train_errors(capsys)
    assert error["stage"] == "cleanup_failed"
    assert "artifact.json" in error["remaining_names"]


def test_rename_and_replace_are_not_used_to_publish(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("os.rename / os.replace で公開しない")

    monkeypatch.setattr(os, "rename", forbidden)
    monkeypatch.setattr(os, "replace", forbidden)
    assert train.main(world.argv()) == 0


class Killed(BaseException):
    """強制終了の模擬（後始末を走らせない）。"""


@pytest.mark.parametrize("after", ["files", "marker"])
def test_a_killed_run_leaves_an_invalid_out_and_rerun_refuses(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    after: str,
) -> None:
    real = training_output._write_new_file

    def killing(directory_fd: int, name: str, payload: bytes) -> None:
        if after == "files" and name == "metadata.json":
            raise Killed
        real(directory_fd, name, payload)
        if after == "marker" and name.startswith(".COMPLETE.json.tmp-"):
            raise Killed

    with monkeypatch.context() as patch:
        patch.setattr(training_output, "_write_new_file", killing)
        with pytest.raises(Killed):
            train.main(world.argv())
    out = world.outputs / "run"
    fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(training_output.TrainingOutputIncomplete):
            training_output.read_training_output(
                fd, max_artifact_bytes=10**7, max_other_bytes=10**6
            )
    finally:
        os.close(fd)
    capsys.readouterr()
    assert train.main(world.argv()) == 1
    [error] = train_errors(capsys)
    assert error["out_exists"] is True and error["complete_marker_valid"] is False


@pytest.mark.parametrize("failure", ["unlink", "fsync"])
@pytest.mark.parametrize("apply", [False, True])
def test_failures_after_the_link_keep_the_out(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    apply: bool,
) -> None:
    real_unlink, real_fsync = os.unlink, os.fsync

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path).startswith(".COMPLETE.json.tmp-"):
            raise OSError(errno.EIO, "unlink failed")
        real_unlink(path, *args, **kwargs)

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode) and "COMPLETE.json" in os.listdir(fd):
            raise OSError(errno.EIO, "fsync failed")
        real_fsync(fd)

    if failure == "unlink":
        monkeypatch.setattr(os, "unlink", unlink)
    else:
        monkeypatch.setattr(os, "fsync", fsync)
    state = registry_state(world)
    capsys.readouterr()
    assert train.main(world.apply_argv() if apply else world.argv()) == 1
    assert stages_of(capsys) == ["publish_durability"]
    out = world.outputs / "run"
    fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        training_output.read_training_output(fd, max_artifact_bytes=10**7, max_other_bytes=10**6)
    finally:
        os.close(fd)
    assert registry_state(world) == state


# ---------------------------------------------------------------- 名前の差し替えと中身の再検証


def test_replacing_the_out_name_before_the_link_is_detected(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = world.outputs / "run"
    moved = world.outputs / "moved"
    real = training_output._write_new_file

    def replace_after_marker(directory_fd: int, name: str, payload: bytes) -> None:
        real(directory_fd, name, payload)
        if name.startswith(".COMPLETE.json.tmp-"):
            out.rename(moved)
            out.mkdir(mode=0o700)
            (out / "theirs").write_bytes(b"x")

    monkeypatch.setattr(training_output, "_write_new_file", replace_after_marker)
    capsys.readouterr()
    assert train.main(world.argv()) == 1
    [error] = train_errors(capsys)
    assert error["stage"] == "out_replaced"
    assert error["actual_identity"] == list(_key(out))
    assert list(moved.iterdir()) == []
    assert [p.name for p in out.iterdir()] == ["theirs"]


@pytest.mark.parametrize("how", ["rename_out", "rename_parent", "symlink_parent"])
def test_replacing_a_name_after_publish_is_not_registered(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    real = train.publish_training_output

    def then_replace(*args: Any) -> training_output.PublishedTrainingOutput:
        published = real(*args)
        if how == "rename_out":
            (world.outputs / "run").rename(world.outputs / "old")
            (world.outputs / "run").mkdir(mode=0o700)
        else:
            world.outputs.rename(world.root / "outputs-old")
            if how == "rename_parent":
                world.outputs.mkdir(mode=0o755)
                (world.outputs / "run").mkdir(mode=0o700)
            else:
                world.outputs.symlink_to(world.root / "outputs-old")
        return published

    monkeypatch.setattr(train, "publish_training_output", then_replace)
    state = registry_state(world)
    capsys.readouterr()
    assert train.main(world.apply_argv()) == 1
    assert stages_of(capsys) == ["out_replaced"]
    assert registry_state(world) == state


def test_the_path_check_does_not_use_lstat(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("path の照合に os.lstat を使わない")

    real = train.publish_training_output

    def without_lstat(*args: Any) -> training_output.PublishedTrainingOutput:
        with monkeypatch.context() as patch:
            patch.setattr(os, "lstat", forbidden)
            published = real(*args)
            check = training_output.path_names_directory(published.path, published.identity)
            assert check.matches
            return published

    monkeypatch.setattr(train, "publish_training_output", without_lstat)
    assert train.main(world.argv()) == 0


@pytest.mark.parametrize("apply", [False, True])
def test_modifying_the_out_after_publish_is_reported(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    apply: bool,
) -> None:
    real = train.publish_training_output

    def then_modify(*args: Any) -> training_output.PublishedTrainingOutput:
        published = real(*args)
        artifact = world.outputs / "run" / "artifact.json"
        artifact.write_bytes(artifact.read_bytes().replace(b"0.1.0", b"0.1.1", 1))
        return published

    monkeypatch.setattr(train, "publish_training_output", then_modify)
    state = registry_state(world)
    capsys.readouterr()
    assert train.main(world.apply_argv() if apply else world.argv()) == 1
    assert stages_of(capsys) == ["out_modified"]
    assert registry_state(world) == state


def test_the_registered_bytes_are_the_verified_bytes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    returned: list[bytes] = []
    registered: list[bytes] = []
    real_read = train.read_training_output
    real_register = ModelRegistry.register_candidate

    def recording_read(*args: Any, **kwargs: Any) -> training_output.VerifiedTrainingOutput:
        verified = real_read(*args, **kwargs)
        returned.append(verified.artifact)
        return verified

    def recording_register(self: ModelRegistry, metadata: Any, payload: bytes, **kw: Any) -> Any:
        registered.append(payload)
        return real_register(self, metadata, payload, **kw)

    monkeypatch.setattr(train, "read_training_output", recording_read)
    monkeypatch.setattr(ModelRegistry, "register_candidate", recording_register)
    assert train.main(world.apply_argv()) == 0
    assert len(registered) == 1 and any(item is registered[0] for item in returned)


@pytest.mark.parametrize("change", ["modify", "rename_parent", "both"])
def test_changes_right_after_register_are_reported_without_retire(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    real = ModelRegistry.register_candidate

    def then_change(self: ModelRegistry, *args: Any, **kwargs: Any) -> Any:
        ref = real(self, *args, **kwargs)
        if change in ("modify", "both"):
            artifact = world.outputs / "run" / "artifact.json"
            data = artifact.read_bytes()
            with artifact.open("r+b") as handle:  # inode を変えずにその場で書き換える
                handle.write(data[:1] + b" " + data[2:])
        if change in ("rename_parent", "both"):
            world.outputs.rename(world.root / "outputs-old")
            world.outputs.mkdir(mode=0o755)
            (world.outputs / "run").mkdir(mode=0o700)
        return ref

    ModelRegistry(world.registry, limits=limits())
    before = snapshot(world)
    monkeypatch.setattr(ModelRegistry, "register_candidate", then_change)
    capsys.readouterr()
    assert train.main(world.apply_argv()) == 1
    stages = stages_of(capsys)
    expected = {
        "modify": ["out_modified_after_register"],
        "rename_parent": ["out_replaced_after_register"],
        "both": ["out_replaced_after_register", "out_modified_after_register"],
    }[change]
    assert stages == expected
    out = world.root / "outputs-old" / "run" if change != "modify" else world.outputs / "run"
    after = snapshot(world)
    assert after.revision == before.revision + 1
    assert [event.event.value for event in after.audit] == ["registered"]
    del out


# ---------------------------------------------------------------- 登録の永続の2つの意味


def _durability_on(name_test: Callable[[str], bool], monkeypatch: pytest.MonkeyPatch) -> None:
    real = ModelRegistry._atomic_write

    def atomic_write(directory_fd: int, name: str, payload: bytes) -> None:
        real(directory_fd, name, payload)
        if name_test(name):
            raise RegistryDurabilityError(f"fsync できない（試験）: {name}")

    monkeypatch.setattr(ModelRegistry, "_atomic_write", staticmethod(atomic_write))


def test_durability_after_the_snapshot_replace_is_register_durability(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ModelRegistry(world.registry, limits=limits())
    before = snapshot(world)
    _durability_on(lambda name: name == "registry.json", monkeypatch)
    capsys.readouterr()
    assert train.main(world.apply_argv()) == 1
    assert stages_of(capsys) == ["register_durability"]
    monkeypatch.undo()
    assert_one_candidate_added(before, snapshot(world), world.outputs / "run")


def test_durability_after_retire_by_another_cli_is_still_register_durability(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ModelRegistry(world.registry, limits=limits())
    _durability_on(lambda name: name == "registry.json", monkeypatch)
    real = ModelRegistry.register_candidate

    def then_retire(self: ModelRegistry, *args: Any, **kwargs: Any) -> Any:
        try:
            return real(self, *args, **kwargs)
        except RegistryDurabilityError as error:
            other = ModelRegistry(world.registry, limits=limits())
            ref = error.ref
            revision = other.inspect().revision
            with monkeypatch.context() as patch:
                patch.setattr(ModelRegistry, "_atomic_write", staticmethod(_real_atomic_write))
                other.retire(ref, actor="other", reason="r", expected_revision=revision)
            raise

    monkeypatch.setattr(ModelRegistry, "register_candidate", then_retire)
    capsys.readouterr()
    assert train.main(world.apply_argv()) == 1
    stages = stages_of(capsys)
    assert stages[0] == "register_durability"


_real_atomic_write = ModelRegistry._atomic_write


def test_durability_in_the_artifact_stage_is_register(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ModelRegistry(world.registry, limits=limits())
    state = registry_state(world)
    _durability_on(lambda name: name != "registry.json", monkeypatch)
    capsys.readouterr()
    assert train.main(world.apply_argv()) == 1
    assert stages_of(capsys) == ["register"]
    assert registry_state(world) == state


def test_a_failing_post_register_check_keeps_the_candidate(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ModelRegistry(world.registry, limits=limits())
    before = snapshot(world)

    def rejected(*_a: Any, **_k: Any) -> None:
        raise ValueError("L9（試験）")

    monkeypatch.setattr(
        train.RegistryCounterfactualThermalModel, "from_verified_artifact", rejected
    )
    capsys.readouterr()
    assert train.main(world.apply_argv()) == 1
    assert stages_of(capsys) == ["post_register_check"]
    after = snapshot(world)
    assert_one_candidate_added(before, after, world.outputs / "run")
    assert all(event.event.value != "retired" for event in after.audit)
