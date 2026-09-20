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
  [0056](0056-model-drift-detection-and-retraining-triggers.md) §2.3 /
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

#### Learned MPC を束縛できない runtime を、そのまま表す

`LearnedMpcController` を作るには `MpcModelBinding` が要り、それには反実仮想能力を申告した
artifact が要る。**いまは1つも無い**（§2.3）。環境が controller を必須にすると、
記録済み trajectory を回すためだけに**偽の attestation を用意する**ことになり、
0052 §2.1 の規律をこちら側で崩してしまう。

そこで環境は `mpc=None` と「束縛できなかった理由」を受け取れる。その場合、理由は運転時と
同じ `LearnedFailure.MODEL_LOAD_FAILURE` として `ControllerGate` へ渡り、requested は
Fallback が作る。**新しい失敗経路を足していない。** 運転時に model 読込が失敗したときと
同じ経路をそのまま通す（AGENTS.md ルール4）。

**その episode では Supervisor action が demand に一切効かない。** 結果は
`learned_controller_available=false` を必ず持ち、`promotable` にはできない。
`PolicyComparison` は arm 間でこの値が揃っていることを要求する。片方だけ controller が
居る比較は policy の差を測っていないためで、両方 `false` の比較は「差が無かった」ではなく
**「action が効いていない条件で回した」**と読む。

### 2.2 dynamics の出どころを3つに分け、step ごとに残す

| provenance | 出どころ | 昇格の根拠にできるか |
|---|---|---|
| `logged_trajectory` | 記録済みの運転（実測） | できる。ただし**記録と同じ action の区間だけ** |
| `registry_attested` | Registry が反実仮想能力を申告した artifact | できる。**いまは該当 artifact が存在しない** |
| `simulated_provisional` | 設定した近似式 | **できない。** 実測に裏づけが無い |

`TrainingMode` は `logged` / `learned_simulator` / `hybrid` の3つで、**結果の型でも混ぜない**。
hybrid は step ごとに provenance を残し、**近似の step が1つでも混ざった episode は
`promotable=False`** にする。混ぜたまま平均すると、実測の裏づけが近似の外挿で薄まる。

`logged` では、要求 demand が記録と `shadow.applied_demand_tolerance` の範囲で一致しない
step を `supported=False` として数え、**観測も reward も作らない**。別の action が掛かって
いた区間の実測をその提案の成果にしない、という 0053 §2.3 / 0054 §2.2 の帰属規則をそのまま使う。

**許容幅は呼び出し側から受け取らない。** `LoggedTrajectoryDynamics` は検証済みの
`ShadowConfig`（`config/fan-policy.yaml` の `shadow`）を受け取り、そこから幅を読む。
scalar で受け取れると、記録された coverage と意味の違う幅で照合した結果を同じ表に
並べられてしまう（0054 §2.6 と同じ規則）。

#### 記録の再生が再現するもの／しないもの

**再生は記録を作り直さない。** `LoggedFrame` は観測を `ObservedWindowFrame` として丸ごと持ち、
再生はそれをそのまま window へ置く。

| 記録された性質 | 再生が再現するか |
|---|---|
| 観測値 | **する。** 値を作り直さない |
| 観測時刻（`ts_ms`）と metric ごとの `source_ts_ms` | **する。** 生成時刻で上書きしない |
| `missing` / `stale` / `suspect` の mask | **する。** `OK` に倒さない |
| 掛かっていた demand（`applied`） | **する。** 次の state も reward もこの値を使う |
| 要求と記録の差（許容幅の中） | **しない。** 記録側が掛かっていた事実なので、要求は `requested` として別に残す |
| 記録に無い action の結果 | **しない。** `supported=False` として数え、観測を作らない |
| 記録に品質が無い場合 | **再現できないので受け取らない。** `LoggedFrame` の mask と `source_ts_ms` は必須で、既定値を持たない |
| 識別に使った許容幅 | **結果に残す。** `EpisodeResult.applied_demand_tolerance`（決定記録 0056 §2.3 と同じ理由。広い幅で作った coverage が `supported` を名乗っていないか、読む側が確かめられるようにする） |
| 読めていない cell（mask が立っている） | **採点に使わない。** reward が作れず、その step は `reward_unusable` で終端する |

最後の2行が規則の要点である。**再現できないものは既定値で埋めず、拒否する。**
品質の分からない記録を `Quality.OK` として再生すると、再生1 step 目から
「実測の裏づけがある健全な観測」に見えてしまう。

**観測の出どころは window ひとつだけ。** `DynamicsStep` は値の欄を持たず、環境は window の
最後の frame から**使える cell だけ**を読む。値を別の欄でも返せると、mask と食い違う値を
渡せてしまう。

