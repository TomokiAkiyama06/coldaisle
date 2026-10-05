"""構造化ログ（JSON Lines）。#8

`print` を使わない（AGENTS.md コード規約）。1行1件の JSON にして、
`jq` で絞り込める形にする。障害のときに読むのは人間だが、
**24時間連続運転の記録を目で追うのは無理**なので、機械で絞れる形を優先する。

ログの時刻は**常に実時計**。`--speed 60` の圧縮再生でも、ログが指すのは
「実際にいつ起きたか」である。シナリオ上の時刻が要る行には、
呼び出し側が `ts_ms` フィールドとして添える（両方が要る場面があるため）。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

FIELDS_KEY = "fields"
"""`logger.info("...", extra={FIELDS_KEY: {...}})` で構造化フィールドを渡す。"""


class JsonLinesFormatter(logging.Formatter):
    """1レコードを1行の JSON にする。

    ``ensure_ascii`` を真にすると、非 ASCII を ``\\uXXXX`` へ逃がす（JSON としては同じ値）。
    出力先の encoding が日本語を表せなくても、行が欠けない。
    """

    def __init__(self, *, ensure_ascii: bool = False) -> None:
        super().__init__()
        self._ensure_ascii = ensure_ascii

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # record.created は実時計。ログは実時間の出来事を指す
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, FIELDS_KEY, None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            # 例外は握りつぶさない。取り込みループだけは継続するが、痕跡は残す
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=self._ensure_ascii, default=str)


def configure(
    level: str = "INFO", stream: TextIO | None = None, *, ensure_ascii: bool = False
) -> None:
    """ルートロガーを JSON Lines へ差し替える。

    既存のハンドラを置き換える。二重に出ると、行数を数えて欠測を推定する
    運用（NFR-02）が狂う。

    ``ensure_ascii`` は、行が欠けると困る監査の記録（`coldaisle-authority`）のためにある。
    `StreamHandler` は encode の失敗を内部で握って traceback を出すだけなので、
    `PYTHONIOENCODING=ascii` の stderr では日本語の行が**黙って欠ける**。
    """
    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.setFormatter(JsonLinesFormatter(ensure_ascii=ensure_ascii))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
