"""Claude API の Provider（#324 / 決定記録 0143）。

要点:

- **既定は今のまま**（`COLDAISLE_LLM_PROVIDER` が無ければ Claude を使わない）
- **送る内容を広げない**（ツールを送らない・渡された発話だけ）
- **鍵をログ・理由に出さない**
- **上限に達したら呼ばない**
- **不達は例外ではなく結果**（FR-507）

外部へは出ない。`httpx` の輸送を差し替えて確かめる（実 API を呼ばない）。
"""

import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from coldaisle.ai import (
    AiSettings,
    ChatMessage,
    OpenAiCompatibleProvider,
    UnavailableProvider,
    provider_from_env,
)
from coldaisle.ai.claude_provider import (
    API_KEY_ENV,
    MESSAGES_URL,
    MODEL_ENV,
    WORKSPACE_ENV,
    AnthropicProvider,
    DailyCallBudget,
)
from coldaisle.ai.provider import BASE_URL_ENV, PROVIDER_ENV
from coldaisle.ai.provider import MODEL_ENV as OPENAI_MODEL_ENV
from conftest import CONFIG_DIR

AI_PATH = CONFIG_DIR / "ai.yaml"
ROOT = Path(AI_PATH).parents[1]

FAKE_KEY = "sk-ant-test-0000-fake"  # pragma: allowlist secret - テストのダミー値
MODEL = "claude-haiku-5-5"


@pytest.fixture
def settings() -> AiSettings:
    return AiSettings.from_yaml(AI_PATH)


def reply(text: str, *, stop: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [
            {"type": "thinking", "thinking": "", "signature": "x"},
            {"type": "text", "text": text},
        ],
        "stop_reason": stop,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def provider(settings, handler, *, limit: int = 100, today=None) -> AnthropicProvider:
    assert settings.anthropic is not None
    return AnthropicProvider(
        model=MODEL,
        settings=settings,
        limits=settings.anthropic,
        api_key=FAKE_KEY,
        budget=DailyCallBudget(limit, today=today or (lambda: date(2026, 10, 15))),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )


def ask(p: AnthropicProvider, text: str = "所見を書いて"):
    return p.chat(
        [
            ChatMessage(role="system", content="日本語で答える"),
            ChatMessage(role="user", content=text),
        ],
        thinking=False,
    )


# ---------------------------------------------------------------- 要求と応答


def test_request_shape(settings):
    """Messages API の形で送る。system は最上位へ、鍵はヘッダだけ。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply("大きな変化はありません"))

    result = ask(provider(settings, handler))
    assert result.available is True
    assert result.text == "大きな変化はありません"
    request = seen[0]
    assert str(request.url) == MESSAGES_URL
    assert request.headers["x-api-key"] == FAKE_KEY
    assert request.headers["anthropic-version"]
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["system"] == "日本語で答える"
    assert body["messages"] == [{"role": "user", "content": "所見を書いて"}]
    assert body["max_tokens"] <= settings.anthropic.max_tokens
    # 既定の設定では effort を送らない（対応するモデルかをコードで判定しない。0143 §2.5）
    assert settings.anthropic.effort is None
    assert "output_config" not in body
    # 既定以外の sampling を拒むモデルがある（0143 §2.5）
    assert "temperature" not in body
    assert "thinking" not in body
    assert "tools" not in body


def test_only_text_blocks_are_shown(settings):
    """thinking の塊は本文に入れない。"""
    result = ask(provider(settings, lambda _: httpx.Response(200, json=reply("結論です。"))))
    assert result.text == "結論です。"
    assert result.thinking == ""


def test_tools_are_never_sent(settings):
    """**ツールの結果を外へ流す経路を開かない**（0143 §2.3）。呼ばずに利用不可。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=reply("x"))

    result = provider(settings, handler).chat(
        [ChatMessage(role="user", content="x")],
        tools=[{"type": "function", "function": {"name": "get_latest"}}],
    )
    assert result.available is False
    assert calls == []


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "model_context_window_exceeded"])
def test_unusable_stop_reason_is_unavailable(settings, stop):
    """拒否・途中で切れた応答は採用しない（切れた所見や JSON を使わない）。"""
    result = ask(provider(settings, lambda _: httpx.Response(200, json=reply("途中", stop=stop))))
    assert result.available is False
    assert stop in result.reason


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"content": "文字列"},
        {"content": []},
        {"content": [{"type": "thinking", "thinking": ""}]},
        {"error": {"type": "overloaded_error"}},
    ],
)
def test_malformed_response_is_unavailable_not_an_exception(settings, body):
    result = ask(provider(settings, lambda _: httpx.Response(200, json=body)))
    assert result.available is False
    assert result.reason


# ---------------------------------------------------------------- 失敗（FR-507）


