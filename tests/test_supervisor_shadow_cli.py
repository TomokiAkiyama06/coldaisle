"""#89 `coldaisle-supervisor-shadow`（決定記録 0074 §2.1）。実機不要。

保存済み decision trace から Shadow 台帳を**制御プロセスの外で**組み立てる CLI を試す。
ここでも「守れているか」ではなく「破れないか」を試す。

1. 同じ入力からは同じ bytes が出る。DB も Registry も書き換えない
2. 振り分けの件数は **3つの鍵を常に全部**出す。集計は包みの中で変わらない
3. 再起動を跨いだ trace（`tick_id` が 0 に戻る）を同じ tick として衝突させない
4. 索引の食い違い・v8 未満・設定 hash の不一致・台帳の拒否は **run 全体を拒否**する
5. RL の識別は Registry の attestation から作る（manifest は model ID と版だけ）
6. 包みの digest は validator が作り直して照合する
7. 制御側はこの CLI を import せず、この CLI は Registry の書く操作を呼ばない
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.clock import SimulatedClock
from coldaisle.control.config import ControlConfig
from coldaisle.control.model_registry import ModelRegistry
from coldaisle.control.schema import (
    ControlConfigDigest,
    ControlTick,
    ControlTickRuntime,
    SupervisorDecision,
    SupervisorPolicyEvaluation,
    SupervisorPolicyIdentity,
    SupervisorPolicyKind,
    WorkloadRegime,
)
from coldaisle.control.supervisor import (
    RulePolicy,
    RulePolicyTable,
    SupervisorPolicyBinding,
    SupervisorShadowLedger,
    rule_policy_table,
)
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.supervisor_shadow import (
    ShadowFilteredCounts,
    ShadowPeriod,
    SupervisorShadowInputError,
    SupervisorShadowRunReport,
    build_run_report,
    main,
    read_run_report,
    render,
)
from test_control_config import valid_documents, write_documents
from test_control_schema import SAFETY_PROVENANCE, fallback_state, passthrough, zones
from test_rl_supervisor_policy import (
    artifact,
    register_policy,
    rl_policy_config,
    supervisor_output,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "coldaisle"
BASE_TS_MS = 1_700_000_000_000
STEP_MS = 1_000
REGIME = WorkloadRegime.SUSTAINED_GPU


# ---------------------------------------------------------------- 下ごしらえ


class Fixture:
    """設定一式・Registry・DB を tmp_path に作る。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.config_dir = tmp_path / "pr89-config"
        self.config_dir.mkdir()
        write_documents(self.config_dir, valid_documents())
        self.control = ControlConfig.from_directory(self.config_dir)
        self.bounds = self.control.policy.supervisor.output_bounds
        self.rule_table: RulePolicyTable = rule_policy_table(
            RulePolicy(self.control.policy.supervisor.rule_policy, SimulatedClock(0))
        )
        self.registry_root = tmp_path / "pr89-registry"
        verified = register_policy(self.registry_root, artifact(self.bounds))
        self.rl_identity: SupervisorPolicyIdentity = SupervisorPolicyBinding.for_shadow(
            verified, expected_policy_version="0.1.0", bounds=self.bounds
        ).identity
        self.rl_policy_path = tmp_path / "pr89-rl-policy.yaml"
        self.rl_policy_path.write_text(yaml.safe_dump(rl_policy_document()), encoding="utf-8")
        self.db = tmp_path / "pr89.db"
        self.out = tmp_path / "pr89-out" / "supervisor-shadow.json"
        self.evidence = tmp_path / "pr89-evidence.yaml"
        self.write_evidence()

    @property
    def policy_sha256(self) -> str:
        return self.control.sources.policy.sha256

    def write_evidence(
        self,
        *,
        start_ms: int = BASE_TS_MS,
        end_ms: int = BASE_TS_MS + 100 * STEP_MS,
        model_id: str = "rl-supervisor-test",
        version: str = "0.1.0",
    ) -> None:
        self.evidence.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "period": {"start_ms": start_ms, "end_ms": end_ms},
                    "rl_artifact": {"model_id": model_id, "version": version},
                }
            ),
            encoding="utf-8",
        )

    def store(self, rows: list[tuple[int, int, int, str]]) -> None:
        with SqliteStore(
            self.db,
            rules=QualityRules.from_yaml(ROOT / "config" / "quality.yaml"),
            clock=SimulatedClock(BASE_TS_MS + 1_000 * STEP_MS),
        ) as store:
            for ts_ms, tick_id, version, body in rows:
                store.record_control_trace(
                    ts_ms=ts_ms, tick_id=tick_id, schema_version=version, trace_json=body
                )

    def argv(self) -> list[str]:
        return [
            "--evidence",
            str(self.evidence),
            "--registry-root",
            str(self.registry_root),
            "--registry-limits",
            str(ROOT / "config"),
            "--db",
            str(self.db),
            "--control-config",
            str(self.config_dir),
            "--rl-policy",
            str(self.rl_policy_path),
            "--out",
            str(self.out),
        ]

    # ------------------------------------------------ decision / trace の組み立て

    def rule_output(self, tick_id: int, ts_ms: int, **overrides: Any) -> Any:
        entry = self.rule_table.entry(REGIME)
        output = supervisor_output(
            tick_id=tick_id,
            ts_ms=ts_ms,
            policy=SupervisorPolicyKind.RULE,
            version=self.rule_table.version,
            regime=REGIME,
            strategy=entry.strategy,
            output_weights=entry.weights,
            output_band=entry.target_band,
        )
        return output.model_copy(update=overrides) if overrides else output

    def rl_output(self, tick_id: int, ts_ms: int, *, strategy: str = "balanced") -> Any:
        return supervisor_output(
            tick_id=tick_id,
            ts_ms=ts_ms,
            policy=SupervisorPolicyKind.RL,
            version=self.rl_identity.version,
            regime=REGIME,
            strategy=strategy,
        )

    def paired(self, tick_id: int, ts_ms: int, *, strategy: str = "balanced") -> SupervisorDecision:
        return SupervisorDecision(
            tick_id=tick_id,
            ts_ms=ts_ms,
            snapshot_schema_version=1,
            active=SupervisorPolicyEvaluation(
                policy=SupervisorPolicyKind.RULE, output=self.rule_output(tick_id, ts_ms)
            ),
            shadow=SupervisorPolicyEvaluation(
                policy=SupervisorPolicyKind.RL,
                output=self.rl_output(tick_id, ts_ms, strategy=strategy),
                policy_identity=self.rl_identity,
                received_monotonic_ms=ts_ms,
                source_monotonic_ms=ts_ms,
            ),
        )

    def rule_only(self, tick_id: int, ts_ms: int) -> SupervisorDecision:
        return SupervisorDecision(
            tick_id=tick_id,
            ts_ms=ts_ms,
            snapshot_schema_version=1,
            active=SupervisorPolicyEvaluation(
                policy=SupervisorPolicyKind.RULE, output=self.rule_output(tick_id, ts_ms)
            ),
        )

    def tick(
        self,
        tick_id: int,
        ts_ms: int,
        decision: SupervisorDecision | None,
        *,
        policy_sha256: str | None = None,
    ) -> ControlTick:
        state: dict[str, Any] = {}
        if decision is not None:
            selected = decision.selected_output
            assert selected is not None
            state = {
                "workload_regime": selected.regime,
                "regime_confidence": selected.regime_confidence,
                "supervisor_policy": selected.policy.value,
            }
        return ControlTick(
            tick_id=tick_id,
            ts_ms=ts_ms,
            state=fallback_state(**state),
            zones=zones(passthrough()),
            supervisor=decision,
            runtime=ControlTickRuntime(
                tick_period_ms=STEP_MS,
                deadline_ms=500,
                duration_ms=10,
                deadline_exceeded=False,
                snapshot_schema_version=1,
                config=ControlConfigDigest(
                    fan_hardware_sha256=self.control.sources.fan_hardware.sha256,
                    safety_sha256=self.control.sources.safety.sha256,
                    policy_sha256=policy_sha256 or self.policy_sha256,
                ),
            ),
            safety_provenance=SAFETY_PROVENANCE,
        )


