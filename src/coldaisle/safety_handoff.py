"""fan daemon 停止後にだけ動かす emergency Max handoff。

決定記録 0028 §2.7 に従い、このモジュールは標準ライブラリ以外に依存せず、
Safety Config やモデルを読まない。書ける値は ``pwmN_enable=0``（hwmon ABI の
「制御なし＝全速」）と ``pwmN=255`` だけである（決定記録 0118 §2.3）。値は引数・
設定・記録から受け取らない。呼び出し側は、fan daemon の writer が停止した
ことを systemd で保証してから実行する。

書き込み先の device は記録の ``hwmonN`` ではなく、書く時点で hwmon の class directory の
下を探し直して決める（0118 §2.2）。ドライバの再 bind で番号が変わっても Max を書ける。
記録の ``hwmonN`` は監査のためだけに残る。
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

HWMON_FULL_SPEED_ENABLE = "0\n"
"""hwmon ABI の ``pwmN_enable=0``（制御なし＝全速）。確かめ済みの driver にだけ書く。"""
HWMON_MAX_PWM = "255\n"
"""hwmon ABI の ``pwmN`` の最大値。manual と読めた header にだけ書く。"""
VERIFIED_HWMON_DRIVERS: frozenset[str] = frozenset({"nct6799"})
"""``pwmN_enable=0`` が全速になることを導入先で確かめた driver（決定記録 0118 §2.3a）。

ドライバによっては ``0`` が Fan を止める意味になる。実行部は設定にもパッケージにも
依存しないため、``coldaisle.control.hardware.VERIFIED_HWMON_DRIVERS`` と同じ一覧を
ここに定数で持ち、2つが一致することを試験で確かめる。
"""
HANDOFF_RECORD_PATH = Path("/run/coldaisle/fan-handoff.json")
HWMON_ROOT = Path("/sys/class/hwmon")
_MANUAL_ENABLE = "1"
_FULL_SPEED_READBACKS = frozenset({"0", "1"})
"""``pwmN=255`` の後に成功と認める ``pwmN_enable`` の読み戻し。

導入先の driver は manual の 255 を ``0`` と報告する（0118 §2.6 の A-3）。どちらも全速。
"""
_RECORD_SCHEMA_VERSIONS = frozenset({1, 2})
_ZONES = frozenset({"front", "rear", "top"})
_MAX_RECORD_BYTES = 64 * 1024
_MAX_ATTRIBUTE_BYTES = 4 * 1024
_CLASS_ENTRY = re.compile(r"hwmon[0-9]+")
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


class _DeviceNotUnique(Exception):
    """記録の header に一致する device が 0 個、または 2 個以上ある。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class _HeaderRecord:
    zone: str
    name_path: str
    expected_name: str
    label_path: str | None
    expected_label: str | None
    pwm_path: str
    enable_path: str
    original_pwm: int
    original_enable: int

    @property
    def channel(self) -> str:
        """``pwmN`` の ``N``（label・enable もこの番号を指すことを検証済み）。"""
        return Path(self.pwm_path).name.removeprefix("pwm")


@dataclass(frozen=True, slots=True)
class _OpenedHeader:
    record: _HeaderRecord
    name_fd: int
    label_fd: int | None
    pwm_read_fd: int
    pwm_write_fd: int
    enable_read_fd: int
    enable_write_fd: int
    pwm_identity: tuple[int, int]
    enable_identity: tuple[int, int]

    @property
    def fds(self) -> tuple[int, ...]:
        return tuple(
            fd
            for fd in (
                self.name_fd,
                self.label_fd,
                self.pwm_read_fd,
                self.pwm_write_fd,
                self.enable_read_fd,
                self.enable_write_fd,
            )
            if fd is not None
        )


@dataclass(frozen=True, slots=True)
class HandoffZoneResult:
    """1 zone の Max handoff 結果。"""

    zone: str
    status: Literal[
        "applied",
        "identity_mismatch",
        "unsupported_driver",
        "io_error",
        "readback_mismatch",
    ]
    phase: Literal["resolve", "identity", "pwm", "enable", "readback", "complete"]
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HandoffResult:
    """emergency handoff の全 zone 結果。書き込み値は受け取らない。"""

    record_found: bool
    zones: tuple[HandoffZoneResult, ...] = ()

    @property
    def applied_zones(self) -> tuple[str, ...]:
        """Max（全速）を書けたと読み戻しで確かめた zone。"""
        return tuple(result.zone for result in self.zones if result.status == "applied")

    @property
    def identity_mismatch_zones(self) -> tuple[str, ...]:
        """device を一意に特定できなかった、または driver name / label が一致しなかった zone。"""
        return tuple(result.zone for result in self.zones if result.status == "identity_mismatch")

    @property
    def failed_zones(self) -> tuple[str, ...]:
        """未確認の driver・I/O 失敗・readback 不一致で handoff を完了できなかった zone。"""
        return tuple(
            result.zone
            for result in self.zones
            if result.status in {"unsupported_driver", "io_error", "readback_mismatch"}
        )


