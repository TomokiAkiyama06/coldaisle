"""3系統 Fan Control Engine の1 tick。#74

決定記録 0027 の Pipeline と 0028 の層間契約を、**1 tick として閉じる**層である。

```text
Telemetry → State Estimator(#102) → Supervisor(#88) → Controller(#79/#86)
  → Confidence / OOD Gate(#85) → Reactive Guard(#80) → Critical Safety(#78)
  → 合成 → Fan Hardware Backend(#77) → decision trace(#82)
```

この module が持つのは**順序・期限・例外の翻訳**だけで、閾値も demand の計算も持たない。

- 内部単位は `demand = 0.0..1.0`。PWM はここに現れない（作れるのは #77 だけ）
- `requested` を作れるのは制御器と人の操作まで。Guard と Safety は迂回できない
- 期限・hold・stall・overrun は**この loop 自身の単調時計**で数え、壁時計は記録の時刻だけに使う
- 1 tick が使う `ControlStateSnapshot` は**1つだけ**で、層をまたいで同じ object を渡す
- Controller / Guard / Gate の例外は握りつぶさず `Fault` へ翻訳する。
  **Critical Safety と合成の例外は捕まえない**（0028 §2.7。プロセスを終わらせ、引き継ぎで Max）

Safety の裁定は requested を見ないので、**Gate より先に評価する**。こうすると Gate は
「この tick の `safety_state`」を見て選べる（0028 §2.5 (c) の条件）。合成はそのあとで、
requested・Guard・Safety の3つを 0028 §2.4 の優先順で1回だけ束ねる。

Air Balance の協調（決定記録 0078 §2.2）は Safety の評価の後・Gate の前に置き、raw baseline
（Fallback の requested）を上げるだけの coordinated baseline を Gate へ渡す。合成の後に値を
足す経路は持たない。`mode: apply` の協調が失敗した tick は Gate を迂回して raw baseline を
requested にする（決定記録 0085）。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle import logs
from coldaisle.clock import Clock, MonotonicClock
from coldaisle.control.air_balance import ConfiguredAirBalanceModel
from coldaisle.control.air_balance_coordination import (
    AirBalanceCoordinator,
    CoordinatorResult,
    ProjectedFloors,
    project_floors,
)
from coldaisle.control.air_balance_trace import AirBalanceRecorder
from coldaisle.control.config import ControlConfig, FanPolicyConfig
from coldaisle.control.fallback.controller import FallbackController
from coldaisle.control.fallback.gate import (
    ControllerGate,
    ControllerSelection,
    LearnedControlStatus,
    SnapshotStatus,
)
from coldaisle.control.hardware.simulated import FanHardwareBackend, FanHardwareResult
from coldaisle.control.learned_handoff import (
    AvailableCalibration,
    LearnedChannelHealth,
    LearnedChannelState,
    LearnedExpectedArtifacts,
    LearnedFrame,
    LearnedFrameSink,
    LearnedRole,
    UnavailableCalibration,
)
from coldaisle.control.logging import ControlTraceLogger
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.operating_mode import AdminAuthorityCommand, AdminModeTracker
from coldaisle.control.operating_mode import ModeCommand as ModeCommand  # 既存の import 先を保つ
from coldaisle.control.reactive.guard import (
    ReactiveGuard,
    ReactiveGuardDecision,
    guard_input_metrics,
)
from coldaisle.control.safety.critical import (
    AIR_TELEMETRY_GROUP,
    AIR_TEMPERATURE_METRICS,
    CPU_POWER_METRIC,
    CPU_TEMPERATURE_METRIC,
    FAN_FAULT_CODES,
    GPU_TEMPERATURE_METRIC,
    ComposedDemands,
    CriticalSafety,
    CriticalSafetyDecision,
    DemandComposer,
)
from coldaisle.control.schema import (
    AIR_BALANCE_COORDINATION_FAILED,
    AIR_BALANCE_RELEASE_HOLD_REASON,
    AIR_BALANCE_ZONE_REASONS,
    BASELINE_STAGE,
    CONTROL_TICK_RUNTIME_SCHEMA_VERSION,
    SCHEMA_VERSION,
    AirBalanceCoordinationFailure,
    AirBalanceCoordinationMode,
    AirBalanceCoordinationReasonCode,
    AirBalanceCoordinationRecord,
    AirBalanceCoordinationSkipReason,
    AirBalanceCoordinationStatus,
    AirBalanceRecord,
    AirBalanceTraceState,
    AuthorityRecord,
    AuthorityStage,
    ConfidenceLevel,
    ControllerKind,
    ControllerProposal,
    ControlState,
    ControlTick,
    ControlTickRuntime,
    Demand,
    EffectiveZoneDemand,
    Fault,
    FaultCode,
    GuardZoneOutput,
    ModeCommandRecord,
    OperatingMode,
    PerZone,
    Reason,
    RegistryProvenance,
    SafetyProvenance,
    SafetyState,
    ShadowRecord,
    SupervisorDecision,
    SupervisorOutput,
    Zone,
    ZoneRecord,
    ZoneRequest,
    lowest_stage,
    stage_rank,
)
from coldaisle.control.shadow.record import ShadowRecorder
from coldaisle.control.state import (
    ControlInputContract,
    ControlInputFrame,
    ControlStateEstimator,
    ControlStateSnapshot,
    CriticalTelemetryGroup,
    FanState,
    SignalSpec,
    TelemetryImportance,
    TelemetryReading,
)
from coldaisle.control.supervisor.policy import (
    DeliveredSupervisorOutput,
    ReceivedSupervisorOutput,
    SupervisorCoordinator,
    SupervisorInput,
)
from coldaisle.control.supervisor.regime import (
    WorkloadRegimeEstimate,
    WorkloadRegimeEstimator,
    WorkloadRegimeState,
)
from coldaisle.measurement import Quality, validate_metric
from coldaisle.metrics import MetricCatalog

LOGGER = logging.getLogger("coldaisle.control")

DERIVED_PREFIX = "d."
"""派生値の名前の接頭辞（決定記録 0002 §2.2）。命名規約であり調整値ではない。"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TelemetrySample(_Frozen):
    """収集層から受け取る1 metric の生の値。**経過時間を持たない。**

    供給側が測った `age_ms` を受け取らない。供給側の壁時計で測った経過を信じると、時刻合わせで
    飛んだときに期限切れの値が fresh として入る。新しさは loop が `source_ts_ms` の変化を
    自分の単調時計で観測して決める（決定記録 0028 §2.6）。
    """

    metric: str
    value: float | None = Field(default=None, allow_inf_nan=False)
    quality: Quality
    source_ts_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _has_a_stored_metric_name(self) -> Self:
        validate_metric(self.metric)
        return self


class TelemetrySource(Protocol):
    """制御 loop が読む、**読み取り専用**の Telemetry 入口（決定記録 0060 §2.2）。

    実装はシリアルを開かず、NVML も呼ばない（AGENTS.md ルール6 / 決定記録 0001 D-07）。
    """

    def read(self) -> tuple[TelemetrySample, ...]:
        """いま手に入る最新の値。**待たない。** 失敗は例外で返してよい。"""
        ...


class OperatingModeSource(Protocol):
    """運転モードの読み取り専用の窓口。**LLM からは到達できない**（AGENTS.md ルール1）。"""

    def current(self) -> ModeCommand:
        """いまのモード。管理ソケットの入口は `AdminModeTracker`（決定記録 0072）が別に持つ。

        **待たない。** この呼び出しは Critical Safety より前にあるので、ここで待つと
        安全側の裁定ごと止まる。手元に無ければ直前の値を返し、失敗は例外で返す。
        """
        ...


class StaticOperatingMode:
    """常に同じモードを返す source。**既定は `AUTO`。**

    再起動で `AUTO` に戻る規則（0028 §2.5 (a)）は、この既定と
    「loop がモードを永続化しない」ことで満たす。
    """

    __slots__ = ("_command",)

    def __init__(self, command: ModeCommand | None = None) -> None:
        self._command = command if command is not None else ModeCommand()

    def current(self) -> ModeCommand:
        """設定されたモードを返す。"""
        return self._command


class LearnedProposalSource(Protocol):
    """worker（別プロセス）が置いた最新の Learned MPC 結果を読むだけの窓口（0028 §2.2）。"""

    def poll(self) -> MpcProposal | None:
        """最新の worker 結果。**同じ結果を何度返してもよい**（loop が新旧を見分ける）。"""
        ...


class SupervisorOutputSource(Protocol):
    """worker が置いた最新の RL Supervisor 出力を読むだけの窓口（決定記録 0077 §2.8）。

    返すのは出力と、それを作った artifact の識別（`SupervisorPolicyIdentity`）。識別は loop が
    `ReceivedSupervisorOutput.identity` へ写し、Coordinator が `expected_rl_identity` と照合する。
    **束縛の用途（`origin`）は運ばない。** loop は `unverified` のままにする（0061 §2.4）。
    """

    def poll(self) -> DeliveredSupervisorOutput | None:
        """最新の RL 出力。**同じ出力を何度返してもよい。** **待たない。**"""
        ...


class DemotionReport(Protocol):
    """#92 が返す降格の結果のうち、loop が記録に残す面。"""

    @property
    def to_stage(self) -> AuthorityStage: ...

    @property
    def persisted(self) -> bool: ...

    @property
    def persist_failure(self) -> Reason | None: ...


class AuthorityObserver(Protocol):
    """#92 の `AuthorityRuntime` のうち、loop が使う読み取りと観測だけの面。

    **上げる経路を持たない。** 昇格は人の承認だけが行う（0028 §2.5 (b)）。

    `observe` は**待たない**。降格は memory 上で先に効かせ、書き残しの I/O に上限を
    掛ける（決定記録 0060 §2.7）。待ち続ける実装を渡すと、2つの heartbeat のあいだに
    その待ちが丸ごと入る。
    """

    def current_stage(self) -> AuthorityStage:
        """この tick に与えてよい制御権。"""
        ...

    def observe(
        self,
        *,
        safety_state: SafetyState,
        demotion_recommended: bool,
        confidence_level: ConfidenceLevel | None,
        ood: bool | None,
        now_mono_ms: int,
    ) -> DemotionReport | None:
        """この tick の健全性を渡す。下げたときだけ結果を返す。"""
        ...

    def apply_lowering(self, *, to_stage: AuthorityStage, actor: str, reason: str) -> bool:
        """人の降格（管理ソケット）を**memory 上で先に**入れる。待たない（決定記録 0072 §2.6）。

        返すのは実効 stage が下がったか。下がらなくても上限は残る。**上げる向きは無い。**
        """
        ...

    def maintain(self) -> None:
        """heartbeat の後に1回。予約した降格の書き残しと journal の変化の検知（0072 §2.6）。

        待つのは lock の待ち上限まで。例外を外へ出さない。効くのは次の tick から。
        """
        ...

    def trace_record(self, *, command_id: int | None) -> AuthorityRecord:
        """decision trace（v13）へ残す、この時点の制御権の出どころ。"""
        ...


