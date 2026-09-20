# 決定記録 0053: Control Shadow Mode の記録内容と counterfactual の扱い

- **種別**: Decision Record
- **Status**: FINAL（2026-09-20、リポジトリ所有者が承認）
- **Date**: 2026-09-20
- **Supersedes**: なし
- **関連**: [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md)、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.3 / §2.5、
  [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md) §5、
  [`0031-thermal-dataset-contract.md`](0031-thermal-dataset-contract.md) §2.2、
  [`0048-thermal-model-artifact-and-inference.md`](0048-thermal-model-artifact-and-inference.md)、
  [`0050-model-confidence-ood-and-authority.md`](0050-model-confidence-ood-and-authority.md)、
  [`0052-learned-mpc-optimizer-and-hard-constraints.md`](0052-learned-mpc-optimizer-and-hard-constraints.md)、
  GitHub #90 / #91 / #92
- **対象 Issue**: #90

## 1. Context

authority stage が `SHADOW` の間、Learned MPC と RL Supervisor の提案は Fan へ届かない。
届かない提案は**記録しなければ検証できない**ので、昇格（#92）も Offline Evaluation（#91）も
根拠を持てない。一方で、記録の仕方を誤ると「届かないはずの提案」が別の形で効いてしまう。

未決だったのは3点である。

1. counterfactual をどこへ、どの形で書くか（0028 は decision trace の存在だけを定めている）
2. 予測した future と、あとから届く実測をどう突き合わせるか
3. SQLite 外への export 形式（0030 §5 が「#90 / #91 で決める」として送った論点）

加えて、#85 / #86 のレビューで繰り返し見つかった失敗の型がある。**同じ穴をここでも開けない。**

- 提案が**自称した** confidence / ood を、検証済みの値として記録する
- **別の推論**の判定や予測を貼り替えて、authority や「当たっていた」記録を作る
- 証拠の時刻に**処理した時刻**を使い、遅れて届いた観測を新しい証拠に見せる

## 2. Decision

### 2.1 counterfactual は decision trace の中の**別枠**に置く（`ControlTick` schema v6）

- `ControlTick` に `shadow`（`ShadowRecord`）を足す。1 tick = 1 record のまま、
  **適用した値と適用しなかった提案を別のフィールドに分けて**持つ（0030 の形を崩さない）
- `ShadowRecord` は `applied_controller` / `applied_effective` を持ち、
  **counterfactual の制御器が適用した制御器と一致してはならない**。
  `applied_effective` は同じ tick の `EffectiveZoneDemand.effective` と一致しなければならない
- shadow の型は `requested`（`0.0..1.0` の demand）までしか表現できない。
  `EffectiveZoneDemand` も PWM も持たず、`control/shadow` は Reactive Guard・Critical Safety・
  Hardware Backend を import しない（AGENTS.md ルール1 / 2。試験で走査する）
- `MANUAL` / `CALIBRATION` には残さない（人が requested を決める mode には比較対象が無い）。
  `MAX` は AUTO の上に重ねる override なので、下で動いていた制御器とともに残す

### 2.2 記録する値と、その出どころを固定する

| 記録 | 出どころ | 決して使わないもの |
|---|---|---|
| 適用した controller / effective demand | Critical Safety の合成結果 | 制御器の requested |
| MPC の requested demand | `ControllerProposal`（**Gate が評価した候補そのもの**） | 同じ推論から作った別の候補 |
| Supervisor の strategy / weights / target band | **その tick の `SupervisorDecision` に実在する** `SupervisorOutput` | 別 tick の戦略 |
| 評価した候補 plan | `MpcSolution.plan`（step ごとの demand をそのまま） | 要求値からの復元 |
| 予測した future | `MpcSolution.prediction`（`plan_digest` と `inference_id` 付き） | 記録時に引き直した予測 |
| confidence / ood | `ModelGateDecision`（`attested` のときだけ） | **提案の自称値** |
| optimizer timeout / error | `optimizer_status`（解も予測も持たせない） | Baseline の値を「解」として |
| worker の失敗（model 読込・例外） | `LearnedFailure` と worker の理由 | 省略 |

裏づけの無い（`attested=False`）記録には confidence も ood も**書かない**（0050 §2.5 と同じ規則）。

