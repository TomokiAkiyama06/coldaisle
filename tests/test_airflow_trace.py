"""エアフロー画面の decision trace の読み方（#106 / 決定記録 0071 §2.3 / §2.6）。

`airflow-trace.js` の変換を node で実際に動かして確かめる（決定記録 0044。CI では node が必須）。

1. **版の解釈は1か所**（`airflow-trace.js`）。v1〜v11 の fixture を読める
2. **3つの状態を混ぜない**: その版に欄が無い／欄はあるが値が無い／画面が知らない版
3. Safety override・Fallback・OOD を通常状態と区別して出す
4. **古さは読む側で判定する**（`runtime.tick_period_ms` × 設定の倍数。v1〜v7 は判定しない）
5. 画面が頼る API の契約（`/control/latest` の形、`/airflow/config` の `control_trace`）
"""

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coldaisle.api.airflow import AirflowUiSettings
from coldaisle.api.app import WEB_ROOT, Config, create_app
from coldaisle.clock import SimulatedClock
from coldaisle.store import SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH

TRACE = WEB_ROOT / "airflow-trace.js"
SCRIPT = WEB_ROOT / "airflow.js"
PAGE = WEB_ROOT / "airflow.html"
FIXTURES = Path(__file__).parent / "fixtures"
AIRFLOW_UI_PATH = CONFIG_DIR / "airflow-ui.yaml"
NOW_MS = 1_787_616_020_000

NOT_IN_VERSION = "この版の記録には無い"
FIXTURE_VERSIONS = range(1, 11)


def _node() -> str:
    """`node` の場所。**CI では無ければ失敗、手元では飛ばす**（決定記録 0044）。"""
    node = shutil.which("node")
    if node is not None:
        return node
    if os.environ.get("CI"):
        pytest.fail("CI では node が必須（決定記録 0044。ci.yml の setup-node を確認）")
    pytest.skip("node が無い（手元では飛ばす。CI では必須）")


def _call(function: str, *args: object) -> Any:
    """airflow-trace.js の関数を node で実行した結果（引数・戻り値は JSON）。"""
    code = (
        "const t = require(process.argv[1]);"
        f"const out = t.{function}(...JSON.parse(process.argv[2]));"
        "console.log(JSON.stringify(out === undefined ? null : out));"
    )
    done = subprocess.run(
        [_node(), "-e", code, str(TRACE), json.dumps(list(args))],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def _body(version: int) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / f"control_tick_v{version}.json").read_text(encoding="utf-8")
    )
    return loaded


def _v11() -> dict[str, Any]:
    """v11 の形（決定記録 0073 §2.5）。v10 に `air_balance` と `zones.*.applied_demand` を足す。"""
    body = _body(10)
    body["schema_version"] = 11
    for zone in body["zones"].values():
        zone["applied_demand"] = 0.4
        zone["estimated_flow"] = 0.5
    body["air_balance"] = {
        "schema_version": 1,
        "status": "enabled",
        "disabled_reason": None,
        "demand_basis": "applied",
        "q_front": 0.5,
        "q_rear": 0.5,
        "q_top": 0.5,
        "estimated_intake": 0.5,
        "estimated_exhaust": 1.0,
        "balance_ratio": 2.0,
        "state": "exhaust_heavy",
        "thermal_limited": False,
        "thermal_reasons": [],
    }
    return body


def _latest(body: dict[str, Any] | None, *, age_ms: int = 500, seq: int = 7) -> dict[str, Any]:
    """`GET /api/v1/control/latest` の応答の形（外枠 + 保存した JSON）。"""
    if body is None:
        return {"trace": None}
    return {
        "trace": {
            "seq": seq,
            "ts_ms": body.get("ts_ms", 0),
            "ts": "2026-08-24T00:00:00+00:00",
            "tick_id": body.get("tick_id", 0),
            "schema_version": body["schema_version"],
            "age_ms": age_ms,
            "body": body,
        }
    }


def _convert(
    body: dict[str, Any] | None, *, age_ms: int = 500, multiplier: float | None = 3.0
) -> Any:
    return _call("controlFromLatest", _latest(body, age_ms=age_ms), multiplier)


def _chip(result: Any, key: str) -> dict[str, Any]:
    chips = [chip for chip in result["control"]["chips"] if chip["k"] == key]
    assert chips, f"{key} の項目が無い"
    chip: dict[str, Any] = chips[0]
    return chip


