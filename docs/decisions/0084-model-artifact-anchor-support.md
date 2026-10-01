# 決定記録 0084: 反実仮想 Thermal Model artifact v2 の anchor 推論の action 軌道・step ごとの support・`metric_binding` の網羅（0079 の後に残った3点）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-01、リポジトリ所有者が承認）
- **Date**: 2026-10-01
- **Supersedes**: [0079](0079-model-artifact-formats.md) の次の部分だけを置き換える。0079 の他の点は有効。
  - §2.1 の Profile v2 の表の「action の学習範囲」の行のうち、計画 demand の min / max・step 間の変化量の
    min / max と件数・(a) 全 zone の計画 demand の組の cell・(c) step 間の cell の組を、**全 step で1つに
    まとめて持つ**部分（本記録 §2.2 で step の番号ごと・step の組ごとに持つ。anchor → 最初の step の
    変化量と (b) の cell の組は、もともと1つの step の組にしか関わらないので変えない）
  - §2.5 の最初の項の、候補の zone ごとの demand と step 間の変化量を「全 step でまとめた範囲」で
    照合する部分、および §2.5「zone をまたぐ同時の support」の「記録するもの」の (a) / (c) と
    「候補の照合」のうち、(a) / (c) の集合を全 step で共有する部分（本記録 §2.2）
  - §2.4 の検査の表の L8 の行（各 entry の検査に、キー集合の一致を加える。本記録 §2.3）
  - 本記録が足すだけで 0079 を置き換えない点: §2.4 の検査の表への L11 / L12 の追加（§2.1 / §2.2）、
    §2.3 の feature schema の項への anchor 推論の action 列の規則の追加（§2.1）、§2.7 の試験の追加（§2.4）

  [0050](0050-model-confidence-ood-and-authority.md) の次の部分だけを、**Profile v2 についてだけ**置き換える。
  v1 の Profile には 0050 をそのまま適用する。0050 の他の点は有効。
  - §2.1 の「residual の基準（出力ごとの RMS）だけは validation から作る」の項と Profile の表の「residual scale」の
    行のうち、validation の**全 example** から作る部分（本記録 §2.1 で、held の列が step ごとの support の外にある
    validation example を除き、除いた件数を Profile に記録し、ある出力で残りが0件なら Profile の作成を拒否する）
  - §2.2 の判定の構成要素の表の `support` の行の「OOD にする条件」（本記録 §2.1 で、anchor 推論の held の列が
    step ごとの support の外であることを条件に加える。構成要素の enum と assessment の形は変えない）
  - 旧記録側への `Superseded by` の追記（README「追記のみ」）は本 PR で 0079 と 0050 に行った
