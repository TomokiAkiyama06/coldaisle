"""書き込み専用のローカル Unix ソケット入口（#67 / 決定記録 0045）。

実機もシステムのパスも使わない。ソケットは一時ディレクトリに作る。
守りたいことは4つ。

1. 読み取り API は GET だけのまま（書き込みはソケットだけ）
2. 受理するのはホワイトリストの種類・形だけで、それ以外は何も残さない
3. ソケットは other に開かず、認可されていない uid を拒否する
4. AI 層・読み取り API・ツール窓口はこの入口を import できない
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
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
    MessageError,
    encode_gpu_mode,
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
    message = parse_message(b'{"v": 1, "type": "gpu_mode", "mode": "compute", "source": "gm"}\n')
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
        b'{"v": 1, "type": "workload_hint", "hint": "training"}\n',
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
        parse_message(line)


def test_overlong_note_is_rejected():
    with pytest.raises(MessageError):
        encode_gpu_mode("ai", note="x" * 201)


def test_rejection_reason_does_not_reflect_the_input():
    """拒否の理由に書き手の文字列を混ぜない（ログや応答を騙す材料にしない）。"""
    injected = "INJECTED-TEXT"
    with pytest.raises(MessageError) as excinfo:
        parse_message(
            json.dumps({"v": 1, "type": "gpu_mode", "mode": injected, injected: 1}).encode() + b"\n"
        )
    assert injected not in excinfo.value.reason
    with pytest.raises(MessageError) as excinfo:
        parse_message(json.dumps({"v": 1, "type": "zz" + injected.lower()}).encode() + b"\n")
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
        b'{"v": 1, "type": "workload_hint", "hint": "training"}\n',
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
        message = parse_message(encode_gpu_mode("compute", source="gm"))
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
