"""decision trace の読み取り API（#106 / 決定記録 0071）。

- 保存: `seq`（記録した順）と削除の境界 `control_trace_prune`（0071 §2.2a）
- API: `GET /api/v1/control/latest` と `GET /api/v1/control/traces`（0071 §2.2）、
  `GET /api/v1/events/{id}`（0071 §2.7）
- 本文は保存した JSON をそのまま返す。v1〜v9 の fixture で確かめる（0071 §2.3 / §2.4）
- API は `coldaisle.control` を import しない。AI ツールに trace を足さない（0071 §2.4 / §2.8）
"""

from __future__ import annotations

import ast
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coldaisle.ai.tools import DEFINITIONS
from coldaisle.api.app import Config, create_app
from coldaisle.clock import SimulatedClock
from coldaisle.control.schema import SCHEMA_VERSION, ControlTick
from coldaisle.store import (
    ControlTraceCursorPrunedError,
    EventRecord,
    QualityRules,
    SqliteStore,
    migrations,
)
from coldaisle.store.rollup import DAY_MS, RetentionRules, run
from conftest import CONFIG_DIR, QUALITY_RULES_PATH

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
METRICS_PATH = CONFIG_DIR / "metrics.yaml"
NOW_MS = 1_787_616_000_000
VERSIONS = tuple(range(1, SCHEMA_VERSION + 1))


def fixture_text(version: int) -> str:
    return (FIXTURES / f"control_tick_v{version}.json").read_text(encoding="utf-8")


def record(
    store: SqliteStore, ts_ms: int, tick_id: int, body: dict[str, Any] | None = None
) -> bool:
    return store.record_control_trace(
        ts_ms=ts_ms,
        tick_id=tick_id,
        schema_version=9,
        trace_json=json.dumps(body if body is not None else {"tick_id": tick_id}),
    )


@pytest.fixture
def clock() -> SimulatedClock:
    return SimulatedClock(NOW_MS)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "trace.db"


@pytest.fixture
def store(db: Path, rules: QualityRules, clock: SimulatedClock) -> Iterator[SqliteStore]:
    with SqliteStore(db, rules=rules, clock=clock) as opened:
        yield opened


def make_client(db: Path, clock: SimulatedClock, **overrides: Any) -> TestClient:
    app = create_app(
        Config(db=db, quality_rules=QUALITY_RULES_PATH, metrics=METRICS_PATH, **overrides),
        clock=clock,
    )
    return TestClient(app)


@pytest.fixture
def client(store: SqliteStore, db: Path, clock: SimulatedClock) -> Iterator[TestClient]:
    with make_client(db, clock) as opened:
        yield opened


def seqs(body: dict[str, Any]) -> list[int]:
    return [trace["seq"] for trace in body["traces"]]


# ---------------------------------------------------------------- fixture（v1〜v9）


@pytest.mark.parametrize("version", VERSIONS)
def test_every_stored_version_has_a_fixture_that_still_loads(version: int) -> None:
    """版ごとの fixture が、その版を名乗る正しい記録であること（画面の変換の試験にも使う）。"""
    tick = ControlTick.model_validate_json(fixture_text(version))
    assert tick.schema_version == version


# ---------------------------------------------------------------- 保存: seq と削除の境界


def test_seq_follows_the_insertion_order_not_the_wall_clock(store: SqliteStore) -> None:
    """壁時計が戻っても、記録した順に番号が増える（0071 §2.2a / §4 N）。"""
    assert record(store, NOW_MS, 1)
    assert record(store, NOW_MS - 60_000, 2)  # 時計が戻った後の行
    rows = store.control_trace_page(0, NOW_MS + 1, after_seq=None, limit=10).traces
    assert [(row.seq, row.tick_id) for row in rows] == [(1, 1), (2, 2)]


