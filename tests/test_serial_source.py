"""SerialSource（#12）。

守る線は3つ。

1. **抜線でデーモンが落ちない。** 例外を上へ投げず、指数バックオフで再接続する
2. **読めない行で落ちない。** ESP32 のブートログが混ざる（FR-103）
3. **非有限値は行ごと捨てる**（決定記録 0003 §2.8・実装必須）

実機の抜き差しは `@pytest.mark.hardware`。ここは偽のポートで回す。
"""

from pathlib import Path

import pytest

from coldaisle.ingest.protocol import RawHello, RawSample, decode_line
from coldaisle.ingest.serial_source import (
    BACKOFF_MAX_S,
    DEFAULT_BAUD,
    PORT_PATTERNS,
    SerialSource,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_OUTPUT = Path(__file__).resolve().parents[1] / "firmware" / "sample_output.jsonl"


class FakePort:
    """決まった行を返して、尽きたら切断する偽のポート。"""

    def __init__(self, lines: list[str], *, fail_at_end: bool = True) -> None:
        self._lines = list(lines)
        self._fail_at_end = fail_at_end
        self.closed = False

    def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0).encode("utf-8")
        if self._fail_at_end:
            raise OSError("device disconnected")
        return b""  # 読み取りの時間切れ

    def close(self) -> None:
        self.closed = True


def source_over(
    *connections: list[str],
    port: str | None = "/dev/fake",
    **kwargs,
) -> tuple[SerialSource, list[float]]:
    """接続ごとの行を順に返すソース。眠った秒数も返す。"""
    pending = list(connections)
    slept: list[float] = []

    def opener(_port: str, _baud: int) -> FakePort:
        if not pending:
            raise OSError("no more devices")
        return FakePort(pending.pop(0))

    source = SerialSource(
        port=port,
        opener=opener,
        sleep=slept.append,
        max_reconnects=len(connections),
        **kwargs,
    )
    return source, slept


