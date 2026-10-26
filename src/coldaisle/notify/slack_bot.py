"""Slack の Bot トークンで送る宛先（#334 / 決定記録 0147 §2.1 / §2.6）。

Incoming Webhook は送った投稿の `ts` を返さず、ファイルも添付できない。Bot トークン
（`chat:write`・`files:write`）があれば本文を `chat.postMessage` で送り、返る `ts` を
グラフの依頼（`on_posted`）へ渡す。グラフを描いて上げるのは別のスレッド
（`coldaisle.alert_graph`）で、**この宛先は待たない。**

**トークンをログ・例外・repr に出さない。** `Authorization` ヘッダにだけ載せる。
失敗は `DeliveryError`（#305）にし、文は宛先の名前・API の名前・HTTP の状態コード・
Slack の `error` の符号だけにする。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any

import httpx

from coldaisle import logs
from coldaisle.notify.models import Notification
from coldaisle.notify.notifiers import TIMEOUT_S, DeliveryError, Notifier

LOGGER = logging.getLogger("coldaisle.notify")

SLACK_BOT_TOKEN_ENV = "COLDAISLE_SLACK_BOT_TOKEN"
SLACK_CHANNEL_ENV = "COLDAISLE_SLACK_CHANNEL"
"""チャンネル **ID**（`C` で始まる）。名前は改名で届かなくなるので使わない（0147 §2.1）。"""

SLACK_API_BASE = "https://slack.com/api/"

_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")
"""Slack の `error` の符号の形。これに合わない値は例外の文に入れない（何が入るか分からない）。"""

OnPosted = Callable[[Notification, str, str], object]
"""本文を送れたときに呼ぶ。引数は (通知, チャンネル ID, 投稿の ts)。

