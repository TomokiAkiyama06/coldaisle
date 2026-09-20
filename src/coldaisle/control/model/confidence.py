"""Model Confidence / OOD 判定（#85 / 決定記録 0050）。

Learned Thermal Model が「知らない状態」で予測値を返しても、それを信用できる根拠にしない。
学習に使った範囲を **Confidence Profile** として artifact と同じ規律（strict JSON・checksum・
モデルへの束縛）で残し、推論時の入力・予測・直近の residual をそれと比べて、決定論的に
``confidence`` と ``ood`` を出す。

このモジュールは **demand / PWM / authority を出さない。** 出すのは Learned MPC の提案に付ける
``confidence`` / ``ood`` と、その理由だけである。Fallback への切替と authority の帯は
Controller Gate（``coldaisle.control.fallback.gate``）が決め、Reactive Guard と Critical Safety は
その後段で常に掛かる（AGENTS.md ルール2〜5）。Critical Safety の代わりにもならない。
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coldaisle.control.config import ModelConfidencePolicy
from coldaisle.control.model.dataset import DatasetExample, DatasetSplit, ThermalDataset
from coldaisle.control.model.thermal import (
    ArtifactVerification,
    ObservedThermalInput,
    ObservedWindowFrame,
    ThermalFeatureSchema,
    ThermalModel,
    ThermalPrediction,
    ThermalTargetSchema,
    canonical_sha256,
)
from coldaisle.control.model.training import _validate_split, split_sha256
from coldaisle.control.schema import (
    MAX_MODEL_GATE_REASONS,
    ControllerKind,
    ControllerProposal,
    PerZone,
    Reason,
    Zone,
)
from coldaisle.store.models import Quality

CONFIDENCE_PROFILE_SCHEMA_VERSION: Literal[1] = 1

MAX_SUPPORT_AXES = 8
"""support cell の軸の上限。組み合わせ数の爆発を防ぐ構造上の上限で、調整値ではない。"""
MAX_AXIS_EDGES = 64
MAX_SUPPORT_CELLS = 100_000
MAX_MISSING_PATTERNS = 4_096
MAX_PENDING_FORECASTS = 4_096
"""照合待ちの予測の上限。これを超えたら古い順に捨て、捨てた件数を数える。"""
MAX_EVALUATION_CASES = 100_000
MAX_EVALUATION_MISSES = 100
"""評価レポートに名前を残す誤判定の件数の上限。"""

FAN_SOURCE_PREFIX = "fan."
"""support 軸で Fan の effective demand を指す接頭辞（``fan.front`` など）。"""

_ZONE_ORDER = (Zone.FRONT, Zone.REAR, Zone.TOP)

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
PositiveFiniteFloat = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# ---------------------------------------------------------------- Profile（学習範囲）


class SupportAxis(_Frozen):
    """support cell の1軸。feature metric、または ``fan.<zone>`` の effective demand。"""

    source: str = Field(min_length=1, max_length=120)
    edges: tuple[FiniteFloat, ...] = Field(min_length=1, max_length=MAX_AXIS_EDGES)
    """bin の境界。値 ``x`` は ``bisect_right(edges, x)`` 番目の bin に入る。"""

    @model_validator(mode="after")
    def _edges_are_strictly_increasing(self) -> Self:
        if any(b <= a for a, b in zip(self.edges, self.edges[1:], strict=False)):
            raise ValueError("support 軸の edges は狭義単調増加にする")
        return self

    def bin_of(self, value: float) -> int:
        """値が入る bin の番号を返す。"""
        return bisect_right(self.edges, value)


class ConfidenceProfileSpec(_Frozen):
    """Profile を作るときに呼び出し側が明示する値。

    **本番の既定値は置かない**（決定記録 0048 §2.3 と同じ扱い）。
    """

    support_axes: tuple[SupportAxis, ...] = Field(min_length=1, max_length=MAX_SUPPORT_AXES)
    residual_scale_floor: PositiveFiniteFloat
    """validation の residual RMS の下限（target の単位）。0 除算と過敏な drift 判定を防ぐ。"""

    @model_validator(mode="after")
    def _axes_are_unique(self) -> Self:
        sources = [axis.source for axis in self.support_axes]
        if len(set(sources)) != len(sources):
            raise ValueError("support 軸の source を重複させない")
        return self


class ModelBinding(_Frozen):
    """Profile を作ったモデル。予測がこれと一致しなければ OOD にする。"""

    model_id: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    artifact_sha256: Sha256
    feature_schema_sha256: Sha256
    target_schema_sha256: Sha256
    training_split_sha256: Sha256


class ValueRange(_Frozen):
    """学習で観測した usable な値の範囲。1度も観測しなければ両端とも None。"""

    source: str = Field(min_length=1, max_length=120)
    minimum: FiniteFloat | None
    maximum: FiniteFloat | None
    observed_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _bounds_are_consistent(self) -> Self:
        if (self.minimum is None) != (self.maximum is None):
            raise ValueError("範囲の両端は一緒に指定する")
        if (self.minimum is None) != (self.observed_count == 0):
            raise ValueError("観測件数と範囲の有無を一致させる")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("範囲は minimum <= maximum にする")
        return self


class MissingPatternCount(_Frozen):
    """window 内で usable でない cell を含んだ metric の組み合わせと、その学習件数。"""

    unavailable_metrics: tuple[str, ...]
    count: int = Field(gt=0)

    @model_validator(mode="after")
    def _metrics_are_canonical(self) -> Self:
        if tuple(sorted(set(self.unavailable_metrics))) != self.unavailable_metrics:
            raise ValueError("欠測の組み合わせは重複なし昇順にする")
        return self


class SupportCellCount(_Frozen):
    """support 軸の bin の組と、その学習件数。"""

    bins: tuple[int, ...] = Field(min_length=1, max_length=MAX_SUPPORT_AXES)
    count: int = Field(gt=0)


class ResidualScale(_Frozen):
    """validation で測った1出力の residual RMS（下限 ``residual_scale_floor`` 適用後）。"""

    horizon_ms: int = Field(gt=0)
    metric: str = Field(min_length=1, max_length=120)
    scale: PositiveFiniteFloat
    validation_samples: int = Field(gt=0)


class ModelConfidenceProfile(_Frozen):
    """Confidence / OOD の基準。実行可能な値を含まない canonical JSON の artifact。

    範囲・欠測の組み合わせ・support は **train だけ**から作る（モデルが学習した範囲そのもの）。
    residual の基準だけは validation から作る（train の in-sample 誤差は楽観的すぎるため）。
    test / purged は使わない。
    """

    schema_name: Literal["coldaisle.confidence_profile"] = "coldaisle.confidence_profile"
    schema_version: Literal[1] = CONFIDENCE_PROFILE_SCHEMA_VERSION
    binding: ModelBinding
    feature_schema: ThermalFeatureSchema
    target_schema: ThermalTargetSchema
    spec: ConfidenceProfileSpec
    train_example_count: int = Field(gt=0)
    feature_ranges: tuple[ValueRange, ...] = Field(min_length=1)
    fan_ranges: PerZone[ValueRange]
    missing_patterns: tuple[MissingPatternCount, ...] = Field(
        min_length=1, max_length=MAX_MISSING_PATTERNS
    )
    support_cells: tuple[SupportCellCount, ...] = Field(max_length=MAX_SUPPORT_CELLS)
    residual_scales: tuple[ResidualScale, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape_matches_schemas(self) -> Self:
        if canonical_sha256(self.feature_schema) != self.binding.feature_schema_sha256:
            raise ValueError("Profile の feature schema が binding と一致しない")
        if canonical_sha256(self.target_schema) != self.binding.target_schema_sha256:
            raise ValueError("Profile の target schema が binding と一致しない")
        if tuple(item.source for item in self.feature_ranges) != self.feature_schema.metrics:
            raise ValueError("feature_ranges は feature schema の metric 順にする")
        for zone in _ZONE_ORDER:
            if self.fan_ranges.get(zone).source != f"{FAN_SOURCE_PREFIX}{zone.value}":
                raise ValueError("fan_ranges の source は fan.<zone> にする")
        allowed_sources = set(self.feature_schema.metrics) | {
            f"{FAN_SOURCE_PREFIX}{zone.value}" for zone in _ZONE_ORDER
        }
        for axis in self.spec.support_axes:
            if axis.source not in allowed_sources:
                raise ValueError(f"support 軸の source が feature / fan に無い: {axis.source}")
        patterns = [item.unavailable_metrics for item in self.missing_patterns]
        if len(set(patterns)) != len(patterns):
            raise ValueError("欠測の組み合わせを重複させない")
        known = set(self.feature_schema.metrics)
        if any(metric not in known for pattern in patterns for metric in pattern):
            raise ValueError("欠測の組み合わせに feature schema 外の metric がある")
        axis_count = len(self.spec.support_axes)
        cells = [item.bins for item in self.support_cells]
        if len(set(cells)) != len(cells):
            raise ValueError("support cell を重複させない")
        for bins in cells:
            if len(bins) != axis_count:
                raise ValueError("support cell の次元が軸の数と一致しない")
            for index, axis in zip(bins, self.spec.support_axes, strict=True):
                if not 0 <= index <= len(axis.edges):
                    raise ValueError("support cell の bin が軸の範囲外")
        outputs = tuple(f"{scale.horizon_ms}:{scale.metric}" for scale in self.residual_scales)
        expected = tuple(
            f"{horizon}:{metric}"
            for horizon in self.target_schema.horizons_ms
            for metric in self.target_schema.metrics
        )
        if outputs != expected:
            raise ValueError("residual_scales は target schema の出力順にする")
        return self

    def canonical_bytes(self) -> bytes:
        """checksum と保存に使う canonical JSON。"""
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    def sha256(self) -> str:
        """Profile の canonical SHA-256。assessment と trace で参照する。"""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def fit_confidence_profile(
    model: ThermalModel,
    dataset: ThermalDataset,
    split: DatasetSplit,
    spec: ConfidenceProfileSpec,
) -> ModelConfidenceProfile:
    """モデルが学習した split から Confidence Profile を作る。

    split がモデルの provenance（``training_split_sha256``）と一致しなければ拒否する。
    別の split で範囲を作ると、モデルが実際に見ていない範囲を「学習済み」と扱ってしまう。
    """
    dataset = ThermalDataset.model_validate(dataset.model_dump(mode="python"))
    split = DatasetSplit.model_validate(split.model_dump(mode="python"))
    spec = ConfidenceProfileSpec.model_validate(spec.model_dump(mode="python"))
    manifest = model.manifest
    if dataset.manifest.examples_sha256 != manifest.training_examples_sha256:
        raise ValueError("Profile の dataset がモデルの学習 dataset と一致しない")
    _validate_split(dataset, split)
    if split_sha256(split) != manifest.training_split_sha256:
        raise ValueError("Profile の split がモデルの学習 split と一致しない")
    if not split.train:
        raise ValueError("Profile には train example が1件以上必要")
    if not split.validation:
        raise ValueError("residual の基準に validation example が1件以上必要")

    feature_schema = model.feature_schema
    target_schema = model.target_schema
    train_inputs = tuple(ObservedThermalInput.from_example(example) for example in split.train)

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
    if len(cell_counts) > MAX_SUPPORT_CELLS:
        raise ValueError("support cell の数が上限を超えている")

    predictions = tuple(
        model.predict(ObservedThermalInput.from_example(example)) for example in split.validation
    )
    artifact_sha256 = _single_artifact(predictions)
    residual_scales = _residual_scales(
        split.validation, predictions, target_schema, spec.residual_scale_floor
    )

    return ModelConfidenceProfile(
        binding=ModelBinding(
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            artifact_sha256=artifact_sha256,
            feature_schema_sha256=canonical_sha256(feature_schema),
            target_schema_sha256=canonical_sha256(target_schema),
            training_split_sha256=manifest.training_split_sha256,
        ),
        feature_schema=feature_schema,
        target_schema=target_schema,
        spec=spec,
        train_example_count=len(split.train),
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
    )


def inference_id(observed: ObservedThermalInput, prediction: ThermalPrediction) -> str:
    """1回の推論（入力と予測）の識別子。

    Learned MPC の提案と assessment の両方に付け、同じ推論の判定だけを提案へ付けられるようにする。
    入力と予測の canonical JSON（artifact SHA-256 を含む）の SHA-256 なので、同じ model version の
    別の入力・別の artifact とは必ず違う値になる。
    """
    if observed.action_ts_ms != prediction.input_action_ts_ms:
        raise ValueError("推論の識別子は同じ入力の予測からだけ作る")
    payload = json.dumps(
        {
            "observed": observed.model_dump(mode="json"),
            "prediction": prediction.model_dump(mode="json"),
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------- Residual drift


class ResidualObservation(_Frozen):
    """照合に使う観測1つ。品質を必ず持つ。

    Profile の residual 基準は ``Quality.OK`` の label だけから作る。照合でも同じく OK の値だけを
    使い、stale / suspect / missing の値を「予測が当たった証拠」に数えない。
    """

    value: FiniteFloat | None
    quality: Quality

    @property
    def usable(self) -> float | None:
        """照合に使える値。OK で値があるときだけ返す。"""
        return self.value if self.quality is Quality.OK else None


class ResidualEvidence(_Frozen):
    """ある時点（``as_of_ts_ms``）で見た、直近の forecast の照合結果。

    **window は記録した forecast を解決した順に数える。** 照合できた forecast（matched）だけでなく、
    許容幅の中で観測が揃わなかったもの（expired）と照合待ちの上限で捨てたもの（dropped）も window の
    枠を占める。照合できない forecast が続けば、古い「当たっていた」証拠は押し出される。
    さらに解決から ``max_age_ms`` を過ぎた matched は証拠に数えない（決定記録 0050 §2.2）。

    件数の単位は forecast。1回の予測が持つ horizon × metric の出力の数では数えない。
    """

    profile_sha256: Sha256
    """この証拠を数えた Profile。別のモデルの証拠で confidence 上限を外さないために照合する。"""
    residual_window: int = Field(gt=0)
    match_tolerance_ms: int = Field(ge=0)
    max_age_ms: int = Field(gt=0)
    """数えた monitor の設定。assessment の設定と一致させる。"""
    as_of_ts_ms: int = Field(ge=0)
    """この証拠を見た時刻。assessment は推論の action 時刻と一致する証拠だけを使う。"""
    forecasts: int = Field(ge=0)
    """window の中で、全出力を OK の観測と照合でき、``max_age_ms`` 以内の forecast の件数。"""
    unmatched: int = Field(ge=0)
    """window の中で、証拠にならなかった forecast（expired / dropped / 古すぎる）の件数。"""
    ratio: FiniteFloat | None = Field(default=None, ge=0.0)
    """証拠に数えた forecast の正規化 residual の二乗平均を平均した値の平方根。"""

    @model_validator(mode="after")
    def _ratio_needs_forecasts(self) -> Self:
        if (self.forecasts == 0) != (self.ratio is None):
            raise ValueError("residual の forecast 件数と ratio の有無を一致させる")
        if self.forecasts + self.unmatched > self.residual_window:
            raise ValueError("residual の forecast 件数は window を超えない")
        return self


class _PendingForecast:
    """照合待ちの1回の予測。出力ごとに、いまの最良の観測候補を持つ。"""

    __slots__ = ("action_ts_ms", "best", "finalized", "targets")

    def __init__(self, prediction: ThermalPrediction) -> None:
        self.action_ts_ms = prediction.input_action_ts_ms
        self.targets: dict[tuple[int, str], tuple[int, float]] = {
            (target.horizon_ms, metric): (target.expected_ts_ms, value)
            for target in prediction.targets
            for metric, value in target.values.items()
        }
        self.best: dict[tuple[int, str], tuple[int, int, float]] = {}
        """出力 → (期待時刻からの距離, 観測時刻, 観測値)。"""
        self.finalized: set[tuple[int, str]] = set()


class ResidualDriftMonitor:
    """過去の予測を後から届いた観測と照合し、予測誤差の drift を forecast 単位で数える。

    照合は Dataset の target 選択（決定記録 0031 §2.2）と同じ規則にする。

    - 期待時刻 ``expected_ts_ms`` に**最も近い**観測を、``±residual_match_tolerance_ms`` の
      中でだけ採る。前後どちら側でもよい。**同距離なら過去側**を採る
    - 観測は action より後に限る。**品質が OK の値だけ**を候補にする
    - metric ごとに、その metric の使える値がある観測だけを候補にする

    観測は時刻順に届く。期待時刻以降に使える値が来るか、許容幅を過ぎたら、その出力の候補は確定する。
    1回の予測の全出力が確定したら forecast を解決し、全出力に観測があれば matched、1つでも
    無ければ expired として window に入れる。
    """

    def __init__(self, profile: ModelConfidenceProfile, policy: ModelConfidencePolicy) -> None:
        self._profile = ModelConfidenceProfile.model_validate(profile.model_dump(mode="python"))
        self._profile_sha256 = self._profile.sha256()
        self._scales = {
            (scale.horizon_ms, scale.metric): scale.scale for scale in profile.residual_scales
        }
        self._tolerance_ms = policy.residual_match_tolerance_ms.value
        shortest_horizon_ms = self._profile.target_schema.horizons_ms[0]
        if self._tolerance_ms >= shortest_horizon_ms:
            # DatasetSpec と同じ不変条件（target_tolerance_ms < 最短 horizon）。許容幅が action に
            # 届くと、予測より前の観測を「予測が当たった証拠」に数えてしまう。
            raise ValueError(
                "residual_match_tolerance_ms は Profile の最短 horizon より小さくなければならない"
            )
        self._window = policy.residual_window.value
        self._max_age_ms = policy.residual_max_age_ms.value
        self._outcomes: deque[tuple[int, float | None]] = deque(maxlen=self._window)
        """解決した forecast の (解決時刻, 誤差または None)。None は expired / dropped。"""
        self._pending: deque[_PendingForecast] = deque()
        self._last_action_ms: int | None = None
        self._clock_ms: int | None = None
        """観測と ``evidence()`` の時刻の最大値。巻き戻しを拒否する。"""

    def record(self, prediction: ThermalPrediction) -> None:
        """照合待ちに予測を加える。

        Profile と別のモデルの予測は受け付けない。同じ action を2回数えないよう、
        action 時刻は狭義単調増加に限る（重複した予測で証拠の件数を水増ししない）。
        """
        binding = self._profile.binding
        if (prediction.model_id, prediction.model_version, prediction.artifact_sha256) != (
            binding.model_id,
            binding.model_version,
            binding.artifact_sha256,
        ):
            raise ValueError("Profile と別のモデルの予測を drift の照合に混ぜない")
        if self._last_action_ms is not None and prediction.input_action_ts_ms <= (
            self._last_action_ms
        ):
            raise ValueError("drift の照合に入れる予測の action 時刻は狭義単調増加にする")
        forecast = _PendingForecast(prediction)
        if set(forecast.targets) != set(self._scales):
            raise ValueError("予測の出力が Profile の target schema と一致しない")
        self._last_action_ms = prediction.input_action_ts_ms
        self._pending.append(forecast)
        while len(self._pending) > MAX_PENDING_FORECASTS:
            dropped = self._pending.popleft()
            # 捨てた forecast も window の枠を占める。証拠が無いまま古い証拠を残さない。
            # 時刻は捨てた forecast 自身の照合期限にする（処理した時刻ではない）。
            self._outcomes.append((self._deadline(dropped), None))

    def observe(self, ts_ms: int, values: Mapping[str, ResidualObservation]) -> None:
        """観測を1つ受け取り、照合待ちの予測の候補を更新する。時刻は巻き戻せない。"""
        self._advance_clock(ts_ms)
        self._settle(ts_ms, values)

    def evidence(self, as_of_ts_ms: int) -> ResidualEvidence:
        """``as_of_ts_ms`` 時点の証拠。許容幅を過ぎた照合待ちはここで expired にする。"""
        self._advance_clock(as_of_ts_ms)
        self._settle(as_of_ts_ms, {})
        oldest = as_of_ts_ms - self._max_age_ms
        errors = [
            error
            for resolved_ts, error in self._outcomes
            if error is not None and resolved_ts >= oldest
        ]
        forecasts = len(errors)
        ratio = None if forecasts == 0 else math.sqrt(math.fsum(errors) / forecasts)
        return ResidualEvidence(
            profile_sha256=self._profile_sha256,
            residual_window=self._window,
            match_tolerance_ms=self._tolerance_ms,
            max_age_ms=self._max_age_ms,
            as_of_ts_ms=as_of_ts_ms,
            forecasts=forecasts,
            unmatched=len(self._outcomes) - forecasts,
            ratio=ratio,
        )

    def _advance_clock(self, ts_ms: int) -> None:
        if ts_ms < 0:
            raise ValueError("residual の時刻は負にできない")
        if self._clock_ms is not None and ts_ms < self._clock_ms:
            raise ValueError("residual の観測時刻は巻き戻せない")
        self._clock_ms = ts_ms

    def _settle(self, ts_ms: int, values: Mapping[str, ResidualObservation]) -> None:
        remaining: deque[_PendingForecast] = deque()
        for forecast in self._pending:
            self._update(forecast, ts_ms, values)
            if len(forecast.finalized) == len(forecast.targets):
                self._complete(forecast)
            else:
                remaining.append(forecast)
        self._pending = remaining

    def _update(
        self, forecast: _PendingForecast, ts_ms: int, values: Mapping[str, ResidualObservation]
    ) -> None:
        for key, (expected_ts_ms, _predicted) in forecast.targets.items():
            if key in forecast.finalized:
                continue
            if ts_ms - expected_ts_ms > self._tolerance_ms:
                # 許容幅を過ぎた。これより近い観測はもう来ない。
                forecast.finalized.add(key)
                continue
            observation = values.get(key[1])
            actual = None if observation is None else observation.usable
            if (
                actual is None
                or ts_ms <= forecast.action_ts_ms
                or expected_ts_ms - ts_ms > self._tolerance_ms
            ):
                continue
            distance = abs(ts_ms - expected_ts_ms)
            best = forecast.best.get(key)
            # 観測は時刻順に届くので、同距離なら先に来た（過去側の）候補を残す。
            if best is None or distance < best[0]:
                forecast.best[key] = (distance, ts_ms, actual)
            if ts_ms >= expected_ts_ms:
                # 期待時刻以降の値が来た。以降の観測はこれより遠いか同距離の未来側。
                forecast.finalized.add(key)

    def _complete(self, forecast: _PendingForecast) -> None:
        """解決した forecast を window へ入れる。

        時刻は **forecast 自身の観測 / 照合期限**から決める。処理した時刻（遅れて届いた別の観測の
        時刻や ``evidence()`` を読んだ時刻）を使うと、古い観測の照合が「新しい証拠」になり、
        証拠が無い間の confidence 上限を外してしまう。
        """
        if len(forecast.best) != len(forecast.targets):
            self._outcomes.append((self._deadline(forecast), None))
            return
        squared = [
            ((forecast.best[key][2] - predicted) / self._scales[key]) ** 2
            for key, (_expected, predicted) in sorted(forecast.targets.items())
        ]
        # 照合に使った観測のうち最も新しいものを、この証拠の時刻とする。
        resolved_ts_ms = max(observation[1] for observation in forecast.best.values())
        self._outcomes.append((resolved_ts_ms, math.fsum(squared) / len(squared)))

    def _deadline(self, forecast: _PendingForecast) -> int:
        """forecast の照合期限（最後の期待時刻 + 許容幅）。"""
        return (
            max(expected_ts_ms for expected_ts_ms, _predicted in forecast.targets.values())
            + self._tolerance_ms
        )


# ---------------------------------------------------------------- Assessment


class ConfidenceComponent(StrEnum):
    """confidence を構成する判定。"""

    MODEL_BINDING = "model_binding"
    FEATURE_RANGE = "feature_range"
    FAN_STATE_RANGE = "fan_state_range"
    SUPPORT = "support"
    MISSING_PATTERN = "missing_pattern"
    UNCERTAINTY = "uncertainty"
    RESIDUAL_DRIFT = "residual_drift"


class ComponentResult(_Frozen):
    """1判定の結果。``score`` が None の判定は confidence の最小値に加えない。"""

    component: ConfidenceComponent
    score: UnitInterval | None
    cap: UnitInterval | None = None
    """この判定が課す confidence の上限（uncertainty 無し・residual の件数不足）。"""
    ood: bool
    detail: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _ood_has_zero_score(self) -> Self:
        if self.ood and self.score != 0.0:
            raise ValueError("OOD の判定の score は 0 にする")
        return self


class ConfidenceAssessment(_Frozen):
    """1回の推論に対する Confidence / OOD の判定。**demand / authority を持たない。**"""

    model_id: str
    model_version: str
    artifact_sha256: Sha256
    artifact_verification: ArtifactVerification
    profile_sha256: Sha256
    input_action_ts_ms: int = Field(ge=0)
    inference_id: Sha256
    """判定した推論（入力と予測）。提案の ``inference_id`` と一致したときだけ提案へ付けられる。"""
    confidence: UnitInterval
    ood: bool
    components: tuple[ComponentResult, ...] = Field(
        min_length=len(ConfidenceComponent), max_length=len(ConfidenceComponent)
    )

    @model_validator(mode="after")
    def _confidence_follows_components(self) -> Self:
        if tuple(item.component for item in self.components) != tuple(ConfidenceComponent):
            raise ValueError("assessment の components は定義順に1つずつ持つ")
        ood = any(item.ood for item in self.components)
        if self.ood != ood:
            raise ValueError("assessment の ood は components の OR にする")
        if self.confidence != _combine(self.components):
            raise ValueError("assessment の confidence は components の最小値にする")
        return self

    @property
    def ood_components(self) -> tuple[ConfidenceComponent, ...]:
        """OOD と判定した構成要素。"""
        return tuple(item.component for item in self.components if item.ood)

    def trace_reasons(self) -> tuple[Reason, ...]:
        """#82 の trace（``ModelGateDecision.assessment``）へ残す理由。"""
        reasons = tuple(
            Reason(
                code=(f"ood_{item.component.value}" if item.ood else item.component.value),
                detail=_trace_detail(item),
            )
            for item in self.components
        )
        return reasons[:MAX_MODEL_GATE_REASONS]

    def apply_to(self, proposal: ControllerProposal) -> ControllerProposal:
        """Learned MPC の提案に confidence / ood を付ける。

        Registry を通っていない artifact の予測は制御へ渡さない（決定記録 0048 §2.4）。
        """
        if proposal.controller is not ControllerKind.LEARNED_MPC:
            raise ValueError("confidence を付けるのは Learned MPC の提案だけ")
        if self.artifact_verification is not ArtifactVerification.REGISTRY_VERIFIED:
            raise ValueError("Registry 検証済みでないモデルの判定を制御の提案に付けない")
        if proposal.model_version != self.model_version:
            raise ValueError("提案と assessment の model_version が一致しない")
        if proposal.inference_id != self.inference_id:
            # 同じ model version でも、別の入力（OOD かもしれない）への提案に、以前の
            # in-distribution な判定を付け替えさせない。付けられなければ提案は使えない。
            raise ValueError("提案と assessment が別の推論のもの")
        return proposal.model_copy(update={"confidence": self.confidence, "ood": self.ood})


