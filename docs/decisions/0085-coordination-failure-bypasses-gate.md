# 決定記録 0085: `mode: apply` で Air Balance の協調が失敗した tick は Controller Gate を迂回して raw baseline を選ぶ

- **種別**: Decision Record
- **Status**: FINAL（2026-10-01、リポジトリ所有者が承認）
- **Date**: 2026-10-01
- **Supersedes**: [0078](0078-air-balance-fallback-coordination.md) §2.2 の流れ図と §2.6 のうち、
  **`mode: apply` で協調が `failed` になった tick に、raw baseline を `ControllerGate.select()` の `fallback` として渡す
  （その tick も Gate を通す）点のみ**。その tick は Gate を呼ばず、raw baseline をそのまま requested にする（§2.1）。
  0078 の他の点（協調の位置・条件・上限・保持・`fallback_exception` への翻訳と次 tick の `EMERGENCY`・
  `mode: shadow` の失敗は記録だけ・`try` / `except` は `AirBalanceCoordinator.apply()` だけを囲む・trace の塊の形）は有効
- **関連**: [0028](0028-fan-control-contracts.md) §2.5 (c) / §2.7 /
  [0053](0053-control-shadow-mode-and-counterfactual-logging.md) /
  [0057](0057-authority-rollout-stage-changes.md) §2.2 /
  [0060](0060-control-loop-runtime.md) §2.4 /
  [0078](0078-air-balance-fallback-coordination.md) §2.2 / §2.5 / §2.6 / §2.7 / §2.10 / §2.11
- **対象 Issue**: #81（2026-09-30 の Issue のコメント「実装の前に決める点」の P2）

## 1. Context

0078（FINAL）の §2.2 の順序は次のとおりで、§2.6 は `mode: apply` の協調の例外（`coordinate()` の不具合・
下げる値の検出）を「`failed` として記録し、その tick は raw baseline を使う。loop が `fallback_exception` に
翻訳し、次 tick の Critical Safety が `EMERGENCY`」と決めた。

```text
Fallback.propose() → Critical Safety.evaluate() → AirBalanceCoordinator.apply()
  → ControllerGate.select(fallback = coordinated baseline（失敗なら raw baseline）, learned = …) → 合成
```

0078 のマージ後に残った Codex の指摘（Issue #81 の 2026-09-30 のコメント、P2）:
この順序では、協調が失敗した tick も raw baseline を持って `ControllerGate.select()` へ進む。
authority が LIMITED / EXPANDED / FULL で健全な Learned MPC の提案があれば、**その tick は Learned の
提案が選ばれうる**。`fallback_exception` が Safety に効くのは次 tick からなので、決定論的な層が壊れた
ことが分かっている tick に、ML の提案が Fan を握る。

いまの loop には同じ形の先例がある。Gate 自身の例外（`ControlLoop._select()`）は `fallback_exception` に
翻訳し、**その tick は Gate の選択を使わず Fallback の値で続ける**（`selection = None` →
`_requested()` が baseline の requested を返す）。協調の失敗も 0078 §2.6 で「0028 §2.7 の Fallback /
Gate の例外と同じ扱い」と決めたのに、その tick の requested の作り方だけが Gate の失敗と揃っていない。

### 所有者の判断（2026-10-01）

2026-10-01 に所有者が P2 の推奨案を採った: **`mode: apply` で協調が失敗した tick は、Gate の失敗と同じく
Gate を迂回し、raw baseline を選ぶ。** 以下 §2 はその内容を実装が参照できる形に書き下したものである。

## 2. Decision

### 2.1 迂回する tick と、その tick の requested

**条件**: `mode: apply` で、`AirBalanceCoordinator.apply()` が例外を投げたか、出力の検証（下げる値の検出を含む）に
失敗した tick（0078 §2.6 の `failed`）。0078 §2.3 の条件を満たした tick だけが `coordinate()` を呼ぶので、
この tick は必ず `AUTO`・snapshot が `AVAILABLE`・Fallback の提案あり・Critical Safety が `NORMAL` か `DEGRADED` である。