def lines_of(name: str) -> list[str]:
    return [f"{line}\n" for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- 読めない行（受入基準）


def test_boot_log_does_not_raise():
    """受入基準: **ブートログを混ぜても例外が出ない**（CI で回せる）。"""
    source, _ = source_over(lines_of("serial_with_boot_log.txt"))
    messages = list(source.stream())
    assert len(messages) == 3
    assert source.dropped_lines == 9


def test_invalid_json_is_dropped():
    source, _ = source_over(lines_of("serial_invalid_json.txt"))
    assert list(source.stream()) == []
    assert source.dropped_lines > 0


def test_blank_lines_are_not_counted_as_dropped():
    """空行は**捨てた行に数えない。** 数えると、溢れた数が意味を持たなくなる。"""
    source, _ = source_over(["\n", "   \n", '{"v":1,"type":"s","seq":1,"up":1}\n'])
    assert len(list(source.stream())) == 1
    assert source.dropped_lines == 0


def test_the_firmware_output_is_read():
    """**実際のファームウェアの出力例**を読めること（#11 と繋がっていること）。"""
    source, _ = source_over([f"{line}\n" for line in SAMPLE_OUTPUT.read_text("utf-8").splitlines()])
    messages = list(source.stream())
    assert isinstance(messages[0], RawHello)
    assert [type(m).__name__ for m in messages[1:]] == ["RawSample"] * 5
    assert source.dropped_lines == 2  # `type:"log"` の2行


# ---------------------------------------------------------------- 非有限値（決定記録 0003 §2.8）


@pytest.mark.parametrize(
    "line",
    [
        '{"v":1,"type":"s","seq":1,"up":1,"room_temp":NaN}',
        '{"v":1,"type":"s","seq":1,"up":1,"room_temp":Infinity}',
        '{"v":1,"type":"s","seq":1,"up":1,"room_temp":-Infinity}',
        '{"v":1,"type":"s","seq":1,"up":1,"room_temp":1e999}',
    ],
)
def test_non_finite_values_drop_the_whole_line(line):
    """**行ごと捨てる**（決定記録 0003 §2.8・実装必須）。

    `readings.value`（REAL）に入ると、そのメトリクスの min / max / mean が
    恒久的に壊れる。生データを消したあとはロールアップからも取り除けない。
    """
    assert decode_line(line) is None


def test_null_is_not_confused_with_non_finite():
    """`null` は「測れなかった」であって、捨てる理由ではない。"""
    message = decode_line('{"v":1,"type":"s","seq":1,"up":1,"room_temp":null}')
    assert isinstance(message, RawSample)
    assert message.channels == {"room_temp": None}


# ---------------------------------------------------------------- 種別


def test_log_lines_are_ignored():
    """`type:"log"` はホストが中身を定義しない（決定記録 0003 §2.2）。"""
    assert decode_line('{"v":1,"type":"log","msg":"line_truncated"}') is None


def test_unknown_types_are_ignored():
    """**未知の種別でそのサンプルだけを捨てる。** デーモンは動き続ける。"""
    assert decode_line('{"v":1,"type":"future","x":1}') is None


def test_a_wrong_schema_version_is_rejected():
    assert decode_line('{"v":2,"type":"s","seq":1,"up":1}') is None


def test_a_missing_required_field_is_rejected():
    assert decode_line('{"v":1,"type":"s","up":1}') is None  # seq が無い


def test_err_is_carried_through():
    message = decode_line('{"v":1,"type":"s","seq":1,"up":1,"rear_exhaust":null,"err":["a:-127"]}')
    assert isinstance(message, RawSample)
    assert message.err == ("a:-127",)


# ---------------------------------------------------------------- 再接続（受入基準）


def test_a_disconnect_does_not_escape():
    """**例外を上へ投げない。** 投げるとデーモンが落ち、監視が止まったことが伝わらない。"""
    source, _ = source_over(
        ['{"v":1,"type":"s","seq":1,"up":1}\n'],
        ['{"v":1,"type":"s","seq":2,"up":2}\n'],
    )
    assert [m.seq for m in source.stream()] == [1, 2]  # type: ignore[union-attr]
    assert source.reconnects == 2


def test_the_port_is_closed_on_disconnect():
    closed: list[FakePort] = []

    def opener(_port: str, _baud: int) -> FakePort:
        handle = FakePort(['{"v":1,"type":"s","seq":1,"up":1}\n'])
        closed.append(handle)
        return handle

    source = SerialSource(port="/dev/fake", opener=opener, sleep=lambda _s: None, max_reconnects=1)
    list(source.stream())
    assert all(handle.closed for handle in closed)


def test_the_backoff_doubles_and_is_capped():
    """1 → 2 → 4 …… 30 で頭打ち。**上限を置く**（復帰に何時間もかけない）。"""
    slept: list[float] = []

    def opener(_port: str, _baud: int) -> FakePort:
        raise OSError("nope")

    source = SerialSource(port="/dev/fake", opener=opener, sleep=slept.append, max_reconnects=12)
    list(source.stream())
    assert slept[:5] == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert max(slept) == BACKOFF_MAX_S
    assert slept[-1] == BACKOFF_MAX_S


def test_the_backoff_resets_after_a_successful_connection():
    """**繋がったら間隔を戻す。** 戻さないと、一度荒れただけで以降ずっと遅い。"""
    source, slept = source_over(
        ['{"v":1,"type":"s","seq":1,"up":1}\n'],
        ['{"v":1,"type":"s","seq":2,"up":2}\n'],
    )
    list(source.stream())
    assert slept == [1.0, 1.0]


def test_a_read_timeout_is_not_a_disconnect():
    """空の `readline()` は時間切れ。**切断として扱わない。**"""
    handle = FakePort(['{"v":1,"type":"s","seq":1,"up":1}\n'], fail_at_end=False)
    reads = {"count": 0}
    original = handle.readline

    def readline() -> bytes:
        reads["count"] += 1
        if reads["count"] > 4:
            raise OSError("disconnected")
        return original()

    handle.readline = readline  # type: ignore[method-assign]
    source = SerialSource(
        port="/dev/fake",
        opener=lambda _p, _b: handle,
        sleep=lambda _s: None,
        max_reconnects=0,
    )
    assert len(list(source.stream())) == 1
    assert reads["count"] == 5  # 時間切れで抜けずに読み続けた


# ---------------------------------------------------------------- ポートの決め方


def test_an_explicit_port_skips_detection():
    calls: list[tuple[str, ...]] = []
    source = SerialSource(
        port="/dev/fake",
        opener=lambda _p, _b: FakePort([]),
        finder=lambda patterns: calls.append(patterns) or [],  # type: ignore[func-returns-value]
        sleep=lambda _s: None,
        max_reconnects=0,
    )
    list(source.stream())
    assert calls == []


def test_detection_prefers_the_first_name_in_order():
    """**毎回同じものを選ぶ。** 見つかった順にすると、起動ごとに相手が変わる。"""
    opened: list[str] = []

    def opener(port: str, _baud: int) -> FakePort:
        opened.append(port)
        return FakePort([])

    source = SerialSource(
        port=None,
        opener=opener,
        finder=lambda _p: ["/dev/cu.usbmodem99", "/dev/cu.usbmodem01"],
        sleep=lambda _s: None,
        max_reconnects=0,
    )
    list(source.stream())
    assert opened == ["/dev/cu.usbmodem01"]


def test_no_port_found_keeps_retrying(caplog):
    """**見つからないことを黙らない。** 落ちもしない。"""
    source = SerialSource(
        port=None,
        opener=lambda _p, _b: FakePort([]),
        finder=lambda _p: [],
        sleep=lambda _s: None,
        max_reconnects=2,
    )
    assert list(source.stream()) == []
    assert "見つからない" in caplog.text


def test_multiple_candidates_are_reported(caplog):
    source = SerialSource(
        port=None,
        opener=lambda _p, _b: FakePort([]),
        finder=lambda _p: ["/dev/ttyACM0", "/dev/ttyACM1"],
        sleep=lambda _s: None,
        max_reconnects=0,
    )
    list(source.stream())
    assert "候補が複数" in caplog.text


def test_the_patterns_cover_macos_and_linux():
    assert "/dev/cu.usbmodem*" in PORT_PATTERNS
    assert "/dev/ttyACM*" in PORT_PATTERNS


def test_the_baud_matches_the_firmware():
    """デバイス側の `Serial.begin` と揃っていること。"""
    sketch = (
        Path(__file__).resolve().parents[1]
        / "firmware"
        / "coldaisle_sensor"
        / "coldaisle_sensor.ino"
    ).read_text(encoding="utf-8")
    assert f"Serial.begin({DEFAULT_BAUD})" in sketch


# ---------------------------------------------------------------- 時計


def test_the_clock_is_wall_time():
    """シリアルは実時間で届く（#42）。圧縮再生の時計を使わない。"""
    source = SerialSource(port="/dev/fake")
    assert type(source.clock).__name__ == "WallClock"


# ---------------------------------------------------------------- 実機


@pytest.mark.hardware
def test_a_real_device_streams_samples():
    """**USB を抜き差ししても落ちない**（#12 の受入基準）。

    実機を繋いで走らせる。`uv run pytest -m hardware`。
    手で抜き差ししながら、例外が出ずにサンプルが再開することを見る。
    """
    from coldaisle.ingest.serial_source import find_ports

    ports = find_ports()
    if not ports:
        pytest.skip("シリアルポートが見つからない")
    source = SerialSource(max_reconnects=1)
    seen = 0
    for _message in source.stream():
        seen += 1
        if seen >= 3:
            break
    assert seen >= 3
