"""質問に読み取り専用のツールで答える短い tool loop（決定記録 0168 §2.3 / §2.5）。

- LLM に渡すツールは `ALLOWED_TOOLS` の 4 つだけ。`query_series`（時系列の点の列）と未定義の名前は、
  呼ばれても**実行せず結果を渡さない**（ルール 8 / FR-504）
- ツールは今の `ToolRegistry` をそのまま使い、除外（`config/report.yaml`。0131 §2.4a）を当てる。
  **除外の一覧が読めなければ LLM を呼ばない**
- ツールの呼び出しを返す応答は 1 つの質問で `max_round_trips` 回まで。超えたらツールを呼ばせずに
  （`tool_choice="none"`）ここまでの結果で答えさせる
- LLM の呼び出しの前に 1 日の上限を数える。上限・LLM が使えないときは固定の文
- 答えに出る数値がツールの結果に無ければ未検証とする（印は `reply.compose` が付ける）

**LLM に書き込み・実行の手段は無い**（ルール 1・2）。この層がするのは ToolRegistry の読み出しと、
LLM の文を返すことだけ。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from coldaisle import logs
from coldaisle.ai.provider import ChatMessage, Provider, SlackQaSettings
from coldaisle.ai.tools import DEFINITIONS, GUIDANCE, ToolRegistry
from coldaisle.clock import Clock, WallClock
from coldaisle.slackbot.policy import DailyCallBudget
from coldaisle.slackbot.reply import invented_numbers, number_values

LOGGER = logging.getLogger("coldaisle.slackbot")

ALLOWED_TOOLS = ("get_latest", "get_stats", "list_alerts", "describe_system")
"""LLM に渡すツール（0168 §2.3 / §5 の 2）。**`query_series` は渡さない。**"""

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    definition for definition in DEFINITIONS if definition["function"]["name"] in ALLOWED_TOOLS
]

DEFINED_TOOLS = frozenset(definition["function"]["name"] for definition in DEFINITIONS)
"""ToolRegistry にあるツールの名前（ログの名前の判定だけに使う）。"""

_LOCAL_SUFFIX = "_local"

SYSTEM_PROMPT = """あなたは GPU サーバーの温度と負荷を監視するアシスタントです。
Slack で聞かれたことに、ツールで得た値だけを使って答えてください。

- 日本語で、短く（数行で）答える
- 数値は**ツールの結果にあるものだけ**を使う。計算や丸めをしない。単位を添える
- 時刻は結果の `*_local`（下の時刻帯の時刻）の欄をそのまま使う
- 分からないこと・ツールで取れないことは、分からないと書く
- 確度・確信度・パーセントでの可能性を書かない
- メトリクスの名前が分からなければ describe_system で確かめる
- 期間は get_stats の window（例: 1h / 24h / 7d）か、
  list_alerts の from / to（Unix ミリ秒）で指定する
