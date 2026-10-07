"""hwmon discovery を一時 sysfs fixture で検証する。実機 path は使わない。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.internal_telemetry import (
    ConfirmationEvidence,
    ConfirmationStatus,
    HwmonAdapter,
    HwmonConfig,
    HwmonMeasurement,
    HwmonSensorConfig,
    InternalTelemetryConfig,
    SourceStatus,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store import Quality
from conftest import CONFIG_DIR


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def sensor(
    metric: str,
    measurement: HwmonMeasurement,
    *,
    driver: str = "example_driver",
    label: str = "Example Label",
    channel: str | None = None,
    required: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
    confirmed: bool = True,
) -> HwmonSensorConfig:
    return HwmonSensorConfig(
        metric=metric,
        enabled=True,
        driver=driver,
        label=None if channel is not None else label,
        channel=channel,
        measurement=measurement,
        required=required,
        minimum=minimum,
        maximum=maximum,
        confirmation=(
            ConfirmationEvidence(
                status=ConfirmationStatus.CONFIRMED,
                basis="fixture physical-input check and owner approval",
            )
            if confirmed
            else None
        ),
        disabled_reason=None,
    )


def adapter(root: Path, *sensors: HwmonSensorConfig) -> HwmonAdapter:
    return HwmonAdapter(HwmonConfig(enabled=True, root=root, sensors=sensors))


def test_discovers_by_driver_and_label_and_converts_units(tmp_path: Path):
    device = tmp_path / "hwmon7"
    write(device / "name", "example_driver\n")
    write(device / "temp4_label", "CPU Package\n")
    write(device / "temp4_input", "62500\n")
    write(device / "power2_label", "CPU Package\n")
    write(device / "power2_input", "175000000\n")
    write(device / "fan9_label", "Front Intake\n")
    write(device / "fan9_input", "1450\n")
    write(device / "pwm9", "128\n")
    reader = adapter(
        tmp_path,
        sensor("cpu.package", HwmonMeasurement.TEMPERATURE, label="CPU Package", required=True),
        sensor("power.cpu.package", HwmonMeasurement.POWER, label="CPU Package"),
        sensor("fan.front.rpm", HwmonMeasurement.RPM, label="Front Intake"),
        sensor("fan.front.pwm", HwmonMeasurement.PWM, label="Front Intake"),
    )

    result = reader.poll()
    readings = {reading.metric: reading for reading in result.readings}

    assert result.status is SourceStatus.OK
    assert readings["cpu.package"].value == 62.5
    assert readings["power.cpu.package"].value == 175.0
    assert readings["fan.front.rpm"].value == 1450.0
    assert readings["fan.front.pwm"].value == pytest.approx(128 / 255 * 100)


def test_hwmon_number_change_is_rediscovered(tmp_path: Path):
    original = tmp_path / "hwmon2"
    write(original / "name", "example_driver")
    write(original / "temp1_label", "Example Label")
    write(original / "temp1_input", "40000")
    reader = adapter(tmp_path, sensor("cpu.package", HwmonMeasurement.TEMPERATURE))
    assert reader.poll().readings[0].value == 40.0

    moved = tmp_path / "hwmon42"
    original.rename(moved)
    write(moved / "temp1_input", "41000")

    assert reader.poll().readings[0].value == 41.0


def test_stable_channel_is_supported_when_driver_has_no_label(tmp_path: Path):
    device = tmp_path / "hwmon5"
    write(device / "name", "example_driver")
    write(device / "fan3_input", "900")
    write(device / "pwm3", "64")
    reader = adapter(
        tmp_path,
        sensor("fan.rear.rpm", HwmonMeasurement.RPM, channel="fan3"),
        sensor("fan.rear.pwm", HwmonMeasurement.PWM, channel="fan3"),
    )

    result = {reading.metric: reading for reading in reader.poll().readings}

    assert result["fan.rear.rpm"].value == 900.0
    assert result["fan.rear.pwm"].value == pytest.approx(64 / 255 * 100)


def test_unconfirmed_physical_mapping_cannot_be_enabled():
    with pytest.raises(ValidationError, match="物理入力の実機確認"):
        sensor(
            "fan.rear.rpm",
            HwmonMeasurement.RPM,
            channel="fan3",
            confirmed=False,
        )


def test_ambiguous_or_missing_selector_is_not_guessed(tmp_path: Path):
    for number in (1, 8):
        device = tmp_path / f"hwmon{number}"
        write(device / "name", "example_driver")
        write(device / "temp1_label", "Example Label")
        write(device / "temp1_input", "40000")
    reader = adapter(
        tmp_path,
        sensor("cpu.package", HwmonMeasurement.TEMPERATURE, required=True),
    )

    result = reader.poll()

    assert result.status is SourceStatus.UNAVAILABLE
    assert result.readings[0].quality is Quality.MISSING
    assert result.readings[0].value is None


def test_t_sensor_outside_configured_range_is_suspect(tmp_path: Path):
    device = tmp_path / "hwmon3"
    write(device / "name", "example_driver")
    write(device / "temp6_label", "T Sensor")
    write(device / "temp6_input", "130000")
    reader = adapter(
        tmp_path,
        sensor(
            "board.connector_12v2x6",
            HwmonMeasurement.TEMPERATURE,
            label="T Sensor",
            required=True,
            minimum=-20.0,
            maximum=125.0,
        ),
    )

    result = reader.poll()

    assert result.status is SourceStatus.DEGRADED
    assert result.readings[0].value == 130.0
    assert result.readings[0].quality is Quality.SUSPECT


def test_stable_selector_rejects_hwmon_number_and_path():
    with pytest.raises(ValidationError, match="hwmonN"):
        sensor("cpu.package", HwmonMeasurement.TEMPERATURE, driver="hwmon2")
    with pytest.raises(ValidationError, match="path"):
        sensor("cpu.package", HwmonMeasurement.TEMPERATURE, label="/temp1")
    with pytest.raises(ValidationError, match="label または"):
        HwmonSensorConfig(
            metric="cpu.package",
            enabled=True,
            driver="example_driver",
            label="Package",
            channel="temp1",
            measurement=HwmonMeasurement.TEMPERATURE,
            confirmation=ConfirmationEvidence(
                status=ConfirmationStatus.CONFIRMED,
                basis="fixture physical-input check and owner approval",
            ),
        )
    with pytest.raises(ValidationError, match="hwmonN path"):
        HwmonConfig(enabled=True, root=Path("/sys/class/hwmon/hwmon2"))


def test_enabled_t_sensor_requires_a_minimum():
    with pytest.raises(ValidationError, match="minimum"):
        sensor("board.connector_12v2x6", HwmonMeasurement.TEMPERATURE)
    with pytest.raises(ValidationError, match="minimum"):
        sensor("board.connector_12v2x6", HwmonMeasurement.TEMPERATURE, maximum=125.0)


def test_range_may_be_one_sided_but_not_inverted():
    """片側だけの範囲を許す（決定記録 0110 §2.4）。両方あるなら minimum < maximum。"""
    assert sensor("cpu.package", HwmonMeasurement.TEMPERATURE, minimum=0.0).maximum is None
    assert sensor("cpu.package", HwmonMeasurement.TEMPERATURE, maximum=125.0).minimum is None
    with pytest.raises(ValidationError, match="minimum は maximum"):
        sensor("cpu.package", HwmonMeasurement.TEMPERATURE, minimum=10.0, maximum=10.0)


@pytest.mark.parametrize(
    ("raw", "value", "quality"),
    [
        ("-40000", -40.0, Quality.SUSPECT),
        ("-500", -0.5, Quality.SUSPECT),
        ("0", 0.0, Quality.OK),
        ("49500", 49.5, Quality.OK),
        ("200000", 200.0, Quality.OK),
    ],
)
def test_t_sensor_below_zero_is_suspect_and_there_is_no_upper_bound(
    tmp_path: Path, raw: str, value: float, quality: Quality
):
    """断線時の負の値は suspect（使えない入力）。上限は置かない（0110 §2.4）。"""
    device = tmp_path / "hwmon4"
    write(device / "name", "example_ec")
    write(device / "temp9_label", "T Sensor")
    write(device / "temp9_input", raw)
    reader = adapter(
        tmp_path,
        sensor(
            "board.connector_12v2x6",
            HwmonMeasurement.TEMPERATURE,
            driver="example_ec",
            label="T Sensor",
            required=True,
            minimum=0.0,
        ),
    )

    reading = reader.poll().readings[0]

    assert reading.value == value
    assert reading.quality is quality


def test_enabled_t_sensor_requires_confirmed_measurement_and_owner_approval():
    with pytest.raises(ValidationError, match="物理入力の実機確認"):
        sensor(
            "board.connector_12v2x6",
            HwmonMeasurement.TEMPERATURE,
            minimum=-20.0,
            maximum=125.0,
            confirmed=False,
        )

    with pytest.raises(ValidationError, match="basis"):
        ConfirmationEvidence(status=ConfirmationStatus.CONFIRMED)


def test_hwmon_poll_has_no_write_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    device = tmp_path / "hwmon1"
    write(device / "name", "example_driver")
    write(device / "temp1_label", "Example Label")
    write(device / "temp1_input", "42000")
    reader = adapter(tmp_path, sensor("cpu.package", HwmonMeasurement.TEMPERATURE))

    def reject_write(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("read-only telemetry adapter attempted a write")

    monkeypatch.setattr(Path, "write_text", reject_write)

    result = reader.poll()

    assert result.readings[0].value == 42.0


def test_repository_config_enables_t_sensor_from_asusec_label():
    """0110 §2.1 / §2.4: asusec の T_Sensor、下限 0 °C、上限なし、所有者承認の confirmed。"""
    config = InternalTelemetryConfig.from_yaml(
        CONFIG_DIR / "internal-telemetry.yaml",
        catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
    )
    t_sensor = next(
        item for item in config.hwmon.sensors if item.metric == "board.connector_12v2x6"
    )

    assert t_sensor.enabled
    assert t_sensor.driver == "asusec"
    assert t_sensor.label == "T_Sensor"
    assert t_sensor.channel is None
    assert t_sensor.required
    assert t_sensor.minimum == 0
    assert t_sensor.maximum is None
    assert t_sensor.confirmation is not None
    assert t_sensor.confirmation.status is ConfirmationStatus.CONFIRMED
    assert t_sensor.confirmation.basis is not None
    assert "2026-10-08" in t_sensor.confirmation.basis
    assert t_sensor.disabled_reason is None


def test_k10temp_labels_resolve_to_cpu_die_metrics(tmp_path: Path):
    """k10temp は Tctl / Tccd* を label で公開する。番号（temp1 / temp3）には依存しない。"""
    device = tmp_path / "hwmon3"
    write(device / "name", "k10temp\n")
    write(device / "temp1_label", "Tctl\n")
    write(device / "temp1_input", "44000\n")
    write(device / "temp3_label", "Tccd1\n")
    write(device / "temp3_input", "43625\n")
    write(device / "temp4_label", "Tccd2\n")
    write(device / "temp4_input", "36500\n")
    reader = adapter(
        tmp_path,
        sensor("cpu.tctl", HwmonMeasurement.TEMPERATURE, driver="k10temp", label="Tctl"),
        sensor("cpu.ccd1", HwmonMeasurement.TEMPERATURE, driver="k10temp", label="Tccd1"),
        sensor("cpu.ccd2", HwmonMeasurement.TEMPERATURE, driver="k10temp", label="Tccd2"),
    )

    readings = {reading.metric: reading.value for reading in reader.poll().readings}

    assert readings == {"cpu.tctl": 44.0, "cpu.ccd1": 43.625, "cpu.ccd2": 36.5}


def test_production_config_enables_k10temp_as_advisory_inputs():
    """所有者承認（2026-09-18）の k10temp 入力が有効で、Critical 扱いの required ではない。"""
    config = InternalTelemetryConfig.from_yaml(
        CONFIG_DIR / "internal-telemetry.yaml",
        catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
    )
    by_metric = {sensor.metric: sensor for sensor in config.hwmon.sensors}

    for metric, label in (("cpu.tctl", "Tctl"), ("cpu.ccd1", "Tccd1"), ("cpu.ccd2", "Tccd2")):
        entry = by_metric[metric]
        assert entry.enabled
        assert (entry.driver, entry.label) == ("k10temp", label)
        assert not entry.required
        assert entry.confirmation is not None
        assert entry.confirmation.status is ConfirmationStatus.CONFIRMED
