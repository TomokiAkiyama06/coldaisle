"""エアフロー / ファン制御の画面（#106 / 決定記録 0046）。

見た目そのものの確認は人間が行う（決定記録 0011 と同じ）。ここは「壊れていたら気づける」線を引く。

1. **表示専用**（書き込みの経路が無い）
2. **実データと模擬データを混ぜない**
3. **色の区切りをコードに書かない**（config/airflow-ui.yaml → `GET /api/v1/airflow/config`）
4. **未接続・未取得を正常値のように見せない**
5. オフラインで見える・API の文字列を HTML として解釈しない
"""

import json
import os
import re
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coldaisle.api.airflow import AirflowUiSettings
from coldaisle.api.app import WEB_ROOT, Config, create_app
from coldaisle.channels import CHANNEL_TO_METRIC
from coldaisle.clock import SimulatedClock
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH

METRICS_PATH = CONFIG_DIR / "metrics.yaml"
AIRFLOW_UI_PATH = CONFIG_DIR / "airflow-ui.yaml"
NOW_MS = 1_787_616_000_000

PAGE = WEB_ROOT / "airflow.html"
SCRIPT = WEB_ROOT / "airflow.js"
MOCK = WEB_ROOT / "airflow-mock.js"
STYLES = WEB_ROOT / "airflow.css"
STATUS = WEB_ROOT / "airflow-status.js"
ASSETS = [PAGE, SCRIPT, MOCK, STATUS, STYLES]

ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
URL_PATTERN = re.compile(r"https?://[^\s\"'()]+")
METRIC_LITERAL = re.compile(r'"((?:air|gpu|cpu|fan|power|board|sys|d)\.[a-z0-9_.]+)"')


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path, rules):
    path = tmp_path / "ui.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS,
                readings=(Reading(metric="fan.front.rpm", value=1020.0, quality=Quality.OK),),
            )
        )
    app = create_app(
        Config(
            db=path,
            quality_rules=QUALITY_RULES_PATH,
            metrics=METRICS_PATH,
            airflow_ui=AIRFLOW_UI_PATH,
        ),
        clock=SimulatedClock(NOW_MS),
    )
    with TestClient(app) as opened:
        yield opened


# ---------------------------------------------------------------- 配信と API


@pytest.mark.parametrize("asset", [path.name for path in ASSETS])
def test_assets_are_served(asset, client):
    assert client.get(f"/{asset}").status_code == 200


def test_the_dashboard_links_to_the_page():
    assert 'href="airflow.html"' in _text(WEB_ROOT / "index.html")


def test_the_config_endpoint_returns_the_thresholds_from_yaml(client):
    body = client.get("/api/v1/airflow/config").json()
    expected = yaml.safe_load(_text(AIRFLOW_UI_PATH))["air_temperature"]
    assert body == {
        "schema_version": 1,
        "air_temperature": {
            "unit": "C",
            "thresholds_c": expected["thresholds_c"],
            "provisional": expected["provisional"],
        },
    }


def test_still_no_write_endpoints(client):
    """画面を足しても読み取り専用のまま（FR-307）。"""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/airflow/config" in paths
    assert {method for operations in paths.values() for method in operations} == {"get"}


def test_the_endpoint_is_documented():
    assert "/api/v1/airflow/config" in _text(
        Path(__file__).resolve().parents[1] / "docs" / "api-contract.md"
    )


@pytest.mark.parametrize(
    "thresholds",
    [
        [27.0, 28.0, 29.0],
        [27.0, 28.0, 29.0, 30.0, 31.0],
        [27.0, 29.0, 28.0, 30.0],
        [27.0, 27.0, 28.0, 29.0],
    ],
)
def test_bad_thresholds_are_rejected(thresholds):
    """段数違い・逆順・重複は起動時に落とす。**ある温度が何色になるか読めなくなる。**"""
    with pytest.raises(ValidationError):
        AirflowUiSettings.model_validate(
            {"version": 1, "air_temperature": {"thresholds_c": thresholds, "provisional": True}}
        )


