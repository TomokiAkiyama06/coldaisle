# 決定記録 0058: RL Supervisor 学習環境の責務・dynamics の出どころ・安全の扱い

- **種別**: Decision Record
- **Status**: Proposed（**リポジトリ所有者の承認が要る。** 安全系・制御系の設計変更は
  実装担当モデルに関係なく人間レビューを必須とする。AGENTS.md「実装の担当」）
- **Date**: 2026-09-20
- **Supersedes**: なし
- **関連**: [0027](0027-fan-control-architecture.md) / [0028](0028-fan-control-contracts.md) §2.3〜§2.6 /
  [0031](0031-thermal-dataset-contract.md) §2.2 / [0048](0048-thermal-model-artifact-and-inference.md) §2.1 /
  [0050](0050-model-confidence-ood-and-authority.md) / [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.1〜§2.5 /
  [0053](0053-control-shadow-mode-and-counterfactual-logging.md) §2.3 / [0054](0054-offline-evaluation-attribution-and-gates.md) §2.2〜§2.7 /
  GitHub #89 / #105
- **対象 Issue**: #105（依存 #75 / #77 / #81 / #83 / #84 / #85 / #86 / #88 / #90 / #91 / #94 / #104。利用者 #89）

## 1. Context

#105 は、RL Supervisor（#89）を実サーバー上の無制限 exploration なしで学習・評価するための
Training Environment を作る。#89 の action は Fan PWM / Demand ではなく **MPC の戦略・目的関数
weight・target band** なので、環境も「Fan を直接探索させる」形にはできない。

ところが実装に入る前に、次の点が未決だった。

1. 環境の action / observation を何にするか。誰が action の範囲を決めるか
2. dynamics（次の観測を作る層）をどこから持ってくるか。**決定記録 0052 §2.1 の binding の規律を
   環境にも適用するのか**
3. 環境の中で Critical Safety（#78）をどう扱うか。模すのか、模さないのか
4. reward の版・採点基準を誰が持つか
5. 記録済み trajectory / learned simulator / hybrid をどう切り分け、どう比較するか

さらに、**いまの世界の状態**を無視できない。0052 §2.1 は MPC の内部モデルに
`counterfactual_action` capability を要求し、0048 §2.1 により現行の #84 artifact は
すべて `observational_replay` である。したがって

> いまは active 制御に使える内部モデルが1つも無い（0052 §3）

という状態がそのまま続いている。**この状態で「学習 simulator の pipeline が完成した」ように
見える成果物を作ると、いちばん危ない読み違えを招く。** 環境はこの事実を隠さずに表す必要がある。

## 2. Decision

### 2.1 action は Supervisor の戦略まで。範囲は `supervisor.output_bounds` から取る

環境の action は `SupervisorAction`（`strategy` / `weights` / `target_band`）だけで、
**`Demand` も `EffectiveZoneDemand` も PWM も表現できない**（`extra="forbid"`）。
observation は #88 の `SupervisorInput` をそのまま使う。

許容範囲は環境が作らず、`config/fan-policy.yaml` の `supervisor.output_bounds`（#103 / #88）を
そのまま使う。環境が独自の範囲を持つと、学習で最適だった action が運転時には Supervisor に
拒否される、という食い違いが生まれる。

**範囲外の action は丸めない。** `InvalidSupervisorActionError` として episode を
`INVALID_ACTION` で終端する。丸めると agent は範囲外を出しても罰を受けず、運転時に通らない
action を学習し続ける。

action から demand への写像は環境が持たない。`LearnedMpcController`（#86）と
`ControllerGate`（#79 / #85）をそのまま通し、**環境が demand を作る API は存在しない**。
その結果、低 Confidence / OOD の tick は環境の中でも Fallback になり、step の記録に残る。

### 2.2 dynamics の出どころを3つに分け、step ごとに残す

| provenance | 出どころ | 昇格の根拠にできるか |
|---|---|---|
| `logged_trajectory` | 記録済みの運転（実測） | できる。ただし**記録と同じ action の区間だけ** |
| `registry_attested` | Registry が反実仮想能力を申告した artifact | できる。**いまは該当 artifact が存在しない** |
| `simulated_provisional` | 設定した近似式 | **できない。** 実測に裏づけが無い |

`TrainingMode` は `logged` / `learned_simulator` / `hybrid` の3つで、**結果の型でも混ぜない**。
hybrid は step ごとに provenance を残し、**近似の step が1つでも混ざった episode は
`promotable=False`** にする。混ぜたまま平均すると、実測の裏づけが近似の外挿で薄まる。

`logged` では、要求 demand が記録と `shadow.applied_demand_tolerance`（#90 の設定。
評価用に別の幅を持たない。0054 §2.6）の範囲で一致しない step を `supported=False` として数え、
**観測も reward も作らない**。別の action が掛かっていた区間の実測をその提案の成果にしない、
という 0053 §2.3 / 0054 §2.2 の帰属規則をそのまま使う。

### 2.3 近似 simulator は Registry の証拠を名乗れない（0052 の規律を環境にも置く）

`DynamicsIdentity` の不変条件で、

- `registry_attested` を名乗るには **Registry が発行した artifact hash** が要る
- `simulated_provisional` は **artifact hash を持てない**（設定 bytes の hash だけ）
- `logged_trajectory` は **記録の digest** を持ち、artifact hash を持てない

`AttestedThermalDynamics.bind` は、attestation の `kind` が `thermal_model`、`capability` が
`counterfactual_action` であること、モデルの自称 identity と schema version が attestation と
一致することを要求する。**現行の artifact はすべて `observational_replay` なので、この経路は
決定論的にすべて拒む。** 0052 §2.1 と同じで、拒否は不具合ではなく意図した振る舞いである。

`production_active` は**要求しない**。0052 §2.1 が「Replay / offline 評価は production でない
attestation をそのまま使う」としているためで、ここは制御経路ではない。代わりに、
この束は `MpcModelBinding` へ変換できず、制御へ配線する API を持たない。

近似 simulator（`SimulatedThermalDynamics`）は研究用途に限り回せるが、そこから出た episode は
`promotable=False` になり、#91 の gate や #92 の昇格判断の根拠にできない。

### 2.4 reward の採点基準は設定が持つ。安全は reward の項にしない

- `reward.target_band` は `rl-training.yaml` が持ち、**agent が出した action の band では
  採点しない**。action の band で採点すると、agent は帯を広げるだけで reward を上げられる
- 温度は band の**外へ出た分だけ**を二乗で数える（帯の中は等価。0052 §2.3 と同じ理由）
- `reward.weights` に **安全の項を置けない**（`extra="forbid"`）。安全を重み付きの項にすると、
  ほかの項の改善で相殺できてしまう（0052 §4 (c) / 0054 §2.4 と同じ理由）
- 安全は `EpisodeSafety`（絶対上限の超過数・floor を下回った要求数・範囲外 action 数・
  最小 margin）として別に数え、**比較は辞書式**で安全が先に立つ
- reward 版は episode の結果と条件 hash に必ず入る

### 2.5 環境は Critical Safety を模さない。違反相当は terminal にする

`control/rl` は `control.hardware` / `control.safety` / `control.reactive` / `serial` /
`subprocess` を import しない（0054 §2.7 と同じ規則。試験で走査する）。したがって
**Reactive Guard も Critical Safety も環境には無い**。`EpisodeResult.safety_model` は
`configured_minimum_only` を名乗り、この事実を結果に残す。

環境が持つのは、`config/safety.yaml` の値を読む決定論的な screen だけである
（**評価設定に写さない**。0054 §2.6）。

| 条件 | 扱い |
|---|---|
| 要求 demand が `safety.zone_min_demand` を下回った | `floor_shortfalls` を数え、`SAFETY_VIOLATION` で終端 |
| 観測が `safety.absolute_temp_ceiling_c` を超えた | `ceiling_exceedances` を数え、`SAFETY_VIOLATION` で終端 |
| action が `supervisor.output_bounds` の外 | `invalid_actions` を数え、`INVALID_ACTION` で終端 |

**違反したら探索を止める。** 運転時は #78 が引き上げるが、環境がそれを模すと
「Safety が直してくれる」前提の policy を学習させてしまう。
`EpisodeResult` は、違反を持つ結果を `HORIZON` で終わったことにできないよう型で縛る。

`floor` を下回る要求は、正しく設定された経路では起きない（#86 の `HardConstraintSet` が
floor を下回らせない）。起きたら設定か Baseline が壊れているので、**そのまま続けない。**

### 2.6 再現性と比較の鍵

- seed・workload trace・初期 window・初期 demand・dynamics の identity・reward 版・
  設定 bytes の hash・authority stage・期待 model 版・安全 screen・coverage 下限・
  action 空間を `conditions_sha256` が覆う（0054 §2.7 と同じ考え方）
- **policy は条件に入れない。** Rule と RL を同じ条件で比べる鍵にするためである
- episode ごとに条件は違う（識別子も seed も違う）ので、arm の比較では
  **(episode_id, 条件 hash) の並び**を突き合わせ、違えば受け取らない
- 同じ条件・同じ seed からは同じ bytes が出る。**壁時計を持たない**（時刻は window の時刻と
  設定した刻みから来る）
- coverage（採点できた step 数・割合・できなかった理由の内訳）は一級の出力で、
  下限に満たない episode は `usable_for_comparison=False` として**比較に使わない**（fail closed）

### 2.7 設定は `config/rl-training.yaml`（schema v1。値はすべて暫定）

```yaml
schema_version: 1
episode:   { step_ms, max_steps, recent_history_steps }
reward:    { version, metrics, scales, weights, target_band, discount }
coverage:  { minimum_supported_fraction, minimum_steps }
safety_screen: { temperature_metrics }   # 閾値は safety.yaml から取る
simulator: { model_id, model_version, responses: [...] }
```

**コードに既定値を置かない**（AGENTS.md ルール9）。metric 名も設定へ出す。
構造上限として 1 episode 4,096 step、simulator の metric 32 を置く。これは未信頼な設定による
資源枯渇を防ぐ境界で、調整値ではない（0052 §2.6 と同じ扱い）。

## 3. Consequences

**いま何ができて、何ができないか**（これを曖昧にしない）。

| やりたいこと | いまできるか |
|---|---|
| 記録済み trajectory からの offline RL | **できる。** ただし記録と同じ action の区間だけ採点でき、残りは coverage に出る |
| Registry 検証済みの learned simulator での policy 評価 | **できない。** 反実仮想能力を申告した artifact が1つも無い |
| 近似 simulator での探索 | **研究用途に限り回せる。** 結果は `promotable=false` で、昇格の根拠にできない |
| Rule / RL を同じ episode 群で比較 | できる。ただし上の裏づけの区別はそのまま結果に残る |
| #91 Offline Evaluation への出力 | episode 結果を arm として渡せる形にしたが、**gate への接続は #91 / #92 側で決める**（§5） |

良くなること。

- agent が Fan Demand を探索する経路が**構造的に塞がる**。action から demand への写像は
  MPC と Gate にしか無い
- 近似 simulator が「検証済みの学習 simulator」に見える道が、型の不変条件で塞がる
- 記録に無い action の結果が生えない。採点できない区間は理由付きで coverage に出る
- 安全側の違反を reward で相殺できない。比較は辞書式で安全が先に立つ
- 低 Confidence / OOD の tick は環境の中でも Fallback になり、step の記録から区別できる

悪くなること（と緩和策）。

- **近似 simulator の上での学習結果は、そのままでは何の根拠にもならない。** 緩和策は
  `promotable=false` を結果の型に持たせ、#91 / #92 が根拠として受け取れないようにすること
- 記録済み trajectory では coverage が低くなりがちで（Shadow 中は特に）、比較できる episode が
  少ない。緩和策は、coverage を一級の出力にして「少ない採点区間で結論を出す」ことを止めること
- 環境に Guard / Safety が無いので、環境で良かった policy が運転で通る保証は無い。
  緩和策は `safety_model` を結果に残し、**最終裁定は運転時の #80 / #78 にあること**を
  読む側へ明示すること

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| agent に Fan Demand を直接探索させ、あとで Guard / Safety を掛ける | #89 の action 定義（0027 / 0028 §2.3）と違う。MPC を通さない demand を学習させることになる |
| 近似 simulator に `registry_attested` を名乗らせ、pipeline を「完成」に見せる | 0048 §2.1 / 0052 §2.1 が禁じている読み替えそのもの。裏づけの無い予測から出た policy が昇格の根拠に混ざる |
| 反実仮想 artifact が無いので、環境そのものを作らない | 記録済み trajectory からの offline RL は**いまでもできる**。環境の interface（#89 の利用者）も先に固められる |
| 環境の中に Critical Safety の複製を置く | 「Critical Safety は ML から独立」（0027）に反する。複製は本物とずれ、ずれた側で学習することになる |
| 安全違反を大きな負の reward として表す | 重みで表した制約は weight の設定次第で破れる。制約は制約のまま扱い、辞書式にする（0052 §4 (c)） |
| 範囲外の action を範囲内へ丸める | 丸めると罰が無くなり、運転時に拒否される action を学び続ける |
| 記録に無い action の step を「無かったこと」にして飛ばす | 飛ばすと coverage が見えなくなり、少ない採点区間の平均を全体の成績として読めてしまう（0054 §2.3） |
| reward の target band を action から取る | agent が帯を広げるだけで reward を上げられる。採点の基準は設定が所有する |
| policy を `conditions_sha256` に入れる | 同じ条件で policy を比べられなくなる。条件と policy は別の軸である |
| arm の比較を「どれか1つの条件 hash」で判定する | episode ごとに条件は違う。並びまで突き合わせないと、別の episode 群を同じ表に並べられる |
| 環境が壁時計を読む | 同じ入力から同じ bytes が出なくなり、再現性の判定に使えない（0054 §2.7） |

## 5. 未決事項

- **本記録は `Proposed` である。所有者の承認が要る**（AGENTS.md「安全系・制御系の設計変更は
  人間レビュー必須」）。承認されるまで §2 は確定していない
- `config/rl-training.yaml` の実運用値（刻み・重み・基準量・coverage 下限・simulator の応答）は
  **すべて実測前の暫定値**。確定には基準となる測定が要る
- 近似 simulator の応答式（1次遅れ + 流量近似）を、実測でどこまで検証するか。
  検証できるまで `promotable=false` のままにする。#83 / #84 の反実仮想 Dataset / Model が
  揃えば `registry_attested` へ置き換える
- episode 結果を #91 の `EvaluationReport` の arm としてどう載せるか（0054 §5 は
  「RL Supervisor を含む比較（#89 / #105）は、同じ arm の枠で足せるが、本記録では扱わない」と
  している）。**接続は #91 / #92 側の記録で決める**
- policy artifact（#104）と episode 結果の対応づけ。いまは `policy` / `policy_version` を
  結果に残すだけで、Registry への登録経路は #89 が持つ
- 学習アルゴリズムそのもの（offline RL の手法・データ量・評価指標）は #89 の範囲。
  本記録は環境の契約だけを決める
- 環境に Reactive Guard 相当の近似を入れるか。いまは入れない（§4）。入れる場合は
  「近似であること」を provenance と同じ強さで結果に残す必要がある
