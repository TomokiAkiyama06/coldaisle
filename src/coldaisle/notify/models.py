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

_STATE_LABELS: dict[str, str] = {"firing": "発生", "resolved": "解除"}

_UNIT_DISPLAY: dict[str, str] = {"C": "°C", "flag": "", "count": ""}
"""`config/metrics.yaml` の `unit` を人が読む本文でだけ写す（決定記録 0142 §2.4）。

API の `unit` は変えない。表に無い単位はそのまま出す。空文字は単位を付けない。
"""


def display_unit(unit: str | None) -> str:
    """本文に出す単位。**無い・付けないものは空文字。**"""
    if unit is None:
        return ""
    return _UNIT_DISPLAY.get(unit, unit)


def _with_unit(number: str, unit: str) -> str:
    return f"{number} {unit}" if unit else number


@dataclass(frozen=True)
class DisplayNames:
    """ID → 人が読む名前（決定記録 0142 §2.3）。**設定の辞書を引くだけ。LLM を使わない。**

    名前が無いルール・メトリクスは ID のまま出す。空のまま作れば今までどおり ID だけになる。
    """

    rules: Mapping[str, str] = field(default_factory=dict)
    """`RULE_ID` → 名前（`config/rules.yaml` の `label`）。"""
    value_kinds: Mapping[str, str] = field(default_factory=dict)
    """`RULE_ID` → 遷移の値の意味（`RuleSet.value_kinds()`）。無ければ `metric` とみなす。"""
    metrics: Mapping[str, str] = field(default_factory=dict)
    """メトリクス → 名前（`config/metrics.yaml` の `label`）。"""
    units: Mapping[str, str] = field(default_factory=dict)
    """メトリクス → `config/metrics.yaml` の `unit`（写す前の値）。"""
    rule_metrics: Mapping[str, tuple[str, ...]] | None = None
    """`RULE_ID` → ルールの定義が参照するメトリクス（`RuleSet.metrics_by_rule()`）。

    `None` なら `現在値:` を絞らない（渡されたものを全部出す。名前の辞書を持たない呼び出し）。
    """
    materials: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """派生値 → 材料（`config/metrics.yaml` の `minuend` と `subtrahend`）。"""
    context_always: tuple[str, ...] = ()
    """`現在値:` の最後に常に付けるメトリクス（`config/notify.yaml` の `context_always`）。"""

    @classmethod
    def build(
        cls,
        *,
        rule_labels: Mapping[str, str],
        value_kinds: Mapping[str, str],
        rule_metrics: Mapping[str, tuple[str, ...]],
        catalog: MetricCatalog,
        context_always: tuple[str, ...],
    ) -> DisplayNames:
        """ルールの設定・`config/metrics.yaml`・`config/notify.yaml` から作る。

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
        return cls(
            rules=dict(rule_labels),
            value_kinds=dict(value_kinds),
            metrics=labels,
            units=units,
            rule_metrics=dict(rule_metrics),
            materials={
                name: (meta.minuend, meta.subtrahend) for name, meta in catalog.derived.items()
            },
            context_always=tuple(context_always),
        )

    def context_metrics(self, rule_id: str, metric: str | None) -> list[str] | None:
        """`現在値:` に出すメトリクスとその順（決定記録 0142 §2.2a）。`None` は絞らない。

        アラートのメトリクス → ルールの定義が参照する残りのメトリクス → それぞれが派生値なら
        その材料 → `context_always`。重複は先に出たほうだけを残す。**手書きの一覧を持たない。**
        """
        if self.rule_metrics is None:
            return None
        targets = [metric] if metric else []
        targets += [name for name in self.rule_metrics.get(rule_id, ()) if name not in targets]
        ordered: list[str] = []
        for name in targets:
            ordered.append(name)
            ordered += self.materials.get(name, ())
        ordered += self.context_always
        return list(dict.fromkeys(ordered))

    def rule(self, rule_id: str) -> str:
        return self.rules.get(rule_id, rule_id)

    def metric(self, metric: str) -> str:
        return self.metrics.get(metric, metric)

    def reading(self, metric: str, value: float | None) -> str:
        """`<名前>=<値> <単位>`。値が無ければ `—`（単位を付けない）。"""
        if value is None:
            return f"{self.metric(metric)}=—"
        return f"{self.metric(metric)}={_with_unit(f'{value:.2f}', self._unit(metric))}"

    def value(self, rule_id: str, metric: str | None, value: float) -> str:
        """`値:` の表示。**単位はルールの種類で決める**（決定記録 0142 §2.4）。"""
        kind = self.value_kinds.get(rule_id, "metric")
        if kind == "seconds":
            return f"{value:.0f} 秒"
        if kind == "samples":
            return f"{value:.0f} サンプル"
        unit = self._unit(metric) if metric else ""
        if kind == "per_minute":
            return f"{value:.2f} {unit}/分" if unit else f"{value:.2f} /分"
        return _with_unit(f"{value:.2f}", unit)

    def _unit(self, metric: str) -> str:
        return display_unit(self.units.get(metric))


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
    """ID → 人が読む名前（決定記録 0142）。空なら ID のまま出す。"""

    @property
    def title(self) -> str:
        """先頭の行。**重大度を必ず載せる。**

        critical と warning は同じ Slack のチャンネルへ届く（決定記録 0135）ため、
        宛先では見分けられない。記号と `[重大度]` で一目で分かるようにする。
        語は日本語（決定記録 0142 §2.1）。ID は最後の行（`id_line`）に残す。
        """
        if self.kind == "report":
            return f"📄 {self.rule_id}"
        severity = SEVERITY_LABELS[self.severity]
        name = self.names.rule(self.rule_id)
        target = f"（{self.names.metric(self.metric)}）" if self.metric else ""
        if self.kind == "explanation":
            return f"🔎 [{severity}] {name}{target}の説明"
        if self.state == "firing":
            mark = _FIRING_MARKS[self.severity]
            label = severity
        else:
            mark = "✅"
            label = f"{severity} 解除"
        state = _STATE_LABELS.get(self.state, self.state)
        return f"{mark} [{label}] {name}{target or ' '}— {state}"

    def _readings(self) -> list[str]:
        """`現在値:` の各項。**そのアラートに関係する値だけ**（決定記録 0142 §2.2a）。

        渡された現在値に無いメトリクスは出さない（`—` は「値が無い」ことを表し、
        「現在値を持っていない」と区別する）。
        """
        selected = self.names.context_metrics(self.rule_id, self.metric)
        if selected is None:
            names = sorted(self.context)
        else:
            names = [name for name in selected if name in self.context]
        return [self.names.reading(name, self.context[name]) for name in names]

    @property
    def id_line(self) -> str:
        """最後の行。ログ・DB・設定と突き合わせるための ID（決定記録 0142 §2.2）。"""
        return f"{self.rule_id} / {self.metric}" if self.metric else self.rule_id

    def as_text(self) -> str:
        """人が読む本文。宛先によらず同じ内容にする。"""
        if self.kind == "report":
            return f"{self.title}\n\n{self.body}\n{self.dashboard_url}"
        if self.body:
            return f"{self.title}\n\n{self.body}\n{self.dashboard_url}\n{self.id_line}"
        lines = [self.title]
        if self.value is not None:
            lines.append(f"値: {self.names.value(self.rule_id, self.metric, self.value)}")
        if self.detail:
            lines.append(self.detail)
        readings = self._readings()
        if readings:
            lines.append(f"現在値: {', '.join(readings)}")
        lines.append(self.dashboard_url)
        lines.append(self.id_line)
        return "\n".join(lines)
