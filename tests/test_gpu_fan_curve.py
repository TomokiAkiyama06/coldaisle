"""GPU の Fan の曲線のサービス（#285 / 決定記録 0128）。

実機の NVML を使わず、偽の NVML で確かめる（AGENTS.md ルール 7。0128 §2.6）。
"""

import ast
import json
import socket
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

import coldaisle.gpu_fan_curve as gpu_fan_module
from coldaisle.gpu_fan_curve import (
    EXIT_CONFIG,
    EXIT_FAILED,
    FAN_POLICY_MANUAL,
    Curve,
    FanCurveService,
    FanLevel,
    GpuModelMismatchError,
    Notifier,
    NvmlError,
    Settings,
    main,
    restore_default_main,
    run,
    wait_for_single_device,
)

CURVE = "45:30,55:50,65:80,70:100"
"""0128 §2.3 の曲線（unit が渡す値と同じ）。"""

GPU_NAME = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"
"""曲線を確かめた型番（unit が --gpu-name に渡す値と同じ。個体の識別子ではない）。"""

FAN_POLICY_DEFAULT = 0


class FakeNvml:
    """2 基の Fan を持つ GPU 1 枚の NVML。呼び出しを記録し、エラーを差し込める。"""

    def __init__(self, *, devices: int = 1, fans: int = 2, temperature: int = 30) -> None:
        self.devices = devices
        self.fans = fans
        self.temperature = temperature
        self.speed_range = (30, 100)
        self.targets = [30] * fans
        self.policies = [FAN_POLICY_DEFAULT] * fans
        self.writes: list[tuple[int, int]] = []
        self.restores: list[int] = []
        self.fail: set[str] = set()
        self.init_calls = 0
        self.name = GPU_NAME
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise NvmlError(name, 999, "injected")

    def init(self) -> None:
        self.init_calls += 1
        self._maybe_fail("init")

    def shutdown(self) -> None:
        pass

    def device_count(self) -> int:
        self._maybe_fail("device_count")
        return self.devices

    def device_name(self) -> str:
        return self.name

    def fan_count(self) -> int:
        return self.fans

    def fan_speed_range(self) -> tuple[int, int]:
        return self.speed_range

    def temperature_c(self) -> int:
        self._maybe_fail("temperature")
        return self.temperature

    def target_fan_speed(self, fan: int) -> int:
        return self.targets[fan]

    def fan_policy(self, fan: int) -> int:
        return self.policies[fan]

    def set_fan_speed(self, fan: int, percent: int) -> None:
        self._maybe_fail("set")
        self.calls.append(f"set {fan} {percent}")
        self.writes.append((fan, percent))
        self.targets[fan] = percent
        self.policies[fan] = FAN_POLICY_MANUAL

    def set_default_fan_speed(self, fan: int) -> None:
        self._maybe_fail("restore")
        self.restores.append(fan)
        self.targets[fan] = 30
        self.policies[fan] = FAN_POLICY_DEFAULT


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__(None)
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "curve": Curve.parse(CURVE),
        "interval_ms": 1000,
        "startup_wait_ms": 0,
        "hysteresis_c": 2.0,
        "down_rate_percent_per_s": 1.0,
        "gpu_name": GPU_NAME,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# --- 曲線 ---


@pytest.mark.parametrize(
    ("temperature", "percent"),
    [(20, 30), (45, 30), (50, 40), (55, 50), (60, 65), (65, 80), (67.5, 90), (70, 100), (90, 100)],
)
def test_the_curve_interpolates_between_points(temperature: float, percent: float) -> None:
    assert Curve.parse(CURVE).percent_at(temperature) == pytest.approx(percent)


@pytest.mark.parametrize(
    "text",
    [
        "45:30",  # 点が1つ
        "45:30,45:100",  # 温度が増えない
        "45:50,55:40,70:100",  # 回転が減る
        "45:30,70:90",  # 最後が 100 % でない
        "45-30,70:100",  # 形が違う
        "nan:30,70:100",  # 温度が有限でない
    ],
)
def test_an_invalid_curve_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        Curve.parse(text)


