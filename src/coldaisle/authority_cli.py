"""合成の起点: Authority Stage の昇格と、fand が止まっているときの rollback（#92）。

決定記録 0072 §2.1 / §2.10 段階 3、0086 段階 3b。**人が自分の uid で実行する CLI** で、
`AuthorityStore.raise_stage()` を呼ぶ唯一の入口である。

```bash
uv run coldaisle-authority raise --authority-root /var/lib/coldaisle-authority \\
    --approval var/stage-approval.json --report var/evaluation.json \\
    --config-dir var/control-config --registry-root var/model-registry
uv run coldaisle-authority rollback --authority-root /var/lib/coldaisle-authority \\
    --reason "挙動を見直す"
```

- **承認者は引数でもファイルでも受け取らない**（0086 §2.5）。CLI を実行した process の
  `uid.<os.getuid()>` を承認者にし、承認ファイルに `approver` / `approver_binding` があれば拒む。
  環境変数（`SUDO_UID` など）は読まない（§2.1）
- 昇格の拒否（root・authority のディレクトリの所有者・`uid != euid`。§2.3）は
  `AuthorityStore` が行う。**ここでは error を表示と終了コードへ写すだけ**にする
- rollback は uid で拒まない（§2.6）。誰が戻したかは `uid.<数値>` で残る
- journal には `uid.<数値>` だけを書く。名前は stdout の表示のときだけ引く（§2.8）
- **DB を開かない**（管理操作の監査の表には書かない。§2.8）。authority の変更の正本は journal

**この module を fand・control・API・AI・eventd・管理ソケットから import しない。**
制御プロセスの中に authority を上げる経路を持ち込まない（0072 §2.1、AGENTS.md ルール1・2）。
"""

from __future__ import annotations

import argparse
import json
import logging
import pwd
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from coldaisle import logs
from coldaisle.clock import Clock, WallClock
from coldaisle.control.authority import (
    APPROVER_BINDING_PROCESS_UID,
    AuthorityApprovalError,
    AuthorityApproverError,
    AuthorityError,
    AuthorityEvidenceError,
    AuthorityJournal,
    AuthorityStateError,
    AuthorityStore,
    AuthorityStoreError,
    OsProcessIdentity,
    ProcessCredentials,
    ProcessIdentity,
    StageApproval,
)
from coldaisle.control.config import ControlConfig
from coldaisle.control.model_registry import (
    ModelRegistry,
    ModelRegistryError,
    ModelRegistryLimits,
    load_model_registry_limits,
)

LOGGER = logging.getLogger("coldaisle.authority")

EXIT_OK = 0
EXIT_FAILED = 1
"""読めない・書けない（path・権限・I/O・壊れた journal・不正な設定）。"""
EXIT_APPROVER_REJECTED = 3
"""実行者を承認者として認めない（決定記録 0086 §2.3 / §2.5）。理由は `code` で区別する。"""
EXIT_APPROVAL_REJECTED = 4
"""承認・証拠を受け入れない（期限・revision・遷移・上限・証拠の不足。0057 §2.3 / §2.4）。"""

_SELF_DECLARED_FIELDS = ("approver", "approver_binding")
"""承認ファイルに置かせない欄。CLI が実行者から組み立てる（0086 §2.5）。"""


class InputFileError(ValueError):
    """入力ファイルが上限を超えている。"""


class ApprovalFileError(ValueError):
    """承認ファイルの形が受け入れられない。"""


def emit(payload: object) -> None:
    """人と `jq` の両方が読める1件の JSON を stdout へ出す。

    ログ（stderr）は運転の記録で、stdout は操作の答えである。混ぜない。
    """
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))  # noqa: T201


def display_actor(uid: int) -> str:
    """`uid.<数値>（<名前>）`。名前を引けなければ数値だけ。

    **表示のためだけに引く。** 記録へ書き戻さない（改名・削除・別ホストで意味が変わる。0086 §2.8）。
    """
    try:
        name = pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return f"uid.{uid}"
    return f"uid.{uid}（{name}）"


def read_bounded(path: Path, max_bytes: int) -> bytes:
    """上限まで**だけ**読む。読んでから大きさを判断しない（`coldaisle-registry` と同じ）。"""
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise InputFileError(f"ファイルが上限 {max_bytes} byte を超えている: {path.name}")
    return payload


