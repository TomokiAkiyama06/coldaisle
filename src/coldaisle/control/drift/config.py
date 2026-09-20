"""Model Drift 検知の設定（`config/drift.yaml`。#93 / 決定記録 0056 §2.8）。

**既定値を置かない。** 実測前の暫定値はすべて `{value, status: provisional}` で持ち、
設定が欠けていれば読み込みで落とす（AGENTS.md ルール9）。

**ほかの契約が持っている値をここへ写さない**（0056 §2.2 / §2.8）。

- 範囲・support・欠測の閾値は `fan-policy.yaml` の `model_confidence`
- 正規化に使う residual の基準は `ModelConfidenceProfile`（validation residual RMS）
- 照合の許容幅は `fan-policy.yaml` の `shadow`

写すと、runtime と offline が別の契約で動く余地ができる。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from coldaisle.control.config import ConfigValue, DriftRatio, PositiveCount, UnitInterval

DRIFT_CONFIG_VERSION: Literal[1] = 1
DRIFT_CONFIG_FILENAME = "drift.yaml"

DriftCount = ConfigValue[PositiveCount]
DriftFraction = ConfigValue[UnitInterval]
DriftRatioValue = ConfigValue[DriftRatio]


class _ConfigModel(BaseModel):
    """設定を欠損・余分な鍵・暗黙変換から守る共通基底。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ResidualDriftGate(_ConfigModel):
    """prediction residual の悪化をどこで warning / degraded と呼ぶか（0056 §2.4）。

    比の基準は **Profile の validation residual RMS**（`ratio = 1.0` が学習時の水準）である。
    """

    minimum_scored_outcomes: DriftCount
    """比に数えられた outcome の下限。これに満たなければ**比を出さない**。"""
    minimum_identifiable_fraction: DriftFraction
    """`scored / outcomes` の下限。採点できた僅かな区間だけで drift を語らせない。"""
    warning_ratio: DriftRatioValue
    degraded_ratio: DriftRatioValue
    """`degraded_ratio` は `model_confidence.residual_drift_ood_ratio` 以下にする。

    値そのものは**写さない**。検知器の生成時に runtime の設定と照合する（0056 §2.8）。
    offline が runtime より鈍いと、runtime が OOD で Fallback へ落ちている最中に
    offline が「問題なし」と言う。
    """
    trend_bucket_outcomes: DriftCount
    """residual trend を切る bucket の大きさ（outcome 件数）。"""
    minimum_bucket_outcomes: DriftCount
    """bucket が比を持つのに要る件数。**足りない bucket は隣から借りない。**"""

    @model_validator(mode="after")
    def _thresholds_are_ordered(self) -> Self:
        if self.warning_ratio.value >= self.degraded_ratio.value:
            raise ValueError("residual.warning_ratio は degraded_ratio 未満にする")
        if self.minimum_bucket_outcomes.value > self.trend_bucket_outcomes.value:
            raise ValueError("residual.minimum_bucket_outcomes は trend_bucket_outcomes 以下にする")
        return self


class InputDriftGate(_ConfigModel):
    """入力分布の逸脱の割合を、どこで warning / degraded と呼ぶか。

    何を「逸脱」と数えるかは **`model_confidence` の閾値**（`range_margin` /
    `min_support_count` / `min_missing_pattern_count`）で決まる。ここには**割合だけ**を置く。
    """

    warning_fraction: DriftFraction
    degraded_fraction: DriftFraction

    @model_validator(mode="after")
    def _thresholds_are_ordered(self) -> Self:
        if self.warning_fraction.value >= self.degraded_fraction.value:
            raise ValueError("warning_fraction は degraded_fraction 未満にする")
        return self


class DriftConfig(_ConfigModel):
    """Model Drift 検知の設定一式。"""

    schema_version: Literal[1]
    residual: ResidualDriftGate
    minimum_inputs: DriftCount
    """入力分布の判定に要る入力の下限。これに満たなければ**割合を出さない**。"""
    feature_range: InputDriftGate
    support: InputDriftGate
    missing_pattern: InputDriftGate

    @classmethod
    def from_file(cls, path: Path) -> tuple[DriftConfig, str]:
        """設定を読み、検証済みの設定と**その bytes の** SHA-256 を返す。

        報告に残すのは hash だけで、絶対 path は残さない（決定記録 0021）。
        """
        text = path.read_text(encoding="utf-8")
        loaded: Any = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"drift 設定が辞書ではない: {path.name}")
        return cls.model_validate(loaded), sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "DRIFT_CONFIG_FILENAME",
    "DRIFT_CONFIG_VERSION",
    "DriftConfig",
    "InputDriftGate",
    "ResidualDriftGate",
]
