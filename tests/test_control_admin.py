"""`coldaisle-fand` の管理ソケット（#74 / 決定記録 0072 §2.10 段階 1）。

1. 設定（§2.8）: 入口の形だけを持ち、`safety.yaml` の周期・締め切りと起動時に照合する
2. プロトコル（§2.3）: ホワイトリスト。`raise_authority` は無く、段階 1 では降格も受けない
3. 受け渡し口（§2.2）: モードの枠は `command_id` が最大の1件。loop は待たない
4. loop（§2.2 / §2.4）: 次の tick の先頭で入る。`MANUAL` の lease は単調時計だけで数える。
   受付スレッドが死んだら `MANUAL` を解除して `forced_max`、**再起動まで保つ**
5. 受付スレッド（§2.2 / §2.7）: 多重化・受信中の追い出し・受信後の枠の向き分け・監査の順番
6. 構造（§2.9）: LLM・読み取り API・`coldaisle-eventd` から到達できない
"""

from __future__ import annotations

import ast
import errno
import grp
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coldaisle.clock import (
    ManualMonotonicClock,
    MonotonicClock,
    SimulatedClock,
    SystemMonotonicClock,
)
from coldaisle.control.operating_mode import (
    MANUAL_COMMAND_REASON,
    AdminModeCommand,
    AdminModeTracker,
    ModeOutcome,
    ModeOutcomeKind,
    ModeStatus,
)
from coldaisle.control.schema import (
    AuthorityStage,
    ControlTick,
    ModeCommandRecord,
    OperatingMode,
    PerZone,
    Zone,
)
from coldaisle.control_admin import client as admin_client
from coldaisle.control_admin import runtime as admin_runtime
from coldaisle.control_admin.audit import AuditWriter
from coldaisle.control_admin.config import ControlAdminConfigError, ControlAdminSettings
from coldaisle.control_admin.mailbox import AdminMailbox, Placed, Superseded
from coldaisle.control_admin.messages import (
    RequestError,
    SetModeRequest,
    StatusRequest,
    encode,
    parse_request,
)
from coldaisle.control_admin.server import ControlAdminServer
from coldaisle.metrics import MetricCatalog
from coldaisle.store import ControlAdminAuditRecord, QualityRules, SqliteStore
from conftest import CONFIG_DIR, QUALITY_RULES_PATH, TEST_EPOCH_MS
from test_control_loop import METRICS_PATH, Harness

SRC = Path(__file__).resolve().parents[1] / "src" / "coldaisle"
ADMIN_CONFIG = CONFIG_DIR / "control-admin.yaml"
"""`--admin-config` の既定。同じ uid を認めない（0072 §2.5 / §2.8）。"""
DEV_ADMIN_CONFIG = CONFIG_DIR / "control-admin.dev.yaml"
"""開発用に明示して渡す設定（`group: null` + `allow_same_user: true`）。

試験の入口はこれを基にする。
"""
RUN_ID = "0123456789abcdef0123456789abcdef"
TICK_MS = 1_000
TICK_DEADLINE_MS = 100
"""`test_control_loop.control_config()` の `safety.yaml` と同じ周期と締め切り。"""

needs_peercred = pytest.mark.skipif(
    not hasattr(socket, "SO_PEERCRED"), reason="SO_PEERCRED が無い（入口は開かない設計）"
)


