"""Internal Telemetry Collector の設定。#65"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.store.models import validate_metric

CONNECTOR_TEMPERATURE_METRIC = "board.connector_12v2x6"
"""ASUS T_SENSOR で測る 12V-2x6 コネクタ外装温度の提案名（決定記録0032）。"""


class _ConfigModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NvmlConfig(_ConfigModel):
    """NVML adapter の設定。GPU index は NVML の列挙順。"""

    enabled: bool
    gpu_indices: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _indices_are_unique_and_non_negative(self) -> Self:
        if any(index < 0 for index in self.gpu_indices):
            raise ValueError("gpu_indices は 0 以上にする")
        if len(set(self.gpu_indices)) != len(self.gpu_indices):
            raise ValueError("gpu_indices は重複させない")
        return self


class HwmonMeasurement(StrEnum):
    """安定 label から解決する hwmon の測定種別。"""

    TEMPERATURE = "temperature"
    POWER = "power"
    RPM = "rpm"
    PWM = "pwm"


class HwmonSensorConfig(_ConfigModel):
    """1 metric と hwmon の安定属性の対応。

    ``hwmonN`` や ``temp3_input`` は再起動・kernel 更新で変わりうるため設定に持たず、
    driver の ``name`` と ``*_label`` から poll ごとに探索する。
    """

    metric: str
    enabled: bool
    driver: str | None = None
    label: str | None = None
    channel: str | None = Field(default=None, pattern=r"^(temp|power|fan)[1-9][0-9]*$")
    measurement: HwmonMeasurement
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def _has_a_stable_selector_and_valid_range(self) -> Self:
        validate_metric(self.metric)
        if self.enabled and not self.driver:
            raise ValueError("有効な hwmon sensor には driver が必要")
        if self.enabled and (self.label is None) == (self.channel is None):
            raise ValueError("有効な hwmon sensor は label または stable channel の片方を指定する")
        if self.driver is not None and (
            re.fullmatch(r"hwmon[0-9]+", self.driver, re.IGNORECASE) or "/" in self.driver
        ):
            raise ValueError("driver に hwmonN や path を指定しない")
        if self.label is not None and "/" in self.label:
            raise ValueError("label に path を指定しない")
        if (self.minimum is None) != (self.maximum is None):
            raise ValueError("minimum と maximum は両方指定する")
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValueError("minimum は maximum より小さくする")
        if self.enabled and self.metric == CONNECTOR_TEMPERATURE_METRIC and self.minimum is None:
            raise ValueError("T_SENSOR を有効にするには #50 で確認した妥当範囲が必要")
        return self


class HwmonConfig(_ConfigModel):
    """Linux hwmon 読み取り設定。root は標準 ABI の探索起点だけを指す。"""

    enabled: bool
    root: Path
    sensors: tuple[HwmonSensorConfig, ...] = ()

    @model_validator(mode="after")
    def _metrics_are_unique(self) -> Self:
        if self.root.name.lower().startswith("hwmon") and self.root.name[5:].isdigit():
            raise ValueError("hwmon root に不安定な hwmonN path を指定しない")
        metrics = [sensor.metric for sensor in self.sensors]
        if len(metrics) != len(set(metrics)):
            raise ValueError("hwmon sensor の metric は重複させない")
        return self


class InternalTelemetryConfig(_ConfigModel):
    """collector 全体の設定。既定値をコードに持たない。"""

    version: Literal[1]
    interval_ms: int = Field(gt=0)
    nvml: NvmlConfig
    hwmon: HwmonConfig

    @classmethod
    def from_yaml(cls, path: Path) -> InternalTelemetryConfig:
        """YAML を厳格に読み、未知キーや不足を起動前に拒否する。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Internal Telemetry 設定が辞書ではない: {path}")
        return cls.model_validate(loaded)
