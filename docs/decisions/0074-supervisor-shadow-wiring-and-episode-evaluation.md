# 決定記録 0074: 運転中の Supervisor decision を Shadow 台帳へ流す配線と、RL episode 結果を Offline Evaluation の arm にする接続形式

- **種別**: Decision Record
- **Status**: FINAL（2026-09-29、リポジトリ所有者が承認）
- **Date**: 2026-09-29
- **Supersedes**: なし
- **関連**: [0028](0028-fan-control-contracts.md) §2.2 / §2.3 /
  [0030](0030-control-decision-trace-storage.md) /
  [0053](0053-control-shadow-mode-and-counterfactual-logging.md) §2.4 /
  [0054](0054-offline-evaluation-attribution-and-gates.md) §2.1〜§2.7・§5 /
  [0055](0055-shadow-duplicate-observation-rule.md) §2.1 /
  [0057](0057-authority-rollout-stage-changes.md) §2.3 / §2.4 /
  [0058](0058-rl-supervisor-training-environment.md) §2.3 / §2.6・§5 /
  [0060](0060-control-loop-runtime.md) §2.4 / §2.6・§5 未決 5 /
  [0061](0061-rl-supervisor-policy-artifact-and-binding.md) §2.4 / §2.6・§5 /
  [0062](0062-model-registry-operations.md) /
  `AGENTS.md`「絶対に守るルール」1・2・4・6・9 /
  GitHub #74 / #89 / #91 / #92 / #104 / #105
- **対象 Issue**: #89 / #105

## 1. Context

FINAL の記録が、次の2つを別々の場所へ送ったまま閉じていない。

| 送った記録 | 未決の内容 | 送り先 |
|---|---|---|
| 0061 §5 | 運転中の `SupervisorDecision` を `SupervisorShadowLedger` へ流す配線（どこで観測し、どこへ保存するか） | control loop 側（#74 / 0060） |
| 0058 §5 / 0054 §5 / 0061 §5 | RL 学習環境の episode 結果を #91 の `EvaluationReport` の arm としてどう載せるか | #91 / #92 側 |

0060 は control loop の実行時契約を決めたが、Shadow 台帳には触れていない（0060 §5 の未決 5 は
worker プロセスの実装を #86 / #89 へ送っただけ）。0054 は arm の名前空間を factual / counterfactual の
2つに決め、「RL Supervisor を含む比較は同じ arm の枠で足せるが本記録では扱わない」とした。
結果として、**どちらの記録の範囲でもない**まま残っている。

いまの main の状態を先に確かめておく。

- `ControlTick.supervisor`（`SupervisorDecision` v2）は `coldaisle-fand` が**すでに decision trace へ
  保存している**（0028 §2.3 / #82）。台帳が読む材料は trace に揃っている
- `SupervisorShadowLedger`（0061 §2.6）は実装済みだが、**どこからも呼ばれていない**
- `SupervisorShadowLedger.observe()` は tick を **`tick_id` だけ**で重複判定している。
  `tick_id` は**再起動で 0 に戻る**（0060 §2.6）。一方で trace の主キーと #91 の評価器は
  `(ts_ms, tick_id)` で行を識別している。**保存済みの trace を再起動を跨いで流すと、別の tick が
  同じ tick として衝突し `SupervisorShadowConflictError` で止まる**
- `coldaisle-fand` は `SupervisorCoordinator` を `expected_rl_identity` 無しで組み立てており、
  RL worker（`SupervisorOutputSource`）も配線していない。したがって**いまの運転では、shadow slot の
  RL 提案はすべて欠落か `supervisor_identity_mismatch` になる**（0061 §2.6 の fail closed）。
  台帳に流しても `usable=False` の集計しか出ない。それが正しい振る舞いである
- `EpisodeResult` / `PolicyComparison`（0058）は `control/rl` に、`EvaluationReport` は
  `control/evaluation` にあり、**互いに import していない**
- `EvaluationReport` の bytes は #92 の `raise_stage()` が**Learned MPC の authority 昇格の証拠**として
  読む（0057 §2.4）
- 反実仮想能力を申告した artifact は1つも無く、episode はすべて `learned_controller_available=false`・
  `promotable=false` である（0058 §3 / 0061 §3）

いま決める理由は2つある。#89 の受入基準「Shadow で MPC 結果へ影響させず評価可能」と
#105 の受入基準「#91 Offline Evaluation へ結果を出力できる」は、**どちらもこの接続が無い限り
満たせない**。そして、接続の形を実装者が決めると、制御プロセスに集計を持ち込む・MPC の昇格証拠に
simulator 由来の数字を混ぜる、という**取り返しのつきにくい向きへ倒れやすい**。

**この記録は `SupervisorPolicyBinding.for_active` を開く条件を決めない**（範囲外。§5）。
ここで作る証拠は、どれも active の門の条件として読まれない。

## 2. Decision

