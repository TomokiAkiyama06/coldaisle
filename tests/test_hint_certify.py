"""認証ランナー ``coldaisle-hint-certify``（#288 / 決定記録 0152 §2.1 段 2・0156・0157・0160）。

守りたいこと（すべて実機不要。ルール 7）:

1. 小さな格子と本物の Model Registry に登録した試験用 artifact で、合格なら証明書を書き、
   不合格なら書かない（終了コード 1）。証明書の欄はゲートの仕様の出力そのもの・部品は同じ関数の値
2. 格子の点で solve が ``ok`` にならなければ不合格（点を除かない。0160 §2.4 の 2）
3. 温度の metric が window に無い・window の metric に軸が無い・使わない軸がある → 不合格
4. 較正: ``--calibration`` の較正を L9 に通して合格・別の較正 / 省略では束縛できず不合格
5. Registry にも設定にも書かない（Registry の bytes が変わらない）。``--out`` を上書きしない
6. 起動の形が ``-I -m`` でない・``SCHED_IDLE`` に下げられないなら何もせずに終了する
7. ``conditions`` の digest は別の revision・別の authority stage の束縛で一致する（0160 §2.2）
8. controller の公開の入口 ``solve`` は ``propose()`` と同じ解を返し、予算を増やす値を拒む
9. AI 層・API・eventd・control_admin・control がランナーを import しない
"""

from __future__ import annotations

import ast
import copy
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from coldaisle import hint_certify
from coldaisle.control.config import ControlConfig
from coldaisle.control.hint_gate import derive_hint_gate_spec
from coldaisle.control.model.thermal import ObservedFanAction
from coldaisle.control.model_registry import (
    ModelCompatibility,
    ModelRegistry,
    load_model_registry_limits,
)
from coldaisle.control.mpc import LearnedMpcController, MpcModelBinding
from coldaisle.control.schema import (
    AuthorityStage,
    OptimizerStatus,
    PerZone,
    StaticAuthorityStage,
)
from coldaisle.hint_certificate import (
    HINT_CERTIFICATE_SCHEMA_VERSION,
    HintCertificate,
    certificate_sha256,
)
from coldaisle.hint_certificate_digest import LaunchForm, conditions_sha256
from coldaisle.hint_certify import (
    EXIT_CERTIFIED,
    EXIT_INPUT,
    EXIT_NOT_CERTIFIED,
    EXIT_OUTPUT,
    EXIT_REFUSED,
    CertifyInputs,
    certify,
    flat_window,
    main,
)
from test_control_config import provisional, write_documents
from test_hint_gate_spec import documents, gate_block, limits_block
from test_learned_mpc import (
    ALL_STAGES,
    CALIBRATION_OFFSETS,
    CATALOG,
    FEATURES,
    GPU,
    RUNTIME_CALIBRATION,
    MpcArtifact,
    ScriptedClock,
    bind,
    demands,
    make_artifact,
    mpc_policy,
    observed_input,
    optimizer_document,
    propose,
    register_payload,
    register_v2,
    safety,
    supervisor_output,
)
from test_model_confidence import AIR

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
REPO = SRC.parents[1]
METRICS = REPO / "config" / "metrics.yaml"
REGISTRY_LIMITS_DIR = REPO / "config"
TARGETS = (AIR, GPU)
"""試験用 artifact の target。目的関数の温度の metric（CPU / GPU）を window の feature にする。"""
LOWER_BAND = {
    "cpu_temperature": {"lower_c": 40.0, "upper_c": 70.0},
    "gpu_temperature": {"lower_c": 40.0, "upper_c": 73.0},
}
"""ヒントの上書き先 context の band（基準より下げる。0066 §2.1 (a)）。"""
OVERRIDES = [{"regime": "transient_gpu", "workload": "training", "context": "sustained_gpu"}]


def small_gate(**overrides: object) -> dict[str, Any]:
    """小さな格子（2³ × 2 × 2 × 2 × 1 = 64 点）。window feature は温度の2つだけ。"""
    block = gate_block(
        previous_demand={"points": [0.5, 0.9]},
        temperature={"cpu": {"points": [-5.0, 2.0]}, "gpu": {"points": [-5.0, 2.0]}},
        window_features=[],
        safety_floors=[
            {"front": 0.4, "rear": 0.4, "top": 0.5},
            {"front": 0.6, "rear": 0.5, "top": 0.6},
        ],
        baselines=[{"front": 0.6, "rear": 0.6, "top": 0.6}],
    )
    block.update(overrides)
    return block


