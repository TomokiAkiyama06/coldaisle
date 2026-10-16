# 決定記録 0156: Workload Hint の単調性ゲートの仕様（格子・`eps`・上限）の設定の形

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: なし（[0066](0066-workload-hint-stage-b-conditions.md) §2.1 (b) / (d) と
  [0152](0152-hint-monotonic-gate-certificate.md) §2.3 / §2.4 を置き換えず、設定の**形**を補う。
  値は 0066 §5 の 1 のとおり実測で決め、この記録では決めない）
- **関連**: [0028](0028-fan-control-contracts.md) §2.8（`provisional` / `confirmed`）/
  [0064](0064-workload-hint-entry-and-supervisor-prior.md) §2.8 /
  [0066](0066-workload-hint-stage-b-conditions.md) §2.1 (a)〜(d)・§5 の 1 /
  [0077](0077-learned-proposal-handoff.md) §2.6 /
  [0119](0119-safety-values-first-host.md) /
  [0133](0133-workload-hint-stage-b-prior-rollout.md) §2.5 / §2.6 /
  [0152](0152-hint-monotonic-gate-certificate.md) §2.1 / §2.3 / §2.4 / §2.6 / §5 の 2 /
  `docs/control-config.md` / `src/coldaisle/control/config.py`（`WorkloadHintConfig`・`ConfigValue`）/
  `src/coldaisle/control/mpc/optimizer.py`（`LearnedMpcOptimizer.solve`）/
  `src/coldaisle/control/mpc/plan.py`（`HardConstraintSet`）/
  `src/coldaisle/control/model/thermal.py`（`ObservedThermalInput`）/
  AGENTS.md「絶対に守るルール」2・3・4・7・9
- **対象 Issue**: #288（0152 の段 1b の前提）

---

## 1. Context

0152 §2.3 は、証明書の `certified_domain` / `eps` / `eps_c` / `grid_points` と部品 `hint_gate` を、
「`fan-policy.yaml` の格子の定義（各軸の範囲・刻み）・`eps` / `eps_c` と `safety.yaml` の `max_eps` / `max_eps_c`
だけを入力にした1つの純粋な関数」（ゲートの仕様）から導くと決めた。fand・worker・ランナーが同じ関数を自分の設定に当てる。

ところが main にはこの入力が**スキーマとして無い**（PR #349 の判断点 2）。

- `fan-policy.yaml` の `supervisor.workload_hint`（`WorkloadHintConfig`。v12）は段階・期限・`context_overrides` だけを持つ
- `safety.yaml`（v4）に `workload_hint` の塊は無い。0119 が確定した値にも含まれない
- 0066 §2.1 (b) は格子の軸を言葉で挙げる（Regime・CPU / GPU 温度の4区分・直前 Demand の3区分・workload・
  hard constraints の代表集合・baseline の代表集合・その他の window feature）が、**設定での表し方**は書いていない
- 0066 §2.1 (b) は `max_eps_c` に「有限の天井」を置き、その値を「`safety.yaml` のスキーマに持たせる（コードに定数を
  置かない）」とするが、天井をどこに持つかの形は書いていない
- 0152 §5 の 2 (a) は証明書の metadata の3欄の意味を例示したが、`feature_schema_version` / `target_schema_version` は
  決めていない（PR #349 の判断点 1。この PR では縛らずに残した）

値（刻み・範囲・`eps`）は 0066 §5 の 1 のとおり Stage A の実測で決める。ここでは**形だけ**を決める。形が決まらないと、
ゲートの仕様の関数・証明書の型（0152 段 1b）・ランナー（段 2）・fand の照合の手順 4（段 3）のどれも書けない。

`LearnedMpcOptimizer.solve` の入力は次のとおりで、格子はこれらを覆う必要がある（0066 §2.1 (b)）。

