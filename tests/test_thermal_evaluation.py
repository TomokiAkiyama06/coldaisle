"""反実仮想 Thermal Model の offline 評価の計算と gate（#84 / 決定記録 0172 §2.3〜§2.6 / §2.9）。

予測器を差し替えて、評価の数字と gate の向きを固定する。example は合成の系の公開した Dataset v2
（``tests/thermal_evaluation_world.py``）の test を使い、値だけを書き換える。実機は要らない。

- 合否の向き: 予測が当たる model は ``pass``・persistence が完全な系は ``skill`` で ``blocked``・
  一様に低くずらした model は ``underprediction`` で ``blocked``（MAE と検定だけなら通る）
- 除外と対: 欠測の target・stale の persistence・予測できない example を理由ごとに数え、
  model と persistence は同じ対で比べる
- 層: 下限未満の層は ``insufficient`` で合否に効かず、十分な層で persistence より悪いと ``blocked``
- 検定: 符号検定の p 値の golden（分数で正確）と、``d = 0`` のブロックを数から除くこと
- 設定: 既定値を持たず、未知の鍵・``status`` の欠け・重複した鍵を拒否する
- 報告: canonical な bytes・``conditions_sha256``・gate の作り直し・``offline_evaluation_ref`` の形
"""

from __future__ import annotations

import json
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from coldaisle import thermal_evaluate
from coldaisle.control.model.counterfactual import ActionTrajectory, ThermalActionSchema
from coldaisle.control.model.counterfactual_training import load_training_dataset_artifact_v2
from coldaisle.control.model.dataset import DatasetExampleV2, split_temporally_v2
from coldaisle.control.model.thermal import ObservedThermalInput, PredictedTarget
from coldaisle.measurement import Quality
from coldaisle.store import QualityRules
from coldaisle.thermal_evaluation_report import (
    ArtifactSection,
    DatasetSection,
    InputsSection,
    OutputKey,
    SplitSection,
    SplitWindow,
    ThermalEvaluationConfig,
    ThermalEvaluationReport,
    canonical_report_bytes,
    conditions_sha256,
    evaluate_gate,
    is_offline_evaluation_ref,
    offline_evaluation_ref,
    parse_evaluation_config,
    parse_report_bytes,
    sign_test_p_value,
)
from thermal_evaluation_world import (
    EVALUATION_CONFIG,
    TARGET,
    TEST_START_MS,
    VALIDATION_START_MS,
    make_world,
)

ACTION_SCHEMA = ThermalActionSchema(step_ms=1_000, steps=2)
OUTPUTS = (
    OutputKey(horizon_ms=1_000, metric=TARGET),
    OutputKey(horizon_ms=2_000, metric=TARGET),
)
SHA = "0" * 64

Predictor = Callable[[ObservedThermalInput, ActionTrajectory], tuple[PredictedTarget, ...]]


# ---------------------------------------------------------------- 道具


@pytest.fixture(scope="module")
def examples(
    tmp_path_factory: pytest.TempPathFactory, rules: QualityRules
) -> tuple[tuple[DatasetExampleV2, ...], tuple[DatasetExampleV2, ...]]:
    """(train, test)。"""
    world = make_world(tmp_path_factory.mktemp("thermal-eval") / "w", rules)
    dataset = load_training_dataset_artifact_v2(world.dataset).dataset
    split = split_temporally_v2(
        dataset.examples, validation_start_ms=VALIDATION_START_MS, test_start_ms=TEST_START_MS
    )
    assert len(split.test) >= 20
    return split.train, split.test


def config(**gate_updates: Any) -> ThermalEvaluationConfig:
    """試験の設定（``gate_updates`` は ``"minimum.stratum_examples": 20`` の形で値を替える）。"""
    import yaml

    document = yaml.safe_load(EVALUATION_CONFIG)
    for dotted, value in gate_updates.items():
        node = document
        *parents, last = dotted.split(".")
        for name in parents:
            node = node[name]
        if isinstance(node[last], dict) and "value" in node[last]:
            node[last]["value"] = value
        else:
            node[last] = value
    return parse_evaluation_config(yaml.safe_dump(document).encode("utf-8"))


def truth(example: DatasetExampleV2, horizon_ms: int) -> float:
    target = next(item for item in example.targets if item.horizon_ms == horizon_ms)
    value = target.values[TARGET]
    # 欠測の target でも予測器は何かを返す（評価の側が除外する）
    return 45.0 if value is None else value


def last_value(example: DatasetExampleV2) -> float:
    value = example.window[-1].values[TARGET]
    assert value is not None
    return value


