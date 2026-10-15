"""Slack の通知に直近 60 分のグラフを添える（#334 / 決定記録 0147）。

要点:

- 本文を先に送り、グラフは後から**同じスレッド**へ（`thread_ts`）
- グラフの失敗・Bot の失敗で本文を止めない（Bot が落ちたら Webhook で送り直す）
- 添えるのは critical / warning の発火だけ。同じアラートは `repeat_after_s` に 1 回まで
- トークン・上げ先の URL をログ・例外・repr に出さない

**Slack の API は呼ばない。** `httpx.MockTransport` の偽物で確かめる。
トークンはテスト用の明らかな仮の値（実物の形にしない）。
"""

from __future__ import annotations

import io
import json
import logging
import struct
import threading
import zlib
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from coldaisle import logs
from coldaisle.alert_graph import (
    QUEUE_SIZE,
    AlertGraphWorker,
    GraphConfig,
    GraphRequest,
    load_graph_config,
    plan_graph,
    read_points,
    render_alert_graph,
)
from coldaisle.clock import SimulatedClock
from coldaisle.metrics import MetricCatalog
from coldaisle.notify import DisplayNames, Notification, NotifyConfig, Router
from coldaisle.notify.notifiers import SLACK_WEBHOOK_ENV, DeliveryError, SlackNotifier
from coldaisle.notify.notifiers import from_env as notifiers_from_env
from coldaisle.notify.png_chart import (
    SERIES_COLORS,
    THRESHOLD,
    Panel,
    Series,
    Threshold,
    _thin,
    render_png,
)
from coldaisle.notify.slack_bot import (
    SLACK_BOT_TOKEN_ENV,
    SLACK_CHANNEL_ENV,
    SlackApi,
    SlackBotNotifier,
)
from coldaisle.rules import RuleSet
from coldaisle.store import AlertSeverity, Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR

TOKEN = "test-bot-token-not-real"
CHANNEL = "C00000TEST"
UPLOAD_URL = "https://files.example.invalid/upload/signed-secret-part"
NOON_MS = 1_787_626_800_000  # 2026-08-25T12:00:00+09:00（昼）
NIGHT_MS = 1_787_670_000_000  # 2026-08-26T00:00:00+09:00（夜）
MINUTE_MS = 60_000
NOTIFY_CONFIG = NotifyConfig.from_yaml(CONFIG_DIR / "notify.yaml")
NAMES = DisplayNames.build(
    rules=RuleSet.from_yaml(CONFIG_DIR / "rules.yaml"),
    catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
    context_always=NOTIFY_CONFIG.context_always,
)
"""本文の「現在値:」と同じ選び方（0142 §2.2a）。デーモンと同じ組み立て。"""


@pytest.fixture(scope="module")
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")


@pytest.fixture(scope="module")
def rule_set() -> RuleSet:
    return RuleSet.from_yaml(CONFIG_DIR / "rules.yaml")


@pytest.fixture(scope="module")
def graph_config(catalog) -> GraphConfig:
    config = load_graph_config(CONFIG_DIR / "notify-graph.yaml")
    assert config is not None, "リポジトリの設定は有効"
    return config


