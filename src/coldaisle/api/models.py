"""API のレスポンス型（L2）。#9

**時刻は2つの形で返す。** `ts_ms`（Unix ミリ秒、保存と同じ値）と
`ts`（ISO8601・UTC）。前者は計算用、後者は人間とログ用。
表示のためのローカル時刻への変換はクライアント側で行う
（api-contract の例は JST だが、サーバはタイムゾーンを持たない）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.store.models import AlertRecord, Quality


def iso(ts_ms: int) -> str:
    """Unix ミリ秒を ISO8601（UTC）にする。"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat()


class MetricValue(BaseModel):
    """1メトリクスの現在値（api-contract §3 の `metrics` に対応）。"""

    model_config = ConfigDict(frozen=True)

    value: float | None
    unit: str | None
    quality: Quality
    age_seconds: float
    """`ts_ms` から応答時刻までの経過。**負なら時計がずれている**（#42）。"""


class LatestResponse(BaseModel):
    """`GET /api/v1/latest`（FR-301）。"""

    model_config = ConfigDict(frozen=True)

    ts_ms: int
    ts: str
    metrics: dict[str, MetricValue]
    derived: dict[str, float | None]
    """派生値（決定記録 0002 §2.2 により保存しない）。計算できなければ `null`。"""
    stale: bool


class SeriesPointOut(BaseModel):
    """時系列の1点。`agg` によって埋まる項目が変わる。

    `agg=raw` では `value` と `quality`、集計では `value`（平均）と
    `min` / `max` / `missing_ratio` が入る。
    """

    model_config = ConfigDict(frozen=True)

    ts_ms: int
    value: float | None
    quality: Quality | None = None
    min: float | None = None
    max: float | None = None
    ok_count: int | None = None
    row_count: int | None = None
    missing_ratio: float | None = None


class SeriesResponse(BaseModel):
    """`GET /api/v1/series`（FR-302）。"""

    model_config = ConfigDict(frozen=True)

    metric: str
    unit: str | None
    agg: str
    """**実際に使った粒度。** 要求より粗くなることがある（`downsampled`）。"""
    downsampled: bool
    from_ms: int = Field(serialization_alias="from")
    to_ms: int = Field(serialization_alias="to")
    points: list[SeriesPointOut]


class StatsResponse(BaseModel):
    """`GET /api/v1/stats`（FR-303）。統計は `quality='ok'` の行のみ（決定記録 0002 §2.8）。"""

    model_config = ConfigDict(frozen=True)

    metric: str
    unit: str | None
    from_ms: int = Field(serialization_alias="from")
    to_ms: int = Field(serialization_alias="to")
    row_count: int
    ok_count: int
    min: float | None
    max: float | None
    mean: float | None
    p95: float | None
    """nearest-rank。**補間しない**（決定記録 0004 §2.3）。"""
    slope_per_min: float | None
    missing_ratio: float | None
    """生データ窓の下限値。通信断は含まない（決定記録 0004 §2.5）。"""


class AlertsResponse(BaseModel):
    """`GET /api/v1/alerts`（FR-304）。書き込むのはルールエンジン（#18）。"""

    model_config = ConfigDict(frozen=True)

    alerts: list[AlertRecord]


class HealthResponse(BaseModel):
    """`GET /api/v1/health`（FR-305）。

    **データが古いときに `ok` を返さない。** 無音で古い値を出すのが
    監視システムの最悪の失敗である（api-contract §3）。
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    source: str | None
    """取り込みソース種別。デーモンが記録する（`sys.ingest_source`）。"""
    last_sample_at: str | None
    last_sample_ts_ms: int | None
    data_age_seconds: float | None
    stale: bool
    metrics: int
    missing_ratio_1h: float | None
    """直近1時間の欠測率。1分ロールアップから出す（決定記録 0002 §2.8）。"""


class StreamMessage(BaseModel):
    """`WS /api/v1/stream`（FR-306）が押し出す1件。"""

    model_config = ConfigDict(frozen=True)

    type: str = "latest"
    latest: LatestResponse