def test_the_curve_is_at_or_above_the_observed_default_control() -> None:
    # 2026-10-14 に観測した GPU 自身の既定の制御（0128 §1）より、どの温度でも高い
    curve = Curve.parse(CURVE)
    for temperature, default_percent in [(30, 30), (58, 30), (64, 36), (72, 42), (79, 47)]:
        assert curve.percent_at(temperature) >= default_percent


# --- 上げ下げ ---


def test_the_level_rises_at_once_and_falls_at_most_one_percent_per_second() -> None:
    level = FanLevel(settings())
    assert level.update(70, 0.0) == 100
    # 30 °C に下がっても、1 s では 1 % しか下げない
    assert level.update(30, 1.0) == 99
    assert level.update(30, 11.0) == 89
    # 上がるときは待たない
    assert level.update(70, 12.0) == 100


def test_the_level_does_not_fall_within_the_hysteresis() -> None:
    level = FanLevel(settings())
    assert level.update(60, 0.0) == 65
    # 1 °C 下がっても、2 °C のヒステリシスの内側なので 65 % のまま（曲線の 59 °C は 62 %）
    assert level.update(59, 10.0) == 65
    # 3 °C 下がると 59 °C（57 + 2）の曲線の値まで下げる
    assert level.update(57, 20.0) == 62


def test_the_level_never_goes_below_the_curve() -> None:
    level = FanLevel(settings(down_rate_percent_per_s=1000.0))
    level.update(70, 0.0)
    for temperature in range(70, 20, -1):
        assert level.update(temperature, 100.0 + (70 - temperature)) >= Curve.parse(
            CURVE
        ).percent_at(temperature)


def test_the_written_percent_is_rounded_up() -> None:
    level = FanLevel(settings())
    # 47 °C は 34 %、47.5 °C は 35 %。端数は切り上げて安全側に寄せる
    assert level.update(47.1, 0.0) == 35


# --- 1 周期の読み書き ---


def test_the_service_writes_only_fans_whose_target_differs() -> None:
    nvml = FakeNvml(temperature=60)
    clock = FakeClock()
    service = FanCurveService(nvml, settings(), monotonic=clock)
    assert service.step() == 65
    assert nvml.writes == [(0, 65), (1, 65)]
    clock.now += 1
    assert service.step() == 65
    assert nvml.writes == [(0, 65), (1, 65)]


def test_the_service_rewrites_when_the_driver_returned_to_default_control() -> None:
    nvml = FakeNvml(temperature=45)
    service = FanCurveService(nvml, settings(), monotonic=FakeClock())
    service.step()
    # 既定の制御も 30 %。目標の値が同じでも、方式が既定に戻っていたら書き直す
    nvml.policies[1] = FAN_POLICY_DEFAULT
    nvml.writes.clear()
    service.step()
    assert nvml.writes == [(1, 30)]


def test_a_curve_outside_the_fan_range_is_rejected() -> None:
    nvml = FakeNvml()
    nvml.speed_range = (35, 100)
    with pytest.raises(ValueError, match="範囲"):
        FanCurveService(nvml, settings(), monotonic=FakeClock())


# --- 運転と終わり方 ---


def test_run_restores_default_control_when_stopped() -> None:
    nvml = FakeNvml(temperature=70)
    notifier = RecordingNotifier()
    stop = threading.Event()
    service = FanCurveService(nvml, settings(), monotonic=FakeClock())
    original_step = service.step

    def step_then_stop() -> int:
        result = original_step()
        stop.set()
        return result

    service.step = step_then_stop  # type: ignore[method-assign]
    assert run(service, 1000, stop, notifier) == 0
    assert nvml.writes == [(0, 100), (1, 100)]
    assert nvml.restores == [0, 1]
    assert notifier.messages == ["READY=1", "WATCHDOG=1"]


