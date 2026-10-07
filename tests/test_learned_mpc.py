"""#86 Learned MPC optimizer と Hard Constraints の連携。実機不要（合成 Dataset v2 / artifact v2）。

**ここでは「守れているか」ではなく「破れないか」を試す。** MPC の提案が Reactive Guard に
届くまでに成り立っていなければならない不変条件を並べ、1つずつ破ろうとする試験を置く。

1. 提案は**必ず1回の検証済み推論に束ねられている**（提案・assessment・解の identity が一致）
2. 内部モデルは **Registry 検証済み**の反実仮想 artifact v2 の封をした型だけ（決定記録 0079 §2.4）
3. optimizer が出せるのは **plan の最初の step だけ**（receding horizon）
4. 要求は **Hard Constraints の中**。Critical Safety の floor を下回れない
5. 実行不能なら**当て推量を返さない**（`ERROR` → Fallback）
6. budget / 評価回数を超えたら **`TIMEOUT`**。Gate はそれを active にしない
7. モデルの異常は **例外で落ちず** Fallback になり、次 tick は通常どおり動く
8. 低 Confidence / OOD の提案は Gate で落ち、trace に**裏付けの無い数値を残さない**
9. 同じ入力・同じ時計からは**同じ提案**が出る（replay 再現性）
10. 採用する解は内部モデルの上で **Baseline 以下のコスト**
11. `control/mpc` は Guard / Safety / Hardware を **import しない**
12. 別時刻の観測 window は使わない（#102 の Snapshot と時刻が揃わなければ失敗）
13. 学習した action 列（同梱 Profile v2 の step ごとの support）の外の候補は**評価しない**。
    Fallback の requested が外なら `plan_out_of_learned_range` で Fallback（0079 §2.5 / 0084 §2.2）
14. 読み込み時の検査（L1〜L12。較正の L9 を含む）に外れた artifact は**型にならず**、runtime は
    `MODEL_LOAD_FAILURE` として Fallback で運転を続ける（0079 §2.6 / 0096 §5 #8）

内部モデルの試験用 artifact は、`test_model_confidence` と同じ合成データから作った Dataset v2 で
学習し、計画 action の列の係数だけを決定論的な値（demand を上げるほど温度が下がる）に置き換えて
**本物の Model Registry に登録・昇格**したものを使う。係数の置き換えは試験のためで、本番の
artifact ではない。
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.control.acoustic import (
    AcousticCurvePoint,
    AcousticModelConfig,
    AcousticModelSource,
    ConfiguredAcousticCostModel,
    ZoneAcousticCurve,
)
from coldaisle.control.air_balance import (
    AirBalanceEstimate,
    AirBalanceMetadata,
    AirBalanceState,
    BalanceBand,
    CharacterizationSource,
    ThermalInputs,
)
from coldaisle.control.config import (
    MAX_MPC_HORIZON_STEPS,
    FanHardwareConfig,
    FanPolicyConfig,
    SafetyConfig,
)
from coldaisle.control.fallback import (
    FallbackCause,
    LearnedControlStatus,
    LearnedFailure,
)
from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.model.confidence import (
    ConfidenceProfileSpec,
    ResidualEvidence,
    SupportAxis,
    ValueRange,
    inference_id,
)
from coldaisle.control.model.counterfactual import (
    ActionCellCount,
    ActionCellTransitionCount,
    ActionSupportV2,
    ActionTrajectory,
    AnchorTransitionSupport,
    ConfidenceProfileV2,
    RegistryCounterfactualThermalModel,
    StepActionSupport,
    StepTransitionSupport,
)
from coldaisle.control.model.counterfactual_confidence import fit_confidence_profile_v2
from coldaisle.control.model.counterfactual_training import (
    CounterfactualTrainedModel,
    CounterfactualTrainingSpec,
    VerifiedTrainingDatasetArtifactV2,
    assemble_counterfactual_artifact,
    train_counterfactual_ridge,
    verify_training_dataset_artifact_v2,
)
from coldaisle.control.model.dataset import (
    ActionExclusionCounts,
    ActionStepV2,
    DatasetExample,
    DatasetExampleV2,
    DatasetManifestV2,
    DatasetSpecV2,
    DatasetSplitV2,
    PriorAction,
    ThermalDatasetV2,
    examples_jsonl_bytes,
    examples_sha256,
    split_temporally_v2,
)
from coldaisle.control.model.thermal import (
    MAX_TARGET_HORIZONS,
    ArtifactVerification,
    InferenceCapability,
    ObservedThermalInput,
    ThermalFeatureSchema,
    ThermalPrediction,
    ThermalTargetSchema,
    canonical_artifact_bytes,
)
from coldaisle.control.model_registry import (
    ApprovalAction,
    ArtifactAttestation,
    ArtifactCapability,
    ArtifactFormat,
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    ArtifactStatus,
    HumanApproval,
    ModelCompatibility,
    ModelRegistry,
    VerifiedArtifact,
    load_model_registry_limits,
)
from coldaisle.control.mpc import (
    ActionPlan,
    CounterfactualModelIdentity,
    HardConstraintSet,
    InfeasiblePlanError,
    LearnedMpcController,
    LearnedMpcOptimizer,
    LearnedMpcRuntime,
    MpcCostModel,
    MpcCostUnusableError,
    MpcModelBinding,
    MpcModelUnusableError,
    MpcProposal,
    PlannedTarget,
    PlannedThermalInput,
    PlanPrediction,
    PlanStep,
)
from coldaisle.control.schema import (
    STAGE_ORDER,
    AuthorityStage,
    ConfidenceLevel,
    ControllerKind,
    ControllerProposal,
    Demand,
    OperatingMode,
    OptimizerStatus,
    PerZone,
    Reason,
    SafetyState,
    StaticAuthorityStage,
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyKind,
    SupervisorTargetBand,
    TemperatureTarget,
    WorkloadRegime,
    Zone,
)
from coldaisle.control.state import ControlStateSnapshot, TelemetryHealth
from coldaisle.metrics import MetricCatalog
from test_control_config import valid_documents
from test_critical_safety import safety_config
from test_fallback_controller import fallback_proposal, gate_for, policy
from test_model_confidence import (
    AIR,
    GPU,
    TEST_START_MS,
    VALIDATION_START_MS,
    dataset,
    evidence,
    shifted,
)
from test_model_confidence import split as split_v1
from test_model_confidence import train as train_v1
from test_thermal_model_v2 import document, encode, metadata_for, reseal

CPU = "cpu.package"
FEATURES = (AIR, GPU)
HORIZONS = (1_000, 2_000, 3_000)
TARGETS = (CPU, GPU)
STEP_MS = 1_000
STEPS = len(HORIZONS)
ACTION_TS_MS = 10_000 + 3 * 5_000
"""試験に使う anchor の時刻（`test_model_confidence` の合成 dataset の刻み）。"""

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
CATALOG = MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")
"""runtime の `config/metrics.yaml`（L8 の照合相手）。"""
CALIBRATION_OFFSETS: dict[str, float] = {"front_intake": 0.191, "room_temp": -0.31}
"""学習時の較正の値。試験用 artifact で較正の掛かる metric は ``air.front_intake`` だけ。"""
RUNTIME_CALIBRATION = RuntimeCalibration.available(CALIBRATION_OFFSETS)
UNAVAILABLE_CALIBRATION = RuntimeCalibration.unavailable("試験: 較正ファイルを読めなかった")
GAIN = 30.0
"""計画 action の列の係数（3 zone の demand を 1 上げると予測温度が GAIN だけ下がる）。"""
FAN_EDGES = (0.5,)
"""support 軸 `fan.<zone>` の bin の境界（cell は zone ごとに 0.5 未満 / 以上の2つ）。"""
MODEL_ID = "rack-thermal"
DATASET_ALIAS = "dataset-00000000000000000000000000000086"
REGISTRY_LIMITS = load_model_registry_limits(CONFIG_DIR)
ALL_STAGES: tuple[AuthorityStage, ...] = STAGE_ORDER
Cell = tuple[int, int, int]
CELLS: tuple[Cell, ...] = tuple(
    (front, rear, top) for front in (0, 1) for rear in (0, 1) for top in (0, 1)
)
EVERY_TRANSITION: tuple[tuple[Cell, Cell], ...] = tuple(
    (source, target) for source in CELLS for target in CELLS
)


# ---------------------------------------------------------------- 合成 Dataset v2


def _as_v2(item: DatasetExample, index: int) -> DatasetExampleV2:
    """v1 の合成 example に、anchor の effective を保った action 列を足す（決定論的）。"""
    tick = 10 + 5 * index
    fan = item.action.front.effective_demand
    current = PerZone[float](front=fan, rear=fan, top=fan)
    return DatasetExampleV2(
        example_id=item.example_id,
        source_run_id=item.source_run_id,
        history_start_ms=item.history_start_ms,
        action_ts_ms=item.action_ts_ms,
        label_end_ms=item.label_end_ms,
        control_tick_id=tick,
        control_schema_version=12,
        window=item.window,
        action=item.action,
        context=item.context,
        prior_action=PriorAction(
            source_ts_ms=item.action_ts_ms - 1_000,
            source_tick_id=tick - 1,
            effective_demand=current,
        ),
        action_steps=tuple(
            ActionStepV2(
                step=step,
                ts_ms=item.action_ts_ms + step * STEP_MS,
                source_ts_ms=item.action_ts_ms + step * STEP_MS,
                source_tick_id=tick + step,
                effective_demand=current,
            )
            for step in range(STEPS)
        ),
        targets=item.targets,
    )


def thermal_dataset_v2(features: tuple[str, ...] = FEATURES) -> ThermalDatasetV2:
    """`test_model_confidence` と同じ観測・label の Dataset v2（``features`` に絞れる）。"""
    data = dataset(HORIZONS, TARGETS)
    examples = tuple(
        _narrowed(_as_v2(item, index), features) for index, item in enumerate(data.examples)
    )
    spec = data.manifest.spec
    return ThermalDatasetV2(
        manifest=DatasetManifestV2(
            spec=DatasetSpecV2(
                window_ms=spec.window_ms,
                sample_period_ms=spec.sample_period_ms,
                horizons_ms=HORIZONS,
                target_tolerance_ms=spec.target_tolerance_ms,
                stale_after_ms=spec.stale_after_ms,
                feature_metrics=features,
                target_metrics=TARGETS,
                action_step_ms=STEP_MS,
                action_steps=STEPS,
                action_stale_after_ms=2_000,
            ),
            source_runs=data.manifest.source_runs,
            telemetry_sha256=data.manifest.telemetry_sha256,
            control_trace_sha256=data.manifest.control_trace_sha256,
            examples_sha256=examples_sha256(examples),
            example_count=len(examples),
            excluded=ActionExclusionCounts(
                stale=0, discontinuity=0, in_step_change=0, restart=0, tick_id_gap=0
            ),
        ),
        examples=examples,
    )


def _narrowed(item: DatasetExampleV2, features: tuple[str, ...]) -> DatasetExampleV2:
    if features == FEATURES:
        return item
    fields = ("values", "source_ts_ms", "quality", "missing_mask", "stale_mask")
    frames = tuple(
        frame.model_copy(
            update={
                name: {metric: getattr(frame, name)[metric] for metric in features}
                for name in fields
            }
        )
        for frame in item.window
    )
    return DatasetExampleV2.model_validate(
        item.model_copy(update={"window": frames}).model_dump(mode="python")
    )


@dataclass(frozen=True)
class _Training:
    trained: CounterfactualTrainedModel
    source: VerifiedTrainingDatasetArtifactV2
    split: DatasetSplitV2


@cache
def _training(features: tuple[str, ...] = FEATURES) -> _Training:
    """学習は feature の組ごとに1回だけ（同じ合成データから同じ係数）。"""
    data = thermal_dataset_v2(features)
    parts = split_temporally_v2(
        data.examples, validation_start_ms=VALIDATION_START_MS, test_start_ms=TEST_START_MS
    )
    directory = Path(tempfile.mkdtemp(prefix="pr86-dataset")) / DATASET_ALIAS
    directory.mkdir()
    (directory / "manifest.json").write_bytes(
        (data.manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    )
    (directory / "examples.jsonl").write_bytes(examples_jsonl_bytes(data.examples))
    source = verify_training_dataset_artifact_v2(
        data, directory / "manifest.json", directory / "examples.jsonl"
    )
    trained_model = train_counterfactual_ridge(
        source,
        parts,
        CounterfactualTrainingSpec(
            model_id=MODEL_ID,
            model_version="0.1.0",
            created_at="2026-10-07T10:00:00+09:00",
            ridge_lambda=0.1,
            authority_compatibility=ALL_STAGES,
            calibration_offsets_c=dict(CALIBRATION_OFFSETS),
            code_commit="0123456789abcdef",
        ),
        metric_catalog=CATALOG,
    )
    return _Training(trained=trained_model, source=source, split=parts)


# ---------------------------------------------------------------- Profile v2 と係数


def ranges(low: float, high: float, *, count: int = 10_000) -> PerZone[ValueRange]:
    """3 zone とも同じ観測範囲。"""
    return PerZone[ValueRange](
        front=ValueRange(source="fan.front", minimum=low, maximum=high, observed_count=count),
        rear=ValueRange(source="fan.rear", minimum=low, maximum=high, observed_count=count),
        top=ValueRange(source="fan.top", minimum=low, maximum=high, observed_count=count),
    )


StepItem = tuple[PerZone[ValueRange], Sequence[Cell]]
PairItem = tuple[PerZone[ValueRange], Sequence[tuple[Cell, Cell]]]


def action_support(
    *,
    steps: Sequence[StepItem] | None = None,
    anchor: PairItem | None = None,
    pairs: Sequence[PairItem] | None = None,
) -> ActionSupportV2:
    """step ごと・step の組ごとの support。省いた欄は「全範囲・全 cell・全遷移」を観測した扱い。"""
    step_items = steps or tuple((ranges(0.0, 1.0), CELLS) for _ in range(STEPS))
    anchor_item = anchor or (ranges(-1.0, 1.0), EVERY_TRANSITION)
    pair_items = pairs or tuple((ranges(-1.0, 1.0), EVERY_TRANSITION) for _ in range(STEPS - 1))
    return ActionSupportV2(
        steps=tuple(
            StepActionSupport(
                step=index,
                demand_ranges=zone_ranges,
                cells=tuple(ActionCellCount(cell=cell, count=1) for cell in cells),
            )
            for index, (zone_ranges, cells) in enumerate(step_items)
        ),
        anchor_to_first=AnchorTransitionSupport(
            delta_ranges=anchor_item[0],
            cells=tuple(
                ActionCellTransitionCount(source=source, target=target, count=1)
                for source, target in anchor_item[1]
            ),
        ),
        transitions=tuple(
            StepTransitionSupport(
                from_step=index,
                delta_ranges=delta_ranges,
                cells=tuple(
                    ActionCellTransitionCount(source=source, target=target, count=1)
                    for source, target in transitions
                ),
            )
            for index, (delta_ranges, transitions) in enumerate(pair_items)
        ),
    )


def profile_spec(features: tuple[str, ...] = FEATURES) -> ConfidenceProfileSpec:
    """`test_model_confidence` の support 軸に、全 zone の `fan.<zone>` 軸を足したもの。"""
    observed = {
        AIR: SupportAxis(source=AIR, edges=(22.0, 24.0, 26.0)),
        GPU: SupportAxis(source=GPU, edges=(44.0, 48.0, 52.0)),
    }
    return ConfidenceProfileSpec(
        support_axes=(
            *(observed[metric] for metric in features),
            SupportAxis(source="fan.front", edges=FAN_EDGES),
            SupportAxis(source="fan.rear", edges=FAN_EDGES),
            SupportAxis(source="fan.top", edges=FAN_EDGES),
        ),
        residual_scale_floor=0.01,
    )


def _artifact_bytes(
    *,
    version: str,
    authority: tuple[AuthorityStage, ...],
    support: ActionSupportV2 | None,
    features: tuple[str, ...],
    edit: Callable[[dict[str, Any]], None] | None,
) -> bytes:
    training = _training(features)
    trained_model = training.trained.model_copy(
        update={"model_version": version, "authority_compatibility": authority}
    )
    fitted = fit_confidence_profile_v2(
        training.trained, training.source, training.split, profile_spec(features)
    )
    # 学習データの action 列は anchor を保つだけなので、候補の探索を試すために action の support を
    # 置き換える（anchor 推論の判定に使う範囲・cell・residual の基準は生成したまま）。
    profile = ConfidenceProfileV2.model_validate(
        fitted.model_dump(mode="python")
        | {
            "action_support": (support or action_support()).model_dump(mode="python"),
            "binding": trained_model.profile_binding().model_dump(mode="python"),
        }
    )
    doc = document(assemble_counterfactual_artifact(trained_model, profile))
    _plan_gain(doc)
    if edit is not None:
        edit(doc)
    return encode(reseal(doc))


def _plan_gain(doc: dict[str, Any]) -> None:
    """計画 action の列の係数を「demand を上げるほど温度が下がる」決定論的な値にする。

    horizon `h` の出力が使ってよい step（`k × step_ms < h`。因果の mask）の列だけに置く。
    """
    columns = doc["feature_schema"]["columns"]
    plan_start = len(columns) - STEPS * 3
    scales = doc["payload"]["feature_scales"]
    for output in doc["payload"]["outputs"]:
        allowed = sum(1 for step in range(STEPS) if step * STEP_MS < output["horizon_ms"])
        for step in range(STEPS):
            for zone in range(3):
                index = plan_start + step * 3 + zone
                output["coefficients"][index] = (
                    -GAIN * scales[index] / (3 * allowed) if step < allowed else 0.0
                )


def _promote(
    root: Path, metadata: ArtifactMetadata, payload: bytes, *, stage: AuthorityStage, promoted: bool
) -> VerifiedArtifact:
    registry = ModelRegistry(root, limits=REGISTRY_LIMITS)
    registry.register_candidate(metadata, payload, actor="trainer", reason="training completed")
    registry.mark_validated(
        metadata.ref,
        offline_evaluation_ref="evaluation/offline/86",
        actor="evaluator",
        reason="offline gates passed",
        expected_revision=registry.inspect().revision,
    )
    compatibility = ModelCompatibility(
        feature_schema_version=metadata.feature_schema_version,
        target_schema_version=metadata.target_schema_version,
        authority_stage=stage,
    )
    if promoted:
        revision = registry.inspect().revision
        registry.promote(
            metadata.ref,
            compatibility,
            shadow_evaluation_ref="evaluation/shadow/86",
            approval=HumanApproval(
                action=ApprovalAction.PROMOTE,
                artifact=metadata.ref,
                artifact_sha256=metadata.sha256,
                expected_revision=revision,
                approver="model-operator",
                approved_at_ms=1_700_000_000_000,
                reason="shadow evaluation passed",
            ),
            expected_revision=revision,
        )
    result = registry.load_version(metadata.ref, compatibility)
    assert result.artifact is not None, result.detail
    return result.artifact


def register_v2(
    root: Path,
    *,
    version: str = "0.1.0",
    authority: tuple[AuthorityStage, ...] = ALL_STAGES,
    stage: AuthorityStage = AuthorityStage.FULL,
    promoted: bool = True,
    support: ActionSupportV2 | None = None,
    features: tuple[str, ...] = FEATURES,
    edit: Callable[[dict[str, Any]], None] | None = None,
) -> VerifiedArtifact:
    """**本物の Model Registry（#104）に登録し、検証経路から `VerifiedArtifact` を受け取る。**"""
    payload = _artifact_bytes(
        version=version, authority=authority, support=support, features=features, edit=edit
    )
    return _promote(root, metadata_for(payload), payload, stage=stage, promoted=promoted)


