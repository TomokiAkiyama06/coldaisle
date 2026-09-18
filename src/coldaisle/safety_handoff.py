"""fan daemon 停止後にだけ動かす emergency Max handoff。

決定記録 0028 §2.7 に従い、このモジュールは標準ライブラリ以外に依存せず、
Safety Config やモデルを読まない。書ける値は hwmon ABI の Max ``255`` と
manual mode ``1`` だけである。呼び出し側は、fan daemon の writer が停止した
ことを systemd で保証してから実行する。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

HWMON_MAX_PWM = "255\n"
HWMON_MANUAL_MODE = "1\n"
HANDOFF_RECORD_PATH = Path("/run/coldaisle/fan-handoff.json")
HWMON_ROOT = Path("/sys/class/hwmon")
_ZONES = frozenset({"front", "rear", "top"})
_MAX_RECORD_BYTES = 64 * 1024
_MAX_ATTRIBUTE_BYTES = 4 * 1024
_HEADER_FIELDS = frozenset(
    {
        "zone",
        "name_path",
        "expected_name",
        "label_path",
        "expected_label",
        "pwm_path",
        "enable_path",
        "original_pwm",
        "original_enable",
    }
)


class HandoffRecordError(ValueError):
    """handoff record が不正、または sysfs root 外を指している。"""


@dataclass(frozen=True, slots=True)
class _HeaderRecord:
    zone: str
    name_path: str
    expected_name: str
    label_path: str
    expected_label: str
    pwm_path: str
    enable_path: str
    original_pwm: int
    original_enable: int


@dataclass(frozen=True, slots=True)
class _OpenedHeader:
    record: _HeaderRecord
    name_fd: int
    label_fd: int
    pwm_read_fd: int
    pwm_write_fd: int
    enable_read_fd: int
    enable_write_fd: int
    pwm_identity: tuple[int, int]
    enable_identity: tuple[int, int]

    @property
    def fds(self) -> tuple[int, ...]:
        return (
            self.name_fd,
            self.label_fd,
            self.pwm_read_fd,
            self.pwm_write_fd,
            self.enable_read_fd,
            self.enable_write_fd,
        )


@dataclass(frozen=True, slots=True)
class HandoffZoneResult:
    """1 zone の Max handoff 結果。"""

    zone: str
    status: Literal["applied", "identity_mismatch", "io_error", "readback_mismatch"]
    phase: Literal["resolve", "identity", "pwm", "enable", "readback", "complete"]
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HandoffResult:
    """emergency handoff の全 zone 結果。書き込み値は受け取らない。"""

    record_found: bool
    zones: tuple[HandoffZoneResult, ...] = ()

    @property
    def applied_zones(self) -> tuple[str, ...]:
        """Max と manual mode の両方を書けた zone。"""
        return tuple(result.zone for result in self.zones if result.status == "applied")

    @property
    def identity_mismatch_zones(self) -> tuple[str, ...]:
        """driver name / label が record と一致しなかった zone。"""
        return tuple(result.zone for result in self.zones if result.status == "identity_mismatch")

    @property
    def failed_zones(self) -> tuple[str, ...]:
        """I/O 失敗または readback 不一致で handoff を完了できなかった zone。"""
        return tuple(
            result.zone
            for result in self.zones
            if result.status in {"io_error", "readback_mismatch"}
        )


def emergency_handoff(record_path: Path, sysfs_root: Path) -> HandoffResult:
    """record で特定済みの header だけを Max の manual mode にする。

    record が無ければ制御を取っていないため何もしない。driver ``name`` または
    header label が一致しない zone も書き込まない。一致した zone は PWM を先に
    Max へ上げ、その後 manual mode に切り替える。
    """
    record_text = _read_record_once(record_path)
    if record_text is None:
        return HandoffResult(record_found=False)

    headers = _load_record(record_text)
    for header in headers:
        _validate_header_paths(header)
    _reject_duplicate_relative_targets(headers)

    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    with ExitStack() as stack:
        root_fd = os.open(sysfs_root, root_flags)
        stack.callback(os.close, root_fd)
        opened: dict[str, _OpenedHeader | OSError] = {}
        for header in headers:
            try:
                item = _open_header(header, root_fd)
            except HandoffRecordError:
                # 不安全な path が1つでもあれば、一部を書く前に record 全体を拒否する。
                raise
            except OSError as exc:
                opened[header.zone] = exc
            else:
                opened[header.zone] = item
                for fd in item.fds:
                    stack.callback(os.close, fd)
        _reject_duplicate_open_targets(
            tuple(item for item in opened.values() if isinstance(item, _OpenedHeader))
        )

        results: list[HandoffZoneResult] = []
        for header in headers:
            opened_item = opened[header.zone]
            if isinstance(opened_item, OSError):
                results.append(
                    HandoffZoneResult(
                        zone=header.zone,
                        status="io_error",
                        phase="resolve",
                        detail=type(opened_item).__name__,
                    )
                )
                continue
            result = _apply_open_header(opened_item)
            results.append(result)
        return HandoffResult(record_found=True, zones=tuple(results))


def _apply_open_header(item: _OpenedHeader) -> HandoffZoneResult:
    header = item.record
    try:
        name = _read_fd_text(item.name_fd)
        label = _read_fd_text(item.label_fd)
    except (OSError, UnicodeError) as exc:
        return HandoffZoneResult(
            zone=header.zone,
            status="io_error",
            phase="identity",
            detail=type(exc).__name__,
        )
    if name != header.expected_name or label != header.expected_label:
        return HandoffZoneResult(
            zone=header.zone,
            status="identity_mismatch",
            phase="identity",
            detail="driver_or_label",
        )

    # auto のまま PWM を Max に上げ、readback=255 を確認してからだけ manual にする。
    try:
        _write_exact(item.pwm_write_fd, HWMON_MAX_PWM.encode("ascii"))
    except OSError as exc:
        return HandoffZoneResult(
            zone=header.zone,
            status="io_error",
            phase="pwm",
            detail=type(exc).__name__,
        )
    try:
        pwm_readback = _read_fd_text(item.pwm_read_fd)
    except (OSError, UnicodeError) as exc:
        return HandoffZoneResult(
            zone=header.zone,
            status="io_error",
            phase="readback",
            detail=type(exc).__name__,
        )
    if pwm_readback != HWMON_MAX_PWM.strip():
        return HandoffZoneResult(
            zone=header.zone,
            status="readback_mismatch",
            phase="readback",
            detail="pwm",
        )

    try:
        _write_exact(item.enable_write_fd, HWMON_MANUAL_MODE.encode("ascii"))
    except OSError as exc:
        return HandoffZoneResult(
            zone=header.zone,
            status="io_error",
            phase="enable",
            detail=type(exc).__name__,
        )
    try:
        final_pwm = _read_fd_text(item.pwm_read_fd)
        enable_readback = _read_fd_text(item.enable_read_fd)
    except (OSError, UnicodeError) as exc:
        return HandoffZoneResult(
            zone=header.zone,
            status="io_error",
            phase="readback",
            detail=type(exc).__name__,
        )
    mismatch = []
    if final_pwm != HWMON_MAX_PWM.strip():
        mismatch.append("pwm")
    if enable_readback != HWMON_MANUAL_MODE.strip():
        mismatch.append("enable")
    if mismatch:
        return HandoffZoneResult(
            zone=header.zone,
            status="readback_mismatch",
            phase="readback",
            detail=",".join(mismatch),
        )
    return HandoffZoneResult(zone=header.zone, status="applied", phase="complete")


def main() -> int:
    """systemd ``ExecStopPost`` 向けの固定入力 entry point。

    書き込む値や対象 root を引数で差し替える経路は持たない。identity
    mismatch は対象外 header へ書かず、非0で systemd へ通知する。
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("coldaisle.safety_handoff")
    try:
        result = emergency_handoff(HANDOFF_RECORD_PATH, HWMON_ROOT)
    except (HandoffRecordError, OSError) as exc:
        logger.error(
            _json_event(
                {
                    "event": "safety_handoff_failed",
                    "record_found": None,
                    "success": False,
                    "failure": {
                        "phase": "record_or_root_validation",
                        "detail": type(exc).__name__,
                    },
                    "zones": [],
                }
            )
        )
        return 1

    failed = bool(result.identity_mismatch_zones or result.failed_zones)
    logger.log(
        logging.ERROR if failed else logging.INFO,
        _json_event(
            {
                "event": "safety_handoff_completed",
                "record_found": result.record_found,
                "success": not failed,
                "zones": [
                    {
                        "zone": zone.zone,
                        "status": zone.status,
                        "phase": zone.phase,
                        "detail": zone.detail,
                    }
                    for zone in result.zones
                ],
            },
        ),
    )
    return 1 if failed else 0


