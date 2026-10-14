# 決定記録 0133: Workload Hint Stage B — ヒントを RulePolicy の context 選択の prior として使う経路・段階・失効

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-14
- **Supersedes**: [0064](0064-workload-hint-entry-and-supervisor-prior.md) §2.10 の Stage A の「**制御デーモンは読まない**」
  の記述のみ（限定的な置き換え。§2.6 の段階 B0 / B1 で制御デーモンがヒントを**読んで記録する**が、
  `requested_demand` は1ビットも変えない。0064 §2.10 の Stage B の前提 4 項目と 0066 §2.1 (b) / (c) の 2 項目は、
  ヒントで `requested_demand` が変わりうる段階 B2 の前提として**すべて有効のまま**）。
  承認された場合、0064 側へ `Superseded by` を追記する（`docs/decisions/README.md`「追記のみ」）
- **関連**: [0028](0028-fan-control-contracts.md) §2.2 / §2.3 / §2.5 /
  [0030](0030-control-decision-trace-storage.md) / [0036](0036-transient-cpu-gpu-regime.md) /
  [0041](0041-supervisor-proposal-freshness.md) / [0045](0045-local-socket-write-entry.md) /
  [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.5 /
  [0053](0053-control-shadow-mode-and-counterfactual-logging.md) /
  [0054](0054-offline-evaluation-attribution-and-gates.md) / [0057](0057-authority-rollout-stage-changes.md) /
  [0058](0058-rl-supervisor-training-environment.md) §2.1 / [0060](0060-control-loop-runtime.md) /
  [0061](0061-rl-supervisor-policy-artifact-and-binding.md) §2.4 /
  [0062](0062-model-registry-operations.md) / [0064](0064-workload-hint-entry-and-supervisor-prior.md) /
  [0066](0066-workload-hint-stage-b-conditions.md) / [0072](0072-control-admin-entry.md) §2.6 /
  [0074](0074-supervisor-shadow-wiring-and-episode-evaluation.md) /
  [0077](0077-learned-proposal-handoff.md) §2.3 / §2.5 / [0098](0098-registry-binding-settled-points.md) /
  AGENTS.md「絶対に守るルール」1・2・3・4・5・9、「制御アーキテクチャの固定前提」
- **対象 Issue**: #288（#107 の Stage B。依存 #82 / #84 / #88）

---

## 1. Context

#288 は、#107 の Stage A（ヒントを `events` に記録するだけ。PR #171）の続きとして、Workload Hint を
Supervisor の prior に使う段階である。原則（Hint = prior、Telemetry = reality。Fan Demand・Reactive Guard・
Critical Safety に影響させない。LLM / AI 層から書けない）は #107 と同じで、本記録でも変えない。

Stage B の骨格は既に FINAL の記録で決まっている。本記録はそれを**繰り返さず**、前提として使う。

| 決まっていること | 記録 |
|---|---|
| ヒントは `WorkloadRegimeEstimator` に入れない。`SupervisorInput` の独立したフィールドにだけ入る | 0064 §2.5 |
| 変えられるのは `rule_policy.contexts` の**選択**だけ。鍵は (Telemetry 由来の Regime, `workload`) の対応表 `context_overrides` | 0064 §2.8 |
| 上書き先は基準に対し `target_band` の上下端を**下げる（等しい）ことだけ**。weight と strategy は基準と等しい | 0066 §2.1 (a) |
| 実際の Optimizer と Thermal Model artifact での網羅的な単調性試験・全入力と実行環境を束ねたハッシュ・認証領域の外では使わない・領域内でも二重解法で毎回確かめる | 0066 §2.1 (b)〜(d) |
| 期限・矛盾の保持は `CLOCK_BOOTTIME`、行の順序は `events.id`、終端状態は制御デーモン自身の表に永続化、読めなければ使わない | 0066 §2.2 |
| 矛盾（計算負荷のヒントなのに Regime が `IDLE` / `COOLDOWN` のまま保持時間以上）で `contradicted`。再採用しない | 0064 §2.7 |
| trace に閉じた語彙の塊（`workload` / `status` / `source_label` / `event_id` / `age_ms` / `influence`） | 0064 §2.9 / 0066 §2.2 |
| 既定は無効。設定で戻せる | 0064 §2.10 |

一方、0064 / 0066 を書いた後に、制御の形が次のように具体化した。これらと Stage B の結び目は
まだどこにも書かれていない。

1. **Learned MPC は `coldaisle-fand` の外の worker で動く**（0077）。worker の入力は毎 tick の
   `LearnedFrame` だけで、そこに載る Supervisor 出力は**選ばれた1つ**（`LearnedFrame.supervisor`）である。
   0066 §2.1 (d) の二重解法（ヒント無しの基準とヒント有りを両方解く）には、worker が**両方の context**を
   受け取る必要があるが、いまの frame はそれを運べない
2. **`SupervisorOutput` を読むのは Learned MPC だけ**である。Fallback（Baseline）は Supervisor の
   context を読まない。したがってヒントが実 Fan に届く道は「RulePolicy の context → MPC のコスト →
   MPC の提案 → Model Confidence / OOD Gate → authority の上限 → Reactive Guard → Critical Safety」の
   1本しかなく、**Learned MPC の authority が `shadow` の間はヒントは実 Fan に1ビットも効かない**
3. **Authority Stage**（`shadow` / `limited` / `expanded` / `full`。0057）と人だけが上げる入口
   （`coldaisle-authority raise`。0086）がある
4. **RL Supervisor の active slot は閉じている**（0061 §2.4）。RL worker は shadow 用の出力だけを返す
5. 0064 §5 の未決 #6（RL artifact の「ヒントを使う」宣言の形）と、0066 §5 の未決 #3〜#5
   （ハッシュの置き場所・二重解法の成功率・Registry との結び付け）が残っている

いまの世界の状態も確認しておく（2026-10-14）。

- `coldaisle-fand` は導入先で disabled のまま、0121 の実機試験の途中である
- RulePolicy の `contexts` / `output_bounds` は本番値として承認されていない（0064 §2.10 の前提 2 が未達）
- 0066 §2.2 の `events` の migration（`boot_id` / `boottime_ns`）・終端状態の表・run の記録は未実装
- Stage A の証拠（「Regime が追いつかなかった tick」の割合）はまだ集計されていない

したがって**本記録を承認しても、いますぐ着手できる段階は無い。** 本記録の目的は、前提が揃ったときに
実装 Issue を切れるよう、上の結び目と段階を先に決めておくことである。

---

## 2. Decision

### 2.1 比較した案

| 案 | 内容 | 評価 |
|---|---|---|
| **A（推奨）** | ヒントは **RulePolicy の context 選択だけ**に効かせる（0064 §2.8 / 0066 の経路のまま）。ヒント有りの context は「2 つ目の Supervisor 出力」として frame で MPC worker へ渡し、二重解法の結果を fand が再検査して選ぶ。**3 段階（B0 記録 → B1 MPC の shadow → B2 active）**で解放し、B2 の実効は Learned MPC の authority に従う | 既決の記録と矛盾しない。Safety / Guard / Fallback は構造的に無関係のまま。段階ごとに「何が変わりうるか」が1つずつ増える |
| B | A と同じ経路で、段階を分けず、0064 / 0066 の前提がすべて揃ってから一度に active まで作る | 記録の置き換えは要らない。ただし前提 3（Stage A の証拠）と 0066 §5 #4（二重解法の成功率）を測る道具が Stage B の実装そのものになり、**測るために効かせる**順序になる |
| C | ヒントを RLPolicy の observation に入れ、RL がヒントの使い方を学ぶ | RL の active は閉じている（0061 §2.4）。ヒントの無い期間の trajectory で評価できない（0064 §2.10 / 0054）。冷却の単調性を構成で保証できない（0066 §2.1 (a) は context の差でしか書けない） |
| D | ヒントの確からしさを連続の重みにし、MPC のコストの band / weight を連続的に混ぜる（期限に向けて減衰させる等） | weight を動かせない（0066 §2.1 (a)）。連続量の混合は認証領域を連続に広げ、ハッシュと格子で束ねられない |
| E | ヒントを Fallback（Baseline）の曲線や Reactive Guard の floor に効かせる（先行冷却） | Fallback は ML 停止時の退避先であり、外部の自己申告に依存させると「退避先が書き手を信じる」ことになる。Guard / Safety へは 0064 §2.8 / ルール 2・3 で禁止済み |

**案 A を採る。** 以下はその詳細である。B / C / D / E の却下理由は §4 にまとめる。

### 2.2 効かせる先は RulePolicy だけ。RLPolicy には渡さない

- ヒントを読むのは `SupervisorCoordinator` の **active slot の RulePolicy** だけとする
- **RLPolicy（shadow）にはヒントを渡さない。** RL worker へ送る frame では、本記録で足す欄（§2.5）を
  常に `None` にする（伏せる。0064 §2.10 の「宣言の無い artifact にはフィールドを伏せて渡す」の具体化）。
  RL 学習環境（0058）の observation もヒント無しのまま変えない
- 0064 §5 の未決 #6（RL artifact の「ヒントを使う」宣言の形）は**本記録では決めない**。
  RL の active slot が開く記録（0061 §2.4 の続き）と同時に、別の決定記録で決める
- 0074 の Supervisor の shadow 集計は、ヒントで context が変わった tick（`influence: context_override`）と
  変わらなかった tick を**別の層として数える**。ヒントを見ない RL と、ヒントで band を下げた Rule を
  同じ母集団で比べない（0054 の帰属の規律）

### 2.3 ヒントが変えるもの・変えないもの（「どの重み」への答え）

- **重みは変えない。** ヒントが変えられるのは `target_band` を下げる向きだけ（0066 §2.1 (a)）。
  strategy・weights（温度・balance・acoustic・change）は基準の context と等しい
- **ヒントの強さを連続量にしない。** 効くか効かないかの2値で、効くときの大きさは
  `context_overrides` が指す context の band の差そのものである。ヒントの `age_ms` や
  `expected_duration_s` の残りで band を補間しない（案 D の却下）
- **Regime も `regime_confidence` も書き換えない。** ヒント有りの出力の `regime` / `regime_confidence` は
  推定器の値のままにする。ヒントで「確からしく見せる」ことをしない（ルール 4）
- **`UNKNOWN` には効かせない。** `context_overrides` の鍵に `regime: unknown` を置いた設定は読み込みで
  拒む（0066 §2.1 (a) の違反と同じく、制御デーモンを起動しない）。未知の状態をヒントで既知の扱いに
  しないため（ルール 4）
- `IDLE` / `COOLDOWN` を鍵に置くことは許す（ジョブの立ち上がりで負荷が Telemetry に現れる前に
  band を下げる＝先行冷却が、ヒントのいちばんの使い道であるため）。ただし 0064 §2.7 の矛盾の保持時間
  `contradiction_hold_ms` を過ぎれば `contradicted` になり効かなくなるので、`IDLE` への効き目は
  保持時間で上が抑えられる。**既定の設定には `IDLE` / `COOLDOWN` の鍵を置かない**（所有者の判断事項 2）
- 選ばれた context は、いまと同じく `supervisor.output_bounds` の検証を通る

### 2.4 期限・失効・Telemetry との食い違い

0064 §2.6 / §2.7 と 0066 §2.2 をそのまま使い、次だけを足す。

- **失効の事由を閉じた集合にする**: `expired`（`CLOCK_BOOTTIME` で数えた残り時間が 0 以下）/
  `ended`（`phase: end`）/ `superseded`（新しいヒント）/ `contradicted`（§2.7 の矛盾）/
  `unsupported_version`。**失効は一方向**で、どの事由でも同じ `event_id` は再び `active` にならない
- **設定・認証の不一致は失効ではなく「使わない」**。`stage` が下がった・ハッシュが合わない・
  認証領域の外・二重解法で単調性が破れた、はヒントの状態を変えず、その tick だけ使わない
  （`hint_prior_skipped` の閉じた語彙。§2.8）。ヒントの状態と「その tick で使ったか」を混ぜない
- **矛盾は Regime の大分類だけで判定する**（0064 §2.7 のまま）。`training` のヒントで Regime が
  `SUSTAINED_CPU` でも矛盾にしない（v1 のヒントは CPU / GPU の軸を持たない）。新しい矛盾の種類は
  `hint_v` を上げる記録で足す
- **ヒント有りの context は Telemetry を上書きしない。** Supervisor の出力の `regime` は推定器の値の
  ままなので、0041 の提案鮮度（Regime の一致）も Telemetry 由来の Regime で判定され続ける

### 2.5 frame と MPC worker（0077 との結び目）

- `LearnedFrame` に欄を1つ足し、schema version を上げる（値は実装時の次の版）:
  `supervisor_hint: SupervisorOutput | None` — その tick のヒント有りの context による Supervisor 出力。
  `supervisor`（基準。いまの欄）と**同じ tick・同じ regime・同じ policy の版**で、`target_band` だけが
  違ってよい。fand は frame を作るときにこの関係を検査し、満たさなければ `None` にする
- `supervisor_hint` を入れるのは、§2.6 の段階が **B1 以上**で、ヒントが `active`・対応表に鍵があり・
  ヒント有りの context が基準と違う tick だけ。それ以外は `None`
- **MPC worker は 0066 §2.1 (d) のとおり二重解法を行う。** 先に基準を解き、残りの `mpc.budget_ms` と
  `mpc.optimizer.max_evaluations` でヒント有りを解く。予算は増やさない
- **基準の解を先に公開する。** worker は基準の solve が終わった時点で、その結果を**いまと同じ `MpcProposal`**
  として経路へ送る（ヒント有りの solve を待たない）。ヒント有りの plan（解けたとき）と、解かなかった・
  使えない理由（閉じた語彙）は、その後に**別のメッセージ**（同じ `run_id` と元 snapshot に束縛した
  Shadow 用の記録）として送る。したがって fand が基準の解を受け取る時刻は、ヒントを扱わない worker と
  変わらない。ヒント有りの solve が遅れても・失敗しても、基準の `MpcProposal` の経路には何も起きない
- **B1 は `requested_demand` の値にも時刻にも影響しない。** B1 では fand はヒント有りの記録を Shadow の台帳へ
  渡すだけで、受け渡し口（`MpcProposal` の slot）にも Gate にも入れない。基準の `MpcProposal` が受け渡し口に
  届く時刻・内容・その後の Gate / Guard / Safety の結果は、`stage: off` の場合と同じである
- 試験で固定する（Mock の worker と simulated backend。ルール 7）:
  - 同じ frame の列に対し、`stage: off` と `stage: shadow_mpc` で、fand が受け取る基準の `MpcProposal` の
    内容と受信の単調時刻、各 tick の `requested_demand` / `effective_demand` が一致すること
  - ヒント有りの solve を人工的に遅らせた・例外にした・予算を使い切らせた場合も、基準の `MpcProposal` の
    送信時刻（worker 側の単調時計）と fand の受信時刻が、ヒントを扱わない場合と変わらないこと
  - worker の中で、基準の `MpcProposal` の送信がヒント有りの solve の開始より先に起きること
    （呼び出し順の記録で確かめる）
  - B1 で、ヒント有りの記録が `MpcProposal` の slot へ入らず、Shadow の台帳にだけ現れること
- **fand が再検査する。** 0066 §2.1 (d) の 2 条件（系統ごとに `demand_hint >= demand_base - eps`、
  かつ両 plan の予測のすべての offset で CPU / GPU が `hint <= base + eps_c`）と認証領域の判定を、
  worker の判定とは別に fand の中で決定論的に行い、両方が通ったときだけヒント有りの plan を
  Gate へ渡す候補にする。worker は ML 側のプロセスであり、Gate と同じく**最終の判定を ML 側に置かない**
  ため（ルール 3・4 の精神。判定は安価な比較だけで、モデルの評価はしない）
- 提案の鮮度・`run_id` の束縛・`expected_artifacts` の照合（0041 / 0077 / 0098）は基準の plan と
  同じ規則でヒント有りの plan にも当てる。どれかが外れれば両方とも使わない

### 2.6 段階（shadow から始める）

段階は `config/fan-policy.yaml` の `supervisor.workload_hint.stage` で持つ。値は
`off` / `observe`（B0）/ `shadow_mpc`（B1）/ `active`（B2）。既定は `off`。
**設定は上限であり、実効の段階はさらに下の条件で下がる。** 設定を下げれば次の起動から戻る。

| 段階 | 制御デーモンがすること | `requested_demand` への影響 | 前提 |
|---|---|---|---|
| `off` | ヒントを読まない（Stage A のまま） | なし | — |
| **B0 `observe`** | ヒントを読み、状態（`active` / `expired` / …）を進め、終端状態を永続化し、「効かせていたら選んだ context」を trace に残す（`influence: would_override`）。frame には載せない | **なし** | 0064 前提 1（#82）・前提 4（所有者の承認）・0066 §2.2 の実装 |
| **B1 `shadow_mpc`** | B0 に加え、frame の `supervisor_hint` を埋め、worker に二重解法をさせる。基準の解は先に `MpcProposal` として届き（§2.5）、ヒント有りの plan は後から **Shadow の台帳（0053 / #90）へ「適用しなかった提案」として残すだけ**で、Gate へは基準の plan だけを渡す | **なし**（値も時刻も。§2.5） | B0 の前提 ＋ 0064 前提 2（RulePolicy の本番値）＋ 0066 §2.1 (b) / (c) の照合済みハッシュ |
| **B2 `active`** | §2.5 の再検査を通ったヒント有りの plan を、基準の plan の代わりに Gate へ渡す | Learned MPC の authority の範囲で**ありうる** | 0064 前提 1〜4 と 0066 §2.1 (b) / (c) のすべて（#288 の着手条件 1〜6） |

- **B0 / B1 は 0064 の Stage B の前提を緩めるものではない。** どちらも `requested_demand` を1ビットも
  変えない「Stage A の延長」で、0064 前提 3（Regime が追いつかなかった tick の割合）と 0066 §5 #4
  （二重解法の成功率）を**効かせずに測る道具**である。0064 §2.10 の Stage A の「制御デーモンは読まない」
  だけを置き換える（Supersedes 欄）
- **B2 の実効は Learned MPC の authority に従う。** authority が `shadow` の間、Gate の先へ届くのは
  Fallback であり、ヒントは実 Fan に効かない。`coldaisle-control rollback-authority`（0072 §2.6）や
  自動降格（0057）で authority が下がれば、ヒントの効き目も同じ tick で下がる。
  **ヒント専用の昇格の入口・journal は作らない**（制御権の段階は authority の1本に集める）
- B1 / B2 を設定で有効にしていても、認証のハッシュが無い・合わない（0066 §2.1 (c)）ときは、実効の段階を
  **B0（記録だけ）に留めて**起動する。0066 §2.1 (c) の「一致しない、または記録が無い場合は Stage B を無効のまま
  （ヒントは記録のみ）」と同じ安全側に揃え、未認証の組み合わせではヒント有りの solve を走らせない。
  ハッシュ不一致で起動を拒否しない（0066 §2.1 (c) のまま）
- B0 に入る前に、Stage A の記録（`events` × decision trace の `ts_ms` による突き合わせ）を一度オフラインで
  集計し、所有者が「続ける価値がある」と判断すること。ここでは数値の閾値は置かない（0064 §2.10 の
  「測る前に数字を決めない」）

### 2.7 Safety / Guard / Fallback からの独立

- ヒントが届くのは RulePolicy の context 選択と、それを読む Learned MPC のコストだけである。
  Model Confidence / OOD Gate・authority の上限・Reactive Guard・Critical Safety・Fallback・
  Fan Hardware Mapping は**ヒントの型を受け取らない**
- 試験で固定する: `coldaisle.control.safety` / `coldaisle.control.reactive` / `coldaisle.control.fallback` /
  `coldaisle.control.hardware` がヒントの型（`SupervisorInput.workload_hint` の型と frame の欄）を
  import しないこと。ヒントの有無・内容を変えても、同じ入力から Guard / Safety / Fallback の出力が
  1ビットも変わらないこと
- `effective_demand` は引き続き Critical Safety が最終裁定する。ヒント有りの plan が選ばれても、
  Safety の floor・emergency Max・manual override はそのまま効く（ルール 2・3）
- ヒントの経路から Fan Demand・PWM・設定・較正を変えられないことは、0064 §2.2 の import 走査を
  そのまま保つ（#107 の受け入れ基準の3つ目）

### 2.8 ML 停止・読めないときの扱い

ヒントは任意の機能であり、どの失敗でも**ヒント無しの既存経路で運転を続ける**。どれも MAX にしない。

| 事象 | 扱い | trace / ログの語彙 |
|---|---|---|
| Learned MPC worker の停止・timeout・モデル読込の失敗・Gate の退避 | 既存のとおり Fallback で運転。Fallback はヒントを読まないので、ヒントの効き目は自動的に 0 | `hint_prior_skipped: mpc_unavailable` |
| 二重解法で予算が尽きた・ヒント有りが解けない | 基準の plan を使う | `budget_exhausted`（0066） |
| 認証領域の外・単調性の再検査が不合格 | 基準の plan を使う | `out_of_certified_domain` / `monotonicity_violated`（0066） |
| ハッシュの不一致・記録が無い | 実効の段階を B0（記録だけ）に留める（0066 §2.1 (c)） | `certification_mismatch` |
| ヒントの状態を読めない（`events`・終端状態の表など） | ヒントを使わず記録のみ | `hint_state_unreadable`（0066） |
| Supervisor の評価の失敗 | Supervisor 出力が無いので MPC も基準も無い（既存のとおり） | 既存の `supervisor_unavailable` |
| authority が `shadow` | ヒント有りの plan は Gate の先へ届かない | `authority_shadow` |

- **ヒントの読み出しで tick を待たせない。** `events` と終端状態の表を読むのは制御デーモン（合成の起点）の
  別スレッドとし、検証済みの不変の値を受け渡し口に置く（`operating_mode.py` と同じ形）。tick は先頭で
  受け渡し口を読むだけで DB を開かない。読み手のスレッドが死んだ・最後に読めた時刻が古い場合は
  `hint_state_unreadable` に倒す（管理ソケットと違い MAX にはしない。ヒントは安全の入力ではないため）
- `coldaisle.control` は引き続き `coldaisle.store.db` を import しない（0064 §2.5）

### 2.9 trace

0064 §2.9 の塊（0066 §2.2 の `age_ms`）に次を足す。いずれも閉じた語彙。

| 項目 | 値域 |
|---|---|
| `stage` | `off` / `observe` / `shadow_mpc` / `active`（その tick の**実効**の段階） |
| `influence` | 0064 の `none` / `context_override` に `would_override`（B0 / B1 で、効かせていれば選択が変わった）を足す |
| `skipped` | §2.8 の `hint_prior_skipped` の語彙。使った tick と効かない tick は `null` |

`ControlTick` の schema version を1つ上げ、古い version の trace にこの塊を持たせない（0064 §2.9 の規律）。
`note`・生の `source`・`peer_uid` は載せない（0064 §2.9 のまま）。

---

## 3. Consequences

### 良くなること

- ヒントが実 Fan へ届く道が「RulePolicy の band → MPC → Gate → authority → Guard → Safety」の1本だけで、
  その強さは authority の1本で上下する。ヒント専用の制御権が生まれない
- B0 / B1 で、`requested_demand` を（値も時刻も）変えずに「Regime が追いつかなかった tick」と「二重解法の成功率・差分」を
  測れる。0064 前提 3 と 0066 §5 #4 を**効かせる前に**答えられる
- 二重解法の最終判定を fand が持つので、ML 側のプロセスの誤りでヒント有りの plan が通ることがない
- ML がどこで止まっても、ヒントの効き目は自動的に 0 になる（Fallback がヒントを読まないため）

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| frame・worker の提案・trace の3つの schema が変わる | いずれも版を上げ、古い版にヒントの塊を持たせない。B0 は frame を変えないので、frame の変更は B1 の実装 Issue に閉じる |
| 二重解法で MPC worker の計算が増える | 予算は増やさない（0066 §2.1 (d)）。基準を先に解いて先に公開するので、基準の結果の到着は遅れない（§2.5 の試験で固定）。成功率は B1 で測り、低ければ B2 を使わない（人が決める） |
| B1 にも認証のハッシュが要るので、二重解法の成功率を測れるのは認証の後になる | 0066 §2.1 (b) の認証ランナー自体が実際の Optimizer と予算で二重解法を回すので、その結果で一次の見積もりは得られる。安全側（0066 §2.1 (c)）を優先する |
| B0 で制御デーモンが DB を読むスレッドを持つ | 読み出しは tick の外。失敗はヒント無しに倒すだけで、制御は止めない |
| `IDLE` の鍵を許すと、ジョブを起動しない嘘のヒントで保持時間ぶん騒音が増える | 冷却を弱めないので温度は上がらない（0066）。保持時間で上が抑えられ、矛盾は trace に数えられる。既定の設定には置かない |
| RL はヒントを使えないまま | RL の active が閉じている間は効果が無い。宣言の形は RL の active を開く記録で決める |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| **段階を分けず、前提がすべて揃ってから一度に active まで作る（案 B）** | 前提 3（Regime の遅れ）と二重解法の成功率を測る道具が Stage B の実装そのものになり、「測るために効かせる」順序になる。B0 / B1 は効かせずに測れる |
| **ヒントを RLPolicy の observation に入れる（案 C）** | RL の active は閉じている（0061 §2.4）。ヒントの無い期間の trajectory で評価できない（0054）。単調性を構成で保証できない |
| **ヒントの確からしさを連続の重みにして band / weight を混ぜる（案 D）** | weight を動かせない（0066 §2.1 (a)）。連続量の混合は認証領域と格子で束ねられない |
| **ヒントを Fallback の曲線・Reactive Guard の floor に効かせる（案 E）** | 退避先が外部の自己申告に依存する。Guard / Safety へはルール 2・3 と 0064 §2.8 で禁止済み |
| **MPC worker の二重解法の判定をそのまま信じる** | 最終判定を ML 側のプロセスに置くことになる。比較は安価なので fand で再検査する |
| **ヒント専用の Authority / 昇格の入口を作る** | 制御権の段階が2本になり、片方だけ下げ忘れる状態が生まれる。B2 の実効は Learned MPC の authority で上下させれば足りる |
| **ハッシュ不一致のとき B1 に下げて二重解法を測り続ける** | 0066 §2.1 (c) は不一致・記録無しで Stage B を無効（記録のみ）にすると定めている。未認証の組み合わせで MPC worker の挙動を変える段階を動かさない。B0 に留める |
| **B1 で二重解法の両方の結果を1つの提案にまとめて送る** | ヒント有りの solve の分だけ基準の解の到着が遅れ、B1 が `requested_demand` の時刻に影響する。基準を先に公開し、ヒント有りは別の Shadow 用の記録にする（§2.5） |
| **ヒントで `regime_confidence` を上げる・`UNKNOWN` を別の Regime の context に写す** | 未知の状態を自己申告で既知に見せる（ルール 4）。0041 の鮮度判定と 0054 の帰属も壊れる |
| **ヒントの残り時間（`expected_duration_s`）に応じて band を補間する** | 申告を時間軸の真値として扱う（AGENTS.md「負荷があと何時間続くかを断定しない」）。期限の算出にだけ使う（0064 §2.3） |
| **tick の中で `events` を読む** | DB のロックで tick の期限を越えうる。受け渡し口の形（`operating_mode.py`）で tick の外に出す |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | B2 へ進む Stage A / B0 の証拠の閾値と観測期間（0064 §5 #1） | B0 の実測を見てから、B2 の実装 Issue で記録する |
| 2 | `context_overrides` の具体値・`contradiction_hold_ms`・TTL の本番値（0064 §5 #3） | `config/fan-policy.yaml`。設定であり記録には書かない |
| 3 | 単調性ゲートのハッシュの置き場所と、Thermal Model の昇格との結び付け（0066 §5 #3 / #5）。推奨は Model Registry の lifecycle（0062）に、Thermal Model の production と組で置くこと | B2 の実装 Issue の前に別の決定記録 |
| 4 | RL artifact の「ヒントを使う」宣言の形（0064 §5 #6） | RL の active slot を開く記録 |
| 5 | B0 の受け渡し口で「最後に読めた時刻が古い」とみなす上限 | B0 の実装 Issue（設定値） |
| 6 | frame・worker の提案・trace の schema の具体の版番号と欄の型 | 各段階の実装 Issue |

### 実装 Issue の切り方（参考。着手は前提が揃ってから）

1. 0066 §2.2 の migration・終端状態の表・run の記録（B0 の前提）
2. B0: 受け渡し口と読み手のスレッド・`SupervisorInput.workload_hint`・状態の遷移・trace の塊
3. 0066 §2.1 (b) の認証ランナーとハッシュ（未決 3 の後。B1 の前提）
4. B1: frame の `supervisor_hint`・worker の二重解法（基準を先に公開・ヒント有りは別の Shadow 用の記録）・fand の再検査・Shadow の台帳への記録
5. B2: Gate へ渡す候補の切り替え（前提 1〜6 がすべて揃ってから）
