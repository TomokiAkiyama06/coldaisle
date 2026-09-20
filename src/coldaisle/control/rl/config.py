"""RL Supervisor 学習環境の設定（`config/rl-training.yaml`。#105 / 決定記録 0058 §2.7）。

**既定値をコードに置かない。** 実測前の値はすべて `{value, status: provisional}` で持ち、
設定が欠けていれば読み込みで落とす（AGENTS.md ルール9）。

**ほかの契約が持つ値をここへ写さない**（決定記録 0054 §2.6 と同じ規則）。

- 絶対温度上限・最低安全 demand: `config/safety.yaml`
- Supervisor action の許容範囲: `config/fan-policy.yaml` の `supervisor.output_bounds`
- 適用 demand の照合幅: `config/fan-policy.yaml` の `shadow.applied_demand_tolerance`

写すと、学習環境だけが別の契約で動く余地ができる。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle.control.config import ConfigValue, FiniteFloat, PositiveMilliseconds, UnitInterval
from coldaisle.control.model.thermal import ThermalMetricName
from coldaisle.control.schema import SupervisorTargetBand

RL_TRAINING_CONFIG_VERSION: Literal[1] = 1
RL_TRAINING_CONFIG_FILENAME = "rl-training.yaml"

MAX_EPISODE_STEPS = 4_096
"""1 episode の step 数の構造上限。

未信頼な設定による資源枯渇を防ぐ境界で、調整値ではない（決定記録 0052 §2.6 と同じ扱い）。
"""

MAX_SIMULATOR_METRICS = 32
"""simulator が応答を持てる metric 数の構造上限。"""

RlFloat = ConfigValue[FiniteFloat]
RlMilliseconds = ConfigValue[PositiveMilliseconds]
RlUnitInterval = ConfigValue[UnitInterval]
RlCount = ConfigValue[Annotated[int, Field(ge=0)]]


def _sequence_to_tuple(value: object) -> object:
    """YAML 配列を、読み込み後は不変な tuple として保持する。"""
    return tuple(value) if isinstance(value, list) else value


class _ConfigModel(BaseModel):
    """設定を欠損・余分な鍵・暗黙変換から守る共通基底。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RewardWeights(_ConfigModel):
    """reward の項ごとの重み。

    **安全の項をここに置けない**（`extra="forbid"`）。安全を重み付きの項にすると、
    ほかの項の改善で違反を相殺できてしまう（決定記録 0052 §4 (c) / 0054 §2.4 と同じ理由）。
    安全は reward ではなく `EpisodeSafety` の台帳で数え、辞書式に先に立つ。
    """

    cpu_temperature: RlUnitInterval
    gpu_temperature: RlUnitInterval
    acoustic: RlUnitInterval
    change: RlUnitInterval

    @model_validator(mode="after")
    def _at_least_one_term_is_enabled(self) -> Self:
        if not any(
            item.value > 0.0
            for item in (self.cpu_temperature, self.gpu_temperature, self.acoustic, self.change)
        ):
            raise ValueError("reward.weights は少なくとも1つを正にする")
        return self


class RewardScales(_ConfigModel):
    """単位の違う項を無単位へ落とす基準量（決定記録 0052 §2.3 と同じ考え方）。"""

    temperature_c: RlFloat
    acoustic_cost: RlFloat
    demand_change: RlFloat

    @model_validator(mode="after")
    def _scales_are_positive(self) -> Self:
        for name in ("temperature_c", "acoustic_cost", "demand_change"):
            scale: RlFloat = getattr(self, name)
            if scale.value <= 0.0:
                raise ValueError(f"reward.scales.{name} は正にする")
        return self


class RewardMetrics(_ConfigModel):
    """reward が読む温度 metric。**コードに metric 名を置かない。**"""

    cpu_temperature: ThermalMetricName
    gpu_temperature: ThermalMetricName

    @model_validator(mode="after")
    def _metrics_are_distinct(self) -> Self:
        if self.cpu_temperature == self.gpu_temperature:
            raise ValueError("reward.metrics の CPU / GPU に同じ metric を使わない")
        return self


class RewardConfig(_ConfigModel):
    """reward 関数の版と中身。**episode の結果に版を必ず残す。**"""

    version: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=64)
    metrics: RewardMetrics
    scales: RewardScales
    weights: RewardWeights
    target_band: SupervisorTargetBand
    """採点に使う温度帯。**action の target band ではない**（決定記録 0058 §2.4）。

    action 側の帯で採点すると、agent は帯を広げるだけで reward を上げられる。
    採点の基準は設定が所有し、agent が動かせない。
    """
    discount: RlUnitInterval
    """割引率。1.0 を許すのは有限 horizon の episode だけを回すため。"""


