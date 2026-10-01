"""3系統 Fan 制御デーモン（`coldaisle-fand`）の合成と常駐ループ。#74

決定記録 0028 §2.2 のとおり、**hwmon の PWM へ書き込むのはこのプロセスの Hardware Backend
だけ**である。M8 の時点で実機へ到達する backend は無く（#77）、ここが組み立てるのは
`SimulatedFanBackend` だけ。実機の backend は #57 の権限・引き継ぎと #75 の実測が揃ってから
同じ `FanHardwareBackend` として差し込む。

起動時の設定の扱いは 0028 §2.7 に従う。

| 状態 | 動作 |
|---|---|
| `fan-hardware.yaml` が不正 | **制御を取らない。** BIOS の制御のまま 0 以外で終了する |
| hardware は正しいが承認前（`provisional`） | 制御を取らない（0028 §2.9 の承認点 3） |
| hardware は正しく、ほかが不正・不在 | 制御を取り、**全 zone を Max**（`config_invalid`） |
| すべて正しく air-balance が `uncalibrated` | Air Balance を**無効**にして通常運転 |
| すべて正しく air-balance が `calibrated` | Air Balance を有効にして通常運転 |

「ほか」は `safety.yaml` / `fan-policy.yaml` / `air-balance.yaml`。Air Balance を無効にしても
Critical Safety と Reactive Guard は変わらない。

どの場合も `STARTUP` の Max を通ってから通常の制御へ入る。

制御権（Authority Stage）の正本は `--authority-root` の `authority.json`（`AuthorityStore`。
決定記録 0057）で、`AuthorityRuntime` が毎 tick 読み、管理ソケットの降格を受け、外の process
（人の CLI）が journal を変えたことを heartbeat の後の `stat` で知る（決定記録 0072 §2.6）。
**起動時に journal を読めなければ制御を取らない**（0057 §2.1。壊れた journal を「記録の無い
状態」と読み替えない）。走行中に読めなくなったら止めずに `SHADOW` へ下げる。

運転モードは管理ソケット（`config/control-admin.yaml`。決定記録 0072）から受ける。
**入口を開けなくても制御は止めない**（設定が不正・`SO_PEERCRED` が無いときは `AUTO` のまま
運転し、error を残す）。受付スレッドが走行中に死んだら、loop が自分で全 zone を Max にして
再起動まで保つ（0072 §2.2）。`air-balance.yaml` の扱いは
決定記録 0073 §2.2 に従う。「無い」を「無効」と読まない。

**動作中に設定を読み直さない**（0028 §2.7）。反映は再起動で行い、再起動は必ず
`STARTUP` の Max を通る。

終了コードは決定記録 0080 §2.4 に従う。unit の `RestartPreventExitStatus=3 4` と対になる
（3 と 4 は人が直すまで結果が変わらない失敗だけに使い、一時的でありうる失敗は 5〜7 にする）。

| 終了 | 意味 | 制御を取ったか | 再起動 |
|---|---|---|---|
| 1 | `config_invalid` で Max を書いた（未捕捉の例外も 1） | 取った | する |
| 2 | `fan-hardware.yaml` が不正 | 取っていない | する |
| 3 | hardware mapping が `provisional` | 取っていない | しない |
| 4 | deadman が恒久的に使えない（`WATCHDOG_USEC` の不在・不正・長短） | 取っていない | しない |
| 5 | 制御設定以外（DB に書けない `db_not_writable` を含む） | 取っていない | する |
| 6 | 通知の I/O の失敗（socket を作れない・`READY=1` を送れない） | 取っていない | する |
| 7 | takeover 後に書き込みの失敗が `hardware_write_fail_exit_ms` 続いた | 取った | する |
"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import os
import signal
import socket
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

from coldaisle import logs
from coldaisle.clock import Clock, MonotonicClock, SystemMonotonicClock, WallClock
from coldaisle.control.authority import AuthorityRuntime, AuthorityStore
from coldaisle.control.config import (
    CONFIG_FILENAMES,
    ControlConfig,
    FanHardwareConfig,
    SafetyConfig,
    load_fan_hardware_document,
)
from coldaisle.control.fallback.controller import FallbackController
from coldaisle.control.fallback.gate import ControllerGate
from coldaisle.control.hardware.simulated import (
    FanHardwareBackend,
    FanHardwareResult,
    SimulatedFanBackend,
)
from coldaisle.control.logging import ControlTraceLogger
from coldaisle.control.loop import (
    ControlLoop,
    ControlTickResult,
    StaticOperatingMode,
    TelemetrySample,
    Watchdog,
    air_balance_input_metrics,
    build_input_contract,
)
from coldaisle.control.operating_mode import AdminAuthorityCommand
from coldaisle.control.reactive.guard import ReactiveGuard
from coldaisle.control.safety.critical import (
    ControlRuntimeBinding,
    CriticalSafety,
    DemandComposer,
    create_control_runtime_binding,
    create_emergency_control_runtime,
)
from coldaisle.control.safety.write_fail_exit import (
    HardwareWriteFailureExit,
    HardwareWriteFailureExitError,
)
from coldaisle.control.schema import PerZone, RegistryProvenance, Zone
from coldaisle.control.shadow.record import ShadowRecorder
from coldaisle.control.state import ControlInputContract, ControlStateEstimator
from coldaisle.control.supervisor.policy import SupervisorCoordinator
from coldaisle.control.supervisor.regime import WorkloadRegimeEstimator
from coldaisle.control_admin import ControlAdminEntry, open_control_admin
from coldaisle.metrics import MetricCatalog
from coldaisle.store import QualityRules, SqliteStore

LOGGER = logging.getLogger("coldaisle.control")

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_DB = Path("var/coldaisle.db")
DEFAULT_METRICS = Path("config/metrics.yaml")
DEFAULT_QUALITY_RULES = Path("config/quality.yaml")
DEFAULT_ADMIN_CONFIG = Path("config/control-admin.yaml")
DEFAULT_AUTHORITY_ROOT = Path("var/authority")
"""`authority.json` を置くディレクトリ（`--db` と同じく場所であって閾値ではない）。

本番は `coldaisle-fand` の実行ユーザーと昇格を行う人だけが書ける場所を指定する（0072 §2.5）。
"""

UNCONFIGURED_MODEL_VERSION = "unconfigured"
"""Learned MPC の worker を配線していない起動で Gate に渡す期待版。

**値そのものに意味は無い。** worker が無い構成では提案が1件も来ないので Gate は必ず
Fallback を選ぶ。worker を配線するときは、Registry の production 版を明示的に渡す。
"""

EXIT_HARDWARE_CONFIG_INVALID = 2
"""`fan-hardware.yaml` が不正で、制御を取らずに終了した（0028 §2.7）。"""

EXIT_ACTUATION_NOT_APPROVED = 3
"""hardware mapping が `provisional` で、実機の制御を取る承認が無い（0028 §2.9）。"""

EXIT_WATCHDOG_UNAVAILABLE = 4
"""外部の deadman が**恒久的に**使えない（0028 §2.6 / 決定記録 0060 §2.7 / 0080 §2.3・§2.4）。

unit か `safety.yaml` の食い違いで、人が直すまで結果が変わらない。**4 はこの意味だけに使う**
（`RestartPreventExitStatus` で再起動しない）。
"""

EXIT_STARTUP_ENVIRONMENT = 5
"""制御設定**以外**の理由で起動できない。**制御を取らない**（BIOS の制御のまま）。

