# 決定記録 0149: Workload Hint の `skipped` の語彙に `rule_policy_not_active` を足す

- **種別**: Decision Record
- **Status**: FINAL（2026-10-16、リポジトリ所有者の承認）
- **Date**: 2026-10-16
- **Supersedes**: なし（[0133](0133-workload-hint-stage-b-prior-rollout.md) §2.8 の表に1行を足して補う。
  0133 の本文は書き換えず、置き換える決定も無い）
- **関連**: [0133](0133-workload-hint-stage-b-prior-rollout.md) §2.2 / §2.6 / §2.8 / §2.9 /
  [0064](0064-workload-hint-entry-and-supervisor-prior.md) §2.9 /
  [0054](0054-offline-evaluation-attribution-and-gates.md) /
  [0061](0061-rl-supervisor-policy-artifact-and-binding.md) §2.4 /
  `docs/control-config.md`（`supervisor.workload_hint`）
- **対象 Issue**: #319（PR #332）

## 1. Context

0133 §2.2 は、ヒントを読むのを `SupervisorCoordinator` の **active slot の RulePolicy** だけに限っている。
B0 `observe` では、その RulePolicy が「ヒントを効かせていたら選んだ context」と実際の選択を比べ、
変わるなら trace に `influence: would_override` を残す（0133 §2.6 / §2.9）。

PR #332（#319、B0 の実装）のレビュー（Codex）で、設定の `active_policy` が `rl_policy` の構成で、
RL の提案が無く RulePolicy へ fallback した tick でも `would_override` を比べてしまう点が指摘された。
fallback の RulePolicy は active slot ではなく、0133 §2.2 の「ヒントを効かせうる経路」ではない。
そこで実装は、設定の `active_policy` が `rule_policy` のときだけ比べ、それ以外の構成では比べずに
`skipped: rule_policy_not_active` を記録するように直した（`control/supervisor/policy.py` の
`SupervisorCoordinator.rule_policy_active` / `would_override`、`control/supervisor/workload_hint.py` の
`WorkloadHintTracker.record`、`control/schema.py` の `HintPriorSkipped.RULE_POLICY_NOT_ACTIVE`）。

ところが `skipped` の値域は 0133 §2.9 で「§2.8 の `hint_prior_skipped` の語彙」と閉じており、§2.8 の表に
この語は無い。0133 は FINAL で書き換えられないため、いまは `docs/control-config.md` にだけ書かれている。
語彙の正本を決定記録に置くため、本記録で足す。

## 2. Decision

### 2.1 0133 §2.8 の表に1行を足す

| 事象 | 扱い | trace / ログの語彙 |
|---|---|---|
| 設定の `supervisor.active_policy` が `rule_policy` でない（`rl_policy` が active。RL の提案が無く RulePolicy へ fallback した tick を含む） | ヒントを効かせうる経路が無いので、`would_override` を判定しない（`influence: none`）。運転は既存のとおり | `rule_policy_not_active` |

- **出る条件**（すべてを満たす tick）: 実効の段階が `off` でない・ヒントの状態を読めている
  （`hint_state_unreadable` / `run_start_not_durable` でない）・その tick に有効なヒント（状態 `active`）がある・
  Supervisor の出力がある（`supervisor_unavailable` でない）・設定の `active_policy` が `rule_policy` でない
- **優先の順**: `hint_state_unreadable` / `run_start_not_durable` → `supervisor_unavailable` →
  `rule_policy_not_active` → `certification_mismatch`。`rule_policy_not_active` の tick では
  `certification_mismatch` を出さない（比べる経路そのものが無いため、段階の留め置きを理由にしない）
- 判定は**設定**（`active_policy`）で行い、その tick に選ばれた出力の種類では行わない。fallback で Rule の出力が
  選ばれた tick も `rule_policy_not_active` とする