def test_connection_failure_is_a_result(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = ask(provider(settings, handler))
    assert result.available is False
    assert result.reason == "ConnectError"


@pytest.mark.parametrize("status", [429, 500, 529])
def test_rate_limit_and_server_errors_are_retried(settings, status):
    statuses = [status, 200]

    def handler(_: httpx.Request) -> httpx.Response:
        code = statuses.pop(0)
        if code == 200:
            return httpx.Response(200, json=reply("回復"))
        return httpx.Response(code, json={"type": "error", "error": {"type": "overloaded_error"}})

    result = ask(provider(settings, handler))
    assert result.available is True
    assert result.text == "回復"


def test_bad_request_is_not_retried(settings):
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "m"}}
        )

    result = ask(provider(settings, handler))
    assert result.available is False
    assert result.reason == "HTTP 400: invalid_request_error"
    assert len(calls) == 1


# ---------------------------------------------------------------- 鍵（0143 §2.4）


def test_key_never_appears_in_reason_or_logs(settings, caplog):
    """**鍵・応答の本文の message・例外の文を理由とログに出さない**（#305 と同じ）。"""

    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": f"bad key {FAKE_KEY}"},
            },
        )

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed with {FAKE_KEY} at {request.url}", request=request)

    caplog.set_level(logging.DEBUG)
    reasons = [ask(provider(settings, h)).reason for h in (unauthorized, broken)]
    assert reasons == ["HTTP 401: authentication_error", "ConnectError"]
    p = provider(settings, unauthorized)
    texts = [*reasons, caplog.text, repr(p), str(p)]
    for record in caplog.records:
        texts.append(json.dumps(getattr(record, "fields", {}), ensure_ascii=False, default=str))
    for text in texts:
        assert FAKE_KEY not in text
        assert "bad key" not in text


def test_odd_error_type_is_not_echoed(settings):
    """`error.type` も形を絞る。任意の文を理由へ通さない。"""
    body = {"type": "error", "error": {"type": "Bad Key: sk-ant-xyz"}}
    result = ask(provider(settings, lambda _: httpx.Response(403, json=body)))
    assert result.reason == "HTTP 403"


# ---------------------------------------------------------------- 上限（0143 §2.5）


def test_daily_limit_stops_calls(settings, caplog):
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=reply("はい"))

    p = provider(settings, handler, limit=2)
    caplog.set_level(logging.WARNING)
    results = [ask(p) for _ in range(4)]
    assert [r.available for r in results] == [True, True, False, False]
    assert len(calls) == 2, "上限に達したら呼ばない"
    assert "上限" in results[2].reason
    warnings = [r for r in caplog.records if "上限" in r.getMessage()]
    assert len(warnings) == 1, "上限の警告はその日に1回だけ"


def test_retries_count_against_the_limit(settings):
    """再試行も 1 回と数える（費用は要求ごとにかかる）。"""
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    p = provider(settings, handler, limit=1)
    result = ask(p)
    assert result.available is False
    assert len(calls) == 1
    assert p.budget.used == 1


def test_limit_resets_on_the_next_day(settings):
    day = [date(2026, 10, 15)]
    p = provider(
        settings, lambda _: httpx.Response(200, json=reply("はい")), limit=1, today=lambda: day[0]
    )
    assert ask(p).available is True
    assert ask(p).available is False
    day[0] = date(2026, 10, 16)
    assert ask(p).available is True


def test_max_tokens_is_capped_by_the_limit(settings):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply("x"))

    small = settings.model_copy(
        update={"anthropic": settings.anthropic.model_copy(update={"max_tokens": 64})}
    )
    ask(provider(small, handler))
    assert seen[0]["max_tokens"] == 64


# ---------------------------------------------------------------- 選び方（0143 §2.2）


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        PROVIDER_ENV,
        API_KEY_ENV,
        MODEL_ENV,
        WORKSPACE_ENV,
        BASE_URL_ENV,
        OPENAI_MODEL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_default_is_unchanged_even_with_a_key(clean_env, settings):
    """**鍵を置いただけでは外へ送らない。** 明示の opt-in が要る。"""
    clean_env.setenv(API_KEY_ENV, FAKE_KEY)
    clean_env.setenv(MODEL_ENV, MODEL)
    assert isinstance(provider_from_env(settings), UnavailableProvider)
    clean_env.setenv(BASE_URL_ENV, "http://127.0.0.1:8001/v1")
    clean_env.setenv(OPENAI_MODEL_ENV, "Qwen/Qwen3.8-27B")
    assert isinstance(provider_from_env(settings), OpenAiCompatibleProvider)


def test_anthropic_is_selected_explicitly(clean_env, settings):
    clean_env.setenv(PROVIDER_ENV, "anthropic")
    clean_env.setenv(API_KEY_ENV, FAKE_KEY)
    clean_env.setenv(MODEL_ENV, MODEL)
    chosen = provider_from_env(settings)
    assert isinstance(chosen, AnthropicProvider)
    assert chosen.model == MODEL
    assert chosen.budget is not None


