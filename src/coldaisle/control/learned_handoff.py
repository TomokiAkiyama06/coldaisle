"""Learned worker との受け渡しで loop が知る型（決定記録 0077 §2.2 / §2.3 / §2.5、0092、0101）。

`coldaisle.control` は**受け渡しの Protocol と frame の形だけ**を知る。ソケット・受付スレッド・
認可の実装は `coldaisle.learned_channel`（合成の起点）にあり、この package はそれを import しない
（下位層が上位を import しない。0077 §2.2）。

- `LearnedFrame`: loop が毎 tick、**その tick の同じ snapshot**から作って worker へ送る入力
  （0077 §2.3）。worker は Telemetry を読まず、この frame の列だけを入力にする
- `LearnedFrameSink`: frame を送り出し用の1枠へ置く口。**待たない**
- `LearnedChannelHealth`: 経路の状態を **lock を取らずに**読む口。`poll()` の形を変えずに
  「なぜ提案が無いのか」を loop へ渡す（0077 §2.5 の「trace の detail の運び方」）

worker からの経路で運べるのは `MpcProposal`（と段階 4 の Supervisor 出力）の型だけで、Demand の
上書き・モード・authority を表す型を持たない（0077 §2.4 の 6、AGENTS.md ルール2）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.schema import (
    AuthorityStage,
    ControlConfigDigest,
    ControllerProposal,
    Demand,
    PerZone,
    RegistryProvenance,
    SupervisorOutput,
)
from coldaisle.control.state import ControlStateSnapshot
from coldaisle.control.supervisor.regime import WorkloadRegimeEstimate

LEARNED_FRAME_SCHEMA_VERSION: Literal[3] = 3
"""`LearnedFrame` の版。

- v1（#86 / 0077 段階 1）: `expected_artifacts` は持たない
- v2（#104 / 0077 段階 2）: `expected_artifacts`（起動時に読んだ registry の production の識別）
  を足した
- v3（#86 / 0077 段階 3）: `calibration`（fand が起動時に読んだ較正。決定記録 0101 §2.2）と
  `metric_catalog_sha256`（fand が読んだ Metric Catalog の SHA-256。決定記録 0107 §2.6）を足した。
  **v2 以前は受け取らない**（0101 §2.3。どの欄も信用できない frame から較正だけを拾わない）
