"""学習の入口の Dataset v2 の検査（合成の起点）。決定記録 0100 §2.8 / 0112 §2.1 / §2.3。

学習に使う Dataset v2 を、元の再生の入力（``--replay-path``）と本番の DB に遡って確かめる。

1. dataset が export の束縛（``ReplayBindingV2``）を持ち、source run が1つであること
   （0112 §2.3 の表 3）
2. 元の入力の fingerprint と manifest の束縛が、dataset の ``SourceRun`` と
   ``ReplayBindingV2`` に一致すること（0112 §2.1）
3. 本番の DB を読み取り専用で開き、較正の記録・``csv_exports`` の行・run の期間の
   ControlTick の trace を同じ read transaction で読む。manifest を行と全欄で照合し
   （0100 §2.6）、dataset の束縛を行から計算し直して照合する（0100 §2.8）。trace が保持期間で
   消えた期間・移行前の行を含む期間は拒否する（0112 §2.3 の 4）
4. 元の入力を一時の専用 DB へ dataset 用の再生で取り込み、本番の trace を ``seq`` ごと写し、
   同じ spec と宣言で builder を走らせ、作り直した公開物の bytes（manifest と
   ``examples.jsonl``）が dataset と完全に一致することを求める（0112 §2.3）

学習の CLI はまだ無い。CLI への配線は学習の CLI の PR で行う（0102 §2.3 と同じ扱い）。
較正ファイルとの照合（:func:`~coldaisle.calibration_log.verify_training_calibration`）は、
返した記録で呼び出し側が続けて行う。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from coldaisle.calibration_log import training_export_ids, verify_training_export_binding
from coldaisle.control.drift.model import DeclaredChange
from coldaisle.control.model.dataset import ThermalDatasetV2, examples_jsonl_bytes
from coldaisle.csv_export_manifest import replay_binding_of_records
from coldaisle.daemon import Daemon
from coldaisle.dataset import (
    ThermalDatasetV2Builder,
    check_production_traces,
    copy_or_match_production_traces,
)
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.normalize import Normalizer
from coldaisle.ingest.replay import ReplaySource, read_replay_export_inputs
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.calibration_history import CalibrationHistory
from coldaisle.store.export_binding import read_training_production

_SOURCE_NAME = "replay"
_DATABASE_NAME = "rebuild.db"


class TrainingDatasetRefused(ValueError):
    """学習に使う Dataset v2 が元の入力・本番の DB と照合できない。学習しない。"""


def published_bytes(dataset: ThermalDatasetV2) -> tuple[bytes, bytes]:
    """公開物の bytes（manifest と ``examples.jsonl``）。``write_dataset`` と同じ直列化。"""
    return (
        (dataset.manifest.model_dump_json(indent=2) + "\n").encode("utf-8"),
        examples_jsonl_bytes(dataset.examples),
    )


def verify_training_dataset_v2(
    dataset: ThermalDatasetV2,
    *,
    production_db: Path,
    replay_path: Path,
    declared_changes: tuple[DeclaredChange, ...],
    quality_rules: QualityRules,
) -> CalibrationHistory:
    """学習の入口の検査（決定記録 0112 §2.3）。通れば本番の DB の較正の記録を返す。

    ``declared_changes`` は dataset を作ったときと同じ宣言（dataset には書かれない。0109）。
    ``quality_rules`` は再生と builder が使う品質の設定。どれも既定値を持たない。
    外れれば :class:`TrainingDatasetRefused`（または照合の途中の ``ValueError``）。
    """
    if not isinstance(declared_changes, tuple):
        raise TypeError("declared_changes は DeclaredChange の tuple を明示して渡す")
    bindings = dataset.manifest.replay_bindings
    if bindings is None:
        raise TrainingDatasetRefused(
            "export の束縛（ReplayBindingV2）の無い Dataset v2 では学習しない（決定記録 0100 §2.8）"
        )
    if len(dataset.manifest.source_runs) != 1:
        raise TrainingDatasetRefused(
            "source run が1つの Dataset v2 だけを学習の入口で作り直せる（決定記録 0112 §2.3）"
        )
    run = dataset.manifest.source_runs[0]
    binding = bindings[0]

    # 0112 §2.1: 元の入力の fingerprint と manifest の束縛
    inputs = read_replay_export_inputs(replay_path)
    if inputs.source_sha256 != run.source_sha256:
        raise TrainingDatasetRefused(
            "--replay-path の fingerprint が SourceRun.source_sha256 と違う"
        )
    if inputs.records is None:
        raise TrainingDatasetRefused(
            "--replay-path に export の manifest が無い（決定記録 0112 §2.1）"
        )
    expected = (binding.local_timezone, binding.export_binding_sha256)
    if replay_binding_of_records(inputs.records) != expected:
        raise TrainingDatasetRefused(
            "--replay-path の manifest から計算した束縛が ReplayBindingV2 と一致しない"
        )

    # 0100 §2.6 / §2.8 と 0112 §2.3: 本番の DB を1つの read transaction で読む
    production = read_training_production(
        production_db, inputs.records, start_ms=run.start_ms, end_ms=run.end_ms
    )
    verify_training_export_binding(
        dataset,
        {export_id: production.rows.get(export_id) for export_id in training_export_ids(dataset)},
    )
    try:
        check_production_traces(production, start_ms=run.start_ms)
    except ValueError as error:
        raise TrainingDatasetRefused(str(error)) from error

    # 0112 §2.3: 一時の専用 DB で作り直して公開物の bytes を比べる
    with tempfile.TemporaryDirectory(prefix="coldaisle-training-") as directory:
        replay = ReplaySource(
            replay_path,
            tz=ZoneInfo(binding.local_timezone),
            timezone_explicit=False,
            bulk=True,
            dataset_provenance=True,
        )
        # 取り込んだ bytes そのものが dataset の束縛と一致すること
        # （読み直しの間の差し替えも見つける）
        if (
            replay.source_sha256 != run.source_sha256
            or (
                replay.local_timezone,
                replay.export_binding_sha256,
            )
            != expected
        ):
            raise TrainingDatasetRefused("再生した入力が dataset の束縛と一致しない")
        with SqliteStore(
            Path(directory) / _DATABASE_NAME, rules=quality_rules, clock=replay.clock
        ) as store:
            stats = Daemon(
                source=replay,
                store=store,
                normalizer=Normalizer(
                    rules=quality_rules, calibration=Calibration(), clock=replay.clock
                ),
                source_name=_SOURCE_NAME,
                dataset_run_alias=run.run_id,
            ).run()
            if stats.dataset_incomplete:
                raise TrainingDatasetRefused("元の入力を欠けなく再生できない")
            copy_or_match_production_traces(store, production.traces)
            rebuilt = ThermalDatasetV2Builder(store).build(
                source_run=run,
                spec=dataset.manifest.spec,
                declared_changes=declared_changes,
                calibration_history=production.history,
                export_binding=production.binding,
            )
    if published_bytes(rebuilt) != published_bytes(dataset):
        raise TrainingDatasetRefused(
            "元の入力と本番の trace から作り直した dataset が渡された dataset と一致しない"
            "（決定記録 0112 §2.3）"
        )
    return production.history
