"""`coldaisle-hint-shadow`（#288 / 決定記録 0162 §2.4）の試験。

- 元の tick の時刻の軸の段の数（候補 → 届いた → recorded → 領域 → 2 条件）と「対応なし」
- 差分の分布（再検査に通った・落ちたで分ける）
- 受け取りの時刻の軸: 区間ごとの差で数える捨てた数・受け入れた行だけの端点・epoch の印・成功率は上限
- 保持期間との整合（``retention_truncated``）
- 読むだけ（DB の bytes が変わらない）・合否を出さない・同じ入力から同じ bytes
- AI 層・API・制御・日次レポート・運用メモリから import されない
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

from coldaisle.clock import SimulatedClock
from coldaisle.control.schema import ControlTick
from coldaisle.hint_shadow import (
    HintShadowEvidenceDatabase,
    HintShadowManifest,
    HintShadowReport,
    PeriodReport,
    build_report,
    main,
    read_report,
    render,
    render_markdown,
)
from coldaisle.store import (
    HintShadowReason,
    HintShadowRecord,
    HintShadowStatus,
    QualityRules,
    SqliteStore,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "coldaisle"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEARNED_CHANNEL = ROOT / "config" / "learned-channel.yaml"
PENDING_MS = 120_000
"""`config/learned-channel.yaml` の `pending_domain_result_ms`（試験はこの値を前提にする）。"""

V17: dict[str, Any] = json.loads((FIXTURES / "control_tick_v17.json").read_text(encoding="utf-8"))
RUN = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
OTHER_RUN = "1" * 32
EPOCH_A = "a" * 32
EPOCH_B = "b" * 32
CERT = "c" * 64
T0 = 1_800_000_000_000
SEC = 1_000


# ---------------------------------------------------------------- 作り方


def trace(
    tick_id: int,
    ts_ms: int,
    *,
    run_id: str = RUN,
    workload: str = "training",
    regime: str = "sustained_gpu",
    candidate: bool = True,
    counters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`stage: shadow_mpc` の v17 の trace。``candidate`` なら ``would_override`` の tick。"""
    body = json.loads(json.dumps(V17))
    body.update(tick_id=tick_id, ts_ms=ts_ms, run_id=run_id)
    if body.get("mode_command", {}).get("run_id") is not None:
        body["mode_command"]["run_id"] = run_id
    body["state"]["workload_regime"] = regime
    body["state"]["regime_confidence"] = 0.9
    hint = body["workload_hint"]
    hint.update(
        event_id=3,
        workload=workload,
        status="active",
        source_label="cli",
        age_ms=1_000,
        influence="would_override" if candidate else "none",
        shadow_counters=counters,
    )
    hint["certificate"]["certificate_sha256"] = CERT
    ControlTick.model_validate_json(json.dumps(body))
    return body


def prediction(values: Iterable[tuple[float, float]]) -> list[dict[str, Any]]:
    """offset ごとの (CPU, GPU) の予測。"""
    return [
        {
            "offset_ms": (index + 1) * 10_000,
            "expected_ts_ms": T0 + (index + 1) * 10_000,
            "values": {"cpu.package_temp_c": cpu, "gpu.core_temp_c": gpu},
        }
        for index, (cpu, gpu) in enumerate(values)
    ]