DB に書けない（`db_not_writable`。決定記録 0080 §2.1）もここに入る。人が権限を直せば次の
起動でそのまま戻れるよう、再起動する側の終了コードにする。
"""

EXIT_NOTIFY_IO_FAILED = 6
"""通知の I/O の失敗（socket を作れない・`READY=1` を送れない。決定記録 0080 §2.4）。

fd の枯渇や systemd 側の一時的な不調など、同じ設定で次は通りうる。4 に混ぜると
`RestartPreventExitStatus` で一時的な失敗のまま制御が戻らない。**制御は取っていない**
（`READY=1` は takeover の前に送る）。
"""

EXIT_HARDWARE_WRITE_FAILED = 7
"""takeover 後、hwmon へ書けない状態が `hardware_write_fail_exit_ms` 続いた（0080 §2.6）。

**引き継ぎ記録を消さずに**終わり、`ExecStopPost` が root で Max を書いてから再起動する。
"""

NOTIFY_SOCKET_ENV = "NOTIFY_SOCKET"
"""systemd が `Type=notify` のサービスへ渡す通知先。**この名前は ABI で、調整値ではない。**"""

WATCHDOG_USEC_ENV = "WATCHDOG_USEC"
"""`WatchdogSec` が有効なときだけ渡る時間切れ（マイクロ秒）。**deadman の有無はこれで決まる。**"""

WATCHDOG_PID_ENV = "WATCHDOG_PID"
"""`WATCHDOG_USEC` が誰宛かを示す PID。自分宛でなければ deadman は自分を見ていない。"""

WATCHDOG_DATAGRAM = b"WATCHDOG=1"
READY_DATAGRAM = b"READY=1"
"""sd_notify の ABI。値を組み立てる余地を残さない。"""

HEARTBEAT_INTERVALS_PER_TIMEOUT = 2
"""時間切れのあいだに入れる heartbeat の最低回数。

