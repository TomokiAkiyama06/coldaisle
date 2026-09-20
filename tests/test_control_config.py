from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.control.config import CONTROL_CONFIG_VERSION, ConfigSource, ControlConfig


def provisional(value: float | int) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def guard_band(
    activate: float,
    clear: float,
    degraded_activate: float,
    degraded_clear: float,
) -> dict[str, object]:
    return {
        "activate_above": provisional(activate),
        "clear_at_or_below": provisional(clear),
        "degraded_activate_above": provisional(degraded_activate),
        "degraded_clear_at_or_below": provisional(degraded_clear),
    }


def mpc_optimizer_config() -> dict[str, object]:
    """#86 optimizer の暫定設定。値は実測前なのですべて provisional のままにする。"""
    return {
        "horizon_ms": provisional(60_000),
        "step_ms": provisional(10_000),
        "candidate_levels": provisional(5),
        "sweeps": provisional(2),
        "max_evaluations": provisional(64),
        "max_step_up": provisional(0.2),
        "max_step_down": provisional(0.1),
        "zone_bounds": {
            "front": {"floor": provisional(0.2), "ceiling": provisional(1.0)},
            "rear": {"floor": provisional(0.2), "ceiling": provisional(1.0)},
            "top": {"floor": provisional(0.2), "ceiling": provisional(1.0)},
        },
        "cost_scales": {
            "temperature_c": provisional(5.0),
            "balance_ratio": provisional(0.2),
            "acoustic_cost": provisional(1.0),
            "demand_change": provisional(0.1),
        },
        "cost_metrics": {
            "cpu_temperature": "cpu.package",
            "gpu_temperature": "gpu.0.core",
        },
        "unknown_balance_cost": provisional(1.0),
    }


