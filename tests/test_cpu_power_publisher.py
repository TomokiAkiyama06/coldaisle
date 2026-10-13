"""CPU Package 電力の publisher（#278 / 決定記録 0122 §2.1）。

偽の powercap は ``tmp_path`` の下の普通のファイルで作る。本物の ``/sys/class/powercap`` は
読まない（``energy_uj`` は root だけが読め、試験の結果が機械に依存するため）。
"""

from __future__ import annotations

import ast
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

import coldaisle.cpu_power_publisher as publisher_module
from coldaisle.cpu_power_publisher import (
    EXIT_ZONE_NOT_UNIQUE,
    OUTPUT_MODE,
    SCHEMA_VERSION,
    ZONE_RETRY_INTERVAL_S,
    Publisher,
    PublisherSettings,
    ZoneNotUniqueError,
    find_package_zone,
    wait_for_package_zone,
    watts_from_energy,
)
from coldaisle.internal_telemetry import CPU_POWER_SCHEMA_VERSION

INTERVAL_MS = 1_000
MAX_RANGE_UJ = 262_143_328_850
"""``max_energy_range_uj`` の形の値（試験用の仮の値）。"""
WALL_NS = 1_790_000_000_123_456_789
_SUBPROCESS_TIMEOUT_S = 30
"""subprocess が戻らないときに試験を止めるための上限。"""


def make_zone(root: Path, entry: str, name: str | None, energy_uj: int = 0) -> Path:
    zone = root / entry
    zone.mkdir(parents=True)
    if name is not None:
        (zone / "name").write_text(f"{name}\n", encoding="ascii")
    (zone / "energy_uj").write_text(f"{energy_uj}\n", encoding="ascii")
    (zone / "max_energy_range_uj").write_text(f"{MAX_RANGE_UJ}\n", encoding="ascii")
    return zone


def set_energy(zone: Path, energy_uj: int) -> None:
    (zone / "energy_uj").write_text(f"{energy_uj}\n", encoding="ascii")


class FakeMonotonic:
    def __init__(self) -> None:
        self.now_ns = 5_000_000_000

    def advance_ms(self, ms: int) -> None:
        self.now_ns += ms * 1_000_000

    def __call__(self) -> int:
        return self.now_ns


def settings(output: Path, *, min_watts: int = 0, max_watts: int = 1_000) -> PublisherSettings:
    return PublisherSettings(
        output=output,
        interval_ms=INTERVAL_MS,
        startup_wait_ms=0,
        min_watts=min_watts,
        max_watts=max_watts,
    )


def make_publisher(
    tmp_path: Path, *, min_watts: int = 0, max_watts: int = 1_000
) -> tuple[Publisher, Path, Path, FakeMonotonic]:
    root = tmp_path / "powercap"
    zone = make_zone(root, "intel-rapl:0", "package-0", energy_uj=1_000_000)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    output = output_dir / "cpu-package.json"
    clock = FakeMonotonic()
    publisher = Publisher(
        zone,
        settings(output, min_watts=min_watts, max_watts=max_watts),
        monotonic_ns=clock,
        wall_ns=lambda: WALL_NS,
    )
    return publisher, zone, output, clock


# --- zone の探索 ---


def test_the_zone_is_found_by_name_not_by_number(tmp_path: Path) -> None:
    root = tmp_path / "powercap"
    make_zone(root, "intel-rapl:0", "psys")
    expected = make_zone(root, "intel-rapl:7", "package-0")
    make_zone(root, "intel-rapl:7:0", "core")
    # name を持たない entry（control type のディレクトリ）は候補にしない
    (root / "intel-rapl").mkdir()

    assert find_package_zone(root) == expected


def test_no_zone_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "powercap"
    make_zone(root, "intel-rapl:0", "psys")

    with pytest.raises(ZoneNotUniqueError) as caught:
        find_package_zone(root)
    assert caught.value.count == 0


