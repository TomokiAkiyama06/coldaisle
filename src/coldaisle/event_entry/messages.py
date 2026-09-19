"""書き込み入口のメッセージ（版付き JSON Lines。決定記録 0045 §2.4）。

**受理する種類はホワイトリストで持つ。** 今は `gpu_mode`（#67）だけ。
`workload_hint`（#107）は名前だけ予約し、実装するまで拒否する。

拒否の理由は固定の文言にし、**入力をそのまま反射しない**。応答やログに
書き手の文字列を混ぜると、ログを読む側（人や集計）を騙す材料になる。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

PROTOCOL_VERSION = 1

GPU_MODE_STATE_KEY = "sys.gpu_mode"
"""受理した GPU Mode を映す `system_state` のキー（決定記録 0045 §2.5 / 0040）。"""

RESERVED_TYPES = frozenset({"workload_hint"})
"""入口の設計だけを共有し、まだ受理しない種類（#107。決定記録 0045 §2.9）。"""

NOTE_MAX_CHARS = 200
_SOURCE_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class MessageError(ValueError):
    """受理できないメッセージ。``reason`` は入力を含まない固定の文言。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GpuModeMessage(BaseModel):
    """GPU Mode の切り替え通知（#67）。時刻は持たない（サーバが確定する）。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    v: StrictInt
    type: Literal["gpu_mode"]
    mode: Literal["ai", "compute"]
    source: str | None = Field(default=None, pattern=_SOURCE_PATTERN)
    note: str | None = Field(default=None, max_length=NOTE_MAX_CHARS)

    @field_validator("v")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value != PROTOCOL_VERSION:
            raise ValueError("unsupported version")
        return value

    @field_validator("note")
    @classmethod
    def _note_has_no_control_characters(cls, value: str | None) -> str | None:
        # 改行や端末の制御列を通すと、ログやダッシュボードで別の行・別の表示に化ける
        if value is not None and any(unicodedata.category(ch).startswith("C") for ch in value):
            raise ValueError("note contains control characters")
        return value

    @property
    def kind(self) -> str:
        return self.type

    def payload_json(self) -> str:
        """保存する JSON。キー順を固定し、同じ内容は同じ文字列にする。"""
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def system_state(self) -> tuple[str, str]:
        """同じトランザクションで映す状態（変化時だけ書かれる）。"""
        return GPU_MODE_STATE_KEY, self.mode


AcceptedMessage = GpuModeMessage
"""受理する種類の和。#107 で `WorkloadHintMessage` を足すときはここに加える。"""

_ACCEPTED: dict[str, type[GpuModeMessage]] = {"gpu_mode": GpuModeMessage}

_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        # 同じキーが2つあると、実装によって先勝ち・後勝ちが分かれる
        raise MessageError("duplicate keys")
    return dict(pairs)


def _reject_constant(_: str) -> NoReturn:
    raise MessageError("non-finite numbers are not allowed")


def parse_message(line: bytes) -> AcceptedMessage:
    """1行を検証して受理できるメッセージにする。受理できなければ `MessageError`。"""
    stripped = line.rstrip(b"\n")
    if not stripped.strip():
        raise MessageError("empty message")
    try:
        text = stripped.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MessageError("message is not UTF-8") from exc
    try:
        decoded: Any = json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except MessageError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise MessageError("message is not JSON") from exc
    if not isinstance(decoded, dict):
        raise MessageError("message must be a JSON object")

    kind = decoded.get("type")
    if not isinstance(kind, str) or not _TYPE_PATTERN.match(kind):
        raise MessageError("missing or malformed type")
    if kind in RESERVED_TYPES:
        raise MessageError("type is reserved and not accepted yet")
    model = _ACCEPTED.get(kind)
    if model is None:
        raise MessageError("type is not accepted")
    try:
        return model.model_validate(decoded)
    except ValidationError as exc:
        # どのフィールドかだけを返し、値は返さない。未知のキーは名前も書き手の入力なので
        # 反射せず、既知のフィールド名だけを返す
        known = set(model.model_fields)
        fields = {
            str(error["loc"][0]) if error["loc"] and error["loc"][0] in known else "unknown field"
            for error in exc.errors()
        }
        raise MessageError(f"invalid fields: {', '.join(sorted(fields))}") from exc


def encode_gpu_mode(mode: str, *, source: str | None = None, note: str | None = None) -> bytes:
    """クライアント用: GPU Mode の通知を1行にする。送る前に同じ検証を通す。"""
    body: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": "gpu_mode", "mode": mode}
    if source is not None:
        body["source"] = source
    if note is not None:
        body["note"] = note
    line = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
    parse_message(line)
    return line