systemd が `WatchdogSec` の半分の間隔で通知することを求めている ABI 側の前提で、
運用で調整する値ではない。`safety.yaml` の側も同じ式で検証している。
"""


def heartbeat_interval_ms(safety: SafetyConfig) -> int:
    """heartbeat の間隔として見込む最悪値（ミリ秒）。

    heartbeat は tick ごとにしか出ない。次の tick が始まるまで（最悪 `tick_ms`）と、
    その tick の処理（最悪 `tick_deadline_ms`）を合わせた分だけ空きうる。
    """
    return safety.tick_ms.value + safety.tick_deadline_ms.value


class WatchdogUnavailableError(RuntimeError):
    """外部の deadman が**恒久的に**使えない（終了コード 4。決定記録 0080 §2.4）。

    通知先が無い・時間切れが無効・間隔が足りない・`safety.yaml` より長い、のどれか。
    **通知の I/O の失敗はここに入れない**（`WatchdogNotifyIoError`）。
    """


class WatchdogNotifyIoError(RuntimeError):
    """通知の I/O の失敗（socket を作れない・送れない。終了コード 6。決定記録 0080 §2.4）。

    **`WatchdogUnavailableError` の派生にしない。** 派生にすると、呼び出し側の
    `except WatchdogUnavailableError` に吸われて終了コード 4（再起動しない）になり、
    一時的な失敗のまま制御が戻らない。
    """


class DbNotWritableError(RuntimeError):
    """DB（`--db`）に権限の理由で書けない（`db_not_writable`。決定記録 0080 §2.1）。

    ``fields`` は構造化ログにそのまま載せる。書けなかった path・mode・所有者・gid と、
    fand の uid・gid・補助グループを持つ。
    """

    def __init__(self, message: str, *, fields: dict[str, object]) -> None:
        super().__init__(message)
        self.fields = fields


class DbLockUnavailableError(RuntimeError):
    """DB の書き込みの lock を `busy_timeout` の間に取れなかった（`SQLITE_BUSY`）。

    **権限の失敗と取り違えない**（決定記録 0080 §2.1）。取り込みの書き込みと重なっただけで、
    権限を直す必要は無い。
    """


class ControlConfigInvalidError(RuntimeError):
    """`safety.yaml` / `fan-policy.yaml` / `air-balance.yaml` が無い・不正。

    0028 §2.7 の「全 zone Max」の条件。`air-balance.yaml` の不在と、`thermal_inputs` の
    metric を Metric Catalog で検証できないことも含む（決定記録 0073 §2.2 / §2.4）。
    """


class StartupEnvironmentError(RuntimeError):
    """制御設定**以外**（Metric Catalog・品質規則・ストアなど）の理由で起動できない。

    **0028 §2.7 の「設定不正なら Max」には当たらない。** あの規則は制御設定そのものが
    読めない場合のもので、ここに混ぜると「DB が一時的に開けない」だけで Max を書く
    ことになる。制御を取らず、BIOS の制御（Safety-0）のまま終了する。
    """


class SystemdWatchdog:
    """`NOTIFY_SOCKET` へ `WATCHDOG=1` を送る deadman（0028 §2.6）。

    **書けるのはこの2つの固定 datagram だけ**で、値を引数から組み立てない。
    送信に失敗したら例外にする。握りつぶすと、heartbeat が届いていないのに
    「通知した」ことになり、deadman が無いのと同じになる。
    """

    __slots__ = ("_address", "_socket")

    def __init__(self, address: str) -> None:
        if not address:
            raise WatchdogUnavailableError(f"{NOTIFY_SOCKET_ENV} が空")
        # systemd の abstract namespace（先頭が `@`）を AF_UNIX の表現へ直す。
        self._address = "\0" + address[1:] if address.startswith("@") else address
        try:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
        except OSError as error:
            # fd の枯渇など一時的でありうる。再起動する終了コード 6（決定記録 0080 §2.4）
            raise WatchdogNotifyIoError(f"通知用 socket を作れない: {error}") from error

    def ready(self) -> None:
        """起動を伝える（`Type=notify` の service が待っている）。"""
        self._send(READY_DATAGRAM)

    def notify(self) -> None:
        """1 tick を書き終えたことを伝える。"""
        self._send(WATCHDOG_DATAGRAM)

    def close(self) -> None:
        """socket を閉じる。"""
        self._socket.close()

    def _send(self, payload: bytes) -> None:
        """**失敗を素の `OSError` のまま外へ出さない。**

        通知先が消えている・権限が無いといった失敗を素通しすると、呼び出し側が
        「設定が不正」など**別の種類の失敗**として扱ってしまう（起動時の分類が狂う）。
        """
        try:
            self._socket.sendto(payload, self._address)
        except OSError as error:
            # 設定の食い違いではなく I/O の失敗。再起動する終了コード 6（決定記録 0080 §2.4）
            raise WatchdogNotifyIoError(f"{NOTIFY_SOCKET_ENV} へ通知できない: {error}") from error


class UnsupervisedWatchdog:
    """外部の deadman が無い環境（手元実行・試験）の代わり。**黙って何もしない訳ではない。**

    `NullWatchdog` をそのまま本番に置くと、hang しても誰も気づかない。この実装は
    起動時に「deadman が無い」ことを error として残し、heartbeat の間隔が
    `watchdog_timeout_ms` を超えたらそのつど記録する。**プロセスを殺す力は無い**ので、
    本番の service では `--require-watchdog` で `SystemdWatchdog` を必須にする。
    """

    __slots__ = ("_last_mono_ms", "_monotonic", "_timeout_ms")

    def __init__(self, *, timeout_ms: int, monotonic: MonotonicClock) -> None:
        self._timeout_ms = timeout_ms
        self._monotonic = monotonic
        self._last_mono_ms: int | None = None
        LOGGER.error(
            "外部の deadman が無い状態で起動する（hang しても停止させられない）",
            extra={
                logs.FIELDS_KEY: {
                    "notify_socket": None,
                    "watchdog_timeout_ms": timeout_ms,
                    "hint": "systemd の Type=notify と WatchdogSec、または --require-watchdog",
                }
            },
        )

    def notify(self) -> None:
        """heartbeat の間隔だけを見る。**遅れを黙って捨てない。**"""
        now_ms = self._monotonic.monotonic_ms()
        previous = self._last_mono_ms
        self._last_mono_ms = now_ms
        if previous is not None and now_ms - previous > self._timeout_ms:
            LOGGER.error(
                "control tick の heartbeat が deadman の時間切れを超えた",
                extra={
                    logs.FIELDS_KEY: {
                        "gap_ms": now_ms - previous,
                        "watchdog_timeout_ms": self._timeout_ms,
                    }
                },
            )


def enabled_watchdog_interval_ms(environ: Mapping[str, str]) -> int | None:
    """systemd が**この process に対して**有効にした deadman の時間切れ（ミリ秒。切り捨て）。

    **`NOTIFY_SOCKET` の有無を deadman の証拠にしない。** 通知先は `Type=notify` なら
    `WatchdogSec` が無くても渡るので、それだけを見ると「`WATCHDOG=1` は届くが誰も
    見ていない」状態を「deadman あり」と誤認する（sd_watchdog_enabled(3) と同じ判定にする）。
    """
    usec = enabled_watchdog_usec(environ)
    return None if usec is None else usec // 1_000


def enabled_watchdog_usec(environ: Mapping[str, str]) -> int | None:
    """`enabled_watchdog_interval_ms` と同じ判定で、値をマイクロ秒のまま返す。

    `safety.yaml` より長いかの比較（決定記録 0080 §2.3）はこちらで行う。ミリ秒へ切り捨てて
    から比べると、1ms 未満だけ長い `WatchdogSec` を「等しい」と読んで通してしまう。
    """
    raw = environ.get(WATCHDOG_USEC_ENV, "")
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        LOGGER.error(
            "watchdog の時間切れが整数ではない",
            extra={logs.FIELDS_KEY: {WATCHDOG_USEC_ENV: raw}},
        )
        return None
    if usec <= 0:
        return None
    owner = environ.get(WATCHDOG_PID_ENV, "")
    if owner:
        try:
            owner_pid = int(owner)
        except ValueError:
            LOGGER.error(
                "watchdog の宛先 PID が整数ではない",
                extra={logs.FIELDS_KEY: {WATCHDOG_PID_ENV: owner}},
            )
            return None
        if owner_pid != os.getpid():
            # 親から引き継いだ環境変数。この process の heartbeat は見られていない。
            LOGGER.error(
                "watchdog の宛先がこの process ではない",
                extra={logs.FIELDS_KEY: {WATCHDOG_PID_ENV: owner_pid, "pid": os.getpid()}},
            )
            return None
    return usec


def create_watchdog(
    *,
    interval_ms: int,
    timeout_ms: int,
    monotonic: MonotonicClock,
    require: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Watchdog:
    """環境に応じた deadman を作る。**既定を無音の no-op にしない。**

    `timeout_ms` は `safety.yaml` が意図した時間切れ、実際に効くのは systemd が渡す
    `WATCHDOG_USEC` である。**効くほうで間隔を検証する。**

    `WATCHDOG_USEC` が `safety.yaml` の `watchdog_timeout_ms` より**長い**ときは起動しない
    （終了コード 4。決定記録 0080 §2.3）。所有者が承認した deadman より遅い deadman で
    運転することになるからである。短いときは起動する（より厳しい deadman）が warning を残す。
    どちらの検査も `READY=1` を送る前（takeover の前）に行う。

    恒久的な食い違いは `WatchdogUnavailableError`（終了コード 4）、通知の I/O の失敗は
    `WatchdogNotifyIoError`（終了コード 6）で返す（0080 §2.4）。
    """
    env = os.environ if environ is None else environ
    address = env.get(NOTIFY_SOCKET_ENV, "")
    enabled_usec = enabled_watchdog_usec(env)
    enabled_ms = None if enabled_usec is None else enabled_usec // 1_000
    if enabled_usec is not None and enabled_ms is not None:
        # 時間切れが有効でも、heartbeat が tick ごとにしか出ない以上、間隔が足りなければ
        # 健全な運転でも殺される。**再起動を繰り返す構成で制御を取らない。**
        required_ms = interval_ms * HEARTBEAT_INTERVALS_PER_TIMEOUT
        if enabled_ms < required_ms:
            raise WatchdogUnavailableError(
                f"{WATCHDOG_USEC_ENV} が heartbeat の間隔に対して短すぎる: "
                f"watchdog={enabled_ms}ms; heartbeat_interval={interval_ms}ms; "
                f"required>={required_ms}ms"
            )
        if enabled_usec > timeout_ms * 1_000:
            raise WatchdogUnavailableError(
                f"{WATCHDOG_USEC_ENV} が safety.yaml の watchdog_timeout_ms より長い: "
                f"watchdog={enabled_usec}us; watchdog_timeout_ms={timeout_ms}ms"
                "（WatchdogSec を watchdog_timeout_ms と同じ値にする。決定記録 0080 §2.3）"
            )
        if enabled_usec < timeout_ms * 1_000:
            LOGGER.warning(
                "systemd の WatchdogSec と safety.yaml の watchdog_timeout_ms が違う",
                extra={
                    logs.FIELDS_KEY: {
                        "systemd_watchdog_ms": enabled_ms,
                        "watchdog_timeout_ms": timeout_ms,
                    }
                },
            )
    if address and enabled_ms is not None:
        watchdog = SystemdWatchdog(address)
        watchdog.ready()
        LOGGER.info(
            "systemd の deadman へ heartbeat を送る",
            extra={
                logs.FIELDS_KEY: {
                    "systemd_watchdog_ms": enabled_ms,
                    "watchdog_timeout_ms": timeout_ms,
                    "heartbeat_interval_ms": interval_ms,
                }
            },
        )
        return watchdog
    if require:
        missing = []
        if not address:
            missing.append(NOTIFY_SOCKET_ENV)
        if enabled_ms is None:
            missing.append(f"有効な {WATCHDOG_USEC_ENV}")
        raise WatchdogUnavailableError(
            f"--require-watchdog が指定されているのに deadman が無い（{', '.join(missing)}）"
        )
    return UnsupervisedWatchdog(timeout_ms=timeout_ms, monotonic=monotonic)


DB_SIDE_FILE_SUFFIXES = ("-wal", "-shm", "-journal")
"""SQLite が DB の隣に作るファイル（ABI。調整値ではない）。1つでも書けなければ DB に書けない。"""

_SQLITE_PERMISSION_CODES = frozenset(
    {sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH}
)
"""権限による失敗として報告する SQLite の主エラーコード（拡張コードの下位 8 bit）。"""

_SQLITE_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
"""lock を取れなかっただけの失敗。**権限の問題と誤って報告しない**（決定記録 0080 §2.1）。"""


def _process_identity() -> dict[str, object]:
    """fand の uid・gid・補助グループ。権限の失敗の原因を journal だけで追えるようにする。"""
    return {
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "supplementary_groups": sorted(os.getgroups()),
    }


def _path_fields(path: Path) -> dict[str, object]:
    """path の mode・所有者・gid。読めなければその理由を残す（推測で埋めない）。"""
    try:
        info = path.stat()
    except OSError as error:
        return {"path": str(path), "stat_error": f"{type(error).__name__}: {error}"}
    return {
        "path": str(path),
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
        "owner_uid": info.st_uid,
        "gid": info.st_gid,
    }


def _not_writable(path: Path, reason: str) -> DbNotWritableError:
    fields: dict[str, object] = {
        "event": "db_not_writable",
        **_path_fields(path),
        "reason": reason,
        "fand": _process_identity(),
    }
    return DbNotWritableError(f"DB に書けない: {path}（{reason}）", fields=fields)


def check_db_paths_writable(db: Path) -> None:
    """DB のディレクトリ・DB・あれば `-wal` / `-shm` / `-journal` に書けるかを見る（0080 §2.1）。

    **SQLite を開く前に見る。** 開いてから権限で落ちると、どのファイルが原因かが SQLite の
    文言からは分からない。既存の導入先で `-shm` だけが古い mode のまま残る場合を名指しできる
    ようにする。書けなければ `DbNotWritableError`（終了コード 5）。

    root では `os.access` が常に真になるので、この検査は何も見つけない（試験は skip する）。
    """
    directory = db.parent if str(db.parent) else Path(".")
    if not directory.is_dir():
        # 権限ではなく配置の誤り。db_not_writable と取り違えない（終了コード 5 は同じ）
        raise StartupEnvironmentError(f"DB のディレクトリが無い: {directory}")
    # ファイルを作るには書き込み権、名前を引くには実行権が要る
    if not os.access(directory, os.W_OK | os.X_OK):
        raise _not_writable(directory, "DB のディレクトリに書き込み・実行の権限が無い")
    for path in (db, *(db.with_name(db.name + suffix) for suffix in DB_SIDE_FILE_SUFFIXES)):
        if path.exists() and not os.access(path, os.R_OK | os.W_OK):
            raise _not_writable(path, "読み書きの権限が無い")


def classify_sqlite_open_error(db: Path, error: sqlite3.Error) -> RuntimeError:
    """SQLite の失敗を「権限」「lock を取れない」「その他」に分ける（0080 §2.1）。

    返すのは `DbNotWritableError` / `DbLockUnavailableError` / `StartupEnvironmentError`。
    どれも終了コード 5 だが、**報告の文言と event を取り違えない。**
    """
    code = getattr(error, "sqlite_errorcode", None)
    primary = None if code is None else code & 0xFF
    detail = f"{type(error).__name__}: {error}"
    if primary in _SQLITE_BUSY_CODES:
        return DbLockUnavailableError(
            f"DB の書き込みの lock を busy_timeout の間に取れなかった（権限の問題ではない）: "
            f"{db}: {detail}"
        )
    if primary in _SQLITE_PERMISSION_CODES:
        return _not_writable(db, detail)
    return StartupEnvironmentError(f"DB を開けない: {db}: {detail}")


def check_db_write_lock(store: SqliteStore, db: Path) -> None:
    """SQLite に実際に書き込みの lock を取らせる（`BEGIN IMMEDIATE` → `ROLLBACK`。行は書かない）。

    `os.access` は ACL・読み取り専用の mount・SELinux などを見落とす。**制御を取る前に**、
    decision trace と監査を書く経路が本当に書けるかを確かめる（決定記録 0080 §2.1）。
    待ちの上限は接続の `busy_timeout`（`tick_deadline_ms`）で、取れなければ
    `DbLockUnavailableError`（権限の失敗とは別の文言）。
    """
    connection = store.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as error:
        raise classify_sqlite_open_error(db, error) from error
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as error:
        raise classify_sqlite_open_error(db, error) from error


@dataclass(frozen=True, slots=True)
class Config:
    """CLI から組み立てるファイル位置と配線の選択。"""

    config_dir: Path = DEFAULT_CONFIG_DIR
    db: Path = DEFAULT_DB
    metrics: Path = DEFAULT_METRICS
    quality_rules: Path = DEFAULT_QUALITY_RULES
    t_sensor_metric: str | None = None
    record_trace: bool = True
    require_watchdog: bool = False
    """外部の deadman へ通知できないときに起動を拒むか（本番の service では真にする）。"""
    admin_config: Path | None = None
    """管理ソケットの設定（決定記録 0072 §2.8）。None は入口を開かない（`AUTO` のまま運転する）。"""
    authority_root: Path = DEFAULT_AUTHORITY_ROOT
    """`authority.json` のディレクトリ（0057 §2.1）。相対 path は起動時の作業場所が基準。"""


@dataclass(slots=True)
class ControlStats:
    """1回の実行で起きたこと。"""

    ticks: int = 0
    overruns: int = 0
    skipped_slots: int = 0
    """処理が周期を超えて飛ばした control tick の枠。追いつくために連続実行しない。"""
    recorded: int = 0
    trace_dropped: int = 0
    """保存できなかった decision trace の数。**落ちた記録を黙って捨てない。**"""
    emergency_max: bool = False

    def as_fields(self) -> dict[str, int | bool]:
        """構造化ログへそのまま載せる形。"""
        return {
            "ticks": self.ticks,
            "overruns": self.overruns,
            "skipped_slots": self.skipped_slots,
            "recorded": self.recorded,
            "trace_dropped": self.trace_dropped,
            "emergency_max": self.emergency_max,
        }


class StoreTelemetrySource:
    """読み取り専用のストアから、制御が読む metric の最新値を取り出す（決定記録 0060 §2.2）。

    **シリアルも NVML も触らない**（AGENTS.md ルール6 / 決定記録 0001 D-07）。取り込み
    デーモン（`air.*`）と Telemetry Collector（#65）が同じ SQLite へ書いた行を読むだけである。

    ストアは `quality.yaml` のしきい値で古い行を `stale` に落とすが、制御はそのうえで
    **自分の許容遅延**（`safety.yaml`）を重ねる。2つの判定は重なるほど保守側にしか動かない
    ので、制御が甘い側へ倒れることはない。
    """

    __slots__ = ("_metrics", "_store")

    def __init__(self, store: SqliteStore, metrics: frozenset[str]) -> None:
        self._store = store
        self._metrics = metrics

    def read(self) -> tuple[TelemetrySample, ...]:
        """契約にある metric の最新値だけを返す。**待たない。**"""
        latest = self._store.latest()
        return tuple(
            TelemetrySample(
                metric=metric,
                value=reading.value,
                quality=reading.quality,
                source_ts_ms=reading.ts_ms,
            )
            for metric, reading in sorted(latest.items())
            if metric in self._metrics
        )


BackendFactory = Callable[[FanHardwareConfig, ControlRuntimeBinding], FanHardwareBackend]


def simulated_backend(
    config: FanHardwareConfig, binding: ControlRuntimeBinding
) -> FanHardwareBackend:
    """M8 の唯一の backend。実機の backend は #57 / #75 のあとで同じ形で差し込む。"""
    return SimulatedFanBackend(config=config, runtime_binding=binding)


