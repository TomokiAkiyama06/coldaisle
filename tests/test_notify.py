"""通知（#20）。

要点は3つ。**連投しない**、**解除は必ず送る**、**送信の失敗で監視を止めない**。

外部サービスへは出ない。宛先は差し替えて確かめる。
秘匿情報はテストにも書かない（#41）。
"""

import io
import json
import logging
from pathlib import Path

import httpx
import pytest

from coldaisle import logs
from coldaisle.clock import SimulatedClock
from coldaisle.metrics import MetricCatalog
from coldaisle.notify import DisplayNames, Notification, NotifyConfig, Router, StdoutNotifier
from coldaisle.notify.models import DASHBOARD_URL_ENV
from coldaisle.notify.notifiers import (
    LINE_TO_ENV,
    LINE_TOKEN_ENV,
    SLACK_WEBHOOK_ENV,
    DeliveryError,
    LineNotifier,
    SlackNotifier,
)
from coldaisle.notify.notifiers import from_env as notifiers_from_env
from coldaisle.rules import RuleSet
from coldaisle.store import AlertSeverity
from conftest import CONFIG_DIR

NOTIFY_PATH = CONFIG_DIR / "notify.yaml"
RULES_PATH = CONFIG_DIR / "rules.yaml"
_RULES = RuleSet.from_yaml(RULES_PATH)
NAMES = DisplayNames.build(
    rules=_RULES,
    catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
    context_always=NotifyConfig.from_yaml(NOTIFY_PATH, environ={}).context_always,
)
"""本番の設定から作った名前（決定記録 0142）。"""
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
        "names": NAMES,
    }
    return Notification(**{**defaults, **kwargs})


def router(config, clock, **targets) -> Router:
    return Router(config=config, notifiers=targets or {"stdout": StdoutNotifier()}, clock=clock)


# ---------------------------------------------------------------- 振り分け


def test_severity_routing(config, tmp_path):
    """critical も warning も Slack（決定記録 0135。0001 D-03 を置き換え）。

    LINE は使わない。区別は本文の重大度で行う。
    """
    assert config.routing[AlertSeverity.CRITICAL] == ["slack", "stdout"]
    assert config.routing[AlertSeverity.WARNING] == ["slack", "stdout"]


def test_notification_goes_only_to_its_targets(config):
    """`routing` に無い宛先へは送らない。LINE は作られていても使わない（決定記録 0135）。"""
    slack, line = Recorder("slack"), Recorder("line")
    sender = router(config, SimulatedClock(NOON_MS), slack=slack, line=line)
    sender.notify(notification(severity="warning"))
    sender.notify(notification(severity="critical", rule_id="SENSOR_FAULT"))

    assert len(slack.sent) == 2
    assert line.sent == [], "routing に無い LINE へは送らない"


def test_critical_reaches_slack(config):
    """critical は夜間でも Slack に届く（決定記録 0135 §2.3）。"""
    slack = Recorder("slack")
    sender = router(config, SimulatedClock(NIGHT_MS), slack=slack)
    sender.notify(notification(severity="critical", rule_id="SENSOR_FAULT"))
    assert [item.severity for item in slack.sent] == [AlertSeverity.CRITICAL]


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
    slack = Recorder("slack")
    sender = router(config, SimulatedClock(NIGHT_MS), slack=slack)
    assert sender.notify(notification(severity="critical")) is True
    assert len(slack.sent) == 1


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


def test_body_carries_context_and_hint():
    """本文だけで何が起きているか・何を確かめるかが分かる（Issue の指定 / 決定記録 0142）。"""
    text = notification().as_text()
    assert "排気が吸気側へ回り込んでいます" in text
    assert "吸気上昇（再循環の指標） 7.2 °C（閾値 5.0 °C を超過）" in text
    assert "室温 26.0 °C" in text
    assert "確認してください" in text


def test_resolution_is_visually_distinct():
    assert "🔴" in notification(severity="critical").title
    assert "🟠" in notification(severity="warning").title
    assert "✅" in notification(state="resolved").title


