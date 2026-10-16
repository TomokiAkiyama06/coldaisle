"""合成の起点: 負荷の状態ごとの差温度（`d.*`）の分布（#50 / 決定記録 0151）。

```bash
uv run coldaisle-load-distribution --db <DB の写し> --start 2026-10-15T16:36 \\
  --end 2026-10-16T16:36 --out var/load-distribution/t1.json
uv run coldaisle-load-distribution --db <DB の写し> --start ... --end ... \\
  --periods var/load-distribution/periods.yaml --format markdown --out var/load-distribution/t1.md
```

**DB を読むだけ。** `soak.py` の `SoakDatabase` と同じ `mode=ro` の接続で開き、マイグレーションを
当てない（0151 §2.1 / 0070）。書くのは `--out` の1ファイルだけ（省けば標準出力）。

**合否も閾値の提案も出さない**（0151 §2.1 / §5 #4）。p99 + マージンの計算と `config/rules.yaml` の
値の決定は人がする。

- 負荷の状態は CPU と GPU の**観測した電力**の帯の組み合わせ（§2.2）。GPU Mode の申告
  （`events` / `sys.gpu_mode`）と Workload Hint は使わない（申告は観測ではない。0045 §2.6）
- `d.*` は保存しない（0002 §2.2）ので、生データ（`readings`）から `config/metrics.yaml` の
  被減数と減数の定義で組み立てる（§2.4）。ロールアップは覆いの確かめ（§2.7）にだけ使う
- `cooling`（誰が物理の Fan を動かしていたか）は trace の有無ではなく、trace の `pwm_raw` と
  `coldaisle-telemetry` が hwmon から読んだ `fan.*.pwm` の突き合わせで決める（§2.6a）
- AI 層・API・`server.py` はこの module を import しない（§2.9。CI が AST で見張る）

**同じ入力からは同じ bytes を出す。** 生成時刻と DB のパスを持たない（ルール 10）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sqlite3
import sys
from array import array
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, Literal, Self
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldaisle import logs
from coldaisle.control.schema import HWMON_PWM_MAX, ControlTick, OperatingMode, Zone
from coldaisle.metrics import MetricCatalog
from coldaisle.soak import SoakDatabase, SoakDatabaseError
from coldaisle.store import migrations
from coldaisle.store.calibration_history import (
    SELECT_ROWS as CALIBRATION_SELECT_ROWS,
)
from coldaisle.store.calibration_history import (
    CalibrationHistoryError,
    verify_activation_rows,
)

LOGGER = logging.getLogger("coldaisle.load_distribution")

REPORT_VERSION: Final = 1
"""出力の JSON の形の版（0151 §2.8）。"""

CONFIG_VERSION: Final = 1
"""`config/load-distribution.yaml` の形の版。"""

MINUTE_MS: Final = 60_000
"""`readings_1m` のバケットの幅（0001）。調整する値ではない。"""

ZONES: Final = tuple(Zone)
"""`cooling` の突き合わせに使う zone（Front / Rear / Top）。"""

CLOCK_NOTE: Final = (
    "時計の戻りは trace のある期間でしか検出していない。"
    "人が知っている時計の調整は --periods の exclude で渡す"
)
"""0151 §2.6。出力の来歴に必ず書く。"""

REGIME_NOTE: Final = "Workload Regime の設定は provisional（決定記録 0119 §2.14）"
"""0151 §2.6 の 4。`--group-by trace-regime` の出力に必ず書く。"""

PROVISIONAL_NOTE: Final = (
    "設定の値（帯・settle_ms ほか）はすべて provisional（決定記録 0151 §2.10）"
)


class LoadDistributionError(RuntimeError):
    """集計を拒否した（設定・期間・覆い・DB の不整合）。**何も出力しない。**"""


# ---------------------------------------------------------------------- 設定


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Band(_Frozen):
    """名前つきの電力の帯。`[lower_w, 次の帯の lower_w)`。"""

    name: str = Field(min_length=1)
    lower_w: float = Field(ge=0, allow_inf_nan=False)


class Axis(_Frozen):
    """負荷の状態の軸（CPU / GPU）。"""

    metric: str
    bands: tuple[Band, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _bands_are_ordered(self) -> Self:
        if self.bands[0].lower_w != 0:
            raise ValueError("最初の帯の下限は 0 にする")
        lowers = [band.lower_w for band in self.bands]
        if any(later <= earlier for earlier, later in pairwise(lowers)):
            raise ValueError("帯の下限は重複なしの昇順にする")
        names = [band.name for band in self.bands]
        if len(set(names)) != len(names):
            raise ValueError("帯の名前が重複している")
        return self

    def band_of(self, value: float) -> str | None:
        """値の入る帯の名前。負の値は帯に入らない（None）。"""
        index = bisect_right([band.lower_w for band in self.bands], value) - 1
        return None if index < 0 else self.bands[index].name


class Axes(_Frozen):
    cpu: Axis
    gpu: Axis


class LoadDistributionConfig(_Frozen):
    """`config/load-distribution.yaml`（0151 §2.10）。**既定値を持たない**（ルール 9）。"""

    version: Literal[1]
    timezone: str
    max_period_days: int = Field(gt=0)
    expected_interval_ms: int = Field(gt=0)
    axes: Axes
    power_window_ms: int = Field(gt=0)
    power_min_ok_ratio: float = Field(gt=0, le=1, allow_inf_nan=False)
    settle_ms: int = Field(ge=0)
    max_gap_ms: int = Field(gt=0)
    pair_max_age_ms: int = Field(ge=0)
    trace_max_age_ms: int = Field(gt=0)
    fan_max_age_ms: int = Field(ge=0)
    pwm_match_tolerance_pct: float = Field(ge=0, allow_inf_nan=False)
    pwm_mismatch_max_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    fan_expected_interval_ms: int = Field(gt=0)
    fan_min_samples_per_segment: int = Field(ge=1)
    fan_min_coverage_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    pwm_apply_delay_ms: int = Field(ge=0)
    quantiles: tuple[float, ...]
    min_samples: int = Field(ge=1)
    metrics: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if any(not 0.0 <= q <= 1.0 for q in self.quantiles):
            raise ValueError("quantiles は 0 以上 1 以下にする")
        if list(self.quantiles) != sorted(set(self.quantiles)):
            raise ValueError("quantiles は重複なしの昇順にする")
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("metrics に重複がある")
        try:
            ZoneInfo(self.timezone)
        except Exception as error:
            raise ValueError(f"タイムゾーンが不正: {self.timezone!r}") from error
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def lookback_ms(self) -> int:
        """`--start` からさかのぼって読む長さ（0151 §2.3）。

        設定から計算し、別の値として持たない。
        """
        return (
            self.settle_ms
            + self.power_window_ms
            + self.fan_max_age_ms
            + max(self.trace_max_age_ms, self.pwm_apply_delay_ms)
        )

    @property
    def overlap_reach_ms(self) -> int:
        """壁時計の重なりを確かめる、時刻からさかのぼる範囲（0151 §2.6）。"""
        return max(
            self.power_window_ms,
            self.fan_max_age_ms + max(self.trace_max_age_ms, self.pwm_apply_delay_ms),
        )

    def validate_against(self, catalog: MetricCatalog) -> None:
        """metric の名前と電力の単位を `metrics.yaml` で確かめる。外れたら起動しない。"""
        for name, axis in (("cpu", self.axes.cpu), ("gpu", self.axes.gpu)):
            meta = catalog.metrics.get(axis.metric)
            if meta is None:
                raise ValueError(f"axes.{name}.metric が metrics.yaml に無い: {axis.metric!r}")
            if meta.unit != "W":
                raise ValueError(f"axes.{name}.metric の単位が W でない: {axis.metric!r}")
        for metric in self.metrics:
            if metric not in catalog.metrics and metric not in catalog.derived:
                raise ValueError(f"metrics の名前が metrics.yaml に無い: {metric!r}")
        for zone in ZONES:
            if fan_pwm_metric(zone) not in catalog.metrics:
                raise ValueError(f"metrics.yaml に {fan_pwm_metric(zone)} が無い")

    @classmethod
    def from_bytes(cls, payload: bytes) -> LoadDistributionConfig:
        loaded: Any = yaml.safe_load(payload.decode("utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("load-distribution の設定が辞書ではない")
        return cls.model_validate_json(_as_json_text(loaded), strict=True)


def _as_json_text(loaded: dict[str, Any]) -> str:
    """YAML を JSON の値にしてから strict で検証する。

    JSON の strict は列を tuple に、整数を float の欄に受け、引用符つきの数字・真偽値の数・
    YAML の datetime は受けない（0151 §2.10「型の違いは拒否する」）。
    """
    try:
        return json.dumps(loaded, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"YAML に JSON で表せない値がある（時刻は引用符で囲む）: {error}"
        ) from error


def fan_pwm_metric(zone: Zone) -> str:
    return f"fan.{zone.value}.pwm"


# ---------------------------------------------------------------------- --periods


def parse_time(text: str, zone: ZoneInfo) -> int:
    """ISO 8601 を Unix ms にする。**タイムゾーンが無ければ設定のものとみなす。**"""
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return int(parsed.timestamp() * 1000)


class _PeriodBase(_Frozen):
    # YAML の引用符の無い時刻は PyYAML が datetime にし、タイムゾーンの扱いが版で変わる。
    # 文字列だけを受け、解釈はこちらで1つにする
    start: str
    end: str


class NamedPeriod(_PeriodBase):
    name: str = Field(min_length=1)


class ExcludePeriod(_PeriodBase):
    reason: str = Field(min_length=1)


class BiosConfirmedPeriod(_PeriodBase):
    basis: str = Field(min_length=1)


class PeriodsFile(_Frozen):
    """`--periods` の YAML（0151 §2.6b）。"""

    periods: tuple[NamedPeriod, ...] = ()
    exclude: tuple[ExcludePeriod, ...] = ()
    bios_confirmed: tuple[BiosConfirmedPeriod, ...] = ()


@dataclass(frozen=True)
class Interval:
    """半開区間 `[start_ms, end_ms)` と、名前（name / reason / basis）。"""

    start_ms: int
    end_ms: int
    text: str


class IntervalSet:
    """同じ種類の区間の集まり。重なりは作る時に拒む（端が接するのは重なりではない）。"""

    __slots__ = ("_intervals", "_starts")

    def __init__(self, kind: str, intervals: Iterable[Interval]) -> None:
        ordered = sorted(intervals, key=lambda item: (item.start_ms, item.end_ms))
        for item in ordered:
            if item.end_ms <= item.start_ms:
                raise ValueError(f"{kind} の区間は end > start にする: {item.text!r}")
        for earlier, later in pairwise(ordered):
            if later.start_ms < earlier.end_ms:
                raise ValueError(f"{kind} の区間が重なっている: {earlier.text!r} と {later.text!r}")
        self._intervals = tuple(ordered)
        self._starts = [item.start_ms for item in ordered]

    def __bool__(self) -> bool:
        return bool(self._intervals)

    @property
    def intervals(self) -> tuple[Interval, ...]:
        return self._intervals

    def crossed(self, after_ms: int, before_ms: int) -> bool:
        """開区間 `(after_ms, before_ms)` のどこかが区間に入るか（評価時刻の間に挟まった区間）。"""
        index = bisect_left(self._starts, before_ms) - 1
        if index < 0:
            return False
        item = self._intervals[index]
        return max(item.start_ms, after_ms + 1) < min(item.end_ms, before_ms)

    def find(self, at_ms: int) -> Interval | None:
        index = bisect_right(self._starts, at_ms) - 1
        if index < 0:
            return None
        item = self._intervals[index]
        return item if at_ms < item.end_ms else None


@dataclass(frozen=True)
class Periods:
    """読み込んだ `--periods`。無ければ空の集まりと `sha256=None`。"""

    named: IntervalSet
    exclude: IntervalSet
    bios_confirmed: IntervalSet
    sha256: str | None
    document: PeriodsFile | None

    @classmethod
    def empty(cls) -> Periods:
        return cls(
            IntervalSet("periods", ()),
            IntervalSet("exclude", ()),
            IntervalSet("bios_confirmed", ()),
            None,
            None,
        )

    @classmethod
    def from_bytes(cls, payload: bytes, zone: ZoneInfo) -> Periods:
        loaded: Any = yaml.safe_load(payload.decode("utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("--periods の YAML が辞書ではない")
        document = PeriodsFile.model_validate_json(_as_json_text(loaded), strict=True)

        def interval(item: _PeriodBase, text: str) -> Interval:
            return Interval(parse_time(item.start, zone), parse_time(item.end, zone), text)

        return cls(
            named=IntervalSet("periods", (interval(item, item.name) for item in document.periods)),
            exclude=IntervalSet(
                "exclude", (interval(item, item.reason) for item in document.exclude)
            ),
            bios_confirmed=IntervalSet(
                "bios_confirmed", (interval(item, item.basis) for item in document.bios_confirmed)
            ),
            sha256=hashlib.sha256(payload).hexdigest(),
            document=document,
        )

    def period_of(self, at_ms: int) -> str | None:
        """`period` の鍵。名前つきの区間が無ければ `all`、あってその外なら None（使わない）。"""
        if not self.named:
            return "all"
        found = self.named.find(at_ms)
        return None if found is None else found.text


# ---------------------------------------------------------------------- DB


@dataclass(frozen=True)
class Row:
    ts_ms: int
    value: float | None
    quality: str


@dataclass(frozen=True)
class PruneState:
    pruned_through_seq: int
    legacy_through_seq: int


@dataclass(frozen=True)
class TraceDetail:
    """§2.6a の突き合わせに使う欄だけ（trace の全体を検証の型に通さない）。"""

    mode: str | None
    pwm_raw: tuple[int | None, ...]
    """zone ごとの `hardware.pwm_raw`。`hardware` が無い・整数でなければ None。"""
    verified: bool
    """3 つの zone すべてで `hardware` があり `write_ok` と `readback_ok` がともに true。"""


class LoadDistributionDatabase(SoakDatabase):
    """集計が読む DB。`SoakDatabase` の `mode=ro` の開き方と版の検査をそのまま使う。"""

    def schema_version(self) -> int:
        return migrations.current_version(self._conn)

    def rows(self, metric: str, from_ms: int, to_ms: int) -> list[Row]:
        """`[from_ms, to_ms)` の生データを時刻の順に（品質を問わない）。"""
        return [
            Row(int(ts), None if value is None else float(value), str(quality))
            for ts, value, quality in self._conn.execute(
                "SELECT ts_ms, value, quality FROM readings "
                "WHERE metric = ? AND ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms",
                (metric, from_ms, to_ms),
            )
        ]

    def raw_bucket_counts(self, metric: str, from_ms: int, to_ms: int) -> dict[int, int]:
        return {
            int(bucket): int(count)
            for bucket, count in self._conn.execute(
                f"SELECT (ts_ms / {MINUTE_MS}) * {MINUTE_MS} AS bucket, COUNT(*) FROM readings "
                "WHERE metric = ? AND ts_ms >= ? AND ts_ms < ? GROUP BY bucket",
                (metric, from_ms, to_ms),
            )
        }

    def rollup_row_counts(self, metric: str, from_ms: int, to_ms: int) -> dict[int, int]:
        return {
            int(bucket): int(count)
            for bucket, count in self._conn.execute(
                "SELECT bucket_ms, row_count FROM readings_1m "
                "WHERE metric = ? AND bucket_ms >= ? AND bucket_ms < ?",
                (metric, from_ms, to_ms),
            )
        }

    def trace_index(self) -> tuple[array[int], array[int]]:
        """全 trace の `(seq, ts_ms)` を `seq` の順に。区間の全体をたどるため期間で切らない。"""
        seqs: array[int] = array("q")
        stamps: array[int] = array("q")
        for seq, ts in self._conn.execute("SELECT seq, ts_ms FROM control_traces ORDER BY seq"):
            seqs.append(int(seq))
            stamps.append(int(ts))
        return seqs, stamps

    def prune_state(self) -> PruneState:
        row = self._conn.execute(
            "SELECT pruned_through_seq, legacy_through_seq FROM control_trace_prune"
        ).fetchone()
        if row is None:
            raise LoadDistributionError("control_trace_prune の行が無い（DB が壊れている）")
        return PruneState(int(row[0]), int(row[1]))

    def trace_details(self, seq_from: int, seq_to: int) -> dict[int, TraceDetail]:
        """`seq` が `[seq_from, seq_to]` の trace の突き合わせの欄（SQL の json_extract で）。"""
        columns = ["json_extract(trace_json, '$.state.operating_mode')"]
        for zone in ZONES:
            base = f"$.zones.{zone.value}.hardware"
            columns += [
                f"json_type(trace_json, '{base}.pwm_raw')",
                f"json_extract(trace_json, '{base}.pwm_raw')",
                f"json_type(trace_json, '{base}.write_ok')",
                f"json_type(trace_json, '{base}.readback_ok')",
            ]
        sql = (
            f"SELECT seq, {', '.join(columns)} FROM control_traces "
            "WHERE seq >= ? AND seq <= ? ORDER BY seq"
        )
        details: dict[int, TraceDetail] = {}
        for row in self._conn.execute(sql, (seq_from, seq_to)):
            mode = row[1] if isinstance(row[1], str) else None
            pwm: list[int | None] = []
            verified = True
            for index in range(len(ZONES)):
                kind, raw, write_kind, read_kind = row[2 + index * 4 : 6 + index * 4]
                value = int(raw) if kind == "integer" else None
                pwm.append(value)
                if value is None or write_kind != "true" or read_kind != "true":
                    verified = False
            details[int(row[0])] = TraceDetail(mode, tuple(pwm), verified)
        return details

    def trace_json(self, seq: int) -> str | None:
        row = self._conn.execute(
            "SELECT trace_json FROM control_traces WHERE seq = ?", (seq,)
        ).fetchone()
        return None if row is None else str(row[0])

    def calibration_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(CALIBRATION_SELECT_ROWS))


# ---------------------------------------------------------------------- trace の時系列


class Kind(StrEnum):
    """Fan のサンプルの突き合わせの結果（§2.6a）。"""

    FAND = "fand"
    FAND_OVERRIDE = "fand_override"
    MODE_MIXED = "mode_mixed"
    MISMATCH = "pwm_mismatch"
    UNVERIFIED = "write_unverified"
    UNALIGNED = "fan_sample_unaligned"
    OVERLAP = "clock_overlap"
    CROSS_SEGMENT = "cross_segment"
    NO_TRACE = "no_trace"


_IN_RATIO: Final = frozenset(Kind) - {Kind.CROSS_SEGMENT, Kind.NO_TRACE}
"""区間の割合の分母に入る結果。別の区間の trace が窓に入ったサンプルはどの区間にも寄せない。"""

_MISMATCHED: Final = frozenset({Kind.MISMATCH, Kind.UNVERIFIED, Kind.UNALIGNED, Kind.OVERLAP})
"""区間の割合の分子（一致にしないもの）。書き込みを確かめられない tick も数える（§2.6a）。"""


@dataclass
class Segment:
    """`seq` の順で連続した trace の区間（§2.6a）。"""

    index: int
    first_pos: int
    last_pos: int
    s0: int
    s1: int
    first_ts: int
    last_ts: int
    min_ts: int
    max_ts: int
    samples: int = 0
    aligned: int = 0
    mismatched: int = 0
    status: str | None = None
    """None なら受け入れる。そうでなければ `undetermined` の理由。"""

    @property
    def seq_gaps(self) -> int:
        return (self.s1 - self.s0 + 1) - (self.last_pos - self.first_pos + 1)


class TraceTimeline:
    """全 trace の `(seq, ts_ms)` から区間・as-of・壁時計の重なりを引く（§2.6 / §2.6a）。"""

    def __init__(
        self, seqs: array[int], stamps: array[int], config: LoadDistributionConfig
    ) -> None:
        self.seqs = seqs
        self.stamps = stamps
        self._max_age = config.trace_max_age_ms
        self._delay = config.pwm_apply_delay_ms
        self._reach = config.overlap_reach_ms
        count = len(seqs)
        # 区間: seq の順で隣り合う 2 行の ts_ms の差が trace_max_age_ms を超えたら切る。
        # 差が負（時計が戻った）なら切らない（§2.6a）
        self.segments: list[Segment] = []
        self._segment_starts: list[int] = []
        start = 0
        for pos in range(1, count + 1):
            if pos == count or stamps[pos] - stamps[pos - 1] > self._max_age:
                window = stamps[start:pos]
                self._segment_starts.append(start)
                self.segments.append(
                    Segment(
                        index=len(self.segments),
                        first_pos=start,
                        last_pos=pos - 1,
                        s0=seqs[start],
                        s1=seqs[pos - 1],
                        first_ts=stamps[start],
                        last_ts=stamps[pos - 1],
                        min_ts=min(window),
                        max_ts=max(window),
                    )
                )
                start = pos
        # 壁時計の重なり: ts_ms が前の行より小さくなる箇所ごとに [戻った後, それまでの最大]
        overlaps: list[tuple[int, int]] = []
        running = stamps[0] if count else 0
        for pos in range(1, count):
            if stamps[pos] < stamps[pos - 1]:
                overlaps.append((stamps[pos], running))
            running = max(running, stamps[pos])
        merged: list[tuple[int, int]] = []
        for low, high in sorted(overlaps):
            if merged and low <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], high))
            else:
                merged.append((low, high))
        self.overlaps = merged
        self._overlap_starts = [low for low, _ in merged]
        # as-of: ts_ms ≤ x の trace のうち seq が最大のもの。ts の順の位置と seq の累積最大で引く
        order = sorted(range(count), key=stamps.__getitem__)
        self._order: array[int] = array("q", order)
        self._sorted_ts: array[int] = array("q", (stamps[pos] for pos in order))
        prefix: array[int] = array("q")
        best = -1
        for pos in order:
            if best < 0 or seqs[pos] > seqs[best]:
                best = pos
            prefix.append(best)
        self._prefix_best = prefix

    @property
    def max_seq(self) -> int | None:
        return self.seqs[-1] if self.seqs else None

    def has_seq(self, seq: int) -> bool:
        index = bisect_left(self.seqs, seq)
        return index < len(self.seqs) and self.seqs[index] == seq

    def segment_of(self, pos: int) -> Segment:
        return self.segments[bisect_right(self._segment_starts, pos) - 1]

    def touches_overlap(self, at_ms: int) -> bool:
        """`[at_ms - reach, at_ms]` が壁時計の重なりの範囲に触れるか。"""
        index = bisect_right(self._overlap_starts, at_ms) - 1
        return index >= 0 and self.overlaps[index][1] >= at_ms - self._reach

    def as_of(self, at_ms: int) -> int | None:
        """as-of の trace の位置（§2.6 の規則）。古さが `trace_max_age_ms` を超えれば None。"""
        index = bisect_right(self._sorted_ts, at_ms) - 1
        if index < 0:
            return None
        pos = self._prefix_best[index]
        return pos if at_ms - self.stamps[pos] <= self._max_age else None

    def positions_between(self, low_ms: int, high_ms: int) -> list[int]:
        """`ts_ms` が `[low_ms, high_ms]` の trace の位置。"""
        lo = bisect_left(self._sorted_ts, low_ms)
        hi = bisect_right(self._sorted_ts, high_ms)
        return [self._order[index] for index in range(lo, hi)]


# ---------------------------------------------------------------------- cooling（§2.6a）


@dataclass(frozen=True)
class SampleResult:
    kind: Kind
    segment: Segment | None


class CoolingJudge:
    """`cooling`（fand / bios / fand_override / undetermined）を時刻ごとに決める（§2.6a）。"""

    def __init__(
        self,
        database: LoadDistributionDatabase,
        timeline: TraceTimeline,
        prune: PruneState,
        config: LoadDistributionConfig,
        periods: Periods,
        *,
        read_from_ms: int,
        read_to_ms: int,
    ) -> None:
        self._timeline = timeline
        self._config = config
        self._periods = periods
        self.bios_conflicts = 0
        # 区間の全体を読むため、期間に触れる区間の端まで Fan の行と trace の欄を広げる
        margin = max(config.trace_max_age_ms, config.pwm_apply_delay_ms)
        low, high = read_from_ms - config.fan_max_age_ms, read_to_ms
        relevant: dict[int, Segment] = {}
        while True:
            for pos in timeline.positions_between(low - margin, high):
                segment = timeline.segment_of(pos)
                relevant.setdefault(segment.index, segment)
            new_low = min([low, *(segment.min_ts for segment in relevant.values())])
            new_high = max(
                [high, *(segment.max_ts + config.trace_max_age_ms for segment in relevant.values())]
            )
            if (new_low, new_high) == (low, high):
                break
            low, high = new_low, new_high
        self._details: dict[int, TraceDetail] = {}
        for segment in relevant.values():
            self._details.update(database.trace_details(segment.s0, segment.s1))
        fan_rows = {
            zone: {
                row.ts_ms: row.value
                for row in database.rows(fan_pwm_metric(zone), low, high + 1)
                if row.quality == "ok" and row.value is not None
            }
            for zone in ZONES
        }
        times = sorted(set().union(*(set(rows) for rows in fan_rows.values())))
        self._sample_times = times
        self._samples: list[SampleResult] = []
        for at_ms in times:
            values = [fan_rows[zone].get(at_ms) for zone in ZONES]
            vector = tuple(value for value in values if value is not None)
            result = self._match(at_ms, vector if len(vector) == len(ZONES) else None)
            self._samples.append(result)
            if result.segment is not None and result.kind in _IN_RATIO:
                result.segment.samples += 1
                if result.kind is not Kind.UNALIGNED:
                    result.segment.aligned += 1
                if result.kind in _MISMATCHED:
                    result.segment.mismatched += 1
        fan_pruned = sorted(
            bucket
            for zone in ZONES
            for bucket in deleted_buckets(database, fan_pwm_metric(zone), low, high + 1)
        )
        for segment in relevant.values():
            segment.status = self._segment_status(segment, prune, fan_pruned)
        self.relevant_segments = sorted(relevant.values(), key=lambda segment: segment.index)

    def _match(self, at_ms: int, vector: tuple[float, ...] | None) -> SampleResult:
        """1つの Fan のサンプルを trace の候補と突き合わせる。サンプルと区間の判定で共有する。"""
        timeline = self._timeline
        as_of = timeline.as_of(at_ms)
        if as_of is None:
            return SampleResult(Kind.NO_TRACE, None)
        segment = timeline.segment_of(as_of)
        candidates = set(timeline.positions_between(at_ms - self._config.pwm_apply_delay_ms, at_ms))
        candidates.add(as_of)
        if any(timeline.segment_of(pos) is not segment for pos in candidates):
            return SampleResult(Kind.CROSS_SEGMENT, segment)
        if vector is None:
            return SampleResult(Kind.UNALIGNED, segment)
        if timeline.touches_overlap(at_ms):
            return SampleResult(Kind.OVERLAP, segment)
        details = [self._details.get(timeline.seqs[pos]) for pos in sorted(candidates)]
        if any(detail is None or not detail.verified for detail in details):
            return SampleResult(Kind.UNVERIFIED, segment)
        tolerance = self._config.pwm_match_tolerance_pct
        modes: set[str | None] = set()
        for detail in details:
            assert detail is not None
            if all(
                raw is not None and abs(observed - raw / HWMON_PWM_MAX * 100.0) <= tolerance
                for observed, raw in zip(vector, detail.pwm_raw, strict=True)
            ):
                modes.add(detail.mode)
        if not modes:
            return SampleResult(Kind.MISMATCH, segment)
        known = {mode.value for mode in OperatingMode}
        if any(mode not in known for mode in modes):
            return SampleResult(Kind.UNVERIFIED, segment)
        if modes == {OperatingMode.AUTO.value}:
            return SampleResult(Kind.FAND, segment)
        if OperatingMode.AUTO.value not in modes:
            return SampleResult(Kind.FAND_OVERRIDE, segment)
        return SampleResult(Kind.MODE_MIXED, segment)

    def _segment_status(
        self, segment: Segment, prune: PruneState, fan_pruned: Sequence[int]
    ) -> str | None:
        timeline = self._timeline
        config = self._config
        if segment.s1 == timeline.max_seq:
            return "segment_open"
        if segment.seq_gaps:
            return "seq_gap"
        if segment.s0 <= prune.legacy_through_seq:
            return "legacy_rows"
        start_ok = timeline.has_seq(segment.s0 - 1) or (
            segment.s0 == 1 and prune.legacy_through_seq == 0
        )
        if not start_ok:
            return "segment_start_unconfirmed"
        if not timeline.has_seq(segment.s1 + 1):
            return "segment_end_unconfirmed"
        first = bisect_left(fan_pruned, (segment.min_ts // MINUTE_MS) * MINUTE_MS)
        if first < len(fan_pruned) and fan_pruned[first] <= segment.max_ts:
            return "fan_raw_pruned"
        length = segment.last_ts - segment.first_ts
        if (
            segment.aligned < config.fan_min_samples_per_segment
            or length <= 0
            or segment.aligned / (length / config.fan_expected_interval_ms)
            < config.fan_min_coverage_ratio
        ):
            return "fan_coverage_low"
        if segment.mismatched / segment.samples > config.pwm_mismatch_max_ratio:
            return "pwm_mismatch_ratio"
        return None

    def cooling_at(self, at_ms: int) -> tuple[str, str | None]:
        """`(cooling, undetermined の理由)`。"""
        timeline = self._timeline
        if timeline.touches_overlap(at_ms):
            return "undetermined", "clock_overlap"
        index = bisect_right(self._sample_times, at_ms) - 1
        sample: SampleResult | None = None
        if index >= 0 and at_ms - self._sample_times[index] <= self._config.fan_max_age_ms:
            sample = self._samples[index]
        as_of = timeline.as_of(at_ms)
        if self._periods.bios_confirmed.find(at_ms) is not None:
            # 人の記録と DB が食い違う時刻（as-of の trace がある・直前のサンプルに trace がある）は
            # bios にしない
            if as_of is None and (sample is None or sample.kind is Kind.NO_TRACE):
                return "bios", None
            return "undetermined", "bios_confirmed_conflict"
        if sample is None:
            return "undetermined", (
                "no_trace_outside_bios_confirmed" if as_of is None else "fan_sample_missing"
            )
        if sample.kind is Kind.NO_TRACE:
            return "undetermined", "no_trace_outside_bios_confirmed"
        if sample.segment is not None and sample.segment.status is not None:
            # 区間ごと受け入れない理由を、サンプルごとの理由より先に出す
            return "undetermined", sample.segment.status
        if sample.kind in (Kind.FAND, Kind.FAND_OVERRIDE):
            return sample.kind.value, None
        return "undetermined", sample.kind.value


# ---------------------------------------------------------------------- 状態のラベル


class PowerSeries:
    """1つの電力の軸の ok の行。`(t - window, t]` の平均を累積和で引く。"""

    def __init__(self, rows: Sequence[Row], config: LoadDistributionConfig) -> None:
        usable = [
            (row.ts_ms, row.value) for row in rows if row.quality == "ok" and row.value is not None
        ]
        self._times = [ts for ts, _ in usable]
        self._sums = [0.0]
        for _, value in usable:
            self._sums.append(self._sums[-1] + value)
        self._window = config.power_window_ms
        self._required = (
            config.power_window_ms / config.expected_interval_ms * config.power_min_ok_ratio
        )

    def mean(self, at_ms: int) -> float | None:
        lo = bisect_right(self._times, at_ms - self._window)
        hi = bisect_right(self._times, at_ms)
        count = hi - lo
        if count == 0 or count < self._required:
            return None
        return (self._sums[hi] - self._sums[lo]) / count


UNKNOWN_STATE: Final = "unknown"


class Labeler:
    """評価時刻のラベル（電力の帯の状態か trace の Workload Regime）。

    返す `(label, excluded)` の `excluded` が真のラベルは分布に入れず件数だけ数える。
    """

    def __init__(
        self,
        mode: Literal["state", "trace-regime"],
        config: LoadDistributionConfig,
        cpu: PowerSeries,
        gpu: PowerSeries,
        timeline: TraceTimeline,
        database: LoadDistributionDatabase,
    ) -> None:
        self.mode = mode
        self._config = config
        self._cpu = cpu
        self._gpu = gpu
        self._timeline = timeline
        self._database = database
        self._regimes: dict[int, str | None] = {}

    def label_at(self, at_ms: int) -> tuple[str, bool]:
        if self.mode == "state":
            return self._state(at_ms)
        return self._regime(at_ms)

    def _state(self, at_ms: int) -> tuple[str, bool]:
        cpu_mean = self._cpu.mean(at_ms)
        gpu_mean = self._gpu.mean(at_ms)
        cpu = None if cpu_mean is None else self._config.axes.cpu.band_of(cpu_mean)
        gpu = None if gpu_mean is None else self._config.axes.gpu.band_of(gpu_mean)
        if cpu is None or gpu is None:
            return UNKNOWN_STATE, True
        return f"cpu:{cpu}/gpu:{gpu}", False

    def _regime(self, at_ms: int) -> tuple[str, bool]:
        timeline = self._timeline
        if timeline.touches_overlap(at_ms):
            return "unknown_ambiguous", True
        pos = timeline.as_of(at_ms)
        if pos is None:
            return "unknown_no_trace", True
        seq = timeline.seqs[pos]
        if seq not in self._regimes:
            self._regimes[seq] = self._read_regime(seq)
        regime = self._regimes[seq]
        return ("unknown_unreadable", True) if regime is None else (regime, False)

    def _read_regime(self, seq: int) -> str | None:
        text = self._database.trace_json(seq)
        if text is None:
            return None
        try:
            tick = ControlTick.model_validate_json(text)
        except ValidationError:
            return None
        regime = tick.state.workload_regime
        return None if regime is None else regime.value


# ---------------------------------------------------------------------- 統計


def quantile_type7(ordered: Sequence[float], q: float) -> float:
    """順序統計量の線形補間（Hyndman–Fan の type 7。NumPy の既定と同じ）。"""
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (position - low) * (ordered[high] - ordered[low])


def summarize(values: Sequence[float], config: LoadDistributionConfig) -> dict[str, Any]:
    """`n`・min / max / mean / 標準偏差（母標準偏差。NumPy の既定）・分位点。小数 3 桁。"""
    count = len(values)
    out: dict[str, Any] = {"n": count}
    if count == 0:
        out.update(min=None, max=None, mean=None, std=None)
        out["quantiles"] = {_qkey(q): None for q in config.quantiles}
        out["reason"] = "no_samples"
        return out
    ordered = sorted(values)
    mean = math.fsum(ordered) / count
    out.update(min=round(ordered[0], 3), max=round(ordered[-1], 3), mean=round(mean, 3))
    if count < config.min_samples:
        out["std"] = None
        out["quantiles"] = {_qkey(q): None for q in config.quantiles}
        out["reason"] = "insufficient_samples"
        return out
    variance = math.fsum((value - mean) ** 2 for value in ordered) / count
    out["std"] = round(math.sqrt(variance), 3)
    out["quantiles"] = {_qkey(q): round(quantile_type7(ordered, q), 3) for q in config.quantiles}
    out["reason"] = None
    return out


def _qkey(q: float) -> str:
    return f"p{q * 100:g}"


# ---------------------------------------------------------------------- 1つの metric の集計


GroupKey = tuple[str, str, str]
"""`(cooling, label, period)`。"""


@dataclass
class _Group:
    values: list[float] = field(default_factory=list)
    segments: list[int] = field(default_factory=list)
    """segment の長さ（ms。端の時刻の差）。"""


@dataclass
class MetricResult:
    metric: str
    unit: str
    groups: dict[GroupKey, _Group] = field(default_factory=lambda: defaultdict(_Group))
    transitions: dict[GroupKey, _Group] = field(default_factory=lambda: defaultdict(_Group))
    excluded: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    undetermined: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    quality: dict[GroupKey, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    start_lookback_incomplete: bool = False


@dataclass(frozen=True)
class _Classified:
    excluded: bool
    period: str | None
    cooling: str
    cooling_reason: str | None
    label: str
    label_excluded: bool


class Aggregator:
    """評価時刻を分類し、落ち着き・segment・統計を積む（§2.3 / §2.4 / §2.6）。"""

    def __init__(
        self,
        config: LoadDistributionConfig,
        periods: Periods,
        cooling: CoolingJudge,
        labeler: Labeler,
        *,
        start_ms: int,
        end_ms: int,
        include_override: bool,
        force_lookback_incomplete: bool = False,
    ) -> None:
        self._force_lookback_incomplete = force_lookback_incomplete
        self._config = config
        self._periods = periods
        self._cooling = cooling
        self._labeler = labeler
        self._start_ms = start_ms
        self._end_ms = end_ms
        self._read_from = start_ms - config.lookback_ms
        self._include_override = include_override
        self._cache: dict[int, _Classified] = {}

    def classify(self, at_ms: int) -> _Classified:
        cached = self._cache.get(at_ms)
        if cached is not None:
            return cached
        if self._periods.exclude.find(at_ms) is not None:
            result = _Classified(True, None, "", None, "", True)
        else:
            cooling, reason = self._cooling.cooling_at(at_ms)
            label, label_excluded = self._labeler.label_at(at_ms)
            result = _Classified(
                False, self._periods.period_of(at_ms), cooling, reason, label, label_excluded
            )
        self._cache[at_ms] = result
        return result

    def _lookback_incomplete(self, times: Sequence[int]) -> bool:
        first_in = bisect_left(times, self._start_ms)
        if first_in >= len(times):
            return False
        span = times[: first_in + 1]
        if span[0] > self._read_from + self._config.max_gap_ms:
            return True
        return any(later - earlier > self._config.max_gap_ms for earlier, later in pairwise(span))

    def run(
        self,
        metric: str,
        unit: str,
        evaluations: Sequence[tuple[int, float | None]],
        non_ok: Sequence[tuple[int, str]],
    ) -> MetricResult:
        """`evaluations` は ok の行の `(ts_ms, 値)`（`d.*` で対が無ければ値は None）。"""
        config = self._config
        result = MetricResult(metric, unit)
        times = [ts for ts, _ in evaluations]
        # 生データが消えた期間の直後は、さかのぼりの行が残っていても始めから続いたとは言えない
        incomplete = self._force_lookback_incomplete or self._lookback_incomplete(times)
        result.start_lookback_incomplete = incomplete
        prev_t: int | None = None
        prev_phys: tuple[str, str] | None = None
        timer: int | None = None
        crossed = False
        open_kind: str | None = None
        open_key: GroupKey | None = None
        open_start = 0
        open_last = 0

        def close() -> None:
            nonlocal open_kind, open_key
            if open_kind is not None and open_key is not None:
                target = result.groups if open_kind == "main" else result.transitions
                target[open_key].segments.append(open_last - open_start)
            open_kind = None
            open_key = None

        bad_times = [ts for ts, _ in non_ok]
        for at_ms, value in evaluations:
            in_window = at_ms >= self._start_ms
            if prev_t is not None and bisect_right(bad_times, prev_t) < bisect_left(
                bad_times, at_ms
            ):
                # 品質で除いた行を挟んだ。受け入れた観測が途切れたので segment を切る（§2.3）
                close()
            force = False
            if in_window and not crossed:
                crossed = True
                force = incomplete
            gap = prev_t is not None and at_ms - prev_t > config.max_gap_ms
            if gap:
                close()
            if prev_t is not None and self._periods.exclude.crossed(prev_t, at_ms):
                # 2 つの評価時刻の間に除く区間が丸ごと挟まった。その間の状態は分からないので、
                # 区間の中の時刻と同じく計時をやり直し segment を切る（0155 §2.2）
                prev_phys = None
                timer = None
                close()
            item = self.classify(at_ms)
            prev_t = at_ms
            if item.excluded:
                # 除く区間の中は状態を決めない。前後を同じ定常として繋がない
                prev_phys = None
                timer = None
                if in_window:
                    result.excluded["excluded_by_periods"] += 1
                    close()
                continue
            phys = (item.cooling, item.label)
            determinable = not item.label_excluded and item.cooling != "undetermined"
            if not determinable:
                timer = None
            elif timer is None or force or gap or phys != prev_phys:
                timer = at_ms
            prev_phys = phys
            if not in_window:
                continue
            kind: str | None = None
            if item.period is None:
                result.excluded["outside_periods"] += 1
            elif item.label_excluded:
                result.excluded[item.label] += 1
            elif item.cooling == "undetermined":
                result.excluded["undetermined"] += 1
                result.undetermined[item.cooling_reason or "unknown"] += 1
            elif item.cooling == "fand_override" and not self._include_override:
                result.excluded["fand_override"] += 1
            elif value is None:
                result.excluded["unpaired"] += 1
                result.quality[(item.cooling, item.label, item.period)]["unpaired"] += 1
            else:
                assert timer is not None
                kind = "main" if at_ms - timer >= config.settle_ms else "transition"
                if kind == "transition":
                    result.excluded["transition"] += 1
            if kind is None or item.period is None:
                close()
                continue
            key = (item.cooling, item.label, item.period)
            if open_kind != kind or open_key != key:
                close()
                open_kind, open_key, open_start = kind, key, at_ms
            open_last = at_ms
            assert value is not None
            target = result.groups if kind == "main" else result.transitions
            target[key].values.append(round(value, 3))
        close()
        for at_ms, quality in non_ok:
            item = self.classify(at_ms)
            if item.excluded:
                key = ("-", "-", "(excluded)")
            elif item.period is None:
                key = ("-", "-", "(outside)")
            else:
                key = (item.cooling, item.label, item.period)
            result.quality[key][quality] += 1
        return result


# ---------------------------------------------------------------------- 覆い（§2.7）


def deleted_buckets(
    database: LoadDistributionDatabase, metric: str, from_ms: int, to_ms: int
) -> list[int]:
    """生データの行数が `readings_1m.row_count` より少ない 1 分のバケット。

    期間の端のバケットは、期間に入る部分ではなくバケットの全体で比べる（0151 §2.7）。
    """
    first = (from_ms // MINUTE_MS) * MINUTE_MS
    last_end = ((to_ms - 1) // MINUTE_MS + 1) * MINUTE_MS
    rolled = database.rollup_row_counts(metric, first, last_end)
    if not rolled:
        return []
    raw = database.raw_bucket_counts(metric, first, last_end)
    return sorted(bucket for bucket, count in rolled.items() if raw.get(bucket, 0) < count)


# ---------------------------------------------------------------------- 組み立て


@dataclass(frozen=True)
class Inputs:
    config: LoadDistributionConfig
    config_sha256: str
    catalog: MetricCatalog
    catalog_sha256: str
    periods: Periods


def _metric_rows(
    database: LoadDistributionDatabase,
    catalog: MetricCatalog,
    metric: str,
    config: LoadDistributionConfig,
    read_from: int,
    end_ms: int,
    start_ms: int,
) -> tuple[list[tuple[int, float | None]], list[tuple[int, str]]]:
    derived = catalog.derived.get(metric)
    source = metric if derived is None else derived.minuend
    rows = database.rows(source, read_from, end_ms)
    evaluations: list[tuple[int, float | None]] = []
    non_ok: list[tuple[int, str]] = []
    pair_times: list[int] = []
    pair_values: list[float] = []
    if derived is not None:
        for row in database.rows(derived.subtrahend, read_from - config.pair_max_age_ms, end_ms):
            if row.quality == "ok" and row.value is not None:
                pair_times.append(row.ts_ms)
                pair_values.append(row.value)
    for row in rows:
        if row.quality != "ok" or row.value is None:
            if row.ts_ms >= start_ms:
                non_ok.append((row.ts_ms, "null" if row.quality == "ok" else row.quality))
            continue
        if derived is None:
            evaluations.append((row.ts_ms, row.value))
            continue
        # 同じ ts_ms の減数があればそれ、無ければ [t - pair_max_age_ms, t] の最も新しい ok の行
        index = bisect_right(pair_times, row.ts_ms) - 1
        if index >= 0 and row.ts_ms - pair_times[index] <= config.pair_max_age_ms:
            evaluations.append((row.ts_ms, round(row.value - pair_values[index], 3)))
        else:
            evaluations.append((row.ts_ms, None))
    return evaluations, non_ok


def build(
    database: LoadDistributionDatabase,
    inputs: Inputs,
    *,
    start_ms: int,
    end_ms: int,
    group_by: Literal["state", "trace-regime"],
    include_override: bool,
    allow_partial: bool,
) -> dict[str, Any]:
    """期間 `[start_ms, end_ms)` を集計した報告（JSON の形の辞書）。**DB には書かない。**"""
    config = inputs.config
    catalog = inputs.catalog
    if end_ms <= start_ms:
        raise LoadDistributionError("期間が空か逆転している（--end は --start より後にする）")
    if end_ms - start_ms > config.max_period_days * 86_400_000:
        raise LoadDistributionError(
            f"期間が max_period_days（{config.max_period_days} 日）を超える"
        )
    read_from = start_ms - config.lookback_ms

    # 生データの覆い（§2.7）。消えたバケットがあれば既定では失敗にする
    used: list[str] = []
    for metric in (*config.metrics, config.axes.cpu.metric, config.axes.gpu.metric,
                   *(fan_pwm_metric(zone) for zone in ZONES)):  # fmt: skip
        derived = catalog.derived.get(metric)
        for name in (metric,) if derived is None else (derived.minuend, derived.subtrahend):
            if name not in used:
                used.append(name)
    pruned = {
        metric: buckets
        for metric in used
        if (buckets := deleted_buckets(database, metric, read_from, end_ms))
    }
    if pruned and not allow_partial:
        first = min(buckets[0] for buckets in pruned.values())
        last = max(buckets[-1] for buckets in pruned.values())
        raise LoadDistributionError(
            "求めた期間の生データが保持期間の削除で消えている"
            f"（最初のバケット {_iso(first, config)}・最後のバケット {_iso(last, config)}・"
            f"metric {sorted(pruned)}）。期間を狭めるか --allow-partial を明示する"
        )
    # --allow-partial: 集計そのものを実際に覆った期間（消えた最後のバケットの次から）に絞る。
    # 来歴だけを狭めると、消えかけのバケットに残った行が分布に混ざる（0155 §2.5）
    covered_start = start_ms
    if pruned:
        covered_start = min(
            end_ms, max(start_ms, max(buckets[-1] for buckets in pruned.values()) + MINUTE_MS)
        )

    # 較正の変更（0099）。全行を検証し、期間の中の行を一覧にする
    try:
        activations = verify_activation_rows(database.calibration_rows())
    except CalibrationHistoryError as error:
        raise LoadDistributionError(f"較正の変更の記録を検証できない: {error}") from error
    changes = [
        {
            "ts_ms": row.ts_ms,
            "time": _iso(row.ts_ms, config),
            "offsets_sha256": row.offsets_sha256,
            "row_sha256": row.row_sha256,
        }
        for row in activations
        if start_ms <= row.ts_ms < end_ms
    ]

    seqs, stamps = database.trace_index()
    timeline = TraceTimeline(seqs, stamps, config)
    prune = database.prune_state()
    cooling = CoolingJudge(
        database, timeline, prune, config, inputs.periods, read_from_ms=read_from, read_to_ms=end_ms
    )
    cpu = PowerSeries(database.rows(config.axes.cpu.metric, read_from, end_ms), config)
    gpu = PowerSeries(database.rows(config.axes.gpu.metric, read_from, end_ms), config)
    labeler = Labeler(group_by, config, cpu, gpu, timeline, database)
    aggregator = Aggregator(
        config,
        inputs.periods,
        cooling,
        labeler,
        start_ms=covered_start,
        end_ms=end_ms,
        include_override=include_override,
        force_lookback_incomplete=bool(pruned),
    )
    results: list[MetricResult] = []
    for metric in config.metrics:
        evaluations, non_ok = _metric_rows(
            database, catalog, metric, config, read_from, end_ms, covered_start
        )
        unit = catalog.unit_for(metric)
        assert unit is not None
        results.append(aggregator.run(metric, unit, evaluations, non_ok))

    segment_reasons: dict[str, int] = defaultdict(int)
    for segment in cooling.relevant_segments:
        segment_reasons[segment.status or "accepted"] += 1
    provenance: dict[str, Any] = {
        "period": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start": _iso(start_ms, config),
            "end": _iso(end_ms, config),
            "timezone": config.timezone,
        },
        "lookback_ms": config.lookback_ms,
        "start_lookback_incomplete": any(item.start_lookback_incomplete for item in results),
        "start_lookback_incomplete_metrics": sorted(
            item.metric for item in results if item.start_lookback_incomplete
        ),
        "config_sha256": inputs.config_sha256,
        "metrics_yaml_sha256": inputs.catalog_sha256,
        "db_schema_version": database.schema_version(),
        "periods_sha256": inputs.periods.sha256,
        "periods": (
            None if inputs.periods.document is None else inputs.periods.document.model_dump()
        ),
        "group_by": group_by,
        "include_override": include_override,
        "calibration_changed": bool(changes),
        "calibration_changes": changes,
        "incomplete": bool(pruned),
        "covered_period": {
            "start_ms": covered_start,
            "end_ms": end_ms,
            "start": _iso(covered_start, config),
            "end": _iso(end_ms, config),
        },
        "deleted_buckets": {
            metric: {
                "count": len(buckets),
                "first": _iso(buckets[0], config),
                "last": _iso(buckets[-1], config),
            }
            for metric, buckets in sorted(pruned.items())
        },
        "clock_rewind_detection": CLOCK_NOTE,
        "clock_overlaps": len(timeline.overlaps),
        "notes": [PROVISIONAL_NOTE, *([REGIME_NOTE] if group_by == "trace-regime" else [])],
        # 除いた件数の内訳（0151 §2.8 の来歴）。黙って捨てない
        "excluded": {
            item.metric: {
                **dict(sorted(item.excluded.items())),
                "undetermined_reasons": dict(sorted(item.undetermined.items())),
            }
            for item in results
        },
    }
    return {
        "version": REPORT_VERSION,
        "provenance": provenance,
        "trace_segments": {
            "count": len(cooling.relevant_segments),
            "by_status": dict(sorted(segment_reasons.items())),
            "segments_with_seq_gaps": sum(
                1 for segment in cooling.relevant_segments if segment.seq_gaps
            ),
            "seq_gaps": sum(segment.seq_gaps for segment in cooling.relevant_segments),
            "pruned_through_seq": prune.pruned_through_seq,
            "legacy_through_seq": prune.legacy_through_seq,
        },
        "metrics": [_metric_dict(item, config) for item in results],
    }


def _groups(groups: dict[GroupKey, _Group], config: LoadDistributionConfig) -> list[dict[str, Any]]:
    return [
        {
            "cooling": key[0],
            "label": key[1],
            "period": key[2],
            "stats": summarize(group.values, config),
            "segments": {
                "count": len(group.segments),
                "longest_ms": max(group.segments, default=0),
                "observed_ms": sum(group.segments),
            },
        }
        for key, group in sorted(groups.items())
    ]


def _metric_dict(item: MetricResult, config: LoadDistributionConfig) -> dict[str, Any]:
    return {
        "metric": item.metric,
        "unit": item.unit,
        "groups": _groups(item.groups, config),
        "transitions": _groups(item.transitions, config),
        "quality": [
            {"cooling": key[0], "label": key[1], "period": key[2], **dict(sorted(counts.items()))}
            for key, counts in sorted(item.quality.items())
        ],
    }


def _iso(at_ms: int, config: LoadDistributionConfig) -> str:
    return datetime.fromtimestamp(at_ms / 1000, tz=config.zone).isoformat()


# ---------------------------------------------------------------------- 出力


def as_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def as_markdown(report: dict[str, Any]) -> str:
    """metric ごとの表と除いた件数の表。

    行 = グループ、列 = n・p50・p95・p99・max・segment の数（0151 §2.8）。
    """
    provenance = report["provenance"]
    period = provenance["period"]
    lines = [
        "# 負荷の状態ごとの分布",
        "",
        f"- 期間: {period['start']} 〜 {period['end']}",
        *(
            [
                f"- **実際に集計した期間（生データの欠けのため）**: "
                f"{provenance['covered_period']['start']} 〜 {provenance['covered_period']['end']}"
            ]
            if provenance["incomplete"]
            else []
        ),
        f"- 切り口: {provenance['group_by']}"
        + ("（fand_override を含む）" if provenance["include_override"] else ""),
        f"- 設定の SHA-256: `{provenance['config_sha256']}`",
        f"- metrics.yaml の SHA-256: `{provenance['metrics_yaml_sha256']}`",
        f"- --periods の SHA-256: `{provenance['periods_sha256'] or '—'}`",
        f"- DB のスキーマの版: {provenance['db_schema_version']}",
        f"- 区間の始めのさかのぼりが不完全: {_yes(provenance['start_lookback_incomplete'])}",
        f"- 較正の変更: {_yes(provenance['calibration_changed'])}",
        f"- 生データの欠け（incomplete）: {_yes(provenance['incomplete'])}",
        f"- {provenance['clock_rewind_detection']}",
        *(f"- {note}" for note in provenance["notes"]),
        "- **合否も閾値の提案も出さない。** 閾値の判断は人がする",
        "",
    ]
    for change in provenance["calibration_changes"]:
        lines.append(f"- 較正の変更: {change['time']} `{change['offsets_sha256']}`")
    if provenance["calibration_changes"]:
        lines.append("")
    for metric in report["metrics"]:
        lines += [f"## {metric['metric']}（{metric['unit']}）", ""]
        lines += _group_table(metric["groups"])
        if metric["transitions"]:
            # 移り変わり直後は主の分布に入れないが捨てない（0151 §2.3）。行き先の状態ごとに出す
            lines += [f"### {metric['metric']} の transition（行き先の状態ごと）", ""]
            lines += _group_table(metric["transitions"])
        if metric["quality"]:
            lines += [
                f"### {metric['metric']} の品質で除いた件数",
                "",
                "| cooling | 状態 | period | missing | suspect | stale | null | unpaired |",
                "|---|---|---|---|---|---|---|---|",
            ]
            lines += [
                f"| {item['cooling']} | {item['label']} | {item['period']} "
                + " ".join(
                    f"| {item.get(name, 0)}"
                    for name in ("missing", "suspect", "stale", "null", "unpaired")
                )
                + " |"
                for item in metric["quality"]
            ]
            lines.append("")
    lines += ["## 除いた件数", "", "| metric | 理由 | 件数 |", "|---|---|---|"]
    for metric, counts in provenance["excluded"].items():
        for reason, count in counts.items():
            if reason == "undetermined_reasons":
                for detail, detail_count in count.items():
                    lines.append(f"| {metric} | undetermined: {detail} | {detail_count} |")
            else:
                lines.append(f"| {metric} | {reason} | {count} |")
    return "\n".join(lines).rstrip() + "\n"


def _group_table(groups: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| cooling | 状態 | period | n | p50 | p95 | p99 | max | segment |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for group in groups:
        stats = group["stats"]
        quantiles = stats["quantiles"]
        lines.append(
            f"| {group['cooling']} | {group['label']} | {group['period']} | {stats['n']} "
            f"| {_num(quantiles.get('p50'))} | {_num(quantiles.get('p95'))} "
            f"| {_num(quantiles.get('p99'))} | {_num(stats['max'])} "
            f"| {group['segments']['count']} |"
        )
    if not groups:
        lines.append("| — | — | — | 0 | — | — | — | — | 0 |")
    return [*lines, ""]


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def _yes(flag: bool) -> str:
    return "あり" if flag else "なし"


# ---------------------------------------------------------------------- CLI


def _read_inputs(config_path: Path, metrics_path: Path, periods_path: Path | None) -> Inputs:
    config_bytes = config_path.read_bytes()
    config = LoadDistributionConfig.from_bytes(config_bytes)
    catalog, catalog_sha256 = MetricCatalog.from_yaml_with_sha256(metrics_path)
    config.validate_against(catalog)
    periods = (
        Periods.empty()
        if periods_path is None
        else Periods.from_bytes(periods_path.read_bytes(), config.zone)
    )
    return Inputs(
        config=config,
        config_sha256=hashlib.sha256(config_bytes).hexdigest(),
        catalog=catalog,
        catalog_sha256=catalog_sha256,
        periods=periods,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-load-distribution`。人が手で時々走らせる。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-load-distribution",
        description="負荷の状態ごとの d.* の分布（DB を読むだけ。合否・閾値の提案は出さない）",
    )
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--config", type=Path, default=Path("config/load-distribution.yaml"))
    parser.add_argument("--metrics", type=Path, default=Path("config/metrics.yaml"))
    parser.add_argument("--start", required=True, help="期間の始まり（ISO 8601）")
    parser.add_argument("--end", required=True, help="期間の終わり（ISO 8601。含まない）")
    parser.add_argument("--periods", type=Path, help="名前つきの区間・除く区間・bios_confirmed")
    parser.add_argument(
        "--group-by", choices=["state", "trace-regime"], default="state", dest="group_by"
    )
    parser.add_argument("--include-override", action="store_true", dest="include_override")
    parser.add_argument("--allow-partial", action="store_true", dest="allow_partial")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--out", type=Path, help="出力先。省けば標準出力")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logs.configure(args.log_level)

    try:
        inputs = _read_inputs(args.config, args.metrics, args.periods)
    except (OSError, ValueError, ValidationError) as error:
        LOGGER.error("設定を読めない", extra={logs.FIELDS_KEY: {"error": str(error)}})
        return 2
    if not args.db.exists():
        # **開かない。** 開くと空の DB が作られ、「データが無い」と区別できなくなる
        LOGGER.error("DB が見つからない")
        return 2
    if args.out is not None:
        protected = [args.db, args.config, args.metrics, *([args.periods] if args.periods else [])]
        if any(args.out.resolve() == path.resolve() for path in protected):
            LOGGER.error("--out が入力のファイルを指している")
            return 2
    zone = inputs.config.zone
    try:
        start_ms = parse_time(args.start, zone)
        end_ms = parse_time(args.end, zone)
    except ValueError as error:
        LOGGER.error("時刻を読めない", extra={logs.FIELDS_KEY: {"error": str(error)}})
        return 2
    if end_ms <= start_ms or end_ms - start_ms > inputs.config.max_period_days * 86_400_000:
        # 期間の指定の誤りは入力の誤り（2）。集計の拒否（1）と見分ける（0155 §2.14）
        LOGGER.error(
            "期間が空か逆転しているか、max_period_days を超える",
            extra={logs.FIELDS_KEY: {"max_period_days": inputs.config.max_period_days}},
        )
        return 2
    try:
        database = LoadDistributionDatabase(args.db)
    except SoakDatabaseError as error:
        LOGGER.error("DB を開けない", extra={logs.FIELDS_KEY: {"error": str(error)}})
        return 2
    try:
        with database, database.read_snapshot():
            report = build(
                database,
                inputs,
                start_ms=start_ms,
                end_ms=end_ms,
                group_by=args.group_by,
                include_override=args.include_override,
                allow_partial=args.allow_partial,
            )
    except LoadDistributionError as error:
        LOGGER.error("集計を拒否した", extra={logs.FIELDS_KEY: {"error": str(error)}})
        return 1
    text = as_json(report) if args.format == "json" else as_markdown(report)
    _emit(text, args.out)
    LOGGER.info(
        "負荷の状態ごとの分布を作成した",
        extra={logs.FIELDS_KEY: {"start_ms": start_ms, "end_ms": end_ms, "format": args.format}},
    )
    return 0


def _emit(text: str, out: Path | None) -> None:
    if out is None:
        # 人が読むもの。print を使わず標準出力へそのまま書く
        sys.stdout.write(text)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
