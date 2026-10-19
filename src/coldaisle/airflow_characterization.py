"""合成の起点: 回転数の掃引と応答の行列の解析（#75 / 決定記録 0153）。

```bash
uv run coldaisle-airflow-characterization \\
  --run var/airflow-characterization/rpm-front-1.yaml --run ... \\
  --format markdown --out var/airflow-characterization/report.md
```

**読み取りだけ。** run の manifest（§2.9）と、そこに書かれた **checkpoint 済みの DB の写し**
（§2.7 の 2）を読み、RPM の表の候補・応答の行列・再現性を JSON か Markdown で 1 つ書く。
DB・設定・Control Config を書かない。Fan を動かさない（測定の実行は所有者の手。0153 §2.2）。
control・AI 層・API はこの module を import しない（§2.7 の 2。CI が AST で見張る）。
結果を LLM のプロンプトへ入れない（ルール 1 / 8）。

- DB の写しは `evaluate.EvidenceDatabase`（`mode=ro` より強い `immutable=1`）で開く。
  空でない `-wal` / `-journal` は拒否する。開く前に SHA-256 を manifest と照合し、違えば拒否する
- 写しの telemetry と `control_traces` の最後の時刻が run の終わり以上でなければ拒否する
- 値を計算する前に窓の覆い（§2.3）を確かめ、満たさない窓は理由を付けて捨てる
- `airflow_index` は RPM の比による代用で、風量を測った値ではない（§2.1）。応答の行列も風量ではない

**同じ入力からは同じ bytes を出す。** 生成時刻・パス・ホスト名を持たない（ルール 10）。

## run の manifest（YAML。0153 §2.9 の形をここで固定する）

```yaml
schema_version: 1
run_id: rpm-front-2026-10-20-a      # #83 の source_run_id に使える形
kind: rpm_sweep                     # または response_matrix
control_config_sha256:              # trace の runtime.config と突き合わせる
  fan_hardware: <64 桁>
  safety: <64 桁>
  policy: <64 桁>
  air_balance: <64 桁>
calibration_offsets_sha256: <64 桁>  # response_matrix では必須（区間の開始で有効な offsets_sha256）
db_copy:                            # manifest のディレクトリからの相対の名前（絶対パスを書かない）
  file: coldaisle-copy.db
  sha256: <64 桁>
  created_at_ms: 1787616000000
load:                               # response_matrix だけ
  kind: gpu                         # gpu / cpu / both
  gpu_power_limit_w: 300            # GPU を含む負荷だけ（設定の load.gpu_power_limit_w と一致）
  cpu_workers: null                 # CPU を含む負荷だけ
  pilot: "予備の試行の結果"
steps:                              # 時刻の順・重ならない
  - zone: front                     # rpm_sweep: 動かす zone・回・方向・段
    repetition: 1
    direction: up
    level: 0.35
    # response_matrix は zone / repetition / direction / level の代わりに label
    # （R / F+ / F- / Rr+ / Rr- / T+ / T- / All+）
    commanded: {front: 0.35, rear: 0.60, top: 0.60}
    command_id: 12                  # coldaisle-control manual の command_id（trace）
    lease_s: 300
    start_ms: 1787616000000
    end_ms: 1787616120000
    discarded: null                 # 人が捨てた段: {reason: aborted | safety_fault, note: "..."}
```
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sqlite3
import statistics
import sys
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, Literal, Self
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from coldaisle import logs
from coldaisle.evaluate import (
    EvidenceDatabase,
    EvidenceDatabaseError,
    EvidenceOutputError,
    refuse_output_on_inputs,
)
from coldaisle.store import migrations
from coldaisle.store.calibration_history import SELECT_ROWS as CALIBRATION_SELECT_ROWS
from coldaisle.store.calibration_history import (
    CalibrationActivation,
    CalibrationHistoryError,
    verify_activation_rows,
)

LOGGER = logging.getLogger("coldaisle.airflow_characterization")

REPORT_VERSION: Final = 1
"""出力の JSON の形の版。"""

ZONES: Final = ("front", "rear", "top")

SUBSTITUTE_BASIS: Final = "RPM の比による代用。風量を測った値ではない（0153）"
"""`airflow_index` の `basis`（§2.1）。報告・画面で「測った風量」と書かない。"""

NOTES: Final = (
    "airflow_index は RPM の比による代用で、風量を測った値ではない（決定記録 0153 §2.1）",
    "応答の行列は風量ではない。運転のままの系の応答で、zone ごとの純粋な効果ではない"
    "（§2.1 / §2.13）",
    "air-balance.yaml は uncalibrated のまま。本報告だけでは calibrated にしない（§2.1）",
    "設定へは写さない。承認は新しい決定記録で所有者が行う（§2.7 の 4）",
    "結果を LLM のプロンプトへ入れない（AGENTS.md ルール 1 / 8）",
)

_SQRT12: Final = math.sqrt(12.0)


class Reason(StrEnum):
    """段・窓・組・回を捨てた理由（0153 §2.3 / §2.9 の一覧）。"""

    FLOOR_BOUND = "floor_bound"
    UNSETTLED = "unsettled"
    ABORTED = "aborted"
    SAFETY_FAULT = "safety_fault"
    ROOM_DRIFT = "room_drift"
    CONDITION_MISMATCH = "condition_mismatch"
    WINDOW_TREND = "window_trend"
    CONFIG_MISMATCH = "config_mismatch"
    POWER_MISMATCH = "power_mismatch"
    CALIBRATION_CHANGED = "calibration_changed"
    TELEMETRY_COVERAGE = "telemetry_coverage"
    TRACE_COVERAGE = "trace_coverage"
    RPM_COVERAGE = "rpm_coverage"


class TableStatus(StrEnum):
    """段・zone・負荷のまとめが値を出さなかった理由（§2.4 / §2.5 の条件。§2.9 の一覧の外）。"""

    INSUFFICIENT_REPETITIONS = "insufficient_repetitions"
    """有効な回が `repetitions` に足りない、または有効な観測の無い回・方向がある。"""
    INSUFFICIENT_DAYS = "insufficient_days"
    """有効な回が `min_distinct_days` 日にまたがらない。"""
    RPM_SPREAD = "rpm_spread"
    """範囲 ÷ 中央値の上側の境界が `approval.max_rpm_spread` を超える（§2.6）。"""
    LEVEL_MISSING = "level_missing"
    """9 段のどれかが値を出せない（§2.4。zone の表を出さない）。"""
    NOT_MONOTONIC = "not_monotonic"
    """段の中央値が demand に対して下がり、`FanProfile` の検証を通らない。"""
    TOO_MANY_REPETITIONS = "too_many_repetitions"
    """回が `repetitions` より多い（掃引は §2.4 の 3 回、応答の行列は §2.6 の 2 回の式）。"""
    LOAD_MISMATCH = "load_mismatch"
    """まとめる回の負荷の設定（電力の上限・worker の数）が違う（§2.5）。"""


class AnalysisError(RuntimeError):
    """解析を拒否した（設定・manifest・DB の写しの不整合）。**何も出力しない。**"""


# ---------------------------------------------------------------------- 設定


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


PosFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Ratio = Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
DemandValue = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
PosInt = Annotated[int, Field(gt=0)]
NonNegInt = Annotated[int, Field(ge=0)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Param[T](_Frozen):
    """解析の設定の 1 つの値（§2.8。`status` と `basis` を必ず持つ）。"""

    value: T
    status: Literal["provisional", "confirmed"]
    basis: str = Field(min_length=1, max_length=500)


class Resolution(_Frozen):
    """分解能。**未定（`undetermined`）は `value: null`**。未定の値を使う解析は失敗する（§2.6）。"""

    value: PosFloat | None
    status: Literal["provisional", "confirmed", "undetermined"]
    basis: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _null_iff_undetermined(self) -> Self:
        if (self.value is None) != (self.status == "undetermined"):
            raise ValueError("value が null なのは status: undetermined のときだけ")
        return self


class RpmSweepConfig(_Frozen):
    levels: Param[tuple[DemandValue, ...]]
    other_zones_demand: Param[DemandValue]
    settle_s: Param[PosInt]
    record_s: Param[PosInt]
    repetitions: Param[PosInt]
    min_distinct_days: Param[PosInt]
    min_rpm_ratio: Param[Ratio]
    max_rpm_gap_ms: Param[PosInt]
    response_band: Param[Ratio]
    tach_update_ms: Param[NonNegInt]
    maximum_rpm_margin: Param[NonNegFloat]
    maximum_rpm_round_up: Param[PosInt]

    @model_validator(mode="after")
    def _levels_increase(self) -> Self:
        levels = self.levels.value
        if len(levels) < 2 or any(later <= earlier for earlier, later in pairwise(levels)):
            raise ValueError("rpm_sweep.levels は 2 つ以上・重複なしの昇順にする")
        if self.repetitions.value < self.min_distinct_days.value:
            raise ValueError("rpm_sweep.min_distinct_days は repetitions 以下にする")
        return self


class IdleConfig(_Frozen):
    cpu_package_c: Param[float]
    gpu_core_c: Param[float]
    gpu_power_w: Param[float]


class WindowConfig(_Frozen):
    length_s: Param[PosInt]
    max_settle_s: Param[PosInt]


class SettleConfig(_Frozen):
    span_s: Param[PosInt]
    max_range_air_c: Param[PosFloat]
    max_range_chip_c: Param[PosFloat]


class TrendConfig(_Frozen):
    temperature_c: Param[PosFloat]
    power_w: Param[PosFloat]


class ResolutionConfig(_Frozen):
    air_c: Resolution
    power_w: Resolution
    gpu_core_c: Resolution
    gpu_hotspot_c: Resolution
    cpu_package_c: Resolution
    board_connector_c: Resolution
    fan_rpm: Resolution


class LoadConfig(_Frozen):
    gpu_power_limit_w: Param[PosFloat]


class ResponseMatrixConfig(_Frozen):
    reference_demand: Param[DemandValue]
    plus_demand: Param[DemandValue]
    minus_demand: Param[DemandValue]
    repetitions: Param[PosInt]
    min_distinct_days: Param[PosInt]
    max_power_delta_w: Param[PosFloat]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.minus_demand.value < self.reference_demand.value < self.plus_demand.value:
            raise ValueError("response_matrix は minus < reference < plus にする")
        if self.repetitions.value < self.min_distinct_days.value:
            raise ValueError("response_matrix.min_distinct_days は repetitions 以下にする")
        return self


class ApprovalConfig(_Frozen):
    max_rpm_spread: Param[Ratio]


class SourceCoverage(_Frozen):
    metrics: tuple[str, ...] = Field(min_length=1)
    min_ok_ratio: Param[Ratio]
    period_ms: Param[PosInt]
    max_sample_gap_ms: Param[PosInt]


class CoverageConfig(_Frozen):
    board: SourceCoverage
    telemetry: SourceCoverage
    cpu_power: SourceCoverage
    trace_max_gap_ratio: Param[PosFloat]
    min_trace_ticks_ratio: Param[Ratio]


class AirflowCharacterizationConfig(_Frozen):
    """`config/airflow-characterization.yaml`（0153 §2.8）。**既定値を持たない**（ルール 9）。"""

    version: Literal[1]
    timezone: str
    bound_coverage_factor: Param[PosFloat]
    rpm_sweep: RpmSweepConfig
    idle: IdleConfig
    window: WindowConfig
    window_blocks: Param[Annotated[int, Field(ge=2)]]
    settle: SettleConfig
    max_window_trend: TrendConfig
    max_room_drift_c: Param[PosFloat]
    max_aux_fan_delta_rpm: Param[PosFloat]
    resolution: ResolutionConfig
    load: LoadConfig
    response_matrix: ResponseMatrixConfig
    approval: ApprovalConfig
    coverage: CoverageConfig

    @model_validator(mode="after")
    def _shape(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except Exception as error:
            raise ValueError(f"タイムゾーンが不正: {self.timezone!r}") from error
        if self.window.length_s.value > self.settle.span_s.value:
            # 値は落ち着きの区間の「最後の 2 分」（§2.5）なので、区間より長くできない
            raise ValueError("window.length_s は settle.span_s 以下にする")
        if self.window.max_settle_s.value < self.settle.span_s.value:
            raise ValueError("window.max_settle_s は settle.span_s 以上にする")
        seen: dict[str, str] = {}
        for name, source in self.sources.items():
            for metric in source.metrics:
                if metric in seen:
                    raise ValueError(f"coverage の metric が 2 つの source にある: {metric!r}")
                seen[metric] = name
        missing = sorted(set(REQUIRED_METRICS) - set(seen))
        if missing:
            raise ValueError(f"coverage にどの source にも無い metric がある: {missing}")
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def k(self) -> float:
        return self.bound_coverage_factor.value

    @property
    def sources(self) -> dict[str, SourceCoverage]:
        return {
            "board": self.coverage.board,
            "telemetry": self.coverage.telemetry,
            "cpu_power": self.coverage.cpu_power,
        }

    def source_of(self, metric: str) -> SourceCoverage:
        for source in self.sources.values():
            if metric in source.metrics:
                return source
        raise AnalysisError(f"coverage に source の無い metric: {metric!r}")

    def resolution_of(self, key: str) -> float:
        """分解能の値。**未定なら解析を拒否する**（§2.6「欠ければ何も出さずに失敗する」）。"""
        resolution: Resolution = getattr(self.resolution, key)
        if resolution.value is None:
            raise AnalysisError(
                f"resolution.{key} が未定（{resolution.basis}）。"
                "実機で値の刻みを確かめて設定してから解析する"
            )
        return resolution.value

    def undetermined(self) -> list[str]:
        return [
            f"resolution.{name}"
            for name, item in self.resolution
            if isinstance(item, Resolution) and item.value is None
        ]

    @classmethod
    def from_bytes(cls, payload: bytes) -> AirflowCharacterizationConfig:
        loaded: Any = yaml.safe_load(payload.decode("utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("airflow-characterization の設定が辞書ではない")
        return cls.model_validate_json(_as_json_text(loaded), strict=True)


def _as_json_text(loaded: object) -> str:
    """YAML を JSON にしてから strict で検証する（load_distribution.py と同じ。型の違いを拒む）。"""
    try:
        return json.dumps(loaded, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"YAML に JSON で表せない値がある: {error}") from error


# ---------------------------------------------------------------------- 指標（§2.5）


@dataclass(frozen=True)
class Indicator:
    """応答の行列の指標。`subtrahend` があれば `air.front_intake` との差（窓の平均の差）。"""

    name: str
    minuend: str
    subtrahend: str | None
    resolution: str
    kind: Literal["temperature", "power"]

    def u_res(self, config: AirflowCharacterizationConfig) -> float:
        """§2.6 の `u_res`。差の温度は 2 本 × L と基準で √4、単独の値は L と基準で √2。"""
        base = config.resolution_of(self.resolution) / _SQRT12
        return base * (2.0 if self.subtrahend is not None else math.sqrt(2.0))


INDICATORS: Final = (
    Indicator("air.gpu_intake", "air.gpu_intake", None, "air_c", "temperature"),
    Indicator("d.gpu_preheat", "air.gpu_intake", "air.front_intake", "air_c", "temperature"),
    Indicator("d.case_delta", "air.rear_exhaust", "air.front_intake", "air_c", "temperature"),
    Indicator(
        "air.gpu_exhaust-air.front_intake",
        "air.gpu_exhaust",
        "air.front_intake",
        "air_c",
        "temperature",
    ),
    Indicator(
        "air.top_exhaust-air.front_intake",
        "air.top_exhaust",
        "air.front_intake",
        "air_c",
        "temperature",
    ),
    Indicator("gpu.0.core", "gpu.0.core", None, "gpu_core_c", "temperature"),
    Indicator("gpu.0.hotspot", "gpu.0.hotspot", None, "gpu_hotspot_c", "temperature"),
    Indicator("cpu.package", "cpu.package", None, "cpu_package_c", "temperature"),
)
"""§2.5 の見る指標。`air.gpu_intake` はそのままの値と `air.front_intake` との差（`d.gpu_preheat`）の
両方を持つ。`air.rear_exhaust` との差は
`d.case_delta`（`config/metrics.yaml` の定義と同じ引き算）。"""

POWER_INDICATORS: Final = (
    Indicator("power.cpu.package", "power.cpu.package", None, "power_w", "power"),
    Indicator("power.gpu.0", "power.gpu.0", None, "power_w", "power"),
)
"""§2.5 の実測の熱の入力。電力の差で正規化しない。"""

SETTLE_AIR: Final = (
    "air.front_intake",
    "air.gpu_intake",
    "air.gpu_exhaust",
    "air.top_exhaust",
    "air.rear_exhaust",
)
SETTLE_CHIP: Final = (
    ("cpu.package", "cpu_package_c"),
    ("gpu.0.core", "gpu_core_c"),
    ("board.connector_12v2x6", "board_connector_c"),
)
"""§2.5 の落ち着きを見る metric。