SEVERITY_TITLES = [
    ("critical", "firing", "🔴 [重大] GPU の吸気温度が急上昇しています"),
    ("critical", "resolved", "✅ [重大 解除] GPU の吸気温度の急上昇が収まりました"),
    ("warning", "firing", "🟠 [警告] GPU の吸気温度が急上昇しています"),
    ("warning", "resolved", "✅ [警告 解除] GPU の吸気温度の急上昇が収まりました"),
    ("info", "firing", "🔵 [情報] GPU の吸気温度が急上昇しています"),
]


def rapid_rise(state: str, severity: str) -> Notification:
    return notification(
        state=state, severity=severity, rule_id="RAPID_RISE", metric="air.gpu_intake"
    )


@pytest.mark.parametrize(("severity", "state", "expected"), SEVERITY_TITLES)
def test_body_starts_with_the_severity_on_slack(severity, state, expected):
    """**本文の先頭の行に重大度が出る。** 文は日本語（決定記録 0142 §2.1）。

    critical と warning は同じ Slack のチャンネルへ届く（決定記録 0135 §2.2）ため、
    宛先では見分けられない。実際に POST される本文で確かめる。
    """
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    SlackNotifier("https://example.invalid/hook", client=client).send(rapid_rise(state, severity))
    lines = posted[0]["text"].splitlines()
    assert lines[0] == expected
    assert lines[-1] == "RAPID_RISE / air.gpu_intake", "最後の行に ID を残す（0142 §2.2）"


@pytest.mark.parametrize(("severity", "state", "expected"), SEVERITY_TITLES)
def test_body_starts_with_the_severity_on_stdout(severity, state, expected, caplog):
    """stdout（構造化ログ）の本文にも同じ先頭の行が出る。ログの欄は ID のまま（0142 §2.7）。"""
    with caplog.at_level("WARNING"):
        StdoutNotifier().send(rapid_rise(state, severity))
    fields = [getattr(record, "fields", None) for record in caplog.records]
    entries = [entry for entry in fields if entry]
    assert entries[0]["text"].splitlines()[0] == expected
    assert entries[0]["rule"] == "RAPID_RISE"


def test_firing_body_in_japanese():
    """発火の本文の全体（所有者が了承した形。決定記録 0142 §2.2）。

    値と detail を重ねず、1 行に「対象の値（変化量）・関係する値・室温」。数は小数 1 桁。
    ループバックのダッシュボードの URL は出さない。
    """
    item = notification(
        severity="critical",
        rule_id="RAPID_RISE",
        metric="air.gpu_intake",
        value=5.16,
        detail="5.16 ℃/分で上昇している",
        context={"air.gpu_intake": 37.15, "air.room": 26.09, "air.room_humidity": 48.0},
    )
    assert item.as_text().splitlines() == [
        "🔴 [重大] GPU の吸気温度が急上昇しています",
        "GPU吸気 37.1 °C（1 分で +5.2 °C）・室温 26.1 °C",
        "GPU の前が塞がれていないか、排気が回り込んでいないかを確認してください。",
        "RAPID_RISE / air.gpu_intake",
    ]


def test_resolved_body_in_japanese():
    """解除は文と現在値の 1 行だけ（確かめることは載せない）。"""
    item = notification(
        severity="critical",
        state="resolved",
        rule_id="RAPID_RISE",
        metric="air.gpu_intake",
        value=None,
        context={"air.gpu_intake": 38.0, "air.room": 24.1},
    )
    assert item.as_text().splitlines() == [
        "✅ [重大 解除] GPU の吸気温度の急上昇が収まりました",
        "GPU吸気 38.0 °C・室温 24.1 °C",
        "RAPID_RISE / air.gpu_intake",
    ]


def test_dashboard_link_is_shown_only_when_reachable():
    """`COLDAISLE_DASHBOARD_URL` を設定したとき（ループバックでない）だけ URL を載せる。"""
    loopback = notification(dashboard_url="http://127.0.0.1:8000/").as_text()
    localhost = notification(dashboard_url="http://localhost:8000/").as_text()
    reachable = notification(dashboard_url="https://dashboard.example.invalid/").as_text()
    assert "127.0.0.1" not in loopback
    assert "localhost" not in localhost
    assert reachable.splitlines()[-2] == "https://dashboard.example.invalid/"


