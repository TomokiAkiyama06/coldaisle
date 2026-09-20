"""#93 Thermal Model の drift 検知と再学習の条件。実機不要（合成した証拠だけで判定する）。

**ここでは「守れているか」ではなく「破れないか」を試す。** 決定記録 0056 の不変条件を並べ、
1つずつ破ろうとする試験を置く。

1. drift は **runtime（0050 の confidence）と offline（この package）に分かれる**。
   offline は confidence / authority / demand / PWM へ届かない
2. 誤差に数えてよいのは `status: scored` で**全出力が照合できた** outcome だけ
3. 証拠は model / artifact / 推論 / 候補 plan の識別子で束縛する。結べない証拠は受け取らない
4. **同じ証拠を2回数えない**（行や outcome を複製して下限を満たせない）
5. coverage が足りなければ**比を出さず** `insufficient_evidence` と答える（`ok` にしない）
6. **一度も採点できていない metric**があれば `sufficient` にしない
7. trend の bucket は**自分の件数だけ**で判定する（隣から借りない）
8. 宣言された構成変更より前の証拠は数えない。除いた結果が薄ければ「言えない」と答える
9. 閾値はすべて設定から来る。**runtime の契約より鈍い設定は受け付けない**
10. 再学習は**推奨まで**。candidate の登録は Production を動かさない
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.control.config import ShadowConfig
from coldaisle.control.drift import (
    ChangeKind,
    DeclaredChange,
    DriftConfig,
    DriftConfigError,
    DriftDetector,
    DriftEvidence,
    DriftInputError,
    DriftReport,
    DriftSignalKind,
    DriftVerdict,
    InputDriftSignal,
    RetrainingRecommendation,
)
from coldaisle.control.evaluation.model import CountedReason
from coldaisle.control.model.confidence import (
    ConfidenceAssessor,
    ConfidenceComponent,
    ModelConfidenceProfile,
)
from coldaisle.control.model.thermal import ArtifactVerification, ObservedThermalInput
from coldaisle.control.schema import (
    AuthorityStage,
    ControllerKind,
    Demand,
    OptimizerStatus,
    PerZone,
    Reason,
    ShadowActionPlan,
    ShadowCounterfactual,
    ShadowPlanStep,
    ShadowPredictedTarget,
    ShadowPrediction,
    ShadowRecord,
)
from coldaisle.control.shadow import (
    OutcomeMatch,
    OutcomeStatus,
    ShadowExportRow,
    ShadowOutcome,
)
from test_fallback_controller import learned_proposal
from test_model_confidence import (
    AIR,
    GPU,
    _active_gate,
    _select,
    assessor,
    confidence_policy,
    dataset,
    evidence,
    fit_confidence_profile,
    profile_spec,
    shifted,
    split,
    train,
)

ROOT = Path(__file__).resolve().parents[1]
DRIFT_PACKAGE = ROOT / "src" / "coldaisle" / "control" / "drift"
SHIPPED_CONFIG = ROOT / "config" / "drift.yaml"

HORIZONS = (1_000, 2_000)
TARGETS = (GPU, AIR)
BASE_TS_MS = 1_787_616_000_000
STEP_MS = 10_000
TOLERANCE_MS = 500
PREDICTED = 50.0


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def trained():
    """合成 dataset で学習した #84 モデルと #85 Profile（`test_model_confidence` と同じ）。"""
    data = dataset(HORIZONS, TARGETS)
    parts = split(data)
    model = train(data, parts)
    profile = fit_confidence_profile(model, data, parts, profile_spec())
    return data, parts, model, profile


def scales(profile: ModelConfidenceProfile) -> dict[tuple[int, str], float]:
    return {(item.horizon_ms, item.metric): item.scale for item in profile.residual_scales}


def provisional(value: float | int) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def drift_config(
    *,
    minimum_scored_outcomes: int = 5,
    minimum_identifiable_fraction: float = 0.2,
    warning_ratio: float = 1.5,
    degraded_ratio: float = 2.0,
    trend_bucket_outcomes: int = 3,
    minimum_bucket_outcomes: int = 2,
    minimum_inputs: int = 4,
) -> DriftConfig:
    """試験用の設定。**値はすべて設定から来る**（コードに既定値は無い）。"""
    band = {"warning_fraction": provisional(0.05), "degraded_fraction": provisional(0.2)}
    return DriftConfig.model_validate(
        {
            "schema_version": 1,
            "residual": {
                "minimum_scored_outcomes": provisional(minimum_scored_outcomes),
                "minimum_identifiable_fraction": provisional(minimum_identifiable_fraction),
                "warning_ratio": provisional(warning_ratio),
                "degraded_ratio": provisional(degraded_ratio),
                "trend_bucket_outcomes": provisional(trend_bucket_outcomes),
                "minimum_bucket_outcomes": provisional(minimum_bucket_outcomes),
            },
            "minimum_inputs": provisional(minimum_inputs),
            "feature_range": band,
            "support": band,
            "missing_pattern": band,
        }
    )


def shadow_config(tolerance_ms: int = TOLERANCE_MS) -> ShadowConfig:
    """照合の許容幅の契約（`fan-policy.yaml` の `shadow`）。**drift 設定に写さない。**"""
    return ShadowConfig.model_validate(
        {
            "enabled": True,
            "outcome_match_tolerance_ms": provisional(tolerance_ms),
            "applied_demand_tolerance": provisional(0.01),
        }
    )


def detector(
    profile: ModelConfidenceProfile,
    config: DriftConfig | None = None,
    *,
    shadow: ShadowConfig | None = None,
) -> DriftDetector:
    return DriftDetector(
        ConfidenceAssessor(profile, confidence_policy()),
        config if config is not None else drift_config(),
        shadow=shadow if shadow is not None else shadow_config(),
        config_sha256="d" * 64,
    )


# ---------------------------------------------------------------- 証拠の組み立て


def demands(value: float) -> PerZone[Demand]:
    return PerZone[Demand](front=value, rear=value, top=value)


def plan_for(demand: float = 0.5) -> ShadowActionPlan:
    return ShadowActionPlan(
        step_ms=HORIZONS[0],
        steps=tuple(
            ShadowPlanStep(offset_ms=offset, demands=demands(demand)) for offset in HORIZONS
        ),
    )


def inference_for(index: int) -> str:
    return f"{index:064x}"