| `solve` の入力 | 中身 | 格子での扱い（§2） |
|---|---|---|
| `observed: ObservedThermalInput` | window（時刻ごとの metric の値と mask）と、直前の effective demand（`action`） | window feature の軸（§2.4）と直前 Demand の軸（§2.3） |
| `anchor` | `observed` から内部モデルが作る予測 | 格子の点ごとに内部モデルで作る（軸ではない） |
| `constraints: HardConstraintSet` | 設定の `zone_bounds`・`zone_min_demand`・**この tick の safety floor**・直前の effective・上げ幅 / 下げ幅 | 設定の部分は `optimizer` の部品と Control Config で束ねる（0152 §2.3）。tick ごとに変わるのは safety floor と直前の effective で、前者を代表集合（§2.5）、後者を直前 Demand の軸にする |
| `weights` / `target_band` | Supervisor の context（基準とヒント有り） | `context_overrides` の鍵から導く（§2.2） |
| `baseline: PerZone[Demand]` | Fallback の Baseline の requested | 代表集合（§2.5） |

---

## 2. Decision

### 2.1 置き場所: `fan-policy.yaml` に `supervisor.workload_hint.monotonicity_gate`、`safety.yaml` に `workload_hint`

- 格子と `eps` / `eps_c` は `fan-policy.yaml` の `supervisor.workload_hint.monotonicity_gate`（新しい塊）に置く。
  ヒントの設定（`context_overrides`）の隣に置き、同じファイルの版で束ねる
- 上限 `max_eps` / `max_eps_c` と `max_eps_c` の天井は `safety.yaml` の `workload_hint`（新しい塊）に置く（0066 §2.1 (b)）。
  人がレビューする安全側の設定に置き、Policy 側の値で上限を動かせないようにする
- どちらも**省ける**。`stage` が `off` / `observe` のときは読まない。`shadow_mpc` / `active` で塊が無ければ
  ゲートの仕様を導けないので `hint_gate_invalid`（0152 §2.4 の 4）で B0 に留まる（起動は止めない）
- 版: 実装の PR で `fan-policy.yaml` を v12 → v13、`safety.yaml` を v4 → v5、束ねた版 `CONTROL_CONFIG_VERSION` を
  1つ上げる（いままでの版上げと同じ手順。`docs/control-config.md` に移行手順を書く）。既存の値（0119 が確定した
  `safety.yaml` の値を含む）は変えない
- ルール 2・3: どの欄も `requested_demand` の手前（Learned MPC の Shadow solver と、将来の B2 の context の選択）に
  しか効かない。Reactive Guard と Critical Safety の値・経路は変えない

### 2.2 Regime と workload の軸は `context_overrides` の鍵から導く（設定に別に書かない）

- 格子の (Regime, workload) は **`context_overrides` の鍵の集合そのもの**とする。鍵に無い組ではヒントは
  context を変えない（0064 §2.8）ので、試す必要が無い。別の列挙を設定に持つと、鍵と食い違ったときにどちらが
  正かを決める規則が要る
- 各鍵について、基準の context は `rule_policy.contexts[regime]`、ヒント有りの context は鍵の値の context
  （0066 §2.1 (a) の制限は読み込み時に検査済み）
- 認証領域の判定（0066 §2.1 (d)）の Regime / workload の軸は「その tick の (Regime, ヒントの workload) が
  `context_overrides` の鍵にある」こと。鍵に無ければそもそもヒントが効かない
- `context_overrides` が空なら格子の点は 0 で、`hint_gate_invalid`（0152 §2.3「0 になる仕様は設定の誤り」）

### 2.3 数値の軸は「点の列」で書く（`min` / `max` / `step` で書かない）

各数値の軸は、試す値を**昇順の有限な値の列**（`points`）で書く。

```yaml
supervisor:
  workload_hint:
    monotonicity_gate:
      eps: {value: <Demand の許容誤差>, status: provisional}
      eps_c: {value: <予測温度の許容誤差 ℃>, status: provisional}
      previous_demand:            # 直前の effective demand（0066 の「低 / 中 / 高」）
        points: [<d1>, <d2>, <d3>]
      temperature:                # CPU / GPU の温度（§2.4。基準の context の upper_c からの差 ℃）
        cpu: {points: [<band の下>, <band の中>, <upper_c の付近>, <超過>]}
        gpu: {points: [...]}
      window_features:            # その他の window feature（§2.4。絶対値）
        - {metric: <metric 名>, points: [...]}
      safety_floors:              # hard constraints の代表集合（§2.5）
        - {front: <d>, rear: <d>, top: <d>}
      baselines:                  # baseline の代表集合（§2.5）
        - {front: <d>, rear: <d>, top: <d>}
```

