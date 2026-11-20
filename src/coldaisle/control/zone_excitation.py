"""普段の運転の Baseline に、1 つの zone だけの小さな上乗せ（揺らぎ）を挟む部品。

#84 / 決定記録 0185（位置と温度の条件は 0186）。

ここにあるのは、予定（``on`` / ``off`` の区間と zone の選択）と、時間帯・温度の条件の判定と、
requested への上乗せの計算だけである。loop の中の位置（Gate の後・Reactive Guard の前）と、
Gate が Baseline を選んだか・AUTO か・Safety と Guard の状態の判定は ``ControlLoop`` が持つ
（0186 §2.1）。

- **上げるだけ。** どの zone も揺らぎの前の requested を下回らない。破れたら例外にする
- **1 度に 1 つの zone だけ。** ほかの zone の requested は同じ値のまま返す
- **ML に依存しない。** 予定は ``seed`` と fand の ``run_id`` と単調時計だけで決まる（ルール 3）。
  同じ ``seed``・``run_id``・条件の列で、同じ予定になる（0185 §2.2）
- 合成（Guard / Critical Safety）の上下限は持たない。揺らぎの後に、いまと同じく掛かる
  （AGENTS.md ルール 2・5）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from hashlib import sha256

from coldaisle.control.config import ZoneExcitationConfig
from coldaisle.control.schema import (
    PerZone,
    Zone,
    ZoneExcitationMode,
    ZoneExcitationRecord,
    ZoneExcitationSkipReason,
    ZoneExcitationState,
    ZoneRequest,
)


class ZoneExcitationError(RuntimeError):
    """揺らぎの出力が不変条件（下げない・1 つの zone だけ）を破った。**不具合として扱う**。"""


@dataclass(frozen=True, slots=True)
class ZoneExcitationTemperatures:
    """温度の条件（0186 §2.2）に使う、この tick の snapshot の値。使えなければ None。"""

    cpu_c: float | None
    gpu_c: float | None


@dataclass(frozen=True, slots=True)
class ZoneExcitationResult:
    """1 tick の揺らぎの結果。``requested`` は Guard と Critical Safety へ渡す値。"""

    requested: PerZone[ZoneRequest]
    record: ZoneExcitationRecord


@dataclass(slots=True)
class _Episode:
    index: int
    zone: Zone
    ends_mono_ms: int


class ZoneExcitation:
    """揺らぎの予定を持ち、tick ごとに requested へ上乗せを掛ける（0185 §2.2）。

    予定は ``off`` の区間から始まる。``off`` の区間が終わった最初の tick で区間（``on``）を始め、
    zone を選ぶ。区間の中で条件が外れた tick は区間をそこで打ち切り（``skipped``）、``off`` へ戻る。
    条件が戻っても打ち切った区間は再開しない。
    """

    def __init__(
        self,
        config: ZoneExcitationConfig,
        *,
        run_id: str,
        local_timezone: tzinfo | None = None,
    ) -> None:
        """``local_timezone`` は ``allowed_hours`` を読む時刻の帯（既定は fand のローカル時刻）。

        試験だけが明示する。``run_id`` は乱数の列を run ごとに変えるために使う（同じ ``seed`` の
        別の起動で同じ予定を繰り返さない）。
        """
        self._config = config
        self._run_id = run_id
        self._timezone = local_timezone
        self._draws = 0
        self._episodes = 0
        self._zones = tuple(zone for zone in Zone if zone in config.zones)
        self._episode: _Episode | None = None
        self._off_ends_mono_ms: int | None = None

    @property
    def mode(self) -> ZoneExcitationMode:
        """設定の ``mode``。"""
        return self._config.mode

    def step(
        self,
        *,
        now_mono_ms: int,
        ts_ms: int,
        requested: PerZone[ZoneRequest],
        blocked: ZoneExcitationSkipReason | None,
        temperatures: ZoneExcitationTemperatures,
    ) -> ZoneExcitationResult:
        """この tick の揺らぎを決め、requested に掛ける。

        ``blocked`` は loop が判定した構造の条件（AUTO か・Gate が Baseline を選んだか・Safety・
        fault・Guard。0185 §2.3 / 0186 §2.1）のうち、最初に外れたもの。外れていなければ None。
        時間帯と温度の条件はここで判定する。
        """
        if self._config.mode is ZoneExcitationMode.OFF:
            return ZoneExcitationResult(requested=requested, record=ZoneExcitationRecord.off())
        if self._off_ends_mono_ms is None and self._episode is None:
            self._off_ends_mono_ms = now_mono_ms + self._off_length_ms()
        episode = self._episode
        if episode is not None and now_mono_ms >= episode.ends_mono_ms:
            # 区間を終えた tick は足さない（区間の長さは on_ms ちょうど）
            self._end_episode(now_mono_ms)
            episode = None
        if episode is None:
            assert self._off_ends_mono_ms is not None
            if now_mono_ms < self._off_ends_mono_ms:
                return self._idle(requested)
            episode = self._start_episode(now_mono_ms)
        reason = blocked or self._unmet_condition(ts_ms, temperatures)
        if reason is not None:
            self._end_episode(now_mono_ms)
            return ZoneExcitationResult(
                requested=requested,
                record=ZoneExcitationRecord(
                    mode=self._config.mode,
                    state=ZoneExcitationState.SKIPPED,
                    episode=episode.index,
                    zone=episode.zone,
                    skip_reason=reason,
                ),
            )
        return self._apply(requested, episode)

    def _idle(self, requested: PerZone[ZoneRequest]) -> ZoneExcitationResult:
        return ZoneExcitationResult(
            requested=requested,
            record=ZoneExcitationRecord(mode=self._config.mode, state=ZoneExcitationState.IDLE),
        )

    def _apply(self, requested: PerZone[ZoneRequest], episode: _Episode) -> ZoneExcitationResult:
        before = requested.get(episode.zone)
        after_demand = min(1.0, before.demand + self._config.amplitude.value)
        if after_demand < before.demand:
            raise ZoneExcitationError("揺らぎが requested を下げた（0185 §2.1）")
        raised = ZoneRequest(demand=after_demand, reason=before.reason)
        output = PerZone[ZoneRequest](
            front=raised if episode.zone is Zone.FRONT else requested.front,
            rear=raised if episode.zone is Zone.REAR else requested.rear,
            top=raised if episode.zone is Zone.TOP else requested.top,
        )
        for zone in Zone:
            if zone is not episode.zone and output.get(zone) != requested.get(zone):
                raise ZoneExcitationError("揺らぎが選んだ zone の外を変えた（0185 §2.1）")
        return ZoneExcitationResult(
            requested=output,
            record=ZoneExcitationRecord(
                mode=self._config.mode,
                state=ZoneExcitationState.ON,
                episode=episode.index,
                zone=episode.zone,
                before=before.demand,
                after=after_demand,
            ),
        )

    def _unmet_condition(
        self, ts_ms: int, temperatures: ZoneExcitationTemperatures
    ) -> ZoneExcitationSkipReason | None:
        """時間帯と温度の条件（0185 §2.3 / 0186 §2.2）。未知を「低い」と読まない（ルール 4）。"""
        start, end = self._config.allowed_hours.value
        hour = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).astimezone(self._timezone).hour
        if not start <= hour < end:
            return ZoneExcitationSkipReason.OUTSIDE_HOURS
        if temperatures.cpu_c is None or temperatures.gpu_c is None:
            return ZoneExcitationSkipReason.TEMPERATURE_UNAVAILABLE
        if temperatures.cpu_c >= self._config.cpu_max_c.value:
            return ZoneExcitationSkipReason.CPU_TEMPERATURE
        if temperatures.gpu_c >= self._config.gpu_max_c.value:
            return ZoneExcitationSkipReason.GPU_TEMPERATURE
        return None

    def _start_episode(self, now_mono_ms: int) -> _Episode:
        zone = self._zones[int(self._draw() * len(self._zones))]
        episode = _Episode(
            index=self._episodes, zone=zone, ends_mono_ms=now_mono_ms + self._config.on_ms.value
        )
        self._episodes += 1
        self._episode = episode
        self._off_ends_mono_ms = None
        return episode

    def _end_episode(self, now_mono_ms: int) -> None:
        self._episode = None
        self._off_ends_mono_ms = now_mono_ms + self._off_length_ms()

    def _off_length_ms(self) -> int:
        low = self._config.off_min_ms.value
        high = self._config.off_max_ms.value
        return low + int(self._draw() * (high - low + 1))

    def _draw(self) -> float:
        """``[0, 1)`` の決定論的な値。SHA-256 の計数器で作り、乱数の実装の版に依らない。"""
        material = f"{self._config.seed}|{self._run_id}|{self._draws}".encode()
        self._draws += 1
        return int.from_bytes(sha256(material).digest()[:8], "big") / 2**64