def register_payload(
    root: Path, payload: bytes, *, stage: AuthorityStage = AuthorityStage.FULL
) -> VerifiedArtifact:
    """artifact v2 の bytes をそのまま別の Registry へ登録・昇格する（同じ bytes・同じ hash）。"""
    return _promote(root, metadata_for(payload), payload, stage=stage, promoted=True)


@dataclass(frozen=True)
class MpcArtifact:
    """Registry が発行した反実仮想 artifact v2 と、その registry の場所。"""

    verified: VerifiedArtifact
    root: Path

    @property
    def attestation(self) -> ArtifactAttestation:
        """Registry 発行の証拠。"""
        return self.verified.attestation

    @property
    def model(self) -> RegistryCounterfactualThermalModel:
        """同じ bytes から作った封をした型（試験で予測や Profile を読むため）。"""
        return RegistryCounterfactualThermalModel.from_verified_artifact(
            self.verified, metric_catalog=CATALOG, calibration=RUNTIME_CALIBRATION
        )

    @property
    def profile(self) -> ConfidenceProfileV2:
        """同梱 Profile v2。"""
        return self.model.confidence_profile


def make_artifact(root: Path, **kwargs: Any) -> MpcArtifact:
    """`register_v2` の結果を `MpcArtifact` に包む。"""
    return MpcArtifact(verified=register_v2(root, **kwargs), root=root)


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory) -> MpcArtifact:
    """production に昇格した反実仮想 artifact v2（全 stage 互換・広い action の support）。"""
    return make_artifact(tmp_path_factory.mktemp("pr86-registry") / "registry")


def issue_verified(
    root: Path,
    *,
    model_id: str = MODEL_ID,
    version: str = "0.1.0",
    authority: tuple[AuthorityStage, ...] = ALL_STAGES,
    feature_schema_version: str = "thermal-features-v1",
    target_schema_version: str = "thermal-targets-v1",
    kind: ArtifactKind = ArtifactKind.THERMAL_MODEL,
    stage: AuthorityStage = AuthorityStage.FULL,
    promoted: bool = True,
    payload: bytes | None = None,
    capability: ArtifactCapability = ArtifactCapability.COUNTERFACTUAL_ACTION,
) -> VerifiedArtifact:
    """任意の payload を本物の Registry に登録し、`VerifiedArtifact` を受け取る。"""
    payload = (
        payload or json.dumps({"model": model_id, "version": version}, sort_keys=True).encode()
    )
    metadata = ArtifactMetadata(
        kind=kind,
        artifact_format=ArtifactFormat.JSON,
        capability=capability,
        model_id=model_id,
        version=version,
        created_at="2026-09-20T10:00:00+09:00",
        training_dataset_version="dataset-00000000000000000000000000000086",
        source_runs=("run-00000000000000000000000000000086",),
        feature_schema_version=feature_schema_version,
        target_schema_version=target_schema_version,
        code_commit="0123456789abcdef",
        sha256=sha256(payload).hexdigest(),
        model_family="ridge_linear_v1",
        hyperparameters={"ridge_lambda": 0.1},
        authority_compatibility=authority,
    )
    return _promote(root, metadata, payload, stage=stage, promoted=promoted)


def issue_attestation(root: Path, **kwargs: Any) -> ArtifactAttestation:
    """`issue_verified` の attestation だけを返す（他の試験が使う）。

    #105 の学習 dynamics（0079 段 6 で v2 の型へ切り替える）・fand の registry 束縛・
    Gate の試験は、artifact の中身を読まずに attestation だけを使う。
    """
    return issue_verified(root, **kwargs).attestation


# ---------------------------------------------------------------- 試験用の部品


class ScriptedClock:
    """試験用の単調時計。値を使い切ったら最後の値を返し続ける。"""

    def __init__(self, *values: int) -> None:
        self._values = list(values) or [0]
        self._index = 0

    def __call__(self) -> int:
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def optimizer_document(**overrides: object) -> dict[str, object]:
    """試験用の `mpc.optimizer` 設定。格子は試験用 artifact の action schema と同じ。"""
    values: dict[str, object] = {
        "horizon_ms": _provisional(STEP_MS * len(HORIZONS)),
        "step_ms": _provisional(STEP_MS),
        "candidate_levels": _provisional(5),
        "sweeps": _provisional(2),
        "max_evaluations": _provisional(64),
        "max_step_up": _provisional(1.0),
        "max_step_down": _provisional(1.0),
        "zone_bounds": {
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(1.0)} for zone in Zone
        },
        "cost_scales": {
            "temperature_c": _provisional(5.0),
            "balance_ratio": _provisional(0.2),
            "acoustic_cost": _provisional(10.0),
            "demand_change": _provisional(0.5),
        },
        "cost_metrics": {"cpu_temperature": CPU, "gpu_temperature": GPU},
        "unknown_balance_cost": _provisional(1.0),
    }
    values.update(overrides)
    return values


def _provisional(value: float | int) -> dict[str, object]:
    return {"value": value, "status": "provisional"}


def mpc_policy(
    *,
    authority: str = "full",
    budget_ms: int = 1_000,
    **optimizer: object,
) -> FanPolicyConfig:
    """#86 の設定を差し替えた fan-policy。"""
    return policy(
        authority=authority,
        mpc={
            "period_ms": 1_000,
            "budget_ms": budget_ms,
            "valid_ms": 2_000,
            "optimizer": optimizer_document(**optimizer),
        },
    )


def supervisor_output(*, balanced: bool = True) -> SupervisorOutput:
    """MPC が context として読む Supervisor 出力。"""
    weights = SupervisorObjectiveWeights(
        gpu_temperature=1.0,
        cpu_temperature=0.2,
        balance=0.0,
        acoustic=0.2 if balanced else 0.0,
        change=0.1 if balanced else 0.0,
    )
    return SupervisorOutput(
        snapshot_schema_version=1,
        tick_id=7,
        ts_ms=ACTION_TS_MS,
        policy=SupervisorPolicyKind.RULE,
        version="rule-1",
        regime=WorkloadRegime.SUSTAINED_GPU,
        regime_confidence=0.9,
        weights=weights,
        strategy="balanced",
        # 合成 dataset の予測は target band より高い。冷却を強めるほどコストが下がる局面を作る。
        target_band=SupervisorTargetBand(
            cpu_temperature=TemperatureTarget(lower_c=10.0, upper_c=25.0),
            gpu_temperature=TemperatureTarget(lower_c=20.0, upper_c=30.0),
        ),
        computed_at_ms=ACTION_TS_MS,
    )


def snapshot(ts_ms: int = ACTION_TS_MS) -> ControlStateSnapshot:
    """MPC が共有入力として受け取る #102 Snapshot。"""
    return ControlStateSnapshot(
        tick_id=7,
        ts_ms=ts_ms,
        monotonic_ms=ts_ms,
        signals=(),
        derived=(),
        trends=(),
        telemetry_health=TelemetryHealth.NORMAL,
        critical_unavailable=(),
    )


def safety() -> SafetyConfig:
    """floor を予測しやすくした Safety 設定（zone ごとの差はここでは見ない）。"""
    return safety_config(uniform_zone_min=0.2)


@cache
def observed_example() -> ObservedThermalInput:
    """anchor 時刻で終わる合成 window（同じ dataset を毎回作り直さない）。"""
    data = dataset(HORIZONS, TARGETS)
    example = next(item for item in data.examples if item.action_ts_ms == ACTION_TS_MS)
    return ObservedThermalInput.from_example(example)


def demands(value: float) -> PerZone[Demand]:
    """3 zone とも同じ demand。"""
    return PerZone[Demand](front=value, rear=value, top=value)


def observed_input(action: float | None = None) -> ObservedThermalInput:
    """anchor 時刻で終わる観測 window。``action`` を省くと dataset の実際の action を使う。"""
    base = observed_example()
    return base if action is None else _with_action(base, action)


