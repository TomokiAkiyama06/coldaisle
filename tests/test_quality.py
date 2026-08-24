"""品質フラグの判定（#5 受入基準、要件 §5.3、spec-review C-02 / C-04）。

**ちょうど 85.00 が suspect になること**が受入基準そのもの。
`-127.00` と違って排気温度としてありえる値なので、見逃すと
誤警報ではなく誤った安心を生む。
"""

import math

import pytest
from pydantic import ValidationError

from coldaisle.store.quality import (
    DS18B20_DISCONNECTED_C,
    DS18B20_POWER_ON_RESET_C,
    Quality,
    QualityRules,
    classify,
)


def test_power_on_reset_value_is_suspect(rules):
    """受入基準: ちょうど 85.00 を suspect として記録する。"""
    assert classify("air.gpu_exhaust", DS18B20_POWER_ON_RESET_C, rules) is Quality.SUSPECT


@pytest.mark.parametrize("value", [84.99, 85.01, 84.9375, 85.0625])
def test_values_next_to_the_power_on_reset_value_are_ok(value, rules):
    """疑うのは 85.00 ちょうどだけ。

    84.9375 / 85.0625 は 11bit 分解能（spec-review C-01）で実際に出る隣の刻み。
    ここまで疑うと、85℃付近の正常な排気温度が丸ごと統計から消える。
    """
    assert classify("air.gpu_exhaust", value, rules) is Quality.OK


def test_disconnected_value_is_suspect(rules):
    assert classify("air.rear_exhaust", DS18B20_DISCONNECTED_C, rules) is Quality.SUSPECT


def test_null_is_missing(rules):
    assert classify("air.rear_exhaust", None, rules) is Quality.MISSING


def test_nan_is_missing(rules):
    """デバイスが NaN を出しても値としては扱わない（決定記録 0003 §2.8）。"""
    assert classify("air.room", math.nan, rules) is Quality.MISSING


@pytest.mark.parametrize("value", [math.inf, -math.inf])
def test_infinite_values_are_suspect(value, rules):
    assert classify("air.room", value, rules) is Quality.SUSPECT


@pytest.mark.parametrize("value", [-55.0, 125.0, 26.42])
def test_values_inside_the_sensor_range_are_ok(value, rules):
    assert classify("air.gpu_intake", value, rules) is Quality.OK


@pytest.mark.parametrize("value", [-55.01, 125.01, -200.0, 999.0])
def test_values_outside_the_sensor_range_are_suspect(value, rules):
    assert classify("air.gpu_intake", value, rules) is Quality.SUSPECT


@pytest.mark.parametrize("value", [0.0, 100.0])
def test_humidity_rails_are_suspect(value, rules):
    """0%RH / 100%RH に張り付くのは配線異常の典型（spec-review C-04）。"""
    assert classify("air.room_humidity", value, rules) is Quality.SUSPECT


@pytest.mark.parametrize("value", [0.1, 48.2, 99.9])
def test_humidity_inside_the_rails_is_ok(value, rules):
    assert classify("air.room_humidity", value, rules) is Quality.OK


def test_humidity_is_not_judged_by_the_temperature_rules(rules):
    """湿度 85.00%RH は正常値。温度側の 85.00 の罠を湿度へ持ち込まない。"""
    assert classify("air.room_humidity", DS18B20_POWER_ON_RESET_C, rules) is Quality.OK


@pytest.mark.parametrize("value", [-1.0, 61.0])
def test_room_temperature_outside_the_habitable_range_is_suspect(value, rules):
    """室温としてありえない値。センサー範囲内でも疑う（spec-review C-04）。"""
    assert classify("air.room", value, rules) is Quality.SUSPECT


def test_room_range_applies_only_to_the_room_metric(rules):
    """排気は 60℃ を超えうる。室温の範囲を他チャネルへ広げない。"""
    assert classify("air.gpu_exhaust", 61.0, rules) is Quality.OK


