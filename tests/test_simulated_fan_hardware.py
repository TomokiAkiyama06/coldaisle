"""#77 simulated Fan Hardware Backend の検証。"""

from __future__ import annotations

from coldaisle.control.config import FanHardwareConfig
from coldaisle.control.hardware import SimulatedFanBackend, SimulatedFaultPlan
from coldaisle.control.schema import BoundBy, EffectiveZoneDemand, FaultCode, PerZone, Zone


def hardware_config() -> FanHardwareConfig:
    """実機の識別子を含まない、測定済み profile のテスト設定を作る。"""
    profile = {
        "startup_demand": 0.6,
        "minimum_stable_demand": 0.3,
        "maximum_rpm": 1800,
        "pwm_to_rpm": [
            {"demand": 0.3, "rpm": 600},
            {"demand": 0.6, "rpm": 1200},
            {"demand": 1.0, "rpm": 1800},
        ],
        "airflow_index": [0.2, 0.6, 1.0],
    }
    return FanHardwareConfig.model_validate(
        {
            "schema_version": 1,
            "approval": {"status": "confirmed", "basis": "test characterization"},
            "zones": {
                "front": {
                    "driver": "test-superio",
                    "label": "front-header",
                    "pwm_attribute": "pwm1",
                    "tach_attribute": "fan1_input",
                    "enable_attribute": "pwm1_enable",
                    "profile": profile,
                },
                "rear": {
                    "driver": "test-superio",
                    "label": "rear-header",
                    "pwm_attribute": "pwm2",
                    "tach_attribute": "fan2_input",
                    "enable_attribute": "pwm2_enable",
                    "profile": profile,
                },
                "top": {
                    "driver": "test-superio",
                    "label": "top-header",
                    "pwm_attribute": "pwm3",
                    "tach_attribute": "fan3_input",
                    "enable_attribute": "pwm3_enable",
                    "profile": profile,
                },
            },
        }
    )


def effective(demand: float) -> EffectiveZoneDemand:
    """Hardware Backend に渡せる、Safety 通過済み demand を作る。"""
    return EffectiveZoneDemand(
        requested=demand,
        effective=demand,
        bound_by=BoundBy.REQUESTED,
        safety_floor=0.0,
        forced_max=False,
    )


def test_simulated_backend_controls_three_zones_independently() -> None:
    backend = SimulatedFanBackend(hardware_config())

    results = backend.apply(PerZone(front=effective(0.4), rear=effective(0.7), top=effective(1.0)))

    # 初回は kick のため Front が startup demand まで上がるが、Rear / Top は
    # それぞれの effective demand に対応する。各 zone は同じ Interface で独立する。
    assert results.front.target_rpm == 1200
    assert results.rear.target_rpm == 1350
    assert results.top.target_rpm == 1800
    assert results.front.airflow_index == 0.6
    assert results.rear.airflow_index == 0.7
    assert results.top.readback.pwm_raw == 255
    assert all(result.fault is None for result in (results.front, results.rear, results.top))


def test_first_write_kicks_and_subsequent_write_never_uses_unsafe_low_pwm() -> None:
    backend = SimulatedFanBackend(hardware_config())
    demands = PerZone(front=effective(0.0), rear=effective(0.0), top=effective(0.0))

    first = backend.apply(demands)
    second = backend.apply(demands)

    # profile の 0.3（minimum stable）未満を backend が生成しない。初回は 0.6 の kick。
    assert first.front.readback.pwm_raw == 153
    assert second.front.readback.pwm_raw == 76
    assert first.front.target_rpm == 1200
    assert second.front.target_rpm == 600


def test_simulated_failures_are_reported_as_zone_faults_for_critical_safety() -> None:
    backend = SimulatedFanBackend(
        hardware_config(),
        SimulatedFaultPlan(
            write_failure=frozenset({Zone.FRONT}),
            readback_mismatch=frozenset({Zone.REAR}),
            tach_stall=frozenset({Zone.TOP}),
        ),
    )

    results = backend.apply(PerZone(front=effective(0.7), rear=effective(0.7), top=effective(0.7)))

    assert results.front.fault is not None
    assert results.front.fault.code is FaultCode.WRITE_FAILURE
    assert results.front.readback.write_ok is False
    assert results.rear.fault is not None
    assert results.rear.fault.code is FaultCode.READBACK_MISMATCH
    assert results.rear.readback.readback_ok is False
    assert results.top.fault is not None
    assert results.top.fault.code is FaultCode.TACH_STALL
    assert results.top.readback.rpm == 0


def test_backend_exposes_no_arbitrary_header_or_hwmon_write_api() -> None:
    backend = SimulatedFanBackend(hardware_config())

    # Protocol の唯一の入口は PerZone[EffectiveZoneDemand]。hwmon 番号・path・
    # 任意 attribute を受け取る API が無く、config で検証済みの3 header 以外へ
    # 向ける経路を作れない。
    assert not hasattr(backend, "write_header")
    assert not hasattr(backend, "write_path")
