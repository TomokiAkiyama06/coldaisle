"""#74 Control Engine / daemon の不変条件。

**不変条件を先に書き、1つずつ破ろうとする test を置く。**

1. 1 tick が使う State Snapshot は1つだけで、層をまたいでも読み直さない
2. tick を重ねない
3. 起動・再起動は必ず `STARTUP` の全 zone Max から始まる（危険な低回転で固定されない）
4. 再起動でモードは `AUTO` に戻り、`MANUAL` の低い値を持ち越さない
5. 上位層は PWM を表現できない（型に `pwm` が無く、余分なキーを拒む）
6. どの mode でも Critical Safety の floor と Reactive Guard を迂回できない
7. Top は CPU cooling floor を必ず守る（`CALIBRATION` でも外れない）
8. `MAX` はすべてに勝ち、requested では表さない
9. 上げる速さは制限せず、下げる速さだけを制限する
10. Guard / Fallback / Gate の例外は握りつぶさず fault へ翻訳する
11. Critical Safety と合成の例外は**捕まえない**
12. tick の締め切り超過を検出し、連続したら安全側へ縮退する
13. Telemetry の新しさは loop 自身の単調時計で、`ts_ms` の変化から数える
14. worker 結果の受信時刻は**初めて見た tick**に押し、毎 tick 更新しない
15. 締め切りを過ぎた tick では ML を通さない（安全側が I/O を待たない）
16. Supervisor / worker が止まっても Baseline で運転を続ける
17. Front / Rear / Top を独立に動かせる
18. 同じ入力・同じ時計・同じ設定なら decision trace が再現する
19. #103 で検証できた設定だけが active になる
20. 遅れた tick を後から取り戻して連続実行しない
21. heartbeat は書き込みと検証の直後に出し、記録や降格の I/O で遅らせない
22. 完了しなかった tick は heartbeat を出さない（deadman が効く）
23. 外部の deadman が無いことを黙って no-op にしない
24. deadman の有無は `WATCHDOG_USEC` で決め、通知先の存在を証拠にしない
25. decision trace の保存は heartbeat の間隔を食いつぶせない（待ちに上限がある）
26. 起動時の失敗を**別の種類**として報告しない（設定不正と環境の失敗を混ぜない）
27. worker の結果は、**この process が出した snapshot**に紐づくものだけを受け取る
28. 降格の書き残しは無期限に待たず、失敗が見える
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.clock import ManualMonotonicClock, SimulatedClock
from coldaisle.control.config import ControlConfig, FanPolicyConfig, SafetyConfig
from coldaisle.control.fallback.controller import FallbackController
from coldaisle.control.fallback.gate import ControllerGate, LearnedControlStatus, SnapshotStatus
from coldaisle.control.hardware.simulated import SimulatedFanBackend, SimulatedFaultPlan
from coldaisle.control.logging import ControlTraceLogger
from coldaisle.control.loop import (
    ControlLoop,
    ControlLoopBusyError,
    ModeCommand,
    StaticAuthority,
    TelemetrySample,
    build_input_contract,
)
from coldaisle.control.reactive.guard import ReactiveGuard
from coldaisle.control.safety.critical import (
    CriticalSafety,
    DemandComposer,
    create_control_runtime_binding,
)
from coldaisle.control.schema import (
    BoundBy,
    ControllerKind,
    ControlTick,
    FaultCode,
    OperatingMode,
    PerZone,
    Reason,
    SafetyState,
    Zone,
    ZoneRequest,
)
from coldaisle.control.shadow.record import ShadowRecorder
from coldaisle.control.state import ControlStateEstimator
from coldaisle.control.supervisor.policy import SupervisorCoordinator
from coldaisle.control.supervisor.regime import WorkloadRegimeEstimator
from coldaisle.control_daemon import (
    EXIT_ACTUATION_NOT_APPROVED,
    EXIT_HARDWARE_CONFIG_INVALID,
    EXIT_STARTUP_ENVIRONMENT,
    EXIT_WATCHDOG_UNAVAILABLE,
    NOTIFY_SOCKET_ENV,
    WATCHDOG_DATAGRAM,
    WATCHDOG_PID_ENV,
    WATCHDOG_USEC_ENV,
    ActuationNotApprovedError,
    Config,
    ControlConfigInvalidError,
    ControlDaemon,
    ControlStats,
    StartupEnvironmentError,
    StoreTelemetrySource,
    SystemdWatchdog,
    UnsupervisedWatchdog,
    WatchdogUnavailableError,
    build,
    create_watchdog,
    heartbeat_interval_ms,
    main,
    run_config_invalid_max,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store.models import Quality
from conftest import TEST_EPOCH_MS
from test_control_config import valid_documents, write_documents
from test_simulated_fan_backend import hardware_config

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
METRICS_PATH = CONFIG_DIR / "metrics.yaml"

READINGS = {
    "cpu.package": 50.0,
    "gpu.0.core": 55.0,
    "gpu.0.hotspot": 65.0,
    "power.cpu.package": 80.0,
    "power.gpu.0": 200.0,
    "air.front_intake": 25.0,
    "air.gpu_intake": 27.0,
    "air.gpu_exhaust": 35.0,
    "air.top_exhaust": 33.0,
    "air.rear_exhaust": 32.0,
    "air.room": 24.0,
}
"""Guard が発火しない、落ち着いた運転の値。閾値との関係は test 側で崩す。"""


# ----------------------------------------------------------------- 足場


@pytest.fixture(scope="session")
def catalog() -> MetricCatalog:
    """**本番と同じ Metric Catalog** を読む。"""
    return MetricCatalog.from_yaml(METRICS_PATH)


def control_config(
    *,
    safety: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    confirmed_hardware: bool = True,
) -> ControlConfig:
    """検証済みの ControlConfig。hardware は実機の識別子を含まない測定済み設定を使う。"""
    documents = valid_documents()
    safety_document = documents["safety.yaml"] | (safety or {})
    policy_document = documents["fan-policy.yaml"] | (policy or {})
    sources = _sources(safety_document, policy_document)
    return ControlConfig(
        fan_hardware=hardware_config(confirmed=confirmed_hardware),
        safety=SafetyConfig.model_validate(safety_document),
        policy=FanPolicyConfig.model_validate(policy_document),
        sources=sources,
    )


def _sources(safety_document: dict[str, Any], policy_document: dict[str, Any]) -> Any:
    from hashlib import sha256

    from coldaisle.control.config import ConfigSource, ConfigSources

    def digest(document: dict[str, Any]) -> str:
        return sha256(yaml.safe_dump(document, sort_keys=True).encode("utf-8")).hexdigest()

    return ConfigSources(
        fan_hardware=ConfigSource(name="fan-hardware.yaml", schema_version=1, sha256="0" * 64),
        safety=ConfigSource(
            name="safety.yaml",
            schema_version=int(safety_document["schema_version"]),
            sha256=digest(safety_document),
        ),
        policy=ConfigSource(
            name="fan-policy.yaml",
            schema_version=int(policy_document["schema_version"]),
            sha256=digest(policy_document),
        ),
    )


@dataclass
class FakeTelemetry:
    """収集層の代わり。**`source_ts_ms` を止めれば、そのまま stale になる。**"""

    clock: SimulatedClock
    monotonic: ManualMonotonicClock
    values: dict[str, float] = field(default_factory=lambda: dict(READINGS))
    frozen_ts_ms: int | None = None
    error: Exception | None = None
    cost_ms: int = 0
    reads: int = 0

    def read(self) -> tuple[TelemetrySample, ...]:
        self.reads += 1
        if self.cost_ms:
            self.monotonic.advance_ms(self.cost_ms)
        if self.error is not None:
            raise self.error
        ts_ms = self.clock.now_ms() if self.frozen_ts_ms is None else self.frozen_ts_ms
        return tuple(
            TelemetrySample(metric=metric, value=value, quality=Quality.OK, source_ts_ms=ts_ms)
            for metric, value in sorted(self.values.items())
        )


@dataclass
class MutableMode:
    """人がいつでも変えられる運転モードの入口の代わり。"""

    command: ModeCommand = field(default_factory=ModeCommand)
    error: Exception | None = None

    def current(self) -> ModeCommand:
        if self.error is not None:
            raise self.error
        return self.command


@dataclass
class RecordingTrace:
    """decision trace の保存先。**制御の経路には触れない。**"""

    rows: list[str] = field(default_factory=list)
    error: Exception | None = None
    order: list[str] = field(default_factory=list)

    def record_control_trace(
        self, *, ts_ms: int, tick_id: int, schema_version: int, trace_json: str
    ) -> bool:
        self.order.append("trace")
        if self.error is not None:
            raise self.error
        self.rows.append(trace_json)
        return True


@dataclass
class RecordingWatchdog:
    """heartbeat の回数・時刻・tick の中での順番を記録する deadman。"""

    order: list[str] = field(default_factory=list)
    monotonic: ManualMonotonicClock | None = None
    beats: int = 0
    beats_at: list[int] = field(default_factory=list)
    error: Exception | None = None

    def notify(self) -> None:
        self.order.append("watchdog")
        self.beats += 1
        if self.monotonic is not None:
            self.beats_at.append(self.monotonic.monotonic_ms())
        if self.error is not None:
            raise self.error


@dataclass
class RecordingAuthority:
    """#92 の runtime の代わり。**journal の書き出しは I/O** なので順番を見る。"""

    inner: StaticAuthority
    order: list[str]

    def current_stage(self):  # type: ignore[no-untyped-def]
        return self.inner.current_stage()

    def observe(self, **kwargs: Any) -> None:
        self.order.append("authority")