def row(tick: ControlTick) -> tuple[int, int, int, str]:
    return (tick.ts_ms, tick.tick_id, tick.schema_version, tick.model_dump_json())


def rl_policy_document() -> dict[str, Any]:
    """`rl_policy_config()` と同じ値の YAML（下限: 2 tick・0.9）。"""
    config, _digest = rl_policy_config()
    return config.model_dump(mode="json")


def ts(step: int) -> int:
    return BASE_TS_MS + step * STEP_MS


@pytest.fixture
def setup(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def restart_rows(setup: Fixture) -> list[tuple[int, int, int, str]]:
    """再起動を跨ぐ区間: tick_id 0..2 のあと、再起動で 0 に戻って 0..2。"""
    rows = [row(setup.tick(i, ts(i), setup.paired(i, ts(i)))) for i in range(3)]
    rows += [row(setup.tick(i, ts(10 + i), setup.paired(i, ts(10 + i)))) for i in range(3)]
    # Supervisor を配線していない tick と、shadow slot の無い tick。
    rows.append(row(setup.tick(0, ts(20), None)))
    rows.append(row(setup.tick(1, ts(21), setup.rule_only(1, ts(21)))))
    return rows


# ------------------------------------- 不変条件 1 / 2 / 3: 決定論・振り分け・再起動


def test_the_cli_builds_a_deterministic_report_without_writing_evidence(setup: Fixture) -> None:
    """**同じ入力からは同じ bytes。DB と Registry を書き換えない。**"""
    setup.store(restart_rows(setup))
    db_before = setup.db.read_bytes()
    registry_before = ModelRegistry(setup.registry_root, limits=_limits()).inspect()

    assert main(setup.argv()) == 0
    first = setup.out.read_bytes()
    assert main(setup.argv()) == 0
    assert setup.out.read_bytes() == first

    assert setup.db.read_bytes() == db_before
    assert not setup.db.with_name(setup.db.name + "-wal").exists() or (
        setup.db.with_name(setup.db.name + "-wal").stat().st_size == 0
    )
    assert ModelRegistry(setup.registry_root, limits=_limits()).inspect() == registry_before

    document = json.loads(first)
    assert document["schema_version"] == 1
    assert document["filtered"] == {
        "active_not_rule": 0,
        "no_shadow_slot": 1,
        "no_supervisor_decision": 1,
    }
    assert document["period"] == {"start_ms": ts(0), "end_ms": ts(100)}
    assert document["fan_policy_sha256"] == setup.policy_sha256
    assert "generated_at" not in document

    report = read_run_report(setup.out)
    summary = report.summary
    # 再起動を跨いだ同じ tick_id を、別の tick として6つ数える。
    assert (summary.observed_ticks, summary.paired_ticks) == (6, 6)
    assert summary.usable is True
    assert summary.rl_policy_identity == setup.rl_identity
    assert summary.rule_policy_identity == setup.rule_table.identity
    assert report.summary_sha256 == summary.digest()
    assert (summary.first_ts_ms, summary.last_ts_ms) == (ts(0), ts(12))
    assert render(report) == first


def test_filtered_counts_always_carry_every_key() -> None:
    """**欠けた鍵と 0 を区別させない。** 既定値を持たない。"""
    with pytest.raises(ValidationError):
        ShadowFilteredCounts.model_validate({"no_supervisor_decision": 0, "no_shadow_slot": 0})
    counts = ShadowFilteredCounts(no_supervisor_decision=0, no_shadow_slot=0, active_not_rule=0)
    assert set(counts.model_dump()) == {
        "no_supervisor_decision",
        "no_shadow_slot",
        "active_not_rule",
    }


def test_the_active_not_rule_rows_are_counted_not_fed(setup: Fixture) -> None:
    """active が Rule でない行は台帳へ渡さず、理由別に数える。"""
    tick_id, ts_ms = 5, ts(5)
    decision = SupervisorDecision(
        tick_id=tick_id,
        ts_ms=ts_ms,
        snapshot_schema_version=1,
        active=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RL,
            output=setup.rl_output(tick_id, ts_ms),
            policy_identity=setup.rl_identity,
            received_monotonic_ms=ts_ms,
            source_monotonic_ms=ts_ms,
        ),
        shadow=SupervisorPolicyEvaluation(
            policy=SupervisorPolicyKind.RULE, output=setup.rule_output(tick_id, ts_ms)
        ),
    )
    report = _build(setup, [row(setup.tick(tick_id, ts_ms, decision))])
    assert report.filtered.active_not_rule == 1
    assert report.summary.observed_ticks == 0
    assert report.summary.usable is False


