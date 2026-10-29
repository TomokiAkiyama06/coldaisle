"""Shadow solver の子（spawn）へ渡す試験用の関数（#288 / 決定記録 0133 §2.5）。

spawn の子はこの module を import して関数を引くので、import の軽い module に置く。
"""

from __future__ import annotations

from typing import Any


def never_lower() -> bool:
    """``SCHED_IDLE`` に下げられない環境の再現（何もせずに False）。"""
    return False


def burn_at_sched_idle(stop: Any, ready: Any) -> None:
    """``SCHED_IDLE`` に下げてから、``stop`` が立つまで CPU を使い切る（実時間の試験の負荷）。

    下げられなければ何もせずに終わる（試験の側が ``ready`` の数で気づく）。
    """
    from coldaisle.learned_worker.shadow_solver import lower_to_sched_idle

    if not lower_to_sched_idle():
        return
    ready.release()
    value = 1
    while not stop.is_set():
        for _ in range(100_000):
            value = (value * 48_271) % 2_147_483_647
