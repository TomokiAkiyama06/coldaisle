"""Zone 別 Acoustic Cost Model (#94) の純粋関数テスト。"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.control import PerZone
from coldaisle.control.acoustic import (
    ACOUSTIC_CONFIG_FILENAME,
    ConfiguredAcousticCostModel,
    DisabledAcousticCostModel,
    load_acoustic_model,
)


def document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model_id": "initial-zone-penalty",
        "source": {
            "kind": "approximate",
            "basis": "No SPL measurements; configurable initial approximation (#94).",
        },
        "zones": {
            "front": {
                "points": [
                    {"demand": 0.0, "acoustic_cost": 0.0},
                    {"demand": 0.5, "acoustic_cost": 0.2},
                    {"demand": 1.0, "acoustic_cost": 0.8},
                ]
            },
            "rear": {
                "points": [
                    {"demand": 0.0, "acoustic_cost": 0.0},
                    {"demand": 0.5, "acoustic_cost": 0.4},
                    {"demand": 1.0, "acoustic_cost": 0.6},
                ]
            },
            "top": {
                "points": [
                    {"demand": 0.0, "acoustic_cost": 0.0},
                    {"demand": 0.5, "acoustic_cost": 0.1},
                    {"demand": 1.0, "acoustic_cost": 1.0},
                ]
            },
        },
    }


def write_config(directory: Path, contents: dict[str, Any] | None = None) -> Path:
    path = directory / ACOUSTIC_CONFIG_FILENAME
    path.write_text(yaml.safe_dump(contents or document()), encoding="utf-8")
    return path


def test_zone_curves_produce_distinct_nonlinear_unitless_costs(tmp_path: Path) -> None:
    estimate = ConfiguredAcousticCostModel.from_file(write_config(tmp_path)).estimate(
        PerZone[float](front=0.75, rear=0.75, top=0.75)
    )

    assert estimate.zone_costs.front == pytest.approx(0.5)
    assert estimate.zone_costs.rear == pytest.approx(0.5)
    assert estimate.zone_costs.top == pytest.approx(0.55)
    assert estimate.acoustic_cost == pytest.approx(1.55)
    assert estimate.metadata.source.kind == "approximate"
    assert "dba" not in estimate.model_dump_json().lower()


def test_cost_metadata_keeps_source_and_configuration_hash(tmp_path: Path) -> None:
    write_config(tmp_path)
    estimate = load_acoustic_model(tmp_path).estimate(PerZone[float](front=0.0, rear=0.0, top=0.0))

    assert estimate.metadata.model_id == "initial-zone-penalty"
    assert len(estimate.metadata.config_sha256) == 64
    assert estimate.metadata.source.basis.startswith("No SPL")


class PairInteraction:
    """将来の同時運転モデルを表すテスト用の読み取り専用 interaction。"""

    name = "front-top-interaction"

    def cost_for(self, demands: PerZone[float]) -> float:
        return 0.25 if demands.front > 0.0 and demands.top > 0.0 else 0.0


def test_interactions_are_optional_and_recorded_in_metadata(tmp_path: Path) -> None:
    model = ConfiguredAcousticCostModel.from_file(
        write_config(tmp_path), interactions=(PairInteraction(),)
    )
    estimate = model.estimate(PerZone[float](front=1.0, rear=0.0, top=1.0))

    assert estimate.interaction_cost == 0.25
    assert estimate.acoustic_cost == pytest.approx(2.05)
    assert estimate.metadata.interaction_names == ("front-top-interaction",)


def test_disabled_model_keeps_acoustic_optimization_optional() -> None:
    assert (
        DisabledAcousticCostModel().estimate(PerZone[float](front=1.0, rear=1.0, top=1.0)) is None
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["zones"]["front"]["points"].pop(), "demand=1.0"),
        (
            lambda data: data["zones"]["front"]["points"].__setitem__(
                1, {"demand": 0.5, "acoustic_cost": -0.1}
            ),
            "greater than or equal",
        ),
        (
            lambda data: data["zones"]["front"]["points"].__setitem__(
                2, {"demand": 0.4, "acoustic_cost": 0.9}
            ),
            "単調増加",
        ),
    ],
)
def test_invalid_curve_is_rejected(
    mutate: Callable[[dict[str, Any]], object], message: str, tmp_path: Path
) -> None:
    contents = document()
    mutate(contents)
    write_config(tmp_path, contents)

    with pytest.raises(ValidationError, match=message):
        load_acoustic_model(tmp_path)


def test_measured_source_is_backward_compatible_when_evidence_is_present(tmp_path: Path) -> None:
    contents = document()
    contents["source"] = {
        "kind": "measured",
        "basis": "measurement run 2026-09-17, calibrated microphone record",
    }
    write_config(tmp_path, contents)

    assert (
        load_acoustic_model(tmp_path)
        .estimate(PerZone[float](front=0.0, rear=0.0, top=0.0))
        .metadata.source.kind
        == "measured"
    )


def test_no_control_path_imports_acoustic_hardware_or_safety() -> None:
    source = Path(__file__).parents[1] / "src" / "coldaisle" / "control" / "acoustic.py"
    imports = {
        node.module
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert all("hardware" not in module and "safety" not in module for module in imports)
