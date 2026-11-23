"""揺らぎを control loop に組み込んだ形と、ControlTick v18 の検査（#84 / 決定記録 0185 / 0186）。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.control import ControlConfig
from coldaisle.control.schema import (
    AuthorityRecord,
    AuthorityStage,
    ControllerKind,
    ControlTick,
    OperatingMode,
    Zone,
    ZoneExcitationMode,
    ZoneExcitationRecord,
    ZoneExcitationSkipReason,
    ZoneExcitationState,
)
from coldaisle.metrics import MetricCatalog
from test_control_config import zone_excitation
from test_control_loop import METRICS_PATH, Harness, control_config, manual


@pytest.fixture(scope="module")
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(METRICS_PATH)


def excitation_config(mode: str = "apply", **overrides: Any) -> ControlConfig:
    """区間 3 s・間 2 s の揺らぎ（値は試験用の仮の値）。"""
    block = zone_excitation(mode) | {
        "on_ms": {"value": 3000, "status": "provisional"},
        "off_min_ms": {"value": 2000, "status": "provisional"},
        "off_max_ms": {"value": 2000, "status": "provisional"},
    }
    return control_config(policy={"zone_excitation": block | overrides})


def ticks_after_settle(harness: Harness, count: int) -> list[ControlTick]:
    harness.settle()
    return [harness.tick().tick for _ in range(count)]


def record(tick: ControlTick) -> ZoneExcitationRecord:
    assert tick.zone_excitation is not None
    return tick.zone_excitation


# ---------------------------------------------------------------- loop


def test_off_records_off_every_tick_and_does_not_change_requested(catalog: MetricCatalog) -> None:
    harness = Harness(catalog, config=excitation_config("off"))
    for tick in ticks_after_settle(harness, 10):
        assert record(tick).state is ZoneExcitationState.OFF
        assert record(tick).mode is ZoneExcitationMode.OFF


def test_apply_raises_one_zone_after_the_gate_chose_the_baseline(catalog: MetricCatalog) -> None:
    """Gate が Baseline を選んだ tick の requested にだけ足す（0186 §2.1）。"""
    harness = Harness(catalog, config=excitation_config())
    ticks = ticks_after_settle(harness, 20)
    on = [tick for tick in ticks if record(tick).state is ZoneExcitationState.ON]
    assert on, "区間が始まる"
    for tick in on:
        excitation = record(tick)
        assert tick.state.active_controller is ControllerKind.FALLBACK
        assert excitation.zone is not None and excitation.before is not None
        zone_record = tick.zones.get(excitation.zone)
        assert zone_record.demand.requested == excitation.after
        assert excitation.after == pytest.approx(min(1.0, excitation.before + 0.10))


def test_the_shadow_and_gate_baseline_do_not_include_the_raise(catalog: MetricCatalog) -> None:
    """Gate と Shadow の記録が使う Baseline は揺らぎを含まない（0186 §2.1）。"""
    harness = Harness(catalog, config=excitation_config())
    ticks = ticks_after_settle(harness, 20)
    on = [tick for tick in ticks if record(tick).state is ZoneExcitationState.ON]
    assert on
    for tick in on:
        if tick.shadow is None:
            continue
        excitation = record(tick)
        assert excitation.zone is not None
        fallback = [
            item
            for item in tick.shadow.counterfactuals
            if item.controller is ControllerKind.FALLBACK
        ]
        for item in fallback:
            assert item.requested.get(excitation.zone).demand == excitation.before


def test_manual_mode_aborts_the_episode(catalog: MetricCatalog) -> None:
    harness = Harness(catalog, config=excitation_config())
    harness.settle()
    states: list[ZoneExcitationState] = []
    for _ in range(10):
        tick = harness.tick().tick
        states.append(record(tick).state)
        if record(tick).state is ZoneExcitationState.ON:
            break
    assert states[-1] is ZoneExcitationState.ON
    harness.mode.command = manual(0.5, 0.5, 0.5)
    tick = harness.tick().tick
    assert tick.state.operating_mode is OperatingMode.MANUAL
    assert record(tick).state is ZoneExcitationState.SKIPPED
    assert record(tick).skip_reason is ZoneExcitationSkipReason.NOT_AUTO
    for zone in Zone:
        assert tick.zones.get(zone).demand.requested == 0.5


def test_a_hot_cpu_skips_the_episode(catalog: MetricCatalog) -> None:
    harness = Harness(catalog, config=excitation_config())
    harness.settle()
    harness.telemetry.values["cpu.package"] = 80.0
    records = [record(harness.tick().tick) for _ in range(20)]
    assert all(item.state is not ZoneExcitationState.ON for item in records)
    # 急に上げた直後は Reactive Guard が先に効く（安全側）。Guard が外れた後は温度の条件で止まる
    reasons = {item.skip_reason for item in records} - {None}
    assert ZoneExcitationSkipReason.CPU_TEMPERATURE in reasons
    assert reasons <= {
        ZoneExcitationSkipReason.CPU_TEMPERATURE,
        ZoneExcitationSkipReason.GUARD_ACTIVE,
    }


def test_the_trace_round_trips_through_json(catalog: MetricCatalog) -> None:
    harness = Harness(catalog, config=excitation_config())
    for tick in ticks_after_settle(harness, 8):
        restored = ControlTick.model_validate_json(tick.model_dump_json())
        assert restored == tick
        assert restored.schema_version == 18
        assert restored.authority is not None and restored.authority.schema_version == 2


# ---------------------------------------------------------------- ControlTick v18 の検査


def _on_tick(catalog: MetricCatalog) -> dict[str, Any]:
    harness = Harness(catalog, config=excitation_config())
    for tick in ticks_after_settle(harness, 20):
        if record(tick).state is ZoneExcitationState.ON:
            document: dict[str, Any] = json.loads(tick.model_dump_json())
            return document
    raise AssertionError("区間が始まらなかった")


def test_a_v18_tick_needs_the_excitation_record(catalog: MetricCatalog) -> None:
    document = _on_tick(catalog)
    del document["zone_excitation"]
    with pytest.raises(ValidationError, match="zone_excitation が要る"):
        ControlTick.model_validate_json(json.dumps(document))


def test_an_older_tick_cannot_carry_the_excitation_record(catalog: MetricCatalog) -> None:
    document = _on_tick(catalog)
    document["schema_version"] = 17
    document["authority"]["schema_version"] = 1
    for key in ("artifact_ceiling", "config_binding_ceiling"):
        document["authority"].pop(key, None)
    with pytest.raises(ValidationError, match="schema version 18"):
        ControlTick.model_validate_json(json.dumps(document))


def test_on_must_match_the_requested_of_the_zone(catalog: MetricCatalog) -> None:
    document = _on_tick(catalog)
    zone = document["zone_excitation"]["zone"]
    document["zones"][zone]["demand"]["requested"] = document["zone_excitation"]["before"]
    with pytest.raises(ValidationError):
        ControlTick.model_validate_json(json.dumps(document))


def test_on_is_not_recorded_on_a_manual_tick(catalog: MetricCatalog) -> None:
    document = _on_tick(catalog)
    document["state"]["operating_mode"] = "manual"
    with pytest.raises(ValidationError):
        ControlTick.model_validate_json(json.dumps(document))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"mode": "off", "state": "idle"}, "mode: off"),
        ({"mode": "apply", "state": "off"}, "mode: off"),
        ({"mode": "apply", "state": "on", "episode": 0, "zone": "front"}, "before と after"),
        ({"mode": "apply", "state": "idle", "episode": 0, "zone": "front"}, "episode と zone"),
        ({"mode": "apply", "state": "skipped", "episode": 0, "zone": "top"}, "skip_reason"),
        (
            {
                "mode": "apply",
                "state": "on",
                "episode": 0,
                "zone": "rear",
                "before": 0.6,
                "after": 0.5,
            },
            "下げない",
        ),
    ],
)
def test_the_record_fields_match_the_state(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ZoneExcitationRecord.model_validate_json(json.dumps({"schema_version": 1, **fields}))


# ---------------------------------------------------------------- AuthorityRecord v2


def test_authority_v2_records_the_binding_ceilings() -> None:
    record = AuthorityRecord(
        entry="journal",
        journal_stage=AuthorityStage.FULL,
        journal_revision=3,
        config_ceiling=AuthorityStage.FULL,
        artifact_ceiling=AuthorityStage.SHADOW,
    )
    assert record.schema_version == 2
    assert record.ceiling() is AuthorityStage.SHADOW
    restored = AuthorityRecord.model_validate_json(record.model_dump_json())
    assert restored == record
    assert "config_binding_ceiling" not in json.loads(record.model_dump_json())


def test_authority_v1_cannot_carry_the_binding_ceilings() -> None:
    with pytest.raises(ValidationError, match="schema version 2"):
        AuthorityRecord(
            schema_version=1,
            entry="journal",
            journal_stage=AuthorityStage.FULL,
            journal_revision=3,
            config_ceiling=AuthorityStage.FULL,
            config_binding_ceiling=AuthorityStage.SHADOW,
        )


def test_a_full_binding_ceiling_is_written_as_none() -> None:
    with pytest.raises(ValidationError, match="None"):
        AuthorityRecord(
            entry="journal",
            journal_stage=AuthorityStage.FULL,
            journal_revision=3,
            config_ceiling=AuthorityStage.FULL,
            artifact_ceiling=AuthorityStage.FULL,
        )


def test_a_static_entry_has_no_binding_ceiling() -> None:
    with pytest.raises(ValidationError, match="照合の上限を持たない"):
        AuthorityRecord(
            entry="static",
            config_ceiling=AuthorityStage.FULL,
            artifact_ceiling=AuthorityStage.SHADOW,
        )


def test_a_v18_tick_needs_authority_v2(catalog: MetricCatalog) -> None:
    document = _on_tick(catalog)
    document["authority"]["schema_version"] = 1
    with pytest.raises(ValidationError, match="authority は v2"):
        ControlTick.model_validate_json(json.dumps(document))