def test_the_ledger_keys_ticks_by_timestamp_and_tick_id(setup: Fixture) -> None:
    """**`tick_id` だけで畳まない**（0074 §2.1）。同じ鍵で食い違えば従来どおり拒む。"""
    config, _digest = rl_policy_config()
    ledger = SupervisorShadowLedger(
        config.shadow, rule_table=setup.rule_table, rl_identity=setup.rl_identity
    )
    ledger.observe(setup.paired(0, ts(0)))
    ledger.observe(setup.paired(0, ts(0)))
    ledger.observe(setup.paired(0, ts(50)))
    assert ledger.summary().observed_ticks == 2

    from coldaisle.control.supervisor import SupervisorShadowConflictError

    with pytest.raises(SupervisorShadowConflictError, match="ts_ms="):
        ledger.observe(setup.paired(0, ts(50), strategy="conservative"))


# ------------------------------------- 不変条件 4: run 全体を拒否する


def test_a_trace_from_another_fan_policy_rejects_the_whole_run(setup: Fixture) -> None:
    """**設定の違う区間を混ぜない。** 落とした行を除いて続けない。何も書かない。"""
    rows = restart_rows(setup)
    rows.append(row(setup.tick(3, ts(30), setup.paired(3, ts(30)), policy_sha256="f" * 64)))
    setup.store(rows)

    assert main(setup.argv()) == 1
    assert not setup.out.exists()


