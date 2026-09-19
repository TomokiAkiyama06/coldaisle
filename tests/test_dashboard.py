"""開発用ダッシュボード（#17）。

本番UIは Workspace 側なので**作り込まない**（Issue の但し書き）。
ここで守るのは3つだけ。

1. **オフラインで見える**（外部への参照を持たない）
2. **異常が異常に見える**（品質4値それぞれに見た目が定義されている）
3. **API から来た文字列を HTML として解釈しない**（要件 §7.4）

見た目そのものの確認は人間が行う。ここは「壊れていたら気づける」線を引く。
"""

import itertools
import json
import os
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


def _require_node() -> str:
    """`node` の場所。**CI では無ければ失敗、手元では飛ばす**（決定記録 0044）。

    CI で黙って飛ばすと、画面の振る舞いのテストが走っていないことに気づけない。
    GitHub Actions は `CI=true` を設定する。
    """
    node = shutil.which("node")
    if node is not None:
        return node
    if os.environ.get("CI"):
        pytest.fail("CI では node が必須（決定記録 0044。ci.yml の setup-node を確認）")
    pytest.skip("node が無い（手元では飛ばす。CI では必須）")


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
        # GPU Mode の切り替えの縦線（#67）。読み取り専用
        "/api/v1/events",
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

    `node` が無い手元では飛ばし、CI では必須（決定記録 0044）。
    """
    node = _require_node()
    assert subprocess.run([node, "--check", str(asset)], capture_output=True).returncode == 0


def test_periodic_refresh_also_reloads_current_values():
    """**赤帯とカードが食い違わないこと。**

    取り込みが止まると新しいサンプルは来ないが、品質は `stale` へ変わる。
    定期更新で最新値を取り直さないと、「データが古い」と言いながら
    カードは「正常」のままになる。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    endpoints = script[script.index("const REFRESH_ENDPOINTS") :]
    endpoints = endpoints[: endpoints.index("];")]
    assert '"/api/v1/latest"' in endpoints, "定期更新で最新値を取り直していない"
    appliers = script[script.index("const APPLY_ENDPOINT") :]
    assert "applyLatest(body)" in appliers[: appliers.index("\n};")]
    refresh = _body(script, "async function refresh()")
    assert "REFRESH_ENDPOINTS.map(" in refresh and "APPLY_ENDPOINT[key](" in refresh


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
    banner = script[script.index("function bannerMessages(") :]
    banner = banner[: banner.index("\n}\n")]
    assert "staleFromLatest(lastLatest)" in banner
    helper = script[script.index("function staleFromLatest(") :]
    assert "latest.stale" in helper[: helper.index("\n}\n")]


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
    失敗はエンドポイントごとに `fetchErrors` に残し、そのエンドポイントが次に成功したときだけ消す。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    banner = script[script.index("function bannerMessages(") :]
    banner = banner[: banner.index("\n}\n")]
    assert "const failed = Boolean(lastFetchError);" in banner
    assert "messages.join(" in _body(script, "function renderBanner("), "成り立つ警告をすべて出す"
    refresh = script[script.index("async function refresh()") : script.index("function connect()")]
    assert "banner.textContent" not in refresh, "赤帯に直接書くと WebSocket の更新で消える"
    assert "delete fetchErrors[key];" in refresh, "成功したエンドポイントの失敗だけを消す"
    assert "fetchErrors[key] = aborted" in refresh
    assert 'lastFetchError = failures.length > 0 ? failures.join(", ") : null;' in refresh, (
        "いま失敗しているエンドポイントをすべて出す"
    )


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


