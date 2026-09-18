"""Server Health の任意 AI 要約。判定と advisory には関与しない。#66"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from coldaisle.ai.provider import ChatMessage, Provider

LOGGER = logging.getLogger("coldaisle.ai.health")

_ALLOWED_SUMMARIES = {
    "監視対象のTelemetryと情報源は正常です。": frozenset(
        {
            "監視対象のTelemetryと情報源は正常です。",
            "監視情報と各データ源は正常です。",
        }
    ),
    "一部のTelemetryまたはアラートに注意が必要です。": frozenset(
        {
            "一部のTelemetryまたはアラートに注意が必要です。",
            "監視情報の一部に注意が必要です。",
        }
    ),
    "監視に必要なTelemetryを取得できないか、重大なアラートがあります。": frozenset(
        {
            "監視に必要なTelemetryを取得できないか、重大なアラートがあります。",
            "必要な監視情報を取得できないか、重大な警告があります。",
        }
    ),
}

_SYSTEM_PROMPT = """あなたはサーバー監視パネルの確定済みテンプレートを分類します。
入力文と同じ意味の許可済み文を、次の候補から完全一致で1つだけ返してください。
監視対象のTelemetryと情報源は正常です。
監視情報と各データ源は正常です。
一部のTelemetryまたはアラートに注意が必要です。
監視情報の一部に注意が必要です。
監視に必要なTelemetryを取得できないか、重大なアラートがあります。
必要な監視情報を取得できないか、重大な警告があります。"""


class Summarizer(Protocol):
    def summarize(self, template: str) -> str | None: ...


@dataclass(frozen=True)
class AiHealthSummarizer:
    """Provider の失敗や不正出力を ``None`` に畳む要約器。"""

    provider: Provider

    def summarize(self, template: str) -> str | None:
        result = self.provider.chat(
            [
                ChatMessage(role="system", content=_SYSTEM_PROMPT),
                ChatMessage(role="user", content=template),
            ],
            thinking=False,
        )
        if not result.available:
            return None
        summary = result.text.strip()
        allowed = _ALLOWED_SUMMARIES.get(template)
        if allowed is None or summary not in allowed:
            return None
        return summary


class BackgroundHealthSummarizer:
    """LLM を daemon thread で呼び、API には待たずに cached 結果を返す。

    同時実行は1本だけに制限する。生成中・失敗後のretry待ちでは ``None`` を返すため、
    呼び出し側は決定論的テンプレートを即座に返せる。
    """

    def __init__(
        self,
        delegate: Summarizer,
        *,
        retry_s: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if retry_s <= 0:
            raise ValueError("retry_s は正でなければならない")
        self._delegate = delegate
        self._retry_s = retry_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached_template: str | None = None
        self._cached_summary: str | None = None
        self._inflight = False
        self._retry_after = 0.0

    def summarize(self, template: str) -> str | None:
        with self._lock:
            if template == self._cached_template:
                return self._cached_summary
            if self._inflight or self._monotonic() < self._retry_after:
                return None
            self._inflight = True
        threading.Thread(
            target=self._generate,
            args=(template,),
            name="coldaisle-health-summary",
            daemon=True,
        ).start()
        return None

    def _generate(self, template: str) -> None:
        try:
            summary = self._delegate.summarize(template)
        except Exception:
            LOGGER.warning("Server Health のAI要約に失敗した", exc_info=True)
            summary = None
        with self._lock:
            if summary is not None:
                self._cached_template = template
                self._cached_summary = summary
            else:
                self._retry_after = self._monotonic() + self._retry_s
            self._inflight = False
