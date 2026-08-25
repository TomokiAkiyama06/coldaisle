"""ツールの窓口（#23）。

#23 は「coldaisle 専用のチャットUIは作らない」に縮小された（決定 D-4）。
残る仕事は、**Workspace のチャットから呼べる形でツールを公開すること**と、
**何を呼んだかが追えること**、そして**制御を行わない位置づけを明記すること**。

守る線は3つ。

1. **窓口が増えても書き込み系は生えない**（GET だけ）
2. **モデルが何を寄こしても 200 で返る**（会話が止まらない）
3. **`api/`（L2）が `ai/`（L3）を import しない**
"""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coldaisle.ai.tools import ADVISORY_SUFFIX, DEFINITIONS, GUIDANCE
from coldaisle.api.app import Config
from coldaisle.clock import SimulatedClock
from coldaisle.server import create_server
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
NOW_MS = 1_787_616_000_000
INTERVAL_MS = 2_500


@pytest.fixture
def clock() -> SimulatedClock:
    return SimulatedClock(NOW_MS)


@pytest.fixture
def db(tmp_path, rules) -> Path:
    path = tmp_path / "tools.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_samples(
            [
                Sample(
                    ts_ms=NOW_MS - step * INTERVAL_MS,
                    readings=(
                        Reading(metric="air.room", value=26.0, quality=Quality.OK),
                        Reading(metric="air.front_intake", value=27.4, quality=Quality.OK),
                    ),
                )
                for step in range(120)
            ]
        )
    return path


@pytest.fixture
def client(db, clock):
    app = create_server(
        Config(
            db=db,
            quality_rules=QUALITY_RULES_PATH,
            metrics=CONFIG_DIR / "metrics.yaml",
            max_points=100,
        ),
        clock=clock,
    )
    with TestClient(app) as opened:
        yield opened


# ---------------------------------------------------------------- 読み取り専用（受入基準）


def test_the_tool_surface_adds_no_write_endpoints(client):
    """**窓口が増えても GET だけ。** ツールが読み取り専用であることが方式に出る。"""
    paths = client.get("/openapi.json").json()["paths"]
    methods = {method for operations in paths.values() for method in operations}
    assert methods == {"get"}, f"書き込み系が入り込んでいる: {sorted(methods - {'get'})}"


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_tools_reject_write_methods(method, client):
    assert getattr(client, method)("/api/v1/tools/get_latest").status_code == 405


def test_the_api_layer_does_not_import_the_ai_layer():
    """**L2 は L3 を import しない**（AGENTS.md「レイヤ間の依存は一方向」）。

    窓口を足すために `api/` から `ai/` を呼ぶと依存が逆流する。
    実体を渡すのは合成の起点（`server.py`）である。
    """
    offending = []
    for path in (SRC / "api").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            offending += [f"{path.name}: {n}" for n in names if n.startswith("coldaisle.ai")]
    assert offending == []


def test_the_envelopes_are_typed_in_openapi(client):
    """**Workspace は OpenAPI から型を生成する**（api-contract §4 / #23 のレビュー指摘）。

    `dict[str, Any]` のままだと、`meta` も `guidance` も自由形式の object になり、
    契約の変更を型で検出できない。**外側の封筒は固定する。**
    `result` と `tools` の中身はツールごと・モデルの作法ごとに変わるので型付けしない。
    """
    document = client.get("/openapi.json").json()
    schemas = document["components"]["schemas"]

    listing = document["paths"]["/api/v1/tools"]["get"]["responses"]["200"]["content"]
    assert listing["application/json"]["schema"]["$ref"].endswith("ToolListResponse")
    assert set(schemas["ToolListResponse"]["required"]) == {
        "read_only",
        "advisory",
        "guidance",
        "tools",
    }

    call = document["paths"]["/api/v1/tools/{name}"]["get"]["responses"]["200"]["content"]
    assert call["application/json"]["schema"]["$ref"].endswith("ToolCallResponse")
    assert set(schemas["ToolCallMeta"]["required"]) == {
        "tool",
        "arguments",
        "ok",
        "ts_ms",
        "ts",
        "elapsed_ms",
        "read_only",
        "advisory",
    }
    assert schemas["ToolCallMeta"]["properties"]["ok"]["type"] == "boolean"


