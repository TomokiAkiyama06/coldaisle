"""``fan-policy.yaml`` v12 の ``supervisor.workload_hint``。

#319 / 決定記録 0133 §2.3 / §2.6、0066 §2.1 (a)。

守りたいこと:

1. v11 を v12 として補完しない。``workload_hint`` の塊は必須
2. ``stage: off`` は他の欄を省ける。``off`` 以外は全ての欄が必須（既定値をコードに置かない）
3. ``context_overrides`` の上書き先は、基準より冷却を弱めうる差
   （weight・strategy・band の引き上げ）を1つでも持てば拒否する。鍵の Regime に
   ``unknown`` を置けない
4. YAML の引用符の無い ``off``（偽として読まれる）を ``off`` と読む
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.control.config import (
    CONTROL_CONFIG_VERSION,
    FAN_POLICY_CONFIG_VERSION,
    ControlConfig,
    WorkloadHintConfig,
)
from coldaisle.control.schema import WorkloadHintKind, WorkloadHintStage, WorkloadRegime
from test_control_config import valid_documents, write_documents


def observe_block(**overrides: object) -> dict[str, Any]:
    block: dict[str, Any] = {
        "stage": "observe",
        "grace_ms": 60_000,
        "default_ttl_ms": 600_000,
        "max_age_ms": 3_600_000,
        "contradiction_hold_ms": 60_000,
        "startup_backfill_ms": 300_000,
        "reader_poll_ms": 1_000,
        "reader_stale_after_ms": 5_000,
        "known_sources": ["job-launcher"],
        "context_overrides": [],
    }
    block.update(overrides)
    return block


def documents_with(block: dict[str, Any] | None, *, contexts: dict[str, Any] | None = None):
    documents = valid_documents()
    supervisor: dict[str, Any] = documents["fan-policy.yaml"]["supervisor"]  # type: ignore[assignment]
    if block is None:
        del supervisor["workload_hint"]
    else:
        supervisor["workload_hint"] = block
    if contexts is not None:
        supervisor["rule_policy"]["contexts"].update(contexts)
    return documents


def load(tmp_path: Path, documents: dict[str, Any]) -> ControlConfig:
    write_documents(tmp_path, documents)
    return ControlConfig.from_directory(tmp_path)


def test_versions_were_raised():
    assert FAN_POLICY_CONFIG_VERSION == 12
    assert CONTROL_CONFIG_VERSION == 16


def test_block_is_required(tmp_path):
    with pytest.raises(ValidationError, match="workload_hint"):
        load(tmp_path, documents_with(None))


def test_v11_is_not_filled_in(tmp_path):
    documents = valid_documents()
    documents["fan-policy.yaml"]["schema_version"] = 11
    with pytest.raises(ValidationError):
        load(tmp_path, documents)


def test_unquoted_off_in_yaml_reads_as_off(tmp_path):
    documents = valid_documents()
    write_documents(tmp_path, documents)
    path = tmp_path / "fan-policy.yaml"
    text = path.read_text(encoding="utf-8").replace("stage: 'off'", "stage: off")
    assert "stage: off" in text
    path.write_text(text, encoding="utf-8")
    config = ControlConfig.from_directory(tmp_path)
    assert config.policy.supervisor.workload_hint.stage is WorkloadHintStage.OFF


def test_yaml_true_is_not_guessed_as_a_stage():
    with pytest.raises(ValidationError):
        WorkloadHintConfig.model_validate({"stage": True})


def test_observe_requires_every_value():
    block = observe_block()
    del block["contradiction_hold_ms"]
    with pytest.raises(ValidationError, match="contradiction_hold_ms"):
        WorkloadHintConfig.model_validate(block)


def test_observe_block_loads(tmp_path):
    config = load(tmp_path, documents_with(observe_block()))
    hint = config.policy.supervisor.workload_hint
    assert hint.stage is WorkloadHintStage.OBSERVE
    assert hint.overrides == {}


def test_stale_limit_must_exceed_the_poll_period():
    with pytest.raises(ValidationError, match="reader_stale_after_ms"):
        WorkloadHintConfig.model_validate(
            observe_block(reader_poll_ms=1_000, reader_stale_after_ms=1_000)
        )


@pytest.mark.parametrize("source", ["other", "Upper", "", "a b"])
def test_known_sources_have_the_entry_shape_and_not_other(source):
    with pytest.raises(ValidationError, match="known_sources"):
        WorkloadHintConfig.model_validate(observe_block(known_sources=[source]))


def test_unknown_regime_key_is_rejected():
    """未知の状態をヒントで既知の扱いにしない（0133 §2.3）。"""
    override = {"regime": "unknown", "workload": "training", "context": "idle"}
    with pytest.raises(ValidationError, match="unknown"):
        WorkloadHintConfig.model_validate(observe_block(context_overrides=[override]))


def test_duplicate_keys_are_rejected():
    override = {"regime": "idle", "workload": "training", "context": "idle"}
    with pytest.raises(ValidationError, match="重複"):
        WorkloadHintConfig.model_validate(observe_block(context_overrides=[override, override]))


def lowered_band_contexts(**changes: Any) -> dict[str, Any]:
    """idle の context を基準に、band だけを下げた sustained_gpu の context（と変更）を作る。"""
    base = copy.deepcopy(valid_documents()["fan-policy.yaml"]["supervisor"]["rule_policy"])  # type: ignore[index]
    idle = base["contexts"]["idle"]
    target = copy.deepcopy(idle)
    target["target_band"] = {
        "cpu_temperature": {"lower_c": 40.0, "upper_c": 70.0},
        "gpu_temperature": {"lower_c": 40.0, "upper_c": 72.0},
    }
    for key, value in changes.items():
        target[key] = value
    return {"sustained_gpu": target}


def with_bounds_for(documents: dict[str, Any], contexts: dict[str, Any]) -> None:
    """上書き先の band と strategy を output_bounds に足す（選べる候補にする）。"""
    bounds = documents["fan-policy.yaml"]["supervisor"]["output_bounds"]
    for context in contexts.values():
        if context["target_band"] not in bounds["target_bands"]:
            bounds["target_bands"].append(context["target_band"])
        if context["strategy"] not in bounds["strategies"]:
            bounds["strategies"].append(context["strategy"])


OVERRIDE = {"regime": "idle", "workload": "training", "context": "sustained_gpu"}


def test_lowering_the_band_is_allowed(tmp_path):
    contexts = lowered_band_contexts()
    documents = documents_with(observe_block(context_overrides=[OVERRIDE]), contexts=contexts)
    with_bounds_for(documents, contexts)
    config = load(tmp_path, documents)
    assert config.policy.supervisor.workload_hint.overrides == {
        (WorkloadRegime.IDLE, WorkloadHintKind.TRAINING): WorkloadRegime.SUSTAINED_GPU
    }


@pytest.mark.parametrize(
    "change",
    [
        {"strategy": "conservative"},
        {
            "weights": {
                "gpu_temperature": 1.0,
                "cpu_temperature": 0.8,
                "balance": 0.5,
                "acoustic": 0.4,
                "change": 0.3,
            }
        },
        {
            "target_band": {
                "cpu_temperature": {"lower_c": 50.0, "upper_c": 75.0},
                "gpu_temperature": {"lower_c": 45.0, "upper_c": 78.0},
            }
        },
        {
            "target_band": {
                "cpu_temperature": {"lower_c": 45.0, "upper_c": 75.0},
                "gpu_temperature": {"lower_c": 45.0, "upper_c": 80.0},
            }
        },
    ],
    ids=["strategy", "weights", "lower_c_raised", "upper_c_raised"],
)
def test_anything_that_may_weaken_cooling_is_rejected(tmp_path, change):
    """weight・strategy の差と band の引き上げは、どれも起動しない（0066 §2.1 (a) / (c)）。"""
    contexts = lowered_band_contexts(**change)
    documents = documents_with(observe_block(context_overrides=[OVERRIDE]), contexts=contexts)
    with_bounds_for(documents, contexts)
    with pytest.raises(ValidationError, match="上書き先"):
        load(tmp_path, documents)


def test_overrides_are_checked_even_when_off(tmp_path):
    contexts = lowered_band_contexts(strategy="conservative")
    documents = documents_with({"stage": "off", "context_overrides": [OVERRIDE]}, contexts=contexts)
    with_bounds_for(documents, contexts)
    with pytest.raises(ValidationError, match="上書き先"):
        load(tmp_path, documents)
