"""誰に・どれだけ答えるか（決定記録 0168 §2.2 / §2.5）。

- 許可リスト: チャンネルとユーザーの ID を環境変数に置き、**両方に当たるときだけ**答える
- 1 ユーザーの質問の回数（1 分 3 質問など。値は `config/ai.yaml` の `slack_qa:`）
- 1 日の LLM の呼び出しの回数（tool loop の 1 往復を 1 回と数える）

ID はログに出さない（数だけ）。何が許可されているかを外に見せない。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date

from coldaisle import logs

LOGGER = logging.getLogger("coldaisle.slackbot")

QA_CHANNELS_ENV = "COLDAISLE_SLACK_QA_CHANNELS"
QA_USERS_ENV = "COLDAISLE_SLACK_QA_USERS"

_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{2,31}$")
"""公開・非公開のチャンネルの ID。DM（`D…`）は許可リストに置けない（0168 §2.2）。"""
_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,31}$")


@dataclass(frozen=True)
class Allowlist:
    """許可リスト。**どちらかが空なら誰にも答えない**（黙って全員に答えない。0168 §2.2）。"""

    channels: frozenset[str]
    users: frozenset[str]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Allowlist:
        """環境変数（カンマ区切り）から読む。

        **形の合わない値が 1 つでもあれば、その一覧は空にする。**

        書き間違いを黙って読み飛ばすと、意図と違う集合で答えることになる。値はログに出さない。
        """
        env = os.environ if environ is None else environ
        channels = _parse(env.get(QA_CHANNELS_ENV, ""), _CHANNEL_ID, QA_CHANNELS_ENV)
        users = _parse(env.get(QA_USERS_ENV, ""), _USER_ID, QA_USERS_ENV)
        return cls(channels=channels, users=users)

    @property
    def empty(self) -> bool:
        return not self.channels or not self.users

    def allows(self, channel: str, user: str) -> bool:
        return not self.empty and channel in self.channels and user in self.users

    def warn_if_empty(self) -> None:
        """起動時に 1 度だけ呼ぶ。空なら警告する（値は出さない。数だけ）。"""
        if self.empty:
            LOGGER.warning(
                "Slack の許可リストが空なので、どのメンションにも答えない（0168 §2.2）",
                extra={
                    logs.FIELDS_KEY: {
                        "channels": len(self.channels),
                        "users": len(self.users),
                        "env": [QA_CHANNELS_ENV, QA_USERS_ENV],
                    }
                },
            )


def _parse(raw: str, pattern: re.Pattern[str], env: str) -> frozenset[str]:
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if any(not pattern.match(item) for item in items):
        LOGGER.error(
            "Slack の許可リストに形の合わない値があるので、この一覧を空として扱う",
            extra={logs.FIELDS_KEY: {"env": env, "count": len(items)}},
        )
        return frozenset()
    return frozenset(items)


class UserRateLimiter:
    """1 ユーザーが窓の間に聞ける質問の数（0168 §2.5）。メモリの中だけで数える。"""

    def __init__(
        self,
        limit: int,
        window_s: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window_s = window_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._seen: dict[str, deque[float]] = {}

    def try_acquire(self, user: str) -> bool:
        """1 回ぶんを使う。**窓の中で上限に達していれば偽。**"""
        now = self._monotonic()
        with self._lock:
            # 窓の外の記録を捨てる（全ユーザーぶん。辞書を際限なく太らせない）
            for key in list(self._seen):
                stamps = self._seen[key]
                while stamps and now - stamps[0] >= self._window_s:
                    stamps.popleft()
                if not stamps:
                    del self._seen[key]
            stamps = self._seen.setdefault(user, deque())
            if len(stamps) >= self._limit:
                return False
            stamps.append(now)
            return True


class DailyCallBudget:
    """この経路の 1 日（ローカル時刻の日付）の LLM の呼び出しの回数（0168 §2.5）。

    tool loop の 1 往復を 1 回と数える。プロセスのメモリで数え、再起動で 0 に戻る粗い蓋
    （課金の正本は Provider の側。0143 §2.5 と同じ）。
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
                    self._warned = day
                    LOGGER.warning(
                        "Slack の返答の 1 日の上限に達した。今日はもう LLM を呼ばない",
                        extra={logs.FIELDS_KEY: {"limit": self._limit, "day": day.isoformat()}},
                    )
                return False
            self._count += 1
            return True

    @property
    def used(self) -> int:
        with self._lock:
            return self._count if self._day == self._today() else 0
