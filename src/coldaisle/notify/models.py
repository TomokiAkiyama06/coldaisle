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

    @property
    def title(self) -> str:
        """先頭の行。**重大度を必ず載せる。**

        critical と warning は同じ Slack のチャンネルへ届く（決定記録 0135）ため、
        宛先では見分けられない。記号と `[重大度]` で一目で分かるようにする。
        """
        if self.kind == "explanation":
            return f"🔎 [{self.severity.value}] {self.rule_id} の説明"
        if self.kind == "report":
            return f"📄 {self.rule_id}"
        target = f" [{self.metric}]" if self.metric else ""
        if self.state == "firing":
            mark = _FIRING_MARKS[self.severity]
            label = self.severity.value
        else:
            mark = "✅"
            label = f"{self.severity.value} 解除"
        return f"{mark} [{label}] {self.rule_id}{target} — {self.state}"

    def as_text(self) -> str:
        """人が読む本文。宛先によらず同じ内容にする。"""
        lines = [self.title]
        if self.body:
            return f"{self.title}\n\n{self.body}\n{self.dashboard_url}"
        if self.value is not None:
            lines.append(f"値: {self.value:.2f}")
        if self.detail:
            lines.append(self.detail)
        if self.context:
            readings = ", ".join(
                f"{metric}={'—' if value is None else f'{value:.2f}'}"
                for metric, value in sorted(self.context.items())
            )
            lines.append(f"現在値: {readings}")
        lines.append(self.dashboard_url)
        return "\n".join(lines)
