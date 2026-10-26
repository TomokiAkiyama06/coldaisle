"""frame v4 のヒントの欄を control loop が埋める（#288 / 決定記録 0133 §2.5・0162 §2.5 の PR-A）。

実機なし（simulated backend・注入した時計・偽の受け渡し口・偽の frame の送り出し）。

``IMPLEMENTED_STAGE`` は PR-C の最後のコミットで ``SHADOW_MPC`` に上げた（0162 §2.5）。
照合が通らない run は B0 に留まり、埋めない。

守りたいこと（0162 §2.1）:

1. 実効の段階が B1 以上の tick だけ、``supervisor_hint`` と ``hint_certification`` が組で入る。
   照合が通らない run（実効は B0）では入らない
2. 0133 §2.5 の関係に合わなければ両方 None で、frame そのものは送る（基準の frame を失わない）
3. 同じ入力に対し、``stage: off`` と B1 で各 tick の ``requested`` / ``effective`` が一致する
4. Safety / Guard / Fallback / hardware は frame の新しい型・判定の module を import しない
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from coldaisle.clock import ManualMonotonicClock, SimulatedClock
from coldaisle.control.learned_handoff import LearnedFrame, supervisor_hint_relation_holds
from coldaisle.control.schema import (
    ControlTick,
    HintPriorSkipped,
    WorkloadHintInfluence,
    WorkloadHintStage,
    Zone,
)
from coldaisle.control.supervisor import workload_hint as workload_hint_module
from coldaisle.control.supervisor.policy import SupervisorCoordinator
from coldaisle.control.supervisor.workload_hint import (
    HintCertificationLatch,
    WorkloadHintTracker,
)
from coldaisle.control_daemon import _learned_hint_certification
from coldaisle.hint_certificate import HintCertificateComponents
from coldaisle.hint_certificate_digest import LaunchForm
from coldaisle.hint_certification import HintCertification, HintCertificationResult
from coldaisle.learned_channel import DisabledLearnedChannel, LearnedChannelEntry
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.metrics import MetricCatalog
from conftest import CONFIG_DIR
from hint_frame_world import HINT_CERTIFICATION
from test_control_loop import METRICS_PATH, Harness, control_config
from test_learned_channel import RecordingSink, StaticSource
from test_workload_hint_loop import FeedingMailbox, hint_policy, training_hint


@pytest.fixture(scope="module")
def catalog() -> MetricCatalog:
    """本番と同じ Metric Catalog。"""
    return MetricCatalog.from_yaml(METRICS_PATH)


@pytest.fixture
def b1_implemented() -> None:
    """実装の上限が B1 であること（PR-C の最後のコミットで上げた。0162 §2.5）。"""
    assert workload_hint_module.IMPLEMENTED_STAGE is WorkloadHintStage.SHADOW_MPC


def staged_policy(stage: str) -> dict[str, Any]:
    policy = copy.deepcopy(hint_policy())
    policy["supervisor"]["workload_hint"]["stage"] = stage
    return policy


def harness(
    catalog: MetricCatalog,
    *,
    stage: str = "shadow_mpc",
    certified: bool = True,
    certification: Any = HINT_CERTIFICATION,
    revocation: HintCertificationLatch | None = None,
) -> tuple[Harness, RecordingSink]:
    config = control_config(policy=staged_policy(stage))
    sink = RecordingSink()

    def factory(clock: SimulatedClock, monotonic: ManualMonotonicClock) -> WorkloadHintTracker:
        return WorkloadHintTracker(
            config.policy.supervisor.workload_hint,
            mailbox=FeedingMailbox(monotonic, training_hint(clock)),
            boot_clock=clock,
            monotonic=monotonic,
            certified=certified,
            revocation=revocation,
        )

    h = Harness(
        catalog,
        config=config,
        learned_source=StaticSource(),
        learned_health=sink,
        learned_sink=sink,
        learned_hint_certification=certification,
        workload_hint_factory=None if stage == "off" else factory,
    )
    return h, sink


def run(h: Harness, sink: RecordingSink, count: int = 12) -> list[tuple[ControlTick, LearnedFrame]]:
    ticks = [result.tick for result in h.run(count)]
    assert len(sink.frames) == len(ticks)
    return list(zip(ticks, sink.frames, strict=True))


def _would_override(tick: ControlTick) -> bool:
    return (
        tick.workload_hint is not None
        and tick.workload_hint.influence is WorkloadHintInfluence.WOULD_OVERRIDE
    )


# ---------------------------------------------------------------- 埋める tick（0133 §2.5）


@pytest.mark.usefixtures("b1_implemented")
def test_b1_fills_both_fields_only_on_ticks_that_would_override(catalog: MetricCatalog) -> None:
    h, sink = harness(catalog)
    pairs = run(h, sink)
    filled = [(tick, frame) for tick, frame in pairs if frame.supervisor_hint is not None]
    assert filled, "ヒント有りの context が基準と違う tick が無い（試験の前提）"
    for tick, frame in pairs:
        assert tick.workload_hint is not None
        assert tick.workload_hint.stage is WorkloadHintStage.SHADOW_MPC
        assert (frame.supervisor_hint is None) == (frame.hint_certification is None)
        assert (frame.supervisor_hint is not None) == _would_override(tick)
    for tick, frame in filled:
        hint = frame.supervisor_hint
        assert hint is not None and frame.supervisor is not None
        assert frame.hint_certification == HINT_CERTIFICATION
        assert supervisor_hint_relation_holds(frame.snapshot, frame.supervisor, hint)
        # 基準の出力（trace に残る選ばれた出力）は変えない（B1 は shadow だけ。0162 §2.1）
        assert tick.supervisor is not None
        assert tick.supervisor.selected_output == frame.supervisor
        assert hint.target_band != frame.supervisor.target_band


def test_an_uncertified_run_never_fills_the_hint(catalog: MetricCatalog) -> None:
    """照合が通らない run は B0 に留まり、埋めない（0133 §2.6 / 0152 §2.4）。"""
    h, sink = harness(catalog, certified=False)
    pairs = run(h, sink)
    assert any(_would_override(tick) for tick, _ in pairs)
    for tick, frame in pairs:
        assert tick.workload_hint is not None
        assert tick.workload_hint.stage is WorkloadHintStage.OBSERVE
        assert frame.supervisor_hint is None and frame.hint_certification is None
        if _would_override(tick):
            assert tick.workload_hint.skipped is HintPriorSkipped.CERTIFICATION_MISMATCH


@pytest.mark.usefixtures("b1_implemented")
def test_a_certified_run_at_b1_does_not_report_a_certification_mismatch(
    catalog: MetricCatalog,
) -> None:
    """``active`` の設定でも実装の上限で B1 に留まるだけで、認証の失敗ではない（0152 §2.4）。"""
    h, sink = harness(catalog, stage="active")
    pairs = run(h, sink)
    assert any(_would_override(tick) for tick, _ in pairs)
    for tick, _frame in pairs:
        assert tick.workload_hint is not None
        assert tick.workload_hint.stage is WorkloadHintStage.SHADOW_MPC
        assert tick.workload_hint.skipped is not HintPriorSkipped.CERTIFICATION_MISMATCH


@pytest.mark.usefixtures("b1_implemented")
@pytest.mark.parametrize(
    "variant", ["uncertified", "no_certification", "stage_observe", "stage_off"]
)
def test_nothing_is_filled_without_b1_and_a_certification(
    catalog: MetricCatalog, variant: str
) -> None:
    kwargs: dict[str, Any] = {
        "uncertified": {"certified": False},
        "no_certification": {"certification": None},
        "stage_observe": {"stage": "observe"},
        "stage_off": {"stage": "off"},
    }[variant]
    h, sink = harness(catalog, **kwargs)
    for _, frame in run(h, sink):
        assert frame.supervisor_hint is None and frame.hint_certification is None


@pytest.mark.usefixtures("b1_implemented")
def test_a_revoked_certificate_stops_filling_from_that_tick(catalog: MetricCatalog) -> None:
    latch = HintCertificationLatch()
    h, sink = harness(catalog, revocation=latch)
    before = run(h, sink)
    assert any(frame.supervisor_hint is not None for _, frame in before)
    latch.revoke()
    sink.frames.clear()
    for _, frame in run(h, sink):
        assert frame.supervisor_hint is None and frame.hint_certification is None


# ---------------------------------------------------------------- 関係の検査（0133 §2.5）


@pytest.mark.usefixtures("b1_implemented")
@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"tick_id": 10_000}, id="other-tick"),
        pytest.param({"regime": "unknown"}, id="other-regime"),
        pytest.param({"version": "other-version"}, id="other-version"),
    ],
)
def test_a_hint_that_breaks_the_relation_is_dropped_but_the_frame_is_sent(
    catalog: MetricCatalog, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    original = SupervisorCoordinator.hinted_output

    def broken(self: SupervisorCoordinator, policy_input: Any, decision: Any) -> Any:
        output = original(self, policy_input, decision)
        return None if output is None else output.model_copy(update=change)

    monkeypatch.setattr(SupervisorCoordinator, "hinted_output", broken)
    h, sink = harness(catalog)
    pairs = run(h, sink)
    assert any(_would_override(tick) for tick, _ in pairs)
    for _, frame in pairs:
        assert frame.supervisor_hint is None and frame.hint_certification is None


@pytest.mark.usefixtures("b1_implemented")
def test_a_failing_hint_does_not_lose_the_base_frame(
    catalog: MetricCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: SupervisorCoordinator, policy_input: Any, decision: Any) -> Any:
        raise RuntimeError("ヒント有りの出力の不具合")

    monkeypatch.setattr(SupervisorCoordinator, "hinted_output", boom)
    h, sink = harness(catalog)
    pairs = run(h, sink)
    assert any(_would_override(tick) for tick, _ in pairs)
    for _, frame in pairs:
        assert frame.supervisor is not None or frame.workload is None
        assert frame.supervisor_hint is None and frame.hint_certification is None


def test_the_loop_refuses_a_certification_without_a_frame_sink(catalog: MetricCatalog) -> None:
    with pytest.raises(ValueError, match="hint_certification"):
        Harness(catalog, learned_hint_certification=HINT_CERTIFICATION)


# ------------------------------------------------------------ B1 は Demand に効かない（0162 §2.1）


def _demands(tick: ControlTick) -> list[tuple[float, float]]:
    return [
        (tick.zones.get(zone).demand.requested, tick.zones.get(zone).demand.effective)
        for zone in (Zone.FRONT, Zone.REAR, Zone.TOP)
    ]


def _without_hint(tick: ControlTick) -> dict[str, Any]:
    dumped = tick.model_dump(mode="json")
    dumped.pop("workload_hint")
    # `stage` は fan-policy.yaml の値なので、設定の digest だけは違って当然（他の欄で比べる）
    dumped["runtime"]["config"].pop("policy_sha256")
    return dumped


def _frame_without_hint(frame: LearnedFrame) -> dict[str, Any]:
    dumped = frame.without_hint().model_dump(mode="json")
    dumped["config"].pop("policy_sha256")
    return dumped


@pytest.mark.usefixtures("b1_implemented")
def test_stage_off_and_b1_give_the_same_demands_every_tick(catalog: MetricCatalog) -> None:
    off, off_sink = harness(catalog, stage="off")
    b1, b1_sink = harness(catalog)
    a = run(off, off_sink, 16)
    b = run(b1, b1_sink, 16)
    assert any(frame.supervisor_hint is not None for _, frame in b)
    assert [_demands(tick) for tick, _ in a] == [_demands(tick) for tick, _ in b]
    # ヒントの塊の外は1ビットも変わらない（Supervisor の選択・Gate・Guard・Safety の結果を含む）
    assert [_without_hint(tick) for tick, _ in a] == [_without_hint(tick) for tick, _ in b]
    # 基準の frame も、ヒントの欄の外は同じ
    assert [_frame_without_hint(frame) for _, frame in a] == [
        _frame_without_hint(frame) for _, frame in b
    ]


# ------------------------------------------------------------ fand の配線（control_daemon）


def _entry(settings: LearnedChannelSettings) -> LearnedChannelEntry:
    # 配線の関数が読むのは `settings` だけ（受付スレッドは開かない）
    return LearnedChannelEntry(
        settings=settings,
        mailbox=None,  # type: ignore[arg-type]
        server=None,  # type: ignore[arg-type]
        run_id="0" * 32,
        shutdown_wait_ms=1,
    )


def _certified(**overrides: Any) -> HintCertificationResult:
    components = HintCertificateComponents(
        source_tree="1" * 64,
        lockfile="2" * 64,
        environment="3" * 64,
        hint_policy="8" * 64,
        hint_gate="4" * 64,
        optimizer="5" * 64,
        conditions="6" * 64,
        calibration="7" * 64,
    )
    values: dict[str, Any] = {
        "certification": HintCertification.CERTIFIED,
        "launch_form": LaunchForm.ISOLATED_MODULE,
        "certificate_sha256": "c" * 64,
        "components": components,
    }
    values.update(overrides)
    return HintCertificationResult(**values)


def _channel_settings() -> LearnedChannelSettings:
    return LearnedChannelSettings.from_yaml(CONFIG_DIR / "learned-channel.yaml")


def test_fand_builds_the_frame_certification_from_the_check_and_the_channel_config() -> None:
    settings = _channel_settings()
    assert settings.hint_domain is not None
    built = _learned_hint_certification(_entry(settings), _certified())
    # worker 側の7つの部品だけを写す（``hint_policy`` は fand だけが照合する。0152 §2.6）
    assert built == HINT_CERTIFICATION.model_copy(
        update={
            "domain_lookback_ms": settings.hint_domain.domain_lookback_ms.value,
            "domain_stale_after_ms": settings.hint_domain.domain_stale_after_ms.value,
        }
    )


@pytest.mark.parametrize("variant", ["uncertified", "no_domain", "disabled", "no_channel"])
def test_fand_sends_no_certification_when_any_part_is_missing(variant: str) -> None:
    settings = _channel_settings()
    learned: Any = _entry(settings)
    certification = _certified()
    if variant == "uncertified":
        certification = _certified(
            certification=HintCertification.LOCKFILE_MISMATCH, components=None
        )
    elif variant == "no_domain":
        learned = _entry(settings.model_copy(update={"hint_domain": None}))
    elif variant == "disabled":
        learned = DisabledLearnedChannel()
    else:
        learned = None
    assert _learned_hint_certification(learned, certification) is None