def emergency_handoff(record_path: Path, sysfs_root: Path) -> HandoffResult:
    """record の header を書く時点で探し直し、Max（全速）にする。

    record が無ければ制御を取っていないため何もしない。driver が確かめ済みの一覧に
    無い zone、device を一意に特定できない zone、driver ``name`` / label が一致しない
    zone には書かない。書く手順は決定記録 0118 §2.3 のとおり
    （``pwmN_enable=0`` を書き、manual と読めたときだけ ``pwmN=255`` を書く）。
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
        class_entries = _list_class_entries(root_fd)
        opened: dict[str, _OpenedHeader | HandoffZoneResult] = {}
        for header in headers:
            if header.expected_name not in VERIFIED_HWMON_DRIVERS:
                # 確かめていない driver では enable=0 が Fan を止めうる（0118 §2.3a）。
                opened[header.zone] = HandoffZoneResult(
                    zone=header.zone,
                    status="unsupported_driver",
                    phase="identity",
                    detail="driver_not_verified",
                )
                continue
            try:
                item = _resolve_and_open_header(header, root_fd, class_entries)
            except HandoffRecordError:
                # 不安全な path が1つでもあれば、一部を書く前に record 全体を拒否する。
                raise
            except _DeviceNotUnique as exc:
                opened[header.zone] = HandoffZoneResult(
                    zone=header.zone,
                    status="identity_mismatch",
                    phase="resolve",
                    detail=exc.detail,
                )
            except (OSError, UnicodeError) as exc:
                opened[header.zone] = HandoffZoneResult(
                    zone=header.zone,
                    status="io_error",
                    phase="resolve",
                    detail=type(exc).__name__,
                )
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
            if isinstance(opened_item, HandoffZoneResult):
                results.append(opened_item)
                continue
            results.append(_apply_open_header(opened_item))
        return HandoffResult(record_found=True, zones=tuple(results))


def _apply_open_header(item: _OpenedHeader) -> HandoffZoneResult:
    header = item.record
    try:
        name = _read_fd_text(item.name_fd)
        label = None if item.label_fd is None else _read_fd_text(item.label_fd)
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

    # 1. enable=0（全速）。どの状態からでも書け、下がる瞬間が無い（0118 §2.3 / §2.6）。
    enable_rejection = ""
    try:
        _write_exact(item.enable_write_fd, HWMON_FULL_SPEED_ENABLE.encode("ascii"))
    except OSError as exc:
        enable_rejection = type(exc).__name__
    if not enable_rejection:
        # 2. pwm が 255 と読めれば全速になっている。
        try:
            pwm_readback = _read_fd_text(item.pwm_read_fd)
        except (OSError, UnicodeError) as exc:
            return HandoffZoneResult(
                zone=header.zone,
                status="io_error",
                phase="readback",
                detail=type(exc).__name__,
            )
        if pwm_readback == HWMON_MAX_PWM.strip():
            return HandoffZoneResult(zone=header.zone, status="applied", phase="complete")

    # 3. enable=0 が拒否されたか、pwm が 255 でない。manual のときだけ pwm=255 を書く。
    try:
        enable_readback = _read_fd_text(item.enable_read_fd)
    except (OSError, UnicodeError) as exc:
        return HandoffZoneResult(
            zone=header.zone,
            status="io_error",
            phase="readback",
            detail=type(exc).__name__,
        )
    if enable_readback != _MANUAL_ENABLE:
        # 自動のまま pwm へ書いても、実行部が終わった後に自動制御が下げうる。書かずに失敗とする。
        detail = ",".join(
            part for part in (enable_rejection, _describe_enable(enable_readback)) if part
        )
        return HandoffZoneResult(
            zone=header.zone,
            status="readback_mismatch",
            phase="enable",
            detail=detail,
        )
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
        final_pwm = _read_fd_text(item.pwm_read_fd)
        final_enable = _read_fd_text(item.enable_read_fd)
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
    if final_enable not in _FULL_SPEED_READBACKS:
        mismatch.append("enable")
    if mismatch:
        return HandoffZoneResult(
            zone=header.zone,
            status="readback_mismatch",
            phase="readback",
            detail=",".join(mismatch),
        )
    return HandoffZoneResult(zone=header.zone, status="applied", phase="complete")


def _describe_enable(value: str) -> str:
    """読み戻した ``pwmN_enable`` を、ログに残す固定の語へ写す（任意の文字列を出さない）。"""
    if value == "0":
        return "enable_full_speed_pwm_not_max"
    if value.isdecimal() and value.isascii() and int(value) >= 2:
        return "enable_auto"
    return "enable_unexpected"


def main() -> int:
    """systemd ``ExecStopPost`` 向けの固定入力 entry point。

    書き込む値や対象 root を引数で差し替える経路は持たない。identity
    mismatch・未確認の driver は対象外 header へ書かず、非0で systemd へ通知する。
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
    schema_version = raw["schema_version"]
    if type(schema_version) is not int or schema_version not in _RECORD_SCHEMA_VERSIONS:
        raise HandoffRecordError("handoff record の schema_version が不正")
    entries = raw["headers"]
    if not isinstance(entries, list) or len(entries) != len(_ZONES):
        raise HandoffRecordError("handoff record に3 zoneすべてが必要")

    headers = tuple(_parse_header(entry, schema_version=schema_version) for entry in entries)
    zones = {header.zone for header in headers}
    if zones != _ZONES:
        raise HandoffRecordError("handoff record の zone は front/rear/top を1つずつにする")
    return headers


