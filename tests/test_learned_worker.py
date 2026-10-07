"""Learned MPC worker（#86 / 決定記録 0077 段階 3、0101、0107）。

偽 fand（手で作った frame の列・`socketpair`）と、本物の Model Registry に登録した試験用の
反実仮想 artifact v2 で確かめる。hardware は使わない。
"""

from __future__ import annotations

import ast
import hashlib
import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coldaisle.clock import SystemMonotonicClock
from coldaisle.control.config import ControlConfig
from coldaisle.control.fallback.gate import LearnedFailure
from coldaisle.control.learned_handoff import (
    LEARNED_FRAME_SCHEMA_VERSION,
    AvailableCalibration,
    CalibrationUnavailableCode,
    LearnedChannelState,
    LearnedExpectedArtifacts,
    LearnedFrame,
    LearnedRole,
    PinnedArtifact,
    UnavailableCalibration,
    calibration_to_runtime,
)
from coldaisle.control.model.calibration_digest import RuntimeCalibration, calibration_digest
from coldaisle.control.model_registry import ModelRegistry
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.schema import AuthorityStage, ControllerKind, Demand, PerZone, Zone
from coldaisle.control.state import (
    ControlStateSnapshot,
    SnapshotSignal,
    TelemetryHealth,
    TelemetryImportance,
)
from coldaisle.learned_channel.messages import (
    FrameBody,
    HelloBody,
    OutboundEnvelope,
    encode_outbound,
    parse_inbound,
)
from coldaisle.learned_worker.cli import EXIT_ROLE_NOT_SUPPORTED, MpcWorker, main
from coldaisle.learned_worker.client import WorkerChannel
from coldaisle.learned_worker.mpc import (
    FailureCode,
    MpcWorkerCore,
    RegistryArtifactSource,
    RegistryProductionCheck,
    WorkerInputs,
)
from coldaisle.learned_worker.window import (
    FrameHistory,
    WindowUnavailable,
    build_observed_input,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store.models import Quality
from test_control_loop import METRIC_CATALOG_SHA256, METRICS_PATH, catalog, control_config
from test_fallback_controller import fallback_proposal
from test_learned_mpc import (
    CALIBRATION_OFFSETS,
    GPU,
    REGISTRY_LIMITS,
    MpcArtifact,
    make_artifact,
    optimizer_document,
    supervisor_output,
    trained,
)

__all__ = ["catalog", "trained"]  # module 単位の fixture（反実仮想 artifact v2 の学習は1回だけ）

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
RUN_ID = "0123456789abcdef0123456789abcdef"
OTHER_RUN_ID = "fedcba9876543210fedcba9876543210"
TICK_MS = 1_000
START_MS = 1_800_000_000_000
"""偽 fand の最初の snapshot の時刻（壁時計）。"""
CATALOG, CATALOG_SHA256 = MetricCatalog.from_yaml_with_sha256(METRICS_PATH)
VALUES = {"air.front_intake": 23.0, "gpu.0.core": 46.0, "cpu.package": 40.0}
"""偽 fand の snapshot の signal（試験用 artifact の feature の範囲の中）。"""
IN_SUPPORT = {"air.front_intake": 25.0, "gpu.0.core": 50.0, "cpu.package": 40.0}
"""本物の loop の Telemetry に置く値（試験用 artifact の support の cell にある組）。"""


def control() -> ControlConfig:
    """試験用 artifact の action の格子に合う `mpc.optimizer` の Control Config。"""
    return control_config(
        policy={
            "authority_stage": "full",
            "mpc": {
                "period_ms": 1_000,
                "budget_ms": 1_000,
                "valid_ms": 4_000,
                "max_source_age_ms": {"value": 4_000, "status": "provisional"},
                "optimizer": optimizer_document(),
            },
            # 許容幅は1 step（1000 ms）より小さくする（fan-policy の検証）
            "shadow": {
                "enabled": True,
                "outcome_match_tolerance_ms": {"value": 500, "status": "provisional"},
                "applied_demand_tolerance": {"value": 0.01, "status": "provisional"},
            },
        }
    )


CONTROL = control()


def pinned_of(artifact: MpcArtifact) -> PinnedArtifact:
    attestation = artifact.attestation
    return PinnedArtifact(
        model_id=attestation.model_id,
        version=attestation.version,
        artifact_sha256=attestation.artifact_sha256,
    )


def available(offsets: dict[str, float] | None = None) -> AvailableCalibration:
    return AvailableCalibration(offsets_c=dict(CALIBRATION_OFFSETS if offsets is None else offsets))


def snapshot(
    tick_id: int, *, values: dict[str, float] | None = None, omit: tuple[str, ...] = ()
) -> ControlStateSnapshot:
    ts_ms = START_MS + tick_id * TICK_MS
    readings = VALUES if values is None else values
    return ControlStateSnapshot(
        tick_id=tick_id,
        ts_ms=ts_ms,
        monotonic_ms=tick_id * TICK_MS,
        signals=tuple(
            SnapshotSignal(
                metric=metric,
                importance=TelemetryImportance.CRITICAL,
                enabled=True,
                value=value,
                quality=Quality.OK,
                source_ts_ms=ts_ms,
            )
            for metric, value in readings.items()
            if metric not in omit
        ),
        derived=(),
        trends=(),
        telemetry_health=TelemetryHealth.NORMAL,
        critical_unavailable=(),
    )


def frame(
    tick_id: int,
    *,
    pins: PinnedArtifact | None,
    stage: AuthorityStage = AuthorityStage.SHADOW,
    calibration: AvailableCalibration | UnavailableCalibration | None = None,
    applied: float | None = 0.5,
    with_supervisor: bool = True,
    config: ControlConfig = CONTROL,
    catalog_sha256: str = CATALOG_SHA256,
    omit: tuple[str, ...] = (),
) -> LearnedFrame:
    return LearnedFrame(
        snapshot=snapshot(tick_id, omit=omit),
        workload=None,
        supervisor=supervisor_output() if with_supervisor else None,
        baseline=fallback_proposal(0.4),
        safety_floor=PerZone[Demand](front=0.2, rear=0.2, top=0.2),
        applied=PerZone[Demand | None](front=applied, rear=applied, top=applied),
        authority_stage=stage,
        expected_artifacts=LearnedExpectedArtifacts(thermal_model=pins, supervisor_policy=None),
        config=config.runtime_digest(),
        calibration=available() if calibration is None else calibration,
        metric_catalog_sha256=catalog_sha256,
    )


def core_for(artifact: MpcArtifact, *, catalog_sha256: str = CATALOG_SHA256) -> MpcWorkerCore:
    registry = ModelRegistry(artifact.root, limits=REGISTRY_LIMITS)
    return MpcWorkerCore(
        WorkerInputs(control=CONTROL, catalog=CATALOG, catalog_sha256=catalog_sha256),
        artifacts=RegistryArtifactSource(registry),
        production=RegistryProductionCheck(registry, artifact.root),
        monotonic_ms=lambda: 0,
    )


def feed(core: MpcWorkerCore, *frames: LearnedFrame, run_id: str = RUN_ID) -> MpcProposal | None:
    """frame を順に渡し、最後に1周期だけ回す。"""
    for item in frames:
        core.receive(run_id, item)
    return core.step()


def ticks(start: int, count: int, **kwargs: Any) -> list[LearnedFrame]:
    return [frame(tick, **kwargs) for tick in range(start, start + count)]


def failure_code(result: MpcProposal | None) -> str | None:
    assert result is not None
    assert result.failure is LearnedFailure.MODEL_LOAD_FAILURE
    assert result.failure_reason is not None
    return result.failure_reason.code


# ================================================================ frame v3（0101 §2.2）


def test_frame_v3_round_trips_the_calibration_and_the_catalog_hash(trained: MpcArtifact) -> None:
    item = frame(10, pins=pinned_of(trained))
    data = encode_outbound(
        OutboundEnvelope(run_id=RUN_ID, role=LearnedRole.MPC, body=FrameBody(frame=item))
    )
    parsed = OutboundEnvelope.model_validate_json(data)

    assert isinstance(parsed.body, FrameBody)
    assert parsed.body.frame == item
    assert parsed.body.frame.schema_version == LEARNED_FRAME_SCHEMA_VERSION == 3
    assert parsed.body.frame.calibration == available()
    assert parsed.body.frame.metric_catalog_sha256 == CATALOG_SHA256


def _frame_json(item: LearnedFrame) -> dict[str, Any]:
    return json.loads(item.model_dump_json())


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda doc: doc.update(schema_version=2), id="v2"),
        pytest.param(lambda doc: doc.pop("calibration"), id="no-calibration"),
        pytest.param(lambda doc: doc.pop("metric_catalog_sha256"), id="no-catalog-hash"),
        pytest.param(lambda doc: doc.update(calibration={"status": "available"}), id="no-offsets"),
        pytest.param(
            lambda doc: doc.update(calibration={"status": "available", "offsets_c": {"a": "x"}}),
            id="offset-type",
        ),
        pytest.param(
            lambda doc: doc.update(
                calibration={"status": "unavailable", "reason": "FileNotFoundError: /etc/x"}
            ),
            id="free-text-reason",
        ),
        pytest.param(lambda doc: doc.update(calibration=None), id="null-calibration"),
    ],
)
def test_old_or_broken_frames_are_refused_as_a_whole(trained: MpcArtifact, mutate: Any) -> None:
    """v2・較正の欠け / 型の違い・自由記述の理由の frame は frame ごと検証に落ちる（0101 §2.3）。"""
    document = _frame_json(frame(10, pins=pinned_of(trained)))
    mutate(document)
    with pytest.raises(ValidationError):
        LearnedFrame.model_validate_json(json.dumps(document))


