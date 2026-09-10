"""ファームウェアとホストの契約（#11）。

**実機もコンパイラも無くても確かめられることがある。** ここで見張るのは
「スケッチの中身が、ホストが期待する形と食い違っていないか」である。

- 出力の例が `schemas/device_v1.schema.json` を通る（決定記録 0003）
- スケッチが出しうるキーが、例と**ホストのチャネル表**の両方と一致する
- spec-review の対策（C-01 / C-02 / W-01）がスケッチから消えていない

**コンパイルと実測は人が行う**（`firmware/README.md` のチェックリスト）。
ここが緑でも「動く」ことの証明にはならない。**形が合っていることの証明である。**
"""

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from coldaisle.channels import CHANNEL_TO_METRIC
from coldaisle.ingest.protocol import RawHello, RawSample

ROOT = Path(__file__).resolve().parents[1]
SKETCH = ROOT / "firmware" / "coldaisle_sensor" / "coldaisle_sensor.ino"
SAMPLE_OUTPUT = ROOT / "firmware" / "sample_output.jsonl"
SCHEMA = ROOT / "schemas" / "device_v1.schema.json"
SPEC_REVIEW = ROOT / "docs" / "spec-review.md"


@pytest.fixture(scope="module")
def sketch() -> str:
    return SKETCH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def lines() -> list[dict]:
    text = SAMPLE_OUTPUT.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------- 出力の形


def test_sample_output_matches_the_schema(lines, validator):
    """例が契約を通る。**通らない例を README に載せない。**"""
    for index, obj in enumerate(lines, start=1):
        errors = sorted(validator.iter_errors(obj), key=str)
        assert not errors, f"{index} 行目: {errors[0].message}"


def test_sample_output_parses_with_the_host_models(lines):
    """スキーマだけでなく、**ホストのモデルでも読めること。**"""
    for obj in lines:
        if obj["type"] == "hello":
            RawHello.model_validate(obj)
        elif obj["type"] == "s":
            RawSample.model_validate(
                {
                    "v": obj["v"],
                    "type": obj["type"],
                    "seq": obj["seq"],
                    "up": obj["up"],
                    "channels": {
                        key: value
                        for key, value in obj.items()
                        if key not in {"v", "type", "seq", "up", "err"}
                    },
                    "err": tuple(obj.get("err", ())),
                }
            )


def test_the_example_covers_hello_sample_and_log(lines):
    """**起動・正常・異常・ログが全部ある例にする。** 正常だけでは足りない。"""
    assert {obj["type"] for obj in lines} == {"hello", "s", "log"}
    errors = [err for obj in lines for err in obj.get("err", [])]
    for reason in ("-127", "85", "read_failed", "absent", "backoff"):
        assert any(err.endswith(f":{reason}") for err in errors), f"{reason} の例が無い"


# ---------------------------------------------------------------- チャネルの一致


def sketch_channels(sketch: str) -> set[str]:
    """スケッチが宣言しているチャネル名。"""
    names = set(re.findall(r'\{"([a-z_0-9]+)", DS\d_PIN\}', sketch))
    names |= {
        match.group(1)
        for match in re.finditer(
            r'static const char (?:ROOM_\w+)_CHANNEL\[\] = "([a-z_]+)"', sketch
        )
    }
    return names


def test_the_sketch_channels_match_the_host_table(sketch):
    """**デバイスが送る名前とホストの写像表を食い違わせない**（決定記録 0003 §2.5）。

    片方だけ変えると、そのメトリクスが黙って取り込まれなくなる。
    """
    assert sketch_channels(sketch) == set(CHANNEL_TO_METRIC)


def test_every_channel_appears_in_the_example(sketch, lines):
    samples = [obj for obj in lines if obj["type"] == "s"]
    assert samples
    for channel in sketch_channels(sketch):
        assert any(channel in obj for obj in samples), f"例に {channel} が無い"


def test_the_hello_names_the_same_ds_channels(sketch, lines):
    hello = next(obj for obj in lines if obj["type"] == "hello")
    ds_names = set(re.findall(r'\{"([a-z_0-9]+)", DS\d_PIN\}', sketch))
    assert {
        name for name, meta in hello["sensors"].items() if meta["kind"] == "ds18b20"
    } == ds_names


# ---------------------------------------------------------------- ピン割り当て


def spec_review_pins() -> dict[str, int]:
    """spec-review W-01 の表から `D番号 → GPIO` を読む。"""
    pins: dict[str, int] = {}
    for row in re.finditer(
        r"^\|[^|]*\|\s*(D\d+)\s*\|\s*(\d+)\s*\|",
        SPEC_REVIEW.read_text(encoding="utf-8"),
        re.MULTILINE,
    ):
        pins[row.group(1)] = int(row.group(2))
    return pins


def test_the_sketch_uses_the_reviewed_pins(sketch):
    """**検証済みの割り当てから勝手に動かさない**（spec-review W-01）。"""
    assigned = dict(re.findall(r"#define (DS\d_PIN|I2C_SD[AL]|I2C_SCL)\s+(D\d+)", sketch))
    assert assigned == {
        "DS1_PIN": "D0",
        "DS2_PIN": "D1",
        "DS3_PIN": "D2",
        "DS4_PIN": "D3",
        "DS5_PIN": "D8",
        "I2C_SDA": "D4",
        "I2C_SCL": "D5",
    }


def test_d6_is_not_used(sketch):
    """**D6 は GPIO43 = UART0 の TXD。** 1-Wire には使えない（spec-review W-01）。"""
    assert not re.search(r"#define \w+\s+D6\b", sketch)


