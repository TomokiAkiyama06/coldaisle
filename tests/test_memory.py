"""運用メモリへの記録（#40 / 構想メモ §17-§23）。

守る線は3つ（Issue の受入基準と要件 Q-12）。

1. **現在の閾値・較正値が Markdown 1ファイルを見れば分かる**
2. **過去の値も Superseded として残り、検索で誤って最新扱いされない**
3. **全自動保存にしない**（`--apply` を付けたときだけ書く）
"""

import subprocess
from datetime import date
from pathlib import Path

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.ingest.calibration import Calibration
from coldaisle.memory import (
    CURRENT_END,
    CURRENT_START,
    Fact,
    apply,
    collect,
    diff,
    initial,
    main,
    mark_superseded,
    read_current,
    render_current,
)
from coldaisle.rules import RuleSet
from coldaisle.store import DeviceRecord, SensorRecord, SqliteStore
from conftest import CONFIG_DIR

TODAY = date(2026, 8, 26)
LATER = date(2026, 9, 10)
LATEST = date(2026, 10, 1)


@pytest.fixture
def rule_set() -> RuleSet:
    """**本番と同じ `config/rules.yaml`** を読む。"""
    return RuleSet.from_yaml(CONFIG_DIR / "rules.yaml")


@pytest.fixture
def calibration() -> Calibration:
    return Calibration.from_json(CONFIG_DIR / "calibration.json")


def facts_of(*pairs: tuple[str, str]) -> list[Fact]:
    return [
        Fact(key=key, label=f"{key} の値", value=value, source="config/rules.yaml")
        for key, value in pairs
    ]


def build(facts, today: date = TODAY, text: str | None = None) -> str:
    """事実を書き込んだ本文を返す。"""
    base = initial("gpu-server") if text is None else text
    return apply(base, facts, diff(facts, read_current(base)), today)


# ---------------------------------------------------------------- 現在値（受入基準）


def test_current_values_are_in_one_place(rule_set, calibration):
    """受入基準: **1ファイルを見れば、いまの閾値と較正値が分かる。**"""
    text = build(collect(rule_set, calibration))
    current = read_current(text)
    assert current["rule.recirculation"][0].endswith(
        f"threshold={rule_set.recirculation.threshold:g}"
    )
    assert current["calibration.room_temp"][0] == "+0.00 C"


def test_each_key_appears_once_in_the_current_block(rule_set, calibration):
    """**現在値は1行だけ。** 同じキーが2行あると、どちらが本当か分からない。"""
    facts = collect(rule_set, calibration)
    block = build(facts).split(CURRENT_START)[1].split(CURRENT_END)[0]
    for fact in facts:
        assert block.count(f"| `{fact.key}` |") == 1


def test_an_unchanged_value_keeps_its_original_date():
    """**「いつからこの値なのか」を消さない。** 毎回今日にすると分からなくなる。"""
    facts = facts_of(("rule.a", "5"))
    first = build(facts, TODAY)
    second = apply(first, facts, diff(facts, read_current(first)), LATER)
    assert read_current(second)["rule.a"] == ("5", TODAY.isoformat())


def test_a_changed_value_takes_the_new_date():
    first = build(facts_of(("rule.a", "5")), TODAY)
    changed = facts_of(("rule.a", "4"))
    second = apply(first, changed, diff(changed, read_current(first)), LATER)
    assert read_current(second)["rule.a"] == ("4", LATER.isoformat())


def test_a_disabled_rule_is_recorded_as_disabled(rule_set, calibration):
    """**無効なルールを黙って省かない。** 「書いていない＝有効」と読まれる。"""
    off = rule_set.model_copy(
        update={"recirculation": rule_set.recirculation.model_copy(update={"enabled": False})}
    )
    assert read_current(build(collect(off, calibration)))["rule.recirculation"][0] == "無効"


# ---------------------------------------------------------------- 履歴（受入基準）


def test_a_change_is_recorded_with_supersedes():
    first = build(facts_of(("rule.a", "5")), TODAY)
    changed = facts_of(("rule.a", "4"))
    second = apply(first, changed, diff(changed, read_current(first)), LATER)
    assert "Decision: rule.a の値 = 4" in second
    assert f"Supersedes: 5（{TODAY.isoformat()}）" in second


def test_the_old_entry_is_marked_superseded():
    """受入基準: **検索で誤って最新扱いされない。**

    新しい記録に `Supersedes` を書くだけでは、古い記録**だけ**を読んだ人が
    失効した値に従う（`docs/decisions/README.md` の「追記のみ」と同じ理由）。
    """
    first = build(facts_of(("rule.a", "5")), TODAY)
    changed = facts_of(("rule.a", "4"))
    second = apply(first, changed, diff(changed, read_current(first)), LATER)
    old = next(block for block in second.split("\n\n") if "= 5" in block)
    assert f"Superseded by: {LATER.isoformat()}" in old


def test_only_the_newest_entry_is_unmarked():
    """3世代でも、印が無いのは**いちばん新しい1件だけ。**"""
    text = build(facts_of(("rule.a", "5")), TODAY)
    for value, at in (("4", LATER), ("3", LATEST)):
        facts = facts_of(("rule.a", value))
        text = apply(text, facts, diff(facts, read_current(text)), at)
    entries = [block for block in text.split("\n\n") if "Key: `rule.a`" in block]
    assert len(entries) == 3
    assert [("Superseded by" in block) for block in entries] == [False, True, True]


def test_a_new_entry_is_not_marked_by_its_own_run():
    """**いま積んだ記録に「これは古い」と書かない。** 印を先に付ける理由。"""
    first = build(facts_of(("rule.a", "5")), TODAY)
    changed = facts_of(("rule.a", "4"))
    second = apply(first, changed, diff(changed, read_current(first)), LATER)
    newest = next(block for block in second.split("\n\n") if "= 4" in block)
    assert "Superseded by" not in newest


