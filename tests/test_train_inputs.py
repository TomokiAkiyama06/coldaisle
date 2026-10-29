"""``coldaisle-train`` の入力の部品と import の約束（決定記録 0150 §2.1 / §2.4 の 1 / §2.7）。

- ``load_training_dataset_artifact_v2``: 1回の読みで公開物へ束縛する
- ``MetricCatalog.from_yaml_bytes_with_sha256`` / ``parse_declared_changes``: 既存の入口と同じ結果
- import の走査（直接の import だけ。推移的な import は約束しない）
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from coldaisle import train
from coldaisle.control.model import counterfactual_training
from coldaisle.control.model.counterfactual_training import (
    load_training_dataset_artifact_v2,
    verify_training_dataset_artifact_v2,
)
from coldaisle.control.model.dataset import DatasetExampleV2, DatasetManifestV2
from coldaisle.declared_changes import (
    DeclaredChangesError,
    parse_declared_changes,
    read_declared_changes,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store import QualityRules
from train_world import CONFIG, World, make_world

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"


@pytest.fixture
def world(tmp_path: Path, rules: QualityRules) -> World:
    return make_world(tmp_path / "w", rules)


# ---------------------------------------------------------------- Dataset の1回の読み


def test_the_loader_returns_the_same_binding_as_verify(world: World) -> None:
    loaded = load_training_dataset_artifact_v2(world.dataset)
    verified = verify_training_dataset_artifact_v2(
        loaded.dataset, world.dataset / "manifest.json", world.dataset / "examples.jsonl"
    )
    assert (loaded.artifact_id, loaded.manifest_sha256, loaded.examples_sha256) == (
        verified.artifact_id,
        verified.manifest_sha256,
        verified.examples_sha256,
    )


def _rewrite_examples(world: World, edit: Any) -> None:
    """examples を書き換え、manifest の ``examples_sha256`` を合わせる（manifest は canonical）。"""
    examples = world.dataset / "examples.jsonl"
    payload = edit(examples.read_bytes())
    examples.write_bytes(payload)
    manifest_path = world.dataset / "manifest.json"
    manifest = DatasetManifestV2.model_validate_json(manifest_path.read_bytes())
    manifest = DatasetManifestV2.model_validate(
        {
            **manifest.model_dump(mode="python"),
            "examples_sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    manifest_path.write_bytes((manifest.model_dump_json(indent=2) + "\n").encode("utf-8"))


def _non_canonical(payload: bytes) -> bytes:
    first, rest = payload.split(b"\n", 1)
    return json.dumps(json.loads(first), indent=1).replace("\n", " ").encode() + b"\n" + rest


def _invalid_row(payload: bytes) -> bytes:
    first, rest = payload.split(b"\n", 1)
    raw = json.loads(first)
    raw["label_end_ms"] = "late"
    return json.dumps(raw, separators=(",", ":")).encode() + b"\n" + rest


@pytest.mark.parametrize(
    "damage",
    [
        "oversize",
        "checksum",
        "non_canonical",
        "invalid_row",
        "alias",
        "dir_symlink",
        "file_symlink",
    ],
)
def test_the_loader_refuses_damaged_artifacts(
    world: World, monkeypatch: pytest.MonkeyPatch, damage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    target = world.dataset
    if damage == "oversize":
        size = (world.dataset / "examples.jsonl").stat().st_size
        monkeypatch.setattr(counterfactual_training, "MAX_DATASET_EXAMPLES_BYTES", size - 1)
    elif damage == "checksum":
        examples = world.dataset / "examples.jsonl"
        examples.write_bytes(examples.read_bytes() + b"\n")
    elif damage == "non_canonical":
        _rewrite_examples(world, _non_canonical)
    elif damage == "invalid_row":
        _rewrite_examples(world, _invalid_row)
    elif damage == "alias":
        target = world.dataset.parent / "not-an-alias"
        shutil.copytree(world.dataset, target)
    elif damage == "dir_symlink":
        target = world.root / ("dataset-" + "b" * 32)
        target.symlink_to(world.dataset)
    else:
        examples = world.dataset / "examples.jsonl"
        moved = world.root / "examples.jsonl"
        examples.rename(moved)
        examples.symlink_to(moved)
    with pytest.raises((ValueError, OSError)):
        load_training_dataset_artifact_v2(target)
    capsys.readouterr()
    assert train.main(world.argv(**{"--dataset": str(target)})) == 1
    assert not (world.outputs / "run").exists()


def test_each_dataset_file_is_opened_once_and_the_first_bytes_are_used(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = (world.dataset / "examples.jsonl").read_bytes()
    opened: list[str] = []
    real_open, real_read = os.open, os.read
    names: dict[int, str] = {}

    def recording_open(path: Any, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, *args, **kwargs)
        names[fd] = str(path)
        if str(path) in ("manifest.json", "examples.jsonl"):
            opened.append(str(path))
        return fd

    def read_then_replace(fd: int, size: int) -> bytes:
        data = real_read(fd, size)
        if names.get(fd) == "examples.jsonl" and not data:
            # 読み終えた後に差し替えても、使った bytes は1回目のもの
            (world.dataset / "examples.jsonl").write_bytes(b"{}\n")
        return data

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "read", read_then_replace)
    loaded = load_training_dataset_artifact_v2(world.dataset)
    monkeypatch.undo()
    assert sorted(opened) == ["examples.jsonl", "manifest.json"]
    assert loaded.examples_sha256 == hashlib.sha256(original).hexdigest()
    assert len(loaded.dataset.examples) == original.count(b"\n")
    assert all(isinstance(item, DatasetExampleV2) for item in loaded.dataset.examples)


# ---------------------------------------------------------------- bytes の入口と path の入口


def test_metric_catalog_bytes_api_matches_the_path_api() -> None:
    path = CONFIG / "metrics.yaml"
    assert MetricCatalog.from_yaml_bytes_with_sha256(
        path.read_bytes(), origin=str(path)
    ) == MetricCatalog.from_yaml_with_sha256(path)
    with pytest.raises(ValueError, match="辞書ではない"):
        MetricCatalog.from_yaml_bytes_with_sha256(b"[]", origin="x")


@pytest.mark.parametrize(
    "payload",
    [
        b"schema_version: 1\nchanges: []\n",
        b"schema_version: 1\nchanges:\n  - kind: fan_replaced\n    ts_ms: 5\n",
        b"",
        b"schema_version: 1\nschema_version: 1\nchanges: []\n",
        b"\xff",
        b"schema_version: 1\nchanges:\n  - kind: unknown\n    ts_ms: 5\n",
    ],
)
def test_parse_declared_changes_matches_read_declared_changes(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "declared.yaml"
    path.write_bytes(payload)
    try:
        expected: object = read_declared_changes(path)
    except DeclaredChangesError as error:
        expected = (type(error), str(error))
    try:
        actual: object = parse_declared_changes(payload, origin=path.name)
    except DeclaredChangesError as error:
        actual = (type(error), str(error))
    assert actual == expected


# ---------------------------------------------------------------- import の約束（0150 §2.1）

_ALLOWED_PREFIXES = ("coldaisle.control.model.",)
_ALLOWED = {
    "coldaisle",  # `from coldaisle import logs`
    "coldaisle.logs",
    "coldaisle.training_entry",
    "coldaisle.calibration_log",
    "coldaisle.declared_changes",
    "coldaisle.control.drift.model",
    "coldaisle.metrics",
    "coldaisle.store",
    "coldaisle.ingest.calibration",
    "coldaisle.control.schema",
    "coldaisle.control.model_registry",
    "coldaisle.training_output",
    "coldaisle.dataset",
}
_FORBIDDEN = (
    "coldaisle.control.loop",
    "coldaisle.control.operating_mode",
    "coldaisle.control.authority",
    "coldaisle.control.hardware",
    "coldaisle.control.safety",
    "coldaisle.control.reactive",
    "coldaisle.control.fallback",
    "coldaisle.control.mpc",
    "coldaisle.control.supervisor",
    "coldaisle.control.rl",
    "coldaisle.control_daemon",
    "coldaisle.learned_worker",
    "coldaisle.learned_channel",
    "coldaisle.control_admin",
    "coldaisle.authority_cli",
    "coldaisle.event_entry",
    "coldaisle.registry",
    "coldaisle.ai",
    "coldaisle.api",
    "coldaisle.server",
    "coldaisle.notify",
    "serial",
    "subprocess",
    "ctypes",
    "socket",
)


def direct_imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    """(module, from で取り出した名前)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, tuple(alias.name for alias in node.names)))
    return found


