"""Thermal Model の drift 検知（#93 / 決定記録 0056）。

**読み取りだけで、時計を持たない。** 入力は #90 の Shadow export（保存済み decision trace と
観測から作られたもの）、Dataset から起こした入力、そして人が宣言した構成変更で、時刻はすべて
そこから来る。同じ入力からは同じ報告（同じ bytes）が出る。

**制御へ届かない。** この module は `coldaisle.control.hardware` / `safety` / `reactive` を
import せず、`Demand` も PWM も authority も作らない。confidence を動かすのは runtime の
`ConfidenceAssessor` だけである（0056 §2.1。**試験で走査する**）。

守る線は5つ（決定記録 0056 §2.3 / §2.4）。

1. 誤差に数えてよいのは `status: scored` で**全出力が照合できた** outcome だけ
2. 証拠は model / artifact / 推論 / 候補 plan の識別子で束縛する。結べない証拠は受け取らない
3. 同じ証拠を2回数えない（複製で下限を満たせないようにする）
4. coverage が足りなければ**比を出さず**、`insufficient_evidence` と答える（`ok` にしない）
5. 宣言された構成変更より前の証拠は数えない
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from coldaisle.control.drift.config import DriftConfig
from coldaisle.control.drift.model import (
    MAX_DRIFT_TREND_BUCKETS,
    ChangeKind,
    DeclaredChange,
    DriftCoverage,
    DriftProvenance,
    DriftReasonCount,
    DriftReport,
    DriftSignalKind,
    DriftSourceCount,
    DriftTarget,
    DriftVerdict,
    InputDriftSignal,
    MetricResidual,
    ResidualDriftSignal,
    ResidualTrendBucket,
    RetrainingRecommendation,
    RetrainingTrigger,
    combine_verdicts,
)
from coldaisle.control.model.confidence import ConfidenceAssessor
from coldaisle.control.model.thermal import ObservedThermalInput
from coldaisle.control.schema import ControllerKind, ShadowCounterfactual
from coldaisle.control.shadow import (
    SHADOW_EXPORT_SCHEMA_VERSION,
    ShadowExportRow,
    ShadowOutcome,
)


class DriftInputError(ValueError):
    """証拠として受け取れない入力（束縛の壊れた記録・重複・別の照合幅）。"""


class DriftConfigError(ValueError):
    """drift 設定が runtime の契約と噛み合わない。"""


@dataclass(frozen=True)
class DriftEvidence:
    """1回の判定に使う証拠。**すべて保存済みの記録か、人が宣言した事実である。**"""

    shadow: tuple[ShadowExportRow, ...] = ()
    """#90 の export（0053 §2.4）。**drift 専用の収集経路は作らない。**"""
    inputs: tuple[ObservedThermalInput, ...] = ()
    """入力分布を見るための直近の推論入力（#83 Dataset の example から起こす）。"""
    changes: tuple[DeclaredChange, ...] = ()
    """宣言された構成変更。**この時刻より前の証拠は数えない**（0056 §2.5）。"""


@dataclass
class _ResidualOutcome:
    """比に数えた outcome 1件。"""

    evidence_ts_ms: int
    inference_id: str
    mean_squared: float
    outputs: int


@dataclass
class _ResidualTally:
    """residual の集計中の状態。"""

    outcomes: int = 0
    scored: int = 0
    unidentifiable: int = 0
    incomplete_scored: int = 0
    outputs: int = 0
    matched_outputs: int = 0
    counted_outputs: int = 0
    excluded_by_change: int = 0
    foreign_model: int = 0
    unidentifiable_reasons: dict[str, int] = field(default_factory=dict)
    unmatched_reasons: dict[str, int] = field(default_factory=dict)
    predicted_metrics: set[str] = field(default_factory=set)
    counted_metrics: dict[str, list[float]] = field(default_factory=dict)
    counted: list[_ResidualOutcome] = field(default_factory=list)