**記録する結果は「Gate が評価した worker 結果そのもの」に束ねる。** 識別子は段階的に足りない。

- 推論の識別子（`inference_id`）は入力と予測を指すだけで、そこから作れる候補 demand は1つではない
- 提案（`ControllerProposal`）だけの識別子では、**解**を覆えない。同じ提案・同じ assessment の
  まま、別の解（同じ plan に対する別の妥当な予測）を抱えた結果をいくつでも作れる

そこで識別子は **worker 結果 1回分の全体**（提案・assessment・解・失敗の理由）の canonical JSON の
SHA-256 とする（`MpcProposal.result_digest()`）。worker がこれを付け、Gate は中身を解釈せず
選択結果へそのまま残し、記録側は自分が持っている結果を数え直して照らす。一致しなければ
**記録しない**（fail closed）。

**識別子は、記録が書く値をすべて覆う。** counterfactual が worker 結果から写すのは
要求 demand・理由・optimizer の結果・latency・評価回数・model 版・推論 id・artifact hash・
候補 plan・予測・コスト・失敗の理由で、いずれも上記の canonical JSON に含まれる（どれを変えても
識別子が変わることを試験で1つずつ確かめる）。**例外は confidence / ood だけ**で、これは worker の
自称値ではなく Gate の判断（`model_gate`）から取り、trace 側で `model_gate` と突き合わせる。

この照合は記録を作る時点で行う。保存後の trace から数え直せる形ではないため、同一プロセス内の
偽造までは防げない（0050 §3 と同じ境界）。

**予測は「どの候補 action に対するものか」まで束ねる。** 版・推論・artifact が合っていても、
それだけでは plan A の要求に plan B の予測を貼れてしまう（同じ tick の候補は step の刻みが同じ）。
記録には採用した候補 plan そのものを残し、読む側が `plan_digest` を数え直して照合する。
要求した demand が plan の最初の step と違う、step 列が予測と違う、digest が合わない記録は
**作れない**（決定記録 0052 §2.2 の識別子をそのまま使い、fail closed にする）。

### 2.3 実測との突き合わせは、**記録された時刻からだけ**決める

- 期待時刻は `expected_ts_ms = 推論の action 時刻 + offset` で、**記録した時点で確定する**
- 突き合わせの規則は Dataset の target 選択（0031 §2.2）と residual drift（0050 §2.2）に揃える
  - `±shadow.outcome_match_tolerance_ms` の中の**最も近い**観測。**同距離なら過去側**
  - 観測は action より後に限る。**品質が `OK` の値だけ**を証拠にする
- **処理した時刻（壁時計・現在時刻）を一切使わない。** 照合器は時計を持たない
- 許容幅は `mpc.optimizer.step_ms` より小さくなければならない（設定で検証する）。
  1 step に届くと、別の候補 action の効果を「この予測が当たった証拠」に数える
- 照合できなかった出力は**理由付きの未照合**として残す。値を埋め合わせない

**採点してよいのは、予測した候補 action が実際に掛かっていた区間だけ。**
counterfactual の予測は「その plan を実行したら」の予測である。実際には Fallback（や Guard /
Safety が決めた別の値）が掛かっていた区間の実測と引き算すると、出てくるのは
**制御器の違いとモデル誤差が混ざった量**で、モデル誤差ではない。判定は記録だけで行う。

- plan の step `i` が覆う区間は `[action + offset[i-1], action + offset[i])`（`offset[0]` の手前は
  action 時刻）。その区間に**記録がある tick** の effective demand を見る
- zone ごとに `|適用値 - plan の値| <= shadow.applied_demand_tolerance` なら、その step は
  「plan どおり実行された」とみなす
- **demand は次の tick まで掛かり続ける。** 区間の先頭が tick と揃っていなければ、最初の記録
  までは直前の tick の値が掛かっている。この**持ち越し分も同じ規則で照らす**（区間の中の記録
  だけを見ると、前の step の demand が効いていた前半を取りこぼす）
- 区間に tick の記録が1つも無ければ `applied_action_unknown`（制御ループが回っていた証拠が
  無い区間を「plan どおり」と決めつけない）、1つでも違えば `applied_action_differs` として
  **`unidentifiable`** にする
