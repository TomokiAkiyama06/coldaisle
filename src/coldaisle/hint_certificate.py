"""Workload Hint の単調性ゲートの**ゲートの仕様**と**証明書**の型（レイヤ横断）。

決定記録 0152 §2.3 / 0156 §2.7。

- :class:`HintGateSpec`: 設定から1つの純粋な関数（``coldaisle.control.hint_gate``）で導いた
  ゲートの仕様。正準な符号化（:meth:`HintGateSpec.canonical_bytes`）の SHA-256 が部品 ``hint_gate``
- :class:`HintCertificate`: kind ``hint_certificate`` の artifact の本体。``certificate_sha256``
  （:func:`certificate_sha256`）が 0066 / 0133 の言う「認証のハッシュ」

**型は形と不変条件だけを持つ。** 証明書の欄は照合の対象であって、値の出どころにしない
（0152 §2.3）。fand・worker は自分の設定から導いたゲートの仕様を使い、証明書の
``certified_domain`` / ``eps`` を読んで使わない。照合（0152 §2.4 / §2.6）は後の段で足す。

ここは ``coldaisle.control`` を import しない（``hint_certificate_digest`` と同じ扱い。Critical
Safety・Reactive Guard とも無関係で、制御の値を1つも作らない）。Regime / workload は制御の列挙の
**値の文字列**で持つ。
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldaisle.hint_certificate_digest import canonical_json_bytes, length_prefixed

HINT_GATE_SPEC_VERSION = "hint-gate-spec-v1"
"""ゲートの仕様の版の印（0156 §2.7）。形を変えるときは版を上げる。

証明書の metadata の ``feature_schema_version`` もこの値（0156 §2.8）。
"""
HINT_CERTIFICATE_SCHEMA_VERSION = "hint-certificate-v1"
"""証明書の本体の形の版（0152 §2.3 の ``schema_version``）。