@pytest.mark.parametrize("code", list(CalibrationUnavailableCode))
def test_the_unavailable_reason_is_one_of_two_closed_codes(
    code: CalibrationUnavailableCode,
) -> None:
    restored = calibration_to_runtime(UnavailableCalibration(reason=code))
    assert not restored.is_available
    assert restored.unavailable_reason == code.value
    assert {item.value for item in CalibrationUnavailableCode} == {
        "calibration_path_not_given",
        "calibration_unreadable",
    }


def test_the_restored_calibration_has_the_same_digest_as_the_one_fand_read(
    trained: MpcArtifact,
) -> None:
    """往復: frame から戻した較正で、fand が読んだ値と同じ digest になる（0101 §2.7）。"""
    entries = trained.model.manifest.metric_binding.entries
    fand = RuntimeCalibration.available(CALIBRATION_OFFSETS)
    restored = calibration_to_runtime(available())
    assert restored.offsets_c is not None and fand.offsets_c is not None
    digest = calibration_digest(entries, restored.offsets_c)
    assert digest is not None
    assert digest == calibration_digest(entries, fand.offsets_c)


def test_an_empty_available_calibration_stays_available() -> None:
    """`available({})` は全チャネル 0.0 の正当な較正で、`unavailable` と別に扱う（0096 §2.9）。"""
    restored = calibration_to_runtime(available({}))
    assert restored.is_available
    assert dict(restored.offsets_c or {}) == {}