class StaticAuthority:
    """journal を持たない構成に使う、**固定 stage の** authority（既定は `SHADOW`）。

    **設定の `authority_stage` を既定にしない。** 配線を忘れた起動が設定の**上限**を
    そのまま制御権にすると、`full` と書かれた設定だけで Learned MPC が実 Fan を握る
    （#79 `ControllerGate.__init__` と同じ理由）。降格の記録は持たないので `observe` は
    何もしない。昇格の経路はどこにも無い。

    人の降格（管理ソケット）は memory 上の上限として受ける（書き残す journal が無いので、
    再起動で戻る）。**受けた降格を黙って捨てない。**
    """

    __slots__ = ("_ceiling", "_config_ceiling", "_stage")

    def __init__(
        self,
        stage: AuthorityStage = BASELINE_STAGE,
        *,
        config_ceiling: AuthorityStage | None = None,
    ) -> None:
        self._stage = stage
        self._ceiling = AuthorityStage.FULL
        # trace に残す設定の上限。渡されなければ固定 stage と同じ（それ以上は与えない）
        self._config_ceiling = stage if config_ceiling is None else config_ceiling

    def current_stage(self) -> AuthorityStage:
        """固定された stage（人が下げていればその上限）を返す。"""
        return lowest_stage(self._stage, self._ceiling)

    def apply_lowering(self, *, to_stage: AuthorityStage, actor: str, reason: str) -> bool:
        """memory 上の上限だけを入れる（journal は無い）。"""
        before = self.current_stage()
        self._ceiling = lowest_stage(self._ceiling, to_stage)
        return stage_rank(self.current_stage()) < stage_rank(before)

    def maintain(self) -> None:
        """journal を持たないので何もしない。"""

    def trace_record(self, *, command_id: int | None) -> AuthorityRecord:
        """journal を持たない構成の記録（`entry="static"`）。"""
        return AuthorityRecord(
            entry="static",
            config_ceiling=self._config_ceiling,
            unpersisted_ceiling=None if self._ceiling is AuthorityStage.FULL else self._ceiling,
            command_id=command_id,
        )

    def observe(
        self,
        *,
        safety_state: SafetyState,
        demotion_recommended: bool,
        confidence_level: ConfidenceLevel | None,
        ood: bool | None,
        now_mono_ms: int,
    ) -> DemotionReport | None:
        """記録を持たないので何もしない。"""
        return None


class Watchdog(Protocol):
    """書き込みと検証まで終えた tick を外部の deadman へ通知する（0028 §2.6）。"""

    def notify(self) -> None:
        """1 tick を完了したことを知らせる。"""
        ...


class NullWatchdog:
    """deadman を持たない構成（試験・手元実行）用の、何もしない watchdog。"""

    def notify(self) -> None:
        """何もしない。"""


class ControlLoopBusyError(RuntimeError):
    """tick の中でもう1つの tick を始めようとした（0028 §2.6「重ねない」）。"""


@dataclass(frozen=True, slots=True)
class ControlTickResult:
    """1 tick の結果。制御の値そのものは `tick` の中にある。"""

    tick: ControlTick
    duration_ms: int
    deadline_exceeded: bool
    recorded: bool
    trace_failed: bool
    """decision trace を保存できなかったか。**落ちた記録を黙って捨てない。**"""
    hardware: PerZone[FanHardwareResult] | None


def build_input_contract(
    config: ControlConfig,
    catalog: MetricCatalog,
    *,
    t_sensor_metric: str | None = None,
) -> ControlInputContract:
    """検証済み設定から、この構成が読む入力の契約を組み立てる。

    **表を手で持たない。** Guard・Fallback・Workload Regime・MPC が設定で指している metric を
    数え直して契約にする。手書きの表にすると、設定へ metric を足したときに契約だけが古いまま
    残り、その入力が「欠測」として静かに無視される。

    許容遅延は `safety.yaml` の源ごとの値から決める。決められない metric は**起動時に拒否する**
    （既定値を置くと、未知の入力が黙って甘い期限で通る）。
    """
    telemetry = config.safety.telemetry
    specs: dict[str, SignalSpec] = {}

    def add(metric: str, importance: TelemetryImportance, stale_after_ms: int) -> None:
        existing = specs.get(metric)
        if existing is not None:
            if existing.importance is importance and existing.stale_after_ms == stale_after_ms:
                return
            raise ValueError(
                "同じ制御入力に別の重要度・許容遅延を割り当てられない: "
                f"{metric}（{existing.importance.value}/{existing.stale_after_ms} と "
                f"{importance.value}/{stale_after_ms}）"
            )
        specs[metric] = SignalSpec(
            metric=metric, importance=importance, stale_after_ms=stale_after_ms
        )

    add(CPU_TEMPERATURE_METRIC, TelemetryImportance.CRITICAL, telemetry.cpu_ms.value)
    add(GPU_TEMPERATURE_METRIC, TelemetryImportance.CRITICAL, telemetry.gpu_ms.value)
    if telemetry.t_sensor.enabled.value:
        if t_sensor_metric is None or telemetry.t_sensor.stale_after_ms is None:
            raise ValueError("T_SENSOR を有効にするなら承認済みの metric と許容遅延を渡す")
        add(t_sensor_metric, TelemetryImportance.CRITICAL, telemetry.t_sensor.stale_after_ms.value)
    elif t_sensor_metric is not None:
        raise ValueError("T_SENSOR が無効のときは metric を渡さない")
    add(CPU_POWER_METRIC, TelemetryImportance.DEGRADED, telemetry.cpu_power_ms.value)
    air_metrics = tuple(sorted(AIR_TEMPERATURE_METRICS))
    for metric in air_metrics:
        add(metric, TelemetryImportance.DEGRADED, telemetry.air_ms.value)

    for metric in sorted(_policy_input_metrics(config.policy, catalog)):
        if metric not in specs:
            add(metric, TelemetryImportance.ADVISORY, _advisory_stale_ms(metric, config))
    # **`air-balance.yaml` の熱の入力も契約へ加える**（決定記録 0073 §2.4）。契約に無い metric は
    # 取り込まれず毎 tick None になり、熱の制約が黙って効かない。`source.status` にかかわらず
    # 加える（無効の間も、校正後に初めて拒否が見つかる事態を作らない）。すでに契約にある
    # metric は、その重要度・許容遅延のまま使う。
    for metric in sorted(
        air_balance_input_metrics(config, catalog, t_sensor_metric=t_sensor_metric)
    ):
        if metric not in specs:
            add(metric, TelemetryImportance.ADVISORY, _advisory_stale_ms(metric, config))

    return ControlInputContract(
        signals=tuple(specs[metric] for metric in sorted(specs)),
        critical_groups=(CriticalTelemetryGroup(code=AIR_TELEMETRY_GROUP, metrics=air_metrics),),
    )


def air_balance_input_metrics(
    config: ControlConfig,
    catalog: MetricCatalog,
    *,
    t_sensor_metric: str | None = None,
) -> frozenset[str]:
    """`air-balance.yaml` の ``thermal_inputs`` が指す、契約へ加える metric（決定記録 0073 §2.4）。

    各 metric が Metric Catalog にあり単位が ``C`` であること、派生値は材料へ展開できること、
    **契約に新しく加わる** metric の許容遅延を決められることを確かめる。**決められない metric は
    拒否する**（起動時の Control Config の不正として扱う。0073 §2.2）。

    すでに契約にある metric（必須入力と `fan-policy.yaml` の入力）は、その重要度・許容遅延の
    まま使うので、ここでは許容遅延を求めない。求めると、承認済みの T_SENSOR（源の決まらない
    ``board.*`` など）を束縛しただけで、契約では扱える設定を拒否してしまう。
    ``t_sensor_metric`` は `build_input_contract()` に渡すものと同じ値を渡す。
    """
    bindings = config.air_balance.thermal_inputs.metrics()
    for metric in bindings:
        unit = catalog.unit_for(metric)
        if unit != "C":
            raise ValueError(
                f"air-balance.yaml の thermal_inputs は既知の温度(C)にする: "
                f"metric={metric}, unit={unit}"
            )
    resolved = _resolve_derived(frozenset(bindings), catalog)
    contracted = _contracted_metrics(config, catalog, t_sensor_metric=t_sensor_metric)
    for metric in resolved - contracted:
        _advisory_stale_ms(metric, config)
    return resolved


def _contracted_metrics(
    config: ControlConfig,
    catalog: MetricCatalog,
    *,
    t_sensor_metric: str | None,
) -> frozenset[str]:
    """`air-balance.yaml` より前に契約へ入る metric。`build_input_contract()` と同じ順で数える。"""
    metrics: set[str] = {CPU_TEMPERATURE_METRIC, GPU_TEMPERATURE_METRIC, CPU_POWER_METRIC}
    metrics.update(AIR_TEMPERATURE_METRICS)
    if config.safety.telemetry.t_sensor.enabled.value and t_sensor_metric is not None:
        metrics.add(t_sensor_metric)
    metrics.update(_policy_input_metrics(config.policy, catalog))
    return frozenset(metrics)


def _resolve_derived(metrics: frozenset[str], catalog: MetricCatalog) -> frozenset[str]:
    """派生値を材料の metric へ展開する。Catalog に無い名前は拒否する。"""
    resolved: set[str] = set()
    for metric in metrics:
        if not metric.startswith(DERIVED_PREFIX):
            resolved.add(metric)
            continue
        derived = catalog.derived.get(metric)
        if derived is None:
            raise ValueError(f"Metric Catalog に無い派生値を制御入力にしている: {metric}")
        resolved.add(derived.minuend)
        resolved.add(derived.subtrahend)
    unknown = sorted(metric for metric in resolved if catalog.unit_for(metric) is None)
    if unknown:
        raise ValueError(f"Metric Catalog に無い制御入力がある: {unknown}")
    return frozenset(resolved)


def _policy_input_metrics(policy: FanPolicyConfig, catalog: MetricCatalog) -> frozenset[str]:
    """`fan-policy.yaml` が指している metric（派生値はその材料へ展開する）。"""
    metrics: set[str] = set()
    for zone in Zone:
        metrics.update(policy.fallback_temperature_inputs.get(zone).metrics)
        if policy.fallback_power_feedforward is not None:
            metrics.add(policy.fallback_power_feedforward.get(zone).metric)
    metrics.add(policy.workload_regime.cpu_power.metric)
    metrics.add(policy.workload_regime.gpu_power.metric)
    metrics.add(policy.mpc.optimizer.cost_metrics.cpu_temperature)
    metrics.add(policy.mpc.optimizer.cost_metrics.gpu_temperature)
    metrics.update(guard_input_metrics(policy.reactive_guard))
    return _resolve_derived(frozenset(metrics), catalog)