def predictor_from(
    examples: tuple[DatasetExampleV2, ...],
    value: Callable[[DatasetExampleV2, int], float],
    *,
    failing: frozenset[int] = frozenset(),
) -> Predictor:
    """``action_ts_ms`` で example を引き、``value(example, horizon)`` を予測として返す。"""
    by_ts = {example.action_ts_ms: example for example in examples}

    def predict(
        observed: ObservedThermalInput, trajectory: ActionTrajectory
    ) -> tuple[PredictedTarget, ...]:
        if observed.action_ts_ms in failing:
            raise ValueError("予測できない")
        example = by_ts[observed.action_ts_ms]
        return tuple(
            PredictedTarget(
                horizon_ms=horizon,
                expected_ts_ms=observed.action_ts_ms + horizon,
                values={TARGET: value(example, horizon)},
            )
            for horizon in (1_000, 2_000)
        )

    return predict


def build(
    test: tuple[DatasetExampleV2, ...],
    train: tuple[DatasetExampleV2, ...],
    predictor: Predictor,
    cfg: ThermalEvaluationConfig,
) -> ThermalEvaluationReport:
    evaluation = thermal_evaluate.evaluate_test_split(
        test,
        train=train,
        predictor=predictor,
        action_schema=ACTION_SCHEMA,
        outputs=OUTPUTS,
        residual_scales=(),
        config=cfg,
        block_origin_ms=TEST_START_MS,
        support=None,
    )
    window = SplitWindow(count=0, start_ms=None, end_ms=None)
    payload = thermal_evaluate.assemble_report(
        artifact=ArtifactSection(
            model_id="rack-thermal",
            version="0.1.0",
            sha256=SHA,
            confidence_profile_sha256=SHA,
            registry_status="candidate",
        ),
        dataset=DatasetSection(
            alias="dataset-" + "a" * 32, manifest_sha256=SHA, examples_sha256=SHA
        ),
        split=SplitSection(
            validation_start_ms=VALIDATION_START_MS,
            test_start_ms=TEST_START_MS,
            split_sha256=SHA,
            train=window,
            validation=window,
            test=window,
            purged=0,
        ),
        inputs=InputsSection(config_sha256=SHA, metrics_config_sha256=SHA, calibration_sha256=SHA),
        outputs=OUTPUTS,
        evaluation=evaluation,
        config=cfg,
    )
    return parse_report_bytes(payload)


