"""subprocess を使わず NVML を直接読む adapter。#65"""

from __future__ import annotations

import importlib
import math
from collections.abc import Callable
from types import ModuleType
from typing import Any, Protocol, cast

from coldaisle.internal_telemetry.config import NvmlConfig
from coldaisle.internal_telemetry.models import AdapterResult, SourceStatus
from coldaisle.store import Quality, Reading

GIB = 1024**3


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


def _gpu_metrics(index: int) -> tuple[str, ...]:
    return (
        f"gpu.{index}.core",
        f"gpu.{index}.hotspot",
        f"gpu.{index}.mem",
        f"gpu.{index}.utilization",
        f"gpu.{index}.vram_used",
        f"power.gpu.{index}",
    )


class NvmlAdapter:
    """単一 GPU を1回の NVML session から読み取る。"""

    name = "nvml"

    def __init__(self, config: NvmlConfig, api: NvmlApi | None = None) -> None:
        self._config = config
        self._api = api or PynvmlApi()
        self._initialized = False
        self._expected_metrics = (
            *(metric for index in config.gpu_indices for metric in _gpu_metrics(index)),
            "sys.cuda_processes",
        )

    @property
    def expected_metrics(self) -> tuple[str, ...]:
        return self._expected_metrics if self._config.enabled else ()

    def poll(self) -> AdapterResult:
        """GPUごとの失敗を分離して読み、取得不能値を missing にする。"""
        if not self._config.enabled:
            return AdapterResult(source=self.name, status=SourceStatus.DISABLED)
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
            )

        readings: list[Reading] = []
        process_ids: set[int] = set()
        process_info_complete = True
        critical_available = True
        power_available = True
        any_available = False
        for index in self._config.gpu_indices:
            if index >= count:
                readings.extend(_missing(metric) for metric in _gpu_metrics(index))
                critical_available = False
                power_available = False
                process_info_complete = False
                continue
            try:
                handle = self._api.handle(index)
            except Exception:
                readings.extend(_missing(metric) for metric in _gpu_metrics(index))
                critical_available = False
                power_available = False
                process_info_complete = False
                continue

            core = self._read(self._api.core_temperature_c, handle)
            hotspot = self._read(self._api.hotspot_temperature_c, handle)
            memory = self._read(self._api.memory_temperature_c, handle)
            utilization = self._read(self._api.utilization_pct, handle)
            vram = self._read(self._api.vram_used_gb, handle)
            power = self._read(self._api.power_w, handle)
            readings.extend(
                (
                    _reading(f"gpu.{index}.core", core),
                    _reading(f"gpu.{index}.hotspot", hotspot),
                    _reading(f"gpu.{index}.mem", memory),
                    _reading(f"gpu.{index}.utilization", utilization),
                    _reading(f"gpu.{index}.vram_used", vram),
                    _reading(f"power.gpu.{index}", power),
                )
            )
            critical_available &= core is not None
            power_available &= power is not None
            any_available |= any(
                value is not None for value in (core, hotspot, memory, utilization, vram, power)
            )
            try:
                process_ids.update(self._api.compute_process_ids(handle))
            except Exception:
                process_info_complete = False

        readings.append(
            Reading(
                metric="sys.cuda_processes",
                value=float(len(process_ids)) if any_available and process_info_complete else None,
                quality=(
                    Quality.OK if any_available and process_info_complete else Quality.MISSING
                ),
            )
        )
        if not any_available:
            status = SourceStatus.UNAVAILABLE
        elif not critical_available or not power_available:
            status = SourceStatus.DEGRADED
        else:
            status = SourceStatus.OK
        return AdapterResult(source=self.name, status=status, readings=tuple(readings))

    @staticmethod
    def _read(reader: Callable[[object], float | None], handle: object) -> float | None:
        try:
            value = reader(handle)
        except Exception:
            return None
        if value is None or not math.isfinite(value):
            return None
        return value

    def _unavailable(self, error: Exception) -> AdapterResult:
        return AdapterResult(
            source=self.name,
            status=SourceStatus.UNAVAILABLE,
            readings=tuple(_missing(metric) for metric in self.expected_metrics),
            detail=type(error).__name__,
        )

    def close(self) -> None:
        """初期化済みの NVML session だけを終了する。"""
        if not self._initialized:
            return
        self._api.shutdown()
        self._initialized = False


def _reading(metric: str, value: float | None) -> Reading:
    return Reading(
        metric=metric,
        value=value,
        quality=Quality.OK if value is not None else Quality.MISSING,
    )


def _missing(metric: str) -> Reading:
    return Reading(metric=metric, value=None, quality=Quality.MISSING)
