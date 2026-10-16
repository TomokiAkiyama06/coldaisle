"""負荷の状態ごとの分布の CLI（#50 / 決定記録 0151 §2.11）。

DB は一時ディレクトリに既存のマイグレーションで作り、合成の行を直接入れる（実機不要）。
時刻は 1 分の境目に揃えた `T0` からの ms で書く。
"""

from __future__ import annotations

import ast
import json
import math
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

from coldaisle import load_distribution as ld
from coldaisle.clock import SimulatedClock
from coldaisle.metrics import MetricCatalog
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.calibration_history import CalibrationActivation
from conftest import CONFIG_DIR

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "coldaisle"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHIPPED_CONFIG = CONFIG_DIR / "load-distribution.yaml"
METRICS_PATH = CONFIG_DIR / "metrics.yaml"
T0 = 1_787_616_000_000
"""1 分の境目（`T0 % 60000 == 0`）。"""
S = 1000
MIN = 60 * S
ZONES = ("front", "rear", "top")

TEST_OVERRIDES: dict[str, Any] = {
    "settle_ms": 60 * S,
    "fan_min_samples_per_segment": 4,
    "min_samples": 3,
    "metrics": ["d.intake_rise", "air.room"],
}
"""短い合成データで確かめるための値。形は出荷の設定のまま。"""


def config_dict(**overrides: Any) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    loaded.update(TEST_OVERRIDES)
    loaded.update(overrides)
    return loaded


def make_config(**overrides: Any) -> ld.LoadDistributionConfig:
    return ld.LoadDistributionConfig.from_bytes(yaml.safe_dump(config_dict(**overrides)).encode())


def pwm_pct(raw: int) -> float:
    return raw / 255 * 100


class Fixture:
    """合成の DB を作り、`build()` を呼ぶ小道具。"""

    def __init__(self, tmp_path: Path, rules: QualityRules) -> None:
        self.tmp_path = tmp_path
        self.db = tmp_path / "coldaisle.db"
        SqliteStore(self.db, rules=rules, clock=SimulatedClock(T0)).close()
        self.conn = sqlite3.connect(self.db, isolation_level=None)

    # ---------------------------------------------------------------- 行を入れる

    def reading(self, metric: str, ts: int, value: float | None, quality: str = "ok") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO readings (metric, ts_ms, value, quality) VALUES (?, ?, ?, ?)",
            (metric, ts, value, quality),
        )

    def series(
        self,
        metric: str,
        start: int,
        end: int,
        value: float | Callable[[int], float],
        step: int = 2500,
    ) -> None:
        for ts in range(start, end, step):
            self.reading(metric, ts, value(ts) if callable(value) else value)

    def power(self, start: int, end: int, cpu: float, gpu: float) -> None:
        self.series("power.cpu.package", start, end, cpu)
        self.series("power.gpu.0", start, end, gpu)

    def air(self, start: int, end: int, room: float = 25.0, intake: float = 26.0) -> None:
        self.series("air.room", start, end, room)
        self.series("air.front_intake", start, end, intake)

    def fan(self, start: int, end: int, raw: int | Callable[[int], int], step: int = 2500) -> None:
        for ts in range(start, end, step):
            value = raw(ts) if callable(raw) else raw
            for zone in ZONES:
                self.reading(f"fan.{zone}.pwm", ts, pwm_pct(value))

    def trace(
        self,
        seq: int,
        ts: int,
        *,
        raw: int = 128,
        mode: str = "auto",
        write_ok: bool = True,
        readback_ok: bool = True,
        hardware: bool = True,
        body: dict[str, Any] | None = None,
    ) -> None:
        if body is None:
            zones = {
                zone: (
                    {
                        "hardware": {
                            "pwm_raw": raw,
                            "write_ok": write_ok,
                            "readback_ok": readback_ok,
                        }
                    }
                    if hardware
                    else {}
                )
                for zone in ZONES
            }
            body = {"state": {"operating_mode": mode}, "zones": zones}
        self.conn.execute(
            "INSERT INTO control_traces (seq, ts_ms, tick_id, schema_version, trace_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (seq, ts, seq, 16, json.dumps(body)),
        )

    def traces(self, seq: int, start: int, end: int, step: int = S, **kwargs: Any) -> int:
        """`[start, end)` に 1 s ごとの trace を足し、次の `seq` を返す。"""
        for ts in range(start, end, step):
            self.trace(seq, ts, **kwargs)
            seq += 1
        return seq

    def fand_run(self, start: int, end: int, raw: int = 128, seq: int = 1, **kwargs: Any) -> int:
        """fand が hwmon を制御した運転（trace と物理の PWM が合う）。次の `seq` を返す。"""
        self.fan(start, end, raw)
        return self.traces(seq, start, end, raw=raw, **kwargs)

    def close_segments(self, seq: int, at: int) -> None:
        """末尾の区間を閉じる（後の運転の trace = `s1 + 1` を足す）。"""
        self.trace(seq, at)

    def rollup(self, metric: str, bucket: int, row_count: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO readings_1m (metric, bucket_ms, ok_value_count, row_count) "
            "VALUES (?, ?, ?, ?)",
            (metric, bucket, row_count, row_count),
        )

    def periods(self, document: dict[str, Any]) -> Path:
        path = self.tmp_path / "periods.yaml"
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        return path

    # ---------------------------------------------------------------- 集計

    def build(
        self,
        start: int,
        end: int,
        *,
        config: ld.LoadDistributionConfig | None = None,
        periods: dict[str, Any] | None = None,
        group_by: str = "state",
        include_override: bool = False,
        allow_partial: bool = False,
    ) -> dict[str, Any]:
        config = config or make_config()
        catalog, catalog_sha = MetricCatalog.from_yaml_with_sha256(METRICS_PATH)
        config.validate_against(catalog)
        parsed = (
            ld.Periods.empty()
            if periods is None
            else ld.Periods.from_bytes(self.periods(periods).read_bytes(), config.zone)
        )
        inputs = ld.Inputs(config, "0" * 64, catalog, catalog_sha, parsed)
        with ld.LoadDistributionDatabase(self.db) as database, database.read_snapshot():
            return ld.build(
                database,
                inputs,
                start_ms=start,
                end_ms=end,
                group_by=group_by,  # type: ignore[arg-type]
                include_override=include_override,
                allow_partial=allow_partial,
            )


