"""Air Balance の協調の設定と純粋な Coordinator（#81 / 決定記録 0078）。

0078 §2.11 の段 2 の試験。**loop へは配線しない**ので、ここでは設定の検証と
`AirBalanceCoordinator` の計算（上げるだけ・上限・保持・失敗）だけを確かめる。
校正済みの値は試験用に `calibrated` を名乗らせた characterization で、実機の値ではない。
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.control.air_balance import (
    AirBalanceCoordination,
    AirBalanceMetadata,
    ConfiguredAirBalanceModel,
    ThermalInputs,
)
from coldaisle.control.air_balance_coordination import (
    AirBalanceCoordinationError,
    AirBalanceCoordinator,
    CoordinatorResult,
)
from coldaisle.control.config import (
    FAN_POLICY_CONFIG_VERSION,
    AirBalanceCoordinationConfig,
    AirBalanceCoordinationMode,
    ControlConfig,
)
from coldaisle.control.schema import Demand, PerZone, Zone
from test_control_config import (
    air_balance_coordination,
    calibrated_air_balance_document,
    provisional,
    valid_documents,
    write_documents,
)

HOLD_MS = 3000
"""試験の `release_hold_ms`（`air_balance_coordination()` の仮の値と同じ）。"""


def zones(front: float, rear: float, top: float) -> PerZone[Demand]:
    return PerZone[Demand](front=front, rear=rear, top=top)


def calibrated_config(
    tmp_path: Path,
    *,
    mode: str = "apply",
    max_raise: tuple[float, float, float] = (0.2, 0.2, 0.0),
    release_hold_ms: int = HOLD_MS,
) -> ControlConfig:
    documents = valid_documents()
    documents["air-balance.yaml"] = calibrated_air_balance_document()
    block = air_balance_coordination(mode)
    block["max_raise"] = {
        zone: provisional(value)
        for zone, value in zip(("front", "rear", "top"), max_raise, strict=True)
    }
    block["release_hold_ms"] = provisional(release_hold_ms)
    documents["fan-policy.yaml"]["air_balance_coordination"] = block
    write_documents(tmp_path, documents)
    return ControlConfig.from_directory(tmp_path)


def real_model(config: ControlConfig) -> ConfiguredAirBalanceModel:
    return ConfiguredAirBalanceModel(config.air_balance, config.sources.air_balance.sha256)


def coordinator(
    tmp_path: Path,
    *,
    model: Any = None,
    **kwargs: Any,
) -> AirBalanceCoordinator:
    config = calibrated_config(tmp_path, **kwargs)
    return AirBalanceCoordinator(
        settings=config.policy.air_balance_coordination,
        model=real_model(config) if model is None else model,
        fan_hardware=config.fan_hardware,
    )


class ScriptedModel:
    """`coordinate()` の requested だけを差し替える偽の model。推定は本物の model で作る。

    ``requested`` が None なら引き上げなし（requested = 渡された demand）。
    """

    def __init__(self, inner: ConfiguredAirBalanceModel) -> None:
        self._inner = inner
        self.requested: PerZone[Demand] | None = None
        self.error: Exception | None = None
        self.lower = False
        self.calls: list[dict[str, Any]] = []

    @property
    def metadata(self) -> AirBalanceMetadata:
        return self._inner.metadata

    def evaluate(self, demands: PerZone[Demand], thermal: ThermalInputs) -> Any:
        return self._inner.evaluate(demands, thermal)

    def coordinate(
        self,
        demands: PerZone[Demand],
        thermal: ThermalInputs,
        *,
        projected_top_floor: Demand | None = None,
    ) -> AirBalanceCoordination:
        self.calls.append({"demands": demands, "projected_top_floor": projected_top_floor})
        if self.error is not None:
            raise self.error
        base = self._inner.coordinate(demands, thermal, projected_top_floor=projected_top_floor)
        if self.lower:
            # AirBalanceCoordination の検証を迂回して「下げる」不具合を模す。
            lowered = zones(0.0, demands.rear, demands.top)
            return AirBalanceCoordination.model_construct(**{**dict(base), "requested": lowered})
        if self.requested is None:
            return base.model_copy(update={"requested": demands})
        requested = PerZone[Demand](
            front=max(self.requested.front, demands.front),
            rear=max(self.requested.rear, demands.rear),
            top=max(self.requested.top, demands.top),
        )
        return base.model_copy(update={"requested": requested})


def scripted(tmp_path: Path, **kwargs: Any) -> tuple[AirBalanceCoordinator, ScriptedModel]:
    config = calibrated_config(tmp_path, **kwargs)
    model = ScriptedModel(real_model(config))
    return (
        AirBalanceCoordinator(
            settings=config.policy.air_balance_coordination,
            model=model,
            fan_hardware=config.fan_hardware,
        ),
        model,
    )


NO_FLOORS = zones(0.0, 0.0, 0.0)
COOL = ThermalInputs(
    gpu_intake_c=25.0, case_delta_c=5.0, cpu_package_c=55.0, gpu_temperature_c=60.0
)
HOT = ThermalInputs(
    gpu_intake_c=40.0, case_delta_c=15.0, cpu_package_c=85.0, gpu_temperature_c=80.0
)


def run(
    unit: AirBalanceCoordinator,
    candidate: PerZone[Demand],
    now_mono_ms: int,
    *,
    thermal: ThermalInputs = COOL,
    floors: PerZone[Demand] = NO_FLOORS,
) -> CoordinatorResult:
    return unit.apply(
        candidate=candidate, thermal=thermal, projected_floors=floors, now_mono_ms=now_mono_ms
    )


# --- 設定（fan-policy.yaml v10） ---


def test_fan_policy_v10_requires_the_coordination_block_and_defaults_to_nothing(
    tmp_path: Path,
) -> None:
    """v10 は塊を必須にする。v9 を補完しない（0078 §2.4）。"""
    assert FAN_POLICY_CONFIG_VERSION == 10
    documents = valid_documents()
    del documents["fan-policy.yaml"]["air_balance_coordination"]
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="air_balance_coordination"):
        ControlConfig.from_directory(tmp_path)


def test_fan_policy_v9_is_rejected_without_filling_in_the_coordination_block(
    tmp_path: Path,
) -> None:
    documents = valid_documents()
    del documents["fan-policy.yaml"]["air_balance_coordination"]
    documents["fan-policy.yaml"]["schema_version"] = 9
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="schema_version"):
        ControlConfig.from_directory(tmp_path)


@pytest.mark.parametrize("mode", ["shadow", "apply"])
def test_shadow_or_apply_with_uncalibrated_air_balance_is_invalid(
    tmp_path: Path, mode: str
) -> None:
    """黙って off と読まない（0078 §2.4 / 所有者の判断 3）。"""
    documents = valid_documents()
    documents["fan-policy.yaml"]["air_balance_coordination"] = air_balance_coordination(mode)
    write_documents(tmp_path, documents)
    with pytest.raises(ValidationError, match="calibrated"):
        ControlConfig.from_directory(tmp_path)


def test_off_with_uncalibrated_air_balance_is_valid(tmp_path: Path) -> None:
    write_documents(tmp_path, valid_documents())
    config = ControlConfig.from_directory(tmp_path)
    assert config.policy.air_balance_coordination.mode is AirBalanceCoordinationMode.OFF
    assert config.air_balance_enabled is False


@pytest.mark.parametrize("mode", ["off", "shadow", "apply"])
def test_every_mode_is_valid_with_calibrated_air_balance(tmp_path: Path, mode: str) -> None:
    config = calibrated_config(tmp_path, mode=mode)
    assert config.policy.air_balance_coordination.mode == AirBalanceCoordinationMode(mode)


def test_unquoted_yaml_off_reads_as_off_but_true_is_rejected(tmp_path: Path) -> None:
    """YAML 1.1 の `off` は偽になる。偽だけを off と読み、真は推測しない。"""
    base = air_balance_coordination()

    def parse(mode_text: str) -> AirBalanceCoordinationConfig:
        text = yaml.safe_dump({**base, "mode": "PLACEHOLDER"}).replace("PLACEHOLDER", mode_text)
        return AirBalanceCoordinationConfig.model_validate(yaml.safe_load(text))

    assert parse("off").mode is AirBalanceCoordinationMode.OFF
    with pytest.raises(ValidationError):
        parse("on")
    with pytest.raises(ValidationError):
        parse("enabled")


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_max_raise_outside_the_unit_interval_is_rejected(value: float) -> None:
    block = air_balance_coordination()
    block["max_raise"]["front"] = provisional(value)  # type: ignore[index]
    with pytest.raises(ValidationError):
        AirBalanceCoordinationConfig.model_validate(block)


def test_negative_release_hold_is_rejected_and_zero_is_allowed() -> None:
    block = air_balance_coordination()
    block["release_hold_ms"] = provisional(-1)
    with pytest.raises(ValidationError):
        AirBalanceCoordinationConfig.model_validate(block)
    block["release_hold_ms"] = provisional(0)
    assert AirBalanceCoordinationConfig.model_validate(block).release_hold_ms.value == 0


def test_provisional_coordination_values_are_listed_at_startup(tmp_path: Path) -> None:
    config = calibrated_config(tmp_path)
    paths = {value.path for value in config.provisional_values()}
    assert {
        "air_balance_coordination.max_raise.front",
        "air_balance_coordination.max_raise.rear",
        "air_balance_coordination.max_raise.top",
        "air_balance_coordination.release_hold_ms",
    } <= paths


# --- Coordinator の作り方 ---


def test_coordinator_is_not_built_for_mode_off(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="off"):
        coordinator(tmp_path, mode="off")


def test_coordinator_refuses_an_uncalibrated_model(tmp_path: Path) -> None:
    config = calibrated_config(tmp_path)
    uncalibrated = ConfiguredAirBalanceModel.from_file(
        Path(__file__).parent / "fixtures" / "air_balance_uncalibrated.yaml",
        allow_uncalibrated_for_testing=True,
    )
    with pytest.raises(ValueError, match="未校正"):
        AirBalanceCoordinator(
            settings=config.policy.air_balance_coordination,
            model=uncalibrated,
            fan_hardware=config.fan_hardware,
        )


# --- 上げるだけ・上限 ---


GRID = (0.0, 0.25, 0.5, 0.75, 1.0)


@pytest.mark.parametrize("mode", ["shadow", "apply"])
def test_never_lowers_and_never_exceeds_max_raise_on_a_demand_grid(
    tmp_path: Path, mode: str
) -> None:
    """3 zone の格子・熱の有無・Top の下限の見込みで、下げず上限を超えない（0078 §2.10）。"""
    unit = coordinator(tmp_path, mode=mode, max_raise=(0.2, 0.15, 0.1))
    limits = unit.max_raise()
    now = 0
    for front, rear, top in itertools.product(GRID, repeat=3):
        for thermal, top_floor in itertools.product((COOL, HOT), (0.0, 1.0)):
            candidate = zones(front, rear, top)
            # 保持が前の格子点の幅を持ち越さないよう、毎回解く。
            unit.release()
            now += 1
            result = run(unit, candidate, now, thermal=thermal, floors=zones(0.0, 0.0, top_floor))
            for zone in Zone:
                c = candidate.get(zone)
                for values in (result.output, result.counterfactual_output):
                    assert values.get(zone) >= c
                    assert values.get(zone) <= min(1.0, c + limits.get(zone))
            if mode == "shadow":
                assert result.output == candidate


def test_exhaust_heavy_tick_raises_front_within_max_raise(tmp_path: Path) -> None:
    """Exhaust 過多で Front make-up air を上げる。上限で切り、切ったことを記録する。"""
    unit = coordinator(tmp_path, max_raise=(0.2, 0.2, 0.0))
    candidate = zones(0.3, 0.5, 0.5)
    result = run(unit, candidate, 0)

    assert result.status == "applied"
    assert result.proposed.front > candidate.front + 0.2
    assert result.output.front == pytest.approx(0.5)
    assert result.bounded_by_max_raise.front is True
    assert (result.output.rear, result.output.top) == (candidate.rear, candidate.top)
    assert [reason.code for reason in result.coordination.reasons] == ["front_makeup_air"]
    assert result.max_raise == zones(0.2, 0.2, 0.0)


def test_zero_max_raise_keeps_the_zone_at_raw_baseline(tmp_path: Path) -> None:
    """Top の `max_raise` 0 は Top を協調で動かさない（最初の apply。所有者の判断 9）。"""
    unit, model = scripted(tmp_path, max_raise=(0.2, 0.2, 0.0))
    model.requested = zones(0.4, 0.4, 0.9)
    result = run(unit, zones(0.4, 0.4, 0.4), 0)
    assert result.output.top == 0.4
    assert result.bounded_by_max_raise.top is True
    assert result.proposed.top == 0.9


def test_balanced_tick_is_not_needed(tmp_path: Path) -> None:
    unit = coordinator(tmp_path)
    candidate = zones(0.6, 0.5, 0.3)  # 試験の曲線で比が許容帯の中
    result = run(unit, candidate, 0)
    assert result.coordination.before.state.value == "balanced"
    assert result.status == "not_needed"
    assert result.output == candidate
    assert result.counterfactual_output == candidate


def test_coordinate_sees_stable_demands_but_untouched_zones_keep_the_raw_value(
    tmp_path: Path,
) -> None:
    """評価は `stable_demands()` の値で行い、引き上げの無い zone へ写さない（0078 §2.4）。"""
    unit, model = scripted(tmp_path)
    candidate = zones(0.1, 0.1, 0.1)  # minimum_stable_demand（0.3）未満
    model.requested = zones(0.3, 0.3, 0.3)  # stable と同じ＝引き上げなし
    result = run(unit, candidate, 0)

    assert model.calls[-1]["demands"] == zones(0.3, 0.3, 0.3)
    assert result.stable_candidate == zones(0.3, 0.3, 0.3)
    assert result.output == candidate
    assert result.proposed == candidate
    assert result.status == "not_needed"


def test_projected_top_floor_is_passed_and_floors_are_recorded(tmp_path: Path) -> None:
    unit, model = scripted(tmp_path)
    floors = zones(0.6, 0.4, 1.0)
    result = run(unit, zones(0.5, 0.5, 0.5), 0, floors=floors)
    assert model.calls[-1]["projected_top_floor"] == 1.0
    assert result.projected_floors == floors


# --- 保持 ---


def test_release_hold_keeps_the_largest_recent_raise_then_lets_go(tmp_path: Path) -> None:
    """幅が消えた・縮んだ後も `release_hold_ms` の間だけ直近の最大の幅を保つ（0078 §2.4）。"""
    unit, model = scripted(tmp_path, max_raise=(0.2, 0.2, 0.0))
    candidate = zones(0.4, 0.4, 0.4)

    model.requested = zones(0.9, 0.4, 0.4)
    first = run(unit, candidate, 0)
    assert first.output.front == pytest.approx(0.6)
    assert first.held.front is False

    model.requested = zones(0.5, 0.4, 0.4)  # 縮んだ
    shrunk = run(unit, candidate, 1000)
    assert shrunk.output.front == pytest.approx(0.6)
    assert shrunk.held.front is True
    assert shrunk.status == "applied"

    model.requested = None  # 消えた
    model_free = zones(0.4, 0.4, 0.4)
    gone = run(unit, model_free, HOLD_MS - 1)
    assert gone.output.front == pytest.approx(0.6)
    assert gone.held.front is True
    # 保持中も上限を守る
    assert gone.output.front - model_free.front <= 0.2 + 1e-12

    # 最初の幅（t=0）が窓を出る。t=1000 の 0.1 の幅だけが残る。
    later = run(unit, model_free, HOLD_MS)
    assert later.output.front == pytest.approx(0.5)
    released = run(unit, model_free, 1000 + HOLD_MS)
    assert released.output == model_free
    assert released.status == "not_needed"


def test_hold_raises_with_the_raw_baseline_and_never_holds_its_drop(tmp_path: Path) -> None:
    """保持するのは幅で値ではない。raw baseline 自身の下げは保持しない。"""
    unit, model = scripted(tmp_path, max_raise=(0.2, 0.2, 0.0))
    model.requested = zones(1.0, 0.0, 0.0)
    run(unit, zones(0.7, 0.4, 0.4), 0)
    model.requested = None
    result = run(unit, zones(0.4, 0.4, 0.4), 1000)
    assert result.output.front == pytest.approx(0.6)


def test_release_clears_the_hold_immediately(tmp_path: Path) -> None:
    unit, model = scripted(tmp_path)
    model.requested = zones(0.9, 0.4, 0.4)
    run(unit, zones(0.4, 0.4, 0.4), 0)
    unit.release()
    model.requested = None
    result = run(unit, zones(0.4, 0.4, 0.4), 1)
    assert result.output == zones(0.4, 0.4, 0.4)


def test_zero_release_hold_does_not_hold(tmp_path: Path) -> None:
    unit, model = scripted(tmp_path, release_hold_ms=0)
    model.requested = zones(0.9, 0.4, 0.4)
    run(unit, zones(0.4, 0.4, 0.4), 0)
    model.requested = None
    result = run(unit, zones(0.4, 0.4, 0.4), 0)
    assert result.output == zones(0.4, 0.4, 0.4)


def test_shadow_counterfactual_matches_apply_output_tick_by_tick(tmp_path: Path) -> None:
    """同じ入力列で shadow の `counterfactual_output` が apply の `output` と一致する。"""
    (tmp_path / "s").mkdir()
    (tmp_path / "a").mkdir()
    shadow, shadow_model = scripted(tmp_path / "s", mode="shadow")
    apply, apply_model = scripted(tmp_path / "a", mode="apply")
    script = [
        (0, zones(0.9, 0.4, 0.4)),
        (1000, zones(0.5, 0.4, 0.4)),
        (2000, None),
        (5000, None),
        (6000, zones(0.4, 0.9, 0.4)),
    ]
    for now, requested in script:
        shadow_model.requested = requested
        apply_model.requested = requested
        candidate = zones(0.4, 0.4, 0.4)
        s = run(shadow, candidate, now)
        a = run(apply, candidate, now)
        assert s.status == "shadow"
        assert s.output == candidate
        assert s.counterfactual_output == a.output
        assert s.held == a.held


# --- 失敗 ---


def test_model_exception_propagates_and_releases_the_hold(tmp_path: Path) -> None:
    """握りつぶさない。保持を解いてから投げ直す（`failed` への記録は呼び出し側。0078 §2.6）。"""
    unit, model = scripted(tmp_path)
    model.requested = zones(0.9, 0.4, 0.4)
    run(unit, zones(0.4, 0.4, 0.4), 0)

    model.error = RuntimeError("壊れた model")
    with pytest.raises(RuntimeError, match="壊れた model"):
        run(unit, zones(0.4, 0.4, 0.4), 1)

    model.error = None
    model.requested = None
    assert run(unit, zones(0.4, 0.4, 0.4), 2).output == zones(0.4, 0.4, 0.4)


def test_a_lowering_proposal_is_an_error_and_releases_the_hold(tmp_path: Path) -> None:
    unit, model = scripted(tmp_path)
    model.requested = zones(0.9, 0.4, 0.4)
    run(unit, zones(0.4, 0.4, 0.4), 0)

    model.requested = None
    model.lower = True
    with pytest.raises(AirBalanceCoordinationError, match="下げた"):
        run(unit, zones(0.4, 0.4, 0.4), 1)

    model.lower = False
    assert run(unit, zones(0.4, 0.4, 0.4), 2).output == zones(0.4, 0.4, 0.4)


def test_a_backwards_monotonic_clock_is_an_error(tmp_path: Path) -> None:
    unit = coordinator(tmp_path)
    run(unit, zones(0.4, 0.4, 0.4), 1000)
    with pytest.raises(AirBalanceCoordinationError, match="単調時計"):
        run(unit, zones(0.4, 0.4, 0.4), 999)


# --- 結果の不変条件 ---


def test_result_rejects_values_that_lower_or_exceed_max_raise(tmp_path: Path) -> None:
    unit, model = scripted(tmp_path)
    model.requested = zones(0.9, 0.4, 0.4)
    result = run(unit, zones(0.4, 0.4, 0.4), 0)
    dumped = dict(result)

    for update, message in (
        ({"output": zones(0.3, 0.4, 0.4)}, "下回る"),
        ({"output": zones(0.7, 0.4, 0.4)}, "max_raise"),
        ({"counterfactual_output": zones(0.4, 0.4, 0.4)}, "一致"),
        ({"status": "not_needed"}, "status"),
    ):
        with pytest.raises(ValidationError, match=message):
            CoordinatorResult.model_validate({**dumped, **update})


def test_shadow_result_must_pass_the_raw_baseline(tmp_path: Path) -> None:
    unit, model = scripted(tmp_path, mode="shadow")
    model.requested = zones(0.9, 0.4, 0.4)
    result = run(unit, zones(0.4, 0.4, 0.4), 0)
    with pytest.raises(ValidationError, match="raw baseline"):
        CoordinatorResult.model_validate({**dict(result), "output": result.counterfactual_output})
