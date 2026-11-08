"""Thermal dataset の決定的な組み立てとartifact出力。#83

保存済みTelemetryと #82 のControlTickを同じ時刻軸で結合する。実機・Fan Hardwareへは
到達せず、ReplayをSQLiteへ投入したあとも同じ関数で再生成できる。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from pydantic import ValidationError

from coldaisle import logs
from coldaisle.calibration_log import (
    calibration_change_points,
    declared_calibration_change_points,
    reject_changes_overlapping,
    reject_declared_changes_overlapping,
    require_history_covers,
)
from coldaisle.clock import WallClock
from coldaisle.control.drift.model import ChangeKind, DeclaredChange
from coldaisle.control.model.dataset import (
    ActionContext,
    ActionExclusionCounts,
    ActionStepV2,
    ActionZone,
    DatasetExample,
    DatasetExampleV2,
    DatasetManifest,
    DatasetManifestV2,
    DatasetSourceKind,
    DatasetSpec,
    DatasetSpecV2,
    DatasetWorkloadRegime,
    PriorAction,
    ReplayBindingV2,
    ReplayExportV2,
    SourceRun,
    TargetFrame,
    ThermalDataset,
    ThermalDatasetV2,
    WindowFrame,
    examples_sha256,
    reject_calibration_changes,
)
from coldaisle.control.schema import ControlTick, PerZone, Zone
from coldaisle.csv_export_manifest import replay_binding_of_records
from coldaisle.declared_changes import DeclaredChangesError, read_declared_changes
from coldaisle.ingest.replay import (
    ReplayBindingError,
    ReplayExportInputs,
    read_replay_export_inputs,
    replay_sha256,
)
from coldaisle.store import Quality, QualityRules, SeriesPoint, SqliteStore
from coldaisle.store.calibration_history import (
    CalibrationHistory,
    CalibrationHistoryError,
)
from coldaisle.store.csv_export import TIMESTAMP_RESOLUTION_MS
from coldaisle.store.export_binding import (
    ExportBinding,
    ExportBindingError,
    TrainingProduction,
    read_training_production,
)
from coldaisle.store.models import ControlTraceRecord, SequencedControlTrace

MANIFEST_FILENAME = "manifest.json"
EXAMPLES_FILENAME = "examples.jsonl"
_ARTIFACT_ALIAS = re.compile(r"^dataset-[0-9a-f]{32}$")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_LOCK_FILENAME = ".coldaisle-dataset.lock"
LOGGER = logging.getLogger("coldaisle.dataset")
SeriesIndex = tuple[tuple[int, ...], tuple[SeriesPoint, ...]]


def replay_fingerprint(path: Path) -> str:
    """Replayが読むCSV集合のSHA-256を返す。

    絶対パスはhashにもmanifestにも入れない。basenameはファイル境界を区別するhashの
    入力にだけ使い、artifactには出さない。公開用source aliasは利用者が別途指定する。
    """
    return replay_sha256(path)


class ThermalDatasetBuilder:
    """SQLiteのraw readingsとControlTickからschema v1の学習例を作る。"""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def build(
        self,
        *,
        source_run: SourceRun,
        spec: DatasetSpec,
        replay_binding: tuple[str | None, str | None] | None = None,
    ) -> ThermalDataset:
        """1 source runを決定的に変換する。

        action候補はrun内のControlTickである。完全なhistoryとtarget探索範囲をrun内に
        持てない端のtickは採用しない。target観測そのものが無い場合は、行を捨てずに
        ``missing_mask`` を立てる。

        ``replay_binding`` は ``--replay-path`` の manifest から計算した
        ``(timezone, export_binding_sha256)``（manifest が無ければ ``(None, None)``）。渡せば
        専用 DB の ``dataset_source_run`` の値との一致を求める（決定記録 0100 §2.8）。
        v1 は照合していない run（``NULL``）も受け入れる。CLI は必ず渡す。
        """
        with self._store.read_snapshot():
            _validate_dedicated_source_db(self._store, source_run)
            if replay_binding is not None:
                _require_db_binding(self._store, replay_binding)
            earliest_action_ms = source_run.start_ms + spec.window_ms
            latest_label_margin_ms = spec.horizons_ms[-1] + spec.target_tolerance_ms
            traces = self._store.control_traces(earliest_action_ms, source_run.end_ms)
            eligible = tuple(
                trace
                for trace in traces
                if trace.ts_ms + latest_label_margin_ms < source_run.end_ms
            )
            if not eligible:
                raise ValueError("source run内に完全なwindow/targetを持つControlTickが無い")

            metrics = tuple(dict.fromkeys((*spec.feature_metrics, *spec.target_metrics)))
            points: dict[str, SeriesIndex] = {}
            for metric in metrics:
                metric_points = self._store.series(metric, source_run.start_ms, source_run.end_ms)
                points[metric] = (tuple(point.ts_ms for point in metric_points), metric_points)
            examples = tuple(
                self._example(trace=trace, source_run=source_run, spec=spec, points=points)
                for trace in eligible
            )
        return ThermalDataset(
            manifest=DatasetManifest(
                spec=spec,
                source_runs=(source_run,),
                telemetry_sha256=_telemetry_digest(metrics, points),
                control_trace_sha256=_trace_digest(eligible),
                examples_sha256=examples_sha256(examples),
                example_count=len(examples),
            ),
            examples=examples,
        )

    def _example(
        self,
        *,
        trace: ControlTraceRecord,
        source_run: SourceRun,
        spec: DatasetSpec,
        points: dict[str, SeriesIndex],
    ) -> DatasetExample:
        tick, raw = _parse_tick(trace)
        history_start_ms = tick.ts_ms - spec.window_ms
        frame_times = range(history_start_ms, tick.ts_ms + 1, spec.sample_period_ms)
        window = tuple(_window_frame(ts_ms, spec=spec, points=points) for ts_ms in frame_times)
        targets = tuple(
            _target_frame(tick.ts_ms, horizon, spec=spec, points=points)
            for horizon in spec.horizons_ms
        )
        action = _per_zone_action(tick)
        context = _action_context(tick, raw)
        label_end_ms = tick.ts_ms + spec.horizons_ms[-1] + spec.target_tolerance_ms
        return DatasetExample(
            example_id=f"{source_run.run_id}:{tick.ts_ms}:{tick.tick_id}",
            source_run_id=source_run.run_id,
            history_start_ms=history_start_ms,
            action_ts_ms=tick.ts_ms,
            label_end_ms=label_end_ms,
            control_tick_id=tick.tick_id,
            control_schema_version=tick.schema_version,
            window=window,
            action=action,
            context=context,
            targets=targets,
        )


class ActionExclusionReason(StrEnum):
    """action の規則で example を作らなかった理由（0087 §2.2）。定義の順が数える優先順である。"""

    STALE = "stale"
    DISCONTINUITY = "discontinuity"
    IN_STEP_CHANGE = "in_step_change"
    RESTART = "restart"
    TICK_ID_GAP = "tick_id_gap"


_ZoneDemands = tuple[float, float, float]


class _RunTicks:
    """1 source run の ControlTick を記録した順（``seq``）に並べたもの。

    構築時に ``ts_ms`` が ``seq`` の順に狭義単調増加であることを確かめてあるので、
    ``ts_ms`` の二分探索で「直近」を引ける（0087 §2.1）。
    """

    def __init__(
        self,
        store: SqliteStore,
        traces: Iterable[SequencedControlTrace],
        *,
        legacy_through_seq: int,
    ) -> None:
        self._store = store
        # **全 tick を検証するが、解析したものも trace の本文も持たない**（#416）。1 tick の
        # ControlTick と生の dict は合わせて約 60 KB、本文の文字列は数 KB〜10 KB で、1 s の tick の
        # 数日分では数 GB〜数十 GB になる。1 回の走査で検証・digest・判定に使う欄の抽出を済ませ、
        # example を作る anchor の tick は `parsed()` で同じ snapshot から ``seq`` で引き直す
        digest = hashlib.sha256()
        effective: list[_ZoneDemands] = []
        seqs: list[int] = []
        ts: list[int] = []
        tick_ids: list[int] = []
        legacy = False
        parse_error: ValueError | None = None
        for trace in traces:
            seqs.append(trace.seq)
            ts.append(trace.ts_ms)
            tick_ids.append(trace.tick_id)
            if trace.seq <= legacy_through_seq:
                legacy = True
            if legacy or parse_error is not None:
                # 拒否は決まっている。理由の優先（移行前 → 検証 → 単調）を変えないため、
                # 走査は最後まで回すが解析はしない
                continue
            try:
                tick, raw = _parse_tick(trace)
            except ValueError as error:
                parse_error = error
                continue
            effective.append(
                (
                    tick.zones.front.demand.effective,
                    tick.zones.rear.demand.effective,
                    tick.zones.top.demand.effective,
                )
            )
            _update_digest(
                digest, [trace.seq, trace.ts_ms, trace.tick_id, trace.schema_version, raw]
            )
        if legacy:
            # 移行前の行の seq は記録した順を表さない（0007 が (ts_ms, tick_id) の順に振った）
            raise ValueError("移行前の ControlTick を含む run から Dataset v2 は作らない")
        if parse_error is not None:
            raise parse_error
        self.effective: tuple[_ZoneDemands, ...] = tuple(effective)
        self.seqs = tuple(seqs)
        self.ts = tuple(ts)
        self.tick_ids = tuple(tick_ids)
        self.trace_sha256 = digest.hexdigest()
        """run の全 tick の SHA-256（manifest の ``control_trace_sha256``）。

        ``seq``・時刻・``tick_id``・版・本文を記録した順に hash する。
        """
        if any(later <= earlier for earlier, later in zip(self.ts, self.ts[1:], strict=False)):
            # 黙って並べ替えない。壁時計が戻った run では「直近」が実行の順と一致しない
            raise ValueError(
                "seq の順に並べた ControlTick の ts_ms が狭義単調増加でない run から"
                " Dataset v2 は作らない"
            )

    def parsed(self, index: int) -> tuple[ControlTick, dict[str, object]]:
        """``index`` の tick を同じ snapshot から引き直して解析する（:func:`_parse_tick`）。

        走査の時と同じ行であることを ``seq``・``ts_ms``・``tick_id`` で確かめる（snapshot の外で
        呼ばれた・行が変わった場合に、別の tick から example を作らない）。
        """
        trace = self._store.control_trace_by_seq(self.seqs[index])
        if trace is None or (trace.ts_ms, trace.tick_id) != (self.ts[index], self.tick_ids[index]):
            raise ValueError("走査の後に ControlTick の trace が変わった")
        return _parse_tick(trace)

    def as_of(self, ts_ms: int) -> int:
        """``ts_ms`` 以前で直近の tick の位置。無ければ -1。"""
        return bisect_right(self.ts, ts_ms) - 1


class _StepAction(NamedTuple):
    """1つの step の action（0087 §2.1 の as-of と、0182 §2.1 / §2.2 の時間平均と幅）。"""

    source: int
    """区間の開始時刻の as-of の tick の位置。"""
    mean: _ZoneDemands
    """区間の中の ``effective`` の時間平均（zone ごと）。"""
    spread: _ZoneDemands
    """区間の中の ``effective`` の幅（最大 − 最小。開始の as-of の値を含む。zone ごと）。"""


def _step_action(ticks: _RunTicks, source: int, start_ms: int, step_ms: int) -> _StepAction:
    """区間 ``[start_ms, start_ms + step_ms)`` の時間平均と幅（決定記録 0182 §2.1 / §2.2）。

    tick と tick の間は直前の tick の値が掛かっていたとみなす（区分的に一定）。区間の開始では
    as-of の tick ``source`` の値が掛かっている。終端（次の step の開始時刻）の tick は区間に
    入れない。
    平均は ``math.fsum`` で足し、丸めで区間の値の範囲を出ないように最小と最大の間に収める。
    """
    end_ms = start_ms + step_ms
    inside = range(bisect_left(ticks.ts, start_ms), bisect_left(ticks.ts, end_ms))
    values = [ticks.effective[source]]
    durations: list[int] = []
    current_ms = start_ms
    for position in inside:
        durations.append(ticks.ts[position] - current_ms)
        values.append(ticks.effective[position])
        current_ms = ticks.ts[position]
    durations.append(end_ms - current_ms)
    means: list[float] = []
    spreads: list[float] = []
    for zone in range(3):
        zone_values = [value[zone] for value in values]
        low, high = min(zone_values), max(zone_values)
        total = math.fsum(
            value * duration for value, duration in zip(zone_values, durations, strict=True)
        )
        means.append(min(high, max(low, total / step_ms)))
        spreads.append(high - low)
    return _StepAction(
        source=source,
        mean=(means[0], means[1], means[2]),
        spread=(spreads[0], spreads[1], spreads[2]),
    )


def _assess_anchor(
    ticks: _RunTicks, index: int, spec: DatasetSpecV2
) -> tuple[ActionExclusionReason | None, tuple[_StepAction, ...]]:
    """anchor ``index`` の example を作れるかを 0087 §2.2 / §2.7 と 0182 §2.2 の順で判定する。

    作れるなら ``(None, step ごとの action)`` を返す。作れなければ、最初に当たった
    理由を返す（理由の順は :class:`ActionExclusionReason` の定義の順）。
    """
    stale_after = spec.action_stale_after_ms
    step_ms = spec.action_step_ms
    anchor_ms = ticks.ts[index]
    horizon_end_ms = anchor_ms + spec.action_steps * step_ms
    prior = index - 1

    # 1. 鮮度: prior_action（厳密に前で直近の tick）と、格子の時刻ごとの as-of の tick
    if prior < 0 or anchor_ms - ticks.ts[prior] >= stale_after:
        return ActionExclusionReason.STALE, ()
    as_of: list[int] = []
    for step in range(spec.action_steps):
        grid_ms = anchor_ms + step * step_ms
        source = ticks.as_of(grid_ms)
        if grid_ms - ticks.ts[source] >= stale_after:
            return ActionExclusionReason.STALE, ()
        as_of.append(source)

    # 2. 連続: prior_action の元の tick から最大の horizon まで、隣り合う tick の差と、
    #    最後の tick から最大の horizon までの差。最大の horizon より後の tick が無い
    #    run の末尾もここに数える
    last = ticks.as_of(horizon_end_ms)
    if any(
        ticks.ts[position + 1] - ticks.ts[position] >= stale_after
        for position in range(prior, last)
    ):
        return ActionExclusionReason.DISCONTINUITY, ()
    if horizon_end_ms - ticks.ts[last] >= stale_after or last + 1 >= len(ticks.ts):
        return ActionExclusionReason.DISCONTINUITY, ()

    # 3. 区間内の変化: 区間 [開始, 終端) の中の effective の幅が、どれか1つの zone で上限を超える
    #    （0182 §2.2）。終端（次の step の開始時刻）の tick は数えない
    actions = tuple(
        _step_action(ticks, source, anchor_ms + step * step_ms, step_ms)
        for step, source in enumerate(as_of)
    )
    limit = spec.action_max_in_step_range
    if any(spread > limit for action in actions for spread in action.spread):
        return ActionExclusionReason.IN_STEP_CHANGE, ()

    # 4 / 5. tick_id は最大の horizon より後の最初の tick まで、ちょうど 1 ずつ増える
    differences = tuple(
        ticks.tick_ids[position + 1] - ticks.tick_ids[position]
        for position in range(prior, last + 1)
    )
    if any(difference <= 0 for difference in differences):
        return ActionExclusionReason.RESTART, ()
    if any(difference >= 2 for difference in differences):
        return ActionExclusionReason.TICK_ID_GAP, ()
    return None, actions


def _anchor_candidates(ts: tuple[int, ...], start_ms: int, stride_ms: int) -> tuple[int, ...]:
    """格子 ``start_ms + k × stride_ms`` の各時刻 g 以上で最初の tick の位置（決定記録 0181 §2.1）。

    同じ tick が複数の g に当たれば 1 回だけ返す。選んだ tick の時刻より後の最初の格子へ跳ぶ
    ので、tick の無い長い区間があっても格子を 1 つずつ歩かない（O(min(格子数, tick 数) × log n)）。
    ``ts`` は狭義単調増加（:class:`_RunTicks` が確かめてある）。
    """
    candidates: list[int] = []
    grid_ms = start_ms
    while True:
        index = bisect_left(ts, grid_ms)
        if index >= len(ts):
            return tuple(candidates)
        candidates.append(index)
        # この tick に当たる格子をすべて飛ばし、tick の時刻より後の最初の格子へ
        grid_ms = start_ms + ((ts[index] - start_ms) // stride_ms + 1) * stride_ms


class ThermalDatasetV2Builder:
    """SQLite の raw readings と ControlTick から Thermal Dataset v2 を作る（0079 段 1 / 0087）。"""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def build(
        self,
        *,
        source_run: SourceRun,
        spec: DatasetSpecV2,
        declared_changes: tuple[DeclaredChange, ...],
        calibration_history: CalibrationHistory,
        export_binding: ExportBinding,
    ) -> ThermalDatasetV2:
        """1 source run を決定的に変換する。

        ``declared_changes`` は 0056 §2.5 の宣言された変更で、**既定値を持たない**。宣言が無い場合も
        空の tuple を明示する（0087 §2.6）。そのうち ``calibration_changed`` は記録の行と同じく
        区間 ``[floor(ts), ts]`` とし、全 example の期間と交われば生成を拒否する（0109 §2.4）。

        ``calibration_history`` は本番の DB から読み取り専用で読んだ較正の変更の記録で、**既定値を
        持たない**（決定記録 0099 §2.6）。専用 DB（再生）には記録が無いので別に渡す。example が
        1件以上あれば、

        - 全 example の期間の先頭以前に記録の行が無ければ拒否する（被覆）
        - 各行を CSV の秒の切り捨ての区間 ``[floor(ts), ts]`` とし、両端を変更の時刻として
          宣言との**和集合**で 0087 §2.6 の検査へ渡す

        example が0件の dataset では被覆と変更の検査を行わない（記録の読み込みの検証は
        :func:`~coldaisle.store.calibration_history.read_calibration_history` が済ませている）。

        ``export_binding`` は ``--replay-path`` の manifest を本番の DB の ``csv_exports`` と全欄で
        照合した結果（決定記録 0100 §2.6）。専用 DB の ``dataset_source_run`` の timezone と
        ``export_binding_sha256`` が一致しなければ拒否し、照合していない run（``NULL``）も拒否する
        （0100 §2.8）。通れば source run ごとの ``ReplayBindingV2`` を manifest に書く。

        次の run からは生成しない（example の除外ではなく、生成全体の拒否。0087 §2.1）。

        - ``seq`` の順に並べた ControlTick の ``ts_ms`` が狭義単調増加でない
        - 移行前の行（``seq ≤ legacy_through_seq``）を含む

        anchor の候補は ``spec.anchor_stride_ms`` の格子の直後の tick だけで、候補にならなかった
        tick は除外の件数に数えない（決定記録 0181 §2.1）。
        完全な window と target をrun内に持てない端の tick は v1 と同じく anchor にしない。
        anchor にできる tick のうち、action の規則（0087 §2.2 / §2.3 / §2.7）に外れたものは
        作らず、理由ごとの件数を manifest に記録する。
        """
        if not isinstance(declared_changes, tuple) or not all(
            isinstance(change, DeclaredChange) for change in declared_changes
        ):
            raise TypeError("declared_changes は DeclaredChange の tuple を明示して渡す")
        if not isinstance(calibration_history, CalibrationHistory):
            raise TypeError(
                "calibration_history は read_calibration_history() の結果を明示して渡す"
            )
        if not isinstance(export_binding, ExportBinding):
            raise TypeError("export_binding は read_production_records() の結果を明示して渡す")
        declared_calibration = tuple(
            sorted(
                {
                    change.ts_ms
                    for change in declared_changes
                    if change.kind is ChangeKind.CALIBRATION_CHANGED
                }
            )
        )
        calibration_changes: tuple[int, ...] = ()
        with self._store.read_snapshot():
            _validate_dedicated_source_db(self._store, source_run)
            # csv_exports との照合（export_binding）と専用 DB の束縛が合わなければ、被覆と
            # 変更の検査（0099 §2.6）へ進まない（0100 §2.6 の 3）
            db_binding = self._store.dataset_source_run_export_binding()
            if db_binding is None or db_binding == (None, None):
                raise ValueError(
                    "export の manifest と照合していない run から Dataset v2 は作らない"
                    "（決定記録 0100 §2.3 / §2.8）"
                )
            _require_db_binding(
                self._store, (export_binding.timezone, export_binding.export_binding_sha256)
            )
            # trace は tuple にせず流す（#416）。移行前の行の拒否も _RunTicks の走査の中で行う
            ticks = _RunTicks(
                self._store,
                self._store.iter_control_traces_in_seq_order(
                    source_run.start_ms, source_run.end_ms
                ),
                legacy_through_seq=self._store.control_trace_legacy_through_seq(),
            )
            earliest_action_ms = source_run.start_ms + spec.window_ms
            # 間引くのは anchor の候補だけ。prior_action・action の格子・as-of は全 tick から引く
            # （決定記録 0181 §2.1）
            eligible = tuple(
                index
                for index in _anchor_candidates(
                    ticks.ts, source_run.start_ms, spec.anchor_stride_ms
                )
                if ticks.ts[index] >= earliest_action_ms
                and ticks.ts[index] + spec.horizons_ms[-1] < source_run.end_ms
            )
            if not eligible:
                raise ValueError("source run内に完全なwindow/targetを持つControlTickが無い")

            metrics = tuple(dict.fromkeys((*spec.feature_metrics, *spec.target_metrics)))
            points: dict[str, SeriesIndex] = {}
            for metric in metrics:
                metric_points = self._store.series(metric, source_run.start_ms, source_run.end_ms)
                points[metric] = (tuple(point.ts_ms for point in metric_points), metric_points)

            excluded = dict.fromkeys(ActionExclusionReason, 0)
            examples: list[DatasetExampleV2] = []
            for index in eligible:
                reason, actions = _assess_anchor(ticks, index, spec)
                if reason is not None:
                    excluded[reason] += 1
                    continue
                examples.append(
                    _example_v2(
                        ticks=ticks,
                        index=index,
                        actions=actions,
                        source_run=source_run,
                        spec=spec,
                        points=points,
                    )
                )
        built = tuple(examples)
        if built:
            start_ms = min(example.history_start_ms for example in built)
            require_history_covers(calibration_history, start_ms)
            end_ms = max(example.label_end_ms for example in built)
            reject_changes_overlapping(calibration_history, start_ms=start_ms, end_ms=end_ms)
            # 宣言も記録の行と同じ区間で見る（0109 §2.4 / §5 #1。宣言は足せるが記録を消せない）
            reject_declared_changes_overlapping(
                declared_calibration, start_ms=start_ms, end_ms=end_ms
            )
            calibration_changes = calibration_change_points(
                calibration_history
            ) + declared_calibration_change_points(declared_calibration)
        reject_calibration_changes(built, calibration_changes)
        return ThermalDatasetV2(
            manifest=DatasetManifestV2(
                spec=spec,
                source_runs=(source_run,),
                telemetry_sha256=_telemetry_digest(metrics, points),
                control_trace_sha256=ticks.trace_sha256,
                examples_sha256=examples_sha256(built),
                example_count=len(built),
                excluded=ActionExclusionCounts(
                    stale=excluded[ActionExclusionReason.STALE],
                    discontinuity=excluded[ActionExclusionReason.DISCONTINUITY],
                    in_step_change=excluded[ActionExclusionReason.IN_STEP_CHANGE],
                    restart=excluded[ActionExclusionReason.RESTART],
                    tick_id_gap=excluded[ActionExclusionReason.TICK_ID_GAP],
                ),
                replay_bindings=(_replay_binding_v2(source_run, export_binding),),
            ),
            examples=built,
        )


def _zone_demands(values: _ZoneDemands) -> PerZone[float]:
    front, rear, top = values
    return PerZone(front=front, rear=rear, top=top)


def _example_v2(
    *,
    ticks: _RunTicks,
    index: int,
    actions: tuple[_StepAction, ...],
    source_run: SourceRun,
    spec: DatasetSpecV2,
    points: dict[str, SeriesIndex],
) -> DatasetExampleV2:
    tick, raw = ticks.parsed(index)
    history_start_ms = tick.ts_ms - spec.window_ms
    frame_times = range(history_start_ms, tick.ts_ms + 1, spec.sample_period_ms)
    window = tuple(_window_frame(ts_ms, spec=spec, points=points) for ts_ms in frame_times)
    targets = tuple(
        _target_frame_at_or_before(tick.ts_ms, horizon, spec=spec, points=points)
        for horizon in spec.horizons_ms
    )
    prior = index - 1
    return DatasetExampleV2(
        example_id=f"{source_run.run_id}:{tick.ts_ms}:{tick.tick_id}",
        source_run_id=source_run.run_id,
        history_start_ms=history_start_ms,
        action_ts_ms=tick.ts_ms,
        label_end_ms=tick.ts_ms + spec.horizons_ms[-1],
        control_tick_id=tick.tick_id,
        control_schema_version=tick.schema_version,
        window=window,
        action=_per_zone_action(tick),
        context=_action_context(tick, raw),
        prior_action=PriorAction(
            source_ts_ms=ticks.ts[prior],
            source_tick_id=ticks.tick_ids[prior],
            effective_demand=_zone_demands(ticks.effective[prior]),
        ),
        action_steps=tuple(
            ActionStepV2(
                step=step,
                ts_ms=tick.ts_ms + step * spec.action_step_ms,
                source_ts_ms=ticks.ts[action.source],
                source_tick_id=ticks.tick_ids[action.source],
                effective_demand=_zone_demands(ticks.effective[action.source]),
                step_demand=_zone_demands(action.mean),
            )
            for step, action in enumerate(actions)
        ),
        in_step_range=_zone_demands(
            (
                max(action.spread[0] for action in actions),
                max(action.spread[1] for action in actions),
                max(action.spread[2] for action in actions),
            )
        ),
        targets=targets,
    )


def _per_zone_action(tick: ControlTick) -> PerZone[ActionZone]:
    return PerZone(
        front=_action_zone(tick, Zone.FRONT),
        rear=_action_zone(tick, Zone.REAR),
        top=_action_zone(tick, Zone.TOP),
    )


def _action_context(tick: ControlTick, raw: dict[str, object]) -> ActionContext:
    state_json = raw.get("state")
    workload_regime: DatasetWorkloadRegime | None = None
    regime_confidence: float | None = None
    if isinstance(state_json, dict):
        raw_regime = state_json.get("workload_regime")
        raw_confidence = state_json.get("regime_confidence")
        if raw_regime is not None or raw_confidence is not None:
            if not isinstance(raw_regime, str) or not isinstance(raw_confidence, (int, float)):
                raise ValueError("workload_regime と regime_confidence のtrace表現が不正")
            workload_regime = DatasetWorkloadRegime(raw_regime)
            regime_confidence = float(raw_confidence)
    return ActionContext(
        operating_mode=tick.state.operating_mode,
        authority_stage=tick.state.authority_stage,
        active_controller=tick.state.active_controller,
        safety_state=tick.state.safety_state,
        fallback_active=tick.state.fallback_active,
        supervisor_policy=tick.state.supervisor_policy,
        workload_regime=workload_regime,
        regime_confidence=regime_confidence,
        fault_codes=tuple(fault.code.value for fault in tick.faults),
    )


def _replay_binding_v2(source_run: SourceRun, binding: ExportBinding) -> ReplayBindingV2:
    """照合を通った export から ``ReplayBindingV2`` を作る。

    CSV の basename は書かない（0100 §2.8）。
    """
    return ReplayBindingV2(
        run_id=source_run.run_id,
        local_timezone=binding.timezone,
        export_binding_sha256=binding.export_binding_sha256,
        exports=tuple(
            ReplayExportV2(
                export_id=record.export_id,
                export_record_sha256=record.record_sha256(),
                day_start_ms=record.day_start_ms,
                day_end_ms=record.day_end_ms,
                csv_sha256=record.csv_sha256,
                row_seconds_sha256=record.row_seconds_sha256,
            )
            for record in binding.records
        ),
    )


def _require_db_binding(store: SqliteStore, expected: tuple[str | None, str | None]) -> None:
    """専用 DB の ``dataset_source_run`` の束縛が、入力の manifest から計算した値と一致する。

    決定記録 0100 §2.8。
    """
    if store.dataset_source_run_export_binding() != expected:
        raise ValueError(
            "専用 DB の再生の timezone / export_binding_sha256 が --replay-path の manifest と"
            "一致しない（決定記録 0100 §2.8）"
        )


def replay_binding_of(inputs: ReplayExportInputs) -> tuple[str | None, str | None]:
    """``--replay-path`` の manifest から ``(timezone, export_binding_sha256)`` を計算する。

    :func:`~coldaisle.csv_export_manifest.replay_binding_of_records` に委ねる（学習の入口と
    同じ関数）。
    """
    return replay_binding_of_records(inputs.records)


_ALL_TRACES_END_MS = 2**62
"""専用 DB の全 trace を読むときの期間の上端（``ts_ms`` の上限ではなく、全体を指すための値）。"""


def check_production_traces(production: TrainingProduction, *, start_ms: int) -> None:
    """本番の DB の trace から dataset を作れる期間か（決定記録 0112 §2.3 の 4 / 0116 §2.1）。

    trace が保持期間で消えた期間（削除の境界が run の開始より後）と、移行前の行を含む期間は
    ``ValueError``。``coldaisle-dataset`` の v2 と学習の入口が同じ関数を使う（0116 §2.2 の 3）。
    """
    if production.pruned_before_ms is not None and production.pruned_before_ms > start_ms:
        raise ValueError(
            "本番の DB の trace が run の期間で保持期間により消えている（決定記録 0112 §2.3 の 4）"
        )
    if any(trace.seq <= production.legacy_through_seq for trace in production.traces):
        raise ValueError("移行前の ControlTick を含む期間からは作らない（0087 §2.1）")


def copy_or_match_production_traces(
    store: SqliteStore, traces: tuple[SequencedControlTrace, ...]
) -> None:
    """専用 DB の ControlTick を本番の DB の trace にそろえる（決定記録 0116 §2.1 / §2.2 の 1）。

    trace の無い専用 DB へは ``seq`` を保って写す。既に trace があれば、全 trace が本番の trace と
    ``seq``・時刻・``tick_id``・版・``trace_json`` と順序まで完全に一致するときだけそのまま使う。
    1件でも違えば ``ValueError``（上書きも混ぜることもしない）。
    """
    existing = store.control_traces_in_seq_order(0, _ALL_TRACES_END_MS)
    if not existing:
        store.copy_control_traces(traces)
        return
    if existing != traces:
        raise ValueError(
            "専用 DB の ControlTick が本番の DB の trace と一致しない（決定記録 0116 §2.2 の 1）。"
            "新しい専用 DB で再生し直す"
        )


def _validate_dedicated_source_db(store: SqliteStore, source_run: SourceRun) -> None:
    """1 run専用DBであることを、snapshot内の保存行とingest sourceから検証する。"""
    connection = store.connection
    outside_readings = int(
        connection.execute(
            "SELECT COUNT(*) FROM readings WHERE ts_ms < ? OR ts_ms >= ?",
            (source_run.start_ms, source_run.end_ms),
        ).fetchone()[0]
    )
    outside_traces = int(
        connection.execute(
            "SELECT COUNT(*) FROM control_traces WHERE ts_ms < ? OR ts_ms >= ?",
            (source_run.start_ms, source_run.end_ms),
        ).fetchone()[0]
    )
    if outside_readings or outside_traces:
        raise ValueError("dataset生成DBはsource run期間だけを持つ専用DBでなければならない")
    ingest_sources = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT value FROM system_state WHERE key = 'sys.ingest_source' ORDER BY value"
        ).fetchall()
    )
    if ingest_sources != (source_run.kind.value,):
        raise ValueError("dataset生成DBのingest sourceがSourceRun.kindと一意に一致しない")
    provenance = store.dataset_source_run()
    expected = (source_run.run_id, source_run.kind.value, source_run.source_sha256)
    if provenance != expected:
        raise ValueError("SourceRunがDBのimmutable provenanceと一致しない")
    if not store.dataset_source_run_completed():
        # 途中停止・sample破棄のあったrunは入力の一部だけを持つ。全体hashの下で公開しない
        raise ValueError(
            "dataset source runが欠けなく最後まで取り込まれていない（途中停止または取りこぼし）"
        )
    # 印の存在だけを信じない。完了後に別のwriterがreadingsを追記・削除していれば
    # 封印したdigestと一致しない（triggerを外したDB等も含めて検出する）
    if store.readings_digest() != store.dataset_readings_seal():
        raise ValueError("dataset source runの完了後にreadingsが変更されている")


def _parse_tick(
    trace: ControlTraceRecord | SequencedControlTrace,
) -> tuple[ControlTick, dict[str, object]]:
    try:
        loaded = json.loads(trace.trace_json)
        # 対応versionの判断はControlTickに委ねる。最新versionとの単純比較にすると、
        # schema v2追加後に互換な保存済みv1 traceまで拒否してしまう。
        tick = ControlTick.model_validate_json(trace.trace_json)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("ControlTick decision traceを検証できない") from exc
    if not isinstance(loaded, dict):
        raise ValueError("ControlTick decision traceはJSON objectでなければならない")
    if (tick.ts_ms, tick.tick_id, tick.schema_version) != (
        trace.ts_ms,
        trace.tick_id,
        trace.schema_version,
    ):
        raise ValueError("ControlTickの外側indexとJSON本文が一致しない")
    return tick, loaded


def _update_digest(digest: hashlib._Hash, value: object) -> None:
    """型と境界を保ったcanonical JSONを1要素としてhashへ加える。"""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _telemetry_digest(metrics: tuple[str, ...], points: dict[str, SeriesIndex]) -> str:
    """dataset生成に読んだ正規化済みTelemetryのSHA-256。"""
    digest = hashlib.sha256()
    for metric in metrics:
        _stamps, metric_points = points[metric]
        _update_digest(
            digest,
            [
                metric,
                [[point.ts_ms, point.value, point.quality.value] for point in metric_points],
            ],
        )
    return digest.hexdigest()


def _trace_digest(traces: tuple[ControlTraceRecord, ...]) -> str:
    """dataset生成に使うControlTick trace集合のSHA-256。"""
    digest = hashlib.sha256()
    for trace in traces:
        _update_digest(
            digest,
            [
                trace.ts_ms,
                trace.tick_id,
                trace.schema_version,
                json.loads(trace.trace_json),
            ],
        )
    return digest.hexdigest()


def _window_frame(
    ts_ms: int,
    *,
    spec: DatasetSpec | DatasetSpecV2,
    points: dict[str, SeriesIndex],
) -> WindowFrame:
    values: dict[str, float | None] = {}
    source_times: dict[str, int | None] = {}
    qualities: dict[str, Quality | None] = {}
    missing: dict[str, bool] = {}
    stale: dict[str, bool] = {}
    for metric in spec.feature_metrics:
        stamps, metric_points = points[metric]
        point = _latest_at(stamps, metric_points, ts_ms)
        if point is None:
            values[metric] = None
            source_times[metric] = None
            qualities[metric] = None
            missing[metric] = True
            stale[metric] = False
            continue
        values[metric] = point.value
        source_times[metric] = point.ts_ms
        qualities[metric] = point.quality
        # 値の無いsuspect（非有限値）もmissingと同じく使えない観測としてmaskする
        missing[metric] = point.value is None
        stale[metric] = point.quality is Quality.STALE or ts_ms - point.ts_ms >= spec.stale_after_ms
    return WindowFrame(
        ts_ms=ts_ms,
        values=values,
        source_ts_ms=source_times,
        quality=qualities,
        missing_mask=missing,
        stale_mask=stale,
    )


def _target_frame(
    action_ts_ms: int,
    horizon_ms: int,
    *,
    spec: DatasetSpec,
    points: dict[str, SeriesIndex],
) -> TargetFrame:
    expected_ts_ms = action_ts_ms + horizon_ms
    values: dict[str, float | None] = {}
    source_times: dict[str, int | None] = {}
    qualities: dict[str, Quality | None] = {}
    missing: dict[str, bool] = {}
    for metric in spec.target_metrics:
        stamps, metric_points = points[metric]
        point = _nearest(
            stamps,
            metric_points,
            expected_ts_ms=expected_ts_ms,
            tolerance_ms=spec.target_tolerance_ms,
            after_ms=action_ts_ms,
        )
        if point is None:
            values[metric] = None
            source_times[metric] = None
            qualities[metric] = None
            missing[metric] = True
            continue
        values[metric] = point.value
        source_times[metric] = point.ts_ms
        qualities[metric] = point.quality
        # 値の無いsuspect（非有限値）もmissingと同じく使えない観測としてmaskする
        missing[metric] = point.value is None
    return TargetFrame(
        horizon_ms=horizon_ms,
        expected_ts_ms=expected_ts_ms,
        values=values,
        source_ts_ms=source_times,
        quality=qualities,
        missing_mask=missing,
    )


def _target_frame_at_or_before(
    action_ts_ms: int,
    horizon_ms: int,
    *,
    spec: DatasetSpecV2,
    points: dict[str, SeriesIndex],
) -> TargetFrame:
    """v2 の target: ``[期待時刻 − 許容誤差, 期待時刻]`` の観測のうち最も遅いもの（0087 §2.4）。

    期待時刻より後ろの観測は、それがより近くても採らない。後ろの観測には入力に無い action が効く。
    """
    expected_ts_ms = action_ts_ms + horizon_ms
    values: dict[str, float | None] = {}
    source_times: dict[str, int | None] = {}
    qualities: dict[str, Quality | None] = {}
    missing: dict[str, bool] = {}
    for metric in spec.target_metrics:
        stamps, metric_points = points[metric]
        point = _latest_at(stamps, metric_points, expected_ts_ms)
        if (
            point is None
            or point.ts_ms < expected_ts_ms - spec.target_tolerance_ms
            or point.ts_ms <= action_ts_ms
        ):
            values[metric] = None
            source_times[metric] = None
            qualities[metric] = None
            missing[metric] = True
            continue
        values[metric] = point.value
        source_times[metric] = point.ts_ms
        qualities[metric] = point.quality
        # 値の無いsuspect（非有限値）もmissingと同じく使えない観測としてmaskする
        missing[metric] = point.value is None
    return TargetFrame(
        horizon_ms=horizon_ms,
        expected_ts_ms=expected_ts_ms,
        values=values,
        source_ts_ms=source_times,
        quality=qualities,
        missing_mask=missing,
    )


def _latest_at(
    stamps: tuple[int, ...], points: tuple[SeriesPoint, ...], ts_ms: int
) -> SeriesPoint | None:
    index = bisect_right(stamps, ts_ms) - 1
    return None if index < 0 else points[index]


def _nearest(
    stamps: tuple[int, ...],
    points: tuple[SeriesPoint, ...],
    *,
    expected_ts_ms: int,
    tolerance_ms: int,
    after_ms: int,
) -> SeriesPoint | None:
    index = bisect_left(stamps, expected_ts_ms)
    candidates = [
        points[candidate]
        for candidate in (index - 1, index)
        if 0 <= candidate < len(points) and points[candidate].ts_ms > after_ms
    ]
    if not candidates:
        return None
    point = min(
        candidates, key=lambda candidate: (abs(candidate.ts_ms - expected_ts_ms), candidate.ts_ms)
    )
    if abs(point.ts_ms - expected_ts_ms) > tolerance_ms:
        return None
    return point


def _action_zone(tick: ControlTick, zone: Zone) -> ActionZone:
    record = tick.zones.get(zone)
    demand = record.demand
    return ActionZone(
        requested_demand=demand.requested,
        effective_demand=demand.effective,
        bound_by=demand.bound_by.value,
        controller_reason=record.controller_reason.code,
        override_reasons=tuple(reason.code for reason in demand.reasons),
    )


def open_output_root(output_root: Path, *, create: bool = True) -> int:
    """pathの各要素をsymlink非追従で開き、必要なdirectoryだけ作る。

    開いた最後の directory が実行 user の所有で group / world が書けないことを確かめ、その fd を
    返す（祖先は確かめない）。``create`` が偽なら無い要素を作らず ``FileNotFoundError``
    （``coldaisle-train`` の ``--out`` の親。決定記録 0150 §2.5 の手順 1）。Dataset の writer は
    既定の真のまま使う（0031 §3 の挙動を変えない）。
    """
    if ".." in output_root.parts:
        raise ValueError("output rootに '..' は使えない")
    absolute = output_root if output_root.is_absolute() else Path.cwd() / output_root
    directory_fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            if component in ("", "."):
                continue
            if create:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        root_stat = os.fstat(directory_fd)
        if root_stat.st_uid != os.geteuid():
            raise PermissionError("output rootは実行user所有でなければならない")
        if root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("output rootはgroup/world writableにできない")
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _lock_output_root(root_fd: int) -> int:
    """全writerが共有する固定inodeをlockし、既存artifact確認とrenameを直列化する。"""
    lock_fd = os.open(_LOCK_FILENAME, _LOCK_FLAGS, 0o600, dir_fd=root_fd)
    try:
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid():
            raise PermissionError("dataset writer lockは実行user所有のregular fileに限る")
        if lock_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or lock_stat.st_nlink != 1:
            raise PermissionError("dataset writer lockの権限またはlink数が安全でない")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        path_stat = os.stat(_LOCK_FILENAME, dir_fd=root_fd, follow_symlinks=False)
        if (path_stat.st_dev, path_stat.st_ino) != (lock_stat.st_dev, lock_stat.st_ino):
            raise RuntimeError("dataset writer lockが取得中に差し替えられた")
        return lock_fd
    except BaseException:
        os.close(lock_fd)
        raise


def _create_staging_directory(root_fd: int) -> tuple[str, int]:
    for _attempt in range(16):
        name = f".coldaisle-stage-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        try:
            return name, os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        except BaseException:
            with suppress(OSError):
                os.rmdir(name, dir_fd=root_fd)
            raise
    raise FileExistsError("一意なdataset staging directoryを作れない")


def _write_regular_file(directory_fd: int, name: str, payload: bytes) -> None:
    file_fd = os.open(name, _FILE_WRITE_FLAGS, 0o600, dir_fd=directory_fd)
    with os.fdopen(file_fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _cleanup_staging(root_fd: int, name: str, directory_fd: int) -> None:
    """自分で作ったstagingの既知2ファイルだけをunlinkする。"""
    for filename in (MANIFEST_FILENAME, EXAMPLES_FILENAME):
        with suppress(FileNotFoundError):
            os.unlink(filename, dir_fd=directory_fd)
    os.rmdir(name, dir_fd=root_fd)


def _revalidated_payloads(dataset: ThermalDataset | ThermalDatasetV2) -> tuple[bytes, bytes]:
    """書き出す bytes を作り、その bytes を検証し直す（v1 / v2 共通）。

    ``model_copy(update=...)`` などで検証を迂回した instance も、書き出しの境界で拒否する。
    **Dataset 全体の JSON を作って丸ごと検証し直さない**（#416）。全体の複製で example の分の
    メモリが2倍になり、1 s の tick の 1 日（約 86,000 example）で 20 GB を超えた。代わりに

    1. manifest と、書き出す ``examples.jsonl`` の行を1行ずつ、その bytes から検証し直す
       （検証した instance は持たない）。書く bytes そのものが field の検証を通る
    2. 検証し直した manifest と元の example で Dataset の検証（件数・ID・tick の照合・
       ``examples_sha256``）を走らせる。example は 1 の bytes と同じ instance から作ったもの
    """
    if isinstance(dataset, ThermalDatasetV2):
        manifest_v2 = DatasetManifestV2.model_validate_json(dataset.manifest.model_dump_json())
        lines_v2 = [example.model_dump_json() for example in dataset.examples]
        for line in lines_v2:
            DatasetExampleV2.model_validate_json(line)
        checked: ThermalDataset | ThermalDatasetV2 = ThermalDatasetV2.model_validate(
            {"manifest": manifest_v2, "examples": dataset.examples}
        )
        payload = b"".join((line + "\n").encode() for line in lines_v2)
        del lines_v2
    else:
        manifest_v1 = DatasetManifest.model_validate_json(dataset.manifest.model_dump_json())
        lines_v1 = [example.model_dump_json() for example in dataset.examples]
        for line in lines_v1:
            DatasetExample.model_validate_json(line)
        checked = ThermalDataset.model_validate(
            {"manifest": manifest_v1, "examples": dataset.examples}
        )
        payload = b"".join((line + "\n").encode() for line in lines_v1)
        del lines_v1
    if hashlib.sha256(payload).hexdigest() != checked.manifest.examples_sha256:
        raise ValueError("manifestのexamples_sha256が書き出すbytesと一致しない")
    manifest_payload = (checked.manifest.model_dump_json(indent=2) + "\n").encode()
    return payload, manifest_payload


def write_dataset(
    dataset: ThermalDataset | ThermalDatasetV2,
    output_root: Path,
    artifact_name: str,
) -> tuple[Path, Path]:
    """検証済み2ファイルをstagingから原子的に公開する（v1 / v2 共通）。

    既存artifactは常に拒否する。全writerが同じparent lockを保持して不存在を確認し、
    同じparent内のstaging directoryをmacOS / Ubuntu共通の``os.rename``で公開する。
    """
    if _ARTIFACT_ALIAS.fullmatch(artifact_name) is None:
        raise ValueError("artifact_nameは公開用の dataset-<32 hex> aliasにする")
    # model_copy(update=...)等でvalidationを迂回したinstanceも書き出し境界で拒否する。
    # 版は入力の型で決め、v1 を v2 として（またはその逆に）読み替えない（0087 §2.8）
    examples_payload, manifest_payload = _revalidated_payloads(dataset)

    root_fd = open_output_root(output_root)
    lock_fd: int | None = None
    staging_fd: int | None = None
    staging_name: str | None = None
    published = False
    try:
        lock_fd = _lock_output_root(root_fd)
        try:
            os.stat(artifact_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("artifactは既に存在するため上書きしない")

        staging_name, staging_fd = _create_staging_directory(root_fd)
        _write_regular_file(staging_fd, EXAMPLES_FILENAME, examples_payload)
        _write_regular_file(staging_fd, MANIFEST_FILENAME, manifest_payload)
        os.fsync(staging_fd)
        staging_path_stat = os.stat(staging_name, dir_fd=root_fd, follow_symlinks=False)
        staging_opened_stat = os.fstat(staging_fd)
        if (staging_path_stat.st_dev, staging_path_stat.st_ino) != (
            staging_opened_stat.st_dev,
            staging_opened_stat.st_ino,
        ):
            raise RuntimeError("書き出し後にstaging directoryが差し替えられたため中止した")

        try:
            os.stat(artifact_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("publish直前にartifactが作られたため上書きしない")
        os.rename(
            staging_name,
            artifact_name,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        published = True
        os.fsync(root_fd)
        artifact_dir = output_root / artifact_name
        return artifact_dir / MANIFEST_FILENAME, artifact_dir / EXAMPLES_FILENAME
    finally:
        if staging_name is not None and staging_fd is not None and not published:
            # 元の例外を優先する。0700かつ既知名のstaging以外は触らない。
            with suppress(OSError):
                _cleanup_staging(root_fd, staging_name, staging_fd)
        if staging_fd is not None:
            os.close(staging_fd)
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)


_V2_ONLY_OPTIONS = (
    "anchor_stride_ms",
    "action_max_in_step_range",
    "action_step_ms",
    "action_steps",
    "action_stale_after_ms",
    "declared_changes",
    "calibration_history_db",
)
"""v2 でだけ必須、v1 では渡すと拒否する引数（0109 §5 #6）。"""


