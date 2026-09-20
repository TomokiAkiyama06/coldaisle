# 決定記録 0059: decision trace が tick ごとに model artifact を記録する（適用側の証拠を artifact へ束縛する）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-21、リポジトリ所有者が承認）
- **Date**: 2026-09-20
- **Supersedes**: [`0057-authority-rollout-stage-changes.md`](0057-authority-rollout-stage-changes.md)
  の **§3 の帰結「適用側（factual）の実績では昇格できない」の項**と、
  **§5 の未決事項「decision trace へ、適用した tick の model artifact を記録すること
  （GitHub #159）」の項**だけ。
  **§2.4 は置き換えない。** §2.4 は「名指した arm は**適用側・counterfactual 側の
  どちらの名前空間でもよい**が、制御器で判断する」と決めており、本記録はその許可を
  そのまま使う。足すのは**適用側の arm にだけ掛かる追加条件**（artifact の束縛。§2.4 の
  条件一覧への**追記**であって、既存の条件の置き換えではない）である。
  0057 のそれ以外の決定（stage の正本・承認の束縛・1段ずつ・自動降格・設定は上限・
  lock の順序・証拠の完全性と新しさ）は**すべてそのまま有効である**
- **関連**: [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md) §2、
  [`0048-thermal-model-artifact-and-inference.md`](0048-thermal-model-artifact-and-inference.md) §2.4、
  [`0050-model-confidence-ood-and-authority.md`](0050-model-confidence-ood-and-authority.md) §2.5、
  [`0053-control-shadow-mode-and-counterfactual-logging.md`](0053-control-shadow-mode-and-counterfactual-logging.md) §2.2、
  [`0054-offline-evaluation-attribution-and-gates.md`](0054-offline-evaluation-attribution-and-gates.md) §2.1 / §2.2 / §2.7、
  GitHub #82 / #85 / #90 / #91 / #92 / #159
- **対象 Issue**: #159

## 1. Context

0057 は昇格の証拠を **いま Production の artifact** へ束縛すると決めた（§2.4）。
ところが decision trace は、**その判断を出した artifact を tick ごとに記録していなかった。**

- `ModelGateDecision` が持つのは `model_version` と `inference_id` だけ
- `ControlState` も `model_version` だけ
- 適用 arm の鍵（`applied:<controller>+<policy>@<stage>/<mode>`。0054 §2.1）に model の
  identity が入らない
- `EvaluationProvenance.versions.model_artifacts` は、counterfactual の `artifact_sha256`
  からしか集まらない

そのため「**artifact B の新しい適用実績**と **artifact A の counterfactual**」が混ざった
報告が、`model_artifacts == {A}` の照合を素通りする。B の実績で A の authority を上げられて
しまうので、0057 は**適用側の arm を昇格の根拠にしない**（fail closed）とした。

帰結は 0057 §3 に書いたとおりである。**Shadow の間は困らない**（Learned の提案は
counterfactual に残り `artifact_sha256` が付く）が、**LIMITED 以降は Learned MPC が適用側に
出る**ので counterfactual に Learned の arm が現れず、EXPANDED / FULL への証拠を作れない。
**rollout が LIMITED で止まる。**

#85 / #90 / #91 / #92 のレビューで繰り返し見つかった失敗の型を、ここでも開けない。

- 束縛していない識別子で、別の推論の結果を「この提案の実績」として数える
- 自己申告の値を「記録されているから」として通す
- 完全でない証拠（一部の tick・一部の区間）を全体の合格として扱う
- 証拠の時刻ではなく処理した時刻を使う

## 2. Decision

### 2.1 artifact は `ModelGateDecision` に置く。`ControlState` には置かない

`ModelGateDecision.artifact_sha256`（`Sha256Hex | None`）を足す。**`ControlState` は変えない。**

理由は「どこに書けるか」ではなく「**何に束縛されているか**」である。

| 置き場所 | 束縛 |
|---|---|
| `ModelGateDecision` | `attested` / `confidence` / `ood` / `inference_id` と**同じ枠**にある。Gate が値を取るのは、その提案に束ねられた**検証済み（`REGISTRY_VERIFIED`）の `ConfidenceAssessment`** ひとつだけで、それは #85 の束縛（`inference_id` と `model_version` の一致）を通ったものである |
| `ControlState` | **どの tick にもある。** 提案の無い tick・`MANUAL` / `CALIBRATION` の tick にも欄ができ、「裏づけのある推論が無いのに artifact だけがある」形を作れる。`ControlState` は attestation の概念を持たないので、そこに置くと束縛の根拠が型から消える |