その tick は次のとおりにする。

```text
Fallback.propose()                     … raw baseline
Critical Safety.evaluate()             … いまと同じ（requested を見ない）
AirBalanceCoordinator.apply()          … failed（mode: apply）
  ↓ Gate を呼ばない（ControllerGate.select() も set_operating_mode() も呼ばない）
requested = raw baseline の requested（zone ごとの値も reason もそのまま）
合成（0028 §2.4）→ Hardware Backend   … いまと同じ。Guard / Safety の floor・ceiling・ramp_down・forced_max は掛かる
次 tick: fallback_exception → Critical Safety が EMERGENCY（全 zone Max）   … 0078 §2.6 のまま
```

- **Gate の失敗（`_select()` の例外）と同じ経路に乗せる。** loop の上では「その tick の `selection` が無い」状態で、
  `_requested()` は raw baseline の requested を返す。Learned の提案はその tick では選ばれず、
  LIMITED / EXPANDED の帯（`_limited_request()`）も計算しない
- **raw baseline を選ぶ。** 失敗した協調の出力（`proposed` や保持中の値）は使わない。0078 §2.4 の保持は
  0078 のとおりこの tick で解く
- **fault は1つ。** その tick の `fallback_exception` は協調の失敗の1件だけで、detail は
  `air_balance_coordination: <例外の型>: <メッセージ>`（500 字まで。Gate の `controller_gate: …` と同じ形）。
  Gate を呼ばないので、同じ tick に Gate の `fallback_exception` が重なることはない
- **`mode: shadow` の失敗は迂回しない**（0078 §2.6 のまま）。shadow の協調は requested に入らないので、
  その tick も raw baseline を `fallback` として Gate を通し、Learned の提案は通常どおり選ばれうる。
  shadow が Fan を変えないこと（0078 §2.10 の段の独立性）を失敗の時にも守る
- `try` / `except` の範囲は 0078 §2.6 のまま（`AirBalanceCoordinator.apply()` の呼び出しだけ）。
  Gate を呼ばない分岐は、その `except` の後に「`failed` かつ `mode: apply` なら `_select()` を呼ばない」として置く。
  Critical Safety の評価と合成の例外は、いまと同じく捕まえない

### 2.2 迂回した tick で起きること（Gate の失敗と同じ）

| 項目 | 迂回した tick | 根拠 |
|---|---|---|
| `ControlTick.model_gate` | `null` | Gate を呼んでいない |
| `state.active_controller` / `fallback_active` | `fallback` / 真 | `selection` が無い tick の既存の扱い（`_control_state()`） |
| `state.fallback_reason` | ML を使えたはずの tick（`AUTO`・`SHADOW` でない stage・`NORMAL`。0028 §2.5 (c)）だけ **`air_balance_coordination_failed`**、ほかは `null` | `ControlState` の既存の検証（ML を使えた tick に Fallback の理由を要る）に合わせる。Gate の失敗の `controller_gate_unavailable` と分け、trace で原因を取り違えない |
| `ZoneRecord.controller_reason` | raw baseline の reason のまま（協調の reason を付けない） | 協調の出力は使っていない |
| 0053 の shadow の記録（`ControlTick.shadow`） | `null` | `selection` が無い tick は記録しない（いまの `_shadow_record()`）。比べる Gate の選択が無い |
| Gate の内部状態（直前の requested・単調時刻・遷移の窓） | 更新しない | Gate を呼ばない。次 tick は `EMERGENCY` で全 zone Max になり、Gate は Safety の `EMERGENCY` を受けて Fallback を選ぶ |
| `fallback_transition_floor` | 掛からない | Gate の `_prevent_transition_drop()` を通らない。直前に Learned MPC が握っていた zone も、requested は raw baseline まで下がりうる（§3） |
| authority の観測（`_observe_authority()`） | `selection` 無しとして観測（降格の推奨なし） | Gate の失敗と同じ。昇格の経路は無い（0057） |

