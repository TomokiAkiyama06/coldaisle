"""品質フラグの判定（#5 受入基準、要件 §5.3、spec-review C-02 / C-04）。

**ちょうど 85.00 が suspect になること**が受入基準そのもの。
`-127.00` と違って排気温度としてありえる値なので、見逃すと
誤警報ではなく誤った安心を生む。
"""

import math

import pytest

from coldaisle.store.quality import (
    DS18B20_DISCONNECTED_C,
    DS18B20_POWER_ON_RESET_C,
    Quality,
    QualityRules,
    classify,
)

RULES = QualityRules()


def test_power_on_reset_value_is_suspect():
    """受入基準: ちょうど 85.00 を suspect として記録する。"""
    assert classify("air.gpu_exhaust", DS18B20_POWER_ON_RESET_C, RULES) is Quality.SUSPECT


@pytest.mark.parametrize("value", [84.99, 85.01, 84.9375, 85.0625])
def test_values_next_to_the_power_on_reset_value_are_ok(value):
    """疑うのは 85.00 ちょうどだけ。

    84.9375 / 85.0625 は 11bit 分解能（spec-review C-01）で実際に出る隣の刻み。
    ここまで疑うと、85℃付近の正常な排気温度が丸ごと統計から消える。
    """
    assert classify("air.gpu_exhaust", value, RULES) is Quality.OK


def test_disconnected_value_is_suspect():
    assert classify("air.rear_exhaust", DS18B20_DISCONNECTED_C, RULES) is Quality.SUSPECT


def test_null_is_missing():
    assert classify("air.rear_exhaust", None, RULES) is Quality.MISSING


def test_nan_is_missing():
    """デバイスが NaN を出しても値としては扱わない（決定記録 0003 §2.8）。"""
    assert classify("air.room", math.nan, RULES) is Quality.MISSING


@pytest.mark.parametrize("value", [math.inf, -math.inf])
def test_infinite_values_are_suspect(value):
    assert classify("air.room", value, RULES) is Quality.SUSPECT


@pytest.mark.parametrize("value", [-55.0, 125.0, 26.42])
def test_values_inside_the_sensor_range_are_ok(value):
    assert classify("air.gpu_intake", value, RULES) is Quality.OK


@pytest.mark.parametrize("value", [-55.01, 125.01, -200.0, 999.0])
def test_values_outside_the_sensor_range_are_suspect(value):
    assert classify("air.gpu_intake", value, RULES) is Quality.SUSPECT


@pytest.mark.parametrize("value", [0.0, 100.0])
def test_humidity_rails_are_suspect(value):
    """0%RH / 100%RH に張り付くのは配線異常の典型（spec-review C-04）。"""
    assert classify("air.room_humidity", value, RULES) is Quality.SUSPECT


@pytest.mark.parametrize("value", [0.1, 48.2, 99.9])
def test_humidity_inside_the_rails_is_ok(value):
    assert classify("air.room_humidity", value, RULES) is Quality.OK


def test_humidity_is_not_judged_by_the_temperature_rules():
    """湿度 85.00%RH は正常値。温度側の 85.00 の罠を湿度へ持ち込まない。"""
    assert classify("air.room_humidity", DS18B20_POWER_ON_RESET_C, RULES) is Quality.OK


@pytest.mark.parametrize("value", [-1.0, 61.0])
def test_room_temperature_outside_the_habitable_range_is_suspect(value):
    """室温としてありえない値。センサー範囲内でも疑う（spec-review C-04）。"""
    assert classify("air.room", value, RULES) is Quality.SUSPECT


def test_room_range_applies_only_to_the_room_metric():
    """排気は 60℃ を超えうる。室温の範囲を他チャネルへ広げない。"""
    assert classify("air.gpu_exhaust", 61.0, RULES) is Quality.OK


def test_stale_is_not_decided_here():
    """`stale` は前回受信からの経過で決まるため、保存時には判定できない。

    判定するのは `SqliteStore.latest()`。ここが `stale` を返し始めたら、
    「値は正常だが古い」を「値が異常」と取り違えている。
    """
    values = [26.4, None, math.nan, -127.0, 85.0, 200.0]
    assert Quality.STALE not in {classify("air.room", value, RULES) for value in values}


def test_rules_are_replaceable():
    """しきい値は設定から差し替える（AGENTS.md ルール6）。既定値に依存しない。"""
    strict = QualityRules(room_temp_min_c=20.0, room_temp_max_c=28.0)
    assert classify("air.room", 30.0, strict) is Quality.SUSPECT
    assert classify("air.room", 30.0, RULES) is Quality.OK