@pytest.fixture(scope="session")
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(METRICS_PATH)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """Unix ソケットのパスは 107 バイトまで。pytest の tmp_path は長くなりうる。"""
    directory = Path(tempfile.mkdtemp(prefix="cadm-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def rules() -> QualityRules:
    return QualityRules.from_yaml(QUALITY_RULES_PATH)


def admin_document(path: Path | None = None, **changes: Any) -> dict[str, Any]:
    """開発用の設定ファイルを読み、指定した値だけを差し替える（`limits.x` のように書く）。

    試験は同じ uid のクライアントから接続するため、`--admin-config` で明示する開発用の設定を使う。
    """
    document: dict[str, Any] = yaml.safe_load(DEV_ADMIN_CONFIG.read_text(encoding="utf-8"))
    if path is not None:
        document["socket"]["path"] = str(path)
    for dotted, value in changes.items():
        cursor = document
        *parents, leaf = dotted.split("__")
        for key in parents:
            cursor = cursor[key]
        cursor[leaf] = value
    return document


def admin_settings(path: Path | None = None, **changes: Any) -> ControlAdminSettings:
    return ControlAdminSettings.model_validate(admin_document(path, **changes))


def wait_until(condition: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("条件が時間内に成り立たなかった")
        time.sleep(0.005)


def manual_command(command_id: int, *, lease_ms: int = 60_000, demand: float = 0.4) -> Any:
    return AdminModeCommand(
        command_id=command_id,
        mode=OperatingMode.MANUAL,
        requested=PerZone[float](front=demand, rear=demand, top=demand),
        lease_ms=lease_ms,
    )


# ================================================================ 1. 設定（0072 §2.8）


@pytest.mark.parametrize("path", [ADMIN_CONFIG, DEV_ADMIN_CONFIG])
def test_the_repository_config_is_valid_and_fits_the_test_safety_timing(path):
    settings = ControlAdminSettings.from_yaml(path)
    settings.check_against(tick_ms=TICK_MS, tick_deadline_ms=TICK_DEADLINE_MS)
    assert settings.manual.max_lease_s.status == "provisional"
    assert settings.manual.max_lease_s.basis
    assert settings.accept_backoff.status == "provisional"
    assert settings.accept_backoff.basis


def test_the_default_admin_config_does_not_admit_the_same_uid():
    """既定で読む設定は同じ uid を認めない。

    API / AI 層が fand と同じ uid で動いていても manual / max を送れない（0072 §2.5）。
    """
    from coldaisle.control_daemon import DEFAULT_ADMIN_CONFIG
    from coldaisle.local_socket import Authorizer

    assert CONFIG_DIR / DEFAULT_ADMIN_CONFIG.name == ADMIN_CONFIG
    settings = ControlAdminSettings.from_yaml(ADMIN_CONFIG)
    assert settings.authorization.allow_same_user is False
    assert settings.socket.group is not None
    # 同じ uid でグループのメンバーでない接続（存在しない gid = どのグループのメンバーでもない）
    unused_gid = max(entry.gr_gid for entry in grp.getgrall()) + 1
    authorizer = Authorizer(
        server_uid=os.geteuid(),
        allow_same_user=settings.authorization.allow_same_user,
        group_gid=unused_gid,
    )
    assert not authorizer.allows(os.geteuid())


def test_the_dev_config_differs_from_the_default_only_in_who_may_connect():
    """開発用の設定は `socket.group` と `allow_same_user` だけが違う（上限などを別に育てない）。"""
    default = yaml.safe_load(ADMIN_CONFIG.read_text(encoding="utf-8"))
    dev = yaml.safe_load(DEV_ADMIN_CONFIG.read_text(encoding="utf-8"))
    assert dev["socket"]["group"] is None
    assert dev["authorization"]["allow_same_user"] is True
    for document in (default, dev):
        del document["socket"]["group"]
        del document["authorization"]["allow_same_user"]
    assert default == dev


def test_a_read_timeout_longer_than_a_tick_is_refused_at_startup():
    """受信中の接続が1 tick を超えて枠を占めない（`read_timeout_s * 1000 <= tick_ms`）。"""
    settings = admin_settings(limits__read_timeout_s=1.5)
    with pytest.raises(ControlAdminConfigError, match="read_timeout_s"):
        settings.check_against(tick_ms=TICK_MS, tick_deadline_ms=TICK_DEADLINE_MS)
    admin_settings(limits__read_timeout_s=1.0).check_against(
        tick_ms=TICK_MS, tick_deadline_ms=TICK_DEADLINE_MS
    )


def test_an_ack_timeout_shorter_than_one_tick_and_its_deadline_is_refused():
    settings = admin_settings(apply_ack_timeout_ms=TICK_MS + TICK_DEADLINE_MS - 1)
    with pytest.raises(ControlAdminConfigError, match="apply_ack_timeout_ms"):
        settings.check_against(tick_ms=TICK_MS, tick_deadline_ms=TICK_DEADLINE_MS)
    admin_settings(apply_ack_timeout_ms=TICK_MS + TICK_DEADLINE_MS).check_against(
        tick_ms=TICK_MS, tick_deadline_ms=TICK_DEADLINE_MS
    )


def test_same_user_is_not_allowed_by_default_in_production():
    """本番（group あり）では同じ uid を認めない。group: null は allow_same_user: true だけ。"""
    with pytest.raises(ValidationError, match="allow_same_user"):
        admin_settings(socket__group="coldaisle-admin")
    admin_settings(socket__group="coldaisle-admin", authorization__allow_same_user=False)
    with pytest.raises(ValidationError, match="group: null"):
        admin_settings(authorization__allow_same_user=False)


@pytest.mark.parametrize("mode", ["0666", "0662", "4660", "0460"])
def test_a_world_accessible_socket_mode_is_refused(mode):
    with pytest.raises(ValidationError):
        admin_settings(socket__mode=mode)


def test_an_accept_backoff_longer_than_a_tick_is_refused_at_startup():
    """休んでいる間に届いた新しい接続（`MAX` を含む）を 1 tick を超えて待たせない。"""
    settings = admin_settings(accept_backoff__max_ms=TICK_MS + 1)
    with pytest.raises(ControlAdminConfigError, match=r"accept_backoff\.max_ms"):
        settings.check_against(tick_ms=TICK_MS, tick_deadline_ms=TICK_DEADLINE_MS)
    admin_settings(accept_backoff__max_ms=TICK_MS).check_against(
        tick_ms=TICK_MS, tick_deadline_ms=TICK_DEADLINE_MS
    )


def test_an_accept_backoff_whose_max_is_below_its_initial_is_refused():
    with pytest.raises(ValidationError, match="max_ms"):
        admin_settings(accept_backoff__initial_ms=200, accept_backoff__max_ms=100)
    with pytest.raises(ValidationError):
        admin_settings(accept_backoff__initial_ms=0)


def test_a_v1_config_is_refused_with_a_version_message(short_dir, rules, caplog):
    """v2 で `accept_backoff` を必須にした。v1 は補わずに拒否し、入口を開かない。"""
    document = admin_document(short_dir / "run" / "admin.sock", version=1)
    del document["accept_backoff"]
    with pytest.raises(ValidationError, match=r"version.*expected=2"):
        ControlAdminSettings.model_validate(document)
    config = short_dir / "control-admin.yaml"
    config.write_text(yaml.safe_dump(document), "utf-8")
    assert _open(config, short_dir / "a.db", rules) is None
    assert not (short_dir / "run" / "admin.sock").exists()
    assert any(
        "expected=2" in getattr(record, "fields", {}).get("reason", "") for record in caplog.records
    )


@pytest.mark.parametrize(
    "multiplier", [1, 1.0, 0.5, 0, -2, 16.5, 1e308, float("inf"), float("-inf"), float("nan")]
)
def test_an_accept_backoff_multiplier_must_grow_and_be_finite_and_bounded(multiplier):
    with pytest.raises(ValidationError, match="multiplier"):
        admin_settings(accept_backoff__multiplier=multiplier)
    admin_settings(accept_backoff__multiplier=1.5)
    admin_settings(accept_backoff__multiplier=16)


@pytest.mark.parametrize("literal", [".inf", ".nan", "-.inf"])
def test_a_non_finite_multiplier_in_the_yaml_is_refused_at_load(short_dir, literal):
    document = yaml.safe_dump(admin_document())
    document = document.replace("multiplier: 2", f"multiplier: {literal}")
    assert f"multiplier: {literal}" in document
    config = short_dir / "control-admin.yaml"
    config.write_text(document, "utf-8")
    with pytest.raises(ValidationError, match="multiplier"):
        ControlAdminSettings.from_yaml(config)


def test_the_escalation_must_come_after_the_longest_backoff():
    """`escalate_after_ms > max_ms`。休みの上限より先に `MAX` へ上げない。"""
    with pytest.raises(ValidationError, match="escalate_after_ms"):
        admin_settings(accept_backoff__max_ms=1_000, accept_backoff__escalate_after_ms=1_000)
    with pytest.raises(ValidationError):
        admin_settings(accept_backoff__escalate_after_ms=0)
    admin_settings(accept_backoff__max_ms=1_000, accept_backoff__escalate_after_ms=1_001)


@pytest.mark.parametrize(
    "missing",
    [
        "apply_ack_timeout_ms",
        "manual",
        "limits__max_pending_commands",
        "accept_backoff",
        "accept_backoff__max_ms",
        "accept_backoff__multiplier",
        "accept_backoff__escalate_after_ms",
    ],
)
def test_values_have_no_defaults_in_code(missing):
    """値はすべて設定に置き、コードに既定値を置かない（AGENTS.md ルール9）。"""
    document = admin_document()
    cursor = document
    *parents, leaf = missing.split("__")
    for key in parents:
        cursor = cursor[key]
    del cursor[leaf]
    with pytest.raises(ValidationError):
        ControlAdminSettings.model_validate(document)


def test_the_control_config_files_do_not_carry_the_admin_entry():
    """入口の形は制御の4ファイルに入れない（0072 §2.8。safety.yaml の承認点に触れない）。"""
    from coldaisle.control.config import SafetyConfig

    assert "apply_ack_timeout_ms" not in SafetyConfig.model_fields
    assert "admin" not in " ".join(SafetyConfig.model_fields)


# ================================================================ 2. プロトコル（0072 §2.3）


def _line(body: dict[str, Any]) -> bytes:
    return encode(body)


def test_the_three_modes_parse():
    maximum = parse_request(_line({"v": 1, "op": "set_mode", "mode": "max", "reason": "試験"}))
    assert isinstance(maximum, SetModeRequest) and not maximum.weakens_cooling
    auto = parse_request(_line({"v": 1, "op": "set_mode", "mode": "auto", "reason": "戻す"}))
    assert isinstance(auto, SetModeRequest) and auto.weakens_cooling
    manual = parse_request(
        _line(
            {
                "v": 1,
                "op": "set_mode",
                "mode": "manual",
                "requested": {"front": 0.6, "rear": 0.5, "top": 1},
                "lease_s": 1800,
                "reason": "騒音の比較",
            }
        )
    )
    assert isinstance(manual, SetModeRequest) and manual.weakens_cooling
    assert isinstance(parse_request(_line({"v": 1, "op": "status"})), StatusRequest)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"v": 1, "op": "raise_authority", "reason": "x"}, "unknown_op"),
        ({"v": 1, "type": "gpu_mode", "mode": "ai"}, "unknown_op"),
        ({"v": 1, "op": "lower_authority", "to_stage": "limited", "reason": "x"}, "unsupported_op"),
        ({"v": 1, "op": "rollback_authority", "reason": "x"}, "unsupported_op"),
        ({"v": 1, "op": "set_mode", "mode": "calibration", "reason": "x"}, "unsupported_mode"),
        ({"v": 2, "op": "status"}, "unsupported_version"),
        ({"v": True, "op": "status"}, "unsupported_version"),
        ({"v": 1, "op": "set_mode", "mode": "max"}, "invalid_fields"),
        ({"v": 1, "op": "set_mode", "mode": "max", "reason": ""}, "invalid_fields"),
        ({"v": 1, "op": "set_mode", "mode": "max", "reason": "a\nb"}, "invalid_fields"),
        ({"v": 1, "op": "set_mode", "mode": "max", "reason": "x" * 201}, "invalid_fields"),
        ({"v": 1, "op": "set_mode", "mode": "max", "reason": "x", "lease_s": 60}, "invalid_fields"),
        (
            {"v": 1, "op": "set_mode", "mode": "manual", "reason": "x", "lease_s": 60},
            "invalid_fields",
        ),
        (
            {
                "v": 1,
                "op": "set_mode",
                "mode": "manual",
                "reason": "x",
                "requested": {"front": 0.5, "rear": 0.5, "top": 0.5},
            },
            "invalid_fields",
        ),
        (
            {
                "v": 1,
                "op": "set_mode",
                "mode": "manual",
                "reason": "x",
                "lease_s": 60,
                "requested": {"front": 0.5, "rear": 0.5},
            },
            "invalid_fields",
        ),
        (
            {
                "v": 1,
                "op": "set_mode",
                "mode": "manual",
                "reason": "x",
                "lease_s": 60,
                "requested": {"front": 0.5, "rear": 0.5, "top": 1.2},
            },
            "invalid_fields",
        ),
        (
            {
                "v": 1,
                "op": "set_mode",
                "mode": "manual",
                "reason": "x",
                "lease_s": 60,
                "requested": {"front": 0.5, "rear": 0.5, "top": 0.5, "pwm": 128},
            },
            "invalid_fields",
        ),
        ({"v": 1, "op": "set_mode", "mode": "max", "reason": "x", "ts": 1}, "invalid_fields"),
        ({"v": 1, "op": "set_mode", "mode": "max", "reason": "x", "actor": "a"}, "invalid_fields"),
        ({"v": 1, "op": "status", "path": "/x"}, "invalid_fields"),
    ],
)
def test_requests_outside_the_whitelist_are_refused(body, code):
    """時刻・操作者名・設定値・path・PWM を受け取らない。昇格の操作は存在しない。"""
    with pytest.raises(RequestError) as excinfo:
        parse_request(_line(body))
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"\n", "empty_message"),
        (b"\xff\n", "not_utf8"),
        (b"{\n", "not_json"),
        (b"[]\n", "not_an_object"),
        (b'{"v": 1, "v": 1, "op": "status"}\n', "duplicate_keys"),
        (
            b'{"v": 1, "op": "set_mode", "mode": "manual", "reason": "x", "lease_s": 1,'
            b' "requested": {"front": NaN, "rear": 0, "top": 0}}\n',
            "non_finite_number",
        ),
        # hash できない型・数値を mode / op / requested に入れても TypeError にしない
        (b'{"v": 1, "op": "set_mode", "mode": [], "reason": "x"}\n', "invalid_fields"),
        (b'{"v": 1, "op": "set_mode", "mode": {}, "reason": "x"}\n', "invalid_fields"),
        (b'{"v": 1, "op": "set_mode", "mode": 1, "reason": "x"}\n', "invalid_fields"),
        (b'{"v": 1, "op": [], "mode": "max", "reason": "x"}\n', "unknown_op"),
        (b'{"v": 1, "op": {}, "mode": "max", "reason": "x"}\n', "unknown_op"),
        (b'{"v": 1, "op": 1, "mode": "max", "reason": "x"}\n', "unknown_op"),
        (
            b'{"v": 1, "op": "set_mode", "mode": "manual", "reason": "x", "lease_s": 1,'
            b' "requested": []}\n',
            "invalid_fields",
        ),
        (
            b'{"v": 1, "op": "set_mode", "mode": "manual", "reason": "x", "lease_s": 1,'
            b' "requested": {"front": [], "rear": {}, "top": 0}}\n',
            "invalid_fields",
        ),
    ],
)
def test_malformed_lines_are_refused_without_reflecting_the_input(raw, code):
    with pytest.raises(RequestError) as excinfo:
        parse_request(raw)
    assert excinfo.value.code == code
    assert str(excinfo.value) == code


# ================================================================ 3. 受け渡し口（0072 §2.2）


def test_the_mode_slot_keeps_the_largest_command_id_even_after_the_loop_took_it():
    """先に届いた弱めうる指令が、後から先に置かれた MAX を上書きしない。"""
    mailbox = AdminMailbox()
    try:
        maximum = AdminModeCommand(command_id=2, mode=OperatingMode.MAX)
        assert mailbox.place_mode(maximum) == Placed(replaced=None)
        assert mailbox.take_mode() == maximum
        late = AdminModeCommand(command_id=1, mode=OperatingMode.AUTO)
        assert mailbox.place_mode(late) == Superseded(by=2)
        assert mailbox.take_mode() is None
        newer = AdminModeCommand(command_id=3, mode=OperatingMode.AUTO)
        assert mailbox.place_mode(newer) == Placed(replaced=None)
        newest = AdminModeCommand(command_id=4, mode=OperatingMode.MAX)
        assert mailbox.place_mode(newest) == Placed(replaced=newer)
        assert mailbox.take_mode() == newest
    finally:
        mailbox.close()


