# 決定記録 0061: RL Supervisor policy の artifact 形式・束縛・昇格の規律

- **種別**: Decision Record
- **Status**: Proposed（**リポジトリ所有者の承認が必要**。§2.1 は #104 の契約を広げる）
- **Date**: 2026-09-21
- **Supersedes**: なし
- **関連**: [0027](0027-fan-control-architecture.md) / [0028](0028-fan-control-contracts.md) §2.3 /
  [0048](0048-thermal-model-artifact-and-inference.md) §2.1 /
  [0050](0050-model-confidence-ood-and-authority.md) §3 /
  [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.1 /
  [0053](0053-control-shadow-mode-and-counterfactual-logging.md) §2.3 /
  [0054](0054-offline-evaluation-attribution-and-gates.md) §2.4 / §2.6 / §2.7 /
  [0055](0055-shadow-duplicate-observation-rule.md) §2.1 /
  [0057](0057-authority-rollout-stage-changes.md) §2.2 /
  [0058](0058-rl-supervisor-training-environment.md)（**全体**）/
  GitHub #88 / #89 / #104 / #105
- **対象 Issue**: #89（依存 #84 / #87 / #88 / #90 / #91 / #102 / #104 / #105）

## 1. Context

0058 は **学習環境**の契約を決めた。action は Supervisor の戦略まで、安全は reward の項に
しない、封をした証拠を持つ episode だけが昇格の根拠になる、そして

> 反実仮想 artifact が揃うまで、この環境で意味のある学習・比較ができるのは
> 「記録済み trajectory のデータ収集」までである（0058 §3）

という状態がそのまま続いている。0058 §5 は **policy artifact（#104）と episode 結果の
対応づけを #89 の範囲**として送った。#89 はその続きで、環境の外側にある次の5点が未決だった。

1. policy artifact は #104 へ**どの能力**を申告するのか。`ArtifactCapability` は
   `observational_replay` / `counterfactual_action` しか持たず、どちらも thermal model の
   能力である。policy にどちらかを名乗らせるのは**偽の申告**になる
2. policy artifact の**形式と family**。何を固定すれば「同じ policy」と言えるのか
3. 探索と shadow 比較の**設定はどこが持つ**のか。`rl-training.yaml`（0058 §2.7、schema v1）に
   足すのか、別の設定にするのか
4. policy を**運転の active slot へ束縛してよい条件**は何か
5. Shadow で集めた Rule / RL の提案を、**どう数えれば証拠になる**のか

さらに、#89 の受入基準のうち「RulePolicy より offline 指標を改善できるか比較できる」は、
**いまの世界の状態では判定できない**。0058 §2.1 のとおり `MpcModelBinding` を作れる artifact が
1つも無く、環境は `learned_controller_available=false` で回る。その条件では
**すべての arm の requested demand が同一になる**ので、候補間に差が出ない。
差が出ないことを「差が無かった」と読ませないことが、この記録の目的の半分である。

## 2. Decision

### 2.1 `ArtifactCapability.SUPERVISOR_STRATEGY` を足す（**所有者の承認が要る契約変更**）

#104 の `ArtifactCapability` に、thermal model とは別の能力を1つ足す。

| capability | 何を主張するか | 誰が要求するか |
|---|---|---|
| `observational_replay` | 観測の再生だけ | Replay / offline 評価 |
| `counterfactual_action` | 候補 Fan action 列に対する将来観測の予測 | #86 の内部モデル / #105 の `registry_attested` dynamics |
| **`supervisor_strategy`**（新） | `WorkloadRegime` から戦略・目的関数 weight・target band への写像 | **#89 の policy 束縛だけ** |

**加算だが、`MODEL_REGISTRY_SCHEMA_VERSION` を 2 から 3 へ上げる。** `ArtifactCapability` は
閉じた列挙なので、値が1つ増えた snapshot を古い読み手は復号できない。復号に失敗すると
`ArtifactLoadStatus.INVALID_REGISTRY` になり、**その registry の artifact が1つも読めなく
なる**（policy を1つ登録しただけで、thermal model の読み込みまで巻き添えになる）。
「加算だから互換」と言えるのは列挙が開いているときだけで、ここは閉じている。
版を上げることで「この snapshot は新しい読み手が要る」を宣言する。

**ただし v2 の registry は読む。** 読めないことにすると、v2 で動いている既存の
production artifact（thermal model）がすべて `INVALID_REGISTRY` になり Fallback へ落ちる。
v3 の差は列挙の値が1つ増えただけで、v2 の snapshot は新しい列挙でそのまま復号できる。

- **読める版は 2 と 3**（`MODEL_REGISTRY_READABLE_SCHEMA_VERSIONS`）。書き出す版は 3
- **v2 は次の書き込みで v3 へ上がる。** 読むだけでは snapshot を書き換えない
- 2 と 3 以外（未知の版）は従来どおり **fail closed**（`INVALID_REGISTRY`）
- `coldaisle.control.model.thermal.ThermalRegistryMetadata` は #104 metadata の写しなので
  同じく 2 と 3 を受け付け、書き出しは 3。**封筒の版は artifact が決める値ではない**ので、
  artifact との照合（`_registry_contract`）には入れない。入れると、v2 で登録済みの
  thermal artifact が snapshot は読めるのに束縛で落ちる
- `RegistryHealthReport.supported_schema_version`（#104 / 0062）は書き出す版の 3 に揃える

良くなること。

- policy artifact が「観測を再生する」「反実仮想を予測する」と**偽って名乗る必要が無くなる**
- 将来観測を1つも予測しない artifact が #86 の `COUNTERFACTUAL_CAPABILITIES` にも
  `AttestedThermalDynamics.bind` の要求にも入らないので、**MPC の内部モデルにも
  学習 dynamics にもなれない**
- 逆向きも塞がる。#89 の束縛は `supervisor_strategy` だけを受け付けるので、
  thermal artifact を `kind=supervisor_policy` として取り違えて渡しても拒まれる

**この1項目だけは #104 の公開契約を広げるので、所有者の承認を要する。**

### 2.2 artifact は `regime_table_v1`。全 regime を1つずつ覆い、Demand を表現できない

`SupervisorPolicyArtifact`（schema `coldaisle.supervisor_policy` v1）は manifest と
`RegimeTablePayload` からなる **非実行の canonical JSON** で、payload は
`WorkloadRegime` → `(strategy, weights, target_band)` の表だけを持つ。

- **`Demand` / `EffectiveZoneDemand` / PWM を鍵として受け付けない**（`extra="forbid"`）。
  action から demand への写像は Learned MPC と Controller Gate にしか無い（0027 / 0028 §2.3）
- **全 regime を値の昇順で1つずつ**持つ。既定の欄や部分的な表を許さない。許すと
  `UNKNOWN` や新しい regime が「通常時の戦略」へ落ちる（AGENTS.md ルール4）。
  並びも1つに固定する。同じ表が2通りの bytes を持つと hash が policy を特定できない
- manifest は `payload_sha256` で**自分の payload だけ**を名指しする。表だけ差し替えた
  artifact を同じ identity のまま読み込ませない
- manifest は `action_space_sha256`（学習時の `supervisor.output_bounds` の hash）・
  `rl_training_config_sha256`・`rl_policy_config_sha256`・`reward_version`・
  学習 episode の識別子を持つ

表にしているのは、**反実仮想モデルが無い以上、より複雑な関数近似を測れない**（0058 §3）
ためである。表なら JSON に封じられ、pickle も任意コードも読み込まずに済み（#104 の制約）、
Rule policy（#88 の `WorkloadPolicyContexts`）と同じ鍵なので同じ episode 群で直接比べられる。
より豊かな state を使う family を足すときは、`policy_family` を増やして版で分ける。

### 2.3 manifest の training 欄は**自称**である。型が縛るのは自称どうしの整合だけ

「どの episode で選んだか」「反実仮想の裏づけがあったか」は artifact が自分で書いた値で、
証拠ではない。証拠は次の3つだけである。

- Registry（#104）が発行する `ArtifactAttestation`（bytes・identity・lifecycle）
- 学習時の `EpisodeResult.promotable`（封をした `DynamicsEvidence` から**環境だけ**が立てる。0058 §2.3）
- 人の承認（promotion は #104 が `HumanApproval` を要求する）

そのうえで、自称が**自分自身と矛盾する**ことは型で塞ぐ。

1. `counterfactual_backed` を名乗るには、`learned_controller_available` と
   「全 episode が promotable」と「Baseline を上回った」の**3つすべて**が要る
2. `counterfactual_backed` でない artifact は、**`SHADOW` 以外の authority を名乗れない**
3. `total_episodes` は**名指しした識別子の数**と一致する。識別子は重複なし昇順なので、
   同じ episode を2度名指しして数を膨らませられない

**同一プロセス内の悪意ある偽造までは防げない**（0050 §3 / 0052 §2.1 と同じ残余リスク）。
狙いは配線の誤りを型で止めることである。

### 2.4 束縛は shadow と active を分ける。active は**いま必ず拒否される**

`SupervisorPolicyBinding` は公開 constructor を持たず、`for_shadow` / `for_active` だけが
発行する。どちらも #104 の `VerifiedArtifact`（nominal 型の照合つき）を要求し、

- `kind` が `supervisor_policy`、`capability` が `supervisor_strategy`
- attestation と manifest の identity（model ID / 版）が一致
- attestation の feature / target schema version が manifest の state / action schema version と一致
- 版が設定の `supervisor.rl_version`（期待する版）と一致
- **runtime の `supervisor.output_bounds` の hash が `action_space_sha256` と一致し、
  表の全欄がその範囲内**（範囲外は**丸めず拒む**。0058 §2.1 と同じ規律）
- 要求 stage が attestation の `authority_compatibility` に入っている
- bytes が canonical JSON で、checksum が attestation と一致する
- **artifact が決める Registry metadata の欄が1つ残らず一致**する。`policy_registry_metadata()`
  が artifact から導く全欄を、`SupervisorPolicyRegistryMetadata` の欄を回って照合し、
  lifecycle 中に Registry 側が書く `offline_evaluation_ref` / `shadow_evaluation_ref` だけを
  名前で除く。**欄を手で並べない。** 並べると、あとから足した欄が照合から漏れ、
  **正しい bytes を登録しながら metadata だけ書き換えた artifact** が通ってしまう

を確かめる。そのうえで用途ごとに違う条件を置く。

| 用途 | 何を要求するか | 理由 |
|---|---|---|
| `for_shadow` | 上の共通条件だけ（**`production_active` を要求しない**） | shadow の出力は MPC にも Fan にも届かない（0053 §2.3）。昇格前の candidate を回せないと、昇格に要る証拠をそもそも集められない |
| `for_active` | **入力を見ずに必ず拒否する** | 下記 |

**`for_active` は開かない門である。** artifact の
`training_evidence.counterfactual_backed` を条件にしない。それは artifact の**自称**で
あって（§2.3）、自称を門の条件にすると**自称を書き換えれば開く門**になる。この記録が
ほかの場所で閉じたのと同じ穴を、いちばん効く場所で開けることになる。

裏づけを**検証できる形**で受け取る手立ては、いまの依存関係では用意できない。環境が
封をした `DynamicsEvidence` の系譜は `control.rl` にあり、`control.supervisor` からは
import できない（`control.rl` が `control.supervisor` を import しているので循環になる）。
そして反実仮想能力を申告した thermal artifact は1つも無いので（0048 §2.1 / 0052 §2.1 /
0058 §3）、**いま通してよい policy もそもそも存在しない**。

だから門は「条件つきで開く」形にせず、**閉じたことと理由を1か所に書く**。
**開く条件——何をもって裏づけとみなし、誰がそれを発行するか——は未決で、
決定記録と所有者の承認が要る**（§5）。

**RL 停止時の fallback は #88 の `SupervisorCoordinator` がすでに持つ**（RL candidate が
未受信・期限切れ・不正なら Rule へ戻る）。本記録は新しい失敗経路を足さない。

#### 束縛の用途は、出力に付いて回る

`SupervisorOutput` は strategy / weights / target band しか持たないので、shadow 用に
束縛した policy の出力と active 用の出力は**値として見分けられない**。用途が値に
付いて回らないと、shadow 用の policy を active slot へ繋いだ構成を Coordinator が
止められない。

そこで `ReceivedSupervisorOutput` に `SupervisorOutputOrigin` を持たせる。

| origin | 誰が付けるか | active slot |
|---|---|---|
| `active_binding` | `for_active` から出た束（**いま発行されない**） | 通る |
| `shadow_binding` | `for_shadow` から出た束 | **通らない** |
| `unverified` | 既定値。Registry を通さない `offline` instance を含む | **通らない** |

値は `RegimeTableRlPolicy.deliver()` が束縛から写すので、呼び出し側が選べない。
**shadow 側は用途を問わない**（MPC にも Fan にも届かないため。問うと昇格前の候補を
観測できなくなる）。手で `active_binding` を名乗る偽造は同一プロセス内では可能で、
0050 §3 と同じ残余リスクである。狙いは配線の誤りを型で止めることである。

### 2.5 探索と shadow の設定は `config/rl-policy.yaml`（schema v1。値はすべて暫定）

```yaml
schema_version: 1
artifact: { model_id }                       # 版は探索のたびに呼び出し側が明示する
search:   { family, seed, max_candidate_tables, minimum_reward_improvement, weight_candidates }
shadow:   { minimum_ticks, minimum_paired_fraction }
```

`artifact.model_id` の長さは、候補の版 `<model_id>-<候補識別子>` が `SupervisorOutput.version`
の上限（120）に収まる分まで（`MAX_POLICY_MODEL_ID_LENGTH`）。候補識別子の最大長は
識別子の形（`<regime>-a<番号>`、番号は `max_candidate_tables` の構造上限の桁数）から導く。
超える model_id は、探索の途中で候補の版が検証に落ちて学習が中断するので、設定の読み込みで落とす。

**`config/rl-training.yaml` へ足さない。** 0058 §2.7 は環境の設定を schema v1 として
確定させており、FINAL の記録が決めた形をこちら側の都合で広げない。所有も分かれる
（環境の契約は #105、探索と昇格の判断材料は #89）。

**ほかの契約が持つ値を写さない**（0054 §2.6 と同じ規則）。action の許容範囲は
`fan-policy.yaml`、episode / reward / coverage は `rl-training.yaml`、絶対上限と
最低安全 demand は `safety.yaml` から取る。

**「反実仮想の裏づけを要求するか」は設定に出さない。** 設定で切れる安全条件は設定次第で
破れる（0052 §4 (c) と同じ理由）。`for_active` がコードとして常に要求する。

### 2.6 Shadow の集計は 0055 と同じ重複規則・完全な識別の束縛・fail closed

`SupervisorShadowLedger` は同じ tick の Rule（active）と RL（shadow）の提案を突き合わせる。
制御へ戻る経路は持たない。

- **同じ tick を2度渡しても数が増えない。** 内容が同じなら畳み、**食い違えば受け取らない**
  （`SupervisorShadowConflictError`）。0055 §2.1 と同じ契約で、どちらを採っても片方の事実が
  消えるので、照合器が選ばない
- **片方しか無い tick を一致として数えない。** RL の提案が無い tick は `Reason.code` ごとに
  数え、対になった tick だけを比較の母数にする
  - 欠落は Rule だけ・RL だけ・**両方**の3つに分けて数える。両方が無い tick を Rule 側へ
    畳むと、同じ tick の RL の欠落理由が内訳から消え、Rule も落ちていた間の RL worker の
    停止が見えなくなる。`rl_errors` の合計は「RL だけ」と「両方」の和に一致する
  - Rule と RL が**同じ版文字列を名乗ってもよい**。版は policy kind ごとに別々に照合し、
    RL 側は下の完全な識別まで束縛するので取り違えない
- **Rule は版、RL は artifact の完全な識別を束縛する。** `SupervisorOutput.version` は
  semantic version だけで、同じ版を名乗る別の model ID・別の bytes を区別できない。
  そこで `SupervisorPolicyIdentity`（model ID・版・artifact bytes の SHA-256）を作り、
  次のように運ぶ
  - `RegimeTableRlPolicy.identity`: Registry を通した instance は attestation の値、
    `offline` は canonical bytes から導いた値（同じ artifact なら同じ値）
  - `deliver()` が `ReceivedSupervisorOutput.identity` へ写す。呼び出し側が選べない
  - `SupervisorCoordinator` は `expected_rl_identity` を受け取り、**完全一致しない提案・
    identity の無い提案を `supervisor_identity_mismatch` で拒む**（active / shadow どちらの
    slot でも）。成功した提案の識別は `SupervisorPolicyEvaluation.policy_identity` として
    trace に残す（成功した RL 出力にだけ付けられる）
  - この欄を足すので、**入れ子の `SupervisorDecision` の版を 1 → 2 へ上げる**
    （`SUPERVISOR_DECISION_SCHEMA_VERSION`）。v1 と名乗りながら v1 に無かった欄を持つ記録は
    作れず、保存済みの v1 はそのまま読める。欄は値が無ければ書き出さない。`ControlTick` の版は
    上げない（番号は PR 間で確保して使うため。#159 の `model_gate` v2 と同じ入れ子の上げ方）
  - `expected_rl_identity` を**渡さない** Coordinator は、照合を飛ばすのではなく
    RL 提案を**すべて** `supervisor_identity_mismatch` で拒む（fail closed。active slot なら
    Rule へ落ちる）。照合を省くと、同じ版を名乗る別の artifact や識別の無い提案が trace に入る
  - `SupervisorShadowLedger` は `rl_identity` を束縛し、完全一致しない・識別の無い提案を
    数えずに拒む。`SupervisorShadowSummary` も識別を持つので、別 artifact の集計と
    digest（= `shadow_evaluation_ref`）が混ざらない
- **時刻は観測した decision から取る**（壁時計を読まない。0054 §2.7）
- `minimum_ticks` と `minimum_paired_fraction` を満たさない集計は `usable=False` で、
  **「差が無かった」と読めない**（fail closed）
  - `minimum_ticks` は **1 以上**（設定の読み込みで検査）。加えて台帳も、対が 0 の集計を
    下限の値によらず `usable` にしない。0 を許すと、比べていない集計が証拠として読めてしまう
  - `usable` は**導いた値**で、台帳と `SupervisorShadowSummary` の型が同じ関数
    （`shadow_summary_usable()`）を使う。型は `minimum_ticks` に 1 以上を要求し、導いた値と
    違う `usable` を持つ集計を受け取らない。保存した集計を読み戻す側が、台帳なら立てない
    `usable=True` を受け取らないためである。学習報告の `promotable` と artifact の
    `counterfactual_backed` も同じく、報告が持つ比較から導く値（`training_counterfactual_backed()`）
    と一致しなければ受け取らない
  - **学習報告に残す値のうち、記録から導けるものはすべて導き直して照合する。** 自称どうしが
    揃っているだけでは、揃えて書き換えた報告が通るためである。探索と報告の型は同じ関数を使う
    - 各候補の `improved`: `candidate_improved()`（記録した安全側の数・共通の長さの reward・
      Baseline arm の値・報告に残した `minimum_reward_improvement` から導く）。短い候補と
      比べられなかった候補は常に false
    - `selected_candidate_id`: `select_candidate()`。`improved_over_baseline` は選ばれた候補の値
    - 選ばれた候補の安全側の数・`short_episodes`・reward、全候補の Baseline の reward: 比較に
      残した arm と `common_matched_steps` から導く
    - `learned_controller_available`: 比較から導く。`comparable` は却下理由の有無と対
    - artifact の `promotable_episodes` / `total_episodes` / `conditions_sha256` /
      `baseline_policy_version`: 比較と報告から導く
    - **artifact が「評価したもの」を名乗る欄**は、形や数ではなく値で比較の記録に束縛する
      - 表: `payload_sha256` は、選ばれた候補として評価した表の hash
        （`CandidateOutcome.table_sha256`）と一致する。さらに、選ばれた arm の episode で実際に
        出した action を、artifact の表がすべて再現する
      - 候補の版: 各候補の `policy_version` は `<manifest.model_id>-<候補識別子>`。比較の
        選ばれた arm はその版で回したもの
      - `training_episode_ids`: 両 arm が実際に回した episode の識別子と一致する
      - `hyperparameters`: 探索 family・seed・候補数・episode 数が報告と一致する
      - `reward_version`・`training_evidence.training_mode` / `dynamics_provenance`: 回した
        episode が持つ値と一致する
      - 報告の記録から導けない欄（`action_space_sha256`・設定の hash・`created_at`・
        `model_version`・`code_commit`・`authority_compatibility`）は、呼び出し側または設定が
        決める値で、Registry の束縛（§2.4）が bytes と metadata の一致を見る
  - **報告だけでは信頼の根にならない**（0050 §3 と同じ立場）。報告は JSON として書き換え
    られるので、上の照合は**報告の中の値どうしの整合**を見るにとどまり、すべてを揃えて
    書き換えた偽造（例: 選ばれた arm が訪れなかった regime の欄だけを変え、`payload_sha256` と
    `table_sha256` を揃える）は止められない
  - **束縛は、設定を持つ登録の前の段階で行う**（`SupervisorPolicyTrainer.certify()`）。候補の表は
    設定（`output_bounds` と `weight_candidates` の並び）と Rule policy から決定論的に作り直せる
    （seed は評価順にしか使わない。`regenerate_candidate_table()`）。`certify()` は次を確かめて
    から artifact を返し、登録はこれを通したものだけにする
    - manifest の `rl_policy_config_sha256` / `rl_training_config_sha256` /
      `action_space_sha256` / `reward_version` / `model_id` と報告の探索条件が、渡した設定と
      一致する（別の設定で作り直させない）。Baseline の版が渡した Rule policy の版と一致し、
      **比較の Baseline arm が記録した action を、その Rule policy の表がすべて再現する**
      （版だけでは同じ版を名乗る別の Rule policy を区別できない）
    - すべての候補の識別子がこの設定で作れ、`table_sha256` が作り直した表の hash と一致し、
      候補数が設定から数えた数と一致する
    - 選ばれた候補の作り直した表が、artifact の表・`payload_sha256` と一致する
  - **登録は照合済みの型だけを受け取る（慣習ではなく型で縛る）。** `certify()` は
    `CertifiedPolicyArtifact` を返し、この型は `certify()` の外では作れない（構築 token。
    同一プロセス内で token を持ち出す偽造は 0050 §3 と同じ残余リスク）。登録用の関数
    `canonical_policy_artifact_bytes()` / `policy_registry_metadata()` は**この型だけ**を受け取り、
    それ以外は `TypeError` で拒む。登録済み artifact の束縛時の照合は、登録の道ではないので
    内部の関数（`_policy_artifact_bytes()` / `_derive_policy_registry_metadata()`）を使う
  - **残余**: #104 の Registry とその CLI（`coldaisle-registry register`、0062）は bytes を解釈せず、
    kind を問わず metadata と payload をそのまま受け取る。そこから照合していない policy
    artifact を書く経路は残る。CLI 側で supervisor policy の登録に照合を要求するかは
    0062（FINAL）の契約を広げる話なので、**所有者の判断**とする（§5）
- 集計の件数（理由別の内訳 `rl_errors` を含む）は**すべて 0 以上**。負の内訳で合計だけを
  合わせた集計を受け取らない
- 集計の digest は #104 の `shadow_evaluation_ref` にそのまま渡せる。ただし **`usable` でない集計は
  参照を出さない**（`evaluation_ref()` が拒む。#104 の `promote()` は参照が空でないことしか見ない）
- **shadow の証拠は、比べた artifact に束縛する。** 候補登録用の `policy_registry_metadata()` は
  評価の参照（`offline_evaluation_ref` / `shadow_evaluation_ref`）を**書かない**（#104 はそれぞれ
  `mark_validated` / `promote` の記録があるときだけ許すので、登録時に書くと
  `register_candidate` が拒む）。shadow の証拠は `SupervisorShadowSummary` で受け取り、昇格の
  policy 専用の入口 `promote_supervisor_policy()` だけが照合してから `ModelRegistry.promote()` へ
  渡す。確かめるのは次のとおり
  - `ref` が照合済み artifact の model ID・版を名指し、registry 上の bytes hash が一致する
  - registry の記録の **artifact が決める metadata の欄がすべて**、照合済み artifact から導いた
    値と一致する（束縛時の照合と同じ関数 `registry_metadata_mismatches()` を使う。checksum
    だけ見て昇格すると、metadata だけ書き換えた記録が production になり、運転時の束縛で
    拒まれて Fallback へ落ちる）
  - 集計の `rl_policy_identity` が照合済み artifact の完全な識別（`certified_identity()`）と一致し、
    集計が `usable` である
  - #104 の `promote()` を直接呼ぶ経路は残る（0062 の契約。§5 の Registry CLI と同じ残余）

違う regime を前提にした提案どうしの比較は、**`SupervisorDecision` の段階で作れない**
（同じ tick の active / shadow は同じ regime を使う。#88）。台帳側で読み替えもしない。

### 2.7 探索は `per_regime_coordinate_v1`。決定論的で、評価順に依らない

Baseline を起点に、**regime を1つだけ**候補 action へ差し替えた表をすべて並べる。
候補 action は `supervisor.output_bounds` の strategy × target band ×
`rl-policy.yaml` の weight 候補である。

**Baseline の表は、評価する Rule policy そのものに聞いて作る**（設定からは作らない）。
設定（`supervisor.rule_policy.contexts`）から作ると、版だけ同じで context の違う policy を
渡されたときに、**報告に載る Baseline arm と `baseline` 候補の表が別物**になる。#88 の
Rule policy は regime から context への写像なので、regime ごとに1度聞けば表が取れる。
Baseline の欄が `output_bounds` の外にあれば**丸めず拒む**。

- **上限を超えたら切り詰めず落とす。** 黙って切ると、報告に出ない候補が生まれ
  「全候補を比べた」と読めてしまう
  - 上限の検査は、action の直積も候補表も**作る前に**、数えて行う（各軸は重複しないので、
    regime ごとの候補数は直積の大きさから Baseline と同じ action の分を引いた数になる）。
    作ってから数えると、大きいが妥当な設定で検査に届く前に資源を使い切る
- 設定した weight 候補が範囲外なら**落とさず拒む**。黙って落とすと、設定した候補が
  評価されていないことに気づけない
- 評価順は seed で決めるが、**選択は順序に依らない**。並び替えの鍵は
  `(安全側の違反, 範囲外 action, -reward, 識別子)` で、同点は識別子で決める
- **比べられない候補は勝たない。** 条件や母集団が揃わず `PolicyComparison` を作れない
  候補は理由付きで記録し、改善扱いにしない（0058 §2.6）
- **比べていない run を「比べた」と記録しない。** Learned MPC を束縛できていない run は、
  action が demand に効かず全 arm の requested が同一になるので、候補を
  `comparable=False`（理由 `learned_controller_unavailable`）にする。`comparable` で
  絞った読み手が「policy を比較した結果」だけを受け取れるようにするためで、
  `True` のまま並べると、差が出なかったことが比較の結論に見えてしまう
- 改善の判定は #105 の辞書式比較をそのまま使う。安全側が同点のときだけ、
  揃えた長さの reward 平均の差が `minimum_reward_improvement` 以上かを見る
- **採点は、すべての候補に共通の長さの上でだけ行う。** 候補ごとに「Baseline と揃えた長さ」で
  採点すると、候補 A を4 step、候補 B を2 step で割り引いた平均を同じ表に並べることになり、
  途中で終わった候補ほど負の reward を積む回数が少なくて有利になる（0058 §2.6 が
  1つの比較の中で言っているのと同じことが、**候補の並びに対しても成り立つ**）。
  候補をすべて回してから、episode ごとに**全 arm の採点できた step 数の最小**を取り、
  その長さで採点する。使った長さは `common_matched_steps` として報告に残す
  （hash では読めないため。0056 §2.3 と同じ理由）
- **Baseline より短い候補を、Baseline と同じ区間を比べたものとして扱わない**（fail closed）。
  reward は共通の長さへ揃えるが、安全側の台帳（違反・範囲外 action）は episode 全体を数える。
  候補が先に終わると、Baseline がその後で観測した安全側の結果を観測しないまま「違反が少ない」
  「同点」に見え、また候補の短さが共通の長さを縮めて健全な候補どうしの並びを変えてしまう
  - **長さは2つあり、目的ごとに意図して使い分ける。** 環境は採点できない終端も記録に積むので、
    どちらも記録の数（`steps`）ではない
    - **観測した長さ**（`safety_observed_steps()`）: 安全側の結果を観測した step の数。
      採点できた step と、採点はできないが安全側の違反を記録した step
      （`safety_floor_shortfall` の終端など）を数え、`dynamics_unusable` /
      `controller_unusable` などの終端記録は数えない。**安全側の台帳を Baseline と同じ区間で
      比べられるか**を決める
    - **採点できた step の数**（`supported_steps`）: reward を持つ step の数。**reward を
      Baseline と同じ区間で比べられるか**を決める。最後の step で `safety_floor_shortfall` を
      記録して終わった候補は、観測した長さが Baseline と等しくても reward を持つ step が1つ少ない
  - **短い**（`short_episodes()`）: 観測した長さ**または**採点できた step の数が Baseline より
    短い episode。終わった理由を問わない。`CandidateOutcome.short_episodes` に該当 episode を
    残し、共通の長さに入れず、`mean_reward_over_common_horizon` を書かず（`None`）、
    **改善扱いにしない**。どちらかが短ければ、違反の数か reward のどちらかを Baseline と同じ
    区間で比べられない。自分の長さで採点した reward を並べると長さの違う総和を並べることになる
    （0058 §2.6）ので、この単純な形を採る
  - **打ち切り**（`truncated_episodes()`）: 観測した長さが Baseline より短く、かつ自分の違反・
    範囲外 action **以外**の理由で終わった episode。違反を観測しなかったことが「違反が少ない」に
    見えるので、候補ごと `comparable=False`（理由 `candidate_truncated`）にし、
    `CandidateOutcome.truncated_episodes` に残す。判定は観測した長さだけで行う。reward を持つ
    step が少ないだけの候補は、安全側の台帳は同じ区間を見ているので却下せず、短い扱いにとどめる。
    自分の違反で終わった短い候補も、違反が台帳に載るので比較には残す（改善にはならない）
  - **比べてよいかは採点の前に1度だけ決める**（`candidate_rejection()`）。共通の長さ
    （`scoring_horizon()`）は、Baseline と、比べてよく、かつ短くない候補だけから取る
- **どの候補も Baseline を上回らなければ、Baseline の表が選ばれる。** 上回っていないのに
  別の表を出さないためで、その artifact は「Rule の戦略を RL artifact として表したもの」になる。
  shadow の配線を確かめるには十分で、戦略は何も変わらない
- `authority_compatibility` の既定は `(SHADOW,)` だけ。authority の拡大は人の判断
  （#92 / 0057）であり、探索の結果として自動で広がってはならない

## 3. Consequences

**いま何ができて、何ができないか**（これを曖昧にしない）。

| #89 の受入基準 | いま満たせるか |
|---|---|
| policy artifact / version を固定できる | **できる。** canonical JSON + manifest hash + Registry 登録まで往復する |
| #88 Interface に適合する | **できる。** `RegimeTableRlPolicy` は `RLPolicy` をそのまま満たす |
| Shadow で MPC 結果へ影響させず評価可能 | **提案の生成と集計はできる。** shadow 用に束縛した policy の提案は active slot を通らない（§2.4）。運転中に decision を台帳へ流す配線は control loop（#74）側 |
| RL 停止時に RulePolicy へ即 fallback 可能 | **できる。** #88 の Coordinator がすでに持ち、本記録は新しい失敗経路を足さない |
| training / evaluation が実 Fan への write 無しで完結する | **できる。** `control/rl` の import 禁止をそのまま引き継ぐ（試験で走査する） |
| #104 へ登録可能な artifact metadata を出力する | **できる。** `policy_registry_metadata()` が `ArtifactMetadata` と1対1に写る |
| **RulePolicy より offline 指標を改善できるか比較できる** | **できない。** `MpcModelBinding` を作れる artifact が1つも無いので、すべての arm の requested が同一になる。仕組みはあるが**判定できない** |

**いちばん重要な帰結**: 反実仮想 artifact が揃うまで、#89 でできるのは
**「policy を固定し、shadow で比べる土台を用意すること」**までである。
**どの戦略が良いかを決めることは、まだできない。** 報告は
`learned_controller_available` と `promotable` にその事実を毎回残し、出力する artifact は
`SHADOW` 互換だけになる。

良くなること。

- policy が Fan Demand を表現する道が**構造的に塞がる**（artifact の型・action 空間・束縛）
- 反実仮想の裏づけの無い policy が active authority を得る道が、型の不変条件で塞がる
- 学習に使った action 空間・設定・reward 版・episode が artifact に固定され、
  別の範囲で学習した policy を読み込めない
- shadow の集計が、重複した入力でも版違いでも部分的な証拠でも膨らまない

悪くなること（と緩和策）。

- **`regime_table_v1` は state を regime へ潰している。** #89 が挙げた state 候補
  （recent history / room conditions / acoustic context）を使っていない。緩和策は
  `policy_family` を版で分け、反実仮想モデルが揃って**差を測れるようになってから**
  豊かな family を足すこと。測れないうちに複雑にしても、良し悪しを判定できない
- 探索を回しても、いまは Baseline の表が選ばれ続ける。緩和策は、その事実を
  `improved_over_baseline=False` と `learned_controller_available=False` として
  報告と artifact の両方に残すこと
- `ArtifactCapability` を1つ足すので、#104 の公開契約が広がる。緩和策は加算的に留め、
  新しい値を要求するのは #89 の束縛だけにすること

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| policy artifact に `observational_replay` を名乗らせ、`ArtifactCapability` を触らない | 偽の申告になる。policy は観測を1つも再生しない。読む側が registry を見て誤解する |
| policy artifact に `counterfactual_action` を名乗らせる | 0052 §2.1 の要求を満たさない artifact が「反実仮想できる」側に混ざる。いちばん危ない読み替えそのもの |
| `kind=supervisor_policy` だけで判定し、capability を見ない | thermal artifact を取り違えて渡す配線ミスが型で止まらない |
| 探索の設定を `config/rl-training.yaml` へ足す | 0058 §2.7 が schema v1 として確定させた形を、こちら側の都合で広げることになる。所有も分かれる |
| 「反実仮想の裏づけを要求するか」を設定に出す | 設定で切れる安全条件は設定次第で破れる（0052 §4 (c)） |
| `for_shadow` にも `production_active` を要求する | 昇格前の candidate を shadow で回せなくなり、昇格に要る証拠をそもそも集められない |
| `for_active` で `production_active` だけを要求する | 人の承認は「registry の手続きを踏んだ」ことしか保証しない。action が demand に効かない条件で選ばれた戦略でも通ってしまう |
| 探索の結果として `authority_compatibility` を自動で広げる | authority の拡大は人の判断（#92 / 0057 §2.2）。探索が自分で権限を増やす経路を作らない |
| `for_active` を「`counterfactual_backed` が真なら通す」形で開けておく | artifact の自称で開く門は、自称を書き換えれば開く。§2.3 で「自称は証拠ではない」と決めた直後に、いちばん効く場所でそれを根拠として読むことになる |
| `capability` を加算するだけで registry の schema 版を据え置く | `ArtifactCapability` は閉じた列挙なので、値が増えた snapshot を古い読み手が復号できない。policy を1つ登録しただけで registry 全体が `INVALID_REGISTRY` になる |
| 未知の capability を前方互換に復号する（版は据え置く） | 復号だけ通しても、再書き出しで未知の値が失われる。#104 の内部表現を変える話になり、本記録の範囲を超える。版を上げるほうが意図を正しく伝える |
| 束縛の用途を `SupervisorOutput` の欄にする | #88 / 0028 §2.3 の出力 schema を広げることになる。用途は「誰が作ったか」であって提案の内容ではないので、worker→loop の carrier（`ReceivedSupervisorOutput`）が持つ |
| shadow slot でも `active_binding` 以外を拒む | 昇格前の候補を観測できなくなる。shadow は MPC にも Fan にも届かないので、用途を問う必要がない |
| registry の読みも v3 だけにする（v2 を `INVALID_REGISTRY` にする） | v2 で動いている既存の production artifact がすべて使えなくなり Fallback へ落ちる。書き出しだけ v3 にして読みは v2 も受け付ければ、移行で使えなくなるものが無い |
| v2 → v3 の移行を専用の書き換え操作にする | 読みが v2 を受け付けるので不要。次の通常の書き込みで v3 になる。読むだけで snapshot を書き換えない |
| thermal の照合に封筒の schema version を入れ続ける | 封筒の版は artifact が決める値ではない。入れると v2 で登録済みの artifact が束縛で落ちる |
| shadow 証拠の識別を semantic version だけで持つ | 同じ版を名乗る別の model ID・別の bytes を区別できず、別 artifact の集計を同じ証拠として読める |
| 識別の照合を台帳だけにする（Coordinator は版だけ） | 同じ版を名乗る別 artifact の提案が Coordinator を通って trace に残り、台帳で初めて落ちる。運ぶ側と数える側の両方で照合する |
| Baseline の表を `supervisor.rule_policy.contexts` から作る | 版だけ同じで context の違う policy を渡されると、報告の Baseline arm と `baseline` 候補の表が別物になる。表は評価する policy そのものに聞く |
| 候補ごとに「Baseline と揃えた長さ」で採点し、その平均を並べる | 候補ごとに長さの違う平均を同じ表に並べることになり、途中で終わった候補が有利になる（0058 §2.6） |
| 束縛時に照合する Registry metadata の欄を手で並べる | あとから足した欄が照合から漏れる。正しい bytes に別の metadata を付けた登録が通る |
| Learned MPC を束縛できない run の候補も `comparable=True` で並べる | `comparable` で絞った読み手が「policy を比較した結果」と受け取る。差が出なかったことが比較の結論に見える |
| 範囲外の候補・範囲外の表を範囲内へ丸める | 丸めると罰が無くなり、運転時に拒否される戦略を学び続ける（0058 §2.1 と同じ） |
| 候補が上限を超えたら切り詰めて続ける | 報告に出ない候補が生まれ、「全候補を比べた」と読めてしまう |
| 比べられなかった候補を「差が無かった」として並べる | 条件や母集団の揃わない結果を同じ表に載せることになる（0058 §2.6） |
| Baseline を上回らないときに、最も reward の高い候補を出す | 「上回っていない」と「最良」を混同する。上回らないなら戦略を変えない |
| shadow の集計で、同じ tick の重複を「最後に来たもの」で上書きする | 何回・どの順で渡したかで結果が変わる。0055 §2.1 が同じ理由で退けている |
| shadow の集計で、RL の提案が無い tick を母数から外す | 母数が縮み、少ない対から高い一致率が出る。欠落は理由別に数えて母数に残す |
| 学習の `created_at` を壁時計から作る | 同じ入力から同じ bytes が出なくなり、再現性の判定に使えない（0054 §2.7） |
| より豊かな state（history / room / acoustic）を使う policy family を先に作る | 反実仮想モデルが無い間は、複雑にしても良し悪しを**測れない**（0058 §3）。測れるようになってから版で足す |
| artifact の training 欄を昇格の根拠として読む | 自称である。根拠は Registry の attestation と環境が立てた `promotable`、そして人の承認 |
| RL policy を `SupervisorCoordinator` の active slot へ直接渡せるようにする | #88 は受信済みの candidate を受け取る形で、鮮度も版も control loop 側が判定する。policy object を差し込む経路を作ると、その判定を迂回できる |

## 5. 未決事項

- **本記録は `Proposed` で、所有者の承認が要る。** とくに §2.1 は #104 の公開契約を広げ、
  **registry の書き出し版を 2 から 3 へ上げる**（v2 は読み、次の書き込みで v3 へ上がる）。
  #104（PR #162 / 決定記録 0062）は main に入り、`RegistryHealthReport` の版を 3 に揃えた
- **`for_active` を開く条件が未決**（§2.4）。何をもって「反実仮想の裏づけ」とみなし、
  誰がその証拠を発行するか。候補は (a) 環境が封をした `DynamicsEvidence` の系譜を
  policy 側へ運べる形にする、(b) 学習で束縛した反実仮想 thermal artifact の
  `ArtifactAttestation` を束縛時に要求する、のいずれか。**決めるまで門は閉じたままにする**
- `config/rl-policy.yaml` の値（seed・候補 weight・候補上限・改善の下限・shadow の下限）は
  **すべて実測前の暫定値**。確定には shadow の実運用データが要る
- 運転中の `SupervisorDecision` を `SupervisorShadowLedger` へ流す配線（どこで観測し、
  どこへ保存するか）は control loop 側（#74 / 決定記録 0060）で決める
- worker が束縛の用途（`SupervisorOutputOrigin`）を control loop まで運ぶ形も未決。
  いまは `ControlLoop` が組み立てる `ReceivedSupervisorOutput` が `unverified` のままで、
  `active_policy: rl_policy` の構成では Rule へ落ちる。**証明できない提案に制御権を
  渡さないので、それが正しい振る舞いである。** 運べる形にするのは、`for_active` の門を
  開く条件を決めるときに一緒に決める
- 学習の入口を CLI（`coldaisle-rl-*`）にするか。**いまは作らない。** 反実仮想モデルが
  無い間、CLI があると「回せば policy が良くなる」と読めてしまう。記録済み trajectory の
  収集経路（#83 / #84）が揃ってから決める
- episode 結果を #91 の `EvaluationReport` の arm として載せる形（0058 §5 が #91 / #92 側へ
  送った未決事項）。本記録も踏み込まない
- `policy_family` を増やす条件。反実仮想 artifact が揃い、候補間の差を実際に測れるように
  なってから、どの state を足すかを新しい記録で決める
- #104 の Registry CLI（`coldaisle-registry register`）で supervisor policy を登録するときに、
  `certify()` を通したことを要求するか（§2.6 の残余）。0062（FINAL）の CLI 契約を広げるので、
  所有者が決める。決めるまでは、照合済みの登録は `certify()` → `canonical_policy_artifact_bytes()`
  / `policy_registry_metadata()` の道で行う
