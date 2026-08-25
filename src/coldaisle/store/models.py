"""コアデータモデル。

命名規約と保存の不変条件は決定記録 0002、非有限値の扱いは 0003 §2.8 に従う。
DB の CHECK 制約と二重に持たせている。取り込み層の不具合を書き込み前に落とすため。

書き込み側（`Sample` / `Reading`）と読み出し側（`LatestReading` / `SeriesPoint` /
`RollupPoint` / `Stats`）を同じ場所に置く。上位レイヤは `coldaisle.store` から
これらを受け取るだけでよく、SQL の行形式を知らずに済む。
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

METRIC_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.([a-z][a-z0-9_]*|[0-9]+)){1,3}$")
"""決定記録 0002 §2.1 の文法。`gpu.0.core` のような添字セグメントを許す。"""

DERIVED_PREFIX = "d."
"""派生メトリクスの予約プレフィクス。保存しない（決定記録 0002 §2.2）。"""


def validate_metric(name: str) -> str:
    """メトリクス名を検証して返す。規約に合わなければ `ValueError`。

    保存する `Reading` と、照会の引数を受け取る `SqliteStore` の両方から呼ぶ。
    検証を書き込み側だけに置くと、保存できない名前で照会できてしまい、
    「0件」なのか「そもそも存在しえない名前」なのかを呼び出し側が区別できない。
    """
    if name.startswith(DERIVED_PREFIX):
        raise ValueError(f"派生メトリクスは保存しない（決定記録 0002 §2.2）: {name!r}")
    if not METRIC_PATTERN.match(name):
        raise ValueError(f"命名規約に合わない（決定記録 0002 §2.1）: {name!r}")
    return name


class Quality(StrEnum):
    """測定値の品質（要件 §5.3、決定記録 0002 §2.5）。"""

    OK = "ok"
    MISSING = "missing"
    SUSPECT = "suspect"
    STALE = "stale"


class Reading(BaseModel):
    """1メトリクスの1測定値。`readings` テーブルの1行に対応する。"""

    model_config = ConfigDict(frozen=True)

    metric: str
    value: float | None = Field(default=None, allow_inf_nan=False)
    quality: Quality

    @field_validator("metric")
    @classmethod
    def _validate_metric(cls, value: str) -> str:
        return validate_metric(value)


class Sample(BaseModel):
    """デバイスの1サンプルを正規化したもの。

    含まれる Reading はすべて同一の `ts_ms` を持つ（決定記録 0002 §2.3）。
    この規則が破れると同時刻の横串が取れなくなるため、時刻は Sample が1つだけ持つ。
    """

    model_config = ConfigDict(frozen=True)

    ts_ms: int = Field(ge=0)
    readings: tuple[Reading, ...]
    seq: int | None = Field(default=None, ge=0)
    """デバイスの連番。取りこぼし検出に使う（FR-105）。mock / replay では None。"""
    up_ms: int | None = Field(default=None, ge=0)
    """デバイス稼働ミリ秒。巻き戻り＝再起動（FR-106）。"""

    @field_validator("readings")
    @classmethod
    def _validate_readings(cls, value: tuple[Reading, ...]) -> tuple[Reading, ...]:
        metrics = [reading.metric for reading in value]
        duplicates = {m for m in metrics if metrics.count(m) > 1}
        if duplicates:
            raise ValueError(f"同一サンプル内でメトリクスが重複している: {sorted(duplicates)}")
        return value


class LatestReading(BaseModel):
    """`v_latest` の1行に、読み出し時点の `stale` 判定を適用したもの（FR-301）。"""

    model_config = ConfigDict(frozen=True)

    metric: str
    ts_ms: int
    value: float | None
    quality: Quality
    age_ms: int
    """`ts_ms` から読み出し時刻までの経過。API の `data_age_seconds` の元になる。"""


class SeriesPoint(BaseModel):
    """生データ1点（`agg=raw`、FR-302）。品質を落とさずそのまま返す。

    `suspect` を呼び出し側で除外できるように `quality` を持つ。
    ここで間引くと、グラフに出ない理由が層をまたいで見えなくなる。
    """

    model_config = ConfigDict(frozen=True)

    ts_ms: int
    value: float | None
    quality: Quality


class RollupPoint(BaseModel):
    """ロールアップ1バケット（`agg=1m` / `1h`）。列の意味は決定記録 0002 §2.8。"""

    model_config = ConfigDict(frozen=True)

    bucket_ms: int
    min_value: float | None
    max_value: float | None
    mean_value: float | None
    ok_value_count: int
    row_count: int
    expected_count: int | None

    @property
    def missing_ratio(self) -> float | None:
        """欠測率。決定記録 0002 §2.8 の式をここ1箇所に持つ。

        `expected_count` が無い（起動バナー未受信）バケットでは `row_count` を
        母数にするため、**届かなかったサンプルを数えられない下限値**になる。
        """
        denominator = self.expected_count if self.expected_count is not None else self.row_count
        if denominator <= 0:
            return None
        return 1.0 - self.ok_value_count / denominator


class Stats(BaseModel):
    """`stats()` の返り値（FR-303）。

    統計量は `quality='ok'` かつ値を持つ行のみから計算する（決定記録 0002 §2.8）。
    `suspect` を混ぜると、センサーの人工物がそのまま統計に乗る。
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    start_ms: int
    end_ms: int
    row_count: int
    """窓に入った全行数（品質を問わない）。欠測率の母数。"""
    ok_value_count: int
    """統計量の母数。`min` 〜 `p95` はこの行だけから計算されている。"""
    min_value: float | None
    max_value: float | None
    mean_value: float | None
    p95_value: float | None
    slope_per_min: float | None
    """最小二乗法による傾き。単位は「メトリクスの単位 / 分」（FR-407 の上昇率）。"""
    missing_ratio: float | None
    """`1 - ok_value_count / row_count`。窓に1行も無ければ None。"""


