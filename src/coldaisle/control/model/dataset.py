"""Learned Thermal Model 用 dataset schema。#83

このモジュールはデータの意味と時刻の不変条件だけを持つ。SQLite からの組み立てと
artifact の書き出しは composition root の :mod:`coldaisle.dataset` が担当する。

window / sampling period / horizon / target alignment tolerance は実測で決める値である。
そのため ``DatasetSpec`` は既定値を一切持たず、収集ごとに明示させる。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
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
        _validate_common_spec_shape(self)
        return self


def _validate_common_spec_shape(spec: DatasetSpec | DatasetSpecV2) -> None:
    """v1 / v2 の spec に共通の形の検査（0031 §2.6）。"""
    if spec.window_ms % spec.sample_period_ms:
        raise ValueError("window_ms は sample_period_ms の整数倍でなければならない")
    if any(horizon <= 0 for horizon in spec.horizons_ms):
        raise ValueError("horizon は正でなければならない")
    if tuple(sorted(set(spec.horizons_ms))) != spec.horizons_ms:
        raise ValueError("horizons_ms は重複なしの昇順でなければならない")
    if spec.target_tolerance_ms >= spec.horizons_ms[0]:
        raise ValueError("target_tolerance_ms は最短 horizon より小さくなければならない")
    if len(set(spec.feature_metrics)) != len(spec.feature_metrics):
        raise ValueError("feature_metrics が重複している")
    if len(set(spec.target_metrics)) != len(spec.target_metrics):
        raise ValueError("target_metrics が重複している")


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
        _check_causal_times(self)
        return self


def _check_causal_times(example: DatasetExample | DatasetExampleV2) -> None:
    """window は action 以前、target は action より後という v1 / v2 共通の時刻の不変条件。"""
    if example.history_start_ms >= example.action_ts_ms:
        raise ValueError("history は action より前から始まらなければならない")
    if example.label_end_ms <= example.action_ts_ms:
        raise ValueError("label_end は action より後でなければならない")
    if example.window[0].ts_ms != example.history_start_ms:
        raise ValueError("window の先頭と history_start_ms が一致しない")
    if example.window[-1].ts_ms != example.action_ts_ms:
        raise ValueError("window は action 時刻で終わらなければならない")
    frame_times = tuple(frame.ts_ms for frame in example.window)
    if tuple(sorted(set(frame_times))) != frame_times:
        raise ValueError("window の時刻は重複なしの昇順でなければならない")
    if any(
        source_ts is not None and source_ts > frame.ts_ms
        for frame in example.window
        for source_ts in frame.source_ts_ms.values()
    ):
        raise ValueError("window に各frameより後の観測を入れない")
    if any(target.expected_ts_ms <= example.action_ts_ms for target in example.targets):
        raise ValueError("target は action より後でなければならない")
    if any(
        source_ts is not None and source_ts <= example.action_ts_ms
        for target in example.targets
        for source_ts in target.source_ts_ms.values()
    ):
        raise ValueError("target に action以前の観測を入れない")


def examples_jsonl_bytes(
    examples: tuple[DatasetExample, ...] | tuple[DatasetExampleV2, ...],
) -> bytes:
    """artifactのcanonicalなexamples JSON Lines表現（v1 / v2 共通）。"""
    return b"".join((example.model_dump_json() + "\n").encode() for example in examples)


def examples_sha256(examples: tuple[DatasetExample, ...] | tuple[DatasetExampleV2, ...]) -> str:
    """manifestへ保存するcanonical examplesのSHA-256。"""
    digest = hashlib.sha256()
    for example in examples:
        digest.update((example.model_dump_json() + "\n").encode())
    return digest.hexdigest()


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
        _check_anchors_and_observations(self.examples)
        spec = self.manifest.spec
        for example in self.examples:
            _check_example_against_run_and_spec(
                example,
                runs[example.source_run_id],
                spec,
                expected_label_end_ms=(
                    example.action_ts_ms + spec.horizons_ms[-1] + spec.target_tolerance_ms
                ),
                # v1 は期待時刻の前後どちらの観測も許容誤差内で採る（0031 §2.2）
                target_source_in_range=lambda source_ts, expected_ts: (
                    abs(source_ts - expected_ts) <= spec.target_tolerance_ms
                ),
            )
        if self.manifest.examples_sha256 != examples_sha256(self.examples):
            raise ValueError("manifest の examples_sha256 と実データが一致しない")
        return self


def _check_anchors_and_observations(
    examples: tuple[DatasetExample, ...] | tuple[DatasetExampleV2, ...],
) -> None:
    """同じ anchor の複製と、同じ観測の食い違いを dataset 全体で拒否する（v1 / v2 共通。0103）。

    anchor は ``(source_run_id, action_ts_ms, control_tick_id)`` で一意にする（0103 §2.1）。
    ``control_traces`` の主キー ``(ts_ms, tick_id)`` に run を足したもので、v1 では同じ時刻の
    別の tick も、再起動で振り直された同じ ``tick_id`` も正当にありうるため、どちらも外さない。

    ``(source_run_id, metric, source_ts_ms)`` は保存された1つの読み取りを指す（``readings`` の
    主キー）ので、全 example の window と target を通して ``(value, quality, missing_mask)`` を
    一致させる（0103 §2.2）。``stale_mask`` は frame の時刻で正当に変わるので照合しない。
    """
    anchors: set[tuple[str, int, int]] = set()
    observations: dict[tuple[str, str, int], tuple[float | None, Quality | None, bool]] = {}
    for example in examples:
        anchor = (example.source_run_id, example.action_ts_ms, example.control_tick_id)
        if anchor in anchors:
            raise ValueError(
                "同じ anchor（source_run_id・action_ts_ms・control_tick_id）の"
                "example が重複している"
            )
        anchors.add(anchor)
        frames: tuple[WindowFrame | TargetFrame, ...] = (*example.window, *example.targets)
        for frame in frames:
            for metric, source_ts in frame.source_ts_ms.items():
                if source_ts is None:
                    continue
                cell = (frame.values[metric], frame.quality[metric], frame.missing_mask[metric])
                seen = observations.setdefault((example.source_run_id, metric, source_ts), cell)
                if seen != cell:
                    raise ValueError(
                        "同じ観測（metric・source_ts_ms）の value・quality・missing_mask が"
                        "食い違っている"
                    )


def _check_example_against_run_and_spec(
    example: DatasetExample | DatasetExampleV2,
    run: SourceRun,
    spec: DatasetSpec | DatasetSpecV2,
    *,
    expected_label_end_ms: int,
    target_source_in_range: Callable[[int, int], bool],
) -> None:
    """window / target を spec と source run に照らす v1 / v2 共通の検査。

    target の観測時刻の許容範囲だけが版で違う（v2 は期待時刻以前だけ。0087 §2.4）ので、
    呼び出し側が ``target_source_in_range(source_ts, expected_ts)`` で渡す。
    """
    expected_features = set(spec.feature_metrics)
    expected_targets = set(spec.target_metrics)
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
    if example.label_end_ms != expected_label_end_ms:
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
                or not target_source_in_range(source_ts, target.expected_ts_ms)
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
    train, validation, test, purged = _split_by_time(
        examples, validation_start_ms=validation_start_ms, test_start_ms=test_start_ms
    )
    return DatasetSplit(train=train, validation=validation, test=test, purged=purged)


def _split_by_time[E: (DatasetExample, DatasetExampleV2)](
    examples: tuple[E, ...], *, validation_start_ms: int, test_start_ms: int
) -> tuple[tuple[E, ...], tuple[E, ...], tuple[E, ...], tuple[E, ...]]:
    """v1 / v2 共通の purged chronological split（0031 §2.5）。"""
    if validation_start_ms < 0 or test_start_ms <= validation_start_ms:
        raise ValueError("split境界は 0 <= validation_start_ms < test_start_ms とする")
    train: list[E] = []
    validation: list[E] = []
    test: list[E] = []
    purged: list[E] = []
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
    return tuple(train), tuple(validation), tuple(test), tuple(purged)


# ---------------------------------------------------------------- Thermal Dataset v2
#
# 決定記録 0079 §2.9 の段 1 と 0087。v1 を v2 として読み替えない（0087 §2.8）ため、v2 は
# ``schema_version`` 2 の別の型とし、v1 の型と意味は変えない。

DATASET_V2_SCHEMA_VERSION: Literal[2] = 2
"""Thermal Dataset v2 の版。action 列（0087）を持つ。"""

DemandValue = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class DatasetSpecV2(_Frozen):
    """Dataset v2 を組み立てるための明示的な設定。**どの欄も既定値を持たない**（0031 §2.6）。

    action の格子（``action_step_ms`` × ``action_steps``）と action の鮮度
    （``action_stale_after_ms``）は実データの後に選ぶ値である
    （0087 §2.2 / §2.4、AGENTS.md ルール9）。
    ``action_stale_after_ms`` は Telemetry の ``stale_after_ms`` と共用しない。
    """

    window_ms: int = Field(gt=0)
    sample_period_ms: int = Field(gt=0)
    horizons_ms: tuple[int, ...] = Field(min_length=1)
    target_tolerance_ms: int = Field(ge=0)
    stale_after_ms: int = Field(gt=0)
    feature_metrics: tuple[MetricName, ...] = Field(min_length=1)
    target_metrics: tuple[MetricName, ...] = Field(min_length=1)
    action_step_ms: int = Field(gt=0)
    action_steps: int = Field(gt=0)
    action_stale_after_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        _validate_common_spec_shape(self)
        # horizon は格子の上、格子の終端は最大の horizon（0087 §2.4）
        if any(horizon % self.action_step_ms for horizon in self.horizons_ms):
            raise ValueError("各 horizon は action_step_ms の整数倍でなければならない")
        if self.action_steps * self.action_step_ms != self.horizons_ms[-1]:
            raise ValueError(
                "action_steps × action_step_ms は最大の horizon と等しくなければならない"
            )
        return self


class ActionStepV2(_Frozen):
    """step ``k`` の action（0087 §2.1）。

    区間 ``[action_ts_ms + k × step_ms, action_ts_ms + (k + 1) × step_ms)`` に掛かっていた
    effective demand。値は区間の開始時刻 ``ts_ms`` 以前で直近の ControlTick から取る（as-of）。
    元にした tick の時刻と ``tick_id`` を step ごとに持つ（1つの tick が全 zone の値を持つ）。
    ``ActionPlan.steps[k]``（``offset_ms = step_ms × (k + 1)`` は区間の終端）と
    同じ区間である（0087 §2.5）。
    """

    step: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    source_ts_ms: int = Field(ge=0)
    source_tick_id: int = Field(ge=0)
    effective_demand: PerZone[DemandValue]

    @model_validator(mode="after")
    def _source_is_as_of(self) -> Self:
        if self.source_ts_ms > self.ts_ms:
            raise ValueError("step の値に格子の時刻より後の ControlTick を使わない")
        return self


class PriorAction(_Frozen):
    """v2 の anchor action（0087 §2.7）。

    anchor の tick の時刻より**厳密に前**で直近の ControlTick の effective demand。
    runtime の anchor 推論が受け取る「いま掛かっている effective」（その tick が demand を
    決める前の値）と意味を揃える。
    """

    source_ts_ms: int = Field(ge=0)
    source_tick_id: int = Field(ge=0)
    effective_demand: PerZone[DemandValue]


class ActionExclusionCounts(_Frozen):
    """action の規則で作らなかった example の件数を理由ごとに持つ（0087 §2.2）。

    1つの example が複数の理由に当たるときは、欄の順（鮮度 → 連続 → 区間内の変化 → 再起動 → 欠番）で
    最初の理由だけに数える。合計は除いた example の数と一致する。**どの欄も既定値を持たない。**
    """

    stale: int = Field(ge=0)
    """鮮度: as-of の tick・``prior_action`` の元の tick が古い。anchor の前に tick が無い。"""
    discontinuity: int = Field(ge=0)
    """連続: 隣り合う tick・最後の tick から最大の horizon までの差が上限以上。run の末尾。"""
    in_step_change: int = Field(ge=0)
    """区間内の変化: step の区間の中の tick の effective が1つの zone でも step の値と違う。"""
    restart: int = Field(ge=0)
    """再起動: ``seq`` の順に隣り合う tick の ``tick_id`` が減る、または同じ。"""
    tick_id_gap: int = Field(ge=0)
    """欠番: ``seq`` の順に隣り合う tick の ``tick_id`` が 2 以上増える（保存に失敗した tick）。"""


class DatasetExampleV2(_Frozen):
    """1 anchor を基点にした window / action 列 / multi-horizon target（0079 段 1 / 0087）。

    ``action`` と ``context`` は v1 と同じく anchor の tick 自身のもので、分析用に持つ。
    v2 の anchor action は ``prior_action`` である（0087 §2.7）。
    """

    schema_version: Literal[2] = DATASET_V2_SCHEMA_VERSION
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
    prior_action: PriorAction
    action_steps: tuple[ActionStepV2, ...] = Field(min_length=1)
    targets: tuple[TargetFrame, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _times_are_causal(self) -> Self:
        _check_causal_times(self)
        # v2 の target は期待時刻以前の観測だけから採る（0087 §2.4）
        if any(
            source_ts is not None and source_ts > target.expected_ts_ms
            for target in self.targets
            for source_ts in target.source_ts_ms.values()
        ):
            raise ValueError("v2 の target に期待時刻より後の観測を入れない")
        if self.prior_action.source_ts_ms >= self.action_ts_ms:
            raise ValueError("prior_action は anchor の tick より厳密に前の tick から取る")
        # prior_action の元の tick から tick_id はちょうど 1 ずつ増える（0087 §2.2）ので、
        # prior_action の元の tick は anchor の tick の直前の tick である
        if self.prior_action.source_tick_id != self.control_tick_id - 1:
            raise ValueError(
                "prior_action の元の tick_id は anchor の tick_id − 1 でなければならない"
            )
        if tuple(step.step for step in self.action_steps) != tuple(range(len(self.action_steps))):
            raise ValueError("action_steps の step 番号は 0 から連続していなければならない")
        first = self.action_steps[0]
        if (first.ts_ms, first.source_ts_ms, first.source_tick_id) != (
            self.action_ts_ms,
            self.action_ts_ms,
            self.control_tick_id,
        ):
            raise ValueError("step 0 は anchor の tick 自身でなければならない")
        if first.effective_demand != PerZone(
            front=self.action.front.effective_demand,
            rear=self.action.rear.effective_demand,
            top=self.action.top.effective_demand,
        ):
            raise ValueError("step 0 の値は anchor の tick の effective と一致しなければならない")
        source_times = tuple(step.source_ts_ms for step in self.action_steps)
        if tuple(sorted(source_times)) != source_times:
            raise ValueError("action_steps の元の tick の時刻が逆行している")
        # 同じ元の tick（同じ ts_ms）を複数の step が使うなら、tick_id も値も同じにする。
        # 1つの tick は1つの effective しか持たない（0087 §2.1）
        # tick_id は ts_ms の順にちょうど 1 ずつ増える（0087 §2.2）ので、元の tick の時刻が進めば
        # tick_id も進む
        for earlier, later in zip(self.action_steps, self.action_steps[1:], strict=False):
            if (later.source_ts_ms > earlier.source_ts_ms) != (
                later.source_tick_id > earlier.source_tick_id
            ):
                raise ValueError("action_steps の元の tick の tick_id が時刻の順と一致しない")
        sources: dict[int, tuple[int, PerZone[float]]] = {}
        for step in self.action_steps:
            seen = sources.setdefault(
                step.source_ts_ms, (step.source_tick_id, step.effective_demand)
            )
            if seen != (step.source_tick_id, step.effective_demand):
                raise ValueError("同じ元の tick を使う step の tick_id か値が食い違っている")
        return self


class DatasetManifestV2(_Frozen):
    """Dataset v2 の再生成条件と、action の規則で除いた件数。"""

    schema_name: Literal["coldaisle.thermal_dataset"] = "coldaisle.thermal_dataset"
    schema_version: Literal[2] = DATASET_V2_SCHEMA_VERSION
    spec: DatasetSpecV2
    source_runs: tuple[SourceRun, ...] = Field(min_length=1)
    telemetry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    examples_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    example_count: int = Field(ge=0)
    excluded: ActionExclusionCounts


class ThermalDatasetV2(_Frozen):
    """manifest と同じ版で検証済みの Dataset v2。"""

    manifest: DatasetManifestV2
    examples: tuple[DatasetExampleV2, ...]

    @model_validator(mode="after")
    def _manifest_matches_examples(self) -> Self:
        if self.manifest.example_count != len(self.examples):
            raise ValueError("manifest の example_count と実データ件数が一致しない")
        runs = {run.run_id: run for run in self.manifest.source_runs}
        if len(runs) != len(self.manifest.source_runs):
            raise ValueError("manifest のsource run IDが重複している")
        if any(example.source_run_id not in runs for example in self.examples):
            raise ValueError("manifest に無い source_run_id が使われている")
        if len({example.example_id for example in self.examples}) != len(self.examples):
            raise ValueError("example_idが重複している")
        _check_anchors_and_observations(self.examples)
        spec = self.manifest.spec
        # 1つの ControlTick は1つの tick_id と effective しか持たない（0087 §2.1）。
        # 重なる example が同じ tick を使うので、run と時刻をキーに全 example を通して照合する
        ticks: dict[tuple[str, int], tuple[int, PerZone[float]]] = {}
        for example in self.examples:
            anchor = PerZone(
                front=example.action.front.effective_demand,
                rear=example.action.rear.effective_demand,
                top=example.action.top.effective_demand,
            )
            uses = (
                (example.action_ts_ms, example.control_tick_id, anchor),
                (
                    example.prior_action.source_ts_ms,
                    example.prior_action.source_tick_id,
                    example.prior_action.effective_demand,
                ),
                *(
                    (step.source_ts_ms, step.source_tick_id, step.effective_demand)
                    for step in example.action_steps
                ),
            )
            for ts_ms, tick_id, demands in uses:
                seen = ticks.setdefault((example.source_run_id, ts_ms), (tick_id, demands))
                if seen != (tick_id, demands):
                    raise ValueError("同じ元の tick の tick_id か値が example の間で食い違っている")
        for example in self.examples:
            run = runs[example.source_run_id]
            _check_example_against_run_and_spec(
                example,
                run,
                spec,
                # v2 の label_end は anchor + 最大の horizon（許容誤差を足さない。0087 §2.4）
                expected_label_end_ms=example.action_ts_ms + spec.horizons_ms[-1],
                target_source_in_range=lambda source_ts, expected_ts: (
                    expected_ts - spec.target_tolerance_ms <= source_ts <= expected_ts
                ),
            )
            _check_action_steps(example, run, spec)
        if self.manifest.examples_sha256 != examples_sha256(self.examples):
            raise ValueError("manifest の examples_sha256 と実データが一致しない")
        return self


def _check_action_steps(example: DatasetExampleV2, run: SourceRun, spec: DatasetSpecV2) -> None:
    """action 列が spec の格子の上にあり、鮮度の上限の中にあることを確かめる。

    0087 §2.1 / §2.2。
    """
    if len(example.action_steps) != spec.action_steps:
        raise ValueError("action 列の長さが action_steps と一致しない")
    for step in example.action_steps:
        if step.ts_ms != example.action_ts_ms + step.step * spec.action_step_ms:
            raise ValueError("action 列の時刻が spec の格子と一致しない")
        if not run.start_ms <= step.source_ts_ms < run.end_ms:
            raise ValueError("action の元の tick が source run の期間外にある")
        if step.ts_ms - step.source_ts_ms >= spec.action_stale_after_ms:
            raise ValueError("action の元の tick が action_stale_after_ms 以上古い")
    prior = example.prior_action
    if not run.start_ms <= prior.source_ts_ms < run.end_ms:
        raise ValueError("prior_action の元の tick が source run の期間外にある")
    if example.action_ts_ms - prior.source_ts_ms >= spec.action_stale_after_ms:
        raise ValueError("prior_action の元の tick が action_stale_after_ms 以上古い")


class DatasetSplitV2(_Frozen):
    """Dataset v2 の purged chronological split の結果（0031 §2.5。label_end は 0087 §2.4）。"""

    train: tuple[DatasetExampleV2, ...]
    validation: tuple[DatasetExampleV2, ...]
    test: tuple[DatasetExampleV2, ...]
    purged: tuple[DatasetExampleV2, ...]


def split_temporally_v2(
    examples: tuple[DatasetExampleV2, ...], *, validation_start_ms: int, test_start_ms: int
) -> DatasetSplitV2:
    """v1 の :func:`split_temporally` と同じ規則で Dataset v2 を分ける。"""
    train, validation, test, purged = _split_by_time(
        examples, validation_start_ms=validation_start_ms, test_start_ms=test_start_ms
    )
    return DatasetSplitV2(train=train, validation=validation, test=test, purged=purged)


def reject_calibration_changes(
    examples: tuple[DatasetExampleV2, ...], calibration_change_ts_ms: tuple[int, ...]
) -> None:
    """全 example の期間の中に較正の変更があれば、Dataset v2 の生成を拒否する（0087 §2.6）。

    期間は ``[history_start_ms の最小, label_end_ms の最大]``（両端を含む）。split の各集合の期間を
    すべて含むので、0079 §2.3 の「全 split を通した期間」の条件を満たす。

    宣言された変更の型（``control/drift``）は受け取らない。``control/drift`` は ``control/model`` を
    import しているので、ここから import すると循環する。合成の起点が ``calibration_changed`` の
    時刻だけを取り出して渡す。
    """
    if not examples:
        return
    start_ms = min(example.history_start_ms for example in examples)
    end_ms = max(example.label_end_ms for example in examples)
    crossing = sorted(ts_ms for ts_ms in calibration_change_ts_ms if start_ms <= ts_ms <= end_ms)
    if crossing:
        raise ValueError(
            f"Dataset v2 の期間の中に較正の変更がある（窓を分けて作り直すのは人の判断）: {crossing}"
        )
