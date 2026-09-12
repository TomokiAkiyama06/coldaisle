"""L0 取り込み: Source 実装（serial / mock / replay）と正規化。

**シリアルポートを開いてよいのはこの層の `SerialSource` だけ**（AGENTS.md ルール6）。
API 層・UI 層・AI 層からデバイスを直接触らない。

**この層は上位レイヤを import しない。** 取り込み・保存・ルールを束ねるのは
`coldaisle.daemon`（合成の起点）の役目である。

`daemon` をここから再輸出しない。**`python -m coldaisle.daemon` で
二重に読み込まれ、`runpy` が JSON でない警告行を stderr へ出す。**
構造化ログを行単位で読む集約側が壊れる（AGENTS.md コード規約）。
デーモンは `from coldaisle.daemon import Daemon` で取る。
"""

from coldaisle.ingest.calibration import Calibration, CalibrationPolicy
from coldaisle.ingest.mock import MockSource, Scenario, load_scenarios
from coldaisle.ingest.normalize import Normalizer
from coldaisle.ingest.protocol import (
    SAMPLE_CHANNELS,
    RawHello,
    RawMessage,
    RawSample,
    RawSensor,
    Source,
    decode_line,
)
from coldaisle.ingest.replay import ReplaySource
from coldaisle.ingest.serial_source import SerialSource

__all__ = [
    "SAMPLE_CHANNELS",
    "Calibration",
    "CalibrationPolicy",
    "MockSource",
    "Normalizer",
    "RawHello",
    "RawMessage",
    "RawSample",
    "RawSensor",
    "ReplaySource",
    "Scenario",
    "SerialSource",
    "Source",
    "decode_line",
    "load_scenarios",
]
