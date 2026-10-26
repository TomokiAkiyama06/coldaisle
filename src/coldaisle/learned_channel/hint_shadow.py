"""fand の Workload Hint B1 の受信・検証・記録（決定記録 0162 §2.2 / §2.3 / §2.5 の PR-C）。

受付スレッド（``LearnedChannelServer``）から呼ばれる3つの部品を置く。**どれも tick の中では何も
しない**（0077 §2.2）。loop の状態に触れず、``requested_demand`` にも Gate にも Reactive Guard /
Critical Safety にも届かない（0162 §2.1。ルール 2・5）。

- :class:`HintDomainTracker`: MPC worker へ**実際に送り出した** frame の軸の値の環状の列と、
  ``supervisor_hint`` を埋めた frame を送り出した時点の認証領域の判定の結果を、記録が届くまで持つ
  **保留の表**（0162 §2.2 の案 c）。判定は ``coldaisle.control.hint_domain`` の同じ純粋な関数
- :class:`HintShadowIntake`: 届いた Shadow の記録を検証し（epoch ごとの数・新しさ・artifact・
  証明書・同じ元の tick の2件目・対の基準）、保留の表の結果と 2 条件の再検査を添えて
  ``hint_shadow_records`` の1行を作る。最後に受け入れた worker の累積の数を、loop が**待たずに**
  読めるように置く（0162 §2.3）
- :class:`HintShadowWriter`: 行を書く**tick の外の書き手**のスレッド（自分の接続。0133 §2.5a）。
  書けなくても制御は続ける（行を捨てて数える）

``record_json`` の形（本文の ``record_schema_version`` = 1）::

    {
      "base": {"requested_demand": {front, rear, top}, "prediction": [PlannedTarget...]},
      "hint": {"requested_demand": {...}, "prediction": [...]},
      "hint_band": SupervisorTargetBand,
      "recheck": {
        "domain": {"passed": bool,
                   "failed": <0162 §2.2 の 1〜5 の項目> | "source_interval_unavailable" | null},
        "monotonicity": {"passed": bool, "failed": "demand" | "cpu" | "gpu" | null}
      },
      "thermal_model": {model_id, version, artifact_sha256},
      "config": ControlConfigDigest,
      "certificate_sha256": str,
      "base_result_digest": str
    }

値の写しを再検査の結果に載せない（値は両 plan の欄にある。0162 §2.2）。``note``・生の ``source``・
``peer_uid`` は載せない（0064 §2.9）。LLM・読み取り API・AI ツールへは出さない（ルール 1 / 8）。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from coldaisle import logs
from coldaisle.clock import Clock
from coldaisle.control.config import SupervisorConfig
from coldaisle.control.hint_domain import (
    DomainCheck,
    DomainFrame,
    DomainJudgement,
    domain_frame,
    judge_certified_domain,
)
from coldaisle.control.hint_shadow import MonotonicityJudgement, recheck_monotonicity
from coldaisle.control.learned_handoff import LearnedFrame, PinnedArtifact
from coldaisle.control.mpc.controller import MpcProposal
from coldaisle.control.schema import (
    ControlConfigDigest,
    Demand,
    HintShadowCounters,
    PerZone,
    SupervisorOutput,
    SupervisorTargetBand,
    WorkloadHintKind,
    WorkloadRegime,
)
from coldaisle.hint_certificate import HintGateSpec
from coldaisle.hint_shadow_record import (
    HintShadowReason,
    HintShadowRecord,
    HintShadowStatus,
)
from coldaisle.learned_channel.messages import HintShadowBody
from coldaisle.learned_channel.messages import HintShadowStatus as WorkerShadowStatus

LOGGER = logging.getLogger("coldaisle.learned_channel")

SOURCE_INTERVAL_UNAVAILABLE = "source_interval_unavailable"
"""保留の表に元の tick の結果が無い（期限切れ・上限で捨てた・送り出していない・鍵が合わない）とき、
``record_json`` の領域の判定に書く最初の外れの項目（0162 §2.2）。"""

SourceKey = tuple[int, int, int]
"""元の frame の (``tick_id``, ``snapshot.ts_ms``, ``monotonic_ms``)。保留の表の鍵（0162 §2.2）。"""


# ---------------------------------------------------------------- 環状の列と保留の表


@dataclass(frozen=True, slots=True)
class HintAxes:
    """``supervisor_hint`` を埋めた frame ``N`` の、判定 1〜3 と基準の ``upper_c`` の入力。"""

    key: tuple[str, str] | None
    """``N`` の (Regime, ヒントの workload)。設定の対応表から引けなければ None（判定 1 で外）。"""
    safety_floor: PerZone[Demand]
    baseline: PerZone[Demand] | None
    cpu_upper_c: float
    gpu_upper_c: float
    """``N`` の**基準の** context の ``upper_c``（0156 §2.4。区間の全 frame をこれで測る）。"""


@dataclass(frozen=True, slots=True)
class SentFrameAxes:
    """送り出した frame の判定に要る値だけ（0162 §2.2「持つもの」）。frame そのものは持たない。"""

    domain: DomainFrame
    monotonic_ms: int
    applied: PerZone[Demand | None]
    hint: HintAxes | None
    """``supervisor_hint`` を埋めた frame だけ。それ以外は None（判定しない）。"""

    @property
    def key(self) -> SourceKey:
        """保留の表の鍵。"""
        return (self.domain.tick_id, self.domain.ts_ms, self.monotonic_ms)


@dataclass(frozen=True, slots=True)
class _Pending:
    judgement: DomainJudgement
    sent_mono_ms: int


def hint_keys_by_band(
    supervisor: SupervisorConfig,
) -> Mapping[tuple[WorkloadRegime, SupervisorTargetBand], tuple[WorkloadHintKind, ...]]:
    """(Regime, ヒント有りの band) → その band を選ぶ ``context_overrides`` の workload の列。

    frame はヒントの ``workload`` を運ばない（0133 §2.5 の形）ので、fand は自分の設定の対応表
    から ``supervisor_hint`` の band を選びうる鍵を引き直す。ここに出る鍵はどれも
    ``context_overrides`` の鍵で、ゲートの仕様の ``hint_keys`` に入る（``derive_hint_gate_spec``
    が同じ表から導く）。
    """
    contexts = supervisor.rule_policy.contexts
    table: dict[tuple[WorkloadRegime, SupervisorTargetBand], list[WorkloadHintKind]] = {}
    for (regime, workload), target in supervisor.workload_hint.overrides.items():
        band = contexts.get(target).target_band
        table.setdefault((regime, band), []).append(workload)
    return {
        key: tuple(sorted(values, key=lambda item: item.value)) for key, values in table.items()
    }


class HintDomainTracker:
    """送り出した frame の環状の列と、送り出しの時点の判定の保留の表（0162 §2.2）。

    **受付スレッドだけが使う**（lock を持たない）。列は ``domain_lookback_ms`` の区間とその直前の
    1 frame だけ持つ。保留の表は ``pending_domain_result_ms`` の間・``max_pending_domain_results``
    件まで持ち、超えたら古いものから捨てる。
    """

    def __init__(
        self,
        *,
        spec: HintGateSpec,
        cpu_temperature_metric: str,
        gpu_temperature_metric: str,
        lookback_ms: int,
        stale_after_ms: int,
        pending_ms: int,
        max_pending: int,
        hint_keys: Mapping[
            tuple[WorkloadRegime, SupervisorTargetBand], tuple[WorkloadHintKind, ...]
        ],
    ) -> None:
        if lookback_ms < 1 or stale_after_ms < 1 or pending_ms < 1 or max_pending < 1:
            raise ValueError("領域の判定の長さ・閾値・保留の期限と件数は正にする")
        self._spec = spec
        self._cpu_metric = cpu_temperature_metric
        self._gpu_metric = gpu_temperature_metric
        self._lookback_ms = lookback_ms
        self._stale_after_ms = stale_after_ms
        self._pending_ms = pending_ms
        self._max_pending = max_pending
        self._hint_keys = dict(hint_keys)
        self._ring: deque[SentFrameAxes] = deque()
        self._pending: OrderedDict[SourceKey, _Pending] = OrderedDict()
        self._consumed: OrderedDict[tuple[int, int], int] = OrderedDict()

    def axes(self, frame: LearnedFrame) -> SentFrameAxes:
        """frame から判定に要る値だけを取り出す（0162 §2.2「持つもの」）。"""
        hint = frame.supervisor_hint
        axes: HintAxes | None = None
        if hint is not None:
            # supervisor_hint は基準の supervisor と組でしか frame に入らない（frame の検証）
            base = frame.supervisor
            assert base is not None
            baseline = frame.baseline
            axes = HintAxes(
                key=self._key(hint),
                safety_floor=frame.safety_floor,
                baseline=None
                if baseline is None
                else PerZone[Demand](
                    front=baseline.requested.front.demand,
                    rear=baseline.requested.rear.demand,
                    top=baseline.requested.top.demand,
                ),
                cpu_upper_c=base.target_band.cpu_temperature.upper_c,
                gpu_upper_c=base.target_band.gpu_temperature.upper_c,
            )
        return SentFrameAxes(
            domain=domain_frame(
                frame,
                cpu_temperature_metric=self._cpu_metric,
                gpu_temperature_metric=self._gpu_metric,
                spec=self._spec,
            ),
            monotonic_ms=frame.snapshot.monotonic_ms,
            applied=frame.applied,
            hint=axes,
        )

    def on_frame_sent(self, frame: LearnedFrame, *, sent_mono_ms: int) -> None:
        """MPC worker へ送れた frame を列に入れる（ヒントを埋めた frame はその場で判定する）。"""
        self.record_sent(self.axes(frame), sent_mono_ms=sent_mono_ms)

    def record_sent(self, sent: SentFrameAxes, *, sent_mono_ms: int) -> None:
        """送れた frame の値を列に入れ、``hint`` があればその場で判定して保留の表に置く。

        Shadow の記録は元の frame より後の frame を何件も送った後に届きうるので、届いた時点では
        なく**送り出しの時点で**区間を判定する（0162 §2.2。PR #365 の Codex P1 の 1）。
        """
        self._ring.append(sent)
        start_ms = sent.domain.ts_ms - self._lookback_ms
        # 区間の前で最新の frame 1つ（0107 §2.1 の as-of で window の先頭に当たりうる frame）は残す
        while len(self._ring) >= 2 and self._ring[1].domain.ts_ms < start_ms:
            self._ring.popleft()
        self._expire(sent_mono_ms)
        if sent.hint is None:
            return
        self._pending[sent.key] = _Pending(judgement=self._judge(sent), sent_mono_ms=sent_mono_ms)
        self._pending.move_to_end(sent.key)
        while len(self._pending) > self._max_pending:
            self._pending.popitem(last=False)

    def take_result(self, key: SourceKey, *, now_mono_ms: int) -> DomainJudgement | None:
        """元の tick の判定の結果を取り出して消す。無ければ None（範囲の外として扱う）。"""
        self._expire(now_mono_ms)
        pending = self._pending.pop(key, None)
        return None if pending is None else pending.judgement

    def already_consumed(self, tick_id: int, ts_ms: int, *, now_mono_ms: int) -> bool:
        """同じ元の tick の記録を既に通したか（2件目は ``hint_record_rejected``。0162 §2.2）。"""
        self._expire(now_mono_ms)
        return (tick_id, ts_ms) in self._consumed

    def consume(self, tick_id: int, ts_ms: int, *, now_mono_ms: int) -> None:
        """この元の tick の記録を通したことを覚える（保留の表と同じ期限・件数で忘れる）。"""
        self._consumed[(tick_id, ts_ms)] = now_mono_ms
        self._consumed.move_to_end((tick_id, ts_ms))
        while len(self._consumed) > self._max_pending:
            self._consumed.popitem(last=False)

    def _expire(self, now_mono_ms: int) -> None:
        cutoff = now_mono_ms - self._pending_ms
        while self._pending:
            key, oldest = next(iter(self._pending.items()))
            if oldest.sent_mono_ms >= cutoff:
                break
            del self._pending[key]
        while self._consumed:
            consumed, at = next(iter(self._consumed.items()))
            if at >= cutoff:
                break
            del self._consumed[consumed]

    def _judge(self, sent: SentFrameAxes) -> DomainJudgement:
        """送り出す frame ``N`` の判定（0162 §2.2 の 1〜5）。``N`` は列の最後にある。"""
        hint = sent.hint
        assert hint is not None
        previous: PerZone[Demand | None] | None = None
        if len(self._ring) >= 2 and self._ring[-2].domain.tick_id == sent.domain.tick_id - 1:
            # N - 1 の frame の applied（0107 §2.2 の anchor の action と同じ時点）
            previous = self._ring[-2].applied
        return judge_certified_domain(
            [entry.domain for entry in self._ring],
            key=hint.key,
            previous_demand=previous,
            safety_floor=hint.safety_floor,
            baseline=hint.baseline,
            cpu_upper_c=hint.cpu_upper_c,
            gpu_upper_c=hint.gpu_upper_c,
            spec=self._spec,
            lookback_ms=self._lookback_ms,
            stale_after_ms=self._stale_after_ms,
        )

    def _key(self, hint: SupervisorOutput) -> tuple[str, str] | None:
        workloads = self._hint_keys.get((hint.regime, hint.target_band))
        if not workloads:
            return None
        return (hint.regime.value, workloads[0].value)


# ---------------------------------------------------------------- 受信と検証


@dataclass(frozen=True, slots=True)
class HintShadowExpectations:
    """fand が起動時に決めた、記録を照らす値（0133 §2.5 / 0152 §2.6）。毎 run 同じ。"""

    thermal_model: PinnedArtifact | None
    """frame の ``expected_artifacts.thermal_model``（起動時の registry の production）。"""
    certificate_sha256: str
    """起動時に照合を通した証明書の認証のハッシュ。"""
    conditions_sha256: str
    """同じ証明書の部品 ``conditions``。"""
    config: ControlConfigDigest
    """``runtime.config`` と同じ値（record_json に写す）。"""
    max_source_age_ms: int
    """``mpc.max_source_age_ms``。基準の提案と同じ「元 snapshot の単調時刻から数えた寿命」
    （0077 §2.4 の4）を、記録に独立に当てる（0133 §2.5 の「新しさ」）。"""
    eps: float
    eps_c: float
    cpu_temperature_metric: str
    gpu_temperature_metric: str


class HintShadowSink(Protocol):
    """行を書く口（:class:`HintShadowWriter`）。**待たない。**"""

    def submit(self, record: HintShadowRecord) -> bool:
        """書き手の待ち行列へ置く。置けなければ False（捨てる）。"""
        ...


class HintShadowIntake:
    """届いた Shadow の記録の検証と行の組み立て（0133 §2.5 / 0152 §2.6 / 0162 §2.2 / §2.3）。

    **受付スレッドだけが呼ぶ。** loop は :meth:`latest_counters` を lock を取らずに読むだけ
    （参照の差し替えは原子的）。どの失敗でも基準の ``MpcProposal`` の扱いは変わらない（片方向）。
    """

    def __init__(
        self,
        *,
        run_id: str,
        tracker: HintDomainTracker,
        expectations: HintShadowExpectations,
        wall: Clock,
        sink: HintShadowSink,
    ) -> None:
        self._run_id = run_id
        self._tracker = tracker
        self._expect = expectations
        self._wall = wall
        self._sink = sink
        self._epochs: dict[str, HintShadowCounters] = {}
        self._latest: HintShadowCounters | None = None
        self._outcomes: Counter[str] = Counter()

    @property
    def outcomes(self) -> dict[str, int]:
        """行の status / reason ごとの件数（ログと試験のため）。"""
        return dict(self._outcomes)

    def latest_counters(self) -> HintShadowCounters | None:
        """最後に受け入れた worker の累積の数（``HintShadowCountersSource``）。lock を取らない。"""
        return self._latest

    def on_frame_sent(self, frame: LearnedFrame, *, sent_mono_ms: int) -> None:
        """MPC worker へ送れた frame（受付スレッドの送信の後）。"""
        self._tracker.on_frame_sent(frame, sent_mono_ms=sent_mono_ms)

    def receive(
        self, body: HintShadowBody, *, base: MpcProposal | None, received_mono_ms: int
    ) -> HintShadowRecord:
        """1件を検証して行を作り、書き手へ渡す。作った行を返す（書けたかは書き手が数える）。

        ``base`` はいま受け渡し口にある基準の ``MpcProposal``（無ければ None）。
        """
        received_ts_ms = self._wall.now_ms()
        counters = HintShadowCounters(
            worker_instance_id=body.worker_instance_id,
            shadow_dropped_backpressure=body.counters.shadow_dropped_backpressure,
            shadow_record_too_large=body.counters.shadow_record_too_large,
            shadow_unavailable=body.counters.shadow_unavailable,
        )
        previous = self._epochs.get(counters.worker_instance_id)
        status: HintShadowStatus
        reason: HintShadowReason | None
        record_json: str | None = None
        if previous is not None and counters.decreased_from(previous):
            # 同じ epoch の中で数が減った記録だけを捨て、epoch の最後の値は更新しない（0162 §2.3）
            status, reason = HintShadowStatus.REJECTED, HintShadowReason.COUNTERS_DECREASED
        else:
            self._epochs[counters.worker_instance_id] = counters
            self._latest = counters
            rejection = self._rejection(body, base=base, received_mono_ms=received_mono_ms)
            if rejection is not None:
                status, reason = HintShadowStatus.REJECTED, rejection
            else:
                self._tracker.consume(
                    body.source_tick_id, body.source_ts_ms, now_mono_ms=received_mono_ms
                )
                if body.status is WorkerShadowStatus.SKIPPED:
                    assert body.reason is not None
                    status = HintShadowStatus.SKIPPED
                    reason = HintShadowReason(body.reason.value)
                else:
                    assert base is not None
                    status, reason = HintShadowStatus.RECORDED, None
                    record_json = self._record_json(
                        body, base=base, received_mono_ms=received_mono_ms
                    )
        record = HintShadowRecord(
            run_id=self._run_id,
            source_tick_id=body.source_tick_id,
            source_ts_ms=body.source_ts_ms,
            source_monotonic_ms=body.source_monotonic_ms,
            received_monotonic_ms=received_mono_ms,
            received_ts_ms=received_ts_ms,
            schema_version=body.record_schema_version,
            status=status,
            reason=reason,
            record_json=record_json,
            worker_instance_id=counters.worker_instance_id,
            shadow_dropped_backpressure=counters.shadow_dropped_backpressure,
            shadow_record_too_large=counters.shadow_record_too_large,
            shadow_unavailable=counters.shadow_unavailable,
        )
        self._outcomes[status.value if reason is None else f"{status.value}.{reason.value}"] += 1
        self._sink.submit(record)
        return record

    def _rejection(
        self, body: HintShadowBody, *, base: MpcProposal | None, received_mono_ms: int
    ) -> HintShadowReason | None:
        """記録の検証（0133 §2.5 の「新しさ・run_id・artifact・対の基準」と 0152 §2.6 の digest）。

        ``run_id`` は封筒の検証で済んでいる（落ちたら行を作らない）。最初に落ちた理由を返す。
        """
        expect = self._expect
        age_ms = received_mono_ms - body.source_monotonic_ms
        if age_ms < 0 or age_ms > expect.max_source_age_ms:
            return HintShadowReason.STALE
        pinned = expect.thermal_model
        if pinned is None or body.thermal_model != pinned:
            return HintShadowReason.ARTIFACT_MISMATCH
        # conditions は子が束縛を作る前に止まった skipped の記録では無い（PR-B の形）。あれば照らす
        if body.certificate_sha256 != expect.certificate_sha256 or (
            body.conditions_sha256 is not None
            and body.conditions_sha256 != expect.conditions_sha256
        ):
            return HintShadowReason.CERTIFICATION_MISMATCH
        if self._tracker.already_consumed(
            body.source_tick_id, body.source_ts_ms, now_mono_ms=received_mono_ms
        ):
            return HintShadowReason.DUPLICATE_SOURCE
        if body.config != expect.config or not _base_pairs_with(base, body):
            # 元の frame の設定が fand と違う記録は、受け渡し口の基準と同じ条件で解かれていない
            return HintShadowReason.BASE_MISMATCH
        return None

    def _record_json(
        self, body: HintShadowBody, *, base: MpcProposal, received_mono_ms: int
    ) -> str:
        expect = self._expect
        solution = base.solution
        plans = body.plans
        assert solution is not None and plans is not None
        key = (body.source_tick_id, body.source_ts_ms, body.source_monotonic_ms)
        domain = self._tracker.take_result(key, now_mono_ms=received_mono_ms)
        # 2 条件の再検査は、領域の判定が範囲の外でも行って残す（0162 §2.2）。基準の plan は
        # 受け渡し口の基準の解（記録の写しと一致することは検証で確かめてある）
        monotonicity = recheck_monotonicity(
            base_requested=solution.requested,
            base_targets=solution.prediction.targets,
            hint_requested=plans.hint.requested,
            hint_targets=plans.hint.targets,
            cpu_metric=expect.cpu_temperature_metric,
            gpu_metric=expect.gpu_temperature_metric,
            eps=expect.eps,
            eps_c=expect.eps_c,
        )
        document: dict[str, Any] = {
            "base": {
                "requested_demand": solution.requested.model_dump(mode="json"),
                "prediction": [
                    target.model_dump(mode="json") for target in solution.prediction.targets
                ],
            },
            "hint": {
                "requested_demand": plans.hint.requested.model_dump(mode="json"),
                "prediction": [target.model_dump(mode="json") for target in plans.hint.targets],
            },
            "hint_band": plans.hint_target_band.model_dump(mode="json"),
            "recheck": {
                "domain": _domain_json(domain),
                "monotonicity": _monotonicity_json(monotonicity),
            },
            "thermal_model": body.thermal_model.model_dump(mode="json"),
            "config": expect.config.model_dump(mode="json"),
            "certificate_sha256": expect.certificate_sha256,
            "base_result_digest": body.base_result_digest,
        }
        return _canonical_json(document)


def _base_pairs_with(base: MpcProposal | None, body: HintShadowBody) -> bool:
    """いま受け渡し口にある基準が、記録の対の基準と同じで、同じ元の snapshot から作られたか。"""
    if base is None or base.proposal is None:
        return False
    if base.result_digest() != body.base_result_digest:
        return False
    # ``ControllerProposal.seq`` / ``computed_at_ms`` は worker が読んだ snapshot の
    # ``tick_id`` / ``ts_ms`` そのもの（#86。loop の ``_result_is_bound_to_our_snapshot`` と同じ）
    if (base.proposal.seq, base.proposal.computed_at_ms) != (
        body.source_tick_id,
        body.source_ts_ms,
    ):
        return False
    plans = body.plans
    if plans is None:
        return True
    solution = base.solution
    if solution is None:
        # 記録の plan と比べる基準の解が無い（失敗した基準とは対にしない）
        return False
    # 記録が写した基準の plan は、受け渡し口の基準の解そのもの。同じ tick・同じ optimizer の
    # ヒント有りの plan は同じ horizon を持つ（違えば比べられない）
    offsets = [target.offset_ms for target in solution.prediction.targets]
    return (
        plans.base.requested == solution.requested
        and tuple(plans.base.targets) == tuple(solution.prediction.targets)
        and [target.offset_ms for target in plans.hint.targets] == offsets
    )


def _domain_json(judgement: DomainJudgement | None) -> dict[str, object]:
    if judgement is None:
        return {"passed": False, "failed": SOURCE_INTERVAL_UNAVAILABLE}
    failed: DomainCheck | None = judgement.failed
    return {"passed": judgement.passed, "failed": None if failed is None else failed.value}


def _monotonicity_json(judgement: MonotonicityJudgement) -> dict[str, object]:
    failed = judgement.failed
    return {"passed": judgement.passed, "failed": None if failed is None else failed.value}


def _canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------- 書き手


class HintShadowStore(Protocol):
    """書き手のスレッドが自分で開く接続（``SqliteStore`` の一部）。"""

    def record_hint_shadow(self, record: HintShadowRecord) -> HintShadowRecord:
        """1行を追記する。"""
        ...

    def close(self) -> None:
        """接続を閉じる。"""
        ...


_STOP = object()


class HintShadowWriter:
    """``hint_shadow_records`` を書く tick の外の書き手（0133 §2.5a）。受付スレッドを待たせない。

    接続は書き手のスレッドの中で開く（SQLite の接続をスレッドの間で共有しない）。書けなければ
    その行を捨てて数え、次の行で開き直す。待ち行列が一杯なら置かずに捨てて数える（制御は続ける）。
    """

    def __init__(self, *, open_store: Callable[[], HintShadowStore], max_queue: int) -> None:
        if max_queue < 1:
            raise ValueError("書き手の待ち行列の上限は正にする")
        self._open_store = open_store
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(target=self._run, name="hint-shadow-writer", daemon=True)
        self._failures: Counter[str] = Counter()
        self._written = 0

    @property
    def failures(self) -> dict[str, int]:
        """捨てた行の件数（``queue_full`` / ``write_failed``）。"""
        return dict(self._failures)

    @property
    def written(self) -> int:
        """書けた行の件数。"""
        return self._written

    def start(self) -> None:
        """書き手のスレッドを起動する。"""
        self._thread.start()

    def submit(self, record: HintShadowRecord) -> bool:
        """待ち行列へ置く。**待たない。** 置けなければ捨てて False。"""
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._count("queue_full")
            return False
        return True

    def stop(self, *, timeout_s: float) -> None:
        """待ち行列に置いた行を書き終えてから止める（``timeout_s`` まで待つ）。"""
        if not self._thread.is_alive():
            return
        try:
            self._queue.put(_STOP, timeout=timeout_s)
        except queue.Full:
            return
        self._thread.join(timeout=timeout_s)

    def _run(self) -> None:
        store: HintShadowStore | None = None
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                assert isinstance(item, HintShadowRecord)
                try:
                    if store is None:
                        store = self._open_store()
                    store.record_hint_shadow(item)
                    self._written += 1
                except Exception:
                    # 書けなくても制御は続ける（0133 §2.5a）。接続を捨てて次の行で開き直す
                    LOGGER.exception(
                        "Shadow の記録を書けなかった（その行を捨てる）",
                        extra={logs.FIELDS_KEY: {"reason": "hint_shadow_write_failed"}},
                    )
                    self._count("write_failed")
                    if store is not None:
                        _close_quietly(store)
                        store = None
        finally:
            if store is not None:
                _close_quietly(store)

    def _count(self, code: str) -> None:
        self._failures[code] += 1
        count = self._failures[code]
        if count & (count - 1) == 0:
            LOGGER.warning(
                "Shadow の記録を捨てた",
                extra={logs.FIELDS_KEY: {"code": code, "count": count}},
            )


def _close_quietly(store: HintShadowStore) -> None:
    try:
        store.close()
    except Exception:
        LOGGER.exception("Shadow の記録の接続を閉じられなかった")