def _json_event(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _read_record_once(path: Path) -> str | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise HandoffRecordError("handoff record は通常ファイルにする")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HandoffRecordError("handoff record を読めない") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise HandoffRecordError("handoff record は通常ファイルにする")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise HandoffRecordError("handoff record が検査中に置き換わった")
        if opened.st_size > _MAX_RECORD_BYTES:
            raise HandoffRecordError("handoff record が大きすぎる")
        payload = bytearray()
        while len(payload) <= _MAX_RECORD_BYTES:
            chunk = os.read(fd, min(8192, _MAX_RECORD_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _MAX_RECORD_BYTES:
            raise HandoffRecordError("handoff record が大きすぎる")
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeError as exc:
            raise HandoffRecordError("handoff record をUTF-8として読めない") from exc
    finally:
        os.close(fd)


def _load_record(text: str) -> tuple[_HeaderRecord, ...]:
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HandoffRecordError("handoff record をJSONとして読めない") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "headers"}:
        raise HandoffRecordError("handoff record の top-level 形式が不正")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise HandoffRecordError("handoff record の schema_version が不正")
    entries = raw["headers"]
    if not isinstance(entries, list) or len(entries) != len(_ZONES):
        raise HandoffRecordError("handoff record に3 zoneすべてが必要")

    headers = tuple(_parse_header(entry) for entry in entries)
    zones = {header.zone for header in headers}
    if zones != _ZONES:
        raise HandoffRecordError("handoff record の zone は front/rear/top を1つずつにする")
    return headers


def _parse_header(raw: object) -> _HeaderRecord:
    if not isinstance(raw, dict) or set(raw) != _HEADER_FIELDS:
        raise HandoffRecordError("handoff header の欠落・未知フィールド")
    for name in (
        "zone",
        "name_path",
        "expected_name",
        "label_path",
        "expected_label",
        "pwm_path",
        "enable_path",
    ):
        if not isinstance(raw[name], str) or not raw[name] or len(raw[name]) > 500:
            raise HandoffRecordError(f"handoff header.{name} が不正")
    for name in ("original_pwm", "original_enable"):
        if type(raw[name]) is not int or not 0 <= raw[name] <= 255:
            raise HandoffRecordError(f"handoff header.{name} が不正")
    return _HeaderRecord(
        zone=raw["zone"],
        name_path=raw["name_path"],
        expected_name=raw["expected_name"],
        label_path=raw["label_path"],
        expected_label=raw["expected_label"],
        pwm_path=raw["pwm_path"],
        enable_path=raw["enable_path"],
        original_pwm=raw["original_pwm"],
        original_enable=raw["original_enable"],
    )


def _validate_header_paths(header: _HeaderRecord) -> None:
    relative_paths = tuple(
        Path(value)
        for value in (
            header.name_path,
            header.label_path,
            header.pwm_path,
            header.enable_path,
        )
    )
    if any(path.is_absolute() or len(path.parts) != 2 for path in relative_paths):
        raise HandoffRecordError("handoff path は hwmon class entry 直下の相対 path にする")
    class_entries = {path.parts[0] for path in relative_paths}
    if len(class_entries) != 1 or re.fullmatch(r"hwmon[0-9]+", next(iter(class_entries))) is None:
        raise HandoffRecordError("handoff header は1つの hwmon class entry を指す")
    name_path, label_path, pwm_path, enable_path = relative_paths
    label_match = re.fullmatch(r"(?:fan|pwm)([1-9][0-9]*)_label", label_path.name)
    if name_path.name != "name" or label_match is None:
        raise HandoffRecordError("handoff header の identity path が hwmon 形式ではない")
    pwm_match = re.fullmatch(r"pwm([1-9][0-9]*)", pwm_path.name)
    if pwm_match is None:
        raise HandoffRecordError("handoff header の PWM path が hwmon 形式ではない")
    if label_match.group(1) != pwm_match.group(1):
        raise HandoffRecordError("handoff header の label と PWM channel が一致しない")
    if enable_path.name != f"{pwm_path.name}_enable":
        raise HandoffRecordError("handoff header の enable path が PWM channel と一致しない")


def _open_header(header: _HeaderRecord, root_fd: int) -> _OpenedHeader:
    class_entry = Path(header.name_path).parts[0]
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    device_fd = os.open(class_entry, directory_flags, dir_fd=root_fd)
    opened: list[int] = []
    try:
        read_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        write_flags = os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK

        def open_attribute(relative: str, flags: int) -> int:
            name = Path(relative).name
            try:
                fd = os.open(name, flags, dir_fd=device_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise HandoffRecordError("handoff attribute の symlink は許可しない") from exc
                raise
            opened.append(fd)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise HandoffRecordError("handoff attribute は通常の sysfs file にする")
            return fd

        name_fd = open_attribute(header.name_path, read_flags)
        label_fd = open_attribute(header.label_path, read_flags)
        pwm_read_fd = open_attribute(header.pwm_path, read_flags)
        pwm_write_fd = open_attribute(header.pwm_path, write_flags)
        enable_read_fd = open_attribute(header.enable_path, read_flags)
        enable_write_fd = open_attribute(header.enable_path, write_flags)
        pwm_identity = _fd_identity(pwm_read_fd)
        enable_identity = _fd_identity(enable_read_fd)
        if pwm_identity != _fd_identity(pwm_write_fd):
            raise HandoffRecordError("PWM の read/write target が一致しない")
        if enable_identity != _fd_identity(enable_write_fd):
            raise HandoffRecordError("enable の read/write target が一致しない")
        if pwm_identity == enable_identity:
            raise HandoffRecordError("PWM と enable は別の attribute にする")
        return _OpenedHeader(
            record=header,
            name_fd=name_fd,
            label_fd=label_fd,
            pwm_read_fd=pwm_read_fd,
            pwm_write_fd=pwm_write_fd,
            enable_read_fd=enable_read_fd,
            enable_write_fd=enable_write_fd,
            pwm_identity=pwm_identity,
            enable_identity=enable_identity,
        )
    except BaseException:
        for fd in opened:
            os.close(fd)
        raise
    finally:
        os.close(device_fd)


def _fd_identity(fd: int) -> tuple[int, int]:
    metadata = os.fstat(fd)
    return metadata.st_dev, metadata.st_ino


def _read_fd_text(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    payload = os.read(fd, _MAX_ATTRIBUTE_BYTES + 1)
    if len(payload) > _MAX_ATTRIBUTE_BYTES:
        raise OSError("hwmon attribute が大きすぎる")
    return payload.decode("utf-8").strip()


def _write_exact(fd: int, payload: bytes) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    written = os.write(fd, payload)
    if written != len(payload):
        raise OSError("hwmon attribute の書込みが途中で終わった")


def _reject_duplicate_relative_targets(headers: tuple[_HeaderRecord, ...]) -> None:
    targets = {(header.pwm_path, header.enable_path) for header in headers}
    if len(targets) != len(headers):
        raise HandoffRecordError("handoff record で複数 zone が同じ header を指している")


def _reject_duplicate_open_targets(headers: tuple[_OpenedHeader, ...]) -> None:
    targets = [(header.pwm_identity, header.enable_identity) for header in headers]
    if len(set(targets)) != len(targets):
        raise HandoffRecordError("handoff record で複数 zone が同じ header を指している")


if __name__ == "__main__":
    raise SystemExit(main())
