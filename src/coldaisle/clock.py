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

import re
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
"""起動ごとの識別子（Linux）。決定記録 0066 §2.2 の boot id の出どころ。"""

BOOT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
"""boot id の形（UUID の小文字 36 文字。0066 §2.2）。"""

SIMULATED_BOOT_ID = "00000000-0000-4000-8000-000000000000"
"""`SimulatedClock` の既定の boot id。実機の値ではないと一目で分かる仮の値（ルール 10）。"""


class BootClockUnavailableError(RuntimeError):
    """boot id または `CLOCK_BOOTTIME` を読めない（Linux 以外・`/proc` が無いなど）。"""


def validate_boot_id(value: str) -> str:
    """boot id の形を確かめて返す。形が違えば `ValueError`。"""
    if not BOOT_ID_PATTERN.match(value):
        raise ValueError("boot id は UUID の小文字 36 文字にする")
    return value


@runtime_checkable
class Clock(Protocol):
    """ホスト受信時刻の供給元（決定 D-05）。"""

    def now_ms(self) -> int:
        """現在時刻（Unix ミリ秒、UTC）。"""
        ...


@runtime_checkable
class BootClock(Protocol):
    """boot id と `CLOCK_BOOTTIME`（決定記録 0066 §2.2）。

    **経過を過小に見積もらない時計。** `CLOCK_MONOTONIC` は suspend 中に進まず、
    壁時計は時刻合わせで戻る。Workload Hint の期限と起動時の取り込みの経過は、
    同じ boot（boot id が一致）の中でだけ `CLOCK_BOOTTIME` の差で数える。

    制御ループの他の判定（tick の deadline・stale・hold・stall）は `MonotonicClock` のまま
    （0028 の契約）。この時計を混ぜない。
    """

    def boot_id(self) -> str:
        """いまの boot の識別子（UUID の小文字 36 文字）。"""
        ...

    def boottime_ns(self) -> int:
        """`CLOCK_BOOTTIME` のナノ秒（0 以上。起点はその boot の起動）。"""
        ...


class WallClock:
    """実時計。`serial` ソースと本番運用で使う。

    `BootClock` も実時計で満たす（boot id は `/proc`、経過は `CLOCK_BOOTTIME`）。
    Linux 以外では `BootClockUnavailableError` を出す。
    """

    def __init__(self, *, boot_id_path: Path = BOOT_ID_PATH) -> None:
        self._boot_id_path = boot_id_path
        # boot id はプロセスの生存中に変わらない。1回読めたら持つ
        self._boot_id: str | None = None

    def now_ms(self) -> int:
        # time_ns から作る。time() の float は 2026 年の Unix 秒で
        # ミリ秒未満の桁が落ちる可能性があり、丸めの向きが環境で変わる
        return time.time_ns() // 1_000_000

    def boot_id(self) -> str:
        if self._boot_id is None:
            try:
                text = self._boot_id_path.read_text(encoding="ascii").strip()
            except (OSError, UnicodeDecodeError) as exc:
                raise BootClockUnavailableError("boot id を読めない") from exc
            try:
                self._boot_id = validate_boot_id(text)
            except ValueError as exc:
                raise BootClockUnavailableError("boot id の形が不正") from exc
        return self._boot_id

    def boottime_ns(self) -> int:
        clock_id = getattr(time, "CLOCK_BOOTTIME", None)
        if clock_id is None:
            raise BootClockUnavailableError("CLOCK_BOOTTIME の無いプラットフォーム")
        try:
            return time.clock_gettime_ns(clock_id)
        except OSError as exc:
            raise BootClockUnavailableError("CLOCK_BOOTTIME を読めない") from exc


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

    def __init__(self, start_ms: int, *, boot_id: str = SIMULATED_BOOT_ID) -> None:
        if start_ms < 0:
            raise ValueError(f"開始時刻が負: {start_ms}")
        self._start_ms = start_ms
        self._now_ms = start_ms
        self._boot_id = validate_boot_id(boot_id)

    def now_ms(self) -> int:
        return self._now_ms

    def boot_id(self) -> str:
        """構築時に与えた boot id（既定は仮の値）。"""
        return self._boot_id

    def boottime_ns(self) -> int:
        """シナリオ時間での経過（開始時刻を boot の起点とみなす）。

        `advance_to_ms` が巻き戻さないので、この値も減らない（0066 §2.2）。
        """
        return (self._now_ms - self._start_ms) * 1_000_000

    def advance_to_ms(self, value: int) -> None:
        """指定時刻まで進める。**巻き戻さない。**

        `readings` の主キーは `(metric, ts_ms)` なので、ホスト時刻が戻ると
        既存の行と衝突する。デバイスの再起動で巻き戻るのは `up` だけであり、
        ホスト受信時刻は戻らない（決定記録 0002 §2.3）。
        """
        if value < self._now_ms:
            raise ValueError(f"時刻は巻き戻さない: {self._now_ms} -> {value}")
        self._now_ms = value


@runtime_checkable
class MonotonicClock(Protocol):
    """経過時間・締め切り・hold・stall の判定に使う単調時計（決定記録 0028 §2.6）。

    **壁時計と分ける。** `Clock` は時刻合わせで前後に飛ぶので、期限の判定に使うと
    時計が戻ったときに古い提案が有効なまま残り、stall や stale の timer が満了しない。
    tick は回り続けるので watchdog も介入しない。
    """

    def monotonic_ms(self) -> int:
        """起点を問わない単調増加のミリ秒。**絶対時刻としての意味を持たない。**"""
        ...


class SystemMonotonicClock:
    """OS の単調時計。制御デーモンの本番で使う。"""

    def monotonic_ms(self) -> int:
        # monotonic_ns から作る。float の monotonic() は長時間稼働で
        # ミリ秒未満の桁が落ち、丸めの向きが環境で変わる
        return time.monotonic_ns() // 1_000_000


class ManualMonotonicClock:
    """試験・Replay 用に、呼び出し側が明示的に進める単調時計。

    **巻き戻せない。** 巻き戻る時計を許すと、期限・hold・stall の検証が
    「戻せば満了しない」経路を持ってしまう。
    """

    def __init__(self, start_ms: int = 0) -> None:
        if start_ms < 0:
            raise ValueError(f"単調時計の開始値が負: {start_ms}")
        self._now_ms = start_ms

    def monotonic_ms(self) -> int:
        return self._now_ms

    def advance_ms(self, delta_ms: int) -> int:
        """`delta_ms` だけ進めて、進めたあとの値を返す。"""
        if delta_ms < 0:
            raise ValueError(f"単調時計は巻き戻さない: {delta_ms}")
        self._now_ms += delta_ms
        return self._now_ms
