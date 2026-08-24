"""連番SQLの適用（決定記録 0002 §2.11）。

規則は決定記録と同じく**追記のみ**。適用済みの SQL ファイルは書き換えない。
書き換えると、既存の DB と新規に作った DB でスキーマが分岐し、
どちらが正しいかを後から判定する手段がなくなる。
"""

from __future__ import annotations

import re
import sqlite3
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


def apply_pending(
    conn: sqlite3.Connection, now_ms: int, directory: Path = MIGRATIONS_DIR
) -> tuple[int, ...]:
    """未適用のマイグレーションを順に適用し、適用したバージョンを返す。

    1ファイル＝1トランザクション。途中の文で失敗したら、そのファイルの
    変更は丸ごと巻き戻る。中途半端なスキーマが残ると、次回の起動が
    「テーブルが既にある」で失敗し、手作業でしか復旧できなくなる。
    """
    migrations = discover(directory)
    version = current_version(conn)
    newest = migrations[-1].version if migrations else 0
    if version > newest:
        raise MigrationError(
            f"DB のスキーマ ({version}) がコード ({newest}) より新しい。"
            "古いバージョンで開くと壊れるため中止する"
        )

    pending = [migration for migration in migrations if migration.version > version]
    if not pending:
        return ()

    # 既定（LEGACY）の接続では executescript が実行前に暗黙 COMMIT を打つため、
    # スクリプト全体を1トランザクションにできない。PEP 249 準拠モードへ一時的に
    # 切り替えることでこれを止める（Python 3.12 以降）。
    previous_autocommit = conn.autocommit
    conn.autocommit = False
    try:
        applied: list[int] = []
        for migration in pending:
            try:
                conn.executescript(migration.read_sql())
                conn.execute(
                    "INSERT INTO schema_version (version, applied_ms) VALUES (?, ?)",
                    (migration.version, now_ms),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            applied.append(migration.version)
        return tuple(applied)
    finally:
        conn.autocommit = previous_autocommit
        if conn.in_transaction:
            # PEP 249 モードは commit 直後に次のトランザクションを開く。
            # 何も書いていないので捨ててよい（開いたままだと WAL を切り詰められない）
            conn.rollback()
