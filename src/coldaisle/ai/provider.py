"""OpenAI 互換 API の Provider（L3）。#21

Ollama と vLLM はどちらも `/v1/chat/completions` を話す。**接続先とモデル名を
環境変数で差し替えるだけ**でバックエンドが切り替わる（FR-501 / 要件 §7.2）。

**LLM が不達でも監視は止まらない**（FR-507）。この層は接続の失敗を
例外として上へ投げず、「利用不可」という結果として返す。
呼び出し側が `try` を書き忘れても、ダッシュボードとルールエンジンは動き続ける。
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle import logs

LOGGER = logging.getLogger("coldaisle.ai")

BASE_URL_ENV = "OPENAI_BASE_URL"
MODEL_ENV = "MODEL_NAME"
API_KEY_ENV = "OPENAI_API_KEY"  # pragma: allowlist secret - 変数名であって値ではない

THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
"""閉じた思考の出力。**利用者へ見せる本文からは外す。**

Evidence 形式（FR-508）で答えるべき場面で、思考の途中経過が根拠のように
読まれてしまうため。必要なときは `ChatResult.thinking` から取る。
"""

OPEN_THINK = "<think>"
"""閉じていない思考の始まり。

`max_tokens` に達すると `</think>` が出ないまま切れる。**長い診断のときだけ
内部の推論が本文に混じる**ことになるので、閉じタグが無くても切り離す。
"""


class AiSettings(BaseModel):
    """`config/ai.yaml`。接続先とモデル名は含まない（環境変数から）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_s: float = Field(gt=0)
    thinking_timeout_s: float = Field(gt=0)
    max_attempts: int = Field(ge=1)
    retry_backoff_s: float = Field(ge=0)
    temperature: float = Field(ge=0)
    max_tokens: int = Field(gt=0)
    thinking: bool
    send_thinking_flag: bool
    explain_alerts: bool = True
    """発火したアラートに Evidence 形式の説明を後追いで付けるか（#38）。"""

    @classmethod
    def from_yaml(cls, path: Path) -> AiSettings:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"AI の設定が辞書ではない: {path}")
        return cls.model_validate(loaded)


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ChatResult:
    """応答。**失敗も結果として返す**（FR-507）。

    `available` が偽なら `reason` に理由が入る。呼び出し側は
    「AI 利用不可」と表示すればよく、例外処理を書かなくても壊れない。
    """

    available: bool
    text: str = ""
    thinking: str = ""
    """`<think>` の中身。利用者へ見せる本文には含めない。"""
    reason: str = ""
    model: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """モデルが呼びたがったツール（#22 が使う）。"""


class Provider(Protocol):
    """LLM への窓口。実装は差し替えられる（試験では偽物を使う）。"""

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult: ...

    def probe(self) -> ChatResult:
        """疎通確認。**失敗しても例外にしない。**"""
        ...


