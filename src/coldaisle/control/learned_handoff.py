"""Learned worker との受け渡しで loop が知る型（決定記録 0077 §2.2 / §2.3 / §2.5、0092、0101）。

`coldaisle.control` は**受け渡しの Protocol と frame の形だけ**を知る。ソケット・受付スレッド・
認可の実装は `coldaisle.learned_channel`（合成の起点）にあり、この package はそれを import しない
（下位層が上位を import しない。0077 §2.2）。

- `LearnedFrame`: loop が毎 tick、**その tick の同じ snapshot**から作って worker へ送る入力
  （0077 §2.3）。worker は Telemetry を読まず、この frame の列だけを入力にする
- `LearnedFrameSink`: frame を送り出し用の1枠へ置く口。**待たない**
- `LearnedChannelHealth`: 経路の状態を **lock を取らずに**読む口。`poll()` の形を変えずに
  「なぜ提案が無いのか」を loop へ渡す（0077 §2.5 の「trace の detail の運び方」）
- `LearnedHintCertification`: frame v4 の `hint_certification`（0152 §2.6 / 0162 §2.2）。
  `supervisor_hint` と**両方そろうか両方 None**（片方だけの frame を型で作れない）

worker からの経路で運べるのは `MpcProposal`（と段階 4 の Supervisor 出力）の型だけで、Demand の
上書き・モード・authority を表す型を持たない（0077 §2.4 の 6、AGENTS.md ルール2）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.schema import (
    AuthorityStage,
    ControlConfigDigest,
    ControllerProposal,
    Demand,
    PerZone,
    RegistryProvenance,
    SupervisorOutput,
    SupervisorPolicyKind,
)
from coldaisle.control.state import ControlStateSnapshot
from coldaisle.control.supervisor.regime import WorkloadRegimeEstimate
from coldaisle.hint_certificate import HintCertificateComponents, Sha256

LEARNED_FRAME_SCHEMA_VERSION: Literal[4] = 4
"""`LearnedFrame` の版。

- v1（#86 / 0077 段階 1）: `expected_artifacts` は持たない
- v2（#104 / 0077 段階 2）: `expected_artifacts`（起動時に読んだ registry の production の識別）
  を足した
- v3（#86 / 0077 段階 3）: `calibration`（fand が起動時に読んだ較正。決定記録 0101 §2.2）と
  `metric_catalog_sha256`（fand が読んだ Metric Catalog の SHA-256。決定記録 0107 §2.6）を足した。
  **v2 以前は受け取らない**（0101 §2.3。どの欄も信用できない frame から較正だけを拾わない）
- v4（#288 / 決定記録 0133 §2.5・0152 §2.6・0162 §2.5 の PR-A）: `supervisor_hint`（ヒント有りの
  context による Supervisor 出力）と `hint_certification`（証明書の識別・worker 側の部品の期待値・
  fand の領域の判定の長さと古さの閾値）を足した。**両方そろうか両方 None。** fand と worker は
  同じ venv で入れ替わる（0157）ので、v3 / v4 の混在は扱わない（v3 以前は受け取らない）
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


class LearnedHintComponents(_FrozenStrict):
    """worker 側で照合する部品の期待値（0152 §2.6 の7つ。``hint_policy`` は fand だけが照合する）。

    fand が起動時に照合を通した証明書の ``components`` から写す（:meth:`from_certificate`）。
    照合が通った証明書の部品は fand が自分で計算した値と一致しているので、写すのは同じ値である。
    """

    source_tree: Sha256
    lockfile: Sha256
    environment: Sha256
    hint_gate: Sha256
    optimizer: Sha256
    conditions: Sha256
    calibration: Sha256

    @classmethod
    def from_certificate(cls, components: HintCertificateComponents) -> Self:
        """証明書の部品から worker 側の7つを写す。"""
        return cls(
            source_tree=components.source_tree,
            lockfile=components.lockfile,
            environment=components.environment,
            hint_gate=components.hint_gate,
            optimizer=components.optimizer,
            conditions=components.conditions,
            calibration=components.calibration,
        )


class LearnedHintCertification(_FrozenStrict):
    """frame v4 の ``hint_certification``（決定記録 0152 §2.6 / 0162 §2.2）。**毎 run 同じ値。**

    - ``certificate_sha256``: fand が起動時に照合を通した証明書の認証のハッシュ
    - ``components``: worker 側の部品の期待値（Shadow solver の子が自分の部品と比べる）
    - ``domain_lookback_ms`` / ``domain_stale_after_ms``: fand の認証領域の判定の区間の長さと
      古さの閾値（``config/learned-channel.yaml``。証明書には束ねない。0162 §2.2）。worker は
      artifact の ``window_ms`` が前者**以下**・``stale_after_ms`` が後者**以上**であることを
      確かめる

    path・個体識別子を持たない（AGENTS.md ルール10）。
    """

    certificate_sha256: Sha256
    components: LearnedHintComponents
    domain_lookback_ms: int = Field(ge=1)
    domain_stale_after_ms: int = Field(ge=1)


