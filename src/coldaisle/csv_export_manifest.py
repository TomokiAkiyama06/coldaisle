"""日次 CSV の export manifest と、CSV の時刻の写像（レイヤ横断）。決定記録 0100 §2.1 / §2.4。

日次 CSV（``store/csv_export.py``。L1）を書く export と、それを読む再生
（``ingest/replay.py``。L0）と試験が**同じ関数**を使うために、``channels`` / ``clock`` の隣に
置く（0100 §2.4。0096 §2.4 と同じ向き）。
ここは store も ingest も import しない。

- **写像**: export の秒は ``ts_ms // 1000``（書いた文字列と同じ秒）。再生の秒は CSV の文字列を
  ``datetime.fromisoformat`` で読み、timezone を当てはめた UTC の Unix 秒（0100 §2.4）
- **manifest**: CSV の横に置く ``sensors_YYYY-MM-DD.export.json``（0100 §2.1 / §5 #2）。
  識別子（ホスト名・ROM・パス・DB の path）を持たない（AGENTS.md ルール 10 / 0021）
- **``export_record_sha256``**: 1つの export の全欄の canonical JSON の SHA-256（0100 §2.8）。
  manifest からも、元の DB の ``csv_exports`` の行からも、この関数で計算する

CSV の形（列・時刻の書式・ファイル名）は変えない（0008 §2.8）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Final, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA: Final = "coldaisle.daily_csv_export"
SCHEMA_VERSION: Final = 1

TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%S"
"""日次 CSV の時刻の書式。従来の出力に合わせる。オフセットもミリ秒も付かない（0008 §2.8）。"""

CSV_PREFIX: Final = "sensors_"
CSV_SUFFIX: Final = ".csv"
MANIFEST_SUFFIX: Final = ".export.json"
"""``sensors_*.csv`` の glob（``ReplaySource.csv_files``）に掛からない名前（0100 §5 #2）。"""

EXPORT_ID_PATTERN: Final = r"^export-[0-9a-f]{32}$"
SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
DAY_PATTERN: Final = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_SECOND = timedelta(seconds=1)


def csv_name_for(day: date) -> str:
    """その日の CSV の basename（``sensors_2026-08-24.csv``）。0008 §2.8 の名前のまま。"""
    return f"{CSV_PREFIX}{day.isoformat()}{CSV_SUFFIX}"


def manifest_name_for(day: date) -> str:
    """その日の export manifest の basename（``sensors_2026-08-24.export.json``）。"""
    return f"{CSV_PREFIX}{day.isoformat()}{MANIFEST_SUFFIX}"


def lock_name_for(day: date) -> str:
    """その日の export のプロセス間 lock ファイルの basename（0100 §2.1 / §5 #17）。

    先頭の ``.`` で隠し、``sensors_*.csv`` の glob にも manifest の名前にも掛からない。
    """
    return f".{CSV_PREFIX}{day.isoformat()}.export.lock"


def manifest_name_for_csv(csv_name: str) -> str | None:
    """CSV の basename に対になる manifest の basename。日次 CSV の名前でなければ ``None``。

    ``sensors_2026-08-24.csv`` → ``sensors_2026-08-24.export.json``。export が書く名前
    （:func:`csv_name_for`）に戻せる名前だけを対にする。
    """
    if not (csv_name.startswith(CSV_PREFIX) and csv_name.endswith(CSV_SUFFIX)):
        return None
    try:
        day = date.fromisoformat(csv_name[len(CSV_PREFIX) : -len(CSV_SUFFIX)])
    except ValueError:
        return None
    if csv_name_for(day) != csv_name:
        return None
    return manifest_name_for(day)


# ---------------------------------------------------------------- 写像（0100 §2.4）


def export_second(ts_ms: int) -> int:
    """export が書く行の絶対時刻（UTC の Unix 秒）。書いた文字列と同じ秒に切り捨てる。"""
    return ts_ms // 1000


def format_local(second: int, tz: ZoneInfo) -> str:
    """UTC の Unix 秒を、CSV に書くローカル時刻の文字列にする（export 側）。"""
    return datetime.fromtimestamp(second, tz=tz).strftime(TIMESTAMP_FORMAT)


def parse_local(text: str, tz: ZoneInfo) -> int:
    """CSV のローカル時刻の文字列を、``tz`` を当てはめた UTC の Unix 秒にする（再生側）。

    オフセット付きの文字列は ``ValueError``。``tz`` で読み替えると、文字列のオフセットと
    当てはめた timezone のどちらを信じたかが分からなくなる（0100 §2.3 の「読み替えない」）。
    DST の曖昧な時刻・存在しない時刻の検出はここでは行わない（``fold=0`` で当てはめる。0100 §2.5 は
    再生の照合（段 2）が行う）。
    """
    when = parse_naive(text)
    return (when.replace(tzinfo=tz) - _EPOCH) // _ONE_SECOND


def parse_naive(text: str) -> datetime:
    """CSV の時刻の文字列を、オフセットを持たないローカル時刻として読む（``fromisoformat``）。"""
    when = datetime.fromisoformat(text)
    if when.tzinfo is not None:
        raise ValueError(f"CSV の時刻にオフセットが付いている: {text!r}")
    return when


