"""Learned worker との経路の封筒（版付きの JSON。1メッセージ = 1 `SOCK_SEQPACKET` の記録）。

決定記録 0077 §2.8。

封筒は `schema_version`・`run_id`・`role`・本文（`body`）を持つ。知らない版・知らない本文は捨てる。

- worker → `coldaisle-fand`: `heartbeat`（受け渡し口も受信時刻も変えない）と、役割ごとの結果。
  MPC は `mpc_result`（`MpcProposal`。提案か失敗）、RL Supervisor は `supervisor_result`
  （`DeliveredSupervisorOutput`。出力と artifact の識別。0077 §2.8 の段階 4 / #89）。
  **どの本文をどのソケットで受け取るかは受付が役割で決める**（別の役割の本文は捨てる）
- `coldaisle-fand` → worker: `hello`（接続を認めた最初の応答。`heartbeat_interval_ms` と
  `run_id`）と `frame`（毎 tick の `LearnedFrame`）

- MPC worker → `coldaisle-fand`: `hint_shadow`（Workload Hint の B1 の Shadow solver の記録。
  決定記録 0133 §2.5 / §2.5a・0152 §2.6・0162 §2.3 / §2.5 の PR-B）。基準の `mpc_result` の**後に**
  別のメッセージとして送る。fand の受信・検証・記録は PR-C で足す（それまで fand は
  `unsupported_body` として捨て、生存の知らせに数えない）

worker からの本文は `MpcProposal` / `DeliveredSupervisorOutput` と、Shadow の記録
（`requested_demand` の段の値と予測の写しだけ）の型しか運べず、Demand の上書き・モード・
authority・束縛の用途（`origin`）を表す型を持たない（0077 §2.4 の 6 / §2.8・0162 §2.1、
AGENTS.md ルール2）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.learned_handoff import LearnedFrame, LearnedRole, PinnedArtifact
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.mpc.counterfactual import PlannedTarget
from coldaisle.control.schema import (
    ControlConfigDigest,
    Demand,
    PerZone,
    SupervisorTargetBand,
)
from coldaisle.control.supervisor.policy import DeliveredSupervisorOutput
from coldaisle.hint_certificate import Sha256

ENVELOPE_SCHEMA_VERSION: Literal[1] = 1

RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
"""`run_id` の形（`control_admin.runtime.new_run_id()` と同じ。個体識別子を含まない）。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HeartbeatBody(_Strict):
    """worker が提案・失敗を送らない間も送る生存の知らせ（0077 §2.7）。"""

    kind: Literal["heartbeat"]


class MpcResultBody(_Strict):
    """Learned MPC worker の1回の結果（提案か失敗）。"""

    kind: Literal["mpc_result"]
    result: MpcProposal


class SupervisorResultBody(_Strict):
    """RL Supervisor worker の1回の出力（0077 §2.8）。失敗の本文は無い（送らないだけ。§2.6）。"""

    kind: Literal["supervisor_result"]
    result: DeliveredSupervisorOutput


HINT_SHADOW_RECORD_SCHEMA_VERSION: Literal[1] = 1
"""Shadow の記録の形の版（``hint_shadow_records.schema_version`` に写す。0133 §2.5a）。"""


class HintShadowStatus(StrEnum):
    """worker が送る Shadow の記録の状態（0133 §2.5a の ``status`` のうち worker が決める2つ）。

    ``rejected``（fand の検証に落ちた）は fand だけが付ける（PR-C）。
    """

    RECORDED = "recorded"
    """ヒント有りの plan がある（worker の領域の判定と 2 条件に通った）。"""
    SKIPPED = "skipped"
    """worker が解かなかった・打ち切った・worker の判定に落ちた（理由つき）。"""


class HintShadowSkipReason(StrEnum):
    """``skipped`` の理由（閉じた語彙。0133 §2.5a / §2.8・0152 §2.6・0162 §2.3）。

    fand はこの値を ``hint_shadow_records.reason`` へ写す（PR-C で ``certification_mismatch`` を
    ``HintShadowReason`` に足す）。
    """

    BUDGET_EXHAUSTED = "budget_exhausted"
    """基準の solve の残りの評価の回数・打ち切り時刻が無い、またはヒント有りが解けなかった。"""
    SHADOW_BUSY = "shadow_busy"
    """Shadow solver が前の依頼の途中だった（依頼を捨てた。待ち行列を作らない）。"""
    OUT_OF_CERTIFIED_DOMAIN = "out_of_certified_domain"
    """worker の認証領域の判定（実際の window の全 frame。0156 §2.4）に落ちた。"""
    MONOTONICITY_VIOLATED = "monotonicity_violated"
    """worker の 2 条件（0066 §2.1 (b)）に落ちた。"""
    CERTIFICATION_MISMATCH = "certification_mismatch"
    """worker 側の部品・起動の形・``window_ms`` / ``stale_after_ms`` の照合に落ちた。

    0152 §2.6 / 0157 §2.3 / 0162 §2.2。
    """
    ARTIFACT_MISMATCH = "artifact_mismatch"
    """子が固定された artifact を読めない・digest が合わない（0133 §2.5）。"""
    BASE_MISMATCH = "base_mismatch"
    """ヒント有りの解の anchor 推論が基準の解のものと違う。

    同じ window・同じ model で解いていない解を基準と比べない。
    """


