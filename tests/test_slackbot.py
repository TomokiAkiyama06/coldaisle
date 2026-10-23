"""Slack のメンションへの返答（#374 / 決定記録 0168）。

実機・ネットワークは使わない。Slack の Web API は `httpx.MockTransport`、WebSocket は偽の接続、
LLM は台本どおりに答える偽の Provider で確かめる（0168 §2.6）。

- 許可リストの外・ボット自身・空のリストで答えない
- tool loop が 4 往復で打ち切られる・`query_series` と未定義のツールを呼ばせても結果を渡さない
- 除外のメトリクスが答えの材料に入らない・除外の一覧が読めないと答えない
- 通知を起こす記法とリンクが返す文から消える
- LLM が使えないと固定の文だけを返す・上限で呼ばない
- `coldaisle.control` と `coldaisle.ingest` を import しない
"""

import ast
import json
import logging
import subprocess
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from coldaisle.ai import ChatMessage, ChatResult, ToolRegistry
from coldaisle.ai.provider import AiSettings, SlackQaSettings
from coldaisle.clock import SimulatedClock
from coldaisle.llm_exclusions import LlmExclusions
from coldaisle.metrics import MetricCatalog
from coldaisle.notify.notifiers import DeliveryError
from coldaisle.notify.slack_bot import SlackApi
from coldaisle.rules import RuleSet
from coldaisle.slackbot import cli
from coldaisle.slackbot.bot import Job, MentionHandler
from coldaisle.slackbot.policy import Allowlist, DailyCallBudget, UserRateLimiter
from coldaisle.slackbot.qa import ALLOWED_TOOLS, TOOL_DEFINITIONS, QuestionAnswerer, time_basis
from coldaisle.slackbot.reply import (
    NO_ANSWER_TEXT,
    UNAVAILABLE_TEXT,
    UNVERIFIED_MARK,
    WAIT_TEXT,
    compose,
    invented_numbers,
    number_values,
    sanitize,
)
from coldaisle.slackbot.socket_mode import Backoff, SocketModeClient
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR

ROOT = Path(__file__).resolve().parents[1]
SLACKBOT_DIR = ROOT / "src" / "coldaisle" / "slackbot"

# 明らかな仮の値（実 ID ではない。ルール 10）
BOT = "U0000BOT00"
OWNER = "U0000OWNER"
STRANGER = "U0000OTHER"
CHANNEL = "C0000TEST0"
OTHER_CHANNEL = "C0000OTHER"
APP_TOKEN = "xapp-test-not-real"  # pragma: allowlist secret - テストのダミー値
BOT_TOKEN = "xoxb-test-not-real"  # pragma: allowlist secret - テストのダミー値
NOW_MS = 1_787_616_000_000
TOKYO = ZoneInfo("Asia/Tokyo")


@pytest.fixture(scope="module")
def qa_settings() -> SlackQaSettings:
    settings = AiSettings.from_yaml(CONFIG_DIR / "ai.yaml").slack_qa
    assert settings is not None
    return settings


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "bot.db", rules=rules, clock=SimulatedClock(NOW_MS)) as opened:
        opened.insert_samples(
            [
                Sample(
                    ts_ms=NOW_MS - 1_000,
                    readings=(
                        Reading(metric="air.gpu_intake", value=31.5, quality=Quality.OK),
                        Reading(metric="air.room", value=24.25, quality=Quality.OK),
                    ),
                )
            ]
        )
        yield opened


def registry(store, exclusions: LlmExclusions | None = None) -> ToolRegistry:
    return ToolRegistry(
        store=store,
        catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
        rules=RuleSet.from_yaml(CONFIG_DIR / "rules.yaml"),
        clock=store.clock,
        exclusions=exclusions or LlmExclusions.from_yaml(CONFIG_DIR / "report.yaml"),
    )


def call(name: str, arguments: dict | None = None, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }


class ScriptedProvider:
    """台本どおりに答える偽の Provider。最後の応答を繰り返す。受けた要求を覚える。"""

    def __init__(self, *results: ChatResult) -> None:
        self.results = list(results)
        self.requests: list[dict] = []

    def chat(self, messages, *, thinking=None, tools=None, tool_choice=None) -> ChatResult:
        self.requests.append(
            {"messages": list(messages), "tools": tools, "tool_choice": tool_choice}
        )
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]

    def probe(self) -> ChatResult:
        return ChatResult(available=True, text="pong")


def answerer(provider, qa_settings, store=None, *, budget=100, open_tools="default", now_ms=NOW_MS):
    if open_tools == "default":
        open_tools = (lambda: registry(store)) if store is not None else None
    return QuestionAnswerer(
        provider=provider,
        settings=qa_settings,
        budget=budget
        if isinstance(budget, DailyCallBudget)
        else DailyCallBudget(budget, today=lambda: date(2026, 8, 25)),
        open_tools=open_tools,
        clock=SimulatedClock(now_ms),
        zone=TOKYO,
    )


def tool_messages(provider: ScriptedProvider) -> list[ChatMessage]:
    return [m for m in provider.requests[-1]["messages"] if m.role == "tool"]


class Posted:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    def __call__(self, channel: str, thread_ts: str, text: str) -> None:
        self.items.append((channel, thread_ts, text))


def handler(qa_settings, provider, store=None, *, allowlist=None, limiter=None, post=None):
    return MentionHandler(
        bot_user_id=BOT,
        allowlist=allowlist or Allowlist(frozenset({CHANNEL}), frozenset({OWNER})),
        limiter=limiter or UserRateLimiter(3, 60.0),
        answerer=answerer(provider, qa_settings, store),
        post=post or Posted(),
        settings=qa_settings,
    )


def mention(**overrides) -> dict:
    event = {
        "type": "app_mention",
        "user": OWNER,
        "channel": CHANNEL,
        "ts": "1787616000.000100",
        "text": f"<@{BOT}> いまの GPU の吸気は？",
    }
    event.update(overrides)
    return event


def drain(target: MentionHandler) -> None:
    """積まれた分をこのスレッドで答える（答えるスレッドの代わり）。"""
    while not target._queue.empty():
        job = target._queue.get_nowait()
        if job is not None:
            target.process(job)


def test_the_worker_answers_until_told_to_stop(qa_settings, store):
    provider = ScriptedProvider(ChatResult(available=True, text="はい"))
    posted = Posted()
    target = handler(qa_settings, provider, store, post=posted)
    worker = threading.Thread(target=target.run_worker, daemon=True)
    worker.start()
    target.offer(mention())
    target.stop_worker()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(posted.items) == 1


# ---------------------------------------------------------------- 設定


def test_repository_settings_follow_the_decision(qa_settings):
    """0168 §5 の推奨: 1 日 100 回・1 ユーザー 1 分 3 質問・往復 4・1 投稿 3000 文字。"""
    assert qa_settings.daily_call_limit == 100
    assert qa_settings.user_questions_per_window == 3
    assert qa_settings.user_window_s == 60.0
    assert qa_settings.max_round_trips == 4
    assert qa_settings.max_reply_chars == 3000


def test_only_the_four_read_only_tools_are_offered():
    """`query_series` を渡さない（0168 §2.3 / ルール 8）。"""
    names = [d["function"]["name"] for d in TOOL_DEFINITIONS]
    assert names == ["get_latest", "get_stats", "list_alerts", "describe_system"]
    assert set(ALLOWED_TOOLS) == set(names)
    assert "query_series" not in names