def test_the_loop_never_waits_for_the_mailbox_lock():
    mailbox = AdminMailbox()
    try:
        mailbox.place_mode(AdminModeCommand(command_id=1, mode=OperatingMode.MAX))
        with mailbox._lock:  # 受付スレッドが置いている最中
            started = time.monotonic()
            assert mailbox.take_mode() is None
            assert time.monotonic() - started < 0.1
        assert mailbox.take_mode() is not None
    finally:
        mailbox.close()


def test_a_mailbox_without_a_started_receiver_is_not_alive():
    """生きていると確かめられない受付は、死んだ側として扱う（安全側）。"""
    mailbox = AdminMailbox()
    try:
        assert mailbox.receiver_alive() is False
        done = threading.Event()
        thread = threading.Thread(target=done.wait)
        mailbox.attach_receiver(thread)
        thread.start()
        assert mailbox.receiver_alive() is True
        done.set()
        thread.join()
        assert mailbox.receiver_alive() is False
        mailbox.mark_stopping()
        assert mailbox.receiver_alive() is True, "停止の手順は死んだとは扱わない"
    finally:
        mailbox.close()


def test_calibration_and_unleased_manual_cannot_be_placed():
    with pytest.raises(ValidationError):
        AdminModeCommand(command_id=1, mode=OperatingMode.CALIBRATION)
    with pytest.raises(ValidationError):
        AdminModeCommand(
            command_id=1,
            mode=OperatingMode.MANUAL,
            requested=PerZone[float](front=0.5, rear=0.5, top=0.5),
        )
    with pytest.raises(ValidationError):
        AdminModeCommand(command_id=1, mode=OperatingMode.MAX, lease_ms=1_000)


# ================================================================ 4. loop（0072 §2.2 / §2.4）


class FakeMailbox:
    """受付スレッドの代わり。loop が使う面だけを持つ。"""

    def __init__(self) -> None:
        self.alive = True
        self.pending: AdminModeCommand | None = None
        self.locked = False
        self.outcomes: list[ModeOutcome] = []
        self.status: ModeStatus | None = None

    def receiver_alive(self) -> bool:
        return self.alive

    def take_mode(self) -> AdminModeCommand | None:
        if self.locked:
            return None
        taken, self.pending = self.pending, None
        return taken

    def report(self, outcome: ModeOutcome) -> None:
        self.outcomes.append(outcome)

    def publish(self, status: ModeStatus) -> None:
        self.status = status


def admin_harness(catalog: MetricCatalog) -> tuple[Harness, FakeMailbox]:
    mailbox = FakeMailbox()
    harness = Harness(catalog, admin_mode=AdminModeTracker(mailbox, run_id=RUN_ID))
    harness.settle()
    return harness, mailbox


def test_a_command_takes_effect_at_the_start_of_the_next_tick(catalog):
    harness, mailbox = admin_harness(catalog)
    mailbox.pending = manual_command(1, demand=0.9)

    result = harness.tick()

    tick = result.tick
    assert tick.state.operating_mode is OperatingMode.MANUAL
    assert tick.mode_command == ModeCommandRecord(
        entry="control_admin", run_id=RUN_ID, command_id=1
    )
    for zone in Zone:
        record = tick.zones.get(zone)
        assert record.controller_reason.code == MANUAL_COMMAND_REASON
        assert record.demand.requested == pytest.approx(0.9)
    assert mailbox.outcomes == [ModeOutcome(ModeOutcomeKind.APPLIED, 1, tick.tick_id)]
    assert mailbox.status is not None and mailbox.status.command_id == 1
    assert tick.schema_version == 12
    assert ControlTick.model_validate_json(tick.model_dump_json()) == tick


def test_manual_cannot_go_below_the_safety_floor(catalog):
    """`MANUAL` は requested までしか作れない。Safety の floor は掛かる（0072 §2.4）。"""
    harness, mailbox = admin_harness(catalog)
    mailbox.pending = manual_command(1, demand=0.0)

    tick = harness.tick().tick

    for zone in Zone:
        demand = tick.zones.get(zone).demand
        assert demand.requested == 0.0
        assert demand.effective >= demand.safety_floor > 0.0


def test_max_is_forced_max_and_has_no_lease(catalog):
    harness, mailbox = admin_harness(catalog)
    mailbox.pending = AdminModeCommand(command_id=1, mode=OperatingMode.MAX)

    results = [harness.tick() for _ in range(5)]
    harness.monotonic.advance_ms(10 * 24 * 3_600_000)
    results.append(harness.tick())

    for result in results:
        assert result.tick.state.operating_mode is OperatingMode.MAX
        assert all(result.tick.zones.get(zone).demand.forced_max for zone in Zone)
    assert results[-1].tick.mode_command is not None
    assert results[-1].tick.mode_command.command_id == 1


def test_the_manual_lease_is_counted_on_the_monotonic_clock_only(catalog):
    """壁時計が戻っても進んでも、期限は単調時計だけで決まる（0072 §2.4）。"""
    harness, mailbox = admin_harness(catalog)
    mailbox.pending = manual_command(7, lease_ms=3 * TICK_MS)
    applied = harness.tick().tick

    # 壁時計が大きく進んでも期限は来ない
    harness.clock.advance_to_ms(harness.clock.now_ms() + 3_600_000)
    second = harness.tick().tick
    assert second.state.operating_mode is OperatingMode.MANUAL
    third = harness.tick().tick
    assert third.state.operating_mode is OperatingMode.MANUAL
    assert mailbox.status is not None
    assert mailbox.status.lease_deadline_mono_ms == (
        harness.monotonic.monotonic_ms() - 2 * TICK_MS + 3 * TICK_MS
    )

    expired = harness.tick().tick
    assert expired.state.operating_mode is OperatingMode.AUTO
    assert expired.mode_command == ModeCommandRecord(
        entry="control_admin", run_id=RUN_ID, manual_lease_expired_command_id=7
    )
    assert ModeOutcome(ModeOutcomeKind.LEASE_EXPIRED, 7, expired.tick_id) in mailbox.outcomes
    assert applied.tick_id < expired.tick_id
    after = harness.tick().tick
    assert after.mode_command == ModeCommandRecord(entry="control_admin", run_id=RUN_ID)


def test_a_new_command_before_the_lease_ends_does_not_record_an_expiry(catalog):
    harness, mailbox = admin_harness(catalog)
    mailbox.pending = manual_command(1, lease_ms=2 * TICK_MS)
    harness.tick()
    mailbox.pending = AdminModeCommand(command_id=2, mode=OperatingMode.AUTO)
    tick = harness.tick().tick
    assert tick.mode_command is not None
    assert tick.mode_command.command_id == 2
    assert tick.mode_command.manual_lease_expired_command_id is None
    harness.run(3)
    assert all(o.kind is ModeOutcomeKind.APPLIED for o in mailbox.outcomes)


def test_a_busy_mailbox_keeps_the_previous_mode(catalog):
    harness, mailbox = admin_harness(catalog)
    mailbox.pending = AdminModeCommand(command_id=1, mode=OperatingMode.MAX)
    harness.tick()
    mailbox.pending = AdminModeCommand(command_id=2, mode=OperatingMode.AUTO)
    mailbox.locked = True
    assert harness.tick().tick.state.operating_mode is OperatingMode.MAX
    mailbox.locked = False
    assert harness.tick().tick.state.operating_mode is OperatingMode.AUTO


def test_a_dead_receiver_releases_manual_and_forces_max_until_restart(catalog, caplog):
    """受付スレッドが死んだら、次の tick から `MANUAL` を解除して `forced_max`（0072 §2.2）。"""
    harness, mailbox = admin_harness(catalog)
    mailbox.pending = manual_command(1, demand=0.3, lease_ms=3_600_000)
    assert harness.tick().tick.state.operating_mode is OperatingMode.MANUAL

    mailbox.alive = False
    dead = harness.tick().tick

    assert dead.state.operating_mode is OperatingMode.MAX
    assert dead.mode_command == ModeCommandRecord(
        entry="control_admin", run_id=RUN_ID, admin_receiver_dead=True
    )
    for zone in Zone:
        assert dead.zones.get(zone).demand.forced_max is True
        assert dead.zones.get(zone).demand.effective == 1.0
    assert any("admin_receiver_dead" in str(record.__dict__) for record in caplog.records)

    # 生き返ったように見えても、残った指令が置かれても、再起動まで Max のまま
    mailbox.alive = True
    mailbox.pending = AdminModeCommand(command_id=2, mode=OperatingMode.AUTO)
    for result in harness.run(10):
        assert result.tick.state.operating_mode is OperatingMode.MAX
        assert result.tick.mode_command is not None
        assert result.tick.mode_command.admin_receiver_dead is True
    assert mailbox.pending is not None, "死んだあとは受け渡し口を読まない"
    assert [o.command_id for o in mailbox.outcomes] == [1]


def test_a_failing_liveness_check_is_treated_as_death(catalog):
    harness, mailbox = admin_harness(catalog)

    def broken() -> bool:
        raise RuntimeError("確かめられない")

    mailbox.receiver_alive = broken  # type: ignore[method-assign]
    assert harness.tick().tick.state.operating_mode is OperatingMode.MAX


def test_a_loop_without_an_entry_is_not_treated_as_dead(catalog):
    """入口を最初から開かなかった構成は `AUTO`（受付スレッドの死とは扱わない。0072 §2.2）。"""
    harness = Harness(catalog)
    tick = harness.settle().tick
    assert tick.state.operating_mode is OperatingMode.AUTO
    assert tick.mode_command == ModeCommandRecord.without_entry()


def test_one_mode_source_only(catalog):
    """モードの出どころは1つ。2つあると、どちらが人の最新の意図か決まらない。"""
    from coldaisle.control.loop import ControlLoop, StaticOperatingMode

    harness = Harness(catalog, admin_mode=AdminModeTracker(FakeMailbox(), run_id=RUN_ID))
    loop = harness.loop
    with pytest.raises(ValueError, match="同時に配線しない"):
        ControlLoop(
            config=loop._config,
            estimator=loop._estimator,
            fallback=loop._fallback,
            gate=loop._gate,
            guard=loop._guard,
            safety=loop._safety,
            composer=loop._composer,
            backend=loop._backend,
            telemetry=loop._telemetry,
            clock=loop._clock,
            monotonic=loop._monotonic,
            registry=loop._registry,
            mode_source=StaticOperatingMode(),
            admin_mode=AdminModeTracker(FakeMailbox(), run_id=RUN_ID),
        )