# ================================================================ 観測 window（0107 §2.1 / §2.2）


def _schema(trained: MpcArtifact) -> Any:
    return trained.model.feature_schema


def test_the_window_uses_the_feature_grid_and_the_prior_tick_applied(trained: MpcArtifact) -> None:
    schema = _schema(trained)
    frames = [frame(10, pins=None, applied=0.3), frame(11, pins=None, applied=0.7)]
    built = build_observed_input(frames, schema)

    assert built.observed is not None
    observed = built.observed
    assert observed.action_ts_ms == frames[-1].snapshot.ts_ms
    expected_grid = list(
        range(
            observed.action_ts_ms - schema.window_ms,
            observed.action_ts_ms + 1,
            schema.sample_period_ms,
        )
    )
    assert [cell.ts_ms for cell in observed.window] == expected_grid
    # anchor の action は tick N - 1 の applied（N 自身の 0.7 ではない。0087 の prior_action）
    assert {observed.action.get(zone).effective_demand for zone in Zone} == {0.3}
    last = observed.window[-1]
    assert last.values[GPU] == VALUES[GPU]
    assert not any(last.missing_mask.values())


def test_a_dropped_tick_makes_its_grid_cells_missing_not_interpolated(trained: MpcArtifact) -> None:
    """欠けた frame を飛び越えて as-of しない（tick_id の飛びで証明できる欠け）。"""
    schema = _schema(trained)
    history = FrameHistory()
    for item in (frame(8, pins=None), frame(10, pins=None), frame(11, pins=None)):
        history.add(RUN_ID, item)
    # 格子 [ts(10), ts(11)]。tick 9 の欠けは格子の外なので missing にならない
    built = build_observed_input(history.frames, schema)
    assert built.observed is not None
    assert not any(any(cell.missing_mask.values()) for cell in built.observed.window)

    gap = FrameHistory()
    for item in (frame(9, pins=None), frame(11, pins=None), frame(12, pins=None)):
        gap.add(RUN_ID, item)
    # 格子 [ts(11), ts(12)]。ts(11) に当てる frame 11 の後は 12 で飛んでいない → 使える
    built = build_observed_input(gap.frames, schema)
    assert built.observed is not None
    assert not any(built.observed.window[0].missing_mask.values())

    hole = FrameHistory()
    for item in (frame(9, pins=None), frame(11, pins=None)):
        hole.add(RUN_ID, item)
    # 格子 [ts(10), ts(11)]。ts(10) に as-of で当たるのは 9 で、その後 10 が欠けている
    built = build_observed_input(hole.frames, schema)
    assert built.unavailable is WindowUnavailable.PRIOR_ACTION_MISSING