def test_a_rejected_run_leaves_an_earlier_output_untouched(setup: Fixture) -> None:
    """拒否した run は既存の `--out` を置き換えない。成功した run は一時ファイルを残さない。"""
    setup.store(restart_rows(setup))
    assert main(setup.argv()) == 0
    earlier = setup.out.read_bytes()
    assert list(setup.out.parent.glob(f".{setup.out.name}*")) == []

    # 同じ DB へ別の fan-policy の行を1つ足す。これで run 全体が拒否される。
    setup.store([row(setup.tick(3, ts(30), setup.paired(3, ts(30)), policy_sha256="f" * 64))])

    assert main(setup.argv()) == 1
    assert setup.out.read_bytes() == earlier


def test_an_index_that_disagrees_with_the_body_rejects_the_run(setup: Fixture) -> None:
    """索引と本文が食い違う行は run ごと拒む（0053 §2.4）。"""
    tick = setup.tick(1, ts(1), setup.paired(1, ts(1)))
    broken = (ts(1), 2, tick.schema_version, tick.model_dump_json())
    with pytest.raises(SupervisorShadowInputError, match="索引と中身"):
        _build(setup, [broken])


def test_a_trace_without_runtime_rejects_the_run(setup: Fixture) -> None:
    """**v8 未満（runtime が無い）は設定を照合できない**ので run ごと拒む。"""
    decision = setup.paired(1, ts(1))
    selected = decision.selected_output
    assert selected is not None
    old = ControlTick(
        schema_version=7,
        tick_id=1,
        ts_ms=ts(1),
        state=fallback_state(
            workload_regime=selected.regime,
            regime_confidence=selected.regime_confidence,
            supervisor_policy=selected.policy.value,
        ),
        zones=zones(passthrough()),
        supervisor=decision,
    )
    with pytest.raises(SupervisorShadowInputError, match="v8 未満"):
        _build(setup, [row(old)])


def test_a_ledger_refusal_rejects_the_run(setup: Fixture) -> None:
    """**Rule 表と合わない行を除いて数えない。** 残りの区間だけで `usable` に届きうる。"""
    rows = [row(setup.tick(i, ts(i), setup.paired(i, ts(i)))) for i in range(4)]
    tick_id, ts_ms = 9, ts(9)
    stale_rule = setup.rule_output(
        tick_id,
        ts_ms,
        weights=setup.rule_table.entry(REGIME).weights.model_copy(update={"change": 0.0}),
    )
    decision = setup.paired(tick_id, ts_ms).model_copy(
        update={
            "active": SupervisorPolicyEvaluation(
                policy=SupervisorPolicyKind.RULE, output=stale_rule
            )
        }
    )
    rows.append(row(setup.tick(tick_id, ts_ms, decision)))
    with pytest.raises(SupervisorShadowInputError, match="台帳が trace を受け取らなかった"):
        _build(setup, rows)


def test_rows_outside_the_period_are_refused(setup: Fixture) -> None:
    """期間の外の行を黙って集計に入れない。"""
    with pytest.raises(SupervisorShadowInputError, match="period の外"):
        _build(setup, [row(setup.tick(1, ts(500), setup.paired(1, ts(500))))])


# ------------------------------------- 不変条件 5: 識別は Registry から


