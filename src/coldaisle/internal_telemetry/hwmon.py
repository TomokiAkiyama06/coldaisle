"""driver名とlabelから探索する読み取り専用 hwmon adapter。#65"""

from __future__ import annotations

import math
import re
from pathlib import Path

from coldaisle.internal_telemetry.config import (
    HwmonConfig,
    HwmonMeasurement,
    HwmonSensorConfig,
)
from coldaisle.internal_telemetry.models import AdapterResult, SourceStatus
from coldaisle.store import Quality, Reading

_LABEL_GLOBS = {
    HwmonMeasurement.TEMPERATURE: "temp[0-9]*_label",
    HwmonMeasurement.POWER: "power[0-9]*_label",
    HwmonMeasurement.RPM: "fan[0-9]*_label",
    HwmonMeasurement.PWM: "fan[0-9]*_label",
}
_PREFIX_PATTERN = re.compile(r"^(temp|power|fan)([0-9]+)$")
HWMON_PWM_MAX = 255.0
"""Linux hwmon ABI の raw PWM 上限。運用で調整する値ではない。"""


class HwmonAdapter:
    """``hwmonN`` を固定せず、各 poll で stable selector を解決する。"""

    name = "hwmon"

    def __init__(self, config: HwmonConfig) -> None:
        self._config = config
        self._enabled = tuple(sensor for sensor in config.sensors if sensor.enabled)

    @property
    def expected_metrics(self) -> tuple[str, ...]:
        if not self._config.enabled:
            return ()
        return tuple(sensor.metric for sensor in self._enabled)

    def poll(self) -> AdapterResult:
        """設定された全 sensor を独立に読む。一部失敗で source 全体を落とさない。"""
        if not self._config.enabled or not self._enabled:
            return AdapterResult(source=self.name, status=SourceStatus.DISABLED)
        readings = tuple(self._read_sensor(sensor) for sensor in self._enabled)
        available = sum(reading.quality is not Quality.MISSING for reading in readings)
        required_missing = any(
            sensor.required and reading.quality is not Quality.OK
            for sensor, reading in zip(self._enabled, readings, strict=True)
        )
        if available == 0:
            status = SourceStatus.UNAVAILABLE
        elif required_missing:
            status = SourceStatus.DEGRADED
        else:
            status = SourceStatus.OK
        return AdapterResult(source=self.name, status=status, readings=readings)

    def _read_sensor(self, sensor: HwmonSensorConfig) -> Reading:
        paths = self._matching_attributes(sensor)
        if len(paths) != 1:
            return _missing(sensor.metric)
        try:
            raw = float(paths[0].read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return _missing(sensor.metric)
        value = _convert(raw, sensor.measurement)
        if not math.isfinite(value):
            return Reading(metric=sensor.metric, value=None, quality=Quality.SUSPECT)
        quality = Quality.OK
        if (
            sensor.minimum is not None
            and sensor.maximum is not None
            and not sensor.minimum <= value <= sensor.maximum
        ):
            quality = Quality.SUSPECT
        return Reading(metric=sensor.metric, value=value, quality=quality)

    def _matching_attributes(self, sensor: HwmonSensorConfig) -> tuple[Path, ...]:
        assert sensor.driver is not None
        matches: list[Path] = []
        try:
            devices = tuple(self._config.root.glob("hwmon*"))
        except OSError:
            return ()
        for device in devices:
            if _read_text(device / "name") != sensor.driver:
                continue
            if sensor.channel is not None:
                attribute = _attribute_name(sensor.channel, sensor.measurement)
                if attribute is not None and (device / attribute).is_file():
                    matches.append(device / attribute)
                continue
            assert sensor.label is not None
            for label_path in device.glob(_LABEL_GLOBS[sensor.measurement]):
                if _read_text(label_path) != sensor.label:
                    continue
                prefix = label_path.name.removesuffix("_label")
                attribute = _attribute_name(prefix, sensor.measurement)
                if attribute is not None and (device / attribute).is_file():
                    matches.append(device / attribute)
        return tuple(matches)

    def close(self) -> None:
        """hwmon は poll ごとに開くため、保持 resource はない。"""


def _attribute_name(prefix: str, measurement: HwmonMeasurement) -> str | None:
    matched = _PREFIX_PATTERN.match(prefix)
    if matched is None:
        return None
    expected_prefix = {
        HwmonMeasurement.TEMPERATURE: "temp",
        HwmonMeasurement.POWER: "power",
        HwmonMeasurement.RPM: "fan",
        HwmonMeasurement.PWM: "fan",
    }[measurement]
    if matched.group(1) != expected_prefix:
        return None
    number = matched.group(2)
    if measurement is HwmonMeasurement.PWM:
        return f"pwm{number}"
    return f"{prefix}_input"


def _convert(raw: float, measurement: HwmonMeasurement) -> float:
    if measurement is HwmonMeasurement.TEMPERATURE:
        return raw / 1_000.0
    if measurement is HwmonMeasurement.POWER:
        return raw / 1_000_000.0
    if measurement is HwmonMeasurement.PWM:
        return raw / HWMON_PWM_MAX * 100.0
    return raw


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _missing(metric: str) -> Reading:
    return Reading(metric=metric, value=None, quality=Quality.MISSING)
