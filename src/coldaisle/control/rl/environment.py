"""RL Supervisor の学習・評価環境（#105 / 決定記録 0058）。

1 step の流れは、**運転時と同じ順序**をそのまま辿る。

```text
Workload 擾乱 → Control State → Supervisor action（strategy / weights / target band）
  → Learned MPC（#86）→ Controller Gate（#79 / #85）→ requested demand
  → Dynamics → 次の観測 → reward + Safety 台帳
```

**agent は Demand を探索しない。** action から demand への写像は Learned MPC が持ち、
環境はそれを迂回する API を持たない（AGENTS.md ルール1 / 2、決定記録 0027 / 0028 §2.4）。

**Fan へ届く経路を持たない。** この package は `control.hardware` / `control.safety` /
`control.reactive` / `serial` / `subprocess` を import せず、`EffectiveZoneDemand` も PWM も
扱わない（試験で走査する）。安全は `safety.yaml` の値を読む screen だけで、
最終裁定は運転時の #80 / #78 が行う（決定記録 0058 §2.5）。

**壁時計を持たない。** 時刻はすべて episode の証拠（window の時刻と設定した刻み）から来る。
同じ設定・同じ seed・同じ trace からは同じ結果が出る（決定記録 0054 §2.7 と同じ規則）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from random import Random
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.control.acoustic import AcousticCostModel
from coldaisle.control.config import FanPolicyConfig, SafetyConfig
from coldaisle.control.fallback.gate import ControllerGate, LearnedControlStatus, LearnedFailure
from coldaisle.control.model.thermal import (
    ObservedThermalInput,
    ObservedWindowFrame,
    canonical_sha256,
)
from coldaisle.control.mpc import LearnedMpcController
from coldaisle.control.rl.action import (
    ActionSpace,
    InvalidSupervisorActionError,
    SupervisorAction,
)
from coldaisle.control.rl.config import RlTrainingConfig
from coldaisle.control.rl.dynamics import (
    DynamicsRequest,
    DynamicsUnusableError,
    EnvironmentDynamics,
    WorkloadSample,
    WorkloadTrace,
    attested_evidence,
)
from coldaisle.control.rl.episode import (
    EPISODE_SCHEMA_VERSION,
    EpisodeCoverage,
    EpisodeResult,
    EpisodeSafety,
    PolicyArm,
    PolicyComparison,
    SafetyModel,
    StepRecord,
    TerminationReason,
    conditions_digest,
)
from coldaisle.control.rl.reward import (
    REWARD_SCHEMA_VERSION,
    RewardFunction,
    RewardUnusableError,
    SafetyLedgerEntry,
)
from coldaisle.control.schema import (
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
    SupervisorPolicyKind,
    Zone,
)
from coldaisle.control.state import (
    ControlStateSnapshot,
    FanState,
    SnapshotSignal,
    TelemetryHealth,
    TelemetryImportance,
)
from coldaisle.control.supervisor.policy import SupervisorInput, SupervisorPolicy
from coldaisle.control.supervisor.regime import (
    RegimeEvidence,
    RegimeReason,
    WorkloadRegimeEstimate,
)
from coldaisle.store.models import Quality

DEMAND_EPSILON = 1e-9
"""floor 比較の丸め許容。構造上の値で、安全の余裕ではない。"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _snapshot_signal(metric: str, frame: ObservedWindowFrame, *, ts_ms: int) -> SnapshotSignal:
    """観測 window の1 cell を、#102 の Snapshot signal へ**そのまま**写す。

    **欠測・stale・suspect を `OK` へ丸めない。** 丸めると、本物の Fallback Controller が
    「使えない」と判断する値を環境だけが使ってしまい、環境と運転で別の demand が出る
    （`SnapshotSignal.available` は `Quality.OK` だけを通す）。
    """
    value = frame.values[metric]
    source_ts = frame.source_ts_ms[metric]
    if frame.missing_mask[metric] or value is None:
        # 未観測 cell は source 時刻も持たない（#84 の window の不変条件）。
        return SnapshotSignal(
            metric=metric,
            importance=TelemetryImportance.CRITICAL,
            enabled=True,
            value=None,
            quality=Quality.MISSING,
        )
    if frame.stale_mask[metric]:
        quality = Quality.STALE
    elif frame.suspect_mask[metric]:
        quality = Quality.SUSPECT
    else:
        quality = Quality.OK
    return SnapshotSignal(
        metric=metric,
        importance=TelemetryImportance.CRITICAL,
        enabled=True,
        value=value,
        quality=quality,
        source_ts_ms=source_ts,
        last_changed_mono_ms=source_ts,
        age_ms=max(0, ts_ms - source_ts) if source_ts is not None else None,
    )


def _window_demands(window: ObservedThermalInput) -> PerZone[Demand]:
    """観測 window の action（= **実際に掛かっていた** demand）を返す。

    #84 の観測 window は「その action が実際に掛かった結果の観測」なので、掛かっている
    action の出どころはここしかない（決定記録 0052 §2.4）。
    """
    action = window.action
    return PerZone[Demand](
        front=action.front.effective_demand,
        rear=action.rear.effective_demand,
        top=action.top.effective_demand,
    )


