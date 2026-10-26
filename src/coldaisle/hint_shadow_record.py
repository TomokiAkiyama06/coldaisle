"""``hint_shadow_records`` の1行の形と語彙（レイヤ横断。#288 / 決定記録 0133 §2.5a / 0162 §2.5）。

書き手は ``coldaisle-fand`` の Learned の経路（``coldaisle.learned_channel``）で、読み手は store と
集計の CLI。``coldaisle.store.models`` に置くと、import した時点で ``coldaisle/store/__init__.py``
が SQLite の永続化層まで読み込み、Learned の経路と worker に SQLite 層を載せない保証
（``tests/test_learned_channel.py`` / ``tests/test_learned_worker.py``）が崩れる。そこで
``measurement.py`` と同じくパッケージ直下に置き、``coldaisle.store.models`` は同じオブジェクトを
再 export する。

**ここは pydantic と標準ライブラリ以外を import しない。**
"""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HintShadowStatus(StrEnum):
    """``hint_shadow_records.status`` の閉じた語彙（#288 / 決定記録 0133 §2.5a）。"""

    RECORDED = "recorded"
    """ヒント有りの plan がある。"""
    SKIPPED = "skipped"
    """worker が解かなかった・打ち切った（理由つき）。"""
    REJECTED = "rejected"
    """fand の検証に落ちた（0133 §2.5 の ``hint_record_rejected``）。"""


class HintShadowReason(StrEnum):
    """``status`` が ``recorded`` 以外のときの理由（決定記録 0133 §2.5a / §2.8 / 0066 §2.1 (d)）。

    0133 §2.5a が列挙した値に、0152 §2.6 / 0162 §2.3 が足した ``certification_mismatch`` /
    ``counters_decreased`` と、0162 §2.2 の「同じ元の tick の2件目」の ``duplicate_source`` を置く。
    語彙を足すときはここへ足す（DB は形だけを検査する）。
    """

    BUDGET_EXHAUSTED = "budget_exhausted"
    SHADOW_BUSY = "shadow_busy"
    OUT_OF_CERTIFIED_DOMAIN = "out_of_certified_domain"
    MONOTONICITY_VIOLATED = "monotonicity_violated"
    STALE = "stale"
    RUN_ID_MISMATCH = "run_id_mismatch"
    ARTIFACT_MISMATCH = "artifact_mismatch"
    BASE_MISMATCH = "base_mismatch"
    CERTIFICATION_MISMATCH = "certification_mismatch"
    """worker の子の照合（``skipped``）か、記録の証明書・``conditions`` の digest が fand の照合した
    証明書と違う（``rejected``。0152 §2.6）。"""
    COUNTERS_DECREASED = "counters_decreased"
    """同じ epoch の中で worker の累積の数のどれかが減った（``rejected``。0162 §2.3）。"""
    DUPLICATE_SOURCE = "duplicate_source"
    """同じ元の tick の記録を既に通した（``rejected``。0162 §2.2 の「2件目は
    ``hint_record_rejected``」）。"""


HINT_SHADOW_RECORD_SCHEMA_VERSION = 1
"""``hint_shadow_records.schema_version`` に書く ``record_json`` の形の版（0162 §2.5 の PR-C）。"""


HINT_SHADOW_FORBIDDEN_KEYS = frozenset({"note", "source", "peer_uid"})
"""``record_json`` のどの深さにも置かない鍵（決定記録 0133 §2.5a / 0064 §2.9）。

ヒントの自由記述・書き手の生の出どころ・接続の uid を、制御の記録へ写さない。
"""


def _forbidden_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        found = {key for key in value if key in HINT_SHADOW_FORBIDDEN_KEYS}
        for item in value.values():
            found |= _forbidden_keys(item)
        return found
    if isinstance(value, list):
        nested: set[str] = set()
        for item in value:
            nested |= _forbidden_keys(item)
        return nested
    return set()


class HintShadowRecord(BaseModel):
    """ヒント有りの solve の記録の1行（``hint_shadow_records``。#288 / 決定記録 0133 §2.5a）。

    **追記のみ。** 書くのは ``coldaisle-fand`` だけ。``status`` が ``recorded`` の行だけが
    ``record_json`` を持ち、それ以外の行だけが ``reason`` を持つ。表の CHECK と同じ条件をここでも
    確かめる。``record_json`` の中身の形は ``coldaisle.learned_channel.hint_shadow`` が定める
    （決定記録 0162 §2.5 の PR-C）。

    ``worker_instance_id`` と累積の数（0162 §2.3）は**すべての行に必須**。fand は封筒の検証を
    通ったメッセージの値をそのまま書く。``recorded`` 以外の行も、どの epoch のどの数を運んだかを
    集計（0162 §2.4）が振り分けられるようにするため。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int | None = Field(default=None, ge=1)
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_tick_id: int = Field(ge=0)
    source_ts_ms: int = Field(ge=0)
    source_monotonic_ms: int = Field(ge=0)
    received_monotonic_ms: int = Field(ge=0)
    received_ts_ms: int = Field(ge=0)
    schema_version: int = Field(ge=1)
    status: HintShadowStatus
    reason: HintShadowReason | None = None
    record_json: str | None = None
    worker_instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    shadow_dropped_backpressure: int = Field(ge=0)
    shadow_record_too_large: int = Field(ge=0)
    shadow_unavailable: int = Field(ge=0)

    @model_validator(mode="after")
    def _fields_match_the_status(self) -> HintShadowRecord:
        recorded = self.status is HintShadowStatus.RECORDED
        if recorded != (self.reason is None):
            raise ValueError("reason を持つのは recorded 以外の行だけ")
        if recorded != (self.record_json is not None):
            raise ValueError("record_json を持つのは recorded の行だけ")
        if self.record_json is not None:
            try:
                decoded = json.loads(self.record_json)
            except json.JSONDecodeError as exc:
                raise ValueError("record_json は JSON object にする") from exc
            if not isinstance(decoded, dict):
                raise ValueError("record_json は JSON object にする")
            forbidden = _forbidden_keys(decoded)
            if forbidden:
                raise ValueError(f"record_json に載せない鍵がある: {sorted(forbidden)}")
        return self