@pytest.mark.parametrize("drop", [API_KEY_ENV, MODEL_ENV])
def test_missing_key_or_model_is_unavailable(clean_env, settings, drop):
    """**モデル ID の既定をコードに持たない。** 無ければ使わない。理由に値を書かない。"""
    clean_env.setenv(PROVIDER_ENV, "anthropic")
    clean_env.setenv(API_KEY_ENV, FAKE_KEY)
    clean_env.setenv(MODEL_ENV, MODEL)
    clean_env.delenv(drop)
    chosen = provider_from_env(settings)
    assert isinstance(chosen, UnavailableProvider)
    assert drop in chosen.reason
    assert FAKE_KEY not in chosen.reason
    assert chosen.chat([ChatMessage(role="user", content="x")]).available is False


def test_missing_limits_is_unavailable(clean_env, settings):
    clean_env.setenv(PROVIDER_ENV, "anthropic")
    clean_env.setenv(API_KEY_ENV, FAKE_KEY)
    clean_env.setenv(MODEL_ENV, MODEL)
    no_limits = settings.model_copy(update={"anthropic": None})
    assert isinstance(provider_from_env(no_limits), UnavailableProvider)


def test_unknown_provider_is_unavailable_not_an_exception(clean_env, settings):
    clean_env.setenv(PROVIDER_ENV, "gpt")
    chosen = provider_from_env(settings)
    assert isinstance(chosen, UnavailableProvider)
    assert PROVIDER_ENV in chosen.reason


# ---------------------------------------------------------------- 文書と設定


def test_config_has_limits_but_no_model_or_key():
    text = Path(AI_PATH).read_text(encoding="utf-8")
    settings = AiSettings.from_yaml(AI_PATH)
    assert settings.anthropic is not None
    assert "claude-" not in text, "モデル ID は環境変数から（0143 §2.2）"
    assert "sk-ant" not in text


def test_env_example_and_docs_name_the_variables():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (PROVIDER_ENV, API_KEY_ENV, MODEL_ENV, WORKSPACE_ENV):
        assert f"{name}=" in example
    setup = (ROOT / "docs" / "llm-setup.md").read_text(encoding="utf-8")
    assert PROVIDER_ENV in setup
    assert "0143" in setup


def test_escalation_does_not_import_a_provider():
    """0019 の案件資料は送らない。`coldaisle-escalate` は Provider を使わない（0143 §2.8）。"""
    source = (ROOT / "src" / "coldaisle" / "escalate.py").read_text(encoding="utf-8")
    assert "coldaisle.ai" not in source
    assert "claude_provider" not in source


# ---------------------------------------------------------------- ワークスペース（0143 §2.4）

WORKSPACE = "wrkspc_0000test"


def _headers_from_env(clean_env, settings) -> httpx.Headers:
    clean_env.setenv(PROVIDER_ENV, "anthropic")
    clean_env.setenv(API_KEY_ENV, FAKE_KEY)
    clean_env.setenv(MODEL_ENV, MODEL)
    chosen = provider_from_env(settings)
    assert isinstance(chosen, AnthropicProvider)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply("はい"))

    chosen.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert ask(chosen).available is True
    assert WORKSPACE not in repr(chosen)
    return seen[0].headers


def test_workspace_header_is_sent_when_set(clean_env, settings):
    """ワークスペースに属さない鍵では `anthropic-workspace-id` が要る。"""
    clean_env.setenv(WORKSPACE_ENV, WORKSPACE)
    assert _headers_from_env(clean_env, settings)["anthropic-workspace-id"] == WORKSPACE


@pytest.mark.parametrize("value", [None, ""])
def test_workspace_header_is_absent_when_unset_or_empty(clean_env, settings, value):
    """空文字は未設定と同じ。"""
    if value is not None:
        clean_env.setenv(WORKSPACE_ENV, value)
    assert "anthropic-workspace-id" not in _headers_from_env(clean_env, settings)


@pytest.mark.parametrize("value", [" wrkspc_x", "wrkspc x", "wrkspc_x\n", "\t"])
def test_malformed_workspace_is_unavailable_without_echo(clean_env, settings, caplog, value):
    """形が不正なら使わない。**値を理由・ログに出さない。**"""
    clean_env.setenv(PROVIDER_ENV, "anthropic")
    clean_env.setenv(API_KEY_ENV, FAKE_KEY)
    clean_env.setenv(MODEL_ENV, MODEL)
    clean_env.setenv(WORKSPACE_ENV, value)
    caplog.set_level(logging.DEBUG)
    chosen = provider_from_env(settings)
    assert isinstance(chosen, UnavailableProvider)
    assert WORKSPACE_ENV in chosen.reason
    assert "wrkspc" not in chosen.reason
    assert "wrkspc" not in caplog.text


# ---------------------------------------------------------------- effort（0143 §2.5）


@pytest.mark.parametrize("effort", [None, "low", "high"])
def test_effort_is_sent_only_when_configured(settings, effort):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply("x"))

    configured = settings.model_copy(
        update={"anthropic": settings.anthropic.model_copy(update={"effort": effort})}
    )
    ask(provider(configured, handler))
    if effort is None:
        assert "output_config" not in seen[0]
    else:
        assert seen[0]["output_config"] == {"effort": effort}


def test_effort_may_be_omitted_from_the_config():
    from coldaisle.ai import AnthropicLimits

    assert AnthropicLimits(max_tokens=10, daily_call_limit=1).effort is None