def first_blocks_end(test: tuple[DatasetExampleV2, ...], count: int) -> int:
    """example の入る最初の ``count`` 個の時間ブロック（4 s。起点は test_start_ms）の終わり。"""
    blocks = sorted({(example.action_ts_ms - TEST_START_MS) // 4_000 for example in test})
    return TEST_START_MS + (blocks[count - 1] + 1) * 4_000


def edit(example: DatasetExampleV2, change: Callable[[dict[str, Any]], None]) -> DatasetExampleV2:
    document = example.model_dump(mode="python")
    change(document)
    return DatasetExampleV2.model_validate(document)


def constant_exhaust(document: dict[str, Any]) -> None:
    for frame in document["window"]:
        if frame["values"][TARGET] is not None:
            frame["values"][TARGET] = 45.0
    for target in document["targets"]:
        target["values"][TARGET] = 45.0


def wiggle(example: DatasetExampleV2, horizon: int) -> float:
    """当たる model（±0.05 の誤差。符号は時刻で交互）。"""
    return truth(example, horizon) + (0.05 if example.action_ts_ms % 2_000 else -0.05)


# ---------------------------------------------------------------- 合否の向き


def test_a_model_that_tracks_the_action_passes(examples: Any) -> None:
    train, test = examples
    report = build(test, train, predictor_from(test, wiggle), config())
    assert report.gate.outcome == "pass", report.gate.reasons
    [h1, h2] = report.evaluation.overall.outputs
    assert h1.paired is not None and h2.paired is not None
    assert h1.paired.model.mae == pytest.approx(0.05)
    assert h2.paired.persistence.mae == pytest.approx(4.0)
    assert h1.paired.sign_test.persistence_better == 0
    assert h1.train_mean is not None


def test_a_system_where_persistence_is_perfect_is_blocked_at_skill(examples: Any) -> None:
    train, test = examples
    flat = tuple(edit(example, constant_exhaust) for example in test)
    report = build(flat, train, predictor_from(flat, wiggle), config())
    assert report.gate.outcome == "blocked"
    assert report.gate.failed_stage == "skill"
    assert any("persistence の MAE が 0" in reason for reason in report.gate.reasons)


def test_a_model_shifted_low_is_blocked_at_underprediction_though_mae_would_pass(
    examples: Any,
) -> None:
    train, test = examples

    def low(example: DatasetExampleV2, horizon: int) -> float:
        return truth(example, horizon) - 0.8

    predictor = predictor_from(test, low)
    strict = build(test, train, predictor, config(**{"gate.underprediction.maximum_c": 0.5}))
    assert strict.gate.outcome == "blocked"
    assert strict.gate.failed_stage == "underprediction"
    # MAE と検定だけなら通る（underprediction の上限を緩めた同じ報告は pass）
    [h1, h2] = strict.evaluation.overall.outputs
    for output in (h1, h2):
        assert output.paired is not None
        assert output.paired.model.mae < output.paired.persistence.mae
        assert output.paired.sign_test.persistence_better == 0
        assert output.model is not None
        assert output.model.underprediction_max == pytest.approx(0.8)
        assert output.model.bias == pytest.approx(-0.8)
    relaxed = config(**{"gate.underprediction.maximum_c": 1.0})
    assert evaluate_gate(strict, relaxed).outcome == "pass"


def test_too_few_examples_or_blocks_is_blocked_at_evidence(examples: Any) -> None:
    train, test = examples
    report = build(test[:3], train, predictor_from(test, wiggle), config())
    assert report.gate.failed_stage == "evidence"
    assert any("test の example が下限" in reason for reason in report.gate.reasons)
    assert any("時間ブロックが下限" in reason for reason in report.gate.reasons)


def test_a_config_whose_outputs_differ_from_the_target_schema_is_blocked(examples: Any) -> None:
    train, test = examples
    report = build(test, train, predictor_from(test, wiggle), config())
    other = config(outputs=[{"metric": "gpu.0.core", "direction": "higher_is_worse"}])
    gate = evaluate_gate(report, other)
    assert gate.failed_stage == "evidence"
    assert any("target schema と一致しない" in reason for reason in gate.reasons)


# ---------------------------------------------------------------- 除外と対


def test_missing_targets_stale_persistence_and_failed_predictions_are_counted(
    examples: Any,
) -> None:
    train, test = examples

    def drop_target(document: dict[str, Any]) -> None:
        target = document["targets"][0]
        target["values"][TARGET] = None
        target["source_ts_ms"][TARGET] = None
        target["quality"][TARGET] = None
        target["missing_mask"][TARGET] = True

    def stale_last(document: dict[str, Any]) -> None:
        frame = document["window"][-1]
        frame["stale_mask"][TARGET] = True
        frame["quality"][TARGET] = Quality.STALE

    edited = (edit(test[0], drop_target), edit(test[1], stale_last), *test[2:])
    failing = frozenset({test[2].action_ts_ms})
    report = build(edited, train, predictor_from(edited, wiggle, failing=failing), config())
    evaluation = report.evaluation
    assert evaluation.test_examples == len(test)
    assert evaluation.predicted_examples == len(test) - 1
    assert evaluation.excluded_examples == {"prediction": 1}
    [h1, h2] = evaluation.overall.outputs
    assert h1.excluded == {"persistence_stale": 1, "target_missing": 1}
    assert h2.excluded == {"persistence_stale": 1}
    assert h1.model is not None and h1.paired is not None
    scored = len(test) - 2
    assert h1.model.count == scored
    # stale の persistence の example は model の誤差に入るが、対の比較には入らない
    assert h1.paired.count == scored - 1
    assert h1.paired.model.count == h1.paired.persistence.count == scored - 1
    assert h2.paired is not None and h2.paired.count == len(test) - 2


def test_an_output_without_persistence_is_blocked_at_evidence(examples: Any) -> None:
    train, test = examples

    def stale_last(document: dict[str, Any]) -> None:
        frame = document["window"][-1]
        frame["stale_mask"][TARGET] = True
        frame["quality"][TARGET] = Quality.STALE

    edited = tuple(edit(example, stale_last) for example in test)
    report = build(edited, train, predictor_from(edited, wiggle), config())
    assert report.gate.failed_stage == "evidence"
    assert all(output.paired is None for output in report.evaluation.overall.outputs)
    assert any("persistence と対になる値が無い" in reason for reason in report.gate.reasons)


def test_too_many_unpredictable_examples_is_blocked_at_evidence(examples: Any) -> None:
    train, test = examples
    failing = frozenset(example.action_ts_ms for example in test[:5])
    report = build(test, train, predictor_from(test, wiggle, failing=failing), config())
    assert report.evaluation.excluded_examples == {"prediction": 5}
    assert report.gate.failed_stage == "evidence"
    assert any("予測できた割合" in reason for reason in report.gate.reasons)


# ---------------------------------------------------------------- 層


def _room(example: DatasetExampleV2, value: float) -> DatasetExampleV2:
    def change(document: dict[str, Any]) -> None:
        document["window"][-1]["values"]["air.room"] = value

    return edit(example, change)


def _cool_first(test: tuple[DatasetExampleV2, ...]) -> tuple[tuple[DatasetExampleV2, ...], int]:
    """最初の2ブロックの example の室温を cool の帯に入れる。"""
    cutoff = first_blocks_end(test, 2)
    edited = tuple(
        _room(example, 20.0) if example.action_ts_ms < cutoff else example for example in test
    )
    return edited, cutoff


def _worse_in_cool(cutoff: int) -> Callable[[DatasetExampleV2, int], float]:
    """cool の層では persistence より悪い（ただし高く外す。underprediction は出さない）。"""

    def value(example: DatasetExampleV2, horizon: int) -> float:
        observed = truth(example, horizon)
        if example.action_ts_ms < cutoff:
            return observed + abs(last_value(example) - observed) + 0.5
        return observed

    return value


def test_a_sufficient_stratum_where_the_model_is_worse_is_blocked(examples: Any) -> None:
    train, test = examples
    edited, cutoff = _cool_first(test)
    cfg = config(**{"gate.skill.alpha": 0.5})
    report = build(edited, train, predictor_from(edited, _worse_in_cool(cutoff)), cfg)
    cool = next(
        stratum
        for stratum in report.evaluation.strata
        if stratum.axis == "room_temperature" and stratum.name == "cool"
    )
    assert cool.status == "sufficient"
    assert report.gate.failed_stage == "skill"
    assert any("層 room_temperature=cool" in reason for reason in report.gate.reasons)


def test_an_insufficient_stratum_is_reported_but_does_not_gate(examples: Any) -> None:
    train, test = examples
    edited, cutoff = _cool_first(test)
    cfg = config(**{"gate.skill.alpha": 0.5, "minimum.stratum_examples": 100})
    report = build(edited, train, predictor_from(edited, _worse_in_cool(cutoff)), cfg)
    assert {stratum.status for stratum in report.evaluation.strata} == {"insufficient"}
    cool = next(stratum for stratum in report.evaluation.strata if stratum.name == "cool")
    [h1, _h2] = cool.outputs
    assert h1.paired is not None and h1.paired.model.mae > h1.paired.persistence.mae
    assert report.gate.outcome == "pass", report.gate.reasons


def test_strata_cover_every_axis_and_unknown_is_kept(examples: Any) -> None:
    train, test = examples
    report = build(test, train, predictor_from(test, wiggle), config())
    axes = {(stratum.axis, stratum.name, stratum.gating) for stratum in report.evaluation.strata}
    # 電力の metric は window に無いので workload は unknown・regime は未接続で unknown
    assert ("workload", "unknown", True) in axes
    assert ("workload_regime", "unknown", False) in axes
    assert {axis for axis, _name, _gating in axes} == {
        "room_temperature",
        "workload",
        "action_change",
        "workload_regime",
    }
    for stratum in report.evaluation.strata:
        assert (
            sum(other.examples for other in report.evaluation.strata if other.axis == stratum.axis)
            == report.evaluation.predicted_examples
        )


# ---------------------------------------------------------------- 検定


@pytest.mark.parametrize(
    ("better", "nonzero", "expected"),
    [
        (0, 0, Fraction(1)),
        (3, 3, Fraction(1, 8)),
        (2, 3, Fraction(1, 2)),
        (0, 3, Fraction(1)),
        (5, 10, Fraction(319, 512)),
        (6, 6, Fraction(1, 64)),
        (9, 10, Fraction(11, 1024)),
    ],
)
def test_sign_test_p_value_golden(better: int, nonzero: int, expected: Fraction) -> None:
    assert sign_test_p_value(better, nonzero) == expected


def test_sign_test_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        sign_test_p_value(4, 3)
    with pytest.raises(ValueError):
        sign_test_p_value(-1, 3)


def test_blocks_with_zero_mean_difference_are_dropped_from_the_test(examples: Any) -> None:
    train, test = examples
    cutoff = first_blocks_end(test, 2)

    def tie_first_blocks(example: DatasetExampleV2, horizon: int) -> float:
        # 最初の2ブロックは persistence と同じ値（d = 0）
        return last_value(example) if example.action_ts_ms < cutoff else truth(example, horizon)

    report = build(test, train, predictor_from(test, tie_first_blocks), config())
    [h1, _h2] = report.evaluation.overall.outputs
    assert h1.paired is not None
    sign = h1.paired.sign_test
    assert sign.ties == 2
    assert sign.model_better + sign.persistence_better == report.evaluation.overall.blocks - 2
    assert Fraction(sign.p_value) == sign_test_p_value(
        sign.model_better, sign.model_better + sign.persistence_better
    )


# ---------------------------------------------------------------- 設定


def test_the_example_config_is_valid() -> None:
    path = Path(__file__).resolve().parents[1] / "config" / "thermal-evaluation.example.yaml"
    parsed = parse_evaluation_config(path.read_bytes())
    assert parsed.gate.underprediction.quantile.value in parsed.quantiles
    # 仮の値の例であることを形で示す（confirmed の値を置かない）
    text = path.read_text(encoding="utf-8")
    assert "confirmed" not in text


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("block_ms: {value: 4000, status: provisional}", "block_ms: {value: 4000}"),
        ("block_ms: {value: 4000, status: provisional}", "block_ms: 4000"),
        ("worst_case_count: 3", "worst_case_count: 3\nunknown_key: 1"),
        ("direction: higher_is_worse", "direction: lower_is_worse"),
        ("worst_case_count: 3", "worst_case_count: 3\nworst_case_count: 4"),
        (
            "quantile: {value: 0.9, status: provisional}",
            "quantile: {value: 0.95, status: provisional}",
        ),
        ("  - name: warm\n", ""),
        ("quantiles: [0.5, 0.9]", "quantiles: [0.9, 0.5]"),
        ("alpha: {value: 0.1, status: provisional}", "alpha: {value: 0.1, status: confirmed}"),
    ],
)
def test_malformed_configs_are_refused(before: str, after: str) -> None:
    assert before in EVALUATION_CONFIG
    with pytest.raises(ValueError):
        parse_evaluation_config(EVALUATION_CONFIG.replace(before, after, 1).encode("utf-8"))