@dataclass(frozen=True)
class StubProposalOrigin:
    """worker が読んだ snapshot（`ControllerProposal.seq` / `computed_at_ms` と同じ）。"""

    seq: int
    computed_at_ms: int


@dataclass
class StubWorkerResult:
    """worker 結果の代わり。loop が使うのは識別子と `to_status` だけである。"""

    digest: str = "a" * 64
    proposal: Any = None
    """この結果が読んだ snapshot（`seq` / `computed_at_ms`）。None は worker の失敗。"""
    seen_received_ms: list[int] = field(default_factory=list)
    seen_deadline_exceeded: list[bool] = field(default_factory=list)
    seen_snapshot_status: list[SnapshotStatus] = field(default_factory=list)

    def result_digest(self) -> str:
        return self.digest

    def to_status(
        self,
        *,
        received_at_mono_ms: int,
        snapshot_status: SnapshotStatus = SnapshotStatus.AVAILABLE,
        supervisor_available: bool = True,
        control_deadline_exceeded: bool = False,
    ) -> LearnedControlStatus:
        self.seen_received_ms.append(received_at_mono_ms)
        self.seen_deadline_exceeded.append(control_deadline_exceeded)
        self.seen_snapshot_status.append(snapshot_status)
        return LearnedControlStatus(
            supervisor_available=supervisor_available,
            control_deadline_exceeded=control_deadline_exceeded,
            snapshot_status=snapshot_status,
        )


