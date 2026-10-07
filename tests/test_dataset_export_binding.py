"""Dataset v2 と学習の入口の export の照合（決定記録 0100 段 3。#237）。

- builder: ``--replay-path`` の manifest を本番の DB の ``csv_exports`` と全欄で照合し、専用 DB の
  ``dataset_source_run`` の束縛と一致しなければ拒否する。照合していない run（``NULL``）も拒否する。
  通れば ``ReplayBindingV2`` を書く（CSV の basename は書かない）
- ``csv_exports`` の照合に失敗したら、被覆と変更の検査へ進まない
- v1 の builder は ``NULL`` を受け入れ、渡された束縛とは一致を求める
- 学習の入口: 元の manifest と CSV が無くても、``csv_exports`` から計算し直して照合し、
  example の期間が束縛した日の区間に収まることを確かめる
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

import coldaisle.calibration_log as calibration_log
import coldaisle.dataset as dataset_module
from coldaisle.calibration_log import training_export_ids, verify_training_export_binding
from coldaisle.clock import SimulatedClock
from coldaisle.control.model.dataset import ReplayBindingV2, ReplayExportV2, ThermalDatasetV2
from coldaisle.csv_export_manifest import (
    ExportRecord,
    export_binding_sha256,
    export_record_sha256,
)
from coldaisle.dataset import ThermalDatasetBuilder, ThermalDatasetV2Builder, replay_binding_of
from coldaisle.ingest.replay import (
    ReplayBindingError,
    ReplaySource,
    read_replay_export_inputs,
    replay_sha256,
)
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from coldaisle.store.calibration_history import CalibrationHistoryError
from coldaisle.store.csv_export import day_bounds_ms, export_day
from coldaisle.store.export_binding import (
    ExportBinding,
    ExportBindingError,
    read_production_records,
    read_training_records,
)
from conftest import add_fixture_exports, fixture_export_binding, fixture_export_record
from test_dataset_cli_v2 import cli_env, log_lines, manifest_of, v2_args  # noqa: F401
from test_thermal_dataset import dataset_store  # noqa: F401
from test_thermal_dataset import source_run as v1_source_run
from test_thermal_dataset import spec as v1_spec
from test_thermal_dataset_v2 import (
    ALIGNED,
    COVERING_ROW_MS,
    FIXTURE_BINDING,
    NO_CHANGES,
    RUN_ALIAS,
    SHA256,
    T0,
    build,
    covering_history,
    history_db,
    run_store,
    source_run,
    spec_v2,
)

END_MS = 7_501
JST = ZoneInfo("Asia/Tokyo")


def other_record(**updates: object) -> ExportRecord:
    """仮の export の別の正当な行（翌日）。"""
    base = fixture_export_record().model_dump()
    base.update(
        export_id="export-" + "f" * 32,
        csv_name="sensors_1970-01-02.csv",
        day="1970-01-02",
        day_start_ms=86_400_000,
        day_end_ms=2 * 86_400_000,
    )
    base.update(updates)
    return ExportRecord.model_validate(base)


def dedicated_path(store: SqliteStore) -> str:
    return str(store.connection.execute("PRAGMA database_list").fetchone()[2])


# ---------------------------------------------------------------- builder


def test_builder_writes_the_replay_binding_without_csv_names(tmp_path, rules, clock):
    with run_store(tmp_path / "run.db", rules, clock, ALIGNED, end_ms=END_MS) as store:
        dataset = build(store, end_ms=END_MS)
    record = fixture_export_record()
    assert dataset.manifest.replay_bindings == (
        ReplayBindingV2(
            run_id=RUN_ALIAS,
            local_timezone="UTC",
            export_binding_sha256=FIXTURE_BINDING[1],
            exports=(
                ReplayExportV2(
                    export_id=record.export_id,
                    export_record_sha256=export_record_sha256(record),
                    day_start_ms=record.day_start_ms,
                    day_end_ms=record.day_end_ms,
                    csv_sha256=record.csv_sha256,
                    row_seconds_sha256=record.row_seconds_sha256,
                ),
            ),
        ),
    )
    text = dataset.manifest.model_dump_json()
    assert "sensors_" not in text and "csv_name" not in text, "CSV の basename を書かない"


def test_builder_refuses_an_unverified_run(tmp_path, rules, clock):
    """照合していない run（manifest の無い入力。NULL）から v2 は作らない（0100 §2.8）。"""
    with run_store(tmp_path / "run.db", rules, clock, ALIGNED, end_ms=END_MS) as store:
        pass
    # 同じ形の DB を NULL で bind し直す
    path = tmp_path / "null.db"
    with SqliteStore(path, rules=rules, clock=clock) as store:
        store.set_system_state("sys.ingest_source", "replay", at_ms=0)
        store.bind_dataset_source_run(
            run_alias=RUN_ALIAS,
            source_kind="replay",
            source_sha256=SHA256,
            at_ms=0,
            local_timezone=None,
            export_binding_sha256=None,
        )
        store.complete_dataset_source_run(at_ms=END_MS)
        with pytest.raises(ValueError, match="照合していない run"):
            build(store, end_ms=END_MS)


def test_builder_refuses_a_binding_that_differs_from_the_db(tmp_path, rules, clock):
    """専用 DB の束縛と、manifest から照合した束縛が違えば拒否する（0100 §2.8 の (c)）。"""
    history = Path(history_db(tmp_path / "history.db", COVERING_ROW_MS))
    add_fixture_exports(history, (fixture_export_record(), other_record()))
    _, other_binding = read_production_records(history, (other_record(),))
    with (
        run_store(tmp_path / "run.db", rules, clock, ALIGNED, end_ms=END_MS) as store,
        pytest.raises(ValueError, match="一致しない"),
    ):
        build(store, end_ms=END_MS, export_binding=other_binding)


def test_builder_requires_a_sealed_export_binding(tmp_path, rules, clock):
    with run_store(tmp_path / "run.db", rules, clock, ALIGNED, end_ms=END_MS) as store:
        builder = ThermalDatasetV2Builder(store)
        with pytest.raises(TypeError, match="export_binding"):
            builder.build(
                source_run=source_run(end_ms=END_MS),
                spec=spec_v2(),
                declared_changes=NO_CHANGES,
                calibration_history=covering_history(dedicated_path(store)),
                export_binding=FIXTURE_BINDING,  # type: ignore[arg-type]
            )
    with pytest.raises(TypeError):
        ExportBinding(object(), (fixture_export_record(),))


def test_v1_builder_accepts_null_and_checks_a_given_binding(dataset_store):  # noqa: F811
    """v1 は照合していない run（NULL）も受け入れ、渡された束縛とは一致を求める（0100 §2.8）。"""
    builder = ThermalDatasetBuilder(dataset_store)
    assert builder.build(source_run=v1_source_run(), spec=v1_spec(), replay_binding=(None, None))
    with pytest.raises(ValueError, match="一致しない"):
        builder.build(source_run=v1_source_run(), spec=v1_spec(), replay_binding=FIXTURE_BINDING)


# ---------------------------------------------------------------- csv_exports との照合（store）


def test_a_csv_from_db_a_with_records_of_db_b_is_refused(tmp_path):
    """#237 の (D): 別の DB を記録の DB に渡すと、その DB に行が無いので拒否する。"""
    db_a = Path(history_db(tmp_path / "a.db", COVERING_ROW_MS))
    db_b = Path(history_db(tmp_path / "b.db", COVERING_ROW_MS))
    add_fixture_exports(db_a)
    assert read_production_records(db_a, (fixture_export_record(),))[1]
    with pytest.raises(ExportBindingError, match="行が無い"):
        read_production_records(db_b, (fixture_export_record(),))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("csv_sha256", "c" * 64),
        ("timezone", "Asia/Tokyo"),
        ("day_end_ms", 86_400_001),
        ("timestamp_format", "%Y-%m-%d %H:%M:%S"),
        ("row_count", 1),
        ("row_seconds_sha256", "b" * 64),
    ],
)
def test_any_field_that_differs_from_the_row_is_refused(tmp_path, field, value):
    db = Path(history_db(tmp_path / "h.db", COVERING_ROW_MS))
    add_fixture_exports(db)
    manifest = ExportRecord.model_validate({**fixture_export_record().model_dump(), field: value})
    with pytest.raises(ExportBindingError, match=field):
        read_production_records(db, (manifest,))


