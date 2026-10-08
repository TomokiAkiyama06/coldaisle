"""Confidence Profile v2 の生成・step ごとの support の照合・同梱 Profile からの判定器（#85）。

決定記録 0079 §2.9 の**段 3** と、0084 のうち段 3 に属する点を実装する。

- :func:`fit_confidence_profile_v2`: 学習結果（段 2 の ``CounterfactualTrainedModel``）と、
  その学習に使った Dataset v2・split から Profile v2 を作る。範囲・欠測・support・action 列の範囲は
  **train だけ**から、residual の基準は **validation** から作る（0050 §2.1）。residual の基準は
  ``hold_effective`` の anchor 推論で作り、held の列が step ごとの support の外にある validation
  example は基準から除いて件数を記録する（0084 §2.1）
- :class:`StepSupportChecker`: step ごと・step の組ごとの support の照合（0084 §2.2）。held の列
  （Profile の residual の基準・runtime の anchor 推論）と、段 4 の候補 plan の照合が
  **同じ関数**を使う。margin も件数の下限も掛けない
- :class:`CounterfactualConfidenceAssessor`: 封をした型 ``RegistryCounterfactualThermalModel`` の
  **同梱 Profile からだけ**作る判定器（0079 §2.4）。別の Profile を渡す引数を持たない。held の列が
  step ごとの support の外なら、既存の構成要素 ``support`` を OOD にする（0084 §2.1）

ここは Demand・PWM・authority を返さず、``control.hardware`` / ``control.safety`` /
``control.reactive`` を import しない（0079 §2.6）。OOD の判定を受けた提案は、既存の Gate が
Fallback にする（0050 §2.3。新しい経路は作らない）。LLM 層へは何も出さない。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from enum import StrEnum
from itertools import pairwise
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.control.config import ModelConfidencePolicy
from coldaisle.control.model.confidence import (
    FAN_SOURCE_PREFIX,
    MAX_MISSING_PATTERNS,
    MAX_SUPPORT_CELLS,
    ConfidenceAssessment,
    ConfidenceProfileSpec,
    MissingPatternCount,
    ResidualDriftMonitor,
    ResidualEvidence,
    ResidualScale,
    SupportAxis,
    SupportCellCount,
    ValueRange,
    _AssessorCore,
    _fan_range,
    _missing_pattern,
    _ProfileBasis,
    _support_cell,
    _usable,
    _value_range,
)
from coldaisle.control.model.counterfactual import (
    ANCHOR_ACTION_RULE,
    ZONE_ORDER,
    ActionCell,
    ActionCellCount,
    ActionCellTransitionCount,
    ActionSupportV2,
    ActionTrajectory,
    AnchorTransitionSupport,
    ConfidenceProfileV2,
    CounterfactualThermalModelArtifact,
    RegistryCounterfactualThermalModel,
    StepActionSupport,
    StepTransitionSupport,
    ThermalActionSchema,
    hold_effective,
    predict_targets,
)
from coldaisle.control.model.counterfactual_training import (
    CounterfactualTrainedModel,
    VerifiedTrainingDatasetArtifactV2,
    assemble_counterfactual_artifact,
)
from coldaisle.control.model.dataset import DatasetExampleV2, DatasetSplitV2, ThermalDatasetV2
from coldaisle.control.model.thermal import (
    ObservedThermalInput,
    ThermalPrediction,
    canonical_sha256,
)
from coldaisle.control.model.training import _example_order, _validate_split, split_sha256
from coldaisle.control.schema import PerZone, Zone
from coldaisle.measurement import Quality


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ---------------------------------------------------------------- cell

_FAN_SOURCES: tuple[str, ...] = tuple(f"{FAN_SOURCE_PREFIX}{zone.value}" for zone in ZONE_ORDER)


def fan_axes(spec: ConfidenceProfileSpec) -> tuple[SupportAxis, ...]:
    """action schema の全 zone の ``fan.<zone>`` 軸を zone の順に返す（0079 §2.5）。

    明示が無ければ作らない（0050 §2.1 のとおり既定値を置かない。候補専用の分け方も持たない）。
    """
    axes = {axis.source: axis for axis in spec.support_axes}
    missing = [source for source in _FAN_SOURCES if source not in axes]
    if missing:
        raise ValueError(f"Profile v2 には全 zone の fan.<zone> 軸が要る: {missing}")
    return tuple(axes[source] for source in _FAN_SOURCES)


def action_cell(demands: PerZone[float], axes: Sequence[SupportAxis]) -> ActionCell:
    """全 zone の demand の組が落ちる cell（Front / Rear / Top の ``fan.<zone>`` 軸の bin）。"""
    front, rear, top = (
        axis.bin_of(demands.get(zone)) for axis, zone in zip(axes, ZONE_ORDER, strict=True)
    )
    return (front, rear, top)


def _delta(before: PerZone[float], after: PerZone[float]) -> PerZone[float]:
    return PerZone(
        front=after.front - before.front,
        rear=after.rear - before.rear,
        top=after.top - before.top,
    )


# ---------------------------------------------------------------- step ごとの support の照合


class SupportViolationKind(StrEnum):
    """action 列が step ごとの support の外にある理由（0079 §2.5 / 0084 §2.2）。"""

    STEP_DEMAND = "step_demand"
    """step ``k`` の zone の demand が step ``k`` の範囲の外。"""
    STEP_CELL = "step_cell"
    """step ``k`` の全 zone の組の cell が step ``k`` の (a) に無い。"""
    ANCHOR_DELTA = "anchor_delta"
    """anchor → 最初の step の zone の変化量が範囲の外。"""
    ANCHOR_CELL = "anchor_cell"
    """anchor の cell → 最初の step の cell の組が (b) に無い。"""
    TRANSITION_DELTA = "transition_delta"
    """step の組 ``(k, k + 1)`` の zone の変化量が、その組の範囲の外。"""
    TRANSITION_CELL = "transition_cell"
    """step の組 ``(k, k + 1)`` の cell の組が、その組の (c) に無い。"""


class StepSupportViolation(_Frozen):
    """照合で最初に外れた点。detail（0084 §2.1 / §2.2）に step・zone・cell を出すための値。

    ``step`` は step の番号（anchor → 最初の step では 0、step の組では ``from_step``）。
    """

    kind: SupportViolationKind
    step: int = Field(ge=0)
    to_step: int | None = Field(default=None, ge=1)
    zone: Zone | None = None
    value: float | None = Field(default=None, allow_inf_nan=False)
    cell: ActionCell | None = None
    source_cell: ActionCell | None = None

    def describe(self) -> str:
        """trace / 失敗理由へ載せる短い文。"""
        if self.kind in (SupportViolationKind.ANCHOR_DELTA, SupportViolationKind.ANCHOR_CELL):
            where = "steps=(anchor,0)"
        elif self.to_step is not None:
            where = f"steps=({self.step},{self.to_step})"
        else:
            where = f"step={self.step}"
        parts = [f"{self.kind.value}", where]
        if self.zone is not None:
            parts.append(f"zone={self.zone.value}")
        if self.value is not None:
            parts.append(f"value={self.value:.6f}")
        if self.source_cell is not None and self.cell is not None:
            parts.append(f"cell={list(self.source_cell)}->{list(self.cell)}")
        elif self.cell is not None:
            parts.append(f"cell={list(self.cell)}")
        return "; ".join(parts)


def _inside(known: ValueRange, value: float) -> bool:
    """観測した min / max の中か（margin を掛けない。観測が無ければどの値も通らない）。"""
    return (
        known.minimum is not None
        and known.maximum is not None
        and (known.minimum <= value <= known.maximum)
    )


class StepSupportChecker:
    """action 列を Profile v2 の step ごとの support に照らす（0084 §2.2）。

    held の列（Profile の residual の基準と runtime の anchor 推論。0084 §2.1）と、段 4 の
    候補 plan と Fallback の requested の照合（0079 §2.5）が、**この1つの照合**を使う。
    margin も件数の下限も掛けない。観測した値・cell・組の外は通さない。
    """

    __slots__ = ("_action_schema", "_anchor", "_anchor_cells", "_axes", "_pairs", "_steps")

    def __init__(self, profile: ConfidenceProfileV2, action_schema: ThermalActionSchema) -> None:
        profile = ConfidenceProfileV2.model_validate(profile.model_dump(mode="python"))
        action_schema = ThermalActionSchema.model_validate(action_schema.model_dump(mode="python"))
        if canonical_sha256(action_schema) != profile.binding.action_schema_sha256:
            raise ValueError("support の照合は Profile が束縛した action schema でだけ行う")
        self._setup(profile.action_support, fan_axes(profile.spec), action_schema)

    @classmethod
    def _from_support(
        cls,
        support: ActionSupportV2,
        axes: tuple[SupportAxis, ...],
        action_schema: ThermalActionSchema,
    ) -> Self:
        """Profile を封じる前（residual の基準を作る途中）の照合。同じ :meth:`check` を使う。"""
        checker = cls.__new__(cls)
        checker._setup(support, axes, action_schema)
        return checker

    def _setup(
        self,
        support: ActionSupportV2,
        axes: tuple[SupportAxis, ...],
        action_schema: ThermalActionSchema,
    ) -> None:
        if tuple(item.step for item in support.steps) != tuple(range(action_schema.steps)):
            raise ValueError("Profile の step ごとの欄が action schema の step と一致しない")
        if tuple(item.from_step for item in support.transitions) != tuple(
            range(action_schema.steps - 1)
        ):
            raise ValueError(
                "Profile の step の組ごとの欄が action schema の step の組と一致しない"
            )
        self._action_schema = action_schema
        self._axes = axes
        self._steps = tuple(
            (item.demand_ranges, frozenset(cell.cell for cell in item.cells))
            for item in support.steps
        )
        self._anchor = support.anchor_to_first.delta_ranges
        self._anchor_cells = frozenset(
            (item.source, item.target) for item in support.anchor_to_first.cells
        )
        self._pairs = tuple(
            (item.delta_ranges, frozenset((cell.source, cell.target) for cell in item.cells))
            for item in support.transitions
        )

    @property
    def action_schema(self) -> ThermalActionSchema:
        """照合の格子。"""
        return self._action_schema

    def check(
        self, anchor: PerZone[float], trajectory: ActionTrajectory
    ) -> StepSupportViolation | None:
        """``anchor``（いま掛かっている effective demand）からの ``trajectory`` を照らす。

        外れた点を1つ返す（決定論的な順: step ごとの範囲と (a)、anchor → 最初の step の
        変化量と (b)、step の組ごとの変化量と (c)。0084 §2.1）。すべて通れば ``None``。
        格子が action schema と違う列は照合しない（例外。補間・外挿をしない）。
        """
        trajectory = ActionTrajectory.model_validate(trajectory.model_dump(mode="python"))
        schema = self._action_schema
        if trajectory.step_ms != schema.step_ms or len(trajectory.demands) != schema.steps:
            raise ValueError("action 列の格子が action schema と一致しない（補間・外挿はしない）")
        cells = tuple(action_cell(demands, self._axes) for demands in trajectory.demands)
        for step, (demands, cell, (ranges, known_cells)) in enumerate(
            zip(trajectory.demands, cells, self._steps, strict=True)
        ):
            for zone in ZONE_ORDER:
                value = demands.get(zone)
                if not _inside(ranges.get(zone), value):
                    return StepSupportViolation(
                        kind=SupportViolationKind.STEP_DEMAND, step=step, zone=zone, value=value
                    )
            if cell not in known_cells:
                return StepSupportViolation(
                    kind=SupportViolationKind.STEP_CELL, step=step, cell=cell
                )
        first = _delta(anchor, trajectory.demands[0])
        for zone in ZONE_ORDER:
            if not _inside(self._anchor.get(zone), first.get(zone)):
                return StepSupportViolation(
                    kind=SupportViolationKind.ANCHOR_DELTA,
                    step=0,
                    zone=zone,
                    value=first.get(zone),
                )
        anchor_cell = action_cell(anchor, self._axes)
        if (anchor_cell, cells[0]) not in self._anchor_cells:
            return StepSupportViolation(
                kind=SupportViolationKind.ANCHOR_CELL,
                step=0,
                source_cell=anchor_cell,
                cell=cells[0],
            )
        for step, ((before, after), (ranges, known_pairs)) in enumerate(
            zip(pairwise(trajectory.demands), self._pairs, strict=True)
        ):
            change = _delta(before, after)
            for zone in ZONE_ORDER:
                if not _inside(ranges.get(zone), change.get(zone)):
                    return StepSupportViolation(
                        kind=SupportViolationKind.TRANSITION_DELTA,
                        step=step,
                        to_step=step + 1,
                        zone=zone,
                        value=change.get(zone),
                    )
            if (cells[step], cells[step + 1]) not in known_pairs:
                return StepSupportViolation(
                    kind=SupportViolationKind.TRANSITION_CELL,
                    step=step,
                    to_step=step + 1,
                    source_cell=cells[step],
                    cell=cells[step + 1],
                )
        return None

    def check_held(self, observed: ObservedThermalInput) -> StepSupportViolation | None:
        """anchor 推論の held の列（``hold_effective``）を照らす（0084 §2.1）。"""
        return self.check(_anchor_demands(observed), hold_effective(observed, self._action_schema))


def _anchor_demands(observed: ObservedThermalInput) -> PerZone[float]:
    return PerZone(
        front=observed.action.front.effective_demand,
        rear=observed.action.rear.effective_demand,
        top=observed.action.top.effective_demand,
    )


# ---------------------------------------------------------------- Profile v2 の生成


def fit_confidence_profile_v2(
    trained: CounterfactualTrainedModel,
    source: VerifiedTrainingDatasetArtifactV2,
    split: DatasetSplitV2,
    spec: ConfidenceProfileSpec,
) -> ConfidenceProfileV2:
    """学習結果と、その学習に使った Dataset v2・split から Profile v2 を作る（0079 §2.1）。

    - dataset・split が学習結果の出どころ（manifest / examples / split の checksum、train の件数）と
      一致しなければ拒否する。別の split で範囲を作ると、学習していない範囲を「学習済み」と扱う
    - 範囲・欠測・support cell・action 列の範囲は **train だけ**から数える。anchor action は
      ``prior_action``（0087 §2.7）、action 列は Dataset v2 に記録した列（step ごと・step の組ごと。
      0084 §2.2）
    - residual の基準は **validation** の各 example の anchor 推論（``hold_effective``）から作る。
      held の列が step ごとの support の外にある example は基準から除き、件数を記録する。ある出力で
      残りが0件なら作らない（0084 §2.1）
    - ``spec``（support 軸と bin の境界、residual の下限）に既定値を置かない。全 zone の
      ``fan.<zone>`` 軸が無ければ作らない（0079 §2.5）
    """
    if not isinstance(trained, CounterfactualTrainedModel):
        raise TypeError("Profile v2 は段 2 の学習結果から作る")
    if not isinstance(source, VerifiedTrainingDatasetArtifactV2):
        raise TypeError("Profile v2 は検証済みの Dataset v2 artifact から作る")
    trained = CounterfactualTrainedModel.model_validate(trained.model_dump(mode="python"))
    spec = ConfidenceProfileSpec.model_validate(spec.model_dump(mode="python"))
    dataset = ThermalDatasetV2.model_validate(source.dataset.model_dump(mode="python"))
    split = DatasetSplitV2.model_validate(split.model_dump(mode="python"))
    provenance = trained.training_data
    if (source.artifact_id, source.manifest_sha256, source.examples_sha256) != (
        provenance.dataset_alias,
        provenance.manifest_sha256,
        provenance.examples_sha256,
    ):
        raise ValueError("Profile の dataset が学習結果の学習 dataset と一致しない")
    _validate_split(dataset, split)
    if split_sha256(split) != provenance.split_sha256:
        raise ValueError("Profile の split が学習結果の学習 split と一致しない")
    if len(split.train) != provenance.train_example_count:
        raise ValueError("Profile の train の件数が学習結果と一致しない")
    if not split.validation:
        raise ValueError("residual の基準に validation example が1件以上必要")
    axes = fan_axes(spec)
    feature_schema = trained.feature_schema.observation_schema()
    action_schema = trained.action_schema

    train = tuple(sorted(split.train, key=_example_order))
    train_inputs = tuple(ObservedThermalInput.from_example_v2(example) for example in train)
    feature_ranges = tuple(
        _value_range(
            metric,
            (
                value
                for observed in train_inputs
                for frame in observed.window
                if (value := _usable(frame, metric)) is not None
            ),
        )
        for metric in feature_schema.metrics
    )
    fan_ranges = PerZone(
        front=_fan_range(Zone.FRONT, train_inputs),
        rear=_fan_range(Zone.REAR, train_inputs),
        top=_fan_range(Zone.TOP, train_inputs),
    )
    pattern_counts: dict[tuple[str, ...], int] = {}
    cell_counts: dict[tuple[int, ...], int] = {}
    for observed in train_inputs:
        pattern = _missing_pattern(observed, feature_schema)
        pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1
        cell = _support_cell(observed, spec.support_axes)
        if cell is not None:
            cell_counts[cell] = cell_counts.get(cell, 0) + 1
    if len(pattern_counts) > MAX_MISSING_PATTERNS:
        raise ValueError("欠測の組み合わせの数が上限を超えている")
    _check_cell_count(len(cell_counts), "support cell")
    action_support = _action_support(train, axes, action_schema.steps)

    checker = StepSupportChecker._from_support(action_support, axes, action_schema)
    validation = tuple(sorted(split.validation, key=_example_order))
    kept: list[tuple[DatasetExampleV2, ObservedThermalInput]] = []
    excluded = 0
    for example in validation:
        observed = ObservedThermalInput.from_example_v2(example)
        if checker.check_held(observed) is not None:
            # runtime ではこの入力は support の OOD になり予測を使わない。その外挿の誤差で
            # residual の基準を広げない（0084 §2.1、PR #205 の Codex P1）
            excluded += 1
            continue
        kept.append((example, observed))
    residual_scales = _residual_scales(trained, kept, spec.residual_scale_floor)

    return ConfidenceProfileV2(
        binding=trained.profile_binding(),
        anchor_action_rule=ANCHOR_ACTION_RULE,
        spec=spec,
        train_example_count=len(train),
        feature_ranges=feature_ranges,
        fan_ranges=fan_ranges,
        missing_patterns=tuple(
            MissingPatternCount(unavailable_metrics=pattern, count=count)
            for pattern, count in sorted(pattern_counts.items())
        ),
        support_cells=tuple(
            SupportCellCount(bins=bins, count=count) for bins, count in sorted(cell_counts.items())
        ),
        residual_scales=residual_scales,
        residual_validation_example_count=len(kept),
        residual_excluded_example_count=excluded,
        action_support=action_support,
    )


def seal_counterfactual_artifact(
    trained: CounterfactualTrainedModel,
    source: VerifiedTrainingDatasetArtifactV2,
    split: DatasetSplitV2,
    spec: ConfidenceProfileSpec,
) -> CounterfactualThermalModelArtifact:
    """学習結果から Profile v2 を作り、同じ artifact に封じる（0079 §2.1 の同梱。段 3）。

    封じる段の検査（L6〜L8 の (1)(2)・L10〜L12）は :func:`assemble_counterfactual_artifact` が行う。
    """
    profile = fit_confidence_profile_v2(trained, source, split, spec)
    return assemble_counterfactual_artifact(trained, profile)


def _check_cell_count(count: int, what: str) -> None:
    # 0050 §3 の cell 数の上限を、集合ごとに当てる（0084 §2.2）。黙って切らない
    if count > MAX_SUPPORT_CELLS:
        raise ValueError(f"{what} の数が上限を超えている（{count} > {MAX_SUPPORT_CELLS}）")


def _zone_ranges(values: Sequence[PerZone[float]]) -> PerZone[ValueRange]:
    def zone_range(zone: Zone) -> ValueRange:
        return _value_range(f"{FAN_SOURCE_PREFIX}{zone.value}", (item.get(zone) for item in values))

    return PerZone(
        front=zone_range(Zone.FRONT), rear=zone_range(Zone.REAR), top=zone_range(Zone.TOP)
    )


def _counted_cells(cells: Iterable[ActionCell], what: str) -> tuple[ActionCellCount, ...]:
    counts: dict[ActionCell, int] = {}
    for cell in cells:
        counts[cell] = counts.get(cell, 0) + 1
    _check_cell_count(len(counts), what)
    return tuple(ActionCellCount(cell=cell, count=count) for cell, count in sorted(counts.items()))


def _counted_pairs(
    pairs: Iterable[tuple[ActionCell, ActionCell]], what: str
) -> tuple[ActionCellTransitionCount, ...]:
    counts: dict[tuple[ActionCell, ActionCell], int] = {}
    for pair in pairs:
        counts[pair] = counts.get(pair, 0) + 1
    _check_cell_count(len(counts), what)
    return tuple(
        ActionCellTransitionCount(source=source, target=target, count=count)
        for (source, target), count in sorted(counts.items())
    )


def _action_support(
    train: Sequence[DatasetExampleV2], axes: tuple[SupportAxis, ...], steps: int
) -> ActionSupportV2:
    """train の記録した action 列から、step ごと・step の組ごとの範囲と cell を数える。

    0084 §2.2。
    """
    sequences = tuple(
        tuple(step.effective_demand for step in example.action_steps) for example in train
    )
    if any(len(sequence) != steps for sequence in sequences):
        raise ValueError("train の action 列の長さが action schema の step 数と一致しない")
    anchors = tuple(example.prior_action.effective_demand for example in train)
    cells = tuple(tuple(action_cell(demands, axes) for demands in item) for item in sequences)
    step_support = tuple(
        StepActionSupport(
            step=step,
            demand_ranges=_zone_ranges(tuple(item[step] for item in sequences)),
            cells=_counted_cells((item[step] for item in cells), f"step {step} の cell"),
        )
        for step in range(steps)
    )
    anchor_to_first = AnchorTransitionSupport(
        delta_ranges=_zone_ranges(
            tuple(_delta(anchor, item[0]) for anchor, item in zip(anchors, sequences, strict=True))
        ),
        cells=_counted_pairs(
            (
                (action_cell(anchor, axes), item[0])
                for anchor, item in zip(anchors, cells, strict=True)
            ),
            "anchor → step 0 の cell の組",
        ),
    )
    transitions = tuple(
        StepTransitionSupport(
            from_step=step,
            delta_ranges=_zone_ranges(
                tuple(_delta(item[step], item[step + 1]) for item in sequences)
            ),
            cells=_counted_pairs(
                ((item[step], item[step + 1]) for item in cells),
                f"step ({step}, {step + 1}) の cell の組",
            ),
        )
        for step in range(steps - 1)
    )
    return ActionSupportV2(
        steps=step_support, anchor_to_first=anchor_to_first, transitions=transitions
    )


def _residual_scales(
    trained: CounterfactualTrainedModel,
    kept: Sequence[tuple[DatasetExampleV2, ObservedThermalInput]],
    floor: float,
) -> tuple[ResidualScale, ...]:
    """残した validation example の anchor 推論（``hold_effective``）から出力ごとの RMS を作る。"""
    predictions = tuple(
        predict_targets(
            payload=trained.payload,
            feature_schema=trained.feature_schema,
            target_schema=trained.target_schema,
            action_schema=trained.action_schema,
            observed=observed,
            trajectory=hold_effective(observed, trained.action_schema),
        )
        for _example, observed in kept
    )
    scales: list[ResidualScale] = []
    for horizon in trained.target_schema.horizons_ms:
        for metric in trained.target_schema.metrics:
            squared: list[float] = []
            for (example, _observed), predicted in zip(kept, predictions, strict=True):
                target = next(item for item in example.targets if item.horizon_ms == horizon)
                actual = target.values[metric]
                if target.quality[metric] is not Quality.OK or actual is None:
                    continue
                value = next(item for item in predicted if item.horizon_ms == horizon).values[
                    metric
                ]
                squared.append((actual - value) ** 2)
            if not squared:
                # 除いた後に基準が残らない出力。黙って基準を作らない・既定値で埋めない（0084 §2.1）
                raise ValueError(
                    "held の列が step ごとの support の中にある validation の観測済み label が無い"
                    f"（Profile v2 を作らない）: horizon={horizon}, metric={metric}"
                )
            rms = math.sqrt(math.fsum(squared) / len(squared))
            scales.append(
                ResidualScale(
                    horizon_ms=horizon,
                    metric=metric,
                    scale=max(rms, floor),
                    validation_samples=len(squared),
                )
            )
    return tuple(scales)


# ---------------------------------------------------------------- 同梱 Profile からの判定器


def _basis_for_model(model: RegistryCounterfactualThermalModel) -> _ProfileBasis:
    """封をした型の同梱 Profile から判定の素材を作る（0079 §2.1 の runtime の束縛）。

    推論ごとに照らす束縛は、Profile v2 の binding（model ID・版）に、封をした型が**同じ bytes から**
    持つ ``artifact_sha256`` を足したもの。呼び出し側は組み立てられない。
    """
    if type(model) is not RegistryCounterfactualThermalModel:
        raise TypeError("v2 の判定器は Registry の検証経路が作った封をした型からだけ作る")
    profile = model.confidence_profile
    return _ProfileBasis(
        identity=(
            profile.binding.model_id,
            profile.binding.model_version,
            model.artifact_sha256,
        ),
        profile_sha256=profile.sha256(),
        feature_schema=model.feature_schema.observation_schema(),
        feature_ranges=profile.feature_ranges,
        fan_ranges=profile.fan_ranges,
        missing_patterns=profile.missing_patterns,
        support_axes=profile.spec.support_axes,
        support_cells=profile.support_cells,
        residual_scales=profile.residual_scales,
        shortest_horizon_ms=model.target_schema.horizons_ms[0],
    )


class CounterfactualConfidenceAssessor:
    """反実仮想 artifact v2 の**同梱 Profile からだけ**作る判定器（0079 §2.4 / 0084 §2.1）。

    **公開 constructor を持たない。** :meth:`for_model` が封をした型を受け取り、その中の Profile を
    使う。別の Profile を渡す引数を持たない。判定の規則は v1 と同じ（構成要素の enum と
    assessment の形を変えない）で、``support`` に「anchor 推論の held の列が step ごとの
    support の外」を OOD の条件として加える。confidence 0 の判定からは、既存の Gate が
    Fallback を選ぶ。
    """

    __slots__ = ("_checker", "_core", "_model")
    _checker: StepSupportChecker
    _core: _AssessorCore
    _model: RegistryCounterfactualThermalModel

    def __init__(self) -> None:
        raise TypeError("CounterfactualConfidenceAssessor は for_model からだけ作る")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("CounterfactualConfidenceAssessor は不変")

    @classmethod
    def for_model(
        cls, model: RegistryCounterfactualThermalModel, policy: ModelConfidencePolicy
    ) -> CounterfactualConfidenceAssessor:
        """封をした型と、runtime の ``model_confidence`` の設定から判定器を作る。"""
        basis = _basis_for_model(model)
        assessor = object.__new__(cls)
        object.__setattr__(assessor, "_model", model)
        object.__setattr__(assessor, "_core", _AssessorCore(basis, policy))
        object.__setattr__(
            assessor,
            "_checker",
            StepSupportChecker(model.confidence_profile, model.action_schema),
        )
        return assessor

    @property
    def model(self) -> RegistryCounterfactualThermalModel:
        """判定の基準を持つ封をした型（予測を出す model と同じもの）。"""
        return self._model

    @property
    def profile(self) -> ConfidenceProfileV2:
        """同梱 Profile v2。"""
        return self._model.confidence_profile

    @property
    def policy(self) -> ModelConfidencePolicy:
        """この判定器が使っている設定（runtime の設定と同じかを呼び出し側が確かめる）。"""
        return self._core.policy

    def held_support(self, observed: ObservedThermalInput) -> StepSupportViolation | None:
        """anchor 推論の held の列を step ごとの support に照らした結果（0084 §2.1）。"""
        return self._checker.check_held(observed)

    def assess(
        self,
        observed: ObservedThermalInput,
        prediction: ThermalPrediction,
        residual: ResidualEvidence | None,
    ) -> ConfidenceAssessment:
        """1回の anchor 推論を判定する。同じ入力からは必ず同じ結果を返す。

        予測の model ID・版・``artifact_sha256`` が封をした型の束縛と違えば ``model_binding`` が
        OOD（0050 §2.2 を v2 でも同じ強さで残す。0079 §2.1）。入力の形が feature schema と
        合わなければ例外にする（呼び出し側は ``LearnedFailure`` として Fallback にする）。
        """

        def held(observed: ObservedThermalInput) -> str | None:
            violation = self._checker.check_held(observed)
            return None if violation is None else violation.describe()

        return self._core.assess(observed, prediction, residual, held_support=held)


def counterfactual_residual_monitor(
    model: RegistryCounterfactualThermalModel, policy: ModelConfidencePolicy
) -> ResidualDriftMonitor:
    """同梱 Profile の residual の基準で照合する monitor（判定器と同じ Profile の証拠を数える）。"""
    return ResidualDriftMonitor._from_basis(_basis_for_model(model), policy)