def _forbidden(module: str) -> bool:
    return module == "coldaisle.control" or any(
        module == item or module.startswith(item + ".") for item in _FORBIDDEN
    )


def _allowed(module: str, names: tuple[str, ...]) -> bool:
    top = module.split(".")[0]
    if top != "coldaisle":
        return top in sys.stdlib_module_names or top in ("pydantic", "yaml")
    if module == "coldaisle":
        return set(names) <= {"logs", "training_output"}
    return module in _ALLOWED or module.startswith(_ALLOWED_PREFIXES)


def test_train_imports_only_the_allowed_modules() -> None:
    for module, names in direct_imports(SRC / "train.py"):
        assert not _forbidden(module), module
        assert _allowed(module, names), module
        if module == "coldaisle":
            assert not any(_forbidden(f"coldaisle.{name}") for name in names)


def test_training_output_imports_no_coldaisle_module() -> None:
    for module, _names in direct_imports(SRC / "training_output.py"):
        top = module.split(".")[0]
        assert top != "coldaisle", module
        assert top in sys.stdlib_module_names or top == "pydantic", module
        assert not _forbidden(module), module


def test_the_forbidden_scan_catches_the_control_package() -> None:
    assert _forbidden("coldaisle.control")
    assert _forbidden("coldaisle.control.safety.critical")
    assert not _forbidden("coldaisle.control.model.counterfactual")


def _importers_of(name: str) -> list[str]:
    importers: list[str] = []
    for path in SRC.rglob("*.py"):
        for module, names in direct_imports(path):
            if (
                module == name
                or module.startswith(name + ".")
                or (module == "coldaisle" and name.removeprefix("coldaisle.") in names)
            ):
                importers.append(str(path.relative_to(SRC)))
                break
    return sorted(importers)


def test_nothing_imports_train() -> None:
    assert _importers_of("coldaisle.train") == []


def test_only_train_and_registry_import_training_output() -> None:
    # thermal_evaluate.py は --out の親の検査（check_trusted_parent）だけを使う（0172 §2.2）
    assert _importers_of("coldaisle.training_output") == [
        "registry.py",
        "thermal_evaluate.py",
        "train.py",
    ]
