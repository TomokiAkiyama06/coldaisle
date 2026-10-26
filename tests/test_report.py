"""日次レポート（#25 / FR-506）。

守る線は3つ。

1. **表の数値はロールアップから機械的に出る**（生データを読まない）
2. **表に無い数値が所見に現れたら、その所見は捨てる**（#38 と同じ）
3. **AI が落ちていても、表だけのレポートが届く**
"""

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coldaisle.ai.provider import ChatResult, UnavailableProvider
from coldaisle.ai.summary import summarise
from coldaisle.clock import SimulatedClock
from coldaisle.llm_exclusions import LlmExclusions
from coldaisle.metrics import MetricCatalog
from coldaisle.notify.models import Notification
from coldaisle.report import (
    MINUTES_PER_DAY,
    DailyReport,
    MetricLine,
    ReportConfig,
    as_notification,
    as_thread,
    build,
    main,
    run,
    send,
)
from coldaisle.rules import RuleSet
from coldaisle.store import AlertSeverity, SqliteStore
from conftest import CONFIG_DIR

DAY = date(2026, 8, 24)
TZ = ZoneInfo("Asia/Tokyo")
EXCLUSIONS = LlmExclusions.from_yaml(CONFIG_DIR / "report.yaml")
DAY_START_MS = 1_787_497_200_000
"""2026-08-24T00:00:00+09:00。"""

HIGHLIGHTS = (
    "air.room",
    "gpu.0.core",
    "cpu.package",
    "board.connector_12v2x6",
    "power.gpu.0",
    "power.cpu.package",
)
MINUTE_MS = 60_000
DAY_MS = MINUTES_PER_DAY * MINUTE_MS


class FakeProvider:
    """決まった応答を返す。**外部へは出ない。**"""

    def __init__(self, text: str = "", available: bool = True) -> None:
        self.text = text
        self.available = available
        self.prompts: list[str] = []

    def chat(self, messages, *, thinking=None, tools=None) -> ChatResult:
        self.prompts.append("\n".join(m.content for m in messages))
        return ChatResult(available=self.available, text=self.text)

    def probe(self) -> ChatResult:
        return ChatResult(available=self.available)


class Recorder:
    name = "recorder"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Notification] = []
        self.fail = fail

    def send(self, notification: Notification) -> None:
        if self.fail:
            raise RuntimeError("宛先が落ちている")
        self.sent.append(notification)


@pytest.fixture
def catalog() -> MetricCatalog:
    """**本番と同じ `config/metrics.yaml`** を読む。"""
    return MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")


@pytest.fixture
def config(tmp_path) -> ReportConfig:
    return ReportConfig(
        timezone="Asia/Tokyo",
        to=["recorder"],
        summarise=True,
        max_alerts=20,
        max_notification_chars=3500,
        thread_title="📄 {month}月{day}日の振り返り",
        highlight_metrics=list(HIGHLIGHTS),
        table_font=tmp_path / "no-such-font.ttc",
        table_font_size=28,
    )


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "report.db", rules=rules, clock=SimulatedClock(0)) as opened:
        yield opened


def bucket(
    store,
    metric: str,
    minute: int,
    *,
    minimum=None,
    maximum=None,
    mean=None,
    ok=1,
    expected=None,
    day_start_ms: int = DAY_START_MS,
) -> None:
    """1分バケットを直接書く。**ロールアップの出力形をそのまま使う。**"""
    store.connection.execute(
        "INSERT OR REPLACE INTO readings_1m (metric, bucket_ms, min_value, max_value, "
        "mean_value, ok_value_count, row_count, expected_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            metric,
            day_start_ms + minute * MINUTE_MS,
            minimum if minimum is not None else mean,
            maximum if maximum is not None else mean,
            mean,
            ok,
            ok,
            expected,
        ),
    )
    store.connection.commit()


