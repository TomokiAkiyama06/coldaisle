"""レイヤ横断: ローカル Unix ソケットの入口に共通の門と起動時の検査（決定記録 0045 / 0072 §2.5）。

`coldaisle-eventd`（0045）と `coldaisle-fand` の管理ソケット（0072）が**同じ検査**を使う。
0072 §2.5 は「0045 の検査を共通の部品にして使う」と決めており、この module がその部品である。

門は2つ（0045 §2.2 / §2.3）:

1. ファイルの権限（`socket.mode` / `socket.group`。other には開けない）
2. 接続相手の uid（`SO_PEERCRED`）。接続のたびに確かめる

起動時の検査: other が書ける親ディレクトリ、ソケット以外の既存ファイル、既に応答する
ソケット、`sun_path` の長さ上限、`umask` を絞った作成、`<socket>.lock` による起動の直列化。

**この module は入口の中身（メッセージ・保存・制御）を知らない。** どの入口も import してよい
（`clock.py` と同じレイヤ横断の部品）。入口どうしは互いを import しない。
"""

from __future__ import annotations

import contextlib
import fcntl
import grp
import os
import pwd
import re
import socket
import stat
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

SOCKET_PATH_MAX_BYTES = 107
"""Linux の `sun_path` は 108 バイトで、終端の NUL を含む。"""

PARENT_DIR_MODE = 0o750
"""親ディレクトリを作るときの権限。other からは辿れない（0045 §2.2）。"""

_MODE_PATTERN = re.compile(r"^0[0-7]{3}$")
_GROUP_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

_PEERCRED = struct.Struct("iII")
"""`struct ucred { pid_t pid; uid_t uid; gid_t gid; }`（Linux）。

pid_t は符号付き、uid_t / gid_t は符号なし。`i` で読むと 2**31 以上の uid が負になり、
認可の比較を誤る。
"""


class SocketStartupError(RuntimeError):
    """安全に待ち受けられない。fail closed で起動しない（0045 §2.2 / §2.3）。"""


