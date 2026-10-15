"""合成の起点の部品: Slack の発火の通知に直近 60 分のグラフを添える（#334 / 決定記録 0147）。

本文は `Router` の送信スレッドが `chat.postMessage` で既に送ってある。ここは**その後に**、
専用のスレッドと自分の DB の接続（0004 §2.8）で値を読み、PNG を描き、同じスレッドへ上げる。

- **待たない。** 依頼は有界の待ち行列へ `put_nowait`。溢れたら捨てて数える（0147 §2.2）
- **失敗は記録して捨てる。** 本文は届いている。再送しない
- **LLM を使わない。** 描く系列と閾値はルールとメトリクスの定義から機械的に決める（0147 §2.3）

取り込み（`coldaisle.daemon`）だけが組み立てる。API・AI 層・制御から import しない。
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coldaisle import logs
from coldaisle.clock import Clock
from coldaisle.measurement import Quality
from coldaisle.metrics import MetricCatalog
from coldaisle.notify.models import DisplayNames, Notification
from coldaisle.notify.png_chart import MAX_SERIES_PER_PANEL, Panel, Series, Threshold, render_png
from coldaisle.rules.models import (
    HotAndSlowRule,
    RangeRule,
    RuleSet,
    ThresholdRule,
)
from coldaisle.store.models import AlertSeverity, SeriesPoint

LOGGER = logging.getLogger("coldaisle.notify")

QUEUE_SIZE = 8
"""グラフの依頼の上限。**有界にする**（0147 §2.2）。"""

STOP_WAIT_S = 5.0
"""停止時に描きかけを待つ上限。グラフは送り切らない（本文は `Router` が送り切る）。"""

_GRAPHED_SEVERITIES = frozenset({AlertSeverity.CRITICAL, AlertSeverity.WARNING})

_COLOR_NAMES = ("青", "橙", "緑", "黄")
"""`png_chart.SERIES_COLORS` と同じ順の色の名前（コメントの凡例に使う）。"""


class GraphConfig(BaseModel):
    """`config/notify-graph.yaml`（0147 §2.7）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    window_s: float = Field(gt=0, le=86_400)
    gap_s: float = Field(gt=0)
    """これより離れた 2 点は結ばない。"""
    width: int = Field(ge=400, le=4000)
    panel_height: int = Field(ge=160, le=2000)
    max_points: int = Field(ge=10, le=100_000)


def load_graph_config(path: Path) -> GraphConfig | None:
    """設定を読む。**読めない・不正ならグラフだけを無効にする**（0147 §2.7）。

    監視と文章の通知はグラフより大事なので、起動は止めない。error を 1 度残す。
    どのアラートにも足すメトリクス（室温）はここに持たず、通知の `context_always` を使う。
    """
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = GraphConfig.model_validate(loaded)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        LOGGER.error(
            "グラフの設定が読めないので、通知にグラフを添えない（文章の通知は届く）",
            extra={logs.FIELDS_KEY: {"config": str(path), "reason": str(error)}},
        )
        return None
    return config if config.enabled else None


class SeriesReader(Protocol):
    """グラフに要る読み出しだけ（`SqliteStore` が満たす）。"""

    def series(
        self, metric: str, start_ms: int, end_ms: int, *, limit: int | None = None
    ) -> tuple[SeriesPoint, ...]: ...

    def close(self) -> None: ...


class Uploader(Protocol):
    """上げる先（`SlackApi` が満たす）。"""

    def upload_to_thread(
        self,
        *,
        channel: str,
        thread_ts: str,
        filename: str,
        content: bytes,
        title: str,
        comment: str,
    ) -> None: ...


@dataclass(frozen=True)
class GraphRequest:
    """グラフ 1 枚の依頼。`end_ms` は本文を送った時刻。"""

    rule_id: str
    metric: str
    channel: str
    thread_ts: str
    end_ms: int


@dataclass(frozen=True)
class GraphPlan:
    """描くもの。`metrics` は描く順、`thresholds` はメトリクスごとの線。"""

    metrics: tuple[str, ...]
    thresholds: dict[str, tuple[Threshold, ...]]