@pytest.fixture
def fx(tmp_path: Path, rules: QualityRules) -> Fixture:
    return Fixture(tmp_path, rules)


def iso(ts: int) -> str:
    return ld._iso(ts, make_config())


def bios_everywhere(start: int = T0 - 60 * MIN, end: int = T0 + 600 * MIN) -> dict[str, Any]:
    return {"bios_confirmed": [{"start": iso(start), "end": iso(end), "basis": "journal で確認"}]}


def metric(report: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = next(item for item in report["metrics"] if item["metric"] == name)
    return found


def groups(report: dict[str, Any], name: str = "air.room") -> dict[tuple[str, str, str], Any]:
    return {
        (group["cooling"], group["label"], group["period"]): group
        for group in metric(report, name)["groups"]
    }


def transitions(report: dict[str, Any], name: str = "air.room") -> dict[tuple[str, str, str], Any]:
    return {
        (group["cooling"], group["label"], group["period"]): group
        for group in metric(report, name)["transitions"]
    }


def excluded(report: dict[str, Any], name: str = "air.room") -> dict[str, Any]:
    found: dict[str, Any] = report["provenance"]["excluded"][name]
    return found


IDLE = ("bios", "cpu:idle/gpu:idle", "all")


def steady_bios(fx: Fixture, start: int, end: int, *, cpu: float = 10, gpu: float = 5) -> None:
    fx.power(start, end, cpu, gpu)
    fx.air(start, end)


# ---------------------------------------------------------------- 読み取り専用


def test_the_db_bytes_and_schema_do_not_change(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 10 * MIN)
    fx.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    fx.conn.close()
    before = fx.db.read_bytes()
    report = fx.build(T0, T0 + 10 * MIN, periods=bios_everywhere())
    assert fx.db.read_bytes() == before
    assert report["provenance"]["db_schema_version"] == len(ld.migrations.discover())


def test_a_db_of_another_schema_version_is_not_opened(fx: Fixture) -> None:
    fx.conn.execute("INSERT INTO schema_version (version, applied_ms) VALUES (999, 0)")
    with pytest.raises(ld.SoakDatabaseError):
        ld.LoadDistributionDatabase(fx.db)
    code = ld.main(["--db", str(fx.db), "--start", iso(T0), "--end", iso(T0 + MIN)])
    assert code != 0


# ---------------------------------------------------------------- 帯


def test_the_lower_bound_of_a_band_belongs_to_the_upper_band() -> None:
    config = make_config()
    assert config.axes.cpu.band_of(49.999) == "idle"
    assert config.axes.cpu.band_of(50) == "light"
    assert config.axes.gpu.band_of(350) == "full"
    assert config.axes.gpu.band_of(-1) is None


def test_an_axis_without_enough_ok_rows_in_the_window_is_unknown(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 10 * MIN
    fx.air(start, end)
    fx.series("power.gpu.0", start, end, 5)
    # CPU は 10 s ごとだけ（30 s の窓に 3 件。必要は 30000 / 2500 × 0.5 = 6 件）
    fx.series("power.cpu.package", start, end, 10, step=10 * S)
    report = fx.build(T0, end, periods=bios_everywhere())
    assert groups(report) == {}
    assert excluded(report)["unknown"] == 10 * MIN // 2500


# ---------------------------------------------------------------- 落ち着き


def test_settle_marks_the_first_settle_ms_after_a_change_as_transition(fx: Fixture) -> None:
    change = T0 + 5 * MIN
    fx.air(T0 - 10 * MIN, T0 + 20 * MIN)
    fx.series("power.gpu.0", T0 - 10 * MIN, T0 + 20 * MIN, 5)
    fx.series(
        "power.cpu.package", T0 - 10 * MIN, T0 + 20 * MIN, lambda ts: 10 if ts < change else 60
    )
    report = fx.build(T0, T0 + 20 * MIN, periods=bios_everywhere())
    light = ("bios", "cpu:light/gpu:idle", "all")
    # 30 s の窓（12 件）の平均が 50 W に届くのは 60 W の行が 10 件になった時刻
    first_light = change + 9 * 2500
    assert transitions(report)[light]["stats"]["n"] == 60 * S // 2500
    assert groups(report)[light]["stats"]["n"] == (T0 + 20 * MIN - first_light - 60 * S) // 2500
    assert groups(report)[IDLE]["stats"]["n"] == (first_light - T0) // 2500


def test_a_gap_longer_than_max_gap_restarts_the_settle_timer(fx: Fixture) -> None:
    fx.power(T0 - 10 * MIN, T0 + 20 * MIN, 10, 5)
    fx.air(T0 - 10 * MIN, T0 + 5 * MIN)
    fx.air(T0 + 5 * MIN + 20 * S, T0 + 20 * MIN)  # 20 s の欠け
    report = fx.build(T0, T0 + 20 * MIN, periods=bios_everywhere())
    assert transitions(report)[IDLE]["stats"]["n"] == 60 * S // 2500
    assert groups(report)[IDLE]["segments"]["count"] == 2


def test_cooling_changes_restart_settle_and_split_segments(fx: Fixture) -> None:
    """fand → undetermined → fand は 2 つの segment。undetermined を挟めば計時もやり直す。"""
    start, end = T0 - 10 * MIN, T0 + 30 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    hole = (T0 + 10 * MIN, T0 + 10 * MIN + 30 * S)
    seq = fx.traces(1, start, end, raw=128)
    fx.fan(start, hole[0], 128)
    fx.fan(hole[0], hole[1], 200)  # この間だけ物理の PWM が合わない
    fx.fan(hole[1], end, 128)
    fx.close_segments(seq, end + 60 * MIN)
    report = fx.build(T0, T0 + 30 * MIN, config=make_config(pwm_mismatch_max_ratio=0.1))
    fand = ("fand", "cpu:idle/gpu:idle", "all")
    assert groups(report)[fand]["segments"]["count"] == 2
    assert transitions(report)[fand]["stats"]["n"] >= 60 * S // 2500
    assert excluded(report)["undetermined_reasons"]["pwm_mismatch"] > 0


def test_a_period_change_splits_the_segment_but_keeps_the_settle_timer(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    periods = {
        "periods": [
            {"name": "ac_on", "start": iso(T0), "end": iso(T0 + 10 * MIN)},
            {"name": "ac_off", "start": iso(T0 + 10 * MIN), "end": iso(T0 + 20 * MIN)},
        ],
        **bios_everywhere(),
    }
    report = fx.build(T0, T0 + 20 * MIN, periods=periods)
    found = groups(report)
    on = ("bios", "cpu:idle/gpu:idle", "ac_on")
    off = ("bios", "cpu:idle/gpu:idle", "ac_off")
    assert found[on]["segments"]["count"] == 1 and found[off]["segments"]["count"] == 1
    assert found[off]["stats"]["n"] == 10 * MIN // 2500  # 切り替わりで transition に戻らない
    assert transitions(report) == {}


# ---------------------------------------------------------------- --periods


def test_periods_are_half_open(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    periods = {
        "periods": [{"name": "p", "start": iso(T0), "end": iso(T0 + 5 * MIN)}],
        **bios_everywhere(),
    }
    report = fx.build(T0, T0 + 20 * MIN, periods=periods)
    assert groups(report)[("bios", "cpu:idle/gpu:idle", "p")]["stats"]["n"] == 5 * MIN // 2500
    assert excluded(report)["outside_periods"] == 15 * MIN // 2500


@pytest.mark.parametrize("kind", ["periods", "exclude", "bios_confirmed"])
def test_overlapping_periods_of_the_same_kind_are_refused(kind: str) -> None:
    extra = {"periods": {"name": "x"}, "exclude": {"reason": "r"}, "bios_confirmed": {"basis": "b"}}
    document = {
        kind: [
            {"start": iso(T0), "end": iso(T0 + 2 * MIN), **extra[kind]},
            {"start": iso(T0 + MIN), "end": iso(T0 + 3 * MIN), **extra[kind]},
        ]
    }
    with pytest.raises(ValueError, match="重なっている"):
        ld.Periods.from_bytes(yaml.safe_dump(document).encode(), make_config().zone)
    touching = {
        kind: [
            {"start": iso(T0), "end": iso(T0 + 2 * MIN), **extra[kind]},
            {"start": iso(T0 + 2 * MIN), "end": iso(T0 + 3 * MIN), **extra[kind]},
        ]
    }
    ld.Periods.from_bytes(yaml.safe_dump(touching).encode(), make_config().zone)


def test_bad_periods_are_refused() -> None:
    zone = make_config().zone
    empty = {"periods": [{"name": "x", "start": iso(T0), "end": iso(T0)}]}
    with pytest.raises(ValueError, match="end > start"):
        ld.Periods.from_bytes(yaml.safe_dump(empty).encode(), zone)
    no_basis = {"bios_confirmed": [{"start": iso(T0), "end": iso(T0 + MIN)}]}
    with pytest.raises(ValueError):
        ld.Periods.from_bytes(yaml.safe_dump(no_basis).encode(), zone)


def test_exclude_wins_over_bios_confirmed_and_named_periods_coexist(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    periods = {
        "periods": [{"name": "ac_off", "start": iso(T0), "end": iso(T0 + 20 * MIN)}],
        "exclude": [{"start": iso(T0 + 5 * MIN), "end": iso(T0 + 6 * MIN), "reason": "時計"}],
        **bios_everywhere(),
    }
    report = fx.build(T0, T0 + 20 * MIN, periods=periods)
    assert excluded(report)["excluded_by_periods"] == MIN // 2500
    assert ("bios", "cpu:idle/gpu:idle", "ac_off") in groups(report)
    assert report["provenance"]["periods"]["exclude"][0]["reason"] == "時計"
    assert report["provenance"]["periods"]["bios_confirmed"][0]["basis"] == "journal で確認"
    assert report["provenance"]["periods_sha256"] is not None


# ---------------------------------------------------------------- 生データの覆い


def test_buckets_deleted_by_retention_fail_by_default(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    fx.rollup("air.room", T0 + MIN, 30)  # 生データは 24 行しかない
    fx.rollup("air.room", T0 + 2 * MIN, 24)  # 一致するバケット
    with pytest.raises(ld.LoadDistributionError, match="消えている"):
        fx.build(T0, T0 + 20 * MIN, periods=bios_everywhere())
    report = fx.build(T0, T0 + 20 * MIN, periods=bios_everywhere(), allow_partial=True)
    provenance = report["provenance"]
    assert provenance["incomplete"] is True
    assert provenance["covered_period"]["start_ms"] == T0 + 2 * MIN
    assert provenance["deleted_buckets"]["air.room"]["count"] == 1


def test_buckets_after_the_rollup_are_accepted(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    fx.rollup("air.room", T0, 24)  # これより後のバケットは readings_1m に行が無い
    report = fx.build(T0, T0 + 20 * MIN, periods=bios_everywhere())
    assert report["provenance"]["incomplete"] is False


def test_the_cli_writes_nothing_when_buckets_are_deleted(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    fx.rollup("air.room", T0 + MIN, 30)
    out = fx.tmp_path / "out" / "report.json"
    code = ld.main(
        ["--db", str(fx.db), "--start", iso(T0), "--end", iso(T0 + 20 * MIN), "--out", str(out)]
    )
    assert code != 0
    assert not out.exists()


# ---------------------------------------------------------------- d.* の対と品質


def test_derived_pairs(fx: Fixture) -> None:
    fx.power(T0 - 10 * MIN, T0 + 10 * MIN, 10, 5)
    for index in range(4):
        ts = T0 + 2 * MIN + index * 10 * S
        fx.reading("air.front_intake", ts, 27.0)
    fx.reading("air.room", T0 + 2 * MIN, 25.0)  # 同じ時刻
    fx.reading("air.room", T0 + 2 * MIN + 6 * S, 24.0)  # 2 件目に 4 s 前の行
    fx.reading("air.room", T0 + 2 * MIN + 25 * S, 99.0, "suspect")  # 4 件目: 使わない
    report = fx.build(T0, T0 + 10 * MIN, periods=bios_everywhere(), config=make_config(settle_ms=0))
    derived = groups(report, "d.intake_rise")[IDLE]["stats"]
    assert derived["n"] == 2
    assert (derived["min"], derived["max"]) == (2.0, 3.0)
    assert excluded(report, "d.intake_rise")["unpaired"] == 2  # 3 件目は 14 s 前・4 件目は suspect


def test_non_ok_rows_are_counted_not_distributed(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 10 * MIN)
    fx.reading("air.room", T0 + MIN, -127.0, "suspect")
    fx.reading("air.room", T0 + 2 * MIN, None, "missing")
    fx.reading("air.room", T0 + 3 * MIN, 25.0, "stale")
    fx.reading("air.room", T0 + 4 * MIN, None, "ok")
    report = fx.build(T0, T0 + 10 * MIN, periods=bios_everywhere())
    stats = groups(report)[IDLE]["stats"]
    assert stats["min"] == 25.0
    quality = {
        (item["cooling"], item["label"], item["period"]): item
        for item in metric(report, "air.room")["quality"]
    }[IDLE]
    assert (quality["suspect"], quality["missing"], quality["stale"], quality["null"]) == (
        1,
        1,
        1,
        1,
    )


def test_calibration_changes_in_the_period_are_listed(fx: Fixture, rules: QualityRules) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 10 * MIN)
    with SqliteStore(fx.db, rules=rules, clock=SimulatedClock(T0)) as store:
        first = CalibrationActivation.next_after(
            None,
            ts_ms=T0 - 30 * MIN,
            source_kind="serial",
            offsets={"air.room": 0.1},
            calibrated_at=None,
            calibration_file_sha256="a" * 64,
        )
        store.append_calibration_activation(first)
        second = CalibrationActivation.next_after(
            first,
            ts_ms=T0 + 5 * MIN,
            source_kind="serial",
            offsets={"air.room": 0.2},
            calibrated_at=None,
            calibration_file_sha256="b" * 64,
        )
        store.append_calibration_activation(second)
    report = fx.build(T0, T0 + 10 * MIN, periods=bios_everywhere())
    provenance = report["provenance"]
    assert provenance["calibration_changed"] is True
    assert [change["ts_ms"] for change in provenance["calibration_changes"]] == [T0 + 5 * MIN]
    assert provenance["calibration_changes"][0]["offsets_sha256"] == second.offsets_sha256


# ---------------------------------------------------------------- 統計


def test_quantiles_follow_type_7_and_small_groups_get_null() -> None:
    config = make_config(quantiles=[0.25, 0.5, 0.99], min_samples=3)
    stats = ld.summarize([1.0, 2.0, 3.0, 4.0, 10.0], config)
    # type 7: h = (n - 1) q。0.25 → x[1] = 2、0.99 → x[3] + 0.96 × (10 - 4)
    assert stats["quantiles"] == {"p25": 2.0, "p50": 3.0, "p99": 9.76}
    assert stats["std"] == round(math.sqrt(sum((v - 4) ** 2 for v in (1, 2, 3, 4, 10)) / 5), 3)
    small = ld.summarize([1.0, 2.0], config)
    assert small["quantiles"] == {"p25": None, "p50": None, "p99": None}
    assert small["std"] is None and small["reason"] == "insufficient_samples"


# ---------------------------------------------------------------- cooling


def fand_scenario(fx: Fixture, start: int, end: int, **kwargs: Any) -> None:
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    seq = fx.fand_run(start, end, **kwargs)
    fx.close_segments(seq, end + 60 * MIN)


FAND = ("fand", "cpu:idle/gpu:idle", "all")


def test_matching_physical_pwm_is_fand(fx: Fixture) -> None:
    fand_scenario(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    report = fx.build(T0, T0 + 20 * MIN)
    assert groups(report)[FAND]["stats"]["n"] == 20 * MIN // 2500
    assert report["trace_segments"]["by_status"]["accepted"] == 1


def test_a_simulated_backend_is_undetermined(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    seq = fx.traces(1, start, end, raw=128)
    fx.fan(start, end, 60)  # BIOS が別の duty で回している
    fx.close_segments(seq, end + 60 * MIN)
    report = fx.build(T0, end)
    assert groups(report) == {}
    assert excluded(report)["undetermined_reasons"] == {"pwm_mismatch_ratio": 20 * MIN // 2500}


def test_a_high_mismatch_ratio_rejects_the_whole_segment(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    seq = fx.traces(1, start, end, raw=128)
    fx.fan(start, end, lambda ts: 128 if (ts // 2500) % 5 else 60)  # 20 % が外れる
    fx.close_segments(seq, end + 60 * MIN)
    report = fx.build(T0, end)
    assert FAND not in groups(report)
    assert excluded(report)["undetermined_reasons"]["pwm_mismatch_ratio"] > 0


def test_missing_fan_samples_are_undetermined(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    seq = fx.fand_run(start, T0 + 10 * MIN)
    seq = fx.traces(seq, T0 + 10 * MIN, end)  # 後半は fan.* が無い
    fx.close_segments(seq, end + 60 * MIN)
    report = fx.build(T0, end, config=make_config(fan_min_coverage_ratio=0.3))
    assert excluded(report)["undetermined_reasons"]["fan_sample_missing"] > 0


def test_bios_needs_bios_confirmed_even_with_an_empty_trace_table(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    fx.fan(T0 - 10 * MIN, T0 + 20 * MIN, 60)
    outside = fx.build(T0, T0 + 20 * MIN)
    assert groups(outside) == {}
    assert excluded(outside)["undetermined_reasons"] == {
        "no_trace_outside_bios_confirmed": 20 * MIN // 2500
    }
    inside = fx.build(T0, T0 + 20 * MIN, periods=bios_everywhere())
    assert groups(inside)[IDLE]["stats"]["n"] == 20 * MIN // 2500


def test_a_trace_inside_bios_confirmed_is_a_conflict(fx: Fixture) -> None:
    fand_scenario(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    report = fx.build(T0, T0 + 20 * MIN, periods=bios_everywhere())
    assert ("bios", "cpu:idle/gpu:idle", "all") not in groups(report)
    assert excluded(report)["undetermined_reasons"]["bios_confirmed_conflict"] == 20 * MIN // 2500


def test_manual_ticks_are_fand_override_and_excluded_by_default(fx: Fixture) -> None:
    fand_scenario(fx, T0 - 10 * MIN, T0 + 20 * MIN, mode="manual")
    report = fx.build(T0, T0 + 20 * MIN)
    assert groups(report) == {}
    assert excluded(report)["fand_override"] == 20 * MIN // 2500
    included = fx.build(T0, T0 + 20 * MIN, include_override=True)
    assert ("fand_override", "cpu:idle/gpu:idle", "all") in groups(included)


@pytest.mark.parametrize(
    "kwargs",
    [{"write_ok": False}, {"readback_ok": False}, {"hardware": False}],
    ids=["write_ok", "readback_ok_enable_reverted", "no_hardware"],
)
def test_unverified_writes_never_match(fx: Fixture, kwargs: dict[str, bool]) -> None:
    fand_scenario(fx, T0 - 10 * MIN, T0 + 20 * MIN, **kwargs)
    report = fx.build(T0, T0 + 20 * MIN)
    assert FAND not in groups(report)
    assert set(excluded(report)["undetermined_reasons"]) <= {
        "write_unverified",
        "pwm_mismatch_ratio",
    }


def test_one_unverified_tick_counts_as_a_mismatch_in_the_segment_ratio(fx: Fixture) -> None:
    """flag が false の tick が候補に入ったサンプルは、PWM が合っていても区間の分子に数える。"""
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    fx.fan(start, end, 128)
    seq = 1
    for ts in range(start, end, S):
        fx.trace(seq, ts, raw=128, readback_ok=((ts // S) % 10 != 0))
        seq += 1
    fx.close_segments(seq, end + 60 * MIN)
    report = fx.build(T0, end)
    assert excluded(report)["undetermined_reasons"] == {"pwm_mismatch_ratio": 20 * MIN // 2500}


def test_sparse_fan_samples_fail_the_coverage_check(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    seq = fx.traces(1, start, end, raw=128)
    fx.fan(T0 + 5 * MIN, T0 + 5 * MIN + 1, 128)  # 1 つだけ合うサンプル
    fx.close_segments(seq, end + 60 * MIN)
    report = fx.build(T0, end, config=make_config(fan_min_samples_per_segment=1))
    assert excluded(report)["undetermined_reasons"].get("fan_coverage_low", 0) > 0
    assert report["trace_segments"]["by_status"]["fan_coverage_low"] == 1


# ---------------------------------------------------------------- seq の境目と穴


def test_a_fresh_db_segment_starting_at_seq_1_can_be_confirmed(fx: Fixture) -> None:
    fand_scenario(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    report = fx.build(T0, T0 + 20 * MIN)
    assert report["trace_segments"]["legacy_through_seq"] == 0
    assert FAND in groups(report)


def test_a_segment_whose_previous_seq_is_missing_is_undetermined(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    fx.fan(start, end, 128)
    seq = fx.traces(5, start, end, raw=128)  # seq 1〜4 は消えた
    fx.close_segments(seq, end + 60 * MIN)
    report = fx.build(T0, end)
    assert excluded(report)["undetermined_reasons"] == {
        "segment_start_unconfirmed": 20 * MIN // 2500
    }


def test_legacy_rows_are_undetermined(fx: Fixture) -> None:
    fand_scenario(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    fx.conn.execute("UPDATE control_trace_prune SET legacy_through_seq = 3")
    report = fx.build(T0, T0 + 20 * MIN)
    assert set(excluded(report)["undetermined_reasons"]) == {"legacy_rows"}


def test_a_seq_hole_inside_a_segment_rejects_it(fx: Fixture) -> None:
    fand_scenario(fx, T0 - 10 * MIN, T0 + 20 * MIN)
    fx.conn.execute("DELETE FROM control_traces WHERE seq = 100")
    report = fx.build(T0, T0 + 20 * MIN)
    assert set(excluded(report)["undetermined_reasons"]) == {"seq_gap"}
    assert report["trace_segments"]["seq_gaps"] == 1


def test_the_pruned_middle_row_case_from_the_record(fx: Fixture) -> None:
    """(1, 5000) と (3, 8000) が残り (2, 1000) を消した DB（0151 §2.6a）。"""
    fx.trace(1, T0 + 5 * S)
    fx.trace(3, T0 + 8 * S)
    fx.trace(4, T0 + 60 * MIN)  # seq 3 の区間を DB の末尾にしない
    with ld.LoadDistributionDatabase(fx.db) as database:
        seqs, stamps = database.trace_index()
        timeline = ld.TraceTimeline(seqs, stamps, make_config())
        judge = ld.CoolingJudge(
            database,
            timeline,
            database.prune_state(),
            make_config(),
            ld.Periods.empty(),
            read_from_ms=T0,
            read_to_ms=T0 + 10 * S,
        )
    statuses = {segment.s0: segment.status for segment in judge.relevant_segments}
    assert statuses[1] == "segment_end_unconfirmed"
    assert statuses[3] == "segment_start_unconfirmed"


def test_the_last_segment_is_always_open_until_a_later_trace_exists(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end + 60 * MIN, 10, 5)
    fx.air(start, end + 60 * MIN)  # 圧縮した replay のように後の readings がある
    seq = fx.fand_run(start, end)
    report = fx.build(T0, end)
    assert set(excluded(report)["undetermined_reasons"]) == {"segment_open"}
    fx.close_segments(seq, end + 30 * MIN)
    assert FAND in groups(fx.build(T0, end))


# ---------------------------------------------------------------- 時計の戻り


def test_a_clock_rewind_does_not_cut_the_segment(fx: Fixture) -> None:
    fx.trace(1, T0 + 10 * S)
    fx.trace(2, T0 + 5 * S)  # 時計が戻った
    fx.trace(3, T0 + 6 * S)
    fx.trace(4, T0 + 60 * MIN)
    with ld.LoadDistributionDatabase(fx.db) as database:
        seqs, stamps = database.trace_index()
    timeline = ld.TraceTimeline(seqs, stamps, make_config())
    assert [(segment.s0, segment.s1) for segment in timeline.segments] == [(1, 3), (4, 4)]
    assert timeline.overlaps == [(T0 + 5 * S, T0 + 10 * S)]


def test_wall_clock_overlap_is_ambiguous(fx: Fixture) -> None:
    """10:00:05 → 09:59:50 と戻り、再び 10:00:05 を越える。重なりの時刻は組にしない。"""
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    fx.fan(start, end, 128)
    seq = fx.traces(1, start, T0 + 5 * S + 1, raw=128)
    seq = fx.traces(seq, T0 - 10 * S, end, raw=128)
    fx.close_segments(seq, end + 60 * MIN)
    report = fx.build(T0, end, config=make_config(pwm_mismatch_max_ratio=0.2))
    assert excluded(report)["undetermined_reasons"]["clock_overlap"] > 0
    assert report["provenance"]["clock_overlaps"] == 1
    regime = fx.build(T0, end, group_by="trace-regime")
    assert excluded(regime)["unknown_ambiguous"] > 0
    inside = fx.build(T0, end, periods=bios_everywhere())
    assert excluded(inside)["undetermined_reasons"]["clock_overlap"] > 0
    assert ("bios", "cpu:idle/gpu:idle", "all") not in groups(inside)


# ---------------------------------------------------------------- as-of の突き合わせ


def test_a_ramp_matches_a_recent_candidate_tick(fx: Fixture) -> None:
    """物理の PWM は 2 tick 前の指令。pwm_apply_delay_ms の窓の候補と一致する。"""
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)

    def command(ts: int) -> int:
        return 50 + 3 * ((ts // S) % 50)

    seq = 1
    for ts in range(start, end, S):
        fx.trace(seq, ts, raw=command(ts))
        seq += 1
    fx.close_segments(seq, end + 60 * MIN)
    fx.fan(start, end, lambda ts: command(ts - 2 * S))
    assert FAND in groups(fx.build(T0, end))


def test_a_lag_beyond_the_apply_delay_does_not_match(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)

    def command(ts: int) -> int:
        return 50 + 3 * ((ts // S) % 50)

    seq = 1
    for ts in range(start, end, S):
        fx.trace(seq, ts, raw=command(ts))
        seq += 1
    fx.close_segments(seq, end + 60 * MIN)
    fx.fan(start, end, lambda ts: command(ts - 5 * S))
    assert FAND not in groups(fx.build(T0, end))


def test_as_of_picks_the_largest_seq_and_never_a_future_trace(fx: Fixture) -> None:
    fx.trace(1, T0)
    fx.trace(2, T0)  # 同じ ts_ms
    fx.trace(3, T0 + 2 * S)
    with ld.LoadDistributionDatabase(fx.db) as database:
        seqs, stamps = database.trace_index()
    timeline = ld.TraceTimeline(seqs, stamps, make_config())
    pos = timeline.as_of(T0 + S)
    assert pos is not None and timeline.seqs[pos] == 2
    assert timeline.as_of(T0 - 1) is None
    assert timeline.as_of(T0 + 2 * S + 2001) is None  # trace_max_age_ms を超えて古い


def _judge(fx: Fixture, at: int) -> ld.SampleResult:
    config = make_config()
    with ld.LoadDistributionDatabase(fx.db) as database:
        seqs, stamps = database.trace_index()
        timeline = ld.TraceTimeline(seqs, stamps, config)
        judge = ld.CoolingJudge(
            database,
            timeline,
            database.prune_state(),
            config,
            ld.Periods.empty(),
            read_from_ms=at - MIN,
            read_to_ms=at + MIN,
        )
    return judge._samples[judge._sample_times.index(at)]


def test_zones_matching_different_ticks_do_not_match(fx: Fixture) -> None:
    at = T0 + 10 * S
    fx.trace(1, at - 2 * S, body=_body({"front": 100, "rear": 50, "top": 50}))
    fx.trace(2, at - S, body=_body({"front": 50, "rear": 100, "top": 100}))
    for zone, raw in (("front", 100), ("rear", 100), ("top", 100)):
        fx.reading(f"fan.{zone}.pwm", at, pwm_pct(raw))
    assert _judge(fx, at).kind is ld.Kind.MISMATCH


def test_a_late_sample_matching_an_old_manual_tick_is_override(fx: Fixture) -> None:
    at = T0 + 10 * S
    fx.trace(1, at - 3 * S, mode="manual", raw=100)
    fx.trace(2, at - 2 * S, mode="auto", raw=150)
    fx.trace(3, at - S, mode="auto", raw=150)
    fx.fan(at, at + 1, 100)
    assert _judge(fx, at).kind is ld.Kind.FAND_OVERRIDE


def test_a_sample_matching_manual_and_auto_ticks_is_mixed(fx: Fixture) -> None:
    at = T0 + 10 * S
    fx.trace(1, at - 3 * S, mode="manual", raw=100)
    fx.trace(2, at - S, mode="auto", raw=100)
    fx.fan(at, at + 1, 100)
    assert _judge(fx, at).kind is ld.Kind.MODE_MIXED


def test_zones_at_different_timestamps_are_unaligned(fx: Fixture) -> None:
    at = T0 + 10 * S
    fx.trace(1, at - S, raw=100)
    fx.reading("fan.front.pwm", at, pwm_pct(100))
    fx.reading("fan.rear.pwm", at, pwm_pct(100))
    fx.reading("fan.top.pwm", at + 1, pwm_pct(100))
    assert _judge(fx, at).kind is ld.Kind.UNALIGNED


def _body(raws: dict[str, int]) -> dict[str, Any]:
    return {
        "state": {"operating_mode": "auto"},
        "zones": {
            zone: {"hardware": {"pwm_raw": raw, "write_ok": True, "readback_ok": True}}
            for zone, raw in raws.items()
        },
    }


# ---------------------------------------------------------------- 区間の全体


def test_a_segment_is_judged_as_a_whole_regardless_of_start(fx: Fixture) -> None:
    """--start より前の前半が外れ、後半だけが合う。--start をどこに置いても undetermined。"""
    start, end = T0 - 60 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    seq = fx.traces(1, start, end, raw=128)
    fx.fan(start, T0 - 10 * MIN, 60)
    fx.fan(T0 - 10 * MIN, end, 128)
    fx.close_segments(seq, end + 60 * MIN)
    for begin in (T0, T0 + 5 * MIN):
        report = fx.build(begin, end)
        assert set(excluded(report)["undetermined_reasons"]) == {"pwm_mismatch_ratio"}


def test_mismatches_after_end_reject_the_segment(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    seq = fx.traces(1, start, end + 60 * MIN, raw=128)
    fx.fan(start, end, 128)
    fx.fan(end, end + 60 * MIN, 60)
    fx.close_segments(seq, end + 180 * MIN)
    report = fx.build(T0, end)
    assert set(excluded(report)["undetermined_reasons"]) == {"pwm_mismatch_ratio"}


# ---------------------------------------------------------------- 区間の始めの境目


def test_a_change_before_start_makes_the_first_times_transition(fx: Fixture) -> None:
    fx.air(T0 - 20 * MIN, T0 + 10 * MIN)
    fx.series("power.cpu.package", T0 - 20 * MIN, T0 + 10 * MIN, 10)
    fx.series(
        "power.gpu.0", T0 - 20 * MIN, T0 + 10 * MIN, lambda ts: 5 if ts < T0 - 30 * S else 400
    )
    report = fx.build(T0, T0 + 10 * MIN, periods=bios_everywhere())
    full = ("bios", "cpu:idle/gpu:full", "all")
    assert transitions(report)[full]["stats"]["n"] > 0
    assert report["provenance"]["start_lookback_incomplete"] is False
    assert IDLE not in groups(report)  # さかのぼった時刻は件数に入らない


def test_no_rows_before_start_restart_the_settle_timer(fx: Fixture) -> None:
    fx.power(T0 - 20 * MIN, T0 + 10 * MIN, 10, 5)
    fx.air(T0, T0 + 10 * MIN)
    report = fx.build(T0, T0 + 10 * MIN, periods=bios_everywhere())
    assert transitions(report)[IDLE]["stats"]["n"] == 60 * S // 2500
    assert report["provenance"]["start_lookback_incomplete"] is True


def test_a_long_steady_state_is_steady_right_after_start(fx: Fixture) -> None:
    steady_bios(fx, T0 - 20 * MIN, T0 + 10 * MIN)
    report = fx.build(T0, T0 + 10 * MIN, periods=bios_everywhere())
    assert transitions(report) == {}
    assert groups(report)[IDLE]["stats"]["n"] == 10 * MIN // 2500


# ---------------------------------------------------------------- trace-regime


def regime_body(ts: int, seq: int, regime: str | None) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(
        (FIXTURES / "control_tick_v16.json").read_text(encoding="utf-8")
    )
    body["ts_ms"] = ts
    body["tick_id"] = seq
    if regime is not None:
        body["state"]["workload_regime"] = regime
        body["state"]["regime_confidence"] = 0.9
    return body


def test_trace_regime_uses_the_as_of_tick_only(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 20 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    fx.fan(start, end, 99)  # fixture の pwm_raw は 99
    seq = 1
    change = T0 + 5 * MIN + 500  # 評価時刻（2.5 s ごと）の間で regime が変わる
    for ts in range(start, end, S):
        regime = "idle" if ts < change else "sustained_gpu"
        fx.trace(seq, ts, body=regime_body(ts, seq, regime))
        seq += 1
    fx.trace(seq, end + 60 * MIN, body=regime_body(end + 60 * MIN, seq, None))  # 末尾は読めない
    report = fx.build(T0, end, group_by="trace-regime")
    found = groups(report)
    sustained = transitions(report)[("fand", "sustained_gpu", "all")]
    assert found[("fand", "idle", "all")]["stats"]["n"] == 5 * MIN // 2500 + 1
    assert sustained["stats"]["n"] == 60 * S // 2500
    assert ld.REGIME_NOTE in report["provenance"]["notes"]


def test_trace_regime_unknowns_are_counted_only(fx: Fixture) -> None:
    start, end = T0 - 10 * MIN, T0 + 10 * MIN
    fx.power(start, end, 10, 5)
    fx.air(start, end)
    fx.fan(start, end, 128)
    seq = fx.traces(1, start, T0 + 5 * MIN, raw=128)  # 検証を通らない trace
    fx.close_segments(seq, end + 60 * MIN)
    report = fx.build(T0, end, group_by="trace-regime", periods=bios_everywhere(T0 + 6 * MIN))
    counts = excluded(report)
    # 最後の trace（T0 + 5 min - 1 s）は trace_max_age_ms（2 s）の間 as-of になる
    assert counts["unknown_unreadable"] == 5 * MIN // 2500 + 1
    assert counts["unknown_no_trace"] == 5 * MIN // 2500 - 1
    assert groups(report) == {}


# ---------------------------------------------------------------- 設定


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"metrics": ["d.no_such"]}, "metrics.yaml に無い"),
        (
            {
                "axes": {
                    "cpu": {"metric": "air.room", "bands": [{"name": "a", "lower_w": 0}]},
                    "gpu": {"metric": "power.gpu.0", "bands": [{"name": "a", "lower_w": 0}]},
                }
            },
            "W でない",
        ),
    ],
)
def test_the_config_is_checked_against_metrics_yaml(
    overrides: dict[str, Any], message: str
) -> None:
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    with pytest.raises(ValueError, match=message):
        make_config(**overrides).validate_against(catalog)


def test_bands_must_ascend_and_keys_must_be_complete() -> None:
    axes = config_dict()["axes"]
    axes["gpu"]["bands"] = [{"name": "a", "lower_w": 0}, {"name": "b", "lower_w": 0}]
    with pytest.raises(ValueError, match="昇順"):
        make_config(axes=axes)
    document = config_dict()
    del document["settle_ms"]
    with pytest.raises(ValueError):
        ld.LoadDistributionConfig.from_bytes(yaml.safe_dump(document).encode())
    with pytest.raises(ValueError):
        make_config(unknown_key=1)


def test_the_shipped_config_matches_the_record() -> None:
    config = ld.LoadDistributionConfig.from_bytes(SHIPPED_CONFIG.read_bytes())
    config.validate_against(MetricCatalog.from_yaml(METRICS_PATH))
    assert [(b.name, b.lower_w) for b in config.axes.gpu.bands] == [
        ("idle", 0), ("light", 30), ("high", 200), ("full", 350)
    ]  # fmt: skip
    assert [(b.name, b.lower_w) for b in config.axes.cpu.bands] == [
        ("idle", 0), ("light", 50), ("full", 120)
    ]  # fmt: skip
    assert config.settle_ms == 300_000
    assert config.lookback_ms == 300_000 + 30_000 + 5_000 + 3_500
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    assert set(catalog.derived) <= set(config.metrics)


# ---------------------------------------------------------------- 出力


def test_the_cli_output_has_provenance_and_no_db_path(fx: Fixture) -> None:
    steady_bios(fx, T0 - 10 * MIN, T0 + 10 * MIN)
    config_path = fx.tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config_dict()), encoding="utf-8")
    periods = fx.periods(bios_everywhere())
    out = fx.tmp_path / "out" / "r.json"
    argv = [
        "--db", str(fx.db), "--config", str(config_path), "--start", iso(T0),
        "--end", iso(T0 + 10 * MIN), "--periods", str(periods), "--out", str(out),
    ]  # fmt: skip
    assert ld.main(argv) == 0
    text = out.read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["version"] == 1
    provenance = report["provenance"]
    for key in (
        "period", "config_sha256", "metrics_yaml_sha256", "db_schema_version", "periods_sha256",
        "calibration_changes", "calibration_changed", "clock_rewind_detection",
    ):  # fmt: skip
        assert key in provenance
    assert str(fx.tmp_path) not in text and "coldaisle.db" not in text
    assert ld.main(argv) == 0 and out.read_text(encoding="utf-8") == text  # 同じ bytes

    markdown = fx.tmp_path / "out" / "r.md"
    assert ld.main([*argv[:-2], "--format", "markdown", "--out", str(markdown)]) == 0
    lines = markdown.read_text(encoding="utf-8").splitlines()
    assert "| cooling | 状態 | period | n | p50 | p95 | p99 | max | segment |" in lines
    assert "## 除いた件数" in lines


def test_the_output_never_overwrites_an_input(fx: Fixture) -> None:
    argv = ["--db", str(fx.db), "--start", iso(T0), "--end", iso(T0 + MIN), "--out", str(fx.db)]
    before = fx.db.read_bytes()
    assert ld.main(argv) != 0
    assert fx.db.read_bytes() == before


# ---------------------------------------------------------------- import の向き


def _imported_modules(path: Path) -> Iterable[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def test_ai_api_and_server_never_import_this_module() -> None:
    paths = [*(SRC / "ai").rglob("*.py"), *(SRC / "api").rglob("*.py"), SRC / "server.py"]
    offenders = [
        str(path.relative_to(SRC))
        for path in paths
        for name in _imported_modules(path)
        if name.startswith("coldaisle.load_distribution")
        or name == "coldaisle.load_distribution"
        or name.endswith(".load_distribution")
    ]
    assert offenders == []
