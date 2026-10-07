"""合成の起点: Offline Evaluation（#91 / 決定記録 0054）。

保存済み decision trace と観測を読み、Controller 構成の比較レポートを1つ書き出す。
**1コマンドで再現できる**ように、比べる run は manifest（YAML）で与える。

```bash
uv run coldaisle-evaluate --runs config/evaluation-runs.yaml --out var/evaluation.json
```

**書き込みも制御もしない。** Store は読み取りだけに使い、Fan へ届く経路は持たない。
レポートに生成時刻は入らないので、同じ入力からは同じ bytes が出る（0054 §2.7）。
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import quote

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle import logs
from coldaisle.control.acoustic import ConfiguredAcousticCostModel
from coldaisle.control.config import ControlConfig
from coldaisle.control.evaluation import (
    EvaluationConfig,
    EvaluationContext,
    EvaluationReport,
    EvaluationRun,
    evaluate,
)
from coldaisle.control.schema import ControlTick
from coldaisle.control.shadow import OutcomeObservation, read_shadow_jsonl
from coldaisle.metrics import MetricCatalog
from coldaisle.store import migrations
from coldaisle.store.models import ControlTraceRecord, Quality, SeriesPoint, validate_metric

LOGGER = logging.getLogger("coldaisle.evaluate")

RUNS_MANIFEST_VERSION: Literal[1] = 1


def _sequence_to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class _Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RunSpec(_Manifest):
    """比較する run 1つ。**時刻は明示する**（「直近」のような相対指定を置かない）。"""

    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    split_boundaries_ms: Annotated[tuple[int, ...], BeforeValidator(_sequence_to_tuple)] = ()
    """時系列 split の境界。**最後の区間が holdout**（決定記録 0054 §2.5）。"""
    shadow_jsonl: str | None = Field(default=None, min_length=1, max_length=500)
    """#90 の export。省略すると同じ照合器で作り直す。"""

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError(f"{self.run_id}: start_ms < end_ms にする")
        boundaries = self.split_boundaries_ms
        if tuple(sorted(set(boundaries))) != boundaries:
            raise ValueError(f"{self.run_id}: split_boundaries_ms は重複なし昇順にする")
        if any(not self.start_ms < boundary < self.end_ms for boundary in boundaries):
            raise ValueError(f"{self.run_id}: split の境界を run の範囲の中に置く")
        return self


class RunsManifest(_Manifest):
    """比較する run 一式。"""

    schema_version: Literal[1]
    runs: Annotated[tuple[RunSpec, ...], BeforeValidator(_sequence_to_tuple), Field(min_length=1)]

    @model_validator(mode="after")
    def _run_ids_are_unique(self) -> Self:
        ids = tuple(run.run_id for run in self.runs)
        if len(set(ids)) != len(ids):
            raise ValueError("同じ run_id の run を2つ置かない")
        return self

    @classmethod
    def from_file(cls, path: Path) -> RunsManifest:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"run manifest が辞書ではない: {path.name}")
        return cls.model_validate(loaded)


EVIDENCE_MIGRATION = "control_traces"
"""評価が読む表が揃う migration の slug（`NNNN_<slug>.sql`）。

`readings`（0001）と `control_traces`（0002）の両方が要る。**版を数字で写さず、
migration の名前から引く**（写し元と食い違う上限を置かないため。0054 §2.6 と同じ規則）。
"""


def required_schema_version() -> int:
    """評価に必要な最小の schema 版。**migration の並びから引く。**"""
    for migration in migrations.discover():
        if migration.path.stem.endswith(EVIDENCE_MIGRATION):
            return migration.version
    raise RuntimeError(f"{EVIDENCE_MIGRATION} の migration が見つからない")


class EvidenceDatabaseError(RuntimeError):
    """証拠の DB を読めない（存在しない・スキーマが古い / 新しい）。"""


SQLITE_MAGIC = b"SQLite format 3\x00"
"""SQLite の file header。**path を開く前に、中身を1度だけ読んで確かめる。**"""

SIDECAR_SUFFIXES = ("-wal", "-journal")
"""書き込みが途中である／WAL に未 checkpoint の内容がある、ことを示す添え file。"""

SQLITE_SIDECARS = ("-wal", "-journal", "-shm")
"""SQLite が DB の隣に置く添え file すべて。`--out` がこれらを指しても DB を壊しうる。"""