class DriftDetector:
    """Profile と運用中の証拠から drift を判定し、再学習の推奨を出す。

    判定に使う「何を逸脱と呼ぶか」は runtime の `model_confidence` がそのまま決める。
    **drift 設定に写さない**（0056 §2.2）。
    """

    def __init__(
        self,
        assessor: ConfidenceAssessor,
        config: DriftConfig,
        *,
        config_sha256: str,
    ) -> None:
        policy = assessor.policy
        if config.residual.degraded_ratio.value > policy.residual_drift_ood_ratio.value:
            # offline が runtime より鈍いと、runtime が OOD で Fallback へ落ちている最中に
            # offline は「問題なし」と答える。写さず、照合して落とす（0056 §2.8）。
            raise DriftConfigError(
                "drift の degraded_ratio は model_confidence.residual_drift_ood_ratio "
                f"以下にする（drift={config.residual.degraded_ratio.value}; "
                f"model_confidence={policy.residual_drift_ood_ratio.value}）"
            )
        self._assessor = assessor
        self._profile = assessor.profile
        self._policy = policy
        self._config = config
        self._config_sha256 = config_sha256
        self._scales = {
            (scale.horizon_ms, scale.metric): scale.scale for scale in self._profile.residual_scales
        }

    def detect(self, evidence: DriftEvidence) -> DriftReport:
        """証拠から1つの報告を作る。**同じ証拠からは同じ bytes になる。**"""
        # まったく同じ宣言が2度届くのは冪等な取り込みで起きる。**1つに畳んでから**
        # 数える（報告の bytes が「何回渡したか」に依存しないように。0054 §2.6 と同じ扱い）。
        changes = tuple(
            sorted(set(evidence.changes), key=lambda item: (item.ts_ms, item.kind.value))
        )
        cutoff_ms = max((change.ts_ms for change in changes), default=None)
        tally = self._tally_residual(evidence.shadow, cutoff_ms)
        residual = self._residual_signal(tally)
        inputs = self._input_signals(evidence.inputs, cutoff_ms)
        verdicts = (residual.verdict, *(signal.verdict for signal in inputs))
        recommendation = self._recommend(residual, inputs, changes)
        binding = self._profile.binding
        return DriftReport(
            target=DriftTarget(
                model_id=binding.model_id,
                model_version=binding.model_version,
                artifact_sha256=binding.artifact_sha256,
                profile_sha256=self._profile.sha256(),
            ),
            verdict=combine_verdicts(verdicts),
            residual=residual,
            inputs=inputs,
            declared_changes=changes,
            recommendation=recommendation,
            provenance=DriftProvenance(
                drift_config_sha256=self._config_sha256,
                profile_sha256=self._profile.sha256(),
                shadow_export_schema_version=SHADOW_EXPORT_SCHEMA_VERSION,
                residual_drift_ood_ratio=self._policy.residual_drift_ood_ratio.value,
                range_margin=self._policy.range_margin.value,
                min_support_count=self._policy.min_support_count.value,
                min_missing_pattern_count=self._policy.min_missing_pattern_count.value,
                shadow_rows=len(evidence.shadow),
                evidence_sha256=_evidence_sha256(evidence),
            ),
        )

    # ------------------------------------------------------------ residual

    def _tally_residual(
        self, rows: Sequence[ShadowExportRow], cutoff_ms: int | None
    ) -> _ResidualTally:
        tally = _ResidualTally()
        seen_ticks: set[tuple[int, int]] = set()
        seen_outcomes: set[tuple[str, str]] = set()
        tolerance_ms: int | None = None
        for row in rows:
            if row.schema_version != SHADOW_EXPORT_SCHEMA_VERSION:
                raise DriftInputError(
                    f"別の版の Shadow export を混ぜない（row={row.schema_version}; "
                    f"既知={SHADOW_EXPORT_SCHEMA_VERSION}）"
                )
            if (row.tick_id, row.ts_ms) in seen_ticks:
                # 行を複製するだけで coverage の下限を満たせる（0054 §2.6 と同じ理由）。
                raise DriftInputError(f"同じ tick の Shadow 行が2度現れた: {row.tick_id}")
            seen_ticks.add((row.tick_id, row.ts_ms))
            candidates = _counterfactuals_by_plan(row)
            for outcome in row.outcomes:
                key = (outcome.inference_id, outcome.plan_digest)
                counterfactual = candidates.get(key)
                if counterfactual is None:
                    # 時刻の近さで結び直さない。結べない記録は受け取らない（0054 §2.6）。
                    raise DriftInputError(
                        f"outcome を同じ tick の counterfactual と結べない: {outcome.inference_id}"
                    )
                if key in seen_outcomes:
                    raise DriftInputError(
                        f"同じ推論・同じ候補 plan の outcome が2度現れた: {outcome.inference_id}"
                    )
                seen_outcomes.add(key)
                if tolerance_ms is None:
                    tolerance_ms = outcome.match_tolerance_ms
                elif tolerance_ms != outcome.match_tolerance_ms:
                    # 違う幅で照合された結果を、同じ coverage として並べない。
                    raise DriftInputError(
                        "別の照合許容幅で作られた outcome を混ぜない"
                        f"（{tolerance_ms} と {outcome.match_tolerance_ms}）"
                    )
                self._tally_outcome(tally, outcome, counterfactual, cutoff_ms)
        return tally

    def _tally_outcome(
        self,
        tally: _ResidualTally,
        outcome: ShadowOutcome,
        counterfactual: ShadowCounterfactual,
        cutoff_ms: int | None,
    ) -> None:
        prediction = counterfactual.prediction
        assert prediction is not None  # _counterfactuals_by_plan が保証する
        if outcome.input_action_ts_ms != prediction.input_action_ts_ms:
            raise DriftInputError("outcome と予測の action 時刻が一致しない")
        binding = self._profile.binding
        if (
            prediction.model_id,
            counterfactual.model_version,
            counterfactual.artifact_sha256,
        ) != (binding.model_id, binding.model_version, binding.artifact_sha256):
            # 別のモデルの区間を、この Production artifact の実績に数えない。
            tally.foreign_model += 1
            return
        _check_observation_times(outcome)
        evidence_ts_ms = _evidence_time(outcome)
        if cutoff_ms is not None and evidence_ts_ms <= cutoff_ms:
            tally.excluded_by_change += 1
            return

        # **出力の集合は予測から取る。** `outcome.matches` に現れた出力だけを見ると、
        # 記録から出力を落とすだけで「全出力が揃った forecast」に仕立てられる
        # （`ShadowOutcome.complete` は残っている match の中だけを見る）。
        expected = {
            (target.offset_ms, metric) for target in prediction.targets for metric in target.values
        }
        actual = {(match.offset_ms, match.metric) for match in outcome.matches}
        if len(actual) != len(outcome.matches):
            # 同じ出力を2度置けば、その誤差を2回数えられる。
            raise DriftInputError(
                f"同じ出力の照合結果が1つの outcome に2つある: {outcome.inference_id}"
            )
        if actual - expected:
            # 予測に無い出力は、記録が別の推論を抱えている証拠である。
            raise DriftInputError(f"予測に無い出力の照合結果がある: {sorted(actual - expected)}")
        missing = expected - actual

        tally.outcomes += 1
        tally.outputs += len(expected)
        tally.predicted_metrics.update(metric for _offset, metric in expected)
        for match in outcome.matches:
            if match.observed is None:
                assert match.unmatched is not None
                _bump(tally.unmatched_reasons, match.unmatched.code)
            else:
                tally.matched_outputs += 1
        for _key in missing:
            # **落ちた出力を「無かったこと」にしない。** 照合できなかったのと同じ扱いにする。
            _bump(tally.unmatched_reasons, "output_missing_from_outcome")
        if not outcome.scored:
            tally.unidentifiable += 1
            assert outcome.unidentifiable is not None
            _bump(tally.unidentifiable_reasons, outcome.unidentifiable.code)
            return

        tally.scored += 1
        if missing or not outcome.complete:
            # 照合できた出力だけの誤差は、当たりやすい出力に偏る（0056 §2.3）。
            tally.incomplete_scored += 1
            return
        squared: list[float] = []
        for match in outcome.matches:
            scale = self._scales.get((match.offset_ms, match.metric))
            if scale is None:
                # binding が一致しているのに出力が Profile の target schema に無い。
                # 記録か Profile のどちらかが壊れている。
                raise DriftInputError(
                    f"予測の出力が Profile の target schema に無い: "
                    f"offset_ms={match.offset_ms}; metric={match.metric}"
                )
            assert match.error is not None  # scored かつ complete
            normalized = (match.error / scale) ** 2
            squared.append(normalized)
            tally.counted_metrics.setdefault(match.metric, []).append(normalized)
        tally.counted.append(
            _ResidualOutcome(
                evidence_ts_ms=evidence_ts_ms,
                inference_id=outcome.inference_id,
                mean_squared=math.fsum(squared) / len(squared),
                outputs=len(squared),
            )
        )
        tally.counted_outputs += len(squared)

    def _residual_signal(self, tally: _ResidualTally) -> ResidualDriftSignal:
        gate = self._config.residual
        counted = len(tally.counted)
        fraction = None if tally.outcomes == 0 else tally.scored / tally.outcomes
        unscored = tuple(sorted(tally.predicted_metrics - set(tally.counted_metrics)))
        sufficient = (
            counted >= gate.minimum_scored_outcomes.value
            and fraction is not None
            and fraction >= gate.minimum_identifiable_fraction.value
            and not unscored
        )
        coverage = DriftCoverage(
            outcomes=tally.outcomes,
            scored=tally.scored,
            counted=counted,
            unidentifiable=tally.unidentifiable,
            identifiable_fraction=fraction,
            unidentifiable_reasons=_counted_reasons(tally.unidentifiable_reasons),
            incomplete_scored=tally.incomplete_scored,
            outputs=tally.outputs,
            matched_outputs=tally.matched_outputs,
            counted_outputs=tally.counted_outputs,
            unmatched_reasons=_counted_reasons(tally.unmatched_reasons),
            predicted_metrics=tuple(sorted(tally.predicted_metrics)),
            unscored_metrics=unscored,
            excluded_by_change=tally.excluded_by_change,
            foreign_model=tally.foreign_model,
            sufficient=sufficient,
        )
        warning_ratio = gate.warning_ratio.value
        degraded_ratio = gate.degraded_ratio.value
        if not sufficient:
            # **足りない証拠からは何も出さない。** 比も trend も metric 別も伏せる。
            return ResidualDriftSignal(
                verdict=DriftVerdict.INSUFFICIENT_EVIDENCE,
                coverage=coverage,
                warning_ratio=warning_ratio,
                degraded_ratio=degraded_ratio,
            )
        ordered = sorted(tally.counted, key=lambda item: (item.evidence_ts_ms, item.inference_id))
        ratio = math.sqrt(math.fsum(item.mean_squared for item in ordered) / len(ordered))
        return ResidualDriftSignal(
            verdict=_verdict_for(ratio, warning_ratio, degraded_ratio),
            coverage=coverage,
            ratio=ratio,
            warning_ratio=warning_ratio,
            degraded_ratio=degraded_ratio,
            per_metric=tuple(
                MetricResidual(
                    metric=metric,
                    counted_outputs=len(values),
                    ratio=math.sqrt(math.fsum(values) / len(values)),
                )
                for metric, values in sorted(tally.counted_metrics.items())
            ),
            trend=self._trend(ordered),
            first_evidence_ts_ms=ordered[0].evidence_ts_ms,
            last_evidence_ts_ms=ordered[-1].evidence_ts_ms,
        )

    def _trend(self, ordered: Sequence[_ResidualOutcome]) -> tuple[ResidualTrendBucket, ...]:
        """証拠時刻順に一定件数ずつ切った residual の推移。

        **bucket は自分の件数だけで判定する。** 足りない bucket は比を持たない。
        隣から借りると、「証拠は薄い区間から、指標は厚い区間から」になる（0056 §2.4）。
        """
        gate = self._config.residual
        size = gate.trend_bucket_outcomes.value
        minimum = gate.minimum_bucket_outcomes.value
        chunks = [ordered[start : start + size] for start in range(0, len(ordered), size)]
        if len(chunks) > MAX_DRIFT_TREND_BUCKETS:
            raise DriftInputError(
                f"residual trend の bucket 数が構造上限を超えた（{len(chunks)} > "
                f"{MAX_DRIFT_TREND_BUCKETS}）。期間を区切って判定する"
            )
        buckets: list[ResidualTrendBucket] = []
        for index, chunk in enumerate(chunks):
            enough = len(chunk) >= minimum
            ratio = (
                math.sqrt(math.fsum(item.mean_squared for item in chunk) / len(chunk))
                if enough
                else None
            )
            buckets.append(
                ResidualTrendBucket(
                    index=index,
                    start_ts_ms=chunk[0].evidence_ts_ms,
                    end_ts_ms=chunk[-1].evidence_ts_ms,
                    outcomes=len(chunk),
                    ratio=ratio,
                    verdict=(
                        DriftVerdict.INSUFFICIENT_EVIDENCE
                        if ratio is None
                        else _verdict_for(
                            ratio, gate.warning_ratio.value, gate.degraded_ratio.value
                        )
                    ),
                )
            )
        return tuple(buckets)

    # ------------------------------------------------------------ input distribution

    def _input_signals(
        self, inputs: Sequence[ObservedThermalInput], cutoff_ms: int | None
    ) -> tuple[InputDriftSignal, ...]:
        """入力分布の逸脱を3つの切り口で数える。

        **判定は `ConfidenceAssessor` と同じ実装**（`coverage()`）を通す。ここで範囲や
        support を数え直すと、runtime と offline が別の規則で動く（0056 §2.2）。
        """
        excluded = 0
        considered = 0
        affected: dict[DriftSignalKind, int] = {
            DriftSignalKind.FEATURE_RANGE: 0,
            DriftSignalKind.SUPPORT: 0,
            DriftSignalKind.MISSING_PATTERN: 0,
        }
        sources: dict[DriftSignalKind, dict[str, int]] = {kind: {} for kind in affected}
        first_ts: int | None = None
        last_ts: int | None = None
        margin = self._policy.range_margin.value
        seen_inputs: set[int] = set()
        for observed in inputs:
            if observed.action_ts_ms in seen_inputs:
                # **同じ推論入力を2回数えない。** 1件を並べ直すだけで `minimum_inputs` を
                # 満たせてしまう（行の複製で coverage を満たせないのと同じ型の穴）。
                raise DriftInputError(
                    f"同じ action 時刻の推論入力が2度現れた: {observed.action_ts_ms}"
                )
            seen_inputs.add(observed.action_ts_ms)
            if cutoff_ms is not None and observed.action_ts_ms <= cutoff_ms:
                excluded += 1
                continue
            considered += 1
            first_ts = (
                observed.action_ts_ms if first_ts is None else min(first_ts, observed.action_ts_ms)
            )
            last_ts = (
                observed.action_ts_ms if last_ts is None else max(last_ts, observed.action_ts_ms)
            )
            coverage = self._assessor.coverage(observed)
            worst, source = (
                (coverage.worst_feature_excess, coverage.worst_feature_source)
                if coverage.worst_feature_excess >= coverage.worst_fan_excess
                else (coverage.worst_fan_excess, coverage.worst_fan_source)
            )
            if worst > margin:
                affected[DriftSignalKind.FEATURE_RANGE] += 1
                _bump(sources[DriftSignalKind.FEATURE_RANGE], source or "unknown")
            if coverage.support_bins is None:
                affected[DriftSignalKind.SUPPORT] += 1
                _bump(sources[DriftSignalKind.SUPPORT], "axis_unavailable")
            elif coverage.support_count < self._policy.min_support_count.value:
                affected[DriftSignalKind.SUPPORT] += 1
                _bump(sources[DriftSignalKind.SUPPORT], _cell_name(coverage.support_bins))
            if coverage.missing_pattern_count < self._policy.min_missing_pattern_count.value:
                affected[DriftSignalKind.MISSING_PATTERN] += 1
                _bump(
                    sources[DriftSignalKind.MISSING_PATTERN],
                    ",".join(coverage.missing_pattern) or "-",
                )
        minimum = self._config.minimum_inputs.value
        gates = {
            DriftSignalKind.FEATURE_RANGE: self._config.feature_range,
            DriftSignalKind.SUPPORT: self._config.support,
            DriftSignalKind.MISSING_PATTERN: self._config.missing_pattern,
        }
        signals: list[InputDriftSignal] = []
        for kind, gate in gates.items():
            enough = considered >= minimum
            fraction = affected[kind] / considered if enough and considered else None
            signals.append(
                InputDriftSignal(
                    kind=kind,
                    verdict=(
                        DriftVerdict.INSUFFICIENT_EVIDENCE
                        if fraction is None
                        else _verdict_for(
                            fraction, gate.warning_fraction.value, gate.degraded_fraction.value
                        )
                    ),
                    inputs=considered + excluded,
                    considered=considered,
                    excluded_by_change=excluded,
                    affected=affected[kind],
                    fraction=fraction,
                    warning_fraction=gate.warning_fraction.value,
                    degraded_fraction=gate.degraded_fraction.value,
                    sources=() if fraction is None else _source_counts(sources[kind]),
                    first_input_ts_ms=None if fraction is None else first_ts,
                    last_input_ts_ms=None if fraction is None else last_ts,
                )
            )
        return tuple(signals)

    # ------------------------------------------------------------ recommendation

    def _recommend(
        self,
        residual: ResidualDriftSignal,
        inputs: Sequence[InputDriftSignal],
        changes: Sequence[DeclaredChange],
    ) -> RetrainingRecommendation:
        """再学習の推奨。**Registry も設定も書き換えない**（0056 §2.6）。"""
        triggers: list[RetrainingTrigger] = []
        if residual.verdict is DriftVerdict.DEGRADED:
            assert residual.ratio is not None
            triggers.append(
                RetrainingTrigger(
                    code="residual_degraded",
                    signal=DriftSignalKind.RESIDUAL,
                    detail=(
                        f"ratio={residual.ratio:.6f}; degraded_ratio="
                        f"{residual.degraded_ratio:.6f}; outcomes={residual.coverage.counted}"
                    ),
                    evidence_ts_ms=residual.last_evidence_ts_ms,
                )
            )
        for signal in inputs:
            if signal.verdict is not DriftVerdict.DEGRADED:
                continue
            assert signal.fraction is not None
            triggers.append(
                RetrainingTrigger(
                    code=f"{signal.kind.value}_degraded",
                    signal=signal.kind,
                    detail=(
                        f"fraction={signal.fraction:.6f}; degraded_fraction="
                        f"{signal.degraded_fraction:.6f}; inputs={signal.considered}"
                    ),
                    evidence_ts_ms=signal.last_input_ts_ms,
                )
            )
        latest: dict[ChangeKind, int] = {}
        for change in changes:
            latest[change.kind] = max(latest.get(change.kind, 0), change.ts_ms)
        for kind, ts_ms in sorted(latest.items(), key=lambda item: item[0].value):
            triggers.append(
                RetrainingTrigger(
                    code=f"declared_{kind.value}",
                    detail="宣言された構成変更。変更より前の証拠は数えていない",
                    evidence_ts_ms=ts_ms,
                )
            )
        inconclusive = tuple(
            kind
            for kind, verdict in (
                (DriftSignalKind.RESIDUAL, residual.verdict),
                *((signal.kind, signal.verdict) for signal in inputs),
            )
            if verdict is DriftVerdict.INSUFFICIENT_EVIDENCE
        )
        return RetrainingRecommendation(
            required=bool(triggers),
            recharacterization_required=bool(changes),
            triggers=tuple(triggers),
            inconclusive_signals=inconclusive,
        )


