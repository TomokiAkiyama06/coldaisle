"""記録した予測を、あとから届いた実測と突き合わせる（#90 / 決定記録 0053 §2.3）。

**この module は時計を持たない。** 照合に使う時刻は、記録した予測自身の ``expected_ts_ms`` と
観測の ``ts_ms`` だけで、処理した時刻は一切使わない。処理時刻が混ざると、遅れて流し込んだ
古い観測が「新しい証拠」になり、あとからいくらでも当たりに見せられる。

照合の規則は Dataset の target 選択（決定記録 0031 §2.2）と Residual drift（0050 §2.2）に揃える。

- 期待時刻に**最も近い**観測を、``±match_tolerance_ms`` の中でだけ採る。**同距離なら過去側**
- 観測は予測の元になった action より後に限る
- **品質が OK の値だけ**を証拠にする。stale / suspect / missing は「当たった」に数えない
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.schema import (
    Reason,
    ShadowMetricName,
    ShadowPrediction,
)
from coldaisle.store.models import Quality


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ShadowOutcomeUnusableError(ValueError):
    """予測と照合設定が噛み合わないため、実測と突き合わせられない。"""


class OutcomeObservation(_Frozen):
    """照合に使う観測1つ。**品質を必ず持つ。**"""

    metric: ShadowMetricName
    ts_ms: int = Field(ge=0)
    value: float | None = Field(default=None, allow_inf_nan=False)
    quality: Quality

    @property
    def usable(self) -> float | None:
        """証拠に使える値。OK で値があるときだけ返す。"""
        return self.value if self.quality is Quality.OK else None


class OutcomeMatch(_Frozen):
    """1つの予測出力（step × metric）と実測の突き合わせ結果。"""

    offset_ms: int = Field(gt=0)
    expected_ts_ms: int = Field(ge=0)
    metric: ShadowMetricName
    predicted: float = Field(allow_inf_nan=False)
    observed: float | None = Field(default=None, allow_inf_nan=False)
    observed_ts_ms: int | None = Field(default=None, ge=0)
    error: float | None = Field(default=None, allow_inf_nan=False)
    unmatched: Reason | None = None
    """照合できなかった理由。**埋め合わせの値は入れない。**"""

    @model_validator(mode="after")
    def _matched_outputs_carry_all_three_values(self) -> Self:
        matched = self.observed is not None
        if matched != (self.observed_ts_ms is not None) or matched != (self.error is not None):
            raise ValueError("照合できた出力は観測値・観測時刻・誤差を揃って持つ")
        if matched == (self.unmatched is not None):
            raise ValueError("照合できた出力に理由を付けず、できなかった出力には必ず付ける")
        if self.observed is not None and self.error != self.observed - self.predicted:
            raise ValueError("誤差は observed - predicted にする")
        return self


class ShadowOutcome(_Frozen):
    """1つの予測に対する照合結果。**予測と同じ推論に束ねて持つ。**"""

    inference_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_action_ts_ms: int = Field(ge=0)
    match_tolerance_ms: int = Field(ge=0)
    matches: tuple[OutcomeMatch, ...] = Field(min_length=1)

    @property
    def matched(self) -> int:
        """実測と突き合わせられた出力の数。"""
        return sum(1 for item in self.matches if item.observed is not None)

    @property
    def complete(self) -> bool:
        """すべての出力を実測と突き合わせられたか。"""
        return self.matched == len(self.matches)


class ShadowOutcomeMatcher:
    """記録済みの予測に実測を突き合わせる。**時計も I/O も持たない。**

    設定の許容幅だけを持ち、同じ入力からは必ず同じ結果を返す。
    """

    def __init__(self, *, match_tolerance_ms: int) -> None:
        if match_tolerance_ms < 0:
            raise ValueError("照合の許容幅は負にできない")
        self._tolerance_ms = match_tolerance_ms

    @property
    def match_tolerance_ms(self) -> int:
        """設定した照合の許容幅。"""
        return self._tolerance_ms

    def match(
        self,
        prediction: ShadowPrediction,
        observations: Sequence[OutcomeObservation],
    ) -> ShadowOutcome:
        """予測の全出力を実測と突き合わせる。照合できない出力は理由付きで残す。"""
        shortest_offset_ms = prediction.targets[0].offset_ms
        if self._tolerance_ms >= shortest_offset_ms:
            # 許容幅が1 step に届くと、別の step（別の候補 action の効果）の実測を
            # 「この予測が当たった証拠」に数えてしまう。
            raise ShadowOutcomeUnusableError(
                f"照合の許容幅={self._tolerance_ms} が最短 offset={shortest_offset_ms} 以上"
            )
        ordered = sorted(observations, key=lambda item: (item.ts_ms, item.metric))
        matches = tuple(
            self._match_output(
                prediction,
                offset_ms=target.offset_ms,
                expected_ts_ms=target.expected_ts_ms,
                metric=metric,
                predicted=value,
                ordered=ordered,
            )
            for target in prediction.targets
            for metric, value in sorted(target.values.items())
        )
        return ShadowOutcome(
            inference_id=prediction.inference_id,
            plan_digest=prediction.plan_digest,
            input_action_ts_ms=prediction.input_action_ts_ms,
            match_tolerance_ms=self._tolerance_ms,
            matches=matches,
        )

    def _match_output(
        self,
        prediction: ShadowPrediction,
        *,
        offset_ms: int,
        expected_ts_ms: int,
        metric: str,
        predicted: float,
        ordered: Sequence[OutcomeObservation],
    ) -> OutcomeMatch:
        best: tuple[int, int, float] | None = None
        for observation in ordered:
            if observation.metric != metric:
                continue
            value = observation.usable
            if value is None or observation.ts_ms <= prediction.input_action_ts_ms:
                continue
            distance = abs(observation.ts_ms - expected_ts_ms)
            if distance > self._tolerance_ms:
                continue
            # 時刻順に見るので、同距離なら先に見た（過去側の）候補を残す。
            if best is None or distance < best[0]:
                best = (distance, observation.ts_ms, value)
        if best is None:
            return OutcomeMatch(
                offset_ms=offset_ms,
                expected_ts_ms=expected_ts_ms,
                metric=metric,
                predicted=predicted,
                unmatched=Reason(
                    code="no_usable_observation",
                    detail=f"expected_ts_ms={expected_ts_ms}; tolerance_ms={self._tolerance_ms}",
                ),
            )
        _distance, observed_ts_ms, observed = best
        return OutcomeMatch(
            offset_ms=offset_ms,
            expected_ts_ms=expected_ts_ms,
            metric=metric,
            predicted=predicted,
            observed=observed,
            observed_ts_ms=observed_ts_ms,
            error=observed - predicted,
        )
