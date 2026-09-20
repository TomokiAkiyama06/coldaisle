# 決定記録 0052: Learned MPC optimizer の内部モデル要件と Hard Constraints の扱い

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-20
- **Supersedes**: なし
- **関連**: [0027](0027-fan-control-architecture.md) / [0028](0028-fan-control-contracts.md) §2.3〜§2.6 /
  [0031](0031-thermal-dataset-contract.md) / [0033](0033-air-balance-config-boundary.md) /
  [0048](0048-thermal-model-artifact-and-inference.md) §2.1 / [0050](0050-model-confidence-ood-and-authority.md) /
  `docs/requirements.md` Q-22
- **対象 Issue**: #86（依存 #61 / #78 / #79 / #81 / #82 / #84 / #85 / #94 / #102 / #103 / #104）

## 1. Context

#86 は Learned Thermal Model を内部モデルとして、Front / Rear / Top の Demand 列を
未来予測しながら最適化する Learned MPC を作る。ところが決定記録 0048 §2.1 は、
Dataset v1 が anchor action から label 時刻までの**後続 Fan action 列を持たない**ことを理由に、
v1 baseline の capability を `observational_replay` に限り、

> anchor action を任意の candidate action へ変えた反実仮想予測や、#86 optimizer の内部 model
> として使えるとは宣言しない

と明記している。つまり「いま学習できる唯一のモデル」は、MPC が必要とする
「候補 action を変えたら将来温度がどう変わるか」を科学的に主張できない。

このまま v1 artifact を optimizer の内部モデルに差し込むと、後続 action 列を学習していない
係数を Fan action の因果効果として読み、その予測でコストを比べて Demand を決めてしまう。
Confidence / OOD（#85）はこの取り違えを検出できない。判定しているのは
「入力が学習範囲にあるか」であって「予測が反実仮想として正しいか」ではないからである。

あわせて、#86 が所有する次の論点も未決だった。

- optimizer が従う制約（Demand 範囲・rate limit・zone 固有 floor / ceiling）と、
  Critical Safety（#78）が持つ絶対制約の関係
- optimizer 内部 timeout と、解が無いときの扱い
- horizon / step / 探索法 / 目的関数の重みと、replay の再現性

## 2. Decision

### 2.1 内部モデルの要件（capability を別の軸として要求する）

MPC の内部モデルには `InferenceCapability.COUNTERFACTUAL_ACTION` を要求する。
`MpcModelBinding.for_control` は、**Registry（#104）が発行した `ArtifactAttestation` と**
モデルの申告を突き合わせ、次をすべて満たすときだけ束縛する。

1. `ArtifactAttestation` を伴っていること（= #104 の検証経路を通った bytes であること）
2. attestation の `kind` が `thermal_model`
3. attestation の `production_active` が真（= その kind の**いまの production pointer そのもの**）
4. `capability` が `counterfactual_action`
5. 要求する authority stage が **attestation の** `authority_compatibility` に含まれる
6. **attestation の** `version` が runtime の期待と一致する
7. モデルの申告（`model_id` / `model_version`）と、公開している feature / target schema version が
   attestation と一致する

3 は **promotion の承認を迂回させない**ための条件である。#104 の `load_version()` は Replay や
offline 評価のために候補・検証済み・引退の artifact も返す。その結果をそのまま controller へ
配線できると、人の承認を経ていないモデルが active authority を得てしまう。attestation は
lifecycle 状態と production pointer かどうかを載せ、active 制御はそれを要求する。
Replay / offline 評価は production でない attestation をそのまま使う。

**verification・authority・版・schema は attestation を正とし、モデルの自称値は照合にだけ使う。**
自称値だけで判断すると、別の artifact を包んだ wrapper が `registry_verified` を名乗って
FULL authority を得られてしまう。