def test_an_events_failure_is_shown_apart_from_no_transitions():
    """注釈の取得に失敗しても履歴は描くが、失敗は注記に残す（#141 のレビュー）。

    空の一覧に読み替えるだけでは「GPU Mode の切り替えが無かった」と見分けがつかない。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    history = script[script.index("async function loadHistory()") :]
    history = history[: history.index("\n}\n")]
    events = history[history.index("const loadEvents") :]
    events = events[: events.index(";\n")]
    assert ".catch(() => [])" not in events, "失敗を黙って空の一覧にしない"
    assert "error" in events
    assert "GPU Mode の記録を取得できませんでした" in history
    assert "eventsNote = eventResult.error" in history
    note = script[script.index("function renderNote()") :]
    note = note[: note.index("\n}\n")]
    assert "eventsNote" in note, "注釈の失敗も注記欄に出す"
    load = script[script.index("async function loadCatalog()") :]
    load = load[: load.index("\n}\n")]
    assert "eventsNote" not in load, "表示名の側から注釈の注記を消さない"


# ---------------------------------------------------------------- 遅れて返る応答（#48 のレビュー）


def _body(script: str, signature: str) -> str:
    body = script[script.index(signature) :]
    return body[: body.index("\n}\n")]


def test_only_one_catalog_request_at_a_time():
    """表示名の表の取得は**同時に1件だけ**。古い失敗が新しい成功を上書きしない。

    固まったまま次を止めないよう、打ち切りの上限も持つ。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    load = _body(script, "async function loadCatalog()")
    assert "if (catalogInFlight || catalog !== null) return;" in load
    assert "catalogInFlight = false" in load[load.index("finally") :]
    assert "withTimeout(REFRESH_TIMEOUT_MS" in load


def test_a_slow_but_eventually_successful_refresh_is_applied():
    """**遅いだけの応答を捨て続けない。**

    「最後に開始した番号」と比べると、周期より遅いエンドポイントは毎回次の要求に
    追い越されて一度も表示されない。**最後に適用した番号**と比べ、
    より新しい応答が先に適用されていない限り使う。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    refresh = _body(script, "async function refresh()")
    assert refresh.count("if (seq <= refreshAppliedSeq[key]) return;") == 1, "成功・失敗とも"
    assert refresh.count("refreshAppliedSeq[key] = seq;") == 1
    assert refresh.index("refreshAppliedSeq[key] = seq;") < refresh.index(
        'result.status === "fulfilled"'
    )
    assert "seq !== refreshSeq" not in refresh, "開始した番号と比べると遅い応答が飢える"


def test_refresh_is_single_flight_with_a_timeout():
    """定期更新は同時に1件。**返らない要求は打ち切る**（次の更新を止め続けない）。"""
    script = SCRIPT.read_text(encoding="utf-8")
    refresh = _body(script, "async function refresh()")
    assert "if (refreshInFlight) return;" in refresh
    assert "refreshInFlight = false" in refresh[refresh.index("finally") :]
    assert "withTimeout(REFRESH_TIMEOUT_MS" in refresh
    assert (
        "REFRESH_ENDPOINTS.map((endpoint) => fetchJson(endpoint.path, endpoint.params, signal))"
        in refresh
    ), "4つの要求すべてを打ち切れること"
    helper = _body(script, "async function withTimeout(")
    assert "new AbortController()" in helper and "controller.abort()" in helper
    assert "clearTimeout(timer)" in helper
    # 上限は周期から決める。別の設定を増やさない
    assert "const REFRESH_TIMEOUT_MS = REFRESH_INTERVAL_MS" in script
    assert "setInterval(refresh, REFRESH_INTERVAL_MS)" in script
    assert "{ signal }" in _body(script, "async function fetchJson(")


def test_a_refresh_does_not_undo_a_newer_stream_update():
    """WebSocket がより新しい最新値を届けていたら、定期更新の古い最新値で戻さない。"""
    script = SCRIPT.read_text(encoding="utf-8")
    refresh = _body(script, "async function refresh()")
    assert "const streamAtStart = streamVersion;" in refresh
    assert "APPLY_ENDPOINT[key](result.value, streamAtStart)" in refresh
    appliers = script[script.index("const APPLY_ENDPOINT") :]
    assert "if (streamVersion === streamAtStart) applyLatest(body);" in appliers
    assert "streamVersion += 1;" in script[script.index("socket.onmessage") :]


def test_history_applies_the_newest_response_for_the_selected_range():
    """履歴も「適用済みより新しい応答だけ」。加えて**いま選ばれている期間**の応答に限る。

    期間を切り替えたあとに前の期間の応答で描き直さない。同じ期間の遅い応答は使う。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    history = _body(script, "async function loadHistory()")
    assert "const range = currentRange;" in history
    assert "seq > historyAppliedSeq && range === currentRange" in history
    assert history.count("if (!usable()) return;") == 2
    assert history.count("historyAppliedSeq = seq;") == 2
    assert "withTimeout(HISTORY_TIMEOUT_MS" in history
    assert "window: range.window" in history, "要求も開始時の期間で出す"