def test_alert_without_metric_has_id_only():
    """`SENSOR_FAULT` はメトリクスを持たない。値は秒数で、°C を付けない（0142 §2.4）。"""
    item = notification(
        severity="critical",
        rule_id="SENSOR_FAULT",
        metric=None,
        value=31.0,
        detail="31 秒サンプルが届いていない",
        context={"air.room": 24.1, "air.gpu_intake": 30.0},
    )
    lines = item.as_text().splitlines()
    assert lines[:2] == [
        "🔴 [重大] センサー基板からのデータが途絶えています",
        "31 秒データが届いていない・室温 24.1 °C",
    ]
    assert lines[-1] == "SENSOR_FAULT"


@pytest.mark.parametrize(
    ("rule_id", "metric", "value", "context", "expected"),
    [
        (
            "SENSOR_MISSING",
            "air.top_exhaust",
            5.0,
            {"air.top_exhaust": None, "air.room": 24.1},
            "トップ排気の値が 5 回続けて欠けた・室温 24.1 °C",
        ),
        (
            "ROOM_HIGH",
            "air.room",
            30.52,
            {"air.room": 30.52},
            "室温 30.5 °C（閾値 30.0 °C を超過）",
        ),
        (
            # 決定記録 0144: 設定の threshold は期待値を超えた分なので数を書かない
            "AIRFLOW_DEGRADED",
            "d.gpu_delta",
            47.0,
            {
                "air.gpu_exhaust": 68.0,
                "air.gpu_intake": 21.0,
                "power.gpu.0": 600.0,
                "air.room": 24.1,
            },
            "GPUが与えた熱 47.0 °C（GPU の電力に応じた正常の上限を超過）・GPU排気 68.0 °C"
            "・GPU吸気 21.0 °C・GPU消費電力 600.0 W・室温 24.1 °C",
        ),
        (
            "HUMIDITY_OUT_OF_RANGE",
            "air.room_humidity",
            71.0,
            {"air.room_humidity": 71.0, "air.room": 24.1},
            "室内湿度 71.0 %RH（上限 70.0 %RH を超過）・室温 24.1 °C",
        ),
        (
            "HUMIDITY_OUT_OF_RANGE",
            "air.room_humidity",
            18.0,
            {"air.room_humidity": 18.0, "air.room": 24.1},
            "室内湿度 18.0 %RH（下限 20.0 %RH を下回る）・室温 24.1 °C",
        ),
        (
            "GPU_FAN_BELOW_CURVE",
            "gpu.0.fan_speed",
            47.0,
            {"gpu.0.core": 81.0, "air.room": 24.1},
            "GPU Fan速度（NVMLの目標値） 47.0 %（70.0 % 未満）・GPUコア温度 81.0 °C・室温 24.1 °C",
        ),
        ("GPU_FAN_NOT_MANUAL", "gpu.0.fan_policy", 0.0, {"air.room": 24.1}, "室温 24.1 °C"),
        (
            "PROBE_CHANGED",
            None,
            None,
            {"air.room": 24.1},
            "較正が対応していないプローブ: gpu_intake・室温 24.1 °C",
        ),
    ],
)
def test_reading_line_follows_the_rule_kind(rule_id, metric, value, context, expected):
    """値の行の書き方はルールの種類で決める（決定記録 0142 §2.4）。`flag` の値は出さない。"""
    item = notification(
        rule_id=rule_id,
        metric=metric,
        value=value,
        detail="較正が対応していないプローブ: gpu_intake",
        context=context,
    )
    assert item.as_text().splitlines()[1] == expected