class ConfidenceAssessor:
    """Profile と設定から、入力・予測ごとの Confidence / OOD を決定論的に出す。"""

    def __init__(self, profile: ModelConfidenceProfile, policy: ModelConfidencePolicy) -> None:
        self._profile = ModelConfidenceProfile.model_validate(profile.model_dump(mode="python"))
        self._profile_sha256 = self._profile.sha256()
        self._policy = policy
        self._patterns = {
            item.unavailable_metrics: item.count for item in self._profile.missing_patterns
        }
        self._cells = {item.bins: item.count for item in self._profile.support_cells}
        self._ranges = {item.source: item for item in self._profile.feature_ranges}

    @property
    def profile(self) -> ModelConfidenceProfile:
        """検証済みの Profile。"""
        return self._profile

    @property
    def policy(self) -> ModelConfidencePolicy:
        """この判定器が使っている設定。

        呼び出し側（#86）が「runtime の設定と同じものか」を確かめられるようにする。
        別の設定で作った判定器を渡されると、閾値だけがすり替わる。
        """
        return self._policy

    def assess(
        self,
        observed: ObservedThermalInput,
        prediction: ThermalPrediction,
        residual: ResidualEvidence | None,
    ) -> ConfidenceAssessment:
        """1回の推論を判定する。同じ入力からは必ず同じ結果を返す。

        入力の形が feature schema と合わない場合は例外にする（予測自体も失敗する）。
        呼び出し側（worker）はそれを ``LearnedFailure`` として Gate へ渡し、Fallback になる。
        """
        observed = ObservedThermalInput.model_validate(observed.model_dump(mode="python"))
        _check_window_shape(observed, self._profile.feature_schema)
        components = (
            self._model_binding(observed, prediction),
            self._feature_range(observed),
            self._fan_state_range(observed),
            self._support(observed),
            self._missing_pattern(observed),
            self._uncertainty(prediction),
            self._residual_drift(residual, observed.action_ts_ms),
        )
        return ConfidenceAssessment(
            model_id=prediction.model_id,
            model_version=prediction.model_version,
            artifact_sha256=prediction.artifact_sha256,
            artifact_verification=prediction.artifact_verification,
            profile_sha256=self._profile_sha256,
            input_action_ts_ms=observed.action_ts_ms,
            inference_id=inference_id(observed, prediction),
            confidence=_combine(components),
            ood=any(item.ood for item in components),
            components=components,
        )

    def _model_binding(
        self, observed: ObservedThermalInput, prediction: ThermalPrediction
    ) -> ComponentResult:
        binding = self._profile.binding
        mismatches = [
            name
            for name, expected, actual in (
                ("model_id", binding.model_id, prediction.model_id),
                ("model_version", binding.model_version, prediction.model_version),
                ("artifact_sha256", binding.artifact_sha256, prediction.artifact_sha256),
                ("input_action_ts_ms", observed.action_ts_ms, prediction.input_action_ts_ms),
            )
            if expected != actual
        ]
        if mismatches:
            return ComponentResult(
                component=ConfidenceComponent.MODEL_BINDING,
                score=0.0,
                ood=True,
                detail=f"mismatch={','.join(mismatches)}",
            )
        return ComponentResult(component=ConfidenceComponent.MODEL_BINDING, score=1.0, ood=False)

    def _feature_range(self, observed: ObservedThermalInput) -> ComponentResult:
        worst = 0.0
        worst_source = ""
        for metric in self._profile.feature_schema.metrics:
            known = self._ranges[metric]
            for frame in observed.window:
                value = _usable(frame, metric)
                if value is None:
                    continue
                excess = _excess(known, value)
                if excess > worst:
                    worst, worst_source = excess, metric
        return self._range_result(ConfidenceComponent.FEATURE_RANGE, worst, worst_source)

    def _fan_state_range(self, observed: ObservedThermalInput) -> ComponentResult:
        worst = 0.0
        worst_source = ""
        for zone in _ZONE_ORDER:
            known = self._profile.fan_ranges.get(zone)
            excess = _excess(known, observed.action.get(zone).effective_demand)
            if excess > worst:
                worst, worst_source = excess, known.source
        return self._range_result(ConfidenceComponent.FAN_STATE_RANGE, worst, worst_source)

    def _range_result(
        self, component: ConfidenceComponent, worst: float, source: str
    ) -> ComponentResult:
        margin = self._policy.range_margin.value
        if worst > margin:
            shown = "inf" if math.isinf(worst) else f"{worst:.6f}"
            return ComponentResult(
                component=component,
                score=0.0,
                ood=True,
                detail=f"source={source}; excess={shown}; margin={margin:.6f}",
            )
        score = 1.0 if worst == 0.0 else 1.0 - worst / margin
        detail = "" if worst == 0.0 else f"source={source}; excess={worst:.6f}"
        return ComponentResult(component=component, score=score, ood=False, detail=detail)

    def _support(self, observed: ObservedThermalInput) -> ComponentResult:
        cell = _support_cell(observed, self._profile.spec.support_axes)
        if cell is None:
            return ComponentResult(
                component=ConfidenceComponent.SUPPORT,
                score=0.0,
                ood=True,
                detail="support axis value is unavailable",
            )
        count = self._cells.get(cell, 0)
        minimum = self._policy.min_support_count.value
        full = self._policy.full_support_count.value
        detail = f"cell={list(cell)}; count={count}"
        if count < minimum:
            return ComponentResult(
                component=ConfidenceComponent.SUPPORT,
                score=0.0,
                ood=True,
                detail=f"{detail}; min={minimum}",
            )
        return ComponentResult(
            component=ConfidenceComponent.SUPPORT,
            score=min(1.0, count / full),
            ood=False,
            detail=detail,
        )

    def _missing_pattern(self, observed: ObservedThermalInput) -> ComponentResult:
        pattern = _missing_pattern(observed, self._profile.feature_schema)
        count = self._patterns.get(pattern, 0)
        minimum = self._policy.min_missing_pattern_count.value
        detail = f"unavailable={','.join(pattern) or '-'}; count={count}"
        if count < minimum:
            # 学習時に無かった欠測の組み合わせは OOD（決定記録 0029）。
            return ComponentResult(
                component=ConfidenceComponent.MISSING_PATTERN,
                score=0.0,
                ood=True,
                detail=f"{detail}; min={minimum}",
            )
        return ComponentResult(
            component=ConfidenceComponent.MISSING_PATTERN, score=1.0, ood=False, detail=detail
        )

    def _uncertainty(self, prediction: ThermalPrediction) -> ComponentResult:
        # Thermal Model v1 の予測は uncertainty を持たない（型が None 固定。0048 §2.5）。
        # 「予測値が出た」ことを信用の根拠にしないため、無い間は設定の上限で confidence を抑える。
        # uncertainty を持つ予測 schema を足すときは、ここを version と一緒に拡張する。
        del prediction
        return ComponentResult(
            component=ConfidenceComponent.UNCERTAINTY,
            score=None,
            cap=self._policy.cap_without_uncertainty.value,
            ood=False,
            detail="model provides no uncertainty",
        )

    def _residual_drift(
        self, residual: ResidualEvidence | None, action_ts_ms: int
    ) -> ComponentResult:
        if residual is not None:
            if residual.profile_sha256 != self._profile_sha256:
                # 別の Profile の証拠で上限を外すと、照合していないモデルへ authority を渡す。
                # （モデルを差し替えた直後など）
                raise ValueError("residual の証拠が assessment の Profile と一致しない")
            settings = (
                residual.residual_window,
                residual.match_tolerance_ms,
                residual.max_age_ms,
            )
            if settings != (
                self._policy.residual_window.value,
                self._policy.residual_match_tolerance_ms.value,
                self._policy.residual_max_age_ms.value,
            ):
                # 別の設定で数えた証拠は、最低件数・照合・鮮度の意味が違う。
                raise ValueError(
                    "residual の証拠が assessment の設定と別の window / 許容幅 / 鮮度で数えられた"
                )
            if residual.as_of_ts_ms != action_ts_ms:
                # 以前の時点で見た証拠を、後の推論で使い回させない。
                raise ValueError("residual の証拠はこの推論の action 時刻で見たものに限る")
        minimum = self._policy.residual_min_samples.value
        forecasts = 0 if residual is None else residual.forecasts
        if residual is None or residual.ratio is None or forecasts < minimum:
            return ComponentResult(
                component=ConfidenceComponent.RESIDUAL_DRIFT,
                score=None,
                cap=self._policy.cap_before_residual_evidence.value,
                ood=False,
                detail=f"forecasts={forecasts}; min={minimum}",
            )
        ood_ratio = self._policy.residual_drift_ood_ratio.value
        ratio = residual.ratio
        detail = f"ratio={ratio:.6f}; forecasts={forecasts}"
        if ratio >= ood_ratio:
            return ComponentResult(
                component=ConfidenceComponent.RESIDUAL_DRIFT,
                score=0.0,
                ood=True,
                detail=f"{detail}; ood_ratio={ood_ratio:.6f}",
            )
        score = 1.0 if ratio <= 1.0 else (ood_ratio - ratio) / (ood_ratio - 1.0)
        return ComponentResult(
            component=ConfidenceComponent.RESIDUAL_DRIFT, score=score, ood=False, detail=detail
        )


