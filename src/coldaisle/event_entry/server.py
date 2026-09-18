"""合成の起点: 書き込み専用の Unix ソケット入口 `coldaisle-eventd`（#67 / 決定記録 0045）。

```bash
uv run coldaisle-eventd                      # config/event-entry.yaml のソケットで待ち受ける
uv run coldaisle-eventd --db var/coldaisle.db
```

**取り込みデーモンに組み込まない理由**（0045 §2.1）: 取り込みは圧縮再生で
SimulatedClock になりうるが、イベントの時刻はホストの実時刻で確定する。
シリアルを持つ唯一のプロセスに止まる理由を足さない。ソケットの権限を
別の systemd unit で絞れる。

1接続 = 1行の要求 + 1行の応答。接続は1本ずつ順に処理する（SQLite の接続を
スレッド間で共有しない。0004 §2.8）。遅い接続は `read_timeout_s` で切る。

門は2つ（0045 §2.2 / §2.3）:

1. ファイルの権限（`socket.mode` / `socket.group`。other には開けない）
2. 接続相手の uid（`SO_PEERCRED`）。接続のたびに確かめる
"""

from __future__ import annotations

import argparse
import contextlib
import grp
import json
import logging
import os
import pwd
import signal
import socket
import stat
import struct
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from coldaisle import logs
from coldaisle.clock import Clock, WallClock
from coldaisle.event_entry.config import DEFAULT_CONFIG, EventEntrySettings
from coldaisle.event_entry.messages import MessageError, parse_message
from coldaisle.store import EventRecord, QualityRules, SqliteStore

LOGGER = logging.getLogger("coldaisle.event_entry")

DEFAULT_DB = Path("var/coldaisle.db")
DEFAULT_QUALITY_RULES = Path("config/quality.yaml")

PARENT_DIR_MODE = 0o750
"""親ディレクトリを作るときの権限。other からは辿れない（0045 §2.2）。"""

ACCEPT_POLL_S = 0.5
"""停止要求を確かめる間隔。待ち受けをこの間隔で起こす。"""

_PEERCRED = struct.Struct("3i")
"""`struct ucred { pid_t pid; uid_t uid; gid_t gid; }`（Linux）。"""


class EntryStartupError(RuntimeError):
    """安全に待ち受けられない。fail closed で起動しない（0045 §2.2 / §2.3）。"""


def peer_uid(conn: socket.socket) -> int:
    """接続相手の uid を `SO_PEERCRED` で取る。"""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
    _, uid, _ = _PEERCRED.unpack(raw)
    return int(uid)