# ---------------------------------------------------------------- helpers


def _counterfactuals_by_plan(row: ShadowExportRow) -> dict[tuple[str, str], ShadowCounterfactual]:
    """1 tick の counterfactual を `(inference_id, plan_digest)` で引けるようにする。"""
    found: dict[tuple[str, str], ShadowCounterfactual] = {}
    for counterfactual in row.shadow.counterfactuals:
        if (
            counterfactual.controller is not ControllerKind.LEARNED_MPC
            or counterfactual.prediction is None
        ):
            continue
        prediction = counterfactual.prediction
        key = (prediction.inference_id, prediction.plan_digest)
        if key in found:
            raise DriftInputError(
                f"同じ推論・同じ候補 plan の counterfactual が1 tick に2つある: {key[0]}"
            )
        found[key] = counterfactual
    return found


def _check_observation_times(outcome: ShadowOutcome) -> None:
    """照合に使った観測が、**記録された照合の規則の中にある**か（0053 §2.3）。

    証拠の時刻はここから決まるので、許容幅の外の観測時刻を置けると、宣言された変更より
    前の証拠を後ろへずらして数えさせたり、trend の bucket を並べ替えたりできる。
    照合器が置ける値の範囲は決まっているので、範囲の外は入力の誤りとして拒む。
    """
    for match in outcome.matches:
        observed_ts_ms = match.observed_ts_ms
        if observed_ts_ms is None:
            continue
        if observed_ts_ms <= outcome.input_action_ts_ms:
            raise DriftInputError(
                f"予測の action より前の観測が証拠になっている: "
                f"observed_ts_ms={observed_ts_ms}; action={outcome.input_action_ts_ms}"
            )
        if abs(observed_ts_ms - match.expected_ts_ms) > outcome.match_tolerance_ms:
            raise DriftInputError(
                f"照合に使った観測が許容幅の外にある: observed_ts_ms={observed_ts_ms}; "
                f"expected_ts_ms={match.expected_ts_ms}; "
                f"tolerance_ms={outcome.match_tolerance_ms}"
            )


