"""Learned Thermal Model 用 dataset schema。#83

このモジュールはデータの意味と時刻の不変条件だけを持つ。SQLite からの組み立てと
artifact の書き出しは composition root の :mod:`coldaisle.dataset` が担当する。

window / sampling period / horizon / target alignment tolerance は実測で決める値である。
そのため ``DatasetSpec`` は既定値を一切持たず、収集ごとに明示させる。
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.schema import (
    AuthorityStage,
    ControllerKind,
    OperatingMode,
    PerZone,
    SafetyState,
)
from coldaisle.store.models import Quality

DATASET_SCHEMA_VERSION: Literal[1] = 1
"""Thermal dataset schema の版。フィールドの意味を変えたら上げる。"""

MetricName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,3}$")]
RunAlias = Annotated[str, Field(pattern=r"^run-[0-9a-f]{32}$")]
SourceAlias = Annotated[str, Field(pattern=r"^source-[0-9a-f]{32}$")]
FiniteValue = Annotated[float, Field(allow_inf_nan=False)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class DatasetSourceKind(StrEnum):
    """元データを得た経路。実機と Replay / Mock を混同しないための識別子。"""

    SERIAL = "serial"
    REPLAY = "replay"
    MOCK = "mock"
    IMPORT = "import"


class DatasetWorkloadRegime(StrEnum):
    """dataset v1が受け付ける観測済み負荷区分。#87のControlTick値と対応する。

    dataset schemaはcontrolの型から独立に版管理するため別のenumに持つ。ただし
    `WorkloadRegime`と値の集合を一致させることをテストで固定し、controlへ区分が
    増えたのにdatasetだけが読めない、というずれを起こさない。
    """

    IDLE = "idle"
    TRANSIENT_CPU = "transient_cpu"
    TRANSIENT_GPU = "transient_gpu"
    TRANSIENT_CPU_GPU = "transient_cpu_gpu"
    SUSTAINED_CPU = "sustained_cpu"
    SUSTAINED_GPU = "sustained_gpu"
    SUSTAINED_CPU_GPU = "sustained_cpu_gpu"
    COOLDOWN = "cooldown"
    UNKNOWN = "unknown"


class DatasetSpec(_Frozen):
    """1つの dataset artifact を組み立てるための明示的な設定。"""

    window_ms: int = Field(gt=0)
    sample_period_ms: int = Field(gt=0)
    horizons_ms: tuple[int, ...] = Field(min_length=1)
    target_tolerance_ms: int = Field(ge=0)
    stale_after_ms: int = Field(gt=0)
    feature_metrics: tuple[MetricName, ...] = Field(min_length=1)
    target_metrics: tuple[MetricName, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.window_ms % self.sample_period_ms:
            raise ValueError("window_ms は sample_period_ms の整数倍でなければならない")
        if any(horizon <= 0 for horizon in self.horizons_ms):
            raise ValueError("horizon は正でなければならない")
        if tuple(sorted(set(self.horizons_ms))) != self.horizons_ms:
            raise ValueError("horizons_ms は重複なしの昇順でなければならない")
        if self.target_tolerance_ms >= self.horizons_ms[0]:
            raise ValueError("target_tolerance_ms は最短 horizon より小さくなければならない")
        if len(set(self.feature_metrics)) != len(self.feature_metrics):
            raise ValueError("feature_metrics が重複している")
        if len(set(self.target_metrics)) != len(self.target_metrics):
            raise ValueError("target_metrics が重複している")
        return self


class SourceRun(_Frozen):
    """元データを一意に追跡するrun。

    ``run_id`` / ``source_refs`` は利用者が払い出した公開用の不透明aliasだけを持つ。
    ファイル名、hostname、IP address、ROM code等の実機識別子はartifactへ入れない。
    """

    run_id: RunAlias
    kind: DatasetSourceKind
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    source_refs: tuple[SourceAlias, ...] = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid_range_and_refs(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("source run の end_ms は start_ms より後でなければならない")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("source_refs が重複している")
        return self


VALUELESS_QUALITIES = frozenset({Quality.MISSING, Quality.SUSPECT})
"""観測時刻を持つのに値が無くてよいquality。

