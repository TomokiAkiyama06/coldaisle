"""#78 異常停止後の emergency Max handoff を偽 sysfs で検証する。

偽の sysfs は決定記録 0118 §2.6 で導入先の ``nct6799`` について確かめた挙動を再現する
（:class:`FakeChip`）。

- 自動制御（``pwmN_enable`` が 2 以上）の間の ``pwmN`` への書き込みは ``EBUSY``
- ``pwmN_enable=0`` で ``pwmN`` がすぐ 255（全速）になる
- manual（``1``）で ``pwmN=255`` のとき、``pwmN_enable`` は ``0`` と読める
"""

from __future__ import annotations

import ast
import errno
import json
import logging
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import coldaisle.safety_handoff as handoff_module
from coldaisle.control.hardware import VERIFIED_HWMON_DRIVERS
from coldaisle.control.safety import HandoffRecordError, emergency_handoff

DRIVER = "nct6799"
ZONES = ("front", "rear", "top")
LABELLESS_CHANNELS = {"front": 6, "rear": 5, "top": 2}
"""導入先の対応（``docs/fan-header-mapping.md``）。label の無い1つの device に3 zone がある。"""


@dataclass
class _Channel:
    channel: int
    pwm: int
    mode: int
    pwm_fd: int
    enable_fd: int
    """状態を書き出す fd。path でなく inode に書くので、path を差し替えても外へ書かない。"""


@dataclass
class FakeChip:
    """``pwmN`` / ``pwmN_enable`` への書き込みを、導入先の driver の挙動で受ける偽物。

    実行部の ``_write_exact`` を差し替え、登録した属性への書き込みだけを解釈する。
    状態はファイルの中身へ書き出すので、実行部の読み戻しは実際の read で行われる。
    実行部が書いてよい値（``pwmN_enable=0`` と ``pwmN=255``）以外を受けたら試験を落とす。
    """

    reject_full_speed: bool = False
    """``pwmN_enable=0`` を ``EINVAL`` で拒否する driver を真似る。"""
    full_speed_sets_pwm: bool = True
    """``False`` なら ``pwmN_enable=0`` を受けても ``pwmN`` を 255 にしない。"""
    channels: dict[tuple[int, int], tuple[_Channel, str]] = field(default_factory=dict)
    writes: list[tuple[str, bytes]] = field(default_factory=list)
    fds: list[int] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeChip:
        original = handoff_module._write_exact

        def write(fd: int, payload: bytes) -> None:
            metadata = os.fstat(fd)
            target = self.channels.get((metadata.st_dev, metadata.st_ino))
            if target is None:
                original(fd, payload)
                return
            self._write(*target, payload)

        monkeypatch.setattr(handoff_module, "_write_exact", write)
        return self

    def add(self, directory: Path, channel: int, *, pwm: int = 64, mode: int = 5) -> None:
        """``directory`` に ``pwmN`` / ``pwmN_enable`` / ``fanN_input`` を作って登録する。"""
        (directory / f"fan{channel}_input").write_text("1500\n", encoding="ascii")
        fds = {}
        for kind, name in (("pwm", f"pwm{channel}"), ("enable", f"pwm{channel}_enable")):
            fd = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
            self.fds.append(fd)
            fds[kind] = fd
        state = _Channel(
            channel=channel,
            pwm=pwm,
            mode=mode,
            pwm_fd=fds["pwm"],
            enable_fd=fds["enable"],
        )
        self._render(state)
        for kind, fd in fds.items():
            metadata = os.fstat(fd)
            self.channels[(metadata.st_dev, metadata.st_ino)] = (state, kind)

    def close(self) -> None:
        for fd in self.fds:
            os.close(fd)
        self.fds.clear()

    def _write(self, state: _Channel, kind: str, payload: bytes) -> None:
        name = f"pwm{state.channel}" if kind == "pwm" else f"pwm{state.channel}_enable"
        self.writes.append((name, payload))
        if kind == "enable":
            assert payload == b"0\n", f"実行部は enable に 0 以外を書かない: {payload!r}"
            if self.reject_full_speed:
                raise OSError(errno.EINVAL, "injected: enable=0 rejected")
            state.mode = 0
            if self.full_speed_sets_pwm:
                state.pwm = 255
        else:
            assert payload == b"255\n", f"実行部は pwm に 255 以外を書かない: {payload!r}"
            if state.mode >= 2:
                raise OSError(errno.EBUSY, "Device or resource busy")
            state.pwm = 255
        self._render(state)

    @staticmethod
    def _render(state: _Channel) -> None:
        # manual の 255 は driver が 0 と報告する（0118 §2.6 の A-3）。
        shown = 0 if state.mode == 1 and state.pwm == 255 else state.mode
        for fd, value in ((state.pwm_fd, state.pwm), (state.enable_fd, shown)):
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{value}\n".encode("ascii"), 0)


ChipFactory = Callable[..., FakeChip]


@pytest.fixture
def make_chip(monkeypatch: pytest.MonkeyPatch) -> Iterator[ChipFactory]:
    """設定を変えた :class:`FakeChip` を作って差し込む。試験の後に fd を閉じる。"""
    created: list[FakeChip] = []

    def factory(**options: bool) -> FakeChip:
        fake = FakeChip(**options).install(monkeypatch)
        created.append(fake)
        return fake

    yield factory
    for fake in created:
        fake.close()


@pytest.fixture
def chip(make_chip: ChipFactory) -> FakeChip:
    return make_chip()


