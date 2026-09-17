from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.control.config import ControlConfig


def provisional(value: float | int) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def valid_documents() -> dict[str, dict[str, object]]:
    profile = {
        "startup_demand": 0.5,
        "minimum_stable_demand": 0.3,
        "maximum_rpm": 1600,
        "pwm_to_rpm": [
            {"demand": 0.3, "rpm": 600},
            {"demand": 1.0, "rpm": 1600},
        ],
        "airflow_index": [0.2, 1.0],
    }
    return {
        "fan-hardware.yaml": {
            "schema_version": 1,
            "approval": {"status": "provisional"},
            "zones": {
                "front": {
                    "driver": "mock-superio",
                    "label": "front-header",
                    "pwm_attribute": "pwm1",
                    "tach_attribute": "fan1_input",
                    "enable_attribute": "pwm1_enable",
                    "profile": profile,
                },
                "rear": {
                    "driver": "mock-superio",
                    "label": "rear-header",
                    "pwm_attribute": "pwm2",
                    "tach_attribute": "fan2_input",
                    "enable_attribute": "pwm2_enable",
                    "profile": profile,
                },
                "top": {
                    "driver": "mock-superio",
                    "label": "top-header",
                    "pwm_attribute": "pwm3",
                    "tach_attribute": "fan3_input",
                    "enable_attribute": "pwm3_enable",
                    "profile": profile,
                },
            },
        },
        "safety.yaml": {
            "schema_version": 1,
            "absolute_temp_ceiling_c": provisional(85.0),
            "zone_min_demand": {
                "front": provisional(0.4),
                "rear": provisional(0.4),
                "top": provisional(0.5),
            },
            "cpu_cooling_floor": [
                {"temperature_c": provisional(35.0), "demand": provisional(0.5)},
                {"temperature_c": provisional(80.0), "demand": provisional(1.0)},
            ],
            "fault_demand": provisional(1.0),
            "stall_check_min_demand": {
                "front": provisional(0.4),
                "rear": provisional(0.4),
                "top": provisional(0.5),
            },
            "stall_min_rpm": {
                "front": provisional(400),
                "rear": provisional(400),
                "top": provisional(400),
            },
            "stall_window_ms": provisional(2000),
            "write_fail_emergency_after": provisional(3),
            "telemetry": {
                "cpu_ms": provisional(1000),
                "gpu_ms": provisional(1000),
                "t_sensor": {"enabled": provisional(False)},
                "air_ms": provisional(1000),
                "air_sensor_period_ms": provisional(500),
            },
            "ramp_down_per_s": provisional(0.1),
            "startup_settle_ms": provisional(3000),
            "fault_clear_hold_ms": provisional(3000),
            "tick_deadline_ms": provisional(100),
            "overrun_consecutive_limit": provisional(3),
            "watchdog_timeout_ms": provisional(5000),
        },
        "fan-policy.yaml": {
            "schema_version": 1,
            "fallback_curve": [
                {"temperature_c": 25.0, "demand": 0.3},
                {"temperature_c": 80.0, "demand": 1.0},
            ],
            "reactive_guard": {
                "floor": provisional(0.4),
                "ceiling": provisional(1.0),
                "hold_ms": provisional(1000),
                "intake_rise_threshold_c": provisional(2.0),
                "gpu_hotspot_threshold_c": provisional(85.0),
            },
            "mpc": {"period_ms": 1000, "budget_ms": 100, "valid_ms": 2000},
            "supervisor": {"period_ms": 1000, "valid_ms": 2000},
            "gate_min_confidence": provisional(0.0),
            "authority_stage": "shadow",
            "authority_limits": {
                "limited": {"permitted_zones": ["front"], "limit_up": 0.1, "limit_down": 0.1},
                "expanded": {
                    "permitted_zones": ["front", "rear", "top"],
                    "limit_up": 0.2,
                    "limit_down": 0.2,
                },
            },
            "recovery_hold_ms": 1000,
            "demote_window_ms": 60000,
            "demote_after": 3,
        },
    }


def write_documents(directory: Path, documents: dict[str, dict[str, object]]) -> None:
    for name, document in documents.items():
        (directory / name).write_text(yaml.safe_dump(document), encoding="utf-8")


def load_config(tmp_path: Path) -> ControlConfig:
    write_documents(tmp_path, valid_documents())
    return ControlConfig.from_directory(tmp_path)