class HintShadowCounters(_Strict):
    """worker のプロセスの中の累積の数（0162 §2.3）。

    ``worker_instance_id`` の epoch の中で減らない。
    """

    shadow_dropped_backpressure: int = Field(ge=0)
    """送れずに捨てた記録（新しい記録で置き換えた・期限を過ぎた・送信に失敗した）。"""
    shadow_record_too_large: int = Field(ge=0)
    """``limits.max_shadow_record_bytes`` を超えて捨てた記録（切り詰めない）。"""
    shadow_unavailable: int = Field(ge=0)
    """Shadow solver の子が使えず、依頼を渡せなかった回数（記録を作らない。0162 §2.3）。"""


class HintShadowPlan(_Strict):
    """1つの plan の写し（0133 §2.5a）。``requested_demand`` の段の値と、予測の全 offset。"""

    requested: PerZone[Demand]
    targets: Annotated[tuple[PlannedTarget, ...], Field(min_length=1)]
    """``PlanPrediction.targets`` の全 offset（fand の 2 条件の再検査に使う）。"""


class HintShadowPlans(_Strict):
    """``recorded`` の記録の両 plan とヒント有りの context の band（0133 §2.5a）。"""

    base: HintShadowPlan
    hint: HintShadowPlan
    hint_target_band: SupervisorTargetBand


class HintShadowBody(_Strict):
    """Workload Hint の B1 の Shadow solver の1件の記録（0133 §2.5 / §2.5a・0162 §2.3）。

    基準の ``MpcProposal`` を送った**後に**、別のメッセージとして MPC の接続からだけ送る。
    ``base_result_digest`` で対になる基準の結果（``MpcProposal.result_digest()``）を指し、
    ``source_*`` で元の frame の snapshot を指す。**Demand の上書き・モード・authority を表す欄を
    持たない**（``plans`` は値の写しで、受け渡し口にも Gate にも入らない。0162 §2.1）。
    """

    kind: Literal["hint_shadow"]
    record_schema_version: Literal[1]
    worker_instance_id: str = Field(pattern=RUN_ID_PATTERN)
    """worker のプロセスの起動ごとの乱数の識別子（``run_id`` と同じ形。個体識別子を含まない）。"""
    counters: HintShadowCounters
    """送った時点の累積の数（``worker_instance_id`` と組で1つの epoch。0162 §2.3）。"""
    source_tick_id: int = Field(ge=0)
    source_ts_ms: int = Field(ge=0)
    source_monotonic_ms: int = Field(ge=0)
    base_result_digest: Sha256
    """対になる基準の ``MpcProposal.result_digest()``。"""
    thermal_model: PinnedArtifact
    """元の frame が固定した artifact（``expected_artifacts.thermal_model``）。"""
    config: ControlConfigDigest
    """元の frame の ``config``（worker の設定と一致したものだけが基準の solve に使われる）。"""
    certificate_sha256: Sha256
    """照合に使った認証のハッシュ（元の frame の ``hint_certification``。0152 §2.6）。"""
    conditions_sha256: Sha256 | None
    """子が束縛から計算した ``conditions`` の digest。計算する前に止まった記録では None。"""
    status: HintShadowStatus
    reason: HintShadowSkipReason | None
    plans: HintShadowPlans | None

    @model_validator(mode="after")
    def _fields_match_the_status(self) -> Self:
        recorded = self.status is HintShadowStatus.RECORDED
        if recorded != (self.reason is None):
            raise ValueError("reason を持つのは skipped の記録だけ")
        if recorded != (self.plans is not None):
            raise ValueError("plans を持つのは recorded の記録だけ")
        if recorded and self.conditions_sha256 is None:
            raise ValueError("recorded の記録には conditions の digest を載せる")
        return self


InboundBody = Annotated[
    HeartbeatBody | MpcResultBody | SupervisorResultBody | HintShadowBody,
    Field(discriminator="kind"),
]


class InboundEnvelope(_Strict):
    """worker から届く封筒。`role` は照合であって認可ではない（役割はソケットで決まる）。"""

    schema_version: Literal[1]
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    role: LearnedRole
    body: InboundBody


class HelloBody(_Strict):
    """接続を認めた最初の応答（0077 §2.7）。"""

    kind: Literal["hello"] = "hello"
    heartbeat_interval_ms: int = Field(ge=1)


class FrameBody(_Strict):
    """毎 tick の worker の入力（0077 §2.3）。"""

    kind: Literal["frame"] = "frame"
    frame: LearnedFrame


class OutboundEnvelope(_Strict):
    """`coldaisle-fand` から worker へ送る封筒。"""

    schema_version: Literal[1] = ENVELOPE_SCHEMA_VERSION
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    role: LearnedRole
    body: Annotated[HelloBody | FrameBody, Field(discriminator="kind")]


def parse_inbound(data: bytes) -> InboundEnvelope:
    """worker から届いた1メッセージを検証する。

    不正なら `ValueError`（`ValidationError` を含む）。
    """
    return InboundEnvelope.model_validate_json(data)


def encode_outbound(envelope: OutboundEnvelope) -> bytes:
    """1メッセージの bytes（UTF-8 の JSON）。"""
    return envelope.model_dump_json().encode("utf-8")
