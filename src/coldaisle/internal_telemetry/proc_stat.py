"""``/proc/stat`` から全 CPU の使用率を読む読み取り専用 adapter。#65 / 決定記録 0047"""

from __future__ import annotations

import sys
from typing import NamedTuple

from coldaisle.internal_telemetry.config import ProcStatConfig
from coldaisle.internal_telemetry.models import AdapterResult, SourceStatus
from coldaisle.store import Quality, Reading

CPU_UTILIZATION_METRIC = "cpu.utilization"
"""全 CPU を合わせた使用率のメトリクス名（決定記録 0047）。"""

_BUSY_FIELDS = 8
"""``cpu`` 行の先頭8列（user nice system idle iowait irq softirq steal）。

guest / guest_nice は kernel が user / nice に既に含めているため、足すと二重に数える。
proc(5) の ABI であり、運用で調整する値ではない。
"""
_IDLE_INDEX = 3
_IOWAIT_INDEX = 4


class CpuTimes(NamedTuple):
    """``cpu`` 行の累積 jiffies。idle は iowait を含む。"""

    total: int
    idle: int


def parse_cpu_times(text: str) -> CpuTimes:
    """``/proc/stat`` の集計行（``cpu `` で始まる行）を読む。形が違えば ``ValueError``。"""
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "cpu":
            continue
        values = [int(field) for field in fields[1 : 1 + _BUSY_FIELDS]]
        # 8列に満たない行で total を作ると irq / softirq / steal が抜け、使用率が
        # 決定記録 0047 の定義からずれる。途中で切れた行は読み取り失敗として扱う
        if len(values) < _BUSY_FIELDS:
            raise ValueError("/proc/stat の cpu 行の列が足りない")
        if any(value < 0 for value in values):
            raise ValueError("/proc/stat の cpu 行に負の値がある")
        return CpuTimes(total=sum(values), idle=values[_IDLE_INDEX] + values[_IOWAIT_INDEX])
    raise ValueError("/proc/stat に cpu 集計行が無い")


class ProcStatAdapter:
    """前回 poll との差分から CPU 使用率（%）を出す。

    ``/proc/stat`` は起動からの累積値なので、1回の読み取りでは使用率にならない。
    最初の poll と、差分が取れない poll（読み取り失敗の直後、カウンタの巻き戻り、
    経過 0）は ``missing`` とし、0 % にしない。
    """

    name = "proc_stat"

    def __init__(self, config: ProcStatConfig, *, platform: str = sys.platform) -> None:
        self._config = config
        self._supported = platform.startswith("linux")
        self._previous: CpuTimes | None = None

    @property
    def expected_metrics(self) -> tuple[str, ...]:
        return (CPU_UTILIZATION_METRIC,) if self._config.enabled else ()

    def poll(self) -> AdapterResult:
        """現在の累積値を読み、前回との差分で使用率を計算する。"""
        if not self._config.enabled:
            return AdapterResult(source=self.name, status=SourceStatus.DISABLED)
        if not self._supported:
            return self._unavailable("unsupported_platform")
        try:
            current = parse_cpu_times(self._config.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            # 次の poll で失敗をまたいだ差分を使わない
            self._previous = None
            return self._unavailable(type(error).__name__)
        previous, self._previous = self._previous, current
        if previous is None:
            return self._result(None, "no_previous_sample")
        total = current.total - previous.total
        idle = current.idle - previous.idle
        if total <= 0 or idle < 0 or idle > total:
            return self._result(None, "counter_not_advanced")
        return self._result((total - idle) / total * 100.0, None)

    def _result(self, value: float | None, detail: str | None) -> AdapterResult:
        # 差分待ちの欠測は source の故障ではないため状態は ok のまま
        return AdapterResult(
            source=self.name,
            status=SourceStatus.OK,
            readings=(
                Reading(
                    metric=CPU_UTILIZATION_METRIC,
                    value=value,
                    quality=Quality.OK if value is not None else Quality.MISSING,
                ),
            ),
            detail=detail,
        )

    def _unavailable(self, detail: str) -> AdapterResult:
        return AdapterResult(
            source=self.name,
            status=SourceStatus.UNAVAILABLE,
            readings=(Reading(metric=CPU_UTILIZATION_METRIC, value=None, quality=Quality.MISSING),),
            detail=detail,
        )

    def close(self) -> None:
        """poll ごとに開くため、保持 resource はない。"""
