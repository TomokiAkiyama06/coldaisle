"""証明書の部品の digest（#288 / 決定記録 0152 §2.3 / §2.8 の「digest の関数」）。実機は要らない。

digest の値は module が計算する。試験は値を作らず、同じ入力で同じ値・1か所の変更でその部品
だけが変わること・境界をずらした入力が別の値になることだけを確かめる。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from coldaisle import hint_certificate_digest as digest
from coldaisle.calibration_offsets import effective_metric_offsets
from coldaisle.channels import METRIC_TO_CHANNEL
from coldaisle.control.learned_handoff import CalibrationUnavailableCode

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
SHA256_HEX = 64


def write(root: Path, relative: str, content: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def tree(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(parents=True)
    for relative, content in files.items():
        write(root, relative, content)
    return root


# ---------------------------------------------------------------- 符号化


def test_length_prefixed_encoding_keeps_boundaries() -> None:
    assert digest.length_prefixed([b"ab", b"c"]) != digest.length_prefixed([b"a", b"bc"])
    assert digest.length_prefixed([b"ab"]) == (2).to_bytes(8, "big") + b"ab"
    assert digest.length_prefixed([]) == b""


# ---------------------------------------------------------------- source_tree


def test_the_same_tree_gives_the_same_digest(tmp_path: Path) -> None:
    files = {"__init__.py": b"", "control/loop.py": b"x = 1\n"}
    first = tree(tmp_path / "a", files)
    second = tree(tmp_path / "b", files)
    value = digest.source_tree_sha256(first)
    assert len(value) == SHA256_HEX
    assert value == digest.source_tree_sha256(second)


def test_shifting_the_boundary_between_path_and_content_changes_the_digest(
    tmp_path: Path,
) -> None:
    first = tree(tmp_path / "a", {"ab": b"c"})
    second = tree(tmp_path / "b", {"a": b"bc"})
    assert digest.source_tree_sha256(first) != digest.source_tree_sha256(second)


def test_shifting_the_boundary_between_two_files_changes_the_digest(tmp_path: Path) -> None:
    first = tree(tmp_path / "a", {"a.py": b"12", "b.py": b"3"})
    second = tree(tmp_path / "b", {"a.py": b"1", "b.py": b"23"})
    assert digest.source_tree_sha256(first) != digest.source_tree_sha256(second)


def test_bytecode_caches_are_not_part_of_the_tree(tmp_path: Path) -> None:
    root = tree(tmp_path / "a", {"__init__.py": b"", "m.py": b"y = 2\n"})
    before = digest.source_tree_sha256(root)
    write(root, "__pycache__/m.cpython-312.pyc", b"\x00cache")
    write(root, "sub/__pycache__/n.cpython-312.pyc", b"\x00cache")
    write(root, "stray.pyc", b"\x00cache")
    assert digest.source_tree_sha256(root) == before


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda root: write(root, "m.py", b"y = 3\n"), id="content"),
        pytest.param(lambda root: write(root, "web/app.js", b"//"), id="non-python-file"),
        pytest.param(lambda root: (root / "m.py").rename(root / "n.py"), id="rename"),
        pytest.param(lambda root: (root / "m.py").unlink(), id="remove"),
    ],
)
def test_any_change_in_the_tree_changes_the_digest(tmp_path: Path, change) -> None:
    root = tree(tmp_path / "a", {"__init__.py": b"", "m.py": b"y = 2\n"})
    before = digest.source_tree_sha256(root)
    change(root)
    assert digest.source_tree_sha256(root) != before


def test_the_order_does_not_depend_on_the_filesystem(tmp_path: Path) -> None:
    first = tree(tmp_path / "a", {"b.py": b"2", "a.py": b"1", "c/d.py": b"3"})
    second = tree(tmp_path / "b", {"c/d.py": b"3", "a.py": b"1", "b.py": b"2"})
    assert digest.source_tree_sha256(first) == digest.source_tree_sha256(second)


@pytest.mark.parametrize("target_is_directory", [False, True])
def test_a_symlink_in_the_tree_is_refused(tmp_path: Path, target_is_directory: bool) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.py").write_bytes(b"z = 1\n")
    root = tree(tmp_path / "a", {"__init__.py": b""})
    target = outside if target_is_directory else outside / "x.py"
    os.symlink(target, root / "linked", target_is_directory=target_is_directory)
    with pytest.raises(ValueError, match=r"symlink|通常のファイル"):
        digest.source_tree_sha256(root)


def test_the_imported_package_is_the_one_under_test() -> None:
    import coldaisle

    package = digest.imported_package_dir()
    assert package == Path(coldaisle.__file__).resolve().parent
    assert (package / "hint_certificate_digest.py").is_file()
    assert len(digest.source_tree_sha256(package)) == SHA256_HEX


# ---------------------------------------------------------------- lockfile


def test_the_lockfile_digest_is_the_digest_of_its_bytes(tmp_path: Path) -> None:
    path = tmp_path / "uv.lock"
    path.write_bytes(b"version = 1\n")
    first = digest.lockfile_sha256(path)
    path.write_bytes(b"version = 2\n")
    assert digest.lockfile_sha256(path) != first
    assert len(first) == SHA256_HEX


def test_a_missing_lockfile_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        digest.lockfile_sha256(tmp_path / "absent.lock")


# ---------------------------------------------------------------- environment


def environment(**overrides: object) -> digest.RuntimeEnvironment:
    values: dict[str, object] = {
        "implementation": "cpython",
        "implementation_version": "3.12.3.final.0",
        "platform": "linux-x86_64",
        "machine": "x86_64",
        "distributions": (
            digest.InstalledDistribution(name="numpy", version="2.0.0", record=b"numpy/a.so,x,1\n"),
            digest.InstalledDistribution(name="pydantic", version="2.9.0", record=b"p,y,2\n"),
        ),
    }
    return digest.RuntimeEnvironment(**(values | overrides))  # type: ignore[arg-type]


def test_the_environment_digest_does_not_depend_on_the_listing_order() -> None:
    base = environment()
    reordered = environment(distributions=tuple(reversed(base.distributions)))
    assert digest.environment_sha256(base) == digest.environment_sha256(reordered)


def test_a_distribution_name_is_normalised_before_ordering() -> None:
    lower = environment(
        distributions=(digest.InstalledDistribution(name="ruamel.yaml", version="1", record=b"r"),)
    )
    spelled = environment(
        distributions=(digest.InstalledDistribution(name="Ruamel_Yaml", version="1", record=b"r"),)
    )
    assert digest.environment_sha256(lower) == digest.environment_sha256(spelled)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"implementation": "pypy"}, id="implementation"),
        pytest.param({"implementation_version": "3.12.4.final.0"}, id="version"),
        pytest.param({"platform": "linux-aarch64"}, id="platform"),
        pytest.param({"machine": "aarch64"}, id="machine"),
        pytest.param(
            {
                "distributions": (
                    digest.InstalledDistribution(
                        name="numpy", version="2.0.0", record=b"numpy/a.so,x,9\n"
                    ),
                    digest.InstalledDistribution(
                        name="pydantic", version="2.9.0", record=b"p,y,2\n"
                    ),
                )
            },
            id="record",
        ),
        pytest.param(
            {
                "distributions": (
                    digest.InstalledDistribution(
                        name="numpy", version="2.0.1", record=b"numpy/a.so,x,1\n"
                    ),
                    digest.InstalledDistribution(
                        name="pydantic", version="2.9.0", record=b"p,y,2\n"
                    ),
                )
            },
            id="distribution-version",
        ),
        pytest.param(
            {
                "distributions": (
                    digest.InstalledDistribution(name="numpy", version="2.0.0", record=None),
                    digest.InstalledDistribution(
                        name="pydantic", version="2.9.0", record=b"p,y,2\n"
                    ),
                )
            },
            id="record-missing",
        ),
        pytest.param(
            {
                "distributions": (
                    digest.InstalledDistribution(
                        name="numpy", version="2.0.0", record=b"numpy/a.so,x,1\n"
                    ),
                )
            },
            id="distribution-removed",
        ),
    ],
)
def test_each_part_of_the_environment_changes_the_digest(overrides: dict[str, object]) -> None:
    assert digest.environment_sha256(environment(**overrides)) != digest.environment_sha256(
        environment()
    )


def test_an_empty_record_is_not_a_missing_record() -> None:
    empty = environment(
        distributions=(digest.InstalledDistribution(name="a", version="1", record=b""),)
    )
    missing = environment(
        distributions=(digest.InstalledDistribution(name="a", version="1", record=None),)
    )
    assert digest.environment_sha256(empty) != digest.environment_sha256(missing)


def test_the_current_environment_is_fingerprinted() -> None:
    current = digest.current_environment()
    assert current.distributions
    assert digest.environment_sha256() == digest.environment_sha256(current)


# ---------------------------------------------------------------- calibration


def a_channel() -> str:
    return next(
        channel for metric, channel in METRIC_TO_CHANNEL.items() if "humidity" not in metric
    )


def test_an_empty_available_calibration_is_not_unavailable() -> None:
    """空の `offsets_c` の available と unavailable を同じ digest にしない（0096 §2.6）。"""
    available = digest.calibration_available_sha256({})
    for code in CalibrationUnavailableCode:
        assert digest.calibration_unavailable_sha256(code.value) != available


def test_the_two_unavailable_codes_differ() -> None:
    values = {
        digest.calibration_unavailable_sha256(code.value) for code in CalibrationUnavailableCode
    }
    assert len(values) == len(CalibrationUnavailableCode)


def test_changing_one_offset_changes_the_calibration_digest() -> None:
    channel = a_channel()
    assert digest.calibration_available_sha256(
        {channel: 0.5}
    ) != digest.calibration_available_sha256({channel: 0.25})


def test_the_calibration_digest_follows_the_effective_offsets() -> None:
    """実効の offset が同じなら同じ digest（無いチャネルは 0.0、`-0.0` は `0.0`）。"""
    channel = a_channel()
    assert digest.calibration_available_sha256({}) == digest.calibration_available_sha256(
        {channel: 0.0}
    )
    assert digest.calibration_available_sha256(
        {channel: -0.0}
    ) == digest.calibration_available_sha256({channel: 0.0})
    assert digest.calibration_available_sha256({}) == digest.calibration_available_sha256(
        {"not-a-channel": 3.0}
    )
    assert effective_metric_offsets({"not-a-channel": 3.0}) == effective_metric_offsets({})


def test_the_available_digest_wraps_the_offsets_digest() -> None:
    """`available` の digest は較正の全体の `offsets_sha256` そのものではない（状態の印で包む）。"""
    from coldaisle.calibration_offsets import offsets_sha256

    channel = a_channel()
    plain = offsets_sha256(effective_metric_offsets({channel: 0.5}))
    assert digest.calibration_available_sha256({channel: 0.5}) != plain


def test_a_non_finite_offset_and_an_empty_code_are_errors() -> None:
    with pytest.raises(ValueError):
        digest.calibration_available_sha256({a_channel(): float("nan")})
    with pytest.raises(ValueError):
        digest.calibration_unavailable_sha256("")


# ---------------------------------------------------------------- 境界（0152 §2.8）


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


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
    ],
)
def test_llm_api_eventd_admin_and_safety_do_not_import_the_digest(package: str) -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted((SRC / package).rglob("*.py"))
        if any(name.startswith("coldaisle.hint_certificate_digest") for name in _imports(path))
    ]
    assert offenders == []


def test_the_digest_module_does_not_import_control() -> None:
    imported = _imports(SRC / "hint_certificate_digest.py")
    assert not any(name.startswith("coldaisle.control") for name in imported)