@pytest.mark.parametrize(
    ("rule_id", "metric", "expected"),
    [
        ("RAPID_RISE", "air.gpu_intake", "🟠 [警告] GPU の吸気温度が急上昇しています"),
        ("INTAKE_HIGH", "air.gpu_intake", "🟠 [警告] GPU の吸気温度が高くなっています"),
        ("SENSOR_MISSING", "air.top_exhaust", "🟠 [警告] センサーの値が欠けています（トップ排気）"),
        ("SENSOR_MISSING", "gpu.1.core", "🟠 [警告] センサーの値が欠けています（gpu.1.core）"),
    ],
)
def test_metric_is_appended_only_when_the_rule_does_not_fix_it(rule_id, metric, expected):
    """名前を重ねない。ルールの定義がメトリクスを決めていないときだけ括弧を付ける（0142 §2.1）。

    `config/metrics.yaml` に無いメトリクスは ID のまま。
    """
    assert notification(rule_id=rule_id, metric=metric).title == expected


def test_without_names_ids_are_shown():
    """名前を渡さなければ ID のまま出る（名前の辞書を持たない呼び出し）。"""
    item = notification(names=DisplayNames(), rule_id="RAPID_RISE", metric="air.gpu_intake")
    assert item.title == "🟠 [警告] RAPID_RISE（air.gpu_intake） 発生"


def test_explanation_title_carries_the_severity():
    """発火に続く説明も同じチャンネルへ届くため、重大度を載せる。最後の行に ID。"""
    item = notification(severity="critical", kind="explanation", body="説明")
    assert item.title == "🔎 [重大] 説明: 排気が吸気側へ回り込んでいます"
    assert item.as_text().splitlines()[-1] == "RECIRCULATION / d.intake_rise"


TEXT_KEYS = ("label", "title", "resolved_title", "hint")


def test_every_rule_has_texts():
    """`config/rules.yaml` のすべてのルールに名前・2 つの文・確かめることがある（0142 §2.3）。"""
    rules = RuleSet.from_yaml(RULES_PATH)
    assert set(NAMES.rules) == {name.upper() for name in type(rules).model_fields}
    for text in NAMES.rules.values():
        for key in TEXT_KEYS:
            value = getattr(text, key)
            assert value.strip() and not value.isascii(), (key, value)


@pytest.mark.parametrize("key", TEXT_KEYS)
@pytest.mark.parametrize("blank", [False, True])
def test_rule_without_a_text_is_rejected(tmp_path, key, blank):
    """名前・文・確かめることが無い・空のルールは読み込みで落とす。"""
    lines = RULES_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    start = lines.index("  rapid_rise:\n")
    index = next(i for i in range(start, len(lines)) if lines[i].startswith(f"    {key}: "))
    lines[index] = f"    {key}: '   '\n" if blank else ""
    path = tmp_path / "rules.yaml"
    path.write_text("".join(lines), encoding="utf-8")
    with pytest.raises(ValueError, match="rapid_rise"):
        RuleSet.from_yaml(path)


AIR = {
    "air.room": 24.1,
    "air.room_humidity": 55.0,
    "air.front_intake": 27.0,
    "air.gpu_intake": 30.0,
    "air.gpu_exhaust": 52.0,
    "air.top_exhaust": 33.0,
    "air.rear_exhaust": 35.0,
    "d.intake_rise": 2.9,
    "d.gpu_delta": 22.0,
}
"""デーモンが渡す現在値の形（空気のセンサー 7 本と派生値）。"""


@pytest.mark.parametrize(
    ("rule_id", "metric", "expected"),
    [
        (
            "AIRFLOW_DEGRADED",
            "d.gpu_delta",
            "GPUが与えた熱 22.0 °C・GPU排気 52.0 °C・GPU吸気 30.0 °C・室温 24.1 °C",
        ),
        (
            "RECIRCULATION",
            "d.intake_rise",
            "吸気上昇（再循環の指標） 2.9 °C・前面吸気 27.0 °C・室温 24.1 °C",
        ),
        ("INTAKE_HIGH", "air.gpu_intake", "GPU吸気 30.0 °C・室温 24.1 °C"),
        ("ROOM_HIGH", "air.room", "室温 24.1 °C"),
        ("HUMIDITY_OUT_OF_RANGE", "air.room_humidity", "室内湿度 55.0 %RH・室温 24.1 °C"),
        ("SENSOR_MISSING", "air.top_exhaust", "トップ排気 33.0 °C・室温 24.1 °C"),
        ("SENSOR_FAULT", None, "室温 24.1 °C"),
    ],
)
def test_context_lists_only_related_readings(rule_id, metric, expected):
    """値の行はアラートのメトリクス・派生値の材料・室温だけ（決定記録 0142 §2.2a）。

    材料は `config/metrics.yaml` の派生値の定義から引く。室温は最後に 1 回だけ。
    解除の本文で確かめる（発火の対象の値の注記が付かない形）。
    """
    item = notification(
        state="resolved", rule_id=rule_id, metric=metric, value=None, context=dict(AIR)
    )
    assert item.as_text().splitlines()[1] == expected