def row(
    profile: ModelConfidenceProfile,
    index: int,
    *,
    ratio: float = 1.0,
    status: OutcomeStatus = OutcomeStatus.SCORED,
    metrics: tuple[str, ...] = TARGETS,
    unmatched_metrics: tuple[str, ...] = (),
    model_version: str | None = None,
    artifact_sha256: str | None = None,
    tick_id: int | None = None,
    inference: str | None = None,
    tolerance_ms: int = TOLERANCE_MS,
    observed_offset_ms: int = 100,
) -> ShadowExportRow:
    """1 tick 分の Shadow 実績。**誤差は validation scale の `ratio` 倍**にする。

    `ratio=1.0` なら学習時と同じ水準、`2.0` なら2倍の誤差である。
    """
    binding = profile.binding
    action_ts_ms = BASE_TS_MS + index * STEP_MS
    plan = plan_for()
    inference_id = inference if inference is not None else inference_for(index)
    scale = scales(profile)
    prediction = ShadowPrediction(
        model_id=binding.model_id,
        model_version=model_version if model_version is not None else binding.model_version,
        artifact_sha256=(
            artifact_sha256 if artifact_sha256 is not None else binding.artifact_sha256
        ),
        inference_id=inference_id,
        plan_digest=plan.digest(),
        input_action_ts_ms=action_ts_ms,
        targets=tuple(
            ShadowPredictedTarget(
                offset_ms=offset,
                expected_ts_ms=action_ts_ms + offset,
                values={metric: PREDICTED for metric in metrics},
            )
            for offset in HORIZONS
        ),
    )
    counterfactual = ShadowCounterfactual(
        controller=ControllerKind.LEARNED_MPC,
        requested=plan.first,
        reason=Reason(code="optimizer_ok"),
        optimizer_status=OptimizerStatus.OK,
        model_version=prediction.model_version,
        inference_id=inference_id,
        artifact_sha256=prediction.artifact_sha256,
        plan=plan,
        prediction=prediction,
        cost_total=1.0,
        baseline_cost_total=2.0,
    )
    scored = status is OutcomeStatus.SCORED
    matches = []
    for offset in HORIZONS:
        for metric in sorted(metrics):
            expected_ts_ms = action_ts_ms + offset
            if metric in unmatched_metrics:
                matches.append(
                    OutcomeMatch(
                        offset_ms=offset,
                        expected_ts_ms=expected_ts_ms,
                        metric=metric,
                        predicted=PREDICTED,
                        unmatched=Reason(code="no_usable_observation"),
                    )
                )
                continue
            # Profile に基準が無い metric（試験用）でも記録は作れる。判定側が拒む。
            observed = PREDICTED + ratio * scale.get((offset, metric), 1.0)
            matches.append(
                OutcomeMatch(
                    offset_ms=offset,
                    expected_ts_ms=expected_ts_ms,
                    metric=metric,
                    predicted=PREDICTED,
                    observed=observed,
                    observed_ts_ms=expected_ts_ms + observed_offset_ms,
                    error=(observed - PREDICTED) if scored else None,
                )
            )
    outcome = ShadowOutcome(
        inference_id=inference_id,
        plan_digest=plan.digest(),
        input_action_ts_ms=action_ts_ms,
        match_tolerance_ms=tolerance_ms,
        status=status,
        unidentifiable=(
            None if scored else Reason(code="applied_action_differs", detail="別の値が掛かっていた")
        ),
        matches=tuple(matches),
    )
    return ShadowExportRow(
        control_schema_version=6,
        tick_id=index if tick_id is None else tick_id,
        ts_ms=action_ts_ms,
        shadow=ShadowRecord(
            tick_id=index if tick_id is None else tick_id,
            ts_ms=action_ts_ms,
            authority_stage=AuthorityStage.SHADOW,
            applied_controller=ControllerKind.FALLBACK,
            applied_effective=demands(0.4),
            counterfactuals=(counterfactual,),
        ),
        outcomes=(outcome,),
    )


def rows(profile: ModelConfidenceProfile, count: int, *, ratio: float = 1.0, start: int = 0):
    return tuple(row(profile, index, ratio=ratio) for index in range(start, start + count))


def residual_of(report: DriftReport):
    return report.residual


def signal_of(report: DriftReport, kind: DriftSignalKind) -> InputDriftSignal:
    return next(item for item in report.inputs if item.kind is kind)


# ---------------------------------------------------------------- 1. 層の分離（制御へ届かない）