def supervisor_config() -> dict[str, object]:
    target_band = {
        "cpu_temperature": {"lower_c": 45.0, "upper_c": 75.0},
        "gpu_temperature": {"lower_c": 45.0, "upper_c": 78.0},
    }
    balanced = {
        "strategy": "balanced",
        "weights": {
            "gpu_temperature": 0.8,
            "cpu_temperature": 0.8,
            "balance": 0.5,
            "acoustic": 0.4,
            "change": 0.3,
        },
        "target_band": target_band,
    }
    conservative = {
        "strategy": "conservative",
        "weights": {
            "gpu_temperature": 1.0,
            "cpu_temperature": 1.0,
            "balance": 0.7,
            "acoustic": 0.1,
            "change": 0.5,
        },
        "target_band": target_band,
    }
    return {
        "period_ms": 1000,
        "valid_ms": 2000,
        "active_policy": "rule_policy",
        "shadow_policy": "rl_policy",
        "rl_version": "rl-test-v1",
        "output_bounds": {
            "strategies": ["balanced", "conservative"],
            "target_bands": [target_band],
            "weights": {
                name: {"minimum": 0.0, "maximum": 1.0}
                for name in (
                    "gpu_temperature",
                    "cpu_temperature",
                    "balance",
                    "acoustic",
                    "change",
                )
            },
        },
        "rule_policy": {
            "version": "rule-test-v1",
            "contexts": {
                "idle": balanced,
                "transient_cpu": balanced,
                "transient_gpu": balanced,
                "transient_cpu_gpu": balanced,
                "sustained_cpu": balanced,
                "sustained_gpu": balanced,
                "sustained_cpu_gpu": balanced,
                "cooldown": balanced,
                "unknown": conservative,
            },
        },
    }


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
            "schema_version": 2,
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
            "cpu_power_cooling_floor": [
                {"power_w": provisional(65.0), "demand": provisional(0.5)},
                {"power_w": provisional(250.0), "demand": provisional(1.0)},
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
                "cpu_power_ms": provisional(1000),
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
            "schema_version": 9,
            "fallback_curve": [
                {"temperature_c": 25.0, "demand": 0.3},
                {"temperature_c": 80.0, "demand": 1.0},
            ],
            "fallback_temperature_inputs": {
                "front": {"metrics": ["gpu.0.core"]},
                "rear": {"metrics": ["gpu.0.core"]},
                "top": {"metrics": ["cpu.package"]},
            },
            "fallback_power_feedforward": {
                "front": {
                    "metric": "power.gpu.0",
                    "curve": [
                        {"power_w": 0.0, "demand": 0.3},
                        {"power_w": 600.0, "demand": 1.0},
                    ],
                },
                "rear": {
                    "metric": "power.gpu.0",
                    "curve": [
                        {"power_w": 0.0, "demand": 0.3},
                        {"power_w": 600.0, "demand": 1.0},
                    ],
                },
                "top": {
                    "metric": "power.cpu.package",
                    "curve": [
                        {"power_w": 0.0, "demand": 0.3},
                        {"power_w": 250.0, "demand": 1.0},
                    ],
                },
            },
            "fallback_dynamics": {"decrease_hysteresis": 0.05, "decrease_hold_ms": 2000},
            "reactive_guard": {
                "floor": provisional(0.6),
                "hold_ms": provisional(1000),
                "cpu_power_metric": None,
                "cpu_temperature_rate_c_per_s": guard_band(2.0, 0.5, 1.5, 0.25),
                "gpu_temperature_rate_c_per_s": guard_band(2.0, 0.5, 1.5, 0.25),
                "cpu_power_rate_w_per_s": guard_band(100.0, 20.0, 75.0, 10.0),
                "gpu_power_rate_w_per_s": guard_band(100.0, 20.0, 75.0, 10.0),
                "intake_rise_c": guard_band(2.0, 1.0, 1.5, 0.5),
                "gpu_hotspot_c": guard_band(85.0, 80.0, 82.0, 78.0),
            },
            "mpc": {
                "period_ms": 1000,
                "budget_ms": 100,
                "valid_ms": 2000,
                "optimizer": mpc_optimizer_config(),
            },
            "supervisor": supervisor_config(),
            "workload_regime": {
                "cpu_power": {
                    "metric": "power.cpu.package",
                    "idle_below_w": 30.0,
                    "active_above_w": 60.0,
                },
                "gpu_power": {
                    "metric": "power.gpu.0",
                    "idle_below_w": 40.0,
                    "active_above_w": 100.0,
                },
                "activity_window_ms": 2000,
                "history_window_ms": 120000,
                "minimum_observation_ms": 2000,
                "sustained_after_ms": 60000,
                "cooldown_ms": 30000,
                "minimum_transition_ms": 1000,
                "confidence_full_window_ms": 60000,
                "max_snapshot_gap_ms": 1000,
            },
            "gate_min_confidence": {
                "limited": provisional(0.6),
                "expanded": provisional(0.7),
                "full": provisional(0.8),
            },
            "model_confidence": {
                "high_min_confidence": provisional(0.85),
                "medium_limit": {
                    "limit_up": provisional(0.1),
                    "limit_down": provisional(0.05),
                },
                "range_margin": provisional(0.1),
                "min_support_count": provisional(1),
                "full_support_count": provisional(5),
                "min_missing_pattern_count": provisional(1),
                "residual_window": provisional(20),
                "residual_min_samples": provisional(5),
                "residual_match_tolerance_ms": provisional(500),
                "residual_max_age_ms": provisional(60_000),
                "residual_drift_ood_ratio": provisional(3.0),
                "cap_without_uncertainty": provisional(0.9),
                "cap_before_residual_evidence": provisional(0.7),
            },
            "authority_stage": "shadow",
            "authority_limits": {
                "limited": {"permitted_zones": ["front"], "limit_up": 0.1, "limit_down": 0.1},
                "expanded": {
                    "permitted_zones": ["front", "rear", "top"],
                    "limit_up": 0.2,
                    "limit_down": 0.2,
                },
            },
            "authority_rollout": {
                "approval_max_age_ms": provisional(3_600_000),
                "evidence_max_age_ms": provisional(604_800_000),
                "unhealthy_window_ms": provisional(600_000),
                "low_confidence_after": provisional(10),
                "ood_after": provisional(5),
            },
            "shadow": {
                "enabled": True,
                "outcome_match_tolerance_ms": provisional(2000),
                "applied_demand_tolerance": provisional(0.01),
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

    assert CONTROL_CONFIG_VERSION == 9
    assert config.actuation_permitted is False
    metadata = config.trace_metadata()["control_config"]
    assert metadata["fan_hardware"]["name"] == "fan-hardware.yaml"
    assert metadata["safety"]["schema_version"] == 2
    assert metadata["policy"]["schema_version"] == 9
    assert len(metadata["safety"]["sha256"]) == 64


def test_safety_v1_is_rejected_without_defaulting_new_safety_fields(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["safety.yaml"]["schema_version"] = 1
    write_documents(tmp_path, documents)

    with pytest.raises(ValidationError, match="schema_version"):
        ControlConfig.from_directory(tmp_path)


def test_trace_source_version_must_match_the_validated_file_model(tmp_path: Path) -> None:
    raw = load_config(tmp_path).model_dump(mode="python")
    raw["sources"]["safety"]["schema_version"] = 1

    with pytest.raises(ValidationError, match="ConfigSource"):
        ControlConfig.model_validate(raw)


def test_config_source_can_record_future_subconfig_versions_without_weakening_models() -> None:
    source = ConfigSource(
        name="fan-policy.yaml",
        schema_version=3,
        sha256="0" * 64,
    )

    assert source.schema_version == 3


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
    documents["fan-policy.yaml"]["reactive_guard"]["gpu_hotspot_c"]["clear_at_or_below"] = (
        provisional(90.0)
    )
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="解除閾値"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["safety.yaml"]["stall_check_min_demand"]["front"] = provisional(0.5)
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match=r"stall_check_min_demand\.front"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["safety.yaml"]["cpu_power_cooling_floor"] = [
        {"power_w": provisional(250.0), "demand": provisional(1.0)},
        {"power_w": provisional(65.0), "demand": provisional(0.5)},
    ]
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="cpu_power_cooling_floor の Power は単調増加"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["safety.yaml"]["cpu_power_cooling_floor"] = [
        {"power_w": provisional(65.0), "demand": provisional(1.0)},
        {"power_w": provisional(250.0), "demand": provisional(0.5)},
    ]
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="cpu_power_cooling_floor の demand は下げない"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    del documents["safety.yaml"]["cpu_power_cooling_floor"]
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="cpu_power_cooling_floor"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["reactive_guard"]["gpu_hotspot_c"]["degraded_activate_above"] = (
        provisional(90.0)
    )
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="保守側"):
        ControlConfig.from_directory(tmp_path)


def test_cpu_power_guard_metric_requires_confirmed_approval(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["fan-policy.yaml"]["reactive_guard"]["cpu_power_metric"] = {
        "value": "power.cpu.package",
        "status": "provisional",
    }
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="confirmed"):
        ControlConfig.from_directory(tmp_path)

    documents["fan-policy.yaml"]["reactive_guard"]["cpu_power_metric"] = {
        "value": "not-a-metric",
        "status": "confirmed",
        "basis": "approved metric contract",
    }
    write_documents(tmp_path, documents)
    with pytest.raises(ValueError, match="metric"):
        ControlConfig.from_directory(tmp_path)

    documents["fan-policy.yaml"]["reactive_guard"]["cpu_power_metric"] = {
        "value": "gpu.0.core",
        "status": "confirmed",
        "basis": "approved metric contract",
    }
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="power ドメイン"):
        ControlConfig.from_directory(tmp_path)

    documents["fan-policy.yaml"]["reactive_guard"]["cpu_power_metric"] = {
        "value": "power.cpu.package",
        "status": "confirmed",
        "basis": "DR0032 FINAL and owner approval",
    }
    write_documents(tmp_path, documents)
    assert ControlConfig.from_directory(tmp_path).policy.reactive_guard.cpu_power_metric is not None


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
    assert any(item.path == "cpu_power_cooling_floor[0].power_w" for item in values)
    assert any(item.path == "telemetry.cpu_power_ms" for item in values)
    assert any(
        item.path == "reactive_guard.gpu_temperature_rate_c_per_s.activate_above" for item in values
    )
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


def test_fallback_inputs_and_power_curves_are_validated(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["fan-policy.yaml"]["fallback_temperature_inputs"]["front"]["metrics"] = [
        "gpu.0.core",
        "gpu.0.core",
    ]
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="重複"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["fallback_power_feedforward"]["front"]["curve"] = [
        {"power_w": 200.0, "demand": 0.5},
        {"power_w": 100.0, "demand": 0.6},
    ]
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="power_w"):
        ControlConfig.from_directory(tmp_path)


def test_confidence_thresholds_are_validated_per_authority_stage(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.policy.gate_min_confidence.limited.value == 0.6
    assert config.policy.gate_min_confidence.full.value == 0.8

    documents = valid_documents()
    documents["fan-policy.yaml"]["gate_min_confidence"] = {
        "limited": provisional(0.9),
        "expanded": provisional(0.7),
        "full": provisional(0.8),
    }
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="limited <= expanded <= full"):
        ControlConfig.from_directory(tmp_path)


def test_workload_regime_thresholds_and_windows_are_configured(tmp_path: Path) -> None:
    config = load_config(tmp_path).policy.workload_regime

    assert config.cpu_power.metric == "power.cpu.package"
    assert config.gpu_power.active_above_w == 100.0

    documents = valid_documents()
    documents["fan-policy.yaml"]["workload_regime"]["cpu_power"]["idle_below_w"] = 70.0
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="active_above_w"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["workload_regime"]["sustained_after_ms"] = 120001
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="history_window_ms"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["workload_regime"]["history_window_ms"] = 62000
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="観測・SUSTAINED判定"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["workload_regime"]["history_window_ms"] = 32000
    documents["fan-policy.yaml"]["workload_regime"]["sustained_after_ms"] = 1000
    documents["fan-policy.yaml"]["workload_regime"]["confidence_full_window_ms"] = 30000
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="観測・COOLDOWN判定"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    cpu_metric = documents["fan-policy.yaml"]["workload_regime"]["cpu_power"]["metric"]
    documents["fan-policy.yaml"]["workload_regime"]["gpu_power"]["metric"] = cpu_metric
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="別々"):
        ControlConfig.from_directory(tmp_path)