def test_a_db_before_migration_0012_has_no_rows(tmp_path):
    """csv_exports の無い DB は「行が無い」として拒否する（0100 §2.7）。"""
    import shutil
    import sqlite3

    from coldaisle.store import migrations

    directory = tmp_path / "m11"
    directory.mkdir()
    for migration in migrations.discover():
        if migration.version <= 11:
            shutil.copy(migration.path, directory / migration.path.name)
    path = tmp_path / "v11.db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        migrations.apply_pending(conn, now_ms=0, directory=directory)
    finally:
        conn.close()
    with pytest.raises(ExportBindingError, match="行が無い"):
        read_production_records(path, (fixture_export_record(),))
    _, rows = read_training_records(path, ["export-" + "e" * 32])
    assert rows == {"export-" + "e" * 32: None}


def test_calibration_history_errors_are_not_hidden(tmp_path):
    with pytest.raises(CalibrationHistoryError):
        read_production_records(tmp_path / "missing.db", (fixture_export_record(),))


# ---------------------------------------------------------------- CLI


def test_v2_cli_refuses_inputs_without_manifests(cli_env, monkeypatch, capsys):  # noqa: F811
    from coldaisle.ingest.replay import ReplayExportInputs

    monkeypatch.setattr(
        dataset_module,
        "read_replay_export_inputs",
        lambda path: ReplayExportInputs(source_sha256=SHA256, records=None),
    )
    assert dataset_module.main(v2_args(cli_env)) == 1
    assert "manifest の無い入力" in capsys.readouterr().err


