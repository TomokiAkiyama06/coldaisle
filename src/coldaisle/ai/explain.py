"""アラートの説明（Evidence 形式）。#38

**モデルの「自信度」ではなく、何が検証済みで何が未検証なのかを管理する**
（FR-508 / 構想メモ §5.1）。「確度87%」には根拠が無い。

## VERIFIED はモデルに書かせない

**測定値と閾値判定はコードが組み立てる。** モデルに書かせると数値を
幻覚できてしまい、「VERIFIED の各行が DB の実データと照合できる」という
受入基準がプロンプトの言い回し次第になる。

モデルが書くのは **UNVERIFIED（未確認の論点）と確認手順だけ**である。

| Level | 誰が保証するか | 例 |
|---|---|---|
| L1 | 値の妥当性（品質フラグ） | 全センサーが `ok` |
| L2 | ルールエンジン（決定論的） | 閾値 5.0℃ を8分継続 |
| L3 | LLM（**推測**） | 「エアコンの稼働状態が未確認」 |
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from coldaisle import logs
from coldaisle.ai.provider import ChatMessage, Provider
from coldaisle.ai.tools import ToolRegistry, as_data
from coldaisle.store.models import AlertRecord, Quality

LOGGER = logging.getLogger("coldaisle.ai.explain")

FORBIDDEN = re.compile(
    r"(確度|確信度|信頼度|confidence|可能性が高い|おそらく間違いなく|\d+\s*%\s*の(可能性|確率))"
)
"""**出力に確度を混ぜさせない**（FR-508）。

プロンプトで禁じるだけでは足りない。書かれていたら丸ごと捨てる。
`%` そのものは湿度で正当に使うため、確率の文脈だけを弾く。
"""

MAX_ITEMS = 6
MAX_ITEM_CHARS = 120

SYSTEM_PROMPT = """あなたはGPUサーバーの熱管理を監視するアシスタントです。
与えられた「検証済みの事実」だけを前提に、次の2つを日本語で出してください。

1. unverified: この事実だけでは判断できない論点（測定していないこと）
2. checks: 人間がその場で確認できる手順

制約:
- **確度・確信度・パーセントでの可能性を書かない。** 根拠がありません
- **測定値を新しく作らない。** 与えられた事実以外の数値を書かない
- 断定しない。「〜が原因です」ではなく「〜が未確認です」と書く
- 各項目は120文字以内、最大6件

