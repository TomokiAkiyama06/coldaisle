"""NVML adapter を fake API で検証する。GPU / driver は不要。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from coldaisle import logs
from coldaisle.internal_telemetry import (
    THROTTLE_REASON_BITS,
    ClockEventReasons,
    NvmlAdapter,
    NvmlConfig,
    SourceStatus,
)
from coldaisle.internal_telemetry.nvml import FAN_POLICY_MANUAL, PynvmlApi
from coldaisle.store import Quality


@dataclass
class FakeNvml:
    count: int = 1
    fail_init: bool = False
    fail_power: bool = False
    fail_processes: bool = False
    initialized: int = 0
    shutdowns: int = 0
    processes: dict[int, tuple[int, ...]] = field(default_factory=lambda: {0: (10, 20)})
    margin: float | None = 62.0
    reasons: ClockEventReasons | None = field(
        default_factory=lambda: ClockEventReasons(active=0, supported=0x1FF)
    )
    fail_reasons: bool = False
    fan: float | None = 30.0
    fan_policy: float | None = 1.0

    def initialize(self) -> None:
        self.initialized += 1
        if self.fail_init:
            raise RuntimeError("driver unavailable")

    def shutdown(self) -> None:
        self.shutdowns += 1

    def device_count(self) -> int:
        return self.count

    def handle(self, index: int) -> object:
        return index

    def core_temperature_c(self, handle: object) -> float:
        return 60.0 + int(handle)

    def hotspot_temperature_c(self, handle: object) -> float | None:
        return None

    def memory_temperature_c(self, handle: object) -> float | None:
        return 70.0 + int(handle)

    def power_w(self, handle: object) -> float:
        if self.fail_power:
            raise RuntimeError("unsupported")
        return 250.0 + int(handle)

    def utilization_pct(self, handle: object) -> float:
        return 80.0

    def vram_used_gb(self, handle: object) -> float:
        return 12.5

    def compute_process_ids(self, handle: object) -> tuple[int, ...]:
        if self.fail_processes:
            raise RuntimeError("unsupported")
        return self.processes.get(int(handle), ())

    def tlimit_margin_c(self, handle: object) -> float | None:
        return self.margin

    def clock_event_reasons(self, handle: object) -> ClockEventReasons | None:
        if self.fail_reasons:
            raise RuntimeError("unsupported")
        return self.reasons

    def fan_speed_pct(self, handle: object) -> float | None:
        return self.fan

    def fan_policy_manual(self, handle: object) -> float | None:
        return self.fan_policy


def config(*indices: int, enabled: bool = True) -> NvmlConfig:
    return NvmlConfig(enabled=enabled, gpu_indices=indices or (0,))


def by_metric(adapter: NvmlAdapter):
    return {reading.metric: reading for reading in adapter.poll().readings}


def test_nvml_reads_direct_metrics_and_marks_optional_temperature_missing():
    adapter = NvmlAdapter(config(0), FakeNvml())

    result = adapter.poll()
    readings = {reading.metric: reading for reading in result.readings}

    assert result.status is SourceStatus.OK
    assert readings["gpu.0.core"].value == 60.0
    assert readings["gpu.0.hotspot"].quality is Quality.MISSING
    assert readings["gpu.0.mem"].value == 70.0
    assert readings["gpu.0.utilization"].value == 80.0
    assert readings["gpu.0.vram_used"].value == 12.5
    assert readings["power.gpu.0"].value == 250.0
    assert readings["sys.cuda_processes"].value == 2.0


def test_nvml_initializes_once_across_polls():
    api = FakeNvml(processes={0: (10, 20)})
    adapter = NvmlAdapter(config(0), api)

    first = by_metric(adapter)
    second = by_metric(adapter)
    adapter.close()

    assert first["sys.cuda_processes"].value == 2.0
    assert second["gpu.0.core"].value == 60.0
    assert api.initialized == 1
    assert api.shutdowns == 1


def test_nvml_rejects_unstable_multi_gpu_enumeration_mapping():
    with pytest.raises(ValidationError, match="単一 GPU"):
        config(0, 1)


def test_nvml_runtime_requires_exactly_one_physical_gpu():
    adapter = NvmlAdapter(config(0), FakeNvml(count=2))

    result = adapter.poll()

    assert result.status is SourceStatus.UNAVAILABLE
    assert result.detail == "unexpected_device_count:2"
    assert {reading.quality for reading in result.readings} == {Quality.MISSING}


def test_nvml_failure_is_explicit_missing_and_does_not_raise():
    adapter = NvmlAdapter(config(0), FakeNvml(fail_init=True))

    result = adapter.poll()

    assert result.status is SourceStatus.UNAVAILABLE
    assert result.detail == "RuntimeError"
    assert {reading.quality for reading in result.readings} == {Quality.MISSING}
    assert {reading.value for reading in result.readings} == {None}


def test_missing_power_degrades_but_preserves_core_temperature():
    adapter = NvmlAdapter(config(0), FakeNvml(fail_power=True))

    result = adapter.poll()
    readings = {reading.metric: reading for reading in result.readings}

    assert result.status is SourceStatus.DEGRADED
    assert readings["gpu.0.core"].quality is Quality.OK
    assert readings["power.gpu.0"].quality is Quality.MISSING


def test_unavailable_process_api_is_missing_not_a_false_zero():
    adapter = NvmlAdapter(config(0), FakeNvml(fail_processes=True))

    readings = by_metric(adapter)

    assert readings["sys.cuda_processes"].value is None
    assert readings["sys.cuda_processes"].quality is Quality.MISSING


@dataclass
class ScalarlessNvml(FakeNvml):
    """温度・電力などの scalar がすべて失敗し、process 一覧だけ取れる driver。"""

    def core_temperature_c(self, handle: object) -> float:
        raise RuntimeError("unsupported")

    def memory_temperature_c(self, handle: object) -> float | None:
        raise RuntimeError("unsupported")

    def power_w(self, handle: object) -> float:
        raise RuntimeError("unsupported")

    def utilization_pct(self, handle: object) -> float:
        raise RuntimeError("unsupported")

    def vram_used_gb(self, handle: object) -> float:
        raise RuntimeError("unsupported")


def test_cuda_process_count_is_valid_when_only_scalars_fail():
    """process 一覧が取れていれば、scalar が全滅しても件数は ok で記録する。"""
    adapter = NvmlAdapter(config(0), ScalarlessNvml(processes={0: (10, 20, 30)}))

    readings = by_metric(adapter)

    assert readings["gpu.0.core"].quality is Quality.MISSING
    assert readings["sys.cuda_processes"].value == 3.0
    assert readings["sys.cuda_processes"].quality is Quality.OK


def test_disabled_nvml_has_no_expected_or_missing_metrics():
    adapter = NvmlAdapter(config(0, enabled=False), FakeNvml(fail_init=True))

    result = adapter.poll()

    assert adapter.expected_metrics == ()
    assert result.status is SourceStatus.DISABLED
    assert result.readings == ()


THROTTLE_METRICS = tuple(f"gpu.0.throttle.{reason}" for reason in THROTTLE_REASON_BITS)


def test_reads_tlimit_margin_fan_speed_and_idle_throttle_flags():
    result = NvmlAdapter(config(0), FakeNvml()).poll()
    readings = {reading.metric: reading for reading in result.readings}

    assert result.status is SourceStatus.OK
    assert readings["gpu.0.tlimit_margin"].value == 62.0
    assert readings["gpu.0.fan_speed"].value == 30.0
    for metric in THROTTLE_METRICS:
        # 報告できる reason が立っていないことは、欠測ではなく ok の 0
        assert readings[metric].value == 0.0
        assert readings[metric].quality is Quality.OK


def test_active_reasons_become_one_per_flag():
    # HW Slowdown (0x8) + HW Thermal (0x40) + SW Power Cap (0x4)。GpuIdle (0x1) は記録しない
    api = FakeNvml(reasons=ClockEventReasons(active=0x8 | 0x40 | 0x4 | 0x1, supported=0x1FF))

    readings = by_metric(NvmlAdapter(config(0), api))

    assert readings["gpu.0.throttle.hw_slowdown"].value == 1.0
    assert readings["gpu.0.throttle.hw_thermal"].value == 1.0
    assert readings["gpu.0.throttle.sw_power_cap"].value == 1.0
    assert readings["gpu.0.throttle.sw_thermal"].value == 0.0
    assert readings["gpu.0.throttle.hw_power_brake"].value == 0.0


def test_unsupported_reason_is_missing_not_zero():
    """GPU が報告しない reason を 0（起きていない）として保存しない。"""
    api = FakeNvml(reasons=ClockEventReasons(active=0, supported=0x1FF & ~0x80))

    readings = by_metric(NvmlAdapter(config(0), api))

    assert readings["gpu.0.throttle.hw_power_brake"].value is None
    assert readings["gpu.0.throttle.hw_power_brake"].quality is Quality.MISSING
    assert readings["gpu.0.throttle.hw_thermal"].quality is Quality.OK


@pytest.mark.parametrize(
    "api",
    [FakeNvml(reasons=None), FakeNvml(fail_reasons=True)],
    ids=["api-absent", "api-raises"],
)
def test_unreadable_reasons_are_all_missing(api: FakeNvml):
    result = NvmlAdapter(config(0), api).poll()
    readings = {reading.metric: reading for reading in result.readings}

    for metric in THROTTLE_METRICS:
        assert readings[metric].value is None
        assert readings[metric].quality is Quality.MISSING
    # Advisory（決定記録 0043 / 0029）なので source 状態は下げない
    assert result.status is SourceStatus.OK


def test_unsupported_margin_and_fan_are_missing_and_advisory():
    result = NvmlAdapter(config(0), FakeNvml(margin=None, fan=None)).poll()
    readings = {reading.metric: reading for reading in result.readings}

    assert readings["gpu.0.tlimit_margin"].quality is Quality.MISSING
    assert readings["gpu.0.tlimit_margin"].value is None
    assert readings["gpu.0.fan_speed"].quality is Quality.MISSING
    assert readings["gpu.0.fan_speed"].value is None
    assert result.status is SourceStatus.OK


def test_negative_margin_is_kept_as_measured():
    """T.Limit を超えると margin は負になる。0 へ丸めると超過量が消える。"""
    readings = by_metric(NvmlAdapter(config(0), FakeNvml(margin=-3.0)))

    assert readings["gpu.0.tlimit_margin"].value == -3.0
    assert readings["gpu.0.tlimit_margin"].quality is Quality.OK


def test_new_metrics_are_expected_so_outages_are_counted():
    expected = NvmlAdapter(config(0), FakeNvml()).expected_metrics

    assert {"gpu.0.tlimit_margin", "gpu.0.fan_speed", *THROTTLE_METRICS} <= set(expected)


def test_throttle_bits_match_the_nvml_abi():
    """pynvml の定数と一致すること。binding が無い環境では飛ばす。"""
    pynvml = pytest.importorskip("pynvml")
    names = {
        "hw_slowdown": "nvmlClocksThrottleReasonHwSlowdown",
        "hw_thermal": "nvmlClocksThrottleReasonHwThermalSlowdown",
        "sw_thermal": "nvmlClocksThrottleReasonSwThermalSlowdown",
        "hw_power_brake": "nvmlClocksThrottleReasonHwPowerBrakeSlowdown",
        "sw_power_cap": "nvmlClocksThrottleReasonSwPowerCap",
    }
    assert {reason: getattr(pynvml, name) for reason, name in names.items()} == dict(
        THROTTLE_REASON_BITS
    )


# ---------------------------------------------------------------- fan_policy（決定記録 0131 §2.2）


@pytest.mark.parametrize(("policy", "expected"), [(1.0, 1.0), (0.0, 0.0)])
def test_fan_policy_is_recorded_as_a_flag(policy, expected):
    result = NvmlAdapter(config(0), FakeNvml(fan_policy=policy)).poll()
    readings = {reading.metric: reading for reading in result.readings}

    assert readings["gpu.0.fan_policy"].value == expected
    assert readings["gpu.0.fan_policy"].quality is Quality.OK
    assert "gpu.0.fan_policy" in NvmlAdapter(config(0), FakeNvml()).expected_metrics


def test_unreadable_fan_policy_is_missing_and_advisory():
    """関数が無い driver・NOT_SUPPORTED は missing。制御の入力ではないので状態を下げない。"""
    result = NvmlAdapter(config(0), FakeNvml(fan_policy=None)).poll()
    readings = {reading.metric: reading for reading in result.readings}

    assert readings["gpu.0.fan_policy"].value is None
    assert readings["gpu.0.fan_policy"].quality is Quality.MISSING
    assert result.status is SourceStatus.OK


class _NotSupportedError(Exception):
    """pynvml の ``NVMLError_NotSupported`` の代わり。"""


class _FakePynvml:
    """``pynvml`` モジュールの代わり。Fan ごとの制御方式を返す（書く関数は持たない）。"""

    def __init__(self, policies: list[int] | None, *, raise_on: int | None = None) -> None:
        self._policies = policies
        self._raise_on = raise_on
        if policies is None:
            return
        self.nvmlDeviceGetNumFans = lambda handle: len(self._policies or [])
        self.nvmlDeviceGetFanControlPolicy_v2 = self._policy

    def _policy(self, handle: object, fan: int) -> int:
        if fan == self._raise_on:
            raise _NotSupportedError("NVML_ERROR_NOT_SUPPORTED")
        assert self._policies is not None
        return self._policies[fan]


def _pynvml_api(module: object) -> PynvmlApi:
    api = PynvmlApi()
    api._module = module  # type: ignore[assignment]
    return api


@pytest.mark.parametrize(
    ("policies", "expected"),
    [
        ([FAN_POLICY_MANUAL, FAN_POLICY_MANUAL], 1.0),
        # 1 つでも手動でなければ「握っていない」（サービスはすべての Fan に同じ値を書く）
        ([FAN_POLICY_MANUAL, 0], 0.0),
        ([0, 0], 0.0),
        ([], None),  # Fan が無い GPU は判定できない
        (None, None),  # 関数が無い binding / driver
    ],
)
def test_pynvml_fan_policy_reads_every_fan(policies, expected):
    assert _pynvml_api(_FakePynvml(policies)).fan_policy_manual(handle=0) == expected


def test_pynvml_not_supported_becomes_missing_through_the_adapter():
    """NOT_SUPPORTED の例外は adapter の ``_read`` が欠測に畳む（0043 の T.Limit と同じ扱い）。"""
    api = _pynvml_api(_FakePynvml([FAN_POLICY_MANUAL, FAN_POLICY_MANUAL], raise_on=1))
    with pytest.raises(_NotSupportedError):
        api.fan_policy_manual(handle=0)
    assert NvmlAdapter._read(api.fan_policy_manual, 0) is None


def test_fan_policy_manual_matches_the_nvml_abi():
    """pynvml の定数と一致すること。binding が無い環境では飛ばす。"""
    pynvml = pytest.importorskip("pynvml")
    if not hasattr(pynvml, "NVML_FAN_POLICY_MANUAL"):
        pytest.skip("この binding は定数を持たない")
    assert pynvml.NVML_FAN_POLICY_MANUAL == FAN_POLICY_MANUAL


def test_nvml_adapter_never_writes_the_fan():
    """読むのは制御方式だけで、書かない（0131 §2.2 / 0128 §2.1）。"""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "internal_telemetry" / "nvml.py"
    ).read_text(encoding="utf-8")
    assert "nvmlDeviceSetFan" not in source
    assert "SetDefaultFan" not in source


# ------------------------------------------------ 記録しない metric（決定記録 0176）


class CountingNvml(FakeNvml):
    """hotspot / memory を読んだ回数を数える。"""

    def __init__(self, *, hotspot: float | None = None, memory: float | None = None) -> None:
        super().__init__()
        self.hotspot = hotspot
        self.memory = memory
        self.hotspot_reads = 0
        self.memory_reads = 0

    def hotspot_temperature_c(self, handle: object) -> float | None:
        self.hotspot_reads += 1
        return self.hotspot

    def memory_temperature_c(self, handle: object) -> float | None:
        self.memory_reads += 1
        return self.memory


def excluding(*metrics: str) -> NvmlConfig:
    return NvmlConfig(enabled=True, gpu_indices=(0,), excluded_metrics=metrics)


def test_excluded_metrics_are_neither_expected_nor_written() -> None:
    adapter = NvmlAdapter(excluding("gpu.0.hotspot", "gpu.0.mem"), CountingNvml())

    assert "gpu.0.hotspot" not in adapter.expected_metrics
    assert "gpu.0.mem" not in adapter.expected_metrics
    assert "gpu.0.core" in adapter.expected_metrics
    for _ in range(3):
        readings = by_metric(adapter)
        assert "gpu.0.hotspot" not in readings
        assert "gpu.0.mem" not in readings
        assert readings["gpu.0.core"].value == 60.0


def test_excluded_metrics_are_left_out_of_the_missing_rows_too() -> None:
    """NVML が使えない周期の missing の行にも、除外した metric を書かない。"""
    adapter = NvmlAdapter(excluding("gpu.0.hotspot", "gpu.0.mem"), FakeNvml(fail_init=True))

    readings = by_metric(adapter)

    assert "gpu.0.hotspot" not in readings
    assert "gpu.0.mem" not in readings
    assert readings["gpu.0.core"].quality is Quality.MISSING


def test_excluded_metrics_are_probed_once_and_never_read_again() -> None:
    api = CountingNvml()
    adapter = NvmlAdapter(excluding("gpu.0.hotspot", "gpu.0.mem"), api)

    for _ in range(4):
        adapter.poll()

    assert (api.hotspot_reads, api.memory_reads) == (1, 1)


def test_an_excluded_metric_that_becomes_readable_is_logged_not_written(caplog) -> None:
    """driver の更新で値が出ても、黙って捨て続けない。行は書かない。"""
    api = CountingNvml(hotspot=None, memory=71.0)
    adapter = NvmlAdapter(excluding("gpu.0.hotspot", "gpu.0.mem"), api)

    with caplog.at_level("WARNING", logger="coldaisle.internal_telemetry"):
        readings = by_metric(adapter)
        adapter.poll()

    warnings = [
        getattr(record, logs.FIELDS_KEY)
        for record in caplog.records
        if getattr(record, logs.FIELDS_KEY, {}).get("event") == "excluded_metric_available"
    ]
    assert warnings == [{"event": "excluded_metric_available", "metrics": ["gpu.0.mem"]}]
    assert "gpu.0.mem" not in readings


def test_unpublished_temperatures_still_read_when_not_excluded() -> None:
    api = CountingNvml(memory=71.0)
    adapter = NvmlAdapter(config(0), api)

    readings = by_metric(adapter)

    assert readings["gpu.0.mem"].value == 71.0
    assert readings["gpu.0.hotspot"].quality is Quality.MISSING
    assert api.hotspot_reads == 1


@pytest.mark.parametrize(
    "metrics",
    [
        ("gpu.0.core",),
        ("power.gpu.0",),
        ("gpu.0.utilization",),
        ("gpu.1.hotspot",),
        ("gpu.0.hotspot", "gpu.0.hotspot"),
    ],
)
def test_only_unpublished_temperatures_can_be_excluded(metrics: tuple[str, ...]) -> None:
    """core（Critical Safety の入力）や電力を誤記で止めない（決定記録 0176 §2.1）。"""
    with pytest.raises(ValidationError):
        excluding(*metrics)