# ---------------------------------------------------------------- 誰に答えるか


@pytest.mark.parametrize(
    "event",
    [
        mention(user=STRANGER),  # 許可リストの外のユーザー
        mention(channel=OTHER_CHANNEL),  # 許可リストの外のチャンネル
        mention(user=BOT),  # ボット自身
        mention(bot_id="B0000OTHER"),  # 他のボット
        mention(edited={"user": OWNER, "ts": "1"}),  # 編集
        mention(subtype="message_changed"),
        mention(channel="D0000DM000"),  # DM
        mention(channel_type="im"),
        mention(type="message"),  # app_mention 以外
        mention(text=f"<@{BOT}>"),  # 本文が空
    ],
)
def test_mentions_outside_the_rules_are_ignored(qa_settings, event):
    provider = ScriptedProvider(ChatResult(available=True, text="x"))
    posted = Posted()
    target = handler(qa_settings, provider, post=posted)
    target.offer(event)
    drain(target)
    assert posted.items == []  # 返事もしない
    assert provider.requests == []


@pytest.mark.parametrize(
    "allowlist",
    [
        Allowlist(frozenset(), frozenset({OWNER})),
        Allowlist(frozenset({CHANNEL}), frozenset()),
        Allowlist(frozenset(), frozenset()),
    ],
)
def test_an_empty_list_answers_nobody(qa_settings, allowlist):
    provider = ScriptedProvider(ChatResult(available=True, text="x"))
    posted = Posted()
    target = handler(qa_settings, provider, allowlist=allowlist, post=posted)
    target.offer(mention())
    drain(target)
    assert posted.items == []
    assert provider.requests == []


def test_allowlist_from_env_needs_both_and_rejects_malformed_values(caplog):
    env = {"COLDAISLE_SLACK_QA_CHANNELS": f" {CHANNEL} , ", "COLDAISLE_SLACK_QA_USERS": OWNER}
    allowlist = Allowlist.from_env(env)
    assert allowlist.allows(CHANNEL, OWNER)
    assert not allowlist.allows(CHANNEL, STRANGER)
    assert Allowlist.from_env({}).empty
    # 1 つでも形が違えば、その一覧は空（答えない）。値はログに出さない
    with caplog.at_level(logging.WARNING, logger="coldaisle.slackbot"):
        broken = Allowlist.from_env({**env, "COLDAISLE_SLACK_QA_USERS": f"{OWNER},#general"})
        broken.warn_if_empty()
    assert broken.empty
    assert OWNER not in caplog.text
    assert "general" not in caplog.text
    # DM のチャンネルは許可リストに置けない
    assert Allowlist.from_env({**env, "COLDAISLE_SLACK_QA_CHANNELS": "D0000DM000"}).empty


def test_an_empty_list_warns_once_at_startup(caplog):
    with caplog.at_level(logging.WARNING, logger="coldaisle.slackbot"):
        Allowlist.from_env({}).warn_if_empty()
    assert len(caplog.records) == 1


def test_an_allowed_mention_is_answered_in_its_thread(qa_settings, store):
    provider = ScriptedProvider(ChatResult(available=True, text="落ち着いています。"))
    posted = Posted()
    target = handler(qa_settings, provider, store, post=posted)
    target.offer(mention(thread_ts="1787615000.000001"))
    drain(target)
    assert posted.items == [(CHANNEL, "1787615000.000001", "落ち着いています。")]
    # 質問からボットへのメンションを除いて送る
    user = next(m for m in provider.requests[0]["messages"] if m.role == "user")
    assert user.content == "いまの GPU の吸気は？"


def test_a_redelivered_mention_is_answered_once(qa_settings, store):
    provider = ScriptedProvider(ChatResult(available=True, text="はい"))
    posted = Posted()
    target = handler(qa_settings, provider, store, post=posted)
    target.offer(mention())
    target.offer(mention())
    drain(target)
    assert len(posted.items) == 1
    assert posted.items[0][1] == mention()["ts"]  # スレッドが無ければメンションの ts へ


def test_a_user_asking_too_often_is_asked_to_wait(qa_settings, store):
    provider = ScriptedProvider(ChatResult(available=True, text="はい"))
    posted = Posted()
    clock = [0.0]
    limiter = UserRateLimiter(3, 60.0, monotonic=lambda: clock[0])
    target = handler(qa_settings, provider, store, limiter=limiter, post=posted)
    for index in range(4):
        target.offer(mention(ts=f"1787616000.00010{index}"))
    drain(target)
    assert [text for _, _, text in posted.items] == ["はい", "はい", "はい", WAIT_TEXT]
    assert len(provider.requests) == 3  # 待たせた質問では LLM を呼ばない
    clock[0] = 60.0
    assert limiter.try_acquire(OWNER)  # 窓を過ぎれば聞ける


def test_a_full_queue_drops_the_mention_without_blocking(qa_settings):
    settings = qa_settings.model_copy(update={"queue_size": 1})
    provider = ScriptedProvider(ChatResult(available=True, text="x"))
    posted = Posted()
    target = MentionHandler(
        bot_user_id=BOT,
        allowlist=Allowlist(frozenset({CHANNEL}), frozenset({OWNER})),
        limiter=UserRateLimiter(10, 60.0),
        answerer=answerer(provider, settings),
        post=posted,
        settings=settings,
    )
    target.offer(mention(ts="1.1"))
    target.offer(mention(ts="1.2"))  # 溢れる（待たない）
    drain(target)
    assert len(posted.items) == 1


# ---------------------------------------------------------------- tool loop


def test_the_tool_loop_stops_after_four_round_trips(qa_settings, store):
    """ツールを呼び続ける LLM でも 4 往復で打ち切り、ツール無しで答えさせる（0168 §2.3）。"""
    provider = ScriptedProvider(
        *[ChatResult(available=True, tool_calls=[call("get_latest")]) for _ in range(4)],
        ChatResult(available=True, text="ここまでの値では吸気は 31.5 です。"),
    )
    result = answerer(provider, qa_settings, store).answer("吸気は？")
    assert result.kind == "answered"
    assert result.round_trips == 4
    assert result.llm_calls == 5
    assert [r["tool_choice"] for r in provider.requests] == [None, None, None, None, "none"]
    assert result.verified is True


def test_a_tool_call_in_the_final_turn_is_not_run(qa_settings, store):
    """打ち切りの応答がまだツールを求めても実行しない（文が無ければ固定の文）。"""
    provider = ScriptedProvider(ChatResult(available=True, tool_calls=[call("get_latest")]))
    result = answerer(provider, qa_settings, store).answer("吸気は？")
    assert result.kind == "no_answer"
    assert result.llm_calls == qa_settings.max_round_trips + 1
    assert len(tool_messages(provider)) == qa_settings.max_round_trips


