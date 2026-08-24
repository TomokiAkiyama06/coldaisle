"""MockSource（#6）。

**シナリオごとの「何が起きるはずか」を、値で確かめる。**
生成器が動いたことだけを見ると、シナリオ定義を空にしても緑になる。

決定論（同じ seed で同一出力）は受入基準そのものなので、
JSON へ落とした形で丸ごと比較する。
"""

import json
import math
from itertools import islice

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from coldaisle.ingest import MockSource, RawHello, RawSample, Source, load_scenarios
from coldaisle.ingest.mock import StuckValue
from coldaisle.store import Quality, classify
from conftest import SCENARIOS_PATH

SCHEMA_PATH = SCENARIOS_PATH.parents[1] / "schemas" / "device_v1.schema.json"
INTERVAL_S = 2.5


def no_sleep(_: float) -> None:
    """待たない。時間圧縮の検証は専用のテストで行う。"""


def messages(scenario, **kwargs):
    return list(MockSource(scenario, sleep=no_sleep, **kwargs).stream())


def samples(scenario, **kwargs):
    return [m for m in messages(scenario, **kwargs) if isinstance(m, RawSample)]


def channel_series(scenario, channel):
    return [sample.channels[channel] for sample in samples(scenario)]


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


# ---------------------------------------------------------------- 契約


def test_mock_source_satisfies_the_source_protocol(scenarios):
    assert isinstance(MockSource(scenarios["idle"]), Source)


def test_every_scenario_is_defined_in_yaml(scenarios):
    """状況の定義はコードではなく YAML にある（#6）。"""
    assert set(scenarios) == {
        "idle",
        "ramp",
        "recirculation",
        "sensor_fail",
        "reset",
        "dropout",
    }


@pytest.mark.parametrize(
    "name", ["idle", "ramp", "recirculation", "sensor_fail", "reset", "dropout"]
)
def test_output_matches_the_device_schema(name, scenarios, validator):
    """生成物が実機と同じ契約に従う（決定記録 0003）。

    ここが通らないと、MockSource で作った上位レイヤが実機で動かない。
    """
    scenario = scenarios[name]
    if scenario.duration_s is None:
        scenario = scenario.model_copy(update={"duration_s": 30.0})
    produced = messages(scenario)
    assert produced, f"{name} が1件も出していない"
    for index, message in enumerate(produced):
        obj = message.to_json_obj()
        errors = sorted(validator.iter_errors(obj), key=str)
        assert not errors, f"{name}[{index}] がスキーマに合わない: {errors[0].message}"


def test_values_are_always_finite(scenarios):
    """非有限値を出さない（決定記録 0003 §2.8）。DB の統計を壊す値を作らない。"""
    for sample in samples(scenarios["ramp"]):
        for channel, value in sample.channels.items():
            assert value is None or math.isfinite(value), channel


def test_hello_comes_first_and_declares_the_interval(scenarios):
    """`interval_ms` は `expected_count`（決定記録 0002 §2.8）の元になる。"""
    produced = messages(scenarios["ramp"])
    assert isinstance(produced[0], RawHello)
    assert produced[0].interval_ms == scenarios["ramp"].interval_ms


# ---------------------------------------------------------------- 決定論


def test_same_seed_gives_identical_output(scenarios):
    """受入基準: seed 固定で同一出力。"""
    first = [m.to_json_obj() for m in messages(scenarios["ramp"])]
    second = [m.to_json_obj() for m in messages(scenarios["ramp"])]
    assert first == second


def test_different_seed_gives_different_noise(scenarios):
    first = channel_series(scenarios["idle"].model_copy(update={"duration_s": 30.0}), "room_temp")
    second = [
        sample.channels["room_temp"]
        for sample in samples(scenarios["idle"].model_copy(update={"duration_s": 30.0}), seed=1)
    ]
    assert first != second