def test_drift_package_cannot_reach_fan_control() -> None:
    """**offline の drift 検知は制御経路を持たない**（決定記録 0056 §2.1）。

    hardware / safety / reactive / serial / subprocess を import しないことを走査で確かめる。
    import できてしまえば、あとから1行で「drift を見て demand を下げる」が書ける。
    """
    forbidden = {
        "coldaisle.control.hardware",
        "coldaisle.control.safety",
        "coldaisle.control.reactive",
        "coldaisle.control.fallback",
        "serial",
        "subprocess",
    }
    found: dict[str, set[str]] = {}
    for path in sorted(DRIFT_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.add(node.module)
        offending = {
            name
            for name in modules
            for banned in forbidden
            if name == banned or name.startswith(f"{banned}.")
        }
        if offending:
            found[path.name] = offending
    assert found == {}


def test_drift_package_has_no_clock() -> None:
    """**時計を持たない**（0056 §2.1）。時刻はすべて証拠から来る。"""
    banned = {"time", "datetime", "coldaisle.clock"}
    for path in sorted(DRIFT_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = set()
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = {node.module}
            assert not names & banned, f"{path.name} が時計を import している: {names}"


def test_report_cannot_carry_demand_or_authority(trained) -> None:
    """報告に demand / PWM / authority の欄を作らない（0056 §2.1）。"""
    _data, _parts, _model, profile = trained
    report = detector(profile).detect(DriftEvidence(shadow=rows(profile, 6)))
    payload = json.dumps(report.model_dump(mode="json"))
    for banned in ("demand", "pwm", "authority", "effective"):
        assert banned not in payload.lower()
    assert not hasattr(report, "confidence")


def test_runtime_drift_still_reaches_the_authority_gate_through_confidence(trained) -> None:
    """**runtime の drift は 0050 の経路のまま**（#93 の「Authority Gate へ通知」）。

    #93 は runtime に2つ目の判定を足さない。residual が OOD 倍率まで悪化した証拠を
    渡せば、`ConfidenceAssessor` が OOD を出し、Gate が Fallback を選ぶ。
    ここが壊れたら、offline の報告を confidence へ流し込みたくなる。
    """
    _data, parts, model, profile = trained
    observed = ObservedThermalInput.from_example(parts.test[0])
    judge = assessor(profile)
    drifted = judge.assess(
        observed,
        model.predict(observed),
        evidence(profile, 3.0, forecasts=20, at=observed.action_ts_ms),
    )
    assert drifted.ood is True
    assert ConfidenceComponent.RESIDUAL_DRIFT in drifted.ood_components
    assert drifted.confidence == 0.0
    assert "ood_residual_drift" in {reason.code for reason in drifted.trace_reasons()}

    # Gate は assessment の値だけを信じる（0050 §2.3）。OOD の判定は Fallback へ落ちる。
    deployed = drifted.model_copy(
        update={
            "artifact_verification": ArtifactVerification.REGISTRY_VERIFIED,
            "model_version": "thermal-v1",
        }
    )
    gate = _active_gate(AuthorityStage.FULL)
    proposal = deployed.apply_to(learned_proposal(0.1, inference_id=deployed.inference_id))
    selected = _select(gate, 2, proposal, deployed)

    assert selected.active_controller is ControllerKind.FALLBACK
    assert selected.fallback_reason is not None and selected.fallback_reason.code == "ood"
    assert selected.model_gate is not None
    assert selected.model_gate.ood is True
    assert "ood_residual_drift" in {reason.code for reason in selected.model_gate.assessment}


# ---------------------------------------------------------------- 2. 合成 drift を検知する


def test_synthetic_drift_is_detected_and_a_healthy_model_is_not(trained) -> None:
    """**合成 drift を検出できる**（#93 受入基準）。学習時の水準は `ok` のまま。"""
    _data, _parts, _model, profile = trained
    judge = detector(profile)

    healthy = judge.detect(DriftEvidence(shadow=rows(profile, 6, ratio=1.0)))
    assert healthy.residual.ratio == pytest.approx(1.0)
    assert healthy.residual.verdict is DriftVerdict.OK

    warned = judge.detect(DriftEvidence(shadow=rows(profile, 6, ratio=1.6)))
    assert warned.residual.verdict is DriftVerdict.WARNING

    drifted = judge.detect(DriftEvidence(shadow=rows(profile, 6, ratio=2.5)))
    assert drifted.residual.ratio == pytest.approx(2.5)
    assert drifted.residual.verdict is DriftVerdict.DEGRADED
    assert drifted.verdict is DriftVerdict.DEGRADED
    assert drifted.recommendation.required is True
    assert [item.code for item in drifted.recommendation.triggers] == ["residual_degraded"]


def test_drift_event_is_bound_to_the_model_and_the_report_is_deterministic(trained) -> None:
    """**drift event は model / version と対応付く**（#93 受入基準）。"""
    _data, _parts, _model, profile = trained
    evidence_set = DriftEvidence(shadow=rows(profile, 6, ratio=2.5))
    first = detector(profile).detect(evidence_set)
    again = detector(profile).detect(evidence_set)

    assert first.canonical_bytes() == again.canonical_bytes()
    assert first.sha256() == again.sha256()
    assert first.target.model_id == profile.binding.model_id
    assert first.target.model_version == profile.binding.model_version
    assert first.target.artifact_sha256 == profile.binding.artifact_sha256
    assert first.target.profile_sha256 == profile.sha256()
    assert DriftReport.model_validate_json(first.canonical_bytes()) == first
    # 生成時刻を持たない（0054 §2.7 と同じ規律）。
    assert "created" not in json.dumps(first.model_dump(mode="json"))


def test_residual_trend_is_ordered_by_evidence_time_and_stored(trained) -> None:
    """**residual trend を可視化・保存できる**（#93 受入基準）。

    bucket の時刻は**照合に使った観測**から来る。処理した順ではない。
    """
    _data, _parts, _model, profile = trained
    evidence_set = DriftEvidence(
        shadow=(
            *rows(profile, 3, ratio=1.0),
            *tuple(row(profile, index, ratio=2.5) for index in range(3, 6)),
        )
    )
    report = detector(profile).detect(evidence_set)
    trend = report.residual.trend

    assert [bucket.index for bucket in trend] == [0, 1]
    assert trend[0].verdict is DriftVerdict.OK
    assert trend[1].verdict is DriftVerdict.DEGRADED
    assert trend[0].end_ts_ms < trend[1].start_ts_ms
    assert report.residual.first_evidence_ts_ms == trend[0].start_ts_ms
    assert report.residual.last_evidence_ts_ms == trend[1].end_ts_ms
    # 入力の順序を変えても同じ報告になる（時刻は証拠から来るため）。
    shuffled = DriftEvidence(shadow=tuple(reversed(evidence_set.shadow)))
    assert detector(profile).detect(shuffled).residual.trend == trend


# ---------------------------------------------------------------- 3. 数えてよい証拠


def test_unidentifiable_outcomes_never_become_measured_drift(trained) -> None:
    """**採点できない区間の差を drift にしない**（0053 §2.3 / 0056 §2.3）。

    掛かっていた action が違えば、差は制御器の違いとモデル誤差の混合である。
    """
    _data, _parts, _model, profile = trained
    clean = rows(profile, 6, ratio=1.0)
    noisy = tuple(
        row(profile, index, ratio=9.0, status=OutcomeStatus.UNIDENTIFIABLE)
        for index in range(6, 12)
    )
    only_clean = detector(profile).detect(DriftEvidence(shadow=clean))
    mixed = detector(profile).detect(DriftEvidence(shadow=(*clean, *noisy)))

    assert mixed.residual.ratio == pytest.approx(only_clean.residual.ratio)
    assert mixed.residual.verdict is DriftVerdict.OK
    coverage = mixed.residual.coverage
    assert (coverage.outcomes, coverage.scored, coverage.unidentifiable) == (12, 6, 6)
    assert coverage.identifiable_fraction == pytest.approx(0.5)
    assert [(item.code, item.count) for item in coverage.unidentifiable_reasons] == [
        ("applied_action_differs", 6)
    ]


def test_partially_matched_outcomes_are_not_counted_as_measured_drift(trained) -> None:
    """**全出力が揃った outcome だけ**を比に数える（0056 §2.3）。

    照合できた出力だけの誤差は、当たりやすい出力に偏る。
    """
    _data, _parts, _model, profile = trained
    complete = rows(profile, 6, ratio=1.0)
    partial = tuple(
        row(profile, index, ratio=9.0, unmatched_metrics=(AIR,)) for index in range(6, 12)
    )
    report = detector(profile).detect(DriftEvidence(shadow=(*complete, *partial)))

    assert report.residual.ratio == pytest.approx(1.0)
    coverage = report.residual.coverage
    assert (coverage.scored, coverage.counted, coverage.incomplete_scored) == (12, 6, 6)
    assert coverage.counted_outputs == 6 * len(HORIZONS) * len(TARGETS)
    assert [(item.code, item.count) for item in coverage.unmatched_reasons] == [
        ("no_usable_observation", 6 * len(HORIZONS))
    ]


def test_a_metric_that_is_never_scored_blocks_sufficiency(trained) -> None:
    """**一度も採点できていない metric があれば `sufficient` にしない**（0054 §2.3 と同じ理由）。

    残りの metric だけで「悪化していない」と言えてしまう。
    """
    _data, _parts, _model, profile = trained
    gpu_only = tuple(row(profile, index, ratio=1.0, metrics=(GPU,)) for index in range(6))
    never_scored = tuple(
        row(profile, index, ratio=1.0, unmatched_metrics=(AIR,)) for index in range(6, 8)
    )
    report = detector(profile).detect(DriftEvidence(shadow=(*gpu_only, *never_scored)))

    coverage = report.residual.coverage
    assert coverage.counted == 6
    assert coverage.predicted_metrics == tuple(sorted(TARGETS))
    assert coverage.unscored_metrics == (AIR,)
    assert coverage.sufficient is False
    assert report.residual.ratio is None
    assert report.residual.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
    assert report.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
    assert report.recommendation.required is False
    assert DriftSignalKind.RESIDUAL in report.recommendation.inconclusive_signals


def test_evidence_from_another_model_is_not_counted_as_this_model_drift(trained) -> None:
    """**identity を束縛する**（0056 §2.3）。別のモデルの区間を混ぜない。"""
    _data, _parts, _model, profile = trained
    ours = rows(profile, 6, ratio=1.0)
    theirs = tuple(row(profile, index, ratio=9.0, model_version="0.2.0") for index in range(6, 12))
    other_artifact = tuple(
        row(profile, index, ratio=9.0, artifact_sha256="f" * 64) for index in range(12, 18)
    )
    report = detector(profile).detect(DriftEvidence(shadow=(*ours, *theirs, *other_artifact)))

    assert report.residual.ratio == pytest.approx(1.0)
    assert report.residual.coverage.foreign_model == 12
    assert report.residual.coverage.outcomes == 6


def rewrite_first_match(source: ShadowExportRow, **overrides: object) -> ShadowExportRow:
    """1行の outcome の**最初の**照合結果だけを差し替える（束縛を1つずつ壊すため）。"""
    outcome = source.outcomes[0]
    first, *rest = outcome.matches
    return source.model_copy(
        update={
            "outcomes": (
                outcome.model_copy(
                    update={"matches": (first.model_copy(update=dict(overrides)), *rest)}
                ),
            )
        }
    )


def test_outputs_that_the_prediction_never_had_are_refused(trained) -> None:
    """**照合結果の出力は、束ねた予測の出力そのものでなければならない。**

    予測に無い出力は、その記録が別の推論を抱えている証拠である。
    """
    _data, _parts, _model, profile = trained
    broken = rewrite_first_match(row(profile, 0), metric="gpu.0.hotspot")
    with pytest.raises(DriftInputError, match="予測に無い出力"):
        detector(profile).detect(DriftEvidence(shadow=(broken,)))


def test_outputs_outside_the_profile_target_schema_are_refused(trained) -> None:
    """binding が一致しているのに Profile に無い出力は、記録か Profile が壊れている。"""
    _data, _parts, _model, profile = trained
    # 予測も照合結果も同じ metric（束縛は壊れていない）。Profile にだけ基準が無い。
    stray = row(profile, 0, metrics=("gpu.0.hotspot",))
    with pytest.raises(DriftInputError, match="target schema"):
        detector(profile).detect(DriftEvidence(shadow=(stray,)))


def test_an_outcome_that_drops_outputs_is_not_a_complete_forecast(trained) -> None:
    """**記録から出力を落とすだけで「全出力が揃った forecast」にできない。**

    `ShadowOutcome.complete` は残っている match の中しか見ない。出力の集合は
    束ねた予測から取り、落ちた出力は**照合できなかった**のと同じ扱いにする。
    """
    _data, _parts, _model, profile = trained
    trimmed = tuple(
        item.model_copy(
            update={
                "outcomes": (
                    item.outcomes[0].model_copy(update={"matches": item.outcomes[0].matches[:1]}),
                )
            }
        )
        for item in rows(profile, 6, ratio=1.0)
    )
    report = detector(profile).detect(DriftEvidence(shadow=trimmed))

    coverage = report.residual.coverage
    assert (coverage.scored, coverage.counted, coverage.incomplete_scored) == (6, 0, 6)
    assert coverage.outputs == 6 * len(HORIZONS) * len(TARGETS)
    assert ("output_missing_from_outcome", 6 * 3) in [
        (item.code, item.count) for item in coverage.unmatched_reasons
    ]
    # 落ちた metric は「採点できていない metric」として残る。
    assert coverage.predicted_metrics == tuple(sorted(TARGETS))
    assert report.residual.ratio is None
    assert report.residual.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE


def test_the_same_output_cannot_be_counted_twice_inside_one_outcome(trained) -> None:
    """同じ出力の照合結果を2つ置けば、その誤差を2回数えられる。"""
    _data, _parts, _model, profile = trained
    source = row(profile, 0)
    outcome = source.outcomes[0]
    doubled = source.model_copy(
        update={
            "outcomes": (
                outcome.model_copy(update={"matches": (*outcome.matches, outcome.matches[0])}),
            )
        }
    )
    with pytest.raises(DriftInputError, match="同じ出力"):
        detector(profile).detect(DriftEvidence(shadow=(doubled,)))


def test_observation_times_outside_the_recorded_tolerance_are_refused(trained) -> None:
    """**証拠の時刻は照合の規則の中にしか置けない**（0053 §2.3）。

    許容幅の外の観測時刻を置けると、宣言された変更より前の証拠を後ろへずらして
    数えさせたり、trend の bucket を並べ替えたりできる。
    """
    _data, _parts, _model, profile = trained
    source = row(profile, 0)
    late = source.outcomes[0].matches[0].expected_ts_ms + TOLERANCE_MS + 1
    with pytest.raises(DriftInputError, match="許容幅の外"):
        detector(profile).detect(
            DriftEvidence(shadow=(rewrite_first_match(source, observed_ts_ms=late),))
        )
    early = source.shadow.ts_ms - 1
    with pytest.raises(DriftInputError, match="action より前"):
        detector(profile).detect(
            DriftEvidence(shadow=(rewrite_first_match(source, observed_ts_ms=early),))
        )


def test_outcomes_must_be_matched_with_the_configured_tolerance(trained) -> None:
    """**照合の許容幅は `fan-policy.yaml` の契約から取る**（0054 §2.6 / 0056 §2.3）。

    outcome どうしが揃っているだけでは足りない。全部が同じ「別の幅」で照合されていれば、
    記録された coverage と意味の違う証拠が、そのまま drift の根拠になる。
    """
    _data, _parts, _model, profile = trained
    mixed = (row(profile, 0), row(profile, 1, tolerance_ms=TOLERANCE_MS + 1))
    with pytest.raises(DriftInputError, match="照合許容幅"):
        detector(profile).detect(DriftEvidence(shadow=mixed))

    # **全部が揃って別の幅**でも受け取らない（契約と照らすため）。
    consistent = rows(profile, 6, ratio=1.0)
    other = tuple(
        row(profile, index, ratio=1.0, tolerance_ms=TOLERANCE_MS + 1) for index in range(6)
    )
    assert detector(profile).detect(DriftEvidence(shadow=consistent)).residual.ratio is not None
    with pytest.raises(DriftInputError, match="fan-policy"):
        detector(profile).detect(DriftEvidence(shadow=other))
    # 契約の側を合わせれば数えられる（写しではなく照合であることの確認）。
    loosened = detector(profile, shadow=shadow_config(TOLERANCE_MS + 1))
    report = loosened.detect(DriftEvidence(shadow=other))
    assert report.provenance.outcome_match_tolerance_ms == TOLERANCE_MS + 1


# ---------------------------------------------------------------- 4. 複製で数を増やせない


def test_duplicated_rows_cannot_inflate_the_counts(trained) -> None:
    """**行を複製するだけで下限を満たせない**（0054 §2.6 と同じ型の穴）。"""
    _data, _parts, _model, profile = trained
    original = rows(profile, 3, ratio=1.0)
    with pytest.raises(DriftInputError, match="2度現れた"):
        detector(profile).detect(DriftEvidence(shadow=(*original, *original)))


def test_duplicated_outcomes_inside_one_row_cannot_inflate_the_counts(trained) -> None:
    """同じ推論・同じ候補 plan の outcome を1 tick に2つ置けない。"""
    _data, _parts, _model, profile = trained
    single = row(profile, 0)
    doubled = single.model_copy(update={"outcomes": (single.outcomes[0], single.outcomes[0])})
    with pytest.raises(DriftInputError, match="2度現れた"):
        detector(profile).detect(DriftEvidence(shadow=(doubled,)))


def test_outcomes_that_cannot_be_bound_to_a_counterfactual_are_refused(trained) -> None:
    """**時刻の近さで結び直さない。** 結べない記録は受け取らない（0054 §2.6）。"""
    _data, _parts, _model, profile = trained
    single = row(profile, 0)
    stray = single.model_copy(
        update={
            "outcomes": (
                single.outcomes[0].model_copy(update={"inference_id": inference_for(999)}),
            )
        }
    )
    with pytest.raises(DriftInputError, match="結べない"):
        detector(profile).detect(DriftEvidence(shadow=(stray,)))


def test_another_export_schema_version_is_refused(trained) -> None:
    """版の違う export を混ぜない（欄の意味が変わっている可能性がある）。"""
    _data, _parts, _model, profile = trained
    single = row(profile, 0)
    with pytest.raises(DriftInputError, match="版"):
        detector(profile).detect(
            DriftEvidence(shadow=(single.model_copy(update={"schema_version": 2}),))
        )


# ---------------------------------------------------------------- 5. 薄い証拠から結論を出さない


def test_thin_evidence_is_never_reported_as_no_drift(trained) -> None:
    """**足りない証拠は `ok` にしない**（fail closed。0056 §2.4）。"""
    _data, _parts, _model, profile = trained
    report = detector(profile).detect(DriftEvidence(shadow=rows(profile, 4, ratio=1.0)))

    assert report.residual.coverage.counted == 4
    assert report.residual.coverage.sufficient is False
    assert report.residual.ratio is None
    assert report.residual.per_metric == ()
    assert report.residual.trend == ()
    assert report.residual.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
    assert report.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
    assert report.recommendation.required is False
    assert report.recommendation.inconclusive_signals[0] is DriftSignalKind.RESIDUAL


def test_low_identifiable_fraction_hides_the_prediction_metrics(trained) -> None:
    """採点できた僅かな区間だけを根拠にしない（0054 §2.3 と同じ規則）。"""
    _data, _parts, _model, profile = trained
    scored = rows(profile, 5, ratio=1.0)
    unidentifiable = tuple(
        row(profile, index, ratio=1.0, status=OutcomeStatus.UNIDENTIFIABLE)
        for index in range(5, 45)
    )
    report = detector(profile).detect(DriftEvidence(shadow=(*scored, *unidentifiable)))

    assert report.residual.coverage.identifiable_fraction == pytest.approx(5 / 45)
    assert report.residual.coverage.sufficient is False
    assert report.residual.ratio is None


def test_no_evidence_at_all_is_inconclusive_not_ok(trained) -> None:
    """証拠が1つも無い報告を「drift 無し」と読ませない。"""
    _data, _parts, _model, profile = trained
    report = detector(profile).detect(DriftEvidence())

    assert report.residual.coverage.outcomes == 0
    assert report.residual.coverage.identifiable_fraction is None
    assert report.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
    assert len(report.recommendation.inconclusive_signals) == 4
    assert report.recommendation.required is False


def test_a_degraded_signal_outranks_an_inconclusive_one(trained) -> None:
    """**言えるなら言う。** `degraded` は `insufficient_evidence` に隠れない。"""
    _data, _parts, _model, profile = trained
    report = detector(profile).detect(DriftEvidence(shadow=rows(profile, 6, ratio=2.5)))

    assert report.residual.verdict is DriftVerdict.DEGRADED
    assert signal_of(report, DriftSignalKind.SUPPORT).verdict is (
        DriftVerdict.INSUFFICIENT_EVIDENCE
    )
    assert report.verdict is DriftVerdict.DEGRADED
    assert report.recommendation.required is True


# ---------------------------------------------------------------- 6. trend の bucket


def test_a_thin_trend_bucket_does_not_borrow_evidence_from_its_neighbours(trained) -> None:
    """**bucket は自分の件数だけで判定する**（0056 §2.4）。"""
    _data, _parts, _model, profile = trained
    # bucket = 3 件、bucket の下限 = 2 件。7 件なら最後の bucket は1件しか無い。
    report = detector(profile).detect(DriftEvidence(shadow=rows(profile, 7, ratio=1.0)))
    trend = report.residual.trend

    assert [bucket.outcomes for bucket in trend] == [3, 3, 1]
    assert trend[-1].ratio is None
    assert trend[-1].verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
    assert sum(bucket.outcomes for bucket in trend) == report.residual.coverage.counted


# ---------------------------------------------------------------- 7. 入力分布


def test_operating_outside_the_learned_range_is_reported_as_input_drift(trained) -> None:
    """学習範囲の外で運転していることを、入力分布の drift として出す。

    判定は **`ConfidenceAssessor` と同じ実装**（`coverage()`）を通る。
    """
    _data, parts, _model, profile = trained
    inside = tuple(ObservedThermalInput.from_example(item) for item in parts.test)
    outside = tuple(shifted(item, air=35.0) for item in inside)
    judge = detector(profile)

    healthy = judge.detect(DriftEvidence(inputs=inside))
    assert signal_of(healthy, DriftSignalKind.FEATURE_RANGE).fraction == pytest.approx(0.0)
    assert signal_of(healthy, DriftSignalKind.FEATURE_RANGE).verdict is DriftVerdict.OK

    report = judge.detect(DriftEvidence(inputs=outside))
    feature = signal_of(report, DriftSignalKind.FEATURE_RANGE)
    assert feature.fraction == pytest.approx(1.0)
    assert feature.verdict is DriftVerdict.DEGRADED
    assert feature.sources[0].source == AIR
    assert signal_of(report, DriftSignalKind.SUPPORT).verdict is DriftVerdict.DEGRADED
    codes = {item.code for item in report.recommendation.triggers}
    assert {"feature_range_degraded", "support_degraded"} <= codes


def test_the_same_input_cannot_be_counted_twice(trained) -> None:
    """**同じ推論入力を2回数えない。** 1件を並べ直すだけで下限を満たせてしまう。"""
    _data, parts, _model, profile = trained
    one = ObservedThermalInput.from_example(parts.test[0])
    with pytest.raises(DriftInputError, match="2度現れた"):
        detector(profile).detect(DriftEvidence(inputs=(one,) * 8))


def test_too_few_inputs_publish_no_fraction(trained) -> None:
    """入力が下限に満たなければ**割合を出さない**（薄い証拠で分布を語らない）。"""
    _data, parts, _model, profile = trained
    few = tuple(ObservedThermalInput.from_example(item) for item in parts.test[:3])
    report = detector(profile).detect(DriftEvidence(inputs=few))

    for kind in (
        DriftSignalKind.FEATURE_RANGE,
        DriftSignalKind.SUPPORT,
        DriftSignalKind.MISSING_PATTERN,
    ):
        signal = signal_of(report, kind)
        assert signal.fraction is None
        assert signal.sources == ()
        assert signal.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------- 8. 宣言された構成変更


def test_evidence_before_a_declared_change_is_not_counted(trained) -> None:
    """**交換前後の residual を平均しない**（0056 §2.5）。"""
    _data, _parts, _model, profile = trained
    before = tuple(row(profile, index, ratio=2.5) for index in range(6))
    after = tuple(row(profile, index, ratio=1.0) for index in range(6, 12))
    change = DeclaredChange(
        kind=ChangeKind.FAN_REPLACED,
        ts_ms=BASE_TS_MS + 6 * STEP_MS - 1,
        detail="front fan を交換",
    )
    report = detector(profile).detect(DriftEvidence(shadow=(*before, *after), changes=(change,)))

    assert report.residual.coverage.excluded_by_change == 6
    assert report.residual.coverage.counted == 6
    assert report.residual.ratio == pytest.approx(1.0)
    assert report.residual.verdict is DriftVerdict.OK
    assert report.recommendation.recharacterization_required is True
    assert report.recommendation.required is True
    assert [item.code for item in report.recommendation.triggers] == ["declared_fan_replaced"]
    assert report.recommendation.triggers[0].evidence_ts_ms == change.ts_ms


def test_a_change_that_removes_all_evidence_leaves_the_answer_open(trained) -> None:
    """交換直後に「劣化していない」と言う根拠は無い（fail closed）。"""
    _data, parts, _model, profile = trained
    change = DeclaredChange(kind=ChangeKind.SENSOR_REPLACED, ts_ms=BASE_TS_MS + 100 * STEP_MS)
    report = detector(profile).detect(
        DriftEvidence(
            shadow=rows(profile, 8, ratio=1.0),
            inputs=tuple(ObservedThermalInput.from_example(item) for item in parts.test),
            changes=(change,),
        )
    )

    assert report.residual.coverage.excluded_by_change == 8
    assert report.residual.coverage.outcomes == 0
    assert report.residual.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
    assert signal_of(report, DriftSignalKind.SUPPORT).considered == 0
    assert report.verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
    assert report.recommendation.required is True
    assert report.recommendation.recharacterization_required is True


# ---------------------------------------------------------------- 9. 設定


def test_offline_thresholds_cannot_be_looser_than_the_runtime_contract(trained) -> None:
    """**runtime より鈍い設定を受け付けない**（0056 §2.8）。

    offline が `degraded` と呼ぶ前に runtime が OOD で Fallback へ落ちていると、
    「Fallback で回っているのに drift は無い」という報告が出る。
    """
    _data, _parts, _model, profile = trained
    ood_ratio = confidence_policy().residual_drift_ood_ratio.value
    with pytest.raises(DriftConfigError, match="residual_drift_ood_ratio"):
        detector(profile, drift_config(warning_ratio=2.0, degraded_ratio=ood_ratio + 0.5))
    # 同じ値までは許す（runtime と同時に degraded になる）。
    assert detector(profile, drift_config(degraded_ratio=ood_ratio)) is not None


def test_drift_config_requires_every_value_and_rejects_silent_widening() -> None:
    """**既定値をコードに置かない**（AGENTS.md ルール9）。欠けた設定は読み込みで落ちる。"""
    full = drift_config().model_dump(mode="python")
    del full["residual"]["warning_ratio"]
    with pytest.raises(ValidationError):
        DriftConfig.model_validate(full)
    with pytest.raises(ValidationError, match="degraded_ratio"):
        drift_config(warning_ratio=2.0, degraded_ratio=1.5)
    with pytest.raises(ValidationError, match="trend_bucket_outcomes"):
        drift_config(trend_bucket_outcomes=2, minimum_bucket_outcomes=3)


def test_shipped_config_loads_and_stays_provisional() -> None:
    """出荷している `config/drift.yaml` は読め、値はすべて暫定のままである。"""
    config, digest = DriftConfig.from_file(SHIPPED_CONFIG)

    assert config.schema_version == 1
    assert len(digest) == 64
    statuses = {
        config.residual.warning_ratio.status,
        config.residual.degraded_ratio.status,
        config.minimum_inputs.status,
        config.feature_range.warning_fraction.status,
        config.support.degraded_fraction.status,
        config.missing_pattern.warning_fraction.status,
    }
    assert statuses == {"provisional"}
    # 0056 §2.2: runtime が持つ閾値を drift 設定へ写さない。
    text = SHIPPED_CONFIG.read_text(encoding="utf-8")
    for copied in ("range_margin:", "min_support_count:", "residual_drift_ood_ratio:"):
        assert copied not in text


def test_reason_count_mirrors_the_evaluation_contract() -> None:
    """写した型が元の契約と食い違わない（0053 §2.5 と同じ規律）。"""
    from coldaisle.control.drift.model import DriftReasonCount

    assert set(DriftReasonCount.model_fields) == set(CountedReason.model_fields)
    mirrored = DriftReasonCount(code="applied_action_differs", count=2)
    assert CountedReason(**mirrored.model_dump()) is not None


# ---------------------------------------------------------------- 10. 再学習は推奨まで


def test_retraining_recommendation_always_needs_a_human(trained) -> None:
    """**承認不要の再学習を表現できない**（0056 §2.6。AGENTS.md ルール1〜5）。"""
    _data, _parts, _model, profile = trained
    report = detector(profile).detect(DriftEvidence(shadow=rows(profile, 6, ratio=2.5)))

    assert report.recommendation.human_approval_required is True
    with pytest.raises(ValidationError):
        RetrainingRecommendation(
            required=True,
            recharacterization_required=False,
            human_approval_required=False,  # type: ignore[arg-type]
            triggers=report.recommendation.triggers,
        )


def test_a_recommendation_cannot_claim_retraining_without_a_reason(trained) -> None:
    """理由の無い要求も、理由のある「不要」も作れない（trigger と要否を結ぶ）。"""
    with pytest.raises(ValidationError, match="要否と理由"):
        RetrainingRecommendation(required=True, recharacterization_required=False)


def test_acting_on_a_recommendation_registers_a_candidate_without_touching_production(
    trained, tmp_path
) -> None:
    """**再学習した成果物は candidate 止まり**（#93 受入基準 / 0056 §2.6）。

    drift の推奨は「候補を作れ」までで、Production ポインタは動かない。昇格は
    #90 / #91 の評価と人の承認を経る（0028 §2.9 承認点 5）。
    """
    from coldaisle.clock import SimulatedClock
    from coldaisle.control.model_registry import ArtifactKind, ArtifactStatus, ModelRegistry
    from test_model_registry import LIMITS, NOW_MS, metadata, payload, promote
    from test_model_registry import register_and_validate as validate

    _data, _parts, _model, profile = trained
    report = detector(profile).detect(DriftEvidence(shadow=rows(profile, 6, ratio=2.5)))
    assert report.recommendation.required is True

    registry = ModelRegistry(tmp_path / "registry", SimulatedClock(NOW_MS), limits=LIMITS)
    validate(registry, "1.0.0")
    promote(registry, "1.0.0")
    before = registry.inspect().production[ArtifactKind.THERMAL_MODEL]

    # 推奨を受けて学習し直した成果物を登録する（drift 検知はこれを**自分ではしない**）。
    candidate = registry.register_candidate(
        metadata("1.1.0"),
        payload("1.1.0"),
        actor="trainer",
        reason=report.recommendation.triggers[0].code,
    )
    snapshot = registry.inspect()

    assert snapshot.production[ArtifactKind.THERMAL_MODEL] == before
    assert snapshot.artifacts[candidate.key].status is ArtifactStatus.CANDIDATE


# ---------------------------------------------------------------- 11. 入口（CLI）


def test_cli_manifest_requires_an_explicit_window() -> None:
    """**「直近」のような相対指定を置かない**（時刻は明示する）。"""
    from coldaisle.drift import DriftEvidenceManifest

    with pytest.raises(ValidationError, match="start_ms < end_ms"):
        DriftEvidenceManifest.model_validate({"schema_version": 1, "start_ms": 100, "end_ms": 100})
    manifest = DriftEvidenceManifest.model_validate(
        {
            "schema_version": 1,
            "start_ms": BASE_TS_MS,
            "end_ms": BASE_TS_MS + STEP_MS,
            "changes": [{"kind": "calibration_changed", "ts_ms": BASE_TS_MS, "detail": "再較正"}],
        }
    )
    assert manifest.changes[0].kind is ChangeKind.CALIBRATION_CHANGED


def write_dataset(directory: Path, data, *, examples=None, manifest=None) -> None:
    """Dataset artifact を書き出す（manifest と examples を別々に壊せるように）。"""
    directory.mkdir(exist_ok=True)
    chosen = data.examples if examples is None else examples
    (directory / "manifest.json").write_bytes(
        (
            (manifest if manifest is not None else data.manifest).model_dump_json(indent=2) + "\n"
        ).encode()
    )
    from coldaisle.control.model.dataset import examples_jsonl_bytes

    (directory / "examples.jsonl").write_bytes(examples_jsonl_bytes(chosen))


def test_cli_refuses_a_dataset_whose_examples_do_not_match_the_manifest(trained, tmp_path) -> None:
    """**書き換えられた example を「範囲外で運転した証拠」にしない。**

    bytes（checksum）だけでなく、`ThermalDataset` の検証も通す。checksum は examples
    からしか作られないので、**manifest ごと差し替えた dataset は checksum では閉じない。**
    """
    from coldaisle.drift import load_inputs

    data, _parts, _model, _profile = trained
    span = {"start_ms": 0, "end_ms": BASE_TS_MS}
    directory = tmp_path / "dataset"
    write_dataset(directory, data)
    assert len(load_inputs(directory, **span)) == len(data.examples)

    write_dataset(directory, data, examples=data.examples[:-1])
    with pytest.raises(ValueError, match="checksum"):
        load_inputs(directory, **span)

    # checksum は examples から作るので、manifest だけを書き換えると通ってしまう。
    # `ThermalDataset` の検証がそれを閉じる。
    liar = data.manifest.model_copy(update={"example_count": len(data.examples) + 1})
    write_dataset(directory, data, manifest=liar)
    with pytest.raises(ValidationError, match="example_count"):
        load_inputs(directory, **span)


def test_cli_ignores_dataset_examples_outside_the_declared_window(trained, tmp_path) -> None:
    """**空の区間が、古い健全な example を借りられないようにする。**"""
    from coldaisle.drift import load_inputs

    data, _parts, _model, _profile = trained
    directory = tmp_path / "dataset"
    write_dataset(directory, data)
    action_times = sorted(example.action_ts_ms for example in data.examples)

    assert load_inputs(directory, start_ms=0, end_ms=action_times[0]) == ()
    inside = load_inputs(directory, start_ms=action_times[0], end_ms=action_times[3])
    assert tuple(item.action_ts_ms for item in inside) == tuple(action_times[:3])


def test_cli_refuses_a_shadow_export_that_does_not_match_the_recomputation(trained) -> None:
    """**渡された export は、同じ trace と観測から数え直した結果と1欄ずつ照らす。**

    識別子と許容幅だけを見ても `status` / `observed` / `error` / 時刻は書き換えられる
    （採点していない区間を `scored` に、外れた予測を当たりに仕立てられる）。
    """
    from coldaisle.drift import DriftEvidenceManifest, _check_supplied_shadow

    _data, _parts, _model, profile = trained
    computed = tuple(rows(profile, 3, ratio=1.0, start=2))
    manifest = DriftEvidenceManifest.model_validate(
        {
            "schema_version": 1,
            "start_ms": BASE_TS_MS + 2 * STEP_MS,
            "end_ms": BASE_TS_MS + 5 * STEP_MS,
        }
    )
    _check_supplied_shadow(computed, computed, manifest)

    tampered = (*computed[:2], rewrite_first_match(computed[2], error=0.0))
    with pytest.raises(ValueError, match="数え直した結果と違う"):
        _check_supplied_shadow(tampered, computed, manifest)

    with pytest.raises(ValueError, match="同じ tick の行が複数"):
        _check_supplied_shadow((*computed, computed[0]), computed, manifest)

    # 期間の外の行（古い健全な証拠）を借りない。
    old = rows(profile, 2, ratio=1.0, start=0)
    with pytest.raises(ValueError, match="期間の外"):
        _check_supplied_shadow((*old, *computed), computed, manifest)


# ---------------------------------------------------------------- 12. 記録された予測に束ねる


def test_recorded_prediction_values_cannot_be_rewritten_into_a_better_score(trained) -> None:
    """**誤差は記録された予測値から数え直す**（0056 §2.3）。

    記録の `predicted` / `error` をそのまま信じると、出力どうしで予測値を入れ替えたり
    「当たった」ことにしたりするだけで、degraded な residual を ok に変えられる。
    """
    _data, _parts, _model, profile = trained
    honest = rows(profile, 6, ratio=2.5)
    # 照合結果の側だけ「予測は完璧だった」と書き換える（型の検査は通る）。
    polished = tuple(
        item.model_copy(
            update={
                "outcomes": (
                    item.outcomes[0].model_copy(
                        update={
                            "matches": tuple(
                                match.model_copy(update={"predicted": match.observed, "error": 0.0})
                                for match in item.outcomes[0].matches
                            )
                        }
                    ),
                )
            }
        )
        for item in honest
    )
    report = detector(profile).detect(DriftEvidence(shadow=polished))

    assert report.residual.ratio == pytest.approx(2.5)
    assert report.residual.verdict is DriftVerdict.DEGRADED


def test_recorded_expected_times_must_match_the_prediction(trained) -> None:
    """期待時刻を書き換えて、別の step の実測を「この出力の当たり」にさせない。"""
    _data, _parts, _model, profile = trained
    source = row(profile, 0)
    moved = source.outcomes[0].matches[2].expected_ts_ms
    with pytest.raises(DriftInputError, match="期待時刻が予測と違う"):
        detector(profile).detect(
            DriftEvidence(shadow=(rewrite_first_match(source, expected_ts_ms=moved),))
        )


# ---------------------------------------------------------------- 13. 条件が digest を覆う


def test_repeated_declarations_do_not_change_the_report(trained) -> None:
    """同じ宣言を2度渡しても、報告の bytes は変わらない（0054 §2.6 と同じ扱い）。"""
    _data, _parts, _model, profile = trained
    change = DeclaredChange(kind=ChangeKind.FAN_REPLACED, ts_ms=BASE_TS_MS, detail="交換")
    shadow = rows(profile, 6, ratio=1.0, start=1)
    once = detector(profile).detect(DriftEvidence(shadow=shadow, changes=(change,)))
    twice = detector(profile).detect(DriftEvidence(shadow=shadow, changes=(change, change)))

    assert once.canonical_bytes() == twice.canonical_bytes()
    assert once.provenance.evidence_sha256 == twice.provenance.evidence_sha256
    assert once.declared_changes == (change,)

    # **同じ時刻・同じ種別で `detail` だけが違う宣言**も、並びが集合の反復順に委ねられない。
    same_time = tuple(
        DeclaredChange(kind=ChangeKind.SENSOR_REPLACED, ts_ms=BASE_TS_MS, detail=detail)
        for detail in ("rear", "front", "top")
    )
    first = detector(profile).detect(DriftEvidence(shadow=shadow, changes=same_time))
    again = detector(profile).detect(
        DriftEvidence(shadow=shadow, changes=tuple(reversed(same_time)))
    )
    assert first.canonical_bytes() == again.canonical_bytes()
    assert [item.detail for item in first.declared_changes] == ["front", "rear", "top"]


def test_the_declared_window_is_part_of_the_report(trained) -> None:
    """**違う期間を見た報告が同じ bytes を名乗れない**（0056 §2.7）。"""
    _data, _parts, _model, profile = trained
    shadow = rows(profile, 6, ratio=1.0)
    narrow = detector(profile).detect(
        DriftEvidence(
            shadow=shadow, window_start_ms=BASE_TS_MS, window_end_ms=BASE_TS_MS + 6 * STEP_MS
        )
    )
    wide = detector(profile).detect(
        DriftEvidence(
            shadow=shadow, window_start_ms=BASE_TS_MS, window_end_ms=BASE_TS_MS + 7 * STEP_MS
        )
    )

    assert narrow.provenance.window_end_ms == BASE_TS_MS + 6 * STEP_MS
    assert narrow.canonical_bytes() != wide.canonical_bytes()
    assert narrow.provenance.evidence_sha256 != wide.provenance.evidence_sha256
    with pytest.raises(DriftInputError, match="両端"):
        detector(profile).detect(DriftEvidence(shadow=shadow, window_start_ms=BASE_TS_MS))
    with pytest.raises(DriftInputError, match="start < end"):
        detector(profile).detect(
            DriftEvidence(shadow=shadow, window_start_ms=BASE_TS_MS, window_end_ms=BASE_TS_MS)
        )


def test_the_detector_itself_refuses_evidence_outside_the_declared_window(trained) -> None:
    """**期間の外の証拠は検知器の境界で閉じる**（CLI を通らない経路も同じ保証にする）。"""
    _data, parts, _model, profile = trained
    window = {"window_start_ms": BASE_TS_MS + STEP_MS, "window_end_ms": BASE_TS_MS + 3 * STEP_MS}
    with pytest.raises(DriftInputError, match="期間の外の Shadow 行"):
        detector(profile).detect(DriftEvidence(shadow=rows(profile, 4), **window))

    inside = rows(profile, 2, start=1)
    detector(profile).detect(DriftEvidence(shadow=inside, **window))
    stale = tuple(ObservedThermalInput.from_example(item) for item in parts.test)
    with pytest.raises(DriftInputError, match="期間の外の推論入力"):
        detector(profile).detect(DriftEvidence(shadow=inside, inputs=stale, **window))


# ---------------------------------------------------------------- 14. 写した上限で報告を落とさない


def test_a_wide_breakdown_is_capped_without_losing_the_count() -> None:
    """**種類が構造上限を超えても報告を作る**（#90 で見つかった「狭い写し」の型）。

    上限で例外にすると、正しい入力から報告が作れない組み合わせが生まれる。
    """
    from coldaisle.control.drift.detector import _counted_reasons, _source_counts
    from coldaisle.control.drift.model import MAX_DRIFT_REASONS

    counts = {f"source_{index:03d}": index + 1 for index in range(100)}
    sources = _source_counts(counts)
    assert len(sources) == MAX_DRIFT_REASONS
    assert sources[-1].source == "other_sources"
    assert sum(item.count for item in sources) == sum(counts.values())

    reasons = _counted_reasons(counts)
    assert len(reasons) == MAX_DRIFT_REASONS
    assert reasons[-1].code == "other_reasons"
    assert sum(item.count for item in reasons) == sum(counts.values())


def test_a_long_missing_pattern_still_fits_in_the_report() -> None:
    """metric 名は1つで 120 文字あるので、2つ並べるだけで表示名の上限を超える。"""
    from coldaisle.control.drift.detector import _pattern_name
    from coldaisle.control.drift.model import MAX_DRIFT_SOURCE_LENGTH, DriftSourceCount

    long_names = tuple(f"{'a' * 110}.{index:03d}" for index in range(4))
    name = _pattern_name(long_names)
    assert len(name) <= MAX_DRIFT_SOURCE_LENGTH
    assert DriftSourceCount(source=name, count=1).source == name
    # **切るだけにしない。** 別の組み合わせが同じ名前にならない。
    assert name != _pattern_name((*long_names[:3], f"{'a' * 110}.999"))
    assert _pattern_name(()) == "-"


def test_a_very_long_trend_is_coarsened_instead_of_failing(trained) -> None:
    """証拠が多くても報告は作れる。**bucket を粗くするだけ**で、判定の規則は変えない。"""
    from coldaisle.control.drift.detector import _ResidualOutcome
    from coldaisle.control.drift.model import MAX_DRIFT_TREND_BUCKETS

    _data, _parts, _model, profile = trained
    judge = detector(profile)
    size = drift_config().residual.trend_bucket_outcomes.value
    many = [
        _ResidualOutcome(
            evidence_ts_ms=BASE_TS_MS + index,
            inference_id=f"{index:064x}",
            mean_squared=1.0,
            outputs=1,
        )
        for index in range(size * MAX_DRIFT_TREND_BUCKETS + 1)
    ]
    buckets, bucket_size = judge._trend(many)

    assert len(buckets) <= MAX_DRIFT_TREND_BUCKETS
    assert bucket_size % size == 0 and bucket_size > size
    assert sum(bucket.outcomes for bucket in buckets) == len(many)


def test_declaring_too_many_changes_is_an_input_error(trained) -> None:
    """報告の型で落とすと理由が分からない。入力の問題として閉じる。"""
    from coldaisle.control.drift.model import MAX_DECLARED_CHANGES

    _data, _parts, _model, profile = trained
    changes = tuple(
        DeclaredChange(kind=ChangeKind.SENSOR_REPLACED, ts_ms=BASE_TS_MS + index)
        for index in range(MAX_DECLARED_CHANGES + 1)
    )
    with pytest.raises(DriftInputError, match="構造上限"):
        detector(profile).detect(DriftEvidence(changes=changes))
