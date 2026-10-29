"""subprocess を使わず NVML を直接読む adapter。#65"""

from __future__ import annotations

import importlib
import logging
import math
from collections.abc import Callable
from types import ModuleType
from typing import Any, NamedTuple, Protocol, cast

from coldaisle import logs
from coldaisle.internal_telemetry.config import NvmlConfig
from coldaisle.internal_telemetry.gpu_state import GPU_STATE_KEY, GpuState, GpuStateClassifier
from coldaisle.internal_telemetry.models import AdapterResult, SourceStatus
from coldaisle.store import Quality, Reading

GIB = 1024**3

LOGGER = logging.getLogger("coldaisle.internal_telemetry")

THROTTLE_REASON_BITS: dict[str, int] = {
    "hw_slowdown": 0x8,
    "hw_thermal": 0x40,
    "sw_thermal": 0x20,
    "hw_power_brake": 0x80,
    "sw_power_cap": 0x4,
}
"""記録する clock event reason と NVML ABI のビット（決定記録 0043）。

値は ``nvml.h`` の ``nvmlClocksEventReason*`` と同じで、運用で調整する値ではない。
pynvml の定数名は版で変わってきた（``ClocksThrottle`` → ``ClocksEvent``）ため、
名前ではなく ABI のビット値に固定する。
"""


FAN_POLICY_MANUAL = 1
"""``nvmlDeviceGetFanControlPolicy_v2`` の手動（``NVML_FAN_POLICY_MANUAL``）。決定記録 0131 §2.2

``coldaisle-gpu-fan``（0128）が Fan を握っている間はすべての Fan がこれになる。
ABI の値に固定する（pynvml の定数名に依存しない。``THROTTLE_REASON_BITS`` と同じ理由）。
"""


class ClockEventReasons(NamedTuple):
    """現在の clock event reason と、この GPU が報告しうる reason のビット集合。"""

    active: int
    supported: int


class NvmlApi(Protocol):
    """pynvml を fake と差し替えるための小さい境界。"""

    def initialize(self) -> None: ...

    def shutdown(self) -> None: ...

    def device_count(self) -> int: ...

    def handle(self, index: int) -> object: ...

    def core_temperature_c(self, handle: object) -> float: ...

    def hotspot_temperature_c(self, handle: object) -> float | None: ...

    def memory_temperature_c(self, handle: object) -> float | None: ...

    def power_w(self, handle: object) -> float: ...

    def utilization_pct(self, handle: object) -> float: ...

    def vram_used_gb(self, handle: object) -> float: ...

    def compute_process_ids(self, handle: object) -> tuple[int, ...]: ...

    def tlimit_margin_c(self, handle: object) -> float | None: ...

    def clock_event_reasons(self, handle: object) -> ClockEventReasons | None: ...

    def fan_speed_pct(self, handle: object) -> float | None: ...

    def fan_policy_manual(self, handle: object) -> float | None: ...


