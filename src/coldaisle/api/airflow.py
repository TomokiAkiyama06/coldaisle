"""エアフロー画面（#106）の表示設定。決定記録 0044。

**表示専用の設定だけを持つ。** 制御・アラートの閾値ではない。
空気の温度の色分けの区切りを `config/airflow-ui.yaml` から読み、
`GET /api/v1/airflow/config` でそのまま返す（AGENTS.md ルール9: 区切りをコードに書かない）。
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

AIR_TEMPERATURE_BANDS = 5
"""色の段数（青→琥珀→赤）。区切りはこれより1つ少ない。

段数は配色（画面側）と対になっているため設定にしない。変えるのは区切りの値だけ。
"""


class AirTemperatureScale(BaseModel):
    """空気の温度の色分け。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thresholds_c: tuple[float, ...] = Field(
        min_length=AIR_TEMPERATURE_BANDS - 1, max_length=AIR_TEMPERATURE_BANDS - 1
    )
    """段の境目（℃）。狭義の昇順。`t < thresholds_c[0]` が最も低い段。"""
    provisional: bool
    """区切りが仮の値か。画面の凡例に「区切りは仮」と出す。"""

    @field_validator("thresholds_c")
    @classmethod
    def _ascending(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        # 逆順や重複があると、ある温度がどの色になるかが読めなくなる
        if any(later <= earlier for earlier, later in pairwise(value)):
            raise ValueError(f"thresholds_c は狭義の昇順で書く: {list(value)}")
        return value


class AirflowUiSettings(BaseModel):
    """`config/airflow-ui.yaml` 全体。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    air_temperature: AirTemperatureScale

    @classmethod
    def from_yaml(cls, path: Path) -> AirflowUiSettings:
        """YAML を厳格に読む。壊れていれば API の起動時に失敗させる。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"エアフロー画面の設定が辞書ではない: {path}")
        return cls.model_validate(loaded)


class AirTemperatureScaleOut(BaseModel):
    """`GET /api/v1/airflow/config` の `air_temperature`。"""

    unit: Literal["C"] = "C"
    thresholds_c: list[float]
    provisional: bool


class AirflowConfigResponse(BaseModel):
    """`GET /api/v1/airflow/config`。**値（測定値）は含まない。**"""

    schema_version: Literal[1] = 1
    air_temperature: AirTemperatureScaleOut


def airflow_config_payload(settings: AirflowUiSettings) -> AirflowConfigResponse:
    """設定をそのまま応答の形にする。"""
    return AirflowConfigResponse(
        air_temperature=AirTemperatureScaleOut(
            thresholds_c=list(settings.air_temperature.thresholds_c),
            provisional=settings.air_temperature.provisional,
        )
    )