class SafetyScreenConfig(_ConfigModel):
    """環境上で「Critical Safety 違反に相当する」と見なす条件（決定記録 0058 §2.5）。

    **これは Critical Safety（#78）ではない。** 環境は `control.safety` を import せず、
    最終裁定も持たない。ここにあるのは「その state / action を探索の終端にする」ための
    決定論的な screen で、閾値は `config/safety.yaml` から取る（写さない）。
    """

    temperature_metrics: Annotated[
        tuple[ThermalMetricName, ...],
        BeforeValidator(_sequence_to_tuple),
        Field(min_length=1, max_length=MAX_SIMULATOR_METRICS),
    ]
    """`safety.absolute_temp_ceiling_c` と突き合わせる metric。"""

    @model_validator(mode="after")
    def _metrics_are_unique(self) -> Self:
        if len(set(self.temperature_metrics)) != len(self.temperature_metrics):
            raise ValueError("safety_screen.temperature_metrics を重複させない")
        return self


class EpisodeConfig(_ConfigModel):
    """episode の刻みと長さ。"""

    step_ms: RlMilliseconds
    max_steps: Annotated[int, Field(ge=1, le=MAX_EPISODE_STEPS)]
    recent_history_steps: Annotated[int, Field(ge=0, le=64)]
    """policy へ渡す過去 snapshot の本数。**コードに既定値を置かない**（AGENTS.md ルール9）。"""


class CoverageConfig(_ConfigModel):
    """採点できた step の下限（決定記録 0054 §2.3 と同じ fail closed）。"""

    minimum_supported_fraction: RlUnitInterval
    minimum_steps: RlCount


class SimulatorResponse(_ConfigModel):
    """1 metric の1次遅れ応答。**実測に裏づけられていない近似**（決定記録 0058 §2.3）。"""

    metric: ThermalMetricName
    ambient_c: RlFloat
    load_gain_c: RlFloat
    flow_floor: RlFloat
    flow_gain: RlFloat
    time_constant_ms: RlMilliseconds
    noise_sigma_c: RlFloat

    @model_validator(mode="after")
    def _response_is_physically_ordered(self) -> Self:
        if self.flow_floor.value <= 0.0:
            # 0 だと demand=0 で分母が消え、温度が発散する。
            raise ValueError("simulator.responses.flow_floor は正にする")
        if self.flow_gain.value < 0.0:
            raise ValueError("simulator.responses.flow_gain は 0 以上にする")
        if self.load_gain_c.value < 0.0:
            # 負にすると「回すほど熱くなる」向きの近似になり、学習が逆を向く。
            raise ValueError("simulator.responses.load_gain_c は 0 以上にする")
        if self.noise_sigma_c.value < 0.0:
            raise ValueError("simulator.responses.noise_sigma_c は 0 以上にする")
        return self


class SimulatorConfig(_ConfigModel):
    """近似 simulator の識別子と応答。

    `model_id` は **Registry の artifact ID ではない**。この simulator は Registry の証拠を
    持てず、ここから作った episode は昇格の根拠にできない（決定記録 0058 §2.3）。
    """

    model_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=120)
    model_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    responses: Annotated[
        tuple[SimulatorResponse, ...],
        BeforeValidator(_sequence_to_tuple),
        Field(min_length=1, max_length=MAX_SIMULATOR_METRICS),
    ]

    @model_validator(mode="after")
    def _metrics_are_unique(self) -> Self:
        metrics = tuple(response.metric for response in self.responses)
        if len(set(metrics)) != len(metrics):
            raise ValueError("simulator.responses に同じ metric を2つ置かない")
        return self


class RlTrainingConfig(_ConfigModel):
    """学習環境の設定一式。"""

    schema_version: Literal[1]
    episode: EpisodeConfig
    reward: RewardConfig
    coverage: CoverageConfig
    safety_screen: SafetyScreenConfig
    simulator: SimulatorConfig

    @model_validator(mode="after")
    def _reward_metrics_are_screened(self) -> Self:
        screened = set(self.safety_screen.temperature_metrics)
        missing = sorted(
            {self.reward.metrics.cpu_temperature, self.reward.metrics.gpu_temperature} - screened
        )
        if missing:
            # 採点する温度が screen の外にあると、上限を超えたまま良い reward を積める。
            raise ValueError(f"reward が読む温度は safety_screen にも入れる: {missing}")
        return self

    @classmethod
    def from_file(cls, path: Path) -> tuple[RlTrainingConfig, str]:
        """設定を読み、検証済みの設定と**その bytes の** SHA-256 を返す。

        報告に残すのは hash だけで、絶対 path は残さない（決定記録 0021）。
        """
        text = path.read_text(encoding="utf-8")
        loaded: Any = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"学習環境の設定が辞書ではない: {path.name}")
        return cls.model_validate(loaded), sha256(text.encode("utf-8")).hexdigest()
