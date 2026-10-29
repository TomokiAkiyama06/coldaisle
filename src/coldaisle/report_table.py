"""日次レポートの測定値の表を PNG に描く（#380 / 決定記録 0171 §2.3）。

**使うのは `coldaisle-report` だけ。** Pillow は関数の中で読む。取り込み・API・fand・Learned の
プロセスには画像ライブラリを載せない（0147 §2.5）。このモジュールを import しても
Pillow は読まれない。

**決定論的に描く。** 同じ行・同じフォント・同じ Pillow から同じ bytes になる
（時刻もランダムも使わない）。
LLM は関わらない（ルール 1・8）。

描けないとき（Pillow が無い・フォントのファイルが無い・描画の失敗）は `None` を返す。
呼び出し側が表を Markdown の文で送る。**画像の失敗で日報を止めない。**
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from coldaisle import logs

if TYPE_CHECKING:  # 型だけ。実行時の import は描くときに限る
    from PIL.ImageFont import FreeTypeFont

LOGGER = logging.getLogger("coldaisle.report")

# 色は描き方の一部であって運用の閾値ではない（グラフの `png_chart` と同じく、コードに置く）
_BACKGROUND = (255, 255, 255)
_HEADER_FILL = (226, 232, 240)
_STRIPE_FILL = (245, 247, 250)
_RULE = (203, 213, 225)
_TEXT = (30, 41, 59)
_MUTED = (148, 163, 184)
"""値の無い行（欠測 100 % など）の文字の色。"""


@dataclass(frozen=True)
class TableRow:
    """表の 1 行。`cells` の 1 つ目は名前（左寄せ）、残りは数（右寄せ）。"""

    cells: tuple[str, ...]
    muted: bool = False
    """値の無い行。薄い色の文字で描く。"""


def render_table_png(
    header: Sequence[str],
    rows: Sequence[TableRow],
    *,
    font_path: Path,
    font_size: int,
) -> bytes | None:
    """表を PNG の bytes にする。**描けなければ `None`**（理由はログに残す）。"""
    if not rows:
        return None
    try:
        from PIL import ImageFont
    except ImportError:
        LOGGER.warning("Pillow が無いので、表を文で送る")
        return None
    try:
        font = ImageFont.truetype(str(font_path), font_size)
    except OSError:
        LOGGER.warning(
            "表のフォントが読めないので、表を文で送る",
            extra={logs.FIELDS_KEY: {"font": str(font_path)}},
        )
        return None
    try:
        return _draw(font, tuple(header), rows, font_size)
    except Exception:
        # 画像は添え物。失敗は記録して、表を文で送る（0171 §2.3）
        LOGGER.warning("表を画像に描けなかったので、表を文で送る", exc_info=True)
        return None


def _draw(
    font: FreeTypeFont, header: tuple[str, ...], rows: Sequence[TableRow], font_size: int
) -> bytes:
    from PIL import Image, ImageDraw

    columns = len(header)
    if any(len(row.cells) != columns for row in rows):
        raise ValueError("表の列の数が揃っていない")
    pad_x = font_size // 2
    pad_y = font_size // 3
    widths = [
        max(font.getlength(text) for text in [header[i], *(row.cells[i] for row in rows)])
        for i in range(columns)
    ]
    col_widths = [int(width) + 2 * pad_x + 1 for width in widths]
    ascent, descent = font.getmetrics()
    row_height = ascent + descent + 2 * pad_y
    width = sum(col_widths) + 1
    height = row_height * (len(rows) + 1) + 1

    image = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, row_height), fill=_HEADER_FILL)
    for index in range(len(rows)):
        if index % 2 == 1:
            top = row_height * (index + 1)
            draw.rectangle((0, top, width - 1, top + row_height), fill=_STRIPE_FILL)

    def put(cells: tuple[str, ...], top: int, color: tuple[int, int, int]) -> None:
        left = 0
        for i, text in enumerate(cells):
            # 名前は左寄せ、数は右寄せ（桁が揃う）
            right = left + col_widths[i] - pad_x - int(font.getlength(text))
            x = left + pad_x if i == 0 else right
            draw.text((x, top + pad_y), text, font=font, fill=color)
            left += col_widths[i]

    put(header, 0, _TEXT)
    for index, row in enumerate(rows):
        put(row.cells, row_height * (index + 1), _MUTED if row.muted else _TEXT)

    # 横の罫線は見出しの下と外枠だけ。行は塗りで分ける
    draw.line((0, row_height, width - 1, row_height), fill=_RULE)
    draw.rectangle((0, 0, width - 1, height - 1), outline=_RULE)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