def test_speed_changes_only_the_waiting(scenarios):
    """時間圧縮しても値は変わらない。圧縮したテストと実運用で挙動を分けない。"""
    slept: list[float] = []
    fast = MockSource(scenarios["ramp"], speed=60.0, sleep=slept.append)
    quick = [m.to_json_obj() for m in fast.stream()]
    assert quick == [m.to_json_obj() for m in messages(scenarios["ramp"])]
    assert slept and all(seconds == pytest.approx(INTERVAL_S / 60) for seconds in slept)


def test_speed_must_be_positive(scenarios):
    with pytest.raises(ValueError, match="speed"):
        MockSource(scenarios["idle"], speed=0)


def test_scenario_without_duration_never_ends(scenarios):
    """`idle` は終端を持たない。デーモンを流し続けられる。"""
    assert scenarios["idle"].duration_s is None
    head = list(islice(MockSource(scenarios["idle"], sleep=no_sleep).stream(), 500))
    assert len(head) == 500


# ---------------------------------------------------------------- シナリオごとの中身


def test_idle_stays_near_room_temperature(scenarios):
    """室温26℃前後、各部 +1〜3℃、微小なノイズ。"""
    idle = scenarios["idle"].model_copy(update={"duration_s": 120.0})
    for sample in samples(idle):
        room = sample.channels["room_temp"]
        assert room == pytest.approx(26.0, abs=0.5)
        for channel in ("front_intake", "gpu_intake", "gpu_exhaust", "rear_exhaust"):
            assert 0.5 <= sample.channels[channel] - room <= 3.0
        assert sample.err == ()