def test_two_zones_are_an_error(tmp_path: Path) -> None:
    root = tmp_path / "powercap"
    make_zone(root, "intel-rapl:0", "package-0")
    make_zone(root, "intel-rapl-mmio:0", "package-0")

    with pytest.raises(ZoneNotUniqueError) as caught:
        find_package_zone(root)
    assert caught.value.count == 2


@pytest.mark.parametrize("zones", [0, 2])
def test_main_exits_2_without_writing_when_the_zone_is_not_unique(
    tmp_path: Path, zones: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pytest の SIGINT の扱いを main() に置き換えさせない
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    root = tmp_path / "powercap"
    root.mkdir()
    for index in range(zones):
        make_zone(root, f"intel-rapl:{index}", "package-0")
    output = tmp_path / "run" / "cpu-package.json"
    output.parent.mkdir()

    code = publisher_module.main(
        [
            "--powercap-root",
            str(root),
            "--output",
            str(output),
            "--interval-ms",
            str(INTERVAL_MS),
            "--startup-wait-ms",
            "0",
            "--min-watts",
            "0",
            "--max-watts",
            "1000",
        ]
    )

    assert code == EXIT_ZONE_NOT_UNIQUE == 2
    assert list(output.parent.iterdir()) == []


def test_main_exits_2_when_the_root_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    code = publisher_module.main(
        [
            "--powercap-root",
            str(tmp_path / "absent"),
            "--output",
            str(tmp_path / "out.json"),
            "--interval-ms",
            "1000",
            "--startup-wait-ms",
            "0",
            "--min-watts",
            "0",
            "--max-watts",
            "1000",
        ]
    )

    assert code == 2


def test_every_argument_is_required() -> None:
    """既定値をコードに持たない（AGENTS.md ルール9）。"""
    parser = publisher_module.build_parser()
    for action in parser._actions:
        if action.dest != "help":
            assert action.required, action.dest


@pytest.mark.parametrize(
    "interval_ms,startup_wait_ms,min_watts,max_watts",
    [(0, 0, 0, 1000), (1000, -1, 0, 1000), (1000, 0, -1, 1000), (1000, 0, 10, 10)],
)
def test_invalid_settings_are_rejected(
    tmp_path: Path, interval_ms: int, startup_wait_ms: int, min_watts: int, max_watts: int
) -> None:
    with pytest.raises(ValueError):
        PublisherSettings(
            output=tmp_path / "out.json",
            interval_ms=interval_ms,
            startup_wait_ms=startup_wait_ms,
            min_watts=min_watts,
            max_watts=max_watts,
        )


class FakeStop(threading.Event):
    """待ちの代わりに時計を進める。driver の読み込みを途中で起こせる。"""

    def __init__(self, clock: list[float], on_wait: Callable[[int], None]) -> None:
        super().__init__()
        self.clock = clock
        self.waits: list[float] = []
        self.on_wait = on_wait

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.waits.append(timeout)
        self.clock[0] += timeout
        self.on_wait(len(self.waits))
        return self.is_set()


def test_startup_retries_every_second_until_the_zone_appears(tmp_path: Path) -> None:
    """driver の読み込みを待つ（0122 §2.1）。root がまだ無い間も待つ。"""
    root = tmp_path / "powercap"
    clock = [100.0]

    def load_driver_on_third_wait(count: int) -> None:
        if count == 3:
            make_zone(root, "intel-rapl:0", "package-0")

    stop = FakeStop(clock, load_driver_on_third_wait)

    zone = wait_for_package_zone(root, 60_000, stop, monotonic=lambda: clock[0])

    assert zone == root / "intel-rapl:0"
    assert stop.waits == [ZONE_RETRY_INTERVAL_S] * 3 == [1.0] * 3


def test_startup_gives_up_after_the_wait(tmp_path: Path) -> None:
    root = tmp_path / "powercap"
    make_zone(root, "intel-rapl:0", "psys")
    clock = [100.0]
    stop = FakeStop(clock, lambda _count: None)

    with pytest.raises(ZoneNotUniqueError):
        wait_for_package_zone(root, 2_500, stop, monotonic=lambda: clock[0])

    assert stop.waits == [1.0, 1.0, 0.5]


def test_startup_wait_stops_on_request(tmp_path: Path) -> None:
    clock = [100.0]
    stop = FakeStop(clock, lambda _count: None)
    stop.set()

    assert (
        wait_for_package_zone(tmp_path / "absent", 60_000, stop, monotonic=lambda: clock[0]) is None
    )


# --- 計算 ---


def test_watts_from_energy() -> None:
    assert watts_from_energy(1_000_000, 31_000_000, MAX_RANGE_UJ, 1_000_000_000) == 30.0


def test_a_negative_delta_is_a_wrap_around() -> None:
    previous = MAX_RANGE_UJ - 10_000_000
    current = 20_000_000

    assert watts_from_energy(previous, current, MAX_RANGE_UJ, 1_000_000_000) == 30.0


def test_the_first_read_only_sets_the_baseline(tmp_path: Path) -> None:
    publisher, _zone, output, _clock = make_publisher(tmp_path)

    assert publisher.step() is None
    assert not output.exists()


def test_a_step_writes_the_rounded_average_atomically(tmp_path: Path) -> None:
    publisher, zone, output, clock = make_publisher(tmp_path)
    publisher.step()
    clock.advance_ms(1_000)
    set_energy(zone, 1_000_000 + 30_600_000)

    assert publisher.step() == 31

    assert json.loads(output.read_text(encoding="ascii")) == {
        "schema_version": SCHEMA_VERSION,
        "ts_ms": WALL_NS // 1_000_000,
        "watts": 31,
        "window_ms": 1_000,
    }
    assert stat.S_IMODE(output.stat().st_mode) == OUTPUT_MODE == 0o640
    # 一時ファイルを残さない
    assert list(output.parent.iterdir()) == [output]


def test_the_schema_version_matches_the_reader() -> None:
    """publisher は coldaisle を import しないので、版の定数は2か所にある。"""
    assert SCHEMA_VERSION == CPU_POWER_SCHEMA_VERSION


def test_a_wrap_around_is_written(tmp_path: Path) -> None:
    publisher, zone, output, clock = make_publisher(tmp_path)
    set_energy(zone, MAX_RANGE_UJ - 5_000_000)
    publisher.step()
    clock.advance_ms(1_000)
    set_energy(zone, 40_000_000)

    assert publisher.step() == 45
    assert json.loads(output.read_text(encoding="ascii"))["watts"] == 45


@pytest.mark.parametrize("elapsed_ms", [499, 2_001, 10_000])
def test_an_out_of_bounds_window_is_not_written(tmp_path: Path, elapsed_ms: int) -> None:
    publisher, zone, output, clock = make_publisher(tmp_path)
    publisher.step()
    clock.advance_ms(elapsed_ms)
    set_energy(zone, 1_000_000 + 30_000 * elapsed_ms)

    assert publisher.step() is None
    assert not output.exists()

    # 次の周期は崩れた窓をまたがない
    clock.advance_ms(1_000)
    set_energy(zone, 1_000_000 + 30_000 * elapsed_ms + 50_000_000)
    assert publisher.step() == 50


@pytest.mark.parametrize("elapsed_ms", [500, 2_000])
def test_the_window_bounds_are_inclusive(tmp_path: Path, elapsed_ms: int) -> None:
    publisher, zone, output, clock = make_publisher(tmp_path)
    publisher.step()
    clock.advance_ms(elapsed_ms)
    set_energy(zone, 1_000_000 + 30_000 * elapsed_ms)

    assert publisher.step() == 30
    assert json.loads(output.read_text(encoding="ascii"))["window_ms"] == elapsed_ms


def test_a_value_outside_the_range_is_not_written(tmp_path: Path) -> None:
    publisher, zone, output, clock = make_publisher(tmp_path, max_watts=100)
    publisher.step()
    clock.advance_ms(1_000)
    set_energy(zone, 1_000_000 + 101_000_000)

    assert publisher.step() is None
    assert not output.exists()


def test_a_bad_energy_value_raises(tmp_path: Path) -> None:
    publisher, zone, _output, _clock = make_publisher(tmp_path)
    (zone / "energy_uj").write_text("not-a-number\n", encoding="ascii")

    with pytest.raises(ValueError):
        publisher.step()


def test_a_failed_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, zone, output, clock = make_publisher(tmp_path)
    publisher.step()
    clock.advance_ms(1_000)
    set_energy(zone, 31_000_000)

    def fail(_src: object, _dst: object) -> None:
        raise OSError("injected")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="injected"):
        publisher.step()
    assert list(output.parent.iterdir()) == []


