"""Learned worker との経路を開いて、受付スレッドを束ねる（決定記録 0077 §2.2 / §2.7）。

**開けなくても `coldaisle-fand` は止めない。** 設定が不正・`SO_PEERCRED` が無い・グループを解決
できない・2つのグループが重なる・ソケットを作れない・スレッドを起動できない、のいずれでも、
`coldaisle-fand` は Learned を使わずに（Fallback / RulePolicy で）運転を続ける（0077 §2.7。
冷却を入口の有無に依存させない）。このとき loop へは `channel_disabled` を答える口を渡し、
理由は error として残す。
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from coldaisle import logs
from coldaisle.clock import MonotonicClock
from coldaisle.control.learned_handoff import LearnedChannelState, LearnedFrame, LearnedRole
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.learned_channel.config import LearnedChannelSettings
from coldaisle.learned_channel.mailbox import LearnedMailbox
from coldaisle.learned_channel.server import GroupDirectory, LearnedChannelServer
from coldaisle.local_socket import peer_credentials_supported, peer_uid

LOGGER = logging.getLogger("coldaisle.learned_channel")


class DisabledLearnedChannel:
    """開かなかった経路。提案は無く、状態は常に `channel_disabled`、frame は捨てる。"""

    def poll(self) -> MpcProposal | None:
        """提案は届かない。"""
        return None

    def state(self, role: LearnedRole) -> LearnedChannelState:
        """経路を開いていない。"""
        return LearnedChannelState.CHANNEL_DISABLED

    def offer(self, frame: LearnedFrame) -> None:
        """送る相手がいないので捨てる。"""


@dataclass(slots=True)
class LearnedChannelEntry:
    """開いた経路一式。loop へは `mailbox`（提案の口・状態の口・送り出しの口）を渡す。"""

    settings: LearnedChannelSettings
    mailbox: LearnedMailbox
    server: LearnedChannelServer
    run_id: str
    shutdown_wait_ms: int
    """停止の手順で受付スレッドを待つ上限。`tick_deadline_ms` を渡す。"""

    def stop(self, *, drain: bool = True) -> None:
        """`coldaisle-fand` の停止の手順。**loop が止まった後に呼ぶ**（死んだとは扱わない）。

        ``drain=False`` は loop が例外で抜けたときの経路。スレッドを待たずに閉じる。
        """
        self.mailbox.mark_stopping()
        self.server.request_stop()
        self.server.join(timeout_s=self.shutdown_wait_ms / 1_000 if drain else 0.0)
        self.server.close()
        self.mailbox.close()
        LOGGER.info(
            "Learned の経路を閉じた",
            extra={logs.FIELDS_KEY: {"run_id": self.run_id, "dropped": self.server.dropped}},
        )


def open_learned_channel(
    config_path: Path,
    *,
    run_id: str,
    tick_deadline_ms: int,
    monotonic: MonotonicClock,
    server_uid: int | None = None,
    groups: GroupDirectory | None = None,
    peer: Callable[[socket.socket], int] = peer_uid,
) -> LearnedChannelEntry | None:
    """経路を開き、受付スレッドを起動する。開けなければ None（呼び出し側が無効の口を渡す）。"""
    if not peer_credentials_supported():
        _log_not_opened(
            "SO_PEERCRED が無いプラットフォームでは Learned の経路を開かない", config_path
        )
        return None
    try:
        settings = LearnedChannelSettings.from_yaml(config_path)
    except Exception as error:
        _log_not_opened(f"Learned の経路の設定が不正: {type(error).__name__}: {error}", config_path)
        return None
    mailbox = LearnedMailbox()
    try:
        server = LearnedChannelServer(
            settings=settings,
            mailbox=mailbox,
            monotonic=monotonic,
            run_id=run_id,
            server_uid=server_uid,
            groups=groups,
            peer=peer,
        )
        server.bind()
    except Exception as error:
        mailbox.close()
        _log_not_opened(f"Learned の経路を開けない: {type(error).__name__}: {error}", config_path)
        return None
    try:
        server.start()
    except Exception as error:
        server.close()
        mailbox.close()
        _log_not_opened(
            f"Learned の経路のスレッドを起動できない: {type(error).__name__}: {error}", config_path
        )
        return None
    return LearnedChannelEntry(
        settings=settings,
        mailbox=mailbox,
        server=server,
        run_id=run_id,
        shutdown_wait_ms=tick_deadline_ms,
    )


def _log_not_opened(reason: str, config_path: Path) -> None:
    LOGGER.error(
        "Learned の経路を開かずに運転する（Fallback / RulePolicy。Learned は使わない）",
        extra={
            logs.FIELDS_KEY: {
                "reason": reason,
                "config": str(config_path),
                "state": LearnedChannelState.CHANNEL_DISABLED.value,
            }
        },
    )