def plan_graph(rule_id: str, metric: str, *, rules: RuleSet, names: DisplayNames) -> GraphPlan:
    """描くメトリクスと閾値の線を決める（0147 §2.3）。

    描くメトリクスは通知の「現在値:」と**同じ選び方**（決定記録 0142 §2.2a の
    `DisplayNames.context_metrics`。アラートのメトリクス → ルールの残りのメトリクス →
    派生値の材料 → `config/notify.yaml` の `context_always`）。選び方を 2 か所に持たない。
    ルールの文が無い ID（知らないルール）は、メトリクス・その材料・`context_always` だけ。
    """
    selected = names.context_metrics(rule_id, metric)
    if selected is None:
        selected = list(
            dict.fromkeys([metric, *names.materials.get(metric, ()), *names.context_always])
        )
    return GraphPlan(metrics=tuple(selected), thresholds=_thresholds(_rule_for(rule_id, rules)))


def _rule_for(rule_id: str, rules: RuleSet) -> BaseModel | None:
    """`RAPID_RISE` → `rapid_rise` の定義。ルールの ID は定義の名前の大文字（エンジンと同じ）。"""
    name = rule_id.lower()
    if name not in type(rules).model_fields:
        return None
    rule = getattr(rules, name)
    return rule if isinstance(rule, BaseModel) else None


def _thresholds(rule: BaseModel | None) -> dict[str, tuple[Threshold, ...]]:
    """値の軸に引ける閾値だけ。変化率・flag・連続欠けは引かない（0147 §2.3）。"""
    if isinstance(rule, ThresholdRule):
        return {rule.metric: (Threshold(rule.threshold, f"thr {rule.threshold:g}"),)}
    if isinstance(rule, RangeRule):
        return {
            rule.metric: (
                Threshold(rule.low, f"low {rule.low:g}"),
                Threshold(rule.high, f"high {rule.high:g}"),
            )
        }
    if isinstance(rule, HotAndSlowRule):
        return {
            rule.core_metric: (Threshold(rule.core_threshold, f"thr {rule.core_threshold:g}"),),
            rule.fan_metric: (Threshold(rule.fan_below, f"thr {rule.fan_below:g}"),),
        }
    return {}


def read_points(
    reader: SeriesReader,
    metric: str,
    catalog: MetricCatalog,
    *,
    start_ms: int,
    end_ms: int,
    limit: int,
) -> tuple[tuple[int, float], ...]:
    """品質 `ok` の点だけ。

    派生値は保存しないので（0002 §2.2）、材料を同じ時刻で突き合わせて引く。
    """
    derived = catalog.derived.get(metric)
    if derived is None:
        return _ok_points(reader.series(metric, start_ms, end_ms, limit=limit))
    minuend = dict(_ok_points(reader.series(derived.minuend, start_ms, end_ms, limit=limit)))
    subtrahend = dict(_ok_points(reader.series(derived.subtrahend, start_ms, end_ms, limit=limit)))
    return tuple(
        (ts_ms, round(value - subtrahend[ts_ms], 3))
        for ts_ms, value in sorted(minuend.items())
        if ts_ms in subtrahend
    )


def _ok_points(points: Sequence[SeriesPoint]) -> tuple[tuple[int, float], ...]:
    return tuple(
        (point.ts_ms, float(point.value))
        for point in points
        if point.quality is Quality.OK and point.value is not None
    )


@dataclass(frozen=True)
class RenderedGraph:
    """上げる PNG と、日本語の凡例（ファイルのコメント）。"""

    png: bytes
    comment: str
    filename: str
    title: str


