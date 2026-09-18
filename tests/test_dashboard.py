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
from coldaisle.metrics import MetricCatalog
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
    assert used == {
        "/api/v1/metrics",
        "/api/v1/latest",
        "/api/v1/series",
        "/api/v1/health",
        "/api/v1/alerts",
        "/api/v1/devices",
    }
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


def test_periodic_refresh_also_reloads_current_values():
    """**赤帯とカードが食い違わないこと。**

    取り込みが止まると新しいサンプルは来ないが、品質は `stale` へ変わる。
    定期更新で最新値を取り直さないと、「データが古い」と言いながら
    カードは「正常」のままになる。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    refresh = script[script.index("async function refresh()") : script.index("function connect()")]
    assert "/api/v1/latest" in refresh, "定期更新で最新値を取り直していない"
    assert "applyLatest" in refresh


def test_chart_breaks_lines_across_gaps():
    """点が無い区間で線をつながない。

    取り込みが止まった区間には**行そのものが無い**（`value: null` の点すら来ない）。
    時刻の飛びで切らないと、測れていない時間帯に線が引かれる。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert "expectedStep" in script
    assert "GAP_FACTOR" in script


def test_startup_installs_recovery_before_fetching():
    """最初の取得に失敗しても「接続中…」で固まらない。

    再接続とタイマーを先に立ててから読みに行く。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    body = script[script.index("function start()") :]
    assert body.index("connect()") < body.index("refresh()")
    assert body.index("setInterval(refresh") < body.index("  refresh();")


# ---------------------------------------------------------------- センサー構成（#14）


def test_the_dashboard_shows_the_sensor_layout():
    """**どの物理プローブがどのメトリクスか**を出す（#14 / spec-review W-03）。"""
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="devices"' in html
    assert "センサー構成" in html


def test_the_dashboard_asks_for_the_devices():
    script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert '"/api/v1/devices"' in script
    assert "renderDevices(" in script


def test_a_changed_probe_is_marked():
    """`PROBE_CHANGED` の対象になっている行に印を付ける。

    **アラート欄に出るだけでは、どのプローブか分からない。**
    """
    script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert "if (sensor.changed) row.className" in script
    assert "tr.changed" in (WEB_ROOT / "styles.css").read_text(encoding="utf-8")


def test_the_marker_does_not_read_the_alert_text():
    """**アラートの文面から読み取らない**（#14 のレビュー指摘）。

    文面は最初の不一致のまま更新されないことがあり（`Engine.on_hello` は
    発火中なら何も返さない）、あとから別のチャネルがずれても印が動かない。
    一覧の上限で古いアラートが落ちる問題も避けられる。
    """
    script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert "probeChangeDetails" not in script
    assert "alert.detail" not in script.split("function renderDevices")[1]