@pytest.mark.parametrize("name", ["query_series", "write_pwm", "set_fan_demand", ""])
def test_disallowed_and_unknown_tools_get_no_results(qa_settings, store, name):
    """`query_series` と未定義の名前は実行せず、結果（値）を渡さない。"""
    provider = ScriptedProvider(
        ChatResult(
            available=True,
            tool_calls=[call(name, {"metric": "air.gpu_intake", "window": "1h"})],
        ),
        ChatResult(available=True, text="分かりません。"),
    )
    called: list[str] = []
    tools = registry(store)
    original = tools.call

    def spy(tool_name, arguments=None):
        called.append(tool_name)
        return original(tool_name, arguments)

    tools.call = spy  # type: ignore[method-assign]
    result = answerer(provider, qa_settings, open_tools=lambda: tools).answer("系列を出して")
    assert called == []
    [message] = tool_messages(provider)
    body = json.loads(message.content)
    assert set(body) == {"error", "available"}
    assert "31.5" not in message.content
    assert "points" not in message.content
    expected = ("query_series",) if name == "query_series" else ("unknown",)
    assert result.tools == expected  # ログに残す名前は既知のものだけ


def test_too_many_calls_in_one_turn_are_not_run(qa_settings, store):
    settings = qa_settings.model_copy(update={"max_tool_calls_per_round": 1})
    provider = ScriptedProvider(
        ChatResult(
            available=True,
            tool_calls=[call("get_latest", call_id="a"), call("get_latest", call_id="b")],
        ),
        ChatResult(available=True, text="はい"),
    )
    answerer(provider, settings, store).answer("x")
    first, second = tool_messages(provider)
    assert first.tool_call_id == "a" and "31.5" in first.content
    # 実行しないが、呼び出しごとに 1 つの結果を返す（Messages API は欠けを拒む）
    assert second.tool_call_id == "b" and "error" in json.loads(second.content)


def test_tool_results_carry_local_times_instead_of_making_the_llm_convert(qa_settings, store):
    provider = ScriptedProvider(
        ChatResult(available=True, tool_calls=[call("get_latest")]),
        ChatResult(available=True, text="はい"),
    )
    answerer(provider, qa_settings, store).answer("x")
    [message] = tool_messages(provider)
    body = json.loads(message.content)
    assert body["ts_ms"] == NOW_MS - 1_000
    local = datetime.fromtimestamp((NOW_MS - 1_000) / 1000, tz=TOKYO)
    assert body["ts_local"] == local.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 除外


def test_excluded_metrics_never_reach_the_llm(qa_settings, store):
    exclusions = LlmExclusions(metric_patterns=("air.gpu_*",), rule_ids=frozenset())
    provider = ScriptedProvider(
        ChatResult(
            available=True,
            tool_calls=[
                call("get_latest", call_id="a"),
                call("get_stats", {"metric": "air.gpu_intake", "window": "1h"}, call_id="b"),
                call("describe_system", call_id="c"),
            ],
        ),
        ChatResult(available=True, text="室温は 24.25 です。"),
    )
    result = answerer(provider, qa_settings, open_tools=lambda: registry(store, exclusions)).answer(
        "x"
    )
    contents = [m.content for m in tool_messages(provider)]
    assert "24.25" in contents[0]  # 除外でないものは渡る
    for content in contents:
        assert "31.5" not in content
        assert 'air.gpu_intake": {' not in content
    assert json.loads(contents[1])["max"] is None  # 無いメトリクスと同じ応答
    assert result.verified is True


def test_unreadable_exclusions_mean_no_llm_and_the_fixed_text(qa_settings, tmp_path):
    broken = tmp_path / "report.yaml"
    broken.write_text("llm_excluded_metrics: [\n", encoding="utf-8")
    args = cli.argparse.Namespace(
        report_config=broken,
        metrics=CONFIG_DIR / "metrics.yaml",
        rules=CONFIG_DIR / "rules.yaml",
        quality_rules=CONFIG_DIR / "quality.yaml",
        db=tmp_path / "x.db",
    )
    opener = cli._tools_opener(args)
    assert opener is None
    provider = ScriptedProvider(ChatResult(available=True, text="31.5 です"))
    posted = Posted()
    target = MentionHandler(
        bot_user_id=BOT,
        allowlist=Allowlist(frozenset({CHANNEL}), frozenset({OWNER})),
        limiter=UserRateLimiter(3, 60.0),
        answerer=answerer(provider, qa_settings, open_tools=opener),
        post=posted,
        settings=qa_settings,
    )
    target.offer(mention())
    drain(target)
    assert provider.requests == []
    assert [text for _, _, text in posted.items] == [UNAVAILABLE_TEXT]


def test_readable_exclusions_open_the_tools_lazily(tmp_path, rules):
    args = cli.argparse.Namespace(
        report_config=CONFIG_DIR / "report.yaml",
        metrics=CONFIG_DIR / "metrics.yaml",
        rules=CONFIG_DIR / "rules.yaml",
        quality_rules=CONFIG_DIR / "quality.yaml",
        db=tmp_path / "lazy.db",
    )
    opener = cli._tools_opener(args)
    assert opener is not None
    assert not (tmp_path / "lazy.db").exists()  # 答えるスレッドで初めて開く
    tools = opener()
    assert isinstance(tools, ToolRegistry)
    tools.store.close()


# ---------------------------------------------------------------- 返す文


@pytest.mark.parametrize(
    ("raw", "gone"),
    [
        ("<!channel> 吸気は 31.5 です", "<!channel>"),
        ("<!here|here> 吸気は 31.5 です", "here"),
        ("<!everyone> 見て", "everyone"),
        ("<!subteam^S0000TEAM> 見て", "subteam"),
        (f"<@{OWNER}> さん、31.5 です", OWNER),
        ("<#C0000OTHER> を見て", "C0000OTHER"),
        ("<https://phish.example.invalid|ダッシュボード> を開いて", "phish"),
        ("<https://phish.example.invalid> を開いて", "phish"),
        ("<mailto:postmaster> へ", "mailto"),
    ],
)
def test_notification_syntax_and_links_are_removed(raw, gone):
    text = sanitize(raw)
    assert gone not in text
    assert "<" not in text and ">" not in text


@pytest.mark.parametrize(
    "raw",
    [
        "詳しくは https://phish.example.invalid/login を開いて",
        "http://phish.example.invalid を開いて",
        "www.phish.example.invalid を開いて",
        "ftp://phish.example.invalid/x を開いて",
    ],
)
def test_bare_urls_are_removed(raw):
    """山括弧で囲まない素の URL も消す（Slack は押せるリンクにする。PR #376 の指摘）。"""
    text = sanitize(raw)
    assert "phish" not in text
    assert "リンクは省きました" in text


@pytest.mark.parametrize(
    "raw",
    [
        "詳細はhttps://phish.example.invalid。温度は71°Cです",
        "詳細はwww.phish.example.invalid。温度は71°Cです",
    ],
)
def test_a_bare_url_right_after_japanese_is_removed(raw):
    text = sanitize(raw)
    assert "phish" not in text
    assert text.startswith("詳細は") and text.endswith("。温度は71°Cです")


def test_prose_after_a_bare_url_is_kept():
    text = sanitize("https://phish.example.invalid。温度は71°Cです")
    assert "phish" not in text
    assert text.endswith("。温度は71°Cです")


def test_numbers_and_units_are_not_taken_for_urls():
    assert sanitize("GPU は 71.0 °C、電力は 300 W です") == "GPU は 71.0 °C、電力は 300 W です"