証明書の metadata の ``target_schema_version`` もこの値（0156 §2.8）。
"""
HINT_CERTIFICATE_RESULT = "passed"
"""証明書の ``result`` が取れる唯一の値（不合格では証明書を作らない。0152 §2.3）。"""

_CERTIFICATE_HASH_TAG = b"coldaisle-hint-certificate-v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ENUM_VALUE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_ZONES = ("front", "rear", "top")
# Registry の ArtifactRef と同じ文法（control/model_registry.py。ここは control を import しないので
# 値を写し、試験で一致を固定する）。production に成りえない参照の証明書を作らせない。
ARTIFACT_MODEL_ID_PATTERN = r"^[a-z][a-z0-9_.-]*$"
ARTIFACT_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
ARTIFACT_MODEL_ID_MAX_LENGTH = 120
ARTIFACT_VERSION_MAX_LENGTH = 80

Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
NonNegativeFinite = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
UnitDemand = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ---------------------------------------------------------------- ゲートの仕様（0156）


class HintGateKey(_Frozen):
    """格子の (Regime, workload) の1つ（``context_overrides`` の鍵。0156 §2.2）。"""

    regime: str = Field(pattern=_ENUM_VALUE_PATTERN)
    workload: str = Field(pattern=_ENUM_VALUE_PATTERN)


class HintGateAxis(_Frozen):
    """数値の軸（0156 §2.3）。``points`` は1つ以上・狭義の昇順・有限。

    認証領域のこの軸の範囲は ``[minimum, maximum]`` = ``[points の先頭, points の末尾]``。
    範囲は ``points`` から導いた値で、別に書けない（食い違えば拒む）。
    """

    points: Annotated[tuple[FiniteFloat, ...], Field(min_length=1)]
    minimum: FiniteFloat
    maximum: FiniteFloat

    @model_validator(mode="after")
    def _points_are_increasing_and_bound_the_range(self) -> Self:
        for previous, current in zip(self.points, self.points[1:], strict=False):
            if not previous < current:
                raise ValueError("格子の軸の points は狭義の昇順にする")
        if self.minimum != self.points[0] or self.maximum != self.points[-1]:
            raise ValueError("格子の軸の範囲は points の先頭と末尾")
        return self

    @classmethod
    def of(cls, points: tuple[float, ...]) -> Self:
        """``points`` から範囲を導いて軸を作る（点の検査はモデルの不変条件）。"""
        if not points:
            raise ValueError("格子の軸の points は1つ以上にする")
        return cls(points=points, minimum=points[0], maximum=points[-1])


class HintGateDemandAxis(HintGateAxis):
    """Demand の軸（直前の effective demand）。点は ``[0, 1]``（0156 §2.3）。"""

    points: Annotated[tuple[UnitDemand, ...], Field(min_length=1)]


class HintGateWindowFeature(HintGateAxis):
    """温度以外の window feature の軸（metric 名と絶対値の点。0156 §2.4）。"""

    metric: str = Field(min_length=1, max_length=200)


class HintGateZoneDemands(_Frozen):
    """3系統の Demand の組（safety floor / baseline の代表集合の1つ。0156 §2.5）。"""

    front: UnitDemand
    rear: UnitDemand
    top: UnitDemand


class HintGateZoneRange(_Frozen):
    minimum: UnitDemand
    maximum: UnitDemand


class HintGateZoneRanges(_Frozen):
    """代表集合の系統ごとの範囲（組の列の外接箱。0156 §2.5）。"""

    front: HintGateZoneRange
    rear: HintGateZoneRange
    top: HintGateZoneRange

    @classmethod
    def bounding(cls, rows: tuple[HintGateZoneDemands, ...]) -> Self:
        """組の列の外接箱。"""
        if not rows:
            raise ValueError("代表集合は1つ以上にする")
        ranges: dict[str, HintGateZoneRange] = {}
        for zone in _ZONES:
            values = [getattr(row, zone) for row in rows]
            ranges[zone] = HintGateZoneRange(minimum=min(values), maximum=max(values))
        return cls(**ranges)


def _rows_are_unique(rows: tuple[HintGateZoneDemands, ...], name: str) -> None:
    if len(set(rows)) != len(rows):
        raise ValueError(f"{name} の組は重複させない")


class HintGateDomain(_Frozen):
    """認証領域（格子の各軸の範囲と刻み。0066 §2.1 (d) / 0152 §2.3 の ``certified_domain``）。

    並びの規則（正準な形）: ``hint_keys`` は (regime, workload) の昇順、``window_features`` は
    metric 名の昇順、``safety_floors`` / ``baselines`` は**書かれた順**（0156 §2.7）。
    """

    hint_keys: Annotated[tuple[HintGateKey, ...], Field(min_length=1)]
    previous_demand: HintGateDemandAxis
    temperature_cpu: HintGateAxis
    """基準の context の ``target_band.cpu_temperature.upper_c`` からの差 ℃（0156 §2.4）。"""
    temperature_gpu: HintGateAxis
    window_features: tuple[HintGateWindowFeature, ...]
    safety_floors: Annotated[tuple[HintGateZoneDemands, ...], Field(min_length=1)]
    safety_floor_range: HintGateZoneRanges
    baselines: Annotated[tuple[HintGateZoneDemands, ...], Field(min_length=1)]
    baseline_range: HintGateZoneRanges

    @model_validator(mode="after")
    def _domain_is_canonical(self) -> Self:
        keys = [(key.regime, key.workload) for key in self.hint_keys]
        if keys != sorted(set(keys)):
            raise ValueError("hint_keys は重複させず (regime, workload) の昇順に並べる")
        metrics = [feature.metric for feature in self.window_features]
        if metrics != sorted(set(metrics)):
            raise ValueError("window_features は metric を重複させず名前の昇順に並べる")
        _rows_are_unique(self.safety_floors, "safety_floors")
        _rows_are_unique(self.baselines, "baselines")
        if self.safety_floor_range != HintGateZoneRanges.bounding(self.safety_floors):
            raise ValueError("safety_floor_range は safety_floors の外接箱")
        if self.baseline_range != HintGateZoneRanges.bounding(self.baselines):
            raise ValueError("baseline_range は baselines の外接箱")
        return self

    def grid_points(self) -> int:
        """格子の点の数（0156 §2.3 の積）。直前 Demand は3系統の直積。"""
        count = len(self.hint_keys)
        for _zone in _ZONES:
            count *= len(self.previous_demand.points)
        count *= len(self.temperature_cpu.points) * len(self.temperature_gpu.points)
        for feature in self.window_features:
            count *= len(feature.points)
        return count * len(self.safety_floors) * len(self.baselines)


class HintGateSpec(_Frozen):
    """ゲートの仕様（0152 §2.3 / 0156 §2.7）。``hint_gate`` の digest の入力。

    ``status`` / ``basis`` と上限（``max_eps`` ほか）は入れない（0156 §2.7）。
    """

    spec_version: Literal["hint-gate-spec-v1"]
    eps: NonNegativeFinite
    eps_c: NonNegativeFinite
    certified_domain: HintGateDomain
    grid_points: int = Field(gt=0)

    @model_validator(mode="after")
    def _grid_points_follow_the_domain(self) -> Self:
        if self.grid_points != self.certified_domain.grid_points():
            raise ValueError("grid_points は certified_domain の点の数")
        return self

    def canonical_bytes(self) -> bytes:
        """正準な符号化（canonical JSON。0156 §2.7）。"""
        return canonical_json_bytes(self.model_dump(mode="json"))

    def sha256(self) -> str:
        """部品 ``hint_gate``: 正準な符号化の SHA-256（0152 §2.3）。"""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


# ---------------------------------------------------------------- 証明書（0152 §2.3）


class CertifiedThermalModel(_Frozen):
    """認証した thermal_model の ``ArtifactRef`` と ``artifact_sha256``（0152 §2.3）。"""

    kind: Literal["thermal_model"]
    model_id: str = Field(
        pattern=ARTIFACT_MODEL_ID_PATTERN, max_length=ARTIFACT_MODEL_ID_MAX_LENGTH
    )
    version: str = Field(pattern=ARTIFACT_VERSION_PATTERN, max_length=ARTIFACT_VERSION_MAX_LENGTH)
    artifact_sha256: Sha256


class HintCertificateComponents(_Frozen):
    """0066 §2.1 (c) が束ねるものの部品ごとの SHA-256（0152 §2.3 の表）。"""

    source_tree: Sha256
    lockfile: Sha256
    environment: Sha256
    hint_policy: Sha256
    hint_gate: Sha256
    optimizer: Sha256
    conditions: Sha256
    calibration: Sha256


COMPONENT_NAMES: tuple[str, ...] = tuple(HintCertificateComponents.model_fields)
"""部品の名前（``certificate_sha256`` の符号化の順）。"""


class HintCertificateBody(_Frozen):
    """証明書の本体（``certificate_sha256`` を除く全欄。0152 §2.3）。"""

    schema_version: Literal["hint-certificate-v1"]
    result: Literal["passed"]
    thermal_model: CertifiedThermalModel
    components: HintCertificateComponents
    certified_domain: HintGateDomain
    eps: NonNegativeFinite
    eps_c: NonNegativeFinite
    grid_points: int = Field(gt=0)

    @model_validator(mode="after")
    def _grid_points_follow_the_domain(self) -> Self:
        if self.grid_points != self.certified_domain.grid_points():
            raise ValueError("grid_points は certified_domain の点の数")
        return self

    @classmethod
    def from_spec(
        cls,
        *,
        thermal_model: CertifiedThermalModel,
        components: HintCertificateComponents,
        spec: HintGateSpec,
    ) -> Self:
        """ゲートの仕様の出力を**そのまま** ``certified_domain`` / ``eps`` / ``grid_points``
        に書く。

        0152 §2.3。``components.hint_gate`` がその仕様の digest でなければ ``ValueError``。
        """
        if components.hint_gate != spec.sha256():
            raise ValueError("components.hint_gate はゲートの仕様の digest にする")
        return cls(
            schema_version="hint-certificate-v1",
            result="passed",
            thermal_model=thermal_model,
            components=components,
            certified_domain=spec.certified_domain,
            eps=spec.eps,
            eps_c=spec.eps_c,
            grid_points=spec.grid_points,
        )

    def canonical_bytes(self) -> bytes:
        """本体の正準な符号化（0152 §2.4 の手順 6 の「丸ごと一致」で比べる形）。"""
        return canonical_json_bytes(self.model_dump(mode="json"))


def certificate_sha256(body: HintCertificateBody) -> str:
    """認証のハッシュ（0152 §2.3 の ``certificate_sha256``）。

    ``thermal_model``・``components``（部品の名前と digest を :data:`COMPONENT_NAMES` の順に）・
    ``certified_domain``・``eps``・``eps_c`` を、版のタグを先頭にして長さ付きで連結した列の
    SHA-256。
    数と構造の値は canonical JSON で符号化する。
    """
    model = body.thermal_model
    parts: list[bytes] = [
        _CERTIFICATE_HASH_TAG,
        model.kind.encode("utf-8"),
        model.model_id.encode("utf-8"),
        model.version.encode("utf-8"),
        model.artifact_sha256.encode("ascii"),
    ]
    for name in COMPONENT_NAMES:
        parts.extend((name.encode("ascii"), str(getattr(body.components, name)).encode("ascii")))
    parts.append(canonical_json_bytes(body.certified_domain.model_dump(mode="json")))
    parts.append(canonical_json_bytes(body.eps))
    parts.append(canonical_json_bytes(body.eps_c))
    return hashlib.sha256(length_prefixed(parts)).hexdigest()


class HintCertificate(HintCertificateBody):
    """kind ``hint_certificate`` の artifact の JSON（本体の欄と ``certificate_sha256``）。

    型は ``certificate_sha256`` が本体から計算した値であることを**確かめない**。照合では
    fand が自分で組み立てた期待する本体から計算した値と比べる（0152 §2.4 の手順 6。証明書自身の
    欄から計算し直したハッシュだけが合う、という通り方を作らない）。
    """

    certificate_sha256: Sha256

    @classmethod
    def issue(cls, body: HintCertificateBody) -> Self:
        """本体から ``certificate_sha256`` を計算して証明書を作る（認証ランナーが使う）。"""
        return cls(**dict(body), certificate_sha256=certificate_sha256(body))

    def body(self) -> HintCertificateBody:
        """``certificate_sha256`` を除いた本体。"""
        fields = {name: getattr(self, name) for name in HintCertificateBody.model_fields}
        return HintCertificateBody(**fields)

    def to_json_bytes(self) -> bytes:
        """artifact として書く bytes（canonical JSON）。"""
        return canonical_json_bytes(self.model_dump(mode="json"))


CertificateReadReason = Literal["certificate_unreadable", "certificate_invalid"]


class HintCertificateError(ValueError):
    """証明書を読めない（``certificate_unreadable``）・形が正しくない（``certificate_invalid``）。

    ``reason`` は 0152 §2.4 の閉じた語彙。メッセージに証明書の値・パスを入れない。
    """

    def __init__(self, reason: CertificateReadReason, message: str) -> None:
        super().__init__(message)
        self.reason: CertificateReadReason = reason


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("証明書の JSON に重複した鍵がある")
    return dict(pairs)


def _reject_constant(value: str) -> object:
    raise ValueError(f"証明書の JSON に非有限の数がある: {value}")


_KNOWN_SCHEMA_VERSIONS = frozenset({HINT_CERTIFICATE_SCHEMA_VERSION})


def parse_hint_certificate(data: bytes, *, max_bytes: int) -> HintCertificate:
    """証明書の bytes を読む（0152 §2.3 / §2.4 の手順 2・3）。

    - ``max_bytes`` を超える・UTF-8 でない・JSON でない・重複した鍵・``NaN`` / ``Infinity`` →
      ``certificate_unreadable``
    - 知らない ``schema_version``・``result`` が ``passed`` でない・形の誤り →
      ``certificate_invalid``

    ``max_bytes`` は Registry の ``max_artifact_bytes``（0062 §2.1）。読む側が上限＋1 byte だけを
    読むのは呼び出し側（後の段）の責務で、ここは渡された bytes の長さを確かめる。
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes は正にする")
    if len(data) > max_bytes:
        raise HintCertificateError("certificate_unreadable", "証明書が大きさの上限を超える")
    try:
        loaded = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise HintCertificateError("certificate_unreadable", "証明書を JSON として読めない") from (
            error
        )
    if not isinstance(loaded, dict):
        raise HintCertificateError("certificate_unreadable", "証明書の JSON が object でない")
    if loaded.get("schema_version") not in _KNOWN_SCHEMA_VERSIONS:
        raise HintCertificateError("certificate_invalid", "証明書の schema_version を知らない")
    if loaded.get("result") != HINT_CERTIFICATE_RESULT:
        raise HintCertificateError("certificate_invalid", "証明書の result が passed でない")
    try:
        return HintCertificate.model_validate_json(data)
    except ValidationError as error:
        # 値を含むメッセージ（pydantic の input）を外へ出さない。欄の位置だけを残す。
        locations = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        raise HintCertificateError(
            "certificate_invalid", f"証明書の形が正しくない: {locations[:5]}"
        ) from None
