"""マイグレーションが決定記録 0002 の DDL と一致していることの検証（#5）。

決定記録は「唯一の参照先」だが、参照先と実装がずれても普通は誰も気づかない。
0002 の SQL ブロックを機械的に照合し、**片方だけ直した状態を CI で落とす。**

一致の判定は空白の詰め方を無視した完全一致にする。コメントも比較対象に含めるため、
`-- 'air.front_intake'` のような列の意味の説明も勝手に消せない。
"""

import re
import sqlite3
from pathlib import Path

import pytest

from coldaisle.store import migrations
from coldaisle.store.models import METRIC_PATTERN

ROOT = Path(__file__).resolve().parents[1]
DECISION = ROOT / "docs" / "decisions" / "0002-metric-naming.md"

SQL_BLOCK = re.compile(r"```sql\n(.*?)```", re.DOTALL)
TEXT_BLOCK = re.compile(r"```text\n(.*?)```", re.DOTALL)


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _decision_sql_blocks() -> list[str]:
    return SQL_BLOCK.findall(DECISION.read_text(encoding="utf-8"))


def test_decision_has_sql_blocks():
    """抽出に失敗していたら以降のテストが素通しになるので、まず本数を固定する。"""
    assert len(_decision_sql_blocks()) == 7


@pytest.mark.parametrize("block", _decision_sql_blocks())
def test_every_decision_ddl_is_in_the_migration(block):
    migration = _normalize((migrations.MIGRATIONS_DIR / "0001_initial.sql").read_text("utf-8"))
    assert _normalize(block) in migration


def test_migration_creates_exactly_the_decided_objects():
    """マイグレーション側にだけ存在するテーブル・ビューが無いことを確かめる。

    上のテストは「決定記録 → マイグレーション」の包含しか見ない。
    逆向きを見ないと、記録に無いテーブルを足しても緑のままになる。
    """
    conn = sqlite3.connect(":memory:")
    try:
        migrations.apply_pending(conn, now_ms=0)
        objects = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()

    assert objects == {
        ("table", "readings"),
        ("table", "system_state"),
        ("table", "readings_1m"),
        ("table", "readings_1h"),
        ("table", "alerts"),
        ("index", "ix_alerts_state"),
        ("index", "ix_alerts_started"),
        ("table", "devices"),
        ("table", "device_sensors"),
        ("table", "schema_version"),
        ("view", "v_latest"),
    }


def test_metric_pattern_matches_the_decision():
    """命名規約の正規表現も決定記録の写しであることを固定する（0002 §2.1）。"""
    blocks = [_normalize(block) for block in TEXT_BLOCK.findall(DECISION.read_text("utf-8"))]
    assert METRIC_PATTERN.pattern in blocks