あわせて 0057 §2.7 の「stage は model version と**独立に**残す」を崩さない。model の identity は
`model_gate` に集め、`ControlState` / `ControllerSelection` / `AuthorityRuntime.trace_metadata()`
は stage だけを持つ。

**書ける値は1つだけである。**

- `ControllerProposal` に artifact の欄を**作らない**。worker が「この提案はこの artifact が
  出した」と名乗る経路を型として無くす（自己申告を受け取らない）
- Gate は `ConfidenceAssessment` を `model_validate` で**検証し直してから**写す。
  `model_copy(update=...)` は検証を通らないため（#85 と同じ扱い）
- **検証し直すだけでは足りない**（codex #4057191721）。`artifact_sha256` は assessment の
  ただの欄で、`model_validate` が見るのは形だけである。artifact B の正しい assessment を
  `model_copy(update={"artifact_sha256": A})` で書き換えたものは、`REGISTRY_VERIFIED` も
  同じ推論 ID も版も confidence もそのまま通り、**A の実績として記録できてしまう。**
  だから Gate は **配線時に束縛した attestation の hash** と照らす。
  `ControllerGate.__init__` は `expected_artifact_sha256` を**必須の引数**にする
  （`ArtifactAttestation.artifact_sha256`。`expected_model_version` と同じ立場）。
  一致しなければ `model_artifact_mismatch` で Fallback にし、**artifact を記録しない**。
  Learned MPC を束縛できなかった runtime は `None` を**明示的に**渡し、その場合は
  どの Learned 提案も採らない（既定値を置かないのは、渡し忘れが「何にも照らさない」
  状態を作らないためである。0057 §2.2 の `authority` と同じ理由）
- **照らす相手も、書き換えられる欄であってはならない**（codex #4057241944）。
  「期待する A」と「assessment が名乗る A」を比べるだけでは、**B の判定を A と名乗らせた**
  ものが通る。推論 ID は B のままなので B の提案と一致し、**B の提案が採られたまま
  A の実績として記録される。**

  そこで **identity を宣言ではなく導出にする。** `ConfidenceAssessment` は
  `input_sha256`（判定に使った入力 window の digest）と `prediction`
  （**判定した予測そのもの。artifact SHA-256 はこの中にある**）を持ち、
  `inference_id` は `derive_inference_id(input_sha256, prediction)` と一致しなければ
  **型として作れない**（`model_validate` が検証する）。あわせて
  `artifact_sha256` / `model_id` / `artifact_verification` / `input_action_ts_ms` が
  `prediction` のそれと一致することも要求する。

  Gate が期待値と照らすのは、**識別子の導出に入っている値**（`prediction.artifact_sha256`）
  である。これで次が閉じる。

  | 手口 | どこで落ちるか |
  |---|---|
  | `artifact_sha256` の欄だけ A に書き換える | `prediction` と食い違う → 型が拒む |
  | `prediction` の中の artifact まで A にする | `inference_id` の導出が合わない → 型が拒む |
  | 識別子も作り直す | もう B の推論ではない。提案の `inference_id` と合わない → Gate が退ける |
  | B の assessment をそのまま出す | 導出側の artifact が B → Gate が `model_artifact_mismatch` |

- **counterfactual 側（`ShadowCounterfactual.artifact_sha256`）も同じ値にする。**
  記録側（#90）は assessment の欄ではなく **Gate が照合し終えた artifact** を写す。
  同じ tick の2つの記録が違う artifact を名乗ることは `ControlTick` が拒む（§2.2）

#### いま何が照合されていて、何を信用したままか

**照合している（写した値ではなく、導出または配線から来る）:**

| 値 | 何と照らすか |
|---|---|
| `model_gate.artifact_sha256` | 配線時の `ArtifactAttestation.artifact_sha256` と、`prediction`（識別子の導出に入る） |
| `model_gate.inference_id` | 提案の `inference_id`。assessment 側は `derive_inference_id` で導出を検証 |
| `model_gate.model_version` | 配線時の `expected_model_version` と、提案・assessment の版 |
| `model_gate.confidence` / `ood` | 検証済み assessment（その validator が components との一致を要求） |
| `model_gate.authority_stage` | Gate 自身の実効 stage（呼び出し側から受け取らない） |
| anchor 推論の identity | `LearnedMpcController._check_anchor()` が attestation と照合（既存） |
| Confidence Profile | `_check_binding_matches_policy()` が attestation と照合（既存） |
| `ControlState.model_version` | `ControlTick` が `model_gate` との一致を要求 |
| `RolloutEvidence.artifact_sha256` | lock を握ったまま読み直した production pointer（0057 §2.3） |