def _with_action(observed: ObservedThermalInput, demand: float) -> ObservedThermalInput:
    return ObservedThermalInput.model_validate(
        observed.model_dump(mode="python")
        | {"action": {zone.value: {"effective_demand": demand} for zone in Zone}}
    )


def bind(
    trained: MpcArtifact,
    *,
    authority_stage: AuthorityStage = AuthorityStage.FULL,
    calibration: RuntimeCalibration = RUNTIME_CALIBRATION,
    expected_model_version: str | None = None,
    catalog: MetricCatalog = CATALOG,
) -> MpcModelBinding:
    """`VerifiedArtifact` から束縛を作る（runtime と同じ1つの呼び出し）。"""
    return MpcModelBinding.from_verified_artifact(
        trained.verified,
        metric_catalog=catalog,
        calibration=calibration,
        authority_stage=authority_stage,
        expected_model_version=expected_model_version or trained.attestation.version,
    )


def build_controller(
    trained: MpcArtifact,
    *,
    policy_config: FanPolicyConfig | None = None,
    clock: ScriptedClock | None = None,
    acoustic: bool = False,
    authority_stage: AuthorityStage | None = None,
) -> tuple[LearnedMpcController, MpcModelBinding, FanPolicyConfig]:
    """束縛済みの MPC controller を組み立てる。"""
    settings = policy_config or mpc_policy()
    binding = bind(trained, authority_stage=settings.authority_stage)
    controller = LearnedMpcController(
        binding,
        settings,
        safety(),
        monotonic_ms=clock or ScriptedClock(0),
        # #92: 実効 stage は journal が決める。試験では設定の stage をそのまま使う。
        authority=StaticAuthorityStage(authority_stage or settings.authority_stage),
        acoustic=acoustic_model() if acoustic else None,
    )
    return controller, binding, settings


def load_runtime(
    verified: VerifiedArtifact,
    *,
    settings: FanPolicyConfig | None = None,
    calibration: RuntimeCalibration = RUNTIME_CALIBRATION,
    expected_model_version: str | None = None,
    catalog: MetricCatalog = CATALOG,
) -> LearnedMpcRuntime:
    """worker と同じ入口（`LearnedMpcRuntime.load`）で読み込む。"""
    config = settings or mpc_policy()
    return LearnedMpcRuntime.load(
        verified,
        config,
        safety(),
        metric_catalog=catalog,
        calibration=calibration,
        expected_model_version=expected_model_version or verified.attestation.version,
        monotonic_ms=ScriptedClock(0),
        authority=StaticAuthorityStage(config.authority_stage),
    )


def acoustic_model() -> ConfiguredAcousticCostModel:
    """demand に対して単調に増える近似音響コスト。"""
    curve = ZoneAcousticCurve(
        points=(
            AcousticCurvePoint(demand=0.0, acoustic_cost=0.0),
            AcousticCurvePoint(demand=1.0, acoustic_cost=40.0),
        )
    )
    config = AcousticModelConfig(
        schema_version=1,
        model_id="test-acoustic",
        source=AcousticModelSource(kind="approximate", basis="試験用の近似"),
        zones=PerZone[ZoneAcousticCurve](front=curve, rear=curve, top=curve),
    )
    return ConfiguredAcousticCostModel(config, "f" * 64)


def propose(
    controller: LearnedMpcController | LearnedMpcRuntime,
    *,
    action: float | None = None,
    baseline: float = 0.4,
    safety_floor: float = 0.2,
    ts_ms: int = ACTION_TS_MS,
    observed: ObservedThermalInput | None = None,
    residual: ResidualEvidence | None = None,
) -> MpcProposal:
    """1 tick 分の提案を作る。"""
    window = observed if observed is not None else observed_input(action)
    return controller.propose(
        snapshot=snapshot(ts_ms),
        observed=window,
        supervisor=supervisor_output(),
        baseline=fallback_proposal(baseline),
        safety_floor=demands(safety_floor),
        residual=residual,
    )


def select_with(
    settings: FanPolicyConfig,
    attestation: ArtifactAttestation,
    result: MpcProposal,
    *,
    fallback: ControllerProposal | None = None,
    now_mono_ms: int = 0,
) -> Any:
    """Gate（#79）に1回だけ選ばせる。期待値は runtime と同じく attestation から取る。"""
    gate = gate_for(
        settings,
        expected_model_version=attestation.version,
        expected_artifact_sha256=attestation.artifact_sha256,
    )
    return gate.select(
        now_mono_ms=now_mono_ms,
        fallback=fallback or fallback_proposal(0.4),
        learned=result.to_status(received_at_mono_ms=now_mono_ms),
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.NORMAL,
    )


@pytest.fixture
def plan_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[ActionPlan]]:
    """`predict_plan` に渡った候補 plan を順に記録する（評価した候補の数と中身）。"""
    seen: list[ActionPlan] = []
    original = MpcModelBinding.predict_plan

    def recording(
        self: MpcModelBinding, planned: PlannedThermalInput, *, anchor: ThermalPrediction
    ) -> PlanPrediction:
        seen.append(planned.plan)
        return original(self, planned, anchor=anchor)

    monkeypatch.setattr(MpcModelBinding, "predict_plan", recording)
    yield seen


Rewrite = Callable[[PlannedThermalInput, PlanPrediction], PlanPrediction]


def break_predict_plan(monkeypatch: pytest.MonkeyPatch, replace: Rewrite | BaseException) -> None:
    """`predict_plan` の結果を差し替える（取り違え・異常の再現）。"""
    original = MpcModelBinding.predict_plan

    def broken(
        self: MpcModelBinding, planned: PlannedThermalInput, *, anchor: ThermalPrediction
    ) -> PlanPrediction:
        if isinstance(replace, BaseException):
            raise replace
        return replace(planned, original(self, planned, anchor=anchor))

    monkeypatch.setattr(MpcModelBinding, "predict_plan", broken)


def rewritten(prediction: PlanPrediction, **updates: object) -> PlanPrediction:
    """予測の一部を書き換えた複製（検証を通す）。"""
    return PlanPrediction.model_validate(prediction.model_dump(mode="python") | updates)


# ------------------------------------- #105 の学習 dynamics の試験用（0079 段 6 まで v1 のまま）


class PlanningModel:
    """**#105 の学習 dynamics の試験だけ**が使う、v1 の model を包んだ試験用の反実仮想モデル。

    MPC の束縛（`MpcModelBinding`）はこの型を受け取らない（決定記録 0079 段 4）。
    `AttestedThermalDynamics.bind` を段 2 の型へ切り替えるのは段 6（#105）。
    """

    def __init__(
        self,
        base: object,
        *,
        capability: InferenceCapability = InferenceCapability.COUNTERFACTUAL_ACTION,
        model_id: str | None = None,
        model_version: str | None = None,
        gain: float = GAIN,
    ) -> None:
        self._base = base
        self._identity = CounterfactualModelIdentity(
            model_id=model_id or base.manifest.model_id,  # type: ignore[attr-defined]
            model_version=(
                model_version or base.manifest.model_version  # type: ignore[attr-defined]
            ),
            capability=capability,
        )
        self._gain = gain

    @property
    def identity(self) -> CounterfactualModelIdentity:
        """束縛の判断に使う identity。"""
        return self._identity

    @property
    def feature_schema(self) -> ThermalFeatureSchema:
        """入力契約。"""
        return self._base.feature_schema  # type: ignore[attr-defined,no-any-return]

    @property
    def target_schema(self) -> ThermalTargetSchema:
        """出力契約。"""
        return self._base.target_schema  # type: ignore[attr-defined,no-any-return]

    def predict(self, observed: ObservedThermalInput) -> ThermalPrediction:
        """anchor 推論。artifact の出どころだけ Registry 検証済みにそろえる。"""
        prediction = self._base.predict(observed)  # type: ignore[attr-defined]
        return ThermalPrediction.model_validate(
            prediction.model_dump(mode="python")
            | {"artifact_verification": ArtifactVerification.REGISTRY_VERIFIED}
        )

    def predict_plan(self, planned: PlannedThermalInput) -> PlanPrediction:
        """候補 action 列に対する単調な応答を返す。"""
        anchor = self.predict(planned.observed)
        anchor_mean = _mean(
            tuple(planned.observed.action.get(zone).effective_demand for zone in Zone)
        )
        by_offset = {target.horizon_ms: target.values for target in anchor.targets}
        targets = []
        for index, step in enumerate(planned.plan.steps):
            delta = _mean(tuple(step.demands.get(zone) for zone in Zone)) - anchor_mean
            weight = (index + 1) / len(planned.plan.steps)
            targets.append(
                PlannedTarget(
                    offset_ms=step.offset_ms,
                    expected_ts_ms=planned.observed.action_ts_ms + step.offset_ms,
                    values={
                        metric: value - self._gain * delta * weight
                        for metric, value in by_offset[step.offset_ms].items()
                    },
                )
            )
        return PlanPrediction(
            model_id=anchor.model_id,
            model_version=anchor.model_version,
            artifact_sha256=anchor.artifact_sha256,
            artifact_verification=anchor.artifact_verification,
            capability=InferenceCapability.COUNTERFACTUAL_ACTION,
            anchor_inference_id=inference_id(planned.observed, anchor),
            input_action_ts_ms=anchor.input_action_ts_ms,
            plan_digest=planned.plan.digest(),
            targets=tuple(targets),
        )


@cache
def v1_model() -> Any:
    """#84 の v1 artifact（`observational_replay`）。MPC に束縛できないことを試すために使う。"""
    data = dataset(HORIZONS, TARGETS)
    return train_v1(data, split_v1(data))


def register_v1(root: Path, *, capability: ArtifactCapability) -> VerifiedArtifact:
    """v1 artifact の bytes を本物の Registry に登録する（capability は申告どおり）。"""
    base = v1_model()
    return issue_verified(
        root,
        model_id=base.manifest.model_id,
        version=base.manifest.model_version,
        capability=capability,
        payload=canonical_artifact_bytes(base._artifact),
    )


# ---------------------------------------------------------------- 不変条件 2: 内部モデル


def test_invariant_2_a_dataset_v1_artifact_is_refused_as_the_internal_model(
    tmp_path: Path,
) -> None:
    """**観測再生だけの artifact を MPC の内部モデルにしない**（決定記録 0048 §2.1 / 0079 §2.2）。

    v1 の bytes は、申告が `observational_replay` なら L1、`counterfactual_action` と偽って
    登録しても L4（v1 を v2 として読み替えない）で拒まれる。
    """
    observational = register_v1(
        tmp_path / "observational", capability=ArtifactCapability.OBSERVATIONAL_REPLAY
    )
    assert observational.attestation.capability is ArtifactCapability.OBSERVATIONAL_REPLAY
    with pytest.raises(MpcModelUnusableError, match=r"（L1）.*反実仮想を申告していない"):
        bind(MpcArtifact(observational, tmp_path))

    claimed = register_v1(tmp_path / "claimed", capability=ArtifactCapability.COUNTERFACTUAL_ACTION)
    with pytest.raises(MpcModelUnusableError, match=r"（L4）"):
        bind(MpcArtifact(claimed, tmp_path))


def test_invariant_2_b_verification_comes_from_the_registry_not_from_the_model(trained) -> None:
    """**モデルの自称ではなく、Registry が発行した証拠に束縛する。**

    `ArtifactAttestation` / `VerifiedArtifact` は #104 の検証経路だけが発行する。束縛は
    `VerifiedArtifact` を1つ受け取る入口しか持たない（決定記録 0079 §2.4）。
    """
    with pytest.raises(TypeError, match="検証経路"):
        ArtifactAttestation()

    binding = bind(trained)

    assert binding.artifact_verification is ArtifactVerification.REGISTRY_VERIFIED
    assert binding.model_version == trained.attestation.version
    assert binding.artifact_sha256 == trained.attestation.artifact_sha256
    assert binding.model.artifact_sha256 == trained.attestation.artifact_sha256
    assert binding.identity.capability is InferenceCapability.COUNTERFACTUAL_ACTION


def test_invariant_2_c_the_binding_takes_no_separately_built_model() -> None:
    """**別に組み立てた model / Profile を並べて渡す口が無い**（決定記録 0079 §2.4）。

    v1 の wrapper を受け取っていた `for_control` は無くなり、入口は `VerifiedArtifact` だけ。
    controller も判定器を受け取らない（同梱 Profile からだけ作る）。
    """
    assert not hasattr(MpcModelBinding, "for_control")
    parameters = inspect.signature(MpcModelBinding.from_verified_artifact).parameters
    assert "model" not in parameters
    assert "profile" not in parameters
    assert "assessor" not in inspect.signature(LearnedMpcController).parameters
    with pytest.raises(TypeError):
        MpcModelBinding()
    with pytest.raises(TypeError):
        RegistryCounterfactualThermalModel()


def test_invariant_2_d_the_registry_decides_the_permitted_authority(tmp_path: Path) -> None:
    """**authority 互換は Registry metadata の値で判断する。** 自称値では広げられない。"""
    shadow_only = make_artifact(
        tmp_path / "shadow-only",
        authority=(AuthorityStage.SHADOW,),
        stage=AuthorityStage.SHADOW,
    )

    with pytest.raises(MpcModelUnusableError, match="authority"):
        bind(shadow_only, authority_stage=AuthorityStage.FULL)
    assert bind(shadow_only, authority_stage=AuthorityStage.SHADOW) is not None


def test_invariant_2_e_a_non_thermal_artifact_is_refused(tmp_path: Path) -> None:
    """thermal model 以外の artifact の証拠では束縛しない。"""
    other_kind = issue_verified(tmp_path / "supervisor", kind=ArtifactKind.SUPERVISOR_POLICY)

    with pytest.raises(MpcModelUnusableError, match="thermal model 以外"):
        bind(MpcArtifact(other_kind, tmp_path))