**掛かっていた action も window から取る。** 記録再生では要求が許容幅の分だけ記録と違いうる
ので、要求をそのまま次の state にすると「掛かっている action」が2つになる。
`StepRecord` は `requested`（MPC / Gate の出力）と `applied`（観測 window の action）を
別の欄として持ち、**reward と次の state は `applied` を使う**（0052 §2.4 と同じ理由）。

#### 学習 mode は dynamics が名乗る

`TrainingMode` は `EpisodeSpec` の欄ではなく `EnvironmentDynamics.mode` から derive する。
欄として持つと、記録再生を `learned_simulator` と書いた結果を作れてしまう。

### 2.3 近似 simulator は Registry の証拠を名乗れない（0052 の規律を環境にも置く）

`DynamicsIdentity` の不変条件で、

- `registry_attested` を名乗るには **Registry が発行した artifact hash** が要る
- `simulated_provisional` は **artifact hash を持てない**（設定 bytes の hash だけ）
- `logged_trajectory` は **記録の digest** を持ち、artifact hash を持てない

`AttestedThermalDynamics.bind` は、attestation の `kind` が `thermal_model`、`capability` が
`counterfactual_action` であること、モデルの自称 identity と schema version が attestation と
一致することを要求する。**現行の artifact はすべて `observational_replay` なので、この経路は
決定論的にすべて拒む。** 0052 §2.1 と同じで、拒否は不具合ではなく意図した振る舞いである。

#### 昇格の根拠にできる条件（完全な規則）

**自称は一切見ない。** `EnvironmentDynamics` が返す `identity`・`provenances`・
`DynamicsStep.provenance` は、どれも実装が並べたただの値である。`registry_attested` も
`logged_trajectory` も、近似 simulator が名乗れる。したがって裏づけは、**この package の
検証経路だけが発行できる封をした object** `DynamicsEvidence` が与える。

`DynamicsEvidence` は公開 constructor を持たず、発行できるのは次の2箇所だけである。

| 発行する場所 | 発行できる provenance | 何を検証したか |
|---|---|---|
| `LoggedTrajectoryDynamics.__init__` | `logged_trajectory` | 検証済みの `LoggedTrajectory` と、検証済み設定（`policy.shadow`）の許容幅 |
| `AttestedThermalDynamics.bind` | `registry_attested` | Registry が発行した `ArtifactAttestation`（kind / capability / identity / schema 版を照合済み） |

`SimulatedThermalDynamics` と `HybridDynamics` は `evidence = None` を返す。
近似を含む以上、記録から来た step があっても昇格の根拠にはしない。

**`EpisodeResult.promotable` が立つのは、次の7つがすべて成り立つときだけ**である。
環境だけがこの欄を立て、1つでも欠ければ `False` になる。

1. coverage の下限を満たした（`usable_for_comparison`）
2. 安全側の違反が0（絶対上限の超過・Critical Safety floor を下回った要求）
3. 設定範囲外の action が0
4. Learned MPC を束縛できた（`learned_controller_available`。§2.1）
5. dynamics が **封をした `DynamicsEvidence`** を持つ
6. その証拠の `identity` が、いま dynamics が名乗っている identity と一致する
   （借りた証拠を別の identity に付けさせない）。`registry_attested` ならさらに
   `ArtifactAttestation` の kind / capability / model ID / 版 / artifact hash が identity と一致する
7. **記録したすべての step が採点でき（`supported`）、その出どころが、証拠が裏づける
   唯一の出どころと等しい**。step が1つも無い episode も根拠にしない。
   採点できなかった終端 step（記録が尽きた・controller が使えなかった）を飛ばして
   「全部裏づけあり」と読まない

#### step が持ちうる出どころは、すべて環境が検証済み入力から作ったものへ辿れる

`StepRecord.provenance` は `DynamicsStep.provenance` から来る。環境はこの値を2回見る。

- **走らせてよいか**: `EnvironmentDynamics.provenances`（実装の宣言）に無い値なら、
  その step を受け取らず `DYNAMICS_UNUSABLE` で終端する。実装の宣言と実際の返り値が
  食い違う配線の誤りを、episode を進める前に止める
- **根拠にしてよいか**: 上の条件 7。封をした証拠の provenance と一致しない step が1つでも
  あれば `promotable` は立たない

この2段があるので、**証拠に辿れない provenance の step は「走ることはできても、根拠には
決してならない」**。近似 simulator が `logged_trajectory` を名乗った step を返す経路
（1巡目・2巡目で指摘された形）は、宣言と一致していれば走るが、`evidence` が `None` なので
`promotable` は立たない。

