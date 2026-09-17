"""Thermal dataset の決定的な組み立てとartifact出力。#83

保存済みTelemetryと #82 のControlTickを同じ時刻軸で結合する。実機・Fan Hardwareへは
到達せず、ReplayをSQLiteへ投入したあとも同じ関数で再生成できる。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from coldaisle.clock import WallClock
from coldaisle.control.model.dataset import (
    ActionContext,
    ActionZone,
    DatasetExample,
    DatasetManifest,
    DatasetSourceKind,
    DatasetSpec,
    DatasetWorkloadRegime,
    SourceRun,
    TargetFrame,
    ThermalDataset,
    WindowFrame,
)
from coldaisle.control.schema import ControlTick, PerZone, Zone
from coldaisle.ingest.replay import csv_files
from coldaisle.store import Quality, QualityRules, SeriesPoint, SqliteStore
from coldaisle.store.models import ControlTraceRecord

MANIFEST_FILENAME = "manifest.json"
EXAMPLES_FILENAME = "examples.jsonl"
SeriesIndex = tuple[tuple[int, ...], tuple[SeriesPoint, ...]]


def replay_fingerprint(path: Path) -> tuple[tuple[str, ...], str]:
    """Replayが読むCSV集合の論理名とSHA-256を返す。

    絶対パスはhashにもmanifestにも入れない。同名ファイルの順序と内容を長さ付きで
    hashするため、ファイル境界が違う入力を同一runとして扱わない。
    """
    files = csv_files(path)
    digest = hashlib.sha256()
    refs: list[str] = []
    for csv_path in files:
        name = csv_path.name
        content = csv_path.read_bytes()
        encoded_name = name.encode("utf-8")
        refs.append(name)
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return tuple(refs), digest.hexdigest()


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
        earliest_action_ms = source_run.start_ms + spec.window_ms
        latest_label_margin_ms = spec.horizons_ms[-1] + spec.target_tolerance_ms
        traces = self._store.control_traces(earliest_action_ms, source_run.end_ms)
        eligible = tuple(
            trace for trace in traces if trace.ts_ms + latest_label_margin_ms < source_run.end_ms
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
        action = PerZone(
            front=_action_zone(tick, Zone.FRONT),
            rear=_action_zone(tick, Zone.REAR),
            top=_action_zone(tick, Zone.TOP),
        )
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
        context = ActionContext(
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


def _parse_tick(trace: ControlTraceRecord) -> tuple[ControlTick, dict[str, object]]:
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
    spec: DatasetSpec,
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
        missing[metric] = point.value is None or point.quality is Quality.MISSING
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
        missing[metric] = point.value is None or point.quality is Quality.MISSING
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


def write_dataset(dataset: ThermalDataset, out_dir: Path) -> tuple[Path, Path]:
    """manifest JSONと学習例JSON Linesを決定的な順序で書く。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / MANIFEST_FILENAME
    examples_path = out_dir / EXAMPLES_FILENAME
    manifest_path.write_text(dataset.manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    with examples_path.open("w", encoding="utf-8", newline="\n") as handle:
        for example in dataset.examples:
            handle.write(example.model_dump_json())
            handle.write("\n")
    return manifest_path, examples_path


def build_parser() -> argparse.ArgumentParser:
    """実測で選ぶ値をすべて必須にしたCLI parserを返す。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-dataset", description="SQLiteからThermal Dataset v1を生成"
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--quality-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--source-kind", choices=[kind.value for kind in DatasetSourceKind], required=True
    )
    parser.add_argument(
        "--replay-path",
        type=Path,
        help="source-kind=replayで必須。Replay対象から参照名とSHA-256を計算する",
    )
    parser.add_argument("--source-ref", action="append")
    parser.add_argument("--source-sha256")
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
    if source_kind is DatasetSourceKind.REPLAY:
        if args.replay_path is None:
            parser.error("source-kind=replay には --replay-path が要る")
        if args.source_ref is not None or args.source_sha256 is not None:
            parser.error("replayのsource-ref / SHA-256は--replay-pathから自動計算する")
        source_refs, source_sha256 = replay_fingerprint(args.replay_path)
    else:
        if args.replay_path is not None:
            parser.error("--replay-path は source-kind=replay だけで使える")
        if args.source_ref is None or args.source_sha256 is None:
            parser.error("replay以外は --source-ref と --source-sha256 が要る")
        source_refs = tuple(args.source_ref)
        source_sha256 = args.source_sha256
    source_run = SourceRun(
        run_id=args.run_id,
        kind=source_kind,
        start_ms=args.start_ms,
        end_ms=args.end_ms,
        source_refs=source_refs,
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
    write_dataset(dataset, args.output_dir)


if __name__ == "__main__":
    main()
