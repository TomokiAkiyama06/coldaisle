"""アラートの説明（Evidence 形式）。#38

**モデルの「自信度」ではなく、何が検証済みで何が未検証なのかを管理する**
（FR-508 / 構想メモ §5.1）。「確度87%」には根拠が無い。

## VERIFIED はモデルに書かせない

**測定値と閾値判定はコードが組み立てる。** モデルに書かせると数値を
幻覚できてしまい、「VERIFIED の各行が DB の実データと照合できる」という
受入基準がプロンプトの言い回し次第になる。

モデルが書くのは **UNVERIFIED（未確認の論点）・見立て・確認手順だけ**である。

見立て（決定記録 0143 §2.11）は、負荷（使用率・電力）の変化と温度の関係から、
空調・筐体内の回り込み・Fan の不足・負荷の増加のどれが近いかを述べる L3 の推測である。
材料の集計はコードが VERIFIED の行として作る。
**Provider に依らない**（ローカル LLM でも Claude でも同じ材料・同じ検査）。

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
from coldaisle.ai.provider import ChatMessage, ExplainContext, Provider
from coldaisle.ai.tools import ToolRegistry, as_data
from coldaisle.store.models import AlertRecord, Quality

LOGGER = logging.getLogger("coldaisle.ai.explain")

_PERCENT = r"\d+(?:\.\d+)?\s*[%％]"

FORBIDDEN = re.compile(
    # 確度を表す語そのもの。**語順に依存しない**（「確率は87%」も「87%の確率」も）
    r"確度|確信度|信頼度|確率|尤度"
    r"|(?i:confidence|probability|likelihood|certainty)"
    r"|可能性が高い|おそらく間違いなく"
    # 「87.5% の可能性」。**助詞で直結している場合だけ**弾く（下記の理由）
    rf"|{_PERCENT}\s*(?:程度|くらい|ほど)?\s*(?:の|である|だ)?\s*可能性"
    rf"|可能性\s*(?:は|が)\s*(?:およそ|約)?\s*{_PERCENT}"
)
"""**出力に確度を混ぜさせない**（FR-508）。

プロンプトで禁じるだけでは足りない。書かれていたら丸ごと捨てる。

`%` そのものは湿度で正当に使うため、確率の文脈だけを弾く。
`可能性` は「〜が未確認である可能性」のように**正当に使う語**なので、
数値と助詞で直結している場合（`87% の可能性`）だけを確度とみなす。
「湿度が 48% まで上がっている可能性」は通す。
"""

STRUCTURE = re.compile(r"VERIFIED|✓|\[L[0-9]\]")
"""**モデルに VERIFIED 行を騙らせない**（#38 のレビュー指摘）。

項目に改行と `VERIFIED（測定値）` を書けば、`? ` の付かない行として
描画され、コードが組み立てた検証済みの行に見えてしまう。改行は潰すが、
**そもそも体裁を書いてきた応答は指示に従えていない**ので採用しない。
"""

_WHITESPACE = re.compile(r"\s+")

MAX_ITEMS = 6
MAX_ITEM_CHARS = 120
MAX_ASSESSMENT_CHARS = 400

_NUMBER = re.compile(r"[-+\u2212]?\d+(?:\.\d+)?")
"""数値らしきもの（**符号を含む**）。見立ての数値を事実と突き合わせるために使う。

符号を落とすと `-450` を `450` と同じと見てしまう。`−`（U+2212）も負の符号として読む。
"""

SYSTEM_PROMPT = """あなたはGPUサーバーの熱管理を監視するアシスタントです。
与えられた「検証済みの事実」だけを前提に、次の3つを日本語で出してください。

1. unverified: この事実だけでは判断できない論点（測定していないこと）
2. checks: 人間がその場で確認できる手順
3. assessment: 負荷（使用率・電力）の変化と温度の関係から、空調・筐体内の回り込み・Fan の不足・
   負荷の増加のどれが近いかを見立て、根拠の値（検証済みの事実にある数値だけ）を挙げる。
   日本語の3〜4文の地の文にする。材料が足りなければ、何が足りないかを書く

制約:
- **確度・確信度・パーセントでの可能性を書かない。** 根拠がありません
- **測定値を新しく作らない。** 与えられた事実以外の数値を書かない
- 断定しない。「〜が原因です」ではなく「〜が未確認です」と書く
- **改行や見出しで体裁を作らない。** 1項目は1行の文にする
- 各項目は120文字以内、最大6件。assessment は400文字以内

