"""通知の設定と本文（#20）。

**秘匿情報は設定ファイルに置かない。** Webhook URL とトークンは環境変数から読む
（リポジトリは public。#41 / 決定 Q-06）。導入先のダッシュボードのアドレスも同じで、
`COLDAISLE_DASHBOARD_URL` で `dashboard_url` を上書きできる（#307）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle.metrics import MetricCatalog
from coldaisle.rules.models import RuleSet, rule_metrics
from coldaisle.store.models import AlertSeverity

DASHBOARD_URL_ENV = "COLDAISLE_DASHBOARD_URL"
"""通知の本文のダッシュボードのリンクを上書きする環境変数（#307）。

`config/notify.yaml` の既定はサーバー自身から見たアドレスで、Slack を別の端末で開くと
届かない。導入先の実際のアドレスは public なリポジトリに書けない（AGENTS.md ルール 10）ため、
導入先の環境変数（`/etc/coldaisle/coldaisle.env`）に置く。**空文字は未設定と同じに扱う**
（`.env.example` を写したままの空の行で既定を潰さない）。
"""

_DASHBOARD_URL_SCHEMES = frozenset({"http", "https"})


def _check_dashboard_url(value: str, *, source: str) -> str:
    """http / https の絶対 URL でホストがあることを確かめる。**起動時に落とす。**

    遅らせると、壊れたリンクが最初の通知で初めて分かる（それも Slack を開いた人にだけ）。
    例外の文に値そのものは入れない。導入先のアドレスや URL に埋めた資格情報が
    ログに残らないようにする（#305 と同じ考え方）。
    """
    if not value or any(ch.isspace() for ch in value):
        raise ValueError(f"{source}: ダッシュボードの URL が空か、空白を含む")
    try:
        parts = urlsplit(value)
        _ = parts.port  # 数字でないポートはここで ValueError になる
    except ValueError:
        # 元の例外の文には値が入りうるので連鎖を切る
        raise ValueError(f"{source}: ダッシュボードの URL を解釈できない") from None
    if parts.scheme.lower() not in _DASHBOARD_URL_SCHEMES:
        raise ValueError(f"{source}: ダッシュボードの URL は http か https の絶対 URL にする")
    if not parts.hostname:
        raise ValueError(f"{source}: ダッシュボードの URL にホストが無い")
    return value


class NightPolicy(BaseModel):
    """夜間の通知方針（決定記録 0001 D-04）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=23)
    timezone: str
    warning_enabled: bool
    """**ベースライン測定（#19）が終わるまで false。**

    夜間も通知する以上、誤警報が睡眠を削る。
    「通知が信用されなくなる」より深刻な実害である。
    """

    @property
    def zone(self) -> ZoneInfo:
        """**読み込み時に検証済み。** 綴り違いをここで初めて知ることにしない。

        遅らせると、最初の warning の遷移で例外になる。その例外は取り込み側で
        「1サンプルの破棄」として記録され、**そのアラートは二度と通知されない**
        （発火の遷移はもう出ないため）。
        """
        return ZoneInfo(self.timezone)

    def is_night(self, at_ms: int) -> bool:
        hour = datetime.fromtimestamp(at_ms / 1000, tz=self.zone).hour
        if self.start_hour == self.end_hour:
            return False
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour


class NotifyConfig(BaseModel):
    """`config/notify.yaml` 全体。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    routing: dict[AlertSeverity, list[str]]
    repeat_after_s: float = Field(gt=0)
    night: NightPolicy
    dashboard_url: str
    context_always: tuple[str, ...]
    """`現在値:` の行に**常に**最後に付けるメトリクス（決定記録 0142 §2.2a。既定の設定は室温）。

    省略を許すと、書き漏らしが「室温が出ない」として静かに通る。
    """

    @classmethod
    def from_yaml(cls, path: Path, *, environ: Mapping[str, str] | None = None) -> NotifyConfig:
        """設定を読み、`COLDAISLE_DASHBOARD_URL` があれば `dashboard_url` を置き換える。

        `environ` は試験用。省くと `os.environ` を読む。
        """
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"通知の設定が辞書ではない: {path}")
        override = (os.environ if environ is None else environ).get(DASHBOARD_URL_ENV, "")
        if override:
            loaded = {
                **loaded,
                "dashboard_url": _check_dashboard_url(override, source=DASHBOARD_URL_ENV),
            }
        elif isinstance(loaded.get("dashboard_url"), str):
            _check_dashboard_url(loaded["dashboard_url"], source=f"{path} の dashboard_url")
        config = cls.model_validate(loaded)
        try:
            _ = config.night.zone  # 綴り違いは起動時に落とす
        except Exception as error:
            raise ValueError(f"タイムゾーンが不正: {config.night.timezone!r}") from error
        missing = set(AlertSeverity) - set(config.routing)
        if missing:
            # 宛先の書き漏らしは「その重大度だけ届かない」として静かに通る
            raise ValueError(f"宛先が定義されていない重大度: {sorted(m.value for m in missing)}")
        return config


_FIRING_MARKS: dict[AlertSeverity, str] = {
    AlertSeverity.CRITICAL: "🔴",
    AlertSeverity.WARNING: "🟠",
    AlertSeverity.INFO: "🔵",
}
"""発火の記号。重大度ごとに変える（決定記録 0135 §2.2）。解除は重大度によらず ✅。"""

SEVERITY_LABELS: dict[AlertSeverity, str] = {
    AlertSeverity.CRITICAL: "重大",
    AlertSeverity.WARNING: "警告",
    AlertSeverity.INFO: "情報",
}
"""重大度の日本語（決定記録 0142 §2.1）。通知の先頭の行と日次レポートの表で使う。"""

_UNIT_DISPLAY: dict[str, str] = {"C": "°C", "flag": "", "count": ""}
"""`config/metrics.yaml` の `unit` を人が読む本文でだけ写す（決定記録 0142 §2.4）。