チップは `max_range_chip_c` と比べ、`cpu.package` は 3 点の中央値の列の幅で見る。
"""

ROOM: Final = "air.room"
PUMP: Final = "fan.aio_pump.rpm"
IDLE_METRICS: Final = (
    ("cpu.package", "cpu_package_c"),
    ("gpu.0.core", "gpu_core_c"),
    ("power.gpu.0", "gpu_power_w"),
)
"""§2.4 のアイドルの条件（metric と `idle.*` の鍵）。"""

MATRIX_VALUE_METRICS: Final = tuple(
    dict.fromkeys(
        [
            *SETTLE_AIR,
            ROOM,
            *(metric for metric, _ in SETTLE_CHIP),
            "gpu.0.hotspot",
            "power.cpu.package",
            "power.gpu.0",
            PUMP,
        ]
    )
)
"""応答の行列の値の窓で覆いを求める metric（§2.3「その窓で使う metric のすべて」）。"""

RECORD_ONLY: Final = ("air.room_humidity", "gpu.0.fan_speed", "fan.vrm.rpm")
"""残すだけの metric（§2.5）。検査には使わないが、値の窓では覆いを求める（欠ければ窓を捨てる）。"""

THROTTLE_METRICS: Final = (
    "gpu.0.throttle.hw_slowdown",
    "gpu.0.throttle.hw_thermal",
    "gpu.0.throttle.sw_thermal",
    "gpu.0.throttle.hw_power_brake",
    "gpu.0.throttle.sw_power_cap",
)
"""捨てた組でも報告に残す throttle の flag（§2.5）。"""

REQUIRED_METRICS: Final = tuple(
    dict.fromkeys([*MATRIX_VALUE_METRICS, *(metric for metric, _ in IDLE_METRICS), *RECORD_ONLY])
)

LABELS: Final = ("R", "F+", "F-", "Rr+", "Rr-", "T+", "T-", "All+")
"""応答の行列の段（§2.5）。`R` は基準の段。"""

MATRIX_SEQUENCE: Final = (
    "R",
    "F+",
    "R",
    "F-",
    "R",
    "Rr+",
    "R",
    "Rr-",
    "R",
    "T+",
    "R",
    "T-",
    "R",
    "All+",
    "R",
)
"""§2.5 の段の並び（基準の段を間に挟む）。manifest はこの並びだけを受ける。"""

_LABEL_ZONE: Final = {"F": "front", "Rr": "rear", "T": "top"}


def expected_commanded(label: str, config: ResponseMatrixConfig) -> dict[str, float]:
    """段の名前から 3 zone の指示の値（§2.5）。"""
    reference = config.reference_demand.value
    if label == "R":
        return dict.fromkeys(ZONES, reference)
    if label == "All+":
        return dict.fromkeys(ZONES, config.plus_demand.value)
    zone = _LABEL_ZONE[label[:-1]]
    values = dict.fromkeys(ZONES, reference)
    values[zone] = config.plus_demand.value if label.endswith("+") else config.minus_demand.value
    return values


# ---------------------------------------------------------------------- manifest（§2.9）


class ConfigHashes(_Frozen):
    fan_hardware: Sha256
    safety: Sha256
    policy: Sha256
    air_balance: Sha256

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.fan_hardware, self.safety, self.policy, self.air_balance)


class DbCopy(_Frozen):
    """DB の写し（§2.7 の 2）。`file` は manifest のディレクトリからの相対の名前。"""

    file: str = Field(min_length=1, max_length=255)
    sha256: Sha256
    created_at_ms: NonNegInt

    @model_validator(mode="after")
    def _relative(self) -> Self:
        path = PurePosixPath(self.file)
        if path.is_absolute() or ".." in path.parts or "\\" in self.file or ":" in self.file:
            # 絶対パスは個体の配置を出す（ルール 10）。外へ出る相対パスも受けない
            raise ValueError("db_copy.file は manifest のディレクトリの中の相対の名前にする")
        return self


class Discard(_Frozen):
    """人が捨てた段（中止の基準・Critical Safety の状態。§2.2）。"""

    reason: Literal["aborted", "safety_fault"]
    note: str = Field(min_length=1, max_length=500)


class Commanded(_Frozen):
    front: DemandValue
    rear: DemandValue
    top: DemandValue

    def get(self, zone: str) -> float:
        value: float = getattr(self, zone)
        return value


class _Step(_Frozen):
    commanded: Commanded
    command_id: PosInt
    lease_s: PosInt
    start_ms: NonNegInt
    end_ms: NonNegInt
    discarded: Discard | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("段は end_ms > start_ms にする")
        return self


class SweepStep(_Step):
    zone: Literal["front", "rear", "top"]
    repetition: PosInt
    direction: Literal["up", "down"]
    level: DemandValue


class MatrixStep(_Step):
    label: Literal["R", "F+", "F-", "Rr+", "Rr-", "T+", "T-", "All+"]


class LoadDefinition(_Frozen):
    kind: Literal["gpu", "cpu", "both"]
    gpu_power_limit_w: PosFloat | None
    cpu_workers: PosInt | None
    pilot: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _matches_kind(self) -> Self:
        if (self.gpu_power_limit_w is not None) != (self.kind in ("gpu", "both")):
            raise ValueError("gpu_power_limit_w は GPU を含む負荷だけに書く")
        if (self.cpu_workers is not None) != (self.kind in ("cpu", "both")):
            raise ValueError("cpu_workers は CPU を含む負荷だけに書く")
        return self


RunId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")]


class _Manifest(_Frozen):
    schema_version: Literal[1]
    run_id: RunId
    control_config_sha256: ConfigHashes
    db_copy: DbCopy


def _steps_in_order(steps: Sequence[_Step]) -> None:
    for earlier, later in pairwise(steps):
        if later.start_ms < earlier.end_ms:
            raise ValueError("steps は時刻の順で、重ならないようにする")


class SweepManifest(_Manifest):
    kind: Literal["rpm_sweep"]
    calibration_offsets_sha256: Sha256 | None = None
    steps: tuple[SweepStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _order(self) -> Self:
        _steps_in_order(self.steps)
        return self


class MatrixManifest(_Manifest):
    kind: Literal["response_matrix"]
    calibration_offsets_sha256: Sha256
    load: LoadDefinition
    steps: tuple[MatrixStep, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def _bracketed(self) -> Self:
        _steps_in_order(self.steps)
        labels = tuple(step.label for step in self.steps)
        if labels != MATRIX_SEQUENCE:
            # 同じ段を 2 度置いた回を 2 回分として数えない。中止した段も並びに残し discarded で示す
            raise ValueError(
                "応答の行列の段は §2.5 の並び（" + ", ".join(MATRIX_SEQUENCE) + "）にする。"
                "動かした段は基準の段（R）で挟む"
            )
        return self


Manifest = SweepManifest | MatrixManifest
_MANIFEST: Final = TypeAdapter[Manifest](
    Annotated[SweepManifest | MatrixManifest, Field(discriminator="kind")]
)


@dataclass(frozen=True)
class LoadedRun:
    manifest: Manifest
    manifest_sha256: str
    db_path: Path

    @property
    def start_ms(self) -> int:
        return self.manifest.steps[0].start_ms

    @property
    def end_ms(self) -> int:
        return self.manifest.steps[-1].end_ms


def load_manifest(path: Path) -> LoadedRun:
    payload = path.read_bytes()
    loaded: Any = yaml.safe_load(payload.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("manifest が辞書ではない")
    manifest = _MANIFEST.validate_json(_as_json_text(loaded), strict=True)
    return LoadedRun(
        manifest=manifest,
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        db_path=path.parent / manifest.db_copy.file,
    )


def validate_against_config(run: LoadedRun, config: AirflowCharacterizationConfig) -> None:
    """manifest の段が設定の段・指示の値と合うか。合わなければ解析を拒否する。"""
    manifest = run.manifest
    name = manifest.run_id
    if isinstance(manifest, MatrixManifest):
        for matrix_step in manifest.steps:
            expected = expected_commanded(matrix_step.label, config.response_matrix)
            if any(matrix_step.commanded.get(zone) != expected[zone] for zone in ZONES):
                raise AnalysisError(f"{name}: 段 {matrix_step.label} の指示の値が設定と違う")
        limit = manifest.load.gpu_power_limit_w
        if limit is not None and limit != config.load.gpu_power_limit_w.value:
            raise AnalysisError(f"{name}: GPU の電力の上限が設定（load.gpu_power_limit_w）と違う")
        return
    sweep = config.rpm_sweep
    levels = sweep.levels.value
    top = levels[-1]
    per_rep: dict[tuple[str, int], list[SweepStep]] = defaultdict(list)
    for step in manifest.steps:
        if step.level not in levels:
            raise AnalysisError(f"{name}: 設定に無い段 {step.level}")
        for zone in ZONES:
            want = step.level if zone == step.zone else sweep.other_zones_demand.value
            if step.commanded.get(zone) != want:
                raise AnalysisError(f"{name}: {step.zone} {step.level} の {zone} の指示の値が違う")
        per_rep[(step.zone, step.repetition)].append(step)
    for (zone, repetition), steps in per_rep.items():
        ups = [step.level for step in steps if step.direction == "up"]
        downs = [step.level for step in steps if step.direction == "down"]
        label = f"{name}: {zone} の {repetition} 回目"
        if any(later <= earlier for earlier, later in pairwise(ups)):
            raise AnalysisError(f"{label}: 上りの段が昇順でない")
        if any(later >= earlier for earlier, later in pairwise(downs)):
            raise AnalysisError(f"{label}: 下りの段が降順でない")
        directions = [step.direction for step in steps]
        if "down" in directions and "up" in directions[directions.index("down") :]:
            raise AnalysisError(f"{label}: 上りの後に下りを行う")
        if top in ups and top in downs:
            raise AnalysisError(f"{label}: 折り返しの段 {top} は 1 方向だけに置く")


# ---------------------------------------------------------------------- DB の写し


@dataclass(frozen=True)
class Row:
    ts_ms: int
    value: float | None
    quality: str


@dataclass(frozen=True)
class ZoneTick:
    effective: float | None
    applied: float | None
    rpm: int | None
    write_ok: bool
    readback_ok: bool


@dataclass(frozen=True)
class Tick:
    """§2.3 の検査に使う trace の欄だけ（版の違いで欠けた欄は None）。"""

    seq: int
    ts_ms: int
    tick_id: int
    run_id: str | None
    period_ms: int | None
    config: tuple[str | None, str | None, str | None, str | None]
    mode: str | None
    command_id: int | None
    safety: str | None
    faults: int
    zones: dict[str, ZoneTick]


def _dig(obj: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def parse_tick(seq: int, ts_ms: int, tick_id: int, trace_json: str) -> Tick:
    """trace の JSON から検査の欄を取り出す。壊れた JSON は検査に通らない tick にする。"""
    try:
        body: object = json.loads(trace_json)
    except ValueError:
        body = {}
    zones: dict[str, ZoneTick] = {}
    for zone in ZONES:
        record = _dig(body, "zones", zone)
        hardware = _dig(record, "hardware")
        zones[zone] = ZoneTick(
            effective=_float(_dig(record, "demand", "effective")),
            applied=_float(_dig(record, "applied_demand")),
            rpm=_int(_dig(hardware, "rpm")),
            write_ok=_dig(hardware, "write_ok") is True,
            readback_ok=_dig(hardware, "readback_ok") is True,
        )
    config = _dig(body, "runtime", "config")
    faults = _dig(body, "faults")
    return Tick(
        seq=seq,
        ts_ms=ts_ms,
        tick_id=tick_id,
        run_id=_str(_dig(body, "run_id")),
        period_ms=_int(_dig(body, "runtime", "tick_period_ms")),
        config=(
            _str(_dig(config, "fan_hardware_sha256")),
            _str(_dig(config, "safety_sha256")),
            _str(_dig(config, "policy_sha256")),
            _str(_dig(config, "air_balance_sha256")),
        ),
        mode=_str(_dig(body, "state", "operating_mode")),
        command_id=_int(_dig(body, "mode_command", "command_id")),
        safety=_str(_dig(body, "state", "safety_state")),
        # 形の分からない faults は「故障あり」として数える（安全側）
        faults=len(faults) if isinstance(faults, list) else (0 if faults is None else 1),
        zones=zones,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CopyDatabase(EvidenceDatabase):
    """DB の写し（§2.7 の 2）。`EvidenceDatabase` の開き方に、同じ版のスキーマの要求を足す。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        known = len(migrations.discover())
        if self.schema_version != known:
            self.close()
            raise EvidenceDatabaseError(
                f"DB の写しのスキーマの版が合わない（DB={self.schema_version}; コード={known}）。"
                "解析は DB を書き換えないので移行もしない"
            )

    def rows(self, metric: str, start_ms: int, end_ms: int) -> list[Row]:
        """`[start_ms, end_ms]` の生データを時刻の順に（品質を問わない）。"""
        return [
            Row(int(ts), None if value is None else float(value), str(quality))
            for ts, value, quality in self._conn.execute(
                "SELECT ts_ms, value, quality FROM readings "
                "WHERE metric = ? AND ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms",
                (metric, start_ms, end_ms),
            )
        ]

    def ticks(self, start_ms: int, end_ms: int) -> list[Tick]:
        """`ts_ms` が `[start_ms, end_ms]` の trace と、`seq` の順でその前後の 1 つずつ。

        並びは `seq`（並べるためだけに使う。§2.3）。前後の 1 つは窓を挟む tick の候補。
        """
        query = "SELECT seq, ts_ms, tick_id, trace_json FROM control_traces "
        found = [
            parse_tick(int(seq), int(ts), int(tick), str(text))
            for seq, ts, tick, text in self._conn.execute(
                query + "WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY seq", (start_ms, end_ms)
            )
        ]
        if not found:
            return found
        before = self._conn.execute(
            query + "WHERE seq < ? ORDER BY seq DESC LIMIT 1", (found[0].seq,)
        ).fetchone()
        after = self._conn.execute(
            query + "WHERE seq > ? ORDER BY seq LIMIT 1", (found[-1].seq,)
        ).fetchone()
        head = [] if before is None else [parse_tick(*_tick_row(before))]
        tail = [] if after is None else [parse_tick(*_tick_row(after))]
        return [*head, *found, *tail]

    def last_ts(self, table: Literal["readings", "control_traces"]) -> int | None:
        row = self._conn.execute(f"SELECT MAX(ts_ms) FROM {table}").fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def calibration_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(CALIBRATION_SELECT_ROWS))