# --- 標準ライブラリだけ・`python -I -S` での起動 ---


def test_the_publisher_imports_only_the_standard_library() -> None:
    project_root = Path(__file__).parents[1]
    source = project_root / "src/coldaisle/cpu_power_publisher.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported_roots = {
        root
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for root in (
            [alias.name.split(".", maxsplit=1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", maxsplit=1)[0]]
        )
    }
    assert imported_roots <= {
        "__future__",
        "argparse",
        "collections",
        "contextlib",
        "dataclasses",
        "json",
        "logging",
        "os",
        "pathlib",
        "signal",
        "tempfile",
        "threading",
        "time",
        "types",
        "typing",
    }
    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["coldaisle-cpu-power"] == (
        "coldaisle.cpu_power_publisher:main"
    )


def _source() -> Path:
    source = Path(__file__).parents[1] / "src/coldaisle/cpu_power_publisher.py"
    assert source.samefile(publisher_module.__file__)
    return source


def _arguments(root: Path, output: Path, interval_ms: int) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-S",
        str(_source()),
        "--powercap-root",
        str(root),
        "--output",
        str(output),
        "--interval-ms",
        str(interval_ms),
        "--startup-wait-ms",
        "0",
        "--min-watts",
        "0",
        "--max-watts",
        "1000",
    ]