def test_context_always_must_be_a_known_metric():
    """常に付けるメトリクスの綴り違いは起動時に落とす。"""
    with pytest.raises(ValueError, match="context_always"):
        DisplayNames.build(
            rules=_RULES,
            catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
            context_always=("air.rooom",),
        )


def test_notify_config_requires_context_always(tmp_path):
    """`config/notify.yaml` の `context_always` は必須（書き漏らしで室温が黙って消えない）。"""
    text = NOTIFY_PATH.read_text(encoding="utf-8")
    broken = text.replace("context_always: [air.room]\n", "")
    assert broken != text
    path = tmp_path / "notify.yaml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError, match="context_always"):
        NotifyConfig.from_yaml(path, environ={})


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


# ---------------------------------------------------------------- レビュー指摘の退行防止


def test_resolution_is_sent_at_night_when_the_firing_was_sent(config):
    """**昼に発火し、夜に解除**しても届く。

    夜間の抑制を解除にも当てると、受け取った側は**鳴りっぱなしだと思い続ける。**
    """
    slack = Recorder()
    clock = SimulatedClock(NOON_MS)
    sender = router(config, clock, slack=slack)
    assert sender.notify(notification(severity="warning")) is True

    clock.advance_to_ms(NIGHT_MS)  # 22時を過ぎた
    assert sender.notify(notification(state="resolved", severity="warning")) is True
    assert [item.state for item in slack.sent] == ["firing", "resolved"]


def test_resolution_is_not_sent_when_the_firing_was_suppressed(config):
    """知らせていないものの「解除」を夜中に鳴らさない。

    夜間の方針（決定記録 0001 D-04）が避けようとしていることそのものになる。
    """
    slack = Recorder()
    sender = router(config, SimulatedClock(NIGHT_MS), slack=slack)
    assert sender.notify(notification(severity="warning")) is False
    assert sender.notify(notification(state="resolved", severity="warning")) is False
    assert slack.sent == []


def test_critical_resolution_keeps_its_severity(config):
    """**解除の重大度が落ちない。** critical の解除が warning として扱われてはいけない。

    critical と warning は同じ Slack へ届く（決定記録 0135）ため、宛先では見分けられない。
    夜間に critical の発火と解除が critical のまま届き、同じ夜間の warning は届かないことで、
    重大度ごとの方針（0001 D-04 / 0013 §2.5）が解除にも保たれていることを確かめる。
    """
    slack = Recorder("slack")
    sender = router(config, SimulatedClock(NIGHT_MS), slack=slack)
    assert sender.notify(notification(severity="critical", rule_id="SENSOR_FAULT")) is True
    assert sender.notify(notification(severity="warning")) is False
    assert (
        sender.notify(notification(severity="critical", rule_id="SENSOR_FAULT", state="resolved"))
        is True
    )
    assert sender.notify(notification(severity="warning", state="resolved")) is False

    assert [(item.rule_id, item.severity, item.state) for item in slack.sent] == [
        ("SENSOR_FAULT", AlertSeverity.CRITICAL, "firing"),
        ("SENSOR_FAULT", AlertSeverity.CRITICAL, "resolved"),
    ]