def _steps(result: Any, zone: str) -> dict[str, dict[str, Any]]:
    return {step["title"]: step for step in result["control"]["zones"][zone]["trace"]}


# ---------------------------------------------------------------- 版（0071 §2.3）


@pytest.mark.parametrize("version", FIXTURE_VERSIONS)
def test_every_stored_version_is_read(version):
    """保存済みの v1〜v10 の fixture を、同じ `page.control` の形にできる。"""
    result = _convert(_body(version))
    assert result["status"] == "ok"
    assert result["schema_version"] == version
    control = result["control"]
    assert set(control["zones"]) == {"front", "rear", "top"}
    for zone in control["zones"].values():
        assert {"requested", "effective", "floor", "bound_by", "trace"} <= set(zone)
    assert control["decision_id"] == "#7"


def test_v11_air_balance_is_read():
    result = _convert(_v11())
    assert result["status"] == "ok"
    balance = result["control"]["balance"]
    assert balance["ratio"] == 2.0
    assert balance["state"] == "排気が多い"
    assert balance["position"] is None  # 目標帯は trace に無い。推測で描かない
    assert "適用した出力 40%" in _steps(result, "front")["ファン"]["detail"]


def test_v11_disabled_air_balance_is_not_called_balanced():
    body = _v11()
    body["air_balance"].update(
        status="disabled",
        disabled_reason="uncalibrated",
        balance_ratio=None,
        state="disabled",
        q_front=None,
        q_rear=None,
        q_top=None,
        estimated_intake=None,
        estimated_exhaust=None,
    )
    balance = _convert(body)["control"]["balance"]
    assert balance["state"] == "使っていない（未校正）"
    assert balance["ratio"] is None
    assert balance["tone"] != "ok"


@pytest.mark.parametrize("version", [1, 4, 7, 10])
def test_a_field_the_version_does_not_have_is_named_so(version):
    """v10 以前に Air Balance の記録は無い。**「釣り合っている」「無し」と言わない。**"""
    result = _convert(_body(version))
    assert result["control"]["balance"] == {"absent": NOT_IN_VERSION}
    assert "適用した出力：この版の記録には無い" in _steps(result, "top")["ファン"]["detail"]


def test_model_gate_absent_in_v4_but_empty_in_v5():
    """v4 に model_gate の欄は無い（→ 版に無い）。v5 で null は「判定なし」（→ 値が無い）。"""
    v4 = _convert(_body(4))
    assert _chip(v4, "予測の信頼度") == {"k": "予測の信頼度", "v": NOT_IN_VERSION, "tone": "absent"}
    assert _chip(v4, "想定外の状態")["v"] == NOT_IN_VERSION
    assert _steps(v4, "front")["信頼度チェック"]["value"] == NOT_IN_VERSION

    v5 = _convert(_body(5))
    assert "model_gate" not in _body(5)  # None の欄は書き出されない。キーの有無では決めない
    assert _chip(v5, "予測の信頼度")["v"].startswith("判定なし")
    assert _chip(v5, "想定外の状態")["v"].startswith("判定なし")
    assert _chip(v5, "想定外の状態").get("tone") != "ok", "判定が無いのに「なし（正常）」と言わない"


def test_supervisor_absent_in_v2_but_empty_in_v9():
    """0071 §2.3 の表の例そのもの: v9 の `supervisor: null` は「出力なし」。"""
    assert _chip(_convert(_body(2)), "運転方針")["v"] == NOT_IN_VERSION
    v9 = _chip(_convert(_body(9)), "運転方針")
    assert v9["v"] != NOT_IN_VERSION
    assert "出力なし" in v9["v"]


def test_workload_regime_absent_in_v1_only():
    assert _chip(_convert(_body(1)), "負荷の傾向")["v"] == NOT_IN_VERSION
    assert _chip(_convert(_body(2)), "負荷の傾向")["v"] != NOT_IN_VERSION


@pytest.mark.parametrize("version", [12, 999])
def test_an_unknown_version_is_not_shown(version):
    """画面が知らない版は「未対応の版」。**制御由来の項目を出さない。**"""
    body = _body(10)
    body["schema_version"] = version
    result = _convert(body)
    assert result["status"] == "unsupported"
    assert "control" not in result
    assert result["freshness"]["state"] == "unknown"
    assert "古い" not in result["freshness"]["text"].replace("古さ", "")


