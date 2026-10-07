"""較正の変更の記録（``calibration_activations``）の形と検証。決定記録 0099 §2.4 / §2.5 / §2.6。

取り込みが新しい較正で起動した時点の行を、書く側（:class:`~coldaisle.store.db.SqliteStore`）と
読む側（:func:`read_calibration_history`）が**同じ規則**で作り・検証する。

- 行の digest（``row_sha256``）は ``id`` と自身を除く全欄の canonical JSON の SHA-256（0099 §2.4）
- 読む側は全行を検証する（再直列化・digest・鎖・単調・隣り合う写像の違い。0099 §2.5）。
  trigger を外した DB を読んでも気づけるようにするため
- :class:`CalibrationHistory` は :func:`read_calibration_history` の外で作れない（0099 §2.6）

ここは store（L1）なので ``ingest`` の ``Calibration`` 型を知らない。写像は
``calibration_offsets``（レイヤ横断）の規約に従う。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from coldaisle.calibration_offsets import canonical_offsets_bytes

TABLE: Final = "calibration_activations"
SOURCE_KINDS: Final = frozenset({"serial", "mock"})
"""記録を書く取り込みの種類。``replay`` は較正を当てないので書かない（0010 §2.9）。"""

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CalibrationHistoryError(ValueError):
    """記録が無い・読めない・§2.5 の検証に外れる。**「変更なし」と読み替えない**（0099 §2.7）。"""


def _canonical_json_bytes(obj: Mapping[str, object]) -> bytes:
    """0099 §2.3 の4（0096 §2.3）と同じ規約の canonical JSON。"""
    return (
        json.dumps(
            dict(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )


def activation_row_sha256(
    *,
    ts_ms: int,
    source_kind: str,
    offsets_sha256: str,
    previous_row_sha256: str | None,
    calibrated_at: str | None,
    calibration_file_sha256: str,
) -> str:
    """行の digest（0099 §2.4）。時刻を含む全欄を鎖に結ぶ。"""
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "ts_ms": ts_ms,
                "source_kind": source_kind,
                "offsets_sha256": offsets_sha256,
                "previous_row_sha256": previous_row_sha256,
                "calibrated_at": calibrated_at,
                "calibration_file_sha256": calibration_file_sha256,
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class CalibrationActivation:
    """``calibration_activations`` の1行（検証済み、または書く前の行）。"""

    ts_ms: int
    source_kind: str
    offsets_json: str
    offsets_sha256: str
    previous_row_sha256: str | None
    calibrated_at: str | None
    calibration_file_sha256: str
    row_sha256: str

    @classmethod
    def next_after(
        cls,
        previous: CalibrationActivation | None,
        *,
        ts_ms: int,
        source_kind: str,
        offsets: Mapping[str, float],
        calibrated_at: str | None,
        calibration_file_sha256: str,
    ) -> CalibrationActivation:
        """``previous`` の次に足す行を作る（digest と鎖はここで計算する）。"""
        offsets_bytes = canonical_offsets_bytes(offsets)
        offsets_digest = hashlib.sha256(offsets_bytes).hexdigest()
        previous_row = None if previous is None else previous.row_sha256
        return cls(
            ts_ms=ts_ms,
            source_kind=source_kind,
            offsets_json=offsets_bytes.decode("utf-8"),
            offsets_sha256=offsets_digest,
            previous_row_sha256=previous_row,
            calibrated_at=calibrated_at,
            calibration_file_sha256=calibration_file_sha256,
            row_sha256=activation_row_sha256(
                ts_ms=ts_ms,
                source_kind=source_kind,
                offsets_sha256=offsets_digest,
                previous_row_sha256=previous_row,
                calibrated_at=calibrated_at,
                calibration_file_sha256=calibration_file_sha256,
            ),
        )

    @property
    def offsets(self) -> dict[str, float]:
        """実効の写像 ``{metric 名: offset}``（後の値。前の値は直前の行）。"""
        return _parse_offsets(self.offsets_json)


def _parse_offsets(text: str) -> dict[str, float]:
    """``offsets_json`` を §2.3 の規約で解析し、再直列化して同じ文字列になることを確かめる。"""
    try:
        loaded: object = json.loads(text)
    except ValueError as error:
        raise CalibrationHistoryError(f"offsets_json が JSON でない: {error}") from error
    if not isinstance(loaded, dict):
        raise CalibrationHistoryError("offsets_json が object でない")
    offsets: dict[str, float] = {}
    for key, value in loaded.items():
        # 写像の値は float（0099 §2.3 の4。整数や真偽値は別の bytes になる）
        if not isinstance(value, float) or not math.isfinite(value):
            raise CalibrationHistoryError(
                f"offsets_json の値が有限の float でない: {key}={value!r}"
            )
        offsets[key] = value
    try:
        canonical = canonical_offsets_bytes(offsets)
    except ValueError as error:
        raise CalibrationHistoryError(f"offsets_json を再直列化できない: {error}") from error
    if canonical != text.encode("utf-8"):
        raise CalibrationHistoryError("offsets_json が canonical でない")
    return offsets


def _row_from_sql(row: sqlite3.Row | tuple[object, ...]) -> CalibrationActivation:
    (
        _id,
        ts_ms,
        source_kind,
        offsets_json,
        offsets_sha256,
        previous_row_sha256,
        calibrated_at,
        calibration_file_sha256,
        row_sha256,
    ) = tuple(row)
    if not isinstance(ts_ms, int) or isinstance(ts_ms, bool):
        raise CalibrationHistoryError(f"ts_ms が整数でない: {ts_ms!r}")
    texts = (source_kind, offsets_json, offsets_sha256, calibration_file_sha256, row_sha256)
    if not all(isinstance(value, str) for value in texts):
        raise CalibrationHistoryError("文字列の欄が文字列でない")
    if previous_row_sha256 is not None and not isinstance(previous_row_sha256, str):
        raise CalibrationHistoryError("previous_row_sha256 が文字列でない")
    if calibrated_at is not None and not isinstance(calibrated_at, str):
        raise CalibrationHistoryError("calibrated_at が文字列でない")
    return CalibrationActivation(
        ts_ms=ts_ms,
        source_kind=str(source_kind),
        offsets_json=str(offsets_json),
        offsets_sha256=str(offsets_sha256),
        previous_row_sha256=previous_row_sha256,
        calibrated_at=calibrated_at,
        calibration_file_sha256=str(calibration_file_sha256),
        row_sha256=str(row_sha256),
    )


SELECT_ROWS: Final = (
    "SELECT id, ts_ms, source_kind, offsets_json, offsets_sha256, previous_row_sha256,"
    f" calibrated_at, calibration_file_sha256, row_sha256 FROM {TABLE} ORDER BY id"
)


def verify_activation_rows(
    rows: Iterable[sqlite3.Row | tuple[object, ...]],
) -> tuple[CalibrationActivation, ...]:
    """全行を 0099 §2.5 の規則で検証し、id の順に返す。外れたらどの行かを言って拒否する。"""
    verified: list[CalibrationActivation] = []
    for index, raw in enumerate(rows):
        try:
            row = _row_from_sql(raw)
            _verify_row(row, verified[-1] if verified else None)
        except CalibrationHistoryError as error:
            raise CalibrationHistoryError(f"{TABLE} の {index + 1} 行目: {error}") from error
        verified.append(row)
    return tuple(verified)


def _verify_row(row: CalibrationActivation, previous: CalibrationActivation | None) -> None:
    if row.source_kind not in SOURCE_KINDS:
        raise CalibrationHistoryError(f"source_kind が serial / mock でない: {row.source_kind!r}")
    if row.ts_ms < 0:
        raise CalibrationHistoryError(f"ts_ms が負: {row.ts_ms}")
    for name in ("offsets_sha256", "calibration_file_sha256", "row_sha256"):
        if _SHA256.fullmatch(getattr(row, name)) is None:
            raise CalibrationHistoryError(f"{name} が64桁の小文字16進でない")
    _parse_offsets(row.offsets_json)
    if hashlib.sha256(row.offsets_json.encode("utf-8")).hexdigest() != row.offsets_sha256:
        raise CalibrationHistoryError("offsets_sha256 が offsets_json の digest と一致しない")
    expected_row = activation_row_sha256(
        ts_ms=row.ts_ms,
        source_kind=row.source_kind,
        offsets_sha256=row.offsets_sha256,
        previous_row_sha256=row.previous_row_sha256,
        calibrated_at=row.calibrated_at,
        calibration_file_sha256=row.calibration_file_sha256,
    )
    if expected_row != row.row_sha256:
        raise CalibrationHistoryError("row_sha256 が全欄から計算し直した値と一致しない")
    if previous is None:
        if row.previous_row_sha256 is not None:
            raise CalibrationHistoryError("最初の行の previous_row_sha256 が NULL でない")
        return
    if row.previous_row_sha256 != previous.row_sha256:
        raise CalibrationHistoryError("previous_row_sha256 が直前の行を指していない（鎖が切れた）")
    if row.ts_ms <= previous.ts_ms:
        raise CalibrationHistoryError(f"ts_ms が単調増加でない: {previous.ts_ms} -> {row.ts_ms}")
    if row.offsets_sha256 == previous.offsets_sha256:
        raise CalibrationHistoryError("隣り合う行の写像が同じ（変化のときだけ足す規則に外れる）")


_SEAL = object()


class CalibrationHistory:
    """検証を通った較正の変更の記録（封をした型。:func:`read_calibration_history` だけが作る）。

    ``control/model`` へは渡さない。合成の起点が時刻の列だけを取り出して渡す（0087 §2.6）。
    """

    __slots__ = ("_rows",)

    def __init__(self, seal: object, rows: tuple[CalibrationActivation, ...]) -> None:
        if seal is not _SEAL:
            raise TypeError("CalibrationHistory は read_calibration_history() だけが作る")
        self._rows = rows

    @property
    def rows(self) -> tuple[CalibrationActivation, ...]:
        """id の順（= 時刻の順）の行。"""
        return self._rows

    @property
    def last(self) -> CalibrationActivation | None:
        return self._rows[-1] if self._rows else None


def read_calibration_history(path: Path) -> CalibrationHistory:
    """本番の DB を**読み取り専用**（``mode=ro``）で開き、検証済みの記録を返す（0099 §2.6）。

    migration を当てない（読む側が本番の DB の schema を進めない）。DB を開けない・表が無い
    （migration 前の DB）・検証に外れる、はすべて :class:`CalibrationHistoryError`。
    表が空のときは行の無い記録を返す（被覆が無いので Dataset v2 の側で拒否される）。
    """
    if not isinstance(path, Path):
        raise TypeError("較正の記録の DB の path を明示して渡す")
    if not path.is_file():
        raise CalibrationHistoryError(f"較正の記録の DB が無い: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as error:
        raise CalibrationHistoryError(f"較正の記録の DB を開けない: {error}") from error
    try:
        try:
            # 1つのスナップショットで読む（取り込みが動いていても行の途中を読まない）
            conn.execute("BEGIN DEFERRED")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
            ).fetchone()
            if exists is None:
                raise CalibrationHistoryError(
                    f"{TABLE} が無い（migration 0011 の前の DB）。"
                    "記録が無いことを変更なしと扱わない"
                )
            raw_rows = conn.execute(SELECT_ROWS).fetchall()
            conn.execute("COMMIT")
        except sqlite3.Error as error:
            raise CalibrationHistoryError(f"較正の記録を読めない: {error}") from error
        return CalibrationHistory(_SEAL, verify_activation_rows(raw_rows))
    finally:
        conn.close()
