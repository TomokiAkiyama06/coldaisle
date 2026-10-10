"""#77 実機の hwmon backend（決定記録 0118 §2.5 / 0080 §2.7・§2.8）を偽の sysfs で確かめる。

偽の sysfs は ``tests/test_safety_handoff.py`` の ``FakeChip`` と同じく、決定記録 0118 §2.6 で
導入先の ``nct6799`` について確かめた挙動を再現する（:class:`FakeDriver`）。

- 自動制御（``pwmN_enable`` が 2 以上）の間の ``pwmN`` への書き込みは ``EBUSY``
- ``pwmN_enable=0`` で ``pwmN`` がすぐ 255（全速）になる
- manual（``1``）で ``pwmN=255`` のとき ``pwmN_enable`` は ``0`` と読め、255 未満なら ``1``

``FakeChip`` は引き継ぎ実行部が書ける値（``0`` と ``255``）以外を受けたら試験を落とすため、
fand が書く値（``1``・255 未満・元の自動制御の値）を受ける偽物をここに置く。登録していない
fd への書き込みは試験を落とす（``pwmN`` / ``pwmN_enable`` 以外へ書かないことの確認）。

**実機の sysfs には触れない**（すべて ``tmp_path`` の下）。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import coldaisle.control.hardware.hwmon as hwmon_module
import coldaisle.safety_handoff as handoff_module
from coldaisle import control_daemon, logs
from coldaisle.clock import ManualMonotonicClock
from coldaisle.control.config import ControlConfig, FanHardwareConfig
from coldaisle.control.hardware import (
    FanHardwareResult,
    HandoffRecordWriteError,
    HwmonFanBackend,
    HwmonHeaderNotIdentifiedError,
    HwmonStartupError,
    HwmonUnsupportedDriverError,
)
from coldaisle.control.safety import (
    CriticalSafety,
    DemandComposer,
    create_control_runtime_binding,
    emergency_handoff,
)
from coldaisle.control.schema import (
    FaultCode,
    GuardZoneOutput,
    OperatingMode,
    PerZone,
    Reason,
    SafetyState,
    Zone,
    ZoneRequest,
)
from coldaisle.control_daemon import (
    EXIT_HARDWARE_CONFIG_INVALID,
    EXIT_STARTUP_ENVIRONMENT,
    Config,
    backend_factory_for,
    build_parser,
    hwmon_backend_factory,
    main,
    run_config_invalid_max,
    simulated_backend,
)
from test_control_config import valid_documents, write_documents
from test_control_loop import CONFIG_DIR, METRICS_PATH
from test_simulated_fan_backend import (
    control_config,
    input_contract,
    ready_snapshot,
    write_hardware_document,
)

DRIVER = "nct6799"
CHANNELS = {Zone.FRONT: 6, Zone.REAR: 5, Zone.TOP: 2}
"""導入先の対応（``docs/fan-header-mapping.md``）。label の無い1つの device に3 zone がある。"""
ENTRY = "hwmon3"
RECORD_NAME = "fan-handoff.json"


# ------------------------------------------------------------------------- 偽の driver


@dataclass
class _Channel:
    channel: int
    pwm: int
    mode: int
    pwm_fd: int
    enable_fd: int


@dataclass
class FakeDriver:
    """``pwmN`` / ``pwmN_enable`` への書き込みを、導入先の driver の挙動で受ける偽物。

    backend と引き継ぎ実行部の ``_write_exact`` を差し替え、登録した属性への書き込みだけを
    解釈する。状態はファイルの中身へ書き出すので、読み戻しは実際の read で行われる。
    """

    ignore_manual: bool = False
    """``pwmN_enable=1`` を受けても manual にならない driver（``0`` のまま）を真似る。"""
    failing: set[str] = field(default_factory=set)
    """書き込みを ``EIO`` で拒む属性名。"""
    channels: dict[tuple[int, int], tuple[_Channel, str]] = field(default_factory=dict)
    by_name: dict[str, _Channel] = field(default_factory=dict)
    writes: list[tuple[str, str]] = field(default_factory=list)
    fds: list[int] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
        def write(fd: int, payload: bytes) -> None:
            metadata = os.fstat(fd)
            target = self.channels.get((metadata.st_dev, metadata.st_ino))
            if target is None:
                pytest.fail("pwmN / pwmN_enable 以外の属性へ書いた")
            self._write(*target, payload)

        monkeypatch.setattr(hwmon_module, "_write_exact", write)
        monkeypatch.setattr(handoff_module, "_write_exact", write)
        return self

    def add(self, directory: Path, channel: int, *, pwm: int = 64, mode: int = 5) -> None:
        (directory / f"fan{channel}_input").write_text("1500\n", encoding="ascii")
        fds = {}
        for kind, name in (("pwm", f"pwm{channel}"), ("enable", f"pwm{channel}_enable")):
            fd = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
            self.fds.append(fd)
            fds[kind] = fd
        state = _Channel(
            channel=channel, pwm=pwm, mode=mode, pwm_fd=fds["pwm"], enable_fd=fds["enable"]
        )
        self._render(state)
        self.by_name[f"pwm{channel}"] = state
        for kind, fd in fds.items():
            metadata = os.fstat(fd)
            self.channels[(metadata.st_dev, metadata.st_ino)] = (state, kind)

    def state(self, zone: Zone) -> tuple[int, int]:
        """（``pwmN``・内部のモード）。"""
        channel = self.by_name[f"pwm{CHANNELS[zone]}"]
        return channel.pwm, channel.mode

    def set_mode_externally(self, zone: Zone, mode: int) -> None:
        """BIOS や別のプロセスが ``pwmN_enable`` を書いたことを真似る。"""
        channel = self.by_name[f"pwm{CHANNELS[zone]}"]
        channel.mode = mode
        if mode == 0:
            channel.pwm = 255
        self._render(channel)

    def zone_writes(self, zone: Zone) -> list[tuple[str, str]]:
        channel = CHANNELS[zone]
        names = {f"pwm{channel}", f"pwm{channel}_enable"}
        return [item for item in self.writes if item[0] in names]

    def close(self) -> None:
        for fd in self.fds:
            os.close(fd)
        self.fds.clear()

    def _write(self, state: _Channel, kind: str, payload: bytes) -> None:
        name = f"pwm{state.channel}" if kind == "pwm" else f"pwm{state.channel}_enable"
        value = int(payload.decode("ascii"))
        self.writes.append((name, str(value)))
        if name in self.failing:
            raise OSError(errno.EIO, "injected")
        if kind == "enable":
            if value == 0:
                state.mode = 0
                state.pwm = 255
            elif value == 1:
                if not self.ignore_manual:
                    state.mode = 1
            else:
                state.mode = value
        else:
            assert 0 <= value <= 255
            if state.mode >= 2:
                raise OSError(errno.EBUSY, "Device or resource busy")
            state.pwm = value
        self._render(state)

    @staticmethod
    def _render(state: _Channel) -> None:
        # manual の 255 は driver が 0 と報告する（0118 §2.6 の A-3）。
        shown = 0 if state.mode == 1 and state.pwm == 255 else state.mode
        for fd, value in ((state.pwm_fd, state.pwm), (state.enable_fd, shown)):
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{value}\n".encode("ascii"), 0)


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDriver]:
    fake = FakeDriver().install(monkeypatch)
    yield fake
    fake.close()


@dataclass
class Sysfs:
    root: Path
    run: Path

    @property
    def record(self) -> Path:
        return self.run / RECORD_NAME

    @property
    def device(self) -> Path:
        return self.root / ENTRY


def make_sysfs(
    tmp_path: Path, driver: FakeDriver, *, name: str = DRIVER, mode: int = 5, pwm: int = 64
) -> Sysfs:
    """label の無い1つの device に3 zone（導入先の形）と、無関係な温度の device を作る。"""
    root = tmp_path / "class-hwmon"
    run = tmp_path / "run"
    root.mkdir()
    run.mkdir()
    unrelated = root / "hwmon0"
    unrelated.mkdir()
    (unrelated / "name").write_text("acpitz\n", encoding="utf-8")
    (unrelated / "temp1_input").write_text("42000\n", encoding="ascii")
    device = root / ENTRY
    device.mkdir()
    (device / "name").write_text(f"{name}\n", encoding="utf-8")
    # BIOS が決めた値。backend はこれらに書かない（登録していない fd への書き込みは落ちる）
    for channel in CHANNELS.values():
        (device / f"pwm{channel}_mode").write_text("1\n", encoding="ascii")
        driver.add(device, channel, pwm=pwm, mode=mode)
    return Sysfs(root=root, run=run)


def hardware(
    *, driver: str = DRIVER, labels: dict[Zone, str | None] | None = None
) -> FanHardwareConfig:
    profile = {
        "startup_demand": 0.6,
        "minimum_stable_demand": 0.3,
        "maximum_rpm": 1800,
        "pwm_to_rpm": [
            {"demand": 0.3, "rpm": 600},
            {"demand": 0.6, "rpm": 1200},
            {"demand": 1.0, "rpm": 1800},
        ],
        "airflow_index": [0.2, 0.6, 1.0],
    }
    labels = labels or {zone: None for zone in Zone}
    return FanHardwareConfig.model_validate(
        {
            "schema_version": 2,
            "approval": {"status": "confirmed", "basis": "test characterization"},
            "zones": {
                zone.value: {
                    "driver": driver,
                    "label": labels[zone],
                    "pwm_attribute": f"pwm{channel}",
                    "tach_attribute": f"fan{channel}_input",
                    "enable_attribute": f"pwm{channel}_enable",
                    "profile": profile,
                }
                for zone, channel in CHANNELS.items()
            },
        }
    )


class Pipeline:
    """Critical Safety → DemandComposer → backend を実際の順で回す（loop の代わり）。"""

    def __init__(self, sysfs: Sysfs, config: ControlConfig | None = None) -> None:
        self.config = config or control_config(fan_hardware=hardware())
        self.contract = input_contract()
        binding = create_control_runtime_binding(self.config)
        self.safety = CriticalSafety(
            self.config.safety, input_contract=self.contract, runtime_binding=binding
        )
        self.backend = HwmonFanBackend(
            self.config.fan_hardware,
            binding,
            record_path=sysfs.record,
            sysfs_root=sysfs.root,
        )
        self.composer = DemandComposer(self.config.safety)
        self.tick = 0

    def step(self, demand: float) -> tuple[SafetyState, PerZone[FanHardwareResult]]:
        self.tick += 1
        decision = self.safety.evaluate(
            ready_snapshot(self.contract, self.tick, (self.tick - 1) * 1_000),
            mode=OperatingMode.AUTO,
        )
        request = ZoneRequest(demand=demand, reason=Reason(code="hardware_test"))
        guard = GuardZoneOutput()
        command = self.composer.compose(
            requested=PerZone(front=request, rear=request, top=request),
            guard=PerZone(front=guard, rear=guard, top=guard),
            safety=decision,
            mode=OperatingMode.AUTO,
        )
        return decision.state, self.backend.apply(command)

    def until_normal(self, demand: float) -> PerZone[FanHardwareResult]:
        """STARTUP の Max を通り、最初の NORMAL の tick の結果を返す。"""
        for _ in range(20):
            state, results = self.step(demand)
            if state is SafetyState.NORMAL:
                return results
        raise AssertionError("STARTUP を抜けない")


@pytest.fixture
def sysfs(tmp_path: Path, driver: FakeDriver) -> Sysfs:
    return make_sysfs(tmp_path, driver)


@pytest.fixture
def pipeline(sysfs: Sysfs) -> Iterator[Pipeline]:
    built = Pipeline(sysfs)
    yield built
    built.backend.close()


def read_record(sysfs: Sysfs) -> dict[str, Any]:
    payload = json.loads(sysfs.record.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def events(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    fields = (getattr(record, logs.FIELDS_KEY, None) for record in caplog.records)
    return [item for item in fields if isinstance(item, dict) and item.get("event") == event]


# ------------------------------------------------------------- 起動: 特定と引き継ぎ記録


def test_startup_writes_the_v2_record_before_touching_any_header(
    pipeline: Pipeline, sysfs: Sysfs, driver: FakeDriver
) -> None:
    """header を特定した後、`pwmN_enable` に触る**前**に記録を書く（0080 §2.7 の 1）。"""
    assert driver.writes == []
    record = read_record(sysfs)
    assert record["schema_version"] == 2
    headers = {item["zone"]: item for item in record["headers"]}
    for zone, channel in CHANNELS.items():
        assert headers[zone.value] == {
            "zone": zone.value,
            "name_path": f"{ENTRY}/name",
            "expected_name": DRIVER,
            "label_path": None,
            "expected_label": None,
            "pwm_path": f"{ENTRY}/pwm{channel}",
            "enable_path": f"{ENTRY}/pwm{channel}_enable",
            "original_pwm": 64,
            "original_enable": 5,
        }
    assert stat.S_IMODE(os.stat(sysfs.record).st_mode) == 0o600
    # 一時ファイルを残さない（同じディレクトリで rename する。0080 §2.7 の 2）
    assert sorted(path.name for path in sysfs.run.iterdir()) == [RECORD_NAME]


def test_the_record_lets_the_executor_write_max_after_a_crash(
    pipeline: Pipeline, sysfs: Sysfs, driver: FakeDriver
) -> None:
    """書いた記録を引き継ぎ実行部がそのまま読み、Max にできる（0080 §2.10 の段階 4）。"""
    pipeline.until_normal(0.4)

    result = emergency_handoff(sysfs.record, sysfs.root)

    assert result.record_found
    assert set(result.applied_zones) == {zone.value for zone in Zone}
    for zone in Zone:
        assert driver.state(zone) == (255, 0)


def test_labelled_headers_are_found_by_fan_or_pwm_label(tmp_path: Path, driver: FakeDriver) -> None:
    """label のある header は同じ番号の `fanN_label`、無ければ `pwmN_label` で特定する。"""
    sysfs = make_sysfs(tmp_path, driver)
    (sysfs.device / "fan6_label").write_text("front-header\n", encoding="utf-8")
    (sysfs.device / "pwm5_label").write_text("rear-header\n", encoding="utf-8")
    (sysfs.device / "fan2_label").write_text("top-header\n", encoding="utf-8")
    labels: dict[Zone, str | None] = {
        Zone.FRONT: "front-header",
        Zone.REAR: "rear-header",
        Zone.TOP: "top-header",
    }
    config = control_config(fan_hardware=hardware(labels=labels))
    pipeline = Pipeline(sysfs, config)
    try:
        headers = {item["zone"]: item for item in read_record(sysfs)["headers"]}
        assert headers["front"]["label_path"] == f"{ENTRY}/fan6_label"
        assert headers["rear"]["label_path"] == f"{ENTRY}/pwm5_label"
        assert headers["top"]["expected_label"] == "top-header"
    finally:
        pipeline.backend.close()


def test_a_label_that_does_not_match_is_not_identified(tmp_path: Path, driver: FakeDriver) -> None:
    sysfs = make_sysfs(tmp_path, driver)
    for channel in CHANNELS.values():
        (sysfs.device / f"fan{channel}_label").write_text("other\n", encoding="utf-8")
    labels: dict[Zone, str | None] = {zone: f"{zone.value}-header" for zone in Zone}

    with pytest.raises(HwmonHeaderNotIdentifiedError):
        Pipeline(sysfs, control_config(fan_hardware=hardware(labels=labels)))

    assert not sysfs.record.exists()
    assert driver.writes == []


def test_two_devices_with_the_same_driver_name_are_not_identified(
    tmp_path: Path, driver: FakeDriver
) -> None:
    """`label: null` は同じ name の device がちょうど1つのときだけ（0118 §2.1）。"""
    sysfs = make_sysfs(tmp_path, driver)
    twin = sysfs.root / "hwmon7"
    twin.mkdir()
    (twin / "name").write_text(f"{DRIVER}\n", encoding="utf-8")

    with pytest.raises(HwmonHeaderNotIdentifiedError, match="一意に特定できない"):
        Pipeline(sysfs)

    assert not sysfs.record.exists()
    assert driver.writes == []


def test_no_device_with_the_driver_name_is_not_identified(
    tmp_path: Path, driver: FakeDriver
) -> None:
    sysfs = make_sysfs(tmp_path, driver, name="nct6798")

    with pytest.raises(HwmonHeaderNotIdentifiedError):
        Pipeline(sysfs)

    assert not sysfs.record.exists()


def test_an_unverified_driver_is_refused_before_anything_is_opened(
    tmp_path: Path, driver: FakeDriver
) -> None:
    """一覧に無い driver では `pwmN_enable` に触らず、記録も書かない（0118 §2.3a）。"""
    sysfs = make_sysfs(tmp_path, driver, name="it87")

    with pytest.raises(HwmonUnsupportedDriverError):
        Pipeline(sysfs, control_config(fan_hardware=hardware(driver="it87")))

    assert not sysfs.record.exists()
    assert driver.writes == []
    assert all(driver.state(zone) == (64, 5) for zone in Zone)


def test_an_unreadable_tach_at_startup_refuses_control(tmp_path: Path, driver: FakeDriver) -> None:
    sysfs = make_sysfs(tmp_path, driver)
    (sysfs.device / "fan5_input").unlink()

    with pytest.raises(HwmonStartupError):
        Pipeline(sysfs)

    assert not sysfs.record.exists()
    assert driver.writes == []


def test_a_symlinked_pwm_attribute_is_not_followed(tmp_path: Path, driver: FakeDriver) -> None:
    """class entry 以外の symlink はたどらない。"""
    sysfs = make_sysfs(tmp_path, driver)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("64\n", encoding="ascii")
    (sysfs.device / "pwm6").unlink()
    (sysfs.device / "pwm6").symlink_to(elsewhere)

    with pytest.raises(HwmonStartupError):
        Pipeline(sysfs)

    assert not sysfs.record.exists()


def test_a_record_that_cannot_be_written_means_no_takeover(
    tmp_path: Path, driver: FakeDriver
) -> None:
    """記録を書けなければ制御を取らない（0080 §2.7 の 3）。"""
    sysfs = make_sysfs(tmp_path, driver)
    sysfs.run.rmdir()

    with pytest.raises(HandoffRecordWriteError):
        Pipeline(sysfs)

    assert driver.writes == []
    assert all(driver.state(zone) == (64, 5) for zone in Zone)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("expected_name", "nct6798"),
        ("expected_label", "front-header"),
        ("pwm_path", f"{ENTRY}/pwm1"),
        ("enable_path", f"{ENTRY}/pwm1_enable"),
    ],
)
def test_a_record_for_other_headers_is_overwritten_with_an_error(
    sysfs: Sysfs,
    driver: FakeDriver,
    caplog: pytest.LogCaptureFixture,
    field_name: str,
    value: str,
) -> None:
    """zone・driver・label・pwm / enable の属性が1つでも違えば引き継がない（0118 §2.2）。"""
    stale = {
        "schema_version": 2,
        "headers": [
            {
                "zone": zone.value,
                "name_path": f"{ENTRY}/name",
                "expected_name": DRIVER,
                "label_path": None,
                "expected_label": None,
                "pwm_path": f"{ENTRY}/pwm{channel}",
                "enable_path": f"{ENTRY}/pwm{channel}_enable",
                "original_pwm": 10,
                "original_enable": 2,
            }
            for zone, channel in CHANNELS.items()
        ],
    }
    stale["headers"][0][field_name] = value
    sysfs.record.write_text(json.dumps(stale), encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="coldaisle"):
        pipeline = Pipeline(sysfs)
    try:
        headers = {item["zone"]: item for item in read_record(sysfs)["headers"]}
        # いまの値（64 / 5）で上書きする。前の記録の値（10 / 2）は1つも残さない
        assert {item["original_enable"] for item in headers.values()} == {5}
        assert {item["original_pwm"] for item in headers.values()} == {64}
        assert events(caplog, "handoff_record_overwritten")
        assert not events(caplog, "handoff_record_inherited")
    finally:
        pipeline.backend.close()


def test_a_matching_record_keeps_the_original_values(
    tmp_path: Path, driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    """前回が異常終了で実行部が Max を書いた後でも、元の値は記録から引き継ぐ（0080 §2.7 の 4）。

    いまの値（実行部の Max = 255 / 0）を元の値にすると、正常停止で BIOS へ返せなくなる。
    """
    sysfs = make_sysfs(tmp_path, driver, mode=0, pwm=255)
    payload = {
        "schema_version": 2,
        "headers": [
            {
                "zone": zone.value,
                "name_path": "hwmon9/name",
                "expected_name": DRIVER,
                "label_path": None,
                "expected_label": None,
                "pwm_path": f"hwmon9/pwm{channel}",
                "enable_path": f"hwmon9/pwm{channel}_enable",
                "original_pwm": 90,
                "original_enable": 5,
            }
            for zone, channel in CHANNELS.items()
        ],
    }
    sysfs.record.write_text(json.dumps(payload), encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="coldaisle"):
        pipeline = Pipeline(sysfs)
    try:
        # hwmonN の番号が違っても（再 bind）、header が同じなら引き継ぐ。path は今の番号
        headers = {item["zone"]: item for item in read_record(sysfs)["headers"]}
        assert {item["original_enable"] for item in headers.values()} == {5}
        assert {item["original_pwm"] for item in headers.values()} == {90}
        assert headers["front"]["pwm_path"] == f"{ENTRY}/pwm6"
        assert events(caplog, "handoff_record_inherited")
        pipeline.until_normal(0.4)
        assert pipeline.backend.hand_back() is True
        assert all(driver.state(zone)[1] == 5 for zone in Zone)
    finally:
        pipeline.backend.close()


def test_hwmon_numbers_appear_only_in_the_record(
    tmp_path: Path, driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    """`hwmonN` の番号は記録の外（ログ）へ出さない（0118 §2.5）。"""
    sysfs = make_sysfs(tmp_path, driver)
    with caplog.at_level(logging.DEBUG, logger="coldaisle"):
        pipeline = Pipeline(sysfs)
        pipeline.until_normal(0.4)
        pipeline.backend.hand_back()
    for record in caplog.records:
        assert ENTRY not in record.getMessage()
        assert ENTRY not in json.dumps(getattr(record, logs.FIELDS_KEY, {}), ensure_ascii=False)


# ---------------------------------------------------------------- takeover と毎 tick


def test_takeover_writes_full_speed_then_manual_and_never_lowers(
    pipeline: Pipeline, driver: FakeDriver
) -> None:
    """takeover の Max は `pwmN_enable=0` → `pwmN_enable=1`（0118 §2.3）。下げる書き込みが無い。"""
    state, results = pipeline.step(0.4)

    assert state is SafetyState.STARTUP
    for zone, channel in CHANNELS.items():
        assert driver.zone_writes(zone) == [
            (f"pwm{channel}_enable", "0"),
            (f"pwm{channel}_enable", "1"),
        ]
        result = results.get(zone)
        assert result.fault is None
        assert result.applied_demand == 1.0
        # manual の 255 は 0 と読める。全速なので成功として扱う
        assert driver.state(zone) == (255, 1)
        assert result.readback.pwm_raw == 255
        assert result.readback.rpm == 1500


def test_lowering_from_max_writes_manual_first_on_the_first_tick(
    pipeline: Pipeline, driver: FakeDriver
) -> None:
    """Max から下げる最初の tick では、`0` と読めた enable を先に `1` にする（0118 §2.3 の 3）。"""
    pipeline.step(0.4)
    driver.writes.clear()

    results = pipeline.until_normal(0.4)

    for zone, channel in CHANNELS.items():
        assert driver.zone_writes(zone)[-2:] == [
            (f"pwm{channel}_enable", "1"),
            (f"pwm{channel}", "102"),
        ]
        result = results.get(zone)
        assert result.fault is None
        assert result.applied_demand == pytest.approx(0.4)
        assert result.readback.pwm_raw == 102
        assert driver.state(zone) == (102, 1)

    # 次の tick は enable が 1 と読めるので pwm だけを書く
    driver.writes.clear()
    _, results = pipeline.step(0.5)
    for zone, channel in CHANNELS.items():
        assert driver.zone_writes(zone) == [(f"pwm{channel}", "128")]
        assert results.get(zone).fault is None


def test_a_header_put_back_to_automatic_is_enable_reverted(
    pipeline: Pipeline, driver: FakeDriver
) -> None:
    """外から `pwmN_enable=5` にされた（0118 §2.4）。書き直さず fault で Safety へ渡す。"""
    pipeline.until_normal(0.4)
    driver.set_mode_externally(Zone.REAR, 5)
    driver.writes.clear()

    _, results = pipeline.step(0.4)

    rear = results.rear
    assert rear.fault is not None
    assert rear.fault.code is FaultCode.ENABLE_REVERTED
    assert rear.applied_demand is None
    assert not rear.readback.write_ok
    # enable を取り戻す書き込みはしない（0060 未決 1 / 0118 未決 4）
    assert ("pwm5_enable", "1") not in driver.writes
    assert results.front.fault is None
    assert results.top.fault is None


def test_full_speed_set_from_outside_is_not_a_fault_at_max(
    pipeline: Pipeline, driver: FakeDriver
) -> None:
    """外から `0` にされても全速なので fault にしない（0118 §2.4）。"""
    pipeline.step(1.0)
    driver.set_mode_externally(Zone.TOP, 0)

    _, results = pipeline.step(1.0)

    assert results.top.fault is None
    assert results.top.applied_demand == 1.0


def test_lowering_while_the_driver_stays_at_full_speed_is_a_readback_mismatch(
    pipeline: Pipeline, driver: FakeDriver
) -> None:
    """`0` のまま pwm だけ下がっても Fan は全速のまま残りうる。成功と扱わない（0118 §2.5）。"""
    pipeline.step(0.4)
    driver.ignore_manual = True
    for zone in Zone:
        driver.set_mode_externally(zone, 0)

    results = pipeline.until_normal(0.4)

    for zone in Zone:
        result = results.get(zone)
        assert result.fault is not None
        assert result.fault.code is FaultCode.READBACK_MISMATCH
        assert result.applied_demand is None


def test_a_changed_device_name_is_a_write_failure_without_writing(
    pipeline: Pipeline, sysfs: Sysfs, driver: FakeDriver
) -> None:
    """運転中に device を探し直さない。name が変われば書かずに WRITE_FAILURE（0118 §2.5）。"""
    pipeline.until_normal(0.4)
    with (sysfs.device / "name").open("r+", encoding="utf-8") as handle:
        handle.write("nct6798\n")
    driver.writes.clear()

    _, results = pipeline.step(0.5)

    assert driver.writes == []
    for zone in Zone:
        result = results.get(zone)
        assert result.fault is not None
        assert result.fault.code is FaultCode.WRITE_FAILURE
        assert result.applied_demand is None


def test_an_unreadable_tach_reports_no_rpm(
    pipeline: Pipeline, sysfs: Sysfs, driver: FakeDriver
) -> None:
    """tach が読めなければ `rpm=None`（stall の判定は Critical Safety）。"""
    pipeline.until_normal(0.4)
    (sysfs.device / "fan6_input").write_text("garbage\n", encoding="ascii")

    _, results = pipeline.step(0.4)

    assert results.front.readback.rpm is None
    assert results.front.applied_demand == pytest.approx(0.4)
    assert results.rear.readback.rpm == 1500


def test_a_rejected_write_is_a_write_failure(pipeline: Pipeline, driver: FakeDriver) -> None:
    pipeline.until_normal(0.4)
    driver.failing.add("pwm2")

    _, results = pipeline.step(0.5)

    assert results.top.fault is not None
    assert results.top.fault.code is FaultCode.WRITE_FAILURE
    assert results.top.applied_demand is None


def test_a_failed_takeover_is_retried_and_does_not_acknowledge(
    pipeline: Pipeline, driver: FakeDriver
) -> None:
    """`pwmN_enable=0` を書けなければ `1` を書かない（BIOS の値に固定しない）。"""
    driver.failing.add("pwm6_enable")

    state, results = pipeline.step(1.0)

    assert state is SafetyState.STARTUP
    assert driver.zone_writes(Zone.FRONT) == [("pwm6_enable", "0")]
    assert results.front.fault is not None
    assert results.front.fault.code is FaultCode.WRITE_FAILURE
    assert driver.state(Zone.FRONT) == (64, 5)

    driver.failing.clear()
    _, results = pipeline.step(1.0)
    assert results.front.fault is None
    assert driver.state(Zone.FRONT) == (255, 1)


# --------------------------------------------------------------------- 正常停止（0080 §2.8）


def test_hand_back_restores_automatic_control_and_removes_the_record(
    pipeline: Pipeline, sysfs: Sysfs, driver: FakeDriver
) -> None:
    pipeline.until_normal(0.4)
    driver.writes.clear()

    assert pipeline.backend.hand_back() is True

    for zone, channel in CHANNELS.items():
        # PWM を下げずに元の enable へ戻す
        assert driver.zone_writes(zone) == [(f"pwm{channel}_enable", "5")]
        assert driver.state(zone)[1] == 5
    assert not sysfs.record.exists()


def test_hand_back_leaves_max_when_an_original_was_manual(
    tmp_path: Path, driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    """元が manual の header があれば返さず Max のまま、記録も消さない（0028 §2.7 / 0080 §2.8）。"""
    sysfs = make_sysfs(tmp_path, driver)
    driver.set_mode_externally(Zone.REAR, 1)
    pipeline = Pipeline(sysfs)
    pipeline.until_normal(0.4)
    driver.writes.clear()

    with caplog.at_level(logging.INFO, logger="coldaisle"):
        assert pipeline.backend.hand_back() is False

    assert sysfs.record.exists()
    for zone in Zone:
        assert driver.state(zone) == (255, 1)
        assert all(value in {"0", "1"} for name, value in driver.zone_writes(zone))
    (event,) = events(caplog, "shutdown_left_max_manual")
    assert event["zones"] == ["rear"]


def test_a_failed_hand_back_keeps_the_record(
    pipeline: Pipeline, sysfs: Sysfs, driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    """途中で戻せなかったら記録を消さない（ExecStopPost が Max を書く）。"""
    pipeline.until_normal(0.4)
    driver.failing.add("pwm5_enable")

    with caplog.at_level(logging.INFO, logger="coldaisle"):
        assert pipeline.backend.hand_back() is False

    assert sysfs.record.exists()
    assert events(caplog, "shutdown_hand_back_failed")


# ----------------------------------------------------------- 合成の起点（coldaisle-fand）


def test_the_backend_defaults_to_simulated() -> None:
    """既定は simulated（開発機で引数を付け忘れて実機の Fan に書かない。0118 §2.5）。"""
    assert build_parser().parse_args([]).backend == "simulated"
    assert backend_factory_for("simulated") is simulated_backend
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--backend", "sysfs"])


def test_the_hwmon_backend_uses_the_executors_record_path() -> None:
    """記録の path は実行部の定数を合成の起点が渡す（0080 §2.7 の 6）。"""
    assert control_daemon.HWMON_HANDOFF_RECORD_PATH == handoff_module.HANDOFF_RECORD_PATH
    assert control_daemon.HWMON_SYSFS_ROOT == handoff_module.HWMON_ROOT


def test_hardware_package_does_not_import_upper_layers() -> None:
    source = Path(hwmon_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("coldaisle.safety_handoff", "coldaisle.control_daemon"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source


def _config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware().model_dump_json())
    write_documents(directory, documents)
    return directory


def _argv(directory: Path, *extra: str) -> list[str]:
    return [
        "--config-dir",
        str(directory),
        "--db",
        str(directory / "control.db"),
        "--metrics",
        str(METRICS_PATH),
        "--quality-rules",
        str(CONFIG_DIR / "quality.yaml"),
        "--no-admin",
        "--authority-root",
        str(directory / "authority"),
        *extra,
    ]


@pytest.fixture
def fand_paths(monkeypatch: pytest.MonkeyPatch, sysfs: Sysfs) -> Sysfs:
    monkeypatch.setattr(control_daemon.logs, "configure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(control_daemon, "HWMON_HANDOFF_RECORD_PATH", sysfs.record)
    monkeypatch.setattr(control_daemon, "HWMON_SYSFS_ROOT", sysfs.root)
    return sysfs


def test_fand_with_the_hwmon_backend_takes_over_and_hands_back(
    tmp_path: Path, fand_paths: Sysfs, driver: FakeDriver
) -> None:
    """`--backend hwmon` で記録 → takeover の Max → 正常停止で返して記録を消す。"""
    code = main(_argv(_config_dir(tmp_path), "--backend", "hwmon", "--max-ticks", "1"))

    assert code == 0
    for zone, channel in CHANNELS.items():
        assert driver.zone_writes(zone) == [
            (f"pwm{channel}_enable", "0"),
            (f"pwm{channel}_enable", "1"),
            (f"pwm{channel}_enable", "5"),
        ]
    assert not fand_paths.record.exists()


def test_fand_refuses_ambiguous_headers_with_exit_code_2(
    tmp_path: Path, fand_paths: Sysfs, driver: FakeDriver
) -> None:
    twin = fand_paths.root / "hwmon7"
    twin.mkdir()
    (twin / "name").write_text(f"{DRIVER}\n", encoding="utf-8")

    code = main(_argv(_config_dir(tmp_path), "--backend", "hwmon", "--max-ticks", "1"))

    assert code == EXIT_HARDWARE_CONFIG_INVALID
    assert driver.writes == []
    assert not fand_paths.record.exists()


def test_fand_refuses_an_unwritable_record_with_exit_code_5(
    tmp_path: Path, fand_paths: Sysfs, driver: FakeDriver
) -> None:
    fand_paths.run.rmdir()

    code = main(_argv(_config_dir(tmp_path), "--backend", "hwmon", "--max-ticks", "1"))

    assert code == EXIT_STARTUP_ENVIRONMENT
    assert driver.writes == []


def test_fand_without_backend_never_touches_the_sysfs(
    tmp_path: Path, fand_paths: Sysfs, driver: FakeDriver
) -> None:
    assert main(_argv(_config_dir(tmp_path), "--max-ticks", "1")) == 0
    assert driver.writes == []
    assert not fand_paths.record.exists()


def test_config_invalid_writes_the_record_then_max_and_keeps_it(
    tmp_path: Path, sysfs: Sysfs, driver: FakeDriver
) -> None:
    """`config_invalid` の Max も記録の後に `0` → `1` の順（0080 §2.7 の 5 / 0118 §2.3）。"""
    directory = tmp_path / "invalid-config"
    directory.mkdir()
    write_hardware_document(directory / "fan-hardware.yaml", hardware())

    stats = run_config_invalid_max(
        Config(config_dir=directory),
        monotonic=ManualMonotonicClock(0),
        backend_factory=hwmon_backend_factory(sysfs.record, sysfs.root),
    )

    assert stats.emergency_max
    record = read_record(sysfs)
    assert {item["original_enable"] for item in record["headers"]} == {5}
    for zone, channel in CHANNELS.items():
        assert driver.zone_writes(zone) == [
            (f"pwm{channel}_enable", "0"),
            (f"pwm{channel}_enable", "1"),
        ]
        assert driver.state(zone) == (255, 1)
    # 正しい設定で再起動するまで解除しない。記録は消さない（ExecStopPost が Max を書く）
    assert sysfs.record.exists()


def test_config_invalid_with_an_ambiguous_header_writes_nothing(
    tmp_path: Path, fand_paths: Sysfs, driver: FakeDriver
) -> None:
    directory = tmp_path / "invalid-config"
    directory.mkdir()
    write_hardware_document(directory / "fan-hardware.yaml", hardware())
    twin = fand_paths.root / "hwmon7"
    twin.mkdir()
    (twin / "name").write_text(f"{DRIVER}\n", encoding="utf-8")

    code = main(_argv(directory, "--backend", "hwmon", "--max-ticks", "1"))

    assert code == EXIT_HARDWARE_CONFIG_INVALID
    assert driver.writes == []