#104 側には、この束縛のために最小限の発行境界を足した。`ArtifactAttestation` は公開
constructor を持たず、`ModelRegistry` の検証経路だけが発行する。`VerifiedArtifact` は
attestation を必須の項目として持つため、**検証経路の外では組み立てられない**。
これは決定記録 0048 §2.4 が #85 / #86 統合前の条件として挙げていた
「sealed / opaque な発行境界」にあたる。

**暗号的な保証ではない。** 同一プロセス内の悪意ある偽造を防げないことは所有者が受け入れている
（決定記録 0050 §3）。狙いは、検証していない artifact や別の artifact を取り違えて制御経路へ
渡す**配線の誤り**を、レビューではなく型で止めることである。

`capability` だけは #104 の `ArtifactMetadata` がまだ持たないため、いまはモデルの申告を読む。
現行の artifact 形式は `observational_replay` しか表現できず active 制御へ届かないので、実害は
無い。反実仮想 artifact を #84 が定義するときに #104 の metadata へ capability を持たせ、
ここも attestation 側から取る（§5）。

`ThermalModelManifest.capability` と `ThermalPrediction.capability` は
`Literal[observational_replay]` に固定されているため、**現行のどの artifact も条件 2 を満たせない。**
したがって束縛は決定論的に失敗する。これは不具合ではなく意図した振る舞いで、0048 §2.1 の
制約をコードで表したものである。

`InferenceCapability` に `counterfactual_action` を追加するのは語彙のためだけで、
v1 artifact がそれを名乗れるようにはしない。後続 action 列を持つ Dataset 版と実測評価が
揃った時点で、#83 / #84 側が artifact 形式と manifest の Literal を更新する。

束縛の失敗は例外として上へ投げず、runtime が `LearnedFailure.MODEL_LOAD_FAILURE` として
Gate（#79 / #85）へ渡す。制御は Fallback で走り続ける（AGENTS.md ルール4）。
**「使えないなら弱い権限で使う」という暗黙の降格はしない。**

### 2.2 提案は1回の検証済み推論に束ねる

1 tick の流れを固定する。

1. いま掛かっている effective demand に対する **anchor 推論**（#84 の `predict`）
2. その推論に対する Confidence / OOD の判定（#85 の `assess`）
3. 候補 action 列の評価（`predict_plan`）と探索
4. 同じ `inference_id` を持つ `ControllerProposal` の生成

`predict_plan` が返す `PlanPrediction` は `anchor_inference_id` と、**評価した候補 plan の識別子**
（`plan_digest` = `ActionPlan.digest()`）を持つ。optimizer は、手元の anchor と候補 plan の
両方に一致しない予測でコストを測らない。同じ tick の候補は step の刻みが同じなので、時刻だけでは
「別の候補（別の demand）の予測」を見分けられない。取り違えたまま採点すると、別の温度予測に
今の音響・風量・変化コストを足して選んでしまう。anchor 推論そのものも、Registry の証拠
（model ID・版）とこの tick の入力時刻へ突き合わせる。`MpcProposal` は型の不変条件として
提案・assessment・解の `inference_id` と `model_version` の一致を要求し、
**判定の付いていない提案や、別の推論の判定を付けた提案を worker が作れないようにする。**
提案に入れる `confidence` / `ood` は `ConfidenceAssessment.apply_to` を必ず通す
（Registry 検証済みでない判定は 0048 §2.4 によりそこで弾かれる）。

### 2.3 探索は Baseline を出発点にした決定論的な座標降下

- **plan の形**: 1つの demand を horizon 全体で保持する（move blocking = 1）。
  次 tick で必ず再計算する receding horizon なので、初版は step ごとに別の値を探索しない。
- **出発点**: Fallback（#79）の requested を制約へ収めた値。したがって incumbent は常に Baseline で、
  採用する解は**内部モデルの上で必ず Baseline 以下のコスト**になる。
- **掃引**: zone を Front → Rear → Top の固定順で回し、各 zone で許容範囲を
  `candidate_levels` 個に等分した格子を試す。改善が無い掃引で打ち切る。
  **等コストでは乗り換えない**（評価順で解が変わると replay が再現しない）。
