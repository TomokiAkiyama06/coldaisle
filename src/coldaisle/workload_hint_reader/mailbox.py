"""読み手のスレッドと loop の間の受け渡し口（決定記録 0133 §2.8）。

**どのメソッドも待たない。** 値は不変の object の参照の差し替えで置き、loop は最後に置かれた参照を
読むだけ。loop からの終端の遷移は `collections.deque` に積む（`append` / `popleft` はスレッド間で
安全で、lock を取らない）。
"""

from __future__ import annotations

import threading
from collections import deque

from coldaisle.control.supervisor.workload_hint import HintFeed, HintTransition


class HintMailbox:
    """`WorkloadHintMailbox`（loop が使う面）と、読み手が使う面を持つ受け渡し口。"""

    def __init__(self) -> None:
        self._feed: HintFeed | None = None
        self._reports: deque[HintTransition] = deque()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ loop が使う面

    def reader_alive(self) -> bool:
        """読み手のスレッドが生きているか。**lock を取らない。**"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def latest(self) -> HintFeed | None:
        """最後に置かれた値（参照を読むだけ）。"""
        return self._feed

    def report(self, transition: HintTransition) -> None:
        """loop が決めた終端の遷移を積む。**待たない。**"""
        self._reports.append(transition)

    # ------------------------------------------------------------ 読み手が使う面

    def bind_thread(self, thread: threading.Thread) -> None:
        """生存を確かめる対象のスレッド。"""
        self._thread = thread

    def publish(self, feed: HintFeed) -> None:
        """値を置く（参照を差し替える）。"""
        self._feed = feed

    def drain(self) -> list[HintTransition]:
        """積まれた遷移を古い順にすべて取り出す。"""
        taken: list[HintTransition] = []
        while True:
            try:
                taken.append(self._reports.popleft())
            except IndexError:
                return taken