def _advisory_stale_ms(metric: str, config: ControlConfig) -> int:
    """任意入力の許容遅延を、同じ源の必須入力と揃える。決められなければ拒否する。"""
    telemetry = config.safety.telemetry
    domain, _, _ = metric.partition(".")
    if domain == "air":
        return telemetry.air_ms.value
    if domain == "power":
        if metric.startswith("power.cpu"):
            return telemetry.cpu_power_ms.value
        return telemetry.gpu_ms.value
    if domain == "cpu":
        return telemetry.cpu_ms.value
    if domain == "gpu":
        return telemetry.gpu_ms.value
    raise ValueError(f"制御入力の許容遅延を決められない metric がある: {metric}")


@dataclass(frozen=True, slots=True)
class _SnapshotIdentity:
    """loop が出した snapshot を、**この process の中で**一意に指すための記録。

    `tick_id` は再起動で 0 に戻るので、それだけで照合すると、前の process の worker 出力が
    新しい process の無関係な snapshot に結び付く（決定記録 0060 §2.6）。
    """

    tick_id: int
    ts_ms: int
    schema_version: int
    monotonic_ms: int


class _TelemetryAgeTracker:
    """metric ごとに「`source_ts_ms` が変わったことを観測した単調時計の時刻」を持つ。

    **現在時刻で塗り替えない。** 値が更新されていない metric に現在時刻を押すと、供給が
    止まっていても永久に fresh に見える（決定記録 0028 §2.6）。
    """

    __slots__ = ("_observed",)

    def __init__(self) -> None:
        self._observed: dict[str, tuple[int, int]] = {}

    def observe(self, sample: TelemetrySample, now_mono_ms: int) -> TelemetryReading:
        """1 metric の観測を反映し、snapshot へ渡す読み取り値を返す。"""
        previous = self._observed.get(sample.metric)
        if previous is None or previous[0] != sample.source_ts_ms:
            self._observed[sample.metric] = (sample.source_ts_ms, now_mono_ms)
        return TelemetryReading(
            metric=sample.metric,
            value=sample.value,
            quality=sample.quality,
            source_ts_ms=sample.source_ts_ms,
            last_changed_mono_ms=self._observed[sample.metric][1],
        )


@dataclass(frozen=True, slots=True)
class _Coordination:
    """1 tick の Air Balance の協調の結果（決定記録 0078 / 0085）。"""

    record: AirBalanceCoordinationRecord
    gate_baseline: ControllerProposal | None
    """Gate へ渡す Baseline。``mode: apply`` で上げた tick は coordinated baseline、ほかは raw。"""
    bypass_gate: bool
    """``mode: apply`` の協調が失敗した tick。

    Gate を呼ばず raw baseline を requested にする（決定記録 0085）。
    """
    fault: Fault | None
    """``mode: apply`` の失敗を翻訳した ``fallback_exception``。次 tick の Safety へ渡す。"""