class DeviceRecord(BaseModel):
    """`devices` の1行（決定記録 0002 §2.10）。起動バナー由来。

    L0 の `RawHello` をそのまま受け取らないのは、下位レイヤの型を L1 が
    知らないようにするため（AGENTS.md の一方向依存）。詰め替えは取り込み側が行う。
    """

    model_config = ConfigDict(frozen=True)

    device_id: str
    fw: str | None = None
    schema_v: int | None = None
    interval_ms: int | None = Field(default=None, gt=0)
    """送信周期。`expected_count`（決定記録 0002 §2.8）の算出に使う。"""


class SensorRecord(BaseModel):
    """`device_sensors` の1行。ROM の変化が `PROBE_CHANGED`（FR-403）の根拠になる。"""

    model_config = ConfigDict(frozen=True)

    channel: str
    kind: str
    gpio: int | None = None
    rom: str | None = None
    resolution: int | None = None


class AlertSeverity(StrEnum):
    """`alerts.severity`（決定記録 0002 §2.9）。"""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(StrEnum):
    """状態機械 `OK → PENDING → FIRING → RESOLVED`（要件 §6.4）。"""

    PENDING = "pending"
    FIRING = "firing"
    RESOLVED = "resolved"


class AlertRecord(BaseModel):
    """`alerts` の1行。書き込むのはルールエンジン（#18）。"""

    model_config = ConfigDict(frozen=True)

    id: int
    rule_id: str
    severity: AlertSeverity
    state: AlertState
    metric: str | None = None
    started_ms: int
    fired_ms: int | None = None
    resolved_ms: int | None = None
    trigger_value: float | None = None
    threshold: float | None = None
    """発火時に適用されていた閾値。**当時の閾値で解釈できないと履歴が読めない**（0002 §2.9）。"""
    detail: str | None = None