def _events(stderr: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in stderr.splitlines()]


def test_isolated_entrypoint_exits_2_without_a_zone(tmp_path: Path) -> None:
    root = tmp_path / "powercap"
    make_zone(root, "intel-rapl:0", "psys")
    output = tmp_path / "run" / "cpu-package.json"
    output.parent.mkdir()
    workdir = tmp_path / "cwd"
    workdir.mkdir()

    completed = subprocess.run(
        _arguments(root, output, INTERVAL_MS),
        cwd=workdir,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )

    assert completed.returncode == 2, completed.stderr
    assert completed.stdout == ""
    assert _events(completed.stderr) == [{"event": "cpu_power_zone_not_unique", "count": 0}]
    assert list(output.parent.iterdir()) == []
    assert list(workdir.iterdir()) == []


def test_isolated_entrypoint_publishes_and_stops_on_sigterm(tmp_path: Path) -> None:
    """`-I -S` の下で書き、SIGTERM で 0 で終わる（unit と同じ起動の形）。"""
    root = tmp_path / "powercap"
    make_zone(root, "intel-rapl:0", "package-0", energy_uj=123)
    output = tmp_path / "run" / "cpu-package.json"
    output.parent.mkdir()
    interval_ms = 100

    process = subprocess.Popen(
        _arguments(root, output, interval_ms),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + _SUBPROCESS_TIMEOUT_S
        while not output.exists() and time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()
            time.sleep(0.05)
        assert output.exists()
        payload = json.loads(output.read_text(encoding="ascii"))
        # energy が変わらないので 0 W（範囲の下限ちょうどは書く）
        assert payload["schema_version"] == 1
        assert payload["watts"] == 0
        assert isinstance(payload["ts_ms"], int)
        assert interval_ms // 2 <= payload["window_ms"] <= interval_ms * 2
        assert stat.S_IMODE(output.stat().st_mode) == 0o640
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=_SUBPROCESS_TIMEOUT_S)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, stderr
    assert stdout == ""
    events = _events(stderr)
    assert events[0]["event"] == "cpu_power_started"
    assert events[-1] == {"event": "cpu_power_stopped", "exit_code": 0}
