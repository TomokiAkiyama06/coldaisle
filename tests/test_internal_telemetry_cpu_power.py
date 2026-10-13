"""CPU Package 電力の adapter（#278 / 決定記録 0122 §2.3）を一時ファイルで検証する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.internal_telemetry import (
    CPU_POWER_SCHEMA_VERSION,
    ConfirmationEvidence,
    ConfirmationStatus,
    CpuPowerAdapter,
    CpuPowerConfig,
    InternalTelemetryConfig,
    SourceStatus,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store import Quality
from coldaisle.telemetry_daemon import periodic_metric_intervals
from conftest import CONFIG_DIR

NOW_MS = 1_790_000_000_000
STALE_AFTER_MS = 5_000
MAX_FUTURE_SKEW_MS = 1_000
METRIC = "power.cpu.package"


def config(path: Path, *, enabled: bool = True) -> CpuPowerConfig:
    return CpuPowerConfig(
        enabled=enabled,
        path=path,
        stale_after_ms=STALE_AFTER_MS,
        max_future_skew_ms=MAX_FUTURE_SKEW_MS,
        metric=METRIC,
        confirmation=ConfirmationEvidence(
            status=ConfirmationStatus.CONFIRMED if enabled else ConfirmationStatus.PROVISIONAL,
            basis="試験の fixture" if enabled else None,
        ),
    )


def adapter(path: Path, *, enabled: bool = True) -> CpuPowerAdapter:
    return CpuPowerAdapter(config(path, enabled=enabled), clock=SimulatedClock(NOW_MS))


def publish(path: Path, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "schema_version": CPU_POWER_SCHEMA_VERSION,
        "ts_ms": NOW_MS - 1_000,
        "watts": 31,
        "window_ms": 1_000,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def only(result) -> tuple[float | None, Quality]:
    (reading,) = result.readings
    assert reading.metric == METRIC
    return reading.value, reading.quality


def test_a_fresh_value_is_stored_in_watts(tmp_path: Path) -> None:
    path = tmp_path / "cpu-package.json"
    publish(path)

    result = adapter(path).poll()

    assert result.status is SourceStatus.OK
    assert only(result) == (31.0, Quality.OK)


def test_a_missing_file_is_missing_not_zero(tmp_path: Path) -> None:
    result = adapter(tmp_path / "absent.json").poll()

    assert result.status is SourceStatus.UNAVAILABLE
    assert only(result) == (None, Quality.MISSING)


@pytest.mark.parametrize(
    "text",
    ["", "{", "not json", "[1, 2]", '"text"', "\xff"],
    ids=["empty", "truncated", "garbage", "list", "string", "binary"],
)
def test_broken_json_is_missing(tmp_path: Path, text: str) -> None:
    path = tmp_path / "cpu-package.json"
    path.write_bytes(text.encode("latin-1"))

    result = adapter(path).poll()

    assert result.status is SourceStatus.UNAVAILABLE
    assert only(result) == (None, Quality.MISSING)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"schema_version": "1"},
        {"schema_version": True},
        {"schema_version": None},
        {"watts": 31.5},
        {"watts": "31"},
        {"watts": True},
        {"watts": -1},
        {"watts": None},
        {"ts_ms": "now"},
        {"ts_ms": 1.5},
    ],
)
def test_an_unexpected_shape_is_missing(tmp_path: Path, overrides: dict[str, Any]) -> None:
    path = tmp_path / "cpu-package.json"
    publish(path, **overrides)

    assert only(adapter(path).poll()) == (None, Quality.MISSING)


def test_a_file_without_watts_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "cpu-package.json"
    path.write_text(json.dumps({"schema_version": 1, "ts_ms": NOW_MS}), encoding="utf-8")

    assert only(adapter(path).poll()) == (None, Quality.MISSING)


def test_an_old_value_is_stale_and_keeps_the_value(tmp_path: Path) -> None:
    """publisher が止まってもファイルは残る。古さで stale と分かる。"""
    path = tmp_path / "cpu-package.json"
    publish(path, ts_ms=NOW_MS - STALE_AFTER_MS - 1)

    result = adapter(path).poll()

    assert result.status is SourceStatus.DEGRADED
    assert only(result) == (31.0, Quality.STALE)


def test_the_stale_boundary_is_still_ok(tmp_path: Path) -> None:
    path = tmp_path / "cpu-package.json"
    publish(path, ts_ms=NOW_MS - STALE_AFTER_MS)

    assert only(adapter(path).poll()) == (31.0, Quality.OK)


def test_a_value_beyond_the_future_skew_is_missing(tmp_path: Path) -> None:
    """時計の巻き戻しの後、止まった値を新しいまま扱い続けない（0122 §2.3）。"""
    path = tmp_path / "cpu-package.json"
    publish(path, ts_ms=NOW_MS + MAX_FUTURE_SKEW_MS + 1)

    result = adapter(path).poll()

    assert result.status is SourceStatus.UNAVAILABLE
    assert only(result) == (None, Quality.MISSING)


def test_a_value_within_the_future_skew_is_ok(tmp_path: Path) -> None:
    path = tmp_path / "cpu-package.json"
    publish(path, ts_ms=NOW_MS + MAX_FUTURE_SKEW_MS)

    assert only(adapter(path).poll()) == (31.0, Quality.OK)


def test_zero_watts_is_a_valid_value(tmp_path: Path) -> None:
    path = tmp_path / "cpu-package.json"
    publish(path, watts=0)

    assert only(adapter(path).poll()) == (0.0, Quality.OK)


def test_disabled_reads_nothing(tmp_path: Path) -> None:
    reader = adapter(tmp_path / "absent.json", enabled=False)

    assert reader.expected_metrics == ()
    result = reader.poll()
    assert result.status is SourceStatus.DISABLED
    assert result.readings == ()


def test_enabling_requires_a_confirmed_basis(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="confirmed"):
        CpuPowerConfig(
            enabled=True,
            path=tmp_path / "cpu-package.json",
            stale_after_ms=STALE_AFTER_MS,
            max_future_skew_ms=MAX_FUTURE_SKEW_MS,
            metric=METRIC,
            confirmation=ConfirmationEvidence(status=ConfirmationStatus.PROVISIONAL),
        )


@pytest.mark.parametrize(
    "field,value",
    [("stale_after_ms", 0), ("max_future_skew_ms", -1), ("metric", "cpu.package")],
)
def test_config_rejects_invalid_values(tmp_path: Path, field: str, value: object) -> None:
    raw: dict[str, Any] = {
        "enabled": False,
        "path": str(tmp_path / "cpu-package.json"),
        "stale_after_ms": STALE_AFTER_MS,
        "max_future_skew_ms": MAX_FUTURE_SKEW_MS,
        "metric": METRIC,
        "confirmation": {"status": "provisional"},
    }
    raw[field] = value

    with pytest.raises(ValidationError):
        CpuPowerConfig.model_validate(raw)


def test_production_config_is_enabled_only_with_owner_approval() -> None:
    config = InternalTelemetryConfig.from_yaml(
        CONFIG_DIR / "internal-telemetry.yaml",
        catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
    )

    # 2026-10-14 に所有者が導入先での有効化を承認した（決定記録 0122）
    assert config.cpu_power.enabled is True
    assert config.cpu_power.confirmation.status is ConfirmationStatus.CONFIRMED
    assert config.cpu_power.confirmation.basis is not None
    assert "0122" in config.cpu_power.confirmation.basis
    assert config.cpu_power.path == Path("/run/coldaisle-power/cpu-package.json")
    assert config.cpu_power.stale_after_ms == 2 * config.interval_ms
    assert config.cpu_power.max_future_skew_ms == 1_000
    # 有効なのでロールアップの期待サンプルに入る
    assert periodic_metric_intervals(config)[METRIC] == config.interval_ms


def test_enabled_config_registers_the_rollup_period() -> None:
    loaded = InternalTelemetryConfig.from_yaml(
        CONFIG_DIR / "internal-telemetry.yaml",
        catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
    )
    enabled = loaded.model_copy(update={"cpu_power": config(Path("/nonexistent"), enabled=True)})

    assert periodic_metric_intervals(enabled)[METRIC] == loaded.interval_ms


def test_the_metric_unit_must_be_watts(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics.yaml"
    text = (CONFIG_DIR / "metrics.yaml").read_text(encoding="utf-8")
    replaced = text.replace('power.cpu.package:   {unit: "W"', 'power.cpu.package:   {unit: "C"')
    assert replaced != text
    metrics.write_text(replaced, encoding="utf-8")

    with pytest.raises(ValueError, match=r"power\.cpu\.package"):
        InternalTelemetryConfig.from_yaml(
            CONFIG_DIR / "internal-telemetry.yaml", catalog=MetricCatalog.from_yaml(metrics)
        )
