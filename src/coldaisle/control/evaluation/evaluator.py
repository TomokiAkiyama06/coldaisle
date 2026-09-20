"""Offline Evaluation の本体（#91 / 決定記録 0054）。

**読み取りだけで、時計を持たない。** 入力は保存済み decision trace と観測、そして設定で、
時刻はすべてそこから来る。同じ入力からは同じ報告（同じ bytes）が出る。

守る線は5つ（0054 §2）。

1. 実測の成果は**適用された構成**にしか帰属させない。適用されなかった提案の下で温度が
   どうなったかは記録から言えない
2. 採点してよいのは `ShadowOutcome.scored` だけ。`unidentifiable` / `unmatched` は
   **coverage として**数え、予測誤差には入れない
3. segment の報告は、その segment の `[start, end)` の中の証拠だけから決まる。
   evidence window を跨ぐ outcome は `purged`
4. 照合の許容幅・絶対温度上限・ΔT の式は**契約から取る**。評価設定に写さない
5. 記録された識別子（`inference_id` / `plan_digest`）で結び直し、結べなければ数えない
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise

from pydantic import ValidationError

from coldaisle.control.acoustic import AcousticCostModel
from coldaisle.control.config import ControlConfig
from coldaisle.control.evaluation.config import (
    UNKNOWN_GROUP,
    EvaluationConfig,
)
from coldaisle.control.evaluation.gate import evaluate_gates
from coldaisle.control.evaluation.model import (
    EVALUATION_REPORT_SCHEMA_VERSION,
    AirBalanceReport,
    AppliedArm,
    AppliedArmReport,
    CountedReason,
    CounterfactualArm,
    CounterfactualArmReport,
    CoverageReport,
    DeltaReport,
    EvaluationProvenance,
    EvaluationReport,
    GroupKind,
    GroupReport,
    InterventionReport,
    ObservedVersions,
    OptimizerReport,
    PredictionReport,
    RunProvenance,
    SegmentReport,
    SegmentRole,
    TemperatureReport,
    WorstCase,
    WorstCaseKind,
    ZoneSeriesReport,
)
from coldaisle.control.evaluation.stats import MetricSummary, shape_of, summarize
from coldaisle.control.schema import (
    BoundBy,
    ControllerKind,
    ControlTick,
    OptimizerStatus,
    PerZone,
    SafetyState,
    ShadowCounterfactual,
    ShadowRecord,
    Zone,
)
from coldaisle.control.shadow import (
    SHADOW_EXPORT_SCHEMA_VERSION,
    ControlTraceRow,
    ObservationIndex,
    OutcomeObservation,
    ShadowExportRow,
    ShadowOutcome,
    ShadowOutcomeMatcher,
    shadow_rows,
)
from coldaisle.metrics import MetricCatalog

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
"""run の名前の形。**`RunProvenance.run_id` と同じにする**（一致は試験で確かめる）。"""

POLICY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
"""記録された Supervisor policy 名の形。**`AppliedArm` の `LegacyPolicyName` と同じにする。**"""

BEFORE_ANY_OBSERVATION_MS = -1
"""`ObservationIndex.nearest` の「この時刻より後」に使う下限。

