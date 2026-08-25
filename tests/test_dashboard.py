"""開発用ダッシュボード（#17）。

本番UIは Workspace 側なので**作り込まない**（Issue の但し書き）。
ここで守るのは3つだけ。

1. **オフラインで見える**（外部への参照を持たない）
2. **異常が異常に見える**（品質4値それぞれに見た目が定義されている）
3. **API から来た文字列を HTML として解釈しない**（要件 §7.4）

見た目そのものの確認は人間が行う。ここは「壊れていたら気づける」線を引く。
"""

import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from coldaisle.api.app import WEB_ROOT, Config, create_app
from coldaisle.clock import SimulatedClock
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH

METRICS_PATH = CONFIG_DIR / "metrics.yaml"
NOW_MS = 1_787_616_000_000

INDEX = WEB_ROOT / "index.html"
SCRIPT = WEB_ROOT / "app.js"
STYLES = WEB_ROOT / "styles.css"

# SVG の名前空間は取得先ではなく識別子。ネットワークへは出ない
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
URL_PATTERN = re.compile(r"https?://[^\s\"'()]+")


@pytest.fixture
def client(tmp_path, rules):
    path = tmp_path / "ui.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS,
                readings=(Reading(metric="air.room", value=26.0, quality=Quality.OK),),
            )
        )
    app = create_app(
        Config(db=path, quality_rules=QUALITY_RULES_PATH, metrics=METRICS_PATH),
        clock=SimulatedClock(NOW_MS),
    )
    with TestClient(app) as opened:
        yield opened


def test_dashboard_is_served_at_the_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "coldaisle" in response.text
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("asset", ["app.js", "styles.css"])
def test_assets_are_served(asset, client):
    assert client.get(f"/{asset}").status_code == 200


def test_api_routes_win_over_the_static_mount(client):
    """静的配信を先に置くと `/api/v1/...` まで飲まれる。順序を固定する。"""
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_still_no_write_endpoints(client):
    """ダッシュボードを足しても読み取り専用のまま（FR-307）。"""
    paths = client.get("/openapi.json").json()["paths"]
    assert {method for operations in paths.values() for method in operations} == {"get"}


@pytest.mark.parametrize("asset", [INDEX, SCRIPT, STYLES])
def test_no_external_references(asset):
    """**オフラインで見える。** CDN を読むと、回線が無い場所でグラフが消える。

    監視の道具が「見えないことに気づけない」壊れ方をしてはいけない。
    """
    found = set(URL_PATTERN.findall(asset.read_text(encoding="utf-8"))) - ALLOWED_URLS
    assert not found, f"{asset.name} が外部を参照している: {sorted(found)}"


def test_every_quality_value_has_a_style():
    """品質4値それぞれに見た目がある（要件 §5.3）。

    1つでも欠けると、**異常なカードが正常なカードと同じに見える。**
    """
    css = STYLES.read_text(encoding="utf-8")
    for quality in Quality:
        assert f".q-{quality.value}" in css, f"{quality.value} の見た目が無い"


def test_script_does_not_use_inner_html():
    """API から来た文字列を HTML として解釈しない（要件 §7.4）。

    `err` やアラートの `detail` はデバイス・ルール由来の文字列である。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert dangerous not in script, f"{dangerous} を使っている"
    assert "textContent" in script


def test_script_reads_only_documented_endpoints():
    """叩く先が API 契約の範囲に収まっていること。"""
    script = SCRIPT.read_text(encoding="utf-8")
    used = set(re.findall(r'"(/api/v1/[a-z]+)"', script))
    assert used == {"/api/v1/latest", "/api/v1/series", "/api/v1/health", "/api/v1/alerts"}
    assert "/api/v1/stream" in script, "WebSocket を使う（FR-306）"


def test_ranges_cover_the_required_periods():
    """期間切替 1h / 6h / 24h / 7d と、24h 以上は 1m 以上の粒度（Issue の指定）。"""
    script = SCRIPT.read_text(encoding="utf-8")
    ranges = re.findall(r'\{ label: "(\w+)", window: "(\w+)", agg: "(\w+)" \}', script)
    assert [label for label, _, _ in ranges] == ["1h", "6h", "24h", "7d"]
    assert dict((label, agg) for label, _, agg in ranges)["24h"] != "raw"
    assert dict((label, agg) for label, _, agg in ranges)["7d"] != "raw"


def test_future_timestamps_are_explained_not_shown_as_negative():
    """受信時刻が未来のとき、負の秒数をそのまま出さない。

    時計のずれ（#42）か、時間圧縮で再生中の DB を見ている
    （決定記録 0007 §2.11）。**「-491秒前」は読み手に何も伝えない。**
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert "data_age_seconds < 0" in script
    assert "未来" in script


@pytest.mark.parametrize("asset", [SCRIPT])
def test_script_parses(asset):
    """構文エラーで真っ白な画面にならないこと。

    `node` が無い環境では飛ばす。CI に Node を足すほどの依存ではない
    （Issue の「作り込まない」に対して釣り合わない）。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node が無い")
    assert subprocess.run([node, "--check", str(asset)], capture_output=True).returncode == 0
