"""アラート説明の Evidence 形式（#38）。

**モデルの「自信度」ではなく、何が検証済みで何が未検証かを管理する**（FR-508）。

守る線は3つ。
1. **出力に確度の数値が一切含まれない**
2. **VERIFIED の各行が DB の実データと照合できる**
3. **AI 生成が失敗しても通知は必ず届く**
"""

import json

import pytest

from coldaisle.ai import (
    AiSettings,
    ChatResult,
    ExplainContext,
    Explainer,
    ToolRegistry,
    UnavailableProvider,
)
from coldaisle.ai.explain import FORBIDDEN, Evidence, Explanation
from coldaisle.clock import SimulatedClock
from coldaisle.llm_exclusions import LlmExclusions
from coldaisle.metrics import MetricCatalog
from coldaisle.rules import RuleSet
from coldaisle.store import (
    AlertRecord,
    AlertSeverity,
    AlertState,
    Quality,
    Reading,
    Sample,
    SqliteStore,
)
from conftest import CONFIG_DIR

NOW_MS = 1_787_616_000_000
INTERVAL_MS = 2_500


class FakeProvider:
    """決まった応答を返す。**外部へは出ない。**"""

    def __init__(self, text: str = "", available: bool = True) -> None:
        self.text = text
        self.available = available
        self.prompts: list[str] = []

    def chat(self, messages, *, thinking=None, tools=None) -> ChatResult:
        self.prompts.append("\n".join(m.content for m in messages))
        return ChatResult(available=self.available, text=self.text)

    def probe(self) -> ChatResult:
        return ChatResult(available=self.available)


ANSWER = json.dumps(
    {
        "unverified": ["エアコンの稼働状態", "サーバー背面と壁の距離"],
        "checks": ["背面と壁の間隔を確認する", "フロントフィルタの目詰まりを見る"],
    },
    ensure_ascii=False,
)


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "explain.db", rules=rules, clock=SimulatedClock(NOW_MS)) as opened:
        for index in range(120):
            opened.insert_sample(
                Sample(
                    ts_ms=NOW_MS - 120 * INTERVAL_MS + index * INTERVAL_MS,
                    readings=(
                        Reading(metric="air.room", value=26.0, quality=Quality.OK),
                        Reading(metric="air.front_intake", value=32.2, quality=Quality.OK),
                    ),
                )
            )
        yield opened


@pytest.fixture
def tools(store) -> ToolRegistry:
    return ToolRegistry(
        store=store,
        catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
        rules=RuleSet.from_yaml(CONFIG_DIR / "rules.yaml"),
        clock=store.clock,
        exclusions=LlmExclusions.from_yaml(CONFIG_DIR / "report.yaml"),
    )


def alert(**overrides) -> AlertRecord:
    defaults = {
        "id": 1,
        "rule_id": "RECIRCULATION",
        "severity": AlertSeverity.WARNING,
        "state": AlertState.FIRING,
        "metric": "air.front_intake",
        "started_ms": NOW_MS - 480_000,
        "fired_ms": NOW_MS,
        "trigger_value": 6.2,
        "threshold": 5.0,
        "detail": None,
    }
    return AlertRecord(**{**defaults, **overrides})


# ---------------------------------------------------------------- VERIFIED（受入基準）


def test_verified_lines_are_built_from_data_not_the_model(tools):
    """**VERIFIED をモデルに書かせない。**

    書かせると数値を幻覚でき、「DB の実データと照合できる」という受入基準が
    プロンプトの言い回し次第になる。
    """
    provider = FakeProvider(ANSWER)
    evidence = Explainer(provider=provider, tools=tools).build_evidence(alert())

    assert provider.prompts == [], "事実の組み立てにモデルを呼ばない"
    text = "\n".join(item.text for item in evidence)
    assert "6.20（閾値 5.00）" in text, "アラート行の値と閾値"
    assert "480 秒継続して発火" in text, "started と fired の差"
    assert "品質が ok" in text


def test_verified_numbers_match_the_database(tools, store):
    """VERIFIED の各行が DB と照合できる（受入基準）。"""
    evidence = Explainer(provider=FakeProvider(ANSWER), tools=tools).build_evidence(alert())
    stats = tools.call("get_stats", {"metric": "air.front_intake", "window": "24h"})

    history = next(item for item in evidence if "直近24h:" in item.text)
    assert f"平均 {stats['mean']:.2f}" in history.text
    assert f"最大 {stats['max']:.2f}" in history.text