def document(
    *,
    hint_front: float = 0.45,
    hint_gpu: Iterable[float] = (60.0, 59.0),
    domain: str | None = None,
    monotonicity: str | None = None,
) -> str:
    """fand が書く形（``learned_channel.hint_shadow`` の v1）の ``record_json``。"""
    base_gpu = (60.0, 60.0)
    body = {
        "base": {
            "requested_demand": {"front": 0.4, "rear": 0.4, "top": 0.3},
            "prediction": prediction((70.0, gpu) for gpu in base_gpu),
        },
        "hint": {
            "requested_demand": {"front": hint_front, "rear": 0.4, "top": 0.3},
            "prediction": prediction((70.5, gpu) for gpu in hint_gpu),
        },
        "hint_band": {},
        "recheck": {
            "domain": {"passed": domain is None, "failed": domain},
            "monotonicity": {"passed": monotonicity is None, "failed": monotonicity},
        },
        "thermal_model": {"model_id": "thermal", "version": "1", "artifact_sha256": "d" * 64},
        "config": {},
        "certificate_sha256": CERT,
        "base_result_digest": "e" * 64,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def record(
    tick_id: int,
    source_ts_ms: int,
    *,
    received_ts_ms: int | None = None,
    run_id: str = RUN,
    epoch: str = EPOCH_A,
    status: HintShadowStatus = HintShadowStatus.RECORDED,
    reason: HintShadowReason | None = None,
    record_json: str | None = None,
    dropped: tuple[int, int, int] = (0, 0, 0),
    schema_version: int = 1,
) -> HintShadowRecord:
    if status is HintShadowStatus.RECORDED and record_json is None:
        record_json = document()
    return HintShadowRecord(
        run_id=run_id,
        source_tick_id=tick_id,
        source_ts_ms=source_ts_ms,
        source_monotonic_ms=5_000,
        received_monotonic_ms=5_400,
        received_ts_ms=source_ts_ms + 400 if received_ts_ms is None else received_ts_ms,
        schema_version=schema_version,
        status=status,
        reason=reason,
        record_json=record_json,
        worker_instance_id=epoch,
        shadow_dropped_backpressure=dropped[0],
        shadow_record_too_large=dropped[1],
        shadow_unavailable=dropped[2],
    )


class Evidence:
    """証拠の DB と manifest を tmp_path に作る（書くのは準備のときだけ）。"""

    def __init__(self, tmp_path: Path, rules: QualityRules) -> None:
        self.tmp_path = tmp_path
        self.db = tmp_path / "evidence.db"
        self.manifest = tmp_path / "manifest.yaml"
        self._store = SqliteStore(self.db, rules=rules, clock=SimulatedClock(0))

    def trace(self, body: dict[str, Any]) -> None:
        assert self._store.record_control_trace(
            ts_ms=body["ts_ms"],
            tick_id=body["tick_id"],
            schema_version=body["schema_version"],
            trace_json=json.dumps(body),
        )

    def record(self, row: HintShadowRecord) -> None:
        self._store.record_hint_shadow(row)

    def prune_traces_before(self, cutoff_ms: int) -> None:
        self._store.delete_control_traces_before(cutoff_ms)

    def close(self) -> None:
        self._store.close()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = self.db.with_name(self.db.name + suffix)
            assert not (sidecar.is_file() and sidecar.stat().st_size > 0), sidecar.name

    def periods(self, *periods: dict[str, Any]) -> Path:
        self.manifest.write_text(
            yaml.safe_dump({"schema_version": 1, "periods": list(periods)}), encoding="utf-8"
        )
        return self.manifest

    def report(self, *periods: dict[str, Any]) -> HintShadowReport:
        self.close()
        manifest = HintShadowManifest.from_file(self.periods(*periods))
        with HintShadowEvidenceDatabase(self.db) as store, store.snapshot():
            rows = store.hint_shadow_rows()
            prune = store.control_trace_prune_state()
            traces = {
                period.name: store.control_traces(period.start_ms, period.end_ms)
                for period in manifest.periods
            }
        return build_report(
            manifest=manifest,
            rows=rows,
            traces_by_period=traces,
            prune=prune,
            pending_ms=PENDING_MS,
        )


@pytest.fixture
def evidence(tmp_path: Path, rules: QualityRules) -> Iterable[Evidence]:
    opened = Evidence(tmp_path, rules)
    yield opened
    opened.close()


def period(
    name: str = "p1", start: int = T0, end: int = T0 + 600 * SEC, **extra: Any
) -> dict[str, Any]:
    return {"name": name, "start_ms": start, "end_ms": end, **extra}


def only(report: HintShadowReport) -> PeriodReport:
    (found,) = report.periods
    return found


# ---------------------------------------------------------------- 元の tick の時刻の軸


def test_the_stage_counts_follow_the_candidate_to_the_two_conditions(evidence: Evidence) -> None:
    for index in range(4):
        evidence.trace(trace(index, T0 + index * SEC))
    evidence.trace(trace(4, T0 + 4 * SEC, candidate=False))
    evidence.record(record(0, T0))
    evidence.record(record(1, T0 + SEC, record_json=document(domain="window_features")))
    evidence.record(record(2, T0 + 2 * SEC, record_json=document(monotonicity="gpu")))
    evidence.record(
        record(
            3,
            T0 + 3 * SEC,
            status=HintShadowStatus.SKIPPED,
            reason=HintShadowReason.BUDGET_EXHAUSTED,
        )
    )

    (layer,) = only(evidence.report(period())).source_axis or ()

    assert layer.layer.model_dump() == {
        "run_id": RUN,
        "certificate_sha256": CERT,
        "regime": "sustained_gpu",
        "workload": "training",
        "trace_matched": True,
    }
    assert (layer.candidate_ticks, layer.received_records, layer.recorded) == (4, 4, 3)
    assert (layer.domain_passed, layer.both_passed) == (2, 1)
    assert [(item.name, item.count) for item in layer.domain_failed] == [("window_features", 1)]
    assert [(item.name, item.count) for item in layer.monotonicity_failed] == [("gpu", 1)]
    assert [(item.status.value, item.reason, item.rows) for item in layer.by_status] == [
        ("recorded", None, 3),
        ("skipped", HintShadowReason.BUDGET_EXHAUSTED, 1),
    ]
    assert layer.both_passed_of_received.model_dump() == {
        "numerator": 1,
        "denominator": 4,
        "value": 0.25,
    }


def test_a_record_without_its_trace_is_counted_as_unmatched(evidence: Evidence) -> None:
    """対応する trace の無い記録は「対応なし」の層に数え、捨てない（0133 §2.5a）。"""
    evidence.trace(trace(0, T0))
    evidence.record(record(0, T0))
    # 別の run の同じ (ts_ms, tick_id) は対応しない
    evidence.record(record(0, T0, run_id=OTHER_RUN))

    layers = only(evidence.report(period())).source_axis or ()

    assert [
        (item.layer.run_id, item.layer.trace_matched, item.received_records) for item in layers
    ] == [
        (RUN, True, 1),
        (OTHER_RUN, False, 1),
    ]
    unmatched = layers[1].layer
    assert (unmatched.certificate_sha256, unmatched.regime, unmatched.workload) == (
        CERT,
        None,
        None,
    )


def test_populations_of_different_keys_are_not_merged(evidence: Evidence) -> None:
    evidence.trace(trace(0, T0, workload="training"))
    evidence.trace(trace(1, T0 + SEC, workload="benchmark"))
    evidence.record(record(0, T0))
    evidence.record(record(1, T0 + SEC))

    layers = only(evidence.report(period())).source_axis or ()

    assert [(item.layer.workload, item.candidate_ticks, item.recorded) for item in layers] == [
        ("benchmark", 1, 1),
        ("training", 1, 1),
    ]


def test_the_diffs_are_the_requested_stage_and_the_prediction(evidence: Evidence) -> None:
    evidence.trace(trace(0, T0))
    evidence.trace(trace(1, T0 + SEC))
    evidence.record(record(0, T0, record_json=document(hint_front=0.5, hint_gpu=(61.0, 58.0))))
    evidence.record(record(1, T0 + SEC, record_json=document(domain="cpu_temperature")))

    (layer,) = only(evidence.report(period())).source_axis or ()

    passed = layer.diffs_recheck_passed
    assert passed.records == 1
    front = passed.requested_demand.front
    assert front is not None and front.maximum == pytest.approx(0.1)
    rear = passed.requested_demand.rear
    assert rear is not None and rear.maximum == 0.0
    gpu = {item.metric: item for item in passed.prediction}["gpu.core_temp_c"]
    assert gpu.max_over_offsets is not None and gpu.max_over_offsets.maximum == 1.0
    assert gpu.final_offset is not None and gpu.final_offset.maximum == -2.0
    assert layer.diffs_recheck_failed.records == 1


def test_a_record_of_an_unknown_shape_refuses_the_whole_run(evidence: Evidence) -> None:
    from coldaisle.hint_shadow import HintShadowInputError

    evidence.record(record(0, T0, schema_version=2))
    with pytest.raises(HintShadowInputError, match="知らない形"):
        evidence.report(period())


def test_the_run_filter_keeps_only_the_named_runs(evidence: Evidence) -> None:
    evidence.trace(trace(0, T0))
    evidence.trace(trace(1, T0 + SEC, run_id=OTHER_RUN))
    evidence.record(record(0, T0))
    evidence.record(record(1, T0 + SEC, run_id=OTHER_RUN))

    found = only(evidence.report(period(run_ids=[OTHER_RUN])))

    assert {item.layer.run_id for item in found.source_axis or ()} == {OTHER_RUN}
    assert found.receive_axis is not None
    assert {item.run_id for item in found.receive_axis.epochs} == {OTHER_RUN}


# ---------------------------------------------------------------- 受け取りの時刻の軸


def test_dropped_counts_are_the_difference_within_the_period(evidence: Evidence) -> None:
    """区間の前に捨てた分を区間に入れない（0162 §2.4。PR #365 の Codex P1 の 3）。"""
    start = T0 + 100 * SEC
    evidence.record(record(0, T0, dropped=(5, 1, 2)))
    evidence.record(record(1, start + 10 * SEC, dropped=(7, 1, 2)))
    evidence.record(record(2, start + 20 * SEC, dropped=(9, 2, 2)))

    found = only(evidence.report(period(start=start, end=start + 60 * SEC)))

    assert found.receive_axis is not None
    (epoch,) = found.receive_axis.epochs
    assert epoch.dropped.model_dump() == {
        "shadow_dropped_backpressure": 4,
        "shadow_record_too_large": 1,
        "shadow_unavailable": 0,
    }
    assert epoch.may_include_before_first_record is False
    assert epoch.rate_bound == "upper_bound"
    # 2 件とも通った。分母は届いた 2 + 捨てた 5
    assert epoch.success.model_dump() == {"numerator": 2, "denominator": 7, "value": 2 / 7}
    # ずれうる幅: 区間の開始から最初の観測まで・最後の観測から区間の終わりまで
    assert (epoch.first_observation_lead_ms, epoch.last_observation_tail_ms) == (
        10 * SEC + 400,
        40 * SEC - 400,
    )


def test_an_epoch_without_earlier_rows_starts_from_zero_and_is_marked(evidence: Evidence) -> None:
    evidence.record(record(0, T0 + 10 * SEC, dropped=(3, 0, 0)))

    found = only(evidence.report(period()))

    assert found.receive_axis is not None
    (epoch,) = found.receive_axis.epochs
    assert epoch.dropped.shadow_dropped_backpressure == 3
    assert epoch.may_include_before_first_record is True
    assert epoch.rate_bound == "direction_undetermined"
    assert found.receive_axis.epochs_with_records_before_first_observation == 1


def test_counts_observed_after_the_period_are_not_in_it(evidence: Evidence) -> None:
    """数は運んだ行を受け取った時刻で区間に振り分ける（0162 §2.4。Codex P1 の 5）。"""
    end = T0 + 60 * SEC
    evidence.record(record(0, T0 - 10 * SEC, received_ts_ms=T0 - 5 * SEC, dropped=(1, 0, 0)))
    evidence.record(record(1, T0 + 10 * SEC, dropped=(2, 0, 0)))
    # 元の tick は区間の中、受け取りは区間の後
    evidence.record(record(2, end - SEC, received_ts_ms=end + SEC, dropped=(9, 0, 0)))

    found = only(evidence.report(period(end=end)))

    assert found.receive_axis is not None
    (epoch,) = found.receive_axis.epochs
    assert epoch.dropped.shadow_dropped_backpressure == 1
    assert epoch.received_records == 1
    # 元の tick の時刻の軸には区間の後に受け取った記録も入る（別の表）
    (layer,) = found.source_axis or ()
    assert layer.received_records == 2


def test_rows_with_decreased_counters_are_not_endpoints(evidence: Evidence) -> None:
    """``counters_decreased`` の行は端点に使わないが、届いた記録には数える（Codex P2）。"""
    evidence.record(record(0, T0 - 10 * SEC, received_ts_ms=T0 - 5 * SEC, dropped=(4, 0, 0)))
    evidence.record(
        record(
            1,
            T0 + SEC,
            status=HintShadowStatus.REJECTED,
            reason=HintShadowReason.COUNTERS_DECREASED,
            dropped=(0, 0, 0),
        )
    )
    evidence.record(record(2, T0 + 2 * SEC, dropped=(6, 0, 0)))

    found = only(evidence.report(period()))

    assert found.receive_axis is not None
    (epoch,) = found.receive_axis.epochs
    assert epoch.dropped.shadow_dropped_backpressure == 2
    assert (epoch.received_records, epoch.both_passed) == (2, 1)
    assert epoch.success.denominator == 4


def test_epochs_are_never_differenced_across_a_restart(evidence: Evidence) -> None:
    """新しい epoch は 0 から始まる。切れ目と、区間の中で終わった epoch を数える。"""
    evidence.record(record(0, T0 - 10 * SEC, received_ts_ms=T0 - 5 * SEC, dropped=(10, 0, 0)))
    evidence.record(record(1, T0 + SEC, dropped=(12, 0, 0)))
    evidence.record(record(2, T0 + 5 * SEC, epoch=EPOCH_B, dropped=(1, 0, 0)))
    evidence.record(record(3, T0 + 6 * SEC, epoch=EPOCH_B, dropped=(3, 0, 1)))

    found = only(evidence.report(period()))

    axis = found.receive_axis
    assert axis is not None
    first, second = axis.epochs
    assert (first.worker_instance_id, first.dropped.shadow_dropped_backpressure) == (EPOCH_A, 2)
    assert (first.ended_in_period, first.end_unknown) == (True, False)
    assert (second.worker_instance_id, second.dropped.shadow_dropped_backpressure) == (EPOCH_B, 3)
    assert (second.ended_in_period, second.end_unknown) == (False, True)
    assert second.may_include_before_first_record is True
    assert axis.epoch_switches == 1
    assert (axis.ended_epochs, axis.end_unknown_epochs) == (1, 1)
    assert axis.dropped_total.model_dump() == {
        "shadow_dropped_backpressure": 5,
        "shadow_record_too_large": 0,
        "shadow_unavailable": 1,
    }


def test_the_trace_counters_are_shown_but_not_counted(evidence: Evidence) -> None:
    counters = {
        "worker_instance_id": EPOCH_A,
        "shadow_dropped_backpressure": 99,
        "shadow_record_too_large": 0,
        "shadow_unavailable": 0,
    }
    evidence.trace(trace(0, T0, counters=counters))
    evidence.record(record(0, T0, dropped=(1, 0, 0)))

    found = only(evidence.report(period()))

    assert found.receive_axis is not None
    (epoch,) = found.receive_axis.epochs
    assert epoch.dropped.shadow_dropped_backpressure == 1
    assert epoch.trace_last_counters is not None
    assert epoch.trace_last_counters.shadow_dropped_backpressure == 99


# ---------------------------------------------------------------- 保持期間


def test_a_period_reaching_into_the_pruned_range_is_retention_truncated(
    evidence: Evidence,
) -> None:
    evidence.prune_traces_before(T0)
    evidence.record(record(0, T0 + 10 * SEC))

    report = evidence.report(
        period("early", start=T0 + 10 * SEC + PENDING_MS - 1, end=T0 + 900 * SEC),
        period("late", start=T0 + 10 * SEC + PENDING_MS, end=T0 + 900 * SEC),
    )

    early, late = report.periods
    assert early.status == "retention_truncated"
    assert (early.source_axis, early.receive_axis) == (None, None)
    assert late.status == "summarized"
    assert early.retention.earliest_start_ms == T0 + 10 * SEC + PENDING_MS


def test_a_late_record_widens_the_margin(evidence: Evidence) -> None:
    """余裕を超えて遅れて届いた行があれば、その遅れを余裕に使う（0162 §2.4）。"""
    evidence.prune_traces_before(T0)
    delay = PENDING_MS + 30 * SEC
    evidence.record(
        record(
            0,
            T0,
            received_ts_ms=T0 + delay,
            status=HintShadowStatus.REJECTED,
            reason=HintShadowReason.STALE,
        )
    )

    found = only(evidence.report(period(start=T0 + PENDING_MS, end=T0 + 900 * SEC)))

    assert found.status == "retention_truncated"
    assert found.retention.margin_ms == delay


def test_a_database_without_pruning_is_not_truncated(evidence: Evidence) -> None:
    evidence.record(record(0, T0 + 10 * SEC))

    found = only(evidence.report(period(start=T0)))

    assert found.status == "summarized"
    assert found.retention.earliest_start_ms is None


# ---------------------------------------------------------------- 出力・CLI


def test_the_report_has_no_verdict_and_is_byte_stable(evidence: Evidence) -> None:
    evidence.trace(trace(0, T0))
    evidence.record(record(0, T0))
    report = evidence.report(period())

    first = render(report)
    assert first == render(report)
    keys: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            keys.update(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(first))
    for word in ("pass_fail", "verdict", "usable", "threshold", "recommend", "promote", "ok"):
        assert word not in keys
    markdown = render_markdown(report)
    assert "**合否は出さない。**" in markdown
    assert "実測でも `effective_demand` でもない" in markdown
    assert "**成功率は常に上限（過大でありうる）**" in markdown


def test_the_cli_reads_without_writing_the_database(evidence: Evidence, tmp_path: Path) -> None:
    evidence.trace(trace(0, T0))
    evidence.record(record(0, T0))
    evidence.close()
    manifest = evidence.periods(period())
    before = evidence.db.read_bytes()
    out = tmp_path / "out" / "hint-shadow.json"
    argv = [
        "--evidence",
        str(manifest),
        "--db",
        str(evidence.db),
        "--learned-channel-config",
        str(LEARNED_CHANNEL),
        "--out",
        str(out),
    ]

    assert main(argv) == 0
    assert evidence.db.read_bytes() == before
    report = read_report(out)
    assert report.pending_domain_result_ms == PENDING_MS
    first = out.read_bytes()
    assert main(argv) == 0
    assert out.read_bytes() == first

    markdown = tmp_path / "out" / "hint-shadow.md"
    assert main([*argv[:-2], "--format", "markdown", "--out", str(markdown)]) == 0
    assert markdown.read_text(encoding="utf-8").startswith("# Workload Hint B1")


def test_the_cli_refuses_to_write_over_its_inputs(evidence: Evidence) -> None:
    evidence.close()
    manifest = evidence.periods(period())
    before = evidence.db.read_bytes()
    argv = ["--evidence", str(manifest), "--db", str(evidence.db)]
    assert (
        main([*argv, "--learned-channel-config", str(LEARNED_CHANNEL), "--out", str(evidence.db)])
        == 1
    )
    assert (
        main([*argv, "--learned-channel-config", str(LEARNED_CHANNEL), "--out", str(manifest)]) == 1
    )
    assert evidence.db.read_bytes() == before


def test_the_cli_refuses_a_broken_record_without_writing(
    evidence: Evidence, tmp_path: Path
) -> None:
    evidence.record(record(0, T0, schema_version=2))
    evidence.close()
    out = tmp_path / "hint-shadow.json"
    argv = [
        "--evidence",
        str(evidence.periods(period())),
        "--db",
        str(evidence.db),
        "--learned-channel-config",
        str(LEARNED_CHANNEL),
        "--out",
        str(out),
    ]
    assert main(argv) == 1
    assert not out.exists()


def test_the_manifest_has_no_default_period() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        HintShadowManifest.model_validate({"schema_version": 1, "periods": []})
    with pytest.raises(ValidationError):
        HintShadowManifest.model_validate(
            {"schema_version": 1, "periods": [period(), period()]}, strict=False
        )
    with pytest.raises(ValidationError):
        HintShadowManifest.model_validate(
            {"schema_version": 1, "periods": [period(start=T0, end=T0)]}, strict=False
        )


# ------------------------------------------------- import の向き（0162 §2.4 / 0064 §2.2）


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _reaches_this_module(name: str) -> bool:
    return name == "coldaisle.hint_shadow" or name.startswith("coldaisle.hint_shadow.")


def test_llm_api_reports_memory_and_control_never_import_the_summary() -> None:
    """LLM へ渡さない・制御へ届かない（0162 §2.4）。日次レポート・運用メモリも読まない。"""
    paths = [
        SRC / "server.py",
        SRC / "report.py",
        SRC / "memory.py",
        SRC / "control_daemon.py",
    ]
    for package in (
        "ai",
        "api",
        "control",
        "notify",
        "slackbot",
        "learned_channel",
        "learned_worker",
    ):
        paths.extend(sorted((SRC / package).rglob("*.py")))
    assert all(path.is_file() for path in paths)
    offenders = [
        f"{path.relative_to(SRC)}: {name}"
        for path in paths
        for name in _imported_modules(path)
        if _reaches_this_module(name)
    ]
    assert offenders == []


def test_the_import_detector_catches_a_real_import(tmp_path: Path) -> None:
    """検査そのものの検査。素通しの検査は緑のまま何も守らない。"""
    snippet = tmp_path / "bad.py"
    snippet.write_text(
        "from coldaisle.hint_shadow import main\nimport coldaisle.hint_shadow\n", encoding="utf-8"
    )
    assert sorted(name for name in _imported_modules(snippet) if _reaches_this_module(name)) == [
        "coldaisle.hint_shadow",
        "coldaisle.hint_shadow",
        "coldaisle.hint_shadow.main",
    ]
    # 似た名前（hint_shadow_record）は対象にしない
    assert not _reaches_this_module("coldaisle.hint_shadow_record")


def test_the_cli_only_reads_the_evidence_and_cannot_reach_actuation() -> None:
    imported = _imported_modules(SRC / "hint_shadow.py")
    assert "coldaisle.evaluate.EvidenceDatabase" in imported
    assert "coldaisle.store.SqliteStore" not in imported
    forbidden = (
        "coldaisle.control.loop",
        "coldaisle.control.safety",
        "coldaisle.control.reactive",
        "coldaisle.control.fallback",
        "coldaisle.control.hardware",
        "coldaisle.control_daemon",
        "coldaisle.control_admin",
        "coldaisle.event_entry",
        "coldaisle.ai",
        "coldaisle.store.db",
        "serial",
        "subprocess",
    )
    offenders = [
        name
        for name in imported
        if any(name == item or name.startswith(f"{item}.") for item in forbidden)
    ]
    assert offenders == []