@pytest.mark.parametrize(
    ("record", "mode", "message"),
    [
        ({"admin_receiver_dead": True}, "auto", "MAX にする"),
        ({"manual_lease_expired_command_id": 3}, "manual", "AUTO へ戻す"),
        ({}, "manual", "指令に由来しない"),
    ],
)
def test_the_trace_cannot_disagree_with_the_mode(record, mode, message):
    body = json.loads(
        (Path(__file__).parent / "fixtures" / "control_tick_v12.json").read_text("utf-8")
    )
    body["mode_command"] = {"schema_version": 1, "entry": "control_admin", "run_id": RUN_ID}
    body["mode_command"].update(record)
    body["state"]["operating_mode"] = mode
    if mode == "manual":
        body["state"]["active_controller"] = None
        body["state"]["fallback_active"] = False
    with pytest.raises(ValidationError, match=message):
        ControlTick.model_validate_json(json.dumps(body))


def test_a_v11_tick_cannot_carry_a_mode_command():
    body = json.loads(
        (Path(__file__).parent / "fixtures" / "control_tick_v12.json").read_text("utf-8")
    )
    body["schema_version"] = 11
    with pytest.raises(ValidationError, match="schema version 12"):
        ControlTick.model_validate_json(json.dumps(body))
    del body["mode_command"]
    ControlTick.model_validate_json(json.dumps(body))
    body["schema_version"] = 12
    with pytest.raises(ValidationError, match="mode_command が要る"):
        ControlTick.model_validate_json(json.dumps(body))


# ============================================================ 5. 受付スレッド（0072 §2.2 / §2.7）


class GatedSink:
    """監査の DB の代わり。`gate` が開くまで書き込みを待たせ、`fail` なら失敗させる。

    書き込みに入ったら `writing` を立てる。監査書き込みスレッドが依頼を queue から取り出した
    ことを、試験が時間ではなくこの合図で待てるようにする。
    """

    def __init__(
        self,
        db: Path,
        rules: QualityRules,
        gate: threading.Event,
        writing: threading.Event,
        fail: bool,
    ) -> None:
        self._store = SqliteStore(db, rules=rules, clock=SimulatedClock(TEST_EPOCH_MS))
        self._gate = gate
        self._writing = writing
        self._fail = fail

    def record_control_admin_audit(
        self, record: ControlAdminAuditRecord
    ) -> ControlAdminAuditRecord:
        self._writing.set()
        self._gate.wait(timeout=10)
        if self._fail:
            raise sqlite3.OperationalError("database is locked")
        return self._store.record_control_admin_audit(record)

    def close(self) -> None:
        self._store.close()


class RunningEntry:
    """管理ソケットを1つ開き、loop の代わりに `tracker.resolve()` を試験から呼ぶ。"""

    def __init__(
        self,
        directory: Path,
        rules: QualityRules,
        *,
        server_uid: int | None = None,
        audit_fails: bool = False,
        monotonic: MonotonicClock | None = None,
        **changes: Any,
    ) -> None:
        changes.setdefault("apply_ack_timeout_ms", 2_000)
        self.settings = admin_settings(directory / "run" / "admin.sock", **changes)
        self.db = directory / "audit.db"
        self.gate = threading.Event()
        self.gate.set()
        self.writing = threading.Event()
        """監査書き込みスレッドが1件目の依頼を取り出して書き込みに入った。"""
        self.mailbox = AdminMailbox()
        self.audit = AuditWriter(
            open_sink=lambda: GatedSink(self.db, rules, self.gate, self.writing, audit_fails),
            queue_max=self.settings.limits.audit_queue_max,
            on_done=self.mailbox.wake,
        )
        self.server = ControlAdminServer(
            settings=self.settings,
            mailbox=self.mailbox,
            audit=self.audit,
            clock=SimulatedClock(TEST_EPOCH_MS),
            monotonic=SystemMonotonicClock() if monotonic is None else monotonic,
            run_id=RUN_ID,
            authority_ceiling=AuthorityStage.SHADOW,
            server_uid=server_uid,
        )
        self.server.bind()
        self.audit.start()
        self.server.start()
        self.tracker = AdminModeTracker(self.mailbox, run_id=RUN_ID)
        self.tick_id = 0
        self.rules = rules
        self.pool = ThreadPoolExecutor(max_workers=8)

    @property
    def path(self) -> Path:
        return self.settings.socket.path

    def send(self, body: dict[str, Any], *, timeout_s: float = 5.0) -> dict[str, Any]:
        return admin_client.send(encode(body), self.path, timeout_s=timeout_s)

    def send_async(self, body: dict[str, Any]) -> Future[dict[str, Any]]:
        return self.pool.submit(self.send, body)

    def tick(self) -> Any:
        """loop の tick の先頭の代わり。"""
        self.tick_id += 1
        resolution = self.tracker.resolve(
            tick_id=self.tick_id, now_mono_ms=SystemMonotonicClock().monotonic_ms()
        )
        self.tracker.publish(tick_id=self.tick_id, authority_stage=AuthorityStage.SHADOW)
        return resolution

    def tick_until(self, mode: OperatingMode) -> Any:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            resolution = self.tick()
            if resolution.command.mode is mode:
                return resolution
            time.sleep(0.005)
        raise AssertionError(f"{mode} が受け渡し口に届かなかった")

    def rows(self) -> list[tuple[str, int, int | None, int | None]]:
        with SqliteStore(self.db, rules=self.rules, clock=SimulatedClock(0)) as store:
            return [
                (row.event, row.command_id, row.tick_id, row.superseded_by)
                for row in store.control_admin_audit(RUN_ID)
            ]

    def stop(self) -> None:
        self.gate.set()
        self.mailbox.mark_stopping()
        self.server.request_stop()
        self.server.join(timeout_s=5)
        self.audit.stop(timeout_s=5)
        self.server.close()
        self.mailbox.close()
        self.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def entry(short_dir: Path, rules: QualityRules) -> Iterator[RunningEntry]:
    running = RunningEntry(short_dir, rules)
    try:
        yield running
    finally:
        running.stop()


MAX = {"v": 1, "op": "set_mode", "mode": "max", "reason": "負荷試験の前に全開"}
AUTO = {"v": 1, "op": "set_mode", "mode": "auto", "reason": "比較の終了"}


def manual_body(lease_s: int = 60) -> dict[str, Any]:
    return {
        "v": 1,
        "op": "set_mode",
        "mode": "manual",
        "requested": {"front": 0.6, "rear": 0.5, "top": 0.7},
        "lease_s": lease_s,
        "reason": "騒音の比較",
    }


@needs_peercred
@pytest.mark.parametrize(
    "raw",
    [
        b'{"v": 1, "op": "set_mode", "mode": [], "reason": "x"}\n',
        b'{"v": 1, "op": "set_mode", "mode": {}, "reason": "x"}\n',
        b'{"v": 1, "op": [], "reason": "x"}\n',
    ],
)
def test_a_malformed_mode_is_refused_and_the_receiver_stays_alive(entry, raw):
    """形の壊れた1行で受付スレッドを死なせない（死ぬと再起動まで全 zone が MAX になる）。"""
    response = admin_client.send(raw, entry.path, timeout_s=5)
    assert response["ok"] is False
    assert response["error"] in {"invalid_fields", "unknown_op"}
    assert entry.mailbox.receiver_alive()
    assert entry.send({"v": 1, "op": "status"})["ok"] is True


class FailingListener:
    def __init__(self) -> None:
        self.calls = 0

    def accept(self) -> tuple[Any, Any]:
        self.calls += 1
        raise OSError(errno.EMFILE, "Too many open files")


class OnceListener:
    """1回だけ接続を返し、あとは待ち行列が空（`BlockingIOError`）。"""

    def __init__(self, sock: socket.socket) -> None:
        self._sock: socket.socket | None = sock

    def accept(self) -> tuple[Any, Any]:
        sock, self._sock = self._sock, None
        if sock is None:
            raise BlockingIOError
        return sock, None


def idle_server(monotonic: ManualMonotonicClock, **changes: Any) -> ControlAdminServer:
    """bind も start もしない受付（selector を持たない）。休みの長さとログだけを見る。"""
    settings = admin_settings(Path("/nonexistent/admin.sock"), **changes)
    mailbox = AdminMailbox()
    return ControlAdminServer(
        settings=settings,
        mailbox=mailbox,
        audit=AuditWriter(
            open_sink=lambda: pytest.fail("監査は開かない"),
            queue_max=settings.limits.audit_queue_max,
            on_done=mailbox.wake,
        ),
        clock=SimulatedClock(TEST_EPOCH_MS),
        monotonic=monotonic,
        run_id=RUN_ID,
        authority_ceiling=AuthorityStage.SHADOW,
    )


@needs_peercred
def test_repeated_accept_failures_back_off_on_the_monotonic_clock_and_log_sparsely(caplog):
    """失敗のたびに休みを倍にし、`max_ms` で頭打ちにする。ログは伸びたときと 2 の冪だけ。"""
    monotonic = ManualMonotonicClock(10_000)
    server = idle_server(monotonic, accept_backoff__initial_ms=100, accept_backoff__max_ms=1_000)
    listener = FailingListener()
    caplog.set_level("INFO", logger="coldaisle.control_admin")
    backoffs: list[int] = []
    for _ in range(12):
        server._accept(listener)  # type: ignore[arg-type]
        assert server._accept_backoff_ms is not None
        backoffs.append(server._accept_backoff_ms)
        resume = server._accept_resume_mono_ms
        assert resume == monotonic.monotonic_ms() + server._accept_backoff_ms
        # 期限の前には戻さず、期限で戻す（sleep しない。loop の周回で単調時計を見るだけ）
        monotonic.advance_ms(server._accept_backoff_ms - 1)
        server._resume_accept_if_due()
        assert server._accept_resume_mono_ms == resume
        monotonic.advance_ms(1)
        server._resume_accept_if_due()
        assert server._accept_resume_mono_ms is None
    assert listener.calls == 12, "1回の失敗で打ち切り、同じ周回で accept を繰り返さない"
    assert backoffs == [100, 200, 400, 800, 1_000, 1_000, 1_000, 1_000, 1_000, 1_000, 1_000, 1_000]
    failures = [
        record
        for record in caplog.records
        if record.getMessage().startswith("接続の受け付けに失敗")
    ]
    # 伸びた5回（1, 2, 3, 4, 5 回目）+ 頭打ちの後の 2 の冪（8 回目）
    assert [record.fields["consecutive_failures"] for record in failures] == [1, 2, 3, 4, 5, 8]
    assert failures[-1].fields["suppressed_since_last_log"] == 2
    assert failures[0].fields["errno"] == "EMFILE"
    assert failures[0].exc_info
    assert not any(record.exc_info for record in failures[1:])

    # 成功したら休みの長さを戻す（次の失敗は initial_ms から）
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server._accept(OnceListener(ours))  # type: ignore[arg-type]
        assert server._accept_backoff_ms is None
        assert server._accept_failures == 0
        assert any(
            record.getMessage() == "接続の受け付けが回復した"
            and record.fields["consecutive_failures"] == 12
            for record in caplog.records
        )
        server._accept(listener)  # type: ignore[arg-type]
        assert server._accept_backoff_ms == 100
    finally:
        server.close()
        theirs.close()