def test_a_result_of_any_shape_still_passes_through(client):
    """封筒を型にしても、**ツールごとに違う `result` は通る。**"""
    body = client.get("/api/v1/tools/describe_system").json()
    assert body["meta"]["ok"] is True
    assert "metrics" in body["result"]


# ---------------------------------------------------------------- 一覧（受入基準）


def test_definitions_are_published(client):
    body = client.get("/api/v1/tools").json()
    assert [tool["function"]["name"] for tool in body["tools"]] == [
        definition["function"]["name"] for definition in DEFINITIONS
    ]


def test_the_listing_states_that_nothing_is_controlled(client):
    """受入基準: **「提案であり制御は行わない」位置づけが分かること。**"""
    body = client.get("/api/v1/tools").json()
    assert body["read_only"] is True
    assert body["advisory"] is True
    assert body["guidance"] == GUIDANCE
    assert "制御" in body["guidance"]


def test_every_description_says_it_controls_nothing():
    """**書き忘れを作らない。** system prompt が渡らない経路でも説明文だけで分かる。"""
    for definition in DEFINITIONS:
        assert definition["function"]["description"].endswith(ADVISORY_SUFFIX)


def test_the_guidance_forbids_claiming_an_action_was_taken():
    """この層には実行する手段が無い。**「実行しておきました」は嘘になる。**"""
    assert "提案" in GUIDANCE
    for forbidden in ("ファン制御", "電源操作", "シャットダウン"):
        assert forbidden in GUIDANCE


# ---------------------------------------------------------------- 実行と根拠（受入基準）


def test_a_call_returns_the_result_and_what_was_called(client):
    """受入基準: **どのツールを呼んだかが追える**（回答の根拠）。"""
    body = client.get(
        "/api/v1/tools/get_stats", params={"metric": "air.room", "window": "1h"}
    ).json()
    assert body["meta"]["tool"] == "get_stats"
    assert body["meta"]["arguments"] == {"metric": "air.room", "window": "1h"}
    assert body["meta"]["ok"] is True
    assert body["meta"]["read_only"] is True
    assert body["meta"]["advisory"] is True
    assert body["result"]["mean"] == pytest.approx(26.0)


def test_the_metadata_carries_a_timestamp(client):
    body = client.get("/api/v1/tools/get_latest").json()
    assert body["meta"]["ts_ms"] == NOW_MS
    assert body["meta"]["ts"].endswith("+00:00")
    assert body["meta"]["elapsed_ms"] >= 0


def test_query_arguments_are_converted_to_the_declared_types(client):
    """引数はクエリ文字列で届く。**型の変換は各ツールの引数モデルに任せる。**"""
    body = client.get("/api/v1/tools/list_alerts", params={"limit": "3"}).json()
    assert body["meta"]["ok"] is True
    assert body["result"]["alerts"] == []


# ---------------------------------------------------------------- 落ちない（受入基準）


def test_an_unknown_tool_answers_200_so_the_conversation_continues(client):
    """**モデルが寄こす名前は入力データであって、呼び出し側の誤りではない。**

    4xx にすると、Workspace 側が会話を続けるために例外を結果へ翻訳することになる。
    """
    response = client.get("/api/v1/tools/rm_rf")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["ok"] is False
    assert "そのツールは無い" in body["result"]["error"]
    assert "get_latest" in body["result"]["available"]


def test_broken_arguments_answer_200(client):
    body = client.get("/api/v1/tools/get_stats", params={"metric": "../etc/passwd"}).json()
    assert body["meta"]["ok"] is False
    assert "error" in body["result"]


def test_unknown_arguments_are_rejected_not_ignored(client):
    """余計な引数を黙って捨てない（`extra="forbid"`）。"""
    body = client.get("/api/v1/tools/get_latest", params={"drop_table": "readings"}).json()
    assert body["meta"]["ok"] is False


# ---------------------------------------------------------------- 既定の入口


def test_the_plain_api_app_has_no_tool_surface(db, clock):
    """`coldaisle.api:app` はそのまま使える（**ツールの窓口が無いだけ**）。"""
    from coldaisle.api.app import create_app

    app = create_app(
        Config(db=db, quality_rules=QUALITY_RULES_PATH, metrics=CONFIG_DIR / "metrics.yaml"),
        clock=clock,
    )
    with TestClient(app) as opened:
        assert "/api/v1/tools" not in opened.get("/openapi.json").json()["paths"]
        assert opened.get("/api/v1/latest").status_code == 200