### 2.1 Shadow 台帳は**制御プロセスの外**で、保存済み decision trace から後で組み立てる（推奨）

台帳を持つのは **新しい合成の起点 `coldaisle-supervisor-shadow`（`src/coldaisle/supervisor_shadow.py`）**
だけにする。`coldaisle-evaluate`（#91）・`coldaisle-drift`（#93）と同じ形の、読み取り専用の
1回実行の CLI である。

```bash
uv run coldaisle-supervisor-shadow --evidence config/supervisor-shadow-runs.yaml \
  --out var/supervisor-shadow.json
```

| 問い | 決定 |
|---|---|
| どのプロセスか | `coldaisle-supervisor-shadow`（人が起動する1回実行の CLI）。`coldaisle-fand` でも RL worker でもない |
| どの時点か | **運転の後**。保存済み trace を、manifest に明示した期間（`[start_ms, end_ms)`。「直近」のような相対指定は置かない）で読む |
| 何を読むか | decision trace（`control_traces`）の `ControlTick.supervisor` だけ。**新しい記録項目も新しい IPC も作らない** |
| どう開くか | `coldaisle-evaluate` の `EvidenceDatabase`（`immutable=1`、`-wal` / `-journal` に中身があれば開かない）をそのまま使う |
| 何を書くか | `SupervisorShadowRunReport`（schema v1、新設。下の「出力の形」）の canonical JSON を1つ、`--out` へ。**DB・Registry・設定・trace には書かない** |
| Registry に触れるか | **読むだけ。** manifest が名指した RL artifact を `ModelRegistry.load_version()`（`create=False` で開き、書かない）で検証し、その attestation から識別を作る（下の「台帳に渡す tick」）。`register_candidate()` / `mark_validated()` / `promote()` などの**書く操作は呼ばない**。import の走査で、CLI の module がそれらを呼ばないことを確かめる |
| 誰が使うか | 人が `promote_supervisor_policy()`（0061 §2.6）へ渡す。昇格は CLI の外で行う |
| shadow の下限をどこから取るか | 検証済みの `rl-policy.yaml`（`PolicyShadowConfig`）を**必須の入力**にする。下限を引数や manifest で上書きする口は作らない（下の「下限の束縛」） |

#### 出力の形（集計と振り分けの件数を同じ文書に置く）

振り分けの件数（下の「台帳に渡す tick と渡さない tick」）は、いまの `SupervisorShadowSummary` v2 に欄が無い。
集計の schema を上げると、昇格の入口と Registry の `shadow_evaluation_ref` が名指す digest の意味が変わるので、
**集計は変えずに包む**。

- `SupervisorShadowRunReport`（schema v1、`extra="forbid"`）は次だけを持つ
  - `summary`: `SupervisorShadowSummary` v2 **そのもの**（入れ子。bytes の作り方は変えない）
  - `summary_sha256`: `summary` の canonical digest。**validator が作り直して一致を確かめる**
  - `filtered`: 理由別の件数（`no_supervisor_decision` / `no_shadow_slot` / `active_not_rule`）。
    **3つの鍵を常に全部出す**（0 でも省かない。欠けた鍵と 0 を区別させない）
  - `period`（`[start_ms, end_ms)`）と、照合に使った `fan-policy.yaml` の digest
- 昇格に使うのは `summary` だけである。人は `SupervisorShadowRunReport.summary` を取り出して
  `promote_supervisor_policy()` へ渡し、Registry の `shadow_evaluation_ref` は**これまでどおり集計の digest**を名指す。
  包みの digest を昇格の証拠にしない（振り分けの件数は監査用で、`usable` の判定に入らない）
- 包みの型は `coldaisle.supervisor_shadow`（合成の起点）に置き、`control/supervisor` は包みを知らない

#### 制御へ逆流しないことの保証

「逆流しない」を運用の約束ではなく、**経路が存在しないこと**で保証する。

1. **プロセスが別である。** 台帳の例外（`SupervisorShadowConflictError` など）・資源の上限
   （`MAX_SHADOW_TICKS`）・集計の遅さは、control tick にも heartbeat（0060 §2.7）にも届かない
2. **import の向きを試験で走査する。** `coldaisle.control_daemon` と `coldaisle.control.loop` は
   `coldaisle.control.supervisor.shadow` を import しない。`coldaisle.supervisor_shadow` は
   `coldaisle.control.hardware` / `safety` / `reactive` / `serial` / `subprocess` / `coldaisle.event_entry`
   を import しない（0054 §2.7 と同じ規則・同じ走査）
3. **DB を書けない開き方をする**（`immutable=1`。上の表）。trace へ集計を書き戻さない（0053 §2.4 と同じ）
4. **出力を読む制御側のコードが存在しない。** 集計は Registry の `shadow_evaluation_ref` の材料に
   なるだけで、`SupervisorCoordinator`・MPC・設定のどれにも入力として渡らない。
   `shadow_evaluation_ref` も production への昇格にしか効かず、active の門（`for_active`）は
   **入力を見ずに拒否する**ままである（0061 §2.4）
