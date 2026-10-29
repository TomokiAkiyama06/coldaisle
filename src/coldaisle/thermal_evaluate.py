"""合成の起点: 反実仮想 Thermal Model の offline 評価の CLI ``coldaisle-thermal-evaluate``。

#84 / 決定記録 0172。

Registry に登録された反実仮想 Thermal Model artifact v2 を、**その artifact を学習した dataset の
test split だけ**で評価し、未学習の baseline（``persistence``）と対で比べた報告を ``--out`` に書く。

```bash
uv run coldaisle-thermal-evaluate --registry-root var/model-registry --registry-limits config \\
    --artifact rack-thermal@0.1.0 --dataset var/datasets/dataset-<32 hex> \\
    --validation-start-ms <学習の split の境界> --test-start-ms <学習の split の境界> \\
    --evaluation-config var/thermal-evaluation.yaml --metrics-config config/metrics.yaml \\
    --calibration config/calibration.json --out var/thermal-evaluation/rack-thermal-0.1.0.json
```

- **読み取りだけ。** 書くのは ``--out`` の1ファイルだけで、Registry・設定・DB に書かない。
  本番の DB は開かない（0172 §2.1 / §2.2）
- **どこからも import しない**（``train.py`` と同じ扱い）。LLM に依らない。人が手で実行する
- ``requested_demand``・PWM・hwmon・Authority Stage・制御設定へ届く経路を持たない（0172 §2.8）。
  gate の ``pass`` は助言で、validated にするかは人が決める（0172 §2.6）
- 壁時計を読まない。乱数を使わない。同じ入力からは同じ bytes の報告を書く

失敗は構造化ログに段（``stage``）を出して終了コード 1（``--out`` は作らない）。報告を書けたときは
gate が ``blocked`` でも終了コード 0（``blocked`` は評価の結果で、実行の失敗ではない）。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import os
import stat
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from coldaisle import logs
from coldaisle.control.evaluation.stats import summarize
from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.model.confidence import ResidualScale
from coldaisle.control.model.counterfactual import (
    FEATURE_SCHEMA_V2_VERSION,
    TARGET_SCHEMA_V2_VERSION,
    ActionTrajectory,
    RegistryCounterfactualThermalModel,
    ThermalActionSchema,
    TimeWindow,
    hold_effective,
)
from coldaisle.control.model.counterfactual_confidence import StepSupportChecker
from coldaisle.control.model.counterfactual_training import (
    load_training_dataset_artifact_v2,
    recorded_trajectory,
)
from coldaisle.control.model.dataset import DatasetExampleV2, WindowFrame, split_temporally_v2
from coldaisle.control.model.thermal import ObservedThermalInput, PredictedTarget
from coldaisle.control.model.training import split_sha256
from coldaisle.control.model_registry import (
    MODEL_REGISTRY_CONFIG_FILENAME,
    ArtifactCapability,
    ArtifactKind,
    ArtifactLoadStatus,
    ArtifactRef,
    ArtifactStatus,
    ModelCompatibility,
    ModelRegistry,
    ModelRegistryError,
    ModelRegistryLimits,
)
from coldaisle.control.schema import AuthorityStage, PerZone
from coldaisle.dataset import open_output_root
from coldaisle.ingest.calibration import Calibration
from coldaisle.measurement import Quality
from coldaisle.metrics import MetricCatalog
from coldaisle.thermal_evaluation_report import (
    AXIS_ACTION_CHANGE,
    AXIS_ACTION_SUPPORT,
    AXIS_ROOM_TEMPERATURE,
    AXIS_WORKLOAD,
    AXIS_WORKLOAD_REGIME,
    GATING_AXES,
    PROCEDURE_VERSION,
    REPORT_SCHEMA_VERSION,
    UNKNOWN_STRATUM,
    ActionEffect,
    ActionEffectOutput,
    ArtifactSection,
    CodeSection,
    DatasetSection,
    ErrorSummary,
    EvaluationSection,
    GateResult,
    GroupResult,
    InputsSection,
    MeanSummary,
    NormalizedError,
    OutputKey,
    OutputResult,
    PairedComparison,
    QuantileValue,
    SignTest,
    SplitSection,
    SplitWindow,
    StratumAxis,
    StratumResult,
    ThermalEvaluationConfig,
    ThermalEvaluationReport,
    WorstCase,
    band_of,
    canonical_report_bytes,
    conditions_sha256,
    evaluate_gate,
    offline_evaluation_ref,
    parse_evaluation_config,
    sign_test_p_value,
    stratum_status,
)
from coldaisle.training_output import UntrustedPathError, check_trusted_parent

LOGGER = logging.getLogger("coldaisle.thermal_evaluate")

EXIT_OK = 0
EXIT_FAILED = 1

MAX_EVALUATION_CONFIG_BYTES = 1024 * 1024
"""設定の入力（4つに共通）を読む上限。