def test_an_interior_gap_is_passed_as_missing_cells(trained: MpcArtifact) -> None:
    """途中の欠けは missing の cell として propose() へ渡す（0107 §2.3。立ち上がりと区別）。"""
    schema = trained.model.feature_schema.model_copy(
        update={"window_ms": 2_000, "columns": ()}, deep=True
    )
    frames = [frame(8, pins=None), frame(10, pins=None), frame(11, pins=None)]
    # 格子 [ts(9), ts(10), ts(11)]。ts(9) に当たる 8 の後は 10 で、9 が欠けている
    built = build_observed_input(frames, schema)
    assert built.observed is not None
    first, middle, last = built.observed.window
    assert all(first.missing_mask.values()) and set(first.source_ts_ms.values()) == {None}
    assert not any(middle.missing_mask.values())
    assert not any(last.missing_mask.values())


def test_the_newest_frame_needs_no_successor(trained: MpcArtifact) -> None:
    """anchor（最新の frame）の後続はまだ届いていないのが正常（0107 §2.1、Codex P1）。"""
    built = build_observed_input([frame(10, pins=None), frame(11, pins=None)], _schema(trained))
    assert built.observed is not None
    assert not any(built.observed.window[-1].missing_mask.values())


def test_warming_up_and_a_missing_prior_action_produce_nothing(trained: MpcArtifact) -> None:
    schema = _schema(trained)
    assert build_observed_input([frame(10, pins=None)], schema).unavailable is (
        WindowUnavailable.WARMING_UP
    )
    missing = [frame(10, pins=None, applied=None), frame(11, pins=None)]
    assert build_observed_input(missing, schema).unavailable is (
        WindowUnavailable.PRIOR_ACTION_MISSING
    )


def test_stale_and_suspect_follow_the_dataset_rules(trained: MpcArtifact) -> None:
    schema = _schema(trained)
    base = frame(11, pins=None)
    signals = tuple(
        signal.model_copy(
            update={"source_ts_ms": base.snapshot.ts_ms - schema.stale_after_ms}
            if signal.metric == GPU
            else {"quality": Quality.SUSPECT}
        )
        for signal in base.snapshot.signals
    )
    anchor = base.model_copy(
        update={"snapshot": base.snapshot.model_copy(update={"signals": signals})}
    )
    built = build_observed_input([frame(10, pins=None), anchor], schema)
    assert built.observed is not None
    last = built.observed.window[-1]
    assert last.stale_mask[GPU] is True
    assert last.suspect_mask["air.front_intake"] is True
    assert last.missing_mask[GPU] is False


def test_a_run_id_change_or_a_backward_tick_drops_the_history() -> None:
    history = FrameHistory()
    history.add(RUN_ID, frame(10, pins=None))
    history.add(RUN_ID, frame(11, pins=None))
    history.add(OTHER_RUN_ID, frame(0, pins=None))
    assert [item.snapshot.tick_id for item in history.frames] == [0]
    history.add(OTHER_RUN_ID, frame(1, pins=None))
    history.add(OTHER_RUN_ID, frame(1, pins=None))
    assert [item.snapshot.tick_id for item in history.frames] == [1]


# ================================================================ worker の1周期


def test_a_healthy_stream_yields_a_proposal_bound_to_the_frame_stage(trained: MpcArtifact) -> None:
    core = core_for(trained)
    result = feed(core, *ticks(10, 2, pins=pinned_of(trained)))

    assert result is not None and result.failure is None, result
    assert result.proposal is not None
    assert result.proposal.seq == 11
    assert result.binding_authority_stage is AuthorityStage.SHADOW


def test_a_run_without_a_pinned_artifact_sends_nothing(trained: MpcArtifact) -> None:
    assert feed(core_for(trained), *ticks(10, 2, pins=None)) is None


@pytest.mark.parametrize("field", ["supervisor"])
def test_a_frame_without_the_selected_supervisor_sends_nothing(
    trained: MpcArtifact, field: str
) -> None:
    core = core_for(trained)
    items = ticks(10, 2, pins=pinned_of(trained), with_supervisor=False)
    assert feed(core, *items) is None


def test_the_offsets_in_the_frame_reach_l9(trained: MpcArtifact) -> None:
    """L9 は frame の較正で照合する。変えた offset で拒まれる（0101 §2.3 / §2.7）。"""
    changed = dict(CALIBRATION_OFFSETS) | {"front_intake": 1.5}
    result = feed(
        core_for(trained), *ticks(10, 2, pins=pinned_of(trained), calibration=available(changed))
    )
    assert failure_code(result) == FailureCode.MODEL_UNUSABLE
    assert result is not None and result.failure_reason is not None
    assert "L9" in result.failure_reason.detail


def test_an_unavailable_calibration_rejects_a_calibrated_artifact(trained: MpcArtifact) -> None:
    """worker は較正ファイルを開かない。frame が `unavailable` なら較正の掛かる artifact は拒む。"""
    unavailable = UnavailableCalibration(reason=CalibrationUnavailableCode.UNREADABLE)
    result = feed(
        core_for(trained), *ticks(10, 2, pins=pinned_of(trained), calibration=unavailable)
    )
    assert failure_code(result) == FailureCode.MODEL_UNUSABLE
    assert result is not None and result.failure_reason is not None
    assert "L9" in result.failure_reason.detail


