"""実機の hwmon へ書く Fan Hardware Backend。#77 / 決定記録 0118 §2.5

``SimulatedFanBackend`` と同じ ``FanHardwareBackend`` を実装し、command の受理・takeover の
確認・profile の写像・``applied_demand`` の規則を ``_common`` で共有する。違うのは、写した
PWM を sysfs の ``pwmN`` / ``pwmN_enable`` へ書き、読み戻すことだけである。

起動（コンストラクタ）

1. driver が確かめ済みの一覧（:data:`VERIFIED_HWMON_DRIVERS`）に無ければ、何も開かずに
   :class:`HwmonUnsupportedDriverError`（0118 §2.3a。``pwmN_enable`` に触らない）
2. header を特定する（0118 §2.1）。``label: null`` は hwmon の ``name`` が driver に一致する
   device がちょうど1つ、label のある header は同じ番号の ``fanN_label``（無ければ
   ``pwmN_label``）も一致する device がちょうど1つ。そうでなければ
   :class:`HwmonHeaderNotIdentifiedError`（0028 §2.7。制御を取らない）
3. ``pwmN`` / ``pwmN_enable`` を書き込み用に開け、``fanN_input`` を読めることを確かめ、
   元の ``pwmN`` / ``pwmN_enable`` を読む
4. 引き継ぎ記録を書く（0080 §2.7 の 1〜6・0118 §2.2）。書けなければ
   :class:`HandoffRecordWriteError`（制御を取らない）

takeover の Max（0118 §2.3）は最初の ``apply()``（``STARTUP`` / ``config_invalid`` の全 zone
Max）で ``pwmN_enable=0`` → ``pwmN_enable=1`` の順に書く。下げる瞬間が無い。

**書くのは ``pwmN`` と ``pwmN_enable`` だけ**（``pwmN_mode`` などの BIOS が決めた値に触らない）。
path は起動時に1回だけ解決して fd で持ち、運転中に device を探し直さない。``hwmonN`` の番号は
引き継ぎ記録（監査のため）の外へ出さない。class entry 以外の symlink はたどらない。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coldaisle import logs
from coldaisle.control.config import FanHardwareConfig, FanHeader
from coldaisle.control.hardware._common import CommandSession, MappedDemand, map_demand
from coldaisle.control.hardware.simulated import FanHardwareResult, confirmed_writes
from coldaisle.control.hardware.verified_drivers import VERIFIED_HWMON_DRIVERS
from coldaisle.control.safety.critical import ComposedDemands, ControlRuntimeBinding
from coldaisle.control.schema import (
    HWMON_PWM_MAX,
    EffectiveZoneDemand,
    Fault,
    FaultCode,
    HardwareReadback,
    PerZone,
    Zone,
)

LOGGER = logging.getLogger("coldaisle.control.hardware")

HANDOFF_RECORD_SCHEMA_VERSION = 2
"""書く引き継ぎ記録の版（0118 §2.2。``label: null`` の header は label の欄が null）。"""

_ENABLE_FULL_SPEED = 0
"""hwmon ABI の「制御なし（全速）」。確かめ済みの driver でだけ全速として扱う（0118 §2.3a）。"""
_ENABLE_MANUAL = 1
_ENABLE_AUTO_MIN = 2
"""これ以上は自動制御。読み戻しでこの値なら外から戻された（0118 §2.4）。"""
_MAX_RECORD_BYTES = 64 * 1024
"""引き継ぎ実行部（``safety_handoff``）が読む上限と同じ。"""
_MAX_ATTRIBUTE_BYTES = 4 * 1024
_CLASS_ENTRY = re.compile(r"hwmon[0-9]+")


class HwmonStartupError(RuntimeError):
    """起動時に実機の制御を取れない（``pwmN_enable`` に触っていない。終了コード 5）。"""


class HwmonUnsupportedDriverError(HwmonStartupError):
    """``pwmN_enable=0`` の意味を確かめていない driver（0118 §2.3a）。"""


class HandoffRecordWriteError(HwmonStartupError):
    """引き継ぎ記録を書けない。記録の無い takeover はしない（0080 §2.7 の 3）。"""


class HwmonHeaderNotIdentifiedError(RuntimeError):
    """header を一意に特定できない（0028 §2.7。制御を取らない。終了コード 2）。"""


@dataclass(frozen=True, slots=True)
class _OpenedHeader:
    """起動時に1回だけ解決した1 zone の属性（fd で持つ）。"""

    zone: Zone
    header: FanHeader
    entry: str
    """class entry（``hwmonN``）。**引き継ぎ記録にだけ書く**（ログに出さない）。"""
    label_attribute: str | None
    name_fd: int
    pwm_read_fd: int
    pwm_write_fd: int
    enable_read_fd: int
    enable_write_fd: int
    tach_fd: int
    pwm_identity: tuple[int, int]
    current_pwm: int
    current_enable: int

    @property
    def fds(self) -> tuple[int, ...]:
        return (
            self.name_fd,
            self.pwm_read_fd,
            self.pwm_write_fd,
            self.enable_read_fd,
            self.enable_write_fd,
            self.tach_fd,
        )


@dataclass(frozen=True, slots=True)
class _Original:
    """記録した元の値（既存の記録から引き継いだ値を含む。0080 §2.7 の 4）。"""

    pwm: int
    enable: int


@dataclass(slots=True)
class HwmonFanBackend:
    """hwmon の ``pwmN`` / ``pwmN_enable`` へ書く backend（0118 §2.5）。

    ``record_path`` は引き継ぎ実行部の定数（``coldaisle.safety_handoff.HANDOFF_RECORD_PATH``）を
    合成の起点が渡す。この package は上位の module を import しない（0080 §2.7 の 6）。
    """

    config: FanHardwareConfig
    runtime_binding: ControlRuntimeBinding
    record_path: Path
    sysfs_root: Path
    _session: CommandSession = field(init=False, repr=False)
    _headers: dict[Zone, _OpenedHeader] = field(default_factory=dict, init=False, repr=False)
    _originals: dict[Zone, _Original] = field(default_factory=dict, init=False, repr=False)
    _manual: set[Zone] = field(default_factory=set, init=False, repr=False)
    """takeover の Max（``0`` → ``1``）を書けた zone。以後は ``pwmN`` を書く。"""
    _running: set[Zone] = field(default_factory=set, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._session = CommandSession(self.config, self.runtime_binding)
        # 一覧に無い driver では何も開かない（enable=0 が Fan を止めうる。0118 §2.3a）
        unsupported = sorted(
            {
                self.config.zones.get(zone).driver
                for zone in Zone
                if self.config.zones.get(zone).driver not in VERIFIED_HWMON_DRIVERS
            }
        )
        if unsupported:
            raise HwmonUnsupportedDriverError(
                f"pwmN_enable=0 を確かめていない driver では制御を取らない: {unsupported}"
            )
        try:
            self._headers = _open_headers(self.config, self.sysfs_root)
            self._originals = _write_handoff_record(self.record_path, self._headers)
        except BaseException:
            self.close()
            raise
        LOGGER.info(
            "hwmon の header を特定し、引き継ぎ記録を書いた",
            extra={
                logs.FIELDS_KEY: {
                    "event": "hwmon_backend_ready",
                    "zones": {
                        zone.value: {
                            "driver": opened.header.driver,
                            "label": opened.header.label,
                            "pwm_attribute": opened.header.pwm_attribute,
                            "original_enable": self._originals[zone].enable,
                        }
                        for zone, opened in self._headers.items()
                    },
                }
            },
        )

    def apply(self, demands: ComposedDemands) -> PerZone[FanHardwareResult]:
        """3 zone の effective demand を写し、hwmon へ書いて読み戻す。

        zone ごとの I/O の失敗は例外にせず、その zone の fault（Critical Safety へ渡る）にする。
        """
        if self._closed:
            raise ValueError("閉じた hwmon backend には書けない")
        zones = self._session.accept(demands)
        for zone in Zone:
            if zone not in self._manual and not zones.get(zone).forced_max:
                # accept() が takeover の確認までは全 zone forced Max しか通さない。確認は
                # 全 zone の takeover が成功した後なので、ここには来ない（来たら書かない）。
                raise AssertionError("takeover していない zone へ Max 以外を書かない")
        results = PerZone(
            front=self._apply_zone(Zone.FRONT, zones.front),
            rear=self._apply_zone(Zone.REAR, zones.rear),
            top=self._apply_zone(Zone.TOP, zones.top),
        )
        self._session.observe_takeover(zones, confirmed_writes(results))
        return results

    def hand_back(self) -> bool:
        """正常停止で元の制御へ返す（0080 §2.8）。引き継ぎ記録を消せたときだけ真。

        - 元の ``pwmN_enable`` がすべて ``0`` または ``2`` 以上: PWM を下げずに元の値へ戻し、
          読み戻しで確かめる。**全 zone を戻せたときだけ**記録を消す
        - いずれかが ``1``（manual）: 返す先の自動制御が無い。戻さずに Max のまま残し、
          構造化ログの event ``shutdown_left_max_manual`` を出す。記録は消さない
          （``ExecStopPost`` が同じ Max を書き直す）
        - 途中の失敗は記録を消さない（``ExecStopPost`` が Max を書く）
        """
        if self._closed:
            # 閉じた後は書かない。記録は残る（ExecStopPost が Max を書く）
            return False
        try:
            manual = [zone for zone in Zone if self._originals[zone].enable == _ENABLE_MANUAL]
            if manual:
                self._leave_max_for_manual(manual)
                return False
            failed: dict[str, str] = {}
            for zone in Zone:
                problem = self._restore_enable(zone)
                if problem is not None:
                    failed[zone.value] = problem
            if failed:
                LOGGER.error(
                    "正常停止で元の制御へ戻せなかったため引き継ぎ記録を残す"
                    "（ExecStopPost が Max を書く）",
                    extra={
                        logs.FIELDS_KEY: {"event": "shutdown_hand_back_failed", "zones": failed}
                    },
                )
                return False
            try:
                os.unlink(self.record_path)
            except OSError as error:
                LOGGER.error(
                    "元の制御へ戻したが引き継ぎ記録を消せなかった（ExecStopPost が Max を書く）",
                    extra={
                        logs.FIELDS_KEY: {
                            "event": "shutdown_record_not_removed",
                            "error": type(error).__name__,
                        }
                    },
                )
                return False
            LOGGER.info(
                "正常停止で元の制御へ戻し、引き継ぎ記録を消した",
                extra={
                    logs.FIELDS_KEY: {
                        "event": "shutdown_handed_back",
                        "original_enable": {
                            zone.value: self._originals[zone].enable for zone in Zone
                        },
                    }
                },
            )
            return True
        finally:
            self.close()

    def close(self) -> None:
        """開いた fd を閉じる。以後は書かない。"""
        self._closed = True
        for opened in self._headers.values():
            for fd in opened.fds:
                try:
                    os.close(fd)
                except OSError:
                    LOGGER.warning("hwmon の fd を閉じられなかった")
        self._headers = {}

    # ------------------------------------------------------------------ zone ごとの書き込み

    def _apply_zone(self, zone: Zone, demand: EffectiveZoneDemand) -> FanHardwareResult:
        opened = self._headers[zone]
        profile = opened.header.profile
        mapped = map_demand(profile, demand.effective, running=zone in self._running)
        identity_problem = _identity_problem(opened)
        if identity_problem is not None:
            # 再 bind などで device が変わった。運転中は探し直さない（0118 §2.5）。
            return _failed(zone, mapped, FaultCode.WRITE_FAILURE, identity_problem, rpm=None)
        if zone not in self._manual:
            result = self._take_over(zone, opened, mapped)
        else:
            result = self._write_pwm(zone, opened, mapped)
        if (
            result.applied_demand is not None
            and result.readback.rpm is not None
            and result.readback.rpm > 0
        ):
            # 書けて読み戻せて回っていると確かめて初めて kick 済みとする（simulated と同じ）。
            self._running.add(zone)
        return result

    def _take_over(
        self, zone: Zone, opened: _OpenedHeader, mapped: MappedDemand
    ) -> FanHardwareResult:
        """takeover の Max: ``pwmN_enable=0``（全速）→ ``pwmN_enable=1``（manual）→ 読み戻し。"""
        if mapped.pwm_raw != HWMON_PWM_MAX:
            raise AssertionError("takeover は Max だけを書く")
        problem = _write_max(opened)
        if problem is not None:
            return _failed(
                zone, mapped, FaultCode.WRITE_FAILURE, problem, rpm=_read_rpm(opened.tach_fd)
            )
        result = _verify(zone, opened, mapped)
        if result.applied_demand is not None:
            self._manual.add(zone)
        return result

    def _write_pwm(
        self, zone: Zone, opened: _OpenedHeader, mapped: MappedDemand
    ) -> FanHardwareResult:
        """毎 tick の書き込み（0118 §2.3 の 3・§2.4）。"""
        # manual の 255 は 0 と読める driver がある（0118 §2.6）。0（全速）のまま pwm だけを
        # 書くとモードが変わらない driver では、読み戻しの pwm が下がっても Fan は全速の
        # まま残りうる。255 未満へ下げる前に manual へ切り替える。
        if (
            mapped.pwm_raw < HWMON_PWM_MAX
            and _read_int(opened.enable_read_fd) == _ENABLE_FULL_SPEED
        ):
            try:
                _write_exact(opened.enable_write_fd, _encode(_ENABLE_MANUAL))
            except OSError as error:
                return _failed(
                    zone,
                    mapped,
                    FaultCode.WRITE_FAILURE,
                    f"enable_manual:{_error_name(error)}",
                    rpm=_read_rpm(opened.tach_fd),
                )
        try:
            _write_exact(opened.pwm_write_fd, _encode(mapped.pwm_raw))
        except OSError as error:
            # 自動制御へ戻された driver は pwm の書き込みを拒む（EBUSY。0118 §2.6 の A-1）。
            enable = _read_int(opened.enable_read_fd)
            code = (
                FaultCode.ENABLE_REVERTED
                if enable is not None and enable >= _ENABLE_AUTO_MIN
                else FaultCode.WRITE_FAILURE
            )
            return _failed(
                zone, mapped, code, f"pwm:{_error_name(error)}", rpm=_read_rpm(opened.tach_fd)
            )
        return _verify(zone, opened, mapped)

    def _leave_max_for_manual(self, manual: list[Zone]) -> None:
        # 元が manual の header には返す先の自動制御が無い（0028 §2.7）。最後の tick の値の
        # ままにせず、takeover と同じ順で Max にしてから終える。記録は消さない。
        failed = {}
        for zone in Zone:
            opened = self._headers[zone]
            problem = _identity_problem(opened) or _write_max(opened)
            if problem is not None:
                failed[zone.value] = problem
        LOGGER.warning(
            "元の pwmN_enable が manual の header があるため、元の制御へ戻さず Max のまま終える",
            extra={
                logs.FIELDS_KEY: {
                    "event": "shutdown_left_max_manual",
                    "zones": [zone.value for zone in manual],
                    "max_write_failed": failed,
                }
            },
        )

    def _restore_enable(self, zone: Zone) -> str | None:
        """元の ``pwmN_enable`` を書いて読み戻す。PWM は書かない（下げない）。"""
        opened = self._headers[zone]
        original = self._originals[zone].enable
        problem = _identity_problem(opened)
        if problem is not None:
            return problem
        try:
            _write_exact(opened.enable_write_fd, _encode(original))
        except OSError as error:
            return f"enable:{_error_name(error)}"
        readback = _read_int(opened.enable_read_fd)
        if readback != original:
            return "enable_readback"
        return None


# ------------------------------------------------------------------ 起動時の特定と記録


def _open_headers(config: FanHardwareConfig, sysfs_root: Path) -> dict[Zone, _OpenedHeader]:
    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        root_fd = os.open(sysfs_root, root_flags)
    except OSError as error:
        raise HwmonHeaderNotIdentifiedError(
            f"hwmon の class directory を開けない: {_error_name(error)}"
        ) from error
    opened: dict[Zone, _OpenedHeader] = {}
    try:
        entries = tuple(
            sorted(name for name in os.listdir(root_fd) if _CLASS_ENTRY.fullmatch(name))
        )
        for zone in Zone:
            opened[zone] = _open_header(zone, config.zones.get(zone), root_fd, entries)
        identities = [header.pwm_identity for header in opened.values()]
        if len(set(identities)) != len(identities):
            raise HwmonHeaderNotIdentifiedError("複数 zone が同じ pwm を指している")
        return opened
    except BaseException:
        for header in opened.values():
            for fd in header.fds:
                os.close(fd)
        raise
    finally:
        os.close(root_fd)


def _open_header(
    zone: Zone, header: FanHeader, root_fd: int, entries: tuple[str, ...]
) -> _OpenedHeader:
    device_fd, entry, label_attribute = _resolve_device(zone, header, root_fd, entries)
    fds: list[int] = []
    try:

        def open_attribute(name: str, flags: int) -> int:
            fd = _open_attribute(device_fd, name, flags)
            fds.append(fd)
            return fd

        try:
            name_fd = open_attribute("name", os.O_RDONLY)
            pwm_read_fd = open_attribute(header.pwm_attribute, os.O_RDONLY)
            pwm_write_fd = open_attribute(header.pwm_attribute, os.O_WRONLY)
            enable_read_fd = open_attribute(header.enable_attribute, os.O_RDONLY)
            enable_write_fd = open_attribute(header.enable_attribute, os.O_WRONLY)
            tach_fd = open_attribute(header.tach_attribute, os.O_RDONLY)
        except OSError as error:
            # 権限が付いていない（udev。0080 §2.6）・属性が無いなど。takeover の前に止める
            raise HwmonStartupError(
                f"{zone.value}: pwm / enable を書き込み用に、tach を読み込み用に開けない: "
                f"{_error_name(error)}"
            ) from error
        pwm_identity = _fd_identity(pwm_read_fd)
        enable_identity = _fd_identity(enable_read_fd)
        if (
            pwm_identity != _fd_identity(pwm_write_fd)
            or enable_identity != _fd_identity(enable_write_fd)
            or pwm_identity == enable_identity
        ):
            raise HwmonHeaderNotIdentifiedError(f"{zone.value}: pwm / enable の対象が一致しない")
        if _read_int(tach_fd) is None:
            raise HwmonStartupError(f"{zone.value}: {header.tach_attribute} を読めない")
        current_pwm = _read_int(pwm_read_fd)
        current_enable = _read_int(enable_read_fd)
        if current_pwm is None or not 0 <= current_pwm <= HWMON_PWM_MAX:
            raise HwmonStartupError(f"{zone.value}: 元の {header.pwm_attribute} を読めない")
        if current_enable is None or not 0 <= current_enable <= HWMON_PWM_MAX:
            raise HwmonStartupError(f"{zone.value}: 元の {header.enable_attribute} を読めない")
        return _OpenedHeader(
            zone=zone,
            header=header,
            entry=entry,
            label_attribute=label_attribute,
            name_fd=name_fd,
            pwm_read_fd=pwm_read_fd,
            pwm_write_fd=pwm_write_fd,
            enable_read_fd=enable_read_fd,
            enable_write_fd=enable_write_fd,
            tach_fd=tach_fd,
            pwm_identity=pwm_identity,
            current_pwm=current_pwm,
            current_enable=current_enable,
        )
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise
    finally:
        os.close(device_fd)


def _resolve_device(
    zone: Zone, header: FanHeader, root_fd: int, entries: tuple[str, ...]
) -> tuple[int, str, str | None]:
    """header に一致する device をちょうど1つ探す（0118 §2.1）。

    戻り値は（device directory の fd・class entry・照合に使った label の属性名）。一致を
    確かめられない device（``name`` が読めない・symlink など）があれば一意とは言えない。
    """
    channel = header.pwm_attribute.removeprefix("pwm")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    matches: list[tuple[int, str, str | None]] = []
    try:
        for entry in entries:
            try:
                # class entry は devices 側への symlink なので、ここだけはたどる。
                device_fd = os.open(entry, directory_flags, dir_fd=root_fd)
            except FileNotFoundError:
                continue
            matched = False
            try:
                if _read_optional_attribute(device_fd, "name") != header.driver:
                    continue
                label_attribute: str | None = None
                if header.label is not None:
                    for candidate in (f"fan{channel}_label", f"pwm{channel}_label"):
                        value = _read_optional_attribute(device_fd, candidate)
                        if value is not None:
                            label_attribute = candidate if value == header.label else None
                            break
                    if label_attribute is None:
                        continue
                matches.append((device_fd, entry, label_attribute))
                matched = True
            except (OSError, UnicodeError, _UnsafeAttributeError) as error:
                raise HwmonHeaderNotIdentifiedError(
                    f"{zone.value}: 一致を確かめられない hwmon device がある"
                ) from error
            finally:
                if not matched:
                    os.close(device_fd)
    except BaseException:
        for device_fd, _, _ in matches:
            os.close(device_fd)
        raise
    if len(matches) != 1:
        for device_fd, _, _ in matches:
            os.close(device_fd)
        raise HwmonHeaderNotIdentifiedError(
            f"{zone.value}: driver {header.driver!r} の header を一意に特定できない"
            f"（一致した device: {len(matches)} 個）"
        )
    return matches[0]


class _UnsafeAttributeError(Exception):
    """属性が symlink または通常のファイルでない。"""


def _open_attribute(device_fd: int, name: str, flags: int) -> int:
    """device directory 直下の属性を symlink をたどらずに開く（通常の sysfs file だけ）。"""
    # O_NOFOLLOW: symlink の属性は ELOOP で開かない
    fd = os.open(name, flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=device_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "hwmon の属性は通常の sysfs file にする")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_optional_attribute(device_fd: int, name: str) -> str | None:
    try:
        fd = _open_attribute(device_fd, name, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EINVAL}:
            raise _UnsafeAttributeError(name) from error
        raise
    try:
        return _read_text(fd)
    finally:
        os.close(fd)


def _write_handoff_record(
    record_path: Path, headers: dict[Zone, _OpenedHeader]
) -> dict[Zone, _Original]:
    """引き継ぎ記録を書き、記録した元の値を返す（0080 §2.7 の 1〜4・0118 §2.2）。"""
    current = {
        zone: _Original(pwm=opened.current_pwm, enable=opened.current_enable)
        for zone, opened in headers.items()
    }
    originals = _inherited_originals(record_path, headers) or current
    payload = {
        "schema_version": HANDOFF_RECORD_SCHEMA_VERSION,
        "headers": [
            {
                "zone": zone.value,
                "name_path": f"{opened.entry}/name",
                "expected_name": opened.header.driver,
                "label_path": (
                    None
                    if opened.label_attribute is None
                    else f"{opened.entry}/{opened.label_attribute}"
                ),
                "expected_label": opened.header.label,
                "pwm_path": f"{opened.entry}/{opened.header.pwm_attribute}",
                "enable_path": f"{opened.entry}/{opened.header.enable_attribute}",
                "original_pwm": originals[zone].pwm,
                "original_enable": originals[zone].enable,
            }
            for zone in Zone
            for opened in (headers[zone],)
        ],
    }
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        _replace_file(record_path, data)
    except OSError as error:
        raise HandoffRecordWriteError(
            f"引き継ぎ記録を書けないため制御を取らない: {_error_name(error)}"
        ) from error
    return originals


def _inherited_originals(
    record_path: Path, headers: dict[Zone, _OpenedHeader]
) -> dict[Zone, _Original] | None:
    """既存の記録の元の値を引き継ぐ（0080 §2.7 の 4・0118 §2.2）。

    前回が異常終了なら、いまの値は引き継ぎ実行部が書いた Max で、それを元の値として
    上書きすると正常停止で BIOS へ返せなくなる。**3 zone すべてで zone・driver・label・
    ``pwm_attribute``・``enable_attribute`` が一致するときだけ**引き継ぐ。一致しない・読めない
    記録は error を残して上書きする。
    """
    try:
        text = _read_existing_record(record_path)
    except (OSError, UnicodeError, ValueError) as error:
        _log_record_not_inherited("unreadable", type(error).__name__)
        return None
    if text is None:
        return None
    try:
        raw: Any = json.loads(text)
        entries = raw["headers"]
        if raw.get("schema_version") not in {1, 2} or not isinstance(entries, list):
            raise ValueError("schema")
        by_zone: dict[str, Any] = {}
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("header")
            zone_name = item.get("zone")
            if not isinstance(zone_name, str) or zone_name in by_zone:
                raise ValueError("zone")
            by_zone[zone_name] = item
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        _log_record_not_inherited("malformed", type(error).__name__)
        return None
    if set(by_zone) != {zone.value for zone in Zone}:
        _log_record_not_inherited("malformed", "zones")
        return None
    inherited: dict[Zone, _Original] = {}
    mismatched: list[str] = []
    for zone in Zone:
        item = by_zone[zone.value]
        header = headers[zone].header
        original_pwm = item.get("original_pwm")
        original_enable = item.get("original_enable")
        if (
            item.get("expected_name") != header.driver
            or item.get("expected_label") != header.label
            or not isinstance(item.get("pwm_path"), str)
            or not isinstance(item.get("enable_path"), str)
            or Path(item["pwm_path"]).name != header.pwm_attribute
            or Path(item["enable_path"]).name != header.enable_attribute
            or type(original_pwm) is not int
            or type(original_enable) is not int
            or not 0 <= original_pwm <= HWMON_PWM_MAX
            or not 0 <= original_enable <= HWMON_PWM_MAX
        ):
            mismatched.append(zone.value)
            continue
        inherited[zone] = _Original(pwm=original_pwm, enable=original_enable)
    if mismatched:
        _log_record_not_inherited("header_mismatch", ",".join(mismatched))
        return None
    LOGGER.warning(
        "既存の引き継ぎ記録の元の値を引き継いだ（前回は記録を消さずに終わった）",
        extra={
            logs.FIELDS_KEY: {
                "event": "handoff_record_inherited",
                "original_enable": {zone.value: inherited[zone].enable for zone in Zone},
            }
        },
    )
    return inherited


def _log_record_not_inherited(reason: str, detail: str) -> None:
    LOGGER.error(
        "既存の引き継ぎ記録が今の header と一致しないため、元の値を引き継がずに上書きする",
        extra={
            logs.FIELDS_KEY: {
                "event": "handoff_record_overwritten",
                "reason": reason,
                "detail": detail,
            }
        },
    )


def _read_existing_record(path: Path) -> str | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("引き継ぎ記録が通常ファイルではない")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_RECORD_BYTES:
            raise ValueError("引き継ぎ記録が通常ファイルでない・大きすぎる")
        payload = os.read(fd, _MAX_RECORD_BYTES + 1)
        if len(payload) > _MAX_RECORD_BYTES:
            raise ValueError("引き継ぎ記録が大きすぎる")
        return payload.decode("utf-8")
    finally:
        os.close(fd)


def _replace_file(path: Path, data: bytes) -> None:
    """同じディレクトリの一時ファイル（``O_EXCL | O_NOFOLLOW``、``0600``）へ書いて rename する。

    引き継ぎ実行部が途中まで書かれた記録を読まない（0080 §2.7 の 2）。
    """
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    temporary = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    created = False
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        created = True
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        created = False
        os.fsync(directory_fd)
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                LOGGER.warning("引き継ぎ記録の一時ファイルを消せなかった")
        os.close(directory_fd)


# ------------------------------------------------------------------ 書き込みと読み戻し


def _write_max(opened: _OpenedHeader) -> str | None:
    """fand の Max（0118 §2.3）: ``pwmN_enable=0`` → ``pwmN_enable=1``。失敗の語を返す。

    ``0`` を書けなければ ``1`` を書かない（manual へ切り替えると、BIOS が直前に出していた
    値に固定される）。
    """
    try:
        _write_exact(opened.enable_write_fd, _encode(_ENABLE_FULL_SPEED))
    except OSError as error:
        return f"enable_full_speed:{_error_name(error)}"
    try:
        _write_exact(opened.enable_write_fd, _encode(_ENABLE_MANUAL))
    except OSError as error:
        return f"enable_manual:{_error_name(error)}"
    return None


def _verify(zone: Zone, opened: _OpenedHeader, mapped: MappedDemand) -> FanHardwareResult:
    """``pwmN`` と ``pwmN_enable`` を読み戻して確かめる（0118 §2.4 / §2.5）。

    - ``pwmN_enable`` が 2 以上: 自動制御へ戻された（``ENABLE_REVERTED``）
    - 255 未満を書いた: ``pwmN`` が一致し、かつ ``pwmN_enable`` が ``1`` のときだけ成功
    - 255 を書いた: ``pwmN`` が 255 で ``pwmN_enable`` が ``0`` か ``1``（どちらも全速）
    """
    pwm = _read_int(opened.pwm_read_fd)
    enable = _read_int(opened.enable_read_fd)
    rpm = _read_rpm(opened.tach_fd)
    pwm_raw = pwm if pwm is not None and 0 <= pwm <= HWMON_PWM_MAX else mapped.pwm_raw
    if enable is not None and enable >= _ENABLE_AUTO_MIN:
        return _readback_failed(
            zone, mapped, FaultCode.ENABLE_REVERTED, pwm_raw, rpm, "enable_auto"
        )
    if mapped.pwm_raw < HWMON_PWM_MAX:
        confirmed = pwm == mapped.pwm_raw and enable == _ENABLE_MANUAL
    else:
        confirmed = pwm == HWMON_PWM_MAX and enable in {_ENABLE_FULL_SPEED, _ENABLE_MANUAL}
    if not confirmed:
        return _readback_failed(
            zone, mapped, FaultCode.READBACK_MISMATCH, pwm_raw, rpm, _describe(pwm, enable)
        )
    return FanHardwareResult(
        target_rpm=mapped.target_rpm,
        airflow_index=mapped.airflow_index,
        readback=HardwareReadback(pwm_raw=pwm_raw, rpm=rpm, write_ok=True, readback_ok=True),
        applied_demand=mapped.demand,
    )


def _describe(pwm: int | None, enable: int | None) -> str:
    parts = []
    parts.append("pwm_unreadable" if pwm is None else "pwm")
    parts.append("enable_unreadable" if enable is None else f"enable={enable}")
    return ",".join(parts)


def _readback_failed(
    zone: Zone,
    mapped: MappedDemand,
    code: FaultCode,
    pwm_raw: int,
    rpm: int | None,
    detail: str,
) -> FanHardwareResult:
    return FanHardwareResult(
        target_rpm=mapped.target_rpm,
        airflow_index=mapped.airflow_index,
        readback=HardwareReadback(pwm_raw=pwm_raw, rpm=rpm, write_ok=True, readback_ok=False),
        applied_demand=None,
        fault=Fault(code=code, zone=zone, detail=detail),
    )


def _failed(
    zone: Zone, mapped: MappedDemand, code: FaultCode, detail: str, *, rpm: int | None
) -> FanHardwareResult:
    """書き込みが失敗した zone の結果（``applied_demand`` を返さない）。"""
    return FanHardwareResult(
        target_rpm=mapped.target_rpm,
        airflow_index=mapped.airflow_index,
        readback=HardwareReadback(
            pwm_raw=mapped.pwm_raw, rpm=rpm, write_ok=False, readback_ok=False
        ),
        applied_demand=None,
        fault=Fault(code=code, zone=zone, detail=detail),
    )


def _identity_problem(opened: _OpenedHeader) -> str | None:
    """毎 tick、device の ``name`` を読み直す（0118 §2.5）。変わっていれば書かない。"""
    try:
        name = _read_text(opened.name_fd)
    except (OSError, UnicodeError) as error:
        return f"device_name_unreadable:{_error_name(error)}"
    if name != opened.header.driver:
        return "device_name_changed"
    return None


def _read_rpm(fd: int) -> int | None:
    """``fanN_input`` を読む。読めなければ ``None``（tach の判定は Critical Safety）。"""
    value = _read_int(fd)
    return value if value is not None and value >= 0 else None


def _read_int(fd: int) -> int | None:
    try:
        text = _read_text(fd)
    except (OSError, UnicodeError):
        return None
    if not text.isascii() or not text.isdecimal():
        return None
    return int(text)


def _read_text(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    payload = os.read(fd, _MAX_ATTRIBUTE_BYTES + 1)
    if len(payload) > _MAX_ATTRIBUTE_BYTES:
        raise OSError(errno.EFBIG, "hwmon の属性が大きすぎる")
    return payload.decode("utf-8").strip()


def _write_exact(fd: int, payload: bytes) -> None:
    """sysfs の属性へ1回で書く。試験は偽の driver をここに差し込む。"""
    os.lseek(fd, 0, os.SEEK_SET)
    written = os.write(fd, payload)
    if written != len(payload):
        raise OSError(errno.EIO, "hwmon の属性への書き込みが途中で終わった")


def _encode(value: int) -> bytes:
    return f"{value}\n".encode("ascii")


def _fd_identity(fd: int) -> tuple[int, int]:
    metadata = os.fstat(fd)
    return metadata.st_dev, metadata.st_ino


def _error_name(error: OSError | UnicodeError) -> str:
    """ログ・fault に残す失敗の語（path を含めない）。"""
    if isinstance(error, OSError) and error.errno is not None and error.errno in errno.errorcode:
        return errno.errorcode[error.errno]
    return type(error).__name__