**同一プロセス内の悪意ある偽造までは防げない**（決定記録 0050 §3）。本物の
`LoggedTrajectoryDynamics` を包んで `evidence` を借りつつ別の値を返す wrapper は作れる。
0052 §2.1 と同じ残余リスクで、狙いは**配線の誤りを型で止めること**である。

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

**screen は `reset` の時点でも掛ける。** 初期 window が既に上限を超えていたり、初期 demand が
floor を下回っていたりする episode を「まだ1 step も進んでいないから安全」として agent へ
見せない。違反している初期 state の episode は、1 step も進まずに `SAFETY_VIOLATION` で終わる。

**掛かっている action は1つしか持てない。** `EpisodeSpec` は初期 demand を欄として持たず、
`initial_window.action`（#84 の「その action が実際に掛かった結果の観測」）から derive する。
別の欄として持つと、window の action と食い違う2つ目の「掛かっている action」を作れてしまい、
片方だけが screen を通る。

**読めていない値を screen の証拠にしない。** 観測 window の `missing` / `stale` / `suspect` の
cell は、安全 screen にも reward にも渡さない。「読めていない」を「上限を下回っている」とも
「超えている」とも読み替えない（0054 §2.6 の `OutcomeObservation.usable` と同じ規則）。
Snapshot へ写すときも品質と source 時刻をそのまま写す。`Quality.OK` へ丸めると、本物の
Fallback Controller が「使えない」と判断する値を環境だけが使い、環境と運転で別の demand が出る。

**違反したら探索を止める。** 運転時は #78 が引き上げるが、環境がそれを模すと
「Safety が直してくれる」前提の policy を学習させてしまう。
`EpisodeResult` は、違反を持つ結果を `HORIZON` で終わったことにできないよう型で縛る。

`floor` を下回る要求は、正しく設定された経路では起きない（#86 の `HardConstraintSet` が
floor を下回らせない）。起きたら設定か Baseline が壊れているので、**そのまま続けない。**

### 2.6 再現性と比較の鍵

- `conditions_sha256` は **結果に効く依存をすべて覆う**（0054 §2.7 と同じ考え方）。
  seed・workload trace・初期 window・reward 版・`rl-training.yaml` の hash・
  **`fan-policy.yaml` と `safety.yaml` を丸ごと hash した値**（mpc.optimizer・authority stage・
  gate 閾値・復帰 hold・shadow の許容幅を欄ごとに数え落とさないため）・期待 model 版・
  Learned MPC の有無・学習 mode・安全 screen・coverage 下限・action 空間を入れる
- **controller は「期待する版」では特定できない。** 束縛した artifact の attestation
  （model ID・版・artifact hash・lifecycle 状態・registry revision）、Confidence Profile の
  hash と判定設定、任意依存（#94 Acoustic / #81 Air Balance）の出どころまでを
  `LearnedMpcController.conditions()` から取って入れる。同じ版を名乗る別の artifact も、
  別の Profile も、別の任意依存も、同じ入力から違う提案を作る
- **`dynamics.identity` ではなく `dynamics.conditions()` を入れる。** 照合の許容幅や、
  hybrid が内側に持つ記録は identity に現れないのに結果を変える
- **注入した依存は呼び出し側が名指しする。** Baseline の factory も Acoustic Model も
  hash できないので、`DependencyIdentity`（名前・版・設定 hash）を**必須**で受け取り、
  条件へ入れる。名指しの無い依存は受け取らない
- **policy は条件に入れない。** Rule と RL を同じ条件で比べる鍵にするためである
- episode ごとに条件は違う（識別子も seed も違う）ので、arm の比較では
  **(episode_id, 条件 hash) の並び**を突き合わせ、違えば受け取らない
- 同じ条件・同じ seed からは同じ bytes が出る。**壁時計を持たない**（時刻は window の時刻と
  設定した刻みから来る）
- coverage（採点できた step 数・割合・できなかった理由の内訳）は一級の出力で、
  下限に満たない episode は `usable_for_comparison=False` として**比較に使わない**（fail closed）
- **arm ごとに落ちた episode を捨てない。** `PolicyComparison` は、比較に使える episode の
  識別子の並びが arm 間で一致することを要求する。捨てると arm ごとに母集団が変わり、
  都合の悪い episode が消えた arm が勝ってしまう