def test_invariant_2_f_a_version_mismatch_is_refused_before_any_inference(trained) -> None:
    """runtime が期待する版と違うモデルで**推論すら始めない**。"""
    with pytest.raises(MpcModelUnusableError, match="版"):
        bind(trained, expected_model_version="9.9.9")


def test_invariant_2_g_a_refused_model_degrades_to_fallback_without_stopping(
    tmp_path: Path,
) -> None:
    """束縛できないモデルでも**運転は止まらない**。Gate は理由付きで Fallback にする。"""
    settings = mpc_policy()
    observational = register_v1(
        tmp_path / "degrade", capability=ArtifactCapability.OBSERVATIONAL_REPLAY
    )
    runtime = load_runtime(observational, settings=settings)
    assert runtime.controller is None

    result = propose(runtime)
    selection = select_with(settings, observational.attestation, result)

    assert result.failure is LearnedFailure.MODEL_LOAD_FAILURE
    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.MODEL_LOAD_FAILURE.value
    # 不変条件 5（理由を落とさない）: 何が起きたのかが trace に残る。
    assert "model_unusable" in selection.fallback_reason.detail
    assert "反実仮想" in selection.fallback_reason.detail
    assert selection.model_gate is None


def test_invariant_2_h_the_binding_cannot_be_swapped_after_it_is_verified(trained) -> None:
    """検証済みの束を**後から差し替えられない**。検査を1度通せば済む形にしない。"""
    binding = bind(trained)

    with pytest.raises(AttributeError):
        binding._model = trained.model  # type: ignore[misc]
    with pytest.raises(AttributeError):
        binding._checker = None  # type: ignore[misc]
    with pytest.raises(AttributeError):
        binding.attestation._model_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        MpcModelBinding()


# ---------------------------------------------------------------- 不変条件 1: 推論への束縛


def test_invariant_1_a_the_proposal_carries_its_own_assessment(trained) -> None:
    """提案の `inference_id` は、判定した assessment と**同じ推論**を指す。"""
    controller, _binding, _settings = build_controller(trained)

    result = propose(controller)

    assert result.proposal is not None and result.assessment is not None
    assert result.proposal.inference_id == result.assessment.inference_id
    assert result.proposal.confidence == result.assessment.confidence
    assert result.proposal.ood == result.assessment.ood
    assert result.solution is not None
    assert result.solution.anchor_inference_id == result.proposal.inference_id
    # 判定器は束縛した artifact の同梱 Profile から作られている（0079 §2.4）。
    assert result.assessment.profile_sha256 == trained.profile.sha256()
    assert result.assessment.prediction.artifact_sha256 == trained.attestation.artifact_sha256


def test_invariant_1_b_a_proposal_cannot_be_paired_with_another_inference(trained) -> None:
    """**別の推論の判定を付け替えられない。** OOD の入力が in-distribution に見えてしまう。"""
    controller, _binding, _settings = build_controller(trained)
    first = propose(controller, action=0.4)
    second = propose(controller, action=0.5)
    assert first.assessment is not None and second.proposal is not None
    assert first.assessment.inference_id != second.assessment.inference_id  # type: ignore[union-attr]

    with pytest.raises(ValidationError, match="別の推論"):
        MpcProposal(proposal=second.proposal, assessment=first.assessment)


def test_invariant_1_c_a_proposal_without_an_assessment_cannot_be_built(trained) -> None:
    """判定の付いていない提案を worker が**作れない**ようにする。"""
    controller, _binding, _settings = build_controller(trained)
    result = propose(controller)

    with pytest.raises(ValidationError, match="assessment"):
        MpcProposal(proposal=result.proposal)


def test_invariant_1_d_a_plan_prediction_from_another_inference_is_rejected(
    trained, monkeypatch: pytest.MonkeyPatch
) -> None:
    """optimizer は**別の anchor に属する予測**でコストを測らない。

    解を返さず `ERROR` にするので、Gate は Fallback へ落とす。
    """
    break_predict_plan(
        monkeypatch,
        lambda _planned, prediction: rewritten(prediction, anchor_inference_id="9" * 64),
    )
    controller, _binding, settings = build_controller(trained)

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR

    selection = select_with(settings, trained.attestation, result)
    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OPTIMIZER_ERROR.value


def test_invariant_1_e_a_prediction_for_other_steps_is_rejected(
    trained, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**候補と違う step 列の予測**を受け取ったまま最適化しない。"""

    def other_steps(planned: PlannedThermalInput, prediction: PlanPrediction) -> PlanPrediction:
        offsets = (1_000, 2_000, 4_000)
        return rewritten(
            prediction,
            targets=tuple(
                target.model_dump(mode="python")
                | {
                    "offset_ms": offset,
                    "expected_ts_ms": planned.observed.action_ts_ms + offset,
                }
                for target, offset in zip(prediction.targets, offsets, strict=True)
            ),
        )

    break_predict_plan(monkeypatch, other_steps)
    controller, _binding, _settings = build_controller(trained)

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR


# ---------------------------------------------------------------- 不変条件 3: 最初の step だけ


def test_invariant_3_a_only_the_first_step_becomes_the_request(trained) -> None:
    """**実行するのは plan の最初の step だけ**（receding horizon）。"""
    controller, _binding, settings = build_controller(trained)

    result = propose(controller)

    assert result.solution is not None and result.proposal is not None
    plan = result.solution.plan
    assert len(plan.steps) == settings.mpc.optimizer.steps == len(HORIZONS)
    for zone in Zone:
        assert result.proposal.requested.get(zone).demand == plan.first.get(zone)


def test_invariant_3_b_a_solution_cannot_claim_a_request_outside_its_plan() -> None:
    """解の `requested` を plan の最初の step と**食い違わせられない**。"""
    from coldaisle.control.mpc.optimizer import MpcSolution

    plan = ActionPlan.held(demands(0.4), step_ms=STEP_MS, steps=2)
    with pytest.raises(ValidationError, match="最初の step"):
        MpcSolution.model_validate(
            {
                "plan": plan.model_dump(mode="python"),
                "requested": demands(0.9).model_dump(mode="python"),
                "cost": _flat_cost(1.0),
                "baseline_cost": _flat_cost(2.0),
                "baseline_requested": demands(0.4).model_dump(mode="python"),
                "anchor_inference_id": "a" * 64,
                "prediction": _flat_prediction(plan),
                "evaluations": 1,
            }
        )


def test_invariant_3_d_a_solution_cannot_carry_another_candidates_prediction() -> None:
    """解に**別の候補 plan の予測**を添えられない（#90 が記録する予測の束縛）。"""
    from coldaisle.control.mpc.optimizer import MpcSolution

    plan = ActionPlan.held(demands(0.4), step_ms=STEP_MS, steps=2)
    other = ActionPlan.held(demands(0.9), step_ms=STEP_MS, steps=2)
    payload = {
        "plan": plan.model_dump(mode="python"),
        "requested": plan.first.model_dump(mode="python"),
        "cost": _flat_cost(1.0),
        "baseline_cost": _flat_cost(2.0),
        "baseline_requested": demands(0.4).model_dump(mode="python"),
        "anchor_inference_id": "a" * 64,
        "evaluations": 1,
    }
    with pytest.raises(ValidationError, match="別の候補 plan"):
        MpcSolution.model_validate(payload | {"prediction": _flat_prediction(other)})
    with pytest.raises(ValidationError, match="別の anchor 推論"):
        MpcSolution.model_validate(
            payload | {"prediction": _flat_prediction(plan, anchor_id="b" * 64)}
        )


def _flat_prediction(plan: ActionPlan, *, anchor_id: str = "a" * 64) -> dict[str, object]:
    return {
        "model_id": "thermal",
        "model_version": "1.0.0",
        "artifact_sha256": "c" * 64,
        "artifact_verification": ArtifactVerification.REGISTRY_VERIFIED,
        "capability": InferenceCapability.COUNTERFACTUAL_ACTION,
        "anchor_inference_id": anchor_id,
        "input_action_ts_ms": 0,
        "plan_digest": plan.digest(),
        "targets": tuple(
            PlannedTarget(
                offset_ms=step.offset_ms,
                expected_ts_ms=step.offset_ms,
                values={"gpu.0.core": 50.0},
            )
            for step in plan.steps
        ),
    }


def _flat_cost(total: float) -> dict[str, object]:
    return {
        "total": total,
        "terms": {
            "gpu_temperature": total,
            "cpu_temperature": 0.0,
            "balance": 0.0,
            "acoustic": 0.0,
            "change": 0.0,
        },
        "steps": 2,
        "balance_known_steps": 0,
    }


def test_invariant_3_c_a_plan_cannot_have_an_irregular_step_grid() -> None:
    """plan の step 幅を**不揃いにできない**。予測の時刻と対応が崩れる。"""
    with pytest.raises(ValidationError, match="等間隔"):
        ActionPlan(
            step_ms=1_000,
            steps=(
                PlanStep(offset_ms=1_000, demands=demands(0.4)),
                PlanStep(offset_ms=2_500, demands=demands(0.4)),
            ),
        )


# ---------------------------------------------------------------- 不変条件 4 / 5: 制約


def test_invariant_4_a_the_request_never_goes_below_the_safety_floor(trained) -> None:
    """**Critical Safety の floor を下回る要求を作らない。**

    後段の #78 が必ず引き上げるが、そもそも下回る候補を評価しない。
    """
    controller, _binding, _settings = build_controller(trained)

    for floor in (0.2, 0.5, 0.75, 0.95):
        result = propose(controller, baseline=0.3, safety_floor=floor)
        assert result.proposal is not None
        for zone in Zone:
            assert result.proposal.requested.get(zone).demand >= floor


def test_invariant_4_b_the_constraint_set_only_narrows(trained) -> None:
    """設定の探索範囲・Safety floor・変化幅は**狭める向きにしか働かない**。"""
    settings = mpc_policy(max_step_up=_provisional(0.05), max_step_down=_provisional(0.05))
    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.2),
        current=demands(0.5),
    )

    lower, upper = constraints.window(Zone.FRONT)
    assert (lower, upper) == (pytest.approx(0.45), pytest.approx(0.55))
    assert constraints.clamp(Zone.FRONT, 1.0) == pytest.approx(0.55)
    assert constraints.clamp(Zone.FRONT, 0.0) == pytest.approx(0.45)


def test_invariant_4_c_a_rising_safety_floor_beats_the_rate_limit(trained) -> None:
    """Safety floor が上がった tick では**上げ幅の制限より floor を優先する**。

    制限を優先すると、冷却を強めるべき瞬間に弱いまま留まる。
    """
    settings = mpc_policy(max_step_up=_provisional(0.01))
    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.9),
        current=demands(0.3),
    )

    lower, upper = constraints.window(Zone.TOP)
    assert lower == pytest.approx(0.9)
    assert upper >= lower


def test_invariant_5_a_an_infeasible_constraint_set_is_refused(trained) -> None:
    """満たせる値が無い制約を**勝手に緩めない**。"""
    settings = mpc_policy(
        zone_bounds={
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(0.5)} for zone in Zone
        }
    )

    with pytest.raises(InfeasiblePlanError, match="ceiling"):
        HardConstraintSet.build(
            optimizer=settings.mpc.optimizer,
            safety=safety(),
            safety_floor=demands(0.8),
            current=demands(0.4),
        )


def test_invariant_5_b_an_infeasible_tick_degrades_to_fallback(trained) -> None:
    """実行不能な tick は当て推量を返さず、`ERROR` として Fallback へ渡る。"""
    settings = mpc_policy(
        zone_bounds={
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(0.5)} for zone in Zone
        }
    )
    controller, _binding, _settings = build_controller(trained, policy_config=settings)

    result = propose(controller, safety_floor=0.9)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "ceiling" in result.failure_reason.detail


# ---------------------------------------------------------------- 不変条件 6: timeout


def test_invariant_6_a_an_exhausted_budget_yields_no_solution(trained) -> None:
    """budget を使い切ったら**解を返さない**。中途半端な探索結果を制御に使わない。"""
    controller, _binding, _settings = build_controller(
        trained, clock=ScriptedClock(0, 0, 10_000), policy_config=mpc_policy(budget_ms=10)
    )

    result = propose(controller)

    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.TIMEOUT
    assert result.solution is None


def test_invariant_6_b_an_evaluation_limit_stops_the_search_deterministically(trained) -> None:
    """時計に頼らない**決定論的な打ち切り**も効く（replay で同じ結果になる）。"""
    controller, _binding, _settings = build_controller(
        trained, policy_config=mpc_policy(max_evaluations=_provisional(2))
    )

    result = propose(controller)

    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.TIMEOUT
    assert result.solution is None


def test_invariant_6_c_a_timed_out_proposal_is_never_made_active(trained) -> None:
    """`TIMEOUT` の提案を Gate が active controller にしない。"""
    controller, _binding, settings = build_controller(
        trained, clock=ScriptedClock(0, 0, 10_000), policy_config=mpc_policy(budget_ms=10)
    )
    result = propose(controller)

    selection = select_with(settings, trained.attestation, result)

    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OPTIMIZER_TIMEOUT.value
    assert selection.model_gate is not None
    assert selection.model_gate.learned_selected is False


# ---------------------------------------------------------------- 不変条件 7 / 12: 失敗と入力


