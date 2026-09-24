"""書き込み入口のメッセージ（版付き JSON Lines。決定記録 0045 §2.4）。

**受理する種類はホワイトリストで持つ。** 今は `gpu_mode`（#67）と
`workload_hint`（#107 / 決定記録 0064）の2つ。どちらも「記録と文脈」であって
「指令」ではない（0064 §2.2）。

`workload_hint` は Stage A（0064 §2.10）で**記録するだけ**で、制御はこれを読まない。
形だけで決まる検証はモデルが持ち、設定で決まる上限（受理する `hint_v`・
`expected_duration_s` の上限）は `parse_message` が `WorkloadHintLimits` で確かめる。

拒否の理由は固定の文言にし、**入力をそのまま反射しない**。応答やログに
書き手の文字列を混ぜると、ログを読む側（人や集計）を騙す材料になる。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    ValidationInfo,
    field_validator,
)

PROTOCOL_VERSION = 1

GPU_MODE_STATE_KEY = "sys.gpu_mode"
"""受理した GPU Mode を映す `system_state` のキー（決定記録 0045 §2.5 / 0040）。"""

RESERVED_TYPES: frozenset[str] = frozenset()
"""入口の設計だけを共有し、まだ受理しない種類。

`workload_hint` は決定記録 0064 で形が決まり、受理する側へ移った（#107）。
"""

IMPLEMENTED_HINT_VERSIONS = frozenset({1})
"""このコードが形を知っている `hint_v`（決定記録 0064 §2.3）。

設定（`workload_hint.accepted_hint_versions`）はこの部分集合しか受け付けない。
形を知らない版を設定だけで受理させると、古い形の検証で新しい意味の行を通してしまう。
"""

NOTE_MAX_CHARS = 200
_SOURCE_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


@dataclass(frozen=True)
class WorkloadHintLimits:
    """設定で決まる Workload Hint の上限（`config/event-entry.yaml`。決定記録 0064 §2.3）。"""

    accepted_hint_versions: frozenset[int]
    max_expected_duration_s: int


class MessageError(ValueError):
    """受理できないメッセージ。``reason`` は入力を含まない固定の文言。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _EntryMessage(BaseModel):
    """受理する種類に共通の封筒（`v`）と、保存の形。時刻は持たない（サーバが確定する）。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    v: StrictInt
    type: str
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

    def system_state(self) -> tuple[str, str] | None:
        """同じトランザクションで映す状態（変化時だけ書かれる）。既定は映さない。"""
        return None


class GpuModeMessage(_EntryMessage):
    """GPU Mode の切り替え通知（#67）。"""

    type: Literal["gpu_mode"]
    mode: Literal["ai", "compute"]

    def system_state(self) -> tuple[str, str]:
        """Server Health（0040）が読む `sys.gpu_mode` へ映す。"""
        return GPU_MODE_STATE_KEY, self.mode


HintWorkload = Literal["training", "benchmark", "inference_service"]
"""Workload Hint の閉じた語彙（決定記録 0064 §2.3）。

「これから暇になる」向きの種類は持たない。冷却を弱める向きのヒントは、
書き手が間違えたときにいちばん高くつく（0064 §2.8）。
"""


