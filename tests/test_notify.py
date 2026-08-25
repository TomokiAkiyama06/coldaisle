"""通知（#20）。

要点は3つ。**連投しない**、**解除は必ず送る**、**送信の失敗で監視を止めない**。

外部サービスへは出ない。宛先は差し替えて確かめる。
秘匿情報はテストにも書かない（#41）。
"""

import json
from pathlib import Path

import httpx
import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.notify import Notification, NotifyConfig, Router, StdoutNotifier
from coldaisle.notify.notifiers import (
    LINE_TO_ENV,
    LINE_TOKEN_ENV,
    SLACK_WEBHOOK_ENV,
    LineNotifier,
    SlackNotifier,
)
from coldaisle.notify.notifiers import from_env as notifiers_from_env
from coldaisle.store import AlertSeverity
from conftest import CONFIG_DIR

NOTIFY_PATH = CONFIG_DIR / "notify.yaml"
NOON_MS = 1_787_626_800_000  # 2026-08-25T12:00:00+09:00（昼）
NIGHT_MS = 1_787_670_000_000  # 2026-08-26T00:00:00+09:00（夜）


class Recorder:
    """届いたものを覚えるだけの宛先。"""

    def __init__(self, name: str = "slack", *, fail: bool = False) -> None:
        self.name = name
        self.sent: list[Notification] = []
        self._fail = fail

    def send(self, notification: Notification) -> None:
        if self._fail:
            raise RuntimeError("宛先が落ちている")
        self.sent.append(notification)


@pytest.fixture
def config() -> NotifyConfig:
    return NotifyConfig.from_yaml(NOTIFY_PATH)


def notification(state: str = "firing", severity: str = "warning", **kwargs) -> Notification:
    defaults = {
        "rule_id": "RECIRCULATION",
        "severity": AlertSeverity(severity),
        "state": state,
        "metric": "d.intake_rise",
        "value": 7.2,
        "detail": None,
        "dashboard_url": "http://127.0.0.1:8000/",
        "context": {"air.room": 26.0, "air.front_intake": 33.2},
    }
    return Notification(**{**defaults, **kwargs})


def router(config, clock, **targets) -> Router:
    return Router(config=config, notifiers=targets or {"stdout": StdoutNotifier()}, clock=clock)


# ---------------------------------------------------------------- 振り分け


def test_severity_routing(config, tmp_path):
    """critical は LINE、warning は Slack（決定記録 0001 D-03）。"""
    assert config.routing[AlertSeverity.CRITICAL] == ["line", "stdout"]
    assert config.routing[AlertSeverity.WARNING] == ["slack", "stdout"]


def test_notification_goes_only_to_its_targets(config):
    slack, line = Recorder("slack"), Recorder("line")
    sender = router(config, SimulatedClock(NOON_MS), slack=slack, line=line)
    sender.notify(notification(severity="warning"))

    assert len(slack.sent) == 1
    assert line.sent == [], "warning は LINE へ送らない"


def test_critical_reaches_line(config):
    line = Recorder("line")
    sender = router(config, SimulatedClock(NIGHT_MS), line=line)
    sender.notify(notification(severity="critical", rule_id="SENSOR_FAULT"))
    assert len(line.sent) == 1


def test_missing_target_is_announced_not_silently_dropped(config, caplog):
    """**黙って無効にしない。** 届かない経路があることを起動時に言う。"""
    with caplog.at_level("WARNING"):
        router(config, SimulatedClock(NOON_MS), stdout=StdoutNotifier())
    assert any("設定されていない通知先" in record.message for record in caplog.records)


# ---------------------------------------------------------------- 連投の抑制


def test_repeated_firing_is_suppressed(config):
    """受入基準: 1つのアラートで通知が連投されない。"""
    slack = Recorder()
    clock = SimulatedClock(NOON_MS)
    sender = router(config, clock, slack=slack)

    assert sender.notify(notification()) is True
    for step in range(1, 10):
        clock.advance_to_ms(NOON_MS + step * 60_000)  # 1分ごとに再発火
        assert sender.notify(notification()) is False
    assert len(slack.sent) == 1


def test_repeat_after_the_interval(config):
    slack = Recorder()
    clock = SimulatedClock(NOON_MS)
    sender = router(config, clock, slack=slack)
    sender.notify(notification())

    clock.advance_to_ms(NOON_MS + int(config.repeat_after_s * 1000) + 1_000)
    assert sender.notify(notification()) is True
    assert len(slack.sent) == 2


def test_resolution_is_always_sent(config):
    """**解除は抑制しない。** 送らないと鳴りっぱなしと区別できない。"""
    slack = Recorder()
    clock = SimulatedClock(NOON_MS)
    sender = router(config, clock, slack=slack)
    sender.notify(notification())
    clock.advance_to_ms(NOON_MS + 60_000)  # 抑制の間隔より短い

    assert sender.notify(notification(state="resolved")) is True
    assert [item.state for item in slack.sent] == ["firing", "resolved"]