def test_invariant_7_a_a_model_exception_becomes_a_failure_not_a_crash(
    trained, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内部モデルが落ちても**制御ループを落とさない**（AGENTS.md ルール4）。"""
    break_predict_plan(monkeypatch, ValueError("model exploded"))
    controller, _binding, _settings = build_controller(trained)

    result = propose(controller)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "model exploded" in result.failure_reason.detail


def test_invariant_7_b_the_worker_keeps_running_after_a_failed_tick(trained) -> None:
    """失敗した次の tick は**通常どおり提案できる**。"""
    controller, _binding, _settings = build_controller(trained)
    broken = propose(controller, ts_ms=0)
    assert broken.failure is LearnedFailure.OPTIMIZER_EXCEPTION

    healthy = propose(controller)

    assert healthy.proposal is not None


def test_invariant_12_a_a_window_from_another_time_is_refused(trained) -> None:
    """**MPC は別時刻の Telemetry を使わない**（#102 の共通入力を守る）。"""
    controller, _binding, _settings = build_controller(trained)

    result = propose(controller, ts_ms=ACTION_TS_MS + 1_000)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "Snapshot" in result.failure_reason.detail


# ---------------------------------------------------------------- 不変条件 8: Confidence / OOD


def test_invariant_8_a_an_ood_input_is_handed_to_the_gate_as_ood(trained) -> None:
    """学習範囲の外の入力は **OOD として Gate へ渡る**。提案の数値も判定と揃う。"""
    controller, _binding, settings = build_controller(trained)
    ood_window = shifted(observed_input(), air=200.0)

    result = propose(controller, observed=ood_window)

    assert result.proposal is not None and result.assessment is not None
    assert result.assessment.ood is True
    assert result.proposal.ood is True
    assert result.proposal.confidence == 0.0

    selection = select_with(settings, trained.attestation, result)
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OOD.value
    assert selection.model_gate is not None
    assert selection.model_gate.attested is True
    assert selection.model_gate.ood is True
    assert selection.model_gate.confidence_level is ConfidenceLevel.LOW
    assert selection.model_gate.learned_selected is False


def test_invariant_8_b_an_inflated_confidence_cannot_be_written_into_the_proposal(
    trained,
) -> None:
    """**提案が判定より高い confidence を名乗れない。** 名乗れば型が拒む。"""
    controller, _binding, _settings = build_controller(trained)
    result = propose(controller)
    assert result.proposal is not None

    inflated = result.proposal.model_copy(update={"confidence": 1.0})
    with pytest.raises(ValidationError, match="confidence"):
        MpcProposal(proposal=inflated, assessment=result.assessment, solution=result.solution)


def test_invariant_8_c_a_shadow_stage_records_the_proposal_without_selecting_it(trained) -> None:
    """Shadow Mode は**実機を操作せず提案だけを記録する**。"""
    settings = mpc_policy(authority="shadow")
    controller, _binding, _settings = build_controller(trained, policy_config=settings)
    result = propose(controller)

    selection = select_with(settings, trained.attestation, result)

    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.model_gate is not None
    assert selection.model_gate.learned_selected is False
    assert selection.model_gate.inference_id == result.proposal.inference_id  # type: ignore[union-attr]
    assert selection.model_gate.limits == ()


def test_invariant_8_d_a_held_anchor_outside_the_step_support_is_ood(tmp_path: Path) -> None:
    """**anchor 推論の held の列が step ごとの support の外なら `support` の OOD**（0084 §2.1）。

    demand 0.45 を step 0 でだけ観測した Profile では、いま 0.45 が掛かっている anchor 推論の
    held の列（全 step 0.45）は遅い step で support の外になる。anchor action の fan range と
    support cell には入っている（0050 の既存の判定だけなら通る）入力で確かめる。
    """
    early_only = make_artifact(
        tmp_path / "early-only",
        support=action_support(
            steps=(
                (ranges(0.0, 1.0), CELLS),
                (ranges(0.0, 0.4), CELLS),
                (ranges(0.0, 0.4), CELLS),
            )
        ),
    )
    controller, _binding, settings = build_controller(early_only)

    result = propose(controller)

    assert result.assessment is not None and result.assessment.ood is True
    support = next(
        item for item in result.assessment.components if item.component.value == "support"
    )
    assert support.ood is True
    assert "step=1" in support.detail
    fan_range = next(
        item for item in result.assessment.components if item.component.value == "fan_state_range"
    )
    assert fan_range.ood is False
    selection = select_with(settings, early_only.attestation, result)
    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OOD.value


# ---------------------------------------------------------------- 不変条件 9 / 10: 再現性と改善


def test_invariant_9_a_the_same_inputs_give_the_same_proposal(trained) -> None:
    """同じ Snapshot / window / model / 設定 / 時計から**同じ提案**が出る（replay 再現性）。"""
    first_controller, _first, _ = build_controller(trained)
    second_controller, _second, _ = build_controller(trained)

    first = propose(first_controller)
    second = propose(second_controller)

    assert first.proposal is not None and second.proposal is not None
    assert first.proposal.model_dump_json() == second.proposal.model_dump_json()
    assert first.solution is not None and second.solution is not None
    assert first.solution.model_dump_json() == second.solution.model_dump_json()
    assert first.result_digest() == second.result_digest()


def test_invariant_10_a_the_chosen_plan_is_never_worse_than_the_baseline(trained) -> None:
    """採用する解は内部モデルの上で **Baseline 以下のコスト**。探索は Baseline から始める。"""
    controller, _binding, _settings = build_controller(trained, acoustic=True)

    result = propose(controller, baseline=0.3)

    assert result.solution is not None
    assert result.solution.cost.total <= result.solution.baseline_cost.total
    # Baseline 自身も制約に収める。下げる速さは safety.ramp_down_per_s（0.1/s）× step（1s）。
    # 観測 window の action は dataset のまま（0.45）。
    assert result.solution.baseline_requested == demands(0.35)


def test_invariant_10_b_the_optimizer_improves_the_total_cost_over_the_baseline(trained) -> None:
    """予測温度が target band を超えている状況では、Baseline より**総合コストを下げる**。"""
    controller, _binding, _settings = build_controller(trained, acoustic=True)

    result = propose(controller, baseline=0.3)

    assert result.solution is not None
    assert result.solution.improvement > 0.0
    # 温度が高い局面なので、より強い冷却を選ぶ（音響と変化のコストを払ってでも）。
    assert result.solution.requested.front > 0.35
    assert result.solution.cost.terms.gpu_temperature < (
        result.solution.baseline_cost.terms.gpu_temperature
    )


def test_invariant_10_c_the_acoustic_term_pulls_the_solution_back(trained) -> None:
    """音響コストを入れると**同じ入力でもより静かな解**を選ぶ。項が効いていることを示す。"""
    loud_controller, _loud, _ = build_controller(trained, acoustic=False)
    quiet_controller, _quiet, _ = build_controller(trained, acoustic=True)

    loud = propose(loud_controller, baseline=0.3)
    quiet = propose(quiet_controller, baseline=0.3)

    assert loud.solution is not None and quiet.solution is not None
    assert sum(quiet.solution.requested.get(zone) for zone in Zone) < sum(
        loud.solution.requested.get(zone) for zone in Zone
    )


# ---------------------------------------------------------------- 不変条件 11: 迂回路を作らない


FORBIDDEN_MODULES = (
    "coldaisle.control.hardware",
    "coldaisle.control.safety",
    "coldaisle.control.reactive",
    "serial",
    "subprocess",
)
"""`control/mpc` が import してはいけない module（AGENTS.md ルール1 / 2 / 6）。"""


def test_invariant_11_a_the_mpc_package_cannot_reach_the_actuation_path() -> None:
    """**Guard / Safety / Hardware を迂回する経路を作らない。**

    MPC から書き込み層へ直接触れられるようになった瞬間、`requested` までという線が消える。
    """
    package = Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control" / "mpc"
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                if any(
                    name == forbidden or name.startswith(f"{forbidden}.")
                    for forbidden in FORBIDDEN_MODULES
                ):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_invariant_11_b_the_mpc_package_never_names_pwm_or_effective_demand() -> None:
    """MPC の型に **PWM も effective demand も現れない**（決定記録 0028 §2.3）。"""
    package = Path(__file__).resolve().parents[1] / "src" / "coldaisle" / "control" / "mpc"
    offenders = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        for name in sorted(names):
            if "pwm" in name.lower() or name == "EffectiveZoneDemand":
                offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_the_optimizer_refuses_a_grid_other_than_the_action_schema(trained) -> None:
    """**`mpc.optimizer` の格子は action schema と完全に一致させる**（0079 §2.4）。

    補間・外挿・丸めをしないので、合わない設定は生成時に拒む（tick ごとに失敗させない）。
    """
    binding = bind(trained)
    for settings in (
        mpc_policy(step_ms=_provisional(7_000), horizon_ms=_provisional(7_000)),
        mpc_policy(horizon_ms=_provisional(2_000)),
    ):
        with pytest.raises(MpcModelUnusableError, match="格子"):
            LearnedMpcOptimizer(
                binding,
                settings.mpc.optimizer,
                MpcCostModel(settings.mpc.optimizer),
                budget_ms=settings.mpc.budget_ms,
                monotonic_ms=ScriptedClock(0),
            )


def test_the_optimizer_refuses_a_model_that_cannot_predict_the_cost_metrics(trained) -> None:
    """目的関数が必要とする metric を**予測できないモデル**も生成時に拒む。"""
    settings = mpc_policy(
        cost_metrics={"cpu_temperature": "air.front_intake", "gpu_temperature": GPU}
    )

    with pytest.raises(MpcModelUnusableError, match="metric"):
        LearnedMpcOptimizer(
            bind(trained),
            settings.mpc.optimizer,
            MpcCostModel(settings.mpc.optimizer),
            budget_ms=settings.mpc.budget_ms,
            monotonic_ms=ScriptedClock(0),
        )


def test_the_whole_chain_hands_a_bounded_request_to_the_guard(trained) -> None:
    """健全な tick では **Gate が Learned MPC を選び、要求は制約の中に収まる**。

    ここで作れるのは `requested` までで、この先の Reactive Guard（#80）と
    Critical Safety（#78）は毎 tick 必ず掛かる。
    """
    settings = mpc_policy(max_step_up=_provisional(0.1))
    controller, _binding, _ = build_controller(trained, policy_config=settings, acoustic=True)
    result = propose(
        controller,
        residual=evidence(trained.profile, 0.5, 10, at=ACTION_TS_MS),
        safety_floor=0.3,
    )
    assert result.proposal is not None
    assert result.assessment is not None
    assert result.assessment.ood is False

    # 復帰 hold を満たすため、健全なまま2 tick 進める（#79）。
    gate = gate_for(
        settings,
        expected_model_version=trained.attestation.version,
        expected_artifact_sha256=trained.attestation.artifact_sha256,
    )
    for now_mono_ms in (0, settings.recovery_hold_ms):
        selection = gate.select(
            now_mono_ms=now_mono_ms,
            fallback=fallback_proposal(0.4),
            learned=result.to_status(received_at_mono_ms=now_mono_ms),
            operating_mode=OperatingMode.AUTO,
            safety_state=SafetyState.NORMAL,
        )

    assert selection.active_controller is ControllerKind.LEARNED_MPC
    assert selection.model_gate is not None
    assert selection.model_gate.learned_selected is True

    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.3),
        current=demands(
            _mean(tuple(observed_input().action.get(zone).effective_demand for zone in Zone))
        ),
    )
    for zone in Zone:
        demand = selection.proposal.requested.get(zone).demand
        assert demand >= 0.3
        lower, upper = constraints.window(zone)
        assert lower <= demand <= upper


# ---------------------------------------------------------------- 目的関数の項


class StubAirBalance:
    """比を返す / 返さないだけを切り替える試験用 Air Balance Model。"""

    def __init__(self, ratio: float | None) -> None:
        self._ratio = ratio
        self.calls = 0
        self.seen: list[PerZone[Demand]] = []

    @property
    def metadata(self) -> AirBalanceMetadata:
        """任意依存を差し替えたことが条件 hash に出るよう、契約どおり出どころを返す。"""
        return AirBalanceMetadata(
            model_id="stub-balance",
            source=CharacterizationSource(status="uncalibrated", basis="試験用"),
            flow_unit="relative",
            config_sha256="e" * 64,
        )

    def evaluate(self, demands: PerZone[Demand], thermal: ThermalInputs) -> AirBalanceEstimate:
        """比と状態だけを持つ最小の評価結果を返す。"""
        del thermal
        self.calls += 1
        self.seen.append(demands)
        known = self._ratio is not None
        return AirBalanceEstimate(
            q_front=1.0 if known else None,
            q_rear=0.5 if known else None,
            q_top=(self._ratio - 0.5) if self._ratio is not None else None,
            estimated_intake=1.0 if known else None,
            estimated_exhaust=self._ratio if known else None,
            balance_ratio=self._ratio,
            state=AirBalanceState.BALANCED if known else AirBalanceState.UNKNOWN,
            thermal_limited=False,
            thermal_reasons=(),
            metadata=AirBalanceMetadata(
                model_id="stub",
                source=CharacterizationSource(status="uncalibrated", basis="試験用"),
                config_sha256="e" * 64,
            ),
        )

    def coordinate(self, demands, thermal, *, projected_floors=None):  # type: ignore[no-untyped-def]
        """MPC は使わない。"""
        raise NotImplementedError


def _balance_band() -> BalanceBand:
    return BalanceBand(target_ratio=1.0, minimum_ratio=0.8, maximum_ratio=1.2)


def _fan_hardware() -> FanHardwareConfig:
    """`minimum_stable_demand` が 0.3 の試験用 profile（`test_control_config` と同じ）。"""
    return FanHardwareConfig.model_validate(valid_documents()["fan-hardware.yaml"])


