"""Workload Hint の単調性ゲートの仕様・部品の digest・証明書の型（0152 段 1b / 決定記録 0156）。

守りたいこと:

1. ``fan-policy.yaml`` v13 / ``safety.yaml`` v5（Control Config v17）。旧版を補完しない。塊は省ける
2. **形**の誤り（未知の鍵・型・``ConfigValue`` の欄の欠落）だけが Control Config の不正。値の制約は
   ゲートの仕様の関数が ``eps_bounds_invalid`` / ``hint_gate_invalid`` にする（0156 §2.6 / §5 の 3）
3. ゲートの仕様は設定だけから1つに決まり、正準な形（並び・``-0.0``）が書き方の揺れで変わらない。
   ``status`` / ``basis`` と上限は digest に入らない（0156 §2.7）
4. ``hint_policy`` / ``optimizer`` / ``conditions`` は束ねるものが変われば digest が変わる
   （``optimizer`` は ``safety.yaml`` の ``zone_min_demand`` / ``ramp_down_per_s`` を含む。
   0156 §2.5）
5. 証明書の ``certificate_sha256`` は束ねる欄のどれが変わっても変わり、読み手は形の誤りを
   閉じた理由で拒む
6. 証明書・ゲートの仕様の module を AI 層・API・eventd・control_admin・Safety 側が import しない
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle import hint_certificate as cert
from coldaisle.control.config import (
    CONTROL_CONFIG_VERSION,
    FAN_POLICY_CONFIG_VERSION,
    SAFETY_CONFIG_VERSION,
    ControlConfig,
)
from coldaisle.control.hint_gate import (
    HintGateRejection,
    HintGateSpecError,
    derive_hint_gate_spec,
    hint_policy_sha256,
    optimizer_sha256,
)
from coldaisle.control.model_registry import (
    HINT_CERTIFICATE_FEATURE_SCHEMA_VERSION,
    HINT_CERTIFICATE_TARGET_SCHEMA_VERSION,
)
from coldaisle.hint_certificate_digest import conditions_sha256
from test_control_config import provisional, valid_documents, write_documents
from test_workload_hint_config import observe_block

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"


def gate_block(**overrides: object) -> dict[str, Any]:
    block: dict[str, Any] = {
        "eps": provisional(0.01),
        "eps_c": provisional(0.5),
        "previous_demand": {"points": [0.2, 0.5, 0.9]},
        "temperature": {
            "cpu": {"points": [-10.0, -2.0, 0.0, 5.0]},
            "gpu": {"points": [-10.0, 0.0]},
        },
        "window_features": [
            {"metric": "gpu.0.power", "points": [50.0, 300.0]},
            {"metric": "cpu.power", "points": [20.0]},
        ],
        "safety_floors": [
            {"front": 0.2, "rear": 0.2, "top": 0.2},
            {"front": 0.6, "rear": 0.3, "top": 0.4},
        ],
        "baselines": [{"front": 0.4, "rear": 0.5, "top": 0.3}],
    }
    block.update(overrides)
    return block


def limits_block(**overrides: object) -> dict[str, Any]:
    block: dict[str, Any] = {
        "max_eps": provisional(0.05),
        "max_eps_c": provisional(1.0),
        "max_eps_c_ceiling": provisional(2.0),
    }
    block.update(overrides)
    return block


OVERRIDES = [
    {"regime": "transient_gpu", "workload": "training", "context": "sustained_gpu"},
    {"regime": "transient_cpu", "workload": "inference_service", "context": "sustained_cpu"},
]


def documents(
    *,
    gate: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    overrides: list[dict[str, Any]] | None = None,
    with_gate: bool = True,
    with_limits: bool = True,
) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = valid_documents()  # type: ignore[assignment]
    hint = observe_block(
        stage="shadow_mpc",
        context_overrides=copy.deepcopy(OVERRIDES if overrides is None else overrides),
    )
    if with_gate:
        hint["monotonicity_gate"] = gate if gate is not None else gate_block()
    docs["fan-policy.yaml"]["supervisor"]["workload_hint"] = hint
    if with_limits:
        docs["safety.yaml"]["workload_hint"] = limits if limits is not None else limits_block()
    return docs


def load(tmp_path: Path, docs: dict[str, dict[str, Any]]) -> ControlConfig:
    write_documents(tmp_path, docs)  # type: ignore[arg-type]
    return ControlConfig.from_directory(tmp_path)


def derive(config: ControlConfig) -> cert.HintGateSpec:
    return derive_hint_gate_spec(
        config.policy.supervisor.workload_hint, config.safety.workload_hint
    )


def rejection(tmp_path: Path, docs: dict[str, dict[str, Any]]) -> HintGateRejection:
    config = load(tmp_path, docs)
    with pytest.raises(HintGateSpecError) as caught:
        derive(config)
    return caught.value.reason


# ---------------------------------------------------------------- 設定の版と形


def test_versions_were_raised() -> None:
    assert FAN_POLICY_CONFIG_VERSION == 13
    assert SAFETY_CONFIG_VERSION == 5
    assert CONTROL_CONFIG_VERSION == 17


@pytest.mark.parametrize(("name", "old"), [("fan-policy.yaml", 12), ("safety.yaml", 4)])
def test_the_previous_versions_are_not_read_as_the_new_ones(
    tmp_path: Path, name: str, old: int
) -> None:
    docs = valid_documents()
    docs[name]["schema_version"] = old
    with pytest.raises(ValidationError, match="schema_version"):
        load(tmp_path, docs)  # type: ignore[arg-type]


def test_both_blocks_can_be_omitted(tmp_path: Path) -> None:
    config = load(tmp_path, valid_documents())  # type: ignore[arg-type]
    assert config.policy.supervisor.workload_hint.monotonicity_gate is None
    assert config.safety.workload_hint is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["fan-policy.yaml"]["supervisor"]["workload_hint"]["monotonicity_gate"].update(
            unknown=1
        ),
        lambda d: d["fan-policy.yaml"]["supervisor"]["workload_hint"]["monotonicity_gate"].pop(
            "baselines"
        ),
        lambda d: d["fan-policy.yaml"]["supervisor"]["workload_hint"]["monotonicity_gate"][
            "eps"
        ].pop("status"),
        lambda d: d["fan-policy.yaml"]["supervisor"]["workload_hint"]["monotonicity_gate"][
            "previous_demand"
        ].update(points="0.5"),
        lambda d: d["fan-policy.yaml"]["supervisor"]["workload_hint"]["monotonicity_gate"][
            "safety_floors"
        ][0].update(front="high"),
        lambda d: d["safety.yaml"]["workload_hint"].update(unknown=1),
        lambda d: d["safety.yaml"]["workload_hint"].pop("max_eps_c_ceiling"),
        lambda d: d["safety.yaml"]["workload_hint"]["max_eps"].update(value="0.1"),
    ],
)
def test_shape_errors_reject_the_control_config(tmp_path: Path, mutate: Any) -> None:
    docs = documents()
    mutate(docs)
    with pytest.raises(ValidationError):
        load(tmp_path, docs)


def test_value_errors_load_and_are_left_to_the_gate_function(tmp_path: Path) -> None:
    """非有限・負値・昇順でない点は読み込みを通す（全 zone Max にしない。0156 §2.6）。"""
    docs = documents(
        gate=gate_block(eps=provisional(float("inf")), previous_demand={"points": [0.9, 0.2]}),
        limits=limits_block(max_eps=provisional(float("nan")), max_eps_c=provisional(-1.0)),
    )
    config = load(tmp_path, docs)
    assert config.policy.supervisor.workload_hint.monotonicity_gate is not None


def test_eps_and_limits_are_listed_as_provisional_values(tmp_path: Path) -> None:
    paths = {item.path for item in load(tmp_path, documents()).provisional_values()}
    assert {
        "supervisor.workload_hint.monotonicity_gate.eps",
        "supervisor.workload_hint.monotonicity_gate.eps_c",
        "workload_hint.max_eps",
        "workload_hint.max_eps_c",
        "workload_hint.max_eps_c_ceiling",
    } <= paths


# ---------------------------------------------------------------- ゲートの仕様


def test_the_spec_is_derived_from_the_config(tmp_path: Path) -> None:
    spec = derive(load(tmp_path, documents()))
    domain = spec.certified_domain
    assert (
        spec.spec_version == cert.HINT_GATE_SPEC_VERSION == HINT_CERTIFICATE_FEATURE_SCHEMA_VERSION
    )
    assert (spec.eps, spec.eps_c) == (0.01, 0.5)
    assert [(key.regime, key.workload) for key in domain.hint_keys] == [
        ("transient_cpu", "inference_service"),
        ("transient_gpu", "training"),
    ]
    assert (domain.previous_demand.minimum, domain.previous_demand.maximum) == (0.2, 0.9)
    assert (domain.temperature_cpu.minimum, domain.temperature_cpu.maximum) == (-10.0, 5.0)
    assert [feature.metric for feature in domain.window_features] == ["cpu.power", "gpu.0.power"]
    assert domain.safety_floor_range.front.model_dump() == {"minimum": 0.2, "maximum": 0.6}
    assert domain.baseline_range.rear.model_dump() == {"minimum": 0.5, "maximum": 0.5}
    # 鍵 2 × 直前 Demand 3³ × CPU 4 × GPU 2 × window feature 1 × 2 × floor 2 × baseline 1
    assert spec.grid_points == 2 * 27 * 4 * 2 * 1 * 2 * 2 * 1


def test_the_spec_is_one_value_for_one_config(tmp_path: Path) -> None:
    first = derive(load(tmp_path, documents()))
    second = derive(load(tmp_path, documents()))
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.sha256() == second.sha256()
    assert json.loads(first.canonical_bytes())["spec_version"] == "hint-gate-spec-v1"


def test_the_written_order_of_keys_and_features_does_not_matter(tmp_path: Path) -> None:
    base = derive(load(tmp_path, documents())).sha256()
    gate = gate_block()
    gate["window_features"].reverse()
    reordered = documents(gate=gate, overrides=list(reversed(OVERRIDES)))
    assert derive(load(tmp_path, reordered)).sha256() == base


def test_the_order_of_the_representative_sets_matters(tmp_path: Path) -> None:
    base = derive(load(tmp_path, documents())).sha256()
    gate = gate_block()
    gate["safety_floors"].reverse()
    assert derive(load(tmp_path, documents(gate=gate))).sha256() != base


def test_negative_zero_is_zero(tmp_path: Path) -> None:
    base = derive(load(tmp_path, documents())).sha256()
    gate = gate_block()
    gate["temperature"]["cpu"]["points"] = [-10.0, -2.0, -0.0, 5.0]
    assert derive(load(tmp_path, documents(gate=gate))).sha256() == base


def test_status_basis_and_limits_are_not_in_the_digest(tmp_path: Path) -> None:
    base = derive(load(tmp_path, documents())).sha256()
    confirmed = {"value": 0.01, "status": "confirmed", "basis": "Stage A の実測"}
    widened = limits_block(max_eps=provisional(0.09), max_eps_c_ceiling=provisional(3.0))
    docs = documents(gate=gate_block(eps=confirmed), limits=widened)
    assert derive(load(tmp_path, docs)).sha256() == base


@pytest.mark.parametrize(
    "gate",
    [
        gate_block(eps=provisional(0.02)),
        gate_block(eps_c=provisional(0.25)),
        gate_block(previous_demand={"points": [0.2, 0.5, 1.0]}),
        gate_block(temperature={"cpu": {"points": [-10.0]}, "gpu": {"points": [-10.0, 0.0]}}),
        gate_block(window_features=[{"metric": "cpu.power", "points": [20.0]}]),
        gate_block(baselines=[{"front": 0.4, "rear": 0.5, "top": 0.35}]),
    ],
)
def test_any_change_of_the_grid_or_eps_changes_the_digest(
    tmp_path: Path, gate: dict[str, Any]
) -> None:
    base = derive(load(tmp_path, documents())).sha256()
    assert derive(load(tmp_path, documents(gate=gate))).sha256() != base


def test_a_missing_gate_block_is_hint_gate_invalid(tmp_path: Path) -> None:
    assert rejection(tmp_path, documents(with_gate=False)) is HintGateRejection.HINT_GATE_INVALID


def test_a_missing_limits_block_is_eps_bounds_invalid(tmp_path: Path) -> None:
    assert rejection(tmp_path, documents(with_limits=False)) is HintGateRejection.EPS_BOUNDS_INVALID


INF = float("inf")
NAN = float("nan")


@pytest.mark.parametrize(
    ("gate", "limits"),
    [
        # 1. 上限が有限・非負
        ({}, {"max_eps": provisional(INF)}),
        ({}, {"max_eps_c": provisional(NAN)}),
        ({}, {"max_eps_c_ceiling": provisional(-1.0)}),
        # 2. max_eps < 1、max_eps_c <= 天井
        ({}, {"max_eps": provisional(1.0)}),
        ({}, {"max_eps_c": provisional(2.5)}),
        # 3. eps / eps_c が有限・非負で上限以下
        ({"eps": provisional(NAN)}, {}),
        ({"eps": provisional(-0.01)}, {}),
        ({"eps_c": provisional(INF)}, {"max_eps_c_ceiling": provisional(INF)}),
        ({"eps": provisional(0.06)}, {}),
        ({"eps_c": provisional(1.5)}, {}),
    ],
)
def test_eps_bounds(tmp_path: Path, gate: dict[str, Any], limits: dict[str, Any]) -> None:
    docs = documents(gate=gate_block(**gate), limits=limits_block(**limits))
    assert rejection(tmp_path, docs) is HintGateRejection.EPS_BOUNDS_INVALID


def test_the_limits_are_checked_before_the_grid(tmp_path: Path) -> None:
    docs = documents(
        gate=gate_block(previous_demand={"points": []}),
        limits=limits_block(max_eps=provisional(INF)),
    )
    assert rejection(tmp_path, docs) is HintGateRejection.EPS_BOUNDS_INVALID


def test_eps_on_the_bounds_is_accepted(tmp_path: Path) -> None:
    docs = documents(
        gate=gate_block(eps=provisional(0.05), eps_c=provisional(2.0)),
        limits=limits_block(max_eps_c=provisional(2.0)),
    )
    assert derive(load(tmp_path, docs)).eps == 0.05


@pytest.mark.parametrize(
    "gate",
    [
        gate_block(previous_demand={"points": []}),
        gate_block(previous_demand={"points": [0.5, 0.5]}),
        gate_block(previous_demand={"points": [0.5, 0.2]}),
        gate_block(previous_demand={"points": [0.5, 1.1]}),
        gate_block(previous_demand={"points": [NAN]}),
        gate_block(temperature={"cpu": {"points": [INF]}, "gpu": {"points": [0.0]}}),
        gate_block(window_features=[{"metric": "cpu.power", "points": []}]),
        gate_block(window_features=[{"metric": "Not A Metric", "points": [1.0]}]),
        gate_block(
            window_features=[
                {"metric": "cpu.power", "points": [1.0]},
                {"metric": "cpu.power", "points": [2.0]},
            ]
        ),
        gate_block(safety_floors=[]),
        gate_block(baselines=[]),
        gate_block(baselines=[{"front": 0.4, "rear": 0.5, "top": 1.2}]),
        gate_block(
            safety_floors=[
                {"front": 0.2, "rear": 0.2, "top": 0.2},
                {"front": 0.2, "rear": 0.2, "top": 0.2},
            ]
        ),
    ],
)
def test_grid_errors_are_hint_gate_invalid(tmp_path: Path, gate: dict[str, Any]) -> None:
    assert rejection(tmp_path, documents(gate=gate)) is HintGateRejection.HINT_GATE_INVALID


def test_no_override_keys_means_zero_points(tmp_path: Path) -> None:
    assert rejection(tmp_path, documents(overrides=[])) is HintGateRejection.HINT_GATE_INVALID


def test_the_error_message_does_not_carry_the_values(tmp_path: Path) -> None:
    config = load(tmp_path, documents(gate=gate_block(previous_demand={"points": [0.123456789]})))
    config_bad = load(
        tmp_path, documents(gate=gate_block(previous_demand={"points": [0.123456789, 7.654321]}))
    )
    assert derive(config).grid_points > 0
    with pytest.raises(HintGateSpecError) as caught:
        derive(config_bad)
    assert "7.654321" not in str(caught.value)


def test_the_spec_rejects_a_grid_points_that_does_not_follow_the_domain(tmp_path: Path) -> None:
    spec = derive(load(tmp_path, documents()))
    raw = json.loads(spec.canonical_bytes())
    raw["grid_points"] += 1
    with pytest.raises(ValidationError, match="grid_points"):
        cert.HintGateSpec.model_validate_json(json.dumps(raw))


# ---------------------------------------------------------------- 設定から選ぶ部品の digest


def test_hint_policy_follows_the_contexts_and_the_overrides(tmp_path: Path) -> None:
    base = hint_policy_sha256(load(tmp_path, documents()).policy.supervisor)
    reordered = load(tmp_path, documents(overrides=list(reversed(OVERRIDES))))
    assert hint_policy_sha256(reordered.policy.supervisor) == base

    fewer = load(tmp_path, documents(overrides=OVERRIDES[:1]))
    assert hint_policy_sha256(fewer.policy.supervisor) != base

    docs = documents()
    contexts = docs["fan-policy.yaml"]["supervisor"]["rule_policy"]["contexts"]
    # 鍵に使っていない context でも基準の全体として束ねる（0152 §2.3）
    contexts["idle"] = copy.deepcopy(contexts["idle"])
    contexts["idle"]["target_band"]["cpu_temperature"]["upper_c"] = 74.0
    docs["fan-policy.yaml"]["supervisor"]["output_bounds"]["target_bands"].append(
        contexts["idle"]["target_band"]
    )
    assert hint_policy_sha256(load(tmp_path, docs).policy.supervisor) != base


def _optimizer(tmp_path: Path, docs: dict[str, dict[str, Any]]) -> str:
    config = load(tmp_path, docs)
    return optimizer_sha256(config.policy, config.safety)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["fan-policy.yaml"]["mpc"]["optimizer"].update(max_evaluations=provisional(32)),
        lambda d: d["fan-policy.yaml"]["mpc"]["optimizer"]["zone_bounds"]["top"].update(
            floor=provisional(0.25)
        ),
        lambda d: d["fan-policy.yaml"]["mpc"]["optimizer"]["cost_scales"].update(
            temperature_c=provisional(4.0)
        ),
        lambda d: d["fan-policy.yaml"]["mpc"].update(
            budget_ms=d["fan-policy.yaml"]["mpc"]["budget_ms"] - 1
        ),
        lambda d: d["safety.yaml"]["zone_min_demand"]["front"].update(value=0.45),
        lambda d: d["safety.yaml"]["ramp_down_per_s"].update(value=0.04),
    ],
)
def test_optimizer_follows_the_solve_settings_and_the_safety_inputs(
    tmp_path: Path, mutate: Any
) -> None:
    base = _optimizer(tmp_path, documents())
    docs = documents()
    mutate(docs)
    assert _optimizer(tmp_path, docs) != base


def test_optimizer_does_not_follow_unrelated_safety_values(tmp_path: Path) -> None:
    base = _optimizer(tmp_path, documents())
    docs = documents(limits=limits_block(max_eps=provisional(0.04)))
    assert _optimizer(tmp_path, docs) == base


def test_conditions_binds_the_whole_mapping() -> None:
    base = conditions_sha256({"binding": {"a": 1}, "acoustic": None})
    assert conditions_sha256({"acoustic": None, "binding": {"a": 1}}) == base
    assert conditions_sha256({"binding": {"a": 2}, "acoustic": None}) != base
    assert conditions_sha256({"binding": {"a": 1}, "acoustic": None, "new": 0}) != base
    with pytest.raises(ValueError):
        conditions_sha256({"x": float("nan")})


def test_component_digests_are_distinct_for_the_same_json() -> None:
    from coldaisle.hint_certificate_digest import json_component_sha256

    assert json_component_sha256(b"a", {}) != json_component_sha256(b"b", {})
    with pytest.raises(ValueError):
        json_component_sha256(b"", {})


# ---------------------------------------------------------------- 証明書


def _hex(char: str) -> str:
    return char * 64


def components(**overrides: str) -> cert.HintCertificateComponents:
    values = {name: _hex(str(index)) for index, name in enumerate(cert.COMPONENT_NAMES)}
    values.update(overrides)
    return cert.HintCertificateComponents(**values)


THERMAL = cert.CertifiedThermalModel(
    kind="thermal_model", model_id="thermal-a", version="1.0.0", artifact_sha256=_hex("a")
)


def a_certificate(tmp_path: Path) -> tuple[cert.HintGateSpec, cert.HintCertificate]:
    spec = derive(load(tmp_path, documents()))
    body = cert.HintCertificateBody.from_spec(
        thermal_model=THERMAL, components=components(hint_gate=spec.sha256()), spec=spec
    )
    return spec, cert.HintCertificate.issue(body)


def test_the_certificate_writes_the_spec_as_it_is(tmp_path: Path) -> None:
    spec, issued = a_certificate(tmp_path)
    assert issued.schema_version == HINT_CERTIFICATE_TARGET_SCHEMA_VERSION
    assert issued.result == "passed"
    assert issued.certified_domain == spec.certified_domain
    assert (issued.eps, issued.eps_c, issued.grid_points) == (
        spec.eps,
        spec.eps_c,
        spec.grid_points,
    )
    assert issued.certificate_sha256 == cert.certificate_sha256(issued.body())


def test_the_hint_gate_component_must_be_the_spec_digest(tmp_path: Path) -> None:
    spec = derive(load(tmp_path, documents()))
    with pytest.raises(ValueError, match="hint_gate"):
        cert.HintCertificateBody.from_spec(
            thermal_model=THERMAL, components=components(), spec=spec
        )


def test_the_certificate_round_trips(tmp_path: Path) -> None:
    _, issued = a_certificate(tmp_path)
    data = issued.to_json_bytes()
    parsed = cert.parse_hint_certificate(data, max_bytes=len(data))
    assert parsed == issued
    assert parsed.body().canonical_bytes() == issued.body().canonical_bytes()


@pytest.mark.parametrize("name", cert.COMPONENT_NAMES)
def test_every_component_changes_the_certificate_hash(tmp_path: Path, name: str) -> None:
    _, issued = a_certificate(tmp_path)
    changed = issued.body().model_copy(
        update={"components": issued.components.model_copy(update={name: _hex("f")})}
    )
    assert cert.certificate_sha256(changed) != issued.certificate_sha256


@pytest.mark.parametrize(
    "update",
    [
        {"model_id": "thermal-b"},
        {"version": "1.0.1"},
        {"artifact_sha256": _hex("b")},
    ],
)
def test_the_thermal_model_changes_the_certificate_hash(
    tmp_path: Path, update: dict[str, str]
) -> None:
    _, issued = a_certificate(tmp_path)
    changed = issued.body().model_copy(update={"thermal_model": THERMAL.model_copy(update=update)})
    assert cert.certificate_sha256(changed) != issued.certificate_sha256


def test_the_domain_and_eps_change_the_certificate_hash(tmp_path: Path) -> None:
    _, issued = a_certificate(tmp_path)
    body = issued.body()
    for update in ({"eps": 0.02}, {"eps_c": 0.25}):
        assert cert.certificate_sha256(body.model_copy(update=update)) != issued.certificate_sha256
    other = derive(
        load(
            tmp_path,
            documents(gate=gate_block(baselines=[{"front": 0.1, "rear": 0.1, "top": 0.1}])),
        )
    )
    moved = body.model_copy(update={"certified_domain": other.certified_domain})
    assert cert.certificate_sha256(moved) != issued.certificate_sha256


def _raw(tmp_path: Path) -> dict[str, Any]:
    _, issued = a_certificate(tmp_path)
    loaded: dict[str, Any] = json.loads(issued.to_json_bytes())
    return loaded


def _reason(data: bytes, max_bytes: int = 1 << 20) -> str:
    with pytest.raises(cert.HintCertificateError) as caught:
        cert.parse_hint_certificate(data, max_bytes=max_bytes)
    return caught.value.reason


def test_unreadable_certificates(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    data = json.dumps(raw).encode()
    assert _reason(data, max_bytes=len(data) - 1) == "certificate_unreadable"
    assert _reason(b"\xff\xfe") == "certificate_unreadable"
    assert _reason(b"{not json") == "certificate_unreadable"
    assert _reason(b"[]") == "certificate_unreadable"
    duplicated = data[:-1] + b', "result": "passed"}'
    assert _reason(duplicated) == "certificate_unreadable"
    assert _reason(data.replace(b'"eps": 0.01', b'"eps": NaN')) == "certificate_unreadable"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update(schema_version="hint-certificate-v2"),
        lambda raw: raw.update(result="failed"),
        lambda raw: raw.pop("result"),
        lambda raw: raw.update(extra=1),
        lambda raw: raw.update(grid_points=raw["grid_points"] + 1),
        lambda raw: raw["components"].pop("calibration"),
        lambda raw: raw["components"].update(source_tree="F" * 64),
        lambda raw: raw["thermal_model"].update(kind="supervisor_policy"),
        lambda raw: raw["certified_domain"]["hint_keys"].reverse(),
        lambda raw: raw["certified_domain"]["previous_demand"].update(minimum=0.0),
        lambda raw: raw.update(eps=-0.1),
    ],
)
def test_invalid_certificates(tmp_path: Path, mutate: Any) -> None:
    raw = _raw(tmp_path)
    mutate(raw)
    assert _reason(json.dumps(raw).encode()) == "certificate_invalid"


def test_the_reader_does_not_trust_or_check_the_stored_hash(tmp_path: Path) -> None:
    """読み手は形だけを見る。合否は期待する本体から計算した値との比較で決める（0152 §2.4 の 6）。"""
    raw = _raw(tmp_path)
    raw["certificate_sha256"] = _hex("0")
    parsed = cert.parse_hint_certificate(json.dumps(raw).encode(), max_bytes=1 << 20)
    assert parsed.certificate_sha256 != cert.certificate_sha256(parsed.body())


def test_the_invalid_message_does_not_carry_the_values(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    raw["thermal_model"]["artifact_sha256"] = "secret-value"
    with pytest.raises(cert.HintCertificateError) as caught:
        cert.parse_hint_certificate(json.dumps(raw).encode(), max_bytes=1 << 20)
    assert "secret-value" not in str(caught.value)


# ---------------------------------------------------------------- 境界（0152 §2.8）


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


@pytest.mark.parametrize(
    "package",
    [
        "ai",
        "api",
        "event_entry",
        "control_admin",
        "control/safety",
        "control/reactive",
        "control/fallback",
        "control/hardware",
    ],
)
def test_llm_api_eventd_admin_and_safety_do_not_import_the_certificate_modules(
    package: str,
) -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted((SRC / package).rglob("*.py"))
        if any(
            name.startswith(("coldaisle.hint_certificate", "coldaisle.control.hint_gate"))
            for name in _imports(path)
        )
    ]
    assert offenders == []


def test_the_certificate_module_does_not_import_control() -> None:
    imported = _imports(SRC / "hint_certificate.py")
    assert not any(name.startswith("coldaisle.control") for name in imported)


def test_the_gate_module_does_not_import_safety_guard_fallback_or_hardware() -> None:
    imported = _imports(SRC / "control" / "hint_gate.py")
    forbidden = tuple(
        f"coldaisle.control.{name}" for name in ("safety", "reactive", "fallback", "hardware")
    )
    assert not any(name.startswith(forbidden) for name in imported)