def test_an_unavailable_calibration_still_admits_an_artifact_without_calibrated_metrics(
    tmp_path: Path,
) -> None:
    """`null` の artifact（較正の掛かる metric を使わない）は `unavailable` でも通る。"""
    plain = make_artifact(tmp_path / "registry", features=(GPU,))
    unavailable = UnavailableCalibration(reason=CalibrationUnavailableCode.PATH_NOT_GIVEN)
    result = feed(core_for(plain), *ticks(10, 2, pins=pinned_of(plain), calibration=unavailable))
    assert result is not None and result.failure is None, result


def test_a_new_run_id_rebinds_with_its_own_calibration(trained: MpcArtifact) -> None:
    """`expected_artifacts` が同じでも、run_id が変われば新しい較正で作り直す（0101 §2.3）。"""
    core = core_for(trained)
    pins = pinned_of(trained)
    first = feed(core, *ticks(10, 2, pins=pins))
    assert first is not None and first.failure is None

    changed = available(dict(CALIBRATION_OFFSETS) | {"front_intake": 1.5})
    second = feed(core, *ticks(0, 2, pins=pins, calibration=changed), run_id=OTHER_RUN_ID)
    assert failure_code(second) == FailureCode.MODEL_UNUSABLE


def test_a_calibration_change_within_a_run_stops_until_the_next_run(trained: MpcArtifact) -> None:
    """同じ run_id の中で較正が変われば失敗を1回返して止まる（0101 §2.3 / §5 #2）。"""
    core = core_for(trained)
    pins = pinned_of(trained)
    assert feed(core, *ticks(10, 2, pins=pins)) is not None
    changed = available(dict(CALIBRATION_OFFSETS) | {"room_temp": 0.0})

    assert failure_code(feed(core, frame(12, pins=pins, calibration=changed))) == (
        FailureCode.CALIBRATION_CHANGED
    )
    assert core.stopped
    # 元の較正に戻っても同じ run_id では再開しない
    assert feed(core, frame(13, pins=pins)) is None
    resumed = feed(core, *ticks(0, 2, pins=pins), run_id=OTHER_RUN_ID)
    assert resumed is not None and resumed.failure is None


def test_a_config_mismatch_fails_every_period_without_stopping(trained: MpcArtifact) -> None:
    other = control_config(policy={"authority_stage": "limited"})
    core = core_for(trained)
    pins = pinned_of(trained)
    for tick in (10, 11):
        assert failure_code(feed(core, frame(tick, pins=pins, config=other))) == (
            FailureCode.CONFIG_MISMATCH
        )
    assert not core.stopped
    assert feed(core, frame(12, pins=pins)) is not None


def test_a_metric_catalog_mismatch_fails_every_period(trained: MpcArtifact) -> None:
    core = core_for(trained, catalog_sha256="0" * 64)
    pins = pinned_of(trained)
    for tick in (10, 11):
        assert failure_code(feed(core, frame(tick, pins=pins))) == (
            FailureCode.METRIC_CATALOG_MISMATCH
        )


def test_a_feature_metric_missing_from_the_snapshot_stops_the_run(trained: MpcArtifact) -> None:
    core = core_for(trained)
    pins = pinned_of(trained)
    items = ticks(10, 2, pins=pins, omit=("air.front_intake",))
    assert failure_code(feed(core, *items)) == FailureCode.FEATURE_METRIC_NOT_IN_SNAPSHOT
    assert feed(core, frame(12, pins=pins)) is None


def test_a_production_move_stops_the_worker_until_the_next_run(tmp_path: Path) -> None:
    """固定した artifact が production でなくなれば1回失敗を返して止まる（0077 §2.6）。"""
    artifact = make_artifact(tmp_path / "registry")
    core = core_for(artifact)
    pins = pinned_of(artifact)
    assert feed(core, *ticks(10, 2, pins=pins)) is not None

    newer = make_artifact(tmp_path / "registry", version="0.2.0")
    assert failure_code(feed(core, frame(12, pins=pins))) == FailureCode.ARTIFACT_NOT_PRODUCTION
    assert feed(core, frame(13, pins=pins)) is None
    # fand の再起動（別の run_id）が新しい固定を運べば再開する
    resumed = feed(core, *ticks(0, 2, pins=pinned_of(newer)), run_id=OTHER_RUN_ID)
    assert resumed is not None and resumed.failure is None


