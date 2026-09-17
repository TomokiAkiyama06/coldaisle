"""#78 異常停止後の emergency Max handoff を偽 sysfs で検証する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import coldaisle.safety_handoff as handoff_module
from coldaisle.control.safety import HandoffRecordError, emergency_handoff


def create_sysfs(root: Path) -> None:
    for index, zone in enumerate(("front", "rear", "top"), start=1):
        header = root / f"hwmon{index}"
        header.mkdir()
        (header / "name").write_text("test-driver\n", encoding="utf-8")
        (header / f"fan{index}_label").write_text(f"{zone}-header\n", encoding="utf-8")
        (header / f"pwm{index}").write_text("64\n", encoding="ascii")
        (header / f"pwm{index}_enable").write_text("2\n", encoding="ascii")


def create_symlinked_sysfs(root: Path, devices: Path) -> None:
    """実機同様に class entry が /sys/devices 側を指す偽 sysfs を作る。"""
    for index, zone in enumerate(("front", "rear", "top"), start=1):
        header = devices / f"device{index}" / "hwmon" / f"hwmon{index}"
        header.mkdir(parents=True)
        (header / "name").write_text("test-driver\n", encoding="utf-8")
        (header / f"fan{index}_label").write_text(f"{zone}-header\n", encoding="utf-8")
        (header / f"pwm{index}").write_text("64\n", encoding="ascii")
        (header / f"pwm{index}_enable").write_text("2\n", encoding="ascii")
        (root / f"hwmon{index}").symlink_to(header, target_is_directory=True)


def record() -> dict[str, Any]:
    headers: list[dict[str, Any]] = []
    for index, zone in enumerate(("front", "rear", "top"), start=1):
        base = f"hwmon{index}"
        headers.append(
            {
                "zone": zone,
                "name_path": f"{base}/name",
                "expected_name": "test-driver",
                "label_path": f"{base}/fan{index}_label",
                "expected_label": f"{zone}-header",
                "pwm_path": f"{base}/pwm{index}",
                "enable_path": f"{base}/pwm{index}_enable",
                "original_pwm": 64,
                "original_enable": 2,
            }
        )
    return {"schema_version": 1, "headers": headers}


def write_record(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_handoff_record_does_not_touch_sysfs(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)

    result = emergency_handoff(tmp_path / "missing.json", sysfs)

    assert result.record_found is False
    assert (sysfs / "hwmon1/pwm1").read_text(encoding="ascii") == "64\n"


def test_matching_headers_are_set_to_constant_max_then_manual(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ("front", "rear", "top")
    assert result.identity_mismatch_zones == ()
    for index in range(1, 4):
        assert (sysfs / f"hwmon{index}/pwm{index}").read_text(encoding="ascii") == "255\n"
        assert (sysfs / f"hwmon{index}/pwm{index}_enable").read_text(encoding="ascii") == "1\n"


def test_realistic_hwmon_class_symlinks_are_resolved_per_device(tmp_path: Path) -> None:
    sysfs = tmp_path / "sys" / "class" / "hwmon"
    sysfs.mkdir(parents=True)
    devices = tmp_path / "sys" / "devices"
    create_symlinked_sysfs(sysfs, devices)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ("front", "rear", "top")
    for index in range(1, 4):
        device = devices / f"device{index}" / "hwmon" / f"hwmon{index}"
        assert (device / f"pwm{index}").read_text(encoding="ascii") == "255\n"
        assert (device / f"pwm{index}_enable").read_text(encoding="ascii") == "1\n"


@pytest.mark.parametrize("identity", ["name", "label"])
def test_identity_mismatch_never_writes_that_header(tmp_path: Path, identity: str) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    target = sysfs / ("hwmon2/name" if identity == "name" else "hwmon2/fan2_label")
    target.write_text("different\n", encoding="utf-8")
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    result = emergency_handoff(handoff, sysfs)

    assert result.identity_mismatch_zones == ("rear",)
    assert (sysfs / "hwmon2/pwm2").read_text(encoding="ascii") == "64\n"
    assert (sysfs / "hwmon2/pwm2_enable").read_text(encoding="ascii") == "2\n"
    assert (sysfs / "hwmon1/pwm1").read_text(encoding="ascii") == "255\n"


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
    assert (sysfs / "hwmon1/pwm1").read_text(encoding="ascii") == "64\n"


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

    assert all(
        (sysfs / f"hwmon{index}/pwm{index}").read_text(encoding="ascii") == "64\n"
        for index in range(1, 4)
    )


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

    assert (sysfs / "hwmon1/pwm1").read_text(encoding="ascii") == "64\n"


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

    assert arbitrary.read_text(encoding="ascii") == "42000\n"


def test_attribute_symlink_cannot_escape_resolved_hwmon_device(tmp_path: Path) -> None:
    sysfs = tmp_path / "sys" / "class" / "hwmon"
    sysfs.mkdir(parents=True)
    devices = tmp_path / "sys" / "devices"
    create_symlinked_sysfs(sysfs, devices)
    outside = tmp_path / "outside"
    outside.write_text("do not change\n", encoding="ascii")
    pwm = devices / "device1/hwmon/hwmon1/pwm1"
    pwm.unlink()
    pwm.symlink_to(outside)
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    with pytest.raises(HandoffRecordError, match="device 外"):
        emergency_handoff(handoff, sysfs)

    assert outside.read_text(encoding="ascii") == "do not change\n"
    assert (devices / "device2/hwmon/hwmon2/pwm2").read_text(encoding="ascii") == "64\n"


def test_one_zone_io_failure_does_not_skip_remaining_max_attempts(tmp_path: Path) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    (sysfs / "hwmon1/pwm1_enable").unlink()
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())

    result = emergency_handoff(handoff, sysfs)

    assert result.applied_zones == ("rear", "top")
    assert result.failed_zones == ("front",)
    assert result.zones[0].status == "io_error"
    assert result.zones[0].detail == "FileNotFoundError"
    assert (sysfs / "hwmon2/pwm2").read_text(encoding="ascii") == "255\n"
    assert (sysfs / "hwmon3/pwm3").read_text(encoding="ascii") == "255\n"


def test_entrypoint_reports_partial_handoff_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    create_sysfs(sysfs)
    (sysfs / "hwmon2/pwm2").unlink()
    handoff = tmp_path / "handoff.json"
    write_record(handoff, record())
    monkeypatch.setattr(handoff_module, "HANDOFF_RECORD_PATH", handoff)
    monkeypatch.setattr(handoff_module, "HWMON_ROOT", sysfs)

    assert handoff_module.main() == 1
    assert (sysfs / "hwmon1/pwm1").read_text(encoding="ascii") == "255\n"
    assert (sysfs / "hwmon3/pwm3").read_text(encoding="ascii") == "255\n"