@dataclass(slots=True)
class ControlDaemon:
    """control tick を単調時計の締め切りで刻む常駐ループ。

    **遅れた tick を後から取り戻して連続実行しない**（0028 §2.6）。連続実行すると、
    遅れているときほど1 tick あたりの入力の新しさが揃わなくなる。
    """

    loop: ControlLoop
    monotonic: MonotonicClock
    store: SqliteStore | None = None
    admin: ControlAdminEntry | None = None
    """開いた管理ソケット。**loop が止まった後に**閉じる（`close()`）。"""
    authority: AuthorityRuntime | None = None
    """制御権の runtime。`close()` で書き残せていない降格を1回だけ書き直す（0057 §2.6）。"""
    write_fail_exit: HardwareWriteFailureExit | None = None
    """書き込みの失敗が続いたら終える判定（決定記録 0080 §2.6）。None は試験の足場だけ。"""
    sleep: Callable[[float], None] = time.sleep
    stats: ControlStats = field(default_factory=ControlStats)
    _stop: bool = field(default=False, init=False, repr=False)

    def request_stop(self) -> None:
        """次の tick の前に止める。**tick の途中では止めない。**"""
        self._stop = True

    def close(self, *, drain: bool = True, persist_without_wait: bool = False) -> None:
        """管理ソケット、ストアの順に閉じる。**loop が止まった後に呼ぶ**（0072 §2.2）。

        ``drain=False`` は `run()` が例外で抜けたとき。管理ソケットのスレッドを待たずに閉じ、
        process の終了（引き継ぎで Max。0028 §2.7）を遅らせない。

        受付スレッドを止めたあとで authority の枠に残っていた降格は、`AuthorityRuntime` へ
        入れてから既存の1回だけの書き残しに回す（SIGTERM が置いた直後に来ても、受理した
        降格を捨てない。0072 §2.6）。

        ``persist_without_wait=True`` は ``drain=False`` と組み合わせる終了コード 7 の経路
        （決定記録 0080 §2.6 / 0083）。スレッドは待たないが、残った降格は journal へ
        **lock を待たずに1回だけ**書き残しを試す（取れなければ error に残す）。
        """
        leftover: AdminAuthorityCommand | None = None
        if self.admin is not None:
            leftover = self.admin.stop(drain=drain)
            self.admin = None
        if self.authority is not None:
            _settle_authority_on_stop(
                self.authority, leftover, drain=drain, persist_without_wait=persist_without_wait
            )
            self.authority = None
        elif leftover is not None:
            _log_lowering_lost(leftover, "authority runtime が無い")
        if self.store is not None:
            self.store.close()
            self.store = None

    def run(self, *, max_ticks: int | None = None) -> ControlStats:
        """止めるまで tick を回す。`max_ticks` は試験と Replay のための上限。

        書き込みの失敗が `hardware_write_fail_exit_ms` 続いたら `HardwareWriteFailureExitError`
        で抜ける（決定記録 0080 §2.6）。**捕まえて運転を続けない。**
        """
        period_ms = self.loop.tick_period_ms
        deadline_ms = self.monotonic.monotonic_ms()
        while not self._stop and (max_ticks is None or self.stats.ticks < max_ticks):
            now_ms = self.monotonic.monotonic_ms()
            if now_ms < deadline_ms:
                self.sleep((deadline_ms - now_ms) / 1_000)
                continue
            result = self.loop.tick()
            self._record(result)
            deadline_ms += period_ms
            after_ms = self.monotonic.monotonic_ms()
            if self.write_fail_exit is not None:
                self.write_fail_exit.observe(
                    _confirmed_writes(result.hardware),
                    tick_started_mono_ms=now_ms,
                    now_mono_ms=after_ms,
                )
            while deadline_ms <= after_ms:
                deadline_ms += period_ms
                self.stats.skipped_slots += 1
        return self.stats

    def _record(self, result: ControlTickResult) -> None:
        self.stats.ticks += 1
        if result.deadline_exceeded:
            self.stats.overruns += 1
        if result.recorded:
            self.stats.recorded += 1
        if result.trace_failed:
            self.stats.trace_dropped += 1
        state = result.tick.state
        LOGGER.info(
            "control tick",
            extra={
                logs.FIELDS_KEY: {
                    "tick_id": result.tick.tick_id,
                    "ts_ms": result.tick.ts_ms,
                    "duration_ms": result.duration_ms,
                    "deadline_exceeded": result.deadline_exceeded,
                    "operating_mode": state.operating_mode.value,
                    "safety_state": state.safety_state.value,
                    "authority_stage": state.authority_stage.value,
                    "active_controller": (
                        None if state.active_controller is None else state.active_controller.value
                    ),
                    "faults": [fault.code.value for fault in result.tick.faults],
                    "effective": {
                        zone.value: result.tick.zones.get(zone).demand.effective for zone in Zone
                    },
                }
            },
        )