def test_a_duplicate_is_ignored_and_does_not_advance_the_sequence(store: SqliteStore) -> None:
    assert record(store, NOW_MS, 1)
    assert not record(store, NOW_MS, 1, {"other": True}), "同じ trace を上書きしない（0030 §2）"
    assert record(store, NOW_MS + 1, 2)
    latest = store.latest_control_trace()
    assert latest is not None and latest.seq == 2
    assert json.loads(store.control_traces(NOW_MS, NOW_MS + 1)[0].trace_json) == {"tick_id": 1}


def test_seq_never_rewinds_after_every_row_is_pruned(store: SqliteStore) -> None:
    """表が全部消えても番号は巻き戻らない。古い cursor が新しい行を指さない（0071 §4 O）。"""
    for tick in range(3):
        record(store, NOW_MS + tick, tick)
    assert store.delete_control_traces_before(NOW_MS + 10) == 3
    assert store.control_trace_prune_state().pruned_through_seq == 3
    record(store, NOW_MS + 20, 9)
    latest = store.latest_control_trace()
    assert latest is not None and latest.seq == 4


def test_prune_advances_the_boundary_and_never_moves_it_back(store: SqliteStore) -> None:
    record(store, NOW_MS + 5_000, 1)  # 時計が未来へ跳んで書かれた行
    record(store, NOW_MS - 5_000, 2)
    record(store, NOW_MS + 1_000, 3)
    initial = store.control_trace_prune_state()
    assert initial.pruned_through_seq == 0 and initial.pruned_before_ms is None

    assert store.delete_control_traces_before(NOW_MS) == 1
    state = store.control_trace_prune_state()
    assert state.pruned_through_seq == 2, "消した行の MAX(seq)。先頭の連続に限らない（§4 Q）"
    assert state.pruned_before_ms == NOW_MS

    assert store.delete_control_traces_before(NOW_MS - DAY_MS) == 0
    state = store.control_trace_prune_state()
    assert state.pruned_through_seq == 2 and state.pruned_before_ms == NOW_MS, "前へ戻さない"


@pytest.mark.parametrize(
    "assignment",
    [
        "pruned_through_seq = 0",
        "pruned_before_ms = NULL",
        "pruned_before_ms = 0",
        "legacy_until_ms = 0",
    ],
)
def test_the_database_refuses_to_rewind_the_boundary(store: SqliteStore, assignment: str) -> None:
    record(store, NOW_MS - 1, 1)
    store.delete_control_traces_before(NOW_MS)
    with pytest.raises(sqlite3.IntegrityError, match="backwards"):
        store.connection.execute(f"UPDATE control_trace_prune SET {assignment}")


def test_an_operator_may_move_legacy_until_later(store: SqliteStore) -> None:
    """`legacy_until_ms` を後ろへずらすのは安全側なので許す（0071 §2.2a）。"""
    store.connection.execute("UPDATE control_trace_prune SET legacy_until_ms = legacy_until_ms + 1")
    assert store.control_trace_prune_state().legacy_until_ms == NOW_MS + 1


def test_the_boundary_row_cannot_be_deleted(store: SqliteStore) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        store.connection.execute("DELETE FROM control_trace_prune")


def test_the_database_also_rejects_non_object_trace_json(store: SqliteStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO control_traces (seq, ts_ms, tick_id, schema_version, trace_json) "
            "VALUES (1, 0, 0, 1, '[]')"
        )