"""

THERMAL_MODEL_KIND = "thermal_model"
"""MPC worker が読む artifact の kind（Model Registry の `ArtifactKind` の値。0077 §2.6）。"""

SUPERVISOR_POLICY_KIND = "supervisor_policy"
"""RL Supervisor worker が読む artifact の kind（0077 §2.6）。"""


class LearnedRole(StrEnum):
    """worker の役割（0077 §2.1 / §2.7）。役割はソケットで決め、worker の名乗りでは決めない。"""

    MPC = "mpc"
    SUPERVISOR = "supervisor"


class LearnedChannelState(StrEnum):
    """経路の状態（0077 §2.2 の閉じた列挙）。trace の `Reason.detail` にこの文字列だけを入れる。"""

    CONNECTED = "connected"
    WORKER_DISCONNECTED = "worker_disconnected"
    """worker が接続していない（まだ来ていない・EOF で切れた）。"""
    WORKER_IDLE = "worker_idle"
    """接続したまま `worker_idle_timeout_ms` 黙ったので受付が閉じた。"""
    CHANNEL_DEAD = "channel_dead"
    """受付スレッドが死んだ。`coldaisle-fand` の再起動まで Learned を読まない。"""
    CHANNEL_DISABLED = "channel_disabled"
    """設定が不正・ソケットを開けないなどで、経路を開かずに起動した。"""
    REGISTRY_SUPERSEDED = "registry_superseded"
    """固定した artifact が production でなくなった・registry が読めなくなったので、
    `coldaisle-fand` の再起動までその役割を閉じた（0077 §2.6）。"""


class PinnedArtifact(BaseModel):
    """worker が読む artifact の固定（0077 §2.3 / §2.6）。registry の production の3つ組。

    path を持たない（AGENTS.md ルール10）。worker はこの3つ組で registry から artifact を引き、
    bytes の checksum と schema を自分で検証する（`coldaisle-fand` は bytes を読まない）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    model_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=120)
    version: str = Field(min_length=1, max_length=80)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LearnedExpectedArtifacts(BaseModel):
    """起動時に読んだ registry の production の識別（frame の `expected_artifacts`。0077 §2.6）。

    役割ごとに1つ。production が無い・registry を読んでいない・読めなかったときは None で、
    worker はその役割の結果を作らない。**`RegistryProvenance` からだけ作る**（`from_provenance`）。
    trace の `registry`・Gate の期待値・`expected_rl_identity` と同じ snapshot から来ることを、
    出どころを1つにして保つためである。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    thermal_model: PinnedArtifact | None
    """MPC worker が読む `thermal_model` の production。"""
    supervisor_policy: PinnedArtifact | None
    """RL Supervisor worker が読む `supervisor_policy` の production。"""

    @classmethod
    def from_provenance(cls, provenance: RegistryProvenance) -> LearnedExpectedArtifacts:
        """trace に載せる registry の版から作る（registry を読んでいなければ両方 None）。"""
        return cls(
            thermal_model=_pinned(provenance, THERMAL_MODEL_KIND),
            supervisor_policy=_pinned(provenance, SUPERVISOR_POLICY_KIND),
        )

    def for_role(self, role: LearnedRole) -> PinnedArtifact | None:
        """役割の固定（`mpc` → `thermal_model`、`supervisor` → `supervisor_policy`）。"""
        return self.thermal_model if role is LearnedRole.MPC else self.supervisor_policy


def _pinned(provenance: RegistryProvenance, kind: str) -> PinnedArtifact | None:
    pointer = provenance.production.get(kind)
    if pointer is None or pointer.artifact_sha256 is None or pointer.established_by is None:
        return None
    change = pointer.established_by
    return PinnedArtifact(
        model_id=change.model_id,
        version=change.model_version,
        artifact_sha256=pointer.artifact_sha256,
    )


class CalibrationUnavailableCode(StrEnum):
    """frame の `unavailable` の理由（決定記録 0101 §2.2 / §5 #1）。**この2値だけ**。

    例外の文字列（path を含みうる）は frame に載せず、fand の構造化ログにだけ残す
    （AGENTS.md ルール10）。
    """

    PATH_NOT_GIVEN = "calibration_path_not_given"
    UNREADABLE = "calibration_unreadable"


class _FrozenStrict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class AvailableCalibration(_FrozenStrict):
    """読めた較正（チャネル名 → ℃。有限値だけ）。空の `offsets_c` も正当な較正（0096 §2.9）。"""

    status: Literal["available"] = "available"
    offsets_c: dict[
        Annotated[str, Field(min_length=1)], Annotated[float, Field(allow_inf_nan=False)]
    ]


class UnavailableCalibration(_FrozenStrict):
    """読めなかった較正。理由は閉じた code だけ（決定記録 0101 §2.2）。"""

    status: Literal["unavailable"] = "unavailable"
    reason: CalibrationUnavailableCode


LearnedCalibration = Annotated[
    AvailableCalibration | UnavailableCalibration, Field(discriminator="status")
]
"""frame の `calibration`（`RuntimeCalibration` の2状態をそのまま写す判別共用体。0101 §2.2）。"""


def calibration_to_runtime(
    calibration: AvailableCalibration | UnavailableCalibration,
) -> RuntimeCalibration:
    """frame の較正を loader（L9）に渡す `RuntimeCalibration` へ戻す（決定記録 0101 §2.3）。

    `unavailable` は code から `RuntimeCalibration.unavailable(<code>)` を作る。欠けた較正を
    `available({})`・0.0 で埋めない（0096 §2.6）。
    """
    if isinstance(calibration, AvailableCalibration):
        return RuntimeCalibration.available(calibration.offsets_c)
    return RuntimeCalibration.unavailable(calibration.reason.value)


class LearnedFrame(BaseModel):
    """1 tick の worker の入力（0077 §2.3、`applied` は 0092）。

    `run_id` はこの frame を包む封筒（`coldaisle.learned_channel`）が運ぶ。frame には
    Telemetry の値と制御の状態だけを入れ、path・個体識別子を入れない（AGENTS.md ルール10）。
    LLM のプロンプトへは渡さない（ルール8）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[3] = LEARNED_FRAME_SCHEMA_VERSION
    snapshot: ControlStateSnapshot
    """その tick の `ControlStateSnapshot`（層をまたいで同じ object。0060 §2.5）。"""
    workload: WorkloadRegimeEstimate | None
    """その tick の Workload Regime の推定。

    Supervisor を配線していない・推定に失敗した tick は None。
    """
    supervisor: SupervisorOutput | None
    """その tick に**選ばれた** Supervisor 出力。無ければ None。"""
    baseline: ControllerProposal | None
    """その tick の Fallback の提案。Fallback が値を出せなかった tick は None。"""
    safety_floor: PerZone[Demand]
    """その tick の Critical Safety の zone ごとの floor。"""
    applied: PerZone[Demand | None]
    """各 zone の `FanHardwareResult.applied_demand`（0092）。

    結果が無い・確かめられない zone は None（欠測）。effective demand などで埋めない。
    """
    authority_stage: AuthorityStage
    """その tick の実効 stage（`binding_authority_stage` の照合。0057 §2.2）。"""
    expected_artifacts: LearnedExpectedArtifacts
    """起動時に読んだ registry の production の識別（0077 §2.3 / §2.6。v2）。

    worker が読む artifact はこれに従い、自分で「いまの production」を追わない。毎 tick 同じ値。
    """
    config: ControlConfigDigest
    """`runtime.config` と同じ値。worker は自分の設定と食い違えば提案を作らない。"""
    calibration: LearnedCalibration
    """fand が起動時に読んだ較正（v3。決定記録 0101 §2.2）。**毎 tick 同じ値**。既定値を持たない。

    worker は束縛を作るたびにこれを `RuntimeCalibration` に戻して loader（L9）に渡す。
    """
    metric_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    """fand が読んだ Metric Catalog の bytes の SHA-256（v3。決定記録 0107 §2.6）。

    worker は自分が読んだ catalog の SHA-256 と違えば束縛を作らない（`metric_catalog_mismatch`）。
    path は載せない（AGENTS.md ルール10）。
    """


class LearnedFrameSink(Protocol):
    """frame を送り出し用の1枠へ置く（0077 §2.2）。"""

    def offer(self, frame: LearnedFrame) -> None:
        """**待たない。** 送れない frame は捨てる。失敗は例外で返してよい（loop が捕まえる）。"""


class LearnedChannelHealth(Protocol):
    """経路の状態の読み出し（0077 §2.2）。"""

    def state(self, role: LearnedRole) -> LearnedChannelState:
        """**lock を取らずに**答える。受付スレッドが死んでいれば `CHANNEL_DEAD`。"""