def test_run_restores_default_control_and_fails_on_an_nvml_error() -> None:
    nvml = FakeNvml(temperature=70)
    nvml.fail.add("temperature")
    notifier = RecordingNotifier()
    service = FanCurveService(nvml, settings(), monotonic=FakeClock())
    assert run(service, 1000, threading.Event(), notifier) == EXIT_FAILED
    assert nvml.restores == [0, 1]
    # READY=1 は最初の周期の前に送る。周期を終えていないので WATCHDOG は送らない
    assert notifier.messages == ["READY=1"]


def test_ready_is_sent_before_the_first_fan_write() -> None:
    """watchdog は READY=1 の後にだけ効く。

    最初の書き込みの途中の hang も watchdog で止める（0128 §2.4）。
    """
    nvml = FakeNvml(temperature=70)
    stop = threading.Event()

    class OrderingNotifier(RecordingNotifier):
        def send(self, message: str) -> None:
            nvml.calls.append(message)
            super().send(message)

    service = FanCurveService(nvml, settings(), monotonic=FakeClock())
    original_step = service.step

    def step_then_stop() -> int:
        result = original_step()
        stop.set()
        return result

    service.step = step_then_stop  # type: ignore[method-assign]
    assert run(service, 1000, stop, OrderingNotifier()) == 0
    assert nvml.calls == ["READY=1", "set 0 100", "set 1 100", "WATCHDOG=1"]


def test_the_service_refuses_a_gpu_model_other_than_the_validated_one() -> None:
    nvml = FakeNvml(temperature=70)
    nvml.name = "NVIDIA Some Other GPU"
    with pytest.raises(GpuModelMismatchError):
        FanCurveService(nvml, settings(), monotonic=FakeClock())
    assert nvml.writes == []


def test_main_stops_without_writing_when_the_gpu_model_differs() -> None:
    """GPU を交換した・別の機械に入れた: 書かずに 2（unit は再起動しない。0128 §2.2）。"""
    nvml = FakeNvml(temperature=70)
    nvml.name = f"{GPU_NAME} (other)"
    argv = ["--curve", CURVE, "--interval-ms", "1000", "--startup-wait-ms", "0",
            "--hysteresis-c", "2", "--down-rate-percent-per-s", "1",
            "--gpu-name", GPU_NAME]  # fmt: skip
    assert main(argv, nvml_factory=lambda: nvml) == EXIT_CONFIG
    assert nvml.writes == []
    assert nvml.restores == []


def test_restoring_tries_every_fan_even_if_one_fails() -> None:
    nvml = FakeNvml()
    calls: list[int] = []

    def flaky(fan: int) -> None:
        calls.append(fan)
        if fan == 0:
            raise NvmlError("restore", 1, "injected")

    nvml.set_default_fan_speed = flaky  # type: ignore[method-assign]
    assert restore_default_main(nvml) == EXIT_FAILED
    assert calls == [0, 1]


def test_restore_default_skips_when_there_is_not_exactly_one_device() -> None:
    nvml = FakeNvml(devices=2)
    assert restore_default_main(nvml) == 0
    assert nvml.restores == []


# --- 起動時の device の待ち ---


def test_waiting_succeeds_once_exactly_one_device_appears() -> None:
    nvml = FakeNvml(devices=0)
    clock = FakeClock()
    stop = threading.Event()
    original = stop.wait

    def advance(timeout: float | None = None) -> bool:
        clock.now += timeout or 0
        nvml.devices = 1
        return original(0)

    stop.wait = advance  # type: ignore[method-assign]
    assert wait_for_single_device(nvml, 5000, stop, monotonic=clock) == 1


@pytest.mark.parametrize("devices", [0, 2])
def test_waiting_gives_up_without_writing_when_the_device_is_not_unique(devices: int) -> None:
    nvml = FakeNvml(devices=devices)
    stop = threading.Event()
    assert wait_for_single_device(nvml, 0, stop) == devices
    assert nvml.writes == []