- ユーザーへの言及・@channel・リンクを書かない
"""


AnswerKind = Literal["answered", "unavailable", "no_answer"]


@dataclass(frozen=True)
class Answer:
    """1 つの質問の結果。`text` は LLM の文（記法の除去とエスケープの前）か空。"""

    kind: AnswerKind
    text: str = ""
    verified: bool = True
    tools: tuple[str, ...] = ()
    """呼ばれたツールの名前（ログ用）。ToolRegistry に無い名前は `unknown`。"""
    round_trips: int = 0
    """ツールの呼び出しを返した応答の数。"""
    llm_calls: int = 0
    reason: str = ""
    """答えられなかった理由（ログ用。本文・ID を含めない）。"""


@dataclass
class QuestionAnswerer:
    """tool loop。**1 つのスレッドからだけ使う**（`SqliteStore` の接続をスレッド間で共有しない）。

    `open_tools` は ToolRegistry を作る関数で、最初の質問のときにこのスレッドで 1 回だけ呼ぶ。
    `None` なら除外の一覧が読めなかったということで、LLM を呼ばない（0168 §2.3）。
    """

    provider: Provider
    settings: SlackQaSettings
    budget: DailyCallBudget
    open_tools: Callable[[], ToolRegistry] | None
    zone: ZoneInfo
    """日の境界と `*_local` の時刻帯。`config/report.yaml` の `timezone`（日次レポートと同じ。
    0168 §2.3）。**OS の時刻帯は使わない。**"""
    clock: Clock = field(default_factory=WallClock)
    """現在時刻（system の指示に入れる）。`coldaisle.clock` から取る（#42）。"""
    _tools: ToolRegistry | None = field(default=None, init=False, repr=False)

    def answer(self, question: str) -> Answer:
        """質問に答える。**例外を上へ投げない**（受信のループを巻き込まない）。"""
        if self.open_tools is None:
            return Answer(kind="unavailable", reason="LLM へ渡さない一覧が読めない")
        try:
            if self._tools is None:
                self._tools = self.open_tools()
            return self._loop(self._tools, question)
        except Exception as error:
            LOGGER.warning(
                "Slack の質問に答える途中で失敗した",
                extra={logs.FIELDS_KEY: {"error": type(error).__name__}},
            )
            return Answer(kind="unavailable", reason=type(error).__name__)

    def _loop(self, tools: ToolRegistry, question: str) -> Answer:
        system = f"{SYSTEM_PROMPT}\n{time_basis(self.clock.now_ms(), self.zone)}\n\n{GUIDANCE}"
        messages = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=question[: self.settings.max_question_chars]),
        ]
        allowed: set[float] = set()
        used: list[str] = []
        round_trips = 0
        calls = 0
        # この質問でこれまでに実行したツールの呼び出しの数（上限で実行しなかったものは数えない）
        executed_in_question = 0
        while True:
            final = round_trips >= self.settings.max_round_trips
            if not self.budget.try_acquire():
                return Answer(
                    kind="unavailable",
                    tools=tuple(used),
                    round_trips=round_trips,
                    llm_calls=calls,
                    reason="1 日の上限",
                )
            calls += 1
            result = self.provider.chat(
                messages,
                thinking=False,
                tools=TOOL_DEFINITIONS,
                tool_choice="none" if final else None,
            )
            if not result.available:
                return Answer(
                    kind="unavailable",
                    tools=tuple(used),
                    round_trips=round_trips,
                    llm_calls=calls,
                    reason="LLM が使えない",
                )
            if final or not result.tool_calls:
                text = result.text.strip()
                if not text:
                    return Answer(
                        kind="no_answer",
                        tools=tuple(used),
                        round_trips=round_trips,
                        llm_calls=calls,
                        reason="返す文が空",
                    )
                return Answer(
                    kind="answered",
                    text=text,
                    verified=not invented_numbers(text, frozenset(allowed)),
                    tools=tuple(used),
                    round_trips=round_trips,
                    llm_calls=calls,
                )
            round_trips += 1
            normalized = [_normalize(call, index) for index, call in enumerate(result.tool_calls)]
            messages.append(
                ChatMessage(role="assistant", content=result.text, tool_calls=tuple(normalized))
            )
            for index, call in enumerate(normalized):
                name = call["function"]["name"]
                # ログに残す名前は既知のものだけ（LLM の作った文字列をそのまま残さない）
                used.append(name if name in DEFINED_TOOLS else "unknown")
                content, ran = self._run(
                    tools, name, call["function"]["arguments"], index, executed_in_question
                )
                executed_in_question += ran
                allowed |= number_values(content)
                messages.append(ChatMessage(role="tool", content=content, tool_call_id=call["id"]))

    def _run(
        self, tools: ToolRegistry, name: str, arguments: str, index: int, so_far: int
    ) -> tuple[str, int]:
        """1 つ実行し、LLM へ渡す文（JSON）と実行した数（0 か 1）を返す。

        **許可していないツールは実行しない。**
        `index` はこの応答の中の順番、`so_far` はこの質問でこれより前に実行した呼び出しの数。
        上限（1 応答 `max_tool_calls_per_round`・1 質問 `max_tool_calls_per_question`）を超えた
        呼び出しは実行せず、エラーの結果だけを返す（0168 §2.3）。
        """
        ran = 0
        if index >= self.settings.max_tool_calls_per_round:
            result: dict[str, Any] = {"error": "1 回の応答で呼べるツールの数の上限を超えた"}
        elif so_far >= self.settings.max_tool_calls_per_question:
            result = {"error": "1 つの質問で呼べるツールの数の上限を超えた"}
        elif name not in ALLOWED_TOOLS:
            result = {"error": f"そのツールは使えない: {name[:64]}", "available": ALLOWED_TOOLS}
        else:
            result = _narrow(name, tools.call(name, arguments))
            ran = 1
        text = json.dumps(_with_local_times(result, self.zone), ensure_ascii=False, default=str)
        return text[: self.settings.max_tool_result_chars], ran


