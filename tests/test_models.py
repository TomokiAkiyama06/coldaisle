"""コアデータモデルの不変条件（#5、決定記録 0002）。

「通すべきものを通す」と「弾くべきものを弾く」を両方書く。
要件 §5.1 に載っている名前が実際に通ることを確かめないと、
規約を厳しくしすぎて必須メトリクスを弾く実装でも緑になる。
"""

import math

import pytest
from pydantic import ValidationError

from coldaisle.store.models import Quality, Reading, Sample, validate_metric

# 要件 §5.1 の表。`board.chipset` は決定記録 0002 §2.1 で `chipset` から改めた名前
REQUIRED_METRICS = [
    "air.room",
    "air.room_humidity",
    "air.front_intake",
    "air.gpu_intake",
    "air.gpu_exhaust",
    "air.top_exhaust",
    "air.rear_exhaust",
    "gpu.0.core",
    "gpu.0.hotspot",
    "gpu.0.mem",
    "gpu.0.vram_used",
    "cpu.package",
    "cpu.vrm",
    "board.chipset",
    "power.gpu.0",
    "power.wall",
    "sys.cuda_processes",
]


@pytest.mark.parametrize("metric", REQUIRED_METRICS)
def test_required_metrics_are_accepted(metric):
    assert validate_metric(metric) == metric


@pytest.mark.parametrize(
    ("metric", "reason"),
    [
        ("chipset", "ドメインの無い1語"),
        ("Air.room", "大文字"),
        ("air-room", "ハイフン"),
        ("air.", "セグメントが空"),
        (".room", "先頭がドット"),
        ("air..room", "ドットの連続"),
        ("0air.room", "先頭が数字"),
        ("air.room.a.b.c", "5セグメント"),
        ("air room", "空白"),
        ("air.room_c", None),  # 単位付きは規約違反だが文法では弾けない（下の注記参照）
    ],
)
def test_invalid_metrics_are_rejected(metric, reason):
    if reason is None:
        # 「単位を名前に含めない」は文法では表現できない。レビューで見る規約であり、
        # ここで通ってしまうことを明示しておく（決定記録 0002 §2.1）
        assert validate_metric(metric) == metric
        return
    with pytest.raises(ValueError, match="命名規約"):
        validate_metric(metric)


@pytest.mark.parametrize("metric", ["d.intake_rise", "d.gpu_delta"])
def test_derived_metrics_are_rejected(metric):
    """派生値は保存しない（決定記録 0002 §2.2）。DB の CHECK と二重に持つ。"""
    with pytest.raises(ValueError, match="派生メトリクス"):
        validate_metric(metric)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_reading_rejects_non_finite_values(value):
    """決定記録 0003 §2.8。ここまで来た非有限値を DB の手前で落とす。"""
    with pytest.raises(ValidationError):
        Reading(metric="air.room", value=value, quality=Quality.OK)


def test_reading_allows_null_value():
    """欠測は値なしで記録する。行自体は残す（欠測率の母数になるため）。"""
    reading = Reading(metric="air.rear_exhaust", quality=Quality.MISSING)
    assert reading.value is None


def test_sample_rejects_duplicate_metrics():
    with pytest.raises(ValidationError, match="重複"):
        Sample(
            ts_ms=1,
            readings=(
                Reading(metric="air.room", value=26.0, quality=Quality.OK),
                Reading(metric="air.room", value=26.5, quality=Quality.OK),
            ),
        )


def test_sample_rejects_negative_timestamp():
    with pytest.raises(ValidationError):
        Sample(ts_ms=-1, readings=())


def test_models_are_frozen():
    """取り込み後に値を書き換えられると、保存した行と食い違う。"""
    reading = Reading(metric="air.room", value=26.0, quality=Quality.OK)
    with pytest.raises(ValidationError):
        reading.value = 27.0