def test_an_unregistered_artifact_is_refused(setup: Fixture) -> None:
    """manifest が名指した版が Registry に無ければ、集計を作らない。"""
    setup.store(restart_rows(setup))
    setup.write_evidence(version="0.2.0")
    assert main(setup.argv()) == 1
    assert not setup.out.exists()


def test_the_manifest_cannot_carry_an_artifact_hash(setup: Fixture) -> None:
    """**hash を文字列で受け取らない。** manifest は model ID と版だけ。"""
    setup.evidence.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "period": {"start_ms": ts(0), "end_ms": ts(100)},
                "rl_artifact": {
                    "model_id": "rl-supervisor-test",
                    "version": "0.1.0",
                    "artifact_sha256": "e" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        main(setup.argv())


# ------------------------------------- 不変条件 6: 包みの digest


def test_the_report_recomputes_the_summary_digest(setup: Fixture) -> None:
    """**包みの digest を呼び出し側から受け取らない。** 食い違えば読めない。"""
    report = _build(setup, restart_rows(setup))
    document = json.loads(render(report))
    document["summary_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="summary_sha256"):
        SupervisorShadowRunReport.model_validate_json(json.dumps(document), strict=False)

    tampered = json.loads(render(report))
    tampered["summary"]["paired_ticks"] = 5
    tampered["summary"]["rl_unavailable_ticks"] = 1
    tampered["summary"]["rl_errors"] = {"supervisor_expired": 1}
    with pytest.raises(ValidationError):
        SupervisorShadowRunReport.model_validate_json(json.dumps(tampered), strict=False)

    missing = json.loads(render(report))
    del missing["filtered"]["active_not_rule"]
    with pytest.raises(ValidationError):
        SupervisorShadowRunReport.model_validate_json(json.dumps(missing), strict=False)


# ------------------------------------- 不変条件 7: 制御へ逆流しない


FORBIDDEN_MODULES = (
    "coldaisle.control.hardware",
    "coldaisle.control.safety",
    "coldaisle.control.reactive",
    "coldaisle.event_entry",
    "serial",
    "subprocess",
)

REGISTRY_WRITE_OPERATIONS = {
    "register_candidate",
    "mark_validated",
    "promote",
    "rollback",
    "retire",
    "promote_supervisor_policy",
}


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
    """CLI は Hardware / Safety / Guard / serial / subprocess / 書き込み入口を import しない。"""
    offenders = [
        name
        for name in _imported_modules(SRC / "supervisor_shadow.py")
        if any(name == item or name.startswith(f"{item}.") for item in FORBIDDEN_MODULES)
    ]
    assert offenders == []


def test_the_control_loop_never_imports_the_ledger_or_the_cli() -> None:
    """**制御プロセスは台帳を持たない**（0074 §2.1。in-loop 案は却下）。"""
    for path in (SRC / "control_daemon.py", SRC / "control" / "loop.py"):
        imported = _imported_modules(path)
        assert not any(
            name.startswith("coldaisle.control.supervisor.shadow")
            or name.startswith("coldaisle.supervisor_shadow")
            or name.endswith(".SupervisorShadowLedger")
            for name in imported
        ), path.name


def test_the_cli_never_calls_a_registry_write_operation() -> None:
    """Registry は読むだけ。**書く操作の名前がこの module に現れない。**"""
    tree = ast.parse((SRC / "supervisor_shadow.py").read_text(encoding="utf-8"))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert not (names & REGISTRY_WRITE_OPERATIONS)


# ---------------------------------------------------------------- 小道具


def _limits() -> Any:
    from coldaisle.control.model_registry import load_model_registry_limits

    return load_model_registry_limits(ROOT / "config")


def _build(setup: Fixture, rows: list[tuple[int, int, int, str]]) -> SupervisorShadowRunReport:
    from coldaisle.store.models import ControlTraceRecord

    config, _digest = rl_policy_config()
    records = sorted(
        (
            ControlTraceRecord(
                ts_ms=ts_ms, tick_id=tick_id, schema_version=version, trace_json=body
            )
            for ts_ms, tick_id, version, body in rows
        ),
        key=lambda item: (item.ts_ms, item.tick_id),
    )
    return build_run_report(
        records,
        control=setup.control,
        shadow_config=config.shadow,
        rl_identity=setup.rl_identity,
        period=ShadowPeriod(start_ms=ts(0), end_ms=ts(100)),
    )
