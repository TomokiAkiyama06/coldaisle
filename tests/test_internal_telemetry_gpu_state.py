"""``sys.gpu_state`` の識別（決定記録 0163 §2.6。#63）を偽の NVML と偽の ``/proc`` で検証する。

GPU・driver・他の利用者の process は要らない（AGENTS.md ルール 7）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.internal_telemetry import (
    GPU_STATE_KEY,
    AdapterResult,
    GpuState,
    GpuStateClassifier,
    GpuStateConfig,
    InternalTelemetryCollector,
    NvmlAdapter,
    NvmlConfig,
    ProcCgroupReader,
    SourceStatus,
    TelemetrySourceKind,
)
from coldaisle.internal_telemetry.gpu_state import (
    CgroupUnreadableError,
    ProcessGoneError,
    unit_from_cgroup,
)
from coldaisle.store import Quality, SqliteStore
from coldaisle.telemetry_daemon import (
    Config,
    InternalTelemetryDaemon,
    build,
    load_gpu_state_classifier,
)
from conftest import CONFIG_DIR
from test_internal_telemetry_nvml import FakeNvml

VLLM = "inference-main.service"
"""試験用の仮の推論サーバの unit 名。"""

SYSTEM_VLLM = f"0::/system.slice/{VLLM}\n"
USER_VLLM = f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{VLLM}\n"
TRAINING_SCOPE = "0::/user.slice/user-1000.slice/session-3.scope\n"
CONTAINER = "0::/system.slice/docker-0123abcd.scope\n"
TRAINING_SERVICE = "0::/system.slice/training-job.service\n"


def config(*units: str) -> GpuStateConfig:
    return GpuStateConfig(ai_units=units or (VLLM,))


# ------------------------------------------------------------- cgroup の path の解釈


@pytest.mark.parametrize(
    ("text", "unit"),
    [
        (SYSTEM_VLLM, VLLM),
        # user の unit は user@<uid>.service ではなく最も内側の .service
        (USER_VLLM, VLLM),
        # 子プロセス（vLLM の EngineCore など）は親と同じ unit の cgroup に入る
        (f"0::/system.slice/{VLLM}/engine-core\n", VLLM),
        (TRAINING_SCOPE, None),
        (CONTAINER, None),
        ("0::/\n", None),
        # cgroup v1 の行が混ざる hybrid でも 0:: の行だけを見る
        (f"12:pids:/system.slice/other.service\n1:name=systemd:/x\n{SYSTEM_VLLM}", VLLM),
    ],
)
def test_the_innermost_service_element_is_the_unit(text: str, unit: str | None):
    assert unit_from_cgroup(text) == unit


def test_a_scope_under_the_user_manager_resolves_to_the_manager():
    """``ai_units`` は ``user@`` を受けないので、この process は compute になる。"""
    text = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-term.scope\n"
    assert unit_from_cgroup(text) == "user@1000.service"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "12:pids:/system.slice/x.service\n",  # cgroup v1 だけ
        f"{SYSTEM_VLLM}{SYSTEM_VLLM}",  # 0:: が2つ
        "0::system.slice/x.service\n",  # 絶対 path でない
    ],
)
def test_cgroups_that_cannot_be_judged_are_unreadable(text: str):
    with pytest.raises(CgroupUnreadableError):
        unit_from_cgroup(text)


# ------------------------------------------------------------- config/gpu_state.yaml


def test_the_repository_config_loads():
    loaded = GpuStateConfig.from_yaml(CONFIG_DIR / "gpu_state.yaml")
    assert loaded.ai_units
    assert all(unit.endswith(".service") for unit in loaded.ai_units)
    assert "ollama.service" in loaded.ai_units


@pytest.mark.parametrize(
    "body",
    [
        "ai_units: []\n",
        "ai_units: inference.service\n",  # 一覧でない
        "ai_units: [inference]\n",  # .service で終わらない
        "ai_units: [.service]\n",
        "ai_units: [inference.scope]\n",
        "ai_units: ['']\n",
        "ai_units: [system.slice/inference.service]\n",  # path
        "ai_units: ['inference main.service']\n",
        "ai_units: [user@1000.service]\n",  # user のサービスマネージャ
        "ai_units: [a.service, a.service]\n",
        "ai_units: [1]\n",
        "other: [a.service]\n",  # 鍵が無い
        "ai_units: [a.service]\nextra: 1\n",
        "- a.service\n",  # 辞書でない
    ],
)
def test_malformed_configs_are_rejected(tmp_path: Path, body: str):
    path = tmp_path / "gpu_state.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises((ValidationError, ValueError)):
        GpuStateConfig.from_yaml(path)


@pytest.mark.parametrize("body", [None, "ai_units: [inference]\n", "ai_units: [\n"])
def test_an_unusable_config_makes_the_state_unknown(tmp_path: Path, body: str | None):
    """形が不正・無い・YAML として壊れている → 起動を止めず unknown（0163 §2.6）。"""
    path = tmp_path / "gpu_state.yaml"
    if body is not None:
        path.write_text(body, encoding="utf-8")

    classifier = load_gpu_state_classifier(path)

    assert classifier.classify(()) is GpuState.UNKNOWN
    assert classifier.classify((10,)) is GpuState.UNKNOWN


# ------------------------------------------------------------- 偽の /proc


def fake_proc(root: Path, cgroups: dict[int, str]) -> Path:
    for pid, text in cgroups.items():
        directory = root / str(pid)
        directory.mkdir(parents=True)
        (directory / "cgroup").write_text(text, encoding="utf-8")
    return root


def test_the_reader_reads_the_cgroup_of_a_pid(tmp_path: Path):
    reader = ProcCgroupReader(fake_proc(tmp_path, {10: SYSTEM_VLLM, 20: TRAINING_SCOPE}))
    assert reader.unit_of(10) == VLLM
    assert reader.unit_of(20) is None


def test_a_pid_that_exited_is_gone(tmp_path: Path):
    reader = ProcCgroupReader(tmp_path, process_exists=lambda _pid: False)
    with pytest.raises(ProcessGoneError):
        reader.unit_of(10)


def test_a_pid_hidden_from_proc_is_unreadable_not_gone(tmp_path: Path):
    """``hidepid=invisible`` では存在する process の ``/proc/<pid>`` も ENOENT になる。

    それを「終わった」と数えると、推論サーバが動いていても idle と記録してしまう。
    """
    reader = ProcCgroupReader(tmp_path, process_exists=lambda _pid: True)
    with pytest.raises(CgroupUnreadableError):
        reader.unit_of(10)


def test_a_cgroup_that_cannot_be_opened_is_unreadable(tmp_path: Path, monkeypatch):
    fake_proc(tmp_path, {10: SYSTEM_VLLM})
    original = Path.read_text

    def denied(self: Path, *args, **kwargs):
        if self.name == "cgroup":
            raise PermissionError(13, "Permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(CgroupUnreadableError):
        ProcCgroupReader(tmp_path).unit_of(10)


def test_a_cgroup_path_that_is_a_directory_is_unreadable(tmp_path: Path):
    (tmp_path / "10" / "cgroup").mkdir(parents=True)
    with pytest.raises(CgroupUnreadableError):
        ProcCgroupReader(tmp_path).unit_of(10)


def test_the_real_reader_resolves_this_process():
    """実の ``/proc`` で自分の cgroup を読める（読めない環境では判定不能の例外）。"""
    import os

    try:
        ProcCgroupReader().unit_of(os.getpid())
    except CgroupUnreadableError:
        pytest.skip("cgroup v2 でない環境")


# ------------------------------------------------------------- 判定の表（0163 §2.6）


def classifier(tmp_path: Path, cgroups: dict[int, str], *units: str) -> GpuStateClassifier:
    return GpuStateClassifier(
        config(*units),
        ProcCgroupReader(fake_proc(tmp_path, cgroups), process_exists=lambda _pid: False),
    )


def test_no_process_is_idle(tmp_path: Path):
    assert classifier(tmp_path, {}).classify(()) is GpuState.IDLE


def test_only_inference_processes_are_ai(tmp_path: Path):
    judged = classifier(tmp_path, {10: SYSTEM_VLLM, 11: f"0::/system.slice/{VLLM}/worker\n"})
    assert judged.classify((10, 11)) is GpuState.AI


def test_a_user_unit_in_the_list_is_ai(tmp_path: Path):
    assert classifier(tmp_path, {10: USER_VLLM}).classify((10,)) is GpuState.AI


@pytest.mark.parametrize("text", [TRAINING_SCOPE, CONTAINER, TRAINING_SERVICE])
def test_only_other_processes_are_compute(tmp_path: Path, text: str):
    assert classifier(tmp_path, {20: text}).classify((20,)) is GpuState.COMPUTE


def test_both_are_mixed(tmp_path: Path):
    judged = classifier(tmp_path, {10: SYSTEM_VLLM, 20: TRAINING_SCOPE})
    assert judged.classify((10, 20)) is GpuState.MIXED


def test_a_pid_that_exited_while_reading_is_not_counted(tmp_path: Path):
    """読む間に消えた PID（ENOENT かつ存在しない）は数えない（0163 §2.6）。"""
    judged = classifier(tmp_path, {10: SYSTEM_VLLM})
    assert judged.classify((10, 99)) is GpuState.AI
    assert judged.classify((99,)) is GpuState.IDLE


def test_one_unreadable_pid_makes_the_whole_state_unknown(tmp_path: Path):
    """読めない PID を compute に数えない（0163 §5 の 5）。"""
    fake_proc(tmp_path, {10: SYSTEM_VLLM, 20: "12:pids:/x\n"})
    judged = GpuStateClassifier(config(), ProcCgroupReader(tmp_path))
    assert judged.classify((10, 20)) is GpuState.UNKNOWN


def test_a_hidden_pid_makes_the_state_unknown_not_idle(tmp_path: Path):
    judged = GpuStateClassifier(config(), ProcCgroupReader(tmp_path, process_exists=lambda _: True))
    assert judged.classify((10,)) is GpuState.UNKNOWN


def test_compute_is_not_evidence_that_no_inference_server_runs(tmp_path: Path):
    """**compute は「AI 由来 0」の証拠ではない**（0163 §2.6）。

    推論サーバの unit が ``ai_units`` から漏れていれば（改名など）、その process も
    compute に数えられる。AI 由来 0 の直接の証拠は idle だけで、compute を使えるのは
    陽性の対照（推論サーバを動かして ai になる）を取れている間だけ。
    """
    renamed = "0::/system.slice/inference-renamed.service\n"
    judged = classifier(tmp_path, {10: renamed})
    assert judged.classify((10,)) is GpuState.COMPUTE


def test_unit_names_match_exactly(tmp_path: Path):
    judged = classifier(tmp_path, {10: f"0::/system.slice/x-{VLLM}\n"})
    assert judged.classify((10,)) is GpuState.COMPUTE


def test_a_state_change_is_logged_once(tmp_path: Path, caplog):
    judged = classifier(tmp_path, {10: SYSTEM_VLLM, 20: TRAINING_SCOPE})
    with caplog.at_level("INFO", logger="coldaisle.internal_telemetry"):
        judged.classify((10,))
        judged.classify((10,))
        judged.classify((10, 20))
    messages = [record for record in caplog.records if "sys.gpu_state" in record.getMessage()]
    assert len(messages) == 2


# ------------------------------------------------------------- NVML adapter に載せる


@dataclass
class FixedClassifier:
    """NVML adapter に渡す偽の判定器。渡された PID を記録する。"""

    state: GpuState = GpuState.AI
    error: Exception | None = None
    seen: list[frozenset[int]] = field(default_factory=list)
    unknown_reasons: list[str] = field(default_factory=list)

    def classify(self, pids):
        self.seen.append(frozenset(pids))
        if self.error is not None:
            raise self.error
        return self.state

    def unknown(self, reason: str) -> GpuState:
        self.unknown_reasons.append(reason)
        return GpuState.UNKNOWN


def nvml(api: FakeNvml, judged, *, enabled: bool = True) -> NvmlAdapter:
    return NvmlAdapter(NvmlConfig(enabled=enabled, gpu_indices=(0,)), api, gpu_state=judged)


def states(result: AdapterResult) -> dict[str, str]:
    return dict(result.states)


def test_the_adapter_passes_the_nvml_pids_and_publishes_the_state():
    judged = FixedClassifier(GpuState.MIXED)
    result = nvml(FakeNvml(processes={0: (10, 20)}), judged).poll()

    assert judged.seen == [frozenset({10, 20})]
    assert states(result) == {GPU_STATE_KEY: "mixed"}


def test_the_adapter_publishes_no_state_without_a_classifier():
    assert nvml(FakeNvml(), None).poll().states == ()


@pytest.mark.parametrize(
    ("api", "enabled"),
    [
        (FakeNvml(fail_init=True), True),
        (FakeNvml(count=2), True),
        (FakeNvml(fail_processes=True), True),
        (FakeNvml(), False),
    ],
    ids=["nvml_unavailable", "device_count", "process_list_failed", "disabled"],
)
def test_nvml_problems_make_the_state_unknown(api: FakeNvml, enabled: bool):
    judged = FixedClassifier(GpuState.IDLE)
    result = nvml(api, judged, enabled=enabled).poll()

    assert states(result) == {GPU_STATE_KEY: "unknown"}
    assert judged.seen == []
    assert judged.unknown_reasons


def test_a_failed_process_list_keeps_the_temperatures():
    """process の一覧が取れなくても温度は欠測にしない（#63「Compute Mode 中も温度欠測なし」）。"""
    result = nvml(FakeNvml(fail_processes=True), FixedClassifier()).poll()
    readings = {reading.metric: reading for reading in result.readings}

    assert readings["gpu.0.core"].quality is Quality.OK
    assert result.status is SourceStatus.OK


