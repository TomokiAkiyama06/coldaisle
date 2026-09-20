# 決定記録 0053: Control Shadow Mode の記録内容と counterfactual の扱い

- **種別**: Decision Record
- **Status**: Proposed
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
| MPC の requested demand | `ControllerProposal`（Gate が選ばなかったもの） | — |
| Supervisor の strategy / weights / target band | **その tick の `SupervisorDecision` に実在する** `SupervisorOutput` | 別 tick の戦略 |
| 予測した future | `MpcSolution.prediction`（`plan_digest` と `inference_id` 付き） | 記録時に引き直した予測 |
| confidence / ood | `ModelGateDecision`（`attested` のときだけ） | **提案の自称値** |
| optimizer timeout / error | `optimizer_status`（解も予測も持たせない） | Baseline の値を「解」として |
| worker の失敗（model 読込・例外） | `LearnedFailure` と worker の理由 | 省略 |

裏づけの無い（`attested=False`）記録には confidence も ood も**書かない**（0050 §2.5 と同じ規則）。

### 2.3 実測との突き合わせは、**記録された時刻からだけ**決める

- 期待時刻は `expected_ts_ms = 推論の action 時刻 + offset` で、**記録した時点で確定する**
- 突き合わせの規則は Dataset の target 選択（0031 §2.2）と residual drift（0050 §2.2）に揃える
  - `±shadow.outcome_match_tolerance_ms` の中の**最も近い**観測。**同距離なら過去側**
  - 観測は action より後に限る。**品質が `OK` の値だけ**を証拠にする
- **処理した時刻（壁時計・現在時刻）を一切使わない。** 照合器は時計を持たない
- 許容幅は `mpc.optimizer.step_ms` より小さくなければならない（設定で検証する）。
  1 step に届くと、別の候補 action の効果を「この予測が当たった証拠」に数える
- 照合できなかった出力は**理由付きの未照合**として残す。値を埋め合わせない

### 2.4 export は JSON Lines（0030 §5 の未決を閉じる）

- 1行 = counterfactual を持つ1 tick。`schema_version` を持ち、適用値・counterfactual・
  予測と実測の突き合わせを、同じ `tick_id` / `ts_ms` と `inference_id` で結んで置く
- 入力は保存済み trace と観測で、**読み取りのみ**。同じ入力からは同じ bytes を出す
- 突き合わせ結果は trace へ**書き戻さない**（追記専用の記録を後から書き換えない）
- #91 Offline Evaluation はこの行を読む。Shadow 側に別の集計経路を作らない

### 2.5 設定（`config/fan-policy.yaml`。schema v8）

```yaml
shadow:
  enabled: true
  outcome_match_tolerance_ms: { value: <ms>, status: provisional }
```

- **実測前の暫定値**として扱う。コードに既定値を置かない
- 記録の構造上の上限（1 tick の counterfactual 4件、1予測の step 16、1 step の metric 8）は
  trace の大きさを抑えるための**構造上の上限**であり、調整値ではないのでコード側に置く

## 3. Consequences

- 1 tick の「何を適用し、何を適用しなかったか」を**1つの不変な record**で読める
- 予測の当たり外れを、あとから決定論的に計算できる。処理の順序や実行時刻に依らない
- #91 は trace と観測だけで比較できる。Shadow 専用の収集経路を作らずに済む
- trace は大きくなる。`shadow.enabled`・構造上の上限・保持期間（0030 の `control_trace_days`）で抑える
- `ControlTick` の版が v6 になる。保存済みの v1〜v5 はそのまま読める（shadow を持てない）

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| counterfactual を別テーブル / 別ファイルへ書く | 適用値とは時刻でしか結べず、後から join を間違える。0030 の「1 tick = 1 不変 record」も崩れる |
| `ControllerProposal` をそのまま shadow に保存する | 提案が自称した confidence / ood が混ざり、裏づけの有無を区別できない |
| 突き合わせ結果を trace へ追記する | 追記専用の判断記録を後から書き換えることになり、事故調査に使えなくなる |
| 照合の時刻に処理時刻（現在時刻）を使う | 遅れて流し込んだ古い観測が「新しい証拠」になる。0050 §2.2 で塞いだ穴と同じ |
| shadow 専用に confidence を計算し直す | #85 と二重になり、どちらが本物か分からなくなる。Gate の判定だけを写す |
| 記録した予測を、あとで plan から引き直す | 探索に使った予測と別のものを記録しうる。採用した解と対で持つ |

## 5. 未決事項

- `outcome_match_tolerance_ms` の実運用値と、Shadow trace の保持期間は**実測後に確定する**
  （いまは provisional。確定には基準となる測定が要る）
- RL Supervisor の counterfactual は `SupervisorDecision.shadow` に入る。Demand を伴う RL の
  提案を記録する必要が出たら（#89）、その時点で別の記録を作る
- 昇格 / 降格の gate 条件（#92）と評価指標のしきい値（#91）はここで決めない
- **この記録は所有者の承認を必要とする。** 安全系・制御系の設計変更に当たるため、
  承認されるまで `Proposed` のままにする（AGENTS.md「実装の担当」）
