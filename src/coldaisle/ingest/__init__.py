"""L0 取り込み: Source 実装（serial / mock / replay）と正規化。

**シリアルポートを開いてよいのはこの層の `SerialSource` だけ**（AGENTS.md ルール3）。
API 層・UI 層・AI 層からデバイスを直接触らない。
"""

from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.daemon import Daemon
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
    "Daemon",
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