@pytest.mark.parametrize("version", ["10", None, 0, 10.5])
def test_a_version_that_is_not_a_known_integer_is_unsupported(version):
    body = _body(10)
    trace = {**_latest(body)["trace"], "schema_version": version}
    result = _call("controlFromLatest", {"trace": trace}, 3.0)
    assert result["status"] == "unsupported"


def test_no_trace_is_unconnected():
    assert _convert(None) == {"status": "none"}
    assert _call("controlFromLatest", None, 3.0) == {"status": "none"}


@pytest.mark.parametrize("breakage", ["no_state", "no_zone", "no_demand", "nan_demand"])
def test_a_broken_body_is_unreadable_not_normal(breakage):
    body = _body(10)
    if breakage == "no_state":
        del body["state"]
    elif breakage == "no_zone":
        del body["zones"]["rear"]
    elif breakage == "no_demand":
        del body["zones"]["top"]["demand"]
    else:
        body["zones"]["front"]["demand"]["effective"] = None
    result = _convert(body)
    assert result["status"] == "unreadable"
    assert "control" not in result


def test_unknown_vocabulary_is_not_shown_as_normal():
    body = _body(10)
    body["state"]["safety_state"] = "something_new"
    chip = _chip(_convert(body), "安全状態")
    assert chip["tone"] == "warn"
    assert chip["v"] != "正常"


# ---------------------------------------------------------------- Safety override / Fallback / OOD


def _override() -> dict[str, Any]:
    """背面のファンが止まり、Critical Safety が背面を最大に固定した tick。"""
    body = _body(10)
    body["state"]["safety_state"] = "degraded"
    body["zones"]["rear"]["demand"].update(
        effective=1.0,
        bound_by="forced_max",
        forced_max=True,
        reasons=[{"code": "tach_stall", "detail": "回転を検出できない"}],
    )
    body["zones"]["front"]["demand"].update(
        effective=0.6,
        bound_by="safety_floor",
        safety_floor=0.6,
        reasons=[{"code": "rear_stall_floor", "detail": "背面停止中の下限"}],
    )
    body["faults"] = [{"code": "tach_stall", "zone": "rear", "detail": ""}]
    return body


def test_safety_override_is_distinct_from_normal():
    result = _convert(_override())
    assert _chip(result, "安全制御の介入") == {
        "k": "安全制御の介入",
        "v": "最大に固定（背面）",
        "tone": "bad",
    }
    assert _chip(result, "安全状態")["tone"] == "warn"
    assert _chip(result, "故障")["tone"] == "bad"
    rear = result["control"]["zones"]["rear"]
    assert rear["forced"] is True
    assert rear["fault"] is True
    assert rear["bound_tone"] == "bad"
    assert result["control"]["zones"]["front"]["bound_by"] == "安全制御の下限"
    assert _steps(result, "rear")["安全制御"]["value"] == "強制最大"
    alert = result["control"]["alert"]
    assert alert is not None
    assert "背面" in alert["text"]


def test_a_normal_tick_has_no_override_and_no_alert():
    result = _convert(_body(10))
    assert _chip(result, "安全制御の介入") == {"k": "安全制御の介入", "v": "なし"}
    assert _chip(result, "安全状態")["tone"] == "ok"
    assert result["control"]["alert"] is None


def test_fallback_with_a_reason_is_distinct():
    body = _body(10)
    body["state"]["fallback_reason"] = {"code": "model_ood", "detail": "想定外の状態"}
    result = _convert(body)
    assert _chip(result, "制御方式") == {"k": "制御方式", "v": "基本制御（予備）", "tone": "warn"}
    assert _chip(result, "切り替えた理由") == {
        "k": "切り替えた理由",
        "v": "想定外の状態",
        "tone": "warn",
    }


def test_ood_is_distinct():
    body = _body(10)
    body["model_gate"] = {
        "schema_version": 2,
        "model_version": "1.0.0",
        "inference_id": "a" * 64,
        "attested": True,
        "confidence": 0.41,
        "ood": True,
        "confidence_level": "low",
        "authority_stage": "limited",
        "learned_selected": False,
    }
    result = _convert(body)
    assert _chip(result, "想定外の状態") == {"k": "想定外の状態", "v": "あり", "tone": "bad"}
    assert _chip(result, "予測の信頼度") == {"k": "予測の信頼度", "v": "41%", "tone": "warn"}
    assert _steps(result, "top")["信頼度チェック"]["tone"] == "bad"


