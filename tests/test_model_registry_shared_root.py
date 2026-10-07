"""Model Registry の共有の root モードと、作るファイルの mode（決定記録 0104 §2.3 / §2.4 / §2.10）。

承認者（`coldaisle-authority raise`）は Registry を**作らず**、lock を `O_RDONLY` で開いて
`flock` だけを取る。Registry の書き手が作るファイルは `umask` に依らず `0640`、ディレクトリは
親の permission の bit を写す。実機は要らない（`tmp_path`）。
"""

from __future__ import annotations

import ast
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.control import ModelRegistry, RegistrySharedRootError, RegistrySnapshot
from test_model_registry import ACTOR, LIMITS, NOW_MS, artifact_path, metadata, payload

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "coldaisle"
RUNS_AS_ROOT = os.geteuid() == 0
LOCK = ".registry.lock"


@contextmanager
def umask(value: int) -> Iterator[None]:
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


def make_shared_root(tmp_path: Path, *, with_lock: bool = True) -> Path:
    """導入手順が作る形（`install -d -m 2770`、lock は `install -m 0660`）。"""
    root = tmp_path / "registry"
    root.mkdir()
    os.chmod(root, 0o2770)
    if not os.stat(root).st_mode & stat.S_ISGID:
        pytest.skip("この環境ではディレクトリに setgid を付けられない")
    if with_lock:
        (root / LOCK).touch()
        os.chmod(root / LOCK, 0o660)
    return root


def reader(root: Path) -> ModelRegistry:
    return ModelRegistry(root, limits=LIMITS, require_shared_root=True)


def writer(root: Path) -> ModelRegistry:
    return ModelRegistry(root, SimulatedClock(NOW_MS), limits=LIMITS)


def register(registry: ModelRegistry, version: str = "1.0.0") -> None:
    registry.register_candidate(metadata(version), payload(version), actor=ACTOR, reason="新規登録")


# --- 共有の root モード: 作らない ---------------------------------------------------------


def test_shared_mode_does_not_create_a_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    with pytest.raises(RegistrySharedRootError, match="root が無い"), reader(root).pinned():
        pass
    assert not root.exists()


def test_shared_mode_does_not_create_missing_ancestors(tmp_path: Path) -> None:
    root = tmp_path / "missing" / "registry"
    with pytest.raises(RegistrySharedRootError), reader(root).pinned():
        pass
    assert not (tmp_path / "missing").exists()


def test_shared_mode_does_not_create_a_missing_lock(tmp_path: Path) -> None:
    root = make_shared_root(tmp_path, with_lock=False)
    with pytest.raises(RegistrySharedRootError, match="lock が無い"), reader(root).pinned():
        pass
    assert sorted(os.listdir(root)) == []


# --- 共有の root モード: root の形 ---------------------------------------------------------


def test_shared_mode_rejects_a_root_without_setgid(tmp_path: Path) -> None:
    root = make_shared_root(tmp_path)
    os.chmod(root, 0o770)
    with pytest.raises(RegistrySharedRootError, match="setgid"), reader(root).pinned():
        pass


def test_shared_mode_rejects_a_root_with_other_permissions(tmp_path: Path) -> None:
    root = make_shared_root(tmp_path)
    os.chmod(root, 0o2775)
    with pytest.raises(RegistrySharedRootError, match="other"), reader(root).pinned():
        pass


def test_shared_mode_checks_the_root_on_inspect_too(tmp_path: Path) -> None:
    """lock を取らない読み取り（`raise_stage()` の読み直し）でも、形の違う root を読まない。"""
    root = make_shared_root(tmp_path)
    os.chmod(root, 0o770)
    with pytest.raises(RegistrySharedRootError, match="setgid"):
        reader(root).inspect()