運用の閾値ではなくメモリを守る構造上の上限（0150 §5 の 12 と同じ値）。
"""

# O_NONBLOCK: 書き手の無い FIFO を開いて止まらない（regular file の読み込みには効かない）
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_CHUNK_BYTES = 64 * 1024

# 除外の理由（example 単位。0172 §2.3）
EXCLUDED_OBSERVED_INPUT = "observed_input"
EXCLUDED_ACTION_TRAJECTORY = "action_trajectory"
EXCLUDED_PREDICTION = "prediction"
# 除外の理由（(example, 出力) 単位）
EXCLUDED_TARGET_MISSING = "target_missing"
EXCLUDED_TARGET_QUALITY = "target_quality"
EXCLUDED_PREDICTION_MISSING = "prediction_missing"
EXCLUDED_PERSISTENCE_UNAVAILABLE = "persistence_unavailable"
EXCLUDED_PERSISTENCE_MISSING = "persistence_missing"
EXCLUDED_PERSISTENCE_STALE = "persistence_stale"
EXCLUDED_PERSISTENCE_QUALITY = "persistence_quality"

_EVALUATION_ERRORS = (ValueError, TypeError, OSError, ModelRegistryError, RecursionError)
"""検査・評価の失敗として終了コード 1 にする例外（握りつぶさず、理由をログに出す）。"""

Predictor = Callable[[ObservedThermalInput, ActionTrajectory], tuple[PredictedTarget, ...]]
"""観測と action 列から target の予測を返す。予測できなければ ``ValueError``。"""

SupportCheck = Callable[[ObservedThermalInput, ActionTrajectory], bool]
"""実際の action 列が Profile の step ごとの support の中か（報告だけの層）。"""


class EvaluationStopped(Exception):
    """1つの段で止まった。``stage`` と ``fields`` を構造化ログに出す。"""

    def __init__(self, message: str, *, stage: str, fields: Mapping[str, object]) -> None:
        super().__init__(message)
        self.stage = stage
        self.fields = dict(fields)


# ---------------------------------------------------------------- 設定の1回の読み


def read_config_bytes(path: Path, *, label: str) -> bytes:
    """上限＋1 byte まで**だけ**読む。symlink をたどらず regular file だけ（0150 §2.7 と同じ）。"""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise ValueError(f"{label} を開けない: {path}: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{label} が regular file ではない: {path}")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_EVALUATION_CONFIG_BYTES:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, MAX_EVALUATION_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    if total > MAX_EVALUATION_CONFIG_BYTES:
        raise ValueError(f"{label} が上限 {MAX_EVALUATION_CONFIG_BYTES} byte を超える: {path}")
    return b"".join(chunks)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationInputs:
    """1回だけ読んだ設定と、同じ bytes の SHA-256。"""

    config: ThermalEvaluationConfig
    config_sha256: str
    metric_catalog: MetricCatalog
    metrics_sha256: str
    calibration: Calibration
    calibration_sha256: str
    registry_limits: ModelRegistryLimits


def read_inputs(args: argparse.Namespace) -> EvaluationInputs:
    """4つの設定を上限で切って1回だけ読み、その bytes から parse して hash する。"""
    config_bytes = read_config_bytes(args.evaluation_config, label="--evaluation-config")
    config = parse_evaluation_config(config_bytes)
    metrics_bytes = read_config_bytes(args.metrics_config, label="--metrics-config")
    try:
        catalog, metrics_sha256 = MetricCatalog.from_yaml_bytes_with_sha256(
            metrics_bytes, origin=str(args.metrics_config)
        )
    except RecursionError as error:
        raise ValueError("--metrics-config の入れ子が深すぎる") from error
    calibration_bytes = read_config_bytes(args.calibration, label="--calibration")
    try:
        calibration = Calibration.from_json_bytes(calibration_bytes, origin=str(args.calibration))
    except RecursionError as error:
        raise ValueError("--calibration の入れ子が深すぎる") from error
    limits_bytes = read_config_bytes(
        args.registry_limits / MODEL_REGISTRY_CONFIG_FILENAME, label="--registry-limits"
    )
    limits = ModelRegistryLimits.model_validate(_yaml_mapping(limits_bytes))
    return EvaluationInputs(
        config=config,
        config_sha256=_sha256(config_bytes),
        metric_catalog=catalog,
        metrics_sha256=metrics_sha256,
        calibration=calibration,
        calibration_sha256=_sha256(calibration_bytes),
        registry_limits=limits,
    )


def _yaml_mapping(payload: bytes) -> dict[str, Any]:
    try:
        loaded: object = yaml.safe_load(payload.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as error:
        raise ValueError(f"--registry-limits を読めない: {error}") from error
    if not isinstance(loaded, dict):
        raise ValueError("--registry-limits が辞書ではない")
    return loaded


# ---------------------------------------------------------------- 1 example の評価（0172 §2.3）


def _usable_value(frame: WindowFrame, metric: str) -> float | None:
    """frame の metric が欠測・stale・``quality != ok`` でなければ値。"""
    if metric not in frame.values:
        return None
    value = frame.values[metric]
    if (
        value is None
        or frame.missing_mask[metric]
        or frame.stale_mask[metric]
        or frame.quality[metric] is not Quality.OK
    ):
        return None
    return value


def _persistence(example: DatasetExampleV2, metric: str) -> tuple[float | None, str | None]:
    """anchor の時点の window の最後のフレームの同じ metric（0172 §2.3）。無ければ理由。"""
    frame = example.window[-1]
    if metric not in frame.values:
        return None, EXCLUDED_PERSISTENCE_UNAVAILABLE
    if frame.values[metric] is None or frame.missing_mask[metric]:
        return None, EXCLUDED_PERSISTENCE_MISSING
    if frame.stale_mask[metric]:
        return None, EXCLUDED_PERSISTENCE_STALE
    if frame.quality[metric] is not Quality.OK:
        return None, EXCLUDED_PERSISTENCE_QUALITY
    return frame.values[metric], None


def _room_stratum(example: DatasetExampleV2, config: ThermalEvaluationConfig) -> str:
    value = _usable_value(example.window[-1], config.room_temperature_metric)
    if value is None:
        return UNKNOWN_STRATUM
    return band_of(
        value,
        tuple(
            (band.name, None if band.below_c is None else band.below_c.value)
            for band in config.room_temperature_bands
        ),
    )


def _window_mean(example: DatasetExampleV2, metric: str, window_ms: int) -> float | None:
    start = example.action_ts_ms - window_ms
    values = [
        value
        for frame in example.window
        if frame.ts_ms > start
        for value in (_usable_value(frame, metric),)
        if value is not None
    ]
    if not values:
        return None
    return math.fsum(values) / len(values)


def _workload_stratum(example: DatasetExampleV2, config: ThermalEvaluationConfig) -> str:
    """CPU と GPU の電力の窓の平均の帯の組み合わせ（0172 §2.4 / §5 の 4）。"""
    bands = config.workload_bands
    window_ms = bands.window_ms.value
    cpu = _window_mean(example, bands.cpu_metric, window_ms)
    gpu = _window_mean(example, bands.gpu_metric, window_ms)
    if cpu is None or gpu is None:
        return UNKNOWN_STRATUM
    cpu_band = band_of(
        cpu,
        tuple(
            (band.name, None if band.below_w is None else band.below_w.value)
            for band in bands.cpu_power
        ),
    )
    gpu_band = band_of(
        gpu,
        tuple(
            (band.name, None if band.below_w is None else band.below_w.value)
            for band in bands.gpu_power
        ),
    )
    return f"cpu_{cpu_band}.gpu_{gpu_band}"


def action_changed(example: DatasetExampleV2) -> bool:
    """action 列のどこかの step が ``prior_action`` と違う（0172 §2.3）。"""
    prior = example.prior_action.effective_demand
    return any(step.effective_demand != prior for step in example.action_steps)


@dataclass(frozen=True, slots=True)
class _Cell:
    """1つの (example, 出力)。"""

    example_id: str
    action_ts_ms: int
    block: int
    observed: float | None
    model: float | None
    held: float | None
    persistence: float | None
    excluded: str | None
    """target か予測が無い理由（``None`` なら ``observed`` と ``model`` がある）。"""
    persistence_excluded: str | None


@dataclass(frozen=True, slots=True)
class _ExampleResult:
    block: int
    strata: Mapping[StratumAxis, str]
    cells: Mapping[OutputKey, _Cell]
    changed: bool
    held_available: bool


def _predicted_value(targets: Sequence[PredictedTarget], key: OutputKey) -> float | None:
    for target in targets:
        if target.horizon_ms == key.horizon_ms:
            return target.values.get(key.metric)
    return None


def _evaluate_example(
    example: DatasetExampleV2,
    *,
    predictor: Predictor,
    action_schema: ThermalActionSchema,
    outputs: tuple[OutputKey, ...],
    config: ThermalEvaluationConfig,
    block_origin_ms: int,
    support: SupportCheck | None,
) -> _ExampleResult | str:
    """予測と baseline を作る。予測できなければ除外の理由（補わない。0172 §2.3）。"""
    try:
        observed = ObservedThermalInput.from_example_v2(example)
    except ValueError:
        return EXCLUDED_OBSERVED_INPUT
    try:
        trajectory = recorded_trajectory(example, action_schema)
    except ValueError:
        return EXCLUDED_ACTION_TRAJECTORY
    try:
        predicted = predictor(observed, trajectory)
    except ValueError:
        return EXCLUDED_PREDICTION
    held: tuple[PredictedTarget, ...] | None
    try:
        held = predictor(observed, hold_effective(observed, action_schema))
    except ValueError:
        held = None
    block = (example.action_ts_ms - block_origin_ms) // config.block_ms.value
    regime = example.context.workload_regime
    changed = action_changed(example)
    strata: dict[StratumAxis, str] = {
        AXIS_ROOM_TEMPERATURE: _room_stratum(example, config),
        AXIS_WORKLOAD: _workload_stratum(example, config),
        AXIS_ACTION_CHANGE: "changed" if changed else "held",
        AXIS_WORKLOAD_REGIME: UNKNOWN_STRATUM if regime is None else regime.value,
    }
    if support is not None:
        try:
            inside = support(observed, trajectory)
        except ValueError:
            strata[AXIS_ACTION_SUPPORT] = UNKNOWN_STRATUM
        else:
            strata[AXIS_ACTION_SUPPORT] = "inside" if inside else "outside"
    targets = {target.horizon_ms: target for target in example.targets}
    cells: dict[OutputKey, _Cell] = {}
    for key in outputs:
        persistence, persistence_excluded = _persistence(example, key.metric)
        target = targets.get(key.horizon_ms)
        observed_value: float | None = None
        excluded: str | None = None
        if (
            target is None
            or key.metric not in target.values
            or target.values[key.metric] is None
            or target.missing_mask[key.metric]
        ):
            excluded = EXCLUDED_TARGET_MISSING
        elif target.quality[key.metric] is not Quality.OK:
            excluded = EXCLUDED_TARGET_QUALITY
        else:
            observed_value = target.values[key.metric]
        model_value = _predicted_value(predicted, key)
        if excluded is None and model_value is None:
            excluded = EXCLUDED_PREDICTION_MISSING
        cells[key] = _Cell(
            example_id=example.example_id,
            action_ts_ms=example.action_ts_ms,
            block=block,
            observed=observed_value,
            model=model_value,
            held=None if held is None else _predicted_value(held, key),
            persistence=persistence,
            excluded=excluded,
            persistence_excluded=persistence_excluded,
        )
    return _ExampleResult(
        block=block, strata=strata, cells=cells, changed=changed, held_available=held is not None
    )


# ---------------------------------------------------------------- 指標（0172 §2.4）


def _worst(items: Iterable[tuple[float, _Cell, float]], count: int) -> tuple[WorstCase, ...]:
    """``(並べる値, cell, e)`` の大きい順に ``count`` 件（同値は時刻・ID の順で決める）。"""
    ordered = sorted(items, key=lambda item: (-item[0], item[1].action_ts_ms, item[1].example_id))
    return tuple(
        WorstCase(example_id=cell.example_id, action_ts_ms=cell.action_ts_ms, error=error)
        for _value, cell, error in ordered[:count]
    )


def _error_summary(
    pairs: Sequence[tuple[_Cell, float]], config: ThermalEvaluationConfig
) -> ErrorSummary | None:
    """``(cell, 予測)`` の列の誤差。``e = 予測 − 観測``・``u = max(0, 観測 − 予測)``。"""
    if not pairs:
        return None
    errors: list[tuple[_Cell, float]] = []
    for cell, predicted in pairs:
        assert cell.observed is not None
        errors.append((cell, predicted - cell.observed))
    values = [error for _cell, error in errors]
    under = [max(0.0, -error) for error in values]
    summary = summarize(under, quantiles=config.quantiles)
    assert summary is not None
    count = len(values)
    return ErrorSummary(
        count=count,
        mae=math.fsum(abs(error) for error in values) / count,
        rmse=math.sqrt(math.fsum(error * error for error in values) / count),
        bias=math.fsum(values) / count,
        underprediction_quantiles=tuple(
            QuantileValue(quantile=item.quantile, value=item.value) for item in summary.percentiles
        ),
        underprediction_max=summary.maximum,
        abs_error_max=max(abs(error) for error in values),
        worst_underprediction=_worst(
            ((max(0.0, -error), cell, error) for cell, error in errors), config.worst_case_count
        ),
        worst_abs_error=_worst(
            ((abs(error), cell, error) for cell, error in errors), config.worst_case_count
        ),
    )


def _mean_summary(cells: Sequence[_Cell], mean: float | None) -> MeanSummary | None:
    if mean is None or not cells:
        return None
    values: list[float] = []
    for cell in cells:
        assert cell.observed is not None
        values.append(mean - cell.observed)
    count = len(values)
    return MeanSummary(
        count=count,
        mae=math.fsum(abs(error) for error in values) / count,
        rmse=math.sqrt(math.fsum(error * error for error in values) / count),
        bias=math.fsum(values) / count,
    )


def _sign_test(paired: Sequence[_Cell]) -> SignTest:
    """ブロックごとの ``d = |e_model| − |e_persistence|`` の平均の符号で数える（0172 §2.4）。"""
    by_block: dict[int, list[float]] = defaultdict(list)
    for cell in paired:
        assert cell.observed is not None and cell.model is not None
        assert cell.persistence is not None
        by_block[cell.block].append(
            abs(cell.model - cell.observed) - abs(cell.persistence - cell.observed)
        )
    model_better = persistence_better = ties = 0
    for block in sorted(by_block):
        values = by_block[block]
        mean = math.fsum(values) / len(values)
        if mean < 0.0:
            model_better += 1
        elif mean > 0.0:
            persistence_better += 1
        else:
            ties += 1
    p_value = sign_test_p_value(model_better, model_better + persistence_better)
    return SignTest(
        model_better=model_better,
        persistence_better=persistence_better,
        ties=ties,
        p_value=float(p_value),
    )


def _output_result(
    key: OutputKey,
    cells: Sequence[_Cell],
    *,
    config: ThermalEvaluationConfig,
    train_mean: float | None,
    residual_scale: float | None,
) -> OutputResult:
    excluded: Counter[str] = Counter()
    scored: list[_Cell] = []
    for cell in cells:
        if cell.excluded is not None:
            excluded[cell.excluded] += 1
            continue
        scored.append(cell)
        if cell.persistence_excluded is not None:
            excluded[cell.persistence_excluded] += 1
    paired = [cell for cell in scored if cell.persistence is not None]
    model_summary = _error_summary(
        [(cell, cell.model) for cell in scored if cell.model is not None], config
    )
    comparison: PairedComparison | None = None
    if paired:
        paired_model = _error_summary(
            [(cell, cell.model) for cell in paired if cell.model is not None], config
        )
        paired_persistence = _error_summary(
            [(cell, cell.persistence) for cell in paired if cell.persistence is not None], config
        )
        assert paired_model is not None and paired_persistence is not None
        comparison = PairedComparison(
            count=len(paired),
            model=paired_model,
            persistence=paired_persistence,
            mae_improvement_fraction=(
                None
                if paired_persistence.mae <= 0.0
                else 1.0 - paired_model.mae / paired_persistence.mae
            ),
            sign_test=_sign_test(paired),
        )
    normalized: NormalizedError | None = None
    if residual_scale is not None and scored:
        ratios = []
        for cell in scored:
            assert cell.model is not None and cell.observed is not None
            ratios.append(abs(cell.model - cell.observed) / residual_scale)
        ratio_summary = summarize(ratios, quantiles=config.quantiles)
        assert ratio_summary is not None
        normalized = NormalizedError(
            residual_scale=residual_scale,
            abs_error_quantiles=tuple(
                QuantileValue(quantile=item.quantile, value=item.value)
                for item in ratio_summary.percentiles
            ),
            abs_error_max=ratio_summary.maximum,
        )
    return OutputResult(
        horizon_ms=key.horizon_ms,
        metric=key.metric,
        excluded=dict(sorted(excluded.items())),
        model=model_summary,
        train_mean=_mean_summary(scored, train_mean),
        paired=comparison,
        normalized=normalized,
    )


def _group_outputs(
    results: Sequence[_ExampleResult],
    outputs: tuple[OutputKey, ...],
    *,
    config: ThermalEvaluationConfig,
    train_means: Mapping[OutputKey, float | None],
    residual_scales: Mapping[OutputKey, float],
) -> tuple[OutputResult, ...]:
    return tuple(
        _output_result(
            key,
            [result.cells[key] for result in results],
            config=config,
            train_mean=train_means.get(key),
            residual_scale=residual_scales.get(key),
        )
        for key in outputs
    )


def _blocks(results: Sequence[_ExampleResult]) -> int:
    return len({result.block for result in results})


def train_means(
    train: Sequence[DatasetExampleV2], outputs: tuple[OutputKey, ...]
) -> dict[OutputKey, float | None]:
    """出力ごとの ``train`` の target の平均（``train`` だけから。0172 §2.3 の ``train_mean``）。"""
    means: dict[OutputKey, float | None] = {}
    for key in outputs:
        values = [
            value
            for example in train
            for target in example.targets
            if target.horizon_ms == key.horizon_ms and key.metric in target.values
            for value in (target.values[key.metric],)
            if value is not None
            and not target.missing_mask[key.metric]
            and target.quality[key.metric] is Quality.OK
        ]
        means[key] = math.fsum(values) / len(values) if values else None
    return means


def _action_effect(
    results: Sequence[_ExampleResult], outputs: tuple[OutputKey, ...]
) -> ActionEffect:
    """``action_changed`` の example で、実際の action 列と ``hold_effective`` の予測を比べる。"""
    changed = [result for result in results if result.changed]
    rows: list[ActionEffectOutput] = []
    for key in outputs:
        recorded: list[float] = []
        held: list[float] = []
        for result in changed:
            cell = result.cells[key]
            if cell.excluded is not None or cell.held is None:
                continue
            assert cell.observed is not None and cell.model is not None
            recorded.append(abs(cell.model - cell.observed))
            held.append(abs(cell.held - cell.observed))
        rows.append(
            ActionEffectOutput(
                horizon_ms=key.horizon_ms,
                metric=key.metric,
                count=len(recorded),
                recorded_mae=math.fsum(recorded) / len(recorded) if recorded else None,
                held_mae=math.fsum(held) / len(held) if held else None,
            )
        )
    return ActionEffect(
        changed_examples=len(changed),
        held_prediction_unavailable=sum(1 for result in changed if not result.held_available),
        outputs=tuple(rows),
    )


def evaluate_test_split(
    test: Sequence[DatasetExampleV2],
    *,
    train: Sequence[DatasetExampleV2],
    predictor: Predictor,
    action_schema: ThermalActionSchema,
    outputs: tuple[OutputKey, ...],
    residual_scales: Sequence[ResidualScale],
    config: ThermalEvaluationConfig,
    block_origin_ms: int,
    support: SupportCheck | None,
) -> EvaluationSection:
    """test の example を予測し、指標と層をまとめる（0172 §2.3 / §2.4）。I/O も時計も持たない。"""
    ordered = sorted(test, key=lambda example: (example.action_ts_ms, example.example_id))
    excluded: Counter[str] = Counter()
    results: list[_ExampleResult] = []
    for example in ordered:
        result = _evaluate_example(
            example,
            predictor=predictor,
            action_schema=action_schema,
            outputs=outputs,
            config=config,
            block_origin_ms=block_origin_ms,
            support=support,
        )
        if isinstance(result, str):
            excluded[result] += 1
        else:
            results.append(result)
    means = train_means(train, outputs)
    scales = {
        OutputKey(horizon_ms=scale.horizon_ms, metric=scale.metric): scale.scale
        for scale in residual_scales
    }
    overall = GroupResult(
        examples=len(results),
        blocks=_blocks(results),
        outputs=_group_outputs(
            results, outputs, config=config, train_means=means, residual_scales=scales
        ),
    )
    axes: list[StratumAxis] = [*GATING_AXES, AXIS_WORKLOAD_REGIME]
    if support is not None:
        axes.append(AXIS_ACTION_SUPPORT)
    strata: list[StratumResult] = []
    for axis in axes:
        names = sorted({result.strata[axis] for result in results})
        for name in names:
            members = [result for result in results if result.strata[axis] == name]
            blocks = _blocks(members)
            strata.append(
                StratumResult(
                    axis=axis,
                    name=name,
                    gating=axis in GATING_AXES,
                    status=stratum_status(len(members), blocks, config.minimum),
                    examples=len(members),
                    blocks=blocks,
                    outputs=_group_outputs(
                        members, outputs, config=config, train_means=means, residual_scales=scales
                    ),
                )
            )
    return EvaluationSection(
        block_origin_ms=block_origin_ms,
        test_examples=len(ordered),
        predicted_examples=len(results),
        excluded_examples=dict(sorted(excluded.items())),
        overall=overall,
        strata=tuple(strata),
        action_effect=_action_effect(results, outputs),
    )


# ---------------------------------------------------------------- 入力の照合（0172 §2.2）


def parse_artifact(value: str) -> ArtifactRef:
    """``<model_id>@<version>`` を thermal_model の ref にする。"""
    model_id, separator, version = value.partition("@")
    if not separator:
        raise ValueError("--artifact は <model_id>@<version> の形にする")
    return ArtifactRef(kind=ArtifactKind.THERMAL_MODEL, model_id=model_id, version=version)


RegistryStatus = Literal["candidate", "validated", "production", "retired"]
_STATUS: dict[ArtifactStatus, RegistryStatus] = {
    ArtifactStatus.CANDIDATE: "candidate",
    ArtifactStatus.VALIDATED: "validated",
    ArtifactStatus.PRODUCTION: "production",
    ArtifactStatus.RETIRED: "retired",
}


@dataclass(frozen=True, slots=True)
class LoadedModel:
    """Registry から読んだ封をした型と、記録の状態。"""

    model: RegistryCounterfactualThermalModel
    status: RegistryStatus


def load_registered_model(
    ref: ArtifactRef, registry: ModelRegistry, inputs: EvaluationInputs
) -> LoadedModel:
    """Registry に登録された bytes を読み、L1〜L12 を通す（0172 §2.2。Registry に書かない）。"""
    snapshot = registry.inspect()
    record = snapshot.artifacts.get(ref.key)
    if record is None:
        raise ValueError(f"artifact が Registry に登録されていない: {ref.key}")
    metadata = record.metadata
    if metadata.capability is not ArtifactCapability.COUNTERFACTUAL_ACTION:
        raise ValueError(
            "反実仮想（counterfactual_action）の thermal model ではない: "
            f"{metadata.capability.value}"
        )
    compatibility = ModelCompatibility(
        feature_schema_version=FEATURE_SCHEMA_V2_VERSION,
        target_schema_version=TARGET_SCHEMA_V2_VERSION,
        authority_stage=AuthorityStage.SHADOW,
    )
    result = registry.load_version(ref, compatibility)
    if result.status is not ArtifactLoadStatus.LOADED or result.artifact is None:
        raise ValueError(f"load_version が読めない: {result.status.value}: {result.detail}")
    if result.artifact.metadata.sha256 != metadata.sha256:
        raise ValueError("読んだ artifact の checksum が Registry の記録と一致しない")
    model = RegistryCounterfactualThermalModel.from_verified_artifact(
        result.artifact,
        metric_catalog=inputs.metric_catalog,
        calibration=RuntimeCalibration.available(inputs.calibration.offsets_c),
    )
    return LoadedModel(model=model, status=_STATUS[record.status])


def _time_window(examples: Sequence[DatasetExampleV2]) -> TimeWindow | None:
    if not examples:
        return None
    return TimeWindow(
        start_ms=min(example.history_start_ms for example in examples),
        end_ms=max(example.label_end_ms for example in examples),
    )


def _split_window(examples: Sequence[DatasetExampleV2]) -> SplitWindow:
    window = _time_window(examples)
    return SplitWindow(
        count=len(examples),
        start_ms=None if window is None else window.start_ms,
        end_ms=None if window is None else window.end_ms,
    )


def _config_outputs_match(config: ThermalEvaluationConfig, metrics: Iterable[str]) -> None:
    target = sorted(metrics)
    configured = sorted(config.output_metrics())
    if configured != target:
        raise ValueError(
            f"--evaluation-config の outputs が target schema と一致しない"
            f"（設定: {configured}・target: {target}）"
        )


def build_report(args: argparse.Namespace, inputs: EvaluationInputs) -> bytes:
    """0172 §2.2〜§2.7。1つでも外れれば例外（``--out`` を作らない）。"""
    ref = parse_artifact(args.artifact)
    registry = ModelRegistry(args.registry_root, limits=inputs.registry_limits)
    loaded = load_registered_model(ref, registry, inputs)
    model = loaded.model
    training_data = model.manifest.training_data
    _config_outputs_match(inputs.config, model.target_schema.metrics)

    source = load_training_dataset_artifact_v2(args.dataset)
    if (
        source.artifact_id != training_data.dataset_alias
        or source.manifest_sha256 != training_data.manifest_sha256
        or source.examples_sha256 != training_data.examples_sha256
    ):
        raise ValueError(
            "--dataset が artifact を学習した dataset ではない（alias・digest の不一致）"
        )
    split = split_temporally_v2(
        source.dataset.examples,
        validation_start_ms=args.validation_start_ms,
        test_start_ms=args.test_start_ms,
    )
    digest = split_sha256(split)
    if digest != training_data.split_sha256:
        raise ValueError(
            "split の digest が artifact の学習の split と一致しない（境界の取り違え）"
        )
    if _time_window(split.test) != training_data.window.test:
        raise ValueError("test の期間が artifact の学習の split と一致しない")
    if not split.test:
        raise ValueError("test が空（空の評価を合格と読ませない）")

    outputs = tuple(
        OutputKey(horizon_ms=horizon, metric=metric)
        for horizon in model.target_schema.horizons_ms
        for metric in model.target_schema.metrics
    )
    checker = StepSupportChecker(model.confidence_profile, model.action_schema)

    def predictor(
        observed: ObservedThermalInput, trajectory: ActionTrajectory
    ) -> tuple[PredictedTarget, ...]:
        return model.predict_trajectory(observed, trajectory).targets

    def support(observed: ObservedThermalInput, trajectory: ActionTrajectory) -> bool:
        anchor = PerZone(
            front=observed.action.front.effective_demand,
            rear=observed.action.rear.effective_demand,
            top=observed.action.top.effective_demand,
        )
        return checker.check(anchor, trajectory) is None

    evaluation = evaluate_test_split(
        split.test,
        train=split.train,
        predictor=predictor,
        action_schema=model.action_schema,
        outputs=outputs,
        residual_scales=model.confidence_profile.residual_scales,
        config=inputs.config,
        block_origin_ms=args.test_start_ms,
        support=support,
    )
    return assemble_report(
        artifact=ArtifactSection(
            model_id=model.manifest.model_id,
            version=model.manifest.model_version,
            sha256=model.artifact_sha256,
            confidence_profile_sha256=model.manifest.confidence_profile_sha256,
            registry_status=loaded.status,
        ),
        dataset=DatasetSection(
            alias=source.artifact_id,
            manifest_sha256=source.manifest_sha256,
            examples_sha256=source.examples_sha256,
        ),
        split=SplitSection(
            validation_start_ms=args.validation_start_ms,
            test_start_ms=args.test_start_ms,
            split_sha256=digest,
            train=_split_window(split.train),
            validation=_split_window(split.validation),
            test=_split_window(split.test),
            purged=len(split.purged),
        ),
        inputs=InputsSection(
            config_sha256=inputs.config_sha256,
            metrics_config_sha256=inputs.metrics_sha256,
            calibration_sha256=inputs.calibration_sha256,
        ),
        outputs=outputs,
        evaluation=evaluation,
        config=inputs.config,
    )


def assemble_report(
    *,
    artifact: ArtifactSection,
    dataset: DatasetSection,
    split: SplitSection,
    inputs: InputsSection,
    outputs: tuple[OutputKey, ...],
    evaluation: EvaluationSection,
    config: ThermalEvaluationConfig,
) -> bytes:
    """gate と ``conditions_sha256`` を足して canonical bytes にする（0172 §2.6 / §2.7）。"""
    placeholder = GateResult(outcome="pass", failed_stage=None, reasons=())
    draft = ThermalEvaluationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        artifact=artifact,
        dataset=dataset,
        split=split,
        inputs=inputs,
        code=CodeSection(
            report_schema_version=REPORT_SCHEMA_VERSION, procedure_version=PROCEDURE_VERSION
        ),
        outputs=outputs,
        evaluation=evaluation,
        gate=placeholder,
        conditions_sha256="0" * 64,
    )
    # gate は報告の数字と設定だけから作る（gate の欄・digest の欄は読まない）
    gate = evaluate_gate(draft, config)
    report = draft.model_copy(update={"gate": gate, "conditions_sha256": conditions_sha256(draft)})
    report = ThermalEvaluationReport.model_validate(report.model_dump(mode="python"))
    return canonical_report_bytes(report)


# ---------------------------------------------------------------- 入口


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise EvaluationStopped(
            "--out に '..' は使えない", stage="untrusted_path", fields={"out": str(path)}
        )
    absolute = path if path.is_absolute() else Path.cwd() / path
    return Path(os.path.normpath(absolute))


def _refuse_existing_out(parent_fd: int, out: Path) -> None:
    try:
        os.stat(out.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise EvaluationStopped(
        f"--out が既にある（上書きしない）: {out}",
        stage="before_write",
        fields={"out": str(out), "out_exists": True},
    )


def write_report(parent_fd: int, name: str, payload: bytes) -> None:
    """``O_CREAT | O_EXCL`` で書いて fsync する。途中で失敗したら書きかけを消す。"""
    fd = os.open(name, _WRITE_FLAGS, 0o644, dir_fd=parent_fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(name, dir_fd=parent_fd)
        raise
    os.close(fd)
    os.fsync(parent_fd)


def _log_stopped(stop: EvaluationStopped) -> None:
    LOGGER.error(
        str(stop),
        extra={logs.FIELDS_KEY: {"stage": stop.stage, "stages": [stop.stage], **stop.fields}},
    )


def run(args: argparse.Namespace) -> int:
    """1回の評価。報告を書けたら 0、失敗で 1（段は構造化ログ）。"""
    out = _absolute(args.out)
    try:
        inputs = read_inputs(args)
    except _EVALUATION_ERRORS as error:
        raise EvaluationStopped(
            f"設定を読めない: {error}", stage="inputs", fields={"error": str(error)}
        ) from error
    try:
        check_trusted_parent(out)
        parent_fd = open_output_root(out.parent, create=False)
    except UntrustedPathError as error:
        raise EvaluationStopped(str(error), stage=error.stage, fields=error.fields()) from error
    except _EVALUATION_ERRORS as error:
        raise EvaluationStopped(
            f"--out の親を開けない: {error}",
            stage="before_write",
            fields={"out": str(out), "error": str(error)},
        ) from error
    try:
        _refuse_existing_out(parent_fd, out)
        try:
            payload = build_report(args, inputs)
        except _EVALUATION_ERRORS as error:
            raise EvaluationStopped(
                f"評価の前の検査か評価に失敗した: {error}",
                stage="evaluate",
                fields={"error": str(error), "error_type": type(error).__name__},
            ) from error
        try:
            write_report(parent_fd, out.name, payload)
        except FileExistsError as error:
            raise EvaluationStopped(
                str(error), stage="before_write", fields={"out": str(out), "out_exists": True}
            ) from error
        except OSError as error:
            raise EvaluationStopped(
                f"--out を書けない: {error}",
                stage="write",
                fields={"out": str(out), "error": str(error)},
            ) from error
    finally:
        os.close(parent_fd)
    report = ThermalEvaluationReport.model_validate_json(payload)
    LOGGER.info(
        "評価の報告を書いた（Registry は書いていない。gate は助言）",
        extra={
            logs.FIELDS_KEY: {
                "out": str(out),
                "artifact": f"{report.artifact.model_id}@{report.artifact.version}",
                "registry_status": report.artifact.registry_status,
                "outcome": report.gate.outcome,
                "failed_stage": report.gate.failed_stage,
                "reasons": list(report.gate.reasons),
                "report_sha256": _sha256(payload),
                "offline_evaluation_ref": offline_evaluation_ref(payload),
            }
        },
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """引数（0172 §2.2。すべて必須・既定値を置かない）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-thermal-evaluate",
        description=(
            "反実仮想 Thermal Model を学習した dataset の test で評価する"
            "（読み取りのみ。#84 / 0172）"
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--registry-root", type=Path, required=True, help="Model Registry の root")
    parser.add_argument(
        "--registry-limits",
        type=Path,
        required=True,
        help="model-registry.yaml のあるディレクトリ",
    )
    parser.add_argument("--artifact", required=True, help="<model_id>@<version>")
    parser.add_argument("--dataset", type=Path, required=True, help="artifact を学習した dataset")
    parser.add_argument("--validation-start-ms", type=int, required=True)
    parser.add_argument("--test-start-ms", type=int, required=True)
    parser.add_argument("--evaluation-config", type=Path, required=True, help="評価の設定")
    parser.add_argument("--metrics-config", type=Path, required=True, help="config/metrics.yaml")
    parser.add_argument(
        "--calibration", type=Path, required=True, help="学習期間に効いていた calibration.json"
    )
    parser.add_argument("--out", type=Path, required=True, help="報告の JSON（まだ無いこと）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """1回の評価を実行し、終了コードを返す。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    try:
        return run(args)
    except EvaluationStopped as stop:
        _log_stopped(stop)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