def test_unattested_numbers_are_not_shown():
    """裏づけの無い（attested でない）判定の数値を出さない（ModelGateDecision と同じ規律）。"""
    body = _body(10)
    body["model_gate"] = {
        "schema_version": 2,
        "model_version": "1.0.0",
        "inference_id": "a" * 64,
        "attested": False,
        "confidence": None,
        "ood": None,
        "confidence_level": "low",
        "authority_stage": "limited",
        "learned_selected": False,
    }
    result = _convert(body)
    assert "裏づけなし" in _chip(result, "予測の信頼度")["v"]
    assert _chip(result, "想定外の状態").get("tone") != "ok"


def test_estimates_without_values_stay_null():
    """推定値が無い tick は 0 にしない（画面は「この tick は推定なし」と出す）。"""
    zone = _convert(_body(10))["control"]["zones"]["front"]
    assert zone["airflow_index"] is None
    assert zone["estimated_flow"] is None


# ---------------------------------------------------------------- 古さ（0071 §2.6）


def _freshness(age_ms: object, version: int, multiplier: object = 3.0) -> dict[str, str]:
    result: dict[str, str] = _call("traceFreshness", age_ms, version, _body(version), multiplier)
    return result


def test_freshness_uses_the_trace_period_times_the_config():
    period = _body(10)["runtime"]["tick_period_ms"]
    assert _freshness(period * 3, 10)["state"] == "fresh"
    assert _freshness(period * 3 + 1, 10)["state"] == "stale"
    assert _freshness(period * 3 + 1, 10, 5.0)["state"] == "fresh"


@pytest.mark.parametrize("version", [1, 7])
def test_versions_without_a_period_are_not_judged(version):
    """v1〜v7 は周期を持たない。経過時間だけを出し、「古い／新しい」を言わない。"""
    result = _freshness(10_000_000, version)
    assert result["state"] == "unknown"
    assert "10000.0 秒前" in result["text"]
    assert "（古い" not in result["text"]


def test_a_negative_age_is_not_fresh():
    """壁時計が戻った直後の負の `age_ms` を「新しい」としない。"""
    assert _freshness(-5, 10)["state"] == "unknown"


def test_without_the_config_freshness_is_not_judged():
    assert _freshness(100, 10, None)["state"] == "unknown"


def test_the_period_must_be_in_the_trace():
    body = _body(10)
    del body["runtime"]
    assert _call("traceFreshness", 100, 10, body, 3.0)["state"] == "unknown"


def test_old_ok_tones_are_removed():
    """古い（または判定できない）記録の「正常」を緑で出さない。"""
    control = _convert(_body(10))["control"]
    assert any(chip.get("tone") == "ok" for chip in control["chips"])
    stripped = _call("withoutOkTones", control)
    assert not any(chip.get("tone") == "ok" for chip in stripped["chips"])
    assert [chip["v"] for chip in stripped["chips"]] == [chip["v"] for chip in control["chips"]]


def test_a_stale_trace_is_converted_with_freshness():
    period = _body(10)["runtime"]["tick_period_ms"]
    result = _convert(_body(10), age_ms=period * 10)
    assert result["freshness"]["state"] == "stale"


# ---------------------------------------------------------------- 画面への配線


def test_the_page_loads_the_converter_before_the_script():
    page = PAGE.read_text(encoding="utf-8")
    assert page.index('src="airflow-trace.js"') < page.index('src="airflow.js"')


def test_the_version_is_interpreted_only_in_the_converter():
    """版の分岐は airflow-trace.js の1か所だけ（0071 §2.3）。airflow.js は結果を描くだけ。"""
    script = SCRIPT.read_text(encoding="utf-8")
    assert "ColdaisleAirflowTrace.controlFromLatest(" in script
    branchings = ("schema_version >", "schema_version <", "schema_version ==", "tick_period_ms")
    for branching in branchings:
        assert branching not in script, branching


def test_the_converter_has_no_write_path():
    text = TRACE.read_text(encoding="utf-8")
    # コメントで API の名前を挙げるのはよい。文字列として持って叩くことはしない
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", '"/api/', "document."):
        assert forbidden not in text, forbidden


def test_the_stale_multiplier_comes_from_the_config():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "trace.stale_after_tick_periods" in script
    assert "page.traceStaleFactor" in script


def test_stale_or_unjudged_traces_lose_the_ok_colour_on_the_page():
    script = SCRIPT.read_text(encoding="utf-8")
    apply = script[script.index("function applyControlTrace(") :]
    apply = apply[: apply.index("\n}\n")]
    assert 'converted.freshness.state === "fresh"' in apply
    assert "withoutOkTones(" in apply