def certify_documents(
    *,
    gate: dict[str, Any] | None = None,
    optimizer: dict[str, Any] | None = None,
    budget_ms: int = 1_000,
) -> dict[str, dict[str, Any]]:
    docs = documents(gate=gate or small_gate(), overrides=copy.deepcopy(OVERRIDES))
    policy = docs["fan-policy.yaml"]
    policy["mpc"]["optimizer"] = optimizer or optimizer_document(
        cost_metrics={"cpu_temperature": AIR, "gpu_temperature": GPU}
    )
    policy["mpc"]["budget_ms"] = budget_ms
    policy["mpc"]["valid_ms"] = 2 * 1_000 + 1_000 + budget_ms
    policy["mpc"]["max_source_age_ms"] = provisional(2 * 1_000 + 1_000 + budget_ms)
    # 許容幅は1 step（試験用 artifact の 1000 ms）より小さくする（fan-policy の検証）
    policy["shadow"]["outcome_match_tolerance_ms"] = provisional(500)
    supervisor = policy["supervisor"]
    supervisor["output_bounds"]["target_bands"].append(copy.deepcopy(LOWER_BAND))
    lowered = copy.deepcopy(supervisor["rule_policy"]["contexts"]["sustained_gpu"])
    lowered["target_band"] = copy.deepcopy(LOWER_BAND)
    supervisor["rule_policy"]["contexts"]["sustained_gpu"] = lowered
    return docs


@pytest.fixture(scope="module")
def registry_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """window の feature に目的関数の温度の metric を持つ反実仮想 artifact v2 の production。"""
    root = tmp_path_factory.mktemp("hint-certify") / "registry"
    make_artifact(root, features=FEATURES, targets=TARGETS, authority=ALL_STAGES)
    return root


def write_calibration(path: Path, offsets: dict[str, float]) -> Path:
    document = {"note": "試験", "calibrated_at": None, "offsets_c": offsets}
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def inputs(
    tmp_path: Path,
    registry_root: Path,
    *,
    docs: dict[str, dict[str, Any]] | None = None,
    calibration: dict[str, float] | None = CALIBRATION_OFFSETS,
) -> CertifyInputs:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    write_documents(config_dir, docs or certify_documents())  # type: ignore[arg-type]
    lockfile = tmp_path / "uv.lock"
    lockfile.write_bytes(b"version = 1\n")
    return CertifyInputs(
        config_dir=config_dir,
        metrics=METRICS,
        registry_root=registry_root,
        registry_limits=REGISTRY_LIMITS_DIR,
        calibration=(
            None
            if calibration is None
            else write_calibration(tmp_path / "calibration.json", calibration)
        ),
        lockfile=lockfile,
        out=tmp_path / "certificate.json",
    )


def reasons(caplog: pytest.LogCaptureFixture) -> list[object]:
    return [getattr(record, "fields", {}).get("reason") for record in caplog.records]