def render_alert_graph(
    request: GraphRequest,
    reader: SeriesReader,
    *,
    rules: RuleSet,
    catalog: MetricCatalog,
    names: DisplayNames,
    config: GraphConfig,
) -> RenderedGraph | None:
    """1 枚を描く。描く値が 1 点も無ければ `None`。"""
    plan = plan_graph(request.rule_id, request.metric, rules=rules, names=names)
    window_ms = round(config.window_s * 1000)
    start_ms = request.end_ms - window_ms
    # `series()` の範囲は [start, end)。送った時刻の点まで入れる
    end_exclusive = request.end_ms + 1
    by_unit: dict[str, list[tuple[str, tuple[tuple[int, float], ...]]]] = {}
    for metric in plan.metrics:
        points = read_points(
            reader,
            metric,
            catalog,
            start_ms=start_ms,
            end_ms=end_exclusive,
            limit=config.max_points * 4,
        )
        unit = catalog.unit_for(metric) or ""
        group = by_unit.setdefault(unit, [])
        if len(group) < MAX_SERIES_PER_PANEL:
            group.append((metric, points))
    if not any(points for group in by_unit.values() for _, points in group):
        return None

    panels: list[Panel] = []
    legend: list[str] = []
    threshold_texts: list[str] = []
    for unit, group in by_unit.items():
        drawn = [(metric, points) for metric, points in group if points]
        if not drawn:
            continue
        thresholds = tuple(t for metric, _ in drawn for t in plan.thresholds.get(metric, ()))
        panels.append(
            Panel(
                unit=unit,
                series=tuple(Series(metric, points) for metric, points in drawn),
                thresholds=thresholds,
            )
        )
        for color, (metric, _) in zip(_COLOR_NAMES, drawn, strict=False):
            legend.append(f"{names.metric(metric)}（{metric}）={color}")
        threshold_texts.extend(
            f"{names.metric(metric)} の閾値 {t.value:g}"
            for metric, _ in drawn
            for t in plan.thresholds.get(metric, ())
        )
    minutes = round(config.window_s / 60)
    comment = f"直近{minutes}分: " + "、".join(legend)
    if threshold_texts:
        comment += "／" + "、".join(threshold_texts) + " = 赤の破線"
    missing = [m for m in plan.metrics if not any(m == d for p in panels for d in _names(p))]
    if missing:
        comment += "／値が無い: " + "、".join(missing)
    png = render_png(
        panels,
        start_ms=start_ms,
        end_ms=request.end_ms,
        width=config.width,
        panel_height=config.panel_height,
        gap_ms=round(config.gap_s * 1000),
        max_points=config.max_points,
    )
    return RenderedGraph(
        png=png,
        comment=comment,
        filename=f"{request.rule_id.lower()}_{request.end_ms}.png",
        title=f"{request.rule_id} {request.metric} 直近{minutes}分",
    )


def _names(panel: Panel) -> tuple[str, ...]:
    return tuple(series.name for series in panel.series)