def test_v2_cli_checks_csv_exports_before_the_calibration_checks(
    cli_env,  # noqa: F811
    tmp_path,
    capsys,
):
    """csv_exports の照合に失敗したら、被覆と変更の検査へ進まない（0100 §2.6 の 3）。"""
    changed = history_db(tmp_path / "changed.db", COVERING_ROW_MS, T0 + 3_000)  # 期間の中の変更
    assert dataset_module.main(v2_args(cli_env, calibration_history_db=str(changed))) == 1
    lines = log_lines(capsys.readouterr().err)
    refused = [line for line in lines if line.get("level") == "error"]
    assert refused and "csv_exports" in str(refused[0]["msg"])
    assert not any("記録の行" in str(line.get("reason")) for line in lines)


def test_v2_cli_writes_the_binding(cli_env):  # noqa: F811
    assert dataset_module.main(v2_args(cli_env)) == 0
    manifest = manifest_of(cli_env)
    assert manifest["replay_bindings"][0]["export_binding_sha256"] == FIXTURE_BINDING[1]


# ------------------------------------------------------- 再生の入力（段 1・2 とのつながり）


def exported(tmp_path: Path, rules) -> tuple[Path, Path]:
    prod = tmp_path / "prod.db"
    out = tmp_path / "csv"
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        for day in (date(2026, 8, 24), date(2026, 8, 25)):
            start_ms, _ = day_bounds_ms(day, JST)
            store.insert_sample(
                Sample(
                    ts_ms=start_ms + 43_200_000,
                    readings=(Reading(metric="air.room", value=20.0, quality=Quality.OK),),
                )
            )
            export_day(store, day, tz=JST, out_dir=out, lock_timeout_s=5.0)
    return prod, out


def test_replay_inputs_bind_the_same_records_as_the_dataset_replay(tmp_path, rules):
    prod, out = exported(tmp_path, rules)
    inputs = read_replay_export_inputs(out)
    assert inputs.source_sha256 == replay_sha256(out)
    assert inputs.records is not None and len(inputs.records) == 2
    replay = ReplaySource(out, tz=JST, timezone_explicit=False, dataset_provenance=True)
    assert replay_binding_of(inputs) == (replay.local_timezone, replay.export_binding_sha256)
    assert inputs.source_sha256 == replay.source_sha256
    _, binding = read_production_records(prod, inputs.records)
    assert binding.export_binding_sha256 == replay.export_binding_sha256
    assert binding.timezone == "Asia/Tokyo"


def test_replay_inputs_refuse_tampered_or_partial_inputs(tmp_path, rules):
    _, out = exported(tmp_path, rules)
    csv_path = out / "sensors_2026-08-24.csv"
    original = csv_path.read_bytes()
    csv_path.write_bytes(original.replace(b"20.0", b"21.0"))
    with pytest.raises(ReplayBindingError) as caught:
        read_replay_export_inputs(out)
    assert caught.value.check == "csv_sha256"
    csv_path.write_bytes(original)
    (out / "sensors_2026-08-25.export.json").unlink()
    with pytest.raises(ReplayBindingError) as caught:
        read_replay_export_inputs(out)
    assert caught.value.check == "manifest_partial"
    (out / "sensors_2026-08-24.export.json").unlink()
    inputs = read_replay_export_inputs(out)
    assert inputs.records is None
    assert replay_binding_of(inputs) == (None, None)