class PynvmlApi:
    """``nvidia-ml-py`` の薄い wrapper。

    optional temperature type は driver / GPU が公開したときだけ読む。公開されない
    場合は ``None`` とし、core temperature まで巻き添えにしない。
    """

    def __init__(self) -> None:
        self._module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is None:
            self._module = importlib.import_module("pynvml")
        return self._module

    def initialize(self) -> None:
        self._load().nvmlInit()

    def shutdown(self) -> None:
        if self._module is not None:
            self._module.nvmlShutdown()

    def device_count(self) -> int:
        return int(self._load().nvmlDeviceGetCount())

    def handle(self, index: int) -> object:
        return cast(object, self._load().nvmlDeviceGetHandleByIndex(index))

    def core_temperature_c(self, handle: object) -> float:
        module = self._load()
        return float(module.nvmlDeviceGetTemperature(handle, module.NVML_TEMPERATURE_GPU))

    def hotspot_temperature_c(self, handle: object) -> float | None:
        return self._optional_temperature(handle, "NVML_TEMPERATURE_HOTSPOT")

    def memory_temperature_c(self, handle: object) -> float | None:
        # 現行 NVML は memory temperature を temperature type ではなく field ID
        # として公開する。driver / GPU が field を実装しない場合は optional 欠測。
        module = self._load()
        field_id = getattr(module, "NVML_FI_DEV_MEMORY_TEMP", None)
        if field_id is None:
            return None
        try:
            values = module.nvmlDeviceGetFieldValues(handle, [field_id])
            if len(values) != 1 or values[0].nvmlReturn != module.NVML_SUCCESS:
                return None
            return float(values[0].value.uiVal)
        except Exception:
            return None

    def _optional_temperature(self, handle: object, constant: str) -> float | None:
        module = self._load()
        sensor = getattr(module, constant, None)
        if sensor is None:
            return None
        try:
            return float(module.nvmlDeviceGetTemperature(handle, sensor))
        except Exception:
            return None

    def power_w(self, handle: object) -> float:
        return float(self._load().nvmlDeviceGetPowerUsage(handle)) / 1_000.0

    def utilization_pct(self, handle: object) -> float:
        rates = self._load().nvmlDeviceGetUtilizationRates(handle)
        return float(rates.gpu)

    def vram_used_gb(self, handle: object) -> float:
        memory = self._load().nvmlDeviceGetMemoryInfo(handle)
        return float(memory.used) / GIB

    def compute_process_ids(self, handle: object) -> tuple[int, ...]:
        module = self._load()
        for name in (
            "nvmlDeviceGetComputeRunningProcesses_v3",
            "nvmlDeviceGetComputeRunningProcesses_v2",
            "nvmlDeviceGetComputeRunningProcesses",
        ):
            function = getattr(module, name, None)
            if not callable(function):
                continue
            processes = cast(Callable[[object], list[Any]], function)(handle)
            return tuple(int(process.pid) for process in processes)
        raise NotImplementedError("NVML が compute process API を公開していない")

    def tlimit_margin_c(self, handle: object) -> float | None:
        # nvidia-smi の ``temperature.gpu.tlimit``。slowdown までの残り温度（°C）で、
        # 超えれば負になる。古い driver / binding は API 自体を持たない
        function = getattr(self._load(), "nvmlDeviceGetMarginTemperature", None)
        if not callable(function):
            return None
        return float(cast(Callable[[object], Any], function)(handle))

    def clock_event_reasons(self, handle: object) -> ClockEventReasons | None:
        module = self._load()
        active = _first_callable(
            module,
            "nvmlDeviceGetCurrentClocksEventReasons",
            "nvmlDeviceGetCurrentClocksThrottleReasons",
        )
        supported = _first_callable(
            module,
            "nvmlDeviceGetSupportedClocksEventReasons",
            "nvmlDeviceGetSupportedClocksThrottleReasons",
        )
        if active is None or supported is None:
            return None
        return ClockEventReasons(active=int(active(handle)), supported=int(supported(handle)))

    def fan_speed_pct(self, handle: object) -> float | None:
        # NVML の fan speed は「意図した回転数」の最大比で、tach の実測ではない。
        # 冷却ファンを持たない GPU では NotSupported が返り、呼び出し側で欠測にする
        return float(self._load().nvmlDeviceGetFanSpeed(handle))

    def fan_policy_manual(self, handle: object) -> float | None:
        """すべての Fan の制御方式が手動なら 1、1 つでも手動でなければ 0（決定記録 0131 §2.2）。

        **読むだけで書かない。** 関数が無い binding / driver は ``None``（欠測）。
        ``NVML_ERROR_NOT_SUPPORTED`` などの例外は呼び出し側の ``_read`` が欠測に畳む。
        Fan の数が 0 なら判定できないので欠測にする（「手動」とも「手動でない」とも言えない）。
        """
        module = self._load()
        count = getattr(module, "nvmlDeviceGetNumFans", None)
        policy = getattr(module, "nvmlDeviceGetFanControlPolicy_v2", None)
        if not callable(count) or not callable(policy):
            return None
        fans = int(count(handle))
        if fans <= 0:
            return None
        manual = all(int(policy(handle, fan)) == FAN_POLICY_MANUAL for fan in range(fans))
        return 1.0 if manual else 0.0


def _first_callable(module: ModuleType, *names: str) -> Callable[[object], Any] | None:
    for name in names:
        function = getattr(module, name, None)
        if callable(function):
            return cast(Callable[[object], Any], function)
    return None


def _gpu_metrics(index: int) -> tuple[str, ...]:
    return (
        f"gpu.{index}.core",
        f"gpu.{index}.hotspot",
        f"gpu.{index}.mem",
        f"gpu.{index}.utilization",
        f"gpu.{index}.vram_used",
        f"power.gpu.{index}",
        f"gpu.{index}.tlimit_margin",
        f"gpu.{index}.fan_speed",
        f"gpu.{index}.fan_policy",
        *(f"gpu.{index}.throttle.{reason}" for reason in THROTTLE_REASON_BITS),
    )