@pytest.mark.parametrize("bad", [".nan", ".inf", "-.inf"])
@pytest.mark.parametrize("position", [0, 1, 3])
def test_non_finite_thresholds_are_rejected(tmp_path, bad, position):
    """YAML の `.nan` / `.inf` は起動時に落とす。**昇順の判定を素通りし、JSON 化で落ちるため。**"""
    thresholds = ["27.0", "28.0", "29.0", "30.0"]
    thresholds[position] = bad
    path = tmp_path / "airflow-ui.yaml"
    path.write_text(
        "version: 1\n"
        "air_temperature:\n"
        f"  thresholds_c: [{', '.join(thresholds)}]\n"
        "  provisional: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="finite"):
        AirflowUiSettings.from_yaml(path)


def test_the_shipped_config_loads():
    AirflowUiSettings.from_yaml(AIRFLOW_UI_PATH)


# ---------------------------------------------------------------- 表示専用


def test_the_page_says_it_is_read_only():
    assert "表示専用 — ここからは何も変更できません" in _text(PAGE)


@pytest.mark.parametrize("asset", ASSETS)
def test_no_write_paths(asset):
    """フォーム・送信・POST を持たない（決定記録 0028 §2.2 / 0046 §2.2）。"""
    text = _text(asset)
    for forbidden in (
        "<form",
        "<input",
        "<select",
        "<textarea",
        "method:",
        "sendBeacon",
        "XMLHttpRequest",
    ):
        assert forbidden not in text, f"{asset.name} に {forbidden}"
    assert not re.search(r"\b(POST|PUT|PATCH|DELETE)\b", text), asset.name


def test_script_reads_only_documented_endpoints():
    used = set(re.findall(r'"(/api/v1/[a-z/]+)"', _text(SCRIPT)))
    assert used == {"/api/v1/latest", "/api/v1/health", "/api/v1/series", "/api/v1/airflow/config"}
    assert re.findall(r"/api/", _text(MOCK)) == [], "模擬データのファイルは API を叩かない"


# ---------------------------------------------------------------- 模擬データの分離


def test_the_mock_is_not_loaded_by_the_page():
    """模擬データは `?mock=` のときだけ動的に読む。**実データの表示では読み込まない。**"""
    assert "airflow-mock.js" not in _text(PAGE)
    script = _text(SCRIPT)
    assert "airflow-mock.js" in script[script.index("function loadMockScript()") :]


def test_mock_mode_does_not_fetch_real_data():
    script = _text(SCRIPT)
    refresh = script[
        script.index("async function refresh()") : script.index("async function loadScale()")
    ]
    assert "if (page.mockName" in refresh
    start = script[script.index("async function start()") :]
    mock_branch = start[: start.index("return;")]
    assert "refresh(" not in mock_branch
    assert "setInterval" not in mock_branch


def test_mock_mode_is_announced_on_the_whole_page():
    script = _text(SCRIPT)
    assert "模擬データを表示中" in script
    assert 'id="mock-banner"' in _text(PAGE)


def test_the_script_holds_no_mock_values():
    """実データのファイルに模擬の値（判断番号など）を持たせない。"""
    script = _text(SCRIPT)
    assert "#184" not in script
    assert "ColdaisleAirflowMock" not in script.replace("window.ColdaisleAirflowMock", "")


# ---------------------------------------------------------------- 未接続・未取得


def test_control_state_is_unconnected_on_real_data():
    script = _text(SCRIPT)
    assert "control: null, // 実データでは常に null（未接続）" in script
    for name in ("renderControl", "renderZones", "renderTrace", "renderBalance"):
        body = script[script.index(f"function {name}()") :][:2500]
        assert "未接続" in body, f"{name} が未接続を出さない"


def test_missing_values_are_named():
    script = _text(SCRIPT)
    assert '"未計測"' in script  # 入力が無い（CPU 使用率）
    assert '"未取得"' in script  # 入力はあるが値が無い（air.* など）
    for quality in Quality:
        assert f".q-{quality.value}" in _text(STYLES), f"{quality.value} の見た目が無い"


def test_estimates_are_marked():
    """推定値を実測と誤解させない（Issue の受入基準）。"""
    script = _text(SCRIPT)
    assert '"推定"' in script
    assert ".est" in _text(STYLES)


# ---------------------------------------------------------------- 設定・メトリクス


def test_thresholds_are_not_in_the_code():
    script = _text(SCRIPT)
    assert "thresholds_c" in script
    assert '"/api/v1/airflow/config"' in script
    thresholds = yaml.safe_load(_text(AIRFLOW_UI_PATH))["air_temperature"]["thresholds_c"]
    # 区切りの並び（`27, 28` や `27.0, 28.0`）が書かれていないこと
    for first, second in pairwise(thresholds):
        pattern = rf"\b{int(first)}(\.0)?\s*,\s*{int(second)}(\.0)?\b"
        assert not re.search(pattern, script), f"{first}, {second} が書かれている"
    assert "value >= thresholds[index]" in script, "区切りは設定から受け取った配列で比べる"


def test_every_metric_the_page_reads_exists():
    """誤記したメトリクスは常に「未取得」に見え、黙って表示から外れる。"""
    catalog = yaml.safe_load(_text(METRICS_PATH))
    known = set(catalog["metrics"]) | set(catalog["derived"])
    used = set(METRIC_LITERAL.findall(_text(SCRIPT) + _text(STATUS))) | set(
        re.findall(r'data-air="([a-z_.]+)"', _text(PAGE))
    )
    assert used, "メトリクスを1つも参照していない"
    assert used <= known, sorted(used - known)


def test_internal_metric_names_are_not_labels():
    """表示名は日本語の場所の名前。**内部のメトリクス名を画面に出さない。**"""
    script = _text(SCRIPT)
    labels = re.findall(r'label: "([^"]+)"', script)
    assert labels
    assert not [label for label in labels if METRIC_LITERAL.fullmatch(f'"{label}"')]
    assert "textContent = metric" not in script


# ---------------------------------------------------------------- グラフ（案B）


def test_ranges_cover_the_required_periods():
    ranges = re.findall(r'\{ label: "(\w+)", window: "(\w+)", agg: "(\w+)" \}', _text(SCRIPT))
    assert [label for label, _, _ in ranges] == ["1h", "6h", "24h", "7d"]


def test_cpu_group_comes_before_gpu():
    script = _text(SCRIPT)
    groups = script[script.index("const GROUPS") : script.index("const FAN_SERIES")]
    assert groups.index('name: "CPU"') < groups.index('name: "GPU"')
    gpu = groups[groups.index('name: "GPU"') :]
    assert [
        gpu.index(label) for label in ("GPU使用率", "GPUコア温度", "GPU吸気", "GPU排気")
    ] == sorted(gpu.index(label) for label in ("GPU使用率", "GPUコア温度", "GPU吸気", "GPU排気"))


def test_chart_breaks_lines_across_gaps():
    script = _text(SCRIPT)
    assert "expectedStep" in script
    assert "GAP_FACTOR" in script


# ---------------------------------------------------------------- 共通の線


@pytest.mark.parametrize("asset", ASSETS)
def test_no_external_references(asset):
    found = set(URL_PATTERN.findall(_text(asset))) - ALLOWED_URLS
    assert not found, f"{asset.name} が外部を参照している: {sorted(found)}"


@pytest.mark.parametrize("asset", [SCRIPT, MOCK, STATUS])
def test_script_does_not_use_inner_html(asset):
    text = _text(asset)
    for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert dangerous not in text, f"{asset.name} が {dangerous} を使っている"


# ---------------------------------------------------------------- GPU スロットリング（案1）

THROTTLE_FLAGS = (
    "gpu.0.throttle.hw_thermal",
    "gpu.0.throttle.sw_thermal",
    "gpu.0.throttle.hw_power_brake",
    "gpu.0.throttle.sw_power_cap",
    "gpu.0.throttle.hw_slowdown",
)


def _node() -> str:
    """`node` の場所。**CI では無ければ失敗、手元では飛ばす**（決定記録 0044）。"""
    node = shutil.which("node")
    if node is not None:
        return node
    if os.environ.get("CI"):
        pytest.fail("CI では node が必須（決定記録 0044。ci.yml の setup-node を確認）")
    pytest.skip("node が無い（手元では飛ばす。CI では必須）")


def _throttle(metrics: dict[str, dict[str, object]]) -> object:
    """airflow-status.js の `gpuThrottleStatus` を node で実行した結果。"""
    code = (
        "const s = require(process.argv[1]);"
        "console.log(JSON.stringify(s.gpuThrottleStatus(JSON.parse(process.argv[2]))));"
    )
    done = subprocess.run(
        [_node(), "-e", code, str(STATUS), json.dumps({"metrics": metrics})],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def _flags(on: tuple[str, ...] = (), quality: str = "ok") -> dict[str, dict[str, object]]:
    return {
        metric: {"value": 1.0 if metric in on else 0.0, "unit": "flag", "quality": quality}
        for metric in THROTTLE_FLAGS
    }


def test_thermal_throttling_is_red():
    status = _throttle(_flags(on=("gpu.0.throttle.hw_thermal",)))
    assert status == {
        "text": "熱による制限",
        "tone": "bad",
        "reason": "理由：熱による制限（ハードウェア）",
    }


def test_thermal_wins_over_power():
    status = _throttle(_flags(on=("gpu.0.throttle.sw_thermal", "gpu.0.throttle.sw_power_cap")))
    assert status["tone"] == "bad"
    assert status["reason"] == "理由：熱による制限（ソフトウェア）"


@pytest.mark.parametrize("flag", ["gpu.0.throttle.hw_power_brake", "gpu.0.throttle.sw_power_cap"])
def test_power_throttling_is_amber(flag):
    status = _throttle(_flags(on=(flag,)))
    assert status["text"] == "電力による制限"
    assert status["tone"] == "warn"


def test_hw_slowdown_alone_is_amber():
    status = _throttle(_flags(on=("gpu.0.throttle.hw_slowdown",)))
    assert status == {
        "text": "ハードウェアによる減速",
        "tone": "warn",
        "reason": "理由：ハードウェアによる減速",
    }


@pytest.mark.parametrize(
    "metrics",
    [
        {},
        _flags(),
        {m: {"value": None, "unit": "flag", "quality": "missing"} for m in THROTTLE_FLAGS},
        _flags(on=THROTTLE_FLAGS, quality="stale"),
        _flags(on=THROTTLE_FLAGS, quality="suspect"),
    ],
    ids=["absent", "all-zero", "missing", "stale", "suspect"],
)
def test_unknown_or_clear_shows_nothing(metrics):
    """値が無い・古いものは「分からない」。**何も出さない**（「制限なし」とも言わない）。"""
    assert _throttle(metrics) is None


def test_the_gpu_source_is_wired_to_the_status():
    """GPU は `sourceStatus()` 経由で状態を出す。CPU は状態を持たない。"""
    script = _text(SCRIPT)
    sources = script[script.index("const HEAT_SOURCES") : script.index("const STATUS_COLOR")]
    assert "gpuThrottleStatus(latest)" in sources
    assert sources.count("status: null") == 1
    assert "function sourceStatus(source)" in script
    page = _text(PAGE)
    assert page.index('src="airflow-status.js"') < page.index('src="airflow.js"')


def test_no_throttling_is_never_claimed():
    for asset in (SCRIPT, STATUS):
        text = _text(asset)
        assert '"制限なし"' not in text
        assert '"スロットリングなし"' not in text


def test_tlimit_margin_is_not_shown():
    """T.Limit までの余裕は案3（採用されていない）。"""
    for asset in (SCRIPT, STATUS, MOCK):
        assert '"gpu.0.tlimit_margin"' not in _text(asset)


def test_throttle_flags_exist_in_the_catalog():
    catalog = yaml.safe_load(_text(METRICS_PATH))
    assert set(THROTTLE_FLAGS) <= set(catalog["metrics"])


def test_the_throttle_mock_is_selectable():
    script = _text(SCRIPT)
    assert 'throttle: "throttle"' in script
    assert '"gpu.0.throttle.hw_thermal": 1' in _text(MOCK)


@pytest.mark.parametrize("asset", [SCRIPT, MOCK, STATUS])
def test_script_parses(asset):
    """構文エラーで真っ白な画面にならないこと。"""
    assert subprocess.run([_node(), "--check", str(asset)], capture_output=True).returncode == 0


def test_history_reads_the_range_the_api_returns(client):
    """`/series` は範囲を `from` / `to` で返す（`*_ms` ではない）。取り違えると横軸が作れない。"""
    body = client.get("/api/v1/series", params={"metric": "fan.front.rpm", "window": "1h"}).json()
    assert {"from", "to"} <= set(body)
    script = _text(SCRIPT)
    assert "results[0].body.from;" in script
    assert "results[0].body.to;" in script


# ---------------------------------------------------------------- データの出どころ（Codex P2）


def _source_label(source: object) -> dict[str, object]:
    code = (
        "const s = require(process.argv[1]);"
        "console.log(JSON.stringify(s.ingestSourceLabel(JSON.parse(process.argv[2]))));"
    )
    done = subprocess.run(
        [_node(), "-e", code, str(STATUS), json.dumps(source)],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded: dict[str, object] = json.loads(done.stdout)
    return loaded


@pytest.mark.parametrize(
    ("source", "text", "live"),
    [
        ("serial", "実機データ", True),
        ("mock", "模擬データ（MockSource）", False),
        ("replay", "再生データ（過去の記録）", False),
        (None, "出どころ不明（実機のライブ値とは限りません）", False),
        ("something-new", "出どころ不明（実機のライブ値とは限りません）", False),
        ("toString", "出どころ不明（実機のライブ値とは限りません）", False),
    ],
)
def test_the_source_label_follows_health_source(source, text, live):
    """**`?mock=` が無いことを「実機」とみなさない**（決定記録 0046 §2.7）。"""
    assert _source_label(source) == {"text": text, "live": live}


def test_the_page_reads_the_source_from_health():
    script = _text(SCRIPT)
    health = script[script.index("function renderHealth(health)") :][:400]
    assert "page.ingestSource = health.source" in health
    render = script[
        script.index("function renderSource()") : script.index("function renderControl()")
    ]
    assert "ingestSourceLabel(page.ingestSource)" in render
    assert '"実機データ"' not in script, "実機かどうかは health.source から決める"
    assert "実機データ" not in _text(PAGE)


def test_health_reports_the_ingest_source(tmp_path, rules):
    """画面が頼る `health.source` が API から届くこと（デーモンが書く `sys.ingest_source`）。"""
    path = tmp_path / "src.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS,
                readings=(Reading(metric="fan.front.rpm", value=1.0, quality=Quality.OK),),
            )
        )
        store.set_system_state("sys.ingest_source", "replay", at_ms=NOW_MS)
    app = create_app(
        Config(
            db=path,
            quality_rules=QUALITY_RULES_PATH,
            metrics=METRICS_PATH,
            airflow_ui=AIRFLOW_UI_PATH,
        ),
        clock=SimulatedClock(NOW_MS),
    )
    with TestClient(app) as opened:
        assert opened.get("/api/v1/health").json()["source"] == "replay"


# ------------------------------------------------------- グラフの品質・値の注記（Codex P2）


def _status_call(function: str, *args: object) -> object:
    """airflow-status.js の関数を node で実行した結果（引数・戻り値は JSON）。"""
    code = (
        "const s = require(process.argv[1]);"
        f"const out = s.{function}(...JSON.parse(process.argv[2]));"
        "console.log(JSON.stringify(out === undefined ? null : out));"
    )
    done = subprocess.run(
        [_node(), "-e", code, str(STATUS), json.dumps(list(args))],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def test_raw_points_that_are_not_ok_become_gaps():
    """raw の suspect（DS18B20 の -127 など）・missing は値なしにする。ts は残す。"""
    points = [
        {"ts_ms": 1, "value": 24.5, "quality": "ok"},
        {"ts_ms": 2, "value": -127.0, "quality": "suspect"},
        {"ts_ms": 3, "value": None, "quality": "missing"},
        {"ts_ms": 4, "value": 25.0, "quality": "stale"},
        {"ts_ms": 5, "value": 25.5},
        {"ts_ms": 6, "value": 26.0, "quality": "ok"},
    ]
    assert [p["value"] for p in _status_call("usablePoints", points, "raw")] == [
        24.5,
        None,
        None,
        None,
        None,
        26.0,
    ]


@pytest.mark.parametrize("agg", ["1m", "5m", "1h"])
def test_aggregate_points_are_used_as_they_are(agg):
    """集計済みの点は ok の行だけで作られている（rollup）ので、quality が無くても使う。"""
    points = [{"ts_ms": 1, "value": 24.5, "min": 24.0, "max": 25.0, "ok_count": 60}]
    assert _status_call("usablePoints", points, agg) == points


def test_the_graph_reads_only_usable_points():
    script = _text(SCRIPT)
    load = script[script.index("async function loadHistory()") :]
    load = load[: load.index("} catch (error) {")]
    assert "usablePoints" in load
    assert "usable(body.points, body.agg)" in load


@pytest.mark.parametrize(
    ("source", "expected", "measured"),
    [
        ("serial", "実機の実測値です", True),
        ("mock", "模擬データ（MockSource）", False),
        ("replay", "過去の記録の再生", False),
        (None, "出どころは不明", False),
        ("something-new", "出どころは不明", False),
        ("toString", "出どころは不明", False),
    ],
)
def test_the_measured_note_follows_health_source(source, expected, measured):
    """**serial のときだけ「実測値です」と言い切る**（見出しの表示と同じ判断）。"""
    note = _status_call("measuredNote", source)
    assert isinstance(note, str)
    assert expected in note
    assert ("実測値です" in note) is measured


def test_the_measured_note_waits_for_health():
    code = (
        "const s = require(process.argv[1]);console.log(JSON.stringify(s.measuredNote(undefined)));"
    )
    done = subprocess.run(
        [_node(), "-e", code, str(STATUS)], capture_output=True, text=True, check=True
    )
    note = json.loads(done.stdout)
    assert "確認中" in note
    assert "実測値です" not in note


def test_the_control_note_does_not_claim_measured_values():
    script = _text(SCRIPT)
    control = script[script.index("function renderControl()") :][:1500]
    assert "measuredNote(displaySource())" in control
    assert "使用率は実測値です" not in script


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("serial", "実測"),
        ("mock", "模擬"),
        ("replay", "再生"),
        (None, "出どころ不明"),
        ("something-new", "出どころ不明"),
        ("toString", "出どころ不明"),
    ],
)
def test_the_ingest_kind_follows_health_source(source, kind):
    """取り込み経路の札は **serial のときだけ「実測」**（measuredNote と同じ判断）。"""
    assert _status_call("ingestKind", source) == kind
    assert _status_call("metricKind", "air.room", source, False) == kind