5. **LLM のツールにしない**（AGENTS.md ルール1）。公開する場合も読み取り専用・集計済みで、
   別の記録で決める（§5）

#### 台帳に渡す tick と渡さない tick

trace の1行ごとに、次の順で振り分ける。**振り分けの件数は理由別に集計と並べて出す**
（黙って落とさない。0054 §2.3 と同じ理由）。

| trace の行 | 扱い | 理由 |
|---|---|---|
| 索引（`ts_ms` / `tick_id` / `schema_version`）と本文が食い違う | **run 全体を拒否** | 0053 §2.4 の trace 検証と同じ |
| `ControlTick` v8 未満（`runtime` が無い） | **run 全体を拒否** | 下の設定の照合ができない |
| `runtime.config` の `fan-policy.yaml` hash が、渡した設定の hash と違う | **run 全体を拒否** | 別の Rule 表・別の `rl_version` で回した区間を同じ集計に混ぜない。期間を分けて渡す |
| `supervisor` が無い | 渡さない（`no_supervisor_decision` として数える） | Supervisor を配線していない区間は比較の母数ではない |
| `supervisor.shadow` が無い / active が Rule でない | 渡さない（`no_shadow_slot` / `active_not_rule` として数える） | 構成上そもそも比較していない区間。台帳は受け取らない（`SupervisorShadowUsageError`） |
| 上記以外 | **台帳へ渡す** | RL の欠落・識別の不一致（`supervisor_identity_mismatch`）は台帳が `rl_errors` として数える（0061 §2.6） |

- **台帳が `SupervisorShadowUsageError` / `SupervisorShadowConflictError` を投げたら、run 全体を
  拒否する**（落とした行を除いて続けない）。Rule 表との不一致は「別の表で回した区間が混ざった」
  ことを意味し、除いて数えると残りの区間だけで `usable` に届きうる
- 台帳に束縛する Rule 表は、渡した検証済み `fan-policy.yaml` から `rule_policy_table()` で作る。
  **RL の識別は文字列で受け取らない。** manifest は model ID と版だけを名指し、CLI が Registry
  （#104）を**読んで**（`load_version()`。上の表）得た attestation から `SupervisorPolicyIdentity`
  （bytes hash を含む）を作る。hash を文字列で
  受け取ると、同じ版を名乗る別 artifact の集計を作れてしまう（0057 §2.3 と同じ理由）
- **同じ入力からは同じ bytes を出す。** 生成時刻を持たず、時刻は decision から取る（0054 §2.7）

#### 下限の束縛（緩い設定で作った集計を昇格に使わせない）

`SupervisorShadowSummary.usable` は、集計が持つ `minimum_ticks` / `minimum_paired_fraction` から導かれる。
CLI が別の（緩い）`rl-policy.yaml` を読めば、`usable=True` の集計を作れてしまう。いまの
`promote_supervisor_policy()` は識別しか照合しないので、その集計が通る。これを塞ぐ。

- `promote_supervisor_policy()` に **検証済みの `PolicyShadowConfig`**（昇格の時点で読んだ `rl-policy.yaml`）
  を必須の引数として足し、集計の `minimum_ticks` / `minimum_paired_fraction` が**それと等しくなければ拒否する**
  （#89 の実装変更）。緩いほうだけでなく厳しいほうの食い違いも拒否する。「どちらが安全側か」を
  昇格の入口で解釈させない
- 束縛は **値で**行い、`rl-policy.yaml` 全体の hash は集計に持たせない。下限の値は集計の bytes に
  すでに入っており（digest が覆う）、値で比べれば完全に照合できる。全体の hash にすると、探索の
  候補（`search`）だけを変えても過去の集計が使えなくなる。**集計の schema 版は上げない**

#### 台帳の重複判定の鍵を `(ts_ms, tick_id)` にする（#89 の実装変更）

`SupervisorShadowLedger` の重複判定の鍵を `tick_id` から **`(ts_ms, tick_id)`** に変える。
trace の主キー・#91 の評価器（`EvaluationInputError`「同じ `(ts_ms, tick_id)` の trace が2つある」）・
0060 §2.6 の「`tick_id` だけで照合しない」と揃える。

- 同じ鍵で同じ内容は畳み、食い違えば受け取らない（0055 §2.1 の契約は変えない）
- `SupervisorShadowSummary` の形は変わらないので、**集計の schema 版は上げない**
  （鍵は台帳の内部状態で、集計の bytes には現れない）

### 2.2 RL episode 結果は**別の report 型**で出し、`EvaluationReport` には入れない（推奨）

0054 §2.1 の arm の枠に **第3の名前空間 `episode:`** を足す。ただし置き場所は `EvaluationReport` ではなく、
`control/evaluation` に新しく作る **`PolicyEpisodeReport`（schema v1）** にする。

