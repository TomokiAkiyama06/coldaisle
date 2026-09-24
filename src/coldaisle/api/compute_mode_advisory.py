"""Compute Mode 切替時の環境条件アドバイザリ（#68 / 決定記録 0063）。

**助言だけを作る。** 出すのは「いまの環境条件」と「過去に実測されたフルロード期間
との差」であり、切替を止めることも、制御へ渡ることもない。

この層が守る不変条件（対応する試験は ``tests/test_compute_mode_advisory.py``）:

I-1 介入しない。``blocking`` は型で常に false。``coldaisle.control`` も
    ``coldaisle.event_entry`` も import せず、Demand / PWM へ至る経路を持たない。
I-2 欠けた判断材料を「問題なし」にしない。使えない条件は ``safe=false`` の理由に
    なり、比較できなかったことは ``limitations`` に残る。黙って省略しない。
I-3 自己申告を根拠にしない。フルロード実績は観測された電力（quality=ok）だけから
    導く。GPU Mode の申告（``events`` / ``sys.gpu_mode``）からは導かない
    （決定記録 0045 §2.6）。
I-4 同じ入力の反復で水増ししない。連続した観測は1つの期間であり、同じ時刻の
    サンプルを繰り返し受けても件数・継続時間は増えない。
I-5 時刻は証拠由来。期間の開始・終了・継続時間は根拠バケットの時刻から決める。
    現在時刻や書き手の申告から作らない。
I-6 AI に依存しない。要約器の有無・失敗で advisory は変わらない。
I-7 しきい値・窓・metric 名をコードに持たない。``config/compute-mode-advisory.yaml``
    が唯一の情報源で、``metrics.yaml`` に無い metric 名は起動時に拒否する。
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle.api.models import (
    AdvisoryCondition,
    ComputeModeAdvisory,
    FullLoadReference,
    HealthSources,
    HealthSourceStatus,
    ServerSignal,
    iso,
)
from coldaisle.metrics import MetricCatalog
from coldaisle.store import Aggregation, AlertSeverity, Quality, SqliteStore
from coldaisle.store.models import AlertRecord, LatestReading, RollupPoint

DAY_MS = 86_400_000

_BUCKET_MS: dict[str, int] = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000}
_AGGREGATION: dict[str, Aggregation] = {
    "1m": Aggregation.MINUTE,
    "5m": Aggregation.FIVE_MINUTES,
    "1h": Aggregation.HOUR,
}


class _SettingsModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConditionSetting(_SettingsModel):
    """提示する環境条件1件。しきい値はすべて任意で、無ければ比較しない。"""

    metric: str
    advisory_max: float | None = None
    warn_delta: float | None = None


class CurrentSettings(_SettingsModel):
    max_age_s: float = Field(gt=0)
    conditions: tuple[ConditionSetting, ...] = Field(min_length=1)


class LoadSettings(_SettingsModel):
    """「フルロードだった」とみなす観測条件。申告ではなく測定値で決める。"""

    metric: str
    min_value: float
    min_ok_samples: int = Field(gt=0)
    min_duration_s: int = Field(gt=0)
    max_gap_s: int = Field(ge=0)


class HistorySettings(_SettingsModel):
    lookback_days: int = Field(gt=0)
    bucket: Literal["1m", "5m", "1h"]
    refresh_s: int = Field(gt=0)
    load: LoadSettings
    peak_metrics: tuple[str, ...] = ()

    @property
    def bucket_ms(self) -> int:
        return _BUCKET_MS[self.bucket]

    @property
    def aggregation(self) -> Aggregation:
        return _AGGREGATION[self.bucket]


class ComputeModeAdvisorySettings(_SettingsModel):
    """``config/compute-mode-advisory.yaml``。助言用の値だけを持つ。"""

    version: Literal[1]
    current: CurrentSettings
    history: HistorySettings

    @classmethod
    def from_yaml(cls, path: Path, *, catalog: MetricCatalog) -> ComputeModeAdvisorySettings:
        """YAML を厳格に読み、metrics.yaml に無い metric 名を起動前に拒否する。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Compute Mode アドバイザリ設定が辞書ではない: {path}")
        settings = cls.model_validate(loaded)
        settings.validate_metrics(catalog)
        return settings

    def validate_metrics(self, catalog: MetricCatalog) -> None:
        """誤記した metric 名は常に欠測に見え、黙って助言から消えるため拒否する。"""
        names = {
            *(condition.metric for condition in self.current.conditions),
            *self.history.peak_metrics,
            self.history.load.metric,
        }
        unknown = sorted(name for name in names if name not in catalog.metrics)
        if unknown:
            raise ValueError(f"metrics.yaml に定義の無い metric: {', '.join(unknown)}")
        duplicated = sorted(
            name
            for name, count in Counter(
                condition.metric for condition in self.current.conditions
            ).items()
            if count > 1
        )
        if duplicated:
            # 同じ条件を2度並べると、同じ事実で警告が2件に増える（I-4）
            raise ValueError(f"条件に同じ metric を重ねられない: {', '.join(duplicated)}")

    @property
    def lookback_ms(self) -> int:
        return self.history.lookback_days * DAY_MS


