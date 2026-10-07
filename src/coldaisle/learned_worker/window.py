"""frame の列から推論の入力（観測 window と anchor の action）を作る（決定記録 0107 §2.1 / §2.2）。

- 格子は artifact の feature schema のまま。`action_ts_ms` は window に使う最新の frame の
  `snapshot.ts_ms`、格子時刻は Dataset と同じ `range(action_ts_ms - window_ms, action_ts_ms + 1,
  sample_period_ms)`（0031 §2.2）
- 各格子時刻 `t` には、受け取った frame のうち `snapshot.ts_ms <= t` で最新のものを当て、その
  snapshot の signal から Dataset と同じ規則で cell を作る
- **欠けた frame を飛び越えて as-of しない。** 欠けは受け取った frame の `tick_id` の飛びで証明
  できるときだけ。window に使う最新の frame（anchor）には後続を求めない
- anchor の action は直前の tick（`N - 1`）の frame の `applied`（0087 の `prior_action` と
  同じ時点）。無い・欠測（0092）なら作らない（推測で埋めない）

**同じ `run_id` の frame だけを持つ。** `run_id` が変わる・`tick_id` / `snapshot.ts_ms` が戻る
frame を受け取ったら、それより前の列を捨てる（0107 §2.1 / 0101 §2.3）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from coldaisle.control.learned_handoff import LearnedFrame
from coldaisle.control.model.counterfactual import ThermalFeatureSchemaV2
from coldaisle.control.model.thermal import (
    ObservedFanAction,
    ObservedThermalInput,
    ObservedWindowFrame,
)
from coldaisle.control.schema import PerZone, Zone

_STALE = "stale"
_SUSPECT = "suspect"
"""`SnapshotSignal.quality` の値（`Quality` の文字列）。