- `unidentifiable` でも**実測は残す**。誤差だけを出さない

適用値は既存の記録から取る。`ControlTick` は zone ごとの effective demand を必ず持つので
（0028 §2.3 / #82）、counterfactual を持たない tick も「その時刻に何が掛かっていたか」の証拠に
なる。**この判定のために新しい記録項目は要らない。**

**反実仮想の補正（因果推定）はこの記録の範囲外とする。** 別の action が掛かっていた区間の
実測から「その plan だったらどうだったか」を推定する道具はここでは作らない。
`unidentifiable` を「採点できない」として残し、評価（#91）はその区分を見て扱いを決める。

### 2.4 export は JSON Lines（0030 §5 の未決を閉じる）

- 1行 = counterfactual を持つ1 tick。`schema_version` を持ち、適用値・counterfactual・
  予測と実測の突き合わせを、同じ `tick_id` / `ts_ms` と `inference_id` で結んで置く
- 突き合わせは3つの状態を**区別して**書く。`status: scored`（採点した）、
  `status: unidentifiable` + 理由（実測はあるが誤差を出さない）、出力ごとの `unmatched` + 理由
  （実測が無い）。採点していない結果に `error` は入らない
- **値の無い欄は書かない**（`exclude_none`）。`"error": null` を書くと、0 と区別できない形で
  「誤差の欄がある」ように見える。欄があること自体が意味になるようにし、省略した欄は既定値
  （`null`）として読み戻せる。保存する decision trace の形（0030）は変えない。**export だけの規則**である
- 採点の可否は**渡された trace の適用 demand だけ**で決める。horizon を覆う tick が範囲外なら
  `unidentifiable`（範囲を跨いで推定しない）
- 入力は保存済み trace と観測で、**読み取りのみ**。同じ入力からは同じ bytes を出す
- 保存した索引（`ts_ms` / `tick_id` / `schema_version`）と trace 本文が食い違う行は流さない。
  **版も照らす**（`coldaisle.dataset` の trace 検証と同じ）。索引の版だけを見て読み分ける側が、
  中身と違う意味で解釈するため
- 実測の索引は **export 全体で1回**だけ作り、照合は期待時刻の周りだけを二分探索で切り出す。
  行ごとに観測を並べ直すと、走査が行数に比例して伸びる（結果は素朴な全走査と同じ）
- 突き合わせ結果は trace へ**書き戻さない**（追記専用の記録を後から書き換えない）
- #91 Offline Evaluation はこの行を読む。Shadow 側に別の集計経路を作らない

### 2.5 設定（`config/fan-policy.yaml`。schema v8）

```yaml
shadow:
  enabled: true
  outcome_match_tolerance_ms: { value: <ms>, status: provisional }
  applied_demand_tolerance: { value: <demand>, status: provisional }
```

- **実測前の暫定値**として扱う。コードに既定値を置かない
- 時刻の許容幅は `mpc.optimizer.step_ms` 未満を設定で検証する（§2.3）
- `applied_demand_tolerance` は「同じ action とみなす」zone ごとの demand の幅。**1.0 未満**を
  設定で検証する（1.0 はどんな適用値も plan どおりにしてしまい、判定が意味を失う）
- 記録の構造上の上限はコード側に置くが、**写し元の契約と同じ値にする**。
  1 tick の counterfactual は制御器の種類の数、候補 plan と予測の step 数は
  `MAX_MPC_HORIZON_STEPS` / `MAX_TARGET_HORIZONS`、1 step の metric 数は `MAX_TARGET_METRICS`、
  metric 名の形と長さは `ThermalMetricName` に合わせる。**記録側だけが狭いと、設定としては
  妥当な MPC が出した解を記録できず、tick の途中で記録が失敗する。** 下位 schema から上位
  module を import しないため値は写しになり、一致は試験で突き合わせる

## 3. Consequences

- 1 tick の「何を適用し、何を適用しなかったか」を**1つの不変な record**で読める
- 予測の当たり外れを、あとから決定論的に計算できる。処理の順序や実行時刻に依らない
- **Shadow の間は多くの予測が `unidentifiable` になる**（提案が実行されていないため）。
  採点できるのは、適用値がたまたま候補 plan と一致した区間だけである。これは制限ではなく、
  観測から言えることの範囲そのもので、混ぜて集計しないための区分である