class AlertGraphWorker:
    """グラフの依頼を受けて、別のスレッドで描いて上げる（0147 §2.2）。

    `offer()` は `SlackBotNotifier` の `on_posted` として `Router` の送信スレッドから呼ばれる。
    **待たない。** DB の接続はこのスレッドが最初の依頼で開く（0004 §2.8）。
    """

    def __init__(
        self,
        *,
        uploader: Uploader,
        open_reader: Callable[[], SeriesReader],
        rules: RuleSet,
        catalog: MetricCatalog,
        names: DisplayNames,
        config: GraphConfig,
        clock: Clock,
        repeat_after_s: float,
    ) -> None:
        self._uploader = uploader
        self._open_reader = open_reader
        self._rules = rules
        self._catalog = catalog
        self._names = names
        self._config = config
        self._clock = clock
        self._repeat_after_ms = round(repeat_after_s * 1000)
        self._last_graph_ms: dict[tuple[str, str], int] = {}
        """`(ルール, メトリクス)` → 上げた（または上げている途中の）グラフの時刻。

        **抑えるのは実際に上げたグラフだけ**（0147 §2.4）。依頼を積んだ時点で予約し、
        描けない・上げられない・停止で捨てたときは予約を外す（`_release`）。外さないと、
        失敗した 1 枚のせいで `repeat_after_s` の間の次の発火にもグラフが付かない。
        """
        self._lock = threading.Lock()
        self._queue: queue.Queue[GraphRequest | None] = queue.Queue(QUEUE_SIZE)
        self._thread: threading.Thread | None = None
        self._reader: SeriesReader | None = None
        self.dropped = 0
        self.uploaded = 0
        self.failed = 0

    # ------------------------------------------------------------------ 受付

    def offer(self, notification: Notification, channel: str, thread_ts: str) -> bool:
        """添える条件（0147 §2.4）に合えば依頼を積む。積んだら `True`。"""
        if (
            notification.kind != "alert"
            or notification.state != "firing"
            or notification.severity not in _GRAPHED_SEVERITIES
            or notification.metric is None
        ):
            return False
        key = (notification.rule_id, notification.metric)
        now_ms = self._clock.now_ms()
        with self._lock:
            last = self._last_graph_ms.get(key)
            if last is not None and now_ms - last < self._repeat_after_ms:
                return False
            request = GraphRequest(
                rule_id=notification.rule_id,
                metric=notification.metric,
                channel=channel,
                thread_ts=thread_ts,
                end_ms=now_ms,
            )
            try:
                self._queue.put_nowait(request)
            except queue.Full:
                self.dropped += 1
                LOGGER.warning(
                    "グラフの待ち行列が溢れた（本文は届いている）",
                    extra={logs.FIELDS_KEY: {"dropped": self.dropped}},
                )
                return False
            self._last_graph_ms[key] = now_ms  # 予約。上げられなければ `_release` で外す
        if self._thread is None:
            self.drain()  # スレッドを起こしていない場合はその場で描く（テスト用）
        return True

    # ------------------------------------------------------------------ スレッド

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="alert-graph", daemon=True)
            self._thread.start()

    def stop(self, *, timeout_s: float = STOP_WAIT_S) -> None:
        """**送り切らない。** 描きかけを最長 `timeout_s` だけ待ち、残りは捨てる（0147 §2.2）。"""
        thread = self._thread
        if thread is None:
            self._close_reader()
            return
        # 積まれている依頼は捨ててから止める（停止を遅らせない）
        while True:
            try:
                discarded = self._queue.get_nowait()
            except queue.Empty:
                break
            if discarded is not None:
                self._release(discarded)
        with contextlib.suppress(queue.Full):  # 直前に空にしているので普通は起きない
            self._queue.put(None, timeout=timeout_s)
        thread.join(timeout=timeout_s)
        if thread.is_alive():  # pragma: no cover - 上げる途中で詰まったとき
            LOGGER.warning("グラフを上げる途中で停止した（本文は届いている）")
            return
        self._thread = None

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                self._handle(item)
        finally:
            self._close_reader()

    def drain(self) -> None:
        """積まれた依頼をその場で処理する（スレッドを起こしていないとき）。"""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                self._handle(item)

    def _handle(self, request: GraphRequest) -> None:
        fields = {"rule": request.rule_id, "metric": request.metric}
        try:
            if self._reader is None:
                self._reader = self._open_reader()
            graph = render_alert_graph(
                request,
                self._reader,
                rules=self._rules,
                catalog=self._catalog,
                names=self._names,
                config=self._config,
            )
            if graph is None:
                LOGGER.info("描く値が無いのでグラフを添えない", extra={logs.FIELDS_KEY: fields})
                self._release(request)
                return
            self._uploader.upload_to_thread(
                channel=request.channel,
                thread_ts=request.thread_ts,
                filename=graph.filename,
                content=graph.png,
                title=graph.title,
                comment=graph.comment,
            )
        except Exception:
            # 例外の文はトークン・上げ先の URL を含まない（DeliveryError。0147 §2.6）
            self.failed += 1
            self._release(request)
            LOGGER.warning(
                "グラフを添えられなかった（本文は届いている）",
                exc_info=True,
                extra={logs.FIELDS_KEY: fields},
            )
            return
        self.uploaded += 1

    def _release(self, request: GraphRequest) -> None:
        """上げられなかった依頼の予約を外す。**その依頼の予約のときだけ**（後の予約を消さない）。"""
        key = (request.rule_id, request.metric)
        with self._lock:
            if self._last_graph_ms.get(key) == request.end_ms:
                del self._last_graph_ms[key]

    def _close_reader(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            finally:
                self._reader = None