def test_supervisor_selection_and_output_bounds_are_validated(tmp_path: Path) -> None:
    config = load_config(tmp_path).policy.supervisor
    assert config.active_policy.value == "rule_policy"
    assert config.shadow_policy is not None and config.shadow_policy.value == "rl_policy"

    documents = valid_documents()
    supervisor = documents["fan-policy.yaml"]["supervisor"]
    supervisor["active_policy"] = "rl_policy"
    supervisor["shadow_policy"] = None
    write_documents(tmp_path, documents)
    assert (
        ControlConfig.from_directory(tmp_path).policy.supervisor.active_policy.value == "rl_policy"
    )

    documents = valid_documents()
    documents["fan-policy.yaml"]["supervisor"]["active_policy"] = "rl_policy"
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="active と shadow"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["supervisor"]["rl_version"] = None
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="rl_version"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["supervisor"]["rule_policy"]["contexts"]["unknown"]["strategy"] = (
        "unconfigured"
    )
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="設定済み候補"):
        ControlConfig.from_directory(tmp_path)


@pytest.mark.parametrize("old_version", [1, 2, 3, 4, 5])
def test_previous_policy_versions_are_rejected_until_explicitly_migrated(
    tmp_path: Path,
    old_version: int,
) -> None:
    documents = valid_documents()
    documents["fan-policy.yaml"]["schema_version"] = old_version
    write_documents(tmp_path, documents)

    with pytest.raises(ValidationError, match="schema_version"):
        ControlConfig.from_directory(tmp_path)