class ControlLoop:
    """1 tick を通す合成の起点。**状態は memory 上だけで、再起動で引き継がない。**

    再起動のたびに `STARTUP`（全 zone Max）と `AUTO` から始まるので、`MANUAL` の低い値や
    測定途中の状態が残らない（0028 §2.5 (a)）。
    """

    def __init__(
        self,
        *,
        config: ControlConfig,
        estimator: ControlStateEstimator,
        fallback: FallbackController,
        gate: ControllerGate,
        guard: ReactiveGuard,
        safety: CriticalSafety,
        composer: DemandComposer,
        backend: FanHardwareBackend,
        telemetry: TelemetrySource,
        clock: Clock,
        monotonic: MonotonicClock,
        registry: RegistryProvenance,
        mode_source: OperatingModeSource | None = None,
        admin_mode: AdminModeTracker | None = None,
        supervisor: SupervisorCoordinator | None = None,
        regime: WorkloadRegimeEstimator | None = None,
        learned_source: LearnedProposalSource | None = None,
        rl_supervisor_source: SupervisorOutputSource | None = None,
        learned_sink: LearnedFrameSink | None = None,
        learned_health: LearnedChannelHealth | None = None,
        learned_calibration: AvailableCalibration | UnavailableCalibration | None = None,
        metric_catalog_sha256: str | None = None,
        shadow: ShadowRecorder | None = None,
        trace: ControlTraceLogger | None = None,
        authority: AuthorityObserver | None = None,
        watchdog: Watchdog | None = None,
        air_balance_coordinator: AirBalanceCoordinator | None = None,
    ) -> None:
        """``air_balance_coordinator`` は試験で偽の model を差し込むときだけ渡す。

        渡さなければ ``fan-policy.yaml`` の ``air_balance_coordination.mode`` から作る
        （``off`` なら作らない）。渡すなら設定と同じ ``mode`` でなければ拒む。

        ``learned_sink`` / ``learned_health`` は Learned worker との経路（決定記録 0077 §2.2）。
        ``coldaisle-fand`` は ``learned_health`` を ``learned_source`` と必ず一緒に渡す
        （経路の状態を見ずに提案を読むと、受付スレッドの死や worker の切断の後も古い提案を
        読みうる）。
        ``learned_source`` だけを渡すのは、経路を持たない試験の偽の worker だけである。

        ``learned_calibration`` / ``metric_catalog_sha256`` は frame v3 の欄（決定記録 0101 §2.2 /
        0107 §2.6）。fand が起動時に読んだ値で、``learned_sink`` を渡すなら**両方とも必須**
        （既定値で「読めなかった」や別の catalog を名乗らせない）。
        """
        if learned_sink is not None and (
            learned_calibration is None or metric_catalog_sha256 is None
        ):
            raise ValueError(
                "frame を送るなら起動時の較正と Metric Catalog の SHA-256 も渡す（0101 / 0107）"
            )
        if (supervisor is None) != (regime is None):
            raise ValueError("Supervisor と Workload Regime 推定は一緒に配線する")
        if rl_supervisor_source is not None and supervisor is None:
            raise ValueError("RL Supervisor worker を配線するなら Supervisor も配線する")
        if learned_health is not None and learned_source is None:
            raise ValueError("Learned の経路の状態は提案の口と一緒に配線する（決定記録 0077 §2.2）")
        if mode_source is not None and admin_mode is not None:
            # モードの出どころを1つにする。2つあると、どちらが人の最新の意図か決まらない
            raise ValueError("mode_source と admin_mode は同時に配線しない")
        self._config = config
        self._estimator = estimator
        self._fallback = fallback
        self._gate = gate
        self._guard = guard
        self._safety = safety
        self._composer = composer
        self._backend = backend
        self._telemetry = telemetry
        self._clock = clock
        self._monotonic = monotonic
        self._mode_source: OperatingModeSource = (
            mode_source if mode_source is not None else StaticOperatingMode()
        )
        # **管理ソケットの受け渡し口**（決定記録 0072 §2.2）。None は入口を開いていない構成。
        self._admin_mode = admin_mode
        self._supervisor = supervisor
        self._regime = regime
        self._learned_source = learned_source
        self._rl_supervisor_source = rl_supervisor_source
        self._learned_sink = learned_sink
        self._learned_calibration = learned_calibration
        self._metric_catalog_sha256 = metric_catalog_sha256
        self._learned_health = learned_health
        # **受付スレッドの死は再起動まで覚える**（決定記録 0077 §2.2）。以後は提案を1件も読まない。
        self._learned_channel_dead = False
        self._shadow = shadow
        self._trace = trace
        self._authority: AuthorityObserver = (
            authority if authority is not None else StaticAuthority()
        )
        self._watchdog: Watchdog = watchdog if watchdog is not None else NullWatchdog()
        # **起動時に渡された registry の版をそのまま毎 tick 載せる**（決定記録 0071 §2.5）。
        # loop は registry も過去の trace も読まない。promotion / rollback が trace に現れるのは
        # それを反映して再起動した最初の tick からで、それが「判断がどの artifact で出たか」の
        # 記録として正しい。既定値を置かないのは、渡し忘れが「registry を読んでいない」と
        # 同じ記録になるのを防ぐため（読んでいない構成は `RegistryProvenance.unbound()` を
        # 明示する）。
        self._registry = registry
        # **frame の `expected_artifacts` は trace の `registry` と同じ値から作る**（決定記録 0077
        # §2.6）。別々に渡すと、worker が読む artifact と trace が名乗る版が食い違いうる。
        self._expected_artifacts = LearnedExpectedArtifacts.from_provenance(registry)

        self._ages = _TelemetryAgeTracker()
        self._tick_id = 0
        self._in_tick = False
        self._previous_snapshot: ControlStateSnapshot | None = None
        self._regime_state: WorkloadRegimeState = WorkloadRegimeEstimator.initial_state()
        self._fans: PerZone[FanState] | None = None
        self._pending_faults: tuple[Fault, ...] = ()
        self._previous_overrun = False
        self._mode = ModeCommand()
        self._learned_digest: str | None = None
        self._learned_received_mono_ms: int | None = None
        self._rl_output: DeliveredSupervisorOutput | None = None
        self._rl_received_mono_ms: int | None = None
        # RL 出力の元 snapshot を loop 自身の単調時計で特定するための窓。有効期限より
        # 長く持っても使えないので、設定から幅を決める（固定長にしない）。
        # **有効期限のいちばん長い提案が収まる幅**にする。窓が短いと、まだ使える提案の
        # 元 snapshot を特定できず、健全な worker の提案まで落としてしまう。
        horizon_ms = max(config.policy.supervisor.valid_ms, config.policy.mpc.valid_ms)
        window = horizon_ms // config.safety.tick_ms.value + 2
        self._snapshots: deque[_SnapshotIdentity] = deque(maxlen=window)
        # **4ファイルの版と SHA-256 を `ControlConfig.sources` から写す**（runtime v2。
        # 決定記録 0073 §2.5 (c)）。手で書かない（起動ログの `trace_metadata()` と同じ出どころ）。
        self._config_digest = config.runtime_digest()
        # **未校正なら推定モデルを作らない**（決定記録 0073 §2.2）。記録するだけで、requested・
        # Guard・Safety へは値を返さない。
        self._air_balance = AirBalanceRecorder.from_control_config(config)
        self._coordinator = self._build_coordinator(config, air_balance_coordinator)

    @property
    def tick_period_ms(self) -> int:
        """検証済み設定が決めた control tick の周期。"""
        return self._config.safety.tick_ms.value

    @property
    def tick_deadline_ms(self) -> int:
        """検証済み設定が決めた1 tick の締め切り。"""
        return self._config.safety.tick_deadline_ms.value

    def tick(self) -> ControlTickResult:
        """1 tick を通す。**同じ loop の tick を重ねられない。**"""
        if self._in_tick:
            # 重なると同じ Backend へ2つの command が並び、どちらが後か決まらない。
            raise ControlLoopBusyError("control tick は重ねない（0028 §2.6）")
        self._in_tick = True
        try:
            return self._run_tick()
        finally:
            self._in_tick = False

    def _run_tick(self) -> ControlTickResult:
        tick_id = self._tick_id
        self._tick_id += 1
        started_mono_ms = self._monotonic.monotonic_ms()
        ts_ms = self._clock.now_ms()
        # **tick の先頭でモードを決める**（決定記録 0072 §2.2 / 0060 §2.5）。受付スレッドの
        # 生存の確認は受け渡し口を覗く前に行い、tick の途中ではモードを変えない。
        mode, mode_record, authority_command = self._resolve_mode(tick_id, started_mono_ms)
        # **降格は Gate がこの tick の stage を読む前に効かせる**（決定記録 0072 §2.6）。
        # memory 上の上限を入れるだけで、journal への書き残しは heartbeat の後に回す。
        authority_record = self._apply_authority_command(tick_id, authority_command)

        snapshot, snapshot_status = self._snapshot(tick_id, ts_ms, started_mono_ms)
        self._snapshots.append(
            _SnapshotIdentity(
                tick_id=snapshot.tick_id,
                ts_ms=snapshot.ts_ms,
                schema_version=snapshot.schema_version,
                monotonic_ms=snapshot.monotonic_ms,
            )
        )
        supervisor_decision, regime_estimate = self._run_supervisor(
            snapshot, now_mono_ms=snapshot.monotonic_ms
        )
        supervisor_output = (
            None if supervisor_decision is None else supervisor_decision.selected_output
        )
        guard_decision, guard_fault = self._run_guard(snapshot)
        guard_zones = self._guard_zones(guard_decision)
        baseline, controller_fault = self._run_fallback(snapshot, snapshot_status)
        learned, learned_unavailable = self._poll_learned(snapshot.monotonic_ms)

        external = self._pending_faults + tuple(
            fault for fault in (guard_fault, controller_fault) if fault is not None
        )
        self._pending_faults = ()
        # **ここから合成までは捕まえない。** Critical Safety と合成の例外は不具合であり、
        # プロセスを終わらせて引き継ぎで Max にする（0028 §2.7）。
        safety_decision = self._safety.evaluate(
            snapshot,
            mode=mode.mode,
            external_faults=external,
            tick_overrun=self._previous_overrun,
        )
        # **協調は Safety の評価の後・Gate の前**（決定記録 0078 §2.2）。裁定と合成の下限を
        # 読むだけで、作り直さない。try が囲むのは協調の部品の呼び出しだけ（0078 §2.6）。
        coordination = self._coordinate(
            mode=mode,
            snapshot=snapshot,
            snapshot_status=snapshot_status,
            baseline=baseline,
            safety=safety_decision,
            guard=guard_zones,
        )
        if coordination.fault is not None:
            self._pending_faults += (coordination.fault,)
        selection: ControllerSelection | None = None
        if not coordination.bypass_gate:
            selection, gate_fault = self._select(
                snapshot=snapshot,
                snapshot_status=snapshot_status,
                mode=mode,
                baseline=coordination.gate_baseline,
                learned=learned,
                learned_unavailable=learned_unavailable,
                safety_state=safety_decision.state,
                supervisor_available=supervisor_output is not None,
                started_mono_ms=started_mono_ms,
            )
            if gate_fault is not None:
                self._pending_faults += (gate_fault,)
        # Gate を通せなかった tick（迂回・Gate の例外）は Gate へ渡すはずだった Baseline を使う。
        # Gate の例外の tick は、apply で上げていれば coordinated baseline（決定記録 0088 §2.1）。
        # 迂回した tick のそれは raw baseline である（決定記録 0085 §2.1）。
        requested = self._requested(mode, coordination.gate_baseline, selection)
        composed = self._composer.compose(
            requested=requested,
            guard=guard_zones,
            safety=safety_decision,
            mode=mode.mode,
        )
        effective = PerZone[EffectiveZoneDemand](
            front=composed.front, rear=composed.rear, top=composed.top
        )
        hardware = self._apply(composed)
        # **書き込みと検証まで**を1 tick の所要時間とする（0028 §2.6 の watchdog と同じ区間）。
        # decision trace の保存時間を含めると、保存先が遅いだけで安全側の縮退が起きる。
        duration_ms = max(0, self._monotonic.monotonic_ms() - started_mono_ms)
        overrun = duration_ms > self.tick_deadline_ms
        self._previous_overrun = overrun
        # **heartbeat は書き込みと検証が終わった時点で出す**（0028 §2.6 / 0060 §2.7）。
        # このあとに置く処理（降格の永続化・decision trace の保存）はどれも I/O を伴い、
        # 保存先のロックで待たされると deadman が鳴る。制御は終わっているのに殺される。
        self._beat()

        self._observe_authority(
            safety_state=safety_decision.state,
            selection=selection,
            now_mono_ms=snapshot.monotonic_ms,
        )
        state = self._control_state(
            mode=mode,
            selection=selection,
            safety_state=safety_decision.state,
            supervisor_decision=supervisor_decision,
            regime_estimate=regime_estimate,
            coordination_bypassed=coordination.bypass_gate,
        )
        if self._admin_mode is not None:
            # `status` のための写しを置くだけ（待たない）。heartbeat の後に置く。
            self._admin_mode.publish(
                tick_id=tick_id, authority_stage=state.authority_stage, authority=authority_record
            )
        flows, air_balance = self._air_balance_record(
            snapshot, hardware=hardware, faults=safety_decision.faults
        )
        tick = ControlTick(
            schema_version=SCHEMA_VERSION,
            tick_id=tick_id,
            ts_ms=ts_ms,
            state=state,
            zones=self._zone_records(requested, effective, hardware, flows),
            supervisor=supervisor_decision,
            model_gate=None if selection is None else selection.model_gate,
            shadow=self._shadow_record(
                tick_id=tick_id,
                ts_ms=ts_ms,
                state=state,
                effective=effective,
                selection=selection,
                # 0053 の Fallback の値は、その tick の coordinated baseline（決定記録 0078 §2.5）
                baseline=coordination.gate_baseline,
                learned=learned,
                supervisor=supervisor_output,
            ),
            faults=safety_decision.faults,
            runtime=ControlTickRuntime(
                schema_version=CONTROL_TICK_RUNTIME_SCHEMA_VERSION,
                tick_period_ms=self.tick_period_ms,
                deadline_ms=self.tick_deadline_ms,
                duration_ms=duration_ms,
                deadline_exceeded=overrun,
                snapshot_schema_version=snapshot.schema_version,
                config=self._config_digest,
            ),
            # **裁定の前提を判断と同じ行に残す**（v9。#78）。T_SENSOR を外していた期間や
            # 暫定値で回っていた期間を、確定値の trace と混ぜないため。
            safety_provenance=SafetyProvenance(
                disabled_inputs=safety_decision.disabled_inputs,
                config_is_provisional=safety_decision.config_is_provisional,
            ),
            # **tick が使っていた registry の版を毎 tick 残す**（v10。#104 / 決定記録 0071 §2.5）。
            registry=self._registry,
            # **Air Balance の推定を毎 tick 残す**（v11。#81 / 決定記録 0073 §2.5 (b)）。
            # 未校正で無効な起動も `disabled` の形で残す。
            air_balance=air_balance,
            # **その tick のモードの出どころを残す**（v12。#74 / 決定記録 0072 §2.7）。
            mode_command=mode_record,
            # **その tick の制御権の出どころを残す**（v13。#92 / 決定記録 0072 §2.6）。
            authority=authority_record,
            # **Baseline への協調と、Safety の裁定の未確認の tach を残す**
            # （v14。#81 / 決定記録 0078 §2.7）。
            # 裁定は直列化しないので、writer がこの tick の裁定から明示的に写す。
            air_balance_coordination=coordination.record,
            tach_unconfirmed_zones=tuple(
                sorted(safety_decision.tach_unconfirmed_zones, key=lambda zone: zone.value)
            ),
        )
        recorded, trace_failed = self._record(tick)
        # **journal の書き残しと変化の検知は heartbeat と trace の保存の後**（0072 §2.6 /
        # 0060 §2.7）。lock の待ち上限つきで、効くのは次の tick から。
        self._maintain_authority()
        self._log_guard_events(guard_decision)
        # **worker への frame は tick の最後**（heartbeat の後。決定記録 0077 §2.2）。
        # 置くだけで待たない。
        self._offer_learned_frame(
            snapshot=snapshot,
            workload=regime_estimate,
            supervisor=supervisor_output,
            baseline=baseline,
            safety=safety_decision,
            hardware=hardware,
            authority_stage=state.authority_stage,
        )

        if overrun:
            LOGGER.warning(
                "control tick deadline overrun",
                extra={
                    logs.FIELDS_KEY: {
                        "tick_id": tick_id,
                        "duration_ms": duration_ms,
                        "deadline_ms": self.tick_deadline_ms,
                    }
                },
            )
        return ControlTickResult(
            tick=tick,
            duration_ms=duration_ms,
            deadline_exceeded=overrun,
            recorded=recorded,
            trace_failed=trace_failed,
            hardware=hardware,
        )

    def _snapshot(
        self, tick_id: int, ts_ms: int, monotonic_ms: int
    ) -> tuple[ControlStateSnapshot, SnapshotStatus]:
        """この tick の唯一の snapshot を作る。**読み直さない。**

        収集層が落ちていても待たない。値の無い入力は 0 で埋めず `missing` のまま渡し、
        Critical Safety が Telemetry の fault として裁く（0028 §2.7）。
        """
        status = SnapshotStatus.AVAILABLE
        readings: tuple[TelemetryReading, ...] = ()
        try:
            readings = tuple(
                self._ages.observe(sample, monotonic_ms) for sample in self._telemetry.read()
            )
        except Exception:
            status = SnapshotStatus.UNAVAILABLE
            LOGGER.exception(
                "control telemetry read failed",
                extra={logs.FIELDS_KEY: {"tick_id": tick_id}},
            )
        try:
            snapshot = self._estimator.build(
                ControlInputFrame(
                    tick_id=tick_id,
                    ts_ms=ts_ms,
                    monotonic_ms=monotonic_ms,
                    readings=readings,
                    fans=self._fans,
                ),
                self._previous_snapshot,
            )
        except Exception:
            # 契約に無い metric などで組み立てに失敗しても、制御は止めない。空の frame で
            # 作り直し、すべて missing の snapshot として安全側の裁定へ渡す。
            status = SnapshotStatus.INVALID
            LOGGER.exception(
                "control state snapshot invalid",
                extra={logs.FIELDS_KEY: {"tick_id": tick_id}},
            )
            snapshot = self._estimator.build(
                ControlInputFrame(
                    tick_id=tick_id, ts_ms=ts_ms, monotonic_ms=monotonic_ms, fans=self._fans
                ),
                self._previous_snapshot,
            )
        self._previous_snapshot = snapshot
        return snapshot, status

    def _resolve_mode(
        self, tick_id: int, now_mono_ms: int
    ) -> tuple[ModeCommand, ModeCommandRecord, AdminAuthorityCommand | None]:
        """この tick のモードと、その出どころの記録、authority の枠から取り出した降格。"""
        if self._admin_mode is not None:
            resolution = self._admin_mode.resolve(tick_id=tick_id, now_mono_ms=now_mono_ms)
            self._mode = resolution.command
            return resolution.command, resolution.record, resolution.authority
        return self._mode_command(), ModeCommandRecord.without_entry(), None

    def _apply_authority_command(
        self, tick_id: int, command: AdminAuthorityCommand | None
    ) -> AuthorityRecord:
        """管理ソケットの降格を memory 上で入れ、この tick の制御権の記録を作る。

        **入れられなかった降格を適用したと返さない。** 例外は捕まえて記録し、制御は続ける
        （降格が入らなくても authority は上がらない）。
        """
        applied_id: int | None = None
        if command is not None:
            try:
                changed = self._authority.apply_lowering(
                    to_stage=command.to_stage,
                    actor=command.actor,
                    reason=command.journal_reason(),
                )
            except Exception:
                LOGGER.exception(
                    "control-admin の authority の降格を入れられなかった",
                    extra={logs.FIELDS_KEY: {"tick_id": tick_id, "command_id": command.command_id}},
                )
            else:
                applied_id = command.command_id
                LOGGER.warning(
                    "control-admin の authority の降格を入れた（journal へは heartbeat の後）",
                    extra={
                        logs.FIELDS_KEY: {
                            "tick_id": tick_id,
                            "command_id": command.command_id,
                            "op": command.op,
                            "to_stage": command.to_stage.value,
                            "changed": changed,
                            "authority_stage": self._authority.current_stage().value,
                        }
                    },
                )
                if self._admin_mode is not None:
                    self._admin_mode.authority_applied(
                        command_id=command.command_id, tick_id=tick_id
                    )
        return self._authority.trace_record(command_id=applied_id)

    def _maintain_authority(self) -> None:
        try:
            self._authority.maintain()
        except Exception:
            # 書き残し・読み直しに失敗しても制御は続ける。上げる経路は無い。
            LOGGER.exception("authority runtime maintenance failed")

    def _mode_command(self) -> ModeCommand:
        """人が決めたモード。読めなければ**直前のモードを保つ。**

        読めないことを理由に `AUTO` へ戻すと、人が `MAX` にした運転が入口の故障で
        勝手に下がる（安全でない側への自動遷移になる）。
        """
        try:
            command = self._mode_source.current()
        except Exception:
            LOGGER.exception(
                "operating mode source failed; keeping the previous mode",
                extra={logs.FIELDS_KEY: {"mode": self._mode.mode.value}},
            )
            return self._mode
        self._mode = command
        return command

    def _run_supervisor(
        self, snapshot: ControlStateSnapshot, *, now_mono_ms: int
    ) -> tuple[SupervisorDecision | None, WorkloadRegimeEstimate | None]:
        if self._supervisor is None or self._regime is None:
            return None, None
        try:
            self._regime_state, estimate = self._regime.step(self._regime_state, snapshot)
        except Exception:
            LOGGER.exception(
                "workload regime estimation failed",
                extra={logs.FIELDS_KEY: {"tick_id": snapshot.tick_id}},
            )
            return None, None
        candidate, rl_error = self._rl_candidate(now_mono_ms)
        try:
            decision = self._supervisor.evaluate(
                SupervisorInput(snapshot=snapshot, workload=estimate),
                now_monotonic_ms=now_mono_ms,
                rl_candidate=candidate,
                rl_error=rl_error,
            )
        except Exception:
            # Supervisor は運転戦略であって安全の経路ではない。失敗しても Fallback で続ける。
            LOGGER.exception(
                "supervisor evaluation failed",
                extra={logs.FIELDS_KEY: {"tick_id": snapshot.tick_id}},
            )
            return None, estimate
        return decision, estimate

    def _rl_candidate(
        self, now_mono_ms: int
    ) -> tuple[ReceivedSupervisorOutput | None, Reason | None]:
        """RL worker の出力を、**loop 自身の単調時計に束縛してから**渡す。

        **受け渡し口を覗く前に経路の状態を読む**（決定記録 0077 §2.5 の RL の行）。受付スレッドが
        死んだ・worker が切れた・黙った・registry の移動で閉じた tick では読まない。`poll()` が
        何も返さないのと同じく Coordinator は RL を未受信として扱い、active なら RulePolicy へ戻る。
        """
        if self._rl_supervisor_source is None:
            return None, None
        if self._learned_channel_unavailable(LearnedRole.SUPERVISOR) is not None:
            return None, None
        try:
            delivered = self._rl_supervisor_source.poll()
        except Exception:
            LOGGER.exception("supervisor worker poll failed")
            return None, Reason(code="supervisor_unavailable", detail="worker poll failed")
        if delivered is None:
            # **識別子と受信時刻を消さない**（MPC と同じ。0060 §2.6）。消すと、一時的に読めなかった
            # 後で同じ出力が出てきたときに新しい受信時刻を押してしまう
            return None, None
        if delivered != self._rl_output:
            self._rl_output = delivered
            self._rl_received_mono_ms = now_mono_ms
        received_mono_ms = self._rl_received_mono_ms
        assert received_mono_ms is not None
        output = delivered.output
        # **tick 番号だけで照合しない。** 番号は再起動で 0 に戻るので、前の process の出力が
        # 新しい process の無関係な snapshot に結び付く。壁時計の時刻と snapshot の形まで
        # 一致した記録だけを「この loop が出した snapshot」とみなす（0060 §2.6）。
        source = self._issued_snapshot(
            output.tick_id, output.ts_ms, schema_version=output.snapshot_schema_version
        )
        if source is None or source.monotonic_ms > received_mono_ms:
            return None, Reason(
                code="supervisor_source_unknown",
                detail=(
                    f"tick_id={output.tick_id}; ts_ms={output.ts_ms}; "
                    f"snapshot_schema_version={output.snapshot_schema_version} "
                    "の元 snapshot をこの loop の単調時計で特定できない"
                ),
            )
        return (
            ReceivedSupervisorOutput(
                output=output,
                source_monotonic_ms=source.monotonic_ms,
                received_monotonic_ms=received_mono_ms,
                # 識別は worker が経路で運んだ値を写すだけ。照合は Coordinator が
                # `expected_rl_identity`（起動時の registry。0077 §2.6）と行う
                identity=delivered.identity,
                # **origin は `unverified` のままにする**（既定値。#89 / 決定記録 0061 §2.4 /
                # 0077 §2.8）。別プロセスの worker が `for_shadow` / `for_active` のどちらで
                # 束縛したかを loop は証明できない。`shadow_binding` を押すとその値の意味が変わり、
                # `active_binding` を名乗らせると証明できない提案に制御権を渡す。結果として
                # `active_policy: rl_policy` の構成では Rule へ落ちるが、**それが正しい**。
                # 用途を経路で証明して運ぶ形は、`for_active` の門を開く別の決定記録で決める。
            ),
            None,
        )

    def _issued_snapshot(
        self, tick_id: int, ts_ms: int, *, schema_version: int | None = None
    ) -> _SnapshotIdentity | None:
        """**この process が出した** snapshot の記録を探す。

        `tick_id` だけでは足りない（再起動で 0 に戻る）。壁時計の `ts_ms` まで一致した
        ものだけを自分が出した snapshot とみなす（決定記録 0060 §2.6）。
        """
        return next(
            (
                item
                for item in self._snapshots
                if item.tick_id == tick_id
                and item.ts_ms == ts_ms
                and (schema_version is None or item.schema_version == schema_version)
            ),
            None,
        )

    def _run_guard(
        self, snapshot: ControlStateSnapshot
    ) -> tuple[ReactiveGuardDecision | None, Fault | None]:
        try:
            return self._guard.evaluate(snapshot), None
        except Exception as error:
            LOGGER.exception(
                "reactive guard failed",
                extra={logs.FIELDS_KEY: {"tick_id": snapshot.tick_id}},
            )
            return None, Fault(
                code=FaultCode.GUARD_EXCEPTION,
                detail=f"{type(error).__name__}: {error}"[:500],
            )

    @staticmethod
    def _guard_zones(decision: ReactiveGuardDecision | None) -> PerZone[GuardZoneOutput]:
        if decision is not None:
            return decision.zones
        empty = GuardZoneOutput()
        return PerZone[GuardZoneOutput](front=empty, rear=empty, top=empty)

    @staticmethod
    def _log_guard_events(decision: ReactiveGuardDecision | None) -> None:
        """Guard の介入の開始・解除を構造化ログへ出す（#80）。

        inactive な GuardZoneOutput は理由を持てないので、解除の理由は events にしか無い。
        ここで落とすと「なぜ floor が外れたか」を後から辿れなくなる。
        **書き込み・heartbeat・trace の保存の後に呼ぶ。** ログの出力先が詰まっても、
        介入が必要な tick の Critical Safety と Fan の書き込みを遅らせないため。
        """
        if decision is None:
            return
        for event in decision.events:
            LOGGER.info(
                "reactive guard %s",
                event.transition.value,
                extra={
                    logs.FIELDS_KEY: {
                        "tick_id": decision.tick_id,
                        "monotonic_ms": decision.monotonic_ms,
                        "zone": event.zone.value,
                        "transition": event.transition.value,
                        "reason_code": event.reason.code,
                        "reason_detail": event.reason.detail,
                        "trigger_codes": list(event.trigger_codes),
                        "threshold_profile": decision.threshold_profile.value,
                    }
                },
            )

    def _run_fallback(
        self, snapshot: ControlStateSnapshot, snapshot_status: SnapshotStatus
    ) -> tuple[ControllerProposal | None, Fault | None]:
        try:
            if snapshot_status is SnapshotStatus.AVAILABLE:
                return self._fallback.propose(snapshot), None
            return (
                self._fallback.propose_without_snapshot(
                    tick_id=snapshot.tick_id,
                    ts_ms=snapshot.ts_ms,
                    monotonic_ms=snapshot.monotonic_ms,
                ),
                None,
            )
        except Exception as error:
            LOGGER.exception(
                "fallback controller failed",
                extra={logs.FIELDS_KEY: {"tick_id": snapshot.tick_id}},
            )
            return None, Fault(
                code=FaultCode.FALLBACK_EXCEPTION,
                detail=f"{type(error).__name__}: {error}"[:500],
            )

    @staticmethod
    def _build_coordinator(
        config: ControlConfig, injected: AirBalanceCoordinator | None
    ) -> AirBalanceCoordinator | None:
        """``fan-policy.yaml`` の ``mode`` から協調の部品を作る（決定記録 0078 §2.1）。"""
        settings = config.policy.air_balance_coordination
        if injected is not None:
            if injected.mode is not settings.mode:
                # 設定と違う mode の部品を差し込むと、trace の mode と実際の扱いが食い違う。
                raise ValueError("Air Balance の協調の部品は fan-policy.yaml と同じ mode にする")
            return injected
        if settings.mode is AirBalanceCoordinationMode.OFF:
            return None
        # `shadow` / `apply` と `uncalibrated` の組は ControlConfig が既に拒んでいる（0078 §2.4）。
        # 部品の側も未校正なら作らない（AirBalanceCoordinator の検証）。
        model = ConfiguredAirBalanceModel(config.air_balance, config.sources.air_balance.sha256)
        return AirBalanceCoordinator(
            settings=settings, model=model, fan_hardware=config.fan_hardware
        )

    def _coordinate(
        self,
        *,
        mode: ModeCommand,
        snapshot: ControlStateSnapshot,
        snapshot_status: SnapshotStatus,
        baseline: ControllerProposal | None,
        safety: CriticalSafetyDecision,
        guard: PerZone[GuardZoneOutput],
    ) -> _Coordination:
        """raw baseline に Air Balance の協調を掛ける（決定記録 0078 §2.2〜§2.6 / 0085）。

        条件（0078 §2.3）が1つでも欠ければ ``skipped`` で raw baseline のまま、保持を解く。
        **例外を捕まえるのは ``AirBalanceCoordinator.apply()`` の呼び出しだけ**で、
        Critical Safety の裁定の読み取りや合成の下限の計算が投げる例外は捕まえない
        （0028 §2.7）。
        """
        coordinator = self._coordinator
        if coordinator is None:
            return _Coordination(
                record=AirBalanceCoordinationRecord.off(),
                gate_baseline=baseline,
                bypass_gate=False,
                fault=None,
            )
        max_raise = coordinator.max_raise()
        candidate = None if baseline is None else _proposal_demands(baseline)
        skip_reason = self._coordination_skip_reason(
            mode=mode, snapshot_status=snapshot_status, baseline=baseline, safety=safety
        )
        released = PerZone[bool](front=False, rear=False, top=False)
        if skip_reason is not None:
            # 条件が崩れた tick は次の tick で保持を即座に解く（0078 §2.4）。
            coordinator.release()
            return _Coordination(
                record=AirBalanceCoordinationRecord(
                    mode=coordinator.mode,
                    status=AirBalanceCoordinationStatus.SKIPPED,
                    skip_reason=skip_reason,
                    candidate=candidate,
                    output=candidate,
                    max_raise=max_raise,
                    held=released,
                ),
                gate_baseline=baseline,
                bypass_gate=False,
                fault=None,
            )
        assert baseline is not None and candidate is not None
        projected = self._projected_floors(safety, guard)
        thermal = self._air_balance.thermal_inputs(snapshot)
        try:
            result = coordinator.apply(
                candidate=candidate,
                thermal=thermal,
                projected_floors=projected.floors,
                now_mono_ms=snapshot.monotonic_ms,
            )
        except Exception as error:
            return self._coordination_failed(
                error,
                tick_id=snapshot.tick_id,
                mode=coordinator.mode,
                baseline=baseline,
                candidate=candidate,
                max_raise=max_raise,
                held=released,
                projected=projected,
            )
        return _Coordination(
            record=_coordination_record(result, projected),
            gate_baseline=_coordinated_baseline(baseline, result),
            bypass_gate=False,
            fault=None,
        )

    def _coordination_skip_reason(
        self,
        *,
        mode: ModeCommand,
        snapshot_status: SnapshotStatus,
        baseline: ControllerProposal | None,
        safety: CriticalSafetyDecision,
    ) -> AirBalanceCoordinationSkipReason | None:
        """0078 §2.3 の表を上から順に見て、最初に欠けた条件を返す。"""
        if not self._air_balance.enabled:
            return AirBalanceCoordinationSkipReason.AIR_BALANCE_DISABLED
        if mode.mode is not OperatingMode.AUTO:
            return AirBalanceCoordinationSkipReason.OPERATING_MODE
        if snapshot_status is not SnapshotStatus.AVAILABLE:
            return AirBalanceCoordinationSkipReason.SNAPSHOT_UNAVAILABLE
        if baseline is None:
            return AirBalanceCoordinationSkipReason.BASELINE_UNAVAILABLE
        if safety.state not in {SafetyState.NORMAL, SafetyState.DEGRADED}:
            return AirBalanceCoordinationSkipReason.SAFETY_STATE
        # Fan fault の zone の実際の風量は demand から言えない（不明か 0）。Top でも同じで、
        # 判定は fault code で行い、SafetyZoneOutput.reason の文字列は読まない（0078 §2.3）。
        # Front / Rear の forced_max は NORMAL / DEGRADED では Fan fault からしか生じないが、
        # 将来の裁定の追加に備えて明示的にも確かめる。
        if (
            any(fault.code in FAN_FAULT_CODES and fault.zone is not None for fault in safety.faults)
            or safety.zones.front.forced_max
            or safety.zones.rear.forced_max
        ):
            return AirBalanceCoordinationSkipReason.ZONE_FAN_FAULT
        if safety.tach_unconfirmed_zones:
            return AirBalanceCoordinationSkipReason.TACH_UNCONFIRMED
        return None

    def _projected_floors(
        self, safety: CriticalSafetyDecision, guard: PerZone[GuardZoneOutput]
    ) -> ProjectedFloors:
        """合成が requested に掛ける zone ごとの下限の見込み（決定記録 0078 §2.2）。

        ramp_down の下限は合成と同じ関数・同じ前 tick の effective・同じ経過時間で求める。
        経過時間は、このあと合成に渡す Safety 裁定の時刻から数える。
        """
        ramp = PerZone[float | None](
            front=self._composer.ramp_floor(Zone.FRONT, safety.monotonic_ms),
            rear=self._composer.ramp_floor(Zone.REAR, safety.monotonic_ms),
            top=self._composer.ramp_floor(Zone.TOP, safety.monotonic_ms),
        )
        return project_floors(safety=safety.zones, guard=guard, ramp=ramp)

    @staticmethod
    def _coordination_failed(
        error: Exception,
        *,
        tick_id: int,
        mode: AirBalanceCoordinationMode,
        baseline: ControllerProposal,
        candidate: PerZone[Demand],
        max_raise: PerZone[Demand],
        held: PerZone[bool],
        projected: ProjectedFloors,
    ) -> _Coordination:
        """協調の失敗を握りつぶさず ``failed`` として残す（決定記録 0078 §2.6 / 0085）。

        ``mode: apply`` では ``fallback_exception`` へ翻訳し（次 tick の Safety が EMERGENCY）、
        この tick は Gate を迂回して raw baseline を requested にする。
        ``mode: shadow`` は記録だけで、raw baseline を持って Gate を通す（shadow は Fan を
        変えない）。保持は ``apply()`` が解いた。
        """
        LOGGER.exception(
            "air balance coordination failed",
            extra={logs.FIELDS_KEY: {"tick_id": tick_id, "mode": mode.value}},
        )
        description = f"{type(error).__name__}: {error}"
        apply_mode = mode is AirBalanceCoordinationMode.APPLY
        return _Coordination(
            record=AirBalanceCoordinationRecord(
                mode=mode,
                status=AirBalanceCoordinationStatus.FAILED,
                candidate=candidate,
                output=candidate,
                max_raise=max_raise,
                held=held,
                projected_floors=projected.floors,
                projected_floor_basis=projected.basis,
                failure=AirBalanceCoordinationFailure(
                    type=type(error).__name__[:120], detail=str(error)[:500]
                ),
            ),
            gate_baseline=baseline,
            bypass_gate=apply_mode,
            fault=(
                Fault(
                    code=FaultCode.FALLBACK_EXCEPTION,
                    detail=f"air_balance_coordination: {description}"[:500],
                )
                if apply_mode
                else None
            ),
        )

    def _select(
        self,
        *,
        snapshot: ControlStateSnapshot,
        snapshot_status: SnapshotStatus,
        mode: ModeCommand,
        baseline: ControllerProposal | None,
        learned: MpcProposal | None,
        learned_unavailable: LearnedChannelState | None,
        safety_state: SafetyState,
        supervisor_available: bool,
        started_mono_ms: int,
    ) -> tuple[ControllerSelection | None, Fault | None]:
        """この tick の制御器を選ぶ。**`MANUAL` / `CALIBRATION` では Gate を呼ばない。**"""
        now_mono_ms = snapshot.monotonic_ms
        try:
            if mode.mode in {OperatingMode.MANUAL, OperatingMode.CALIBRATION}:
                self._gate.set_operating_mode(mode.mode, now_mono_ms=now_mono_ms)
                return None, None
            if baseline is None:
                return None, None
            # **締め切りを過ぎていたら ML を通さない。** 遅れた tick で新しい提案を採ると、
            # 安全側の裁定が ML の遅延を待つ形になる（0028 §2.6）。
            deadline_exceeded = (
                self._monotonic.monotonic_ms() - started_mono_ms > self.tick_deadline_ms
            )
            status = self._learned_status(
                learned,
                unavailable=learned_unavailable,
                snapshot_status=snapshot_status,
                supervisor_available=supervisor_available,
                control_deadline_exceeded=deadline_exceeded,
            )
            return (
                self._gate.select(
                    now_mono_ms=now_mono_ms,
                    fallback=baseline,
                    learned=status,
                    operating_mode=mode.mode,
                    safety_state=safety_state,
                ),
                None,
            )
        except Exception as error:
            # Gate は決定論的な層である。例外は不具合なので `fallback_exception` へ翻訳し、
            # この tick は Fallback の値で続ける（次 tick の Safety が EMERGENCY にする）。
            LOGGER.exception(
                "controller gate failed",
                extra={logs.FIELDS_KEY: {"tick_id": snapshot.tick_id}},
            )
            return None, Fault(
                code=FaultCode.FALLBACK_EXCEPTION,
                detail=f"controller_gate: {type(error).__name__}: {error}"[:500],
            )

    @staticmethod
    def _requested(
        mode: ModeCommand,
        baseline: ControllerProposal | None,
        selection: ControllerSelection | None,
    ) -> PerZone[ZoneRequest]:
        """この tick の requested。**どの経路でも Guard と Safety を必ず通る。**"""
        if mode.requested is not None:
            return mode.requested
        if selection is not None:
            return selection.proposal.requested
        if baseline is not None:
            return baseline.requested
        # 制御器も Gate も値を出せなかった tick。使えない入力を 0 とみなさず Max を要求する。
        request = ZoneRequest(
            demand=1.0,
            reason=Reason(code="controller_unavailable", detail="制御器が requested を出せない"),
        )
        return PerZone[ZoneRequest](front=request, rear=request, top=request)

    def _poll_learned(
        self, now_mono_ms: int
    ) -> tuple[MpcProposal | None, LearnedChannelState | None]:
        """worker 結果を読み、**初めて見た結果にだけ**受信時刻を押す。

        毎 tick 現在時刻を押すと、worker が止まって同じ結果を返し続けても永久に期限切れに
        ならない。識別子（`MpcProposal.result_digest()`）で新旧を見分ける。

        **受け渡し口を覗く前に経路の状態を読む**（決定記録 0077 §2.2 / §2.5）。受付スレッドが
        死んでいれば再起動まで1件も読まない。worker が切れた・黙った tick では読まずに
        Fallback へ倒し、その状態を2つ目の値として返す（Gate が `Reason.detail` に入れる）。
        """
        if self._learned_source is None:
            return None, None
        unavailable = self._learned_channel_unavailable(LearnedRole.MPC)
        if unavailable is not None:
            return None, unavailable
        try:
            result = self._learned_source.poll()
        except Exception:
            LOGGER.exception("learned worker poll failed")
            return None, None
        if result is None:
            # **識別子と受信時刻を消さない。** 消すと、worker が一時的に読めなくなった
            # あとで同じ提案が出てきたときに新しい受信時刻を押してしまい、止まった worker の
            # 古い提案が何度でも有効期限を取り戻す（決定記録 0060 §2.6）。
            return None, None
        if not self._result_is_bound_to_our_snapshot(result):
            return None, None
        digest = result.result_digest()
        if digest != self._learned_digest:
            self._learned_digest = digest
            self._learned_received_mono_ms = now_mono_ms
        return result, None

    def _learned_channel_unavailable(self, role: LearnedRole) -> LearnedChannelState | None:
        """その役割の経路が結果を渡せない状態ならその値。渡せる（`connected`）なら None。

        受付スレッドの死は役割をまたいで再起動まで覚える（受付スレッドは1本。0077 §2.2）。
        """
        if self._learned_health is None:
            return None
        if self._learned_channel_dead:
            return LearnedChannelState.CHANNEL_DEAD
        try:
            state = self._learned_health.state(role)
        except Exception:
            # 状態を答えられない経路の提案は読まない（読める根拠が無い）。再起動まで閉じる
            LOGGER.exception("learned channel health failed; closing the learned channel")
            state = LearnedChannelState.CHANNEL_DEAD
        if state is LearnedChannelState.CHANNEL_DEAD:
            self._learned_channel_dead = True
            LOGGER.error(
                "learned channel の受付スレッドが止まった。再起動まで Learned を読まない",
                extra={logs.FIELDS_KEY: {"reason": "learned_channel_dead"}},
            )
        if state is LearnedChannelState.CONNECTED:
            return None
        return state

    def _offer_learned_frame(
        self,
        *,
        snapshot: ControlStateSnapshot,
        workload: WorkloadRegimeEstimate | None,
        supervisor: SupervisorOutput | None,
        baseline: ControllerProposal | None,
        safety: CriticalSafetyDecision,
        hardware: PerZone[FanHardwareResult] | None,
        authority_stage: AuthorityStage,
    ) -> None:
        """その tick の入力を送り出し用の1枠へ置く（決定記録 0077 §2.2 / §2.3）。**待たない。**

        失敗しても制御には何も返さない（frame が届かないのは worker の window の欠けであり、
        Confidence / OOD と Gate が Fallback へ倒す）。
        """
        if self._learned_sink is None:
            return
        assert self._learned_calibration is not None and self._metric_catalog_sha256 is not None
        try:
            frame = LearnedFrame(
                snapshot=snapshot,
                workload=workload,
                supervisor=supervisor,
                baseline=baseline,
                safety_floor=PerZone[Demand](
                    front=safety.zones.front.floor,
                    rear=safety.zones.rear.floor,
                    top=safety.zones.top.floor,
                ),
                applied=_applied_demands(hardware),
                authority_stage=authority_stage,
                expected_artifacts=self._expected_artifacts,
                config=self._config_digest,
                # **毎 tick 同じ値**（fand は較正も catalog も起動時にしか読まない。0101 §2.2）
                calibration=self._learned_calibration,
                metric_catalog_sha256=self._metric_catalog_sha256,
            )
            self._learned_sink.offer(frame)
        except Exception:
            LOGGER.exception(
                "learned worker への frame を置けなかった",
                extra={logs.FIELDS_KEY: {"tick_id": snapshot.tick_id}},
            )

    def _learned_source_mono_ms(self, result: MpcProposal) -> int | None:
        """提案の元 snapshot の単調時刻（**この loop の時計**。決定記録 0077 §2.4 の4）。

        `_result_is_bound_to_our_snapshot` を通った結果だけがここへ来る。worker が名乗る時刻は
        使わず、この process が出した snapshot の記録から引く。提案の無い結果（失敗）は None。
        """
        proposal = result.proposal
        if proposal is None:
            return None
        source = self._issued_snapshot(proposal.seq, proposal.computed_at_ms)
        return None if source is None else source.monotonic_ms

    def _result_is_bound_to_our_snapshot(self, result: MpcProposal) -> bool:
        """worker 結果が、**この process が出した snapshot**から作られたか（0060 §2.6）。

        受信時刻は「この loop が初めて見た時刻」なので、再起動をまたいで生き残った worker の
        結果をそのまま受け取ると、前の process のときに作られた提案へ新しい受信時刻を押す。
        `tick_id` は 0 から振り直されるため、それだけでは見分けられない。

        提案の無い結果（worker の失敗）は素通しする。**Fallback へ倒す向きにしか効かず、
        制御権を与えないため**である。
        """
        proposal = result.proposal
        if proposal is None:
            return True
        # `ControllerProposal.seq` / `computed_at_ms` は、worker が読んだ snapshot の
        # `tick_id` / `ts_ms` そのものである（#86）。
        if self._issued_snapshot(proposal.seq, proposal.computed_at_ms) is not None:
            return True
        LOGGER.warning(
            "learned worker の結果がこの process の snapshot に紐づかない",
            extra={
                logs.FIELDS_KEY: {
                    "seq": proposal.seq,
                    "computed_at_ms": proposal.computed_at_ms,
                }
            },
        )
        return False

    def _learned_status(
        self,
        learned: MpcProposal | None,
        *,
        unavailable: LearnedChannelState | None,
        snapshot_status: SnapshotStatus,
        supervisor_available: bool,
        control_deadline_exceeded: bool,
    ) -> LearnedControlStatus:
        source_mono_ms = None if learned is None else self._learned_source_mono_ms(learned)
        if learned is not None and learned.proposal is not None and source_mono_ms is None:
            # `_poll_learned` で元 snapshot を特定した結果だけが来るので、ここへは来ない想定。
            # 来ても元 snapshot の時刻を言えない提案は使わない（新しさを確かめられない）
            learned = None
        if learned is None:
            return LearnedControlStatus(
                supervisor_available=supervisor_available,
                control_deadline_exceeded=control_deadline_exceeded,
                snapshot_status=snapshot_status,
                unavailable_detail=unavailable,
            )
        received_mono_ms = self._learned_received_mono_ms
        assert received_mono_ms is not None
        return learned.to_status(
            received_at_mono_ms=received_mono_ms,
            source_snapshot_mono_ms=source_mono_ms,
            snapshot_status=snapshot_status,
            supervisor_available=supervisor_available,
            control_deadline_exceeded=control_deadline_exceeded,
        )

    def _apply(self, composed: ComposedDemands) -> PerZone[FanHardwareResult] | None:
        """合成済み command だけを Backend へ渡す。失敗は次 tick の fault にする。"""
        try:
            results = self._backend.apply(composed)
        except Exception as error:
            LOGGER.exception("fan hardware backend failed")
            detail = f"{type(error).__name__}: {error}"[:500]
            self._pending_faults += tuple(
                Fault(code=FaultCode.WRITE_FAILURE, zone=zone, detail=detail) for zone in Zone
            )
            self._fans = None
            return None
        self._fans = PerZone[FanState](
            front=_fan_state(composed.front, results.front),
            rear=_fan_state(composed.rear, results.rear),
            top=_fan_state(composed.top, results.top),
        )
        self._pending_faults += tuple(
            result.fault
            for result in (results.front, results.rear, results.top)
            if result.fault is not None
        )
        return results

    def _observe_authority(
        self,
        *,
        safety_state: SafetyState,
        selection: ControllerSelection | None,
        now_mono_ms: int,
    ) -> None:
        gate = None if selection is None else selection.model_gate
        try:
            demotion = self._authority.observe(
                safety_state=safety_state,
                demotion_recommended=selection is not None and selection.demotion_recommended,
                confidence_level=None if gate is None else gate.confidence_level,
                ood=None if gate is None else gate.ood,
                now_mono_ms=now_mono_ms,
            )
        except Exception:
            # 降格を記録できなくても制御は続ける（#92 が理由を持つ）。上げる経路は無い。
            LOGGER.exception("authority runtime observation failed")
            return
        if demotion is None:
            return
        if demotion.persisted:
            LOGGER.warning(
                "authority stage を下げた",
                extra={logs.FIELDS_KEY: {"to_stage": demotion.to_stage.value}},
            )
            return
        # **書き残せなかった降格を黙って流さない。** memory 上の上限は下がったままだが、
        # 再起動で戻るので、運用者が直せるように残す（0057 §2.6 / 0060 §2.7）。
        failure = demotion.persist_failure
        LOGGER.error(
            "authority stage を下げたが書き残せなかった（再起動で戻る）",
            extra={
                logs.FIELDS_KEY: {
                    "to_stage": demotion.to_stage.value,
                    "persist_failure": None if failure is None else failure.model_dump(mode="json"),
                }
            },
        )

    def _control_state(
        self,
        *,
        mode: ModeCommand,
        selection: ControllerSelection | None,
        safety_state: SafetyState,
        supervisor_decision: SupervisorDecision | None,
        regime_estimate: WorkloadRegimeEstimate | None,
        coordination_bypassed: bool,
    ) -> ControlState:
        set_by_people = mode.mode in {OperatingMode.MANUAL, OperatingMode.CALIBRATION}
        stage = self._effective_stage(selection)
        gate = None if selection is None else selection.model_gate
        active: ControllerKind | None = None
        if not set_by_people:
            active = ControllerKind.FALLBACK if selection is None else selection.active_controller
        fallback_reason = None if selection is None else selection.fallback_reason
        if (
            active is ControllerKind.FALLBACK
            and fallback_reason is None
            and mode.mode is OperatingMode.AUTO
            and stage is not AuthorityStage.SHADOW
            and safety_state is SafetyState.NORMAL
        ):
            # Gate を通せなかった tick。理由の無い Fallback を trace に残さない。
            # 協調の失敗で迂回した tick は Gate の失敗と分ける（決定記録 0085 §2.2）。
            fallback_reason = (
                Reason(
                    code=AIR_BALANCE_COORDINATION_FAILED,
                    detail="Air Balance の協調が失敗したため Controller Gate を迂回した",
                )
                if coordination_bypassed
                else Reason(
                    code="controller_gate_unavailable",
                    detail="Controller Gate がこの tick の選択を返さなかった",
                )
            )
        selected = None if supervisor_decision is None else supervisor_decision.selected_output
        return ControlState(
            operating_mode=mode.mode,
            authority_stage=stage,
            active_controller=active,
            safety_state=safety_state,
            fallback_active=active is ControllerKind.FALLBACK,
            fallback_reason=fallback_reason,
            supervisor_policy=None if selected is None else selected.policy.value,
            workload_regime=None if regime_estimate is None else regime_estimate.regime,
            regime_confidence=None if regime_estimate is None else regime_estimate.confidence,
            model_version=None if gate is None else gate.model_version,
            model_confidence=None if gate is None else gate.confidence,
            model_ood=None if gate is None else gate.ood,
        )

    def _effective_stage(self, selection: ControllerSelection | None) -> AuthorityStage:
        """この tick に効いていた制御権。Gate を呼ばない mode でも残す（0057 §2.7）。"""
        if selection is not None:
            return selection.authority_stage
        # Gate と同じ式で数える。**設定の上限を超えない**（#79 / 決定記録 0057 §2.2）。
        return lowest_stage(self._authority.current_stage(), self._config.policy.authority_stage)

    def _air_balance_record(
        self,
        snapshot: ControlStateSnapshot,
        *,
        hardware: PerZone[FanHardwareResult] | None,
        faults: tuple[Fault, ...],
    ) -> tuple[PerZone[float | None], AirBalanceRecord]:
        """applied demand から zone の風量と Air Balance の記録を作る（決定記録 0073 §2.5）。

        **回っていない・確かめられていない Fan に風量を書かない。** 次の zone は None にする。

        - この tick の書き込み結果が無い、または ``write_ok`` / ``readback_ok`` が偽
          （backend が ``applied_demand`` を返さない）
        - **この tick の** backend の fault がある（stall 窓が満ちる前の ``TACH_STALL`` を含む。
          simulated の stall は write / readback が成功のまま返るため、結果を直接見る）
        - その zone の stall が Critical Safety の有効な fault に含まれる
        """
        stalled = frozenset(
            fault.zone
            for fault in faults
            if fault.code is FaultCode.TACH_STALL and fault.zone is not None
        )

        def applied(zone: Zone) -> float | None:
            result = None if hardware is None else hardware.get(zone)
            if result is None or result.fault is not None or zone in stalled:
                return None
            return result.applied_demand

        demands = PerZone[float | None](
            front=applied(Zone.FRONT), rear=applied(Zone.REAR), top=applied(Zone.TOP)
        )
        return self._air_balance.record(demands, self._air_balance.thermal_inputs(snapshot))

    @staticmethod
    def _zone_records(
        requested: PerZone[ZoneRequest],
        effective: PerZone[EffectiveZoneDemand],
        hardware: PerZone[FanHardwareResult] | None,
        flows: PerZone[float | None],
    ) -> PerZone[ZoneRecord]:
        def record(zone: Zone) -> ZoneRecord:
            result = None if hardware is None else hardware.get(zone)
            return ZoneRecord(
                controller_reason=requested.get(zone).reason,
                demand=effective.get(zone),
                airflow_index=None if result is None else result.airflow_index,
                estimated_flow=flows.get(zone),
                applied_demand=None if result is None else result.applied_demand,
                hardware=None if result is None else result.readback,
            )

        return PerZone[ZoneRecord](
            front=record(Zone.FRONT), rear=record(Zone.REAR), top=record(Zone.TOP)
        )

    def _shadow_record(
        self,
        *,
        tick_id: int,
        ts_ms: int,
        state: ControlState,
        effective: PerZone[EffectiveZoneDemand],
        selection: ControllerSelection | None,
        baseline: ControllerProposal | None,
        learned: MpcProposal | None,
        supervisor: SupervisorOutput | None,
    ) -> ShadowRecord | None:
        if self._shadow is None or selection is None or baseline is None:
            return None
        demands = PerZone[Demand](
            front=effective.front.effective,
            rear=effective.rear.effective,
            top=effective.top.effective,
        )
        try:
            return self._shadow.record(
                tick_id=tick_id,
                ts_ms=ts_ms,
                state=state,
                effective=demands,
                selection=selection,
                baseline=baseline,
                learned=learned,
                supervisor=supervisor,
            )
        except Exception:
            # 記録の失敗で制御を止めない（決定記録 0053 §2.5）。
            LOGGER.exception("shadow record failed", extra={logs.FIELDS_KEY: {"tick_id": tick_id}})
            return None

    def _beat(self) -> None:
        """外部の deadman へ「この tick を書き終えた」と伝える。

        **watchdog の失敗で冷却を止めない。** 送れなかったことは記録に残し、運転は続ける。
        本当に heartbeat が途切れていれば、外の systemd が時間切れで終わらせ、
        引き継ぎで Max になる（0028 §2.6 / §2.7、決定記録 0060 §2.7）。
        """
        try:
            self._watchdog.notify()
        except Exception:
            LOGGER.exception("watchdog へ heartbeat を送れなかった")

    def _record(self, tick: ControlTick) -> tuple[bool, bool]:
        """decision trace を保存する。返すのは（保存できたか, 失敗したか）。

        **制御は止めない**（0060 §2.6）。保存先の待ち時間は接続側で上限を掛けてあるので、
        ここで待ち続けて次の tick を遅らせることはない（0060 §2.7）。落ちた記録は
        呼び出し側が数えられるように、失敗を戻り値で返す。
        """
        if self._trace is None:
            return False, False
        try:
            return self._trace.record(tick), False
        except Exception:
            LOGGER.exception(
                "control trace record failed",
                extra={logs.FIELDS_KEY: {"tick_id": tick.tick_id}},
            )
            return False, True