def test_history_is_never_rewritten():
    """**過去の本文は書き換えない。** 足すのは `Superseded by` の1行だけ。"""
    first = build(facts_of(("rule.a", "5")), TODAY)
    changed = facts_of(("rule.a", "4"))
    second = apply(first, changed, diff(changed, read_current(first)), LATER)
    for line in first.split("## Decisions")[1].splitlines():
        if line.strip():
            assert line in second


def test_marking_is_idempotent():
    text = build(facts_of(("rule.a", "5")), TODAY)
    once = mark_superseded(text, {"rule.a"}, LATER)
    assert mark_superseded(once, {"rule.a"}, LATEST) == once


def test_new_facts_are_recorded_without_supersedes():
    assert "Supersedes" not in build(facts_of(("rule.a", "5")), TODAY)


# ---------------------------------------------------------------- 人の文章を壊さない


def test_text_outside_the_block_is_preserved():
    """**人が書いた文章を機械が壊さない。** 印の外は触らない。"""
    text = build(facts_of(("rule.a", "5")), TODAY)
    text = text.replace("## Current Facts", "## メモ\n\n手で書いた注意書き。\n\n## Current Facts")
    facts = facts_of(("rule.a", "4"))
    updated = apply(text, facts, diff(facts, read_current(text)), LATER)
    assert "手で書いた注意書き。" in updated


def test_a_file_without_markers_gets_a_block():
    """印の無いファイル（人が先に作った）にも足せる。"""
    text = "# 手で作ったメモ\n\n何か書いてある。\n"
    facts = facts_of(("rule.a", "5"))
    updated = apply(text, facts, diff(facts, read_current(text)), TODAY)
    assert "何か書いてある。" in updated
    assert read_current(updated)["rule.a"][0] == "5"


def test_a_missing_block_is_not_read_as_empty_values():
    assert read_current("# メモ\n\n本文\n") == {}


# ---------------------------------------------------------------- 収集


def test_sensor_layout_is_recorded(tmp_path, rules, rule_set, calibration):
    """**較正値が対応している ROM がどれか**を残す（#14 / FR-403）。"""
    with SqliteStore(tmp_path / "m.db", rules=rules, clock=SimulatedClock(0)) as store:
        store.record_hello(
            DeviceRecord(device_id="dev-1", fw="1.0.0", schema_v=1, interval_ms=2500),
            [SensorRecord(channel="room_temp", kind="ds18b20", rom="28AABB")],
            at_ms=0,
        )
        facts = {fact.key: fact.value for fact in collect(rule_set, calibration, store)}
    assert facts["sensors.dev-1"] == "1 本 / ROM 28AABB"


def test_values_come_only_from_files_and_the_database(rule_set, calibration):
    """**推測しない。** 出所を必ず持つ。"""
    for fact in collect(rule_set, calibration):
        assert fact.source in {"config/rules.yaml", "config/calibration.json"}


# ---------------------------------------------------------------- 書かない（受入基準）


def test_nothing_is_written_without_apply(tmp_path, capsys):
    """受入基準: **全自動保存にしない**（要件 Q-12 / 構想メモ §23）。"""
    target = tmp_path / "memory" / "gpu-server.md"
    assert main([*_args(target), "--db", str(tmp_path / "none.db")]) == 0
    assert not target.exists()
    assert "書き込んでいません" in capsys.readouterr().out


def test_apply_writes_the_file(tmp_path):
    target = tmp_path / "memory" / "gpu-server.md"
    assert main([*_args(target), "--db", str(tmp_path / "none.db"), "--apply"]) == 0
    assert "## Current Facts" in target.read_text(encoding="utf-8")


def test_commit_without_apply_is_refused(tmp_path):
    """**書いていないものはコミットできない。**"""
    with pytest.raises(SystemExit):
        main([*_args(tmp_path / "m.md"), "--db", str(tmp_path / "none.db"), "--commit"])


def test_a_second_run_reports_no_change(tmp_path, capsys):
    target = tmp_path / "m.md"
    argv = [*_args(target), "--db", str(tmp_path / "none.db")]
    main([*argv, "--apply"])
    capsys.readouterr()
    main(argv)
    assert "変更はありません" in capsys.readouterr().out


def test_commit_records_the_change(tmp_path):
    """**人が差分を追える**ように git commit する（#40）。push はしない。"""
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        subprocess.run(argv, cwd=tmp_path, check=True, capture_output=True)
    target = tmp_path / "gpu-server.md"
    main([*_args(target), "--db", str(tmp_path / "none.db"), "--apply", "--commit"])
    log = subprocess.run(
        ["git", "log", "--oneline", "--name-only"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "運用メモリを更新する" in log
    assert "gpu-server.md" in log


def _args(target: Path) -> list[str]:
    return [
        "--memory",
        str(target),
        "--rules",
        str(CONFIG_DIR / "rules.yaml"),
        "--calibration",
        str(CONFIG_DIR / "calibration.json"),
    ]


# ---------------------------------------------------------------- 体裁


def test_the_block_is_a_markdown_table(rule_set, calibration):
    block = render_current(collect(rule_set, calibration), {}, TODAY)
    assert block.startswith(CURRENT_START)
    assert block.rstrip().endswith(CURRENT_END)
    assert "| キー | 項目 | 値 | 出所 | 記録日 |" in block


def test_the_file_starts_with_front_matter():
    """YAML Front Matter + Markdown（構想メモ §19）。"""
    text = initial("gpu-server")
    assert text.startswith("---\nproject: gpu-server\n")
