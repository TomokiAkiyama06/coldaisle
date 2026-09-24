"""書き込み専用のローカル Unix ソケット入口（#67 / 決定記録 0045）。

実機もシステムのパスも使わない。ソケットは一時ディレクトリに作る。
守りたいことは5つ。

1. 読み取り API は GET だけのまま（書き込みはソケットだけ）
2. 受理するのはホワイトリストの種類・形だけで、それ以外は何も残さない
3. ソケットは other に開かず、認可されていない uid を拒否する
4. AI 層・読み取り API・ツール窓口はこの入口を import できない
5. Workload Hint（#107 / 決定記録 0064）は Stage A では記録されるだけで、
   `system_state` にも制御にも届かない
"""

from __future__ import annotations

import ast
import errno
import fcntl
import grp
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coldaisle.api.app import Config, create_app
from coldaisle.clock import SimulatedClock
from coldaisle.event_entry import client as event_client
from coldaisle.event_entry import server as event_server
from coldaisle.event_entry.config import EventEntrySettings
from coldaisle.event_entry.messages import (
    GPU_MODE_STATE_KEY,
    RESERVED_TYPES,
    MessageError,
    WorkloadHintLimits,
    encode_gpu_mode,
    encode_workload_hint,
    parse_message,
)
from coldaisle.event_entry.server import EntryStartupError, EventEntryServer
from coldaisle.store import EventRecord, QualityRules, SqliteStore
from coldaisle.store import migrations as store_migrations
from conftest import CONFIG_DIR, QUALITY_RULES_PATH, TEST_EPOCH_MS

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "coldaisle"
CONFIG_PATH = CONFIG_DIR / "event-entry.yaml"
DECISION = ROOT / "docs" / "decisions" / "0045-local-socket-write-entry.md"
HINT_LIMITS = EventEntrySettings.from_yaml(CONFIG_PATH).hint_limits
"""出荷した設定で決まる Workload Hint の上限。"""

needs_peercred = pytest.mark.skipif(
    not hasattr(socket, "SO_PEERCRED"), reason="SO_PEERCRED が無い（入口は起動しない設計）"
)


# ---------------------------------------------------------------- 下ごしらえ


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """Unix ソケットのパスは 107 バイトまで。pytest の tmp_path は長くなりうる。"""
    directory = Path(tempfile.mkdtemp(prefix="ca-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def settings_for(path: Path, **limits: float) -> EventEntrySettings:
    """本番の設定ファイルを読み、ソケットの場所（と上限）だけを差し替える。"""
    base = EventEntrySettings.from_yaml(CONFIG_PATH)
    return base.model_copy(
        update={
            "socket": base.socket.model_copy(update={"path": path}),
            "limits": base.limits.model_copy(update=limits),
        }
    )


class RunningServer:
    """別スレッドで入口を動かす。SQLite の接続はそのスレッドで開く（0004 §2.8）。"""

    def __init__(
        self, settings: EventEntrySettings, db: Path, rules: QualityRules, **kwargs: int
    ) -> None:
        self.settings = settings
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self.server: EventEntryServer | None = None

        def run() -> None:
            try:
                clock = SimulatedClock(TEST_EPOCH_MS)
                with SqliteStore(db, rules=rules, clock=clock) as store:
                    self.server = EventEntryServer(
                        settings=settings, store=store, clock=clock, **kwargs
                    )
                    self.server.bind()
                    self._ready.set()
                    try:
                        self.server.serve()
                    finally:
                        self.server.close()
            except BaseException as exc:
                self._error = exc
                self._ready.set()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10)
        if self._error is not None:
            raise self._error

    def send(self, line: bytes) -> dict[str, object]:
        return event_client.send(line, self.settings.socket.path, timeout_s=5.0)

    def stop(self) -> None:
        assert self.server is not None
        self.server.request_stop()
        self._thread.join(10)
        assert not self._thread.is_alive()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "events.db"


@pytest.fixture
def running(short_dir: Path, db: Path, rules: QualityRules) -> Iterator[RunningServer]:
    entry = RunningServer(settings_for(short_dir / "run" / "events.sock"), db, rules)
    try:
        yield entry
    finally:
        entry.stop()


def stored_events(db: Path, rules: QualityRules) -> tuple[EventRecord, ...]:
    with SqliteStore(db, rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)) as store:
        return store.events(0, TEST_EPOCH_MS + 1)


def gpu_mode_state_rows(db: Path) -> list[tuple[int, str]]:
    conn = sqlite3.connect(db)
    try:
        return [
            (int(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT ts_ms, value FROM system_state WHERE key = ? ORDER BY ts_ms",
                (GPU_MODE_STATE_KEY,),
            )
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------- 設定


def test_the_shipped_config_is_never_world_accessible():
    settings = EventEntrySettings.from_yaml(CONFIG_PATH)
    assert settings.socket.mode_bits & stat.S_IRWXO == 0
    assert settings.socket.mode == "0660"
    assert not settings.socket.path.is_absolute(), "実行環境の絶対パスを書かない（ルール10）"


@pytest.mark.parametrize("mode", ["0666", "0662", "0661", "0664", "4660", "2660", "1660", "0460"])
def test_dangerous_socket_modes_are_rejected(mode):
    base = EventEntrySettings.from_yaml(CONFIG_PATH).model_dump()
    base["socket"]["mode"] = mode
    with pytest.raises(ValueError):
        EventEntrySettings.model_validate(base)


def test_unquoted_yaml_mode_is_rejected(tmp_path):
    """YAML の `0660` は8進として読まれ、意図と違う値になりうる。文字列だけを受ける。"""
    text = CONFIG_PATH.read_text(encoding="utf-8").replace('mode: "0660"', "mode: 0660")
    path = tmp_path / "event-entry.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        EventEntrySettings.from_yaml(path)


def test_socket_path_longer_than_sun_path_is_rejected():
    base = EventEntrySettings.from_yaml(CONFIG_PATH).model_dump()
    base["socket"]["path"] = "var/" + "x" * 120 + ".sock"
    with pytest.raises(ValueError):
        EventEntrySettings.model_validate(base)


def test_unknown_config_keys_are_rejected():
    base = EventEntrySettings.from_yaml(CONFIG_PATH).model_dump()
    base["socket"]["tcp_port"] = 9000
    with pytest.raises(ValueError):
        EventEntrySettings.model_validate(base)


# ---------------------------------------------------------------- メッセージ


def test_gpu_mode_message_is_accepted():
    message = parse_message(
        b'{"v": 1, "type": "gpu_mode", "mode": "compute", "source": "gm"}\n',
        hint_limits=HINT_LIMITS,
    )
    assert message.kind == "gpu_mode"
    assert message.system_state() == ("sys.gpu_mode", "compute")
    assert json.loads(message.payload_json()) == {
        "v": 1,
        "type": "gpu_mode",
        "mode": "compute",
        "source": "gm",
    }


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"\n",
        b"not json\n",
        b"[1, 2]\n",
        b'"gpu_mode"\n',
        b"\xff\xfe\n",
        b'{"v": 1, "mode": "ai"}\n',
        b'{"v": 1, "type": "drop_table", "mode": "ai"}\n',
        b'{"v": 1, "type": "Gpu_Mode", "mode": "ai"}\n',
        b'{"v": 1, "type": "fan_demand", "front": 1.0}\n',
        b'{"v": 1, "type": "workload_hint", "hint_v": 1, "phase": "start", "hint": "training"}\n',
        b'{"v": 2, "type": "gpu_mode", "mode": "ai"}\n',
        b'{"v": true, "type": "gpu_mode", "mode": "ai"}\n',
        b'{"v": 1.0, "type": "gpu_mode", "mode": "ai"}\n',
        b'{"v": "1", "type": "gpu_mode", "mode": "ai"}\n',
        b'{"v": 1, "type": "gpu_mode", "mode": "shared"}\n',
        b'{"v": 1, "type": "gpu_mode", "mode": "AI"}\n',
        b'{"v": 1, "type": "gpu_mode"}\n',
        b'{"v": 1, "type": "gpu_mode", "mode": "ai", "ts_ms": 0}\n',
        b'{"v": 1, "type": "gpu_mode", "mode": "ai", "source": "Bad Name"}\n',
        b'{"v": 1, "type": "gpu_mode", "mode": "ai", "note": "a\\u001b[31mred"}\n',
        b'{"v": 1, "type": "gpu_mode", "mode": "ai", "note": "line\\nbreak"}\n',
        b'{"v": 1, "type": "gpu_mode", "mode": "ai", "mode": "compute"}\n',
        b'{"v": NaN, "type": "gpu_mode", "mode": "ai"}\n',
    ],
)
def test_malformed_or_unauthorized_messages_are_rejected(line):
    with pytest.raises(MessageError):
        parse_message(line, hint_limits=HINT_LIMITS)