def test_the_ingest_metrics_match_the_channel_table():
    """health.source が当てはまるのは取り込みデーモンが書く air.* だけ（channels.py）。"""
    script = _text(STATUS)
    listed = re.findall(r'"(air\.[a-z_]+)"', script)
    assert sorted(listed) == sorted(CHANNEL_TO_METRIC.values())


@pytest.mark.parametrize("source", ["serial", "mock", "replay", None, "something-new"])
@pytest.mark.parametrize(
    "metric", ["fan.front.pwm", "fan.top.rpm", "cpu.package", "gpu.0.core", "gpu.0.utilization"]
)
def test_internal_telemetry_is_not_labelled_by_the_ingest_source(metric, source):
    """内部テレメトリは別の経路。health.source で「実測」「模擬」「再生」と言わない。"""
    assert _status_call("metricKind", metric, source, False) == "読み取り値"


@pytest.mark.parametrize("metric", ["air.room", "fan.front.pwm", "gpu.0.core"])
def test_mock_query_makes_every_value_mock(metric):
    assert _status_call("metricKind", metric, "serial", True) == "模擬"


@pytest.mark.parametrize("source", ["serial", "mock", "replay", None])
def test_the_measured_note_separates_internal_telemetry(source):
    note = _status_call("measuredNote", source)
    assert isinstance(note, str)
    assert "空気の温度" in note
    assert "内部テレメトリの読み取り値" in note


