"""L0 取り込み: Source 実装（serial / mock / replay）と正規化。

**シリアルポートを開いてよいのはこの層の `SerialSource` だけ**（AGENTS.md ルール3）。
API 層・UI 層・AI 層からデバイスを直接触らない。

`daemon` をここから再輸出しない。**`python -m coldaisle.ingest.daemon` で
二重に読み込まれ、`runpy` が JSON でない警告行を stderr へ出す。**
構造化ログを行単位で読む集約側が壊れる（AGENTS.md コード規約）。
デーモンは `from coldaisle.ingest.daemon import Daemon` で取る。
"""

from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.mock import MockSource, Scenario, load_scenarios
from coldaisle.ingest.normalize import Normalizer
from coldaisle.ingest.protocol import (
    SAMPLE_CHANNELS,
    RawHello,
    RawMessage,
    RawSample,
    RawSensor,
    Source,
)

__all__ = [
    "SAMPLE_CHANNELS",
    "Calibration",
    "MockSource",
    "Normalizer",
    "RawHello",
    "RawMessage",
    "RawSample",
    "RawSensor",
    "Scenario",
    "Source",
    "load_scenarios",
]