def test_overlong_note_is_rejected():
    with pytest.raises(MessageError):
        encode_gpu_mode("ai", note="x" * 201)


def test_rejection_reason_does_not_reflect_the_input():
    """拒否の理由に書き手の文字列を混ぜない（ログや応答を騙す材料にしない）。"""
    injected = "INJECTED-TEXT"
    with pytest.raises(MessageError) as excinfo:
        parse_message(
            json.dumps({"v": 1, "type": "gpu_mode", "mode": injected, injected: 1}).encode()
            + b"\n",
            hint_limits=HINT_LIMITS,
        )
    assert injected not in excinfo.value.reason
    with pytest.raises(MessageError) as excinfo:
        parse_message(
            json.dumps({"v": 1, "type": "zz" + injected.lower()}).encode() + b"\n",
            hint_limits=HINT_LIMITS,
        )
    assert injected.lower() not in excinfo.value.reason


# ---------------------------------------------------------------- 保存（追記のみ）


def test_events_are_append_only(tmp_path, rules):
    with SqliteStore(tmp_path / "a.db", rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)) as store:
        store.record_event(
            EventRecord(ts_ms=TEST_EPOCH_MS, kind="gpu_mode", payload_json='{"mode":"ai"}')
        )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.connection.execute("UPDATE events SET kind = 'other'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.connection.execute("DELETE FROM events")
        assert len(store.events(0, TEST_EPOCH_MS + 1)) == 1


def test_event_payload_must_be_a_json_object(tmp_path, rules):
    with SqliteStore(tmp_path / "a.db", rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO events (ts_ms, kind, payload) VALUES (0, 'gpu_mode', '[1]')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO events (ts_ms, kind, payload) VALUES (0, 'Bad-Kind', '{}')"
            )


def test_events_query_keeps_the_newest_side_and_filters_kind(tmp_path, rules):
    with SqliteStore(tmp_path / "a.db", rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)) as store:
        for offset in range(5):
            store.record_event(
                EventRecord(ts_ms=TEST_EPOCH_MS + offset, kind="gpu_mode", payload_json="{}")
            )
        store.record_event(EventRecord(ts_ms=TEST_EPOCH_MS + 9, kind="other", payload_json="{}"))
        newest = store.events(TEST_EPOCH_MS, TEST_EPOCH_MS + 10, kinds=["gpu_mode"], limit=2)
        assert [event.ts_ms for event in newest] == [TEST_EPOCH_MS + 3, TEST_EPOCH_MS + 4]
        assert store.events(TEST_EPOCH_MS, TEST_EPOCH_MS + 10, kinds=[]) == ()


def test_the_decision_ddl_is_the_migration():
    """決定記録 0045 §2.5 の SQL と 0006 のマイグレーションを片方だけ直させない。"""
    block = re.search(r"```sql\n(.*?)```", DECISION.read_text(encoding="utf-8"), re.DOTALL)
    assert block is not None
    migration = (store_migrations.MIGRATIONS_DIR / "0006_events.sql").read_text(encoding="utf-8")

    def normalize(sql: str) -> str:
        return re.sub(r"\s+", " ", sql).strip()

    assert normalize(block.group(1)) in normalize(migration)


# ---------------------------------------------------------------- ソケット


@needs_peercred
def test_gpu_mode_event_is_recorded_with_server_time(running, db, rules):
    response = running.send(encode_gpu_mode("compute", source="workspace-gpu-manager"))
    assert response["ok"] is True
    assert response["ts_ms"] == TEST_EPOCH_MS, "時刻はサーバの時計で決まる"

    (event,) = stored_events(db, rules)
    assert event.kind == "gpu_mode"
    assert event.peer_uid == os.geteuid()
    assert json.loads(event.payload_json)["mode"] == "compute"
    assert gpu_mode_state_rows(db) == [(TEST_EPOCH_MS, "compute")]


@needs_peercred
def test_repeated_mode_is_an_event_but_not_a_state_change(running, db, rules):
    assert running.send(encode_gpu_mode("ai"))["ok"] is True
    assert running.send(encode_gpu_mode("ai"))["ok"] is True
    assert len(stored_events(db, rules)) == 2
    assert [value for _, value in gpu_mode_state_rows(db)] == ["ai"]


@needs_peercred
def test_socket_is_created_with_the_configured_mode(running):
    path = running.settings.socket.path
    mode = os.lstat(path).st_mode
    assert stat.S_ISSOCK(mode)
    assert stat.S_IMODE(mode) == 0o660
    assert stat.S_IMODE(os.stat(path.parent).st_mode) & stat.S_IRWXO == 0


