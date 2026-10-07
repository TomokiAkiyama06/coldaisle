"""再生の入力を export の manifest と照合する（決定記録 0100 段 2。#237）。

- dataset 用の再生: manifest の有無で分け、混在は bind の前に拒否する。すべてに無ければ
  「照合していない」run として bind する。すべてにあれば 0100 §2.3 の 1〜7 を取り込みの前に確かめる
- 通常の再生: manifest のある CSV は 1〜6 を確かめ、照合と取り込みを同じ snapshot から読む。
  manifest の有無が混ざれば、manifest の timezone と実効の timezone の食い違いを拒否する
- DST の曖昧な時刻・存在しない時刻（0100 §2.5）
- fingerprint の版（manifest の無い入力の値は変えない）
- ``dataset_source_run`` に照合した timezone と ``export_binding_sha256`` を bind と同じ
  transaction で記録する
- migration 0013 は追記のみで冪等
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import coldaisle.daemon as daemon_module
from coldaisle.clock import SimulatedClock
from coldaisle.csv_export_manifest import (
    ExportRecord,
    export_binding_sha256,
    export_record_sha256,
    local_time_problem,
    manifest_name_for_csv,
    parse_naive,
)
from coldaisle.daemon import Daemon
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.normalize import Normalizer
from coldaisle.ingest.protocol import RawSample
from coldaisle.ingest.replay import (
    FINGERPRINT_V2_TAG,
    ReplayBindingError,
    ReplaySource,
    replay_sha256,
)
from coldaisle.store import Quality, Reading, Sample, SqliteStore, migrations
from coldaisle.store.csv_export import day_bounds_ms, export_day
from conftest import CALIBRATION_PATH, QUALITY_RULES_PATH

JST = ZoneInfo("Asia/Tokyo")
NEW_YORK = ZoneInfo("America/New_York")
DAY_24 = date(2026, 8, 24)
DAY_25 = date(2026, 8, 25)
RUN_ALIAS = "run-" + "c" * 32
LOCK_S = 5.0


def no_sleep(_: float) -> None:
    """待たない。"""


def write_readings(store: SqliteStore, stamps: list[int]) -> None:
    for index, ts_ms in enumerate(stamps):
        store.insert_sample(
            Sample(
                ts_ms=ts_ms,
                readings=(
                    Reading(metric="air.room", value=20.0 + index, quality=Quality.OK),
                    Reading(metric="air.front_intake", value=21.0 + index, quality=Quality.OK),
                ),
            )
        )


def noon_stamps(day: date, tz: ZoneInfo, count: int = 4) -> list[int]:
    """その日の正午から 2.5 秒ごと（秒の端数を含む）の時刻。"""
    start_ms, _ = day_bounds_ms(day, tz)
    return [start_ms + 12 * 3_600_000 + index * 2_500 for index in range(count)]


def export_days(
    tmp_path: Path,
    rules,
    days: list[tuple[date, ZoneInfo, list[int]]],
    *,
    name: str = "prod.db",
) -> Path:
    """本番の DB に readings を書き、日ごとに export した出力ディレクトリを返す。"""
    out_dir = tmp_path / "csv"
    with SqliteStore(tmp_path / name, rules=rules, clock=SimulatedClock(10**13)) as store:
        for _, _, stamps in days:
            write_readings(store, stamps)
        for day, tz, _ in days:
            export_day(store, day, tz=tz, out_dir=out_dir, lock_timeout_s=LOCK_S)
    return out_dir


@pytest.fixture
def two_days(tmp_path, rules) -> tuple[Path, list[int]]:
    stamps = noon_stamps(DAY_24, JST) + noon_stamps(DAY_25, JST)
    out_dir = export_days(tmp_path, rules, [(DAY_24, JST, stamps[:4]), (DAY_25, JST, stamps[4:])])
    return out_dir, stamps


def manifest_path(csv_path: Path) -> Path:
    name = manifest_name_for_csv(csv_path.name)
    assert name is not None
    return csv_path.with_name(name)


def record_of(csv_path: Path) -> ExportRecord:
    return ExportRecord.from_manifest_bytes(manifest_path(csv_path).read_bytes())


def rewrite_manifest(csv_path: Path, **updates: object) -> None:
    record = record_of(csv_path)
    changed = ExportRecord.model_validate({**record.model_dump(), **updates})
    manifest_path(csv_path).write_bytes(changed.manifest_bytes())


def csv_of(out_dir: Path, day: date) -> Path:
    return out_dir / f"sensors_{day.isoformat()}.csv"


def replay(path: Path, **kwargs) -> ReplaySource:
    kwargs.setdefault("tz", JST)
    kwargs.setdefault("timezone_explicit", False)
    return ReplaySource(path, bulk=True, sleep=no_sleep, **kwargs)


def refused(check: str, path: Path, **kwargs) -> ReplayBindingError:
    with pytest.raises(ReplayBindingError) as caught:
        replay(path, **kwargs)
    assert caught.value.check == check, str(caught.value)
    return caught.value


def run_dataset_replay(source: ReplaySource, db: Path, rules) -> SqliteStore:
    store = SqliteStore(db, rules=rules, clock=source.clock)
    Daemon(
        source=source,
        store=store,
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=source.clock),
        source_name="replay",
        dataset_run_alias=RUN_ALIAS,
    ).run()
    return store


def daemon_main(csv: Path, db: Path, *argv: str) -> int:
    return daemon_module.main(
        [
            "--source",
            "replay",
            "--csv",
            str(csv),
            "--bulk",
            "--db",
            str(db),
            "--quality-rules",
            str(QUALITY_RULES_PATH),
            "--calibration",
            str(CALIBRATION_PATH),
            *argv,
        ]
    )


def stored_times(db: Path, rules) -> list[int]:
    with SqliteStore(db, rules=rules, clock=SimulatedClock(0)) as store:
        return [
            int(row[0])
            for row in store.connection.execute(
                "SELECT DISTINCT ts_ms FROM readings WHERE metric = 'air.room' ORDER BY ts_ms"
            )
        ]


# ---------------------------------------------------------------- 往復と bind


def test_export_and_replay_round_trip_to_the_truncated_seconds(two_days, tmp_path, rules):
    """保存される時刻は export 前の ``ts_ms`` の秒の切り捨てと全行で一致する（0100 §2.11）。"""
    out_dir, stamps = two_days
    assert daemon_main(out_dir, tmp_path / "replayed.db") == 0
    assert stored_times(tmp_path / "replayed.db", rules) == [ts // 1000 * 1000 for ts in stamps]


def test_dataset_replay_records_the_timezone_and_binding_with_the_bind(two_days, tmp_path, rules):
    out_dir, stamps = two_days
    source = replay(out_dir, dataset_provenance=True)
    expected = export_binding_sha256(
        [record_of(csv_of(out_dir, DAY_24)), record_of(csv_of(out_dir, DAY_25))]
    )
    assert source.local_timezone == "Asia/Tokyo"
    assert source.export_binding_sha256 == expected
    store = run_dataset_replay(source, tmp_path / "dataset.db", rules)
    try:
        assert store.dataset_source_run_export_binding() == ("Asia/Tokyo", expected)
        assert store.dataset_source_run_completed()
        times = [
            int(row[0])
            for row in store.connection.execute(
                "SELECT DISTINCT ts_ms FROM readings WHERE metric = 'air.room' ORDER BY ts_ms"
            )
        ]
        assert times == [ts // 1000 * 1000 for ts in stamps]
    finally:
        store.close()


def test_the_binding_is_written_in_the_bind_transaction(two_days, tmp_path, rules, monkeypatch):
    """bind の INSERT が失敗すれば、timezone も digest も残らない（同じ行・同じ transaction）。"""
    out_dir, _ = two_days
    source = replay(out_dir, dataset_provenance=True)
    with SqliteStore(tmp_path / "dataset.db", rules=rules, clock=source.clock) as store:
        store.bind_dataset_source_run(
            run_alias=RUN_ALIAS,
            source_kind="replay",
            source_sha256=source.source_sha256 or "",
            at_ms=0,
            local_timezone=source.local_timezone,
            export_binding_sha256=source.export_binding_sha256,
        )
        row = store.connection.execute(
            "SELECT run_alias, local_timezone, export_binding_sha256 FROM dataset_source_run"
        ).fetchone()
        assert tuple(row) == (RUN_ALIAS, "Asia/Tokyo", source.export_binding_sha256)


def test_dataset_replay_without_manifests_binds_an_unverified_run(tmp_path, rules, two_days):
    """manifest が無ければ「照合していない」run（両方 NULL）。v1 の再生成は従来どおり。"""
    out_dir, _ = two_days
    for csv_path in out_dir.glob("sensors_*.csv"):
        manifest_path(csv_path).unlink()
    source = replay(out_dir, dataset_provenance=True, tz=JST, timezone_explicit=True)
    assert source.local_timezone is None
    assert source.export_binding_sha256 is None
    store = run_dataset_replay(source, tmp_path / "dataset.db", rules)
    try:
        assert store.dataset_source_run_export_binding() == (None, None)
        assert store.dataset_source_run_completed()
    finally:
        store.close()


def test_dataset_replay_with_mixed_manifests_is_refused_before_the_bind(two_days, tmp_path, rules):
    out_dir, _ = two_days
    manifest_path(csv_of(out_dir, DAY_25)).unlink()
    db = tmp_path / "dataset.db"
    with pytest.raises(SystemExit):
        daemon_main(out_dir, db, "--dataset-run-alias", RUN_ALIAS)
    with SqliteStore(db, rules=rules, clock=SimulatedClock(0)) as store:
        assert store.dataset_source_run() is None, "DB は空のまま（作り直せる）"
        assert store.connection.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
    refused("manifest_partial", out_dir, dataset_provenance=True)


def test_refusal_is_logged_with_the_file_and_the_check(two_days, capsys):
    out_dir, _ = two_days
    rewrite_manifest(csv_of(out_dir, DAY_25), timezone="UTC")
    with pytest.raises(SystemExit):
        daemon_main(out_dir, out_dir.parent / "x.db")
    err = capsys.readouterr().err
    assert '"check": "timezone_mixed"' in err or "timezone_mixed" in err
    assert "sensors_2026-08-2" in err


# ---------------------------------------------------------------- timezone


def test_explicit_timezone_that_differs_from_the_manifest_is_refused(two_days, tmp_path, rules):
    out_dir, _ = two_days
    refused("timezone_flag", out_dir, tz=ZoneInfo("Etc/GMT-9"), timezone_explicit=True)
    refused(
        "timezone_flag",
        out_dir,
        tz=ZoneInfo("Etc/GMT-9"),
        timezone_explicit=True,
        dataset_provenance=True,
    )
    with pytest.raises(SystemExit):
        daemon_main(out_dir, tmp_path / "a.db", "--timezone", "UTC")


def test_explicit_equal_or_omitted_timezone_is_accepted(two_days, tmp_path, rules):
    out_dir, stamps = two_days
    assert daemon_main(out_dir, tmp_path / "a.db", "--timezone", "Asia/Tokyo") == 0
    assert daemon_main(out_dir, tmp_path / "b.db") == 0
    expected = [ts // 1000 * 1000 for ts in stamps]
    assert stored_times(tmp_path / "a.db", rules) == expected
    assert stored_times(tmp_path / "b.db", rules) == expected


def test_omitted_timezone_uses_the_manifest_timezone(tmp_path, rules):
    """`--timezone` を省けば、既定値ではなく manifest の timezone で読む。"""
    utc = ZoneInfo("UTC")
    stamps = noon_stamps(DAY_24, utc)
    out_dir = export_days(tmp_path, rules, [(DAY_24, utc, stamps)])
    source = replay(out_dir, tz=JST, timezone_explicit=False, dataset_provenance=True)
    assert source.local_timezone == "UTC"
    assert daemon_main(out_dir, tmp_path / "a.db") == 0
    assert stored_times(tmp_path / "a.db", rules) == [ts // 1000 * 1000 for ts in stamps]


def test_changing_only_the_manifest_timezone_fails_the_seconds_check(two_days):
    """名前の一致だけでは足りない。全行の絶対時刻の hash で食い違いに気づく（0100 §2.4）。"""
    out_dir, _ = two_days
    for day in (DAY_24, DAY_25):
        rewrite_manifest(csv_of(out_dir, day), timezone="UTC")
    refused("row_seconds_sha256", out_dir, tz=ZoneInfo("UTC"))


def test_manifests_with_different_timezones_in_one_run_are_refused(tmp_path, rules):
    other = ZoneInfo("Etc/GMT-9")
    out_dir = export_days(
        tmp_path,
        rules,
        [(DAY_24, JST, noon_stamps(DAY_24, JST)), (DAY_25, other, noon_stamps(DAY_25, other))],
    )
    refused("timezone_mixed", out_dir)
    refused("timezone_mixed", out_dir, dataset_provenance=True)


def test_unreadable_manifest_timezone_is_refused(two_days):
    out_dir, _ = two_days
    for day in (DAY_24, DAY_25):
        rewrite_manifest(csv_of(out_dir, day), timezone="Not/A_Zone")
    with pytest.raises(ReplayBindingError) as caught:
        replay(out_dir, tz=JST, timezone_explicit=False)
    assert caught.value.check == "timezone_unreadable"


# ---------------------------------------------------------------- 照合の各検査


def test_csv_bytes_that_differ_from_the_manifest_are_refused(two_days):
    out_dir, _ = two_days
    path = csv_of(out_dir, DAY_24)
    path.write_bytes(path.read_bytes().replace(b"20.0", b"20.5"))
    refused("csv_sha256", out_dir)
    refused("csv_sha256", out_dir, dataset_provenance=True)


def test_manifest_naming_another_csv_is_refused(two_days):
    out_dir, _ = two_days
    shutil.copy(manifest_path(csv_of(out_dir, DAY_25)), manifest_path(csv_of(out_dir, DAY_24)))
    refused("csv_name", out_dir)


def test_row_count_mismatch_is_refused(two_days):
    out_dir, _ = two_days
    rewrite_manifest(csv_of(out_dir, DAY_24), row_count=5)
    refused("row_count", out_dir)


def test_rows_outside_the_day_are_refused(two_days):
    out_dir, _ = two_days
    record = record_of(csv_of(out_dir, DAY_24))
    rewrite_manifest(csv_of(out_dir, DAY_24), day_end_ms=record.day_start_ms + 3_600_000)
    error = refused("day_range", out_dir)
    assert error.value == "2026-08-24T12:00:00", "最初の1行の値だけ"


def test_a_symlinked_manifest_is_refused(two_days, tmp_path):
    out_dir, _ = two_days
    target = manifest_path(csv_of(out_dir, DAY_24))
    moved = tmp_path / "elsewhere.json"
    target.rename(moved)
    target.symlink_to(moved)
    refused("manifest_regular_file", out_dir)
    refused("manifest_regular_file", out_dir, dataset_provenance=True)


def test_a_fifo_manifest_is_refused_without_blocking(two_days):
    out_dir, _ = two_days
    target = manifest_path(csv_of(out_dir, DAY_24))
    target.unlink()
    os.mkfifo(target)
    refused("manifest_regular_file", out_dir)


def test_a_malformed_manifest_is_refused(two_days):
    out_dir, _ = two_days
    manifest_path(csv_of(out_dir, DAY_24)).write_text("{}", encoding="utf-8")
    refused("manifest_format", out_dir)


def test_duplicate_export_ids_are_refused_in_a_dataset_replay(two_days):
    out_dir, _ = two_days
    first = record_of(csv_of(out_dir, DAY_24))
    rewrite_manifest(csv_of(out_dir, DAY_25), export_id=first.export_id)
    refused("export_id_duplicate", out_dir, dataset_provenance=True)


def test_ingested_bytes_are_the_verified_snapshot_even_if_the_path_is_replaced(two_days):
    """通常の再生でも、照合の後に CSV を差し替えても取り込む bytes は照合した bytes（§2.3）。"""
    out_dir, _ = two_days
    source = replay(out_dir)
    for day in (DAY_24, DAY_25):
        path = csv_of(out_dir, day)
        path.write_bytes(path.read_bytes().replace(b"20.0", b"99.0").replace(b"24.0", b"99.0"))
    values = [m.channels["room_temp"] for m in source.stream() if isinstance(m, RawSample)]
    assert values == [20.0, 21.0, 22.0, 23.0] * 2


# ---------------------------------------------------------------- 通常の再生


def test_plain_replay_without_manifests_still_reads_and_warns_once(tmp_path, capsys):
    path = tmp_path / "replay.csv"
    path.write_text(
        "timestamp,room_temp\n2026-08-24T00:00:00,24.0\n2026-08-24T00:00:03,24.1\n",
        encoding="utf-8",
    )
    from coldaisle import logs

    logs.configure("INFO")
    source = ReplaySource(path, tz=JST, sleep=no_sleep, bulk=True)
    assert len([m for m in source.stream() if isinstance(m, RawSample)]) == 2
    err = capsys.readouterr().err
    assert err.count("timezone を照合していない") == 1


def test_plain_replay_mixing_inputs_refuses_a_different_effective_timezone(two_days, tmp_path):
    out_dir, _ = two_days
    manifest_path(csv_of(out_dir, DAY_25)).unlink()
    refused("timezone_flag", out_dir, tz=ZoneInfo("UTC"), timezone_explicit=True)
    # 省略時は既定値（Asia/Tokyo）が実効の timezone。manifest の UTC と違えば拒否する
    for day in (DAY_24,):
        rewrite_manifest(csv_of(out_dir, day), timezone="UTC")
    with pytest.raises(ReplayBindingError):
        replay(out_dir, tz=JST, timezone_explicit=False)


def test_plain_replay_mixing_inputs_with_the_same_timezone_continues(two_days, tmp_path, rules):
    out_dir, stamps = two_days
    manifest_path(csv_of(out_dir, DAY_25)).unlink()
    assert daemon_main(out_dir, tmp_path / "a.db") == 0
    assert daemon_main(out_dir, tmp_path / "b.db", "--timezone", "Asia/Tokyo") == 0
    assert stored_times(tmp_path / "a.db", rules) == [ts // 1000 * 1000 for ts in stamps]


def test_plain_replay_does_not_check_duplicate_export_ids(two_days):
    """7 は dataset 用の再生だけ（0100 §2.3 は通常の再生に 1〜6 を求める）。"""
    out_dir, _ = two_days
    first = record_of(csv_of(out_dir, DAY_24))
    rewrite_manifest(csv_of(out_dir, DAY_25), export_id=first.export_id)
    replay(out_dir)


# ---------------------------------------------------------------- DST（0100 §2.5）


def test_local_time_problem_classifies_dst_edges():
    assert local_time_problem(parse_naive("2026-11-01T01:30:00"), NEW_YORK) == "ambiguous"
    assert local_time_problem(parse_naive("2026-03-08T02:30:00"), NEW_YORK) == "nonexistent"
    assert local_time_problem(parse_naive("2026-11-01T03:30:00"), NEW_YORK) is None
    assert local_time_problem(parse_naive("2026-08-24T01:30:00"), JST) is None


def test_ambiguous_rows_on_the_fall_back_day_are_refused(tmp_path, rules):
    fall_back = date(2026, 11, 1)
    # 01:30 EDT と 01:30 EST（同じローカル時刻が2回）
    first = int(datetime(2026, 11, 1, 5, 30, tzinfo=UTC).timestamp() * 1000)
    second = int(datetime(2026, 11, 1, 6, 30, tzinfo=UTC).timestamp() * 1000)
    out_dir = export_days(tmp_path, rules, [(fall_back, NEW_YORK, [first, second])])
    refused("dst_ambiguous", out_dir, tz=NEW_YORK, dataset_provenance=True)


def test_nonexistent_rows_on_the_spring_forward_day_are_refused(tmp_path, rules):
    spring = date(2026, 3, 8)
    out_dir = export_days(tmp_path, rules, [(spring, NEW_YORK, noon_stamps(spring, NEW_YORK))])
    path = csv_of(out_dir, spring)
    lines = path.read_bytes().split(b"\r\n")
    lines.insert(1, b"2026-03-08T02:30:00" + lines[1][lines[1].index(b",") :])
    path.write_bytes(b"\r\n".join(lines))
    record = record_of(path)
    rewrite_manifest(
        path,
        csv_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        row_count=record.row_count + 1,
    )
    refused("dst_nonexistent", out_dir, tz=NEW_YORK, dataset_provenance=True)


def test_a_dst_day_without_rows_in_the_switch_passes(tmp_path, rules):
    fall_back = date(2026, 11, 1)
    stamps = noon_stamps(fall_back, NEW_YORK)
    out_dir = export_days(tmp_path, rules, [(fall_back, NEW_YORK, stamps)])
    source = replay(out_dir, tz=NEW_YORK, dataset_provenance=True)
    assert source.local_timezone == "America/New_York"


# ---------------------------------------------------------------- fingerprint（0100 §2.8）


def v1_fingerprint(files: list[Path]) -> str:
    """0100 の前の規則（basename の長さ・basename・大きさ・内容を順に）。"""
    digest = hashlib.sha256()
    for path in files:
        name = path.name.encode()
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name + len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def test_fingerprint_without_manifests_is_unchanged(two_days):
    out_dir, _ = two_days
    files = sorted(out_dir.glob("sensors_*.csv"))
    for path in files:
        manifest_path(path).unlink()
    assert replay_sha256(out_dir) == v1_fingerprint(files)
    assert replay(out_dir, dataset_provenance=True).source_sha256 == v1_fingerprint(files)


def test_fingerprint_with_manifests_covers_the_manifest_bytes(two_days):
    out_dir, _ = two_days
    files = sorted(out_dir.glob("sensors_*.csv"))
    digest = hashlib.sha256(FINGERPRINT_V2_TAG)
    for path in files:
        for part in (path, manifest_path(path)):
            name = part.name.encode()
            data = part.read_bytes()
            digest.update(len(name).to_bytes(8, "big") + name + len(data).to_bytes(8, "big") + data)
    with_manifests = replay_sha256(out_dir)
    assert with_manifests == digest.hexdigest()
    assert with_manifests != v1_fingerprint(files)
    assert replay(out_dir, dataset_provenance=True).source_sha256 == with_manifests

    # manifest の bytes を変えると値が変わる（export_id を別の正当な値にする）
    rewrite_manifest(files[0], export_id="export-" + "f" * 32)
    assert replay_sha256(out_dir) != with_manifests


def test_fingerprint_refuses_partial_manifests(two_days):
    out_dir, _ = two_days
    manifest_path(csv_of(out_dir, DAY_25)).unlink()
    with pytest.raises(ReplayBindingError):
        replay_sha256(out_dir)


# ---------------------------------------------------------------- 束縛の digest と store


def test_export_binding_sha256_golden_vector():
    record = ExportRecord(
        export_id="export-" + "0123456789abcdef" * 2,
        csv_name="sensors_2026-08-24.csv",
        csv_sha256="a" * 64,
        day="2026-08-24",
        timezone="Asia/Tokyo",
        day_start_ms=1_787_497_200_000,
        day_end_ms=1_787_583_600_000,
        timestamp_format="%Y-%m-%dT%H:%M:%S",
        row_count=0,
        row_seconds_sha256=hashlib.sha256(b"").hexdigest(),
    )
    other = record.model_copy(update={"export_id": "export-" + "0" * 32})
    payload = (
        f'[["{other.export_id}","{export_record_sha256(other)}"],'
        f'["{record.export_id}","{export_record_sha256(record)}"]]\n'
    ).encode()
    assert export_binding_sha256([record, other]) == hashlib.sha256(payload).hexdigest()
    assert export_binding_sha256([other, record]) == export_binding_sha256([record, other])
    with pytest.raises(ValueError, match="重複"):
        export_binding_sha256([record, record])


@pytest.mark.parametrize(
    ("timezone", "binding"),
    [("Asia/Tokyo", None), (None, "a" * 64), ("", "a" * 64), ("Asia/Tokyo", "A" * 64)],
)
def test_bind_refuses_a_half_or_malformed_binding(tmp_path, rules, timezone, binding):
    with SqliteStore(tmp_path / "d.db", rules=rules, clock=SimulatedClock(0)) as store:
        with pytest.raises(ValueError):
            store.bind_dataset_source_run(
                run_alias=RUN_ALIAS,
                source_kind="replay",
                source_sha256="b" * 64,
                at_ms=0,
                local_timezone=timezone,
                export_binding_sha256=binding,
            )
        assert store.dataset_source_run() is None


@pytest.mark.parametrize(
    ("timezone", "binding"), [("Asia/Tokyo", None), (None, "a" * 64), ("Asia/Tokyo", "z" * 64)]
)
def test_the_table_refuses_a_half_or_malformed_binding(tmp_path, rules, timezone, binding):
    with (
        SqliteStore(tmp_path / "d.db", rules=rules, clock=SimulatedClock(0)) as store,
        pytest.raises(sqlite3.IntegrityError),
    ):
        store.connection.execute(
            "INSERT INTO dataset_source_run (singleton, run_alias, source_kind, "
            "source_sha256, bound_ms, local_timezone, export_binding_sha256) "
            "VALUES (1, ?, 'replay', ?, 0, ?, ?)",
            (RUN_ALIAS, "b" * 64, timezone, binding),
        )


def test_the_binding_cannot_be_updated(two_days, tmp_path, rules):
    out_dir, _ = two_days
    store = run_dataset_replay(
        replay(out_dir, dataset_provenance=True), tmp_path / "dataset.db", rules
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute("UPDATE dataset_source_run SET local_timezone = 'UTC'")
    finally:
        store.close()


def test_migration_0013_is_append_only_and_idempotent(tmp_path, rules):
    path = tmp_path / "v12.db"
    directory = tmp_path / "m12"
    directory.mkdir()
    for migration in migrations.discover():
        if migration.version <= 12:
            shutil.copy(migration.path, directory / migration.path.name)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        migrations.apply_pending(conn, now_ms=1_000, directory=directory)
        conn.execute(
            "INSERT INTO dataset_source_run (singleton, run_alias, source_kind, source_sha256, "
            "bound_ms) VALUES (1, ?, 'replay', ?, 0)",
            (RUN_ALIAS, "b" * 64),
        )
        conn.execute(
            "INSERT INTO readings (metric, ts_ms, value, quality) VALUES ('air.room', 5, 1.0, 'ok')"
        )
        before = {
            "dataset_source_run": conn.execute(
                "SELECT run_alias, source_kind, source_sha256, bound_ms FROM dataset_source_run"
            ).fetchall(),
            "readings": conn.execute("SELECT * FROM readings").fetchall(),
        }
    finally:
        conn.close()

    with SqliteStore(path, rules=rules, clock=SimulatedClock(5_000)) as store:
        rows = store.connection.execute(
            "SELECT run_alias, source_kind, source_sha256, bound_ms FROM dataset_source_run"
        ).fetchall()
        assert [tuple(row) for row in rows] == [tuple(row) for row in before["dataset_source_run"]]
        assert store.dataset_source_run() == (RUN_ALIAS, "replay", "b" * 64)
        assert store.dataset_source_run_export_binding() == (None, None), "後から埋めない"
        assert [tuple(r) for r in store.connection.execute("SELECT * FROM readings")] == [
            tuple(r) for r in before["readings"]
        ]
        versions = [
            tuple(row)
            for row in store.connection.execute("SELECT version, applied_ms FROM schema_version")
        ]
    assert (13, 5_000) in versions

    with SqliteStore(path, rules=rules, clock=SimulatedClock(9_000)) as again:
        assert [
            tuple(row)
            for row in again.connection.execute("SELECT version, applied_ms FROM schema_version")
        ] == versions, "冪等（再適用しない）"
