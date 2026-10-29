"""評価の CLI ``coldaisle-thermal-evaluate``（#84 / 決定記録 0172 §2.1 / §2.2 / §2.8 / §2.9）。

実際に ``coldaisle-train --apply`` で candidate を登録し、その bytes を評価する。実機は要らない。

- 未来が action に依存する合成の系で学習した artifact は ``pass``
- 同じ入力から同じ bytes。評価は壁時計を読まない
- 読み取りだけ: Registry の bytes が変わらず、``--out`` の1ファイルのほかに何も作らない
- 時間のリークを閉じる照合: 境界のずれ・別の split の artifact・別の dataset・空の test は拒否し、
  ``--out`` を作らない
- import の約束（0150 §2.1 と同じ形）と、どこからも import されないこと（安全の境界。0172 §2.8）
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from coldaisle import thermal_evaluate, train
from coldaisle.store import QualityRules
from coldaisle.thermal_evaluation_report import offline_evaluation_ref, parse_report_bytes
from test_train_cli import log_lines, registry_state, snapshot
from test_train_inputs import SRC, _forbidden, _importers_of, direct_imports
from thermal_evaluation_world import (
    END_MS,
    EVALUATION_CONFIG,
    TEST_START_MS,
    TRAINING_SPEC,
    VALIDATION_START_MS,
    evaluate_argv,
    make_world,
)
from train_world import World

KEY = "thermal_model/rack-thermal/0.1.0"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory, rules: QualityRules) -> World:
    """candidate ``rack-thermal@0.1.0`` を登録した世界（試験は ``--out`` を別の名前にする）。"""
    built = make_world(tmp_path_factory.mktemp("thermal-cli") / "w", rules)
    assert train.main(built.apply_argv(built.outputs / "train-0.1.0")) == 0
    return built


def errors_of(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [
        line
        for line in log_lines(capsys.readouterr().err)
        if line["logger"] == "coldaisle.thermal_evaluate" and line["level"] == "error"
    ]


def refused(world: World, out: Path, capsys: pytest.CaptureFixture[str], **overrides: str) -> str:
    """拒否されて ``--out`` が無く、Registry が変わらないこと。理由の文を返す。"""
    capsys.readouterr()
    before = registry_state(world)
    assert thermal_evaluate.main(evaluate_argv(world, out, **overrides)) == 1
    assert not out.exists()
    assert registry_state(world) == before
    [error] = errors_of(capsys)
    message: str = error["msg"]
    return message


def train_version(world: World, version: str, spec: str) -> None:
    path = world.root / f"training-{version}.yaml"
    path.write_text(spec, encoding="utf-8")
    argv = world.apply_argv(
        world.outputs / f"train-{version}",
        **{"--model-version": version, "--training-spec": str(path)},
    )
    assert train.main(argv) == 0


# ---------------------------------------------------------------- 成功・自己一致・読み取りだけ


def test_a_model_trained_on_an_action_dependent_system_passes(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = world.outputs / "pass.json"
    before = registry_state(world)
    capsys.readouterr()
    assert thermal_evaluate.main(evaluate_argv(world, out)) == 0
    assert registry_state(world) == before
    payload = out.read_bytes()
    report = parse_report_bytes(payload)
    assert report.gate.outcome == "pass", report.gate.reasons
    record = snapshot(world).artifacts[KEY]
    assert report.artifact.sha256 == record.metadata.sha256
    assert report.artifact.registry_status == "candidate"
    assert report.dataset.alias == world.dataset.name
    assert report.split.test.count == report.evaluation.test_examples > 0
    assert (
        report.inputs.config_sha256 == hashlib.sha256(EVALUATION_CONFIG.encode("utf-8")).hexdigest()
    )
    # 実際の action 列の予測は hold_effective の予測よりずっと当たる（報告だけ）
    for row in report.evaluation.action_effect.outputs:
        assert row.recorded_mae is not None and row.held_mae is not None
        assert row.recorded_mae < row.held_mae
    # support の中の集計（報告だけ）と、Profile の residual で割った誤差（報告だけ）
    assert any(stratum.axis == "action_support" for stratum in report.evaluation.strata)
    assert all(output.normalized is not None for output in report.evaluation.overall.outputs)
    [info] = [
        line
        for line in log_lines(capsys.readouterr().err)
        if line["logger"] == "coldaisle.thermal_evaluate"
    ]
    assert info["outcome"] == "pass"
    assert info["offline_evaluation_ref"] == offline_evaluation_ref(payload)


def test_same_inputs_give_the_same_bytes(world: World) -> None:
    first, second = world.outputs / "same-a.json", world.outputs / "same-b.json"
    assert thermal_evaluate.main(evaluate_argv(world, first)) == 0
    assert thermal_evaluate.main(evaluate_argv(world, second)) == 0
    assert first.read_bytes() == second.read_bytes()


def test_the_evaluation_does_not_read_the_wall_clock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = world.outputs / "clock.json"
    assert thermal_evaluate.main(evaluate_argv(world, out)) == 0
    args = thermal_evaluate.build_parser().parse_args(
        evaluate_argv(world, world.outputs / "unused.json")
    )
    inputs = thermal_evaluate.read_inputs(args)

    def forbidden(*_args: object) -> float:
        raise AssertionError("壁時計を読んだ")

    for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter"):
        monkeypatch.setattr(time, name, forbidden)
    payload = thermal_evaluate.build_report(args, inputs)
    monkeypatch.undo()
    assert payload == out.read_bytes()


def test_only_the_out_file_is_created(world: World) -> None:
    before = sorted(path.relative_to(world.root) for path in world.root.rglob("*"))
    out = world.outputs / "only.json"
    assert thermal_evaluate.main(evaluate_argv(world, out)) == 0
    after = sorted(path.relative_to(world.root) for path in world.root.rglob("*"))
    assert sorted(set(after) - set(before)) == [out.relative_to(world.root)]


def test_an_existing_out_is_refused_and_kept(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = world.outputs / "existing.json"
    out.write_bytes(b"keep\n")
    capsys.readouterr()
    assert thermal_evaluate.main(evaluate_argv(world, out)) == 1
    assert out.read_bytes() == b"keep\n"
    [error] = errors_of(capsys)
    assert error["stage"] == "before_write"


# ---------------------------------------------------------------- 時間のリークを閉じる照合


@pytest.mark.parametrize("flag", ["--validation-start-ms", "--test-start-ms"])
def test_a_boundary_shifted_by_one_ms_is_refused(
    world: World, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    value = VALIDATION_START_MS if flag == "--validation-start-ms" else TEST_START_MS
    message = refused(world, world.outputs / f"shift{flag}.json", capsys, **{flag: str(value + 1)})
    assert "split" in message


def test_an_artifact_trained_on_another_split_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    train_version(
        world, "0.2.0", TRAINING_SPEC.replace(str(TEST_START_MS), str(TEST_START_MS + 4_000))
    )
    message = refused(
        world, world.outputs / "other-split.json", capsys, **{"--artifact": "rack-thermal@0.2.0"}
    )
    assert "split の digest" in message


def test_an_empty_test_is_refused(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    beyond = TEST_START_MS + END_MS
    train_version(world, "0.3.0", TRAINING_SPEC.replace(str(TEST_START_MS), str(beyond)))
    message = refused(
        world,
        world.outputs / "empty.json",
        capsys,
        **{"--artifact": "rack-thermal@0.3.0", "--test-start-ms": str(beyond)},
    )
    assert "test が空" in message


def test_another_dataset_is_refused(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    other = world.dataset.parent / ("dataset-" + "b" * 32)
    if not other.exists():
        shutil.copytree(world.dataset, other)
    message = refused(
        world, world.outputs / "other-dataset.json", capsys, **{"--dataset": str(other)}
    )
    assert "artifact を学習した dataset ではない" in message


def test_an_unregistered_artifact_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    message = refused(
        world, world.outputs / "unknown.json", capsys, **{"--artifact": "rack-thermal@9.9.9"}
    )
    assert "登録されていない" in message


def test_a_config_whose_outputs_differ_from_the_target_schema_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = world.root / "other-outputs.yaml"
    path.write_text(
        EVALUATION_CONFIG.replace("metric: air.gpu_exhaust", "metric: gpu.0.core"), encoding="utf-8"
    )
    message = refused(
        world, world.outputs / "other-outputs.json", capsys, **{"--evaluation-config": str(path)}
    )
    assert "target schema と一致しない" in message


def test_a_malformed_config_is_refused_before_the_registry_is_read(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = world.root / "malformed.yaml"
    path.write_text(EVALUATION_CONFIG + "unknown_key: 1\n", encoding="utf-8")
    capsys.readouterr()
    out = world.outputs / "malformed.json"
    assert (
        thermal_evaluate.main(evaluate_argv(world, out, **{"--evaluation-config": str(path)})) == 1
    )
    assert not out.exists()
    [error] = errors_of(capsys)
    assert error["stage"] == "inputs"


# ---------------------------------------------------------------- import の約束（0172 §2.1 / §2.8）

_ALLOWED_PREFIXES = ("coldaisle.control.model.",)
_ALLOWED = {
    "coldaisle",
    "coldaisle.logs",
    "coldaisle.control.evaluation.stats",
    "coldaisle.control.model_registry",
    "coldaisle.control.schema",
    "coldaisle.dataset",
    "coldaisle.ingest.calibration",
    "coldaisle.measurement",
    "coldaisle.metrics",
    "coldaisle.thermal_evaluation_report",
    "coldaisle.training_output",
}


def _allowed(module: str, names: tuple[str, ...]) -> bool:
    top = module.split(".")[0]
    if top != "coldaisle":
        return top in sys.stdlib_module_names or top in ("pydantic", "yaml")
    if module == "coldaisle":
        return set(names) <= {"logs"}
    return module in _ALLOWED or module.startswith(_ALLOWED_PREFIXES)


def test_thermal_evaluate_imports_only_the_allowed_modules() -> None:
    for module, names in direct_imports(SRC / "thermal_evaluate.py"):
        # 禁止の一覧は 0150 §2.1 と同じものに coldaisle.train を足す
        assert not _forbidden(module), module
        assert module != "coldaisle.train" and not module.startswith("coldaisle.train."), module
        assert _allowed(module, names), module


def test_thermal_evaluation_report_imports_no_coldaisle_module() -> None:
    for module, _names in direct_imports(SRC / "thermal_evaluation_report.py"):
        top = module.split(".")[0]
        assert top != "coldaisle", module
        assert top in sys.stdlib_module_names or top in ("pydantic", "yaml"), module


def test_nothing_imports_thermal_evaluate() -> None:
    assert _importers_of("coldaisle.thermal_evaluate") == []


def test_only_the_evaluation_cli_imports_the_report_module() -> None:
    # 段 2 で coldaisle-registry（registry.py）が加わる（0172 §2.7）
    assert _importers_of("coldaisle.thermal_evaluation_report") == ["thermal_evaluate.py"]


def test_the_report_has_no_wall_clock_field(world: World) -> None:
    out = world.outputs / "fields.json"
    assert thermal_evaluate.main(evaluate_argv(world, out)) == 0
    text = json.dumps(json.loads(out.read_bytes()))
    for word in ("created_at", "generated_at", "evaluated_at", "timestamp"):
        assert word not in text