def _band_is_not_weaker(base: SupervisorOutput, hint: SupervisorOutput) -> bool:
    """ヒント有りの band が基準より冷却を弱めない（CPU / GPU の上限・下限とも基準以下）。

    設定の読み込み（0066 §2.1 (a)）が上書き先 context に課す条件と同じ向き。ここで重ねて確かめる。
    """
    for name in ("cpu_temperature", "gpu_temperature"):
        hinted = getattr(hint.target_band, name)
        based = getattr(base.target_band, name)
        if hinted.upper_c > based.upper_c or hinted.lower_c > based.lower_c:
            return False
    return True


def supervisor_hint_relation_holds(
    snapshot: ControlStateSnapshot,
    base: SupervisorOutput | None,
    hint: SupervisorOutput,
) -> bool:
    """``supervisor_hint`` と基準の ``supervisor`` の関係（決定記録 0133 §2.5）が成り立つか。

    **同じ tick**（基準・ヒント有りとも frame の ``snapshot`` の ``tick_id`` / ``ts_ms`` /
    schema の版）・**同じ regime**・**同じ policy の版**で、``target_band`` **だけが違う**こと。
    加えて、どちらも RulePolicy の出力であること（ヒントを効かせるのは RulePolicy だけ。0133 §2.2）
    と、ヒント有りの band が基準より冷却を弱めないこと（0066 §2.1 (a) の向き）を確かめる。
    fand は frame を作る前にこれを確かめ、成り立たなければ両方 None にする。frame の検証も同じ
    関数を呼ぶ。
    """
    if base is None:
        return False
    if base.policy is not SupervisorPolicyKind.RULE or hint.policy is not SupervisorPolicyKind.RULE:
        return False
    if (base.tick_id, base.ts_ms, base.snapshot_schema_version) != (
        snapshot.tick_id,
        snapshot.ts_ms,
        snapshot.schema_version,
    ):
        return False
    if hint.target_band == base.target_band:
        return False
    rest_base = {name: value for name, value in base if name != "target_band"}
    rest_hint = {name: value for name, value in hint if name != "target_band"}
    if rest_base != rest_hint:
        return False
    return _band_is_not_weaker(base, hint)


class LearnedFrame(BaseModel):
    """1 tick の worker の入力（0077 §2.3、`applied` は 0092）。

    `run_id` はこの frame を包む封筒（`coldaisle.learned_channel`）が運ぶ。frame には
    Telemetry の値と制御の状態だけを入れ、path・個体識別子を入れない（AGENTS.md ルール10）。
    LLM のプロンプトへは渡さない（ルール8）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[4] = LEARNED_FRAME_SCHEMA_VERSION
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
    supervisor_hint: SupervisorOutput | None
    """その tick のヒント有りの context による Supervisor 出力（v4。決定記録 0133 §2.5）。

    fand が入れるのは実効の段階が B1（``shadow_mpc``）以上で、ヒントが ``active``・
    対応表に鍵があり・ヒント有りの context が基準と違う tick だけ。
    **RL Supervisor worker への frame では常に None**（0133 §2.2。:meth:`without_hint`）。
    既定値を持たない。
    """
    hint_certification: LearnedHintCertification | None
    """証明書の識別と worker 側の部品の期待値（v4。決定記録 0152 §2.6 / 0162 §2.2）。

    ``supervisor_hint`` と**両方そろうか両方 None**。既定値を持たない。
    """

    @model_validator(mode="after")
    def _hint_fields_come_together_and_relate(self) -> Self:
        if (self.supervisor_hint is None) != (self.hint_certification is None):
            # 片方だけの frame を作らない（0152 §2.6）
            raise ValueError("supervisor_hint と hint_certification は両方入れるか両方 None にする")
        if self.supervisor_hint is not None and not supervisor_hint_relation_holds(
            self.snapshot, self.supervisor, self.supervisor_hint
        ):
            raise ValueError(
                "supervisor_hint は基準の supervisor と同じ tick・regime・policy の版で、"
                "target_band だけが違う出力にする（0133 §2.5）"
            )
        return self

    def without_hint(self) -> LearnedFrame:
        """ヒントの欄を伏せた frame（RL Supervisor worker へ送る形。0133 §2.2）。"""
        if self.supervisor_hint is None and self.hint_certification is None:
            return self
        return self.model_copy(update={"supervisor_hint": None, "hint_certification": None})


class LearnedFrameSink(Protocol):
    """frame を送り出し用の1枠へ置く（0077 §2.2）。"""

    def offer(self, frame: LearnedFrame) -> None:
        """**待たない。** 送れない frame は捨てる。失敗は例外で返してよい（loop が捕まえる）。"""


class LearnedChannelHealth(Protocol):
    """経路の状態の読み出し（0077 §2.2）。"""

    def state(self, role: LearnedRole) -> LearnedChannelState:
        """**lock を取らずに**答える。受付スレッドが死んでいれば `CHANNEL_DEAD`。"""