def create_sysfs(root: Path, chip: FakeChip | None = None, *, driver: str = DRIVER) -> None:
    """label のある3つの device（v1 の形）。``chip`` が無ければ普通のファイルで作る。"""
    for index, zone in enumerate(ZONES, start=1):
        header = root / f"hwmon{index}"
        header.mkdir()
        _create_labelled_device(header, index, zone, chip, driver=driver)


def _create_labelled_device(
    header: Path,
    index: int,
    zone: str,
    chip: FakeChip | None,
    *,
    driver: str = DRIVER,
) -> None:
    (header / "name").write_text(f"{driver}\n", encoding="utf-8")
    (header / f"fan{index}_label").write_text(f"{zone}-header\n", encoding="utf-8")
    if chip is None:
        (header / f"pwm{index}").write_text("64\n", encoding="ascii")
        (header / f"pwm{index}_enable").write_text("2\n", encoding="ascii")
    else:
        chip.add(header, index, pwm=64, mode=2)


def create_symlinked_sysfs(root: Path, devices: Path, chip: FakeChip | None = None) -> None:
    """実機同様に class entry が /sys/devices 側を指す偽 sysfs を作る。"""
    for index, zone in enumerate(ZONES, start=1):
        header = devices / f"device{index}" / "hwmon" / f"hwmon{index}"
        header.mkdir(parents=True)
        _create_labelled_device(header, index, zone, chip)
        (root / f"hwmon{index}").symlink_to(header, target_is_directory=True)


def create_labelless_device(
    root: Path,
    chip: FakeChip,
    *,
    entry: str = "hwmon3",
    name: str = DRIVER,
    pwm: int = 64,
    mode: int = 5,
) -> Path:
    """label の無い1つの device に3 zone の channel を作る（導入先の ``nct6799`` の形）。"""
    device = root / entry
    device.mkdir()
    (device / "name").write_text(f"{name}\n", encoding="utf-8")
    for channel in LABELLESS_CHANNELS.values():
        chip.add(device, channel, pwm=pwm, mode=mode)
    return device


def create_unrelated_device(root: Path, entry: str, name: str) -> None:
    """別の driver の device（温度だけを持つ）。探し直しで候補にならない。"""
    device = root / entry
    device.mkdir()
    (device / "name").write_text(f"{name}\n", encoding="utf-8")
    (device / "temp1_input").write_text("42000\n", encoding="ascii")


def record() -> dict[str, Any]:
    headers: list[dict[str, Any]] = []
    for index, zone in enumerate(ZONES, start=1):
        base = f"hwmon{index}"
        headers.append(
            {
                "zone": zone,
                "name_path": f"{base}/name",
                "expected_name": DRIVER,
                "label_path": f"{base}/fan{index}_label",
                "expected_label": f"{zone}-header",
                "pwm_path": f"{base}/pwm{index}",
                "enable_path": f"{base}/pwm{index}_enable",
                "original_pwm": 64,
                "original_enable": 2,
            }
        )
    return {"schema_version": 1, "headers": headers}


def labelless_record(entry: str = "hwmon3") -> dict[str, Any]:
    """v2 の記録。label の無い header は ``label_path`` / ``expected_label`` が null。"""
    return {
        "schema_version": 2,
        "headers": [
            {
                "zone": zone,
                "name_path": f"{entry}/name",
                "expected_name": DRIVER,
                "label_path": None,
                "expected_label": None,
                "pwm_path": f"{entry}/pwm{channel}",
                "enable_path": f"{entry}/pwm{channel}_enable",
                "original_pwm": 64,
                "original_enable": 5,
            }
            for zone, channel in LABELLESS_CHANNELS.items()
        ],
    }


