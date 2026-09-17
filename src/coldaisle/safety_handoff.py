"""fan daemon 停止後にだけ動かす emergency Max handoff。

決定記録 0028 §2.7 に従い、このモジュールは標準ライブラリ以外に依存せず、
Safety Config やモデルを読まない。書ける値は hwmon ABI の Max ``255`` と
manual mode ``1`` だけである。呼び出し側は、fan daemon の writer が停止した
ことを systemd で保証してから実行する。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

HWMON_MAX_PWM = "255\n"
HWMON_MANUAL_MODE = "1\n"
HANDOFF_RECORD_PATH = Path("/run/coldaisle/fan-handoff.json")
HWMON_ROOT = Path("/sys/class/hwmon")
_ZONES = frozenset({"front", "rear", "top"})
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
    if not record_path.exists():
        return HandoffResult(record_found=False)

    headers = _load_record(record_path)
    root = sysfs_root.resolve(strict=True)
    for header in headers:
        _validate_header_paths(header)
    _reject_duplicate_relative_targets(headers)

    resolved: dict[str, tuple[Path, Path, Path, Path] | OSError] = {}
    for header in headers:
        try:
            resolved[header.zone] = _resolve_header(header, root)
        except HandoffRecordError:
            # 不安全な path が1つでもあれば、一部を書く前に record 全体を拒否する。
            raise
        except OSError as exc:
            resolved[header.zone] = exc
    _reject_duplicate_targets(
        tuple(paths for paths in resolved.values() if isinstance(paths, tuple))
    )

    results: list[HandoffZoneResult] = []
    for header in headers:
        paths = resolved[header.zone]
        if isinstance(paths, OSError):
            results.append(
                HandoffZoneResult(
                    zone=header.zone,
                    status="io_error",
                    phase="resolve",
                    detail=type(paths).__name__,
                )
            )
            continue
        name_path, label_path, pwm_path, enable_path = paths
        try:
            name = name_path.read_text(encoding="utf-8").strip()
            label = label_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            results.append(
                HandoffZoneResult(
                    zone=header.zone,
                    status="io_error",
                    phase="identity",
                    detail=type(exc).__name__,
                )
            )
            continue
        if name != header.expected_name or label != header.expected_label:
            results.append(
                HandoffZoneResult(
                    zone=header.zone,
                    status="identity_mismatch",
                    phase="identity",
                    detail="driver_or_label",
                )
            )
            continue
        # auto のまま PWM を Max にしてから manual に切り替えれば、途中で
        # プロセスが止まっても一時的に冷却を下げる書き込みにはならない。
        try:
            pwm_path.write_text(HWMON_MAX_PWM, encoding="ascii")
        except OSError as exc:
            results.append(
                HandoffZoneResult(
                    zone=header.zone,
                    status="io_error",
                    phase="pwm",
                    detail=type(exc).__name__,
                )
            )
            continue
        try:
            enable_path.write_text(HWMON_MANUAL_MODE, encoding="ascii")
        except OSError as exc:
            results.append(
                HandoffZoneResult(
                    zone=header.zone,
                    status="io_error",
                    phase="enable",
                    detail=type(exc).__name__,
                )
            )
            continue
        try:
            pwm_readback = pwm_path.read_text(encoding="ascii").strip()
            enable_readback = enable_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            results.append(
                HandoffZoneResult(
                    zone=header.zone,
                    status="io_error",
                    phase="readback",
                    detail=type(exc).__name__,
                )
            )
            continue
        mismatch = []
        if pwm_readback != HWMON_MAX_PWM.strip():
            mismatch.append("pwm")
        if enable_readback != HWMON_MANUAL_MODE.strip():
            mismatch.append("enable")
        if mismatch:
            results.append(
                HandoffZoneResult(
                    zone=header.zone,
                    status="readback_mismatch",
                    phase="readback",
                    detail=",".join(mismatch),
                )
            )
            continue
        results.append(HandoffZoneResult(zone=header.zone, status="applied", phase="complete"))
    return HandoffResult(record_found=True, zones=tuple(results))


def main() -> int:
    """systemd ``ExecStopPost`` 向けの固定入力 entry point。

    書き込む値や対象 root を引数で差し替える経路は持たない。identity
    mismatch は対象外 header へ書かず、非0で systemd へ通知する。
    """
    result = emergency_handoff(HANDOFF_RECORD_PATH, HWMON_ROOT)
    failed = bool(result.identity_mismatch_zones or result.failed_zones)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("coldaisle.safety_handoff").log(
        logging.ERROR if failed else logging.INFO,
        json.dumps(
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
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    return 1 if failed else 0


def _load_record(path: Path) -> tuple[_HeaderRecord, ...]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HandoffRecordError("handoff record を読めない") from exc
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


def _resolve_header(header: _HeaderRecord, root: Path) -> tuple[Path, Path, Path, Path]:
    paths = (
        _resolve_beneath(root, header.name_path),
        _resolve_beneath(root, header.label_path),
        _resolve_beneath(root, header.pwm_path),
        _resolve_beneath(root, header.enable_path),
    )
    if len({path.parent for path in paths}) != 1:
        raise HandoffRecordError("handoff header の解決先が同一 device ではない")
    return paths


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


def _resolve_beneath(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    class_entry = root / candidate.parts[0]
    device_root = class_entry.resolve(strict=True)
    resolved = (device_root / candidate.parts[1]).resolve(strict=True)
    if not resolved.is_relative_to(device_root):
        raise HandoffRecordError("handoff attribute が解決後の hwmon device 外を指している")
    return resolved


def _reject_duplicate_relative_targets(headers: tuple[_HeaderRecord, ...]) -> None:
    targets = {(header.pwm_path, header.enable_path) for header in headers}
    if len(targets) != len(headers):
        raise HandoffRecordError("handoff record で複数 zone が同じ header を指している")


def _reject_duplicate_targets(headers: tuple[tuple[Path, Path, Path, Path], ...]) -> None:
    targets = [(paths[2], paths[3]) for paths in headers]
    if len(set(targets)) != len(targets):
        raise HandoffRecordError("handoff record で複数 zone が同じ header を指している")


if __name__ == "__main__":
    raise SystemExit(main())