def load_approval(payload: bytes, credentials: ProcessCredentials) -> StageApproval:
    """人が書いた承認に、**実行者**を承認者として足して `StageApproval` にする。

    承認ファイルに承認者を書かせない（0086 §2.5）。書き手が自分の uid を調べて書いても、
    合わなければ拒まれるだけで得るものが無く、合っていても自己申告の欄を残すことになる。
    承認者以外の欄（遷移・revision・理由・時刻・証拠）は**そのまま**渡す（0062 §2.2）。
    """
    try:
        document: Any = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApprovalFileError("承認ファイルを JSON として読めない") from error
    if not isinstance(document, dict):
        raise ApprovalFileError("承認ファイルは JSON object にする")
    present = [name for name in _SELF_DECLARED_FIELDS if name in document]
    if present:
        raise ApprovalFileError(
            f"承認ファイルに {', '.join(present)} を書かない（CLI が実行した uid を記録する）"
        )
    bound = {
        **document,
        "approver": credentials.actor,
        "approver_binding": APPROVER_BINDING_PROCESS_UID,
    }
    try:
        return StageApproval.model_validate_json(json.dumps(bound))
    except ValidationError as error:
        # 設定の検証の誤り（ValidationError）と区別できるよう、承認の誤りとして包む。
        raise ApprovalFileError(f"承認ファイルを検証できない: {error}") from error


def _store(args: argparse.Namespace, identity: ProcessIdentity, clock: Clock) -> AuthorityStore:
    # CLI の store はディレクトリを作らず、書く前に setgid・other 権限なしを確かめる（0086 §2.4）。
    # lock は待ち続けてよい。人の経路には deadman が無い（0060 §2.7）。
    return AuthorityStore(
        args.authority_root.absolute(),
        clock,
        identity=identity,
        require_shared_root=True,
    )


def _limits(directory: Path) -> ModelRegistryLimits:
    return load_model_registry_limits(directory)


def run_raise(args: argparse.Namespace, identity: ProcessIdentity, clock: Clock) -> int:
    """承認ファイルと評価報告で、authority stage を1段上げる。"""
    authority = _store(args, identity, clock)
    # 承認を組み立てる前に、実行者を承認者にできるかを store に確かめさせる（0086 §2.3）。
    credentials = authority.approver_credentials()
    limits = _limits(args.registry_limits)
    # 承認ファイルと報告の上限は Model Registry の上限を流用する（承認は `coldaisle-registry` と
    # 同じ snapshot の上限、報告は artifact の上限）。管理 process を巨大なファイルで落とさない。
    approval = load_approval(read_bounded(args.approval, limits.max_snapshot_bytes), credentials)
    report = read_bounded(args.report, limits.max_artifact_bytes)
    config = ControlConfig.from_directory(args.config_dir)
    registry = ModelRegistry(args.registry_root, limits=limits)
    journal = authority.raise_stage(
        approval=approval,
        evaluation_report=report,
        config=config,
        registry=registry,
    )
    report_sha256 = sha256(report).hexdigest()
    _log_change("authority stage を上げた", "raised", credentials, journal, report_sha256)
    emit(_result("raised", credentials, journal, report_sha256=report_sha256))
    return EXIT_OK


def run_rollback(args: argparse.Namespace, identity: ProcessIdentity, clock: Clock) -> int:
    """Baseline（Shadow）へ1手で戻す。**承認も uid による拒否も無い**（0057 §2.6 / 0086 §2.6）。"""
    credentials = identity.credentials()
    authority = _store(args, identity, clock)
    # **自分が追記したかは store が lock の中で決めたものを使う。** lock の外で読んだ journal と
    # 比べると、間に別の書き手（自動降格・別の rollback）が下げた変更を自分の操作として記録する。
    journal, appended = authority.rollback_to_baseline_with_outcome(
        actor=credentials.actor, reason=args.reason
    )
    changed = appended is not None
    event = "rolled_back" if changed else "already_baseline"
    from_stage = appended.from_stage.value if appended is not None else journal.stage.value
    _log_change(
        "authority stage を Baseline へ戻した" if changed else "すでに Baseline だった",
        event,
        credentials,
        journal,
        None,
        from_stage=from_stage,
    )
    emit(
        _result(
            event,
            credentials,
            journal,
            from_stage=from_stage,
            reason=None if appended is None else appended.reason,
        )
    )
    return EXIT_OK