@pytest.mark.parametrize(
    ("devices", "expected"),
    # 0 個は driver の読み込みが遅いことがあるので再起動させる。
    # 2 個以上は人が直すまで変わらない（0128 §2.2）
    [(0, EXIT_FAILED), (2, EXIT_CONFIG)],
)
def test_main_does_not_write_when_the_device_is_not_unique(devices: int, expected: int) -> None:
    nvml = FakeNvml(devices=devices)
    code = main(
        [
            "--curve",
            CURVE,
            "--interval-ms",
            "1000",
            "--startup-wait-ms",
            "0",
            "--hysteresis-c",
            "2",
            "--down-rate-percent-per-s",
            "1",
            "--gpu-name",
            GPU_NAME,
        ],
        nvml_factory=lambda: nvml,
    )
    assert code == expected
    assert nvml.writes == []


@pytest.mark.parametrize(
    "argv",
    [
        [],  # 引数が無い（既定値を持たない）
        ["--curve", "45:30,70:90", "--interval-ms", "1000", "--startup-wait-ms", "0",
         "--hysteresis-c", "2", "--down-rate-percent-per-s", "1",
         "--gpu-name", GPU_NAME],  # 最後が 100 % でない
        ["--curve", CURVE, "--interval-ms", "0", "--startup-wait-ms", "0",
         "--hysteresis-c", "2", "--down-rate-percent-per-s", "1",
         "--gpu-name", GPU_NAME],  # 周期が正でない
        ["--curve", CURVE, "--interval-ms", "1000", "--startup-wait-ms", "0",
         "--hysteresis-c", "2", "--down-rate-percent-per-s", "0",
         "--gpu-name", GPU_NAME],  # 下げる速さが正でない
        ["--curve", CURVE, "--interval-ms", "1000", "--startup-wait-ms", "0",
         "--hysteresis-c", "2", "--down-rate-percent-per-s", "1"],  # 型番が無い
        ["--curve", CURVE, "--interval-ms", "1000", "--startup-wait-ms", "0",
         "--hysteresis-c", "2", "--down-rate-percent-per-s", "1",
         "--gpu-name", " "],  # 型番が空
    ],
)  # fmt: skip
def test_main_rejects_invalid_arguments_without_touching_nvml(argv: list[str]) -> None:
    def factory() -> FakeNvml:
        raise AssertionError("NVML を開かない")

    assert main(argv, nvml_factory=factory) == EXIT_CONFIG


def test_main_reports_a_missing_library_as_a_configuration_error() -> None:
    def factory() -> FakeNvml:
        raise OSError("libnvidia-ml.so.1: cannot open shared object file")

    argv = ["--curve", CURVE, "--interval-ms", "1000", "--startup-wait-ms", "0",
            "--hysteresis-c", "2", "--down-rate-percent-per-s", "1",
            "--gpu-name", GPU_NAME]  # fmt: skip
    assert main(argv, nvml_factory=factory) == EXIT_CONFIG


# --- sd_notify ---


def test_the_notifier_sends_to_a_filesystem_socket(tmp_path: Path) -> None:
    path = tmp_path / "notify"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(path))
        Notifier(str(path)).send("WATCHDOG=1")
        assert server.recv(64) == b"WATCHDOG=1"


def test_the_notifier_does_nothing_without_a_socket() -> None:
    Notifier(None).send("READY=1")


# --- 標準ライブラリだけ・`python -I -S` での起動 ---


def test_the_service_imports_only_the_standard_library() -> None:
    project_root = Path(__file__).parents[1]
    source = project_root / "src/coldaisle/gpu_fan_curve.py"
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
        "ctypes",
        "dataclasses",
        "itertools",
        "json",
        "logging",
        "math",
        "os",
        "signal",
        "socket",
        "threading",
        "time",
        "types",
        "typing",
    }
    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["coldaisle-gpu-fan"] == "coldaisle.gpu_fan_curve:main"


def test_the_source_runs_isolated_and_rejects_missing_arguments() -> None:
    source = Path(__file__).parents[1] / "src/coldaisle/gpu_fan_curve.py"
    assert source.samefile(gpu_fan_module.__file__)
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(source)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == EXIT_CONFIG
    events = [json.loads(line) for line in completed.stderr.splitlines()]
    assert events[-1]["event"] == "gpu_fan_invalid_arguments"
