# 決定記録 0059: decision trace が tick ごとに model artifact を記録する（適用側の証拠を artifact へ束縛する）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-20
- **Supersedes**: [`0057-authority-rollout-stage-changes.md`](0057-authority-rollout-stage-changes.md)
  の **§2.4 の「適用側の arm は昇格の根拠にできない」**、**§3 の
  「適用側（factual）の実績では昇格できない」**、**§5 の「decision trace へ、適用した
  tick の model artifact を記録すること（GitHub #159）」**。
  0057 のそれ以外の決定（stage の正本・承認の束縛・1段ずつ・自動降格・設定は上限・
  lock の順序）は**そのまま有効である**
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

`AppliedArmReport` に2つ足す。**0054 の帰属規則は変えない。** 記録から言える事実を増やした
だけで、どの実測をどの arm に帰属させるか（§2.1 / §2.2）も coverage の扱い（§2.3）も
gate の段（§2.4）も同じである。

| 欄 | 意味 |
|---|---|
| `model_artifacts` | この arm で**裏づけのある提案を適用した** tick の artifact（昇順・重複なし） |
| `unbound_attested_ticks` | 裏づけのある提案を適用したのに **artifact を言えなかった** tick の数 |

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

**報告の schema version は上げない。** 既存の欄の意味は変わらず、古い報告は欄が無い
（`()` / `0`）として読まれる。0057 §2.4 が `last_attested_ts_ms` を足したときと同じ扱いで、
**古い報告では適用側の arm が昇格の根拠にならない**（判断は fail closed のまま）。

### 2.4 適用側の arm を昇格の根拠にできるようにする（0057 §2.4 / §3 の置き換え）

`_check_learned_arms()` の「適用側の arm は根拠にできない」を外す。外すかわりに、
**適用側の arm には次をすべて要求する。**

- `unbound_attested_ticks` が **0**（artifact を言えない tick が1つも無い）
- `model_artifacts` が**空でない**（旧 version だけで回した区間を通さない）
- `model_artifacts` が **いま Production の artifact ちょうど1つ**である

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
| `ControlTick` の version を上げず、欄だけ足す | 0030 §2 が「schema の意味を変える場合は version を上げ、既存 trace を新しい意味として解釈しない」と決めている。上げないと、欄の無い v6 を「artifact 不明」ではなく「まだ書いていないだけ」と読める |
| 欄の無い古い tick を、同じ区間の別の tick の artifact で埋める | 推測である。**記録から言えないことを言わない**（0054 §2.2 と同じ向き） |
| `unbound_attested_ticks` を持たず、artifact の集合だけを見る | 新旧の trace が混ざった区間で、残った tick の artifact が区間全体の実績に見える。部分的な束縛が完全なものとして通る |
| artifact と「不明」を、最も新しい segment のものだけで見る | 別の artifact で回した古い segment が照合から消える。混ざった実績が1つの artifact を名乗れる |
| 報告の schema version を上げる | 既存の欄の意味は変わっていない。古い報告は欄が無い＝根拠にならないとして読まれ、判断は fail closed のまま（0057 §2.4 の `last_attested_ts_ms` と同じ扱い） |
| 適用側の arm を、報告全体の `model_artifacts` の照合だけで受け入れる | 照合が1箇所になる。arm ごとの束縛を持たないと、報告の素性の欄を1つ書き換えるだけで通る |
| rollout gate が通ったら自動で昇格する | Issue #92 の原則に反する。0057 §4 のまま、gate は助言で判断は人が行う |

## 5. 未決事項

- **所有者の承認が要る。** 本記録は 0057（`FINAL`）の §2.4 / §3 / §5 を置き換える。
  安全系・制御系の設計変更は人間レビューが必須である（AGENTS.md）。承認までは `Proposed`
- **適用された Learned MPC の optimizer 実績を trace に残すか**（0054 §5 / 0057 §3）。
  本記録では扱わない。`ControlTick` の追加が要るので、必要になったときに別の記録で決める
- **各段に必要な運転期間**（0057 §5 のまま）。いまも「その段で運転した証拠があること」しか
  求めていない
- **v6 から v7 への切り替え時期の運用。** 切り替え直後は `unbound_attested_ticks` を持つ
  区間が残るので、最初の EXPANDED への昇格は v7 だけで回した区間が貯まるまで通らない。
  運用の手順書（管理操作の入口。0057 §5）を決めるときに一緒に書く