def _database_at_version_6(path: Path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN")
        for migration in migrations.discover()[:6]:
            for statement in migrations._statements(migration.read_sql()):
                conn.execute(statement, {"now_ms": 0})
            conn.execute(
                "INSERT INTO schema_version (version, applied_ms) VALUES (?, 0)",
                (migration.version,),
            )
        conn.executemany(
            "INSERT INTO control_traces (ts_ms, tick_id, schema_version, trace_json) "
            "VALUES (?, ?, ?, ?)",
            [
                (NOW_MS + 9_000, 1, 1, fixture_text(1)),  # 移行前に時計が進んでいた行
                (NOW_MS - 2_000, 7, 3, fixture_text(3)),
                (NOW_MS - 2_000, 5, 2, fixture_text(2)),
            ],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_migration_numbers_existing_rows_and_marks_the_legacy_period(
    db: Path, rules: QualityRules, clock: SimulatedClock
) -> None:
    """既存の行は `(ts_ms, tick_id)` の順に 1 から。移行前の期間はまとめて不明（0071 §2.2a）。"""
    _database_at_version_6(db)
    with SqliteStore(db, rules=rules, clock=clock) as migrated:
        page = migrated.control_trace_page(0, NOW_MS + DAY_MS, after_seq=None, limit=10)
        assert [(row.seq, row.ts_ms, row.tick_id) for row in page.traces] == [
            (1, NOW_MS - 2_000, 5),
            (2, NOW_MS - 2_000, 7),
            (3, NOW_MS + 9_000, 1),
        ]
        state = migrated.control_trace_prune_state()
        assert state.legacy_until_ms == NOW_MS + 9_000, "壁時計と MAX(ts_ms) の大きいほう"
        assert state.pruned_through_seq == 0 and state.pruned_before_ms is None
        assert page.retained_from_ms == NOW_MS + 9_000
        assert migrated.control_traces(0, NOW_MS + DAY_MS)[0].tick_id == 5, "ts_ms 順は変えない"
        record(migrated, NOW_MS, 99)
        latest = migrated.latest_control_trace()
        assert latest is not None and (latest.seq, latest.tick_id) == (4, 99)


def test_migration_of_an_empty_table_uses_the_wall_clock(
    db: Path, rules: QualityRules, clock: SimulatedClock
) -> None:
    with SqliteStore(db, rules=rules, clock=clock) as opened:
        assert opened.control_trace_prune_state().legacy_until_ms == NOW_MS


# ---------------------------------------------------------------- rollup


def test_rollup_prunes_with_the_boundary_and_counts_future_rows(store: SqliteStore) -> None:
    rules = RetentionRules(raw_days=30, control_trace_days=30, csv_dir="unused")
    now_ms = 40 * DAY_MS
    record(store, 1 * DAY_MS, 1)
    record(store, 39 * DAY_MS, 2)
    record(store, 41 * DAY_MS, 3)  # 時計が未来へ跳んで書かれた行

    result = run(store, rules, now_ms=now_ms)

    assert result.deleted_control_traces == 1
    assert result.future_control_traces == 1
    state = store.control_trace_prune_state()
    assert state.pruned_through_seq == 1
    assert state.pruned_before_ms == 10 * DAY_MS


# ---------------------------------------------------------------- /control/latest


def test_latest_is_null_without_any_trace(client: TestClient) -> None:
    """記録が無いのは 404 ではなく `trace: null`（ルートが無いのと取り違えない）。"""
    response = client.get("/api/v1/control/latest")
    assert response.status_code == 200
    assert response.json() == {"trace": None}


def test_latest_is_the_last_recorded_row_even_if_the_clock_went_back(
    store: SqliteStore, client: TestClient
) -> None:
    record(store, NOW_MS - 1_000, 1)
    record(store, NOW_MS - 61_000, 2)  # 時計が1分戻った後の行
    trace = client.get("/api/v1/control/latest").json()["trace"]
    assert (trace["seq"], trace["tick_id"]) == (2, 2)
    assert trace["age_ms"] == 61_000


def test_latest_keeps_a_negative_age(store: SqliteStore, client: TestClient) -> None:
    """壁時計が戻った直後は負になる。API は丸めない（0071 §2.2）。"""
    record(store, NOW_MS + 500, 1)
    assert client.get("/api/v1/control/latest").json()["trace"]["age_ms"] == -500


@pytest.mark.parametrize("version", VERSIONS)
def test_latest_returns_the_stored_json_as_is(
    store: SqliteStore, client: TestClient, version: int
) -> None:
    stored = fixture_text(version)
    tick = json.loads(stored)
    store.record_control_trace(
        ts_ms=tick["ts_ms"], tick_id=tick["tick_id"], schema_version=version, trace_json=stored
    )
    trace = client.get("/api/v1/control/latest").json()["trace"]
    assert trace["schema_version"] == version
    assert trace["body"] == tick, "項目を足さない・消さない・直さない（0071 §2.3）"
    assert set(trace) == {"seq", "ts_ms", "ts", "tick_id", "schema_version", "age_ms", "body"}


# ---------------------------------------------------------------- /control/traces


def test_traces_return_every_version_unchanged_in_recorded_order(
    store: SqliteStore, client: TestClient
) -> None:
    """v1〜v9 が混ざっていても、そのまま返す。検証し直さない・投影しない（0071 §2.4）。"""
    for version in reversed(VERSIONS):  # 時刻の並びと記録の順を逆にする
        stored = fixture_text(version)
        tick = json.loads(stored)
        store.record_control_trace(
            ts_ms=tick["ts_ms"], tick_id=tick["tick_id"], schema_version=version, trace_json=stored
        )
    # 画面より新しい版の記録も、そのまま返す
    future = {"schema_version": SCHEMA_VERSION + 1, "unknown_block": {"x": 1}}
    store.record_control_trace(
        ts_ms=NOW_MS + 99_000,
        tick_id=999,
        schema_version=SCHEMA_VERSION + 1,
        trace_json=json.dumps(future),
    )

    body = client.get(
        "/api/v1/control/traces", params={"from": NOW_MS, "to": NOW_MS + 100_000}
    ).json()

    assert [trace["schema_version"] for trace in body["traces"]] == [
        *reversed(VERSIONS),
        SCHEMA_VERSION + 1,
    ]
    for trace in body["traces"][: len(VERSIONS)]:
        assert trace["body"] == json.loads(fixture_text(trace["schema_version"]))
        assert set(trace) == {"seq", "ts_ms", "ts", "tick_id", "schema_version", "body"}
    assert body["traces"][-1]["body"] == future
    assert body["has_more"] is False


def test_paging_walks_the_whole_period_without_gaps(store: SqliteStore, client: TestClient) -> None:
    for tick in range(7):
        record(store, NOW_MS + tick * 1_000, tick)
    params: dict[str, Any] = {"from": NOW_MS, "to": NOW_MS + 60_000, "limit": 3}

    first = client.get("/api/v1/control/traces", params=params).json()
    assert seqs(first) == [1, 2, 3] and first["has_more"] is True
    assert first["next_after"] == "3"

    second = client.get(
        "/api/v1/control/traces", params=params | {"after": first["next_after"]}
    ).json()
    third = client.get(
        "/api/v1/control/traces", params=params | {"after": second["next_after"]}
    ).json()
    assert seqs(second) == [4, 5, 6] and second["has_more"] is True
    assert seqs(third) == [7] and third["has_more"] is False
    assert third["next_after"] == "7"


def test_rows_written_after_the_clock_went_back_are_not_skipped(
    store: SqliteStore, client: TestClient
) -> None:
    """時計が戻って期間内の古い時刻で書かれた行も、同じ `after` で読み直せば取れる（0071 §2.2）。"""
    record(store, NOW_MS + 5_000, 1)
    params: dict[str, Any] = {"from": NOW_MS, "to": NOW_MS + 60_000}
    first = client.get("/api/v1/control/traces", params=params).json()
    assert seqs(first) == [1] and first["has_more"] is False

    record(store, NOW_MS + 1_000, 2)  # 時計が戻った後、cursor より前の時刻で書かれた
    again = client.get(
        "/api/v1/control/traces", params=params | {"after": first["next_after"]}
    ).json()
    assert seqs(again) == [2]


def test_the_period_filters_by_ts_ms_as_a_half_open_range(
    store: SqliteStore, client: TestClient
) -> None:
    record(store, NOW_MS - 1, 1)
    record(store, NOW_MS, 2)
    record(store, NOW_MS + 999, 3)
    record(store, NOW_MS + 1_000, 4)
    body = client.get(
        "/api/v1/control/traces", params={"from": NOW_MS, "to": NOW_MS + 1_000}
    ).json()
    assert [trace["tick_id"] for trace in body["traces"]] == [2, 3]
    assert (body["from_ms"], body["to_ms"]) == (NOW_MS, NOW_MS + 1_000)


def test_window_is_resolved_from_now(store: SqliteStore, client: TestClient) -> None:
    record(store, NOW_MS - 10 * 60_000, 1)
    record(store, NOW_MS - 60_000, 2)
    body = client.get("/api/v1/control/traces", params={"window": "5m"}).json()
    assert [trace["tick_id"] for trace in body["traces"]] == [2]
    assert (body["from_ms"], body["to_ms"]) == (NOW_MS - 5 * 60_000, NOW_MS)


@pytest.mark.parametrize(
    "params",
    [
        {"after": "0", "window": "5m"},
        {"after": "0", "from": NOW_MS},
        {"after": "0", "to": NOW_MS},
        {"after": "0", "from": NOW_MS, "to": NOW_MS + 1, "window": "5m"},
        {"after": "-1", "from": NOW_MS, "to": NOW_MS + 1},
        {"after": "01", "from": NOW_MS, "to": NOW_MS + 1},
        {"after": "abc", "from": NOW_MS, "to": NOW_MS + 1},
        {"after": "9" * 19, "from": NOW_MS, "to": NOW_MS + 1},
        {"from": NOW_MS},
        {"from": NOW_MS + 1, "to": NOW_MS},
        {"window": "5m", "limit": 0},
        {"window": "5m", "limit": 501},
    ],
)
def test_invalid_requests_are_rejected(client: TestClient, params: dict[str, Any]) -> None:
    """2ページ目以降は期間を固定する。`window` と `after` の併用は 422（0071 §2.2）。"""
    assert client.get("/api/v1/control/traces", params=params).status_code == 422


def test_limit_default_and_maximum_come_from_the_environment(
    store: SqliteStore, db: Path, clock: SimulatedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    for tick in range(5):
        record(store, NOW_MS - 1_000 + tick, tick)
    monkeypatch.setenv("COLDAISLE_CONTROL_TRACE_LIMIT", "2")
    monkeypatch.setenv("COLDAISLE_CONTROL_TRACE_MAX_LIMIT", "3")
    config = Config.from_env()
    assert (config.control_trace_limit, config.control_trace_max_limit) == (2, 3)
    with make_client(db, clock, control_trace_limit=2, control_trace_max_limit=3) as client:
        body = client.get("/api/v1/control/traces", params={"window": "1m"}).json()
        assert seqs(body) == [1, 2] and body["has_more"] is True
        assert (
            client.get("/api/v1/control/traces", params={"window": "1m", "limit": 4}).status_code
            == 422
        )


def test_the_default_limit_cannot_exceed_the_maximum() -> None:
    with pytest.raises(ValueError, match="limit"):
        Config(control_trace_limit=10, control_trace_max_limit=5)


# ---------------------------------------------------------------- 保持期間の境界と 409


def test_a_cursor_behind_the_pruned_rows_gets_409(store: SqliteStore, client: TestClient) -> None:
    """未読の行が消えたかもしれないなら、黙って続けず 409 で止める（0071 §2.2）。"""
    for tick in range(4):
        record(store, NOW_MS + tick * 1_000, tick)
    params: dict[str, Any] = {"from": NOW_MS, "to": NOW_MS + 60_000, "limit": 1}
    first = client.get("/api/v1/control/traces", params=params).json()
    assert first["next_after"] == "1"

    store.delete_control_traces_before(NOW_MS + 2_000)  # seq 1, 2 を消す

    response = client.get("/api/v1/control/traces", params=params | {"after": "1"})
    assert response.status_code == 409
    assert response.json()["retained_from_ms"] == NOW_MS + 2_000
    # 境界ちょうどの cursor には未読の行が消えていない
    resumed = client.get("/api/v1/control/traces", params=params | {"after": "2"})
    assert resumed.status_code == 200 and seqs(resumed.json()) == [3]
    # 409 の案内どおりに読み直せる
    restart = client.get(
        "/api/v1/control/traces",
        params={"from": response.json()["retained_from_ms"], "to": NOW_MS + 60_000},
    ).json()
    assert seqs(restart) == [3, 4] and restart["before_retained"] is False


def test_the_first_page_before_the_boundary_is_flagged_not_refused(
    store: SqliteStore, client: TestClient
) -> None:
    """1ページ目でも境界を返し、境界より前から読むなら `before_retained`（0071 §2.2 / §4 P）。"""
    record(store, NOW_MS + 1_000, 1)
    record(store, NOW_MS + 3_000, 2)
    store.delete_control_traces_before(NOW_MS + 2_000)

    flagged = client.get("/api/v1/control/traces", params={"from": NOW_MS, "to": NOW_MS + 10_000})
    assert flagged.status_code == 200
    body = flagged.json()
    assert body["retained_from_ms"] == NOW_MS + 2_000
    assert body["before_retained"] is True
    assert seqs(body) == [2]

    inside = client.get(
        "/api/v1/control/traces", params={"from": NOW_MS + 2_000, "to": NOW_MS + 10_000}
    ).json()
    assert inside["before_retained"] is False


def test_the_legacy_period_counts_as_possibly_missing(
    store: SqliteStore, client: TestClient
) -> None:
    """移行前の期間は、`rollup` の cutoff が越えるまで `before_retained`（0071 §2.2a）。"""
    body = client.get(
        "/api/v1/control/traces", params={"from": NOW_MS - 1, "to": NOW_MS + 1}
    ).json()
    assert body["retained_from_ms"] == NOW_MS
    assert body["before_retained"] is True


def test_the_store_checks_the_cursor_inside_one_snapshot(store: SqliteStore) -> None:
    record(store, NOW_MS, 1)
    store.delete_control_traces_before(NOW_MS + 1)
    with pytest.raises(ControlTraceCursorPrunedError) as raised:
        store.control_trace_page(0, NOW_MS + 10, after_seq=0, limit=10)
    assert raised.value.retained_from_ms == NOW_MS + 1
    assert not store.connection.in_transaction, "読み取りトランザクションを閉じる"


# ---------------------------------------------------------------- /events/{id}


def test_an_event_can_be_looked_up_by_id_without_the_peer_uid(
    store: SqliteStore, client: TestClient
) -> None:
    saved = store.record_event(
        EventRecord(
            ts_ms=NOW_MS - DAY_MS,
            kind="workload_hint",
            payload_json=json.dumps({"v": 1, "type": "workload_hint", "note": "学習"}),
            peer_uid=1000,
        )
    )
    response = client.get(f"/api/v1/events/{saved.id}")
    assert response.status_code == 200
    assert response.json() == {
        "id": saved.id,
        "ts_ms": NOW_MS - DAY_MS,
        "ts": response.json()["ts"],
        "kind": "workload_hint",
        "payload": {"v": 1, "type": "workload_hint", "note": "学習"},
    }


def test_a_missing_event_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/events/12345").status_code == 404


# ---------------------------------------------------------------- 境界（読み取り専用・層・LLM）


@pytest.mark.parametrize(
    "path", ["/api/v1/control/latest", "/api/v1/control/traces", "/api/v1/events/1"]
)
@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_the_new_routes_are_read_only(client: TestClient, path: str, method: str) -> None:
    assert getattr(client, method)(path).status_code == 405


def test_the_api_does_not_import_the_control_package() -> None:
    """版の解釈を1か所（読む側）に置く。API は制御の内部型に依存しない（0071 §2.4）。"""
    offenders = []
    for path in sorted((ROOT / "src" / "coldaisle" / "api").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [
                f"{path.name}: {name}"
                for name in names
                if name == "coldaisle.control" or name.startswith("coldaisle.control.")
            ]
    assert offenders == []


def test_the_traces_are_not_exposed_as_ai_tools() -> None:
    """trace は生の時系列。AI ツールに足さない（FR-504 / 0071 §2.8）。"""
    names = {definition["function"]["name"] for definition in DEFINITIONS}
    assert not any("control" in name or "trace" in name for name in names)
