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
from coldaisle.internal_telemetry import (
    SOURCE_STATE_PREFIX,
    TELEMETRY_KIND_KEY,
    TelemetrySourceKind,
)
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
        "cpu_utilization": {"measured": None},  # collector の状態が無い = 分からない
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
    assert '"未計測"' in script  # 取得していない（/latest に CPU 使用率のキーが無い等）
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
    assert "measuredNote(displaySource(), page.telemetrySource, page.latest)" in control
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
    assert _status_call("metricKind", "air.room", source, False, "hardware", None) == kind


def test_the_ingest_metrics_match_the_channel_table():
    """health.source が当てはまるのは取り込みデーモンが書く air.* だけ（channels.py）。"""
    script = _text(STATUS)
    listed = re.findall(r'"(air\.[a-z_]+)"', script)
    assert sorted(listed) == sorted(CHANNEL_TO_METRIC.values())


@pytest.mark.parametrize("source", ["serial", "mock", "replay", None, "something-new"])
@pytest.mark.parametrize(
    "metric",
    [
        "fan.front.pwm",
        "fan.top.rpm",
        "cpu.package",
        "cpu.utilization",
        "gpu.0.core",
        "gpu.0.utilization",
    ],
)
def test_internal_telemetry_is_not_labelled_by_the_ingest_source(metric, source):
    """内部テレメトリは別の経路。health.source で「実測」「模擬」「再生」と言わない。"""
    delivered = _latest({metric: _ok(1.0)})
    assert _status_call("metricKind", metric, source, False, None, delivered) == "読み取り値"


@pytest.mark.parametrize("source", ["serial", "mock", "replay", None, "something-new"])
@pytest.mark.parametrize("metric", ["fan.front.pwm", "cpu.utilization", "gpu.0.core"])
def test_internal_telemetry_follows_its_own_source_kind(metric, source):
    """内部テレメトリの札は health.telemetry_source で決まり、取り込みの種類に左右されない。"""
    delivered = _latest({metric: _ok(1.0)})
    assert _status_call("metricKind", metric, source, False, "hardware", delivered) == "実測"
    assert _status_call("metricKind", metric, source, False, "mock", delivered) == "模擬"


@pytest.mark.parametrize("metric", ["air.room", "fan.front.pwm", "gpu.0.core"])
def test_mock_query_makes_every_value_mock(metric):
    delivered = _latest({metric: _ok(1.0)})
    assert _status_call("metricKind", metric, "serial", True, "hardware", delivered) == "模擬"


@pytest.mark.parametrize("source", ["serial", "mock", "replay", None])
def test_the_measured_note_separates_internal_telemetry(source):
    note = _status_call("measuredNote", source, None, _latest({"fan.front.pwm": _ok(48.0)}))
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
    assert "telemetryKind()" in render


# ---------------------------------- 内部テレメトリの出どころの札（決定記録 0049）


def _ok(value: float, quality: str = "ok") -> dict[str, object]:
    """`/api/v1/latest` の1件（届いている値）。"""
    return {"value": value, "quality": quality, "age_seconds": 1.0}


def _latest(metrics: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"metrics": metrics, "derived": {}}


TELEMETRY_METRIC = "fan.front.pwm"


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        ("hardware", "実測"),
        ("mock", "模擬"),
        (None, "読み取り値"),
        ("something-new", "読み取り値"),
        ("toString", "読み取り値"),
    ],
)
def test_a_delivered_telemetry_value_follows_the_source_kind(kind, label):
    """届いている値だけに札を付ける。**知らない種類・記録の無い DB では実測と言わない**。"""
    assert _status_call("telemetryKind", kind, _ok(48.0)) == label