def sidecar_bases(path: Path) -> tuple[Path, ...]:
    """添え file を探す基準: 渡された path と、**その実体**（symlink を解いた path）。

    SQLite が添え file を置くのは**実体の隣**である。symlink の隣だけを見ると、
    実体の隣にある未 checkpoint の WAL を見落とす（#227）。
    """
    return tuple(dict.fromkeys((path, path.resolve())))


class EvidenceOutputError(ValueError):
    """`--out` が入力（証拠の DB・添え file・manifest・設定）を指している（#227）。"""


def control_config_files(directory: Path) -> tuple[Path, ...]:
    """`--control-config` の4ファイル（`--out` で上書きさせない入力）。"""
    return tuple(
        directory / name
        for name in ("fan-hardware.yaml", "safety.yaml", "fan-policy.yaml", "air-balance.yaml")
    )


def _same_file(left: Path, right: Path) -> bool:
    if left.exists() and right.exists():
        return os.path.samefile(left, right)
    return left.resolve() == right.resolve()


def refuse_output_on_inputs(
    out: Path,
    *,
    db: Path,
    inputs: Iterable[Path] = (),
    input_roots: Iterable[Path] = (),
) -> None:
    """`--out` が入力を指していれば `EvidenceOutputError` で拒む（#227）。

    証拠を読むだけの CLI は報告を `--out` へ書く（`write_text` / `os.replace()`）。
    `--out` が DB を指すと**証拠を報告で上書きする**。読むだけの CLI を破壊的な操作に
    しないため、**どの入力を読むより前に**呼ぶ（壊れた入力を読んで落ちる前に拒む）。

    - `db` とその添え file（symlink の隣と実体の隣の両方）
    - `inputs`: 読む file（manifest・設定など）
    - `input_roots`: 中身をすべて入力として読むディレクトリ（Model Registry など）。
      `--out` がこの下にあれば拒む
    """
    candidates = [
        db,
        *(
            base.with_name(base.name + suffix)
            for base in sidecar_bases(db)
            for suffix in SQLITE_SIDECARS
        ),
        *inputs,
    ]
    for path in candidates:
        if _same_file(out, path):
            raise EvidenceOutputError(f"--out が入力のファイルを指している: {path}")
    resolved = out.resolve()
    for root in input_roots:
        if resolved.is_relative_to(root.resolve()):
            raise EvidenceOutputError(f"--out が入力のディレクトリの中を指している: {root}")
        # hard link は resolve しても外の path のまま。既存の `--out` は中の file と
        # inode で照らす（書き出しが中の入力を切り詰めないように）。
        if out.is_file() and root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and os.path.samefile(out, path):
                    raise EvidenceOutputError(f"--out が入力のファイルを指している: {path}")