def test_stale_is_not_decided_here(rules):
    """`stale` は前回受信からの経過で決まるため、保存時には判定できない。

    判定するのは `SqliteStore.latest()`。ここが `stale` を返し始めたら、
    「値は正常だが古い」を「値が異常」と取り違えている。
    """
    values = [26.4, None, math.nan, -127.0, 85.0, 200.0]
    assert Quality.STALE not in {classify("air.room", value, rules) for value in values}


def test_rules_are_replaceable(rules):
    """しきい値は設定から差し替える（AGENTS.md ルール6）。"""
    strict = rules.model_copy(update={"room_temp_min_c": 20.0, "room_temp_max_c": 28.0})
    assert classify("air.room", 30.0, strict) is Quality.SUSPECT
    assert classify("air.room", 30.0, rules) is Quality.OK


# ---------------------------------------------------------------- ドメイン別の判定


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("gpu.0.core", DS18B20_POWER_ON_RESET_C),  # GPU コアの85℃はありふれた正常値
        ("gpu.0.hotspot", 95.0),
        ("gpu.0.vram_used", 40.0),  # GB。DS18B20 の測定範囲とは無関係
        ("power.wall", 500.0),  # W
        ("power.gpu.0", 600.0),
        ("cpu.package", 95.0),
        ("sys.cuda_processes", 3.0),
    ],
)
def test_other_domains_are_not_judged_by_ds18b20_rules(metric, value, rules):
    """DS18B20 の番兵値と測定範囲を全メトリクスへ当てない。

    当てると `gpu.0.core` のちょうど 85℃ や `power.wall` の 500W が
    `suspect` になり、**正常値がロールアップの母数から静かに消える。**
    `air.*` 以外の判定規則は #34 で決める。
    """
    assert classify(metric, value, rules) is Quality.OK


@pytest.mark.parametrize("metric", ["air.gpu_exhaust", "air.front_intake", "air.rear_exhaust"])
def test_air_domain_still_gets_the_ds18b20_rules(metric, rules):
    """ドメインで絞ったあとも、外付けセンサーの判定は効いている。"""
    assert classify(metric, DS18B20_POWER_ON_RESET_C, rules) is Quality.SUSPECT
    assert classify(metric, DS18B20_DISCONNECTED_C, rules) is Quality.SUSPECT


@pytest.mark.parametrize("metric", ["gpu.0.core", "power.wall"])
def test_missing_and_non_finite_apply_to_every_domain(metric, rules):
    """値が無い・有限でないことは、センサーの種類によらず判定できる。"""
    assert classify(metric, None, rules) is Quality.MISSING
    assert classify(metric, math.nan, rules) is Quality.MISSING
    assert classify(metric, math.inf, rules) is Quality.SUSPECT


# ---------------------------------------------------------------- 設定ファイル


def test_rules_come_from_the_config_file(rules):
    """`config/quality.yaml` が唯一の情報源（AGENTS.md ルール6）。"""
    assert rules.stale_after_ms == 10_000  # 要件 §5.3「10秒以上」


def test_rules_have_no_defaults():
    """既定値を持たない。省略した呼び出しが黙って別のしきい値で動かない。"""
    with pytest.raises(ValidationError):
        QualityRules()  # type: ignore[call-arg]


def test_unknown_key_is_rejected(tmp_path):
    """設定のタイプミスを黙って無視しない。無視すると既定値で動いたと誤解する。"""
    path = tmp_path / "quality.yaml"
    path.write_text("stale_after_ms: 10000\nstale_after_sec: 10\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        QualityRules.from_yaml(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        QualityRules.from_yaml(tmp_path / "absent.yaml")


def test_non_mapping_file_raises(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("- 10000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="辞書ではない"):
        QualityRules.from_yaml(path)


def test_config_file_covers_every_field(rules):
    """モデルに項目を足したら設定ファイルにも足す。片方だけの追加を落とす。"""
    import yaml

    from conftest import QUALITY_RULES_PATH

    loaded = yaml.safe_load(QUALITY_RULES_PATH.read_text(encoding="utf-8"))
    assert set(loaded) == set(QualityRules.model_fields)