def test_a_delivered_telemetry_value_waits_for_health():
    """health がまだ届いていない（undefined）間は「確認中」。実測とは言わない。"""
    code = (
        "const s = require(process.argv[1]);"
        "console.log(JSON.stringify(s.telemetryKind(undefined, JSON.parse(process.argv[2]))));"
    )
    done = subprocess.run(
        [_node(), "-e", code, str(STATUS), json.dumps(_ok(48.0))],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(done.stdout) == "確認中"


@pytest.mark.parametrize(
    ("item", "why"),
    [
        ({"value": 48.0, "quality": "stale"}, "古い値"),
        ({"value": None, "quality": "missing"}, "未取得"),
        # hwmon は有限でない読み値を value: null / quality: suspect で保存する。
        # 画面は値が無いので「未取得」と出すため、ここに札を付けると表示の無い値に実測が付く
        ({"value": None, "quality": "suspect"}, "値の無い suspect"),
        (None, "キーが無い"),
    ],
)
def test_a_value_that_is_not_delivered_stays_neutral(item, why):
    """届いていない値は種類が hardware でも「読み取り値」のまま（決定記録 0049 §2.5）。"""
    assert _status_call("telemetryKind", "hardware", item) == "読み取り値", why


def test_a_suspect_value_that_has_a_number_is_delivered():
    """`ok` / `suspect` は届いている（Server Health の数え方と同じ。決定記録 0042 §2.4）。"""
    assert _status_call("telemetryKind", "hardware", _ok(-127.0, "suspect")) == "実測"


def test_a_stale_telemetry_value_is_not_labelled_by_the_current_kind():
    """DB を使い回して mock → hardware に切り替えても、古い模擬の行に「実測」を付けない。"""
    stale = _latest({TELEMETRY_METRIC: {"value": 48.0, "quality": "stale"}})
    assert _status_call("metricKind", TELEMETRY_METRIC, "serial", False, "hardware", stale) == (
        "読み取り値"
    )


def test_the_heading_label_follows_the_delivered_values():
    """見出し・凡例は「いま届いている値の出どころ」。1つも届いていなければ中立。"""
    delivered = _latest({TELEMETRY_METRIC: _ok(48.0)})
    assert _status_call("telemetrySummaryKind", "hardware", delivered, False) == "実測"
    assert _status_call("telemetrySummaryKind", "mock", delivered, False) == "模擬"
    assert _status_call("telemetrySummaryKind", None, delivered, False) == "読み取り値"

    nothing = _latest({TELEMETRY_METRIC: {"value": 48.0, "quality": "stale"}})
    assert _status_call("telemetrySummaryKind", "hardware", nothing, False) == "読み取り値"
    assert _status_call("telemetrySummaryKind", "hardware", _latest({}), False) == "読み取り値"


def test_the_heading_label_ignores_the_ingest_metrics():
    """空気の温度が届いていても、内部テレメトリの札にはしない（経路が違う）。"""
    air_only = _latest({"air.room": _ok(24.5)})
    assert _status_call("telemetrySummaryKind", "hardware", air_only, False) == "読み取り値"


def test_the_heading_label_waits_for_health():
    code = (
        "const s = require(process.argv[1]);"
        "const latest = JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(s.telemetrySummaryKind(undefined, latest, false)));"
    )
    done = subprocess.run(
        [_node(), "-e", code, str(STATUS), json.dumps(_latest({TELEMETRY_METRIC: _ok(48.0)}))],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(done.stdout) == "確認中"


def test_the_mock_query_wins_over_the_source_kind():
    delivered = _latest({TELEMETRY_METRIC: _ok(48.0)})
    assert _status_call("telemetrySummaryKind", "hardware", delivered, True) == "模擬"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("hardware", "いま届いている値は実機の読み取り値です"),
        ("mock", "模擬の adapter の値です"),
        (None, "内部テレメトリの読み取り値です"),
    ],
)
def test_the_telemetry_note_speaks_only_about_delivered_values(kind, expected):
    note = _status_call("telemetryNote", kind, _latest({TELEMETRY_METRIC: _ok(48.0)}))
    assert isinstance(note, str)
    assert expected in note
    # **過去の値の出どころは表示しない**（模擬の adapter で書いた区間が履歴に混ざりうる）
    assert "古い値とグラフの過去の値の出どころは表示しません。" in note


