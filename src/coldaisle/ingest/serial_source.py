"""SerialSource: USB CDC から読む（#12）。

**シリアルポートを開いてよいのはここだけ**（AGENTS.md ルール6）。

守っていること。

1. **例外を上へ投げない。** 抜線はこの層で処理し、再接続を待つ。投げると
   デーモンが落ち、「監視が止まったこと」が誰にも伝わらない
2. **黙って止まらない。** 再接続の間もサンプルを出さないだけなので、
   無音が続けば `SENSOR_FAULT`（FR-401）が鳴る
3. **読めない行で落ちない。** ESP32 のブートログが混ざる（FR-103）

`stream()` は終端しない。抜線は「終わり」ではなく「いま届いていない」である。
"""

from __future__ import annotations

import glob
import logging
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol

from coldaisle import logs
from coldaisle.clock import Clock, WallClock
from coldaisle.ingest.protocol import RawMessage, decode_line

LOGGER = logging.getLogger("coldaisle.ingest.serial")

DEFAULT_BAUD = 115200
"""デバイス側と揃える（`firmware/coldaisle_sensor` の `Serial.begin`）。"""

PORT_PATTERNS: tuple[str, ...] = ("/dev/cu.usbmodem*", "/dev/ttyACM*")
"""macOS と Linux の並び。**見つけた順ではなく、名前の順で決める**（毎回同じ選択にする）。"""

READ_TIMEOUT_S = 1.0
"""1回の読み取りで待つ上限。**無限に待たない**（停止要求に気づけなくなる）。"""

BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 30.0
"""再接続の間隔。1 → 2 → 4 …… 30 で頭打ち。

**上限を置く。** 置かないと、長時間の抜線のあと復帰までに何時間もかかる。
"""


class SerialPort(Protocol):
    """`serial.Serial` のうち、ここが使う部分だけ。**試験で差し替える。**"""

    def readline(self) -> bytes: ...

    def close(self) -> None: ...


def find_ports(patterns: tuple[str, ...] = PORT_PATTERNS) -> list[str]:
    """候補のポート。**名前の順**に返す。"""
    found: list[str] = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))
    return sorted(set(found))


def open_port(port: str, baud: int) -> SerialPort:
    """本物のポートを開く。**`pyserial` を import するのはここだけ。**"""
    import serial  # 実機が無い環境での import 失敗を、この関数の中だけに閉じ込める

    return serial.Serial(port, baud, timeout=READ_TIMEOUT_S)


@dataclass
class SerialSource:
    """`Source` のシリアル実装（FR-101 / FR-103）。"""

    port: str | None = None
    """明示指定。`None` なら自動検出（`COLDAISLE_SERIAL_PORT` は CLI が渡す）。"""
    baud: int = DEFAULT_BAUD
    patterns: tuple[str, ...] = PORT_PATTERNS
    opener: Callable[[str, int], SerialPort] = open_port
    finder: Callable[[tuple[str, ...]], list[str]] = find_ports
    sleep: Callable[[float], None] = time.sleep
    _clock: Clock = field(default_factory=WallClock)
    max_reconnects: int | None = None
    """再接続の回数の上限。**試験でだけ使う**（本番は無制限）。"""

    dropped_lines: int = 0
    """読めなかった行の数。**黙って捨てるが、数は残す。**"""
    reconnects: int = 0

    @property
    def clock(self) -> Clock:
        """実時計。シリアルは実時間で届く（#42）。"""
        return self._clock

    def stream(self) -> Iterator[RawMessage]:
        """届いた順に返す。**終端しない。**"""
        backoff = BACKOFF_START_S
        attempts = 0
        while True:
            port = self.port or self._choose()
            if port is None:
                LOGGER.warning(
                    "シリアルポートが見つからない",
                    extra={logs.FIELDS_KEY: {"patterns": list(self.patterns)}},
                )
            else:
                connected = yield from self._read_from(port)
                if connected:
                    # **繋がったあとの切断は、間を置かずに試す。**
                    #
                    # 開けなかった場合まで戻すと、いつまでも開けないポートを
                    # 1秒ごとに叩き続けることになる（指数バックオフの意味が無い）
                    backoff = BACKOFF_START_S
            attempts += 1
            if self.max_reconnects is not None and attempts > self.max_reconnects:
                return
            self.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
            self.reconnects += 1

    # ------------------------------------------------------------------ 内部

    def _choose(self) -> str | None:
        # **名前の順にするのはここ。** 探す側が何を返しても、選び方は変わらない。
        # 見つかった順にすると、起動ごとに読む相手が変わりうる
        candidates = sorted(self.finder(self.patterns))
        if not candidates:
            return None
        if len(candidates) > 1:
            # **どれを選んだかを言う。** 黙って1つ選ぶと、別の機器を読んでいても気づけない
            LOGGER.warning(
                "候補が複数ある。名前の順で先頭を使う",
                extra={logs.FIELDS_KEY: {"candidates": candidates}},
            )
        return candidates[0]

    def _read_from(self, port: str) -> Generator[RawMessage, None, bool]:
        """1回の接続ぶん。**切断したら黙って戻る**（例外を上へ投げない）。

        戻り値は「開けたかどうか」。呼び出し側が再接続の間隔を戻すかを決める。
        """
        try:
            handle = self.opener(port, self.baud)
        except Exception as error:
            LOGGER.warning(
                "シリアルポートを開けない",
                extra={logs.FIELDS_KEY: {"port": port, "error": str(error)[:200]}},
            )
            return False
        LOGGER.info("シリアルポートに接続した", extra={logs.FIELDS_KEY: {"port": port}})
        dropped_before = self.dropped_lines
        try:
            while True:
                raw = handle.readline()
                if not raw:
                    continue  # 読み取りの時間切れ。**切断ではない**
                message = self._decode(raw)
                if message is not None:
                    yield message
        except Exception as error:
            # **抜線はここで終わる。** 上へ投げるとデーモンが落ちる
            LOGGER.warning(
                "シリアルが切れた（再接続する）",
                extra={logs.FIELDS_KEY: {"port": port, "error": str(error)[:200]}},
            )
        finally:
            with suppress(Exception):
                handle.close()
            dropped = self.dropped_lines - dropped_before
            if dropped:
                # 1行ごとには出さない。**ブートログで埋まる**
                LOGGER.info(
                    "読めない行を捨てた（ブートログなど）",
                    extra={logs.FIELDS_KEY: {"port": port, "dropped": dropped}},
                )
        return True

    def _decode(self, raw: bytes) -> RawMessage | None:
        text = raw.decode("utf-8", errors="replace")
        message = decode_line(text)
        if message is None and text.strip():
            self.dropped_lines += 1
        return message