- `points` は1つ以上・**狭義の昇順**・全て有限。Demand の軸は `[0, 1]`。満たさなければ `hint_gate_invalid`
- **認証領域のその軸の範囲は `[points の先頭, points の末尾]`**、刻みは `points` そのもの。範囲と刻みを別々に書かない
  ので、範囲の端が刻みに乗らない・浮動小数の刻みで点の数が環境によって変わる、が起きない
- 直前 Demand は3系統それぞれに同じ `points` を当て、**3系統の直積**を試す（系統ごとに独立に変わるため。
  認証領域は系統ごとに同じ範囲）
- 格子の点 = (§2.2 の鍵) × (直前 Demand の直積) × (CPU 温度) × (GPU 温度) × (各 window feature) × (safety floor の
  代表集合) × (baseline の代表集合)。点の数（`grid_points`）はこの積で、0 なら `hint_gate_invalid`
- 点の数に上限の設定は置かない（ランナーは `SCHED_IDLE` で走り、制御に効かない。0152 §2.1）。ランナーは
  走らせる前に点の数を構造化ログに出す（§5 の 6）

### 2.4 window feature: 「window 全体を同じ値にした平らな window」を作る。温度は band からの差で書く

- 格子の点ごとに、ランナーは内部モデルが要求する window の長さ・時刻の並び（artifact の window の仕様）で
  **全 frame が同じ値**の window を作る。mask はすべて立てない（missing / stale / suspect の無い観測）
- CPU / GPU の温度の metric は `mpc.optimizer.cost_metrics.cpu_temperature` / `gpu_temperature` の metric で、
  値は**その鍵の基準の context の `target_band.*.upper_c` に `points` の値を足したもの**（0066 の「band 下 / band 内 /
  基準 `upper_c` 付近 / 超過」は band に対する位置なので、band を変えても意味が保たれる形にする）。
  認証領域の判定でも、その tick の Regime の基準の `upper_c` からの差で比べる
- それ以外の metric は `window_features` に **metric 名と絶対値の `points`** で書く
- **内部モデルが window に要求する metric は、すべて軸を持たなければならない**（温度の2つか `window_features`）。
  軸の無い metric があれば、その artifact に対してはゲートの仕様を当てられないので、ランナーは不合格にし
  （証明書を出さない）、worker の Shadow solver は `certification_mismatch` で解かない。値を1つに固定したい metric は
  `points` を1点にする（黙って何かの値で埋めない）。`window_features` に artifact が使わない metric があれば
  同じく不合格（書き損じを見逃さない）
- fand は artifact を読まない（0077 §2.6）ので、この網羅の検査はランナーと worker が行う。fand が導く仕様は
  設定だけから作れる（0152 §2.3 の「設定から1つの純粋な関数」を崩さない）
- 認証領域の判定（0066 §2.1 (d)）は、その tick の window の**全 frame の値**が各軸の範囲の中にあること、かつ
  軸を持つ metric に missing / stale / suspect が無いこと。どれかが外なら `out_of_certified_domain`
- 平らな window は、時間方向の変化（上昇中・下降中）を試さない。この差は 0066 §2.1 (d) の二重解法（領域の中の
  tick でも毎回 2 条件を確かめる）が受け持つ。範囲の判定は**外を弾くためだけ**に使う（0066 §2.1 (d) のとおり）

### 2.5 hard constraints と baseline は「3系統の値の組の列」で代表集合を書く

- **hard constraints**: `HardConstraintSet` のうち tick ごとに変わるのは safety floor と直前の effective で、
  後者は §2.3 の軸。前者を `safety_floors` に、`{front, rear, top}` の Demand の組の列で書く。設定由来の
  `zone_bounds`・`zone_min_demand`・上げ幅 / 下げ幅は `optimizer` の部品と Control Config で束ねる（0152 §2.3）ので、
  ここには書かない。ランナーは各点で `HardConstraintSet.build` を**実行時と同じ関数**で呼ぶ。実行不能（`InfeasiblePlanError`）
  になる点は、実行時も Fallback になる点なので**試験の対象から外して数え**、その数を証明書には載せずログに出す
