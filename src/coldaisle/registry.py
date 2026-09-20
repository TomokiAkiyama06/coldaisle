"""合成の起点: Control Model Registry の運用入口（#104 / 決定記録 0062）。

`coldaisle.control.model_registry.ModelRegistry` の lifecycle 操作を、人の手から1コマンドで
実行できるようにする。**読み取りが既定**で、registry を書き換える操作は必ず
`--expected-revision` を要求する。

```bash
uv run coldaisle-registry status   --root var/model-registry
uv run coldaisle-registry verify   --root var/model-registry --contract config/model-runtime.yaml
uv run coldaisle-registry audit    --root var/model-registry --pointer-changes
uv run coldaisle-registry register --root var/model-registry \\
    --metadata var/candidate.json --payload var/candidate.model.json \\
    --actor trainer --reason "training completed"
uv run coldaisle-registry validate --root var/model-registry \\
    --artifact thermal_model/rack-thermal/1.2.0 \\
    --offline-evaluation-ref evaluation/offline/1.2.0 \\
    --actor evaluator --reason "offline gates passed" --expected-revision 1
uv run coldaisle-registry promote  --root var/model-registry --approval var/approval.json \\
    --shadow-evaluation-ref evaluation/shadow/1.2.0 \\
    --feature-schema thermal-features-v1 --target-schema thermal-targets-v1 \\
    --authority-stage shadow --expected-revision 2
uv run coldaisle-registry rollback --root var/model-registry --kind thermal_model \\
    --approval var/rollback-approval.json \\
    --feature-schema thermal-features-v1 --target-schema thermal-targets-v1 \\
    --authority-stage shadow --expected-revision 3
```

**承認を合成しない。** `promote` / `rollback` の `HumanApproval` は人が書いたファイルから
そのまま渡す。承認者・理由・対象 artifact の checksum・対象 revision を CLI の引数から
組み立てる経路は作らない（決定記録 0062 §2.2）。

**artifact を deserialize も実行もしない。** ここから Fan Demand・PWM・Authority Stage へ
届く経路は無い（AGENTS.md ルール1・2、決定記録 0062 §2.3）。
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coldaisle import logs
from coldaisle.control.model_registry import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    ArtifactVerificationError,
    HumanApproval,
    ModelCompatibility,
    ModelRegistry,
    ModelRegistryError,
    ModelRegistryLimits,
    RegistryHealth,
    RegistryHealthReport,
    load_model_registry_limits,
)
from coldaisle.control.schema import AuthorityStage

LOGGER = logging.getLogger("coldaisle.registry")

RUNTIME_CONTRACT_VERSION: Literal[1] = 1

EXIT_OK = 0
EXIT_FAILED = 1
"""操作が実行できなかった（不正な遷移・revision の競合・検証失敗など）。"""
EXIT_DEGRADED = 3
"""`verify` 専用: production は使えるが、known-good な rollback 先が無い。"""
EXIT_FALLBACK = 4
"""`verify` 専用: その registry では production を使えない。制御は #79 Fallback で動く。"""

_VERIFY_EXIT = {
    RegistryHealth.OK: EXIT_OK,
    RegistryHealth.DEGRADED: EXIT_DEGRADED,
    RegistryHealth.UNUSABLE: EXIT_FALLBACK,
    RegistryHealth.NO_PRODUCTION: EXIT_FALLBACK,
    RegistryHealth.INVALID: EXIT_FALLBACK,
}


class _Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class KindContract(_Manifest):
    """1 kind の runtime contract。**artifact の申告ではなく、動かす側の契約。**"""

    feature_schema_version: str = Field(min_length=1, max_length=120)
    target_schema_version: str = Field(min_length=1, max_length=120)
    authority_stage: AuthorityStage
    """いま**実際に運転する** stage（決定記録 0057 §2.2）。設定上の上限ではない。"""

    def as_compatibility(self) -> ModelCompatibility:
        """Return the registry-side contract this entry describes."""
        return ModelCompatibility(
            feature_schema_version=self.feature_schema_version,
            target_schema_version=self.target_schema_version,
            authority_stage=self.authority_stage,
        )


