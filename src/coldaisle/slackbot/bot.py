"""メンションを受け付けて答えを返す（決定記録 0168 §2.1〜§2.5）。

`offer()` は受信のスレッドから呼ぶ（ack の後）。ここでは**速く**ふるい分けて待ち行列へ積むだけで、
答えは別のスレッド（`run_worker()`）で作り、メンションのスレッドへ `chat.postMessage` で返す。

答えないもの（返事もしない。何が許可されているかを外に見せない。0168 §2.2）:

- `app_mention` 以外・編集（`edited` / `subtype`）・ボットの投稿（`bot_id`）・ボット自身
- DM（`D…` のチャンネル）
- 許可リストのチャンネル**かつ**ユーザーに当たらないもの（どちらかが空なら全部）
- 同じメンションの 2 度目（Slack の再送）・本文の空のメンション

**質問・答えの本文と ID はログに出さない**
（長さ・ツールの名前・往復の数・所要時間だけ。0168 §2.5）。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from coldaisle import logs
from coldaisle.ai.provider import SlackQaSettings
from coldaisle.notify.notifiers import DeliveryError
from coldaisle.slackbot.policy import Allowlist, UserRateLimiter
from coldaisle.slackbot.qa import Answer, QuestionAnswerer
from coldaisle.slackbot.reply import (
    NO_ANSWER_TEXT,
    UNAVAILABLE_TEXT,
    WAIT_TEXT,
    compose,
    fixed,
)

LOGGER = logging.getLogger("coldaisle.slackbot")

Post = Callable[[str, str, str], object]
"""返す。引数は (チャンネル ID, スレッドの ts, 本文)。"""

_SEEN_LIMIT = 512
"""再送を見分けるために覚えておくメンションの数（メモリを際限なく使わない）。"""

_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^<>]*)?>")


@dataclass(frozen=True)
class Job:
    kind: Literal["question", "wait"]
    channel: str
    thread_ts: str
    question: str = ""


class MentionHandler:
    """ふるい分けと答えの作成。

    待ち行列は 1 本で、答えるスレッドも 1 本（DB の接続を共有しない）。
    """

    def __init__(
        self,
        *,
        bot_user_id: str,
        allowlist: Allowlist,
        limiter: UserRateLimiter,
        answerer: QuestionAnswerer,
        post: Post,
        settings: SlackQaSettings,
    ) -> None:
        self._bot_user_id = bot_user_id
        self._allowlist = allowlist
        self._limiter = limiter
        self._answerer = answerer
        self._post = post
        self._settings = settings
        self._queue: queue.Queue[Job | None] = queue.Queue(maxsize=settings.queue_size)
        self._seen_keys: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 受信のスレッド

    def offer(self, event: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        """1 つの event を受け付ける。**答えないものは黙って捨てる。**

        `payload` は Socket Mode の外側の payload で、`event_id` を持つ（再配信の重複を見分ける）。
        """
        event_id = None if payload is None else payload.get("event_id")
        job = self._screen(event, event_id if isinstance(event_id, str) and event_id else None)
        if job is None:
            return
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            LOGGER.warning(
                "答えを作る待ち行列が溢れたので、このメンションには答えない",
                extra={logs.FIELDS_KEY: {"queue_size": self._settings.queue_size}},
            )

    def _screen(self, event: dict[str, Any], event_id: str | None) -> Job | None:
        if event.get("type") != "app_mention":
            return None
        if "subtype" in event or "edited" in event or "bot_id" in event or "bot_profile" in event:
            return None
        user = event.get("user")
        channel = event.get("channel")
        ts = event.get("ts")
        text = event.get("text")
        if not all(isinstance(value, str) and value for value in (user, channel, ts)):
            return None
        assert isinstance(user, str) and isinstance(channel, str) and isinstance(ts, str)
        if user == self._bot_user_id:
            return None
        if channel.startswith("D") or event.get("channel_type") == "im":
            return None
        if not self._allowlist.allows(channel, user):
            return None
        if not self._first_time(event_id, (channel, ts)):
            return None
        question = _MENTION.sub(" ", text if isinstance(text, str) else "").strip()
        if not question:
            return None
        thread = event.get("thread_ts")
        thread_ts = thread if isinstance(thread, str) and thread else ts
        if not self._limiter.try_acquire(user):
            return Job(kind="wait", channel=channel, thread_ts=thread_ts)
        return Job(kind="question", channel=channel, thread_ts=thread_ts, question=question)

    def _first_time(self, event_id: str | None, key: tuple[str, str]) -> bool:
        """初めて見るメンションか。

        **`event_id` と (チャンネル, ts) のどちらかが既に見たものなら偽。**
        受け付けた時点で覚える（処理中・処理済みの両方を覆う。0168 §2.1）。
        Slack は ACK が届かなかった event を `retry_attempt` 付きで再配信するので、
        同じものに 2 回答えない。覚えるのは
        `_SEEN_LIMIT` 件まで（古いものから忘れる）。
        """
        with self._lock:
            if key in self._seen_keys or (event_id is not None and event_id in self._seen_ids):
                if event_id is not None:
                    _remember(self._seen_ids, event_id)
                _remember(self._seen_keys, key)
                return False
            _remember(self._seen_keys, key)
            if event_id is not None:
                _remember(self._seen_ids, event_id)
            return True

    # ------------------------------------------------------------------ 答えるスレッド

    def run_worker(self) -> None:
        """待ち行列が `None`（止める合図）を返すまで答え続ける。"""
        while True:
            job = self._queue.get()
            if job is None:
                return
            self.process(job)

    def stop_worker(self) -> None:
        """答えるスレッドを止める合図を積む。**待たない**。

        溢れていて積めなければ何もしない
        （答えるスレッドは daemon で、プロセスの終了とともに終わる）。
        """
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            LOGGER.info("待ち行列が溢れているので、答えるスレッドの終わりを待たない")

    def process(self, job: Job) -> None:
        """1 つ答えて返す。**例外を上へ投げない**（答えるスレッドを止めない）。"""
        started = time.monotonic()
        answer: Answer | None = None
        limit = self._settings.max_reply_chars
        if job.kind == "wait":
            text = fixed(WAIT_TEXT, max_chars=limit)
        else:
            answer = self._answerer.answer(job.question)
            if answer.kind == "answered":
                text = compose(answer.text, verified=answer.verified, max_chars=limit)
            elif answer.kind == "no_answer":
                text = fixed(NO_ANSWER_TEXT, max_chars=limit)
            else:
                text = fixed(UNAVAILABLE_TEXT, max_chars=limit)
        posted = True
        try:
            self._post(job.channel, job.thread_ts, text)
        except DeliveryError as error:
            posted = False
            # DeliveryError の文は宛先・API の名前・符号だけ（トークンを含まない。0147）
            LOGGER.warning(
                "Slack へ答えを返せなかった",
                extra={logs.FIELDS_KEY: {"reason": str(error)}},
            )
        except Exception as error:
            posted = False
            LOGGER.warning(
                "Slack へ答えを返せなかった",
                extra={logs.FIELDS_KEY: {"error": type(error).__name__}},
            )
        LOGGER.info(
            "Slack のメンションに返した",
            extra={
                logs.FIELDS_KEY: {
                    "kind": job.kind,
                    "outcome": None if answer is None else answer.kind,
                    "reason": None if answer is None or not answer.reason else answer.reason,
                    "question_chars": len(job.question),
                    "reply_chars": len(text),
                    "tools": [] if answer is None else list(answer.tools),
                    "round_trips": 0 if answer is None else answer.round_trips,
                    "llm_calls": 0 if answer is None else answer.llm_calls,
                    "verified": None if answer is None else answer.verified,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "posted": posted,
                }
            },
        )


def _remember[K](seen: OrderedDict[K, None], key: K) -> None:
    """覚える（新しいものとして末尾へ）。`_SEEN_LIMIT` を超えたら古いものから忘れる。"""
    seen[key] = None
    seen.move_to_end(key)
    while len(seen) > _SEEN_LIMIT:
        seen.popitem(last=False)