- **乱数を使わない。** 同じ Snapshot / window / model / 設定 / 単調時計の列からは同じ解が出る。
  deterministic seed を持たないのは、そもそも確率的な探索をしないからである。
- **実行するのは plan の最初の step だけ。** `MpcSolution` は `requested` が
  `plan.steps[0]` と一致することを型で要求する。

目的関数は #88 Supervisor の `SupervisorObjectiveWeights` を重みに使い、項は
GPU 温度・CPU 温度・Air Balance（#81）・Acoustic（#94）・変化量の5つ。単位の違う項を足すため、
各項は設定の基準量（`mpc.optimizer.cost_scales`）で割って無単位にする。
温度は Supervisor の target band の**外へ出た分だけ**を二乗で数える（帯の中は等価に扱う。
帯の中で最も低い値を選ばせると常に最大風量が最適になるため）。Air Balance の目標比は
#81 の `air-balance.yaml` が持つ `BalanceBand` をそのまま受け取り、MPC 側に写さない。
比を推定できない step には `unknown_balance_cost` を課し、**不明を「良い」と読み替えない。**

Fan power の項は初版に入れない（§5）。

### 2.4 Hard Constraints は「探索を狭める写し」であって安全の根拠ではない

optimizer が従う制約は3つを重ねた最も狭い範囲とする。

1. 設定した zone ごとの探索範囲（`mpc.optimizer.zone_bounds`）
2. Critical Safety の最低 demand（`safety.zone_min_demand` と、その tick の Safety floor）
3. 直前の effective demand からの変化幅（下げる向きは `safety.ramp_down_per_s` × step）

規則は2つ。

- **どれも範囲を狭める向きにしか働かない。** 満たせる値が無ければ緩めず、実行不能として扱う。
- **Safety floor は上げ幅の制限に勝つ。** floor が直前の値より上にある tick では上げ幅の制限を外す。
  制限を優先すると、冷却を強めるべき瞬間に弱いまま留まる。
- **ceiling は緩めない。** 直前の値が ceiling より上（forced Max の直後など）で、下げ幅の制限の
  ために1 step では ceiling まで下げきれないときは、ceiling の上へ範囲を広げず**実行不能**とする。
  広げてしまうと、設定した探索上限を MPC 自身が上書きしたことになる。

**この制約は安全上の保証ではない。** 最終裁定は後段の Reactive Guard（#80）と
Critical Safety（#78）が毎 tick 決定論的に行う（0028 §2.4）。ここで狭めるのは
「どうせ潰される候補を評価して budget を使わない」ためである。
`control/mpc` は `control/hardware` / `control/safety` / `control/reactive` を import せず、
`EffectiveZoneDemand` も PWM も表現しない。この2点は試験で機械的に検査する。

### 2.5 timeout と実行不能の扱い

optimizer 内部の打ち切りは2つ持つ。

| 打ち切り | 設定 | 結果 |
|---|---|---|
| 計算時間 | `mpc.budget_ms`（0028 §2.6。単調時計で数える） | `optimizer_status = timeout` |
| 内部モデル評価回数 | `mpc.optimizer.max_evaluations` | `optimizer_status = timeout` |

評価回数の上限は**時計に依存しない決定論的な打ち切り**で、replay で同じ結果を得るために持つ。

計算時間は **anchor 推論と Confidence 判定を含めて**数える。探索だけを測ると、推論が遅い tick で
予算を超えたまま `ok` を返してしまう。経過時間は**探索の入口と、各モデル評価の前と後**に見る。
入口で見るのは、推論と判定だけで予算を使い切った tick にさらに1回まるごとモデルを回させない
ためである（予測は高価になりうる）。前後の両方で見るのは、Baseline の評価だけで使い切る場合や、
最後の候補で越える場合を `ok` にしないためである。

- `timeout` / 実行不能（`error`）のとき、optimizer は**解を返さない**。
  中途半端な探索結果を制御に使わない。
