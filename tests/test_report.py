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
from coldaisle.metrics import MetricCatalog
from coldaisle.notify.models import Notification
from coldaisle.report import (
    MINUTES_PER_DAY,
    DailyReport,
    ReportConfig,
    as_notification,
    build,
    main,
    run,
    send,
    write,
)
from coldaisle.store import AlertSeverity, SqliteStore
from conftest import CONFIG_DIR

DAY = date(2026, 8, 24)
TZ = ZoneInfo("Asia/Tokyo")
DAY_START_MS = 1_787_497_200_000
"""2026-08-24T00:00:00+09:00。"""

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
        output_dir=tmp_path / "reports",
        to=["recorder"],
        summarise=True,
        max_alerts=20,
        max_notification_chars=3500,
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


def alert(store, rule_id: str, *, minute: int, fired: bool = True, severity="warning") -> None:
    started = DAY_START_MS + minute * MINUTE_MS
    store.connection.execute(
        "INSERT INTO alerts (rule_id, severity, state, metric, started_ms, fired_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (rule_id, severity, "resolved", "air.room", started, started if fired else None),
    )
    store.connection.commit()


def report_for(store, catalog, day: date = DAY) -> DailyReport:
    return build(store, day=day, tz=TZ, catalog=catalog, max_alerts=20)


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
    run(store, _config_with(summarise=True), catalog, day=DAY, summariser=provider)
    assert "## 測定値" in provider.prompts[0]
    assert "## 所見" not in provider.prompts[0]


def _config_with(**overrides) -> ReportConfig:
    base = {
        "timezone": "Asia/Tokyo",
        "output_dir": Path("var/reports"),
        "to": [],
        "summarise": True,
        "max_alerts": 20,
        "max_notification_chars": 3500,
    }
    return ReportConfig(**{**base, **overrides})


def test_facts_come_before_the_summary(store, catalog):
    """**測定値が先、AI の所見は最後。** #38 と同じ並び。"""
    bucket(store, "air.room", 0, mean=26.0, ok=24)
    text = report_for(store, catalog).with_summary("大きな変化はありません。").as_markdown()
    assert text.index("## 測定値") < text.index("## 所見（AI 生成・未検証）")


# ---------------------------------------------------------------- 出力と送信


def test_file_keeps_the_full_text_even_when_the_notification_is_cut(store, catalog, tmp_path):
    """**全文はファイルに残る。** LINE は5000文字で切られる。"""
    for minute in range(MINUTES_PER_DAY):
        bucket(store, "air.room", minute, mean=25.0, ok=24)
    report = report_for(store, catalog).with_summary("あ" * 300)
    path = write(report, tmp_path / "reports")
    notification = as_notification(report, dashboard_url="http://x/", max_chars=500)
    assert len(notification.body) <= 560
    assert "…（全文は 2026-08-24.md）" in notification.body
    assert path.read_text(encoding="utf-8") == report.as_markdown()


def test_rewriting_the_same_day_is_idempotent(store, catalog, tmp_path):
    bucket(store, "air.room", 0, mean=26.0, ok=24)
    report = report_for(store, catalog)
    first = write(report, tmp_path / "reports")
    second = write(report, tmp_path / "reports")
    assert first == second
    assert first.read_text(encoding="utf-8") == report.as_markdown()


def test_notification_is_titled_as_a_report(store, catalog):
    notification = as_notification(
        report_for(store, catalog), dashboard_url="http://x/", max_chars=9999
    )
    assert notification.title == "📄 日次レポート 2026-08-24"
    assert notification.kind == "report"


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
        "timezone: Asia/Tokio\noutput_dir: var/reports\nto: []\nsummarise: false\n"
        "max_alerts: 20\nmax_notification_chars: 3500\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="タイムゾーン"):
        ReportConfig.from_yaml(path)


def test_shipped_config_is_valid():
    """**本番の設定ファイルが読めること。** 壊れていたら毎朝黙って落ちる。"""
    config = ReportConfig.from_yaml(CONFIG_DIR / "report.yaml")
    assert config.zone.key == "Asia/Tokyo"


def test_cli_writes_a_file_without_sending(tmp_path, rules, catalog):
    db = tmp_path / "cli.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(0)) as store:
        bucket(store, "air.room", 0, mean=26.0, ok=24)
    config_path = tmp_path / "report.yaml"
    config_path.write_text(
        f"timezone: Asia/Tokyo\noutput_dir: {tmp_path / 'out'}\nto: []\nsummarise: false\n"
        "max_alerts: 20\nmax_notification_chars: 3500\n",
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
                "--date",
                "2026-08-24",
                "--no-send",
            ]
        )
        == 0
    )
    assert "26.00" in (tmp_path / "out" / "2026-08-24.md").read_text(encoding="utf-8")