def test_sibling_requests_are_aborted_once_the_group_settles():
    """1件が先に失敗しても、**残りの要求を走らせ続けない。**

    `Promise.all` は最初の失敗で決着するが、ほかの fetch は止まらない。
    決着したら必ず abort する。ただし**元の失敗の文言は変えない**
    （「N秒以内に応答がありません」と言い換えるのは、上限に達したときだけ）。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    helper = _body(script, "async function withTimeout(")
    cleanup = helper[helper.index("finally") :]
    assert "controller.abort()" in cleanup, "決着後に中断していない"
    assert "clearTimeout(timer)" in cleanup
    failure = helper[helper.index("catch (error)") : helper.index("finally")]
    assert "if (timedOut)" in failure, "決着後の abort を打ち切りと取り違えない"
    assert "controller.signal.aborted" not in failure
    assert "throw error;" in failure, "元の失敗をそのまま投げ直す"


def test_event_metrics_do_not_keep_the_banner_up():
    """事象メトリクスの `stale` で赤帯を出さない（health と同じ規則。決定記録 0009 §2.12）。

    `sys.dropped_samples` などは起きたときにしか書かれない。各メトリクスの品質を
    画面側で見ると、**一度でも取りこぼしがあれば赤帯が消えなくなる。**
    判定はサーバの `latest.stale`（周期メトリクスだけを見る）に任せる。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    helper = _body(script, "function staleFromLatest(")
    assert "if (!latest.stale) return" in helper
    assert "latest.metrics).some(" not in helper, "各メトリクスの品質から古さを判定しない"
    assert "isCardMetric(metric)" in helper, "経過秒も周期メトリクス（カードの air.*）から"


def test_the_server_stale_flag_ignores_event_metrics(tmp_path, rules):
    """画面が頼る `latest.stale` は、事象メトリクスだけが古いときに立たない。"""
    path = tmp_path / "events.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS - 600_000,
                readings=(Reading(metric="sys.dropped_samples", value=1, quality=Quality.OK),),
            )
        )
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
        latest = opened.get("/api/v1/latest").json()
    assert latest["metrics"]["sys.dropped_samples"]["quality"] == "stale"
    assert latest["stale"] is False


def test_the_banner_age_comes_from_the_stale_cards_only():
    """赤帯の秒数は **stale のカードの中でいちばん古いもの**。

    全カードの最小を取ると、1枚だけ止まったときに正常なカードの「0 秒」が出る。
    stale のカードが無い（カード外の周期メトリクスが原因）ときは秒数を出さない。
    health の `data_age_seconds`（いちばん新しいサンプルの経過）にも戻らない。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    helper = _body(script, "function staleFromLatest(")
    assert 'isCardMetric(metric) && item.quality === "stale"' in helper
    assert "Math.max(...ages)" in helper
    assert "Math.min" not in helper
    assert "ages.length ? Math.max(...ages) : null" in helper, "stale のカードが無ければ秒数なし"
    banner = _body(script, "function bannerMessages(")
    assert "BANNER_TEXT.stale(fromLatest ? fromLatest.seconds : null)" in banner


def test_the_banner_is_stale_if_either_latest_or_health_says_so():
    """「古い」は `/latest` と health の**どちらか一方でも**古ければ出す（安全側）。

    片方だけを信じると、もう片方が古いと言っているのに赤帯が消える。
    WebSocket の新しい最新値が消せるのは最新値の側だけで、health の側は
    次の health の応答（`lastHealth` を書くのは適用された定期更新だけ）まで残る。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    banner = _body(script, "function bannerMessages(")
    assert (
        "const stale = Boolean(fromLatest && fromLatest.stale) || Boolean(health && health.stale);"
        in banner
    )
    assert "fromLatest ? fromLatest.stale :" not in banner, "片方だけを信じない"
    # health の側を書き換えるのは、適用された定期更新だけ（WebSocket では消えない）
    assert script.count("lastHealth = ") == 2, "宣言と定期更新の2か所だけ"
    assert "lastHealth = body;" in script[script.index("const APPLY_ENDPOINT") :]
    refresh = _body(script, "async function refresh()")
    assert refresh.index("refreshAppliedSeq[key] = seq;") < refresh.index("APPLY_ENDPOINT[key](")
    assert "lastHealth" not in script[script.index("socket.onmessage") :].split("};")[0]


# ---------------------------------------------------------------- 赤帯の組み合わせ（表駆動）