worker は `coldaisle.store` を import しない。
"""


class WindowUnavailable(StrEnum):
    """推論の入力を作らない理由（決定記録 0107 §2.3。**何も送らない**周期）。"""

    WARMING_UP = "warming_up"
    """格子の先頭に当てる frame がまだ無い（立ち上がり中）。"""
    PRIOR_ACTION_MISSING = "prior_action_missing"
    """anchor の action に要る tick `N - 1` の frame が無い・どれかの zone の `applied` が欠測。"""


@dataclass(frozen=True, slots=True)
class WindowResult:
    """作れた入力か、作らない理由のどちらか一方。"""

    observed: ObservedThermalInput | None
    unavailable: WindowUnavailable | None


class FrameHistory:
    """1つの `run_id` の frame の列（`tick_id` の昇順）。"""

    __slots__ = ("_frames", "_run_id")

    def __init__(self) -> None:
        self._run_id: str | None = None
        self._frames: list[LearnedFrame] = []

    @property
    def run_id(self) -> str | None:
        """持っている列の `run_id`。"""
        return self._run_id

    @property
    def latest(self) -> LearnedFrame | None:
        """最後に受け取った frame。"""
        return self._frames[-1] if self._frames else None

    @property
    def frames(self) -> tuple[LearnedFrame, ...]:
        """持っている列（試験と診断のため）。"""
        return tuple(self._frames)

    def add(self, run_id: str, frame: LearnedFrame) -> None:
        """検証を通った frame を足す。別の `run_id`・戻った frame なら列を作り直す。"""
        if run_id != self._run_id:
            self._run_id = run_id
            self._frames = []
        latest = self.latest
        if latest is not None and (
            frame.snapshot.tick_id <= latest.snapshot.tick_id
            or frame.snapshot.ts_ms <= latest.snapshot.ts_ms
        ):
            # 同じ run_id の中では起きない想定。起きたら安全側＝window の作り直し（0107 §2.1）
            self._frames = []
        self._frames.append(frame)

    def prune(self, window_ms: int | None) -> None:
        """要らなくなった frame を捨てる。

        ``window_ms`` が分かれば、最も古い格子時刻に as-of で当たりうる frame と anchor の action に
        要る `N - 1` までを残す。分からなければ（束縛がまだ無い）`N - 1` と `N` だけを残す。
        """
        if len(self._frames) <= 2:
            return
        if window_ms is None:
            self._frames = self._frames[-2:]
            return
        oldest_grid_ms = self._frames[-1].snapshot.ts_ms - window_ms
        keep_from = 0
        for index in range(len(self._frames) - 1):
            # 次の frame が最も古い格子時刻以前なら、この frame はどの格子にも当たらない
            if self._frames[index + 1].snapshot.ts_ms <= oldest_grid_ms:
                keep_from = index + 1
        keep_from = min(keep_from, len(self._frames) - 2)
        self._frames = self._frames[keep_from:]


def build_observed_input(
    frames: Sequence[LearnedFrame], schema: ThermalFeatureSchemaV2
) -> WindowResult:
    """最新の frame を anchor にした `ObservedThermalInput`（決定記録 0107 §2.1 / §2.2）。"""
    if not frames:
        return WindowResult(observed=None, unavailable=WindowUnavailable.WARMING_UP)
    anchor = frames[-1]
    action_ts_ms = anchor.snapshot.ts_ms
    grid = range(action_ts_ms - schema.window_ms, action_ts_ms + 1, schema.sample_period_ms)
    if frames[0].snapshot.ts_ms > grid[0]:
        return WindowResult(observed=None, unavailable=WindowUnavailable.WARMING_UP)
    action = _prior_action(frames)
    if action is None:
        return WindowResult(observed=None, unavailable=WindowUnavailable.PRIOR_ACTION_MISSING)
    window = tuple(_cell_frame(frames, ts_ms, schema) for ts_ms in grid)
    return WindowResult(
        observed=ObservedThermalInput(action_ts_ms=action_ts_ms, window=window, action=action),
        unavailable=None,
    )


def _prior_action(frames: Sequence[LearnedFrame]) -> PerZone[ObservedFanAction] | None:
    """tick `N - 1` の frame の `applied`。無い・欠測なら None（0107 §2.2 / 0092 §2.2）。"""
    if len(frames) < 2:
        return None
    anchor, prior = frames[-1], frames[-2]
    if prior.snapshot.tick_id != anchor.snapshot.tick_id - 1:
        return None
    front, rear, top = (prior.applied.get(zone) for zone in (Zone.FRONT, Zone.REAR, Zone.TOP))
    if front is None or rear is None or top is None:
        return None
    return PerZone[ObservedFanAction](
        front=ObservedFanAction(effective_demand=front),
        rear=ObservedFanAction(effective_demand=rear),
        top=ObservedFanAction(effective_demand=top),
    )


def _cell_frame(
    frames: Sequence[LearnedFrame], ts_ms: int, schema: ThermalFeatureSchemaV2
) -> ObservedWindowFrame:
    index = _as_of_index(frames, ts_ms)
    # 格子の先頭は呼び出し側で確かめてあるので、as-of の frame は必ずある
    assert index is not None
    if _gap_after(frames, index):
        return _missing_frame(ts_ms, schema)
    signals = frames[index].snapshot.signals_by_metric
    values: dict[str, float | None] = {}
    sources: dict[str, int | None] = {}
    missing: dict[str, bool] = {}
    stale: dict[str, bool] = {}
    suspect: dict[str, bool] = {}
    for metric in schema.metrics:
        signal = signals.get(metric)
        if signal is None:
            values[metric], sources[metric] = None, None
            missing[metric], stale[metric], suspect[metric] = True, False, False
            continue
        value = signal.value
        source = signal.source_ts_ms
        values[metric] = value
        sources[metric] = source
        missing[metric] = value is None
        if source is None:
            # 未観測の cell は missing だけを立てる（`ObservedWindowFrame` の不変条件）
            values[metric] = None
            missing[metric], stale[metric], suspect[metric] = True, False, False
            continue
        # Dataset と同じ規則（`dataset._window_frame` / `thermal._observed_window`）
        stale[metric] = signal.quality == _STALE or ts_ms - source >= schema.stale_after_ms
        suspect[metric] = signal.quality == _SUSPECT and value is not None
    return ObservedWindowFrame(
        ts_ms=ts_ms,
        values=values,
        source_ts_ms=sources,
        missing_mask=missing,
        stale_mask=stale,
        suspect_mask=suspect,
    )


def _as_of_index(frames: Sequence[LearnedFrame], ts_ms: int) -> int | None:
    found: int | None = None
    for index, frame in enumerate(frames):
        if frame.snapshot.ts_ms <= ts_ms:
            found = index
        else:
            break
    return found


def _gap_after(frames: Sequence[LearnedFrame], index: int) -> bool:
    """as-of で当てる frame の後に、受け取っていない tick があるか（`tick_id` の飛び）。

    **anchor（最新の frame）には後続を求めない。** 後続がまだ届いていないのは正常である。
    """
    if index == len(frames) - 1:
        return False
    return frames[index + 1].snapshot.tick_id != frames[index].snapshot.tick_id + 1


def _missing_frame(ts_ms: int, schema: ThermalFeatureSchemaV2) -> ObservedWindowFrame:
    """欠けた tick の格子時刻。すべての cell を missing にする（補間しない。0077 §2.3）。"""
    metrics = schema.metrics
    return ObservedWindowFrame(
        ts_ms=ts_ms,
        values=dict.fromkeys(metrics),
        source_ts_ms=dict.fromkeys(metrics),
        missing_mask=dict.fromkeys(metrics, True),
        stale_mask=dict.fromkeys(metrics, False),
        suspect_mask=dict.fromkeys(metrics, False),
    )