def test_the_page_has_no_fixed_measured_wording():
    """「実測」を固定の文言で出さない。札は valueKind から、`?mock=` は mock とみなす。"""
    page_text = re.sub(r"<!--.*?-->", "", _text(PAGE), flags=re.S)
    assert "実測" not in page_text
    literals = re.findall(r"\"[^\"\n]*\"|`[^`\n]*`|'[^'\n]*'", _text(SCRIPT))
    assert not [text for text in literals if "実測" in text]
    script = _text(SCRIPT)
    assert 'page.mockName ? "mock" : page.ingestSource' in script
    assert "PWM（${valueKind(zone.pwm)}）" in script
    render = script[
        script.index("function renderSource()") : script.index("function renderControl()")
    ]
    assert 'getElementById("value-kind-key")' in render
    assert 'id="value-kind-key"' in _text(PAGE)


def test_the_header_names_each_provenance_on_both_tabs():
    """見出しは経路ごとに出す。**health.source は空気の温度の出どころとして書く**（Codex P2）。"""
    page = _text(PAGE)
    header = page[page.index('<header class="af-header">') : page.index("</header>")]
    assert 'id="source-label"' in header
    assert "空気の温度（現在の取り込み元）：" in header
    assert 'id="telemetry-label"' in header
    # 見出しはタブの外（どちらのタブでも見える）
    assert page.index("</header>") < page.index('<main id="view-now"')
    assert page.index("</header>") < page.index('<main id="view-graph"')

    script = _text(SCRIPT)
    assert 'const AIR_SOURCE_PREFIX = "空気の温度（現在の取り込み元）：";' in script
    render = script[
        script.index("function renderSource()") : script.index("function renderControl()")
    ]
    assert "`${AIR_SOURCE_PREFIX}出どころを確認中…`" in render
    assert "`${AIR_SOURCE_PREFIX}${" in render
    assert 'getElementById("telemetry-label")' in render
    assert 'valueKind("fan.front.pwm")' in render