def _tick_row(row: sqlite3.Row) -> tuple[int, int, int, str]:
    return int(row[0]), int(row[1]), int(row[2]), str(row[3])


# ---------------------------------------------------------------------- 窓の統計（§2.6）


@dataclass(frozen=True)
class WindowStat:
    """窓の平均・代表の時刻・ブロックの平均・最小二乗の傾き。"""

    mean: float
    t_rep: float
    blocks: tuple[float, ...]
    slope_per_ms: float
    length_ms: int

    @property
    def trend(self) -> float:
        """`|b| × 窓の長さ`（§2.6 の `window_trend` の検査に使う）。"""
        return abs(self.slope_per_ms) * self.length_ms

    @property
    def u_win(self) -> float:
        """`sqrt(sd(ブロックの平均)^2 + u_trend^2)`。**ブロックの数で割らない**（§2.6）。"""
        sd = statistics.stdev(self.blocks)
        return math.sqrt(sd**2 + (self.trend / _SQRT12) ** 2)

    def minus(self, other: WindowStat) -> WindowStat:
        """差の指標（A − B）の窓。ブロックごとの平均の差・傾きの差。"""
        return WindowStat(
            mean=self.mean - other.mean,
            t_rep=(self.t_rep + other.t_rep) / 2.0,
            blocks=tuple(a - b for a, b in zip(self.blocks, other.blocks, strict=True)),
            slope_per_ms=self.slope_per_ms - other.slope_per_ms,
            length_ms=self.length_ms,
        )


