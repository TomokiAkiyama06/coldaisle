"""返す文の扱い（決定記録 0168 §2.3 の数値の検査 / §2.4）。

- 通知を起こす記法（`<!channel>` `<!here>` `<@U…>` など）と `<…|…>` のリンクを取り除く
- `&` `<` `>` を Slack の規則でエスケープする
- 1 投稿の長さで切る
- ツールの結果に無い数値があれば末尾に未検証の印を付ける（答えは捨てない。0168 §5 の 5）
"""

from __future__ import annotations

import re
from collections.abc import Iterable

UNAVAILABLE_TEXT = "いま AI が使えません。ダッシュボードで確かめてください。"
"""LLM が使えない（止まっている・上限・鍵が無い）ときの固定の文（0168 §2.4）。

値を自分で作らない。
"""

NO_ANSWER_TEXT = "答えを作れませんでした。ダッシュボードで確かめてください。"
"""LLM は応じたが、返す文が空だったとき。"""

WAIT_TEXT = "少し待ってください。質問が続いたので、しばらくしてからもう一度聞いてください。"
"""1 ユーザーの質問の回数の上限に達したとき（0168 §2.5）。"""

UNVERIFIED_MARK = "※ ツールの結果に無い数値を含みます（未検証）"
"""答えの数値の検査（0168 §2.3 / 0143 §2.11 と同じ形）で印を付けるときの文。"""

_SPECIAL = re.compile(r"<(?:[!@#]|[a-zA-Z][a-zA-Z0-9+.-]*:)[^<>]*>|<[^<>|]*\|[^<>]*>")
"""Slack の特別な記法。通知（`<!channel>` `<!here>` `<!everyone>` `<!subteam^…>`）・
ユーザーとチャンネルへの言及（`<@U…>` `<#C…>`）・リンク（`<https://…>` `<mailto:…>` `<…|…>`）。

山括弧の全部を消すと「温度 < 50 かつ > 30」のような地の文まで消えるので、記法の形だけを消す。
残った `<` `>` は下のエスケープで記法として読まれなくなる。
"""

_BROADCAST = re.compile(r"@(channel|here|everyone)\b", re.IGNORECASE)
"""地の文の `@here` など。

`chat.postMessage` は既定では通知にしないが、念のため全角の `＠` にする。
"""

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_PARTIAL_ENTITY = re.compile(r"&[a-z]{0,3}$")
"""切った末尾に残った途中のエスケープ（`&am` など）。"""

NUMBER = re.compile(r"(?:(?<![0-9])[-+−])?\d+(?:\.\d+)?")
"""数値らしきもの。符号は**数字の直後でないとき**だけ数値の一部とみなす。

`2026-10-23` を `2026` / `10` / `23` と読む（`-10` と読まない）。`−`（U+2212）も負の符号。
"""


def sanitize(text: str) -> str:
    """LLM の文から記法を取り除き、Slack の規則でエスケープする。"""
    cleaned = _CONTROL.sub("", text)
    cleaned = _SPECIAL.sub("", cleaned)
    cleaned = _BROADCAST.sub(lambda m: f"＠{m.group(1)}", cleaned)
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").strip()


def compose(text: str, *, verified: bool, max_chars: int) -> str:
    """返す 1 投稿を作る。**印は切らずに残す**（本文のほうを短くする）。"""
    body = sanitize(text)
    mark = "" if verified else f"\n{UNVERIFIED_MARK}"
    limit = max_chars - len(mark)
    if len(body) > limit:
        body = _PARTIAL_ENTITY.sub("", body[: max(limit - 1, 0)]) + "…"
    return f"{body}{mark}"


def fixed(text: str, *, max_chars: int) -> str:
    """固定の文（エスケープの要らない文だけ）。"""
    return text[:max_chars]


def number_values(text: str) -> frozenset[float]:
    """文の中の数値の集合（ツールの結果の JSON から許す数値を作る）。"""
    return frozenset(_value(token) for token in NUMBER.findall(text))


def invented_numbers(text: str, allowed: Iterable[float] | frozenset[float]) -> list[str]:
    """`allowed` に無い数値。`45.2` と `45.20` は同じ値とみなす。符号は含めて比べる。"""
    allowed_set = frozenset(allowed)
    return [token for token in NUMBER.findall(text) if _value(token) not in allowed_set]


def _value(token: str) -> float:
    return float(token.replace("−", "-"))
