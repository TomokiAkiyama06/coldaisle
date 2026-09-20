"""評価の要約統計（#91）。**決定論的で、時計も I/O も持たない。**

percentile は **nearest-rank** で決める。補間すると、同じ入力でも足し算の順序で
末尾が揺れることがあり、「同じ入力からは同じ bytes」（決定記録 0054 §2.7）が崩れる。
順位で選べば、出てくるのは必ず入力に実在した値である。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import pairwise
from math import ceil

from pydantic import BaseModel, ConfigDict, Field

MILLISECONDS_PER_HOUR = 3_600_000


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class PercentileValue(_Frozen):
    """`quantile` の nearest-rank 値。"""

    quantile: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    value: float = Field(allow_inf_nan=False)


class MetricSummary(_Frozen):
    """1つの量の分布。**値が1つも無ければ作れない**（空を 0 として扱わないため）。"""

    count: int = Field(ge=1)
    mean: float = Field(allow_inf_nan=False)
    minimum: float = Field(allow_inf_nan=False)
    maximum: float = Field(allow_inf_nan=False)
    percentiles: tuple[PercentileValue, ...] = ()


def summarize(values: Iterable[float], *, quantiles: Sequence[float] = ()) -> MetricSummary | None:
    """値の分布を要約する。**値が無ければ `None`**（0 で埋めない）。"""
    collected = sorted(values)
    if not collected:
        return None
    return MetricSummary(
        count=len(collected),
        mean=sum(collected) / len(collected),
        minimum=collected[0],
        maximum=collected[-1],
        percentiles=tuple(
            PercentileValue(quantile=quantile, value=_nearest_rank(collected, quantile))
            for quantile in quantiles
        ),
    )


def _nearest_rank(ordered: Sequence[float], quantile: float) -> float:
    """昇順の列から nearest-rank percentile を返す。"""
    rank = max(1, min(len(ordered), ceil(quantile * len(ordered))))
    return ordered[rank - 1]


class SeriesShape(_Frozen):
    """時系列の変動量とハンチング。

    `reversals` は「不感帯を超える変化の**向きが反転した**回数」である。
    不感帯以下の変化は向きを持たないものとして読み飛ばす（測定ノイズを反転に数えない）。
    """

    samples: int = Field(ge=1)
    first_ts_ms: int = Field(ge=0)
    last_ts_ms: int = Field(ge=0)
    level: MetricSummary
    step: MetricSummary | None = None
    """隣り合う記録の変化の大きさ（絶対値）。記録が1つなら `None`。"""
    reversals: int = Field(ge=0)
    reversals_per_hour: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    """**時間の幅が 0 のときは `None`。** 0 除算を大きな数で埋め合わせない。"""


def shape_of(
    points: Sequence[tuple[int, float]],
    *,
    deadband: float,
    quantiles: Sequence[float] = (),
) -> SeriesShape | None:
    """時刻付きの列から変動量とハンチングを出す。**点が無ければ `None`。**

    入力は時刻の昇順であることを前提にしない。ここで並べ替えてから数える。
    """
    if not points:
        return None
    ordered = sorted(points)
    level = summarize((value for _ts_ms, value in ordered), quantiles=quantiles)
    assert level is not None  # ordered が空でないので必ず値がある
    steps = [
        abs(later - earlier) for (_earlier_ts, earlier), (_later_ts, later) in pairwise(ordered)
    ]
    span_ms = ordered[-1][0] - ordered[0][0]
    return SeriesShape(
        samples=len(ordered),
        first_ts_ms=ordered[0][0],
        last_ts_ms=ordered[-1][0],
        level=level,
        step=summarize(steps, quantiles=quantiles),
        reversals=_reversals(ordered, deadband=deadband),
        reversals_per_hour=(
            None
            if span_ms <= 0
            else _reversals(ordered, deadband=deadband) * MILLISECONDS_PER_HOUR / span_ms
        ),
    )


def _reversals(ordered: Sequence[tuple[int, float]], *, deadband: float) -> int:
    """不感帯を超える変化の向きが反転した回数。"""
    reversals = 0
    direction = 0
    for (_earlier_ts, earlier), (_later_ts, later) in pairwise(ordered):
        change = later - earlier
        if abs(change) <= deadband:
            # 不感帯の中の揺れは向きを持たない。直前の向きをそのまま保つ。
            continue
        current = 1 if change > 0.0 else -1
        if direction != 0 and current != direction:
            reversals += 1
        direction = current
    return reversals