def _normalized_stage(entry: object) -> object:
    """Turn the YAML stage string into the enum the strict manifest requires."""
    if isinstance(entry, dict) and isinstance(entry.get("authority_stage"), str):
        return {**entry, "authority_stage": AuthorityStage(entry["authority_stage"])}
    return entry


class RuntimeContracts(_Manifest):
    """`verify` が kind ごとに使う runtime contract の一覧。"""

    schema_version: Literal[1]
    contracts: dict[ArtifactKind, KindContract]

    @classmethod
    def from_bytes(cls, payload: bytes) -> RuntimeContracts:
        """Validate the runtime contract manifest that was already read under a bound."""
        loaded = yaml.safe_load(payload.decode("utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("runtime contract が辞書ではない")
        contracts = loaded.get("contracts")
        if isinstance(contracts, dict):
            # YAML の kind と stage は文字列で来る。**ここで明示的に写す。**
            # 未知の値は ArtifactKind / AuthorityStage が拒む（黙って落とすと、
            # 検査したつもりの kind が検査されない）。
            loaded = {
                **loaded,
                "contracts": {
                    ArtifactKind(key): _normalized_stage(value) for key, value in contracts.items()
                },
            }
        return cls.model_validate(loaded)

    def compatibility(self) -> dict[ArtifactKind, ModelCompatibility]:
        """Return the per-kind compatibility map consumed by ``ModelRegistry.verify()``."""
        return {kind: entry.as_compatibility() for kind, entry in self.contracts.items()}


def parse_artifact(value: str) -> ArtifactRef:
    """``kind/model-id/version`` を `ArtifactRef` にする（`ArtifactRef.key` と同じ形）。"""
    parts = value.split("/")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("artifact は kind/model-id/version の形で指定する")
    kind, model_id, version = parts
    try:
        return ArtifactRef(kind=ArtifactKind(kind), model_id=model_id, version=version)
    except (ValueError, ValidationError) as exc:
        raise argparse.ArgumentTypeError(f"artifact を解釈できない: {value}") from exc


def emit(payload: object) -> None:
    """人と `jq` の両方が読める1件の JSON を stdout へ出す。

    ログ（stderr）は運転の記録で、stdout は問い合わせの答えである。混ぜない。
    """
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))  # noqa: T201


def _limits(args: argparse.Namespace) -> ModelRegistryLimits:
    directory: Path = args.limits
    with as_configuration_error(directory.name):
        return load_model_registry_limits(directory)


def _registry(args: argparse.Namespace, limits: ModelRegistryLimits | None = None) -> ModelRegistry:
    return ModelRegistry(args.root, limits=_limits(args) if limits is None else limits)


@contextmanager
def as_configuration_error(name: str) -> Iterator[None]:
    """設定 parser の再帰を、**記録される設定の誤り**へ寄せる。

    深く入れ子にした YAML は、byte 上限に収まっていても PyYAML の再帰を尽くし、
    `RecursionError` を投げる。これは `ValueError` でも `yaml.YAMLError` でもないため、
    そのままでは traceback で終わり、終了コード 1 と構造化ログという約束が破れる
    （決定記録 0062 §2.1）。

    **深さを先に測って弾く方式は採らない。** YAML は flow（`[[[`）と block（字下げ）と
    alias で入れ子を作れるので、片方だけを数える走査は、持っていない上限を持っていると
    主張することになる。ここでは parser に測らせ、結果を設定の誤りとして扱う。
    `RecursionError` が伝播する時点で stack は巻き戻っているため、このあとのログ出力は
    再び深さを尽くさない。
    """
    try:
        yield
    except RecursionError as exc:
        raise ValueError(f"設定の入れ子が深すぎる: {name}") from exc