# ---------------------------------------------------------------- Offline evaluation


class OodEvaluationCase(_Frozen):
    """offline 評価の1件。``expected_ood`` は評価者が付けた正解。"""

    label: str = Field(min_length=1, max_length=200)
    observed: ObservedThermalInput
    expected_ood: bool
    residual: ResidualEvidence | None = None


class OodEvaluationReport(_Frozen):
    """OOD 検出の false positive / false negative。"""

    true_positive: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    true_negative: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    false_positive_rate: UnitInterval | None
    """in-distribution を OOD と判定した割合。in-distribution が無ければ None。"""
    false_negative_rate: UnitInterval | None
    """OOD を見逃した割合。OOD の case が無ければ None。"""
    ood_by_component: dict[str, int]
    false_positive_labels: tuple[str, ...] = Field(max_length=MAX_EVALUATION_MISSES)
    false_negative_labels: tuple[str, ...] = Field(max_length=MAX_EVALUATION_MISSES)


def evaluate_ood_detection(
    model: ThermalModel,
    assessor: ConfidenceAssessor,
    cases: Sequence[OodEvaluationCase],
) -> OodEvaluationReport:
    """offline dataset（Replay・合成）で OOD 判定の誤り率を数える。制御には使わない。"""
    if len(cases) > MAX_EVALUATION_CASES:
        raise ValueError("OOD 評価の件数が上限を超えている")
    tp = fp = tn = fn = 0
    by_component: dict[str, int] = {item.value: 0 for item in ConfidenceComponent}
    fp_labels: list[str] = []
    fn_labels: list[str] = []
    for case in cases:
        prediction = model.predict(case.observed)
        assessment = assessor.assess(case.observed, prediction, case.residual)
        for component in assessment.ood_components:
            by_component[component.value] += 1
        if assessment.ood and case.expected_ood:
            tp += 1
        elif assessment.ood:
            fp += 1
            if len(fp_labels) < MAX_EVALUATION_MISSES:
                fp_labels.append(case.label)
        elif case.expected_ood:
            fn += 1
            if len(fn_labels) < MAX_EVALUATION_MISSES:
                fn_labels.append(case.label)
        else:
            tn += 1
    negatives = fp + tn
    positives = tp + fn
    return OodEvaluationReport(
        true_positive=tp,
        false_positive=fp,
        true_negative=tn,
        false_negative=fn,
        false_positive_rate=None if negatives == 0 else fp / negatives,
        false_negative_rate=None if positives == 0 else fn / positives,
        ood_by_component=by_component,
        false_positive_labels=tuple(fp_labels),
        false_negative_labels=tuple(fn_labels),
    )