def _proposal_demands(proposal: ControllerProposal) -> PerZone[Demand]:
    """提案の zone ごとの requested demand。"""
    return PerZone[Demand](
        front=proposal.requested.front.demand,
        rear=proposal.requested.rear.demand,
        top=proposal.requested.top.demand,
    )


def _coordination_record(
    result: CoordinatorResult, projected: ProjectedFloors
) -> AirBalanceCoordinationRecord:
    """``coordinate()`` を呼んだ tick の trace の塊（決定記録 0078 §2.7）。"""
    coordination = result.coordination
    return AirBalanceCoordinationRecord(
        mode=result.mode,
        status=AirBalanceCoordinationStatus(result.status),
        candidate=result.candidate,
        proposed=result.proposed,
        output=result.output,
        counterfactual_output=result.counterfactual_output,
        max_raise=result.max_raise,
        bounded_by_max_raise=result.bounded_by_max_raise,
        held=result.held,
        projected_floors=result.projected_floors,
        projected_floor_basis=projected.basis,
        before_state=AirBalanceTraceState(coordination.before.state.value),
        before_ratio=coordination.before.balance_ratio,
        projected_state=AirBalanceTraceState(coordination.projected.state.value),
        projected_ratio=coordination.projected.balance_ratio,
        # code の値は record の型（Literal）が検証する。
        reasons=cast(
            tuple[AirBalanceCoordinationReasonCode, ...],
            tuple(reason.code for reason in coordination.reasons),
        ),
    )


