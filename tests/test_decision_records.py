"""決定記録の番号と索引（docs/decisions/）。

**番号は再利用しない**（AGENTS.md「決定記録」）。別々の環境で同じ番号を
採ると、「決定記録 0021」がどちらを指すのか分からなくなる。
実際に `0021` が2件になった（`public-repo-hygiene` と `three-zone-fan-control`）。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECISIONS = ROOT / "docs" / "decisions"
RECORD = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")


def records() -> list[Path]:
    return sorted(path for path in DECISIONS.iterdir() if RECORD.match(path.name))


def number_of(path: Path) -> str:
    matched = RECORD.match(path.name)
    assert matched is not None
    return matched.group(1)


def test_numbers_are_unique():
    """**同じ番号を2件に使わない。** 参照したときに指す先が割れる。"""
    numbers = [number_of(path) for path in records()]
    duplicated = sorted({number for number in numbers if numbers.count(number) > 1})
    assert duplicated == []


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
        if stated is not None and stated.group(1) != number_of(path):
            wrong.append(f"{path.name}: {heading}")
    assert wrong == []
