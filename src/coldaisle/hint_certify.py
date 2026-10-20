"""認証ランナー ``coldaisle-hint-certify``（#288 / 決定記録 0152 §2.1 の段 2・0157・0160 §2.4）。

Workload Hint の単調性ゲート（0066 §2.1 (b)）を、本番のホストで人の手で走らせる。格子の生成・
二重解法・全 offset の比較・合否・証明書の生成をすべてここで行う（``tests/`` やリリーススクリプトは
これを呼ぶだけ。0066 §2.1 (c)）。合格したときだけ証明書の JSON を ``--out`` へ書く。

**書くのは ``--out`` の1ファイルだけ。** Model Registry にも設定にも書かない（登録は人が
``coldaisle-registry`` で ``register`` → ``validate`` → ``promote`` する。0152 §2.2）。
Fan Demand・Reactive Guard・Critical Safety には何も届かない（ルール 2・3）。この module は
AI 層・API・eventd・管理ソケットから import しない（0152 §2.1）。

起動の形（0157 §2.1 / §2.3）::

    /opt/coldaisle/.venv/bin/python -I -m coldaisle.hint_certify --config-dir ... \\
        --registry-root ... --hint-lockfile ... --out ...

起動の形が ``isolated_module`` でない・``SCHED_IDLE`` に下げられないときは何もせずに終了する。
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import logging
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from coldaisle import logs
from coldaisle.clock import SystemMonotonicClock
from coldaisle.control.config import ControlConfig, SupervisorPolicyContext
from coldaisle.control.hint_gate import (
    HintGateSpecError,
    calibration_sha256,
    derive_hint_gate_spec,
    hint_policy_sha256,
    optimizer_sha256,
)
from coldaisle.control.model.counterfactual import (
    FEATURE_SCHEMA_V2_VERSION,
    TARGET_SCHEMA_V2_VERSION,
    ThermalFeatureSchemaV2,
)
from coldaisle.control.model.thermal import (
    ObservedFanAction,
    ObservedThermalInput,
    ObservedWindowFrame,
)
from coldaisle.control.model_registry import (
    ArtifactKind,
    ModelCompatibility,
    ModelRegistry,
    load_model_registry_limits,
)
from coldaisle.control.mpc.controller import (
    LearnedMpcController,
    LearnedMpcRuntime,
    MpcCostDependencies,
)
from coldaisle.control.mpc.optimizer import MpcSolution
from coldaisle.control.schema import (
    AuthorityStage,
    Demand,
    OptimizerStatus,
    PerZone,
    StaticAuthorityStage,
    WorkloadRegime,
)
from coldaisle.hint_certificate import (
    CertifiedThermalModel,
    HintCertificate,
    HintCertificateBody,
    HintCertificateComponents,
    HintGateSpec,
    HintGateZoneDemands,
)
from coldaisle.hint_certificate_digest import (
    LaunchForm,
    conditions_sha256,
    current_launch_form,
    environment_sha256,
    imported_package_dir,
    lockfile_sha256,
    source_tree_sha256,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.startup_calibration import read_startup_calibration

LOGGER = logging.getLogger("coldaisle.hint_certify")

ENTRY_MODULE = "coldaisle.hint_certify"
"""入口の module（0157 §2.3 の 2）。設定から読まない（入口の識別であって閾値ではない）。"""
PROG = "coldaisle-hint-certify"

EXIT_CERTIFIED = 0
"""合格し、証明書を ``--out`` へ書いた。"""
EXIT_NOT_CERTIFIED = 1
"""不合格（証明書を書かない）。理由は構造化ログの ``reason``。"""
EXIT_USAGE = 2
"""引数の誤り（argparse と同じ値）。"""
EXIT_REFUSED = 3
"""起動の形が ``isolated_module`` でない・``SCHED_IDLE`` に下げられない（何もしていない）。"""
EXIT_INPUT = 4
"""入力（Control Config・Metric Catalog・Registry の上限・ロックファイル・実行環境）を読めない。"""
EXIT_OUTPUT = 5
"""``--out`` が既にある・書けない（証明書は書いていない）。"""

AUTHORITY_STAGE = AuthorityStage.SHADOW
"""thermal_model を束縛する authority stage（0160 §2.4 の 1。最も低い制御権に固定する）。"""

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_METRICS = Path("config/metrics.yaml")
DEFAULT_REGISTRY_LIMITS = Path("config")
"""fand（``coldaisle-fand``）・worker（``coldaisle-learnd``）と同じ既定。"""

_OUT_MODE = 0o644
"""証明書のファイルの mode。中身は秘匿ではなく、登録する人（``coldaisle-registry``）が読む。"""


class NotCertifiedError(Exception):
    """不合格。``reason`` は閉じた語彙で、メッセージ・ログに設定の値と path を載せない。"""

    def __init__(self, reason: str, *, fields: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fields = dict(fields or {})


class InputUnreadableError(Exception):
    """入力を読めない（``EXIT_INPUT``）。``reason`` は閉じた語彙。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CertifyInputs:
    """ランナーの入力（すべて path。証明書の値を外から渡す経路は無い。0152 §2.8）。"""

    config_dir: Path
    metrics: Path
    registry_root: Path
    registry_limits: Path
    calibration: Path | None
    lockfile: Path
    out: Path


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    """CLI。**閾値・格子・証明書の値は受け取らない**（設定と Registry が持つ）。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Workload Hint の単調性ゲートの認証ランナー（決定記録 0152 §2.1）。"
            "合格したときだけ証明書を --out に書く。Registry にも設定にも書かない"
        ),
    )
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument(
        "--registry-root",
        type=Path,
        required=True,
        help="Model Registry の root（thermal_model の production を読むだけ。書かない）",
    )
    parser.add_argument(
        "--registry-limits",
        type=Path,
        default=DEFAULT_REGISTRY_LIMITS,
        help="model-registry.yaml のあるディレクトリ",
    )
    parser.add_argument(
        "--calibration",
        type=Path,
        default=None,
        help=(
            "fand が起動時に読むのと同じ較正ファイル（0152 §2.1）。既定は無い。省くと "
            "unavailable として扱い、較正の掛かる artifact は L9 で束縛できず不合格になる"
        ),
    )
    parser.add_argument(
        "--hint-lockfile",
        type=Path,
        required=True,
        help="ロックファイル（root の所有の uv.lock。部品 lockfile。0152 §5 の 4）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="証明書の JSON を書く path（既にあれば書かない。上書きしない）",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def lower_to_sched_idle() -> bool:
    """自分のスケジューリングを ``SCHED_IDLE`` に下げ、``sched_getscheduler`` で確かめる。

    0152 §2.1 / §5 の 5。本番のホストで fand・Learned worker と CPU を取り合わないため。
    下げられない・確かめられない（Linux 以外を含む）なら False。
    """
    try:
        policy = os.SCHED_IDLE
        os.sched_setscheduler(0, policy, os.sched_param(0))
        return os.sched_getscheduler(0) == policy
    except (AttributeError, OSError):
        return False


def main(
    argv: Sequence[str] | None = None,
    *,
    launch_form: Callable[[str], LaunchForm] = current_launch_form,
    lower_priority: Callable[[], bool] = lower_to_sched_idle,
) -> int:
    """``coldaisle-hint-certify`` の入口。

    ``launch_form`` / ``lower_priority`` は前提の確かめ方だけを差し替える（試験で、pytest の
    プロセスを ``SCHED_IDLE`` に下げないため）。証明書の値には効かない。
    """
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    form = launch_form(ENTRY_MODULE)
    if form is not LaunchForm.ISOLATED_MODULE:
        # 0157 §2.1: console script などで走らせても、本番と合わない証明書を作らない
        LOGGER.error(
            "起動の形が -I -m でないため何もせずに終了する",
            extra={
                logs.FIELDS_KEY: {"reason": "launch_form_not_isolated", "launch_form": form.value}
            },
        )
        return EXIT_REFUSED
    if not lower_priority():
        LOGGER.error(
            "SCHED_IDLE に下げられないため何もせずに終了する",
            extra={logs.FIELDS_KEY: {"reason": "sched_idle_unavailable"}},
        )
        return EXIT_REFUSED
    return certify(
        CertifyInputs(
            config_dir=args.config_dir,
            metrics=args.metrics,
            registry_root=args.registry_root,
            registry_limits=args.registry_limits,
            calibration=args.calibration,
            lockfile=args.hint_lockfile,
            out=args.out,
        )
    )


def certify(inputs: CertifyInputs) -> int:
    """格子を回し、合格なら証明書を書く。終了コードを返す（例外で落とさない）。

    起動の形と ``SCHED_IDLE`` は :func:`main` が確かめてから呼ぶ。
    """
    try:
        certificate = _run(inputs)
    except InputUnreadableError as error:
        LOGGER.error(
            "入力を読めないため証明書を作らない",
            extra={logs.FIELDS_KEY: {"reason": error.reason, "error": error.detail[:500]}},
        )
        return EXIT_INPUT
    except NotCertifiedError as error:
        LOGGER.error(
            "単調性ゲートに合格しなかったため証明書を作らない",
            extra={logs.FIELDS_KEY: {"reason": error.reason, **error.fields}},
        )
        return EXIT_NOT_CERTIFIED
    except _OutputError as error:
        LOGGER.error(
            "証明書を書けない",
            extra={logs.FIELDS_KEY: {"reason": error.reason, "error": error.detail[:500]}},
        )
        return EXIT_OUTPUT
    LOGGER.info(
        "単調性ゲートに合格し、証明書を書いた（Registry への登録は人が行う）",
        extra={
            logs.FIELDS_KEY: {
                "reason": "certified",
                "certificate_sha256": certificate.certificate_sha256,
                "grid_points": certificate.grid_points,
                "thermal_model": certificate.thermal_model.model_dump(mode="json"),
            }
        },
    )
    return EXIT_CERTIFIED


# ---------------------------------------------------------------- 本体


def _run(inputs: CertifyInputs) -> HintCertificate:
    if os.path.lexists(inputs.out):
        # 格子を回す前に止める（回してから書けないと分かるのを避ける）。書く時も O_EXCL で確かめる
        raise _OutputError("out_exists", "--out が既にある（上書きしない）")
    control = _read(
        "control_config_invalid", lambda: ControlConfig.from_directory(inputs.config_dir)
    )
    catalog = _read("metric_catalog_unreadable", lambda: MetricCatalog.from_yaml(inputs.metrics))
    policy = control.policy
    try:
        spec = derive_hint_gate_spec(policy.supervisor.workload_hint, control.safety.workload_hint)
    except HintGateSpecError as error:
        raise NotCertifiedError(error.reason.value) from None
    LOGGER.info(
        "ゲートの仕様を導いた（格子の点の数）",
        extra={logs.FIELDS_KEY: {"grid_points": spec.grid_points, "hint_gate": spec.sha256()}},
    )
    runtime_calibration, frame_calibration = read_startup_calibration(inputs.calibration)
    limits = _read(
        "registry_limits_unreadable", lambda: load_model_registry_limits(inputs.registry_limits)
    )
    registry = ModelRegistry(inputs.registry_root, limits=limits)
    # **読むだけ。** load_production は root も lock も作らない（create=False）
    loaded = _read(
        "registry_unreadable",
        lambda: registry.load_production(
            ArtifactKind.THERMAL_MODEL,
            ModelCompatibility(
                feature_schema_version=FEATURE_SCHEMA_V2_VERSION,
                target_schema_version=TARGET_SCHEMA_V2_VERSION,
                authority_stage=AUTHORITY_STAGE,
            ),
        ),
    )
    if loaded.artifact is None:
        raise NotCertifiedError(
            "thermal_model_unavailable", fields={"registry_status": loaded.status.value}
        )
    verified = loaded.artifact
    clock = SystemMonotonicClock()
    dependencies = MpcCostDependencies.from_control(control)
    runtime = LearnedMpcRuntime.load(
        verified,
        policy,
        control.safety,
        metric_catalog=catalog,
        # L9 を含む L1〜L12 を worker と同じ loader で通す（0152 §2.1。L9 を飛ばす経路を作らない）
        calibration=runtime_calibration,
        expected_model_version=verified.attestation.version,
        monotonic_ms=clock.monotonic_ms,
        authority=StaticAuthorityStage(AUTHORITY_STAGE),
        air_balance=dependencies.air_balance,
        balance_band=dependencies.balance_band,
        fan_hardware=dependencies.fan_hardware,
    )
    controller = runtime.controller
    if controller is None:
        assert runtime.failure_reason is not None
        raise NotCertifiedError(
            "thermal_model_unbindable", fields={"code": runtime.failure_reason.code}
        )
    schema = controller.binding.model.feature_schema
    _check_window_metrics(schema, control, spec)
    try:
        conditions = conditions_sha256(controller.conditions())
    except ValueError:
        raise NotCertifiedError("conditions_unexpected_shape") from None
    attestation = verified.attestation
    components = HintCertificateComponents(
        source_tree=_read(
            "source_tree_unreadable", lambda: source_tree_sha256(imported_package_dir())
        ),
        lockfile=_read("lockfile_unreadable", lambda: lockfile_sha256(inputs.lockfile)),
        environment=_read("environment_unreadable", environment_sha256),
        hint_policy=hint_policy_sha256(policy.supervisor),
        hint_gate=spec.sha256(),
        optimizer=optimizer_sha256(policy, control.safety),
        conditions=conditions,
        calibration=calibration_sha256(frame_calibration),
    )
    result = _run_grid(controller, control, spec, schema, clock.monotonic_ms)
    result.raise_if_failed(spec)
    body = HintCertificateBody.from_spec(
        thermal_model=CertifiedThermalModel(
            kind="thermal_model",
            model_id=attestation.model_id,
            version=attestation.version,
            artifact_sha256=attestation.artifact_sha256,
        ),
        components=components,
        spec=spec,
    )
    certificate = HintCertificate.issue(body)
    _write_exclusive(inputs.out, certificate.to_json_bytes())
    return certificate


def _read[T](reason: str, read: Callable[[], T]) -> T:
    try:
        return read()
    except Exception as error:
        raise InputUnreadableError(reason, f"{type(error).__name__}: {error}") from error


def _check_window_metrics(
    schema: ThermalFeatureSchemaV2, control: ControlConfig, spec: HintGateSpec
) -> None:
    """artifact の window の metric と格子の軸が1対1に対応することを確かめる（0156 §2.4）。

    - 目的関数の温度の metric（CPU / GPU）が window に無い → 不合格（0160 §2.4 の 4）
    - window の metric に軸が無い・``window_features`` に artifact が使わない metric がある・
      温度の metric を ``window_features`` にも書いた → 不合格（何かの値で埋めない・書き損じを
      見逃さない）
    """
    cost = control.policy.mpc.optimizer.cost_metrics
    temperatures = (cost.cpu_temperature, cost.gpu_temperature)
    window = set(schema.metrics)
    if not set(temperatures) <= window:
        raise NotCertifiedError("temperature_metric_not_in_window")
    features = [feature.metric for feature in spec.certified_domain.window_features]
    if set(features) & set(temperatures):
        raise NotCertifiedError("window_feature_duplicates_temperature")
    if window != set(temperatures) | set(features):
        raise NotCertifiedError(
            "window_metrics_mismatch",
            fields={
                "missing_axes": len(window - set(temperatures) - set(features)),
                "unused_features": len(set(features) - window),
            },
        )


# ---------------------------------------------------------------- 格子


@dataclass(frozen=True, slots=True)
class _WindowAxes:
    """平らな window を決める軸の**位置**（値は載せない）。"""

    hint_key: int
    previous_demand: tuple[int, int, int]
    temperature_cpu: int
    temperature_gpu: int
    window_features: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _PointAxes:
    """格子の点の各軸の**位置**（値は載せない。0152 §2.1 の不合格のログ）。"""

    window: _WindowAxes
    safety_floor: int
    baseline: int

    def as_fields(self) -> dict[str, object]:
        return {
            "hint_key": self.window.hint_key,
            "previous_demand": list(self.window.previous_demand),
            "temperature_cpu": self.window.temperature_cpu,
            "temperature_gpu": self.window.temperature_gpu,
            "window_features": list(self.window.window_features),
            "safety_floor": self.safety_floor,
            "baseline": self.baseline,
        }


@dataclass(slots=True)
class _GridResult:
    """格子を最後まで回した結果（点を黙って除かない。0156 §2.5 / 0160 §2.4 の 2）。"""

    tried: int = 0
    unsolved: int = 0
    violated: int = 0
    first_unsolved: tuple[_PointAxes, str, str] | None = None
    first_violated: tuple[_PointAxes, str] | None = None
    unsolved_reasons: set[str] = field(default_factory=set)

    def raise_if_failed(self, spec: HintGateSpec) -> None:
        if self.unsolved:
            assert self.first_unsolved is not None
            axes, condition, code = self.first_unsolved
            raise NotCertifiedError(
                "grid_point_unsolved",
                fields={
                    "unsolved_points": self.unsolved,
                    "violated_points": self.violated,
                    "first_point": axes.as_fields(),
                    "first_condition": condition,
                    "first_code": code,
                    "codes": sorted(self.unsolved_reasons),
                },
            )
        if self.violated:
            assert self.first_violated is not None
            axes, check = self.first_violated
            raise NotCertifiedError(
                "monotonicity_violated",
                fields={
                    "violated_points": self.violated,
                    "first_point": axes.as_fields(),
                    "first_check": check,
                },
            )
        if self.tried != spec.grid_points:
            # 仕様の点の数と回した点の数が食い違えば、試していない点を試したと記録しうる
            raise NotCertifiedError("grid_points_mismatch")


def _run_grid(
    controller: LearnedMpcController,
    control: ControlConfig,
    spec: HintGateSpec,
    schema: ThermalFeatureSchemaV2,
    monotonic_ms: Callable[[], int],
) -> _GridResult:
    supervisor = control.policy.supervisor
    contexts = supervisor.rule_policy.contexts
    overrides = {
        (item.regime.value, item.workload.value): item.context
        for item in supervisor.workload_hint.context_overrides or ()
    }
    domain = spec.certified_domain
    keys = [(key.regime, key.workload) for key in domain.hint_keys]
    if sorted(overrides) != keys:
        # ゲートの仕様は同じ設定から導いたので起きない。起きたら導出が壊れている
        raise NotCertifiedError("hint_keys_mismatch")
    result = _GridResult()
    for key_index, key in enumerate(keys):
        base = contexts.get(WorkloadRegime(key[0]))
        hinted = contexts.get(overrides[key])
        for axes, observed in _windows(key_index, base, control, spec, schema):
            for floor_index, floor in enumerate(domain.safety_floors):
                for baseline_index, baseline in enumerate(domain.baselines):
                    point = _PointAxes(
                        window=axes, safety_floor=floor_index, baseline=baseline_index
                    )
                    result.tried += 1
                    _check_point(
                        result,
                        point,
                        controller=controller,
                        control=control,
                        spec=spec,
                        observed=observed,
                        floor=_demands(floor),
                        baseline=_demands(baseline),
                        base=base,
                        hinted=hinted,
                        monotonic_ms=monotonic_ms,
                    )
    return result


def _windows(
    key_index: int,
    base: SupervisorPolicyContext,
    control: ControlConfig,
    spec: HintGateSpec,
    schema: ThermalFeatureSchemaV2,
) -> Iterator[tuple[_WindowAxes, ObservedThermalInput]]:
    """(直前 Demand の直積) × (CPU 温度) × (GPU 温度) × (各 window feature) の平らな window。"""
    domain = spec.certified_domain
    cost = control.policy.mpc.optimizer.cost_metrics
    previous = domain.previous_demand.points
    cpu_upper = base.target_band.cpu_temperature.upper_c
    gpu_upper = base.target_band.gpu_temperature.upper_c
    features = domain.window_features
    for previous_index in itertools.product(range(len(previous)), repeat=3):
        action = PerZone[ObservedFanAction](
            front=ObservedFanAction(effective_demand=previous[previous_index[0]]),
            rear=ObservedFanAction(effective_demand=previous[previous_index[1]]),
            top=ObservedFanAction(effective_demand=previous[previous_index[2]]),
        )
        for cpu_index, cpu_offset in enumerate(domain.temperature_cpu.points):
            for gpu_index, gpu_offset in enumerate(domain.temperature_gpu.points):
                for feature_index in itertools.product(
                    *(range(len(feature.points)) for feature in features)
                ):
                    values = {
                        # 温度は基準の context の upper_c からの差（0156 §2.4）
                        cost.cpu_temperature: cpu_upper + cpu_offset,
                        cost.gpu_temperature: gpu_upper + gpu_offset,
                    }
                    for feature, index in zip(features, feature_index, strict=True):
                        values[feature.metric] = feature.points[index]
                    axes = _WindowAxes(
                        hint_key=key_index,
                        previous_demand=(previous_index[0], previous_index[1], previous_index[2]),
                        temperature_cpu=cpu_index,
                        temperature_gpu=gpu_index,
                        window_features=tuple(feature_index),
                    )
                    yield axes, flat_window(schema, values, action)


def flat_window(
    schema: ThermalFeatureSchemaV2,
    values: dict[str, float],
    action: PerZone[ObservedFanAction],
) -> ObservedThermalInput:
    """全 frame が同じ値の window（0156 §2.4 / 0160 §2.4 の 5）。

    ``action_ts_ms`` は artifact の ``window_ms``、格子の時刻は 0 から ``sample_period_ms`` 刻み
    （worker の ``build_observed_input`` と同じ並び）。各 cell の ``source_ts_ms`` はその格子の
    時刻で、mask はどれも立てない（missing / stale / suspect の無い観測）。
    """
    if set(values) != set(schema.metrics):
        raise ValueError("平らな window の metric は artifact の window の metric と同じにする")
    action_ts_ms = schema.window_ms
    metrics = schema.metrics
    frames = tuple(
        ObservedWindowFrame(
            ts_ms=ts_ms,
            values={metric: values[metric] for metric in metrics},
            source_ts_ms=dict.fromkeys(metrics, ts_ms),
            missing_mask=dict.fromkeys(metrics, False),
            stale_mask=dict.fromkeys(metrics, False),
            suspect_mask=dict.fromkeys(metrics, False),
        )
        for ts_ms in range(0, action_ts_ms + 1, schema.sample_period_ms)
    )
    return ObservedThermalInput(action_ts_ms=action_ts_ms, window=frames, action=action)


def _demands(row: HintGateZoneDemands) -> PerZone[Demand]:
    return PerZone[Demand](front=row.front, rear=row.rear, top=row.top)


def _check_point(
    result: _GridResult,
    point: _PointAxes,
    *,
    controller: LearnedMpcController,
    control: ControlConfig,
    spec: HintGateSpec,
    observed: ObservedThermalInput,
    floor: PerZone[Demand],
    baseline: PerZone[Demand],
    base: SupervisorPolicyContext,
    hinted: SupervisorPolicyContext,
    monotonic_ms: Callable[[], int],
) -> None:
    solutions: list[MpcSolution] = []
    for condition, context in (("base", base), ("hint", hinted)):
        solved = _solve(controller, control, observed, floor, baseline, context, monotonic_ms)
        if isinstance(solved, str):
            result.unsolved += 1
            result.unsolved_reasons.add(solved)
            if result.first_unsolved is None:
                result.first_unsolved = (point, condition, solved)
            return
        solutions.append(solved)
    violation = _violation(solutions[0], solutions[1], control, spec)
    if violation is not None:
        result.violated += 1
        if result.first_violated is None:
            result.first_violated = (point, violation)


def _solve(
    controller: LearnedMpcController,
    control: ControlConfig,
    observed: ObservedThermalInput,
    floor: PerZone[Demand],
    baseline: PerZone[Demand],
    context: SupervisorPolicyContext,
    monotonic_ms: Callable[[], int],
) -> MpcSolution | str:
    """1回の solve。解けなければ理由の code（0160 §2.4 の 2・3）。

    予算は設定の ``mpc.budget_ms`` と ``mpc.optimizer.max_evaluations`` を丸ごと渡し、実時計で
    測る（0160 §2.4 の 3）。
    """
    mpc = control.policy.mpc
    try:
        outcome = controller.solve(
            observed=observed,
            safety_floor=floor,
            weights=context.weights,
            target_band=context.target_band,
            baseline=baseline,
            max_evaluations=mpc.optimizer.max_evaluations.value,
            deadline_mono_ms=monotonic_ms() + mpc.budget_ms,
        )
    except Exception as error:
        # 制約の組み立ての実行不能（InfeasiblePlanError）・anchor 推論の失敗など。点を除かない
        return _code(type(error).__name__)
    if outcome.status is not OptimizerStatus.OK or outcome.solution is None:
        return outcome.reason.code
    return outcome.solution


def _violation(
    base: MpcSolution, hinted: MpcSolution, control: ControlConfig, spec: HintGateSpec
) -> str | None:
    """0066 §2.1 (b) の 2 条件。破れた条件の名前（値は返さない）か、満たせば None。

    1. 各系統で ``demand_hint >= demand_base - eps``
    2. 両 plan の予測の**対応するすべての offset** で、CPU / GPU を要素ごとに
       ``hint <= base + eps_c``
    """
    for zone in ("front", "rear", "top"):
        if getattr(hinted.requested, zone) < getattr(base.requested, zone) - spec.eps:
            return f"demand_{zone}"
    base_targets = base.prediction.targets
    hint_targets = hinted.prediction.targets
    if [target.offset_ms for target in base_targets] != [
        target.offset_ms for target in hint_targets
    ]:
        return "prediction_offsets_differ"
    cost = control.policy.mpc.optimizer.cost_metrics
    for base_target, hint_target in zip(base_targets, hint_targets, strict=True):
        for name, metric in (("cpu", cost.cpu_temperature), ("gpu", cost.gpu_temperature)):
            base_value = base_target.values.get(metric)
            hint_value = hint_target.values.get(metric)
            if base_value is None or hint_value is None:
                return f"prediction_{name}_missing"
            if hint_value > base_value + spec.eps_c:
                return f"temperature_{name}"
    return None


def _code(name: str) -> str:
    """例外名を理由の code の形（小文字と下線）にする。"""
    lowered = "".join(f"_{char.lower()}" if char.isupper() else char for char in name)
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in lowered)
    return cleaned.strip("_")[:64] or "solve_failed"


# ---------------------------------------------------------------- 出力


class _OutputError(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _write_exclusive(out: Path, data: bytes) -> None:
    """``out`` が無いときだけ、完全な中身で現れるように書く（上書きしない）。

    同じディレクトリの一時ファイルへ ``O_EXCL | O_NOFOLLOW`` で書いて ``fsync`` し、``link`` で
    ``out`` に置く（``out`` が既にあれば失敗する）。途中で失敗したら一時ファイルを消し、``out`` は
    作らない。
    """
    directory = out.parent if str(out.parent) else Path(".")
    temporary = directory / f".{out.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(temporary, flags, _OUT_MODE)
    except OSError as error:
        raise _OutputError("out_unwritable", f"{type(error).__name__}: {error}") from error
    try:
        try:
            os.fchmod(descriptor, _OUT_MODE)
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(temporary, out, follow_symlinks=False)
    except FileExistsError as error:
        raise _OutputError("out_exists", "--out が既にある（上書きしない）") from error
    except OSError as error:
        raise _OutputError("out_unwritable", f"{type(error).__name__}: {error}") from error
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    try:
        _fsync_directory(directory)
    except OSError as error:
        # 置いたことを永続にできなければ、証明書を残さない（人が登録してから消えうる）
        out.unlink(missing_ok=True)
        raise _OutputError("out_not_durable", f"{type(error).__name__}: {error}") from error


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# 本番は `python -I -m coldaisle.hint_certify` で起動する（決定記録 0157 §2.1）
if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