def window_stat(
    points: Sequence[tuple[int, float]], start_ms: int, end_ms: int, blocks: int
) -> WindowStat | None:
    """`[start_ms, end_ms)` の点の統計。空のブロックがあれば求めない（None）。"""
    inside = [(ts, value) for ts, value in points if start_ms <= ts < end_ms]
    if len(inside) < 2:
        return None
    length = end_ms - start_ms
    sums = [0.0] * blocks
    counts = [0] * blocks
    for ts, value in inside:
        index = min(blocks - 1, (ts - start_ms) * blocks // length)
        sums[index] += value
        counts[index] += 1
    if any(count == 0 for count in counts):
        return None
    times = [float(ts) for ts, _ in inside]
    values = [value for _, value in inside]
    t_mean = statistics.fmean(times)
    v_mean = statistics.fmean(values)
    sxx = sum((t - t_mean) ** 2 for t in times)
    if sxx == 0:
        return None
    sxy = sum((t - t_mean) * (v - v_mean) for t, v in zip(times, values, strict=True))
    return WindowStat(
        mean=v_mean,
        t_rep=t_mean,
        blocks=tuple(total / count for total, count in zip(sums, counts, strict=True)),
        slope_per_ms=sxy / sxx,
        length_ms=length,
    )


@dataclass(frozen=True)
class Difference:
    """動かした段 L と、前後の基準の窓を内挿した値との差（§2.5 / §2.6）。"""

    value: float
    u: float
    weight: float


def interpolated_difference(
    moved: WindowStat, before: WindowStat, after: WindowStat, u_res: float
) -> Difference:
    """`Δ = x̄_L − ((1−w) x̄_1 + w x̄_2)`、`u(Δ) = sqrt(u_L² + (1−w)² u_1² + w² u_2² + u_res²)`。"""
    span = after.t_rep - before.t_rep
    if span <= 0:
        raise AnalysisError("基準の段の代表の時刻が前後で逆転している")
    weight = (moved.t_rep - before.t_rep) / span
    baseline = (1.0 - weight) * before.mean + weight * after.mean
    u = math.sqrt(
        moved.u_win**2
        + ((1.0 - weight) * before.u_win) ** 2
        + (weight * after.u_win) ** 2
        + u_res**2
    )
    return Difference(moved.mean - baseline, u, weight)


# ---------------------------------------------------------------------- 覆い（§2.3）


class Series:
    """1 つの metric の行（時刻の順）。窓の中の行を二分探索で引く。"""

    __slots__ = ("_rows", "_stamps")

    def __init__(self, rows: Sequence[Row]) -> None:
        self._rows = list(rows)
        self._stamps = [row.ts_ms for row in self._rows]

    def between(self, start_ms: int, end_ms: int) -> list[Row]:
        """`[start_ms, end_ms)` の行。"""
        return self._rows[bisect_left(self._stamps, start_ms) : bisect_left(self._stamps, end_ms)]


def telemetry_covered(
    rows: Sequence[Row], start_ms: int, end_ms: int, source: SourceCoverage
) -> bool:
    """§2.3 の metric ごとの覆い。行は**すべて** ok・数の下限・隣と両端の間の上限。"""
    if not rows or any(row.quality != "ok" or row.value is None for row in rows):
        return False
    need = math.ceil(
        source.min_ok_ratio.value * (end_ms - start_ms) / source.period_ms.value - 1e-9
    )
    if len(rows) < need:
        return False
    stamps = [start_ms, *(row.ts_ms for row in rows), end_ms]
    return all(
        later - earlier <= source.max_sample_gap_ms.value for earlier, later in pairwise(stamps)
    )


def _points(rows: Iterable[Row]) -> list[tuple[int, float]]:
    return [(row.ts_ms, row.value) for row in rows if row.value is not None]


@dataclass(frozen=True)
class TraceCheck:
    reason: Reason | None
    ticks: tuple[Tick, ...] = ()
    """窓の中の tick（挟む 2 つを含まない）。"""


def check_trace(
    ticks: Sequence[Tick],
    start_ms: int,
    end_ms: int,
    step: _Step,
    hashes: ConfigHashes,
    config: AirflowCharacterizationConfig,
) -> TraceCheck:
    """§2.3 の trace の覆い（1）〜（6）と、floor・Safety・Control Config の一致（§2.2 / §2.9）。"""
    positions = [index for index, tick in enumerate(ticks) if start_ms <= tick.ts_ms < end_ms]
    if not positions or positions != list(range(positions[0], positions[-1] + 1)):
        return TraceCheck(Reason.TRACE_COVERAGE)
    first, last = positions[0], positions[-1]
    if first == 0 or last + 1 >= len(ticks):
        return TraceCheck(Reason.TRACE_COVERAGE)
    before, after = ticks[first - 1], ticks[last + 1]
    if before.ts_ms > start_ms or after.ts_ms < end_ms:
        return TraceCheck(Reason.TRACE_COVERAGE)
    span = ticks[first - 1 : last + 2]
    inside = tuple(ticks[first : last + 1])
    run_ids = {tick.run_id for tick in span}
    periods = {tick.period_ms for tick in span}
    if len(run_ids) != 1 or None in run_ids or len(periods) != 1 or None in periods:
        return TraceCheck(Reason.TRACE_COVERAGE)
    period = periods.pop()
    assert period is not None
    if any(later.tick_id != earlier.tick_id + 1 for earlier, later in pairwise(span)):
        return TraceCheck(Reason.TRACE_COVERAGE)
    max_gap = config.coverage.trace_max_gap_ratio.value * period
    stamps = [start_ms, *(tick.ts_ms for tick in inside), end_ms]
    edges = [before.ts_ms, *stamps, after.ts_ms]
    if any(not 0 <= later - earlier <= max_gap for earlier, later in pairwise(edges)):
        return TraceCheck(Reason.TRACE_COVERAGE)
    need = math.ceil(
        config.coverage.min_trace_ticks_ratio.value * (end_ms - start_ms) / period - 1e-9
    )
    if len(inside) < need:
        return TraceCheck(Reason.TRACE_COVERAGE)
    if any(tick.config != hashes.as_tuple() for tick in span):
        return TraceCheck(Reason.CONFIG_MISMATCH)
    if any(tick.mode != "manual" or tick.command_id != step.command_id for tick in span):
        return TraceCheck(Reason.TRACE_COVERAGE)
    if any(tick.safety != "normal" or tick.faults for tick in span):
        return TraceCheck(Reason.SAFETY_FAULT)
    commanded = {zone: step.commanded.get(zone) for zone in ZONES}
    if any(tick.zones[zone].effective != commanded[zone] for tick in span for zone in ZONES):
        return TraceCheck(Reason.FLOOR_BOUND)
    for tick in span:
        for zone in ZONES:
            item = tick.zones[zone]
            if not (item.write_ok and item.readback_ok) or item.applied != commanded[zone]:
                return TraceCheck(Reason.TRACE_COVERAGE)
    return TraceCheck(None, inside)


def faulted_during(ticks: Sequence[Tick], step: _Step) -> bool:
    """段の区間の**全体**で Critical Safety が NORMAL でない・fault がある tick があったか（§2.2）。

    値の窓の外で DEGRADED / EMERGENCY になって戻った段も、段ごと捨てる。
    """
    return any(
        step.start_ms <= tick.ts_ms < step.end_ms and (tick.safety != "normal" or tick.faults)
        for tick in ticks
    )


def step_interval_reason(context: RunContext, step: _Step) -> Reason | None:
    """段の区間の全体（`[start_ms, end_ms)`）の検査。窓の外の ramp・落ち着きの間も含める。

    - Critical Safety が NORMAL でない・fault がある → `safety_fault`（§2.2）
    - Control Config の hash が manifest と違う → `config_mismatch`（§2.9）
    """
    if faulted_during(context.ticks, step):
        return Reason.SAFETY_FAULT
    expected = context.hashes.as_tuple()
    if any(
        step.start_ms <= tick.ts_ms < step.end_ms and tick.config != expected
        for tick in context.ticks
    ):
        return Reason.CONFIG_MISMATCH
    return None


def reached_tick(ticks: Sequence[Tick], step: _Step) -> tuple[Tick | None, Reason]:
    """3 zone の `effective` が指示の値に届いた最初の tick（§2.4 の「届いた tick」）。"""
    seen_command = False
    for tick in ticks:
        if not step.start_ms <= tick.ts_ms <= step.end_ms:
            continue
        if tick.mode != "manual" or tick.command_id != step.command_id:
            continue
        seen_command = True
        if all(tick.zones[zone].effective == step.commanded.get(zone) for zone in ZONES):
            return tick, Reason.TRACE_COVERAGE
    # 指示は届いているのに effective が一度も指示の値にならない → floor が掛かった（§2.2）
    return None, (Reason.FLOOR_BOUND if seen_command else Reason.TRACE_COVERAGE)


# ---------------------------------------------------------------------- run の文脈


@dataclass
class RunContext:
    run: LoadedRun
    ticks: list[Tick]
    series: dict[str, Series]
    db_sha256: str
    calibration: list[CalibrationActivation]

    @property
    def hashes(self) -> ConfigHashes:
        return self.run.manifest.control_config_sha256


def _day(at_ms: int, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(at_ms / 1000, tz=UTC).astimezone(zone).date().isoformat()


def _iso(at_ms: float, zone: ZoneInfo) -> str:
    return (
        datetime.fromtimestamp(at_ms / 1000, tz=UTC).astimezone(zone).isoformat(timespec="seconds")
    )


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------------- 回転数の掃引（§2.4）


@dataclass
class SweepObservation:
    run_id: str
    zone: str
    repetition: int
    direction: str
    level: float
    start_ms: int
    reason: Reason | None = None
    window_start_ms: int | None = None
    window_end_ms: int | None = None
    rpm_median: float | None = None
    u: float | None = None
    rpm_max: int | None = None
    response_ms: int | None = None

    def as_dict(self, zone: ZoneInfo) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "zone": self.zone,
            "repetition": self.repetition,
            "direction": self.direction,
            "level": self.level,
            "status": "ok" if self.reason is None else "discarded",
            "reason": None if self.reason is None else self.reason.value,
            "window_start": None
            if self.window_start_ms is None
            else _iso(self.window_start_ms, zone),
            "window_end": None if self.window_end_ms is None else _iso(self.window_end_ms, zone),
            "rpm_median": self.rpm_median,
            "u": _round(self.u),
            "rpm_max": self.rpm_max,
            "response_ms": self.response_ms,
        }


def analyse_sweep_step(
    context: RunContext, step: SweepStep, config: AirflowCharacterizationConfig
) -> SweepObservation:
    """1 つの段の観測（落ち着き 30 s の後の 60 s の `hardware.rpm` の中央値）。"""
    sweep = config.rpm_sweep
    observation = SweepObservation(
        run_id=context.run.manifest.run_id,
        zone=step.zone,
        repetition=step.repetition,
        direction=step.direction,
        level=step.level,
        start_ms=step.start_ms,
    )
    if step.discarded is not None:
        observation.reason = Reason(step.discarded.reason)
        return observation
    step_reason = step_interval_reason(context, step)
    if step_reason is not None:
        observation.reason = step_reason
        return observation
    reach, missing = reached_tick(context.ticks, step)
    if reach is None:
        observation.reason = missing
        return observation
    start = reach.ts_ms + sweep.settle_s.value * 1000
    end = start + sweep.record_s.value * 1000
    observation.window_start_ms, observation.window_end_ms = start, end
    if end > step.end_ms:
        # 段が窓を覆う前に終わった（次の段の tick は別の command_id）
        observation.reason = Reason.TRACE_COVERAGE
        return observation
    checked = check_trace(context.ticks, start, end, step, context.hashes, config)
    if checked.reason is not None:
        observation.reason = checked.reason
        return observation
    # 回転数の覆い（§2.3）: write_ok / readback_ok が true でも rpm が null の tick はありうる
    rpm_points = [
        (tick.ts_ms, float(rpm))
        for tick in checked.ticks
        if (rpm := tick.zones[step.zone].rpm) is not None
    ]
    period = checked.ticks[0].period_ms
    assert period is not None
    need = math.ceil(sweep.min_rpm_ratio.value * (end - start) / period - 1e-9)
    stamps = [start, *(ts for ts, _ in rpm_points), end]
    if len(rpm_points) < need or any(
        later - earlier > sweep.max_rpm_gap_ms.value for earlier, later in pairwise(stamps)
    ):
        observation.reason = Reason.RPM_COVERAGE
        return observation
    stat = window_stat(rpm_points, start, end, config.window_blocks.value)
    if stat is None:
        observation.reason = Reason.RPM_COVERAGE
        return observation
    # アイドルの条件（§2.4）: 窓の平均 + k × u が値未満（§2.6 の上限の検査）
    for metric, key in IDLE_METRICS:
        rows = context.series[metric].between(start, end)
        if not telemetry_covered(rows, start, end, config.source_of(metric)):
            observation.reason = Reason.TELEMETRY_COVERAGE
            return observation
        idle = window_stat(_points(rows), start, end, config.window_blocks.value)
        limit: Param[float] = getattr(config.idle, key)
        if idle is None:
            observation.reason = Reason.TELEMETRY_COVERAGE
            return observation
        if not idle.mean + config.k * idle.u_win < limit.value:
            observation.reason = Reason.CONDITION_MISMATCH
            return observation
        trend_limit = (
            config.max_window_trend.power_w.value
            if metric.startswith("power.")
            else config.max_window_trend.temperature_c.value
        )
        if idle.trend > trend_limit:
            observation.reason = Reason.WINDOW_TREND
            return observation
    values = [value for _, value in rpm_points]
    median = statistics.median(values)
    quantization = config.resolution_of("fan_rpm") / _SQRT12
    observation.rpm_median = median
    observation.u = math.sqrt(stat.u_win**2 + quantization**2)
    observation.rpm_max = int(max(values))
    observation.response_ms = _response_time(context.ticks, step, median, start, end, config)
    return observation


def _response_time(
    ticks: Sequence[Tick],
    step: SweepStep,
    target: float,
    window_start_ms: int,
    window_end_ms: int,
    config: AirflowCharacterizationConfig,
) -> int | None:
    """`effective` が変わった tick から、`hardware.rpm` が ±band に入って留まった tick まで。

    §2.4 の応答の時間。

    tach の更新の間隔（`tach_update_ms`）を足して記録する（0126 と同じ測り方）。
    """
    band = config.rpm_sweep.response_band.value * target
    in_step = [tick for tick in ticks if step.start_ms <= tick.ts_ms < window_end_ms]
    earlier = [tick for tick in ticks if tick.ts_ms < step.start_ms]
    previous = earlier[-1].zones[step.zone].effective if earlier else None
    change = next(
        (
            index
            for index, tick in enumerate(in_step)
            if tick.command_id == step.command_id and tick.zones[step.zone].effective != previous
        ),
        None,
    )
    if change is None:
        return None
    settled: int | None = None
    for index in range(len(in_step) - 1, change - 1, -1):
        rpm = in_step[index].zones[step.zone].rpm
        if rpm is None:
            # null の間は「帯に留まった」と言えないので、窓の中でも前でもそこで区切る
            # （応答の時間を短く見積もらない）。最後の tick が null なら応答の時間は求めない
            break
        if abs(rpm - target) > band:
            break
        settled = index
    if settled is None:
        return None
    return in_step[settled].ts_ms - in_step[change].ts_ms + config.rpm_sweep.tach_update_ms.value


@dataclass
class _Rep:
    run_id: str
    repetition: int
    day: str
    hashes: tuple[str, str, str, str]
    observations: list[SweepObservation] = field(default_factory=list)


def aggregate_sweep(
    observations: Sequence[SweepObservation],
    reps_meta: dict[tuple[str, str, int], tuple[str, tuple[str, str, str, str]]],
    config: AirflowCharacterizationConfig,
) -> dict[str, Any]:
    """zone ごとの候補の表（§2.4 / §2.6 / §2.7 の 4）。"""
    sweep = config.rpm_sweep
    levels = sweep.levels.value
    top = levels[-1]
    zones: dict[str, Any] = {}
    for zone in ZONES:
        reps: dict[tuple[str, int], _Rep] = {}
        for (run_id, rep_zone, repetition), (day, hashes) in sorted(reps_meta.items()):
            if rep_zone == zone:
                reps[(run_id, repetition)] = _Rep(run_id, repetition, day, hashes)
        if not reps:
            continue
        for item in observations:
            if item.zone == zone:
                reps[(item.run_id, item.repetition)].observations.append(item)
        zone_reasons: list[str] = []
        if len({rep.hashes for rep in reps.values()}) != 1:
            zone_reasons.append(Reason.CONFIG_MISMATCH.value)
        level_rows: list[dict[str, Any]] = []
        candidate_points: list[tuple[float, int]] = []
        max_observed: int | None = None
        for level in levels:
            row = _level_row(level, top, list(reps.values()), config)
            level_rows.append(row)
            if row["emitted"]:
                candidate_points.append((level, _round_half_up(row["rpm_median"])))
                observed = row["rpm_max"]
                max_observed = observed if max_observed is None else max(max_observed, observed)
        if len(reps) < sweep.repetitions.value:
            zone_reasons.append(TableStatus.INSUFFICIENT_REPETITIONS.value)
        elif len(reps) > sweep.repetitions.value:
            # 0153 §2.4 は zone ごとに 3 回。多い回を黙って中央値へ混ぜない
            zone_reasons.append(TableStatus.TOO_MANY_REPETITIONS.value)
        if any(not row["emitted"] for row in level_rows):
            zone_reasons.append(TableStatus.LEVEL_MISSING.value)
        candidate: dict[str, Any] | None = None
        if not zone_reasons and max_observed is not None:
            rpms = [rpm for _, rpm in candidate_points]
            if any(later < earlier for earlier, later in pairwise(rpms)) or rpms[-1] <= 0:
                zone_reasons.append(TableStatus.NOT_MONOTONIC.value)
            else:
                round_up = sweep.maximum_rpm_round_up.value
                maximum = (
                    math.ceil(max_observed * (1.0 + sweep.maximum_rpm_margin.value) / round_up)
                    * round_up
                )
                candidate = {
                    "pwm_to_rpm": [
                        {"demand": demand, "rpm": rpm} for demand, rpm in candidate_points
                    ],
                    "airflow_index": [round(rpm / rpms[-1], 4) for rpm in rpms],
                    "airflow_index_basis": SUBSTITUTE_BASIS,
                    "maximum_rpm": maximum,
                    "max_observed_rpm": max_observed,
                }
        zones[zone] = {
            "status": "ok" if candidate is not None else "not_emitted",
            "reasons": zone_reasons,
            "repetitions": [
                {"run_id": rep.run_id, "repetition": rep.repetition, "day": rep.day}
                for rep in reps.values()
            ],
            "levels": level_rows,
            "candidate": candidate,
        }
    return zones


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _level_row(
    level: float, top: float, reps: Sequence[_Rep], config: AirflowCharacterizationConfig
) -> dict[str, Any]:
    sweep = config.rpm_sweep
    k = config.k
    reason: str | None = None
    valid_obs: list[SweepObservation] = []
    valid_days: set[str] = set()
    for rep in reps:
        steps = [item for item in rep.observations if item.level == level]
        present = {item.direction for item in steps}
        required = present if level == top and present else {"up", "down"}
        ok = [item for item in steps if item.reason is None]
        if not steps or {item.direction for item in ok} != required or len(ok) != len(steps):
            reason = TableStatus.INSUFFICIENT_REPETITIONS.value
            continue
        valid_obs.extend(ok)
        valid_days.add(rep.day)
    if reason is None and len(reps) < sweep.repetitions.value:
        reason = TableStatus.INSUFFICIENT_REPETITIONS.value
    if reason is None and len(reps) > sweep.repetitions.value:
        reason = TableStatus.TOO_MANY_REPETITIONS.value
    if reason is None and len(valid_days) < sweep.min_distinct_days.value:
        reason = TableStatus.INSUFFICIENT_DAYS.value
    values = [item.rpm_median for item in valid_obs if item.rpm_median is not None]
    us = [item.u for item in valid_obs if item.u is not None]
    median = statistics.median(values) if values else None
    spread: float | None = None
    if values and us:
        u_max = max(us)
        value_range = max(values) - min(values)
        denominator = statistics.median(values) - k * 1.25 * u_max / math.sqrt(len(values))
        spread = (
            (value_range + k * math.sqrt(2.0) * u_max) / denominator
            if denominator > 0
            else math.inf
        )
        if reason is None and spread > config.approval.max_rpm_spread.value:
            reason = TableStatus.RPM_SPREAD.value
    ups = [
        item.rpm_median
        for item in valid_obs
        if item.direction == "up" and item.rpm_median is not None
    ]
    downs = [
        item.rpm_median
        for item in valid_obs
        if item.direction == "down" and item.rpm_median is not None
    ]
    hysteresis = statistics.median(ups) - statistics.median(downs) if ups and downs else None
    response = [item.response_ms for item in valid_obs if item.response_ms is not None]
    return {
        "level": level,
        "emitted": reason is None and median is not None,
        "reason": reason,
        "observations": len(values),
        "days": len(valid_days),
        "rpm_median": median,
        "rpm_range": None if not values else max(values) - min(values),
        "spread_upper": None if spread is None or math.isinf(spread) else round(spread, 5),
        "rpm_max": max(
            (item.rpm_max for item in valid_obs if item.rpm_max is not None), default=None
        ),
        "hysteresis_up_minus_down": hysteresis,
        "response_ms_up": sorted(
            item.response_ms
            for item in valid_obs
            if item.direction == "up" and item.response_ms is not None
        ),
        "response_ms_down": sorted(
            item.response_ms
            for item in valid_obs
            if item.direction == "down" and item.response_ms is not None
        ),
        "response_count": len(response),
    }


# ---------------------------------------------------------------------- 応答の行列（§2.5）


@dataclass
class MatrixWindow:
    """1 つの段の値の窓（落ち着いた後の最後の `window.length_s`）。"""

    label: str
    index: int
    start_ms: int
    reason: Reason | None = None
    window_start_ms: int | None = None
    window_end_ms: int | None = None
    stats: dict[str, WindowStat] = field(default_factory=dict)
    medians: dict[str, float] = field(default_factory=dict)
    ranges: dict[str, float] = field(default_factory=dict)
    recorded: dict[str, float | None] = field(default_factory=dict)
    throttle: dict[str, bool | None] = field(default_factory=dict)
    zone_rpm: dict[str, float | None] = field(default_factory=dict)

    def indicator(self, item: Indicator) -> WindowStat:
        stat = self.stats[item.minuend]
        return stat if item.subtrahend is None else stat.minus(self.stats[item.subtrahend])


def _settle_range_ok(
    rows: Sequence[Row], limit: float, u: float, k: float, *, median3: bool
) -> bool:
    values = [row.value for row in rows if row.value is not None]
    if median3:
        values = [statistics.median(values[i : i + 3]) for i in range(len(values) - 2)]
    if not values:
        return False
    return max(values) - min(values) + k * u <= limit


def analyse_matrix_step(
    context: RunContext,
    index: int,
    step: MatrixStep,
    config: AirflowCharacterizationConfig,
) -> MatrixWindow:
    """落ち着き（§2.5）を探し、最後の 2 分を値の窓にする。"""
    window = MatrixWindow(label=step.label, index=index, start_ms=step.start_ms)
    if step.discarded is not None:
        window.reason = Reason(step.discarded.reason)
        return window
    step_reason = step_interval_reason(context, step)
    if step_reason is not None:
        window.reason = step_reason
        return window
    reach, missing = reached_tick(context.ticks, step)
    if reach is None:
        window.reason = missing
        return window
    k = config.k
    span = config.settle.span_s.value * 1000
    length = config.window.length_s.value * 1000
    deadline = min(step.end_ms, step.start_ms + config.window.max_settle_s.value * 1000)
    u_air = config.resolution_of("air_c") / _SQRT12 * math.sqrt(2.0)
    chips = [
        (metric, config.resolution_of(key) / _SQRT12 * math.sqrt(2.0))
        for metric, key in SETTLE_CHIP
    ]
    coverage_failure: Reason | None = None
    range_evaluated = False
    value_failure: Reason | None = None
    for tick in context.ticks:
        at = tick.ts_ms
        if at < reach.ts_ms + span or at > deadline:
            continue
        lo = at - span
        rows = {metric: context.series[metric].between(lo, at) for metric in SETTLE_AIR}
        chip_rows = {metric: context.series[metric].between(lo, at) for metric, _ in chips}
        # 落ち着きの区間の全体の覆い（§2.3）を**幅を計算する前に**確かめる。満たさなければ
        # 次の時刻を見る（stale / suspect の外れ値を「未収束」と読み違えない）
        settle_rows = {**rows, **chip_rows}
        if not all(
            telemetry_covered(settle_rows[metric], lo, at, config.source_of(metric))
            for metric in settle_rows
        ):
            coverage_failure = Reason.TELEMETRY_COVERAGE
            continue
        settle_trace = check_trace(context.ticks, lo, at, step, context.hashes, config)
        if settle_trace.reason is not None:
            coverage_failure = settle_trace.reason
            continue
        if not all(
            _settle_range_ok(
                rows[metric], config.settle.max_range_air_c.value, u_air, k, median3=False
            )
            for metric in SETTLE_AIR
        ) or not all(
            _settle_range_ok(
                chip_rows[metric],
                config.settle.max_range_chip_c.value,
                u,
                k,
                median3=metric == "cpu.package",
            )
            for metric, u in chips
        ):
            # 覆いを満たした区間が幅で外れた。前後の覆いの失敗より「未収束」を理由にする
            range_evaluated = True
            continue
        attempt = MatrixWindow(label=step.label, index=index, start_ms=step.start_ms)
        attempt = _value_window(context, attempt, step, at - length, at, config)
        if attempt.reason in _RETRY_VALUE_WINDOW:
            # 値の窓だけの覆いの欠け・傾きなら、締め切りまで後の時刻の窓を探す（§2.5）
            value_failure = attempt.reason
            continue
        return attempt
    if value_failure is not None:
        window.reason = value_failure
    else:
        window.reason = (
            Reason.UNSETTLED if range_evaluated else (coverage_failure or Reason.UNSETTLED)
        )
    return window


_RETRY_VALUE_WINDOW: Final = frozenset(
    {Reason.TELEMETRY_COVERAGE, Reason.TRACE_COVERAGE, Reason.WINDOW_TREND}
)
"""値の窓で捨てても、後の時刻の窓を探し続ける理由。floor・Safety・Config は段ごと捨てる。"""


def _value_window(
    context: RunContext,
    window: MatrixWindow,
    step: MatrixStep,
    start: int,
    end: int,
    config: AirflowCharacterizationConfig,
) -> MatrixWindow:
    window.window_start_ms, window.window_end_ms = start, end
    checked = check_trace(context.ticks, start, end, step, context.hashes, config)
    if checked.reason is not None:
        window.reason = checked.reason
        return window
    blocks = config.window_blocks.value
    for metric in MATRIX_VALUE_METRICS:
        rows = context.series[metric].between(start, end)
        if not telemetry_covered(rows, start, end, config.source_of(metric)):
            window.reason = Reason.TELEMETRY_COVERAGE
            return window
        stat = window_stat(_points(rows), start, end, blocks)
        if stat is None:
            window.reason = Reason.TELEMETRY_COVERAGE
            return window
        window.stats[metric] = stat
        values = [value for _, value in _points(rows)]
        window.medians[metric] = statistics.median(values)
        window.ranges[metric] = max(values) - min(values)
    for item in (*INDICATORS, *POWER_INDICATORS):
        limit = (
            config.max_window_trend.temperature_c.value
            if item.kind == "temperature"
            else config.max_window_trend.power_w.value
        )
        if window.indicator(item).trend > limit:
            window.reason = Reason.WINDOW_TREND
            return window
    # 指標に入らない温度（air.room・board.connector_12v2x6 など）も値の窓の統計として
    # 保持するので、同じ傾きの上限を当てる（§2.6）。電力は上の POWER_INDICATORS で見た
    for metric, stat in window.stats.items():
        if metric == PUMP or metric.startswith("power."):
            continue
        if stat.trend > config.max_window_trend.temperature_c.value:
            window.reason = Reason.WINDOW_TREND
            return window
    for metric in RECORD_ONLY:
        # 残すものも値の窓で使う metric に数える。覆わなければ運転の状態の記録を欠いた窓として捨てる
        rows = context.series[metric].between(start, end)
        if not telemetry_covered(rows, start, end, config.source_of(metric)):
            window.reason = Reason.TELEMETRY_COVERAGE
            return window
        window.recorded[metric] = statistics.fmean(value for _, value in _points(rows))
    for metric in THROTTLE_METRICS:
        # 覆いの欠けた窓では「立っていない」と言えない（欠けた間に立ったかもしれない）→ None
        rows = context.series[metric].between(start, end)
        covered = telemetry_covered(rows, start, end, config.coverage.telemetry)
        window.throttle[metric] = any(bool(row.value) for row in rows) if covered else None
    for zone in ZONES:
        rpms = [float(rpm) for tick in checked.ticks if (rpm := tick.zones[zone].rpm) is not None]
        window.zone_rpm[zone] = statistics.median(rpms) if rpms else None
    return window


def _window_dict(window: MatrixWindow, zone: ZoneInfo) -> dict[str, Any]:
    return {
        "label": window.label,
        "index": window.index,
        "status": "ok" if window.reason is None else "discarded",
        "reason": None if window.reason is None else window.reason.value,
        "window_start": None
        if window.window_start_ms is None
        else _iso(window.window_start_ms, zone),
        "window_end": None if window.window_end_ms is None else _iso(window.window_end_ms, zone),
        "means": {metric: _round(stat.mean) for metric, stat in sorted(window.stats.items())},
        "recorded": {metric: _round(value) for metric, value in window.recorded.items()},
        "throttle": window.throttle,
        "hardware_rpm_median": window.zone_rpm,
    }


def _pair(
    moved: MatrixWindow,
    before: MatrixWindow,
    after: MatrixWindow,
    config: AirflowCharacterizationConfig,
) -> dict[str, Any]:
    """動かした段と前後の基準の段の組（§2.5 の応答の値・電力とポンプの条件）。"""
    k = config.k
    result: dict[str, Any] = {
        "label": moved.label,
        "index": moved.index,
        "status": "ok",
        "reason": None,
        "reason_from": None,
        "power_delta": {},
        "throttle": moved.throttle,
        "responses": {},
    }
    for role, window in (("moved", moved), ("baseline_before", before), ("baseline_after", after)):
        if window.reason is not None:
            result.update(status="discarded", reason=window.reason.value, reason_from=role)
            return result
    mismatch: Reason | None = None
    for item in POWER_INDICATORS:
        diff = interpolated_difference(
            moved.indicator(item), before.indicator(item), after.indicator(item), item.u_res(config)
        )
        result["power_delta"][item.name] = {"value": _round(diff.value), "u": _round(diff.u)}
        if abs(diff.value) + k * diff.u > config.response_matrix.max_power_delta_w.value:
            mismatch = mismatch or Reason.POWER_MISMATCH
    pump_limit = config.max_aux_fan_delta_rpm.value
    u_moved = moved.stats[PUMP].u_win
    for base in (before, after):
        u_base = base.stats[PUMP].u_win
        delta = abs(moved.medians[PUMP] - base.medians[PUMP])
        if delta + k * math.sqrt(u_moved**2 + u_base**2) > pump_limit:
            mismatch = mismatch or Reason.CONDITION_MISMATCH
    for window in (moved, before, after):
        if window.ranges[PUMP] + k * window.stats[PUMP].u_win > pump_limit:
            mismatch = mismatch or Reason.CONDITION_MISMATCH
    if mismatch is not None:
        # 捨てた組の電力の差と throttle の flag は残す（§2.5。黙って消さない）
        result.update(status="discarded", reason=mismatch.value, reason_from="pair")
        return result
    for item in INDICATORS:
        diff = interpolated_difference(
            moved.indicator(item), before.indicator(item), after.indicator(item), item.u_res(config)
        )
        result["responses"][item.name] = {
            "value": _round(diff.value),
            "u": _round(diff.u),
            "weight": _round(diff.weight),
        }
    return result


def _room_drift(context: RunContext, config: AirflowCharacterizationConfig) -> bool:
    """セッションの中の `air.room` のブロックの平均の最大 − 最小の上側の境界（§2.6）。

    値の窓だけでなく**セッションの全体**（最初の段の始まりから最後の段の終わり）を、値の窓の
    ブロックと同じ長さ（`window.length_s / window_blocks`）に区切って見る。落ち着きの途中や
    捨てた段の間のずれも、セッションをやり直す理由にする（§2.5）。ok の行の無いブロックは飛ばす。
    """
    run = context.run
    width = config.window.length_s.value * 1000 // config.window_blocks.value
    blocks: list[float] = []
    for start in range(run.start_ms, run.end_ms, width):
        values = [
            row.value
            for row in context.series[ROOM].between(start, min(start + width, run.end_ms))
            if row.quality == "ok" and row.value is not None
        ]
        if values:
            blocks.append(statistics.fmean(values))
    if not blocks:
        return False
    u = config.resolution_of("air_c") / _SQRT12 * math.sqrt(2.0)
    return max(blocks) - min(blocks) + config.k * u > config.max_room_drift_c.value


def _calibration_reason(context: RunContext) -> tuple[Reason | None, str | None]:
    """§2.9 の較正との結びつけ。区間の中の有効化・区間の開始で有効な行の無さ・digest の違い。"""
    run = context.run
    manifest = run.manifest
    rows = context.calibration
    if any(run.start_ms <= row.ts_ms <= run.end_ms for row in rows):
        return Reason.CALIBRATION_CHANGED, None
    effective = [row for row in rows if row.ts_ms < run.start_ms]
    if not effective:
        return Reason.CALIBRATION_CHANGED, None
    digest = effective[-1].offsets_sha256
    if digest != manifest.calibration_offsets_sha256:
        return Reason.CALIBRATION_CHANGED, digest
    return None, digest


def analyse_matrix_run(
    context: RunContext, config: AirflowCharacterizationConfig
) -> dict[str, Any]:
    manifest = context.run.manifest
    assert isinstance(manifest, MatrixManifest)
    zone = config.zone
    run_reason, digest = _calibration_reason(context)
    windows = [
        analyse_matrix_step(context, index, step, config)
        for index, step in enumerate(manifest.steps)
    ]
    if run_reason is None and _room_drift(context, config):
        run_reason = Reason.ROOM_DRIFT
    pairs: list[dict[str, Any]] = []
    if run_reason is None:
        for index, window in enumerate(windows):
            if window.label == "R":
                continue
            pairs.append(_pair(window, windows[index - 1], windows[index + 1], config))
    return {
        "run_id": manifest.run_id,
        "day": _day(context.run.start_ms, zone),
        "load": manifest.load.model_dump(),
        "status": "ok" if run_reason is None else "discarded",
        "reason": None if run_reason is None else run_reason.value,
        "calibration_offsets_sha256": digest,
        "steps": [_window_dict(window, zone) for window in windows],
        "pairs": pairs,
    }


def aggregate_matrix(
    runs: Sequence[tuple[RunContext, dict[str, Any]]], config: AirflowCharacterizationConfig
) -> list[dict[str, Any]]:
    """負荷ごとの 2 回のまとめ（§2.5 / §2.6）。平均の `u` は `max(sqrt(Σu²)/n, 2 回の差/2)`。"""
    matrix = config.response_matrix
    by_load: dict[str, list[tuple[RunContext, dict[str, Any]]]] = defaultdict(list)
    for context, report in runs:
        manifest = context.run.manifest
        assert isinstance(manifest, MatrixManifest)
        by_load[manifest.load.kind].append((context, report))
    output: list[dict[str, Any]] = []
    for load in sorted(by_load):
        members = [
            (context, report) for context, report in by_load[load] if report["status"] == "ok"
        ]
        reasons: list[str] = []
        if len({context.hashes.as_tuple() for context, _ in members}) > 1:
            reasons.append(Reason.CONFIG_MISMATCH.value)
        if len({report["calibration_offsets_sha256"] for _, report in members}) > 1:
            reasons.append(Reason.CALIBRATION_CHANGED.value)
        settings = {
            (report["load"]["gpu_power_limit_w"], report["load"]["cpu_workers"])
            for _, report in members
        }
        if len(settings) > 1:
            reasons.append(TableStatus.LOAD_MISMATCH.value)
        entries: list[dict[str, Any]] = []
        for label in LABELS[1:]:
            for item in INDICATORS:
                found = [
                    (report["run_id"], report["day"], pair["responses"][item.name])
                    for _, report in members
                    for pair in report["pairs"]
                    if pair["label"] == label and pair["status"] == "ok"
                ]
                values = [entry["value"] for _, _, entry in found]
                us = [entry["u"] for _, _, entry in found]
                status: str | None = None
                if reasons:
                    status = reasons[0]
                elif len(found) < matrix.repetitions.value:
                    status = TableStatus.INSUFFICIENT_REPETITIONS.value
                elif len(found) > matrix.repetitions.value:
                    # §2.6 の平均の u は 2 回の式。黙って一般化しない
                    status = TableStatus.TOO_MANY_REPETITIONS.value
                elif len({day for _, day, _ in found}) < matrix.min_distinct_days.value:
                    status = TableStatus.INSUFFICIENT_DAYS.value
                mean = u_mean = spread = None
                if status is None:
                    n = len(values)
                    mean = statistics.fmean(values)
                    spread = max(values) - min(values)
                    u_mean = max(math.sqrt(sum(u**2 for u in us)) / n, spread / 2.0)
                entries.append(
                    {
                        "label": label,
                        "indicator": item.name,
                        "runs": [
                            {"run_id": run_id, "day": day, "value": entry["value"], "u": entry["u"]}
                            for run_id, day, entry in found
                        ],
                        "status": "ok" if status is None else "not_emitted",
                        "reason": status,
                        "mean": _round(mean),
                        "u_mean": _round(u_mean),
                        "difference": _round(spread),
                    }
                )
        output.append({"load": load, "reasons": reasons, "entries": entries})
    return output


# ---------------------------------------------------------------------- 組み立て


def _required_resolutions(kinds: Iterable[str]) -> list[str]:
    kinds = set(kinds)
    keys: list[str] = []
    if "rpm_sweep" in kinds:
        keys.append("fan_rpm")
    if "response_matrix" in kinds:
        keys += [
            "air_c",
            "power_w",
            "gpu_core_c",
            "gpu_hotspot_c",
            "cpu_package_c",
            "board_connector_c",
        ]
    return keys


_TABLES: Final[tuple[Literal["readings", "control_traces"], ...]] = ("readings", "control_traces")


def _open_contexts(
    runs: Sequence[LoadedRun], config: AirflowCharacterizationConfig
) -> list[RunContext]:
    databases: dict[Path, tuple[CopyDatabase, str]] = {}
    contexts: list[RunContext] = []
    try:
        for run in runs:
            manifest = run.manifest
            key = run.db_path.resolve()
            if key not in databases:
                if not run.db_path.is_file():
                    raise AnalysisError(
                        f"{manifest.run_id}: DB の写しが無い: {manifest.db_copy.file}"
                    )
                digest = file_sha256(run.db_path)
                # **開く前に** bytes の digest を照合する。照合できない写しを SQLite に渡さない
                if digest != manifest.db_copy.sha256:
                    raise AnalysisError(
                        f"{manifest.run_id}: DB の写しの SHA-256 が manifest と違う"
                    )
                try:
                    databases[key] = (CopyDatabase(run.db_path), digest)
                except EvidenceDatabaseError as error:
                    raise AnalysisError(f"{manifest.run_id}: {error}") from error
            database, digest = databases[key]
            if digest != manifest.db_copy.sha256:
                raise AnalysisError(f"{manifest.run_id}: DB の写しの SHA-256 が manifest と違う")
            for table in _TABLES:
                last = database.last_ts(table)
                if last is None or last < run.end_ms:
                    raise AnalysisError(
                        f"{manifest.run_id}: DB の写しの {table} が run の終わりを覆わない"
                        "（写しは回の終了より後に作る。§2.7 の 2）"
                    )
            calibration: list[CalibrationActivation] = []
            if isinstance(manifest, MatrixManifest):
                try:
                    calibration = list(verify_activation_rows(database.calibration_rows()))
                except CalibrationHistoryError as error:
                    raise AnalysisError(
                        f"{manifest.run_id}: 較正の記録を検証できない: {error}"
                    ) from error
            lookback = config.settle.span_s.value * 1000
            metrics = [*REQUIRED_METRICS, *THROTTLE_METRICS]
            series = {
                metric: Series(database.rows(metric, run.start_ms - lookback, run.end_ms))
                for metric in metrics
            }
            contexts.append(
                RunContext(
                    run=run,
                    ticks=database.ticks(run.start_ms, run.end_ms),
                    series=series,
                    db_sha256=digest,
                    calibration=calibration,
                )
            )
    finally:
        for database, _ in databases.values():
            database.close()
    return contexts


def build(
    runs: Sequence[LoadedRun], config: AirflowCharacterizationConfig, config_sha256: str
) -> dict[str, Any]:
    """manifest と DB の写しから報告を作る。拒否なら `AnalysisError`（何も出さない）。"""
    if len({run.manifest.run_id for run in runs}) != len(runs):
        raise AnalysisError("run_id が重複している")
    kinds = {run.manifest.kind for run in runs}
    for key in _required_resolutions(kinds):
        config.resolution_of(key)
    for run in runs:
        validate_against_config(run, config)
    contexts = _open_contexts(runs, config)
    zone = config.zone
    observations: list[SweepObservation] = []
    reps_meta: dict[tuple[str, str, int], tuple[str, tuple[str, str, str, str]]] = {}
    matrix_runs: list[tuple[RunContext, dict[str, Any]]] = []
    for context in contexts:
        manifest = context.run.manifest
        if isinstance(manifest, SweepManifest):
            for step in manifest.steps:
                rep_key = (manifest.run_id, step.zone, step.repetition)
                if rep_key not in reps_meta:
                    reps_meta[rep_key] = (_day(step.start_ms, zone), context.hashes.as_tuple())
                observations.append(analyse_sweep_step(context, step, config))
        else:
            matrix_runs.append((context, analyse_matrix_run(context, config)))
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "decision": "0153",
        "notes": list(NOTES),
        "inputs": {
            "config_sha256": config_sha256,
            "timezone": config.timezone,
            "undetermined": config.undetermined(),
            "runs": [
                {
                    "run_id": context.run.manifest.run_id,
                    "kind": context.run.manifest.kind,
                    "manifest_sha256": context.run.manifest_sha256,
                    "db_copy_sha256": context.db_sha256,
                    "start": _iso(context.run.start_ms, zone),
                    "end": _iso(context.run.end_ms, zone),
                }
                for context in contexts
            ],
        },
        "rpm_sweep": None,
        "response_matrix": None,
    }
    if observations:
        report["rpm_sweep"] = {
            "zones": aggregate_sweep(observations, reps_meta, config),
            "steps": [item.as_dict(zone) for item in observations],
        }
    if matrix_runs:
        report["response_matrix"] = {
            "runs": [item for _, item in matrix_runs],
            "aggregate": aggregate_matrix(matrix_runs, config),
        }
    return report


