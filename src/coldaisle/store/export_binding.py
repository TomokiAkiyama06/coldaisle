"""再生の入力の export と、本番の DB の ``csv_exports`` の照合（L1）。決定記録 0100 §2.6 / §2.8。

- **Dataset v2 の生成**: ``--replay-path`` の manifest を、``--calibration-history-db`` の
  ``csv_exports`` と全欄で照合する（:func:`read_production_records`）。較正の記録と
  **同じ読み取り専用の接続・同じ read transaction**で読む。照合を通ったら封をした
  :class:`ExportBinding` を返す
- **学習の入口**: dataset に残した ``export_id`` の行を、同じく較正の記録と同じ接続で読む
  （:func:`read_training_records`）。照合そのものは合成の起点が行う（dataset の型は control 側）

``ExportBinding`` は store と合成の起点だけが扱い、``control/model`` へは渡さない（0100 §2.6）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from coldaisle.csv_export_manifest import ExportRecord, export_binding_sha256
from coldaisle.store.calibration_history import (
    CalibrationHistory,
    CalibrationHistoryError,
    read_calibration_history_with,
)
from coldaisle.store.csv_export import TABLE, read_csv_export
from coldaisle.store.models import SequencedControlTrace


class ExportBindingError(ValueError):
    """入力の export が本番の DB の ``csv_exports`` と照合できない（0100 §2.6）。"""


_SEAL = object()


class ExportBinding:
    """``csv_exports`` と全欄で照合した入力の export。

    封をした型で、:func:`read_production_records` だけが作る。
    """

    __slots__ = ("_digest", "_records", "_timezone")

    def __init__(self, seal: object, records: tuple[ExportRecord, ...]) -> None:
        if seal is not _SEAL:
            raise TypeError("ExportBinding は read_production_records() だけが作る")
        self._records = tuple(sorted(records, key=lambda record: record.export_id))
        timezones = {record.timezone for record in records}
        if len(timezones) != 1:
            raise ExportBindingError(f"入力の export の timezone が1つでない: {sorted(timezones)}")
        self._timezone = timezones.pop()
        self._digest = export_binding_sha256(records)

    @property
    def records(self) -> tuple[ExportRecord, ...]:
        """``export_id`` の順の、照合を通った export の記録。"""
        return self._records

    @property
    def timezone(self) -> str:
        """入力の export の timezone（すべて同じ文字列）。"""
        return self._timezone

    @property
    def export_binding_sha256(self) -> str:
        """入力の export の束縛の digest（0100 §2.8。形は PR #256 で固定）。"""
        return self._digest


def _read_rows(
    connection: sqlite3.Connection, export_ids: Iterable[str]
) -> dict[str, ExportRecord | None]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    ids = sorted(set(export_ids))
    if exists is None:
        # migration 0012 の前の DB。行が無いのと同じく、照合は通らない（0100 §2.7）
        return dict.fromkeys(ids)
    return {export_id: read_csv_export(connection, export_id) for export_id in ids}


def read_training_records(
    path: Path, export_ids: Iterable[str]
) -> tuple[CalibrationHistory, Mapping[str, ExportRecord | None]]:
    """較正の記録と、``export_ids`` の ``csv_exports`` の行を同じ read transaction で読む。

    無い行は ``None``。行の形が壊れていれば :class:`ExportBindingError`。
    """
    try:
        return read_calibration_history_with(path, lambda conn: _read_rows(conn, export_ids))
    except (CalibrationHistoryError, ExportBindingError):
        raise
    except ValueError as error:
        # 行の形が ExportRecord の検証に外れる（trigger を外した DB など）
        raise ExportBindingError(f"csv_exports の行を読めない: {error}") from error


def read_production_records(
    path: Path, records: Iterable[ExportRecord]
) -> tuple[CalibrationHistory, ExportBinding]:
    """Dataset v2 の生成: 較正の記録を読み、入力の manifest を ``csv_exports`` と全欄で照合する。

    すべての manifest の ``export_id`` の行があり、各欄（``csv_name`` / ``csv_sha256`` / ``day`` /
    ``timezone`` / ``day_start_ms`` / ``day_end_ms`` / ``timestamp_format`` / ``row_count`` /
    ``row_seconds_sha256``）が一致しなければ :class:`ExportBindingError`（0100 §2.6 の 1 / 2）。
    呼び出し側はこれを通るまで被覆と変更の検査（0099 §2.6）へ進まない（同 3）。
    """
    manifests = _require_manifests(records)
    history, rows = read_training_records(path, (record.export_id for record in manifests))
    return history, _bind(manifests, rows)


