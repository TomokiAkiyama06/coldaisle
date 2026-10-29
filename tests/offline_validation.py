"""試験で candidate を validated にするときの ``offline_evaluation_ref`` の渡し方（決定記録 0172）。

反実仮想 thermal model（``thermal_model`` / ``counterfactual_action``）では ``mark_validated()`` が
ref の形（``thermal-offline-v1:<64 hex>``）と、報告の bytes の digest が ref と一致することを
確かめる（0172 §5 の 10・11）。Registry は報告の中身を読まないので、評価そのものを試験しない
試験は、digest の合う小さな bytes を渡す。他の kind は従来どおり自由記述の ref を渡す。
"""

from __future__ import annotations

from typing import Any

from coldaisle.control.model_registry import ArtifactCapability, ArtifactKind, ArtifactMetadata
from coldaisle.thermal_evaluation_report import offline_evaluation_ref

STUB_REPORT = b"{}\n"
"""評価の中身を試験しない試験の報告の bytes（Registry は中身を読まない）。"""

STUB_REF = offline_evaluation_ref(STUB_REPORT)


def offline_validation(metadata: ArtifactMetadata, free_ref: str) -> dict[str, Any]:
    """``mark_validated()`` の ``offline_evaluation_ref`` と、要るなら報告の bytes。"""
    if (
        metadata.kind is ArtifactKind.THERMAL_MODEL
        and metadata.capability is ArtifactCapability.COUNTERFACTUAL_ACTION
    ):
        return {"offline_evaluation_ref": STUB_REF, "offline_evaluation_report": STUB_REPORT}
    return {"offline_evaluation_ref": free_ref}
