"""Workload Hint の認証領域の判定（決定記録 0162 §2.2 の判定 1〜5 と古さの検査）。

**1つの純粋な関数** :func:`judge_certified_domain` を、fand（自分が送り出した frame の区間）・
worker（実際の window）・試験が同じように呼ぶ（0162 §2.2「関数」）。入力は時刻順の frame ごとの
軸の値・quality・``source_ts_ms`` の列（:class:`DomainFrame`）と、``N`` の鍵・直前 Demand・
safety floor・Baseline・基準の ``upper_c``・**設定から導いたゲートの仕様**・区間の長さ・
古さの閾値で、出力は合否と最初の外れの項目の閉じた語彙（:class:`DomainCheck`）だけ。

- 判定に使う範囲は、呼び出し側が**自分の設定から** ``derive_hint_gate_spec`` で導いた仕様の
  ``certified_domain`` で、証明書の ``certified_domain`` を読まない（0152 §2.3）
- 値の写しを出力に載せない（0162 §2.2「`record_json` に載せる fand の再検査の結果」）
- ここは判定だけで、環状の列・保留の表・記録の受信は持たない（0162 §2.5 の PR-C）

**制御の値を1つも作らない。** ``requested_demand`` にも Reactive Guard / Critical Safety にも
届かない（ルール 2・3）。``coldaisle.control.safety`` / ``reactive`` / ``fallback`` /
``hardware`` を import しない（0133 §2.7 / 0162 §2.1。試験で固定する）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise

from coldaisle.control.learned_handoff import LearnedFrame
from coldaisle.control.schema import Demand, PerZone, Zone
from coldaisle.control.state import SnapshotSignal
from coldaisle.hint_certificate import (
    HintGateAxis,
    HintGateDomain,
    HintGateSpec,
    HintGateZoneRanges,
)
from coldaisle.measurement import Quality

_ZONES = (Zone.FRONT, Zone.REAR, Zone.TOP)


class DomainCheck(StrEnum):
    """最初に外れた項目の閉じた語彙（0162 §2.2 の判定 1〜5 の名前）。

    判定は番号の順に行い、最初に外れた項目だけを返す。
    """

    HINT_KEY = "hint_key"
    """1: ``N`` の (Regime, workload) がゲートの仕様の ``hint_keys`` に無い。"""
    PREVIOUS_DEMAND = "previous_demand"
    """2: ``N - 1`` の ``applied`` の3系統のどれかが無い・``previous_demand`` の範囲の外。"""
    SAFETY_FLOOR = "safety_floor"
    """3: ``N`` の ``safety_floor`` のどれかの系統が ``safety_floor_range`` の外。"""
    BASELINE = "baseline"
    """3: ``N`` の Baseline が無い・どれかの系統が ``baseline_range`` の外。"""
    SIGNAL_MISSING = "signal_missing"
    """4: 区間のある frame で、軸の metric の値か ``source_ts_ms`` が無い。"""
    SIGNAL_QUALITY = "signal_quality"
    """4: 区間のある frame で、軸の metric の quality が ``ok`` でない（``stale`` など）。"""
    SIGNAL_STALE = "signal_stale"
    """4: 区間のある frame で、軸の metric が古さの検査で ``domain_stale_after_ms`` 以上古い。"""
    TEMPERATURE_CPU = "temperature_cpu"
    """4: 区間のある frame の CPU 温度が「基準の ``upper_c`` + 軸の範囲」の外。"""
    TEMPERATURE_GPU = "temperature_gpu"
    """4: 区間のある frame の GPU 温度が「基準の ``upper_c`` + 軸の範囲」の外。"""
    WINDOW_FEATURE = "window_feature"
    """4: 区間のある frame の ``window_features`` の metric が軸の範囲の外。"""
    INTERVAL_UNCOVERED = "interval_uncovered"
    """5: 区間の先頭（``N`` の時刻 − 区間の長さ）より前の frame が無い・``N`` が列の最後でない。"""
    INTERVAL_GAP = "interval_gap"
    """5: 区間の frame の ``tick_id`` が連続していない（送れなかった tick がある）・時刻が戻る。"""


@dataclass(frozen=True, slots=True)
class DomainSignal:
    """1つの metric の、判定に要る値だけ（値・quality・``source_ts_ms``）。"""

    value: float | None
    quality: Quality
    source_ts_ms: int | None


@dataclass(frozen=True, slots=True)
class DomainFrame:
    """1つの frame の、判定に要る値だけ（0162 §2.2「持つもの」の軸の部分）。

    ``cpu_temperature`` / ``gpu_temperature`` は ``mpc.optimizer.cost_metrics`` の metric、
    ``features`` はゲートの仕様の ``window_features`` の metric の値。frame に無い metric は
    None（値が無いのと同じく外）。
    """

    tick_id: int
    ts_ms: int
    cpu_temperature: DomainSignal | None
    gpu_temperature: DomainSignal | None
    features: Mapping[str, DomainSignal | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DomainJudgement:
    """判定の結果。合格なら ``failed`` は None。"""

    failed: DomainCheck | None

    @property
    def passed(self) -> bool:
        """認証領域の中か。"""
        return self.failed is None


_PASSED = DomainJudgement(failed=None)


def domain_signal(signal: SnapshotSignal | None) -> DomainSignal | None:
    """snapshot の signal を判定の入力にする。無効にした signal は値が無いものとして扱う。"""
    if signal is None:
        return None
    return DomainSignal(
        value=signal.value if signal.enabled else None,
        quality=signal.quality,
        source_ts_ms=signal.source_ts_ms,
    )


def domain_frame(
    frame: LearnedFrame,
    *,
    cpu_temperature_metric: str,
    gpu_temperature_metric: str,
    spec: HintGateSpec,
) -> DomainFrame:
    """frame から判定に要る軸の値だけを取り出す（0162 §2.2「持つもの」）。"""
    signals = frame.snapshot.signals_by_metric
    return DomainFrame(
        tick_id=frame.snapshot.tick_id,
        ts_ms=frame.snapshot.ts_ms,
        cpu_temperature=domain_signal(signals.get(cpu_temperature_metric)),
        gpu_temperature=domain_signal(signals.get(gpu_temperature_metric)),
        features={
            feature.metric: domain_signal(signals.get(feature.metric))
            for feature in spec.certified_domain.window_features
        },
    )


def _within(value: float, minimum: float, maximum: float) -> bool:
    # NaN は比較がすべて偽なので外になる
    return minimum <= value <= maximum


def _zones_within(values: PerZone[Demand], ranges: HintGateZoneRanges) -> bool:
    for zone in _ZONES:
        bounds = getattr(ranges, zone.value)
        if not _within(values.get(zone), bounds.minimum, bounds.maximum):
            return False
    return True


def _previous_demand_within(
    previous: PerZone[Demand | None] | None, domain: HintGateDomain
) -> bool:
    if previous is None:
        return False
    axis = domain.previous_demand
    for zone in _ZONES:
        value = previous.get(zone)
        if value is None or not _within(value, axis.minimum, axis.maximum):
            return False
    return True


def _signal_check(
    signal: DomainSignal | None,
    *,
    reference_ts_ms: int,
    stale_after_ms: int,
    minimum: float,
    maximum: float,
    out_of_range: DomainCheck,
) -> DomainCheck | None:
    """1つの signal の検査（値と時刻の有無 → quality → 古さ → 範囲）。外れた項目か None。"""
    if signal is None or signal.value is None or signal.source_ts_ms is None:
        return DomainCheck.SIGNAL_MISSING
    if signal.quality is not Quality.OK:
        return DomainCheck.SIGNAL_QUALITY
    if reference_ts_ms - signal.source_ts_ms >= stale_after_ms:
        return DomainCheck.SIGNAL_STALE
    if not _within(signal.value, minimum, maximum):
        return out_of_range
    return None


def _frame_check(
    frame: DomainFrame,
    *,
    reference_ts_ms: int,
    domain: HintGateDomain,
    cpu_upper_c: float,
    gpu_upper_c: float,
    stale_after_ms: int,
) -> DomainCheck | None:
    temperatures: tuple[tuple[DomainSignal | None, float, HintGateAxis, DomainCheck], ...] = (
        (frame.cpu_temperature, cpu_upper_c, domain.temperature_cpu, DomainCheck.TEMPERATURE_CPU),
        (frame.gpu_temperature, gpu_upper_c, domain.temperature_gpu, DomainCheck.TEMPERATURE_GPU),
    )
    for signal, upper_c, axis, check in temperatures:
        # 「N の基準の upper_c + 軸の範囲」（0156 §2.4。ランナーの平らな window と同じ足し算）
        failed = _signal_check(
            signal,
            reference_ts_ms=reference_ts_ms,
            stale_after_ms=stale_after_ms,
            minimum=upper_c + axis.minimum,
            maximum=upper_c + axis.maximum,
            out_of_range=check,
        )
        if failed is not None:
            return failed
    for feature in domain.window_features:
        failed = _signal_check(
            frame.features.get(feature.metric),
            reference_ts_ms=reference_ts_ms,
            stale_after_ms=stale_after_ms,
            minimum=feature.minimum,
            maximum=feature.maximum,
            out_of_range=DomainCheck.WINDOW_FEATURE,
        )
        if failed is not None:
            return failed
    return None


def _interval(
    frames: Sequence[DomainFrame], lookback_ms: int
) -> tuple[Sequence[DomainFrame] | None, DomainCheck | None]:
    """区間の frame（区間の中の frame と、その前で最新の frame 1つ）。作れなければ外れた項目。

    区間は ``snapshot.ts_ms`` が ``[N の時刻 − lookback_ms, N の時刻]`` に入る frame と、
    それより前で最新の frame 1つ（0107 §2.1 の as-of で window の先頭の格子時刻に当たりうる
    frame）。
    """
    if not frames:
        return None, DomainCheck.INTERVAL_UNCOVERED
    for previous, current in pairwise(frames):
        if current.ts_ms <= previous.ts_ms or current.tick_id <= previous.tick_id:
            # 時刻順でない列を推測で並べ直さない
            return None, DomainCheck.INTERVAL_GAP
    start_ms = frames[-1].ts_ms - lookback_ms
    first = None
    for index, frame in enumerate(frames):
        if frame.ts_ms < start_ms:
            first = index
    if first is None:
        # 区間の先頭の格子時刻に当たる frame が無い（区間の先頭ちょうどの frame は区間の中）
        if frames[0].ts_ms != start_ms:
            return None, DomainCheck.INTERVAL_UNCOVERED
        first = 0
    return frames[first:], None


def judge_certified_domain(
    frames: Sequence[DomainFrame],
    *,
    key: tuple[str, str] | None,
    previous_demand: PerZone[Demand | None] | None,
    safety_floor: PerZone[Demand],
    baseline: PerZone[Demand] | None,
    cpu_upper_c: float,
    gpu_upper_c: float,
    spec: HintGateSpec,
    lookback_ms: int,
    stale_after_ms: int,
) -> DomainJudgement:
    """``N``（``frames`` の最後）の tick が認証領域の中か（決定記録 0162 §2.2 の判定 1〜5）。

    - ``frames``: 時刻順の frame の列。最後が ``N``。区間より古い frame を含んでよい（区間は
      ``lookback_ms`` からこの関数が選ぶ）
    - ``key``: ``N`` の (Regime の値, ヒントの workload の値)。無ければ外
    - ``previous_demand``: ``N - 1`` の frame の ``applied``（0107 §2.2 の anchor の action）
    - ``safety_floor`` / ``baseline``: ``N`` の値（``baseline`` は Baseline の requested。
      無ければ外）
    - ``cpu_upper_c`` / ``gpu_upper_c``: ``N`` の**基準の** context の ``target_band.*.upper_c``。
      区間の全 frame をこの基準で測る（0156 §2.4）
    - ``spec``: 呼び出し側が**自分の設定から**導いたゲートの仕様（証明書の値を渡さない。0152 §2.3）
    - ``lookback_ms``: fand は ``domain_lookback_ms``、worker は artifact の ``window_ms``
    - ``stale_after_ms``: fand は ``domain_stale_after_ms``、worker は artifact の
      ``stale_after_ms``

    古さ = 「その frame の**次の** frame の時刻（``N`` は自分の時刻）− ``source_ts_ms``」で、
    ``stale_after_ms`` 以上なら外（0162 §2.2「古さの検査」）。例外を投げない（入力の列が時刻順で
    なければ外にする）。
    """
    if lookback_ms < 0 or stale_after_ms <= 0:
        raise ValueError("lookback_ms は 0 以上、stale_after_ms は正にする")
    domain = spec.certified_domain
    # 1. 鍵
    keys = {(item.regime, item.workload) for item in domain.hint_keys}
    if key is None or key not in keys:
        return DomainJudgement(failed=DomainCheck.HINT_KEY)
    # 2. 直前 Demand
    if not _previous_demand_within(previous_demand, domain):
        return DomainJudgement(failed=DomainCheck.PREVIOUS_DEMAND)
    # 3. safety floor と Baseline
    if not _zones_within(safety_floor, domain.safety_floor_range):
        return DomainJudgement(failed=DomainCheck.SAFETY_FLOOR)
    if baseline is None or not _zones_within(baseline, domain.baseline_range):
        return DomainJudgement(failed=DomainCheck.BASELINE)
    interval, uncovered = _interval(frames, lookback_ms)
    # 4. 区間のすべての frame の値（区間を作れないときは 5 で外にする）
    if interval is not None:
        for index, frame in enumerate(interval):
            following = interval[index + 1] if index + 1 < len(interval) else frame
            failed = _frame_check(
                frame,
                reference_ts_ms=following.ts_ms,
                domain=domain,
                cpu_upper_c=cpu_upper_c,
                gpu_upper_c=gpu_upper_c,
                stale_after_ms=stale_after_ms,
            )
            if failed is not None:
                return DomainJudgement(failed=failed)
    # 5. 区間に欠けが無い
    if uncovered is not None:
        return DomainJudgement(failed=uncovered)
    assert interval is not None
    for previous, current in pairwise(interval):
        if current.tick_id != previous.tick_id + 1:
            return DomainJudgement(failed=DomainCheck.INTERVAL_GAP)
    return _PASSED
