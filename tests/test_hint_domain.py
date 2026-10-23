"""Workload Hint の認証領域の判定の関数（#288 / 決定記録 0162 §2.2 の判定 1〜5 と古さの検査）。

実機なし。ゲートの仕様は本物の設定の読み込みと ``derive_hint_gate_spec`` で作る。

格子（``test_hint_gate_spec.gate_block``）: 直前 Demand [0.2, 0.9]、CPU 温度は ``upper_c`` から
[-10, +5]、GPU は [-10, 0]、``gpu.0.power`` [50, 300]・``cpu.power`` [20, 20]、safety floor の外接箱
front [0.2, 0.6] / rear [0.2, 0.3] / top [0.2, 0.4]、Baseline (0.4, 0.5, 0.3)。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path
from typing import Any

import pytest

from coldaisle.control.hint_domain import (
    DomainCheck,
    DomainFrame,
    DomainSignal,
    domain_frame,
    judge_certified_domain,
)
from coldaisle.control.hint_gate import derive_hint_gate_spec
from coldaisle.control.learned_handoff import LearnedHintCertification
from coldaisle.control.schema import Demand, PerZone
from coldaisle.control.state import SnapshotSignal, TelemetryImportance
from coldaisle.hint_certificate import HintGateAxis, HintGateSpec
from coldaisle.measurement import Quality
from hint_frame_world import with_hint
from test_hint_gate_spec import documents, load

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
TICK_MS = 1_000
BASE_MS = 1_800_000_000_000
LOOKBACK_MS = 5_000
STALE_MS = 3_000
CPU_UPPER = 70.0
GPU_UPPER = 72.0
N = 10
"""判定する tick。区間は tick 5〜10（時刻 [N − 5000, N]）と、その前の tick 4。"""


@pytest.fixture(scope="module")
def spec(tmp_path_factory: pytest.TempPathFactory) -> HintGateSpec:
    config = load(tmp_path_factory.mktemp("gate"), documents())
    return derive_hint_gate_spec(
        config.policy.supervisor.workload_hint, config.safety.workload_hint
    )


def ts(tick: int) -> int:
    return BASE_MS + tick * TICK_MS


def ok(value: float, tick: int) -> DomainSignal:
    return DomainSignal(value=value, quality=Quality.OK, source_ts_ms=ts(tick))


def good_frame(tick: int) -> DomainFrame:
    return DomainFrame(
        tick_id=tick,
        ts_ms=ts(tick),
        cpu_temperature=ok(CPU_UPPER - 2.0, tick),
        gpu_temperature=ok(GPU_UPPER - 5.0, tick),
        features={"gpu.0.power": ok(200.0, tick), "cpu.power": ok(20.0, tick)},
    )


def frames(start: int = 0, end: int = N, **replace: DomainFrame) -> list[DomainFrame]:
    """tick ``start``〜``end`` の領域の中の frame。``replace`` は ``t<tick>`` を差し替える。"""
    items = [good_frame(tick) for tick in range(start, end + 1)]
    for key, frame in replace.items():
        tick = int(key.removeprefix("t"))
        items[tick - start] = frame
    return items


def zones(front: float, rear: float, top: float) -> PerZone[Demand]:
    return PerZone[Demand](front=front, rear=rear, top=top)


def judge(spec: HintGateSpec, items: list[DomainFrame], **overrides: Any) -> DomainCheck | None:
    kwargs: dict[str, Any] = {
        "key": ("transient_gpu", "training"),
        "previous_demand": PerZone[Demand | None](front=0.5, rear=0.5, top=0.5),
        "safety_floor": zones(0.2, 0.2, 0.2),
        "baseline": zones(0.4, 0.5, 0.3),
        "cpu_upper_c": CPU_UPPER,
        "gpu_upper_c": GPU_UPPER,
        "spec": spec,
        "lookback_ms": LOOKBACK_MS,
        "stale_after_ms": STALE_MS,
    }
    kwargs.update(overrides)
    return judge_certified_domain(items, **kwargs).failed


def test_a_tick_whose_whole_interval_is_inside_passes(spec: HintGateSpec) -> None:
    assert judge(spec, frames()) is None


# ---------------------------------------------------------------- 1〜3: N の軸


@pytest.mark.parametrize(
    "key", [None, ("transient_gpu", "inference_service"), ("idle", "training")]
)
def test_a_key_outside_the_overrides_is_out(spec: HintGateSpec, key: Any) -> None:
    assert judge(spec, frames(), key=key) is DomainCheck.HINT_KEY


@pytest.mark.parametrize(
    "previous",
    [
        None,
        PerZone[Demand | None](front=0.5, rear=None, top=0.5),
        PerZone[Demand | None](front=0.1, rear=0.5, top=0.5),
        PerZone[Demand | None](front=0.5, rear=0.5, top=0.95),
    ],
)
def test_a_previous_demand_outside_or_missing_is_out(spec: HintGateSpec, previous: Any) -> None:
    assert judge(spec, frames(), previous_demand=previous) is DomainCheck.PREVIOUS_DEMAND


def test_the_previous_demand_bounds_are_inside(spec: HintGateSpec) -> None:
    edge = PerZone[Demand | None](front=0.2, rear=0.9, top=0.2)
    assert judge(spec, frames(), previous_demand=edge) is None


def test_a_safety_floor_outside_the_bounding_box_is_out(spec: HintGateSpec) -> None:
    # rear の外接箱は [0.2, 0.3]
    assert judge(spec, frames(), safety_floor=zones(0.6, 0.31, 0.4)) is DomainCheck.SAFETY_FLOOR
    assert judge(spec, frames(), safety_floor=zones(0.6, 0.3, 0.4)) is None


@pytest.mark.parametrize("baseline", [None, zones(0.4, 0.5, 0.31)])
def test_a_baseline_outside_or_missing_is_out(spec: HintGateSpec, baseline: Any) -> None:
    assert judge(spec, frames(), baseline=baseline) is DomainCheck.BASELINE


def test_the_first_failing_item_follows_the_numbering(spec: HintGateSpec) -> None:
    hot = dataclasses.replace(good_frame(N), cpu_temperature=ok(CPU_UPPER + 6.0, N))
    assert judge(spec, frames(t10=hot), key=None) is DomainCheck.HINT_KEY
    assert judge(spec, frames(t10=hot), baseline=None) is DomainCheck.BASELINE
    assert judge(spec, frames(t10=hot)) is DomainCheck.TEMPERATURE_CPU


# ---------------------------------------------------------------- 4: 区間の全 frame の値


@pytest.mark.parametrize("tick", [4, 5, 7, N])
def test_one_frame_of_the_interval_out_of_range_is_out(spec: HintGateSpec, tick: int) -> None:
    """区間の 1 frame だけ温度を外にすると外（window に使わない最古の frame 4 でも。厳しい側）。"""
    hot = dataclasses.replace(good_frame(tick), cpu_temperature=ok(CPU_UPPER + 5.5, tick))
    assert judge(spec, frames(**{f"t{tick}": hot})) is DomainCheck.TEMPERATURE_CPU


def test_a_frame_older_than_the_interval_does_not_matter(spec: HintGateSpec) -> None:
    hot = dataclasses.replace(good_frame(3), cpu_temperature=ok(CPU_UPPER + 50.0, 3))
    assert judge(spec, frames(t3=hot)) is None


def test_temperatures_are_measured_from_the_base_upper_c(spec: HintGateSpec) -> None:
    # 同じ値でも、基準の upper_c が違えば外（band の位置で比べる。0156 §2.4）
    assert judge(spec, frames(), gpu_upper_c=GPU_UPPER - 6.0) is DomainCheck.TEMPERATURE_GPU
    edge = dataclasses.replace(good_frame(N), cpu_temperature=ok(CPU_UPPER + 5.0, N))
    assert judge(spec, frames(t10=edge)) is None


def test_a_window_feature_out_of_range_or_missing_is_out(spec: HintGateSpec) -> None:
    base = good_frame(8)
    over = dataclasses.replace(base, features={**base.features, "gpu.0.power": ok(301.0, 8)})
    assert judge(spec, frames(t8=over)) is DomainCheck.WINDOW_FEATURE
    pinned = dataclasses.replace(base, features={**base.features, "cpu.power": ok(20.5, 8)})
    assert judge(spec, frames(t8=pinned)) is DomainCheck.WINDOW_FEATURE
    absent = dataclasses.replace(base, features={"gpu.0.power": ok(200.0, 8)})
    assert judge(spec, frames(t8=absent)) is DomainCheck.SIGNAL_MISSING


@pytest.mark.parametrize("quality", [Quality.STALE, Quality.SUSPECT, Quality.MISSING])
def test_a_signal_whose_quality_is_not_ok_is_out(spec: HintGateSpec, quality: Quality) -> None:
    bad = dataclasses.replace(
        good_frame(6),
        gpu_temperature=DomainSignal(value=GPU_UPPER - 5.0, quality=quality, source_ts_ms=ts(6)),
    )
    assert judge(spec, frames(t6=bad)) is DomainCheck.SIGNAL_QUALITY


@pytest.mark.parametrize(
    "signal",
    [
        None,
        DomainSignal(value=None, quality=Quality.OK, source_ts_ms=ts(6)),
        DomainSignal(value=CPU_UPPER, quality=Quality.OK, source_ts_ms=None),
    ],
    ids=["absent", "no-value", "no-source-ts"],
)
def test_a_signal_without_a_value_or_a_source_time_is_out(
    spec: HintGateSpec, signal: DomainSignal | None
) -> None:
    bad = dataclasses.replace(good_frame(6), cpu_temperature=signal)
    assert judge(spec, frames(t6=bad)) is DomainCheck.SIGNAL_MISSING


def test_an_ok_signal_with_an_old_source_time_is_stale(spec: HintGateSpec) -> None:
    """quality が ok でも、次の frame の時刻 − ``source_ts_ms`` が閾値以上なら外（0162 §2.2）。"""
    # frame 6 の古さは frame 7 の時刻から測る
    stale_source = ts(7) - STALE_MS
    old = dataclasses.replace(good_frame(6), cpu_temperature=ok(CPU_UPPER, 6))
    old = dataclasses.replace(
        old,
        cpu_temperature=DomainSignal(
            value=CPU_UPPER, quality=Quality.OK, source_ts_ms=stale_source
        ),
    )
    assert judge(spec, frames(t6=old)) is DomainCheck.SIGNAL_STALE
    fresh = dataclasses.replace(
        old,
        cpu_temperature=DomainSignal(
            value=CPU_UPPER, quality=Quality.OK, source_ts_ms=stale_source + 1
        ),
    )
    assert judge(spec, frames(t6=fresh)) is None


def test_the_last_frame_is_aged_against_its_own_time(spec: HintGateSpec) -> None:
    old = dataclasses.replace(
        good_frame(N),
        gpu_temperature=DomainSignal(
            value=GPU_UPPER - 5.0, quality=Quality.OK, source_ts_ms=ts(N) - STALE_MS
        ),
    )
    assert judge(spec, frames(t10=old)) is DomainCheck.SIGNAL_STALE
    assert judge(spec, frames(t10=old), stale_after_ms=STALE_MS + 1) is None


# ---------------------------------------------------------------- 5: 区間の欠け


def test_a_tick_gap_inside_the_interval_is_out(spec: HintGateSpec) -> None:
    items = [frame for frame in frames() if frame.tick_id != 7]
    assert judge(spec, items) is DomainCheck.INTERVAL_GAP


def test_a_gap_between_the_preceding_frame_and_the_interval_is_out(spec: HintGateSpec) -> None:
    items = [frame for frame in frames() if frame.tick_id != 5]
    # tick 4 が区間の前の frame、tick 6 が区間の中の最古の frame（tick 5 が欠けた）
    assert judge(spec, items) is DomainCheck.INTERVAL_GAP


def test_a_gap_before_the_interval_does_not_matter(spec: HintGateSpec) -> None:
    items = [frame for frame in frames() if frame.tick_id != 2]
    assert judge(spec, items) is None


def test_an_interval_without_a_preceding_frame_is_uncovered(spec: HintGateSpec) -> None:
    assert judge(spec, frames(start=6)) is DomainCheck.INTERVAL_UNCOVERED
    # 区間の先頭ちょうどの frame があれば、先頭の格子時刻は覆われる
    assert judge(spec, frames(start=5)) is None
    assert judge(spec, frames(start=6), lookback_ms=4_000) is None
    assert judge(spec, [], lookback_ms=0) is DomainCheck.INTERVAL_UNCOVERED


def test_frames_out_of_order_are_out(spec: HintGateSpec) -> None:
    items = frames()
    items[6], items[7] = items[7], items[6]
    assert judge(spec, items) is DomainCheck.INTERVAL_GAP


# ---------------------------------------------------------------- 仕様の出どころ（0152 §2.3）


def test_the_judgement_uses_only_the_spec_it_is_given(spec: HintGateSpec) -> None:
    """証明書の ``certified_domain`` を書き換えても fand の判定は変わらない（0152 §2.8）。

    判定の入力に証明書は無く、frame の ``hint_certification`` も領域を運ばない。呼び出し側は
    自分の設定から導いた仕様を渡す。広げた（書き換えた）仕様なら通る値でも、設定の仕様では外。
    """
    assert "certificate" not in " ".join(inspect.signature(judge_certified_domain).parameters)
    assert not {"certified_domain", "eps", "eps_c"} & set(LearnedHintCertification.model_fields)
    wide = HintGateAxis.of((-50.0, 50.0))
    forged = spec.certified_domain.model_copy(update={"temperature_cpu": wide})
    forged_spec = spec.model_copy(update={"certified_domain": forged})
    hot = dataclasses.replace(good_frame(N), cpu_temperature=ok(CPU_UPPER + 20.0, N))
    assert judge(spec, frames(t10=hot)) is DomainCheck.TEMPERATURE_CPU
    assert judge(forged_spec, frames(t10=hot)) is None


def test_a_learned_frame_is_reduced_to_its_axes(spec: HintGateSpec) -> None:
    from coldaisle.metrics import MetricCatalog
    from test_control_loop import METRICS_PATH, Harness
    from test_learned_channel import RecordingSink, StaticSource  # 重い fixture を遅らせる

    sink = RecordingSink()
    harness = Harness(
        MetricCatalog.from_yaml(METRICS_PATH),
        learned_source=StaticSource(),
        learned_health=sink,
        learned_sink=sink,
    )
    harness.settle()
    frame = with_hint(sink.frames[-1])
    snapshot = frame.snapshot
    metric = snapshot.signals[0].metric
    reduced = domain_frame(
        frame, cpu_temperature_metric=metric, gpu_temperature_metric="no.such.metric", spec=spec
    )
    assert reduced.tick_id == snapshot.tick_id and reduced.ts_ms == snapshot.ts_ms
    signal = snapshot.signals_by_metric[metric]
    assert reduced.cpu_temperature == DomainSignal(
        value=signal.value, quality=signal.quality, source_ts_ms=signal.source_ts_ms
    )
    assert reduced.gpu_temperature is None
    assert set(reduced.features) == {"cpu.power", "gpu.0.power"}


def test_a_disabled_signal_counts_as_missing() -> None:
    from coldaisle.control.hint_domain import domain_signal

    disabled = SnapshotSignal(
        metric="cpu.package",
        importance=TelemetryImportance.CRITICAL,
        enabled=False,
        value=40.0,
        quality=Quality.OK,
        source_ts_ms=ts(0),
    )
    reduced = domain_signal(disabled)
    assert reduced is not None and reduced.value is None


# -------------------------------------------------------- import の走査（0133 §2.7 / 0162 §2.1）

NEW_MODULES = ("coldaisle.control.hint_domain",)
NEW_NAMES = {
    "LearnedHintCertification",
    "LearnedHintComponents",
    "supervisor_hint_relation_holds",
    "DomainCheck",
    "DomainFrame",
    "DomainSignal",
    "DomainJudgement",
    "judge_certified_domain",
}
INDEPENDENT = ("safety", "reactive", "fallback", "hardware")


def _imports(path: Path) -> list[tuple[str, set[str]]]:
    found: list[tuple[str, set[str]]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(alias.name, set()) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.append((node.module, {alias.name for alias in node.names}))
    return found


@pytest.mark.parametrize("package", INDEPENDENT)
def test_safety_guard_fallback_and_hardware_do_not_import_the_new_hint_parts(package: str) -> None:
    offenders: list[str] = []
    for path in sorted((SRC / "control" / package).rglob("*.py")):
        for module, names in _imports(path):
            if module.startswith(NEW_MODULES):
                offenders.append(f"{path.name}: {module}")
            offenders += [f"{path.name}: {module}.{name}" for name in names & NEW_NAMES]
    assert offenders == []


def test_the_domain_module_does_not_import_safety_guard_fallback_or_hardware() -> None:
    forbidden = tuple(f"coldaisle.control.{name}" for name in INDEPENDENT)
    imported = [module for module, _ in _imports(SRC / "control" / "hint_domain.py")]
    assert not [module for module in imported if module.startswith(forbidden)]