def write_record(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def read(path: Path) -> str:
    return path.read_text(encoding="ascii")


def assert_full_speed(device: Path, channel: int) -> None:
    """全速（``pwmN=255``。manual の 255 は ``0`` と読めるので enable は 0 か 1）。"""
    assert read(device / f"pwm{channel}") == "255\n"
    assert read(device / f"pwm{channel}_enable") in {"0\n", "1\n"}


# --- 確かめ済みの driver の一覧（0118 §2.3a） ---


def test_verified_driver_lists_match_between_handoff_and_control() -> None:
    """実行部はパッケージに依存しないので一覧を自分で持つ。2つの定数は同じでなければならない。"""
    assert handoff_module.VERIFIED_HWMON_DRIVERS == VERIFIED_HWMON_DRIVERS
    assert frozenset({"nct6799"}) == VERIFIED_HWMON_DRIVERS


def test_handoff_writes_only_full_speed_enable_and_max_pwm() -> None:
    """書ける値は ``pwmN_enable=0`` と ``pwmN=255`` だけ（0118 §2.3）。``1`` の定数は無い。"""
    assert handoff_module.HWMON_FULL_SPEED_ENABLE == "0\n"
    assert handoff_module.HWMON_MAX_PWM == "255\n"
    assert not hasattr(handoff_module, "HWMON_MANUAL_MODE")


# --- 書き方（0118 §2.3） ---


def test_missing_handoff_record_does_not_touch_sysfs(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)

    result = emergency_handoff(tmp_path / "missing.json", sysfs)

    assert result.record_found is False
    assert read(sysfs / "hwmon1/pwm1") == "64\n"


def test_auto_headers_get_full_speed_enable_without_writing_pwm(
    tmp_path: Path, chip: FakeChip
) -> None:
    """自動制御のまま pwm へ書けば EBUSY。enable=0 だけで全速になり、pwm には書かない。"""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ZONES
    assert result.identity_mismatch_zones == ()
    assert result.failed_zones == ()
    for index in range(1, 4):
        assert read(sysfs / f"hwmon{index}/pwm{index}") == "255\n"
        assert read(sysfs / f"hwmon{index}/pwm{index}_enable") == "0\n"
    assert chip.writes == [(f"pwm{index}_enable", b"0\n") for index in range(1, 4)]


def test_labelless_record_v2_resolves_the_unique_driver_device(
    tmp_path: Path, chip: FakeChip
) -> None:
    """label: null の header は、同じ name の device がちょうど1つのときに書く（0118 §2.2）。"""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_unrelated_device(sysfs, "hwmon0", "acpitz")
    create_unrelated_device(sysfs, "hwmon1", "nvme")
    device = create_labelless_device(sysfs, chip)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ZONES
    for channel in LABELLESS_CHANNELS.values():
        assert_full_speed(device, channel)


def test_manual_header_whose_enable_write_is_rejected_gets_max_pwm(
    tmp_path: Path, make_chip: ChipFactory
) -> None:
    """enable=0 が拒否されても、manual と読めれば pwm=255 を書く。manual の 255 は 0 と読める。"""
    chip = make_chip(reject_full_speed=True)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    device = create_labelless_device(sysfs, chip, pwm=40, mode=1)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ZONES
    for channel in LABELLESS_CHANNELS.values():
        assert read(device / f"pwm{channel}") == "255\n"
        # driver は manual の 255 を 0 と報告する。それでも成功に数える（0118 §2.3 の 3）。
        assert read(device / f"pwm{channel}_enable") == "0\n"
    assert chip.writes == [
        write
        for channel in LABELLESS_CHANNELS.values()
        for write in ((f"pwm{channel}_enable", b"0\n"), (f"pwm{channel}", b"255\n"))
    ]


def test_auto_header_whose_enable_write_is_rejected_is_not_written(
    tmp_path: Path, make_chip: ChipFactory
) -> None:
    """自動のまま pwm へ書いても後で自動制御が下げうる。pwm に書かず失敗とする。"""
    chip = make_chip(reject_full_speed=True)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    device = create_labelless_device(sysfs, chip, pwm=64, mode=5)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ()
    assert result.failed_zones == ZONES
    assert {(zone.status, zone.phase, zone.detail) for zone in result.zones} == {
        ("readback_mismatch", "enable", "OSError,enable_auto")
    }
    assert all(name.endswith("_enable") for name, _ in chip.writes)
    for channel in LABELLESS_CHANNELS.values():
        assert read(device / f"pwm{channel}") == "64\n"
        assert read(device / f"pwm{channel}_enable") == "5\n"


def test_full_speed_enable_without_max_pwm_is_a_failure_without_further_writes(
    tmp_path: Path, make_chip: ChipFactory
) -> None:
    """enable=0 を受けても pwm が 255 にならない driver では、manual でない限り書き足さない。"""
    chip = make_chip(full_speed_sets_pwm=False)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_labelless_device(sysfs, chip)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())

    result = emergency_handoff(handoff, sysfs)

    assert result.failed_zones == ZONES
    assert {(zone.status, zone.phase, zone.detail) for zone in result.zones} == {
        ("readback_mismatch", "enable", "enable_full_speed_pwm_not_max")
    }
    assert all(name.endswith("_enable") for name, _ in chip.writes)


def test_manual_readback_must_show_max_pwm(
    tmp_path: Path, make_chip: ChipFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """manual で pwm=255 を書いても読み戻しが 255 でなければ失敗。ほかの zone は続ける。"""
    chip = make_chip(reject_full_speed=True)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_labelless_device(sysfs, chip, pwm=40, mode=1)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())
    installed = handoff_module._write_exact
    ignored = False

    def ignore_front_pwm(fd: int, payload: bytes) -> None:
        nonlocal ignored
        if not ignored and payload == b"255\n":
            ignored = True
            return
        installed(fd, payload)

    monkeypatch.setattr(handoff_module, "_write_exact", ignore_front_pwm)

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ("rear", "top")
    assert result.failed_zones == ("front",)
    assert (result.zones[0].status, result.zones[0].phase, result.zones[0].detail) == (
        "readback_mismatch",
        "readback",
        "pwm",
    )


# --- 書く時点の探し直し（0118 §2.2） ---


def test_rebound_device_with_a_new_hwmon_number_still_gets_max(
    tmp_path: Path, chip: FakeChip
) -> None:
    """記録の hwmonN は監査用。再 bind で番号が変わっても、探し直した device へ書く。"""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    device = create_labelless_device(sysfs, chip, entry="hwmon7")
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record(entry="hwmon3"))

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ZONES
    for channel in LABELLESS_CHANNELS.values():
        assert_full_speed(device, channel)


def test_labelled_record_follows_the_label_after_a_rebind(tmp_path: Path, chip: FakeChip) -> None:
    """label のある header は name と label で探す。番号が入れ替わっても正しい device へ書く。"""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    # front の device が hwmon2、rear の device が hwmon1 に入れ替わった。
    for entry, index, zone in (("hwmon2", 1, "front"), ("hwmon1", 2, "rear"), ("hwmon3", 3, "top")):
        header = sysfs / entry
        header.mkdir()
        _create_labelled_device(header, index, zone, chip)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ZONES
    assert_full_speed(sysfs / "hwmon2", 1)
    assert_full_speed(sysfs / "hwmon1", 2)


