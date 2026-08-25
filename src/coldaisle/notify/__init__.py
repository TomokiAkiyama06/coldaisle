"""通知（#20）。アラートの**遷移**を人へ届ける。

状態ではなく遷移を送る。`firing` になった瞬間と `resolved` になった瞬間だけで、
発生中のあいだ送り続けはしない（決定記録 0013 §2.2）。

**通知の失敗で監視を止めない。** 取り込み・保存・判定が本体であり、
通知はその外側にある。
"""

from coldaisle.notify.models import Notification, NotifyConfig
from coldaisle.notify.notifiers import LineNotifier, Notifier, SlackNotifier, StdoutNotifier
from coldaisle.notify.notifiers import from_env as notifiers_from_env
from coldaisle.notify.router import Router

__all__ = [
    "LineNotifier",
    "Notification",
    "Notifier",
    "NotifyConfig",
    "Router",
    "SlackNotifier",
    "StdoutNotifier",
    "notifiers_from_env",
]
