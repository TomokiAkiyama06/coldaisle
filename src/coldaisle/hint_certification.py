"""``coldaisle-fand`` の起動時の証明書の照合（#288 / 決定記録 0152 §2.4 の段 3・0157 §2.3）。

fand は起動時に1回だけ、tick を始める前に、Model Registry の kind ``hint_certificate`` の
production を照合し、Workload Hint の実効の段階を決める::

    実効の段階 = min(設定の stage, 実装が動かせる最も高い段階, 照合の結果)

**どの失敗でも起動は止めない**（0066 §2.1 (c) / 0133 §2.6）。照合が通らなければ B0 で、
``requested_demand``・Reactive Guard・Critical Safety・Fallback には何も届かない（0152 §2.5。
ルール 2・3）。照合の手順（最初に落ちたところで止め、その理由を返す）:

1. ``--registry-root`` が渡され、snapshot を読めた（``registry_unbound``）
2. hint_certificate の production があり、checksum・形式が合う（``certificate_missing`` /
   ``certificate_unreadable``）
3. 証明書の ``schema_version`` を知っている・``result`` が ``passed``（``certificate_invalid``）
4. 自分の設定からゲートの仕様を導ける（``eps_bounds_invalid`` / ``hint_gate_invalid``）。
   証明書の ``eps`` ではなく、導いた値で検査する
5. 期待する証明書の本体を**自分で組み立てる**（``thermal_model`` は同じ snapshot の production、
   ``certified_domain`` / ``eps`` / ``eps_c`` / ``grid_points`` は導いたゲートの仕様、
   fand 側の部品は自分で計算した digest、worker 側の部品 ``optimizer`` / ``conditions`` だけは
   証明書の値）
6. 証明書の本体が期待する本体と**正準な符号化で丸ごと一致**し、``certificate_sha256`` が**期待する
   本体から計算した**値と一致する。理由には最初に食い違った欄を名指すが、合否は丸ごとの一致で決める

起動の形が ``isolated_module`` でなければ ``environment`` を計算せず ``environment_mismatch``
（0157 §2.3。新しい理由を作らない）。

ここは AI 層・API・eventd・管理ソケットから import しない。``coldaisle.control.safety`` /
``reactive`` / ``fallback`` / ``hardware`` も import しない（0152 §2.8 / 0133 §2.7）。Registry へは
書かない（読むのは ``learned_channel.registry_watch`` が渡す、同じ snapshot から読んだ値だけ）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from coldaisle import logs
from coldaisle.control.config import SafetyWorkloadHintConfig, SupervisorConfig
from coldaisle.control.hint_gate import (
    HintGateSpecError,
    calibration_sha256,
    derive_hint_gate_spec,
    hint_policy_sha256,
)
from coldaisle.control.learned_handoff import AvailableCalibration, UnavailableCalibration
from coldaisle.control.model_registry import (
    HINT_CERTIFICATE_FEATURE_SCHEMA_VERSION,
    HINT_CERTIFICATE_TARGET_SCHEMA_VERSION,
    ArtifactKind,
    ArtifactLoadResult,
    ArtifactLoadStatus,
    ArtifactRef,
    ModelCompatibility,
    RegistrySnapshot,
)
from coldaisle.control.schema import AuthorityStage
from coldaisle.hint_certificate import (
    COMPONENT_NAMES,
    CertifiedThermalModel,
    HintCertificate,
    HintCertificateBody,
    HintCertificateComponents,
    HintCertificateError,
    HintGateSpec,
    certificate_sha256,
    parse_hint_certificate,
)
from coldaisle.hint_certificate_digest import (
    LaunchForm,
    current_launch_form,
    environment_sha256,
    imported_package_dir,
    lockfile_sha256,
    source_tree_sha256,
)

LOGGER = logging.getLogger("coldaisle.hint_certification")

FAND_ENTRY_MODULE = "coldaisle.control_daemon"
"""``coldaisle-fand`` の入口の module（0157 §2.3 の 2）。設定から読まない（入口の識別）。"""

CERTIFICATE_COMPATIBILITY = ModelCompatibility(
    feature_schema_version=HINT_CERTIFICATE_FEATURE_SCHEMA_VERSION,
    target_schema_version=HINT_CERTIFICATE_TARGET_SCHEMA_VERSION,
    authority_stage=AuthorityStage.SHADOW,
)
"""fand が証明書を読むときの ``ModelCompatibility``（0156 §2.8 の2つの版）。

