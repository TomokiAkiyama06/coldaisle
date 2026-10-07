"""`coldaisle-fand` が起動時に読んだ registry の snapshot から作る束縛（決定記録 0077 §2.6）。

**1つの `RegistryProvenance` から次をすべて作る。** 別々に読むと、途中で promotion が起きたときに
Gate と trace が別の版を名乗る（0077 §2.6）。

- `provenance` → `ControlLoop(registry=...)`（trace の `registry`。毎 tick 同じ値。0071 §2.5）
- `expected_artifacts` → frame の `expected_artifacts`（loop が `provenance` から同じ規則で作る）
- `thermal_model` の production → `ControllerGate(expected_model_version=...,
  expected_artifact_sha256=...)` と `AuthorityRuntime(loaded_artifact_sha256=...)`（0089 §2.1）
- `supervisor_policy` の production → `SupervisorCoordinator(expected_rl_identity=...)`（0074 §5）

production が無い・registry を読んでいない・読めないときは、Gate へ空でない番兵
`UNCONFIGURED_MODEL_VERSION` と `expected_artifact_sha256=None` を渡す（どの Learned 提案も
採らない。Gate の契約は変えない。0059 §2.1）。**この module は registry を読まない**
（読むのは合成の起点）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from coldaisle import logs
from coldaisle.control.learned_handoff import LearnedExpectedArtifacts
from coldaisle.control.model_registry import RegistrySnapshot
from coldaisle.control.schema import RegistryProvenance, SupervisorPolicyIdentity

LOGGER = logging.getLogger("coldaisle.control.registry_binding")

UNCONFIGURED_MODEL_VERSION = "unconfigured"
"""束縛する `thermal_model` が無い起動で Gate に渡す期待版（0077 §2.6 の番兵）。

**値そのものに意味は無い。** `ControllerGate` は空の期待版を拒むので、空でない値を置く。
どの worker の提案もこの版を名乗らないので、Gate は必ず Fallback を選ぶ。
"""


@dataclass(frozen=True, slots=True)
class RegistryBinding:
    """起動時の registry の版から作った、Gate・Coordinator・trace・frame の期待値の組。"""

    provenance: RegistryProvenance
    expected_artifacts: LearnedExpectedArtifacts
    expected_model_version: str
    expected_artifact_sha256: str | None
    expected_rl_identity: SupervisorPolicyIdentity | None

    @classmethod
    def unbound(cls) -> RegistryBinding:
        """registry を読んでいない・読めなかった起動（Learned を無効にする）。"""
        return cls.from_provenance(RegistryProvenance.unbound())

    @classmethod
    def from_snapshot(cls, snapshot: RegistrySnapshot) -> RegistryBinding:
        """1つの snapshot から作る。

        snapshot が自己矛盾していれば `ValueError`（呼び出し側は読めなかったとして扱う）。
        """
        return cls.from_provenance(snapshot.trace_provenance())

    @classmethod
    def from_provenance(cls, provenance: RegistryProvenance) -> RegistryBinding:
        """trace に載せる版から、残りの期待値をすべて導く（出どころを1つにする）。"""
        pins = LearnedExpectedArtifacts.from_provenance(provenance)
        thermal = pins.thermal_model
        return cls(
            provenance=provenance,
            expected_artifacts=pins,
            # MPC の提案が名乗る版は attestation の semantic version
            # （`MpcModelBinding.model_version`）。model の取り違えは artifact の sha256 の
            # 照合で止める（0059 §2.1）
            expected_model_version=(
                UNCONFIGURED_MODEL_VERSION if thermal is None else thermal.version
            ),
            expected_artifact_sha256=None if thermal is None else thermal.artifact_sha256,
            expected_rl_identity=_rl_identity(pins),
        )


def _rl_identity(pins: LearnedExpectedArtifacts) -> SupervisorPolicyIdentity | None:
    pinned = pins.supervisor_policy
    if pinned is None:
        return None
    try:
        return SupervisorPolicyIdentity(
            model_id=pinned.model_id,
            version=pinned.version,
            artifact_sha256=pinned.artifact_sha256,
        )
    except ValidationError:
        # registry の版の書式（semver の build metadata `+` など）が RL の識別に収まらない。
        # 識別を作れない policy の出力は1件も採らない（RulePolicy のまま）。安全側にしか効かない
        LOGGER.error(
            "supervisor_policy の production から RL の識別を作れないため、RL の出力を採らない",
            extra={
                logs.FIELDS_KEY: {
                    "reason": "rl_identity_unrepresentable",
                    "model_id": pinned.model_id,
                    "version": pinned.version,
                }
            },
        )
        return None
