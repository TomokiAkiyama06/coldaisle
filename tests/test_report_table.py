"""日次レポートの測定値の表の画像（#380 / 決定記録 0171 §2.3）。

要点:

- 同じ入力から同じ bytes（決定論的）
- フォントが無い・Pillow が無いときは `None`（呼び出し側が表を文で送る）
- 取り込み・API・fand・Learned のプロセスに Pillow を載せない（0147 §2.5）

描画の試験は日本語のフォント（無ければ DejaVu）があるときだけ走る。CI にフォントが無くても、
フォントが無いときの経路は必ず試す。
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from coldaisle.report import ReportConfig
from coldaisle.report_table import TableRow, render_table_png
from conftest import CONFIG_DIR

HEADER = ("メトリクス", "平均", "最高")
ROWS = [
    TableRow(("室温", "24.6 °C", "27.5 °C")),
    TableRow(("室内湿度", "52.0 %RH", "65.3 %RH")),
    TableRow(("GPU消費電力", "—", "—"), muted=True),
]

_CANDIDATES = (
    ReportConfig.from_yaml(CONFIG_DIR / "report.yaml").table_font,
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)
FONT = next((path for path in _CANDIDATES if path.exists()), None)
needs_font = pytest.mark.skipif(FONT is None, reason="描画に使えるフォントが無い")


def render(rows=ROWS) -> bytes | None:
    assert FONT is not None
    return render_table_png(HEADER, rows, font_path=FONT, font_size=20)


@needs_font
def test_same_input_gives_the_same_png():
    first = render()
    assert first is not None
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert render() == first


@needs_font
def test_rows_are_striped_and_rows_without_values_are_muted():
    from PIL import Image

    png = render()
    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("RGB")
    rows = len(ROWS) + 1
    row_height = image.height // rows
    # 2 行目のデータ（index 1）は薄く塗る。1 行目は白のまま
    assert image.getpixel((2, row_height * 1 + row_height // 2)) == (255, 255, 255)
    assert image.getpixel((2, row_height * 2 + row_height // 2)) == (245, 247, 250)
    # 値の無い行の印を外すと画像が変わる（薄い文字で描いている）
    plain = [*ROWS[:2], TableRow(ROWS[2].cells, muted=False)]
    assert render(plain) != png


def test_missing_font_returns_none(tmp_path, caplog):
    assert (
        render_table_png(HEADER, ROWS, font_path=tmp_path / "no-such-font.ttc", font_size=20)
        is None
    )
    assert "表のフォントが読めない" in caplog.text


def test_without_pillow_returns_none(monkeypatch, tmp_path, caplog):
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert render_table_png(HEADER, ROWS, font_path=tmp_path / "f.ttc", font_size=20) is None
    assert "Pillow が無い" in caplog.text


def test_no_rows_returns_none(tmp_path):
    assert render_table_png(HEADER, [], font_path=tmp_path / "f.ttc", font_size=20) is None


def test_long_running_processes_do_not_load_pillow():
    """取り込み・API・fand・Learned と、日報のモジュールの import では Pillow を読まない。"""
    code = (
        "import sys\n"
        "import coldaisle.daemon, coldaisle.api.app, coldaisle.control_daemon\n"
        "import coldaisle.learned_worker.cli, coldaisle.report\n"
        "loaded = sorted(m for m in sys.modules if m == 'PIL' or m.startswith('PIL.'))\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
