"""LLM へ GPU の Fan の事実を渡さない（決定記録 0131 §2.4a / §2.6）。

**LLM へ渡すものは、除外の一覧を当てた後の集合だけから作る**（件数・時刻・欠けの注記など
派生した値を含む）。人が見る決定論的な出力は、除く前の集合から作る。

経路は 0131 §2.4a の表のとおり: 日次レポートの所見・AI ツールの窓口・アラートの自動の説明・
Server Health の AI 要約・案件資料。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from coldaisle import report as report_module
from coldaisle.ai import Explainer, ToolRegistry
from coldaisle.clock import SimulatedClock
from coldaisle.daemon import Daemon
from coldaisle.escalate import EscalationRules, build_packet, evaluate
from coldaisle.ingest import Calibration, Normalizer
from coldaisle.llm_exclusions import LlmExclusions, LlmExclusionsError
from coldaisle.metrics import MetricCatalog
from coldaisle.notify import Notification
from coldaisle.report import MINUTES_PER_DAY, ReportConfig, build, run
from coldaisle.rules import RuleSet, Transition
from coldaisle.store import (
    AlertRecord,
    AlertSeverity,
    AlertState,
    Quality,
    Reading,
    Sample,
    SqliteStore,
)
from conftest import CONFIG_DIR
from test_ai_explain import ANSWER
from test_ai_explain import FakeProvider as ExplainProvider
from test_server_health_api import NOW_MS as HEALTH_NOW_MS
from test_server_health_api import FakeSummarizer, _app, _populate

REPORT_YAML = CONFIG_DIR / "report.yaml"
EXCLUSIONS = LlmExclusions.from_yaml(REPORT_YAML)
TZ = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 8, 24)
DAY_START_MS = 1_787_497_200_000
DAY_MS = 86_400_000
MINUTE_MS = 60_000
NOW_MS = DAY_START_MS + DAY_MS + 3_600_000  # 翌日の 01:00
POLICY = "gpu.0.fan_policy"
GPU_RULES = ("GPU_FAN_NOT_MANUAL", "GPU_FAN_BELOW_CURVE")


@pytest.fixture
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "llm.db", rules=rules, clock=SimulatedClock(NOW_MS)) as opened:
        yield opened


def put_alert(
    store: SqliteStore,
    rule_id: str,
    *,
    fired_ms: int,
    metric: str | None = "air.room",
    severity: str = "warning",
    resolved_ms: int | None = None,
) -> None:
    store.connection.execute(
        "INSERT INTO alerts (rule_id, severity, state, metric, started_ms, fired_ms, resolved_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            rule_id,
            severity,
            "firing" if resolved_ms is None else "resolved",
            metric,
            fired_ms - 30_000,
            fired_ms,
            resolved_ms,
        ),
    )
    store.connection.commit()


def bucket(store: SqliteStore, metric: str, minute: int, mean: float, *, day_offset: int = 0):
    store.connection.execute(
        "INSERT OR REPLACE INTO readings_1m (metric, bucket_ms, min_value, max_value, "
        "mean_value, ok_value_count, row_count, expected_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            metric,
            DAY_START_MS + day_offset * DAY_MS + minute * MINUTE_MS,
            mean,
            mean,
            mean,
            24,
            24,
            24,
        ),
    )
    store.connection.commit()


def daily(store, catalog, exclusions: LlmExclusions | None = EXCLUSIONS):
    return build(store, day=DAY, tz=TZ, catalog=catalog, max_alerts=20, exclusions=exclusions)


# ---------------------------------------------------------------- 一覧の形


def test_shipped_list_names_the_gpu_fan_policy_and_rules():
    assert EXCLUSIONS.excludes_metric(POLICY)
    assert EXCLUSIONS.excludes_metric("gpu.1.fan_policy")
    assert not EXCLUSIONS.excludes_metric("gpu.0.fan_speed"), (
        "0043 の fan speed は #301 で別に決める"
    )
    assert EXCLUSIONS.rule_ids == frozenset(GPU_RULES)


def test_shipped_report_config_still_loads_with_the_list():
    """`ReportConfig` は一覧を読まない（別の読み手）。一覧があっても読み込みは通る。"""
    assert ReportConfig.from_yaml(REPORT_YAML).summarise is True


@pytest.mark.parametrize(
    "text",
    [
        "llm_excluded_rules: []\n",  # キーの省略を「除くものが無い」と読まない
        "llm_excluded_metrics: []\n",
        "llm_excluded_metrics: gpu.*.fan_policy\nllm_excluded_rules: []\n",
        "llm_excluded_metrics: ['gpu.?.fan_policy']\nllm_excluded_rules: []\n",
        "llm_excluded_metrics: ['GPU.0.x']\nllm_excluded_rules: []\n",
        "llm_excluded_metrics: []\nllm_excluded_rules: [gpu_fan_not_manual]\n",
        "llm_excluded_metrics: []\nllm_excluded_rules: [1]\n",
        "- not a mapping\n",
        "llm_excluded_metrics: [\n",
    ],
)
def test_malformed_list_is_rejected(tmp_path, text):
    path = tmp_path / "report.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LlmExclusionsError):
        LlmExclusions.from_yaml(path)


def test_unreadable_file_is_rejected(tmp_path):
    with pytest.raises(LlmExclusionsError):
        LlmExclusions.from_yaml(tmp_path / "absent.yaml")


def test_derived_values_built_from_an_excluded_metric_are_excluded(catalog):
    exclusions = LlmExclusions(metric_patterns=("air.room",), rule_ids=frozenset())
    assert exclusions.excludes_metric_or_inputs("d.intake_rise", catalog)  # front_intake - room
    assert not exclusions.excludes_metric_or_inputs("d.gpu_delta", catalog)


# ---------------------------------------------------------------- 日次レポート


def test_facts_for_the_llm_drop_the_policy_and_gpu_fan_alerts(store, catalog):
    for minute in range(MINUTES_PER_DAY):
        bucket(store, "air.room", minute, 26.0)
        bucket(store, POLICY, minute, 0.0)
    put_alert(store, "RECIRCULATION", fired_ms=DAY_START_MS + 3_600_000, resolved_ms=NOW_MS - 1)
    put_alert(store, "GPU_FAN_NOT_MANUAL", fired_ms=DAY_START_MS + 7_200_000, metric=POLICY)

    report = daily(store, catalog)
    human = report.as_markdown()
    facts = report.facts_markdown()

    # 人が読む本文には出る（LLM を通さない、コードが作る表）
    assert POLICY in human
    assert "GPU_FAN_NOT_MANUAL" in human
    assert "発火 2 件（前日 0 件）" in human
    # LLM へ渡す facts には、メトリクスの行も、アラートの件数の行も、続いている節の行も無い
    assert "fan_policy" not in facts
    assert "GPU_FAN" not in facts
    assert "発火 1 件（前日 0 件）" in facts
    assert "RECIRCULATION" in facts


def test_counts_for_the_llm_are_recounted_after_exclusion(store, catalog):
    """件数・前日の件数・表は除いた後で数え直す。本文は除く前のまま。"""
    for index in range(2):
        put_alert(store, "RECIRCULATION", fired_ms=DAY_START_MS + index * 60_000, resolved_ms=1)
    for index in range(3):
        put_alert(
            store, "GPU_FAN_NOT_MANUAL", fired_ms=DAY_START_MS + index * 60_000, metric=POLICY
        )
    put_alert(store, "GPU_FAN_BELOW_CURVE", fired_ms=DAY_START_MS - DAY_MS + 60_000, metric="x")
    put_alert(store, "INTAKE_HIGH", fired_ms=DAY_START_MS - DAY_MS + 60_000, resolved_ms=1)

    report = daily(store, catalog)
    assert (report.alert_total, report.prev_alert_total) == (5, 2)
    assert report.llm_view is not None
    assert (report.llm_view.alert_total, report.llm_view.prev_alert_total) == (2, 1)
    assert [a.rule_id for a in report.llm_view.alerts] == ["RECIRCULATION"]
    assert "発火 2 件（前日 1 件）" in report.facts_markdown()
    assert "発火 5 件（前日 2 件）" in report.as_markdown()


def test_alert_pointing_at_an_excluded_metric_is_also_dropped(store, catalog):
    """除外のメトリクスを指すアラート（例: SENSOR_MISSING）も名前が漏れるので除く。"""
    put_alert(store, "SENSOR_MISSING", fired_ms=DAY_START_MS + 60_000, metric=POLICY)
    report = daily(store, catalog)
    assert report.alert_total == 1
    assert "SENSOR_MISSING" not in report.facts_markdown()
    assert "発火 0 件" in report.facts_markdown()


@pytest.mark.parametrize(
    ("kept", "excluded", "human_truncated", "llm_truncated"),
    [
        (2, 2, True, False),  # 除く前は上限に届くが、除いた後は届かない
        (3, 2, True, True),  # 除いた後でも上限ちょうど
        (2, 0, False, False),
    ],
)
def test_truncation_note_is_judged_after_exclusion(
    store, catalog, monkeypatch, kept, excluded, human_truncated, llm_truncated
):
    monkeypatch.setattr(report_module, "ALERT_SCAN_LIMIT", 3)
    for index in range(kept):
        put_alert(store, "RECIRCULATION", fired_ms=DAY_START_MS + index * 60_000, resolved_ms=1)
    for index in range(excluded):
        put_alert(
            store, "GPU_FAN_BELOW_CURVE", fired_ms=DAY_START_MS + (10 + index) * 60_000, metric="x"
        )
    report = daily(store, catalog)
    note = "3 件を超えたため打ち切った"
    assert (note in report.as_markdown()) is human_truncated
    assert (note in report.facts_markdown()) is llm_truncated
    assert report.llm_view is not None
    assert report.llm_view.alert_total == min(kept, 3)


def test_incomplete_note_for_the_llm_ignores_excluded_metrics(store, catalog):
    """`gpu.0.fan_policy` だけが欠けた日は、LLM 向けには「集計の欠け」を出さない。"""
    for minute in range(MINUTES_PER_DAY):
        bucket(store, "air.room", minute, 26.0)
    bucket(store, POLICY, 0, 1.0)  # 1 分しか無い
    report = daily(store, catalog)
    assert "## 集計の欠け" in report.as_markdown()
    assert "## 集計の欠け" not in report.facts_markdown()


def test_ongoing_alerts_section_lists_alerts_still_firing_at_the_end_of_the_day(store, catalog):
    """0131 §2.4: 型番の不一致は人が直すまで続く。発火は1回きりでも毎日出す。"""
    two_days_ago = DAY_START_MS - 2 * DAY_MS + 3_600_000
    put_alert(store, "GPU_FAN_NOT_MANUAL", fired_ms=two_days_ago, metric=POLICY)
    put_alert(store, "ROOM_HIGH", fired_ms=two_days_ago, resolved_ms=DAY_START_MS + DAY_MS + 1)
    put_alert(store, "INTAKE_HIGH", fired_ms=two_days_ago, resolved_ms=DAY_START_MS + 1)

    report = daily(store, catalog)
    human = report.as_markdown()
    assert report.alert_total == 0, "その日に発火した件数には出ない"
    assert "## 日の終わりに続いているアラート" in human
    section = human.split("## 日の終わりに続いているアラート")[1]
    assert "| GPU_FAN_NOT_MANUAL | warning | 2026-08-22 01:00 |" in section
    # 翌日に解除されたものも、その日の終わりには続いていた（作り直しても同じ）
    assert "ROOM_HIGH" in section
    assert "INTAKE_HIGH" not in section, "その日のうちに解除された"
    # LLM 向けにはルール名で分岐せず、一覧の rule_id だけを除く
    facts = report.facts_markdown()
    assert "ROOM_HIGH" in facts
    assert "GPU_FAN_NOT_MANUAL" not in facts


def test_report_without_a_valid_list_has_no_llm_view(store, catalog):
    report = daily(store, catalog, exclusions=None)
    assert report.llm_view is None
    with pytest.raises(ValueError, match="除外"):
        report.facts_markdown()


def test_summary_is_not_requested_without_a_valid_list(store, catalog):
    provider = ExplainProvider("大きな変化はありません。")
    config = ReportConfig.from_yaml(REPORT_YAML)
    report = run(store, config, catalog, day=DAY, summariser=provider, exclusions=None)
    assert provider.prompts == []
    assert report.summary is None


def test_cli_skips_the_summary_when_the_list_is_malformed(tmp_path, rules, monkeypatch):
    db = tmp_path / "cli.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(0)) as opened:
        bucket(opened, "air.room", 0, 26.0)
    config_path = tmp_path / "report.yaml"
    config_path.write_text(
        f"timezone: Asia/Tokyo\noutput_dir: {tmp_path / 'out'}\nto: []\nsummarise: true\n"
        "max_alerts: 20\nmax_notification_chars: 3500\nllm_excluded_metrics: oops\n"
        "llm_excluded_rules: []\n",
        encoding="utf-8",
    )
    called: list[Path] = []
    monkeypatch.setattr(report_module, "_summariser", lambda path, config: called.append(path))
    argv = [
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
        DAY.isoformat(),
        "--no-send",
    ]
    assert report_module.main(argv) == 0
    assert called == [], "一覧が不正なら所見の provider を作らない"
    assert (tmp_path / "out" / f"{DAY.isoformat()}.md").exists(), "表だけのレポートは出る"


# ---------------------------------------------------------------- AI ツール


@pytest.fixture
def tools(store, catalog) -> ToolRegistry:
    return ToolRegistry(
        store=store,
        catalog=catalog,
        rules=RuleSet.from_yaml(CONFIG_DIR / "rules.yaml"),
        clock=store.clock,
        exclusions=EXCLUSIONS,
    )


def reading(store: SqliteStore, ts_ms: int, metric: str, value: float | None) -> None:
    store.insert_sample(
        Sample(
            ts_ms=ts_ms,
            readings=(
                Reading(
                    metric=metric,
                    value=value,
                    quality=Quality.OK if value is not None else Quality.MISSING,
                ),
            ),
        )
    )


def test_get_latest_drops_the_policy_and_takes_ts_from_what_is_left(store, tools):
    reading(store, NOW_MS - 5_000, "air.room", 26.0)
    reading(store, NOW_MS - 1_000, POLICY, 0.0)  # 除外のメトリクスがいちばん新しい
    result = tools.call("get_latest")
    assert POLICY not in result["metrics"]
    assert result["ts_ms"] == NOW_MS - 5_000


def test_get_latest_derived_is_built_from_what_is_left(store, catalog):
    exclusions = LlmExclusions(metric_patterns=("air.room",), rule_ids=frozenset())
    registry = ToolRegistry(
        store=store,
        catalog=catalog,
        rules=RuleSet.from_yaml(CONFIG_DIR / "rules.yaml"),
        clock=store.clock,
        exclusions=exclusions,
    )
    reading(store, NOW_MS - 1_000, "air.room", 26.0)
    reading(store, NOW_MS - 1_000, "air.front_intake", 27.0)
    derived = registry.call("get_latest")["derived"]
    assert "d.intake_rise" not in derived, "材料が除外の派生値は引き算で復元できる"


def test_series_and_stats_answer_as_if_the_metric_did_not_exist(store, tools):
    for index in range(10):
        reading(store, NOW_MS - 600_000 + index * 2_500, POLICY, 0.0)
    unknown = "gpu.0.no_such_metric"
    for tool, args in (("query_series", {"window": "1h"}), ("get_stats", {"window": "1h"})):
        excluded = tools.call(tool, {"metric": POLICY, **args})
        absent = tools.call(tool, {"metric": unknown, **args})
        assert {**excluded, "metric": unknown} == absent
        assert "error" not in excluded


def test_list_alerts_drops_excluded_rules_and_metrics(store, tools):
    put_alert(store, "RECIRCULATION", fired_ms=NOW_MS - 60_000)
    put_alert(store, "GPU_FAN_NOT_MANUAL", fired_ms=NOW_MS - 50_000, metric=POLICY)
    put_alert(store, "GPU_FAN_BELOW_CURVE", fired_ms=NOW_MS - 40_000, metric="gpu.0.fan_speed")
    put_alert(store, "SENSOR_MISSING", fired_ms=NOW_MS - 30_000, metric=POLICY)
    listed = tools.call("list_alerts", {"limit": 2})["alerts"]
    # 上限より先に除く（除いてから 2 件取る。取ってから除くと件数が減る）
    assert [a["rule_id"] for a in listed] == ["RECIRCULATION"]


def test_describe_system_drops_the_metric_and_the_rule_thresholds(tools):
    described = tools.call("describe_system")
    assert POLICY not in described["metrics"]
    assert not set(GPU_RULES) & set(described["thresholds"])
    assert "INTAKE_HIGH" in described["thresholds"]
    assert "fan_policy" not in str(described)


# ---------------------------------------------------------------- アラートの説明


def test_explanation_evidence_does_not_name_the_excluded_metric(store, tools):
    reading(store, NOW_MS - 1_000, "air.room", 26.0)
    reading(store, NOW_MS - 1_000, POLICY, None)  # quality が ok でない
    alert = AlertRecord(
        id=1,
        rule_id="RECIRCULATION",
        severity=AlertSeverity.WARNING,
        state=AlertState.FIRING,
        metric="d.intake_rise",
        started_ms=NOW_MS - 300_000,
        fired_ms=NOW_MS,
        trigger_value=6.0,
        threshold=5.0,
    )
    provider = ExplainProvider(ANSWER)
    explanation = Explainer(provider=provider, tools=tools).explain(alert)
    assert explanation is not None
    assert "fan_policy" not in explanation.as_text()
    assert all("fan_policy" not in prompt for prompt in provider.prompts)
    assert "全 1 メトリクスの品質が ok" in explanation.as_text()


class _Notifier:
    @dataclass
    class _Config:
        dashboard_url: str = ""

    def __init__(self) -> None:
        self.config = self._Config()
        self.sent: list[Notification] = []

    def notify(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return True


def _daemon(tmp_path, rules, exclusions: LlmExclusions | None) -> tuple[Daemon, _Notifier, list]:
    clock = SimulatedClock(NOW_MS)
    notifier = _Notifier()
    daemon = Daemon(
        source=None,  # type: ignore[arg-type]
        store=SqliteStore(tmp_path / "daemon.db", rules=rules, clock=clock),
        normalizer=Normalizer(rules=rules, calibration=Calibration(), clock=clock),
        notifier=notifier,  # type: ignore[arg-type]
        explainer_factory=lambda: None,  # type: ignore[arg-type,return-value]
        llm_exclusions=exclusions,
    )
    asked: list[str] = []
    daemon._request_explanation = lambda t: asked.append(t.rule_id)  # type: ignore[method-assign]
    return daemon, notifier, asked


def _firing(rule_id: str, metric: str | None) -> Transition:
    return Transition(rule_id, metric, "firing", 0.0, None, "warning")


def test_excluded_rule_is_notified_but_not_explained(tmp_path, rules):
    daemon, notifier, asked = _daemon(tmp_path, rules, EXCLUSIONS)
    try:
        daemon._record(
            [
                _firing("GPU_FAN_NOT_MANUAL", POLICY),
                _firing("GPU_FAN_BELOW_CURVE", "gpu.0.fan_speed"),
                _firing("SENSOR_MISSING", POLICY),
                _firing("RECIRCULATION", "d.intake_rise"),
            ]
        )
        assert [n.rule_id for n in notifier.sent] == [
            "GPU_FAN_NOT_MANUAL",
            "GPU_FAN_BELOW_CURVE",
            "SENSOR_MISSING",
            "RECIRCULATION",
        ], "通知（素のアラート）は届く"
        assert asked == ["RECIRCULATION"], "一覧に無い rule_id は今までどおり説明を求める"
    finally:
        daemon.store.close()


def test_without_a_list_no_explanation_is_requested(tmp_path, rules):
    daemon, notifier, asked = _daemon(tmp_path, rules, None)
    try:
        daemon._record([_firing("RECIRCULATION", "d.intake_rise")])
        assert len(notifier.sent) == 1
        assert asked == []
        assert daemon._explainer_factory is None
    finally:
        daemon.store.close()


# ---------------------------------------------------------------- 案件資料


def test_escalation_packet_never_carries_the_excluded_rules_or_metric(store, catalog):
    rules = EscalationRules.from_yaml(CONFIG_DIR / "escalation.yaml")
    for offset in (-2, -1, 0):
        at_ms = DAY_START_MS + offset * DAY_MS + 3_600_000
        # 重大度を後で critical に変えても載らないこと
        put_alert(store, "GPU_FAN_NOT_MANUAL", fired_ms=at_ms, metric=POLICY, severity="critical")
        put_alert(store, "SENSOR_FAULT", fired_ms=at_ms, metric=None, severity="critical")
    reading(store, DAY_START_MS + DAY_MS - 1_000, "air.room", 26.0)
    reading(store, DAY_START_MS + DAY_MS - 1_000, POLICY, None)

    findings = evaluate(store, rules, catalog, day=DAY, exclusions=EXCLUSIONS)
    assert [f.rule_id for f in findings] == ["SENSOR_FAULT"]
    packet = build_packet(
        store,
        findings[0],
        rules,
        catalog,
        day=DAY,
        exclusions=EXCLUSIONS,
        rule_set=RuleSet.from_yaml(CONFIG_DIR / "rules.yaml"),
    )
    assert "fan_policy" not in packet
    assert "GPU_FAN" not in packet
    assert "すべて ok" in packet, "品質の判定も除いた後の集合から"


# ---------------------------------------------------------------- Server Health の AI 要約

YELLOW_TEMPLATE = "一部のTelemetryまたはアラートに注意が必要です。"


def _fire(path: Path, rules, rule_id: str, metric: str) -> None:
    with SqliteStore(path, rules=rules, clock=SimulatedClock(HEALTH_NOW_MS)) as opened:
        alert_id = opened.open_alert(
            rule_id=rule_id,
            severity="warning",
            metric=metric,
            started_ms=HEALTH_NOW_MS - 60_000,
            threshold=None,
            trigger_value=0.0,
            detail=None,
        )
        opened.fire_alert(alert_id, fired_ms=HEALTH_NOW_MS - 30_000, trigger_value=0.0)


def test_health_summary_is_not_requested_when_only_an_excluded_alert_changes_the_signal(
    tmp_path, rules
):
    path = tmp_path / "health.db"
    _populate(path, rules)
    _fire(path, rules, "GPU_FAN_NOT_MANUAL", POLICY)
    summarizer = FakeSummarizer()
    with TestClient(_app(path, SimulatedClock(HEALTH_NOW_MS), summarizer)) as client:
        body = client.get("/api/v1/server-health").json()
    # 人が見る応答は除く前のまま
    assert body["signal"] == "yellow"
    assert [a["rule_id"] for a in body["active_alerts"]] == ["GPU_FAN_NOT_MANUAL"]
    # AI 用の signal（green）と食い違うので、要約を求めず決定論的なテンプレートを返す
    assert summarizer.facts == []
    assert body["summary_source"] == "template"
    assert body["summary"] == YELLOW_TEMPLATE


def test_health_summary_uses_the_signal_after_exclusion_when_it_agrees(tmp_path, rules):
    path = tmp_path / "health.db"
    _populate(path, rules)
    _fire(path, rules, "GPU_FAN_NOT_MANUAL", POLICY)
    _fire(path, rules, "RECIRCULATION", "d.intake_rise")
    summarizer = FakeSummarizer()
    with TestClient(_app(path, SimulatedClock(HEALTH_NOW_MS), summarizer)) as client:
        body = client.get("/api/v1/server-health").json()
    assert body["signal"] == "yellow"
    assert {a["rule_id"] for a in body["active_alerts"]} == {"GPU_FAN_NOT_MANUAL", "RECIRCULATION"}
    assert summarizer.facts == [YELLOW_TEMPLATE]
    assert body["summary_source"] == "ai"


def test_health_summary_is_not_requested_without_a_list(tmp_path, rules, monkeypatch):
    """一覧が読めなければ、合成の起点は AI 要約もツールの窓口も開かない。"""
    import coldaisle.server as server

    bad = tmp_path / "report.yaml"
    bad.write_text("llm_excluded_metrics: oops\n", encoding="utf-8")
    monkeypatch.setenv("COLDAISLE_REPORT_CONFIG", str(bad))
    monkeypatch.setenv("COLDAISLE_DB", str(tmp_path / "server.db"))
    app = server.create_server()
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/v1/tools" not in paths