def failure_fields(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    for record in caplog.records:
        fields = getattr(record, "fields", {})
        if record.name == "coldaisle.hint_certify" and record.levelno >= logging.ERROR:
            return dict(fields)
    raise AssertionError("不合格のログが無い")


def registry_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------- 合格


def test_a_small_grid_passes_and_writes_a_certificate(
    tmp_path: Path, registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    before = registry_bytes(registry_root)
    given = inputs(tmp_path, registry_root)
    assert certify(given) == EXIT_CERTIFIED
    certificate = HintCertificate.model_validate_json(given.out.read_bytes())
    # 証明書の欄はゲートの仕様の出力そのもの（0152 §2.3）
    control = ControlConfig.from_directory(given.config_dir)
    spec = derive_hint_gate_spec(
        control.policy.supervisor.workload_hint, control.safety.workload_hint
    )
    assert certificate.schema_version == HINT_CERTIFICATE_SCHEMA_VERSION
    assert certificate.result == "passed"
    assert certificate.certified_domain == spec.certified_domain
    assert (certificate.eps, certificate.eps_c) == (spec.eps, spec.eps_c)
    assert certificate.grid_points == spec.grid_points == 64
    assert certificate.components.hint_gate == spec.sha256()
    assert certificate.certificate_sha256 == certificate_sha256(certificate.body())
    production = ModelRegistry(registry_root, limits=_limits()).inspect()
    slot = production.production[next(iter(production.production))]
    assert certificate.thermal_model.model_id == slot.active.model_id
    assert certificate.thermal_model.version == slot.active.version
    # Registry にも設定にも書かない
    assert registry_bytes(registry_root) == before
    assert "certified" in reasons(caplog)
    # 起動前に点の数をログに出す（0156 §2.3）
    assert any(getattr(r, "fields", {}).get("grid_points") == 64 for r in caplog.records)


def _limits() -> Any:
    return load_model_registry_limits(REGISTRY_LIMITS_DIR)


def test_a_reduced_grid_does_not_match_the_original_configuration(
    tmp_path: Path, registry_root: Path
) -> None:
    """格子の軸を減らした設定の証明書は、元の設定から導いた仕様と合わない（0152 §2.8）。"""
    reduced = inputs(
        tmp_path,
        registry_root,
        docs=certify_documents(gate=small_gate(previous_demand={"points": [0.5]})),
    )
    assert certify(reduced) == EXIT_CERTIFIED
    certificate = HintCertificate.model_validate_json(reduced.out.read_bytes())
    original = tmp_path / "original"
    original.mkdir()
    write_documents(original, certify_documents())  # type: ignore[arg-type]
    control = ControlConfig.from_directory(original)
    spec = derive_hint_gate_spec(
        control.policy.supervisor.workload_hint, control.safety.workload_hint
    )
    assert certificate.components.hint_gate != spec.sha256()
    assert certificate.certified_domain != spec.certified_domain


# ---------------------------------------------------------------- 不合格


def test_an_unsolved_grid_point_fails_without_a_certificate(
    tmp_path: Path, registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """評価の回数の上限 1 では候補を評価する前に打ち切る（timeout）。点を除かず不合格。"""
    docs = certify_documents(
        optimizer=optimizer_document(
            cost_metrics={"cpu_temperature": AIR, "gpu_temperature": GPU},
            max_evaluations=provisional(1),
        )
    )
    given = inputs(tmp_path, registry_root, docs=docs)
    assert certify(given) == EXIT_NOT_CERTIFIED
    assert not given.out.exists()
    fields = failure_fields(caplog)
    assert fields["reason"] == "grid_point_unsolved"
    assert fields["unsolved_points"] == 64
    assert fields["first_code"] == "optimizer_budget_exhausted"
    assert fields["first_condition"] == "base"
    # 値は載せず軸の位置だけ
    assert fields["first_point"] == {
        "hint_key": 0,
        "previous_demand": [0, 0, 0],
        "temperature_cpu": 0,
        "temperature_gpu": 0,
        "window_features": [],
        "safety_floor": 0,
        "baseline": 0,
    }


def test_a_monotonicity_violation_fails_without_a_certificate(
    tmp_path: Path,
    registry_root: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ヒント有りの解の Demand が基準より eps を超えて下がれば不合格（比較の経路の試験）。"""
    original = LearnedMpcController.solve
    calls = {"count": 0}

    def lowered(self: LearnedMpcController, **kwargs: Any) -> Any:
        calls["count"] += 1
        outcome = original(self, **kwargs)
        if calls["count"] % 2 == 1 or outcome.solution is None:
            return outcome
        solution = outcome.solution
        lower = solution.requested.model_copy(update={"front": solution.requested.front - 0.2})
        plan = solution.plan.model_copy(
            update={
                "steps": tuple(
                    step.model_copy(update={"demands": lower}) for step in solution.plan.steps
                )
            }
        )
        forged = solution.model_construct(**(dict(solution) | {"requested": lower, "plan": plan}))
        return outcome.model_construct(**(dict(outcome) | {"solution": forged}))

    monkeypatch.setattr(LearnedMpcController, "solve", lowered)
    given = inputs(tmp_path, registry_root)
    assert certify(given) == EXIT_NOT_CERTIFIED
    assert not given.out.exists()
    fields = failure_fields(caplog)
    assert fields["reason"] == "monotonicity_violated"
    assert fields["first_check"] == "demand_front"
    assert fields["violated_points"] == 64


def test_a_temperature_metric_outside_the_window_fails(
    tmp_path: Path, registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """目的関数の温度の metric が artifact の window に無ければ不合格（0160 §2.4 の 4）。"""
    root = tmp_path / "registry"
    make_artifact(root, features=(AIR,), targets=TARGETS, authority=ALL_STAGES)
    given = inputs(tmp_path, root)
    assert certify(given) == EXIT_NOT_CERTIFIED
    assert failure_fields(caplog)["reason"] == "temperature_metric_not_in_window"
    assert not given.out.exists()


def test_an_unused_window_feature_axis_fails(
    tmp_path: Path, registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """artifact が使わない metric の軸があれば不合格（書き損じを見逃さない。0156 §2.4）。"""
    gate = small_gate(window_features=[{"metric": "gpu.0.power", "points": [50.0]}])
    given = inputs(tmp_path, registry_root, docs=certify_documents(gate=gate))
    assert certify(given) == EXIT_NOT_CERTIFIED
    fields = failure_fields(caplog)
    assert fields["reason"] == "window_metrics_mismatch"
    assert fields["unused_features"] == 1


def test_a_gate_spec_that_cannot_be_derived_fails(
    tmp_path: Path, registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    docs = certify_documents()
    docs["safety.yaml"]["workload_hint"] = limits_block(max_eps=provisional(0.001))
    given = inputs(tmp_path, registry_root, docs=docs)
    assert certify(given) == EXIT_NOT_CERTIFIED
    assert failure_fields(caplog)["reason"] == "eps_bounds_invalid"


def test_no_production_thermal_model_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    given = inputs(tmp_path, tmp_path / "absent-registry")
    assert certify(given) == EXIT_NOT_CERTIFIED
    assert failure_fields(caplog)["reason"] == "thermal_model_unavailable"
    assert not (tmp_path / "absent-registry").exists()


@pytest.mark.parametrize(
    "calibration", [None, {"front_intake": 0.5, "room_temp": -0.31}], ids=["omitted", "other"]
)
def test_calibration_must_pass_l9(
    tmp_path: Path,
    registry_root: Path,
    caplog: pytest.LogCaptureFixture,
    calibration: dict[str, float] | None,
) -> None:
    """較正つきの artifact は、--calibration を省く・別の較正では束縛できず不合格（0152 §2.1）。"""
    given = inputs(tmp_path, registry_root, calibration=calibration)
    assert certify(given) == EXIT_NOT_CERTIFIED
    fields = failure_fields(caplog)
    assert fields["reason"] == "thermal_model_unbindable"
    assert not given.out.exists()


def test_the_calibration_component_follows_the_calibration(
    tmp_path: Path, registry_root: Path
) -> None:
    """別の offset のチャネル（artifact が使わない）を変えても、部品 calibration は変わる。"""
    (tmp_path / "a").mkdir()
    first = inputs(tmp_path / "a", registry_root)
    assert certify(first) == EXIT_CERTIFIED
    second_dir = tmp_path / "b"
    second_dir.mkdir()
    changed = dict(CALIBRATION_OFFSETS) | {"room_temp": 1.0}
    second = inputs(second_dir, registry_root, calibration=changed)
    assert certify(second) == EXIT_CERTIFIED
    one = HintCertificate.model_validate_json(first.out.read_bytes())
    two = HintCertificate.model_validate_json(second.out.read_bytes())
    assert one.components.calibration != two.components.calibration
    assert one.components.model_dump(exclude={"calibration"}) == two.components.model_dump(
        exclude={"calibration"}
    )


# ---------------------------------------------------------------- 出力と入力


def test_an_existing_out_is_not_overwritten(
    tmp_path: Path, registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    given = inputs(tmp_path, registry_root)
    given.out.write_bytes(b"keep")
    assert certify(given) == EXIT_OUTPUT
    assert given.out.read_bytes() == b"keep"
    assert "out_exists" in reasons(caplog)


def test_an_invalid_control_config_is_an_input_error(
    tmp_path: Path, registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    given = inputs(tmp_path, registry_root)
    (given.config_dir / "safety.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    assert certify(given) == EXIT_INPUT
    assert "control_config_invalid" in reasons(caplog)
    assert not given.out.exists()


def test_an_unreadable_lockfile_is_an_input_error(
    tmp_path: Path, registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    given = inputs(tmp_path, registry_root)
    given.lockfile.unlink()
    assert certify(given) == EXIT_INPUT
    assert "lockfile_unreadable" in reasons(caplog)


def test_flat_window_puts_the_same_values_on_every_frame(registry_root: Path) -> None:
    model = bind(_artifact(registry_root)).model
    schema = model.feature_schema
    action = PerZone[ObservedFanAction](
        front=ObservedFanAction(effective_demand=0.5),
        rear=ObservedFanAction(effective_demand=0.6),
        top=ObservedFanAction(effective_demand=0.7),
    )
    observed = flat_window(schema, {AIR: 70.0, GPU: 75.0}, action)
    assert observed.action_ts_ms == schema.window_ms
    assert [frame.ts_ms for frame in observed.window] == list(
        range(0, schema.window_ms + 1, schema.sample_period_ms)
    )
    for frame in observed.window:
        assert frame.values == {AIR: 70.0, GPU: 75.0}
        assert set(frame.source_ts_ms.values()) == {frame.ts_ms}
        assert not any(frame.missing_mask.values())
        assert not any(frame.stale_mask.values())
        assert not any(frame.suspect_mask.values())
    with pytest.raises(ValueError):
        flat_window(schema, {AIR: 70.0}, action)


def _artifact(root: Path) -> MpcArtifact:
    """fixture の Registry の production（thermal_model）を読み直す。"""
    registry = ModelRegistry(root, limits=_limits())
    slot = next(iter(registry.inspect().production.values()))
    loaded = registry.load_version(
        slot.active,
        ModelCompatibility(
            feature_schema_version="thermal-features-v2",
            target_schema_version="thermal-targets-v1",
            authority_stage=AuthorityStage.SHADOW,
        ),
    )
    assert loaded.artifact is not None
    return MpcArtifact(verified=loaded.artifact, root=root)


# ---------------------------------------------------------------- 入口の前提


def stderr_reasons(capsys: pytest.CaptureFixture[str]) -> list[object]:
    """``main`` は ``logs.configure`` で JSON Lines を stderr へ出す（caplog には残らない）。"""
    return [
        json.loads(line).get("reason")
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{")
    ]


def test_main_refuses_a_launch_form_other_than_isolated_module(
    tmp_path: Path, registry_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lowered: list[bool] = []
    code = main(
        _argv(inputs(tmp_path, registry_root)),
        launch_form=lambda _entry: LaunchForm.OTHER,
        lower_priority=lambda: lowered.append(True) or True,
    )
    assert code == EXIT_REFUSED
    assert lowered == []
    assert "launch_form_not_isolated" in stderr_reasons(capsys)
    assert not (tmp_path / "certificate.json").exists()


def test_main_refuses_when_sched_idle_is_unavailable(
    tmp_path: Path, registry_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        _argv(inputs(tmp_path, registry_root)),
        launch_form=lambda _entry: LaunchForm.ISOLATED_MODULE,
        lower_priority=lambda: False,
    )
    assert code == EXIT_REFUSED
    assert "sched_idle_unavailable" in stderr_reasons(capsys)
    assert not (tmp_path / "certificate.json").exists()


def test_main_runs_the_grid_after_both_checks(tmp_path: Path, registry_root: Path) -> None:
    given = inputs(tmp_path, registry_root)
    code = main(
        _argv(given),
        launch_form=lambda entry: (
            LaunchForm.ISOLATED_MODULE if entry == "coldaisle.hint_certify" else LaunchForm.OTHER
        ),
        lower_priority=lambda: True,
    )
    assert code == EXIT_CERTIFIED
    assert given.out.exists()


def _argv(given: CertifyInputs) -> list[str]:
    argv = [
        "--config-dir",
        str(given.config_dir),
        "--metrics",
        str(given.metrics),
        "--registry-root",
        str(given.registry_root),
        "--registry-limits",
        str(given.registry_limits),
        "--hint-lockfile",
        str(given.lockfile),
        "--out",
        str(given.out),
    ]
    if given.calibration is not None:
        argv += ["--calibration", str(given.calibration)]
    return argv


def _run_module(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_console_style_launch_does_nothing(tmp_path: Path) -> None:
    """``-I`` の無い起動（console script と同じく ``isolated_module`` でない）は何もしない。"""
    out = tmp_path / "certificate.json"
    result = _run_module(
        [
            "-m",
            "coldaisle.hint_certify",
            "--registry-root",
            str(tmp_path / "registry"),
            "--hint-lockfile",
            str(tmp_path / "uv.lock"),
            "--out",
            str(out),
        ],
        tmp_path,
    )
    assert result.returncode == EXIT_REFUSED, result.stderr
    assert "launch_form_not_isolated" in result.stderr + result.stdout
    assert not out.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SCHED_IDLE は Linux だけ")
def test_an_isolated_module_launch_passes_the_checks(tmp_path: Path) -> None:
    """``-I -m`` の起動は起動の形と ``SCHED_IDLE`` を通り、入力を読む段に進む（子だけ下げる）。"""
    out = tmp_path / "certificate.json"
    result = _run_module(
        [
            "-I",
            "-m",
            "coldaisle.hint_certify",
            "--config-dir",
            str(tmp_path / "absent-config"),
            "--registry-root",
            str(tmp_path / "registry"),
            "--hint-lockfile",
            str(tmp_path / "uv.lock"),
            "--out",
            str(out),
        ],
        tmp_path,
    )
    assert result.returncode == EXIT_INPUT, result.stderr
    assert "control_config_invalid" in result.stderr + result.stdout
    assert not out.exists()


# ---------------------------------------------------------------- conditions（0160 §2.2）


def test_conditions_digest_survives_registry_revision_and_authority_stage(
    tmp_path: Path, registry_root: Path
) -> None:
    """本物の controller で、同じ artifact を別の revision・別の stage で束縛しても一致する。"""
    first = _artifact(registry_root).verified
    # 同じ bytes を、先に別の操作がある Registry へ登録する（revision が違う）
    other_root = tmp_path / "other"
    register_v2(other_root, version="0.2.0", promoted=False, features=FEATURES, targets=TARGETS)
    other = register_payload(other_root, first.payload)
    assert other.attestation.artifact_sha256 == first.attestation.artifact_sha256
    assert other.attestation.registry_revision != first.attestation.registry_revision
    policy = mpc_policy(cost_metrics={"cpu_temperature": AIR, "gpu_temperature": GPU})

    def conditions(verified: Any, stage: AuthorityStage) -> dict[str, object]:
        binding = MpcModelBinding.from_verified_artifact(
            verified,
            metric_catalog=CATALOG,
            calibration=RUNTIME_CALIBRATION,
            authority_stage=stage,
            expected_model_version=verified.attestation.version,
        )
        controller = LearnedMpcController(
            binding,
            policy,
            safety(),
            monotonic_ms=ScriptedClock(0),
            authority=StaticAuthorityStage(stage),
        )
        return controller.conditions()

    shadow = conditions(first, AuthorityStage.SHADOW)
    full = conditions(other, AuthorityStage.FULL)
    # conditions() そのものは変えない（revision・stage は値に残る。0160 §2.1）
    assert shadow != full
    assert conditions_sha256(shadow) == conditions_sha256(full)


# ---------------------------------------------------------------- controller の入口（0160 §2.4）


def test_solve_returns_the_same_solution_as_propose(registry_root: Path) -> None:
    artifact = _artifact(registry_root)
    policy = mpc_policy(cost_metrics={"cpu_temperature": AIR, "gpu_temperature": GPU})
    binding = bind(artifact, authority_stage=AuthorityStage.FULL)

    def controller() -> LearnedMpcController:
        return LearnedMpcController(
            binding,
            policy,
            safety(),
            monotonic_ms=ScriptedClock(0),
            authority=StaticAuthorityStage(AuthorityStage.FULL),
        )

    proposed = propose(controller(), baseline=0.4, safety_floor=0.2)
    assert proposed.solution is not None
    supervisor = supervisor_output()
    outcome = controller().solve(
        observed=observed_input(),
        safety_floor=demands(0.2),
        weights=supervisor.weights,
        target_band=supervisor.target_band,
        baseline=demands(0.4),
        max_evaluations=policy.mpc.optimizer.max_evaluations.value,
        deadline_mono_ms=policy.mpc.budget_ms,
    )
    assert outcome.solution is not None
    assert outcome.solution.requested == proposed.solution.requested
    assert outcome.solution.prediction == proposed.solution.prediction
    assert outcome.solution.cost == proposed.solution.cost


@pytest.mark.parametrize(
    ("evaluations", "deadline_extra"),
    [(0, 0), (65, 0), (64, 1)],
    ids=["no-evaluations", "more-evaluations", "later-deadline"],
)
def test_solve_refuses_a_budget_larger_than_the_configuration(
    registry_root: Path, evaluations: int, deadline_extra: int
) -> None:
    artifact = _artifact(registry_root)
    policy = mpc_policy(cost_metrics={"cpu_temperature": AIR, "gpu_temperature": GPU})
    assert policy.mpc.optimizer.max_evaluations.value == 64
    controller = LearnedMpcController(
        bind(artifact, authority_stage=AuthorityStage.FULL),
        policy,
        safety(),
        monotonic_ms=ScriptedClock(0),
        authority=StaticAuthorityStage(AuthorityStage.FULL),
    )
    supervisor = supervisor_output()
    with pytest.raises(ValueError):
        controller.solve(
            observed=observed_input(),
            safety_floor=demands(0.2),
            weights=supervisor.weights,
            target_band=supervisor.target_band,
            baseline=demands(0.4),
            max_evaluations=evaluations,
            deadline_mono_ms=policy.mpc.budget_ms + deadline_extra,
        )


def test_a_narrower_evaluation_limit_stops_earlier(registry_root: Path) -> None:
    artifact = _artifact(registry_root)
    policy = mpc_policy(cost_metrics={"cpu_temperature": AIR, "gpu_temperature": GPU})
    controller = LearnedMpcController(
        bind(artifact, authority_stage=AuthorityStage.FULL),
        policy,
        safety(),
        monotonic_ms=ScriptedClock(0),
        authority=StaticAuthorityStage(AuthorityStage.FULL),
    )
    supervisor = supervisor_output()
    outcome = controller.solve(
        observed=observed_input(),
        safety_floor=demands(0.2),
        weights=supervisor.weights,
        target_band=supervisor.target_band,
        baseline=demands(0.4),
        max_evaluations=1,
        deadline_mono_ms=policy.mpc.budget_ms,
    )
    assert outcome.status is OptimizerStatus.TIMEOUT
    assert outcome.evaluations == 1
    # 打ち切りの時刻が過ぎていれば、モデルを1回も評価せずに timeout
    expired = LearnedMpcController(
        bind(artifact, authority_stage=AuthorityStage.FULL),
        policy,
        safety(),
        monotonic_ms=ScriptedClock(100),
        authority=StaticAuthorityStage(AuthorityStage.FULL),
    )
    late = expired.solve(
        observed=observed_input(),
        safety_floor=demands(0.2),
        weights=supervisor.weights,
        target_band=supervisor.target_band,
        baseline=demands(0.4),
        max_evaluations=64,
        deadline_mono_ms=50,
    )
    assert late.status is OptimizerStatus.TIMEOUT
    assert late.evaluations == 0


# ---------------------------------------------------------------- 境界


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
    "package", ["ai", "api", "event_entry", "control_admin", "control", "learned_worker"]
)
def test_nothing_on_the_llm_or_control_side_imports_the_runner(package: str) -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted((SRC / package).rglob("*.py"))
        if any(name.startswith("coldaisle.hint_certify") for name in _imports(path))
    ]
    assert offenders == []


def test_the_runner_does_not_import_safety_guard_fallback_hardware_or_llm() -> None:
    imported = _imports(SRC / "hint_certify.py")
    forbidden = (
        *(f"coldaisle.control.{name}" for name in ("safety", "reactive", "fallback", "hardware")),
        "coldaisle.ai",
        "coldaisle.api",
        "coldaisle.store",
        "subprocess",
        "sqlite3",
    )
    assert not any(name.startswith(forbidden) for name in imported)


def test_the_runner_never_writes_the_registry() -> None:
    """Registry の書き込みの操作を呼ばない（登録・昇格は人が行う。0152 §2.2）。"""
    tree = ast.parse((SRC / "hint_certify.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & {"register_candidate", "mark_validated", "promote", "rollback", "retire"}


def test_the_console_script_and_module_entry_are_declared() -> None:
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'coldaisle-hint-certify = "coldaisle.hint_certify:main"' in pyproject
    assert hint_certify.build_parser().prog == "coldaisle-hint-certify"
    assert hint_certify.ENTRY_MODULE == "coldaisle.hint_certify"
