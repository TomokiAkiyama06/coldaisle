"""固定された artifact がいまもその kind の production かの照合（決定記録 0077 §2.6）。

MPC worker（`thermal_model`）と RL Supervisor worker（`supervisor_policy`）が、結果を作る直前に
毎周期呼ぶ。`registry.json` を読み取り専用で確かめ（flock を取らない）、**registry へは書かない**。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from coldaisle import logs
from coldaisle.control.learned_handoff import LearnedExpectedArtifacts, LearnedRole, PinnedArtifact
from coldaisle.control.model_registry import REGISTRY_STATE_FILENAME, ModelRegistry
from coldaisle.learned_channel.registry_watch import FileIdentity, snapshot_identity

LOGGER = logging.getLogger("coldaisle.learned_worker")


class ProductionCheck(Protocol):
    """固定された artifact がいまもその kind の production か（0077 §2.6）。"""

    def is_production(self, pinned: PinnedArtifact) -> bool:
        """**例外を出さない。** 読めない・壊れているときは False（保守側）。"""


class RegistryProductionCheck:
    """`registry.json` を読み取り専用で確かめる（flock を取らない。0077 §2.6）。

    `stat` の同一性（`st_dev` / `st_ino` / `st_mtime_ns` / `st_size`）と固定が前回と同じなら
    読み直さない。``role`` の kind（`mpc` → `thermal_model`、`supervisor` →
    `supervisor_policy`）の production と比べる。
    """

    def __init__(
        self, registry: ModelRegistry, root: Path, *, role: LearnedRole = LearnedRole.MPC
    ) -> None:
        self._registry = registry
        self._path = root / REGISTRY_STATE_FILENAME
        self._role = role
        self._cached: tuple[FileIdentity | None, PinnedArtifact, bool] | None = None

    def is_production(self, pinned: PinnedArtifact) -> bool:
        """固定がいまのその kind の production の3つ組と一致するか。"""
        try:
            identity = snapshot_identity(self._path)
            cached = self._cached
            if cached is not None and cached[0] == identity and cached[1] == pinned:
                return cached[2]
            snapshot = self._registry.inspect()
            current = LearnedExpectedArtifacts.from_provenance(snapshot.trace_provenance())
        except Exception as error:
            LOGGER.error(
                "registry を確かめられない。固定した artifact を production とみなさない",
                extra={
                    logs.FIELDS_KEY: {
                        "reason": "registry_unreadable",
                        "role": self._role.value,
                        "error": type(error).__name__,
                    }
                },
            )
            self._cached = None
            return False
        result = current.for_role(self._role) == pinned
        self._cached = (identity, pinned, result)
        return result
