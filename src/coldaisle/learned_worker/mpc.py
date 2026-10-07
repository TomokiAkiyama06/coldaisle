"""Learned MPC worker の1周期（決定記録 0077 §2.3 / §2.6、0101 §2.3、0107）。

`MpcWorkerCore.step()` を `mpc.period_ms` ごとに1回呼ぶ。最後に受け取った frame に対して、

1. 別の `run_id` なら束縛と停止を捨てる（0101 §2.3。fand の再起動で較正が変わりうる）
2. 止まっていれば何もしない（`artifact_not_production` / `calibration_changed_within_run` /
   `feature_metric_not_in_snapshot` の後は別の `run_id` まで再開しない）
3. frame の `config` と Metric Catalog の SHA-256 を自分の値と照合する（違えば失敗を毎周期）
4. 同じ `run_id` の中で `calibration` が変わったら止まる（0101 §2.3 / §5 #2）
5. 固定された artifact がいまも production かを registry で照合する（0077 §2.6）
6. 束縛が無い・`authority_stage` が上がったら、frame の較正で束縛を作り直す（0077 §2.3 / 0101 §2.3）
7. feature metric と目的関数の metric が snapshot の signal にあるか（0107 §2.5）
8. 観測 window と anchor の action を作り（0107 §2.1 / §2.2）、`propose()` を呼ぶ

結果は `MpcProposal`（提案か失敗）。**何も送らない周期は None**（0107 §2.3）。提案に出せるのは
requested までで、Gate → Reactive Guard → Critical Safety は fand の中で常に掛かる
（AGENTS.md ルール2）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from coldaisle import logs
from coldaisle.control.air_balance import ConfiguredAirBalanceModel
from coldaisle.control.config import ControlConfig
from coldaisle.control.fallback.gate import LearnedFailure
from coldaisle.control.learned_handoff import (
    AvailableCalibration,
    LearnedExpectedArtifacts,
    LearnedFrame,
    PinnedArtifact,
    UnavailableCalibration,
    calibration_to_runtime,
)
from coldaisle.control.model.counterfactual import (
    FEATURE_SCHEMA_V2_VERSION,
    TARGET_SCHEMA_V2_VERSION,
)
from coldaisle.control.model_registry import (
    REGISTRY_STATE_FILENAME,
    ArtifactKind,
    ArtifactRef,
    ModelCompatibility,
    ModelRegistry,
    VerifiedArtifact,
)
from coldaisle.control.mpc.controller import LearnedMpcRuntime, MpcProposal
from coldaisle.control.schema import AuthorityStage, Reason, stage_rank
from coldaisle.learned_channel.registry_watch import FileIdentity, snapshot_identity
from coldaisle.learned_worker.window import FrameHistory, build_observed_input
from coldaisle.metrics import MetricCatalog

LOGGER = logging.getLogger("coldaisle.learned_worker")

FrameCalibration = AvailableCalibration | UnavailableCalibration


class FailureCode:
    """worker が返す失敗の理由の code（`Reason.code`）。すべて `model_load_failure` で返す。"""

    CONFIG_MISMATCH = "config_mismatch"
    """frame の `config` が worker の Control Config と違う（0077 §2.3。一致するまで毎周期）。"""
    METRIC_CATALOG_MISMATCH = "metric_catalog_mismatch"
    """frame の `metric_catalog_sha256` が worker の catalog と違う（0107 §2.6。毎周期）。"""
    ARTIFACT_NOT_PRODUCTION = "artifact_not_production"
    """固定した artifact が production でない・registry が読めない（0077 §2.6。1回で止まる）。"""
    CALIBRATION_CHANGED = "calibration_changed_within_run"
    """同じ `run_id` の中で `calibration` が変わった（0101 §2.3。1回返して止まる）。"""
    FEATURE_METRIC_NOT_IN_SNAPSHOT = "feature_metric_not_in_snapshot"
    """artifact の feature / 目的関数の metric が snapshot に無い（0107 §2.5。1回返して止まる）。"""
    MODEL_UNUSABLE = "model_unusable"
    """registry から読めない・束縛の検査に外れた（`LearnedMpcRuntime.load` と同じ code）。"""


class ProductionCheck(Protocol):
    """固定された artifact がいまもその kind の production か（0077 §2.6）。"""

    def is_production(self, pinned: PinnedArtifact) -> bool:
        """**例外を出さない。** 読めない・壊れているときは False（保守側）。"""


class ArtifactSource(Protocol):
    """固定された artifact を registry の検証経路で読む（bytes の checksum と schema）。"""

    def load(self, pinned: PinnedArtifact, stage: AuthorityStage) -> VerifiedArtifact | str:
        """検証済みの artifact か、読めなかった理由（path を含まない）。**例外を出さない。**"""


class RegistryProductionCheck:
    """`registry.json` を読み取り専用で確かめる（flock を取らない。0077 §2.6）。

    `stat` の同一性（`st_dev` / `st_ino` / `st_mtime_ns` / `st_size`）と固定が前回と同じなら
    読み直さない。
    """

    def __init__(self, registry: ModelRegistry, root: Path) -> None:
        self._registry = registry
        self._path = root / REGISTRY_STATE_FILENAME
        self._cached: tuple[FileIdentity | None, PinnedArtifact, bool] | None = None

    def is_production(self, pinned: PinnedArtifact) -> bool:
        """固定がいまの `thermal_model` の production の3つ組と一致するか。"""
        try:
            identity = snapshot_identity(self._path)
            cached = self._cached
            if cached is not None and cached[0] == identity and cached[1] == pinned:
                return cached[2]
            snapshot = self._registry.inspect()
            current = LearnedExpectedArtifacts.from_provenance(snapshot.trace_provenance())
        except Exception as error:
            LOGGER.error(
                "registry を確かめられない。固定した artifact を production とみなさない",
                extra={
                    logs.FIELDS_KEY: {
                        "reason": "registry_unreadable",
                        "error": type(error).__name__,
                    }
                },
            )
            self._cached = None
            return False
        result = current.thermal_model == pinned
        self._cached = (identity, pinned, result)
        return result


class RegistryArtifactSource:
    """固定された版を `load_version` で読む（Registry を作らず書かない）。"""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    def load(self, pinned: PinnedArtifact, stage: AuthorityStage) -> VerifiedArtifact | str:
        """反実仮想 artifact v2 の runtime の契約（schema と**実効 stage**）で読む。"""
        try:
            ref = ArtifactRef(
                kind=ArtifactKind.THERMAL_MODEL, model_id=pinned.model_id, version=pinned.version
            )
            result = self._registry.load_version(
                ref,
                ModelCompatibility(
                    feature_schema_version=FEATURE_SCHEMA_V2_VERSION,
                    target_schema_version=TARGET_SCHEMA_V2_VERSION,
                    authority_stage=stage,
                ),
            )
        except Exception as error:
            return f"registry から読めない: {type(error).__name__}"
        if result.artifact is None:
            return f"registry から読めない（status={result.status.value}）: {result.detail}"
        if result.artifact.attestation.artifact_sha256 != pinned.artifact_sha256:
            # 同じ版を名乗る別の bytes を使わない（frame の固定は3つ組。0077 §2.6）
            return "registry の artifact の SHA-256 が frame の固定と違う"
        return result.artifact


class FrameAuthority:
    """controller が毎 tick 照らす「いまの実効 stage」。使う frame の `authority_stage`。"""

    __slots__ = ("stage",)

    def __init__(self, stage: AuthorityStage) -> None:
        self.stage = stage

    def current_stage(self) -> AuthorityStage:
        """使う frame の実効 stage（0057 §2.2）。"""
        return self.stage


@dataclass(frozen=True, slots=True)
class WorkerInputs:
    """worker が起動時に読んだもの（Control Config と Metric Catalog）。"""

    control: ControlConfig
    catalog: MetricCatalog
    catalog_sha256: str


class MpcWorkerCore:
    """1周期ごとの判断。I/O（ソケット・時計の待ち）を持たない。"""

    def __init__(
        self,
        inputs: WorkerInputs,
        *,
        artifacts: ArtifactSource,
        production: ProductionCheck,
        monotonic_ms: Callable[[], int],
    ) -> None:
        self._inputs = inputs
        self._digest = inputs.control.runtime_digest()
        self._artifacts = artifacts
        self._production = production
        self._monotonic_ms = monotonic_ms
        self._history = FrameHistory()
        self._run_id: str | None = None
        self._stopped = False
        self._calibration: FrameCalibration | None = None
        """この `run_id` で最初に受け取った frame の較正（束縛と照合の基準）。"""
        self._calibration_changed = False
        self._runtime: LearnedMpcRuntime | None = None
        self._runtime_failure: Reason | None = None
        self._bound_stage: AuthorityStage | None = None
        self._authority = FrameAuthority(AuthorityStage.SHADOW)

    @property
    def history(self) -> FrameHistory:
        """受け取った frame の列（試験と診断のため）。"""
        return self._history

    @property
    def stopped(self) -> bool:
        """別の `run_id` まで止まっているか。"""
        return self._stopped

    @property
    def bound_stage(self) -> AuthorityStage | None:
        """いまの束縛を作ったときの stage。束縛が無ければ None。"""
        return self._bound_stage

    def receive(self, run_id: str, frame: LearnedFrame) -> None:
        """検証を通った v3 の frame を受け取る（壊れた frame はここへ来ない。0101 §2.3）。"""
        if run_id != self._run_id:
            self._start_run(run_id)
        # **受け取った frame ごとに**較正を照らす。周期の間に複数届いても、最新の frame だけを
        # 見ると途中の変化（A → B → A）を見落とし、食い違った frame で window を作りうる
        if self._calibration is None:
            self._calibration = frame.calibration
        elif frame.calibration != self._calibration:
            self._calibration_changed = True
        self._history.add(run_id, frame)
        schema_window = self._window_ms()
        self._history.prune(schema_window)

    def step(self) -> MpcProposal | None:
        """この周期の結果。何も送らない周期は None（0107 §2.3）。"""
        frame = self._history.latest
        run_id = self._history.run_id
        if frame is None or run_id is None:
            return None
        if self._stopped:
            return None
        if frame.config != self._digest:
            return _failure(FailureCode.CONFIG_MISMATCH, "frame の config が worker の設定と違う")
        if frame.metric_catalog_sha256 != self._inputs.catalog_sha256:
            return _failure(
                FailureCode.METRIC_CATALOG_MISMATCH,
                "frame の metric_catalog_sha256 が worker の Metric Catalog と違う",
            )
        if self._calibration_changed:
            # 束縛を黙って作り直さない。fand の不具合を隠さない（0101 §5 #2）
            return self._stop(
                FailureCode.CALIBRATION_CHANGED,
                "同じ run_id の中で calibration が変わった",
            )
        pinned = frame.expected_artifacts.thermal_model
        if pinned is None:
            # production が無い（0077 §2.6）。何も送らない
            return None
        if not self._production.is_production(pinned):
            return self._stop(
                FailureCode.ARTIFACT_NOT_PRODUCTION,
                "固定された artifact がいまの production でない・registry を確かめられない",
            )
        if self._needs_binding(frame.authority_stage):
            stopped = self._bind(frame, pinned)
            if stopped is not None:
                return stopped
        if self._runtime_failure is not None:
            return MpcProposal(
                failure=LearnedFailure.MODEL_LOAD_FAILURE, failure_reason=self._runtime_failure
            )
        runtime = self._runtime
        assert runtime is not None and runtime.controller is not None
        built = build_observed_input(
            self._history.frames, runtime.controller.binding.model.feature_schema
        )
        if built.observed is None or frame.supervisor is None or frame.baseline is None:
            return None
        self._authority.stage = frame.authority_stage
        return runtime.propose(
            snapshot=frame.snapshot,
            observed=built.observed,
            supervisor=frame.supervisor,
            baseline=frame.baseline,
            safety_floor=frame.safety_floor,
            # 段階 3 は residual の証拠を持たない（0107 §2.4）
            residual=None,
        )

    # ------------------------------------------------------------------ 内部

    def _start_run(self, run_id: str) -> None:
        """別の `run_id`（fand の再起動）。束縛・停止・較正を捨てる（0101 §2.3 / 0077 §2.6）。"""
        self._run_id = run_id
        self._stopped = False
        self._calibration = None
        self._calibration_changed = False
        self._runtime = None
        self._runtime_failure = None
        self._bound_stage = None

    def _stop(self, code: str, detail: str) -> MpcProposal:
        """失敗を1回返し、別の `run_id` の frame まで止まる。"""
        self._stopped = True
        self._runtime = None
        self._runtime_failure = None
        self._bound_stage = None
        LOGGER.error(
            "Learned MPC worker を別の run_id まで止める",
            extra={logs.FIELDS_KEY: {"reason": code, "run_id": self._run_id}},
        )
        return _failure(code, detail)

    def _needs_binding(self, stage: AuthorityStage) -> bool:
        """束縛が無い、または frame の stage が束縛の stage を上回った（0077 §2.3）。

        **下がったときは作り直さない**（束縛が実効 stage を覆っている限り Gate は通す）。
        """
        if self._bound_stage is None:
            return True
        return stage_rank(stage) > stage_rank(self._bound_stage)

    def _bind(self, frame: LearnedFrame, pinned: PinnedArtifact) -> MpcProposal | None:
        """frame の較正と固定された artifact で束縛を作り直す（L1〜L12。L9 は frame の較正）。"""
        stage = frame.authority_stage
        self._runtime = None
        self._runtime_failure = None
        self._bound_stage = stage
        self._authority.stage = stage
        loaded = self._artifacts.load(pinned, stage)
        if isinstance(loaded, str):
            self._runtime_failure = Reason(code=FailureCode.MODEL_UNUSABLE, detail=loaded[:500])
            return None
        assert self._calibration is not None
        control = self._inputs.control
        enabled = control.air_balance_enabled
        runtime = LearnedMpcRuntime.load(
            loaded,
            control.policy,
            control.safety,
            metric_catalog=self._inputs.catalog,
            # **frame の較正だけを渡す。** worker は較正ファイルを読まない（0101 §2.1）
            calibration=calibration_to_runtime(self._calibration),
            expected_model_version=pinned.version,
            monotonic_ms=self._monotonic_ms,
            authority=self._authority,
            # 任意依存は fand と同じ規則で同じ Control Config から作る（0107 §2.6）。
            # acoustic は #94 まで渡さない
            air_balance=(
                ConfiguredAirBalanceModel(control.air_balance, control.sources.air_balance.sha256)
                if enabled
                else None
            ),
            balance_band=control.air_balance.balance if enabled else None,
            fan_hardware=control.fan_hardware if enabled else None,
        )
        if runtime.controller is None:
            assert runtime.failure_reason is not None
            self._runtime_failure = runtime.failure_reason
            return None
        missing = self._metrics_missing_from(frame, runtime)
        if missing:
            return self._stop(
                FailureCode.FEATURE_METRIC_NOT_IN_SNAPSHOT, f"snapshot に無い metric: {missing}"
            )
        self._runtime = runtime
        self._history.prune(self._window_ms())
        return None

    def _metrics_missing_from(self, frame: LearnedFrame, runtime: LearnedMpcRuntime) -> list[str]:
        assert runtime.controller is not None
        schema = runtime.controller.binding.model.feature_schema
        cost = self._inputs.control.policy.mpc.optimizer.cost_metrics
        required = {*schema.metrics, cost.cpu_temperature, cost.gpu_temperature}
        present = {signal.metric for signal in frame.snapshot.signals}
        return sorted(required - present)

    def _window_ms(self) -> int | None:
        runtime = self._runtime
        if runtime is None or runtime.controller is None:
            return None
        return runtime.controller.binding.model.feature_schema.window_ms


def _failure(code: str, detail: str) -> MpcProposal:
    return MpcProposal(
        failure=LearnedFailure.MODEL_LOAD_FAILURE,
        failure_reason=Reason(code=code, detail=detail[:500]),
    )
