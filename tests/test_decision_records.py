"""決定記録の番号と索引（docs/decisions/）。

**番号は再利用しない**（AGENTS.md「決定記録」）。別々の環境で同じ番号を
採ると、「決定記録 0021」がどちらを指すのか分からなくなる。
実際に `0021` が2件になった（`public-repo-hygiene` と `three-zone-fan-control`）。
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DECISIONS = ROOT / "docs" / "decisions"

NUMBERED = re.compile(r"^(\d{4})-.+\.md$")
"""番号の付いた記録。**名前の形が崩れていても拾う**（崩れていることは別の試験で言う）。"""

CONVENTION = re.compile(r"^\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")

BASE_REFS = ("origin/main", "main")


def records() -> list[Path]:
    return sorted(path for path in DECISIONS.iterdir() if NUMBERED.match(path.name))


def number_of(name: str) -> str:
    matched = NUMBERED.match(name)
    assert matched is not None
    return matched.group(1)


def test_every_record_file_follows_the_naming_convention():
    """**形の崩れた名前で検査の外に出さない**（#97 のレビュー指摘）。

    `0026-Fan_Control.md` のような名前を「番号付きではない」として読み飛ばすと、
    番号の重複も索引の漏れも見逃す。README 以外の Markdown はすべてこの形にする。
    """
    wrong = sorted(
        path.name
        for path in DECISIONS.glob("*.md")
        if path.name != "README.md" and not CONVENTION.match(path.name)
    )
    assert wrong == []


def test_numbers_are_unique():
    """**同じ番号を2件に使わない。** 参照したときに指す先が割れる。"""
    numbers = [number_of(path.name) for path in records()]
    duplicated = sorted({number for number in numbers if numbers.count(number) > 1})
    assert duplicated == []


def _merge_base() -> str | None:
    for ref in BASE_REFS:
        done = subprocess.run(
            ["git", "merge-base", ref, "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    return None


def test_numbers_are_not_reassigned():
    """**過去に使った番号を別の記録へ付け替えない**（#97 のレビュー指摘）。

    いまの木だけを見ると、記録を消した番号に別の記録を入れても重複にならず通る。
    分岐元（main）と比べ、既にある番号は同じ記録のまま残っていることを確かめる。
    番号ごと消すことも認めない（取り下げは `Status: Rejected` で残す）。

    **別の番号へ移すこと（0021 → 0026）は認める。** 移った先の番号が以前は
    使われていなければ、付け替えではない。

    分岐元より前の履歴は見ない。`0021` は既に2件の記録に使われた過去があり、
    全履歴と比べると**直しようがなく永久に落ちる**（決定記録 0021 §2.6 と同じ理由）。
    """
    base = _merge_base()
    if base is None:
        assert not os.environ.get("CI"), "CI で履歴が取れていない（fetch-depth を確認）"
        pytest.skip("main が無いため履歴と比べられない")
    listed = subprocess.run(
        ["git", "ls-tree", "--name-only", base, "docs/decisions/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    before: dict[str, set[str]] = {}
    for path in listed:
        name = Path(path).name
        if NUMBERED.match(name):
            before.setdefault(number_of(name), set()).add(name)
    after: dict[str, set[str]] = {}
    for record in records():
        after.setdefault(number_of(record.name), set()).add(record.name)

    problems = []
    for number, names in sorted(before.items()):
        now = after.get(number, set())
        if not now:
            problems.append(f"{number}: 番号ごと消えている（以前は {sorted(names)}）")
        elif now - names:
            problems.append(
                f"{number}: {sorted(now - names)} が、以前は {sorted(names)} の番号を使っている"
            )
    assert problems == []


def test_every_record_is_indexed():
    """**索引に無い記録は、無いのと同じ。** 読む人は索引から辿る。"""
    index = (DECISIONS / "README.md").read_text(encoding="utf-8")
    missing = [path.name for path in records() if f"]({path.name})" not in index]
    assert missing == []


def test_the_heading_number_matches_the_file_number():
    """見出しの番号とファイル名の番号を食い違わせない（振り直したときの書き忘れ）。

    見出しに `NNNN:` の形で番号を持つ記録だけを見る。`0001` のように日付を
    見出しにしている記録は対象外にする（**既存の記録は書き換えない**）。
    """
    wrong = []
    for path in records():
        heading = next(
            (
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("# ")
            ),
            "",
        )
        stated = re.search(r"\b(\d{4}):", heading)
        if stated is not None and stated.group(1) != number_of(path.name):
            wrong.append(f"{path.name}: {heading}")
    assert wrong == []
