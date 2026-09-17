"""#81 Air Balance Model を実機なしの characterization で検証する。"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.control import PerZone
from coldaisle.control.air_balance import (
    AirBalanceConfig,
    AirBalanceState,
    ConfiguredAirBalanceModel,
    ThermalInputs,
    UncalibratedAirBalanceError,
)

TEST_CONFIG_FILENAME = "air-balance.yaml"


def document() -> dict[str, Any]:
    """物理 CFM を含まない、Mock 用の未校正 EFU characterization。"""
    return {
        "schema_version": 1,
        "model_id": "mock-air-balance",
        "source": {
            "status": "uncalibrated",
            "basis": "Synthetic software test only; replace with #75 characterization.",
        },
        "flow_unit": "efu",
        "zones": {
            "front": {
                "points": [
                    {"demand": 0.0, "airflow_index": 0.0, "effective_flow": 0.0},
                    {"demand": 0.5, "airflow_index": 0.4, "effective_flow": 2.0},
                    {"demand": 1.0, "airflow_index": 1.0, "effective_flow": 5.0},
                ]
            },
            "rear": {
                "points": [
                    {"demand": 0.0, "airflow_index": 0.0, "effective_flow": 0.0},
                    {"demand": 0.5, "airflow_index": 0.45, "effective_flow": 1.35},
                    {"demand": 1.0, "airflow_index": 1.0, "effective_flow": 3.0},
                ]
            },
            "top": {
                "points": [
                    {"demand": 0.0, "airflow_index": 0.0, "effective_flow": 0.0},
                    {"demand": 0.5, "airflow_index": 0.35, "effective_flow": 1.4},
                    {"demand": 1.0, "airflow_index": 1.0, "effective_flow": 4.0},
                ]
            },
        },
        "balance": {
            "target_ratio": 0.9,
            "minimum_ratio": 0.8,
            "maximum_ratio": 1.05,
        },
        "thermal_limits": {
            "gpu_intake_c": 35.0,
            "case_delta_c": 12.0,
            "cpu_package_c": 80.0,
            "gpu_temperature_c": 78.0,
        },
    }


def write_config(directory: Path, contents: dict[str, Any] | None = None) -> Path:
    path = directory / TEST_CONFIG_FILENAME
    path.write_text(yaml.safe_dump(contents or document()), encoding="utf-8")
    return path


def model(tmp_path: Path) -> ConfiguredAirBalanceModel:
    return ConfiguredAirBalanceModel.from_file(
        write_config(tmp_path),
        allow_uncalibrated_for_testing=True,
    )


def cool() -> ThermalInputs:
    return ThermalInputs(
        gpu_intake_c=25.0,
        case_delta_c=5.0,
        cpu_package_c=55.0,
        gpu_temperature_c=60.0,
    )


def test_q_values_share_efu_scale_and_keep_uncalibrated_metadata(tmp_path: Path) -> None:
    estimate = model(tmp_path).evaluate(
        PerZone[float](front=0.5, rear=0.5, top=0.5),
        cool(),
    )

    assert estimate.q_front == pytest.approx(2.0)
    assert estimate.q_rear == pytest.approx(1.35)
    assert estimate.q_top == pytest.approx(1.4)
    assert estimate.estimated_intake == estimate.q_front
    assert estimate.estimated_exhaust == pytest.approx(2.75)
    assert estimate.balance_ratio == pytest.approx(1.375)
    assert estimate.metadata.flow_unit == "efu"
    assert estimate.metadata.source.status == "uncalibrated"
    assert len(estimate.metadata.config_sha256) == 64


def test_committed_fixture_requires_explicit_test_only_uncalibrated_opt_in() -> None:
    fixture = Path(__file__).parent / "fixtures" / "air_balance_uncalibrated.yaml"

    with pytest.raises(UncalibratedAirBalanceError, match="opt-in"):
        ConfiguredAirBalanceModel.from_file(fixture)

    estimate = ConfiguredAirBalanceModel.from_file(
        fixture,
        allow_uncalibrated_for_testing=True,
    ).evaluate(
        PerZone[float](front=0.5, rear=0.5, top=0.5),
        cool(),
    )

    assert estimate.metadata.model_id == "provisional-air-balance"
    assert estimate.metadata.source.status == "uncalibrated"
    assert "replace" in estimate.metadata.source.basis.lower()
    assert not (Path(__file__).parents[1] / "config" / TEST_CONFIG_FILENAME).exists()


@pytest.mark.parametrize(
    ("demands", "thermal", "expected"),
    [
        ((0.5, 0.4, 0.2), cool(), AirBalanceState.BALANCED),
        ((1.0, 0.2, 0.2), cool(), AirBalanceState.INTAKE_HEAVY),
        ((0.2, 0.8, 0.5), cool(), AirBalanceState.EXHAUST_HEAVY),
        (
            (0.5, 0.4, 0.2),
            ThermalInputs(
                gpu_intake_c=35.0,
                case_delta_c=5.0,
                cpu_package_c=55.0,
                gpu_temperature_c=60.0,
            ),
            AirBalanceState.THERMALLY_LIMITED,
        ),
        ((0.0, 0.5, 0.5), cool(), AirBalanceState.UNKNOWN),
    ],
)
def test_mock_demand_and_thermal_transitions(
    demands: tuple[float, float, float],
    thermal: ThermalInputs,
    expected: AirBalanceState,
    tmp_path: Path,
) -> None:
    estimate = model(tmp_path).evaluate(
        PerZone[float](front=demands[0], rear=demands[1], top=demands[2]),
        thermal,
    )

    assert estimate.state is expected
    assert (estimate.balance_ratio is None) is (expected is AirBalanceState.UNKNOWN)


@pytest.mark.parametrize(
    "thermal",
    [
        ThermalInputs(gpu_intake_c=35.0),
        ThermalInputs(case_delta_c=12.0),
        ThermalInputs(cpu_package_c=80.0),
        ThermalInputs(gpu_temperature_c=78.0),
    ],
)
def test_each_configured_thermal_indicator_can_limit_state(
    thermal: ThermalInputs,
    tmp_path: Path,
) -> None:
    estimate = model(tmp_path).evaluate(
        PerZone[float](front=0.5, rear=0.4, top=0.2),
        thermal,
    )

    assert estimate.state is AirBalanceState.THERMALLY_LIMITED
    assert len(estimate.thermal_reasons) == 1


def test_exhaust_heavy_proposes_front_makeup_toward_configured_non_unity_target(
    tmp_path: Path,
) -> None:
    candidate = PerZone[float](front=0.5, rear=0.5, top=0.5)
    proposal = model(tmp_path).coordinate(candidate, cool())

    assert proposal.before.state is AirBalanceState.EXHAUST_HEAVY
    assert proposal.requested.front > candidate.front
    assert proposal.requested.rear == candidate.rear
    assert proposal.requested.top == candidate.top
    assert proposal.projected.balance_ratio == pytest.approx(0.9)
    assert [reason.code for reason in proposal.reasons] == ["front_makeup_air"]


def test_zero_front_with_active_exhaust_still_proposes_makeup_air(tmp_path: Path) -> None:
    candidate = PerZone[float](front=0.0, rear=0.5, top=0.5)
    proposal = model(tmp_path).coordinate(candidate, cool())

    assert proposal.before.state is AirBalanceState.UNKNOWN
    assert proposal.before.balance_ratio is None
    assert proposal.requested.front > 0.0
    assert proposal.requested.rear == candidate.rear
    assert proposal.requested.top == candidate.top
    assert proposal.projected.balance_ratio == pytest.approx(0.9)
    assert proposal.reasons[0].code == "front_makeup_air"
    assert "0" in proposal.reasons[0].detail


def test_intake_heavy_with_heat_uses_rear_before_top(tmp_path: Path) -> None:
    thermal = ThermalInputs(case_delta_c=15.0)
    rear_can_cover = model(tmp_path).coordinate(
        PerZone[float](front=0.5, rear=0.1, top=0.1),
        thermal,
    )
    needs_top = model(tmp_path).coordinate(
        PerZone[float](front=1.0, rear=0.0, top=0.0),
        thermal,
    )

    assert rear_can_cover.requested.rear > 0.1
    assert rear_can_cover.requested.top == 0.1
    assert [reason.code for reason in rear_can_cover.reasons] == ["rear_thermal_exhaust"]
    assert needs_top.requested.rear == 1.0
    assert needs_top.requested.top > 0.0
    assert [reason.code for reason in needs_top.reasons] == [
        "rear_thermal_exhaust",
        "top_case_aux_exhaust",
    ]


def test_top_proposal_is_case_aux_only_and_cannot_lower_cpu_floor(tmp_path: Path) -> None:
    candidate = PerZone[float](front=1.0, rear=0.0, top=0.0)
    proposal = model(tmp_path).coordinate(candidate, ThermalInputs(cpu_package_c=85.0))
    cpu_cooling_floor = 0.85

    assert proposal.top_request_role == "case_aux_exhaust"
    assert proposal.requested.top >= candidate.top
    # CPU floor は Air Balance の入力・出力ではない。後段の Critical Safety の max が
    # case auxiliary より強い floor をそのまま保持する。
    assert max(proposal.requested.top, cpu_cooling_floor) == cpu_cooling_floor


def test_intake_heavy_without_thermal_accumulation_does_not_raise_exhaust(tmp_path: Path) -> None:
    candidate = PerZone[float](front=1.0, rear=0.1, top=0.1)
    proposal = model(tmp_path).coordinate(candidate, cool())

    assert proposal.before.state is AirBalanceState.INTAKE_HEAVY
    assert proposal.requested == candidate
    assert proposal.reasons == ()


def test_characterization_can_be_replaced_without_code_changes(tmp_path: Path) -> None:
    first = model(tmp_path).evaluate(PerZone[float](front=0.5, rear=0.5, top=0.5), cool())
    calibrated = document()
    calibrated["model_id"] = "measured-run-2026-09-18"
    calibrated["source"] = {
        "status": "calibrated",
        "basis": "Installed-system characterization dataset run-test-001.",
    }
    calibrated["zones"]["front"]["points"][1]["effective_flow"] = 2.5
    second = ConfiguredAirBalanceModel.from_file(write_config(tmp_path, calibrated)).evaluate(
        PerZone[float](front=0.5, rear=0.5, top=0.5),
        cool(),
    )

    assert second.q_front == pytest.approx(2.5)
    assert second.q_front != first.q_front
    assert second.metadata.source.status == "calibrated"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data["balance"].__setitem__("target_ratio", 1.2),
            "許容帯",
        ),
        (
            lambda data: data["zones"]["front"]["points"][1].__setitem__("airflow_index", 1.0),
            "airflow_index",
        ),
        (
            lambda data: data["zones"]["front"]["points"][1].__setitem__(
                "effective_flow", float("nan")
            ),
            "finite number",
        ),
        (
            lambda data: data["zones"]["front"]["points"][0].__setitem__("demand", 0.1),
            "demand=0.0",
        ),
        (
            lambda data: data["zones"]["front"]["points"][-1].__setitem__("airflow_index", 0.9),
            "airflow_index=0.0",
        ),
    ],
)
def test_invalid_characterization_is_rejected(
    mutate: Callable[[dict[str, Any]], object],
    message: str,
    tmp_path: Path,
) -> None:
    contents = document()
    mutate(contents)
    write_config(tmp_path, contents)

    with pytest.raises(ValidationError, match=message):
        AirBalanceConfig.from_file(tmp_path / TEST_CONFIG_FILENAME)


def imported_modules(source: str) -> set[str]:
    """Python source に書かれた絶対 import のモジュール名を返す。"""
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    return imports


def test_air_balance_has_no_hardware_or_safety_dependency() -> None:
    path = Path(__file__).parents[1] / "src" / "coldaisle" / "control" / "air_balance.py"
    imports = imported_modules(path.read_text(encoding="utf-8"))

    assert all("hardware" not in module and "safety" not in module for module in imports)