def _frame_values(window: ObservedThermalInput) -> dict[str, float]:
    """window の最後の frame の、**使える値だけ**を返す。

    欠測・stale・suspect の cell は安全 screen にも reward にも渡さない。
    「読めていない値」を「上限を下回っている」と読み替えないためである。
    """
    frame = window.window[-1]
    return {
        metric: value
        for metric, value in frame.values.items()
        if value is not None
        and not frame.missing_mask[metric]
        and not frame.stale_mask[metric]
        and not frame.suspect_mask[metric]
    }


def _config_digest(config: BaseModel) -> str:
    """検証済み設定そのものの SHA-256。**欄を数え上げずに全体を覆う。**"""
    payload = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


class EnvironmentUsageError(RuntimeError):
    """環境の使い方が間違っている（reset していない、終わった episode を進めた、など）。

    **これは episode の終端理由にしない。** agent の action の善し悪しではなく、
    呼び出し側の誤りなので、そのまま例外として出す。
    """


class DependencyIdentity(_Frozen):
    """注入した依存を条件 hash へ載せるための、安定した識別子。

    **factory も model object も hash できない。** それでも Baseline の設定や Acoustic の
    曲線が違えば結果は変わるので、比較したときに「依存の差」が「policy の差」に見えてしまう。
    呼び出し側に名指しを**必須**で求めることで、その取り違えを作れなくする
    （決定記録 0058 §2.6）。
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=120)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    """設定 bytes などの hash。持てるなら必ず入れる。"""


class BaselineProposer(Protocol):
    """Baseline（#79 Fallback）の提案を作る読み取り専用の契約。

    `FallbackController` がそのまま満たす。環境が Baseline を自前で作らないのは、
    MPC の探索の出発点が Baseline であること（決定記録 0052 §2.3）を崩さないためである。
    """

    def propose(self, snapshot: ControlStateSnapshot) -> ControllerProposal:
        """1 tick の Fallback 提案を返す。"""
        ...


class _ProposalFacts(_Frozen):
    """Gate が選んだ提案について、step の記録に残す事実。"""

    controller: ControllerKind
    fallback_reason: Reason | None = None
    optimizer_status: OptimizerStatus | None = None
    confidence: ConfidenceLevel | None = None
    ood: bool | None = None
    detail: str = Field(default="", max_length=500)


class EpisodeSpec(_Frozen):
    """1 episode を再現するために固定するもの。

    **seed・dataset / model 版・workload trace・reward 版をすべて固定する**（#105 受入基準）。
    reward 版と model 版は環境が持ち、ここには episode ごとに変わるものだけを置く。

    **学習 mode は欄として持たない。** 注入した dynamics（`EnvironmentDynamics.mode`）から
    derive する。欄として持つと、記録再生を `learned_simulator` と書いた結果を作れてしまう
    （決定記録 0058 §2.2）。
    """

    episode_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=120)
    seed: int = Field(ge=0)
    trace: WorkloadTrace
    initial_window: ObservedThermalInput
    max_steps: int | None = Field(default=None, ge=1)
    """省略すると設定の `episode.max_steps` を使う。設定より長くはできない。"""

    @property
    def initial_demands(self) -> PerZone[Demand]:
        """episode の開始時に掛かっている demand。

        **観測 window の action から derive する。** 別の欄として持つと、`initial_window` の
        action と食い違う「2つの掛かっている action」を作れてしまい、片方だけが安全 screen を
        通る。#84 の観測 window は「その action が実際に掛かった結果の観測」なので、
        起点はそこにしかない（決定記録 0052 §2.4 と同じ理由）。
        """
        action = self.initial_window.action
        return PerZone[Demand](
            front=action.front.effective_demand,
            rear=action.rear.effective_demand,
            top=action.top.effective_demand,
        )


class _EpisodeState:
    """進行中の episode の可変状態。**環境の外へは出さない。**"""

    __slots__ = (
        "applied",
        "ceiling_exceedances",
        "conditions_sha256",
        "ended",
        "floor_shortfalls",
        "history",
        "invalid_actions",
        "max_steps",
        "minimum_margin_c",
        "rng",
        "spec",
        "step_index",
        "steps",
        "termination",
        "termination_reason",
        "tick_id",
        "window",
    )

    def __init__(self, spec: EpisodeSpec, *, max_steps: int, conditions_sha256: str) -> None:
        self.spec = spec
        self.max_steps = max_steps
        self.conditions_sha256 = conditions_sha256
        self.rng = Random(spec.seed)
        self.window = spec.initial_window
        self.applied = spec.initial_demands
        self.step_index = 0
        self.tick_id = 0
        self.steps: list[StepRecord] = []
        self.history: list[ControlStateSnapshot] = []
        self.ended = False
        self.termination: TerminationReason | None = None
        self.termination_reason: Reason | None = None
        self.ceiling_exceedances = 0
        self.floor_shortfalls = 0
        self.invalid_actions = 0
        self.minimum_margin_c: float | None = None


class SupervisorTrainingEnvironment:
    """Supervisor action を MPC と dynamics を通して評価する環境。

    **`step` は Demand を受け取らない。** 受け取るのは `SupervisorAction` だけで、
    その action から demand を作るのは `LearnedMpcController` と `ControllerGate` である。
    """

    def __init__(
        self,
        config: RlTrainingConfig,
        *,
        config_sha256: str,
        policy: FanPolicyConfig,
        safety: SafetyConfig,
        dynamics: EnvironmentDynamics,
        baseline: Callable[[], BaselineProposer],
        baseline_identity: DependencyIdentity,
        expected_model_version: str,
        mpc: LearnedMpcController | None = None,
        mpc_unavailable: Reason | None = None,
        acoustic: AcousticCostModel | None = None,
        acoustic_identity: DependencyIdentity | None = None,
    ) -> None:
        """設定と依存を束ねる。**噛み合わなければ生成時に落とす。**

        `mpc` を省くと、**Learned MPC を束縛できなかった runtime** として動く。反実仮想能力を
        申告した artifact が1つも無い現状ではこちらが既定で（決定記録 0048 §2.1 / 0052 §2.1）、
        `mpc_unavailable` の理由を `LearnedFailure.MODEL_LOAD_FAILURE` として Gate（#79）へ渡し、
        Fallback で episode を回す。**偽の attestation を作らないための経路である。**
        """
        if not expected_model_version:
            raise EnvironmentUsageError("expected_model_version は空にできない")
        if (mpc is None) == (mpc_unavailable is None):
            raise EnvironmentUsageError(
                "Learned MPC controller か、束縛できなかった理由のどちらか一方を渡す"
            )
        if (acoustic is None) != (acoustic_identity is None):
            # 条件 hash に載らない依存を黙って受け取らない。
            raise EnvironmentUsageError("Acoustic Model と その identity は一緒に渡す")
        self._config = config
        self._config_sha256 = config_sha256
        self._policy = policy
        self._safety = safety
        self._dynamics = dynamics
        self._mpc = mpc
        self._mpc_unavailable = mpc_unavailable
        # **episode ごとに作り直す。** Fallback は復帰 hold などの状態を持つので、
        # 前の episode の履歴を次へ持ち越すと同じ seed でも結果が変わる。
        self._baseline_factory = baseline
        self._baseline_identity = baseline_identity
        self._acoustic_identity = acoustic_identity
        self._baseline: BaselineProposer | None = None
        self._expected_model_version = expected_model_version
        self._action_space = ActionSpace(policy.supervisor.output_bounds)
        self._reward = RewardFunction(config.reward, acoustic=acoustic)
        self._floor = PerZone[Demand](
            front=safety.zone_min_demand.front.value,
            rear=safety.zone_min_demand.rear.value,
            top=safety.zone_min_demand.top.value,
        )
        self._episode: _EpisodeState | None = None
        self._gate: ControllerGate | None = None
        # 結果に残す policy の識別。**条件 hash には入れない**（同じ条件で policy を比べるため）。
        self._policy_kind = SupervisorPolicyKind.RL
        self._policy_version = "unspecified"

    @property
    def action_space(self) -> ActionSpace:
        """設定済みの action 空間（`supervisor.output_bounds`）。"""
        return self._action_space

    @property
    def config_sha256(self) -> str:
        """この環境が回している `rl-training.yaml` の bytes hash。

        **利用者（#89）に写しを持たせないために公開する。** 同じ値を別経路で持つと、
        環境と報告が違う設定を指したまま気づけない。
        """
        return self._config_sha256

    @property
    def reward_version(self) -> str:
        """この環境が使っている reward 版（`rl-training.yaml` の `reward.version`）。"""
        return self._reward.version

    @property
    def dynamics(self) -> EnvironmentDynamics:
        """注入した dynamics への読み取り参照。**環境が持つものと同一である。**

        学習 mode と出どころを報告へ写すために公開する。object そのものは呼び出し側が
        渡したものなので、新しい能力は増えない。
        """
        return self._dynamics

    @property
    def safety_model(self) -> SafetyModel:
        """環境が持っている安全の表現。**Critical Safety そのものではない。**"""
        return SafetyModel.CONFIGURED_MINIMUM_ONLY

    @property
    def learned_controller_available(self) -> bool:
        """Learned MPC を束縛できているか。

        **`False` のとき、Supervisor action は demand に一切効かない**（requested はすべて
        Fallback が作る）。反実仮想 artifact が無い間はこちらが既定である。
        """
        return self._mpc is not None

    # ------------------------------------------------------------------ episode 単位

    def reset(
        self,
        spec: EpisodeSpec,
        *,
        policy: SupervisorPolicyKind = SupervisorPolicyKind.RL,
        policy_version: str = "unspecified",
    ) -> SupervisorInput:
        """episode を初期化し、policy が読む最初の入力を返す。

        `policy` / `policy_version` は**結果に残すためだけ**に受け取る。条件 hash には
        入れないので、同じ条件のまま別の policy を回して比べられる。
        """
        self._policy_kind = policy
        self._policy_version = policy_version
        if spec.trace.step_ms != self._config.episode.step_ms.value:
            raise EnvironmentUsageError(
                "workload trace の刻みが episode 設定と違う"
                f"（trace={spec.trace.step_ms}; config={self._config.episode.step_ms.value}）"
            )
        max_steps = spec.max_steps or self._config.episode.max_steps
        if max_steps > self._config.episode.max_steps:
            raise EnvironmentUsageError("episode の step 数を設定の上限より長くしない")
        self._episode = _EpisodeState(
            spec, max_steps=max_steps, conditions_sha256=self._conditions_sha256(spec, max_steps)
        )
        # Gate も episode ごとに作り直す。復帰 hold や降格の数えを前の episode から持ち越さない。
        self._baseline = self._baseline_factory()
        self._gate = ControllerGate(
            self._policy,
            expected_model_version=self._expected_model_version,
            # **束縛した artifact の hash を Gate へ渡す**（#159 / 決定記録 0059）。
            # Learned MPC を束縛できなかった episode は `None` を渡す（提案が無いので
            # 照らす相手も無い。渡し忘れで「何にも照らさない」状態を作らない）。
            expected_artifact_sha256=(
                None if self._mpc is None else self._mpc.binding.attestation.artifact_sha256
            ),
            # **学習環境は journal を読まない**（#92 / 決定記録 0057 §2.2）。実機の制御権は
            # `AuthorityRuntime` が持つが、ここは同じ条件を再現するための simulation なので、
            # 設定の stage をそのまま実効 stage として固定する。
            authority=StaticAuthorityStage(self._policy.authority_stage),
        )
        # **最初の state にも screen を掛ける。** 掛けないと、上限を超えた初期 window を
        # 「まだ1 step も進んでいないから安全」として agent へ見せ、そこから探索を始めてしまう。
        self._screen_initial_state(self._episode)
        return self.observation()

    def _screen_initial_state(self, state: _EpisodeState) -> None:
        """episode の開始時点で既に違反している state を、探索の入口で終端する。"""
        shortfalls = tuple(
            zone
            for zone in Zone
            if state.applied.get(zone) < self._floor.get(zone) - DEMAND_EPSILON
        )
        if shortfalls:
            state.floor_shortfalls += len(shortfalls)
            detail = "; ".join(
                f"{zone.value}: initial={state.applied.get(zone):.6f}; "
                f"floor={self._floor.get(zone):.6f}"
                for zone in shortfalls
            )
            self._end(state, TerminationReason.SAFETY_VIOLATION, "initial_floor_shortfall", detail)
        exceedances, margin = self._screen_temperatures(_frame_values(state.window))
        if margin is not None:
            state.minimum_margin_c = (
                margin if state.minimum_margin_c is None else min(state.minimum_margin_c, margin)
            )
        if exceedances:
            state.ceiling_exceedances += exceedances
            self._end(
                state,
                TerminationReason.SAFETY_VIOLATION,
                "initial_ceiling_exceeded",
                f"ceiling={self._safety.absolute_temp_ceiling_c.value:.3f}; "
                f"exceedances={exceedances}",
            )

    def _screen_temperatures(self, values: Mapping[str, float]) -> tuple[int, float | None]:
        """screen の metric を絶対上限と突き合わせ、(超過数, 最小余裕) を返す。"""
        ceiling = self._safety.absolute_temp_ceiling_c.value
        screened = [
            values[metric]
            for metric in self._config.safety_screen.temperature_metrics
            if metric in values
        ]
        if not screened:
            return 0, None
        return sum(1 for value in screened if value > ceiling), ceiling - max(screened)

    def observation(self) -> SupervisorInput:
        """いまの state を policy が読む形で返す。**Demand の指令経路は持たない。**"""
        state = self._require_episode()
        snapshot = self._snapshot(state)
        workload = self._workload_estimate(state, snapshot)
        # **0 を「全部」と読まない。** `history[-0:]` は履歴すべてを返す。
        window = self._config.episode.recent_history_steps
        history = tuple(state.history[-window:]) if window > 0 else ()
        return SupervisorInput(snapshot=snapshot, recent_history=history, workload=workload)

    def step(self, action: SupervisorAction) -> StepRecord:
        """1 step 進める。**Demand は受け取らない。**

        agent の action が原因の失敗（範囲外・記録に無い action・安全側の違反）は例外にせず、
        episode の終端として記録する。呼び出し側の誤りだけを例外にする。
        """
        state = self._require_episode()
        if state.ended:
            raise EnvironmentUsageError("終わった episode を進めない")
        # `model_copy(update=...)` は検証を通らないため、ここで必ず検証し直す。
        # Demand を持つ別の型を action として渡す配線ミスも、ここで止まる。
        action = SupervisorAction.model_validate(action)
        snapshot = self._snapshot(state)
        record = self._step(state, snapshot, action)
        state.steps.append(record)
        state.history.append(snapshot)
        state.step_index += 1
        if not state.ended and state.step_index >= state.max_steps:
            self._end(
                state, TerminationReason.HORIZON, "episode_horizon", "設定した step 数に達した"
            )
        return record

    def episode_result(self) -> EpisodeResult:
        """いままでの step から結果を組み立てる。**終わっていなければ拒む。**"""
        state = self._require_episode()
        if not state.ended:
            raise EnvironmentUsageError("終わっていない episode の結果は作らない")
        assert state.termination is not None and state.termination_reason is not None
        coverage = self._coverage(state)
        safety = EpisodeSafety(
            ceiling_exceedances=state.ceiling_exceedances,
            floor_shortfalls=state.floor_shortfalls,
            invalid_actions=state.invalid_actions,
            minimum_margin_c=state.minimum_margin_c,
        )
        usable = self._is_usable(coverage)
        # **自称では立てない。** 封をした `DynamicsEvidence` object だけを見る（0058 §2.3）。
        evidence = attested_evidence(self._dynamics)
        # すべての step が、その証拠が裏づける唯一の出どころから来ていること。
        # 近似の step を `logged_trajectory` と名乗らせても、証拠が無ければここで落ちる。
        # **採点できなかった step を飛ばさない。** 終端の1 step だけが採点できていない
        # episode を「全部裏づけあり」と読むと、記録が尽きた区間が根拠に混ざる。
        every_step_is_backed = (
            evidence is not None
            and bool(state.steps)
            and all(
                step.supported and step.provenance is evidence.provenance for step in state.steps
            )
        )
        return EpisodeResult(
            episode_id=state.spec.episode_id,
            seed=state.spec.seed,
            mode=self._dynamics.mode,
            safety_model=self.safety_model,
            policy=self._policy_kind,
            policy_version=self._policy_version,
            reward_version=self._reward.version,
            discount=self._config.reward.discount.value,
            dynamics=self._dynamics.identity,
            steps=tuple(state.steps),
            termination=state.termination,
            termination_reason=state.termination_reason,
            coverage=coverage,
            safety=safety,
            applied_demand_tolerance=self._dynamics.applied_demand_tolerance,
            conditions_sha256=state.conditions_sha256,
            usable_for_comparison=usable,
            learned_controller_available=self.learned_controller_available,
            promotable=(
                usable
                and not safety.violated
                and safety.invalid_actions == 0
                and every_step_is_backed
                and self.learned_controller_available
            ),
        )

    def run_episode(self, policy: SupervisorPolicy, spec: EpisodeSpec) -> EpisodeResult:
        """1つの policy で episode を最後まで回す。

        `RulePolicy` も `RLPolicy` も同じ `SupervisorPolicy` 契約なので、**同じ episode 群で
        そのまま比べられる**（#105 受入基準 / #88 の interface）。
        """
        self.reset(spec, policy=policy.kind, policy_version=policy.version)
        state = self._require_episode()
        while not state.ended:
            observation = self.observation()
            self.step(SupervisorAction.from_output(policy.propose(observation)))
        return self.episode_result()

    def run_policy(self, policy: SupervisorPolicy, specs: Sequence[EpisodeSpec]) -> PolicyArm:
        """同じ episode 群を1つの policy で回し、比較用の arm を作る。"""
        if not specs:
            raise EnvironmentUsageError("episode の無い arm は作らない")
        return PolicyArm(
            policy=policy.kind,
            policy_version=policy.version,
            episodes=tuple(self.run_episode(policy, spec) for spec in specs),
        )

    @staticmethod
    def compare(arms: Sequence[PolicyArm]) -> PolicyComparison:
        """同じ条件で回した arm を並べる。**条件が違えば受け取らない。**"""
        if len(arms) < 2:
            raise EnvironmentUsageError("比較には2つ以上の arm が要る")
        expected = arms[0].conditions
        if any(arm.conditions != expected for arm in arms):
            raise EnvironmentUsageError("条件 hash の違う arm を同じ比較に入れない")
        comparable = arms[0].comparable_episode_ids
        if any(arm.comparable_episode_ids != comparable for arm in arms):
            # arm ごとに落ちた episode を捨てると、違う母集団の平均を並べることになる。
            raise EnvironmentUsageError("arm ごとに比較できる episode 群が違う")
        return PolicyComparison(conditions_sha256=conditions_digest(expected), arms=tuple(arms))

    # ------------------------------------------------------------------ 1 step の中身

    def _step(
        self,
        state: _EpisodeState,
        snapshot: ControlStateSnapshot,
        action: SupervisorAction,
    ) -> StepRecord:
        try:
            self._action_space.validate(action)
        except InvalidSupervisorActionError as error:
            # **丸めない。** 範囲外の action は terminal / invalid として扱う。
            state.invalid_actions += 1
            self._end(state, TerminationReason.INVALID_ACTION, "action_out_of_bounds", str(error))
            return self._unsupported_record(
                state, action, snapshot, "action_out_of_bounds", str(error)
            )

        workload = self._require_workload(state)
        if workload is None:
            self._end(
                state,
                TerminationReason.TRAJECTORY_EXHAUSTED,
                "workload_trace_exhausted",
                "workload trace が尽きた",
            )
            return self._unsupported_record(
                state, action, snapshot, "workload_trace_exhausted", "workload trace が尽きた"
            )

        requested, proposal_facts = self._requested_demand(state, snapshot, action, workload)
        if requested is None:
            self._end(
                state,
                TerminationReason.CONTROLLER_UNUSABLE,
                "controller_unusable",
                proposal_facts.detail,
            )
            return self._unsupported_record(
                state, action, snapshot, "controller_unusable", proposal_facts.detail
            )

        shortfalls = tuple(
            zone for zone in Zone if requested.get(zone) < self._floor.get(zone) - DEMAND_EPSILON
        )
        if shortfalls:
            # 記録された Critical Safety floor を下回った要求（決定記録 0054 §2.4 と同じ数え方）。
            # 運転時は #78 が引き上げるが、**環境はそれを模さず探索を止める。**
            state.floor_shortfalls += len(shortfalls)
            detail = "; ".join(
                f"{zone.value}: requested={requested.get(zone):.6f}; "
                f"floor={self._floor.get(zone):.6f}"
                for zone in shortfalls
            )
            self._end(state, TerminationReason.SAFETY_VIOLATION, "safety_floor_shortfall", detail)
            return self._unsupported_record(
                state,
                action,
                snapshot,
                "safety_floor_shortfall",
                detail,
                requested=requested,
                facts=proposal_facts,
                safety=SafetyLedgerEntry(
                    ceiling_exceedances=0, floor_shortfalls=len(shortfalls), margin_c=None
                ),
            )

        try:
            outcome = self._dynamics.advance(
                DynamicsRequest(
                    window=state.window,
                    applied=requested,
                    workload=workload,
                    step_ms=self._config.episode.step_ms.value,
                    step_index=state.step_index,
                ),
                rng=state.rng,
            )
        except (DynamicsUnusableError, ValueError) as error:
            detail = f"{type(error).__name__}: {error}"
            self._end(state, TerminationReason.DYNAMICS_UNUSABLE, "dynamics_unusable", detail)
            return self._unsupported_record(
                state,
                action,
                snapshot,
                "dynamics_unusable",
                detail,
                requested=requested,
                facts=proposal_facts,
            )

        if outcome.provenance not in self._dynamics.provenances:
            # **step ごとの provenance も自称である。** 契約した出どころの外を受け取らない。
            detail = (
                f"provenance={outcome.provenance.value}; "
                f"allowed={sorted(item.value for item in self._dynamics.provenances)}"
            )
            self._end(state, TerminationReason.DYNAMICS_UNUSABLE, "provenance_unexpected", detail)
            return self._unsupported_record(
                state,
                action,
                snapshot,
                "provenance_unexpected",
                detail,
                requested=requested,
                facts=proposal_facts,
            )

        if not outcome.supported:
            assert outcome.reason is not None
            # 記録に無い action の結果は作らない（決定記録 0053 §2.3 / 0054 §2.2）。
            self._end(
                state,
                TerminationReason.UNSUPPORTED_ACTION,
                outcome.reason.code,
                outcome.reason.detail,
            )
            return self._unsupported_record(
                state,
                action,
                snapshot,
                outcome.reason.code,
                outcome.reason.detail,
                requested=requested,
                facts=proposal_facts,
            )

        assert outcome.window is not None
        # **観測の出どころは window ひとつ。** 使える cell だけを読む（mask を無視しない）。
        observed = _frame_values(outcome.window)
        ceiling = self._safety.absolute_temp_ceiling_c.value
        exceedances, margin = self._screen_temperatures(observed)
        if margin is not None:
            state.minimum_margin_c = (
                margin if state.minimum_margin_c is None else min(state.minimum_margin_c, margin)
            )
        safety_entry = SafetyLedgerEntry(
            ceiling_exceedances=exceedances, floor_shortfalls=0, margin_c=margin
        )
        # **実際に掛かっていた action は、観測 window の action から取る。** 記録再生では
        # 許容幅の分だけ要求と違いうるので、要求をそのまま次の state にすると「掛かっている
        # action」が2つになる（決定記録 0052 §2.4 / 0058 §2.2）。
        applied = _window_demands(outcome.window)
        try:
            reward = self._reward.evaluate(values=observed, demands=applied, previous=state.applied)
        except RewardUnusableError as error:
            detail = f"{type(error).__name__}: {error}"
            self._end(state, TerminationReason.DYNAMICS_UNUSABLE, "reward_unusable", detail)
            return self._unsupported_record(
                state,
                action,
                snapshot,
                "reward_unusable",
                detail,
                requested=requested,
                facts=proposal_facts,
            )

        record = StepRecord(
            step_index=state.step_index,
            tick_id=state.tick_id,
            ts_ms=snapshot.ts_ms,
            action=action,
            regime=workload.regime,
            requested=requested,
            applied=applied,
            active_controller=proposal_facts.controller,
            fallback_reason=proposal_facts.fallback_reason,
            optimizer_status=proposal_facts.optimizer_status,
            confidence=proposal_facts.confidence,
            ood=proposal_facts.ood,
            provenance=outcome.provenance,
            supported=True,
            observed=dict(observed),
            reward=reward,
            safety=safety_entry,
        )
        state.window = outcome.window
        state.applied = applied
        state.tick_id += 1
        if exceedances:
            state.ceiling_exceedances += exceedances
            detail = f"ceiling={ceiling:.3f}; exceedances={exceedances}"
            self._end(
                state, TerminationReason.SAFETY_VIOLATION, "absolute_ceiling_exceeded", detail
            )
        return record

    # ------------------------------------------------------------------ 補助

    def _requested_demand(
        self,
        state: _EpisodeState,
        snapshot: ControlStateSnapshot,
        action: SupervisorAction,
        workload: WorkloadSample,
    ) -> tuple[PerZone[Demand] | None, _ProposalFacts]:
        """action を MPC と Gate に通して requested を得る。**環境は demand を作らない。**"""
        assert self._gate is not None and self._baseline is not None
        ood: bool | None = None
        try:
            baseline = self._baseline.propose(snapshot)
            if baseline.controller is not ControllerKind.FALLBACK:
                raise EnvironmentUsageError("baseline には Fallback の提案を渡す")
            if self._mpc is None:
                # **偽の attestation を作らない。** 束縛できなかった事実を、運転時と同じ
                # `LearnedFailure.MODEL_LOAD_FAILURE` として Gate へ渡し、Fallback で回す
                # （決定記録 0052 §2.1 / 0058 §2.1）。
                assert self._mpc_unavailable is not None
                learned = LearnedControlStatus(
                    failure=LearnedFailure.MODEL_LOAD_FAILURE,
                    failure_reason=self._mpc_unavailable,
                )
            else:
                supervisor = action.to_output(
                    snapshot_schema_version=snapshot.schema_version,
                    tick_id=snapshot.tick_id,
                    ts_ms=snapshot.ts_ms,
                    regime=workload.regime,
                    regime_confidence=workload.regime_confidence,
                    policy=self._policy_kind,
                    version=self._policy_version,
                )
                proposal = self._mpc.propose(
                    snapshot=snapshot,
                    observed=state.window,
                    supervisor=supervisor,
                    baseline=baseline,
                    safety_floor=self._floor,
                )
                ood = proposal.assessment.ood if proposal.assessment is not None else None
                learned = proposal.to_status(received_at_mono_ms=snapshot.monotonic_ms)
            selection = self._gate.select(
                now_mono_ms=snapshot.monotonic_ms,
                fallback=baseline,
                learned=learned,
                operating_mode=OperatingMode.AUTO,
                safety_state=SafetyState.NORMAL,
            )
        except EnvironmentUsageError:
            raise
        except Exception as error:
            # worker 境界（0052 §2.5）と同じく、種類で選ばず構造化した失敗へ翻訳する。
            return None, _ProposalFacts(
                controller=ControllerKind.FALLBACK,
                detail=f"{type(error).__name__}: {error}"[:500],
            )
        chosen = selection.proposal
        return (
            PerZone[Demand](
                front=chosen.requested.front.demand,
                rear=chosen.requested.rear.demand,
                top=chosen.requested.top.demand,
            ),
            _ProposalFacts(
                controller=chosen.controller,
                fallback_reason=selection.fallback_reason,
                optimizer_status=chosen.optimizer_status,
                confidence=None
                if selection.model_gate is None
                else selection.model_gate.confidence_level,
                ood=ood,
                detail="",
            ),
        )

    def _snapshot(self, state: _EpisodeState) -> ControlStateSnapshot:
        """いまの window から Controller が読む Snapshot を作る。

        時刻は window の action 時刻をそのまま使う（**壁時計を読まない**）。
        単調時計にも同じ値を使うので、同じ episode を何度回しても同じ列になる。
        """
        frame = state.window.window[-1]
        ts_ms = state.window.action_ts_ms
        signals = tuple(_snapshot_signal(metric, frame, ts_ms=ts_ms) for metric in frame.values)
        return ControlStateSnapshot(
            tick_id=state.tick_id,
            ts_ms=ts_ms,
            monotonic_ms=ts_ms,
            signals=signals,
            derived=(),
            trends=(),
            telemetry_health=TelemetryHealth.NORMAL,
            critical_unavailable=(),
            fans=PerZone[FanState](
                front=FanState(effective_demand=state.applied.front),
                rear=FanState(effective_demand=state.applied.rear),
                top=FanState(effective_demand=state.applied.top),
            ),
        )

    def _workload_estimate(
        self, state: _EpisodeState, snapshot: ControlStateSnapshot
    ) -> WorkloadRegimeEstimate:
        """trace の擾乱を #87 の推定結果と同じ形で渡す。

        **環境が regime を推定し直さない。** trace が持っているのは「この episode で観測される
        負荷」そのもので、推定器（#87）の検証は #87 の試験の仕事である。
        """
        sample = state.spec.trace.at(state.step_index) or state.spec.trace.samples[-1]
        return WorkloadRegimeEstimate(
            regime=sample.regime,
            confidence=sample.regime_confidence,
            reason=RegimeReason.OBSERVED_HISTORY,
            as_of_tick_id=snapshot.tick_id,
            computed_at_ms=snapshot.ts_ms,
            evidence=RegimeEvidence(
                cpu_power_mean_w=sample.cpu_power_w,
                gpu_power_mean_w=sample.gpu_power_w,
                observed_window_ms=state.step_index * self._config.episode.step_ms.value,
            ),
        )

    @staticmethod
    def _require_workload(state: _EpisodeState) -> WorkloadSample | None:
        return state.spec.trace.at(state.step_index)

    def _require_episode(self) -> _EpisodeState:
        if self._episode is None:
            raise EnvironmentUsageError("reset していない環境を使わない")
        return self._episode

    @staticmethod
    def _end(state: _EpisodeState, termination: TerminationReason, code: str, detail: str) -> None:
        if state.ended:
            return
        state.ended = True
        state.termination = termination
        state.termination_reason = Reason(code=code, detail=detail[:500])

    def _unsupported_record(
        self,
        state: _EpisodeState,
        action: SupervisorAction,
        snapshot: ControlStateSnapshot,
        code: str,
        detail: str,
        *,
        requested: PerZone[Demand] | None = None,
        facts: _ProposalFacts | None = None,
        safety: SafetyLedgerEntry | None = None,
    ) -> StepRecord:
        """採点できなかった step。**観測も reward も作らない。**"""
        sample = state.spec.trace.at(state.step_index) or state.spec.trace.samples[-1]
        return StepRecord(
            step_index=state.step_index,
            tick_id=state.tick_id,
            ts_ms=snapshot.ts_ms,
            action=action,
            regime=sample.regime,
            requested=requested if requested is not None else state.applied,
            active_controller=ControllerKind.FALLBACK if facts is None else facts.controller,
            fallback_reason=None if facts is None else facts.fallback_reason,
            optimizer_status=None if facts is None else facts.optimizer_status,
            confidence=None if facts is None else facts.confidence,
            ood=None if facts is None else facts.ood,
            supported=False,
            unsupported_reason=Reason(code=code, detail=detail[:500]),
            safety=safety
            or SafetyLedgerEntry(ceiling_exceedances=0, floor_shortfalls=0, margin_c=None),
        )

    @staticmethod
    def _coverage(state: _EpisodeState) -> EpisodeCoverage:
        unsupported: dict[str, int] = {}
        for step in state.steps:
            if step.supported:
                continue
            assert step.unsupported_reason is not None
            code = step.unsupported_reason.code
            unsupported[code] = unsupported.get(code, 0) + 1
        return EpisodeCoverage(
            steps=len(state.steps),
            supported_steps=sum(1 for step in state.steps if step.supported),
            unsupported=unsupported,
        )

    def _is_usable(self, coverage: EpisodeCoverage) -> bool:
        """coverage の下限を満たしたか。**満たさない episode は比較に使わない**（fail closed）。"""
        limits = self._config.coverage
        if coverage.supported_steps < limits.minimum_steps.value:
            return False
        fraction = coverage.supported_fraction
        if fraction is None:
            return False
        return fraction >= limits.minimum_supported_fraction.value

    def _conditions_sha256(self, spec: EpisodeSpec, max_steps: int) -> str:
        """policy 以外の条件をすべて覆う hash（決定記録 0054 §2.7 と同じ考え方）。

        **policy を入れない。** Rule と RL を同じ条件で比べる鍵にするためである。
        覆えていない条件があると、違う条件の比較が同じ hash を名乗れる。
        """
        payload = {
            "episode_schema_version": EPISODE_SCHEMA_VERSION,
            "reward_schema_version": REWARD_SCHEMA_VERSION,
            "rl_training_config_sha256": self._config_sha256,
            # 渡された hash が中身と食い違っていても、**検証済み設定そのものの hash** で
            # 条件が割れる（呼び出し側の写しだけを信じない）。
            "rl_training_config_digest": _config_digest(self._config),
            "reward_version": self._config.reward.version,
            "discount": self._config.reward.discount.value,
            "episode_id": spec.episode_id,
            "seed": spec.seed,
            "mode": self._dynamics.mode.value,
            "max_steps": max_steps,
            "step_ms": self._config.episode.step_ms.value,
            "recent_history_steps": self._config.episode.recent_history_steps,
            # **identity だけでは足りない。** 照合の許容幅や hybrid の内側は identity に
            # 現れないのに結果を変える（決定記録 0058 §2.6）。
            "dynamics": self._dynamics.conditions(),
            "workload_trace": spec.trace.digest(),
            "initial_window": canonical_sha256(spec.initial_window),
            "initial_demands": spec.initial_demands.model_dump(mode="json"),
            # 制御器の設定は**設定全体の hash で覆う**。mpc.optimizer・authority_limits・
            # gate 閾値・復帰 hold・shadow の許容幅まで、欄を数え落とさずに入る。
            "fan_policy_sha256": _config_digest(self._policy),
            "safety_sha256": _config_digest(self._safety),
            "expected_model_version": self._expected_model_version,
            "learned_controller_available": self.learned_controller_available,
            # **期待する版だけでは足りない。** 同じ版を名乗る別の artifact、別の Confidence
            # Profile、別の任意依存（#94 / #81）は、同じ入力から違う提案を作る。
            "controller": None if self._mpc is None else self._mpc.conditions(),
            "mpc_unavailable": (
                None
                if self._mpc_unavailable is None
                else self._mpc_unavailable.model_dump(mode="json")
            ),
            # 注入した依存は呼び出し側が名指しする。名前が無い依存は受け取らない。
            "baseline": self._baseline_identity.model_dump(mode="json"),
            "acoustic": (
                None
                if self._acoustic_identity is None
                else self._acoustic_identity.model_dump(mode="json")
            ),
            "safety_model": self.safety_model.value,
            "safety_screen": list(self._config.safety_screen.temperature_metrics),
            "coverage": self._config.coverage.model_dump(mode="json"),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()
