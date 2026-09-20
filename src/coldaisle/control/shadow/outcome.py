"""記録した予測を、あとから届いた実測と突き合わせる（#90 / 決定記録 0053 §2.3）。

**この module は時計を持たない。** 照合に使う時刻は、記録した予測自身の ``expected_ts_ms`` と
観測の ``ts_ms`` だけで、処理した時刻は一切使わない。処理時刻が混ざると、遅れて流し込んだ
古い観測が「新しい証拠」になり、あとからいくらでも当たりに見せられる。

照合の規則は Dataset の target 選択（決定記録 0031 §2.2）と Residual drift（0050 §2.2）に揃える。

- 期待時刻に**最も近い**観測を、``±match_tolerance_ms`` の中でだけ採る。**同距離なら過去側**
- 観測は予測の元になった action より後に限る
- **品質が OK の値だけ**を証拠にする。stale / suspect / missing は「当たった」に数えない

**採点してよいのは、予測した候補 action が実際に掛かっていた区間だけ**（0053 §2.3）。
counterfactual の予測は「その plan を実行したら」の予測なので、実際には Fallback（や Guard /
Safety が決めた別の値）が掛かっていた区間の実測と引き算しても、出てくるのは
**制御器の違いとモデル誤差が混ざった量**である。掛かっていた action が plan と違う場合は
``unidentifiable`` として、観測は残したまま**誤差を出さない**。
ここで因果推定（反実仮想の補正）はしない。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.schema import (
    Demand,
    PerZone,
    Reason,
    ShadowActionPlan,
    ShadowMetricName,
    ShadowPrediction,
    Zone,
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


class OutcomeStatus(StrEnum):
    """1つの予測に対する照合結果の区分（#91 が読み分ける）。"""

    SCORED = "scored"
    """予測した候補 action が実際に掛かっていた。誤差をモデル誤差として読める。"""
    UNIDENTIFIABLE = "unidentifiable"
    """掛かっていた action が plan と違う（または記録が無い）。**誤差を出さない。**"""


class OutcomeMatch(_Frozen):
    """1つの予測出力（step × metric）と実測の突き合わせ結果。

    状態は3つあり、**混ぜない**。

    - 照合できて採点した: ``observed`` と ``error`` を持つ
    - 照合できたが採点していない: ``observed`` だけを持つ（``unidentifiable`` な予測）
    - 照合できなかった: ``unmatched`` に理由だけを持つ
    """

    offset_ms: int = Field(gt=0)
    expected_ts_ms: int = Field(ge=0)
    metric: ShadowMetricName
    predicted: float = Field(allow_inf_nan=False)
    observed: float | None = Field(default=None, allow_inf_nan=False)
    observed_ts_ms: int | None = Field(default=None, ge=0)
    error: float | None = Field(default=None, allow_inf_nan=False)
    """予測誤差。**採点してよい予測のときだけ**入る。"""
    unmatched: Reason | None = None
    """照合できなかった理由。**埋め合わせの値は入れない。**"""

    @model_validator(mode="after")
    def _matched_outputs_carry_their_evidence(self) -> Self:
        matched = self.observed is not None
        if matched != (self.observed_ts_ms is not None):
            raise ValueError("照合できた出力は観測値と観測時刻を揃って持つ")
        if matched == (self.unmatched is not None):
            raise ValueError("照合できた出力に理由を付けず、できなかった出力には必ず付ける")
        if self.error is not None:
            if not matched:
                raise ValueError("照合できていない出力に誤差を付けない")
            assert self.observed is not None
            if self.error != self.observed - self.predicted:
                raise ValueError("誤差は observed - predicted にする")
        return self