def replay_cases(
    examples: Iterable[DatasetExample], *, expected_ood: bool
) -> tuple[OodEvaluationCase, ...]:
    """保存済み dataset の example を、正解ラベル付きの評価 case にする。"""
    return tuple(
        OodEvaluationCase(
            label=example.example_id,
            observed=ObservedThermalInput.from_example(example),
            expected_ood=expected_ood,
        )
        for example in examples
    )


# ---------------------------------------------------------------- helpers


def _combine(components: Iterable[ComponentResult]) -> float:
    items = tuple(components)
    if any(item.ood for item in items):
        return 0.0
    values = [item.score for item in items if item.score is not None]
    values.extend(item.cap for item in items if item.cap is not None)
    return min(values, default=0.0)


def _trace_detail(item: ComponentResult) -> str:
    parts = []
    if item.score is not None:
        parts.append(f"score={item.score:.6f}")
    if item.cap is not None:
        parts.append(f"cap={item.cap:.6f}")
    if item.detail:
        parts.append(item.detail)
    return "; ".join(parts)[:500]


def _usable(frame: ObservedWindowFrame, metric: str) -> float | None:
    """usable（missing / stale / suspect でない）値だけを返す。"""
    if frame.missing_mask[metric] or frame.stale_mask[metric] or frame.suspect_mask[metric]:
        return None
    return frame.values[metric]