def test_the_graph_says_history_may_come_from_another_source():
    """履歴の点ごとの出どころは API に無い。グラフのタブで「現在の取り込み元」だけだと言う。"""
    page = _text(PAGE)
    graph = page[page.index('<main id="view-graph"') :]
    graph = graph[: graph.index("</main>")]
    note = graph[graph.index('id="graph-source-note"') :]
    note = note[: note.index("</p>")]
    assert "現在の取り込み元" in note
    assert "過去の点" in note
    assert "実測" not in note


def test_a_failed_range_load_clears_the_old_graph():
    """取得に失敗した期間の下に、前の期間の線・軸・読み取り値を残さない（Codex P2）。"""
    script = _text(SCRIPT)
    clear = script[script.index("function clearGraph()") :]
    clear = clear[: clear.index("\n}\n")]
    for reset in (
        "graph.data = new Map();",
        "graph.fromMs = null;",
        "graph.toMs = null;",
        "graph.cursorMs = null;",
        "graph.loaded = false;",
        "cursors.length = 0;",
    ):
        assert reset in clear, reset
    for element in ("chart-temp", "chart-fan", "util-rows", "readout", "fan-legend"):
        assert f'"{element}"' in clear, element
    assert 'getElementById("graph-agg").textContent = ""' in clear
    # 描き直しは fromMs が null なら何もしない（失敗後にリサイズ等で古い軸が戻らない）
    draw = script[script.index("function drawGraph()") :][:120]
    assert "if (graph.fromMs === null) return;" in draw

    load = script[script.index("async function loadHistory()") :]
    load = load[: load.index("\n}\n")]
    failure = load[load.index("} catch (error) {") :]
    # 古い要求の失敗で新しい期間を消さない → 消してから注記を書く
    assert failure.index("if (token !== graph.token) return;") < failure.index("clearGraph();")
    assert failure.index("clearGraph();") < failure.index("履歴を取得できません")


def test_a_late_response_cannot_overwrite_a_newer_range():
    script = _text(SCRIPT)
    load = script[script.index("async function loadHistory()") :]
    load = load[: load.index("\n}\n")]
    assert "const token = ++graph.token;" in load
    success = load[: load.index("} catch (error) {")]
    assert success.index("if (token !== graph.token) return;") < success.index("graph.data = ")