**待たないこと。** 戻り値は使わない。
"""


class SlackApi:
    """Slack の Web API の必要な分だけ。**トークンは repr に出さない。**

    Bot トークン（`xoxb-`）でも App トークン（`xapp-`。Socket Mode の接続だけ）でも同じ形で使う。
    """

    def __init__(
        self, token: str, *, client: httpx.Client | None = None, timeout_s: float = TIMEOUT_S
    ) -> None:
        if not token:
            raise ValueError("Slack のトークンが空")
        self.__token = token
        self._client = client
        self._timeout_s = timeout_s

    def __repr__(self) -> str:
        return "SlackApi(token=<hidden>)"

    def post_message(
        self, channel: str, text: str, *, thread_ts: str | None = None, unfurl: bool = True
    ) -> str:
        """`chat.postMessage`。投稿の `ts` を返す。

        `thread_ts` があればそのスレッドへ返す。`unfurl=False` はリンクの展開と、
        本文の URL・名前の自動リンク（`parse: none`）を止める（メンションへの返答。
        決定記録 0168 §2.4）。どちらも省けば今までと同じ要求になる。
        """
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        if not unfurl:
            payload["unfurl_links"] = False
            payload["unfurl_media"] = False
            payload["parse"] = "none"
        body = self._call("chat.postMessage", json_body=payload)
        ts = body.get("ts")
        if not isinstance(ts, str) or not ts:
            raise DeliveryError("slack: chat.postMessage: ts が無い")
        return ts

    def bot_user_id(self) -> str:
        """`auth.test` でこのトークンのボット自身のユーザー ID を得る（決定記録 0168 §2.2）。"""
        body = self._call("auth.test")
        user_id = body.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise DeliveryError("slack: auth.test: user_id が無い")
        return user_id

    def open_socket_url(self) -> str:
        """`apps.connections.open`（App トークン）で Socket Mode の WebSocket の URL を得る。

        **URL には接続のための券が入るので、ログ・例外・repr に出さない**（決定記録 0168 §2.1）。
        """
        body = self._call("apps.connections.open")
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith("wss://"):
            raise DeliveryError("slack: apps.connections.open: url が無い")
        return url

    def upload_to_thread(
        self,
        *,
        channel: str,
        thread_ts: str,
        filename: str,
        content: bytes,
        title: str,
        comment: str,
    ) -> None:
        """ファイルを投稿のスレッドへ上げる（`files.uploadV2` と同じ 3 段）。

        1. `files.getUploadURLExternal` で上げ先の URL と ID を得る
        2. その URL へ本体を POST する（**URL は署名入りなのでログ・例外に出さない**）
        3. `files.completeUploadExternal` でチャンネルのスレッドへ共有する
        """
        reserved = self._call(
            "files.getUploadURLExternal",
            form={"filename": filename, "length": str(len(content))},
        )
        upload_url = reserved.get("upload_url")
        file_id = reserved.get("file_id")
        if not isinstance(upload_url, str) or not isinstance(file_id, str):
            raise DeliveryError("slack: files.getUploadURLExternal: 上げ先が無い")
        self._request("upload", upload_url, content=content, authorized=False)
        self._call(
            "files.completeUploadExternal",
            form={
                "files": json.dumps([{"id": file_id, "title": title}], ensure_ascii=False),
                "channel_id": channel,
                "thread_ts": thread_ts,
                "initial_comment": comment,
            },
        )

    # ------------------------------------------------------------------ 内部

    def _call(
        self,
        method: str,
        *,
        json_body: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            method, SLACK_API_BASE + method, json_body=json_body, form=form, authorized=True
        )
        try:
            body: Any = response.json()
        except ValueError:
            raise DeliveryError(f"slack: {method}: 応答が JSON でない") from None
        if not isinstance(body, dict):
            raise DeliveryError(f"slack: {method}: 応答の形が違う")
        if body.get("ok") is not True:
            error = body.get("error")
            code = error if isinstance(error, str) and _ERROR_CODE.fullmatch(error) else "unknown"
            raise DeliveryError(f"slack: {method}: {code}")
        return body

    def _request(
        self,
        label: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        content: bytes | None = None,
        authorized: bool,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.__token}"} if authorized else {}
        try:
            if self._client is not None:
                response = self._client.post(
                    url, json=json_body, data=form, content=content, headers=headers
                )
            else:
                with httpx.Client(timeout=self._timeout_s) as owned:
                    response = owned.post(
                        url, json=json_body, data=form, content=content, headers=headers
                    )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise DeliveryError(f"slack: {label}: HTTP {error.response.status_code}") from None
        except httpx.HTTPError as error:
            raise DeliveryError(f"slack: {label}: {type(error).__name__}") from None
        return response


class SlackBotNotifier:
    """`chat.postMessage` で本文を送る宛先。名前は Webhook と同じ `slack`（`routing` を変えない）。

    通知に `thread_title` があれば、題だけの親を先に送り、本文をそのスレッドへ返す
    （日次レポート。決定記録 0171）。

    **Bot の送信が失敗し、Webhook が設定されていれば、その 1 通は Webhook で送り直す**
    （0147 §2.1）。トークンの失効で critical が届かなくなるのを避ける。送り直した通知には
    グラフを添えない（スレッドの `ts` が無い）。
    """

    name = "slack"

    def __init__(
        self,
        api: SlackApi,
        channel: str,
        *,
        fallback: Notifier | None = None,
        on_posted: OnPosted | None = None,
    ) -> None:
        self.api = api
        self.channel = channel
        self._fallback = fallback
        self._on_posted = on_posted

    def __repr__(self) -> str:
        return f"SlackBotNotifier(fallback={self._fallback is not None})"

    @classmethod
    def from_env(cls, *, fallback: Notifier | None = None) -> SlackBotNotifier | None:
        """トークンとチャンネルが**両方**あるときだけ作る。片方だけなら警告して作らない。"""
        token = os.environ.get(SLACK_BOT_TOKEN_ENV, "")
        channel = os.environ.get(SLACK_CHANNEL_ENV, "")
        if not token and not channel:
            return None
        if not token or not channel:
            # 値は出さない。どちらが欠けているかだけ
            LOGGER.warning(
                "Slack の Bot は トークンとチャンネルの両方が要る。片方だけなので使わない",
                extra={
                    logs.FIELDS_KEY: {
                        "missing": SLACK_CHANNEL_ENV if token else SLACK_BOT_TOKEN_ENV,
                    }
                },
            )
            return None
        return cls(SlackApi(token), channel, fallback=fallback)

    def attach(self, on_posted: OnPosted) -> None:
        """本文を送れたときの続き（グラフの依頼）をつなぐ。合成の起点が 1 回だけ呼ぶ。"""
        self._on_posted = on_posted

    def send(self, notification: Notification) -> None:
        if notification.thread_title:
            self._send_threaded(notification)
            return
        self._send_one(notification)

    def _send_threaded(self, notification: Notification) -> None:
        """題だけの親を送り、本文をそのスレッドへ返す（日次レポート。決定記録 0171）。

        **本文を黙って失わない。** どこで失敗しても、最後は今までと同じ 1 通（`_send_one`。
        Bot が落ちれば Webhook）に戻す。

        - 親が送れない: スレッドをあきらめ、本文を 1 通で送る（親は無い）
        - 親は送れたが返信が送れない: 本文をスレッドでなくチャンネルへ 1 通で送る。
          題だけの親がチャンネルに残るが、本文が無いより良い。返信が実は届いていて応答だけが
          失われた（timeout など）なら本文が 2 通になるが、失うより重ねるほうを選ぶ
        - 戻した 1 通も送れなければ `DeliveryError` を投げる（呼び出し側が送れなかったと数える）

        `on_posted`（アラートのグラフの依頼）は呼ばない。スレッドはレポートのためのもので、
        グラフを添える対象でもない（0147 §2.1）。
        """
        try:
            parent_ts = self.api.post_message(self.channel, notification.thread_title)
        except DeliveryError as error:
            LOGGER.warning(
                "スレッドの親を送れなかったので、本文を 1 通で送る",
                extra={logs.FIELDS_KEY: {"rule": notification.rule_id, "reason": str(error)}},
            )
            self._send_one(notification)
            return
        try:
            self.api.post_message(self.channel, notification.as_text(), thread_ts=parent_ts)
        except DeliveryError as error:
            LOGGER.warning(
                "スレッドへ本文を返せなかったので、本文をチャンネルへ 1 通で送る",
                extra={logs.FIELDS_KEY: {"rule": notification.rule_id, "reason": str(error)}},
            )
            self._send_one(notification)

    def _send_one(self, notification: Notification) -> None:
        try:
            ts = self.api.post_message(self.channel, notification.as_text())
        except DeliveryError as error:
            if self._fallback is None:
                raise
            LOGGER.warning(
                "Slack の Bot で送れなかったので Webhook で送り直す（グラフは添えない）",
                extra={logs.FIELDS_KEY: {"rule": notification.rule_id, "reason": str(error)}},
            )
            self._fallback.send(notification)
            return
        if self._on_posted is None:
            return
        try:
            self._on_posted(notification, self.channel, ts)
        except Exception:
            # グラフの依頼の失敗で本文の送信を失敗扱いにしない（本文は届いている）
            LOGGER.warning(
                "グラフの依頼に失敗した（本文は届いている）",
                exc_info=True,
                extra={logs.FIELDS_KEY: {"rule": notification.rule_id}},
            )