def _value_range(source: str, values: Iterable[float]) -> ValueRange:
    items = tuple(values)
    if not items:
        return ValueRange(source=source, minimum=None, maximum=None, observed_count=0)
    return ValueRange(
        source=source, minimum=min(items), maximum=max(items), observed_count=len(items)
    )


def _fan_range(zone: Zone, inputs: Sequence[ObservedThermalInput]) -> ValueRange:
    return _value_range(
        f"{FAN_SOURCE_PREFIX}{zone.value}",
        (observed.action.get(zone).effective_demand for observed in inputs),
    )


def _excess(known: ValueRange, value: float) -> float:
    """学習範囲の幅で正規化した、範囲外へのはみ出し量。範囲内なら 0。"""
    if known.minimum is None or known.maximum is None:
        # 学習で一度も usable でなかった信号に値が来た。比べる基準が無い。
        return math.inf
    distance = max(0.0, known.minimum - value, value - known.maximum)
    if distance == 0.0:
        return 0.0
    width = known.maximum - known.minimum
    return math.inf if width == 0.0 else distance / width


def _missing_pattern(
    observed: ObservedThermalInput, schema: ThermalFeatureSchema
) -> tuple[str, ...]:
    return tuple(
        sorted(
            metric
            for metric in schema.metrics
            if any(_usable(frame, metric) is None for frame in observed.window)
        )
    )


