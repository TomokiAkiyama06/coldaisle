"""`coldaisle-fand` の終了コードと起動時の確認（決定記録 0080 §2.10 の段階 2。#74）。

1. `WATCHDOG_USEC` が `safety.yaml` の `watchdog_timeout_ms` より**長い**なら終了コード 4
   （等しい・短いは起動する。§2.3）
2. 通知の I/O の失敗（socket の作成・`READY=1` の送信）は終了コード 6 で、4 ではない（§2.4）
3. takeover 後に書き込みと読み戻しが `hardware_write_fail_exit_ms` 続けて失敗したら終了コード 7
   （途中で1回成功すれば数え直す。§2.6）
4. 起動時に `faulthandler` を有効にする（§2.5）。起動の後も SIGABRT に Python の処理関数を置かず、
   SIGABRT を塞がない（決定記録 0158 §2.5 の T2・T3）
5. 制御を取る前に DB へ書けるかを確かめ、書けなければ `db_not_writable` で終了コード 5。
   `SQLITE_BUSY` は権限の失敗と取り違えない（§2.1）

実機は要らない（simulated backend と `tmp_path`）。
"""

from __future__ import annotations

import errno
import json
import os
import resource
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coldaisle import control_daemon, logs
from coldaisle.clock import ManualMonotonicClock
from coldaisle.control.hardware.simulated import SimulatedFaultPlan
from coldaisle.control.safety.write_fail_exit import (
    HardwareWriteFailureExit,
    HardwareWriteFailureExitError,
)
from coldaisle.control.schema import Zone
from coldaisle.control_daemon import (
    EXIT_HARDWARE_WRITE_FAILED,
    EXIT_NOTIFY_IO_FAILED,
    EXIT_STARTUP_ENVIRONMENT,
    EXIT_WATCHDOG_UNAVAILABLE,
    NOTIFY_SOCKET_ENV,
    WATCHDOG_PID_ENV,
    WATCHDOG_USEC_ENV,
    Config,
    ControlDaemon,
    DbLockUnavailableError,
    DbNotWritableError,
    SystemdWatchdog,
    WatchdogNotifyIoError,
    WatchdogUnavailableError,
    build,
    create_watchdog,
    main,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store import QualityRules, SqliteStore
from conftest import TEST_EPOCH_MS
from test_control_config import valid_documents, write_documents
from test_control_loop import CONFIG_DIR, METRICS_PATH, Harness
from test_simulated_fan_backend import hardware_config

TIMEOUT_MS = 5_000
"""試験用 safety.yaml の `watchdog_timeout_ms`（`valid_documents()` と同じ値）。"""

HEARTBEAT_MS = 1_100
"""試験用 safety.yaml の `tick_ms + tick_deadline_ms`。"""

running_as_root = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="権限の試験。root では os.access と SQLite が権限を無視するので確かめられない",
)


