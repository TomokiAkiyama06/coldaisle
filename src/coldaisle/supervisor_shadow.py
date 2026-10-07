"""合成の起点: 運転後に Supervisor の Shadow 台帳を組み立てる（#89 / 決定記録 0074 §2.1）。

保存済み decision trace（`control_traces`）の `ControlTick.supervisor` だけを読み、
Rule（active）と RL（shadow）の提案を `SupervisorShadowLedger` で突き合わせ、
`SupervisorShadowRunReport` の canonical JSON を1つ書き出す。

```bash
uv run coldaisle-supervisor-shadow --evidence config/supervisor-shadow-runs.yaml \\
  --registry-root var/model-registry --out var/supervisor-shadow.json
```

**制御へ逆流する経路を持たない**（0074 §2.1「制御へ逆流しないことの保証」）。

- 制御プロセスとは別の、人が起動する1回実行の CLI である。台帳の例外・上限・遅さは
  control tick にも heartbeat にも届かない
- 証拠の DB は `coldaisle-evaluate` の `EvidenceDatabase`（`immutable=1`）で開き、書かない
- Registry は `load_version()` で**読むだけ**。登録・検証・昇格などの書く操作は呼ばない
- 出力を読む制御側のコードは無い。人が `summary` を取り出して `promote_supervisor_policy()`
  へ渡す（昇格は CLI の外）
- LLM のツールにしない（AGENTS.md ルール1）

**同じ入力からは同じ bytes を出す。** 生成時刻を持たず、時刻は decision から取る（0054 §2.7）。
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldaisle import logs
from coldaisle.clock import SimulatedClock
from coldaisle.control.config import ControlConfig, SupervisorOutputBounds
from coldaisle.control.model.thermal import canonical_json_bytes
from coldaisle.control.model_registry import (
    MODEL_REGISTRY_CONFIG_FILENAME,
    ArtifactKind,
    ArtifactRef,
    ModelCompatibility,
    ModelRegistry,
    load_model_registry_limits,
)
from coldaisle.control.schema import (
    AuthorityStage,
    ControlTick,
    SupervisorPolicyIdentity,
    SupervisorPolicyKind,
)
from coldaisle.control.supervisor import (
    POLICY_ACTION_SCHEMA_VERSION,
    POLICY_STATE_SCHEMA_VERSION,
    PolicyShadowConfig,
    RlPolicyConfig,
    RulePolicy,
    SupervisorPolicyBinding,
    SupervisorPolicyUnusableError,
    SupervisorShadowConflictError,
    SupervisorShadowLedger,
    SupervisorShadowSummary,
    SupervisorShadowUsageError,
    rule_policy_table,
)
from coldaisle.evaluate import (
    EvidenceDatabase,
    EvidenceOutputError,
    control_config_files,
    refuse_output_on_inputs,
)
from coldaisle.store.models import ControlTraceRecord

LOGGER = logging.getLogger("coldaisle.supervisor_shadow")

SUPERVISOR_SHADOW_RUN_REPORT_SCHEMA_VERSION: Literal[1] = 1
"""`SupervisorShadowRunReport` の形の版。"""

SUPERVISOR_SHADOW_MANIFEST_VERSION: Literal[1] = 1
"""`--evidence` の manifest の形の版。"""

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SupervisorShadowInputError(ValueError):
    """run 全体を拒否した（索引の食い違い・古い trace・設定の不一致・台帳の拒否など）。

    **落とした行を除いて続けない**（0074 §2.1）。除いて数えると、残りの区間だけで
    `usable` に届きうる。期間を分けて渡し直す。
    """


class ShadowPeriod(_Frozen):
    """集計した期間 `[start_ms, end_ms)`。**「直近」のような相対指定を置かない。**"""

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("period は start_ms < end_ms にする")
        return self


class ShadowArtifactName(_Frozen):
    """manifest が名指す RL artifact。**model ID と版だけ**で、hash は受け取らない。

    hash を文字列で受け取ると、同じ版を名乗る別 artifact の集計を作れてしまう
    （0057 §2.3 と同じ理由）。識別は Registry の attestation から作る。
    """

    model_id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=80)

    def ref(self) -> ArtifactRef:
        """#104 の `ArtifactRef`（kind は supervisor policy に固定）。"""
        return ArtifactRef(
            kind=ArtifactKind.SUPERVISOR_POLICY, model_id=self.model_id, version=self.version
        )


