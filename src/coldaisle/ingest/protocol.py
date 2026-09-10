"""デバイス出力の型と `Source` の契約（L0）。

`schemas/device_v1.schema.json`（決定記録 0003）が唯一の参照先。本モジュールは
その写しであり、**片方だけを変えない**。`tests/test_mock_source.py` が
生成したメッセージを実際のスキーマへ通して突き合わせている。

デバイス側のチャネル名は `front_intake` のように短い。`air.` を付けるのは
ホスト側の関心事であり、その対応付けは正規化（#8）が持つ（決定記録 0003）。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldaisle import channels
from coldaisle.clock import Clock

SCHEMA_VERSION: Literal[1] = 1
"""`v`。破壊的変更でのみ増える（決定記録 0003 §2.7）。"""

SAMPLE_CHANNELS = channels.SAMPLE_CHANNELS
"""v1 のサンプルが持つチャネル（要件 §5.2）。定義は `coldaisle.channels`。"""


DS18B20 = "ds18b20"
ROM_PATTERN = r"^[0-9A-F]{16}$"
"""大文字16進16桁（`schemas/device_v1.schema.json` の `sensor.rom`）。

小文字を通すと、ホストは同じプローブを**別物として比べる**ことになる
（FR-403 の判定は文字列の一致）。
"""

ERR_PATTERN = r"^[a-z][a-z0-9_]*:.+$"
"""`<channel>:<reason>`（決定記録 0003 §2.9）。理由の語彙は v1 では固定しない。"""

CHANNEL_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class RawSensor(BaseModel):
    """起動バナーが申告するセンサー1つ分。"""

    model_config = ConfigDict(frozen=True)

    kind: str
    gpio: int | None = Field(default=None, ge=0)
    rom: str | None = Field(default=None, pattern=ROM_PATTERN)
    """DS18B20 の64bit ROM ID。**実機の値をコードへ書かない**（#41）。"""
    res: int | None = Field(default=None, ge=9, le=12)

    @model_validator(mode="after")
    def _ds18b20_is_fully_described(self) -> RawSensor:
        """`kind` が `ds18b20` なら `gpio` / `rom` / `res` は必須（決定記録 0003 §2.4）。

        **欠けたまま受け入れない。** `rom` の無いバナーを基準として記録すると、
        次にプローブを差し替えても比べる相手が無く、FR-403 が黙る。
        """
        if self.kind == DS18B20 and (self.gpio is None or self.rom is None or self.res is None):
            raise ValueError(f"{DS18B20} には gpio / rom / res が要る")
        return self


class RawHello(BaseModel):
    """起動バナー（`type: "hello"`）。電源投入時に1回。"""

    model_config = ConfigDict(frozen=True)

    v: Literal[1] = SCHEMA_VERSION
    type: Literal["hello"] = "hello"
    fw: str
    dev: str
    interval_ms: int = Field(gt=0)
    sensors: dict[str, RawSensor] = Field(min_length=1)
    """**空にしない。** 空を基準として記録すると、次の正しいバナーとの
    突き合わせが意味を失う（`schemas/device_v1.schema.json` の `minProperties`）。
    """

    @model_validator(mode="after")
    def _channel_names_are_well_formed(self) -> RawHello:
        for channel in self.sensors:
            if not re.match(CHANNEL_NAME_PATTERN, channel):
                raise ValueError(f"チャネル名が不正: {channel!r}")
        return self

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
    err: tuple[Annotated[str, Field(pattern=ERR_PATTERN)], ...] = ()
    """`<channel>:<reason>`。**品質の判定には使わない**補助情報（決定記録 0003 §2.9）。"""

    def to_json_obj(self) -> dict[str, Any]:
        """デバイスが出す1行と同じ形にする。チャネルは最上位へ展開する。"""
        obj: dict[str, Any] = {"v": self.v, "type": self.type, "seq": self.seq, "up": self.up}
        obj.update(self.channels)
        if self.err:
            obj["err"] = list(self.err)
        return obj