def _parse_header(raw: object, *, schema_version: int) -> _HeaderRecord:
    if not isinstance(raw, dict) or set(raw) != _HEADER_FIELDS:
        raise HandoffRecordError("handoff header の欠落・未知フィールド")
    for name in (
        "zone",
        "name_path",
        "expected_name",
        "pwm_path",
        "enable_path",
    ):
        if not _is_record_text(raw[name]):
            raise HandoffRecordError(f"handoff header.{name} が不正")
    label_path = raw["label_path"]
    expected_label = raw["expected_label"]
    if schema_version >= 2 and label_path is None and expected_label is None:
        # v2: label の無い header（fan-hardware.yaml の label: null。決定記録 0118 §2.2）。
        pass
    else:
        for name in ("label_path", "expected_label"):
            if not _is_record_text(raw[name]):
                raise HandoffRecordError(f"handoff header.{name} が不正")
    for name in ("original_pwm", "original_enable"):
        if type(raw[name]) is not int or not 0 <= raw[name] <= 255:
            raise HandoffRecordError(f"handoff header.{name} が不正")
    return _HeaderRecord(
        zone=raw["zone"],
        name_path=raw["name_path"],
        expected_name=raw["expected_name"],
        label_path=label_path,
        expected_label=expected_label,
        pwm_path=raw["pwm_path"],
        enable_path=raw["enable_path"],
        original_pwm=raw["original_pwm"],
        original_enable=raw["original_enable"],
    )


def _is_record_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 500


def _validate_header_paths(header: _HeaderRecord) -> None:
    name_path = Path(header.name_path)
    label_path = None if header.label_path is None else Path(header.label_path)
    pwm_path = Path(header.pwm_path)
    enable_path = Path(header.enable_path)
    relative_paths = tuple(
        path for path in (name_path, label_path, pwm_path, enable_path) if path is not None
    )
    if any(path.is_absolute() or len(path.parts) != 2 for path in relative_paths):
        raise HandoffRecordError("handoff path は hwmon class entry 直下の相対 path にする")
    class_entries = {path.parts[0] for path in relative_paths}
    if len(class_entries) != 1 or _CLASS_ENTRY.fullmatch(next(iter(class_entries))) is None:
        raise HandoffRecordError("handoff header は1つの hwmon class entry を指す")
    if name_path.name != "name":
        raise HandoffRecordError("handoff header の identity path が hwmon 形式ではない")
    pwm_match = re.fullmatch(r"pwm([1-9][0-9]*)", pwm_path.name)
    if pwm_match is None:
        raise HandoffRecordError("handoff header の PWM path が hwmon 形式ではない")
    if label_path is not None:
        label_match = re.fullmatch(r"(?:fan|pwm)([1-9][0-9]*)_label", label_path.name)
        if label_match is None:
            raise HandoffRecordError("handoff header の identity path が hwmon 形式ではない")
        if label_match.group(1) != pwm_match.group(1):
            raise HandoffRecordError("handoff header の label と PWM channel が一致しない")
    if enable_path.name != f"{pwm_path.name}_enable":
        raise HandoffRecordError("handoff header の enable path が PWM channel と一致しない")


def _list_class_entries(root_fd: int) -> tuple[str, ...]:
    """sysfs root 直下の ``hwmonN`` の class entry（番号順に依存しないよう名前で並べる）。"""
    return tuple(sorted(name for name in os.listdir(root_fd) if _CLASS_ENTRY.fullmatch(name)))