- それでも anchor 推論とその判定は残っているので、controller は理由と
  `optimizer_status` を付けた提案を作る。requested には Fallback の値を入れる。
  Gate はこの status を見て Fallback へ落とす（#79 の `optimizer_timeout` / `optimizer_error`）。
  **「なぜ使わなかったか」を trace に残すため**に、提案そのものは作る。
- 推論・判定・制約の組み立て自体が失敗したときは提案を作らず、
  `LearnedFailure.OPTIMIZER_EXCEPTION` として渡す。次 tick は通常どおり動く。
- **worker 境界では例外の種類で選ばない。** 推論・判定・最適化・任意依存の model が何を投げても
  構造化した失敗へ翻訳する。特定の例外型だけを捕まえると、例えば #84 が内部の feature layout
  異常に使う `RuntimeError` が素通りして制御ループごと死ぬ。`KeyboardInterrupt` /
  `SystemExit` などの `BaseException` は停止の合図なので素通しする。
- **失敗の理由を Gate の手前で落とさない。** `LearnedControlStatus` に `failure_reason` を持たせ、
  Gate は Fallback の理由の `detail` へ載せる。`model_load_failure` としか残らなければ、
  decision trace から原因を追えない。

1 tick 全体の締め切り（#74）は optimizer の所有ではない。ループ側が
`control_deadline_exceeded` として Gate へ渡す（#79 で実装済み）。

### 2.6 設定は #103 の validated config に置く（値はすべて暫定）

`fan-policy.yaml` の `mpc` を拡張し、`schema_version` を 7 へ上げる
（v6 を v7 の意味で読まず、起動前に拒否する）。

```yaml
mpc:
  period_ms: ...
  budget_ms: ...
  valid_ms: ...
  optimizer:
    horizon_ms:        { value: ..., status: provisional }
    step_ms:           { value: ..., status: provisional }
    candidate_levels:  { value: ..., status: provisional }
    sweeps:            { value: ..., status: provisional }
    max_evaluations:   { value: ..., status: provisional }
    max_step_up:       { value: ..., status: provisional }
    max_step_down:     { value: ..., status: provisional }
    zone_bounds:       { front: { floor: ..., ceiling: ... }, rear: ..., top: ... }
    cost_scales:       { temperature_c: ..., balance_ratio: ..., acoustic_cost: ..., demand_change: ... }
    cost_metrics:      { cpu_temperature: ..., gpu_temperature: ... }
    unknown_balance_cost: { value: ..., status: provisional }
```

- **コードに既定値を置かない。** metric 名（`cost_metrics`）も設定へ出す。
  内部モデルの target schema がその metric と horizon を覆うことは、optimizer の**生成時**に照合する
  （tick ごとに失敗させない）。
- horizon / step の候補は統合メモ 2026-09-13 §21.3 の
  「prediction horizon 60〜120 秒級、制御周期 2〜5 秒級」を出発点とするが、**確定値としない。**
  0028 §2.6 の `mpc.period_ms = 10000` を含め、実測と deadline の評価で決める（Q-22）。
- 構造上限として control step 数 64、1 tick の内部モデル評価 4,096 回を置く。
  これは未信頼な設定による資源枯渇を防ぐ境界で、調整値ではない。
- `provisional_values()` は新しい暫定値の**位置と根拠だけ**を起動ログへ出す（値は出さない）。

## 3. Consequences

良くなること。

- v1 artifact を MPC の内部モデルに差し込む経路が**構造的に塞がる。**
  「観測再生の係数を因果効果として読む」取り違えが、レビューではなく型と束縛で止まる。
- 提案・assessment・解が1つの推論に束ねられ、別の入力の判定を借りて authority を得られない。
- Baseline を出発点にするので、内部モデルの上で Baseline より悪い解を採用しない。
- timeout / 実行不能 / モデル異常のすべてが、例外ではなく trace に理由の残る Fallback になる。
- 設定と構造上限が分かれ、実測後に調整すべき値が `provisional` として一覧できる。

