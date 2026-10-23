"""Claude API（Messages API）の Provider（L3）。#324 / 決定記録 0143

**ローカル LLM（#58）が導入先に入るまでの間だけの代わり。** `COLDAISLE_LLM_PROVIDER=anthropic`
と明示したときにだけ使う（0143 §2.2）。既定は今のまま OpenAI 互換。

守ること（0143 §2）:

- **送る内容を広げない。** 呼び出し側が組み立てた `ChatMessage` の列（集計値だけ）をそのまま送る。
  **ツールは送らない**（`tools` が渡されたら呼ばずに「利用不可」）。例外は Slack のメンションへの
  返答（決定記録 0168 §2.5）だけで、`allow_tools=True` で作ったときに限り Messages API の
  `tool_use` / `tool_result` を話す
- **鍵をログ・`reason`・例外の文に出さない。** 失敗の理由は状態コード・`error.type`・
  例外の型の名前だけ（#305）
- **費用と回数の上限。** 1 日の要求の回数を超えたら呼ばずに「利用不可」を返す
- **不達は例外ではなく結果**（FR-507 / 0014 §2.2）

公式 SDK は使わず httpx で直接呼ぶ（0143 §2.10。使う面が `POST /v1/messages` の 1 つだけで、
SDK の自前の再試行と待ち時間を別に止める必要もあるため）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from coldaisle import logs
from coldaisle.ai.provider import (
    AiSettings,
    AnthropicLimits,
    ChatMessage,
    ChatResult,
    Provider,
    ToolChoice,
    UnavailableProvider,
)

LOGGER = logging.getLogger("coldaisle.ai.claude")

API_KEY_ENV = "ANTHROPIC_API_KEY"  # pragma: allowlist secret - 変数名であって値ではない
WORKSPACE_ENV = "ANTHROPIC_WORKSPACE_ID"
"""任意。ワークスペースに属さない鍵で要る `anthropic-workspace-id` ヘッダの値（0143 §2.4）。

秘匿ではないが鍵と同じく**ログ・理由・repr に出さない**（扱いを揃える）。空文字は未設定と同じ。
"""
MODEL_ENV = "ANTHROPIC_MODEL"
"""モデル ID。**コードに既定を持たない**（0143 §2.2）。

推奨値は `.env.example` と `docs/llm-setup.md` に書く。
"""

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
"""Anthropic の公開の endpoint。上書きの環境変数は設けない（0143 §2.2）。"""

API_VERSION = "2023-06-01"
"""`anthropic-version` ヘッダ。Messages API の版。"""

_ERROR_TYPE = re.compile(r"^[a-z_]{1,64}$")
"""応答の `error.type` として理由に載せてよい形。

