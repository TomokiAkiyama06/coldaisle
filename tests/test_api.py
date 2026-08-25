"""読み取り専用 API と WebSocket（#9）。

受入基準は2つ。**書き込み系が1つも無いこと**と、
**`series` が点数を制限して自動でダウンサンプルすること**。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coldaisle.api.app import Config, choose_aggregation, create_app, parse_window
from coldaisle.api.metrics_meta import MetricCatalog
from coldaisle.clock import SimulatedClock
from coldaisle.store import Aggregation, DeviceRecord, Quality, Reading, Sample, SqliteStore
from coldaisle.store.rollup import rollup_minutes
from conftest import CONFIG_DIR, QUALITY_RULES_PATH

METRICS_PATH = CONFIG_DIR / "metrics.yaml"
NOW_MS = 1_787_616_000_000
MINUTE_MS = 60_000
INTERVAL_MS = 2_500


@pytest.fixture
def clock() -> SimulatedClock:
    return SimulatedClock(NOW_MS)


@pytest.fixture
def db(tmp_path, rules, clock) -> Path:
    """`ramp` 相当の生データを10分ぶん入れた DB。"""
    path = tmp_path / "api.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(0)) as store:
        store.record_hello(
            DeviceRecord(device_id="dev", fw="1.0.0", schema_v=1, interval_ms=INTERVAL_MS),
            [],
            at_ms=NOW_MS - 10 * MINUTE_MS,
        )
        store.set_system_state("sys.ingest_source", "mock", at_ms=NOW_MS - 10 * MINUTE_MS)
        samples = []
        for step in range(240):  # 10分ぶん
            ts = NOW_MS - 10 * MINUTE_MS + step * INTERVAL_MS
            samples.append(
                Sample(
                    ts_ms=ts,
                    readings=(
                        Reading(metric="air.room", value=26.0, quality=Quality.OK),
                        Reading(metric="air.front_intake", value=27.4, quality=Quality.OK),
                        Reading(
                            metric="air.gpu_intake", value=28.0 + step / 100, quality=Quality.OK
                        ),
                        Reading(
                            metric="air.gpu_exhaust", value=29.0 + step / 50, quality=Quality.OK
                        ),
                    ),
                )
            )
        store.insert_samples(samples)
        rollup_minutes(store)
    return path


@pytest.fixture
def client(db, clock):
    app = create_app(
        Config(db=db, quality_rules=QUALITY_RULES_PATH, metrics=METRICS_PATH, max_points=100),
        clock=clock,
    )
    with TestClient(app) as opened:
        yield opened


# ---------------------------------------------------------------- 読み取り専用（受入基準）


def test_no_write_endpoints_exist(client):
    """受入基準: POST / PUT / DELETE のエンドポイントが1つも存在しない（FR-307）。

    Workspace から状態を変更できないことが、2つのリポジトリを分けている前提。
    """
    paths = client.get("/openapi.json").json()["paths"]
    methods = {method for operations in paths.values() for method in operations}
    assert methods == {"get"}, f"書き込み系が入り込んでいる: {sorted(methods - {'get'})}"


def test_openapi_is_generated(client):
    """OpenAPI が自動生成される（後で AI ツール定義に流用する）。"""
    document = client.get("/openapi.json").json()
    assert document["info"]["title"] == "coldaisle"
    assert "/api/v1/latest" in document["paths"]


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_write_methods_are_rejected(method, client):
    assert getattr(client, method)("/api/v1/latest").status_code == 405


# ---------------------------------------------------------------- latest（FR-301）


def test_latest_returns_values_units_and_quality(client):
    body = client.get("/api/v1/latest").json()
    assert body["metrics"]["air.room"]["value"] == 26.0
    assert body["metrics"]["air.room"]["unit"] == "C"
    assert body["metrics"]["air.room"]["quality"] == "ok"
    assert body["ts"].endswith("+00:00"), "時刻は UTC の ISO8601"
    assert body["stale"] is False


def test_latest_computes_derived_values(client):
    """派生値は保存せず計算する（決定記録 0002 §2.2）。"""
    body = client.get("/api/v1/latest").json()
    assert body["derived"]["d.intake_rise"] == pytest.approx(1.4)  # front_intake - room
    assert body["derived"]["d.gpu_internal_delta"] is None, "GPU 内部温度はまだ取り込んでいない"


def test_derived_value_is_null_when_an_input_is_not_ok(tmp_path, rules, clock):
    """**疑わしい値を引き算しない。** もっともらしい数値が出て故障を隠す。"""
    path = tmp_path / "suspect.db"
    with SqliteStore(path, rules=rules, clock=clock) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS,
                readings=(
                    Reading(metric="air.room", value=26.0, quality=Quality.OK),
                    Reading(metric="air.front_intake", value=-127.0, quality=Quality.SUSPECT),
                ),
            )
        )
    app = create_app(
        Config(db=path, quality_rules=QUALITY_RULES_PATH, metrics=METRICS_PATH), clock=clock
    )
    with TestClient(app) as client:
        body = client.get("/api/v1/latest").json()
    assert body["derived"]["d.intake_rise"] is None
    assert body["metrics"]["air.front_intake"]["value"] == -127.0, "測定値そのものは返す"


def test_stale_data_is_reported(db, rules):
    """**古いときに ok を返さない**（api-contract §3）。"""
    late = SimulatedClock(NOW_MS + 60_000)
    app = create_app(
        Config(db=db, quality_rules=QUALITY_RULES_PATH, metrics=METRICS_PATH), clock=late
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/latest").json()["stale"] is True
        health = client.get("/api/v1/health").json()
    assert health["ok"] is False
    assert health["stale"] is True
    assert health["data_age_seconds"] > 10


# ---------------------------------------------------------------- series（FR-302）


def test_series_returns_raw_points(client):
    body = client.get(
        "/api/v1/series",
        params={"metric": "air.room", "from": NOW_MS - 2 * MINUTE_MS, "to": NOW_MS},
    ).json()
    assert body["agg"] == "raw"
    assert body["unit"] == "C"
    assert body["downsampled"] is False
    assert len(body["points"]) == 48
    assert body["points"][0]["quality"] == "ok"


def test_series_downsamples_when_there_are_too_many_points(client):
    """受入基準: 最大点数を制限し、超過時は自動でダウンサンプルする。"""
    body = client.get("/api/v1/series", params={"metric": "air.room", "window": "10m"}).json()
    assert body["agg"] == "1m", "生のままだと240点で上限（100）を超える"
    assert body["downsampled"] is True
    assert len(body["points"]) <= 100
    assert body["points"][0]["min"] is not None, "集計では min / max も返す"


def test_series_never_returns_a_finer_aggregation_than_requested(client):
    """要求より粗くはするが、細かくはしない。"""
    body = client.get(
        "/api/v1/series", params={"metric": "air.room", "window": "10m", "agg": "1h"}
    ).json()
    assert body["agg"] == "1h"


def test_series_rejects_unknown_metric_names(client):
    assert (
        client.get("/api/v1/series", params={"metric": "Air.Room", "window": "1m"}).status_code
        == 422
    )


def test_series_requires_a_range(client):
    assert client.get("/api/v1/series", params={"metric": "air.room"}).status_code == 422


def test_series_rejects_a_reversed_range(client):
    response = client.get(
        "/api/v1/series", params={"metric": "air.room", "from": NOW_MS, "to": NOW_MS - 1}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("window", ["10", "10x", "m", ""])
def test_invalid_window_is_rejected(window, client):
    response = client.get("/api/v1/series", params={"metric": "air.room", "window": window})
    assert response.status_code == 422


# ---------------------------------------------------------------- stats / alerts / health


def test_stats_uses_raw_data(client):
    body = client.get("/api/v1/stats", params={"metric": "air.gpu_exhaust", "window": "10m"}).json()
    assert body["ok_count"] == 240
    assert body["min"] == pytest.approx(29.0)
    assert body["p95"] is not None
    assert body["slope_per_min"] > 0, "ramp なので上昇している"
    assert body["missing_ratio"] == 0.0


def test_alerts_are_empty_until_the_rule_engine_exists(client):
    """表はあるが書き手はまだいない（#18）。"""
    assert client.get("/api/v1/alerts").json() == {"alerts": []}