def _confirmed_writes(
    hardware: PerZone[FanHardwareResult] | None,
) -> dict[Zone, bool] | None:
    """zone ごとの「書き込みと読み戻しが成功したか」（trace の `write_ok` / `readback_ok`）。

    None は Backend が例外で結果を返さなかった tick（全 zone の失敗として数える）。
    """
    if hardware is None:
        return None
    return {
        zone: hardware.get(zone).readback.write_ok and hardware.get(zone).readback.readback_ok
        for zone in Zone
    }


def _settle_authority_on_stop(
    authority: AuthorityRuntime,
    leftover: AdminAuthorityCommand | None,
    *,
    drain: bool,
    persist_without_wait: bool = False,
) -> None:
    """停止の前に、枠に残った降格を入れてから、書き残せていない降格を1回だけ書き直す。

    `ControlDaemon.close()` と、`build()` が管理ソケットを開いた後に組み立てに失敗した経路の
    両方が通る。どちらでも、クライアントに `pending` を返し監査に `accepted` がある降格を
    捨てると、再起動で高い stage に戻る（0072 §2.6）。
    """
    persists = drain or persist_without_wait
    if leftover is not None:
        _lower_left_in_mailbox(authority, leftover, persists=persists)
    _flush_authority(authority, drain=drain, persist_without_wait=persist_without_wait)


def _lower_left_in_mailbox(
    authority: AuthorityRuntime, command: AdminAuthorityCommand, *, persists: bool
) -> None:
    """どの tick にも取り出されなかった管理ソケットの降格を、停止の前に入れる（0072 §2.6）。

    tick の経路と同じく、いまの stage と比べずに**無条件で**上限として入れる。journal へは
    続く `_flush_authority` が書く。監査には `applied` を足さない（`applied` は適用した
    tick を持つ事象で、この降格はどの tick でも効いていない）。受付の行だけがある
    「受理済み・未確定」として残り、journal の人の event と構造化ログで追える。
    """
    try:
        changed = authority.apply_lowering(
            to_stage=command.to_stage, actor=command.actor, reason=command.journal_reason()
        )
    except Exception:
        LOGGER.exception(
            "停止時に control-admin の authority の降格を入れられなかった（再起動で戻る）",
            extra={logs.FIELDS_KEY: _lowering_fields(command)},
        )
        return
    LOGGER.warning(
        "停止時に、tick に取り出されなかった control-admin の authority の降格を入れた",
        extra={logs.FIELDS_KEY: {**_lowering_fields(command), "changed": changed}},
    )
    if not persists:
        # 例外での停止では journal へ書かない（下の `_flush_authority`）。何が残ったかを
        # 主体と理由つきで残す
        _log_lowering_lost(command, "例外での停止のため journal へ書かない")


def _flush_authority(
    authority: AuthorityRuntime, *, drain: bool, persist_without_wait: bool = False
) -> None:
    """書き残せていない降格を、停止の前に1回だけ書き直す（lock の待ち上限つき）。

    **再起動すると journal の stage で運転が再開する**ので、残った降格を黙って捨てない。
    ``drain=False``（例外での停止）では待たずに、残った降格を error に残すだけにする。
    ``persist_without_wait`` なら lock を待たずに1回だけ試す（終了コード 7。0083）。
    """
    try:
        if drain:
            authority.flush_pending_on_shutdown()
            return
        if persist_without_wait:
            authority.flush_pending_on_shutdown(wait=False)
            return
        pending = authority.pending_stages
    except Exception:
        LOGGER.exception("停止時に authority の降格を書き残せなかった")
        return
    if pending:
        LOGGER.error(
            "停止までに authority の降格を journal へ書き残せなかった（再起動で戻る）",
            extra={logs.FIELDS_KEY: {"to_stages": [stage.value for stage in pending]}},
        )