# ---------------------------------------------------------------- 報告


def test_the_report_is_canonical_and_the_gate_is_rebuilt_from_its_numbers(examples: Any) -> None:
    train, test = examples
    cfg = config()
    report = build(test, train, predictor_from(test, wiggle), cfg)
    payload = canonical_report_bytes(report)
    assert payload.endswith(b"}\n") and not payload.endswith(b"\n\n")
    assert parse_report_bytes(payload) == report
    assert report.conditions_sha256 == conditions_sha256(report)
    assert evaluate_gate(report, cfg) == report.gate
    # 手で outcome を書き換えた報告は、作り直した gate と一致しない
    document = json.loads(payload)
    document["gate"] = {"outcome": "pass", "failed_stage": None, "reasons": []}
    flat = build(
        tuple(edit(example, constant_exhaust) for example in test),
        train,
        predictor_from(tuple(edit(example, constant_exhaust) for example in test), wiggle),
        cfg,
    )
    forged_document = json.loads(canonical_report_bytes(flat))
    forged_document["gate"] = {"outcome": "pass", "failed_stage": None, "reasons": []}
    forged = ThermalEvaluationReport.model_validate_json(json.dumps(forged_document))
    assert evaluate_gate(forged, cfg).outcome == "blocked"


def test_non_canonical_or_inconsistent_reports_are_refused(examples: Any) -> None:
    train, test = examples
    report = build(test, train, predictor_from(test, wiggle), config())
    document = json.loads(canonical_report_bytes(report))
    indented = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with pytest.raises(ValueError, match="canonical"):
        parse_report_bytes(indented)
    document["split"]["test_start_ms"] += 1
    shifted = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with pytest.raises(ValueError, match="conditions_sha256"):
        parse_report_bytes(shifted)


def test_offline_evaluation_ref_has_the_fixed_form() -> None:
    ref = offline_evaluation_ref(b"{}\n")
    assert ref.startswith("thermal-offline-v1:")
    assert is_offline_evaluation_ref(ref)
    assert not is_offline_evaluation_ref("thermal-offline-v1:" + "A" * 64)
    assert not is_offline_evaluation_ref("supervisor-episode:" + "0" * 64)
    assert not is_offline_evaluation_ref("evaluation-2026-10-01")