@dataclass
class StubWorkerSource:
    result: Any = None
    error: Exception | None = None

    def poll(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


class Harness:
    """1つの ControlLoop と、それを進める時計・入力をまとめた足場。"""

    def __init__(
        self,
        catalog: MetricCatalog,
        *,
        config: ControlConfig | None = None,
        with_supervisor: bool = True,
        with_trace: bool = True,
        fault_plan: SimulatedFaultPlan | None = None,
        learned_source: Any = None,
        watchdog: Any = None,
        authority: Any = None,
        guard: Any = None,
        fallback: Any = None,
        gate: Any = None,
        safety: Any = None,
        composer: Any = None,
        backend: Any = None,
    ) -> None:
        self.config = config if config is not None else control_config()
        self.clock = SimulatedClock(TEST_EPOCH_MS)
        self.monotonic = ManualMonotonicClock(0)
        self.telemetry = FakeTelemetry(self.clock, self.monotonic)
        self.mode = MutableMode()
        self.order: list[str] = []
        self.watchdog = watchdog or RecordingWatchdog(self.order, monotonic=self.monotonic)
        self.trace = RecordingTrace(order=self.order)
        self.contract = build_input_contract(self.config, catalog)
        binding = create_control_runtime_binding(self.config)
        self.backend = backend or SimulatedFanBackend(
            config=self.config.fan_hardware,
            runtime_binding=binding,
            fault_plan=fault_plan or SimulatedFaultPlan(),
        )
        self.authority = StaticAuthority()
        self.loop = ControlLoop(
            config=self.config,
            estimator=ControlStateEstimator(self.contract, catalog),
            fallback=fallback or FallbackController(self.config.policy, catalog),
            gate=gate
            or ControllerGate(
                self.config.policy,
                expected_model_version="thermal-vtest",
                authority=self.authority,
            ),
            guard=guard or ReactiveGuard(self.config.policy.reactive_guard, catalog),
            safety=safety
            or CriticalSafety(
                self.config.safety, input_contract=self.contract, runtime_binding=binding
            ),
            composer=composer or DemandComposer(self.config.safety),
            backend=self.backend,
            telemetry=self.telemetry,
            clock=self.clock,
            monotonic=self.monotonic,
            mode_source=self.mode,
            supervisor=(
                SupervisorCoordinator(self.config.policy.supervisor, self.clock)
                if with_supervisor
                else None
            ),
            regime=(
                WorkloadRegimeEstimator(self.config.policy.workload_regime, catalog, self.clock)
                if with_supervisor
                else None
            ),
            learned_source=learned_source,
            shadow=ShadowRecorder(self.config.policy.shadow),
            trace=ControlTraceLogger(self.trace) if with_trace else None,
            authority=authority or RecordingAuthority(self.authority, self.order),
            watchdog=self.watchdog,
        )

    def tick(self, *, advance_ms: int | None = None) -> Any:
        """時計を進めてから1 tick 回す。**時計を進めるのは試験側だけ。**"""
        step = self.config.safety.tick_ms.value if advance_ms is None else advance_ms
        self.monotonic.advance_ms(step)
        self.clock.advance_to_ms(self.clock.now_ms() + step)
        return self.loop.tick()

    def run(self, ticks: int) -> list[Any]:
        return [self.tick() for _ in range(ticks)]

    def settle(self) -> Any:
        """`NORMAL` になるまで回す。STARTUP の settle と tach の確認を通す。"""
        result = None
        for _ in range(8):
            result = self.tick()
            if result.tick.state.safety_state is SafetyState.NORMAL:
                return result
        raise AssertionError(f"NORMAL にならなかった: {result.tick.state if result else None}")


def manual(front: float, rear: float, top: float) -> ModeCommand:
    return ModeCommand(
        mode=OperatingMode.MANUAL,
        requested=PerZone[ZoneRequest](
            front=ZoneRequest(demand=front, reason=Reason(code="operator")),
            rear=ZoneRequest(demand=rear, reason=Reason(code="operator")),
            top=ZoneRequest(demand=top, reason=Reason(code="operator")),
        ),
    )


# ------------------------------------------------- 1. 1 tick に snapshot は1つ


def test_invariant_1_one_tick_reads_telemetry_once_and_shares_one_snapshot(catalog) -> None:
    """**1 tick で入力を読み直さない。** 読み直すと層ごとに別の状態を見る。"""
    harness = Harness(catalog)
    harness.tick()

    assert harness.telemetry.reads == 1
    harness.tick()
    assert harness.telemetry.reads == 2


def test_invariant_1_every_layer_receives_the_same_snapshot_object(catalog) -> None:
    """Guard・Fallback・Safety が**同じ object**を受け取る（tick 中に作り直さない）。"""
    seen: list[int] = []

    class SpyGuard(ReactiveGuard):
        def evaluate(self, snapshot):  # type: ignore[no-untyped-def]
            seen.append(id(snapshot))
            return super().evaluate(snapshot)

    class SpyFallback(FallbackController):
        def propose(self, snapshot):  # type: ignore[no-untyped-def]
            seen.append(id(snapshot))
            return super().propose(snapshot)

    class SpySafety(CriticalSafety):
        def evaluate(self, snapshot, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(id(snapshot))
            return super().evaluate(snapshot, **kwargs)

    config = control_config()
    contract = build_input_contract(config, catalog)
    binding = create_control_runtime_binding(config)
    harness = Harness(
        catalog,
        config=config,
        guard=SpyGuard(config.policy.reactive_guard, catalog),
        fallback=SpyFallback(config.policy, catalog),
        safety=SpySafety(config.safety, input_contract=contract, runtime_binding=binding),
        backend=SimulatedFanBackend(config=config.fan_hardware, runtime_binding=binding),
    )
    harness.tick()

    assert len(seen) == 3
    assert len(set(seen)) == 1


# ------------------------------------------------------------- 2. tick を重ねない


def test_invariant_2_a_tick_cannot_start_while_another_tick_is_running(catalog) -> None:
    """**tick の中から tick を呼べない。** 重なると2つの command が同じ Backend へ並ぶ。"""
    harness = Harness(catalog)
    real = harness.loop._backend
    reentered: list[str] = []

    class Reentrant:
        def apply(self, demands):  # type: ignore[no-untyped-def]
            with pytest.raises(ControlLoopBusyError):
                harness.loop.tick()
            reentered.append("refused")
            return real.apply(demands)

    harness.loop._backend = Reentrant()  # type: ignore[assignment]
    result = harness.tick()

    assert reentered == ["refused"]
    # 内側の呼び出しは tick にならない（tick_id も trace も増えない）。
    assert result.tick.tick_id == 0
    assert len(harness.trace.rows) == 1


def test_invariant_2_the_busy_flag_itself_refuses_a_second_tick(catalog) -> None:
    """重なりの判定は flag そのもので閉じる（呼び出し元の作法に頼らない）。"""
    harness = Harness(catalog)
    harness.loop._in_tick = True

    with pytest.raises(ControlLoopBusyError):
        harness.loop.tick()


# ------------------------------------------- 3 / 4. 起動・再起動は Max と AUTO から


def test_invariant_3_the_first_tick_forces_every_zone_to_max(catalog) -> None:
    """制御を取った直後は全 zone Max（0028 §2.5 (d)）。**低い値から始めない。**"""
    harness = Harness(catalog)
    result = harness.tick()

    assert result.tick.state.safety_state is SafetyState.STARTUP
    for zone in Zone:
        demand = result.tick.zones.get(zone).demand
        assert demand.forced_max is True
        assert demand.effective == 1.0
        assert demand.bound_by is BoundBy.FORCED_MAX
    assert result.hardware is not None
    assert all(result.hardware.get(zone).readback.pwm_raw == 255 for zone in Zone)


def test_invariant_3_a_restart_starts_again_from_startup_max_not_from_the_last_demand(
    catalog,
) -> None:
    """**再起動で低い値を引き継がない。** 落ちた瞬間の demand を復元しない。"""
    first = Harness(catalog)
    first.settle()
    first.mode.command = manual(0.0, 0.0, 0.0)
    settled = first.tick()
    assert settled.tick.zones.front.demand.effective < 1.0

    restarted = Harness(catalog)
    result = restarted.tick()

    assert result.tick.state.safety_state is SafetyState.STARTUP
    assert all(result.tick.zones.get(zone).demand.effective == 1.0 for zone in Zone)


def test_invariant_4_a_restart_returns_to_auto(catalog) -> None:
    """再起動後の mode は `AUTO`（0028 §2.5 (a)）。`MANUAL` を持ち越さない。"""
    first = Harness(catalog)
    first.settle()
    first.mode.command = manual(0.1, 0.1, 0.1)
    assert first.tick().tick.state.operating_mode is OperatingMode.MANUAL

    restarted = Harness(catalog)

    assert restarted.tick().tick.state.operating_mode is OperatingMode.AUTO


def test_invariant_4_a_failing_mode_source_keeps_the_previous_mode(catalog) -> None:
    """入口が壊れても**安全でない側へ勝手に戻さない**（`MAX` を解除しない）。"""
    harness = Harness(catalog)
    harness.settle()
    harness.mode.command = ModeCommand(mode=OperatingMode.MAX)
    assert harness.tick().tick.state.operating_mode is OperatingMode.MAX

    harness.mode.error = RuntimeError("ソケットが切れた")
    result = harness.tick()

    assert result.tick.state.operating_mode is OperatingMode.MAX
    assert all(result.tick.zones.get(zone).demand.effective == 1.0 for zone in Zone)


# ------------------------------------------------------ 5. 上位層は PWM を持てない


def test_invariant_5_upper_layers_cannot_express_a_raw_pwm() -> None:
    """requested の型に PWM が無く、余分なキーも通らない（0028 §2.3）。"""
    with pytest.raises(ValidationError):
        ZoneRequest.model_validate(
            {"demand": 0.5, "reason": {"code": "x"}, "pwm": 255},
        )
    with pytest.raises(ValidationError):
        ModeCommand.model_validate({"mode": "manual", "pwm": 255})
    assert "pwm" not in ZoneRequest.model_fields
    assert "pwm" not in ModeCommand.model_fields


def test_invariant_5_the_control_loop_never_touches_hwmon() -> None:
    """loop の source に hwmon / sysfs への経路が無い（書けるのは #77 だけ）。"""
    source = (Path(__file__).resolve().parents[1] / "src/coldaisle/control/loop.py").read_text(
        encoding="utf-8"
    )

    for forbidden in ("hwmon", "sysfs", "subprocess", "/sys/"):
        assert forbidden not in source


def test_invariant_5_a_human_request_cannot_carry_a_mode_without_demands() -> None:
    """`MAX` を requested で表さない（0028 §2.4 の「Max はすべてに勝つ」を崩さない）。"""
    with pytest.raises(ValidationError, match="MANUAL / CALIBRATION"):
        ModeCommand(
            mode=OperatingMode.MAX,
            requested=PerZone[ZoneRequest](
                front=ZoneRequest(demand=1.0, reason=Reason(code="x")),
                rear=ZoneRequest(demand=1.0, reason=Reason(code="x")),
                top=ZoneRequest(demand=1.0, reason=Reason(code="x")),
            ),
        )
    with pytest.raises(ValidationError, match="MANUAL / CALIBRATION"):
        ModeCommand(mode=OperatingMode.MANUAL)


# -------------------------------------------- 6 / 7. Safety と Guard を迂回できない


@pytest.mark.parametrize("mode", (OperatingMode.MANUAL, OperatingMode.CALIBRATION))
def test_invariant_6_human_modes_cannot_go_below_the_safety_floor(catalog, mode) -> None:
    """`MANUAL` / `CALIBRATION` の 0.0 でも Critical Safety の floor が掛かる。"""
    harness = Harness(catalog)
    harness.settle()
    command = manual(0.0, 0.0, 0.0)
    harness.mode.command = command.model_copy(update={"mode": mode})

    result = harness.tick()

    state = result.tick.state
    assert state.operating_mode is mode
    assert state.active_controller is None
    for zone in Zone:
        demand = result.tick.zones.get(zone).demand
        assert demand.requested == 0.0
        assert demand.effective >= demand.safety_floor


def test_invariant_7_top_keeps_the_cpu_cooling_floor_even_in_calibration(catalog) -> None:
    """Top の floor は CPU 温度・Power の曲線で決まり、測定中でも外れない（0028 §2.4）。"""
    harness = Harness(catalog)
    harness.settle()
    harness.mode.command = manual(0.0, 0.0, 0.0).model_copy(
        update={"mode": OperatingMode.CALIBRATION}
    )

    # ramp down の制限が解けるまで回し、floor だけが残る状態にする。
    result = harness.tick()
    for _ in range(30):
        result = harness.tick()
        if result.tick.zones.top.demand.bound_by is not BoundBy.RAMP_DOWN:
            break

    top = result.tick.zones.top.demand
    assert top.bound_by is BoundBy.SAFETY_FLOOR
    assert top.effective == top.safety_floor > 0.0
    assert result.tick.zones.front.demand.effective < top.effective


def test_invariant_6_a_guard_floor_is_never_bypassed(catalog) -> None:
    """Guard が floor を上げたら、requested が低くても effective は下がらない。"""
    harness = Harness(catalog)
    harness.settle()
    harness.mode.command = manual(0.0, 0.0, 0.0)
    for _ in range(30):
        result = harness.tick()
        if result.tick.zones.front.demand.bound_by is not BoundBy.RAMP_DOWN:
            break

    # GPU hotspot を発火閾値より上げる（activate_above=85.0）。
    harness.telemetry.values["gpu.0.hotspot"] = 95.0
    result = harness.tick()

    guard_floor = harness.config.policy.reactive_guard.floor.value
    for zone in (Zone.FRONT, Zone.REAR):
        demand = result.tick.zones.get(zone).demand
        assert demand.guard_floor == guard_floor
        assert demand.effective >= guard_floor


# ------------------------------------------------------------- 8. MAX はすべてに勝つ


def test_invariant_8_max_mode_forces_every_zone_and_keeps_fallback_active(catalog) -> None:
    """`MAX` は Safety の `forced_max` として掛かり、下で動く制御器は記録に残る。"""
    harness = Harness(catalog)
    harness.settle()
    harness.mode.command = ModeCommand(mode=OperatingMode.MAX)

    result = harness.tick()

    assert result.tick.state.operating_mode is OperatingMode.MAX
    assert result.tick.state.active_controller is ControllerKind.FALLBACK
    for zone in Zone:
        demand = result.tick.zones.get(zone).demand
        assert demand.forced_max is True
        assert demand.effective == 1.0


# ------------------------------------------------- 9. 上げるのは速く、下げるのは遅く


def test_invariant_9_demand_rises_at_once_but_falls_at_the_configured_rate(catalog) -> None:
    """下げる速さだけを制限する（0028 §2.4）。上げる向きに制限を掛けない。"""
    config = control_config(
        safety={"ramp_down_per_s": {"value": 0.1, "status": "provisional"}},
    )
    harness = Harness(catalog, config=config)
    harness.settle()

    harness.mode.command = manual(0.0, 0.0, 0.0)
    first = harness.tick().tick.zones.front.demand
    second = harness.tick().tick.zones.front.demand

    rate = config.safety.ramp_down_per_s.value
    step = rate * config.safety.tick_ms.value / 1_000
    assert first.bound_by is BoundBy.RAMP_DOWN
    assert second.bound_by is BoundBy.RAMP_DOWN
    assert second.effective == pytest.approx(first.effective - step)

    harness.mode.command = manual(1.0, 1.0, 1.0)
    risen = harness.tick().tick.zones.front.demand

    assert risen.effective == pytest.approx(1.0)
    assert risen.bound_by is BoundBy.REQUESTED


# ------------------------------------------------------------- 10 / 11. 例外の扱い


def test_invariant_10_a_guard_exception_becomes_an_emergency_fault(catalog) -> None:
    """Guard の例外を握りつぶさず `guard_exception` にする（0028 §2.7）。"""

    class BrokenGuard(ReactiveGuard):
        def evaluate(self, snapshot):  # type: ignore[no-untyped-def]
            raise RuntimeError("guard が壊れた")

    config = control_config()
    harness = Harness(
        catalog, config=config, guard=BrokenGuard(config.policy.reactive_guard, catalog)
    )
    result = harness.tick()

    codes = {fault.code for fault in result.tick.faults}
    assert FaultCode.GUARD_EXCEPTION in codes
    assert result.tick.state.safety_state is SafetyState.EMERGENCY
    assert all(result.tick.zones.get(zone).demand.effective == 1.0 for zone in Zone)


def test_invariant_10_a_fallback_exception_becomes_an_emergency_fault(catalog) -> None:
    """決定論的な制御器の例外も不具合として `EMERGENCY` にする。"""

    class BrokenFallback(FallbackController):
        def propose(self, snapshot):  # type: ignore[no-untyped-def]
            raise RuntimeError("fallback が壊れた")

        def propose_without_snapshot(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("fallback が壊れた")

    config = control_config()
    harness = Harness(catalog, config=config, fallback=BrokenFallback(config.policy, catalog))
    result = harness.tick()

    codes = {fault.code for fault in result.tick.faults}
    assert FaultCode.FALLBACK_EXCEPTION in codes
    assert result.tick.state.safety_state is SafetyState.EMERGENCY
    assert result.tick.state.active_controller is ControllerKind.FALLBACK


def test_invariant_10_a_gate_exception_keeps_control_and_is_recorded_next_tick(catalog) -> None:
    """Gate の例外でも運転は続き、理由の無い Fallback を trace に残さない。"""

    class BrokenGate(ControllerGate):
        def select(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("gate が壊れた")

    config = control_config()
    harness = Harness(
        catalog,
        config=config,
        gate=BrokenGate(
            config.policy, expected_model_version="thermal-vtest", authority=StaticAuthority()
        ),
    )
    harness.tick()
    second = harness.tick()

    assert second.tick.state.active_controller is ControllerKind.FALLBACK
    codes = {fault.code for fault in second.tick.faults}
    assert FaultCode.FALLBACK_EXCEPTION in codes


def test_invariant_11_a_critical_safety_exception_is_not_caught(catalog) -> None:
    """Safety の例外は**捕まえない**。プロセスを終わらせて引き継ぎで Max にする。"""

    class BrokenSafety:
        def evaluate(self, snapshot, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("safety が壊れた")

    harness = Harness(catalog, safety=BrokenSafety())
    with pytest.raises(RuntimeError, match="safety が壊れた"):
        harness.tick()


def test_invariant_11_a_composition_exception_is_not_caught(catalog) -> None:
    """合成の例外も捕まえない（安全の最終裁定を握りつぶさない）。"""

    class BrokenComposer:
        def compose(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("合成が壊れた")

    harness = Harness(catalog, composer=BrokenComposer())
    with pytest.raises(RuntimeError, match="合成が壊れた"):
        harness.tick()


# ----------------------------------------------------------- 12. 締め切りと縮退


def test_invariant_12_tick_overrun_is_detected_and_recorded(catalog) -> None:
    """締め切り超過を検出し、**同じ行に所要時間と一緒に**残す。"""
    harness = Harness(catalog)
    harness.telemetry.cost_ms = harness.config.safety.tick_deadline_ms.value + 50

    result = harness.tick()

    assert result.deadline_exceeded is True
    runtime = result.tick.runtime
    assert runtime is not None
    assert runtime.deadline_exceeded is True
    assert runtime.duration_ms > runtime.deadline_ms
    assert runtime.deadline_ms == harness.config.safety.tick_deadline_ms.value
    assert runtime.tick_period_ms == harness.config.safety.tick_ms.value
    assert runtime.config.safety_sha256 == harness.config.sources.safety.sha256
    assert runtime.snapshot_schema_version >= 1


def test_invariant_12_a_recorded_overrun_cannot_disagree_with_its_duration() -> None:
    """記録した超過の有無を、同じ行の所要時間と食い違わせられない。"""
    from coldaisle.control.schema import ControlConfigDigest, ControlTickRuntime

    digest = ControlConfigDigest(
        fan_hardware_sha256="0" * 64, safety_sha256="1" * 64, policy_sha256="2" * 64
    )
    with pytest.raises(ValidationError, match="deadline_exceeded"):
        ControlTickRuntime(
            tick_period_ms=1_000,
            deadline_ms=100,
            duration_ms=900,
            deadline_exceeded=False,
            snapshot_schema_version=1,
            config=digest,
        )
    with pytest.raises(ValidationError, match="締め切りを周期より長く"):
        ControlTickRuntime(
            tick_period_ms=100,
            deadline_ms=1_000,
            duration_ms=10,
            deadline_exceeded=False,
            snapshot_schema_version=1,
            config=digest,
        )


def test_invariant_12_consecutive_overruns_degrade_to_emergency_max(catalog) -> None:
    """overrun が続いたら全 zone Max（0028 §2.7）。**縮退を先送りしない。**"""
    harness = Harness(catalog)
    harness.settle()
    harness.telemetry.cost_ms = harness.config.safety.tick_deadline_ms.value + 50

    limit = harness.config.safety.overrun_consecutive_limit.value
    result = None
    for _ in range(limit + 2):
        result = harness.tick()
        if result.tick.state.safety_state is SafetyState.EMERGENCY:
            break

    assert result is not None
    assert result.tick.state.safety_state is SafetyState.EMERGENCY
    assert FaultCode.TICK_OVERRUN in {fault.code for fault in result.tick.faults}
    assert all(result.tick.zones.get(zone).demand.effective == 1.0 for zone in Zone)


# ----------------------------------------------- 13. Telemetry の新しさは単調時計で


def test_invariant_13_an_unchanged_source_timestamp_goes_stale(catalog) -> None:
    """**同じ `ts_ms` を返し続ける供給を fresh と扱わない**（0028 §2.6）。"""
    harness = Harness(catalog)
    harness.settle()
    harness.telemetry.frozen_ts_ms = harness.clock.now_ms()

    stale_after_ms = harness.config.safety.telemetry.cpu_ms.value
    result = None
    for _ in range(stale_after_ms // harness.config.safety.tick_ms.value + 2):
        result = harness.tick()

    assert result is not None
    codes = {fault.code for fault in result.tick.faults}
    assert FaultCode.CPU_TELEMETRY_STALE in codes
    assert result.tick.zones.top.demand.forced_max is True


def test_invariant_13_a_telemetry_failure_does_not_stop_the_loop(catalog) -> None:
    """収集層が落ちても待たず、欠測として安全側の裁定へ渡す。"""
    harness = Harness(catalog)
    harness.telemetry.error = RuntimeError("ストアが読めない")

    result = harness.tick()

    assert result.tick.state.safety_state is SafetyState.STARTUP
    assert all(result.tick.zones.get(zone).demand.effective == 1.0 for zone in Zone)
    harness.telemetry.error = None
    assert harness.tick().tick.tick_id == 1


# ------------------------------------------------ 14 / 15. worker 結果の扱い


def test_invariant_14_a_repeated_worker_result_keeps_its_first_receipt_time(catalog) -> None:
    """**同じ結果を返し続けても新しくならない。** 受信時刻は初めて見た tick に押す。"""
    result = StubWorkerResult()
    harness = Harness(catalog, learned_source=StubWorkerSource(result=result))
    harness.settle()

    first = list(result.seen_received_ms)
    harness.tick()
    harness.tick()

    assert len(result.seen_received_ms) > len(first)
    assert len(set(result.seen_received_ms)) == 1


def test_invariant_14_a_new_worker_result_gets_a_new_receipt_time(catalog) -> None:
    """別の結果は別の受信時刻になる（識別子で見分ける）。"""
    stub = StubWorkerResult()
    source = StubWorkerSource(result=stub)
    harness = Harness(catalog, learned_source=source)
    harness.settle()
    stub.digest = "b" * 64
    harness.tick()

    assert len(set(stub.seen_received_ms)) == 2


def test_invariant_14_an_empty_poll_does_not_refresh_a_cached_result(catalog) -> None:
    """worker が一時的に読めなくなっても、**同じ提案は新しくならない**。

    空の poll で識別子と受信時刻を捨てると、あとで同じ提案が出てきたときに新しい受信時刻を
    押してしまい、止まった worker の古い提案が何度でも有効期限を取り戻す
    （Codex 4057225329）。
    """
    stub = StubWorkerResult()
    source = StubWorkerSource(result=stub)
    harness = Harness(catalog, learned_source=source)
    harness.settle()
    first = stub.seen_received_ms[0]

    source.result = None
    harness.tick()
    harness.tick()
    source.result = stub
    harness.tick()

    assert set(stub.seen_received_ms) == {first}


def test_invariant_14_an_rl_output_from_another_process_is_not_bound(catalog) -> None:
    """RL 出力は **tick 番号だけ**で束縛しない（番号は再起動で 0 に戻る）。

    前の process の出力が新しい process の無関係な snapshot に結び付くと、
    古い戦略が「この tick のもの」として新鮮に見える（Codex 4057225330）。
    """
    harness = Harness(catalog)
    result = harness.tick()
    now_ms = harness.monotonic.monotonic_ms()
    same_tick_id_other_process = _rl_output(tick_id=result.tick.tick_id, ts_ms=1)

    harness.loop._rl_supervisor_source = StubWorkerSource(result=same_tick_id_other_process)
    candidate, reason = harness.loop._rl_candidate(now_ms)

    assert candidate is None
    assert reason is not None and reason.code == "supervisor_source_unknown"

    # 同じ snapshot（tick_id / ts_ms / snapshot schema）を指す出力なら束縛できる。
    matching = _rl_output(tick_id=result.tick.tick_id, ts_ms=result.tick.ts_ms)
    harness.loop._rl_supervisor_source = StubWorkerSource(result=matching)
    candidate, reason = harness.loop._rl_candidate(now_ms)

    assert reason is None
    assert candidate is not None and candidate.output is matching


def _rl_output(*, tick_id: int, ts_ms: int) -> Any:
    from coldaisle.control.schema import (
        SupervisorObjectiveWeights,
        SupervisorOutput,
        SupervisorPolicyKind,
        SupervisorTargetBand,
        TemperatureTarget,
        WorkloadRegime,
    )

    return SupervisorOutput(
        snapshot_schema_version=1,
        tick_id=tick_id,
        ts_ms=ts_ms,
        policy=SupervisorPolicyKind.RL,
        version="rl-vtest",
        regime=WorkloadRegime.UNKNOWN,
        regime_confidence=0.0,
        weights=SupervisorObjectiveWeights(
            gpu_temperature=0.8, cpu_temperature=0.8, balance=0.5, acoustic=0.4, change=0.3
        ),
        strategy="balanced",
        target_band=SupervisorTargetBand(
            cpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=75.0),
            gpu_temperature=TemperatureTarget(lower_c=45.0, upper_c=78.0),
        ),
        computed_at_ms=ts_ms,
    )


def test_invariant_27_a_learned_result_from_another_process_is_not_accepted(catalog) -> None:
    """**再起動をまたいだ worker 結果を新しく見せない**（Codex 4057590589）。

    受信時刻は「この loop が初めて見た時刻」なので、生き残った worker の古い結果を
    そのまま受け取ると、前の process のときに作られた提案へ新しい受信時刻を押す。
    `tick_id` は 0 から振り直されるため、番号だけでは見分けられない。
    """
    source = StubWorkerSource(result=None)
    harness = Harness(catalog, learned_source=source)
    first = harness.tick()

    ours = StubWorkerResult(
        digest="b" * 64,
        proposal=StubProposalOrigin(seq=first.tick.tick_id, computed_at_ms=first.tick.ts_ms),
    )
    source.result = ours
    harness.tick()
    assert ours.seen_received_ms, "この process の snapshot から作られた結果は受け取る"

    stale = StubWorkerResult(
        digest="c" * 64,
        proposal=StubProposalOrigin(seq=first.tick.tick_id, computed_at_ms=1),
    )
    source.result = stale
    harness.tick()

    assert stale.seen_received_ms == [], "別 process の snapshot の結果を Gate へ渡している"


def test_invariant_27_a_worker_failure_still_reaches_the_gate(catalog) -> None:
    """提案の無い結果（worker の失敗）は素通しする。**Fallback へ倒すだけで制御権を与えない。**"""
    stub = StubWorkerResult()
    harness = Harness(catalog, learned_source=StubWorkerSource(result=stub))
    harness.tick()

    assert stub.seen_received_ms


def test_invariant_28_the_authority_store_does_not_wait_forever_for_its_lock(
    tmp_path: Path,
) -> None:
    """降格の書き残しが lock を無期限に待たない（Codex 4057590594）。

    待ち続けると、control tick のあいだに他 process の lock が居座り、2つの heartbeat の
    あいだにその待ちが丸ごと入る。
    """
    import fcntl
    import os

    from coldaisle.control.authority import (
        AuthorityStore,
        AuthorityStoreError,
        AuthorityTrigger,
    )

    root = tmp_path / "authority"
    root.mkdir(parents=True)
    store = AuthorityStore(root, SimulatedClock(TEST_EPOCH_MS), lock_timeout_ms=20)
    held = os.open(root / ".authority.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(AuthorityStoreError, match="以内に取れなかった"):
            store.lower_stage(
                to_stage=StaticAuthority().current_stage(),
                actor="test",
                reason="lock",
                trigger=AuthorityTrigger.AUTOMATIC,
            )
    finally:
        os.close(held)

    with pytest.raises(AuthorityStoreError, match="lock の待ち上限"):
        # 待ち上限の無い store を control runtime に配線させない。
        from coldaisle.control.authority import AuthorityRuntime

        AuthorityRuntime(
            AuthorityStore(root, SimulatedClock(TEST_EPOCH_MS)),
            control_config().policy,
        )


def test_invariant_28_a_demotion_that_cannot_be_persisted_is_visible(catalog, caplog) -> None:
    """書き残せなかった降格を黙って流さない（再起動で戻るので運用者が直す）。"""

    @dataclass
    class FailingAuthority:
        inner: StaticAuthority

        def current_stage(self):  # type: ignore[no-untyped-def]
            return self.inner.current_stage()

        def observe(self, **kwargs: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(
                to_stage=self.inner.current_stage(),
                persisted=False,
                persist_failure=Reason(code="authority_persist_failed", detail="disk full"),
            )

    harness = Harness(catalog, authority=FailingAuthority(StaticAuthority()))
    with caplog.at_level("ERROR"):
        result = harness.tick()

    assert result.hardware is not None, "記録できなくても制御は続ける"
    assert any("書き残せなかった" in record.message for record in caplog.records)


def test_invariant_15_a_late_tick_never_lets_the_learned_proposal_in(catalog) -> None:
    """締め切りを過ぎた tick では ML を通さない（安全側が I/O を待たない）。"""
    stub = StubWorkerResult()
    harness = Harness(catalog, learned_source=StubWorkerSource(result=stub))
    harness.settle()
    assert stub.seen_deadline_exceeded[-1] is False

    harness.telemetry.cost_ms = harness.config.safety.tick_deadline_ms.value + 50
    harness.tick()

    assert stub.seen_deadline_exceeded[-1] is True


def test_invariant_15_a_failing_worker_poll_does_not_stop_the_loop(catalog) -> None:
    """worker の入口が壊れても Baseline で続ける。"""
    harness = Harness(catalog, learned_source=StubWorkerSource(error=RuntimeError("worker 断")))
    result = harness.settle()

    assert result.tick.state.active_controller is ControllerKind.FALLBACK


# ---------------------------------------------- 16. ML / Supervisor が止まっても続く


def test_invariant_16_control_continues_without_any_learned_or_supervisor_layer(catalog) -> None:
    """Supervisor も worker も無い構成で、Baseline が3 zone を動かし続ける。"""
    harness = Harness(catalog, with_supervisor=False)
    harness.settle()
    harness.run(3)

    last = harness.loop._previous_snapshot
    assert last is not None
    recorded = [ControlTick.model_validate_json(row) for row in harness.trace.rows]
    assert len(recorded) >= 4
    assert all(tick.state.active_controller is ControllerKind.FALLBACK for tick in recorded)
    assert all(tick.supervisor is None for tick in recorded)


def test_invariant_16_a_broken_supervisor_falls_back_without_stopping_control(catalog) -> None:
    """Supervisor の失敗は運転戦略の欠落であって、制御の停止ではない。"""

    class BrokenSupervisor(SupervisorCoordinator):
        def evaluate(self, policy_input, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("supervisor が壊れた")

    config = control_config()
    harness = Harness(catalog, config=config)
    harness.loop._supervisor = BrokenSupervisor(config.policy.supervisor, harness.clock)
    result = harness.settle()

    assert result.tick.supervisor is None
    assert result.tick.state.supervisor_policy is None
    assert result.tick.state.active_controller is ControllerKind.FALLBACK


# ---------------------------------------------------------- 17. 3系統は独立に動く


def test_invariant_17_the_three_zones_take_independent_demands(catalog) -> None:
    """Front / Rear / Top を別々の値で動かせる（同じ値へ丸めない）。"""
    config = control_config(
        safety={"ramp_down_per_s": {"value": 1.0, "status": "provisional"}},
    )
    harness = Harness(catalog, config=config)
    harness.settle()
    harness.mode.command = manual(0.45, 0.6, 0.85)

    result = harness.tick()

    effective = {zone: result.tick.zones.get(zone).demand.effective for zone in Zone}
    assert effective[Zone.FRONT] == pytest.approx(0.45)
    assert effective[Zone.REAR] == pytest.approx(0.6)
    assert effective[Zone.TOP] == pytest.approx(0.85)
    assert result.hardware is not None
    pwms = {zone: result.hardware.get(zone).readback.pwm_raw for zone in Zone}
    assert len(set(pwms.values())) == 3


def test_invariant_17_a_zone_fault_only_forces_that_zone_to_max(catalog) -> None:
    """1つの zone の故障でその zone を Max にし、ほかの zone は運転を続ける（0028 §2.7）。"""
    harness = Harness(catalog)
    harness.settle()
    # 運転に入ってから front の tach だけを止める。起動前に止めると STARTUP のまま
    # 全 zone Max になり、「zone 単位の縮退」を確かめられない。
    harness.backend.fault_plan = SimulatedFaultPlan(tach_stall=frozenset({Zone.FRONT}))

    result = None
    for _ in range(8):
        result = harness.tick()
        if FaultCode.TACH_STALL in {fault.code for fault in result.tick.faults}:
            break

    assert result is not None
    stall = [fault for fault in result.tick.faults if fault.code is FaultCode.TACH_STALL]
    assert stall and stall[0].zone is Zone.FRONT
    assert result.tick.state.safety_state is SafetyState.DEGRADED
    assert result.tick.zones.front.demand.forced_max is True
    for zone in (Zone.REAR, Zone.TOP):
        demand = result.tick.zones.get(zone).demand
        assert demand.forced_max is False, f"{zone.value} まで Max にしている"
        assert demand.effective >= demand.safety_floor


# ----------------------------------------------------------------- 18. 再現できる


def test_invariant_18_the_same_inputs_produce_the_same_decision_trace(catalog) -> None:
    """同じ Replay・同じ時計・同じ設定なら tick の順序と中身が一致する。"""

    def run() -> list[dict[str, Any]]:
        harness = Harness(catalog)
        harness.settle()
        harness.run(3)
        return [json.loads(row) for row in harness.trace.rows]

    assert run() == run()


def test_invariant_18_each_tick_is_recorded_once_with_an_increasing_id(catalog) -> None:
    """tick ごとに1行。**保存に失敗しても制御は止めない。**"""
    harness = Harness(catalog)
    harness.settle()
    ids = [ControlTick.model_validate_json(row).tick_id for row in harness.trace.rows]
    assert ids == list(range(len(ids)))

    harness.trace.error = RuntimeError("保存先が落ちた")
    result = harness.tick()

    assert result.recorded is False
    assert result.hardware is not None


# -------------------------------------------- 19. 検証済み設定だけが active になる


def test_invariant_19_a_deadline_longer_than_the_period_is_rejected() -> None:
    """締め切りが周期より長い設定を採用しない（超過を検出する前に次が始まる）。"""
    documents = valid_documents()
    documents["safety.yaml"]["tick_deadline_ms"] = {"value": 2_000, "status": "provisional"}
    with pytest.raises(ValidationError, match="tick_deadline_ms"):
        SafetyConfig.model_validate(documents["safety.yaml"])


def test_invariant_19_a_watchdog_shorter_than_the_period_is_rejected() -> None:
    """1 周期ぶんの遅れで deadman が落ちる設定を採用しない。"""
    documents = valid_documents()
    documents["safety.yaml"]["watchdog_timeout_ms"] = {"value": 1_000, "status": "provisional"}
    with pytest.raises(ValidationError, match="watchdog_timeout_ms"):
        SafetyConfig.model_validate(documents["safety.yaml"])


def test_invariant_19_unapproved_hardware_mapping_never_takes_control(tmp_path: Path) -> None:
    """`provisional` の hardware mapping では制御を取らない（0028 §2.9 の承認点 3）。"""
    write_documents(tmp_path, valid_documents())
    config = Config(config_dir=tmp_path, db=tmp_path / "x.db", metrics=METRICS_PATH)

    with pytest.raises(ActuationNotApprovedError):
        build(config)
    assert main(_argv(tmp_path)) == EXIT_ACTUATION_NOT_APPROVED


def test_invariant_19_a_broken_hardware_mapping_leaves_the_bios_in_control(
    tmp_path: Path,
) -> None:
    """header を特定できない設定では**何も書かずに**終了する（0028 §2.7）。"""
    documents = valid_documents()
    write_documents(tmp_path, documents)
    (tmp_path / "fan-hardware.yaml").write_text("zones: {}\n", encoding="utf-8")

    assert main(_argv(tmp_path)) == EXIT_HARDWARE_CONFIG_INVALID


def test_invariant_19_invalid_safety_config_writes_max_only(tmp_path: Path) -> None:
    """hardware は正しく safety / policy が壊れているときは、全 zone Max だけを書く。"""
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(tmp_path, documents)
    applied: list[Any] = []

    class Recorder:
        def __init__(self, config, binding) -> None:  # type: ignore[no-untyped-def]
            self._inner = SimulatedFanBackend(config=config, runtime_binding=binding)

        def apply(self, demands):  # type: ignore[no-untyped-def]
            applied.append(tuple(demands.get(zone).effective for zone in Zone))
            return self._inner.apply(demands)

    stats = run_config_invalid_max(
        Config(config_dir=tmp_path, metrics=METRICS_PATH),
        monotonic=ManualMonotonicClock(0),
        backend_factory=Recorder,
    )

    assert stats.emergency_max is True
    assert applied == [(1.0, 1.0, 1.0)]


def _argv(config_dir: Path) -> list[str]:
    return [
        "--config-dir",
        str(config_dir),
        "--db",
        str(config_dir / "control.db"),
        "--metrics",
        str(METRICS_PATH),
        "--max-ticks",
        "1",
    ]


# ------------------------------------------- 20. 遅れた tick を取り戻して連続実行しない


@dataclass
class FakeLoop:
    """scheduler だけを見るための最小の loop。制御の中身は持たない。"""

    tick_period_ms: int
    monotonic: ManualMonotonicClock
    cost_ms: int = 0
    calls: list[int] = field(default_factory=list)
    on_tick: Any = None

    def tick(self) -> Any:
        self.calls.append(self.monotonic.monotonic_ms())
        if self.cost_ms:
            self.monotonic.advance_ms(self.cost_ms)
        if self.on_tick is not None:
            self.on_tick()
        return _fake_result()


def _fake_result() -> Any:
    """`ControlDaemon` が読むぶんだけを持つ結果。"""
    from types import SimpleNamespace

    from coldaisle.control.loop import ControlTickResult

    zones = SimpleNamespace(get=lambda zone: SimpleNamespace(demand=SimpleNamespace(effective=1.0)))
    state = SimpleNamespace(
        operating_mode=OperatingMode.AUTO,
        safety_state=SafetyState.STARTUP,
        authority_stage=StaticAuthority().current_stage(),
        active_controller=None,
    )
    tick = SimpleNamespace(tick_id=0, ts_ms=0, state=state, faults=(), zones=zones)
    return ControlTickResult(
        tick=tick,  # type: ignore[arg-type]
        duration_ms=0,
        deadline_exceeded=False,
        recorded=False,
        trace_failed=False,
        hardware=None,
    )


def test_invariant_20_the_scheduler_skips_missed_slots_instead_of_catching_up() -> None:
    """周期を跨いだ tick のぶんを連続実行しない（0028 §2.6）。"""
    monotonic = ManualMonotonicClock(0)
    loop = FakeLoop(tick_period_ms=1_000, monotonic=monotonic, cost_ms=2_500)
    daemon = ControlDaemon(
        loop=loop,  # type: ignore[arg-type]
        monotonic=monotonic,
        sleep=lambda seconds: monotonic.advance_ms(round(seconds * 1_000)),
        stats=ControlStats(),
    )

    daemon.run(max_ticks=3)

    assert len(loop.calls) == 3
    # 1 tick が 2.5 周期かかるので、毎回 2 枠が飛ぶ。追いつくための連続実行はしない。
    assert daemon.stats.skipped_slots == 6
    gaps = [call - previous for previous, call in zip(loop.calls, loop.calls[1:], strict=False)]
    assert gaps == [3_000, 3_000]


def test_the_scheduler_stops_between_ticks_when_asked() -> None:
    """停止は tick の境目で効く。**tick の途中で止めない。**"""
    monotonic = ManualMonotonicClock(0)
    loop = FakeLoop(tick_period_ms=1_000, monotonic=monotonic)
    daemon = ControlDaemon(
        loop=loop,  # type: ignore[arg-type]
        monotonic=monotonic,
        sleep=lambda seconds: monotonic.advance_ms(1_000),
        stats=ControlStats(),
    )
    loop.on_tick = daemon.request_stop

    daemon.run()

    assert len(loop.calls) == 1
    assert daemon.stats.ticks == 1


# ------------------------------------------------- 21〜23. deadman（watchdog）


def test_invariant_21_the_heartbeat_is_sent_before_any_persistence(catalog) -> None:
    """**書き込みと検証の直後に heartbeat を出す。**

    降格の永続化（#92）と decision trace の保存（#82）はどちらも I/O を伴う。あとに置くと、
    保存先のロックで待たされているあいだ heartbeat が出ず、制御は終わっているのに
    deadman に殺される（Codex 4057225328）。
    """
    harness = Harness(catalog)
    harness.tick()

    assert harness.order == ["watchdog", "authority", "trace"]
    assert harness.watchdog.beats == 1


def test_invariant_22_a_tick_that_never_completes_sends_no_heartbeat(catalog) -> None:
    """完了しなかった tick では heartbeat を出さない（deadman が効く）。"""

    class BrokenSafety:
        def evaluate(self, snapshot, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("safety が壊れた")

    harness = Harness(catalog, safety=BrokenSafety())
    with pytest.raises(RuntimeError):
        harness.tick()

    assert harness.watchdog.beats == 0


def test_invariant_22_a_failing_watchdog_does_not_stop_cooling(catalog) -> None:
    """heartbeat を送れなくても制御は続ける（本当に途切れていれば外の deadman が終わらせる）。"""
    harness = Harness(catalog)
    harness.watchdog.error = OSError("notify socket が詰まった")

    result = harness.tick()

    assert result.hardware is not None
    assert all(result.tick.zones.get(zone).demand.effective == 1.0 for zone in Zone)


def test_invariant_23_a_missing_notify_socket_is_not_a_silent_no_op(catalog, caplog) -> None:
    """deadman が無い環境を黙って no-op にしない（`--require-watchdog` なら起動しない）。"""
    monotonic = ManualMonotonicClock(0)
    with caplog.at_level("ERROR"):
        watchdog = create_watchdog(
            interval_ms=1_100, timeout_ms=5_000, monotonic=monotonic, environ={}
        )

    assert isinstance(watchdog, UnsupervisedWatchdog)
    assert any("deadman" in record.message for record in caplog.records)

    with pytest.raises(WatchdogUnavailableError):
        create_watchdog(
            interval_ms=1_100,
            timeout_ms=5_000,
            monotonic=monotonic,
            require=True,
            environ={},
        )


def test_invariant_23_the_unsupervised_watchdog_reports_a_late_heartbeat(caplog) -> None:
    """外の deadman が無くても、heartbeat の遅れを捨てない。"""
    monotonic = ManualMonotonicClock(0)
    watchdog = create_watchdog(interval_ms=400, timeout_ms=1_000, monotonic=monotonic, environ={})
    watchdog.notify()
    monotonic.advance_ms(500)
    with caplog.at_level("ERROR"):
        caplog.clear()
        watchdog.notify()
    assert caplog.records == []

    monotonic.advance_ms(5_000)
    with caplog.at_level("ERROR"):
        caplog.clear()
        watchdog.notify()

    assert any("heartbeat" in record.message for record in caplog.records)


def test_invariant_23_the_systemd_watchdog_sends_the_abi_datagram(tmp_path: Path) -> None:
    """`NOTIFY_SOCKET` へ送るのは固定の `WATCHDOG=1` だけ（値を組み立てない）。"""
    import socket

    address = str(tmp_path / "notify.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(address)
    try:
        watchdog = create_watchdog(
            interval_ms=1_100,
            timeout_ms=5_000,
            monotonic=ManualMonotonicClock(0),
            environ=_watchdog_env(address, 5_000),
        )
        assert isinstance(watchdog, SystemdWatchdog)
        assert listener.recv(64) == b"READY=1"
        watchdog.notify()
        assert listener.recv(64) == WATCHDOG_DATAGRAM
        watchdog.close()
    finally:
        listener.close()


def test_invariant_23_the_daemon_composition_always_wires_a_deadman(tmp_path: Path) -> None:
    """`build()` が deadman を配線する（`NullWatchdog` のまま本番に出ない）。"""
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(tmp_path, documents)
    config = Config(
        config_dir=tmp_path,
        db=tmp_path / "control.db",
        metrics=METRICS_PATH,
        quality_rules=CONFIG_DIR / "quality.yaml",
    )

    daemon = build(config)
    try:
        assert isinstance(daemon.loop._watchdog, UnsupervisedWatchdog)
    finally:
        if daemon.store is not None:
            daemon.store.close()

    assert main([*_argv(tmp_path), "--require-watchdog"]) == EXIT_WATCHDOG_UNAVAILABLE


def _watchdog_env(address: str, usec_ms: int, *, pid: int | None = None) -> dict[str, str]:
    """`WatchdogSec` が有効なときに systemd が渡す環境。"""
    env = {NOTIFY_SOCKET_ENV: address, WATCHDOG_USEC_ENV: str(usec_ms * 1_000)}
    if pid is not None:
        env[WATCHDOG_PID_ENV] = str(pid)
    return env


def test_invariant_24_a_notify_socket_alone_is_not_proof_of_a_deadman(tmp_path: Path) -> None:
    """**通知先があることを deadman の証拠にしない**（Codex 4057548959）。

    `Type=notify` なら `WatchdogSec` が無くても `NOTIFY_SOCKET` は渡る。それだけで
    `--require-watchdog` を通すと、`WATCHDOG=1` は誰にも見られないまま「deadman あり」
    として運転してしまう。
    """
    import socket

    address = str(tmp_path / "notify.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(address)
    try:
        with pytest.raises(WatchdogUnavailableError, match=WATCHDOG_USEC_ENV):
            create_watchdog(
                interval_ms=1_100,
                timeout_ms=5_000,
                monotonic=ManualMonotonicClock(0),
                require=True,
                environ={NOTIFY_SOCKET_ENV: address},
            )
    finally:
        listener.close()


def test_invariant_24_a_watchdog_meant_for_another_process_is_not_ours(tmp_path: Path) -> None:
    """`WATCHDOG_PID` が別 process を指していれば、その deadman は自分を見ていない。"""
    import os
    import socket

    address = str(tmp_path / "notify.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(address)
    try:
        with pytest.raises(WatchdogUnavailableError):
            create_watchdog(
                interval_ms=1_100,
                timeout_ms=5_000,
                monotonic=ManualMonotonicClock(0),
                require=True,
                environ=_watchdog_env(address, 5_000, pid=os.getpid() + 1),
            )
        watchdog = create_watchdog(
            interval_ms=1_100,
            timeout_ms=5_000,
            monotonic=ManualMonotonicClock(0),
            environ=_watchdog_env(address, 5_000, pid=os.getpid()),
        )
        assert isinstance(watchdog, SystemdWatchdog)
        watchdog.close()
    finally:
        listener.close()


def test_invariant_24_a_watchdog_shorter_than_the_heartbeat_never_starts(tmp_path: Path) -> None:
    """時間切れが heartbeat の間隔に足りない unit では**制御を取らない。**

    起動できてしまうと、健全な運転のまま殺されて再起動を繰り返す。
    """
    import socket

    address = str(tmp_path / "notify.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(address)
    try:
        with pytest.raises(WatchdogUnavailableError, match="短すぎる"):
            create_watchdog(
                interval_ms=1_100,
                timeout_ms=5_000,
                monotonic=ManualMonotonicClock(0),
                environ=_watchdog_env(address, 2_000),
            )
    finally:
        listener.close()


def test_invariant_26_a_dead_notify_socket_is_reported_as_a_watchdog_failure(
    tmp_path: Path,
) -> None:
    """通知先が消えている失敗を、素の `OSError` のまま外へ出さない（Codex 4057548964）。

    素通しすると、起動時の分類が「設定が不正」へ落ちて全 zone Max を書いてしまう。
    """
    stale = str(tmp_path / "gone.sock")

    with pytest.raises(WatchdogUnavailableError, match=NOTIFY_SOCKET_ENV):
        create_watchdog(
            interval_ms=1_100,
            timeout_ms=5_000,
            monotonic=ManualMonotonicClock(0),
            environ=_watchdog_env(stale, 5_000),
        )


def test_invariant_25_the_trace_store_cannot_wait_longer_than_a_tick_deadline(
    tmp_path: Path,
) -> None:
    """decision trace の保存の待ちに上限を掛ける（Codex 4057548962）。

    既定の 5 秒待つと、保存が終わるまで次の tick が始まらず、heartbeat の間隔が
    deadman の時間切れを超える。**制御を止めずに、落ちた記録は見えるようにする。**
    """
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(tmp_path, documents)
    config = Config(
        config_dir=tmp_path,
        db=tmp_path / "control.db",
        metrics=METRICS_PATH,
        quality_rules=CONFIG_DIR / "quality.yaml",
    )

    daemon = build(config)
    try:
        assert daemon.store is not None
        busy_timeout_ms = daemon.store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        deadline_ms = daemon.loop.tick_deadline_ms
        assert busy_timeout_ms <= deadline_ms
        # 最悪でも「保存の待ち + 次の tick」が deadman の時間切れに収まる。
        assert (
            busy_timeout_ms + heartbeat_interval_ms(daemon.loop._config.safety)
            < daemon.loop._config.safety.watchdog_timeout_ms.value
        )
    finally:
        if daemon.store is not None:
            daemon.store.close()


def test_invariant_25_a_dropped_trace_is_counted_and_control_continues(catalog) -> None:
    """保存に失敗した記録を黙って捨てない。**制御は止めない。**"""
    harness = Harness(catalog)
    harness.trace.error = RuntimeError("保存先が落ちた")

    result = harness.tick()

    assert result.trace_failed is True
    assert result.recorded is False
    assert result.hardware is not None

    daemon = ControlDaemon(loop=harness.loop, monotonic=harness.monotonic)
    daemon._record(result)
    assert daemon.stats.trace_dropped == 1


def test_invariant_22_a_hung_tick_leaves_a_gap_longer_than_the_deadman(catalog) -> None:
    """**tick が固まれば heartbeat が途切れる。** 実運用の時間切れで確かめる。"""
    harness = Harness(catalog)
    harness.tick()
    watchdog_ms = harness.config.safety.watchdog_timeout_ms.value

    # 収集層が固まった tick を1つ作る（書き込みと検証が終わるまで heartbeat は出ない）。
    harness.telemetry.cost_ms = watchdog_ms + 1_000
    harness.tick()

    gaps = [
        later - earlier
        for earlier, later in zip(
            harness.watchdog.beats_at, harness.watchdog.beats_at[1:], strict=False
        )
    ]
    assert gaps and max(gaps) > watchdog_ms


def test_invariant_26_startup_failures_are_reported_by_their_own_kind(tmp_path: Path) -> None:
    """起動時の失敗を**種類ごとに**返す（設定不正と環境の失敗を混ぜない）。

    ここを1つの `Exception` にまとめると、Metric Catalog が読めないだけで
    0028 §2.7 の「設定不正なら全 zone Max」が走る。
    """
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(tmp_path, documents)
    base = Config(
        config_dir=tmp_path,
        db=tmp_path / "control.db",
        metrics=METRICS_PATH,
        quality_rules=CONFIG_DIR / "quality.yaml",
    )

    # Metric Catalog が無い → 制御を取らない（BIOS の制御のまま）。
    with pytest.raises(StartupEnvironmentError):
        build(base_with(base, metrics=tmp_path / "missing-metrics.yaml"))
    assert (
        main(
            [
                *_argv(tmp_path),
                "--quality-rules",
                str(CONFIG_DIR / "quality.yaml"),
                "--metrics",
                str(tmp_path / "missing-metrics.yaml"),
            ]
        )
        == EXIT_STARTUP_ENVIRONMENT
    )

    # safety.yaml が不正 → 0028 §2.7 の「全 zone Max」。
    broken = valid_documents()
    broken["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    broken["safety.yaml"]["tick_deadline_ms"] = {"value": 999_999, "status": "provisional"}
    write_documents(tmp_path, broken)
    with pytest.raises(ControlConfigInvalidError):
        build(base)
    assert main([*_argv(tmp_path), "--quality-rules", str(CONFIG_DIR / "quality.yaml")]) == 1


def base_with(config: Config, **overrides: Any) -> Config:
    from dataclasses import replace

    return replace(config, **overrides)


def test_invariant_19_a_watchdog_that_only_covers_one_period_is_rejected() -> None:
    """heartbeat は tick ごとにしか出ないので、時間切れは2周期ぶん以上にする。"""
    documents = valid_documents()
    documents["safety.yaml"]["watchdog_timeout_ms"] = {"value": 1_500, "status": "provisional"}
    with pytest.raises(ValidationError, match="watchdog_timeout_ms"):
        SafetyConfig.model_validate(documents["safety.yaml"])


# ------------------------------------------------------------- 入力契約の組み立て


def test_the_input_contract_is_derived_from_the_validated_config(catalog) -> None:
    """契約を手書きの表にせず、設定が指している metric から数え直す。"""
    config = control_config()
    contract = build_input_contract(config, catalog)

    metrics = {spec.metric: spec for spec in contract.signals}
    assert "gpu.0.hotspot" in metrics, "Guard の trigger が契約から漏れている"
    assert "air.room" in metrics, "派生 trigger の材料が契約から漏れている"
    assert metrics["cpu.package"].stale_after_ms == config.safety.telemetry.cpu_ms.value
    assert metrics["power.gpu.0"].stale_after_ms == config.safety.telemetry.gpu_ms.value
    assert contract.critical_groups[0].code == "air_telemetry"


def test_an_input_without_a_known_delay_is_rejected_before_startup(catalog) -> None:
    """許容遅延を決められない metric を、既定値で黙って通さない。"""
    documents = valid_documents()
    documents["fan-policy.yaml"]["workload_regime"]["cpu_power"]["metric"] = "power.cpu.package"
    config = control_config()
    policy = config.policy.model_copy(
        update={
            "fallback_temperature_inputs": config.policy.fallback_temperature_inputs.model_copy(
                update={
                    "top": config.policy.fallback_temperature_inputs.top.model_copy(
                        update={"metrics": ("board.chipset",)}
                    )
                }
            )
        }
    )
    broken = config.model_copy(update={"policy": policy})

    with pytest.raises(ValueError, match="許容遅延を決められない"):
        build_input_contract(broken, catalog)


def test_the_store_source_only_returns_metrics_in_the_contract(catalog, tmp_path: Path) -> None:
    """契約に無い metric を制御へ渡さない（State Estimator が拒む前に絞る）。"""

    class FakeStore:
        def latest(self):  # type: ignore[no-untyped-def]
            from coldaisle.store.models import LatestReading

            return {
                "cpu.package": LatestReading(
                    metric="cpu.package", ts_ms=1, value=50.0, quality=Quality.OK, age_ms=0
                ),
                "humidity.room": LatestReading(
                    metric="humidity.room", ts_ms=1, value=40.0, quality=Quality.OK, age_ms=0
                ),
            }

    source = StoreTelemetrySource(FakeStore(), frozenset({"cpu.package"}))  # type: ignore[arg-type]

    assert [sample.metric for sample in source.read()] == ["cpu.package"]
