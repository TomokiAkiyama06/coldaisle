"""Zone 別の読み取り専用 Acoustic Cost Model (#94)。

返す ``acoustic_cost`` は無単位の最適化用コストであり、dBA / SPL の推定値ではない。
Thermal Model、Safety、Hardware Backend には依存せず、制御器が requested demand を
選ぶときだけ任意で参照できる。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Self

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle.control.schema import Demand, PerZone

ACOUSTIC_CONFIG_VERSION: Literal[1] = 1
ACOUSTIC_CONFIG_FILENAME = "acoustic.yaml"
FiniteCost = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


def _yaml_sequence_to_tuple(value: object) -> object:
    """YAML 配列を読み込み後は不変な tuple として保持する。"""
    return tuple(value) if isinstance(value, list) else value


class _Frozen(BaseModel):
    """外部設定と最適化入力を暗黙変換・余分なキーから守る基底。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class AcousticModelSource(_Frozen):
    """コスト曲線の根拠。値を物理量と取り違えないために必ず残す。"""

    kind: Literal["approximate", "measured"]
    basis: str = Field(min_length=1, max_length=500)


class AcousticCurvePoint(_Frozen):
    """Zone ごとの demand と無単位 acoustic cost の対応点。"""

    demand: Demand
    acoustic_cost: FiniteCost


class ZoneAcousticCurve(_Frozen):
    """単調な点列で表す Zone 固有の非線形コスト曲線。"""

    points: Annotated[
        tuple[AcousticCurvePoint, ...],
        BeforeValidator(_yaml_sequence_to_tuple),
        Field(min_length=2),
    ]

    @model_validator(mode="after")
    def _points_are_monotonic(self) -> Self:
        previous_demand: float | None = None
        previous_cost: float | None = None
        for point in self.points:
            if previous_demand is not None and point.demand <= previous_demand:
                raise ValueError("音響曲線の demand は単調増加にする")
            if previous_cost is not None and point.acoustic_cost < previous_cost:
                raise ValueError("音響曲線の acoustic_cost は demand に対して下げない")
            previous_demand = point.demand
            previous_cost = point.acoustic_cost
        if self.points[0].demand != 0.0 or self.points[-1].demand != 1.0:
            raise ValueError("音響曲線は demand=0.0 と demand=1.0 を両方含める")
        return self

    def cost_at(self, demand: Demand) -> float:
        """指定 demand のコストを、隣接する設定点の間で線形補間して返す。"""
        first = self.points[0]
        if demand == first.demand:
            return first.acoustic_cost
        for lower, upper in zip(self.points, self.points[1:], strict=True):
            if demand <= upper.demand:
                if demand == upper.demand:
                    return upper.acoustic_cost
                proportion = (demand - lower.demand) / (upper.demand - lower.demand)
                return lower.acoustic_cost + proportion * (
                    upper.acoustic_cost - lower.acoustic_cost
                )
        return self.points[-1].acoustic_cost


class AcousticInteraction(Protocol):
    """複数 Zone 同時運転の追加コストを差し替える読み取り専用の契約。"""

    @property
    def name(self) -> str:
        """decision trace に残せる interaction の識別子を返す。"""

    def cost_for(self, demands: PerZone[Demand]) -> float:
        """要求 demand から無単位の追加コストを返す。"""


class AcousticCostMetadata(_Frozen):
    """推定値ではないことと、再現に必要な入力情報を伝える metadata。"""

    model_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=120)
    source: AcousticModelSource
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    interaction_names: tuple[str, ...]


class AcousticCostEstimate(_Frozen):
    """MPC が任意で読む Zone 別・合計の音響コスト。物理音圧は表さない。"""

    acoustic_cost: FiniteCost
    zone_costs: PerZone[FiniteCost]
    interaction_cost: FiniteCost
    metadata: AcousticCostMetadata

    @model_validator(mode="after")
    def _total_matches_components(self) -> Self:
        expected = (
            self.zone_costs.front
            + self.zone_costs.rear
            + self.zone_costs.top
            + self.interaction_cost
        )
        if self.acoustic_cost != expected:
            raise ValueError("acoustic_cost は zone_costs と interaction_cost の合計にする")
        return self


class AcousticModelConfig(_Frozen):
    """実測前の近似と将来の実測モデルが共有する設定形式。"""

    schema_version: Literal[1]
    model_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=120)
    source: AcousticModelSource
    zones: PerZone[ZoneAcousticCurve]

    @classmethod
    def from_file(cls, path: Path) -> tuple[AcousticModelConfig, str]:
        """設定を読み、検証済みモデル設定と内容ハッシュを返す。"""
        text = path.read_text(encoding="utf-8")
        loaded: Any = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"音響設定が辞書ではない: {path.name}")
        return cls.model_validate(loaded), sha256(text.encode("utf-8")).hexdigest()


class AcousticCostModel(Protocol):
    """MPC が optional に依存できる、demand を変更しない音響コスト API。"""

    def estimate(self, demands: PerZone[Demand]) -> AcousticCostEstimate | None:
        """コストが利用可能なら返し、無効時は ``None`` を返す。"""


class ConfiguredAcousticCostModel:
    """設定済み Zone 曲線と任意 interaction を評価する初期実装。"""

    def __init__(
        self,
        config: AcousticModelConfig,
        config_sha256: str,
        *,
        interactions: tuple[AcousticInteraction, ...] = (),
    ) -> None:
        self._config = config
        self._interactions = interactions
        self._metadata = AcousticCostMetadata(
            model_id=config.model_id,
            source=config.source,
            config_sha256=config_sha256,
            interaction_names=tuple(interaction.name for interaction in interactions),
        )

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        interactions: tuple[AcousticInteraction, ...] = (),
    ) -> ConfiguredAcousticCostModel:
        """YAML 設定から読み取り専用モデルを作る。"""
        config, config_sha256 = AcousticModelConfig.from_file(path)
        return cls(config, config_sha256, interactions=interactions)

    def estimate(self, demands: PerZone[Demand]) -> AcousticCostEstimate:
        """Zone 曲線と追加 interaction を足した無単位コストを返す。"""
        zone_costs = PerZone[FiniteCost](
            front=self._config.zones.front.cost_at(demands.front),
            rear=self._config.zones.rear.cost_at(demands.rear),
            top=self._config.zones.top.cost_at(demands.top),
        )
        interaction_cost = sum(interaction.cost_for(demands) for interaction in self._interactions)
        if interaction_cost < 0.0:
            raise ValueError("Acoustic interaction は負のコストを返せない")
        return AcousticCostEstimate(
            acoustic_cost=zone_costs.front + zone_costs.rear + zone_costs.top + interaction_cost,
            zone_costs=zone_costs,
            interaction_cost=interaction_cost,
            metadata=self._metadata,
        )


class DisabledAcousticCostModel:
    """音響コストを目的関数へ入れない場合の明示的な optional 実装。"""

    def estimate(self, demands: PerZone[Demand]) -> None:
        """モデルが無効なので常に ``None`` を返す。"""
        del demands
        return None


def load_acoustic_model(
    directory: Path,
    *,
    interactions: tuple[AcousticInteraction, ...] = (),
) -> ConfiguredAcousticCostModel:
    """規定ファイル名の検証済み音響モデルを読み込む。"""
    return ConfiguredAcousticCostModel.from_file(
        directory / ACOUSTIC_CONFIG_FILENAME,
        interactions=interactions,
    )
