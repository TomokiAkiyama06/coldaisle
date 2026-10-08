"""測定値の名前の規約と品質（レイヤ横断）。#261 / 決定記録 0113 §2.3

メトリクス名の文法（決定記録 0002 §2.1）・派生メトリクスの予約プレフィクス（0002 §2.2）・
測定値の品質（要件 §5.3、0002 §2.5）は、保存層（store）だけでなく制御・メトリクス定義・
取り込みが共有する。もとは `coldaisle.store.models` に置いていたが、そこから import すると
Python が先に `coldaisle/store/__init__.py` を実行し、SQLite の永続化層（`store.db` と
`sqlite3`）まで読み込む。Learned MPC worker は frame だけを入力にする（0077 §2.7 / §2.10）
ので、その保証を import の構造で持てるよう、`clock` / `channels` / `metrics` と同じく
パッケージ直下に置く。

**ここは標準ライブラリ以外を import しない。** store・control などから読まれる最下層のため。
`coldaisle.store.models` は互換のためにここの名前を再 export する（同じオブジェクト）。
"""

from __future__ import annotations

import re
from enum import StrEnum

METRIC_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.([a-z][a-z0-9_]*|[0-9]+)){1,3}$")
"""決定記録 0002 §2.1 の文法。`gpu.0.core` のような添字セグメントを許す。"""

DERIVED_PREFIX = "d."
"""派生メトリクスの予約プレフィクス。保存しない（決定記録 0002 §2.2）。"""


def validate_metric(name: str) -> str:
    """メトリクス名を検証して返す。規約に合わなければ `ValueError`。

    保存する `Reading` と、照会の引数を受け取る `SqliteStore` の両方から呼ぶ。
    検証を書き込み側だけに置くと、保存できない名前で照会できてしまい、
    「0件」なのか「そもそも存在しえない名前」なのかを呼び出し側が区別できない。
    """
    if name.startswith(DERIVED_PREFIX):
        raise ValueError(f"派生メトリクスは保存しない（決定記録 0002 §2.2）: {name!r}")
    if not METRIC_PATTERN.match(name):
        raise ValueError(f"命名規約に合わない（決定記録 0002 §2.1）: {name!r}")
    return name


class Quality(StrEnum):
    """測定値の品質（要件 §5.3、決定記録 0002 §2.5）。"""

    OK = "ok"
    MISSING = "missing"
    SUSPECT = "suspect"
    STALE = "stale"