### 2.3 trace の検証（0078 §2.7 の不変条件に足す）

0078 §2.11 の段 3（PR（b））で入れる `ControlTick` の新しい版の検証に、次を加える（新しい版だけ。旧版は書き換えない）。

| 条件 | 内容 |
|---|---|
| `air_balance_coordination.mode: apply` かつ `status: failed` | `model_gate` が `null`、`shadow` が `null`、`state.active_controller` が `fallback`、各 zone の `ZoneRecord` の requested が `candidate` と**等しい**（`fallback_transition_floor` も協調の reason も無い）。`state.fallback_reason` は ML を使えたはずの tick なら code が `air_balance_coordination_failed`、ほかは `null` |
| `state.fallback_reason` の code が `air_balance_coordination_failed` | 同じ tick の `air_balance_coordination` が `mode: apply` かつ `status: failed` |
| `mode: shadow` かつ `status: failed` | 追加の制約なし（Gate を通るので、Learned MPC が選ばれた tick もありうる） |

- 0078 §2.7 の行「Gate が Fallback を選んだ tick: requested ≥ `output`」は、迂回した tick には当たらない
  （Gate が選んでいない）。迂回した tick は上の1行目で「requested = `candidate`（= `output`）」を検査する
- `air_balance_coordination_failed` は `ControlState.fallback_reason` の code として新しい版で許す値に加える。
  offline 評価（`evaluator.py` の `fallback_reasons` の集計）は code を数えるだけなので変えない

### 2.4 試験（0078 §2.11 の段 3（PR（b））で足す）

0078 §2.10 の「失敗」の項に加え、次を Mock / simulated backend で確かめる（`hardware` マーカーを付けない）。

1. **迂回**: authority を LIMITED / EXPANDED / FULL のそれぞれにし、健全で選ばれるはずの Learned MPC の提案を用意して、
   `mode: apply` で `coordinate()` が例外を投げる偽の model を差し込む。その tick の requested が zone ごとに raw baseline と
   等しく、`state.active_controller` が `fallback`、`model_gate` が `null`、Gate の `select()` が呼ばれていない（spy で確かめる）こと
2. **下げる値の検出でも同じ**: 偽の model が candidate を下回る値を返したとき（`failed`）も 1 と同じであること
3. **fault**: 次 tick の Critical Safety の `external_faults` に `fallback_exception` が1件だけあり、detail が
   `air_balance_coordination:` で始まり、その tick が `EMERGENCY` になること
4. **shadow は迂回しない**: 同じ入力で `mode: shadow` にすると Gate が呼ばれ、Learned MPC が選ばれ、Safety の状態も
   Backend に渡る effective も `mode: off` と tick ごとに一致すること
5. **`fallback_reason`**: `NORMAL`・LIMITED 以上では code が `air_balance_coordination_failed`、
   authority が `SHADOW` の tick と `DEGRADED` の tick では `null` で、どちらも `ControlTick` の検証を通ること
6. **遷移**: 直前の tick で Learned MPC が raw baseline より高い requested を握っていた zone が、迂回した tick に
   raw baseline へ下がる場合、effective の下げが Critical Safety の `ramp_down` で制限されること（合成を迂回しない）
7. **捕まえる範囲**: 迂回の分岐を入れた後も、Critical Safety の評価と合成の例外が loop の外へ出ること
8. **trace の不変条件**: §2.3 の各行の正負の試験（`mode: apply`・`failed` で `model_gate` が非 null、
   requested が `candidate` と違う、`air_balance_coordination_failed` が `failed` でない tick に付く、の各記録が拒まれること）
