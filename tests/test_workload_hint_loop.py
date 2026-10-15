"""Workload Hint B0 を control loop に通す（#319 / 決定記録 0133 §2.6 / §2.7 / §2.9）。

実機なし（simulated backend・注入した時計・偽の受け渡し口）。

守りたいこと:

1. B0 は「効かせていたら選択が変わったか」（``would_override``）を v16 の trace に残すだけで、
   Supervisor の出力も requested / effective も変えない
2. ヒントの有無・内容・読み手の生死を変えても、同じ入力から Guard / Safety / Fallback と
   Fan の Demand が1ビットも変わらない（0133 §2.7）
3. 設定がヒントを読むと言うのに読み手を配線していない loop は作らない
4. Safety / Guard / Fallback / hardware はヒントの型を import しない（0133 §2.7。import の走査）
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import pytest

from coldaisle.clock import ManualMonotonicClock, SimulatedClock
from coldaisle.control.schema import (
    ControlTick,
    WorkloadHintInfluence,
    WorkloadHintKind,
    WorkloadHintRecord,
    WorkloadHintStage,
    WorkloadHintStatus,
    WorkloadRegime,
)
from coldaisle.control.supervisor.workload_hint import (
    HintFeed,
    HintFeedState,
    HintObservation,
    WorkloadHintTracker,
)
from coldaisle.metrics import MetricCatalog
from test_control_config import valid_documents
from test_control_loop import METRICS_PATH, Harness, control_config
from test_workload_hint_config import lowered_band_contexts, observe_block
from test_workload_hint_tracker import FakeMailbox


@pytest.fixture(scope="session")
def catalog() -> MetricCatalog:
    """本番と同じ Metric Catalog。"""
    return MetricCatalog.from_yaml(METRICS_PATH)


KEYED_REGIMES = ("transient_cpu_gpu", "sustained_cpu_gpu", "transient_gpu", "transient_cpu")


def hint_policy() -> dict[str, Any]:
    """band を下げた context を、いくつかの Regime × training の上書き先にした fan-policy.yaml。"""
    policy = copy.deepcopy(valid_documents()["fan-policy.yaml"])
    supervisor: dict[str, Any] = policy["supervisor"]  # type: ignore[assignment]
    contexts = lowered_band_contexts()
    supervisor["rule_policy"]["contexts"].update(contexts)
    bounds = supervisor["output_bounds"]
    bounds["target_bands"].append(contexts["sustained_gpu"]["target_band"])
    overrides = [
        {"regime": regime, "workload": "training", "context": "sustained_gpu"}
        for regime in KEYED_REGIMES
    ]
    supervisor["workload_hint"] = observe_block(context_overrides=overrides)
    return policy


class FeedingMailbox(FakeMailbox):
    """毎 tick、読み手が読み直したかのように新しい時刻で同じヒントを置く偽の受け渡し口。"""

    def __init__(self, monotonic: ManualMonotonicClock, observation: HintObservation | None):
        super().__init__()
        self.monotonic = monotonic
        self.observation = observation

    def latest(self) -> HintFeed | None:
        return HintFeed(
            state=HintFeedState.OK,
            read_monotonic_ms=self.monotonic.monotonic_ms(),
            current=self.observation,
        )


def training_hint(clock: SimulatedClock) -> HintObservation:
    return HintObservation(
        event_id=42,
        status=WorkloadHintStatus.ACTIVE,
        workload=WorkloadHintKind.TRAINING,
        source_label="job-launcher",
        declared_ms=3_600_000,
        ts_ms=clock.now_ms(),
        boot_id=clock.boot_id(),
        boottime_ns=clock.boottime_ns(),
    )


def harness(catalog, *, with_hint: bool, alive: bool = True) -> tuple[Harness, list[FakeMailbox]]:
    config = control_config(policy=hint_policy())
    mailboxes: list[FakeMailbox] = []

    def factory(clock: SimulatedClock, monotonic: ManualMonotonicClock) -> WorkloadHintTracker:
        mailbox = FeedingMailbox(monotonic, training_hint(clock) if with_hint else None)
        mailbox.alive = alive
        mailboxes.append(mailbox)
        return WorkloadHintTracker(
            config.policy.supervisor.workload_hint,
            mailbox=mailbox,
            boot_clock=clock,
            monotonic=monotonic,
        )

    return Harness(catalog, config=config, workload_hint_factory=factory), mailboxes


def test_b0_records_would_override_without_changing_the_supervisor_output(catalog):
    h, _ = harness(catalog, with_hint=True)
    ticks: list[ControlTick] = [result.tick for result in h.run(12)]
    keyed = [
        tick
        for tick in ticks
        if tick.state.workload_regime is not None
        and tick.state.workload_regime.value in KEYED_REGIMES
    ]
    assert keyed, "上書きの鍵の Regime になる tick が無い（試験の前提）"
    lowered = lowered_band_contexts()["sustained_gpu"]["target_band"]
    for tick in keyed:
        record = tick.workload_hint
        assert record is not None
        assert record.stage is WorkloadHintStage.OBSERVE
        assert record.status is WorkloadHintStatus.ACTIVE
        assert record.event_id == 42
        assert record.influence is WorkloadHintInfluence.WOULD_OVERRIDE
        assert tick.supervisor is not None
        selected = tick.supervisor.selected_output
        assert selected is not None
        # 出力は基準の context のまま（B0 は選ばない）
        assert selected.target_band.model_dump() != lowered
    for tick in ticks:
        if tick.state.workload_regime is WorkloadRegime.UNKNOWN:
            assert tick.workload_hint is not None
            assert tick.workload_hint.influence is WorkloadHintInfluence.NONE


def _without_hint(tick: ControlTick) -> dict[str, Any]:
    dumped = tick.model_dump(mode="json")
    dumped.pop("workload_hint")
    return dumped


@pytest.mark.parametrize("variant", ["no_hint", "dead_reader"])
def test_hint_does_not_change_one_bit_of_the_control_path(catalog, variant):
    """同じ入力から、ヒントの有無・読み手の生死で trace が塊の外で1ビットも変わらない。"""
    with_hint, _ = harness(catalog, with_hint=True)
    if variant == "no_hint":
        other, _ = harness(catalog, with_hint=False)
    else:
        other, _ = harness(catalog, with_hint=True, alive=False)
    a = [r.tick for r in with_hint.run(12)]
    b = [r.tick for r in other.run(12)]
    assert [_without_hint(t) for t in a] == [_without_hint(t) for t in b]
    assert any(
        t.workload_hint is not None
        and t.workload_hint.influence is WorkloadHintInfluence.WOULD_OVERRIDE
        for t in a
    )
    if variant == "dead_reader":
        assert all(t.workload_hint is not None and t.workload_hint.event_id is None for t in b)


def test_default_config_records_stage_off_every_tick(catalog):
    h = Harness(catalog)
    for result in h.run(3):
        assert result.tick.schema_version == 16
        assert result.tick.workload_hint == WorkloadHintRecord.off()


def test_loop_refuses_observe_without_a_reader(catalog):
    with pytest.raises(ValueError, match="読み手を配線"):
        Harness(catalog, config=control_config(policy=hint_policy()))


# ---------------------------------------------------------------- import の走査（0133 §2.7）

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
HINT_MODULES = {
    "coldaisle.control.supervisor.workload_hint",
    "coldaisle.control.supervisor.hint_clock",
    "coldaisle.workload_hint_reader",
    "coldaisle.store.hint_state",
}
HINT_NAMES = {
    "WorkloadHint",
    "WorkloadHintTracker",
    "HintFeed",
    "HintObservation",
    "HintTransition",
    "WorkloadHintMailbox",
    "WorkloadHintRecord",
    "WorkloadHintKind",
    "WorkloadHintStatus",
    "WorkloadHintStage",
    "WorkloadHintInfluence",
    "HintPriorSkipped",
    "WorkloadHintConfig",
    "WorkloadHintContextOverride",
    "SupervisorInput",
}
INDEPENDENT_PACKAGES = ("safety", "reactive", "fallback", "hardware")


def _imports(path: Path) -> list[tuple[str, set[str]]]:
    found: list[tuple[str, set[str]]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(alias.name, set()) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.append((node.module, {alias.name for alias in node.names}))
    return found


@pytest.mark.parametrize("package", INDEPENDENT_PACKAGES)
def test_safety_guard_fallback_and_hardware_do_not_import_the_hint_types(package):
    files = sorted((SRC / "control" / package).rglob("*.py"))
    assert files
    offenders: list[str] = []
    for path in files:
        for module, names in _imports(path):
            if any(module == m or module.startswith(m + ".") for m in HINT_MODULES):
                offenders.append(f"{path.name}: {module}")
            if module.startswith("coldaisle.control.supervisor"):
                offenders.append(f"{path.name}: {module}")
            offenders += [f"{path.name}: {module}.{name}" for name in names & HINT_NAMES]
    assert offenders == []


def test_control_does_not_import_the_reader_or_the_entry():
    """ヒントの読み手（DB）と書き込み入口は合成の起点。control は Protocol だけを知る。"""
    offenders = [
        f"{path.relative_to(SRC)}: {module}"
        for path in sorted((SRC / "control").rglob("*.py"))
        for module, _ in _imports(path)
        if module.startswith(("coldaisle.workload_hint_reader", "coldaisle.event_entry"))
        or module == "coldaisle.store.db"
    ]
    assert offenders == []


@pytest.mark.parametrize("package", ["ai", "api"])
def test_llm_and_api_layers_do_not_import_the_reader(package):
    offenders = [
        f"{path.relative_to(SRC)}: {module}"
        for path in sorted((SRC / package).rglob("*.py"))
        for module, _ in _imports(path)
        if module.startswith("coldaisle.workload_hint_reader")
    ]
    assert offenders == []


# ---------------------------------------------------------------- fand の組み立て


def test_fand_wires_the_reader_and_ends_the_run_cleanly(tmp_path):
    """`stage: observe` の fand は読み手を起こし、毎 tick 塊を載せ、停止で run を clean で終える。

    run の記録の run_id は trace の run_id と同じ。
    """
    import json
    import sqlite3

    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import Config, build
    from test_control_config import write_documents
    from test_control_loop import METRICS_PATH
    from test_simulated_fan_backend import hardware_config

    config_dir = tmp_path / "control"
    config_dir.mkdir()
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    documents["fan-policy.yaml"] = hint_policy()
    write_documents(config_dir, documents)
    db = tmp_path / "control.db"
    daemon = build(
        Config(
            config_dir=config_dir,
            db=db,
            metrics=METRICS_PATH,
            admin_config=tmp_path / "missing.yaml",
        ),
        watchdog=NullWatchdog(),
    )
    try:
        assert daemon.workload_hint_reader is not None
        assert daemon.workload_hint_reader.wait_ready(5.0)
        tick = daemon.loop.tick().tick
        assert tick.workload_hint == WorkloadHintRecord(stage=WorkloadHintStage.OBSERVE)
        run_id = tick.run_id
    finally:
        daemon.close()
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT r.run_id, e.status FROM workload_hint_runs r "
            "JOIN workload_hint_run_ends e ON e.run_seq = r.run_seq"
        ).fetchall()
    assert rows == [(run_id, "clean")]


def test_fand_with_stage_off_starts_no_reader(tmp_path):
    import json

    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import Config, build
    from test_control_config import write_documents
    from test_control_loop import METRICS_PATH
    from test_simulated_fan_backend import hardware_config

    config_dir = tmp_path / "control"
    config_dir.mkdir()
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(config_dir, documents)
    daemon = build(
        Config(
            config_dir=config_dir,
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            admin_config=tmp_path / "missing.yaml",
        ),
        watchdog=NullWatchdog(),
    )
    try:
        assert daemon.workload_hint_reader is None
        assert daemon.loop.tick().tick.workload_hint == WorkloadHintRecord.off()
    finally:
        daemon.close()


def test_a_slow_reader_does_not_delay_startup_or_the_first_tick(tmp_path, monkeypatch):
    """読み手が run の開始で止まっていても、fand の組み立てと最初の tick は待たない（Codex P1）。

    最初の tick は ``hint_state_unreadable`` の記録になり、読み手が進めば次から読める。
    """
    import json
    import threading
    import time

    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control.schema import HintPriorSkipped
    from coldaisle.control_daemon import Config, build
    from coldaisle.workload_hint_reader import WorkloadHintReader
    from test_control_config import write_documents
    from test_simulated_fan_backend import hardware_config

    release = threading.Event()
    original_begin = WorkloadHintReader.begin

    def slow_begin(self: WorkloadHintReader) -> None:
        release.wait(timeout=30)
        original_begin(self)

    monkeypatch.setattr(WorkloadHintReader, "begin", slow_begin)
    config_dir = tmp_path / "control"
    config_dir.mkdir()
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    documents["fan-policy.yaml"] = hint_policy()
    write_documents(config_dir, documents)
    started = time.monotonic()
    daemon = build(
        Config(
            config_dir=config_dir,
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            admin_config=tmp_path / "missing.yaml",
        ),
        watchdog=NullWatchdog(),
    )
    try:
        tick = daemon.loop.tick().tick
        elapsed = time.monotonic() - started
        # reader_stale_after_ms（5 s）も busy timeout（5 s）も待っていない
        assert elapsed < 3.0
        assert tick.workload_hint == WorkloadHintRecord(
            stage=WorkloadHintStage.OBSERVE, skipped=HintPriorSkipped.HINT_STATE_UNREADABLE
        )
        release.set()
        reader = daemon.workload_hint_reader
        assert reader is not None
        assert reader.wait_ready(5.0)
        assert daemon.loop.tick().tick.workload_hint == WorkloadHintRecord(
            stage=WorkloadHintStage.OBSERVE
        )
    finally:
        release.set()
        daemon.close()