**本文の `message` は載せない**（何が入るか分からない）。
"""

_UNUSABLE_STOPS = frozenset({"refusal", "max_tokens", "model_context_window_exceeded"})
"""採用しない `stop_reason`。切れた所見・JSON を使わない（0143 §2.6）。"""

_RETRYABLE_4XX = frozenset({408, 429})
"""4xx のうち再試行するもの。他の 4xx は要求が悪いので粘らない（0014 §2.5）。"""

_TOOL_USE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
"""`tool_use` の `id` として受ける形。合わない応答は採用しない（何が入るか分からない）。"""


class DailyCallBudget:
    """1 日（ローカル時刻の日付）あたりの要求の回数の上限（0143 §2.5）。

    **プロセスの中のメモリで数える。** 再起動で 0 に戻る粗い蓋であり、課金の正本は
    Anthropic の Console の使用量と spend limit である。Server Health の要約は別スレッドから
    呼ぶので lock で守る。
    """

    def __init__(self, limit: int, *, today: Callable[[], date] = date.today) -> None:
        if limit < 1:
            raise ValueError("daily_call_limit は 1 以上でなければならない")
        self._limit = limit
        self._today = today
        self._lock = threading.Lock()
        self._day: date | None = None
        self._count = 0
        self._warned: date | None = None

    def try_acquire(self) -> bool:
        """1 回ぶんを使う。**上限に達していれば偽**（呼ばない）。"""
        with self._lock:
            day = self._today()
            if day != self._day:
                self._day = day
                self._count = 0
            if self._count >= self._limit:
                if self._warned != day:
                    # その日に 1 回だけ。毎回出すと本物の障害が埋もれる
                    self._warned = day
                    LOGGER.warning(
                        "Claude API の 1 日の上限に達した。今日はもう呼ばない（監視は継続）",
                        extra={logs.FIELDS_KEY: {"limit": self._limit, "day": day.isoformat()}},
                    )
                return False
            self._count += 1
            return True

    @property
    def used(self) -> int:
        """今日使った回数（試験と運用の確認用）。"""
        with self._lock:
            return self._count if self._day == self._today() else 0


@dataclass
class AnthropicProvider:
    """Claude の Messages API を話す。`Provider` プロトコルを満たす。"""

    model: str
    settings: AiSettings
    limits: AnthropicLimits
    api_key: str = field(repr=False)
    """**repr に出さない。** dataclass の既定の repr はログや例外に紛れうる。"""
    budget: DailyCallBudget
    workspace_id: str | None = field(default=None, repr=False)
    """あれば `anthropic-workspace-id` ヘッダで送る。repr に出さない。"""
    client: httpx.Client | None = None
    sleep: Callable[[float], None] = time.sleep
    allow_tools: bool = False
    """ツールの定義と結果を送ってよいか（決定記録 0168 §2.5）。

    **真にするのは `coldaisle-slackbot` だけ。**

    偽（既定）なら、`tools` を渡されても呼ばずに「利用不可」を返す（0143 §2.3 のまま）。
    """

    @classmethod
    def from_env(cls, settings: AiSettings, *, allow_tools: bool = False) -> Provider:
        """環境変数から組み立てる。**足りなければ `UnavailableProvider`**。

        作って毎回失敗させない（0014 §2.2）。

        理由には変数名だけを書き、値は書かない（0143 §2.4）。
        """
        api_key = (os.environ.get(API_KEY_ENV) or "").strip()
        model = (os.environ.get(MODEL_ENV) or "").strip()
        workspace_id = os.environ.get(WORKSPACE_ENV) or None
        limits = settings.anthropic
        missing = [
            name for name, value in ((API_KEY_ENV, api_key), (MODEL_ENV, model)) if not value
        ]
        if limits is None:
            missing.append("config/ai.yaml の anthropic:")
        malformed = [
            name
            for name, value in ((API_KEY_ENV, api_key), (WORKSPACE_ENV, workspace_id))
            if value and not _header_safe(value)
        ]
        if malformed:
            # 値は書かない（変数名だけ）。ヘッダに載せられない形なら送らずに使わない。
            # 貼り付けたスマートクォートなどは送信時に UnicodeEncodeError になるため、起動時に止める
            LOGGER.warning(
                "Claude API の鍵かワークスペース ID の形が不正。AI を使わない（監視は継続）",
                extra={logs.FIELDS_KEY: {"env": malformed}},
            )
            return UnavailableProvider(
                reason=f"{' / '.join(malformed)} の形が不正（ASCII の印字できる文字・空白なし）"
            )
        if missing or limits is None:
            reason = f"Claude API が設定されていない（{' / '.join(missing)}）"
            LOGGER.warning(
                "Claude API を選んだが設定が足りないので AI を使わない（監視は継続する）",
                extra={logs.FIELDS_KEY: {"missing": missing}},
            )
            return UnavailableProvider(reason=reason)
        return cls(
            model=model,
            settings=settings,
            limits=limits,
            api_key=api_key,
            budget=DailyCallBudget(limits.daily_call_limit),
            workspace_id=workspace_id,
            allow_tools=allow_tools,
        )

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
    ) -> ChatResult:
        if tools and not self.allow_tools:
            # ツールの結果を外へ流す経路をこの Provider では開かない（0143 §2.3）
            return self._unavailable("外部の Provider にはツールを渡さない（決定記録 0143 §2.3）")
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        try:
            turns = _turns(messages) if tools else _plain_turns(messages)
            anthropic_tools = [_anthropic_tool(definition) for definition in tools or ()]
        except ValueError as error:
            return self._unavailable(f"ツールの発話を Messages API の形にできない（{error}）")
        if not turns:
            return self._unavailable("送る発話が無い")
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": min(self.settings.max_tokens, self.limits.max_tokens),
            "messages": turns,
            # temperature / thinking は送らない
            # （Haiku 5.5 / Sonnet 5.5 は既定以外の sampling を拒む。0143 §2.5）
        }
        if system:
            payload["system"] = system
        if anthropic_tools:
            payload["tools"] = anthropic_tools
            if tool_choice is not None:
                payload["tool_choice"] = {"type": tool_choice}
        if self.limits.effort is not None:
            # 設定したときだけ送る。対応するモデルかをコードで判定しない（0143 §2.5）
            payload["output_config"] = {"effort": self.limits.effort}
        use_thinking = self.settings.thinking if thinking is None else thinking
        timeout = self.settings.thinking_timeout_s if use_thinking else self.settings.timeout_s
        return self._post(payload, timeout)

    def probe(self) -> ChatResult:
        """短い応答で疎通を見る。**1 回ぶんの上限を使う。**"""
        return self.chat([ChatMessage(role="user", content="ping")], thinking=False)

    # ------------------------------------------------------------------ 内部

    def _post(self, payload: dict[str, Any], timeout_s: float) -> ChatResult:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        if self.workspace_id:
            headers["anthropic-workspace-id"] = self.workspace_id
        last_reason = ""
        for attempt in range(1, self.settings.max_attempts + 1):
            if not self.budget.try_acquire():
                return ChatResult(
                    available=False,
                    reason="Claude API の 1 日の呼び出しの上限に達した",
                    model=self.model,
                )
            try:
                response = self._send(payload, headers, timeout_s)
                status = response.status_code
                if status >= 500 or status in _RETRYABLE_4XX:
                    last_reason = _http_reason(response)
                elif status >= 400:
                    # 要求そのもの（鍵・モデル ID・形）が悪い。粘っても直らない
                    return self._unavailable(_http_reason(response))
                else:
                    return self._parse(response.json())
            except httpx.HTTPError as error:
                # **例外の文を載せない。** httpx の文には URL や要求の情報が入りうる（#305）
                last_reason = type(error).__name__
            except UnicodeError as error:
                # ヘッダを符号化できない（起動時の検査をすり抜けた値）。粘っても直らない。
                # 例外の文には値の一部が入るので型の名前だけ（0143 §2.4）
                return self._unavailable(f"ヘッダを符号化できない（{type(error).__name__}）")
            except ValueError as error:
                last_reason = f"応答を JSON として読めない（{type(error).__name__}）"
            if attempt < self.settings.max_attempts:
                self.sleep(self.settings.retry_backoff_s * attempt)
        return self._unavailable(last_reason)

    def _send(
        self, payload: dict[str, Any], headers: dict[str, str], timeout_s: float
    ) -> httpx.Response:
        if self.client is not None:
            return self.client.post(MESSAGES_URL, json=payload, headers=headers)
        with httpx.Client(timeout=timeout_s) as client:
            return client.post(MESSAGES_URL, json=payload, headers=headers)

    def _parse(self, body: object) -> ChatResult:
        """応答を読む。**形が想定と違っても例外にしない**（FR-507）。"""
        if not isinstance(body, dict):
            return self._unavailable("応答の形が違う")
        stop = body.get("stop_reason")
        if isinstance(stop, str) and stop in _UNUSABLE_STOPS:
            return self._unavailable(f"stop_reason: {stop}")
        content = body.get("content")
        if not isinstance(content, list):
            return self._unavailable("応答に content が無い")
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        tool_calls: list[dict[str, Any]] = []
        if self.allow_tools:
            # ツールを送れない作り（既定）では tool_use を読まない（0143 §2.3 のまま）
            uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            for block in uses:
                call = _tool_call(block)
                if call is None:
                    return self._unavailable("応答の tool_use の形が違う")
                tool_calls.append(call)
        if not texts and not tool_calls:
            return self._unavailable("応答に text が無い")
        model = body.get("model")
        return ChatResult(
            available=True,
            text="".join(texts).strip(),
            model=model if isinstance(model, str) else self.model,
            tool_calls=tool_calls,
        )

    def _unavailable(self, reason: str) -> ChatResult:
        LOGGER.warning(
            "Claude API に到達できない・使えない（監視は継続する）",
            extra={logs.FIELDS_KEY: {"model": self.model, "reason": reason}},
        )
        return ChatResult(available=False, reason=reason, model=self.model)


def _plain_turns(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """ツールを送らないときの発話（**今までと同じ形**。user と assistant の文だけ）。"""
    return [
        {"role": m.role, "content": m.content} for m in messages if m.role in ("user", "assistant")
    ]


def _turns(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """tool loop の発話を Messages API の形にする（決定記録 0168 §2.5）。

    - assistant の `tool_calls` は `tool_use` の塊にする
    - 続く role `tool` の発話（ツールの結果）は、1 つの user の発話の `tool_result` の塊にまとめる
      （Messages API は user と assistant が交互であることを求める）
    """
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            if message.tool_call_id is None:
                raise ValueError("tool の発話に tool_call_id が無い")
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            last = turns[-1] if turns else None
            if last is not None and last["role"] == "user" and isinstance(last["content"], list):
                last["content"].append(block)
            else:
                turns.append({"role": "user", "content": [block]})
            continue
        if message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(_tool_use(call) for call in message.tool_calls)
            turns.append({"role": "assistant", "content": blocks})
            continue
        if message.role in ("user", "assistant"):
            turns.append({"role": message.role, "content": message.content})
    return turns


def _tool_use(call: dict[str, Any]) -> dict[str, Any]:
    """OpenAI の形の呼び出し → `tool_use` の塊。"""
    function = call.get("function")
    call_id = call.get("id")
    if not isinstance(function, dict) or not isinstance(call_id, str):
        raise ValueError("tool_calls の形が違う")
    name = function.get("name")
    arguments = function.get("arguments")
    if not isinstance(name, str):
        raise ValueError("tool_calls に name が無い")
    parsed: Any = json.loads(arguments) if isinstance(arguments, str) and arguments else {}
    if not isinstance(parsed, dict):
        raise ValueError("tool_calls の arguments が辞書でない")
    return {"type": "tool_use", "id": call_id, "name": name, "input": parsed}


def _anthropic_tool(definition: dict[str, Any]) -> dict[str, Any]:
    """OpenAI の function calling の定義 → Messages API の `tools` の 1 つ。"""
    function = definition.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise ValueError("ツールの定義の形が違う")
    return {
        "name": function["name"],
        "description": str(function.get("description", "")),
        "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
    }


def _tool_call(block: dict[str, Any]) -> dict[str, Any] | None:
    """`tool_use` の塊 → OpenAI の形の呼び出し（`ChatResult.tool_calls` の形を揃える）。"""
    call_id = block.get("id")
    name = block.get("name")
    arguments = block.get("input")
    if not isinstance(call_id, str) or not _TOOL_USE_ID.match(call_id):
        return None
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return None
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _header_safe(value: str) -> bool:
    """HTTP のヘッダにそのまま載せられるか（ASCII の印字できる文字だけ・空白なし）。"""
    return value.isascii() and value.isprintable() and not any(c.isspace() for c in value)


def _http_reason(response: httpx.Response) -> str:
    """HTTP の失敗の理由。**状態コードと `error.type` だけ**（本文の `message` は載せない）。"""
    reason = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return reason
    error = body.get("error") if isinstance(body, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    if isinstance(kind, str) and _ERROR_TYPE.match(kind):
        return f"{reason}: {kind}"
    return reason
