"""L1 永続化: SQLite、ロールアップ、CSVエクスポート。

上位レイヤ（API / ルールエンジン / AI）はここから公開されたものだけを使う。
`sqlite3` の行や SQL 文字列を層の外へ出さない。出すと、保存形式を変えるたびに
上位レイヤを直すことになり、決定記録 0002 の「スキーマ変更を起きない前提にする」が
上位に漏れる。
"""

from coldaisle.store.db import Aggregation, SqliteStore
from coldaisle.store.migrations import MigrationError
from coldaisle.store.models import (
    LatestReading,
    Quality,
    Reading,
    RollupPoint,
    Sample,
    SeriesPoint,
    Stats,
)
from coldaisle.store.quality import QualityRules, classify

__all__ = [
    "Aggregation",
    "LatestReading",
    "MigrationError",
    "Quality",
    "QualityRules",
    "Reading",
    "RollupPoint",
    "Sample",
    "SeriesPoint",
    "SqliteStore",
    "Stats",
    "classify",
]
