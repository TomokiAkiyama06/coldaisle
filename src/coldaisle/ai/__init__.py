"""L3 AI: LLM Provider 抽象、ツール、プロンプト。

**LLM に書き込み・実行の権限を与えない**（AGENTS.md ルール1）。
ツールは読み取り専用のみ（#22）。

**LLM が不達でも監視は止まらない**（FR-507）。この層の失敗は例外ではなく
「利用不可」という結果として返る。
"""

from coldaisle.ai.explain import Evidence, Explainer, Explanation
from coldaisle.ai.provider import (
    AiSettings,
    ChatMessage,
    ChatResult,
    OpenAiCompatibleProvider,
    Provider,
    UnavailableProvider,
)
from coldaisle.ai.provider import from_env as provider_from_env
from coldaisle.ai.tools import DEFINITIONS, ToolRegistry, as_data

__all__ = [
    "DEFINITIONS",
    "AiSettings",
    "ChatMessage",
    "ChatResult",
    "Evidence",
    "Explainer",
    "Explanation",
    "OpenAiCompatibleProvider",
    "Provider",
    "ToolRegistry",
    "UnavailableProvider",
    "as_data",
    "provider_from_env",
]