def test_plain_broadcast_words_and_brackets_are_defused():
    text = sanitize("@here 温度 < 50 かつ > 30 & 安定")
    assert "@here" not in text
    assert text == "＠here 温度 &lt; 50 かつ &gt; 30 &amp; 安定"


def test_the_reply_is_cut_to_the_limit_and_keeps_the_mark():
    text = compose("あ" * 5000 + " 99.9", verified=False, max_chars=3000)
    assert len(text) <= 3000
    assert text.endswith(UNVERIFIED_MARK)
    escaped = compose("&" * 5000, verified=True, max_chars=3000)
    assert len(escaped) <= 3000
    assert escaped.endswith("&amp;…")  # 途中のエスケープを残さない


def test_numbers_not_in_tool_results_get_the_unverified_mark(qa_settings, store):
    provider = ScriptedProvider(
        ChatResult(available=True, tool_calls=[call("get_latest")]),
        ChatResult(available=True, text="吸気は 31.5 で、排気は 48.0 です。"),
    )
    posted = Posted()
    target = handler(qa_settings, provider, store, post=posted)
    target.offer(mention())
    drain(target)
    [(_, _, text)] = posted.items
    assert text.startswith("吸気は 31.5 で、排気は 48.0 です。")  # 答えは捨てない
    assert text.endswith(UNVERIFIED_MARK)


def test_numbers_in_tool_results_are_verified():
    allowed = number_values('{"value": 31.5, "slope": -0.25, "ts_local": "2026-08-25 09:00:00"}')
    assert invented_numbers("31.50 ℃、−0.25 ℃/分、2026-08-25 09:00", allowed) == []
    assert invented_numbers("0.25 ℃/分", allowed) == ["0.25"]  # 符号は含めて比べる


# ---------------------------------------------------------------- LLM が使えない・上限


def test_an_unavailable_llm_gets_only_the_fixed_text(qa_settings, store):
    provider = ScriptedProvider(ChatResult(available=False, reason="HTTP 500"))
    posted = Posted()
    target = handler(qa_settings, provider, store, post=posted)
    target.offer(mention())
    drain(target)
    assert [text for _, _, text in posted.items] == [UNAVAILABLE_TEXT]


def test_an_empty_answer_gets_the_fixed_text(qa_settings, store):
    provider = ScriptedProvider(ChatResult(available=True, text="  "))
    posted = Posted()
    target = handler(qa_settings, provider, store, post=posted)
    target.offer(mention())
    drain(target)
    assert [text for _, _, text in posted.items] == [NO_ANSWER_TEXT]


def test_the_daily_limit_stops_calling_the_llm(qa_settings, store):
    provider = ScriptedProvider(
        ChatResult(available=True, tool_calls=[call("get_latest")]),
        ChatResult(available=True, text="はい"),
    )
    target = answerer(provider, qa_settings, store, budget=1)
    result = target.answer("x")
    assert result.kind == "unavailable"
    assert len(provider.requests) == 1  # 2 回目（往復の続き）を呼ばない
    again = target.answer("y")
    assert again.kind == "unavailable"
    assert len(provider.requests) == 1  # 上限の後は 1 回も呼ばない


def test_the_daily_limit_resets_with_the_date():
    days = [date(2026, 8, 25)]
    budget = DailyCallBudget(1, today=lambda: days[0])
    assert budget.try_acquire()
    assert not budget.try_acquire()
    days[0] = date(2026, 8, 26)
    assert budget.try_acquire()


# ---------------------------------------------------------------- ログ


def test_logs_carry_no_question_answer_or_ids(qa_settings, store, caplog):
    provider = ScriptedProvider(
        ChatResult(available=True, tool_calls=[call("get_latest")]),
        ChatResult(available=True, text="秘密の答え 31.5"),
    )
    target = handler(qa_settings, provider, store)
    with caplog.at_level(logging.DEBUG):
        target.process(
            Job(kind="question", channel=CHANNEL, thread_ts="1.1", question="秘密の質問")
        )
    text = caplog.text + json.dumps([r.__dict__.get("fields") for r in caplog.records], default=str)
    for secret in ("秘密の質問", "秘密の答え", CHANNEL, OWNER):
        assert secret not in text
    record = next(r for r in caplog.records if r.getMessage() == "Slack のメンションに返した")
    fields = record.__dict__["fields"]
    assert fields["tools"] == ["get_latest"]
    assert fields["round_trips"] == 1
    assert fields["question_chars"] == len("秘密の質問")


def test_a_failed_post_is_logged_without_raising(qa_settings, store, caplog):
    provider = ScriptedProvider(ChatResult(available=True, text="はい"))

    def broken(channel, thread_ts, text):
        raise DeliveryError("slack: chat.postMessage: channel_not_found")

    target = handler(qa_settings, provider, store, post=broken)
    with caplog.at_level(logging.WARNING, logger="coldaisle.slackbot"):
        target.process(Job(kind="question", channel=CHANNEL, thread_ts="1.1", question="x"))
    assert any(
        r.__dict__.get("fields", {}).get("reason") == "slack: chat.postMessage: channel_not_found"
        for r in caplog.records
    )


# ---------------------------------------------------------------- Slack の Web API


def test_slack_api_posts_into_the_thread_without_unfurling():
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/auth.test"):
            return httpx.Response(200, json={"ok": True, "user_id": BOT})
        if request.url.path.endswith("/apps.connections.open"):
            return httpx.Response(200, json={"ok": True, "url": "wss://example.invalid/link?t=x"})
        return httpx.Response(200, json={"ok": True, "ts": "2.2"})

    api = SlackApi(BOT_TOKEN, client=httpx.Client(transport=httpx.MockTransport(transport)))
    assert api.post_message(CHANNEL, "本文", thread_ts="1.1", unfurl=False) == "2.2"
    body = json.loads(seen[-1].content)
    assert body == {
        "channel": CHANNEL,
        "text": "本文",
        "thread_ts": "1.1",
        "unfurl_links": False,
        "unfurl_media": False,
    }
    # 今までの呼び方は今までと同じ要求
    api.post_message(CHANNEL, "本文")
    assert json.loads(seen[-1].content) == {"channel": CHANNEL, "text": "本文"}
    assert api.bot_user_id() == BOT
    assert api.open_socket_url().startswith("wss://")
    assert BOT_TOKEN not in repr(api)


def test_slack_api_errors_do_not_carry_the_socket_url():
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "url": "https://not-a-websocket"})

    api = SlackApi(APP_TOKEN, client=httpx.Client(transport=httpx.MockTransport(transport)))
    with pytest.raises(DeliveryError) as raised:
        api.open_socket_url()
    assert "not-a-websocket" not in str(raised.value)


# ---------------------------------------------------------------- Socket Mode


class FakeConnection:
    def __init__(self, frames: list, *, log: list) -> None:
        self.frames = list(frames)
        self.log = log
        self.closed = False

    def recv(self, timeout=None):
        if not self.frames:
            raise ConnectionError("closed by peer")
        frame = self.frames.pop(0)
        if isinstance(frame, BaseException):
            raise frame
        return frame

    def send(self, message: str) -> None:
        self.log.append(("ack", json.loads(message)))

    def close(self) -> None:
        self.closed = True


def envelope(envelope_id: str, event: dict) -> str:
    return json.dumps(
        {
            "envelope_id": envelope_id,
            "type": "events_api",
            "accepts_response_payload": False,
            "payload": {"type": "event_callback", "event": event},
        }
    )