- **baseline**: `baselines` に `{front, rear, top}` の Demand の組の列で書く（0066 の「baseline の代表集合」の文言どおり）
- 認証領域: 系統ごとに、列の値の最小〜最大の範囲（組の列の外接箱）。その tick の safety floor / Baseline の
  requested が系統のどれか1つでも範囲の外なら `out_of_certified_domain`
- `safety_floors` には、Critical Safety と Reactive Guard の floor が掛かっていないときの値（`zone_min_demand`）を
  含めることを**推奨**するが、強制しない（ゲートの仕様の関数は `safety.yaml` の `zone_min_demand` を読まない。
  0152 §2.3 の入力に無い）。含めなければ、その状態の tick が領域の外になって B0 と同じ振る舞いになるだけ

### 2.6 `eps` / `eps_c` と上限の形

- `eps` / `eps_c` は `fan-policy.yaml` の `monotonicity_gate` に `ConfigValue`（`value` / `status` / `basis`）で置く
- `safety.yaml` の `workload_hint` は次の3つを `ConfigValue` で持つ

  ```yaml
  workload_hint:
    max_eps: {value: <Demand>, status: provisional}
    max_eps_c: {value: <℃>, status: provisional}
    max_eps_c_ceiling: {value: <℃>, status: provisional}   # max_eps_c の天井（0066 §2.1 (b)）
  ```

- **天井は `safety.yaml` の値として置く**（0066 §2.1 (b)「`safety.yaml` のスキーマに持たせる（コードに定数を置かない）」）。
  コードは「有限・非負」と大小関係だけを検査する
- 検査（0066 §2.1 (b)。どれか1つでも外れれば `eps_bounds_invalid` で B0。**起動は止めない**。上限の検査が通るまで
  `eps` の比較を始めない）:
  1. `max_eps` / `max_eps_c` / `max_eps_c_ceiling` が揃って有限・非負（YAML の `.inf` / `.nan`・負値・欠落は不正）
  2. `max_eps < 1.0`、`max_eps_c <= max_eps_c_ceiling`
  3. `eps` / `eps_c` が有限・非負で、`eps < 1.0`・`eps <= max_eps`・`eps_c <= max_eps_c`
- そのため、この2つの塊の**値の制約**は Control Config の読み込みの検査（`config_invalid` の全 zone Max）に入れず、
  ゲートの仕様の関数の中で検査する。**形の誤り**（未知の鍵・型の違い・`ConfigValue` の欄の欠落）は、ほかの欄と
  同じく Control Config の不正にする（§5 の 3）
- `status` の扱い: ゲートの仕様は `provisional` でも導く（B1 は `requested_demand` に効かない。0133 §2.6）。
  B2 で `confirmed` を要るとするかは B2 の記録で決める（§5 の 4）

### 2.7 ゲートの仕様の正準な形（`hint_gate` の digest の入力）

- 関数の出力は、§2.2〜§2.6 から導いた次の値を、`calibration_offsets.canonical_offsets_bytes` と同じ規約の canonical JSON
  （`sort_keys`・`allow_nan=False`・区切りの空白なし）にしたもの: 版の印・`eps` / `eps_c` の **value**・
  (Regime, workload) の鍵の列（昇順）・各数値軸の `points`・`safety_floors` / `baselines` の列（書かれた順を保ち、
  重複は不正）・認証領域（各軸の範囲）・`grid_points`
- `status` / `basis` は入れない（根拠の書き直しで再認証を要らなくする。値が変われば digest は変わる）
- 上限（`max_eps` ほか）は出力に入れない（検査に通ることだけが条件。上限を変えても `eps` が同じなら同じ証明書）
- 版の印は `hint-gate-spec-v1`。この記録の形を変えるときは版を上げる

### 2.8 証明書の metadata の `feature_schema_version` / `target_schema_version` はここで決める

- kind `hint_certificate` の metadata（0152 §5 の 2 (a)）の2欄を、次の値に固定する
  - `feature_schema_version` = `hint-gate-spec-v1`（§2.7 の版の印。証明書が何の仕様で作られたか）
  - `target_schema_version` = `hint-certificate-v1`（証明書の本体の形の版。0152 §2.3 の `schema_version`）