def _lowering_fields(command: AdminAuthorityCommand) -> dict[str, object]:
    return {
        "command_id": command.command_id,
        "op": command.op,
        "to_stage": command.to_stage.value,
        "actor": command.actor,
        "reason": command.reason[:500],
    }


def _log_lowering_lost(command: AdminAuthorityCommand, why: str) -> None:
    LOGGER.error(
        "停止までに control-admin の authority の降格を journal へ書き残せなかった（再起動で戻る）",
        extra={logs.FIELDS_KEY: {**_lowering_fields(command), "why": why}},
    )


def build(
    config: Config,
    *,
    backend_factory: BackendFactory = simulated_backend,
    watchdog: Watchdog | None = None,
) -> ControlDaemon:
    """検証済み設定から control loop 一式を組み立てる。

    **1つでも検証に失敗したら組み立てない。** 途中まで配線した状態で走らせると、どの層が
    設定を持っていないのかが運転中にしか分からなくなる。

    失敗は**種類ごとに違う例外**で返す。起動時の失敗を1つの `Exception` にまとめると、
    「DB が一時的に開けない」だけで 0028 §2.7 の「設定不正なら全 zone Max」が走る。
    """
    try:
        control = ControlConfig.from_directory(config.config_dir)
    except Exception as error:
        # ここが 0028 §2.7 の「hardware は正しく safety / policy が不正」に当たる。
        # `air-balance.yaml` の不在・不正も同じ扱い（決定記録 0073 §2.2）。
        raise ControlConfigInvalidError(str(error)) from error
    if not control.actuation_permitted:
        raise ActuationNotApprovedError(
            "fan-hardware.yaml の approval が confirmed ではないため制御を取らない"
        )
    clock: Clock = WallClock()
    monotonic: MonotonicClock = SystemMonotonicClock()
    try:
        catalog = MetricCatalog.from_yaml(config.metrics)
    except Exception as error:
        raise StartupEnvironmentError(f"{type(error).__name__}: {error}") from error
    try:
        # `thermal_inputs` の metric を Catalog で確かめられないのは `air-balance.yaml` の不正で
        # あり、起動環境の問題ではない（決定記録 0073 §2.4 / §2.2）。
        air_balance_input_metrics(control, catalog, t_sensor_metric=config.t_sensor_metric)
    except Exception as error:
        raise ControlConfigInvalidError(str(error)) from error
    try:
        contract = build_input_contract(control, catalog, t_sensor_metric=config.t_sensor_metric)
        rules = QualityRules.from_yaml(config.quality_rules)
    except Exception as error:
        raise StartupEnvironmentError(f"{type(error).__name__}: {error}") from error
    store = open_writable_store(config.db, rules=rules, clock=clock, control=control)
    binding = create_control_runtime_binding(control)
    # **Gate と authority runtime が照らす artifact は1つの変数から渡す**（決定記録 0089 §2.1）。
    # Learned MPC の worker を配線していないので束縛する artifact は無い。authority は journal が
    # 上がっていても Baseline より上を有効にしない（0089 §2.2）。worker を配線するときは、起動時に
    # 読んだ registry の snapshot の production artifact をここへ置く（0077 §2.6）。
    loaded_artifact_sha256: str | None = None
    try:
        authority = open_authority_runtime(
            config.authority_root,
            control,
            clock=clock,
            loaded_artifact_sha256=loaded_artifact_sha256,
        )
    except Exception as error:
        store.close()
        raise StartupEnvironmentError(f"{type(error).__name__}: {error}") from error
    # **deadman を必ず配線する。** ここを省くと hang しても heartbeat の欠落が起きず、
    # `watchdog_timeout_ms` が一度も効かない（0028 §2.6 / 決定記録 0060 §2.7）。
    try:
        deadman = (
            watchdog
            if watchdog is not None
            else create_watchdog(
                interval_ms=heartbeat_interval_ms(control.safety),
                timeout_ms=control.safety.watchdog_timeout_ms.value,
                monotonic=monotonic,
                require=config.require_watchdog,
            )
        )
    except BaseException:
        store.close()
        raise
    admin = _open_admin(config, control, rules=rules, clock=clock, monotonic=monotonic)
    try:
        loop = _build_loop(
            config,
            control,
            catalog=catalog,
            contract=contract,
            binding=binding,
            authority=authority,
            loaded_artifact_sha256=loaded_artifact_sha256,
            deadman=deadman,
            store=store,
            clock=clock,
            monotonic=monotonic,
            backend_factory=backend_factory,
            admin=admin,
        )
    except BaseException:
        if admin is not None:
            # 開いてから組み立てに失敗するまでに受理した降格も書き残す（0072 §2.6）
            _settle_authority_on_stop(authority, admin.stop(), drain=True)
        raise
    return ControlDaemon(
        loop=loop,
        monotonic=monotonic,
        store=store,
        admin=admin,
        authority=authority,
        # **書けないまま制御を持ち続けない**（決定記録 0080 §2.6）。時間は safety.yaml が持つ
        write_fail_exit=HardwareWriteFailureExit(control.safety.hardware_write_fail_exit_ms.value),
    )


def open_writable_store(
    db: Path, *, rules: QualityRules, clock: Clock, control: ControlConfig
) -> SqliteStore:
    """制御を取る前に DB を開き、**書けることを確かめてから**返す（決定記録 0080 §2.1）。

    権限の失敗は `DbNotWritableError`（`db_not_writable`）、lock を取れないだけの失敗は
    `DbLockUnavailableError`、それ以外は `StartupEnvironmentError`。どれも終了コード 5 で
    制御を取らない（BIOS の制御のまま。人が直せば次の起動で戻れる）。
    """
    check_db_paths_writable(db)
    try:
        store = SqliteStore(
            db,
            rules=rules,
            clock=clock,
            # **decision trace の保存で待てる上限を tick の締め切りに収める。**
            # 既定の 5 秒待つと、保存が終わるまで次の tick が始まらず、heartbeat の
            # 間隔が deadman の時間切れを超える（決定記録 0060 §2.7）。待てなかった
            # 書き込みは失敗として記録に残る（制御は止めない）。
            busy_timeout_ms=control.safety.tick_deadline_ms.value,
        )
    except sqlite3.Error as error:
        raise classify_sqlite_open_error(db, error) from error
    except Exception as error:
        raise StartupEnvironmentError(f"{type(error).__name__}: {error}") from error
    try:
        check_db_write_lock(store, db)
    except BaseException:
        store.close()
        raise
    return store


def open_authority_runtime(
    root: Path,
    control: ControlConfig,
    *,
    clock: Clock,
    loaded_artifact_sha256: str | None,
) -> AuthorityRuntime:
    """`authority.json` を読む `AuthorityRuntime`（決定記録 0057 / 0072 §2.6）。

    **lock の待ち上限は `tick_deadline_ms`**（decision trace の保存の busy timeout と同じ。
    0060 §2.7）。書き残しは heartbeat の後に行うので、待ちは次の tick の開始を遅らせるだけで、
    Fan の書き込みと heartbeat は待たない。上限で諦めた降格は memory 上で下げたまま残る。

    journal が無ければ `SHADOW` から始まる。読めない・壊れているときは例外で止まる（0057 §2.1）。
    ``loaded_artifact_sha256`` は Gate の ``expected_artifact_sha256`` と同じ値
    （決定記録 0089 §2.1）。
    """
    store = AuthorityStore(
        root.absolute(), clock, lock_timeout_ms=control.safety.tick_deadline_ms.value
    )
    runtime = AuthorityRuntime(store, control.policy, loaded_artifact_sha256=loaded_artifact_sha256)
    LOGGER.info(
        "authority journal を読み込んだ",
        extra={
            logs.FIELDS_KEY: {
                "authority_root": str(root),
                "journal_stage": runtime.journal.stage.value,
                "journal_revision": runtime.journal.revision,
                "authority_config_ceiling": runtime.configured_ceiling.value,
                "authority_stage": runtime.current_stage().value,
                "authority_artifact_ceiling": runtime.artifact_ceiling.value,
                "loaded_artifact_sha256": loaded_artifact_sha256,
            }
        },
    )
    return runtime


