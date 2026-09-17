"""Learned MPC から Fallback への即時退避と、hold 後の復帰。#79"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.config import AuthorityLimit, FanPolicyConfig
from coldaisle.control.schema import (
    AuthorityStage,
    ControllerKind,
    ControllerProposal,
    OperatingMode,
    OptimizerStatus,
    PerZone,
    Reason,
    SafetyState,
    Zone,
    ZoneRequest,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SnapshotStatus(StrEnum):
    """Gate が受け取った State Snapshot の状態。"""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class FallbackCause(StrEnum):
    """decision trace へ残す ML 退避理由。"""

    MODEL_LOAD_FAILURE = "model_load_failure"
    LEARNED_PROPOSAL_UNAVAILABLE = "learned_proposal_unavailable"
    OPTIMIZER_TIMEOUT = "optimizer_timeout"
    OPTIMIZER_ERROR = "optimizer_error"
    LOW_CONFIDENCE = "low_confidence"
    OOD = "ood"
    SUPERVISOR_FAILURE = "supervisor_failure"
    MODEL_VERSION_MISMATCH = "model_version_mismatch"
    CONTROL_DEADLINE_EXCEEDED = "control_deadline_exceeded"
    SNAPSHOT_UNAVAILABLE = "state_snapshot_unavailable"
    SNAPSHOT_INVALID = "state_snapshot_invalid"
    PROPOSAL_EXPIRED = "learned_proposal_expired"
    SAFETY_NOT_NORMAL = "safety_not_normal"
    RECOVERY_HOLD = "ml_recovery_hold"


class LearnedControlStatus(_Frozen):
    """worker 提案と、ループ側だけが知る受信・失敗状態。"""

    proposal: ControllerProposal | None = None
    received_at_mono_ms: int | None = Field(default=None, ge=0)
    model_loaded: bool = True
    supervisor_available: bool = True
    control_deadline_exceeded: bool = False
    snapshot_status: SnapshotStatus = SnapshotStatus.AVAILABLE

    @model_validator(mode="after")
    def _proposal_and_receipt_match(self) -> Self:
        if (self.proposal is None) != (self.received_at_mono_ms is None):
            raise ValueError("Learned proposal と受信単調時刻は一緒に指定する")
        if self.proposal is not None and self.proposal.controller is not ControllerKind.LEARNED_MPC:
            raise ValueError("LearnedControlStatus には Learned MPC の提案だけを入れる")
        if not self.model_loaded and self.proposal is not None:
            raise ValueError("モデル読込失敗時に Learned proposal は指定できない")
        return self


class ControllerSelection(_Frozen):
    """Gate がこの tick で選んだ requested と trace 用状態。"""

    proposal: ControllerProposal
    fallback_reason: Reason | None = None
    transitioned: bool
    recovery_healthy_since_mono_ms: int | None = Field(default=None, ge=0)

    @property
    def active_controller(self) -> ControllerKind:
        """この tick の active controller。"""
        return self.proposal.controller

    @property
    def fallback_active(self) -> bool:
        """Fallback が requested を作ったか。"""
        return self.active_controller is ControllerKind.FALLBACK

    def trace_metadata(self) -> dict[str, object]:
        """#82 の ControlState / structured log にそのまま載せられる値を返す。"""
        return {
            "active_controller": self.active_controller.value,
            "fallback_active": self.fallback_active,
            "fallback_reason": (
                None
                if self.fallback_reason is None
                else self.fallback_reason.model_dump(mode="json")
            ),
        }


