# 決定記録 0097: 0079 段 2 / 段 3（artifact v2・Confidence Profile v2。PR #226 / #230）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が承認）
- **Date**: 2026-10-07
- **Supersedes**: なし（0079 / 0084 / 0087 が決めていなかった点への追加と、記録の文面だけでは読み方が分かれうる点の
  明記。どの記録の決定も置き換えない）
- **関連**: [0079](0079-model-artifact-formats.md) §2.1 / §2.4 / §2.9 /
  [0084](0084-model-artifact-anchor-support.md) §2.1 / §2.2 / §5 #2 /
  [0087](0087-dataset-v2-action-grid.md) §2.7 /
  [0050](0050-model-confidence-ood-and-authority.md) §2.2 / §3 /
  [0048](0048-thermal-model-artifact-and-inference.md) §2.4 /
  [0058](0058-rl-supervisor-training-environment.md) §2.3 /
  [0094](0094-dataset-v2-settled-points.md) /
  `src/coldaisle/control/model/counterfactual.py` / `src/coldaisle/control/model/counterfactual_training.py` /
  `src/coldaisle/control/model/counterfactual_confidence.py` / `src/coldaisle/control/model/confidence.py`
- **対象 Issue**: #84（実装 PR #226）/ #85（実装 PR #230）

## 1. Context

0079 §2.9 の段 2（反実仮想 Thermal Model artifact v2・trainer・読み込み時の検査）を PR #226 で、
段 3（Confidence Profile v2 の生成と同梱・同梱 Profile からの判定器）を PR #230 で実装した。
その実装で、0079 / 0084 / 0087 のどれにも書かれていない挙動と、記録の文面だけでは読み方が分かれうる点があった。
いずれも各 PR の「人間のレビューが必要な点」で示し、2026-10-07 に所有者が推奨案のとおりすべて承認した。
決定記録は追記のみなので、0079 / 0084 / 0087 の本文には手を入れず、ここに残す。
記録だけを読んだ人が、実装と違う挙動を前提にしないためである。

## 2. Decision

### 段 2（PR #226）

### 2.1 段 2 / 段 3 の境界

0079 §2.9 は Profile v2 の「生成と同梱」を段 3 に置く一方、段 2 の L6 / L7 と 0084 の L11 / L12 は Profile の中身を検査する。
これを次のとおり読む。

- **段 2**: Profile v2 の**形**（schema。`ConfidenceProfileV2`）と、読み込み時の検査（L1〜L12）
- **段 3**: train / validation からの Profile v2 の**生成**・同梱 Profile からの判定器・step ごとの support の照合関数
- 段 2 の trainer は Profile を含まない学習結果（`CounterfactualTrainedModel`）を返し、`assemble_counterfactual_artifact`
  で Profile と1つの artifact に封じる。段 2 の試験の Profile は手で作る
- 理由: 段 2 の検査は Profile の形が無ければ書けない。一方で生成は residual の基準（0084 §2.1）と support の照合に
  依り、段 3 の内容そのものである

### 2.2 Profile v2 の欄のまとめ方

0084 §2.2 の表の欄を、step ごと・step の組ごとに次の型へまとめる。

| 型 | まとめる欄（0084 §2.2） |
|---|---|
| `StepActionSupport`（step `k` ごと） | 計画 demand の zone ごとの範囲と、(a) 全 zone の計画 demand の組の cell |
| `StepTransitionSupport`（step の組 `(k, k + 1)` ごと） | step 間の変化量の zone ごとの範囲と、(c) 隣り合う step の cell の組 |
| `AnchorTransitionSupport`（1つ） | anchor → 最初の step の変化量の範囲と、(b) の cell の組 |

- 3つを `ActionSupportV2`（`steps` / `anchor_to_first` / `transitions`）に持つ。L12 の「step ごとの欄の数と番号」は
  `steps` と `transitions` に対して検査する
- このまとめ方を変えるなら、Profile の schema の版を上げる（新しい記録が先）

### 2.3 `action_variation_summary` の数え方