def read_bounded(path: Path, max_bytes: int) -> bytes:
    """上限まで**だけ**読む。読んでから大きさを判断しない。

    運用者が間違えて数 GB のファイルを指したときに、管理 process を MemoryError で
    落とさないようにする。`fstat` の大きさを信じずに上限＋1 byte を読むので、読取中に
    伸びたファイルも拒否できる（決定記録 0062 §2.1）。
    """
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ArtifactVerificationError(
            f"ファイルが上限 {max_bytes} byte を超えている: {path.name}"
        )
    return payload


def _compatibility(args: argparse.Namespace) -> ModelCompatibility:
    return ModelCompatibility(
        feature_schema_version=args.feature_schema,
        target_schema_version=args.target_schema,
        authority_stage=AuthorityStage(args.authority_stage),
    )


def _approval(path: Path, limits: ModelRegistryLimits) -> HumanApproval:
    """人が書いた承認をそのまま読む。**CLI は中身を作らない・直さない。**"""
    return HumanApproval.model_validate_json(read_bounded(path, limits.max_snapshot_bytes))


def run_status(args: argparse.Namespace) -> int:
    """production pointer と全 artifact の lifecycle を出す（bytes は読まない）。"""
    snapshot = _registry(args).inspect()
    emit(
        {
            "schema_version": snapshot.schema_version,
            "revision": snapshot.revision,
            "production": {
                kind.value: {
                    "active": slot.active.key,
                    "rollback_target": None if slot.previous is None else slot.previous.key,
                }
                for kind, slot in sorted(
                    snapshot.production.items(), key=lambda item: item[0].value
                )
            },
            "artifacts": [
                {
                    "artifact": key,
                    "status": record.status.value,
                    "capability": record.metadata.capability.value,
                    "created_at": record.metadata.created_at,
                    "training_dataset_version": record.metadata.training_dataset_version,
                    "source_runs": list(record.metadata.source_runs),
                    "feature_schema_version": record.metadata.feature_schema_version,
                    "target_schema_version": record.metadata.target_schema_version,
                    "code_commit": record.metadata.code_commit,
                    "model_family": record.metadata.model_family,
                    "sha256": record.metadata.sha256,
                    "offline_evaluation_ref": record.metadata.offline_evaluation_ref,
                    "shadow_evaluation_ref": record.metadata.shadow_evaluation_ref,
                    "authority_compatibility": [
                        stage.value for stage in record.metadata.authority_compatibility
                    ],
                }
                for key, record in sorted(snapshot.artifacts.items())
            ],
        }
    )
    return EXIT_OK


def run_audit(args: argparse.Namespace) -> int:
    """監査 event を出す。`--pointer-changes` で promotion / rollback だけに絞る。"""
    snapshot = _registry(args).inspect()
    events = snapshot.pointer_changes if args.pointer_changes else snapshot.audit
    emit([event.trace_metadata() for event in events])
    return EXIT_OK


def run_verify(args: argparse.Namespace) -> int:
    """起動時検証を実行する。**registry を書き換えない。**"""
    limits = _limits(args)
    contracts: dict[ArtifactKind, ModelCompatibility] = {}
    if args.contract is not None:
        payload = read_bounded(args.contract, limits.max_snapshot_bytes)
        with as_configuration_error(args.contract.name):
            contracts = RuntimeContracts.from_bytes(payload).compatibility()
    report = _registry(args, limits).verify(contracts)
    if args.contract is not None and report.unchecked_kinds():
        # **contract を渡したなら、いま production の kind を全部覆う。** 欠けた kind は
        # checksum と format だけで `loaded` になり、schema や authority が合っていなくても
        # 総合判定が `ok` になる。判定を出さずに失敗させる（決定記録 0062 §2.4）。
        missing = ", ".join(kind.value for kind in report.unchecked_kinds())
        raise ValueError(f"runtime contract に production の kind がない: {missing}")
    emit(report.trace_metadata())
    _log_health(report)
    return _VERIFY_EXIT[report.health]


def _log_health(report: RegistryHealthReport) -> None:
    level = logging.INFO if report.health is RegistryHealth.OK else logging.WARNING
    LOGGER.log(
        level,
        "Model Registry の起動時検証",
        extra={
            logs.FIELDS_KEY: {
                "health": report.health.value,
                "registry_revision": report.revision,
                "detail": report.detail,
                "fallback_kinds": [kind.value for kind in report.fallback_kinds()],
            }
        },
    )