@pytest.fixture(autouse=True)
def keep_caplog_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` の `logs.configure()` が root の handler を差し替え、caplog が記録を失うのを防ぐ。

    構造化ログの形そのもの（JSON Lines）は `logs` の試験が見ている。
    """
    monkeypatch.setattr(control_daemon.logs, "configure", lambda *_args, **_kwargs: None)


@pytest.fixture(scope="module")
def catalog() -> MetricCatalog:
    """**本番と同じ Metric Catalog** を読む。"""
    return MetricCatalog.from_yaml(METRICS_PATH)


@pytest.fixture
def listener(tmp_path: Path) -> Iterator[tuple[str, socket.socket]]:
    """systemd の通知先の代わり（`READY=1` を受け取るだけ）。"""
    address = str(tmp_path / "notify.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(address)
    sock.setblocking(False)
    try:
        yield address, sock
    finally:
        sock.close()


def _env(address: str, usec: int) -> dict[str, str]:
    return {NOTIFY_SOCKET_ENV: address, WATCHDOG_USEC_ENV: str(usec)}


def _confirmed_documents(directory: Path) -> None:
    """承認済みの hardware mapping（試験用）で4ファイルを置く。実機の値ではない。"""
    documents = valid_documents()
    documents["fan-hardware.yaml"] = json.loads(hardware_config().model_dump_json())
    write_documents(directory, documents)


def _argv(directory: Path, *extra: str, db: Path | None = None) -> list[str]:
    return [
        "--config-dir",
        str(directory),
        "--db",
        str(db if db is not None else directory / "control.db"),
        "--metrics",
        str(METRICS_PATH),
        "--quality-rules",
        str(CONFIG_DIR / "quality.yaml"),
        "--no-admin",
        "--authority-root",
        str(directory / "authority"),
        "--max-ticks",
        "1",
        *extra,
    ]


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    fields = (getattr(record, logs.FIELDS_KEY, None) for record in caplog.records)
    return [item for item in fields if isinstance(item, dict) and item.get("event") == event]


# ------------------------------------------- 1. WATCHDOG_USEC と watchdog_timeout_ms（§2.3）


def test_a_watchdog_equal_to_the_safety_timeout_starts_without_a_warning(
    listener: tuple[str, socket.socket], caplog: pytest.LogCaptureFixture
) -> None:
    """`WatchdogSec` = `watchdog_timeout_ms` は正しい構成。warning も出さない。"""
    address, sock = listener
    with caplog.at_level("WARNING", logger="coldaisle"):
        watchdog = create_watchdog(
            interval_ms=HEARTBEAT_MS,
            timeout_ms=TIMEOUT_MS,
            monotonic=ManualMonotonicClock(0),
            environ=_env(address, TIMEOUT_MS * 1_000),
        )
    assert isinstance(watchdog, SystemdWatchdog)
    assert sock.recv(64) == b"READY=1"
    assert not [record for record in caplog.records if record.levelname == "WARNING"]
    watchdog.close()


def test_a_watchdog_shorter_than_the_safety_timeout_starts_with_a_warning(
    listener: tuple[str, socket.socket], caplog: pytest.LogCaptureFixture
) -> None:
    """短いのはより厳しい deadman なので起動する。食い違いは warning に残す（0060 のまま）。"""
    address, sock = listener
    with caplog.at_level("WARNING", logger="coldaisle"):
        watchdog = create_watchdog(
            interval_ms=HEARTBEAT_MS,
            timeout_ms=TIMEOUT_MS,
            monotonic=ManualMonotonicClock(0),
            environ=_env(address, (TIMEOUT_MS - 1_000) * 1_000),
        )
    assert isinstance(watchdog, SystemdWatchdog)
    assert sock.recv(64) == b"READY=1"
    assert any("WatchdogSec" in record.getMessage() for record in caplog.records)
    watchdog.close()


@pytest.mark.parametrize(
    "usec",
    [
        (TIMEOUT_MS + 1_000) * 1_000,
        # ミリ秒へ切り捨てると等しく見える、1 マイクロ秒だけ長い値も通さない
        TIMEOUT_MS * 1_000 + 1,
    ],
)
def test_a_watchdog_longer_than_the_safety_timeout_never_starts(
    listener: tuple[str, socket.socket], usec: int
) -> None:
    """承認した deadman より遅い deadman では**制御を取らない**（終了コード 4）。

    拒否は `READY=1` の前（takeover の前）である。
    """
    address, sock = listener
    with pytest.raises(WatchdogUnavailableError, match="watchdog_timeout_ms"):
        create_watchdog(
            interval_ms=HEARTBEAT_MS,
            timeout_ms=TIMEOUT_MS,
            monotonic=ManualMonotonicClock(0),
            environ=_env(address, usec),
        )
    with pytest.raises(BlockingIOError):
        sock.recv(64)


def test_main_refuses_a_longer_watchdog_with_exit_code_4(
    tmp_path: Path, listener: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """合成の起点でも終了コード 4（`RestartPreventExitStatus` で再起動しない）。"""
    address, _ = listener
    _confirmed_documents(tmp_path)
    monkeypatch.setenv(NOTIFY_SOCKET_ENV, address)
    monkeypatch.setenv(WATCHDOG_USEC_ENV, str((TIMEOUT_MS + 1_000) * 1_000))
    monkeypatch.delenv(WATCHDOG_PID_ENV, raising=False)

    assert main(_argv(tmp_path, "--require-watchdog")) == EXIT_WATCHDOG_UNAVAILABLE


# --------------------------------------------------- 2. 通知の I/O の失敗（§2.4）


def test_a_notify_socket_that_cannot_be_created_is_not_exit_code_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fd の枯渇などで socket を作れないのは一時的でありうる。4 の例外にしない。"""

    def exhausted(*_args: object, **_kwargs: object) -> socket.socket:
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(control_daemon.socket, "socket", exhausted)
    with pytest.raises(WatchdogNotifyIoError, match="socket を作れない") as raised:
        create_watchdog(
            interval_ms=HEARTBEAT_MS,
            timeout_ms=TIMEOUT_MS,
            monotonic=ManualMonotonicClock(0),
            environ=_env("/nonexistent/notify.sock", TIMEOUT_MS * 1_000),
        )
    assert not isinstance(raised.value, WatchdogUnavailableError)


def test_main_reports_a_failed_ready_as_exit_code_6_not_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`READY=1` を送れないときは再起動する終了コード 6（4 だと制御が戻らない）。"""
    _confirmed_documents(tmp_path)
    monkeypatch.setenv(NOTIFY_SOCKET_ENV, str(tmp_path / "gone.sock"))
    monkeypatch.setenv(WATCHDOG_USEC_ENV, str(TIMEOUT_MS * 1_000))
    monkeypatch.delenv(WATCHDOG_PID_ENV, raising=False)

    code = main(_argv(tmp_path, "--require-watchdog"))

    assert code == EXIT_NOTIFY_IO_FAILED
    assert code != EXIT_WATCHDOG_UNAVAILABLE


# ------------------------------------- 3. 書き込みが続けて失敗したら終わる（§2.6）


def test_the_monitor_counts_a_continuous_failure_from_the_first_failed_tick() -> None:
    """期間は最初に失敗した tick の開始から数え、`>=` で終える。"""
    monitor = HardwareWriteFailureExit(3_000)
    failed = dict.fromkeys(Zone, False)

    monitor.observe(failed, tick_started_mono_ms=1_000, now_mono_ms=1_010)
    monitor.observe(failed, tick_started_mono_ms=2_000, now_mono_ms=2_010)
    monitor.observe(failed, tick_started_mono_ms=3_000, now_mono_ms=3_999)
    with pytest.raises(HardwareWriteFailureExitError) as raised:
        monitor.observe(failed, tick_started_mono_ms=4_000, now_mono_ms=4_000)

    assert raised.value.zones == tuple(Zone)
    assert raised.value.limit_ms == 3_000


def test_one_success_restarts_the_count_for_that_zone_only() -> None:
    """その zone の書き込みと読み戻しが1回成功すれば数え直す。ほかの zone は数え続ける。"""
    monitor = HardwareWriteFailureExit(3_000)
    all_failed = dict.fromkeys(Zone, False)
    top_recovered = {**all_failed, Zone.TOP: True}

    monitor.observe(all_failed, tick_started_mono_ms=0, now_mono_ms=0)
    monitor.observe(top_recovered, tick_started_mono_ms=2_000, now_mono_ms=2_000)
    assert monitor.failing_since(Zone.TOP) is None
    assert monitor.failing_since(Zone.FRONT) == 0

    with pytest.raises(HardwareWriteFailureExitError) as raised:
        monitor.observe(all_failed, tick_started_mono_ms=3_000, now_mono_ms=3_000)
    # Top は 3000 から数え直しているので、まだ終える理由にならない
    assert raised.value.zones == (Zone.FRONT, Zone.REAR)
    assert raised.value.failing_ms[Zone.TOP] == 0


def test_a_backend_exception_counts_as_a_failure_of_every_zone() -> None:
    """結果が無い tick（Backend の例外）や欠けた zone を、成功として扱わない。"""
    monitor = HardwareWriteFailureExit(1_000)
    monitor.observe(None, tick_started_mono_ms=0, now_mono_ms=0)
    monitor.observe({Zone.FRONT: True}, tick_started_mono_ms=500, now_mono_ms=500)
    assert monitor.failing_since(Zone.FRONT) is None
    assert monitor.failing_since(Zone.REAR) == 0

    with pytest.raises(HardwareWriteFailureExitError) as raised:
        monitor.observe(None, tick_started_mono_ms=1_000, now_mono_ms=1_000)
    assert raised.value.zones == (Zone.REAR, Zone.TOP)


def _daemon(
    harness: Harness, schedule: dict[int, SimulatedFaultPlan] | None = None
) -> ControlDaemon:
    """Harness の loop を `ControlDaemon` で回す。時計は sleep の中でだけ進める。

    ``schedule`` は「この単調時刻に達したら fault plan をこれに替える」表（simulated backend への
    失敗の注入）。
    """
    plans = dict(schedule or {})

    def sleep(seconds: float) -> None:
        step = round(seconds * 1_000)
        harness.monotonic.advance_ms(step)
        harness.clock.advance_to_ms(harness.clock.now_ms() + step)
        now = harness.monotonic.monotonic_ms()
        for at in sorted(plans):
            if at <= now:
                harness.backend.fault_plan = plans.pop(at)

    return ControlDaemon(
        loop=harness.loop,
        monotonic=harness.monotonic,
        sleep=sleep,
        write_fail_exit=HardwareWriteFailureExit(
            harness.config.safety.hardware_write_fail_exit_ms.value
        ),
    )


def test_failing_writes_from_takeover_end_the_daemon_after_the_configured_time(
    catalog: MetricCatalog,
) -> None:
    """takeover の最初の Max から書けないまま `hardware_write_fail_exit_ms` 経てば終わる。"""
    harness = Harness(catalog, fault_plan=SimulatedFaultPlan(write_failure=frozenset(Zone)))
    daemon = _daemon(harness)
    limit_ms = harness.config.safety.hardware_write_fail_exit_ms.value
    period_ms = harness.config.safety.tick_ms.value

    with pytest.raises(HardwareWriteFailureExitError) as raised:
        daemon.run(max_ticks=100)

    assert raised.value.zones == tuple(Zone)
    # tick は 0, 1000, ..., 5000 ms。5000 ms の tick で期間に達する（それより前には終えない）
    assert harness.monotonic.monotonic_ms() == limit_ms
    assert daemon.stats.ticks == limit_ms // period_ms + 1


def test_a_readback_mismatch_on_one_zone_also_ends_the_daemon(catalog: MetricCatalog) -> None:
    """読み戻しの不一致も数える（書けたと確かめられていない）。"""
    harness = Harness(
        catalog, fault_plan=SimulatedFaultPlan(readback_mismatch=frozenset({Zone.TOP}))
    )
    daemon = _daemon(harness)

    with pytest.raises(HardwareWriteFailureExitError) as raised:
        daemon.run(max_ticks=100)

    assert raised.value.zones == (Zone.TOP,)


def test_a_single_success_restarts_the_count(catalog: MetricCatalog) -> None:
    """途中で1回成功すれば数え直す。**成功の後の失敗から** 期間を数える。"""
    failing = SimulatedFaultPlan(write_failure=frozenset(Zone))
    harness = Harness(catalog, fault_plan=failing)
    # 0〜3000 ms は失敗、4000 ms の1 tick だけ成功、5000 ms から再び失敗
    daemon = _daemon(harness, {4_000: SimulatedFaultPlan(), 5_000: failing})
    limit_ms = harness.config.safety.hardware_write_fail_exit_ms.value

    with pytest.raises(HardwareWriteFailureExitError):
        daemon.run(max_ticks=100)

    # 数え直さなければ 5000 ms で終わっていた
    assert harness.monotonic.monotonic_ms() == 5_000 + limit_ms


def test_healthy_writes_never_end_the_daemon(catalog: MetricCatalog) -> None:
    """書けている間は、期間を何倍過ぎても終えない。"""
    harness = Harness(catalog)
    daemon = _daemon(harness)

    stats = daemon.run(max_ticks=20)

    assert stats.ticks == 20


def test_main_exits_with_code_7_without_returning_control(
    tmp_path: Path,
    catalog: MetricCatalog,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """終了コード 7 で終え、構造化ログに `hardware_write_fail_exit` を残す。

    終える経路は例外の経路と同じ後片付けだけで、返却（0080 §2.8）も記録の削除もしない。
    ここでは「返却の経路（`hand_back_hardware`）を通らない」ことを終了コードと event で
    確かめる（実機 backend の記録が残ることは `test_hwmon_fan_backend.py`）。
    """
    _confirmed_documents(tmp_path)
    harness = Harness(catalog, fault_plan=SimulatedFaultPlan(write_failure=frozenset(Zone)))
    daemon = _daemon(harness)
    built: list[Config] = []

    def fake_build(config: Config, **_kwargs: object) -> ControlDaemon:
        built.append(config)
        return daemon

    monkeypatch.setattr(control_daemon, "build", fake_build)
    hand_backs: list[None] = []

    def recording_hand_back(self: ControlDaemon) -> bool | None:
        hand_backs.append(None)
        return None

    monkeypatch.setattr(ControlDaemon, "hand_back_hardware", recording_hand_back)
    closes: list[dict[str, bool]] = []
    original_close = ControlDaemon.close

    def recording_close(
        self: ControlDaemon, *, drain: bool = True, persist_without_wait: bool = False
    ) -> None:
        closes.append({"drain": drain, "persist_without_wait": persist_without_wait})
        original_close(self, drain=drain, persist_without_wait=persist_without_wait)

    # slots の dataclass なのでインスタンスには差し込めない。クラスの側で記録する
    monkeypatch.setattr(ControlDaemon, "close", recording_close)
    argv = [*_argv(tmp_path)[:-2], "--max-ticks", "100"]

    with caplog.at_level("INFO", logger="coldaisle"):
        code = main(argv)

    assert code == EXIT_HARDWARE_WRITE_FAILED
    assert len(built) == 1
    # スレッドは待たないが、受理済みの降格は lock を待たずに1回書き残しを試す（決定記録 0083）
    assert closes == [{"drain": False, "persist_without_wait": True}]
    assert hand_backs == []
    events = _events(caplog, "hardware_write_fail_exit")
    assert len(events) == 1
    assert events[0]["zones"] == [zone.value for zone in Zone]
    assert events[0]["hardware_write_fail_exit_ms"] == (
        harness.config.safety.hardware_write_fail_exit_ms.value
    )
    json.dumps(events[0])


def test_build_wires_the_write_fail_exit_from_safety_yaml(tmp_path: Path) -> None:
    """合成の起点が判定を必ず配線し、期間は safety.yaml から取る（コードに置かない）。"""
    _confirmed_documents(tmp_path)
    daemon = build(
        Config(
            config_dir=tmp_path,
            db=tmp_path / "control.db",
            metrics=METRICS_PATH,
            quality_rules=CONFIG_DIR / "quality.yaml",
            authority_root=tmp_path / "authority",
        )
    )
    try:
        assert daemon.write_fail_exit is not None
        expected = valid_documents()["safety.yaml"]["hardware_write_fail_exit_ms"]
        assert isinstance(expected, dict)
        assert daemon.write_fail_exit.limit_ms == expected["value"]
    finally:
        daemon.close()


# --------------------------------------------------------- 4. faulthandler（§2.5）


def test_main_enables_faulthandler_for_every_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """watchdog の SIGABRT で全スレッドの traceback が残るよう、起動の最初に有効にする。"""
    calls: list[dict[str, object]] = []
    fake = SimpleNamespace(
        is_enabled=lambda: False,
        enable=lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(control_daemon, "faulthandler", fake)
    # 最も早く終わる経路（fan-hardware.yaml が無い）でも有効にしてから終える
    main(["--config-dir", str(tmp_path / "missing")])

    assert calls == [{"all_threads": True}]


def test_a_faulthandler_failure_does_not_stop_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """診断の欠落で冷却の制御を失わない（error に残して続ける）。"""

    def broken(**_kwargs: object) -> None:
        raise RuntimeError("sys.stderr is None")

    monkeypatch.setattr(
        control_daemon, "faulthandler", SimpleNamespace(is_enabled=lambda: False, enable=broken)
    )
    with caplog.at_level("ERROR", logger="coldaisle"):
        control_daemon._enable_faulthandler()

    assert any("faulthandler" in record.getMessage() for record in caplog.records)


SIGABRT_BIT = 1 << (signal.SIGABRT - 1)
"""`/proc/<pid>/task/<tid>/status` の `SigBlk`（16 進のマスク）での SIGABRT の位置。"""

linux_proc = pytest.mark.skipif(
    not Path("/proc/self/task").is_dir(),
    reason="スレッドごとのシグナルマスクを /proc で読む（Linux）",
)


def _sigabrt_blocked_threads(pid: int | str) -> list[str]:
    """SIGABRT を塞いでいるスレッドの tid を返す（`SigBlk` を読む）。"""
    blocked: list[str] = []
    for task in sorted(Path(f"/proc/{pid}/task").iterdir()):
        try:
            status = (task / "status").read_text(encoding="ascii")
        except (FileNotFoundError, ProcessLookupError):
            continue  # 読む間に終わったスレッド
        mask = next(line for line in status.splitlines() if line.startswith("SigBlk:"))
        if int(mask.split()[1], 16) & SIGABRT_BIT:
            blocked.append(task.name)
    return blocked


def test_main_leaves_sigabrt_at_the_default_disposition_after_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動の後も SIGABRT は既定（`SIG_DFL`）のまま（決定記録 0158 §2.5 の T2）。

    Python の処理関数は main thread がバイトコードに戻るまで動かないので、置くとユーザー空間の
    固まりが SIGABRT で終わらず、約 5 s の経路が約 11 s の経路（SIGKILL）に変わりうる（0158 §2.3）。
    `faulthandler` は C の処理関数で、`signal.getsignal` には現れない。
    """
    _confirmed_documents(tmp_path)
    seen: list[object] = []
    original_run = ControlDaemon.run

    def observing_run(self: ControlDaemon, max_ticks: int | None = None) -> Any:
        # tick を回している間（SIGTERM・SIGINT の処理関数を置いた後）の状態を見る
        seen.append(signal.getsignal(signal.SIGABRT))
        stats = original_run(self, max_ticks=max_ticks)
        seen.append(signal.getsignal(signal.SIGABRT))
        return stats

    monkeypatch.setattr(ControlDaemon, "run", observing_run)
    assert signal.getsignal(signal.SIGABRT) is signal.SIG_DFL, "試験の前提（pytest が置いていない）"

    assert main(_argv(tmp_path)) == 0
    assert seen == [signal.SIG_DFL, signal.SIG_DFL]
    assert signal.getsignal(signal.SIGABRT) is signal.SIG_DFL


@linux_proc
def test_main_does_not_block_sigabrt_on_any_thread_after_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`faulthandler` を有効にした後に、SIGABRT をどのスレッドでも塞がない（0158 §2.5 の T3）。

    塞がれた SIGABRT は届かず、SIGKILL の経路（約 11 s）になる。main thread は
    `pthread_sigmask` で、tick の間に生きている全スレッドは `/proc/self/task` で見る。
    """
    _confirmed_documents(tmp_path)
    main_masks: list[set[signal.Signals]] = []
    blocked: list[list[str]] = []
    original_run = ControlDaemon.run

    def observing_run(self: ControlDaemon, max_ticks: int | None = None) -> Any:
        main_masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
        blocked.append(_sigabrt_blocked_threads("self"))
        return original_run(self, max_ticks=max_ticks)

    monkeypatch.setattr(ControlDaemon, "run", observing_run)
    assert _sigabrt_blocked_threads("self") == [], "試験の前提（pytest が塞いでいない）"

    assert main(_argv(tmp_path)) == 0
    assert len(main_masks) == 1
    assert signal.SIGABRT not in main_masks[0]
    assert blocked == [[]]


@linux_proc
def test_a_running_fand_process_ends_by_sigabrt_with_a_traceback(tmp_path: Path) -> None:
    """動いている fand の process は SIGABRT で traceback を残して既定の動作で終わる。

    決定記録 0158 §2.5 の T2・T3 を、別の process の実際の終わり方で確かめる。

    systemd の `WatchdogSignal=SIGABRT` の代わりに試験から送る（systemd の経路全体は試さない。
    0158 §5 の 4）。別の process で確かめるのは、pytest が `faulthandler` を先に有効にしているため、
    同じ process では fand 自身が有効にした状態を作れないから。simulated backend で実機は要らない。
    """
    _confirmed_documents(tmp_path)
    argv = _argv(tmp_path)[:-2]  # --max-ticks を外し、止めるまで回す
    log = tmp_path / "fand.stderr"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {NOTIFY_SOCKET_ENV, WATCHDOG_USEC_ENV, WATCHDOG_PID_ENV}
    }

    def no_core() -> None:
        # unit の LimitCORE=0 と同じ（core の書き出しで終わりを遅らせない）
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    with log.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "coldaisle.control_daemon", *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            env=env,
            preexec_fn=no_core,
        )
        try:
            deadline = time.monotonic() + 60
            while '"msg": "control tick"' not in log.read_text(encoding="utf-8", errors="replace"):
                assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")
                assert time.monotonic() < deadline, "fand が tick を始めない"
                time.sleep(0.05)

            assert _sigabrt_blocked_threads(process.pid) == []
            process.send_signal(signal.SIGABRT)
            returncode = process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    output = log.read_text(encoding="utf-8", errors="replace")
    assert returncode == -signal.SIGABRT, output
    assert "Fatal Python error: Aborted" in output  # faulthandler が書いてから終わった


# ------------------------------------------------ 5. 起動時の DB の書き込み確認（§2.1）


def _make_db(path: Path) -> None:
    """取り込みが作ったのと同じ形の DB を置く（schema まで作って閉じる）。"""
    from coldaisle.clock import SimulatedClock

    SqliteStore(
        path,
        rules=QualityRules.from_yaml(CONFIG_DIR / "quality.yaml"),
        clock=SimulatedClock(TEST_EPOCH_MS),
    ).close()


def _chmod_during(path: Path, mode: int) -> Callable[[], None]:
    original = path.stat().st_mode & 0o7777
    path.chmod(mode)
    return lambda: path.chmod(original)


@running_as_root
@pytest.mark.parametrize(
    ("target", "mode"),
    [
        ("db", 0o440),
        ("shm", 0o440),
        ("directory", 0o550),
    ],
)
def test_an_unwritable_db_is_reported_and_control_is_not_taken(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    mode: int,
) -> None:
    """書けない DB では制御を取らず、`db_not_writable` に path と権限を載せて終了コード 5。

    `-shm` だけが古い mode で残る移行の取りこぼし（0080 §2.1 の手順 3）も名指しする。
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _confirmed_documents(config_dir)
    state = tmp_path / "state"
    state.mkdir()
    db = state / "coldaisle.db"
    _make_db(db)
    shm = state / "coldaisle.db-shm"
    shm.write_bytes(b"")
    offending = {"db": db, "shm": shm, "directory": state}[target]
    ran: list[object] = []

    def never_run(self: ControlDaemon, **kwargs: object) -> object:
        # tick を1つでも回せば takeover（STARTUP の Max）になる。ここへ来てはならない
        ran.append(kwargs)
        raise AssertionError("制御を取ってはならない")

    monkeypatch.setattr(ControlDaemon, "run", never_run)
    restore = _chmod_during(offending, mode)
    try:
        with caplog.at_level("INFO", logger="coldaisle"):
            code = main(_argv(config_dir, db=db))
        with pytest.raises(DbNotWritableError):
            build(
                Config(
                    config_dir=config_dir,
                    db=db,
                    metrics=METRICS_PATH,
                    quality_rules=CONFIG_DIR / "quality.yaml",
                    authority_root=config_dir / "authority",
                )
            )
    finally:
        restore()

    assert code == EXIT_STARTUP_ENVIRONMENT
    assert ran == []
    events = _events(caplog, "db_not_writable")
    assert len(events) == 1
    event = events[0]
    assert event["path"] == str(offending)
    assert event["mode"] == f"{mode:04o}"
    assert event["gid"] == offending.stat().st_gid
    assert event["owner_uid"] == offending.stat().st_uid
    assert event["fand"]["uid"] == os.geteuid()
    assert event["fand"]["supplementary_groups"] == sorted(os.getgroups())
    json.dumps(event)


def test_a_writable_db_lets_startup_continue(tmp_path: Path) -> None:
    """書ける DB（既存・新規のどちらも）では起動が続く。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _confirmed_documents(config_dir)
    existing = tmp_path / "existing.db"
    _make_db(existing)

    assert main(_argv(config_dir, db=existing)) == 0
    assert main(_argv(config_dir, db=tmp_path / "fresh.db")) == 0


def test_a_busy_db_is_not_reported_as_a_permission_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """取り込みの書き込みと重なった `SQLITE_BUSY` は `db_not_writable` にしない。

    `busy_timeout`（`tick_deadline_ms`）まで待って取れなければ、lock を取れないだけとして
    別の文言で報告する（終了コード 5 は同じ）。
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _confirmed_documents(config_dir)
    db = tmp_path / "coldaisle.db"
    _make_db(db)
    holder = sqlite3.connect(str(db), isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(DbLockUnavailableError):
            build(
                Config(
                    config_dir=config_dir,
                    db=db,
                    metrics=METRICS_PATH,
                    quality_rules=CONFIG_DIR / "quality.yaml",
                    authority_root=config_dir / "authority",
                )
            )
        with caplog.at_level("INFO", logger="coldaisle"):
            code = main(_argv(config_dir, db=db))
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert code == EXIT_STARTUP_ENVIRONMENT
    assert _events(caplog, "db_not_writable") == []
    assert any("lock を取れない" in record.getMessage() for record in caplog.records)


def test_a_missing_db_directory_is_not_reported_as_a_permission_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ディレクトリが無いのは配置の誤りで、権限の問題として報告しない。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _confirmed_documents(config_dir)

    with caplog.at_level("INFO", logger="coldaisle"):
        code = main(_argv(config_dir, db=tmp_path / "missing" / "coldaisle.db"))

    assert code == EXIT_STARTUP_ENVIRONMENT
    assert _events(caplog, "db_not_writable") == []