def _open_admin(
    config: Config,
    control: ControlConfig,
    *,
    rules: QualityRules,
    clock: Clock,
    monotonic: MonotonicClock,
) -> ControlAdminEntry | None:
    """管理ソケットを開く。**開けなくても起動は続ける**（0072 §2.8）。"""
    if config.admin_config is None:
        LOGGER.error(
            "管理ソケットの設定が指定されていないため入口を開かない（AUTO のまま運転する）",
            extra={logs.FIELDS_KEY: {"reason": "admin_config_not_given"}},
        )
        return None
    db = config.db
    return open_control_admin(
        config.admin_config,
        tick_ms=control.safety.tick_ms.value,
        tick_deadline_ms=control.safety.tick_deadline_ms.value,
        authority_ceiling=control.policy.authority_stage,
        # 監査の表は**別の接続**で書く（0072 §2.7 / 0066 の前例）。接続は監査書き込みスレッドの
        # 中で開く。待ってよいのはこのスレッドだけなので、busy timeout は既定のままにする
        open_audit_sink=lambda: SqliteStore(db, rules=rules, clock=clock),
        clock=clock,
        monotonic=monotonic,
    )


def _build_loop(
    config: Config,
    control: ControlConfig,
    *,
    catalog: MetricCatalog,
    contract: ControlInputContract,
    binding: ControlRuntimeBinding,
    authority: AuthorityRuntime,
    loaded_artifact_sha256: str | None,
    deadman: Watchdog,
    store: SqliteStore,
    clock: Clock,
    monotonic: MonotonicClock,
    backend_factory: BackendFactory,
    admin: ControlAdminEntry | None,
) -> ControlLoop:
    safety = CriticalSafety(
        control.safety,
        input_contract=contract,
        runtime_binding=binding,
        approved_t_sensor_metric=config.t_sensor_metric,
        metric_catalog=catalog if config.t_sensor_metric is not None else None,
    )
    loop = ControlLoop(
        config=control,
        estimator=ControlStateEstimator(contract, catalog),
        fallback=FallbackController(control.policy, catalog),
        gate=ControllerGate(
            control.policy,
            expected_model_version=UNCONFIGURED_MODEL_VERSION,
            # **束縛した artifact が無いことを明示する**（いまは None。#159 / 決定記録 0059 §2.1）。
            # worker を配線していないので提案は1件も来ないが、仮に来ても採らない。
            # 既定値を置かず必須の引数にしてあるのは、渡し忘れが「何にも照らさない
            # Gate」を作らないためである。
            # authority runtime と同じ値（決定記録 0089 §2.1）。
            expected_artifact_sha256=loaded_artifact_sha256,
            authority=authority,
        ),
        guard=ReactiveGuard(control.policy.reactive_guard, catalog),
        safety=safety,
        composer=DemandComposer(control.safety),
        backend=backend_factory(control.fan_hardware, binding),
        telemetry=StoreTelemetrySource(store, frozenset(spec.metric for spec in contract.signals)),
        clock=clock,
        monotonic=monotonic,
        # **registry を読んでいないことを明示する**（#104 / 決定記録 0071 §2.5）。Learned MPC の
        # worker を配線していない構成では束縛する artifact が無く、registry の root も
        # 設定に無い。worker を配線するときは、起動時に読んだ snapshot の
        # `RegistrySnapshot.trace_provenance()` を渡す（Gate の期待版と同じ snapshot から作る）。
        registry=RegistryProvenance.unbound(),
        # **モードの出どころは1つ。** 管理ソケットを開けたら受け渡し口、開けなければ常に AUTO
        # （0072 §2.2。開かなかった入口は死んだとは扱わない）
        mode_source=None if admin is not None else StaticOperatingMode(),
        admin_mode=None if admin is None else admin.tracker,
        supervisor=SupervisorCoordinator(control.policy.supervisor, clock),
        regime=WorkloadRegimeEstimator(control.policy.workload_regime, catalog, clock),
        shadow=ShadowRecorder(control.policy.shadow),
        trace=ControlTraceLogger(store) if config.record_trace else None,
        authority=authority,
        watchdog=deadman,
    )
    _log_configuration(control, safety)
    return loop


class ActuationNotApprovedError(RuntimeError):
    """実機の制御を取る承認（0028 §2.9 の承認点 3）がまだ無い。"""


def run_config_invalid_max(
    config: Config,
    *,
    monotonic: MonotonicClock,
    backend_factory: BackendFactory = simulated_backend,
) -> ControlStats:
    """制御設定が無い・不正なときの、全 zone Max だけの経路（0028 §2.7 / 0073 §2.2）。

    対象は `safety.yaml` / `fan-policy.yaml` / `air-balance.yaml`。

    **不正な閾値を読まない。** 確認済みの hardware mapping だけを使い、`config_invalid` の
    `EMERGENCY` を1回書く。以後は正しい設定で再起動するまで解除しない。
    """
    runtime = create_emergency_control_runtime(config.config_dir / CONFIG_FILENAMES["fan_hardware"])
    if runtime.fan_hardware.approval.status != "confirmed":
        raise ActuationNotApprovedError(
            "fan-hardware.yaml の approval が confirmed ではないため制御を取らない"
        )
    backend = backend_factory(runtime.fan_hardware, runtime.binding)
    composer = DemandComposer.for_invalid_config(runtime.binding)
    backend.apply(composer.compose_invalid_config(tick_id=0, monotonic_ms=monotonic.monotonic_ms()))
    LOGGER.error(
        "設定が不正なため全 zone を Max に固定した（正しい設定で再起動するまで解除しない）",
        extra={logs.FIELDS_KEY: {"reason": "config_invalid"}},
    )
    return ControlStats(ticks=1, emergency_max=True)


def _log_configuration(control: ControlConfig, safety: CriticalSafety) -> None:
    """起動時に暫定値の位置を出す（0028 §2.8）。**値そのものは出さない。**

    Critical Safety の裁定の前提（設定で外した入力の理由と、暫定値を含むか）も同じ行に
    出す。decision trace（`ControlTick` v9 の `safety_provenance`）と同じ内容を、tick が
    1つも保存されないまま止まった起動でも追えるようにするため（#78）。
    """
    provisional = control.provisional_values()
    air_balance = control.air_balance
    LOGGER.info(
        "制御設定を読み込んだ",
        extra={
            logs.FIELDS_KEY: {
                **control.trace_metadata(),
                # **未校正で無効にした起動を黙って流さない**（決定記録 0073 §2.2）。
                # 毎 tick の trace にも `disabled: uncalibrated` が残る。
                "air_balance": {
                    "status": "enabled" if control.air_balance_enabled else "disabled",
                    "disabled_reason": None if control.air_balance_enabled else "uncalibrated",
                    "model_id": air_balance.model_id,
                    "source_status": air_balance.source.status,
                },
                "tick_ms": control.safety.tick_ms.value,
                "tick_deadline_ms": control.safety.tick_deadline_ms.value,
                "authority_stage_ceiling": control.policy.authority_stage.value,
                "provisional_values": [f"{item.source}:{item.path}" for item in provisional],
                "safety_config_is_provisional": safety.config_is_provisional,
                "safety_disabled_inputs": [
                    reason.model_dump(mode="json") for reason in safety.disabled_inputs
                ],
            }
        },
    )


