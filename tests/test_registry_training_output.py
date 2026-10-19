"""``coldaisle-registry register --training-output`` と、``--metadata`` / ``--payload`` の拒否。

決定記録 0150 §2.5 / §2.9。印を確かめたときに読んだ bytes そのものを登録し、登録の後に path の
照合と中身の再検証をもう一度行う。確定の後の ``RegistryDurabilityError`` は例外の型で分ける。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from coldaisle import registry, train, training_output
from coldaisle.control.model_registry import (
    ArtifactMetadata,
    ArtifactRef,
    ModelRegistry,
    RegistryCommitNotDurableError,
    RegistryDurabilityError,
)
from coldaisle.store import QualityRules
from test_train_cli import (
    assert_one_candidate_added,
    files_of,
    log_lines,
    registry_state,
    snapshot,
)
from train_world import CONFIG, World, limits, make_world


@pytest.fixture
def world(tmp_path: Path, rules: QualityRules) -> World:
    return make_world(tmp_path / "w", rules)


@pytest.fixture
def out(world: World) -> Path:
    path = world.outputs / "run"
    assert train.main(world.argv(path)) == 0
    return path


def register(world: World, *extra: str, limits_dir: Path = CONFIG) -> int:
    return registry.main(
        [
            "register",
            "--root",
            str(world.registry),
            "--limits",
            str(limits_dir),
            *extra,
            "--actor",
            "trainer",
            "--reason",
            "training completed",
        ]
    )


def registry_errors(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [
        line
        for line in log_lines(capsys.readouterr().err)
        if line["logger"] == "coldaisle.registry" and line["level"] == "error"
    ]


# ---------------------------------------------------------------- 登録できる


def test_a_valid_output_is_registered_as_a_candidate(world: World, out: Path) -> None:
    ModelRegistry(world.registry, limits=limits())
    before = snapshot(world)
    contents = files_of(out)
    assert register(world, "--training-output", str(out)) == 0
    assert_one_candidate_added(before, snapshot(world), out)
    assert files_of(out) == contents


def test_the_registered_bytes_are_the_verified_bytes(
    world: World, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = (out / "artifact.json").read_bytes()
    real = ModelRegistry.register_candidate

    def replace_then_register(self: ModelRegistry, *args: Any, **kwargs: Any) -> Any:
        (out / "artifact.json").unlink()
        (out / "artifact.json").write_bytes(b"{}")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ModelRegistry, "register_candidate", replace_then_register)
    assert register(world, "--training-output", str(out)) == 1  # 登録の後の再検証で外れる
    stored = list((world.registry / "artifacts").rglob("artifact.payload"))
    assert [path.read_bytes() for path in stored] == [original]


# ---------------------------------------------------------------- 拒否（Registry を開かない）


@pytest.mark.parametrize("damage", ["no_marker", "partial_marker", "sha", "temp_only"])
def test_an_incomplete_output_is_refused(
    world: World, out: Path, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    marker = out / "COMPLETE.json"
    if damage == "no_marker":
        marker.unlink()
    elif damage == "partial_marker":
        marker.write_bytes(marker.read_bytes()[:20])
    elif damage == "sha":
        artifact = out / "artifact.json"
        data = artifact.read_bytes()
        artifact.write_bytes(data[:1] + b" " + data[2:])
    else:
        marker.rename(out / (".COMPLETE.json.tmp-" + "0" * 32))
    capsys.readouterr()
    assert register(world, "--training-output", str(out)) == 1
    assert not world.registry.exists()


def test_training_output_and_metadata_are_exclusive(world: World, out: Path) -> None:
    assert (
        register(
            world,
            "--training-output",
            str(out),
            "--metadata",
            str(out / "metadata.json"),
            "--payload",
            str(out / "artifact.json"),
        )
        == 1
    )
    assert not world.registry.exists()


def _small_limits(tmp: Path, *, artifact: int, other: int) -> Path:
    directory = tmp / "small-limits"
    directory.mkdir()
    text = (CONFIG / "model-registry.yaml").read_text(encoding="utf-8")
    text = text.replace("max_artifact_bytes: 8388608", f"max_artifact_bytes: {artifact}")
    text = text.replace("max_snapshot_bytes: 4194304", f"max_snapshot_bytes: {other}")
    (directory / "model-registry.yaml").write_text(text, encoding="utf-8")
    return directory


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, int], dict[str, int]]:
    names: dict[int, str] = {}
    opened: dict[str, int] = {}
    read: dict[str, int] = {}
    real_open, real_read = os.open, os.read

    def recording_open(path: Any, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, *args, **kwargs)
        names[fd] = os.path.basename(str(path))
        opened[names[fd]] = opened.get(names[fd], 0) + 1
        return fd

    def counting_read(fd: int, size: int) -> bytes:
        data = real_read(fd, size)
        if fd in names:
            read[names[fd]] = read.get(names[fd], 0) + len(data)
        return data

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "read", counting_read)
    return opened, read


_NAMES = ["COMPLETE.json", "metadata.json", "training-report.json", "artifact.json"]


@pytest.mark.parametrize("name", _NAMES)
def test_files_past_the_bounds_are_refused_without_opening_or_reading_past_them(
    world: World, out: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """上限は ``--limits`` の値（artifact は max_artifact_bytes・他は max_snapshot_bytes）。"""
    files = files_of(out)
    artifact_limit = len(files["artifact.json"])
    other_limit = max(len(data) for key, data in files.items() if key != "artifact.json")
    if name == "artifact.json":
        artifact_limit = len(files[name]) - 1
    else:
        other_limit = len(files[name]) - 1
    limits_dir = _small_limits(world.root, artifact=artifact_limit, other=other_limit)
    opened, read = _count_reads(monkeypatch)
    assert register(world, "--training-output", str(out), limits_dir=limits_dir) == 1
    monkeypatch.undo()
    assert not world.registry.exists()
    bound = (artifact_limit if name == "artifact.json" else other_limit) + 1
    assert read.get(name, 0) <= bound
    if name != "COMPLETE.json":
        assert opened.get(name, 0) == 0, "印の大きさが上限を超えるファイルは開かない"


@pytest.mark.parametrize("name", _NAMES[1:])
def test_a_file_larger_than_its_marker_is_refused_without_reading_past_the_marker(
    world: World, out: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    files = files_of(out)
    (out / name).write_bytes(files[name] + b" " * 100_000)
    _opened, read = _count_reads(monkeypatch)
    assert register(world, "--training-output", str(out)) == 1
    monkeypatch.undo()
    assert not world.registry.exists()
    assert read.get(name, 0) <= len(files[name]) + 1


@pytest.mark.parametrize(
    "names",
    [
        ["/etc/passwd", "metadata.json", "training-report.json"],
        ["../artifact.json", "metadata.json", "training-report.json"],
        ["sub/artifact.json", "metadata.json", "training-report.json"],
        ["artifact.json", "artifact.json", "training-report.json"],
        ["artifact.json", "metadata.json"],
        ["artifact.json", "metadata.json", "training-report.json", "extra.json"],
    ],
)
def test_marker_names_other_than_the_fixed_three_open_nothing(
    world: World, out: Path, monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> None:
    marker = json.loads((out / "COMPLETE.json").read_bytes())
    template = marker["files"][0]
    marker["files"] = [{**template, "name": name} for name in names]
    (out / "COMPLETE.json").write_text(json.dumps(marker), encoding="utf-8")
    opened, _read = _count_reads(monkeypatch)
    assert register(world, "--training-output", str(out)) == 1
    monkeypatch.undo()
    assert set(opened) <= {"COMPLETE.json", "model-registry.yaml", "", *_path_parts(out)}
    assert not world.registry.exists()


def _path_parts(path: Path) -> set[str]:
    return set(path.parts)


@pytest.mark.parametrize("name", ["COMPLETE.json", "artifact.json"])
@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_special_files_are_refused_without_blocking(
    world: World, out: Path, name: str, kind: str
) -> None:
    target = out / name
    target.unlink()
    if kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    assert register(world, "--training-output", str(out)) == 1
    assert not world.registry.exists()


def test_renaming_the_parent_after_the_check_is_refused_before_register(
    world: World, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = training_output.read_training_output
    calls: list[int] = []

    def then_rename(*args: Any, **kwargs: Any) -> training_output.VerifiedTrainingOutput:
        verified = real(*args, **kwargs)
        if not calls:
            world.outputs.rename(world.root / "outputs-old")
            world.outputs.mkdir(mode=0o755)
            (world.outputs / "run").mkdir(mode=0o700)
        calls.append(1)
        return verified

    monkeypatch.setattr(training_output, "read_training_output", then_rename)
    assert register(world, "--training-output", str(out)) == 1
    assert not world.registry.exists()


@pytest.mark.parametrize("change", ["rename_parent", "modify", "both"])
def test_changes_right_after_register_are_reported(
    world: World,
    out: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    ModelRegistry(world.registry, limits=limits())
    before = snapshot(world)
    original = files_of(out)
    real = ModelRegistry.register_candidate

    def then_change(self: ModelRegistry, *args: Any, **kwargs: Any) -> Any:
        ref = real(self, *args, **kwargs)
        if change in ("modify", "both"):
            data = (out / "artifact.json").read_bytes()
            with (out / "artifact.json").open("r+b") as handle:
                handle.write(data[:1] + b" " + data[2:])
        if change in ("rename_parent", "both"):
            world.outputs.rename(world.root / "outputs-old")
            world.outputs.mkdir(mode=0o755)
            (world.outputs / "run").mkdir(mode=0o700)
        return ref

    monkeypatch.setattr(ModelRegistry, "register_candidate", then_change)
    capsys.readouterr()
    assert register(world, "--training-output", str(out)) == 1
    [error] = registry_errors(capsys)
    expected = {
        "rename_parent": ["out_replaced_after_register"],
        "modify": ["out_modified_after_register"],
        "both": ["out_replaced_after_register", "out_modified_after_register"],
    }[change]
    assert error["stages"] == expected
    assert error["artifact"] == "thermal_model/rack-thermal/0.1.0"
    monkeypatch.undo()
    after = snapshot(world)
    assert after.revision == before.revision + 1
    assert all(event.event.value != "retired" for event in after.audit)
    stored = next((world.registry / "artifacts").rglob("artifact.payload")).read_bytes()
    assert stored == original["artifact.json"]


def _durable_failure_on(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    real = ModelRegistry._atomic_write

    def atomic_write(directory_fd: int, target: str, payload: bytes) -> None:
        real(directory_fd, target, payload)
        if (target == "registry.json") == (name == "registry.json"):
            raise RegistryDurabilityError(f"fsync できない（試験）: {target}")

    monkeypatch.setattr(ModelRegistry, "_atomic_write", staticmethod(atomic_write))


@pytest.mark.parametrize("retire_first", [False, True])
@pytest.mark.parametrize("also", [None, "modify"])
def test_a_commit_that_is_not_durable_is_register_durability(
    world: World,
    out: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    retire_first: bool,
    also: str | None,
) -> None:
    ModelRegistry(world.registry, limits=limits())
    real_write = ModelRegistry._atomic_write
    _durable_failure_on("registry.json", monkeypatch)
    real = ModelRegistry.register_candidate

    def wrapped(self: ModelRegistry, *args: Any, **kwargs: Any) -> Any:
        try:
            return real(self, *args, **kwargs)
        except RegistryCommitNotDurableError as error:
            if retire_first:
                other = ModelRegistry(world.registry, limits=limits())
                with monkeypatch.context() as patch:
                    patch.setattr(ModelRegistry, "_atomic_write", staticmethod(real_write))
                    revision = other.inspect().revision
                    other.retire(error.ref, actor="other", reason="r", expected_revision=revision)
            if also == "modify":
                data = (out / "artifact.json").read_bytes()
                with (out / "artifact.json").open("r+b") as handle:
                    handle.write(data[:1] + b" " + data[2:])
            raise

    monkeypatch.setattr(ModelRegistry, "register_candidate", wrapped)
    capsys.readouterr()
    assert register(world, "--training-output", str(out)) == 1
    [error] = registry_errors(capsys)
    expected = ["register_durability"] + (["out_modified_after_register"] if also else [])
    assert error["stages"] == expected
    assert error["msg"] != "Model Registry の操作に失敗した"


def test_an_artifact_stage_durability_error_is_a_plain_failure(
    world: World,
    out: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ModelRegistry(world.registry, limits=limits())
    state = registry_state(world)
    _durable_failure_on("artifact.payload", monkeypatch)
    capsys.readouterr()
    assert register(world, "--training-output", str(out)) == 1
    [error] = registry_errors(capsys)
    assert error["msg"] == "Model Registry の操作に失敗した"
    assert registry_state(world) == state


# ---------------------------------------------------------------- 例外の型（model_registry）


def test_register_candidate_raises_commit_not_durable_only_after_the_snapshot(
    world: World, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = ArtifactMetadata.model_validate_json((out / "metadata.json").read_bytes())
    payload = (out / "artifact.json").read_bytes()
    target = ModelRegistry(world.registry, limits=limits())
    _durable_failure_on("artifact.payload", monkeypatch)
    with pytest.raises(RegistryDurabilityError) as caught:
        target.register_candidate(metadata, payload, actor="trainer", reason="r")
    assert type(caught.value) is RegistryDurabilityError
    # lock を放した後に、別の呼び出しが同じ ref・同じ sha256 を登録しても型は変わらない
    monkeypatch.undo()
    target.register_candidate(metadata, payload, actor="trainer", reason="r")
    second = ModelRegistry(world.root / "second", limits=limits())
    _durable_failure_on("registry.json", monkeypatch)
    with pytest.raises(RegistryCommitNotDurableError) as committed:
        second.register_candidate(metadata, payload, actor="trainer", reason="r")
    assert committed.value.ref == ArtifactRef(
        kind=metadata.kind, model_id=metadata.model_id, version=metadata.version
    )


# ---------------------------------------------------------------- --metadata / --payload の拒否


@pytest.mark.parametrize("where", ["out", "copy", "symlink", "hardlink", "before_marker"])
def test_counterfactual_metadata_is_refused_on_the_legacy_path(
    world: World, out: Path, where: str
) -> None:
    metadata, payload = out / "metadata.json", out / "artifact.json"
    elsewhere = world.root / "elsewhere"
    elsewhere.mkdir()
    if where == "copy":
        shutil.copy(metadata, elsewhere / "m.json")
        shutil.copy(payload, elsewhere / "p.json")
        metadata, payload = elsewhere / "m.json", elsewhere / "p.json"
    elif where == "symlink":
        (elsewhere / "m.json").symlink_to(metadata)
        (elsewhere / "p.json").symlink_to(payload)
        metadata, payload = elsewhere / "m.json", elsewhere / "p.json"
    elif where == "hardlink":
        os.link(metadata, elsewhere / "m.json")
        os.link(payload, elsewhere / "p.json")
        metadata, payload = elsewhere / "m.json", elsewhere / "p.json"
    elif where == "before_marker":
        (out / "COMPLETE.json").unlink()
        (out / "training-report.json").unlink()
    assert register(world, "--metadata", str(metadata), "--payload", str(payload)) == 1
    assert not world.registry.exists()


def test_training_output_registers_only_counterfactual_thermal_models(
    world: World, out: Path
) -> None:
    metadata = json.loads((out / "metadata.json").read_bytes())
    metadata["capability"] = "observational_replay"
    payload = (json.dumps(metadata, sort_keys=True, separators=(",", ":"))).encode()
    (out / "metadata.json").write_bytes(payload)
    files = files_of(out)
    marker = training_output.completion_marker_bytes(
        {name: files[name] for name in training_output.OUTPUT_FILENAMES}
    )
    (out / "COMPLETE.json").write_bytes(marker)
    assert register(world, "--training-output", str(out)) == 1
    assert not world.registry.exists()


def test_an_os_error_in_the_path_check_after_register_is_reported(
    world: World, out: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_register, real_open = ModelRegistry.register_candidate, os.open
    broken: list[bool] = []

    def failing_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if broken and str(path) == "outputs":
            raise OSError(5, "I/O error")
        return real_open(path, *args, **kwargs)

    def then_break(self: ModelRegistry, *args: Any, **kwargs: Any) -> Any:
        ref = real_register(self, *args, **kwargs)
        broken.append(True)
        return ref

    monkeypatch.setattr(os, "open", failing_open)
    monkeypatch.setattr(ModelRegistry, "register_candidate", then_break)
    capsys.readouterr()
    assert register(world, "--training-output", str(out)) == 1
    [error] = registry_errors(capsys)
    assert error["stages"] == ["out_replaced_after_register"]
    assert error["artifact"] == "thermal_model/rack-thermal/0.1.0"