def test_the_balance_term_scores_the_demand_the_hardware_would_apply(trained) -> None:
    """**balance の項は `minimum_stable_demand` へ引き上げた demand で評価する**（0073 §2.3）。

    backend は下限未満を引き上げてから書くので、候補のまま採点すると実際には起きない
    風量の比を最適化してしまう。写像は balance の項だけに使い、plan は変えない。
    """
    settings = mpc_policy()
    plan, prediction = _plan_prediction(trained, settings)
    stub = StubAirBalance(1.0)
    hardware = _fan_hardware()
    MpcCostModel(
        settings.mpc.optimizer,
        air_balance=stub,
        balance_band=_balance_band(),
        fan_hardware=hardware,
    ).evaluate(
        plan=plan,
        prediction=prediction,
        weights=supervisor_output().weights,
        target_band=supervisor_output().target_band,
        previous=demands(0.45),
    )

    assert len(stub.seen) == len(plan.steps)
    for step, seen in zip(plan.steps, stub.seen, strict=True):
        assert seen == hardware.stable_demands(step.demands)
        for zone in Zone:
            minimum = hardware.zones.get(zone).profile.minimum_stable_demand
            assert seen.get(zone) >= minimum


def test_air_balance_without_the_hardware_profile_is_refused_at_construction(trained) -> None:
    """Air Balance を渡して profile を渡さない組み合わせは構成時に拒否する（0073 §2.3）。"""
    del trained
    settings = mpc_policy()
    with pytest.raises(MpcCostUnusableError, match=r"fan-hardware\.yaml"):
        MpcCostModel(
            settings.mpc.optimizer,
            air_balance=StubAirBalance(1.0),
            balance_band=_balance_band(),
        )


def test_an_unknown_air_balance_ratio_is_not_treated_as_a_good_one(trained) -> None:
    """**比を推定できない step を「釣り合っている」と読み替えない。**

    設定した `unknown_balance_cost` を課し、未知が最適解に見えないようにする。
    """
    settings = mpc_policy()
    prediction = _plan_prediction(trained, settings)
    weights = supervisor_output().weights.model_copy(update={"balance": 1.0})

    unknown = MpcCostModel(
        settings.mpc.optimizer,
        air_balance=StubAirBalance(None),
        balance_band=_balance_band(),
        fan_hardware=_fan_hardware(),
    ).evaluate(
        plan=prediction[0],
        prediction=prediction[1],
        weights=weights,
        target_band=supervisor_output().target_band,
        previous=demands(0.45),
    )
    on_target = MpcCostModel(
        settings.mpc.optimizer,
        air_balance=StubAirBalance(1.0),
        balance_band=_balance_band(),
        fan_hardware=_fan_hardware(),
    ).evaluate(
        plan=prediction[0],
        prediction=prediction[1],
        weights=weights,
        target_band=supervisor_output().target_band,
        previous=demands(0.45),
    )

    assert unknown.balance_known_steps == 0
    assert on_target.balance_known_steps == len(prediction[0].steps)
    assert unknown.terms.balance == pytest.approx(settings.mpc.optimizer.unknown_balance_cost.value)
    assert on_target.terms.balance == pytest.approx(0.0)


def test_the_air_balance_target_band_must_come_from_its_own_config(trained) -> None:
    """目標比は #81 の設定が持つ。**MPC 側に写させない。**"""
    del trained
    settings = mpc_policy()
    with pytest.raises(MpcCostUnusableError, match="BalanceBand"):
        MpcCostModel(settings.mpc.optimizer, air_balance=StubAirBalance(1.0))


def test_a_prediction_without_the_cost_metrics_is_refused(trained) -> None:
    """コストに必要な metric が予測に無ければ、**0 で埋めずに失敗する**。"""
    settings = mpc_policy()
    plan, prediction = _plan_prediction(trained, settings)
    stripped = PlanPrediction.model_validate(
        prediction.model_dump(mode="python")
        | {
            "targets": tuple(
                target.model_dump(mode="python") | {"values": {GPU: target.values[GPU]}}
                for target in prediction.targets
            )
        }
    )

    with pytest.raises(MpcCostUnusableError, match=CPU):
        MpcCostModel(settings.mpc.optimizer).evaluate(
            plan=plan,
            prediction=stripped,
            weights=supervisor_output().weights,
            target_band=supervisor_output().target_band,
            previous=demands(0.45),
        )


def _plan_prediction(
    trained: MpcArtifact,
    settings: FanPolicyConfig,
) -> tuple[ActionPlan, PlanPrediction]:
    """コスト単体の試験に使う plan と、その plan に対する予測。"""
    plan = ActionPlan.held(
        demands(0.5),
        step_ms=settings.mpc.optimizer.step_ms.value,
        steps=settings.mpc.optimizer.steps,
    )
    return plan, _with_anchor(
        bind(trained), PlannedThermalInput(observed=observed_input(), plan=plan)
    )


def _with_anchor(binding: MpcModelBinding, planned: PlannedThermalInput) -> PlanPrediction:
    """この観測の anchor 推論を1回作って候補の予測へ渡す（optimizer と同じ使い方）。"""
    return binding.predict_plan(planned, anchor=binding.predict(planned.observed))


# ------------------------------------------- codex レビュー（PR #151）への修正の回帰試験


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("internal feature layout failure"),
        KeyError("gpu.0.core"),
        ZeroDivisionError("division by zero"),
        MemoryError("out of memory"),
    ],
)
def test_any_ordinary_exception_from_the_model_becomes_a_fallback(
    trained, error, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**種類を問わず、worker 境界の外へ例外を出さない**（codex #4055491980）。"""
    break_predict_plan(monkeypatch, error)
    controller, _binding, _settings = build_controller(trained)

    result = propose(controller)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert result.failure_reason.code == _expected_reason_code(type(error).__name__)


def _expected_reason_code(name: str) -> str:
    lowered = "".join(
        f"_{character.lower()}" if character.isupper() else character for character in name
    ).strip("_")
    return lowered


@pytest.mark.parametrize("escape", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_still_propagate_from_the_worker(
    trained, escape, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**停止の合図は握りつぶさない。** `BaseException` は worker 境界を素通りする。"""
    break_predict_plan(monkeypatch, escape())
    controller, _binding, _settings = build_controller(trained)

    with pytest.raises(escape):
        propose(controller)


def test_the_budget_is_rechecked_after_the_baseline_evaluation(trained) -> None:
    """**Baseline の評価だけで予算を使い切った tick を OK にしない**（codex #4055491984）。"""
    controller, _binding, _settings = build_controller(
        trained,
        clock=ScriptedClock(0, 10_000),
        policy_config=mpc_policy(budget_ms=10),
    )

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.TIMEOUT


def test_a_ramp_down_bound_above_the_ceiling_is_infeasible_not_widened() -> None:
    """**設定した探索 ceiling を MPC 自身が緩めない**（codex #4055491988）。"""
    settings = mpc_policy(
        max_step_down=_provisional(0.1),
        zone_bounds={
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(0.5)} for zone in Zone
        },
    )

    with pytest.raises(InfeasiblePlanError, match="ceiling"):
        HardConstraintSet.build(
            optimizer=settings.mpc.optimizer,
            safety=safety(),
            safety_floor=demands(0.2),
            current=demands(1.0),
        )


def test_a_floor_above_the_step_up_limit_is_still_allowed() -> None:
    """対になる向き（floor が上げ幅に勝つ）は**実行不能ではない**。floor に合わせる。"""
    settings = mpc_policy(max_step_up=_provisional(0.01), max_step_down=_provisional(0.5))
    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.9),
        current=demands(0.3),
    )

    assert constraints.window(Zone.FRONT) == (pytest.approx(0.9), pytest.approx(0.9))