def test_the_firmware_readme_repeats_the_reviewed_table():
    """配線表が spec-review と `firmware/README.md` の2か所にある。**ずれさせない。**

    ピン番号はスケッチに直接書いてある（ファームウェアは `config/*.yaml` を
    読めない）。その代わり、**表と実装の一致を機械が見張る**ことで
    AGENTS.md ルール6 の意図（唯一の情報源を持つ）を満たす。
    """
    readme = (ROOT / "firmware" / "README.md").read_text(encoding="utf-8")
    in_readme = {
        row.group(1): int(row.group(2))
        for row in re.finditer(r"^\|[^|]*\|\s*(D\d+)\s*\|\s*(\d+)\s*\|", readme, re.MULTILINE)
    }
    reviewed = spec_review_pins()
    assert in_readme, "README に配線表が無い"
    for pin, gpio in in_readme.items():
        assert reviewed.get(pin) == gpio, f"{pin} が spec-review と食い違う"


def test_the_hello_gpio_numbers_match_the_reviewed_table(lines):
    """`hello` の `gpio` が W-01 の表と一致する。"""
    table = spec_review_pins()
    expected = {
        "front_intake": table["D0"],
        "gpu_intake": table["D1"],
        "gpu_exhaust": table["D2"],
        "top_exhaust": table["D3"],
        "rear_exhaust": table["D8"],
    }
    hello = next(obj for obj in lines if obj["type"] == "hello")
    actual = {
        name: meta["gpio"] for name, meta in hello["sensors"].items() if meta["kind"] == "ds18b20"
    }
    assert actual == expected


def test_am2320_has_no_gpio(lines):
    """**I²C は2本なので単一の gpio では表せない**（決定記録 0003 §2.4）。"""
    hello = next(obj for obj in lines if obj["type"] == "hello")
    assert "gpio" not in hello["sensors"]["room"]


# ---------------------------------------------------------------- 対策が消えていないこと


def test_the_conversion_is_non_blocking(sketch):
    """spec-review C-01。**既定のままでは 750ms × 5 で周期が破綻する。**"""
    assert "setWaitForConversion(false)" in sketch


def test_the_resolution_is_eleven_bits(sketch):
    """11bit = 0.125C / 375ms。12bit はセンサー確度に対して過剰（spec-review C-01）。"""
    assert re.search(r"DS_RESOLUTION_BITS\s*=\s*11\b", sketch)


def test_conversions_start_together(sketch):
    """5本が独立 GPIO である利点を使う（spec-review C-01）。"""
    assert re.search(
        r"for .*DS_COUNT.*\n\s*if \(ds_present\[i\]\) \{\n\s*ds\[i\]\.requestTemperatures\(\);",
        sketch,
    )


def test_the_power_on_reset_value_is_rejected(sketch):
    """spec-review C-02。**ちょうど 85.00 は「もっともらしい」ので危険。**"""
    assert re.search(r"DS_POWER_ON_RESET_C\s*=\s*85\.0f", sketch)
    assert '"85"' in sketch


def test_the_disconnected_value_is_rejected(sketch):
    assert re.search(r"DS_DISCONNECTED_C\s*=\s*-127\.0f", sketch)
    assert '"-127"' in sketch


def test_the_i2c_clock_is_slowed_down(sketch):
    """AM2320 は高速モードで不安定（spec-review）。"""
    assert "Wire.setClock(100000)" in sketch


def test_the_watchdog_is_enabled(sketch):
    """**無言で停止させない。** ループが止まったら再起動する。"""
    assert "esp_task_wdt_add(NULL)" in sketch
    assert "esp_task_wdt_reset()" in sketch


def test_the_interval_is_two_and_a_half_seconds(sketch):
    assert re.search(r"INTERVAL_MS\s*=\s*2500\b", sketch)


def test_the_period_is_driven_by_a_deadline(sketch):
    """`delay(INTERVAL)` ではなく締切で刻む。**処理時間ぶん周期が伸びない。**"""
    assert "next_due_ms += INTERVAL_MS" in sketch
    assert not re.search(r"delay\(\s*INTERVAL_MS\s*\)", sketch)


def test_the_uptime_does_not_wrap_in_fifty_days(sketch):
    """`millis()` は約49.7日で巻き戻り、ホストは**再起動として扱う**（FR-106）。"""
    assert "esp_timer_get_time()" in sketch
    assert not re.search(r"\bup\b.*millis\(\)", sketch)


def test_a_truncated_line_is_not_sent(sketch):
    """**壊れた JSON を出さない。** 出すと原因の切り分けが難しくなる。"""
    assert "line_truncated" in sketch


def test_no_sensors_does_not_emit_an_invalid_hello(sketch):
    """スキーマは `sensors` に1つ以上を要求する。**空の hello を出さない。**"""
    assert "no_sensors" in sketch


def test_hello_is_replayed_when_the_host_connects(sketch):
    """**待つだけでは足りない**（#11 のレビュー指摘）。

    デバイスのほうが先に起動していると、誰も読んでいない間に hello が流れて消える。
    ホストはサンプルだけを受け取り、ROM の一覧も `interval_ms` も知らないまま動く。
    """
    assert "host_connected" in sketch
    assert re.search(
        r"if \(connected && !host_connected\) \{" + "\n" + r"\s*send_hello\(\);", sketch
    )


def test_hello_is_not_sent_periodically(sketch):
    """**立ち上がりだけで出す。** 周期的に出すと「電源投入時に1回だけ」が崩れる。"""
    assert sketch.count("send_hello();") == 2  # setup と、接続の立ち上がり


def test_newlines_are_written_without_carriage_return(sketch):
    """`println` は `\\r\\n` を付ける。行末に `\\r` を残さない。"""
    assert "Serial.println" not in sketch
    assert "Serial.write('\\n')" in sketch