def _evidence_time(outcome: ShadowOutcome) -> int:
    """証拠の時刻。**処理した時刻は使わない**（決定記録 0050 §2.2 / 0053 §2.3）。

    照合できた出力があれば、使った観測のうち最も新しい時刻。1つも照合できなければ
    照合期限（最後の期待時刻 + 許容幅）とする。
    """
    observed = [
        match.observed_ts_ms for match in outcome.matches if match.observed_ts_ms is not None
    ]
    if observed:
        return max(observed)
    return max(match.expected_ts_ms for match in outcome.matches) + outcome.match_tolerance_ms


def _verdict_for(value: float, warning: float, degraded: float) -> DriftVerdict:
    if value >= degraded:
        return DriftVerdict.DEGRADED
    if value >= warning:
        return DriftVerdict.WARNING
    return DriftVerdict.OK


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _counted_reasons(counts: Mapping[str, int]) -> tuple[DriftReasonCount, ...]:
    return tuple(DriftReasonCount(code=code, count=count) for code, count in sorted(counts.items()))


def _source_counts(counts: Mapping[str, int]) -> tuple[DriftSourceCount, ...]:
    return tuple(
        DriftSourceCount(source=source, count=count) for source, count in sorted(counts.items())
    )


def _cell_name(bins: Iterable[int]) -> str:
    return "cell:" + ",".join(str(index) for index in bins)


def _evidence_sha256(evidence: DriftEvidence) -> str:
    """数えた証拠そのものの digest。**同じ証拠なら同じ値**になる。"""
    payload = json.dumps(
        {
            "shadow": [row.model_dump(mode="json") for row in evidence.shadow],
            "inputs": [observed.model_dump(mode="json") for observed in evidence.inputs],
            "changes": [change.model_dump(mode="json") for change in evidence.changes],
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "DriftConfigError",
    "DriftDetector",
    "DriftEvidence",
    "DriftInputError",
]