| 名前空間 | 出どころ | 置く report | 読む側 |
|---|---|---|---|
| `applied:` | decision trace（適用された構成） | `EvaluationReport` | #92（authority 昇格。0057 §2.4） |
| `counterfactual:` | `ShadowRecord` | `EvaluationReport` | #92 |
| **`episode:<policy>+<policy_version>`**（新） | `PolicyComparison`（0058 §2.6） | **`PolicyEpisodeReport`** | supervisor policy の validated 化（#104。§2.3） |

**`EvaluationReport` の形も版も変えない。** 理由は3つある。

1. `EvaluationReport` の bytes は **Learned MPC の authority 昇格の証拠**として #92 が読む（0057 §2.4）。
   同じ文書に simulator / 記録再生の episode の数字を混ぜると、承認者が名指す digest に
   **運転の実績ではない数字**が入り、episode を足しただけで承認済みの digest が変わる
2. `EvaluationReport` は時系列 split の segment と holdout の gate を必須にしている（0054 §2.5）。
   episode の比較は segment を持たない。入れるには「segment の無い report」を許すことになり、
   0054 §2.6 の「tick の無い segment を作らない」が守る穴を別の形で開ける
3. 型が別なら、#92 の `raise_stage()` は `PolicyEpisodeReport` の bytes を `EvaluationReport` として
   **復号できずに拒む**（`extra="forbid"`・版の Literal）。episode の結果が MPC の昇格証拠として
   読まれない、を**型で**保証できる（試験で確かめる）

#### `PolicyEpisodeReport` が持つもの・持たないもの

入力は **`PolicyComparison` 1つ、検証済みの `RlPolicyConfig`**（`rl-policy.yaml`。cost の閾値を取る）、
**RL arm ごとの `certify()` を通した artifact**（識別を取る）だけ。
同じ条件・同じ episode 群・同じ Learned MPC の有無は 0058 §2.6 で比較にすでに保証されている。
report は `control/evaluation/episode.py` に置き、`control.rl` の型を**読むだけ**で import する
（`control.rl` は `control.evaluation` を import しない。向きは一方向）。
**呼び出し側が数字や hash を文字列で渡す口は作らない。** report の欄は、すべてこれらの入力から導く。
`PolicyComparison` の arm は `policy` と `policy_version` しか持たず、model ID や bytes hash を含む
`SupervisorPolicyIdentity` を比較からは作れない。そこで RL arm ごとに artifact を受け取り
（arm の `policy_version` をキーにした対応。キーは**対応を引くためだけ**に使う）、次の**表と action の証拠**で
arm を artifact へ束縛する。**版の文字列どうしの一致では束縛しない。**
`SupervisorPolicyTrainer` は RL arm を `<model_id>-<候補識別子>` の版で回し（`training.py` の
`_run_candidate()`）、`certified_identity().version` は manifest の意味論的な `model_version`（例 `0.1.0`）で、
**2つは別の名前空間**である。版の一致を要求すると、実際の学習出力から report を1つも作れない

- **表**: artifact の `manifest.payload_sha256` を束縛の digest にする。`certify()` は、この値が
  **選ばれた候補の `CandidateOutcome.table_sha256`** と一致し、設定から作り直した表の hash と一致することを
  確かめている（`training.py` の `certify()`）。report はこの digest を arm の欄として残す
- **action**: 比較のその arm の全 episode・全 step の `StepRecord.action` を、artifact の表が
  すべて再現する（`first_unreplayed_step(artifact.payload, arm)` が `None`。`certify()` と学習報告の照合と同じ関数）。
  1 step でも再現しなければ report を作らない
- **1対1**: 異なる RL arm に同じ artifact（同じ `payload_sha256`）を当てない。RL arm に対応する artifact が
  欠けている・余っている場合も作らない
- arm の `policy_version` は比較が持つラベルとしてそのまま report に写す。**識別（model ID・版・bytes hash）は
  `certified_identity()` からだけ取り**、ラベルから導かない

| 欄 | 出どころ |
|---|---|
| `comparison_sha256` | 入力の `PolicyComparison` の canonical digest。**report をその比較へ束縛する**（§2.3 の照合に使う） |
| `conditions_sha256` | `PolicyComparison.conditions_sha256` |
| 設定の digest（`rl-training.yaml` / `fan-policy.yaml` / `safety.yaml`） | episode の `config_digests`（下の「条件 hash を2段にする」）。**arm 間・episode 間で揃っていなければ作らない** |
| `rl-policy.yaml` の digest と `minimum_reward_improvement` の値 | 入力の検証済み `RlPolicyConfig` から |
| `training_mode` / dynamics の provenance / `safety_model` / `reward_version` / `applied_demand_tolerance` | episode の値。**arm 間で揃っていなければ作らない** |
| `learned_controller_available` | 比較から導く（欄として受け取らない） |
| arm ごと: `policy` / `policy_version` / RL なら `SupervisorPolicyIdentity` と表の digest | Rule は版、RL は入力の `certify()` を通した artifact の `certified_identity()` と `manifest.payload_sha256` から（上の表と action の証拠で arm に束縛したもの。`policy_version` はラベルとして写すだけ） |
| arm ごと: 安全側の数（絶対上限の超過・floor 不足・範囲外 action・最小 margin） | `EpisodeSafety` の合計と **worst-case episode** |
| arm ごと: coverage（採点できた step・割合・理由別の内訳・`usable_for_comparison` でない episode 数） | `EpisodeCoverage` |
| arm ごと: 共通の長さ（episode ごとの全 arm の採点できた step 数の最小）で揃えた reward 平均 | 0058 §2.6 / 0061 §2.7 と同じ規則。**使った長さを欄として残す** |
| arm ごと: `promotable` な episode の数 / 全 episode 数 | `EpisodeResult.promotable`（環境だけが立てる） |

