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

FORBIDDEN_CALLS = {
    ("time", "time"),
    ("time", "time_ns"),
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("date", "today"),
}
"""現在時刻を作る呼び出し。`clock.py` の外に置かない。

`time.monotonic()` と `time.perf_counter()` は**経過時間の計測**であり、
タイムスタンプを作らないので対象にしない（`db.py` の WAL 再試行が使っている）。
"""


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
    """`time.time()` / `datetime.now()` を各所で直接呼ばない（#42 受入基準）。

    直接呼ぶと、そこだけ実時計で動く。圧縮再生では
    「取り込みはシナリオ時間、`stale` 判定は実時計」という混在が静かに成立し、
    継続時間ベースのルールが評価されなくなる。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "clock.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if isinstance(owner, ast.Name) and (owner.id, node.func.attr) in FORBIDDEN_CALLS:
                offenders.append(f"{path.name}:{node.lineno} {owner.id}.{node.func.attr}()")
    assert not offenders, f"時刻の直接取得: {offenders}。coldaisle.clock を使う"