def _result(
    event: str,
    credentials: ProcessCredentials,
    journal: AuthorityJournal,
    *,
    report_sha256: str | None = None,
    from_stage: str | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    last = journal.last_change
    if reason is None and event == "raised" and last is not None:
        # 昇格は lock の中で追記した直後の journal なので、最後の event が自分のものである。
        reason = last.reason
    return {
        "event": event,
        "actor": display_actor(credentials.uid),
        "from_stage": from_stage if from_stage is not None else _from_stage(journal),
        "to_stage": journal.stage.value,
        "revision": journal.revision,
        "schema_version": journal.schema_version,
        "reason": reason,
        "report_sha256": report_sha256,
    }


def _from_stage(journal: AuthorityJournal) -> str | None:
    last = journal.last_change
    return None if last is None else last.from_stage.value


def _log_change(
    message: str,
    event: str,
    credentials: ProcessCredentials,
    journal: AuthorityJournal,
    report_sha256: str | None,
    *,
    from_stage: str | None = None,
) -> None:
    """1操作1行の構造化ログ（0086 §2.8）。名前は残さず uid だけを残す。"""
    LOGGER.info(
        message,
        extra={
            logs.FIELDS_KEY: {
                "event": event,
                "uid": credentials.uid,
                "euid": credentials.euid,
                "from_stage": from_stage if from_stage is not None else _from_stage(journal),
                "to_stage": journal.stage.value,
                "revision": journal.revision,
                "report_sha256": report_sha256,
            }
        },
    )


def _failure(error: BaseException) -> tuple[int, str]:
    """error を終了コードと理由の code へ写す。**判断はしない**（判断は store が行う）。"""
    if isinstance(error, AuthorityApproverError):
        return EXIT_APPROVER_REJECTED, error.code.value
    if isinstance(error, AuthorityEvidenceError):
        return EXIT_APPROVAL_REJECTED, "evidence_rejected"
    if isinstance(error, AuthorityApprovalError):
        return EXIT_APPROVAL_REJECTED, "approval_rejected"
    if isinstance(error, ApprovalFileError):
        return EXIT_APPROVAL_REJECTED, "invalid_approval"
    if isinstance(error, InputFileError):
        return EXIT_FAILED, "input_too_large"
    if isinstance(error, AuthorityStateError):
        return EXIT_FAILED, "journal_invalid"
    if isinstance(error, AuthorityStoreError):
        return EXIT_FAILED, "store_error"
    if isinstance(error, ModelRegistryError):
        return EXIT_FAILED, "registry_error"
    return EXIT_FAILED, "io_or_config_error"


def build_parser() -> argparse.ArgumentParser:
    """CLI を組み立てる。**`--approver` は作らない**（0086 §2.5）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-authority",
        description="Authority Stage の昇格と rollback（#92 / 決定記録 0057 / 0086）",
    )
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    raise_parser = subparsers.add_parser(
        "raise", help="承認と評価報告で1段上げる（承認者は実行した uid）"
    )
    raise_parser.add_argument(
        "--authority-root",
        type=Path,
        required=True,
        help="authority.json のディレクトリ（導入手順で作る。CLI は作らない）",
    )
    raise_parser.add_argument(
        "--approval",
        type=Path,
        required=True,
        help="人が書いた昇格の承認（JSON。approver は書かない）",
    )
    raise_parser.add_argument(
        "--report", type=Path, required=True, help="承認が指した Offline Evaluation の報告"
    )
    raise_parser.add_argument(
        "--config-dir",
        type=Path,
        required=True,
        help="いま coldaisle-fand が使っている Control Config の4ファイルのディレクトリ",
    )
    raise_parser.add_argument(
        "--registry-root", type=Path, required=True, help="Model Registry の root"
    )
    raise_parser.add_argument(
        "--registry-limits",
        type=Path,
        default=Path("config"),
        help="model-registry.yaml のあるディレクトリ",
    )
    raise_parser.set_defaults(handler=run_raise)

    rollback = subparsers.add_parser(
        "rollback", help="Baseline（Shadow）へ戻す（承認は要らない。uid を記録する）"
    )
    rollback.add_argument(
        "--authority-root", type=Path, required=True, help="authority.json のディレクトリ"
    )
    rollback.add_argument("--reason", required=True, help="なぜ戻すか")
    rollback.set_defaults(handler=run_rollback)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    identity: ProcessIdentity | None = None,
    clock: Clock | None = None,
) -> int:
    """1操作を実行し、終了コードを返す。

    ``identity`` / ``clock`` は試験で差し替えるためだけにある（実 root を要さない。0086 §2.9）。
    """
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    source: ProcessIdentity = identity if identity is not None else OsProcessIdentity()
    try:
        exit_code: int = args.handler(args, source, clock if clock is not None else WallClock())
    except (
        AuthorityError,
        ValidationError,
        ValueError,
        OSError,
        ModelRegistryError,
        yaml.YAMLError,
        RecursionError,
    ) as error:
        # YAML の構文の誤り（`yaml.YAMLError` は ValueError ではない）と、深い入れ子が PyYAML の
        # 再帰を尽くす `RecursionError` も、設定の誤りとして終了コード 1 と構造化ログにする
        # （`coldaisle-registry` の `as_configuration_error` と同じ扱い）。
        # traceback で終わらせない。
        # 握りつぶさない。操作は行われなかったことを、理由の code とともに残す（0086 §2.8）。
        exit_code, code = _failure(error)
        fields: dict[str, object] = {"event": f"{args.command}_failed", "code": code}
        try:
            credentials = source.credentials()
        except AuthorityApproverError:
            pass
        else:
            fields |= {"uid": credentials.uid, "euid": credentials.euid}
        LOGGER.error(
            "authority の操作を行わなかった",
            extra={logs.FIELDS_KEY: fields | {"error": str(error)}},
        )
        return exit_code
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
