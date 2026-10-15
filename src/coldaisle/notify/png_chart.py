"""通知に添える折れ線グラフを PNG で描く（#334 / 決定記録 0147 §2.3 / §2.5）。

**標準ライブラリだけで書く。** 取り込みのプロセスに画像ライブラリ（Pillow / matplotlib）を
載せないため（0147 §2.5）。PNG は `zlib` と `struct` で組み、線は整数の座標で引き、
文字は 5×7 の点の字形を 2 倍にして置く。描けるのは ASCII だけで、日本語の凡例は
呼び出し側がファイルのコメントに書く。

**決定論的。** 同じ入力からは同じ bytes ができる（乱数・時刻・環境に依らない）。
LLM は関わらない（AGENTS.md ルール 1・8）。
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

Rgb = tuple[int, int, int]

BACKGROUND: Rgb = (0xFC, 0xFC, 0xFB)
TEXT: Rgb = (0x0B, 0x0B, 0x0B)
TEXT_MUTED: Rgb = (0x52, 0x51, 0x4E)
GRID: Rgb = (0xE4, 0xE3, 0xDF)
AXIS: Rgb = (0xA8, 0xA7, 0xA2)
THRESHOLD: Rgb = (0xD0, 0x3B, 0x3B)
"""閾値の線。状態の色（critical）で、系列の色とは重ねない。"""

SERIES_COLORS: tuple[Rgb, ...] = (
    (0x2A, 0x78, 0xD6),  # 青
    (0xEB, 0x68, 0x34),  # 橙
    (0x1B, 0xAF, 0x7A),  # 緑（aqua）
    (0xED, 0xA1, 0x00),  # 黄
)
"""系列の色。**固定の順で割り当てる**（巡回させない）。1 つの面に最大 4 系列。"""

MAX_SERIES_PER_PANEL = len(SERIES_COLORS)

SCALE = 2
"""字形の拡大率。5×7 の字形が 10×14 になる。"""
GLYPH_W = 5
GLYPH_H = 7
CHAR_ADVANCE = (GLYPH_W + 1) * SCALE

MARGIN_LEFT = 84
MARGIN_RIGHT = 20
LEGEND_ROW = 22
MARGIN_TOP = 8 + 2 * LEGEND_ROW + 8
"""凡例を 2 行まで置ける高さ。"""
MARGIN_BOTTOM = 34


@dataclass(frozen=True)
class Series:
    """1 本の線。`points` は時刻（ms）の昇順の `(ts_ms, value)`。"""

    name: str
    points: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class Threshold:
    """面の横の破線。`label` は ASCII（例 `thr 40.0`）。"""

    value: float
    label: str


@dataclass(frozen=True)
class Panel:
    """単位が同じ系列の面。**1 つの面に縦軸は 1 つだけ**（0147 §2.3）。"""

    unit: str
    series: tuple[Series, ...]
    thresholds: tuple[Threshold, ...] = ()


def render_png(
    panels: Sequence[Panel],
    *,
    start_ms: int,
    end_ms: int,
    width: int,
    panel_height: int,
    gap_ms: int,
    max_points: int,
) -> bytes:
    """面を縦に並べた PNG の bytes を返す。

    `gap_ms` より離れた隣り合う点は結ばない（欠けを欠けとして見せる）。
    1 系列の点が `max_points` を超えたら同じ間隔で間引く（直近の点は必ず残す）。
    """
    if not panels:
        raise ValueError("描く面が無い")
    if end_ms <= start_ms:
        raise ValueError("期間が空")
    if width < MARGIN_LEFT + MARGIN_RIGHT + 100 or panel_height < MARGIN_TOP + MARGIN_BOTTOM + 40:
        raise ValueError("画像が小さすぎる")
    canvas = _Canvas(width, panel_height * len(panels))
    for index, panel in enumerate(panels):
        _draw_panel(
            canvas,
            panel,
            top=index * panel_height,
            height=panel_height,
            start_ms=start_ms,
            end_ms=end_ms,
            gap_ms=gap_ms,
            max_points=max_points,
        )
    return canvas.to_png()


# ---------------------------------------------------------------------------- 面


def _draw_panel(
    canvas: _Canvas,
    panel: Panel,
    *,
    top: int,
    height: int,
    start_ms: int,
    end_ms: int,
    gap_ms: int,
    max_points: int,
) -> None:
    series = panel.series[:MAX_SERIES_PER_PANEL]
    left = MARGIN_LEFT
    right = canvas.width - MARGIN_RIGHT
    plot_top = top + MARGIN_TOP
    plot_bottom = top + height - MARGIN_BOTTOM

    values = [v for s in series for _, v in s.points] + [t.value for t in panel.thresholds]
    low, high = _value_range(values)
    ticks = _nice_ticks(low, high)
    low, high = min(low, ticks[0]), max(high, ticks[-1])

    def y_of(value: float) -> int:
        ratio = (value - low) / (high - low)
        return round(plot_bottom - ratio * (plot_bottom - plot_top))

    def x_of(ts_ms: int) -> int:
        ratio = (ts_ms - start_ms) / (end_ms - start_ms)
        return round(left + ratio * (right - left))

    # 罫線と縦軸の目盛り
    decimals = _decimals(ticks)
    for tick in ticks:
        y = y_of(tick)
        canvas.hline(left, right, y, GRID)
        label = f"{tick:.{decimals}f}"
        label_x = left - 8 - len(label) * CHAR_ADVANCE
        canvas.text(label_x, y - GLYPH_H * SCALE // 2, label, TEXT_MUTED)
    # 横軸: 10 分ごと。終わりからの相対（-60m … 0m）
    span_min = (end_ms - start_ms) / 60_000
    step_min = 10 if span_min > 20 else 5
    minute = 0
    while minute <= span_min + 1e-9:
        x = x_of(end_ms - minute * 60_000)
        canvas.vline(x, plot_top, plot_bottom, GRID)
        label = f"-{minute}m" if minute else "0m"
        label_w = len(label) * CHAR_ADVANCE
        label_x = min(x - label_w // 2, canvas.width - label_w - 2)  # 右端の 0m を切らない
        canvas.text(label_x, plot_bottom + 10, label, TEXT_MUTED)
        minute += step_min
    canvas.hline(left, right, plot_bottom, AXIS)
    canvas.vline(left, plot_top, plot_bottom, AXIS)

    for threshold in panel.thresholds:
        canvas.dashed_hline(left, right, y_of(threshold.value), THRESHOLD)

    for color, line in zip(SERIES_COLORS, series, strict=False):
        points = _thin(line.points, max_points)
        for index, (ts_ms, value) in enumerate(points):
            joined_before = index > 0 and ts_ms - points[index - 1][0] <= gap_ms
            joined_after = index + 1 < len(points) and points[index + 1][0] - ts_ms <= gap_ms
            if joined_before:
                before_ts, before_value = points[index - 1]
                canvas.thick_line(
                    x_of(before_ts), y_of(before_value), x_of(ts_ms), y_of(value), color
                )
            elif not joined_after:
                # 前後どちらとも結ばれない点は、線にならないので点で見せる
                canvas.dot(x_of(ts_ms), y_of(value), color)

    _draw_legend(canvas, panel, series, top=top, left=left, right=right)


def _draw_legend(
    canvas: _Canvas, panel: Panel, series: Sequence[Series], *, top: int, left: int, right: int
) -> None:
    """上の余白に単位 → 系列 → 閾値の順に並べる。入らなければ 2 行目へ折り返す。"""
    x = left
    y = top + 8
    unit = f"[{panel.unit}]" if panel.unit else ""
    if unit:
        canvas.text(x, y, unit, TEXT)
        x += (len(unit) + 2) * CHAR_ADVANCE
    entries: list[tuple[Rgb, str, bool]] = [
        (color, line.name, False) for color, line in zip(SERIES_COLORS, series, strict=False)
    ] + [(THRESHOLD, threshold.label, True) for threshold in panel.thresholds]
    for color, label, dashed in entries:
        needed = 30 + len(label) * CHAR_ADVANCE
        if x > left and x + needed > right:
            x = left
            y += LEGEND_ROW
        if dashed:
            canvas.dashed_hline(x, x + 22, y + 7, color)
        else:
            canvas.thick_line(x, y + 7, x + 22, y + 7, color)
        canvas.text(x + 30, y, label, TEXT)
        x += needed + 2 * CHAR_ADVANCE


def _value_range(values: Sequence[float]) -> tuple[float, float]:
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return 0.0, 1.0
    low, high = min(finite), max(finite)
    if high - low < 1e-9:
        return low - 1.0, high + 1.0
    pad = (high - low) * 0.05
    return low - pad, high + pad


def _nice_ticks(low: float, high: float) -> list[float]:
    """4〜6 本ほどのきりの良い目盛り（1・2・2.5・5 の 10 の冪倍）。"""
    raw = (high - low) / 4
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.floor(low / step) * step
    ticks: list[float] = []
    value = first
    while value <= high + step * 1e-9:
        ticks.append(round(value, 10))
        value += step
    if ticks[-1] < high:
        ticks.append(round(value, 10))
    return ticks


def _decimals(ticks: Sequence[float]) -> int:
    if len(ticks) < 2:
        return 1
    step = abs(ticks[1] - ticks[0])
    for decimals in range(4):
        if abs(round(step, decimals) - step) < 1e-9:
            return decimals
    return 3


def _thin(points: tuple[tuple[int, float], ...], max_points: int) -> tuple[tuple[int, float], ...]:
    """同じ間隔で間引く。**最後の点（直近）は必ず残す。**"""
    if len(points) <= max_points:
        return points
    stride = math.ceil(len(points) / max_points)
    kept = points[len(points) - 1 :: -stride][::-1]
    return kept


# ---------------------------------------------------------------------------- 画素


class _Canvas:
    """RGB の画素の配列。範囲の外への書き込みは黙って捨てる（線の端が余白へはみ出しても良い）。"""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self._pixels = bytearray(bytes(BACKGROUND) * (width * height))

    def set(self, x: int, y: int, color: Rgb) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self._pixels[offset : offset + 3] = bytes(color)

    def hline(self, x0: int, x1: int, y: int, color: Rgb) -> None:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            self.set(x, y, color)

    def vline(self, x: int, y0: int, y1: int, color: Rgb) -> None:
        for y in range(min(y0, y1), max(y0, y1) + 1):
            self.set(x, y, color)

    def dashed_hline(self, x0: int, x1: int, y: int, color: Rgb) -> None:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            if (x - x0) % 10 < 6:
                self.set(x, y, color)
                self.set(x, y + 1, color)

    def dot(self, x: int, y: int, color: Rgb) -> None:
        for dx in (-1, 0, 1, 2):
            for dy in (-1, 0, 1, 2):
                self.set(x + dx, y + dy, color)

    def thick_line(self, x0: int, y0: int, x1: int, y1: int, color: Rgb) -> None:
        """太さ 2 の線（Bresenham の各点に 2×2 を置く）。"""
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            self.set(x0, y0, color)
            self.set(x0 + 1, y0, color)
            self.set(x0, y0 + 1, color)
            self.set(x0 + 1, y0 + 1, color)
            if x0 == x1 and y0 == y1:
                return
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += sx
            if doubled <= dx:
                error += dx
                y0 += sy

    def text(self, x: int, y: int, text: str, color: Rgb) -> None:
        for index, char in enumerate(text):
            glyph = _GLYPHS.get(char, _GLYPHS["?"])
            origin = x + index * CHAR_ADVANCE
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit != "#":
                        continue
                    for sy in range(SCALE):
                        for sx in range(SCALE):
                            self.set(origin + col * SCALE + sx, y + row * SCALE + sy, color)

    def to_png(self) -> bytes:
        stride = self.width * 3
        raw = b"".join(
            b"\x00" + bytes(self._pixels[row * stride : (row + 1) * stride])
            for row in range(self.height)
        )
        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b"")
        )


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


# ---------------------------------------------------------------------------- 字形

_FONT = {
    " ": "..... ..... ..... ..... ..... ..... .....",
    "?": ".###. #...# ....# ...#. ..#.. ..... ..#..",
    ".": "..... ..... ..... ..... ..... .##.. .##..",
    ",": "..... ..... ..... ..... .##.. ..#.. .#...",
    "_": "..... ..... ..... ..... ..... ..... #####",
    "-": "..... ..... ..... ##### ..... ..... .....",
    "+": "..... ..#.. ..#.. ##### ..#.. ..#.. .....",
    "=": "..... ..... ##### ..... ##### ..... .....",
    ":": "..... .##.. .##.. ..... .##.. .##.. .....",
    "/": "..... ....# ...#. ..#.. .#... #.... .....",
    "%": "##... ##..# ...#. ..#.. .#... #..## ...##",
    "[": ".###. .#... .#... .#... .#... .#... .###.",
    "]": ".###. ...#. ...#. ...#. ...#. ...#. .###.",
    "(": "...#. ..#.. .#... .#... .#... ..#.. ...#.",
    ")": ".#... ..#.. ...#. ...#. ...#. ..#.. .#...",
    "0": ".###. #...# #..## #.#.# ##..# #...# .###.",
    "1": "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",
    "2": ".###. #...# ....# ...#. ..#.. .#... #####",
    "3": "##### ...#. ..#.. ...#. ....# #...# .###.",
    "4": "...#. ..##. .#.#. #..#. ##### ...#. ...#.",
    "5": "##### #.... ####. ....# ....# #...# .###.",
    "6": "..##. .#... #.... ####. #...# #...# .###.",
    "7": "##### ....# ...#. ..#.. .#... .#... .#...",
    "8": ".###. #...# #...# .###. #...# #...# .###.",
    "9": ".###. #...# #...# .#### ....# ...#. .##..",
    "A": ".###. #...# #...# ##### #...# #...# #...#",
    "B": "####. #...# #...# ####. #...# #...# ####.",
    "C": ".###. #...# #.... #.... #.... #...# .###.",
    "D": "###.. #..#. #...# #...# #...# #..#. ###..",
    "E": "##### #.... #.... ####. #.... #.... #####",
    "F": "##### #.... #.... ####. #.... #.... #....",
    "G": ".###. #...# #.... #.### #...# #...# .####",
    "H": "#...# #...# #...# ##### #...# #...# #...#",
    "I": ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    "J": "..### ...#. ...#. ...#. ...#. #..#. .##..",
    "K": "#...# #..#. #.#.. ##... #.#.. #..#. #...#",
    "L": "#.... #.... #.... #.... #.... #.... #####",
    "M": "#...# ##.## #.#.# #.#.# #...# #...# #...#",
    "N": "#...# #...# ##..# #.#.# #..## #...# #...#",
    "O": ".###. #...# #...# #...# #...# #...# .###.",
    "P": "####. #...# #...# ####. #.... #.... #....",
    "Q": ".###. #...# #...# #...# #.#.# #..#. .##.#",
    "R": "####. #...# #...# ####. #.#.. #..#. #...#",
    "S": ".#### #.... #.... .###. ....# ....# ####.",
    "T": "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    "U": "#...# #...# #...# #...# #...# #...# .###.",
    "V": "#...# #...# #...# #...# #...# .#.#. ..#..",
    "W": "#...# #...# #...# #.#.# #.#.# #.#.# .#.#.",
    "X": "#...# #...# .#.#. ..#.. .#.#. #...# #...#",
    "Y": "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..",
    "Z": "##### ....# ...#. ..#.. .#... #.... #####",
    "a": "..... ..... .###. ....# .#### #...# .####",
    "b": "#.... #.... #.##. ##..# #...# #...# ####.",
    "c": "..... ..... .###. #.... #.... #...# .###.",
    "d": "....# ....# .##.# #..## #...# #...# .####",
    "e": "..... ..... .###. #...# ##### #.... .###.",
    "f": "..##. .#..# .#... ###.. .#... .#... .#...",
    "g": "..... ..... .#### #...# #...# #...# .#### ....# .###.",
    "h": "#.... #.... #.##. ##..# #...# #...# #...#",
    "i": "..#.. ..... .##.. ..#.. ..#.. ..#.. .###.",
    "j": "...#. ..... ..##. ...#. ...#. ...#. ...#. #..#. .##..",
    "k": "#.... #.... #..#. #.#.. ##... #.#.. #..#.",
    "l": ".##.. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    "m": "..... ..... ##.#. #.#.# #.#.# #...# #...#",
    "n": "..... ..... #.##. ##..# #...# #...# #...#",
    "o": "..... ..... .###. #...# #...# #...# .###.",
    "p": "..... ..... ####. #...# #...# #...# ####. #.... #....",
    "q": "..... ..... .#### #...# #...# #...# .#### ....# ....#",
    "r": "..... ..... #.##. ##..# #.... #.... #....",
    "s": "..... ..... .###. #.... .###. ....# ####.",
    "t": ".#... .#... ###.. .#... .#... .#..# ..##.",
    "u": "..... ..... #...# #...# #...# #..## .##.#",
    "v": "..... ..... #...# #...# #...# .#.#. ..#..",
    "w": "..... ..... #...# #...# #.#.# #.#.# .#.#.",
    "x": "..... ..... #...# .#.#. ..#.. .#.#. #...#",
    "y": "..... ..... #...# #...# #...# #...# .#### ....# .###.",
    "z": "..... ..... ##### ...#. ..#.. .#... #####",
}
"""5×7 の字形。行を空白で区切り、`#` が点。手で書いた字形で、外部のフォントに由来しない。

`g` `j` `p` `q` `y` だけは下へ 2 行はみ出す 9 行（数字の `9` と見分けるため）。
"""

_GLYPHS: dict[str, tuple[str, ...]] = {char: tuple(rows.split()) for char, rows in _FONT.items()}