- Registry の metadata の検査（PR #349 の `_hint_certificate_fields_are_fixed`）にこの2つを足し、fand が証明書を
  読むときの `ModelCompatibility`（段 3）もこの値を渡す。どちらかの版を上げた実装は、古い証明書を
  schema の不一致で読まず B0 に倒れる（安全側）

---

## 3. Consequences

### 良くなること

- 0152 の段 1b（ゲートの仕様の関数・証明書の型）・段 2（ランナー）・段 3（fand の照合の手順 4）を書ける
- 範囲と刻みを `points` の1つで書くので、範囲の端が刻みに乗らない・浮動小数の刻みの丸めで点の数が揺れる、が起きない
- (Regime, workload) を `context_overrides` から導くので、試験の範囲とヒントが効く範囲が食い違わない
- artifact が使う metric の網羅をランナーと worker が確かめるので、軸の無い feature を何かの値で埋めて
  「認証した」ことにならない
- 値の制約の違反は B0 に倒れるだけで、全 zone Max にも起動拒否にもならない（0066 §2.1 (b) / 0152 §2.4）

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 直積で点の数が大きくなる（例: 鍵 2 × 直前 Demand 3³ × 温度 4 × 4 × 他の軸 × 代表集合） | ランナーは `SCHED_IDLE`・制御の外。点の数をログに出し、人が刻みを選ぶ。上限の設定は置かない（§5 の 6） |
| 平らな window は時間方向の変化を試さない | 0066 §2.1 (d) の二重解法が領域の中の tick を毎回確かめる。範囲の判定は外を弾くためだけ |
| 代表集合の外接箱は、試した組より広い領域を「中」とみなす | 同じく二重解法が受け持つ。範囲の判定で領域の中を認証済みとは扱わない（0066 §2.1 (d)） |
| `zone_min_demand` を `safety_floors` に入れ忘れると、平常の tick がすべて領域の外になる | B0 と同じ振る舞いになるだけ（安全側）。`docs/control-config.md` に推奨を書く |
| `safety.yaml` の版が上がる | 既存の値は変えない。移行手順を `docs/control-config.md` に書く（いままでの版上げと同じ） |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 数値の軸を `{min, max, step}` で書く | 範囲の端が刻みに乗らないときの扱い・浮動小数の刻みで点の数が環境ごとに変わりうる、の規則が要る。`points` なら列そのものが刻みで、範囲は先頭と末尾 |
| (Regime, workload) の軸を設定に別に列挙する | `context_overrides` と食い違ったときに、試していない鍵でヒントが効く・効かない鍵を試す、が起きる |
| 温度の軸を絶対値（℃）で書く | 0066 の4区分は band に対する位置。band（`rule_policy.contexts`）を変えると格子の意味がずれる |
| window の軸に傾き（上昇・下降の速さ）も持たせる | 点の数が軸ごとに倍以上になる。時間方向の変化は二重解法で受け持つ（0066 §2.1 (d)）。必要になれば版を上げて足す（§5 の 5） |
| 記録済みの window（Thermal Dataset の代表例）を格子に使う | 設定から導けず、fand が仕様を組み立てられない（0152 §2.3 の「設定から1つの純粋な関数」と合わない）。認証領域を範囲で言えない |
| 軸の無い metric を設定の既定値や0で埋める | 試していない値で「認証した」ことになる。1点の軸として明示させる |
| baseline を Fallback の曲線から格子の状態ごとに導く | 実行時の Baseline に近いが、ランナーが制御状態の snapshot を合成する必要があり、ゲートの仕様が Fallback の設定に依る（fand の仕様の関数の入力が増える）。0066 の「代表集合」の文言にも合わない（§5 の 2） |
| safety floor を系統ごとの軸にして直積で試す | 点の数が3乗で増える。floor の組み合わせは Guard / Safety の状態で決まり、組の列のほうが実際の状態を表しやすい |
| 天井をコードの定数（スキーマの `le=`）にする | 0066 §2.1 (b) の「コードに定数を置かない」と合わない（ルール 9） |
| `eps` の値の制約違反を Control Config の不正（全 zone Max）にする | 0066 §2.1 (b) は「Stage B を無効のまま起動」。ヒントは安全の入力ではないので、Fan を全開にする理由にしない |
| `feature_schema_version` / `target_schema_version` を段 3 に回す | 形が決まったいま決められる。段 3 まで空けると、それまでに登録した証明書の値がばらつく |

