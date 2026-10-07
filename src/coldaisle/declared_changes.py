"""宣言された変更（``DeclaredChange``）の入力の形。決定記録 0056 §2.5 / 0109。

合成の起点 ``coldaisle-drift``（証拠 YAML の ``changes:``）と ``coldaisle-dataset`` の v2
（``--declared-changes`` の YAML）が、1件の形と ``kind`` の写し方を共有する。合成の起点どうしを
import させないため、ここに置く（0109 §2.1）。
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, field_validator

from coldaisle.control.drift.model import MAX_DECLARED_CHANGES, ChangeKind, DeclaredChange

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class DeclaredChangesError(ValueError):
    """宣言のファイルを読めない・検証に外れる（0109 §2.3）。dataset を作らない。"""


def normalize_declared_changes(value: object) -> object:
    """YAML の変更一覧を ``DeclaredChange`` へ渡せる形にする（drift と dataset で共有）。

    記録の型は strict なので、``kind`` の文字列は**ここで明示的に写す**。未知の種別は
    ``ChangeKind`` が拒む（黙って落とすと、宣言したはずの交換が無かったことになる）。
    list でない値はそのまま返し、型の検証に拒ませる。
    """
    if not isinstance(value, list):
        return value
    normalized: list[object] = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("kind"), str):
            normalized.append({**item, "kind": ChangeKind(item["kind"])})
        else:
            normalized.append(item)
    return tuple(normalized)


def _change_order(change: DeclaredChange) -> tuple[int, str, str]:
    """宣言の全順序（すべての欄で決める。集合の反復順に委ねない）。"""
    return (change.ts_ms, change.kind.value, change.detail)


class DeclaredChangesFile(BaseModel):
    """``coldaisle-dataset --declared-changes`` のファイル（0109 §2.1 / §2.2）。

    ``changes`` に**既定値を置かない**。宣言が無いときも ``changes: []`` を明示させる（0087 §2.6）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    changes: Annotated[tuple[DeclaredChange, ...], BeforeValidator(normalize_declared_changes)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_is_an_int(cls, value: object) -> object:
        # Literal[1] は True も 1 と等しいとみなすので、bool を先に拒む
        if isinstance(value, bool):
            raise ValueError("schema_version は整数の 1")
        return value

    @field_validator("changes", mode="after")
    @classmethod
    def _deduplicate_then_bound(
        cls, value: tuple[DeclaredChange, ...]
    ) -> tuple[DeclaredChange, ...]:
        # 完全に同じ件を1件にまとめてから上限を掛ける（drift の検知器と同じ順。0109 §2.3 の 5 / 6）
        unique = tuple(sorted(set(value), key=_change_order))
        if len(unique) > MAX_DECLARED_CHANGES:
            raise ValueError(
                f"宣言された変更の数が構造上限を超えた（{len(unique)} > {MAX_DECLARED_CHANGES}）"
            )
        return unique


@dataclass(frozen=True, slots=True)
class DeclaredChangesInput:
    """検証済みの宣言と、読んだファイルの bytes の SHA-256（ログに残す。0109 §2.5）。"""

    changes: tuple[DeclaredChange, ...]
    file_sha256: str


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """mapping の重複した鍵を拒否する（0109 §5 #5）。

    ``yaml.safe_load`` は重複した鍵を黙って後の値で上書きする。
    """


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    seen: set[str] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            # 鍵は文字列だけ（`? [a, b]` のような複合の鍵は hash できず、拒否の経路を外れる）
            raise DeclaredChangesError(
                f"mapping の鍵は文字列にする: {key!r}（行 {key_node.start_mark.line + 1}）"
            )
        if key in seen:
            raise DeclaredChangesError(
                f"重複した鍵がある: {key!r}（行 {key_node.start_mark.line + 1}）"
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _read_regular_file(path: Path) -> bytes:
    """symlink を辿らず regular file だけを1回読む（検証した bytes と使う bytes を同じにする）。"""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise DeclaredChangesError(f"宣言のファイルを開けない: {path.name}: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DeclaredChangesError(f"宣言のファイルが regular file ではない: {path.name}")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_declared_changes(path: Path) -> DeclaredChangesInput:
    """``--declared-changes`` のファイルを読み、検証して返す（0109 §2.3）。

    空のファイル・``null``・mapping でない最上位・``changes`` の鍵の欠け・未知の種別・
    strict に外れる値（bool / 浮動小数 / 文字列の時刻）・重複した鍵・上限超えは、すべて
    :class:`DeclaredChangesError`。
    """
    payload = _read_regular_file(path)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DeclaredChangesError(f"宣言のファイルが UTF-8 ではない: {path.name}") from error
    try:
        loaded: object = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as error:
        raise DeclaredChangesError(f"宣言のファイルを YAML として読めない: {path.name}") from error
    if not isinstance(loaded, dict):
        raise DeclaredChangesError(
            f"宣言のファイルの最上位が mapping ではない: {path.name}"
            "（宣言が無いときも schema_version: 1 と changes: [] を書く）"
        )
    try:
        parsed = DeclaredChangesFile.model_validate(loaded)
    except ValidationError as error:
        raise DeclaredChangesError(f"宣言のファイルが検証に外れる: {path.name}: {error}") from error
    return DeclaredChangesInput(
        changes=parsed.changes, file_sha256=hashlib.sha256(payload).hexdigest()
    )


__all__ = [
    "DeclaredChangesError",
    "DeclaredChangesFile",
    "DeclaredChangesInput",
    "normalize_declared_changes",
    "read_declared_changes",
]
