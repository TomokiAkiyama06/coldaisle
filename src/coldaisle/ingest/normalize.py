"""正規化: デバイス出力 → 保存できるサンプル（L0）。#8

デバイスは短いチャネル名を送る。`air.` を付けるのはホスト側の関心事であり、
その対応付けはここが持つ（決定記録 0003）。

ホスト受信時刻をここで付ける（決定 D-05）。デバイス時刻は信用しない。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from coldaisle.clock import Clock
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.protocol import RawSample
from coldaisle.store import Quality, QualityRules, Reading, Sample, classify

CHANNEL_TO_METRIC = {
    "room_temp": "air.room",
    "room_humidity": "air.room_humidity",
    "front_intake": "air.front_intake",
    "gpu_intake": "air.gpu_intake",
    "gpu_exhaust": "air.gpu_exhaust",
    "top_exhaust": "air.top_exhaust",
    "rear_exhaust": "air.rear_exhaust",
}
"""決定記録 0003 の対応表。ここに無いチャネルは**捨てる**（0003 §2.7）。

未知のフィールドで取り込みを止めない。ファームが1つ足しただけで
全サンプルが落ちるのが最悪の壊れ方であり、足りないチャネルは
`SENSOR_MISSING`（FR-402）として別途検出される。
"""

DROPPED_SAMPLES_METRIC = "sys.dropped_samples"
"""`seq` が飛んだときだけ書く。値は飛んだ件数（FR-105）。

毎サンプル書かないのは、行数を14%増やす価値が無いため。**最新値ではなく
期間の合計で読む**メトリクスである（`latest()` に出る値は直近の欠損の大きさ）。
"""

DEVICE_RESTART_METRIC = "sys.device_restarts"
"""`up` の巻き戻りを検出したときだけ 1 を書く（FR-106）。

イベント表（#36）はまだ無い。ログだけに残すと後から集計できないため、
当面は `readings` に置く。#36 で表ができたら移す。
"""

HUMIDITY_SUFFIX = "_humidity"


@dataclass(frozen=True)
class Normalized:
    """1サンプルの正規化結果。異常の検出はサンプルとは別に返す。

    デーモンはこれを見てログと統計を出す。保存するのは `sample` だけ。
    """

    sample: Sample
    dropped_samples: int
    """`seq` の飛び（FR-105）。0 なら連続している。"""
    device_restarted: bool
    """`up` の巻き戻り（FR-106）。"""
    out_of_order: bool
    """`seq` が進んでいない。再送・順序入れ替わりの疑い。"""
    unknown_channels: tuple[str, ...]
    """対応表に無いチャネル。捨てた事実を呼び出し側へ伝える。"""


class Normalizer:
    """デバイス1台ぶんの正規化。`seq` と `up` の連続性をここで見る。

    状態を持つのは1台ぶんの前回値だけ。複数台を扱う場合は
    インスタンスを分ける（v1 は1台前提。要件 N-04）。
    """

    def __init__(self, *, rules: QualityRules, calibration: Calibration, clock: Clock) -> None:
        self._rules = rules
        self._calibration = calibration
        self._clock = clock
        self._last_seq: int | None = None
        self._last_up: int | None = None

    @property
    def clock(self) -> Clock:
        """ホスト受信時刻を決める時計。合成の起点が同じものを配れたかの検査用（#42）。"""
        return self._clock

    def normalize(self, raw: RawSample) -> Normalized:
        ts_ms = self._clock.now_ms()
        # `up` の巻き戻りが再起動の定義（FR-106）。稼働時間は戻らないため、
        # 戻っていれば電源が入り直したということ。届くのが遅れた古いサンプルも
        # 同じ形に見えるが、区別する手立ては v1 のスキーマには無い
        restarted = self._last_up is not None and raw.up < self._last_up
        dropped = 0
        out_of_order = False
        if restarted:
            # 再起動すると seq は 0 から振り直される。飛びとして数えない
            pass
        elif self._last_seq is not None:
            if raw.seq > self._last_seq + 1:
                dropped = raw.seq - self._last_seq - 1
            elif raw.seq <= self._last_seq:
                out_of_order = True
        if not out_of_order:
            # **逆行したサンプルで基準を戻さない。** 戻すと次の正常なサンプルが
            # 大量の取りこぼしに見える（`10, 5, 11` で 11 が5件飛びと数えられる）。
            # FR-105 が数えるのは失われた件数なので、基準は到達済みの最大値で持つ
            self._last_seq = raw.seq
            self._last_up = raw.up

        readings: list[Reading] = []
        unknown: list[str] = []
        for channel, value in raw.channels.items():
            metric = CHANNEL_TO_METRIC.get(channel)
            if metric is None:
                unknown.append(channel)
                continue
            readings.append(self._reading(metric, channel, value))

        if dropped:
            readings.append(
                Reading(metric=DROPPED_SAMPLES_METRIC, value=float(dropped), quality=Quality.OK)
            )
        if restarted:
            readings.append(Reading(metric=DEVICE_RESTART_METRIC, value=1.0, quality=Quality.OK))

        return Normalized(
            sample=Sample(ts_ms=ts_ms, readings=tuple(readings), seq=raw.seq, up_ms=raw.up),
            dropped_samples=dropped,
            device_restarted=restarted,
            out_of_order=out_of_order,
            unknown_channels=tuple(sorted(unknown)),
        )

    def _reading(self, metric: str, channel: str, value: float | None) -> Reading:
        """品質を先に決め、`ok` のときだけ較正を当てる。

        **順序が逆だと番兵値を取り逃す。** `-127.00 + 0.3` は `-126.7` になり、
        ちょうど `85.00` も `85.3` になって、どちらも判定をすり抜ける
        （要件 §5.3 / spec-review C-02）。較正は「正常な測定値のずれ」を直すもので、
        故障を示す値に足すものではない。
        """
        quality = classify(metric, value, self._rules)
        if value is not None and not math.isfinite(value):
            # 非有限値は保存しない（決定記録 0003 §2.8）。**行ごと捨てるのはパーサの役目**
            # （FR-103 / #12）で、ここまで来たものは値だけ落として品質を残す。
            # 1チャネルの異常で他6チャネルまで失うのは割に合わない
            return Reading(metric=metric, value=None, quality=quality)
        if quality is not Quality.OK or value is None:
            return Reading(metric=metric, value=value, quality=quality)
        if metric.endswith(HUMIDITY_SUFFIX):
            # 較正値の単位は℃。%RH には当てない（#13 で扱うのは温度チャネルのみ）
            return Reading(metric=metric, value=value, quality=quality)
        return Reading(
            metric=metric, value=value + self._calibration.offset_for(channel), quality=quality
        )