def test_a_failed_trace_request_clears_the_old_state():
    script = SCRIPT.read_text(encoding="utf-8")
    refresh = script[script.index("async function refresh()") :]
    refresh = refresh[: refresh.index("\n}\n")]
    assert 'fetchJson("/api/v1/control/latest")' in refresh
    failed = refresh[refresh.index('control.status === "fulfilled"') :]
    assert "page.control = null;" in failed
    assert "/api/v1/control/latest）を取得できません" in failed


# ---------------------------------------------------------------- API の契約（画面が頼る形）


def _app(tmp_path: Path, rules: Any, traces: list[dict[str, Any]]) -> TestClient:
    path = tmp_path / "trace.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        for body in traces:
            store.record_control_trace(
                ts_ms=body["ts_ms"],
                tick_id=body["tick_id"],
                schema_version=body["schema_version"],
                trace_json=json.dumps(body),
            )
    app = create_app(
        Config(
            db=path,
            quality_rules=QUALITY_RULES_PATH,
            metrics=CONFIG_DIR / "metrics.yaml",
            airflow_ui=AIRFLOW_UI_PATH,
        ),
        clock=SimulatedClock(NOW_MS),
    )
    return TestClient(app)


def test_the_page_reads_what_control_latest_returns(tmp_path, rules):
    """保存した trace → `/control/latest` → 画面の変換。**API は body を直さない**（0071 §2.3）。"""
    body = _body(10)
    with _app(tmp_path, rules, [body]) as client:
        response = client.get("/api/v1/control/latest").json()
    trace = response["trace"]
    assert trace["body"] == body
    assert trace["schema_version"] == 10
    assert trace["age_ms"] == NOW_MS - body["ts_ms"]
    assert isinstance(trace["seq"], int)
    result = _call("controlFromLatest", response, 3.0)
    assert result["status"] == "ok"
    # fixture の ts から NOW までは 10 秒。周期 1 秒 × 3 を超えるので古い
    assert result["freshness"]["state"] == "stale"


def test_an_empty_store_is_unconnected_through_the_api(tmp_path, rules):
    with _app(tmp_path, rules, []) as client:
        response = client.get("/api/v1/control/latest")
    assert response.status_code == 200
    assert response.json() == {"trace": None}
    assert _call("controlFromLatest", response.json(), 3.0) == {"status": "none"}


def test_an_unknown_version_passes_through_the_api_unchanged(tmp_path, rules):
    body = copy.deepcopy(_body(10))
    body["schema_version"] = 99
    with _app(tmp_path, rules, [body]) as client:
        response = client.get("/api/v1/control/latest").json()
    assert response["trace"]["schema_version"] == 99
    assert _call("controlFromLatest", response, 3.0)["status"] == "unsupported"


def test_the_config_returns_the_stale_multiplier(tmp_path, rules):
    shipped = yaml.safe_load(AIRFLOW_UI_PATH.read_text(encoding="utf-8"))["control_trace"]
    with _app(tmp_path, rules, []) as client:
        body = client.get("/api/v1/airflow/config").json()
    assert body["control_trace"] == shipped
    assert shipped["provisional"] is True, "0071 §5 #1 の値は仮（実運用で見直す）"


def _settings(control_trace: object) -> dict[str, object]:
    return {
        "version": 1,
        "air_temperature": {"thresholds_c": [27.0, 28.0, 29.0, 30.0], "provisional": True},
        "control_trace": control_trace,
    }


@pytest.mark.parametrize("factor", [1.0, 0.5, 0, -3, float("nan"), float("inf")])
def test_a_multiplier_that_cannot_work_is_rejected(factor):
    """1 以下だと周期どおりに記録していても「古い」になる。NaN・無限大も起動時に落とす。"""
    with pytest.raises(ValidationError):
        AirflowUiSettings.model_validate(
            _settings({"stale_after_tick_periods": factor, "provisional": True})
        )


def test_the_multiplier_is_required():
    """コードに既定値を持たない（AGENTS.md ルール 9）。"""
    with pytest.raises(ValidationError):
        AirflowUiSettings.model_validate(
            {
                "version": 1,
                "air_temperature": {"thresholds_c": [27.0, 28.0, 29.0, 30.0], "provisional": True},
            }
        )
