"""正規化（#8）。

**較正と品質判定の順序**がここの要点。逆にすると番兵値を取り逃す。
"""

import math

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.normalize import (
    CHANNEL_TO_METRIC,
    DEVICE_RESTART_METRIC,
    DROPPED_SAMPLES_METRIC,
    Normalizer,
)
from coldaisle.ingest.protocol import RawSample
from coldaisle.store import Quality

OFFSETS = Calibration(offsets_c={"gpu_intake": 0.3, "rear_exhaust": -0.2})


def raw(seq=0, up=1_000, **channels) -> RawSample:
    return RawSample(seq=seq, up=up, channels=channels)


@pytest.fixture
def normalizer(rules):
    return Normalizer(rules=rules, calibration=OFFSETS, clock=SimulatedClock(5_000))


def values(normalized) -> dict[str, float | None]:
    return {reading.metric: reading.value for reading in normalized.sample.readings}


def qualities(normalized) -> dict[str, Quality]:
    return {reading.metric: reading.quality for reading in normalized.sample.readings}


# ---------------------------------------------------------------- 対応付け


def test_channels_map_to_metrics(normalizer):
    """デバイスの短い名前をホストの名前空間へ移す（決定記録 0003）。"""
    normalized = normalizer.normalize(raw(room_temp=26.0, front_intake=27.0))
    assert set(values(normalized)) == {"air.room", "air.front_intake"}


def test_mapping_covers_every_channel_of_the_contract():
    """スキーマのチャネルが全て対応表にあること。片方だけ足す事故を防ぐ。"""
    from coldaisle.ingest.protocol import SAMPLE_CHANNELS

    assert set(SAMPLE_CHANNELS) == set(CHANNEL_TO_METRIC)


def test_unknown_channels_are_dropped_not_fatal(normalizer):
    """ファームが1つ足しただけで取り込みが止まらない（決定記録 0003 §2.7）。"""
    normalized = normalizer.normalize(raw(room_temp=26.0, vrm_temp=55.0))
    assert normalized.unknown_channels == ("vrm_temp",)
    assert set(values(normalized)) == {"air.room"}


def test_host_time_comes_from_the_clock(normalizer):
    """デバイス時刻は信用しない（決定 D-05）。"""
    normalized = normalizer.normalize(raw(up=999_999, room_temp=26.0))
    assert normalized.sample.ts_ms == 5_000
    assert normalized.sample.up_ms == 999_999


# ---------------------------------------------------------------- 較正（FR-107）


def test_offset_is_applied_to_good_values(normalizer):
    normalized = normalizer.normalize(raw(gpu_intake=28.0, rear_exhaust=27.0))
    assert values(normalized)["air.gpu_intake"] == pytest.approx(28.3)
    assert values(normalized)["air.rear_exhaust"] == pytest.approx(26.8)


def test_channels_without_an_offset_pass_through(normalizer):
    assert values(normalizer.normalize(raw(top_exhaust=27.0)))["air.top_exhaust"] == 27.0


def test_humidity_is_not_calibrated(rules):
    """較正値の単位は℃。%RH に足さない（#13 が扱うのは温度チャネルのみ）。"""
    wrong = Calibration(offsets_c={"room_humidity": 5.0})
    normalizer = Normalizer(rules=rules, calibration=wrong, clock=SimulatedClock(0))
    assert values(normalizer.normalize(raw(room_humidity=48.0)))["air.room_humidity"] == 48.0


@pytest.mark.parametrize("sentinel", [-127.0, 85.0])
def test_sentinels_are_judged_before_calibration(sentinel, rules):
    """**順序が逆だと番兵値をすり抜ける。**

    `-127.00 + 0.3` は `-126.7`、`85.00 + 0.3` は `85.3` になり、
    どちらも判定に掛からなくなる（要件 §5.3 / spec-review C-02）。
    """
    normalizer = Normalizer(
        rules=rules,
        calibration=Calibration(offsets_c={"rear_exhaust": 0.3}),
        clock=SimulatedClock(0),
    )
    normalized = normalizer.normalize(raw(rear_exhaust=sentinel))
    assert qualities(normalized)["air.rear_exhaust"] is Quality.SUSPECT
    assert values(normalized)["air.rear_exhaust"] == sentinel, "疑わしい値に較正を足さない"


def test_missing_values_stay_missing(normalizer):
    normalized = normalizer.normalize(raw(gpu_intake=None))
    assert qualities(normalized)["air.gpu_intake"] is Quality.MISSING
    assert values(normalized)["air.gpu_intake"] is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(math.inf, Quality.SUSPECT), (-math.inf, Quality.SUSPECT), (math.nan, Quality.MISSING)],
)
def test_non_finite_values_lose_the_value_but_keep_the_row(value, expected, normalizer):
    """非有限値は DB に入れない（決定記録 0003 §2.8）。

    **行は残す。** 1チャネルの異常でサンプル全体を捨てると、他の6チャネルの
    観測まで失う。行ごと捨てるのはパーサ（FR-103 / #12）の役目。
    """
    normalized = normalizer.normalize(raw(gpu_intake=value, room_temp=26.0))
    assert values(normalized)["air.gpu_intake"] is None
    assert qualities(normalized)["air.gpu_intake"] is expected
    assert values(normalized)["air.room"] == 26.0, "他のチャネルは残る"