def test_health_reports_the_ingest_source(client):
    """FR-305。ソース種別はデーモンが `system_state` に記録する。"""
    body = client.get("/api/v1/health").json()
    assert body["ok"] is True
    assert body["source"] == "mock"
    assert body["metrics"] == 4
    assert body["last_sample_at"].endswith("+00:00")
    assert body["missing_ratio_1h"] is not None


# ---------------------------------------------------------------- WebSocket（FR-306）


def test_stream_pushes_the_latest_sample(client):
    with client.websocket_connect("/api/v1/stream") as socket:
        message = socket.receive_json()
    assert message["type"] == "latest"
    assert message["latest"]["metrics"]["air.room"]["value"] == 26.0


# ---------------------------------------------------------------- 単体


@pytest.mark.parametrize(
    ("window", "expected"),
    [("30s", 30_000), ("15m", 900_000), ("2h", 7_200_000), ("7d", 604_800_000)],
)
def test_parse_window(window, expected):
    assert parse_window(window) == expected


@pytest.mark.parametrize(
    ("span_ms", "requested", "expected"),
    [
        (60_000, None, Aggregation.RAW),
        (24 * 3_600_000, None, Aggregation.HOUR),
        (3_600_000, None, Aggregation.MINUTE),
        (60_000, Aggregation.HOUR, Aggregation.HOUR),
    ],
)
def test_choose_aggregation(span_ms, requested, expected):
    used, _ = choose_aggregation(span_ms, requested, max_points=100, interval_ms=INTERVAL_MS)
    assert used is expected