RawMessage = RawHello | RawSample

_ENVELOPE = frozenset({"v", "type", "seq", "up", "err"})
"""サンプルの最上位でチャネル以外のもの。残りがチャネルになる。"""


def _reject_constant(token: str) -> float:
    """`NaN` / `Infinity` / `-Infinity` を JSON の定数として受け取らない。

    決定記録 0003 §2.8（**実装必須**）。`json.loads` は既定でこれらを受け入れ、
    `jsonschema` からは `number` に見えるのでスキーマでも止まらない。
    `readings.value`（REAL）に入ると**そのメトリクスの min / max / mean が
    恒久的に壊れ**、生データを消したあとはロールアップからも取り除けない。
    """
    raise ValueError(f"JSON として不正な定数: {token}")


def decode_line(line: str) -> RawMessage | None:
    """デバイスの1行を読む。**読めなければ `None`。例外を投げない。**

    ESP32 は起動時にブートログを吐き、USB CDC はその途中から拾えることもある
    （FR-103）。**取り込みを止める理由にしない。**

    `type: "log"` と未知の種別も `None` を返す。決定記録 0003 §2.2 のとおり
    ホストは中身を定義せず、無視する。
    """
    text = line.strip()
    if not text.startswith("{"):
        # ブートログ。JSON として読もうとするまでもない
        return None
    try:
        obj: Any = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        # 非有限値を含む行は**行ごと捨てる**（決定記録 0003 §2.8 / FR-103）
        return None
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    err = obj.get("err", ())
    if not isinstance(err, list | tuple):
        # `err` は配列（決定記録 0003 §2.3）。`null` や文字列は契約違反。
        # **ここで捨てないと、`tuple("abc")` が1文字ずつの理由になる**
        return None
    try:
        if kind == "hello":
            return RawHello.model_validate(obj, strict=True)
        if kind == "s":
            return RawSample.model_validate(
                {
                    **{key: obj[key] for key in ("v", "type", "seq", "up") if key in obj},
                    "channels": _finite_channels(obj),
                    "err": tuple(err),
                },
                strict=True,
            )
    except (ValidationError, ValueError, TypeError):
        return None
    return None


def _finite_channels(obj: dict[str, Any]) -> dict[str, float | None]:
    """チャネルだけを抜く。**非有限値があれば行ごと捨てる。**

    `parse_constant` は `NaN` / `Infinity` の**字面**しか見ない。`1e999` は
    `float()` を通って `inf` になるため、値そのものも確かめる
    （決定記録 0003 §2.8 の趣旨）。
    """
    channels_out: dict[str, float | None] = {}
    for key, value in obj.items():
        if key in _ENVELOPE:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"非有限値: {key}")
        channels_out[key] = value
    return channels_out


@runtime_checkable
class Source(Protocol):
    """デバイス出力の供給元。`serial` / `mock` / `replay` の3実装（FR-101）。

    **シリアルポートを開いてよいのは `SerialSource` だけ**（AGENTS.md ルール3）。
    後始末が必要な実装は `stream()` の内側（`try` / `finally`）で閉じる。
    呼び出し側に `close()` を強いると、閉じ忘れが取り込み停止として現れる。
    """

    @property
    def clock(self) -> Clock:
        """このソースで流すときのホスト受信時刻の供給元（#42）。

        **時間基準を決めるのはソース側**にする。圧縮再生かどうかを知っているのは
        ソースだけであり、決める場所が2つあると、取り込みは圧縮時間・保存は実時計、
        という組み合わせが静かに成立する。デーモンはここから受け取った時計を
        保存層とルールエンジンへそのまま渡す。
        """
        ...

    def stream(self) -> Iterator[RawMessage]:
        """メッセージを届いた順に返す。終端のない実装もある（`idle` シナリオなど）。"""
        ...