def run_register(args: argparse.Namespace) -> int:
    """candidate を登録する。**production にはしない。**"""
    limits = _limits(args)
    metadata = ArtifactMetadata.model_validate_json(
        read_bounded(args.metadata, limits.max_snapshot_bytes)
    )
    # artifact 本体は**読む前に**上限で切る。registry 側の検査は bytes を作ったあとに走る。
    payload = read_bounded(args.payload, limits.max_artifact_bytes)
    ref = _registry(args, limits).register_candidate(
        metadata,
        payload,
        actor=args.actor,
        reason=args.reason,
    )
    _log_change("candidate を登録した", artifact=ref, actor=args.actor, reason=args.reason)
    emit({"artifact": ref.key, "status": "candidate"})
    return EXIT_OK


def run_validate(args: argparse.Namespace) -> int:
    """offline evaluation の結果を記録し、candidate を validated にする。"""
    revision = _registry(args).mark_validated(
        args.artifact,
        offline_evaluation_ref=args.offline_evaluation_ref,
        actor=args.actor,
        reason=args.reason,
        expected_revision=args.expected_revision,
    )
    _log_change(
        "artifact を validated にした",
        artifact=args.artifact,
        actor=args.actor,
        reason=args.reason,
        revision=revision,
    )
    emit({"artifact": args.artifact.key, "status": "validated", "revision": revision})
    return EXIT_OK


def run_promote(args: argparse.Namespace) -> int:
    """人が署名した承認で、validated artifact を production にする。"""
    limits = _limits(args)
    approval = _approval(args.approval, limits)
    revision = _registry(args, limits).promote(
        approval.artifact,
        _compatibility(args),
        shadow_evaluation_ref=args.shadow_evaluation_ref,
        approval=approval,
        expected_revision=args.expected_revision,
    )
    _log_change(
        "artifact を production にした",
        artifact=approval.artifact,
        actor=approval.approver,
        reason=approval.reason,
        revision=revision,
    )
    emit({"artifact": approval.artifact.key, "status": "production", "revision": revision})
    return EXIT_OK


def run_rollback(args: argparse.Namespace) -> int:
    """人が署名した承認で、known-good な artifact へ戻す。"""
    limits = _limits(args)
    approval = _approval(args.approval, limits)
    revision = _registry(args, limits).rollback(
        ArtifactKind(args.kind),
        _compatibility(args),
        approval=approval,
        expected_revision=args.expected_revision,
    )
    _log_change(
        "production を rollback した",
        artifact=approval.artifact,
        actor=approval.approver,
        reason=approval.reason,
        revision=revision,
    )
    emit({"artifact": approval.artifact.key, "status": "production", "revision": revision})
    return EXIT_OK


def run_retire(args: argparse.Namespace) -> int:
    """candidate / validated artifact を retire する（production は retire できない）。"""
    revision = _registry(args).retire(
        args.artifact,
        actor=args.actor,
        reason=args.reason,
        expected_revision=args.expected_revision,
    )
    _log_change(
        "artifact を retire した",
        artifact=args.artifact,
        actor=args.actor,
        reason=args.reason,
        revision=revision,
    )
    emit({"artifact": args.artifact.key, "status": "retired", "revision": revision})
    return EXIT_OK


def _log_change(
    message: str,
    *,
    artifact: ArtifactRef,
    actor: str,
    reason: str,
    revision: int | None = None,
) -> None:
    LOGGER.info(
        message,
        extra={
            logs.FIELDS_KEY: {
                "artifact": artifact.key,
                "actor": actor,
                "reason": reason,
                "registry_revision": revision,
            }
        },
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, required=True, help="registry root ディレクトリ")
    parser.add_argument(
        "--limits",
        type=Path,
        default=Path("config"),
        help="model-registry.yaml のあるディレクトリ",
    )