def test_a_new_firing_after_resolution_is_sent(config):
    """解除したら抑制も忘れる。次の発火は届く。"""
    slack = Recorder()
    clock = SimulatedClock(NOON_MS)
    sender = router(config, clock, slack=slack)
    sender.notify(notification())
    sender.notify(notification(state="resolved"))
    clock.advance_to_ms(NOON_MS + 60_000)

    assert sender.notify(notification()) is True
    assert len(slack.sent) == 3


def test_different_metrics_are_suppressed_independently(config):
    slack = Recorder()
    sender = router(config, SimulatedClock(NOON_MS), slack=slack)
    sender.notify(notification(rule_id="SENSOR_MISSING", metric="air.room"))
    sender.notify(notification(rule_id="SENSOR_MISSING", metric="air.rear_exhaust"))
    assert len(slack.sent) == 2


# ---------------------------------------------------------------- 夜間の方針


def test_night_suppresses_warning_until_thresholds_are_measured(config):
    """**ベースライン測定（#19）が終わるまで夜間の warning を止める。**

    夜間も通知する以上、誤警報が睡眠を削る。
    「通知が信用されなくなる」より深刻な実害（決定記録 0001 D-04）。
    """
    assert config.night.warning_enabled is False, "#19 の完了まで false のまま"
    slack = Recorder()
    sender = router(config, SimulatedClock(NIGHT_MS), slack=slack)
    assert sender.notify(notification(severity="warning")) is False
    assert slack.sent == []


def test_night_never_suppresses_critical(config):
    line = Recorder("line")
    sender = router(config, SimulatedClock(NIGHT_MS), line=line)
    assert sender.notify(notification(severity="critical")) is True


def test_daytime_warning_is_sent(config):
    slack = Recorder()
    sender = router(config, SimulatedClock(NOON_MS), slack=slack)
    assert sender.notify(notification(severity="warning")) is True


def test_night_warning_can_be_enabled(config):
    """閾値確定後にフラグで有効化できる（Issue の指定）。"""
    enabled = config.model_copy(
        update={"night": config.night.model_copy(update={"warning_enabled": True})}
    )
    slack = Recorder()
    sender = router(enabled, SimulatedClock(NIGHT_MS), slack=slack)
    assert sender.notify(notification(severity="warning")) is True


@pytest.mark.parametrize(("at_ms", "expected"), [(NIGHT_MS, True), (NOON_MS, False)])
def test_night_window_wraps_midnight(at_ms, expected, config):
    assert config.night.is_night(at_ms) is expected


# ---------------------------------------------------------------- 本文と失敗


def test_body_carries_context_and_dashboard_link():
    """本文だけで何が起きているか判断できるようにする（Issue の指定）。"""
    text = notification().as_text()
    assert "RECIRCULATION" in text
    assert "7.20" in text
    assert "air.room=26.00" in text
    assert "http://127.0.0.1:8000/" in text


def test_resolution_is_visually_distinct():
    assert "🔴" in notification().title
    assert "✅" in notification(state="resolved").title


def test_a_failing_target_does_not_stop_the_others(config, caplog):
    """**送信の失敗で監視を止めない。** 片方が落ちても残りへ届く。"""
    broken, stdout = Recorder("slack", fail=True), Recorder("stdout")
    sender = router(config, SimulatedClock(NOON_MS), slack=broken, stdout=stdout)
    with caplog.at_level("WARNING"):
        sender.notify(notification())
    assert len(stdout.sent) == 1
    assert any("通知の送信に失敗した" in record.message for record in caplog.records)


def test_disabled_router_sends_nothing(config):
    slack = Recorder()
    sender = router(
        config.model_copy(update={"enabled": False}), SimulatedClock(NOON_MS), slack=slack
    )
    assert sender.notify(notification()) is False


# ---------------------------------------------------------------- 宛先の実装


def test_slack_posts_the_text():
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    SlackNotifier("https://example.invalid/hook", client=client).send(notification())
    assert "RECIRCULATION" in posted[0]["text"]


def test_line_pushes_to_the_configured_destination():
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    LineNotifier("token", "U123", client=client).send(notification(severity="critical"))
    authorization, payload = seen[0]
    assert authorization == "Bearer token"
    assert payload["to"] == "U123"
    assert payload["messages"][0]["type"] == "text"


def test_targets_without_credentials_are_not_created(monkeypatch):
    """**未設定の宛先は作らない。** 毎回失敗させるとログが埋まる。"""
    for name in (SLACK_WEBHOOK_ENV, LINE_TOKEN_ENV, LINE_TO_ENV):
        monkeypatch.delenv(name, raising=False)
    assert set(notifiers_from_env()) == {"stdout"}


def test_targets_are_created_from_the_environment(monkeypatch):
    monkeypatch.setenv(SLACK_WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setenv(LINE_TOKEN_ENV, "token")
    monkeypatch.setenv(LINE_TO_ENV, "U123")
    assert set(notifiers_from_env()) == {"stdout", "slack", "line"}


def test_no_secrets_in_the_config_file():
    """秘匿情報は環境変数から。リポジトリは public（#41 / 決定 Q-06）。"""
    text = Path(NOTIFY_PATH).read_text(encoding="utf-8")
    assert "hooks.slack.com" not in text
    assert "Bearer" not in text
    assert "COLDAISLE_SLACK_WEBHOOK" in text, "どこから読むかは書いてある"
