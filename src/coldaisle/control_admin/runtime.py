"""管理ソケットを開いて、受付スレッドと監査書き込みスレッドを束ねる（決定記録 0072 §2.2 / §2.8）。

**開けなかったら None を返し、`coldaisle-fand` は止めない。** 入口の設定が不正、`SO_PEERCRED` が
無い、ソケットを作れない、スレッドを起動できない、のいずれでも、`coldaisle-fand` は `AUTO` と
journal の stage で運転を続ける（冷却を入口の有無に依存させない）。
入口が無いことは error として残す。
このとき loop は受付スレッドの死とは扱わない（`MANUAL` に入る経路が無いため）。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from coldaisle import logs
from coldaisle.clock import Clock, MonotonicClock
from coldaisle.control.operating_mode import AdminModeTracker
from coldaisle.control.schema import AuthorityStage
from coldaisle.control_admin.audit import AuditSink, AuditWriter
from coldaisle.control_admin.config import ControlAdminSettings
from coldaisle.control_admin.mailbox import AdminMailbox
from coldaisle.control_admin.server import ControlAdminServer
from coldaisle.local_socket import peer_credentials_supported

LOGGER = logging.getLogger("coldaisle.control_admin")


def new_run_id() -> str:
    """起動ごとの識別子（32桁の16進の乱数。個体識別子を含まない。0072 §2.7）。"""
    return secrets.token_hex(16)


@dataclass(slots=True)
class ControlAdminEntry:
    """開いた管理ソケット一式。loop へは `tracker` だけを渡す。"""

    settings: ControlAdminSettings
    mailbox: AdminMailbox
    server: ControlAdminServer
    audit: AuditWriter
    tracker: AdminModeTracker
    run_id: str
    shutdown_wait_ms: int
    """停止の手順で受付・監査の各スレッドを待つ上限。`tick_deadline_ms` を渡す。

    `apply_ack_timeout_ms` で待つと、監査の DB が lock されているときに process の終了
    （と引き継ぎの Max）が秒単位で遅れる。その間 loop は止まり PWM は最後の値のままになる。
    """

    def stop(self, *, drain: bool = True) -> None:
        """`coldaisle-fand` の停止の手順。**loop が止まった後に呼ぶ**（死んだとは扱わない）。

        ``drain=False`` は loop が例外で抜けたときの経路（0028 §2.7 の「終わらせて引き継ぎで
        Max」）。スレッドを待たずに閉じる（どちらも daemon thread なので終了を妨げない）。
        書き終えていない監査の依頼は失われうるが、構造化ログには残っている。
        """
        self.mailbox.mark_stopping()
        timeout_s = self.shutdown_wait_ms / 1_000 if drain else 0.0
        self.server.request_stop()
        self.server.join(timeout_s=timeout_s)
        self.audit.stop(timeout_s=timeout_s)
        self.server.close()
        self.mailbox.close()
        LOGGER.info(
            "管理ソケットを閉じた",
            extra={logs.FIELDS_KEY: {"run_id": self.run_id, "audit_failures": self.audit.failures}},
        )


def open_control_admin(
    config_path: Path,
    *,
    tick_ms: int,
    tick_deadline_ms: int,
    authority_ceiling: AuthorityStage,
    open_audit_sink: Callable[[], AuditSink],
    clock: Clock,
    monotonic: MonotonicClock,
    server_uid: int | None = None,
) -> ControlAdminEntry | None:
    """管理ソケットを開き、受付スレッドと監査書き込みスレッドを起動する。開けなければ None。"""
    if not peer_credentials_supported():
        _log_not_opened("SO_PEERCRED が無いプラットフォームでは管理ソケットを開かない", config_path)
        return None
    try:
        settings = ControlAdminSettings.from_yaml(config_path)
        settings.check_against(tick_ms=tick_ms, tick_deadline_ms=tick_deadline_ms)
    except Exception as error:
        _log_not_opened(f"管理ソケットの設定が不正: {type(error).__name__}: {error}", config_path)
        return None
    run_id = new_run_id()
    mailbox = AdminMailbox()
    audit = AuditWriter(
        open_sink=open_audit_sink,
        queue_max=settings.limits.audit_queue_max,
        on_done=mailbox.wake,
    )
    try:
        server = ControlAdminServer(
            settings=settings,
            mailbox=mailbox,
            audit=audit,
            clock=clock,
            monotonic=monotonic,
            run_id=run_id,
            authority_ceiling=authority_ceiling,
            server_uid=server_uid,
        )
        server.bind()
    except Exception as error:
        mailbox.close()
        _log_not_opened(f"管理ソケットを開けない: {type(error).__name__}: {error}", config_path)
        return None
    try:
        audit.start()
        server.start()
    except Exception as error:
        # スレッドを起動できない（"can't start new thread" など）ときも、build() を止めずに
        # 入口なし・AUTO で運転を続ける（0072 §2.8）。起動できた監査スレッドは止め、
        # 作ったソケットは消す（残すと次の起動の検査で「既にあるソケット」に当たる）
        audit.stop(timeout_s=tick_deadline_ms / 1_000)
        server.close()
        mailbox.close()
        _log_not_opened(
            f"管理ソケットのスレッドを起動できない: {type(error).__name__}: {error}", config_path
        )
        return None
    return ControlAdminEntry(
        settings=settings,
        mailbox=mailbox,
        server=server,
        audit=audit,
        tracker=AdminModeTracker(mailbox, run_id=run_id),
        run_id=run_id,
        shutdown_wait_ms=tick_deadline_ms,
    )


def _log_not_opened(reason: str, config_path: Path) -> None:
    LOGGER.error(
        "管理ソケットを開かずに運転する（AUTO と journal の stage。モードは変えられない）",
        extra={logs.FIELDS_KEY: {"reason": reason, "config": str(config_path)}},
    )