def test_quality_problems_are_reported(tmp_path, rules):
    """**故障と現象を混同させない。** 品質が ok でないメトリクスは名指しする。"""
    with SqliteStore(tmp_path / "bad.db", rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        store.insert_sample(
            Sample(
                ts_ms=NOW_MS,
                readings=(
                    Reading(metric="air.room", value=26.0, quality=Quality.OK),
                    Reading(metric="air.rear_exhaust", value=None, quality=Quality.MISSING),
                ),
            )
        )
        registry = ToolRegistry(
            store=store,
            catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
            rules=RuleSet.from_yaml(CONFIG_DIR / "rules.yaml"),
            clock=store.clock,
            exclusions=LlmExclusions.from_yaml(CONFIG_DIR / "report.yaml"),
        )
        evidence = Explainer(provider=FakeProvider(ANSWER), tools=registry).build_evidence(alert())
    assert any("air.rear_exhaust" in item.text for item in evidence)


def test_verification_levels_are_labelled(tools):
    """L1（値の妥当性）と L2（ルールエンジン）を分ける。"""
    evidence = Explainer(provider=FakeProvider(ANSWER), tools=tools).build_evidence(alert())
    levels = {item.level for item in evidence}
    assert levels == {"L1", "L2"}, "VERIFIED に L3（LLM）は入らない"


# ---------------------------------------------------------------- 確度の禁止（受入基準）


@pytest.mark.parametrize(
    "text",
    [
        "排気の再循環である確度が高い",
        "確信度は中程度です",
        "87% の可能性で再循環",
        "confidence: high",
        "再循環の可能性が高いです",
        # **語順を変えただけの言い回しを通さない**（#38 のレビュー指摘）
        "再循環である確率は87%",
        "87.5% の可能性で再循環",
        "確率としては低い",
        "probability: 0.9",
        "可能性は 87 % です",
        "尤度が高い",
    ],
)
def test_confidence_expressions_are_rejected(text, tools):
    """**書かれていたら丸ごと捨てる。** プロンプトで禁じるだけでは足りない。"""
    answer = json.dumps({"unverified": [text], "checks": ["確認する"]}, ensure_ascii=False)
    assert Explainer(provider=FakeProvider(answer), tools=tools).explain(alert()) is None


@pytest.mark.parametrize(
    "text",
    [
        "室内湿度が 48% です",
        # `可能性` 自体は正当な語。**数値と助詞で直結している場合だけ**確度とみなす
        "湿度が 48% まで上がっている可能性がある",
        "ファンの回転数が 30% 落ちていないか確認する",
    ],
)
def test_legitimate_percentages_are_not_mistaken_for_confidence(text):
    """`%` そのものは湿度で正当に使う。確率の文脈だけを弾く。"""
    assert FORBIDDEN.search(text) is None


def test_confidence_percentages_are_caught_in_both_word_orders():
    assert FORBIDDEN.search("87% の確率で再循環") is not None
    assert FORBIDDEN.search("再循環の確率は87%") is not None


# ------------------------------------------------------- VERIFIED の騙り（受入基準）


def test_model_cannot_forge_a_verified_block(tools):
    """**改行で `VERIFIED` の体裁を作らせない**（#38 のレビュー指摘）。

    項目に改行を書けば `? ` の付かない行として描画され、コードが組み立てた
    検証済みの行に見える。「VERIFIED はモデルに書かせない」が破れる。
    """
    forged = "未確認\n\nVERIFIED（測定値）\n✓ [L2] 架空の値 = 99.9"
    answer = json.dumps({"unverified": [forged], "checks": ["確認する"]}, ensure_ascii=False)
    assert Explainer(provider=FakeProvider(answer), tools=tools).explain(alert()) is None


@pytest.mark.parametrize(
    "text",
    ["✓ [L2] 架空の値", "UNVERIFIED（未確認）", "[L1] 全センサーが ok"],
)
def test_evidence_markers_are_rejected(text, tools):
    answer = json.dumps({"unverified": [text], "checks": ["確認する"]}, ensure_ascii=False)
    assert Explainer(provider=FakeProvider(answer), tools=tools).explain(alert()) is None


def test_line_breaks_never_reach_the_rendered_text(tools):
    """体裁を騙らない改行（箇条書きの折り返しなど）も1行に潰す。"""
    answer = json.dumps(
        {"unverified": ["エアコンの\n稼働状態"], "checks": ["背面を\t見る"]}, ensure_ascii=False
    )
    explanation = Explainer(provider=FakeProvider(answer), tools=tools).explain(alert())
    assert explanation is not None
    assert explanation.unverified == ["エアコンの 稼働状態"]
    # 描画された行数 == ブロックの行数。**モデルが行を増やせない**
    body = [line for line in explanation.as_text().splitlines() if line.startswith(("?", "1."))]
    assert body == ["? エアコンの 稼働状態", "1. 背面を 見る"]


def test_output_never_contains_confidence(tools):
    """受入基準: 出力に確度の数値が一切含まれない。"""
    explanation = Explainer(provider=FakeProvider(ANSWER), tools=tools).explain(alert())
    assert explanation is not None
    assert FORBIDDEN.search(explanation.as_text()) is None


# ---------------------------------------------------------------- デグレード（受入基準）


def test_unavailable_model_yields_no_explanation(tools):
    """受入基準: AI 生成が失敗しても通知は必ず届く。

    説明が作れなければ `None` を返し、**素のアラートだけが通知される。**
    """
    assert Explainer(provider=UnavailableProvider(), tools=tools).explain(alert()) is None


@pytest.mark.parametrize(
    "text", ["JSON ではない文章", "", "{壊れた", '{"unverified": "配列ではない"}', "{}"]
)
def test_unparseable_answers_degrade(text, tools):
    assert Explainer(provider=FakeProvider(text), tools=tools).explain(alert()) is None


def test_extra_prose_around_json_is_tolerated(tools):
    """前後に説明文を付けてくるモデルは多い。JSON だけ拾う。"""
    provider = FakeProvider(f"以下が結果です。\n{ANSWER}\n以上です。")
    explanation = Explainer(provider=provider, tools=tools).explain(alert())
    assert explanation is not None
    assert "エアコンの稼働状態" in explanation.unverified


def test_long_items_are_trimmed(tools):
    answer = json.dumps({"unverified": ["あ" * 500], "checks": ["い" * 500]}, ensure_ascii=False)
    explanation = Explainer(provider=FakeProvider(answer), tools=tools).explain(alert())
    assert explanation is not None
    assert len(explanation.unverified[0]) <= 120


# ---------------------------------------------------------------- 体裁


def test_text_has_the_three_blocks(tools):
    text = Explainer(provider=FakeProvider(ANSWER), tools=tools).explain(alert()).as_text()
    assert "VERIFIED" in text
    assert "UNVERIFIED" in text
    assert "確認手順" in text
    assert text.index("VERIFIED") < text.index("UNVERIFIED") < text.index("確認手順")


def test_prompt_forbids_confidence_and_new_numbers(tools):
    provider = FakeProvider(ANSWER)
    Explainer(provider=provider, tools=tools).explain(alert())
    prompt = provider.prompts[0]
    assert "確度" in prompt, "禁止をプロンプトでも伝える"
    assert "測定値を新しく作らない" in prompt
    assert "6.20（閾値 5.00）" in prompt, "検証済みの事実を渡している"


def test_explanation_without_ai_parts_still_renders():
    explanation = Explanation(
        rule_id="SENSOR_FAULT", metric=None, verified=[Evidence("L2", "30秒サンプルが無い")]
    )
    assert "✓ [L2] 30秒サンプルが無い" in explanation.as_text()
    assert "UNVERIFIED" not in explanation.as_text()


# ------------------------------------------------ 負荷と温度の材料と見立て（0143 §2.11）


CONTEXT = ExplainContext(
    window="15m",
    metrics=(
        "gpu.0.utilization",
        "power.gpu.0",
        "gpu.0.core",
        "air.room",
        "gpu.0.fan_speed",
        "gpu.0.fan_policy",
    ),
)

ASSESSED = json.dumps(
    {
        "unverified": ["エアコンの稼働状態"],
        "checks": ["背面と壁の間隔を確認する"],
        "assessment": (
            "GPU使用率は平均 92.00 で、GPU消費電力も最大 450.00 まで上がっています。"
            "室温は 26.0 のままで、吸気の上昇は負荷の増加と重なっています。"
            "空調よりも負荷の増加に近い見立てです。"
        ),
    },
    ensure_ascii=False,
)

# 出荷の一覧そのもの（config/report.yaml。0131 §2.4a / 0138）
SHIPPED_EXCLUSIONS = LlmExclusions.from_yaml(CONFIG_DIR / "report.yaml")


@pytest.fixture
def loaded_store(tmp_path, rules):
    """負荷と温度、除外のメトリクス（GPU の Fan）を持つ DB。"""
    with SqliteStore(tmp_path / "load.db", rules=rules, clock=SimulatedClock(NOW_MS)) as opened:
        for index in range(120):
            opened.insert_sample(
                Sample(
                    ts_ms=NOW_MS - 120 * INTERVAL_MS + index * INTERVAL_MS,
                    readings=(
                        Reading(metric="air.room", value=26.0, quality=Quality.OK),
                        Reading(metric="air.front_intake", value=32.2, quality=Quality.OK),
                        Reading(metric="gpu.0.utilization", value=92.0, quality=Quality.OK),
                        Reading(metric="power.gpu.0", value=450.0, quality=Quality.OK),
                        Reading(metric="gpu.0.core", value=78.0, quality=Quality.OK),
                        Reading(metric="gpu.0.fan_speed", value=63.0, quality=Quality.OK),
                        Reading(metric="gpu.0.fan_policy", value=1.0, quality=Quality.OK),
                    ),
                )
            )
        yield opened


def _registry(store, exclusions: LlmExclusions) -> ToolRegistry:
    return ToolRegistry(
        store=store,
        catalog=MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml"),
        rules=RuleSet.from_yaml(CONFIG_DIR / "rules.yaml"),
        clock=store.clock,
        exclusions=exclusions,
    )


def _explainer(store, answer: str = ASSESSED) -> tuple[Explainer, FakeProvider]:
    provider = FakeProvider(answer)
    explainer = Explainer(
        provider=provider, tools=_registry(store, SHIPPED_EXCLUSIONS), context=CONTEXT
    )
    return explainer, provider


def test_load_and_temperature_aggregates_are_in_the_material(loaded_store):
    """使用率・電力・温度の**集計**が材料に入る。生の値の列は入らない。"""
    explainer, provider = _explainer(loaded_store)
    assert explainer.explain(alert()) is not None
    prompt = provider.prompts[0]
    for metric in ("gpu.0.utilization", "power.gpu.0", "gpu.0.core", "air.room"):
        assert f"{metric} 発火前15m: 平均" in prompt
    assert "gpu.0.utilization 発火前15m: 平均 92.00 / 最大 92.00" in prompt
    assert "points" not in prompt
    assert "ts_ms" not in prompt, "生の時系列を渡さない（ルール 8）"


def test_excluded_metrics_never_reach_the_material(loaded_store):
    """**0131 / 0138 の除外は必ず当たる。** 設定に書いても材料にならない。"""
    explainer, provider = _explainer(loaded_store)
    evidence = explainer.build_evidence(alert())
    explainer.explain(alert())
    text = "\n".join(item.text for item in evidence) + provider.prompts[0]
    assert "fan_speed" not in text
    assert "fan_policy" not in text
    assert "63.00" not in text


def test_shipped_context_avoids_the_exclusion_list():
    """出荷の `explain_context` に、LLM へ渡さない一覧のメトリクスを書かない（見張り）。"""
    settings = AiSettings.from_yaml(CONFIG_DIR / "ai.yaml")
    assert settings.explain_context is not None
    catalog = MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")
    # 前提: 出荷の一覧が GPU の Fan を除いている（この PR では除外を当てたまま。0146 は別）
    assert SHIPPED_EXCLUSIONS.excludes_metric("gpu.0.fan_speed")
    assert SHIPPED_EXCLUSIONS.excludes_metric("gpu.0.fan_policy")
    for metric in settings.explain_context.metrics:
        assert not SHIPPED_EXCLUSIONS.excludes_metric_or_inputs(metric, catalog), metric
        assert metric in catalog.metrics, metric
        assert "fan" not in metric, "explain_context に Fan を足さない（0146 の FINAL の後に別で）"


def test_no_context_keeps_the_old_material(loaded_store):
    """`explain_context` が無ければ今のまま（足さない）。"""
    evidence = Explainer(
        provider=FakeProvider(ANSWER), tools=_registry(loaded_store, SHIPPED_EXCLUSIONS)
    ).build_evidence(alert())
    assert not any("15m:" in item.text for item in evidence)


def test_assessment_is_rendered_as_l3(loaded_store):
    explanation = _explainer(loaded_store)[0].explain(alert())
    assert explanation is not None
    assert "負荷の増加に近い" in explanation.assessment
    text = explanation.as_text()
    assert "見立て（L3 推測・未検証）" in text
    assert text.index("VERIFIED") < text.index("見立て") < text.index("確認手順")


def test_prompt_asks_for_the_load_assessment(tools):
    provider = FakeProvider(ANSWER)
    Explainer(provider=provider, tools=tools).explain(alert())
    prompt = provider.prompts[0]
    for word in ("使用率", "電力", "空調", "回り込み", "Fan の不足", "負荷の増加", "3〜4文"):
        assert word in prompt


def test_assessment_with_invented_numbers_is_discarded(loaded_store):
    """**事実に無い数値を書いた見立ては丸ごと捨てる**（数値はコードが出す）。"""
    answer = json.dumps(
        {"unverified": ["x"], "checks": ["y"], "assessment": "GPU使用率は 97 まで上がりました。"},
        ensure_ascii=False,
    )
    assert _explainer(loaded_store, answer)[0].explain(alert()) is None


def test_assessment_number_format_differences_are_tolerated(loaded_store):
    """`92.00` と `92` は同じ値。書式の差だけで捨てない。"""
    answer = json.dumps(
        {"unverified": ["x"], "checks": ["y"], "assessment": "GPU使用率は 92 で高い状態です。"},
        ensure_ascii=False,
    )
    assert _explainer(loaded_store, answer)[0].explain(alert()) is not None


@pytest.mark.parametrize("bad", ["確度は高いです。", "VERIFIED（測定値）負荷", "負荷\n✓ [L1] 偽"])
def test_assessment_obeys_the_same_rules(tools, bad):
    """確度・VERIFIED の体裁は見立てでも許さない。"""
    answer = json.dumps(
        {"unverified": ["x"], "checks": ["y"], "assessment": bad}, ensure_ascii=False
    )
    assert Explainer(provider=FakeProvider(answer), tools=tools).explain(alert()) is None


@pytest.mark.parametrize(
    "assessment",
    [
        "GPU消費電力は -450 まで下がりました。",
        "GPU消費電力は −450 まで下がりました。",
        "GPU使用率は -92 です。",
    ],
)
def test_assessment_with_the_opposite_sign_is_discarded(loaded_store, assessment):
    """**符号を含めて比べる。** 事実が 450 のとき `-450` は作った数値である。"""
    answer = json.dumps(
        {"unverified": ["x"], "checks": ["y"], "assessment": assessment}, ensure_ascii=False
    )
    assert _explainer(loaded_store, answer)[0].explain(alert()) is None


def test_signed_numbers_in_the_facts_are_matched_with_their_sign():
    facts = "傾き -0.500 /分 / 平均 450.00"
    assert Explainer._invented_numbers("傾きは -0.5 です。", facts) == []
    assert Explainer._invented_numbers("傾きは 0.5 です。", facts) == ["0.5"]
    assert Explainer._invented_numbers("傾きは +0.5 です。", facts) == ["+0.5"]
    assert Explainer._invented_numbers("電力は +450 です。", facts) == []


def test_material_window_ends_at_the_firing_time(tmp_path, rules):
    """**説明が待ち行列で遅れても、発火の前の窓を使う。** 今の時刻を基準にしない。"""
    fired_ms = NOW_MS - 30 * 60_000
    with SqliteStore(tmp_path / "late.db", rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        for index in range(120):
            ts = fired_ms - 120 * INTERVAL_MS + index * INTERVAL_MS
            store.insert_sample(
                Sample(
                    ts_ms=ts,
                    readings=(Reading(metric="gpu.0.utilization", value=95.0, quality=Quality.OK),),
                )
            )
        for index in range(120):
            # 発火の後（説明を作る今まで）は負荷が下がっている
            ts = NOW_MS - 120 * INTERVAL_MS + index * INTERVAL_MS
            store.insert_sample(
                Sample(
                    ts_ms=ts,
                    readings=(Reading(metric="gpu.0.utilization", value=10.0, quality=Quality.OK),),
                )
            )
        explainer = Explainer(
            provider=FakeProvider(ASSESSED),
            tools=_registry(store, SHIPPED_EXCLUSIONS),
            context=ExplainContext(window="15m", metrics=("gpu.0.utilization",)),
        )
        evidence = explainer.build_evidence(alert(fired_ms=fired_ms, started_ms=fired_ms - 60_000))
    line = next(item.text for item in evidence if item.text.startswith("gpu.0.utilization"))
    assert "平均 95.00 / 最大 95.00" in line, line
    assert "10.00" not in line