def test_shared_mode_rejects_a_symlinked_root(tmp_path: Path) -> None:
    real = make_shared_root(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(Exception, match="symlink"), reader(link).pinned():
        pass


# --- 共有の root モード: lock は O_RDONLY で flock だけ --------------------------------------


@pytest.mark.skipif(RUNS_AS_ROOT, reason="root は mode に関わらず書き込みで開ける")
def test_shared_mode_takes_the_lock_through_a_read_only_open(tmp_path: Path) -> None:
    """lock を `r` だけにしても取れる（承認者に lock の `w` を与えない。0104 §2.4 / §5 の 5）。"""
    root = make_shared_root(tmp_path)
    os.chmod(root / LOCK, 0o440)
    with reader(root).pinned() as snapshot:
        assert snapshot == RegistrySnapshot(revision=0)


def test_shared_mode_lock_excludes_other_holders(tmp_path: Path) -> None:
    """`O_RDONLY` の fd の `flock` でも排他になる（書き手の lock と同じ lock）。"""
    root = make_shared_root(tmp_path)
    other = os.open(root / LOCK, os.O_RDWR)
    try:
        with reader(root).pinned(), pytest.raises(BlockingIOError):
            flock(other, LOCK_EX | LOCK_NB)
        flock(other, LOCK_EX | LOCK_NB)
        flock(other, LOCK_UN)
    finally:
        os.close(other)


def test_shared_mode_reads_what_the_writer_wrote(tmp_path: Path) -> None:
    root = make_shared_root(tmp_path)
    register(writer(root))
    with reader(root).pinned() as snapshot:
        assert snapshot.revision == 1


# --- 既定のモード: 共有の root では lock を作らない ------------------------------------------


def test_writer_does_not_create_the_lock_in_a_shared_root(tmp_path: Path) -> None:
    """消えた lock を書き手が `0600` で作り直すと、承認者が flock を取れなくなる（0104 §2.4）。"""
    root = make_shared_root(tmp_path, with_lock=False)
    with pytest.raises(RegistrySharedRootError, match="lock が無い"):
        register(writer(root))
    assert sorted(os.listdir(root)) == []


def test_writer_still_creates_a_development_root_and_lock(tmp_path: Path) -> None:
    """開発用（setgid の無い root）は従来どおり作る。root は `0700`。"""
    root = tmp_path / "var" / "model-registry"
    with umask(0o022):
        register(writer(root))
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert (root / LOCK).is_file()


def test_a_root_created_under_a_setgid_parent_is_not_taken_for_a_shared_root(
    tmp_path: Path,
) -> None:
    """setgid の親の下で作った root は setgid を継ぐが、開発用の root として扱う（codex P2）。

    継いだ setgid のままだと「共有の root」と取り違え、lock を作らずに止まる。
    """
    parent = tmp_path / "workspace"
    parent.mkdir()
    os.chmod(parent, 0o2775)
    if not os.stat(parent).st_mode & stat.S_ISGID:
        pytest.skip("この環境ではディレクトリに setgid を付けられない")
    root = parent / "var" / "model-registry"
    with umask(0o022):
        register(writer(root))
    for directory in (parent / "var", root):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700, directory
    assert (root / LOCK).is_file()


# --- 作るファイルとディレクトリの mode ----------------------------------------------------


@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_files_are_0640_regardless_of_umask(tmp_path: Path, mask: int) -> None:
    root = make_shared_root(tmp_path)
    with umask(mask):
        register(writer(root))
    for path in (root / "registry.json", artifact_path(root)):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o640, path


@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_directories_follow_the_shared_root(tmp_path: Path, mask: int) -> None:
    root = make_shared_root(tmp_path)
    with umask(mask):
        register(writer(root))
    directory = artifact_path(root).parent
    while directory != root:
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o2770, directory
        directory = directory.parent


@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_directories_stay_private_under_a_development_root(tmp_path: Path, mask: int) -> None:
    root = tmp_path / "model-registry"
    with umask(mask):
        register(writer(root))
    directory = artifact_path(root).parent
    while directory != root:
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700, directory
        directory = directory.parent


# --- 構造: 制御は Registry の lock を取らない（0104 §2.4 / §2.10） ---------------------------

_LOCKING_CALLS = frozenset(
    {"pinned", "register_candidate", "mark_validated", "promote", "rollback", "retire"}
)
"""Registry の排他 lock を取る `ModelRegistry` の操作。"""


def _called_attributes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _control_process_sources() -> list[Path]:
    """制御プロセスの起点（fand・Learned worker の経路・control loop）。"""
    files = [SRC / "control_daemon.py", SRC / "control" / "loop.py"]
    files += sorted((SRC / "learned_channel").rglob("*.py"))
    return files


def test_control_process_never_takes_the_registry_lock() -> None:
    offenders = {
        str(path.relative_to(SRC)): sorted(_called_attributes(path) & _LOCKING_CALLS)
        for path in _control_process_sources()
        if _called_attributes(path) & _LOCKING_CALLS
    }
    assert offenders == {}


def test_only_raise_stage_pins_the_registry() -> None:
    """`pinned()` を呼ぶのは `AuthorityStore.raise_stage()`（`control/authority.py`）だけ。"""
    callers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name != "model_registry.py" and "pinned" in _called_attributes(path)
    )
    assert callers == ["control/authority.py"]


def test_authority_cli_opens_the_registry_in_shared_mode() -> None:
    tree = ast.parse((SRC / "authority_cli.py").read_text(encoding="utf-8"))
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ModelRegistry"
    ]
    assert constructions
    for node in constructions:
        flags = {
            keyword.arg: keyword.value
            for keyword in node.keywords
            if isinstance(keyword.value, ast.Constant)
        }
        assert "require_shared_root" in flags
        assert flags["require_shared_root"].value is True  # type: ignore[attr-defined]