manifest の `action_variation_summary`（0079 では「zone ごとの action 変化の件数」とだけ書いている）は、
**zone ごとに、train の各 example の「`prior_action` → step 0」と「step `k` → `k + 1`」の組の数（`transitions`）と、
そのうち値が変わった組の数（`changed`）** とする。

- 起点は `prior_action`（0087 §2.7 の v2 の anchor action）。v1 の `action`（anchor の tick 自身の値）は数えない
- train だけから数える（validation / test は数えない。0050 §2.1 と同じ向き）

### 2.4 L11 は schema 全体の読み込みより前に、生の JSON で見分ける

manifest / Profile v2 の `anchor_action_rule` が Literal（`hold_effective`）の外の値のとき、schema 全体の読み込み
（Pydantic の型の検査。失敗は L4 として報告する）で落とさず、**生の JSON の段で L11 として拒否する**。

- 順は L1〜L3 → L4 のうち `schema_name` / `schema_version` の照合（生の JSON） → **L11**（生の JSON） →
  schema 全体の読み込み（失敗は L4） → L5〜L10 / L12。したがって L11 は L5〜L10 より先に報告され、
  版の照合（L4 の前半）には後れる。版も規則も外れた artifact は L4 で拒否される
- 理由: Literal 外の値を schema 全体の読み込みの失敗に混ぜると、検査の番号（`check`）から原因が L11 だと読めなくなる。
  版の照合を先にするのは、v2 でない artifact に v2 の欄を探しに行かないため

### 2.5 loader は authority stage を受け取らない

`RegistryCounterfactualThermalModel.from_verified_artifact` は authority stage を引数に取らない。

- authority stage との照合は MPC の側（`for_control`）が attestation で行う。0058 §2.3 の dynamics は production を
  要求しないので、loader に stage を持たせると dynamics の用途で読めなくなる
- L2 のうち JSON の入れ子・token 数の上限は、Registry が `VerifiedArtifact` を発行する前の検査（`_validate_format`）に
  依る。loader は `VerifiedArtifact` だけを受け取るので、未検査の bytes が L3 以降へ届かない

### 2.6 `MAX_ACTION_STEPS` は新しい数値ではない

action 列の step 数の構造上の上限 `MAX_ACTION_STEPS` は、**`MAX_FEATURE_COLUMNS // 3`（zone の数）から導く**。
新しい数値は足さない。

- 理由: feature schema `thermal-features-v2` は step ごとに zone の数だけ `plan[k].<zone>.effective_demand` の列を持つので、
  feature の列の上限 `MAX_FEATURE_COLUMNS` を超える step 数の artifact はそもそも作れない

### 2.7 較正の digest の計算方法は決めない（決定記録 0096 に委ねる）

段 2 の trainer と loader は、較正の digest を呼び出し側から値（`str | None`）で受け取り、**完全一致だけを見る**。
`config/calibration.json` から「使った metric に関わる較正値の digest」をどう作るかは本記録では決めず、
決定記録 0096（検討中）に委ねる。段 4（#86。`coldaisle-fand` の起動時読み込み）の前に決める。

### 段 3（PR #230）

### 2.8 support の OOD の detail は最初に外れた1点だけ

held の列が step ごとの support の外のとき（0084 §2.1）、`support` の OOD の detail には、v1 の材料（cell と件数）の後ろに
`held_out_of_step_support: <kind>; step=..; zone=..; value=..`（cell の照合なら `cell=[..]`）を付ける。

- 照合（`StepSupportChecker`）は**最初に外れた1点だけ**を返す。順は 0084 §2.1 / §2.2 の列挙順:
  step ごとの範囲と (a)（step 0 から順に）→ anchor → step 0 の変化量と (b) → step の組ごとの変化量と (c)
- held の列・residual の基準からの除外・段 4 の候補 plan の照合は、同じ照合を使う
- 理由: detail は長さを切り詰めて載せる（`_bounded_detail`）。全部の外れを並べても、Fallback に退避する判断は変わらない

### 2.9 `residual_validation_example_count` は除外後の件数