def _require_manifests(records: Iterable[ExportRecord]) -> tuple[ExportRecord, ...]:
    manifests = tuple(records)
    if not manifests:
        raise ExportBindingError("照合する export が無い")
    return manifests


def _bind(
    manifests: tuple[ExportRecord, ...], rows: Mapping[str, ExportRecord | None]
) -> ExportBinding:
    for record in manifests:
        row = rows.get(record.export_id)
        if row is None:
            raise ExportBindingError(
                "csv_exports に export_id の行が無い（別の DB から export した CSV か、"
                f"段 1 の前の export）: {record.export_id}"
            )
        if row != record:
            differing = sorted(
                name
                for name, value in record.model_dump().items()
                if row.model_dump()[name] != value
            )
            raise ExportBindingError(
                f"csv_exports の行が manifest と違う: {record.export_id} の {differing}"
            )
    return ExportBinding(_SEAL, manifests)


@dataclass(frozen=True)
class TrainingProduction:
    """学習の入口が本番の DB から1つの read transaction で読んだもの（決定記録 0112 §2.3）。"""

    history: CalibrationHistory
    binding: ExportBinding
    rows: Mapping[str, ExportRecord | None]
    """``csv_exports`` の行（入力の manifest の ``export_id`` ごと）。"""
    traces: tuple[SequencedControlTrace, ...]
    """run の期間 ``[start_ms, end_ms)`` の ControlTick の trace（記録した順）。"""
    pruned_before_ms: int | None
    """trace の削除の境界。これより前の ``ts_ms`` の行は消したことがある（0071 §2.2a）。"""
    legacy_through_seq: int
    """移行前の行の ``seq`` の上限（0087 §2.1）。"""


def read_training_production(
    path: Path, records: Iterable[ExportRecord], *, start_ms: int, end_ms: int
) -> TrainingProduction:
    """学習の入口: 較正の記録・``csv_exports``・run の期間の trace を同じ read transaction で読む。

    入力の manifest は :func:`read_production_records` と同じく全欄で照合する（0100 §2.6）。
    ControlTick の出どころは本番の DB の trace（決定記録 0112 §2.3）。削除の境界と移行前の
    境界も同じ transaction で読み、呼び出し側が「trace が消えた期間」を判定する。
    """
    manifests = _require_manifests(records)
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("run の期間が不正")

    def read(
        conn: sqlite3.Connection,
    ) -> tuple[dict[str, ExportRecord | None], tuple[SequencedControlTrace, ...], int | None, int]:
        rows = _read_rows(conn, (record.export_id for record in manifests))
        traces = tuple(
            SequencedControlTrace(
                seq=int(row[0]),
                ts_ms=int(row[1]),
                tick_id=int(row[2]),
                schema_version=int(row[3]),
                trace_json=str(row[4]),
            )
            for row in conn.execute(
                "SELECT seq, ts_ms, tick_id, schema_version, trace_json FROM control_traces "
                "WHERE ts_ms >= ? AND ts_ms < ? ORDER BY seq",
                (start_ms, end_ms),
            )
        )
        prune = conn.execute(
            "SELECT pruned_before_ms, legacy_through_seq FROM control_trace_prune"
        ).fetchone()
        if prune is None:
            raise ExportBindingError("本番の DB に control_trace_prune の行が無い")
        return rows, traces, None if prune[0] is None else int(prune[0]), int(prune[1])

    try:
        history, (rows, traces, pruned_before_ms, legacy_through_seq) = (
            read_calibration_history_with(path, read)
        )
    except (CalibrationHistoryError, ExportBindingError):
        raise
    except ValueError as error:
        raise ExportBindingError(f"本番の DB の行を読めない: {error}") from error
    return TrainingProduction(
        history=history,
        binding=_bind(manifests, rows),
        rows=rows,
        traces=traces,
        pruned_before_ms=pruned_before_ms,
        legacy_through_seq=legacy_through_seq,
    )
