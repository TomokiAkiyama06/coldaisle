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
    examples_jsonl_bytes,
    examples_sha256,
)
from coldaisle.control.schema import ControlTick, PerZone, Zone
from coldaisle.ingest.replay import replay_sha256
from coldaisle.store import Quality, QualityRules, SeriesPoint, SqliteStore
from coldaisle.store.models import ControlTraceRecord

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
    dataset: ThermalDataset,
    output_root: Path,
    artifact_name: str,
) -> tuple[Path, Path]:
    """検証済み2ファイルをstagingから原子的に公開する。

    既存artifactは常に拒否する。全writerが同じparent lockを保持して不存在を確認し、
    同じparent内のstaging directoryをmacOS / Ubuntu共通の``os.rename``で公開する。
    """
    if _ARTIFACT_ALIAS.fullmatch(artifact_name) is None:
        raise ValueError("artifact_nameは公開用の dataset-<32 hex> aliasにする")
    # model_copy(update=...)等でvalidationを迂回したinstanceも書き出し境界で拒否する。
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
