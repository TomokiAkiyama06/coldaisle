"""読み取り専用ツール（L3）。#22

**LLM に書き込み・実行の権限を与えない**（AGENTS.md ルール1 / FR-503）。
定義するのは5つだけで、いずれも読み取り。任意 SQL も受け付けない。
`subprocess` / `eval` はこの層に存在しない（テストで機械的に確かめている）。

**生の時系列をモデルへ渡さない**（FR-504）。集計は SQL 側で済ませ、
モデルには言語化だけをさせる。長大な数値テーブルに対する算術は不安定で、
「2万行のCSVを渡して最大値を聞く」は誤答する。

**デバイス由来の文字列はデータとして囲む**（要件 §7.4）。`err` やアラートの
`detail` は外部由来であり、指示として読まれてはいけない。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coldaisle import logs
from coldaisle.clock import Clock
from coldaisle.metrics import MetricCatalog, compute_derived
from coldaisle.rules.models import RuleSet
from coldaisle.store import Aggregation, SqliteStore
from coldaisle.store.db import MINUTE_MS
from coldaisle.store.models import validate_metric

LOGGER = logging.getLogger("coldaisle.ai.tools")

MAX_SERIES_POINTS = 200
"""1回の応答で返す最大点数（要件 §7.3）。**超えるぶんは粗い粒度へ落とす。**"""

MAX_TEXT = 500
"""デバイス由来の文字列を切り詰める長さ。長文を投げ込まれても文脈を埋めない。"""

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

WINDOW_PATTERN = re.compile(r"^(\d+)([smhd])$")
_WINDOW_UNITS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def as_data(text: str | None) -> str | None:
    """外部由来の文字列を**データとして**囲む（要件 §7.4）。

    `err` はデバイスが作った文字列であり、モデルへの指示ではない。
    囲みタグ自体を書かれても閉じられないよう、`<` と `>` を落とす。
    """
    if text is None:
        return None
    cleaned = CONTROL_CHARS.sub("", text).replace("<", "＜").replace(">", "＞")
    return f"<data>{cleaned[:MAX_TEXT]}</data>"


class _Args(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SeriesArgs(_Args):
    metric: str
    from_ms: int | None = Field(default=None, alias="from")
    to_ms: int | None = Field(default=None, alias="to")
    window: str | None = None
    agg: str | None = None


class StatsArgs(_Args):
    metric: str
    window: str = "1h"


class AlertsArgs(_Args):
    from_ms: int | None = Field(default=None, alias="from")
    to_ms: int | None = Field(default=None, alias="to")
    state: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class EmptyArgs(_Args):
    pass


DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_latest",
            "description": "全メトリクスの現在値・派生値・品質フラグを返す。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_series",
            "description": (
                f"時系列を**集計して**返す。各点はバケットの平均・最小・最大で、"
                f"生の測定値は返さない。最大 {MAX_SERIES_POINTS} 点へ自動で粗くする。"
                "期間は from/to（Unix ミリ秒）か window（例: 6h）で指定する。"
                "from だけなら現在まで、to だけなら window ぶん遡る。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "例: air.gpu_intake"},
                    "from": {"type": "integer"},
                    "to": {"type": "integer"},
                    "window": {"type": "string", "description": "例: 30m / 6h / 7d"},
                    # **生は選べない。** 集計しない系列をモデルへ渡さない（FR-504）
                    "agg": {"type": "string", "enum": ["1m", "5m", "1h"]},
                },
                "required": ["metric"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stats",
            "description": "期間の min/max/mean/p95/傾き/欠測率を返す。数値の集計はこれを使う。",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "window": {"type": "string", "description": "例: 1h / 24h / 7d"},
                },
                "required": ["metric"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_alerts",
            "description": "アラートの履歴を新しい順に返す。",
            "parameters": {
                "type": "object",
                "properties": {
                    "from": {"type": "integer"},
                    "to": {"type": "integer"},
                    "state": {"type": "string", "enum": ["pending", "firing", "resolved"]},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_system",
            "description": "センサー配置・メトリクスの意味・閾値の要約を返す。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]
"""OpenAI function calling 形式の定義。**書き込み・実行系は無い。**"""


@dataclass
class ToolRegistry:
    """ツールの実行時。**存在しないツールを呼ばれても落ちない。**"""

    store: SqliteStore
    catalog: MetricCatalog
    rules: RuleSet
    clock: Clock

    def definitions(self) -> list[dict[str, Any]]:
        return DEFINITIONS

    def call(self, name: str, arguments: str | dict[str, Any] | None = None) -> dict[str, Any]:
        """1つ実行して結果を返す。**例外を上へ投げない。**

        モデルは存在しないツールや壊れた引数を寄こす。そこで落ちると、
        会話が続かないだけでなく、呼び出し側（API / デーモン）まで巻き込む。
        """
        try:
            parsed = self._parse_arguments(arguments)
        except ValueError as error:
            return {"error": f"引数が JSON ではない: {error}"}

        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {
                "error": f"そのツールは無い: {name}",
                "available": [d["function"]["name"] for d in DEFINITIONS],
            }
        try:
            result: dict[str, Any] = handler(parsed)
            return result
        except ValidationError as error:
            return {"error": "引数が不正", "detail": error.errors(include_url=False)[:3]}
        except ValueError as error:
            return {"error": str(error)}
        except Exception as error:
            LOGGER.warning(
                "ツールの実行に失敗した", exc_info=True, extra={logs.FIELDS_KEY: {"tool": name}}
            )
            return {"error": f"実行に失敗した: {type(error).__name__}: {error}"}

    # ------------------------------------------------------------------ 各ツール

    def _tool_get_latest(self, arguments: dict[str, Any]) -> dict[str, Any]:
        EmptyArgs.model_validate(arguments)
        readings = self.store.latest()
        return {
            "ts_ms": max((r.ts_ms for r in readings.values()), default=None),
            "metrics": {
                metric: {
                    "value": reading.value,
                    "unit": self.catalog.unit_for(metric),
                    "quality": reading.quality.value,
                    "age_seconds": round(reading.age_ms / 1000, 1),
                }
                for metric, reading in sorted(readings.items())
            },
            "derived": compute_derived(readings, self.catalog),
        }

    def _tool_query_series(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """**必ず集計して返す。** 生の測定値は1点も渡さない。

        AGENTS.md のルール5（生の時系列を LLM のプロンプトに直接入れない）は
        点数の上限とは別の話である。200点に切り詰めても、
        中身が個々の測定値なら「生の時系列」のままになる。
        """
        args = SeriesArgs.model_validate(arguments)
        validate_metric(args.metric)
        start_ms, end_ms = self._range(args.from_ms, args.to_ms, args.window)
        agg = self._choose_agg(args.agg, end_ms - start_ms)
        buckets = self.store.aggregate(args.metric, start_ms, end_ms, agg, limit=MAX_SERIES_POINTS)
        return {
            "metric": args.metric,
            "unit": self.catalog.unit_for(args.metric),
            "agg": agg.value,
            "from_ms": start_ms,
            "to_ms": end_ms,
            "point_count": len(buckets),
            "note": "各点はバケットの集計値。生の測定値は返さない（FR-504）",
            "points": [
                {
                    "ts_ms": bucket.bucket_ms,
                    "mean": bucket.mean_value,
                    "min": bucket.min_value,
                    "max": bucket.max_value,
                    "missing_ratio": bucket.missing_ratio,
                }
                for bucket in buckets
            ],
        }

    def _tool_get_stats(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = StatsArgs.model_validate(arguments)
        validate_metric(args.metric)
        start_ms, end_ms = self._range(None, None, args.window)
        stats = self.store.stats(args.metric, start_ms, end_ms)
        return {
            "metric": args.metric,
            "unit": self.catalog.unit_for(args.metric),
            "window": args.window,
            "from_ms": stats.start_ms,
            "to_ms": stats.end_ms,
            "sample_count": stats.ok_value_count,
            "min": stats.min_value,
            "max": stats.max_value,
            "mean": stats.mean_value,
            "p95": stats.p95_value,
            "slope_per_min": stats.slope_per_min,
            "missing_ratio": stats.missing_ratio,
        }

    def _tool_list_alerts(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = AlertsArgs.model_validate(arguments)
        alerts = self.store.alerts(
            state=args.state, start_ms=args.from_ms, end_ms=args.to_ms, limit=args.limit
        )
        return {
            "alerts": [
                {
                    "rule_id": alert.rule_id,
                    "severity": alert.severity.value,
                    "state": alert.state.value,
                    "metric": alert.metric,
                    "started_ms": alert.started_ms,
                    "fired_ms": alert.fired_ms,
                    "resolved_ms": alert.resolved_ms,
                    "trigger_value": alert.trigger_value,
                    "threshold": alert.threshold,
                    # ルール由来の文字列。**データとして囲む**（要件 §7.4）
                    "detail": as_data(alert.detail),
                }
                for alert in alerts
            ]
        }

    def _tool_describe_system(self, arguments: dict[str, Any]) -> dict[str, Any]:
        EmptyArgs.model_validate(arguments)
        devices = [
            {
                # `dev` / `fw` / `kind` はデバイスが名乗る文字列。
                # **指示として読まれないよう囲む**（要件 §7.4）
                "device_id": as_data(str(row[0])),
                "fw": as_data(None if row[1] is None else str(row[1])),
                "interval_ms": row[2],
                "sensors": [
                    {
                        "channel": as_data(sensor.channel),
                        "kind": as_data(sensor.kind),
                        "gpio": sensor.gpio,
                        # **ROM は伏せる。** 環境固有の識別子をモデルへ出さない（#41）
                        "rom_suffix": None if sensor.rom is None else sensor.rom[-4:],
                        "resolution": sensor.resolution,
                    }
                    for sensor in self.store.sensors_for(str(row[0]))
                ],
            }
            for row in self.store.connection.execute(
                "SELECT device_id, fw, interval_ms FROM devices ORDER BY device_id"
            )
        ]
        return {
            "devices": devices,
            "metrics": {
                name: {"unit": meta.unit, "label": meta.label}
                for name, meta in sorted(self.catalog.metrics.items())
            },
            "derived": {
                name: {
                    "unit": meta.unit,
                    "label": meta.label,
                    "formula": f"{meta.minuend} - {meta.subtrahend}",
                }
                for name, meta in sorted(self.catalog.derived.items())
            },
            "thresholds": self._thresholds(),
            "note": (
                "閾値はすべて暫定値であり、実測（#19）で確定する。"
                "派生値は保存せず参照のたびに計算する。"
            ),
        }

    # ------------------------------------------------------------------ 内部

    def _thresholds(self) -> dict[str, Any]:
        """設定された項目を**そのまま全部**出す。

        選んで載せると、`SENSOR_FAULT` の無音秒数や `SENSOR_MISSING` の連続回数の
        ように「そのルールの発火条件そのもの」が抜ける。
        抜けたまま説明させると、モデルは無い情報を埋めようとする。
        """
        return {name.upper(): rule.model_dump(mode="json") for name, rule in self.rules}

    def _range(self, from_ms: int | None, to_ms: int | None, window: str | None) -> tuple[int, int]:
        """期間を決める。**片側だけの指定を黙って捨てない。**

        捨てて既定の1時間に落とすと、**要求と無関係な区間の結果を
        「成功」として返す**ことになる。次のように解く（返り値にも入れる）。

        | 指定 | 解釈 |
        |---|---|
        | from と to | そのまま |
        | from だけ | from 〜 現在 |
        | to だけ | to から window ぶん遡る |
        | どちらも無い | 現在から window ぶん遡る |
        """
        if from_ms is not None and to_ms is not None:
            if from_ms > to_ms:
                raise ValueError(f"範囲が逆転している: from={from_ms} > to={to_ms}")
            return from_ms, to_ms
        if from_ms is not None:
            end = self.clock.now_ms()
            if from_ms > end:
                raise ValueError(f"from が未来: from={from_ms}")
            return from_ms, end
        matched = WINDOW_PATTERN.match(window or "1h")
        if matched is None:
            raise ValueError(f"window の書式が不正: {window!r}（例: 30m, 6h, 7d）")
        end = self.clock.now_ms() if to_ms is None else to_ms
        return end - int(matched.group(1)) * _WINDOW_UNITS[matched.group(2)], end

    def _choose_agg(self, requested: str | None, span_ms: int) -> Aggregation:
        """点数が上限に収まるいちばん細かい粒度を選ぶ。

        **候補に生は入らない**（2.2）。いちばん細かくても1分バケットである。
        要求より粗くはするが細かくはしない（決定記録 0009 §2.4 と同じ）。
        """
        order = [
            (Aggregation.MINUTE, MINUTE_MS),
            (Aggregation.FIVE_MINUTES, 5 * MINUTE_MS),
            (Aggregation.HOUR, 60 * MINUTE_MS),
        ]
        start = 0
        if requested is not None and requested != Aggregation.RAW.value:
            wanted = Aggregation(requested)
            start = next(i for i, (agg, _) in enumerate(order) if agg is wanted)
        for agg, step in order[start:]:
            if span_ms // step <= MAX_SERIES_POINTS:
                return agg
        return Aggregation.HOUR

    @staticmethod
    def _parse_arguments(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
        if arguments is None or arguments == "":
            return {}
        if isinstance(arguments, dict):
            return arguments
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError(f"引数が辞書ではない: {parsed!r}")
        return parsed