BANNER_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
class Node {
  constructor() {
    this.children = []; this.className = ""; this.textContent = ""; this.style = {};
    this.hidden = true;
    const self = this;
    this.classList = {
      add(c) { if (c === "hidden") self.hidden = true; },
      remove(c) { if (c === "hidden") self.hidden = false; },
    };
  }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  addEventListener() {}
}
const nodes = {};
const get = (id) => nodes[id] || (nodes[id] = new Node());
const context = {
  console, Math, Date, Number, Object, URL, JSON, Promise, Infinity, Boolean, Error,
  AbortController,
  document: {
    getElementById: get,
    createElement: () => new Node(),
    createElementNS: () => new Node(),
    createTextNode: (t) => { const n = new Node(); n.textContent = t; return n; },
  },
  window: { location: { origin: "http://127.0.0.1", protocol: "http:", host: "127.0.0.1" } },
  WebSocket: function () {},
  setInterval() {}, setTimeout() {}, clearTimeout() {},
  fetch: () => new Promise(() => {}), // 起動時の読み込みは返さない（状態は下で直接与える）
};
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(process.argv[2], "utf8") +
    "\n;this.__set = (s) => { lastFetchError = s.fetchError; lastHealth = s.health;" +
    " lastLatest = s.latest; renderBanner(); };",
  context,
);
const results = [];
for (const state of JSON.parse(fs.readFileSync(process.argv[3], "utf8"))) {
  context.__set(state);
  const banner = get("banner");
  results.push({ text: banner.textContent, hidden: banner.hidden });
}
process.stdout.write(JSON.stringify(results));
"""

BANNER_EXPECTED = {
    "fetch_error": "API に接続できません: /api/v1/alerts: 500",
    "no_data": "データが1件も届いていません。",
    "future": "受信時刻が未来です。",
    "stale": "データが古い",
}


def _banner_state(fetch_error: bool, no_data: bool, future: bool, stale: bool) -> dict:
    """4つの条件を**それぞれ独立に**立てた入力。

    「データなし」と「未来」は health だけでは同時に表せない（経過秒が null になる）
    ため、未来は最新値の側（カードの `age_seconds < 0`）で立てる。古さは health の側。
    """
    return {
        "fetchError": "/api/v1/alerts: 500" if fetch_error else None,
        "health": {
            "last_sample_ts_ms": None if no_data else NOW_MS,
            "data_age_seconds": None if no_data else 1.0,
            "stale": stale,
            "source": "mock",
        },
        "latest": {
            "stale": False,
            "metrics": {
                "air.room": {
                    "value": 26.0,
                    "unit": "C",
                    "quality": "ok",
                    "age_seconds": -30.0 if future else 1.0,
                }
            },
            "derived": {},
        },
    }


def test_the_banner_shows_exactly_the_applicable_messages(tmp_path):
    """赤帯の4条件（API の失敗・データなし・未来の時刻・古い）の**全16通り**。

    どれかが else-if で別の条件を隠していないこと、成り立つものだけが
    **重い順**に並ぶこと、何も無ければ帯が隠れることを確かめる。

    `node` が無い手元では飛ばし、CI では必須（決定記録 0044）。
    文字列の検査では、条件どうしの組み合わせは確かめられない。
    """
    node = _require_node()
    order = list(BANNER_EXPECTED)
    combos = list(itertools.product([False, True], repeat=4))
    states = tmp_path / "states.json"
    states.write_text(json.dumps([_banner_state(*combo) for combo in combos]), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(BANNER_HARNESS, encoding="utf-8")
    run = subprocess.run(
        [node, str(harness), str(SCRIPT), str(states)], capture_output=True, text=True, check=True
    )
    results = json.loads(run.stdout)
    assert len(results) == 16
    for combo, result in zip(combos, results, strict=True):
        wanted = [name for name, on in zip(order, combo, strict=True) if on]
        parts = result["text"].split(" / ") if result["text"] else []
        assert len(parts) == len(wanted), f"{combo}: {result['text']!r}"
        for part, name in zip(parts, wanted, strict=True):
            assert part.startswith(BANNER_EXPECTED[name]), f"{combo}: {name} の位置に {part!r}"
        assert result["hidden"] is (not wanted), f"{combo}: 帯の表示が食い違う"


# ---------------------------------------------------------------- 定期更新の部分的な失敗


REFRESH_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
class Node {
  constructor() {
    this.children = []; this.className = ""; this.textContent = ""; this.style = {};
    this.hidden = true;
    this.viewBox = { baseVal: { width: 800, height: 260 } };
    const self = this;
    this.classList = {
      add(c) { if (c === "hidden") self.hidden = true; },
      remove(c) { if (c === "hidden") self.hidden = false; },
    };
  }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  addEventListener() {}
}
const nodes = {};
const get = (id) => nodes[id] || (nodes[id] = new Node());
const phases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
let phase = 0;
const context = {
  console, Math, Date, Number, Object, URL, JSON, Promise, Infinity, Boolean, Error,
  AbortController,
  document: {
    getElementById: get,
    createElement: () => new Node(),
    createElementNS: () => new Node(),
    createTextNode: (t) => { const n = new Node(); n.textContent = t; return n; },
  },
  window: { location: { origin: "http://127.0.0.1", protocol: "http:", host: "127.0.0.1" } },
  WebSocket: function () {},
  setInterval() {}, setTimeout() {}, clearTimeout() {},
  fetch: async (url) => {
    const answer = phases[phase][url.pathname];
    if (answer === undefined || answer === null) return { ok: false, status: 500 };
    return { ok: true, json: async () => answer };
  },
};
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(process.argv[2], "utf8") +
    "\n;this.__refresh = refresh;" +
    " this.__state = () => ({ health: lastHealth, alerts: lastAlerts, error: lastFetchError," +
    " banner: document.getElementById('banner').textContent });",
  context,
);
const settle = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  const states = [];
  await settle(); await settle();
  states.push(context.__state()); // 起動時の定期更新（phase 0）
  for (phase = 1; phase < phases.length; phase += 1) {
    await context.__refresh();
    await settle();
    states.push(context.__state());
  }
  process.stdout.write(JSON.stringify(states));
})();
"""