class SupervisorShadowManifest(_Frozen):
    """1回の集計の入力（`--evidence`）。**期間は1つ、RL artifact も1つ。**"""

    schema_version: Literal[1]
    period: ShadowPeriod
    rl_artifact: ShadowArtifactName

    @classmethod
    def from_file(cls, path: Path) -> SupervisorShadowManifest:
        """YAML を読み、検証済みの manifest を返す。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"supervisor shadow の manifest が辞書ではない: {path.name}")
        return cls.model_validate(loaded)


class ShadowFilteredCounts(_Frozen):
    """台帳へ渡さなかった trace の行の数（理由別。監査用）。

    **3つの鍵を常に全部出す。** 既定値を持たせず、0 でも省かない。欠けた鍵と 0 を
    読む側に区別させないため（0074 §2.1）。`usable` の判定には入らない。
    """

    no_supervisor_decision: int = Field(ge=0)
    """`supervisor` が無い行（Supervisor を配線していない区間）。"""
    no_shadow_slot: int = Field(ge=0)
    """`supervisor.shadow` が無い行（構成上そもそも比較していない区間）。"""
    active_not_rule: int = Field(ge=0)
    """active が Rule でない行。"""


class SupervisorShadowRunReport(_Frozen):
    """保存済み trace から作った Shadow 集計と、その振り分けの件数（schema v1）。

    **集計（`summary`）は変えずに包む。** 集計の schema を上げると、昇格の入口と Registry の
    `shadow_evaluation_ref` が名指す digest の意味が変わる。昇格に使うのは `summary` だけで、
    **包みの digest を昇格の証拠にしない。**
    """

    schema_version: Literal[1] = SUPERVISOR_SHADOW_RUN_REPORT_SCHEMA_VERSION
    summary: SupervisorShadowSummary
    summary_sha256: str = Field(pattern=_SHA256_PATTERN)
    """`summary` の canonical digest。**validator が作り直して一致を確かめる。**"""
    filtered: ShadowFilteredCounts
    period: ShadowPeriod
    fan_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    """照合に使った `fan-policy.yaml` の SHA-256（trace の `runtime.config` と一致した値）。"""

    @model_validator(mode="after")
    def _summary_is_bound(self) -> Self:
        if self.summary.schema_version != 2:
            # v1 の集計は Rule の版しか持たず、昇格の証拠にならない。包みにも入れない。
            raise ValueError("SupervisorShadowRunReport の summary は schema version 2 にする")
        if self.summary_sha256 != self.summary.digest():
            raise ValueError("summary_sha256 が summary の digest と一致しない")
        first, last = self.summary.first_ts_ms, self.summary.last_ts_ms
        if (
            first is not None
            and last is not None
            and (first < self.period.start_ms or last >= self.period.end_ms)
        ):
            raise ValueError("summary の観測区間が period の外にある")
        return self

    @classmethod
    def build(
        cls,
        summary: SupervisorShadowSummary,
        *,
        filtered: ShadowFilteredCounts,
        period: ShadowPeriod,
        fan_policy_sha256: str,
    ) -> SupervisorShadowRunReport:
        """集計から包みを作る。digest はここで導く（呼び出し側から受け取らない）。"""
        return cls(
            summary=summary,
            summary_sha256=summary.digest(),
            filtered=filtered,
            period=period,
            fan_policy_sha256=fan_policy_sha256,
        )


def render(report: SupervisorShadowRunReport) -> bytes:
    """包みの canonical JSON bytes。**同じ入力からは同じ bytes になる。**"""
    return canonical_json_bytes(report)


def read_run_report(path: Path) -> SupervisorShadowRunReport:
    """書き出した包みを読み戻す。`summary_sha256` の照合は validator が行う。

    昇格では、読み戻した `.summary` を `promote_supervisor_policy()` へ渡す。
    """
    return SupervisorShadowRunReport.model_validate_json(path.read_bytes(), strict=False)


def resolve_rl_identity(
    registry: ModelRegistry,
    name: ShadowArtifactName,
    *,
    bounds: SupervisorOutputBounds,
) -> SupervisorPolicyIdentity:
    """Registry を**読んで**、名指した RL artifact の完全な識別を作る。

    `load_version()` は Registry を作らず書かない。検証済み bytes を shadow 用の束縛
    （`SupervisorPolicyBinding.for_shadow`）に通し、kind・能力・metadata・action 空間を
    運転時と同じ規則で照合してから識別を取り出す。
    """
    compatibility = ModelCompatibility(
        feature_schema_version=POLICY_STATE_SCHEMA_VERSION,
        target_schema_version=POLICY_ACTION_SCHEMA_VERSION,
        authority_stage=AuthorityStage.SHADOW,
    )
    result = registry.load_version(name.ref(), compatibility)
    if result.artifact is None:
        raise SupervisorShadowInputError(
            f"RL artifact を Registry から読めない（status={result.status.value}; {result.detail}）"
        )
    try:
        binding = SupervisorPolicyBinding.for_shadow(
            result.artifact, expected_policy_version=name.version, bounds=bounds
        )
    except SupervisorPolicyUnusableError as error:
        raise SupervisorShadowInputError(
            f"RL artifact を shadow に束縛できない: {error}"
        ) from error
    return binding.identity


def build_run_report(
    traces: Iterable[ControlTraceRecord],
    *,
    control: ControlConfig,
    shadow_config: PolicyShadowConfig,
    rl_identity: SupervisorPolicyIdentity,
    period: ShadowPeriod,
) -> SupervisorShadowRunReport:
    """trace の行を振り分け、台帳へ渡して包みを作る（0074 §2.1「台帳に渡す tick」）。

    次のどれかがあれば **run 全体を拒否する**（`SupervisorShadowInputError`）。

    - 索引（`ts_ms` / `tick_id` / `schema_version`）と本文が食い違う・本文を読めない
    - `runtime` が無い（v8 未満）
    - `runtime.config` の `fan-policy.yaml` hash が渡した設定の hash と違う
    - 台帳が `SupervisorShadowUsageError` / `SupervisorShadowConflictError` を投げた
    - period の外の行が混ざっている
    """
    fan_policy_sha256 = control.sources.policy.sha256
    # Rule 表は検証済み fan-policy.yaml から作る。`RulePolicy` は時計を要求するが、
    # 表を作る問い合わせの時刻は集計に現れない。**壁時計を読まない**よう固定の時計を渡す。
    rule_table = rule_policy_table(
        RulePolicy(control.policy.supervisor.rule_policy, SimulatedClock(0))
    )
    ledger = SupervisorShadowLedger(shadow_config, rule_table=rule_table, rl_identity=rl_identity)
    no_supervisor = 0
    no_shadow = 0
    active_not_rule = 0
    for row in traces:
        if not period.start_ms <= row.ts_ms < period.end_ms:
            raise SupervisorShadowInputError(
                f"period の外の trace が渡された（ts_ms={row.ts_ms}; tick_id={row.tick_id}）"
            )
        try:
            tick = ControlTick.model_validate_json(row.trace_json)
        except ValidationError as error:
            raise SupervisorShadowInputError(
                f"decision trace の本文を読めない（ts_ms={row.ts_ms}; tick_id={row.tick_id}）"
            ) from error
        if (tick.ts_ms, tick.tick_id, tick.schema_version) != (
            row.ts_ms,
            row.tick_id,
            row.schema_version,
        ):
            # 0053 §2.4 の trace 検証と同じ。索引の壊れた行を集計へ流さない。
            raise SupervisorShadowInputError(
                "decision trace の索引と中身が一致しない"
                f"（ts_ms={row.ts_ms}; tick_id={row.tick_id}）"
            )
        if tick.runtime is None:
            # v8 未満は設定の hash を持たず、どの Rule 表・rl_version で回したかを照合できない。
            raise SupervisorShadowInputError(
                "runtime の無い（v8 未満の）decision trace は集計できない"
                f"（ts_ms={row.ts_ms}; tick_id={row.tick_id}; version={tick.schema_version}）"
            )
        if tick.runtime.config.policy_sha256 != fan_policy_sha256:
            # 別の Rule 表・別の rl_version で回した区間を同じ集計に混ぜない。期間を分けて渡す。
            raise SupervisorShadowInputError(
                "decision trace の fan-policy.yaml hash が渡した設定と違う"
                f"（ts_ms={row.ts_ms}; tick_id={row.tick_id}）。期間を分けて渡す"
            )
        decision = tick.supervisor
        if decision is None:
            no_supervisor += 1
            continue
        if decision.shadow is None:
            no_shadow += 1
            continue
        if decision.active.policy is not SupervisorPolicyKind.RULE:
            active_not_rule += 1
            continue
        try:
            ledger.observe(decision)
        except (SupervisorShadowUsageError, SupervisorShadowConflictError) as error:
            # Rule 表との不一致は「別の表で回した区間が混ざった」ことを意味する。除いて続けない。
            raise SupervisorShadowInputError(
                f"shadow 台帳が trace を受け取らなかった"
                f"（ts_ms={row.ts_ms}; tick_id={row.tick_id}）: {error}"
            ) from error
    return SupervisorShadowRunReport.build(
        ledger.summary(),
        filtered=ShadowFilteredCounts(
            no_supervisor_decision=no_supervisor,
            no_shadow_slot=no_shadow,
            active_not_rule=active_not_rule,
        ),
        period=period,
        fan_policy_sha256=fan_policy_sha256,
    )


def write(report: SupervisorShadowRunReport, path: Path) -> Path:
    """包みを書き出す。**書くのはこの path だけ**（DB・Registry・設定・trace には書かない）。

    同じディレクトリの一時ファイルへ書いてから置き換える。途中で落ちても `path` に
    書きかけの包みが残らず、`path` が変わるのは書き出しが最後まで済んだときだけになる。
    一時ファイルは呼び出しごとに排他的に作る。同じ `--out` へ2つの run が同時に書いても、
    互いの一時ファイルを上書きして**他の run の包みを自分の成功として置く**ことがない。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(render(report))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def build_parser() -> argparse.ArgumentParser:
    """CLI の引数。**下限を上書きする口は作らない**（`rl-policy.yaml` が唯一の情報源）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-supervisor-shadow",
        description="保存済み decision trace から Supervisor の Shadow 集計を作る（#89）",
    )
    parser.add_argument(
        "--evidence", type=Path, required=True, help="期間と RL artifact を名指す manifest"
    )
    parser.add_argument(
        "--registry-root", type=Path, required=True, help="Model Registry の root（読むだけ）"
    )
    parser.add_argument(
        "--registry-limits",
        type=Path,
        default=Path("config"),
        help="model-registry.yaml のあるディレクトリ",
    )
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument(
        "--control-config",
        type=Path,
        default=Path("config"),
        help=(
            "fan-hardware.yaml / safety.yaml / fan-policy.yaml / air-balance.yaml"
            " のあるディレクトリ（4ファイルとも必須）"
        ),
    )
    parser.add_argument(
        "--rl-policy",
        type=Path,
        default=Path("config/rl-policy.yaml"),
        help="shadow の下限を取る検証済みの rl-policy.yaml",
    )
    parser.add_argument("--out", type=Path, default=Path("var/supervisor-shadow.json"))
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI の入口。拒否した run では何も書かずに 1 を返す。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)

    try:
        # **どの入力を開くより前に**確かめる（#227）。`write()` は `os.replace()` で
        # 置き換えるので、`--out` が DB を指すと証拠を報告で上書きする。
        refuse_output_on_inputs(
            args.out,
            db=args.db,
            inputs=(
                args.evidence,
                args.registry_limits / MODEL_REGISTRY_CONFIG_FILENAME,
                *control_config_files(args.control_config),
                args.rl_policy,
            ),
            input_roots=(args.registry_root,),
        )
    except EvidenceOutputError as error:
        LOGGER.error(
            "Supervisor の shadow 集計を拒否した（何も書かない）",
            extra={logs.FIELDS_KEY: {"reason": str(error), "out": str(args.out)}},
        )
        return 1
    manifest = SupervisorShadowManifest.from_file(args.evidence)
    control = ControlConfig.from_directory(args.control_config)
    rl_policy, rl_policy_sha256 = RlPolicyConfig.from_file(args.rl_policy)
    registry = ModelRegistry(
        args.registry_root, limits=load_model_registry_limits(args.registry_limits)
    )
    period = manifest.period
    try:
        rl_identity = resolve_rl_identity(
            registry, manifest.rl_artifact, bounds=control.policy.supervisor.output_bounds
        )
        # **証拠の DB は読み取り専用で開く**（`immutable=1`。添え file に中身があれば開かない）。
        with EvidenceDatabase(args.db) as store, store.snapshot():
            traces = store.control_traces(period.start_ms, period.end_ms)
        report = build_run_report(
            traces,
            control=control,
            shadow_config=rl_policy.shadow,
            rl_identity=rl_identity,
            period=period,
        )
    except SupervisorShadowInputError as error:
        LOGGER.error(
            "Supervisor の shadow 集計を拒否した（何も書かない）",
            extra={
                logs.FIELDS_KEY: {
                    "reason": str(error),
                    # 既存の --out は消さない。残っていても**今回の run の結果ではない**。
                    "out": str(args.out),
                    "stale_out_exists": args.out.exists(),
                    "start_ms": period.start_ms,
                    "end_ms": period.end_ms,
                }
            },
        )
        return 1
    path = write(report, args.out)
    summary = report.summary
    LOGGER.info(
        "Supervisor の shadow 集計を書き出した",
        extra={
            logs.FIELDS_KEY: {
                "path": str(path),
                "summary_sha256": report.summary_sha256,
                "usable": summary.usable,
                "observed_ticks": summary.observed_ticks,
                "paired_ticks": summary.paired_ticks,
                "filtered": report.filtered.model_dump(mode="json"),
                "rl_policy_sha256": rl_policy_sha256,
                "fan_policy_sha256": report.fan_policy_sha256,
            }
        },
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