API の `unit` は変えない。表に無い単位はそのまま出す。空文字は単位を付けない。
"""

_LOOPBACK_HOSTS = frozenset({"localhost", "::1"})


def display_unit(unit: str | None) -> str:
    """本文に出す単位。**無い・付けないものは空文字。**"""
    if unit is None:
        return ""
    return _UNIT_DISPLAY.get(unit, unit)


def _num(value: float, unit: str, *, sign: bool = False) -> str:
    """小数 1 桁と単位（決定記録 0142 §2.4）。"""
    number = f"{value:+.1f}" if sign else f"{value:.1f}"
    return f"{number} {unit}" if unit else number


def is_reachable_link(url: str) -> bool:
    """別の端末から開けるリンクか。**ループバックのアドレスは出さない**（決定記録 0142 §2.2）。

    既定の `dashboard_url` はサーバー自身から見たアドレスで、スマホの Slack から開けない（#307）。
    `COLDAISLE_DASHBOARD_URL` を設定したときだけ本文に載る。
    """
    host = (urlsplit(url).hostname or "").lower()
    return not (host in _LOOPBACK_HOSTS or host.startswith("127."))


@dataclass(frozen=True)
class RuleText:
    """1 ルールの人が読む文（`config/rules.yaml`。決定記録 0142 §2.3）と、値の読み方。"""

    label: str
    """短い名前。日次レポートの表に出る。"""
    title: str
    """発火の見出しの文（例「GPU の吸気温度が急上昇しています」）。"""
    resolved_title: str
    """解除の見出しの文（例「GPU の吸気温度の急上昇が収まりました」）。"""
    hint: str
    """確かめることの一文。発火の本文にだけ出る。"""
    kind: str
    """遷移の `value` の意味（`RuleSet.value_kinds()`）。"""
    metrics: tuple[str, ...] = ()
    """定義が参照するメトリクス。"""
    threshold: float | None = None
    low: float | None = None
    high: float | None = None
    below: float | None = None


@dataclass(frozen=True)
class DisplayNames:
    """ID → 人が読む文と名前（決定記録 0142）。**設定の辞書を引くだけ。LLM を使わない。**

    空のまま作ると、ルールの文を持たない形（ID と渡された現在値の全部）で出す。
    """

    rules: Mapping[str, RuleText] = field(default_factory=dict)
    """`RULE_ID` → 文。"""
    metrics: Mapping[str, str] = field(default_factory=dict)
    """メトリクス → 名前（`config/metrics.yaml` の `label`）。"""
    units: Mapping[str, str] = field(default_factory=dict)
    """メトリクス → `config/metrics.yaml` の `unit`（写す前の値）。"""
    materials: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """派生値 → 材料（`config/metrics.yaml` の `derived` の `minuend` と `subtrahend`）。"""
    context_always: tuple[str, ...] = ()
    """現在値の最後に常に付けるメトリクス（`config/notify.yaml` の `context_always`）。"""

    @classmethod
    def build(
        cls, *, rules: RuleSet, catalog: MetricCatalog, context_always: tuple[str, ...]
    ) -> DisplayNames:
        """`config/rules.yaml`・`config/metrics.yaml`・`config/notify.yaml` から作る。

        `context_always` に `config/metrics.yaml` に無い名前があれば落とす（綴り違いで
        室温が黙って出なくなるのを起動時に見つける）。
        """
        labels = {name: meta.label for name, meta in catalog.metrics.items()}
        labels.update({name: meta.label for name, meta in catalog.derived.items()})
        units = {name: meta.unit for name, meta in catalog.metrics.items()}
        units.update({name: meta.unit for name, meta in catalog.derived.items()})
        unknown = [name for name in context_always if name not in labels]
        if unknown:
            raise ValueError(f"context_always に未知のメトリクス: {unknown}（config/metrics.yaml）")
        kinds = rules.value_kinds()
        texts = {
            name.upper(): RuleText(
                label=rule.label,
                title=rule.title,
                resolved_title=rule.resolved_title,
                hint=rule.hint,
                kind=kinds[name.upper()],
                metrics=rule_metrics(rule),
                threshold=getattr(rule, "threshold", None),
                low=getattr(rule, "low", None),
                high=getattr(rule, "high", None),
                below=getattr(rule, "fan_below", None),
            )
            for name, rule in rules
        }
        return cls(
            rules=texts,
            metrics=labels,
            units=units,
            materials={
                name: (meta.minuend, meta.subtrahend) for name, meta in catalog.derived.items()
            },
            context_always=tuple(context_always),
        )

    def metric(self, metric: str) -> str:
        return self.metrics.get(metric, metric)

    def unit(self, metric: str) -> str:
        return display_unit(self.units.get(metric))

    def reading(self, metric: str, value: float | None) -> str:
        """`<名前> <値> <単位>`。値が無ければ `—`。"""
        shown = "—" if value is None else _num(value, self.unit(metric))
        return f"{self.metric(metric)} {shown}"

    def context_metrics(self, rule_id: str, metric: str | None) -> list[str] | None:
        """現在値に出すメトリクスとその順（決定記録 0142 §2.2a）。`None` は絞らない。

        アラートのメトリクス → ルールの定義が参照する残りのメトリクス → それぞれが派生値なら
        その材料 → `context_always`。重複は先に出たほうだけを残す。**手書きの一覧を持たない。**
        """
        text = self.rules.get(rule_id)
        if text is None:
            return None
        targets = [metric] if metric else []
        targets += [name for name in text.metrics if name not in targets]
        ordered: list[str] = []
        for name in targets:
            ordered.append(name)
            ordered += self.materials.get(name, ())
        ordered += self.context_always
        return list(dict.fromkeys(ordered))


@dataclass(frozen=True)
class Notification:
    """1通ぶん。**本文は宛先ごとに組み立て直さない。**"""

    rule_id: str
    severity: AlertSeverity
    state: str
    """`firing` か `resolved`。"""
    metric: str | None
    value: float | None
    detail: str | None
    dashboard_url: str
    context: dict[str, float | None] = field(default_factory=dict)
    """関連するメトリクスの現在値。**何が起きているかを本文だけで判断できるように。**"""
    kind: str = "alert"
    """`alert` / `explanation`（#38）/ `report`（#25）。

    `explanation` は発火の通知に**後から続く**説明。`report` は日次レポートで、
    `Router` を通さず宛先へ直接送る（`coldaisle.report.send`）。
    """
    body: str = ""
    """本文をそのまま渡す場合に使う（説明の全文）。"""
    names: DisplayNames = field(default_factory=DisplayNames)
    """ID → 人が読む文と名前（決定記録 0142）。空なら ID のまま出す。"""

    @property
    def title(self) -> str:
        """先頭の行。**重大度を必ず載せる。**

        critical と warning は同じ Slack のチャンネルへ届く（決定記録 0135）ため、
        宛先では見分けられない。記号と `[重大度]` で一目で分かるようにする。
        語と文は日本語（決定記録 0142 §2.1）。ID は最後の行（`id_line`）に残す。
        """
        if self.kind == "report":
            return f"📄 {self.rule_id}"
        severity = SEVERITY_LABELS[self.severity]
        firing = self.state != "resolved"
        sentence = self._sentence(firing=firing or self.kind == "explanation")
        if self.kind == "explanation":
            return f"🔎 [{severity}] 説明: {sentence}"
        if firing:
            return f"{_FIRING_MARKS[self.severity]} [{severity}] {sentence}"
        return f"✅ [{severity} 解除] {sentence}"

    def _sentence(self, *, firing: bool) -> str:
        """見出しの文。メトリクスの括弧は、**ルールの定義がメトリクスを決めていない**ときだけ付ける。

        `SENSOR_MISSING` はアラートごとにメトリクスが違うので「（トップ排気）」を付ける。
        `RAPID_RISE` の文は対象（GPU の吸気）を含むので重ねない（決定記録 0142 §2.1）。
        """
        text = self.names.rules.get(self.rule_id)
        if text is None:
            target = f"（{self.metric}）" if self.metric else ""
            return f"{self.rule_id}{target} {'発生' if firing else '解除'}"
        sentence = text.title if firing else text.resolved_title
        if self.metric and self.metric not in text.metrics:
            sentence += f"（{self.names.metric(self.metric)}）"
        return sentence

    @property
    def id_line(self) -> str:
        """最後の行。ログ・DB・設定と突き合わせるための ID（決定記録 0142 §2.2）。"""
        return f"{self.rule_id} / {self.metric}" if self.metric else self.rule_id

    def as_text(self) -> str:
        """人が読む本文。宛先によらず同じ内容にする。"""
        link = [self.dashboard_url] if is_reachable_link(self.dashboard_url) else []
        if self.kind == "report":
            return "\n".join([self.title, "", self.body, *link])
        if self.body:
            return "\n".join([self.title, "", self.body, *link, self.id_line])
        lines = [self.title]
        readings = self._readings()
        if readings:
            lines.append(readings)
        text = self.names.rules.get(self.rule_id)
        if self.state != "resolved" and text is not None:
            lines.append(text.hint)
        lines += link
        lines.append(self.id_line)
        return "\n".join(lines)

    def _readings(self) -> str:
        """値の 1 行（決定記録 0142 §2.2）。対象の値（変化量・閾値の超過）・関係する値・室温。

        渡された現在値に無いメトリクスは出さない。
        """
        selected = self.names.context_metrics(self.rule_id, self.metric)
        if selected is None:
            legacy = [f"値 {self.value:.1f}"] if self.value is not None else []
            if self.detail:
                legacy.append(self.detail)
            legacy += [self.names.reading(m, v) for m, v in sorted(self.context.items())]
            return "・".join(legacy)
        parts: list[str] = []
        shown: set[str] = set()
        if self.state != "resolved":
            lead = self._lead()
            if lead is not None:
                parts.append(lead)
                if self.metric:
                    shown.add(self.metric)
        parts += [
            self.names.reading(name, self.context[name])
            for name in selected
            if name not in shown and name in self.context
        ]
        return "・".join(parts)

    def _lead(self) -> str | None:
        """発火の対象の値と、その意味（変化量・閾値）。ルールの種類で決める（0142 §2.4）。"""
        text = self.names.rules[self.rule_id]
        value, metric = self.value, self.metric
        if text.kind == "event":
            return self.detail
        if value is None:
            return None
        if text.kind == "seconds":
            return f"{value:.0f} 秒データが届いていない"
        if metric is None:
            return None
        name, unit = self.names.metric(metric), self.names.unit(metric)
        if text.kind == "samples":
            return f"{name}の値が {value:.0f} 回続けて欠けた"
        if text.kind == "flag":
            return None  # 0 / 1 の値は見出しの文が言っている
        if text.kind == "slope":
            current = self.context.get(metric)
            change = f"1 分で {_num(value, unit, sign=True)}"
            if current is None:
                return f"{name} {change}"
            return f"{name} {_num(current, unit)}（{change}）"
        note = ""
        if text.kind == "threshold" and text.threshold is not None:
            note = f"（閾値 {_num(text.threshold, unit)} を超過）"
        elif text.kind == "range" and text.low is not None and value < text.low:
            note = f"（下限 {_num(text.low, unit)} を下回る）"
        elif text.kind == "range" and text.high is not None and value > text.high:
            note = f"（上限 {_num(text.high, unit)} を超過）"
        elif text.kind == "below" and text.below is not None:
            note = f"（{_num(text.below, unit)} 未満）"
        return f"{name} {_num(value, unit)}{note}"