def _add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True, help="操作した人（小文字の識別子）")
    parser.add_argument("--reason", required=True, help="なぜこの操作をしたか")


def _add_expected_revision(parser: argparse.ArgumentParser) -> None:
    # 既定値を持たせない。いまの revision を読まずに書けると、他者の判断を後勝ちで潰す。
    parser.add_argument(
        "--expected-revision",
        type=int,
        required=True,
        help="この操作が前提とする registry revision（status で確認する）",
    )


def _add_contract(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--feature-schema", required=True, help="runtime の feature schema version")
    parser.add_argument("--target-schema", required=True, help="runtime の target schema version")
    parser.add_argument(
        "--authority-stage",
        required=True,
        choices=[stage.value for stage in AuthorityStage],
        help="**実際に運転する** authority stage（設定上の上限ではない）",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the operator CLI parser."""
    parser = argparse.ArgumentParser(
        prog="coldaisle-registry",
        description="Control Model Registry の lifecycle 操作（#104）",
    )
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="production pointer と lifecycle を出す")
    _add_common(status)
    status.set_defaults(handler=run_status)

    audit = subparsers.add_parser("audit", help="監査 event を出す")
    _add_common(audit)
    audit.add_argument(
        "--pointer-changes",
        action="store_true",
        help="promotion / rollback だけに絞る",
    )
    audit.set_defaults(handler=run_audit)

    verify = subparsers.add_parser("verify", help="起動時検証（書き換えない）")
    _add_common(verify)
    verify.add_argument(
        "--contract",
        type=Path,
        default=None,
        help="kind ごとの runtime contract。省略すると checksum と format だけを見る",
    )
    verify.set_defaults(handler=run_verify)

    register = subparsers.add_parser("register", help="candidate を登録する")
    _add_common(register)
    register.add_argument("--metadata", type=Path, required=True, help="ArtifactMetadata の JSON")
    register.add_argument("--payload", type=Path, required=True, help="artifact 本体")
    _add_actor(register)
    register.set_defaults(handler=run_register)

    validate = subparsers.add_parser("validate", help="candidate を validated にする")
    _add_common(validate)
    validate.add_argument("--artifact", type=parse_artifact, required=True)
    validate.add_argument("--offline-evaluation-ref", required=True)
    _add_actor(validate)
    _add_expected_revision(validate)
    validate.set_defaults(handler=run_validate)

    promote = subparsers.add_parser("promote", help="validated artifact を production にする")
    _add_common(promote)
    # 対象 artifact は**承認が名指ししたもの**を使う。別に指定できると、承認とずれた
    # 対象を渡す経路ができる（registry も拒むが、経路自体を作らない）。
    promote.add_argument("--approval", type=Path, required=True, help="人が書いた承認の JSON")
    promote.add_argument("--shadow-evaluation-ref", required=True)
    _add_contract(promote)
    _add_expected_revision(promote)
    promote.set_defaults(handler=run_promote)

    rollback = subparsers.add_parser("rollback", help="known-good artifact へ戻す")
    _add_common(rollback)
    rollback.add_argument(
        "--kind",
        required=True,
        choices=[kind.value for kind in ArtifactKind],
    )
    rollback.add_argument("--approval", type=Path, required=True, help="人が書いた承認の JSON")
    _add_contract(rollback)
    _add_expected_revision(rollback)
    rollback.set_defaults(handler=run_rollback)

    retire = subparsers.add_parser("retire", help="candidate / validated を retire する")
    _add_common(retire)
    retire.add_argument("--artifact", type=parse_artifact, required=True)
    _add_actor(retire)
    _add_expected_revision(retire)
    retire.set_defaults(handler=run_retire)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one registry operation and return its exit code."""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    try:
        exit_code: int = args.handler(args)
    except (
        ModelRegistryError,
        ValidationError,
        ValueError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        # 握りつぶさない。操作は行われなかったことを、理由とともに残す。
        LOGGER.error(
            "Model Registry の操作に失敗した",
            extra={logs.FIELDS_KEY: {"command": args.command, "error": str(exc)}},
        )
        return EXIT_FAILED
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
