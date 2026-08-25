"""ハードウェア故障疑いの引き上げ（#39 / FR-509）。

**ローカルモデルに高価な機材の故障判断をさせない**（構想メモ §10）。

守る線は3つ（Issue の受入基準）。

1. **判定に LLM を使わない**（決定論的）
2. **送信内容がユーザーに事前提示される**（自動で送らない）
3. **リポジトリ全体や生ログを送らない**
"""

import ast
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.escalate import (
    DERIVED_BASIS,
    PREAMBLE,
    EscalationRules,
    Finding,
    as_notification,
    build_packet,
    daily_means,
    evaluate,
    main,
    write_packet,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.rules import RuleSet
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
CONFIG_PATH = CONFIG_DIR / "escalation.yaml"
TZ = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 8, 24)
DAY_START_MS = 1_787_497_200_000
"""2026-08-24T00:00:00+09:00。"""

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


@pytest.fixture
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")


@pytest.fixture
def rules() -> EscalationRules:
    """**本番と同じ `config/escalation.yaml`** を読む。"""
    return EscalationRules.from_yaml(CONFIG_PATH)


@pytest.fixture
def store(tmp_path):
    from coldaisle.store.quality import QualityRules

    quality = QualityRules.from_yaml(CONFIG_DIR / "quality.yaml")
    with SqliteStore(
        tmp_path / "escalate.db", rules=quality, clock=SimulatedClock(DAY_START_MS)
    ) as opened:
        yield opened


def hour(store, metric: str, *, day_offset: int, hour_index: int, mean: float, ok: int = 1440):
    """1時間バケットを直接書く。**ロールアップの出力形をそのまま使う。**"""
    bucket_ms = DAY_START_MS + day_offset * DAY_MS + hour_index * HOUR_MS
    store.connection.execute(
        "INSERT OR REPLACE INTO readings_1h (metric, bucket_ms, min_value, max_value, "
        "mean_value, ok_value_count, row_count, expected_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (metric, bucket_ms, mean, mean, mean, ok, ok, ok),
    )
    store.connection.commit()


def day_of(store, metric: str, *, day_offset: int, mean: float) -> None:
    for index in range(24):
        hour(store, metric, day_offset=day_offset, hour_index=index, mean=mean)


def critical(store, rule_id: str, *, day_offset: int) -> None:
    at = DAY_START_MS + day_offset * DAY_MS + HOUR_MS
    store.connection.execute(
        "INSERT INTO alerts (rule_id, severity, state, metric, started_ms, fired_ms) "
        "VALUES (?, 'critical', 'resolved', 'air.room', ?, ?)",
        (rule_id, at, at),
    )
    store.connection.commit()


def with_step_change(store, *, before: float, after: float, hold_days: int = 3) -> None:
    """`d.gpu_internal_delta` に段差を作る（材料は `gpu.0.core` / `gpu.0.hotspot`）。"""
    for offset in range(-13, 1):
        gap = after if offset > -hold_days else before
        day_of(store, "gpu.0.core", day_offset=offset, mean=70.0)
        day_of(store, "gpu.0.hotspot", day_offset=offset, mean=70.0 + gap)


# ---------------------------------------------------------------- 決定論（受入基準）