# ---------------------------------------------------------------- ReplayBindingV2 の形


def binding_dict() -> dict[str, object]:
    record = fixture_export_record()
    return {
        "run_id": RUN_ALIAS,
        "local_timezone": "UTC",
        "export_binding_sha256": FIXTURE_BINDING[1],
        "exports": [
            {
                "export_id": record.export_id,
                "export_record_sha256": export_record_sha256(record),
                "day_start_ms": 0,
                "day_end_ms": 86_400_000,
                "csv_sha256": record.csv_sha256,
                "row_seconds_sha256": record.row_seconds_sha256,
            }
        ],
    }


def test_replay_binding_digest_must_match_its_exports():
    ReplayBindingV2.model_validate_json(json.dumps(binding_dict()))
    tampered = binding_dict()
    tampered["export_binding_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="export_binding_sha256"):
        ReplayBindingV2.model_validate_json(json.dumps(tampered))
    duplicated = binding_dict()
    duplicated["exports"] = duplicated["exports"] * 2  # type: ignore[operator]
    with pytest.raises(ValidationError, match="重複"):
        ReplayBindingV2.model_validate_json(json.dumps(duplicated))
    with pytest.raises(ValidationError):
        ReplayBindingV2.model_validate_json(
            json.dumps({**binding_dict(), "csv_name": "sensors_1970-01-01.csv"})
        )


# ---------------------------------------------------------------- 学習の入口（0100 §2.8）


@pytest.fixture
def built(tmp_path, rules, clock) -> tuple[ThermalDatasetV2, Path]:
    with run_store(tmp_path / "run.db", rules, clock, ALIGNED, end_ms=END_MS) as store:
        dataset = build(store, end_ms=END_MS)
        history = Path(dedicated_path(store)).with_name("run-history.db")
    return dataset, history


@pytest.fixture
def rows_only(monkeypatch):
    """元の入力の照合（0112 §2.1。下の節で試す）を外し、``csv_exports`` の行の検査だけを試す。"""
    monkeypatch.setattr(calibration_log, "_verify_replay_input", lambda *args: None)

    def verify(dataset: ThermalDatasetV2, rows) -> None:
        verify_training_export_binding(
            dataset,
            rows,
            replay_paths={run.run_id: Path("unused") for run in dataset.manifest.source_runs},
        )

    return verify


def rows_of(history: Path, dataset: ThermalDatasetV2):
    return read_training_records(history, training_export_ids(dataset))[1]


def test_training_entry_accepts_the_built_dataset_rows(built, rows_only):
    dataset, history = built
    rows_only(dataset, rows_of(history, dataset))


def test_training_entry_refuses_a_dataset_without_a_binding(built, rows_only):
    dataset, history = built
    bare = dataset.model_copy(
        update={"manifest": dataset.manifest.model_copy(update={"replay_bindings": None})}
    )
    with pytest.raises(ValueError, match="ReplayBindingV2"):
        rows_only(bare, rows_of(history, dataset))


def test_training_entry_refuses_a_missing_row(built, tmp_path, rows_only):
    dataset, _ = built
    other = Path(history_db(tmp_path / "other.db", COVERING_ROW_MS))
    with pytest.raises(ValueError, match="行が無い"):
        rows_only(dataset, rows_of(other, dataset))


def test_training_entry_refuses_a_row_with_another_record(built, tmp_path, rows_only):
    """同じ export_id でも、行の中身が違えば export_record_sha256 が合わない。"""
    dataset, _ = built
    other = Path(history_db(tmp_path / "other.db", COVERING_ROW_MS))
    add_fixture_exports(
        other, (fixture_export_record().model_copy(update={"csv_sha256": "c" * 64}),)
    )
    with pytest.raises(ValueError, match="export_record_sha256"):
        rows_only(dataset, rows_of(other, dataset))


def rebound(dataset: ThermalDatasetV2, records: tuple[ExportRecord, ...]) -> ThermalDatasetV2:
    """別の正当な export の ID と digest を名乗る dataset（手で組んだもの）。"""
    raw = json.loads(dataset.model_dump_json())
    raw["manifest"]["replay_bindings"] = [
        {
            "run_id": RUN_ALIAS,
            "local_timezone": "UTC",
            "export_binding_sha256": export_binding_sha256(records),
            "exports": [
                {
                    "export_id": record.export_id,
                    "export_record_sha256": export_record_sha256(record),
                    "day_start_ms": record.day_start_ms,
                    "day_end_ms": record.day_end_ms,
                    "csv_sha256": record.csv_sha256,
                    "row_seconds_sha256": record.row_seconds_sha256,
                }
                for record in sorted(records, key=lambda r: r.export_id)
            ],
        }
    ]
    return ThermalDatasetV2.model_validate_json(json.dumps(raw))


def test_training_entry_refuses_ids_borrowed_from_another_day(built, rows_only):
    """関係の無い正当な export の ID だけを借りた dataset は、期間の検査で拒否する。"""
    dataset, history = built
    add_fixture_exports(history, (other_record(),))
    borrowed = rebound(dataset, (other_record(),))
    with pytest.raises(ValueError, match="日の区間"):
        rows_only(borrowed, rows_of(history, borrowed))


def test_training_entry_accepts_adjacent_days_that_cover_the_period(built, rows_only):
    dataset, history = built
    add_fixture_exports(history, (other_record(),))
    both = rebound(dataset, (fixture_export_record(), other_record()))
    rows_only(both, rows_of(history, both))


def test_training_entry_refuses_listed_fields_that_differ_from_the_row(built, rows_only):
    """digest は合っても、写した欄（日の区間など）が行と違えば拒否する。"""
    dataset, history = built
    raw = json.loads(dataset.model_dump_json())
    raw["manifest"]["replay_bindings"][0]["exports"][0]["day_end_ms"] = 86_400_001
    tampered = ThermalDatasetV2.model_validate_json(json.dumps(raw))
    with pytest.raises(ValueError, match="欄が csv_exports の行と違う"):
        rows_only(tampered, rows_of(history, dataset))


def test_training_entry_uses_the_same_read_as_the_calibration_history(built):
    dataset, history = built
    calibration, rows = read_training_records(history, training_export_ids(dataset))
    assert calibration.rows, "較正の記録も同じ呼び出しで読む"
    assert set(rows) == {fixture_export_record().export_id}


def test_fixture_binding_matches_its_digest():
    """試験の前提: 仮の export の束縛は PR #256 で固定した形の digest。"""
    record = fixture_export_record()
    payload = f'[["{record.export_id}","{export_record_sha256(record)}"]]\n'.encode()
    assert ("UTC", hashlib.sha256(payload).hexdigest()) == FIXTURE_BINDING
    assert fixture_export_binding  # conftest の補助が import できる


def test_training_entry_joins_adjacent_day_spans(built, rows_only):
    """example の期間が2つの export の境目をまたいでも、つながった区間に収まれば通る。"""
    dataset, history = built
    middle = T0 + 3_000  # ALIGNED の唯一の example の期間 [T0, T0 + 7000] の中
    first = fixture_export_record().model_copy(
        update={"export_id": "export-" + "1" * 32, "day_end_ms": middle}
    )
    second = other_record(export_id="export-" + "2" * 32, day_start_ms=middle)
    add_fixture_exports(history, (first, second))
    split = rebound(dataset, (first, second))
    rows_only(split, rows_of(history, split))
    gap = other_record(export_id="export-" + "3" * 32, day_start_ms=middle + 1)
    add_fixture_exports(history, (gap,))
    holed = rebound(dataset, (first, gap))
    with pytest.raises(ValueError, match="日の区間"):
        rows_only(holed, rows_of(history, holed))


def test_each_source_run_has_exactly_one_binding(built):
    dataset, _ = built
    raw = json.loads(dataset.model_dump_json())
    raw["manifest"]["replay_bindings"][0]["run_id"] = "run-" + "9" * 32
    with pytest.raises(ValidationError, match="source run ごと"):
        ThermalDatasetV2.model_validate_json(json.dumps(raw))
    raw = json.loads(dataset.model_dump_json())
    raw["manifest"]["replay_bindings"] *= 2
    with pytest.raises(ValidationError, match="source run ごと"):
        ThermalDatasetV2.model_validate_json(json.dumps(raw))


# ---------------------------------------------------------- 学習の入口の元の入力（0112 §2.1）

UTC_DAY = date(1970, 1, 1)


def export_utc_day(prod: Path, out: Path, rules, value: float) -> ExportRecord:
    """試験の専用 DB と同じ 1970-01-01（UTC）を、本番の DB から実際に export する。"""
    with SqliteStore(prod, rules=rules, clock=SimulatedClock(10**13)) as store:
        store.insert_sample(
            Sample(
                ts_ms=T0 + 1_000,
                readings=(Reading(metric="air.room", value=value, quality=Quality.OK),),
            )
        )
        export_day(store, UTC_DAY, tz=ZoneInfo("UTC"), out_dir=out, lock_timeout_s=5.0)
    return ExportRecord.from_manifest_bytes((out / "sensors_1970-01-01.export.json").read_bytes())


def claiming(
    dataset: ThermalDatasetV2, record: ExportRecord, source_sha256: str
) -> ThermalDatasetV2:
    """``record`` の束縛と ``source_sha256`` を名乗る dataset（builder を通さずに組んだもの）。"""
    raw = json.loads(rebound(dataset, (record,)).model_dump_json())
    raw["manifest"]["source_runs"][0]["source_sha256"] = source_sha256
    return ThermalDatasetV2.model_validate_json(json.dumps(raw))


@pytest.fixture
def real_export(tmp_path, rules, built):
    dataset, _ = built
    prod, out = tmp_path / "prod.db", tmp_path / "csv"
    record = export_utc_day(prod, out, rules, 20.0)
    return dataset, prod, out, record


def verify_with(dataset: ThermalDatasetV2, prod: Path, replay_paths: dict[str, Path]) -> None:
    verify_training_export_binding(
        dataset,
        read_training_records(prod, training_export_ids(dataset))[1],
        replay_paths=replay_paths,
    )


def test_training_entry_accepts_the_original_input(real_export):
    dataset, prod, out, record = real_export
    honest = claiming(dataset, record, replay_sha256(out))
    verify_with(honest, prod, {RUN_ALIAS: out})


def test_training_entry_requires_a_replay_path_for_every_run(real_export, tmp_path):
    dataset, prod, out, record = real_export
    honest = claiming(dataset, record, replay_sha256(out))
    with pytest.raises(ValueError, match="過不足なく"):
        verify_with(honest, prod, {})
    with pytest.raises(ValueError, match="過不足なく"):
        verify_with(honest, prod, {RUN_ALIAS: out, "run-" + "9" * 32: out})
    with pytest.raises(TypeError):
        verify_training_export_binding(honest, {})  # type: ignore[call-arg]


def test_training_entry_refuses_a_fingerprint_that_differs(real_export):
    dataset, prod, out, record = real_export
    with pytest.raises(ValueError, match="fingerprint"):
        verify_with(claiming(dataset, record, "0" * 64), prod, {RUN_ALIAS: out})


def test_training_entry_refuses_an_input_without_manifests(real_export):
    dataset, prod, out, record = real_export
    (out / "sensors_1970-01-01.export.json").unlink()
    without = claiming(dataset, record, replay_sha256(out))
    with pytest.raises(ValueError, match="manifest が無い"):
        verify_with(without, prod, {RUN_ALIAS: out})


def test_training_entry_refuses_a_rewritten_csv(real_export):
    dataset, prod, out, record = real_export
    honest = claiming(dataset, record, replay_sha256(out))
    csv_path = out / "sensors_1970-01-01.csv"
    csv_path.write_bytes(csv_path.read_bytes().replace(b"20.0", b"25.0"))
    with pytest.raises(ValueError, match="csv_sha256"):
        verify_with(honest, prod, {RUN_ALIAS: out})


def test_training_entry_refuses_another_legitimate_export_of_the_same_day(real_export, rules):
    """#237 の Codex P1: 同じ日を覆う別の正当な export B の束縛を写した dataset。

    A と B はどちらも本番の DB の csv_exports に行があり、digest も期間も合う。--replay-path の
    元の入力（B の CSV と manifest）と照合して初めて食い違いが分かる。
    """
    dataset, prod, out, record_a = real_export
    record_b = export_utc_day(prod, out, rules, 30.0)  # 同じ日の書き直し（別の export_id）
    assert record_a.export_id != record_b.export_id and record_a.day == record_b.day
    # 行と digest と期間の検査だけなら通る（0100 §2.8 の残っていた穴）
    forged = claiming(dataset, record_a, replay_sha256(out))
    rows = read_training_records(prod, training_export_ids(forged))[1]
    assert rows[record_a.export_id] == record_a
    with pytest.raises(ValueError, match="ReplayBindingV2 と一致しない"):
        verify_with(forged, prod, {RUN_ALIAS: out})
    verify_with(claiming(dataset, record_b, replay_sha256(out)), prod, {RUN_ALIAS: out})
