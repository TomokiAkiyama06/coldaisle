"""時刻ソース（レイヤ横断）。#42

**`time.time()` / `datetime.now()` を各所で直接呼ばない。**
時計は1つ選び、取り込み・保存・ルールエンジンが**同じものを参照する**。

これが必要なのは時間圧縮のため。`--speed 60` で流すと、デバイス時刻（`up`）は
60秒ぶん進むのにホストの実時刻は1秒しか進まない。ホスト受信時刻を採用する
決定（D-05）のもとでは、継続時間ベースのルール（FR-401〜409）と `stale` 判定が
**実時間で評価され、圧縮再生では発火しなくなる。**

`ingest`（L0）と `store`（L1）の両方が使うため、どちらの層にも置けない。
下位レイヤが上位レイヤを import しない規約（AGENTS.md）に従うと、
共有する土台はパッケージ直下に置くことになる。
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """ホスト受信時刻の供給元（決定 D-05）。"""

    def now_ms(self) -> int:
        """現在時刻（Unix ミリ秒、UTC）。"""
        ...


class WallClock:
    """実時計。`serial` ソースと本番運用で使う。"""

    def now_ms(self) -> int:
        # time_ns から作る。time() の float は 2026 年の Unix 秒で
        # ミリ秒未満の桁が落ちる可能性があり、丸めの向きが環境で変わる
        return time.time_ns() // 1_000_000


class SimulatedClock:
    """シナリオ時間で進む時計。`mock` / `replay` で使う。

    **進めるのはソースだけ。** 読み手（保存・ルールエンジン）は `now_ms()` しか
    呼ばない。進める側が複数あると、どれが時刻を決めたのか追えなくなる。

    **メモリ上のオブジェクトなので、プロセスをまたいで共有できない。**
    取り込みデーモンと API を別プロセスで起動する構成では、API 側に実時計を
    渡すと `--speed 60` のサンプルが実時刻より未来になり（`data_age_seconds`
    が負になる）、別の `SimulatedClock` を渡すと誰も進めないので止まったままになる。
    どちらを採るかは #8 で決める（#42 の Issue に選択肢を書いた）。
    """

    def __init__(self, start_ms: int) -> None:
        if start_ms < 0:
            raise ValueError(f"開始時刻が負: {start_ms}")
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance_to_ms(self, value: int) -> None:
        """指定時刻まで進める。**巻き戻さない。**

        `readings` の主キーは `(metric, ts_ms)` なので、ホスト時刻が戻ると
        既存の行と衝突する。デバイスの再起動で巻き戻るのは `up` だけであり、
        ホスト受信時刻は戻らない（決定記録 0002 §2.3）。
        """
        if value < self._now_ms:
            raise ValueError(f"時刻は巻き戻さない: {self._now_ms} -> {value}")
        self._now_ms = value