def test_a_broken_registry_counts_as_not_production(tmp_path: Path) -> None:
    artifact = make_artifact(tmp_path / "registry")
    core = core_for(artifact)
    pins = pinned_of(artifact)
    assert feed(core, *ticks(10, 2, pins=pins)) is not None
    (artifact.root / "registry.json").write_text("{", "utf-8")
    assert failure_code(feed(core, frame(12, pins=pins))) == FailureCode.ARTIFACT_NOT_PRODUCTION


def test_a_raised_stage_rebinds_and_a_lowered_stage_does_not(trained: MpcArtifact) -> None:
    """昇格で束縛を作り直し、`binding_authority_stage` が実効 stage に追従する（0077 §2.3）。"""
    core = core_for(trained)
    pins = pinned_of(trained)
    shadow = feed(core, *ticks(10, 2, pins=pins))
    assert shadow is not None and shadow.binding_authority_stage is AuthorityStage.SHADOW

    limited = feed(core, frame(12, pins=pins, stage=AuthorityStage.LIMITED))
    assert limited is not None and limited.failure is None
    assert limited.binding_authority_stage is AuthorityStage.LIMITED
    assert core.bound_stage is AuthorityStage.LIMITED

    lowered = feed(core, frame(13, pins=pins, stage=AuthorityStage.SHADOW))
    assert lowered is not None and lowered.binding_authority_stage is AuthorityStage.LIMITED
    assert core.bound_stage is AuthorityStage.LIMITED


def test_a_stage_the_registry_does_not_allow_is_a_model_load_failure(tmp_path: Path) -> None:
    shadow_only = make_artifact(
        tmp_path / "registry", authority=(AuthorityStage.SHADOW,), stage=AuthorityStage.SHADOW
    )
    core = core_for(shadow_only)
    pins = pinned_of(shadow_only)
    assert feed(core, *ticks(10, 2, pins=pins)) is not None
    raised = feed(core, frame(12, pins=pins, stage=AuthorityStage.LIMITED))
    assert failure_code(raised) == FailureCode.MODEL_UNUSABLE


# ================================================================ ソケット（偽 fand）


class FakeFand:
    """`socketpair` の片側。hello と frame を送り、結果と heartbeat を受け取る。"""

    def __init__(self) -> None:
        self.fand, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.fand.settimeout(3.0)
        self.channel = WorkerChannel(worker, max_message_bytes=1 << 20)

    def hello(self, run_id: str = RUN_ID, heartbeat_ms: int = 20) -> None:
        self.fand.send(
            encode_outbound(
                OutboundEnvelope(
                    run_id=run_id,
                    role=LearnedRole.MPC,
                    body=HelloBody(heartbeat_interval_ms=heartbeat_ms),
                )
            )
        )

    def send_frame(self, item: LearnedFrame, run_id: str = RUN_ID) -> None:
        self.fand.send(
            encode_outbound(
                OutboundEnvelope(run_id=run_id, role=LearnedRole.MPC, body=FrameBody(frame=item))
            )
        )

    def send_raw(self, payload: dict[str, Any]) -> None:
        self.fand.send(json.dumps(payload).encode("utf-8"))

    def receive(self) -> Any:
        return parse_inbound(self.fand.recv(1 << 20))


def run_worker(core: MpcWorkerCore, fake: FakeFand, *, periods: int) -> threading.Thread:
    worker = MpcWorker(
        core, fake.channel, period_ms=50, hello_timeout_ms=3_000, monotonic=SystemMonotonicClock()
    )
    thread = threading.Thread(target=worker.run, kwargs={"max_periods": periods}, daemon=True)
    thread.start()
    return thread


def test_the_worker_sends_heartbeats_and_results_over_the_socket(trained: MpcArtifact) -> None:
    fake = FakeFand()
    fake.hello()
    for item in ticks(10, 2, pins=pinned_of(trained)):
        fake.send_frame(item)
    thread = run_worker(core_for(trained), fake, periods=6)
    kinds: list[str] = []
    results: list[MpcProposal] = []
    while len(results) < 1 or "heartbeat" not in kinds:
        envelope = fake.receive()
        assert envelope.run_id == RUN_ID and envelope.role is LearnedRole.MPC
        kinds.append(envelope.body.kind)
        if envelope.body.kind == "mpc_result":
            results.append(envelope.body.result)
    thread.join(timeout=3.0)
    assert results[0].failure is None and results[0].proposal is not None


def test_an_old_frame_on_the_wire_is_dropped_and_never_used(trained: MpcArtifact) -> None:
    """v2 の frame は窓にも束縛にも使わない。v3 が無い run_id では提案を出さない（0101 §2.3）。"""
    fake = FakeFand()
    fake.hello()
    for item in ticks(10, 2, pins=pinned_of(trained)):
        document = _frame_json(item)
        document["schema_version"] = 2
        del document["calibration"]
        fake.send_raw(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "role": "mpc",
                "body": {"kind": "frame", "frame": document},
            }
        )
    core = core_for(trained)
    thread = run_worker(core, fake, periods=4)
    thread.join(timeout=3.0)
    assert core.history.frames == ()
    assert fake.channel.dropped.get("invalid") == 2
    fake.fand.settimeout(0.2)
    while True:
        try:
            envelope = fake.receive()
        except TimeoutError:
            break
        assert envelope.body.kind == "heartbeat"