#### episode の条件 hash を2段にする（#105 の実装変更）

いまの `EpisodeResult.conditions_sha256` は、`rl-training.yaml` / `fan-policy.yaml` / `safety.yaml` の digest を
**中に含むが外から読めない**1つの hash である。report が設定ごとの digest を出すには、それが episode の
条件に本当に入っていたことを読む側が確かめられなければならない（呼び出し側の値を載せるだけなら、gate の
閾値と provenance が別の設定を指せる）。そこで次のように変える。

- `EpisodeResult` に `config_digests`（上の3つの検証済み設定の digest。環境が自分の持つ設定から作る）と
  `other_conditions`（それ以外の条件。いまの payload から3つの digest を除いたもの）を**読める形の欄**として持たせる。
  中の hash を1つだけ持たせる形は採らない。中の hash を payload から作り直す手段が無いと、episode の
  dynamics・`reward_version`・`safety_model`・許容幅を書き換えても中の hash が変わらず、外の一致が通ってしまう
- **`EpisodeResult` の validator は内側から順に確かめる。** (1) `other_conditions` のうち episode の欄としても
  現れる値（`episode_id` / `seed` / `mode` / `reward_version` / `discount` / `dynamics` / `safety_model` /
  `applied_demand_tolerance` / `learned_controller_available`）が、episode の欄と一致する。
  (2) `conditions_sha256 = canonical_sha256({config_digests, other_conditions})` を作り直して一致する。
  食い違う episode は作れず、読み戻しでも拒む
- `other_conditions` のうち episode の欄に現れない入力（workload trace・初期 window・controller の条件など）は
  いまと同じく digest として入る。これらは episode の中からは作り直せず、**環境で同じ条件・同じ seed から
  回し直して同じ bytes になること**（0058）で確かめる。この記録はそこを変えない
- 条件 hash が policy 以外のすべてを覆う、という 0058 §2.6 の性質は変わらない。変わるのは
  「3つの設定の digest を外から照合できる」ことだけである。episode の schema 版は 2 に上げ、
  v1 の episode は report の入力にしない
- `rl-policy.yaml` は episode 環境の入力ではない（条件 hash に入らない）。report の入力として
  別に受け取り、§2.3 の validated 化で**同じ検証済み設定から作り直して**照合する

**置かないもの**: 「運転での温度実績」に見える欄。episode の観測は記録の再生か近似 simulator の
出力であって、**適用された構成の運転実績ではない**（0054 §2.2 と同じ帰属規則を、第3の名前空間にも
掛ける）。`applied:` の欄（温度 percentile・ΔT・Air Balance・RPM）を episode arm に作らない。

#### gate は 0054 §2.4 と同じ3段・辞書式で、閾値は**写さない**

Rule arm（Baseline）以外の各 arm に gate を1つ出す。**上の段が落ちたら下の段では覆らない。**

1. `safety`: 安全側の違反が 0、範囲外 action が 0（worst-case episode で判定）
2. `evidence`: **比較できる episode がすべて `promotable`**、`learned_controller_available=true`、
   coverage の下限を満たす、短い / 打ち切りの episode が無い（0061 §2.7）
3. `cost`: Baseline に対する改善が `minimum_reward_improvement` 以上（0061 §2.7 の辞書式比較をそのまま使う）

- 閾値は `rl-training.yaml`（coverage）と `rl-policy.yaml`（`minimum_reward_improvement`）から取る。
  **`evaluation.yaml` に写さない**（0054 §2.6 / 0061 §2.5 と同じ規則。新しい設定値は作らない）
  - coverage の下限は **report が読み直さない。** 環境が episode ごとに `usable_for_comparison` /
    `promotable` として判定済みで、その `rl-training.yaml` は `config_digests` で episode に束縛されている。
    report が別の `rl-training.yaml` を読んで判定し直すと、episode が作られた設定と gate の設定が分かれる
  - `minimum_reward_improvement` は入力の検証済み `RlPolicyConfig` から取り、値と digest を report に残す