def alert(
    store,
    rule_id: str,
    *,
    minute: int,
    fired: bool = True,
    fired_minute: int | None = None,
    severity="warning",
) -> None:
    started = DAY_START_MS + minute * MINUTE_MS
    at = DAY_START_MS + (minute if fired_minute is None else fired_minute) * MINUTE_MS
    store.connection.execute(
        "INSERT INTO alerts (rule_id, severity, state, metric, started_ms, fired_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (rule_id, severity, "resolved", "air.room", started, at if fired else None),
    )
    store.connection.commit()


def report_for(store, catalog, day: date = DAY) -> DailyReport:
    return build(store, day=day, tz=TZ, catalog=catalog, max_alerts=20, exclusions=EXCLUSIONS)


def line_for(report: DailyReport, metric: str):
    return next(line for line in report.metrics if line.metric == metric)


# ---------------------------------------------------------------- 集計（受入基準）


def test_mean_is_weighted_by_sample_count(store, catalog):
    """**件数で重み付けする。** 単純平均だと値の少ないバケットが同じ重さになる。"""
    bucket(store, "air.room", 0, mean=20.0, ok=1)
    bucket(store, "air.room", 1, mean=30.0, ok=99)
    line = line_for(report_for(store, catalog), "air.room")
    assert line.mean == pytest.approx((20.0 * 1 + 30.0 * 99) / 100)


def test_min_and_max_come_from_bucket_extremes(store, catalog):
    """**平均の最小ではなく、生の最小。** ロールアップが持っている値を使う。"""
    bucket(store, "air.room", 0, mean=25.0, minimum=21.0, maximum=29.0, ok=24)
    bucket(store, "air.room", 1, mean=26.0, minimum=24.0, maximum=33.0, ok=24)
    line = line_for(report_for(store, catalog), "air.room")
    assert (line.minimum, line.maximum) == (21.0, 33.0)


def test_previous_day_is_compared(store, catalog):
    bucket(store, "air.room", 0, mean=26.0, ok=24)
    bucket(store, "air.room", 0, mean=24.0, ok=24, day_start_ms=DAY_START_MS - DAY_MS)
    line = line_for(report_for(store, catalog), "air.room")
    assert (line.mean, line.prev_mean) == (26.0, 24.0)
    assert "+2.00" in report_for(store, catalog).as_markdown()


def test_missing_ratio_uses_expected_count(store, catalog):
    """**届かなかったサンプルを数える。**

    `stats()` の欠測率は届いた行だけが母数で下限値になる（決定記録 0002 §2.8）。
    ロールアップの `expected_count` を使うと本当の欠測率が出る。
    """
    bucket(store, "air.room", 0, mean=25.0, ok=12, expected=24)
    line = line_for(report_for(store, catalog), "air.room")
    assert line.missing_ratio == pytest.approx(0.5)
    assert "50.0%" in report_for(store, catalog).as_markdown()


def test_unknown_expectations_are_left_out_of_the_missing_ratio(store, catalog):
    """**期待サンプル数を知らないバケットを母数に混ぜない**（#25 のレビュー指摘）。

    起動バナーを受け取る前のバケットは `expected_count` が NULL。0 として足すと
    その分の `ok` だけが分子に残り、欠測率が実際より低く（0% に）出る。
    """
    bucket(store, "air.room", 0, mean=25.0, ok=24)  # 期待サンプル数を知らない
    bucket(store, "air.room", 1, mean=25.0, ok=12, expected=24)
    line = line_for(report_for(store, catalog), "air.room")
    assert line.missing_ratio == pytest.approx(0.5)  # 0% ではない
    assert (line.missing_minutes, line.minutes) == (1, 2)


def test_a_partial_basis_is_written_in_the_table(store, catalog):
    """母数が1日ぶんに満たないなら、**その分数も書く**（黙って割合だけ出さない）。"""
    bucket(store, "air.room", 0, mean=25.0, ok=24)
    bucket(store, "air.room", 1, mean=25.0, ok=12, expected=24)
    assert "50.0%（1分）" in report_for(store, catalog).as_markdown()