def test_v4_to_v5_migration_requires_explicit_supervisor_policy_values(tmp_path: Path) -> None:
    documents = valid_documents()
    supervisor = supervisor_config()
    documents["fan-policy.yaml"]["supervisor"] = {"period_ms": 1000, "valid_ms": 2000}
    documents["fan-policy.yaml"]["schema_version"] = 4
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="schema_version"):
        ControlConfig.from_directory(tmp_path)

    documents["fan-policy.yaml"]["supervisor"] = supervisor
    documents["fan-policy.yaml"]["schema_version"] = 9
    write_documents(tmp_path, documents)
    assert ControlConfig.from_directory(tmp_path).policy.schema_version == 9

    del documents["fan-policy.yaml"]["supervisor"]["active_policy"]
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="active_policy"):
        ControlConfig.from_directory(tmp_path)


def test_v5_to_v6_migration_requires_explicit_model_confidence_values(tmp_path: Path) -> None:
    """#85: Confidence / OOD の閾値と MEDIUM 帯は自動補完しない（決定記録 0050）。"""
    documents = valid_documents()
    del documents["fan-policy.yaml"]["model_confidence"]
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="model_confidence"):
        ControlConfig.from_directory(tmp_path)

    documents["fan-policy.yaml"]["schema_version"] = 5
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="schema_version"):
        ControlConfig.from_directory(tmp_path)


