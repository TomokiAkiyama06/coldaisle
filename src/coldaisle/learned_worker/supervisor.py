"""RL Supervisor worker の1周期（#89 / 決定記録 0077 段階 4、§2.3 / §2.6 / §2.8）。

`SupervisorWorkerCore.step()` を `supervisor.period_ms` ごとに1回呼ぶ。最後に受け取った frame に
対して、

1. 別の `run_id` なら束縛と停止を捨てる（fand の再起動。0077 §2.6）
2. 止まっていれば何もしない（固定した policy が production でなくなった後は別の `run_id` まで
   再開しない。0077 §2.6）
3. frame の `config` を自分の Control Config と照合する（違えば出力を作らない。0077 §2.3）
4. 固定された policy がいまも `supervisor_policy` の production かを registry で照合する
   （0077 §2.6）
5. 束縛が無ければ、固定された policy を registry の検証経路で読み、**shadow 用に**束縛する
   （`SupervisorPolicyBinding.for_shadow`。active への門は閉じたまま。0061 §2.4 / 0077 §2.10）
6. frame の snapshot と Workload Regime から `SupervisorInput` を作り、policy を1回呼ぶ

結果は `DeliveredSupervisorOutput`（`SupervisorOutput` と artifact の識別）。**何も送らない周期は
None。** RL worker には失敗を送る本文が無い（0077 §2.5 / §2.6 の「出力を送らない」）。fand の
Coordinator は未受信として扱い、active slot なら RulePolicy へ戻る。

- **Demand を出さない。** 出せるのは strategy / 目的関数の重み / target band / regime まで
  （AGENTS.md ルール5）。Rule / RL の選択・期限・識別の照合・`output_bounds` の検査は fand の
  `SupervisorCoordinator` が行い、Gate → Reactive Guard → Critical Safety も fand の中で常に掛かる
- **束縛の用途（`origin`）を名乗らない。** 経路の型に欄が無く、fand は `unverified` のまま扱う
  （0077 §2.8）。したがって RL の出力は shadow slot でだけ使われる
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from coldaisle import logs
from coldaisle.clock import Clock
from coldaisle.control.config import ControlConfig
from coldaisle.control.learned_handoff import LearnedFrame, PinnedArtifact
from coldaisle.control.model_registry import (
    ArtifactKind,
    ArtifactRef,
    ModelCompatibility,
    ModelRegistry,
    VerifiedArtifact,
)
from coldaisle.control.schema import AuthorityStage
from coldaisle.control.supervisor.artifact import (
    POLICY_ACTION_SCHEMA_VERSION,
    POLICY_STATE_SCHEMA_VERSION,
)
from coldaisle.control.supervisor.policy import DeliveredSupervisorOutput, SupervisorInput
from coldaisle.control.supervisor.rl_policy import (
    RegimeTableRlPolicy,
    SupervisorPolicyBinding,
    SupervisorPolicyUnusableError,
)
from coldaisle.learned_worker.registry import ProductionCheck

LOGGER = logging.getLogger("coldaisle.learned_worker.supervisor")


class SkipCode:
    """出力を作らない理由の code（構造化ログだけに残す。fand へは送らない）。"""

    CONFIG_MISMATCH = "config_mismatch"
    """frame の `config` が worker の Control Config と違う（0077 §2.3。一致するまで毎周期）。"""
    RL_NOT_CONFIGURED = "rl_version_not_configured"
    """Control Config に `supervisor.rl_version` が無い（RL を使わない構成）。"""
    ARTIFACT_NOT_PRODUCTION = "artifact_not_production"
    """固定した policy が production でない・registry が読めない（0077 §2.6）。

    別の run_id まで止まる。
    """
    POLICY_UNUSABLE = "policy_unusable"
    """registry から読めない・束縛の検査に外れた（別の run_id まで作り直さない）。"""
    WORKLOAD_UNAVAILABLE = "workload_unavailable"
    """frame に Workload Regime の推定が無い・snapshot と揃わない。"""


class PolicySource(Protocol):
    """固定された policy を registry の検証経路で読む（bytes の checksum と schema）。"""

    def load(self, pinned: PinnedArtifact) -> VerifiedArtifact | str:
        """検証済みの artifact か、読めなかった理由（path を含まない）。**例外を出さない。**"""


class RegistryPolicySource:
    """固定された版を `load_version` で読む（Registry を作らず書かない）。"""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    def load(self, pinned: PinnedArtifact) -> VerifiedArtifact | str:
        """shadow の stage で読む（`for_shadow` と同じ契約。`supervisor_shadow` の CLI と同じ）。"""
        try:
            result = self._registry.load_version(
                ArtifactRef(
                    kind=ArtifactKind.SUPERVISOR_POLICY,
                    model_id=pinned.model_id,
                    version=pinned.version,
                ),
                ModelCompatibility(
                    feature_schema_version=POLICY_STATE_SCHEMA_VERSION,
                    target_schema_version=POLICY_ACTION_SCHEMA_VERSION,
                    authority_stage=AuthorityStage.SHADOW,
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


@dataclass(frozen=True, slots=True)
class SupervisorWorkerInputs:
    """worker が起動時に読んだもの（Control Config）。Metric Catalog は使わない。"""

    control: ControlConfig


class SupervisorWorkerCore:
    """1周期ごとの判断。I/O（ソケット・時計の待ち）を持たない。"""

    def __init__(
        self,
        inputs: SupervisorWorkerInputs,
        *,
        policies: PolicySource,
        production: ProductionCheck,
        clock: Clock,
    ) -> None:
        self._control = inputs.control
        self._digest = inputs.control.runtime_digest()
        self._policies = policies
        self._production = production
        self._clock = clock
        self._run_id: str | None = None
        self._latest: LearnedFrame | None = None
        self._stopped = False
        self._policy: RegimeTableRlPolicy | None = None
        self._unusable = False
        self._noted: set[str] = set()

    @property
    def run_id(self) -> str | None:
        """いま受け取っている frame の `run_id`。"""
        return self._run_id

    @property
    def latest(self) -> LearnedFrame | None:
        """最後に受け取った frame（試験と診断のため）。"""
        return self._latest

    @property
    def stopped(self) -> bool:
        """別の `run_id` まで止まっているか。"""
        return self._stopped

    @property
    def bound(self) -> bool:
        """policy を束縛しているか。"""
        return self._policy is not None

    def receive(self, run_id: str, frame: LearnedFrame) -> None:
        """検証を通った v3 の frame を受け取る。**最新の1つだけ**を持つ（window を作らない）。"""
        if run_id != self._run_id:
            self._start_run(run_id)
        self._latest = frame

    def step(self) -> DeliveredSupervisorOutput | None:
        """この周期の出力。何も送らない周期は None。"""
        frame = self._latest
        if frame is None or self._stopped:
            return None
        if frame.config != self._digest:
            self._skip(SkipCode.CONFIG_MISMATCH)
            return None
        expected_version = self._control.policy.supervisor.rl_version
        if expected_version is None:
            self._skip(SkipCode.RL_NOT_CONFIGURED)
            return None
        pinned = frame.expected_artifacts.supervisor_policy
        if pinned is None:
            # production が無い（0077 §2.6）。何も送らない
            return None
        if not self._production.is_production(pinned):
            self._stop()
            return None
        policy = self._policy
        if policy is None:
            if self._unusable:
                return None
            policy = self._bind(pinned, expected_version)
            if policy is None:
                return None
        workload = frame.workload
        if workload is None:
            self._skip(SkipCode.WORKLOAD_UNAVAILABLE)
            return None
        try:
            policy_input = SupervisorInput(snapshot=frame.snapshot, workload=workload)
        except ValueError:
            # Workload Regime と snapshot の tick が揃わない frame（fand の不具合）。推測で揃えない
            self._skip(SkipCode.WORKLOAD_UNAVAILABLE)
            return None
        output = policy.propose(policy_input)
        return DeliveredSupervisorOutput(output=output, identity=policy.identity)

    # ------------------------------------------------------------------ 内部

    def _start_run(self, run_id: str) -> None:
        """別の `run_id`（fand の再起動）。束縛・停止を捨てる（0077 §2.6）。"""
        self._run_id = run_id
        self._latest = None
        self._stopped = False
        self._policy = None
        self._unusable = False
        self._noted = set()

    def _stop(self) -> None:
        """別の `run_id` の frame まで止まる（0077 §2.6）。

        registry が元へ戻っても自分では再開しない。
        """
        self._stopped = True
        self._policy = None
        LOGGER.error(
            "固定した RL policy が production でない・registry を確かめられないため、"
            "RL Supervisor worker を別の run_id まで止める",
            extra={
                logs.FIELDS_KEY: {
                    "reason": SkipCode.ARTIFACT_NOT_PRODUCTION,
                    "run_id": self._run_id,
                }
            },
        )

    def _bind(self, pinned: PinnedArtifact, expected_version: str) -> RegimeTableRlPolicy | None:
        """固定された policy を読み、**shadow 用に**束縛する。失敗は別の run_id まで覚える。"""
        loaded = self._policies.load(pinned)
        detail: str | None = None
        if isinstance(loaded, str):
            detail = loaded
        else:
            try:
                binding = SupervisorPolicyBinding.for_shadow(
                    loaded,
                    expected_policy_version=expected_version,
                    bounds=self._control.policy.supervisor.output_bounds,
                )
            except SupervisorPolicyUnusableError as error:
                detail = str(error)
            else:
                self._policy = RegimeTableRlPolicy.from_binding(binding, self._clock)
                LOGGER.info(
                    "RL policy を shadow 用に束縛した",
                    extra={
                        logs.FIELDS_KEY: {
                            "run_id": self._run_id,
                            "model_id": pinned.model_id,
                            "version": pinned.version,
                        }
                    },
                )
                return self._policy
        self._unusable = True
        LOGGER.error(
            "RL policy を束縛できないため、別の run_id まで出力を作らない",
            extra={
                logs.FIELDS_KEY: {
                    "reason": SkipCode.POLICY_UNUSABLE,
                    "run_id": self._run_id,
                    "detail": detail[:500],
                }
            },
        )
        return None

    def _skip(self, code: str) -> None:
        """出力を作らない。理由は run_id ごとに1回だけログに残す（毎周期は溢れる）。"""
        if code not in self._noted:
            self._noted.add(code)
            LOGGER.warning(
                "RL Supervisor の出力を作らない",
                extra={logs.FIELDS_KEY: {"reason": code, "run_id": self._run_id}},
            )
        return None