観測の時刻は 0 以上なので、これを渡すと前後どちらの観測も候補になる。
tick へ観測を結び付けるときは、**前後を問わず最も近いもの**を採る
（予測の照合とは別の用途で、あちらは action より後に限る）。
"""


class EvaluationInputError(ValueError):
    """入力が評価に使えない（索引と中身の食い違い、束縛できない記録、設定の不一致）。"""


@dataclass(frozen=True)
class EvaluationRun:
    """1回分の運転（同じ Dataset / Replay 条件の単位）。

    `shadow` を渡すと #90 の export（JSON Lines）をそのまま取り込む。渡さなければ
    同じ照合器で作り直す。**どちらの場合も trace の記録と突き合わせてから使う。**
    """

    run_id: str
    traces: tuple[ControlTraceRow, ...]
    observations: tuple[OutcomeObservation, ...] = ()
    split_boundaries_ms: tuple[int, ...] = ()
    shadow: tuple[ShadowExportRow, ...] | None = None


@dataclass(frozen=True)
class DerivedDelta:
    """派生 ΔT の式（`config/metrics.yaml` の `derived` から引く）。"""

    name: str
    minuend: str
    subtrahend: str


@dataclass(frozen=True)
class EvaluationContext:
    """評価に使う設定一式と、その素性。

    `delta_metrics` の式は **`config/metrics.yaml` から引いて**ここで束ねる（0054 §2.6）。
    評価設定に式を写すと、`metrics.yaml` と食い違ったときに評価だけが別の定義で動く。
    """

    config: EvaluationConfig
    config_sha256: str
    control: ControlConfig
    catalog: MetricCatalog
    catalog_sha256: str
    acoustic: AcousticCostModel | None = None
    acoustic_sha256: str | None = None
    deltas: tuple[DerivedDelta, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        config: EvaluationConfig,
        config_sha256: str,
        control: ControlConfig,
        catalog: MetricCatalog,
        catalog_sha256: str,
        acoustic: AcousticCostModel | None = None,
        acoustic_sha256: str | None = None,
    ) -> EvaluationContext:
        """派生値の式を引いてから context を作る。引けない名前は入力の誤りとして落とす。"""
        deltas: list[DerivedDelta] = []
        for name in config.delta_metrics:
            meta = catalog.derived.get(name)
            if meta is None:
                raise EvaluationInputError(f"config/metrics.yaml に派生値 {name!r} が無い")
            deltas.append(DerivedDelta(name=name, minuend=meta.minuend, subtrahend=meta.subtrahend))
        return cls(
            config=config,
            config_sha256=config_sha256,
            control=control,
            catalog=catalog,
            catalog_sha256=catalog_sha256,
            acoustic=acoustic,
            acoustic_sha256=acoustic_sha256,
            deltas=tuple(deltas),
        )

    def matcher(self) -> ShadowOutcomeMatcher:
        """**契約の値だけ**で照合器を作る（0054 §2.6）。"""
        shadow = self.control.policy.shadow
        return ShadowOutcomeMatcher(
            match_tolerance_ms=shadow.outcome_match_tolerance_ms.value,
            applied_demand_tolerance=shadow.applied_demand_tolerance.value,
        )


@dataclass(frozen=True)
class _Segment:
    """1つの run の1区間。"""

    run_id: str
    index: int
    role: SegmentRole
    start_ms: int
    end_ms: int
    ticks: tuple[ControlTick, ...]
    observations: tuple[OutcomeObservation, ...]
    shadow: tuple[ShadowExportRow, ...]
    purged_outcomes: int


@dataclass
class _AppliedBucket:
    """1つの group の1つの適用 arm に集まった証拠。"""

    arm: AppliedArm
    ticks: list[ControlTick] = field(default_factory=list)
    values: dict[str, list[float]] = field(default_factory=dict)
    """metric ごとの観測値（この arm の tick に結び付いたもの）。"""
    attributed: dict[tuple[int, int], frozenset[str]] = field(default_factory=dict)
    """tick ごとに、観測が結び付いた metric。**証拠の薄さを数えるのに使う。**"""


@dataclass
class _CounterfactualBucket:
    """1つの group の1つの counterfactual arm に集まった証拠。"""

    arm: CounterfactualArm
    entries: list[tuple[ControlTick, ShadowCounterfactual]] = field(default_factory=list)
    outcomes: list[ShadowOutcome] = field(default_factory=list)


def evaluate(runs: Sequence[EvaluationRun], *, context: EvaluationContext) -> EvaluationReport:
    """比較レポートを作る。**同じ入力からは同じ報告が出る。**"""
    if not runs:
        raise EvaluationInputError("評価する run が1つも無い")
    if len({run.run_id for run in runs}) != len(runs):
        raise EvaluationInputError("同じ run_id の run を2つ渡さない")
    for run in runs:
        if not RUN_ID_PATTERN.fullmatch(run.run_id):
            # 報告の鍵になる名前なので、**報告を組み立てる前に**閉じる。
            raise EvaluationInputError(f"run_id の形が正しくない: {run.run_id!r}")

    segments: list[SegmentReport] = []
    provenances: list[RunProvenance] = []
    versions = _VersionCollector()
    for unchecked in sorted(runs, key=lambda item: item.run_id):
        # **同じ証拠を1つに畳んでから数える。** 以降はこの run だけを使う。
        run = _check_observations(unchecked)
        parsed = _parse_traces(run)
        rows = _shadow_rows_for(run, parsed, context)
        provenances.append(_run_provenance(run, parsed))
        for tick in parsed:
            versions.add_tick(tick)
        for row in rows:
            versions.add_shadow(row.shadow)
        for segment in _segments(run, parsed, rows, context):
            segments.append(_segment_report(segment, context))

    report_segments = tuple(segments)
    worst = _worst_cases(report_segments, context)
    return EvaluationReport(
        provenance=_provenance(context, tuple(provenances), versions.collected()),
        segments=report_segments,
        worst_cases=worst,
        gates=evaluate_gates(report_segments, config=context.config, worst_cases=worst),
    )


# ------------------------------------------------------------------ 入力の検証


def _parse_traces(run: EvaluationRun) -> tuple[ControlTick, ...]:
    """trace を検証して並べ替える。**索引と中身が食い違う行は流さない。**

    版も照らす（`coldaisle.dataset` / `control.shadow.export` と同じ規則）。索引の版だけを
    見て読み分ける側が、中身と違う意味で解釈してしまうため。
    """
    ticks: list[ControlTick] = []
    for row in run.traces:
        try:
            tick = ControlTick.model_validate_json(row.trace_json)
        except ValidationError as exc:
            raise EvaluationInputError(
                f"{run.run_id}: ControlTick decision trace を検証できない"
            ) from exc
        if (tick.ts_ms, tick.tick_id, tick.schema_version) != (
            row.ts_ms,
            row.tick_id,
            row.schema_version,
        ):
            raise EvaluationInputError(
                f"{run.run_id}: ControlTick の外側 index と JSON 本文が一致しない"
            )
        ticks.append(tick)
    ordered = sorted(ticks, key=lambda tick: (tick.ts_ms, tick.tick_id))
    if len({(tick.ts_ms, tick.tick_id) for tick in ordered}) != len(ordered):
        raise EvaluationInputError(f"{run.run_id}: 同じ (ts_ms, tick_id) の trace が2つある")
    return tuple(ordered)


def _check_observations(run: EvaluationRun) -> EvaluationRun:
    """観測を検証し、**同じ証拠を1つに畳んだ** run を返す（決定記録 0054 §2.6）。

    どちらかを選ぶ規則（小さいほう／大きいほう）を置くと、**どちらを選んでも片方の
    事実が消える**。小さいほうを採れば絶対上限の超過が消え、大きいほうを採れば
    予測の当たりが消える。時刻ごとに1つの値しか持てない以上、食い違いは入力の誤りで
    あって、評価が選んでよいものではない。**受け取らずに閉じる**（fail closed）。

    品質が `OK` でない観測は証拠に使わないので（`OutcomeObservation.usable`）、
    食い違いの判定では見ない。

    **まったく同じ観測が2度届くのは冪等な取り込みで起きる**ので許すが、そのまま数えると
    件数も digest も「何回渡したか」に依存する。証拠の集合は同じなので、**1つに畳んでから
    数える**（同じ証拠から同じ `conditions_sha256` が出るようにする。0054 §2.7）。
    """
    seen: dict[tuple[str, int], float] = {}
    for observation in run.observations:
        value = observation.usable
        if value is None:
            continue
        key = (observation.metric, observation.ts_ms)
        previous = seen.get(key)
        if previous is not None and previous != value:
            raise EvaluationInputError(
                f"{run.run_id}: 同じ metric・同じ時刻に食い違う観測がある"
                f"（metric={observation.metric}; ts_ms={observation.ts_ms}; "
                f"{previous} と {value}）"
            )
        seen[key] = value
    unique = sorted(set(run.observations), key=_observation_order)
    return replace(run, observations=tuple(unique))


def _shadow_rows_for(
    run: EvaluationRun, ticks: tuple[ControlTick, ...], context: EvaluationContext
) -> tuple[ShadowExportRow, ...]:
    """Shadow 実績を用意する。**渡された export も trace と突き合わせてから使う。**"""
    matcher = context.matcher()
    computed = tuple(
        sorted(
            shadow_rows(run.traces, observations=run.observations, matcher=matcher),
            key=lambda row: (row.ts_ms, row.tick_id),
        )
    )
    if run.shadow is None:
        return computed
    by_tick = {(tick.ts_ms, tick.tick_id): tick for tick in ticks if tick.shadow is not None}
    rows = sorted(run.shadow, key=lambda row: (row.ts_ms, row.tick_id))
    keys = [(row.ts_ms, row.tick_id) for row in rows]
    if len(set(keys)) != len(keys):
        # 同じ tick の行が2つあると、その分だけ outcome と scored が水増しされ、
        # coverage の下限を「行を複製するだけ」で満たせてしまう。
        duplicated = sorted({key for key in keys if keys.count(key) > 1})
        raise EvaluationInputError(
            f"{run.run_id}: Shadow export に同じ tick の行が複数ある（{duplicated}）"
        )
    if set(keys) != set(by_tick):
        missing = sorted(set(by_tick) - set(keys))
        extra = sorted(set(keys) - set(by_tick))
        # counterfactual を持つ tick と行は**1対1**にする。足りない行は「その区間には
        # 提案が無かった」に化け、余分な行は trace に無い実績を足す。
        raise EvaluationInputError(
            f"{run.run_id}: Shadow export と counterfactual を持つ tick が1対1でない"
            f"（足りない={missing}; 余分={extra}）"
        )
    for row in rows:
        tick = by_tick[(row.ts_ms, row.tick_id)]
        if tick.schema_version != row.control_schema_version or tick.shadow != row.shadow:
            # 別の記録を貼り替えた export を、その tick の実績として読まない。
            raise EvaluationInputError(
                f"{run.run_id}: Shadow export の counterfactual が trace と一致しない"
                f"（tick={row.tick_id}/{row.ts_ms}）"
            )
        _check_outcomes(run.run_id, row, matcher.match_tolerance_ms)
    _check_recomputed(run.run_id, tuple(rows), computed)
    # 照らし合わせが済んだら、**数え直したほうを使う。** 渡された行と1 bit も違わない
    # ことを確かめてあるので値は同じで、以降の経路に外から来た object を残さない。
    return computed


def _check_recomputed(
    run_id: str, supplied: tuple[ShadowExportRow, ...], computed: tuple[ShadowExportRow, ...]
) -> None:
    """渡された export を、**同じ trace と観測から数え直した結果と1欄ずつ照らす**。

    識別子（`inference_id` / `plan_digest`）と許容幅だけを見ても、`status`・`observed`・
    `error`・`expected_ts_ms`・`input_action_ts_ms` は書き換えられる。**採点していない
    区間を `scored` に、外れた予測を誤差の小さい予測に仕立てられる**ので、識別子の照合
    だけでは閉じない。

    照合器は時計も I/O も持たず、同じ入力からは同じ結果を返す（0053 §2.3）。だから
    「数え直して一致するか」で閉じられる。一致しなければ受け取らない（fail closed）。
    """
    if supplied == computed:
        return
    for left, right in zip(supplied, computed, strict=False):
        if left == right:
            continue
        raise EvaluationInputError(
            f"{run_id}: Shadow export が、同じ trace と観測から数え直した結果と違う"
            f"（tick={left.tick_id}/{left.ts_ms}）。"
            f"照合の結果は記録と観測だけから決まるので、書き換えられた行は受け取らない"
        )
    raise EvaluationInputError(
        f"{run_id}: Shadow export の行数が、数え直した結果と違う"
        f"（export={len(supplied)}; 数え直し={len(computed)}）"
    )


def _check_outcomes(run_id: str, row: ShadowExportRow, tolerance_ms: int) -> None:
    """1行の outcome が、同じ行の counterfactual と**1対1で結べる**か（0054 §2.6）。

    結べない outcome は、その行が trace と別の推論を抱えている証拠である。
    数えないだけにすると、壊れた export でも「予測が無かった」として素通りする。
    同じ識別子の outcome が2つあれば、複製するだけで採点数を増やせてしまう。
    """
    bound = {
        (item.inference_id, item.plan.digest())
        for item in row.shadow.counterfactuals
        if item.plan is not None and item.inference_id is not None
    }
    seen: set[tuple[str, str]] = set()
    for outcome in row.outcomes:
        if outcome.match_tolerance_ms != tolerance_ms:
            # 別の許容幅で照合された結果を、同じ coverage として並べない（0054 §2.6）。
            raise EvaluationInputError(
                f"{run_id}: Shadow export の照合許容幅が設定と違う"
                f"（export={outcome.match_tolerance_ms}; config={tolerance_ms}）"
            )
        key = (outcome.inference_id, outcome.plan_digest)
        if key not in bound:
            raise EvaluationInputError(
                f"{run_id}: Shadow export の outcome が、その tick の counterfactual と"
                f"結べない（tick={row.tick_id}/{row.ts_ms}; inference={outcome.inference_id}）"
            )
        if key in seen:
            raise EvaluationInputError(
                f"{run_id}: Shadow export に同じ推論・同じ候補 plan の outcome が複数ある"
                f"（tick={row.tick_id}/{row.ts_ms}; inference={outcome.inference_id}）"
            )
        seen.add(key)


def _bind_outcomes(
    counterfactual: ShadowCounterfactual, outcomes: Sequence[ShadowOutcome]
) -> tuple[ShadowOutcome, ...]:
    """outcome を counterfactual へ**記録済みの識別子で**結び直す（0054 §2.6）。

    時刻の近さでは結ばない。同じ tick に複数の候補がありうるため、`inference_id` と
    `plan_digest` の両方が一致したものだけを、その提案の実績として数える。
    """
    plan = counterfactual.plan
    if plan is None or counterfactual.inference_id is None:
        return ()
    digest = plan.digest()
    return tuple(
        outcome
        for outcome in outcomes
        if outcome.inference_id == counterfactual.inference_id and outcome.plan_digest == digest
    )


# ------------------------------------------------------------------ 時系列 split


def _segments(
    run: EvaluationRun,
    ticks: tuple[ControlTick, ...],
    rows: tuple[ShadowExportRow, ...],
    context: EvaluationContext,
) -> tuple[_Segment, ...]:
    """run を順序付き・重ならない segment に切る（0054 §2.5）。**最後が holdout。**"""
    if not ticks:
        # **tick が1つも無い run を黙って通さない。** segment も gate も作られないので、
        # その run は報告のどこにも現れず、ほかの run だけで `pass` が出てしまう
        # （tick の無い segment を拒むのと同じ理由）。
        raise EvaluationInputError(
            f"{run.run_id}: 指定した期間に decision trace が1つも無い"
            f"（評価する証拠が無い run を、黙って落とさない）"
        )
    start_ms = ticks[0].ts_ms
    end_ms = ticks[-1].ts_ms + 1
    boundaries = tuple(sorted(set(run.split_boundaries_ms)))
    for boundary in boundaries:
        if not start_ms < boundary < end_ms:
            raise EvaluationInputError(
                f"{run.run_id}: split の境界 {boundary} が run の範囲 "
                f"[{start_ms}, {end_ms}) の中に無い"
            )
    edges = (start_ms, *boundaries, end_ms)
    tolerance_ms = context.control.policy.shadow.outcome_match_tolerance_ms.value
    segments: list[_Segment] = []
    for index, (lower, upper) in enumerate(pairwise(edges)):
        inside = tuple(tick for tick in ticks if lower <= tick.ts_ms < upper)
        if not inside:
            # tick の無い区間を作らない。**空の holdout は何も判定しない gate になる**
            # （条件が1つも無い結果を「問題なし」と読める）。
            raise EvaluationInputError(
                f"{run.run_id}: split の境界が tick の無い区間を作る [{lower}, {upper})"
            )
        kept, purged = _split_shadow(rows, lower, upper, tolerance_ms=tolerance_ms)
        segments.append(
            _Segment(
                run_id=run.run_id,
                index=index,
                role=SegmentRole.HOLDOUT if index == len(edges) - 2 else SegmentRole.CALIBRATION,
                start_ms=lower,
                end_ms=upper,
                ticks=inside,
                observations=tuple(
                    observation
                    for observation in run.observations
                    if lower <= observation.ts_ms < upper
                ),
                shadow=kept,
                purged_outcomes=purged,
            )
        )
    return tuple(segments)


def _split_shadow(
    rows: tuple[ShadowExportRow, ...], lower: int, upper: int, *, tolerance_ms: int
) -> tuple[tuple[ShadowExportRow, ...], int]:
    """segment に入る Shadow の行と、evidence window を跨いで `purged` にした outcome 数。

    outcome の証拠は `expected_ts_ms ± 許容幅` の観測なので、**その全部が segment の
    終わりより手前**になければ、この segment の中だけでは採点を確かめられない。
    跨ぐ outcome はどの segment にも入れない（#84 の `split_temporally` と同じ扱い）。
    """
    kept: list[ShadowExportRow] = []
    purged = 0
    for row in rows:
        if not lower <= row.ts_ms < upper:
            continue
        inside: list[ShadowOutcome] = []
        for outcome in row.outcomes:
            latest_ms = max(match.expected_ts_ms for match in outcome.matches)
            if latest_ms + tolerance_ms < upper:
                inside.append(outcome)
            else:
                purged += 1
        kept.append(row.model_copy(update={"outcomes": tuple(inside)}))
    return tuple(kept), purged


# ------------------------------------------------------------------ segment の集計


def _segment_report(segment: _Segment, context: EvaluationContext) -> SegmentReport:
    """1つの segment を、切り口ごとに集計する。"""
    config = context.config
    index = ObservationIndex(segment.observations)
    tick_times = [tick.ts_ms for tick in segment.ticks]
    room = _room_bands(segment.ticks, index, config)
    groups: list[GroupReport] = []
    keys: list[tuple[GroupKind, str]] = [(GroupKind.OVERALL, "overall")]
    keys.extend(
        (GroupKind.WORKLOAD_REGIME, value)
        for value in sorted(
            {
                UNKNOWN_GROUP
                if tick.state.workload_regime is None
                else tick.state.workload_regime.value
                for tick in segment.ticks
            }
        )
    )
    keys.extend((GroupKind.ROOM_TEMPERATURE_BAND, value) for value in sorted(set(room.values())))
    attributed, unattributed = _attribute_observations(segment, tick_times, context)
    for kind, value in keys:
        selected = tuple(
            tick for tick in segment.ticks if _in_group(tick, room, kind=kind, value=value)
        )
        if not selected:
            continue
        chosen = {(tick.ts_ms, tick.tick_id) for tick in selected}
        groups.append(
            GroupReport(
                kind=kind,
                value=value,
                applied=_applied_reports(selected, attributed, context),
                counterfactual=_counterfactual_reports(segment, chosen, context),
            )
        )
    return SegmentReport(
        run_id=segment.run_id,
        index=segment.index,
        role=segment.role,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        ticks=len(segment.ticks),
        unattributed_observations=unattributed,
        purged_outcomes=segment.purged_outcomes,
        groups=tuple(groups),
    )


def _in_group(
    tick: ControlTick, room: dict[tuple[int, int], str], *, kind: GroupKind, value: str
) -> bool:
    if kind is GroupKind.OVERALL:
        return True
    if kind is GroupKind.WORKLOAD_REGIME:
        regime = tick.state.workload_regime
        return value == (UNKNOWN_GROUP if regime is None else regime.value)
    return room[(tick.ts_ms, tick.tick_id)] == value


def _room_bands(
    ticks: Sequence[ControlTick], index: ObservationIndex, config: EvaluationConfig
) -> dict[tuple[int, int], str]:
    """tick ごとの室温帯。**観測が無ければ `unknown`**（黙って落とさない）。"""
    tolerance_ms = config.observation_match_tolerance_ms.value
    bands: dict[tuple[int, int], str] = {}
    for tick in ticks:
        found = index.nearest(
            config.room_temperature_metric,
            expected_ts_ms=tick.ts_ms,
            tolerance_ms=tolerance_ms,
            after_ts_ms=BEFORE_ANY_OBSERVATION_MS,
        )
        bands[(tick.ts_ms, tick.tick_id)] = config.band_for(None if found is None else found[1])
    return bands


def _attribute_observations(
    segment: _Segment, tick_times: Sequence[int], context: EvaluationContext
) -> tuple[dict[tuple[int, int], dict[str, list[float]]], int]:
    """観測を**最も近い tick**へ結び付ける（許容幅の外は帰属させない）。

    温度も ΔT も、tick に結び付いて初めて arm と group に帰属できる。どの tick からも
    離れた観測は、どの arm の実績にも数えない。
    """
    config = context.config
    wanted = set(config.temperature_metrics)
    for delta in context.deltas:
        wanted.update({delta.minuend, delta.subtrahend})
    by_time: dict[int, dict[str, float]] = {}
    tolerance_ms = config.observation_match_tolerance_ms.value
    for observation in segment.observations:
        if observation.metric not in wanted:
            continue
        value = observation.usable
        if value is None:
            continue
        # 食い違う重複は `_check_observations` が先に弾いているので、同じ鍵に来る値は
        # 必ず同じである。**「どちらを採るか」の規則をここに置かない**（どちらを選んでも
        # 片方の事実が消えるため。決定記録 0054 §2.6）。
        by_time.setdefault(observation.ts_ms, {})[observation.metric] = value
    attributed: dict[tuple[int, int], dict[str, list[float]]] = {}
    unattributed = 0
    for ts_ms, values in sorted(by_time.items()):
        tick_key = _nearest_tick(segment.ticks, tick_times, ts_ms, tolerance_ms=tolerance_ms)
        if tick_key is None:
            # **黙って落とさずに数える。** どの tick からも離れた観測は、どの arm の
            # 実績にもしないが、「無かった」ことにもしない。
            unattributed += len(values)
            continue
        bucket = attributed.setdefault(tick_key, {})
        for metric in config.temperature_metrics:
            if metric in values:
                bucket.setdefault(metric, []).append(values[metric])
        for delta in context.deltas:
            # 派生値は**同じ時刻の1組**からだけ作る（別々の時刻の値を引き算しない）。
            if delta.minuend in values and delta.subtrahend in values:
                bucket.setdefault(delta.name, []).append(
                    values[delta.minuend] - values[delta.subtrahend]
                )
    return attributed, unattributed


def _nearest_tick(
    ticks: Sequence[ControlTick],
    tick_times: Sequence[int],
    ts_ms: int,
    *,
    tolerance_ms: int,
) -> tuple[int, int] | None:
    """観測時刻に最も近い tick。**同距離なら過去側**（照合の規則に揃える）。"""
    if not ticks:
        return None
    position = bisect_left(tick_times, ts_ms)
    best: tuple[int, int, int] | None = None
    for candidate in (position - 1, position):
        if not 0 <= candidate < len(ticks):
            continue
        tick = ticks[candidate]
        distance = abs(tick.ts_ms - ts_ms)
        if distance > tolerance_ms:
            continue
        if best is None or distance < best[0]:
            best = (distance, tick.ts_ms, tick.tick_id)
    return None if best is None else (best[1], best[2])


# ------------------------------------------------------------------ 適用された構成


def _applied_arm(tick: ControlTick) -> AppliedArm:
    """tick の**記録から** arm を決める（設定の宣言ではなく証拠から。0054 §2.1）。

    policy は `ControlState.supervisor_policy` を**そのまま**使う。v3 以降は
    `ControlTick` が「選ばれた出力の policy と揃っていること」を検証しているので
    `SupervisorDecision` から引くのと同じ値になり、`SupervisorDecision` を持てない
    v1 / v2 では**記録されていた自由文字列がそのまま残る**。
    列挙値へ狭めると、違う policy で回した古い区間が1つの arm に潰れてしまう。
    """
    policy = tick.state.supervisor_policy
    if policy is not None and not POLICY_NAME_PATTERN.fullmatch(policy):
        # 鍵に入れられない名前は「識別できない」。黙って `none` に潰さない。
        raise EvaluationInputError(
            f"Supervisor policy の名前を arm の鍵にできない"
            f"（tick={tick.tick_id}/{tick.ts_ms}; policy={policy!r}）"
        )
    return AppliedArm(
        controller=tick.state.active_controller,
        supervisor_policy=policy,
        authority_stage=tick.state.authority_stage,
        operating_mode=tick.state.operating_mode,
    )


def _applied_reports(
    ticks: Sequence[ControlTick],
    attributed: dict[tuple[int, int], dict[str, list[float]]],
    context: EvaluationContext,
) -> tuple[AppliedArmReport, ...]:
    buckets: dict[str, _AppliedBucket] = {}
    for tick in ticks:
        arm = _applied_arm(tick)
        bucket = buckets.setdefault(arm.key, _AppliedBucket(arm=arm))
        bucket.ticks.append(tick)
        at_tick = attributed.get((tick.ts_ms, tick.tick_id), {})
        bucket.attributed[(tick.ts_ms, tick.tick_id)] = frozenset(at_tick)
        for metric, values in at_tick.items():
            bucket.values.setdefault(metric, []).extend(values)
    return tuple(_applied_report(buckets[key], context) for key in sorted(buckets))


def _applied_report(bucket: _AppliedBucket, context: EvaluationContext) -> AppliedArmReport:
    config = context.config
    quantiles = config.percentiles
    ticks = bucket.ticks
    gaps: Counter[str] = Counter()
    ceiling = context.control.safety.absolute_temp_ceiling_c.value

    temperatures: list[TemperatureReport] = []
    for metric in config.temperature_metrics:
        values = bucket.values.get(metric, [])
        summary = summarize(values, quantiles=quantiles)
        if summary is None:
            gaps["no_temperature_observation"] += 1
            continue
        margin = summarize((ceiling - value for value in values), quantiles=quantiles)
        assert margin is not None
        sampled = sum(
            1 for tick in ticks if metric in bucket.attributed.get((tick.ts_ms, tick.tick_id), ())
        )
        temperatures.append(
            TemperatureReport(
                metric=metric,
                values=summary,
                ticks=len(ticks),
                # **薄い証拠で温度を語らせない。** 1000 tick に1件の観測でも
                # 平均も margin も出せてしまうので、どれだけの tick が裏づけられて
                # いるかを一緒に持つ（gate が下限を課す）。
                sample_coverage=sampled / len(ticks),
                threshold_c=ceiling,
                margin=margin,
                exceedances=sum(1 for value in values if value >= ceiling),
            )
        )

    deltas: list[DeltaReport] = []
    for delta in context.deltas:
        summary = summarize(bucket.values.get(delta.name, []), quantiles=quantiles)
        if summary is None:
            gaps["no_delta_observation"] += 1
            continue
        sampled = sum(
            1
            for tick in ticks
            if delta.name in bucket.attributed.get((tick.ts_ms, tick.tick_id), ())
        )
        deltas.append(
            DeltaReport(
                metric=delta.name,
                minuend=delta.minuend,
                subtrahend=delta.subtrahend,
                values=summary,
                ticks=len(ticks),
                sample_coverage=sampled / len(ticks),
            )
        )

    acoustic = _acoustic_summary(
        [
            PerZone[float](
                front=tick.zones.front.demand.effective,
                rear=tick.zones.rear.demand.effective,
                top=tick.zones.top.demand.effective,
            )
            for tick in ticks
        ],
        context,
        quantiles=quantiles,
        gaps=gaps,
    )

    rpm = _zone_series(
        ticks, _rpm_of, deadband=config.hunting.rpm_deadband.value, quantiles=quantiles
    )
    if not rpm:
        gaps["no_rpm_readback"] += 1
    elif len(rpm) < len(Zone):
        # **一部の zone だけの RPM を「読めている」と読ませない。**
        gaps["partial_rpm_readback"] += 1

    balance = _air_balance(ticks, quantiles=quantiles)
    if balance is None:
        gaps["no_estimated_flow"] += 1
    elif balance.ticks_without_ratio:
        gaps["partial_estimated_flow"] += 1

    if bucket.arm.controller is ControllerKind.LEARNED_MPC:
        # 適用された Learned MPC の optimizer latency / timeout は `ControlTick`（v6）に
        # 残らない（`ControllerProposal` が trace に埋まっていない。決定記録 0054 §3）。
        # **「この arm には optimizer があるのに記録が無い」ことを、明示的に残す。**
        # 制御器が Fallback の arm に欄が無いこと（該当しない）と区別するため。
        gaps["applied_optimizer_record_unavailable"] += 1

    artifacts, bound, unbound = _applied_artifacts(ticks)
    if unbound:
        # **「artifact 不明」を黙って落とさない。** 落とすと、残った tick の artifact が
        # 区間全体の実績に見え、#92 が部分的な証拠を完全なものとして読む。
        gaps["applied_artifact_unknown"] += 1

    return AppliedArmReport(
        arm=bucket.arm,
        arm_key=bucket.arm.key,
        ticks=len(ticks),
        first_ts_ms=min(tick.ts_ms for tick in ticks),
        last_ts_ms=max(tick.ts_ms for tick in ticks),
        temperatures=tuple(temperatures),
        deltas=tuple(deltas),
        air_balance=balance,
        effective_demand=_zone_series(
            ticks,
            lambda tick, zone: tick.zones.get(zone).demand.effective,
            deadband=config.hunting.demand_deadband.value,
            quantiles=quantiles,
        ),
        requested_demand=_zone_series(
            ticks,
            lambda tick, zone: tick.zones.get(zone).demand.requested,
            deadband=config.hunting.demand_deadband.value,
            quantiles=quantiles,
        ),
        rpm=rpm,
        acoustic_cost=acoustic,
        interventions=_interventions(ticks),
        last_attested_ts_ms=_last_attested_ts_ms(ticks),
        model_artifacts=artifacts,
        bound_attested_ticks=bound,
        unbound_attested_ticks=unbound,
        gaps=_counted(gaps),
    )


def _applied_artifacts(ticks: Sequence[ControlTick]) -> tuple[tuple[str, ...], int, int]:
    """適用した artifact と、**言えた tick の数・言えなかった tick の数**（#159）。

    出どころは trace の `model_gate.artifact_sha256` だけである（決定記録 0059）。
    欄を持たない v1〜v6 の tick は「artifact 不明」として数え、artifact の集合には入れない。
    **記録の無い tick を、記録のある tick の artifact で埋めない。**

    **tick 数も返す。** 集合だけでは「どの artifact か」しか言えず、区間の何割を
    束縛できたのかが分からない（codex #4057573941）。適用 Learned MPC の arm では
    `bound + unbound` がその arm の tick 数と一致する。
    """
    artifacts: set[str] = set()
    bound = 0
    unbound = 0
    for tick in ticks:
        artifact = tick.applied_model_artifact
        if artifact is not None:
            artifacts.add(artifact)
            bound += 1
        elif tick.applied_artifact_unknown:
            unbound += 1
    return tuple(sorted(artifacts)), bound, unbound


def _last_attested_ts_ms(ticks: Sequence[ControlTick]) -> int | None:
    """**裏づけのある Learned 提案が、実際に適用された**最後の tick の時刻。

    区間の最後（`last_ts_ms`）とは別物である。model を読めなかった tick を1つ足すだけで
    古い実績を「新しい」ことにできないよう、記録から言える形で分けて持つ（0057 §2.4）。
    """
    found: int | None = None
    for tick in ticks:
        gate = tick.model_gate
        if gate is None or not gate.attested or not gate.learned_selected:
            continue
        found = tick.ts_ms if found is None else max(found, tick.ts_ms)
    return found


def _rpm_of(tick: ControlTick, zone: Zone) -> float | None:
    readback = tick.zones.get(zone).hardware
    if readback is None or readback.rpm is None:
        return None
    return float(readback.rpm)


def _zone_series(
    ticks: Sequence[ControlTick],
    select: Callable[[ControlTick, Zone], float | None],
    *,
    deadband: float,
    quantiles: Sequence[float],
) -> tuple[ZoneSeriesReport, ...]:
    """zone ごとの時系列を要約する。値の無い zone は**作らない**（0 で埋めない）。"""
    reports: list[ZoneSeriesReport] = []
    for zone in Zone:
        points = [
            (tick.ts_ms, value)
            for tick in ticks
            for value in (select(tick, zone),)
            if value is not None
        ]
        shape = shape_of(points, deadband=deadband, quantiles=quantiles)
        if shape is not None:
            reports.append(ZoneSeriesReport(zone=zone, shape=shape))
    return tuple(reports)


def _air_balance(
    ticks: Sequence[ControlTick], *, quantiles: Sequence[float]
) -> AirBalanceReport | None:
    """推定風量から吸排気比を出す（`AirBalanceEstimate.balance_ratio` と同じ定義）。"""
    ratios: list[float] = []
    without = 0
    for tick in ticks:
        flows = tuple(tick.zones.get(zone).estimated_flow for zone in Zone)
        front, rear, top = flows
        if front is None or rear is None or top is None or front <= 0.0:
            without += 1
            continue
        ratios.append((rear + top) / front)
    summary = summarize(ratios, quantiles=quantiles)
    if summary is None:
        return None
    return AirBalanceReport(
        ratio=summary, ticks_with_ratio=len(ratios), ticks_without_ratio=without
    )


def _acoustic_summary(
    demands: Sequence[PerZone[float]],
    context: EvaluationContext,
    *,
    quantiles: Sequence[float],
    gaps: Counter[str],
) -> MetricSummary | None:
    """approximate Acoustic Cost（無単位。dBA ではない）。

    モデルが無い・値が1つも出なかったときは `None`。**一部の demand でしかコストが
    出なかったときは、その旨を理由として残す**（部分的な証拠を全体に見せない）。
    """
    if context.acoustic is None:
        gaps["no_acoustic_model"] += 1
        return None
    costs: list[float] = []
    for demand in demands:
        estimate = context.acoustic.estimate(demand)
        if estimate is not None:
            costs.append(estimate.acoustic_cost)
    summary = summarize(costs, quantiles=quantiles)
    if summary is None:
        gaps["no_acoustic_cost"] += 1
    elif len(costs) < len(demands):
        gaps["partial_acoustic_cost"] += 1
    return summary


def _interventions(ticks: Sequence[ControlTick]) -> InterventionReport:
    """Safety / Guard / Fallback の介入を数える。"""
    forced_max = safety_floor = guard_floor = guard_ceiling = ramp_down = guard_active = 0
    fallback = emergency = degraded = faulted = 0
    fallback_reasons: Counter[str] = Counter()
    fault_codes: Counter[str] = Counter()
    states: Counter[str] = Counter()
    bound_zones: Counter[str] = Counter()
    for tick in ticks:
        bounds = {tick.zones.get(zone).demand.bound_by for zone in Zone}
        for zone in Zone:
            # **同時に縛られた zone の数を隠さない**（tick 数だけだと 3 zone でも 1）。
            bound_zones[tick.zones.get(zone).demand.bound_by.value] += 1
        forced_max += BoundBy.FORCED_MAX in bounds
        safety_floor += BoundBy.SAFETY_FLOOR in bounds
        guard_floor += BoundBy.GUARD_FLOOR in bounds
        guard_ceiling += BoundBy.GUARD_CEILING in bounds
        ramp_down += BoundBy.RAMP_DOWN in bounds
        guard_active += any(
            tick.zones.get(zone).demand.guard_floor is not None
            or tick.zones.get(zone).demand.guard_ceiling is not None
            for zone in Zone
        )
        if tick.state.fallback_active:
            fallback += 1
            if tick.state.fallback_reason is not None:
                fallback_reasons[tick.state.fallback_reason.code] += 1
        emergency += tick.state.safety_state is SafetyState.EMERGENCY
        degraded += tick.state.safety_state is SafetyState.DEGRADED
        if tick.faults:
            faulted += 1
        for fault in tick.faults:
            fault_codes[fault.code.value] += 1
        states[tick.state.safety_state.value] += 1
    return InterventionReport(
        ticks=len(ticks),
        forced_max_ticks=forced_max,
        safety_floor_ticks=safety_floor,
        guard_floor_ticks=guard_floor,
        guard_ceiling_ticks=guard_ceiling,
        ramp_down_ticks=ramp_down,
        guard_active_ticks=guard_active,
        fallback_ticks=fallback,
        fallback_reasons=_counted(fallback_reasons),
        emergency_ticks=emergency,
        degraded_ticks=degraded,
        fault_ticks=faulted,
        fault_codes=_counted(fault_codes),
        safety_states=_counted(states),
        bound_zone_ticks=_counted(bound_zones),
    )


# ------------------------------------------------------ 適用されなかった提案（counterfactual）


def _counterfactual_reports(
    segment: _Segment, chosen: set[tuple[int, int]], context: EvaluationContext
) -> tuple[CounterfactualArmReport, ...]:
    buckets: dict[str, _CounterfactualBucket] = {}
    by_tick = {(tick.ts_ms, tick.tick_id): tick for tick in segment.ticks}
    for row in segment.shadow:
        key = (row.ts_ms, row.tick_id)
        if key not in chosen:
            continue
        tick = by_tick[key]
        policy = None if row.shadow.supervisor is None else row.shadow.supervisor.policy
        for counterfactual in row.shadow.counterfactuals:
            arm = CounterfactualArm(
                controller=counterfactual.controller,
                supervisor_policy=policy,
                authority_stage=row.shadow.authority_stage,
            )
            bucket = buckets.setdefault(arm.key, _CounterfactualBucket(arm=arm))
            bucket.entries.append((tick, counterfactual))
            bucket.outcomes.extend(_bind_outcomes(counterfactual, row.outcomes))
    return tuple(_counterfactual_report(buckets[key], context) for key in sorted(buckets))


def _counterfactual_report(
    bucket: _CounterfactualBucket, context: EvaluationContext
) -> CounterfactualArmReport:
    config = context.config
    quantiles = config.percentiles
    gaps: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    requested: dict[Zone, list[tuple[int, float]]] = {zone: [] for zone in Zone}
    demands: list[PerZone[float]] = []
    statuses: Counter[OptimizerStatus] = Counter()
    latencies: list[float] = []
    evaluations: list[float] = []
    improvements: list[float] = []
    confidences: list[float] = []
    attested = ood = proposals = shortfalls = 0
    worst_shortfall = 0.0

    last_attested_ts_ms: int | None = None
    for tick, counterfactual in bucket.entries:
        ts_ms = tick.ts_ms
        if counterfactual.failure is not None:
            failures[counterfactual.failure.code] += 1
        if counterfactual.requested is not None:
            proposals += 1
            for zone in Zone:
                value = counterfactual.requested.get(zone)
                requested[zone].append((ts_ms, value))
                # **記録された** Critical Safety floor と比べる（推定しない）。
                floor = tick.zones.get(zone).demand.safety_floor
                if value < floor:
                    shortfalls += 1
                    worst_shortfall = max(worst_shortfall, floor - value)
            demands.append(
                PerZone[float](
                    front=counterfactual.requested.front,
                    rear=counterfactual.requested.rear,
                    top=counterfactual.requested.top,
                )
            )
        if counterfactual.optimizer_status is not None:
            statuses[counterfactual.optimizer_status] += 1
        if counterfactual.latency_ms is not None:
            latencies.append(float(counterfactual.latency_ms))
        if counterfactual.evaluations is not None:
            evaluations.append(float(counterfactual.evaluations))
        if (
            counterfactual.cost_total is not None
            and counterfactual.baseline_cost_total is not None
            and counterfactual.baseline_cost_total > 0.0
        ):
            improvements.append(
                (counterfactual.baseline_cost_total - counterfactual.cost_total)
                / counterfactual.baseline_cost_total
            )
        if counterfactual.attested:
            attested += 1
            if counterfactual.requested is not None:
                # **提案が実在して、裏づけもある tick だけ**を「証拠の新しさ」に数える。
                # 失敗の tick で区間だけ伸ばしても、この時刻は動かない（0057 §2.4）。
                last_attested_ts_ms = (
                    ts_ms if last_attested_ts_ms is None else max(last_attested_ts_ms, ts_ms)
                )
            if counterfactual.confidence is not None:
                confidences.append(counterfactual.confidence)
            if counterfactual.ood:
                ood += 1

    coverage = _coverage(bucket.outcomes, config)
    predictions: tuple[PredictionReport, ...] = ()
    if coverage.sufficient:
        predictions = _predictions(bucket.outcomes, quantiles=quantiles)
    elif coverage.outcomes:
        # 少数の採点区間の平均を、全体の予測精度に見える形で出さない（0054 §2.3）。
        gaps["insufficient_coverage"] += 1

    acoustic = _acoustic_summary(demands, context, quantiles=quantiles, gaps=gaps)
    optimizer = _optimizer(statuses, latencies, evaluations, quantiles=quantiles)
    if optimizer is None:
        gaps["no_optimizer_record"] += 1
    else:
        if optimizer.latency_ms is None:
            gaps["no_optimizer_latency"] += 1
        elif not optimizer.latency_complete:
            # 50 回のうち1回だけ記録があれば、その1件が最大値になって gate を通る。
            gaps["partial_optimizer_latency"] += 1
        if optimizer.evaluations is not None and not optimizer.evaluations_complete:
            gaps["partial_optimizer_evaluations"] += 1
    if proposals == 0:
        gaps["no_proposal"] += 1
    elif proposals < len(bucket.entries):
        # 一部の tick にしか提案が無い区間の要約を、全体の要約として読ませない。
        gaps["partial_proposal"] += 1
    improvement = summarize(improvements, quantiles=quantiles)
    if improvement is None:
        gaps["no_cost_record"] += 1
    elif improvement.count < statuses[OptimizerStatus.OK]:
        gaps["partial_cost_record"] += 1
    confidence = summarize(confidences, quantiles=quantiles)
    if confidence is None:
        # 裏づけ済み（`attested`）の判断が無い区間を「confidence が 0 だった」に見せない。
        gaps["no_attested_confidence"] += 1
    elif confidence.count < attested:
        gaps["partial_attested_confidence"] += 1

    return CounterfactualArmReport(
        arm=bucket.arm,
        arm_key=bucket.arm.key,
        ticks=len(bucket.entries),
        first_ts_ms=min(tick.ts_ms for tick, _item in bucket.entries),
        last_ts_ms=max(tick.ts_ms for tick, _item in bucket.entries),
        proposals=proposals,
        failures=_counted(failures),
        requested_demand=_zone_points(
            requested, deadband=config.hunting.demand_deadband.value, quantiles=quantiles
        ),
        acoustic_cost=acoustic,
        optimizer=optimizer,
        cost_improvement=improvement,
        safety_floor_shortfalls=None if proposals == 0 else shortfalls,
        maximum_floor_shortfall=None if proposals == 0 else worst_shortfall,
        attested_ticks=attested,
        ood_ticks=ood,
        confidence=confidence,
        coverage=coverage,
        predictions=predictions,
        last_attested_ts_ms=last_attested_ts_ms,
        gaps=_counted(gaps),
    )


def _zone_points(
    points: dict[Zone, list[tuple[int, float]]],
    *,
    deadband: float,
    quantiles: Sequence[float],
) -> tuple[ZoneSeriesReport, ...]:
    """時刻付きの点列（zone ごと）を要約する。点の無い zone は作らない。"""
    reports: list[ZoneSeriesReport] = []
    for zone in Zone:
        shape = shape_of(points[zone], deadband=deadband, quantiles=quantiles)
        if shape is not None:
            reports.append(ZoneSeriesReport(zone=zone, shape=shape))
    return tuple(reports)


def _optimizer(
    statuses: Counter[OptimizerStatus],
    latencies: Sequence[float],
    evaluations: Sequence[float],
    *,
    quantiles: Sequence[float],
) -> OptimizerReport | None:
    samples = sum(statuses.values())
    if samples == 0:
        return None
    return OptimizerReport(
        samples=samples,
        ok=statuses[OptimizerStatus.OK],
        timeout=statuses[OptimizerStatus.TIMEOUT],
        error=statuses[OptimizerStatus.ERROR],
        timeout_rate=statuses[OptimizerStatus.TIMEOUT] / samples,
        error_rate=statuses[OptimizerStatus.ERROR] / samples,
        latency_ms=summarize(latencies, quantiles=quantiles),
        evaluations=summarize(evaluations, quantiles=quantiles),
        # **一部の sample にしか記録が無い要約を、全体の要約として読ませない。**
        latency_complete=len(latencies) == samples,
        evaluations_complete=len(evaluations) == samples,
    )


def _coverage(outcomes: Sequence[ShadowOutcome], config: EvaluationConfig) -> CoverageReport:
    """**どれだけが採点できたか**（0054 §2.3）。理由を落とさずに数える。"""
    scored = sum(1 for outcome in outcomes if outcome.scored)
    unidentifiable_reasons: Counter[str] = Counter()
    unmatched_reasons: Counter[str] = Counter()
    predicted: set[str] = set()
    scored_metrics: set[str] = set()
    outputs = matched = scored_outputs = 0
    for outcome in outcomes:
        if outcome.unidentifiable is not None:
            unidentifiable_reasons[outcome.unidentifiable.code] += 1
        for match in outcome.matches:
            outputs += 1
            predicted.add(match.metric)
            if match.observed is not None:
                matched += 1
            if match.unmatched is not None:
                unmatched_reasons[match.unmatched.code] += 1
            if match.error is not None:
                scored_outputs += 1
                scored_metrics.add(match.metric)
    total = len(outcomes)
    fraction = None if total == 0 else scored / total
    # **`scored` は「掛かっていた action を識別できた」ことしか言わない。**
    # 識別できた outcome でも、ある metric の実測が一度も照合できなければ、その metric の
    # 誤差はどこにも出てこない。残りの metric だけで gate を通せないよう、名指しで残す。
    unscored = tuple(sorted(predicted - scored_metrics))
    sufficient = (
        scored > 0
        and not unscored
        and fraction is not None
        and fraction >= config.gate.evidence.minimum_identifiable_fraction.value
        and scored >= config.gate.evidence.minimum_scored_outcomes.value
    )
    return CoverageReport(
        outcomes=total,
        scored=scored,
        unidentifiable=total - scored,
        identifiable_fraction=fraction,
        unidentifiable_reasons=_counted(unidentifiable_reasons),
        outputs=outputs,
        matched_outputs=matched,
        scored_outputs=scored_outputs,
        unmatched_reasons=_counted(unmatched_reasons),
        predicted_metrics=tuple(sorted(predicted)),
        unscored_metrics=unscored,
        sufficient=sufficient,
    )


def _predictions(
    outcomes: Sequence[ShadowOutcome], *, quantiles: Sequence[float]
) -> tuple[PredictionReport, ...]:
    """**`scored` な outcome の出力だけ**から予測誤差を出す（0054 §2.2）。"""
    errors: dict[str, list[float]] = {}
    for outcome in outcomes:
        if not outcome.scored:
            # 掛かっていた action が plan と違う区間の差は、モデル誤差ではない。
            continue
        for match in outcome.matches:
            if match.error is None:
                continue
            errors.setdefault(match.metric, []).append(match.error)
    reports: list[PredictionReport] = []
    for metric in sorted(errors):
        values = errors[metric]
        summary = summarize(values, quantiles=quantiles)
        absolute = summarize((abs(value) for value in values), quantiles=quantiles)
        assert summary is not None and absolute is not None
        under = [value for value in values if value > 0.0]
        reports.append(
            PredictionReport(
                metric=metric,
                scored_outputs=len(values),
                error=summary,
                absolute_error=absolute,
                underprediction_outputs=len(under),
                underprediction_rate=len(under) / len(values),
                maximum_underprediction=max(under) if under else 0.0,
            )
        )
    return tuple(reports)


# ------------------------------------------------------------------ worst-case


def _worst_cases(
    segments: Sequence[SegmentReport], context: EvaluationContext
) -> tuple[WorstCase, ...]:
    """**必ず並べる**（Issue の「worst-case run を必ず確認する」）。

    平均で薄まらないよう、切り口は `overall` の group だけを見て、種類ごとに上位を採る。
    """
    candidates: dict[WorstCaseKind, list[WorstCase]] = {kind: [] for kind in WorstCaseKind}
    for segment in segments:
        for group in segment.groups:
            if group.kind is not GroupKind.OVERALL:
                continue
            for applied in group.applied:
                for temperature in applied.temperatures:
                    candidates[WorstCaseKind.MINIMUM_THRESHOLD_MARGIN].append(
                        _worst(
                            WorstCaseKind.MINIMUM_THRESHOLD_MARGIN,
                            segment,
                            applied.arm_key,
                            temperature.margin.minimum,
                            metric=temperature.metric,
                        )
                    )
                    candidates[WorstCaseKind.MAXIMUM_TEMPERATURE].append(
                        _worst(
                            WorstCaseKind.MAXIMUM_TEMPERATURE,
                            segment,
                            applied.arm_key,
                            temperature.values.maximum,
                            metric=temperature.metric,
                        )
                    )
                    candidates[WorstCaseKind.CEILING_EXCEEDANCES].append(
                        _worst(
                            WorstCaseKind.CEILING_EXCEEDANCES,
                            segment,
                            applied.arm_key,
                            float(temperature.exceedances),
                            metric=temperature.metric,
                        )
                    )
                if applied.temperatures:
                    # **gate が読むのは metric を足した数。** worst-case もそれに揃える。
                    candidates[WorstCaseKind.SEGMENT_CEILING_EXCEEDANCES].append(
                        _worst(
                            WorstCaseKind.SEGMENT_CEILING_EXCEEDANCES,
                            segment,
                            applied.arm_key,
                            float(sum(item.exceedances for item in applied.temperatures)),
                        )
                    )
                candidates[WorstCaseKind.EMERGENCY_TICKS].append(
                    _worst(
                        WorstCaseKind.EMERGENCY_TICKS,
                        segment,
                        applied.arm_key,
                        float(applied.interventions.emergency_ticks),
                    )
                )
            for counterfactual in group.counterfactual:
                for prediction in counterfactual.predictions:
                    candidates[WorstCaseKind.MAXIMUM_UNDERPREDICTION].append(
                        _worst(
                            WorstCaseKind.MAXIMUM_UNDERPREDICTION,
                            segment,
                            counterfactual.arm_key,
                            prediction.maximum_underprediction,
                            metric=prediction.metric,
                        )
                    )
                fraction = counterfactual.coverage.identifiable_fraction
                if fraction is not None:
                    candidates[WorstCaseKind.MINIMUM_IDENTIFIABLE_FRACTION].append(
                        _worst(
                            WorstCaseKind.MINIMUM_IDENTIFIABLE_FRACTION,
                            segment,
                            counterfactual.arm_key,
                            fraction,
                        )
                    )
    ascending = {
        WorstCaseKind.MINIMUM_THRESHOLD_MARGIN,
        WorstCaseKind.MINIMUM_IDENTIFIABLE_FRACTION,
    }
    worst: list[WorstCase] = []
    for kind in WorstCaseKind:
        items = candidates[kind]
        items.sort(
            key=lambda item: (
                item.value if kind in ascending else -item.value,
                item.run_id,
                item.segment_index,
                item.arm_key,
                item.metric or "",
            )
        )
        worst.extend(items[: context.config.worst_case_count])
    return tuple(worst)


def _worst(
    kind: WorstCaseKind,
    segment: SegmentReport,
    arm_key: str,
    value: float,
    *,
    metric: str | None = None,
) -> WorstCase:
    return WorstCase(
        kind=kind,
        run_id=segment.run_id,
        segment_index=segment.index,
        role=segment.role,
        arm_key=arm_key,
        metric=metric,
        value=value,
    )


# ------------------------------------------------------------------ 素性（provenance）


class _VersionCollector:
    """入力に**実在した**版と識別子だけを集める。"""

    def __init__(self) -> None:
        self._control: set[int] = set()
        self._shadow: set[int] = set()
        self._policies: set[str] = set()
        self._models: set[str] = set()
        self._artifacts: set[str] = set()
        self._stages: set[str] = set()
        self._modes: set[str] = set()

    def add_tick(self, tick: ControlTick) -> None:
        self._control.add(tick.schema_version)
        self._stages.add(tick.state.authority_stage.value)
        self._modes.add(tick.state.operating_mode.value)
        if tick.state.model_version is not None:
            self._models.add(tick.state.model_version)
        gate = tick.model_gate
        if gate is not None and gate.artifact_sha256 is not None:
            # **適用側からも集める**（#159 / 決定記録 0059）。counterfactual からしか
            # 集めないと、「artifact B の適用実績 + artifact A の counterfactual」という
            # 報告が `{A}` の照合を素通りする（決定記録 0057 §3）。
            self._artifacts.add(gate.artifact_sha256)
        if tick.supervisor is not None:
            for evaluation in (
                tick.supervisor.active,
                tick.supervisor.fallback,
                tick.supervisor.shadow,
            ):
                if evaluation is not None and evaluation.output is not None:
                    self._policies.add(
                        f"{evaluation.output.policy.value}:{evaluation.output.version}"
                    )

    def add_shadow(self, record: ShadowRecord) -> None:
        self._shadow.add(record.schema_version)
        for counterfactual in record.counterfactuals:
            if counterfactual.model_version is not None:
                self._models.add(counterfactual.model_version)
            if counterfactual.artifact_sha256 is not None:
                self._artifacts.add(counterfactual.artifact_sha256)

    def collected(self) -> ObservedVersions:
        return ObservedVersions(
            control_schema_versions=tuple(sorted(self._control)),
            shadow_schema_versions=tuple(sorted(self._shadow)),
            supervisor_policies=tuple(sorted(self._policies)),
            model_versions=tuple(sorted(self._models)),
            model_artifacts=tuple(sorted(self._artifacts)),
            authority_stages=tuple(sorted(self._stages)),
            operating_modes=tuple(sorted(self._modes)),
        )


def _run_provenance(run: EvaluationRun, ticks: tuple[ControlTick, ...]) -> RunProvenance:
    return RunProvenance(
        run_id=run.run_id,
        start_ms=ticks[0].ts_ms if ticks else 0,
        end_ms=(ticks[-1].ts_ms + 1) if ticks else 0,
        split_boundaries_ms=tuple(sorted(set(run.split_boundaries_ms))),
        traces=len(ticks),
        observations=len(run.observations),
        trace_sha256=_digest(
            [tick.ts_ms, tick.tick_id, tick.schema_version, json.loads(tick.model_dump_json())]
            for tick in ticks
        ),
        observation_sha256=_digest(
            [observation.metric, observation.ts_ms, observation.value, observation.quality.value]
            # **全部の欄で並べる。** `(metric, ts_ms)` だけだと、同じ時刻に2つ届いた
            # 観測の順序が入力の順序に残り、同じ入力集合から違う digest が出る。
            for observation in sorted(run.observations, key=_observation_order)
        ),
    )


def _observation_order(observation: OutcomeObservation) -> tuple[str, int, bool, float, str]:
    """観測の全順序。`value` が `None` でも壊れないようにする。"""
    return (
        observation.metric,
        observation.ts_ms,
        observation.value is None,
        observation.value if observation.value is not None else 0.0,
        observation.quality.value,
    )


def _provenance(
    context: EvaluationContext, runs: tuple[RunProvenance, ...], versions: ObservedVersions
) -> EvaluationProvenance:
    shadow = context.control.policy.shadow
    sources = context.control.sources
    conditions = _digest(
        [
            [
                context.config_sha256,
                sources.fan_hardware.sha256,
                sources.safety.sha256,
                sources.policy.sha256,
                context.catalog_sha256,
                context.acoustic_sha256,
                # **出力の形を決めるコード側の版も条件に入れる。** 設定の hash だけでは
                # 「同じ条件」が同じ意味の行を指しているとは言えない。
                SHADOW_EXPORT_SCHEMA_VERSION,
                EVALUATION_REPORT_SCHEMA_VERSION,
            ],
            [json.loads(run.model_dump_json()) for run in runs],
        ]
    )
    return EvaluationProvenance(
        evaluation_config_sha256=context.config_sha256,
        fan_hardware_config_sha256=sources.fan_hardware.sha256,
        safety_config_sha256=sources.safety.sha256,
        fan_policy_config_sha256=sources.policy.sha256,
        metric_catalog_sha256=context.catalog_sha256,
        acoustic_config_sha256=context.acoustic_sha256,
        absolute_temp_ceiling_c=context.control.safety.absolute_temp_ceiling_c.value,
        outcome_match_tolerance_ms=shadow.outcome_match_tolerance_ms.value,
        applied_demand_tolerance=shadow.applied_demand_tolerance.value,
        shadow_export_schema_version=SHADOW_EXPORT_SCHEMA_VERSION,
        runs=runs,
        versions=versions,
        conditions_sha256=conditions,
    )


def _digest(items: Iterable[object]) -> str:
    """型と境界を保った canonical JSON を1要素ずつ加えた SHA-256。

    `coldaisle.dataset` の digest と同じ形にする（長さを前置きして境界を保つ）。
    """
    digest = hashlib.sha256()
    for item in items:
        encoded = json.dumps(
            item, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _counted(counter: Counter[str]) -> tuple[CountedReason, ...]:
    """理由ごとの件数を、**名前の昇順**で固定する（同じ入力から同じ bytes を出すため）。"""
    return tuple(CountedReason(code=code, count=count) for code, count in sorted(counter.items()))


def controller_kinds(report: EvaluationReport) -> frozenset[ControllerKind]:
    """報告に現れた counterfactual の制御器。比較対象の確認に使う。"""
    return frozenset(
        item.arm.controller
        for segment in report.segments
        for group in segment.groups
        for item in group.counterfactual
    )
