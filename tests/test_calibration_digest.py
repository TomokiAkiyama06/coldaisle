"""較正の digest（``calibration-digest-v1``）の性質。決定記録 0096 §2.9。

artifact を通した学習と読み込みの一致・L9 は ``test_thermal_model_v2.py`` が確かめる。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

import coldaisle.ingest.normalize as normalize_module
from coldaisle.calibration_offsets import (
    calibration_applies,
    canonical_offsets_bytes,
    effective_metric_offsets,
)
from coldaisle.channels import METRIC_TO_CHANNEL
from coldaisle.clock import SimulatedClock
from coldaisle.control.model.calibration_digest import (
    RuntimeCalibration,
    calibrated_metrics,
    calibration_digest,
    calibration_digest_bytes,
)
from coldaisle.control.model.counterfactual import DerivedMetricDefinition, MetricBindingEntry
from coldaisle.ingest.calibration import Calibration
from coldaisle.ingest.normalize import Normalizer
from coldaisle.ingest.protocol import RawSample
from coldaisle.store import QualityRules

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

GOLDEN_BYTES = b'{"air.front_intake":0.191,"air.room":0.0}\n'
GOLDEN_SHA256 = (
    "56b6ec56558b020e282e2676fbb096645b3b77dbe422c73f35d6b007eb41add0"  # pragma: allowlist secret
)
"""0096 §2.3 の例。``printf '{"air.front_intake":0.191,"air.room":0.0}\\n' | sha256sum`` の値。"""


def plain(metric: str) -> MetricBindingEntry:
    return MetricBindingEntry(metric=metric, unit="°C", derived=None)


def derived(metric: str, minuend: str, subtrahend: str) -> MetricBindingEntry:
    return MetricBindingEntry(
        metric=metric,
        unit="°C",
        derived=DerivedMetricDefinition(minuend=minuend, subtrahend=subtrahend),
    )


INTAKE_RISE = derived("d.intake_rise", "air.front_intake", "air.room")
EXAMPLE = (INTAKE_RISE, plain("gpu.0.core"))
"""0096 §2.3 の例の metric の集合 ``{d.intake_rise, gpu.0.core}``。"""


def from_file(
    tmp_path: Path, document: dict[str, Any], name: str = "calibration.json"
) -> Calibration:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return Calibration.from_json(path)


# ---------------------------------------------------------------- golden vector


def test_golden_vector_bytes_and_digest() -> None:
    offsets = {"front_intake": 0.191}
    assert calibration_digest_bytes(EXAMPLE, offsets) == GOLDEN_BYTES
    assert calibration_digest(EXAMPLE, offsets) == GOLDEN_SHA256
    assert hashlib.sha256(GOLDEN_BYTES).hexdigest() == GOLDEN_SHA256


# ---------------------------------------------------------------- 決定性


def test_digest_is_deterministic_and_ignores_key_order_and_metadata(tmp_path: Path) -> None:
    first = from_file(
        tmp_path,
        {
            "note": "a",
            "calibrated_at": "2026-10-01T09:00:00+09:00",
            "reference": "mean_of_all",
            "samples": {"front_intake": 10},
            "offsets_c": {"front_intake": 0.191, "room_temp": -0.31, "top_exhaust": 0.2},
        },
        "first.json",
    )
    second = from_file(
        tmp_path,
        {
            "offsets_c": {"top_exhaust": 0.2, "room_temp": -0.31, "front_intake": 0.191},
            "samples": {"front_intake": 99, "room_temp": 3},
            "reference": "other",
            "calibrated_at": None,
            "note": "b",
        },
        "second.json",
    )
    assert calibration_digest(EXAMPLE, first.offsets_c) == calibration_digest(
        EXAMPLE, second.offsets_c
    )
    assert calibration_digest(EXAMPLE, first.offsets_c) == calibration_digest(
        EXAMPLE, first.offsets_c
    )


# ---------------------------------------------------------------- 実効値


@pytest.mark.parametrize("zero", ["0", "0.0", "0.00", "-0.0", "-0"])
def test_missing_channel_and_any_spelling_of_zero_are_the_same(tmp_path: Path, zero: str) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(
        '{"offsets_c": {"front_intake": 0.191, "room_temp": ' + zero + "}}", encoding="utf-8"
    )
    explicit = Calibration.from_json(path)
    missing = {"front_intake": 0.191}
    assert calibration_digest(EXAMPLE, explicit.offsets_c) == calibration_digest(EXAMPLE, missing)
    assert calibration_digest(EXAMPLE, explicit.offsets_c) == GOLDEN_SHA256


def test_negative_zero_is_normalized_in_the_bytes() -> None:
    data = calibration_digest_bytes(EXAMPLE, {"front_intake": 0.191, "room_temp": -0.0})
    assert data == GOLDEN_BYTES
    assert b"-0.0" not in data


def test_offsets_are_not_rounded() -> None:
    value = 0.1 + 0.2  # 0.30000000000000004
    data = calibration_digest_bytes(EXAMPLE, {"front_intake": value})
    assert data == b'{"air.front_intake":0.30000000000000004,"air.room":0.0}\n'


# ---------------------------------------------------------------- 感度


@pytest.mark.parametrize("channel", ["front_intake", "room_temp"])
@pytest.mark.parametrize("direction", [math.inf, -math.inf])
def test_smallest_change_of_a_used_offset_changes_the_digest(
    channel: str, direction: float
) -> None:
    base = {"front_intake": 0.191, "room_temp": -0.31}
    changed = {**base, channel: math.nextafter(base[channel], direction)}
    assert calibration_digest(EXAMPLE, changed) != calibration_digest(EXAMPLE, base)


@pytest.mark.parametrize(
    "channel", ["gpu_intake", "gpu_exhaust", "top_exhaust", "rear_exhaust", "room_humidity"]
)
def test_unused_channels_do_not_change_the_digest(channel: str) -> None:
    base = {"front_intake": 0.191}
    assert calibration_digest(EXAMPLE, {**base, channel: 1.5}) == GOLDEN_SHA256


def test_unknown_channel_keys_are_ignored() -> None:
    assert calibration_digest(EXAMPLE, {"front_intake": 0.191, "not_a_channel": 4.0}) == (
        GOLDEN_SHA256
    )


# ---------------------------------------------------------------- 展開


def test_derived_metric_depends_on_its_operands_not_its_name() -> None:
    renamed = (derived("d.other_name", "air.front_intake", "air.room"), plain("gpu.0.core"))
    assert calibration_digest(renamed, {"front_intake": 0.191}) == GOLDEN_SHA256
    assert calibration_digest((INTAKE_RISE,), {"front_intake": 0.191}) == GOLDEN_SHA256
    assert calibration_digest((INTAKE_RISE,), {"room_temp": 0.5}) != calibration_digest(
        (INTAKE_RISE,), {}
    )


def test_shared_operands_become_one_key() -> None:
    entries = (
        INTAKE_RISE,
        derived("d.gpu_preheat", "air.gpu_intake", "air.room"),
        plain("air.room"),
    )
    data = calibration_digest_bytes(entries, {"front_intake": 0.191})
    assert data == b'{"air.front_intake":0.191,"air.gpu_intake":0.0,"air.room":0.0}\n'


def test_nested_derived_expansion_is_refused() -> None:
    with pytest.raises(ValueError, match="また派生値"):
        calibration_digest((derived("d.x", "d.intake_rise", "air.room"),), {})
    with pytest.raises(ValueError, match="また派生値"):
        calibrated_metrics((derived("d.x", "air.room", "d.intake_rise"),))


# ---------------------------------------------------------------- 湿度と null


def test_humidity_is_excluded_even_when_it_has_an_offset() -> None:
    entries = (plain("air.room_humidity"), plain("air.front_intake"))
    data = calibration_digest_bytes(entries, {"front_intake": 0.191, "room_humidity": 2.0})
    assert data == b'{"air.front_intake":0.191}\n'
    assert calibration_digest((plain("air.room_humidity"),), {"room_humidity": 2.0}) is None


@pytest.mark.parametrize(
    "entries",
    [
        (plain("gpu.0.core"), plain("cpu.package")),
        (plain("gpu.0.core"), plain("air.room_humidity")),
        (derived("d.gpu_rise", "gpu.0.core", "cpu.package"),),
    ],
)
def test_digest_is_null_without_calibrated_metrics(entries: tuple[MetricBindingEntry, ...]) -> None:
    assert calibrated_metrics(entries) == frozenset()
    assert calibration_digest(entries, {"front_intake": 0.191, "room_humidity": 1.0}) is None
    assert calibration_digest_bytes(entries, {}) is None


def test_normalizer_and_digest_share_one_humidity_predicate() -> None:
    # 判定を2か所に書かない（0096 §5 #3）
    assert normalize_module.calibration_applies is calibration_applies
    assert not calibration_applies("air.room_humidity")
    assert calibration_applies("air.room")
    normalizer = Normalizer(
        rules=QualityRules.from_yaml(CONFIG_DIR / "quality.yaml"),
        calibration=Calibration(offsets_c={"room_humidity": 5.0, "room_temp": 0.5}),
        clock=SimulatedClock(1_000),
    )
    sample = normalizer.normalize(
        RawSample(seq=1, up=1_000, channels={"room_temp": 20.0, "room_humidity": 40.0})
    ).sample
    values = {reading.metric: reading.value for reading in sample.readings}
    assert values["air.room_humidity"] == 40.0
    assert values["air.room"] == 20.5


def test_effective_mapping_covers_every_calibrated_channel() -> None:
    mapped = effective_metric_offsets({"front_intake": -0.0, "room_humidity": 3.0})
    assert set(mapped) == {m for m in METRIC_TO_CHANNEL if not m.endswith("_humidity")}
    assert all(value == 0.0 and math.copysign(1.0, value) == 1.0 for value in mapped.values())
    assert canonical_offsets_bytes({"b": 1.0, "a": 0.0}) == b'{"a":0.0,"b":1.0}\n'
    with pytest.raises(ValueError, match="有限でない"):
        effective_metric_offsets({"room_temp": math.inf})


# ---------------------------------------------------------------- runtime の較正の2状態


def test_runtime_calibration_has_two_distinct_states() -> None:
    empty = RuntimeCalibration.available({})
    missing = RuntimeCalibration.unavailable("読めない")
    assert empty.is_available and empty.offsets_c == {}
    assert not missing.is_available and missing.offsets_c is None
    assert empty != missing
    with pytest.raises(ValueError):
        RuntimeCalibration.unavailable("")
    with pytest.raises(ValueError):
        RuntimeCalibration(offsets_c=None, unavailable_reason=None)
    with pytest.raises(ValueError):
        RuntimeCalibration(offsets_c={}, unavailable_reason="x")
    with pytest.raises(ValueError, match="有限でない"):
        RuntimeCalibration.available({"room_temp": math.nan})


def test_runtime_calibration_copies_the_offsets() -> None:
    source = {"front_intake": 0.191}
    runtime = RuntimeCalibration.available(source)
    source["front_intake"] = 9.0
    assert runtime.offsets_c == {"front_intake": 0.191}
    with pytest.raises(TypeError):
        runtime.offsets_c["front_intake"] = 1.0  # type: ignore[index]