def as_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _num(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _row(cells: Iterable[object]) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _header(*names: str) -> list[str]:
    return [_row(names), _row("---" for _ in names)]


def as_markdown(report: dict[str, Any]) -> str:
    """人が読む表。**個体識別子・ホスト名・パスを出さない**（ルール 10）。"""
    lines = ["# 回転数の掃引と応答の行列（決定記録 0153）", ""]
    lines += [f"- {note}" for note in report["notes"]]
    inputs = report["inputs"]
    lines += [
        "",
        "## 入力",
        "",
        f"- 設定の SHA-256: `{inputs['config_sha256']}`",
        f"- 未定の設定: {', '.join(inputs['undetermined']) or 'なし'}",
        "",
        *_header("run_id", "種類", "開始", "終了", "DB の写しの SHA-256"),
    ]
    lines += [
        _row(
            [
                run["run_id"],
                run["kind"],
                run["start"],
                run["end"],
                f"`{run['db_copy_sha256'][:16]}…`",
            ]
        )
        for run in inputs["runs"]
    ]
    if report["rpm_sweep"] is not None:
        lines += _sweep_markdown(report["rpm_sweep"])
    if report["response_matrix"] is not None:
        lines += _matrix_markdown(report["response_matrix"])
    return "\n".join(lines) + "\n"


def _sweep_markdown(sweep: dict[str, Any]) -> list[str]:
    lines = ["", "## 回転数の掃引（§2.4）"]
    for zone, item in sweep["zones"].items():
        reasons = f"（{', '.join(item['reasons'])}）" if item["reasons"] else ""
        lines += [
            "",
            f"### {zone}: {item['status']}{reasons}",
            "",
            *_header(
                "demand",
                "値",
                "理由",
                "観測",
                "日",
                "RPM 中央値",
                "範囲",
                "範囲÷中央値の上側",
                "上り−下り",
                "応答 ms（上り）",
            ),
        ]
        for row in item["levels"]:
            lines.append(
                _row(
                    [
                        f"{row['level']:.2f}",
                        "出す" if row["emitted"] else "出さない",
                        row["reason"] or "—",
                        row["observations"],
                        row["days"],
                        _num(row["rpm_median"]),
                        _num(row["rpm_range"]),
                        _num(row["spread_upper"]),
                        _num(row["hysteresis_up_minus_down"]),
                        ", ".join(str(value) for value in row["response_ms_up"]) or "—",
                    ]
                )
            )
        candidate = item["candidate"]
        if candidate is not None:
            points = ", ".join(f"{p['demand']:.2f}→{p['rpm']}" for p in candidate["pwm_to_rpm"])
            index = ", ".join(f"{value:.4f}" for value in candidate["airflow_index"])
            lines += [
                "",
                f"- 候補の `maximum_rpm`: {candidate['maximum_rpm']}"
                f"（観測の最大 {candidate['max_observed_rpm']}）",
                f"- 候補の `pwm_to_rpm`: {points}",
                f"- 候補の `airflow_index`: {index}（{candidate['airflow_index_basis']}）",
            ]
    discarded = [step for step in sweep["steps"] if step["status"] != "ok"]
    if discarded:
        lines += ["", "### 捨てた段", "", *_header("run_id", "zone", "回", "方向", "段", "理由")]
        lines += [
            _row(
                [
                    step["run_id"],
                    step["zone"],
                    step["repetition"],
                    step["direction"],
                    f"{step['level']:.2f}",
                    step["reason"],
                ]
            )
            for step in discarded
        ]
    return lines


def _matrix_markdown(matrix: dict[str, Any]) -> list[str]:
    lines = ["", "## 応答の行列（§2.5。風量ではない）"]
    for run in matrix["runs"]:
        reason = f"（{run['reason']}）" if run["reason"] else ""
        title = f"{run['run_id']}（負荷 {run['load']['kind']}・{run['day']}）"
        lines += [
            "",
            f"### {title}: {run['status']}{reason}",
            "",
            *_header(
                "段",
                "状態",
                "理由",
                "ΔP CPU",
                "ΔP GPU",
                "throttle",
                *(item.name for item in INDICATORS),
            ),
        ]
        for pair in run["pairs"]:
            power = pair["power_delta"]
            why = pair["reason"] or "—"
            if pair["reason_from"]:
                why += f"（{pair['reason_from']}）"
            lines.append(
                _row(
                    [
                        pair["label"],
                        pair["status"],
                        why,
                        _cell(power.get("power.cpu.package")),
                        _cell(power.get("power.gpu.0")),
                        _throttle(pair["throttle"]),
                        *(_cell(pair["responses"].get(item.name)) for item in INDICATORS),
                    ]
                )
            )
        lines += [
            "",
            f"#### {run['run_id']} の段ごとの記録（値の窓の平均・中央値）",
            "",
            *_header(
                "段",
                "状態",
                "理由",
                "窓",
                *(_SHORT[metric] for metric in _STEP_COLUMNS),
                "RPM front / rear / top",
            ),
        ]
        for step in run["steps"]:
            values = {**step["means"], **step["recorded"]}
            rpm = step["hardware_rpm_median"]
            lines.append(
                _row(
                    [
                        step["label"],
                        step["status"],
                        step["reason"] or "—",
                        "—"
                        if step["window_start"] is None
                        else f"{step['window_start']} 〜 {step['window_end']}",
                        *(_num(values.get(metric)) for metric in _STEP_COLUMNS),
                        " / ".join(_num(rpm.get(zone)) for zone in ZONES) if rpm else "—",
                    ]
                )
            )
    lines += [
        "",
        "### まとめ（負荷ごと）",
        "",
        *_header("負荷", "段", "指標", "状態", "平均", "u", "2 回の差"),
    ]
    for group in matrix["aggregate"]:
        for entry in group["entries"]:
            lines.append(
                _row(
                    [
                        group["load"],
                        entry["label"],
                        entry["indicator"],
                        entry["reason"] or "ok",
                        _num(entry["mean"]),
                        _num(entry["u_mean"]),
                        _num(entry["difference"]),
                    ]
                )
            )
    return lines


_STEP_COLUMNS: Final = (
    "air.room",
    "air.room_humidity",
    "board.connector_12v2x6",
    "power.cpu.package",
    "power.gpu.0",
    PUMP,
    "fan.vrm.rpm",
    "gpu.0.fan_speed",
)
_SHORT: Final = {
    "air.room": "室温",
    "air.room_humidity": "湿度",
    "board.connector_12v2x6": "12V-2x6",
    "power.cpu.package": "P CPU",
    "power.gpu.0": "P GPU",
    PUMP: "ポンプ rpm",
    "fan.vrm.rpm": "VRM rpm",
    "gpu.0.fan_speed": "GPU Fan %",
}
"""Markdown の段ごとの表の列（§2.5 の「残す」もの）。"""


def _throttle(flags: dict[str, bool | None]) -> str:
    """立っていた throttle の flag（§2.5。捨てた組でも残す）。覆いの欠けた flag は「不明」。"""
    if not flags:
        return "—"
    raised = [name.removeprefix("gpu.0.throttle.") for name, value in flags.items() if value]
    unknown = [
        name.removeprefix("gpu.0.throttle.") for name, value in flags.items() if value is None
    ]
    parts = [*raised, f"不明: {', '.join(unknown)}"] if unknown else raised
    return ", ".join(parts) or "なし"


def _cell(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "—"
    return f"{_num(entry['value'])} ± {_num(entry['u'])}"


# ---------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-airflow-characterization`。所有者が測定の後に手で走らせる。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-airflow-characterization",
        description=(
            "回転数の掃引と応答の行列の解析（DB の写しと manifest を読むだけ。設定・DB を書かない）"
        ),
    )
    parser.add_argument("--run", type=Path, action="append", required=True, dest="runs")
    parser.add_argument("--config", type=Path, default=Path("config/airflow-characterization.yaml"))
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--out", type=Path, help="出力先。省けば標準出力")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logs.configure(args.log_level)

    try:
        payload = args.config.read_bytes()
        config = AirflowCharacterizationConfig.from_bytes(payload)
        runs = [load_manifest(path) for path in args.runs]
    except (OSError, ValueError, ValidationError) as error:
        LOGGER.error("設定か manifest を読めない", extra={logs.FIELDS_KEY: {"error": str(error)}})
        return 2
    if args.out is not None:
        try:
            for run in runs:
                refuse_output_on_inputs(args.out, db=run.db_path, inputs=[args.config, *args.runs])
        except EvidenceOutputError as error:
            LOGGER.error("--out が入力を指している", extra={logs.FIELDS_KEY: {"error": str(error)}})
            return 2
    try:
        report = build(runs, config, hashlib.sha256(payload).hexdigest())
    except AnalysisError as error:
        LOGGER.error("解析を拒否した", extra={logs.FIELDS_KEY: {"error": str(error)}})
        return 1
    text = as_json(report) if args.format == "json" else as_markdown(report)
    if args.out is None:
        # 人が読むもの。print を使わず標準出力へそのまま書く
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    LOGGER.info(
        "回転数の掃引と応答の行列を解析した",
        extra={logs.FIELDS_KEY: {"runs": len(runs), "format": args.format}},
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