def test_model_confidence_values_are_cross_validated(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    confidence = config.policy.model_confidence
    assert confidence.high_min_confidence.value == 0.85
    assert confidence.medium_limit.limit_down.value == 0.05

    cases = [
        ("high_min_confidence", provisional(0.7), "high_min_confidence"),
        ("full_support_count", provisional(0), "full_support_count"),
        ("residual_min_samples", provisional(21), "residual_min_samples"),
        ("residual_drift_ood_ratio", provisional(1.0), "residual_drift_ood_ratio"),
        ("range_margin", provisional(-0.1), "range_margin"),
        ("cap_without_uncertainty", provisional(1.5), "cap_without_uncertainty"),
        # 証拠が無い間に HIGH（帯なし）へ届く組み合わせは拒否する
        ("cap_before_residual_evidence", provisional(0.85), "cap_before_residual_evidence"),
    ]
    for name, value, match in cases:
        documents = valid_documents()
        documents["fan-policy.yaml"]["model_confidence"][name] = value
        write_documents(tmp_path, documents)
        with pytest.raises(ValidationError, match=match):
            ControlConfig.from_directory(tmp_path)


def test_model_confidence_values_are_listed_as_provisional(tmp_path: Path) -> None:
    paths = {item.path for item in load_config(tmp_path).provisional_values()}
    assert "model_confidence.high_min_confidence" in paths
    assert "model_confidence.medium_limit.limit_down" in paths
    assert "model_confidence.residual_drift_ood_ratio" in paths


def test_mpc_optimizer_horizon_must_be_a_whole_number_of_control_steps(tmp_path: Path) -> None:
    """#86 の plan は等間隔の control step で予測時刻に対応する。端数を黙って丸めない。"""
    documents = valid_documents()
    documents["fan-policy.yaml"]["mpc"]["optimizer"]["horizon_ms"] = provisional(65_000)
    write_documents(tmp_path, documents)

    with pytest.raises(ValidationError, match="整数倍"):
        ControlConfig.from_directory(tmp_path)


def test_mpc_optimizer_zone_bounds_and_cost_scales_are_checked(tmp_path: Path) -> None:
    """探索範囲の上下が逆なもの、0 除算になる基準量は起動前に拒否する。"""
    documents = valid_documents()
    documents["fan-policy.yaml"]["mpc"]["optimizer"]["zone_bounds"]["top"] = {
        "floor": provisional(0.9),
        "ceiling": provisional(0.5),
    }
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="ceiling"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["mpc"]["optimizer"]["cost_scales"]["temperature_c"] = provisional(
        0.0
    )
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match=r"cost_scales\.temperature_c"):
        ControlConfig.from_directory(tmp_path)


def test_mpc_validity_window_cannot_be_shorter_than_the_recalculation_period(
    tmp_path: Path,
) -> None:
    """周期より短い有効期限では、健全な提案でも毎 tick 期限切れになる。"""
    documents = valid_documents()
    documents["fan-policy.yaml"]["mpc"]["valid_ms"] = 500
    write_documents(tmp_path, documents)

    with pytest.raises(ValidationError, match=r"mpc.valid_ms"):
        ControlConfig.from_directory(tmp_path)


def test_mpc_optimizer_cost_metrics_must_be_stored_and_distinct(tmp_path: Path) -> None:
    """metric 名をコードに埋めない代わりに、設定側で保存対象かどうかを検証する。"""
    documents = valid_documents()
    documents["fan-policy.yaml"]["mpc"]["optimizer"]["cost_metrics"]["cpu_temperature"] = (
        "gpu.0.core"
    )
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="別の metric"):
        ControlConfig.from_directory(tmp_path)

    documents = valid_documents()
    documents["fan-policy.yaml"]["mpc"]["optimizer"]["cost_metrics"]["gpu_temperature"] = (
        "Not.A.Metric"
    )
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError):
        ControlConfig.from_directory(tmp_path)


def test_provisional_values_list_the_new_mpc_optimizer_settings(tmp_path: Path) -> None:
    """実測前の #86 の値も、位置と根拠だけを起動ログへ出せるようにする。"""
    paths = {item.path for item in load_config(tmp_path).provisional_values()}

    assert "mpc.optimizer.horizon_ms" in paths
    assert "mpc.optimizer.step_ms" in paths
    assert "mpc.optimizer.cost_scales.temperature_c" in paths
    assert "mpc.optimizer.zone_bounds.top.ceiling" in paths
    # 値そのものは出さない（ProvisionalConfigValue は path と basis だけを持つ）。
    assert all(not hasattr(item, "value") for item in load_config(tmp_path).provisional_values())