@dataclass(frozen=True, slots=True)
class Authorizer:
    """接続相手の uid を認可する（0045 §2.3）。

    root を暗黙に認めない。グループの判定は接続のたびに引き直すので、
    グループから外した利用者はサーバを再起動しなくても書けなくなる。
    """

    server_uid: int
    allow_same_user: bool
    group_gid: int | None

    def allows(self, uid: int) -> bool:
        if self.allow_same_user and uid == self.server_uid:
            return True
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
def _umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _prepare_path(path: Path) -> None:
    """親ディレクトリと既存のソケットを確かめる。危ないものは消さずに止まる。"""
    parent = path.parent
    if not parent.exists():
        with _umask(0o077):
            parent.mkdir(parents=True, mode=PARENT_DIR_MODE)
        os.chmod(parent, PARENT_DIR_MODE)
    parent_mode = os.stat(parent).st_mode
    if not stat.S_ISDIR(parent_mode):
        raise EntryStartupError(f"ソケットの親がディレクトリではない: {parent}")
    if parent_mode & stat.S_IWOTH:
        # other が書けるディレクトリでは、他人がソケットを消して差し替えられる
        raise EntryStartupError(f"ソケットの親ディレクトリを other が書ける: {parent}")

    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(existing.st_mode):
        # 設定の誤りで任意のファイルを消さない
        raise EntryStartupError(f"ソケットの位置にソケット以外がある: {path}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        # 前回の停止で消し損ねたソケット。誰も待ち受けていないので消してよい
        path.unlink(missing_ok=True)
        return
    finally:
        probe.close()
    raise EntryStartupError(f"別の coldaisle-eventd が待ち受けている: {path}")


class EventEntryServer:
    """ソケットで待ち受け、検証済みのイベントを `events` へ追記する。"""

    def __init__(
        self,
        *,
        settings: EventEntrySettings,
        store: SqliteStore,
        clock: Clock,
        server_uid: int | None = None,
    ) -> None:
        if not hasattr(socket, "SO_PEERCRED"):
            raise EntryStartupError(
                "SO_PEERCRED が無いプラットフォームでは起動しない（接続相手を確かめられない）"
            )
        self._settings = settings
        self._store = store
        self._clock = clock
        group_gid: int | None = None
        if settings.socket.group is not None:
            try:
                group_gid = grp.getgrnam(settings.socket.group).gr_gid
            except KeyError as exc:
                raise EntryStartupError(
                    f"socket.group を解決できない: {settings.socket.group}"
                ) from exc
        self._authorizer = Authorizer(
            server_uid=os.geteuid() if server_uid is None else server_uid,
            allow_same_user=settings.authorization.allow_same_user,
            group_gid=group_gid,
        )
        self._path = settings.socket.path
        self._listener: socket.socket | None = None
        self._inode: int | None = None
        self._stop = False
        self.accepted = 0
        self.rejected = 0

    @property
    def path(self) -> Path:
        return self._path

    def bind(self) -> None:
        """ソケットを作り、権限を設定して待ち受けを始める。"""
        _prepare_path(self._path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # 作った瞬間から所有者以外に開かない。権限は直後に設定値へ広げる
            with _umask(0o177):
                listener.bind(str(self._path))
            if self._authorizer.group_gid is not None:
                os.chown(self._path, -1, self._authorizer.group_gid)
            os.chmod(self._path, self._settings.socket.mode_bits)
            listener.listen(16)
            listener.settimeout(ACCEPT_POLL_S)
        except BaseException:
            listener.close()
            self._path.unlink(missing_ok=True)
            raise
        self._listener = listener
        self._inode = os.lstat(self._path).st_ino
        LOGGER.info(
            "書き込み入口で待ち受ける",
            extra={
                logs.FIELDS_KEY: {
                    "socket": str(self._path),
                    "mode": self._settings.socket.mode,
                    "group": self._settings.socket.group,
                }
            },
        )

    def request_stop(self) -> None:
        self._stop = True

    def serve(self, *, max_connections: int | None = None) -> None:
        """停止要求（または `max_connections` 件の処理）まで接続を順に処理する。"""
        if self._listener is None:
            raise RuntimeError("bind() の前に serve() を呼んだ")
        handled = 0
        while not self._stop:
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            with conn:
                self.handle(conn)
            handled += 1
            if max_connections is not None and handled >= max_connections:
                break

    def close(self) -> None:
        """待ち受けを閉じ、**自分が作ったソケットだけ**を消す。"""
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        with contextlib.suppress(FileNotFoundError):
            if self._inode is not None and os.lstat(self._path).st_ino == self._inode:
                self._path.unlink()
        self._inode = None

    def handle(self, conn: socket.socket) -> None:
        """1接続を処理する。**1接続の失敗で入口を落とさない**（ログして継続）。"""
        try:
            uid = peer_uid(conn)
        except OSError:
            LOGGER.warning("接続相手の uid を取れないため拒否した", exc_info=True)
            self._reply(conn, {"ok": False, "error": "unauthorized"})
            self.rejected += 1
            return
        if not self._authorizer.allows(uid):
            LOGGER.warning(
                "認可されていない接続を拒否した", extra={logs.FIELDS_KEY: {"peer_uid": uid}}
            )
            self._reply(conn, {"ok": False, "error": "unauthorized"})
            self.rejected += 1
            return
        try:
            line = self._read_line(conn)
            message = parse_message(line)
        except MessageError as exc:
            LOGGER.warning(
                "メッセージを拒否した",
                extra={logs.FIELDS_KEY: {"peer_uid": uid, "reason": exc.reason}},
            )
            self._reply(conn, {"ok": False, "error": exc.reason})
            self.rejected += 1
            return

        now_ms = self._clock.now_ms()
        try:
            stored = self._store.record_event(
                EventRecord(
                    ts_ms=now_ms,
                    kind=message.kind,
                    payload_json=message.payload_json(),
                    peer_uid=uid,
                ),
                state=message.system_state(),
            )
        except Exception:
            # 保存の失敗は書き手に伝え、入口は止めない。原因はログに残す
            LOGGER.exception(
                "イベントを保存できなかった", extra={logs.FIELDS_KEY: {"peer_uid": uid}}
            )
            self._reply(conn, {"ok": False, "error": "storage failure"})
            self.rejected += 1
            return
        LOGGER.info(
            "イベントを記録した",
            extra={
                logs.FIELDS_KEY: {
                    "event_id": stored.id,
                    "kind": stored.kind,
                    "ts_ms": stored.ts_ms,
                    "peer_uid": uid,
                }
            },
        )
        self._reply(conn, {"ok": True, "event_id": stored.id, "ts_ms": stored.ts_ms})
        self.accepted += 1

    def _read_line(self, conn: socket.socket) -> bytes:
        """1行を読む。上限の超過・期限切れ・途中の切断は拒否する。"""
        limit = self._settings.limits.max_message_bytes
        deadline = time.monotonic() + self._settings.limits.read_timeout_s
        buffer = bytearray()
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MessageError("read timeout")
            conn.settimeout(remaining)
            try:
                chunk = conn.recv(min(4096, limit + 1 - len(buffer)))
            except TimeoutError as exc:
                raise MessageError("read timeout") from exc
            except OSError as exc:
                raise MessageError("connection error") from exc
            if not chunk:
                raise MessageError("message must end with a newline")
            buffer += chunk
            if len(buffer.split(b"\n", 1)[0]) > limit:
                raise MessageError("message too large")
        return bytes(buffer.split(b"\n", 1)[0])

    @staticmethod
    def _reply(conn: socket.socket, body: dict[str, object]) -> None:
        try:
            conn.sendall((json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            # 相手が先に切った。記録（または拒否）は済んでいる
            LOGGER.warning("応答を返せなかった", exc_info=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coldaisle-eventd",
        description="書き込み専用のローカル Unix ソケット入口（#67 / 決定記録 0045）",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--quality-rules", type=Path, default=DEFAULT_QUALITY_RULES)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    install_signals: Callable[[EventEntryServer], None] | None = None,
) -> int:
    """`coldaisle-eventd` の入口。時計は常に実時計（0045 §2.5）。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    settings = EventEntrySettings.from_yaml(args.config)
    clock = WallClock()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteStore(
        args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=clock
    ) as store:
        server = EventEntryServer(settings=settings, store=store, clock=clock)
        (install_signals or _install_signal_handlers)(server)
        server.bind()
        try:
            server.serve()
        finally:
            server.close()
            LOGGER.info(
                "書き込み入口を停止した",
                extra={logs.FIELDS_KEY: {"accepted": server.accepted, "rejected": server.rejected}},
            )
    return 0


def _install_signal_handlers(server: EventEntryServer) -> None:
    def stop(_signum: int, _frame: FrameType | None) -> None:
        server.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