class FakeSlack:
    """Slack の Web API の偽物。受けた要求を覚え、決まった応答を返す。"""

    def __init__(self, *, fail: dict[str, httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._fail = fail or {}
        self._ts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for key, response in self._fail.items():
            if key in url:
                return response
        if url.endswith("/chat.postMessage"):
            self._ts += 1
            return httpx.Response(200, json={"ok": True, "ts": f"1787626800.00010{self._ts}"})
        if url.endswith("/files.getUploadURLExternal"):
            return httpx.Response(
                200, json={"ok": True, "upload_url": UPLOAD_URL, "file_id": "F00000TEST"}
            )
        if url == UPLOAD_URL:
            return httpx.Response(200, text="OK")
        if url.endswith("/files.completeUploadExternal"):
            return httpx.Response(200, json={"ok": True, "files": [{"id": "F00000TEST"}]})
        return httpx.Response(404)

    def calls(self) -> list[str]:
        return [
            str(r.url).rsplit("/", 1)[-1] if "slack.com" in str(r.url) else "upload"
            for r in self.requests
        ]


def api(fake: FakeSlack) -> SlackApi:
    return SlackApi(TOKEN, client=httpx.Client(transport=httpx.MockTransport(fake)))


def firing(severity: str = "warning", **kwargs) -> Notification:
    defaults = {
        "rule_id": "AIRFLOW_DEGRADED",
        "severity": AlertSeverity(severity),
        "state": "firing",
        "metric": "d.gpu_delta",
        "value": 21.0,
        "detail": None,
        "dashboard_url": "http://127.0.0.1:8000/",
    }
    return Notification(**{**defaults, **kwargs})


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "graph.db", rules=rules, clock=SimulatedClock(NOON_MS)) as opened:
        yield opened


def fill(store: SqliteStore, *, end_ms: int = NOON_MS, minutes: int = 70) -> None:
    """GPU の排気・吸気・室温を 10 s ごとに。排気は最後の 10 分で上がる。"""
    samples = []
    for i in range(minutes * 6):
        ts_ms = end_ms - minutes * MINUTE_MS + i * 10_000
        rising = max(0, i - (minutes - 10) * 6) * 0.2
        samples.append(
            Sample(
                ts_ms=ts_ms,
                readings=(
                    Reading(metric="air.gpu_exhaust", value=45.0 + rising, quality=Quality.OK),
                    Reading(metric="air.gpu_intake", value=28.0, quality=Quality.OK),
                    Reading(metric="air.room", value=25.0, quality=Quality.OK),
                ),
            )
        )
    store.insert_samples(samples)


def pixels(png: bytes) -> tuple[int, int, bytes]:
    """自前の PNG（フィルタ 0・RGB 8 bit）を読み戻す。"""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    offset = 8
    idat = b""
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", png[offset + 8 + length : offset + 12 + length])[0]
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF
        if kind == b"IDAT":
            idat += data
        offset += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 3 + 1
    assert len(raw) == stride * height
    assert all(raw[row * stride] == 0 for row in range(height))
    body = b"".join(raw[row * stride + 1 : (row + 1) * stride] for row in range(height))
    return width, height, body


def has_color(body: bytes, color: tuple[int, int, int]) -> bool:
    target = bytes(color)
    return any(body[i : i + 3] == target for i in range(0, len(body), 3))


# ---------------------------------------------------------------- 描画（標準ライブラリだけ）


def test_png_is_valid_and_deterministic():
    """同じ入力からは同じ bytes。閾値の赤と系列の色が描かれている（0147 §2.3 / §2.5）。"""
    points = tuple((NOON_MS - 3_600_000 + i * 10_000, 30.0 + i * 0.01) for i in range(360))
    panels = [
        Panel("C", (Series("air.gpu_intake", points),), (Threshold(40.0, "thr 40"),)),
        Panel("%", (Series("gpu.0.fan_speed", points),)),
    ]
    kwargs = {
        "start_ms": NOON_MS - 3_600_000,
        "end_ms": NOON_MS,
        "width": 960,
        "panel_height": 300,
        "gap_ms": 30_000,
        "max_points": 4000,
    }
    first = render_png(panels, **kwargs)
    assert render_png(panels, **kwargs) == first
    width, height, body = pixels(first)
    assert (width, height) == (960, 600), "単位ごとに 1 面"
    assert has_color(body, THRESHOLD)
    assert has_color(body, SERIES_COLORS[0])


def test_png_draws_an_isolated_point_and_breaks_lines_at_gaps():
    """離れた点は結ばない。1 点だけでも描ける（欠けを欠けとして見せる）。"""
    lone = render_png(
        [Panel("C", (Series("air.room", ((NOON_MS - 60_000, 25.0),)),))],
        start_ms=NOON_MS - 3_600_000,
        end_ms=NOON_MS,
        width=960,
        panel_height=300,
        gap_ms=30_000,
        max_points=4000,
    )
    assert has_color(pixels(lone)[2], SERIES_COLORS[0])


def test_thin_keeps_the_latest_point():
    points = tuple((i, float(i)) for i in range(10_001))
    thinned = _thin(points, 1000)
    assert len(thinned) <= 1000
    assert thinned[-1] == points[-1], "直近は必ず残す"
    assert list(thinned) == sorted(thinned)


def test_render_refuses_nothing_to_draw():
    with pytest.raises(ValueError):
        render_png(
            [],
            start_ms=0,
            end_ms=1,
            width=960,
            panel_height=300,
            gap_ms=1,
            max_points=10,
        )


# ---------------------------------------------------------------- 何を描くか


def test_plan_for_a_derived_metric_adds_its_inputs_and_room(rule_set, catalog):
    """派生値は材料も、最後に室温（通知の `context_always`。0142 §2.2a と同じ選び方）。

    閾値は派生値の面に（0147 §2.3）。
    """
    plan = plan_graph(
        "AIRFLOW_DEGRADED",
        "d.gpu_delta",
        rules=rule_set,
        names=NAMES,
    )
    assert plan.metrics == ("d.gpu_delta", "air.gpu_exhaust", "air.gpu_intake", "air.room")
    assert [t.value for t in plan.thresholds["d.gpu_delta"]] == [
        rule_set.airflow_degraded.threshold
    ]


def test_plan_for_hot_and_slow_puts_each_threshold_on_its_metric(rule_set, catalog):
    plan = plan_graph(
        "GPU_FAN_BELOW_CURVE",
        "gpu.0.core",
        rules=rule_set,
        names=NAMES,
    )
    rule = rule_set.gpu_fan_below_curve
    assert plan.metrics == ("gpu.0.core", "gpu.0.fan_speed", "air.room")
    assert plan.thresholds["gpu.0.core"][0].value == rule.core_threshold
    assert plan.thresholds["gpu.0.fan_speed"][0].value == rule.fan_below


@pytest.mark.parametrize(
    ("rule_id", "metric", "expected_metrics", "expected_lines"),
    [
        ("RAPID_RISE", "air.gpu_intake", ("air.gpu_intake", "air.room"), {}),
        ("ROOM_HIGH", "air.room", ("air.room",), {"air.room": 1}),
        (
            "HUMIDITY_OUT_OF_RANGE",
            "air.room_humidity",
            ("air.room_humidity", "air.room"),
            {"air.room_humidity": 2},
        ),
        ("SENSOR_MISSING", "air.top_exhaust", ("air.top_exhaust", "air.room"), {}),
        ("NOT_A_RULE", "air.gpu_exhaust", ("air.gpu_exhaust", "air.room"), {}),
    ],
)
def test_plan_by_rule_kind(rule_set, catalog, rule_id, metric, expected_metrics, expected_lines):
    """変化率・連続欠け・知らないルールには閾値の線を引かない（値の軸の閾値ではない）。"""
    plan = plan_graph(rule_id, metric, rules=rule_set, names=NAMES)
    assert plan.metrics == expected_metrics
    assert {m: len(lines) for m, lines in plan.thresholds.items()} == expected_lines


def test_derived_points_are_computed_from_ok_inputs_only(store, catalog):
    """派生値は保存しないので材料を突き合わせて引く。`ok` でない時刻は除く（0002 §2.2）。"""
    store.insert_samples(
        [
            Sample(
                ts_ms=NOON_MS - 20_000,
                readings=(
                    Reading(metric="air.gpu_exhaust", value=50.0, quality=Quality.OK),
                    Reading(metric="air.gpu_intake", value=30.0, quality=Quality.OK),
                ),
            ),
            Sample(
                ts_ms=NOON_MS - 10_000,
                readings=(
                    Reading(metric="air.gpu_exhaust", value=-127.0, quality=Quality.SUSPECT),
                    Reading(metric="air.gpu_intake", value=30.0, quality=Quality.OK),
                ),
            ),
        ]
    )
    points = read_points(
        store, "d.gpu_delta", catalog, start_ms=NOON_MS - MINUTE_MS, end_ms=NOON_MS, limit=100
    )
    assert points == ((NOON_MS - 20_000, 20.0),)


def test_render_alert_graph_reads_the_last_window(store, rule_set, catalog, graph_config):
    fill(store)
    graph = render_alert_graph(
        GraphRequest("AIRFLOW_DEGRADED", "d.gpu_delta", CHANNEL, "1.0", NOON_MS),
        store,
        rules=rule_set,
        catalog=catalog,
        names=NAMES,
        config=graph_config,
    )
    assert graph is not None
    width, height, body = pixels(graph.png)
    assert width == graph_config.width
    assert height == graph_config.panel_height, "4 本とも °C なので 1 面"
    assert has_color(body, THRESHOLD)
    assert graph.comment.startswith("直近60分: GPUが与えた熱（d.gpu_delta）=青")
    assert "室温（air.room）=黄" in graph.comment
    assert "閾値 20 = 赤の破線" in graph.comment
    assert graph.filename == f"airflow_degraded_{NOON_MS}.png"


def test_render_alert_graph_without_values_is_none(store, rule_set, catalog, graph_config):
    """描く値が無ければ添えない（本文は届いている）。"""
    graph = render_alert_graph(
        GraphRequest("ROOM_HIGH", "air.room", CHANNEL, "1.0", NOON_MS),
        store,
        rules=rule_set,
        catalog=catalog,
        names=NAMES,
        config=graph_config,
    )
    assert graph is None


def test_broken_graph_config_disables_graphs_only(tmp_path, catalog, caplog):
    """壊れた設定は起動を止めず、グラフだけを無効にする（0147 §2.7）。"""
    broken = tmp_path / "notify-graph.yaml"
    broken.write_text("enabled: true\nwindow_s: -1\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="coldaisle.notify"):
        assert load_graph_config(broken) is None
        assert load_graph_config(tmp_path / "missing.yaml") is None
    assert len(caplog.records) == 2

    # 室温は通知の context_always から取る。グラフの設定に持たせると拒む（2 か所に持たない）
    duplicated = tmp_path / "duplicated.yaml"
    duplicated.write_text(
        (CONFIG_DIR / "notify-graph.yaml").read_text(encoding="utf-8")
        + "always_metrics: [air.room]\n",
        encoding="utf-8",
    )
    assert load_graph_config(duplicated) is None

    disabled = tmp_path / "disabled.yaml"
    disabled.write_text(
        (CONFIG_DIR / "notify-graph.yaml")
        .read_text(encoding="utf-8")
        .replace("enabled: true", "enabled: false"),
        encoding="utf-8",
    )
    assert load_graph_config(disabled) is None


# ---------------------------------------------------------------- Slack の API（偽物）


def test_post_message_uses_the_bot_token_and_returns_ts():
    fake = FakeSlack()
    ts = api(fake).post_message(CHANNEL, "本文")
    assert ts == "1787626800.000101"
    request = fake.requests[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {"channel": CHANNEL, "text": "本文"}


def test_upload_goes_to_the_thread():
    """`files.getUploadURLExternal` → 本体 → `files.completeUploadExternal`（`thread_ts`）。"""
    fake = FakeSlack()
    api(fake).upload_to_thread(
        channel=CHANNEL,
        thread_ts="1787626800.000101",
        filename="g.png",
        content=b"\x89PNG-bytes",
        title="題",
        comment="直近60分",
    )
    assert fake.calls() == [
        "files.getUploadURLExternal",
        "upload",
        "files.completeUploadExternal",
    ]
    reserve = parse_qs(fake.requests[0].content.decode())
    assert reserve == {"filename": ["g.png"], "length": [str(len(b"\x89PNG-bytes"))]}
    upload = fake.requests[1]
    assert upload.content == b"\x89PNG-bytes"
    assert "Authorization" not in upload.headers, "上げ先の URL にトークンを送らない"
    complete = parse_qs(fake.requests[2].content.decode())
    assert complete["channel_id"] == [CHANNEL]
    assert complete["thread_ts"] == ["1787626800.000101"]
    assert complete["initial_comment"] == ["直近60分"]
    assert json.loads(complete["files"][0]) == [{"id": "F00000TEST", "title": "題"}]


@pytest.mark.parametrize(
    ("fail", "expected"),
    [
        (
            {
                "chat.postMessage": httpx.Response(
                    200, json={"ok": False, "error": "not_in_channel"}
                )
            },
            "slack: chat.postMessage: not_in_channel",
        ),
        (
            {"chat.postMessage": httpx.Response(200, json={"ok": False, "error": TOKEN})},
            "slack: chat.postMessage: unknown",
        ),
        ({"chat.postMessage": httpx.Response(500)}, "slack: chat.postMessage: HTTP 500"),
        ({"upload/": httpx.Response(403)}, "slack: upload: HTTP 403"),
    ],
)
def test_failures_never_carry_the_token_or_the_upload_url(fail, expected):
    """#305 と同じ。例外の文は API の名前・状態コード・Slack の error の符号だけ（0147 §2.6）。"""
    client = api(FakeSlack(fail=fail))
    with pytest.raises(DeliveryError) as raised:
        if "upload/" in fail:
            client.upload_to_thread(
                channel=CHANNEL,
                thread_ts="1.0",
                filename="g.png",
                content=b"x",
                title="t",
                comment="c",
            )
        else:
            client.post_message(CHANNEL, "本文")
    assert str(raised.value) == expected
    assert raised.value.__cause__ is None
    # httpx の例外（URL を含みうる）から作ったものは連鎖を切ってある
    assert raised.value.__context__ is None or raised.value.__suppress_context__
    assert TOKEN not in repr(client)
    assert "signed-secret-part" not in str(raised.value)


def test_network_error_is_a_delivery_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url} with {TOKEN}")

    client = SlackApi(TOKEN, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(DeliveryError) as raised:
        client.post_message(CHANNEL, "本文")
    assert str(raised.value) == "slack: chat.postMessage: ConnectError"


# ---------------------------------------------------------------- 宛先と送り直し


def test_bot_failure_falls_back_to_the_webhook(caplog):
    """Bot が落ちても、Webhook があれば本文は届く。グラフは添えない（0147 §2.1）。"""
    fake = FakeSlack(
        fail={"chat.postMessage": httpx.Response(200, json={"ok": False, "error": "invalid_auth"})}
    )
    webhook_requests: list[httpx.Request] = []

    def webhook(request: httpx.Request) -> httpx.Response:
        webhook_requests.append(request)
        return httpx.Response(200)

    posted: list[str] = []
    notifier = SlackBotNotifier(
        api(fake),
        CHANNEL,
        fallback=SlackNotifier(
            "https://hooks.example.invalid/x",
            client=httpx.Client(transport=httpx.MockTransport(webhook)),
        ),
        on_posted=lambda n, c, ts: posted.append(ts),
    )
    with caplog.at_level(logging.WARNING, logger="coldaisle.notify"):
        notifier.send(firing())
    assert len(webhook_requests) == 1
    assert posted == []
    assert TOKEN not in caplog.text


def test_bot_failure_without_webhook_raises():
    fake = FakeSlack(fail={"chat.postMessage": httpx.Response(500)})
    with pytest.raises(DeliveryError):
        SlackBotNotifier(api(fake), CHANNEL).send(firing())


def test_on_posted_failure_does_not_fail_the_text():
    def broken(notification: Notification, channel: str, ts: str) -> None:
        raise RuntimeError("グラフの依頼が壊れた")

    SlackBotNotifier(api(FakeSlack()), CHANNEL, on_posted=broken).send(firing())


def test_from_env_prefers_the_bot_and_keeps_the_webhook_as_fallback(monkeypatch):
    monkeypatch.setenv(SLACK_BOT_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(SLACK_CHANNEL_ENV, CHANNEL)
    monkeypatch.setenv(SLACK_WEBHOOK_ENV, "https://hooks.example.invalid/x")
    slack = notifiers_from_env()["slack"]
    assert isinstance(slack, SlackBotNotifier)
    assert repr(slack) == "SlackBotNotifier(fallback=True)"
    assert TOKEN not in repr(slack.api)


@pytest.mark.parametrize("present", [SLACK_BOT_TOKEN_ENV, SLACK_CHANNEL_ENV])
def test_from_env_with_only_one_of_the_two_keeps_the_webhook(monkeypatch, present):
    """片方だけなら Bot を作らず警告する（LINE と同じ。0013 §2.1）。値はログに出さない。"""
    monkeypatch.delenv(SLACK_BOT_TOKEN_ENV, raising=False)
    monkeypatch.delenv(SLACK_CHANNEL_ENV, raising=False)
    monkeypatch.setenv(present, TOKEN)
    monkeypatch.setenv(SLACK_WEBHOOK_ENV, "https://hooks.example.invalid/x")
    stream = io.StringIO()
    logs.configure("INFO", stream)
    slack = notifiers_from_env()["slack"]
    assert isinstance(slack, SlackNotifier)
    assert "片方だけ" in stream.getvalue()
    assert TOKEN not in stream.getvalue()


# ---------------------------------------------------------------- 添える条件と非同期


class FakeUploader:
    def __init__(self, *, block: threading.Event | None = None, fail: bool = False) -> None:
        self.uploads: list[dict[str, object]] = []
        self._block = block
        self._fail = fail
        self.started = threading.Event()

    def upload_to_thread(self, **kwargs: object) -> None:
        self.started.set()
        if self._block is not None:
            self._block.wait(5)
        if self._fail:
            raise DeliveryError("slack: files.completeUploadExternal: not_in_channel")
        self.uploads.append(kwargs)


def worker(store, rule_set, catalog, graph_config, uploader, clock) -> AlertGraphWorker:
    return AlertGraphWorker(
        uploader=uploader,
        open_reader=lambda: store,
        rules=rule_set,
        catalog=catalog,
        names=NAMES,
        config=graph_config,
        clock=clock,
        repeat_after_s=1800.0,
    )


@pytest.mark.parametrize(
    "notification",
    [
        firing(state="resolved"),
        firing(severity="info"),
        firing(kind="explanation"),
        firing(rule_id="SENSOR_FAULT", metric=None),
    ],
    ids=["resolved", "info", "explanation", "no-metric"],
)
def test_only_critical_and_warning_firings_get_a_graph(
    store, rule_set, catalog, graph_config, notification
):
    uploader = FakeUploader()
    graphs = worker(store, rule_set, catalog, graph_config, uploader, SimulatedClock(NOON_MS))
    assert graphs.offer(notification, CHANNEL, "1.0") is False
    assert uploader.uploads == []


def test_same_alert_gets_one_graph_per_repeat_interval(store, rule_set, catalog, graph_config):
    """発火と解除を往復するアラートで画像を連投しない（0147 §2.4）。"""
    fill(store)
    clock = SimulatedClock(NOON_MS)
    uploader = FakeUploader()
    graphs = worker(store, rule_set, catalog, graph_config, uploader, clock)
    assert graphs.offer(firing(severity="critical"), CHANNEL, "1.0") is True
    clock.advance_to_ms(NOON_MS + 29 * MINUTE_MS)
    assert graphs.offer(firing(severity="critical"), CHANNEL, "2.0") is False
    assert graphs.offer(firing(rule_id="RECIRCULATION", metric="d.intake_rise"), CHANNEL, "3.0")
    clock.advance_to_ms(NOON_MS + 30 * MINUTE_MS)
    assert graphs.offer(firing(severity="critical"), CHANNEL, "4.0") is True
    assert [u["thread_ts"] for u in uploads_of(uploader)] == ["1.0", "3.0", "4.0"]


def uploads_of(uploader: FakeUploader) -> list[dict[str, object]]:
    return uploader.uploads


def test_upload_failure_is_logged_and_dropped(store, rule_set, catalog, graph_config, caplog):
    fill(store)
    uploader = FakeUploader(fail=True)
    graphs = worker(store, rule_set, catalog, graph_config, uploader, SimulatedClock(NOON_MS))
    with caplog.at_level(logging.WARNING, logger="coldaisle.notify"):
        assert graphs.offer(firing(), CHANNEL, "1.0") is True
    assert graphs.failed == 1
    assert "グラフを添えられなかった" in caplog.text


class FlakyUploader(FakeUploader):
    """1 回目だけ失敗する。"""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def upload_to_thread(self, **kwargs: object) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise DeliveryError("slack: upload: HTTP 500")
        self.uploads.append(kwargs)


def test_failed_graph_does_not_suppress_the_next_firing(store, rule_set, catalog, graph_config):
    """抑えるのは**実際に上げた**グラフだけ（0147 §2.4。Codex レビュー P2）。

    上げに失敗したら予約を外し、`repeat_after_s` の間の次の発火には添える。上げられた後は抑える。
    """
    fill(store)
    clock = SimulatedClock(NOON_MS)
    uploader = FlakyUploader()
    graphs = worker(store, rule_set, catalog, graph_config, uploader, clock)
    assert graphs.offer(firing(), CHANNEL, "1.0") is True
    assert graphs.failed == 1
    clock.advance_to_ms(NOON_MS + MINUTE_MS)
    assert graphs.offer(firing(), CHANNEL, "2.0") is True, "失敗の後の発火には添える"
    assert [u["thread_ts"] for u in uploader.uploads] == ["2.0"]
    clock.advance_to_ms(NOON_MS + 2 * MINUTE_MS)
    assert graphs.offer(firing(), CHANNEL, "3.0") is False, "上げた後は repeat_after_s まで抑える"


def test_graph_without_values_does_not_suppress_the_next_firing(
    store, rule_set, catalog, graph_config
):
    """描く値が無くて添えなかったときも予約を外す。"""
    clock = SimulatedClock(NOON_MS)
    uploader = FakeUploader()
    graphs = worker(store, rule_set, catalog, graph_config, uploader, clock)
    assert graphs.offer(firing(), CHANNEL, "1.0") is True
    assert uploader.uploads == []
    fill(store, end_ms=NOON_MS + MINUTE_MS)
    clock.advance_to_ms(NOON_MS + MINUTE_MS)
    assert graphs.offer(firing(), CHANNEL, "2.0") is True
    assert [u["thread_ts"] for u in uploader.uploads] == ["2.0"]


def test_failed_upload_log_has_neither_token_nor_upload_url(store, rule_set, catalog, graph_config):
    """整形したログ（exc_info 込み）にトークンも署名入りの上げ先も出ない（0147 §2.6）。"""
    fill(store)
    stream = io.StringIO()
    logs.configure("INFO", stream)
    graphs = worker(
        store,
        rule_set,
        catalog,
        graph_config,
        api(FakeSlack(fail={"upload/": httpx.Response(500)})),
        SimulatedClock(NOON_MS),
    )
    graphs.offer(firing(), CHANNEL, "1.0")
    text = stream.getvalue()
    assert "グラフを添えられなかった" in text
    assert "slack: upload: HTTP 500" in text
    assert TOKEN not in text
    assert "signed-secret-part" not in text


def test_reader_failure_is_logged_and_dropped(rule_set, catalog, graph_config):
    def cannot_open():
        raise OSError("DB を開けない")

    graphs = AlertGraphWorker(
        uploader=FakeUploader(),
        open_reader=cannot_open,
        rules=rule_set,
        catalog=catalog,
        names=NAMES,
        config=graph_config,
        clock=SimulatedClock(NOON_MS),
        repeat_after_s=1800.0,
    )
    assert graphs.offer(firing(), CHANNEL, "1.0") is True
    assert graphs.failed == 1


def test_offer_never_waits_for_the_upload(tmp_path, rules, rule_set, catalog, graph_config):
    """上げている間も `offer` はすぐ戻り、溢れたら捨てる（0147 §2.2）。"""
    db = tmp_path / "graph.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(NOON_MS)) as seeded:
        fill(seeded)
    release = threading.Event()
    uploader = FakeUploader(block=release)
    clock = SimulatedClock(NOON_MS)
    graphs = AlertGraphWorker(
        uploader=uploader,
        # グラフのスレッドが自分の接続を開く（0004 §2.8）
        open_reader=lambda: SqliteStore(db, rules=rules, clock=clock),
        rules=rule_set,
        catalog=catalog,
        names=NAMES,
        config=graph_config,
        clock=clock,
        repeat_after_s=1800.0,
    )
    graphs.start()
    try:
        assert graphs.offer(firing(rule_id="R0"), CHANNEL, "0")
        assert uploader.started.wait(5), "1 枚目を上げ始めた（ここで止まる）"
        for index in range(QUEUE_SIZE):
            assert graphs.offer(firing(rule_id=f"R{index + 1}"), CHANNEL, str(index + 1))
        assert graphs.offer(firing(rule_id="OVERFLOW"), CHANNEL, "x") is False
        assert graphs.dropped == 1
    finally:
        release.set()
        graphs.stop()
    assert graphs.uploaded >= 1


def test_router_sends_text_first_then_graph_in_the_same_thread(
    store, rule_set, catalog, graph_config
):
    """本文は `chat.postMessage`、グラフはその `ts` のスレッドへ。解除には添えない。"""
    fill(store)
    fake = FakeSlack()
    slack_api = api(fake)
    clock = SimulatedClock(NOON_MS)
    bot = SlackBotNotifier(slack_api, CHANNEL)
    graphs = AlertGraphWorker(
        uploader=slack_api,
        open_reader=lambda: store,
        rules=rule_set,
        catalog=catalog,
        names=NAMES,
        config=graph_config,
        clock=clock,
        repeat_after_s=1800.0,
    )
    bot.attach(graphs.offer)
    router = Router(
        config=NotifyConfig.from_yaml(CONFIG_DIR / "notify.yaml"),
        notifiers={"slack": bot},
        clock=clock,
    )
    assert router.notify(firing())
    assert router.notify(firing(state="resolved"))
    assert fake.calls() == [
        "chat.postMessage",
        "files.getUploadURLExternal",
        "upload",
        "files.completeUploadExternal",
        "chat.postMessage",
    ]
    complete = parse_qs(fake.requests[3].content.decode())
    assert complete["thread_ts"] == ["1787626800.000101"], "発火の本文のスレッド"


def test_night_suppressed_warning_gets_neither_text_nor_graph(
    store, rule_set, catalog, graph_config
):
    """夜間に送らなかった warning にはグラフも添えない（送った通知にだけ続く）。"""
    fake = FakeSlack()
    clock = SimulatedClock(NIGHT_MS)
    bot = SlackBotNotifier(api(fake), CHANNEL)
    graphs = worker(store, rule_set, catalog, graph_config, FakeUploader(), clock)
    bot.attach(graphs.offer)
    router = Router(
        config=NotifyConfig.from_yaml(CONFIG_DIR / "notify.yaml"),
        notifiers={"slack": bot},
        clock=clock,
    )
    assert router.notify(firing()) is False
    assert fake.requests == []


# ---------------------------------------------------------------- 組み立て


def test_daemon_builds_graphs_only_with_the_bot(tmp_path, rules, rule_set, catalog):
    from coldaisle.daemon import Config, _alert_graph
    from coldaisle.notify.notifiers import StdoutNotifier

    config = Config(db=tmp_path / "x.db", notify_graph=CONFIG_DIR / "notify-graph.yaml")
    clock = SimulatedClock(NOON_MS)
    webhook_only = {"stdout": StdoutNotifier(), "slack": SlackNotifier("https://x.invalid/")}
    assert (
        _alert_graph(config, webhook_only, rules, clock, rule_set, catalog, NOTIFY_CONFIG) is None
    )

    bot = SlackBotNotifier(api(FakeSlack()), CHANNEL)
    built = _alert_graph(config, {"slack": bot}, rules, clock, rule_set, catalog, NOTIFY_CONFIG)
    assert isinstance(built, AlertGraphWorker)

    broken = Config(db=tmp_path / "x.db", notify_graph=tmp_path / "missing.yaml")
    assert (
        _alert_graph(broken, {"slack": bot}, rules, clock, rule_set, catalog, NOTIFY_CONFIG) is None
    )


def test_repo_graph_config_matches_the_decision(graph_config):
    """0147 §2.3 / §2.7 の既定。"""
    assert graph_config.window_s == 3600
    assert "air.room" in NOTIFY_CONFIG.context_always, "室温は通知の context_always から"


def test_example_env_lists_the_bot_variables():
    text = Path(CONFIG_DIR.parent / ".env.example").read_text(encoding="utf-8")
    assert f"{SLACK_BOT_TOKEN_ENV}=\n" in text
    assert f"{SLACK_CHANNEL_ENV}=\n" in text
