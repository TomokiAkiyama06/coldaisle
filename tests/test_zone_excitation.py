"""揺らぎの部品 ``ZoneExcitation``（#84 / 決定記録 0185 §2.2 / §2.3・0186 §2.2）。"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest

from coldaisle.control.config import ZoneExcitationConfig
from coldaisle.control.schema import (
    PerZone,
    Reason,
    Zone,
    ZoneExcitationMode,
    ZoneExcitationSkipReason,
    ZoneExcitationState,
    ZoneRequest,
)
from coldaisle.control.zone_excitation import (
    ZoneExcitation,
    ZoneExcitationResult,
    ZoneExcitationTemperatures,
)
from test_control_config import zone_excitation

RUN_ID = "0123456789abcdef" * 2
COOL = ZoneExcitationTemperatures(cpu_c=50.0, gpu_c=55.0)
TS_NOON_UTC = 1_794_830_400_000  # 2026-11-16 12:00:00 UTC
TICK_MS = 1000


def settings(mode: str = "apply", **overrides: Any) -> ZoneExcitationConfig:
    """試験の揺らぎの設定。区間は 3 s、間は 2 s ちょうど（値は試験用の仮の値）。"""
    block = zone_excitation(mode) | {
        "on_ms": {"value": 3000, "status": "provisional"},
        "off_min_ms": {"value": 2000, "status": "provisional"},
        "off_max_ms": {"value": 2000, "status": "provisional"},
    }
    return ZoneExcitationConfig.model_validate(block | overrides)


def baseline(front: float = 0.5, rear: float = 0.5, top: float = 0.5) -> PerZone[ZoneRequest]:
    reason = Reason(code="fallback_baseline", detail="")
    return PerZone[ZoneRequest](
        front=ZoneRequest(demand=front, reason=reason),
        rear=ZoneRequest(demand=rear, reason=reason),
        top=ZoneRequest(demand=top, reason=reason),
    )


class Driver:
    """1 s ごとに ``step()`` を呼ぶ足場。"""

    def __init__(self, config: ZoneExcitationConfig, *, run_id: str = RUN_ID) -> None:
        self.excitation = ZoneExcitation(config, run_id=run_id, local_timezone=UTC)
        self.now_mono_ms = 0
        self.ts_ms = TS_NOON_UTC

    def step(
        self,
        requested: PerZone[ZoneRequest] | None = None,
        *,
        blocked: ZoneExcitationSkipReason | None = None,
        temperatures: ZoneExcitationTemperatures = COOL,
    ) -> ZoneExcitationResult:
        result = self.excitation.step(
            now_mono_ms=self.now_mono_ms,
            ts_ms=self.ts_ms,
            requested=requested or baseline(),
            blocked=blocked,
            temperatures=temperatures,
        )
        self.now_mono_ms += TICK_MS
        self.ts_ms += TICK_MS
        return result

    def states(self, ticks: int, **kwargs: Any) -> list[ZoneExcitationState]:
        return [self.step(**kwargs).record.state for _ in range(ticks)]


def test_off_returns_the_requested_unchanged_and_records_off() -> None:
    driver = Driver(settings("off"))
    requested = baseline()
    for _ in range(10):
        result = driver.step(requested)
        assert result.requested is requested
        assert result.record.mode is ZoneExcitationMode.OFF
        assert result.record.state is ZoneExcitationState.OFF


def test_the_schedule_starts_idle_then_alternates_on_and_off() -> None:
    """off 2 s → on 3 s → off 2 s → …（0185 §2.2）。"""
    driver = Driver(settings())
    assert driver.states(10) == [
        ZoneExcitationState.IDLE,
        ZoneExcitationState.IDLE,
        ZoneExcitationState.ON,
        ZoneExcitationState.ON,
        ZoneExcitationState.ON,
        ZoneExcitationState.IDLE,
        ZoneExcitationState.IDLE,
        ZoneExcitationState.ON,
        ZoneExcitationState.ON,
        ZoneExcitationState.ON,
    ]


def test_on_raises_only_the_chosen_zone_by_the_amplitude() -> None:
    driver = Driver(settings())
    requested = baseline(0.4, 0.5, 0.6)
    results = [driver.step(requested) for _ in range(5)]
    on = [result for result in results if result.record.state is ZoneExcitationState.ON]
    assert len(on) == 3
    zones = {result.record.zone for result in on}
    assert len(zones) == 1, "1 つの区間の中で zone は変わらない"
    for result in on:
        zone = result.record.zone
        assert zone is not None
        before = requested.get(zone).demand
        assert result.record.before == before
        assert result.record.after == pytest.approx(before + 0.10)
        assert result.requested.get(zone).demand == result.record.after
        assert result.requested.get(zone).reason == requested.get(zone).reason
        for other in Zone:
            if other is not zone:
                assert result.requested.get(other) == requested.get(other)


def test_the_episode_number_increases() -> None:
    driver = Driver(settings())
    episodes = [driver.step().record.episode for _ in range(15)]
    assert [episode for episode in episodes if episode is not None] == [0, 0, 0, 1, 1, 1, 2, 2, 2]


def test_the_raise_is_clipped_at_one() -> None:
    driver = Driver(settings())
    requested = baseline(0.95, 0.95, 0.95)
    on = [result for result in (driver.step(requested) for _ in range(5)) if result.record.after]
    assert on
    for result in on:
        assert result.record.before == 0.95
        assert result.record.after == 1.0


def test_a_zone_already_at_one_is_not_lowered() -> None:
    driver = Driver(settings())
    requested = baseline(1.0, 1.0, 1.0)
    for _ in range(6):
        result = driver.step(requested)
        for zone in Zone:
            assert result.requested.get(zone).demand == 1.0


def test_the_same_seed_and_run_repeat_the_same_schedule() -> None:
    def zones(run_id: str, seed: int) -> list[Zone | None]:
        driver = Driver(settings(seed=seed), run_id=run_id)
        return [driver.step().record.zone for _ in range(200)]

    assert zones(RUN_ID, 1) == zones(RUN_ID, 1)
    assert zones(RUN_ID, 1) != zones("f" * 32, 1)
    assert zones(RUN_ID, 1) != zones(RUN_ID, 2)


def test_every_configured_zone_is_chosen_and_others_never() -> None:
    driver = Driver(settings(zones=["front", "top"]))
    chosen = {driver.step().record.zone for _ in range(400)} - {None}
    assert chosen == {Zone.FRONT, Zone.TOP}


def test_off_lengths_stay_within_the_configured_range() -> None:
    config = settings(
        off_min_ms={"value": 2000, "status": "provisional"},
        off_max_ms={"value": 5000, "status": "provisional"},
    )
    driver = Driver(config)
    states = driver.states(500)
    idle_runs: list[int] = []
    run = 0
    for state in states:
        if state is ZoneExcitationState.IDLE:
            run += 1
        elif run:
            idle_runs.append(run)
            run = 0
    # 1 tick 1 s。間の idle の tick の数は off_ms / 1 s（2〜5）か、それより 1 つ多い
    assert idle_runs and min(idle_runs) >= 2 and max(idle_runs) <= 6
    assert len(set(idle_runs)) > 1, "乱数で長さが変わる"


@pytest.mark.parametrize("reason", list(ZoneExcitationSkipReason))
def test_a_blocked_tick_aborts_the_episode_and_it_does_not_resume(
    reason: ZoneExcitationSkipReason,
) -> None:
    """条件が外れた tick で区間を打ち切る。条件が戻っても再開しない（0185 §2.2）。"""
    driver = Driver(settings())
    driver.states(3)  # idle, idle, on
    skipped = driver.step(blocked=reason)
    assert skipped.record.state is ZoneExcitationState.SKIPPED
    assert skipped.record.skip_reason is reason
    assert skipped.record.episode == 0
    assert skipped.requested == baseline()
    # 打ち切った tick から off の区間（2 s）を数え直す（打ち切った tick を含めて 2 tick）
    assert driver.states(2) == [ZoneExcitationState.IDLE, ZoneExcitationState.ON]
    assert driver.step().record.episode == 1


def test_a_blocked_off_period_does_not_record_skipped() -> None:
    """区間の外で条件が外れても何もしない（打ち切る区間が無い）。"""
    driver = Driver(settings())
    assert driver.step(blocked=ZoneExcitationSkipReason.NOT_AUTO).record.state is (
        ZoneExcitationState.IDLE
    )


def test_an_episode_due_while_blocked_is_skipped_at_its_start() -> None:
    driver = Driver(settings())
    driver.states(2)
    result = driver.step(blocked=ZoneExcitationSkipReason.GUARD_ACTIVE)
    assert result.record.state is ZoneExcitationState.SKIPPED
    assert result.record.zone is not None


@pytest.mark.parametrize(
    ("temperatures", "reason"),
    [
        (ZoneExcitationTemperatures(cpu_c=None, gpu_c=55.0), "temperature_unavailable"),
        (ZoneExcitationTemperatures(cpu_c=50.0, gpu_c=None), "temperature_unavailable"),
        (ZoneExcitationTemperatures(cpu_c=80.0, gpu_c=55.0), "cpu_temperature"),
        (ZoneExcitationTemperatures(cpu_c=50.0, gpu_c=77.0), "gpu_temperature"),
    ],
)
def test_hot_or_unknown_temperatures_block_the_raise(
    temperatures: ZoneExcitationTemperatures, reason: str
) -> None:
    """上限は「より低いときだけ」。未知を低いと読まない（0186 §2.2 / ルール 4）。"""
    driver = Driver(settings())
    driver.states(2)
    result = driver.step(temperatures=temperatures)
    assert result.record.state is ZoneExcitationState.SKIPPED
    assert result.record.skip_reason is ZoneExcitationSkipReason(reason)


def test_just_below_the_limits_is_allowed() -> None:
    driver = Driver(settings())
    driver.states(2)
    result = driver.step(temperatures=ZoneExcitationTemperatures(cpu_c=79.9, gpu_c=76.9))
    assert result.record.state is ZoneExcitationState.ON


def test_outside_the_allowed_hours_blocks_the_raise() -> None:
    """``allowed_hours`` は ``[開始, 終了)``。12:00 UTC は [13, 24) の外、[12, 13) の中。"""
    outside = Driver(settings(allowed_hours={"value": [13, 24], "status": "provisional"}))
    outside.states(2)
    result = outside.step()
    assert result.record.skip_reason is ZoneExcitationSkipReason.OUTSIDE_HOURS

    inside = Driver(settings(allowed_hours={"value": [12, 13], "status": "provisional"}))
    inside.states(2)
    assert inside.step().record.state is ZoneExcitationState.ON