def test_a_full_basis_is_written_plainly(store, catalog):
    bucket(store, "air.room", 0, mean=25.0, ok=12, expected=24)
    assert "| 50.0% |" in report_for(store, catalog).as_markdown()


def test_missing_ratio_is_absent_without_expected_count(store, catalog):
    """期待サンプル数を知らないなら**推測しない**（`—` を出す）。"""
    bucket(store, "air.room", 0, mean=25.0, ok=12)
    assert line_for(report_for(store, catalog), "air.room").missing_ratio is None


# ---------------------------------------------------------------- カウンタ


def test_counters_are_summed_not_averaged(store, catalog):
    """`unit: count` は積み上げ。**メトリクス名で分岐しない**（設定が決める）。"""
    bucket(store, "sys.queue_drops", 0, mean=3.0, ok=1)
    bucket(store, "sys.queue_drops", 5, mean=2.0, ok=1)
    line = line_for(report_for(store, catalog), "sys.queue_drops")
    assert (line.counter, line.total) == (True, 5.0)
    assert "| 5 | — |" in report_for(store, catalog).as_markdown()  # 前日は不明


def test_counters_have_no_missing_ratio(store, catalog):
    """**事象が起きたときだけ書かれる値**に欠測率を出さない（常に100%になる）。"""
    bucket(store, "sys.queue_drops", 0, mean=3.0, ok=1, expected=24)
    assert line_for(report_for(store, catalog), "sys.queue_drops").missing_ratio is None


def test_counters_are_not_listed_among_measurements(store, catalog):
    bucket(store, "sys.queue_drops", 0, mean=3.0, ok=1)
    text = report_for(store, catalog).as_markdown()
    assert text.index("## カウンタ") > text.index("## アラート") or "## 測定値" not in text


# ---------------------------------------------------------------- アラート


def test_only_fired_alerts_are_counted(store, catalog):
    """**pending のまま消えたものは誰にも届いていない。**"""
    alert(store, "RECIRCULATION", minute=10)
    alert(store, "RECIRCULATION", minute=20)
    alert(store, "SENSOR_MISSING", minute=30, fired=False)
    report = report_for(store, catalog)
    assert report.alert_total == 2
    assert report.alerts == (
        type(report.alerts[0])(rule_id="RECIRCULATION", severity=AlertSeverity.WARNING, count=2),
    )


def test_alert_table_shows_rule_names_and_japanese_severity(store, catalog):
    """表のルールは `<名前>（<RULE_ID>）`、重大度は日本語（決定記録 0142 §2.6）。

    LLM へ渡す版も同じ形で作る。名前の無いルールは ID のまま。
    """
    alert(store, "RECIRCULATION", minute=10)
    alert(store, "RAPID_RISE", minute=20, severity="critical")
    alert(store, "UNLISTED_RULE", minute=30)
    labels = RuleSet.from_yaml(CONFIG_DIR / "rules.yaml").labels()
    report = build(
        store,
        day=DAY,
        tz=TZ,
        catalog=catalog,
        max_alerts=20,
        exclusions=EXCLUSIONS,
        rule_labels=labels,
    )
    for text in (report.as_markdown(), report.facts_markdown()):
        assert "| 排気の再循環（RECIRCULATION） | 警告 | 1 |" in text
        assert "| 温度の急上昇（RAPID_RISE） | 重大 | 1 |" in text
        assert "| UNLISTED_RULE | 警告 | 1 |" in text
    assert report.as_markdown().startswith("# 日次レポート 2026-08-24"), "見出しは変えない"


def test_alerts_are_compared_with_the_previous_day(store, catalog):
    alert(store, "RECIRCULATION", minute=10)
    store.connection.execute(
        "INSERT INTO alerts (rule_id, severity, state, metric, started_ms, fired_ms) "
        "VALUES ('RECIRCULATION', 'warning', 'resolved', 'air.room', ?, ?)",
        (DAY_START_MS - DAY_MS, DAY_START_MS - DAY_MS),
    )
    store.connection.commit()
    report = report_for(store, catalog)
    assert (report.alert_total, report.prev_alert_total) == (1, 1)
    assert "発火 1 件（前日 1 件）" in report.as_markdown()