---

## 5. 判断点（決定済み。推奨を先頭に）

**所有者の決定（2026-10-16）**: 1〜9 のすべてを推奨の案（いずれも (a)）で決めた。§2 の本文はその決定を書いたものである。

| # | 判断点 | 選択肢（推奨を先頭） | 決定 |
|---|---|---|---|
| 1 | 数値の軸の書き方（§2.3） | **(a 推奨) 昇順の `points` の列。範囲は先頭と末尾** / (b) `{min, max, step}`（端と丸めの規則が要る） | 決定済み（所有者、2026-10-16）: (a) |
| 2 | baseline の代表集合（§2.5） | **(a 推奨) 設定に `{front, rear, top}` の組の列で書く。領域は外接箱** / (b) 格子の状態ごとに Fallback の曲線から導く（実行時に近いが、ゲートの仕様が Fallback の設定に依り、ランナーが snapshot を合成する） | 決定済み（所有者、2026-10-16）: (a) |
| 3 | `monotonicity_gate` / `safety.yaml` の `workload_hint` の不正の扱い（§2.6） | **(a 推奨) 形の誤り（未知の鍵・型・欄の欠落）は Control Config の不正、値の制約（有限・非負・大小・点の数）は `hint_gate_invalid` / `eps_bounds_invalid` で B0** / (b) どちらも B0（他の欄と扱いが割れる）/ (c) どちらも Control Config の不正（0066 §2.1 (b) の「無効のまま起動」と合わない） | 決定済み（所有者、2026-10-16）: (a) |
| 4 | `provisional` の `eps` / 上限で Stage B に進めるか（§2.6） | **(a 推奨) 仕様は `provisional` でも導く。B1 は進める。B2 で `confirmed` を要るかは B2 の記録で決める** / (b) B1 から `confirmed` を要る（実測の前に B1 の測定を始められない） | 決定済み（所有者、2026-10-16）: (a) |
| 5 | window feature の形（§2.4） | **(a 推奨) 平らな window（全 frame 同じ値）。温度は基準の `upper_c` からの差、他は絶対値。artifact の使う metric はすべて軸を持つ** / (b) 値に加えて傾きの軸も持つ（点の数が増える）/ (c) 記録済みの代表 window（設定から導けない） | 決定済み（所有者、2026-10-16）: (a) |
| 6 | 点の数の上限 | **(a 推奨) 設定に置かない。ランナーが点の数をログに出す** / (b) `monotonicity_gate.max_grid_points` を置き、超えたら `hint_gate_invalid`（大きすぎる格子の打ち間違いを止める） | 決定済み（所有者、2026-10-16）: (a) |
| 7 | `max_eps_c` の天井の置き場所（§2.6） | **(a 推奨) `safety.yaml` の `max_eps_c_ceiling` の値（`ConfigValue`）。コードは有限・非負と大小だけを見る** / (b) コードのスキーマの定数（0066 の「コードに定数を置かない」と合わない） | 決定済み（所有者、2026-10-16）: (a) |
| 8 | 証明書の metadata の2欄（§2.8） | **(a 推奨) ここで `hint-gate-spec-v1` / `hint-certificate-v1` に固定する** / (b) 段 3（fand の照合）で `ModelCompatibility` と一緒に決める | 決定済み（所有者、2026-10-16）: (a) |
| 9 | `safety_floors` に `zone_min_demand` を含めることを強制するか（§2.5） | **(a 推奨) 強制しない（推奨として文書に書く。含めなければ領域の外で B0 と同じ）** / (b) 強制する（ゲートの仕様の関数が `safety.yaml` の `zone_min_demand` を読むことになり、0152 §2.3 の入力が増える） | 決定済み（所有者、2026-10-16）: (a) |

値そのもの（各 `points`・`eps` / `eps_c`・`max_eps` / `max_eps_c` / 天井）は 0066 §5 の 1 のとおり Stage A の実測で
決める。この記録では決めない。
