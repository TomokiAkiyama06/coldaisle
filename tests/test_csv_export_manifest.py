"""日次 CSV の export manifest と ``csv_exports``（決定記録 0100 段 1。#237）。

- manifest の ``csv_sha256`` / ``row_count`` / ``row_seconds_sha256`` が書いた CSV と一致し、
  ``csv_exports`` の行が manifest と同じ内容を持つ（0100 §2.11）
- CSV の bytes は 0100 の前と同じ（0008 §2.8）
- ``csv_exports`` は追記のみ（trigger）。保持期間の削除で消えない。bind 済みの DB では export しない
- 同じ日を書き直すと、古い manifest は新しい CSV と対にならず、行は2つ残る
- 各段階で落ちても、残るのは「manifest の無い CSV」か「対の無い行」（安全側）
- 同じ日の export は日ごとの lock で直列になる。上限内に取れなければ何も書かない
- ``export_record_sha256`` は manifest と行から同じ値（golden vector）
- 写像（0100 §2.4）: export → 再生の往復で秒が一致する
- migration 0012 は追記のみで冪等
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import os
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coldaisle.channels import METRIC_TO_CHANNEL, SAMPLE_CHANNELS
from coldaisle.clock import SimulatedClock
from coldaisle.csv_export_manifest import (
    SCHEMA,
    SCHEMA_VERSION,
    TIMESTAMP_FORMAT,
    ExportRecord,
    export_record_sha256,
    export_second,
    format_local,
    lock_name_for,
    manifest_name_for,
    parse_local,
    row_seconds_sha256,
)
from coldaisle.store import Quality, Reading, Sample, SqliteStore, migrations
from coldaisle.store import csv_export as csv_export_module
from coldaisle.store.csv_export import (
    CsvExportError,
    CsvExportLockTimeout,
    day_bounds_ms,
    export_day,
    manifest_path_for,
    read_csv_export,
)
from coldaisle.store.rollup import RetentionRules, run

JST = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 8, 25)
NOON_JST_MS = 1_787_626_800_000  # 2026-08-25T12:00:00+09:00
LOCK_S = 5.0
RUN_ALIAS = "run-" + "a" * 32


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "prod.db"


@pytest.fixture
def store(db_path, rules):
    with SqliteStore(db_path, rules=rules, clock=SimulatedClock(NOON_JST_MS + 86_400_000)) as s:
        yield s


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "csv"


def write(store: SqliteStore, ts_ms: int, **channels: float | None) -> None:
    store.insert_sample(
        Sample(
            ts_ms=ts_ms,
            readings=tuple(
                Reading(
                    metric=metric,
                    value=value,
                    quality=Quality.OK if value is not None else Quality.MISSING,
                )
                for metric, value in channels.items()
            ),
        )
    )


def fill(store: SqliteStore) -> list[int]:
    """1日の中に、秒の端数を持つ時刻を含む数行を書く。書いた ts_ms を返す。"""
    stamps = [NOON_JST_MS + offset for offset in (0, 999, 2_500, 5_001, 3_600_000 + 7)]
    for index, ts_ms in enumerate(stamps):
        write(store, ts_ms, **{"air.room": 20.0 + index, "air.front_intake": 21.5 + index})
    # 前日と翌日の行は入らない
    start_ms, end_ms = day_bounds_ms(DAY, JST)
    write(store, start_ms - 1, **{"air.room": 1.0})
    write(store, end_ms, **{"air.room": 2.0})
    return stamps


def export(store: SqliteStore, out_dir: Path, *, tz: ZoneInfo = JST) -> Path:
    return export_day(store, DAY, tz=tz, out_dir=out_dir, lock_timeout_s=LOCK_S)


def manifest_of(csv_path: Path) -> ExportRecord:
    return ExportRecord.from_manifest_bytes(manifest_path_for(csv_path).read_bytes())


def rows(store: SqliteStore) -> list[tuple[object, ...]]:
    return [
        tuple(row) for row in store.connection.execute("SELECT * FROM csv_exports ORDER BY rowid")
    ]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def legacy_export_bytes(store: SqliteStore, day: date, tz: ZoneInfo) -> bytes:
    """0100 の前の ``export_day`` と同じ手順で作った CSV の bytes（0008 §2.8 の形の基準）。"""
    start_ms, end_ms = day_bounds_ms(day, tz)
    found = store.connection.execute(
        "SELECT ts_ms, metric, value, quality FROM readings "
        "WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms",
        (start_ms, end_ms),
    ).fetchall()
    pivoted: dict[int, dict[str, float | None]] = {}
    for row in found:
        channel = METRIC_TO_CHANNEL.get(row["metric"])
        if channel is None:
            continue
        value = row["value"] if row["quality"] == Quality.OK.value else None
        pivoted.setdefault(int(row["ts_ms"]), {})[channel] = value
    path = Path(store.connection.execute("PRAGMA database_list").fetchone()[2]).parent / "old.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", *SAMPLE_CHANNELS])
        for ts_ms in sorted(pivoted):
            stamp = datetime.fromtimestamp(ts_ms / 1000, tz=tz).strftime("%Y-%m-%dT%H:%M:%S")
            values = pivoted[ts_ms]
            writer.writerow(
                [stamp, *("" if values.get(c) is None else values[c] for c in SAMPLE_CHANNELS)]
            )
    return path.read_bytes()


# ---------------------------------------------------------------- manifest と行の内容


def test_manifest_matches_the_csv_it_sits_beside(store, out_dir):
    stamps = fill(store)
    csv_path = export(store, out_dir)
    record = manifest_of(csv_path)

    assert manifest_path_for(csv_path).name == "sensors_2026-08-25.export.json"
    assert record.csv_name == csv_path.name == "sensors_2026-08-25.csv"
    assert record.csv_sha256 == sha256(csv_path.read_bytes())
    assert record.day == "2026-08-25"
    assert record.timezone == "Asia/Tokyo"
    assert (record.day_start_ms, record.day_end_ms) == day_bounds_ms(DAY, JST)
    assert record.timestamp_format == TIMESTAMP_FORMAT
    data_rows = list(csv.reader(csv_path.open(encoding="utf-8", newline="")))[1:]
    assert record.row_count == len(data_rows) == len(stamps)
    # 秒の列は、書いた ts_ms の切り捨てから独立に作る（export の実装を通さない）
    expected = hashlib.sha256("".join(f"{ts // 1000}\n" for ts in stamps).encode()).hexdigest()
    assert record.row_seconds_sha256 == expected
    # 書いた文字列を再生側の写像で読むと、同じ秒の列になる（0100 §2.4）
    assert [parse_local(row[0], JST) for row in data_rows] == [ts // 1000 for ts in stamps]


def test_the_db_row_holds_the_same_record_as_the_manifest(store, out_dir):
    fill(store)
    csv_path = export(store, out_dir)
    record = manifest_of(csv_path)

    from_db = read_csv_export(store.connection, record.export_id)
    assert from_db == record
    assert from_db is not None
    assert export_record_sha256(from_db) == export_record_sha256(record)
    exported_ms = store.connection.execute(
        "SELECT exported_ms FROM csv_exports WHERE export_id = ?", (record.export_id,)
    ).fetchone()[0]
    assert exported_ms == store.clock.now_ms(), "行の時刻は store の時計"


def test_manifest_bytes_are_canonical_and_carry_the_schema(store, out_dir):
    fill(store)
    csv_path = export(store, out_dir)
    raw = manifest_path_for(csv_path).read_bytes()
    record = ExportRecord.from_manifest_bytes(raw)
    assert raw == record.manifest_bytes()
    assert raw.endswith(b"}\n")
    assert f'"schema":"{SCHEMA}"'.encode() in raw
    assert f'"schema_version":{SCHEMA_VERSION}'.encode() in raw


def test_csv_bytes_are_unchanged_from_before_the_manifest(store, out_dir):
    """0008 §2.8 の形を保つ。0100 の前の書き方と bytes で一致する。"""
    fill(store)
    store.insert_sample(
        Sample(
            ts_ms=NOON_JST_MS + 9_000,
            readings=(Reading(metric="air.rear_exhaust", value=-127.0, quality=Quality.SUSPECT),),
        )
    )
    csv_path = export(store, out_dir)
    assert csv_path.read_bytes() == legacy_export_bytes(store, DAY, JST)


def test_empty_day_has_a_manifest_with_zero_rows(store, out_dir):
    csv_path = export(store, out_dir)
    record = manifest_of(csv_path)
    assert record.row_count == 0
    assert record.row_seconds_sha256 == sha256(b"")


def test_manifest_and_row_hold_no_paths_or_host_names(store, out_dir, db_path, tmp_path):
    """識別子を書かない（AGENTS.md ルール 10 / 0021）。``csv_name`` は basename だけ。"""
    fill(store)
    csv_path = export(store, out_dir)
    raw = manifest_path_for(csv_path).read_text(encoding="utf-8")
    row_text = repr(rows(store))
    for text in (raw, row_text):
        assert str(tmp_path) not in text
        assert str(out_dir) not in text
        assert str(db_path) not in text
        assert db_path.name not in text
        assert "/" not in text.replace("Asia/Tokyo", "")
        assert socket.gethostname() not in text


def test_manifest_timezone_is_the_configured_string_without_normalising(store, out_dir):
    """同じオフセットの別の名前（``Etc/GMT-9``）を ``Asia/Tokyo`` に直さない（0100 §2.2）。"""
    fill(store)
    tokyo = export(store, out_dir).read_bytes()
    csv_path = export(store, out_dir, tz=ZoneInfo("Etc/GMT-9"))
    assert csv_path.read_bytes() == tokyo
    assert manifest_of(csv_path).timezone == "Etc/GMT-9"


def test_replay_glob_does_not_pick_up_the_manifest_or_the_lock(store, out_dir):
    fill(store)
    export(store, out_dir)
    assert sorted(path.name for path in out_dir.glob("sensors_*.csv")) == ["sensors_2026-08-25.csv"]
    assert (out_dir / lock_name_for(DAY)).exists()
    assert not [p for p in out_dir.iterdir() if p.name.endswith(".tmp")], "一時ファイルを残さない"


# ---------------------------------------------------------------- golden vector


GOLDEN = ExportRecord(
    export_id="export-" + "0123456789abcdef" * 2,
    csv_name="sensors_2026-08-24.csv",
    csv_sha256="a" * 64,
    day="2026-08-24",
    timezone="Asia/Tokyo",
    day_start_ms=1_787_497_200_000,
    day_end_ms=1_787_583_600_000,
    timestamp_format="%Y-%m-%dT%H:%M:%S",
    row_count=2,
    row_seconds_sha256=row_seconds_sha256([1_787_497_200, 1_787_497_202]),
)


def test_golden_vectors():
    """型と bytes を固定する（0100 §2.1「実装の PR で型と golden vector を固定する」）。"""
    assert GOLDEN.row_seconds_sha256 == sha256(b"1787497200\n1787497202\n")
    assert GOLDEN.manifest_bytes() == (
        b'{"csv_name":"sensors_2026-08-24.csv","csv_sha256":"' + b"a" * 64 + b'",'
        b'"day":"2026-08-24","day_end_ms":1787583600000,"day_start_ms":1787497200000,'
        b'"export_id":"export-0123456789abcdef0123456789abcdef","row_count":2,'
        b'"row_seconds_sha256":"' + GOLDEN.row_seconds_sha256.encode() + b'",'
        b'"schema":"coldaisle.daily_csv_export","schema_version":1,'
        b'"timestamp_format":"%Y-%m-%dT%H:%M:%S","timezone":"Asia/Tokyo"}\n'
    )
    canonical = (
        b'{"csv_name":"sensors_2026-08-24.csv","csv_sha256":"' + b"a" * 64 + b'",'
        b'"day":"2026-08-24","day_end_ms":1787583600000,"day_start_ms":1787497200000,'
        b'"export_id":"export-0123456789abcdef0123456789abcdef","row_count":2,'
        b'"row_seconds_sha256":"' + GOLDEN.row_seconds_sha256.encode() + b'",'
        b'"schema_version":1,'
        b'"timestamp_format":"%Y-%m-%dT%H:%M:%S","timezone":"Asia/Tokyo"}\n'
    )
    assert export_record_sha256(GOLDEN) == sha256(canonical)
    assert GOLDEN.record_sha256() == export_record_sha256(GOLDEN)


def test_golden_record_round_trips_through_the_table(store):
    """表の行から計算しても同じ ``export_record_sha256``（0100 §2.8）。"""
    with store.transaction():
        store.connection.execute(
            "INSERT INTO csv_exports (export_id, csv_name, csv_sha256, day, timezone, "
            "day_start_ms, day_end_ms, timestamp_format, row_count, row_seconds_sha256, "
            "exported_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*GOLDEN.model_dump().values(), 1),
        )
    from_db = read_csv_export(store.connection, GOLDEN.export_id)
    assert from_db is not None
    assert export_record_sha256(from_db) == export_record_sha256(GOLDEN)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("export_id", "export-" + "A" * 32),
        ("csv_name", "../sensors_2026-08-24.csv"),
        ("csv_name", "sensors_2026-08-25.csv"),
        ("csv_sha256", "z" * 64),
        ("timezone", ""),
        ("day_start_ms", 1_787_583_600_000),
        ("row_count", -1),
        ("row_count", True),
    ],
)
def test_manifest_fields_are_validated(field, value):
    with pytest.raises(ValueError):
        ExportRecord.model_validate({**GOLDEN.model_dump(), field: value})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.update(schema="other"),
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=True),
        lambda d: d.pop("schema"),
    ],
)
def test_manifest_with_an_unknown_shape_is_rejected(mutate):
    import json

    document = json.loads(GOLDEN.manifest_bytes())
    mutate(document)
    with pytest.raises(ValueError):
        ExportRecord.from_manifest_bytes(json.dumps(document).encode())


# ---------------------------------------------------------------- 写像（0100 §2.4）


@pytest.mark.parametrize("ts_ms", [0, 999, 1_000, NOON_JST_MS + 1, NOON_JST_MS + 59_999])
def test_export_and_replay_share_the_mapping(ts_ms):
    second = export_second(ts_ms)
    assert second == ts_ms // 1000
    text = format_local(second, JST)
    assert text == datetime.fromtimestamp(ts_ms / 1000, tz=JST).strftime(TIMESTAMP_FORMAT)
    assert parse_local(text, JST) == second


def test_every_second_of_a_tokyo_day_round_trips():
    start_ms, end_ms = day_bounds_ms(DAY, JST)
    for second in range(start_ms // 1000, end_ms // 1000, 997):
        assert parse_local(format_local(second, JST), JST) == second


def test_parse_local_refuses_a_text_with_an_offset():
    with pytest.raises(ValueError, match="オフセット"):
        parse_local("2026-08-25T12:00:00+09:00", JST)


# ---------------------------------------------------------------- 書き直し


def test_rewriting_a_day_pairs_the_new_csv_with_the_new_manifest(store, out_dir):
    fill(store)
    first = manifest_of(export(store, out_dir))
    write(store, NOON_JST_MS + 7_200_000, **{"air.room": 30.0})
    csv_path = export(store, out_dir)
    second = manifest_of(csv_path)

    assert second.export_id != first.export_id
    assert second.csv_sha256 == sha256(csv_path.read_bytes())
    assert second.csv_sha256 != first.csv_sha256
    assert second.row_count == first.row_count + 1
    ids = [row[0] for row in rows(store)]
    assert ids == [first.export_id, second.export_id], "古い行は消さない（追記のみ）"


# ---------------------------------------------------------------- 追記のみ・保持期間・bind 済み


def test_update_and_delete_are_rejected(store, out_dir):
    fill(store)
    export(store, out_dir)
    before = rows(store)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.connection.execute("UPDATE csv_exports SET row_count = 0")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.connection.execute("DELETE FROM csv_exports")
    assert rows(store) == before


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("export_id", "export-" + "g" * 32),
        ("export_id", "export-" + "a" * 31),
        ("csv_name", "other.csv"),
        ("csv_sha256", "A" * 64),
        ("day", "2026-8-24"),
        ("timezone", ""),
        ("day_end_ms", 1_787_497_200_000),
        ("row_count", -1),
        ("row_count", 1.5),
        ("row_seconds_sha256", "0" * 63),
        ("exported_ms", -1),
    ],
)
def test_check_constraints(store, column, value):
    values = {**GOLDEN.model_dump(), "exported_ms": 1, column: value}
    columns = ", ".join(values)
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            f"INSERT INTO csv_exports ({columns}) VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )


def test_retention_does_not_delete_the_rows(store, out_dir, rules):
    fill(store)
    export(store, out_dir)
    before = rows(store)
    retention = RetentionRules(raw_days=1, control_trace_days=1, csv_dir=str(out_dir))
    result = run(store, retention, now_ms=NOON_JST_MS + 400 * 86_400_000)
    assert result.deleted_rows > 0, "生データは消えている"
    assert rows(store) == before


def test_a_bound_dataset_db_is_refused_and_nothing_is_written(tmp_path, rules, out_dir):
    with SqliteStore(tmp_path / "dataset.db", rules=rules, clock=SimulatedClock(0)) as store:
        store.set_system_state("sys.ingest_source", "replay", at_ms=0)
        store.bind_dataset_source_run(
            run_alias=RUN_ALIAS, source_kind="replay", source_sha256="b" * 64, at_ms=0
        )
        with pytest.raises(CsvExportError, match="dataset専用DB"):
            export(store, out_dir)
        assert rows(store) == []
    assert not out_dir.exists() or not any(out_dir.iterdir())


# ---------------------------------------------------------------- 落ちたときに残るもの


def assert_safe(out_dir: Path, store: SqliteStore) -> None:
    """残っている manifest は、必ず横の CSV の bytes と DB の行に対になる。"""
    manifest_path = out_dir / manifest_name_for(DAY)
    if manifest_path.exists():
        record = ExportRecord.from_manifest_bytes(manifest_path.read_bytes())
        assert record.csv_sha256 == sha256((out_dir / record.csv_name).read_bytes())
        assert read_csv_export(store.connection, record.export_id) == record
    assert not [p for p in out_dir.iterdir() if p.name.endswith(".tmp")]


class Boom(RuntimeError):
    pass


@pytest.mark.parametrize("previous", [False, True], ids=["first", "rewrite"])
def test_failure_before_the_commit_writes_nothing(store, out_dir, monkeypatch, previous):
    fill(store)
    if previous:
        export(store, out_dir)
    before_rows = rows(store)
    before_files = {p.name: p.read_bytes() for p in out_dir.iterdir()} if previous else {}

    def fail(*_: object) -> None:
        raise Boom

    monkeypatch.setattr(csv_export_module, "_append_record", fail)
    write(store, NOON_JST_MS + 7_200_000, **{"air.room": 30.0})
    with pytest.raises(Boom):
        export(store, out_dir)
    assert rows(store) == before_rows
    after_files = {p.name: p.read_bytes() for p in out_dir.iterdir()}
    after_files.pop(lock_name_for(DAY), None)
    before_files.pop(lock_name_for(DAY), None)
    assert after_files == before_files
    assert_safe(out_dir, store)


def test_failure_while_writing_temporaries_writes_nothing(store, out_dir, monkeypatch):
    fill(store)

    def fail(_: int) -> None:
        raise Boom

    monkeypatch.setattr(csv_export_module.os, "fsync", fail)
    with pytest.raises(Boom):
        export(store, out_dir)
    assert rows(store) == []
    assert [p.name for p in out_dir.iterdir()] == [lock_name_for(DAY)]


@pytest.mark.parametrize("fail_at", [1, 2], ids=["csv-rename", "manifest-rename"])
@pytest.mark.parametrize("previous", [False, True], ids=["first", "rewrite"])
def test_failure_during_the_renames_leaves_only_safe_states(
    store, out_dir, monkeypatch, fail_at, previous
):
    fill(store)
    if previous:
        export(store, out_dir)
    write(store, NOON_JST_MS + 7_200_000, **{"air.room": 30.0})
    real_replace = os.replace
    calls = {"n": 0}

    def replace(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] == fail_at:
            raise Boom
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(csv_export_module.os, "replace", replace)
    with pytest.raises(Boom):
        export(store, out_dir)

    manifest_path = out_dir / manifest_name_for(DAY)
    assert not manifest_path.exists(), "落ちた export の後に manifest は残らない"
    assert len(rows(store)) == (2 if previous else 1), "行は commit 済み（対の無い行）"
    csv_path = out_dir / "sensors_2026-08-25.csv"
    if fail_at == 2 or previous:
        assert csv_path.exists(), "manifest の無い CSV"
    else:
        assert not csv_path.exists()
    assert_safe(out_dir, store)


# ---------------------------------------------------------------- lock


def hold_lock(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(out_dir / lock_name_for(DAY), os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_held_lock_refuses_within_the_limit_and_writes_nothing(store, out_dir):
    fill(store)
    fd = hold_lock(out_dir)
    try:
        started = time.monotonic()
        with pytest.raises(CsvExportLockTimeout):
            export_day(store, DAY, tz=JST, out_dir=out_dir, lock_timeout_s=0.2)
        assert time.monotonic() - started < 3.0
    finally:
        os.close(fd)
    assert rows(store) == []
    assert [p.name for p in out_dir.iterdir()] == [lock_name_for(DAY)]


def test_another_day_is_not_blocked(store, out_dir):
    fill(store)
    fd = hold_lock(out_dir)
    try:
        path = export_day(store, date(2026, 8, 26), tz=JST, out_dir=out_dir, lock_timeout_s=0.2)
    finally:
        os.close(fd)
    assert path.exists()


def test_an_export_waits_for_the_lock_and_then_completes(db_path, rules, store, out_dir):
    fill(store)
    fd = hold_lock(out_dir)
    outcome: dict[str, object] = {}

    def worker() -> None:
        with SqliteStore(db_path, rules=rules, clock=SimulatedClock(1)) as other:
            outcome["path"] = export_day(other, DAY, tz=JST, out_dir=out_dir, lock_timeout_s=10)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        time.sleep(0.3)
        assert thread.is_alive(), "lock を待っている"
        assert rows(store) == []
        assert not (out_dir / "sensors_2026-08-25.csv").exists()
    finally:
        os.close(fd)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["path"] == out_dir / "sensors_2026-08-25.csv"
    assert_safe(out_dir, store)


def test_concurrent_exports_of_the_same_day_leave_a_matching_pair(
    db_path, rules, store, out_dir, monkeypatch
):
    """lock が無いと、CSV B と manifest A が対になって残りうる（PR #238 の Codex の指摘）。

    export ごとに CSV の内容を変え、rename の間を広げて、入れ違いを起こしやすくする。
    """
    fill(store)
    real_render = csv_export_module._render
    real_replace = os.replace

    def render(*args: object) -> tuple[bytes, list[int]]:
        payload, seconds = real_render(*args)  # type: ignore[arg-type]
        return payload + f"# {secrets.token_hex(8)}\n".encode(), seconds

    def slow_replace(src: object, dst: object) -> None:
        # 間隔をばらつかせて、export どうしの rename の順序を入れ違わせる
        time.sleep(secrets.randbelow(30) / 1000)
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(csv_export_module, "_render", render)
    monkeypatch.setattr(csv_export_module.os, "replace", slow_replace)
    for _ in range(8):
        run_concurrent_exports(db_path, rules, out_dir, workers=6)
        record = manifest_of(out_dir / "sensors_2026-08-25.csv")
        assert record.csv_sha256 == sha256((out_dir / "sensors_2026-08-25.csv").read_bytes())
        assert_safe(out_dir, store)
    assert len(rows(store)) == 48


def run_concurrent_exports(db_path: Path, rules, out_dir: Path, *, workers: int) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(workers)

    def worker() -> None:
        try:
            with SqliteStore(db_path, rules=rules, clock=SimulatedClock(1)) as other:
                barrier.wait()
                export_day(other, DAY, tz=JST, out_dir=out_dir, lock_timeout_s=30)
        except BaseException as exc:  # 試験のスレッドから持ち帰る
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []


# ---------------------------------------------------------------- migration 0012


def test_migration_is_append_only_and_idempotent(tmp_path, rules):
    path = tmp_path / "v11.db"
    directory = tmp_path / "m11"
    directory.mkdir()
    for migration in migrations.discover():
        if migration.version <= 11:
            shutil.copy(migration.path, directory / migration.path.name)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        migrations.apply_pending(conn, now_ms=1_000, directory=directory)
        conn.executemany(
            "INSERT INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, 'ok')",
            [("air.room", 2_000, 25.0), ("air.front_intake", 2_000, 26.0)],
        )
        conn.execute(
            "INSERT INTO events (ts_ms, kind, payload) VALUES (2000, 'gpu_mode', '{\"v\":1}')"
        )
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        before = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in tables
            if table != "schema_version"
        }
        assert "csv_exports" not in tables
    finally:
        conn.close()

    with SqliteStore(path, rules=rules, clock=SimulatedClock(5_000)) as store:
        for table, content in before.items():
            after = [tuple(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
            assert after == [tuple(row) for row in content], table
        assert rows(store) == [], "過去の export の行を埋めない（0100 §2.7）"
        versions = [
            tuple(row)
            for row in store.connection.execute("SELECT version, applied_ms FROM schema_version")
        ]
    assert versions[-1] == (12, 5_000)

    with SqliteStore(path, rules=rules, clock=SimulatedClock(9_000)) as again:
        assert [
            tuple(row)
            for row in again.connection.execute("SELECT version, applied_ms FROM schema_version")
        ] == versions, "冪等（再適用しない）"