def test_alerts_outside_the_day_are_not_counted(store, catalog):
    alert(store, "RECIRCULATION", minute=MINUTES_PER_DAY + 1)
    assert report_for(store, catalog).alert_total == 0


def test_the_day_boundary_applies_to_the_firing_time(store, catalog):
    """**通知が飛んだ日に数える**（#25 のレビュー指摘）。

    23:55 に条件が成立して 00:03 に発火したアラートは、受け取った人にとって
    翌日の出来事である。条件の成立時刻で切ると前日の件数に入り、
    「昨日は静かだったのに件数が1件ある」という食い違いになる。
    """
    alert(store, "RECIRCULATION", minute=-5, fired_minute=3)  # 前日 23:55 成立 → 00:03 発火
    alert(store, "SENSOR_MISSING", minute=MINUTES_PER_DAY - 5, fired_minute=MINUTES_PER_DAY + 3)
    report = report_for(store, catalog)
    assert report.alert_total == 1
    assert report.alerts[0].rule_id == "RECIRCULATION"
    assert report.prev_alert_total == 0  # 前日側にも入らない（発火は当日）


# ---------------------------------------------------------------- 欠けの報告


def test_a_day_without_data_is_reported_as_an_anomaly(store, catalog):
    """**このレポート自体が異常の知らせである。** 空で黙るほうが危ない。"""
    report = report_for(store, catalog)
    assert report.has_data is False
    assert "この日のデータがありません" in report.as_markdown()


def test_incomplete_rollup_is_stated(store, catalog):
    """**集計が未完了なら、そう書く。** 半日ぶんの平均を1日の平均として出さない。"""
    for minute in range(60):
        bucket(store, "air.room", minute, mean=25.0, ok=24)
    text = report_for(store, catalog).as_markdown()
    assert "## 集計の欠け" in text
    assert f"| 60/{MINUTES_PER_DAY} |" in text  # 沈黙していた時間が行に出る


def test_complete_day_says_nothing_about_gaps(store, catalog):
    for minute in range(MINUTES_PER_DAY):
        bucket(store, "air.room", minute, mean=25.0, ok=24)
    text = report_for(store, catalog).as_markdown()
    assert "## 集計の欠け" not in text
    assert f"| {MINUTES_PER_DAY}/{MINUTES_PER_DAY} |" in text


def test_coverage_is_separate_from_the_missing_ratio(store, catalog):
    """**装置ごと沈黙していた時間は欠測率に出ない。** 収録の分数に出る。

    期待サンプル数を知らないと欠測率は出せない（`—`）が、バケットが無いことは
    分かる。この2つを1つの列にまとめると、どちらの欠けか読めなくなる。
    """
    bucket(store, "air.room", 0, mean=25.0, ok=24)
    line = line_for(report_for(store, catalog), "air.room")
    assert (line.missing_ratio, line.minutes) == (None, 1)
    assert "| — | 1/1440 |" in report_for(store, catalog).as_markdown()


def test_metrics_outside_the_catalog_are_not_dropped(store, catalog):
    bucket(store, "air.unknown_probe", 0, mean=25.0, ok=24)
    assert line_for(report_for(store, catalog), "air.unknown_probe").label == "air.unknown_probe"


# ---------------------------------------------------------------- 所見（受入基準）


def test_summary_is_rejected_when_it_invents_a_number(store, catalog):
    """**表に無い数値は、モデルが作った数値である。**"""
    report = report_for(store, catalog)
    assert summarise(FakeProvider("最高 32.8 度まで上がりました"), report.facts_markdown()) is None


def test_summary_survives_when_numbers_come_from_the_table(store, catalog):
    bucket(store, "air.room", 0, mean=26.0, minimum=26.0, maximum=26.0, ok=24)
    facts = report_for(store, catalog).facts_markdown()
    assert summarise(FakeProvider("平均 26.00 度で推移しました"), facts) is not None