def test_a_classifier_failure_keeps_the_temperatures():
    judged = FixedClassifier(error=RuntimeError("bug"))
    result = nvml(FakeNvml(), judged).poll()
    readings = {reading.metric: reading for reading in result.readings}

    assert states(result) == {GPU_STATE_KEY: "unknown"}
    assert readings["gpu.0.core"].value == 60.0


def test_an_unexpected_adapter_error_still_makes_the_state_unknown(monkeypatch):
    """collector で畳むと状態が書かれず、直前の値（idle など）が残り続ける。"""
    judged = FixedClassifier(GpuState.IDLE)
    adapter = nvml(FakeNvml(), judged)

    def broken() -> AdapterResult:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(adapter, "_poll", broken)
    result = adapter.poll()

    assert result.status is SourceStatus.UNAVAILABLE
    assert states(result) == {GPU_STATE_KEY: "unknown"}


# ------------------------------------------------------------- daemon が system_state に書く


def _daemon(tmp_path: Path, rules, adapter: NvmlAdapter):
    clock = SimulatedClock(1_000)
    store = SqliteStore(tmp_path / "gpu_state.db", rules=rules, clock=clock)
    daemon = InternalTelemetryDaemon(
        collector=InternalTelemetryCollector((adapter,), clock),
        store=store,
        interval_ms=2_500,
        source_kind=TelemetrySourceKind.MOCK,
        sleep=lambda _: None,
    )
    return daemon, store, clock