@dataclass
class OpenAiCompatibleProvider:
    """Ollama / vLLM のどちらでも同じように話す。"""

    base_url: str
    model: str
    settings: AiSettings
    api_key: str | None = None
    client: httpx.Client | None = None
    sleep: Any = time.sleep

    @classmethod
    def from_env(cls, settings: AiSettings) -> OpenAiCompatibleProvider | None:
        """環境変数から組み立てる。**未設定なら作らない。**

        作って毎回失敗させると、ログが失敗で埋まって本物の障害が見えなくなる
        （通知の宛先と同じ扱い。決定記録 0013 §2.1）。
        """
        base_url = os.environ.get(BASE_URL_ENV)
        model = os.environ.get(MODEL_ENV)
        if not base_url or not model:
            return None
        return cls(
            base_url=base_url.rstrip("/"),
            model=model,
            settings=settings,
            api_key=os.environ.get(API_KEY_ENV),
        )

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        use_thinking = self.settings.thinking if thinking is None else thinking
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if self.settings.send_thinking_flag:
            # Qwen3 系 + vLLM の作法。対応しないバックエンドでは外すこと
            payload["chat_template_kwargs"] = {"enable_thinking": use_thinking}
        timeout = self.settings.thinking_timeout_s if use_thinking else self.settings.timeout_s
        return self._post(payload, timeout)

    def probe(self) -> ChatResult:
        """短い応答で疎通を見る。UI の「AI 利用不可」表示に使う。"""
        return self.chat([ChatMessage(role="user", content="ping")], thinking=False)

    # ------------------------------------------------------------------ 内部

    def _post(self, payload: dict[str, Any], timeout_s: float) -> ChatResult:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last_reason = ""
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                response = self._send(url, payload, headers, timeout_s)
                if response.status_code >= 500:
                    last_reason = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    # 要求そのものが悪い。粘っても直らない
                    return self._unavailable(f"HTTP {response.status_code}: {response.text[:200]}")
                else:
                    return self._parse(response.json())
            except (httpx.HTTPError, ValueError, KeyError) as error:
                last_reason = f"{type(error).__name__}: {error}"
            if attempt < self.settings.max_attempts:
                self.sleep(self.settings.retry_backoff_s * attempt)
        return self._unavailable(last_reason)

    def _send(
        self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout_s: float
    ) -> httpx.Response:
        if self.client is not None:
            return self.client.post(url, json=payload, headers=headers)
        with httpx.Client(timeout=timeout_s) as client:
            return client.post(url, json=payload, headers=headers)

    def _parse(self, body: dict[str, Any]) -> ChatResult:
        """応答を読む。**形が想定と違っても例外にしない**（FR-507）。

        OpenAI 互換をうたっていても、`choices` が空だったり `message` が
        辞書でなかったりする実装がありうる。そこで例外が出ると、
        `chat()` の「不達は結果として返す」という約束が破れる。
        """
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return self._unavailable(f"応答に choices が無い: {str(body)[:200]}")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return self._unavailable(f"応答の形が違う: {str(choices[0])[:200]}")
        text, thinking = split_thinking(str(message.get("content") or ""))
        tool_calls = message.get("tool_calls")
        return ChatResult(
            available=True,
            text=text,
            thinking=thinking,
            model=str(body.get("model", self.model)),
            tool_calls=list(tool_calls) if isinstance(tool_calls, list) else [],
        )

    def _unavailable(self, reason: str) -> ChatResult:
        LOGGER.warning(
            "LLM に到達できない（監視は継続する）",
            extra={logs.FIELDS_KEY: {"base_url": self.base_url, "reason": reason}},
        )
        return ChatResult(available=False, reason=reason, model=self.model)


class UnavailableProvider:
    """設定されていないときの代わり。**常に「利用不可」を返す。**

    `None` を配って呼び出し側に分岐を書かせると、書き忘れが落ちる経路になる。
    """

    reason: str = "LLM が設定されていない（OPENAI_BASE_URL / MODEL_NAME）"

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        return ChatResult(available=False, reason=self.reason)

    def probe(self) -> ChatResult:
        return ChatResult(available=False, reason=self.reason)


def split_thinking(content: str) -> tuple[str, str]:
    """本文と思考に分ける。

    **閉じていない `<think>` も思考として扱う。** `max_tokens` に達すると
    閉じタグが出ないまま切れるため、閉じたものだけを外す実装では
    長い診断のときに内部の推論がそのまま利用者へ出る。
    """
    thinking = [block.strip() for block in THINK_BLOCK.findall(content)]
    text = THINK_BLOCK.sub("", content)
    opened = text.find(OPEN_THINK)
    if opened != -1:
        thinking.append(text[opened + len(OPEN_THINK) :].strip())
        text = text[:opened]
    return text.strip(), "\n".join(part for part in thinking if part).strip()


def from_env(settings: AiSettings) -> Provider:
    """使える Provider を返す。**必ず何かを返す**（`None` を返さない）。"""
    provider = OpenAiCompatibleProvider.from_env(settings)
    return provider if provider is not None else UnavailableProvider()
