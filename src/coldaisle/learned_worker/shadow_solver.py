"""Workload Hint の B1 の Shadow solver の子プロセス（#288 / 決定記録 0133 §2.5・0152 §2.6・0157
§2.3・0160 §2.4 の 6・0162 §2.2 / §2.5 の PR-B）。

基準のプロセス（``coldaisle-learnd --role mpc``）が起動する**子プロセス1つ**で、ヒント有りの
context の solve を基準の周期から切り離して解く。基準の solve と送信は子を待たない（0133 §2.5）。

- **子は起動の直後に** ``PR_SET_PDEATHSIG`` を設定し（親が死んだら子も終わる）、
  自分のスケジューリングを ``SCHED_IDLE`` に下げて ``sched_getscheduler`` で確かめる。
  確かめるまで artifact の読み込みも solve も始めない。下げられない・
  確かめられないときは何もせずに終了し、親はそのプロセスの間は作り直さない
  （``shadow_unavailable``。``nice`` で代えない）
- 子は fand のソケットを持たない。親が固定した artifact の識別を受け取り、自分で Registry
  から読んで digest が合うときだけ解く（合わなければ ``artifact_mismatch``）
- 子は自分の部品（``source_tree`` / ``lockfile`` / ``environment`` は起動時に1回、``hint_gate`` /
  ``optimizer`` は設定から1回、``conditions`` / ``calibration`` は束縛ごとに1回）
  を同じ関数で計算し、frame の ``hint_certification`` の期待値とすべて一致するときだけ解く（0152
  §2.6）。``environment`` は親が ``isolated_module`` で、子自身も ``-I``（``isolated`` と
  ``safe_path``）のときだけ計算する（0157 §2.3）
- artifact の ``window_ms`` が frame の ``domain_lookback_ms`` 以下、``stale_after_ms`` が
  ``domain_stale_after_ms`` 以上であることを確かめる（0162 §2.2）
- worker 自身の認証領域の判定は、**実際の window の全 frame** と artifact の ``window_ms`` /
  ``stale_after_ms`` で、fand と同じ関数（``control/hint_domain.py``）を呼ぶ（0156 §2.4 / 0162
  §2.2）
- 二重解法の残り: 基準の solve が使った評価の回数を引いた残りと、共通の打ち切り時刻
  ``min(基準の solve の開始 + mpc.budget_ms, 次の基準の周期の開始)``（``CLOCK_MONOTONIC``）を
  ``LearnedMpcController.solve`` へ渡す（予算を増やさない。0160 §2.4 の 6 / 0066 §2.1 (d)）
- 2 条件は認証ランナー・fand の再検査と同じ関数（``control/hint_monotonicity.py``）で比べる

**親子の経路**: Unix の ``SOCK_STREAM`` の socketpair。どちらの向きも pydantic で検証する JSON
（親 → 子は依頼、子 → 親は結果。**どちらも unpickle しない**）。
親は送受信ともノンブロッキングで、基準の solve の外（frame を待つ間の受信の多重化の中）
でだけ読む。

**Demand・PWM・設定・較正を変えない。** 子が出せるのは記録に写す値（両 plan の ``requested``
と予測）だけで、受け渡し口にも Gate にも入らない（0162 §2.1・ルール 2）。Reactive Guard /
Critical Safety / Fallback / hardware を import しない（0133 §2.7）。
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import multiprocessing
import os
import signal
import socket
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from coldaisle import logs
from coldaisle.clock import SystemMonotonicClock
from coldaisle.control.config import ControlConfig
from coldaisle.control.hint_domain import DomainFrame, judge_certified_domain
from coldaisle.control.hint_gate import (
    HintGateSpecError,
    calibration_sha256,
    derive_hint_gate_spec,
    optimizer_sha256,
)
from coldaisle.control.hint_monotonicity import monotonicity_violation
from coldaisle.control.learned_handoff import (
    AvailableCalibration,
    LearnedHintCertification,
    PinnedArtifact,
    UnavailableCalibration,
    calibration_to_runtime,
)
from coldaisle.control.model.thermal import ObservedThermalInput
from coldaisle.control.model_registry import ModelRegistry, load_model_registry_limits
from coldaisle.control.mpc.controller import LearnedMpcRuntime, MpcCostDependencies
from coldaisle.control.mpc.counterfactual import PlannedTarget
from coldaisle.control.schema import (
    AuthorityStage,
    ControlConfigDigest,
    Demand,
    OptimizerStatus,
    PerZone,
    StaticAuthorityStage,
    SupervisorOutput,
)
from coldaisle.hint_certificate import HintGateSpec, Sha256
from coldaisle.hint_certificate_digest import (
    conditions_sha256,
    environment_sha256,
    imported_package_dir,
    lockfile_sha256,
    source_tree_sha256,
)
from coldaisle.learned_channel.messages import (
    HintShadowPlan,
    HintShadowPlans,
    HintShadowSkipReason,
    HintShadowStatus,
)
from coldaisle.learned_worker.mpc import ArtifactSource, RegistryArtifactSource
from coldaisle.metrics import MetricCatalog

LOGGER = logging.getLogger("coldaisle.learned_worker.shadow")

WORKER_ENTRY_MODULE = "coldaisle.learned_worker.cli"
"""worker の入口の module（0157 §2.3 の 2。入口の識別であって閾値ではないのでコードに持つ）。"""

_PR_SET_PDEATHSIG = 1
"""``prctl(2)`` の option の番号（Linux の ABI。``<linux/prctl.h>``）。"""
_FRAME_HEADER = struct.Struct("!I")
"""親子の経路の1メッセージの長さ（4 byte の big endian）。"""

FrameCalibration = AvailableCalibration | UnavailableCalibration


# ================================================================ 親子の経路の型


class ShadowRequest(BaseModel):
    """親から子への1枠の依頼（0133 §2.5）。親が自分の frame の列と基準の解から作る。

    経路では JSON で運ぶ（``PerZone[...]`` の型は pickle できないうえ、親子のどちらの向きも
    同じ検証を通す形にそろえる）。

    ``observed`` は基準の solve に使ったのと**同じ**観測 window。``domain_frames`` は基準の
    window を作った frame の列（時刻順・最後が ``N``）から判定に要る軸の値だけを取り出したもの。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(ge=0)
    observed: ObservedThermalInput
    domain_frames: tuple[DomainFrame, ...]
    previous_applied: PerZone[Demand | None] | None
    """``N - 1`` の frame の ``applied``（tick が続いていなければ None。0107 §2.2）。"""
    safety_floor: PerZone[Demand]
    baseline: PerZone[Demand] | None
    base_supervisor: SupervisorOutput
    hint_supervisor: SupervisorOutput
    base_requested: PerZone[Demand]
    base_targets: tuple[PlannedTarget, ...]
    base_anchor_inference_id: str
    remaining_evaluations: int
    deadline_mono_ms: int
    certification: LearnedHintCertification
    calibration: FrameCalibration
    thermal_model: PinnedArtifact
    authority_stage: AuthorityStage
    config: ControlConfigDigest
    """元の frame の ``config``（親は自分の設定と一致した frame でだけ基準を解く）。"""
    metric_catalog_sha256: Sha256
    """元の frame の Metric Catalog の SHA-256。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ShadowOutcome(_Strict):
    """子から親への1件の結果（0133 §2.5）。親はこれと依頼の識別から Shadow の記録を作る。"""

    kind: Literal["result"] = "result"
    seq: int = Field(ge=0)
    status: HintShadowStatus
    reason: HintShadowSkipReason | None
    plans: HintShadowPlans | None
    conditions_sha256: Sha256 | None


class ShadowSetupCode(StrEnum):
    """子が何もせずに終了した理由（親はそのプロセスの間、子を作り直さない）。"""

    PDEATHSIG_UNAVAILABLE = "pdeathsig_unavailable"
    PARENT_GONE = "parent_gone"
    SCHED_IDLE_UNAVAILABLE = "sched_idle_unavailable"


class ShadowReady(_Strict):
    """子が ``SCHED_IDLE`` を確かめ、起動時の部品を計算し終えた知らせ。"""

    kind: Literal["ready"] = "ready"


class ShadowSetupFailed(_Strict):
    """子が前提を満たせず、何もせずに終了する知らせ。"""

    kind: Literal["setup_failed"] = "setup_failed"
    code: ShadowSetupCode


ShadowReply = Annotated[
    ShadowOutcome | ShadowReady | ShadowSetupFailed, Field(discriminator="kind")
]
_REPLY = TypeAdapter[ShadowOutcome | ShadowReady | ShadowSetupFailed](ShadowReply)


# ================================================================ 子の判断（I/O を持たない）


@dataclass(frozen=True, slots=True)
class WorkerStaticComponents:
    """子が起動時に1回だけ計算する部品（0152 §2.6）。

    計算できなかった部品は None（照合で不一致になる）。
    """

    source_tree: str | None
    lockfile: str | None
    environment: str | None


def compute_static_components(
    *, lockfile: Path | None, parent_isolated: bool
) -> WorkerStaticComponents:
    """``source_tree`` / ``lockfile`` / ``environment`` を同じ関数で計算する（0152 §2.6）。

    計算できない部品は理由をログに残して None にする（照合で ``certification_mismatch`` に倒れる。
    0152 §2.4 の「渡されない・読めないときは不一致」と同じ向き）。``environment`` は、親が
    ``isolated_module`` で、子自身も ``-I`` で動くときだけ計算する（``other`` は計算しない。0157
    §2.3）。
    """
    return WorkerStaticComponents(
        source_tree=_component("source_tree", lambda: source_tree_sha256(imported_package_dir())),
        lockfile=None
        if lockfile is None
        else _component("lockfile", lambda: lockfile_sha256(lockfile)),
        environment=(
            _component("environment", environment_sha256)
            if parent_isolated and sys.flags.isolated and sys.flags.safe_path
            else None
        ),
    )


def _component(name: str, compute: Callable[[], str]) -> str | None:
    try:
        return compute()
    except Exception as error:
        LOGGER.warning(
            "Shadow solver の部品を計算できない; 照合は不一致になる",
            extra={logs.FIELDS_KEY: {"component": name, "error": type(error).__name__}},
        )
        return None


@dataclass(slots=True)
class _Binding:
    key: tuple[PinnedArtifact, FrameCalibration, AuthorityStage]
    runtime: LearnedMpcRuntime | None
    conditions_sha256: str | None


class ShadowSolverCore:
    """子の1件の判断（I/O を持たない。試験は同じプロセスで呼ぶ）。"""

    def __init__(
        self,
        *,
        control: ControlConfig,
        catalog: MetricCatalog,
        catalog_sha256: str,
        artifacts: ArtifactSource,
        components: WorkerStaticComponents,
        monotonic_ms: Callable[[], int],
    ) -> None:
        self._control = control
        self._digest = control.runtime_digest()
        self._catalog = catalog
        self._catalog_sha256 = catalog_sha256
        self._artifacts = artifacts
        self._components = components
        self._monotonic_ms = monotonic_ms
        policy = control.policy
        try:
            spec: HintGateSpec | None = derive_hint_gate_spec(
                policy.supervisor.workload_hint, control.safety.workload_hint
            )
        except HintGateSpecError as error:
            LOGGER.warning(
                "ゲートの仕様を導けない; 照合は不一致になる",
                extra={logs.FIELDS_KEY: {"reason": error.reason.value}},
            )
            spec = None
        self._spec = spec
        self._optimizer_sha256 = optimizer_sha256(policy, control.safety)
        self._binding: _Binding | None = None

    def handle(self, request: ShadowRequest) -> ShadowOutcome:
        """1件の依頼を判定して解く。

        **例外は翻訳しない**（solve の失敗だけは「解けなかった」として扱う。0133 §2.8）。
        """
        if self._monotonic_ms() >= request.deadline_mono_ms:
            return _skipped(request, HintShadowSkipReason.BUDGET_EXHAUSTED, None)
        binding = self._bind(request)
        runtime = binding.runtime
        if runtime is None or runtime.controller is None:
            return _skipped(request, HintShadowSkipReason.ARTIFACT_MISMATCH, None)
        conditions = binding.conditions_sha256
        controller = runtime.controller
        schema = controller.binding.model.feature_schema
        certification = request.certification
        spec = self._spec
        if (
            spec is None
            or request.config != self._digest
            or request.metric_catalog_sha256 != self._catalog_sha256
            or not self._components_match(request, conditions, spec)
            or schema.window_ms > certification.domain_lookback_ms
            or schema.stale_after_ms < certification.domain_stale_after_ms
        ):
            return _skipped(request, HintShadowSkipReason.CERTIFICATION_MISMATCH, conditions)
        base_band = request.base_supervisor.target_band
        judgement = judge_certified_domain(
            request.domain_frames,
            key=self._hint_key(request.hint_supervisor, spec),
            previous_demand=request.previous_applied,
            safety_floor=request.safety_floor,
            baseline=request.baseline,
            cpu_upper_c=base_band.cpu_temperature.upper_c,
            gpu_upper_c=base_band.gpu_temperature.upper_c,
            spec=spec,
            # worker は実際の window と artifact の値で判定する（0156 §2.4 / 0162 §2.2）
            lookback_ms=schema.window_ms,
            stale_after_ms=schema.stale_after_ms,
        )
        if not judgement.passed:
            return _skipped(request, HintShadowSkipReason.OUT_OF_CERTIFIED_DOMAIN, conditions)
        baseline = request.baseline
        assert baseline is not None  # 領域の判定が Baseline の欠けを外にしている
        if self._monotonic_ms() >= request.deadline_mono_ms:
            return _skipped(request, HintShadowSkipReason.BUDGET_EXHAUSTED, conditions)
        hint = request.hint_supervisor
        try:
            outcome = controller.solve(
                observed=request.observed,
                safety_floor=request.safety_floor,
                weights=hint.weights,
                target_band=hint.target_band,
                baseline=baseline,
                max_evaluations=request.remaining_evaluations,
                deadline_mono_ms=request.deadline_mono_ms,
            )
        except Exception as error:
            # 0133 §2.8: 「ヒント有りが解けない」は budget_exhausted。基準は既に送ってある
            LOGGER.warning(
                "ヒント有りの solve が解けなかった",
                extra={logs.FIELDS_KEY: {"seq": request.seq, "error": type(error).__name__}},
            )
            return _skipped(request, HintShadowSkipReason.BUDGET_EXHAUSTED, conditions)
        solution = outcome.solution
        if outcome.status is not OptimizerStatus.OK or solution is None:
            return _skipped(request, HintShadowSkipReason.BUDGET_EXHAUSTED, conditions)
        if solution.anchor_inference_id != request.base_anchor_inference_id:
            # 同じ window・同じ model で解いていない解を、基準と比べない
            return _skipped(request, HintShadowSkipReason.BASE_MISMATCH, conditions)
        cost = self._control.policy.mpc.optimizer.cost_metrics
        violated = monotonicity_violation(
            base_requested=request.base_requested,
            base_targets=request.base_targets,
            hint_requested=solution.requested,
            hint_targets=solution.prediction.targets,
            cpu_temperature_metric=cost.cpu_temperature,
            gpu_temperature_metric=cost.gpu_temperature,
            eps=spec.eps,
            eps_c=spec.eps_c,
        )
        if violated is not None:
            return _skipped(request, HintShadowSkipReason.MONOTONICITY_VIOLATED, conditions)
        assert conditions is not None  # 照合に通った
        return ShadowOutcome(
            seq=request.seq,
            status=HintShadowStatus.RECORDED,
            reason=None,
            plans=HintShadowPlans(
                base=HintShadowPlan(requested=request.base_requested, targets=request.base_targets),
                hint=HintShadowPlan(
                    requested=solution.requested, targets=solution.prediction.targets
                ),
                hint_target_band=hint.target_band,
            ),
            conditions_sha256=conditions,
        )

    # ------------------------------------------------------------------ 内部

    def _bind(self, request: ShadowRequest) -> _Binding:
        """依頼の artifact・較正・stage で束縛する（同じ組なら作り直さない）。"""
        key = (request.thermal_model, request.calibration, request.authority_stage)
        if self._binding is not None and self._binding.key == key:
            return self._binding
        self._binding = _Binding(key=key, runtime=None, conditions_sha256=None)
        loaded = self._artifacts.load(request.thermal_model, request.authority_stage)
        if isinstance(loaded, str):
            LOGGER.warning(
                "Shadow solver が固定された artifact を読めない",
                extra={logs.FIELDS_KEY: {"reason": "artifact_mismatch", "detail": loaded[:500]}},
            )
            return self._binding
        control = self._control
        dependencies = MpcCostDependencies.from_control(control)
        runtime = LearnedMpcRuntime.load(
            loaded,
            control.policy,
            control.safety,
            metric_catalog=self._catalog,
            # 親と同じく frame の較正だけを渡す（較正ファイルを読まない。0101 §2.1）
            calibration=calibration_to_runtime(request.calibration),
            expected_model_version=request.thermal_model.version,
            monotonic_ms=self._monotonic_ms,
            authority=StaticAuthorityStage(request.authority_stage),
            air_balance=dependencies.air_balance,
            balance_band=dependencies.balance_band,
            fan_hardware=dependencies.fan_hardware,
        )
        if runtime.controller is None:
            failure = runtime.failure_reason
            LOGGER.warning(
                "Shadow solver が固定された artifact で束縛できない",
                extra={
                    logs.FIELDS_KEY: {
                        "reason": "artifact_mismatch",
                        "code": None if failure is None else failure.code,
                    }
                },
            )
            return self._binding
        self._binding.runtime = runtime
        try:
            self._binding.conditions_sha256 = conditions_sha256(runtime.controller.conditions())
        except ValueError:
            self._binding.conditions_sha256 = None
        return self._binding

    def _components_match(
        self, request: ShadowRequest, conditions: str | None, spec: HintGateSpec
    ) -> bool:
        expected = request.certification.components
        own = self._components
        pairs = (
            (own.source_tree, expected.source_tree),
            (own.lockfile, expected.lockfile),
            (own.environment, expected.environment),
            (spec.sha256(), expected.hint_gate),
            (self._optimizer_sha256, expected.optimizer),
            (conditions, expected.conditions),
            (calibration_sha256(request.calibration), expected.calibration),
        )
        return all(mine is not None and mine == theirs for mine, theirs in pairs)

    def _hint_key(self, hint: SupervisorOutput, spec: HintGateSpec) -> tuple[str, str] | None:
        """ヒント有りの出力から (Regime, workload) の鍵を導く（frame は workload を運ばない）。

        ``context_overrides`` のうち、Regime が同じで、上書き先の context の ``target_band``
        がヒント有りの band と等しい行の鍵。無ければ None（判定 1 で外）。どの鍵もゲートの仕様の
        ``hint_keys`` にある（仕様は同じ ``context_overrides`` から導く）ので、判定 1
        は「ヒント有りの band が、この Regime の上書きのどれかから来たか」を確かめることになる。
        """
        supervisor = self._control.policy.supervisor
        contexts = supervisor.rule_policy.contexts
        keys = {(item.regime, item.workload) for item in spec.certified_domain.hint_keys}
        found = sorted(
            (item.regime.value, item.workload.value)
            for item in supervisor.workload_hint.context_overrides or ()
            if item.regime is hint.regime
            and contexts.get(item.context).target_band == hint.target_band
            and (item.regime.value, item.workload.value) in keys
        )
        return found[0] if found else None


def _skipped(
    request: ShadowRequest, reason: HintShadowSkipReason, conditions: str | None
) -> ShadowOutcome:
    return ShadowOutcome(
        seq=request.seq,
        status=HintShadowStatus.SKIPPED,
        reason=reason,
        plans=None,
        conditions_sha256=conditions,
    )


# ================================================================ 子プロセスの入口


def lower_to_sched_idle() -> bool:
    """自分のスケジューリングを ``SCHED_IDLE`` に下げ、``sched_getscheduler`` で確かめる。

    決定記録 0133 §2.5。

    Linux では特権なしで下げられる。下げられない・確かめられない（Linux 以外を含む）なら False。
    """
    try:
        policy = os.SCHED_IDLE
        os.sched_setscheduler(0, policy, os.sched_param(0))
        return os.sched_getscheduler(0) == policy
    except (AttributeError, OSError):
        return False


def set_parent_death_signal() -> bool:
    """親（このプロセスを作ったスレッド）が終わったら ``SIGKILL`` を受け取るようにする。"""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return int(libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)) == 0
    except (OSError, AttributeError):
        return False


@dataclass(frozen=True, slots=True)
class ShadowChildArgs:
    """子へ渡す値（spawn で pickle される）。path・個体識別子をログに出さない（ルール 10）。

    Control Config と Metric Catalog は親と同じ path から子が自分で読む（設定の型は pickle
    できない）。親が読んだ後に書き換わっていても、子は依頼の frame の ``config`` /
    ``metric_catalog_sha256`` と照らして ``certification_mismatch`` に倒れる。
    """

    config_dir: Path
    metrics: Path
    registry_root: Path
    registry_limits: Path
    lockfile: Path | None
    parent_isolated: bool
    parent_pid: int
    log_level: str
    lower_priority: Callable[[], bool] = lower_to_sched_idle
    parent_death_signal: Callable[[], bool] = set_parent_death_signal


def run_child(sock: socket.socket, args: ShadowChildArgs) -> None:
    """子プロセスの入口（``multiprocessing`` の spawn の target）。"""
    logs.configure(args.log_level)
    sock.setblocking(True)
    if not args.parent_death_signal():
        _send_reply(sock, ShadowSetupFailed(code=ShadowSetupCode.PDEATHSIG_UNAVAILABLE))
        return
    if os.getppid() != args.parent_pid:
        _send_reply(sock, ShadowSetupFailed(code=ShadowSetupCode.PARENT_GONE))
        return
    if not args.lower_priority():
        # 確かめるまで artifact の読み込みも solve も始めない（0133 §2.5）
        _send_reply(sock, ShadowSetupFailed(code=ShadowSetupCode.SCHED_IDLE_UNAVAILABLE))
        return
    catalog, catalog_sha256 = MetricCatalog.from_yaml_with_sha256(args.metrics)
    core = ShadowSolverCore(
        control=ControlConfig.from_directory(args.config_dir),
        catalog=catalog,
        catalog_sha256=catalog_sha256,
        artifacts=RegistryArtifactSource(
            ModelRegistry(
                args.registry_root, limits=load_model_registry_limits(args.registry_limits)
            )
        ),
        components=compute_static_components(
            lockfile=args.lockfile, parent_isolated=args.parent_isolated
        ),
        monotonic_ms=SystemMonotonicClock().monotonic_ms,
    )
    _send_reply(sock, ShadowReady())
    try:
        while True:
            payload = _recv_frame(sock)
            if payload is None:
                return
            _send_reply(sock, core.handle(ShadowRequest.model_validate_json(payload)))
    except Exception:
        # 握りつぶさずに終了する。親が経路の切断として扱い、間隔をあけて作り直す（0133 §2.5）
        LOGGER.exception("Shadow solver の子が依頼の処理で失敗したため終了する")
        raise


def _send_reply(
    sock: socket.socket, reply: ShadowOutcome | ShadowReady | ShadowSetupFailed
) -> None:
    data = reply.model_dump_json().encode("utf-8")
    sock.sendall(_FRAME_HEADER.pack(len(data)) + data)


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            return None
        chunks += chunk
    return bytes(chunks)


def _recv_frame(sock: socket.socket) -> bytes | None:
    header = _recv_exact(sock, _FRAME_HEADER.size)
    if header is None:
        return None
    (size,) = _FRAME_HEADER.unpack(header)
    return _recv_exact(sock, size)


# ================================================================ 親の側の持ち手


class SolverState(StrEnum):
    """子の状態（親の側から見たもの）。"""

    NOT_STARTED = "not_started"
    """まだ作っていない（最初のヒントの依頼で :meth:`ShadowSolverPort.start` が作る）。"""
    STARTING = "starting"
    """起動した・``ready`` を待っている（依頼を渡せない）。"""
    IDLE = "idle"
    """依頼を渡せる。"""
    BUSY = "busy"
    """依頼の途中（新しい依頼は ``shadow_busy``）。"""
    WAITING_RESPAWN = "waiting_respawn"
    """死んだ・応答しない・経路が壊れた。``respawn_interval_ms`` の後に作り直す。"""
    DISABLED = "disabled"
    """前提（``SCHED_IDLE`` など）を満たせない。このプロセスの間は作り直さない。"""


@dataclass(frozen=True, slots=True)
class SolverEvents:
    """1回の ``service`` で起きたこと。"""

    results: tuple[ShadowOutcome, ...] = ()
    lost: tuple[int, ...] = ()
    """結果を返さずに失われた依頼の ``seq``（子の死・応答しない・経路の破損）。"""


class ShadowSolverPort(Protocol):
    """親の周期のループが使う Shadow solver の口（試験は偽物に替える）。"""

    @property
    def state(self) -> SolverState:
        """いまの状態。"""

    def start(self, now_ms: int) -> None:
        """``NOT_STARTED`` なら次の ``service`` で子を作る（それ以外の状態では何もしない）。"""

    def submit(self, request: ShadowRequest) -> None:
        """``IDLE`` のときだけ呼ぶ。**待たない**（書けない分は ``service`` で書く）。"""

    def service(self, now_ms: int) -> SolverEvents:
        """届いた結果を読み、死活・作り直しを進める。**待たない。**"""

    def readable_fds(self) -> list[int]:
        """読み待ちに加える fd。"""

    def writable_fds(self) -> list[int]:
        """書き待ちに加える fd（書き残しがあるときだけ）。"""

    def close(self) -> None:
        """子を終わらせる。"""


@dataclass(slots=True)
class _Child:
    process: multiprocessing.process.BaseProcess
    sock: socket.socket
    inbox: bytearray = field(default_factory=bytearray)
    outbox: bytearray = field(default_factory=bytearray)


class ShadowSolverProcess:
    """子プロセス1つの持ち手（0133 §2.5）。親のスレッド1本（周期のループ）からだけ呼ぶ。"""

    def __init__(
        self,
        args: ShadowChildArgs,
        *,
        respawn_interval_ms: int,
        unresponsive_after_ms: int,
        max_reply_bytes: int,
        monotonic_ms: Callable[[], int],
    ) -> None:
        self._args = args
        self._respawn_interval_ms = respawn_interval_ms
        self._unresponsive_after_ms = unresponsive_after_ms
        self._max_reply_bytes = max_reply_bytes
        self._monotonic_ms = monotonic_ms
        self._context = multiprocessing.get_context("spawn")
        self._child: _Child | None = None
        self._state = SolverState.NOT_STARTED
        self._respawn_at_ms = 0
        self._inflight: tuple[int, int] | None = None
        """依頼の途中の (``seq``, 打ち切り時刻)。"""
        self._reaping: list[multiprocessing.process.BaseProcess] = []

    @property
    def state(self) -> SolverState:
        """いまの状態。"""
        return self._state

    def start(self, now_ms: int) -> None:
        """最初のヒントの依頼で子を作る（B1 を使わない間は子を作らない）。"""
        if self._state is SolverState.NOT_STARTED:
            self._state = SolverState.WAITING_RESPAWN
            self._respawn_at_ms = now_ms

    def submit(self, request: ShadowRequest) -> None:
        """依頼を1枠で渡す（``IDLE`` のときだけ）。書けない分は ``service`` で書く。"""
        if self._state is not SolverState.IDLE or self._child is None:
            raise RuntimeError("Shadow solver が依頼を受けられる状態でない")
        payload = request.model_dump_json().encode("utf-8")
        self._child.outbox += _FRAME_HEADER.pack(len(payload)) + payload
        self._inflight = (request.seq, request.deadline_mono_ms)
        self._state = SolverState.BUSY
        self._flush(self._child)

    def service(self, now_ms: int) -> SolverEvents:
        """届いた結果を読み、死活・作り直しを進める。**待たない。**"""
        self._reap()
        child = self._child
        if child is None:
            if self._state is SolverState.WAITING_RESPAWN and now_ms >= self._respawn_at_ms:
                self._spawn(now_ms)
            return SolverEvents()
        results: list[ShadowOutcome] = []
        broken = not self._flush(child) or not self._read(child, results)
        if not broken and self._state is SolverState.DISABLED:
            return SolverEvents(results=tuple(results))
        if not broken and not child.process.is_alive():
            broken = True
        if (
            not broken
            and self._state is SolverState.BUSY
            and self._inflight is not None
            and now_ms >= self._inflight[1] + self._unresponsive_after_ms
        ):
            LOGGER.warning(
                "Shadow solver の子が応答しないため終わらせる",
                extra={logs.FIELDS_KEY: {"reason": "shadow_unavailable", "seq": self._inflight[0]}},
            )
            broken = True
        if not broken:
            return SolverEvents(results=tuple(results))
        lost = () if self._inflight is None else (self._inflight[0],)
        self._discard_child(now_ms)
        return SolverEvents(results=tuple(results), lost=lost)

    def readable_fds(self) -> list[int]:
        """子の結果の fd。"""
        return [] if self._child is None else [self._child.sock.fileno()]

    def writable_fds(self) -> list[int]:
        """書き残しがあるときだけ子の fd。"""
        if self._child is None or not self._child.outbox:
            return []
        return [self._child.sock.fileno()]

    def close(self) -> None:
        """子を終わらせる（worker の終了時）。"""
        child, self._child = self._child, None
        self._state = SolverState.DISABLED
        if child is not None:
            self._kill(child)
        for process in self._reaping:
            process.join(timeout=1.0)
        self._reaping.clear()

    # ------------------------------------------------------------------ 内部

    def _spawn(self, now_ms: int) -> None:
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            process = self._context.Process(
                target=run_child,
                args=(child_sock, self._args),
                name="coldaisle-shadow-solver",
                daemon=True,
            )
            process.start()
        except Exception:
            parent_sock.close()
            LOGGER.exception(
                "Shadow solver の子を作れない; 間隔をあけて作り直す",
                extra={logs.FIELDS_KEY: {"reason": "shadow_unavailable"}},
            )
            self._respawn_at_ms = now_ms + self._respawn_interval_ms
            return
        finally:
            child_sock.close()
        parent_sock.setblocking(False)
        self._child = _Child(process=process, sock=parent_sock)
        self._state = SolverState.STARTING
        LOGGER.info(
            "Shadow solver の子を起動した",
            extra={logs.FIELDS_KEY: {"pid": process.pid}},
        )

    def _flush(self, child: _Child) -> bool:
        """書き残しを書けるだけ書く。経路が壊れていれば False。"""
        while child.outbox:
            try:
                sent = child.sock.send(child.outbox)
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                return False
            del child.outbox[:sent]
        return True

    def _read(self, child: _Child, results: list[ShadowOutcome]) -> bool:
        """届いているだけ読む。EOF・壊れた結果なら False。"""
        while True:
            try:
                chunk = child.sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                return False
            if not chunk:
                self._take_frames(child, results)
                return False
            child.inbox += chunk
        return self._take_frames(child, results)

    def _take_frames(self, child: _Child, results: list[ShadowOutcome]) -> bool:
        while len(child.inbox) >= _FRAME_HEADER.size:
            (size,) = _FRAME_HEADER.unpack_from(child.inbox)
            if size > self._max_reply_bytes:
                LOGGER.warning(
                    "Shadow solver の子の結果が上限を超えた; 子を終わらせる",
                    extra={logs.FIELDS_KEY: {"reason": "shadow_unavailable", "bytes": size}},
                )
                return False
            if len(child.inbox) < _FRAME_HEADER.size + size:
                break
            payload = bytes(child.inbox[_FRAME_HEADER.size : _FRAME_HEADER.size + size])
            del child.inbox[: _FRAME_HEADER.size + size]
            try:
                reply = _REPLY.validate_json(payload)
            except ValidationError:
                LOGGER.warning(
                    "Shadow solver の子の結果が壊れている; 子を終わらせる",
                    extra={logs.FIELDS_KEY: {"reason": "shadow_unavailable"}},
                )
                return False
            if not self._accept(reply, results):
                return False
        return True

    def _accept(
        self, reply: ShadowOutcome | ShadowReady | ShadowSetupFailed, results: list[ShadowOutcome]
    ) -> bool:
        if isinstance(reply, ShadowReady):
            if self._state is not SolverState.STARTING:
                return False
            self._state = SolverState.IDLE
            LOGGER.info("Shadow solver の子が依頼を受けられる")
            return True
        if isinstance(reply, ShadowSetupFailed):
            # 1つだけの扱い: そのプロセスの間は作り直さない（0133 §2.5。nice で代えない）
            LOGGER.error(
                "Shadow solver の子が前提を満たせない; このプロセスの間は作り直さない",
                extra={logs.FIELDS_KEY: {"reason": "shadow_unavailable", "code": reply.code.value}},
            )
            self._state = SolverState.DISABLED
            child, self._child = self._child, None
            if child is not None:
                self._kill(child)
            return True
        if self._state is not SolverState.BUSY or self._inflight is None:
            return False
        if reply.seq != self._inflight[0]:
            return False
        self._inflight = None
        self._state = SolverState.IDLE
        results.append(reply)
        return True

    def _discard_child(self, now_ms: int) -> None:
        child, self._child = self._child, None
        self._inflight = None
        if child is not None:
            self._kill(child)
        if self._state is not SolverState.DISABLED:
            self._state = SolverState.WAITING_RESPAWN
            self._respawn_at_ms = now_ms + self._respawn_interval_ms
            LOGGER.warning(
                "Shadow solver の子を終わらせた; 間隔をあけて作り直す",
                extra={logs.FIELDS_KEY: {"reason": "shadow_unavailable"}},
            )

    def _kill(self, child: _Child) -> None:
        with contextlib.suppress(OSError):
            child.sock.close()
        if child.process.is_alive():
            child.process.kill()
        self._reaping.append(child.process)
        self._reap()

    def _reap(self) -> None:
        # 待たない（join(0) は WNOHANG）。終わった子だけを片付ける
        remaining = []
        for process in self._reaping:
            process.join(timeout=0)
            if process.exitcode is None:
                remaining.append(process)
        self._reaping = remaining