@needs_peercred
@pytest.mark.parametrize(
    "line",
    [
        b'{"v": 1, "type": "workload_hint", "hint_v": 1, "phase": "start", "hint": "training"}\n',
        b'{"v": 1, "type": "fan_demand", "front": 1.0}\n',
        b"garbage\n",
    ],
)
def test_rejected_messages_leave_nothing_behind(running, db, rules, line):
    response = running.send(line)
    assert response["ok"] is False
    assert stored_events(db, rules) == ()
    assert gpu_mode_state_rows(db) == []


@needs_peercred
def test_oversized_message_is_rejected(running, db, rules):
    limit = running.settings.limits.max_message_bytes
    response = running.send(b'{"v": 1, "note": "' + b"x" * limit + b'"}\n')
    assert response == {"ok": False, "error": "message too large"}
    assert stored_events(db, rules) == ()


@needs_peercred
def test_slow_or_unterminated_writer_is_cut_off(short_dir, db, rules):
    entry = RunningServer(settings_for(short_dir / "events.sock", read_timeout_s=0.2), db, rules)
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5)
        conn.connect(str(entry.settings.socket.path))
        conn.sendall(b'{"v": 1, "type": "gpu_mode", "mode": "ai"}')  # 改行を送らない
        reply = json.loads(conn.recv(1024).decode())
        conn.close()
        assert reply == {"ok": False, "error": "read timeout"}
        # 詰まった接続のあとも入口は生きている
        assert entry.send(encode_gpu_mode("ai"))["ok"] is True
    finally:
        entry.stop()


@needs_peercred
def test_unauthorized_uid_is_rejected(short_dir, db, rules):
    """サーバと別の uid で、グループも設定していなければ受理しない。"""
    entry = RunningServer(
        settings_for(short_dir / "events.sock"), db, rules, server_uid=os.geteuid() + 4242
    )
    try:
        assert entry.send(encode_gpu_mode("compute")) == {"ok": False, "error": "unauthorized"}
    finally:
        entry.stop()
    assert stored_events(db, rules) == ()


@needs_peercred
def test_same_user_is_rejected_when_not_allowed(short_dir, db, rules):
    base = settings_for(short_dir / "events.sock")
    settings = base.model_copy(
        update={"authorization": base.authorization.model_copy(update={"allow_same_user": False})}
    )
    entry = RunningServer(settings, db, rules)
    try:
        assert entry.send(encode_gpu_mode("ai")) == {"ok": False, "error": "unauthorized"}
    finally:
        entry.stop()


@needs_peercred
def test_peer_credentials_decode_uid_as_unsigned():
    """uid_t / gid_t は符号なし。2**31 以上の uid を負に読まない（#141 のレビュー）。"""
    big_uid = 2**32 - 2
    raw = struct.pack("iII", -1, big_uid, 2**31 + 5)

    class FakeConn:
        def getsockopt(self, level: int, option: int, size: int) -> bytes:
            assert (level, option) == (socket.SOL_SOCKET, socket.SO_PEERCRED)
            assert size == len(raw)
            return raw

    assert event_server.peer_uid(FakeConn()) == big_uid  # type: ignore[arg-type]


def test_group_membership_authorizes_other_users():
    """グループの判定は主グループと補助グループの両方を見る。root を暗黙に認めない。"""
    import grp
    import pwd

    me = pwd.getpwuid(os.geteuid())
    other_uid = os.geteuid() + 4242
    by_primary = event_server.Authorizer(
        server_uid=other_uid, allow_same_user=True, group_gid=me.pw_gid
    )
    assert by_primary.allows(os.geteuid())
    nobody = event_server.Authorizer(server_uid=other_uid, allow_same_user=True, group_gid=None)
    assert not nobody.allows(os.geteuid())
    assert not nobody.allows(0), "root を暗黙に認めない"
    unknown_group = max(group.gr_gid for group in grp.getgrall()) + 1
    assert not event_server.Authorizer(
        server_uid=other_uid, allow_same_user=False, group_gid=unknown_group
    ).allows(os.geteuid())


@needs_peercred
def test_refuses_a_world_writable_parent(short_dir, db, rules):
    parent = short_dir / "open"
    parent.mkdir()
    os.chmod(parent, 0o777)
    with pytest.raises(EntryStartupError, match="other"):
        RunningServer(settings_for(parent / "events.sock"), db, rules)


def settings_with_group(path: Path) -> EventEntrySettings:
    """`socket.group` を自分の主グループにする（自分が属していることが確実なグループ）。"""
    base = settings_for(path)
    group = grp.getgrgid(os.getgid()).gr_name
    return base.model_copy(update={"socket": base.socket.model_copy(update={"group": group})})


@needs_peercred
def test_created_parents_get_the_socket_group(short_dir, db, rules, monkeypatch):
    """作った親ディレクトリは 0750 のまま socket.group にする（#141 のレビュー）。

    主グループのままだと、書き手のグループがたどれず EACCES になる。
    """
    os.chmod(short_dir, 0o711)  # テストの外側は other が通れるだけ
    parent = short_dir / "a" / "b"
    chowned: list[tuple[str, int]] = []
    real_chown = os.chown

    def recording_chown(target: str | os.PathLike[str], uid: int, gid: int) -> None:
        chowned.append((str(target), gid))
        real_chown(target, uid, gid)

    monkeypatch.setattr(event_server.os, "chown", recording_chown)
    entry = RunningServer(settings_with_group(parent / "events.sock"), db, rules)
    try:
        for directory in (short_dir / "a", parent):
            st = os.stat(directory)
            assert stat.S_IMODE(st.st_mode) == 0o750
            assert st.st_gid == os.getgid()
            assert (str(directory), os.getgid()) in chowned
    finally:
        entry.stop()


@needs_peercred
def test_parent_chown_failure_is_reported(short_dir, db, rules, monkeypatch):
    os.chmod(short_dir, 0o711)

    def failing_chown(target: object, uid: int, gid: int) -> None:
        raise PermissionError("not a member")

    monkeypatch.setattr(event_server.os, "chown", failing_chown)
    with pytest.raises(EntryStartupError, match="に変えられない"):
        RunningServer(settings_with_group(short_dir / "new" / "events.sock"), db, rules)