def test_socket_mode_acks_first_then_hands_over_the_event_and_reconnects():
    log: list = []
    stop = threading.Event()
    sessions = [
        [
            json.dumps({"type": "hello"}),
            TimeoutError(),
            envelope("e1", mention()),
            json.dumps({"envelope_id": "e2", "type": "slash_commands", "payload": {}}),
            "not json",
            json.dumps({"type": "disconnect", "reason": "refresh_requested"}),
        ],
        [json.dumps({"type": "hello"}), envelope("e3", mention(ts="9.9"))],
    ]
    connections: list[FakeConnection] = []
    waits: list[float] = []

    def connect(url: str) -> FakeConnection:
        assert url == "wss://example.invalid/socket"
        connection = FakeConnection(sessions.pop(0), log=log)
        connections.append(connection)
        return connection

    def on_event(event: dict, payload: dict) -> None:
        log.append(("event", event["ts"]))
        if event["ts"] == "9.9":
            stop.set()

    original_wait = stop.wait

    def wait(timeout=None):
        waits.append(timeout)
        return original_wait(0)

    stop.wait = wait  # type: ignore[method-assign]
    SocketModeClient(
        open_url=lambda: "wss://example.invalid/socket",
        connect=connect,
        on_event=on_event,
        backoff=Backoff(2.0, 300.0),
        stop=stop,
        idle_reconnect_s=900.0,
    ).run()
    assert log == [
        ("ack", {"envelope_id": "e1"}),
        ("event", mention()["ts"]),
        ("ack", {"envelope_id": "e2"}),  # 答えない種類も ack する
        ("ack", {"envelope_id": "e3"}),
        ("event", "9.9"),
    ]
    assert all(c.closed for c in connections)
    assert waits == [2.0]  # disconnect の後も間隔をあけてつなぎ直す


def test_socket_mode_backs_off_when_it_cannot_connect(caplog):
    stop = threading.Event()
    waits: list[float] = []
    attempts = [0]

    def open_url() -> str:
        attempts[0] += 1
        raise DeliveryError("slack: apps.connections.open: invalid_auth")

    def wait(timeout=None):
        waits.append(timeout)
        if len(waits) == 5:
            stop.set()
        return stop.is_set()

    stop.wait = wait  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="coldaisle.slackbot"):
        SocketModeClient(
            open_url=open_url,
            connect=lambda url: pytest.fail("つなげないのに接続しない"),
            on_event=lambda event, payload: None,
            backoff=Backoff(2.0, 10.0),
            stop=stop,
            idle_reconnect_s=900.0,
        ).run()
    assert waits == [2.0, 4.0, 8.0, 10.0, 10.0]
    reasons = {r.__dict__["fields"]["reason"] for r in caplog.records}
    assert reasons == {"slack: apps.connections.open: invalid_auth"}


# ---------------------------------------------------------------- Claude API の tool_use


def test_anthropic_speaks_tool_use_only_when_allowed():
    from coldaisle.ai.claude_provider import AnthropicProvider
    from coldaisle.ai.claude_provider import DailyCallBudget as ClaudeBudget

    settings = AiSettings.from_yaml(CONFIG_DIR / "ai.yaml")
    assert settings.anthropic is not None
    seen: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "m",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "調べます"},
                    {"type": "tool_use", "id": "toolu_01", "name": "get_latest", "input": {}},
                ],
            },
        )

    def provider(allow: bool) -> AnthropicProvider:
        return AnthropicProvider(
            model="m",
            settings=settings,
            limits=settings.anthropic,
            api_key="sk-ant-test-fake",  # pragma: allowlist secret - テストのダミー値
            budget=ClaudeBudget(100),
            client=httpx.Client(transport=httpx.MockTransport(transport)),
            sleep=lambda _: None,
            allow_tools=allow,
        )

    history = [
        ChatMessage(role="system", content="短く"),
        ChatMessage(role="user", content="吸気は？"),
        ChatMessage(role="assistant", content="", tool_calls=(call("get_stats", {"metric": "a"}),)),
        ChatMessage(role="tool", content='{"max": 31.5}', tool_call_id="call_1"),
    ]
    # 既定（日次レポート・アラートの説明）は今のまま呼ばない（0143 §2.3）
    assert provider(False).chat(history, tools=TOOL_DEFINITIONS).available is False
    assert seen == []

    result = provider(True).chat(history, tools=TOOL_DEFINITIONS, tool_choice="none")
    assert result.available is True
    assert result.text == "調べます"
    assert result.tool_calls == [
        {
            "id": "toolu_01",
            "type": "function",
            "function": {"name": "get_latest", "arguments": "{}"},
        }
    ]
    body = seen[0]
    assert body["system"] == "短く"
    assert [t["name"] for t in body["tools"]] == list(ALLOWED_TOOLS)
    assert body["tool_choice"] == {"type": "none"}
    assert body["messages"] == [
        {"role": "user", "content": "吸気は？"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_1", "name": "get_stats", "input": {"metric": "a"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": '{"max": 31.5}'}
            ],
        },
    ]


def test_openai_compatible_sends_tool_turns():
    from coldaisle.ai import OpenAiCompatibleProvider

    settings = AiSettings.from_yaml(CONFIG_DIR / "ai.yaml")
    seen: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "はい"}}]})

    provider = OpenAiCompatibleProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        settings=settings,
        client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    provider.chat(
        [
            ChatMessage(role="user", content="x"),
            ChatMessage(role="assistant", content="", tool_calls=(call("get_latest"),)),
            ChatMessage(role="tool", content="{}", tool_call_id="call_1"),
        ],
        tools=TOOL_DEFINITIONS,
        tool_choice="none",
    )
    body = seen[0]
    assert body["tool_choice"] == "none"
    assert body["messages"][0] == {"role": "user", "content": "x"}  # ツールの欄の無い発話は今のまま
    assert body["messages"][1]["tool_calls"][0]["id"] == "call_1"
    assert body["messages"][2] == {"role": "tool", "content": "{}", "tool_call_id": "call_1"}


# ---------------------------------------------------------------- 起動


def test_main_refuses_to_start_without_tokens(monkeypatch, caplog):
    monkeypatch.delenv("COLDAISLE_SLACK_APP_TOKEN", raising=False)
    monkeypatch.setenv("COLDAISLE_SLACK_BOT_TOKEN", BOT_TOKEN)
    with caplog.at_level(logging.ERROR):
        assert cli.main(["--ai", str(CONFIG_DIR / "ai.yaml")]) == cli.EXIT_CONFIG
    assert BOT_TOKEN not in caplog.text


