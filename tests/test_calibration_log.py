"""較正の変更の記録（#233 / 決定記録 0099）。

実機は使わない（AGENTS.md ルール 7）。mock・一時 DB・手で進める時計で、0099 §2.9 の性質を確かめる。

- 変化のときだけ1行（全温度 metric。note などや 0 / -0.0、湿度、対応表に無いキーでは増えない）
- 書き手の排他（DB ごとの lock）
- 起動時と運転中の時計の戻り
- 追記のみ（trigger）と、読む側の全行の検証（trigger を外した DB）
- ``--apply`` は書かず、取り込みの再起動で入る。replay は読みも書きもしない
- 学習の入口の検査
- migration 0011 は追記のみで冪等
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from coldaisle.calibration_log import (
    CalibrationActivationRefused,
    IngestCalibrationGate,
    activate_calibration,
    calibration_change_points,
    ingest_lock_path,
    verify_training_calibration,
)
from coldaisle.calibration_offsets import (
    calibration_applies,
    canonical_offsets_bytes,
    effective_metric_offsets,
)
from coldaisle.channels import METRIC_TO_CHANNEL
from coldaisle.clock import SimulatedClock
from coldaisle.daemon import Config, Daemon, build, main
from coldaisle.ingest import Normalizer
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.protocol import RawMessage, RawSample
from coldaisle.store import Quality, QualityRules, Reading, Sample, SqliteStore, migrations
from coldaisle.store.calibration_history import (
    CalibrationActivation,
    CalibrationHistory,
    CalibrationHistoryError,
    activation_row_sha256,
    read_calibration_history,
)
from coldaisle.store.csv_export import TIMESTAMP_RESOLUTION_MS
from conftest import CALIBRATION_PATH, CONFIG_DIR, QUALITY_RULES_PATH, SCENARIOS_PATH

FILE_SHA = "f" * 64
CALIBRATED_CHANNELS = tuple(
    sorted(channel for metric, channel in METRIC_TO_CHANNEL.items() if calibration_applies(metric))
)


class ManualClock:
    """試験が自由に動かせる時計（壁時計が戻る状況を作る。``SimulatedClock`` は戻せない）。"""

    def __init__(self, now_ms: int) -> None:
        self.now = now_ms

    def now_ms(self) -> int:
        return self.now


class StampedSource:
    """各メッセージの直前に時計を ``stamps`` の時刻へ合わせて流すソース。"""

    def __init__(self, messages: list[RawMessage], clock: ManualClock, stamps: list[int]) -> None:
        self._messages = messages
        self._clock = clock
        self._stamps = stamps

    @property
    def clock(self) -> ManualClock:
        return self._clock

    def stream(self) -> Iterator[RawMessage]:
        for message, at_ms in zip(self._messages, self._stamps, strict=True):
            self._clock.now = at_ms
            yield message


def raw_sample(seq: int) -> RawSample:
    return RawSample(seq=seq, up=1_000 * (seq + 1), channels={"room_temp": 26.0})


def rows(store: SqliteStore) -> tuple[CalibrationActivation, ...]:
    return store.calibration_activations()


def record(store: SqliteStore, now_ms: int, offsets_c: dict[str, float], **fields: object) -> int:
    return activate_calibration(
        store,
        source_kind=str(fields.get("source_kind", "mock")),
        offsets_c=offsets_c,
        calibrated_at=fields.get("calibrated_at"),  # type: ignore[arg-type]
        calibration_file_sha256=str(fields.get("calibration_file_sha256", FILE_SHA)),
        now_ms=now_ms,
    )


def readings_at(store: SqliteStore, *ts_ms: int) -> None:
    store.insert_samples(
        Sample(ts_ms=ts, readings=(Reading(metric="air.room", value=25.0, quality=Quality.OK),))
        for ts in ts_ms
    )


@pytest.fixture
def store(tmp_path, rules) -> Iterator[SqliteStore]:
    with SqliteStore(tmp_path / "prod.db", rules=rules, clock=SimulatedClock(0)) as opened:
        yield opened


def history_rows(path: Path, *rows_ms: int) -> Path:
    """``rows_ms`` の各時刻に写像を変えながら行を足した DB。"""
    with SqliteStore(
        path, rules=QualityRules.from_yaml(QUALITY_RULES_PATH), clock=SimulatedClock(0)
    ) as db:
        for index, ts_ms in enumerate(rows_ms):
            record(db, ts_ms, {"front_intake": 0.5 * (index + 1)})
    return path


# ---------------------------------------------------------------- §2.2 / §2.3 変化のときだけ


def test_same_calibration_twice_records_one_row(store):
    assert record(store, 1_000, {"front_intake": 0.25}) == 1_000
    assert record(store, 2_000, {"front_intake": 0.25}) == 1_000, "下限は最後の行の時刻のまま"
    (row,) = rows(store)
    assert row.ts_ms == 1_000
    assert row.previous_row_sha256 is None
    assert row.source_kind == "mock"
    assert row.calibration_file_sha256 == FILE_SHA
    assert row.offsets == effective_metric_offsets({"front_intake": 0.25})
    assert row.offsets_json.encode() == canonical_offsets_bytes(row.offsets)
    assert row.offsets_json.endswith("\n")
    assert hashlib.sha256(row.offsets_json.encode()).hexdigest() == row.offsets_sha256


@pytest.mark.parametrize(
    ("offsets_c", "fields"),
    [
        pytest.param({}, {"calibrated_at": "2026-10-07T00:00:00+09:00"}, id="calibrated-at"),
        pytest.param({}, {"calibration_file_sha256": "e" * 64}, id="file-bytes-only"),
        pytest.param({"front_intake": 0.0}, {}, id="explicit-zero"),
        pytest.param({"front_intake": -0.0}, {}, id="negative-zero"),
        pytest.param({"front_intake": 0}, {}, id="integer-zero"),
        pytest.param({"room_humidity": 1.5}, {}, id="humidity-only"),
        pytest.param({"not_a_channel": 2.0}, {}, id="key-not-in-the-map"),
    ],
)
def test_changes_that_do_not_change_the_effective_map_add_no_row(store, offsets_c, fields):
    record(store, 1_000, {})
    record(store, 2_000, offsets_c, **fields)
    assert len(rows(store)) == 1


def test_note_reference_and_samples_do_not_reach_the_record(tmp_path, rules):
    """``Calibration`` の説明の欄（note / reference / samples）は比べない（0099 §2.3）。"""
    path = tmp_path / "prod.db"
    base = Calibration(offsets_c={"front_intake": 0.1})
    edited = base.model_copy(
        update={"note": "書き換えた", "reference": "other", "samples": {"front_intake": 9}}
    )
    clock = ManualClock(1_000)
    with SqliteStore(path, rules=rules, clock=clock) as db:
        for calibration in (base, edited):
            gate = IngestCalibrationGate(
                db_path=path,
                source_kind="mock",
                calibration=calibration,
                calibration_file_sha256=FILE_SHA,
            )
            gate.open(db, clock)
            gate.close()
            clock.now += 1_000
        assert len(rows(db)) == 1


@pytest.mark.parametrize("channel", CALIBRATED_CHANNELS)
def test_the_smallest_change_on_any_temperature_channel_adds_a_row(store, channel):
    """artifact の有無に依らず、全温度 metric で比べる（0099 §2.3 / R3）。"""
    record(store, 1_000, {channel: 0.5})
    record(store, 2_000, {channel: math.nextafter(0.5, 1.0)})
    first, second = rows(store)
    assert second.previous_row_sha256 == first.row_sha256
    assert second.ts_ms == 2_000


def test_calibrated_channels_are_every_temperature_channel():
    assert "room_humidity" not in CALIBRATED_CHANNELS
    assert set(CALIBRATED_CHANNELS) == set(METRIC_TO_CHANNEL.values()) - {"room_humidity"}


# ---------------------------------------------------------------- §2.2 の4 / 5 起動時の時計


def test_changed_calibration_with_the_clock_at_or_before_saved_readings_is_refused(store):
    record(store, 1_000, {"front_intake": 0.1})
    readings_at(store, 1_500, 5_000)
    with pytest.raises(CalibrationActivationRefused, match="readings の最大の時刻"):
        record(store, 5_000, {"front_intake": 0.2})
    assert len(rows(store)) == 1, "記録を書かない"
    assert record(store, 5_001, {"front_intake": 0.2}) == 5_001


def test_first_row_on_a_db_with_readings_follows_the_readings(store):
    readings_at(store, 1_000, 2_000)
    with pytest.raises(CalibrationActivationRefused):
        record(store, 2_000, {})
    assert rows(store) == ()
    assert record(store, 2_001, {}) == 2_001


@pytest.mark.parametrize("now_ms", [999, 1_000])
def test_unchanged_calibration_with_the_clock_at_or_before_the_last_row_is_refused(store, now_ms):
    record(store, 1_000, {"front_intake": 0.1})
    with pytest.raises(CalibrationActivationRefused, match="最後の行の時刻"):
        record(store, now_ms, {"front_intake": 0.1})


def test_changed_calibration_with_the_clock_at_or_before_the_last_row_is_refused(store):
    record(store, 1_000, {"front_intake": 0.1})
    with pytest.raises(CalibrationActivationRefused, match="最後の行の時刻"):
        record(store, 1_000, {"front_intake": 0.2})
    assert len(rows(store)) == 1


def test_unchanged_calibration_after_the_last_row_starts_even_behind_the_readings(store):
    """readings の最大との比較は較正が変わる起動だけ（§2.2 の5）。"""
    record(store, 1_000, {"front_intake": 0.1})
    readings_at(store, 9_000)
    assert record(store, 5_000, {"front_intake": 0.1}) == 1_000


def test_the_row_sits_between_old_and_new_readings(tmp_path, rules):
    """行の時刻は、その起動より前の readings の最大より大きく、最初に保存した sample 以下。"""
    path = tmp_path / "prod.db"
    stored: list[int] = []
    for calibration, start_ms in (
        (Calibration(offsets_c={"front_intake": 0.1}), 10_000),
        (Calibration(offsets_c={"front_intake": 0.2}), 20_000),
    ):
        clock = ManualClock(start_ms)
        daemon = Daemon(
            source=StampedSource(
                [raw_sample(0), raw_sample(1)], clock, [start_ms + 0, start_ms + 2_500]
            ),
            store=SqliteStore(path, rules=rules, clock=clock),
            normalizer=Normalizer(rules=rules, calibration=calibration, clock=clock),
            calibration_gate=IngestCalibrationGate(
                db_path=path,
                source_kind="mock",
                calibration=calibration,
                calibration_file_sha256=FILE_SHA,
            ),
        )
        try:
            before = daemon.store.max_reading_ts_ms()
            daemon.run()
            new_row = rows(daemon.store)[-1]
            first_saved = daemon.store.connection.execute(
                "SELECT MIN(ts_ms) FROM readings WHERE ts_ms >= ?", (new_row.ts_ms,)
            ).fetchone()[0]
        finally:
            daemon.store.close()
        assert before is None or new_row.ts_ms > before
        assert new_row.ts_ms <= first_saved
        stored.append(new_row.ts_ms)
    assert stored == [10_000, 20_000]


# ---------------------------------------------------------------- §2.2 / §5 #11 運転中の時計の戻り


def test_samples_before_the_last_row_are_discarded_and_counted(tmp_path, rules):
    path = tmp_path / "prod.db"
    clock = ManualClock(10_000)
    calibration = Calibration(offsets_c={"front_intake": 0.1})
    stamps = [11_000, 9_000, 9_999, 10_000, 12_000]
    daemon = Daemon(
        source=StampedSource([raw_sample(seq) for seq in range(5)], clock, stamps),
        store=SqliteStore(path, rules=rules, clock=clock),
        normalizer=Normalizer(rules=rules, calibration=calibration, clock=clock),
        calibration_gate=IngestCalibrationGate(
            db_path=path,
            source_kind="mock",
            calibration=calibration,
            calibration_file_sha256=FILE_SHA,
        ),
    )
    try:
        stats = daemon.run()
        saved = [
            row[0]
            for row in daemon.store.connection.execute(
                "SELECT DISTINCT ts_ms FROM readings ORDER BY ts_ms"
            )
        ]
    finally:
        daemon.store.close()
    assert stats.before_calibration_record == 2
    assert stats.discarded == 0, "取り込みは止まらず、捨てた理由は別に数える"
    assert stats.samples == 3
    assert saved == [10_000, 11_000, 12_000], "記録の時刻ちょうどの sample は保存する"
    assert stats.as_fields()["before_calibration_record"] == 2


# ---------------------------------------------------------------- §2.2 / §5 #12 書き手の排他


def test_a_second_ingest_on_the_same_db_cannot_take_the_lock(tmp_path, rules):
    path = tmp_path / "prod.db"
    clock = ManualClock(1_000)
    first = IngestCalibrationGate(
        db_path=path,
        source_kind="serial",
        calibration=Calibration(offsets_c={"front_intake": 0.1}),
        calibration_file_sha256=FILE_SHA,
    )
    second = IngestCalibrationGate(
        db_path=path,
        source_kind="serial",
        calibration=Calibration(offsets_c={"front_intake": 0.2}),
        calibration_file_sha256=FILE_SHA,
    )
    with (
        SqliteStore(path, rules=rules, clock=clock) as one,
        SqliteStore(path, rules=rules, clock=clock) as two,
    ):
        assert first.open(one, clock) == 1_000
        clock.now = 2_000
        with pytest.raises(CalibrationActivationRefused, match="lock"):
            second.open(two, clock)
        assert len(rows(one)) == 1, "lock が取れなければ記録を書かない"
        first.close()
        assert second.open(two, clock) == 2_000
        second.close()
        assert len(rows(one)) == 2
    assert ingest_lock_path(path) == tmp_path / "prod.db.ingest.lock"


def test_a_refused_start_releases_the_lock(tmp_path, rules):
    path = tmp_path / "prod.db"
    clock = ManualClock(1_000)
    gate = IngestCalibrationGate(
        db_path=path,
        source_kind="mock",
        calibration=Calibration(),
        calibration_file_sha256=FILE_SHA,
    )
    with SqliteStore(path, rules=rules, clock=clock) as db:
        gate.open(db, clock)
        gate.close()
        with pytest.raises(CalibrationActivationRefused):
            gate.open(db, clock)  # 時計が最後の行の時刻と同じ
        clock.now = 2_000
        assert gate.open(db, clock) == 1_000, "拒否の後に lock が残っていない"
        gate.close()


def test_replay_cannot_be_a_writer():
    with pytest.raises(ValueError, match="serial / mock"):
        IngestCalibrationGate(
            db_path=Path("unused.db"),
            source_kind="replay",
            calibration=Calibration(),
            calibration_file_sha256=FILE_SHA,
        )


# ---------------------------------------------------------------- 合成の起点（build / main）


def daemon_config(tmp_path: Path, **overrides: object) -> Config:
    values: dict[str, object] = {
        "db": tmp_path / "coldaisle.db",
        "scenarios": SCENARIOS_PATH,
        "quality_rules": QUALITY_RULES_PATH,
        "calibration": CALIBRATION_PATH,
        "speed": 1_000.0,
        "scenario": "idle",
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


def run_once(config: Config) -> None:
    daemon = build(config)
    try:
        daemon.run(max_samples=1)
    finally:
        daemon.store.close()
    # 次の起動の壁時計が、保存した最後の時刻より確実に進むようにする（ms の同着を避ける）
    time.sleep(0.005)


def db_rows(path: Path) -> tuple[CalibrationActivation, ...]:
    return read_calibration_history(path).rows


@pytest.mark.parametrize("source", ["mock", "serial"])
def test_build_wires_the_gate_for_sources_that_apply_calibration(tmp_path, source):
    daemon = build(daemon_config(tmp_path, source=source))
    try:
        gate = daemon._calibration_gate
        assert gate is not None
        assert gate._source_kind == source
        assert (
            gate._calibration_file_sha256
            == hashlib.sha256(CALIBRATION_PATH.read_bytes()).hexdigest()
        )
    finally:
        daemon.store.close()


def test_restarting_with_the_same_file_records_one_row(tmp_path):
    config = daemon_config(tmp_path)
    run_once(config)
    run_once(config)
    (row,) = db_rows(config.db)
    shipped = Calibration.from_json(CALIBRATION_PATH)
    assert row.offsets == effective_metric_offsets(shipped.offsets_c)
    assert row.calibrated_at == shipped.calibrated_at


def test_apply_writes_no_row_and_the_restart_records_it(tmp_path, rules, monkeypatch):
    """``--apply`` は記録を書かず、取り込みの再起動で1行入る。手の書き換えも同じ（0099 §2.2）。"""
    from coldaisle.calibrate import main as calibrate_main
    from test_calibrate import NOW_MS, _argv, fill

    monkeypatch.setattr("coldaisle.calibrate.WallClock", lambda: SimulatedClock(NOW_MS))
    db = tmp_path / "prod.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(NOW_MS)) as filled:
        fill(filled)
    target = tmp_path / "calibration.json"
    target.write_bytes(CALIBRATION_PATH.read_bytes())
    config = daemon_config(tmp_path, db=db, calibration=target)

    run_once(config)
    assert len(db_rows(db)) == 1

    assert calibrate_main([*_argv(db, target), "--apply"]) == 0
    assert len(db_rows(db)) == 1, "--apply は記録を書かない"
    run_once(config)
    after_apply = db_rows(db)
    assert len(after_apply) == 2
    applied = Calibration.from_json(target)
    assert after_apply[-1].offsets == effective_metric_offsets(applied.offsets_c)
    assert (
        after_apply[-1].calibration_file_sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    )

    edited = json.loads(target.read_text(encoding="utf-8"))
    edited["offsets_c"]["gpu_exhaust"] = edited["offsets_c"].get("gpu_exhaust", 0.0) + 0.125
    target.write_text(json.dumps(edited), encoding="utf-8")
    assert len(db_rows(db)) == 2, "ファイルを書いただけでは記録されない"
    run_once(config)
    assert len(db_rows(db)) == 3, "手で書き換えたファイルも取り込みの再起動で記録される"


def test_apply_says_the_change_is_recorded_at_the_ingest_restart():
    from coldaisle.calibrate import RESTART_ORDER

    assert "取り込みを再起動した時点で記録されます" in RESTART_ORDER


def test_main_refuses_to_start_while_another_ingest_holds_the_lock(tmp_path, capsys):
    db = tmp_path / "coldaisle.db"
    gate = IngestCalibrationGate(
        db_path=db, source_kind="mock", calibration=Calibration(), calibration_file_sha256=FILE_SHA
    )
    with SqliteStore(
        db, rules=QualityRules.from_yaml(QUALITY_RULES_PATH), clock=SimulatedClock(1)
    ) as held:
        gate.open(held, SimulatedClock(1))
        try:
            code = main(
                [
                    "--source",
                    "mock",
                    "--speed",
                    "1000",
                    "--max-samples",
                    "1",
                    "--db",
                    str(db),
                    "--calibration",
                    str(CALIBRATION_PATH),
                    "--scenarios",
                    str(SCENARIOS_PATH),
                    "--quality-rules",
                    str(QUALITY_RULES_PATH),
                    "--rules",
                    str(CONFIG_DIR / "rules.yaml"),
                    "--notify",
                    str(CONFIG_DIR / "notify.yaml"),
                    "--ai",
                    str(CONFIG_DIR / "ai.yaml"),
                    "--metrics",
                    str(CONFIG_DIR / "metrics.yaml"),
                ]
            )
        finally:
            gate.close()
        assert code == 1
        assert len(rows(held)) == 1, "2つ目の取り込みは記録を書かない"
        assert held.max_reading_ts_ms() is None, "2つ目の取り込みは sample を保存しない"
    logged = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert any(line.get("msg") == "取り込みを起動しない（較正の記録）" for line in logged)


# ---------------------------------------------------------------- replay は読みも書きもしない


def test_replay_neither_reads_nor_writes_the_record(tmp_path, rules):
    db = tmp_path / "replay.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(0)) as broken:
        # 読めば拒否される壊れた表（trigger を外して row_sha256 を壊した）
        record(broken, 1_000, {})
        broken.connection.execute("DROP TRIGGER calibration_activations_no_update")
        broken.connection.execute("UPDATE calibration_activations SET row_sha256 = ?", ("0" * 64,))
        before = broken.connection.execute("SELECT * FROM calibration_activations").fetchall()
    csv_path = tmp_path / "sensors_2026-08-23.csv"
    csv_path.write_text(
        "timestamp,room_temp,room_humidity,front_intake,gpu_intake,gpu_exhaust,top_exhaust,rear_exhaust\n"
        "2026-08-23T20:16:40,24.5,60.0,24.31,23.94,24.0,24.06,24.19\n",
        encoding="utf-8",
    )
    daemon = build(daemon_config(tmp_path, db=db, source="replay", csv=csv_path, bulk=True))
    try:
        assert daemon._calibration_gate is None
        stats = daemon.run()
        after = daemon.store.connection.execute("SELECT * FROM calibration_activations").fetchall()
    finally:
        daemon.store.close()
    assert stats.samples == 1
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert not ingest_lock_path(db).exists(), "replay は取り込みの lock も取らない"


# ---------------------------------------------------------------- §2.5 追記のみ（trigger）


def next_row(store: SqliteStore, ts_ms: int, offsets_c: dict[str, float]) -> CalibrationActivation:
    existing = rows(store)
    return CalibrationActivation.next_after(
        existing[-1] if existing else None,
        ts_ms=ts_ms,
        source_kind="mock",
        offsets=effective_metric_offsets(offsets_c),
        calibrated_at=None,
        calibration_file_sha256=FILE_SHA,
    )


def test_update_and_delete_are_rejected(store):
    record(store, 1_000, {})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute("UPDATE calibration_activations SET ts_ms = 2000")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute("DELETE FROM calibration_activations")
    assert len(rows(store)) == 1


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        pytest.param(
            lambda row, first: row.__class__(**{**row.__dict__, "previous_row_sha256": "0" * 64}),
            "chain",
            id="broken-chain",
        ),
        pytest.param(
            lambda row, first: row.__class__(**{**row.__dict__, "previous_row_sha256": None}),
            "chain",
            id="second-first-row",
        ),
        pytest.param(
            lambda row, first: row.__class__(**{**row.__dict__, "ts_ms": first.ts_ms}),
            "ts_ms must be greater",
            id="same-ts",
        ),
        pytest.param(
            lambda row, first: row.__class__(**{**row.__dict__, "ts_ms": first.ts_ms - 1}),
            "ts_ms must be greater",
            id="ts-goes-back",
        ),
        pytest.param(
            lambda row, first: row.__class__(
                **{
                    **row.__dict__,
                    "offsets_json": first.offsets_json,
                    "offsets_sha256": first.offsets_sha256,
                }
            ),
            "offsets must differ",
            id="same-digest",
        ),
    ],
)
def test_inserts_that_break_the_log_are_rejected(store, mutate, match):
    record(store, 1_000, {})
    (first,) = rows(store)
    with pytest.raises(sqlite3.IntegrityError, match=match):
        store.append_calibration_activation(
            mutate(next_row(store, 2_000, {"front_intake": 0.1}), first)
        )
    assert len(rows(store)) == 1


def test_first_row_must_not_point_to_a_previous_row(store):
    row = next_row(store, 1_000, {})
    with pytest.raises(sqlite3.IntegrityError, match="chain"):
        store.append_calibration_activation(
            row.__class__(**{**row.__dict__, "previous_row_sha256": "0" * 64})
        )


def test_an_explicit_id_before_the_last_row_is_rejected(store):
    record(store, 1_000, {})
    row = next_row(store, 2_000, {"front_intake": 0.1})
    with pytest.raises(sqlite3.IntegrityError, match="id must be greater"):
        store.connection.execute(
            "INSERT INTO calibration_activations (id, ts_ms, source_kind, offsets_json,"
            " offsets_sha256, previous_row_sha256, calibrated_at, calibration_file_sha256,"
            " row_sha256) VALUES (0, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.ts_ms,
                row.source_kind,
                row.offsets_json,
                row.offsets_sha256,
                row.previous_row_sha256,
                row.calibrated_at,
                row.calibration_file_sha256,
                row.row_sha256,
            ),
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("source_kind", "replay"),
        ("offsets_sha256", "A" * 64),
        ("calibration_file_sha256", "abc"),
        ("row_sha256", "g" * 64),
        ("offsets_json", "[1]"),
        ("ts_ms", -1),
        ("ts_ms", "soon"),
    ],
)
def test_check_constraints(store, column, value):
    row = next_row(store, 1_000, {})
    with pytest.raises(sqlite3.IntegrityError):
        store.append_calibration_activation(row.__class__(**{**row.__dict__, column: value}))


# ---------------------------------------------------------------- §2.5 / §2.6 読む側の検証


def three_rows(path: Path) -> Path:
    return history_rows(path, 1_000, 2_000, 3_000)


def without_triggers(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    for name in ("chain", "no_update", "no_delete"):
        conn.execute(f"DROP TRIGGER calibration_activations_{name}")
    return conn


def test_a_valid_history_is_read(tmp_path):
    history = read_calibration_history(three_rows(tmp_path / "prod.db"))
    assert isinstance(history, CalibrationHistory)
    assert [row.ts_ms for row in history.rows] == [1_000, 2_000, 3_000]
    assert history.last is not None and history.last.ts_ms == 3_000


@pytest.mark.parametrize(
    ("tamper", "match"),
    [
        pytest.param(
            "UPDATE calibration_activations SET ts_ms = 2500 WHERE id = 2",
            "row_sha256",
            id="ts-moved-between-neighbours",
        ),
        pytest.param("DELETE FROM calibration_activations WHERE id = 2", "鎖", id="middle-deleted"),
        pytest.param(
            "UPDATE calibration_activations SET calibrated_at = 'x' WHERE id = 1",
            "row_sha256",
            id="field-changed",
        ),
        pytest.param(
            "UPDATE calibration_activations SET offsets_sha256 = (SELECT offsets_sha256 FROM"
            " calibration_activations WHERE id = 1) WHERE id = 3",
            "offsets_sha256",
            id="digest-mismatch",
        ),
        pytest.param(
            "UPDATE calibration_activations SET ts_ms = 1000 WHERE id = 3",
            "row_sha256",
            id="not-monotonic",
        ),
    ],
)
def test_tampered_rows_are_refused(tmp_path, tamper, match):
    path = three_rows(tmp_path / "prod.db")
    conn = without_triggers(path)
    try:
        conn.execute(tamper)
    finally:
        conn.close()
    with pytest.raises(CalibrationHistoryError, match=match):
        read_calibration_history(path)


def _insert_raw(path: Path, **row: object) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO calibration_activations (ts_ms, source_kind, offsets_json, offsets_sha256,"
            " previous_row_sha256, calibrated_at, calibration_file_sha256, row_sha256)"
            " VALUES (:ts_ms, :source_kind, :offsets_json, :offsets_sha256, :previous_row_sha256,"
            " :calibrated_at, :calibration_file_sha256, :row_sha256)",
            row,
        )
    finally:
        conn.close()


def _consistent(offsets_json: str, ts_ms: int = 1_000) -> dict[str, object]:
    digest = hashlib.sha256(offsets_json.encode()).hexdigest()
    return {
        "ts_ms": ts_ms,
        "source_kind": "mock",
        "offsets_json": offsets_json,
        "offsets_sha256": digest,
        "previous_row_sha256": None,
        "calibrated_at": None,
        "calibration_file_sha256": FILE_SHA,
        "row_sha256": activation_row_sha256(
            ts_ms=ts_ms,
            source_kind="mock",
            offsets_sha256=digest,
            previous_row_sha256=None,
            calibrated_at=None,
            calibration_file_sha256=FILE_SHA,
        ),
    }


@pytest.mark.parametrize(
    ("offsets_json", "match"),
    [
        pytest.param('{"air.front_intake": 0.5}\n', "canonical", id="spaces"),
        pytest.param('{"air.front_intake":0.5}', "canonical", id="no-trailing-newline"),
        pytest.param('{"b":0.5,"a":0.5}\n', "canonical", id="unsorted"),
        pytest.param('{"air.front_intake":1}\n', "float", id="integer-value"),
        pytest.param('{"air.front_intake":0.50}\n', "canonical", id="not-repr"),
    ],
)
def test_non_canonical_offsets_json_is_refused(tmp_path, rules, offsets_json, match):
    path = tmp_path / "prod.db"
    SqliteStore(path, rules=rules, clock=SimulatedClock(0)).close()
    _insert_raw(path, **_consistent(offsets_json))
    with pytest.raises(CalibrationHistoryError, match=match):
        read_calibration_history(path)


def test_reading_is_read_only_and_does_not_advance_the_schema(tmp_path):
    """migration を当てない。表の無い DB は拒否し、schema_version も表も変わらない（0099 §2.6）。"""
    legacy = tmp_path / "legacy.db"
    directory = tmp_path / "m10"
    directory.mkdir()
    for migration in migrations.discover():
        if migration.version <= 10:
            shutil.copy(migration.path, directory / migration.path.name)
    conn = sqlite3.connect(legacy, isolation_level=None)
    try:
        migrations.apply_pending(conn, now_ms=0, directory=directory)
    finally:
        conn.close()
    before = legacy.read_bytes()

    with pytest.raises(CalibrationHistoryError, match="migration 0011 の前"):
        read_calibration_history(legacy)

    assert legacy.read_bytes() == before
    conn = sqlite3.connect(legacy)
    try:
        assert migrations.current_version(conn) == 10
    finally:
        conn.close()


def test_reading_a_current_db_does_not_write(tmp_path):
    path = three_rows(tmp_path / "prod.db")
    before = path.read_bytes()
    read_calibration_history(path)
    assert path.read_bytes() == before


def test_a_missing_db_is_refused_and_not_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(CalibrationHistoryError, match="無い"):
        read_calibration_history(path)
    assert not path.exists()


def test_history_cannot_be_made_outside_the_reader():
    with pytest.raises(TypeError, match="read_calibration_history"):
        CalibrationHistory(object(), ())


def test_ingest_refuses_to_start_on_a_tampered_log(tmp_path, rules):
    path = three_rows(tmp_path / "prod.db")
    conn = without_triggers(path)
    try:
        conn.execute("DELETE FROM calibration_activations WHERE id = 2")
    finally:
        conn.close()
    with SqliteStore(path, rules=rules, clock=SimulatedClock(0)) as db:
        with pytest.raises(CalibrationActivationRefused, match="壊れている"):
            record(db, 10_000, {"front_intake": 9.0})
        assert (
            db.connection.execute("SELECT COUNT(*) FROM calibration_activations").fetchone()[0] == 2
        )


# ---------------------------------------------------------------- §2.6 区間の幅


def test_change_points_are_both_ends_of_the_csv_truncation_interval(tmp_path):
    history = read_calibration_history(history_rows(tmp_path / "prod.db", 1_000, 2_999))
    assert TIMESTAMP_RESOLUTION_MS == 1_000, "CSV の時刻は秒の精度（csv_export の書式から導く）"
    assert calibration_change_points(history) == (1_000, 1_000, 2_000, 2_999)


# ---------------------------------------------------------------- 学習の入口（0096 §5 #4）


def test_training_accepts_the_last_row_matching_the_file(tmp_path):
    history = read_calibration_history(history_rows(tmp_path / "prod.db", 1_000, 2_000))
    last = verify_training_calibration(
        history, period_end_ms=5_000, calibration_offsets_c={"front_intake": 1.0}
    )
    assert last.ts_ms == 2_000


def test_training_refuses_a_row_after_the_period(tmp_path):
    history = read_calibration_history(history_rows(tmp_path / "prod.db", 1_000, 6_000))
    with pytest.raises(ValueError, match="より後に較正の変更"):
        verify_training_calibration(
            history, period_end_ms=5_000, calibration_offsets_c={"front_intake": 1.0}
        )


def test_training_refuses_a_file_that_differs_from_the_last_row(tmp_path):
    history = read_calibration_history(history_rows(tmp_path / "prod.db", 1_000, 2_000))
    with pytest.raises(ValueError, match="最後の行と一致しない"):
        verify_training_calibration(
            history,
            period_end_ms=5_000,
            calibration_offsets_c={"front_intake": math.nextafter(1.0, 2.0)},
        )


def test_training_refuses_an_empty_history(tmp_path, rules):
    path = tmp_path / "prod.db"
    SqliteStore(path, rules=rules, clock=SimulatedClock(0)).close()
    with pytest.raises(ValueError, match="記録が無い"):
        verify_training_calibration(
            read_calibration_history(path), period_end_ms=5_000, calibration_offsets_c={}
        )


# ---------------------------------------------------------------- migration 0011


def test_migration_is_append_only_and_idempotent(tmp_path, rules):
    path = tmp_path / "v10.db"
    directory = tmp_path / "m10"
    directory.mkdir()
    for migration in migrations.discover():
        if migration.version <= 10:
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
    finally:
        conn.close()

    with SqliteStore(path, rules=rules, clock=SimulatedClock(5_000)) as store:
        latest = store.latest(at_ms=2_000)
        for table, content in before.items():
            after = [tuple(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
            assert after == [tuple(row) for row in content], table
        assert rows(store) == (), "過去の履歴を埋めない（0099 §2.8）"
        versions = store.connection.execute(
            "SELECT version, applied_ms FROM schema_version"
        ).fetchall()
    assert latest["air.room"].value == 25.0
    assert [tuple(row) for row in versions][-1] == (11, 5_000)

    with SqliteStore(path, rules=rules, clock=SimulatedClock(9_000)) as again:
        assert [
            tuple(row)
            for row in again.connection.execute("SELECT version, applied_ms FROM schema_version")
        ] == [tuple(row) for row in versions], "冪等（再適用しない）"
        assert again.latest(at_ms=2_000) == latest


def test_reading_opens_the_db_in_read_only_mode(tmp_path, monkeypatch):
    """SQLite の URI ``mode=ro`` で開く（読む側が本番の DB に書けない。0099 §2.6）。"""
    path = three_rows(tmp_path / "prod.db")
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append(database)
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("coldaisle.store.calibration_history.sqlite3.connect", spy)
    read_calibration_history(path)
    assert len(opened) == 1
    assert opened[0].startswith("file:") and opened[0].endswith("?mode=ro")


def test_a_symlink_to_the_same_db_shares_the_lock(tmp_path, rules):
    """同じ DB を別の path（symlink）で指しても同じ lock になる（PR #240 の Codex の指摘）。"""
    real = tmp_path / "data" / "prod.db"
    real.parent.mkdir()
    alias = tmp_path / "alias.db"
    clock = ManualClock(1_000)
    with SqliteStore(real, rules=rules, clock=clock) as one:
        alias.symlink_to(real)
        first = IngestCalibrationGate(
            db_path=real,
            source_kind="mock",
            calibration=Calibration(),
            calibration_file_sha256=FILE_SHA,
        )
        second = IngestCalibrationGate(
            db_path=alias,
            source_kind="mock",
            calibration=Calibration(offsets_c={"front_intake": 0.1}),
            calibration_file_sha256=FILE_SHA,
        )
        first.open(one, clock)
        clock.now = 2_000
        try:
            with (
                SqliteStore(alias, rules=rules, clock=clock) as two,
                pytest.raises(CalibrationActivationRefused, match="lock"),
            ):
                second.open(two, clock)
        finally:
            first.close()
        assert len(rows(one)) == 1
    assert ingest_lock_path(alias) == ingest_lock_path(real)