def build_parser() -> argparse.ArgumentParser:
    """実測で選ぶ値をすべて必須にしたCLI parserを返す。版も既定値なしで選ばせる（0109 §5 #6）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-dataset", description="SQLiteからThermal Dataset v1 / v2を生成"
    )
    parser.add_argument(
        "--dataset-version",
        type=int,
        choices=(1, 2),
        required=True,
        help="作る dataset の版。既定値は無い（決定記録 0109 §5 #6）",
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--quality-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--run-alias", required=True)
    parser.add_argument("--source-kind", choices=[DatasetSourceKind.REPLAY.value], required=True)
    parser.add_argument(
        "--replay-path",
        type=Path,
        help="source-kind=replayで必須。Replay対象からSHA-256を計算する",
    )
    parser.add_argument("--source-alias", action="append", required=True)
    parser.add_argument("--start-ms", type=int, required=True)
    parser.add_argument("--end-ms", type=int, required=True)
    parser.add_argument("--window-ms", type=int, required=True)
    parser.add_argument("--sample-period-ms", type=int, required=True)
    parser.add_argument("--horizon-ms", type=int, action="append", required=True)
    parser.add_argument("--target-tolerance-ms", type=int, required=True)
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--feature-metric", action="append", required=True)
    parser.add_argument("--target-metric", action="append", required=True)
    v2 = parser.add_argument_group("v2（--dataset-version 2 で必須。v1 では渡さない）")
    v2.add_argument(
        "--anchor-stride-ms",
        type=int,
        help="anchor の候補を選ぶ格子の間隔（ms）。既定値は無い（決定記録 0181 §2.1）",
    )
    v2.add_argument(
        "--action-max-in-step-range",
        type=float,
        help=(
            "step の区間の中の effective の幅の上限（0〜1）。超えた example は作らない。"
            "0 で区間の中の変化を許さない。既定値は無い（決定記録 0182 §2.2）"
        ),
    )
    v2.add_argument("--action-step-ms", type=int)
    v2.add_argument("--action-steps", type=int)
    v2.add_argument("--action-stale-after-ms", type=int)
    v2.add_argument(
        "--declared-changes",
        type=Path,
        help="宣言された変更の YAML（schema_version: 1 と changes:。無ければ changes: []）",
    )
    v2.add_argument(
        "--calibration-history-db",
        type=Path,
        help="較正の変更の記録を持つ本番の DB（読み取り専用で開く。決定記録 0099 §2.6）",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def _option_name(dest: str) -> str:
    return "--" + dest.replace("_", "-")


def _same_file(left: Path, right: Path) -> bool:
    """同じ実体か（symlink・相対 path・hard link の別名を含む）。"""
    if left.resolve() == right.resolve():
        return True
    try:
        a, b = left.stat(), right.stat()
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _overlaps(ts_ms: int, start_ms: int, end_ms: int) -> bool:
    floor_ms = ts_ms // TIMESTAMP_RESOLUTION_MS * TIMESTAMP_RESOLUTION_MS
    return floor_ms <= end_ms and start_ms <= ts_ms


def _source_run(args: argparse.Namespace, source_sha256: str) -> SourceRun:
    return SourceRun(
        run_id=args.run_alias,
        kind=DatasetSourceKind(args.source_kind),
        start_ms=args.start_ms,
        end_ms=args.end_ms,
        source_refs=tuple(args.source_alias),
        source_sha256=source_sha256,
    )


def _common_spec(args: argparse.Namespace) -> dict[str, object]:
    return {
        "window_ms": args.window_ms,
        "sample_period_ms": args.sample_period_ms,
        "horizons_ms": tuple(args.horizon_ms),
        "target_tolerance_ms": args.target_tolerance_ms,
        "stale_after_ms": args.stale_after_ms,
        "feature_metrics": tuple(args.feature_metric),
        "target_metrics": tuple(args.target_metric),
    }


def _main_v1(args: argparse.Namespace) -> int:
    # fingerprint と manifest を同じ1回の読み出しから得る（決定記録 0100 §2.8）
    inputs = read_replay_export_inputs(args.replay_path)
    source_run = _source_run(args, inputs.source_sha256)
    spec = DatasetSpec.model_validate(_common_spec(args))
    rules = QualityRules.from_yaml(args.quality_config)
    with SqliteStore(args.db, rules=rules, clock=WallClock()) as store:
        dataset = ThermalDatasetBuilder(store).build(
            source_run=source_run, spec=spec, replay_binding=replay_binding_of(inputs)
        )
    write_dataset(dataset, args.output_root, args.artifact_name)
    return 0


def _main_v2(args: argparse.Namespace) -> int:
    # 読む順序: 宣言のファイル → fingerprint → 較正の記録 → 専用 DB（0109 §7）。
    # DB を開く前に引数とファイルの誤りで止める。
    try:
        declared = read_declared_changes(args.declared_changes)
    except DeclaredChangesError as error:
        LOGGER.error(
            "宣言された変更のファイルを拒否した（dataset を作らない）",
            extra={logs.FIELDS_KEY: {"reason": str(error)}},
        )
        return 1
    # 読む順序: fingerprint と manifest（同じ1回の読み出し）→ 本番の DB の較正の記録と
    # csv_exports（同じ read transaction）→ 専用 DB（決定記録 0100 §2.6 / §2.8）
    try:
        inputs = read_replay_export_inputs(args.replay_path)
    except ReplayBindingError as error:
        LOGGER.error(
            "--replay-path の manifest を拒否した（dataset を作らない）",
            extra={logs.FIELDS_KEY: {"reason": str(error), "check": error.check}},
        )
        return 1
    if inputs.records is None:
        LOGGER.error(
            "export の manifest の無い入力から Dataset v2 は作らない（決定記録 0100 §2.7）",
            extra={logs.FIELDS_KEY: {"reason": "manifest が無い"}},
        )
        return 1
    source_run = _source_run(args, inputs.source_sha256)
    spec = DatasetSpecV2.model_validate(
        {
            **_common_spec(args),
            "anchor_stride_ms": args.anchor_stride_ms,
            "action_max_in_step_range": args.action_max_in_step_range,
            "action_step_ms": args.action_step_ms,
            "action_steps": args.action_steps,
            "action_stale_after_ms": args.action_stale_after_ms,
        }
    )
    try:
        # 較正の記録・csv_exports の行・run の期間の trace を同じ read transaction で読む
        # （決定記録 0100 §2.6 / 0116 §2.1）
        production = read_training_production(
            args.calibration_history_db,
            inputs.records,
            start_ms=source_run.start_ms,
            end_ms=source_run.end_ms,
        )
        check_production_traces(production, start_ms=source_run.start_ms)
    except CalibrationHistoryError as error:
        LOGGER.error(
            "較正の変更の記録を読めない（dataset を作らない）",
            extra={logs.FIELDS_KEY: {"reason": str(error)}},
        )
        return 1
    except ExportBindingError as error:
        LOGGER.error(
            "入力の export を本番の DB の csv_exports と照合できない（dataset を作らない）",
            extra={logs.FIELDS_KEY: {"reason": str(error)}},
        )
        return 1
    except ValueError as error:
        LOGGER.error(
            "本番の DB の trace から作れない期間（dataset を作らない）",
            extra={logs.FIELDS_KEY: {"reason": str(error)}},
        )
        return 1
    # 本番の trace（数日分で数 GB）は専用 DB へ写したら手放す（#416）
    history, production_binding, production_traces = (
        production.history,
        production.binding,
        production.traces,
    )
    del production
    rules = QualityRules.from_yaml(args.quality_config)
    try:
        with SqliteStore(args.db, rules=rules, clock=WallClock()) as store:
            # ControlTick の出どころは本番の DB の trace（決定記録 0116 §2.1）
            copy_or_match_production_traces(store, production_traces)
            del production_traces
            dataset = ThermalDatasetV2Builder(store).build(
                source_run=source_run,
                spec=spec,
                declared_changes=declared.changes,
                calibration_history=history,
                export_binding=production_binding,
            )
    except ValueError as error:
        LOGGER.error(
            "Dataset v2 の生成を拒否した",
            extra={
                logs.FIELDS_KEY: {
                    "reason": str(error),
                    "declared_changes_sha256": declared.file_sha256,
                }
            },
        )
        return 1
    in_period: list[DeclaredChange] = []
    if dataset.examples:
        start_ms = min(example.history_start_ms for example in dataset.examples)
        end_ms = max(example.label_end_ms for example in dataset.examples)
        in_period = [
            change for change in declared.changes if _overlaps(change.ts_ms, start_ms, end_ms)
        ]
    ignored = [change for change in in_period if change.kind is not ChangeKind.CALIBRATION_CHANGED]
    if ignored:
        # 0087 §2.6 のまま生成には効かせない。人が気づけるよう警告だけ出す（0109 §5 #2）
        LOGGER.warning(
            "検査に使わない種別の宣言が期間の中にある（生成は拒否しない）",
            extra={
                logs.FIELDS_KEY: {
                    "changes": [
                        {"kind": change.kind.value, "ts_ms": change.ts_ms} for change in ignored
                    ]
                }
            },
        )
    write_dataset(dataset, args.output_root, args.artifact_name)
    LOGGER.info(
        "Dataset v2 を書き出した",
        extra={
            logs.FIELDS_KEY: {
                "artifact_name": args.artifact_name,
                "example_count": dataset.manifest.example_count,
                "excluded": dataset.manifest.excluded.model_dump(),
                "declared_changes_sha256": declared.file_sha256,
                "declared_changes": len(declared.changes),
                "declared_changes_by_kind": dict(
                    sorted(Counter(change.kind.value for change in declared.changes).items())
                ),
                "declared_changes_in_period": len(in_period),
                "calibration_history_rows": len(history.rows),
            }
        },
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    logs.configure(args.log_level)
    if args.replay_path is None:
        parser.error("source-kind=replay には --replay-path が要る")
    if args.dataset_version == 1:
        # v1 には較正の検査が無い。渡しても効かない値を黙って受け取らない（0109 §5 #6）
        given = [_option_name(dest) for dest in _V2_ONLY_OPTIONS if getattr(args, dest) is not None]
        if given:
            parser.error(f"--dataset-version 1 では渡さない: {', '.join(given)}")
        return _main_v1(args)
    missing = [_option_name(dest) for dest in _V2_ONLY_OPTIONS if getattr(args, dest) is None]
    if missing:
        parser.error(f"--dataset-version 2 には必須: {', '.join(missing)}")
    if _same_file(args.calibration_history_db, args.db):
        # 専用 DB は空の calibration_activations を持ちうる。0件の dataset では被覆の検査を
        # 素通りするので、取り違えとして先に拒否する（0109 §7）
        parser.error("--calibration-history-db に専用 DB（--db）と同じ DB は渡さない")
    return _main_v2(args)


if __name__ == "__main__":
    raise SystemExit(main())