def _support_cell(
    observed: ObservedThermalInput, axes: Sequence[SupportAxis]
) -> tuple[int, ...] | None:
    """action 時点の値で support cell を決める。軸の値が usable でなければ None。"""
    last = observed.window[-1]
    bins: list[int] = []
    for axis in axes:
        if axis.source.startswith(FAN_SOURCE_PREFIX):
            zone = Zone(axis.source.removeprefix(FAN_SOURCE_PREFIX))
            value: float | None = observed.action.get(zone).effective_demand
        else:
            if axis.source not in last.values:
                return None
            value = _usable(last, axis.source)
        if value is None:
            return None
        bins.append(axis.bin_of(value))
    return tuple(bins)


def _check_window_shape(observed: ObservedThermalInput, schema: ThermalFeatureSchema) -> None:
    expected = tuple(
        range(
            observed.action_ts_ms - schema.window_ms,
            observed.action_ts_ms + 1,
            schema.sample_period_ms,
        )
    )
    if tuple(frame.ts_ms for frame in observed.window) != expected:
        raise ValueError("assessment の入力 window が feature schema と一致しない")
    metrics = set(schema.metrics)
    if any(set(frame.values) != metrics for frame in observed.window):
        raise ValueError("assessment の入力 metric が feature schema と一致しない")


def _single_artifact(predictions: Sequence[ThermalPrediction]) -> str:
    hashes = {prediction.artifact_sha256 for prediction in predictions}
    if len(hashes) != 1:
        raise ValueError("Profile の作成中にモデルの artifact が変わった")
    return next(iter(hashes))


def _residual_scales(
    examples: Sequence[DatasetExample],
    predictions: Sequence[ThermalPrediction],
    target_schema: ThermalTargetSchema,
    floor: float,
) -> tuple[ResidualScale, ...]:
    scales: list[ResidualScale] = []
    for horizon in target_schema.horizons_ms:
        for metric in target_schema.metrics:
            squared: list[float] = []
            for example, prediction in zip(examples, predictions, strict=True):
                target = next(item for item in example.targets if item.horizon_ms == horizon)
                actual = target.values[metric]
                if target.quality[metric] is not Quality.OK or actual is None:
                    continue
                predicted = next(
                    item for item in prediction.targets if item.horizon_ms == horizon
                ).values[metric]
                squared.append((actual - predicted) ** 2)
            if not squared:
                raise ValueError(
                    f"validation に観測済み label が無い: horizon={horizon}, metric={metric}"
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