def local_time_problem(naive: datetime, tz: ZoneInfo) -> str | None:
    """DST の曖昧な時刻（``"ambiguous"``）・存在しない時刻（``"nonexistent"``）なら理由を返す。

    0100 §2.5 の判定: ``fold=0`` と ``fold=1`` の UTC オフセットが違うか、ローカル → UTC →
    ローカルの往復で元に戻らないか。往復で戻らなければ存在しない時刻（時計が進む区間）、
    戻るのにオフセットが違えば曖昧な時刻（時計が戻る区間）。どちらでもなければ ``None``。
    """
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    round_trip = first.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if round_trip != naive:
        return "nonexistent"
    if first.utcoffset() != second.utcoffset():
        return "ambiguous"
    return None


def row_seconds_sha256(seconds: Iterable[int]) -> str:
    """行の絶対時刻（UTC の Unix 秒）の列の SHA-256（0100 §2.1 の ``row_seconds_sha256``）。

    各行の秒を10進の ASCII にし、末尾に ``\\n`` を付けて行の順に連結した bytes を hash する
    （行が無ければ空の bytes）。行の数と順序も hash に入る。
    """
    digest = hashlib.sha256()
    for second in seconds:
        digest.update(f"{int(second)}\n".encode("ascii"))
    return digest.hexdigest()


# ---------------------------------------------------------------- manifest


def canonical_json_bytes(obj: Mapping[str, object]) -> bytes:
    """0096 §2.3 と同じ規約の canonical JSON（キー順・区切り・末尾改行）。"""
    return (
        json.dumps(
            dict(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )


class ExportRecord(BaseModel):
    """1回の export の記録。manifest と ``csv_exports`` の行が同じ内容を持つ（0100 §2.1 / §2.6）。

    ``schema`` と ``schema_version`` は manifest のファイルの形を示す定数で、``csv_exports`` の
    列には持たない（表の形そのものが版 1 に当たる）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    export_id: str = Field(pattern=EXPORT_ID_PATTERN)
    csv_name: str
    """対になる CSV の basename だけ。ディレクトリや絶対パスは書かない（0021）。"""
    csv_sha256: str = Field(pattern=SHA256_PATTERN)
    day: str = Field(pattern=DAY_PATTERN)
    timezone: str = Field(min_length=1)
    """export に使った IANA の timezone 名。設定の文字列のまま（正規化しない。0100 §2.2）。"""
    day_start_ms: int = Field(ge=0)
    day_end_ms: int = Field(ge=0)
    timestamp_format: str = Field(min_length=1)
    row_count: int = Field(ge=0)
    row_seconds_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        parsed = date.fromisoformat(self.day)
        if self.csv_name != csv_name_for(parsed):
            raise ValueError(f"csv_name が day と対にならない: {self.csv_name!r}")
        if self.day_start_ms >= self.day_end_ms:
            raise ValueError("day_start_ms が day_end_ms 以上")
        return self

    def manifest_bytes(self) -> bytes:
        """CSV の横に置く manifest の bytes（canonical JSON）。"""
        return canonical_json_bytes(
            {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, **self.model_dump()}
        )

    def record_sha256(self) -> str:
        """``export_record_sha256``（0100 §2.8）。:func:`export_record_sha256` と同じ値。"""
        return export_record_sha256(self)

    @classmethod
    def from_manifest_bytes(cls, raw: bytes) -> ExportRecord:
        """manifest の bytes を読む。形が違えば ``ValueError``（未知の欄・版・schema も拒否）。"""
        loaded = json.loads(raw.decode("utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("export manifest が JSON の object ではない")
        if loaded.pop("schema", None) != SCHEMA:
            raise ValueError("export manifest の schema が違う")
        version = loaded.pop("schema_version", None)
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError(f"export manifest の schema_version が違う: {version!r}")
        return cls.model_validate(loaded)


def export_record_sha256(record: ExportRecord) -> str:
    """1つの export の全欄（``schema_version`` を含む）の canonical JSON の SHA-256（0100 §2.8）。

    canonical JSON は 0096 §2.3 と同じ規約。manifest からも ``csv_exports`` の行からも同じ値になる。
    """
    fields: dict[str, object] = {"schema_version": SCHEMA_VERSION, **record.model_dump()}
    return hashlib.sha256(canonical_json_bytes(fields)).hexdigest()


def export_binding_sha256(records: Iterable[ExportRecord]) -> str:
    """入力の export の束縛の digest ``export_binding_sha256``（0100 §2.8 / 0111 §2.1）。

    ``export_id`` の順に並べた ``[export_id, export_record_sha256]`` の組の列（JSON の
    配列の配列）を、0096 §2.3 と同じ規約（区切り・末尾改行。``ensure_ascii=False``）で
    直列化した bytes の SHA-256。``export_id`` の重複は ``ValueError``（0100 §2.3 の 7。
    呼び出し側が先に拒否している）。
    """
    return export_binding_sha256_of_pairs(
        (record.export_id, export_record_sha256(record)) for record in records
    )


def export_binding_sha256_of_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    """``(export_id, export_record_sha256)`` の組から ``export_binding_sha256`` を計算する。

    dataset に残した組（``ReplayBindingV2``）から、元の manifest 無しで計算し直すために使う
    （0100 §2.8）。:func:`export_binding_sha256` と同じ値になる。
    """
    ordered = sorted(pairs)
    ids = [export_id for export_id, _ in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("export_id が重複している")
    payload = (
        json.dumps(
            [list(pair) for pair in ordered],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    return hashlib.sha256(payload).hexdigest()


EXPORT_FIELDS: Final[tuple[str, ...]] = tuple(ExportRecord.model_fields)
"""``csv_exports`` の列のうち manifest と照合する欄（0100 §2.6 の 2）。表の列順と同じ。"""