def test_the_worker_failure_detail_reaches_the_fallback_trace(
    trained, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**理由を trace の手前で落とさない**（codex #4055491991）。"""
    break_predict_plan(monkeypatch, RuntimeError("feature layout broken"))
    settings = mpc_policy()
    controller, _binding, _ = build_controller(trained, policy_config=settings)
    result = propose(controller)

    selection = select_with(settings, trained.attestation, result)

    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OPTIMIZER_EXCEPTION.value
    assert "runtime_error" in selection.fallback_reason.detail
    assert "feature layout broken" in selection.fallback_reason.detail
    metadata = selection.trace_metadata()["fallback_reason"]
    assert isinstance(metadata, dict)
    assert "feature layout broken" in str(metadata["detail"])


def test_a_status_without_a_failure_cannot_carry_a_failure_reason() -> None:
    """失敗していない状態に理由だけを付けられない（trace の読み違いを防ぐ）。"""
    with pytest.raises(ValidationError, match="failure_reason"):
        LearnedControlStatus(failure_reason=Reason(code="made_up"))


def test_a_plan_prediction_from_another_artifact_is_rejected(
    trained, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**anchor と別の artifact の予測でコストを測らない。**"""
    break_predict_plan(
        monkeypatch, lambda _planned, prediction: rewritten(prediction, artifact_sha256="b" * 64)
    )
    controller, _binding, _settings = build_controller(trained)

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR


@pytest.mark.parametrize("promoted", [False, True])
def test_only_the_production_pointer_can_be_bound_for_active_control(
    tmp_path: Path, promoted: bool
) -> None:
    """**promotion を経ていない artifact に制御権を渡さない**（codex #4055572072）。"""
    artifact = make_artifact(tmp_path / ("promoted" if promoted else "pinned"), promoted=promoted)

    assert artifact.attestation.production_active is promoted
    if promoted:
        assert artifact.attestation.status is ArtifactStatus.PRODUCTION
        assert bind(artifact).attestation.production_active is True
        return

    assert artifact.attestation.status is ArtifactStatus.VALIDATED
    with pytest.raises(MpcModelUnusableError, match="production pointer"):
        bind(artifact)


def test_a_retired_artifact_pinned_by_version_is_refused(tmp_path: Path) -> None:
    """rollback などで引退した artifact も、version 固定で制御へ戻せない。"""
    root = tmp_path / "retired"
    old = make_artifact(root)
    assert old.attestation.production_active is True

    # 次の版を promote すると、前の版は production pointer ではなくなる。
    newer = make_artifact(root, version="0.2.0")
    registry = ModelRegistry(root, limits=REGISTRY_LIMITS)
    stale = registry.load_version(
        ArtifactRef(kind=ArtifactKind.THERMAL_MODEL, model_id=MODEL_ID, version="0.1.0"),
        ModelCompatibility(
            feature_schema_version=old.attestation.feature_schema_version,
            target_schema_version=old.attestation.target_schema_version,
            authority_stage=AuthorityStage.FULL,
        ),
    )

    assert newer.attestation.production_active is True
    assert stale.artifact is not None
    assert stale.artifact.attestation.production_active is False
    with pytest.raises(MpcModelUnusableError, match="production pointer"):
        bind(MpcArtifact(stale.artifact, root))


def test_a_prediction_for_another_candidate_is_rejected(
    trained, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**別の候補の予測を今の候補のコストに使わせない**（codex #4055572075）。"""
    stale = ActionPlan.held(demands(0.95), step_ms=STEP_MS, steps=STEPS)
    break_predict_plan(
        monkeypatch, lambda _planned, prediction: rewritten(prediction, plan_digest=stale.digest())
    )
    controller, _binding, settings = build_controller(trained)

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR
    selection = select_with(settings, trained.attestation, result)
    assert selection.active_controller is ControllerKind.FALLBACK


def test_the_plan_digest_covers_the_per_zone_demands() -> None:
    """plan の識別子は zone ごとの demand まで含む（offset だけでは足りない）。"""
    first = ActionPlan.held(demands(0.4), step_ms=STEP_MS, steps=2)
    second = ActionPlan.held(demands(0.5), step_ms=STEP_MS, steps=2)
    zoned = ActionPlan(
        step_ms=STEP_MS,
        steps=tuple(
            PlanStep(
                offset_ms=STEP_MS * (index + 1),
                demands=PerZone[Demand](front=0.4, rear=0.4, top=0.5),
            )
            for index in range(2)
        ),
    )

    assert first.offsets_ms == second.offsets_ms == zoned.offsets_ms
    assert len({first.digest(), second.digest(), zoned.digest()}) == 3
    assert first.digest() == ActionPlan.held(demands(0.4), step_ms=STEP_MS, steps=2).digest()


def test_an_expired_tick_never_spends_another_model_evaluation(
    trained, plan_calls: list[ActionPlan]
) -> None:
    """**予算を使い切った tick で、さらに1回モデルを回さない**（codex #4055572079）。"""
    controller, _binding, _settings = build_controller(
        trained,
        clock=ScriptedClock(0, 10_000),
        policy_config=mpc_policy(budget_ms=10),
    )

    result = propose(controller)

    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.TIMEOUT
    assert plan_calls == []


def test_an_anchor_from_other_bytes_is_refused(trained, monkeypatch: pytest.MonkeyPatch) -> None:
    """**ID と版が同じでも、別の bytes の anchor 推論は使わない**（codex #4055635586）。"""
    original = MpcModelBinding.predict

    def other_bytes(self: MpcModelBinding, observed: ObservedThermalInput) -> ThermalPrediction:
        prediction = original(self, observed)
        return ThermalPrediction.model_validate(
            prediction.model_dump(mode="python") | {"artifact_sha256": "c" * 64}
        )

    monkeypatch.setattr(MpcModelBinding, "predict", other_bytes)
    settings = mpc_policy()
    controller, _binding, _ = build_controller(trained, policy_config=settings)

    result = propose(controller)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "artifact_sha256" in result.failure_reason.detail
    selection = select_with(settings, trained.attestation, result)
    assert selection.active_controller is ControllerKind.FALLBACK
    assert "artifact_sha256" in selection.fallback_reason.detail  # type: ignore[union-attr]


def test_the_binding_must_cover_the_effective_authority_stage(trained) -> None:
    """**Registry が SHADOW だけを許した束縛を、より高い実効 stage で動かさない。**

    照合の相手は設定の `authority_stage` ではなく**実効 stage**（#92 / 0057 §2.2）。
    """
    shadow_binding = bind(trained, authority_stage=AuthorityStage.SHADOW)
    full_policy = mpc_policy(authority="full")

    with pytest.raises(MpcModelUnusableError, match="authority stage"):
        LearnedMpcController(
            shadow_binding,
            full_policy,
            safety(),
            monotonic_ms=ScriptedClock(0),
            authority=StaticAuthorityStage(AuthorityStage.FULL),
        )

    # **上限が LIMITED でも、journal が SHADOW なら SHADOW 互換の束縛は動く。**
    limited_ceiling = mpc_policy(authority="limited")
    controller = LearnedMpcController(
        shadow_binding,
        limited_ceiling,
        safety(),
        monotonic_ms=ScriptedClock(0),
        authority=StaticAuthorityStage(AuthorityStage.SHADOW),
    )
    assert propose(controller).proposal is not None


def test_a_raised_stage_stops_a_binding_that_no_longer_covers_it(trained) -> None:
    """**昇格のあと、束縛の覆っていない stage で提案を出し続けない。**"""
    binding = bind(trained, authority_stage=AuthorityStage.SHADOW)
    settings = mpc_policy(authority="limited")

    class Rising:
        """途中で昇格した journal を模す。"""

        def __init__(self) -> None:
            self.stage = AuthorityStage.SHADOW

        def current_stage(self) -> AuthorityStage:
            return self.stage

    authority = Rising()
    controller = LearnedMpcController(
        binding,
        settings,
        safety(),
        monotonic_ms=ScriptedClock(0),
        authority=authority,
    )
    assert propose(controller).proposal is not None

    authority.stage = AuthorityStage.LIMITED
    result = propose(controller)

    assert result.proposal is None
    assert result.failure is LearnedFailure.MODEL_LOAD_FAILURE
    assert result.failure_reason is not None
    assert "authority" in result.failure_reason.detail


def test_the_confidence_assessor_follows_the_runtime_policy(trained) -> None:
    """判定器は**runtime の `model_confidence` と同梱 Profile から**作られる（0079 §2.4）。

    閾値だけがすり替わった判定器を渡す口そのものが無い。
    """
    settings = mpc_policy()
    other = policy(
        authority="full",
        high_min_confidence=0.95,
        mpc={
            "period_ms": 1_000,
            "budget_ms": 1_000,
            "valid_ms": 2_000,
            "optimizer": optimizer_document(),
        },
    )
    controller, _binding, _ = build_controller(trained, policy_config=settings)
    stricter, _binding, _ = build_controller(trained, policy_config=other)

    assert controller.conditions()["model_confidence"] == settings.model_confidence.model_dump(
        mode="json"
    )
    assert stricter.conditions()["model_confidence"] == other.model_confidence.model_dump(
        mode="json"
    )
    assert controller.conditions()["confidence_profile_sha256"] == trained.profile.sha256()


ATTESTATION_FIELD_CHECKS: dict[str, str] = {
    "kind": "from_verified_artifact: L1 / thermal_model 以外を拒む",
    "capability": "from_verified_artifact: L1 / 反実仮想を申告していない artifact を拒む",
    "model_id": "L5 / _check_model_matches_attestation / _check_anchor: anchor と照合",
    "version": "L5 / 期待版 / _check_anchor / _check_prediction",
    "artifact_sha256": "L1（bytes の SHA-256）/ _check_anchor / 判定器の model binding",
    "feature_schema_version": "L5 / model.feature_schema と照合",
    "target_schema_version": "L5 / model.target_schema と照合",
    "authority_compatibility": "L5 / from_verified_artifact: 要求 stage が含まれるか",
    "status": "from_verified_artifact: production_active と一緒に判断",
    "production_active": "from_verified_artifact: production pointer 以外を拒む",
    "model_version": "trace 用の派生値（model_id@version）。個別の照合は上の2つ",
    "registry_revision": "trace のみ。推論時に対応する申告が無い",
    "trace_metadata": "trace 出力（#82）",
}
"""attestation が持つ値ごとに、**どこで模型の申告と突き合わせているか**。"""


def test_every_attested_value_has_a_place_where_it_is_compared() -> None:
    """**証拠に載っている値を、照合しないまま増やさない**（決定記録 0052 §2.1）。"""
    public = {name for name in dir(ArtifactAttestation) if not name.startswith("_")}

    assert public == set(ATTESTATION_FIELD_CHECKS)


def test_the_policy_values_with_a_binding_counterpart_are_compared(trained) -> None:
    """運転設定と束縛の対応を、生成時にすべて突き合わせていること。

    - **実効 authority stage**（#92。設定の `authority_stage` は上限）↔ `binding.authority_stage`
    - `mpc.optimizer` の格子・cost_metrics ↔ model の action / target schema
    """
    settings = mpc_policy()
    binding = bind(trained, authority_stage=settings.authority_stage)

    def build(
        policy_config: FanPolicyConfig,
        *,
        stage: AuthorityStage | None = None,
        with_binding: MpcModelBinding = binding,
    ) -> LearnedMpcController:
        return LearnedMpcController(
            with_binding,
            policy_config,
            safety(),
            monotonic_ms=ScriptedClock(0),
            authority=StaticAuthorityStage(stage or policy_config.authority_stage),
        )

    assert build(settings) is not None
    assert build(mpc_policy(authority="shadow"), stage=AuthorityStage.SHADOW) is not None
    shadow_binding = bind(trained, authority_stage=AuthorityStage.SHADOW)
    with pytest.raises(MpcModelUnusableError, match="authority stage"):
        build(settings, stage=AuthorityStage.EXPANDED, with_binding=shadow_binding)
    with pytest.raises(MpcModelUnusableError, match="格子"):
        build(mpc_policy(step_ms=_provisional(7_000), horizon_ms=_provisional(7_000)))
    with pytest.raises(MpcModelUnusableError, match="metric"):
        build(
            mpc_policy(cost_metrics={"cpu_temperature": "air.front_intake", "gpu_temperature": GPU})
        )


def test_the_configured_step_limit_cannot_exceed_the_prediction_contract() -> None:
    """**設定の上限を、予測の契約より大きくしない**（codex #4055686520）。"""
    assert MAX_MPC_HORIZON_STEPS <= MAX_TARGET_HORIZONS
    assert MAX_MPC_HORIZON_STEPS == 32

    at_limit = mpc_policy(
        step_ms=_provisional(1_000),
        horizon_ms=_provisional(1_000 * MAX_MPC_HORIZON_STEPS),
    )
    assert at_limit.mpc.optimizer.steps == MAX_MPC_HORIZON_STEPS
    assert len(ActionPlan.held(demands(0.4), step_ms=1_000, steps=MAX_MPC_HORIZON_STEPS).steps) == (
        MAX_MPC_HORIZON_STEPS
    )

    with pytest.raises(ValidationError, match="control step"):
        mpc_policy(
            step_ms=_provisional(1_000),
            horizon_ms=_provisional(1_000 * (MAX_MPC_HORIZON_STEPS + 1)),
        )


def test_the_rate_limit_origin_comes_from_the_observed_action(trained) -> None:
    """**変化幅の起点を呼び出し側から受け取らない**（codex #4055749781）。"""
    controller, _binding, settings = build_controller(trained)
    signature = inspect.signature(controller.propose)

    assert "current_demand" not in signature.parameters

    applied = 0.45
    window = observed_input(applied)
    result = propose(controller, observed=window, safety_floor=0.2)
    assert result.solution is not None

    constraints = HardConstraintSet.build(
        optimizer=settings.mpc.optimizer,
        safety=safety(),
        safety_floor=demands(0.2),
        current=demands(applied),
    )
    for zone in Zone:
        lower, upper = constraints.window(zone)
        assert lower <= result.solution.requested.get(zone) <= upper
    assert constraints.window(Zone.FRONT)[0] == pytest.approx(applied - 0.1)


def test_a_window_action_outside_the_search_bounds_fails_closed(trained) -> None:
    """window の action が探索 ceiling より上なら、勝手に広げず実行不能にする。"""
    settings = mpc_policy(
        max_step_down=_provisional(0.1),
        zone_bounds={
            zone.value: {"floor": _provisional(0.2), "ceiling": _provisional(0.5)} for zone in Zone
        },
    )
    controller, _binding, _ = build_controller(trained, policy_config=settings)

    result = propose(controller, observed=observed_input(1.0), safety_floor=0.2)

    assert result.proposal is None
    assert result.failure is LearnedFailure.OPTIMIZER_EXCEPTION
    assert result.failure_reason is not None
    assert "ceiling" in result.failure_reason.detail


# ------------------------------- 0079 段 4: PlanPrediction と格子（0079 §2.3 / 0087 §2.5）


def test_a_plan_prediction_follows_the_action_grid_of_the_artifact(trained) -> None:
    """候補 plan の予測は **action schema の格子の上**で、offset と plan の step が対応する。

    `ActionPlan.steps[k]` は Dataset v2 / artifact v2 の step `k`（区間の終端が `offset_ms`）と
    同じ区間である（0087 §2.5）。予測は plan の識別子と anchor 推論に束ねられる。
    """
    binding = bind(trained)
    observed = observed_input()
    plan = ActionPlan(
        step_ms=STEP_MS,
        steps=tuple(
            PlanStep(offset_ms=STEP_MS * (index + 1), demands=demands(value))
            for index, value in enumerate((0.4, 0.6, 0.8))
        ),
    )

    anchor = binding.predict(observed)
    prediction = binding.predict_plan(
        PlannedThermalInput(observed=observed, plan=plan), anchor=anchor
    )

    assert prediction.matches(plan)
    assert prediction.plan_digest == plan.digest()
    assert prediction.capability is InferenceCapability.COUNTERFACTUAL_ACTION
    assert prediction.artifact_sha256 == trained.attestation.artifact_sha256
    assert prediction.anchor_inference_id == inference_id(observed, anchor)
    assert tuple(target.offset_ms for target in prediction.targets) == HORIZONS
    # step k の action は horizon (k + 1) × step_ms の予測から効き始める（因果の mask）。
    expected = trained.model.predict_trajectory(
        observed,
        ActionTrajectory(step_ms=plan.step_ms, demands=tuple(step.demands for step in plan.steps)),
    )
    for target, reference in zip(prediction.targets, expected.targets, strict=True):
        assert target.values == reference.values


def test_the_anchor_inference_equals_the_held_plan_of_the_current_demand(trained) -> None:
    """anchor 推論（`hold_effective`）は、いまの effective を保つ held plan の予測と一致する。"""
    binding = bind(trained)
    observed = observed_input(0.45)
    held = ActionPlan.held(demands(0.45), step_ms=STEP_MS, steps=STEPS)

    anchor = binding.predict(observed)
    planned = binding.predict_plan(PlannedThermalInput(observed=observed, plan=held), anchor=anchor)

    assert [target.values for target in anchor.targets] == [
        target.values for target in planned.targets
    ]


@pytest.mark.parametrize(
    "plan",
    [
        ActionPlan.held(demands(0.5), step_ms=STEP_MS, steps=STEPS - 1),
        ActionPlan.held(demands(0.5), step_ms=STEP_MS * 2, steps=STEPS),
    ],
    ids=["fewer-steps", "other-step-ms"],
)
def test_a_plan_off_the_action_grid_is_never_predicted(trained, plan: ActionPlan) -> None:
    """**plan の格子が action schema と違えば予測しない**（0079 §2.3）。"""
    binding = bind(trained)

    with pytest.raises(ValueError, match="格子"):
        _with_anchor(binding, PlannedThermalInput(observed=observed_input(), plan=plan))
    with pytest.raises(ValueError, match="格子"):
        binding.plan_support_violation(observed_input(), plan)


# ------------------------- 0079 段 4: 学習した action 列の外は探索しない（0079 §2.5 / 0084 §2.2）


def zone_ranges(front: tuple[float, float], other: tuple[float, float]) -> PerZone[ValueRange]:
    """Front だけ別の範囲にした step の support。"""
    whole = ranges(other[0], other[1])
    return whole.model_copy(
        update={
            "front": ValueRange(
                source="fan.front", minimum=front[0], maximum=front[1], observed_count=10_000
            )
        }
    )


def _evaluated_fronts(plans: Sequence[ActionPlan]) -> set[float]:
    return {round(plan.first.front, 6) for plan in plans}


def test_candidates_outside_the_learned_step_ranges_are_not_evaluated(
    tmp_path: Path, plan_calls: list[ActionPlan]
) -> None:
    """**観測した demand の範囲の外の候補は評価しない**（margin を掛けない。0079 §2.5）。

    Front の計画 demand を 0.2〜0.6 でだけ観測した Profile では、Front 0.8 / 1.0 の候補は
    評価されない（`range_margin` の幅に入る 0.6 を少し越える値も同じ）。
    """
    narrow = make_artifact(
        tmp_path / "narrow",
        support=action_support(
            steps=tuple((zone_ranges((0.2, 0.6), (0.0, 1.0)), CELLS) for _ in range(STEPS))
        ),
    )
    controller, binding, _settings = build_controller(narrow)

    result = propose(controller, baseline=0.4)

    assert result.solution is not None
    assert plan_calls, "Baseline は範囲内なので評価される"
    assert all(0.2 <= plan.steps[k].demands.front <= 0.6 for plan in plan_calls for k in range(3))
    assert 0.2 <= result.solution.requested.front <= 0.6
    # 端をわずかに越える値（margin の幅の中）も評価しない。
    just_outside = ActionPlan.held(
        PerZone[Demand](front=0.6 + 1e-9, rear=0.45, top=0.45), step_ms=STEP_MS, steps=STEPS
    )
    assert binding.plan_support_violation(observed_input(), just_outside) is not None
    full_controller, _full, _ = build_controller(
        MpcArtifact(register_v2(tmp_path / "wide"), tmp_path)
    )
    plan_calls.clear()
    propose(full_controller, baseline=0.4)
    assert max(plan.first.front for plan in plan_calls) > 0.6


def test_an_anchor_jump_outside_the_learned_change_is_not_evaluated(
    tmp_path: Path, plan_calls: list[ActionPlan]
) -> None:
    """**anchor → 最初の step の跳び**だけが範囲外の held plan も評価しない（0079 §2.5）。

    held plan は step 間の変化量が 0 なので、step 間だけを見ると常に通る。
    """
    small_jumps = make_artifact(
        tmp_path / "small-jumps",
        support=action_support(anchor=(ranges(-0.1, 0.1), EVERY_TRANSITION)),
    )
    controller, binding, _settings = build_controller(small_jumps)

    result = propose(controller, baseline=0.45)

    assert result.solution is not None
    anchor = observed_input().action.front.effective_demand
    for plan in plan_calls:
        for zone in Zone:
            assert abs(plan.first.get(zone) - anchor) <= 0.1 + 1e-12
    jump = ActionPlan.held(demands(0.9), step_ms=STEP_MS, steps=STEPS)
    violation = binding.plan_support_violation(observed_input(), jump)
    assert violation is not None and violation.kind.value == "anchor_delta"


def test_a_cross_zone_combination_outside_the_joint_cells_is_not_evaluated(
    tmp_path: Path, plan_calls: list[ActionPlan]
) -> None:
    """**zone ごとの範囲に入っても、zone の組として学習していない候補は評価しない**（0079 §2.5）。

    全 zone が同じ側（すべて 0.5 未満か、すべて 0.5 以上）の cell だけを観測した Profile では、
    Front だけを上げた (1, 0, 0) の候補は評価されない。
    """
    same_side = ((0, 0, 0), (1, 1, 1))
    joint = make_artifact(
        tmp_path / "joint",
        support=action_support(
            steps=tuple((ranges(0.0, 1.0), same_side) for _ in range(STEPS)),
            anchor=(ranges(-1.0, 1.0), tuple((a, b) for a in same_side for b in same_side)),
            pairs=tuple(
                (ranges(-1.0, 1.0), tuple((a, b) for a in same_side for b in same_side))
                for _ in range(STEPS - 1)
            ),
        ),
    )
    controller, binding, _settings = build_controller(joint)

    result = propose(controller, baseline=0.4)

    assert result.proposal is not None
    for plan in plan_calls:
        bins = {int(plan.first.get(zone) >= 0.5) for zone in Zone}
        assert len(bins) == 1, plan.first
    mixed = ActionPlan.held(
        PerZone[Demand](front=0.9, rear=0.3, top=0.3), step_ms=STEP_MS, steps=STEPS
    )
    violation = binding.plan_support_violation(observed_input(), mixed)
    assert violation is not None and violation.kind.value == "step_cell"


def test_a_value_learned_only_at_step_zero_is_not_accepted_at_a_later_step(tmp_path: Path) -> None:
    """**step ごとの support**: step 0 でだけ観測した値を遅い step に持つ候補は評価しない。"""
    early = make_artifact(
        tmp_path / "early",
        support=action_support(
            steps=(
                (ranges(0.0, 1.0), CELLS),
                (ranges(0.0, 0.6), CELLS),
                (ranges(0.0, 0.6), CELLS),
            )
        ),
    )
    binding = bind(early)
    observed = observed_input(0.45)
    rising = ActionPlan(
        step_ms=STEP_MS,
        steps=tuple(
            PlanStep(offset_ms=STEP_MS * (index + 1), demands=demands(value))
            for index, value in enumerate((0.8, 0.5, 0.5))
        ),
    )
    late = ActionPlan(
        step_ms=STEP_MS,
        steps=tuple(
            PlanStep(offset_ms=STEP_MS * (index + 1), demands=demands(value))
            for index, value in enumerate((0.5, 0.5, 0.8))
        ),
    )

    assert binding.plan_support_violation(observed, rising) is None
    violation = binding.plan_support_violation(observed, late)
    assert violation is not None
    assert (violation.kind.value, violation.step) == ("step_demand", 2)


def test_a_fallback_request_outside_the_learned_range_is_an_optimizer_error(
    tmp_path: Path, plan_calls: list[ActionPlan]
) -> None:
    """**Fallback の requested 自体が範囲外なら解を返さない**（`plan_out_of_learned_range`）。

    範囲内へ丸めて探索を続けない。`MpcProposal` の不変条件（提案のある結果に
    `failure_reason` を付けない）を破らず、理由は requested の理由に載る。Gate は
    `optimizer_error` として Fallback を選ぶ（0079 §2.5 / §6 の質問 6）。
    """
    settings = mpc_policy()
    upper_only = make_artifact(
        tmp_path / "upper-only",
        support=action_support(
            steps=(
                (ranges(0.0, 1.0), CELLS),
                (ranges(0.0, 1.0), CELLS),
                (ranges(0.4, 1.0), CELLS),
            )
        ),
    )
    controller, _binding, _ = build_controller(upper_only, policy_config=settings)

    result = propose(controller, baseline=0.35)

    assert plan_calls == []
    assert result.failure is None and result.failure_reason is None
    assert result.solution is None
    assert result.proposal is not None
    assert result.proposal.optimizer_status is OptimizerStatus.ERROR
    reason = result.proposal.requested.front.reason
    assert reason.code == "plan_out_of_learned_range"
    assert "step=2" in reason.detail and "zone=front" in reason.detail
    # Fallback の requested をそのまま運ぶ（丸めた値を ML の提案にしない）。
    assert result.proposal.requested.front.demand == pytest.approx(0.35)

    selection = select_with(
        settings, upper_only.attestation, result, fallback=fallback_proposal(0.35)
    )
    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.OPTIMIZER_ERROR.value
    assert selection.proposal == fallback_proposal(0.35)

    # 同じ artifact で、範囲内の Fallback requested なら探索する。
    assert propose(controller, baseline=0.45).solution is not None


# ------------------------ 0079 段 4: 読み込みの失敗は MODEL_LOAD_FAILURE → Fallback（§2.6）


def _assert_degrades_to_fallback(
    runtime: LearnedMpcRuntime, attestation: ArtifactAttestation, *, needle: str
) -> None:
    settings = mpc_policy()
    assert runtime.controller is None
    assert runtime.failure_reason is not None
    assert needle in runtime.failure_reason.detail

    for ts_ms in (ACTION_TS_MS, ACTION_TS_MS):
        result = propose(runtime, ts_ms=ts_ms)
        assert result.failure is LearnedFailure.MODEL_LOAD_FAILURE
        assert result.failure_reason == runtime.failure_reason
    fallback = fallback_proposal(0.4)
    selection = select_with(settings, attestation, result, fallback=fallback)

    assert selection.active_controller is ControllerKind.FALLBACK
    assert selection.fallback_reason is not None
    assert selection.fallback_reason.code == FallbackCause.MODEL_LOAD_FAILURE.value
    assert needle in selection.fallback_reason.detail
    # Gate は Fallback の requested をそのまま後段へ渡す。Guard / Safety の入力は変わらない。
    assert selection.proposal == fallback


def test_a_calibration_mismatch_degrades_to_fallback(trained) -> None:
    """**較正の digest が runtime の較正と違えば L9 で拒否して Fallback**（0079 §6 の質問 4）。"""
    changed = RuntimeCalibration.available({**CALIBRATION_OFFSETS, "front_intake": 0.2})

    runtime = load_runtime(trained.verified, calibration=changed)

    _assert_degrades_to_fallback(runtime, trained.attestation, needle="L9")
    # 使わないチャネルだけの変更では digest が変わらない（0096 §2.9 の感度）。
    unrelated = RuntimeCalibration.available({**CALIBRATION_OFFSETS, "room_temp": 1.0})
    assert load_runtime(trained.verified, calibration=unrelated).controller is not None


def test_an_unreadable_calibration_refuses_a_calibrated_artifact(trained) -> None:
    """**読めなかった較正では、較正の掛かる metric を使う artifact を使わない**（0096 §5 #8）。"""
    runtime = load_runtime(trained.verified, calibration=UNAVAILABLE_CALIBRATION)

    _assert_degrades_to_fallback(runtime, trained.attestation, needle="L9")
    assert "読めなかった" in (runtime.failure_reason.detail if runtime.failure_reason else "")


def test_an_artifact_without_calibrated_metrics_loads_without_a_calibration(
    tmp_path: Path,
) -> None:
    """**較正の掛からない metric だけの artifact は、較正を読めなくても使える**（0096 §5 #8）。"""
    uncalibrated = register_v2(tmp_path / "gpu-only", features=(GPU,))
    model = RegistryCounterfactualThermalModel.from_verified_artifact(
        uncalibrated, metric_catalog=CATALOG, calibration=UNAVAILABLE_CALIBRATION
    )
    assert model.manifest.calibration_binding.sha256 is None

    runtime = load_runtime(uncalibrated, calibration=UNAVAILABLE_CALIBRATION)

    assert runtime.failure_reason is None
    assert runtime.controller is not None
    assert runtime.controller.binding.artifact_sha256 == uncalibrated.attestation.artifact_sha256


def test_a_v1_artifact_degrades_to_fallback_and_is_never_read_as_v2(tmp_path: Path) -> None:
    """**v1 artifact は v2 として読み替えず、Fallback で運転を続ける**（0079 §2.2）。"""
    claimed = register_v1(tmp_path / "v1", capability=ArtifactCapability.COUNTERFACTUAL_ACTION)

    _assert_degrades_to_fallback(load_runtime(claimed), claimed.attestation, needle="L4")


def test_a_unit_change_in_the_runtime_catalog_degrades_to_fallback(trained) -> None:
    """**runtime の単位が学習時と違えば L8 で拒否**して Fallback（0079 §2.4）。"""
    document_ = json.loads(json.dumps(CATALOG.model_dump(mode="json")))
    document_["metrics"][GPU]["unit"] = "F"
    changed = MetricCatalog.model_validate(document_)

    _assert_degrades_to_fallback(
        load_runtime(trained.verified, catalog=changed), trained.attestation, needle="L8"
    )


def test_a_grid_mismatch_with_the_config_degrades_to_fallback(trained) -> None:
    """**格子が合わない設定**も、起動を止めずに `MODEL_LOAD_FAILURE` で Fallback（0079 §2.6）。"""
    settings = mpc_policy(step_ms=_provisional(1_500), horizon_ms=_provisional(4_500))

    runtime = load_runtime(trained.verified, settings=settings)

    _assert_degrades_to_fallback(runtime, trained.attestation, needle="格子")


def test_a_healthy_runtime_proposes_like_the_controller(trained) -> None:
    """読み込みに成功した runtime は、controller と同じ提案を出す（経路を増やさない）。"""
    runtime = load_runtime(trained.verified)
    controller, _binding, _settings = build_controller(trained)

    assert runtime.failure_reason is None
    assert propose(runtime).result_digest() == propose(controller).result_digest()
    with pytest.raises(ValueError, match="どちらか一方"):
        LearnedMpcRuntime(controller=None, failure_reason=None)


def test_the_anchor_inference_runs_once_per_tick(trained, monkeypatch: pytest.MonkeyPatch) -> None:
    """**候補ごとに anchor 推論をやり直さない**（予算を候補の評価に使う。PR #239 の Codex P2）。"""
    calls: list[int] = []
    original = RegistryCounterfactualThermalModel.predict

    def counting(
        self: RegistryCounterfactualThermalModel, observed: ObservedThermalInput
    ) -> ThermalPrediction:
        calls.append(observed.action_ts_ms)
        return original(self, observed)

    monkeypatch.setattr(RegistryCounterfactualThermalModel, "predict", counting)
    controller, _binding, _settings = build_controller(trained)

    result = propose(controller)

    assert result.solution is not None and result.solution.evaluations > 1
    # anchor 推論1回 + 判定器が held の列を照らすだけ（予測はしない）。
    assert calls == [ACTION_TS_MS]


def test_a_plan_prediction_needs_the_anchor_of_the_same_artifact_and_input(trained) -> None:
    """別の観測・別の artifact の anchor 推論を渡されたら予測しない。"""
    binding = bind(trained)
    plan = ActionPlan.held(demands(0.5), step_ms=STEP_MS, steps=STEPS)
    planned = PlannedThermalInput(observed=observed_input(), plan=plan)
    anchor = binding.predict(observed_input())

    for other in (
        ThermalPrediction.model_validate(
            anchor.model_dump(mode="python") | {"artifact_sha256": "c" * 64}
        ),
        binding.predict(dataset_observed(ACTION_TS_MS + 5_000)),
    ):
        with pytest.raises(ValueError, match="anchor"):
            binding.predict_plan(planned, anchor=other)


def dataset_observed(action_ts_ms: int) -> ObservedThermalInput:
    """別の anchor 時刻の観測 window。"""
    data = dataset(HORIZONS, TARGETS)
    example = next(item for item in data.examples if item.action_ts_ms == action_ts_ms)
    return ObservedThermalInput.from_example(example)


def test_incomplete_cost_wiring_degrades_to_fallback(trained) -> None:
    """**目的関数の任意依存の組み立ての失敗**も `MODEL_LOAD_FAILURE`（PR #239 の Codex P2）。

    Air Balance を渡して `fan-hardware.yaml` の profile を渡さない組み合わせ（決定記録 0073 §2.3）。
    """
    settings = mpc_policy()
    runtime = LearnedMpcRuntime.load(
        trained.verified,
        settings,
        safety(),
        metric_catalog=CATALOG,
        calibration=RUNTIME_CALIBRATION,
        expected_model_version=trained.attestation.version,
        monotonic_ms=ScriptedClock(0),
        authority=StaticAuthorityStage(settings.authority_stage),
        air_balance=StubAirBalance(1.0),
        balance_band=_balance_band(),
    )

    _assert_degrades_to_fallback(runtime, trained.attestation, needle="fan-hardware.yaml")