def test_metrics_catalog_covers_the_required_derived_values():
    """要件 §5.1 の派生値がすべて定義されていること。"""
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    assert set(catalog.derived) == {
        "d.intake_rise",
        "d.gpu_preheat",
        "d.gpu_delta",
        "d.top_rise",
        "d.gpu_internal_delta",
    }
    assert catalog.derived["d.intake_rise"].minuend == "air.front_intake"
    assert catalog.derived["d.intake_rise"].subtrahend == "air.room"


def test_catalog_rejects_a_derived_name_without_the_prefix(tmp_path):
    path = tmp_path / "metrics.yaml"
    path.write_text(
        "derived:\n  intake_rise:\n    unit: C\n    label: x\n"
        "    minuend: air.front_intake\n    subtrahend: air.room\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"`d\.`"):
        MetricCatalog.from_yaml(path)


def _insert_alert(store, alert_id: int, *, state: str, started_ms: int, severity="warning") -> None:
    """ルールエンジン（#18）が書く行を、まだ無いので手で入れる。"""
    store.connection.execute(
        "INSERT INTO alerts (id, rule_id, severity, state, metric, started_ms, threshold) "
        "VALUES (?, 'RECIRCULATION', ?, ?, 'air.front_intake', ?, 5.0)",
        (alert_id, severity, state, started_ms),
    )


def test_alerts_can_be_filtered(db, rules, clock):
    with SqliteStore(db, rules=rules, clock=clock) as store:
        _insert_alert(store, 1, state="resolved", started_ms=NOW_MS - 3 * MINUTE_MS)
        _insert_alert(store, 2, state="firing", started_ms=NOW_MS - MINUTE_MS)
        _insert_alert(store, 3, state="firing", started_ms=NOW_MS)

    app = create_app(
        Config(db=db, quality_rules=QUALITY_RULES_PATH, metrics=METRICS_PATH), clock=clock
    )
    with TestClient(app) as client:
        firing = client.get("/api/v1/alerts", params={"state": "firing"}).json()["alerts"]
        window = client.get(
            "/api/v1/alerts", params={"from": NOW_MS - 2 * MINUTE_MS, "to": NOW_MS}
        ).json()["alerts"]
        limited = client.get("/api/v1/alerts", params={"limit": 1}).json()["alerts"]

    assert [alert["id"] for alert in firing] == [3, 2], "新しい順"
    assert [alert["id"] for alert in window] == [2]
    assert [alert["id"] for alert in limited] == [3]
    assert firing[0]["threshold"] == 5.0, "当時の閾値（決定記録 0002 §2.9）"