def test_main_refuses_swapped_tokens(monkeypatch, caplog):
    monkeypatch.setenv("COLDAISLE_SLACK_APP_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("COLDAISLE_SLACK_BOT_TOKEN", APP_TOKEN)
    with caplog.at_level(logging.ERROR):
        assert cli.main(["--ai", str(CONFIG_DIR / "ai.yaml")]) == cli.EXIT_CONFIG
    assert BOT_TOKEN not in caplog.text and APP_TOKEN not in caplog.text


def test_main_refuses_to_start_without_the_slack_qa_block(monkeypatch, tmp_path):
    monkeypatch.setenv("COLDAISLE_SLACK_APP_TOKEN", APP_TOKEN)
    monkeypatch.setenv("COLDAISLE_SLACK_BOT_TOKEN", BOT_TOKEN)
    import yaml

    loaded = yaml.safe_load((CONFIG_DIR / "ai.yaml").read_text(encoding="utf-8"))
    del loaded["slack_qa"]
    path = tmp_path / "ai.yaml"
    path.write_text(yaml.safe_dump(loaded), encoding="utf-8")
    assert cli.main(["--ai", str(path)]) == cli.EXIT_CONFIG


def test_the_console_script_is_registered():
    import tomllib

    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    assert scripts["coldaisle-slackbot"] == "coldaisle.slackbot.cli:main"


# ---------------------------------------------------------------- import の走査


FORBIDDEN_PREFIXES = ("coldaisle.control", "coldaisle.ingest")


def test_the_slackbot_does_not_import_control_or_ingest_directly():
    found: list[str] = []
    for path in sorted(SLACKBOT_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            found += [f"{path.name}: {n}" for n in names if n.startswith(FORBIDDEN_PREFIXES)]
            found += [
                f"{path.name}: {n}"
                for n in names
                if n.split(".")[0] in {"subprocess", "shutil", "ctypes", "serial"}
            ]
    assert found == []


def test_importing_the_slackbot_pulls_in_neither_control_nor_ingest():
    """推移的な import でも制御と取り込みを読み込まない（別のプロセスで確かめる）。"""
    code = (
        "import sys, coldaisle.slackbot.cli\n"
        f"bad = sorted(m for m in sys.modules if m.startswith({FORBIDDEN_PREFIXES!r}))\n"
        "print(','.join(bad))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120, check=True
    )
    assert completed.stdout.strip() == ""


# ---------------------------------------------------------------- つなぎ直し（0168 §2.1）


def run_sessions(sessions: list[list], *, idle_s: float = 900.0, clock=None) -> list[str]:
    """`sessions` を 1 つずつ接続として渡し、使い切ったら止める。接続に使った URL を返す。"""
    stop = threading.Event()
    urls: list[str] = []
    counter = [0]

    def open_url() -> str:
        counter[0] += 1
        return f"wss://example.invalid/socket/{counter[0]}"

    def connect(url: str) -> FakeConnection:
        urls.append(url)
        if not sessions:
            stop.set()
            return FakeConnection([], log=[])
        return FakeConnection(sessions.pop(0), log=[])

    stop.wait = lambda timeout=None: stop.is_set()  # type: ignore[method-assign]
    SocketModeClient(
        open_url=open_url,
        connect=connect,
        on_event=lambda event, payload: None,
        backoff=Backoff(2.0, 300.0),
        stop=stop,
        idle_reconnect_s=idle_s,
        monotonic=clock or (lambda: 0.0),
    ).run()
    return urls


@pytest.mark.parametrize("reason", ["warning", "refresh_requested", "link_disabled", None])
def test_any_disconnect_fetches_a_new_url(reason):
    message = {"type": "disconnect"} if reason is None else {"type": "disconnect", "reason": reason}
    urls = run_sessions([[json.dumps({"type": "hello"}), json.dumps(message)]])
    assert urls[:2] == ["wss://example.invalid/socket/1", "wss://example.invalid/socket/2"]


def test_a_closed_websocket_fetches_a_new_url():
    # 受信が尽きると FakeConnection は ConnectionError（閉じた）を投げる
    urls = run_sessions([[json.dumps({"type": "hello"})]])
    assert urls[:2] == ["wss://example.invalid/socket/1", "wss://example.invalid/socket/2"]


def test_a_silent_connection_is_replaced_after_the_idle_time():
    """警告なしに入れ替えられた接続を待ち続けない（`idle_reconnect_s`）。"""
    now = [0.0]

    def clock() -> float:
        return now[0]

    class Silent(FakeConnection):
        def recv(self, timeout=None):
            if self.frames:
                return self.frames.pop(0)
            now[0] += 1.0
            raise TimeoutError

    stop = threading.Event()
    urls: list[str] = []

    def connect(url: str) -> FakeConnection:
        urls.append(url)
        if len(urls) == 2:
            stop.set()
        return Silent([json.dumps({"type": "hello"})], log=[])

    stop.wait = lambda timeout=None: stop.is_set()  # type: ignore[method-assign]
    SocketModeClient(
        open_url=lambda: f"wss://example.invalid/socket/{len(urls) + 1}",
        connect=connect,
        on_event=lambda event, payload: None,
        backoff=Backoff(2.0, 300.0),
        stop=stop,
        idle_reconnect_s=5.0,
        monotonic=clock,
    ).run()
    assert urls == ["wss://example.invalid/socket/1", "wss://example.invalid/socket/2"]
    assert now[0] == 5.0  # 何も受けない時間がちょうど設定の時間に達したところで


def test_the_idle_time_is_in_the_configuration(qa_settings):
    assert qa_settings.idle_reconnect_s >= 300.0  # ping の間隔（約 20 s）より十分長い


# ---------------------------------------------------------------- 再配信の重複（0168 §2.1）


def redelivery(envelope_id: str, event_id: str, event: dict, attempt: int) -> str:
    return json.dumps(
        {
            "envelope_id": envelope_id,
            "type": "events_api",
            "retry_attempt": attempt,
            "retry_reason": "timeout" if attempt else "",
            "payload": {"type": "event_callback", "event_id": event_id, "event": event},
        }
    )


def test_a_retried_event_is_answered_once(qa_settings, store):
    provider = ScriptedProvider(ChatResult(available=True, text="はい"))
    posted = Posted()
    target = handler(qa_settings, provider, store, post=posted)
    stop = threading.Event()
    frames = [
        json.dumps({"type": "hello"}),
        redelivery("e1", "Ev0001", mention(), 0),
        redelivery("e2", "Ev0001", mention(), 1),  # 同じ event_id（ACK が届かなかった）
        redelivery("e3", "Ev0002", mention(), 2),  # 別の event_id でも同じ (チャンネル, ts)
        redelivery("e4", "Ev0001", mention(ts="2.2"), 3),  # 同じ event_id で別の ts
        redelivery("e5", "Ev0003", mention(ts="3.3"), 0),  # 新しいメンション
    ]
    log: list = []

    def connect(url: str) -> FakeConnection:
        stop.set()  # この接続を使い切ったら止める
        return FakeConnection(frames, log=log)

    stop.wait = lambda timeout=None: True  # type: ignore[method-assign]
    stop.is_set = lambda: len(log) >= 5  # type: ignore[method-assign]
    SocketModeClient(
        open_url=lambda: "wss://example.invalid/socket",
        connect=connect,
        on_event=target.offer,
        backoff=Backoff(2.0, 300.0),
        stop=stop,
        idle_reconnect_s=900.0,
    ).run()
    assert [entry[1]["envelope_id"] for entry in log] == ["e1", "e2", "e3", "e4", "e5"]
    drain(target)
    assert [thread for _, thread, _ in posted.items] == [mention()["ts"], "3.3"]
    assert len(provider.requests) == 2


def test_a_mention_being_answered_is_not_queued_again(qa_settings, store):
    """処理中（待ち行列にいる間）の再配信にも答えない。"""
    provider = ScriptedProvider(ChatResult(available=True, text="はい"))
    posted = Posted()
    target = handler(qa_settings, provider, store, post=posted)
    target.offer(mention(), {"event_id": "Ev0001"})
    assert target._queue.qsize() == 1
    target.offer(mention(), {"event_id": "Ev0001", "retry_attempt": 1})
    assert target._queue.qsize() == 1
    drain(target)
    target.offer(mention(), {"event_id": "Ev0001"})  # 処理済みの後も
    drain(target)
    assert len(posted.items) == 1


# ---------------------------------------------------------------- 外へ出す欄を絞る（0168 §2.3）


def test_describe_system_reaches_the_llm_without_devices(qa_settings, store):
    from coldaisle.store import DeviceRecord, SensorRecord

    store.record_hello(
        DeviceRecord(device_id="dev-unique-0001", fw="9.8.7", interval_ms=2_500),
        [
            SensorRecord(
                channel="rear_exhaust",
                kind="ds18b20",
                gpio=7,
                rom="28FFFFFFFFFFFF05",
                resolution=12,
            )
        ],
        at_ms=NOW_MS,
    )
    provider = ScriptedProvider(
        ChatResult(available=True, tool_calls=[call("describe_system")]),
        ChatResult(available=True, text="はい"),
    )
    tools = registry(store)
    answerer(provider, qa_settings, open_tools=lambda: tools).answer("構成は？")
    [message] = tool_messages(provider)
    body = json.loads(message.content)
    assert "devices" not in body
    for secret in ("dev-unique-0001", "9.8.7", "FF05", "gpio", "resolution", "ds18b20"):
        assert secret not in message.content
    assert {"metrics", "derived", "thresholds"} <= set(body)
    # Workspace 向けの describe_system は変えない
    assert (
        tools.call("describe_system")["devices"][0]["device_id"] == "<data>dev-unique-0001</data>"
    )


def test_get_stats_period_and_extreme_times_reach_the_llm_as_local_times(qa_settings, store):
    provider = ScriptedProvider(
        ChatResult(
            available=True,
            tool_calls=[
                call(
                    "get_stats",
                    {"metric": "air.room", "from": NOW_MS - 3_600_000, "to": NOW_MS},
                )
            ],
        ),
        ChatResult(available=True, text="はい"),
    )
    answerer(provider, qa_settings, store).answer("いちばん高かったのはいつ？")
    [message] = tool_messages(provider)
    body = json.loads(message.content)
    assert body["max_ts_ms"] == NOW_MS - 1_000
    local = datetime.fromtimestamp((NOW_MS - 1_000) / 1000, tz=TOKYO)
    assert body["max_ts_local"] == local.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------ 2 往復目以降の会話の形（0168 §2.5）


def two_rounds_then_text(kind: str):
    """1 往復目 get_latest、2 往復目 get_stats、3 回目に文を返す相手。受けた要求の本文を返す。"""
    seen: list[dict] = []

    def openai(index: int) -> dict:
        if index == 0:
            calls = [call("get_latest", call_id="call_a")]
        elif index == 1:
            calls = [call("get_stats", {"metric": "air.room", "window": "1h"}, call_id="call_b")]
        else:
            return {"choices": [{"message": {"content": "室温は 24.25 です。"}}]}
        return {"choices": [{"message": {"content": "", "tool_calls": calls}}]}

    def anthropic(index: int) -> dict:
        if index == 0:
            content = [{"type": "tool_use", "id": "toolu_a", "name": "get_latest", "input": {}}]
        elif index == 1:
            content = [
                {"type": "text", "text": "続けて調べます"},
                {
                    "type": "tool_use",
                    "id": "toolu_b",
                    "name": "get_stats",
                    "input": {"metric": "air.room", "window": "1h"},
                },
            ]
        else:
            content = [{"type": "text", "text": "室温は 24.25 です。"}]
        stop = "end_turn" if index >= 2 else "tool_use"
        return {"model": "m", "stop_reason": stop, "content": content}

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = (openai if kind == "openai" else anthropic)(len(seen) - 1)
        return httpx.Response(200, json=body)

    return seen, httpx.Client(transport=httpx.MockTransport(transport))


def test_openai_compatible_carries_earlier_tool_results_into_later_rounds(qa_settings, store):
    from coldaisle.ai import OpenAiCompatibleProvider

    seen, client = two_rounds_then_text("openai")
    provider = OpenAiCompatibleProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        settings=AiSettings.from_yaml(CONFIG_DIR / "ai.yaml"),
        client=client,
    )
    result = answerer(provider, qa_settings, store).answer("室温は？")
    assert result.kind == "answered" and result.round_trips == 2
    assert len(seen) == 3
    roles = [m["role"] for m in seen[2]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    first_call, first_result = seen[2]["messages"][2], seen[2]["messages"][3]
    assert first_call["tool_calls"][0]["id"] == "call_a"
    assert first_result["tool_call_id"] == "call_a" and "24.25" in first_result["content"]
    second_call, second_result = seen[2]["messages"][4], seen[2]["messages"][5]
    assert second_call["tool_calls"][0]["function"]["name"] == "get_stats"
    assert second_result["tool_call_id"] == "call_b"
    assert json.loads(second_result["content"])["max"] == 24.25
    # 2 往復目の要求にも 1 往復目の結果が載っている
    assert [m["role"] for m in seen[1]["messages"]] == ["system", "user", "assistant", "tool"]


def test_anthropic_carries_earlier_tool_results_into_later_rounds(qa_settings, store):
    from coldaisle.ai.claude_provider import AnthropicProvider
    from coldaisle.ai.claude_provider import DailyCallBudget as ClaudeBudget

    settings = AiSettings.from_yaml(CONFIG_DIR / "ai.yaml")
    assert settings.anthropic is not None
    seen, client = two_rounds_then_text("anthropic")
    provider = AnthropicProvider(
        model="m",
        settings=settings,
        limits=settings.anthropic,
        api_key="sk-ant-test-fake",  # pragma: allowlist secret - テストのダミー値
        budget=ClaudeBudget(100),
        client=client,
        sleep=lambda _: None,
        allow_tools=True,
    )
    result = answerer(provider, qa_settings, store).answer("室温は？")
    assert result.kind == "answered" and result.round_trips == 2
    assert len(seen) == 3
    messages = seen[2]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    assert messages[1]["content"] == [
        {"type": "tool_use", "id": "toolu_a", "name": "get_latest", "input": {}}
    ]
    [first] = messages[2]["content"]
    assert first["type"] == "tool_result" and first["tool_use_id"] == "toolu_a"
    assert "24.25" in first["content"]
    assert messages[3]["content"][0] == {"type": "text", "text": "続けて調べます"}
    assert messages[3]["content"][1]["id"] == "toolu_b"
    [second] = messages[4]["content"]
    assert second["tool_use_id"] == "toolu_b"
    assert json.loads(second["content"])["max"] == 24.25
    assert "system" not in [m["role"] for m in messages]
    assert seen[2]["system"].startswith("あなたは")


# ---------------------------------------------------------------- 設定の誤り（PR #376 の指摘）


@pytest.mark.parametrize(
    "content",
    [None, "timeout_s: [\n", "timeout_s: 1\n", "- just a list\n"],
    ids=["missing", "broken-yaml", "invalid", "not-a-mapping"],
)
def test_an_unreadable_ai_config_exits_with_the_config_code(monkeypatch, tmp_path, caplog, content):
    monkeypatch.setenv("COLDAISLE_SLACK_APP_TOKEN", APP_TOKEN)
    monkeypatch.setenv("COLDAISLE_SLACK_BOT_TOKEN", BOT_TOKEN)
    path = tmp_path / "ai.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert cli.main(["--ai", str(path)]) == cli.EXIT_CONFIG
    assert BOT_TOKEN not in caplog.text and APP_TOKEN not in caplog.text
    assert "timeout_s: [" not in caplog.text  # 中身（値）を出さない


# ------------------------------------------------ 時刻帯と日の境界（0168 §2.3）

# 2026-08-25T23:30:00Z = 2026-08-26T08:30:00+09:00。UTC と東京で日付が違う時刻
LATE_UTC_MS = 1_787_700_600_000


@pytest.fixture
def os_zone_differs(monkeypatch):
    """OS の時刻帯を東京と違うもの（ロサンゼルス）にする。終わったら戻す。"""
    import time as time_module

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time_module.tzset()
    yield
    monkeypatch.undo()
    time_module.tzset()


def test_yesterday_follows_the_configured_zone_not_the_os(qa_settings, os_zone_differs):
    basis = time_basis(LATE_UTC_MS, TOKYO)
    today = datetime(2026, 8, 26, tzinfo=TOKYO)
    yesterday = datetime(2026, 8, 25, tzinfo=TOKYO)
    assert "時刻帯: Asia/Tokyo" in basis
    assert "現在時刻: 2026-08-26 08:30:00" in basis
    assert (
        f"昨日: from={int(yesterday.timestamp() * 1000)} to={int(today.timestamp() * 1000)}"
        in basis
    )
    assert "2026-08-25 の 0〜24 時" in basis
    # OS（ロサンゼルス）ではまだ 8/25 の 16:30。OS の時刻帯なら昨日は 8/24 になる
    assert datetime.fromtimestamp(LATE_UTC_MS / 1000).day == 25


def test_the_system_prompt_and_local_times_use_the_configured_zone(
    qa_settings, store, os_zone_differs
):
    provider = ScriptedProvider(
        ChatResult(available=True, tool_calls=[call("get_latest")]),
        ChatResult(available=True, text="2026-08-25 08:59:59 に 31.5 でした。"),
    )
    result = answerer(provider, qa_settings, store, now_ms=LATE_UTC_MS).answer("x")
    system = provider.requests[0]["messages"][0].content
    assert "時刻帯: Asia/Tokyo" in system
    assert "2026-08-26 08:30:00" in system
    [message] = tool_messages(provider)
    body = json.loads(message.content)
    # NOW_MS - 1000 = 2026-08-24T23:59:59Z = 08:59:59 JST（OS のロサンゼルスなら前日の 16:59:59）
    assert body["ts_local"] == "2026-08-25 08:59:59"
    # 整形した時刻の数字は検証済み（0168 §2.3）
    assert result.verified is True


def test_an_unreadable_zone_means_no_answers(tmp_path):
    broken = tmp_path / "report.yaml"
    broken.write_text("timezone: Not/AZone\n", encoding="utf-8")
    assert cli._zone(broken) is None
    assert cli._zone(CONFIG_DIR / "report.yaml") == ZoneInfo("Asia/Tokyo")


# ------------------------------------------------ 呼び出しの回数（0168 §2.3 / §2.5）


def test_one_question_calls_the_provider_at_most_five_times_and_each_counts(qa_settings, store):
    """4 往復の後の答えを作る呼び出しを含めて 5 回。1 日の上限をそれぞれ 1 回と数える。"""
    looping = [ChatResult(available=True, tool_calls=[call("get_latest")]) for _ in range(4)]
    provider = ScriptedProvider(*looping, ChatResult(available=True, text="はい"))
    budget = DailyCallBudget(100, today=lambda: date(2026, 8, 25))
    result = answerer(provider, qa_settings, store, budget=budget).answer("x")
    assert result.kind == "answered"
    assert len(provider.requests) == 5
    assert budget.used == 5

    # 上限が 4 なら、答えを作る 5 回目を呼ばない
    provider = ScriptedProvider(
        *[ChatResult(available=True, tool_calls=[call("get_latest")]) for _ in range(4)],
        ChatResult(available=True, text="はい"),
    )
    short = DailyCallBudget(4, today=lambda: date(2026, 8, 25))
    result = answerer(provider, qa_settings, store, budget=short).answer("x")
    assert result.kind == "unavailable"
    assert len(provider.requests) == 4
    assert short.used == 4


def test_the_tool_calls_of_one_question_are_capped(qa_settings, store):
    """1 応答 8 個まで・1 質問の合計 16 個まで。超えた分は実行せずエラーの結果だけ。"""
    assert qa_settings.max_tool_calls_per_round == 8
    assert qa_settings.max_tool_calls_per_question == 16
    rounds = [
        ChatResult(
            available=True,
            tool_calls=[call("get_latest", call_id=f"r{r}c{c}") for c in range(9)],
        )
        for r in range(3)
    ]
    provider = ScriptedProvider(*rounds, ChatResult(available=True, text="はい"))
    tools = registry(store)
    executed: list[str] = []
    original = tools.call

    def spy(name, arguments=None):
        executed.append(name)
        return original(name, arguments)

    tools.call = spy  # type: ignore[method-assign]
    answerer(provider, qa_settings, open_tools=lambda: tools).answer("x")
    assert len(executed) == 16
    results = tool_messages(provider)
    assert len(results) == 27  # 呼び出しごとに 1 つの結果
    errors = [json.loads(m.content).get("error", "") for m in results]
    # 各応答の 9 個目は応答の上限。2 回の応答で 16 個を実行したので、3 回目は 1 質問の上限
    assert errors[8].startswith("1 回の応答")
    assert errors[17].startswith("1 回の応答")
    assert all(e.startswith("1 つの質問") for e in errors[18:26])
    assert errors[26].startswith("1 回の応答")
    assert sum(1 for e in errors if not e) == 16


@pytest.mark.parametrize("flag", ["--metrics", "--rules", "--quality-rules"])
@pytest.mark.parametrize("content", [None, "metrics: [\n"], ids=["missing", "broken-yaml"])
def test_an_unreadable_tool_config_exits_with_the_config_code(
    monkeypatch, tmp_path, caplog, flag, content
):
    """metrics / rules / quality の設定の誤りも終了コード 2（unit は再起動しない。PR #376 の指摘）。

    Slack へつなぐ前に止まる（auth.test を呼ばない）。
    """
    monkeypatch.setenv("COLDAISLE_SLACK_APP_TOKEN", APP_TOKEN)
    monkeypatch.setenv("COLDAISLE_SLACK_BOT_TOKEN", BOT_TOKEN)

    def no_network(self):
        raise AssertionError("設定の誤りでは Slack へつながない")

    monkeypatch.setattr(cli.SlackApi, "bot_user_id", no_network)
    path = tmp_path / "broken.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert cli.main(["--ai", str(CONFIG_DIR / "ai.yaml"), flag, str(path)]) == cli.EXIT_CONFIG
    assert BOT_TOKEN not in caplog.text and APP_TOKEN not in caplog.text