class WorkloadHintMessage(_EntryMessage):
    """これから流す負荷の申告（#107 / 決定記録 0064 §2.3）。

    **記録と文脈であって指令ではない。** Stage A（0064 §2.10）では `events` に残すだけで、
    制御はこれを読まない。`system_state` にも映さない（0064 §2.4）。ヒントは真値ではなく、
    「いまの値」を1箇所に置くと読み取り API 越しに真値として読む経路ができる。

    時刻（`ts`）・識別子（`job_id` / `user` / `host` / `pid` / パス）・進捗
    （`progress` / `step` / `epoch`）は持たない。未知のフィールドとして拒否される。
    """

    type: Literal["workload_hint"]
    hint_v: StrictInt
    phase: Literal["start", "end"]
    # validate_default: 省略されたときも phase との組み合わせを確かめるため
    workload: HintWorkload | None = Field(default=None, validate_default=True)
    expected_duration_s: StrictInt | None = Field(default=None, ge=1, validate_default=True)

    @field_validator("hint_v")
    @classmethod
    def _implemented_hint_version(cls, value: int) -> int:
        if value not in IMPLEMENTED_HINT_VERSIONS:
            raise ValueError("unsupported hint version")
        return value

    @field_validator("workload")
    @classmethod
    def _workload_matches_phase(
        cls, value: HintWorkload | None, info: ValidationInfo
    ) -> HintWorkload | None:
        phase = info.data.get("phase")
        if phase == "start" and value is None:
            raise ValueError("workload is required on start")
        if phase == "end" and value is not None:
            # end は「いま有効なヒントを取り消す」だけ。どの負荷を終えたかは言わせない
            raise ValueError("workload is not allowed on end")
        return value

    @field_validator("expected_duration_s")
    @classmethod
    def _duration_only_on_start(cls, value: int | None, info: ValidationInfo) -> int | None:
        # 取り消しに期間は無い。読み手のいないフィールドは黙って意味が変わる（0064 §2.3）
        if info.data.get("phase") == "end" and value is not None:
            raise ValueError("expected_duration_s is not allowed on end")
        return value


AcceptedMessage = GpuModeMessage | WorkloadHintMessage
"""受理する種類の和。"""

_ACCEPTED: dict[str, type[GpuModeMessage] | type[WorkloadHintMessage]] = {
    "gpu_mode": GpuModeMessage,
    "workload_hint": WorkloadHintMessage,
}

_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        # 同じキーが2つあると、実装によって先勝ち・後勝ちが分かれる
        raise MessageError("duplicate keys")
    return dict(pairs)


def _reject_constant(_: str) -> NoReturn:
    raise MessageError("non-finite numbers are not allowed")


def parse_message(line: bytes, *, hint_limits: WorkloadHintLimits) -> AcceptedMessage:
    """1行を検証して受理できるメッセージにする。受理できなければ `MessageError`。

    ``hint_limits`` は設定で決まる Workload Hint の上限。入口（サーバ）は必ずこれを通す。
    """
    message = _parse_shape(line)
    if isinstance(message, WorkloadHintMessage):
        _check_hint_limits(message, hint_limits)
    return message


def _check_hint_limits(message: WorkloadHintMessage, limits: WorkloadHintLimits) -> None:
    if message.hint_v not in limits.accepted_hint_versions:
        raise MessageError("unsupported hint version")
    duration = message.expected_duration_s
    if duration is not None and duration > limits.max_expected_duration_s:
        # 申告で期限を伸ばせると、1回の誤った書き込みが長く残る（0064 §2.6）
        raise MessageError("expected_duration_s is too long")


def _parse_shape(line: bytes) -> AcceptedMessage:
    """設定に依らない形だけの検証。クライアントが送る前にも使う。"""
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
    return _encoded(body)


def encode_workload_hint(
    phase: str,
    *,
    workload: str | None = None,
    expected_duration_s: int | None = None,
    source: str | None = None,
    note: str | None = None,
) -> bytes:
    """クライアント用: Workload Hint を1行にする（決定記録 0064 §2.3）。

    送る前に形の検証を通す。設定で決まる上限（受理する `hint_v`・期間の上限）は
    サーバだけが知っている。クライアントは設定ファイルを読まずに呼ばれうるため。
    """
    body: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "workload_hint",
        "hint_v": max(IMPLEMENTED_HINT_VERSIONS),
        "phase": phase,
    }
    optional = {
        "workload": workload,
        "expected_duration_s": expected_duration_s,
        "source": source,
        "note": note,
    }
    body.update({key: value for key, value in optional.items() if value is not None})
    return _encoded(body)


def _encoded(body: dict[str, Any]) -> bytes:
    line = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
    _parse_shape(line)
    return line