def _coordinated_baseline(
    baseline: ControllerProposal, result: CoordinatorResult
) -> ControllerProposal:
    """協調の ``output`` を requested にした Fallback の提案（Gate へ渡す Baseline）。

    上げた zone だけ値と理由を差し替え、detail に raw baseline の理由と ``candidate`` /
    ``output`` を残す（``fallback_coordinated_max`` と同じ形。決定記録 0078 §2.7）。
    shadow と ``not_needed`` は ``output == candidate`` なので raw baseline のまま返す。
    """
    requests: dict[Zone, ZoneRequest] = {}
    for zone in Zone:
        raw = baseline.requested.get(zone)
        output = result.output.get(zone)
        if output <= raw.demand:
            requests[zone] = raw
            continue
        code = (
            AIR_BALANCE_RELEASE_HOLD_REASON
            if result.held.get(zone)
            else AIR_BALANCE_ZONE_REASONS[zone]
        )
        detail = "; ".join(
            (
                f"candidate={raw.demand:.6f}",
                f"output={output:.6f}",
                f"baseline_reason={raw.reason.code}",
                f"baseline_detail={raw.reason.detail}",
            )
        )
        requests[zone] = ZoneRequest(demand=output, reason=Reason(code=code, detail=detail[:500]))
    if all(requests[zone] is baseline.requested.get(zone) for zone in Zone):
        return baseline
    return baseline.model_copy(
        update={
            "requested": PerZone[ZoneRequest](
                front=requests[Zone.FRONT], rear=requests[Zone.REAR], top=requests[Zone.TOP]
            )
        }
    )


def _applied_demands(hardware: PerZone[FanHardwareResult] | None) -> PerZone[Demand | None]:
    """frame の `applied`（決定記録 0092）。**確かめられない zone は欠測のまま**にする。

    結果が無い tick（Backend の例外）は3 zone とも None。zone の `applied_demand` が None
    （書き込み・readback を確かめられない）ならその zone だけ None。effective demand で埋めない。
    """
    if hardware is None:
        return PerZone[Demand | None](front=None, rear=None, top=None)
    return PerZone[Demand | None](
        front=hardware.front.applied_demand,
        rear=hardware.rear.applied_demand,
        top=hardware.top.applied_demand,
    )


def _fan_state(demand: EffectiveZoneDemand, result: FanHardwareResult) -> FanState:
    """次 tick の Safety が読む actuator の帰還。**指令ではない。**"""
    return FanState(
        effective_demand=demand.effective,
        pwm_raw=result.readback.pwm_raw,
        rpm=result.readback.rpm,
    )
