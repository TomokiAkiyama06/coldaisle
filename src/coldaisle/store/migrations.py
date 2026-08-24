"""連番SQLの適用（決定記録 0002 §2.11）。

規則は決定記録と同じく**追記のみ**。適用済みの SQL ファイルは書き換えない。
書き換えると、既存の DB と新規に作った DB でスキーマが分岐し、
どちらが正しいかを後から判定する手段がなくなる。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
"""`0001_initial.sql`。番号は0埋め4桁で、ファイル名順＝適用順になるようにする。"""


class MigrationError(RuntimeError):
    """マイグレーションの並びが壊れている、または DB がコードより新しい。"""


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path

    def read_sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """ディレクトリ内の SQL を番号順に返す。

    番号の飛びと重複を**読み込み時に**落とす。飛びを許すと、後から
    欠番を埋めるファイルが追加されたときに、DB ごとに適用済みの集合が
    変わってしまう（新規 DB は適用され、既存 DB は素通しされる）。
    """
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        matched = _FILENAME.match(path.name)
        if matched is None:
            raise MigrationError(f"命名が `NNNN_<slug>.sql` に合わない: {path.name}")
        migrations.append(Migration(version=int(matched.group(1)), path=path))

    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"連番が飛んでいるか重複している: {versions}")
    return tuple(migrations)


def current_version(conn: sqlite3.Connection) -> int:
    """適用済みの最大バージョン。未適用の DB は 0。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        # schema_version 自体を 0001 が作るため、初回だけはテーブルが無い
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return 0 if row[0] is None else int(row[0])


def _pending(conn: sqlite3.Connection, migrations: tuple[Migration, ...]) -> list[Migration]:
    version = current_version(conn)
    newest = migrations[-1].version if migrations else 0
    if version > newest:
        raise MigrationError(
            f"DB のスキーマ ({version}) がコード ({newest}) より新しい。"
            "古いバージョンで開くと壊れるため中止する"
        )
    return [migration for migration in migrations if migration.version > version]


def _statements(script: str) -> Iterator[str]:
    """SQL スクリプトを文単位に分ける。

    `executescript()` を使わないのは、**実行前に暗黙 COMMIT を打つ**ため。
    それでは適用前に取った書き込みロックが外れ、バージョンの判定と適用の間に
    他プロセスが割り込める（`apply_pending` を参照）。

    分割は `sqlite3.complete_statement()` に任せる。`;` で素朴に切ると、
    文字列リテラル中の `;` やトリガ本体（`BEGIN ... END;`）で壊れる。
    """
    parts = script.split(";")
    buffer = ""
    for part in parts[:-1]:
        buffer += part + ";"
        if sqlite3.complete_statement(buffer):
            yield buffer.strip()
            buffer = ""
    remainder = "\n".join(
        line
        for line in (buffer + parts[-1]).splitlines()
        if line.strip() and not line.lstrip().startswith("--")
    )
    if remainder:
        raise MigrationError(f"末尾の文が `;` で終わっていない: {remainder[:80]!r}")


def apply_pending(
    conn: sqlite3.Connection, now_ms: int, directory: Path = MIGRATIONS_DIR
) -> tuple[int, ...]:
    """未適用のマイグレーションを適用し、適用したバージョンを返す。

    呼び出し側は**自動コミット接続**（`isolation_level=None`）であること。
    ここで明示的にトランザクションを制御する。

    **バージョンの判定と適用を同じ書き込みトランザクションで行う。**
    取り込みデーモンと API が新しい DB を同時に開くと、両方が「未適用」と
    判定してから片方が適用し、もう片方が `table readings already exists` で
    落ちる。`busy_timeout` では防げない（待つ前に判定が終わっているため）。
    `BEGIN IMMEDIATE` でロックを取ってから判定し直す。

    適用は丸ごと成功するか丸ごと巻き戻る。中途半端なスキーマが残ると、
    次回の起動が「テーブルが既にある」で失敗し、手作業でしか復旧できない。
    """
    migrations = discover(directory)
    if not _pending(conn, migrations):
        # 大半の起動はここで終わる。適用が要らないなら書き込みロックを取らない
        return ()

    conn.execute("BEGIN IMMEDIATE")
    try:
        # ロックを待っている間に他プロセスが適用し終えている可能性がある
        applied: list[int] = []
        for migration in _pending(conn, migrations):
            for statement in _statements(migration.read_sql()):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_version (version, applied_ms) VALUES (?, ?)",
                (migration.version, now_ms),
            )
            applied.append(migration.version)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return tuple(applied)