- **長さの違う episode の総和を並べない。** 途中で終わった episode は負の reward を積む
  回数が少ないので、総和で比べると「早く壊れたほうが良い」になる。比較は episode ごとに
  **すべての arm が採点できた step 数（最小）**まで揃えたうえで行う

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
| 記録済み trajectory からの offline RL データ収集 | **できる。** 記録と同じ action の区間だけ採点でき、残りは coverage に出る |
| Learned MPC を通した action の評価 | **できない。** `MpcModelBinding` を作れる artifact が1つも無いので、環境は `learned_controller_available=false` で回る |
| Registry 検証済みの learned simulator での policy 評価 | **できない。** 反実仮想能力を申告した artifact が1つも無い |
| 近似 simulator での探索 | **研究用途に限り回せる。** 結果は `promotable=false` で、昇格の根拠にできない |
| Rule / RL を同じ episode 群で比較 | 仕組みはある。ただし **controller が無い間は両 arm の requested が同一になり、差は出ない**（`learned_controller_available=false` が結果に残る） |
| #91 Offline Evaluation への出力 | episode 結果を arm として渡せる形にしたが、**gate への接続は #91 / #92 側で決める**（§5） |

**いちばん重要な帰結**: 反実仮想 artifact が揃うまで、この環境で意味のある学習・比較ができるのは
「記録済み trajectory のデータ収集」までである。**action が demand に効く形で policy を比べる
ことは、まだできない。** 環境はその事実を `learned_controller_available` として毎回の結果に残し、
`promotable` を立てない。

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
| 記録済み trajectory を回すために、試験用の attestation を production へ昇格させる | 0052 §2.1 の promotion の規律をこちら側で崩す。`mpc=None` の経路を持てば偽の証拠は要らない |
| `identity.provenance` が `registry_attested` なら裏づけありとみなす | provenance も hash もただの値。近似 simulator でも名乗れる。`ArtifactAttestation` object を要求する |
| step ごとの provenance をそのまま信じる | 近似 simulator が「記録から来た」と名乗れる。出しうる出どころを宣言させ、外れた step を拒む |
| 記録再生だけは「`attestation` が無い」ことを裏づけとみなす | 自称の `logged_trajectory` がすべて通る。記録側にも封をした `DynamicsEvidence` を要求する |
| `EnvironmentDynamics.provenances`（実装の宣言）を昇格の根拠に使う | 宣言も自称である。根拠は封をした証拠の provenance との一致で判断する |
| 初期 demand を `EpisodeSpec` の欄として持つ | window の action と食い違う2つ目の「掛かっている action」ができ、片方だけが screen を通る |
| Snapshot へ写すときに品質を `OK` へ丸める | 本物の Fallback が「使えない」とする値を環境だけが使い、環境と運転で別の demand が出る |
| `history[-recent_history_steps:]` をそのまま使う | 0 のとき履歴が全件返る。0 は「渡さない」である |
| 採点できなかった終端 step を出どころの照合から外す | 記録が尽きた区間を飛ばして「全部裏づけあり」と読める |
| 記録と僅かに違う要求を、そのまま次の state の掛かっている action にする | window の action と食い違う2つ目の applied ができる |
| 識別の幅を条件 hash に入れるだけにする | hash は読めない。どの幅で `supported` にしたのかを読む側が確かめられない（0056 §2.3） |
| 再生時に観測の時刻と品質 mask を作り直す | 1 step 目から stale / suspect が `OK` に見える。記録が品質を持たないなら受け取らない |
| `DynamicsStep` に window とは別の値の欄を持たせる | mask と食い違う値を渡せる。観測の出どころは window ひとつにする |
| `TrainingMode` を `EpisodeSpec` の欄にする | 記録再生を `learned_simulator` と書いた結果を作れる |
| controller を `expected_model_version` だけで条件へ入れる | 同じ版の別 artifact・別 Profile・別の任意依存が、policy の差に見える |
| 呼び出し側が渡した設定 hash だけを条件に入れる | 中身と食い違う hash を渡せる。検証済み設定 object からの hash も併せて入れる |
| 記録照合の許容幅を呼び出し側から scalar で受け取る | 記録された coverage と意味の違う幅で照合した結果を、同じ表に並べられる（0054 §2.6） |
| Baseline / Acoustic を条件 hash に載せない（factory は hash できないから） | 依存の差が policy の差に見える。名指しを必須にすれば載せられる |
| arm ごとに coverage 不足の episode を落として平均する | arm ごとに母集団が変わる。都合の悪い episode が消えた arm が勝つ |
| 長さの違う episode の割引総和をそのまま平均する | 早く終わった episode ほど負の reward が少ない。「早く壊れたほうが良い」になる |
| 初期 state に screen を掛けない（1 step 進んでから見る） | 上限を超えた state を agent に見せ、そこから探索を始めてしまう |
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