def _rows(store: SqliteStore) -> list[tuple[int, str]]:
    return [
        (int(row[0]), str(row[1]))
        for row in store.connection.execute(
            "SELECT ts_ms, value FROM system_state WHERE key = ? ORDER BY ts_ms", (GPU_STATE_KEY,)
        )
    ]


def test_the_daemon_writes_the_state_only_when_it_changes(tmp_path: Path, rules):
    api = FakeNvml(processes={0: ()})
    fake_proc(tmp_path / "proc", {10: SYSTEM_VLLM})
    judged = GpuStateClassifier(
        config(), ProcCgroupReader(tmp_path / "proc", process_exists=lambda _: False)
    )
    daemon, store, clock = _daemon(tmp_path, rules, nvml(api, judged))

    daemon.run(max_cycles=2)
    clock.advance_to_ms(3_500)
    api.processes = {0: (10,)}
    daemon.run(max_cycles=1)

    assert _rows(store) == [(1_000, "idle"), (3_500, "ai")]
    assert store.current_state(GPU_STATE_KEY) == "ai"


def test_build_with_a_broken_gpu_state_config_records_unknown_and_keeps_collecting(
    tmp_path: Path,
):
    """設定の誤りで収集を止めない（0163 §2.6）。"""
    telemetry = tmp_path / "internal-telemetry.yaml"
    telemetry.write_text(
        "version: 1\ninterval_ms: 2500\n"
        "nvml: {enabled: false, gpu_indices: [0]}\n"
        "proc_stat: {enabled: false, path: /proc/stat}\n"
        "cpu_power: {enabled: false, path: /run/coldaisle-power/cpu-package.json,"
        " stale_after_ms: 5000, max_future_skew_ms: 1000, metric: power.cpu.package,"
        " confirmation: {status: provisional}}\n"
        "hwmon: {enabled: false, root: /sys/class/hwmon, sensors: []}\n",
        encoding="utf-8",
    )
    broken = tmp_path / "gpu_state.yaml"
    broken.write_text("ai_units: [not-a-unit]\n", encoding="utf-8")
    daemon = build(
        Config(
            db=tmp_path / "built.db",
            telemetry=telemetry,
            quality_rules=CONFIG_DIR / "quality.yaml",
            metrics=CONFIG_DIR / "metrics.yaml",
            gpu_state=broken,
        ),
        clock=SimulatedClock(0),
        sleep=lambda _: None,
    )
    try:
        stats = daemon.run(max_cycles=1)
        assert stats.cycles == 1
        assert daemon.store.current_state(GPU_STATE_KEY) == "unknown"
    finally:
        daemon.store.close()