def test_invalid_timezone_fails_at_load(tmp_path):
    """綴り違いを最初の warning まで持ち越さない。

    持ち越すと、その例外は取り込み側で「1サンプルの破棄」として記録され、
    **そのアラートは二度と通知されない**（発火の遷移はもう出ないため）。
    """
    text = (
        Path(NOTIFY_PATH)
        .read_text(encoding="utf-8")
        .replace('timezone: "Asia/Tokyo"', 'timezone: "Asia/Tokyoo"')
    )
    path = tmp_path / "notify.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="タイムゾーン"):
        NotifyConfig.from_yaml(path)


# ---------------------------------------------------------------- ダッシュボードの場所（#307）

EXAMPLE_DASHBOARD = "https://dashboard.example.invalid/"


def test_dashboard_url_env_overrides_yaml():
    """導入先のアドレスは環境変数で与える。public なリポジトリの config に書けない（ルール 10）。"""
    config = NotifyConfig.from_yaml(NOTIFY_PATH, environ={DASHBOARD_URL_ENV: EXAMPLE_DASHBOARD})
    assert config.dashboard_url == EXAMPLE_DASHBOARD
    lines = notification(dashboard_url=config.dashboard_url).as_text().splitlines()
    assert lines[-2] == EXAMPLE_DASHBOARD, "URL の後は ID の行（決定記録 0142 §2.2）"


def test_dashboard_url_without_env_keeps_yaml():
    assert NotifyConfig.from_yaml(NOTIFY_PATH, environ={}).dashboard_url == "http://127.0.0.1:8000/"


def test_dashboard_url_empty_env_is_unset():
    """`.env.example` を写したままの空の行で既定を潰さない。"""
    config = NotifyConfig.from_yaml(NOTIFY_PATH, environ={DASHBOARD_URL_ENV: ""})
    assert config.dashboard_url == "http://127.0.0.1:8000/"


def test_dashboard_url_reads_process_environment(monkeypatch):
    """`environ` を省くとプロセスの環境変数を読む（daemon / report / escalate の起動時）。"""
    monkeypatch.setenv(DASHBOARD_URL_ENV, "http://192.0.2.10:8000/")
    assert NotifyConfig.from_yaml(NOTIFY_PATH).dashboard_url == "http://192.0.2.10:8000/"


@pytest.mark.parametrize(
    "value",
    [
        "dashboard.example.invalid",  # scheme が無い
        "/airflow.html",  # 相対
        "ftp://dashboard.example.invalid/",
        "javascript:alert(1)",
        "http://",  # ホストが無い
        "https:///path",
        "http://dashboard.example.invalid:port/",  # ポートが数字でない
        " https://dashboard.example.invalid/",
        "https://dashboard.example.invalid/ x",
        " ",
    ],
)
def test_dashboard_url_env_rejects_malformed(value):
    """壊れたリンクを最初の通知で知ることにしない。起動時に落とす。"""
    with pytest.raises(ValueError, match=DASHBOARD_URL_ENV) as raised:
        NotifyConfig.from_yaml(NOTIFY_PATH, environ={DASHBOARD_URL_ENV: value})
    if value.strip():
        # 導入先のアドレスをログに残さない
        assert value.strip() not in str(raised.value)
        assert raised.value.__cause__ is None


def test_dashboard_url_in_yaml_is_checked(tmp_path):
    text = (
        Path(NOTIFY_PATH)
        .read_text(encoding="utf-8")
        .replace('dashboard_url: "http://127.0.0.1:8000/"', 'dashboard_url: "127.0.0.1:8000"')
    )
    path = tmp_path / "notify.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="dashboard_url"):
        NotifyConfig.from_yaml(path, environ={})
    # 環境変数で上書きするなら config の値は使わないので問わない
    assert (
        NotifyConfig.from_yaml(path, environ={DASHBOARD_URL_ENV: EXAMPLE_DASHBOARD}).dashboard_url
        == EXAMPLE_DASHBOARD
    )


# ---------------------------------------------------------------- 説明の後追い（#38）


