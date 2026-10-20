"""`coldaisle-fand` の registry の読み込みと、走行中の production の監視（決定記録 0077 §2.6）。

- **起動時に1回だけ** `registry.json` を読み取り専用で読み（flock を取らない。原子置換された file を
  読むだけ）、その1つの snapshot から `RegistryBinding`（provenance・Gate の期待値・
  `expected_rl_identity`・frame の `expected_artifacts`）を作る。読めない・壊れているときは
  **起動を止めずに** Learned を無効にする（`RegistryBinding.unbound()`。0077 §2.6 / §5 の決定 7）
- 走行中は受付スレッドが `safety.tick_ms` ごとに `RegistryWatch.check()` を呼ぶ。固定した
  artifact がその kind の production でなくなった（移動・消失）・registry が読めない役割を
  返し、受付スレッドがその役割を**再起動まで**閉じる（`registry_superseded`）。
  **Gate の期待値・`expected_rl_identity`・trace の provenance は書き換えない**（0071 §2.5）
- Workload Hint の B1 / B2 を設定した起動では、**同じ1つの snapshot** から hint_certificate の
  production（証明書の bytes。JSON で、ML ではない）と thermal_model の production を読み、起動時の
  照合（`coldaisle.hint_certification`）に渡す（決定記録 0152 §2.2 / §2.4）。走行中にどちらかの
  production が変わった・確かめられないときは `HintCertificationLatch` を立て、ヒントの実効の段階を
  再起動まで B0 に下げる（下げるだけ。0152 §5 の 6 (a)）

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
    ArtifactKind,
    ArtifactRef,
    ModelRegistry,
    RegistrySnapshot,
    load_model_registry_limits,
)
from coldaisle.control.registry_binding import RegistryBinding
from coldaisle.control.supervisor.workload_hint import HintCertificationLatch
from coldaisle.hint_certificate import CertifiedThermalModel
from coldaisle.hint_certification import (
    CERTIFICATE_COMPATIBILITY,
    HintCertificateSource,
    certificate_source,
    production_certificate_ref,
    production_thermal_model,
)

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
        hint: HintProductionPins | None = None,
    ) -> None:
        self._registry = registry
        self._path = snapshot_path
        self._identity = identity
        self._pins = pins
        self._identity_of = identity_of
        self._hint = hint

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
            if self._hint is not None:
                # 読めない間に何が production だったかを確かめられない。保守側に倒す
                self._hint.latch.revoke()
            return frozenset(LearnedRole)
        if self._hint is not None:
            self._hint.check(snapshot)
        # 読み直した版を覚える。同じ file を毎回読み直さないため（閉じた役割は受付が覚えている）
        self._identity = identity
        return frozenset(
            role for role in LearnedRole if current.for_role(role) != self._pins.for_role(role)
        )


class HintProductionPins:
    """起動時の照合に使った production（証明書の pointer と thermal_model）と、変化の印。

    0152 §2.4: 稼働中にどちらかの production が変わったら、印を立てて実効の段階を B0 に下げる。
    **元に戻っても印は下ろさない**（再起動まで上げない）。受付スレッドの側だけが使う。
    """

    __slots__ = ("_certificate_ref", "_latch", "_thermal_model")

    def __init__(
        self,
        *,
        certificate_ref: ArtifactRef | None,
        thermal_model: CertifiedThermalModel | None,
        latch: HintCertificationLatch,
    ) -> None:
        self._certificate_ref = certificate_ref
        self._thermal_model = thermal_model
        self._latch = latch

    @property
    def latch(self) -> HintCertificationLatch:
        """変化の印（loop の側の `WorkloadHintTracker` が読む）。"""
        return self._latch

    def check(self, snapshot: RegistrySnapshot) -> None:
        """読み直した snapshot の production が起動時と違えば印を立てる。"""
        if self._latch.revoked:
            return
        certificate_ref = production_certificate_ref(snapshot)
        thermal_model = production_thermal_model(snapshot)
        if certificate_ref == self._certificate_ref and thermal_model == self._thermal_model:
            return
        self._latch.revoke()
        LOGGER.warning(
            "証明書か thermal_model の production が変わったため、"
            "workload hint を再起動まで B0 に下げる",
            extra={
                logs.FIELDS_KEY: {
                    "reason": "registry_production_changed",
                    "certificate_changed": certificate_ref != self._certificate_ref,
                    "thermal_model_changed": thermal_model != self._thermal_model,
                }
            },
        )


@dataclass(frozen=True, slots=True)
class StartupRegistry:
    """起動時に読んだ registry。`watch` は読めたときだけある（読めなければ監視しない）。

    ``hint_certificate`` は、証明書を読むよう頼まれ（``hint_latch`` を渡した）、registry を
    読めたときだけある（同じ snapshot から読んだ値。0152 §2.2）。
    """

    binding: RegistryBinding
    watch: RegistryWatch | None
    hint_certificate: HintCertificateSource | None = None


def read_startup_registry(
    root: Path | None,
    *,
    limits_dir: Path,
    hint_latch: HintCertificationLatch | None = None,
) -> StartupRegistry:
    """`--registry-root` を起動時に1回だけ読む。**例外で起動を止めない**（0077 §2.6）。

    ``root`` が None（`--registry-root` を省いた）なら、いまと同じく registry を読まない。
    ``hint_latch`` を渡すと（Workload Hint の B1 / B2 を設定した起動）、同じ snapshot から
    hint_certificate の production も読み、走行中の production の変化でその印を立てる
    （0152 §2.2 / §2.4）。
    """
    if root is None:
        LOGGER.info(
            "registry の root が指定されていないため読まない（Learned は使わない）",
            extra={logs.FIELDS_KEY: {"reason": "registry_root_not_given"}},
        )
        return StartupRegistry(binding=RegistryBinding.unbound(), watch=None)
    absolute = Path(os.path.abspath(root))
    snapshot_path = absolute / REGISTRY_STATE_FILENAME
    hint_source: HintCertificateSource | None = None
    try:
        limits = load_model_registry_limits(limits_dir)
        registry = ModelRegistry(absolute, limits=limits)
        # **同一性を読む前に取る。** 読んだ後に取ると、その間の promotion を見落とす
        identity = snapshot_identity(snapshot_path)
        if hint_latch is None:
            snapshot = registry.inspect()
        else:
            # 証明書と thermal_model の production を**同じ1つの snapshot** から読む（0152 §2.2）
            snapshot, loaded = registry.inspect_with_production(
                ArtifactKind.HINT_CERTIFICATE, CERTIFICATE_COMPATIBILITY
            )
            hint_source = certificate_source(
                snapshot, loaded, max_artifact_bytes=limits.max_artifact_bytes
            )
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
    hint_pins = (
        None
        if hint_latch is None or hint_source is None
        else HintProductionPins(
            certificate_ref=hint_source.certificate_ref,
            thermal_model=hint_source.thermal_model,
            latch=hint_latch,
        )
    )
    return StartupRegistry(
        binding=binding,
        watch=RegistryWatch(
            registry,
            snapshot_path=snapshot_path,
            identity=identity,
            pins=pins,
            hint=hint_pins,
        ),
        hint_certificate=hint_source,
    )


def _pin_fields(pins: LearnedExpectedArtifacts, role: LearnedRole) -> dict[str, str] | None:
    pinned = pins.for_role(role)
    return None if pinned is None else pinned.model_dump()