def test_only_frames_are_taken_from_fand_after_hello(trained: MpcArtifact) -> None:
    """hello の後に届いた frame 以外の本文・別の役割の封筒は捨てて数える。"""
    fake = FakeFand()
    fake.hello()
    fake.hello()
    item = frame(10, pins=pinned_of(trained))
    fake.fand.send(
        encode_outbound(
            OutboundEnvelope(run_id=RUN_ID, role=LearnedRole.SUPERVISOR, body=FrameBody(frame=item))
        )
    )
    fake.send_frame(frame(11, pins=pinned_of(trained)))
    assert fake.channel.receive_hello(timeout_s=1.0).run_id == RUN_ID
    received = fake.channel.receive_frames(timeout_s=0.2)
    assert [entry.frame.snapshot.tick_id for entry in received] == [11]
    assert fake.channel.dropped == {"unexpected_body": 1, "role_mismatch": 1}


def test_the_role_supervisor_is_refused_until_stage_4() -> None:
    assert main(
        ["--role", "supervisor", "--registry-root", "x", "--learned-channel-config", "y"]
    ) == (EXIT_ROLE_NOT_SUPPORTED)


# ============================================================ 境界（AGENTS.md ルール1・2・6・10）


FORBIDDEN_IMPORTS = (
    "sqlite3",
    "serial",
    "coldaisle.store",
    "coldaisle.ai",
    "coldaisle.api",
    "coldaisle.server",
    "coldaisle.ingest",
    "coldaisle.control.hardware",
    "coldaisle.control_daemon",
    "coldaisle.control_admin",
    "coldaisle.event_entry",
)