def test_the_telemetry_note_does_not_claim_the_past():
    """グラフの点を「実測値」と言い切らない（決定記録 0049 が 0051 §2.3 の文言を置き換える）。"""
    note = _status_call("telemetryNote", "hardware", _latest({TELEMETRY_METRIC: _ok(48.0)}))
    assert "実測値" not in note
    assert "過去" in note


def test_the_page_reads_the_telemetry_kind_from_health():
    script = _text(SCRIPT)
    health = script[script.index("function renderHealth(health)") :][:600]
    assert "page.telemetrySource = health.telemetry_source" in health
    value_kind = script[script.index("function valueKind(metric)") :][:400]
    assert "page.telemetrySource" in value_kind
    assert "page.latest" in value_kind
    # health を取れなかったときは種類を残さない（前回の「実測」を出し続けない）
    refresh = script[script.index("async function refresh()") :]
    refresh = refresh[: refresh.index("async function loadScale()")]
    assert "page.telemetrySource = null;" in refresh


def test_the_pwm_label_uses_the_value_kind():
    """0046 §2.5 の固定の札（`PWM（実測）`）は、値ごとの札に置き換わる。"""
    script = _text(SCRIPT)
    assert "PWM（${valueKind(zone.pwm)}）" in script
    assert "PWM（実測）" not in script
    assert "PWM（実測）" not in _text(PAGE)


def test_health_reports_the_telemetry_source(tmp_path, rules):
    """`sys.telemetry_kind` の現在値が `/api/v1/health` に出る（決定記録 0049 §2.4）。"""
    path = tmp_path / "kind.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS,
                readings=(Reading(metric="fan.front.rpm", value=1.0, quality=Quality.OK),),
            )
        )
        store.set_system_state(TELEMETRY_KIND_KEY, TelemetrySourceKind.HARDWARE.value, at_ms=NOW_MS)
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
        body = opened.get("/api/v1/health").json()
    assert body["telemetry_source"] == "hardware"
    # 取り込みの種類とは別の経路。混ぜない
    assert body["source"] is None


def test_health_reports_no_telemetry_source_on_an_older_db(tmp_path, rules):
    """キーを書かない版のデーモンで貯めた DB では null（画面は「読み取り値」のまま）。"""
    path = tmp_path / "older.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS,
                readings=(Reading(metric="fan.front.rpm", value=1.0, quality=Quality.OK),),
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
        assert opened.get("/api/v1/health").json()["telemetry_source"] is None


def test_the_graph_says_history_may_come_from_another_source():
    """履歴の点ごとの出どころは API に無い。グラフのタブで「現在の取り込み元」だけだと言う。"""
    page = _text(PAGE)
    graph = page[page.index('<main id="view-graph"') :]
    graph = graph[: graph.index("</main>")]
    note = graph[graph.index('id="graph-source-note"') :]
    note = note[: note.index("</p>")]
    assert "現在の取り込み元" in note
    assert "過去の点" in note
    # 内部テレメトリも同じ。過去の点の出どころは主張しない（決定記録 0049 §2.5）
    assert "グラフの過去の点の出どころは表示しません" in note
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


def _function(script: str, signature: str) -> str:
    body = script[script.index(signature) :]
    return body[: body.index("\n}\n")]


def test_latest_and_health_are_applied_independently():
    """/latest と /health の片方が失敗しても、もう片方は反映する。失敗は別々に言う（Codex P2）。"""
    refresh = _function(_text(SCRIPT), "async function refresh()")
    assert "Promise.allSettled([" in refresh
    assert "Promise.all(" not in refresh
    assert 'latest.status === "fulfilled"' in refresh
    assert 'health.status === "fulfilled"' in refresh
    assert "renderHealth(health.value)" in refresh
    assert "page.latest = latest.value" in refresh
    # 失敗した側は古い表示を残さない（値は未取得、出どころは不明、鮮度の表示は消す）
    assert "page.latest = null;" in refresh
    assert "page.ingestSource = null;" in refresh
    assert "/api/v1/latest）を取得できません" in refresh
    assert "/api/v1/health）を取得できません" in refresh
    assert 'showBanner("api-banner"' in refresh
    assert refresh.index('showBanner("api-banner"') < refresh.index("renderNow();")
    assert 'id="api-banner"' in _text(PAGE)