DROPPED_FIELDS: dict[str, tuple[str, ...]] = {"describe_system": ("devices",)}
"""この経路で外へ出さない欄（0168 §2.3）。`devices` は `device_id`・`fw`・センサーの `channel`・
`gpio`・ROM の末尾・分解能で、個体の識別に近く、質問に答えるのに要らない。
Workspace 向けの `describe_system` は変えない（ここで取り除くだけ）。
"""


def _narrow(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """この経路で渡さない欄を取り除く（`ToolRegistry` の結果は変えずに写しを作る）。"""
    dropped = DROPPED_FIELDS.get(name, ())
    return {key: value for key, value in result.items() if key not in dropped}


def _normalize(call: dict[str, Any], index: int) -> dict[str, Any]:
    """呼び出しを OpenAI の形に揃える。形が崩れていても落とさず、空の呼び出しとして扱う。

    応答の 1 つの呼び出しに必ず 1 つの結果を返す（Messages API は欠けを拒む）。
    """
    function = call.get("function") if isinstance(call, dict) else None
    function = function if isinstance(function, dict) else {}
    name = function.get("name")
    arguments = function.get("arguments")
    call_id = call.get("id") if isinstance(call, dict) else None
    return {
        "id": call_id if isinstance(call_id, str) and call_id else f"call_{index}",
        "type": "function",
        "function": {
            "name": name if isinstance(name, str) else "",
            "arguments": _arguments(arguments),
        },
    }


def _arguments(arguments: object) -> str:
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False)
    return "{}"


def time_basis(now_ms: int, zone: ZoneInfo) -> str:
    """system の指示に入れる時刻の基準（0168 §2.3）。現在時刻・時刻帯・今日と昨日の範囲。

    「昨日」は設定の時刻帯の前日の 0 時から 24 時。範囲の Unix ミリ秒はコードが作り、LLM に日の
    境界を計算させない。夏時間のある時刻帯でも日付の 0 時どうしで作る。
    """
    now = datetime.fromtimestamp(now_ms / 1000, tz=zone)
    today = datetime.combine(now.date(), time.min, tzinfo=zone)
    yesterday = datetime.combine(now.date() - timedelta(days=1), time.min, tzinfo=zone)

    def ms(at: datetime) -> int:
        return int(at.timestamp() * 1000)

    return (
        f"時刻帯: {zone.key}（日の境界はこの時刻帯の 0 時。*_local もこの時刻帯）\n"
        f"現在時刻: {now.strftime('%Y-%m-%d %H:%M:%S')}（Unix ミリ秒 {now_ms}）\n"
        f"今日: from={ms(today)} から現在まで\n"
        f"昨日: from={ms(yesterday)} to={ms(today)}（{yesterday.strftime('%Y-%m-%d')} の 0〜24 時）"
    )


def _with_local_times(value: Any, zone: ZoneInfo) -> Any:  # noqa: ANN401 - JSON の木をそのまま辿る
    """`*_ms`（Unix ミリ秒）の欄の横に、ローカル時刻の文字列 `*_local` を足す。

    LLM にミリ秒から時刻を計算させない（数値を作らせない）ため。値はコードが作る。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key] = _with_local_times(item, zone)
            if (
                isinstance(key, str)
                and key.endswith("_ms")
                and isinstance(item, int)
                and not isinstance(item, bool)
                and item > 0
            ):
                out[f"{key[:-3]}{_LOCAL_SUFFIX}"] = _local(item, zone)
        return out
    if isinstance(value, list | tuple):
        return [_with_local_times(item, zone) for item in value]
    return value


def _local(ms: int, zone: ZoneInfo) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, tz=zone).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""
