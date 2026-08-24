"""時刻ソースの注入（#42）。

**時計は1つ選び、取り込み・保存・ルールエンジンが同じものを参照する。**
最後のテストがその規約を機械的に守らせる部分で、
`time.time()` を1箇所書き足しただけで落ちる。
"""

import ast
from pathlib import Path

import pytest

from coldaisle.clock import Clock, SimulatedClock, WallClock

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"

WALL_CLOCK_NAMES = frozenset(
    {
        "time",
        "time_ns",
        "gmtime",
        "localtime",
        "clock_gettime",
        "clock_gettime_ns",
        "now",
        "utcnow",
        "today",
    }
)
"""現在時刻を作る呼び出しの名前。`clock.py` の外に置かない。

**モジュール名ではなく呼ばれた名前で照合する。** `time.time` と `datetime.now` の
2つだけを挙げると、`time.time_ns()` や `import time as t` や
`from time import time` で素通りする。名前で見れば別名 import も
`from` 形式も同じ網に掛かる。

`time.monotonic()` / `time.perf_counter()` / `time.sleep()` は**経過時間の計測と待機**
であり、タイムスタンプを作らないので対象にしない（`db.py` の WAL 再試行が使う）。
`datetime.fromtimestamp()` も、与えられた時刻を変換するだけなので対象にしない
（API の ISO8601 整形で使う）。
"""


def wall_clock_calls(tree: ast.AST) -> list[str]:
    """構文木から現在時刻の直接取得を拾う。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        name = (
            called.attr
            if isinstance(called, ast.Attribute)
            else called.id
            if isinstance(called, ast.Name)
            else None
        )
        if name in WALL_CLOCK_NAMES:
            found.append(f"{node.lineno}: {name}()")
    return found


def test_wall_clock_returns_unix_milliseconds():
    now = WallClock().now_ms()
    assert now > 1_700_000_000_000, "秒を返していないか"
    assert now < 100_000_000_000_000, "マイクロ秒を返していないか"


def test_simulated_clock_holds_its_value():
    clock = SimulatedClock(1_000)
    assert clock.now_ms() == 1_000
    assert clock.now_ms() == 1_000, "読むだけでは進まない"


def test_simulated_clock_advances():
    clock = SimulatedClock(1_000)
    clock.advance_to_ms(3_500)
    assert clock.now_ms() == 3_500


def test_simulated_clock_refuses_to_rewind():
    """ホスト受信時刻は戻らない。戻ると `(metric, ts_ms)` が既存行と衝突する。

    デバイスの再起動で巻き戻るのは `up` だけ（決定記録 0002 §2.3）。
    """
    clock = SimulatedClock(1_000)
    with pytest.raises(ValueError, match="巻き戻さない"):
        clock.advance_to_ms(999)


def test_simulated_clock_refuses_a_negative_start():
    with pytest.raises(ValueError, match="負"):
        SimulatedClock(-1)


@pytest.mark.parametrize("implementation", [WallClock(), SimulatedClock(0)])
def test_implementations_satisfy_the_protocol(implementation):
    assert isinstance(implementation, Clock)


def test_no_direct_wall_clock_calls_outside_the_clock_module():
    """`clock.py` 以外で現在時刻を作らない（#42 受入基準）。

    直接呼ぶと、そこだけ実時計で動く。圧縮再生では
    「取り込みはシナリオ時間、`stale` 判定は実時計」という混在が静かに成立し、
    継続時間ベースのルールが評価されなくなる。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "clock.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path.name}:{found}" for found in wall_clock_calls(tree)]
    assert not offenders, f"時刻の直接取得: {offenders}。coldaisle.clock を使う"


@pytest.mark.parametrize(
    "snippet",
    [
        "import time\ntime.time()\n",
        "import time\ntime.time_ns()\n",
        "import time as t\nt.time()\n",
        "from time import time\ntime()\n",
        "from datetime import datetime\ndatetime.now()\n",
        "import datetime\ndatetime.datetime.utcnow()\n",
        "from datetime import date\ndate.today()\n",
    ],
)
def test_the_detector_catches_every_spelling(snippet):
    """検査そのものの検査。**素通しの検査は緑のまま何も守らない。**

    綴りを2つだけ見る実装だと、この一覧の大半をすり抜ける。
    """
    assert wall_clock_calls(ast.parse(snippet))


@pytest.mark.parametrize(
    "snippet",
    [
        "import time\ntime.monotonic()\n",  # 経過時間の計測
        "import time\ntime.perf_counter()\n",
        "import time\ntime.sleep(1)\n",
        "from datetime import datetime\ndatetime.fromtimestamp(0)\n",  # 与えた時刻の変換
        "clock.now_ms()\n",
    ],
)
def test_the_detector_allows_elapsed_time_and_conversion(snippet):
    assert not wall_clock_calls(ast.parse(snippet))
