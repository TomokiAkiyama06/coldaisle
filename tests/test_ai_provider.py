"""LLM Provider（#21）。

要点は2つ。**環境変数だけでバックエンドが切り替わる**ことと、
**LLM が不達でも例外にしない**こと（FR-507）。

外部へは出ない。`httpx` の輸送を差し替えて確かめる。
"""

import json
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
from coldaisle.ai.provider import API_KEY_ENV, BASE_URL_ENV, MODEL_ENV
from conftest import CONFIG_DIR

AI_PATH = CONFIG_DIR / "ai.yaml"


@pytest.fixture
def settings() -> AiSettings:
    return AiSettings.from_yaml(AI_PATH)


def reply(content: str, *, tool_calls=None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"model": "qwen3.8:27b", "choices": [{"message": message}]}


def provider(settings, handler, **kwargs) -> OpenAiCompatibleProvider:
    return OpenAiCompatibleProvider(
        base_url="http://llm.invalid/v1",
        model="qwen3.8:27b",
        settings=settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
        **kwargs,
    )


# ---------------------------------------------------------------- 応答


def test_chat_returns_the_text(settings):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply("室温は26.4℃です。"))

    result = provider(settings, handler).chat([ChatMessage(role="user", content="室温は?")])
    assert result.available is True
    assert result.text == "室温は26.4℃です。"
    assert seen[0]["model"] == "qwen3.8:27b"
    assert seen[0]["stream"] is False


def test_thinking_is_separated_from_the_answer(settings):
    """`<think>` は**利用者へ見せる本文から外す**。

    Evidence 形式（FR-508）で答えるべき場面で、思考の途中経過が
    根拠のように読まれてしまう。
    """
    body = reply("<think>まず室温を見る。次に排気。</think>室温は26.4℃です。")
    result = provider(settings, lambda _: httpx.Response(200, json=body)).chat(
        [ChatMessage(role="user", content="室温は?")]
    )
    assert result.text == "室温は26.4℃です。"
    assert "まず室温を見る" in result.thinking


def test_tool_calls_are_passed_through(settings):
    """ツール呼び出しは #22 が使う。ここでは素通しする。"""
    calls = [{"id": "1", "function": {"name": "get_latest", "arguments": "{}"}}]
    body = reply("", tool_calls=calls)
    result = provider(settings, lambda _: httpx.Response(200, json=body)).chat(
        [ChatMessage(role="user", content="現在値は?")]
    )
    assert result.tool_calls[0]["function"]["name"] == "get_latest"


# ---------------------------------------------------------------- 思考モード


def test_thinking_flag_is_sent(settings):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply("はい"))

    target = provider(settings, handler)
    target.chat([ChatMessage(role="user", content="x")], thinking=True)
    target.chat([ChatMessage(role="user", content="x")], thinking=False)

    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": True}
    assert seen[1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_thinking_flag_can_be_suppressed(settings):
    """対応しないバックエンドでは 400 になりうるので、外せるようにする。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply("はい"))

    quiet = settings.model_copy(update={"send_thinking_flag": False})
    provider(quiet, handler).chat([ChatMessage(role="user", content="x")])
    assert "chat_template_kwargs" not in seen[0]


def test_default_thinking_comes_from_the_config(settings):
    assert settings.thinking is False, "日常のQ&Aは非思考で低レイテンシ"


# ---------------------------------------------------------------- 不達（FR-507）


def test_connection_failure_is_a_result_not_an_exception(settings):
    """**例外を上へ投げない。** 呼び出し側が try を書き忘れても壊れない。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("接続できない")

    result = provider(settings, handler).chat([ChatMessage(role="user", content="x")])
    assert result.available is False
    assert "ConnectError" in result.reason
    assert result.text == ""


def test_server_error_is_retried(settings):
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503)

    result = provider(settings, handler).chat([ChatMessage(role="user", content="x")])
    assert len(attempts) == settings.max_attempts
    assert result.available is False


def test_bad_request_is_not_retried(settings):
    """要求そのものが悪いなら粘っても直らない。**落ちている相手に粘らない。**"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, text="unknown field chat_template_kwargs")

    result = provider(settings, handler).chat([ChatMessage(role="user", content="x")])
    assert len(attempts) == 1
    assert result.available is False
    assert "400" in result.reason


def test_recovery_after_one_failure(settings):
    responses = [httpx.Response(503), httpx.Response(200, json=reply("はい"))]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    assert provider(settings, handler).chat([ChatMessage(role="user", content="x")]).available


def test_probe_reports_availability(settings):
    ok = provider(settings, lambda _: httpx.Response(200, json=reply("pong"))).probe()
    assert ok.available is True

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("止まっている")

    assert provider(settings, broken).probe().available is False


# ---------------------------------------------------------------- 切り替え


def test_backend_switches_by_environment(monkeypatch, settings):
    """受入基準: 環境変数の変更のみでバックエンドが切り替わる。"""
    monkeypatch.setenv(BASE_URL_ENV, "http://127.0.0.1:11434/v1")
    monkeypatch.setenv(MODEL_ENV, "qwen3:8b")
    ollama = OpenAiCompatibleProvider.from_env(settings)
    assert ollama is not None
    assert ollama.base_url == "http://127.0.0.1:11434/v1"

    monkeypatch.setenv(BASE_URL_ENV, "http://127.0.0.1:8001/v1/")
    monkeypatch.setenv(MODEL_ENV, "Qwen/Qwen3.8-27B")
    vllm = OpenAiCompatibleProvider.from_env(settings)
    assert vllm is not None
    assert vllm.base_url == "http://127.0.0.1:8001/v1", "末尾のスラッシュを揃える"
    assert vllm.model == "Qwen/Qwen3.8-27B"


def test_api_key_is_sent_when_configured(monkeypatch, settings):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json=reply("はい"))

    # pragma: allowlist secret - テストのダミー値
    provider(settings, handler, api_key="dummy-key").chat(  # pragma: allowlist secret
        [ChatMessage(role="user", content="x")]
    )
    assert seen[0] == "Bearer dummy-key"


def test_missing_configuration_yields_an_unavailable_provider(monkeypatch, settings):
    """**`None` を返さない。** 呼び出し側に分岐を書かせると書き忘れが落ちる経路になる。"""
    for name in (BASE_URL_ENV, MODEL_ENV, API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    fallback = provider_from_env(settings)
    assert isinstance(fallback, UnavailableProvider)
    result = fallback.chat([ChatMessage(role="user", content="x")])
    assert result.available is False
    assert "OPENAI_BASE_URL" in result.reason


def test_config_has_no_connection_details():
    """接続先は環境変数から。設定ファイルに書くと切り替えが2箇所になる。"""
    text = Path(AI_PATH).read_text(encoding="utf-8")
    for token in ("11434", "8001", "http://127.0.0.1:11434/v1\n"):
        assert f"\n{token}" not in text
    assert "OPENAI_BASE_URL" in text, "どこから読むかは書いてある"
