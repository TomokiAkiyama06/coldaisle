"""デバイス出力の型と `Source` の契約（L0）。

`schemas/device_v1.schema.json`（決定記録 0003）が唯一の参照先。本モジュールは
その写しであり、**片方だけを変えない**。`tests/test_mock_source.py` が
生成したメッセージを実際のスキーマへ通して突き合わせている。

デバイス側のチャネル名は `front_intake` のように短い。`air.` を付けるのは
ホスト側の関心事であり、その対応付けは正規化（#8）が持つ（決定記録 0003）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal[1] = 1
"""`v`。破壊的変更でのみ増える（決定記録 0003 §2.7）。"""

SAMPLE_CHANNELS = (
    "room_temp",
    "room_humidity",
    "front_intake",
    "gpu_intake",
    "gpu_exhaust",
    "top_exhaust",
    "rear_exhaust",
)
"""v1 のサンプルが持つチャネル（要件 §5.2）。順序は JSON へ出す順でもある。"""


class RawSensor(BaseModel):
    """起動バナーが申告するセンサー1つ分。"""

    model_config = ConfigDict(frozen=True)

    kind: str
    gpio: int | None = None
    rom: str | None = None
    """DS18B20 の64bit ROM ID。**実機の値をコードへ書かない**（#41）。"""
    res: int | None = None


class RawHello(BaseModel):
    """起動バナー（`type: "hello"`）。電源投入時に1回。"""

    model_config = ConfigDict(frozen=True)

    v: Literal[1] = SCHEMA_VERSION
    type: Literal["hello"] = "hello"
    fw: str
    dev: str
    interval_ms: int = Field(gt=0)
    sensors: dict[str, RawSensor]

    def to_json_obj(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class RawSample(BaseModel):
    """通常サンプル（`type: "s"`）。**正規化前**で、ホスト受信時刻をまだ持たない。

    チャネルを固定の列ではなく辞書で持つのは、決定記録 0003 §2.7 が
    フィールドの追加に寛容であること（ファームが1つ足しただけで取り込みが
    止まらないこと）を型の側でも守るため。
    """

    model_config = ConfigDict(frozen=True)

    v: Literal[1] = SCHEMA_VERSION
    type: Literal["s"] = "s"
    seq: int = Field(ge=0)
    up: int = Field(ge=0)
    channels: dict[str, float | None]
    err: tuple[str, ...] = ()
    """`<channel>:<reason>`。**品質の判定には使わない**補助情報（決定記録 0003 §2.9）。"""

    def to_json_obj(self) -> dict[str, Any]:
        """デバイスが出す1行と同じ形にする。チャネルは最上位へ展開する。"""
        obj: dict[str, Any] = {"v": self.v, "type": self.type, "seq": self.seq, "up": self.up}
        obj.update(self.channels)
        if self.err:
            obj["err"] = list(self.err)
        return obj


RawMessage = RawHello | RawSample


@runtime_checkable
class Source(Protocol):
    """デバイス出力の供給元。`serial` / `mock` / `replay` の3実装（FR-101）。

    **シリアルポートを開いてよいのは `SerialSource` だけ**（AGENTS.md ルール3）。
    後始末が必要な実装は `stream()` の内側（`try` / `finally`）で閉じる。
    呼び出し側に `close()` を強いると、閉じ忘れが取り込み停止として現れる。
    """

    def stream(self) -> Iterator[RawMessage]:
        """メッセージを届いた順に返す。終端のない実装もある（`idle` シナリオなど）。"""
        ...
