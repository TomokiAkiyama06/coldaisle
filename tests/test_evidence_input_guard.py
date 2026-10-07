"""証拠を読むだけの CLI が、入力を**壊さない・読み違えない**こと。実機不要。

`coldaisle-air-balance-shadow`（#81 / PR #223）で見つかった2つの穴が、`EvidenceDatabase`
を使う他の CLI（`coldaisle-evaluate` / `coldaisle-drift` / `coldaisle-supervisor-shadow`）
にも残っていた。ここでは4つの CLI を同じ試験で破ろうとする。

1. `--out` が証拠の DB・その添え file・manifest・設定を指したら、**どの入力も読む前に**
   exit 1 で拒み、何も書かない（DB を報告で上書きしない）
2. `--db` が symlink でも、**実体の隣**の未 checkpoint の WAL を見落とさない
   （`immutable=1` は WAL を黙って無視して古い断面を読む）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from coldaisle.clock import SimulatedClock
from coldaisle.evaluate import EvidenceDatabase, EvidenceDatabaseError
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.models import Reading, Sample
from test_control_config import valid_documents, write_documents

ROOT = Path(__file__).resolve().parents[1]
QUALITY_YAML = ROOT / "config" / "quality.yaml"


@dataclass
class Case:
    """1つの CLI を、成功する引数一式とともに用意したもの。"""

    main: Callable[[list[str]], int]
    argv: list[str]
    db: Path
    out: Path
    inputs: dict[str, Path]
    """`--out` で上書きさせない入力（名前 → path）。"""

    def with_arg(self, flag: str, value: Path) -> list[str]:
        argv = list(self.argv)
        argv[argv.index(flag) + 1] = str(value)
        return argv


def _control_config(tmp_path: Path) -> Path:
    directory = tmp_path / "guard-config"
    directory.mkdir()
    write_documents(directory, valid_documents())
    return directory


def evaluate_case(tmp_path: Path) -> Case:
    from coldaisle.evaluate import main
    from test_offline_evaluation import (
        EVALUATION_YAML,
        METRICS_YAML,
        STEP_MS,
        TICK_TS_MS,
        plan_following_run,
    )

    directory = _control_config(tmp_path)
    db = tmp_path / "guard.db"
    traces, observations = plan_following_run(ticks=6)
    with SqliteStore(
        db,
        rules=QualityRules.from_yaml(QUALITY_YAML),
        clock=SimulatedClock(TICK_TS_MS + 100 * STEP_MS),
    ) as store:
        for trace in traces:
            store.record_control_trace(
                ts_ms=trace.ts_ms,
                tick_id=trace.tick_id,
                schema_version=trace.schema_version,
                trace_json=trace.trace_json,
            )
        by_time: dict[int, list[Reading]] = {}
        for item in observations:
            by_time.setdefault(item.ts_ms, []).append(
                Reading(metric=item.metric, value=item.value, quality=item.quality)
            )
        store.insert_samples(
            Sample(ts_ms=ts_ms, readings=tuple(readings))
            for ts_ms, readings in sorted(by_time.items())
        )
    manifest = tmp_path / "guard-runs.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "runs": [
                    {
                        "run_id": "guard",
                        "start_ms": TICK_TS_MS,
                        "end_ms": TICK_TS_MS + 20 * STEP_MS,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "guard-out" / "evaluation.json"
    argv = [
        "--runs",
        str(manifest),
        "--db",
        str(db),
        "--config",
        str(EVALUATION_YAML),
        "--control-config",
        str(directory),
        "--metrics",
        str(METRICS_YAML),
        "--out",
        str(out),
    ]
    return Case(
        main=main,
        argv=argv,
        db=db,
        out=out,
        inputs={"manifest": manifest, "policy": directory / "fan-policy.yaml"},
    )


def drift_case(tmp_path: Path) -> Case:
    from coldaisle.drift import main
    from test_model_confidence import (
        dataset,
        fit_confidence_profile,
        profile_spec,
        split,
        train,
    )
    from test_model_drift import BASE_TS_MS, HORIZONS, STEP_MS, TARGETS

    data = dataset(HORIZONS, TARGETS)
    parts = split(data)
    profile = fit_confidence_profile(train(data, parts), data, parts, profile_spec())
    profile_path = tmp_path / "guard-profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")

    directory = _control_config(tmp_path)
    db = tmp_path / "guard.db"
    # 空の証拠でも報告は出る（`insufficient_evidence`）。壊れるかどうかを見るには十分。
    with SqliteStore(
        db, rules=QualityRules.from_yaml(QUALITY_YAML), clock=SimulatedClock(BASE_TS_MS)
    ):
        pass
    evidence = tmp_path / "guard-evidence.yaml"
    evidence.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "start_ms": BASE_TS_MS, "end_ms": BASE_TS_MS + 10 * STEP_MS}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "guard-out" / "drift.json"
    argv = [
        "--evidence",
        str(evidence),
        "--profile",
        str(profile_path),
        "--db",
        str(db),
        "--config",
        str(ROOT / "config" / "drift.yaml"),
        "--control-config",
        str(directory),
        "--out",
        str(out),
    ]
    return Case(
        main=main,
        argv=argv,
        db=db,
        out=out,
        inputs={"manifest": evidence, "policy": directory / "fan-policy.yaml"},
    )


def supervisor_shadow_case(tmp_path: Path) -> Case:
    from coldaisle.supervisor_shadow import main
    from test_supervisor_shadow_cli import Fixture, restart_rows

    setup = Fixture(tmp_path)
    setup.store(restart_rows(setup))
    return Case(
        main=main,
        argv=setup.argv(),
        db=setup.db,
        out=setup.out,
        inputs={"manifest": setup.evidence, "policy": setup.config_dir / "fan-policy.yaml"},
    )


def air_balance_shadow_case(tmp_path: Path) -> Case:
    from coldaisle.air_balance_shadow import main
    from test_air_balance_shadow_cli import Fixture, mixed_ticks

    setup = Fixture(tmp_path)
    setup.store(mixed_ticks(setup))
    return Case(
        main=main,
        argv=setup.argv(),
        db=setup.db,
        out=setup.out,
        inputs={"manifest": setup.evidence, "policy": setup.config_dir / "fan-policy.yaml"},
    )


CASES: dict[str, Callable[[Path], Case]] = {
    "evaluate": evaluate_case,
    "drift": drift_case,
    "supervisor-shadow": supervisor_shadow_case,
    "air-balance-shadow": air_balance_shadow_case,
}


@pytest.fixture(params=sorted(CASES))
def case(request: pytest.FixtureRequest, tmp_path: Path) -> Case:
    return CASES[request.param](tmp_path)


def _symlinked_db(case: Case) -> Path:
    link_dir = case.db.parent / "guard-link-dir"
    link_dir.mkdir()
    link = link_dir / "linked.db"
    link.symlink_to(case.db)
    return link


def test_the_case_runs_cleanly(case: Case) -> None:
    """下ごしらえが正しいこと（以降の拒否が別の理由で起きていないこと）。"""
    assert case.main(case.argv) == 0
    assert case.out.is_file()


def test_an_out_on_the_db_is_refused_and_the_db_is_untouched(case: Case) -> None:
    """**`--out` が DB を指しても、証拠を報告で上書きしない。**"""
    before = case.db.read_bytes()

    assert case.main(case.with_arg("--out", case.db)) == 1
    assert case.db.read_bytes() == before


def test_an_out_through_a_symlink_to_the_db_is_refused(case: Case) -> None:
    before = case.db.read_bytes()
    link = case.db.with_name("guard-link.json")
    link.symlink_to(case.db)

    assert case.main(case.with_arg("--out", link)) == 1
    assert case.db.read_bytes() == before


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_an_out_on_a_sidecar_of_the_db_is_refused(case: Case, suffix: str) -> None:
    sidecar = case.db.with_name(case.db.name + suffix)

    assert case.main(case.with_arg("--out", sidecar)) == 1
    assert not sidecar.exists()


def test_an_out_on_the_sidecar_beside_a_symlinked_db_target_is_refused(case: Case) -> None:
    """SQLite が使うのは**実体の隣**の添え file。symlink の隣だけを見ても守れない。"""
    link = _symlinked_db(case)
    wal = case.db.with_name(case.db.name + "-wal")
    argv = case.with_arg("--db", link)
    argv[argv.index("--out") + 1] = str(wal)

    assert case.main(argv) == 1
    assert not wal.exists()


@pytest.mark.parametrize("name", ["manifest", "policy"])
def test_an_out_on_an_input_is_refused_before_it_is_read(case: Case, name: str) -> None:
    """壊れた入力を `--out` が指していても、読んで落ちる前に exit 1 で拒む。"""
    alias = case.inputs[name]
    alias.write_text("{not yaml", encoding="utf-8")

    assert case.main(case.with_arg("--out", alias)) == 1
    assert alias.read_text(encoding="utf-8") == "{not yaml"


def test_a_symlinked_db_with_a_live_wal_beside_its_target_is_refused(case: Case) -> None:
    """symlink の `--db` でも、実体の隣に中身のある WAL があれば**古い断面を読まない**。"""
    link = _symlinked_db(case)
    case.db.with_name(case.db.name + "-wal").write_bytes(b"uncheckpointed")

    with pytest.raises(EvidenceDatabaseError, match="静止していない"):
        case.main(case.with_arg("--db", link))
    assert not case.out.exists()


# ------------------------------------------------ 共有の入口そのもの


def test_evidence_database_checks_the_sidecars_beside_the_target(tmp_path: Path) -> None:
    """**呼び出し側が resolve しなくても**、実体の隣の WAL で開かない。"""
    db = tmp_path / "guard.db"
    with SqliteStore(db, rules=QualityRules.from_yaml(QUALITY_YAML), clock=SimulatedClock(0)):
        pass
    link_dir = tmp_path / "guard-link-dir"
    link_dir.mkdir()
    link = link_dir / "linked.db"
    link.symlink_to(db)

    with EvidenceDatabase(link):
        pass
    db.with_name(db.name + "-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(EvidenceDatabaseError, match="静止していない"), EvidenceDatabase(link):
        pass


def test_supervisor_shadow_refuses_an_out_inside_the_registry(tmp_path: Path) -> None:
    """Model Registry は読むだけの入力。`--out` で artifact を上書きさせない。"""
    from coldaisle.supervisor_shadow import main
    from test_supervisor_shadow_cli import Fixture, restart_rows

    setup = Fixture(tmp_path)
    setup.store(restart_rows(setup))
    victim = next(path for path in sorted(setup.registry_root.rglob("*")) if path.is_file())
    before = victim.read_bytes()
    argv = setup.argv()
    argv[argv.index("--out") + 1] = str(victim)

    assert main(argv) == 1
    assert victim.read_bytes() == before


def test_evaluate_refuses_an_out_on_the_export_the_manifest_names(tmp_path: Path) -> None:
    """manifest が名指す #90 の export も入力。読む前に拒む。"""
    case = evaluate_case(tmp_path)
    manifest = case.inputs["manifest"]
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["runs"][0]["shadow_jsonl"] = "guard-shadow.jsonl"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    export = manifest.parent / "guard-shadow.jsonl"
    export.write_text("{not json\n", encoding="utf-8")

    assert case.main(case.with_arg("--out", export)) == 1
    assert export.read_text(encoding="utf-8") == "{not json\n"


def test_drift_refuses_an_out_inside_the_dataset_the_manifest_names(tmp_path: Path) -> None:
    """manifest が名指す Dataset artifact は丸ごと入力。その中へは書かない。"""
    case = drift_case(tmp_path)
    manifest = case.inputs["manifest"]
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["dataset"] = "guard-dataset"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    victim = manifest.parent / "guard-dataset" / "manifest.json"
    victim.parent.mkdir()
    victim.write_text("{not json", encoding="utf-8")

    assert case.main(case.with_arg("--out", victim)) == 1
    assert victim.read_text(encoding="utf-8") == "{not json"