def test_summary_without_numbers_is_preferred(store, catalog):
    facts = report_for(store, catalog).facts_markdown()
    text = "室温は前日よりわずかに高めで推移しています。大きな変化はありません。"
    assert summarise(FakeProvider(text), facts) == text


@pytest.mark.parametrize(
    "text",
    [
        "再循環である確率は高いです",
        "確度としては中程度です",
        "87% の可能性で再循環しています",
    ],
)
def test_summary_with_confidence_is_rejected(text, store, catalog):
    """#38 と同じ判断。**確度の表現は丸ごと捨てる。**"""
    assert summarise(FakeProvider(text), report_for(store, catalog).facts_markdown()) is None


@pytest.mark.parametrize("text", ["## 所見\n室温が高い", "- 室温が高い", "| 表 | を |"])
def test_summary_that_builds_structure_is_rejected(text, store, catalog):
    """**レポートの構造はコードが持つ。** 見出しや表をモデルに作らせない。"""
    assert summarise(FakeProvider(text), report_for(store, catalog).facts_markdown()) is None


def test_unavailable_model_yields_no_summary(store, catalog):
    """受入基準: **AI が落ちていても表だけのレポートが届く。**"""
    assert summarise(UnavailableProvider(), "facts") is None


def test_model_receives_the_table_not_the_raw_series(store, catalog):
    """生の時系列をプロンプトに入れない（FR-504）。"""
    bucket(store, "air.room", 0, mean=26.0, ok=24)
    provider = FakeProvider("大きな変化はありません。")
    run(
        store,
        _config_with(summarise=True),
        catalog,
        day=DAY,
        summariser=provider,
        exclusions=EXCLUSIONS,
    )
    assert "## 測定値" in provider.prompts[0]
    assert "## 所見" not in provider.prompts[0]


def _config_with(**overrides) -> ReportConfig:
    base = {
        "timezone": "Asia/Tokyo",
        "to": [],
        "summarise": True,
        "max_alerts": 20,
        "max_notification_chars": 3500,
        "thread_title": "📄 {month}月{day}日の振り返り",
        "highlight_metrics": list(HIGHLIGHTS),
        "table_font": Path("/nonexistent/font.ttc"),
        "table_font_size": 28,
    }
    return ReportConfig(**{**base, **overrides})


def test_facts_come_before_the_summary(store, catalog):
    """**測定値が先、AI の所見は最後。** #38 と同じ並び。"""
    bucket(store, "air.room", 0, mean=26.0, ok=24)
    text = report_for(store, catalog).with_summary("大きな変化はありません。").as_markdown()
    assert text.index("## 測定値") < text.index("## 所見（AI 生成・未検証）")


# ---------------------------------------------------------------- 出力と送信


def test_long_single_message_is_cut_with_the_dashboard_note(store, catalog):
    """Webhook・LINE の 1 通は長ければ切り、続きはダッシュボードで（決定記録 0171 §2.4）。"""
    for minute in range(MINUTES_PER_DAY):
        bucket(store, "air.room", minute, mean=25.0, ok=24)
    report = report_for(store, catalog).with_summary("あ" * 300)
    notification = as_notification(report, dashboard_url="http://x/", max_chars=500)
    assert len(notification.body) <= 530
    assert notification.body.endswith("…（続きはダッシュボードで）")
    assert ".md" not in notification.body


def test_notification_is_titled_as_a_report(store, catalog):
    notification = as_notification(
        report_for(store, catalog), dashboard_url="http://x/", max_chars=9999
    )
    assert notification.title == "📄 日次レポート 2026-08-24"
    assert notification.kind == "report"


def test_thread_title_is_month_and_day_without_year_or_padding():
    """親の題は対象日の月日だけ。0 埋めしない・年を付けない（決定記録 0171）。"""
    shipped = ReportConfig.from_yaml(CONFIG_DIR / "report.yaml")
    assert shipped.thread_title_for(date(2026, 10, 25)) == "📄 10月25日の振り返り"
    assert shipped.thread_title_for(date(2026, 3, 5)) == "📄 3月5日の振り返り"