- trace の版は上げない。`HintPriorSkipped` は B1 / B2 の値も含めて先に置いた閉じた語彙で、本記録の値は
  `ControlTick` v16（PR #332）で既に出ている。B0 の実装が出す `skipped` は
  `hint_state_unreadable` / `run_start_not_durable` / `certification_mismatch` / `supervisor_unavailable` /
  `rule_policy_not_active` の5つになる

### 2.2 なぜ `would_override` に数えないか

- **0133 §2.2**: ヒントを読むのは active slot の RulePolicy だけ。RL が active の構成で Rule が出すのは
  fallback（0061 §2.4）で、B2 になってもそこへヒントは効かない。効かない経路で「効かせていたら変わった」と
  数えるのは、B2 で起きない事象を数えることになる
- **B0 の証拠を汚さない**: B0 / B1 は 0064 前提 3 などを「効かせずに測る道具」（0133 §2.6）であり、
  `would_override` の件数は B2 へ進むかの判断の材料になる。RL の失敗の頻度に応じて fallback の tick が混ざると、
  件数がヒントの性質ではなく RL worker の調子で動く。0133 §2.2 の「ヒントで band を下げた Rule と、ヒントを
  見ない RL を同じ母集団で比べない」（0054 の帰属の規律）と同じ理由で、層を分ける
- `influence: none` かつ `skipped: null` にもしない。それでは「比べて変わらなかった」tick と区別できず、
  分母に混ざる。比べなかったことを語で残す

### 2.3 既存の語との違い

- `supervisor_unavailable`: Supervisor の出力そのものが無い（評価の失敗）。`rule_policy_not_active` は
  出力はある（RL の出力か、fallback の Rule の出力）が、ヒントを読む slot ではない
- `certification_mismatch`: 設定は B1 / B2 だが認証のハッシュが無い・合わないので B0 に留めた。
  active の RulePolicy で比べた結果（`would_override`）は残る。`rule_policy_not_active` では比べない
- `hint_state_unreadable` / `run_start_not_durable`: ヒントの状態そのものが読めない。
  `rule_policy_not_active` ではヒントは読めており、`event_id` / `status` / `age_ms` などの欄は残る
- `authority_shadow`（B2 の語）: ヒント有りの plan が authority のため Gate の先へ届かない。
  `rule_policy_not_active` は plan より前の、context 選択の段で経路が無い

### 2.4 変えないもの

- 0133 の他の節（段階・frame・Shadow solver・`hint_shadow`・Safety / Guard / Fallback からの独立）は変えない
- `requested_demand` への影響は無い（B0 のまま）。RLPolicy にヒントを渡さないこと（0133 §2.2）も変えない

## 3. Consequences

### 良くなること

- trace の `skipped` の語彙の正本が決定記録に揃い、`docs/control-config.md` と `control/schema.py` が
  参照できる
- B0 の `would_override` の件数が、active の RulePolicy で比べた tick だけで数えられる

### 悪くなること・その緩和

- RL が active の構成では、B0 を有効にしてもヒントの効き目を測れない（`rule_policy_not_active` だけが並ぶ）。
  RL の active slot がヒントを使う宣言の形は 0133 §2.2 のとおり別の記録で決める（0064 §5 #6）。
  B0 で測るときは `active_policy: rule_policy` の構成で行う

## 4. 却下した代替案

- **fallback の Rule でも比べる（PR #332 の最初の実装）**: 0133 §2.2 に反し、§2.2 の理由で B0 の証拠を汚す
- **`influence: none` / `skipped: null` で残す**: 比べなかった tick と比べて変わらなかった tick が区別できない
- **既存の `supervisor_unavailable` を流用する**: Supervisor の出力はあり、意味が違う。集計で取り違える
- **0133 を Supersede して §2.8 の表を書き直す**: 足すのは1行で、0133 の決定は何も変わらない。
  置き換えにすると読み手が 0133 の全体を疑う。補う記録で足りる

## 5. 未決事項

なし