class ControllerGate:
    """ML の不健全を即時に退避し、連続健全 hold 後だけ復帰させる。

    Confidence / OOD 自体の算出は #85 の責務。この Gate は提案に付いた判定と
    worker / loop の状態を消費し、Fallback への切替だけを決める。
    """

    def __init__(self, policy: FanPolicyConfig, *, expected_model_version: str) -> None:
        if not expected_model_version:
            raise ValueError("expected_model_version は空にできない")
        self._policy = policy
        self._expected_model_version = expected_model_version
        self._active_controller: ControllerKind | None = None
        self._last_requested: PerZone[ZoneRequest] | None = None
        self._healthy_since_mono_ms: int | None = None
        self._last_mono_ms: int | None = None

    def select(
        self,
        *,
        now_mono_ms: int,
        fallback: ControllerProposal,
        learned: LearnedControlStatus,
        operating_mode: OperatingMode,
        safety_state: SafetyState,
    ) -> ControllerSelection:
        """この tick の制御器を選ぶ。安全でない側への復帰だけ hold する。"""
        self._check_inputs(now_mono_ms, fallback, operating_mode)
        previous_controller = self._active_controller

        if operating_mode is OperatingMode.MAX:
            self._healthy_since_mono_ms = None
            return self._remember(fallback, None, previous_controller)

        if self._policy.authority_stage is AuthorityStage.SHADOW:
            self._healthy_since_mono_ms = None
            return self._remember(fallback, None, previous_controller)

        reason = self._unhealthy_reason(now_mono_ms, learned, safety_state)
        if reason is not None:
            self._healthy_since_mono_ms = None
            selected = fallback
            if previous_controller is ControllerKind.LEARNED_MPC:
                selected = self._prevent_transition_drop(fallback)
            return self._remember(selected, reason, previous_controller)

        assert learned.proposal is not None
        if previous_controller is not ControllerKind.LEARNED_MPC:
            if self._healthy_since_mono_ms is None:
                self._healthy_since_mono_ms = now_mono_ms
            elapsed_ms = now_mono_ms - self._healthy_since_mono_ms
            if elapsed_ms < self._policy.recovery_hold_ms:
                reason = Reason(
                    code=FallbackCause.RECOVERY_HOLD.value,
                    detail=(
                        f"healthy_for_ms={elapsed_ms}; required_ms={self._policy.recovery_hold_ms}"
                    ),
                )
                return self._remember(fallback, reason, previous_controller)

        selected = self._apply_authority(learned.proposal, fallback)
        self._healthy_since_mono_ms = None
        return self._remember(selected, None, previous_controller)

    def _unhealthy_reason(
        self,
        now_mono_ms: int,
        learned: LearnedControlStatus,
        safety_state: SafetyState,
    ) -> Reason | None:
        if learned.snapshot_status is SnapshotStatus.UNAVAILABLE:
            return self._reason(FallbackCause.SNAPSHOT_UNAVAILABLE)
        if learned.snapshot_status is SnapshotStatus.INVALID:
            return self._reason(FallbackCause.SNAPSHOT_INVALID)
        if learned.control_deadline_exceeded:
            return self._reason(FallbackCause.CONTROL_DEADLINE_EXCEEDED)
        if safety_state is not SafetyState.NORMAL:
            return self._reason(FallbackCause.SAFETY_NOT_NORMAL, f"state={safety_state.value}")
        if not learned.supervisor_available:
            return self._reason(FallbackCause.SUPERVISOR_FAILURE)
        if not learned.model_loaded:
            return self._reason(FallbackCause.MODEL_LOAD_FAILURE)
        proposal = learned.proposal
        if proposal is None:
            return self._reason(FallbackCause.LEARNED_PROPOSAL_UNAVAILABLE)

        assert learned.received_at_mono_ms is not None
        age_ms = now_mono_ms - learned.received_at_mono_ms
        if age_ms < 0 or age_ms > self._policy.mpc.valid_ms:
            return self._reason(
                FallbackCause.PROPOSAL_EXPIRED,
                f"age_ms={age_ms}; valid_ms={self._policy.mpc.valid_ms}",
            )
        if proposal.optimizer_status is OptimizerStatus.TIMEOUT:
            return self._reason(FallbackCause.OPTIMIZER_TIMEOUT)
        if proposal.optimizer_status is OptimizerStatus.ERROR:
            return self._reason(FallbackCause.OPTIMIZER_ERROR)
        if proposal.model_version != self._expected_model_version:
            return self._reason(
                FallbackCause.MODEL_VERSION_MISMATCH,
                f"expected={self._expected_model_version}; actual={proposal.model_version}",
            )
        if proposal.ood:
            return self._reason(FallbackCause.OOD)
        assert proposal.confidence is not None
        if proposal.confidence < self._policy.gate_min_confidence.value:
            return self._reason(
                FallbackCause.LOW_CONFIDENCE,
                (
                    f"confidence={proposal.confidence:.6f}; "
                    f"required={self._policy.gate_min_confidence.value:.6f}"
                ),
            )
        return None

    def _apply_authority(
        self,
        learned: ControllerProposal,
        fallback: ControllerProposal,
    ) -> ControllerProposal:
        stage = self._policy.authority_stage
        if stage is AuthorityStage.FULL:
            return learned
        limit = (
            self._policy.authority_limits.limited
            if stage is AuthorityStage.LIMITED
            else self._policy.authority_limits.expanded
        )
        requests = {zone: self._limited_request(zone, learned, fallback, limit) for zone in Zone}
        return learned.model_copy(
            update={
                "requested": PerZone(
                    front=requests[Zone.FRONT],
                    rear=requests[Zone.REAR],
                    top=requests[Zone.TOP],
                )
            }
        )

    @staticmethod
    def _limited_request(
        zone: Zone,
        learned: ControllerProposal,
        fallback: ControllerProposal,
        limit: AuthorityLimit,
    ) -> ZoneRequest:
        baseline = fallback.requested.get(zone).demand
        candidate = learned.requested.get(zone)
        if zone not in limit.permitted_zones:
            return ZoneRequest(
                demand=baseline,
                reason=Reason(
                    code="authority_zone_fallback",
                    detail=f"zone={zone.value}; stage does not permit Learned MPC",
                ),
            )
        lower = max(0.0, baseline - limit.limit_down)
        upper = min(1.0, baseline + limit.limit_up)
        bounded = min(max(candidate.demand, lower), upper)
        if bounded == candidate.demand:
            return candidate
        return ZoneRequest(
            demand=bounded,
            reason=Reason(
                code="authority_bounded",
                detail=(
                    f"zone={zone.value}; candidate={candidate.demand:.6f}; "
                    f"lower={lower:.6f}; upper={upper:.6f}"
                ),
            ),
        )

    def _prevent_transition_drop(self, fallback: ControllerProposal) -> ControllerProposal:
        previous = self._last_requested
        if previous is None:
            return fallback
        requests: dict[Zone, ZoneRequest] = {}
        for zone in Zone:
            baseline = fallback.requested.get(zone)
            previous_demand = previous.get(zone).demand
            if baseline.demand >= previous_demand:
                requests[zone] = baseline
                continue
            requests[zone] = ZoneRequest(
                demand=previous_demand,
                reason=Reason(
                    code="fallback_transition_floor",
                    detail=(
                        f"zone={zone.value}; baseline={baseline.demand:.6f}; "
                        f"previous={previous_demand:.6f}; baseline_reason={baseline.reason.code}"
                    ),
                ),
            )
        return fallback.model_copy(
            update={
                "requested": PerZone(
                    front=requests[Zone.FRONT],
                    rear=requests[Zone.REAR],
                    top=requests[Zone.TOP],
                )
            }
        )

    def _remember(
        self,
        proposal: ControllerProposal,
        fallback_reason: Reason | None,
        previous_controller: ControllerKind | None,
    ) -> ControllerSelection:
        self._active_controller = proposal.controller
        self._last_requested = proposal.requested
        return ControllerSelection(
            proposal=proposal,
            fallback_reason=fallback_reason,
            transitioned=(
                previous_controller is not None and previous_controller is not proposal.controller
            ),
            recovery_healthy_since_mono_ms=self._healthy_since_mono_ms,
        )

    def _check_inputs(
        self,
        now_mono_ms: int,
        fallback: ControllerProposal,
        operating_mode: OperatingMode,
    ) -> None:
        if now_mono_ms < 0:
            raise ValueError("Controller Gate の単調時計は負にできない")
        if self._last_mono_ms is not None and now_mono_ms < self._last_mono_ms:
            raise ValueError("Controller Gate の単調時計は巻き戻せない")
        self._last_mono_ms = now_mono_ms
        if fallback.controller is not ControllerKind.FALLBACK:
            raise ValueError("fallback 引数には Fallback proposal を渡す")
        if operating_mode in {OperatingMode.MANUAL, OperatingMode.CALIBRATION}:
            raise ValueError("MANUAL / CALIBRATION の requested は Controller Gate が選ばない")

    @staticmethod
    def _reason(cause: FallbackCause, detail: str = "") -> Reason:
        return Reason(code=cause.value, detail=detail)
