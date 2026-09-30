"""管理ソケットのプロトコル（版付きの JSON Lines。決定記録 0072 §2.3）。

0045 §2.4 と同じ枠組み（1接続につき1行の要求と1行の応答、UTF-8 の JSON object を `\\n` で
終える、`extra = forbid`、strict、`v` は `1` のみ、入力を応答に反射しない）を使う。
**名前空間は 0045 と共有しない**（`type` ではなく `op` で区別する）。取り違えて別の入口へ
送っても、どちらも未知として拒否する。

受理する `op` はホワイトリストで持つ。**`raise_authority` は存在しない**（管理ソケットは
制御権を増やせない。0072 §2.1）。段階 1（#74）では `lower_authority` / `rollback_authority` を
`unsupported_op` で拒否する（0072 §2.10）。

拒否の理由は固定の code にし、**入力をそのまま反射しない。**
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

PROTOCOL_VERSION = 1

REASON_MAX_CHARS = 200

KNOWN_OPS = frozenset({"set_mode", "lower_authority", "rollback_authority", "status"})
"""プロトコルが定める `op`（0072 §2.3）。"""

UNSUPPORTED_OPS = frozenset({"lower_authority", "rollback_authority"})
"""形は決まっているが、この段階では受理しない `op`（0072 §2.10 段階 2 で受理する）。"""

RESERVED_MODES = frozenset({"calibration"})
"""#75 の測定計画の参照形が決まるまで拒否するモード（0072 §2.3 / §2.10 段階 4）。"""


class RequestError(ValueError):
    """受理できない要求。``code`` は入力を含まない固定の code。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    v: StrictInt

    @field_validator("v")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value != PROTOCOL_VERSION:
            raise ValueError("unsupported version")
        return value


class ManualRequested(BaseModel):
    """`MANUAL` の zone ごとの demand。**3つすべて**を `0.0..1.0` で持つ（PWM は受け取らない）。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    front: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    rear: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    top: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class SetModeRequest(_Request):
    """`set_mode`（0072 §2.3）。時刻・操作者名・設定値・path を受け取らない。"""

    op: Literal["set_mode"]
    mode: Literal["auto", "manual", "max"]
    requested: ManualRequested | None = None
    lease_s: StrictInt | None = Field(default=None, ge=1)
    """上限（`manual.max_lease_s`）は設定で決まるので、受付スレッドが確かめる。"""
    reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)

    @field_validator("reason")
    @classmethod
    def _reason_has_no_control_characters(cls, value: str) -> str:
        # 改行や端末の制御列を通すと、ログや監査の表示で別の行・別の表示に化ける
        if any(unicodedata.category(ch).startswith("C") for ch in value):
            raise ValueError("reason contains control characters")
        return value

    @property
    def weakens_cooling(self) -> bool:
        """冷却を弱めうる指令か（0072 §2.7）。**指令の種類だけで保守的に決める。**

        `MAX` への移行だけが安全側。`AUTO` へ戻すことも、`MANUAL` の値が Fallback より高いと
        冷却が下がるので弱めうる側に入れる。実際の demand の比較では決めない。
        """
        return self.mode != "max"

    def body(self) -> dict[str, Any]:
        """監査の表へ残す検証済みの本文（キー順を固定する）。"""
        return self.model_dump(mode="json", exclude_none=True)


class StatusRequest(_Request):
    """`status`（読むだけ）。"""

    op: Literal["status"]


AdminRequest = SetModeRequest | StatusRequest


def parse_request(line: bytes) -> AdminRequest:
    """1行を検証して受理できる要求にする。受理できなければ `RequestError`。"""
    stripped = line.rstrip(b"\n")
    if not stripped.strip():
        raise RequestError("empty_message")
    try:
        text = stripped.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestError("not_utf8") from exc
    try:
        decoded: Any = json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except RequestError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RequestError("not_json") from exc
    if not isinstance(decoded, dict):
        raise RequestError("not_an_object")
    version = decoded.get("v")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise RequestError("unsupported_version")
    op = decoded.get("op")
    if not isinstance(op, str) or op not in KNOWN_OPS:
        raise RequestError("unknown_op")
    if op in UNSUPPORTED_OPS:
        raise RequestError("unsupported_op")
    if op == "status":
        return _validate(StatusRequest, decoded)
    if decoded.get("mode") in RESERVED_MODES:
        raise RequestError("unsupported_mode")
    request = _validate(SetModeRequest, decoded)
    manual = request.mode == "manual"
    if manual != (request.requested is not None) or manual != (request.lease_s is not None):
        # requested と lease は MANUAL のときだけ必須で、ほかのモードでは拒否する
        raise RequestError("invalid_fields")
    return request


def _validate[T: BaseModel](model: type[T], decoded: dict[str, Any]) -> T:
    try:
        return model.model_validate(decoded)
    except ValidationError as exc:
        # どのフィールドかも返さない（未知のキーの名前も書き手の入力）
        raise RequestError("invalid_fields") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise RequestError("duplicate_keys")
    return dict(pairs)


def _reject_constant(_name: str) -> NoReturn:
    raise RequestError("non_finite_number")


def encode(body: dict[str, Any]) -> bytes:
    """1行の JSON にする（要求・応答の両方）。"""
    return (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def encode_request(body: dict[str, Any]) -> bytes:
    """クライアント用: 要求を1行にする。**送る前にサーバと同じ形の検証を通す。**"""
    line = encode(body)
    parse_request(line)
    return line