**信用したまま（ここでは塞がない）:**

- **同一 process 内で、一式を整合させて作り直す偽造。** 0050 §3 が受け入れた範囲である。
  秘密鍵を持たない以上、導出関数は誰でも呼べる。塞いだのは**欄の書き換え**であって、
  「A の入力・A の予測・A の識別子を揃えて作る」ことではない。ただしそれは
  **もう A の仕事そのもの**であり、B の実績を A に付け替えることにはならない
- `LearnedControlStatus.binding_authority_stage`。worker 側の値だが、0057 §2.2 が
  `result_digest` に覆わせたうえで意図的にそう決めた。本記録では変えない
- `ConfidenceAssessment.profile_sha256` と `model_version`。前者は
  `LearnedMpcController` の生成時に attestation と照合済み、後者は配線時の
  `expected_model_version` と提案の両方に照らしている
- **counterfactual 側（`ShadowCounterfactual.artifact_sha256`）も同じ値にする。**
  記録側（#90）は assessment の欄ではなく **Gate が照合し終えた artifact** を写す。
  同じ tick の2つの記録が違う artifact を名乗ることは `ControlTick` が拒む（§2.2）
- 裏づけの無い判断（`attested` でない）には**付けない**。付けられると、別の推論の artifact が
  この tick の実績として読まれる

### 2.2 `ControlTick` の schema version を 7 へ上げる。v1〜v6 はそのまま読める

0030 §2 の規則どおり、**意味の変わる欄を足したので version を上げる。**

- `schema_version < 7` の tick は `model_gate.artifact_sha256` を**持てない**
  （古い version に、後から意味の違う欄を足して読ませない）
- `schema_version >= 7` の tick で `model_gate.attested` なら **artifact は必須**。
  **新しい trace で「artifact 不明」を作れないようにする。** 作れると、束縛できる形に
  直したあとも、欄を空けるだけで束縛を外せる
- 保存済みの v1〜v6 は**そのまま読める**。欄が無い tick は
  **「artifact 不明」**（`ControlTick.applied_model_artifact is None`、
  `applied_artifact_unknown is True`）として扱い、**昇格の証拠に使わない**（fail closed）。
  **推測で埋めない**
- 同じ tick の `shadow` の counterfactual が別の artifact を名乗ることを拒む。
  2つの記録が食い違うと、あとから読む側が「どの artifact の提案か」を決められない

`ControlTick.applied_model_artifact` が値を返すのは、**その tick の requested を実際に作った**
裏づけのある Learned MPC の判断だけである（`attested` かつ `learned_selected`）。

### 2.3 適用側の arm を artifact へ束縛する（0054 への追記）

**0054 の帰属規則は変えない。** 記録から言える事実を増やしただけで、どの実測をどの arm に帰属させるか（§2.1 / §2.2）も coverage の扱い（§2.3）も
gate の段（§2.4）も同じである。

`AppliedArmReport` に3つ足す。

| 欄 | 意味 |
|---|---|
| `model_artifacts` | この arm で**裏づけのある提案を適用した** tick の artifact（昇順・重複なし） |
| `bound_attested_ticks` | **artifact を言えた** tick の数 |
| `unbound_attested_ticks` | **artifact を言えなかった** tick の数 |

- 出どころは `ControlTick.model_gate.artifact_sha256` **だけ**である。走らせた側の自己申告も
  設定の宣言も入れない（0054 §2.6 と同じ向き）
- **「artifact 不明」を黙って落とさない。** 落とすと、残った tick の artifact が区間全体の
  実績に見え、部分的な束縛が完全なものとして読まれる。理由は `applied_artifact_unknown`
  として `gaps` にも残す
- artifact を持てるのは `controller` が Learned MPC の適用 arm だけで、裏づけのある提案が
  1つも無い arm（`last_attested_ts_ms` が `None`）には付けられない
- `EvaluationProvenance.versions.model_artifacts` を**適用側からも集める。**
  counterfactual からしか集めない限り、「B の適用実績 + A の counterfactual」が `{A}` を
  名乗れる
- 報告の型として、**適用 arm の `model_artifacts` は `provenance.versions.model_artifacts` の
  部分集合**でなければならない。run が一度も見ていない artifact を arm の実績に書けない

