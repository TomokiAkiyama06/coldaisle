"""回転数の掃引と応答の行列の解析（#75 / 決定記録 0153 §2.11）。

DB は一時ディレクトリに既存のマイグレーションで作り、合成の trace と行を直接入れてから
`.backup` の写しにする（実機不要）。時刻は `T0` からの ms で書く。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import sqlite3
import statistics
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

from coldaisle import airflow_characterization as ac
from coldaisle.clock import SimulatedClock
from coldaisle.control.config import FanProfile
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.calibration_history import CalibrationActivation
from conftest import CONFIG_DIR

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "coldaisle"
SHIPPED_CONFIG = CONFIG_DIR / "airflow-characterization.yaml"
T0 = 1_787_616_000_000
"""2026-08-25T09:00+09:00。"""
S = 1000
DAY = 86_400 * S
ZONES = ("front", "rear", "top")
HASHES = {"fan_hardware": "a" * 64, "safety": "b" * 64, "policy": "c" * 64, "air_balance": "d" * 64}
FAND_RUN = "0" * 31 + "1"
OFFSETS = {"air.room": 0.1}
OFFSETS_SHA = CalibrationActivation.next_after(
    None,
    ts_ms=0,
    source_kind="serial",
    offsets=OFFSETS,
    calibrated_at=None,
    calibration_file_sha256="e" * 64,
).offsets_sha256
LEVELS = (0.35, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00)
LABELS = ("R", "F+", "R", "F-", "R", "Rr+", "R", "Rr-", "R", "T+", "R", "T-", "R", "All+", "R")
SWEEP_STEP_MS = 95 * S
MATRIX_STEP_MS = 200 * S


def config_dict() -> dict[str, Any]:
    """出荷の設定に、未定の分解能だけを仮の値で埋めたもの（テストの中だけ）。"""
    loaded: dict[str, Any] = yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    for key in ("cpu_package_c", "board_connector_c"):
        loaded["resolution"][key] = {"value": 1.0, "status": "provisional", "basis": "テスト"}
    loaded["resolution"]["fan_rpm"] = {"value": 10.0, "status": "provisional", "basis": "テスト"}
    return loaded


def make_config(data: dict[str, Any] | None = None) -> ac.AirflowCharacterizationConfig:
    return ac.AirflowCharacterizationConfig.from_bytes(
        yaml.safe_dump(data or config_dict(), allow_unicode=True).encode()
    )


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


ZoneFn = Callable[[dict[str, Any], str, int], dict[str, Any]]
ValueFn = Callable[[int], float | None]


class Bench:
    """合成の DB（trace と readings）と manifest を作る小道具。"""

    def __init__(self, tmp_path: Path, rules: QualityRules) -> None:
        self.tmp_path = tmp_path
        self.rules = rules
        self.live = tmp_path / "live.db"
        SqliteStore(self.live, rules=rules, clock=SimulatedClock(T0)).close()
        self.conn = sqlite3.connect(self.live, isolation_level=None)
        self.seq = 1
        self.tick_id = 100

    # ---------------------------------------------------------------- trace

    def tick(
        self,
        ts: int,
        *,
        commanded: dict[str, float],
        command_id: int | None,
        mode: str = "manual",
        safety: str = "normal",
        run_id: str = FAND_RUN,
        period: int = S,
        hashes: dict[str, str] | None = None,
        zones: dict[str, dict[str, Any]] | None = None,
        faults: list[Any] | None = None,
        skip_tick_id: bool = False,
        skip_seq: bool = False,
    ) -> None:
        if skip_tick_id:
            self.tick_id += 1
        if skip_seq:
            self.seq += 1
        hashes = hashes or HASHES
        body_zones: dict[str, Any] = {}
        for zone in ZONES:
            item = {
                "effective": commanded[zone],
                "applied": commanded[zone],
                "rpm": 1000,
                "write_ok": True,
                "readback_ok": True,
            }
            item.update((zones or {}).get(zone, {}))
            record: dict[str, Any] = {
                "demand": {"effective": item["effective"]},
                "hardware": {
                    "pwm_raw": 128,
                    "rpm": item["rpm"],
                    "write_ok": item["write_ok"],
                    "readback_ok": item["readback_ok"],
                },
            }
            if item["applied"] is not None:
                record["applied_demand"] = item["applied"]
            body_zones[zone] = record
        body = {
            "schema_version": 16,
            "tick_id": self.tick_id,
            "ts_ms": ts,
            "run_id": run_id,
            "state": {"operating_mode": mode, "safety_state": safety},
            "mode_command": {"command_id": command_id},
            "runtime": {
                "tick_period_ms": period,
                "config": {f"{name}_sha256": value for name, value in hashes.items()},
            },
            "zones": body_zones,
            "faults": faults or [],
        }
        self.conn.execute(
            "INSERT INTO control_traces (seq, ts_ms, tick_id, schema_version, trace_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.seq, ts, self.tick_id, 16, json.dumps(body)),
        )
        self.seq += 1
        self.tick_id += 1

    def drive(
        self,
        steps: list[dict[str, Any]],
        *,
        zone_fn: ZoneFn | None = None,
        period: int = S,
        tick_fn: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
    ) -> None:
        """段の区間に `period` ごとの tick を入れる。前後に AUTO の tick を 1 つずつ足す。"""
        first, last = steps[0], steps[-1]
        idle = dict.fromkeys(ZONES, 0.6)
        self.tick(first["start_ms"] - period, commanded=idle, command_id=None, mode="auto")
        for step in steps:
            for ts in range(step["start_ms"], step["end_ms"], period):
                zones = {zone: zone_fn(step, zone, ts) for zone in ZONES} if zone_fn else None
                extra = tick_fn(step, ts) if tick_fn else {}
                self.tick(
                    ts,
                    commanded=step["commanded"],
                    command_id=step["command_id"],
                    period=period,
                    zones=zones,
                    **extra,
                )
        self.tick(last["end_ms"], commanded=idle, command_id=None, mode="auto")

    # ---------------------------------------------------------------- readings

    def reading(self, metric: str, ts: int, value: float | None, quality: str = "ok") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, ?)",
            (metric, ts, value, quality),
        )

    def series(
        self, metric: str, start: int, end: int, value: float | ValueFn, step: int = 2500
    ) -> None:
        for ts in range(start, end, step):
            self.reading(metric, ts, value(ts) if callable(value) else value)

    def calibration(self, ts: int, offsets: dict[str, float] | None = None) -> None:
        previous = store_previous(self.live)
        if previous is not None and previous.offsets == (offsets or OFFSETS):
            return  # 同じ写像は記録されない（0099）。前の行がそのまま有効
        with SqliteStore(self.live, rules=self.rules, clock=SimulatedClock(T0)) as store:
            store.append_calibration_activation(
                CalibrationActivation.next_after(
                    previous,
                    ts_ms=ts,
                    source_kind="serial",
                    offsets=offsets or OFFSETS,
                    calibrated_at=None,
                    calibration_file_sha256="e" * 64,
                )
            )

    # ---------------------------------------------------------------- 写しと manifest

    def snapshot(self, name: str = "copy.db") -> Path:
        path = self.tmp_path / name
        destination = sqlite3.connect(path)
        self.conn.backup(destination)
        destination.close()
        return path

    def manifest(self, document: dict[str, Any], name: str) -> Path:
        path = self.tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        return path


def store_previous(path: Path) -> CalibrationActivation | None:
    from coldaisle.store.calibration_history import SELECT_ROWS, verify_activation_rows

    conn = sqlite3.connect(path)
    try:
        rows = verify_activation_rows(conn.execute(SELECT_ROWS).fetchall())
    finally:
        conn.close()
    return rows[-1] if rows else None


@pytest.fixture
def bench(tmp_path: Path, rules: QualityRules) -> Bench:
    return Bench(tmp_path, rules)


# ---------------------------------------------------------------------- 回転数の掃引


def sweep_steps(
    zone: str,
    repetition: int,
    start: int,
    *,
    command_id: int,
    levels: Iterable[float] = LEVELS,
    down: Iterable[float] | None = None,
) -> list[dict[str, Any]]:
    """上りで `levels`、続けて下りで `down`（既定は最後の段を除いた逆順）。"""
    ups = list(levels)
    downs = list(reversed(ups[:-1])) if down is None else list(down)
    steps: list[dict[str, Any]] = []
    at = start
    for direction, items in (("up", ups), ("down", downs)):
        for level in items:
            commanded = dict.fromkeys(ZONES, 0.6)
            commanded[zone] = level
            steps.append(
                {
                    "zone": zone,
                    "repetition": repetition,
                    "direction": direction,
                    "level": level,
                    "commanded": commanded,
                    "command_id": command_id,
                    "lease_s": 400,
                    "start_ms": at,
                    "end_ms": at + SWEEP_STEP_MS,
                }
            )
            command_id += 1
            at += SWEEP_STEP_MS
    return steps


def rpm_of(level: float) -> int:
    return round(400 + 1600 * level)


def steady_rpm(step: dict[str, Any], zone: str, ts: int) -> dict[str, Any]:
    return {"rpm": rpm_of(step["commanded"][zone])}


def idle_readings(bench: Bench, start: int, end: int) -> None:
    bench.series("cpu.package", start - 300 * S, end, 40.0)
    bench.series("gpu.0.core", start - 300 * S, end, 35.0)
    bench.series("power.gpu.0", start - 300 * S, end, 20.0)


def manifest_doc(
    kind: str,
    run_id: str,
    steps: list[dict[str, Any]],
    db: Path,
    *,
    hashes: dict[str, str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "kind": kind,
        "control_config_sha256": hashes or HASHES,
        "db_copy": {"file": db.name, "sha256": sha(db), "created_at_ms": steps[-1]["end_ms"]},
        "steps": steps,
    }
    document.update(extra)
    return document


def sweep_runs(
    bench: Bench,
    *,
    days: tuple[int, int, int] = (0, 0, 1),
    zone_fn: ZoneFn = steady_rpm,
    mutate: Callable[[int, list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> list[Path]:
    """Front の 3 回の掃引（各回が 1 つの run）。manifest のパスを返す。"""
    all_steps: list[list[dict[str, Any]]] = []
    for repetition, day in enumerate(days, start=1):
        start = T0 + day * DAY + repetition * 3600 * S
        steps = sweep_steps("front", repetition, start, command_id=repetition * 100)
        if mutate is not None:
            steps = mutate(repetition, steps)
        bench.drive(steps, zone_fn=zone_fn)
        idle_readings(bench, steps[0]["start_ms"], steps[-1]["end_ms"] + 10 * S)
        all_steps.append(steps)
    db = bench.snapshot()
    return [
        bench.manifest(manifest_doc("rpm_sweep", f"rpm-front-{index}", steps, db), f"rpm{index}")
        for index, steps in enumerate(all_steps, start=1)
    ]


def build(paths: list[Path], config: ac.AirflowCharacterizationConfig | None = None) -> Any:
    runs = [ac.load_manifest(path) for path in paths]
    return ac.build(runs, config or make_config(), "0" * 64)


def test_sweep_candidate_table_passes_fan_profile_validation(bench: Bench) -> None:
    report = build(sweep_runs(bench))
    front = report["rpm_sweep"]["zones"]["front"]
    assert front["status"] == "ok", front["reasons"]
    candidate = front["candidate"]
    assert [p["demand"] for p in candidate["pwm_to_rpm"]] == list(LEVELS)
    assert [p["rpm"] for p in candidate["pwm_to_rpm"]] == [rpm_of(level) for level in LEVELS]
    assert candidate["airflow_index"][-1] == 1.0
    assert candidate["airflow_index_basis"] == ac.SUBSTITUTE_BASIS
    # 観測の最大 2000 + 5 % → 100 RPM へ切り上げ
    assert candidate["maximum_rpm"] == 2100
    FanProfile.model_validate(
        {
            "startup_demand": 0.35,
            "minimum_stable_demand": 0.35,
            "maximum_rpm": candidate["maximum_rpm"],
            "pwm_to_rpm": candidate["pwm_to_rpm"],
            "airflow_index": candidate["airflow_index"],
        }
    )
    level = front["levels"][0]
    assert level["observations"] == 6 and level["days"] == 2
    assert level["response_ms_up"] == [1500] * 3


def test_sweep_spread_between_directions_withholds_the_level(bench: Bench) -> None:
    def hysteresis(step: dict[str, Any], zone: str, ts: int) -> dict[str, Any]:
        rpm = rpm_of(step["commanded"][zone])
        if zone == "front" and step["direction"] == "down" and step["level"] == 0.5:
            rpm = round(rpm * 1.05)
        return {"rpm": rpm}

    report = build(sweep_runs(bench, zone_fn=hysteresis))
    front = report["rpm_sweep"]["zones"]["front"]
    level = next(row for row in front["levels"] if row["level"] == 0.5)
    assert level["emitted"] is False
    assert level["reason"] == "rpm_spread"
    assert level["hysteresis_up_minus_down"] < 0
    assert front["candidate"] is None
    assert "level_missing" in front["reasons"]


def test_sweep_one_direction_only_is_not_emitted(bench: Bench) -> None:
    def no_down_050(repetition: int, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [s for s in steps if not (s["direction"] == "down" and s["level"] == 0.5)]

    report = build(sweep_runs(bench, mutate=no_down_050))
    front = report["rpm_sweep"]["zones"]["front"]
    level = next(row for row in front["levels"] if row["level"] == 0.5)
    assert level["emitted"] is False and level["reason"] == "insufficient_repetitions"
    assert front["candidate"] is None


def test_sweep_single_day_is_not_emitted(bench: Bench) -> None:
    report = build(sweep_runs(bench, days=(0, 0, 0)))
    front = report["rpm_sweep"]["zones"]["front"]
    assert all(row["reason"] == "insufficient_days" for row in front["levels"])
    assert front["candidate"] is None


def test_sweep_without_the_full_speed_level_emits_no_table(bench: Bench) -> None:
    def drop_top(repetition: int, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [s for s in steps if s["level"] != 1.0]

    report = build(sweep_runs(bench, mutate=drop_top))
    front = report["rpm_sweep"]["zones"]["front"]
    top = next(row for row in front["levels"] if row["level"] == 1.0)
    assert top["emitted"] is False
    assert front["candidate"] is None and "level_missing" in front["reasons"]
    # 0.90 は両方向にあり出せる。一部の段だけを出しても表は作らない
    assert next(row for row in front["levels"] if row["level"] == 0.9)["emitted"] is True


def test_sweep_null_rpm_ticks_are_rpm_coverage_even_when_writes_are_ok(bench: Bench) -> None:
    def sparse(step: dict[str, Any], zone: str, ts: int) -> dict[str, Any]:
        rpm: int | None = rpm_of(step["commanded"][zone])
        if zone == "front" and step["level"] == 0.6 and (ts // S) % 5 != 0:
            rpm = None
        return {"rpm": rpm}

    report = build(sweep_runs(bench, zone_fn=sparse))
    reasons = {step["reason"] for step in report["rpm_sweep"]["steps"] if step["level"] == 0.6}
    assert reasons == {"rpm_coverage"}


def test_sweep_floor_bound_step_is_discarded(bench: Bench) -> None:
    def floored(step: dict[str, Any], zone: str, ts: int) -> dict[str, Any]:
        effective = step["commanded"][zone]
        if zone == "front" and step["level"] == 0.35:
            effective = 0.45
        return {"rpm": rpm_of(effective), "effective": effective, "applied": effective}

    report = build(sweep_runs(bench, zone_fn=floored))
    reasons = {step["reason"] for step in report["rpm_sweep"]["steps"] if step["level"] == 0.35}
    assert reasons == {"floor_bound"}


def test_sweep_floor_inside_the_window_is_discarded(bench: Bench) -> None:
    def brief_floor(step: dict[str, Any], zone: str, ts: int) -> dict[str, Any]:
        effective = step["commanded"][zone]
        if zone == "top" and step["level"] == 0.4 and ts - step["start_ms"] == 60 * S:
            effective = 0.7
        return {"rpm": 1000, "effective": effective, "applied": effective}

    report = build(sweep_runs(bench, zone_fn=brief_floor))
    reasons = {step["reason"] for step in report["rpm_sweep"]["steps"] if step["level"] == 0.4}
    assert reasons == {"floor_bound"}


def test_idle_condition_is_checked_with_the_bound(bench: Bench) -> None:
    paths = sweep_runs(bench)
    report = build(paths)
    assert report["rpm_sweep"]["zones"]["front"]["status"] == "ok"
    data = config_dict()
    data["idle"]["gpu_power_w"]["value"] = 20.0  # 窓の平均 20 W は「未満」を満たさない
    report = build(paths, make_config(data))
    assert {step["reason"] for step in report["rpm_sweep"]["steps"]} == {"condition_mismatch"}


# ---------------------------------------------------------------------- 窓の覆い


def test_trace_coverage_rejects_gaps_run_changes_and_null_applied(bench: Bench) -> None:
    def tick_fn(step: dict[str, Any], ts: int) -> dict[str, Any]:
        offset = ts - step["start_ms"]
        if step["level"] == 0.4 and step["direction"] == "up" and offset == 60 * S:
            return {"skip_tick_id": True}
        if step["level"] == 0.45 and step["direction"] == "up" and offset >= 60 * S:
            return {"run_id": "f" * 32}
        return {}

    def zone_fn(step: dict[str, Any], zone: str, ts: int) -> dict[str, Any]:
        item = steady_rpm(step, zone, ts)
        if step["level"] == 0.5 and step["direction"] == "up" and ts - step["start_ms"] == 50 * S:
            item["applied"] = None
        return item

    steps = sweep_steps("front", 1, T0, command_id=1)
    bench.drive(steps, zone_fn=zone_fn, tick_fn=tick_fn)
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    db = bench.snapshot()
    report = build([bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")])
    by_level = {
        (step["level"], step["direction"]): step["reason"] for step in report["rpm_sweep"]["steps"]
    }
    assert by_level[(0.4, "up")] == "trace_coverage"
    assert by_level[(0.45, "up")] == "trace_coverage"
    assert by_level[(0.5, "up")] == "trace_coverage"
    assert by_level[(0.6, "up")] is None


def test_seq_gaps_with_continuous_tick_ids_are_kept(bench: Bench) -> None:
    def tick_fn(step: dict[str, Any], ts: int) -> dict[str, Any]:
        return {"skip_seq": (ts // S) % 7 == 0}

    steps = sweep_steps("front", 1, T0, command_id=1)
    bench.drive(steps, zone_fn=steady_rpm, tick_fn=tick_fn)
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    db = bench.snapshot()
    report = build([bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")])
    assert {step["reason"] for step in report["rpm_sweep"]["steps"]} == {None}


def test_tick_period_other_than_one_second_sets_the_count(bench: Bench) -> None:
    steps = sweep_steps("front", 1, T0, command_id=1)
    bench.drive(steps, zone_fn=steady_rpm, period=2 * S)
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    db = bench.snapshot()
    report = build([bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")])
    assert {step["reason"] for step in report["rpm_sweep"]["steps"]} == {None}


def test_missing_boundary_tick_is_trace_coverage(bench: Bench) -> None:
    steps = sweep_steps("front", 1, T0, command_id=1, levels=(0.35, 0.40), down=())
    bench.drive(steps, zone_fn=steady_rpm)
    # 0.35 の窓の終わり（start + 90 s）の後の tick を消す
    end = steps[0]["start_ms"] + 90 * S
    bench.conn.execute(
        "DELETE FROM control_traces WHERE ts_ms >= ? AND ts_ms < ?", (end, end + 3 * S)
    )
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    db = bench.snapshot()
    report = build([bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")])
    assert report["rpm_sweep"]["steps"][0]["reason"] == "trace_coverage"


def test_stale_and_suspect_rows_are_telemetry_coverage(bench: Bench) -> None:
    steps = sweep_steps("front", 1, T0, command_id=1, levels=(0.35, 0.40), down=())
    bench.drive(steps, zone_fn=steady_rpm)
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    bench.reading("cpu.package", steps[0]["start_ms"] + 50 * S, 40.0, quality="stale")
    bench.reading("gpu.0.core", steps[1]["start_ms"] + 50 * S, 35.0, quality="suspect")
    db = bench.snapshot()
    report = build([bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")])
    assert [step["reason"] for step in report["rpm_sweep"]["steps"]] == [
        "telemetry_coverage",
        "telemetry_coverage",
    ]


def test_config_hash_mismatch_with_the_trace_is_discarded(bench: Bench) -> None:
    steps = sweep_steps("front", 1, T0, command_id=1, levels=(0.35, 0.40), down=())
    bench.drive(steps, zone_fn=steady_rpm)
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    db = bench.snapshot()
    other = {**HASHES, "safety": "9" * 64}
    path = bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db, hashes=other), "r1")
    report = build([path])
    assert {step["reason"] for step in report["rpm_sweep"]["steps"]} == {"config_mismatch"}


def test_runs_with_different_config_hashes_are_not_combined(bench: Bench) -> None:
    paths = sweep_runs(bench)
    document = yaml.safe_load(paths[2].read_text(encoding="utf-8"))
    document["control_config_sha256"]["policy"] = "8" * 64
    paths[2].write_text(yaml.safe_dump(document), encoding="utf-8")
    report = build(paths)
    front = report["rpm_sweep"]["zones"]["front"]
    assert front["candidate"] is None and "config_mismatch" in front["reasons"]


# ---------------------------------------------------------------------- 窓の統計


def test_u_win_does_not_divide_by_the_number_of_blocks() -> None:
    # 窓より長い相関（ゆっくりした正弦）を持つ合成の窓
    points = [(ts, math.sin(ts / 40_000)) for ts in range(0, 120_000, 2500)]
    stat = ac.window_stat(points, 0, 120_000, 4)
    assert stat is not None
    divided = statistics.stdev(stat.blocks) / math.sqrt(len(stat.blocks))
    assert stat.u_win >= statistics.stdev(stat.blocks) > divided


def test_interpolation_removes_a_linear_drift() -> None:
    def window(start: int, offset: float) -> ac.WindowStat:
        points = [(ts, 0.001 * ts / S + offset) for ts in range(start, start + 120_000, 2500)]
        stat = ac.window_stat(points, start, start + 120_000, 4)
        assert stat is not None
        return stat

    diff = ac.interpolated_difference(
        window(0, 0.0), window(-400_000, 0.0), window(500_000, 0.0), 0
    )
    assert abs(diff.value) < 1e-9
    moved = ac.interpolated_difference(
        window(0, -2.0), window(-400_000, 0.0), window(500_000, 0.0), 0
    )
    assert moved.value == pytest.approx(-2.0)


# ---------------------------------------------------------------------- 応答の行列


def matrix_steps(start: int, *, command_id: int = 1) -> list[dict[str, Any]]:
    config = make_config().response_matrix
    steps = []
    for index, label in enumerate(LABELS):
        steps.append(
            {
                "label": label,
                "commanded": ac.expected_commanded(label, config),
                "command_id": command_id + index,
                "lease_s": 600,
                "start_ms": start + index * MATRIX_STEP_MS,
                "end_ms": start + (index + 1) * MATRIX_STEP_MS,
            }
        )
    return steps


def label_at(steps: list[dict[str, Any]], ts: int) -> str | None:
    for step in steps:
        if step["start_ms"] <= ts < step["end_ms"]:
            return str(step["label"])
    return None


Effects = dict[str, Callable[[str | None, int], float]]


def matrix_readings(
    bench: Bench, steps: list[dict[str, Any]], effects: Effects | None = None
) -> None:
    start = steps[0]["start_ms"] - 300 * S
    end = steps[-1]["end_ms"] + 10 * S
    base: dict[str, float] = {
        "air.front_intake": 25.0,
        "air.gpu_intake": 27.0,
        "air.gpu_exhaust": 40.0,
        "air.top_exhaust": 33.0,
        "air.rear_exhaust": 31.0,
        "air.room": 24.0,
        "air.room_humidity": 40.0,
        "gpu.0.core": 60.0,
        "gpu.0.hotspot": 70.0,
        "cpu.package": 50.0,
        "board.connector_12v2x6": 35.0,
        "power.cpu.package": 100.0,
        "power.gpu.0": 300.0,
        "fan.aio_pump.rpm": 2000.0,
        "fan.vrm.rpm": 1500.0,
        "gpu.0.fan_speed": 40.0,
    }
    effects = effects or {}
    for metric, value in base.items():
        effect = effects.get(metric)

        def at(ts: int, value: float = value, effect: Any = effect) -> float:
            return value + (effect(label_at(steps, ts), ts) if effect else 0.0)

        bench.series(metric, start, end, at)
    bench.series("gpu.0.throttle.sw_power_cap", start, end, 0.0)


def matrix_run(
    bench: Bench,
    run_id: str,
    start: int,
    *,
    effects: Effects | None = None,
    zone_fn: ZoneFn | None = None,
    load: dict[str, Any] | None = None,
    calibration: bool = True,
) -> Path:
    steps = matrix_steps(start, command_id=1 + (start // S) % 1000)
    if calibration:
        bench.calibration(start - 3600 * S)
    bench.drive(steps, zone_fn=zone_fn)
    matrix_readings(bench, steps, effects)
    db = bench.snapshot(f"{run_id}.db")
    return bench.manifest(
        manifest_doc(
            "response_matrix",
            run_id,
            steps,
            db,
            calibration_offsets_sha256=OFFSETS_SHA,
            load=load
            or {"kind": "gpu", "gpu_power_limit_w": 300.0, "cpu_workers": None, "pilot": "予備"},
        ),
        run_id,
    )


def pair(report: dict[str, Any], run: int, label: str) -> dict[str, Any]:
    found: dict[str, Any] = next(
        item for item in report["response_matrix"]["runs"][run]["pairs"] if item["label"] == label
    )
    return found


def drift_and_effect(label: str | None, ts: int) -> float:
    return 0.5 * (ts - T0) / 1_000_000 + (-2.0 if label == "F+" else 0.0)


def test_matrix_response_removes_drift_and_reports_two_runs(bench: Bench) -> None:
    first = matrix_run(bench, "m1", T0, effects={"gpu.0.core": drift_and_effect})
    second = matrix_run(bench, "m2", T0 + DAY, effects={"gpu.0.core": drift_and_effect})
    report = build([first, second])
    for run in (0, 1):
        assert report["response_matrix"]["runs"][run]["status"] == "ok"
        f_plus = pair(report, run, "F+")
        assert f_plus["status"] == "ok", f_plus
        assert f_plus["responses"]["gpu.0.core"]["value"] == pytest.approx(-2.0, abs=1e-3)
        assert pair(report, run, "T+")["responses"]["gpu.0.core"]["value"] == pytest.approx(
            0.0, abs=1e-3
        )
    entry = next(
        item
        for item in report["response_matrix"]["aggregate"][0]["entries"]
        if item["label"] == "F+" and item["indicator"] == "gpu.0.core"
    )
    assert entry["status"] == "ok"
    assert entry["mean"] == pytest.approx(-2.0, abs=1e-3)
    assert entry["u_mean"] > 0


def test_power_rise_and_fall_are_both_power_mismatch(bench: Bench) -> None:
    def power(label: str | None, ts: int) -> float:
        return {"F+": 10.0, "F-": -10.0}.get(label or "", 0.0)

    report = build([matrix_run(bench, "m1", T0, effects={"power.gpu.0": power})])
    for label, sign in (("F+", 1), ("F-", -1)):
        item = pair(report, 0, label)
        assert item["status"] == "discarded" and item["reason"] == "power_mismatch"
        assert item["power_delta"]["power.gpu.0"]["value"] == pytest.approx(10.0 * sign, abs=1e-6)
        assert item["throttle"]["gpu.0.throttle.sw_power_cap"] is False
        assert item["responses"] == {}
    assert pair(report, 0, "T+")["status"] == "ok"
    assert "air.gpu_intake" in pair(report, 0, "T+")["responses"]
    markdown = ac.as_markdown(report)
    assert "throttle" in markdown and "| F+ | discarded | power_mismatch（pair） |" in markdown
    assert "段ごとの記録" in markdown and "ポンプ rpm" in markdown and "| 2000 |" in markdown


def test_stale_outlier_in_the_settle_span_is_telemetry_coverage(bench: Bench) -> None:
    path = matrix_run(bench, "m1", T0)
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["steps"]
    rr_minus = next(step for step in steps if step["label"] == "Rr-")
    # 落ち着きの区間に必ず入るよう、段の中に 5 s ごとの stale の外れ値を入れる
    for ts in range(rr_minus["start_ms"], rr_minus["end_ms"], 5000):
        bench.reading("air.gpu_exhaust", ts, 90.0, quality="stale")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["db_copy"]["sha256"] = sha(bench.snapshot("m1.db"))
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    item = pair(build([path]), 0, "Rr-")
    assert item["reason"] == "telemetry_coverage" and item["reason_from"] == "moved"


def test_pump_mismatch_is_condition_mismatch(bench: Bench) -> None:
    def pump(label: str | None, ts: int) -> float:
        return 100.0 if label == "Rr+" else 0.0

    report = build([matrix_run(bench, "m1", T0, effects={"fan.aio_pump.rpm": pump})])
    assert pair(report, 0, "Rr+")["reason"] == "condition_mismatch"


def test_unsettled_step_is_discarded(bench: Bench) -> None:
    def wobble(label: str | None, ts: int) -> float:
        return (1.5 if (ts // 2500) % 2 else -1.5) if label == "T-" else 0.0

    report = build([matrix_run(bench, "m1", T0, effects={"gpu.0.core": wobble})])
    item = pair(report, 0, "T-")
    assert item["reason"] == "unsettled" and item["reason_from"] == "moved"


def test_window_trend_is_discarded(bench: Bench) -> None:
    # 0.25 °C / 120 s の傾き（落ち着きの 180 s の幅 0.375 °C + 境界は 0.5 °C の内側）
    def ramp(label: str | None, ts: int) -> float:
        return 0.25 * ((ts - T0) % MATRIX_STEP_MS) / 120_000 if label == "All+" else 0.0

    report = build([matrix_run(bench, "m1", T0, effects={"air.top_exhaust": ramp})])
    assert pair(report, 0, "All+")["reason"] == "window_trend"


def test_room_drift_discards_the_session(bench: Bench) -> None:
    def room(label: str | None, ts: int) -> float:
        return 2.0 * (ts - T0) / (len(LABELS) * MATRIX_STEP_MS)

    report = build([matrix_run(bench, "m1", T0, effects={"air.room": room})])
    run = report["response_matrix"]["runs"][0]
    assert run["status"] == "discarded" and run["reason"] == "room_drift"
    assert run["pairs"] == []


def test_room_excursion_outside_the_value_windows_discards_the_session(bench: Bench) -> None:
    # 値の窓（各段の 60〜180 s）の外、落ち着きの途中だけで 2 °C 動いて戻る
    def room(label: str | None, ts: int) -> float:
        return 2.0 if label == "Rr+" and (ts - T0) % MATRIX_STEP_MS < 40 * S else 0.0

    run = build([matrix_run(bench, "m1", T0, effects={"air.room": room})])["response_matrix"][
        "runs"
    ][0]
    assert run["reason"] == "room_drift"


def test_an_early_coverage_gap_does_not_hide_unsettled(bench: Bench) -> None:
    def wobble(label: str | None, ts: int) -> float:
        return (1.5 if (ts // 2500) % 2 else -1.5) if label == "T+" else 0.0

    path = matrix_run(bench, "m1", T0, effects={"gpu.0.core": wobble})
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["steps"]
    t_plus = next(step for step in steps if step["label"] == "T+")
    # 段の始めの 1 行だけ stale（最初の候補の区間は覆いを欠く）
    bench.reading("air.room", t_plus["start_ms"] + 2500, 24.0, quality="stale")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["db_copy"]["sha256"] = sha(bench.snapshot("m1.db"))
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    item = pair(build([path]), 0, "T+")
    assert item["reason"] == "unsettled"


def test_matrix_floor_bound_baseline_discards_both_neighbours(bench: Bench) -> None:
    def floored(step: dict[str, Any], zone: str, ts: int) -> dict[str, Any]:
        effective = step["commanded"][zone]
        if step["label"] == "T-" and zone == "top":
            effective = 0.5
        return {"effective": effective, "applied": effective}

    report = build([matrix_run(bench, "m1", T0, zone_fn=floored)])
    assert pair(report, 0, "T-")["reason"] == "floor_bound"


def test_calibration_activation_inside_the_run_is_calibration_changed(bench: Bench) -> None:
    path = matrix_run(bench, "m1", T0)
    bench.calibration(T0 + 10 * S, {"air.room": 0.2})
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    db = bench.snapshot("m1.db")
    document["db_copy"]["sha256"] = sha(db)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    run = build([path])["response_matrix"]["runs"][0]
    assert run["status"] == "discarded" and run["reason"] == "calibration_changed"


def test_calibration_digest_mismatch_and_missing_row(bench: Bench) -> None:
    path = matrix_run(bench, "m1", T0, calibration=False)
    assert build([path])["response_matrix"]["runs"][0]["reason"] == "calibration_changed"
    bench.calibration(T0 - 7200 * S, {"air.room": 0.3})
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    db = bench.snapshot("m1.db")
    document["db_copy"]["sha256"] = sha(db)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    run = build([path])["response_matrix"]["runs"][0]
    assert run["reason"] == "calibration_changed"
    assert run["calibration_offsets_sha256"] != OFFSETS_SHA


def test_matrix_manifest_must_bracket_moved_steps(bench: Bench) -> None:
    steps = matrix_steps(T0)
    del steps[2]  # F+ の後の R を外す
    for index, step in enumerate(steps):
        step["start_ms"] = T0 + index * MATRIX_STEP_MS
        step["end_ms"] = step["start_ms"] + MATRIX_STEP_MS
    db = bench.snapshot()
    path = bench.manifest(
        manifest_doc(
            "response_matrix",
            "m1",
            steps,
            db,
            calibration_offsets_sha256=OFFSETS_SHA,
            load={"kind": "gpu", "gpu_power_limit_w": 300.0, "cpu_workers": None, "pilot": "x"},
        ),
        "m1",
    )
    with pytest.raises(ValueError, match="並び"):
        ac.load_manifest(path)


def test_matrix_manifest_rejects_a_repeated_moved_step(bench: Bench) -> None:
    steps = matrix_steps(T0)
    steps[3].update(label="F+", commanded=steps[1]["commanded"])  # F- の代わりにもう一度 F+
    db = bench.snapshot()
    document = manifest_doc(
        "response_matrix",
        "m1",
        steps,
        db,
        calibration_offsets_sha256=OFFSETS_SHA,
        load={"kind": "gpu", "gpu_power_limit_w": 300.0, "cpu_workers": None, "pilot": "x"},
    )
    with pytest.raises(ValueError, match="並び"):
        ac.load_manifest(bench.manifest(document, "m1"))


# ---------------------------------------------------------------------- DB の写し・設定


def test_shipped_config_refuses_while_resolutions_are_undetermined(bench: Bench) -> None:
    config = ac.AirflowCharacterizationConfig.from_bytes(SHIPPED_CONFIG.read_bytes())
    assert config.undetermined() == [
        "resolution.cpu_package_c",
        "resolution.board_connector_c",
        "resolution.fan_rpm",
    ]
    paths = sweep_runs(bench)
    with pytest.raises(ac.AnalysisError, match="fan_rpm"):
        build(paths, config)
    assert ac.main(["--run", str(paths[0]), "--config", str(SHIPPED_CONFIG)]) == 1


def test_undetermined_needs_null_and_null_needs_undetermined() -> None:
    data = config_dict()
    data["resolution"]["fan_rpm"] = {"value": None, "status": "provisional", "basis": "x"}
    with pytest.raises(ValueError, match="undetermined"):
        make_config(data)
    data["resolution"]["fan_rpm"] = {"value": 5.0, "status": "undetermined", "basis": "x"}
    with pytest.raises(ValueError, match="undetermined"):
        make_config(data)


def test_config_rejects_missing_and_unknown_keys() -> None:
    data = config_dict()
    del data["approval"]
    with pytest.raises(ValueError):
        make_config(data)
    data = config_dict()
    data["rpm_sweep"]["extra"] = {"value": 1, "status": "provisional", "basis": "x"}
    with pytest.raises(ValueError):
        make_config(data)
    data = config_dict()
    data["max_room_drift_c"] = {"value": "1.0", "status": "provisional", "basis": "x"}
    with pytest.raises(ValueError):
        make_config(data)


def write_test_config(tmp_path: Path) -> Path:
    path = tmp_path / "airflow.yaml"
    path.write_text(yaml.safe_dump(config_dict(), allow_unicode=True), encoding="utf-8")
    return path


def test_db_copy_sha_mismatch_is_refused(bench: Bench, tmp_path: Path) -> None:
    paths = sweep_runs(bench)
    document = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    document["db_copy"]["sha256"] = "0" * 64
    paths[0].write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ac.AnalysisError, match="SHA-256"):
        build(paths)
    assert ac.main(["--run", str(paths[0]), "--config", str(write_test_config(tmp_path))]) == 1


def test_non_empty_wal_is_refused(bench: Bench) -> None:
    paths = sweep_runs(bench)
    (bench.tmp_path / "copy.db-wal").write_bytes(b"x")
    with pytest.raises(ac.AnalysisError, match="静止"):
        build(paths)


def test_copy_that_does_not_cover_the_run_is_refused(bench: Bench) -> None:
    steps = sweep_steps("front", 1, T0, command_id=1)
    bench.drive(steps[:3], zone_fn=steady_rpm)
    idle_readings(bench, T0, steps[2]["end_ms"])
    db = bench.snapshot()
    path = bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")
    with pytest.raises(ac.AnalysisError, match="覆わない"):
        build([path])


def test_db_copy_file_must_be_relative() -> None:
    with pytest.raises(ValueError, match="相対"):
        ac.DbCopy.model_validate({"file": "/abs/copy.db", "sha256": "0" * 64, "created_at_ms": 0})
    with pytest.raises(ValueError, match="相対"):
        ac.DbCopy.model_validate({"file": "../copy.db", "sha256": "0" * 64, "created_at_ms": 0})


def test_cli_writes_nothing_but_the_report(bench: Bench, tmp_path: Path) -> None:
    paths = sweep_runs(bench)
    config = write_test_config(tmp_path)
    db = bench.tmp_path / "copy.db"
    before = {path: path.read_bytes() for path in [db, config, *paths]}
    out = tmp_path / "out" / "report.md"
    argv = ["--config", str(config), "--format", "markdown", "--out", str(out)]
    for path in paths:
        argv += ["--run", str(path)]
    assert ac.main(argv) == 0
    assert {path: path.read_bytes() for path in before} == before
    assert not (bench.tmp_path / "copy.db-wal").exists()
    text = out.read_text(encoding="utf-8")
    assert "風量を測った値ではない" in text
    assert str(tmp_path) not in text and str(Path.home()) not in text
    # 同じ入力からは同じ bytes
    assert ac.main(argv) == 0 and out.read_text(encoding="utf-8") == text
    json_out = tmp_path / "report.json"
    assert ac.main([*argv[:-4], "--out", str(json_out)]) == 0
    assert str(tmp_path) not in json_out.read_text(encoding="utf-8")


def test_cli_refuses_out_on_an_input(bench: Bench, tmp_path: Path) -> None:
    paths = sweep_runs(bench)
    db = bench.tmp_path / "copy.db"
    before = db.read_bytes()
    argv = ["--run", str(paths[0]), "--config", str(write_test_config(tmp_path))]
    assert ac.main([*argv, "--out", str(db)]) == 2
    assert ac.main([*argv, "--out", str(paths[0])]) == 2
    assert db.read_bytes() == before


# ---------------------------------------------------------------------- import の向き


def _imported_modules(path: Path) -> Iterable[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def test_control_ai_api_and_server_never_import_this_module() -> None:
    paths = [
        *(SRC / "control").rglob("*.py"),
        *(SRC / "ai").rglob("*.py"),
        *(SRC / "api").rglob("*.py"),
        SRC / "server.py",
        SRC / "control_daemon.py",
    ]
    offenders = [
        str(path.relative_to(SRC))
        for path in paths
        for name in _imported_modules(path)
        if "airflow_characterization" in name
    ]
    assert offenders == []


def test_module_has_no_write_paths() -> None:
    """DB へ書く SQL・設定への書き込みを持たない（出力は `--out` の 1 つだけ）。"""
    text = (SRC / "airflow_characterization.py").read_text(encoding="utf-8")
    for word in ("INSERT", "UPDATE ", "DELETE", "CREATE ", "DROP ", "commit(", "safe_dump"):
        assert word not in text
    assert text.count("write_text(") == 1


def test_safety_fault_outside_the_window_discards_the_step(bench: Bench) -> None:
    def tick_fn(step: dict[str, Any], ts: int) -> dict[str, Any]:
        # 0.50 の段の始めの 5 s だけ DEGRADED（値の窓は 30 s 後から）
        if step["level"] == 0.5 and step["direction"] == "up" and ts - step["start_ms"] < 5 * S:
            return {"safety": "degraded"}
        return {}

    steps = sweep_steps("front", 1, T0, command_id=1)
    bench.drive(steps, zone_fn=steady_rpm, tick_fn=tick_fn)
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    db = bench.snapshot()
    report = build([bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")])
    by_level = {
        (step["level"], step["direction"]): step["reason"] for step in report["rpm_sweep"]["steps"]
    }
    assert by_level[(0.5, "up")] == "safety_fault"
    assert by_level[(0.6, "up")] is None


def test_matrix_fault_before_settling_discards_the_step(bench: Bench) -> None:
    def tick_fn(step: dict[str, Any], ts: int) -> dict[str, Any]:
        if step["label"] == "Rr-" and ts - step["start_ms"] < 3 * S:
            return {"faults": [{"code": "telemetry_loss"}]}
        return {}

    steps = matrix_steps(T0)
    bench.calibration(T0 - 3600 * S)
    bench.drive(steps, tick_fn=tick_fn)
    matrix_readings(bench, steps)
    db = bench.snapshot("m1.db")
    document = manifest_doc(
        "response_matrix",
        "m1",
        steps,
        db,
        calibration_offsets_sha256=OFFSETS_SHA,
        load={"kind": "gpu", "gpu_power_limit_w": 300.0, "cpu_workers": None, "pilot": "x"},
    )
    item = pair(build([bench.manifest(document, "m1")]), 0, "Rr-")
    assert item["reason"] == "safety_fault" and item["reason_from"] == "moved"


def test_partially_covered_throttle_flag_is_unknown(bench: Bench) -> None:
    path = matrix_run(bench, "m1", T0)
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["steps"]
    f_plus = next(step for step in steps if step["label"] == "F+")
    for ts in range(f_plus["start_ms"], f_plus["end_ms"], 2500):
        if (ts // 2500) % 3 == 0:
            bench.reading("gpu.0.throttle.sw_power_cap", ts, None, quality="stale")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["db_copy"]["sha256"] = sha(bench.snapshot("m1.db"))
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    report = build([path])
    assert pair(report, 0, "F+")["throttle"]["gpu.0.throttle.sw_power_cap"] is None
    assert pair(report, 0, "T+")["throttle"]["gpu.0.throttle.sw_power_cap"] is False
    assert "不明: " in ac.as_markdown(report)


def test_digest_is_checked_before_opening(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = sweep_runs(bench)
    document = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    document["db_copy"]["sha256"] = "0" * 64
    paths[0].write_text(yaml.safe_dump(document), encoding="utf-8")

    def refuse(path: Path) -> None:
        raise AssertionError("照合の前に DB を開いた")

    monkeypatch.setattr(ac, "CopyDatabase", refuse)
    with pytest.raises(ac.AnalysisError, match="SHA-256"):
        build(paths[:1])


def test_fault_on_the_next_step_first_tick_is_not_blamed_on_this_step(bench: Bench) -> None:
    def tick_fn(step: dict[str, Any], ts: int) -> dict[str, Any]:
        if step["level"] == 0.45 and step["direction"] == "up" and ts == step["start_ms"]:
            return {"safety": "degraded"}
        return {}

    steps = sweep_steps("front", 1, T0, command_id=1)
    bench.drive(steps, zone_fn=steady_rpm, tick_fn=tick_fn)
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    db = bench.snapshot()
    report = build([bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")])
    by_level = {
        (step["level"], step["direction"]): step["reason"] for step in report["rpm_sweep"]["steps"]
    }
    assert by_level[(0.4, "up")] is None
    assert by_level[(0.45, "up")] == "safety_fault"


def test_uncovered_record_only_metric_discards_the_window(bench: Bench) -> None:
    path = matrix_run(bench, "m1", T0)
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["steps"]
    t_plus = next(step for step in steps if step["label"] == "T+")
    for ts in range(t_plus["start_ms"], t_plus["end_ms"], 2500):
        bench.reading("fan.vrm.rpm", ts, 1500.0, quality="suspect")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["db_copy"]["sha256"] = sha(bench.snapshot("m1.db"))
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    item = pair(build([path]), 0, "T+")
    assert item["reason"] == "telemetry_coverage" and item["reason_from"] == "moved"


def test_surplus_matrix_repetitions_are_not_averaged(bench: Bench) -> None:
    paths = [matrix_run(bench, f"m{day}", T0 + day * DAY) for day in range(3)]
    entries = build(paths)["response_matrix"]["aggregate"][0]["entries"]
    assert {entry["reason"] for entry in entries} == {"too_many_repetitions"}
    assert all(entry["mean"] is None for entry in entries)


def test_config_change_before_the_window_discards_the_step(bench: Bench) -> None:
    other = {**HASHES, "policy": "7" * 64}

    def tick_fn(step: dict[str, Any], ts: int) -> dict[str, Any]:
        if step["level"] == 0.6 and step["direction"] == "up" and ts - step["start_ms"] < 10 * S:
            return {"hashes": other}
        return {}

    steps = sweep_steps("front", 1, T0, command_id=1)
    bench.drive(steps, zone_fn=steady_rpm, tick_fn=tick_fn)
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    db = bench.snapshot()
    report = build([bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")])
    by_level = {
        (step["level"], step["direction"]): step["reason"] for step in report["rpm_sweep"]["steps"]
    }
    assert by_level[(0.6, "up")] == "config_mismatch"
    assert by_level[(0.7, "up")] is None


def test_surplus_sweep_repetitions_emit_no_table(bench: Bench) -> None:
    report = build(sweep_runs(bench, days=(0, 0, 1, 1)))
    front = report["rpm_sweep"]["zones"]["front"]
    assert front["candidate"] is None and "too_many_repetitions" in front["reasons"]


def test_value_window_failure_keeps_searching_later_windows(bench: Bench) -> None:
    path = matrix_run(bench, "m1", T0)
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["steps"]
    rr_plus = next(step for step in steps if step["label"] == "Rr+")
    # 最初の値の窓（61〜181 s）の始めにだけ hotspot の stale を 1 行（落ち着きには使わない）
    bench.reading("gpu.0.hotspot", rr_plus["start_ms"] + 62_500, 70.0, quality="stale")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["db_copy"]["sha256"] = sha(bench.snapshot("m1.db"))
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    report = build([path])
    item = pair(report, 0, "Rr+")
    assert item["status"] == "ok", item
    step = next(s for s in report["response_matrix"]["runs"][0]["steps"] if s["label"] == "Rr+")
    assert step["window_start"] > ac._iso(rr_plus["start_ms"] + 62_500, make_config().zone)


def test_null_rpm_during_the_ramp_restarts_the_response_time(bench: Bench) -> None:
    def gap(step: dict[str, Any], zone: str, ts: int) -> dict[str, Any]:
        item = steady_rpm(step, zone, ts)
        offset = ts - step["start_ms"]
        if zone == "front" and step["level"] == 0.4 and 2 * S <= offset < 10 * S:
            item["rpm"] = None
        return item

    steps = sweep_steps("front", 1, T0, command_id=1, levels=(0.35, 0.40), down=())
    bench.drive(steps, zone_fn=gap)
    idle_readings(bench, T0, steps[-1]["end_ms"] + 10 * S)
    db = bench.snapshot()
    report = build([bench.manifest(manifest_doc("rpm_sweep", "r1", steps, db), "r1")])
    # 帯に入るのは null の後（10 s）。tach の更新 1500 ms を足す
    assert report["rpm_sweep"]["steps"][1]["response_ms"] == 10 * S + 1500