- #91 は trace と観測だけで比較できる。Shadow 専用の収集経路を作らずに済む
- trace は大きくなる。`shadow.enabled`・構造上の上限・保持期間（0030 の `control_trace_days`）で抑える
- `ControlTick` の版が v6 になる。保存済みの v1〜v5 はそのまま読める（shadow を持てない）
- 記録の束縛（推論・候補 plan・適用値との対応）が壊れていれば、記録は**例外で閉じる**。
  記録は制御の入力ではないので、制御ループ側はこの失敗で運転を止めない配線にする（#83）。
  逆に「黙って記録しない」を選ぶと、食い違いに気づけないまま評価だけが進む

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| counterfactual を別テーブル / 別ファイルへ書く | 適用値とは時刻でしか結べず、後から join を間違える。0030 の「1 tick = 1 不変 record」も崩れる |
| `ControllerProposal` をそのまま shadow に保存する | 提案が自称した confidence / ood が混ざり、裏づけの有無を区別できない |
| 突き合わせ結果を trace へ追記する | 追記専用の判断記録を後から書き換えることになり、事故調査に使えなくなる |
| 照合の時刻に処理時刻（現在時刻）を使う | 遅れて流し込んだ古い観測が「新しい証拠」になる。0050 §2.2 で塞いだ穴と同じ |
| 適用 action を問わず、予測と実測の差を「予測誤差」として残す | 制御器の違いとモデル誤差が混ざる。Shadow の提案は実行されていないので、その差は当たり外れではない |
| 別の action の実測から反実仮想を推定して採点する | 因果推定はこの記録の範囲外。学習中のモデルの評価に、検証していない推定を重ねない |
| 採点できない予測を export から落とす | 「予測が無かった」と「採点できなかった」を区別できなくなる。実測は残して区分で示す |
| 区間の中の記録だけで適用 action を判定する | demand は次の tick まで掛かり続けるので、区間の前半に効いていた前の step の値を取りこぼす |
| 記録する提案を `inference_id` だけで Gate の判断と結び付ける | 同じ推論から別の候補 demand の提案を作れる。「退けられたのはこれ」と言えない |
| 提案（`ControllerProposal`）だけの識別子で結び付ける | 解を覆えない。同じ提案のまま別の予測を抱えた結果を、その判断の予測として記録できる |
| 採点していない欄に `null` を書く | 0 と区別できない形で誤差の欄が見える。無い値は書かない |
| shadow 専用に confidence を計算し直す | #85 と二重になり、どちらが本物か分からなくなる。Gate の判定だけを写す |
| 記録した予測を、あとで plan から引き直す | 探索に使った予測と別のものを記録しうる。採用した解と対で持つ |
| 候補 plan を残さず、要求と offset から plan を組み直して digest を数える | v1 の「horizon 全体で同じ demand」に依存する。step ごとに違う demand を探索する版が入った瞬間、正しい記録まで閉じる |
| 記録側の上限を小さめに置いて trace を抑える | 設定としては妥当な解を記録できない tick が生まれる。量は `enabled` と保持期間で抑える |

## 5. 未決事項

- `outcome_match_tolerance_ms` / `applied_demand_tolerance` の実運用値と、Shadow trace の
  保持期間は**実測後に確定する**（いまは provisional。確定には基準となる測定が要る）
- 実行されなかった提案の当たり外れをどう評価するかは #91 で決める。採点できる区間が少ない場合の
  扱い（例: 適用値が一致した区間だけを集計する、Shadow 以外の証拠を使う）もそちらの論点とする
- RL Supervisor の counterfactual は `SupervisorDecision.shadow` に入る。Demand を伴う RL の
  提案を記録する必要が出たら（#89）、その時点で別の記録を作る
- 昇格 / 降格の gate 条件（#92）と評価指標のしきい値（#91）はここで決めない
- 所有者の承認（2026-09-20）で本記録は `FINAL` になった。**§2 の決定が変わるときは、
  書き換えずに新しい記録を作る**（`docs/decisions/README.md`「追記のみ」）。
  上の未決事項と provisional な設定値は、確定するまで開いたままである