def test_the_worker_package_reads_only_frames() -> None:
    """worker は Telemetry・SQLite・較正ファイル・hwmon・LLM 層を import しない（0077 §2.7）。"""
    for path in sorted((SRC / "learned_worker").glob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                assert not any(
                    name == forbidden or name.startswith(f"{forbidden}.")
                    for forbidden in FORBIDDEN_IMPORTS
                ), f"{path.name} imports {name}"
        assert "calibration.json" not in path.read_text("utf-8")


def test_the_worker_cli_has_no_calibration_path() -> None:
    from coldaisle.learned_worker.cli import build_parser

    options = {option for action in build_parser()._actions for option in action.option_strings}
    assert not any("calibration" in option for option in options)


def test_the_catalog_hash_is_the_hash_of_the_bytes_fand_reads() -> None:
    assert (
        CATALOG_SHA256
        == METRIC_CATALOG_SHA256
        == hashlib.sha256(METRICS_PATH.read_bytes()).hexdigest()
    )


# ============================================== 実ソケットの結合（fand の受付 → loop → Gate）

from coldaisle.control.fallback.gate import ControllerGate  # noqa: E402
from coldaisle.control.loop import StaticAuthority  # noqa: E402
from test_control_loop import Harness  # noqa: E402
from test_learned_channel import (  # noqa: E402
    MPC_UID,
    Running,
    channel,
    no_file_groups,
    short_dir,
    wait_until,
)

__all__ += ["channel", "no_file_groups", "short_dir"]


class Pipeline:
    """本物の受付スレッドと loop に、本物の worker（socket・core）をつないだ足場。"""

    def __init__(self, catalog: MetricCatalog, running: Running, artifact: MpcArtifact) -> None:
        provenance = (
            ModelRegistry(artifact.root, limits=REGISTRY_LIMITS).inspect().trace_provenance()
        )
        config = control_config(
            policy={
                "authority_stage": "limited",
                "mpc": {
                    "period_ms": 1_000,
                    "budget_ms": 500,
                    "valid_ms": 6_000,
                    # 下限 2 * tick 1000 + period 1000 + budget 500 = 3500
                    "max_source_age_ms": {"value": 3_500, "status": "provisional"},
                    "optimizer": optimizer_document(),
                },
                "shadow": {
                    "enabled": True,
                    "outcome_match_tolerance_ms": {"value": 500, "status": "provisional"},
                    "applied_demand_tolerance": {"value": 0.01, "status": "provisional"},
                },
            }
        )
        authority = StaticAuthority(AuthorityStage.LIMITED, config_ceiling=AuthorityStage.LIMITED)
        attestation = artifact.attestation
        gate = ControllerGate(
            config.policy,
            expected_model_version=attestation.version,
            expected_artifact_sha256=attestation.artifact_sha256,
            authority=authority,
        )
        self.running = running
        self.harness = Harness(
            catalog,
            config=config,
            gate=gate,
            authority=authority,
            registry=provenance,
            learned_source=running.mailbox,
            learned_health=running.mailbox,
            learned_sink=running.mailbox,
            learned_calibration=available(),
        )
        # 試験用 artifact の support の中の値（OOD にしない。fan が学習時の範囲へ下がってから採る）
        self.harness.telemetry.values.update(IN_SUPPORT)
        registry = ModelRegistry(artifact.root, limits=REGISTRY_LIMITS)
        self.core = MpcWorkerCore(
            WorkerInputs(control=config, catalog=CATALOG, catalog_sha256=CATALOG_SHA256),
            artifacts=RegistryArtifactSource(registry),
            production=RegistryProductionCheck(registry, artifact.root),
            monotonic_ms=lambda: 0,
        )
        running.peer.uid = MPC_UID
        self.client = WorkerChannel.connect(running.path(LearnedRole.MPC), max_message_bytes=65_536)
        self.hello = self.client.receive_hello(timeout_s=3.0)

    def tick_and_answer(self, *, send: bool = True) -> tuple[Any, MpcProposal | None]:
        """1 tick 回し、その tick の frame を worker が受け取って1周期回す。"""
        result = self.harness.tick()
        tick_id = result.tick.tick_id
        while (latest := self.core.history.latest) is None or latest.snapshot.tick_id < tick_id:
            for received in self.client.receive_frames(timeout_s=3.0):
                self.core.receive(received.run_id, received.frame)
        answer = self.core.step()
        if send and answer is not None:
            self.client.send_result(self.hello.run_id, answer)
            assert wait_until(lambda: self.running.mailbox.poll() == answer)
        return result, answer


def test_a_worker_proposal_reaches_the_gate_and_still_passes_guard_and_safety(
    catalog: MetricCatalog, channel: Any, trained: MpcArtifact
) -> None:
    """偽 fand ではなく本物の受付スレッドと loop。worker の提案は requested にしかならず、
    effective は Reactive Guard と Critical Safety を通る（AGENTS.md ルール2 / 0077 §2.4 の6）。"""
    pipeline = Pipeline(catalog, channel(), trained)
    try:
        pipeline.harness.settle()
        results = [pipeline.tick_and_answer() for _ in range(8)]
        accepted = [
            result
            for result, _ in results
            if result.tick.state.active_controller is ControllerKind.LEARNED_MPC
        ]
        assert accepted, [result.tick.state.fallback_reason for result, _ in results]
        for result in accepted:
            # 採った提案も requested にしかならない。effective は Guard と Critical Safety の合成
            # の結果で、Safety の floor を下回らない（schema の不変条件でも止まる）
            assert result.tick.model_gate is not None
            for zone in Zone:
                demand = result.tick.zones.get(zone).demand
                assert demand.effective >= demand.safety_floor
                if demand.guard_floor is not None:
                    assert demand.effective >= demand.guard_floor
    finally:
        pipeline.client.close()
    assert wait_until(
        lambda: (
            pipeline.running.mailbox.state(LearnedRole.MPC)
            is LearnedChannelState.WORKER_DISCONNECTED
        )
    )
    after = pipeline.harness.tick().tick.state
    assert after.active_controller is ControllerKind.FALLBACK
    assert (after.fallback_reason.code, after.fallback_reason.detail) == (
        "learned_proposal_unavailable",
        "worker_disconnected",
    )


def test_a_result_the_worker_held_too_long_expires_by_its_source_age(
    catalog: MetricCatalog, channel: Any, trained: MpcArtifact
) -> None:
    """`mpc.max_source_age_ms`（0077 §2.4 の4）: 元 snapshot から古い結果は受信直後でも使わない。"""
    pipeline = Pipeline(catalog, channel(), trained)
    try:
        pipeline.harness.settle()
        for _ in range(8):
            pipeline.tick_and_answer()
        _result, held = pipeline.tick_and_answer(send=False)
        assert held is not None and held.failure is None
        for _ in range(5):
            pipeline.harness.tick()
        pipeline.client.send_result(pipeline.hello.run_id, held)
        assert wait_until(lambda: pipeline.running.mailbox.poll() == held)
        state = pipeline.harness.tick().tick.state
    finally:
        pipeline.client.close()

    assert state.active_controller is ControllerKind.FALLBACK
    assert state.fallback_reason is not None
    assert state.fallback_reason.code == "learned_proposal_expired"
    # 6 tick 前の snapshot（1 tick = 1000 ms）。受信からの経過は 0
    assert state.fallback_reason.detail == "source_age_ms=6000; max_source_age_ms=3500"