class SocketSettings(BaseModel):
    """ソケットの場所と権限（0045 §2.2。0072 §2.8 も同じ規則）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    mode: str = Field(description='8進の文字列（例: "0660"）')
    group: str | None = None

    @field_validator("path")
    @classmethod
    def _path_fits_sun_path(cls, value: Path) -> Path:
        if not str(value):
            raise ValueError("socket.path が空")
        if len(bytes(value)) > SOCKET_PATH_MAX_BYTES:
            raise ValueError(f"socket.path が長すぎる（{SOCKET_PATH_MAX_BYTES} バイトまで）")
        return value

    @field_validator("mode")
    @classmethod
    def _mode_is_not_world_accessible(cls, value: str) -> str:
        if not _MODE_PATTERN.match(value):
            raise ValueError('socket.mode は "0660" のような4桁の8進文字列で書く')
        bits = int(value, 8)
        if bits & stat.S_IRWXO:
            # other に1ビットでも開けると、同じホストの全利用者が門の手前まで来られる
            raise ValueError(f"socket.mode に other の権限を含めない: {value}")
        if bits & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise ValueError(f"socket.mode に setuid / setgid / sticky を含めない: {value}")
        if not bits & stat.S_IWUSR:
            raise ValueError(f"socket.mode は所有者が書ける値にする: {value}")
        return value

    @field_validator("group")
    @classmethod
    def _group_is_a_plain_name(cls, value: str | None) -> str | None:
        if value is not None and not _GROUP_PATTERN.match(value):
            raise ValueError(f"socket.group の書式が不正: {value!r}")
        return value

    @property
    def mode_bits(self) -> int:
        """`mode` を整数のビットにしたもの。"""
        return int(self.mode, 8)


def peer_credentials_supported() -> bool:
    """このプラットフォームで `SO_PEERCRED` を使えるか。"""
    return hasattr(socket, "SO_PEERCRED")


def peer_uid(conn: socket.socket) -> int:
    """接続相手の uid を `SO_PEERCRED` で取る。"""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
    _, uid, _ = _PEERCRED.unpack(raw)
    return int(uid)


def resolve_group(name: str | None) -> int | None:
    """`socket.group` の gid。**解決できなければ起動しない。**"""
    if name is None:
        return None
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError as exc:
        raise SocketStartupError(f"socket.group を解決できない: {name}") from exc


@dataclass(frozen=True, slots=True)
class Authorizer:
    """接続相手の uid を認可する（0045 §2.3）。

    root を暗黙に認めない。グループの判定は接続のたびに引き直すので、
    グループから外した利用者はサーバを再起動しなくても書けなくなる。

    ``allow_same_user`` が false のとき、サーバと同じ uid は**グループの判定より前に**
    拒否する。サーバのユーザーがそのグループのメンバーでも同じ（決定記録 0080 §2.1）。
    `coldaisle-fand` はソケットのグループを付け替えるためにそのグループへ入るので、
    グループの判定だけでは fand と同じ uid で動く任意のプロセスが管理操作を送れてしまう。
    """

    server_uid: int
    allow_same_user: bool
    group_gid: int | None

    def allows(self, uid: int) -> bool:
        """この uid の接続を受けてよいか。"""
        if uid == self.server_uid:
            return self.allow_same_user
        if self.group_gid is None:
            return False
        try:
            user = pwd.getpwuid(uid)
            group = grp.getgrgid(self.group_gid)
        except KeyError:
            # 名前を引けない uid はメンバーと判定できない。認めない側へ倒す
            return False
        return user.pw_gid == self.group_gid or user.pw_name in group.gr_mem


@contextlib.contextmanager
def umask(mask: int) -> Iterator[None]:
    """ブロックの間だけ `umask` を差し替える。"""
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def lock_path(path: Path) -> Path:
    """ソケットの隣の `<socket>.lock`。"""
    return path.with_name(path.name + ".lock")


def acquire_lock(path: Path, *, service: str) -> int:
    """ソケットの隣の `<socket>.lock` を排他ロックし、その fd を返す。

    **古いソケットの掃除から bind までを直列にするため。** 同時に起動した2つが
    どちらも古いソケットを「誰も待ち受けていない」と見ると、先に bind した側の
    ソケットを後の側が消してしまう。ロックは待ち受けている間ずっと持ち、取れなければ
    待たずに止まる。ロックファイル自体は消さない（消すと別の inode をロックする
    プロセスが現れ、直列にならない）。
    """
    lock = lock_path(path)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(lock, flags, 0o600)
    except OSError as exc:
        raise SocketStartupError(f"ロックファイルを開けない: {lock}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise SocketStartupError(f"別の {service} が起動している: {lock}") from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


def prepare_parent(parent: Path, group_gid: int | None, *, service: str) -> None:
    """ソケットの親ディレクトリを作り、他人に差し替えられないことを確かめる。

    `socket.group` が決まっているときは、**そのグループが親までたどれること**も確かめる。
    ソケットを 0660 + グループにしても、途中のディレクトリを通れなければ書き手は
    EACCES で接続できない。作るディレクトリは 0750 のままグループだけを合わせ、
    既にあるディレクトリの権限は広げない（足りなければ止まって直し方を示す）。
    """
    if group_gid is not None:
        # 作る前に確かめる。リンク先に勝手にディレクトリを作らない
        reject_symlink_components(parent)
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        # parents=True は途中のディレクトリを umask のまま作る。1段ずつ作って揃える
        with umask(0o077):
            directory.mkdir(mode=PARENT_DIR_MODE)
        os.chmod(directory, PARENT_DIR_MODE)
        if group_gid is not None:
            try:
                os.chown(directory, -1, group_gid)
            except OSError as exc:
                raise SocketStartupError(
                    f"作ったディレクトリのグループを socket.group に変えられない: {directory}"
                    f"（{service} の実行ユーザーがそのグループに属している必要がある）"
                ) from exc
    parent_mode = os.stat(parent).st_mode
    if not stat.S_ISDIR(parent_mode):
        raise SocketStartupError(f"ソケットの親がディレクトリではない: {parent}")
    if parent_mode & stat.S_IWOTH:
        # other が書けるディレクトリでは、他人がソケットを消して差し替えられる
        raise SocketStartupError(f"ソケットの親ディレクトリを other が書ける: {parent}")
    if group_gid is not None:
        check_group_can_traverse(parent, group_gid)


def lexical_ancestors(parent: Path) -> tuple[Path, ...]:
    """設定どおりの字面のパスで、親からルートまでのディレクトリ（書き手が通る順の逆）。"""
    absolute = parent.absolute()
    return (absolute, *absolute.parents)


def reject_symlink_components(parent: Path) -> None:
    """親までの途中にシンボリックリンクがあれば起動しない。

    リンク（や多段のリンク）を経由すると、書き手が実際に通るディレクトリが
    字面からは決まらず、たどれるかを確かめきれない。追いかけずに**実体のパスを
    設定させる**。まだ無い段（これから作る段）は対象外。
    """
    for component in lexical_ancestors(parent):
        try:
            mode = os.lstat(component).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise SocketStartupError(
                f"ソケットの親までのパスにシンボリックリンクがある: {component}"
                "（socket.group を指定するときは、リンクを含まない実体のパスを "
                "socket.path に設定する必要がある）"
            )


def check_group_can_traverse(parent: Path, group_gid: int) -> None:
    """`socket.group` の利用者が親ディレクトリまでたどれることを確かめる。

    ルートから親までのどのディレクトリも「グループが一致して g+x」か「o+x」でなければ
    ならない。満たさなければ起動しない（書き手が EACCES になるだけの状態で待ち受けない）。
    途中にリンクが無いことは `reject_symlink_components` で確かめてあるので、
    字面の祖先だけを見れば足りる。
    """
    for directory in lexical_ancestors(parent):
        st = os.lstat(directory)
        if st.st_mode & stat.S_IXOTH:
            continue
        if st.st_gid == group_gid and st.st_mode & stat.S_IXGRP:
            continue
        raise SocketStartupError(
            f"socket.group の利用者がソケットの親までたどれない: {directory}"
            "（このディレクトリのグループを socket.group にして g+x を付けるか、"
            "o+x を付ける必要がある）"
        )


def prepare_path(path: Path, *, service: str) -> None:
    """既存のソケットを確かめる。危ないものは消さずに止まる。

    **`acquire_lock()` を持ってから呼ぶ。** 古いソケットの判定と削除を直列にするため。
    """
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(existing.st_mode):
        # 設定の誤りで任意のファイルを消さない
        raise SocketStartupError(f"ソケットの位置にソケット以外がある: {path}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        # 前回の停止で消し損ねたソケット。誰も待ち受けていないので消してよい。
        # 確かめたものと同じ場合だけ消す（ロックの外から差し替えられても触らない）
        unlink_if_same(path, identity(existing))
        return
    finally:
        probe.close()
    raise SocketStartupError(f"別の {service} が待ち受けている: {path}")


def identity(st: os.stat_result) -> tuple[int, int]:
    """ファイルの同一性（`st_dev`, `st_ino`）。"""
    return (st.st_dev, st.st_ino)


def unlink_if_same(path: Path, bound: tuple[int, int] | None) -> None:
    """`bound` と同じファイルがまだ `path` にある場合だけ消す。

    自分が作っていない（`bound is None`）か、別のものに置き換わっていれば触らない。
    """
    if bound is None:
        return
    with contextlib.suppress(FileNotFoundError):
        if identity(os.lstat(path)) == bound:
            path.unlink()