def _open_attribute(device_fd: int, name: str, flags: int) -> int:
    """device directory の直下の属性を symlink をたどらずに開く（通常の sysfs file だけ）。"""
    try:
        fd = os.open(name, flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=device_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HandoffRecordError("handoff attribute の symlink は許可しない") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HandoffRecordError("handoff attribute は通常の sysfs file にする")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_optional_attribute(device_fd: int, name: str) -> str | None:
    """属性を読む。無ければ ``None``（読めない・symlink は呼び出し側へ例外で伝える）。"""
    try:
        fd = _open_attribute(device_fd, name, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        return _read_fd_text(fd)
    finally:
        os.close(fd)


def _resolve_device(
    header: _HeaderRecord,
    root_fd: int,
    class_entries: tuple[str, ...],
) -> tuple[int, str | None]:
    """記録の header に一致する device を、記録の ``hwmonN`` を使わずに探す（0118 §2.2）。

    - label の無い header: ``name`` が一致する device がちょうど1つ
    - label のある header: ``name`` と、同じ番号の ``fanN_label``（無ければ ``pwmN_label``）が
      一致する device がちょうど1つ

    戻り値は（device directory の fd・照合に使った label の属性名）。0 個・2 個以上なら
    :class:`_DeviceNotUnique`。一致を確かめられない device（``name`` が読めない など）が
    あれば一意とは言えないので、例外をそのまま伝えてその zone を失敗にする。
    """
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    matches: list[tuple[int, str | None]] = []
    try:
        for entry in class_entries:
            try:
                # class entry は devices 側への symlink なので、ここだけはたどる。
                device_fd = os.open(entry, directory_flags, dir_fd=root_fd)
            except FileNotFoundError:
                # 一覧の後に消えた entry（再 bind の途中）は候補ではない。
                continue
            matched = False
            try:
                if _read_optional_attribute(device_fd, "name") != header.expected_name:
                    continue
                label_attribute: str | None = None
                if header.expected_label is not None:
                    for candidate in (
                        f"fan{header.channel}_label",
                        f"pwm{header.channel}_label",
                    ):
                        value = _read_optional_attribute(device_fd, candidate)
                        if value is not None:
                            label_attribute = candidate if value == header.expected_label else None
                            break
                    if label_attribute is None:
                        continue
                matches.append((device_fd, label_attribute))
                matched = True
            except HandoffRecordError as exc:
                # 候補の device の name / label が symlink や通常でないファイルだと、一致するかを
                # 確かめられない。記録の不正ではないので、その zone を「一意に特定できない」にする。
                raise _DeviceNotUnique("unverifiable_device") from exc
            finally:
                if not matched:
                    os.close(device_fd)
    except BaseException:
        for device_fd, _ in matches:
            os.close(device_fd)
        raise
    if len(matches) != 1:
        for device_fd, _ in matches:
            os.close(device_fd)
        raise _DeviceNotUnique("no_matching_device" if not matches else "multiple_matching_devices")
    return matches[0]


def _resolve_and_open_header(
    header: _HeaderRecord,
    root_fd: int,
    class_entries: tuple[str, ...],
) -> _OpenedHeader:
    device_fd, label_attribute = _resolve_device(header, root_fd, class_entries)
    opened: list[int] = []
    try:

        def open_attribute(name: str, flags: int) -> int:
            fd = _open_attribute(device_fd, name, flags)
            opened.append(fd)
            return fd

        pwm_name = Path(header.pwm_path).name
        enable_name = Path(header.enable_path).name
        name_fd = open_attribute("name", os.O_RDONLY)
        label_fd = None if label_attribute is None else open_attribute(label_attribute, os.O_RDONLY)
        pwm_read_fd = open_attribute(pwm_name, os.O_RDONLY)
        pwm_write_fd = open_attribute(pwm_name, os.O_WRONLY)
        enable_read_fd = open_attribute(enable_name, os.O_RDONLY)
        enable_write_fd = open_attribute(enable_name, os.O_WRONLY)
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
    # 書く時点で探し直すので、記録の hwmonN が違っても同じ header に行き着きうる。
    # 探し直しに使う組（driver・label・channel）も zone ごとに別でなければならない。
    identities = {
        (header.expected_name, header.expected_label, Path(header.pwm_path).name)
        for header in headers
    }
    if len(targets) != len(headers) or len(identities) != len(headers):
        raise HandoffRecordError("handoff record で複数 zone が同じ header を指している")


def _reject_duplicate_open_targets(headers: tuple[_OpenedHeader, ...]) -> None:
    targets = [(header.pwm_identity, header.enable_identity) for header in headers]
    if len(set(targets)) != len(targets):
        raise HandoffRecordError("handoff record で複数 zone が同じ header を指している")


if __name__ == "__main__":
    raise SystemExit(main())
