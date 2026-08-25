"""読み取り専用 REST API と WebSocket（L2）。#9

**書き込み系のエンドポイントを1つも持たない**（FR-307 / api-contract §1）。
Workspace から状態を変更できないことが、2つのリポジトリを分けている前提である。

既定で `127.0.0.1` に bind する（api-contract §5）。外部公開しない。
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect

from coldaisle.api import derived as derived_values
from coldaisle.api.metrics_meta import MetricCatalog
from coldaisle.api.models import (
    AlertsResponse,
    HealthResponse,
    LatestResponse,
    MetricValue,
    SeriesPointOut,
    SeriesResponse,
    StatsResponse,
    StreamMessage,
    iso,
)
from coldaisle.clock import Clock, WallClock
from coldaisle.store import Aggregation, Quality, QualityRules, SqliteStore
from coldaisle.store.db import FIVE_MINUTES_MS, HOUR_MS, MINUTE_MS
from coldaisle.store.models import validate_metric

DEFAULT_SAMPLE_INTERVAL_MS = 2_500
"""起動バナーを受け取れていないときの想定周期。点数の見積りにだけ使う。"""

WINDOW_PATTERN = re.compile(r"^(\d+)([smhd])$")
_WINDOW_UNITS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}

_BUCKET_MS = {
    Aggregation.MINUTE: MINUTE_MS,
    Aggregation.FIVE_MINUTES: FIVE_MINUTES_MS,
    Aggregation.HOUR: HOUR_MS,
}
_COARSER = (Aggregation.RAW, Aggregation.MINUTE, Aggregation.FIVE_MINUTES, Aggregation.HOUR)


@dataclass(frozen=True)
class Config:
    """API プロセスの設定。環境変数から作る（AGENTS.md ルール6）。

    `uvicorn coldaisle.api:app` には引数を渡せないため、CLI ではなく環境変数にする。
    """

    db: Path = Path("var/coldaisle.db")
    quality_rules: Path = Path("config/quality.yaml")
    metrics: Path = Path("config/metrics.yaml")
    max_points: int = 2_000
    """1レスポンスの最大点数。超えるなら粗い粒度へ自動で落とす（受入基準）。"""
    stream_poll_s: float = 1.0
    """WebSocket が新着を見に行く間隔。"""

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            db=Path(os.environ.get("COLDAISLE_DB", str(cls.db))),
            quality_rules=Path(os.environ.get("COLDAISLE_QUALITY_RULES", str(cls.quality_rules))),
            metrics=Path(os.environ.get("COLDAISLE_METRICS", str(cls.metrics))),
            max_points=int(os.environ.get("COLDAISLE_MAX_POINTS", cls.max_points)),
            stream_poll_s=float(os.environ.get("COLDAISLE_STREAM_POLL_S", cls.stream_poll_s)),
        )


class StoreProvider:
    """スレッドごとに接続を1本持つ。

    `SqliteStore` はインスタンスをスレッド間で共有しない約束（決定記録 0004 §2.8）。
    FastAPI の同期エンドポイントはスレッドプールで動くため、
    スレッドローカルに持つのが素直。リクエストごとに開くとマイグレーションの
    確認が毎回走る。
    """

    def __init__(self, config: Config, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._rules = QualityRules.from_yaml(config.quality_rules)
        self._local = threading.local()
        self._opened: list[SqliteStore] = []
        self._lock = threading.Lock()

    def get(self) -> SqliteStore:
        store: SqliteStore | None = getattr(self._local, "store", None)
        if store is None:
            store = SqliteStore(self._config.db, rules=self._rules, clock=self._clock)
            self._local.store = store
            with self._lock:
                self._opened.append(store)
        return store

    def close_all(self) -> None:
        """開いた接続を閉じる。**別スレッドが持つものは閉じられない。**

        `sqlite3` の接続は作ったスレッドからしか閉じられず、停止処理は
        別のスレッドで走る。ワーカースレッドが持つ接続はプロセス終了時に
        解放される。WAL はコミット済みなので、閉じ損ねてもデータは失われない。
        """
        with self._lock:
            for store in self._opened:
                with suppress(sqlite3.ProgrammingError):
                    store.close()
            self._opened.clear()


def parse_window(window: str) -> int:
    """`15m` / `2h` / `7d` をミリ秒にする。"""
    matched = WINDOW_PATTERN.match(window)
    if matched is None:
        raise HTTPException(422, f"window の書式が不正: {window!r}（例: 15m, 2h, 7d）")
    return int(matched.group(1)) * _WINDOW_UNITS[matched.group(2)]


def choose_aggregation(
    span_ms: int, requested: Aggregation | None, max_points: int, interval_ms: int
) -> tuple[Aggregation, bool]:
    """点数が上限を超えないいちばん細かい粒度を選ぶ（受入基準）。

    要求された粒度より**粗くすることはあるが細かくはしない。** 細かくすると、
    呼び出し側が想定した点数を超えて返すことになる。
    """
    start = _COARSER.index(requested) if requested is not None else 0
    for candidate in _COARSER[start:]:
        step = interval_ms if candidate is Aggregation.RAW else _BUCKET_MS[candidate]
        if span_ms // max(step, 1) <= max_points:
            return candidate, candidate is not (requested or Aggregation.RAW)
    return Aggregation.HOUR, True


def create_app(config: Config | None = None, *, clock: Clock | None = None) -> FastAPI:
    settings = config or Config.from_env()
    catalog = MetricCatalog.from_yaml(settings.metrics)
    provider = StoreProvider(settings, clock or WallClock())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            provider.close_all()

    app = FastAPI(
        title="coldaisle",
        version="1",
        description="GPUサーバー温湿度監視の読み取り専用 API。書き込み系は存在しない。",
        lifespan=lifespan,
    )
    app.state.provider = provider
    app.state.config = settings
    app.state.catalog = catalog

    def latest_payload() -> LatestResponse:
        store = provider.get()
        readings = store.latest()
        now_ms = store.clock.now_ms()
        newest = max((reading.ts_ms for reading in readings.values()), default=now_ms)
        return LatestResponse(
            ts_ms=newest,
            ts=iso(newest),
            metrics={
                metric: MetricValue(
                    value=reading.value,
                    unit=catalog.unit_for(metric),
                    quality=reading.quality,
                    age_seconds=round(reading.age_ms / 1000, 3),
                )
                for metric, reading in sorted(readings.items())
            },
            derived=derived_values.compute(readings, catalog),
            stale=any(reading.quality is Quality.STALE for reading in readings.values()),
        )

    @app.get("/api/v1/latest", response_model=LatestResponse, response_model_by_alias=True)
    def get_latest() -> LatestResponse:
        """全メトリクスの最新値・派生値・品質（FR-301）。"""
        return latest_payload()

    @app.get("/api/v1/series", response_model=SeriesResponse, response_model_by_alias=True)
    def get_series(
        metric: str,
        from_ms: int | None = Query(default=None, alias="from"),
        to_ms: int | None = Query(default=None, alias="to"),
        window: str | None = None,
        agg: Aggregation | None = None,
    ) -> SeriesResponse:
        """時系列（FR-302）。`agg` は raw / 1m / 5m / 1h。

        点数が上限を超えるときは**自動で粗い粒度へ落とす**。返す `agg` が
        実際に使った粒度で、`downsampled` が落としたかどうかを示す。
        """
        store = provider.get()
        _require_metric(metric)
        start_ms, end_ms = _resolve_range(store, from_ms, to_ms, window)
        interval_ms = _interval_ms(store)
        used, downsampled = choose_aggregation(
            end_ms - start_ms, agg, settings.max_points, interval_ms
        )
        if used is Aggregation.RAW:
            points = [
                SeriesPointOut(ts_ms=point.ts_ms, value=point.value, quality=point.quality)
                for point in store.series(metric, start_ms, end_ms, limit=settings.max_points)
            ]
        else:
            points = [
                SeriesPointOut(
                    ts_ms=bucket.bucket_ms,
                    value=bucket.mean_value,
                    min=bucket.min_value,
                    max=bucket.max_value,
                    ok_count=bucket.ok_value_count,
                    row_count=bucket.row_count,
                    missing_ratio=bucket.missing_ratio,
                )
                for bucket in store.rollup(metric, start_ms, end_ms, used)
            ]
        return SeriesResponse(
            metric=metric,
            unit=catalog.unit_for(metric),
            agg=used.value,
            downsampled=downsampled,
            from_ms=start_ms,
            to_ms=end_ms,
            points=points,
        )

    @app.get("/api/v1/stats", response_model=StatsResponse, response_model_by_alias=True)
    def get_stats(
        metric: str,
        from_ms: int | None = Query(default=None, alias="from"),
        to_ms: int | None = Query(default=None, alias="to"),
        window: str | None = None,
    ) -> StatsResponse:
        """min / max / mean / p95 / 傾き / 欠測率（FR-303）。

        **常に生データから計算する。** 分位数はロールアップから合成できない
        （決定記録 0004 §2.9）。
        """
        store = provider.get()
        _require_metric(metric)
        start_ms, end_ms = _resolve_range(store, from_ms, to_ms, window)
        stats = store.stats(metric, start_ms, end_ms)
        return StatsResponse(
            metric=metric,
            unit=catalog.unit_for(metric),
            from_ms=stats.start_ms,
            to_ms=stats.end_ms,
            row_count=stats.row_count,
            ok_count=stats.ok_value_count,
            min=stats.min_value,
            max=stats.max_value,
            mean=stats.mean_value,
            p95=stats.p95_value,
            slope_per_min=stats.slope_per_min,
            missing_ratio=stats.missing_ratio,
        )

    @app.get("/api/v1/alerts", response_model=AlertsResponse)
    def get_alerts(
        state: str | None = None,
        from_ms: int | None = Query(default=None, alias="from"),
        to_ms: int | None = Query(default=None, alias="to"),
        limit: int = Query(default=100, ge=1, le=1_000),
    ) -> AlertsResponse:
        """アラート一覧（FR-304）。新しい順。"""
        store = provider.get()
        return AlertsResponse(
            alerts=list(store.alerts(state=state, start_ms=from_ms, end_ms=to_ms, limit=limit))
        )

    @app.get("/api/v1/health", response_model=HealthResponse)
    def get_health() -> HealthResponse:
        """デーモンの稼働状況（FR-305）。**古いときに ok を返さない。**"""
        store = provider.get()
        readings = store.latest()
        now_ms = store.clock.now_ms()
        newest = max((reading.ts_ms for reading in readings.values()), default=None)
        stale = any(reading.quality is Quality.STALE for reading in readings.values())
        return HealthResponse(
            ok=bool(readings) and not stale,
            source=store.current_state("sys.ingest_source"),
            last_sample_at=None if newest is None else iso(newest),
            last_sample_ts_ms=newest,
            data_age_seconds=None if newest is None else round((now_ms - newest) / 1000, 3),
            stale=stale,
            metrics=len(readings),
            missing_ratio_1h=_missing_ratio_1h(store, now_ms),
        )

    @app.websocket("/api/v1/stream")
    async def stream(websocket: WebSocket) -> None:
        """新しいサンプルを押し出す（FR-306）。

        取り込みは別プロセスなので、**DB を見に行って変化したら送る。**
        プロセスをまたぐ通知の仕組みを足すより、1秒ごとの問い合わせのほうが
        止まりにくい（最新値の取得はメトリクス数に比例する。決定記録 0004 §2.11）。
        """
        await websocket.accept()
        last_sent: int | None = None
        try:
            while True:
                payload = await asyncio.to_thread(latest_payload)
                if payload.ts_ms != last_sent:
                    last_sent = payload.ts_ms
                    await websocket.send_json(
                        StreamMessage(latest=payload).model_dump(mode="json", by_alias=True)
                    )
                await asyncio.sleep(settings.stream_poll_s)
        except WebSocketDisconnect:  # pragma: no cover - 切断はクライアント都合
            return

    def _interval_ms(store: SqliteStore) -> int:
        device = next(
            (
                record
                for record in (store.device(row) for row in _device_ids(store))
                if record is not None and record.interval_ms
            ),
            None,
        )
        return DEFAULT_SAMPLE_INTERVAL_MS if device is None else int(device.interval_ms or 0)

    def _device_ids(store: SqliteStore) -> list[str]:
        return [
            str(row[0])
            for row in store.connection.execute(
                "SELECT device_id FROM devices ORDER BY last_hello_ms DESC LIMIT 1"
            )
        ]

    def _resolve_range(
        store: SqliteStore, from_ms: int | None, to_ms: int | None, window: str | None
    ) -> tuple[int, int]:
        """`from` / `to` か `window` のどちらかで範囲を決める。

        範囲は `[from, to)`（決定記録 0004 §2.1）。`window` は「今から遡って」。
        """
        if window is not None:
            end = store.clock.now_ms() if to_ms is None else to_ms
            return end - parse_window(window), end
        if from_ms is None or to_ms is None:
            raise HTTPException(422, "from と to、または window を指定する")
        if from_ms > to_ms:
            raise HTTPException(422, f"範囲が逆転している: from={from_ms} > to={to_ms}")
        return from_ms, to_ms

    def _missing_ratio_1h(store: SqliteStore, now_ms: int) -> float | None:
        row = store.connection.execute(
            "SELECT SUM(ok_value_count), SUM(COALESCE(expected_count, row_count)) "
            "FROM readings_1m WHERE bucket_ms >= ?",
            (now_ms - HOUR_MS,),
        ).fetchone()
        if row is None or row[1] in (None, 0):
            return None
        return round(1 - float(row[0] or 0) / float(row[1]), 4)

    return app


def _require_metric(metric: str) -> None:
    try:
        validate_metric(metric)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