@needs_peercred
def test_existing_parent_must_be_traversable_by_the_group(short_dir, db, rules):
    """既存の親は広げずに確かめ、たどれなければ直し方を示して止まる。"""
    os.chmod(short_dir, 0o711)
    parent = short_dir / "run"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    settings = settings_with_group(parent / "events.sock")
    with pytest.raises(EntryStartupError, match="たどれない") as excinfo:
        RunningServer(settings, db, rules)
    assert "g+x" in str(excinfo.value)
    assert stat.S_IMODE(os.stat(parent).st_mode) == 0o700, "既存の権限を勝手に広げない"
    assert not (parent / "events.sock").exists()

    os.chmod(parent, 0o750)  # グループが一致して g+x
    RunningServer(settings, db, rules).stop()


@needs_peercred
def test_group_traversal_checks_every_ancestor(short_dir, db, rules):
    os.chmod(short_dir, 0o700)  # 親は通れても、その上で止まる
    parent = short_dir / "run"
    parent.mkdir()
    os.chmod(parent, 0o750)
    with pytest.raises(EntryStartupError, match="たどれない"):
        RunningServer(settings_with_group(parent / "events.sock"), db, rules)


@needs_peercred
def test_symlinked_parent_is_rejected_when_a_group_is_set(short_dir, db, rules):
    """socket.group を指定したら、親がリンクなら止まる（#141 のレビュー）。

    多段のリンクを経由すると、書き手が通るディレクトリを確かめきれない。
    リンクを追わず、実体のパスを設定させる。
    """
    os.chmod(short_dir, 0o711)
    real = short_dir / "run"
    real.mkdir()
    os.chmod(real, 0o750)
    link = short_dir / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(EntryStartupError, match="シンボリックリンク") as excinfo:
        RunningServer(settings_with_group(link / "events.sock"), db, rules)
    assert str(link) in str(excinfo.value)
    assert "実体のパス" in str(excinfo.value)
    assert not (real / "events.sock").exists()

    RunningServer(settings_with_group(real / "events.sock"), db, rules).stop()
    # グループを指定しない開発用の設定は、従来どおりリンクを通してよい
    RunningServer(settings_for(link / "events.sock"), db, rules).stop()


@needs_peercred
def test_symlink_in_an_ancestor_is_rejected_before_creating_anything(short_dir, db, rules):
    """途中の祖先がリンクでも止まり、リンク先にディレクトリを作らない。"""
    os.chmod(short_dir, 0o711)
    real = short_dir / "real"
    real.mkdir()
    os.chmod(real, 0o711)
    link = short_dir / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(EntryStartupError, match="シンボリックリンク") as excinfo:
        RunningServer(settings_with_group(link / "new" / "events.sock"), db, rules)
    assert str(link) in str(excinfo.value)
    assert not (real / "new").exists()


@needs_peercred
def test_does_not_delete_a_non_socket_file(short_dir, db, rules):
    victim = short_dir / "events.sock"
    victim.write_text("keep me", encoding="utf-8")
    with pytest.raises(EntryStartupError, match="ソケット以外"):
        RunningServer(settings_for(victim), db, rules)
    assert victim.read_text(encoding="utf-8") == "keep me"


@needs_peercred
def test_refuses_to_start_twice(running, db, rules):
    with pytest.raises(EntryStartupError, match="別の"):
        RunningServer(running.settings, db, rules)


@needs_peercred
def test_losing_a_concurrent_start_leaves_the_winner_socket(running, db, rules, monkeypatch):
    """同時起動で両方が `_prepare_path()` を通り、負けた側が EADDRINUSE になる場合。"""
    path = running.settings.socket.path
    before = os.lstat(path)
    # ロックと古いソケットの確認を両方すり抜けた場合の、最後の防御を確かめる
    monkeypatch.setattr(
        event_server, "_acquire_lock", lambda _path: os.open(os.devnull, os.O_RDONLY)
    )
    monkeypatch.setattr(event_server, "_prepare_path", lambda _path: None)
    with pytest.raises(OSError) as excinfo:
        RunningServer(running.settings, db, rules)
    assert excinfo.value.errno == errno.EADDRINUSE
    after = os.lstat(path)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert running.send(encode_gpu_mode("ai"))["ok"] is True