**報告の schema version を 2 へ上げる。** 0057 §2.4 が `last_attested_ts_ms` を足したときは
上げなかったが、あれは**欄が無い＝`None`＝「言えない」**と読まれたからである。
今度の欄は**無い＝`()` / `0`**、すなわち**「不明が1件も無い」＝完全**と読めてしまう
（codex #4057527950）。**absence が unknown に落ちるか completeness に落ちるかで扱いを変える。**
v1 の報告がこの3欄を持つことは型が拒む（古い version に意味の違う欄を後から足さない）。

**適用 arm が Learned MPC なら、その arm のすべての tick を勘定する。**
報告 v2 では `bound_attested_ticks + unbound_attested_ticks == ticks` を要求する。
`model_artifacts` は集合なので「どの artifact か」しか言わず、**1 tick だけ束縛できた
100 tick の arm でも `{production}` / 不明 0 になりうる**（codex #4057573941）。
tick 数で突き合わせないと、区間の何割を束縛できたのかを言えていない報告が
「完全に束縛できた」と読まれる。

**この勘定を要求するのは報告 v2 以降だけである。** v1 の報告は**そのまま読める**
（codex #4057573943）。型の段で完全性を要求すると、保存済みの v1 が**読めなくなる。**
読めることと、昇格の根拠にできることは別で、`#92` は v1 を
「artifact の完全性を言えない報告」として証拠から外す（§2.5）。

### 2.4 適用側の arm を昇格の根拠にできるようにする（0057 §3 の帰結の置き換え）

0057 §2.4 は「名指した arm は適用側・counterfactual 側のどちらの名前空間でもよい」と
**既に許している。** 使えなくしていたのは §3 の帰結（「適用側（factual）の実績では昇格
できない」）を写した `_check_learned_arms()` の拒否である。**その拒否を外す。**
外すかわりに、§2.4 の条件一覧へ次を**足す**。

**（a）報告に現れた Learned MPC の arm すべてについて**、`unbound_attested_ticks` が **0**
（artifact を言えない適用 tick が1つも無い）。**名指した arm だけを見ない。**
名指した arm だけを見ると、同じ holdout に v1〜v6 の tick を含む別の適用 arm が残っていても
昇格できてしまう（codex #4057191724）。gate の判定をすべての Learned arm に要求するのと
同じ理由である。

**（b）名指した arm が適用側なら**、`model_artifacts` が**空でなく**（旧 version だけで
回した区間を通さない）、**いま Production の artifact ちょうど1つ**である。

**artifact と「不明」の数は segment をまたいで足し合わせる。** 新しいほうの segment だけを
残すと、別の artifact で回した区間や、artifact の欄を持たない古い trace の区間が束縛の
照合から消える。

0057 §2.4 のほかの条件は**すべてそのまま効く。**

- 名指した arm が holdout の `overall` group に実在し、その制御器が Learned MPC である
- 名指した arm の `authority_stage` が遷移元と同じである
- 報告に現れた Learned MPC の arm すべてに gate 判定があり、すべて `pass` である
- 証拠の新しさは `last_attested_ts_ms`（**裏づけのある提案が最後に実在した時刻**）で測り、
  期限は registry と authority の lock を両方取ったあとに読み直した時刻で判断する
- 報告全体の `model_artifacts` が Production の artifact ちょうど1つである

**昇格は1段ずつ・人の承認だけ**（0057 §2.3 / §2.5）も、**降格は承認なしで即時**（§2.6）も、
**Critical Safety は全 stage で同一**（§2.7）も変えない。

### 2.5 **記録の無さは常に「不明」であって「完全」ではない**

本記録で足した欄はすべて「無い」ことがありうる。**そのとき何と読むかを1つの規則で固定する。**

> **artifact に関する記録が無いことは、常に `unknown` である。**
> **`none` / `0` / `complete` とは読まない。**

具体的には次のとおり（codex #4057527947 / #4057527950）。

| 読む場所 | 「無い」とき | **してはいけない読み方** |
|---|---|---|
| `ControlTick.model_gate` が無い（v1〜v4）で `active_controller` が Learned MPC | `applied_artifact_unknown = True` | 「gate が無い＝適用していない＝不明も無い」 |
| `model_gate.artifact_sha256` が無い（v1〜v6） | 同上 | 「欄が無い＝まだ書いていないだけ」 |
| `AppliedArmReport.model_artifacts` が空 | その arm は artifact を言えない | 「混ざっていない＝1つに絞れている」 |
| `AppliedArmReport.bound_attested_ticks` / `unbound_attested_ticks` が 0 | **報告 v2 でだけ**「不明は無い」。v2 では2つの和が `ticks` と一致することも求める | v1 でも同じに読む／和を確かめずに「不明 0」だけ見る |
| `EvaluationReport.schema_version` が 1 | 完全性を**言えない** | 「欄が無い＝完全」 |
| `ObservedVersions.model_artifacts` が空 | artifact を言えない | 「Production だけで回した」 |

