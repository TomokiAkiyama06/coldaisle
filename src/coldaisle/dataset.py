"""Thermal dataset の決定的な組み立てとartifact出力。#83

保存済みTelemetryと #82 のControlTickを同じ時刻軸で結合する。実機・Fan Hardwareへは
到達せず、ReplayをSQLiteへ投入したあとも同じ関数で再生成できる。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from contextlib import suppress
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from coldaisle.calibration_log import calibration_change_points, require_history_covers
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
    SourceRun,
    TargetFrame,
    ThermalDataset,
    ThermalDatasetV2,
    WindowFrame,
    examples_jsonl_bytes,
    examples_sha256,
    reject_calibration_changes,
)
from coldaisle.control.schema import ControlTick, PerZone, Zone
from coldaisle.ingest.replay import replay_sha256
from coldaisle.store import Quality, QualityRules, SeriesPoint, SqliteStore
from coldaisle.store.calibration_history import CalibrationHistory
from coldaisle.store.models import ControlTraceRecord, SequencedControlTrace

MANIFEST_FILENAME = "manifest.json"
EXAMPLES_FILENAME = "examples.jsonl"
_ARTIFACT_ALIAS = re.compile(r"^dataset-[0-9a-f]{32}$")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_LOCK_FILENAME = ".coldaisle-dataset.lock"
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

    def build(self, *, source_run: SourceRun, spec: DatasetSpec) -> ThermalDataset:
        """1 source runを決定的に変換する。

        action候補はrun内のControlTickである。完全なhistoryとtarget探索範囲をrun内に
        持てない端のtickは採用しない。target観測そのものが無い場合は、行を捨てずに
        ``missing_mask`` を立てる。
        """
        with self._store.read_snapshot():
            _validate_dedicated_source_db(self._store, source_run)
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

    def __init__(self, traces: tuple[SequencedControlTrace, ...]) -> None:
        self.traces = traces
        self.ticks = tuple(_parse_tick(trace) for trace in traces)
        self.ts = tuple(trace.ts_ms for trace in traces)
        self.tick_ids = tuple(trace.tick_id for trace in traces)
        self.effective: tuple[_ZoneDemands, ...] = tuple(
            (
                tick.zones.front.demand.effective,
                tick.zones.rear.demand.effective,
                tick.zones.top.demand.effective,
            )
            for tick, _raw in self.ticks
        )
        if any(later <= earlier for earlier, later in zip(self.ts, self.ts[1:], strict=False)):
            # 黙って並べ替えない。壁時計が戻った run では「直近」が実行の順と一致しない
            raise ValueError(
                "seq の順に並べた ControlTick の ts_ms が狭義単調増加でない run から"
                " Dataset v2 は作らない"
            )

    def as_of(self, ts_ms: int) -> int:
        """``ts_ms`` 以前で直近の tick の位置。無ければ -1。"""
        return bisect_right(self.ts, ts_ms) - 1


def _assess_anchor(
    ticks: _RunTicks, index: int, spec: DatasetSpecV2
) -> tuple[ActionExclusionReason | None, tuple[int, ...]]:
    """anchor ``index`` の example を作れるかを 0087 §2.2 / §2.3 / §2.7 の順で判定する。

    作れるなら ``(None, step ごとの as-of の tick の位置)`` を返す。作れなければ、最初に当たった
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

    # 3. 区間内の変化: 区間 [開始, 終端) の中の tick の effective が step の値と違う。
    #    終端（次の step の開始時刻）の tick は数えない
    for step, source in enumerate(as_of):
        start_ms = anchor_ms + step * step_ms
        value = ticks.effective[source]
        inside = range(bisect_left(ticks.ts, start_ms), bisect_left(ticks.ts, start_ms + step_ms))
        if any(ticks.effective[position] != value for position in inside):
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
    return None, tuple(as_of)


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
    ) -> ThermalDatasetV2:
        """1 source run を決定的に変換する。

        ``declared_changes`` は 0056 §2.5 の宣言された変更で、**既定値を持たない**。宣言が無い場合も
        空の tuple を明示する（0087 §2.6）。そのうち ``calibration_changed`` が全 example の期間に
        あれば生成を拒否する。

        ``calibration_history`` は本番の DB から読み取り専用で読んだ較正の変更の記録で、**既定値を
        持たない**（決定記録 0099 §2.6）。専用 DB（再生）には記録が無いので別に渡す。example が
        1件以上あれば、

        - 全 example の期間の先頭以前に記録の行が無ければ拒否する（被覆）
        - 各行を CSV の秒の切り捨ての区間 ``[floor(ts), ts]`` とし、両端を変更の時刻として
          宣言との**和集合**で 0087 §2.6 の検査へ渡す

        example が0件の dataset では被覆と変更の検査を行わない（記録の読み込みの検証は
        :func:`~coldaisle.store.calibration_history.read_calibration_history` が済ませている）。

        次の run からは生成しない（example の除外ではなく、生成全体の拒否。0087 §2.1）。

        - ``seq`` の順に並べた ControlTick の ``ts_ms`` が狭義単調増加でない
        - 移行前の行（``seq ≤ legacy_through_seq``）を含む

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
        calibration_changes = tuple(
            change.ts_ms
            for change in declared_changes
            if change.kind is ChangeKind.CALIBRATION_CHANGED
        )
        with self._store.read_snapshot():
            _validate_dedicated_source_db(self._store, source_run)
            traces = self._store.control_traces_in_seq_order(source_run.start_ms, source_run.end_ms)
            legacy_through_seq = self._store.control_trace_legacy_through_seq()
            if any(trace.seq <= legacy_through_seq for trace in traces):
                # 移行前の行の seq は記録した順を表さない（0007 が (ts_ms, tick_id) の順に振った）
                raise ValueError("移行前の ControlTick を含む run から Dataset v2 は作らない")
            ticks = _RunTicks(traces)
            earliest_action_ms = source_run.start_ms + spec.window_ms
            eligible = tuple(
                index
                for index, ts_ms in enumerate(ticks.ts)
                if ts_ms >= earliest_action_ms and ts_ms + spec.horizons_ms[-1] < source_run.end_ms
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
                reason, as_of = _assess_anchor(ticks, index, spec)
                if reason is not None:
                    excluded[reason] += 1
                    continue
                examples.append(
                    _example_v2(
                        ticks=ticks,
                        index=index,
                        as_of=as_of,
                        source_run=source_run,
                        spec=spec,
                        points=points,
                    )
                )
        built = tuple(examples)
        if built:
            require_history_covers(
                calibration_history, min(example.history_start_ms for example in built)
            )
            calibration_changes += calibration_change_points(calibration_history)
        reject_calibration_changes(built, calibration_changes)
        return ThermalDatasetV2(
            manifest=DatasetManifestV2(
                spec=spec,
                source_runs=(source_run,),
                telemetry_sha256=_telemetry_digest(metrics, points),
                control_trace_sha256=_sequenced_trace_digest(traces),
                examples_sha256=examples_sha256(built),
                example_count=len(built),
                excluded=ActionExclusionCounts(
                    stale=excluded[ActionExclusionReason.STALE],
                    discontinuity=excluded[ActionExclusionReason.DISCONTINUITY],
                    in_step_change=excluded[ActionExclusionReason.IN_STEP_CHANGE],
                    restart=excluded[ActionExclusionReason.RESTART],
                    tick_id_gap=excluded[ActionExclusionReason.TICK_ID_GAP],
                ),
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
    as_of: tuple[int, ...],
    source_run: SourceRun,
    spec: DatasetSpecV2,
    points: dict[str, SeriesIndex],
) -> DatasetExampleV2:
    tick, raw = ticks.ticks[index]
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
                source_ts_ms=ticks.ts[source],
                source_tick_id=ticks.tick_ids[source],
                effective_demand=_zone_demands(ticks.effective[source]),
            )
            for step, source in enumerate(as_of)
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


def _sequenced_trace_digest(traces: tuple[SequencedControlTrace, ...]) -> str:
    """Dataset v2 の生成に読んだ ControlTick 全体（記録した順）の SHA-256。

    v2 は anchor の tick だけでなく、prior_action・格子の as-of・連続と tick_id の検査に run の全
    tick を使うので、run の全 tick を ``seq`` 付きで hash する。
    """
    digest = hashlib.sha256()
    for trace in traces:
        _update_digest(
            digest,
            [
                trace.seq,
                trace.ts_ms,
                trace.tick_id,
                trace.schema_version,
                json.loads(trace.trace_json),
            ],
        )
    return digest.hexdigest()


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


def _open_output_root(output_root: Path) -> int:
    """pathの各要素をsymlink非追従で開き、必要なdirectoryだけ作る。"""
    if ".." in output_root.parts:
        raise ValueError("output rootに '..' は使えない")
    absolute = output_root if output_root.is_absolute() else Path.cwd() / output_root
    directory_fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            if component in ("", "."):
                continue
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
    if isinstance(dataset, ThermalDatasetV2):
        dataset = ThermalDatasetV2.model_validate_json(dataset.model_dump_json())
    else:
        dataset = ThermalDataset.model_validate_json(dataset.model_dump_json())
    examples_payload = examples_jsonl_bytes(dataset.examples)
    if hashlib.sha256(examples_payload).hexdigest() != dataset.manifest.examples_sha256:
        raise ValueError("manifestのexamples_sha256が書き出すbytesと一致しない")
    manifest_payload = (dataset.manifest.model_dump_json(indent=2) + "\n").encode()

    root_fd = _open_output_root(output_root)
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


def build_parser() -> argparse.ArgumentParser:
    """実測で選ぶ値をすべて必須にしたCLI parserを返す。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-dataset", description="SQLiteからThermal Dataset v1を生成"
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
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    source_kind = DatasetSourceKind(args.source_kind)
    if args.replay_path is None:
        parser.error("source-kind=replay には --replay-path が要る")
    source_sha256 = replay_fingerprint(args.replay_path)
    source_run = SourceRun(
        run_id=args.run_alias,
        kind=source_kind,
        start_ms=args.start_ms,
        end_ms=args.end_ms,
        source_refs=tuple(args.source_alias),
        source_sha256=source_sha256,
    )
    spec = DatasetSpec(
        window_ms=args.window_ms,
        sample_period_ms=args.sample_period_ms,
        horizons_ms=tuple(args.horizon_ms),
        target_tolerance_ms=args.target_tolerance_ms,
        stale_after_ms=args.stale_after_ms,
        feature_metrics=tuple(args.feature_metric),
        target_metrics=tuple(args.target_metric),
    )
    rules = QualityRules.from_yaml(args.quality_config)
    with SqliteStore(args.db, rules=rules, clock=WallClock()) as store:
        dataset = ThermalDatasetBuilder(store).build(source_run=source_run, spec=spec)
    write_dataset(dataset, args.output_root, args.artifact_name)


if __name__ == "__main__":
    main()