- 判定できないこと（欠測・未設定・coverage 不足・Learned MPC 無し）は `blocked`。**`pass` にしない**
- **いまはすべての RL arm が `evidence` で `blocked`（`learned_controller_unavailable`）になる。**
  それが 0058 §3 / 0061 §3 の「いまはどの戦略が良いかを決められない」の正しい表現である
- gate は**助言**である（0054 §2.4）

### 2.3 episode report は supervisor policy の `offline_evaluation_ref` にだけ使い、policy 専用の入口を通す

#104 の `promote()` は **validated（`offline_evaluation_ref` あり）の artifact だけ**を production にする。
supervisor policy について、その参照の出どころはこれまで決まっていなかった。

- `PolicyEpisodeReport.evaluation_ref()` は `supervisor-episode:<digest>` を返す。**名指した RL arm の gate が
  `pass` でなければ参照を出さない**（0061 §2.6 の `evaluation_ref()` と同じ fail closed）
- validated 化は **policy 専用の入口 `validate_supervisor_policy()`**（`control/supervisor/artifact.py`。
  0061 §2.6 の `promote_supervisor_policy()` と対になる）だけで行う。入力は report に加えて
  **元の `PolicyComparison`**・検証済みの `rl-training.yaml` / `fan-policy.yaml` / `safety.yaml` / `rl-policy.yaml`・
  `certify()` を通した artifact・Baseline の Rule policy・validated にする Registry の `ref` と `expected_revision` である。
  次を確かめてから `ModelRegistry.mark_validated()` へ渡す
  - **validated にする対象が、渡した `certify()` 済みの artifact そのものである。** `ref` が supervisor policy で、
    その `(model_id, version)` が `certified_identity(certified)` の model ID・版と一致する。Registry の
    `expected_revision` の snapshot にその `ref` の記録があり、記録の checksum（`metadata.sha256`）が
    `certified_identity(certified).artifact_sha256` と一致し、artifact から導いた Registry metadata と
    記録の metadata に食い違いが無い（`registry_metadata_mismatches()` が空）。1つでも満たさなければ
    **拒否し、`mark_validated()` を呼ばない**。`promote_supervisor_policy()` の照合と同じ内容である。
    これが無いと、artifact A と A の通る report で**別の candidate B** を validated にでき、B が A の
    `offline_evaluation_ref` を持ったまま、B の shadow 証拠で `promote_supervisor_policy()` を通る
    （昇格は `offline_evaluation_ref` を検証し直さない）
  - 渡した比較の canonical digest が report の `comparison_sha256` と一致し、**その比較・検証済み
    `rl-policy.yaml`・渡した `certify()` 済みの artifact から report を作り直した bytes が、渡した report の bytes と一致する**。
    report は集計だけを持ち action の列を持たないので、action の照合は元の比較に対して行う。
    作り直しの一致で、report の識別・数字・gate がその比較から導かれたことを保証する
    （識別だけを差し替えた report を通さない）
  - report の設定の digest が、渡した検証済み設定の digest と一致する（別の設定で作った episode の
    結果を、いまの設定での評価として validated にしない）
  - report の RL arm の `SupervisorPolicyIdentity` が、`certify()` を通した artifact の識別
    （`certified_identity()`）と一致する
  - 元の比較のその arm の全 episode・全 step の `StepRecord.action` を、artifact の表がすべて再現する
    （0061 §2.6 と同じ照合）
  - Baseline arm の Rule が、渡した Rule policy の表（`rule_policy_identity()`）と一致する
- report に action の列を入れる案・action 列の digest だけを入れる案は採らない。前者は report が
  `MAX_EPISODES_PER_ARM` × step 数に比例して膨らみ、後者は digest を照合するのに結局元の比較が要る。
  **元の比較を必須の入力にし、report はそこへ digest で束縛する**
- **#104 の `mark_validated()` を直接呼ぶ経路は残る**（0062 の CLI 契約。0061 §5 の Registry CLI と同じ残余として記録する）

**帰結として、policy 専用の入口（`validate_supervisor_policy()` / `promote_supervisor_policy()`）を通る限り、
反実仮想 artifact が揃うまで supervisor policy は validated にも production にもならない。**
これは**入口を通ることを前提にした性質であって、Registry が強制する性質ではない。** 上のとおり #104 の
`mark_validated()` / `promote()`（0062 の CLI）を直接呼べば、episode の gate を経ずに参照を付けて昇格できる。
その経路を閉じるか（Registry CLI が `supervisor_policy` の kind について policy 専用の入口を要求する等）は
0061 §5 と同じく別の記録で決める（§5）。**それまでは「fail closed」を Registry の保証として扱わない。**
運用者は supervisor policy の validated 化・昇格を policy 専用の入口でだけ行う
shadow の運転は `for_shadow` が `production_active` を要求しない（0061 §2.4）ので、**candidate のまま
shadow で比べられる**。証拠の収集は止まらない。

## 3. Consequences

**いま何ができて、何ができないか。**

