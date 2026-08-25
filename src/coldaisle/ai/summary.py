"""日次レポートの所見を書かせる（#25 / FR-506）。

**数値はコードが出す。モデルは文だけを書く**（#38 と同じ）。

レポートの表はロールアップから機械的に組み立てたものであり、そこに載っていない
数値が所見に現れたら、それはモデルが作った数値である。プロンプトで禁じるだけでは
足りないので、**表に無い数値を含む所見は捨てる**（表だけが届く）。
"""

from __future__ import annotations

import logging
import re

from coldaisle import logs
from coldaisle.ai.explain import FORBIDDEN
from coldaisle.ai.provider import ChatMessage, Provider

LOGGER = logging.getLogger("coldaisle.ai.summary")

NUMBER = re.compile(r"\d+(?:\.\d+)?")
"""数値らしきもの。**所見と表で突き合わせる**ために使う。"""

MAX_CHARS = 400

SYSTEM_PROMPT = """あなたはGPUサーバーの熱管理を監視するアシスタントです。
前日の集計結果を渡します。運用者が朝に読む「所見」を日本語で書いてください。

制約:
- **数値を書かない。** 数値は表に載っています。文章では傾向だけを述べる
- **確度・確信度・パーセントでの可能性を書かない。** 根拠がありません
- 断定しない。「〜が原因です」ではなく「〜と重なっています」と書く
- 見出し・箇条書き・表を作らない。**2〜3文の地の文**にする
- 特筆すべきことが無ければ「大きな変化はありません」とだけ書く

所見の文だけを出力してください（前置きも引用符も付けない）。"""


def summarise(provider: Provider, facts: str) -> str | None:
    """所見を1つ返す。**作れなければ `None`**（表だけが届く）。

    `facts` はレポートの本文（表を含む）。ここに現れない数値を所見が使ったら、
    モデルが作った数値なので採用しない。
    """
    result = provider.chat(
        [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(role="user", content=facts),
        ],
        thinking=False,
    )
    if not result.available:
        LOGGER.info("AI の所見は付けない（表だけを出す）")
        return None
    return _accept(result.text.strip(), facts)


def _accept(text: str, facts: str) -> str | None:
    if not text:
        return None
    if FORBIDDEN.search(text):
        LOGGER.warning(
            "所見に確度の表現があったので捨てた",
            extra={logs.FIELDS_KEY: {"text": text[:100]}},
        )
        return None
    if "\n" in text or text.lstrip().startswith(("#", "|", "-", "*")):
        # **体裁を作らせない。** レポートの構造はコードが持つ（#38 §2.3 と同じ）
        LOGGER.warning(
            "所見が体裁を作ったので捨てた", extra={logs.FIELDS_KEY: {"text": text[:100]}}
        )
        return None
    allowed = set(NUMBER.findall(facts))
    invented = [token for token in NUMBER.findall(text) if token not in allowed]
    if invented:
        # **表に無い数値は、モデルが作った数値である**
        LOGGER.warning(
            "所見に表へ無い数値があったので捨てた",
            extra={logs.FIELDS_KEY: {"numbers": invented[:5], "text": text[:100]}},
        )
        return None
    return text[:MAX_CHARS]
