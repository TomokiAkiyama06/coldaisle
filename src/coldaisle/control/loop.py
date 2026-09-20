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
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle import logs
from coldaisle.clock import Clock, MonotonicClock
from coldaisle.control.config import ControlConfig, FanPolicyConfig
from coldaisle.control.fallback.controller import FallbackController
from coldaisle.control.fallback.gate import (
    ControllerGate,
    ControllerSelection,
    LearnedControlStatus,
    SnapshotStatus,
)
from coldaisle.control.hardware.simulated import FanHardwareBackend, FanHardwareResult
from coldaisle.control.logging import ControlTraceLogger
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.reactive.guard import ReactiveGuard, guard_input_metrics
from coldaisle.control.safety.critical import (
    AIR_TELEMETRY_GROUP,
    AIR_TEMPERATURE_METRICS,
    CPU_POWER_METRIC,
    CPU_TEMPERATURE_METRIC,
    GPU_TEMPERATURE_METRIC,
    ComposedDemands,
    CriticalSafety,
    DemandComposer,
)
from coldaisle.control.schema import (
    BASELINE_STAGE,
    SCHEMA_VERSION,
    AuthorityStage,
    ConfidenceLevel,
    ControlConfigDigest,
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
    OperatingMode,
    PerZone,
    Reason,
    SafetyState,
    ShadowRecord,
    SupervisorDecision,
    SupervisorOutput,
    Zone,
    ZoneRecord,
    ZoneRequest,
    lowest_stage,
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
    ReceivedSupervisorOutput,
    SupervisorCoordinator,
    SupervisorInput,
)
from coldaisle.control.supervisor.regime import (
    WorkloadRegimeEstimate,
    WorkloadRegimeEstimator,
    WorkloadRegimeState,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store.models import Quality, validate_metric

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


class ModeCommand(_Frozen):
    """人が決めた運転モードと、人が決めた requested（0028 §2.5 (a)）。

    `MANUAL` / `CALIBRATION` では人が requested を決めるので、その値をここで運ぶ。
    `MAX` は **requested では表さない**（Critical Safety の `forced_max` が所有する）。
    """

    mode: OperatingMode = OperatingMode.AUTO
    requested: PerZone[ZoneRequest] | None = None

    @model_validator(mode="after")
    def _people_set_demands_only_where_they_own_them(self) -> Self:
        owns_requested = self.mode in {OperatingMode.MANUAL, OperatingMode.CALIBRATION}
        if owns_requested != (self.requested is not None):
            raise ValueError("requested を持てるのは MANUAL / CALIBRATION だけ（0028 §2.5 (a)）")
        return self


class OperatingModeSource(Protocol):
    """運転モードの読み取り専用の窓口。**LLM からは到達できない**（AGENTS.md ルール1）。"""

    def current(self) -> ModeCommand:
        """いまのモード。入口（ローカル Unix ソケット）の配線は #60（0028 未決 3）。

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
    """worker が置いた最新の RL Supervisor 出力を読むだけの窓口。"""

    def poll(self) -> SupervisorOutput | None:
        """最新の RL 出力。**同じ出力を何度返してもよい。**"""
        ...


class AuthorityObserver(Protocol):
    """#92 の `AuthorityRuntime` のうち、loop が使う読み取りと観測だけの面。

    **上げる経路を持たない。** 昇格は人の承認だけが行う（0028 §2.5 (b)）。
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
    ) -> object:
        """この tick の健全性を渡す。戻り値は loop では使わない。"""
        ...


