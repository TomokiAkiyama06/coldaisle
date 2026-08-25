"""運用メモリへの記録（#40 / 構想メモ §17-§23）。

**較正値と閾値は運用中に何度も変わる。** 会話履歴やコミットログに散らばると、
3ヶ月後に「いまの閾値はいくつだっけ」が分からなくなる。

この道具がすること。

1. いまの設定（`rules.yaml` / `calibration.json`）と DB から**事実を集める**
2. 記録済みの内容と**突き合わせて差分を出す**
3. **`--apply` を付けたときだけ**書く（全自動保存にしない。要件 Q-12）

書き方の約束（構想メモ §21）。

- **Current Facts は1箇所だけ。** 印で囲んだ区画を丸ごと作り直す
- **History は書き換えない。** 変わった事実ごとに `Supersedes` 付きで積む
- 区画の外は触らない。**人が書いた文章を機械が壊さない**
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from coldaisle import logs
from coldaisle.clock import Clock, WallClock
from coldaisle.ingest.calibration import Calibration
from coldaisle.rules.models import RuleSet
from coldaisle.store.db import SqliteStore
from coldaisle.store.quality import QualityRules

LOGGER = logging.getLogger("coldaisle.memory")

CURRENT_START = "<!-- coldaisle:current:start -->"
CURRENT_END = "<!-- coldaisle:current:end -->"
"""**この印の外は触らない。** 人が書いた文章を機械が壊さないため。"""

DECISIONS_HEADING = "## Decisions"
TABLE_HEADER = "| キー | 項目 | 値 | 出所 | 記録日 |"
TABLE_RULE = "|---|---|---|---|---|"

ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$")

PREAMBLE = """<!-- この節は `coldaisle-memory` が作り直します。手で書き足さないでください。 -->
<!-- 経緯は下の Decisions に積まれます。そちらは書き換えません。 -->"""


@dataclass(frozen=True)
class Fact:
    """いま効いている値1つ。**出所を必ず持つ。**"""

    key: str
    label: str
    value: str
    source: str


@dataclass(frozen=True)
class Change:
    """記録済みとの差分1つ。"""

    fact: Fact
    previous: str | None
    previous_date: str | None

    @property
    def is_new(self) -> bool:
        return self.previous is None

    def as_line(self) -> str:
        if self.is_new:
            return f"+ {self.fact.label}: {self.fact.value}（新規）"
        return f"~ {self.fact.label}: {self.previous} → {self.fact.value}"


# ---------------------------------------------------------------------- 収集


def collect(
    rules: RuleSet, calibration: Calibration, store: SqliteStore | None = None
) -> list[Fact]:
    """いまの事実を集める。**推測しない。** 設定ファイルと DB にあるものだけ。"""
    facts = [*_rule_facts(rules), *_calibration_facts(calibration)]
    if store is not None:
        facts += _sensor_facts(store)
    return facts


def _rule_facts(rules: RuleSet) -> list[Fact]:
    """ルールの閾値。**種類ごとに書き分けない**（項目が増えても落ちない）。"""
    facts: list[Fact] = []
    for name, rule in sorted(rules):
        dumped = rule.model_dump()
        if not dumped.pop("enabled"):
            value = "無効"
        else:
            severity = dumped.pop("severity")
            body = ", ".join(f"{key}={_fmt(item)}" for key, item in sorted(dumped.items()))
            value = f"{severity} / {body}" if body else str(severity)
        facts.append(
            Fact(
                key=f"rule.{name}", label=f"{name} の設定", value=value, source="config/rules.yaml"
            )
        )
    return facts


def _calibration_facts(calibration: Calibration) -> list[Fact]:
    return [
        Fact(
            key=f"calibration.{channel}",
            label=f"{channel} の較正オフセット",
            value=f"{offset:+.2f} C",
            source="config/calibration.json",
        )
        for channel, offset in sorted(calibration.offsets_c.items())
    ]


def _sensor_facts(store: SqliteStore) -> list[Fact]:
    """センサー構成。**較正値が対応している ROM がどれか**を残す（#14 / FR-403）。"""
    facts: list[Fact] = []
    for device_id in sorted(_device_ids(store)):
        sensors = store.sensors_for(device_id)
        roms = sorted(sensor.rom for sensor in sensors if sensor.rom)
        facts.append(
            Fact(
                key=f"sensors.{device_id}",
                label=f"{device_id} のセンサー構成",
                value=f"{len(sensors)} 本" + (f" / ROM {', '.join(roms)}" if roms else ""),
                source="devices テーブル（起動バナー）",
            )
        )
    return facts