悪くなること（と緩和策）。

- **いまは active 制御に使える内部モデルが1つも無い。** #86 の機構は実装されるが、
  本番では常に Fallback になる。緩和策は、反実仮想を扱える Dataset 版（後続 action 列を持つ）と
  実測評価を #83 / #84 / #105 で用意すること。それまでの評価は offline / shadow で行う。
- 座標降下と move blocking = 1 は最適解を保証しない。緩和策は、receding horizon で毎 tick
  再計算することと、採用解が Baseline 以下であることを型で保証すること。
- 目的関数の基準量（`cost_scales`）が増え、設定の項目数が増える。緩和策は、すべてを
  `provisional` として起動ログに出し、#90 / #91 の評価で根拠付きに置き換えること。

## 4. 却下した代替案

**(a) v1 artifact をそのまま内部モデルに使い、authority を SHADOW に絞る。**
却下。shadow でも記録した提案は #90 / #91 の評価と #92 の昇格判断に使われる。
反実仮想として成立しない予測から作った提案を「MPC の提案」として評価に混ぜると、
昇格の根拠そのものが汚れる。0048 §2.1 が禁じているのはまさにこの読み替えである。

**(b) MPC 専用の capability 列挙（`PlanCapability`）を新設し、#84 の `InferenceCapability` に触れない。**
却下。能力の語彙が2つに割れ、「artifact は何を主張しているのか」を2箇所で読む必要が出る。
`InferenceCapability` に値を1つ足しても、manifest の `Literal` が固定されている限り
v1 artifact がそれを名乗ることはできず、安全性は変わらない。

**(c) Critical Safety の制約をコストの重い項として目的関数へ入れる。**
却下。決定記録 0027 の「Critical Safety は ML から独立」に反する。重みで表した制約は
weight の設定次第で破れる。制約は制約のまま扱い、最終裁定は後段に残す。

**(d) timeout のとき、その時点の incumbent を解として返す。**
却下。incumbent は Baseline か、途中までの掃引で選ばれた値である。前者なら Fallback と同じで、
後者なら「探索を打ち切った理由」を trace に残さないまま ML の提案として記録されてしまう。
timeout は Fallback の理由として明示的に残す。

**(e) 探索を確率的（焼きなまし・ランダムサンプリング）にし、seed を設定に置く。**
却下。seed を固定しても、budget による打ち切り位置が実行環境の速度で変わるため、
replay の再現性は seed だけでは守れない。決定論的な探索と評価回数上限のほうが素直である。

**(f) Air Balance の目標比を `mpc.optimizer` へ写す。**
却下。同じ定数が `air-balance.yaml`（#81 / 決定記録 0033）と2箇所になる。
目標帯は #81 が所有し、MPC は受け取るだけにする。

## 5. 未決事項

| 論点 | どこで決めるか |
|---|---|
| #104 の `ArtifactMetadata` へ `capability` を持たせる | 反実仮想 artifact 形式を定める #84 と同時に #104 |
| Acoustic / Air Balance の推定に、要求との対応づけ（どの demand への推定か）を持たせるか | #81 / #94。いまは設定済みの純粋な model だけを繋ぐ前提 |
| モデル object と検証済み bytes の byte 一致（#84 の `from_verified_artifact` 相当を反実仮想側にも） | #84 / #104 |
| horizon / step / 目的関数の重み・基準量の確定値 | 実測後に #103 の設定と後続の決定記録（Q-22） |
| 反実仮想を扱える Dataset / Model 形式（後続 action 列） | #83 / #84 / #105 |
| Fan power のコスト項（取得方法と近似の根拠） | #75 / #94 の実測後 |
| move blocking を複数ブロックへ広げるか | horizon と budget の実測後 |
| Acoustic を実測 SPL へ置き換えたときの基準量 | #95 |
| stage 昇格の判断に MPC の改善量をどう使うか | #90 / #91 / #92 |
