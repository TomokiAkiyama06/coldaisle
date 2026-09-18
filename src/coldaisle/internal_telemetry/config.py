"""Internal Telemetry Collector の設定。#65"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.store.db import MINUTE_MS
from coldaisle.store.models import validate_metric

CONNECTOR_TEMPERATURE_METRIC = "board.connector_12v2x6"
"""ASUS T_SENSOR で測る 12V-2x6 コネクタ外装温度のメトリクス名（決定記録0032）。"""


class _ConfigModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConfirmationStatus(StrEnum):
    """実機対応・較正の確認状態。confirmed は所有者承認済みを表す。"""

    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"


class ConfirmationEvidence(_ConfigModel):
    """実機での確認と所有者承認を追跡する証跡。"""

    status: ConfirmationStatus
    basis: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _confirmed_requires_a_basis(self) -> Self:
        if self.status is ConfirmationStatus.CONFIRMED and self.basis is None:
            raise ValueError("confirmed には測定記録と所有者承認の basis が必要")
        return self


class NvmlConfig(_ConfigModel):
    """NVML adapter の設定。v1 は単一 GPU の論理 index 0 だけを扱う。"""

    enabled: bool
    gpu_indices: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _indices_are_unique_and_non_negative(self) -> Self:
        if any(index < 0 for index in self.gpu_indices):
            raise ValueError("gpu_indices は 0 以上にする")
        if len(set(self.gpu_indices)) != len(self.gpu_indices):
            raise ValueError("gpu_indices は重複させない")
        if self.gpu_indices != (0,):
            raise ValueError(
                "v1 は単一 GPU の logical index 0 だけを扱う; "
                "複数 GPU には承認済みの物理 GPU 対応が必要"
            )
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
    confirmation: ConfirmationEvidence | None = None
    disabled_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _has_a_stable_selector_and_valid_range(self) -> Self:
        validate_metric(self.metric)
        if self.enabled and not self.driver:
            raise ValueError("有効な hwmon sensor には driver が必要")
        if self.enabled and self.disabled_reason is not None:
            raise ValueError("有効な hwmon sensor に disabled_reason を指定しない")
        if not self.enabled and self.disabled_reason is None:
            raise ValueError("無効な hwmon sensor には disabled_reason が必要")
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
        if self.enabled and (
            self.confirmation is None
            or self.confirmation.status is not ConfirmationStatus.CONFIRMED
        ):
            raise ValueError(
                "hwmon sensor を有効にするには、物理入力の実機確認を "
                "confirmed の basis 付きで記録する"
            )
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

    @model_validator(mode="after")
    def _interval_divides_a_minute(self) -> Self:
        # ロールアップは1分あたりの期待サンプル数を 60000 // interval_ms で固定する。
        # 割り切れない周期では実際の件数が分ごとに揺れ、欠測率が負になりうる
        if MINUTE_MS % self.interval_ms != 0:
            raise ValueError(f"interval_ms は {MINUTE_MS} を割り切る値にする")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> InternalTelemetryConfig:
        """YAML を厳格に読み、未知キーや不足を起動前に拒否する。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Internal Telemetry 設定が辞書ではない: {path}")
        return cls.model_validate(loaded)