def _device_ids(store: SqliteStore) -> list[str]:
    rows = store.connection.execute("SELECT device_id FROM devices ORDER BY device_id").fetchall()
    return [row["device_id"] for row in rows]


def _fmt(value: object) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


# ---------------------------------------------------------------------- 読み書き


def read_current(text: str) -> dict[str, tuple[str, str]]:
    """記録済みの Current Facts を `{キー: (値, 記録日)}` で返す。"""
    if CURRENT_START not in text or CURRENT_END not in text:
        return {}
    block = text.split(CURRENT_START, 1)[1].split(CURRENT_END, 1)[0]
    recorded: dict[str, tuple[str, str]] = {}
    for line in block.splitlines():
        matched = ROW.match(line.strip())
        if matched is None:
            continue
        key, _label, value, _source, at = matched.groups()
        recorded[key.strip()] = (value.strip(), at.strip())
    return recorded


def diff(facts: Sequence[Fact], recorded: dict[str, tuple[str, str]]) -> list[Change]:
    """**消えた項目は差分に出さない。** 設定から消えたことは History で追える。"""
    changes: list[Change] = []
    for fact in facts:
        previous = recorded.get(fact.key)
        if previous is not None and previous[0] == fact.value:
            continue
        changes.append(
            Change(
                fact=fact,
                previous=None if previous is None else previous[0],
                previous_date=None if previous is None else previous[1],
            )
        )
    return changes


def render_current(facts: Sequence[Fact], recorded: dict[str, tuple[str, str]], today: date) -> str:
    """Current Facts の区画。**変わっていない項目の記録日は据え置く。**

    毎回今日の日付にすると、「いつからこの値なのか」が消える。
    """
    lines = [CURRENT_START, PREAMBLE, "", TABLE_HEADER, TABLE_RULE]
    for fact in facts:
        previous = recorded.get(fact.key)
        at = (
            previous[1] if previous is not None and previous[0] == fact.value else today.isoformat()
        )
        lines.append(f"| `{fact.key}` | {fact.label} | {fact.value} | {fact.source} | {at} |")
    lines += ["", CURRENT_END]
    return "\n".join(lines)


def render_history(changes: Sequence[Change], today: date) -> str:
    """History の1件ぶん。**書き換えず積む**（決定記録の作法と同じ）。

    各段落は `Key:` で始める。**あとから「これは古い」と印を付けるため**に、
    どの項目の記録なのかが機械にも読める必要がある。
    """
    blocks = [f"### {today.isoformat()}"]
    for change in changes:
        lines = [
            f"Key: `{change.fact.key}`",
            f"Decision: {change.fact.label} = {change.fact.value}",
            "Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）",
            f"Evidence: {change.fact.source}",
        ]
        if change.previous is not None:
            lines.append(f"Supersedes: {change.previous}（{change.previous_date}）")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def mark_superseded(text: str, keys: set[str], today: date) -> str:
    """古い記録に `Superseded by` を**追記する**。

    受入基準「過去の値も Superseded として残っており、**検索で誤って最新扱い
    されない**」がこれ。新しい記録に `Supersedes` を書くだけでは、古い記録**だけ**
    を読んだ人が失効した値に従う（`docs/decisions/README.md` の「追記のみ」と
    同じ理由）。

    **本文は書き換えない。** 行を1つ足すだけ。
    """
    if not keys:
        return text
    marked = []
    for block in text.split("\n\n"):
        key = _key_of(block)
        if key in keys and "Superseded by:" not in block:
            marked.append(block.rstrip() + f"\nSuperseded by: {today.isoformat()}")
        else:
            marked.append(block)
    return "\n\n".join(marked)


def _key_of(block: str) -> str | None:
    for line in block.splitlines():
        if line.startswith("Key: `") and line.rstrip().endswith("`"):
            return line[len("Key: `") : -1]
    return None


def initial(project: str) -> str:
    """まだファイルが無いときの雛形。"""
    return "\n".join(
        [
            "---",
            f"project: {project}",
            "source: coldaisle",
            "---",
            "",
            f"# {project} 運用メモリ",
            "",
            "**いまの値はここだけを見れば分かります。** 経緯は Decisions に積まれます。",
            "",
            "## Current Facts",
            "",
            CURRENT_START,
            CURRENT_END,
            "",
            DECISIONS_HEADING,
            "",
        ]
    )