def test_health_applies_while_alerts_fails(tmp_path):
    """**/alerts が失敗していても、取れた /health は使う。**

    `Promise.all` だと1つの失敗で残りを捨て、health（赤帯の入力）が失敗の続く間
    固まる。エンドポイントごとに適用し、失敗しているものだけを赤帯に出す。
    成功したら、そのエンドポイントの失敗だけが消える。
    """
    node = _require_node()
    latest = {
        "stale": False,
        "metrics": {"air.room": {"value": 26.0, "unit": "C", "quality": "ok", "age_seconds": 1.0}},
        "derived": {},
    }
    health = {
        "last_sample_ts_ms": NOW_MS,
        "data_age_seconds": 1.0,
        "stale": False,
        "source": "mock",
    }
    stale_health = dict(health, stale=True)
    alerts = {
        "alerts": [
            {
                "rule_id": "R",
                "severity": "warning",
                "state": "firing",
                "metric": "air.room",
                "started_ms": NOW_MS,
            }
        ]
    }
    devices = {"devices": []}
    catalog = {"metrics": {}, "derived": {}}

    def phase(health_body, alerts_body):
        return {
            "/api/v1/metrics": catalog,
            "/api/v1/latest": latest,
            "/api/v1/health": health_body,
            "/api/v1/alerts": alerts_body,
            "/api/v1/devices": devices,
            "/api/v1/series": {"points": [], "agg": "raw"},
        }

    phases = [
        phase(health, alerts),  # 0: すべて成功
        phase(stale_health, None),  # 1: /alerts だけ失敗、health は古いと言う
        phase(health, alerts),  # 2: 回復
    ]
    spec = tmp_path / "phases.json"
    spec.write_text(json.dumps(phases), encoding="utf-8")
    harness = tmp_path / "refresh.js"
    harness.write_text(REFRESH_HARNESS, encoding="utf-8")
    run = subprocess.run(
        [node, str(harness), str(SCRIPT), str(spec)], capture_output=True, text=True, check=True
    )
    first, failing, recovered = json.loads(run.stdout)

    assert first["error"] is None
    assert failing["health"]["stale"] is True, "/alerts の失敗で /health を捨てている"
    assert failing["alerts"] == alerts["alerts"], "失敗したエンドポイントは前の値を残す"
    assert failing["error"] == "/api/v1/alerts: 500", "失敗しているエンドポイントだけを出す"
    assert failing["banner"].startswith("API に接続できません: /api/v1/alerts: 500")
    assert "データが古い" in failing["banner"], "取れた health の stale も同時に出す"
    assert recovered["error"] is None, "成功したら、そのエンドポイントの失敗は消える"
    assert recovered["health"]["stale"] is False
    assert recovered["banner"] == ""
