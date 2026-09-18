"""エアフロー / ファン制御の画面（#106 / 決定記録 0046）。

見た目そのものの確認は人間が行う（決定記録 0011 と同じ）。ここは「壊れていたら気づける」線を引く。

1. **表示専用**（書き込みの経路が無い）
2. **実データと模擬データを混ぜない**
3. **色の区切りをコードに書かない**（config/airflow-ui.yaml → `GET /api/v1/airflow/config`）
4. **未接続・未取得を正常値のように見せない**
5. オフラインで見える・API の文字列を HTML として解釈しない
"""

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
ASSETS = [PAGE, SCRIPT, MOCK, STYLES]

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
    used = set(METRIC_LITERAL.findall(_text(SCRIPT))) | set(
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


@pytest.mark.parametrize("asset", [SCRIPT, MOCK])
def test_script_does_not_use_inner_html(asset):
    text = _text(asset)
    for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert dangerous not in text, f"{asset.name} が {dangerous} を使っている"


def test_throttle_hook_exists_but_shows_nothing_yet():
    """GPU スロットリング表示の差し込み口（決定記録 0046 §2.6）。**まだ何も表示しない。**"""
    script = _text(SCRIPT)
    assert "function sourceStatus(source)" in script
    sources = script[script.index("const HEAT_SOURCES") : script.index("function sourceStatus")]
    assert sources.count("status: null") == 2
    assert "throttle." not in script


@pytest.mark.parametrize("asset", [SCRIPT, MOCK])
def test_script_parses(asset):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node が無い")
    assert subprocess.run([node, "--check", str(asset)], capture_output=True).returncode == 0


def test_history_reads_the_range_the_api_returns(client):
    """`/series` は範囲を `from` / `to` で返す（`*_ms` ではない）。取り違えると横軸が作れない。"""
    body = client.get("/api/v1/series", params={"metric": "fan.front.rpm", "window": "1h"}).json()
    assert {"from", "to"} <= set(body)
    script = _text(SCRIPT)
    assert "results[0].body.from;" in script
    assert "results[0].body.to;" in script