**判断の起点は `ControlState.active_controller` に置く。** `model_gate` の有無から数えると、
`model_gate` を持たない v1〜v4 の適用 tick が「不明」にも「束縛できた」にも数えられず、
**欠けていること自体が記録から消える。**

**逆向きも固定する。** Fallback で回していた tick は artifact を持たないのが正しい姿であり、
**「不明」ではない。** ここを混ぜると Fallback の多い報告がすべて不明で埋まり、
昇格が永久に来ない。区別するのは `active_controller` である。

## 3. Consequences

良くなること。

- **rollout が LIMITED で止まらない。** LIMITED → EXPANDED → FULL を、その段で実際に
  運転した実績で歩ける（実機なしの試験で通しで確かめる）
- 「どの artifact の実績か」が **trace の1 tick 単位で**言える。事故調査でも、適用した
  判断とモデルの版・hash を突き合わせられる
- 混ざった報告が**2箇所**で落ちる。報告全体の `model_artifacts` と、arm ごとの束縛
- artifact を言えない区間が**数として残る**。「記録が無いだけ」と「束縛できた」を混ぜない

悪くなること。

- **保存済みの v1〜v6 だけで回した区間は、適用側の証拠にならない。** 緩和はしない。
  推測で埋めるより、「上げられない」側に倒れるほうがよい。v7 を書き始めてから
  `evidence_max_age_ms` 分の運転を貯めれば足りる
- `ControlTick` の reader が版を1つ増やす。v7 の attested な判断は artifact 必須なので、
  trace を作る側（Gate 以外）で欄を埋め忘れると schema が拒む。**拒むことが目的である**