def test_no_model_is_involved():
    """受入基準: **エスカレーション判定に LLM を使わない。**

    高価な機材の故障判断をローカルモデルにさせないための Issue で、
    その判定自体をモデルに任せたら本末転倒である。
    """
    tree = ast.parse((SRC / "escalate.py").read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
    assert [name for name in imported if name.startswith("coldaisle.ai")] == []


def test_the_same_input_gives_the_same_findings(store, rules, catalog):
    with_step_change(store, before=12.0, after=22.0)
    first = evaluate(store, rules, catalog, day=DAY)
    second = evaluate(store, rules, catalog, day=DAY)
    assert first == second
    assert [finding.trigger for finding in first] == ["step_change"]


# ---------------------------------------------------------------- 段差


def test_a_sustained_step_is_escalated(store, rules, catalog):
    """急に変わって、**そのまま戻らない**ものを拾う。"""
    with_step_change(store, before=12.0, after=22.0)
    findings = evaluate(store, rules, catalog, day=DAY)
    assert len(findings) == 1
    assert "d.gpu_internal_delta" in findings[0].problem
    assert findings[0].started == DAY - timedelta(days=2)


def test_a_one_day_spike_is_not_escalated(store, rules, catalog):
    """**1日だけの跳ねは拾わない。** 負荷の山かもしれない。"""
    for offset in range(-13, 1):
        gap = 22.0 if offset == -1 else 12.0
        day_of(store, "gpu.0.core", day_offset=offset, mean=70.0)
        day_of(store, "gpu.0.hotspot", day_offset=offset, mean=70.0 + gap)
    assert evaluate(store, rules, catalog, day=DAY) == []


def test_a_small_change_is_not_escalated(store, rules, catalog):
    with_step_change(store, before=12.0, after=14.0)
    assert evaluate(store, rules, catalog, day=DAY) == []


def test_a_drifting_level_is_not_a_step(store, rules, catalog):
    """跳ねたあとも動き続けているなら段差ではない（変動）。"""
    for offset in range(-13, 1):
        gap = {0: 40.0, -1: 30.0, -2: 22.0}.get(offset, 12.0)
        day_of(store, "gpu.0.core", day_offset=offset, mean=70.0)
        day_of(store, "gpu.0.hotspot", day_offset=offset, mean=70.0 + gap)
    assert evaluate(store, rules, catalog, day=DAY) == []


def test_missing_days_do_not_produce_a_step(store, rules, catalog):
    """**データが無い日を「変わっていない」とみなさない。**"""
    day_of(store, "gpu.0.core", day_offset=0, mean=70.0)
    day_of(store, "gpu.0.hotspot", day_offset=0, mean=92.0)
    assert evaluate(store, rules, catalog, day=DAY) == []


# ---------------------------------------------------------------- 逸脱と再発


def test_sustained_deviation_is_off_until_the_baseline_is_measured(rules):
    """**#19 が終わるまで false のまま。** 実測していない基準で毎日引き上げない。"""
    assert rules.sustained_deviation.enabled is False


def test_sustained_deviation_fires_when_enabled(store, rules, catalog):
    enabled = rules.model_copy(
        update={
            "sustained_deviation": rules.sustained_deviation.model_copy(
                update={"enabled": True, "metric": "air.room", "baseline": 25.0, "tolerance": 2.0}
            )
        }
    )
    for index in range(24):
        hour(store, "air.room", day_offset=0, hour_index=index, mean=31.0)
    findings = evaluate(store, enabled, catalog, day=DAY)
    assert [finding.trigger for finding in findings] == ["sustained_deviation"]


def test_a_gap_in_the_hours_is_not_treated_as_normal(store, rules, catalog):
    """**欠けている時間を「正常」とみなさない。** 沈黙は正常の証拠ではない。"""
    enabled = rules.model_copy(
        update={
            "sustained_deviation": rules.sustained_deviation.model_copy(
                update={"enabled": True, "metric": "air.room", "baseline": 25.0, "tolerance": 2.0}
            )
        }
    )
    for index in range(24):
        if index == 20:
            continue  # 1時間だけ欠測
        hour(store, "air.room", day_offset=0, hour_index=index, mean=31.0)
    assert evaluate(store, enabled, catalog, day=DAY) == []


def test_repeated_criticals_are_escalated(store, rules, catalog):
    for offset in (-4, -2, 0):
        critical(store, "SENSOR_FAULT", day_offset=offset)
    findings = evaluate(store, rules, catalog, day=DAY)
    assert [finding.rule_id for finding in findings] == ["SENSOR_FAULT"]


def test_two_criticals_are_not_enough(store, rules, catalog):
    """**1回や2回では引き上げない。** 対処済みかもしれない。"""
    for offset in (-2, 0):
        critical(store, "SENSOR_FAULT", day_offset=offset)
    assert evaluate(store, rules, catalog, day=DAY) == []


def test_criticals_outside_the_window_are_not_counted(store, rules, catalog):
    for offset in (-30, -20, 0):
        critical(store, "SENSOR_FAULT", day_offset=offset)
    assert evaluate(store, rules, catalog, day=DAY) == []


# ---------------------------------------------------------------- 案件資料（受入基準）


def test_the_packet_has_the_five_sections(store, rules, catalog):
    """構想メモ §10 の形。**問題 / 測定値 / 経過 / 試行済み / 争点。**"""
    with_step_change(store, before=12.0, after=22.0)
    finding = evaluate(store, rules, catalog, day=DAY)[0]
    text = build_packet(store, finding, rules, catalog, day=DAY)
    for section in ("## 問題", "## 測定値", "## 経過", "## 試行済み", "## 争点"):
        assert section in text


def test_the_packet_says_it_was_not_sent(store, rules, catalog):
    """受入基準: **送信内容がユーザーに事前提示される。**"""
    finding = Finding(
        trigger="step_change", problem="x", question="y", started=DAY, metric="air.room"
    )
    text = build_packet(store, finding, rules, catalog, day=DAY)
    assert PREAMBLE in text
    assert "送信は行っていません" in text


def test_the_packet_contains_no_raw_series(store, rules, catalog):
    """受入基準: **生ログを送らない。** 経過は日ごとの平均だけ。"""
    with_step_change(store, before=12.0, after=22.0)
    store.insert_samples(
        [
            Sample(
                ts_ms=DAY_START_MS + step * 2_500,
                readings=(Reading(metric="air.room", value=26.0, quality=Quality.OK),),
            )
            for step in range(100)
        ]
    )
    finding = evaluate(store, rules, catalog, day=DAY)[0]
    text = build_packet(store, finding, rules, catalog, day=DAY)
    assert str(DAY_START_MS) not in text  # ミリ秒の時刻が出ない
    assert text.count("\n|") <= rules.course_days + 10  # 行数が日数の桁で収まる


def test_the_packet_is_capped(store, rules, catalog):
    small = rules.model_copy(update={"max_chars": 500})
    with_step_change(store, before=12.0, after=22.0)
    finding = evaluate(store, small, catalog, day=DAY)[0]
    text = build_packet(store, finding, small, catalog, day=DAY)
    assert len(text) <= 540
    assert "上限で切りました" in text  # **黙って切らない**


def test_a_derived_metric_states_how_it_was_computed(store, rules, catalog):
    """**1日平均どうしの差**であることを書く（生の差の平均とは一致しない）。"""
    with_step_change(store, before=12.0, after=22.0)
    finding = evaluate(store, rules, catalog, day=DAY)[0]
    assert DERIVED_BASIS in build_packet(store, finding, rules, catalog, day=DAY)


def test_the_measurements_use_the_ingredients_of_a_derived_metric(store, rules, catalog):
    with_step_change(store, before=12.0, after=22.0)
    finding = evaluate(store, rules, catalog, day=DAY)[0]
    text = build_packet(store, finding, rules, catalog, day=DAY)
    assert "gpu.0.hotspot" in text and "gpu.0.core" in text


def test_derived_daily_means_need_both_ingredients(store, catalog):
    """片方が欠けたら差も無い（決定記録 0009 §2.2 と同じ理由）。"""
    day_of(store, "gpu.0.core", day_offset=0, mean=70.0)
    series = daily_means(store, "d.gpu_internal_delta", catalog, day=DAY, days=2, tz=TZ)
    assert [value for _, value in series] == [None, None]


def test_the_checks_compare_against_the_configured_thresholds(store, rules, catalog):
    """「吸気温は正常であることを確認」（Issue の例）。**別の基準を持たない。**"""
    rule_set = RuleSet.from_yaml(CONFIG_DIR / "rules.yaml")
    with_step_change(store, before=12.0, after=22.0)
    store.insert_samples(
        [
            Sample(
                ts_ms=DAY_START_MS + DAY_MS - 5_000,
                readings=(Reading(metric="air.gpu_intake", value=29.1, quality=Quality.OK),),
            )
        ]
    )
    finding = evaluate(store, rules, catalog, day=DAY)[0]
    text = build_packet(store, finding, rules, catalog, day=DAY, rule_set=rule_set)
    threshold = rule_set.intake_high.threshold
    assert (
        f"air.gpu_intake（24時間 p95 29.1）: 正常範囲（intake_high の閾値 {threshold:.1f} 未満）"
        in text
    )


def test_a_value_outside_the_threshold_is_marked(store, rules, catalog):
    """**「確認しました」で済ませない。** 外れているならそう書く。"""
    rule_set = RuleSet.from_yaml(CONFIG_DIR / "rules.yaml")
    with_step_change(store, before=12.0, after=22.0)
    store.insert_samples(
        [
            Sample(
                ts_ms=DAY_START_MS + DAY_MS - 5_000,
                readings=(
                    Reading(
                        metric="air.gpu_intake",
                        value=rule_set.intake_high.threshold + 5.0,
                        quality=Quality.OK,
                    ),
                ),
            )
        ]
    )
    finding = evaluate(store, rules, catalog, day=DAY)[0]
    text = build_packet(store, finding, rules, catalog, day=DAY, rule_set=rule_set)
    assert "**外れている**" in text


def test_untested_things_are_not_claimed_as_checked(store, rules, catalog):
    """**測っていないことを「試行済み」に書かない。** それは争点である。

    Issue の例にある「ファン回転数に変化なし」「負荷パターンに変化なし」は
    いま測っていない。書けば、読む側は確認済みだと思う。
    """
    with_step_change(store, before=12.0, after=22.0)
    finding = evaluate(store, rules, catalog, day=DAY)[0]
    text = build_packet(store, finding, rules, catalog, day=DAY)
    checked = text.split("## 試行済み")[1].split("## 争点")[0]
    for untested in ("ファン回転数", "負荷パターン"):
        assert untested not in checked
    # 争点の側には書いてよい（「人が確認すること」として）
    assert "負荷パターン" in text.split("## 争点")[1]


# ---------------------------------------------------------------- 送らない（受入基準）


def test_the_notification_carries_the_path_not_the_contents(store, rules, catalog):
    """**知らせるだけ。** 中身も送らない。"""
    finding = Finding(
        trigger="step_change", problem="段差がある", question="争点", started=DAY, metric="air.room"
    )
    notification = as_notification(finding, Path("var/escalations/x.md"), dashboard_url="http://x/")
    assert "var/escalations/x.md" in notification.body
    assert "送信していません" in notification.body
    assert "争点" not in notification.body


def test_the_same_case_is_not_written_twice(tmp_path):
    """**同じ案件を毎日作らない。** 毎朝同じ資料が増えると読まれなくなる。"""
    finding = Finding(
        trigger="step_change", problem="x", question="y", started=DAY, metric="d.gpu_internal_delta"
    )
    assert write_packet("本文", finding, tmp_path) is not None
    assert write_packet("本文", finding, tmp_path) is None
    assert write_packet("新しい本文", finding, tmp_path, force=True) is not None


def test_nothing_is_written_when_there_is_nothing_to_escalate(store, rules, catalog, tmp_path):
    assert evaluate(store, rules, catalog, day=DAY) == []


# ---------------------------------------------------------------- 設定と CLI


def test_shipped_config_is_valid(rules):
    """**本番の設定ファイルが読めること。** 壊れていたら毎朝黙って落ちる。"""
    assert rules.zone.key == "Asia/Tokyo"
    assert rules.step_change.hold_days >= 2  # 1日で騒がない


def test_config_rejects_a_misspelled_timezone(tmp_path):
    text = CONFIG_PATH.read_text(encoding="utf-8").replace(
        'timezone: "Asia/Tokyo"', 'timezone: "Asia/Tokio"'
    )
    path = tmp_path / "escalation.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="タイムゾーン"):
        EscalationRules.from_yaml(path)


def test_cli_writes_a_packet_without_sending(tmp_path, catalog):
    from coldaisle.store.quality import QualityRules

    db = tmp_path / "cli.db"
    quality = QualityRules.from_yaml(CONFIG_DIR / "quality.yaml")
    with SqliteStore(db, rules=quality, clock=SimulatedClock(DAY_START_MS)) as opened:
        with_step_change(opened, before=12.0, after=22.0)
    config = tmp_path / "escalation.yaml"
    config.write_text(
        CONFIG_PATH.read_text(encoding="utf-8").replace(
            'output_dir: "var/escalations"', f'output_dir: "{tmp_path / "out"}"'
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--db",
                str(db),
                "--config",
                str(config),
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
        )
        == 0
    )
    written = list((tmp_path / "out").glob("*.md"))
    assert len(written) == 1
    assert "送信は行っていません" in written[0].read_text(encoding="utf-8")
