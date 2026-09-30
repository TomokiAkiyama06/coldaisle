"""エアフロー画面（#106）の表示設定。決定記録 0046 / 0071 §2.6。

**表示専用の設定だけを持つ。** 制御・アラートの閾値ではない。
空気の温度の色分けの区切りと、decision trace を「古い」と言う倍数を `config/airflow-ui.yaml`
から読み、`GET /api/v1/airflow/config` でそのまま返す（AGENTS.md ルール9: 値をコードに書かない）。
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator

from coldaisle.internal_telemetry import SourceStatus

AIR_TEMPERATURE_BANDS = 5
"""色の段数（青→琥珀→赤）。区切りはこれより1つ少ない。

段数は配色（画面側）と対になっているため設定にしない。変えるのは区切りの値だけ。
"""


class AirTemperatureScale(BaseModel):
    """空気の温度の色分け。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thresholds_c: tuple[FiniteFloat, ...] = Field(
        min_length=AIR_TEMPERATURE_BANDS - 1, max_length=AIR_TEMPERATURE_BANDS - 1
    )
    """段の境目（℃）。有限の値で狭義の昇順。`t < thresholds_c[0]` が最も低い段。

    **NaN・無限大は各要素の型（FiniteFloat）で昇順の判定より前に弾く。** NaN はどの比較も
    偽になり昇順の判定を素通りし、応答の JSON 化で初めて落ちるため（Codex P2）。
    """
    provisional: bool
    """区切りが仮の値か。画面の凡例に「区切りは仮」と出す。"""

    @field_validator("thresholds_c")
    @classmethod
    def _ascending(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        # 逆順や重複があると、ある温度がどの色になるかが読めなくなる
        if any(later <= earlier for earlier, later in pairwise(value)):
            raise ValueError(f"thresholds_c は狭義の昇順で書く: {list(value)}")
        return value


class ControlTraceFreshness(BaseModel):
    """decision trace の古さの判定（決定記録 0071 §2.6 / §5 #1）。

    **判定は画面が行う。** API は `age_ms` を返すだけで、`safety.yaml` も読まない
    （読み取り API が制御の設定に依存しない。0071 §4 K）。周期は trace 自身
    （v8 以降の `runtime.tick_period_ms`）が持ち、ここはその何倍で「古い」と言うかだけを持つ。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_tick_periods: FiniteFloat = Field(gt=1.0)
    """`age_ms > tick_period_ms × この値` なら「古い」。

    1 以下を拒む。1 以下だと、周期どおりに記録していても tick の直前には毎回「古い」になる。
    """
    provisional: bool
    """仮の値か（0071 §5 #1 は値を実装 PR に委ねた）。

    出荷時の 3.0 は 2026-09-30 にオーナーが確定した（`false`）。
    """


class AirflowUiSettings(BaseModel):
    """`config/airflow-ui.yaml` 全体。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    air_temperature: AirTemperatureScale
    control_trace: ControlTraceFreshness

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


class CpuUtilizationOut(BaseModel):
    """`GET /api/v1/airflow/config` の `cpu_utilization`（#145 / 決定記録 0047）。"""

    measured: bool | None
    """collector が CPU 使用率を収集しているか。偽なら画面は「未計測」、null は分からない。

    collector が保存した `sys.telemetry_source.proc_stat` の状態から決める（決定記録 0051）。
    測定値ではない。
    """


class ControlTraceFreshnessOut(BaseModel):
    """`GET /api/v1/airflow/config` の `control_trace`（決定記録 0071 §2.6）。"""

    stale_after_tick_periods: float
    """`/control/latest` の `age_ms` が trace の周期のこの倍を超えたら古い。

    周期は trace 自身の `runtime.tick_period_ms`（v8 以降）。
    """
    provisional: bool


class AirflowConfigResponse(BaseModel):
    """`GET /api/v1/airflow/config`。**値（測定値）は含まない。**"""

    schema_version: Literal[1] = 1
    air_temperature: AirTemperatureScaleOut
    cpu_utilization: CpuUtilizationOut
    control_trace: ControlTraceFreshnessOut


def cpu_utilization_measured(state: str | None) -> bool | None:
    """collector が保存した proc_stat の状態 → `cpu_utilization.measured`（決定記録 0051）。

    - `disabled`（`proc_stat.enabled: false`）→ False（未計測）
    - `ok` / `degraded` → True（収集している。値が無ければ画面は「未取得」）
    - `unavailable`・状態が無い・知らない値 → None（分からない）。`unavailable` は
      Linux 以外（決定記録 0047 §2.2）と一時的な読み取り失敗の両方で、
      保存された状態からは区別できない
    """
    if state == SourceStatus.DISABLED:
        return False
    if state in (SourceStatus.OK, SourceStatus.DEGRADED):
        return True
    return None


def airflow_config_payload(
    settings: AirflowUiSettings, *, cpu_utilization_measured: bool | None
) -> AirflowConfigResponse:
    """設定をそのまま応答の形にする。"""
    return AirflowConfigResponse(
        air_temperature=AirTemperatureScaleOut(
            thresholds_c=list(settings.air_temperature.thresholds_c),
            provisional=settings.air_temperature.provisional,
        ),
        cpu_utilization=CpuUtilizationOut(measured=cpu_utilization_measured),
        control_trace=ControlTraceFreshnessOut(
            stale_after_tick_periods=settings.control_trace.stale_after_tick_periods,
            provisional=settings.control_trace.provisional,
        ),
    )