def test_label_falls_back_to_pwm_label_when_fan_label_is_absent(
    tmp_path: Path, chip: FakeChip
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    for index, zone in enumerate(ZONES, start=1):
        (sysfs / f"hwmon{index}/fan{index}_label").unlink()
        (sysfs / f"hwmon{index}/pwm{index}_label").write_text(f"{zone}-header\n", encoding="utf-8")
    payload = record()
    for index, header in enumerate(payload["headers"], start=1):
        header["label_path"] = f"hwmon{index}/pwm{index}_label"
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ZONES


def test_two_devices_with_the_same_driver_name_are_not_written(
    tmp_path: Path, chip: FakeChip
) -> None:
    """label: null で同じ name の device が2つあれば一意に特定できない。どちらにも書かない。"""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    first = create_labelless_device(sysfs, chip, entry="hwmon3")
    second = create_labelless_device(sysfs, chip, entry="hwmon4")
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())

    result = emergency_handoff(handoff, sysfs)

    assert result.identity_mismatch_zones == ZONES
    assert {(zone.phase, zone.detail) for zone in result.zones} == {
        ("resolve", "multiple_matching_devices")
    }
    assert chip.writes == []
    for device in (first, second):
        for channel in LABELLESS_CHANNELS.values():
            assert read(device / f"pwm{channel}_enable") == "5\n"


def test_missing_driver_device_is_reported_per_zone(tmp_path: Path, chip: FakeChip) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_unrelated_device(sysfs, "hwmon0", "acpitz")
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())

    result = emergency_handoff(handoff, sysfs)

    assert result.identity_mismatch_zones == ZONES
    assert {(zone.phase, zone.detail) for zone in result.zones} == {
        ("resolve", "no_matching_device")
    }
    assert chip.writes == []


def test_unverified_driver_is_never_written(tmp_path: Path, chip: FakeChip) -> None:
    """一覧に無い driver では enable=0 が Fan を止めうる。その zone に書かない（0118 §2.3a）。"""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    device = create_labelless_device(sysfs, chip, name="pwm-fan")
    payload = labelless_record()
    for header in payload["headers"]:
        header["expected_name"] = "pwm-fan"
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ()
    assert result.failed_zones == ZONES
    assert {(zone.status, zone.phase, zone.detail) for zone in result.zones} == {
        ("unsupported_driver", "identity", "driver_not_verified")
    }
    assert chip.writes == []
    assert read(device / "pwm6_enable") == "5\n"


def test_unverified_driver_in_one_zone_does_not_skip_the_others(
    tmp_path: Path, chip: FakeChip
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    (sysfs / "hwmon2/name").write_text("pwm-fan\n", encoding="utf-8")
    payload = record()
    payload["headers"][1]["expected_name"] = "pwm-fan"
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ("front", "top")
    assert result.failed_zones == ("rear",)
    assert read(sysfs / "hwmon2/pwm2_enable") == "2\n"


def test_unreadable_candidate_name_fails_the_zone_instead_of_guessing(
    tmp_path: Path, chip: FakeChip
) -> None:
    """name を確かめられない device があれば一意とは言えない。その zone は書かずに失敗とする。"""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_labelless_device(sysfs, chip)
    broken = sysfs / "hwmon9"
    broken.mkdir()
    (broken / "name").mkdir()  # 通常ファイルでない name
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())

    result = emergency_handoff(handoff, sysfs)

    assert result.identity_mismatch_zones == ZONES
    assert {(zone.phase, zone.detail) for zone in result.zones} == {
        ("resolve", "unverifiable_device")
    }
    assert chip.writes == []


def test_unreadable_candidate_name_is_an_io_failure_of_the_zone(
    tmp_path: Path, chip: FakeChip, monkeypatch: pytest.MonkeyPatch
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_labelless_device(sysfs, chip)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())
    original = handoff_module._read_optional_attribute

    def eio_on_name(device_fd: int, name: str) -> str | None:
        if name == "name":
            raise OSError(errno.EIO, "injected")
        return original(device_fd, name)

    monkeypatch.setattr(handoff_module, "_read_optional_attribute", eio_on_name)

    result = emergency_handoff(handoff, sysfs)

    assert {(zone.status, zone.phase, zone.detail) for zone in result.zones} == {
        ("io_error", "resolve", "OSError")
    }
    assert result.failed_zones == ZONES
    assert chip.writes == []


# --- 記録の版（0118 §2.2） ---


def test_v1_record_cannot_omit_the_label(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    payload = labelless_record()
    payload["schema_version"] = 1
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="label_path"):
        emergency_handoff(handoff, sysfs)


@pytest.mark.parametrize(
    ("label_path", "expected_label"),
    [(None, "front-header"), ("hwmon3/fan6_label", None), ("", None)],
)
def test_v2_record_label_path_and_expected_label_are_null_together(
    tmp_path: Path, label_path: str | None, expected_label: str | None
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    payload = labelless_record()
    payload["headers"][0]["label_path"] = label_path
    payload["headers"][0]["expected_label"] = expected_label
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="label"):
        emergency_handoff(handoff, sysfs)


@pytest.mark.parametrize("version", [0, 3, "2", True])
def test_unknown_record_schema_versions_are_rejected(tmp_path: Path, version: object) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    payload = labelless_record()
    payload["schema_version"] = version
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="schema_version"):
        emergency_handoff(handoff, sysfs)


def test_v2_record_with_labels_is_read_like_v1(tmp_path: Path, chip: FakeChip) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    payload = record()
    payload["schema_version"] = 2
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    assert emergency_handoff(handoff, sysfs).applied_zones == ZONES


