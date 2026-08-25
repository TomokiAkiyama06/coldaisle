"""通知の宛先（#20）。

**送信の失敗で取り込みを止めない。** 記録して続ける。監視の本体は
取り込み・保存・判定であり、通知はその外側にある（FR-507 と同じ考え方）。

秘匿情報は環境変数から読む。設定ファイルにもコードにも書かない（#41）。
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import httpx

from coldaisle import logs
from coldaisle.notify.models import Notification

LOGGER = logging.getLogger("coldaisle.notify")

SLACK_WEBHOOK_ENV = "COLDAISLE_SLACK_WEBHOOK"
LINE_TOKEN_ENV = "COLDAISLE_LINE_TOKEN"
LINE_TO_ENV = "COLDAISLE_LINE_TO"

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
"""LINE Messaging API の push。**LINE Notify は2025年に終了している。**

トークンと宛先が要るため、両方が環境変数に無ければこの宛先は使えない。
"""

TIMEOUT_S = 10.0


class Notifier(Protocol):
    """1つの宛先。`send` は例外を投げてよい（呼び出し側が記録して続ける）。"""

    @property
    def name(self) -> str: ...

    def send(self, notification: Notification) -> None: ...


class StdoutNotifier:
    """構造化ログへ出すだけの宛先。

    **常に使える。** 外部サービスが未設定・不通でも、通知が起きたことは残る。
    """

    name = "stdout"

    def send(self, notification: Notification) -> None:
        LOGGER.warning(
            "通知",
            extra={
                logs.FIELDS_KEY: {
                    "rule": notification.rule_id,
                    "severity": notification.severity.value,
                    "state": notification.state,
                    "metric": notification.metric,
                    "value": notification.value,
                    "text": notification.as_text(),
                }
            },
        )


class SlackNotifier:
    """Incoming Webhook へ POST する。"""

    name = "slack"

    def __init__(self, webhook_url: str, *, client: httpx.Client | None = None) -> None:
        self._webhook_url = webhook_url
        self._client = client

    @classmethod
    def from_env(cls) -> SlackNotifier | None:
        url = os.environ.get(SLACK_WEBHOOK_ENV)
        return None if not url else cls(url)

    def send(self, notification: Notification) -> None:
        payload = {"text": notification.as_text()}
        if self._client is not None:
            self._client.post(self._webhook_url, json=payload).raise_for_status()
            return
        with httpx.Client(timeout=TIMEOUT_S) as client:
            client.post(self._webhook_url, json=payload).raise_for_status()


class LineNotifier:
    """LINE Messaging API へ push する。"""

    name = "line"

    def __init__(self, token: str, to: str, *, client: httpx.Client | None = None) -> None:
        self._token = token
        self._to = to
        self._client = client

    @classmethod
    def from_env(cls) -> LineNotifier | None:
        token = os.environ.get(LINE_TOKEN_ENV)
        to = os.environ.get(LINE_TO_ENV)
        return None if not token or not to else cls(token, to)

    def send(self, notification: Notification) -> None:
        payload = {
            "to": self._to,
            "messages": [{"type": "text", "text": notification.as_text()[:4_900]}],
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        if self._client is not None:
            self._client.post(LINE_PUSH_URL, json=payload, headers=headers).raise_for_status()
            return
        with httpx.Client(timeout=TIMEOUT_S) as client:
            client.post(LINE_PUSH_URL, json=payload, headers=headers).raise_for_status()


def from_env() -> dict[str, Notifier]:
    """環境変数から使える宛先を組み立てる。

    **設定されていない宛先は作らない。** 作って毎回失敗させると、
    ログが失敗で埋まって本物の障害が見えなくなる。
    足りないことは起動時に1度だけ警告する（`Router`）。
    """
    available: dict[str, Notifier] = {"stdout": StdoutNotifier()}
    slack = SlackNotifier.from_env()
    if slack is not None:
        available["slack"] = slack
    line = LineNotifier.from_env()
    if line is not None:
        available["line"] = line
    return available