@needs_peercred
def test_stale_cleanup_waits_for_the_lock(short_dir, db, rules):
    """古いソケットが残る中で2つが同時に起動しても、掃除と bind は1つずつ（#141 のレビュー）。

    ロックを持つ側（ここではテスト）がいる間、後から来た側は古いソケットに触らず止まる。
    """
    path = short_dir / "events.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    before = os.lstat(path)
    holder = os.open(short_dir / "events.sock.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EntryStartupError, match="別の"):
            RunningServer(settings_for(path), db, rules)
        after = os.lstat(path)
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    finally:
        os.close(holder)
    # ロックが空けば、古いソケットを片付けて起動できる
    entry = RunningServer(settings_for(path), db, rules)
    assert entry.send(encode_gpu_mode("ai"))["ok"] is True
    entry.stop()


@needs_peercred
def test_the_lock_is_released_on_close(short_dir, db, rules):
    path = short_dir / "events.sock"
    RunningServer(settings_for(path), db, rules).stop()
    entry = RunningServer(settings_for(path), db, rules)
    assert entry.send(encode_gpu_mode("ai"))["ok"] is True
    entry.stop()
    lock = os.open(short_dir / "events.sock.lock", os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock)


@needs_peercred
def test_failure_after_bind_removes_only_its_own_socket(short_dir, db, rules, monkeypatch):
    path = short_dir / "events.sock"

    def failing_chmod(target: object, mode: int) -> None:
        raise PermissionError("chmod failed")

    monkeypatch.setattr(event_server.os, "chmod", failing_chmod)
    with pytest.raises(PermissionError):
        RunningServer(settings_for(path), db, rules)
    assert not path.exists()


@needs_peercred
def test_stale_socket_is_replaced_and_removed_on_close(short_dir, db, rules):
    path = short_dir / "events.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # 待ち受けていないソケットが残る
    entry = RunningServer(settings_for(path), db, rules)
    assert entry.send(encode_gpu_mode("ai"))["ok"] is True
    entry.stop()
    assert not path.exists()


# ---------------------------------------------------------------- CLI


@needs_peercred
def test_cli_client_records_and_reports(running, capsys, db, rules):
    path = str(running.settings.socket.path)
    assert event_client.main(["--socket", path, "gpu-mode", "compute", "--source", "gm"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert event_client.main(["--socket", path, "gpu-mode", "ai", "--source", "Bad Name"]) == 1
    assert len(stored_events(db, rules)) == 1


def test_cli_client_reports_an_unreachable_entry(short_dir, capsys):
    missing = short_dir / "none.sock"
    assert event_client.main(["--socket", str(missing), "gpu-mode", "ai"]) == 2
    assert json.loads(capsys.readouterr().err)["ok"] is False


@needs_peercred
def test_cli_client_with_socket_works_outside_the_checkout(
    running, capsys, tmp_path, monkeypatch, db, rules
):
    """`--socket` を渡せば設定ファイルを読まない（Workspace はリポジトリの外から呼ぶ）。"""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert not (elsewhere / "config").exists()
    path = str(running.settings.socket.path)
    assert event_client.main(["--socket", path, "--timeout", "3", "gpu-mode", "ai"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert len(stored_events(db, rules)) == 1


def test_cli_client_without_socket_reports_a_missing_config(tmp_path, monkeypatch, capsys):
    """`--socket` が無く設定も無ければ、例外ではなく理由の分かる失敗にする。"""
    monkeypatch.chdir(tmp_path)
    assert event_client.main(["gpu-mode", "ai"]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False
    assert "設定ファイルが無い" in error["error"]
    assert "--socket" in error["error"]


def test_cli_client_reports_an_unreadable_config(tmp_path, capsys):
    broken = tmp_path / "event-entry.yaml"
    broken.write_text("socket: [", encoding="utf-8")
    assert event_client.main(["--config", str(broken), "gpu-mode", "ai"]) == 2
    assert "設定ファイルを読めない" in json.loads(capsys.readouterr().err)["error"]


@needs_peercred
def test_cli_client_reads_the_socket_from_config_when_not_given(running, capsys, tmp_path):
    config = tmp_path / "event-entry.yaml"
    config.write_text(
        CONFIG_PATH.read_text(encoding="utf-8").replace(
            "path: var/run/coldaisle-events.sock", f"path: {running.settings.socket.path}"
        ),
        encoding="utf-8",
    )
    assert event_client.main(["--config", str(config), "gpu-mode", "compute"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_client_timeout_comes_from_flag_then_env_then_default(monkeypatch):
    monkeypatch.delenv(event_client.TIMEOUT_ENV, raising=False)
    assert event_client._timeout(None) == event_client.DEFAULT_TIMEOUT_S
    monkeypatch.setenv(event_client.TIMEOUT_ENV, "1.5")
    assert event_client._timeout(None) == 1.5
    assert event_client._timeout(0.25) == 0.25


@pytest.mark.parametrize(
    ("argv", "env"),
    [
        (["--timeout", "0", "gpu-mode", "ai"], None),
        (["--timeout", "-1", "gpu-mode", "ai"], None),
        (["--timeout", "inf", "gpu-mode", "ai"], None),
        (["gpu-mode", "ai"], "soon"),
        (["gpu-mode", "ai"], "nan"),
    ],
)
def test_cli_client_rejects_bad_timeouts(argv, env, monkeypatch, short_dir):
    if env is None:
        monkeypatch.delenv(event_client.TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(event_client.TIMEOUT_ENV, env)
    with pytest.raises(SystemExit) as excinfo:
        event_client.main(["--socket", str(short_dir / "none.sock"), *argv])
    assert excinfo.value.code == 2


@needs_peercred
def test_server_main_serves_until_stopped(short_dir, tmp_path):
    config = tmp_path / "event-entry.yaml"
    socket_path = short_dir / "main.sock"
    config.write_text(
        CONFIG_PATH.read_text(encoding="utf-8").replace(
            "path: var/run/coldaisle-events.sock", f"path: {socket_path}"
        ),
        encoding="utf-8",
    )
    started = threading.Event()
    holder: list[EventEntryServer] = []

    def install(server: EventEntryServer) -> None:
        holder.append(server)
        started.set()

    db_path = tmp_path / "main.db"
    thread = threading.Thread(
        target=event_server.main,
        args=(
            [
                "--config",
                str(config),
                "--db",
                str(db_path),
                "--quality-rules",
                str(QUALITY_RULES_PATH),
            ],
        ),
        kwargs={"install_signals": install},
        daemon=True,
    )
    thread.start()
    assert started.wait(10)
    for _ in range(100):
        if socket_path.exists():
            break
        threading.Event().wait(0.05)
    response = event_client.send(encode_gpu_mode("ai"), socket_path, timeout_s=5)
    assert response["ok"] is True
    holder[0].request_stop()
    thread.join(10)
    assert not thread.is_alive()
    assert not socket_path.exists()


# ---------------------------------------------------------------- 読み取り API


@pytest.fixture
def api(tmp_path, rules):
    path = tmp_path / "api.db"
    with SqliteStore(path, rules=rules, clock=SimulatedClock(TEST_EPOCH_MS)) as store:
        message = parse_message(encode_gpu_mode("compute", source="gm"), hint_limits=HINT_LIMITS)
        store.record_event(
            EventRecord(
                ts_ms=TEST_EPOCH_MS - 60_000,
                kind=message.kind,
                payload_json=message.payload_json(),
                peer_uid=1234,
            ),
            state=message.system_state(),
        )
    app = create_app(
        Config(db=path, quality_rules=QUALITY_RULES_PATH, metrics=CONFIG_DIR / "metrics.yaml"),
        clock=SimulatedClock(TEST_EPOCH_MS),
    )
    with TestClient(app) as opened:
        yield opened


def test_events_are_readable_for_the_timeline(api):
    body = api.get("/api/v1/events", params={"window": "1h", "kind": "gpu_mode"}).json()
    assert body["truncated"] is False
    (event,) = body["events"]
    assert event["ts_ms"] == TEST_EPOCH_MS - 60_000
    assert event["payload"] == {"v": 1, "type": "gpu_mode", "mode": "compute", "source": "gm"}
    assert "peer_uid" not in event, "監査用の uid は読み取り API に出さない"


def test_events_endpoint_validates_its_parameters(api):
    assert api.get("/api/v1/events", params={"window": "1h", "kind": "Bad-Kind"}).status_code == 422
    assert api.get("/api/v1/events").status_code == 422


def test_recorded_gpu_mode_reaches_server_health(api):
    assert api.get("/api/v1/server-health").json()["gpu"]["mode"] == "compute"


def test_read_api_is_still_get_only(api):
    """書き込みの入口を足しても、読み取り API に書き込み系は現れない（0009 §3）。"""
    paths = api.get("/openapi.json").json()["paths"]
    assert {method for operations in paths.values() for method in operations} == {"get"}
    assert api.post("/api/v1/events", json={"v": 1}).status_code == 405


# ---------------------------------------------------------------- 構造（AI から届かない）


ENTRY_PACKAGE = "coldaisle.event_entry"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _modules_that_must_not_reach_the_entry() -> list[Path]:
    paths = [SRC / "server.py"]
    for package in ("ai", "api", "control", "rules", "notify"):
        paths.extend(sorted((SRC / package).rglob("*.py")))
    return paths


def test_ai_api_and_tool_window_do_not_import_the_write_entry():
    """AI 層・読み取り API・ツール窓口・制御は書き込み入口を import しない（0045 §2.7）。"""
    offenders = [
        f"{path.relative_to(SRC)}: {name}"
        for path in _modules_that_must_not_reach_the_entry()
        for name in _imports(path)
        if name == ENTRY_PACKAGE or name.startswith(ENTRY_PACKAGE + ".")
    ]
    assert offenders == []


def test_the_import_detector_catches_a_real_import(tmp_path):
    """検査そのものの検査。素通しの検査は緑のまま何も守らない。"""
    snippet = tmp_path / "bad.py"
    snippet.write_text(
        "from coldaisle.event_entry.client import send\nimport coldaisle.event_entry\n",
        encoding="utf-8",
    )
    hits = [name for name in _imports(snippet) if name.startswith(ENTRY_PACKAGE)]
    assert ENTRY_PACKAGE in hits and f"{ENTRY_PACKAGE}.client.send" in hits


def test_the_write_entry_does_not_reach_control_or_config_writers():
    """入口から Fan Demand・制御・設定の書き込みへ至る import を持たない（0045 §2.6）。"""
    forbidden = (
        "coldaisle.control",
        "coldaisle.ai",
        "coldaisle.api",
        "coldaisle.server",
        "coldaisle.calibrate",
        "coldaisle.memory",
        "coldaisle.daemon",
        "coldaisle.safety_handoff",
        "subprocess",
    )
    offenders = [
        f"{path.name}: {name}"
        for path in sorted((SRC / "event_entry").rglob("*.py"))
        for name in _imports(path)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]
    assert offenders == []


def test_importing_the_tool_window_does_not_load_the_write_entry(tmp_path):
    """`coldaisle.server`（AI ツールの窓口）を読み込んでも入口のモジュールは載らない。"""
    code = (
        "import sys\n"
        "import coldaisle.server\n"
        "import coldaisle.ai\n"
        f"loaded = sorted(m for m in sys.modules if m.startswith({ENTRY_PACKAGE!r}))\n"
        "assert not loaded, loaded\n"
    )
    env = {**os.environ, "COLDAISLE_DB": str(tmp_path / "server.db")}
    for key in [name for name in env if name.startswith("COLDAISLE_AI")]:
        env.pop(key)
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr


def test_no_ai_tool_can_send_events():
    """AI 向けツールの定義に書き込み・イベント送信が無い（0045 §2.7）。"""
    from coldaisle.ai.tools import DEFINITIONS

    text = json.dumps(DEFINITIONS, ensure_ascii=False).lower()
    for word in ("event", "gpu_mode", "socket", "write", "record"):
        assert word not in text, word


# ---------------------------------------------------------------- Workload Hint（#107 / 0064）
#
# Stage A（0064 §2.10）: 入口は受理して `events` に残すだけ。`system_state` にも制御にも届かない。

HINT_START = {
    "v": 1,
    "type": "workload_hint",
    "hint_v": 1,
    "phase": "start",
    "workload": "training",
}
HINT_END = {"v": 1, "type": "workload_hint", "hint_v": 1, "phase": "end"}


def hint_line(body: dict[str, object], **changes: object) -> bytes:
    """``body`` を ``changes`` で上書きした1行。値が ``None`` のキーは消す。"""
    merged = {**body, **changes}
    return (json.dumps({k: v for k, v in merged.items() if v is not None}) + "\n").encode()


def all_state_rows(db: Path) -> list[tuple[int, str, str]]:
    conn = sqlite3.connect(db)
    try:
        return [
            (int(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                "SELECT ts_ms, key, value FROM system_state ORDER BY ts_ms, key"
            )
        ]
    finally:
        conn.close()


def test_workload_hint_is_no_longer_reserved():
    assert "workload_hint" not in RESERVED_TYPES


def test_shipped_hint_config_matches_the_decision():
    """既定で受理する `hint_v` は `[1]` だけ（0064 §2.3）。"""
    assert HINT_LIMITS.accepted_hint_versions == frozenset({1})
    assert HINT_LIMITS.max_expected_duration_s >= 1


def test_workload_hint_start_is_accepted_and_keeps_hint_v():
    line = hint_line(
        HINT_START,
        expected_duration_s=14400,
        source="workspace-job-launcher",
        note="nightly run",
    )
    message = parse_message(line, hint_limits=HINT_LIMITS)
    assert message.kind == "workload_hint"
    assert json.loads(message.payload_json()) == {
        "v": 1,
        "type": "workload_hint",
        "hint_v": 1,
        "phase": "start",
        "workload": "training",
        "expected_duration_s": 14400,
        "source": "workspace-job-launcher",
        "note": "nightly run",
    }


def test_workload_hint_end_is_accepted_with_the_minimal_shape():
    message = parse_message(hint_line(HINT_END), hint_limits=HINT_LIMITS)
    assert json.loads(message.payload_json()) == HINT_END


@pytest.mark.parametrize("workload", ["training", "benchmark", "inference_service"])
def test_every_workload_in_the_closed_set_is_accepted(workload):
    message = parse_message(hint_line(HINT_START, workload=workload), hint_limits=HINT_LIMITS)
    assert json.loads(message.payload_json())["workload"] == workload


@pytest.mark.parametrize(
    "body",
    [HINT_START, HINT_END, {**HINT_START, "expected_duration_s": 60, "note": "x"}],
    ids=["start", "end", "full"],
)
def test_workload_hint_never_writes_system_state(body):
    """ヒントは真値ではない。「いまの値」を1箇所に置かない（0064 §2.4）。"""
    message = parse_message(hint_line(body), hint_limits=HINT_LIMITS)
    assert message.system_state() is None


def test_duration_up_to_the_configured_limit_is_accepted():
    limit = HINT_LIMITS.max_expected_duration_s
    line = hint_line(HINT_START, expected_duration_s=limit)
    assert parse_message(line, hint_limits=HINT_LIMITS).kind == "workload_hint"
    with pytest.raises(MessageError) as excinfo:
        parse_message(hint_line(HINT_START, expected_duration_s=limit + 1), hint_limits=HINT_LIMITS)
    assert excinfo.value.reason == "expected_duration_s is too long"


def test_the_duration_limit_comes_from_the_limits_not_the_code():
    tight = WorkloadHintLimits(accepted_hint_versions=frozenset({1}), max_expected_duration_s=60)
    parse_message(hint_line(HINT_START, expected_duration_s=60), hint_limits=tight)
    with pytest.raises(MessageError):
        parse_message(hint_line(HINT_START, expected_duration_s=61), hint_limits=tight)


def test_hint_version_outside_the_configured_set_is_rejected():
    """形を知っている版でも、設定で受理していなければ拒否する（空 = 1件も受理しない）。"""
    closed = WorkloadHintLimits(accepted_hint_versions=frozenset(), max_expected_duration_s=60)
    with pytest.raises(MessageError) as excinfo:
        parse_message(hint_line(HINT_START), hint_limits=closed)
    assert excinfo.value.reason == "unsupported hint version"
    with pytest.raises(MessageError):
        parse_message(hint_line(HINT_END), hint_limits=closed)
    # 同じ上限でも GPU Mode は巻き込まない（封筒の版と本体の版は別。0064 §2.3）
    assert parse_message(encode_gpu_mode("ai"), hint_limits=closed).kind == "gpu_mode"


def test_configuring_an_unimplemented_hint_version_does_not_widen_the_shape():
    """上限に未知の版が紛れても、形を知らない版は通らない。"""
    wide = WorkloadHintLimits(accepted_hint_versions=frozenset({1, 2}), max_expected_duration_s=60)
    with pytest.raises(MessageError):
        parse_message(hint_line(HINT_START, hint_v=2), hint_limits=wide)


@pytest.mark.parametrize(
    "changes",
    [
        # 版
        {"hint_v": None},
        {"hint_v": 0},
        {"hint_v": 2},
        {"hint_v": "1"},
        {"hint_v": True},
        {"hint_v": 1.0},
        {"v": 2},
        # phase と workload の組み合わせ
        {"phase": None},
        {"phase": "stop"},
        {"phase": "START"},
        {"workload": None},
        {"phase": "end", "workload": "training"},
        {"phase": "end", "workload": None, "expected_duration_s": 60},
        # 閉じた語彙の外（冷却を弱める向きの申告を含む。0064 §2.8）
        {"workload": "idle"},
        {"workload": "idle_planned"},
        {"workload": "cooldown"},
        {"workload": "Training"},
        {"workload": ""},
        {"workload": 1},
        # 期間
        {"expected_duration_s": 0},
        {"expected_duration_s": -1},
        {"expected_duration_s": 14400.0},
        {"expected_duration_s": "4h"},
        {"expected_duration_s": True},
        {"expected_duration_s": 10**30},
        # 時刻は書き手に渡させない（0045 §2.5）
        {"ts": 0},
        {"ts_ms": 0},
        {"expires_at": 0},
        # 識別子（0064 §2.3 / AGENTS.md ルール10）
        {"job_id": "job-1"},
        {"user": "someone"},
        {"host": "node"},
        {"pid": 1234},
        {"path": "/srv/job"},
        # 読み手のいない進捗（0064 §2.3 未決 #2）
        {"progress": 0.5},
        {"step": 10},
        {"epoch": 1},
        # 0045 と同じ source / note の規則
        {"source": "Bad Name"},
        {"source": ""},
        {"note": "line\nbreak"},
        {"note": "a\u001b[31mred"},
        {"note": "x" * 201},
        # 指令のような未知のフィールド
        {"demand": 1.0},
        {"mode": "compute"},
    ],
)
def test_malformed_workload_hints_are_rejected(changes):
    with pytest.raises(MessageError):
        parse_message(hint_line(HINT_START, **changes), hint_limits=HINT_LIMITS)


@pytest.mark.parametrize(
    "line",
    [
        b'{"v": 1, "type": "idle_planned", "hint_v": 1, "phase": "start"}\n',
        b'{"v": 1, "type": "workload_idle", "hint_v": 1, "phase": "start"}\n',
        b'{"v": 1, "type": "Workload_Hint", "hint_v": 1, "phase": "end"}\n',
        b'{"v": 1, "type": "workload_hint", "hint_v": 1, "phase": "end", "phase": "start",'
        b' "workload": "training"}\n',
    ],
)
def test_hint_lookalikes_are_rejected(line):
    with pytest.raises(MessageError):
        parse_message(line, hint_limits=HINT_LIMITS)


def test_hint_rejection_reason_does_not_reflect_the_input():
    injected = "INJECTED-TEXT"
    for changes in ({"workload": injected}, {injected: 1}, {"note": injected + "\n"}):
        with pytest.raises(MessageError) as excinfo:
            parse_message(hint_line(HINT_START, **changes), hint_limits=HINT_LIMITS)
        assert injected not in excinfo.value.reason


def test_encode_workload_hint_checks_the_shape_before_sending():
    assert json.loads(encode_workload_hint("end")) == HINT_END
    line = encode_workload_hint("start", workload="benchmark", expected_duration_s=90)
    assert json.loads(line) == {**HINT_START, "workload": "benchmark", "expected_duration_s": 90}
    with pytest.raises(MessageError):
        encode_workload_hint("start")
    with pytest.raises(MessageError):
        encode_workload_hint("end", workload="training")
    with pytest.raises(MessageError):
        encode_workload_hint("end", expected_duration_s=60)
    with pytest.raises(MessageError):
        encode_workload_hint("start", workload="idle")
    with pytest.raises(MessageError):
        encode_workload_hint("start", workload="training", expected_duration_s=0)


@pytest.mark.parametrize("versions", [[2], [1, 1], [0, 1]])
def test_hint_config_rejects_versions_the_code_does_not_know(versions):
    base = EventEntrySettings.from_yaml(CONFIG_PATH).model_dump()
    base["workload_hint"]["accepted_hint_versions"] = versions
    with pytest.raises(ValueError):
        EventEntrySettings.model_validate(base)


def test_hint_config_may_accept_nothing():
    """空は「1件も受理しない」。拒否する側なので起動を妨げない。"""
    base = EventEntrySettings.from_yaml(CONFIG_PATH).model_dump()
    base["workload_hint"]["accepted_hint_versions"] = []
    assert EventEntrySettings.model_validate(base).hint_limits.accepted_hint_versions == frozenset()


@pytest.mark.parametrize("value", [0, -1])
def test_hint_duration_limit_must_be_positive(value):
    base = EventEntrySettings.from_yaml(CONFIG_PATH).model_dump()
    base["limits"]["max_expected_duration_s"] = value
    with pytest.raises(ValueError):
        EventEntrySettings.model_validate(base)


def test_hint_duration_limit_is_owned_by_the_config_not_by_a_code_ceiling():
    """上限は設定の値だけが決める。コードの天井で、設定した長い上限を拒まない。"""
    base = EventEntrySettings.from_yaml(CONFIG_PATH).model_dump()
    base["limits"]["max_expected_duration_s"] = 90 * 24 * 3600
    settings = EventEntrySettings.model_validate(base)
    assert settings.hint_limits.max_expected_duration_s == 90 * 24 * 3600


def test_hint_config_is_required():
    """ヒントの受理条件を書き忘れた設定では起動しない（黙って既定に倒さない）。"""
    base = EventEntrySettings.from_yaml(CONFIG_PATH).model_dump()
    del base["workload_hint"]
    with pytest.raises(ValueError):
        EventEntrySettings.model_validate(base)
    base = EventEntrySettings.from_yaml(CONFIG_PATH).model_dump()
    del base["limits"]["max_expected_duration_s"]
    with pytest.raises(ValueError):
        EventEntrySettings.model_validate(base)


@needs_peercred
def test_workload_hint_is_recorded_as_an_event_only(running, db, rules):
    """`events` に行が残り、`system_state` は1行も増えない（0064 §2.4）。"""
    start = running.send(encode_workload_hint("start", workload="training", source="launcher"))
    assert start["ok"] is True
    assert start["ts_ms"] == TEST_EPOCH_MS, "時刻はサーバの時計で決まる"
    assert running.send(encode_workload_hint("end"))["ok"] is True

    first, second = stored_events(db, rules)
    assert (first.kind, second.kind) == ("workload_hint", "workload_hint")
    assert first.peer_uid == os.geteuid()
    assert json.loads(first.payload_json) == {**HINT_START, "source": "launcher"}
    assert json.loads(second.payload_json) == HINT_END
    assert all_state_rows(db) == []


@needs_peercred
def test_workload_hint_does_not_touch_the_gpu_mode_state(running, db, rules):
    assert running.send(encode_gpu_mode("compute"))["ok"] is True
    before = all_state_rows(db)
    assert running.send(encode_workload_hint("start", workload="inference_service"))["ok"] is True
    assert all_state_rows(db) == before == [(TEST_EPOCH_MS, GPU_MODE_STATE_KEY, "compute")]


@needs_peercred
def test_the_server_enforces_the_configured_duration_limit(short_dir, db, rules):
    """クライアントは上限を知らなくても送れる。上限を決めるのはサーバの設定。"""
    settings = settings_for(short_dir / "events.sock", max_expected_duration_s=3600)
    entry = RunningServer(settings, db, rules)
    try:
        too_long = encode_workload_hint("start", workload="training", expected_duration_s=3601)
        assert entry.send(too_long) == {"ok": False, "error": "expected_duration_s is too long"}
        assert stored_events(db, rules) == ()
        at_limit = encode_workload_hint("start", workload="training", expected_duration_s=3600)
        assert entry.send(at_limit)["ok"] is True
    finally:
        entry.stop()


@needs_peercred
def test_a_server_that_accepts_no_hint_version_still_accepts_gpu_mode(short_dir, db, rules):
    base = settings_for(short_dir / "events.sock")
    hint = base.workload_hint.model_copy(update={"accepted_hint_versions": ()})
    entry = RunningServer(base.model_copy(update={"workload_hint": hint}), db, rules)
    try:
        assert entry.send(encode_workload_hint("end")) == {
            "ok": False,
            "error": "unsupported hint version",
        }
        assert entry.send(encode_gpu_mode("ai"))["ok"] is True
        assert [event.kind for event in stored_events(db, rules)] == ["gpu_mode"]
    finally:
        entry.stop()


@needs_peercred
def test_rejected_hints_leave_nothing_behind(running, db, rules):
    for changes in ({"workload": "idle"}, {"ts": 0}, {"job_id": "x"}, {"hint_v": 2}):
        assert running.send(hint_line(HINT_START, **changes))["ok"] is False
    assert stored_events(db, rules) == ()
    assert all_state_rows(db) == []


@needs_peercred
def test_cli_client_sends_workload_hints(running, capsys, db, rules):
    argv = ["--socket", str(running.settings.socket.path), "workload-hint"]
    assert event_client.main([*argv, "training", "--expected-duration", "4h"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert event_client.main([*argv, "benchmark", "--source", "bench", "--note", "sweep"]) == 0
    assert event_client.main([*argv, "end"]) == 0
    # 取り消しに期間は付けられない。送る前に落ち、何も残らない
    assert event_client.main([*argv, "end", "--expected-duration", "1h"]) == 1
    assert event_client.main([*argv, "training", "--expected-duration", "0"]) == 1

    payloads = [json.loads(event.payload_json) for event in stored_events(db, rules)]
    assert payloads == [
        {**HINT_START, "expected_duration_s": 14400},
        {**HINT_START, "workload": "benchmark", "source": "bench", "note": "sweep"},
        HINT_END,
    ]
    assert all_state_rows(db) == []


@pytest.mark.parametrize(
    "extra",
    [
        ["idle"],
        ["idle_planned"],
        ["training", "--expected-duration", "4 hours"],
        ["training", "--expected-duration", "-1h"],
        ["training", "--expected-duration", "1.5h"],
        ["training", "--ts", "0"],
        ["training", "--job-id", "x"],
    ],
)
def test_cli_client_refuses_unknown_hint_arguments(extra, short_dir):
    with pytest.raises(SystemExit) as excinfo:
        event_client.main(["--socket", str(short_dir / "none.sock"), "workload-hint", *extra])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90s", 90), ("30m", 1800), ("4h", 14400), ("2d", 172800), ("14400", 14400)],
)
def test_cli_duration_units(text, seconds):
    args = event_client.build_parser().parse_args(
        ["workload-hint", "training", "--expected-duration", text]
    )
    assert args.expected_duration == seconds


HINT_NAME = re.compile(r"workload[_-]?hint", re.IGNORECASE)


def test_control_does_not_read_workload_hints_in_stage_a():
    """Stage A では制御は1ビットも変わらない（0064 §2.10）。名前すら出てこない。"""
    sources = [SRC / "control_daemon.py", *sorted((SRC / "control").rglob("*.py"))]
    offenders = [
        str(path.relative_to(SRC))
        for path in sources
        if HINT_NAME.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_control_does_not_open_the_event_store():
    """`coldaisle.control` は DB を開かない。`events` を読む経路が無い（0064 §2.5）。"""
    offenders = [
        f"{path.relative_to(SRC)}: {name}"
        for path in sorted((SRC / "control").rglob("*.py"))
        for name in _imports(path)
        if name in ("coldaisle.store.db", "coldaisle.store.SqliteStore", "sqlite3")
        or name.startswith("coldaisle.store.db.")
    ]
    assert offenders == []


def test_only_the_entry_config_mentions_workload_hints():
    """Stage B の設定（`supervisor.workload_hint` 等）はまだ無い（0064 §2.10）。"""
    mentioned = sorted(
        path.name
        for path in CONFIG_DIR.rglob("*")
        if path.is_file() and HINT_NAME.search(path.read_text(encoding="utf-8", errors="ignore"))
    )
    assert mentioned == ["event-entry.yaml"]