JSON だけを出力してください（説明文を付けない）:
{"unverified": ["...", "..."], "checks": ["...", "..."]}"""


@dataclass(frozen=True)
class Evidence:
    """検証済みの1行。**コードが組み立てる。**"""

    level: str
    text: str

    def as_line(self) -> str:
        return f"✓ [{self.level}] {self.text}"


@dataclass(frozen=True)
class Explanation:
    rule_id: str
    metric: str | None
    verified: list[Evidence]
    unverified: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    ai_available: bool = False

    def as_text(self) -> str:
        lines = [f"{self.rule_id} の説明", "", "VERIFIED（測定値・閾値判定）"]
        lines += [item.as_line() for item in self.verified] or ["✓ （該当なし）"]
        if self.unverified:
            lines += ["", "UNVERIFIED（未確認・L3 推測）"]
            lines += [f"? {item}" for item in self.unverified]
        if self.checks:
            lines += ["", "確認手順"]
            lines += [f"{index}. {item}" for index, item in enumerate(self.checks, start=1)]
        return "\n".join(lines)


@dataclass
class Explainer:
    """アラート1件から Evidence 形式の説明を作る。"""

    provider: Provider
    tools: ToolRegistry

    def explain(self, alert: AlertRecord) -> Explanation | None:
        """説明を作る。**作れなければ `None`**（素のアラートだけが通知される）。

        生成の失敗で通知を止めない（#38 の受入基準）。
        """
        verified = self.build_evidence(alert)
        asked = self._ask(alert, verified)
        if asked is None:
            LOGGER.info(
                "AI の説明は付けない（素のアラートのみ通知する）",
                extra={logs.FIELDS_KEY: {"rule": alert.rule_id}},
            )
            return None
        unverified, checks = asked
        return Explanation(
            rule_id=alert.rule_id,
            metric=alert.metric,
            verified=verified,
            unverified=unverified,
            checks=checks,
            ai_available=True,
        )

    # ------------------------------------------------------------------ L1 / L2

    def build_evidence(self, alert: AlertRecord) -> list[Evidence]:
        """**測定値と閾値判定だけ**を集める。推論を混ぜない。

        ここに入る行はすべて DB の実データと照合できる。
        """
        evidence: list[Evidence] = []
        evidence += self._rule_evidence(alert)
        evidence += self._quality_evidence()
        evidence += self._history_evidence(alert)
        return evidence

    def _rule_evidence(self, alert: AlertRecord) -> list[Evidence]:
        """ルールエンジンが決定論的に判定したこと（L2）。"""
        lines: list[Evidence] = []
        target = alert.metric or "（複数メトリクス）"
        if alert.trigger_value is not None and alert.threshold is not None:
            lines.append(
                Evidence(
                    "L2",
                    f"{target} = {alert.trigger_value:.2f}（閾値 {alert.threshold:.2f}）",
                )
            )
        if alert.fired_ms is not None:
            held_s = (alert.fired_ms - alert.started_ms) / 1000
            lines.append(Evidence("L2", f"条件が {held_s:.0f} 秒継続して発火"))
        if alert.detail:
            lines.append(Evidence("L2", f"詳細: {as_data(alert.detail)}"))
        return lines

    def _quality_evidence(self) -> list[Evidence]:
        """センサーが正常に測れているか（L1）。**故障と現象を混同させない。**"""
        latest = self.tools.call("get_latest")
        metrics: dict[str, Any] = latest.get("metrics", {})
        if not metrics:
            return []
        bad = sorted(
            name for name, item in metrics.items() if item.get("quality") != Quality.OK.value
        )
        if bad:
            return [Evidence("L1", f"品質が ok でないメトリクス: {', '.join(bad)}")]
        return [Evidence("L1", f"全 {len(metrics)} メトリクスの品質が ok（センサー異常ではない）")]

    def _history_evidence(self, alert: AlertRecord) -> list[Evidence]:
        """直近24時間との比較（L1）。**平常時との差**を数字で置く。"""
        if alert.metric is None:
            return []
        stats = self.tools.call("get_stats", {"metric": alert.metric, "window": "24h"})
        if "error" in stats or stats.get("mean") is None:
            return []
        lines = [
            Evidence(
                "L1",
                f"{alert.metric} 直近24h: 平均 {stats['mean']:.2f} / 最大 {stats['max']:.2f}"
                f" / p95 {stats['p95']:.2f}（{stats['sample_count']} 件）",
            )
        ]
        if stats.get("slope_per_min") is not None:
            lines.append(Evidence("L1", f"直近24hの傾き {stats['slope_per_min']:.3f} /分"))
        if stats.get("missing_ratio"):
            lines.append(Evidence("L1", f"直近24hの欠測率 {stats['missing_ratio']:.1%}"))
        return lines

    # ------------------------------------------------------------------ L3

    def _ask(
        self, alert: AlertRecord, verified: list[Evidence]
    ) -> tuple[list[str], list[str]] | None:
        facts = "\n".join(item.as_line() for item in verified)
        user = (
            f"アラート: {alert.rule_id}（重大度 {alert.severity.value}）\n"
            f"対象: {alert.metric or '複数'}\n\n"
            f"検証済みの事実:\n{facts}"
        )
        result = self.provider.chat(
            [
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(role="user", content=user),
            ],
            thinking=False,
        )
        if not result.available:
            return None
        return self._parse(result.text)

    def _parse(self, text: str) -> tuple[list[str], list[str]] | None:
        """モデルの出力を読む。**約束を破っていたら丸ごと捨てる。**

        確度を書いてきた出力を「その行だけ消して使う」ことはしない。
        指示を守れていない応答の残りを信用する理由が無い。
        """
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            LOGGER.warning("AI の応答が JSON ではない")
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            LOGGER.warning("AI の応答を JSON として読めない")
            return None
        if not isinstance(parsed, dict):
            return None
        unverified = self._clean(parsed.get("unverified"))
        checks = self._clean(parsed.get("checks"))
        if not unverified and not checks:
            return None
        for item in unverified + checks:
            if FORBIDDEN.search(item):
                LOGGER.warning(
                    "AI の出力に確度の表現があったので捨てた",
                    extra={logs.FIELDS_KEY: {"text": item[:100]}},
                )
                return None
        return unverified, checks

    @staticmethod
    def _clean(values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        cleaned: list[str] = []
        for value in values[:MAX_ITEMS]:
            if isinstance(value, str) and value.strip():
                cleaned.append(value.strip()[:MAX_ITEM_CHARS])
        return cleaned