9. **失敗が続く場合の解除と再試行（§2.6）**: 毎回 `coordinate()` が例外を投げる偽の model で、simulated clock を
   `fault_clear_hold_ms` より長く進める。(a) 失敗した tick の次 tick から `EMERGENCY` になり、その間 `coordinate()` が
   呼ばれない（spy で確かめる）こと、(b) `fault_clear_hold_ms` の後に `fallback_exception` が解除されて
   `NORMAL`（ほかの fault が無いとき）に戻り、その tick で協調が再び呼ばれて失敗し、§2.1 のとおり迂回すること、
   (c) その迂回した tick の effective が直前の Max から `ramp_down` の分しか下がらず、次 tick が再び `EMERGENCY` になること、
   (d) この繰り返しのどの tick でも Learned MPC が選ばれないこと

### 2.5 実装の段階との関係

- 0078 §2.11 の段 2（PR（a）: `fan-policy.yaml` の `air_balance_coordination` と純粋な `AirBalanceCoordinator`。
  loop へは配線しない）には影響しない。`AirBalanceCoordinator` は Gate を知らず、失敗を例外か `failed` として
  呼び出し側へ返すだけでよい
- 本記録の内容は段 3（PR（b）: loop への配線と `ControlTick` の新しい版）で実装する。**`ControlTick` の版を本記録のために
  別に上げない**（段 3 の版上げに含める）

### 2.6 失敗が続く場合: Max は再起動まで続かず、解除と再試行を繰り返す

本記録は fault の解除の規則を変えない。`fallback_exception` は 0028 §2.5 のとおり、`fault_clear_hold_ms` の間ずっと
観測されなければ解除される（再起動まで解除しないのは `config_invalid` だけ。0028 §2.5 / §2.7）。
協調は 0078 §2.3 の条件で Critical Safety が `NORMAL` か `DEGRADED` の tick だけ呼ばれ、`EMERGENCY` の間は呼ばれない。
したがって `coordinate()` の不具合が続くとき（毎回失敗する入力・不具合が直らない場合）は、次を繰り返す。

```text
tick n      : 協調が failed → §2.1 の迂回（requested = raw baseline。effective は ramp_down で制限）
tick n+1 〜 : fallback_exception → EMERGENCY（全 zone Max）。協調は呼ばれず、fault は新たに観測されない
             … fault_clear_hold_ms の間
解除の tick : fallback_exception が解除され NORMAL / DEGRADED に戻る → 協調を再び呼ぶ → 失敗 → tick n と同じ
```

- **Gate の失敗とはここが違う。** Gate は `EMERGENCY` の tick も毎 tick 呼ばれる（Safety の `EMERGENCY` を受けて
  Fallback を選ぶ）ので、Gate の不具合が続けば `fallback_exception` が毎 tick 観測され、解除されず Max が続く。
  協調の失敗は `EMERGENCY` の間に観測されないので、`fault_clear_hold_ms` ごとに1 tick だけ Max を離れる
- **離れる tick の Fan は Max の近くに留まる。** その tick も合成を迂回しないので、effective は直前の Max から
  `ramp_down_per_s × tick の経過時間` までしか下がらず、次 tick で再び Max になる。Learned MPC はその tick も選ばれない（§2.1）
- **原因は trace で読める。** 繰り返しのたびに `air_balance_coordination` の `status: failed` の tick と
  `fallback_exception`（detail `air_balance_coordination: …`）が1件ずつ記録される
- 0078 §3 の「`apply` の協調の不具合1つで全 zone Max になり、再起動まで騒音が続く」「正しい設定で再起動するか
  `mode: off` に戻して再起動するまで」は、この繰り返しを含めて読む（Max からほとんど下がらない状態が、
  人が `mode: off` にして再起動するまで続く。厳密に連続した Max ではない）。0078 自体は書き換えない
- 再起動まで解除しない fault（`config_invalid` と同じ扱い）にするかは本記録で決めない（§5）

## 3. Consequences

### 良くなること

- 決定論的な層（Baseline の協調）が壊れたと分かっている tick に、Learned MPC が Fan を握らない
- 協調の失敗と Gate の失敗が、その tick の requested の作り方・次 tick の `EMERGENCY` まで同じ扱いになり、
  0028 §2.7 の「決定論的な層の例外」の扱いが1通りになる