@pytest.mark.parametrize("template", ["{mon}月{day}日", "{0}", "{month", "   "])
def test_config_rejects_a_broken_thread_title(template):
    with pytest.raises(ValueError, match="thread_title"):
        _config_with(thread_title=template)


def test_thread_does_not_change_the_single_message(store, catalog, config):
    """スレッドの形は `as_text()` に入らない。Webhook・LINE・stdout の本文と長さは今までどおり。"""
    bucket(store, "air.room", 0, mean=26.0, ok=24)
    report = report_for(store, catalog)
    plain = as_notification(report, dashboard_url="http://x/", max_chars=500)
    threaded = as_notification(
        report,
        dashboard_url="http://x/",
        max_chars=500,
        thread=as_thread(report, config, dashboard_url="http://x/"),
    )
    assert plain.thread is None
    assert threaded.thread is not None
    assert threaded.thread.title == "📄 8月24日の振り返り"
    assert threaded.as_text() == plain.as_text()
    assert "振り返り" not in threaded.as_text()


def test_non_slack_destinations_get_the_same_single_message(store, catalog, config):
    """Slack 以外（LINE・stdout など）は 1 通のまま。親の題も要点も送らない。"""
    recorder = Recorder()
    report = report_for(store, catalog)
    notification = as_notification(
        report, dashboard_url="", max_chars=9999, thread=as_thread(report, config, dashboard_url="")
    )
    assert send(notification, {"line": recorder}, ["line"]) == 1
    assert len(recorder.sent) == 1
    assert recorder.sent[0].as_text() == notification.as_text()


# ---------------------------------------------------------------- 要点と表（0171 §2.2 / §2.3）


def _lead_fixture(store) -> None:
    for minute in range(MINUTES_PER_DAY):
        bucket(store, "air.room", minute, mean=25.0, maximum=27.5, ok=24, expected=24)
    bucket(store, "gpu.0.core", 0, mean=60.0, maximum=71.25, ok=12, expected=24)
    bucket(store, "power.gpu.0", 0, mean=None, ok=0, expected=24)
    alert(store, "CPU_HOT", minute=10, severity="warning")
    alert(store, "CPU_HOT", minute=20 - MINUTES_PER_DAY, severity="warning")  # 前日


def test_lead_has_alerts_summary_peaks_missing_and_link_in_order(store, catalog):
    _lead_fixture(store)
    report = report_for(store, catalog).with_summary("室温は安定していた。")
    lead = report.lead_text(HIGHLIGHTS, "https://dashboard.example.invalid/")
    order = [
        "*アラート*",
        "発火 1 件（前日 1 件）",
        "*所見（AI 生成・未検証）*",
        "室温は安定していた。",
        "*主な値（最高）*",
        "・室温 27.5 °C",
        "・GPUコア温度 71.2 °C",
        "*欠測*",
        "https://dashboard.example.invalid/",
    ]
    positions = [lead.index(text) for text in order]
    assert positions == sorted(positions)
    # 値の無い metric（GPU 消費電力は届いたサンプルが無い・CPU は行が無い）は主な値に書かない
    assert "GPU消費電力 " not in lead.split("*欠測*")[0]
    assert "CPU Package温度" not in lead
    missing_line = lead.split("*欠測*\n", 1)[1].splitlines()[0]
    assert missing_line == "GPUコア温度 50.0%、GPU消費電力 100.0%"
    assert "|" not in lead, "表は画像で送る"


def _report_with_missing(*lines: MetricLine) -> DailyReport:
    return DailyReport(
        day=DAY, timezone="Asia/Tokyo", metrics=lines, alerts=(), alert_total=0, prev_alert_total=0
    )