class ShadowOutcome(_Frozen):
    """1つの予測に対する照合結果。**予測と同じ推論・同じ候補 plan に束ねて持つ。**"""

    inference_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_action_ts_ms: int = Field(ge=0)
    match_tolerance_ms: int = Field(ge=0)
    status: OutcomeStatus
    unidentifiable: Reason | None = None
    """採点できない理由（掛かっていた action が plan と違う / 記録が無い）。"""
    matches: tuple[OutcomeMatch, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _scoring_follows_the_applied_action(self) -> Self:
        if (self.status is OutcomeStatus.UNIDENTIFIABLE) != (self.unidentifiable is not None):
            raise ValueError("採点できない結果にだけ理由を付ける")
        if self.status is OutcomeStatus.UNIDENTIFIABLE:
            if any(item.error is not None for item in self.matches):
                # 別の action の結果との差を「予測誤差」として残さない（0053 §2.3）。
                raise ValueError("採点できない予測に誤差を残さない")
            return self
        if any((item.error is None) != (item.observed is None) for item in self.matches):
            raise ValueError("採点する予測では、照合できた出力に必ず誤差を付ける")
        return self

    @property
    def scored(self) -> bool:
        """予測誤差として読んでよい結果か。"""
        return self.status is OutcomeStatus.SCORED

    @property
    def matched(self) -> int:
        """実測と突き合わせられた出力の数。**採点したかどうかとは別。**"""
        return sum(1 for item in self.matches if item.observed is not None)

    @property
    def complete(self) -> bool:
        """すべての出力を実測と突き合わせられたか。"""
        return self.matched == len(self.matches)


class AppliedInterval(_Frozen):
    """ある区間に**掛かっていた** effective demand の全体。

    demand は次の tick まで掛かり続けるので、区間の中の記録だけを見ると取りこぼす。
    区間の先頭が tick と揃っていなければ、そこから最初の記録までは**直前の tick の値**が
    掛かっている（``carried_in``）。
    """

    carried_in: tuple[int, PerZone[Demand]] | None = None
    """区間の直前に記録された適用値（時刻付き）。区間の先頭まで掛かり続けていたもの。"""
    inside: tuple[tuple[int, PerZone[Demand]], ...] = ()
    """区間 ``[start, end)`` の中に記録がある適用値（時刻順）。"""

    def in_force(self, *, start_ms: int) -> tuple[tuple[int, PerZone[Demand]], ...] | None:
        """区間に掛かっていた値を時刻順に返す。先頭の値が分からなければ None。"""
        if self.inside and self.inside[0][0] == start_ms:
            # 区間の先頭に記録がある。直前の値はそこで置き換わっている。
            return self.inside
        if self.carried_in is None:
            return None
        return (self.carried_in, *self.inside)


class AppliedActionTimeline:
    """各 tick で**実際に掛かった** effective demand の列（#82 の decision trace から作る）。

    counterfactual の予測を採点してよいかは、この列だけで決める。推定も補間もしない。
    ただし **demand は次の tick まで掛かり続ける**ので、区間の中の記録だけでなく、
    直前の tick から持ち越された値も合わせて見る（``AppliedInterval``）。
    """

    __slots__ = ("_applied", "_timestamps")

    def __init__(self, applied: Iterable[tuple[int, PerZone[Demand]]]) -> None:
        points = sorted(applied, key=lambda item: item[0])
        self._applied = points
        self._timestamps = [ts_ms for ts_ms, _demands in points]

    def covering(self, *, start_ms: int, end_ms: int) -> AppliedInterval:
        """半開区間 ``[start, end)`` に掛かっていた適用値を、持ち越し分とともに返す。"""
        low = bisect_left(self._timestamps, start_ms)
        high = bisect_left(self._timestamps, end_ms)
        return AppliedInterval(
            carried_in=self._applied[low - 1] if low > 0 else None,
            inside=tuple(self._applied[low:high]),
        )


class ObservationIndex:
    """metric ごとに時刻順へ並べ直した実測。**1回だけ作って使い回す。**

    予測1件ごとに観測の履歴を並べ直すと、走査が「その tick の候補の数 × 履歴の長さ」に
    比例して伸びる。ここで metric ごとに1回だけ並べ、照合は期待時刻の周り
    （``±tolerance``）だけを二分探索で切り出して見る。**規則は変えない。**

    同じ metric・同じ時刻の観測が2つあれば、**値の小さいほうを採る**。どちらでも誤差の
    扱いは同じだが、入力の順序で結果が変わらないように順序を固定しておく。
    """

    __slots__ = ("_by_metric",)

    def __init__(self, observations: Iterable[OutcomeObservation]) -> None:
        collected: dict[str, list[tuple[int, float]]] = {}
        for observation in observations:
            value = observation.usable
            if value is None:
                # 品質が OK でない値は「当たった証拠」に数えない。索引にも入れない。
                continue
            collected.setdefault(observation.metric, []).append((observation.ts_ms, value))
        # 時刻の列も**ここで一度だけ**作る。照合のたびに作り直すと、二分探索の意味が無くなる。
        self._by_metric: dict[str, tuple[list[int], list[tuple[int, float]]]] = {}
        for metric, points in collected.items():
            points.sort()
            self._by_metric[metric] = ([point[0] for point in points], points)

    def nearest(
        self, metric: str, *, expected_ts_ms: int, tolerance_ms: int, after_ts_ms: int
    ) -> tuple[int, float] | None:
        """期待時刻に最も近い観測を返す。**同距離なら過去側**、無ければ None。"""
        found = self._by_metric.get(metric)
        if found is None:
            return None
        timestamps, points = found
        low = bisect_left(timestamps, expected_ts_ms - tolerance_ms)
        high = bisect_right(timestamps, expected_ts_ms + tolerance_ms)
        best: tuple[int, int, float] | None = None
        for ts_ms, value in points[low:high]:
            if ts_ms <= after_ts_ms:
                # 予測の元になった action より後の観測だけが、その action の効果を含む。
                continue
            distance = abs(ts_ms - expected_ts_ms)
            # 時刻の昇順に見るので、同距離なら先に見た（過去側の）候補が残る。
            if best is None or distance < best[0]:
                best = (distance, ts_ms, value)
        return None if best is None else (best[1], best[2])


class ShadowOutcomeMatcher:
    """記録済みの予測に実測を突き合わせ、**採点してよいときだけ**誤差を出す。

    **時計も I/O も持たない。** 設定の2つの許容幅だけを持ち、同じ入力からは必ず同じ結果を返す。
    """

    def __init__(self, *, match_tolerance_ms: int, applied_demand_tolerance: float) -> None:
        if match_tolerance_ms < 0:
            raise ValueError("照合の許容幅は負にできない")
        if not 0.0 <= applied_demand_tolerance < 1.0:
            # 1.0 はどんな適用値も「plan どおり」にしてしまう（識別の判定が意味を失う）。
            raise ValueError("適用 demand の許容幅は 0 以上 1 未満にする")
        self._tolerance_ms = match_tolerance_ms
        self._demand_tolerance = applied_demand_tolerance

    @property
    def match_tolerance_ms(self) -> int:
        """設定した照合の許容幅。"""
        return self._tolerance_ms

    @property
    def applied_demand_tolerance(self) -> float:
        """適用値と plan を同じとみなす zone ごとの許容幅。"""
        return self._demand_tolerance

    def match(
        self,
        prediction: ShadowPrediction,
        observations: Sequence[OutcomeObservation] | ObservationIndex,
        *,
        plan: ShadowActionPlan,
        applied: AppliedActionTimeline,
    ) -> ShadowOutcome:
        """予測の全出力を実測と突き合わせる。照合できない出力は理由付きで残す。

        ``plan`` はこの予測が前提にした候補 action 列、``applied`` は実際に掛かった値の列で、
        **両方を必ず要求する**。「実行されたか分からないまま採点する」呼び方を作らない。
        多くの予測を続けて照合するときは ``ObservationIndex`` を一度作って渡す。
        """
        shortest_offset_ms = prediction.targets[0].offset_ms
        if self._tolerance_ms >= shortest_offset_ms:
            # 許容幅が1 step に届くと、別の step（別の候補 action の効果）の実測を
            # 「この予測が当たった証拠」に数えてしまう。
            raise ShadowOutcomeUnusableError(
                f"照合の許容幅={self._tolerance_ms} が最短 offset={shortest_offset_ms} 以上"
            )
        if plan.digest() != prediction.plan_digest:
            # 別の候補の plan で識別を判定させない（決定記録 0052 §2.2 の識別子で閉じる）。
            raise ShadowOutcomeUnusableError("予測と別の候補 plan で識別を判定しようとしている")
        unidentifiable = self._unidentifiable_reason(prediction, plan, applied)
        index = (
            observations
            if isinstance(observations, ObservationIndex)
            else ObservationIndex(observations)
        )
        matches = tuple(
            self._match_output(
                prediction,
                offset_ms=target.offset_ms,
                expected_ts_ms=target.expected_ts_ms,
                metric=metric,
                predicted=value,
                index=index,
                scored=unidentifiable is None,
            )
            for target in prediction.targets
            for metric, value in sorted(target.values.items())
        )
        return ShadowOutcome(
            inference_id=prediction.inference_id,
            plan_digest=prediction.plan_digest,
            input_action_ts_ms=prediction.input_action_ts_ms,
            match_tolerance_ms=self._tolerance_ms,
            status=(
                OutcomeStatus.SCORED if unidentifiable is None else OutcomeStatus.UNIDENTIFIABLE
            ),
            unidentifiable=unidentifiable,
            matches=matches,
        )

    def _unidentifiable_reason(
        self,
        prediction: ShadowPrediction,
        plan: ShadowActionPlan,
        applied: AppliedActionTimeline,
    ) -> Reason | None:
        """予測した候補 action が**実際に掛かっていたか**を、記録だけで判定する。

        step ``i`` が覆うのは ``[action + offset[i-1], action + offset[i])``（``offset[0]`` の手前は
        action 時刻）。その区間に記録がある tick の effective demand が、zone ごとに plan の
        値と許容幅の中で一致していれば、その step は「plan どおり実行された」とみなす。

        - 区間に tick の記録が1つも無ければ、実行されたと**言えない**（採点しない）
        - 区間の先頭が tick と揃っていなければ、最初の記録までは**直前の tick の demand**が
          掛かっている。持ち越し分も同じ規則で照らす（区間の中だけを見ると取りこぼす）
        - 1つでも違う値が掛かっていれば、その差は制御器の違いであってモデル誤差ではない
        """
        action_ts_ms = prediction.input_action_ts_ms
        previous_offset_ms = 0
        for step in plan.steps:
            start_ms = action_ts_ms + previous_offset_ms
            end_ms = action_ts_ms + step.offset_ms
            previous_offset_ms = step.offset_ms
            interval = applied.covering(start_ms=start_ms, end_ms=end_ms)
            if not interval.inside:
                # 区間の中に記録が無い。制御ループが回っていた証拠が無いので採点しない。
                return Reason(
                    code="applied_action_unknown",
                    detail=f"[{start_ms}, {end_ms}) に適用 demand の記録が無い",
                )
            in_force = interval.in_force(start_ms=start_ms)
            if in_force is None:
                return Reason(
                    code="applied_action_unknown",
                    detail=f"{start_ms} の時点で掛かっていた demand の記録が無い",
                )
            for ts_ms, demands in in_force:
                for zone in Zone:
                    planned = step.demands.get(zone)
                    actual = demands.get(zone)
                    if abs(actual - planned) > self._demand_tolerance:
                        return Reason(
                            code="applied_action_differs",
                            detail=(
                                f"zone={zone.value}; ts_ms={ts_ms}; "
                                f"carried_in={ts_ms < start_ms}; "
                                f"applied={actual:.6f}; planned={planned:.6f}; "
                                f"tolerance={self._demand_tolerance:.6f}"
                            ),
                        )
        return None

    def _match_output(
        self,
        prediction: ShadowPrediction,
        *,
        offset_ms: int,
        expected_ts_ms: int,
        metric: str,
        predicted: float,
        index: ObservationIndex,
        scored: bool,
    ) -> OutcomeMatch:
        best = index.nearest(
            metric,
            expected_ts_ms=expected_ts_ms,
            tolerance_ms=self._tolerance_ms,
            after_ts_ms=prediction.input_action_ts_ms,
        )
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
        observed_ts_ms, observed = best
        return OutcomeMatch(
            offset_ms=offset_ms,
            expected_ts_ms=expected_ts_ms,
            metric=metric,
            predicted=predicted,
            observed=observed,
            observed_ts_ms=observed_ts_ms,
            # **観測は残し、採点だけを止める。** 別の action の結果との差は予測誤差ではない。
            error=(observed - predicted) if scored else None,
        )
