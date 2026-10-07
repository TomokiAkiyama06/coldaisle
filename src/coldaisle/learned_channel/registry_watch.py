"""`coldaisle-fand` の registry の読み込みと、走行中の production の監視（決定記録 0077 §2.6）。

- **起動時に1回だけ** `registry.json` を読み取り専用で読み（flock を取らない。原子置換された file を
  読むだけ）、その1つの snapshot から `RegistryBinding`（provenance・Gate の期待値・
  `expected_rl_identity`・frame の `expected_artifacts`）を作る。読めない・壊れているときは
  **起動を止めずに** Learned を無効にする（`RegistryBinding.unbound()`。0077 §2.6 / §5 の決定 7）
- 走行中は受付スレッドが `safety.tick_ms` ごとに `RegistryWatch.check()` を呼ぶ。固定した
  artifact がその kind の production でなくなった（移動・消失）・registry が読めない役割を
  返し、受付スレッドがその役割を**再起動まで**閉じる（`registry_superseded`）。
  **Gate の期待値・`expected_rl_identity`・trace の provenance は書き換えない**（0071 §2.5）

`coldaisle-fand` は artifact の bytes を読まず、deserialize もしない（ML を制御プロセスへ
入れない）。bytes の検証は worker の仕事である（0077 §2.6）。**registry へは書かない**
（#104 と #92 の境界）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from coldaisle import logs
from coldaisle.control.learned_handoff import LearnedExpectedArtifacts, LearnedRole
from coldaisle.control.model_registry import (
    REGISTRY_STATE_FILENAME,
    ModelRegistry,
    load_model_registry_limits,
)
from coldaisle.control.registry_binding import RegistryBinding

LOGGER = logging.getLogger("coldaisle.learned_channel.registry")

FileIdentity = tuple[int, int, int, int]
"""`registry.json` の `stat` の同一性（`st_dev` / `st_ino` / `st_mtime_ns` / `st_size`）。"""


def snapshot_identity(path: Path) -> FileIdentity | None:
    """`registry.json` の同一性。無ければ None（registry は空として読める）。

    無い以外の失敗（権限など）は `OSError` のまま返す（呼び出し側は読めないとして扱う）。
    symlink はたどらない（registry 自身も `O_NOFOLLOW` で開く）。
    """
    try:
        status = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)


class RegistryWatch:
    """固定した artifact が、いまもその kind の production かを確かめる（受付スレッドの側）。

    `stat` の同一性が前回と同じなら読み直さない。違えば snapshot を読み直し、役割ごとの固定と
    比べる。**例外を外へ出さない**（受付スレッドを殺さない）。読めないときは全役割を返す
    （読めない間に何が production だったかを確かめられないため。保守側に倒す。0077 §2.6）。
    """

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        snapshot_path: Path,
        identity: FileIdentity | None,
        pins: LearnedExpectedArtifacts,
        identity_of: Callable[[Path], FileIdentity | None] = snapshot_identity,
    ) -> None:
        self._registry = registry
        self._path = snapshot_path
        self._identity = identity
        self._pins = pins
        self._identity_of = identity_of

    @property
    def pins(self) -> LearnedExpectedArtifacts:
        """起動時の固定（比べる基準。走行中に変えない）。"""
        return self._pins

    def check(self) -> frozenset[LearnedRole]:
        """固定した artifact が production でなくなった・確かめられない役割。"""
        try:
            identity = self._identity_of(self._path)
            if identity is not None and identity == self._identity:
                return frozenset()
            if identity is None and self._identity is None:
                return frozenset()
            snapshot = self._registry.inspect()
            current = LearnedExpectedArtifacts.from_provenance(snapshot.trace_provenance())
        except Exception as error:
            LOGGER.error(
                "registry を確かめられないため、Learned の役割を再起動まで閉じる",
                extra={
                    logs.FIELDS_KEY: {
                        "reason": "registry_unreadable",
                        "error": type(error).__name__,
                    }
                },
            )
            return frozenset(LearnedRole)
        # 読み直した版を覚える。同じ file を毎回読み直さないため（閉じた役割は受付が覚えている）
        self._identity = identity
        return frozenset(
            role for role in LearnedRole if current.for_role(role) != self._pins.for_role(role)
        )


@dataclass(frozen=True, slots=True)
class StartupRegistry:
    """起動時に読んだ registry。`watch` は読めたときだけある（読めなければ監視しない）。"""

    binding: RegistryBinding
    watch: RegistryWatch | None


def read_startup_registry(root: Path | None, *, limits_dir: Path) -> StartupRegistry:
    """`--registry-root` を起動時に1回だけ読む。**例外で起動を止めない**（0077 §2.6）。

    ``root`` が None（`--registry-root` を省いた）なら、いまと同じく registry を読まない。
    """
    if root is None:
        LOGGER.info(
            "registry の root が指定されていないため読まない（Learned は使わない）",
            extra={logs.FIELDS_KEY: {"reason": "registry_root_not_given"}},
        )
        return StartupRegistry(binding=RegistryBinding.unbound(), watch=None)
    absolute = Path(os.path.abspath(root))
    snapshot_path = absolute / REGISTRY_STATE_FILENAME
    try:
        limits = load_model_registry_limits(limits_dir)
        registry = ModelRegistry(absolute, limits=limits)
        # **同一性を読む前に取る。** 読んだ後に取ると、その間の promotion を見落とす
        identity = snapshot_identity(snapshot_path)
        snapshot = registry.inspect()
        binding = RegistryBinding.from_snapshot(snapshot)
    except Exception as error:
        # 「読まなかった」（unbound）と trace では区別できないので、ここで error に残す（0077 §2.6）
        LOGGER.error(
            "registry を読めないため Learned を無効にして起動する（Fallback / RulePolicy で運転）",
            extra={
                logs.FIELDS_KEY: {
                    "reason": "registry_unreadable",
                    "registry_root": str(root),
                    "error": f"{type(error).__name__}: {error}",
                }
            },
        )
        return StartupRegistry(binding=RegistryBinding.unbound(), watch=None)
    pins = binding.expected_artifacts
    LOGGER.info(
        "registry を読み込んだ（再起動まで読み直さない。production の移動は役割を閉じるだけ）",
        extra={
            logs.FIELDS_KEY: {
                "registry_root": str(root),
                "registry_revision": binding.provenance.revision,
                "registry_present": identity is not None,
                "thermal_model": _pin_fields(pins, LearnedRole.MPC),
                "supervisor_policy": _pin_fields(pins, LearnedRole.SUPERVISOR),
            }
        },
    )
    return StartupRegistry(
        binding=binding,
        watch=RegistryWatch(registry, snapshot_path=snapshot_path, identity=identity, pins=pins),
    )


def _pin_fields(pins: LearnedExpectedArtifacts, role: LearnedRole) -> dict[str, str] | None:
    pinned = pins.for_role(role)
    return None if pinned is None else pinned.model_dump()