def apply(text: str, facts: Sequence[Fact], changes: Sequence[Change], today: date) -> str:
    """区画を作り直し、History を積む。**区画の外と過去の記録は触らない。**"""
    recorded = read_current(text)
    block = render_current(facts, recorded, today)
    if CURRENT_START in text and CURRENT_END in text:
        head, rest = text.split(CURRENT_START, 1)
        _, tail = rest.split(CURRENT_END, 1)
        text = head + block + tail
    else:
        # 印が無いファイル（人が先に作った）にも足せるようにする
        text = text.rstrip() + "\n\n## Current Facts\n\n" + block + "\n"
    if not changes:
        return text
    # **古い記録に先に印を付ける。** 新しい記録を積んでからだと、いま積んだものに
    # 「これは古い」と書いてしまう
    text = mark_superseded(
        text, {change.fact.key for change in changes if not change.is_new}, today
    )
    entry = render_history(changes, today)
    if DECISIONS_HEADING in text:
        head, tail = text.split(DECISIONS_HEADING, 1)
        # **新しいものを上に積む。** 古い記録は下へ流れるだけで、書き換わらない。
        # 段落の区切り（空行）を必ず入れる。詰めると、次に印を付けるときに
        # 見出しと記録が1つの段落として扱われる
        body = tail.lstrip()
        return f"{head}{DECISIONS_HEADING}\n\n{entry.rstrip()}\n\n" + (f"{body}\n" if body else "")
    return f"{text.rstrip()}\n\n{DECISIONS_HEADING}\n\n{entry}"


def commit(path: Path, message: str) -> bool:
    """`git add` と `git commit` を1回ずつ。**push はしない。**

    人が差分を追えるようにするため（#40）。シェルを通さず、引数を固定で渡す。
    失敗しても例外にしない（ファイルは既に書けている）。
    """
    for argv in (
        ["git", "add", "--", path.name],
        ["git", "commit", "-m", message, "--", path.name],
    ):
        done = subprocess.run(argv, cwd=path.parent, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            LOGGER.warning(
                "git に失敗した（ファイルは書けている）",
                extra={logs.FIELDS_KEY: {"argv": argv[:2], "stderr": done.stderr.strip()[:200]}},
            )
            return False
    return True


# ---------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-memory`。**既定は「見せるだけ」**（要件 Q-12 / 構想メモ §23）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-memory", description="運用メモリの更新案を出す（既定では書かない）"
    )
    parser.add_argument("--memory", type=Path, default=Path("memory/projects/gpu-server.md"))
    parser.add_argument("--project", default="gpu-server")
    parser.add_argument("--rules", type=Path, default=Path("config/rules.yaml"))
    parser.add_argument("--calibration", type=Path, default=Path("config/calibration.json"))
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--quality-rules", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--timezone", default="Asia/Tokyo")
    parser.add_argument("--apply", action="store_true", help="書き込む。**付けなければ見せるだけ**")
    parser.add_argument("--commit", action="store_true", help="`--apply` と併せて git commit する")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    if args.commit and not args.apply:
        parser.error("--commit は --apply と一緒に使う（書いていないものはコミットできない）")

    logs.configure(args.log_level)
    rules = RuleSet.from_yaml(args.rules)
    calibration = Calibration.from_json(args.calibration)
    clock: Clock = WallClock()
    today = datetime.fromtimestamp(clock.now_ms() / 1000, tz=ZoneInfo(args.timezone)).date()

    store: SqliteStore | None = None
    if args.db.exists():
        store = SqliteStore(args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=clock)
    try:
        facts = collect(rules, calibration, store)
    finally:
        if store is not None:
            store.close()

    text = (
        args.memory.read_text(encoding="utf-8") if args.memory.exists() else initial(args.project)
    )
    changes = diff(facts, read_current(text))
    _report(changes, args.memory, applied=args.apply)
    if not args.apply:
        return 0

    args.memory.parent.mkdir(parents=True, exist_ok=True)
    args.memory.write_text(apply(text, facts, changes, today), encoding="utf-8")
    committed = False
    if args.commit and changes:
        committed = commit(args.memory, f"chore: 運用メモリを更新する（{len(changes)} 件）")
    LOGGER.info(
        "運用メモリを更新した",
        extra={
            logs.FIELDS_KEY: {
                "path": str(args.memory),
                "changes": len(changes),
                "committed": committed,
            }
        },
    )
    return 0


def _report(changes: Sequence[Change], path: Path, *, applied: bool) -> None:
    """人が読んで確認するための出力。**ここが確認の場**なので端末へ出す。"""
    if not changes:
        print(f"変更はありません（{path}）")  # noqa: T201
        return
    print(f"{len(changes)} 件の変更（{path}）")  # noqa: T201
    for change in changes:
        print(f"  {change.as_line()}")  # noqa: T201
    if not applied:
        print("\n書き込んでいません。内容を確認して `--apply` を付けてください。")  # noqa: T201


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