- **関連**: [0031](0031-thermal-dataset-contract.md) §2.2 /
  [0048](0048-thermal-model-artifact-and-inference.md) §2.1 / §2.4 /
  [0050](0050-model-confidence-ood-and-authority.md) §2.1 / §2.2 / §3 / §5 #2 /
  [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.2 / §2.3 / §2.4 /
  [0053](0053-control-shadow-mode-and-counterfactual-logging.md) /
  [0079](0079-model-artifact-formats.md) §2.1 / §2.3 / §2.4 / §2.5 / §2.7 / §2.9
- **対象 Issue**: #104（2026-09-30 のコメント「実装の前に決める点」）。実装は 0079 §2.9 の段 2（#84）・段 3（#85）・段 4（#86）

## 1. Context

0079 のマージ後、Codex のレビューで3点が残り、2026-09-30 に Issue #104 のコメントへ「実装の前に
小さな決定記録で決める」として記録した。

1. **P1（0079 §2.3 付近）anchor 推論に使う action の軌道が決まっていない。** v2 の feature schema
   `thermal-features-v2` は計画の全 `step × zone` の action の列を持つ（0079 §2.3）。ところが anchor 推論
   （0052 §2.2 の 1. `predict(observed)`）は候補の `ActionPlan` を作る前に呼ばれ、入力の
   `ObservedThermalInput` は現在の effective demand（`action: PerZone[ObservedFanAction]`）しか持たない。
   計画 action の列に何を入れるかが決まっていないと、実装ごとに違う値が入り、Profile の residual の基準
   （validation から作る。0050 §2.1）と runtime の residual drift（0050 §2.2）が別の入力の予測を比べることになる
2. **P1（0079 §2.5 付近）support を step ごとに持っていない。** 0079 は計画 demand の範囲と cell・遷移の
   集合を全 step で1つにまとめて持つ。早い horizon（step 0）でしか観測していない demand が、遅い horizon
   （step 5 など）の列でも「観測した」として通る。model は step ごとに別の係数を持つ（0079 §2.3 の因果の
   mask）ので、遅い step の列でその値を学習していないなら、そこは外挿である
3. **P2（0079 §2.4 の L8 付近）`metric_binding` が全 metric を覆うことを求めていない。** L8 は binding に
   ある entry を1つずつ runtime の `MetricCatalog` と照合するだけで、feature / target が使う metric が
   binding から**抜けている**ことを検出しない。抜けた metric の単位は束縛されず、0079 §2.3 の
   「単位の変更を黙って通さない」が破れる

2026-10-01 にリポジトリ所有者が、3点とも Issue のコメントの推奨案で承認した。さらに同日、本記録の初版で
推奨案の文言を超えて決めた3点（§6 の「追加で承認した点」）も、所有者が承認した。本記録はそれを
実装が参照できる具体性で書く。**数値のしきい値・設定値は新しく決めない。**

## 2. Decision

### 2.1 anchor 推論の action 軌道は「現在の effective demand を horizon の間保つ」（決定論的な規則）

**規則 `hold_effective`**: anchor 推論では、feature schema の計画 action の列（step `k` = 0 .. `steps − 1`、
zone `z` ∈ Front / Rear / Top）に、すべての `k` について `observed.action.<z>.effective_demand` を入れる。

- `steps` と zone の順は action schema `thermal-actions-v1`（0079 §2.3）の格子から取る。`ObservedThermalInput`
  は格子を持たないので、写しは**封をした型 `RegistryCounterfactualThermalModel`（0079 §2.4）の中の1つの関数**
  が行い、呼び出し側が計画 action の列を渡す引数を `predict(observed)` に持たせない
- これは `ActionPlan.held(現在の effective demand, step_ms=action schema の step_ms, steps=action schema の
  step 数)` と同じ action 列である。**同じ観測に対して、anchor 推論の予測値は、その held plan を候補として
  評価した予測値と一致しなければならない**（試験で確かめる。§2.4）
- 値は丸め・補間・clamp をしない。`effective_demand` は既に 0.0..1.0 で検証されている（`ObservedFanAction`）
- 因果の mask（0079 §2.3）はそのまま掛かる。held の列であっても horizon より後の step の係数は 0 である
- **held の列も、候補と同じ step ごとの support の照合を受ける（margin なし）。** 0050 の判定は anchor action
  （action 時点の値）の `fan range` と support cell しか見ないので、held の列の遅い step の値が、その step で
  学習していない値であっても通ってしまう。そのまま予測を出すと、外挿の予測が通常の confidence を得て
  Learned MPC の提案を通しうる（AGENTS.md ルール4 に反する）。そこで次のとおりにする（2026-10-01 所有者承認。
  §6 の追加）
  - 照合は §2.2 の候補の照合と**同じ関数**で行う。held の列について、step `k` ごとの範囲と (a)、
    anchor → 最初の step の変化量の範囲と (b)、組 `(k, k + 1)` ごとの変化量の範囲と (c) のすべてを当てる。
    held の変化量は 0 なので、変化量の照合は「その step の組で変化量 0 が観測された範囲に入るか」になる。
    margin も件数の下限も掛けない（§2.2 と同じ）
  - 1つでも外れれば、その anchor 推論を **OOD** とする。0050 §2.2 の既存の構成要素 **`support`**
    （`ConfidenceComponent.SUPPORT`）の OOD の条件に、Profile v2 についてだけ「held の列が step ごとの support の外」を
    加え、score は 0、`ood = true`、confidence は 0 とする（0050 §2.2「1つでも OOD なら confidence は 0」）。
    detail には外れた step の番号（遷移なら step の組）・zone・cell を入れる。**構成要素の enum と assessment の
    形は変えない**（新しい reason code を足さない）
  - confidence 0 の anchor 推論からは、0050 / 0052 の既存の経路どおり Gate が Learned MPC の提案を通さず、
    Baseline / Fallback で運転する。新しい経路は作らない
  - この照合は Profile v2（同梱）を持つ判定器だけが行う。v1 の Profile の判定は変えない

**学習と検証も同じ規則で行う。** 同じ関数を、次のすべてで使う（別の実装を持たない）。

| 使う場所 | 計画 action の列 |
|---|---|
| runtime の anchor 推論（0052 §2.2 の 1.） | `hold_effective` |
| Profile v2 の residual の基準（validation の各 example の anchor 推論。0050 §2.1） | `hold_effective`。example の anchor action（`ObservedThermalInput.from_example(example).action`）から作り、Dataset v2 に記録した action 列は使わない。held の列が step ごとの support の外にある example は基準から**除く**（下記。§5 #1 の決着） |
| offline の評価・Shadow の照合で「anchor 推論」として扱う予測（0053 / 0054） | `hold_effective` |
| 候補 plan の評価（`predict_plan`）・trainer の係数の当てはめ | 対象外。候補は `ActionPlan` の列、当てはめは Dataset v2 に記録した action 列（0079 §2.9 段 1）のまま |

理由: runtime の residual drift（0050 §2.2）は anchor 推論の予測と後の実測を比べる。その基準となる
validation の residual が、記録した（実際に掛かった）action 列で作られていると、runtime と基準とで入力の
意味が違い、正規化 residual の比が model の当たり外れでなく入力の違いを測ってしまう。両方を
`hold_effective` に揃えると、「いまの demand を保つと仮定した予測が、その後の実際の運転でどれだけ外れるか」
を同じ意味で比べられる。

**residual の基準から support の外の validation example を除く**（2026-10-01 所有者承認。§6 の追加）。

- validation の各 example の held の列を、上の runtime の照合と**同じ関数**で、train から数えた step ごとの support
  （§2.2）に当てる。1つでも外れた example は、Profile v2 の residual の基準（出力ごとの validation residual RMS。
  0050 §2.1）の計算に入れない。runtime ではその入力は `support` の OOD になり予測を使わないので、その外挿の誤差で
  residual scale を広げると、support の中の予測の residual drift が見えにくくなる（PR #205 の Codex P1）
- 除いた example の件数を Profile v2 に記録する（基準に使った件数とともに）。除いた理由の内訳は detail と同じく
  step の番号（遷移なら step の組）・zone・cell で数えてよいが、型の必須は件数だけとする
- 除いた後、ある出力について基準に使える example が **1件も残らない**なら、Profile の作成（したがって artifact の
  登録）を拒否する。黙って基準を作らない・既定値で埋めない。「0件」は構造上の条件であり、新しい数値の
  しきい値は足さない（件数が少ない基準の扱いは 0050 の `residual_scale_floor` のまま）
- この除外は Profile v2 だけに当てる。v1 の Profile の作り方は変えない

**読み込み時の検査（0079 §2.4 への追加 L11）**: manifest と Profile v2 は、この規則を `anchor_action_rule`
として持つ。v2 で取れる値は `hold_effective` だけの Literal とする（規則を変えるなら schema の版を上げ、
新しい決定記録を先に作る）。

| # | 検査 | 外れたとき |
|---|---|---|
| L11 | manifest の `anchor_action_rule` と Profile v2 の `anchor_action_rule` が、ともに `hold_effective` で一致する | 拒否 |

Profile の residual の基準をどの規則の予測から作ったかが、Profile の bytes に入る。L11 は「別の規則で作った
基準を持つ Profile」を型にしない。規則の実行そのもの（関数が1つであること）は、型の中に写しを閉じ込める
ことと、§2.4 の試験で保つ。

### 2.2 support は step の番号ごと、遷移の support は対応する step の組ごとに持つ（0079 §2.1 / §2.5 の置き換え）

Profile v2 の「action の学習範囲」（0079 §2.1 の表）を次のとおり持つ。どれも **train だけ**から数える
（0050 §2.1。変えない）。

| 欄 | 0079 | 本記録 |
|---|---|---|
| 計画 demand の範囲 | zone ごとの min / max（全 step で1つ） | **step `k` ごと**・zone ごとの min / max と件数 |
| step 間の変化量 | zone ごとの符号付き min / max と件数（全 step の組で1つ） | **step の組 `(k, k + 1)` ごと**・zone ごとの符号付き min / max と件数 |
| anchor → 最初の step の変化量 | zone ごとの符号付き min / max と件数 | 変えない（もともと組 `(anchor, 0)` だけ） |
| (a) 全 zone の計画 demand の組の cell | cell と件数（全 step で1つ） | **step `k` ごと**の cell と件数 |
| (b) anchor の cell → 最初の step の cell | cell の組と件数 | 変えない |
| (c) 隣り合う step の cell の組 | cell の組と件数（全 step の組で1つ） | **step の組 `(k, k + 1)` ごと**の cell の組と件数 |

- **候補の照合**（0079 §2.5 の最初の項と「候補の照合」の置き換え）: 候補 plan の step `k` の zone ごとの demand は
  step `k` の範囲に、step `k` の全 zone の組の cell は step `k` の (a) に、step `k` → `k + 1` の変化量は組
  `(k, k + 1)` の範囲に、その cell の組は組 `(k, k + 1)` の (c) に入っていなければ、その候補を**評価しない**。
  anchor → 最初の step は 0079 のまま (b) と anchor → 最初の step の範囲で照合する。ある step で観測が
  1件も無い（範囲や集合が空の）ときは、その step にどの値も通らない
- **margin を掛けない・件数の下限を足さない**（0079 §2.5 と同じ。観測した値・cell・組の外は評価しない）
- **Fallback の requested も同じ照合を受ける。** 外れれば 0079 §2.5 どおり optimizer が `error`
  （`reason.code = plan_out_of_learned_range`）を返し、Gate は `optimizer_error` として Fallback にする。
  detail には外れた step の番号（遷移なら step の組）・zone・cell を入れる
- **cell の分け方は変えない**（0050 §2.1 の support 軸 `fan.<zone>` の bin の境界。境界の実値は 0050 §5 #2 で
  開いたまま）
- **構造上の上限**: 0050 §3 の cell 数の上限を、step ごとの集合と step の組ごとの集合の**それぞれ**に当てる。
  全体の大きさは artifact の上限（0048 §2.4）で抑える。どちらかを超えれば Profile の作成（したがって artifact の
  登録）を拒否する。黙って切らない。新しい数値は足さない（合計に別の上限が要るかは §5 #2）

**読み込み時の検査（0079 §2.4 への追加 L12）**:

| # | 検査 | 外れたとき |
|---|---|---|
| L12 | Profile v2 の step ごとの欄（範囲と (a)）の数が action schema の step 数と等しく、step の組ごとの欄（変化量と (c)）の数が step 数 − 1 と等しく、番号が 0 から欠けずに並ぶ | 拒否 |

格子と support の step がずれた Profile を型にしない（0079 §2.3 の「plan の offset 列は action schema の格子と
完全に一致」と同じ向き）。

### 2.3 L8 は `metric_binding` のキー集合が feature と target の metric の和集合と一致することも検査する（0079 §2.4 の L8 の置き換え）

| # | 検査 | 外れたとき |
|---|---|---|
| L8 | (1) `metric_binding.entries` の `metric` に重複が無い。(2) その集合が、feature schema の metric と target schema の metric の**和集合と一致**する（不足も余分も認めない）。(3) 各 entry が runtime の `MetricCatalog` に同じ単位（派生値は同じ定義）で存在する（0079 の L8 のまま） | 拒否 |

- 余分な entry も拒否する。使わない metric の単位を束縛すると、その metric の catalog の変更だけで
  artifact が使えなくなり、しかも理由が model の入力と無関係になる
- 派生値の `derived: {minuend, subtrahend}` の要素は、和集合に入っていなくてよい（定義は entry の中で
  照合する。0079 の L8 のまま）
- 同じ検査を artifact の**作成時**（trainer / Profile の同梱）にも行い、満たさない artifact を作らない。
  読み込み時の L8 は、作成時の検査を経ない bytes に対する最後の門である

### 2.4 試験（0079 §2.7 への追加。すべて `-m "not hardware"`）

- anchor 推論: 同じ `ObservedThermalInput` について、anchor 推論の予測値が `ActionPlan.held(現在の
  effective demand, ...)` を候補として評価した予測値と一致する。Profile v2 の residual の基準を、合成 dataset で
  記録した action 列が held と**違う** validation example から作ったとき、基準が held の予測から計算されている
- held の列の support（§2.1）: train で demand 0.8 を step 0 でだけ観測し、遅い step では観測していない Profile で、
  現在の effective demand が 0.8 の anchor 推論が `support` の OOD（confidence 0、detail に step の番号）になり、
  Gate が Fallback の requested を選ぶ。anchor action の `fan range` と support cell には入っている（0050 の既存の
  判定だけなら通る）入力で確かめる。組 `(k, k + 1)` の (c) にだけ無い held の遷移も同じく OOD になる。
  step ごとの support の範囲に入る held の列は、この照合で OOD にならない。v1 の Profile の判定は変わらない
- residual の基準の除外（§2.1）: held の列が step ごとの support の外にある validation example を含む合成 dataset で、
  その example が基準の RMS に入らず、除いた件数が Profile に記録される。ある出力の validation example がすべて
  support の外のとき、Profile の作成が拒否され artifact が登録されない
- L11: `anchor_action_rule` が manifest と Profile で食い違う・Literal 外の値の artifact が型にならず、
  runtime が `MODEL_LOAD_FAILURE` として Fallback の requested を出す（0079 §2.7 の L1〜L10 と同じ確かめ方）
- step ごとの support: train の計画 action で、ある demand（または cell）を step 0 でだけ観測した Profile で、
  その値を step 0 に持つ候補は評価され、同じ値を遅い step に持つ候補は評価されない。遷移も同じ
  （組 `(0, 1)` でだけ観測した cell の組を、組 `(3, 4)` に持つ候補は評価されない）。Fallback の requested が
  ある step でだけ外れるとき `plan_out_of_learned_range` になり、detail に step の番号が入る
- L12: step の欄の数が action schema の step 数と違う Profile、step の番号が欠けた Profile の artifact が型にならない
- L8: feature / target の metric の1つを `metric_binding` から抜いた artifact、使わない metric の entry を足した
  artifact、同じ metric の entry が2つある artifact が、それぞれ型にならない。作成時にも同じ入力が拒否される

## 3. Consequences

良くなること。

- anchor 推論の入力が実装によらず1つに決まり、Profile の residual の基準と runtime の residual drift が
  同じ意味の予測を比べる
- 早い horizon でしか学習していない demand・cell・遷移を、遅い horizon の候補で使わない。外挿の予測で
  Demand を選ぶ経路がさらに狭まる（0052 §2.4 の「狭める向きだけ」）
- `metric_binding` から metric が抜けた artifact が型にならず、単位の変更が黙って通る経路が塞がる

悪くなること（と緩和策）。

| 悪くなること | 緩和策 |
|---|---|
| support を step ごとに持つため、同じ train でも各 step の観測は薄くなり、評価される候補が減る（Fallback が増えうる） | 意図どおり（学習していない step の値で選ばない）。頻度は #90 / #91 の Shadow・評価で測る。緩めるなら新しい記録 |
| Profile の大きさが step 数に比例して増える | 上限は step ごとの集合と artifact の上限で抑え、超えれば登録で拒否する（黙って切らない）。合計の上限が要るかは §5 #2 |
| runtime の residual には「held を仮定した予測」と「実際に掛けた action」の差の分も入る | validation の基準も同じ差を含む（過去の運転でも action は変わっていた）ので、比はその分を相殺する方向に働く。運転方針が過去と大きく変わると比が動きうる。drift の判断（0056）で見る（§5 #3） |
| `metric_binding` に使わない metric を入れられない | 単位の束縛は model の入力に関わる metric だけに限るのが目的に合う |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| anchor 推論の計画 action の列を 0（または欠測）にする | 学習時に一度も入らない入力で、ridge の予測が意味を持たない。anchor 推論の予測値そのものが外挿になる |
| anchor 推論を候補 plan の評価の後に回し、選んだ plan の列で anchor を作る | 0052 §2.2 の順（anchor 推論 → Confidence 判定 → 探索）を逆にする。判定が探索の結果に依存し、判定の対象が tick ごとに変わる |
| anchor 推論には直前の tick に選んだ plan の残りの step を使う | 直前の plan は Guard / Safety の後に掛かった値と違いうる（0079 §2.3）。状態を tick 間に持ち越す必要があり、再起動や Fallback の後で規則が変わる |
| anchor 推論の列を Fallback（Baseline）の requested の held にする | anchor 推論が Fallback controller の実装に依存する。学習・検証の側で過去の各 example の Baseline を再現できず、同じ規則で基準を作れない |
| residual の基準は記録した action 列の予測から作り、runtime だけ held にする | runtime と基準で入力の意味が違い、residual drift の比が model の誤差でなく入力の違いを測る（§2.1） |
| support を全 step でまとめたまま持つ（0079） | 早い horizon でしか観測していない値が遅い horizon で通る（§1 の 2.） |
| step ごとに持つが、隣の step の観測で穴を埋める（近傍の step を許す） | 「観測した範囲の外は評価しない」（0079 §2.5）に margin を step の方向へ足すことになり、新しいしきい値（何 step まで許すか）が要る |
| 遷移の support を step の組で分けず、step ごとの cell だけを持つ | step ごとの cell が両方とも観測されていても、その組の遷移は学習していないことがある（0079 §2.5 で (c) を足した理由と同じ） |
| L8 で binding が feature / target の metric を**含む**ことだけを求める（余分は許す） | 使わない metric の catalog の変更で artifact が失効し、理由が model と無関係になる。作成時に余分を入れる理由が無い |
| `metric_binding` を持たず、feature / target schema から読み込み時に導く | 学習時の単位を artifact に写すこと自体が目的（0079 §2.3）。runtime の catalog から導くと、学習時と違う単位を照合できない |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | **決着**（§2.1）: held の列が step ごとの support の外なら anchor 推論を `support` の OOD にし Fallback へ。Profile v2 の residual の基準からも、held の列が support の外にある validation example を同じ関数で除き、除いた件数を記録し、残りが無い出力があれば Profile の作成を拒否する（2026-10-01 所有者承認。PR #205 の 488f918 への Codex P1 の解消） | 決着（本記録 §2.1） |
| 2 | step ごとの集合の合計に、0050 §3 の上限とは別の構造上の上限が要るか | 段 3（#85）の実装で Profile の大きさを測った後。数値を足すなら新しい記録 |
| 3 | 運転方針（MPC の authority）が学習時と大きく変わったとき、held を仮定した residual の比がどれだけ動くか | #90 / #91 の Shadow・評価と 0056 の drift の運用で測る |
| 4 | 0079 §5 #5（window / horizon / step 格子・target metric 集合の実値）と 0050 §5 #2（support 軸と bin の境界）は変わらず開いたまま | 実機 dataset（#50 / #83）の後 |

## 6. 所有者の決定（2026-10-01）

Issue #104 の 2026-09-30 のコメントで示した推奨案を、2026-10-01 にリポジトリ所有者が承認した。

- **1. anchor の action 軌道** → 推奨案「現在の effective demand を horizon の間保つ」を anchor の決定論的な規則とし、
  学習と検証も同じ規則で行う（§2.1）
- **2. support の持ち方** → 推奨案「demand の support は step の番号ごと、遷移の support は対応する step の組ごと」。
  外れた候補は評価しない（margin なし。0079 のまま）（§2.2）
- **3. `metric_binding` の網羅** → 推奨案「binding の metric のキー集合が feature と target の metric の和集合と一致することを
  L8 で検査する」（各 entry の検査に加える）（§2.3）

### 追加で承認した点（2026-10-01）

初版（44dc9b9）で推奨案の文言を超えて決めた次の3点を、2026-10-01 にリポジトリ所有者が承認した。

- 係数の当てはめは Dataset v2 に記録した action 列のままとし、「学習と検証も同じ規則」は Profile の residual の基準と
  anchor 予測の評価に当てる（§2.1 の表）
- zone ごとの計画 demand の min / max と step 間の変化量も、cell と同じく step ごと・step の組ごとに持つ（§2.2）
- 0050 §3 の cell 数の上限を step ごと・step の組ごとの集合それぞれに当てる（合計の上限は §5 #2）

同日、PR #205 のレビュー（Codex P1）を受けて、held の anchor の列にも step ごとの support の照合を当て、外れれば
`support` の OOD として Fallback にすることを承認した（§2.1。初版では §5 #1 に先送りしていた）。

同日、PR #205 のレビュー（488f918 への Codex P1）を受けて、Profile v2 の residual の基準から held の列が step ごとの
support の外にある validation example を同じ照合関数で除き、除いた件数を Profile に記録し、ある出力で残りが0件なら
Profile の作成（artifact の登録）を拒否することを承認した（§2.1。新しい数値のしきい値は足さない。§5 #1 の決着）。
