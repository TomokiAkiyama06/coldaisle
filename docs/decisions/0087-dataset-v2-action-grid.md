# 決定記録 0087: Thermal Dataset v2 の action 列を格子へ写す規則（as-of・鮮度・step 内の変化・格子と horizon・step の番号・較正の変更の検査の置き場所）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-01、リポジトリ所有者が承認）
- **Date**: 2026-10-01
- **Supersedes**: [0079](0079-model-artifact-formats.md) の次の部分だけを置き換える。0079 の他の点は有効。
  - §2.9 の段 1 の行のうち、action 列の範囲を「anchor から `label_end_ms` まで」とする部分。本記録 §2.4 で
    格子 `[anchor, anchor + steps × step_ms)` に置き換える（`label_end_ms` は 0031 のまま、最大の horizon に
    target の許容誤差を足した時刻）
  - 本記録が足すだけで 0079 を置き換えない点: 段 1 の action 列を格子へ写す規則（§2.1〜§2.3）、step の番号と
    `PlanStep.offset_ms` の対応の明文化（§2.5）、較正の変更の検査を置く場所（§2.6）

  旧記録（0079）側への `Superseded by` の追記は本 PR で行った。
- **関連**: [0031](0031-thermal-dataset-contract.md) §2.1 / §2.2 / §2.6 /
  [0048](0048-thermal-model-artifact-and-inference.md) §2.1 /
  [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.2 / §2.3 / §2.4 /
  [0056](0056-model-drift-detection-and-retraining-triggers.md) §2.5 /
  [0079](0079-model-artifact-formats.md) §2.3 / §2.5 / §2.9 /
  [0084](0084-model-artifact-anchor-support.md) §2.1 / §2.2 /
  AGENTS.md「絶対に守るルール」9
- **対象 Issue**: #83（0079 §2.9 の段 1: Thermal Dataset v2）

## 1. Context

0084 の実装は 0079 §2.9 の段 2（#84）から始まる。しかし段 2 の trainer と artifact v2 は、段 1（#83）の
Thermal Dataset v2 を入力にする。2026-10-01 時点の main には段 1 がまだ無い（`control/model/dataset.py` の
`DATASET_SCHEMA_VERSION` は `Literal[1]` だけ）。段 1 は依存の順で次に来る段である。

0079 の段 1 の行は、次の内容を決めている。

- 各 example に action 列を持たせる。列は action schema の格子の上に置き、`action_source` が指す値（effective demand）とする
- 列の各値の元になった ControlTick の時刻を持たせる
- 較正の変更をまたぐ窓は拒否する（0079 §2.3）

一方、ControlTick の時系列から格子の値を**どう取るか**は書いていない。決めないまま実装すると、同じ run から
実装ごとに違う action 列ができる。すると、0084 の step ごとの support（§2.2）と residual の基準（§2.1）が
比べている量の意味が揃わない。決まっていない点は次の6つである。

1. step `k` の値を、どの ControlTick から取るか
2. その tick が古すぎる場合をどう扱うか（action の鮮度）
3. step の区間の途中で effective demand が変わった場合をどう扱うか
4. 格子と target の horizon をどう対応させるか
5. step の番号と `ActionPlan` の `PlanStep.offset_ms` をどう対応させるか（0079 §2.3 は step `k` を区間
   `[k × step_ms, (k + 1) × step_ms)` とする。`control/mpc/plan.py` は `offset_ms = step_ms × (k + 1)` である）
6. 較正の変更の検査（0079 §2.3）を、どの層で行うか

さらに本記録の初版のレビューで、7つ目の点が見つかった（§2.7）。

7. v2 の example の anchor action（0084 §2.1 の `hold_effective` の起点、0079 §2.5 の「anchor → 最初の step」と
   (b) の起点）を、どの tick の effective とするか

2026-10-01、リポジトリ所有者が7点とも推奨案で承認した（初版のレビューで残った点も含む。§6）。本記録はそれを、
実装が参照できる具体性で書く。
**数値のしきい値・設定値は新しく決めない**（§2.2 の `action_stale_after_ms` は欄を作るだけで、値は実データの後に決める）。

## 2. Decision

### 2.1 step `k` の値は、格子の時刻の as-of で取る（0031 §2.2 と同じ規則）

anchor の ControlTick の時刻を `t_a` とする。step `k`（`k` = 0 .. `steps − 1`）の zone `z` の値は次のとおりとする。

- 時刻 `t_a + k × step_ms` **以前**で直近の ControlTick（同じ source run の中）を取り、その tick の
  `zones.<z>.demand.effective` を値とする。等しい時刻の tick は含める
- 元にした tick の時刻（`ts_ms`）と `tick_id` を step ごとに記録する。zone ごとには記録しない。1つの tick が
  全 zone の値を持つからである
- step 0 は時刻 `t_a` の as-of なので、anchor の tick 自身である
- **ControlTick の時刻が単調でない run からは生成しない**（2026-10-01 所有者承認。PR #212 の Codex P2）。
  「直近」は `ts_ms` の順で決める。これが実行の順と一致するのは、記録した順（`seq`。0071 §2.2a）に並べた
  `ts_ms` が狭義単調増加のときだけである。壁時計が戻った run や、同じ `ts_ms` の tick を持つ run では、後で
  掛かった action や、その時刻に掛かっていなかった action を選びうる（`tick_id` も再起動で 0 に戻る。0071 §2.2）。
  そこで次のとおりにする
  - source run の ControlTick を `seq` の昇順に並べたとき、`ts_ms` が狭義単調増加でなければ、その run からの
    Dataset v2 の生成を**拒否する**。`seq` や `ts_ms` で黙って並べ替えない。example の除外ではなく、生成全体の拒否とする
  - 移行前の行（0071 §2.2a の `legacy_until_ms` 以前の `ts_ms` を持つ ControlTick）を含む run も拒否する。移行前の
    行の `seq` は記録した順を表さず、順序を確かめられないからである。`legacy_until_ms` は移行前の行の `ts_ms` の
    上限（0071 §2.2a）なので、等しい時刻も移行前の行でありうるものとして含める
  - 同じ問題は v1 の builder（`ORDER BY ts_ms, tick_id`）にもある。v1 の扱いは本記録では変えない（§5 #3）
- 値は丸め・補間・clamp をしない。ControlTick の `effective` は既に 0.0..1.0 で検証されている
- 0031 §2.2 の window と同じ規則（「その時点以前の直近観測だけを as-of で使う」「元の時刻を残す」）を
  action にも当てる。window と action で別の時刻対応を持たない

理由: 0031 §2.2 の規則が既にあり、window の各 frame と同じ考え方で action の列を作れる。「その時刻に掛かって
いた値」を素直に表すので、0079 §2.3 の「plan の demand は、その値が effective として掛かったという仮定で
model へ渡す」と同じ意味になる。

### 2.2 action の鮮度: `action_stale_after_ms`（既定値なし。値は実データの後に決める）

- Dataset v2 の spec（`DatasetSpec` v2）に `action_stale_after_ms`（正の整数、ms）を足す。**既定値を置かない。**
  設定（呼び出し側が渡す spec）で毎回明示させる（0031 §2.6「実測前に固定しない値」と AGENTS.md ルール9）
- step `k` について `(t_a + k × step_ms) − 元にした tick の ts_ms ≥ action_stale_after_ms` なら、その anchor の
  example を**作らない**。不等号は window の `stale_mask` の判定（`frame.ts_ms − source_ts ≥ stale_after_ms`）と
  向きを揃える。§2.7 の `prior_action` の元の tick も同じ上限で検査する（`t_a − 元の tick の ts_ms ≥
  action_stale_after_ms` なら作らない）
- 鮮度で作らなかった example の**件数**を Dataset v2 の manifest に記録する。0 件でも 0 と記録する。件数は
  dataset の bytes（manifest）に入るので、同じ DB と spec からは同じ件数になる。どの step で外れたかの内訳は
  記録してよいが、型の必須は件数だけとする（0084 §2.1 で除いた validation example の件数を記録するのと同じ向き）
- 鮮度は格子の時刻でだけ見る。最後の step の区間の終わり（最大の horizon）までと、区間の中で tick が途切れた場合を
  どう扱うかは未決で、段 1 の実装の前に決める（§5 #7）
- 既存の `stale_after_ms`（Telemetry の鮮度）とは共用しない。ControlTick の周期と Telemetry の周期は別物であり、
  片方の都合でもう片方の判定を変えないため
- **実際の値は本記録では決めない。** 実機の ControlTick の周期と、tick の欠け方（再起動・停止の間隔）を
  実データ（#50 / #83 の実機計測）で見てから選ぶ。それまでの試験と合成 dataset では、試験ごとに明示した値を使う

理由: 制御デーモンが止まっていた間など、tick が途切れた区間がある。そこで as-of がずっと前の値を持ち続けると、
実際には掛かっていなかったかもしれない action を「掛かっていた」として学習してしまう。作らずに除けば、
学習データに「確かでない action」が入らない。

### 2.3 step の区間の途中で値が変わっても、step の開始時刻の as-of 値だけを持つ

- step `k` の値は §2.1 の1点だけで決める。区間 `(t_a + k × step_ms, t_a + (k + 1) × step_ms)` の中の
  別の tick は、その step の値に使わない。平均も取らない
- 区間の途中で値が変わった example も除かない

理由: model の入力は「step の間は一定の demand」（`ActionPlan.held`、0052 §2.3）である。学習側も1つの step を
1つの値で表すのが入力の意味と揃う。tick の周期が `step_ms` より短い運用では、途中で値が変わる example は
多くなる。それを除くと、学習データのほとんどを失いうる。平均は、実際には一度も掛からなかった値を作る
（0079 §2.3「補間しない」に反する）。

### 2.4 格子と horizon の関係: horizon は格子の上、格子の終端は最大の horizon

Dataset v2 の spec の検証に、次を加える。

- action の格子は、spec の明示の欄 `action_step_ms`（正）と `action_steps`（正）で持つ。どちらも既定値を置かない
- **各 horizon は `action_step_ms` の整数倍**とする
- **`action_steps × action_step_ms` は最大の horizon と等しい**とする
- 1つでも満たさない spec は拒否する

したがって、各 example の action 列は区間 `[t_a, t_a + action_steps × action_step_ms)` を覆う。horizon `h` の
target が使える step（0079 §2.3 の因果の mask。`k × step_ms < h`）は、ちょうど `h / step_ms` 個になる。
target の許容誤差の分（最大の horizon から `label_end_ms` まで）の action は持たない。どの horizon の予測にも
使えない列だからである（因果の mask の外。0079 の段 1 の行の「`label_end_ms` まで」を置き換える部分。Supersedes）。

理由: 0079 §2.3 の「target schema の horizon 列もこの格子と一致させる」を、spec の段階で検査できる形にした。
最大の horizon より先の列を持つと、使われない列のために example の範囲が延び、作れる example がその分だけ減る。

### 2.5 step の番号と `PlanStep.offset_ms` の対応

- step の番号 `k`（0 始まり）は区間 `[k × step_ms, (k + 1) × step_ms)` に対応する（0079 §2.3 のまま）
- `ActionPlan.steps[k].offset_ms = step_ms × (k + 1)` は、**その区間の終端**（step `k` の action を掛け終える
  時刻 = その step の予測時刻）を指す。`steps[k].demands` は区間 `[k × step_ms, (k + 1) × step_ms)` に掛かる demand である
- したがって Dataset v2 の step `k` の値と `ActionPlan.steps[k]` は、同じ区間の action である。
  `plan.py` の型も意味も変えない。docstring でこの対応を明記するのは段 2 / 段 4 の実装の PR で行う

理由: 既存の `ActionPlan` は「offset は step の予測時刻」として作られている（`offsets_ms` の docstring）。終端と
読めば、0079 §2.3 の区間の定義と矛盾しない。どちらかの型を変える必要がない。

### 2.6 較正の変更の検査は合成の起点で行い、`control/model` には時刻の列だけを渡す

- 0079 §2.3 の「較正の変更をまたぐ学習データは作らない」の検査は、合成の起点 `src/coldaisle/dataset.py`
  （`coldaisle-dataset` の builder と CLI）で行う
- 呼び出し側は 0056 §2.5 の宣言された変更（`DeclaredChange`）を**明示して渡す**。既定値を置かない。
  宣言が無い場合も「空」を明示させる。合成の起点は、そのうち `kind = calibration_changed` の `ts_ms` だけを
  取り出して、`control/model/dataset.py` の検査関数へ整数の列として渡す。`control/model` は `control/drift` を
  import しない
- 検査する期間は、生成した**全 example** の `[history_start_ms の最小, label_end_ms の最大]` とする。その中に
  較正の変更が1つでもあれば、dataset の生成を拒否する。split は dataset の生成より後に決まる。全 example の
  期間は、どの split の集合の期間も含む。したがって 0079 §2.3 の「全 split を通した期間」の条件は、
  これで満たされる（後で purge される example の期間も含むので、0079 より厳しい。狭める向きにだけ厳しい
  ことを、2026-10-01 に所有者が承認した。§6）

理由: `control/drift` は既に `control/model`（`confidence` / `thermal`）を import している。`control/model` から
`control/drift` を import すると循環する。宣言された変更の型を `control/model` へ持ち込むと、drift の型の変更が
dataset の schema に波及する。時刻の列だけを渡せば、どちらの依存も生まない。

### 2.7 v2 の anchor action は、anchor の tick より厳密に前の直近の ControlTick の effective（`prior_action`）

- Dataset v2 の example に `prior_action` を足す。値は、anchor の tick の時刻 `t_a` より**厳密に前**（`ts_ms < t_a`）で
  直近の ControlTick（同じ source run の中）の、zone ごとの `effective` とする。元にした tick の時刻と `tick_id` を記録する
- v2 の anchor action はこの `prior_action` とする。具体的には次の3つがすべて `prior_action` を起点にする
  - v2 の example から作る `ObservedThermalInput` の `action`（`from_example` の v2 版。0084 §2.1 の residual の基準の
    「example の anchor action」）
  - 0084 §2.1 の `hold_effective` の列
  - 0079 §2.5 の「anchor → 最初の step」の変化量と (b) の cell の組（Profile v2 は train の `prior_action` → step 0 から数える）
- step 0 は §2.1 のとおり anchor の tick 自身の effective である（`prior_action` とは別の値になりうる）
- 鮮度は §2.2 と同じ `action_stale_after_ms` で検査する。anchor の tick より前に tick が1つも無い（run の最初の tick
  など）場合も、その example を作らない。どちらも §2.2 の除外の件数に数える
- v1 と同じ `action` の欄（anchor の tick 自身の requested / effective と介入理由。0031 §2.1）は、v2 でも分析用に
  そのまま持つ。ただし v2 の anchor action としては使わない

理由: runtime の anchor 推論が受け取る action は「いま掛かっている effective demand」（0052 §2.2）で、その tick が
demand を決める**前**の値である。v1 の意味（anchor の tick 自身の effective）のままでは、§2.1 の step 0 と同じ値に
なる。すると「anchor → step 0」の変化量が常に 0 になり、0079 §2.5 の (b) は対角の cell だけになる。その結果、
demand を変える候補がすべて評価されなくなる。直前の tick の effective を anchor にすれば、学習と runtime で anchor の
意味が揃う。

### 2.8 変えないこと

- **`ControlTick` の版を上げない。** 段 1 は ControlTick を読むだけで、trace へ何も足さない（0079 §2.9 の段 1 の行のまま）
- v1 の dataset を v2 として読み替えない。v2 は `schema_version` 2 の別の型とし、v1 の builder と artifact は変えない
- 0031 §2.1〜§2.6 の規律（時刻対応・mask・split・source run・値を既定しない）はそのまま当てる

### 2.9 段 1 の試験（すべて `-m "not hardware"`。合成の ControlTick と Telemetry で確かめる）

- **as-of**（§2.1）: tick の時刻が格子の時刻と一致する場合、ずれる場合、同じ時刻に等しい場合のそれぞれで、
  step `k` の値が `t_a + k × step_ms` 以前で直近の tick の `effective` になり、元の tick の時刻と `tick_id` が
  記録される。step 0 は anchor の tick 自身になる。`requested` が `effective` と違う tick で、`effective` が使われる
- **鮮度**（§2.2）: 格子の時刻と元の tick の差が `action_stale_after_ms` 未満なら example ができる。ちょうど
  等しい、または超える step が1つでもあれば、その anchor の example はできない。`action_stale_after_ms` を
  持たない spec は型にならない（既定値が無い）
- **step 内の変化**（§2.3）: 区間の途中で `effective` が変わる tick 列でも example ができる。値は区間の開始の
  as-of 値であり、途中の tick の値や平均ではない
- **格子と horizon**（§2.4）: `action_step_ms` の整数倍でない horizon、`action_steps × action_step_ms` が最大の
  horizon と違う spec が、それぞれ拒否される。example の action 列の長さは `action_steps` に等しく、最大の
  horizon から `label_end_ms` までの列を持たない
- **step の番号**（§2.5）: Dataset v2 の step `k` の区間が `ActionPlan.held(..., step_ms, steps)` の `steps[k]` と
  同じ区間であることを、格子の時刻と `offset_ms − step_ms` の一致で確かめる
- **時刻が単調でない run**（§2.1）: `seq` の順で `ts_ms` が戻る run、同じ `ts_ms` の tick を2つ持つ run、
  `legacy_until_ms` 以前（等しい時刻を含む）の ControlTick を含む run からの生成が、それぞれ拒否される。
  単調な run は通る。v1 の builder の出力は変わらない
- **較正**（§2.6）: 全 example の期間の中に `calibration_changed` の宣言があると、生成が拒否される。期間の外
  なら通る。`calibration_changed` 以外の宣言では拒否しない。宣言を渡さない呼び出しは型にならない。
  `control/model` が `control/drift` を import しないことを、既存の import 走査試験の方式で確かめる
- **`prior_action`**（§2.7）: `prior_action` が `t_a` より厳密に前で直近の tick の `effective` になり、元の tick の
  時刻と `tick_id` が記録される。anchor の tick と同じ時刻の別の tick は使わない。step 0（anchor の tick 自身）と
  違う値になる tick 列で、v2 の example から作る `ObservedThermalInput` の `action` が `prior_action` と一致する。
  前に tick が無い anchor と、`prior_action` の元の tick が `action_stale_after_ms` 以上古い anchor では、example が
  できない
- **除外の件数**（§2.2 / §2.7）: step の鮮度、`prior_action` の鮮度、前の tick が無いことで作らなかった example の
  件数が manifest に記録され、合計がそれぞれの場合の数と一致する。除外が無ければ 0 と記録される。件数を持たない
  manifest は型にならない
- **決定性**: 同じ DB と spec から、同じ bytes の Dataset v2 ができる。manifest の `examples_sha256` と除外の件数が一致する
- **v1 を読み替えない**: v1 の manifest / example を v2 の型で読むと拒否される。v1 の builder の出力は変わらない

## 3. Consequences

良くなること。

- 同じ run から、実装によらず同じ action 列ができる。0084 の step ごとの support と residual の基準が、
  同じ意味の action を数える
- tick が途切れた区間の「確かでない action」を学習しない
- horizon と格子のずれを、spec の段階で拒否できる
- 学習と runtime で anchor action の意味（いま掛かっている effective）が揃い、「anchor → 最初の step」の照合が意味を持つ
- 鮮度で除いた件数が manifest に残り、除外がどれだけ効いたかを後から確かめられる
- `control/model` と `control/drift` の依存が増えない

悪くなること（と緩和策）。

| 悪くなること | 緩和策 |
|---|---|
| tick の周期が `step_ms` より短いと、区間の途中の action の変化を学習データが表さない | model の入力そのものが「step の間は一定」なので、入力と学習の意味は揃う。周期と `step_ms` の選び方は実データ（0079 §5 #5）で決める |
| `action_stale_after_ms` の値が決まるまで、実機の Dataset v2 は作れない | 0079 §5 #5 / 0050 §5 #2 のとおり、実機の値で作るのは実データの後である。試験と合成 dataset は明示の値で進められる |
| horizon が `step_ms` の整数倍に縛られる | 0079 §2.3 が既に horizon と格子の一致を求めている。新しい制約ではない |
| 較正の検査が全 example の期間で行われ、purge される example の期間の中の変更でも拒否される | 狭める向きにだけ厳しい。窓を分けて作り直すのは人の判断（0079 §2.3 のまま） |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| step の区間の中に tick が1つも無ければ example を拒否する（as-of を使わない） | tick の周期が `step_ms` より長い運用では、ほとんどの example が作れない。鮮度は §2.2 の明示の上限で見るほうが、意図（確かでない action を入れない）を直接表す |
| 格子の時刻の後で最も近い tick（区間の終端の値）を使う | 0031 §2.2 の「その時点以前の直近」と逆向きになる。区間の後半にしか掛かっていない値を、区間全体の値として扱う |
| 鮮度に既存の `stale_after_ms` を共用する | Telemetry と ControlTick の周期は別物で、片方を変えるともう片方の判定が変わる |
| 鮮度に既定値を置く | 実データの前に数値を決めることになる（AGENTS.md ルール9、0031 §2.6） |
| 区間の途中で値が変わった example を除く | tick の周期が `step_ms` より短いと、学習データのほとんどを失う |
| 区間の中の tick の値を平均する | 一度も掛からなかった値を作る（0079 §2.3「補間しない」） |
| action 列を `label_end_ms` まで持つ（0079 の段 1 の文言のまま） | 許容誤差の分の列は因果の mask の外で、どの予測にも使えない。example の範囲が延びて件数が減る |
| `PlanStep.offset_ms` を区間の始端（`step_ms × k`）に変える | `ActionPlan` の検証・digest・既存の試験と、`offsets_ms` の docstring の「予測時刻」の意味を変える。終端と読めば矛盾しない |
| `control/model/dataset.py` が `DeclaredChange` を直接受け取る | `control/drift` が `control/model` を import しているので循環する。drift の型の変更が dataset の schema に波及する |
| v2 の anchor action を v1 のまま anchor の tick 自身の effective とし、0079 §2.5 の「anchor → 最初の step」の照合を外す | runtime の anchor（いま掛かっている effective）と学習の anchor の意味が違うままになる。0079 の照合を外すと、anchor の effective から plan の最初の step への跳びが、範囲の両端の値だけで評価されてしまう（0079 §2.5 で足した理由そのもの） |
| `prior_action` を anchor の tick と同じ時刻の tick まで含めて取る | 同じ時刻の tick は anchor の tick 自身（または同時刻の別の決定）であり、「決める前の値」にならない |
| 鮮度で除いた件数を記録しない | 除外が増えても dataset から見えず、`action_stale_after_ms` の値を選ぶ根拠が残らない |
| 較正の検査を split の後（学習の段）で行う | 0079 §2.3 は「Dataset v2 の生成は拒否する」と決めている。生成された dataset が較正をまたいでいれば、後で何に使っても同じ問題が残る |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | **決着**（§2.7）: v2 の anchor action は、anchor の tick より厳密に前で直近の ControlTick の effective（`prior_action`）とし、元の tick を記録し、`action_stale_after_ms` で鮮度を検査する（2026-10-01 所有者承認） | 決着（本記録 §2.7） |
| 2 | **決着**（§2.2）: 鮮度で作らなかった example の件数を manifest に記録する（2026-10-01 所有者承認） | 決着（本記録 §2.2） |
| 3 | **v2 は決着**（§2.1。2026-10-01 所有者承認）: `seq` の順の `ts_ms` が狭義単調増加でない run と、移行前の行を含む run からの Dataset v2 の生成を拒否する。**v1 の builder の同じ問題は開いたまま**: v1 の builder（`ORDER BY ts_ms, tick_id`）は時刻が単調でない run でも anchor の tick を `ts_ms` の順に選ぶ。v1 にも同じ拒否を足すかは、v1 の dataset を作り直す影響（既存 artifact の再生成で bytes が変わりうる）とあわせて別に決める。以下は初版の記録。§2.1 の as-of の前提（`seq` の順に並べた ControlTick の `ts_ms` が狭義単調増加）が崩れた run をどう扱うか。**推奨案**: その run からの Dataset v2 の生成を拒否する（fail closed。黙って並べ替えない）。`seq` を持たない移行前の行（0071 §2.2a の `legacy_until_ms` より前）を含む run も、順序を確かめられないので拒否する。代替案: `seq` の順で「直近」を選ぶ（壁時計が戻った区間では、格子の時刻と action の時刻の対応自体が崩れるので推奨しない）。v1 の builder（`ORDER BY ts_ms, tick_id`）にも同じ問題があるが、v1 を変えるかは別に決める | v2 は決着（本記録 §2.1）。v1 は #83 で別の Issue コメントまたは記録として扱う |
| 4 | `action_stale_after_ms` の値 | 実機の ControlTick の周期と欠け方を見た後（#50 / #83） |
| 5 | step ごとの context（mode・Safety の状態など）を持つか。本記録は step ごとに値と元の tick だけを持ち、context は anchor の tick のものだけとする（0031 §2.1 のまま） | 必要になったら新しい記録。Shadow・評価（#90 / #91）で Guard / Safety が値を変えた step の扱いが問題になった時 |
| 6 | 0079 §5 #5（window / horizon / step 格子の実値）と 0050 §5 #2（support 軸と bin の境界）は変わらず開いたまま | 実機 dataset（#50 / #83）の後 |
| 7 | **段 1 の実装の前に決める必要がある**（PR #212 の 6d584e6 への Codex P2）。§2.2 の鮮度は格子の時刻（各 step の開始）でしか見ない。最後の step の開始時点で新しい tick があっても、その後 `coldaisle-fand` が止まり、最大の horizon までの間に外部の deadman / 引き継ぎが Fan を Max にすると、dataset は最後の step の開始の値を持ち続ける。target は分からない（違う）action の下で学習される。途中の step の区間の中で止まって再開した場合も、次の格子の時刻に新しい tick があれば見逃す。**推奨案**: `prior_action` の元の tick から `t_a + steps × step_ms`（最大の horizon）までの間で、`seq` の順に隣り合う ControlTick の時刻の差と、最後の tick から最大の horizon までの差が、すべて `action_stale_after_ms` 未満であることを求める（tick の連続の証拠）。外れた example は作らず、§2.2 の除外件数に数える。§2.3 のとおり区間の中の tick の**値**は使わず、tick の**存在**だけを見る。代替案: 格子の時刻に加えて最大の horizon の時刻でも as-of の鮮度を見る（終端だけを塞ぎ、区間の中の停止は見逃す） | 所有者の判断。決まったら本 PR のレビュー中の修正、またはマージ後なら新しい記録 |

## 6. 所有者の決定（2026-10-01）

2026-10-01、#83 の段 1 に入る前に示した6点を、リポジトリ所有者が推奨案で承認した。

- **1. as-of** → 格子の時刻 `t_a + k × step_ms` 以前で直近の ControlTick の effective を使い、元の tick の時刻を記録する（§2.1）
- **2. 鮮度** → spec に既定値の無い `action_stale_after_ms` を足して明示させ、超えた example は作らない。値は今は決めず、
  実データから後で選ぶ（§2.2）
- **3. step 内の変化** → step の開始時刻の as-of 値だけを持つ（§2.3）
- **4. 格子と horizon** → 各 horizon を `step_ms` の整数倍、`steps × step_ms` を最大の horizon と等しくすることを spec の検証で求める（§2.4）
- **5. step の番号** → index `k` は区間 `[k, k + 1)`、`offset_ms` はその終端（§2.5）
- **6. 較正の検査** → 合成の起点の `src/coldaisle/dataset.py` で行い、`control/model` には時刻の列だけを渡す（§2.6）

あわせて、記録を先に作ってマージし、その後に段 1（#83）を実装する順で進めることも承認した。

### 追加で承認した点（2026-10-01）

本記録の初版（PR #212）のレビューで残した4点を、同日、リポジトリ所有者が推奨案で承認した。

- **anchor action の起点** → anchor の tick より厳密に前で直近の ControlTick の effective を `prior_action` として持ち、
  v2 の anchor にする。元の tick を記録し、`action_stale_after_ms` で鮮度を検査する（§2.7。初版の §5 #1）
- **較正の検査の期間** → 全 example の期間で検査する（0079 の「全 split を通した期間」より厳しい。§2.6）
- **除外の件数** → 鮮度で作らなかった example の件数を manifest に記録する（§2.2。初版の §5 #2）
- **0079 の扱い** → 0079 §2.9 の段 1 の行のうち、action 列の範囲の文言だけを部分的に置き換える（Supersedes。§2.4）

同日、PR #212 のレビュー（6d584e6 時点の §5 #3。Codex P2）を受けて、`seq` の順の `ts_ms` が狭義単調増加でない run と、
移行前の行を含む run からの Dataset v2 の生成を拒否することを承認した。黙って並べ替えない。v1 の builder の同じ問題は
開いたまま別に扱う（§2.1、§5 #3）。