class EvidenceDatabase:
    """評価が読む decision trace と観測。**開いても何も作らず、何も変えない。**

    `SqliteStore` は開くだけで WAL を設定し、未適用の migration を当て、path が
    無ければ**作る**。証拠として読む DB をそれで開くと、評価が証拠を書き換えてしまう
    （古い run の DB を開いた瞬間にスキーマが上がる）。

    **`mode=ro` でも足りない。** WAL の DB を `mode=ro` で開くと `-shm` / `-wal` が
    **作られる**ので、「何も変えない」が守れず、読み取り専用の媒体では開けもしない。
    ここでは `immutable=1` を使う。添え file を作らず、ロックも取らない。

    **`immutable=1` は「DB が静止している」ことを前提にする。** 動いている書き手が
    いると未定義で、WAL に未 checkpoint の内容があると**それを黙って無視して古い
    断面を読む**（table が丸ごと見えないことさえある）。証拠を読み違えるくらいなら
    読まないほうがよいので、`-wal` / `-journal` が中身を持っていれば**開かずに落とす**。
    その場合は checkpoint するか、別の場所へ複製してから渡す。
    """

    __slots__ = ("_conn", "_version")

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            # `immutable=1` は作らないが、理由が分かるメッセージで落とす。
            raise EvidenceDatabaseError(f"証拠の DB が無い: {path}")
        header = path.read_bytes()[: len(SQLITE_MAGIC)]
        if header != SQLITE_MAGIC:
            raise EvidenceDatabaseError(f"証拠の DB が SQLite の file ではない: {path}")
        # 実体の隣も確かめる。呼び出し側が symlink のまま渡しても、SQLite が使う
        # 添え file は実体の隣にある（#227）。
        for sidecar in (
            base.with_name(base.name + suffix)
            for base in sidecar_bases(path)
            for suffix in SIDECAR_SUFFIXES
        ):
            if sidecar.is_file() and sidecar.stat().st_size > 0:
                raise EvidenceDatabaseError(
                    f"証拠の DB が静止していない（{sidecar.name} が残っている）: {path}。"
                    f"**評価は DB を書き換えないので checkpoint もしない。**"
                    f" 書き手を止めて checkpoint するか、複製してから渡す"
                )
        uri = f"file:{quote(str(path.resolve()))}?immutable=1"
        try:
            self._conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        except sqlite3.Error as exc:
            raise EvidenceDatabaseError(f"証拠の DB を読み取り専用で開けない: {path}") from exc
        self._conn.row_factory = sqlite3.Row
        self._version = migrations.current_version(self._conn)
        required = required_schema_version()
        known = len(migrations.discover())
        if self._version < required:
            self.close()
            raise EvidenceDatabaseError(
                f"証拠の DB のスキーマが古い（version={self._version}; "
                f"評価には {required} 以上が要る）。**評価は DB を書き換えないので、"
                f"必要なら別の手段で移行する**: {path}"
            )
        if self._version > known:
            self.close()
            raise EvidenceDatabaseError(
                f"証拠の DB がこのコードより新しい（version={self._version}; 既知={known}）: {path}"
            )

    @property
    def schema_version(self) -> int:
        """開いた時点の適用済み版。**変えていない。**"""
        return self._version

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """複数の読み出しを1つの読み取りトランザクションにまとめる。

        `immutable=1` では file が変わらない前提なので断面は元々1つだが、
        読み出しの単位を明示しておく。
        """
        self._conn.execute("BEGIN")
        try:
            yield
        finally:
            self._conn.execute("COMMIT")

    def control_traces(self, start_ms: int, end_ms: int) -> tuple[ControlTraceRecord, ...]:
        """`[start_ms, end_ms)` の decision trace（`SqliteStore` と同じ並び）。"""
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("control trace の期間が不正")
        rows = self._conn.execute(
            "SELECT ts_ms, tick_id, schema_version, trace_json FROM control_traces "
            "WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms, tick_id",
            (start_ms, end_ms),
        ).fetchall()
        return tuple(
            ControlTraceRecord(
                ts_ms=row["ts_ms"],
                tick_id=row["tick_id"],
                schema_version=row["schema_version"],
                trace_json=row["trace_json"],
            )
            for row in rows
        )

    def series(self, metric: str, start_ms: int, end_ms: int) -> tuple[SeriesPoint, ...]:
        """`[start_ms, end_ms)` の生データ。**品質は保存された値をそのまま読む。**"""
        validate_metric(metric)
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("観測の期間が不正")
        rows = self._conn.execute(
            "SELECT ts_ms, value, quality FROM readings "
            "WHERE metric = ? AND ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms",
            (metric, start_ms, end_ms),
        ).fetchall()
        return tuple(
            SeriesPoint(
                ts_ms=int(row["ts_ms"]), value=row["value"], quality=Quality(row["quality"])
            )
            for row in rows
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> EvidenceDatabase:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def load_run(
    store: EvidenceDatabase, spec: RunSpec, *, context: EvaluationContext, base: Path
) -> EvaluationRun:
    """1つの run の trace と観測を読む。**必要な metric は記録から決める。**

    予測の対象 metric は `ShadowPrediction` に記録されているものを使う。評価設定に
    写すと、モデルの target を変えたときに評価だけが古い metric を見続ける。
    """
    traces = store.control_traces(spec.start_ms, spec.end_ms)
    wanted = set(context.config.temperature_metrics)
    wanted.add(context.config.room_temperature_metric)
    for delta in context.deltas:
        wanted.update({delta.minuend, delta.subtrahend})
    wanted.update(_predicted_metrics(traces))
    observations: list[OutcomeObservation] = []
    for metric in sorted(wanted):
        observations.extend(
            OutcomeObservation(
                metric=metric, ts_ms=point.ts_ms, value=point.value, quality=point.quality
            )
            for point in store.series(metric, spec.start_ms, spec.end_ms)
        )
    shadow = None
    if spec.shadow_jsonl is not None:
        with (base / spec.shadow_jsonl).open(encoding="utf-8") as stream:
            shadow = read_shadow_jsonl(stream)
    return EvaluationRun(
        run_id=spec.run_id,
        traces=tuple(traces),
        observations=tuple(observations),
        split_boundaries_ms=spec.split_boundaries_ms,
        shadow=shadow,
    )


def _predicted_metrics(traces: tuple[ControlTraceRecord, ...]) -> set[str]:
    """counterfactual が予測した metric（照合に要る観測を読むため）。"""
    metrics: set[str] = set()
    for trace in traces:
        tick = ControlTick.model_validate_json(trace.trace_json)
        if tick.shadow is None:
            continue
        for counterfactual in tick.shadow.counterfactuals:
            if counterfactual.prediction is None:
                continue
            for target in counterfactual.prediction.targets:
                metrics.update(target.values)
    return metrics


def build_context(
    *,
    config_path: Path,
    control_dir: Path,
    metrics_path: Path,
    acoustic_path: Path | None,
) -> EvaluationContext:
    """設定一式を読み、素性（hash）とともに束ねる。"""
    config, config_sha256 = EvaluationConfig.from_file(config_path)
    catalog = MetricCatalog.from_yaml(metrics_path)
    catalog_sha256 = sha256(metrics_path.read_bytes()).hexdigest()
    acoustic = None
    acoustic_sha256 = None
    if acoustic_path is not None:
        acoustic = ConfiguredAcousticCostModel.from_file(acoustic_path)
        acoustic_sha256 = sha256(acoustic_path.read_bytes()).hexdigest()
    return EvaluationContext.build(
        config=config,
        config_sha256=config_sha256,
        control=ControlConfig.from_directory(control_dir),
        catalog=catalog,
        catalog_sha256=catalog_sha256,
        acoustic=acoustic,
        acoustic_sha256=acoustic_sha256,
    )


def render(report: EvaluationReport) -> str:
    """レポートを JSON へ。**無い値は書かない**（0 と区別できる形にする。0053 §2.4）。"""
    return report.model_dump_json(exclude_none=True, indent=2) + "\n"


def write(report: EvaluationReport, path: Path) -> Path:
    """レポートを書き出す。同じ入力からは同じ bytes になる。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coldaisle-evaluate",
        description="Offline Evaluation: Controller 構成の比較レポート（#91）",
    )
    parser.add_argument("--runs", type=Path, required=True, help="比較する run の manifest")
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--config", type=Path, default=Path("config/evaluation.yaml"))
    parser.add_argument(
        "--control-config",
        type=Path,
        default=Path("config"),
        help=(
            "fan-hardware.yaml / safety.yaml / fan-policy.yaml / air-balance.yaml"
            " のあるディレクトリ（4ファイルとも必須）"
        ),
    )
    parser.add_argument("--metrics", type=Path, default=Path("config/metrics.yaml"))
    parser.add_argument(
        "--acoustic",
        type=Path,
        default=None,
        help="acoustic.yaml。省略すると Acoustic Cost は出さない（欠測として残す）",
    )
    parser.add_argument("--out", type=Path, default=Path("var/evaluation.json"))
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)

    try:
        # **どの入力を開くより前に**確かめる（#227）。
        refuse_output_on_inputs(
            args.out,
            db=args.db,
            inputs=(
                args.runs,
                args.config,
                *control_config_files(args.control_config),
                args.metrics,
                *(() if args.acoustic is None else (args.acoustic,)),
            ),
        )
        manifest = RunsManifest.from_file(args.runs)
        # manifest が名指す export も入力。読む前に確かめる。
        refuse_output_on_inputs(
            args.out,
            db=args.db,
            inputs=(
                args.runs.parent / spec.shadow_jsonl
                for spec in manifest.runs
                if spec.shadow_jsonl is not None
            ),
        )
    except EvidenceOutputError as error:
        LOGGER.error(
            "Offline Evaluation を拒否した（何も書かない）",
            extra={logs.FIELDS_KEY: {"reason": str(error), "out": str(args.out)}},
        )
        return 1
    context = build_context(
        config_path=args.config,
        control_dir=args.control_config,
        metrics_path=args.metrics,
        acoustic_path=args.acoustic,
    )
    # **証拠の DB は読み取り専用で開く。** `SqliteStore` は開くだけで WAL を設定し、
    # 未適用の migration を当て、path が無ければ作る。
    with EvidenceDatabase(args.db) as store, store.snapshot():
        runs = [
            load_run(store, spec, context=context, base=args.runs.parent) for spec in manifest.runs
        ]

    report = evaluate(runs, context=context)
    path = write(report, args.out)
    LOGGER.info(
        "Offline Evaluation のレポートを書き出した",
        extra={
            logs.FIELDS_KEY: {
                "path": str(path),
                "runs": len(report.provenance.runs),
                "segments": len(report.segments),
                "conditions_sha256": report.provenance.conditions_sha256,
                "db_schema_version": store.schema_version,
                "gates": {gate.arm_key: gate.outcome.value for gate in report.gates},
            }
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