def build_parser() -> argparse.ArgumentParser:
    """CLI を組み立てる。**閾値は受け取らない**（設定ファイルが持つ）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-fand",
        description="3系統 Fan 制御デーモン（Supervisor + MPC + Guard + Critical Safety）",
    )
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--quality-rules", type=Path, default=DEFAULT_QUALITY_RULES)
    parser.add_argument(
        "--t-sensor-metric",
        default=None,
        help="承認済みの T_SENSOR metric（safety.yaml で有効にしたときだけ指定する）",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="この tick 数で終了する（試験・Replay 用）",
    )
    parser.add_argument("--no-trace", action="store_true", help="decision trace を保存しない")
    parser.add_argument(
        "--require-watchdog",
        action="store_true",
        help=f"{NOTIFY_SOCKET_ENV} が無ければ起動しない（systemd の Type=notify で使う）",
    )
    parser.add_argument(
        "--admin-config",
        type=Path,
        default=DEFAULT_ADMIN_CONFIG,
        help="管理ソケットの設定（決定記録 0072 §2.8）。不正なら入口を開かず AUTO で運転する",
    )
    parser.add_argument(
        "--authority-root",
        type=Path,
        default=DEFAULT_AUTHORITY_ROOT,
        help="authority.json のディレクトリ（決定記録 0057）。読めなければ制御を取らない",
    )
    parser.add_argument(
        "--no-admin",
        action="store_true",
        help="管理ソケットを開かない（AUTO のまま運転する。試験・Replay 用）",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def _enable_faulthandler() -> None:
    """watchdog の SIGABRT で全スレッドの traceback を journal へ残す（決定記録 0080 §2.5）。

    hang の原因（どのスレッドがどこで止まったか）を後から追うためのもので、制御には効かない。
    **有効にできなくても起動は止めない**（診断の欠落で冷却の制御を失わない）。既に有効
    （`PYTHONFAULTHANDLER` など）なら出力先を変えない。
    """
    if faulthandler.is_enabled():
        return
    try:
        faulthandler.enable(all_threads=True)
    except (RuntimeError, ValueError, OSError, AttributeError):
        LOGGER.exception("faulthandler を有効にできなかった（hang の traceback が残らない）")


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-fand` の入口。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    _enable_faulthandler()
    config = Config(
        config_dir=args.config_dir,
        db=args.db,
        metrics=args.metrics,
        quality_rules=args.quality_rules,
        t_sensor_metric=args.t_sensor_metric,
        record_trace=not args.no_trace,
        require_watchdog=args.require_watchdog,
        admin_config=None if args.no_admin else args.admin_config,
        authority_root=args.authority_root,
    )
    monotonic: MonotonicClock = SystemMonotonicClock()

    try:
        load_fan_hardware_document(config.config_dir / CONFIG_FILENAMES["fan_hardware"])
    except Exception:
        # 対象の header を特定できない設定で Max を書くと、対象外の header へ書き込む。
        LOGGER.exception("fan-hardware.yaml が不正なため制御を取らない（BIOS の制御のまま）")
        return EXIT_HARDWARE_CONFIG_INVALID

    try:
        daemon = build(config)
    except ActuationNotApprovedError:
        LOGGER.exception("実機の制御を取る承認が無いため起動しない（決定記録 0028 §2.9）")
        return EXIT_ACTUATION_NOT_APPROVED
    except WatchdogUnavailableError:
        LOGGER.exception("外部の deadman が使えないため起動しない（決定記録 0028 §2.6）")
        return EXIT_WATCHDOG_UNAVAILABLE
    except WatchdogNotifyIoError:
        LOGGER.exception(
            "systemd への通知の I/O に失敗したため制御を取らずに終える（再起動で再試行する。"
            "決定記録 0080 §2.4）"
        )
        return EXIT_NOTIFY_IO_FAILED
    except DbNotWritableError as error:
        LOGGER.error(
            "DB に書けないため制御を取らない（権限を直せば次の起動で戻る。決定記録 0080 §2.1）",
            exc_info=True,
            extra={logs.FIELDS_KEY: error.fields},
        )
        return EXIT_STARTUP_ENVIRONMENT
    except DbLockUnavailableError:
        LOGGER.exception("DB の書き込みの lock を取れないため制御を取らない（権限の問題ではない）")
        return EXIT_STARTUP_ENVIRONMENT
    except ControlConfigInvalidError:
        LOGGER.exception(
            "safety.yaml / fan-policy.yaml / air-balance.yaml が無い・不正なため"
            "全 zone を Max にする"
        )
        try:
            stats = run_config_invalid_max(config, monotonic=monotonic)
        except ActuationNotApprovedError:
            LOGGER.exception("実機の制御を取る承認が無いため Max も書かない")
            return EXIT_ACTUATION_NOT_APPROVED
        LOGGER.error("config_invalid で停止", extra={logs.FIELDS_KEY: stats.as_fields()})
        return 1
    except Exception:
        # **制御設定の不正と同じ扱いにしない。** Metric Catalog・品質規則・ストアの失敗で
        # Max を書くと、0028 §2.7 が決めていない状況で制御を取ることになる。BIOS の
        # 制御（Safety-0）のまま終了し、原因をそのまま記録する。
        LOGGER.exception("制御設定以外の理由で起動できないため制御を取らない")
        return EXIT_STARTUP_ENVIRONMENT

    def _stop(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("シグナルを受けた", extra={logs.FIELDS_KEY: {"signal": signum}})
        daemon.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        stats = daemon.run(max_ticks=args.max_ticks)
    except HardwareWriteFailureExitError as error:
        # **返却（0080 §2.8）をしない。引き継ぎ記録を消さない。** BIOS へ戻す書き込みも同じ理由で
        # 失敗しうる。ExecStopPost が root で Max・manual を書き、Restart=always で takeover から
        # やり直す（決定記録 0080 §2.6）。後片付けは例外の経路と同じくスレッドを待たないが、
        # 受理済みの降格は lock を待たずに1回だけ journal へ書き残しを試す（0083）。
        LOGGER.critical(
            "Fan の書き込みが続けて失敗したため終了する"
            "（引き継ぎ記録を残し、ExecStopPost の Max に任せる）",
            exc_info=True,
            extra={
                logs.FIELDS_KEY: {
                    "event": "hardware_write_fail_exit",
                    "zones": [zone.value for zone in error.zones],
                    "failing_ms": {zone.value: ms for zone, ms in error.failing_ms.items()},
                    "hardware_write_fail_exit_ms": error.limit_ms,
                    **daemon.stats.as_fields(),
                }
            },
        )
        daemon.close(drain=False, persist_without_wait=True)
        return EXIT_HARDWARE_WRITE_FAILED
    except BaseException:
        # 例外で抜けた経路では管理ソケットの後片付けを待たない（終了と引き継ぎを遅らせない）
        daemon.close(drain=False)
        raise
    daemon.close()
    LOGGER.info("control daemon を終了する", extra={logs.FIELDS_KEY: stats.as_fields()})
    return 0


if __name__ == "__main__":  # pragma: no cover - `python -m coldaisle.control_daemon`
    raise SystemExit(main())