def test_zones_that_resolve_to_the_same_header_are_rejected_before_any_write(
    tmp_path: Path, chip: FakeChip
) -> None:
    """記録の hwmonN が違っても、探し直す組（driver・label・channel）が同じなら同じ header。"""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_labelless_device(sysfs, chip)
    payload = labelless_record()
    payload["headers"][1]["name_path"] = "hwmon4/name"
    payload["headers"][1]["pwm_path"] = "hwmon4/pwm6"
    payload["headers"][1]["enable_path"] = "hwmon4/pwm6_enable"
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="同じ header"):
        emergency_handoff(handoff, sysfs)

    assert chip.writes == []


# --- 記録の検査（0028 §2.7 / 0080 §2.5） ---


@pytest.mark.parametrize("identity", ["name", "label"])
def test_identity_mismatch_never_writes_that_header(
    tmp_path: Path, chip: FakeChip, identity: str
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    target = sysfs / ("hwmon2/name" if identity == "name" else "hwmon2/fan2_label")
    target.write_text("different\n", encoding="utf-8")
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    result = emergency_handoff(handoff, sysfs)

    assert result.identity_mismatch_zones == ("rear",)
    assert read(sysfs / "hwmon2/pwm2") == "64\n"
    assert read(sysfs / "hwmon2/pwm2_enable") == "2\n"
    assert read(sysfs / "hwmon1/pwm1") == "255\n"


def test_identity_changed_after_resolution_is_not_written(
    tmp_path: Path, chip: FakeChip, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探し直した後、書く前に name が変われば書かない（開いた fd で読み直して照合する）。"""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())
    original_apply = handoff_module._apply_open_header

    def rename_before_apply(item: Any) -> Any:
        if item.record.zone == "rear":
            (sysfs / "hwmon2/name").write_text("different\n", encoding="utf-8")
        return original_apply(item)

    monkeypatch.setattr(handoff_module, "_apply_open_header", rename_before_apply)

    result = emergency_handoff(handoff, sysfs)

    assert result.identity_mismatch_zones == ("rear",)
    assert result.zones[1].phase == "identity"
    assert read(sysfs / "hwmon2/pwm2_enable") == "2\n"


def test_realistic_hwmon_class_symlinks_are_resolved_per_device(
    tmp_path: Path, chip: FakeChip
) -> None:
    sysfs = tmp_path / "sys" / "class" / "hwmon"
    sysfs.mkdir(parents=True)
    devices = tmp_path / "sys" / "devices"
    create_symlinked_sysfs(sysfs, devices, chip)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ZONES
    for index in range(1, 4):
        assert_full_speed(devices / f"device{index}" / "hwmon" / f"hwmon{index}", index)


def test_record_cannot_redirect_a_write_outside_sysfs_root(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    outside = tmp_path / "outside"
    outside.write_text("do not change\n", encoding="utf-8")
    payload = record()
    payload["headers"][0]["pwm_path"] = "../outside"
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="handoff"):
        emergency_handoff(handoff, sysfs)

    assert outside.read_text(encoding="utf-8") == "do not change\n"
    assert read(sysfs / "hwmon1/pwm1") == "64\n"


def test_record_requires_exactly_three_unique_zones_and_fields(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    payload = record()
    payload["headers"][0]["unknown"] = True
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="未知"):
        emergency_handoff(handoff, sysfs)

    assert all(read(sysfs / f"hwmon{index}/pwm{index}") == "64\n" for index in range(1, 4))


def test_duplicate_write_targets_are_rejected_before_any_write(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    payload = record()
    payload["headers"][1]["name_path"] = "hwmon1/name"
    payload["headers"][1]["label_path"] = "hwmon1/fan1_label"
    payload["headers"][1]["pwm_path"] = "hwmon1/pwm1"
    payload["headers"][1]["enable_path"] = "hwmon1/pwm1_enable"
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="同じ header"):
        emergency_handoff(handoff, sysfs)

    assert read(sysfs / "hwmon1/pwm1") == "64\n"


def test_record_cannot_target_an_arbitrary_hwmon_attribute(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    arbitrary = sysfs / "hwmon1/temp1_input"
    arbitrary.write_text("42000\n", encoding="ascii")
    payload = record()
    payload["headers"][0]["pwm_path"] = "hwmon1/temp1_input"
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="PWM path"):
        emergency_handoff(handoff, sysfs)

    assert read(arbitrary) == "42000\n"


def test_label_and_pwm_must_identify_the_same_channel(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    (sysfs / "hwmon1/fan2_label").write_text("front-header\n", encoding="utf-8")
    payload = record()
    payload["headers"][0]["label_path"] = "hwmon1/fan2_label"
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="channel"):
        emergency_handoff(handoff, sysfs)

    assert read(sysfs / "hwmon1/pwm1") == "64\n"


def test_enable_must_identify_the_pwm_channel_without_a_label(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    payload = labelless_record()
    payload["headers"][0]["enable_path"] = "hwmon3/pwm5_enable"
    handoff = tmp_path / "handoff.json"
    write_record(handoff, payload)

    with pytest.raises(HandoffRecordError, match="enable path"):
        emergency_handoff(handoff, sysfs)


def test_attribute_symlink_cannot_escape_resolved_hwmon_device(
    tmp_path: Path, chip: FakeChip
) -> None:
    sysfs = tmp_path / "sys" / "class" / "hwmon"
    sysfs.mkdir(parents=True)
    devices = tmp_path / "sys" / "devices"
    create_symlinked_sysfs(sysfs, devices, chip)
    outside = tmp_path / "outside"
    outside.write_text("do not change\n", encoding="ascii")
    pwm = devices / "device1/hwmon/hwmon1/pwm1"
    pwm.unlink()
    pwm.symlink_to(outside)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    with pytest.raises(HandoffRecordError, match="symlink"):
        emergency_handoff(handoff, sysfs)

    assert read(outside) == "do not change\n"
    assert read(devices / "device2/hwmon/hwmon2/pwm2") == "64\n"
    assert chip.writes == []


def test_attribute_symlink_cannot_switch_channels_inside_the_device(
    tmp_path: Path, chip: FakeChip
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    alternate = sysfs / "hwmon1/pwm4"
    alternate.write_text("32\n", encoding="ascii")
    pwm = sysfs / "hwmon1/pwm1"
    pwm.unlink()
    pwm.symlink_to(alternate)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    with pytest.raises(HandoffRecordError, match="symlink"):
        emergency_handoff(handoff, sysfs)

    assert read(alternate) == "32\n"
    assert read(sysfs / "hwmon2/pwm2") == "64\n"
    assert chip.writes == []


def test_one_zone_io_failure_does_not_skip_remaining_max_attempts(
    tmp_path: Path, chip: FakeChip
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    (sysfs / "hwmon1/pwm1_enable").unlink()
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ("rear", "top")
    assert result.failed_zones == ("front",)
    assert result.zones[0].status == "io_error"
    assert result.zones[0].phase == "resolve"
    assert result.zones[0].detail == "FileNotFoundError"
    assert read(sysfs / "hwmon2/pwm2") == "255\n"
    assert read(sysfs / "hwmon3/pwm3") == "255\n"


def test_ignored_enable_write_is_a_structured_failure_and_other_zones_continue(
    tmp_path: Path,
    chip: FakeChip,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())
    installed = handoff_module._write_exact
    ignored = False

    def ignore_write(fd: int, payload: bytes) -> None:
        nonlocal ignored
        if not ignored and payload == b"0\n":
            ignored = True
            return
        installed(fd, payload)

    monkeypatch.setattr(handoff_module, "_write_exact", ignore_write)

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ("rear", "top")
    assert result.failed_zones == ("front",)
    assert result.zones[0].status == "readback_mismatch"
    assert result.zones[0].phase == "enable"
    assert result.zones[0].detail == "enable_auto"
    assert read(sysfs / "hwmon1/pwm1_enable") == "2\n"
    assert read(sysfs / "hwmon1/pwm1") == "64\n"


def test_path_swap_after_resolution_cannot_redirect_open_pwm_fd(
    tmp_path: Path,
    make_chip: ChipFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chip = make_chip(reject_full_speed=True)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    device = create_labelless_device(sysfs, chip, pwm=40, mode=1)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, labelless_record())
    outside = tmp_path / "outside"
    outside.write_text("do not change\n", encoding="ascii")
    original_apply: Callable[[Any], Any] = handoff_module._apply_open_header

    def swap_before_apply(item: Any) -> Any:
        if item.record.zone == "front":
            pwm_path = device / "pwm6"
            pwm_path.rename(device / "pwm6.orig")
            pwm_path.symlink_to(outside)
        return original_apply(item)

    monkeypatch.setattr(handoff_module, "_apply_open_header", swap_before_apply)

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ZONES
    assert read(outside) == "do not change\n"
    assert read(device / "pwm6.orig") == "255\n"


@pytest.mark.parametrize("kind", ["dangling_symlink", "fifo", "oversize"])
def test_nonregular_or_oversize_handoff_record_fails_without_blocking(
    tmp_path: Path,
    kind: str,
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    handoff = tmp_path / "handoff.json"
    if kind == "dangling_symlink":
        handoff.symlink_to("missing.json")
    elif kind == "fifo":
        os.mkfifo(handoff)
    else:
        handoff.write_bytes(b" " * (64 * 1024 + 1))

    with pytest.raises(HandoffRecordError):
        emergency_handoff(handoff, sysfs)


def test_handoff_record_replacement_between_lstat_and_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    handoff = tmp_path / "handoff.json"
    replacement = tmp_path / "replacement.json"
    write_record(handoff, record())
    write_record(replacement, record())
    original_open = os.open
    replaced = False

    def replace_before_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if not replaced and os.fspath(path) == str(handoff):
            replaced = True
            os.replace(replacement, handoff)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_open)

    with pytest.raises(HandoffRecordError, match="置き換わった"):
        emergency_handoff(handoff, sysfs)


def test_standalone_entrypoint_imports_only_the_standard_library() -> None:
    project_root = Path(__file__).parents[1]
    source_path = project_root / "src/coldaisle/safety_handoff.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots = {
        node.names[0].name.split(".", maxsplit=1)[0]
        if isinstance(node, ast.Import)
        else (node.module or "").split(".", maxsplit=1)[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    assert imported_roots <= {
        "__future__",
        "contextlib",
        "dataclasses",
        "errno",
        "json",
        "logging",
        "os",
        "pathlib",
        "re",
        "stat",
        "typing",
    }
    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["coldaisle-safety-handoff"] == (
        "coldaisle.safety_handoff:main"
    )


def test_entrypoint_reports_partial_handoff_failure(
    tmp_path: Path,
    chip: FakeChip,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs, chip)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())
    installed = handoff_module._write_exact
    enable_writes = 0

    def fail_rear_enable(fd: int, payload: bytes) -> None:
        nonlocal enable_writes
        if payload == b"0\n":
            enable_writes += 1
        if payload == b"0\n" and enable_writes == 2:
            raise OSError("injected enable failure")
        installed(fd, payload)

    monkeypatch.setattr(handoff_module, "_write_exact", fail_rear_enable)
    monkeypatch.setattr(handoff_module, "HANDOFF_RECORD_PATH", handoff)
    monkeypatch.setattr(handoff_module, "HWMON_ROOT", sysfs)
    caplog.set_level(logging.INFO, logger="coldaisle.safety_handoff")

    assert handoff_module.main() == 1
    assert read(sysfs / "hwmon1/pwm1") == "255\n"
    assert read(sysfs / "hwmon2/pwm2") == "64\n"
    assert read(sysfs / "hwmon3/pwm3") == "255\n"
    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "safety_handoff_completed"
    assert payload["success"] is False
    assert payload["zones"] == [
        {"zone": "front", "status": "applied", "phase": "complete", "detail": ""},
        {
            "zone": "rear",
            "status": "readback_mismatch",
            "phase": "enable",
            "detail": "OSError,enable_auto",
        },
        {"zone": "top", "status": "applied", "phase": "complete", "detail": ""},
    ]


def test_entrypoint_reports_record_validation_failure_as_structured_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    handoff = tmp_path / "handoff.json"
    handoff.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(handoff_module, "HANDOFF_RECORD_PATH", handoff)
    monkeypatch.setattr(handoff_module, "HWMON_ROOT", sysfs)
    caplog.set_level(logging.INFO, logger="coldaisle.safety_handoff")

    assert handoff_module.main() == 1

    payload = json.loads(caplog.records[-1].message)
    assert payload == {
        "event": "safety_handoff_failed",
        "record_found": None,
        "success": False,
        "failure": {
            "phase": "record_or_root_validation",
            "detail": "HandoffRecordError",
        },
        "zones": [],
    }


def test_entrypoint_does_not_treat_a_record_symlink_loop_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    handoff = tmp_path / "handoff.json"
    handoff.symlink_to(handoff.name)
    monkeypatch.setattr(handoff_module, "HANDOFF_RECORD_PATH", handoff)
    monkeypatch.setattr(handoff_module, "HWMON_ROOT", sysfs)
    caplog.set_level(logging.INFO, logger="coldaisle.safety_handoff")

    assert handoff_module.main() == 1

    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "safety_handoff_failed"
    assert payload["failure"] == {
        "phase": "record_or_root_validation",
        "detail": "HandoffRecordError",
    }


def test_entrypoint_does_not_hide_unexpected_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_record: Path, _root: Path) -> handoff_module.HandoffResult:
        raise RuntimeError("injected bug")

    monkeypatch.setattr(handoff_module, "emergency_handoff", unexpected)

    with pytest.raises(RuntimeError, match="injected bug"):
        handoff_module.main()


# --- 0080 §2.10 段階 3: ExecStopPost と同じ `python -I -S <ファイル>` での起動 ---

_ISOLATED_RUN_TIMEOUT_S = 30
"""subprocess が戻らないときに試験を止めるための上限（Safety の値ではない）。"""

_RECORD_CONSTANT_LINE = 'HANDOFF_RECORD_PATH = Path("/run/coldaisle/fan-handoff.json")'
_HWMON_CONSTANT_LINE = 'HWMON_ROOT = Path("/sys/class/hwmon")'


def _standalone_source() -> Path:
    """ExecStopPost が指すのと同じ相対位置（``src/coldaisle/safety_handoff.py``）のソース。"""
    source = Path(__file__).parents[1] / "src/coldaisle/safety_handoff.py"
    # 写しの元が、import して試験しているモジュールと同じ実体であること。
    assert source.samefile(handoff_module.__file__)
    return source


def _isolated_copy(tmp_path: Path) -> tuple[Path, Path, Path]:
    """固定の path の定数2つだけを ``tmp_path`` の下へ書き換えた実行部の写しを作る。

    実行部は引数を取らず固定の path を読む（0080 §2.5）。本物のファイルをそのまま
    subprocess で走らせると、存在の確認の後に fand が記録を作った場合に本物の
    ``/sys/class/hwmon`` へ書きうる（確認と起動の間の競合）。写しは live の path を
    文字として持たないので、子は本物の記録も hwmon にも届かない。

    写しが元と**定数の2行だけ**違うことを確かめるので、子が走らせるのは本物の
    ファイルのコードそのものである。戻り値は（写し・記録の path・偽の sysfs）。
    """
    source = _standalone_source()
    original = source.read_text(encoding="utf-8")
    original_lines = original.splitlines(keepends=True)
    for constant in (_RECORD_CONSTANT_LINE, _HWMON_CONSTANT_LINE):
        assert [line.rstrip("\n") for line in original_lines].count(constant) == 1

    record_path = tmp_path / "run" / "fan-handoff.json"
    record_path.parent.mkdir()
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    replacements = {
        _RECORD_CONSTANT_LINE: f"HANDOFF_RECORD_PATH = Path({str(record_path)!r})",
        _HWMON_CONSTANT_LINE: f"HWMON_ROOT = Path({str(sysfs)!r})",
    }
    copied_lines = [
        replacements[line.rstrip("\n")] + "\n" if line.rstrip("\n") in replacements else line
        for line in original_lines
    ]
    copied = "".join(copied_lines)

    # 違うのは定数の2行だけで、それ以外は1行も変えていない。
    differing = [
        (before, after)
        for before, after in zip(original_lines, copied_lines, strict=True)
        if before != after
    ]
    assert [before.rstrip("\n") for before, _ in differing] == [
        _RECORD_CONSTANT_LINE,
        _HWMON_CONSTANT_LINE,
    ]
    # 写しには live の path が文字として残っていない（docstring の説明文も含めて）。
    assert "/run/coldaisle" not in copied
    assert "/sys/" not in copied.replace(str(sysfs), "")

    executable_dir = tmp_path / "exec"
    executable_dir.mkdir()
    copy = executable_dir / "safety_handoff.py"
    copy.write_text(copied, encoding="utf-8")
    return copy, record_path, sysfs


def _run_isolated(script: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_ISOLATED_RUN_TIMEOUT_S,
        check=False,
    )


def _single_event(completed: subprocess.CompletedProcess[str]) -> Any:
    assert completed.stdout == ""
    lines = completed.stderr.splitlines()
    assert len(lines) == 1, completed.stderr
    return json.loads(lines[0])


def _assert_absent_record_completion(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 0, completed.stderr
    assert _single_event(completed) == {
        "event": "safety_handoff_completed",
        "record_found": False,
        "success": True,
        "zones": [],
    }


def _sysfs_snapshot(sysfs: Path) -> dict[str, str]:
    return {
        str(path.relative_to(sysfs)): path.read_text(encoding="utf-8")
        for path in sorted(sysfs.rglob("*"))
        if path.is_file()
    }


def test_isolated_no_site_entrypoint_exits_zero_without_record(tmp_path: Path) -> None:
    """記録の無い環境では `python -I -S <ファイル>` で起動し、何も書かずに 0 で終わる。

    venv と ``coldaisle`` パッケージに依存しないこと（0028 §2.7「壊れた環境でも動く」、
    0080 §2.5 / §2.10 の段階 3）を、systemd の ``ExecStopPost`` と同じ起動方法で確かめる。
    """
    script, record_path, sysfs = _isolated_copy(tmp_path)
    create_sysfs(sysfs)
    before = _sysfs_snapshot(sysfs)
    workdir = tmp_path / "cwd"
    workdir.mkdir()

    completed = _run_isolated(script, workdir, env={"PATH": os.environ.get("PATH", "")})

    _assert_absent_record_completion(completed)
    assert not record_path.exists()
    assert _sysfs_snapshot(sysfs) == before
    # 作業ディレクトリにも、写しの隣にも何も作らない（bytecode・ログファイルなど）。
    assert list(workdir.iterdir()) == []
    assert list(script.parent.iterdir()) == [script]


def test_isolated_no_site_entrypoint_writes_max_from_record(tmp_path: Path) -> None:
    """記録があれば、`-I -S` の下でも探し直した header に ``pwmN_enable=0`` を書いて 0 で終わる。

    子の process には :class:`FakeChip` を差し込めないので、偽の sysfs は普通のファイルで、
    fand が Max（manual の 255）にした後の状態から始める。v2 の label の無い記録を使う。
    """
    script, record_path, sysfs = _isolated_copy(tmp_path)
    device = sysfs / "hwmon7"
    device.mkdir()
    (device / "name").write_text(f"{DRIVER}\n", encoding="utf-8")
    for channel in LABELLESS_CHANNELS.values():
        (device / f"pwm{channel}").write_text("255\n", encoding="ascii")
        (device / f"pwm{channel}_enable").write_text("1\n", encoding="ascii")
    write_record(record_path, labelless_record(entry="hwmon3"))
    workdir = tmp_path / "cwd"
    workdir.mkdir()

    completed = _run_isolated(script, workdir, env={"PATH": os.environ.get("PATH", "")})

    assert completed.returncode == 0, completed.stderr
    payload = _single_event(completed)
    assert payload["event"] == "safety_handoff_completed"
    assert payload["record_found"] is True
    assert payload["success"] is True
    assert [zone["status"] for zone in payload["zones"]] == ["applied"] * 3
    for channel in LABELLESS_CHANNELS.values():
        assert read(device / f"pwm{channel}") == "255\n"
        assert read(device / f"pwm{channel}_enable") == "0\n"


def test_isolated_entrypoint_ignores_environment_and_cwd_module_shadows(tmp_path: Path) -> None:
    """`-I -S` により、環境変数・作業ディレクトリ・site の差し込みを読まない。

    ``ExecStopPost`` は root で動くので、``PYTHONPATH`` や作業ディレクトリに置かれた
    同名のモジュール、``sitecustomize`` が読まれると、root でのコード実行の経路になる。
    どれかが読まれれば印のファイルが作られ、終了コードも 0 でなくなる。
    """
    script, _record_path, _sysfs = _isolated_copy(tmp_path)
    marker = tmp_path / "shadow-imported"
    shadow_body = (
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text(__name__, encoding='utf-8')\n"
        "raise SystemExit(97)\n"
    )
    shadow_dirs = (tmp_path / "pythonpath", tmp_path / "cwd")
    for directory in shadow_dirs:
        directory.mkdir()
        for module in ("json", "logging", "re", "stat", "sitecustomize", "usercustomize"):
            (directory / f"{module}.py").write_text(shadow_body, encoding="utf-8")
        package = directory / "coldaisle"
        package.mkdir()
        (package / "__init__.py").write_text(shadow_body, encoding="utf-8")
    startup = tmp_path / "startup.py"
    startup.write_text(shadow_body, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(shadow_dirs[0]),
        "PYTHONSTARTUP": str(startup),
        "PYTHONHOME": str(tmp_path / "no-such-home"),
        "PYTHONUSERBASE": str(shadow_dirs[0]),
    }

    completed = _run_isolated(script, shadow_dirs[1], env=env)

    _assert_absent_record_completion(completed)
    assert not marker.exists()