def test_ramp_raises_gpu_exhaust_by_15c_in_ten_minutes(scenarios):
    series = channel_series(scenarios["ramp"], "gpu_exhaust")
    assert series[-1] - series[0] == pytest.approx(15.0, abs=0.3)
    # 単調に近い上昇であること。ノイズで往復するだけの系列と区別する
    midpoint = series[len(series) // 2]
    assert series[0] < midpoint < series[-1]


def test_recirculation_crosses_the_alert_threshold_and_stays(scenarios):
    """FR-404 は `d.intake_rise > 5.0℃` が5分継続で発火する。

    その入力になっていること（超えたあと5分以上そのままであること）まで見る。
    """
    produced = samples(scenarios["recirculation"])
    rises = [sample.channels["front_intake"] - sample.channels["room_temp"] for sample in produced]
    assert rises[0] < 5.0, "最初から超えていては継続時間の試験にならない"
    crossed_at = next(index for index, rise in enumerate(rises) if rise > 5.0)
    assert all(rise > 5.0 for rise in rises[crossed_at:])
    assert (len(rises) - crossed_at) * INTERVAL_S >= 300, "5分の継続に足りない"


def test_sensor_fail_returns_the_disconnected_sentinel(scenarios):
    """途中から `-127.00` を返す（要件 §5.3 / spec-review C-02）。"""
    produced = samples(scenarios["sensor_fail"])
    before = produced[: int(120 / INTERVAL_S)]
    after = produced[int(120 / INTERVAL_S) :]

    assert all(sample.channels["rear_exhaust"] > 20.0 for sample in before)
    assert all(sample.err == () for sample in before)
    assert all(sample.channels["rear_exhaust"] == -127.0 for sample in after)
    assert all(sample.err == ("rear_exhaust:-127",) for sample in after)
    # 他のチャネルは壊れない。1本の故障が全体の欠測に見えてはいけない
    assert all(sample.channels["gpu_exhaust"] > 20.0 for sample in after)


def test_sensor_fail_is_classified_as_suspect(scenarios, rules):
    """L0 が出した値が L1 の判定に届くことまで確かめる。"""
    last = samples(scenarios["sensor_fail"])[-1]
    assert classify("air.rear_exhaust", last.channels["rear_exhaust"], rules) is Quality.SUSPECT
    assert classify("air.gpu_exhaust", last.channels["gpu_exhaust"], rules) is Quality.OK


def test_reset_rewinds_uptime_and_restarts_the_sequence(scenarios):
    """FR-106（`up` の巻き戻り＝再起動）と FR-105 の入力になる。"""
    produced = messages(scenarios["reset"])
    hellos = [index for index, message in enumerate(produced) if isinstance(message, RawHello)]
    assert len(hellos) == 2, "再起動のたびに起動バナーが出る"

    ordered = [m for m in produced if isinstance(m, RawSample)]
    rewound = [
        index for index in range(1, len(ordered)) if ordered[index].up < ordered[index - 1].up
    ]
    assert len(rewound) == 1
    boundary = rewound[0]
    assert ordered[boundary].seq == 0
    assert ordered[boundary - 1].seq > 0
    assert ordered[boundary].up < ordered[boundary - 1].up


def test_dropout_creates_a_sequence_gap(scenarios):
    """デバイスは動き続けているので `seq` は進む（FR-105 の検出対象）。"""
    produced = samples(scenarios["dropout"])
    gaps = [
        (produced[index - 1].seq, produced[index].seq)
        for index in range(1, len(produced))
        if produced[index].seq != produced[index - 1].seq + 1
    ]
    assert len(gaps) == 1
    before, after = gaps[0]
    assert (after - before - 1) == int(30 / INTERVAL_S), "30秒ぶん飛ぶ"


def test_dropout_does_not_shift_the_values_around_it(scenarios):
    """届かなかっただけで、以降の値が変わってはいけない。

    ドロップアウト中も乱数を引いていることの確認。引かないと、
    復帰後の系列がドロップアウトの有無で変わってしまう。
    """
    with_gap = {sample.seq: sample.channels for sample in samples(scenarios["dropout"])}
    without = {
        sample.seq: sample.channels
        for sample in samples(scenarios["dropout"].model_copy(update={"effects": ()}))
    }
    assert with_gap == {seq: values for seq, values in without.items() if seq in with_gap}


# ---------------------------------------------------------------- 定義ファイル


def test_unknown_effect_kind_is_rejected(tmp_path):
    """効果の綴り違いを黙って無視しない。無視すると**何も起きないシナリオ**になる。"""
    path = tmp_path / "scenarios.yaml"
    path.write_text(
        "defaults: {interval_ms: 2500, duration_s: 10, seed: 1, baseline: "
        "{room_c: 26.0, room_humidity_pct: 48.0, noise_c: 0.0, noise_pct: 0.0, offsets_c: {}}}\n"
        "scenarios:\n  broken:\n    description: x\n    effects:\n      - kind: drfit\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_scenarios(path)


def test_unknown_key_is_rejected(tmp_path):
    path = tmp_path / "scenarios.yaml"
    path.write_text(
        "defaults: {interval_ms: 2500, duration_s: 10, seed: 1, baseline: "
        "{room_c: 26.0, room_humidity_pct: 48.0, noise_c: 0.0, noise_pct: 0.0, offsets_c: {}}}\n"
        "scenarios:\n  broken:\n    description: x\n    durations_s: 10\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_scenarios(path)


def test_file_without_scenarios_is_rejected(tmp_path):
    path = tmp_path / "scenarios.yaml"
    path.write_text("defaults: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="scenarios"):
        load_scenarios(path)


def test_stuck_value_can_be_a_plain_dropout_of_one_channel(scenarios):
    """`value: null` で「欠測」も表現できる（実機ファームの null + err 側）。

    `err` を省いた場合に、理由なしの欠測として出ることまで確かめる。
    決定記録 0003 §2.9 のとおり `err` は補助情報であり、必須ではない。
    """
    silent = scenarios["sensor_fail"].model_copy(
        update={
            "effects": (
                StuckValue(kind="stuck_value", channel="top_exhaust", start_s=10.0, value=None),
            ),
            "duration_s": 30.0,
        }
    )
    produced = samples(silent)
    assert produced[0].channels["top_exhaust"] is not None
    assert produced[-1].channels["top_exhaust"] is None
    assert produced[-1].err == ()
