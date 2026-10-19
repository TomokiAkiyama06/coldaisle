"""学習の出力（``coldaisle-train --out``）の形・公開の手順・path の照合・確かめる関数。

決定記録 0150 §2.5。

``coldaisle-train``（書き手）と ``coldaisle-registry register --training-output``（読み手）の
2つの合成の起点が共有する。どちらの合成の起点にも属さないレイヤ横断の module に置く
（``csv_export_manifest.py`` / ``declared_changes.py`` と同じ前例）。

- **標準ライブラリと pydantic だけを使い、coldaisle の module を import しない。**
  上限は呼び出し側が int で渡す（``ModelRegistryLimits`` を受け取らない）
- 公開は OS に依らない POSIX の操作だけで行う（NFR-07）: ``os.mkdir`` で ``--out`` を
  排他的に確保し、
  3つのファイルを ``O_EXCL`` で書いて fsync し、完成の印を一時名で書いて fsync してから
  ``os.link`` で ``COMPLETE.json`` として公開する（既にある名前を置き換えない）。``os.rename`` /
  ``os.replace`` は使わない
- path の照合は ``--out`` の絶対 path を根から1要素ずつ ``O_NOFOLLOW`` の fd でたどり直し、各段が
  信頼できる祖先であることを確かめる（``os.lstat`` の連続は使わない）
- 読む側は有効な ``COMPLETE.json`` の無い ``--out`` を未完成として拒み、印に書かれた固定の3つの
  ファイルと ``COMPLETE.json`` だけを上限つきで読む

脅威の前提は (a) 途中の失敗・電源断・強制終了と (b) 別のユーザーによる名前の差し替えだけである。
同じ uid の悪意あるプロセスは前提の外（中身の再検証は同じ uid の誤った操作を見つける安価な検査）。
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ARTIFACT_FILENAME = "artifact.json"
METADATA_FILENAME = "metadata.json"
REPORT_FILENAME = "training-report.json"
COMPLETE_FILENAME = "COMPLETE.json"

OUTPUT_FILENAMES: tuple[str, ...] = (REPORT_FILENAME, ARTIFACT_FILENAME, METADATA_FILENAME)
"""書く順（``training-report.json`` を最初に。0150 §2.5 の手順 3）。"""

_TEMP_MARKER_PREFIX = ".COMPLETE.json.tmp-"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: 書き手の無い FIFO を開いて止まらない（regular file の読み込みには効かない）
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_ACL_XATTRS = frozenset({"system.posix_acl_access", "system.posix_acl_default"})
_READ_CHUNK_BYTES = 1024 * 1024

Identity = tuple[int, int]
"""``(st_dev, st_ino)``。"""


# ---------------------------------------------------------------- 印の形


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class MarkerEntry(_Frozen):
    """印に書く1つのファイル。"""

    name: str = Field(min_length=1, max_length=64)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompletionMarker(_Frozen):
    """``COMPLETE.json`` の中身（3つのファイルの名前・大きさ・SHA-256）。"""

    schema_version: Literal[1]
    files: tuple[MarkerEntry, ...] = Field(min_length=1, max_length=8)


def completion_marker_bytes(files: Mapping[str, bytes]) -> bytes:
    """書く3つのファイルから印の canonical bytes を作る。"""
    marker = CompletionMarker(
        schema_version=1,
        files=tuple(
            MarkerEntry(name=name, size=len(files[name]), sha256=_sha256(files[name]))
            for name in OUTPUT_FILENAMES
        ),
    )
    return (marker.model_dump_json() + "\n").encode("utf-8")


# ---------------------------------------------------------------- 例外


class TrainingOutputError(Exception):
    """学習の出力の公開・照合・読みの失敗。``stage`` は 0150 §2.4 の表の段。"""

    stage = "before_publish"

    def fields(self) -> dict[str, object]:
        """構造化ログに出す欄。"""
        return {}


class UntrustedPathError(TrainingOutputError):
    """根から ``--out`` の親までに信頼できない祖先がある・symlink を含む。

    段 ``untrusted_path``。
    """

    stage = "untrusted_path"

    def __init__(self, message: str, *, path: str, detail: Mapping[str, object]) -> None:
        super().__init__(message)
        self.path = path
        self.detail = dict(detail)

    def fields(self) -> dict[str, object]:
        return {"untrusted_path": self.path, **self.detail}


class OutputExistsError(TrainingOutputError):
    """``--out`` が既にある（手順 2 の ``EEXIST``）。書き手は何も作っていない。"""


class OutputReplacedError(TrainingOutputError):
    """``--out`` の path が手順 2 で開いたディレクトリを指していない（段 ``out_replaced``）。"""

    stage = "out_replaced"

    def __init__(
        self, message: str, *, expected: Identity, actual: Identity | None, reason: str
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual
        self.reason = reason

    def fields(self) -> dict[str, object]:
        return {
            "expected_identity": list(self.expected),
            "actual_identity": None if self.actual is None else list(self.actual),
            "mismatch_reason": self.reason,
        }


class PublishFailed(TrainingOutputError):
    """公開の前の失敗で、後始末が成功した（``--out`` は残らない）。原因は ``__cause__``。"""


@dataclass(frozen=True, slots=True)
class CleanupProblem:
    """後始末の1つの失敗（握りつぶさずにログへ出す）。"""

    operation: str
    name: str
    errno: int | None
    message: str


class CleanupFailed(TrainingOutputError):
    """公開の前の失敗の後始末が失敗した（段 ``cleanup_failed``）。元の失敗は ``__cause__``。"""

    stage = "cleanup_failed"

    def __init__(
        self,
        message: str,
        *,
        problems: tuple[CleanupProblem, ...],
        remaining: tuple[str, ...] | None,
    ) -> None:
        super().__init__(message)
        self.problems = problems
        self.remaining = remaining

    def fields(self) -> dict[str, object]:
        return {
            "cleanup_errors": [
                {
                    "operation": problem.operation,
                    "name": problem.name,
                    "errno": problem.errno,
                    "error": problem.message,
                }
                for problem in self.problems
            ],
            "remaining_names": None if self.remaining is None else list(self.remaining),
        }


class PublishDurabilityError(TrainingOutputError):
    """公開の後の一時名の unlink・``--out`` と親の fsync の失敗（段 ``publish_durability``）。"""

    stage = "publish_durability"


class TrainingOutputIncomplete(TrainingOutputError):
    """有効な ``COMPLETE.json`` が無い・印と実物が一致しない（読む側の拒否）。"""


# ---------------------------------------------------------------- path の照合


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(fd: int) -> Identity:
    result = os.fstat(fd)
    return (result.st_dev, result.st_ino)


def _absolute(path: Path) -> Path:
    """``..`` を拒み、作業ディレクトリからの絶対 path にする（symlink は解かない）。"""
    if ".." in path.parts:
        raise UntrustedPathError(
            "path に '..' は使えない", path=str(path), detail={"reason": "dotdot"}
        )
    absolute = path if path.is_absolute() else Path.cwd() / path
    return Path(os.path.normpath(absolute))


def _acl_names(fd: int) -> tuple[str, ...]:
    """fd が持つ Linux の POSIX ACL の xattr の名前。``os.listxattr`` が無ければ検査しない。"""
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        return ()
    try:
        names: list[str] = listxattr(fd)
    except OSError as error:
        if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            # xattr を持てないファイルシステムには ACL も無い
            return ()
        raise
    return tuple(sorted(name for name in names if name in _ACL_XATTRS))


def _untrusted_reason(directory: os.stat_result, child: os.stat_result | None) -> str | None:
    """祖先 D の規則 (a)(b)（0150 §2.5「path の照合」）。満たせば ``None``。"""
    trusted_owners = (0, os.geteuid())
    owner_ok = directory.st_uid in trusted_owners
    if owner_ok and not directory.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None  # (a)
    if directory.st_mode & stat.S_ISVTX and owner_ok:
        if child is None or child.st_uid in trusted_owners:
            return None  # (b)
        return "sticky な祖先の中の次の要素が他の uid の所有"
    if not owner_ok:
        return "祖先の所有者が root でも実行した uid でもない"
    return "祖先に group / other が書ける（sticky でない）"


def _check_ancestor(fd: int, child: os.stat_result | None, shown: str) -> None:
    directory = os.fstat(fd)
    reason = _untrusted_reason(directory, child)
    if reason is not None:
        raise UntrustedPathError(
            f"信頼できない祖先: {shown}: {reason}",
            path=shown,
            detail={
                "reason": reason,
                "owner_uid": directory.st_uid,
                "mode": oct(stat.S_IMODE(directory.st_mode)),
            },
        )
    acl = _acl_names(fd)
    if acl:
        raise UntrustedPathError(
            f"祖先に POSIX ACL がある: {shown}",
            path=shown,
            detail={"reason": "posix_acl", "xattrs": list(acl)},
        )


def _symlink_error(name: str, parent_fd: int, shown: str) -> UntrustedPathError:
    try:
        target: str | None = os.readlink(name, dir_fd=parent_fd)
    except OSError:
        target = None
    return UntrustedPathError(
        f"path に symlink を含む: {shown}（symlink を含まない実の path を渡す。"
        "macOS なら /private/tmp/... の形）",
        path=shown,
        detail={"reason": "symlink", "symlink": shown, "symlink_target": target},
    )


def _walk(path: Path, *, through_last: bool, symlink_is_untrusted: bool) -> int:
    """根から1要素ずつ ``O_NOFOLLOW`` でたどり、各祖先を確かめて最後に開いた fd を返す。

    ``through_last`` が偽なら最後の要素（``--out``）の親までをたどる。開けない要素は
    ``OSError``（``ENOENT`` / ``ENOTDIR`` / ``ELOOP``）。``symlink_is_untrusted`` なら symlink は
    :class:`UntrustedPathError`（開始の時点の検査）。
    """
    absolute = _absolute(path)
    components = [part for part in absolute.parts[1:] if part not in ("", ".")]
    if not through_last:
        components = components[:-1]
    current = os.open("/", _DIRECTORY_FLAGS)
    shown = Path("/")
    try:
        for name in components:
            child_path = shown / name
            try:
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=current)
            except OSError as error:
                if (
                    symlink_is_untrusted
                    and error.errno in (errno.ELOOP, errno.ENOTDIR)
                    and _is_symlink_at(name, current)
                ):
                    raise _symlink_error(name, current, str(child_path)) from error
                raise
            try:
                _check_ancestor(current, os.fstat(child), str(shown))
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
            shown = child_path
        if not through_last:
            # 親そのもの（次の要素はまだ無い。規則 (b) の次の要素の検査は以後の照合で行う）
            _check_ancestor(current, None, str(shown))
        return current
    except BaseException:
        os.close(current)
        raise


def _is_symlink_at(name: str, parent_fd: int) -> bool:
    try:
        os.readlink(name, dir_fd=parent_fd)
    except OSError:
        return False
    return True


def check_trusted_parent(path: Path) -> None:
    """開始の時点の検査: 根から ``path`` の親までが信頼できる祖先で、symlink を含まないこと。"""
    os.close(_walk(path, through_last=False, symlink_is_untrusted=True))


def open_trusted_path(path: Path) -> int:
    """``path`` を根から fd でたどって開く。祖先が信頼できなければ :class:`UntrustedPathError`。

    最後の要素まで ``O_NOFOLLOW | O_DIRECTORY`` で開き、その fd を返す（呼び出し側が閉じる）。
    途中か最後が symlink なら :class:`UntrustedPathError`（symlink を含まない実の path を渡す）。
    """
    return _walk(path, through_last=True, symlink_is_untrusted=True)


@dataclass(frozen=True, slots=True)
class PathCheck:
    """path の照合の結果。"""

    matches: bool
    actual: Identity | None
    reason: str


def path_names_directory(path: Path, expected: Identity) -> PathCheck:
    """``path`` がいま ``expected`` のディレクトリを指すか（根から fd でたどり直す）。

    途中か最後の要素が symlink・ディレクトリでない・無いときは不一致。祖先が信頼できなければ
    :class:`UntrustedPathError`（照合のたびに信頼の検査も行う）。
    """
    try:
        fd = _walk(path, through_last=True, symlink_is_untrusted=False)
    except UntrustedPathError:
        raise
    except OSError as error:
        # どの OSError も不一致として返す（照合できないものを一致とは言わない。呼び出し側は
        # 登録しない・rmdir しない・登録の後なら段として報告する）
        reason = {
            errno.ELOOP: "symlink",
            errno.ENOTDIR: "not_a_directory_or_symlink",
            errno.ENOENT: "missing",
        }.get(error.errno or 0, f"os_error: {error}")
        return PathCheck(matches=False, actual=None, reason=reason)
    try:
        actual = _identity(fd)
    except OSError as error:
        return PathCheck(matches=False, actual=None, reason=f"os_error: {error}")
    finally:
        os.close(fd)
    if actual != expected:
        return PathCheck(matches=False, actual=actual, reason="different_directory")
    return PathCheck(matches=True, actual=actual, reason="same_directory")


def require_path_names_directory(path: Path, expected: Identity, *, when: str) -> None:
    """照合が外れれば :class:`OutputReplacedError`。"""
    check = path_names_directory(path, expected)
    if not check.matches:
        raise OutputReplacedError(
            f"--out の名前が書き手のディレクトリを指していない（{when}）: {path}",
            expected=expected,
            actual=check.actual,
            reason=check.reason,
        )


# ---------------------------------------------------------------- 書く側（手順 2〜7）


@dataclass(slots=True)
class PublishedTrainingOutput:
    """公開した ``--out``。``fd`` は手順 2 で開いたディレクトリ（呼び出し側が閉じる）。"""

    path: Path
    fd: int
    identity: Identity
    files: dict[str, bytes] = field(default_factory=dict)
    marker: bytes = b""

    def close(self) -> None:
        """開いた fd を閉じる。"""
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _write_new_file(directory_fd: int, name: str, payload: bytes) -> None:
    fd = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=directory_fd)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_training_output(
    parent_fd: int, out_path: Path, files: Mapping[str, bytes]
) -> PublishedTrainingOutput:
    """0150 §2.5 の手順 2〜7 で ``--out`` を公開する。``parent_fd`` は手順 1 で開いた親。

    失敗は :class:`TrainingOutputError` の派生で、``stage`` が 0150 §2.4 の表の段を表す。
    link（公開の確定点）の前の失敗では、自分で作った ``--out`` の既知の名前だけを消して片づける。
    link の後の失敗では ``--out`` を片づけない。
    """
    if set(files) != set(OUTPUT_FILENAMES):
        raise ValueError("学習の出力は artifact.json・metadata.json・training-report.json の3つ")
    absolute = _absolute(out_path)
    name = absolute.name
    marker = completion_marker_bytes(files)
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError as error:
        raise OutputExistsError(f"--out が既にある（上書きしない）: {absolute}") from error
    try:
        out_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        # 作った直後に開けない: 名前が差し替えられた。書き手の作ったものか確かめられないので消さない
        raise CleanupFailed(
            f"作った --out を開けない: {absolute}",
            problems=(CleanupProblem("open", name, error.errno, str(error)),),
            remaining=None,
        ) from error
    identity = _identity(out_fd)
    temp_name = f"{_TEMP_MARKER_PREFIX}{secrets.token_hex(16)}"
    try:
        # 手順 2: 差し替えの検知・空であること・ACL を受け継いでいないこと
        require_path_names_directory(absolute, identity, when="作成の直後")
        if os.listdir(out_fd):
            raise OutputReplacedError(
                f"作った --out が空ではない: {absolute}",
                expected=identity,
                actual=identity,
                reason="not_empty",
            )
        acl = _acl_names(out_fd)
        if acl:
            raise UntrustedPathError(
                f"--out が POSIX ACL を持つ: {absolute}",
                path=str(absolute),
                detail={"reason": "posix_acl", "xattrs": list(acl)},
            )
        # 手順 3: training-report.json を最初に、それぞれ fsync
        for filename in OUTPUT_FILENAMES:
            _write_new_file(out_fd, filename, files[filename])
        # 手順 4: 印を一時名で書いて fsync（読む側はこの名前を見ない）
        _write_new_file(out_fd, temp_name, marker)
        # 手順 5: link の直前の照合、そして公開の確定点
        require_path_names_directory(absolute, identity, when="公開の直前")
        os.link(
            temp_name,
            COMPLETE_FILENAME,
            src_dir_fd=out_fd,
            dst_dir_fd=out_fd,
            follow_symlinks=False,
        )
    except Exception as error:
        try:
            _clean_up_before_publish(parent_fd, out_fd, absolute, identity, temp_name, error)
        finally:
            os.close(out_fd)

    published = PublishedTrainingOutput(
        path=absolute, fd=out_fd, identity=identity, files=dict(files), marker=marker
    )
    # 手順 6: 公開の後。ここからはどの失敗でも --out を片づけない
    problems: list[str] = []
    try:
        os.unlink(temp_name, dir_fd=out_fd)
    except OSError as error:
        problems.append(f"一時名の unlink: {error}")
    for label, fd in (("--out", out_fd), ("親", parent_fd)):
        try:
            os.fsync(fd)
        except OSError as error:
            problems.append(f"{label} の fsync: {error}")
    if problems:
        raise PublishDurabilityError(
            f"公開は済んだが永続が確かでない: {absolute}: {'; '.join(problems)}"
        )
    require_path_names_directory(absolute, identity, when="公開の後")
    return published


def _clean_up_before_publish(
    parent_fd: int,
    out_fd: int,
    absolute: Path,
    identity: Identity,
    temp_name: str,
    original: Exception,
) -> NoReturn:
    """手順 7: 既知の名前だけを fd で消し、照合が通るときだけ rmdir する。常に例外で終わる。"""
    problems: list[CleanupProblem] = []
    for filename in (*OUTPUT_FILENAMES, temp_name):
        try:
            os.unlink(filename, dir_fd=out_fd)
        except FileNotFoundError:
            continue  # 書く前に止まった名前
        except OSError as error:
            problems.append(CleanupProblem("unlink", filename, error.errno, str(error)))
    if problems:
        raise CleanupFailed(
            f"公開の前の失敗の後始末に失敗した: {absolute}",
            problems=tuple(problems),
            remaining=_remaining(out_fd),
        ) from original
    if isinstance(original, OutputReplacedError | UntrustedPathError):
        # 名前が別のものを指す・祖先が信頼できない: rmdir しない（空の --out が残る）
        raise original
    try:
        require_path_names_directory(absolute, identity, when="後始末の rmdir の前")
    except (UntrustedPathError, OutputReplacedError) as error:
        raise error from original
    try:
        os.rmdir(absolute.name, dir_fd=parent_fd)
    except OSError as error:
        raise CleanupFailed(
            f"公開の前の失敗の後始末に失敗した: {absolute}",
            problems=(CleanupProblem("rmdir", absolute.name, error.errno, str(error)),),
            remaining=_remaining(out_fd),
        ) from original
    raise PublishFailed(f"公開の前に失敗した（--out は片づけた）: {original}") from original


def _remaining(directory_fd: int) -> tuple[str, ...] | None:
    try:
        return tuple(sorted(os.listdir(directory_fd)))
    except OSError:
        return None


# ---------------------------------------------------------------- 読む側（確かめる関数）


@dataclass(frozen=True, slots=True)
class VerifiedTrainingOutput:
    """印を確かめたときに読んだ bytes そのもの（読み直さない）。"""

    artifact: bytes
    metadata: bytes
    report: bytes
    marker: bytes

    def file_bytes(self) -> dict[str, bytes]:
        """ファイル名 → bytes。"""
        return {
            ARTIFACT_FILENAME: self.artifact,
            METADATA_FILENAME: self.metadata,
            REPORT_FILENAME: self.report,
        }


def _open_regular_at(directory_fd: int, name: str) -> tuple[int, int]:
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
    except OSError as error:
        raise TrainingOutputIncomplete(f"{name} を開けない: {error}") from error
    try:
        try:
            result = os.fstat(fd)
        except OSError as error:
            raise TrainingOutputIncomplete(f"{name} を確かめられない: {error}") from error
        if not stat.S_ISREG(result.st_mode):
            raise TrainingOutputIncomplete(f"{name} が regular file ではない")
        return fd, result.st_size
    except BaseException:
        os.close(fd)
        raise


def _read_limited(fd: int, limit: int) -> bytes:
    """上限＋1 byte まで**だけ**読む。"""
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = os.read(fd, min(_READ_CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def read_training_output(
    directory_fd: int, *, max_artifact_bytes: int, max_other_bytes: int
) -> VerifiedTrainingOutput:
    """``--out`` の fd から印を確かめ、読んだ bytes を返す（0150 §2.5）。

    ``artifact.json`` は ``max_artifact_bytes``、``COMPLETE.json``・``metadata.json``・
    ``training-report.json`` は ``max_other_bytes`` を上限とし、parse の前に当てる。印の名前は
    固定の3つの basename とちょうど一致することを、どのファイルを開く前にも確かめる。
    """
    for label, value in (
        ("max_artifact_bytes", max_artifact_bytes),
        ("max_other_bytes", max_other_bytes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError(f"{label} は 0 以上の int を明示して渡す")
    fd, _size = _open_regular_at(directory_fd, COMPLETE_FILENAME)
    try:
        marker_bytes = _read_limited(fd, max_other_bytes)
    except OSError as error:
        raise TrainingOutputIncomplete(f"{COMPLETE_FILENAME} を読めない: {error}") from error
    finally:
        os.close(fd)
    if len(marker_bytes) > max_other_bytes:
        raise TrainingOutputIncomplete(
            f"{COMPLETE_FILENAME} が上限 {max_other_bytes} byte を超える"
        )
    try:
        marker = CompletionMarker.model_validate_json(marker_bytes)
    except ValidationError as error:
        raise TrainingOutputIncomplete(f"{COMPLETE_FILENAME} を読めない: {error}") from error
    names = [entry.name for entry in marker.files]
    if len(names) != len(OUTPUT_FILENAMES) or set(names) != set(OUTPUT_FILENAMES):
        raise TrainingOutputIncomplete(
            f"{COMPLETE_FILENAME} の名前が artifact.json・metadata.json・training-report.json の"
            "3つちょうどではない"
        )
    entries = {entry.name: entry for entry in marker.files}
    limits = {
        filename: max_artifact_bytes if filename == ARTIFACT_FILENAME else max_other_bytes
        for filename in OUTPUT_FILENAMES
    }
    for filename in OUTPUT_FILENAMES:
        if entries[filename].size > limits[filename]:
            raise TrainingOutputIncomplete(
                f"{filename} の大きさが上限 {limits[filename]} byte を超える"
            )
    contents: dict[str, bytes] = {}
    for filename in OUTPUT_FILENAMES:
        entry = entries[filename]
        fd, size = _open_regular_at(directory_fd, filename)
        try:
            if size != entry.size:
                raise TrainingOutputIncomplete(f"{filename} の大きさが印と違う")
            payload = _read_limited(fd, entry.size)
        except OSError as error:
            raise TrainingOutputIncomplete(f"{filename} を読めない: {error}") from error
        finally:
            os.close(fd)
        if len(payload) != entry.size:
            raise TrainingOutputIncomplete(f"{filename} の大きさが読取中に印と違った")
        if _sha256(payload) != entry.sha256:
            raise TrainingOutputIncomplete(f"{filename} の SHA-256 が印と違う")
        contents[filename] = payload
    return VerifiedTrainingOutput(
        artifact=contents[ARTIFACT_FILENAME],
        metadata=contents[METADATA_FILENAME],
        report=contents[REPORT_FILENAME],
        marker=marker_bytes,
    )


def output_differences(
    verified: VerifiedTrainingOutput, expected: Mapping[str, bytes], expected_marker: bytes
) -> list[dict[str, str]]:
    """確かめた bytes と期待した bytes の違い（ファイル名と2つの SHA-256）。"""
    differences: list[dict[str, str]] = []
    actual = {**verified.file_bytes(), COMPLETE_FILENAME: verified.marker}
    wanted = {**dict(expected), COMPLETE_FILENAME: expected_marker}
    for filename in (*OUTPUT_FILENAMES, COMPLETE_FILENAME):
        if actual[filename] != wanted[filename]:
            differences.append(
                {
                    "file": filename,
                    "expected_sha256": _sha256(wanted[filename]),
                    "actual_sha256": _sha256(actual[filename]),
                }
            )
    return differences


__all__ = [
    "ARTIFACT_FILENAME",
    "COMPLETE_FILENAME",
    "METADATA_FILENAME",
    "OUTPUT_FILENAMES",
    "REPORT_FILENAME",
    "CleanupFailed",
    "CleanupProblem",
    "CompletionMarker",
    "MarkerEntry",
    "OutputExistsError",
    "OutputReplacedError",
    "PathCheck",
    "PublishDurabilityError",
    "PublishFailed",
    "PublishedTrainingOutput",
    "TrainingOutputError",
    "TrainingOutputIncomplete",
    "UntrustedPathError",
    "VerifiedTrainingOutput",
    "check_trusted_parent",
    "completion_marker_bytes",
    "open_trusted_path",
    "output_differences",
    "path_names_directory",
    "publish_training_output",
    "read_training_output",
    "require_path_names_directory",
]
