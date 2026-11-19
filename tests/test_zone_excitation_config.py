"""`fan-policy.yaml` v14 の `zone_excitation`（#84 / 決定記録 0185 §2.4）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.control import (
    CONTROL_CONFIG_VERSION,
    FAN_POLICY_CONFIG_VERSION,
    ControlConfig,
    ZoneExcitationMode,
)
from coldaisle.control.schema import Zone
from test_control_config import valid_documents, write_documents, zone_excitation


def load(tmp_path: Path, block: dict[str, Any] | None) -> ControlConfig:
    documents = valid_documents()
    if block is None:
        del documents["fan-policy.yaml"]["zone_excitation"]
    else:
        documents["fan-policy.yaml"]["zone_excitation"] = block
    write_documents(tmp_path, documents)
    return ControlConfig.from_directory(tmp_path)


def test_versions_were_raised() -> None:
    assert FAN_POLICY_CONFIG_VERSION == 14
    assert CONTROL_CONFIG_VERSION == 18


def test_block_is_required(tmp_path: Path) -> None:
    """v13 以前を補完しない（0185 §2.4）。黙って off と読まない。"""
    with pytest.raises(ValidationError, match="zone_excitation"):
        load(tmp_path, None)


def test_v13_is_not_read_as_v14(tmp_path: Path) -> None:
    documents = valid_documents()
    documents["fan-policy.yaml"]["schema_version"] = 13
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="schema_version"):
        ControlConfig.from_directory(tmp_path)


def test_valid_block_is_read(tmp_path: Path) -> None:
    excitation = load(tmp_path, zone_excitation("apply")).policy.zone_excitation
    assert excitation.mode is ZoneExcitationMode.APPLY
    assert excitation.zones == frozenset(Zone)
    assert excitation.amplitude.value == pytest.approx(0.10)
    assert excitation.on_ms.value == 300_000
    assert (excitation.off_min_ms.value, excitation.off_max_ms.value) == (600_000, 1_200_000)
    assert excitation.allowed_hours.value == (0, 24)
    assert excitation.seed == 1


def test_unquoted_off_is_read_as_off(tmp_path: Path) -> None:
    """YAML 1.1 の引用符の無い off（偽）だけを off と読む。"""
    block = zone_excitation()
    block["mode"] = False
    assert load(tmp_path, block).policy.zone_excitation.mode is ZoneExcitationMode.OFF


@pytest.mark.parametrize("mode", [True, "shadow", "on"])
def test_modes_that_cannot_be_decided_are_rejected(tmp_path: Path, mode: object) -> None:
    """真（on / yes）は推測しない。shadow は持たない（0185 §5 の 6）。"""
    block = zone_excitation()
    block["mode"] = mode
    with pytest.raises(ValidationError):
        load(tmp_path, block)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("zones", [], "zones"),
        ("zones", ["side"], "zones"),
        ("amplitude", {"value": 0.0, "status": "provisional"}, "amplitude"),
        ("amplitude", {"value": 0.31, "status": "provisional"}, "amplitude"),
        ("amplitude", {"value": float("nan"), "status": "provisional"}, "amplitude"),
        ("on_ms", {"value": 0, "status": "provisional"}, "on_ms"),
        ("off_min_ms", {"value": 1_300_000, "status": "provisional"}, "off_min_ms"),
        ("margin_c", {"value": 0.0, "status": "provisional"}, "margin_c"),
        ("allowed_hours", {"value": [8, 8], "status": "provisional"}, "allowed_hours"),
        ("allowed_hours", {"value": [22, 6], "status": "provisional"}, "allowed_hours"),
        ("allowed_hours", {"value": [0, 25], "status": "provisional"}, "allowed_hours"),
        ("allowed_hours", {"value": [0, 12, 24], "status": "provisional"}, "allowed_hours"),
        ("seed", -1, "seed"),
        ("seed", 1.5, "seed"),
    ],
)
def test_invalid_values_make_the_config_invalid(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    """不正な値は Control Config の不正（全 zone Max）にする（0185 §2.4）。"""
    block = zone_excitation("apply")
    block[field] = value
    with pytest.raises(ValidationError, match=message):
        load(tmp_path, block)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    block = zone_excitation()
    block["amplitude_down"] = {"value": 0.1, "status": "provisional"}
    with pytest.raises(ValidationError, match="amplitude_down"):
        load(tmp_path, block)


def test_confirmed_value_needs_a_basis(tmp_path: Path) -> None:
    block = zone_excitation()
    block["amplitude"] = {"value": 0.10, "status": "confirmed"}
    with pytest.raises(ValidationError, match="basis"):
        load(tmp_path, block)


def test_provisional_values_are_listed(tmp_path: Path) -> None:
    paths = {
        value.path
        for value in load(tmp_path, zone_excitation()).provisional_values()
        if value.source == "fan-policy.yaml"
    }
    assert {
        "zone_excitation.amplitude",
        "zone_excitation.on_ms",
        "zone_excitation.off_min_ms",
        "zone_excitation.off_max_ms",
        "zone_excitation.margin_c",
        "zone_excitation.allowed_hours",
    } <= paths


def test_block_changes_the_config_digest(tmp_path: Path) -> None:
    """揺らぎの設定は authority の照合（0090）の対象に入る（設定の hash が変わる）。"""
    (tmp_path / "off").mkdir()
    (tmp_path / "apply").mkdir()
    off = load(tmp_path / "off", zone_excitation())
    applied = load(tmp_path / "apply", zone_excitation("apply"))
    assert off.sources.policy.sha256 != applied.sources.policy.sha256