``authority_stage`` は ``shadow``（最も低い制御権）に固定する。証明書は authority を上げる道具では
なく（0160 §2.3）、認証ランナーも thermal_model を ``shadow`` で束縛する（0160 §2.4 の 1）。
"""


class HintCertification(StrEnum):
    """照合の結果の閉じた語彙（0152 §2.4）。``certified`` 以外はすべて B0。"""

    CERTIFIED = "certified"
    REGISTRY_UNBOUND = "registry_unbound"
    CERTIFICATE_MISSING = "certificate_missing"
    CERTIFICATE_UNREADABLE = "certificate_unreadable"
    CERTIFICATE_INVALID = "certificate_invalid"
    EPS_BOUNDS_INVALID = "eps_bounds_invalid"
    HINT_GATE_INVALID = "hint_gate_invalid"
    THERMAL_MODEL_MISMATCH = "thermal_model_mismatch"
    SOURCE_TREE_MISMATCH = "source_tree_mismatch"
    LOCKFILE_MISMATCH = "lockfile_mismatch"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    HINT_POLICY_MISMATCH = "hint_policy_mismatch"
    HINT_GATE_MISMATCH = "hint_gate_mismatch"
    OPTIMIZER_MISMATCH = "optimizer_mismatch"
    CONDITIONS_MISMATCH = "conditions_mismatch"
    CALIBRATION_MISMATCH = "calibration_mismatch"
    CERTIFIED_DOMAIN_MISMATCH = "certified_domain_mismatch"
    EPS_MISMATCH = "eps_mismatch"
    GRID_POINTS_MISMATCH = "grid_points_mismatch"
    CERTIFICATE_HASH_MISMATCH = "certificate_hash_mismatch"


def _component_mismatch(name: str) -> HintCertification:
    return HintCertification(f"{name}_mismatch")


@dataclass(frozen=True, slots=True)
class HintCertificateSource:
    """fand が**同じ1つの snapshot**から読んだ証明書と thermal_model の production（0152 §2.2）。

    ``thermal_model`` は、その snapshot の thermal_model の production（無ければ None）。
    ``certificate_ref`` は hint_certificate の production の pointer（無ければ None）。どちらも
    稼働中の production の監視の基準になる（0152 §2.4。下げるだけ）。
    """

    load: ArtifactLoadResult
    thermal_model: CertifiedThermalModel | None
    certificate_ref: ArtifactRef | None
    max_artifact_bytes: int


def production_thermal_model(snapshot: RegistrySnapshot) -> CertifiedThermalModel | None:
    """snapshot の thermal_model の production を、証明書の ``thermal_model`` の形で返す。"""
    slot = snapshot.production.get(ArtifactKind.THERMAL_MODEL)
    if slot is None:
        return None
    record = snapshot.artifacts.get(slot.active.key)
    if record is None:
        return None
    return CertifiedThermalModel(
        kind="thermal_model",
        model_id=slot.active.model_id,
        version=slot.active.version,
        artifact_sha256=record.metadata.sha256,
    )


def production_certificate_ref(snapshot: RegistrySnapshot) -> ArtifactRef | None:
    """snapshot の hint_certificate の production の pointer（無ければ None）。"""
    slot = snapshot.production.get(ArtifactKind.HINT_CERTIFICATE)
    return None if slot is None else slot.active


def certificate_source(
    snapshot: RegistrySnapshot, load: ArtifactLoadResult, *, max_artifact_bytes: int
) -> HintCertificateSource:
    """同じ snapshot から読んだ値を束ねる（``inspect_with_production`` の結果から作る）。"""
    return HintCertificateSource(
        load=load,
        thermal_model=production_thermal_model(snapshot),
        certificate_ref=production_certificate_ref(snapshot),
        max_artifact_bytes=max_artifact_bytes,
    )


@dataclass(frozen=True, slots=True)
class HintCertificationResult:
    """照合の結果。``certificate_sha256`` は証明書を読めたときの値（ログに出す。0152 §2.4）。"""

    certification: HintCertification
    launch_form: LaunchForm
    certificate_sha256: str | None = None
    components: HintCertificateComponents | None = None
    """照合が通ったときだけ、その証明書の部品（frame v4 の ``hint_certification`` の worker 側の
    期待値の出どころ。0152 §2.6）。通らなければ None。"""

    @property
    def certified(self) -> bool:
        """照合が通ったか。"""
        return self.certification is HintCertification.CERTIFIED


_LOAD_FAILURES: dict[ArtifactLoadStatus, HintCertification] = {
    ArtifactLoadStatus.NO_PRODUCTION: HintCertification.CERTIFICATE_MISSING,
    ArtifactLoadStatus.SCHEMA_MISMATCH: HintCertification.CERTIFICATE_INVALID,
    ArtifactLoadStatus.AUTHORITY_INCOMPATIBLE: HintCertification.CERTIFICATE_INVALID,
}
"""Registry の読み込みの失敗の写像。ここに無い status はすべて ``certificate_unreadable``。"""


class _Mismatch(Exception):
    def __init__(self, certification: HintCertification) -> None:
        super().__init__(certification.value)
        self.certification = certification


def verify_hint_certificate(
    *,
    source: HintCertificateSource | None,
    supervisor: SupervisorConfig,
    limits: SafetyWorkloadHintConfig | None,
    lockfile: Path | None,
    calibration: AvailableCalibration | UnavailableCalibration,
    launch_form: Callable[[str], LaunchForm] = current_launch_form,
) -> HintCertificationResult:
    """起動時の照合（0152 §2.4 の手順 1〜6）。**例外を外へ出さない**（起動を止めない）。

    ``source`` は ``read_startup_registry`` が同じ snapshot から読んだ値（registry を読んで
    いない・読めなければ None）。``calibration`` は起動時に ``--calibration`` から読んだ値
    （frame で worker へ運ぶのと同じ値）。``launch_form`` は起動の形の確かめ方だけを差し替える
    （試験で、pytest のプロセスを ``-I -m`` に見せるため）。digest の値には効かない。
    """
    form = launch_form(FAND_ENTRY_MODULE)
    certificate: HintCertificate | None = None
    try:
        certificate = _read_certificate(source)
        _check_against_expected(
            certificate,
            source=source,
            supervisor=supervisor,
            limits=limits,
            lockfile=lockfile,
            calibration=calibration,
            form=form,
        )
    except _Mismatch as mismatch:
        return HintCertificationResult(
            certification=mismatch.certification,
            launch_form=form,
            certificate_sha256=None if certificate is None else certificate.certificate_sha256,
        )
    except Exception:
        # 想定していない失敗でも起動は止めない。照合が通らなかったものとして B0 に倒す
        LOGGER.exception(
            "証明書を照合できなかった（workload hint は B0 に留める）",
            extra={logs.FIELDS_KEY: {"reason": HintCertification.CERTIFICATE_INVALID.value}},
        )
        return HintCertificationResult(
            certification=HintCertification.CERTIFICATE_INVALID,
            launch_form=form,
            certificate_sha256=None if certificate is None else certificate.certificate_sha256,
        )
    return HintCertificationResult(
        certification=HintCertification.CERTIFIED,
        launch_form=form,
        certificate_sha256=certificate.certificate_sha256,
        components=certificate.components,
    )


def _read_certificate(source: HintCertificateSource | None) -> HintCertificate:
    """手順 1〜3。"""
    if source is None:
        raise _Mismatch(HintCertification.REGISTRY_UNBOUND)
    load = source.load
    if load.status is not ArtifactLoadStatus.LOADED or load.artifact is None:
        raise _Mismatch(_LOAD_FAILURES.get(load.status, HintCertification.CERTIFICATE_UNREADABLE))
    try:
        certificate = parse_hint_certificate(
            load.artifact.payload, max_bytes=source.max_artifact_bytes
        )
    except HintCertificateError as error:
        raise _Mismatch(HintCertification(error.reason)) from None
    # Registry の metadata が名指す thermal_model と認証のハッシュ（0152 §5 の 2 (a)）が、証明書の
    # 本体と食い違う artifact は、登録の誤りか書き換えなので使わない（Registry は中身を読まない）
    metadata = load.artifact.metadata
    named = certificate.thermal_model
    if metadata.training_dataset_version != (
        f"{named.kind}/{named.model_id}/{named.version}"
    ) or metadata.source_runs != (certificate.certificate_sha256,):
        raise _Mismatch(HintCertification.CERTIFICATE_INVALID)
    return certificate


def _check_against_expected(
    certificate: HintCertificate,
    *,
    source: HintCertificateSource | None,
    supervisor: SupervisorConfig,
    limits: SafetyWorkloadHintConfig | None,
    lockfile: Path | None,
    calibration: AvailableCalibration | UnavailableCalibration,
    form: LaunchForm,
) -> None:
    """手順 4〜6。最初に食い違った欄で ``_Mismatch`` を投げる。"""
    assert source is not None
    try:
        spec = derive_hint_gate_spec(supervisor.workload_hint, limits)
    except HintGateSpecError as error:
        raise _Mismatch(HintCertification(error.reason.value)) from None
    if source.thermal_model is None or source.thermal_model != certificate.thermal_model:
        raise _Mismatch(HintCertification.THERMAL_MODEL_MISMATCH)
    components = _expected_components(
        certificate,
        spec=spec,
        supervisor=supervisor,
        lockfile=lockfile,
        calibration=calibration,
        form=form,
    )
    if spec.certified_domain != certificate.certified_domain:
        raise _Mismatch(HintCertification.CERTIFIED_DOMAIN_MISMATCH)
    if (spec.eps, spec.eps_c) != (certificate.eps, certificate.eps_c):
        raise _Mismatch(HintCertification.EPS_MISMATCH)
    if spec.grid_points != certificate.grid_points:
        raise _Mismatch(HintCertification.GRID_POINTS_MISMATCH)
    expected = HintCertificateBody.from_spec(
        thermal_model=source.thermal_model, components=components, spec=spec
    )
    # 合否は丸ごとの一致だけで決める（欄ごとに別々に通さない。0152 §2.4 の手順 6）
    if expected.canonical_bytes() != certificate.body().canonical_bytes():
        raise _Mismatch(HintCertification.CERTIFICATE_HASH_MISMATCH)
    if certificate.certificate_sha256 != certificate_sha256(expected):
        raise _Mismatch(HintCertification.CERTIFICATE_HASH_MISMATCH)


def _expected_components(
    certificate: HintCertificate,
    *,
    spec: HintGateSpec,
    supervisor: SupervisorConfig,
    lockfile: Path | None,
    calibration: AvailableCalibration | UnavailableCalibration,
    form: LaunchForm,
) -> HintCertificateComponents:
    """期待する部品を**名前の順に1つずつ**作り、食い違った最初の部品で止める（0152 §2.4 の 5）。

    計算できない部品（読めない・symlink を含む・起動の形が違う）は、その部品の不一致として扱う。
    重い ``environment`` は、それより前の部品が合ったときだけ計算する。
    """
    providers: dict[str, Callable[[], str]] = {
        "source_tree": lambda: source_tree_sha256(imported_package_dir()),
        "lockfile": lambda: _lockfile_sha256(lockfile),
        "environment": lambda: _environment_sha256(form),
        "hint_policy": lambda: hint_policy_sha256(supervisor),
        "hint_gate": spec.sha256,
        # worker 側の部品は証明書の値を置く（worker が frame で受け取って照合する。0152 §2.6）
        "optimizer": lambda: certificate.components.optimizer,
        "conditions": lambda: certificate.components.conditions,
        "calibration": lambda: calibration_sha256(calibration),
    }
    if set(providers) != set(COMPONENT_NAMES):
        raise RuntimeError("部品の一覧と照合の一覧が食い違う")
    values: dict[str, str] = {}
    for name in COMPONENT_NAMES:
        try:
            value = providers[name]()
        except Exception:
            raise _Mismatch(_component_mismatch(name)) from None
        if value != getattr(certificate.components, name):
            raise _Mismatch(_component_mismatch(name))
        values[name] = value
    return HintCertificateComponents(**values)


def _lockfile_sha256(lockfile: Path | None) -> str:
    if lockfile is None:
        # 渡されないときは不一致（0152 §2.4 / §5 の 4 (a)）
        raise ValueError("ロックファイルが渡されていない")
    return lockfile_sha256(lockfile)


def _environment_sha256(form: LaunchForm) -> str:
    if form is not LaunchForm.ISOLATED_MODULE:
        # 起動の形が違うプロセスは指紋を計算しない（0157 §2.3）
        raise ValueError("起動の形が isolated_module でない")
    return environment_sha256()