def test_complete_config_has_traceable_sources_and_is_not_actuation_ready(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert config.actuation_permitted is False
    metadata = config.trace_metadata()["control_config"]
    assert metadata["fan_hardware"]["name"] == "fan-hardware.yaml"
    assert len(metadata["safety"]["sha256"]) == 64


def test_missing_or_unknown_config_is_rejected_before_activation(tmp_path: Path) -> None:
    documents = valid_documents()
    del documents["safety.yaml"]
    write_documents(tmp_path, documents)
    with pytest.raises(FileNotFoundError):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["safety.yaml"]["typo"] = True
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ControlConfig.from_directory(tmp_path)


def test_confirmed_value_requires_basis(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["safety.yaml"]["absolute_temp_ceiling_c"] = {
        "value": 85.0,
        "status": "confirmed",
    }
    write_documents(tmp_path, documents)

    with pytest.raises(ValidationError, match="basis"):
        ControlConfig.from_directory(tmp_path)


def test_cross_field_validation_rejects_unsafe_or_unstable_values(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["safety.yaml"]["fault_demand"] = provisional(0.2)
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="fault_demand"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["reactive_guard"]["ceiling"] = provisional(0.2)
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="ceiling"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["safety.yaml"]["stall_check_min_demand"]["front"] = provisional(0.5)
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match=r"stall_check_min_demand\.front"):
        ControlConfig.from_directory(tmp_path)


def test_unstable_hwmon_number_is_rejected(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["fan-hardware.yaml"]["zones"]["front"]["label"] = "hwmon2"
    write_documents(tmp_path, documents)

    with pytest.raises(ValidationError, match="hwmonN"):
        ControlConfig.from_directory(tmp_path)


def test_absolute_path_in_hardware_mapping_is_rejected(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["fan-hardware.yaml"]["zones"]["front"]["label"] = "/sys/class/hwmon/hwmon2"
    write_documents(tmp_path, documents)

    with pytest.raises(ValidationError, match="pattern"):
        ControlConfig.from_directory(tmp_path)


def test_new_config_is_validated_separately_and_never_replaces_running_config(
    tmp_path: Path,
) -> None:
    active = load_config(tmp_path)
    candidate_documents = valid_documents()
    candidate_documents["safety.yaml"]["fault_demand"] = provisional(0.9)
    write_documents(tmp_path, candidate_documents)

    candidate = ControlConfig.from_directory(tmp_path)
    assert active.safety.fault_demand.value == 1.0
    assert candidate.safety.fault_demand.value == 0.9
    assert not hasattr(active, "reload_from_directory")


def test_disabled_t_sensor_has_no_stale_delay_and_enabled_sensor_needs_approval(
    tmp_path: Path,
) -> None:
    config = load_config(tmp_path)
    assert config.safety.telemetry.t_sensor.enabled.value is False
    assert config.safety.telemetry.t_sensor.stale_after_ms is None

    documents = valid_documents()
    documents["safety.yaml"]["telemetry"]["t_sensor"] = {
        "enabled": provisional(True),
        "stale_after_ms": provisional(1000),
    }
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="confirmed"):
        ControlConfig.from_directory(tmp_path)

    documents["safety.yaml"]["telemetry"]["t_sensor"]["enabled"] = {
        "value": True,
        "status": "confirmed",
        "basis": "docs/decisions/0029-t-sensor.md",
    }
    write_documents(tmp_path, documents)
    assert ControlConfig.from_directory(tmp_path).safety.telemetry.t_sensor.enabled.value is True


def test_enable_attribute_must_match_pwm_channel(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["fan-hardware.yaml"]["zones"]["front"]["enable_attribute"] = "pwm2_enable"
    write_documents(tmp_path, documents)

    with pytest.raises(ValidationError, match="同じ channel"):
        ControlConfig.from_directory(tmp_path)


def test_invalid_candidate_is_rejected_without_changing_loaded_config(tmp_path: Path) -> None:
    active = load_config(tmp_path)
    documents = valid_documents()
    documents["safety.yaml"]["unknown"] = True
    write_documents(tmp_path, documents)

    with pytest.raises(ValidationError):
        ControlConfig.from_directory(tmp_path)
    assert active.safety.fault_demand.value == 1.0


def test_provisional_values_identify_safety_and_policy_without_exposing_values(
    tmp_path: Path,
) -> None:
    values = load_config(tmp_path).provisional_values()

    assert {item.source for item in values} == {
        "fan-hardware.yaml",
        "safety.yaml",
        "fan-policy.yaml",
    }
    assert any(item.path == "fault_demand" for item in values)
    assert any(item.path == "stall_check_min_demand.front" for item in values)
    assert any(item.path == "write_fail_emergency_after" for item in values)
    assert any(item.path == "telemetry.t_sensor.enabled" for item in values)
    assert any(item.path == "reactive_guard.ceiling" for item in values)
    assert all("value" not in item.model_dump() for item in values)


def test_authority_limits_are_typed_per_stage_and_reject_empty_zone_set(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    limited = config.policy.authority_limits.limited
    assert limited.permitted_zones == {"front"}
    assert limited.limit_up == 0.1

    documents = valid_documents()
    documents["fan-policy.yaml"]["authority_limits"]["limited"]["permitted_zones"] = []
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="at least 1 item"):
        ControlConfig.from_directory(tmp_path)


def test_mpc_and_supervisor_validity_windows_are_required_and_budget_is_bounded(
    tmp_path: Path,
) -> None:
    documents = valid_documents()
    del documents["fan-policy.yaml"]["mpc"]["valid_ms"]
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="valid_ms"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["mpc"]["budget_ms"] = 2000
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match=r"mpc.budget_ms"):
        ControlConfig.from_directory(tmp_path)