def test_explanation_follows_a_delivered_alert(config):
    """説明は**発火を届けた相手にだけ**、抑制を通さず後から続く（#38）。"""
    slack = Recorder()
    clock = SimulatedClock(NOON_MS)
    sender = router(config, clock, slack=slack)
    sender.notify(notification())

    clock.advance_to_ms(NOON_MS + 30_000)  # 抑制の間隔より短い
    assert sender.notify(notification(kind="explanation", body="VERIFIED\n✓ ...")) is True
    assert [item.kind for item in slack.sent] == ["alert", "explanation"]


def test_explanation_is_not_sent_when_the_alert_was_suppressed(config):
    """届けていない発火の説明だけを送らない。"""
    slack = Recorder()
    sender = router(config, SimulatedClock(NIGHT_MS), slack=slack)
    assert sender.notify(notification(severity="warning")) is False
    assert sender.notify(notification(kind="explanation", body="x")) is False


def test_explanation_body_is_used_as_is(config):
    text = "VERIFIED（測定値・閾値判定）\n✓ [L2] 6.20（閾値 5.00）"
    body = notification(kind="explanation", body=text).as_text()
    assert text in body
    assert "🔎" in body


def test_explanation_survives_an_alert_that_resolved_first(config):
    """**解除が先に来ても説明は届く**（#38 のレビュー指摘）。

    生成は最長120秒かかる。短命なアラートでは解除が先に来るが、
    発火を届けた事実は変わらない。連投の抑制（解除で消える）とは別に持つ。
    """
    slack = Recorder()
    clock = SimulatedClock(NOON_MS)
    sender = router(config, clock, slack=slack)
    sender.notify(notification())

    clock.advance_to_ms(NOON_MS + 20_000)
    assert sender.notify(notification(state="resolved")) is True
    clock.advance_to_ms(NOON_MS + 40_000)  # LLM の生成が終わった
    assert sender.notify(notification(kind="explanation", body="x")) is True
    assert [item.kind for item in slack.sent] == ["alert", "alert", "explanation"]


def test_explanation_is_sent_once_per_firing(config):
    """1回の発火につき説明は1回だけ（二重に鳴らさない）。"""
    sender = router(config, SimulatedClock(NOON_MS))
    sender.notify(notification())
    assert sender.notify(notification(kind="explanation", body="x")) is True
    assert sender.notify(notification(kind="explanation", body="x")) is False


# ---------------------------------------------------------------- 秘匿情報をログに出さない（#305）

SECRET_WEBHOOK = (
    "https://hooks.slack.com/services/TFAKE/BFAKE/secret-token-for-test"  # pragma: allowlist secret
)


@pytest.mark.parametrize(
    "respond",
    [
        lambda request: httpx.Response(404),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom", request=request)),
    ],
    ids=["http-status", "transport"],
)
def test_slack_failure_never_carries_the_webhook_url(respond):
    """httpx の例外の文には URL が入る。送信の失敗は URL を含まない例外に置き換える。"""
    client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(DeliveryError) as raised:
        SlackNotifier(SECRET_WEBHOOK, client=client).send(notification())
    assert "secret-token-for-test" not in str(raised.value)
    assert raised.value.__suppress_context__
    assert raised.value.__cause__ is None


def test_failed_delivery_log_does_not_leak_the_webhook_url(config):
    """`Router` は失敗を exc_info で残す。整形した行に URL が出ないこと。"""
    stream = io.StringIO()
    logs.configure("INFO", stream)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    sender = router(
        config,
        SimulatedClock(NOON_MS),
        slack=SlackNotifier(SECRET_WEBHOOK, client=client),
        stdout=Recorder("stdout"),
    )
    sender.notify(notification())
    text = stream.getvalue()
    assert "通知の送信に失敗した" in text
    assert "slack: HTTP 403" in text
    assert "secret-token-for-test" not in text


def test_successful_delivery_does_not_log_the_webhook_url(config):
    """httpx は info で URL を出す。`logs.configure` は httpx / httpcore を warning にする。"""
    stream = io.StringIO()
    logs.configure("INFO", stream)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    SlackNotifier(SECRET_WEBHOOK, client=client).send(notification())
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    assert "secret-token-for-test" not in stream.getvalue()
