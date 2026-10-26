"""Learned MPC worker の側の Workload Hint の B1 の受け渡し（#288 / 決定記録 0133 §2.5・0162 §2.3 /
§2.5 の PR-B）。

基準のプロセスの周期のループが持つのは、基準の solve と送信・frame の受信・Shadow solver
との受け渡しだけ（0133 §2.5）。この module はその「受け渡し」で、モデルを評価しない（解くのは
子）。

- **基準を先に送る。** :meth:`HintShadowCoordinator.after_base` は基準の ``MpcProposal`` を送り、
  次の周期の開始 ``next_due`` を数えた**後に**呼ぶ。``next_due`` はこの module の進み具合を見ない
- 依頼は1枠。子が前の依頼の途中なら依頼を捨てて ``shadow_busy`` の記録にする（待ち行列を作らない）
- 子が使えない（起動中・作り直しの待ち・``SCHED_IDLE`` に下げられない・送信バッファを足せない）
  ときは依頼を渡さず、``shadow_unavailable`` の数にだけ数える（記録を作らない。0162 §2.3）
- 親が持てる未転送の記録は1件だけ。新しい記録が来たら古い記録を捨てる。転送できないまま、元の
  frame の次の基準の周期の開始を過ぎたら捨てる（どちらも ``shadow_dropped_backpressure``）。
  ``limits.max_shadow_record_bytes`` を超えた記録は切り詰めずに捨てる
  （``shadow_record_too_large``）
- 累積の数は worker のプロセスの中で持ち、``worker_instance_id``（プロセスの起動ごとの乱数の識別
  子）と組で、送れた Shadow の記録に毎回載せる（0162 §2.3）。heartbeat には載せない
- 基準の結果に解が無い（失敗・``ok`` でない）周期は、対にする基準の plan
  が無いので依頼も記録も作らない

**失敗の影響は片方向。** ここのどの失敗も、基準の ``MpcProposal`` の内容・送信時刻・
扱いを変えない（0133 §2.5）。記録は値の写しで、Demand の上書き・モード・authority
を表さない（0162 §2.1・ルール 2）。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from coldaisle import logs
from coldaisle.control.config import ControlConfig
from coldaisle.control.hint_domain import DomainFrame, DomainSignal, domain_signal
from coldaisle.control.hint_gate import HintGateSpecError, derive_hint_gate_spec
from coldaisle.control.learned_handoff import LearnedFrame, LearnedRole, PinnedArtifact
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.schema import ControlConfigDigest, Demand, PerZone, ZoneRequest
from coldaisle.hint_certificate import HintGateSpec
from coldaisle.learned_channel.messages import (
    ENVELOPE_SCHEMA_VERSION,
    HINT_SHADOW_RECORD_SCHEMA_VERSION,
    HintShadowBody,
    HintShadowCounters,
    HintShadowPlans,
    HintShadowSkipReason,
    HintShadowStatus,
    InboundEnvelope,
)
from coldaisle.learned_worker.client import ShadowSendOutcome
from coldaisle.learned_worker.mpc import StepInput
from coldaisle.learned_worker.shadow_solver import (
    ShadowOutcome,
    ShadowRequest,
    ShadowSolverPort,
    SolverState,
)

LOGGER = logging.getLogger("coldaisle.learned_worker.shadow")


def new_worker_instance_id() -> str:
    """worker のプロセスの起動ごとの識別子（``run_id`` と同じ形の乱数。個体識別子を含まない）。"""
    return secrets.token_hex(16)


class ShadowSender(Protocol):
    """Shadow の記録の送信の入口（``WorkerChannel.try_send_shadow``）。"""

    def try_send_shadow(self, data: bytes) -> ShadowSendOutcome:
        """1回だけ送ってみる。待たない。"""


@dataclass(frozen=True, slots=True)
class _RecordContext:
    """記録の識別（依頼を作った時点で決まる値）。"""

    run_id: str
    source_tick_id: int
    source_ts_ms: int
    source_monotonic_ms: int
    base_result_digest: str
    thermal_model: PinnedArtifact
    config: ControlConfigDigest
    certificate_sha256: str
    expires_at_ms: int
    """元の frame の次の基準の周期の開始（これを過ぎたら転送せずに捨てる。0133 §2.5）。"""


@dataclass(frozen=True, slots=True)
class _PendingRecord:
    context: _RecordContext
    status: HintShadowStatus
    reason: HintShadowSkipReason | None
    plans: HintShadowPlans | None
    conditions_sha256: str | None


@dataclass(slots=True)
class _Counters:
    shadow_dropped_backpressure: int = 0
    shadow_record_too_large: int = 0
    shadow_unavailable: int = 0

    def snapshot(self) -> HintShadowCounters:
        return HintShadowCounters(
            shadow_dropped_backpressure=self.shadow_dropped_backpressure,
            shadow_record_too_large=self.shadow_record_too_large,
            shadow_unavailable=self.shadow_unavailable,
        )


class HintShadowCoordinator:
    """周期のループのスレッド1本からだけ呼ぶ（基準の送信と同じスレッド。0133 §2.5）。"""

    def __init__(
        self,
        *,
        solver: ShadowSolverPort | None,
        sender: ShadowSender,
        control: ControlConfig,
        max_record_bytes: int,
        parent_isolated: bool,
        monotonic_ms: Callable[[], int],
        worker_instance_id: str | None = None,
    ) -> None:
        """``solver`` が None なら子を使えない（送信バッファを足せなかったなど。0133 §2.5）。"""
        self._solver = solver
        self._sender = sender
        self._max_record_bytes = max_record_bytes
        self._parent_isolated = parent_isolated
        self._monotonic_ms = monotonic_ms
        self.worker_instance_id = worker_instance_id or new_worker_instance_id()
        policy = control.policy
        self._budget_ms = policy.mpc.budget_ms
        self._max_evaluations = policy.mpc.optimizer.max_evaluations.value
        cost = policy.mpc.optimizer.cost_metrics
        self._cpu_metric = cost.cpu_temperature
        self._gpu_metric = cost.gpu_temperature
        try:
            spec: HintGateSpec | None = derive_hint_gate_spec(
                policy.supervisor.workload_hint, control.safety.workload_hint
            )
        except HintGateSpecError:
            # 子も同じ設定から導けず certification_mismatch を返す。軸の値は取り出さない
            spec = None
        self._feature_metrics = (
            ()
            if spec is None
            else tuple(feature.metric for feature in spec.certified_domain.window_features)
        )
        self._counters = _Counters()
        self._seq = 0
        self._inflight: dict[int, _RecordContext] = {}
        self._outbox: _PendingRecord | None = None

    @property
    def counters(self) -> HintShadowCounters:
        """いまの累積の数。"""
        return self._counters.snapshot()

    def readable_fds(self) -> list[int]:
        """読み待ちに加える fd（子の結果）。"""
        return [] if self._solver is None else self._solver.readable_fds()

    def writable_fds(self) -> list[int]:
        """書き待ちに加える fd（子への依頼の書き残し）。"""
        return [] if self._solver is None else self._solver.writable_fds()

    def close(self) -> None:
        """子を終わらせる。"""
        if self._solver is not None:
            self._solver.close()

    def after_base(
        self,
        *,
        run_id: str,
        step: StepInput,
        base: MpcProposal,
        base_started_ms: int,
        next_due_ms: int,
    ) -> None:
        """基準を送った後に、ヒント有りの依頼を1枠で渡す（**待たない**）。"""
        frame = step.frame
        hint = frame.supervisor_hint
        certification = frame.hint_certification
        pinned = frame.expected_artifacts.thermal_model
        solution = base.solution
        if hint is None or certification is None or pinned is None:
            return
        if solution is None or base.proposal is None or frame.supervisor is None:
            return
        snapshot = frame.snapshot
        context = _RecordContext(
            run_id=run_id,
            source_tick_id=snapshot.tick_id,
            source_ts_ms=snapshot.ts_ms,
            source_monotonic_ms=snapshot.monotonic_ms,
            base_result_digest=base.result_digest(),
            thermal_model=pinned,
            config=frame.config,
            certificate_sha256=certification.certificate_sha256,
            expires_at_ms=next_due_ms,
        )
        if not self._parent_isolated:
            # 0157 §2.3: other の worker は子を作らずに certification_mismatch を返す
            self._enqueue(context, HintShadowSkipReason.CERTIFICATION_MISMATCH)
            return
        solver = self._solver
        state = SolverState.DISABLED if solver is None else solver.state
        if state is SolverState.BUSY:
            self._enqueue(context, HintShadowSkipReason.SHADOW_BUSY)
            return
        if solver is None or state is not SolverState.IDLE:
            if solver is not None:
                # B1 の依頼が来て初めて子を作る（B0 の間は作らない）
                solver.start(self._monotonic_ms())
            self._counters.shadow_unavailable += 1
            return
        remaining = self._max_evaluations - solution.evaluations
        deadline = min(base_started_ms + self._budget_ms, next_due_ms)
        if remaining < 1 or deadline <= self._monotonic_ms():
            self._enqueue(context, HintShadowSkipReason.BUDGET_EXHAUSTED)
            return
        self._seq += 1
        request = ShadowRequest(
            seq=self._seq,
            observed=step.observed,
            domain_frames=tuple(self._domain_frame(item) for item in step.frames),
            previous_applied=_previous_applied(step.frames),
            safety_floor=frame.safety_floor,
            baseline=None if frame.baseline is None else _demands(frame.baseline.requested),
            base_supervisor=frame.supervisor,
            hint_supervisor=hint,
            base_requested=solution.requested,
            base_targets=solution.prediction.targets,
            base_anchor_inference_id=solution.anchor_inference_id,
            remaining_evaluations=remaining,
            deadline_mono_ms=deadline,
            certification=certification,
            calibration=frame.calibration,
            thermal_model=pinned,
            authority_stage=frame.authority_stage,
            config=frame.config,
            metric_catalog_sha256=frame.metric_catalog_sha256,
        )
        self._inflight[request.seq] = context
        solver.submit(request)

    def service(self) -> None:
        """子の結果を読み、記録を期限つきで転送する（**待たない**）。frame を待つ間に呼ぶ。"""
        now_ms = self._monotonic_ms()
        if self._solver is not None:
            events = self._solver.service(now_ms)
            for seq in events.lost:
                # 子の死・応答しない。結果の無い試みは記録を作らず数にだけ数える（0162 §2.3）
                if self._inflight.pop(seq, None) is not None:
                    self._counters.shadow_unavailable += 1
            for outcome in events.results:
                self._accept(outcome)
        self._forward(now_ms)

    # ------------------------------------------------------------------ 内部

    def _accept(self, outcome: ShadowOutcome) -> None:
        context = self._inflight.pop(outcome.seq, None)
        if context is None:
            LOGGER.warning(
                "依頼の識別の無い Shadow solver の結果を捨てた",
                extra={logs.FIELDS_KEY: {"seq": outcome.seq}},
            )
            return
        self._put(
            _PendingRecord(
                context=context,
                status=outcome.status,
                reason=outcome.reason,
                plans=outcome.plans,
                conditions_sha256=outcome.conditions_sha256,
            )
        )

    def _enqueue(self, context: _RecordContext, reason: HintShadowSkipReason) -> None:
        self._put(
            _PendingRecord(
                context=context,
                status=HintShadowStatus.SKIPPED,
                reason=reason,
                plans=None,
                conditions_sha256=None,
            )
        )
        self._forward(self._monotonic_ms())

    def _put(self, record: _PendingRecord) -> None:
        if self._outbox is not None:
            # 未転送の記録は1件だけ。新しいものが来たら古いものを捨てる（0133 §2.5）
            self._counters.shadow_dropped_backpressure += 1
        self._outbox = record

    def _forward(self, now_ms: int) -> None:
        record = self._outbox
        if record is None:
            return
        if now_ms >= record.context.expires_at_ms:
            self._outbox = None
            self._counters.shadow_dropped_backpressure += 1
            return
        data = self._encode(record)
        if len(data) > self._max_record_bytes:
            # 切り詰めずに捨てる（0133 §2.5）
            self._outbox = None
            self._counters.shadow_record_too_large += 1
            return
        outcome = self._sender.try_send_shadow(data)
        if outcome is ShadowSendOutcome.SENT:
            self._outbox = None
        elif outcome is ShadowSendOutcome.FAILED:
            self._outbox = None
            self._counters.shadow_dropped_backpressure += 1
        # それ以外（lock・送信待ち・EAGAIN）は次の待ちの間に再び試す

    def _encode(self, record: _PendingRecord) -> bytes:
        context = record.context
        body = HintShadowBody(
            kind="hint_shadow",
            record_schema_version=HINT_SHADOW_RECORD_SCHEMA_VERSION,
            worker_instance_id=self.worker_instance_id,
            counters=self._counters.snapshot(),
            source_tick_id=context.source_tick_id,
            source_ts_ms=context.source_ts_ms,
            source_monotonic_ms=context.source_monotonic_ms,
            base_result_digest=context.base_result_digest,
            thermal_model=context.thermal_model,
            config=context.config,
            certificate_sha256=context.certificate_sha256,
            conditions_sha256=record.conditions_sha256,
            status=record.status,
            reason=record.reason,
            plans=record.plans,
        )
        envelope = InboundEnvelope(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            run_id=context.run_id,
            role=LearnedRole.MPC,
            body=body,
        )
        return envelope.model_dump_json().encode("utf-8")

    def _domain_frame(self, frame: LearnedFrame) -> DomainFrame:
        """判定に要る軸の値だけを取り出す（子が worker の判定に使う。0162 §2.2）。"""
        signals = frame.snapshot.signals_by_metric
        features: dict[str, DomainSignal | None] = {
            metric: domain_signal(signals.get(metric)) for metric in self._feature_metrics
        }
        return DomainFrame(
            tick_id=frame.snapshot.tick_id,
            ts_ms=frame.snapshot.ts_ms,
            cpu_temperature=domain_signal(signals.get(self._cpu_metric)),
            gpu_temperature=domain_signal(signals.get(self._gpu_metric)),
            features=features,
        )


def _previous_applied(frames: Sequence[LearnedFrame]) -> PerZone[Demand | None] | None:
    """``N - 1`` の frame の ``applied``。

    tick が続いていなければ None（推測で埋めない。0107 §2.2）。
    """
    if len(frames) < 2:
        return None
    anchor, prior = frames[-1], frames[-2]
    if prior.snapshot.tick_id != anchor.snapshot.tick_id - 1:
        return None
    return prior.applied


def _demands(requested: PerZone[ZoneRequest]) -> PerZone[Demand]:
    return PerZone[Demand](
        front=requested.front.demand, rear=requested.rear.demand, top=requested.top.demand
    )