class NvmlAdapter:
    """単一 GPU を1回の NVML session から読み取る。

    ``gpu_state`` を渡すと、compute process の PID から ``sys.gpu_state`` を決めて
    ``AdapterResult.states`` に載せる（決定記録 0163 §2.6）。NVML が使えない・process の
    一覧が欠けた周期は ``unknown``。渡さなければ状態を出さない（読み取りの値だけ）。
    """

    name = "nvml"

    def __init__(
        self,
        config: NvmlConfig,
        api: NvmlApi | None = None,
        *,
        gpu_state: GpuStateClassifier | None = None,
    ) -> None:
        self._config = config
        self._api = api or PynvmlApi()
        self._gpu_state = gpu_state
        self._initialized = False
        self._excluded = frozenset(config.excluded_metrics)
        # 除外した metric が読めるかは起動後に1回だけ確かめる（決定記録 0176 §2.1）
        self._excluded_probed = not self._excluded
        self._expected_metrics = (
            *(metric for index in config.gpu_indices for metric in self._gpu_metrics(index)),
            "sys.cuda_processes",
        )

    @property
    def expected_metrics(self) -> tuple[str, ...]:
        return self._expected_metrics if self._config.enabled else ()

    def poll(self) -> AdapterResult:
        """GPUごとの失敗を分離して読み、取得不能値を missing にする。

        予期しない例外も collector と同じく source の ``unavailable`` に畳むが、ここで畳むのは
        ``sys.gpu_state`` を ``unknown`` にするため。collector で畳むと状態が書かれず、
        直前の値（たとえば ``idle``）が残り続ける。
        """
        try:
            return self._poll()
        except Exception as error:
            return self._unavailable(error)

    def _poll(self) -> AdapterResult:
        if not self._config.enabled:
            return AdapterResult(
                source=self.name,
                status=SourceStatus.DISABLED,
                states=self._unknown_state("nvml_disabled"),
            )
        try:
            if not self._initialized:
                self._api.initialize()
                self._initialized = True
            count = self._api.device_count()
        except Exception as error:
            return self._unavailable(error)
        if count != 1:
            return AdapterResult(
                source=self.name,
                status=SourceStatus.UNAVAILABLE,
                readings=tuple(_missing(metric) for metric in self.expected_metrics),
                detail=f"unexpected_device_count:{count}",
                states=self._unknown_state(f"unexpected_device_count:{count}"),
            )

        readings: list[Reading] = []
        process_ids: set[int] = set()
        process_info_complete = True
        critical_available = True
        power_available = True
        any_available = False
        for index in self._config.gpu_indices:
            if index >= count:
                readings.extend(_missing(metric) for metric in self._gpu_metrics(index))
                critical_available = False
                power_available = False
                process_info_complete = False
                continue
            try:
                handle = self._api.handle(index)
            except Exception:
                readings.extend(_missing(metric) for metric in self._gpu_metrics(index))
                critical_available = False
                power_available = False
                process_info_complete = False
                continue

            self._probe_excluded(index, handle)
            core = self._read(self._api.core_temperature_c, handle)
            hotspot = self._read_unless_excluded(
                f"gpu.{index}.hotspot", self._api.hotspot_temperature_c, handle
            )
            memory = self._read_unless_excluded(
                f"gpu.{index}.mem", self._api.memory_temperature_c, handle
            )
            utilization = self._read(self._api.utilization_pct, handle)
            vram = self._read(self._api.vram_used_gb, handle)
            power = self._read(self._api.power_w, handle)
            margin = self._read(self._api.tlimit_margin_c, handle)
            fan = self._read(self._api.fan_speed_pct, handle)
            fan_policy = self._read(self._api.fan_policy_manual, handle)
            throttle = _throttle_flags(self._api, handle)
            readings.extend(
                reading
                for reading in (
                    _reading(f"gpu.{index}.core", core),
                    _reading(f"gpu.{index}.hotspot", hotspot),
                    _reading(f"gpu.{index}.mem", memory),
                    _reading(f"gpu.{index}.utilization", utilization),
                    _reading(f"gpu.{index}.vram_used", vram),
                    _reading(f"power.gpu.{index}", power),
                    _reading(f"gpu.{index}.tlimit_margin", margin),
                    _reading(f"gpu.{index}.fan_speed", fan),
                    _reading(f"gpu.{index}.fan_policy", fan_policy),
                    *(
                        _reading(f"gpu.{index}.throttle.{reason}", flag)
                        for reason, flag in throttle.items()
                    ),
                )
                if reading.metric not in self._excluded
            )
            critical_available &= core is not None
            power_available &= power is not None
            any_available |= any(
                value is not None
                for value in (
                    core,
                    hotspot,
                    memory,
                    utilization,
                    vram,
                    power,
                    margin,
                    fan,
                    fan_policy,
                    *throttle.values(),
                )
            )
            try:
                process_ids.update(self._api.compute_process_ids(handle))
            except Exception:
                process_info_complete = False

        readings.append(
            Reading(
                metric="sys.cuda_processes",
                # 温度・電力などの scalar とは独立に取れる。scalar が全滅しても、
                # process 一覧が全 GPU ぶん取れていれば件数は正しい
                value=float(len(process_ids)) if process_info_complete else None,
                quality=Quality.OK if process_info_complete else Quality.MISSING,
            )
        )
        if not any_available:
            status = SourceStatus.UNAVAILABLE
        elif not critical_available or not power_available:
            status = SourceStatus.DEGRADED
        else:
            status = SourceStatus.OK
        if process_info_complete:
            states = self._classified_state(process_ids)
        else:
            # 一部の GPU の一覧しか無いと、見えていない GPU の process を数え落とし
            # idle / ai と誤る。温度の status とは独立に unknown にする
            states = self._unknown_state("compute_process_list_incomplete")
        return AdapterResult(
            source=self.name, status=status, readings=tuple(readings), states=states
        )

    def _classified_state(self, process_ids: set[int]) -> tuple[tuple[str, str], ...]:
        if self._gpu_state is None:
            return ()
        try:
            state = self._gpu_state.classify(process_ids)
        except Exception as error:
            # 判定の失敗で温度まで欠測にしない（#63「Compute Mode 中も温度欠測なし」）。
            # 握りつぶさず unknown とその理由をログに残す
            return self._unknown_state(f"classification_failed:{type(error).__name__}")
        return ((GPU_STATE_KEY, state.value),)

    def _unknown_state(self, reason: str) -> tuple[tuple[str, str], ...]:
        if self._gpu_state is None:
            return ()
        self._gpu_state.unknown(reason)
        return ((GPU_STATE_KEY, GpuState.UNKNOWN.value),)

    @staticmethod
    def _read(reader: Callable[[object], float | None], handle: object) -> float | None:
        try:
            value = reader(handle)
        except Exception:
            return None
        if value is None or not math.isfinite(value):
            return None
        return value

    def _gpu_metrics(self, index: int) -> tuple[str, ...]:
        """この GPU で記録する metric（``excluded_metrics`` を除く）。"""
        return tuple(metric for metric in _gpu_metrics(index) if metric not in self._excluded)

    def _read_unless_excluded(
        self, metric: str, reader: Callable[[object], float | None], handle: object
    ) -> float | None:
        # 除外した値は NVML から読まない。公開されない値を毎周期問い合わせても得るものが無い
        if metric in self._excluded:
            return None
        return self._read(reader, handle)

    def _probe_excluded(self, index: int, handle: object) -> None:
        """除外した metric を1回だけ読み、取れたら警告する（決定記録 0176 §2.1）。

        driver の更新で値が出るようになっても、黙って捨て続けないため。行は書かない。
        """
        if self._excluded_probed:
            return
        self._excluded_probed = True
        readers = {
            f"gpu.{index}.hotspot": self._api.hotspot_temperature_c,
            f"gpu.{index}.mem": self._api.memory_temperature_c,
        }
        available = sorted(
            metric
            for metric, reader in readers.items()
            if metric in self._excluded and self._read(reader, handle) is not None
        )
        if available:
            LOGGER.warning(
                "記録しない設定の GPU の metric が読める",
                extra={
                    logs.FIELDS_KEY: {
                        "event": "excluded_metric_available",
                        "metrics": available,
                    }
                },
            )

    def _unavailable(self, error: Exception) -> AdapterResult:
        return AdapterResult(
            source=self.name,
            status=SourceStatus.UNAVAILABLE,
            readings=tuple(_missing(metric) for metric in self.expected_metrics),
            detail=type(error).__name__,
            states=self._unknown_state(f"nvml_unavailable:{type(error).__name__}"),
        )

    def close(self) -> None:
        """初期化済みの NVML session だけを終了する。"""
        if not self._initialized:
            return
        self._api.shutdown()
        self._initialized = False


def _throttle_flags(api: NvmlApi, handle: object) -> dict[str, float | None]:
    """reason ごとの 0/1。報告されない reason は 0 ではなく欠測にする。

    GPU が報告しない reason のビットは常に 0 で、「起きていない」と区別できない。
    supported に無いビットを 0 として保存すると、誤った安心を生む。
    """
    try:
        reasons = api.clock_event_reasons(handle)
    except Exception:
        reasons = None
    if reasons is None:
        return dict.fromkeys(THROTTLE_REASON_BITS)
    return {
        reason: (float(bool(reasons.active & bit)) if reasons.supported & bit else None)
        for reason, bit in THROTTLE_REASON_BITS.items()
    }


def _reading(metric: str, value: float | None) -> Reading:
    return Reading(
        metric=metric,
        value=value,
        quality=Quality.OK if value is not None else Quality.MISSING,
    )


def _missing(metric: str) -> Reading:
    return Reading(metric=metric, value=None, quality=Quality.MISSING)