- trace の `fallback_reason` で、Gate の失敗（`controller_gate_unavailable`）と協調の失敗（`air_balance_coordination_failed`）を
  読み分けられる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 迂回した tick は Gate の `fallback_transition_floor` が掛からず、直前に Learned MPC が高く回していた zone の requested が raw baseline まで下がりうる | effective の下げは Critical Safety の `ramp_down` が制限する（合成は迂回しない）。次 tick は `EMERGENCY` で全 zone Max になる。Gate の失敗の tick といまも同じ挙動である |
| Gate の内部状態が1 tick 分更新されない | 次 tick は Safety が `EMERGENCY` で全 zone Max になり、Gate は Fallback を選ぶ。`fault_clear_hold_ms` の後に解除されて戻る tick の Gate は、Max の間に Fallback を選び続けた状態から再開する（§2.6） |
| 不具合が続くと、Max が連続せず `fault_clear_hold_ms` ごとに1 tick だけ迂回の tick（requested = raw baseline）が入る（§2.6。Gate の失敗は解除されず Max が続くのと違う） | 迂回の tick も合成を通るので effective は `ramp_down` の分しか下がらず、次 tick で Max に戻る。Learned MPC はどの tick でも選ばれない。繰り返しは trace の `failed` と `fallback_exception` で見える。止めるのは人が `mode: off` にして再起動（0078 §2.8）。試験は §2.4 の 9 |
| `ControlState.fallback_reason` の許す code が1つ増える | 段 3 の `ControlTick` の版上げに含め、別の版上げを作らない。offline 評価は code を数えるだけ |
| 迂回した tick は 0053 の shadow の記録が無い | 協調の失敗は `fallback_exception` の fault で、その tick は比較の対象として意味が薄い。Gate の失敗の tick と同じ |

## 4. 却下した代替案

| 案 | 利点 | 却下理由 |
|---|---|---|
| A. 0078 のまま、raw baseline を持って Gate を通す | Gate の状態と遷移の floor が途切れない | 決定論的な層が壊れた tick に Learned MPC が選ばれうる（Codex の P2）。Gate の失敗の扱いと揃わない。所有者は推奨案（迂回）を採った（2026-10-01） |
| B. Gate を通すが、Learned を `unavailable` として渡し Fallback を強制する | Gate の遷移の floor と状態が保たれる | Gate が Air Balance の失敗を Learned の不調（`FallbackCause`）として記録し、原因を取り違える。Gate の入力の意味を協調の失敗のために変えることになり、役割が混ざる（AGENTS.md ルール5） |
| C. その tick から全 zone Max を requested にする | 失敗の tick から最も安全側 | 0028 §2.7 は決定論的な層の例外を「その tick は Fallback の値、次 tick から Safety が `EMERGENCY`」と決めており、Max は Safety の裁定で出す。requested を Max にする経路を協調のために作ると、Safety を通らない Max の出し方が増える |
| D. 前 tick の coordinated baseline を使う | 協調の効果がその tick も続く | 失敗した部品の古い出力に頼る。0078 §2.4 は失敗の tick で保持を即座に解くと決めている |
| E. `fallback_reason` は Gate の失敗と同じ `controller_gate_unavailable` にする | 許す code が増えない | trace から Gate の不具合か協調の不具合かを読み分けられない。協調の塊の `status: failed` と照合しないと原因が分からない |

## 5. 未決事項

| 論点 | どこで決めるか | 実測待ちか |
|---|---|---|
| `ControlTick` の版番号（段 3 の版上げに含める） | 段 3（PR（b））の実装の時点で次の空いた番号 | いいえ |
| 協調の失敗の `fallback_exception` を再起動まで解除しない fault（`config_invalid` と同じ扱い）にするか（§2.6） | 所有者の判断。決めるなら 0028 §2.5 の解除の例外を足す新しい決定記録で | いいえ |
| 協調の失敗を `airflow.html` に表示するか | 0078 §5 の「協調の状態を `airflow.html` に表示するか」と一緒に #106 の後続で | いいえ |
