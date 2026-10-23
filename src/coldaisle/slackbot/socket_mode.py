"""Slack の Socket Mode のクライアント（決定記録 0168 §2.1）。

1. App トークン（`xapp-`）で `apps.connections.open` を呼び、WebSocket の URL を得る
2. その URL へこちらからつなぎ（外から入る口を開けない）、envelope を受ける
3. envelope は**受けたらすぐ ack する**（3 s の期限）。
   `events_api` の event だけを `on_event` へ渡す
4. Slack の `disconnect`（理由を問わず）・WebSocket が閉じた・`idle_reconnect_s` の間
   何も受けない、のどれでも `apps.connections.open` から新しい URL を取り直してつなぎ直す
   （0168 §2.1。Slack は接続を数時間ごとに入れ替え、警告が届かないこともある）。
   間隔をあけ、失敗が続けば倍にし、`hello` を受けたら最初の間隔に戻す
   （Slack が切断を繰り返しても詰めてつなぎ直さない）

Slack の SDK は使わない（0168 §2.1）。WebSocket は依存の `websockets` の同期のクライアントで話す。
**URL には接続の券が入るので、ログ・例外に出さない**（失敗は例外の型の名前だけ）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from coldaisle import logs
from coldaisle.notify.notifiers import DeliveryError

LOGGER = logging.getLogger("coldaisle.slackbot")

RECV_POLL_S = 1.0
"""受信を待つ 1 回の時間。止める合図を見るための間隔で、閾値ではない。"""


class Connection(Protocol):
    """WebSocket の接続（`websockets.sync.client.ClientConnection` の使う面だけ）。"""

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def send(self, message: str) -> None: ...

    def close(self) -> None: ...


Connect = Callable[[str], Connection]
OnEvent = Callable[[dict[str, Any], dict[str, Any]], None]
"""(event, 外側の payload)。payload は `event_id` を持つ（再配信の重複を見分ける。0168 §2.1）。"""


def websocket_connect(url: str) -> Connection:
    """本番の接続。`websockets` の同期のクライアント（ping による死活の確認は既定のまま）。"""
    from websockets.sync.client import connect

    connection: Connection = connect(url, open_timeout=10, close_timeout=5, max_size=2**20)
    return connection


class Backoff:
    """つなぎ直すまで待つ時間。失敗が続くと倍にし、上限で止める。`hello` で最初に戻す。"""

    def __init__(self, initial_s: float, max_s: float) -> None:
        self._initial_s = initial_s
        self._max_s = max(max_s, initial_s)
        self._next_s = initial_s

    def next(self) -> float:
        wait = self._next_s
        self._next_s = min(self._next_s * 2, self._max_s)
        return wait

    def reset(self) -> None:
        self._next_s = self._initial_s


class SocketModeClient:
    """受信のループ。`run()` は `stop` が立つまで戻らない。**例外を上へ投げない。**"""

    def __init__(
        self,
        *,
        open_url: Callable[[], str],
        connect: Connect,
        on_event: OnEvent,
        backoff: Backoff,
        stop: threading.Event,
        idle_reconnect_s: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle_reconnect_s = idle_reconnect_s
        self._monotonic = monotonic
        self._open_url = open_url
        self._connect = connect
        self._on_event = on_event
        self._backoff = backoff
        self._stop = stop

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except DeliveryError as error:
                # DeliveryError の文は API の名前と Slack の符号だけ
                # （トークン・URL を含まない。0147）
                LOGGER.warning(
                    "Slack の Socket Mode の URL を得られなかった。間隔をあけてつなぎ直す",
                    extra={logs.FIELDS_KEY: {"error": "DeliveryError", "reason": str(error)}},
                )
            except Exception as error:
                # 例外の文には URL や応答が入りうる。型の名前だけ（0168 §2.1）
                LOGGER.warning(
                    "Slack の Socket Mode の接続が切れた。間隔をあけてつなぎ直す",
                    extra={logs.FIELDS_KEY: {"error": type(error).__name__}},
                )
            if self._stop.is_set():
                return
            self._stop.wait(self._backoff.next())

    def _session(self) -> None:
        """1 回の接続。止める合図・切断・Slack の `disconnect` で戻る。"""
        connection = self._connect(self._open_url())
        last_received = self._monotonic()
        try:
            while not self._stop.is_set():
                try:
                    raw = connection.recv(timeout=RECV_POLL_S)
                except TimeoutError:
                    if self._monotonic() - last_received >= self._idle_reconnect_s:
                        # 黙って入れ替えられた接続を待ち続けない（0168 §2.1）
                        LOGGER.info(
                            "Slack から長く何も届かないので、新しい URL でつなぎ直す",
                            extra={logs.FIELDS_KEY: {"idle_s": self._idle_reconnect_s}},
                        )
                        return
                    continue
                last_received = self._monotonic()
                if self._handle(connection, raw):
                    return
        finally:
            try:
                connection.close()
            except Exception:
                LOGGER.debug("WebSocket を閉じるときに失敗した（無視する）")

    def _handle(self, connection: Connection, raw: str | bytes) -> bool:
        try:
            message: Any = json.loads(raw)
        except ValueError:
            LOGGER.warning("Socket Mode の受信が JSON でない（読み飛ばす）")
            return False
        if not isinstance(message, dict):
            return False
        kind = message.get("type")
        if kind == "hello":
            self._backoff.reset()
            LOGGER.info("Slack の Socket Mode につないだ")
            return False
        if kind == "disconnect":
            reason = message.get("reason")
            LOGGER.info(
                "Slack が切断を求めたので、つなぎ直す",
                extra={logs.FIELDS_KEY: {"reason": reason if isinstance(reason, str) else None}},
            )
            return True
        envelope_id = message.get("envelope_id")
        if isinstance(envelope_id, str) and envelope_id:
            # **まず ack する**（3 s の期限。答えは別のスレッドで作る。0168 §2.1）
            connection.send(json.dumps({"envelope_id": envelope_id}))
        if kind != "events_api":
            return False
        payload = message.get("payload")
        event = payload.get("event") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and isinstance(event, dict):
            try:
                self._on_event(event, payload)
            except Exception as error:
                LOGGER.warning(
                    "メンションの受け付けに失敗した（受信は続ける）",
                    extra={logs.FIELDS_KEY: {"error": type(error).__name__}},
                )
        return False
