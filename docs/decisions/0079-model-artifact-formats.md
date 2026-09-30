# 決定記録 0079: Confidence Profile の artifact 化と Thermal Model との対の扱い、反実仮想 Thermal Model artifact（v2）の形式・検証・失敗時の意味

- **種別**: Decision Record
- **Status**: FINAL（2026-09-30、リポジトリ所有者が推奨案で承認。§6）
- **Date**: 2026-09-30
- **Supersedes**: 次の部分だけを置き換える（**v2 の Profile と artifact についてのみ**。v1 の Profile には
  従来どおり適用する）。
  - [0050](0050-model-confidence-ood-and-authority.md) §2.1 の「モデルに束縛する」の項のうち、Profile が持つ
    束縛の対象を artifact 全体の SHA-256 から model payload の SHA-256 へ替える部分（§2.1「Profile v2」）
  - [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.1 の attestation の表の `artifact_sha256` の行の
    「生成時: Confidence Profile の binding と照合」を、封をした型が同じ bytes から model と Profile を
    作ることの保証へ替える部分（§2.1「推論ごとの model binding」）
  - 0050 §2.2 の model binding の構成要素（予測の artifact SHA-256 を毎推論で照合する）は**置き換えない**。
    v2 でも同じ強さで残す（§2.1）
  - 旧記録側への `Superseded by` の追記（README「追記のみ」）は、2026-09-30 の所有者の承認を受けて
    本 PR で 0050 / 0052 に行った（§6 の質問 1）
- **関連**: [0027](0027-fan-control-architecture.md) / [0028](0028-fan-control-contracts.md) §2.4 / §2.7 / §2.9 /
  [0031](0031-thermal-dataset-contract.md) §2.1 / §2.2 / §2.6 /
  [0037](0037-model-registry-rollback-target.md) §2 / §5 /
  [0048](0048-thermal-model-artifact-and-inference.md) §2.1 / §2.4 / §3 / §5 /
  [0050](0050-model-confidence-ood-and-authority.md) §2.1 / §2.2 / §2.3 / §5 #3 /
  [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.1 / §2.3 / §2.4 / §5 /
  [0056](0056-model-drift-detection-and-retraining-triggers.md) §2.5 /
  [0058](0058-rl-supervisor-training-environment.md) §2.3 / §5 /
  [0059](0059-decision-trace-model-artifact.md) §2.1 / [0061](0061-rl-supervisor-policy-artifact-and-binding.md) §2.1 / §2.4 /
  [0062](0062-model-registry-operations.md) §2.4 / §5 /
  [0060](0060-control-loop-runtime.md) / [0071](0071-control-trace-read-api.md) §2.5 /
  [0075](0075-trace-registry-block-reason-digest.md) / [0076](0076-implementation-settled-points.md) /
  0077（open PR #196。Learned MPC worker の読み込みと受け渡し）/ 0078（open PR #194。`ControlTick` の版上げを含む）/
  `docs/model-registry.md` / `docs/requirements.md` Q-22 / Q-23
- **対象 Issue**: #104 / #84 / #85 / #86 / #105（`registry_attested` dynamics）。依存 #83

**2026-09-30 オーナーが推奨案で承認した。** 提案時に所有者へ問うた選択（§6）は推奨案を採り（質問 7 だけは
最終レビュー 5e1b742 の推奨で置き換えた）、同レビューで残った指摘も推奨どおりに直してから承認した。承認で本文へ反映した点は次のとおり。

- 候補の plan の範囲の照合には `range_margin` を**掛けない**（観測した範囲の外は評価しない。§2.5、§6 の質問 7）
- 提案時の前提「0037 / 0048 は `main` で `Proposed`」は古いので消した。PR #193 のマージで両者は `FINAL` であり、
  本記録はそれを土台として引く

実装は §2.9 の段 1 から順に進める。安全・制御に関わる判断を含むため、実装担当モデルに関係なく
各段の人間のレビューを必須とする（AGENTS.md「実装の担当」）。本記録はしきい値・安全の定数を新しく決めない。

## 1. Context

Learned MPC（#86）と `registry_attested` な学習 dynamics（#105）は、どちらも「候補の Fan action 列を
与えると将来の熱状態を返す」内部モデルを要求する（0052 §2.1 / 0058 §2.3）。そして現行のどの
artifact もそれを名乗れない。0048 §2.1 が v1 artifact の capability を `observational_replay` に
固定しているためで、これは意図した状態である。**v1 の後に何が来るのか**が決まっていないため、
次の未決事項が連鎖して止まっている。

| 未決事項 | 出どころ | 止まっているもの |
|---|---|---|
| Confidence Profile を `confidence_model` artifact として登録・昇格・rollback する経路 | 0050 §5 #3 | #85 の Registry 統合、#86 の Profile の出どころ |
| 複数 kind（confidence model など）を**同時に**入れ替える手順 | 0062 §5 | Thermal Model と Profile の組を安全に差し替える手順 |
| 時系列の候補 action 列を扱う dataset / model schema | 0048 §5 / 0052 §5 | #84 の v2、#86 の active 経路、#105 の `registry_attested` |
| 反実仮想モデルを Registry の loader だけが作れる型にすること、検証済み bytes との byte 一致 | 0052 §5 | #86 / #105 の束縛を「配線の誤りを型で止める」強さにすること |
| rollback target が無い・壊れていることを Rule Engine / Notification へ流すか | 0037 §5 / 0062 §5 | registry の健全性を運用者が見る経路 |

いまある事実を確認しておく。

- `ModelConfidenceProfile`（`coldaisle.confidence_profile` v1）は canonical JSON で、`ModelBinding` に
  model ID・版・**artifact 全体の SHA-256**・feature / target schema の checksum・split の checksum を
  持つ。Registry を通って読み込む経路は無く、呼び出し側が組み立てた object がそのまま
  `ConfidenceAssessor` に渡る。`LearnedMpcController` は生成時に Profile の binding と thermal
  attestation の model ID・版・artifact hash を照合する（`_check_binding_matches_policy`）
- Registry の `ArtifactKind` は `confidence_model` を持つが、その kind が申告できる
  `ArtifactCapability` は無い（`observational_replay` / `counterfactual_action` /
  `supervisor_strategy` の3つ）。0061 §2.1 が示したとおり、policy に thermal の能力を名乗らせるのは
  偽の申告で、Profile も同じである
- Registry は artifact を deserialize しない（`docs/model-registry.md`「安全境界」）。したがって
  「この Profile はどの thermal artifact のものか」を Registry が payload から読んで確かめることは
  できない
- `ControlTick` v10 以降は、tick が使っていた registry の kind ごとの production
  `artifact_sha256` を毎 tick 載せる（0071 §2.5 / 0075）。`model_gate.artifact_sha256`（0059）は
  判断を出した thermal artifact の hash である
- `ControlTick` は main で v12、open PR #192 が v13 へ上げる。`src/coldaisle/web/airflow-trace.js` の
  `KNOWN_VERSIONS` は v12 まで
- Confidence の判定器は推論ごとに、予測の model ID・版・`artifact_sha256`（attestation 由来の artifact
  全体の hash）を Profile の `ModelBinding` と照合する（0050 §2.2 の model binding。`control/model/confidence.py`）
- Metric の単位は `config/metrics.yaml`（`MetricCatalog`）が持つ。較正の変更は 0056 §2.5 で
  「人が宣言する変更」として証拠を切る
- 較正は**取り込み**（`coldaisle-daemon` の `Normalizer`）で掛かり、store には較正後の値だけが入る。
  どの較正で作った値かを store は記録しない。制御ループは store を読む（0060）
- `MpcProposal` は提案のある結果に `failure_reason` を付けられない。optimizer 自身の失敗の理由は
  `OptimizerOutcome.reason`（`infeasible` / `cost_unusable` など）に載り、Gate は `OptimizerStatus.ERROR`
  を detail の無い `optimizer_error` として Fallback にする

実機 dataset はまだ無い。**本記録は形式・束縛・検証・失敗時の意味だけを決め、数値は決めない。**

## 2. Decision

### 2.1 Confidence Profile は Thermal Model artifact v2 に**同梱**する（別 kind にしない）

反実仮想 Thermal Model artifact v2（§2.3）は、その model 専用の Confidence Profile を
**同じ artifact の中の1区画**として持つ。Profile を `confidence_model` kind の別 artifact として
登録しない。

- **組が1つの bytes になる。** promotion / rollback は既存の `thermal_model` kind の操作だけで、
  model と Profile が必ず一緒に動く。0037 の rollback target の選び方（checksum と format を通った
  最初のもの）がそのまま「正しい組」を選ぶ。**「新しい model と古い Profile」が production に
  並ぶ状態が構造上存在しない**
- **Registry の schema も CLI も変えない。** 新しい capability も、kind をまたぐ同時昇格の操作も
  要らない。0062 §5 の「複数 kind を同時に入れ替える手順」は、この組については**不要**として閉じる
  （他の kind の組み合わせについては開いたまま。§5）
- **decision trace の版を上げない。** `model_gate.artifact_sha256`（0059）が model と Profile の
  両方を指すので、適用側の証拠の束縛（0059 §2.3）が Profile の差し替えもそのまま切り分ける
- `confidence_model` kind は**将来の独立した uncertainty model**（アンサンブル・分位点など。0050 §5 #4）
  のために残し、本記録では使わない。使うときは capability の追加（Registry schema の版上げ。0061 §2.1
  と同じ規律）と、thermal artifact との束縛の決定記録を先に作る

**Profile を作り直すと model の版が上がる。** support 軸の変更（0050 §5 #2）や residual 下限の変更は、
係数が同じでも新しい版の artifact として登録し、人の承認を経て promotion する。OOD の基準は
authority の出方を変えるので、model の変更と同じ承認を通すのが正しい、と考える。

#### Profile v2（`coldaisle.confidence_profile` v2）

v1 の `ModelBinding.artifact_sha256` は「artifact 全体の hash」なので、artifact に Profile を
含めると循環する。v2 は次の点だけを変える。

| 項目 | v1 | v2 |
|---|---|---|
| model への束縛 | artifact 全体の SHA-256 | **model payload の canonical SHA-256**（manifest の `payload_sha256` と同じ値）と feature / target / action schema の checksum・split checksum |
| action の学習範囲 | anchor action の zone ごとの effective demand の範囲（`fan_ranges`） | v1 の欄に加え、**学習した action 列の範囲**（§2.5）: zone ごとの計画 demand の min / max、anchor → 最初の step の変化量の min / max と件数、step 間の変化量の min / max と件数 |
| 置き場所 | 単独の JSON | artifact v2 の `confidence_profile` 区画。manifest が `confidence_profile_sha256` を持つ |

作り方は 0050 §2.1 のまま（範囲・欠測・support は train だけ、residual の基準だけは validation、
test / purged は使わない、呼び出し側が明示する値に既定値を置かない）。v1 の Profile は
offline 評価の object として残し、Registry へは登録しない。

#### 推論ごとの model binding（0050 §2.2）を v2 でどう保つか

v2 の Profile は artifact 全体の hash を持てないが、0050 §2.2 の「予測の artifact SHA-256 が Profile と
違えば OOD」は**弱めずに残す**。

- 封をした型 `RegistryCounterfactualThermalModel`（§2.4）は、`VerifiedArtifact` の `artifact_sha256`
  （Registry が検証した bytes 全体の hash）と、manifest の `payload_sha256`・model ID・版を**同じ bytes から**
  取り出して持つ。判定器へ渡す runtime の束縛は、Profile v2 の binding（payload hash・schema・split）に、
  この型が持つ `artifact_sha256` を**型の中で**足したものとし、呼び出し側が組み立てられない
- 予測は従来どおり attestation 由来の `artifact_sha256` を持つ。判定器は推論ごとに、予測の model ID・版・
  `artifact_sha256` を上の runtime の束縛と照合し、違えば OOD（0050 §2.2 の構成要素そのまま）。加えて
  型の生成時に、Profile の `payload_sha256` が manifest の `payload_sha256` と一致することを L7 で確かめる
- これにより「Profile が束縛する payload」と「予測を出した artifact」が同じ bytes に属することを、生成時
  （L6 / L7）と推論ごと（0050 §2.2）の2段で確かめる。0052 §2.1 の「生成時: Profile の binding と照合」は、
  v2 では「同じ `VerifiedArtifact` から model と Profile を作った型であること」に置き換わる（Supersedes）

### 2.2 v1 artifact は変えない

`coldaisle.thermal_model` v1 は `observational_replay` のまま、Profile を持たない。v1 を
v2 として読み替えない。v1 を MPC / 学習 dynamics に束縛する経路は、0052 §2.1 / 0058 §2.3 のとおり
決定論的に拒否され続ける。

### 2.3 反実仮想 Thermal Model artifact v2 の形式

artifact は 0048 §2.4 と同じ規律に従う：Pydantic strict schema の canonical UTF-8 JSON だけ、
pickle / joblib / 任意 import / 実行可能 code を持たない、構造上限を展開前に検査する。

```text
ThermalModelArtifact v2  (schema_name "coldaisle.thermal_model", schema_version 2)
├─ manifest
│   ├─ identity: model_id, model_version(semver), created_at(RFC 3339, TZ 付き), model_family
│   ├─ capability: counterfactual_action          # v2 ではこの値だけ（Literal）
│   ├─ authority_compatibility                   # SHADOW から順に。実測評価で裏づけた stage だけ
│   ├─ training_data:
│   │   dataset_alias, dataset_schema_version(=2), manifest_sha256, examples_sha256,
│   │   source_runs[{alias, source_sha256}], split_sha256,
│   │   window: {train: [start_ms, end_ms], validation: [...], test: [...]},   # 学習データの時間窓
│   │   train_example_count, action_variation_summary（zone ごとの action 変化の件数）
│   ├─ schemas: feature(thermal-features-v2) / target(thermal-targets-v1) / action(thermal-actions-v1)
│   │           それぞれの版と canonical SHA-256
│   ├─ metric_binding:                             # 使った metric の意味と単位
│   │   entries[{metric, unit, derived: {minuend, subtrahend} | null}], sha256
│   ├─ calibration_binding: {sha256 | null}         # 使った metric に関わる較正値の digest
│   ├─ hyperparameters（有限な scalar の要約）, code_commit（取得できれば）
│   ├─ payload_sha256                              # model payload の canonical SHA-256
│   └─ confidence_profile_sha256                   # 同梱 Profile の canonical SHA-256
├─ payload                                         # model family 固有の数値だけ
└─ confidence_profile                              # §2.1 の Profile v2
```

- **action schema `thermal-actions-v1`** は `step_ms`・step 数・zone の順（Front / Rear / Top）・
  単位（demand、0.0..1.0）と、**action の出どころ `action_source`**（v1 では `effective_demand` だけの
  Literal。0031 §2 の「熱応答の action は effective_demand」に従う）を持つ。applied demand へ替えるなら
  action schema の版を上げ、0031 を置き換える新しい記録を作る（§5 #2）。出どころの違う artifact を
  取り違えて読まない。推論の入力は 0052 の `PlannedThermalInput`（観測 window と
  `ActionPlan`）で、**plan の offset 列は action schema の格子と完全に一致しなければならない。**
  補間・外挿・丸めはしない。target schema の horizon 列もこの格子と一致させる（0052 §2.3 の
  「設定の上限を予測の契約へ合わせる」と同じ向き）
- **plan の demand は「その値が effective として掛かった」という仮定として model へ渡す。**
  `ActionPlan` は requested しか表現しない（0052 §2.4。`control/mpc/plan.py`）が、学習の action は
  effective である。この2つを同じ意味で扱えるのは、候補が 0052 §2.4 の写し（Safety の最低 demand・
  直前の effective からの変化幅）で既に狭めてあり、Reactive Guard / Critical Safety が手を入れない
  tick では requested と effective が一致するからである。Guard / Safety が値を変えた tick では、
  その tick の予測は実際に掛かる action と違う仮定の上に立つ。本記録ではこの差を model の入力へ
  持ち込まない（MPC は `control/safety` / `control/reactive` を import しない。0052 §2.4）。
  次の tick の anchor と変化幅の起点は観測 window の effective から取り直すので（0052 §2.2 / §2.4）、
  差は1 tick で観測の側へ戻る。requested と effective の差そのものは既存の decision trace に並んで
  残る。§2.5 の範囲の照合も同じ仮定（plan の値を effective の仮定として）で行う。
  **これは Guard / Safety の後段の裁定を変えない。** 差をこれ以上は扱わない（§6 の質問 10 で (a) を承認）。
  Guard / Safety の決定論的な写しを MPC 側へ持つ、差が出た tick の予測を評価から除く等へ広げるなら、
  新しい決定記録を先に作る（§5 #14）
- **feature schema `thermal-features-v2`** は v1（0048 §2.1）の列に、計画 action の列
  （step × zone）を足す。anchor action が「いま掛かっている effective demand」であることは v1 と同じ
- **target の horizon より後の action は、その horizon の予測に使わない（因果の mask）。**
  step `k` の action は `[k × step_ms, (k + 1) × step_ms)` に掛かるものとし、horizon `h_ms` の target が
  使ってよい計画 action の列は `k × step_ms < h_ms` を満たす step だけとする。0048 の ridge 出力は
  horizon ごとに全 feature への係数を持つので、v2 の payload では**それ以外の計画 action の列の係数を
  厳密に 0 とする**（trainer はその列を落として当てはめる）。過去の制御では前後の step の action が
  相関するため、mask が無いと近い horizon の予測が物理的に後の操作に依存し、オフライン評価と MPC の
  コストを歪める。mask は feature schema と target schema の格子から一意に導けるので、別の欄を持たず、
  loader が L10 で検査する
- **学習データの時間窓**は split の各集合の `[history_start_ms の最小, label_end_ms の最大]` を持つ。
  drift の判断（0056）と「いつのデータで学習したか」の説明に使う。hostname・path などの
  実機識別子は持たない（0031 §2.3 の alias の規律、AGENTS.md ルール10）
- **metric の意味と単位の束縛**: feature / target の全 metric について、学習時の `MetricCatalog` の
  単位（派生値なら引き算の定義も）を写し、その canonical SHA-256 を持つ。表示名（label）は
  含めない（表示の変更で model を失効させない）
- **較正の束縛**: 学習データに効いていた較正値のうち、使った metric に関わるものの digest を持つ。
  較正を使わない metric だけなら `null`。store は較正の出どころを記録しないので（§1）、digest は
  dataset を作る側が明示して渡す（既定値を置かない）
- **較正の変更をまたぐ学習データは作らない。** Dataset v2 の生成（段 1）は、呼び出し側が渡す
  0056 §2.5 の宣言された変更（`DeclaredChange`）のうち較正の変更が、**全 split を通した期間**
  `[全集合の history_start_ms の最小, 全集合の label_end_ms の最大]` の中にあれば**拒否する**
  （窓を分けて作り直すのは人の判断）。集合ごとの窓だけを見ると、0031 §2.5 の purge で空いた集合の
  間の隙間に変更があるとき、変更前の train と変更後の validation / test が1つの artifact に入ってしまう。
  全 split が1つの較正の期間に収まることが、1つの artifact に1つの較正 digest しか持たせないための条件である
- **digest は4層**：Registry の `artifact_sha256`（登録した bytes 全体）、manifest の
  `payload_sha256` と `confidence_profile_sha256`、schema ごとの checksum、学習データの checksum。
  **登録する bytes は canonical 直列化そのもの**とし、同じ artifact が2通りの bytes を持たない
- **版の付け方**: bytes が1 byte でも違えば model の semver を変える（Registry は同じ版の再登録を
  拒否する）。形式の意味を変えるときは `schema_version` を上げ、古い版を新しい意味で読まない
- 最初の model family は 0048 §2.3 と同じ決定的な ridge 線形（計画 action の列を含む）でよい。
  family の優劣は実データの評価で決める（§5）

Registry へは既存の `thermal_model` kind・`counterfactual_action` capability で登録する。
`ThermalRegistryMetadata` は manifest から導き、**artifact が決める metadata の欄はすべて照合する**
（0061 §2.4 と同じ。欄を手で並べず、lifecycle 中に Registry が書く評価参照だけを名前で除く）。

### 2.4 読み込み時の検証（すべて通ったときだけ型ができる）

制御と学習 dynamics が受け取る型は、**Registry の検証経路が発行した `VerifiedArtifact` からだけ
作れる封をした型** `RegistryCounterfactualThermalModel` とする（公開 constructor を持たない。
0052 §5 の1行目と3行目をここで閉じる）。同じ bytes から model と Profile を**両方**組み立てて返し、
別々に渡す API を作らない。loader は次を順に検査し、1つでも外れたら型を作らない。

| # | 検査 | 外れたとき |
|---|---|---|
| L1 | `VerifiedArtifact` と attestation（kind `thermal_model`、capability `counterfactual_action`）を伴う | 拒否 |
| L2 | bytes の大きさと構造上限（0048 §2.4）を展開前に検査 | 拒否 |
| L3 | bytes が canonical 直列化と完全一致 | 拒否 |
| L4 | `schema_name` / `schema_version` が v2。v1 は反実仮想の型にならない | 拒否 |
| L5 | manifest から導いた Registry metadata の全欄が attestation / metadata と一致 | 拒否 |
| L6 | `payload_sha256`・`confidence_profile_sha256`・schema checksum を再計算して一致 | 拒否 |
| L7 | Profile の binding が manifest の payload・schema・split と一致 | 拒否 |
| L8 | metric binding の全 entry が runtime の `MetricCatalog` に同じ単位（派生値は同じ定義）で存在 | 拒否 |
| L9 | 較正の digest が runtime の較正と一致（**不一致は拒否**。§6 の質問 4） | 拒否 |
| L10 | payload の各 horizon で、§2.3 の因果の mask の外（horizon より後の step）の計画 action の係数がすべて厳密に 0 | 拒否 |

**L9 の「runtime の較正」**は `coldaisle-fand` が起動時に1回だけ読む較正ファイル
（取り込みと同じ `config/calibration.json` の path を設定で受け取る。既定の path を制御側に置かない）の、
使った metric に関わる値の digest とする。ファイルを書き換えてから取り込みを再起動するまでの間は、
store の値を作った較正と食い違いうる。この隙は本記録では**閉じない**（取り込みが使った較正の digest を
store へ記録しない。§6 の質問 5）。較正の変更は 0056 §2.5 で人が宣言するものなので、宣言と同時に
取り込みと `coldaisle-fand` を再起動する手順で運用する。

さらに利用側の生成時に次を照合する（tick ごとに失敗させない。0052 §2.6 と同じ）。

- #86: `mpc.optimizer` の `step_ms` と step 数が action schema の格子と一致し、`cost_metrics` が
  target schema に含まれる。既存の `MpcModelBinding.for_control` の条件（production pointer・
  authority・版・schema）はそのまま
- #86 と 0077（open PR #196）: 0077 の MPC worker が registry から artifact を検証して読み込む段は、
  **`VerifiedArtifact` から本型を作る1つの呼び出し**にする。worker が `VerifiedArtifact` と、別に組み立てた
  model / Profile を並べて持つ経路を作らない。本記録の段 4 と 0077 の読み込みの段は同じ境界を指すので、
  先にマージされた方の実装にもう一方を合わせる
- #86 / #85: Confidence 判定器は**同梱の Profile から作る**。別の Profile を渡す引数を制御側に持たせない
- #105: `AttestedThermalDynamics.bind` は `RegistryCounterfactualThermalModel` だけを受け取る
  （`production_active` は従来どおり要求しない。0058 §2.3）

### 2.5 学習した action 列の外は探索しない（0052 §2.4 への追加。狭める向きだけ）

Confidence / OOD（0050）は **anchor 推論**を判定する。反実仮想モデルでは、optimizer が評価する
**候補 plan** が学習した action の範囲を外れることがあり、そこでの予測は外挿である。0050 の判定は
それを見ない。そこで、0052 §2.4 の探索範囲に4つ目の「狭める写し」を足す。

- 候補 plan の zone ごとの demand が Profile v2 の計画 demand の範囲（**観測した min / max そのもの**）を
  外れるか、変化量が学習した変化量の範囲（同じく観測した min / max そのもの）を外れる候補は**評価しない**
- **変化量には anchor から最初の step への遷移を含める。** 現行の optimizer は `ActionPlan.held` で
  step の間を一定に保つ plan を作るので、step 間の変化量だけを見ると常に 0 で通り、anchor の effective
  demand 0.2 から plan 0.9 への跳びが、両端の値が範囲内であれば評価されてしまう。Profile v2 は
  「anchor action → 最初の step」の変化量（zone ごと・符号付きの min / max と件数）を step 間の変化量と
  **別の欄**として持ち、候補ごとに anchor の effective demand からの遷移をこの範囲で照合する
- **Fallback の requested（incumbent の出発点。0052 §2.3）自体が範囲外なら、optimizer は解を返さない**
  （`OptimizerOutcome` の `status = error`、`reason.code = plan_out_of_learned_range`、detail に外れた
  zone と量）。`MpcProposal.failure_reason` には書かない（提案のある結果に付けられない。§1）。
  この理由は既存の経路どおり提案の zone ごとの requested の理由に載り、Gate は既存の `optimizer_error`
  として detail なしで Fallback にする。Gate の `fallback_reason` の detail に範囲外を出すかは Gate と
  trace の変更になるのでしない（§5 #7、§6 の質問 6）。Baseline を範囲内へ丸めた値を出発点に
  しない（丸めた値は Fallback ではなく、ML の外挿が選んだ値になるため）
- **候補の plan（と Fallback の requested の照合）には margin を掛けない。** 観測した範囲の外の値や
  変化量を1つでも含む候補は評価しない。`model_confidence.range_margin` は従来どおり anchor 推論の
  OOD 判定（0050）にだけ掛かる。候補の plan は anchor の OOD 判定を通らないので、margin を候補へ
  掛けると、たとえば観測 0.2〜0.8・margin 0.05 のとき約 0.17〜0.83 の学習していない action を
  optimizer が選べ、外挿の予測がそのまま Demand の選択に効く（最終レビュー 5e1b742 の P1）。
  専用の margin の設定値も足さない（§6 の質問 7）
- 0052 §2.4 のとおり、**これは安全上の保証ではない。** 後段の Reactive Guard と Critical Safety は
  常に掛かる。ここは「外挿の予測で Demand を選ばない」ための写しである

### 2.6 失敗時の意味（fail-safe）

- §2.4 の検査に1つでも外れた artifact、同梱 Profile が壊れている artifact、格子が合わない設定は、
  すべて `MpcModelUnusableError` → `LearnedFailure.MODEL_LOAD_FAILURE`（`failure_reason` 付き）として
  Gate へ渡し、**Fallback で運転を続ける**（AGENTS.md ルール4、0052 §2.1）。起動を止めない
- **暗黙の降格はしない。** 反実仮想 artifact を「Profile なしで使う」「observational として制御で
  使う」「別の Profile で使う」経路を作らない。使えないなら使わない
- Registry の `verify`（0062 §2.4）は、`thermal_model` の runtime contract を
  `thermal-features-v2` / `thermal-targets-v1` で渡したとき、v1 の production を `unusable` と
  報告する（schema 不一致）。これは Fallback を意味し、正しい
- Thermal Model / loader / Profile は Demand・PWM・authority を返さず、`control.hardware` /
  `control.safety` / `control.reactive` を import しない（0048 §2.1 / 0050 §3 と同じ）
- LLM 層へはこの artifact も読み込み結果も出さない（AGENTS.md ルール1 / 8）

### 2.7 実機なしでの試験

すべて `-m "not hardware"` で走る。

- 合成 dataset v2（決定的な生成器）から artifact v2 を学習し、同じ入力から同じ bytes が出る
- L1〜L10 のそれぞれについて、1箇所だけ壊した artifact / 設定（1 byte 改変・非 canonical な並び・
  Profile の binding 違い・単位違いの catalog・較正 digest 違い・v1 artifact・格子違い・horizon より後の計画 action に 0 でない係数）を与え、
  **型が作られず、runtime が `MODEL_LOAD_FAILURE` として Fallback の requested を出し、Guard /
  Safety の後段の結果が変わらない**ことを確かめる
- 範囲外の Fallback requested で optimizer が `error`（`plan_out_of_learned_range`）を返し、Gate が Fallback を選ぶ。
  `MpcProposal` の不変条件（提案のある結果に `failure_reason` を付けない）を破らない
- 範囲外の候補が評価されない（評価回数と選ばれた解で確かめる）。観測した範囲の端をわずかに越える
  （`range_margin` の幅の中に入る）候補も評価されない。**anchor から最初の step への跳びだけが
  範囲外で、各 step の値と step 間の変化量は範囲内の held plan** も評価されない
- model binding: 同じ model ID・版で別の bytes の artifact から出た予測（`artifact_sha256` だけ違う）を
  v2 の判定器へ渡すと、model binding の構成要素が OOD になる。Profile の `payload_sha256` だけを
  書き換えた artifact は L6 / L7 で型にならない
- 較正: 宣言された較正の変更を train の窓の中に含む入力で Dataset v2 の生成が拒否される。窓の外なら通る
- 一時 directory の Registry で「v2 を promotion → rollback」したとき、model と Profile が組のまま
  戻る（組の食い違いが作れない）
- 既存の import 走査試験（`control/model` と `control/mpc` が hardware / safety / reactive を
  import しない）をそのまま通す

### 2.8 Registry の健全性を Rule Engine / Notification へ流すかは、**本記録では決めない**

0037 §5 / 0062 §5 の論点は**明示的に開いたまま**にする。決めないのは、選択が運用形態
（`coldaisle-fand` の常駐化は 0069 が範囲外にした）に依存し、本記録の形式の決定と独立しているため
である。ただし、どの経路を選んでも守る条件をここで固定する。

1. 制御ループの中で通知しない。制御は通知の成否に依存しない
2. 読み取り専用。健全性の報告から registry を直さない・pointer を動かさない（0062 §4）
3. 報告の原資は `RegistryHealthReport` とし、判定を別に作り直さない
4. LLM へ生の報告を渡さない（ルール 8）

選択肢は §4 の表（案 H1〜H3）に挙げた。**方向は H2**（Rule Engine を通さず、読み取り専用の
定期 job が `verify` の結果の遷移を通知へ流す）だが、決めるのは #82 / #20 の統合時とする
（2026-09-30 オーナーが「本記録では開いたままにする」推奨案を承認。§6 の質問 9）。

### 2.9 実装の段階

| 段 | Issue / PR | 内容 | `ControlTick` の版 |
|---|---|---|---|
| 0 | 本 PR | 本記録（2026-09-30 承認・FINAL） | 変えない |
| 1 | #83 | Thermal Dataset v2：各 example に anchor から `label_end_ms` までの **action 列**（action schema の格子上の、`action_source` が指す値。本記録と 0031 §2 では effective demand。applied へ替えるなら 0031 を置き換える記録が先。§5 #2）と、その元の ControlTick の時刻）を持たせる。較正の変更をまたぐ窓は拒否する（§2.3）。v1 を v2 として読み替えない。0031 §2.1〜§2.6 の規律（時刻対応・mask・split・値を既定しない）はそのまま | 変えない |
| 2 | #84 | artifact v2・trainer・`RegistryCounterfactualThermalModel.from_verified_artifact`（L1〜L10）・`ThermalRegistryMetadata` の全欄照合 | 変えない |
| 3 | #85 | Profile v2 の生成（payload 束縛・action 列の範囲）と同梱。制御用の判定器は同梱 Profile からしか作れないようにする | 変えない |
| 4 | #86 | 束縛を段 2 の型へ切り替え、格子の照合と §2.5 の探索範囲の写しを足す | 変えない（既存の `optimizer_status` / `failure_reason` に載せる） |
| 5 | #104 | runtime contract の例と `docs/model-registry.md` を v2 に合わせる。`verify` の挙動は変えない | 変えない |
| 6 | #105 | `AttestedThermalDynamics.bind` を段 2 の型だけにし、同梱 Profile で step の OOD を**記録**する（`promotable` の条件は変えない。§5） | 変えない（trace ではない） |

- 段 1 → 2 → 3 → 4 の順に依存する。段 5 は段 2 の後ならいつでもよい。段 6 は段 3 の後
- **本記録では `ControlTick` の版を上げない。** 後から trace へ何かを足す（たとえば §2.5 で除いた
  候補の件数を記録する、新しい記録で Profile の hash を `model_gate` に載せる）場合は、
  - 他の版上げ（open PR #192 が v12 → v13、open PR #194 の 0078 が `air_balance_coordination` でその次）と
    **直列化**し、マージ時点の次の空き番号を使う
    （0073 が v10 と書いて v11 になった前例。0073 §5）
  - `src/coldaisle/web/airflow-trace.js` の `KNOWN_VERSIONS` とその試験を同じ PR で更新する
  - 入れ子の `ModelGateDecision` を変えるなら、その版も上げる（0065 §2.2）

## 3. Consequences

良くなること。

- **Thermal Model と Profile の組が production で食い違う状態が作れない。** promotion / rollback /
  0037 の rollback target の選び方が、すべて組の単位で働く
- #86 / #105 が「反実仮想を申告した artifact」を受け取れる形式が定まり、0052 §3 の
  「active 制御に使える内部モデルが1つも無い」状態から抜ける道筋ができる（抜けるのは実データで
  学習・評価し、人が承認したとき）
- 単位・metric の意味・較正が学習時と違う runtime では、その artifact が**使われない**。
  値の意味がずれた予測で Demand を選ぶ経路が塞がる
- 学習した action 列の外で optimizer が外挿の予測を比べない
- Registry の schema・CLI・decision trace の版を動かさずに済む

悪くなること（と緩和策）。

| 悪くなること | 緩和策 |
|---|---|
| Profile だけを直したいときも model の版を上げ、人の承認を通す必要がある | OOD の基準の変更は authority の出方を変えるので、承認を通すのは意図どおり。係数が同じことは manifest の `payload_sha256` が同じことで読める |
| artifact が大きくなる（8 MiB の上限を Profile と分け合う） | Profile は support cell と欠測の組み合わせに構造上限を持つ（0050）。上限に当たれば登録で拒否され、黙って切られない |
| 較正を変えるたびに artifact が使えなくなる（L9） | 0056 §2.5 が較正の変更で証拠を切るのと同じ向き。運用上重すぎれば新しい決定記録で「記録のみ」に切り替える（§5 #3） |
| `confidence_model` kind が当面使われない | 将来の独立した uncertainty model のために残す。使うときは決定記録を先に作る |
| Dataset v2 を作るまで何も active にならない | いまと同じ（0052 §3）。段 1 から順に進める |
| 観測ログから学んだ「反実仮想」は、ログの action が状態に依存して選ばれていれば交絡する | capability は申告であって科学的保証ではない。SHADOW より上の authority は 0054 の gate と人の承認（0057）を通る。action の変化の量は manifest の `action_variation_summary` で読める。十分な励起の条件は実機で決める（§5） |

## 4. 却下した代替案

### Profile と Thermal Model の対の扱い

| 案 | 内容 | 利点 | 欠点 |
|---|---|---|---|
| **案 1（採用）同梱** | §2.1 | 組の食い違いが構造上起きない。Registry / trace の版が動かない | Profile だけの差し替えでも model の版と承認が要る |
| 却下: 案 2 別 kind ＋ metadata の束縛 ＋ 同時昇格 | `confidence_model` に新しい capability（例 `confidence_profile`）を足し、`ArtifactMetadata` に対の thermal artifact（ref と SHA-256）を持たせる（Registry schema v4。0061 §2.1 と同じ版上げ）。thermal と Profile を**1つの revision で原子的に**昇格・rollback する操作を足し、rollback target は「互いに束縛し合う組」で選ぶ（0037 の拡張） | Profile だけを差し替えられる。0050 §5 #3 の文面どおり | Registry の schema・CLI・audit・0037 の選び方がすべて変わる。Profile の hash を trace へ載せるなら `ModelGateDecision` と `ControlTick` の版上げ（直列化が要る）。実装量が最も多い |
| 却下: 案 3 別 kind ＋ 実行時の照合だけ | 案 2 の schema 変更はするが、同時昇格は作らない。順に昇格し、食い違う間は MPC の生成時照合で拒否され Fallback | Registry の操作を変えない | 昇格・rollback のたびに Fallback の期間ができうる。0037 の選び方が kind ごとに独立なので、rollback 後に**組が揃わない**ことがあり、そのとき人が気付く経路が `verify` しかない |
| 却下: 別 kind で束縛を payload の中だけに持つ | Registry が payload を読まないので、昇格時に組を確かめられない | — | 食い違いに気付くのが制御の起動時になる |
| 却下: Profile を Registry を通さずファイルで渡す | — | — | Profile の差し替えが承認も audit も通らない。OOD の基準を黙って変えられる |

### 反実仮想 artifact の形式

| 案 | 却下理由 |
|---|---|
| v1 の manifest の `capability` を広げて v2 を作らない | 0048 §2.1 の「v1 を新しい意味で読まない」に反する。v1 の bytes が反実仮想を名乗れる余地ができる |
| plan の offset を学習時の格子へ補間する | 学習していない時刻の action を model に与えることになる。格子の不一致は拒否する |
| metric を名前だけで束縛し、単位を見ない | 単位の変更（たとえば摂氏と華氏、% と 0..1）が黙って通る |
| 表示名（label）まで束縛に含める | 表示の変更だけで model が失効する |
| 検査に外れた artifact を SHADOW だけで使う | 0052 §2.1 の「暗黙の降格はしない」に反する。壊れた artifact の予測が証拠に混ざる |
| pickle / ONNX など別形式を許す | 0048 §2.4 / `docs/model-registry.md` の非実行の規律。構造 validator が無い |
| 範囲外の Fallback requested を範囲内へ丸めて探索を続ける | 丸めた値は Fallback ではなく ML の外挿で選んだ値になる。`error` として Fallback にする |
| 候補 plan の範囲の照合に `range_margin` を掛ける | 候補は anchor の OOD 判定を通らないので、margin の幅だけ学習していない action を optimizer が選べる（§2.5） |
| 候補 plan の範囲の照合に専用の margin の設定値を足す | 外挿を許す幅を新しく作ることになり、`fan-policy.yaml` の版も上がる。観測した範囲の外は評価しない |
| 候補 plan の範囲外を OOD として confidence を 0 にする | anchor 推論の判定（0050）と plan の探索の話を混ぜる。候補を除くほうが「狭める向きだけ」（0052 §2.4）と整合する |

### Registry の健全性の通知（§2.8 で開いたまま。どれも却下はしていない）

| 案 | 内容 | 利点 | 欠点 |
|---|---|---|---|
| H1 Rule Engine | `verify` の結果を `sys.*` の metric として store へ書き、ルールで発火・解除 | 既存の遷移・連投抑制（0013）をそのまま使える | Rule Engine は取り込みと同じプロセス・同じ時計で telemetry を評価する（0012 §2.1）。時系列でない registry の状態を metric にする書き手が要り、書き手の選び方が新しい決定になる |
| **H2（方向）通知へ直接** | 読み取り専用の定期 job が `verify` を実行し、前回と違う結果（遷移）だけを通知へ渡す | Rule Engine と取り込みに手を入れない。制御から独立 | 前回結果の保存先と job の常駐化（0069 の範囲外）を決める必要がある |
| H3 流さない | `coldaisle-registry verify` の終了コード（0062 §2.4）と trace の `registry` の塊（0071）だけで見る | 追加の仕組みが要らない | 戻り先を失ったことに気付く保証が無い（0037 §5 の問題が残る） |

## 5. 未決事項

2026-09-30 の承認で決着した項も、番号を保つため行を残し「決着」と書いた。

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | §4 の案 1 / 2 / 3 の選択 | **決着**：案 1 同梱（§6 の質問 2） |
| 2 | Dataset v2 の action 列の出どころ。**決着**：effective demand（0031 §2.2 と同じ。§6 の質問 3）で段 1 を進める。applied demand（`ControlTick` v11 以降、PWM へ写す直前）との差が熱応答に効くかは**実機で測る** | 見直すなら #83 の実機計測の後、0031 を置き換える新しい記録と action schema の版上げ |
| 3 | 較正の digest。**決着**：不一致は拒否して Fallback（§6 の質問 4）、取り込みが使った較正の digest は store へ記録しない（§6 の質問 5） | 較正の頻度が実運用で分かった後、見直すなら新しい記録 |
| 4 | 反実仮想 capability を登録時に申告してよい条件。**決着**：Dataset v2 由来なら申告してよい。SHADOW より上は既存の gate（0054）と人の承認（0057）で止める（§6 の質問 11）。action の励起の量・分布を登録の条件に足すかと、励起の実験（安全範囲内の step 応答など）の設計は開いたまま | #83 / #84 / #91。**実機で測った後**、足すなら新しい記録 |
| 5 | model family・ridge lambda・window / horizon / step 格子・target metric 集合の実値（0048 §5 / 0052 §5 / Q-22） | 実機 dataset の評価の後、#103 の設定と後続の記録 |
| 6 | 候補の plan の範囲の margin。**決着**：margin を掛けない（観測した範囲の外は評価しない）。専用の設定値も足さない（§2.5、§6 の質問 7） | — |
| 7 | §2.5 で除いた候補の件数や理由、`plan_out_of_learned_range` を trace や Gate の `fallback_reason` の detail に載せるか。**決着**：載せない（§6 の質問 6） | 載せるなら新しい記録（Gate の変更と `ControlTick` の版上げ。§2.9 の直列化の規則に従う）。実装は #86 / #82 |
| 8 | #105 の episode で同梱 Profile が OOD と判定した step を `promotable` の条件に入れるか（0058 §2.3 の7条件を変えるので新しい記録が要る）。段 6 は記録だけにする | #105 の後続の記録 |
| 9 | Registry の健全性を Rule Engine / Notification へ流す経路（§2.8。H1〜H3）。本記録では決めないことを承認した（§6 の質問 9） | #82 / #20 の統合時（方向は H2） |
| 10 | thermal と Profile 以外の kind の組（supervisor policy と thermal model など）を同時に入れ替える手順（0062 §5 の残り） | 必要が生じたとき |
| 11 | uncertainty を出す model（アンサンブル・分位点）と、そのときの `confidence_model` kind の使い方（0050 §5 #4） | #84 の後続 |
| 12 | `coldaisle-registry status` が manifest の学習データの時間窓・metric 束縛を表示するか（Registry は payload を読まないので、別の読み取り専用 tool になる） | #104 |
| 13 | retired artifact の bytes の保持期間（0062 §5） | 変わらず開いたまま |
| 14 | plan（requested）を effective の仮定として model へ渡すこと（§2.3）の差。**決着**：1 tick で観測へ戻し、差は既存の decision trace に残すだけにする（§6 の質問 10）。差の頻度は #90 / #91 の Shadow・評価で測る | 扱いを広げるなら新しい記録 |

**実機の計測を待つ値**：#2 の差の大きさ、#4 の励起の条件、#5 の格子と family と正則化。
いずれも `status: provisional` の設定または後続の決定記録で扱い、本記録はコードに既定値を置かない（AGENTS.md ルール9）。

## 6. 所有者の決定（2026-09-30）

提案時に番号付きで問うた選択に、2026-09-30 オーナーが「推奨案で直してから承認」と答えた。
各問の答えを残す（番号は本文からの参照を保つため提案時のまま。質問 8 は下の注記のとおり削除した）。

- **質問 1. 0050 / 0052 の部分的な置き換え**（ヘッダの Supersedes）→ **承認**。v2 の Profile の束縛先を payload hash にし、
  0052 §2.1 の「生成時: Profile の binding と照合」を封をした型の保証に替える。0050 §2.2 の推論ごとの照合は残す。
  0050 / 0052 のヘッダへ `Superseded by: 0079（該当部分のみ）` を本 PR で追記した
- **質問 2. Profile と Thermal Model の対**（§4）→ **案 1 同梱**（§2.1）。案 2 / 案 3 は却下
- **質問 3. Dataset v2 の action の出どころ**（§5 #2）→ **effective demand**（`action_source = effective_demand`）で段 1 を進め、
  実機で差を測ってから見直す
- **質問 4. 較正の digest の不一致（L9）**（§5 #3）→ **artifact を拒否し Fallback**
- **質問 5. 取り込みが使った較正の digest を store へ記録するか**（§2.4）→ **記録しない**。較正の変更の宣言と同時に
  取り込みと `coldaisle-fand` を再起動する運用で埋め、実運用で食い違いが起きたら別の記録で足す
- **質問 6. `plan_out_of_learned_range` と除いた候補の件数を Gate の detail や trace に出すか**（§5 #7）→ **出さない**
  （`OptimizerOutcome.reason` と提案の requested の理由に残すだけ。Gate・`ControlTick` の版を動かさない）
- **質問 7. plan の範囲の margin**（§5 #6）→ **候補の plan には margin を掛けない**（観測した範囲の外は評価しない）。
  提案時の推奨は「既存の `model_confidence.range_margin` を使う」だったが、最終レビュー（5e1b742）の P1
  （margin が学習データの外の action を許す）への推奨「候補の action には margin を掛けない」を採って承認した。
  専用の設定値を足す案も採らない（§2.5、§4）
- **質問 8.** 削除。提案時の問い「本記録の承認を PR #193（0037 / 0048 を FINAL にする）のマージの後にするか」は、
  #193 のマージで前提が満たされたので問う必要が無くなった（最終レビュー 5e1b742 の P2）
- **質問 9. Registry の健全性の通知経路**（§2.8、§5 #9）→ **本記録では決めない**。#82 / #20 の統合時に決める（方向は H2）
- **質問 10. plan（requested）と学習の action（effective）の意味の差**（§2.3、§5 #14）→ **(a)**：plan を「effective として
  掛かった」仮定として渡す。差が出た tick は次の tick の anchor で観測へ戻し、差は既存の decision trace に残すだけにする。
  Guard / Safety の後段の裁定は変えない
- **質問 11. 反実仮想 capability を申告してよい条件**（§5 #4。提案時は PR の説明で問うた）→ **Dataset v2 由来なら申告してよい**。
  SHADOW より上の authority は既存の gate（0054）と人の承認（0057）で止める。励起の量を登録の条件に足すかは実機で測った後
- **Fallback の requested が学習範囲の外にあるとき**（§2.5。提案時は PR の説明で問うた）→ **optimizer は `error` を返して Fallback にする**。
  範囲内へ丸めて探索を続ける案は却下（§4）