class StaticAuthority:
    """journal を持たない構成に使う、**固定 stage の** authority（既定は `SHADOW`）。

    **設定の `authority_stage` を既定にしない。** 配線を忘れた起動が設定の**上限**を
    そのまま制御権にすると、`full` と書かれた設定だけで Learned MPC が実 Fan を握る
    （#79 `ControllerGate.__init__` と同じ理由）。降格の記録は持たないので `observe` は
    何もしない。昇格の経路はどこにも無い。
    """

    __slots__ = ("_stage",)

    def __init__(self, stage: AuthorityStage = BASELINE_STAGE) -> None:
        self._stage = stage

    def current_stage(self) -> AuthorityStage:
        """固定された stage を返す。"""
        return self._stage

    def observe(
        self,
        *,
        safety_state: SafetyState,
        demotion_recommended: bool,
        confidence_level: ConfidenceLevel | None,
        ood: bool | None,
        now_mono_ms: int,
    ) -> None:
        """記録を持たないので何もしない。"""


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

    return ControlInputContract(
        signals=tuple(specs[metric] for metric in sorted(specs)),
        critical_groups=(CriticalTelemetryGroup(code=AIR_TELEMETRY_GROUP, metrics=air_metrics),),
    )


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
        mode_source: OperatingModeSource | None = None,
        supervisor: SupervisorCoordinator | None = None,
        regime: WorkloadRegimeEstimator | None = None,
        learned_source: LearnedProposalSource | None = None,
        rl_supervisor_source: SupervisorOutputSource | None = None,
        shadow: ShadowRecorder | None = None,
        trace: ControlTraceLogger | None = None,
        authority: AuthorityObserver | None = None,
        watchdog: Watchdog | None = None,
    ) -> None:
        if (supervisor is None) != (regime is None):
            raise ValueError("Supervisor と Workload Regime 推定は一緒に配線する")
        if rl_supervisor_source is not None and supervisor is None:
            raise ValueError("RL Supervisor worker を配線するなら Supervisor も配線する")
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
        self._supervisor = supervisor
        self._regime = regime
        self._learned_source = learned_source
        self._rl_supervisor_source = rl_supervisor_source
        self._shadow = shadow
        self._trace = trace
        self._authority: AuthorityObserver = (
            authority if authority is not None else StaticAuthority()
        )
        self._watchdog: Watchdog = watchdog if watchdog is not None else NullWatchdog()

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
        self._rl_output: SupervisorOutput | None = None
        self._rl_received_mono_ms: int | None = None
        # RL 出力の元 snapshot を loop 自身の単調時計で特定するための窓。有効期限より
        # 長く持っても使えないので、設定から幅を決める（固定長にしない）。
        window = config.policy.supervisor.valid_ms // config.safety.tick_ms.value + 2
        self._snapshots: deque[_SnapshotIdentity] = deque(maxlen=window)
        self._config_digest = ControlConfigDigest(
            fan_hardware_sha256=config.sources.fan_hardware.sha256,
            safety_sha256=config.sources.safety.sha256,
            policy_sha256=config.sources.policy.sha256,
        )

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

        snapshot, snapshot_status = self._snapshot(tick_id, ts_ms, started_mono_ms)
        self._snapshots.append(
            _SnapshotIdentity(
                tick_id=snapshot.tick_id,
                ts_ms=snapshot.ts_ms,
                schema_version=snapshot.schema_version,
                monotonic_ms=snapshot.monotonic_ms,
            )
        )
        mode = self._mode_command()
        supervisor_decision, regime_estimate = self._run_supervisor(
            snapshot, now_mono_ms=snapshot.monotonic_ms
        )
        supervisor_output = (
            None if supervisor_decision is None else supervisor_decision.selected_output
        )
        guard_zones, guard_fault = self._run_guard(snapshot)
        baseline, controller_fault = self._run_fallback(snapshot, snapshot_status)
        learned = self._poll_learned(snapshot.monotonic_ms)

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
        selection, gate_fault = self._select(
            snapshot=snapshot,
            snapshot_status=snapshot_status,
            mode=mode,
            baseline=baseline,
            learned=learned,
            safety_state=safety_decision.state,
            supervisor_available=supervisor_output is not None,
            started_mono_ms=started_mono_ms,
        )
        if gate_fault is not None:
            self._pending_faults += (gate_fault,)
        requested = self._requested(mode, baseline, selection)
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
        )
        tick = ControlTick(
            schema_version=SCHEMA_VERSION,
            tick_id=tick_id,
            ts_ms=ts_ms,
            state=state,
            zones=self._zone_records(requested, effective, hardware),
            supervisor=supervisor_decision,
            model_gate=None if selection is None else selection.model_gate,
            shadow=self._shadow_record(
                tick_id=tick_id,
                ts_ms=ts_ms,
                state=state,
                effective=effective,
                selection=selection,
                baseline=baseline,
                learned=learned,
                supervisor=supervisor_output,
            ),
            faults=safety_decision.faults,
            runtime=ControlTickRuntime(
                tick_period_ms=self.tick_period_ms,
                deadline_ms=self.tick_deadline_ms,
                duration_ms=duration_ms,
                deadline_exceeded=overrun,
                snapshot_schema_version=snapshot.schema_version,
                config=self._config_digest,
            ),
        )
        recorded, trace_failed = self._record(tick)

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
        """RL worker の出力を、**loop 自身の単調時計に束縛してから**渡す。"""
        if self._rl_supervisor_source is None:
            return None, None
        try:
            output = self._rl_supervisor_source.poll()
        except Exception:
            LOGGER.exception("supervisor worker poll failed")
            return None, Reason(code="supervisor_unavailable", detail="worker poll failed")
        if output is None:
            return None, None
        if output != self._rl_output:
            self._rl_output = output
            self._rl_received_mono_ms = now_mono_ms
        received_mono_ms = self._rl_received_mono_ms
        assert received_mono_ms is not None
        # **tick 番号だけで照合しない。** 番号は再起動で 0 に戻るので、前の process の出力が
        # 新しい process の無関係な snapshot に結び付く。壁時計の時刻と snapshot の形まで
        # 一致した記録だけを「この loop が出した snapshot」とみなす（0060 §2.6）。
        source = next(
            (
                item
                for item in self._snapshots
                if item.tick_id == output.tick_id
                and item.ts_ms == output.ts_ms
                and item.schema_version == output.snapshot_schema_version
            ),
            None,
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
            ),
            None,
        )

    def _run_guard(
        self, snapshot: ControlStateSnapshot
    ) -> tuple[PerZone[GuardZoneOutput], Fault | None]:
        try:
            decision = self._guard.evaluate(snapshot)
        except Exception as error:
            LOGGER.exception(
                "reactive guard failed",
                extra={logs.FIELDS_KEY: {"tick_id": snapshot.tick_id}},
            )
            empty = GuardZoneOutput()
            return (
                PerZone[GuardZoneOutput](front=empty, rear=empty, top=empty),
                Fault(
                    code=FaultCode.GUARD_EXCEPTION,
                    detail=f"{type(error).__name__}: {error}"[:500],
                ),
            )
        return decision.zones, None

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

    def _select(
        self,
        *,
        snapshot: ControlStateSnapshot,
        snapshot_status: SnapshotStatus,
        mode: ModeCommand,
        baseline: ControllerProposal | None,
        learned: MpcProposal | None,
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

    def _poll_learned(self, now_mono_ms: int) -> MpcProposal | None:
        """worker 結果を読み、**初めて見た結果にだけ**受信時刻を押す。

        毎 tick 現在時刻を押すと、worker が止まって同じ結果を返し続けても永久に期限切れに
        ならない。識別子（`MpcProposal.result_digest()`）で新旧を見分ける。
        """
        if self._learned_source is None:
            return None
        try:
            result = self._learned_source.poll()
        except Exception:
            LOGGER.exception("learned worker poll failed")
            return None
        if result is None:
            # **識別子と受信時刻を消さない。** 消すと、worker が一時的に読めなくなった
            # あとで同じ提案が出てきたときに新しい受信時刻を押してしまい、止まった worker の
            # 古い提案が何度でも有効期限を取り戻す（決定記録 0060 §2.6）。
            return None
        digest = result.result_digest()
        if digest != self._learned_digest:
            self._learned_digest = digest
            self._learned_received_mono_ms = now_mono_ms
        return result

    def _learned_status(
        self,
        learned: MpcProposal | None,
        *,
        snapshot_status: SnapshotStatus,
        supervisor_available: bool,
        control_deadline_exceeded: bool,
    ) -> LearnedControlStatus:
        if learned is None:
            return LearnedControlStatus(
                supervisor_available=supervisor_available,
                control_deadline_exceeded=control_deadline_exceeded,
                snapshot_status=snapshot_status,
            )
        received_mono_ms = self._learned_received_mono_ms
        assert received_mono_ms is not None
        return learned.to_status(
            received_at_mono_ms=received_mono_ms,
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
            self._authority.observe(
                safety_state=safety_state,
                demotion_recommended=selection is not None and selection.demotion_recommended,
                confidence_level=None if gate is None else gate.confidence_level,
                ood=None if gate is None else gate.ood,
                now_mono_ms=now_mono_ms,
            )
        except Exception:
            # 降格を記録できなくても制御は続ける（#92 が理由を持つ）。上げる経路は無い。
            LOGGER.exception("authority runtime observation failed")

    def _control_state(
        self,
        *,
        mode: ModeCommand,
        selection: ControllerSelection | None,
        safety_state: SafetyState,
        supervisor_decision: SupervisorDecision | None,
        regime_estimate: WorkloadRegimeEstimate | None,
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
            fallback_reason = Reason(
                code="controller_gate_unavailable",
                detail="Controller Gate がこの tick の選択を返さなかった",
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

    @staticmethod
    def _zone_records(
        requested: PerZone[ZoneRequest],
        effective: PerZone[EffectiveZoneDemand],
        hardware: PerZone[FanHardwareResult] | None,
    ) -> PerZone[ZoneRecord]:
        def record(zone: Zone) -> ZoneRecord:
            result = None if hardware is None else hardware.get(zone)
            return ZoneRecord(
                controller_reason=requested.get(zone).reason,
                demand=effective.get(zone),
                airflow_index=None if result is None else result.airflow_index,
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


def _fan_state(demand: EffectiveZoneDemand, result: FanHardwareResult) -> FanState:
    """次 tick の Safety が読む actuator の帰還。**指令ではない。**"""
    return FanState(
        effective_demand=demand.effective,
        pwm_raw=result.readback.pwm_raw,
        rpm=result.readback.rpm,
    )