- 適用側の arm は counterfactual と違い、optimizer の実績を持たない（0054 §3）。
  昇格の gate は `safety` / `evidence` の段だけで判断することになる。これは 0054 §3 の
  「適用された arm の gate には cost の条件が無い」のままで、本記録では変えない

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `ControlState` に `artifact_sha256` を持たせる | どの tick にもある欄になり、裏づけのある推論が無い tick にも artifact を書ける。`ControlState` は attestation を持たないので、束縛の根拠が型から消える（§2.1） |
| `ControllerProposal` に artifact の欄を足し、Gate がそれを写す | **自己申告を受け取ることになる。** 提案の `confidence` / `ood` を信用しないのと同じ理由で、artifact も信用しない（#85） |
| 適用 arm の**鍵**（`arm_key`）に artifact を入れる | artifact を入れ替えるたびに arm が別物になり、同じ構成の運転実績が分断される。鍵は「どう回したか」で、artifact は「何で回したか」である |
| assessment の `artifact_sha256` を、形の検証だけで信じる | `model_validate` は形しか見ない。artifact B の assessment を A に書き換えたものが素通りする（codex #4057191721）。**何とも照らされていない欄を記録しない** |
| 期待する artifact を、assessment が名乗る `artifact_sha256` と比べる | **攻撃されている欄同士を比べている。** A を期待する Gate に「B の判定を A と名乗らせた」ものが通り、推論 ID は B のままなので B の提案が採られる（codex #4057241944）。識別子の導出に入っている値と照らす |
| `ConfidenceAssessment` に digest（seal）を1つ足して自己整合だけを見る | 欄の書き換えは止まるが、**identity が宣言のままである。** どの推論・どの artifact のものかを外から確かめられない。導出（`inference_id`）で縛る |
| 入力 window そのものを assessment に持たせて識別子を作り直す | tick ごとに大きな object を運ぶ。digest（`input_sha256`）で同じ導出ができる |
| `ControllerGate` の `expected_artifact_sha256` に既定値を置く | 渡し忘れた配線が「何にも照らさない」Gate を作る。0057 §2.2 が `authority` を必須にしたのと同じ理由で、必須の引数にする |
| 「artifact を言えない適用 tick が無いこと」を、名指した arm だけに求める | 同じ holdout に v1〜v6 の tick を含む別の適用 arm が残っていても昇格できる（codex #4057191724）。gate の判定と同じく、報告に現れた Learned MPC の arm すべてに求める |
| `model_gate` の有無から「artifact 不明」を数える | `model_gate` が必須なのは v5 から。v1〜v4 の適用 tick が不明にも束縛済みにも数えられず、**欠けていることが消える**（codex #4057527947）。起点は `ControlState.active_controller` |
| 報告 v1 の欄の無さを `()` / `0` として読む | 「不明が1件も無い」＝完全に見える。`last_attested_ts_ms`（`None`＝言えない）とは向きが逆である（codex #4057527950）。version を上げ、v1 は昇格の証拠にしない |
| 適用 Learned MPC の arm に artifact の勘定が無いことを許す | 「数えていない」と「全部束縛できた」が同じ形になる。必ずどちらかを数える |
| Fallback の tick も「artifact 不明」に数える | 正しい姿を欠落として数えることになり、Fallback の多い報告が不明で埋まって昇格が永久に来ない |
| `ControlTick` の version を上げず、欄だけ足す | 0030 §2 が「schema の意味を変える場合は version を上げ、既存 trace を新しい意味として解釈しない」と決めている。上げないと、欄の無い v6 を「artifact 不明」ではなく「まだ書いていないだけ」と読める |
| 欄の無い古い tick を、同じ区間の別の tick の artifact で埋める | 推測である。**記録から言えないことを言わない**（0054 §2.2 と同じ向き） |
| `unbound_attested_ticks` を持たず、artifact の集合だけを見る | 新旧の trace が混ざった区間で、残った tick の artifact が区間全体の実績に見える。部分的な束縛が完全なものとして通る |
| artifact と「不明」を、最も新しい segment のものだけで見る | 別の artifact で回した古い segment が照合から消える。混ざった実績が1つの artifact を名乗れる |
| 報告の schema version を上げず、欄だけ足す | 欄の無さが `()` / `0`＝「不明が1件も無い」＝完全に見える。`last_attested_ts_ms`（`None`＝「言えない」）とは向きが逆なので、あのときと同じ扱いにはできない（codex #4057527950）。§2.3 のとおり v2 へ上げる |
| 完全性（すべての tick の勘定）を `AppliedArmReport` の型で要求する | 保存済みの v1 の報告が**読めなくなる**（codex #4057573943）。読めることと根拠にできることは別で、判定は版を知っている `EvaluationReport` 側で行う |
| artifact の集合だけで「束縛できた」と判断する | 集合は「どの artifact か」しか言わない。1 tick だけ束縛できた 100 tick の arm が完全に見える（codex #4057573941）。tick 数で突き合わせる |
| 適用側の arm を、報告全体の `model_artifacts` の照合だけで受け入れる | 照合が1箇所になる。arm ごとの束縛を持たないと、報告の素性の欄を1つ書き換えるだけで通る |
| rollout gate が通ったら自動で昇格する | Issue #92 の原則に反する。0057 §4 のまま、gate は助言で判断は人が行う |

## 5. 未決事項

- 所有者の承認（2026-09-21）で本記録は `FINAL` になった。**§2 の決定が変わるときは、
  書き換えずに新しい記録を作る**（`docs/decisions/README.md`「追記のみ」）。
  下の未決事項は、確定するまで開いたままである
- **適用された Learned MPC の optimizer 実績を trace に残すか**（0054 §5 / 0057 §3）。
  本記録では扱わない。`ControlTick` の追加が要るので、必要になったときに別の記録で決める
- **各段に必要な運転期間**（0057 §5 のまま）。いまも「その段で運転した証拠があること」しか
  求めていない
- **v6 から v7 への切り替え時期の運用。** 切り替え直後は `unbound_attested_ticks` を持つ
  区間が残るので、最初の EXPANDED への昇格は v7 だけで回した区間が貯まるまで通らない。
  運用の手順書（管理操作の入口。0057 §5）を決めるときに一緒に書く

**2026-09-21、リポジトリ所有者がこの記録を承認した。** 承認の対象は §2 の決定すべて
（artifact を `ModelGateDecision` に置くこと、`ControlTick` を v7 へ上げること、
推論の identity を宣言ではなく導出にすること、適用 arm の artifact の勘定と報告 v2、
適用側の arm を昇格の根拠にできるようにすること、§2.5 の
「記録の無さは常に unknown であって completeness ではない」という規則、
および 0057 §3 の帰結1項と §5 の未決1項の置き換え）である。