def _gauge(label: str, ratio: float, *, missing_minutes: int = MINUTES_PER_DAY) -> MetricLine:
    return MetricLine(
        metric=f"x.{len(label)}",
        label=label,
        unit="C",
        counter=False,
        minimum=20.0,
        maximum=30.0,
        mean=25.0,
        missing_ratio=ratio,
        missing_minutes=missing_minutes,
        minutes=MINUTES_PER_DAY,
    )


def _missing_section(report: DailyReport) -> list[str]:
    lead = report.lead_text(HIGHLIGHTS, "")
    if "*欠測*" not in lead:
        return []
    return lead.split("*欠測*\n", 1)[1].splitlines()[0].split("、")


def test_lead_missing_uses_the_same_rounding_as_the_table():
    """表で 0.0% に見えるもの（数サンプル・浮動小数の端数）は要点に出さない（0171 §2.2）。"""
    report = _report_with_missing(
        _gauge("わずか", 0.000004),  # 0.0004 %（表では 0.0%）
        _gauge("端数", 1 - 1439.9999999 / 1440),
        _gauge("負の端数", 1 - 1440.0000001 / 1440),
        _gauge("切り捨て", 0.00049),  # 0.049 %（表では 0.0%）
        _gauge("千分の一", 0.001),  # 0.1 %
        _gauge("全部", 1.0),
    )
    assert _missing_section(report) == ["千分の一 0.1%", "全部 100.0%"]
    table = report.table_markdown()
    assert "| わずか（x.3） | 20.00 | 25.00 | 30.00 | — | 0.0% |" in table
    assert "-0.0%" not in table


def test_lead_missing_carries_the_minutes_note_like_the_table():
    report = _report_with_missing(_gauge("一部", 0.5, missing_minutes=600))
    assert _missing_section(report) == ["一部 50.0%（600分）"]
    assert "50.0%（600分）" in report.table_markdown()


def test_lead_has_no_missing_section_when_everything_shows_zero():
    report = _report_with_missing(_gauge("わずか", 0.0004 / 100))
    assert _missing_section(report) == []


def test_lead_never_cuts_the_summary(store, catalog):
    """所見は切らない（デモでは 3500 文字の上限で所見が消えた。0171 §2）。"""
    _lead_fixture(store)
    summary = "所見の文。" * 1000
    lead = report_for(store, catalog).with_summary(summary).lead_text(HIGHLIGHTS, "")
    assert summary in lead


def test_lead_names_ongoing_alerts_with_severity(store, catalog):
    fired = DAY_START_MS + 1430 * MINUTE_MS
    store.connection.execute(
        "INSERT INTO alerts (rule_id, severity, state, metric, started_ms, fired_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("CPU_HOT", "critical", "firing", "cpu.package", fired, fired),
    )
    store.connection.commit()
    lead = report_for(store, catalog).lead_text(HIGHLIGHTS, "")
    assert "日の終わりに続いている:" in lead
    assert "CPU_HOT" in lead and "[重大]" in lead


def test_lead_without_data_says_so(store, catalog):
    lead = report_for(store, catalog).lead_text(HIGHLIGHTS, "http://127.0.0.1:8000/")
    assert lead.startswith("*この日のデータがありません。*")
    assert "127.0.0.1" not in lead, "ループバックのリンクは載せない（0142 §2.2）"


def test_shipped_highlights_are_the_agreed_default():
    """既定の主な値（0171 §2.2）。すべて `config/metrics.yaml` にある。"""
    config = ReportConfig.from_yaml(CONFIG_DIR / "report.yaml")
    assert config.highlight_metrics == list(HIGHLIGHTS)
    catalog = MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")
    assert set(config.highlight_metrics) <= set(catalog.metrics)


def test_table_rows_use_labels_and_mute_rows_without_values(store, catalog):
    _lead_fixture(store)
    header, rows = report_for(store, catalog).table_rows()
    assert header[0] == "メトリクス" and len(header) == 7
    by_name = {row.cells[0]: row for row in rows}
    assert not by_name["室温"].muted
    assert by_name["GPU消費電力"].muted
    assert by_name["室温"].cells[3] == "27.50"