`missing`は欠測。`suspect`は非有限値（`inf`等）のように値を保存できない疑わしい観測で、
決定記録 0003 §2.8により値は落としqualityだけを残す。どちらも学習には使えないため
`missing_mask=true`とする。値のある`suspect`（範囲外等）は`missing_mask=false`のまま。
"""


def _check_observed_cell(value: float | None, quality: Quality, missing: bool, where: str) -> None:
    """観測時刻を持つcellの value / quality / missing_mask の整合を検証する。

    `missing_mask`は「値が使えない」ことを表し、`value is None`と同値にする。
    """
    if missing != (value is None):
        raise ValueError(f"{where}のmissing_maskはvalue=nullと一致しなければならない")
    if value is None and quality not in VALUELESS_QUALITIES:
        raise ValueError(f"{where}でvalue=nullにできるのはquality=missing/suspectだけ")
    if quality is Quality.MISSING and value is not None:
        raise ValueError(f"{where}のquality=missingはvalue=nullでなければならない")


class WindowFrame(_Frozen):
    """window 内の1時点。値は直近観測をas-ofで保持し、元時刻とmaskを併記する。"""

    ts_ms: int = Field(ge=0)
    values: dict[str, FiniteValue | None]
    source_ts_ms: dict[str, int | None]
    quality: dict[str, Quality | None]
    missing_mask: dict[str, bool]
    stale_mask: dict[str, bool]

    @model_validator(mode="after")
    def _masks_match_cells(self) -> Self:
        mappings = (
            self.source_ts_ms,
            self.quality,
            self.missing_mask,
            self.stale_mask,
        )
        if any(set(mapping) != set(self.values) for mapping in mappings):
            raise ValueError("window cellのmetric集合が一致しない")
        for metric, value in self.values.items():
            source_ts = self.source_ts_ms[metric]
            quality = self.quality[metric]
            missing = self.missing_mask[metric]
            stale = self.stale_mask[metric]
            if source_ts is None:
                if value is not None or quality is not None or not missing or stale:
                    raise ValueError(
                        "未観測cellはvalue/qualityなし・missing=true・stale=falseにする"
                    )
                continue
            if quality is None:
                raise ValueError("観測時刻を持つcellにはqualityが要る")
            _check_observed_cell(value, quality, missing, "window cell")
            if quality is Quality.STALE and not stale:
                raise ValueError("quality=staleのcellはstale_maskを立てる")
        return self


class TargetFrame(_Frozen):
    """action より後の1 horizon に対応する教師値。"""

    horizon_ms: int = Field(gt=0)
    expected_ts_ms: int = Field(ge=0)
    values: dict[str, FiniteValue | None]
    source_ts_ms: dict[str, int | None]
    quality: dict[str, Quality | None]
    missing_mask: dict[str, bool]

    @model_validator(mode="after")
    def _mask_matches_cells(self) -> Self:
        mappings = (self.source_ts_ms, self.quality, self.missing_mask)
        if any(set(mapping) != set(self.values) for mapping in mappings):
            raise ValueError("target cellのmetric集合が一致しない")
        for metric, value in self.values.items():
            source_ts = self.source_ts_ms[metric]
            quality = self.quality[metric]
            missing = self.missing_mask[metric]
            if source_ts is None:
                if value is not None or quality is not None or not missing:
                    raise ValueError("未観測targetはvalue/qualityなし・missing=trueにする")
                continue
            if quality is None:
                raise ValueError("観測時刻を持つtargetにはqualityが要る")
            _check_observed_cell(value, quality, missing, "target cell")
        return self


class ActionZone(_Frozen):
    """zone ごとの提案値と実際に適用したaction。"""

    requested_demand: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    effective_demand: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    bound_by: str
    controller_reason: str
    override_reasons: tuple[str, ...]


class ActionContext(_Frozen):
    """action の解釈に必要なControlTick context。

    workload regime は #87 の producer が未接続でも schema 上は欠測を明示できる。
    """

    operating_mode: OperatingMode
    authority_stage: AuthorityStage
    active_controller: ControllerKind | None
    safety_state: SafetyState
    fallback_active: bool
    supervisor_policy: str | None
    workload_regime: DatasetWorkloadRegime | None
    regime_confidence: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    fault_codes: tuple[str, ...]

    @model_validator(mode="after")
    def _regime_and_confidence_are_a_pair(self) -> Self:
        if (self.workload_regime is None) != (self.regime_confidence is None):
            raise ValueError("workload_regime と regime_confidence は一緒に記録する")
        return self


class DatasetExample(_Frozen):
    """1 action を基点にした window / action / multi-horizon target。"""

    schema_version: Literal[1] = DATASET_SCHEMA_VERSION
    example_id: str
    source_run_id: str
    history_start_ms: int = Field(ge=0)
    action_ts_ms: int = Field(ge=0)
    label_end_ms: int = Field(ge=0)
    control_tick_id: int = Field(ge=0)
    control_schema_version: int = Field(ge=1)
    window: tuple[WindowFrame, ...] = Field(min_length=2)
    action: PerZone[ActionZone]
    context: ActionContext
    targets: tuple[TargetFrame, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _times_are_causal(self) -> Self:
        if self.history_start_ms >= self.action_ts_ms:
            raise ValueError("history は action より前から始まらなければならない")
        if self.label_end_ms <= self.action_ts_ms:
            raise ValueError("label_end は action より後でなければならない")
        if self.window[0].ts_ms != self.history_start_ms:
            raise ValueError("window の先頭と history_start_ms が一致しない")
        if self.window[-1].ts_ms != self.action_ts_ms:
            raise ValueError("window は action 時刻で終わらなければならない")
        frame_times = tuple(frame.ts_ms for frame in self.window)
        if tuple(sorted(set(frame_times))) != frame_times:
            raise ValueError("window の時刻は重複なしの昇順でなければならない")
        if any(
            source_ts is not None and source_ts > frame.ts_ms
            for frame in self.window
            for source_ts in frame.source_ts_ms.values()
        ):
            raise ValueError("window に各frameより後の観測を入れない")
        if any(target.expected_ts_ms <= self.action_ts_ms for target in self.targets):
            raise ValueError("target は action より後でなければならない")
        if any(
            source_ts is not None and source_ts <= self.action_ts_ms
            for target in self.targets
            for source_ts in target.source_ts_ms.values()
        ):
            raise ValueError("target に action以前の観測を入れない")
        return self


def examples_jsonl_bytes(examples: tuple[DatasetExample, ...]) -> bytes:
    """v1 artifactのcanonicalなexamples JSON Lines表現。"""
    return b"".join((example.model_dump_json() + "\n").encode() for example in examples)


def examples_sha256(examples: tuple[DatasetExample, ...]) -> str:
    """manifestへ保存するcanonical examplesのSHA-256。"""
    return hashlib.sha256(examples_jsonl_bytes(examples)).hexdigest()


class DatasetManifest(_Frozen):
    """artifact 全体の再生成条件。"""

    schema_name: Literal["coldaisle.thermal_dataset"] = "coldaisle.thermal_dataset"
    schema_version: Literal[1] = DATASET_SCHEMA_VERSION
    spec: DatasetSpec
    source_runs: tuple[SourceRun, ...] = Field(min_length=1)
    telemetry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    examples_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    example_count: int = Field(ge=0)


class ThermalDataset(_Frozen):
    """manifest と同じversionで検証済みの学習例集合。"""

    manifest: DatasetManifest
    examples: tuple[DatasetExample, ...]

    @model_validator(mode="after")
    def _manifest_matches_examples(self) -> Self:
        if self.manifest.example_count != len(self.examples):
            raise ValueError("manifest の example_count と実データ件数が一致しない")
        runs = {run.run_id: run for run in self.manifest.source_runs}
        if len(runs) != len(self.manifest.source_runs):
            raise ValueError("manifest のsource run IDが重複している")
        run_ids = set(runs)
        if any(example.source_run_id not in run_ids for example in self.examples):
            raise ValueError("manifest に無い source_run_id が使われている")
        example_ids = {example.example_id for example in self.examples}
        if len(example_ids) != len(self.examples):
            raise ValueError("example_idが重複している")
        spec = self.manifest.spec
        expected_features = set(self.manifest.spec.feature_metrics)
        expected_targets = set(self.manifest.spec.target_metrics)
        for example in self.examples:
            run = runs[example.source_run_id]
            if example.history_start_ms < run.start_ms or example.label_end_ms >= run.end_ms:
                raise ValueError("学習例がsource runの期間外を参照している")
            if example.history_start_ms != example.action_ts_ms - spec.window_ms:
                raise ValueError("学習例のwindow幅がspecと一致しない")
            expected_frame_times = tuple(
                range(
                    example.history_start_ms,
                    example.action_ts_ms + 1,
                    spec.sample_period_ms,
                )
            )
            if tuple(frame.ts_ms for frame in example.window) != expected_frame_times:
                raise ValueError("学習例のsampling periodがspecと一致しない")
            if tuple(target.horizon_ms for target in example.targets) != spec.horizons_ms:
                raise ValueError("学習例のhorizon集合がspecと一致しない")
            if any(
                target.expected_ts_ms != example.action_ts_ms + target.horizon_ms
                for target in example.targets
            ):
                raise ValueError("targetの期待時刻がaction + horizonと一致しない")
            if example.label_end_ms != (
                example.action_ts_ms + spec.horizons_ms[-1] + spec.target_tolerance_ms
            ):
                raise ValueError("label_endがtarget探索範囲と一致しない")
            for frame in example.window:
                if not run.start_ms <= frame.ts_ms < run.end_ms:
                    raise ValueError("window frameがsource runの期間外にある")
                for metric, source_ts in frame.source_ts_ms.items():
                    if source_ts is not None and not run.start_ms <= source_ts < run.end_ms:
                        raise ValueError("window観測時刻がsource runの期間外にある")
                    quality = frame.quality[metric]
                    expected_stale = source_ts is not None and (
                        quality is Quality.STALE or frame.ts_ms - source_ts >= spec.stale_after_ms
                    )
                    if frame.stale_mask[metric] != expected_stale:
                        raise ValueError("window cellのstale_maskがspecの鮮度と一致しない")
                mappings = (
                    frame.values,
                    frame.source_ts_ms,
                    frame.quality,
                    frame.missing_mask,
                    frame.stale_mask,
                )
                if any(set(mapping) != expected_features for mapping in mappings):
                    raise ValueError("window frame のmetric集合がspecと一致しない")
            for target in example.targets:
                if not run.start_ms <= target.expected_ts_ms < run.end_ms:
                    raise ValueError("target期待時刻がsource runの期間外にある")
                if any(
                    source_ts is not None
                    and (
                        not run.start_ms <= source_ts < run.end_ms
                        or abs(source_ts - target.expected_ts_ms) > spec.target_tolerance_ms
                    )
                    for source_ts in target.source_ts_ms.values()
                ):
                    raise ValueError("target観測がrun期間または許容時刻差の外にある")
                target_mappings = (
                    target.values,
                    target.source_ts_ms,
                    target.quality,
                    target.missing_mask,
                )
                if any(set(mapping) != expected_targets for mapping in target_mappings):
                    raise ValueError("target frame のmetric集合がspecと一致しない")
            for metric in expected_features:
                source_times = tuple(
                    source_ts
                    for frame in example.window
                    if (source_ts := frame.source_ts_ms[metric]) is not None
                )
                if tuple(sorted(source_times)) != source_times:
                    raise ValueError("window観測時刻がmetric内で逆行している")
            for metric in expected_targets:
                target_times = tuple(
                    source_ts
                    for target in example.targets
                    if (source_ts := target.source_ts_ms[metric]) is not None
                )
                if tuple(sorted(target_times)) != target_times:
                    raise ValueError("target観測時刻がmetric内で逆行している")
        if self.manifest.examples_sha256 != examples_sha256(self.examples):
            raise ValueError("manifest の examples_sha256 と実データが一致しない")
        return self


class DatasetSplit(_Frozen):
    """purged chronological split の結果。境界を跨ぐ例は ``purged`` へ置く。"""

    train: tuple[DatasetExample, ...]
    validation: tuple[DatasetExample, ...]
    test: tuple[DatasetExample, ...]
    purged: tuple[DatasetExample, ...]


def split_temporally(
    examples: tuple[DatasetExample, ...], *, validation_start_ms: int, test_start_ms: int
) -> DatasetSplit:
    """時刻境界で分割し、windowまたはlabelが境界を跨ぐ例を除外する。

    ランダムsplitは隣接windowを別集合へ置き、同じ観測をtrainと評価に共有する。
    ここでは例が使う全期間 ``[history_start_ms, label_end_ms]`` を見るため、同じrunの
    観測時刻が境界越しに共有されない。
    """
    if validation_start_ms < 0 or test_start_ms <= validation_start_ms:
        raise ValueError("split境界は 0 <= validation_start_ms < test_start_ms とする")
    train: list[DatasetExample] = []
    validation: list[DatasetExample] = []
    test: list[DatasetExample] = []
    purged: list[DatasetExample] = []
    for example in examples:
        if example.label_end_ms < validation_start_ms:
            train.append(example)
        elif (
            example.history_start_ms >= validation_start_ms and example.label_end_ms < test_start_ms
        ):
            validation.append(example)
        elif example.history_start_ms >= test_start_ms:
            test.append(example)
        else:
            purged.append(example)
    return DatasetSplit(
        train=tuple(train),
        validation=tuple(validation),
        test=tuple(test),
        purged=tuple(purged),
    )
