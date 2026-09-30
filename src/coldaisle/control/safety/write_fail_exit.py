"""書き込みが続けて失敗したら fand を終える判定（決定記録 0080 §2.6）。#74

takeover の後、fand の中の `EMERGENCY` は Max を**求める**ことしかできない。書き込みそのものが
失敗していれば求めた Max は hwmon に届かず、それでも loop は tick を回すので heartbeat は止まらず、
`ExecStopPost`（root で Max を書く最後の防衛線）も走らない。ここはその穴を閉じる。

- いずれかの zone で、書き込みと読み戻しが**一度も成功しないまま** `hardware_write_fail_exit_ms`
  続いたら終わる。takeover の書き込み（最初の Max）の失敗も同じく数える
- 期間は zone ごとの**連続**で数え、その zone の書き込みと読み戻しが1回成功すれば数え直す
- 判定の入力は Backend の `write_ok` / `readback_ok`（decision trace と同じ値。0028 §2.2）だけで、
  ML にも demand の計算にも依存しない（AGENTS.md ルール3）

この module は**判定だけ**を持ち、終了や記録の扱いは合成の起点（`control_daemon.py`）が行う。
終わるときは §2.8 の返却をせず、引き継ぎ記録を消さない（BIOS へ戻す書き込みも同じ理由で
失敗しうるため）。
"""

from __future__ import annotations

from collections.abc import Mapping

from coldaisle.control.schema import Zone


class HardwareWriteFailureExitError(RuntimeError):
    """書き込みと読み戻しの失敗が `hardware_write_fail_exit_ms` 続いた（決定記録 0080 §2.6）。

    **捕まえて運転を続けない。** 合成の起点が終了コード 7 で終え、`ExecStopPost` に Max を任せる。
    """

    def __init__(
        self, *, zones: tuple[Zone, ...], failing_ms: dict[Zone, int], limit_ms: int
    ) -> None:
        self.zones = zones
        self.failing_ms = failing_ms
        self.limit_ms = limit_ms
        detail = ", ".join(f"{zone.value}={failing_ms[zone]}ms" for zone in zones)
        super().__init__(
            "Fan の書き込みと読み戻しが一度も成功しないまま "
            f"hardware_write_fail_exit_ms（{limit_ms}ms）を超えた: {detail}"
        )


class HardwareWriteFailureExit:
    """zone ごとに「書き込みと読み戻しが成功していない期間」を単調時計で数える。

    時刻は呼び出し側の単調時計（ミリ秒）で受け取る。壁時計を使うと、時刻合わせで戻ったときに
    期間が満了しない（0028 §2.6）。
    """

    __slots__ = ("_failing_since", "_limit_ms")

    def __init__(self, limit_ms: int) -> None:
        if limit_ms <= 0:
            raise ValueError("hardware_write_fail_exit_ms は正にする")
        self._limit_ms = limit_ms
        self._failing_since: dict[Zone, int] = {}

    @property
    def limit_ms(self) -> int:
        """終える期間（`safety.yaml` の `hardware_write_fail_exit_ms`）。"""
        return self._limit_ms

    def failing_since(self, zone: Zone) -> int | None:
        """その zone が失敗し始めた tick の開始時刻。成功している間は None。"""
        return self._failing_since.get(zone)

    def observe(
        self,
        confirmed: Mapping[Zone, bool] | None,
        *,
        tick_started_mono_ms: int,
        now_mono_ms: int,
    ) -> None:
        """1 tick の書き込みの結果を入れる。期間を超えたら `HardwareWriteFailureExitError`。

        ``confirmed`` は zone ごとの ``write_ok and readback_ok``。**None は Backend が例外で
        結果を返さなかった tick**で、全 zone の失敗として数える（書けたと言える根拠が無い）。
        zone が欠けていても失敗として数える（黙って成功扱いにしない）。

        失敗の始まりは、最初に失敗した **tick の開始時刻**に置く。書き込みを試みた時点から
        数えるほうが、終了が遅れる側へ倒れない。
        """
        if now_mono_ms < tick_started_mono_ms:
            raise ValueError("単調時計が tick の開始より前を指している")
        for zone in Zone:
            ok = confirmed is not None and confirmed.get(zone, False)
            if ok:
                self._failing_since.pop(zone, None)
            else:
                self._failing_since.setdefault(zone, tick_started_mono_ms)
        failing_ms = {zone: now_mono_ms - since for zone, since in self._failing_since.items()}
        expired = tuple(zone for zone in Zone if failing_ms.get(zone, -1) >= self._limit_ms)
        if expired:
            raise HardwareWriteFailureExitError(
                zones=expired, failing_ms=failing_ms, limit_ms=self._limit_ms
            )