| やりたいこと | この記録の後にできるか |
|---|---|
| 保存済み trace から Rule / RL の shadow 集計を作る | **できる**（CLI）。ただし RL worker と `expected_rl_identity` の配線が無い間は、RL 提案がすべて欠落か識別不一致になり **`usable=False`** になる |
| 再起動を跨いだ期間の trace を集計する | **できる**（鍵が `(ts_ms, tick_id)` になる） |
| 設定を変えた前後を1つの集計にまとめる | **できない**（run ごと拒否。期間を分けて渡す） |
| episode の比較を #91 の枠（名前空間・3段 gate・digest）で出す | **できる**（`PolicyEpisodeReport`） |
| episode の結果で supervisor policy を validated にする | **できない**（gate が `blocked`。反実仮想 artifact が揃うまで） |
| episode の結果で Learned MPC の authority を上げる | **できない**（型が違い、#92 が復号できない） |
| RL に制御権を渡す | **できない**（`for_active` は閉じたまま。範囲外） |

良くなること。

- #89「Shadow で MPC 結果へ影響させず評価可能」と #105「#91 へ結果を出力できる」の接続が決まり、
  実装に入れる
- 台帳・集計の失敗が control tick と deadman に届く経路が**存在しない**
- trace という既存の不変な記録だけから集計を作り直せる。集計を失っても trace が残っていれば再現できる
- MPC の昇格証拠（`EvaluationReport`）の意味が変わらない。0057 の検証をやり直さずに済む
- episode の結果が「運転の実績」に見える欄を持たない

悪くなること（と緩和策）。

- **集計は後追いで、運転中には見えない。** 緩和策は、shadow の比較は昇格の証拠であって運転の判断材料
  ではないので、後追いで足りると明示すること。常時見たくなったら、読み取り専用の表示として別に決める（§5）
- **trace の保持期間（0030 / `control_trace_days`）を過ぎた区間は集計できない。** 緩和策は、出力の
  集計 JSON 自体を証拠として保存し、Registry へ参照（digest）で束縛すること
- **`fan-policy.yaml` の hash は設定全体を覆うので、Supervisor に関係の無い変更でも期間を分けることになる。**
  緩和策は、分けた期間ごとに集計を作れること。欄ごとに「関係あるか」を判定する規則は、
  数え落とし（0058 §2.6 の「丸ごと hash」と同じ理由）のほうが危ないので採らない
- **v8 未満の trace は集計できない。** v8（`runtime`）は 0060 で入ったばかりで、それ以前に RL の
  shadow 運転は存在しない（worker が無かった）ので、失うものは無い
- **episode の schema 版が 2 に上がり、v1 の episode は report の入力にできない**（§2.2 の条件 hash の2段化）。
  緩和策は、episode は同じ条件・同じ seed から同じ bytes で作り直せる（0058）ので、v1 を移行せず回し直すこと
- **`promote_supervisor_policy()` と validated 化の入口の引数が増える**（検証済み設定・元の比較）。
  緩和策は、どれも「文字列ではなく検証済みの物を渡す」既存の規則の延長で、呼び出し側の選択肢は増えないこと
- report の型が2つになる。緩和策は、名前空間・gate の段・`GateOutcome`・digest の作り方を
  `EvaluationReport` と共有し、**型だけを分ける**こと

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `ControlLoop` が tick ごとに台帳へ `observe()` する（in-loop） | 台帳の例外・1,000,000 tick の上限・集計の時間を制御プロセスへ持ち込む。heartbeat の後に置いても次の tick の前に居座る（0060 §2.7 の「記録の待ち時間は heartbeat の間隔を食いつぶせない」と同じ問題）。再起動で集計も消える |
| 常駐の別デーモンが trace を追いかけて台帳を更新する | 常駐プロセスと「どこまで読んだか」の永続化という失敗の種類が増えるわりに、trace は既に保存されている。後から読めば足りる |
| RL worker が台帳を持つ | worker は自分の出力しか見えない。Coordinator が識別の不一致・期限切れで拒んだことも、同じ tick の Rule の出力も知らないので、trace と食い違う集計になる |
| `coldaisle-evaluate` のサブコマンドにする | manifest・出力の型・読む側（#92 と #104）が違う。1つの CLI が2つの証拠を出すと、どちらの digest を承認に使うかを取り違えやすい。`EvidenceDatabase` は共有する |
| 台帳の鍵を `tick_id` のまま、CLI が再起動の境目で期間を切る | 境目を「`tick_id` が戻った」ことから推定することになり、短い再起動で番号が重なると取り違える。trace の主キーと同じ鍵にすれば推定が要らない |
| 設定の食い違う行・Rule 表と合わない行を除いて集計を続ける | 残りの区間だけで `usable` に届きうる。除いたことが証拠の digest から読めない。run ごと拒否して期間を分けさせる |
| RL の識別（bytes hash）を manifest の文字列で受け取る | 同じ版を名乗る別 artifact の集計を作れる（0057 §2.3 と同じ穴）。Registry の attestation から作る |
| episode arm を `EvaluationReport` v3 の第3名前空間として同じ report に入れる（主な代替案） | 1つの report で済み、#105 の受入基準の文言にいちばん素直に合う。**ただし** #92 が承認で名指す digest に simulator / 記録再生の数字が混ざり、segment を持たない arm のために「segment の無い report」を許す必要が出る。#92 側に「episode arm を読まない」検証を足すことになり、FINAL の 0057 §2.4 の検証の意味を広げる |
| episode を ControlTick 風の trace に変換して既存の評価器に流す | 運転していない tick を trace の形で作ることになり、`applied:` の名前空間に simulator の結果が「適用された実績」として入る。0054 §2.2 と 0058 §2.2 がそれぞれ禁じた帰属そのもの |
| `PolicyComparison` をそのまま #91 の出力とみなす（接続しない） | 0054 の3段 gate・coverage の fail closed・digest の規則が掛からない。Registry に渡す参照を何から作るかが決まらないまま残る |
| episode の gate の閾値を `evaluation.yaml` に置く | `rl-training.yaml` / `rl-policy.yaml` が既に持つ値の写しになる（0054 §2.6 / 0061 §2.5） |
| `blocked` の episode report からも `offline_evaluation_ref` を出し、validated 化は人の判断に任せる | 参照は「評価を通った」ことの記録として #104 の監査に残る。通っていない評価を通った形で残すと、あとで production への昇格の根拠として読まれる。人の判断は `promote` の `HumanApproval` で別に入る |
| supervisor policy の validated 化に episode report を使わず、shadow 集計だけで validated / production を決める | 0061 §2.6 は shadow 集計を production の条件（`shadow_evaluation_ref`）に使っている。offline と shadow の2つの証拠を1つに潰すと、#104 の2段の lifecycle の意味が失われる |