def test_the_current_rom_is_shown():
    """**差し替えたあとに「何に変わったのか」が分かること。**"""
    assert "sensor.observed_rom" in (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert "いまの ROM" in (WEB_ROOT / "app.js").read_text(encoding="utf-8")


def test_the_rom_is_monospaced():
    """ROM は目で突き合わせるもの。**等幅で並べる。**"""
    assert "td.rom" in (WEB_ROOT / "styles.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 表示名と式（#48 / 決定記録 0039）


def test_the_catalog_endpoint_returns_labels_and_formulas(client):
    """表示名と派生値の式は `config/metrics.yaml` そのまま（決定記録 0039 §2.1）。"""
    body = client.get("/api/v1/metrics").json()
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    assert body["metrics"]["air.room"] == {"unit": "C", "label": catalog.metrics["air.room"].label}
    assert set(body["metrics"]) == set(catalog.metrics)
    rise = body["derived"]["d.intake_rise"]
    assert rise["minuend"] == "air.front_intake"
    assert rise["subtrahend"] == "air.room"
    assert rise["label"] == catalog.derived["d.intake_rise"].label


def test_the_catalog_does_not_need_data(tmp_path):
    """**取り込みが止まっていても表示名は引ける。** DB が空でも 200。"""
    app = create_app(
        Config(db=tmp_path / "empty.db", quality_rules=QUALITY_RULES_PATH, metrics=METRICS_PATH),
        clock=SimulatedClock(NOW_MS),
    )
    with TestClient(app) as opened:
        response = opened.get("/api/v1/metrics")
    assert response.status_code == 200
    assert "air.room" in response.json()["metrics"]


def test_every_card_metric_has_a_label():
    """カードに出す7つには表示名がある。**無いと内部名が画面に出る。**"""
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    air = [name for name in catalog.metrics if name.startswith("air.")]
    assert len(air) == 7
    assert all(catalog.metrics[name].label for name in air)


def test_internal_metric_names_are_not_rendered_as_labels():
    """`air.room` のような内部名を見出しにしない（決定記録 0039 §2.2）。"""
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'el("div", "label", metric)' not in script
    assert 'el("div", "label", name)' not in script
    assert 'el("div", "label", labelOf(metric))' in script
    assert 'el("div", "label", labelOf(name))' in script
    assert "labelOf(entry.metric)" in script, "凡例も表示名"
    assert "labelOf(sensor.metric)" in script, "センサー構成も表示名"
    assert "labelOf(alert.metric)" in script, "アラートの対象も表示名"


def test_derived_cards_show_the_formula():
    """派生値には「何から何を引いたか」を添える。"""
    script = SCRIPT.read_text(encoding="utf-8")
    assert "formulaOf(name)" in script
    assert "entry.minuend" in script and "entry.subtrahend" in script
    assert ".formula" in STYLES.read_text(encoding="utf-8")


def test_stale_cards_carry_no_badge_text():
    """カードに「古い」を出さない。古さは画面全体の赤帯が言う（決定記録 0039 §2.3）。

    **見た目は残す。** 赤い左辺が無いと、古いカードが正常と同じに見える。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    labels = script[
        script.index("const QUALITY_LABEL") : script.index(
            "};", script.index("const QUALITY_LABEL")
        )
    ]
    assert "stale" not in labels
    assert "古い" not in labels
    for quality in (Quality.OK, Quality.MISSING, Quality.SUSPECT):
        assert f"{quality.value}:" in labels, f"{quality.value} の文言が無い"
    assert ".q-stale" in STYLES.read_text(encoding="utf-8")


def test_a_stale_metric_always_raises_the_banner(tmp_path, rules):
    """カードから文言を外してよい根拠。**stale のカードがあれば赤帯も必ず出る。**

    どれか1つのメトリクスが `stale` なら health も `stale`（赤帯の条件）。
    """
    path = tmp_path / "stale.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS,
                readings=(Reading(metric="air.room", value=26.0, quality=Quality.OK),),
            )
        )
    app = create_app(
        Config(db=path, quality_rules=QUALITY_RULES_PATH, metrics=METRICS_PATH),
        clock=SimulatedClock(NOW_MS + 600_000),
    )
    with TestClient(app) as opened:
        latest = opened.get("/api/v1/latest").json()
        health = opened.get("/api/v1/health").json()
    assert latest["metrics"]["air.room"]["quality"] == "stale"
    assert health["stale"] is True


def test_the_catalog_is_retried_until_it_loads():
    """起動直後に API が落ちていても、回復後に内部名のまま残らない。"""
    script = SCRIPT.read_text(encoding="utf-8")
    refresh = script[script.index("async function refresh()") : script.index("function connect()")]
    assert "if (catalog === null) loadCatalog();" in refresh
    assert '"/api/v1/metrics"' in script[script.index("async function loadCatalog()") :]


def test_a_catalog_failure_does_not_stop_the_refresh():
    """**表の取得に失敗しても、最新値・health の更新を止めない。**

    `await` で待つと、失敗した時点で赤帯もカードも更新されなくなる。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    refresh = script[script.index("async function refresh()") : script.index("function connect()")]
    assert "await loadCatalog" not in refresh
    assert 'await fetchJson("/api/v1/metrics")' not in refresh
    assert refresh.index("loadCatalog()") < refresh.index("try {")


def test_every_derived_operand_has_a_label():
    """派生値の式に内部名が出ないこと（`gpu.0.hotspot − gpu.0.core` にならない）。"""
    catalog = MetricCatalog.from_yaml(METRICS_PATH)
    for name, meta in catalog.derived.items():
        for operand in (meta.minuend, meta.subtrahend):
            assert operand in catalog.metrics, f"{name} の {operand} に表示名が無い"


def test_alerts_are_a_table_with_state_and_severity_in_words():
    """アラート一覧は表。状態と重大度は色だけでなく文言でも出す（0011 §2.4 と同じ理由）。"""
    script = SCRIPT.read_text(encoding="utf-8")
    body = script[script.index("function renderAlerts") :]
    assert '"alert-table"' in body
    assert "ALERT_STATE_LABEL" in body and "SEVERITY_LABEL" in body
    assert ".alert-table" in STYLES.read_text(encoding="utf-8")


def test_the_banner_follows_stale_cards_from_the_stream():
    """**WebSocket でカードが stale になったら、同じ応答で赤帯も出す。**

    health の問い合わせは5秒ごと。health だけで赤帯を決めると、
    赤いカードがあるのに文言が無い時間ができる（決定記録 0039 §2.3 は文言を赤帯に任せる）。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    apply = script[script.index("function applyLatest(") :]
    apply = apply[: apply.index("\n}\n")]
    assert "renderBanner()" in apply
    banner = script[script.index("function renderBanner(") :]
    banner = banner[: banner.index("\n}\n")]
    assert "staleFromLatest(lastLatest)" in banner
    helper = script[script.index("function staleFromLatest(") :]
    assert 'item.quality === "stale"' in helper[: helper.index("\n}\n")]


def test_a_late_catalog_rerenders_every_labelled_part():
    """表示名の表が後から届いたら、**内部名を出していた箇所をすべて**描き直す。

    カードと派生値だけだと、アラート・センサー構成・凡例に内部名が残る。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    load = script[script.index("async function loadCatalog()") :]
    assert "rerenderAll()" in load[: load.index("\n}\n")]
    rerender = script[script.index("function rerenderAll()") :]
    rerender = rerender[: rerender.index("\n}\n")]
    for call in (
        "applyLatest(lastLatest)",
        "renderAlerts(lastAlerts)",
        "renderDevices(lastDevices)",
    ):
        assert call in rerender, f"{call} を描き直していない"
    assert "drawCharts()" in rerender, "凡例（グラフ）を描き直していない"


def test_a_stream_update_does_not_hide_an_api_failure():
    """WebSocket の更新で、**API の失敗を知らせる赤帯を消さない。**

    定期更新（`/alerts` や `/devices`）が失敗していても、WebSocket は届き続けることがある。
    失敗を赤帯に直接書くと、次の `applyLatest → renderBanner` で上書きされて消える。
    失敗は `lastFetchError` に残し、定期更新が丸ごと成功したときだけ消す。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    banner = script[script.index("function renderBanner(") :]
    banner = banner[: banner.index("\n}\n")]
    assert "if (lastFetchError)" in banner
    assert "messages.join(" in banner, "成り立つ警告をすべて出す"
    refresh = script[script.index("async function refresh()") : script.index("function connect()")]
    failure = refresh[refresh.index("catch (error)") :]
    assert "lastFetchError = error.message" in failure
    assert "banner.textContent" not in failure, "赤帯に直接書くと WebSocket の更新で消える"
    success = refresh[: refresh.index("catch (error)")]
    assert "lastFetchError = null" in success
    assert success.index("Promise.all") < success.index("lastFetchError = null")


def test_the_catalog_note_clears_on_its_own():
    """表示名が取れたら「表示名を取得できません」をその場で消す。

    履歴と表示名が同じ注記欄を上書きし合うと、表示名が取れたあとも
    次の履歴更新（60秒）まで古い失敗が残る。**状態を別々に持つ。**
    """
    script = SCRIPT.read_text(encoding="utf-8")
    load = script[script.index("async function loadCatalog()") :]
    load = load[: load.index("\n}\n")]
    success = load[: load.index("catch (error)")]
    assert 'catalogNote = ""' in success
    assert "historyNote" not in load, "表示名の側から履歴の注記を消さない"
    assert "renderNote()" in load
    history = script[script.index("async function loadHistory()") :]
    history = history[: history.index("\n}\n")]
    assert "catalogNote" not in history, "履歴の側から表示名の注記を消さない"
    assert "historyNote = " in history and "renderNote()" in history
    assert script.count('getElementById("chart-note")') == 1, "注記欄に書くのは renderNote だけ"


# ---------------------------------------------------------------- 遅れて返る応答（#48 のレビュー）


def _body(script: str, signature: str) -> str:
    body = script[script.index(signature) :]
    return body[: body.index("\n}\n")]


def test_only_one_catalog_request_at_a_time():
    """表示名の表の取得は**同時に1件だけ**。古い失敗が新しい成功を上書きしない。"""
    script = SCRIPT.read_text(encoding="utf-8")
    load = _body(script, "async function loadCatalog()")
    assert "if (catalogInFlight || catalog !== null) return;" in load
    assert "catalogInFlight = false" in load[load.index("finally") :]
    assert load.count("if (seq !== catalogSeq) return;") == 2, "成功・失敗の両方で古い応答を捨てる"


def test_a_late_refresh_does_not_overwrite_a_newer_one():
    """遅れて返った定期更新で、新しい結果（成功・失敗とも）を上書きしない。

    **実行中なら飛ばす方式にはしない。** 応答が返らないまま固まった1件が、
    以後の更新を全部止めてしまう。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    refresh = _body(script, "async function refresh()")
    assert "const seq = ++refreshSeq;" in refresh
    assert refresh.count("if (seq !== refreshSeq) return;") == 2
    assert "InFlight" not in refresh, "定期更新は実行中でも次を出す"


def test_a_refresh_does_not_undo_a_newer_stream_update():
    """WebSocket がより新しい最新値を届けていたら、定期更新の古い最新値で戻さない。"""
    script = SCRIPT.read_text(encoding="utf-8")
    refresh = _body(script, "async function refresh()")
    assert "const streamAtStart = streamVersion;" in refresh
    assert "if (streamVersion === streamAtStart) applyLatest(latest);" in refresh
    assert "streamVersion += 1;" in script[script.index("socket.onmessage") :]


def test_a_late_history_response_does_not_overwrite_the_selected_range():
    """期間を切り替えたあとに、前の期間の応答でグラフを描き直さない。"""
    script = SCRIPT.read_text(encoding="utf-8")
    history = _body(script, "async function loadHistory()")
    assert "const seq = ++historySeq;" in history
    assert history.count("if (seq !== historySeq) return;") == 2
