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

from coldaisle.clock import SimulatedClock
from coldaisle.ingest import MockSource, RawHello, RawSample, Source, load_scenarios
from coldaisle.ingest.mock import StuckValue
from coldaisle.store import (
    Quality,
    Reading,
    Sample,
    SqliteStore,
    classify,
)
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
        "sensor_fail_raw",
        "sensor_reset_85",
        "reset",
        "dropout",
    }


@pytest.mark.parametrize(
    "name",
    [
        "idle",
        "ramp",
        "recirculation",
        "sensor_fail",
        "sensor_fail_raw",
        "sensor_reset_85",
        "reset",
        "dropout",
    ],
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


def test_sensor_fail_reports_the_contract_shape(scenarios):
    """既定は `null` + `err`。**実機のファームが送ってくる形**（決定記録 0003）。

    MockSource の役割は「実機が送ってくるもの」の再現なので、
    契約どおりの故障をこちらに置く。生値が届く場合は下の2つで試す。
    """
    produced = samples(scenarios["sensor_fail"])
    before = produced[: int(120 / INTERVAL_S)]
    after = produced[int(120 / INTERVAL_S) :]

    assert all(sample.channels["rear_exhaust"] > 20.0 for sample in before)
    assert all(sample.err == () for sample in before)
    assert all(sample.channels["rear_exhaust"] is None for sample in after)
    assert all(sample.err == ("rear_exhaust:-127",) for sample in after)
    # 他のチャネルは壊れない。1本の故障が全体の欠測に見えてはいけない
    assert all(sample.channels["gpu_exhaust"] > 20.0 for sample in after)


def test_sensor_fail_raw_sends_the_sentinel_without_any_hint(scenarios):
    """契約違反のファーム。生値が届き、**`err` も付かない**。

    `err` を付けると「デバイスが教えてくれる」前提になり、
    ホストの防御的パース（要件 §5.3）の試験にならない。
    """
    after = samples(scenarios["sensor_fail_raw"])[int(120 / INTERVAL_S) :]
    assert all(sample.channels["rear_exhaust"] == -127.0 for sample in after)
    assert all(sample.err == () for sample in after)


def test_sensor_reset_85_is_indistinguishable_from_a_real_reading(scenarios, rules):
    """ちょうど 85.00 が**測定範囲にも収まり、排気温度としてありえる**こと。

    `-127.00` は誰が見ても異常だが、`85.00` はダッシュボードに出ると本物に見える
    （spec-review C-02）。範囲検査では捕まらないことを、しきい値そのもので示す。
    """
    after = samples(scenarios["sensor_reset_85"])[int(300 / INTERVAL_S) :]
    assert after, "300秒目以降のサンプルが無い"
    assert all(sample.channels["rear_exhaust"] == 85.0 for sample in after)
    assert all(sample.err == () for sample in after)
    assert rules.sensor_min_c <= 85.0 <= rules.sensor_max_c, "範囲検査では弾けない値"


@pytest.mark.parametrize(
    ("scenario_name", "at_s", "expected"),
    [
        ("sensor_fail", 120, Quality.MISSING),
        ("sensor_fail_raw", 120, Quality.SUSPECT),
        ("sensor_reset_85", 300, Quality.SUSPECT),
    ],
)
def test_failure_scenarios_reach_the_quality_rules(scenario_name, at_s, expected, scenarios, rules):
    """L0 が出した値が L1 の判定に届くところまで確かめる（#5 の実データ試験）。"""
    last = samples(scenarios[scenario_name])[-1]
    assert classify("air.rear_exhaust", last.channels["rear_exhaust"], rules) is expected
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


def test_power_on_reset_stays_suspect_while_it_is_stuck(scenarios, rules):
    """張り付いている間ずっと `suspect` であること。

    「前サンプルとの差が大きい場合だけ疑う」条件を足すと、最初の1サンプル以外は
    差がゼロなので `ok` に戻り、**故障が続いている間だけ正常扱いになる。**
    無条件で疑っていることを、連続サンプルで固定する。

    5サンプル続くことは `SENSOR_MISSING`（FR-402）の発火条件でもある。
    """
    stuck = samples(scenarios["sensor_reset_85"])[int(300 / INTERVAL_S) :]
    assert len(stuck) >= 5, "FR-402 の5サンプル連続を試すには足りない"
    assert all(
        classify("air.rear_exhaust", sample.channels["rear_exhaust"], rules) is Quality.SUSPECT
        for sample in stuck
    )


# ---------------------------------------------------------------- 時刻（#42）


def test_source_exposes_the_clock_to_use(scenarios):
    """時間基準を決めるのはソース側。デーモンはここから受け取って配る。"""
    clock = SimulatedClock(1_000)
    source = MockSource(scenarios["idle"], clock=clock)
    assert source.clock is clock


def test_store_and_source_share_one_clock_instance(scenarios, rules, tmp_path):
    """**同じ値ではなく同じオブジェクト**であることを見る（#42）。

    `Clock` は型しか縛らないので、取り込みが `SimulatedClock`、保存が実時計、
    という組み合わせでも型検査は通る。合成の起点（#8 のデーモン）が
    1つの時計を配れたかは、`is` でしか確かめられない。
    """
    source = MockSource(scenarios["idle"], clock=SimulatedClock(0))
    with SqliteStore(tmp_path / "shared.db", rules=rules, clock=source.clock) as store:
        assert store.clock is source.clock


def test_host_time_advances_with_the_scenario(scenarios):
    """ホスト受信時刻がシナリオ時間で進む。1サンプルにつき `interval_ms`。"""
    source = MockSource(scenarios["ramp"], sleep=no_sleep, clock=SimulatedClock(0))
    stamps = [
        source.clock.now_ms() for message in source.stream() if isinstance(message, RawSample)
    ]
    assert stamps[0] == 0
    assert stamps[-1] == (len(stamps) - 1) * scenarios["ramp"].interval_ms


def test_compression_does_not_change_host_time(scenarios):
    """`--speed 60` でも生成される時刻は実時間と同一。待ち時間だけが縮む。"""

    def run(speed: float) -> tuple[list[int], float]:
        slept: list[float] = []
        source = MockSource(
            scenarios["ramp"], sleep=slept.append, speed=speed, clock=SimulatedClock(0)
        )
        stamps = [
            source.clock.now_ms() for message in source.stream() if isinstance(message, RawSample)
        ]
        return stamps, sum(slept)

    real_time, real_wait = run(1.0)
    compressed, compressed_wait = run(60.0)
    assert compressed == real_time
    assert compressed_wait == pytest.approx(real_wait / 60)


def test_compressed_replay_still_spans_five_minutes_of_host_time(scenarios):
    """#42 の狙い。FR-404 は `d.intake_rise > 5.0℃` が**5分継続**で発火する。

    ルールエンジン（#18）はまだ無いので、ここでは「閾値を超えてから
    ホスト時刻で5分以上経つこと」と「実際に待った時間はそれよりずっと短いこと」を
    確かめる。ホスト時刻を実時計にすると、この5分が実時間の15秒に潰れる。
    """
    slept: list[float] = []
    source = MockSource(
        scenarios["recirculation"], sleep=slept.append, speed=60.0, clock=SimulatedClock(0)
    )
    crossed_at_ms: int | None = None
    last_ms = 0
    for message in source.stream():
        if not isinstance(message, RawSample):
            continue
        last_ms = source.clock.now_ms()
        rise = message.channels["front_intake"] - message.channels["room_temp"]
        if crossed_at_ms is None and rise > 5.0:
            crossed_at_ms = last_ms

    assert crossed_at_ms is not None, "閾値を超えていない"
    assert last_ms - crossed_at_ms >= 300_000, "ホスト時刻で5分に足りない"
    assert sum(slept) < 30, "実際にはこれだけ短い時間しか待っていない"


def test_shared_clock_keeps_freshly_ingested_data_out_of_stale(scenarios, rules, tmp_path):
    """同じ時計を L0 と L1 が共有すると、圧縮再生でも `stale` が正しく出る。

    保存側だけ実時計にすると、シナリオ上は2.5秒前のサンプルが
    「10秒以上前」と判定されうる（要件 §5.3）。取り込み（#8）は未実装なので、
    ここでは正規化に相当する最小の橋渡しを手で書いて配線だけを確かめる。
    """
    source = MockSource(scenarios["ramp"], sleep=no_sleep, speed=60.0, clock=SimulatedClock(0))
    with SqliteStore(tmp_path / "mock.db", rules=rules, clock=source.clock) as store:
        for message in source.stream():
            if not isinstance(message, RawSample):
                continue
            store.insert_sample(
                Sample(
                    ts_ms=source.clock.now_ms(),
                    readings=(
                        Reading(
                            metric="air.gpu_exhaust",
                            value=message.channels["gpu_exhaust"],
                            quality=Quality.OK,
                        ),
                    ),
                    seq=message.seq,
                    up_ms=message.up,
                )
            )
        latest = store.latest()["air.gpu_exhaust"]
    assert latest.quality is Quality.OK, "直前に入れた値が stale になっている"
    assert latest.age_ms == 0