@dataclass(frozen=True)
class _Episode:
    """観測されたフルロード期間1件。時刻はすべて根拠バケット由来（I-5）。"""

    started_at_ms: int
    ended_at_ms: int
    bucket_count: int
    covered_ms: int
    load_peak: float | None
    load_mean: float | None


@dataclass(frozen=True)
class _History:
    """さかのぼり評価の結果。``refresh_s`` の窓ごとに1回だけ作る。"""

    reference: FullLoadReference | None
    count: int
    evaluated_at_ms: int
    limitations: tuple[str, ...]


class ComputeModeAdvisor:
    """決定論的な advisory を組み立てる。AI も制御も関与しない。

    履歴部分は設定の ``refresh_s`` の窓ごとに1回だけ評価する。WebSocket は
    毎秒 payload を作り直すため、30日ぶんの集計を毎回走らせない。窓の鍵は
    時刻そのものであり、**同じ時刻・同じ DB なら常に同じ結果**になる。
    呼び出し側は ``ordered()`` の中で時刻とスナップショットを読む。
    """

    def __init__(self, settings: ComputeModeAdvisorySettings, catalog: MetricCatalog) -> None:
        self._settings = settings
        self._catalog = catalog
        # 再入可能にする。ordered() の中から evaluate() が同じロックを取るため
        self._lock = threading.RLock()
        # 評価済みの窓とその結果。ordered() の中で呼ぶ限り、窓は前にしか進まない
        self._cached: tuple[int, _History] | None = None

    @property
    def settings(self) -> ComputeModeAdvisorySettings:
        return self._settings

    def ordered(self) -> AbstractContextManager[bool]:
        """呼び出しを直列にする。**現在時刻とスナップショットはこの中で読む。**

        時刻を読んでからロックを待つと、遅れた呼び出しが自分より新しい時刻で
        作られた履歴を受け取り、`evaluated_at` が `generated_at` を追い越したり、
        応答のスナップショットに無いデータが `reference` に混ざったりする
        （決定記録 0042 §2.6）。時刻を読む前に順序を決めれば、観測される窓は
        ロックの順に単調に進み、履歴は窓が進んだときだけ評価すればよい。
        """
        return self._lock

    def evaluate(
        self,
        store: SqliteStore,
        readings: Mapping[str, LatestReading],
        *,
        signal: ServerSignal,
        sources: HealthSources,
        alerts: Sequence[AlertRecord],
        firing: Mapping[AlertSeverity, int],
        now_ms: int,
    ) -> ComputeModeAdvisory:
        """判断材料を1つにまとめる。**どの入力でも切替を止めない**（I-1）。"""
        history = self._history(store, now_ms)
        conditions = self._conditions(readings, history.reference)

        warnings = list(_monitoring_warnings(sources, alerts, firing))
        warnings.extend(_condition_warnings(conditions, self._settings.current.conditions))
        if signal is not ServerSignal.GREEN and not warnings:
            warnings.append("one or more telemetry values are not quality=ok")

        unusable = [condition for condition in conditions if not condition.usable]
        exceeded = [condition for condition in conditions if condition.exceeded]
        return ComputeModeAdvisory(
            # 条件が読めない・上限を超えているのに safe を立てない（I-2）。
            # safe は「いまの条件が揃っていて範囲内」であり、将来の負荷の安全を
            # 保証しない（決定記録 0063 §2.5）
            safe=signal is ServerSignal.GREEN and not unusable and not exceeded,
            warnings=tuple(warnings),
            conditions=conditions,
            reference=history.reference,
            reference_count=history.count,
            reference_window_days=self._settings.history.lookback_days,
            evaluated_at_ms=history.evaluated_at_ms,
            evaluated_at=iso(history.evaluated_at_ms),
            limitations=history.limitations,
        )

    def _conditions(
        self,
        readings: Mapping[str, LatestReading],
        reference: FullLoadReference | None,
    ) -> tuple[AdvisoryCondition, ...]:
        max_age_ms = self._settings.current.max_age_s * 1000
        result: list[AdvisoryCondition] = []
        for setting in self._settings.current.conditions:
            reading = readings.get(setting.metric)
            # 保存されていない metric は保存済みの missing と同じに扱う（0042 §2.3）
            quality = Quality.MISSING if reading is None else reading.quality
            value = None if reading is None else reading.value
            age_ms = None if reading is None else reading.age_ms
            usable = (
                value is not None
                and quality is Quality.OK
                and age_ms is not None
                # 未来の時刻の値（負の age）も「いまの条件」として使わない
                and 0 <= age_ms <= max_age_ms
            )
            reference_value = (
                None if reference is None else reference.conditions.get(setting.metric)
            )
            result.append(
                AdvisoryCondition(
                    metric=setting.metric,
                    value=value,
                    unit=self._catalog.unit_for(setting.metric),
                    quality=quality,
                    age_seconds=None if age_ms is None else round(age_ms / 1000, 3),
                    usable=usable,
                    advisory_max=setting.advisory_max,
                    exceeded=(
                        usable
                        and setting.advisory_max is not None
                        and value is not None
                        and value > setting.advisory_max
                    ),
                    reference_value=reference_value,
                    delta=(
                        round(value - reference_value, 3)
                        if usable and value is not None and reference_value is not None
                        else None
                    ),
                )
            )
        return tuple(result)

    def _history(self, store: SqliteStore, now_ms: int) -> _History:
        window = now_ms // (self._settings.history.refresh_s * 1000)
        # REST と WS が同じ advisor を別スレッドから呼ぶ。評価中もロックを持ち、
        # 窓ごとの評価を1回に限る。呼び出し側は ordered() の中で時刻と
        # スナップショットを読むため、窓はロックの順に単調に進む
        with self._lock:
            cached = self._cached
            if cached is not None and cached[0] == window:
                return cached[1]
            # 窓が変わったら、その呼び出しの時刻とスナップショットで評価し直す。
            # 他の呼び出しの結果を流用しないので、evaluated_at <= generated_at が
            # 常に成り立ち、応答に無いデータが reference に混ざらない
            history = self._evaluate_history(store, now_ms)
            self._cached = (window, history)
            return history

    def _evaluate_history(self, store: SqliteStore, now_ms: int) -> _History:
        history = self._settings.history
        bucket_ms = history.bucket_ms
        # 完了したバケットだけを読む。進行中のバケットは5分ぶん観測していないのに
        # 5分として数えられ、終了時刻も未来になる（0063 §2.3）
        end_ms = now_ms // bucket_ms * bucket_ms
        start_ms = max(0, (now_ms - self._settings.lookback_ms) // bucket_ms * bucket_ms)
        limitations: list[str] = []
        if start_ms >= end_ms:
            return _History(None, 0, now_ms, ("history window is empty",))

        buckets = store.aggregate(history.load.metric, start_ms, end_ms, history.aggregation)
        episodes = self._episodes(buckets, bucket_ms)
        if not episodes:
            # 「比較できなかった」ことを消さない（I-2）
            limitations.append(
                f"no observed full-load period in the last {history.lookback_days} days "
                f"({history.load.metric} >= {history.load.min_value:g} for "
                f"{history.load.min_duration_s}s)"
            )
            return _History(None, 0, now_ms, tuple(limitations))

        latest = episodes[-1]
        conditions: dict[str, float] = {}
        for setting in self._settings.current.conditions:
            mean = self._mean_over(store, setting.metric, latest)
            if mean is None:
                limitations.append(
                    f"no {setting.metric} evidence during the referenced full-load period"
                )
            else:
                conditions[setting.metric] = mean
        peaks: dict[str, float] = {}
        for metric in history.peak_metrics:
            peak = self._peak_over(store, metric, latest)
            if peak is None:
                limitations.append(f"no {metric} evidence during the referenced full-load period")
            else:
                peaks[metric] = peak

        reference = FullLoadReference(
            started_at_ms=latest.started_at_ms,
            started_at=iso(latest.started_at_ms),
            ended_at_ms=latest.ended_at_ms,
            ended_at=iso(latest.ended_at_ms),
            covered_s=latest.covered_ms / 1000,
            bucket_count=latest.bucket_count,
            load_metric=history.load.metric,
            load_peak=latest.load_peak,
            load_mean=latest.load_mean,
            conditions=conditions,
            peaks=peaks,
        )
        return _History(reference, len(episodes), now_ms, tuple(limitations))

    def _episodes(self, buckets: Sequence[RollupPoint], bucket_ms: int) -> list[_Episode]:
        """観測された電力だけからフルロード期間を切り出す（I-3 / I-4 / I-5）。"""
        load = self._settings.history.load
        join_ms = bucket_ms + load.max_gap_s * 1000
        runs: list[list[RollupPoint]] = []
        broken = True
        for point in sorted(buckets, key=lambda item: item.bucket_ms):
            if point.mean_value is None:
                # quality=ok の値が無いバケットは「観測できなかった」＝欠落と同じ扱い
                continue
            if point.ok_value_count < load.min_ok_samples:
                # 数サンプルしか届かなかったバケットを5分ぶんの根拠に数えない（I-2）。
                # 負荷が下がった証拠でもないので、値の高低によらず欠落と同じく
                # 切りも数えもしない
                continue
            if point.mean_value < load.min_value:
                # 閾値未満を**十分に観測した**バケットは欠落ではない。期間をここで必ず切る
                # （欠落として繋ぐと、途切れた負荷を連続したフルロードに数えてしまう）
                broken = True
                continue
            if not broken and point.bucket_ms - runs[-1][-1].bucket_ms <= join_ms:
                runs[-1].append(point)
            else:
                runs.append([point])
            broken = False

        episodes: list[_Episode] = []
        for run in runs:
            # 継続時間は「観測できたバケットの合計」。欠落を跨いで繋いでも、
            # 跨いだ時間は根拠として数えない（I-4）
            covered_ms = len(run) * bucket_ms
            if covered_ms < load.min_duration_s * 1000:
                continue
            weights = sum(point.ok_value_count for point in run)
            episodes.append(
                _Episode(
                    started_at_ms=run[0].bucket_ms,
                    ended_at_ms=run[-1].bucket_ms + bucket_ms,
                    bucket_count=len(run),
                    covered_ms=covered_ms,
                    load_peak=max(
                        (point.max_value for point in run if point.max_value is not None),
                        default=None,
                    ),
                    load_mean=(
                        round(
                            sum(
                                point.mean_value * point.ok_value_count
                                for point in run
                                if point.mean_value is not None
                            )
                            / weights,
                            3,
                        )
                        if weights > 0
                        else None
                    ),
                )
            )
        return episodes

    def _points(self, store: SqliteStore, metric: str, episode: _Episode) -> list[RollupPoint]:
        points = store.aggregate(
            metric,
            episode.started_at_ms,
            episode.ended_at_ms,
            self._settings.history.aggregation,
        )
        # quality=ok の値が1つも無いバケットは根拠にしない（I-2）
        return [point for point in points if point.ok_value_count > 0]

    def _mean_over(self, store: SqliteStore, metric: str, episode: _Episode) -> float | None:
        points = [
            point for point in self._points(store, metric, episode) if point.mean_value is not None
        ]
        weights = sum(point.ok_value_count for point in points)
        if weights <= 0:
            return None
        total = sum(
            point.mean_value * point.ok_value_count
            for point in points
            if point.mean_value is not None
        )
        return round(total / weights, 3)

    def _peak_over(self, store: SqliteStore, metric: str, episode: _Episode) -> float | None:
        return max(
            (
                point.max_value
                for point in self._points(store, metric, episode)
                if point.max_value is not None
            ),
            default=None,
        )


def _monitoring_warnings(
    sources: HealthSources,
    alerts: Sequence[AlertRecord],
    firing: Mapping[AlertSeverity, int],
) -> list[str]:
    """#66 から引き継いだ、情報源とアラートの警告（決定記録 0042 §2.5）。"""
    warnings = [
        f"{name} source is {source.status.value}"
        for name, source in (
            ("sensor_unit", sources.sensor_unit),
            ("nvml", sources.nvml),
            ("lm_sensors", sources.lm_sensors),
        )
        if source.status is not HealthSourceStatus.OK
    ]
    warnings.extend(f"active alert: {alert.rule_id} ({alert.severity.value})" for alert in alerts)
    listed = Counter(alert.severity for alert in alerts)
    unlisted = {
        severity: firing.get(severity, 0) - listed.get(severity, 0) for severity in AlertSeverity
    }
    if any(count > 0 for count in unlisted.values()):
        # 一覧から外れた古いアラートも、件数と重大度だけは警告に残す
        detail = ", ".join(
            f"{severity.value}={count}" for severity, count in unlisted.items() if count > 0
        )
        warnings.append(f"more active alerts not listed: {detail}")
    return warnings


def _condition_warnings(
    conditions: Sequence[AdvisoryCondition],
    settings: Sequence[ConditionSetting],
) -> list[str]:
    """環境条件の警告。設定に並べた順で、1条件につき最大1件。"""
    by_metric = {setting.metric: setting for setting in settings}
    warnings: list[str] = []
    for condition in conditions:
        if not condition.usable:
            age = "unknown" if condition.age_seconds is None else f"{condition.age_seconds:g}s"
            # 読めない条件を黙って飛ばさない（I-2）
            warnings.append(
                f"condition unavailable: {condition.metric} "
                f"(quality={condition.quality.value}, age={age})"
            )
            continue
        unit = condition.unit or ""
        if condition.exceeded and condition.advisory_max is not None:
            warnings.append(
                f"{condition.metric} {condition.value:g}{unit} is above the advisory "
                f"maximum {condition.advisory_max:g}{unit}"
            )
            continue
        warn_delta = by_metric[condition.metric].warn_delta
        if (
            warn_delta is not None
            and condition.delta is not None
            and condition.reference_value is not None
            # 設定の意味は「これ以上高ければ警告」。delta は payload と同じ丸めの値
            and condition.delta >= warn_delta
        ):
            warnings.append(
                f"{condition.metric} is {condition.delta:+g}{unit} versus the last observed "
                f"full load ({condition.reference_value:g}{unit})"
            )
    return warnings
