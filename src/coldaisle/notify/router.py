"""通知の振り分けと抑制（#20）。

3つのことをする。

1. **重大度で宛先を選ぶ**（決定記録 0001 D-03）
2. **連投を抑える。** 同じアラートは初回とその後は間隔をあけて。
   ただし**解除は必ず送る**（鳴りっぱなしと区別できなくなるため）
3. **夜間の方針**（決定記録 0001 D-04）。閾値が実測で確定するまで
   夜間の warning を止める

送信は**別スレッド**で行う。宛先が応答しないときに取り込みが止まっては本末転倒
（決定記録 0013 §2.4）。
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field

from coldaisle import logs
from coldaisle.clock import Clock
from coldaisle.notify.models import Notification, NotifyConfig
from coldaisle.notify.notifiers import Notifier
from coldaisle.store.models import AlertSeverity

LOGGER = logging.getLogger("coldaisle.notify")

QUEUE_SIZE = 128
"""送信待ちの上限。**有界にする**（常駐メモリ。NFR-05）。"""


@dataclass
class Router:
    """通知を振り分けて送る。"""

    config: NotifyConfig
    notifiers: dict[str, Notifier]
    clock: Clock
    _last_sent_ms: dict[tuple[str, str | None], int] = field(default_factory=dict)
    _queue: queue.Queue[Notification | None] = field(
        default_factory=lambda: queue.Queue(QUEUE_SIZE)
    )
    _worker: threading.Thread | None = field(default=None)
    dropped: int = 0

    def __post_init__(self) -> None:
        wanted = {name for names in self.config.routing.values() for name in names}
        missing = sorted(wanted - set(self.notifiers))
        if missing:
            # **黙って無効にしない。** 届かない経路があることを起動時に言う
            LOGGER.warning(
                "設定されていない通知先がある（その重大度は残りの宛先だけに届く）",
                extra={logs.FIELDS_KEY: {"missing": missing}},
            )

    # ------------------------------------------------------------------ 送信

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()

    def stop(self, *, timeout_s: float = 30.0) -> None:
        """送信し切ってから止める。**送る前に落とさない。**

        宛先1つあたり最大10秒かかり、宛先は順に処理する。短い待ちで諦めて
        参照を捨てると、**最後の critical を積んだままプロセスが終わる。**
        送り切れなかった場合はスレッドを手放さず、記録して知らせる。
        """
        if self._worker is None:
            return
        self._queue.put(None)
        self._worker.join(timeout=timeout_s)
        if self._worker.is_alive():  # pragma: no cover - 宛先が長時間詰まったとき
            LOGGER.error(
                "通知を送り切れないまま停止しようとしている",
                extra={logs.FIELDS_KEY: {"pending": self._queue.qsize()}},
            )
            return
        self._worker = None

    def notify(self, notification: Notification) -> bool:
        """送信を予約する。送るべきでなければ `False`。"""
        if not self.config.enabled or not self._should_send(notification):
            return False
        try:
            self._queue.put_nowait(notification)
        except queue.Full:  # pragma: no cover - 宛先が長時間詰まったとき
            self.dropped += 1
            LOGGER.warning(
                "通知の待ち行列が溢れた", extra={logs.FIELDS_KEY: {"dropped": self.dropped}}
            )
            return False
        if self._worker is None:
            self._drain()  # スレッドを起こしていない場合はその場で送る（テスト用）
        return True

    # ------------------------------------------------------------------ 判定

    def _should_send(self, notification: Notification) -> bool:
        key = (notification.rule_id, notification.metric)
        now = self.clock.now_ms()
        if notification.kind == "explanation":
            # **発火を届けた相手にだけ、後から続ける。** 連投の抑制も夜間の方針も
            # 通さない（元の通知は既に届いている）。逆に届けていないなら送らない
            return key in self._last_sent_ms
        if notification.state == "resolved":
            # **発火を届けたなら、解除も必ず届ける。** 連投の抑制も夜間の方針も
            # 通さない。送らないと、受け取った側は鳴りっぱなしだと思い続ける。
            #
            # 逆に、発火を送っていない（夜間に抑制した warning など）なら
            # 解除も送らない。知らせていないものの「解除」を夜中に鳴らすのは、
            # 夜間の方針（決定記録 0001 D-04）が避けようとしていることそのもの。
            return self._last_sent_ms.pop(key, None) is not None
        last = self._last_sent_ms.get(key)
        if last is not None and (now - last) / 1000 < self.config.repeat_after_s:
            return False
        if not self._passes_night_policy(notification, now):
            return False
        self._last_sent_ms[key] = now
        return True

    def _passes_night_policy(self, notification: Notification, now_ms: int) -> bool:
        """夜間は critical だけにする（閾値が確定するまで。決定記録 0001 D-04）。

        **誤警報が睡眠を削るほうが、通知が遅れるより実害が大きい。**
        解除には適用しない（上記）。
        """
        if notification.severity is AlertSeverity.CRITICAL:
            return True
        if not self.config.night.is_night(now_ms):
            return True
        return self.config.night.warning_enabled

    # ------------------------------------------------------------------ 実送信

    def _run(self) -> None:  # pragma: no cover - スレッドの本体
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._deliver(item)

    def _drain(self) -> None:
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None:
                self._deliver(item)

    def _deliver(self, notification: Notification) -> None:
        for name in self.config.routing.get(notification.severity, []):
            notifier = self.notifiers.get(name)
            if notifier is None:
                continue
            try:
                notifier.send(notification)
            except Exception:
                LOGGER.warning(
                    "通知の送信に失敗した",
                    exc_info=True,
                    extra={logs.FIELDS_KEY: {"target": name, "rule": notification.rule_id}},
                )