@needs_peercred
def test_the_backoff_grows_by_the_configured_multiplier():
    monotonic = ManualMonotonicClock(0)
    server = idle_server(
        monotonic,
        accept_backoff__initial_ms=100,
        accept_backoff__max_ms=1_000,
        accept_backoff__multiplier=1.5,
    )
    backoffs: list[int] = []
    for _ in range(8):
        server._accept(FailingListener())  # type: ignore[arg-type]
        assert server._accept_backoff_ms is not None
        backoffs.append(server._accept_backoff_ms)
    assert backoffs == [100, 150, 225, 338, 507, 761, 1_000, 1_000]
    server.close()


@needs_peercred
@pytest.mark.parametrize("multiplier", [1e308, float("inf"), float("nan")])
def test_a_huge_multiplier_clamps_to_max_ms_without_raising(multiplier):
    """起動時の検証を通り抜けた値でも、受付スレッドを落とさず `max_ms` で頭打ちにする。"""
    monotonic = ManualMonotonicClock(0)
    server = idle_server(monotonic, accept_backoff__initial_ms=100, accept_backoff__max_ms=1_000)
    backoff = server._settings.accept_backoff.model_copy(update={"multiplier": multiplier})
    server._settings = server._settings.model_copy(update={"accept_backoff": backoff})
    backoffs: list[int] = []
    for _ in range(4):
        server._accept(FailingListener())  # type: ignore[arg-type]
        assert server._accept_backoff_ms is not None
        backoffs.append(server._accept_backoff_ms)
    assert backoffs == [100, 1_000, 1_000, 1_000]
    assert not server._accept_exhausted
    server.close()


def fail_at(server: ControlAdminServer, monotonic: ManualMonotonicClock, at_ms: int) -> None:
    monotonic.advance_ms(at_ms - monotonic.monotonic_ms())
    server._accept(FailingListener())  # type: ignore[arg-type]


@needs_peercred
def test_an_unbroken_run_of_accept_failures_is_escalated_only_at_escalate_after_ms(caplog):
    """起点は途切れずに続く失敗の最初。`escalate_after_ms` に届く前は休むだけ。"""
    monotonic = ManualMonotonicClock(10_000)
    server = idle_server(
        monotonic,
        accept_backoff__initial_ms=100,
        accept_backoff__max_ms=1_000,
        accept_backoff__escalate_after_ms=5_000,
    )
    caplog.set_level("INFO", logger="coldaisle.control_admin")
    for at_ms in (10_000, 11_000, 12_000, 14_999):
        fail_at(server, monotonic, at_ms)
        assert not server._accept_exhausted
        assert server._accept_resume_mono_ms is not None
    fail_at(server, monotonic, 15_000)
    assert server._accept_exhausted
    assert server._accept_resume_mono_ms is None, "待ち受けを戻さない"
    exhausted = [
        record
        for record in caplog.records
        if getattr(record, "fields", {}).get("reason") == "admin_accept_exhausted"
    ]
    assert len(exhausted) == 1
    assert exhausted[0].levelname == "ERROR"
    assert exhausted[0].fields["consecutive_failures"] == 5
    assert exhausted[0].fields["failing_for_ms"] == 5_000
    assert exhausted[0].fields["errno"] == "EMFILE"
    server.close()


@needs_peercred
def test_a_successful_accept_restarts_the_escalation_clock():
    monotonic = ManualMonotonicClock(10_000)
    server = idle_server(
        monotonic,
        accept_backoff__initial_ms=100,
        accept_backoff__max_ms=1_000,
        accept_backoff__escalate_after_ms=5_000,
    )
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        fail_at(server, monotonic, 10_000)
        fail_at(server, monotonic, 14_000)
        server._accept(OnceListener(ours))  # type: ignore[arg-type]
        assert server._accept_first_failure_mono_ms is None
        fail_at(server, monotonic, 16_000)  # 最初の失敗から 6 秒だが、成功の後は 0 秒
        fail_at(server, monotonic, 20_999)
        assert not server._accept_exhausted
        fail_at(server, monotonic, 21_000)
        assert server._accept_exhausted
    finally:
        server.close()
        theirs.close()


