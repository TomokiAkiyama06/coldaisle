"""#81 `coldaisle-air-balance-shadow`（決定記録 0093 §2.1）。実機不要。

保存済み decision trace から Air Balance の協調の shadow 集計を
**制御プロセスの外で**作る CLI を試す。

1. 同じ入力からは同じ bytes が出る。DB を書き換えない。合否の欄を持たない
2. `status` / `skip_reason` の件数は鍵を常に全部出す
3. 引き上げ幅（`counterfactual_output - candidate`）の分布は記録した tick だけから zone ごとに作る
4. 引き上げの有無の切り替わりは、記録した tick だけを順に並べて数える（`skipped` を跨ぐ）
5. Top の Fan fault は `safety_state` と同じ tick の `faults` を合わせて数える（0088 §3）
6. 索引の食い違い・v14 未満・設定 hash の不一致・period の外は **run 全体を拒否**する
7. 制御側はこの CLI を import せず、この CLI は書き込み経路を import しない
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.air_balance_shadow import (
    AirBalanceShadowConfig,
    AirBalanceShadowInputError,
    AirBalanceShadowPeriod,
    AirBalanceShadowReport,
    build_report,
    config_binding,
    main,
    read_report,
)
from coldaisle.clock import SimulatedClock
from coldaisle.control.config import CONTROL_CONFIG_VERSION, ControlConfig
from coldaisle.control.schema import (
    AirBalanceCoordinationMode,
    AirBalanceCoordinationRecord,
    AirBalanceCoordinationSkipReason,
    AirBalanceCoordinationStatus,
    AirBalanceRecord,
    AirBalanceTraceState,
    AuthorityRecord,
    AuthorityStage,
    ControlConfigDigest,
    ControlTick,
    ControlTickRuntime,
    Fault,
    FaultCode,
    ModeCommandRecord,
    PerZone,
    ProjectedFloorBasis,
    SafetyState,
    Zone,
)
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.models import ControlTraceRecord
from test_control_config import valid_documents, write_documents
from test_control_schema import (
    REGISTRY_PROVENANCE,
    SAFETY_PROVENANCE,
    fallback_state,
    forced,
    passthrough,
    zones,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "coldaisle"
BASE_TS_MS = 1_700_000_000_000
STEP_MS = 1_000
CANDIDATE = 0.4
"""`passthrough()` の requested。shadow では requested == output == candidate。"""
FORCED_REQUESTED = 0.3
"""`forced()` の requested（EMERGENCY の tick）。"""
MAX_RAISE = 0.2


def ts(step: int) -> int:
    return BASE_TS_MS + step * STEP_MS


def per_zone(front: float, rear: float, top: float) -> PerZone[float]:
    return PerZone[float](front=front, rear=rear, top=top)


def flags(value: bool = False) -> PerZone[bool]:
    return PerZone[bool](front=value, rear=value, top=value)


def shadow_record(front: float, rear: float, top: float) -> AirBalanceCoordinationRecord:
    """`mode: shadow` の評価した tick。値は candidate からの引き上げ幅。"""
    candidate = per_zone(CANDIDATE, CANDIDATE, CANDIDATE)
    counterfactual = per_zone(CANDIDATE + front, CANDIDATE + rear, CANDIDATE + top)
    return AirBalanceCoordinationRecord(
        mode=AirBalanceCoordinationMode.SHADOW,
        status=AirBalanceCoordinationStatus.SHADOW,
        candidate=candidate,
        proposed=counterfactual,
        output=candidate,
        counterfactual_output=counterfactual,
        max_raise=per_zone(MAX_RAISE, MAX_RAISE, MAX_RAISE),
        bounded_by_max_raise=flags(),
        held=flags(),
        projected_floors=per_zone(0.0, 0.0, 0.0),
        projected_floor_basis=PerZone[ProjectedFloorBasis](
            front=ProjectedFloorBasis.NONE,
            rear=ProjectedFloorBasis.NONE,
            top=ProjectedFloorBasis.NONE,
        ),
        before_state=AirBalanceTraceState.UNKNOWN,
        projected_state=AirBalanceTraceState.UNKNOWN,
    )


def skipped_record(
    reason: AirBalanceCoordinationSkipReason, *, baseline: float = CANDIDATE
) -> AirBalanceCoordinationRecord:
    candidate = per_zone(baseline, baseline, baseline)
    return AirBalanceCoordinationRecord(
        mode=AirBalanceCoordinationMode.SHADOW,
        status=AirBalanceCoordinationStatus.SKIPPED,
        skip_reason=reason,
        candidate=candidate,
        output=candidate,
        max_raise=per_zone(MAX_RAISE, MAX_RAISE, MAX_RAISE),
        held=flags(),
    )


class Fixture:
    """設定一式と DB を tmp_path に作る。"""

    def __init__(self, tmp_path: Path) -> None:
        self.config_dir = tmp_path / "pr81-config"
        self.config_dir.mkdir()
        write_documents(self.config_dir, valid_documents())
        self.control = ControlConfig.from_directory(self.config_dir)
        self.db = tmp_path / "pr81.db"
        self.out = tmp_path / "pr81-out" / "air-balance-shadow.json"
        self.evidence = tmp_path / "pr81-evidence.yaml"
        self.evidence.write_text(
            yaml.safe_dump({"schema_version": 1, "period": {"start_ms": ts(0), "end_ms": ts(100)}}),
            encoding="utf-8",
        )

    def tick(
        self,
        step: int,
        record: AirBalanceCoordinationRecord,
        *,
        tick_id: int | None = None,
        faults: tuple[Fault, ...] = (),
        safety_state: SafetyState = SafetyState.NORMAL,
        policy_sha256: str | None = None,
    ) -> ControlTick:
        sources = self.control.sources
        emergency = safety_state is SafetyState.EMERGENCY
        return ControlTick(
            tick_id=step if tick_id is None else tick_id,
            ts_ms=ts(step),
            state=fallback_state(safety_state=safety_state),
            zones=zones(forced() if emergency else passthrough(CANDIDATE)),
            runtime=ControlTickRuntime(
                tick_period_ms=STEP_MS,
                deadline_ms=500,
                duration_ms=10,
                deadline_exceeded=False,
                snapshot_schema_version=1,
                config=ControlConfigDigest(
                    fan_hardware_sha256=sources.fan_hardware.sha256,
                    safety_sha256=sources.safety.sha256,
                    policy_sha256=policy_sha256 or sources.policy.sha256,
                    air_balance_sha256=sources.air_balance.sha256,
                    control_config_version=CONTROL_CONFIG_VERSION,
                    fan_hardware_schema_version=sources.fan_hardware.schema_version,
                    safety_schema_version=sources.safety.schema_version,
                    policy_schema_version=sources.policy.schema_version,
                    air_balance_schema_version=sources.air_balance.schema_version,
                ),
            ),
            safety_provenance=SAFETY_PROVENANCE,
            registry=REGISTRY_PROVENANCE,
            air_balance=AirBalanceRecord.disabled(
                model_id=self.control.air_balance.model_id,
                config_sha256=sources.air_balance.sha256,
            ),
            mode_command=ModeCommandRecord.without_entry(),
            authority=AuthorityRecord(entry="static", config_ceiling=AuthorityStage.FULL),
            air_balance_coordination=record,
            tach_unconfirmed_zones=(),
            faults=faults,
        )

    def store(self, ticks: list[ControlTick]) -> None:
        with SqliteStore(
            self.db,
            rules=QualityRules.from_yaml(ROOT / "config" / "quality.yaml"),
            clock=SimulatedClock(BASE_TS_MS + 1_000 * STEP_MS),
        ) as store:
            for tick in ticks:
                store.record_control_trace(
                    ts_ms=tick.ts_ms,
                    tick_id=tick.tick_id,
                    schema_version=tick.schema_version,
                    trace_json=tick.model_dump_json(),
                )

    def argv(self, *extra: str) -> list[str]:
        return [
            "--evidence",
            str(self.evidence),
            "--db",
            str(self.db),
            "--control-config",
            str(self.config_dir),
            "--config",
            str(ROOT / "config" / "air-balance-shadow.yaml"),
            "--out",
            str(self.out),
            *extra,
        ]

    def build(self, ticks: list[ControlTick]) -> AirBalanceShadowReport:
        return build_report(
            [as_row(tick) for tick in ticks],
            binding=config_binding(self.control),
            period=AirBalanceShadowPeriod(start_ms=ts(0), end_ms=ts(100)),
            settings=AirBalanceShadowConfig(schema_version=1, percentiles=(0.5, 1.0)),
        )


def as_row(tick: ControlTick) -> ControlTraceRecord:
    return ControlTraceRecord(
        ts_ms=tick.ts_ms,
        tick_id=tick.tick_id,
        schema_version=tick.schema_version,
        trace_json=tick.model_dump_json(),
    )


@pytest.fixture
def setup(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def front_fault_tick(setup: Fixture, step: int) -> ControlTick:
    """Front の書き込み失敗（DEGRADED。0078 §2.3 の `zone_fan_fault`）。"""
    return setup.tick(
        step,
        skipped_record(AirBalanceCoordinationSkipReason.ZONE_FAN_FAULT),
        faults=(Fault(code=FaultCode.WRITE_FAILURE, zone=Zone.FRONT),),
        safety_state=SafetyState.DEGRADED,
    )


def top_fault_tick(setup: Fixture, step: int) -> ControlTick:
    """Top の tach stall（無条件の EMERGENCY なので `safety_state`。0088 §2.3）。"""
    return setup.tick(
        step,
        skipped_record(AirBalanceCoordinationSkipReason.SAFETY_STATE, baseline=FORCED_REQUESTED),
        faults=(Fault(code=FaultCode.TACH_STALL, zone=Zone.TOP),),
        safety_state=SafetyState.EMERGENCY,
    )


def emergency_without_fan_fault(setup: Fixture, step: int) -> ControlTick:
    """Fan fault ではない EMERGENCY（`safety_state` だが zone の Fan fault には数えない）。"""
    return setup.tick(
        step,
        skipped_record(AirBalanceCoordinationSkipReason.SAFETY_STATE, baseline=FORCED_REQUESTED),
        faults=(Fault(code=FaultCode.CONFIG_INVALID),),
        safety_state=SafetyState.EMERGENCY,
    )


def mixed_ticks(setup: Fixture) -> list[ControlTick]:
    """評価した tick（上げる・上げない）と skipped を混ぜた区間。"""
    return [
        setup.tick(0, shadow_record(0.0, 0.0, 0.0)),
        setup.tick(1, shadow_record(0.1, 0.0, 0.0)),
        setup.tick(2, shadow_record(0.2, 0.05, 0.0)),
        front_fault_tick(setup, 3),
        setup.tick(4, shadow_record(0.1, 0.0, 0.0)),
        top_fault_tick(setup, 5),
        emergency_without_fan_fault(setup, 6),
        setup.tick(7, shadow_record(0.0, 0.0, 0.0)),
        setup.tick(8, skipped_record(AirBalanceCoordinationSkipReason.OPERATING_MODE)),
        setup.tick(9, shadow_record(0.0, 0.1, 0.0)),
    ]


# ------------------------------------- 1 / 2: 決定論・読み取り専用・件数


def test_the_cli_builds_a_deterministic_report_without_writing_evidence(setup: Fixture) -> None:
    """**同じ入力からは同じ bytes。DB を書き換えない。合否の欄を持たない。**"""
    setup.store(mixed_ticks(setup))
    db_before = setup.db.read_bytes()

    assert main(setup.argv()) == 0
    first = setup.out.read_bytes()
    assert main(setup.argv()) == 0
    assert setup.out.read_bytes() == first
    assert setup.db.read_bytes() == db_before

    document = json.loads(first)
    assert document["schema_version"] == 1
    assert document["mode"] == "shadow"
    assert document["ticks"] == 10
    assert document["config"]["fan_policy_sha256"] == setup.control.sources.policy.sha256
    flattened = json.dumps(document)
    for word in ("verdict", "pass", "usable", "recommend", "threshold", "generated_at"):
        assert word not in flattened
    assert read_report(setup.out).model_dump(mode="json") == document


def test_status_and_skip_reason_counts_carry_every_key(setup: Fixture) -> None:
    report = setup.build(mixed_ticks(setup))

    assert report.status.model_dump() == {
        "off": 0,
        "skipped": 4,
        "not_needed": 0,
        "shadow": 6,
        "applied": 0,
        "failed": 0,
    }
    assert report.skip_reason.model_dump() == {
        "air_balance_disabled": 0,
        "operating_mode": 1,
        "snapshot_unavailable": 0,
        "baseline_unavailable": 0,
        "safety_state": 2,
        "zone_fan_fault": 1,
        "tach_unconfirmed": 0,
    }
    assert report.evaluated_ticks == 6
    assert (report.first_ts_ms, report.last_ts_ms) == (ts(0), ts(9))


# ------------------------------------- 3 / 4: 分布と切り替わり


def test_raise_distribution_comes_only_from_evaluated_ticks(setup: Fixture) -> None:
    report = setup.build(mixed_ticks(setup))
    front = report.zones.front

    assert front.raised_ticks == 3
    assert front.all_ticks is not None and front.all_ticks.count == 6
    assert front.all_ticks.maximum == pytest.approx(0.2)
    assert front.all_ticks.minimum == 0.0
    assert front.raised_only is not None and front.raised_only.count == 3
    assert front.raised_only.minimum == pytest.approx(0.1)
    assert [item.quantile for item in front.raised_only.percentiles] == [0.5, 1.0]

    assert report.zones.rear.raised_ticks == 2
    assert report.zones.top.raised_ticks == 0
    assert report.zones.top.raised_only is None  # 0 で埋めない
    assert report.any_zone.raised_ticks == 4


def test_toggles_skip_over_ticks_without_a_counterfactual(setup: Fixture) -> None:
    """評価した tick だけの列: front 0→1→1→(skip)→1→(skip×2)→0→(skip)→0 = 2 回。"""
    report = setup.build(mixed_ticks(setup))

    assert report.zones.front.toggles == 2
    # rear: 0, 0, 1, 0, 0, 1 → 3 回
    assert report.zones.rear.toggles == 3
    assert report.zones.top.toggles == 0
    # どれか: 0, 1, 1, 1, 0, 1 → 3 回
    assert report.any_zone.toggles == 3


def test_toggles_follow_time_order_not_input_order(setup: Fixture) -> None:
    ticks = mixed_ticks(setup)
    assert setup.build(list(reversed(ticks))) == setup.build(ticks)


def test_an_empty_period_reports_no_distribution(setup: Fixture) -> None:
    report = setup.build([])
    assert report.ticks == 0
    assert report.mode is None
    assert report.zones.front.all_ticks is None
    assert report.status.total() == 0


# ------------------------------------- 5: Fan fault の数え方


def test_top_fan_fault_is_counted_through_safety_state(setup: Fixture) -> None:
    """Top は `safety_state` になる。`zone_fan_fault` だけで数えると取りこぼす（0088 §3）。"""
    report = setup.build(mixed_ticks(setup))

    assert report.fan_fault_skips.model_dump() == {"front": 1, "rear": 0, "top": 1}
    assert report.skip_reason.zone_fan_fault == 1  # Top はここに入っていない


def test_a_safety_state_skip_without_a_fan_fault_is_not_a_fan_fault(setup: Fixture) -> None:
    report = setup.build([emergency_without_fan_fault(setup, 0)])
    assert report.fan_fault_skips.model_dump() == {"front": 0, "rear": 0, "top": 0}
    assert report.skip_reason.safety_state == 1


# ------------------------------------- 6: 拒否


def test_a_trace_from_another_fan_policy_rejects_the_whole_run(setup: Fixture) -> None:
    ticks = mixed_ticks(setup)
    ticks.append(setup.tick(10, shadow_record(0.0, 0.0, 0.0), policy_sha256="f" * 64))
    with pytest.raises(AirBalanceShadowInputError, match="期間を分けて渡す"):
        setup.build(ticks)


def test_a_rejected_run_writes_nothing(setup: Fixture) -> None:
    setup.store([setup.tick(0, shadow_record(0.0, 0.0, 0.0), policy_sha256="f" * 64)])
    assert main(setup.argv()) == 1
    assert not setup.out.exists()


@pytest.mark.parametrize("target", ["db", "wal", "evidence", "policy"])
def test_an_out_that_aliases_an_input_is_refused(setup: Fixture, target: str) -> None:
    """`--out` が DB・添え file・manifest・設定を指せば、読む前に拒んで何も書かない（Codex P1）。"""
    setup.store(mixed_ticks(setup))
    alias = {
        "db": setup.db,
        "wal": setup.db.with_name(setup.db.name + "-wal"),
        "evidence": setup.evidence,
        "policy": setup.config_dir / "fan-policy.yaml",
    }[target]
    before = alias.read_bytes() if alias.exists() else None
    db_before = setup.db.read_bytes()
    argv = setup.argv()
    argv[argv.index("--out") + 1] = str(alias)

    assert main(argv) == 1
    assert setup.db.read_bytes() == db_before
    assert (alias.read_bytes() if alias.exists() else None) == before


def test_an_out_on_the_sidecar_of_a_symlinked_db_target_is_refused(setup: Fixture) -> None:
    """`--db` が symlink なら、実体の隣の添え file も `--out` に取らせない（Codex P1）。"""
    setup.store(mixed_ticks(setup))
    link_dir = setup.db.parent / "pr81-link-dir"
    link_dir.mkdir()
    link = link_dir / "linked.db"
    link.symlink_to(setup.db)
    wal = setup.db.with_name(setup.db.name + "-wal")
    argv = setup.argv()
    argv[argv.index("--db") + 1] = str(link)
    argv[argv.index("--out") + 1] = str(wal)

    assert main(argv) == 1
    assert not wal.exists()


@pytest.mark.parametrize("target", ["evidence", "config", "policy"])
def test_an_aliased_input_is_refused_before_it_is_read(setup: Fixture, target: str) -> None:
    """壊れた入力を `--out` が指していても、読んで落ちる前に exit 1 で拒む（Codex P2）。"""
    alias = {
        "evidence": setup.evidence,
        "config": setup.config_dir / "pr81-shadow.yaml",
        "policy": setup.config_dir / "fan-policy.yaml",
    }[target]
    alias.write_text("{not yaml", encoding="utf-8")
    argv = setup.argv()
    argv[argv.index("--out") + 1] = str(alias)
    if target == "config":
        argv[argv.index("--config") + 1] = str(alias)

    assert main(argv) == 1
    assert alias.read_text(encoding="utf-8") == "{not yaml"


def test_an_out_through_a_symlink_to_the_db_is_refused(setup: Fixture) -> None:
    setup.store(mixed_ticks(setup))
    db_before = setup.db.read_bytes()
    link = setup.db.with_name("pr81-link.json")
    link.symlink_to(setup.db)
    argv = setup.argv()
    argv[argv.index("--out") + 1] = str(link)

    assert main(argv) == 1
    assert setup.db.read_bytes() == db_before


def test_an_index_that_disagrees_with_the_body_rejects_the_run(setup: Fixture) -> None:
    tick = setup.tick(0, shadow_record(0.0, 0.0, 0.0))
    row = as_row(tick).model_copy(update={"tick_id": 99})
    with pytest.raises(AirBalanceShadowInputError, match="索引"):
        build_report(
            [row],
            binding=config_binding(setup.control),
            period=AirBalanceShadowPeriod(start_ms=ts(0), end_ms=ts(100)),
            settings=AirBalanceShadowConfig(schema_version=1, percentiles=(0.5,)),
        )


def test_a_trace_without_coordination_rejects_the_run(setup: Fixture) -> None:
    tick = setup.tick(0, shadow_record(0.0, 0.0, 0.0))
    body: dict[str, Any] = json.loads(tick.model_dump_json())
    body["schema_version"] = 13
    del body["air_balance_coordination"]
    del body["tach_unconfirmed_zones"]
    row = ControlTraceRecord(
        ts_ms=tick.ts_ms, tick_id=tick.tick_id, schema_version=13, trace_json=json.dumps(body)
    )
    with pytest.raises(AirBalanceShadowInputError, match="v14 未満"):
        build_report(
            [row],
            binding=config_binding(setup.control),
            period=AirBalanceShadowPeriod(start_ms=ts(0), end_ms=ts(100)),
            settings=AirBalanceShadowConfig(schema_version=1, percentiles=(0.5,)),
        )


def test_rows_outside_the_period_are_refused(setup: Fixture) -> None:
    with pytest.raises(AirBalanceShadowInputError, match="period の外"):
        setup.build([setup.tick(200, shadow_record(0.0, 0.0, 0.0))])


def test_percentiles_must_be_ordered_and_in_range() -> None:
    with pytest.raises(ValidationError):
        AirBalanceShadowConfig(schema_version=1, percentiles=(0.9, 0.5))
    with pytest.raises(ValidationError):
        AirBalanceShadowConfig(schema_version=1, percentiles=(0.0,))
    shipped = AirBalanceShadowConfig.from_file(ROOT / "config" / "air-balance-shadow.yaml")
    assert shipped.percentiles


def test_the_report_rejects_counts_that_do_not_add_up(setup: Fixture) -> None:
    document = setup.build(mixed_ticks(setup)).model_dump(mode="json")
    document["ticks"] += 1
    with pytest.raises(ValidationError):
        AirBalanceShadowReport.model_validate_json(json.dumps(document), strict=False)


def test_markdown_is_written_from_the_same_report(setup: Fixture) -> None:
    setup.store(mixed_ticks(setup))
    markdown = setup.out.with_suffix(".md")
    assert main([*setup.argv()[:-1], str(markdown), "--format", "markdown"]) == 0
    text = markdown.read_text(encoding="utf-8")
    assert "合否は出さない" in text
    assert "| `top` | 1 |" in text


# ------------------------------------- 7: 制御へ逆流しない


FORBIDDEN_MODULES = (
    "coldaisle.control.hardware",
    "coldaisle.control.safety",
    "coldaisle.control.reactive",
    "coldaisle.control.loop",
    "coldaisle.control.fallback",
    "coldaisle.control.air_balance_coordination",
    "coldaisle.event_entry",
    "coldaisle.control_admin",
    "coldaisle.ai",
    "serial",
    "subprocess",
)


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


def test_the_cli_cannot_reach_the_actuation_path() -> None:
    offenders = [
        name
        for name in _imported_modules(SRC / "air_balance_shadow.py")
        if any(name == item or name.startswith(f"{item}.") for item in FORBIDDEN_MODULES)
    ]
    assert offenders == []


def test_the_cli_does_not_open_a_writable_store() -> None:
    """証拠の DB は `EvidenceDatabase`（`immutable=1`）だけで開く。"""
    imported = _imported_modules(SRC / "air_balance_shadow.py")
    assert "coldaisle.evaluate.EvidenceDatabase" in imported
    assert not any(name.startswith("coldaisle.store.sqlite") for name in imported)
    assert "coldaisle.store.SqliteStore" not in imported


def test_the_control_side_never_imports_the_cli() -> None:
    paths = [SRC / "control_daemon.py", *sorted((SRC / "control").rglob("*.py"))]
    for path in paths:
        assert not any(
            name.startswith("coldaisle.air_balance_shadow") for name in _imported_modules(path)
        ), path.name


def test_the_offline_evaluation_does_not_recompute_the_baseline() -> None:
    """**評価器は Baseline を作り直さない**（決定記録 0093 §2.2）。

    Fallback も協調器も import しないので、Baseline の arm は trace に記録した値
    （協調後の値）を読むだけになる。
    """
    forbidden = ("coldaisle.control.fallback", "coldaisle.control.air_balance_coordination")
    for path in sorted((SRC / "control" / "evaluation").rglob("*.py")):
        imported = _imported_modules(path)
        assert not any(
            name == item or name.startswith(f"{item}.") for name in imported for item in forbidden
        ), path.name