def test_changing_the_range_clears_the_graph_before_loading():
    """新しい期間の応答が届くまで、前の期間のグラフを出さない（Codex P2）。"""
    ranges = _function(_text(SCRIPT), "function renderRanges()")
    click = ranges[ranges.index('addEventListener("click"') :]
    assert "graph.range = range;" in click
    assert click.index("clearGraph();") < click.index("loadHistory();")
    assert click.index("読み込み中…") < click.index("loadHistory();")


# ------------------------------------------------------- CPU 使用率（#145 / 決定記録 0047）


def test_cpu_utilization_is_wired():
    """熱源の CPU とグラフの「CPU使用率」は `cpu.utilization` を読む（「未計測」固定ではない）。"""
    script = _text(SCRIPT)
    sources = script[script.index("const HEAT_SOURCES") : script.index("const STATUS_COLOR")]
    cpu = sources[sources.index('name: "CPU"') : sources.index('name: "GPU"')]
    assert 'util: "cpu.utilization"' in cpu
    assert "measured: () => page.cpuMeasured" in cpu, "計測していない設定では「未計測」と出す"
    assert "util: null" not in sources
    assert script.count("utilReading(source)") == 3  # 定義 + 側面図 + 熱源パネル
    assert "reading(source.util" not in script.replace("return reading(source.util", "")
    groups = script[script.index("const GROUPS") : script.index("const FAN_SERIES")]
    assert re.search(r'key: "cpu_util", label: "CPU使用率", metric: "cpu\.utilization"', groups)
    assert "page.cpuMeasured = cpu && typeof cpu.measured" in script


def test_mock_has_cpu_utilization():
    """模擬データ（通常・異常時。throttle は通常から作る）にも CPU 使用率がある。"""
    assert _text(MOCK).count('"cpu.utilization":') == 2


def _util(page: dict[str, object], source: dict[str, object] | None = None) -> dict[str, object]:
    """airflow.js の `utilReading()` を、`page` を差し替えて node で実行した結果。

    CPU の熱源は `measured: () => page.cpuMeasured`。`source` に `"measured": null` を渡すと
    `measured` を持たない熱源（GPU）として試す。
    """
    script = _text(SCRIPT)
    start = script.index("function fmt(")
    end = script.index("function derivedValue(")
    tag = script[script.index("const QUALITY_TAG") :]
    tag = tag[: tag.index(";") + 1]
    code = (
        f"{tag}\n{script[start:end]}\n"
        "const page = JSON.parse(process.argv[1]);"
        "const spec = JSON.parse(process.argv[2]);"
        "const source = { util: spec.util };"
        "if (spec.measured !== null) source.measured = () => page.cpuMeasured;"
        "console.log(JSON.stringify(utilReading(source)));"
    )
    spec = {"util": "cpu.utilization", "measured": True, **(source or {})}
    done = subprocess.run(
        [_node(), "-e", code, json.dumps(page), json.dumps(spec)],
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, object] = json.loads(done.stdout)
    return result


def _cpu(value: float | None, quality: str) -> dict[str, object]:
    return {
        "metrics": {
            "cpu.utilization": {"value": value, "unit": "%", "quality": quality, "age_seconds": 0.4}
        }
    }


UNMEASURED = {"text": "未計測", "quality": "missing", "value": None}


def test_cpu_utilization_reading_ok():
    result = _util({"latest": _cpu(29.4, "ok"), "cpuMeasured": True, "mockName": None})
    assert result["text"] == "29%"
    assert result["quality"] == "ok"
    assert result.get("tag") is None