def test_thread_without_a_font_sends_the_table_as_text(store, catalog, config):
    """フォントのファイルが無ければ画像を付けず、表を Markdown で送る（0171 §2.3）。"""
    _lead_fixture(store)
    thread = as_thread(report_for(store, catalog), config, dashboard_url="")
    assert thread.table_image is None
    assert thread.table_text.startswith("## 測定値")
    assert "| 室温（air.room） |" in thread.table_text


def test_a_failing_destination_does_not_stop_the_others(store, catalog):
    """**1日1回しか出ない。** 1つ落ちても残りへ送る。"""
    good, bad = Recorder(), Recorder(fail=True)
    notification = as_notification(report_for(store, catalog), dashboard_url="", max_chars=9999)
    assert send(notification, {"good": good, "bad": bad}, ["bad", "good"]) == 1
    assert len(good.sent) == 1


def test_an_unknown_destination_is_logged_not_raised(store, catalog, caplog):
    notification = as_notification(report_for(store, catalog), dashboard_url="", max_chars=9999)
    assert send(notification, {}, ["slack"]) == 0
    assert "宛先が設定されていない" in caplog.text


# ---------------------------------------------------------------- 設定と CLI


def test_config_rejects_a_misspelled_timezone(tmp_path):
    path = tmp_path / "report.yaml"
    path.write_text(
        "timezone: Asia/Tokio\nto: []\nsummarise: false\n"
        'max_alerts: 20\nmax_notification_chars: 3500\nthread_title: "{month}月{day}日"\n'
        "highlight_metrics: [air.room]\ntable_font: /nonexistent/font.ttc\ntable_font_size: 28\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="タイムゾーン"):
        ReportConfig.from_yaml(path)


def test_shipped_config_is_valid():
    """**本番の設定ファイルが読めること。** 壊れていたら毎朝黙って落ちる。"""
    config = ReportConfig.from_yaml(CONFIG_DIR / "report.yaml")
    assert config.zone.key == "Asia/Tokyo"


def test_cli_does_not_write_a_report_file(tmp_path, rules, catalog, monkeypatch, capsys):
    """ファイルに保存しない。`--print` で標準出力へ出す（決定記録 0171 §2.4）。"""
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "cli.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(0)) as store:
        bucket(store, "air.room", 0, mean=26.0, ok=24)
    config_path = tmp_path / "report.yaml"
    config_path.write_text(
        "timezone: Asia/Tokyo\nto: []\nsummarise: false\n"
        'max_alerts: 20\nmax_notification_chars: 3500\nthread_title: "{month}月{day}日"\n'
        "highlight_metrics: [air.room]\ntable_font: /nonexistent/font.ttc\ntable_font_size: 28\n",
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--db",
                str(db),
                "--config",
                str(config_path),
                "--metrics",
                str(CONFIG_DIR / "metrics.yaml"),
                "--notify",
                str(CONFIG_DIR / "notify.yaml"),
                "--quality-rules",
                str(CONFIG_DIR / "quality.yaml"),
                "--rules",
                str(CONFIG_DIR / "rules.yaml"),
                "--date",
                "2026-08-24",
                "--no-send",
                "--print",
            ]
        )
        == 0
    )
    assert "26.00" in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cli.db", "report.yaml"]


@pytest.mark.skipif(
    not ReportConfig.from_yaml(CONFIG_DIR / "report.yaml").table_font.exists(),
    reason="日本語のフォントが無い（CI）",
)
def test_thread_with_the_shipped_font_attaches_the_table_image(store, catalog):
    _lead_fixture(store)
    config = ReportConfig.from_yaml(CONFIG_DIR / "report.yaml")
    report = report_for(store, catalog)
    first = as_thread(report, config, dashboard_url="").table_image
    second = as_thread(report, config, dashboard_url="").table_image
    assert first is not None and second is not None
    assert first.content.startswith(b"\x89PNG")
    assert first.content == second.content