Profile v2 の `residual_validation_example_count` は、**held の列が step ごとの support の外の example を除いた後に、
residual の基準へ使った validation example の数**とする。除いた数は `residual_excluded_example_count` に持つ。
出力ごとの label の件数は従来どおり `ResidualScale.validation_samples` に持つ。

### 2.10 step ごとの集合の合計の上限（0084 §5 #2）は、いまは数値を足さない

0084 §5 #2（step ごとの集合の合計に、0050 §3 とは別の構造上の上限が要るか）は、**新しい数値を足さず、
実機の dataset で Profile の大きさを測ってから判断する**。

- PR #230 の合成 dataset で作った Profile は 3 KiB 程度だった。cell の1件は約 33 bytes で、合計がおよそ 25 万件に
  なると artifact の上限（8 MiB。0048 §2.4）に当たる
- 現状でも、上限を超える artifact は**登録で拒否される**（黙って切られない）。cell 数の上限（0050 §3）も集合ごとに当て、
  超えれば Profile を作らない（0084 §2.2）
- 数値を足すなら新しい記録を先に作る（0084 §5 #2 のとおり）

### 2.11 v2 の fan range と v1 の support cell の anchor は `prior_action`

Profile v2 の `fan_ranges` と、v1 と共通の support cell（`support_cells`）の `fan.<zone>` 軸に入れる anchor action は、
どちらも **`prior_action`**（0087 §2.7 の v2 の anchor action）とする。

- 理由: 0087 §2.7 は v2 の anchor action を `prior_action` とし、`hold_effective` の列と (b) もそこを起点にする。
  ここだけ anchor の tick 自身の値を使うと、同じ example の判定で起点が食い違う

## 3. Consequences

### 良くなること

- 記録だけを読んでも、段 2 / 段 3 の境界・Profile v2 の型・manifest の件数・検査の順・loader の責務・
  support の OOD の detail・residual の基準の件数を、実装と同じに読める
- 合計の上限を推測で決めずに、実機の Profile の大きさから決められる
- v2 の判定の起点が `prior_action` に揃い、範囲・cell・held の列が同じ action から数えられる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| support の OOD の detail から、2点目以降の外れが読めない | 判断（Fallback への退避）は1点目で決まる。原因を調べるときは Profile と held の列から照合し直せる |
| step ごとの集合の合計に専用の上限が無い | artifact の 8 MiB 上限で登録時に拒否され、黙って切られない。実機 dataset で測ってから決める |
| 較正の digest の計算方法が決まっておらず、段 4 で使えない | 決定記録 0096 で段 4 の前に決める。それまでは値の完全一致だけを見る |
| loader が L2 の一部を Registry の検査に依る | loader は `VerifiedArtifact` しか受け取らず、公開 constructor も無い |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| A. Profile v2 の形も段 3 で入れる | 段 2 の L6 / L7 / L11 / L12 が書けず、段 2 の artifact が検査されないまま残る |
| B. Literal 外の `anchor_action_rule` を schema の読み込み（L4〜）の失敗として拒否する | 検査の番号から原因が L11 だと読めなくなる |
| C. loader に authority stage を渡して照合する | 0058 §2.3 の dynamics（production を要求しない）で読めなくなる。照合は MPC の側の attestation で足りる |
| D. 照合で外れた点をすべて detail に並べる | detail の上限を圧迫し、退避の判断は変わらない |
| E. 合計の上限を合成 dataset の大きさから今決める | 合成 dataset の Profile（3 KiB 程度）は実機の大きさを表さない |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | drift 検知（#93）の `coverage()` に、held の列の step ごとの support を含めるか（`ProfileCoverage` の形が変わる） | drift 検知の v2 対応の PR（形を変えるなら先に記録） |
| 2 | 較正の digest の計算方法（§2.7） | 決定記録 0096（検討中）。段 4（#86）の前 |
| 3 | step ごとの集合の合計に構造上の上限を足すか（§2.10。0084 §5 #2） | 実機 dataset で Profile の大きさを測った後。数値を足すなら新しい記録 |