## 5. 未決事項

- 次の5点は、2026-09-29 に所有者が本文（推奨案）のとおり承認した（Status: FINAL）
  1. 台帳を制御プロセスの外（CLI）に置くか、in-loop にするか（§2.1。推奨は外）
  2. 台帳の鍵を `(ts_ms, tick_id)` に変え、設定の食い違う期間を run ごと拒否するか（§2.1）
  3. episode の結果を別の report 型にするか、`EvaluationReport` v3 の第3名前空間にするか（§2.2。推奨は別の型）
  4. 反実仮想 artifact が揃うまで supervisor policy を validated にしない帰結を受け入れるか（§2.3）。
     この帰結は policy 専用の入口を通る限りのもので、#104 の直接の経路は別の記録まで残る
  5. 証拠の束縛のために、`promote_supervisor_policy()` へ検証済み `PolicyShadowConfig` を足し（§2.1）、
     episode の条件 hash を2段にして schema を v2 に上げるか（§2.2）、shadow の出力を包みの型
     `SupervisorShadowRunReport` にするか（§2.1）
- **`SupervisorPolicyBinding.for_active` を開く条件は、この記録の範囲外である。** 何を反実仮想の裏づけと
  みなし誰が発行するか、RL へ制御権を渡す時期と条件は、0061 §5 のとおり**別の決定記録で決める**。
  本記録の shadow 集計と episode report は、その門の条件として読まない
- RL worker プロセスと `SupervisorOutputSource` の実装、worker が束縛の用途（`SupervisorOutputOrigin`）と
  識別を control loop まで運ぶ形（0060 §5 未決 5 / 0061 §5）。本記録が固定するのは「`coldaisle-fand` に渡す
  `expected_rl_identity` は Registry の attestation から作り、文字列から作らない」ことだけである。
  それまでは集計が `usable=False` になり続ける
- `coldaisle-supervisor-shadow` を定期実行（systemd timer など）にするか。いまは人が起動する。
  自動にするなら、出力の置き場所と保持を含めて決める
- trace に書けなかった tick（`trace_dropped`。0060 §2.7）を集計の母数にどう出すか。いまの台帳は
  「観測した tick」しか数えず、落ちた tick は見えない。`runtime.tick_period_ms` から期待 tick 数を数えて
  欠けを出すかは、実運用の trace を見てから決める
- 1つの `PolicyEpisodeReport` に複数の `PolicyComparison`（探索の全候補）を載せるか。v1 は1つだけにする
- shadow 集計・episode report をダッシュボードや AI ツールに出すか。出すなら読み取り専用・集計済みで
  （AGENTS.md ルール1 / 8）、別の記録で決める
- `config/rl-policy.yaml` の shadow の下限と `minimum_reward_improvement`、`config/rl-training.yaml` の
  coverage の下限は、0058 / 0061 のとおり**すべて実測前の暫定値**のままである
- #104 の `mark_validated()` / `promote()` を直接呼ぶ経路（0062 の CLI）は残余として残る。
  CLI 側で supervisor policy に policy 専用の入口を要求するかは、0061 §5 と同じく新しい記録で決める