@pytest.mark.parametrize(("quality", "tag"), [("stale", "古い"), ("suspect", "疑わしい")])
def test_cpu_utilization_reading_marks_quality(quality, tag):
    result = _util({"latest": _cpu(29.0, quality), "cpuMeasured": True, "mockName": None})
    assert result["text"] == "29%"
    assert result["tag"] == tag


@pytest.mark.parametrize(
    "latest",
    [
        _cpu(None, "missing"),  # proc_stat 有効・Linux 以外で保存される missing の行（Codex P2）
        _cpu(31.0, "ok"),  # 無効にする前に保存された行
        {"metrics": {}},
        None,
    ],
)
def test_unmeasured_cpu_utilization_ignores_stored_rows(latest):
    """設定で計測していないなら、`/latest` の行の有無・値に関わらず「未計測」。"""
    assert _util({"latest": latest, "cpuMeasured": False, "mockName": None}) == UNMEASURED


def test_missing_cpu_utilization_is_not_acquired_while_measured():
    """計測する設定で値が無い（起動直後の1サンプル等。0047 §2.2）→ 「未取得」。"""
    page = {"latest": _cpu(None, "missing"), "cpuMeasured": True, "mockName": None}
    assert _util(page)["text"] == "未取得"
    assert _util({**page, "latest": {"metrics": {}}})["text"] == "未取得"


def test_unknown_config_does_not_claim_unmeasured():
    """設定を読めない（null）ときは「未計測」と決めつけず、値の有無で出す。"""
    assert _util({"latest": _cpu(29.0, "ok"), "cpuMeasured": None, "mockName": None})["text"] == (
        "29%"
    )
    assert _util({"latest": None, "cpuMeasured": None, "mockName": None})["text"] == "未取得"


def test_mock_data_is_not_gated_by_the_host_config():
    """模擬データは画面側の値。API のホストの設定で「未計測」にしない。"""
    page = {"latest": _cpu(29.0, "ok"), "cpuMeasured": False, "mockName": "normal"}
    assert _util(page)["text"] == "29%"


def test_sources_without_the_flag_read_the_value():
    """GPU など `measured` を持たない熱源は従来どおり。"""
    page = {
        "latest": {"metrics": {"gpu.0.utilization": {"value": 93, "unit": "%", "quality": "ok"}}},
        "cpuMeasured": False,
        "mockName": None,
    }
    assert _util(page, {"util": "gpu.0.utilization", "measured": None})["text"] == "93%"


# ------------------------------------------------------- airflow/config の cpu_utilization（#145）


@pytest.mark.parametrize(
    ("state", "measured"),
    [
        ("disabled", False),  # proc_stat.enabled: false
        ("ok", True),
        ("degraded", True),
        ("unavailable", None),  # Linux 以外と一時的な読み取り失敗を区別できない（0047 §2.2）
        ("something-new", None),
        (None, None),  # collector が一度も状態を書いていない
    ],
)
def test_cpu_utilization_measured_follows_the_collector_state(tmp_path, rules, state, measured):
    """collector が保存した proc_stat の状態で決める（API 側の設定・OS は見ない）。"""
    path = tmp_path / "state.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        if state is not None:
            store.set_system_state(SOURCE_STATE_PREFIX + "proc_stat", state, at_ms=NOW_MS)
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
        body = opened.get("/api/v1/airflow/config").json()
    assert body["cpu_utilization"] == {"measured": measured}


def test_cpu_utilization_measured_ignores_the_api_side_config(tmp_path, rules):
    """API の設定で proc_stat が有効でも、collector が disabled なら未計測（Codex P2）。"""
    loaded = yaml.safe_load((CONFIG_DIR / "internal-telemetry.yaml").read_text(encoding="utf-8"))
    assert loaded["proc_stat"]["enabled"] is True
    path = tmp_path / "other.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.set_system_state(SOURCE_STATE_PREFIX + "proc_stat", "disabled", at_ms=NOW_MS)
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
        assert opened.get("/api/v1/airflow/config").json()["cpu_utilization"]["measured"] is False
