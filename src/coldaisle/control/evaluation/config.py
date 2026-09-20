"""Offline Evaluation の設定（`config/evaluation.yaml`。#91 / 決定記録 0054 §2.8）。

**既定値を置かない。** 実測前の暫定値はすべて `{value, status: provisional}` で持ち、
設定が欠けていれば読み込みで落とす。コード側の既定値があると、設定を忘れた評価が
「どこかで決めた数字」で通ってしまう（AGENTS.md ルール9）。

**ほかの契約が持っている値をここへ写さない**（0054 §2.6）。

- 絶対温度上限は `safety.yaml` の `absolute_temp_ceiling_c`
- ΔT の式は `config/metrics.yaml` の `derived`
- 照合の許容幅は `fan-policy.yaml` の `shadow`

写すと、評価だけが別の契約で動く余地ができる。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle.control.config import ConfigValue, FiniteFloat, PositiveMilliseconds, UnitInterval
from coldaisle.control.schema import ShadowMetricName

EVALUATION_CONFIG_VERSION: Literal[1] = 1
EVALUATION_CONFIG_FILENAME = "evaluation.yaml"

MAX_EVALUATION_METRICS = 32
"""1回の評価で扱う metric 数の上限。

**写している契約と同じ値にする。** 予測が持てる metric 数（`MAX_SHADOW_PREDICTION_METRICS`）と
同じで、ここだけ狭いと「記録できた予測を評価できない」組み合わせが生まれる。
一致は試験で確かめる。
"""

EvalFloat = ConfigValue[FiniteFloat]
EvalMilliseconds = ConfigValue[PositiveMilliseconds]
EvalUnitInterval = ConfigValue[UnitInterval]
EvalCount = ConfigValue[Annotated[int, Field(ge=0)]]

UNKNOWN_GROUP = "unknown"
"""帯や regime を決められなかった tick の区分。**黙って落とさずにここへ入れる。**"""

DerivedMetricName = Annotated[str, Field(pattern=r"^d\.[a-z][a-z0-9_]*$", max_length=120)]
"""派生 ΔT の名前（`config/metrics.yaml` の `derived` の鍵）。"""


def _sequence_to_tuple(value: object) -> object:
    """YAML 配列を、読み込み後は不変な tuple として保持する。"""
    return tuple(value) if isinstance(value, list) else value


class _ConfigModel(BaseModel):
    """設定を欠損・余分な鍵・暗黙変換から守る共通基底。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RoomTemperatureBand(_ConfigModel):
    """室温の帯（0054 §2.5 の「室温帯ごとに評価する」）。

    帯は**順に並べ、最後の1つだけ上限を持たない**。上限のある帯を最後に置くと、
    その上の室温が「どの帯にも入らない」まま黙って落ちる。
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=32)
    below_c: EvalFloat | None = None
    """この値**未満**をこの帯とする。最後の帯では `None`（上限なし）。"""


class HuntingConfig(_ConfigModel):
    """ハンチング（向きの反転）を数えるときの不感帯。"""

    demand_deadband: ConfigValue[UnitInterval]
    """これ以下の demand の変化は「向き」を持たないとみなす。"""
    rpm_deadband: EvalFloat
    """これ以下の RPM の変化は「向き」を持たないとみなす。"""

    @model_validator(mode="after")
    def _deadbands_are_not_negative(self) -> Self:
        if self.rpm_deadband.value < 0.0:
            raise ValueError("hunting.rpm_deadband は 0 以上にする")
        if self.demand_deadband.value >= 1.0:
            # demand の全域を不感帯にすると、どんな往復も反転として数えられなくなる。
            raise ValueError("hunting.demand_deadband は 1.0 未満にする")
        return self


class SafetyGate(_ConfigModel):
    """Safety の gate 条件（0054 §2.4 の第1段）。**worst-case segment で判定する。**"""

    maximum_ceiling_exceedances: EvalCount
    """`safety.absolute_temp_ceiling_c` を超えた観測の許容数。"""
    maximum_emergency_ticks: EvalCount
    maximum_fault_ticks: EvalCount
    minimum_threshold_margin_c: EvalFloat
    """絶対上限までの最小余裕。これを下回る segment があれば `blocked`。"""
    maximum_floor_shortfalls: EvalCount
    """適用されなかった提案が、記録された Critical Safety floor を下回った (tick, zone) の許容数。

    実行されていない提案について**記録から言える安全側の指標**である。
    Safety が引き上げたはずの要求を何度出したかを数える（決定記録 0054 §2.4）。
    """


class EvidenceGate(_ConfigModel):
    """証拠の量の gate 条件（0054 §2.3 / §2.4 の第2段）。"""

    minimum_identifiable_fraction: EvalUnitInterval
    """`scored / outcomes`。これに満たない counterfactual の予測指標は出さない。"""
    minimum_scored_outcomes: EvalCount
    """採点できた outcome の最小数。少数の当たりを根拠にしない。"""


class CostGate(_ConfigModel):
    """cost の gate 条件（0054 §2.4 の第3段）。**Safety を相殺しない。**"""

    maximum_underprediction_c: EvalFloat
    """scored な予測の最大 underprediction（実測が予測より高い向き）。"""
    maximum_optimizer_timeout_rate: EvalUnitInterval
    maximum_optimizer_latency_ms: EvalMilliseconds
    """optimizer latency の最大値（`mpc.optimizer` の予算とは別の、評価上の許容値）。"""

    @model_validator(mode="after")
    def _underprediction_limit_is_positive(self) -> Self:
        if self.maximum_underprediction_c.value <= 0.0:
            raise ValueError("gate.cost.maximum_underprediction_c は正にする")
        return self


class GateConfig(_ConfigModel):
    """rollout gate の3段（0054 §2.4）。**段を混ぜて合成スコアにしない。**"""

    safety: SafetyGate
    evidence: EvidenceGate
    cost: CostGate


class EvaluationConfig(_ConfigModel):
    """Offline Evaluation の設定一式。"""

    schema_version: Literal[1]
    temperature_metrics: Annotated[
        tuple[ShadowMetricName, ...],
        BeforeValidator(_sequence_to_tuple),
        Field(min_length=1, max_length=MAX_EVALUATION_METRICS),
    ]
    """平均・percentile・最大と threshold margin を出す温度 metric。"""
    delta_metrics: Annotated[
        tuple[DerivedMetricName, ...],
        BeforeValidator(_sequence_to_tuple),
        Field(max_length=MAX_EVALUATION_METRICS),
    ] = ()
    """ΔT（GPU 吸気上昇・ケース内予熱など）。式は `config/metrics.yaml` から引く。"""
    room_temperature_metric: ShadowMetricName
    """室温帯を決める metric。"""
    observation_match_tolerance_ms: EvalMilliseconds
    """観測を tick へ結び付けるときの許容幅。

    **1つの規則をすべての metric に使う。** metric ごとに別の幅を持つと、
    同じ tick に別の時刻の証拠が集まり、group の切り分けが metric ごとに変わる。
    この幅に入る tick が無い観測は、どの arm にも帰属させない（理由を残して数える）。
    """
    percentiles: Annotated[
        tuple[UnitInterval, ...],
        BeforeValidator(_sequence_to_tuple),
        Field(min_length=1, max_length=8),
    ]
    """出力する上位 percentile（0.0..1.0）。**nearest-rank で計算する。**"""
    room_temperature_bands: Annotated[
        tuple[RoomTemperatureBand, ...],
        BeforeValidator(_sequence_to_tuple),
        Field(min_length=1, max_length=8),
    ]
    hunting: HuntingConfig
    worst_case_count: Annotated[int, Field(ge=1, le=100)]
    """worst-case として必ず並べる件数（0054 §2.4）。"""
    gate: GateConfig

    @model_validator(mode="after")
    def _bands_and_percentiles_are_ordered(self) -> Self:
        bands = self.room_temperature_bands
        previous: float | None = None
        for band in bands[:-1]:
            if band.below_c is None:
                # 上限のない帯が途中にあると、その先の帯へは誰も入らない。
                raise ValueError("上限のない室温帯は最後の1つだけにする")
            if previous is not None and band.below_c.value <= previous:
                raise ValueError("room_temperature_bands の below_c は単調増加にする")
            previous = band.below_c.value
        if bands[-1].below_c is not None:
            raise ValueError("最後の室温帯は below_c を持たない（上限なし）")
        names = tuple(band.name for band in bands)
        if len(set(names)) != len(names):
            raise ValueError("room_temperature_bands の name を重複させない")
        if len(set(self.temperature_metrics)) != len(self.temperature_metrics):
            raise ValueError("temperature_metrics を重複させない")
        if len(set(self.delta_metrics)) != len(self.delta_metrics):
            raise ValueError("delta_metrics を重複させない")
        quantiles = self.percentiles
        if tuple(sorted(set(quantiles))) != quantiles:
            raise ValueError("percentiles は重複なし昇順にする")
        if any(quantile <= 0.0 for quantile in quantiles):
            raise ValueError("percentiles は 0 より大きい値にする")
        return self

    @classmethod
    def from_file(cls, path: Path) -> tuple[EvaluationConfig, str]:
        """設定を読み、検証済みの設定と**その bytes の** SHA-256 を返す。

        報告に残すのは hash だけで、絶対 path は残さない（決定記録 0021）。
        """
        text = path.read_text(encoding="utf-8")
        loaded: Any = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"評価設定が辞書ではない: {path.name}")
        return cls.model_validate(loaded), sha256(text.encode("utf-8")).hexdigest()

    def band_for(self, room_temperature_c: float | None) -> str:
        """室温を帯の名前へ写す。**観測が無ければ `unknown`。**

        帯に入らない室温は作れない（最後の帯が上限を持たないため）。
        """
        if room_temperature_c is None:
            return UNKNOWN_GROUP
        for band in self.room_temperature_bands:
            if band.below_c is None or room_temperature_c < band.below_c.value:
                return band.name
        raise AssertionError("最後の帯が上限を持たないので、ここへは来ない")