class AcceptSwitch:
    """待ち受けの `accept()` だけを EMFILE で失敗させ、呼ばれた回数を数える。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, listener: socket.socket) -> None:
        self.failing = False
        self.failures = 0
        original = socket.socket.accept
        switch = self

        def accept(sock: socket.socket) -> tuple[socket.socket, Any]:
            if sock is listener and switch.failing:
                switch.failures += 1
                raise OSError(errno.EMFILE, "Too many open files")
            return original(sock)

        monkeypatch.setattr(socket.socket, "accept", accept)


def connect(path: Path) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5)
    conn.connect(str(path))
    return conn


@needs_peercred
def test_a_persistent_accept_failure_does_not_spin_the_receiver(short_dir, rules, monkeypatch):
    """待ち受けが読める状態のまま accept が失敗し続けても、空回りせずログも溢れない。"""
    running = RunningEntry(
        short_dir, rules, accept_backoff__initial_ms=50, accept_backoff__max_ms=200
    )
    pending: socket.socket | None = None
    try:
        assert running.server._listener is not None
        switch = AcceptSwitch(monkeypatch, running.server._listener)
        switch.failing = True
        pending = connect(running.path)  # カーネルの待ち行列に残り、待ち受けは読める状態が続く
        time.sleep(1.0)
        # 休まなければ 1 秒で数千回。休みは 50, 100, 200, 200, ... ms なので 1 秒で 7 回前後
        assert 1 <= switch.failures <= 12
        assert running.mailbox.receiver_alive()
    finally:
        if pending is not None:
            pending.close()
        running.stop()


@needs_peercred
def test_max_on_an_existing_connection_is_delivered_while_accept_is_backing_off(
    short_dir, rules, monkeypatch
):
    """休んでいるのは待ち受けだけ。接続済みの接続の `MAX` は遅れずに受け渡し口へ置く。"""
    running = RunningEntry(
        short_dir,
        rules,
        limits__read_timeout_s=1.0,
        accept_backoff__initial_ms=5_000,
        accept_backoff__max_ms=5_000,
    )
    existing: socket.socket | None = None
    newcomer: socket.socket | None = None
    try:
        assert running.server._listener is not None
        switch = AcceptSwitch(monkeypatch, running.server._listener)
        existing = connect(running.path)
        wait_until(lambda: len(running.server._connections) == 1)
        switch.failing = True
        newcomer = connect(running.path)
        wait_until(lambda: running.server._accept_resume_mono_ms is not None)
        paused_at = time.monotonic()
        existing.sendall(encode(MAX))
        running.tick_until(OperatingMode.MAX)
        reply = json.loads(existing.recv(1_000).split(b"\n")[0])
        assert reply["applied"] is True
        assert time.monotonic() - paused_at < 2.0, "休みの 5 秒を待たずに届く"
        assert running.server._accept_resume_mono_ms is not None, "まだ休んでいる"
        assert switch.failures == 1
    finally:
        for conn in (existing, newcomer):
            if conn is not None:
                conn.close()
        running.stop()


@needs_peercred
def test_persistent_accept_failures_end_the_receiver_and_the_loop_holds_max(
    short_dir, rules, catalog, monkeypatch
):
    """0076 §2.7: 途切れない失敗が `escalate_after_ms` に届いたら受付スレッドを終わらせ、
    loop は既存の「受付スレッドの死」の経路で `MANUAL` を解除し、再起動まで `MAX` にする。"""
    monotonic = ManualMonotonicClock(1_000_000)
    running = RunningEntry(
        short_dir,
        rules,
        monotonic=monotonic,
        accept_backoff__initial_ms=100,
        accept_backoff__max_ms=100,
        accept_backoff__escalate_after_ms=300,
    )
    harness = Harness(catalog, admin_mode=running.tracker)
    pending: socket.socket | None = None
    try:
        harness.settle()
        future = running.send_async(manual_body(lease_s=3_600))
        wait_until(lambda: 1 in running.server._awaiting_ack)
        assert harness.tick().tick.state.operating_mode is OperatingMode.MANUAL
        assert future.result(timeout=5)["applied"] is True

        assert running.server._listener is not None
        switch = AcceptSwitch(monkeypatch, running.server._listener)
        switch.failing = True
        pending = connect(running.path)  # 待ち受けが読める状態を続ける
        wait_until(lambda: switch.failures == 1)
        # 失敗が escalate_after_ms に届くまでは休むだけ。受付は生きていて、モードも変わらない
        for expected in (2, 3):
            monotonic.advance_ms(100)
            running.mailbox.wake()  # 次の周回で期限を見て待ち受けを戻す
            wait_until(lambda n=expected: switch.failures == n)
            assert running.server.thread.is_alive()
            assert harness.tick().tick.state.operating_mode is OperatingMode.MANUAL

        monotonic.advance_ms(100)  # 最初の失敗から 300 ms
        running.mailbox.wake()
        running.server.thread.join(timeout=5)
        assert not running.server.thread.is_alive(), "受付スレッドを終わらせる"
        assert switch.failures == 4
        assert running.server._listener is None, "待ち受けを閉じる"
        assert running.server._connections == set()

        dead = harness.tick().tick
        assert dead.state.operating_mode is OperatingMode.MAX
        assert dead.mode_command is not None and dead.mode_command.admin_receiver_dead
        assert all(dead.zones.get(zone).demand.forced_max for zone in Zone)
        for result in harness.run(5):
            assert result.tick.state.operating_mode is OperatingMode.MAX
            assert result.tick.mode_command is not None
            assert result.tick.mode_command.admin_receiver_dead is True
    finally:
        if pending is not None:
            pending.close()
        running.stop()
    assert not running.path.exists(), "停止の手順でソケットのファイルを消す"


@needs_peercred
def test_accept_resumes_after_the_backoff_and_resets_it(short_dir, rules, monkeypatch):
    """休みが明けたら待ち受けを戻し、待たされていた接続にも答える。成功で休みの長さを戻す。"""
    running = RunningEntry(
        short_dir, rules, accept_backoff__initial_ms=200, accept_backoff__max_ms=200
    )
    waiting: socket.socket | None = None
    try:
        assert running.server._listener is not None
        switch = AcceptSwitch(monkeypatch, running.server._listener)
        switch.failing = True
        waiting = connect(running.path)
        waiting.sendall(encode({"v": 1, "op": "status"}))
        wait_until(lambda: running.server._accept_resume_mono_ms is not None)
        switch.failing = False  # fd が空いた
        reply = json.loads(waiting.recv(1_000).split(b"\n")[0])
        assert reply["ok"] is True
        assert switch.failures == 1
        assert running.server._accept_backoff_ms is None
        assert running.server._accept_failures == 0
        assert running.send({"v": 1, "op": "status"})["ok"] is True
    finally:
        if waiting is not None:
            waiting.close()
        running.stop()


def test_stopping_without_drain_does_not_wait_for_a_blocked_audit(short_dir, rules):
    """loop が例外で抜けた経路は、監査の DB が詰まっていても終了を遅らせない。"""
    running = RunningEntry(short_dir, rules, apply_ack_timeout_ms=3_000)
    running.gate.clear()  # 監査の DB が lock されている
    running.send_async(AUTO)
    assert running.writing.wait(timeout=5), "監査書き込みスレッドが gate で止まるまで"
    admin = admin_runtime.ControlAdminEntry(
        settings=running.settings,
        mailbox=running.mailbox,
        server=running.server,
        audit=running.audit,
        tracker=running.tracker,
        run_id=RUN_ID,
        shutdown_wait_ms=3_000,
    )
    started = time.monotonic()
    admin.stop(drain=False)
    elapsed = time.monotonic() - started
    running.gate.set()
    running.pool.shutdown(wait=False, cancel_futures=True)
    assert elapsed < 0.5


def test_the_daemon_close_passes_the_drain_choice_to_the_entry():
    from coldaisle.control_daemon import ControlDaemon

    calls: list[bool] = []

    class StubAdmin:
        def stop(self, *, drain: bool = True) -> None:
            calls.append(drain)

    daemon = ControlDaemon(loop=None, monotonic=SystemMonotonicClock(), admin=StubAdmin())  # type: ignore[arg-type]
    daemon.close(drain=False)
    assert calls == [False]
    assert daemon.admin is None


@needs_peercred
def test_status_reports_the_loop_state_without_touching_the_loop(entry):
    before = entry.send({"v": 1, "op": "status"})
    assert before["ok"] is True and before["mode"] is None and before["tick_id"] is None
    future = entry.send_async(manual_body(lease_s=60))
    entry.tick_until(OperatingMode.MANUAL)
    assert future.result(timeout=5)["applied"] is True

    status = entry.send({"v": 1, "op": "status"})

    assert status["mode"] == "manual"
    assert status["command_id"] == 1
    assert 0 < status["manual_lease_remaining_ms"] <= 60_000
    assert status["authority_stage"] == "shadow"
    assert status["authority_ceiling"] == "shadow"
    assert status["authority_journal_stage"] is None
    assert status["persist_failure"] is None
    assert status["entry"] == "open"
    assert status["audit_failures"] == 0
    assert status["run_id"] == RUN_ID


@needs_peercred
def test_a_command_is_applied_and_acknowledged_with_its_tick(entry):
    future = entry.send_async(MAX)
    resolution = entry.tick_until(OperatingMode.MAX)
    response = future.result(timeout=5)

    assert response == {
        "ok": True,
        "command_id": 1,
        "applied": True,
        "applied_tick_id": entry.tick_id,
    }
    assert resolution.record.command_id == 1
    wait_until(lambda: ("applied", 1, entry.tick_id, None) in entry.rows())
    assert entry.rows()[0] == ("accepted", 1, None, None)


@needs_peercred
def test_an_unapplied_command_answers_pending_and_is_not_withdrawn(entry):
    response = entry.send(MAX)  # loop が回らないまま apply_ack_timeout_ms を過ぎる
    assert response == {
        "ok": True,
        "command_id": 1,
        "applied": False,
        "applied_tick_id": None,
        "pending": True,
    }
    resolution = entry.tick()
    assert resolution.command.mode is OperatingMode.MAX, "遅れて効き、取り消さない"
    wait_until(lambda: ("applied", 1, entry.tick_id, None) in entry.rows())


@needs_peercred
def test_max_is_placed_before_its_audit_row_and_does_not_wait_for_the_database(entry):
    """監査の DB が lock されていても、MAX は次の tick で効く（0072 §2.7）。"""
    entry.gate.clear()
    future = entry.send_async(MAX)
    entry.tick_until(OperatingMode.MAX)
    assert future.result(timeout=5)["applied"] is True
    assert entry.rows() == [], "受付の行はまだ書けていない"
    entry.gate.set()
    wait_until(lambda: len(entry.rows()) == 2)
    assert entry.rows()[0] == ("accepted", 1, None, None)


@needs_peercred
def test_a_weakening_command_waits_for_its_audit_row_before_it_is_placed(entry):
    entry.gate.clear()
    future = entry.send_async(AUTO)
    wait_until(lambda: 1 in entry.server._auditing)
    assert entry.tick().record.command_id is None, "監査を書けるまで受け渡し口へ置かない"
    assert not future.done()
    entry.gate.set()
    wait_until(lambda: entry.tick().record.command_id == 1)
    assert future.result(timeout=5)["ok"] is True
    wait_until(lambda: entry.rows()[:1] == [("accepted", 1, None, None)])


@needs_peercred
def test_max_arriving_during_an_audit_wait_supersedes_the_earlier_weakening(entry):
    """先に届いた弱めうる指令が、後から先に置かれた MAX を上書きしない（0072 §2.2）。"""
    entry.gate.clear()
    weakening = entry.send_async(manual_body())
    wait_until(lambda: 1 in entry.server._auditing)
    maximum = entry.send_async(MAX)
    entry.tick_until(OperatingMode.MAX)
    assert maximum.result(timeout=5)["command_id"] == 2

    entry.gate.set()
    response = weakening.result(timeout=5)

    assert response == {
        "ok": True,
        "command_id": 1,
        "applied": False,
        "applied_tick_id": None,
        "superseded_by": 2,
    }
    for _ in range(3):
        assert entry.tick().command.mode is OperatingMode.MAX
    wait_until(lambda: ("superseded", 1, None, 2) in entry.rows())
    events = entry.rows()
    assert ("accepted", 1, None, None) in events
    assert ("accepted", 2, None, None) in events
    assert not any(event == "applied" and command_id == 1 for event, command_id, *_ in events)


@needs_peercred
def test_two_commands_before_one_tick_keep_only_the_newest(entry):
    first = entry.send_async(MAX)
    wait_until(lambda: 1 in entry.server._awaiting_ack)
    second = entry.send_async(manual_body())
    wait_until(lambda: 2 in entry.server._awaiting_ack)
    resolution = entry.tick()
    assert resolution.command.mode is OperatingMode.MANUAL
    assert first.result(timeout=5)["superseded_by"] == 2
    assert second.result(timeout=5)["applied"] is True
    wait_until(lambda: ("superseded", 1, None, 2) in entry.rows())


@needs_peercred
def test_status_counts_audit_failures(short_dir, rules):
    running = RunningEntry(short_dir, rules, audit_fails=True)
    try:
        # 弱めうる指令は受付の行を書けなければ受けない（記録の無い弱化を作らない）
        assert running.send(AUTO) == {"ok": False, "error": "audit_unavailable"}
        assert running.tick().record.command_id is None
        # 安全側は書けなくても適用を妨げず、取り消さない
        future = running.send_async(MAX)
        running.tick_until(OperatingMode.MAX)
        assert future.result(timeout=5)["applied"] is True
        wait_until(lambda: running.send({"v": 1, "op": "status"})["audit_failures"] >= 3)
    finally:
        running.stop()


@needs_peercred
def test_a_full_audit_queue_refuses_weakening_but_still_applies_max(short_dir, rules):
    running = RunningEntry(short_dir, rules, limits__audit_queue_max=1)
    try:
        running.gate.clear()
        blocked = running.send_async(AUTO)  # 書き込み中で止まる
        wait_until(lambda: 1 in running.server._auditing)
        # 1件目が queue に残ったままだと、2件目は queue の1枠に入れず audit_unavailable で
        # 拒否され、`_auditing` に入らない。書き込みスレッドが取り出して gate で止まるまで待つ
        assert running.writing.wait(timeout=5), "監査書き込みスレッドが1件目を取り出していない"
        queued = running.send_async(AUTO)  # queue の1枠を埋める
        wait_until(lambda: 2 in running.server._auditing)
        assert running.send(manual_body()) == {"ok": False, "error": "audit_unavailable"}
        future = running.send_async(MAX)
        running.tick_until(OperatingMode.MAX)
        assert future.result(timeout=5)["applied"] is True
        running.gate.set()
        assert blocked.result(timeout=5)["superseded_by"] == 4
        assert queued.result(timeout=5)["superseded_by"] == 4
    finally:
        running.stop()


@needs_peercred
def test_a_full_pending_slot_refuses_weakening_but_places_max(short_dir, rules):
    running = RunningEntry(short_dir, rules, limits__max_pending_commands=1)
    try:
        waiting = running.send_async(MAX)
        wait_until(lambda: 1 in running.server._awaiting_ack)
        assert running.send(AUTO) == {"ok": False, "error": "busy"}
        response = running.send(MAX)
        assert response == {
            "ok": True,
            "command_id": 2,
            "applied": False,
            "applied_tick_id": None,
            "pending": True,
        }
        resolution = running.tick()
        assert resolution.record.command_id == 2
        assert waiting.result(timeout=5)["superseded_by"] == 2
        wait_until(lambda: ("applied", 2, running.tick_id, None) in running.rows())
        assert not any(command_id == 3 for _, command_id, *_ in running.rows()), (
            "busy の指令は採番も監査もしない"
        )
    finally:
        running.stop()


@needs_peercred
def test_silent_receivers_are_evicted_oldest_first_so_max_still_gets_through(short_dir, rules):
    """1行を送り切らない接続が枠を埋めても、新しい指令は読まれる（0072 §2.2）。"""
    running = RunningEntry(short_dir, rules, limits__max_connections=2, limits__read_timeout_s=1.0)
    idle: list[socket.socket] = []
    try:
        for _ in range(2):
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(str(running.path))
            conn.sendall(b'{"v": 1, ')
            idle.append(conn)
            time.sleep(0.05)
        wait_until(lambda: len(running.server._connections) == 2)
        future = running.send_async(MAX)
        running.tick_until(OperatingMode.MAX)
        assert future.result(timeout=5)["applied"] is True
        idle[0].settimeout(2)
        assert idle[0].recv(100) == b"", "最も古い受信中の接続を応答なしで閉じる"
    finally:
        for conn in idle:
            conn.close()
        running.stop()


@needs_peercred
def test_a_slow_sender_is_cut_at_the_read_timeout(short_dir, rules):
    running = RunningEntry(short_dir, rules, limits__read_timeout_s=0.2)
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(3)
        conn.connect(str(running.path))
        conn.sendall(b'{"v": 1,')
        reply = json.loads(conn.recv(200).split(b"\n")[0])
        assert reply == {"ok": False, "error": "read_timeout"}
        conn.close()
    finally:
        running.stop()


@needs_peercred
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"v": 1, "op": "rollback_authority", "reason": "x"}, "unsupported_op"),
        ({"v": 1, "op": "lower_authority", "to_stage": "shadow", "reason": "x"}, "unsupported_op"),
        ({"v": 1, "op": "raise_authority", "reason": "x"}, "unknown_op"),
        ({**manual_body(), "lease_s": 10**7}, "lease_too_long"),
    ],
)
def test_authority_changes_are_not_accepted_in_stage_1(entry, body, code):
    assert entry.send(body) == {"ok": False, "error": code}
    assert entry.tick().record.command_id is None
    assert entry.rows() == []


@needs_peercred
def test_an_unauthorized_peer_is_refused_and_never_evicts_anyone(short_dir, rules):
    """同じ uid も root も暗黙には認めない（`server_uid` を別人にして確かめる）。"""
    running = RunningEntry(short_dir, rules, server_uid=os.getuid() + 1)
    try:
        assert running.send(MAX) == {"ok": False, "error": "unauthorized"}
        assert running.tick().record.command_id is None
    finally:
        running.stop()


class _WriteAfterSocket(socket.socket):
    """`sendall` を合図まで待たせる。サーバが先に応答して閉じる順序を毎回つくる。"""

    gate: threading.Event

    def sendall(self, data: Any, flags: int = 0) -> None:  # type: ignore[override]
        assert self.gate.wait(timeout=5)
        super().sendall(data, flags)


def _client_writes_after(monkeypatch: pytest.MonkeyPatch, gate: threading.Event) -> None:
    """クライアントの `socket` だけを差し替える（受付スレッドのソケットには触れない）。"""
    delayed = type("DelayedSocket", (_WriteAfterSocket,), {"gate": gate})
    namespace = type(
        "SocketModule",
        (),
        {"AF_UNIX": socket.AF_UNIX, "SOCK_STREAM": socket.SOCK_STREAM, "socket": delayed},
    )
    monkeypatch.setattr(admin_client, "socket", namespace)


@needs_peercred
def test_an_unauthorized_peer_sees_unauthorized_even_if_the_server_closed_first(
    short_dir, rules, monkeypatch
):
    """拒否の応答を書いて閉じたあとにクライアントが書く順序（EPIPE）でも「拒否」と分かる。

    CI で間欠的に「接続できない: Broken pipe」になった順序を、合図で毎回つくる。
    """
    running = RunningEntry(short_dir, rules, server_uid=os.getuid() + 1)
    closed = threading.Event()
    reply_and_close = running.server._reply_and_close

    def reply_then_signal(sock: socket.socket, body: dict[str, Any]) -> None:
        reply_and_close(sock, body)
        closed.set()

    running.server._reply_and_close = reply_then_signal  # type: ignore[method-assign]
    _client_writes_after(monkeypatch, closed)
    try:
        assert running.send(MAX) == {"ok": False, "error": "unauthorized"}
        assert running.tick().record.command_id is None
    finally:
        running.stop()


def test_the_client_reads_a_reply_already_sent_when_its_write_fails(short_dir, monkeypatch):
    """書き込みが EPIPE でも、届いている応答の1行を読む。応答が無ければ「接続できない」。"""
    path = short_dir / "peer.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    closed = threading.Event()
    replies = [b'{"ok": false, "error": "unauthorized"}\n', b""]

    def refuse() -> None:
        for reply in replies:
            peer, _ = listener.accept()
            peer.sendall(reply)
            peer.close()
            closed.set()

    server = threading.Thread(target=refuse, daemon=True)
    server.start()
    _client_writes_after(monkeypatch, closed)
    try:
        response = admin_client.send(encode(MAX), path, timeout_s=5)
        assert response == {"ok": False, "error": "unauthorized"}
        closed.clear()
        with pytest.raises(admin_client.AdminUnavailableError, match="接続できない"):
            admin_client.send(encode(MAX), path, timeout_s=5)
    finally:
        server.join(timeout=5)
        listener.close()


@needs_peercred
def test_the_lease_expiry_is_audited_as_its_own_event(entry):
    future = entry.send_async(manual_body(lease_s=1))
    entry.tick_until(OperatingMode.MANUAL)
    assert future.result(timeout=5)["applied"] is True
    applied_tick = entry.tick_id
    time.sleep(1.05)
    resolution = entry.tick()
    assert resolution.command.mode is OperatingMode.AUTO
    assert resolution.record.manual_lease_expired_command_id == 1
    wait_until(lambda: ("lease_expired", 1, entry.tick_id, None) in entry.rows())
    assert ("applied", 1, applied_tick, None) in entry.rows()


@needs_peercred
def test_a_dead_receiver_thread_forces_max_on_the_next_tick_until_restart(
    short_dir, rules, catalog
):
    """受付スレッドを止めた試験（0072 §2.10 段階 1）。**本物の受付スレッド**を落とす。"""
    running = RunningEntry(short_dir, rules)
    harness = Harness(catalog, admin_mode=running.tracker)
    try:
        harness.settle()
        future = running.send_async(manual_body(lease_s=3_600))
        wait_until(lambda: 1 in running.server._awaiting_ack)
        assert harness.tick().tick.state.operating_mode is OperatingMode.MANUAL
        assert future.result(timeout=5)["applied"] is True

        def crash() -> None:
            raise RuntimeError("受付スレッドの不具合")

        running.server._on_loop_outcomes = crash  # type: ignore[method-assign]
        running.mailbox.wake()
        running.server.thread.join(timeout=5)
        assert not running.server.thread.is_alive()

        dead = harness.tick().tick
        assert dead.state.operating_mode is OperatingMode.MAX
        assert dead.mode_command is not None and dead.mode_command.admin_receiver_dead
        assert all(dead.zones.get(zone).demand.forced_max for zone in Zone)

        running.mailbox.place_mode(AdminModeCommand(command_id=99, mode=OperatingMode.AUTO))
        for result in harness.run(5):
            assert result.tick.state.operating_mode is OperatingMode.MAX
            assert result.tick.mode_command is not None
            assert result.tick.mode_command.admin_receiver_dead is True
    finally:
        running.stop()


def test_the_audit_table_is_append_only(tmp_path, rules):
    with SqliteStore(tmp_path / "a.db", rules=rules, clock=SimulatedClock(0)) as store:
        store.record_control_admin_audit(
            ControlAdminAuditRecord(
                run_id=RUN_ID,
                command_id=1,
                event="accepted",
                ts_ms=1,
                peer_uid=1000,
                op="set_mode",
                body_json='{"mode":"max"}',
            )
        )
        store.record_control_admin_audit(
            ControlAdminAuditRecord(
                run_id=RUN_ID, command_id=1, event="applied", ts_ms=2, tick_id=5
            )
        )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.connection.execute("UPDATE control_admin_audit SET ts_ms = 0")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.connection.execute("DELETE FROM control_admin_audit")
        # 1つの command_id に結末は高々1つ。lease 切れは別の一意制約
        with pytest.raises(sqlite3.IntegrityError):
            store.record_control_admin_audit(
                ControlAdminAuditRecord(
                    run_id=RUN_ID, command_id=1, event="superseded", ts_ms=3, superseded_by=2
                )
            )
        store.record_control_admin_audit(
            ControlAdminAuditRecord(
                run_id=RUN_ID, command_id=1, event="lease_expired", ts_ms=4, tick_id=9
            )
        )
        # 再起動（別の run_id）では同じ command_id を使える
        store.record_control_admin_audit(
            ControlAdminAuditRecord(
                run_id="f" * 32,
                command_id=1,
                event="accepted",
                ts_ms=5,
                peer_uid=1000,
                op="set_mode",
                body_json="{}",
            )
        )
        assert [row.event for row in store.control_admin_audit(RUN_ID)] == [
            "accepted",
            "applied",
            "lease_expired",
        ]


def test_an_audit_row_carries_only_the_fields_of_its_event():
    with pytest.raises(ValidationError):
        ControlAdminAuditRecord(run_id=RUN_ID, command_id=1, event="applied", ts_ms=1)
    with pytest.raises(ValidationError):
        ControlAdminAuditRecord(
            run_id=RUN_ID, command_id=1, event="applied", ts_ms=1, tick_id=1, peer_uid=0
        )
    with pytest.raises(ValidationError):
        ControlAdminAuditRecord(
            run_id=RUN_ID, command_id=2, event="superseded", ts_ms=1, superseded_by=1
        )


# ================================================================ 起動（0072 §2.5 / §2.8）


def _open(path: Path, db: Path, rules: QualityRules) -> Any:
    return admin_runtime.open_control_admin(
        path,
        tick_ms=TICK_MS,
        tick_deadline_ms=TICK_DEADLINE_MS,
        authority_ceiling=AuthorityStage.SHADOW,
        open_audit_sink=lambda: SqliteStore(db, rules=rules, clock=SimulatedClock(0)),
        clock=SimulatedClock(TEST_EPOCH_MS),
        monotonic=SystemMonotonicClock(),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"limits__read_timeout_s": 2.0},
        {"apply_ack_timeout_ms": 10},
        {"version": 3},
    ],
)
def test_an_invalid_entry_config_does_not_open_the_socket(short_dir, rules, changes, caplog):
    config = short_dir / "control-admin.yaml"
    config.write_text(
        yaml.safe_dump(admin_document(short_dir / "run" / "admin.sock", **changes)), "utf-8"
    )
    assert _open(config, short_dir / "a.db", rules) is None
    assert not (short_dir / "run" / "admin.sock").exists()
    assert any("管理ソケットを開かずに運転する" in record.getMessage() for record in caplog.records)


@needs_peercred
@pytest.mark.parametrize("failing", ["control-admin-audit", "control-admin-receiver"])
def test_a_thread_that_cannot_start_leaves_no_socket_and_no_thread(
    short_dir, rules, monkeypatch, caplog, failing
):
    """スレッドを起動できなくても build() を止めず、入口なし（AUTO）へ退く（0072 §2.8）。"""
    config = short_dir / "control-admin.yaml"
    socket_path = short_dir / "run" / "admin.sock"
    config.write_text(yaml.safe_dump(admin_document(socket_path)), "utf-8")
    started: list[threading.Thread] = []
    # 前の試験（drain=False の停止など）が残した daemon thread は数えない。この試験が起動した
    # スレッドだけを見る
    earlier = set(threading.enumerate())
    original_start = threading.Thread.start

    def start(self: threading.Thread) -> None:
        if self.name == failing:
            raise RuntimeError("can't start new thread")
        started.append(self)
        original_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    assert _open(config, short_dir / "a.db", rules) is None
    assert not socket_path.exists()
    for thread in started:
        thread.join(timeout=5.0)
    assert not any(thread.is_alive() for thread in started)
    assert not any(
        thread.name.startswith("control-admin") and thread.is_alive()
        for thread in threading.enumerate()
        if thread not in earlier
    )
    assert any(
        "スレッドを起動できない" in getattr(record, "fields", {}).get("reason", "")
        for record in caplog.records
    )
    # 同じ場所でもう一度開ける（ロックもソケットも残っていない）
    monkeypatch.setattr(threading.Thread, "start", original_start)
    entry = _open(config, short_dir / "a.db", rules)
    assert entry is not None
    entry.stop()


def test_a_missing_entry_config_does_not_open_the_socket(short_dir, rules):
    assert _open(short_dir / "missing.yaml", short_dir / "a.db", rules) is None


def test_no_peer_credentials_means_no_socket(short_dir, rules, monkeypatch):
    config = short_dir / "control-admin.yaml"
    config.write_text(yaml.safe_dump(admin_document(short_dir / "run" / "admin.sock")), "utf-8")
    monkeypatch.setattr(admin_runtime, "peer_credentials_supported", lambda: False)
    assert _open(config, short_dir / "a.db", rules) is None


@needs_peercred
def test_the_daemon_opens_the_entry_and_records_it_every_tick(short_dir, tmp_path):
    """`coldaisle-fand` が入口を開き、loop は受け渡し口からモードを読む。停止でソケットを消す。"""
    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import Config, build
    from test_control_config import valid_documents, write_documents
    from test_simulated_fan_backend import hardware_config

    config_dir = tmp_path / "control"
    config_dir.mkdir()
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(config_dir, documents)
    admin_config = short_dir / "control-admin.yaml"
    admin_config.write_text(
        yaml.safe_dump(admin_document(short_dir / "run" / "admin.sock")), "utf-8"
    )
    daemon = build(
        Config(
            config_dir=config_dir,
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            admin_config=admin_config,
        ),
        watchdog=NullWatchdog(),
    )
    try:
        assert daemon.admin is not None
        socket_path = short_dir / "run" / "admin.sock"
        assert socket_path.exists()
        tick = daemon.loop.tick().tick
        assert tick.mode_command is not None
        assert tick.mode_command.entry == "control_admin"
        assert tick.mode_command.run_id == daemon.admin.run_id
        assert tick.mode_command.admin_receiver_dead is False
    finally:
        daemon.close()
    assert not (short_dir / "run" / "admin.sock").exists()


def test_the_daemon_runs_auto_without_an_entry(tmp_path):
    from coldaisle.control.loop import NullWatchdog
    from coldaisle.control_daemon import Config, build
    from test_control_config import valid_documents, write_documents
    from test_simulated_fan_backend import hardware_config

    config_dir = tmp_path / "control"
    config_dir.mkdir()
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(config_dir, documents)
    daemon = build(
        Config(
            config_dir=config_dir,
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            admin_config=tmp_path / "missing.yaml",
        ),
        watchdog=NullWatchdog(),
    )
    try:
        assert daemon.admin is None
        tick = daemon.loop.tick().tick
        assert tick.mode_command == ModeCommandRecord.without_entry()
    finally:
        daemon.close()


# ================================================================ クライアント


@needs_peercred
def test_the_client_cli_round_trips(entry, capsys):
    socket_args = ["--socket", str(entry.path), "--timeout", "5"]
    assert admin_client.main([*socket_args, "status"]) == 0
    assert json.loads(capsys.readouterr().out)["entry"] == "open"
    assert (
        admin_client.main(
            [
                *socket_args,
                "manual",
                "--front",
                "0.5",
                "--rear",
                "0.5",
                "--top",
                "0.5",
                "--lease",
                "10m",
                "--reason",
                "試験",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["pending"] is True
    resolution = entry.tick()
    assert resolution.command.mode is OperatingMode.MANUAL
    assert (
        admin_client.main(
            [
                *socket_args,
                "manual",
                "--front",
                "0.5",
                "--rear",
                "0.5",
                "--top",
                "0.5",
                "--lease",
                "999d",
                "--reason",
                "長すぎ",
            ]
        )
        == 1
    )


def test_the_client_needs_a_lease_for_manual_and_has_no_authority_raise():
    parser = admin_client.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["manual", "--front", "1", "--rear", "1", "--top", "1", "--reason", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["raise", "--reason", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["calibration", "--reason", "x"])


def test_the_client_reports_an_unreachable_socket(short_dir, capsys):
    code = admin_client.main(["--socket", str(short_dir / "none.sock"), "--timeout", "1", "status"])
    assert code == 2
    assert "接続できない" in capsys.readouterr().err


# ================================================================ 6. 構造（0072 §2.9）


ADMIN_PACKAGE = "coldaisle.control_admin"


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


def _hits(paths: list[Path], prefixes: tuple[str, ...]) -> list[str]:
    return [
        f"{path.relative_to(SRC)}: {name}"
        for path in paths
        for name in _imports(path)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
    ]


def test_llm_read_api_event_entry_and_control_do_not_import_the_admin_entry():
    """AI 層・読み取り API・ツール窓口・eventd・制御は管理ソケットを import しない。"""
    paths = [SRC / "server.py"]
    for package in ("ai", "api", "event_entry", "control", "rules", "notify"):
        paths.extend(sorted((SRC / package).rglob("*.py")))
    assert _hits(paths, (ADMIN_PACKAGE,)) == []


def test_critical_safety_does_not_know_where_the_mode_came_from():
    """Critical Safety はモードを値としてだけ受け取る（0072 §2.4）。"""
    paths = sorted((SRC / "control" / "safety").rglob("*.py"))
    assert _hits(paths, (ADMIN_PACKAGE, "coldaisle.control.operating_mode")) == []


def test_the_admin_entry_does_not_reach_the_llm_the_read_api_or_hwmon():
    paths = sorted((SRC / "control_admin").rglob("*.py"))
    forbidden = (
        "coldaisle.ai",
        "coldaisle.api",
        "coldaisle.server",
        "coldaisle.event_entry",
        "coldaisle.control.hardware",
        "coldaisle.control.safety",
        "coldaisle.control.loop",
        "coldaisle.control.authority",
        "subprocess",
    )
    assert _hits(paths, forbidden) == []


def test_the_import_detector_catches_a_real_import(tmp_path):
    """検査そのものの検査。素通しの検査は緑のまま何も守らない。"""
    snippet = SRC / "_probe_for_test.py"
    try:
        snippet.write_text(
            "from coldaisle.control_admin.client import send\nimport coldaisle.control_admin\n",
            encoding="utf-8",
        )
        assert len(_hits([snippet], (ADMIN_PACKAGE,))) == 3
    finally:
        snippet.unlink()


def test_importing_the_tool_window_does_not_load_the_admin_entry(tmp_path):
    """`coldaisle.server`（AI ツールの窓口）と AI 層を読み込んでも管理ソケットは載らない。"""
    code = (
        "import sys\n"
        "import coldaisle.server\n"
        "import coldaisle.ai\n"
        "import coldaisle.api\n"
        f"loaded = sorted(m for m in sys.modules if m.startswith({ADMIN_PACKAGE!r}))\n"
        "assert not loaded, loaded\n"
    )
    env = {**os.environ, "COLDAISLE_DB": str(tmp_path / "server.db")}
    for key in [name for name in env if name.startswith("COLDAISLE_AI")]:
        env.pop(key)
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def test_the_read_api_has_no_mode_route():
    """読み取り API は GET のみのまま（0009 §3）。モードや authority の経路を足さない。"""
    from coldaisle.api.app import Config as ApiConfig
    from coldaisle.api.app import create_app

    del ApiConfig
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((SRC / "api").rglob("*.py"))
    )
    for word in ("set_mode", "control-admin", "control_admin", "coldaisle-control"):
        assert word not in source
    assert callable(create_app)