# ---------------------------------------------------------------- seq / up


def test_continuous_sequence_reports_no_gap(normalizer):
    normalizer.normalize(raw(seq=10, up=1_000, room_temp=26.0))
    normalized = normalizer.normalize(raw(seq=11, up=3_500, room_temp=26.0))
    assert normalized.dropped_samples == 0
    assert DROPPED_SAMPLES_METRIC not in values(normalized)


def test_sequence_gap_is_counted_and_recorded(normalizer):
    """FR-105。飛んだときだけメトリクスに書く。"""
    normalizer.normalize(raw(seq=10, up=1_000, room_temp=26.0))
    normalized = normalizer.normalize(raw(seq=15, up=13_500, room_temp=26.0))
    assert normalized.dropped_samples == 4
    assert values(normalized)[DROPPED_SAMPLES_METRIC] == 4.0


def test_restart_is_detected_by_uptime_rewind(normalizer):
    """FR-106。`up` が戻ったら再起動。"""
    normalizer.normalize(raw(seq=100, up=250_000, room_temp=26.0))
    normalized = normalizer.normalize(raw(seq=0, up=1_200, room_temp=26.0))
    assert normalized.device_restarted
    assert values(normalized)[DEVICE_RESTART_METRIC] == 1.0


def test_restart_is_not_counted_as_a_gap(normalizer):
    """再起動で `seq` は 0 に戻る。取りこぼしと数えない。"""
    normalizer.normalize(raw(seq=100, up=250_000, room_temp=26.0))
    normalized = normalizer.normalize(raw(seq=0, up=1_200, room_temp=26.0))
    assert normalized.dropped_samples == 0
    assert DROPPED_SAMPLES_METRIC not in values(normalized)


def test_repeated_sequence_is_flagged(normalizer):
    """再送・順序入れ替わり。取りこぼしとは別の異常として伝える。"""
    normalizer.normalize(raw(seq=10, up=1_000, room_temp=26.0))
    normalized = normalizer.normalize(raw(seq=10, up=3_500, room_temp=26.0))
    assert normalized.out_of_order
    assert normalized.dropped_samples == 0


def test_out_of_order_sample_does_not_move_the_baseline(normalizer):
    """`10, 5, 11` の 11 は 10 の続き。**5件飛んだことにしない。**

    逆行したサンプルで基準を戻すと、次の正常なサンプルが大量の取りこぼしに見える。
    FR-105 が数えるのは失われた件数なので、基準は到達済みの最大値で持つ。

    `up` は進んでいる（時刻は戻らない）ことに注意。`up` が戻る場合は
    定義上デバイス再起動である（FR-106）。
    """
    normalizer.normalize(raw(seq=10, up=25_000, room_temp=26.0))
    out_of_order = normalizer.normalize(raw(seq=5, up=27_500, room_temp=26.0))
    forward = normalizer.normalize(raw(seq=11, up=30_000, room_temp=26.0))

    assert out_of_order.out_of_order
    assert not out_of_order.device_restarted
    assert forward.dropped_samples == 0
    assert DROPPED_SAMPLES_METRIC not in values(forward)


def test_gap_after_an_out_of_order_sample_is_measured_from_the_baseline(normalizer):
    """基準は 10 のまま。13 なら飛びは2件。"""
    normalizer.normalize(raw(seq=10, up=25_000, room_temp=26.0))
    normalizer.normalize(raw(seq=5, up=27_500, room_temp=26.0))
    assert normalizer.normalize(raw(seq=13, up=32_500, room_temp=26.0)).dropped_samples == 2


def test_first_sample_has_no_history(normalizer):
    """起動直後は比較対象が無い。飛びとして報告しない。"""
    normalized = normalizer.normalize(raw(seq=1042, up=2_605_250, room_temp=26.0))
    assert normalized.dropped_samples == 0
    assert not normalized.device_restarted


def test_calibration_file_must_be_a_mapping(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text("[0.0]", encoding="utf-8")
    with pytest.raises(ValueError, match="辞書ではない"):
        Calibration.from_json(path)


@pytest.mark.parametrize("offset", ["1e309", "-1e309"])
def test_non_finite_offsets_are_rejected_at_load(offset, tmp_path):
    """`1e309` は `json.loads` が `inf` にする。**読み込み時に弾く。**

    通すと較正後の値が非有限になって `Reading` に拒否され、そのチャネルを含む
    全サンプルが1件ずつ破棄され続ける。設定の誤りは設定を読む時点で言う。
    """
    path = tmp_path / "calibration.json"
    path.write_text('{"offsets_c": {"gpu_intake": ' + offset + "}}", encoding="utf-8")
    with pytest.raises(ValidationError):
        Calibration.from_json(path)