JSON だけを出力してください（説明文を付けない）:
{"unverified": ["...", "..."], "checks": ["...", "..."], "assessment": "..."}"""


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
    assessment: str = ""
    """見立て（L3 の推測。0143 §2.11）。空なら出さない。"""

    def as_text(self) -> str:
        lines = [f"{self.rule_id} の説明", "", "VERIFIED（測定値・閾値判定）"]
        lines += [item.as_line() for item in self.verified] or ["✓ （該当なし）"]
        if self.unverified:
            lines += ["", "UNVERIFIED（未確認・L3 推測）"]
            lines += [f"? {item}" for item in self.unverified]
        if self.assessment:
            lines += ["", "見立て（L3 推測・未検証）", f"? {self.assessment}"]
        if self.checks:
            lines += ["", "確認手順"]
            lines += [f"{index}. {item}" for index, item in enumerate(self.checks, start=1)]
        return "\n".join(lines)


@dataclass
class Explainer:
    """アラート1件から Evidence 形式の説明を作る。"""

    provider: Provider
    tools: ToolRegistry
    context: ExplainContext | None = None
    """負荷と温度の集計を材料に足すか（0143 §2.11）。無ければ足さない。"""

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
        unverified, checks, assessment = asked
        return Explanation(
            rule_id=alert.rule_id,
            metric=alert.metric,
            verified=verified,
            unverified=unverified,
            checks=checks,
            ai_available=True,
            assessment=assessment,
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
        evidence += self._context_evidence(alert)
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

    def _context_evidence(self, alert: AlertRecord) -> list[Evidence]:
        """負荷と温度の直近の集計（L1。0143 §2.11）。**平均・最大・傾きだけ**で、生の値は渡さない。

        集計はツール（`get_stats`）を通すので、LLM へ渡さない一覧（0131 §2.4a / 0138）に当たる
        メトリクスは無いメトリクスと同じ空の集計になり、ここで落ちる（行を作らない）。
        """
        if self.context is None:
            return []
        window = self.context.window
        # 窓の終わりを発火の時刻に固定する。説明が待ち行列で遅れても、発火の前の状態を見る
        end_ms = alert.fired_ms
        span = f"直近{window}" if end_ms is None else f"発火前{window}"
        lines: list[Evidence] = []
        for metric in self.context.metrics:
            try:
                stats = self.tools.stats(metric, window, end_ms=end_ms)
            except Exception as error:
                # `tools.call` と同じく 1 つの失敗で説明を止めない（その行を作らないだけ）
                LOGGER.warning(
                    "説明の材料の集計に失敗した（その行は付けない）",
                    extra={logs.FIELDS_KEY: {"metric": metric, "error": type(error).__name__}},
                )
                continue
            if "error" in stats or stats.get("mean") is None or stats.get("max") is None:
                continue
            unit = stats.get("unit") or ""
            text = (
                f"{metric} {span}: 平均 {stats['mean']:.2f} / 最大 {stats['max']:.2f}"
                f"（{unit}、{stats['sample_count']} 件）"
            )
            if stats.get("slope_per_min") is not None:
                text += f" / 傾き {stats['slope_per_min']:.3f} /分"
            lines.append(Evidence("L1", text))
        return lines

    # ------------------------------------------------------------------ L3

    def _ask(
        self, alert: AlertRecord, verified: list[Evidence]
    ) -> tuple[list[str], list[str], str] | None:
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
        return self._parse(result.text, user)

    def _parse(self, text: str, facts: str = "") -> tuple[list[str], list[str], str] | None:
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
        assessment = self._clean_assessment(parsed.get("assessment"))
        if not unverified and not checks and not assessment:
            return None
        if assessment and self._invented_numbers(assessment, facts):
            # **事実に無い数値は、モデルが作った数値である**（日次レポートの所見と同じ）
            LOGGER.warning(
                "AI の見立てに事実へ無い数値があったので捨てた",
                extra={logs.FIELDS_KEY: {"text": assessment[:100]}},
            )
            return None
        for item in [*unverified, *checks, *([assessment] if assessment else [])]:
            if FORBIDDEN.search(item):
                LOGGER.warning(
                    "AI の出力に確度の表現があったので捨てた",
                    extra={logs.FIELDS_KEY: {"text": item[:100]}},
                )
                return None
            if STRUCTURE.search(item):
                LOGGER.warning(
                    "AI の出力が VERIFIED の体裁を騙ったので捨てた",
                    extra={logs.FIELDS_KEY: {"text": item[:100]}},
                )
                return None
        return unverified, checks, assessment

    @staticmethod
    def _clean_assessment(value: object) -> str:
        """見立ての文。**改行を潰す**（VERIFIED の行に紛れさせない。`_clean` と同じ理由）。"""
        if not isinstance(value, str):
            return ""
        return _WHITESPACE.sub(" ", value).strip()[:MAX_ASSESSMENT_CHARS]

    @staticmethod
    def _invented_numbers(text: str, facts: str) -> list[str]:
        """事実に現れない数値。`45.2` と `45.20` は同じ値とみなす（書式の差で捨てない）。

        **符号は含めて比べる**（`-0.5` と `0.5` は違う値）。`+0.5` は `0.5` と同じ。
        """
        allowed = {_signed(token) for token in _NUMBER.findall(facts)}
        return [token for token in _NUMBER.findall(text) if _signed(token) not in allowed]

    @staticmethod
    def _clean(values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        cleaned: list[str] = []
        for value in values[:MAX_ITEMS]:
            if not isinstance(value, str):
                continue
            # **改行を潰す。** 残すと `? ` の付かない行が描画され、
            # コードが組み立てた VERIFIED 行に紛れる（STRUCTURE も参照）
            text = _WHITESPACE.sub(" ", value).strip()
            if text:
                cleaned.append(text[:MAX_ITEM_CHARS])
        return cleaned


def _signed(token: str) -> float:
    """符号つきの数値の文字列を値にする（`−` は負の符号）。"""
    return float(token.replace("\u2212", "-"))
