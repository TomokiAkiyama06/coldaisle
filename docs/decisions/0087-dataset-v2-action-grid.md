# 決定記録 0087: Thermal Dataset v2 の action 列を格子へ写す規則（as-of・鮮度と連続・step 内の変化・格子と horizon・target の時刻・step の番号・較正の変更の検査の置き場所）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-01、リポジトリ所有者が承認）
- **Date**: 2026-10-01
- **Supersedes**: [0079](0079-model-artifact-formats.md) の次の部分だけを置き換える。0079 の他の点は有効。
  - §2.9 の段 1 の行のうち、action 列の範囲を「anchor から `label_end_ms` まで」とする部分。本記録 §2.4 で
    格子 `[anchor, anchor + steps × step_ms)` に置き換える（v2 の `label_end_ms` は `anchor + 最大の horizon`。§2.4）
  - 本記録が足すだけで 0079 を置き換えない点: 段 1 の action 列を格子へ写す規則（§2.1〜§2.3）、step の番号と
    `PlanStep.offset_ms` の対応の明文化（§2.5）、較正の変更の検査を置く場所（§2.6）

  [0031](0031-thermal-dataset-contract.md) の次の部分だけを、**Dataset v2 についてだけ**置き換える。v1 には 0031 を
  そのまま適用する。0031 の他の点は有効。
  - §2.2 の「target は期待時刻に最も近い実観測を、明示された許容誤差内でだけ採用する。同距離なら過去側を選ぶ」の
    うち、期待時刻より**後ろ**の観測を採りうる部分。v2 は期待時刻**以前**の観測だけを採る（§2.4）。これに伴い、
    v2 の `label_end_ms` は「最大の horizon + 許容誤差」ではなく `anchor + 最大の horizon` とする

  旧記録（0079 / 0031）側への `Superseded by` の追記は本 PR で行った。
- **関連**: [0031](0031-thermal-dataset-contract.md) §2.1 / §2.2 / §2.3 / §2.6 /
  [0048](0048-thermal-model-artifact-and-inference.md) §2.1 /
  [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.2 / §2.3 / §2.4 /
  [0056](0056-model-drift-detection-and-retraining-triggers.md) §2.5 /
  [0060](0060-control-loop-runtime.md) §2.6 / §2.7 /
  [0071](0071-control-trace-read-api.md) §2.2 / §2.2a /
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
  - 移行前の行を含む run も拒否する。移行前の行の `seq` は migration が `(ts_ms, tick_id)` の順に振ったもので
    （`0007_control_trace_seq.sql`）、記録した順を表さず、順序を確かめられないからである
  - **移行前の行は時刻ではなく `seq` で見分ける**（2026-10-01 所有者承認。PR #212 の 3165109 への Codex P1。§5 #12）。
    新しい migration で `control_trace_prune` に `legacy_through_seq`（移行前の行の `seq` の上限。0 以上の整数）を足す
    - 行が無い DB に適用するときは 0 とする（0007 と同時に適用する新しい DB を含む）
    - 0007 を適用済みの DB では、`ts_ms ≤ legacy_until_ms` の行の `MAX(seq)`（行が無ければ 0）で埋める。0007 の後に
      同じ時刻で記録された行を含みうるが、狭める向き（拒否が増える向き）にだけずれる安全側の上限である
    - Dataset v2 は、`seq ≤ legacy_through_seq` の ControlTick を含む run からの生成を拒否する。0031 §2.3 のとおり
      dataset 用の DB は run の前に新しく作るので、通常は 0 で、どの run も拒否されない
    - **この migration は段 1（#83）の実装 PR に同梱する。** 本記録は規則だけを決め、migration を書かない
    - 初版で確認した「`ts_ms ≤ legacy_until_ms` の行を含む run を拒否」という読み方は、Dataset v2 については
      これで置き換える（§6）。理由: 空の DB でも 0007 が `legacy_until_ms` をストアの時計（`:now_ms`）で書くので、
      `SimulatedClock` が進む前に記録した最初の tick がその時刻と等しくなり、正しい mock / replay の run を拒否する
      （AGENTS.md ルール7 の経路を壊す）。0071 §2.2a の `legacy_until_ms` の意味と、読み取り API での使い方は変えない
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
- 作らなかった example の**件数を理由ごとに** Dataset v2 の manifest に記録する（2026-10-01 所有者承認。§5 #8）。
  理由は次の5つで、どれも 0 件なら 0 と記録する。1つの example が複数の理由に当たるときは、下の順で最初に当たった
  理由だけに数える（合計が除いた example の数と一致するように）
  1. **鮮度**: 格子の時刻の as-of の tick が古い、`prior_action` の元の tick が古い、または anchor の tick より前に
     tick が無い（§2.7）
  2. **連続**: 下の「tick の連続」の検査に外れた
  3. **区間内の変化**: §2.3 の検査に外れた
  4. **再起動**: 下の「再起動をまたがない」の検査に外れた（`tick_id` の減少・同値）
  5. **欠番**: 下の「`tick_id` の欠番を許さない」の検査に外れた（`tick_id` の 2 以上の増加）

  件数は dataset の bytes（manifest）に入るので、同じ DB と spec からは同じ件数になる。どの step で外れたかの
  内訳は記録してよいが、型の必須は理由ごとの件数だけとする（0084 §2.1 で除いた validation example の件数を
  記録するのと同じ向き）。件数は `step_ms` と `action_stale_after_ms` の選び方の根拠にも使う
- **tick の連続も求める**（2026-10-01 所有者承認。PR #212 の 6d584e6 への Codex P2）。格子の時刻だけを見ると、
  最後の step の区間の途中で `coldaisle-fand` が止まり、外部の deadman / 引き継ぎが Fan を Max にした場合や、
  途中の区間の中で止まって再開した場合を見逃す。そこで、`prior_action`（§2.7）の元の tick から最大の horizon
  `t_a + steps × step_ms` までの間で、次の差が**すべて** `action_stale_after_ms` 未満であることを求める
  - `seq` の順に隣り合う ControlTick の `ts_ms` の差（§2.1 で `ts_ms` は `seq` の順に狭義単調増加である）
  - 最後の ControlTick（最大の horizon 以前で直近）の `ts_ms` から、最大の horizon までの差

  1つでも満たさなければ、その example を作らず、除外件数の「連続」に数える。区間の中の tick は**存在だけ**を見て、
  値は使わない（§2.3）。格子の時刻ごとの as-of の鮮度の検査（上の項）は、この検査に含まれるが、別に残してよい
- **再起動をまたがない**（2026-10-01 所有者承認。PR #212 の 023e55c への Codex P2）。`coldaisle-fand` が
  `action_stale_after_ms` より短い間に再起動すると、上の連続の検査は時刻の差しか見ないので通る。しかし古い process の
  最後の tick と新しい process の最初の tick の間に、systemd の引き継ぎ（0080）が Fan を Max や BIOS の制御へ切り替え
  うる。その action は trace に無く、直前の demand として学習されてしまう。そこで、`prior_action` の元の tick から
  最大の horizon `t_a + steps × step_ms` までの間で、`seq` の順に隣り合う ControlTick の `tick_id` が**狭義単調増加**で
  あることを求める。`tick_id` は再起動で 0 に戻る（`control/loop.py`）ので、減少または同値は再起動とみなす。
  満たさなければ、その example を作らず、除外件数の「再起動」に数える。**trace の版は上げない**（既存の `tick_id`
  だけを使う。§2.8）
- **`tick_id` の欠番を許さない**（2026-10-01 所有者承認。PR #212 の 3165109 への Codex P1。§5 #11）。trace の保存に
  失敗しても制御は続く（`ControlLoop._record`、0060 §2.6）。`tick_id` は保存の成否によらず tick ごとに1つ進むので、
  保存された隣り合う行の `tick_id` が `n` と `n + 2` になりうる。保存されなかった tick は別の demand を掛けて
  戻したかもしれず、時刻の差しか見ない連続の検査も、保存された tick だけを見る §2.3 の検査も通ってしまう。そこで、
  上の「再起動をまたがない」を強め、`prior_action` の元の tick から最大の horizon までの間で、`seq` の順に隣り合う
  ControlTick の `tick_id` が**ちょうど 1 ずつ**増えることを求める
  - 差が 0 以下（減少・同値）なら「再起動」、2 以上なら「欠番」に数える。両者を分けるのは、trace の保存の失敗の量を
    再起動と混ぜずに見えるようにするためである
  - **trace の版は上げない**（既存の `tick_id` と `seq` だけを使う。§2.8）
  - **検査は最大の horizon より後の最初の ControlTick まで1つ延ばす**（2026-10-01 所有者承認。§5 #13）。範囲の中の
    隣り合う組だけでは、範囲の中で最後に保存された tick（`tick_id = n`）と最大の horizon の間の、保存されなかった tick
    `n + 1` や再起動が見えない（連続の検査は時刻の差しか見ない）。そこで、最大の horizon より後で `seq` の順に最初の
    ControlTick の `tick_id` が `n + 1` であることも求める。その tick の値は使わない（as-of の定義から最大の horizon
    より後であり、どの step の値にもならない）
    - その tick の `tick_id` が `n` 以下なら「再起動」、`n + 2` 以上なら「欠番」に数える
    - その tick がまだ無い（run の末尾）example は作らず、「連続」に数える
    - 最大の horizon の直後に `coldaisle-fand` が止まった（または再起動した）run の example も除かれる。止まったのが
      最大の horizon の前か後かを trace から区別できないので、安全側として受け入れる（2026-10-01 所有者承認）
- 既存の `stale_after_ms`（Telemetry の鮮度）とは共用しない。ControlTick の周期と Telemetry の周期は別物であり、
  片方の都合でもう片方の判定を変えないため
- **実際の値は本記録では決めない。** 実機の ControlTick の周期と、tick の欠け方（再起動・停止の間隔）を
  実データ（#50 / #83 の実機計測）で見てから選ぶ。それまでの試験と合成 dataset では、試験ごとに明示した値を使う

理由: 制御デーモンが止まっていた間など、tick が途切れた区間がある。そこで as-of がずっと前の値を持ち続けると、
実際には掛かっていなかったかもしれない action を「掛かっていた」として学習してしまう。作らずに除けば、
学習データに「確かでない action」が入らない。

### 2.3 step の値は開始時刻の as-of 値。区間の中で値が変わった example は作らない

- step `k` の値は §2.1 の1点（区間の開始時刻の as-of）だけで決める。平均も取らない
- 区間 `[t_a + k × step_ms, t_a + (k + 1) × step_ms)` の中にある ControlTick の `effective` が、**どれか1つの zone
  でも** step `k` の値と違えば、その example を**作らない**。除外件数の「区間内の変化」に数える（§2.2）。全 step に
  ついて検査する（2026-10-01 所有者承認。PR #212 の 7b2783b への Codex P1。初版の「途中で値が変わった example も
  除かない」を置き換えた。§6）
- 値の比較は丸めずに行う（ControlTick に記録された `effective` の値そのもの）。許容幅は置かない

理由: model の入力は「step の間は一定の demand」（`ActionPlan.held`、0052 §2.3）である。区間の途中で値が変わった
example を残すと、target は入力に無い軌道の結果になる。同じ action の特徴量に別の軌道で生じた label が付き、
係数が MPC の使う「step の間は保つ」という反実仮想の意味を支えなくなる。平均は、実際には一度も掛からなかった
値を作る（0079 §2.3「補間しない」に反する）。tick の周期が `step_ms` より短い運用では除外が増えうるが、理由ごとの
件数でその量が見える。tick の周期を `step_ms` と同じに選べば、この除外は起きない（`step_ms` の実値は 0079 §5 #5）。

### 2.4 格子と horizon の関係: horizon は格子の上、格子の終端は最大の horizon

Dataset v2 の spec の検証に、次を加える。

- action の格子は、spec の明示の欄 `action_step_ms`（正）と `action_steps`（正）で持つ。どちらも既定値を置かない
- **各 horizon は `action_step_ms` の整数倍**とする
- **`action_steps × action_step_ms` は最大の horizon と等しい**とする
- 1つでも満たさない spec は拒否する

したがって、各 example の action 列は区間 `[t_a, t_a + action_steps × action_step_ms)` を覆う。horizon `h` の
target が使える step（0079 §2.3 の因果の mask。`k × step_ms < h`）は、ちょうど `h / step_ms` 個になる。
最大の horizon より後の action は持たない（0079 の段 1 の行の「`label_end_ms` まで」を置き換える部分。Supersedes）。

**target は期待時刻以前の観測だけから採る**（2026-10-01 所有者承認。PR #212 の 7b2783b への Codex P1。0031 §2.2 の
v2 についての部分的な置き換え。Supersedes）。

- horizon `h` の target は、`t_a + h − target_tolerance_ms ≤ source_ts ≤ t_a + h` の観測のうち、期待時刻 `t_a + h` に
  最も近いもの（すなわち範囲の中で最も遅いもの）を採る。範囲の中に観測が無ければ、0031 §2.1 のとおり欠測
  （`missing_mask`）とする
- target は 0031 §2.2 のとおり anchor より後（`source_ts > t_a`）に限る。`target_tolerance_ms` は 0031 の spec の
  検証（最短の horizon より小さい）のままなので、この条件は範囲の下端で常に満たされる
- v2 の `label_end_ms` は `t_a + 最大の horizon` とする（v1 の「+ 許容誤差」を持たない）。§2.6 の較正の検査の期間と
  0031 §2.5 の split の判定も、この `label_end_ms` を使う

理由: 期待時刻より後ろの観測を採ると、`t_a + h` から観測時刻までの action が label に効くのに、因果の mask で
model の入力には無い。期待時刻以前に限れば、label に効く action はすべて入力の中にある。代わりに
`[source_ts, t_a + h)` の action は入力にあって label に効かないことがあるが、入力に無い action が label に効くより
安全側である（入力に無い操作を学習しない）。

理由: 0079 §2.3 の「target schema の horizon 列もこの格子と一致させる」を、spec の段階で検査できる形にした。
最大の horizon より先の列を持つと、使われない列のために example の範囲が延び、作れる example がその分だけ減る。
この節の target の規則により、最大の horizon より先の action は、どの label にも効かない。

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
  など）場合も、その example を作らない。どちらも §2.2 の除外件数の「鮮度」に数える
- v1 と同じ `action` の欄（anchor の tick 自身の requested / effective と介入理由。0031 §2.1）は、v2 でも分析用に
  そのまま持つ。ただし v2 の anchor action としては使わない

理由: runtime の anchor 推論が受け取る action は「いま掛かっている effective demand」（0052 §2.2）で、その tick が
demand を決める**前**の値である。v1 の意味（anchor の tick 自身の effective）のままでは、§2.1 の step 0 と同じ値に
なる。すると「anchor → step 0」の変化量が常に 0 になり、0079 §2.5 の (b) は対角の cell だけになる。その結果、
demand を変える候補がすべて評価されなくなる。直前の tick の effective を anchor にすれば、学習と runtime で anchor の
意味が揃う。

### 2.8 変えないこと

- **`ControlTick` の版を上げない。** 段 1 は ControlTick を読むだけで、trace へ何も足さない（0079 §2.9 の段 1 の行のまま）。
  段 1 が store に足すのは、`control_trace_prune` の `legacy_through_seq` の migration だけである（§2.1）。
  `control_traces` の表と `legacy_until_ms` の意味は変えない
- v1 の dataset を v2 として読み替えない。v2 は `schema_version` 2 の別の型とし、v1 の builder と artifact は変えない
- 0031 §2.1〜§2.6 の規律（時刻対応・mask・split・source run・値を既定しない）はそのまま当てる

### 2.9 段 1 の試験（すべて `-m "not hardware"`。合成の ControlTick と Telemetry で確かめる）

- **as-of**（§2.1）: tick の時刻が格子の時刻と一致する場合、ずれる場合、同じ時刻に等しい場合のそれぞれで、
  step `k` の値が `t_a + k × step_ms` 以前で直近の tick の `effective` になり、元の tick の時刻と `tick_id` が
  記録される。step 0 は anchor の tick 自身になる。`requested` が `effective` と違う tick で、`effective` が使われる
- **鮮度**（§2.2）: 格子の時刻と元の tick の差が `action_stale_after_ms` 未満なら example ができる。ちょうど
  等しい、または超える step が1つでもあれば、その anchor の example はできない。`action_stale_after_ms` を
  持たない spec は型にならない（既定値が無い）
- **step 内の変化**（§2.3）: 区間の中の tick の `effective` が1つの zone だけ step の開始の値と違う example ができず、
  除外件数の「区間内の変化」に数えられる。区間の中の tick がすべて同じ値なら example ができ、step の値は開始の
  as-of 値になる。区間の終端（次の step の開始時刻）の tick の値の変化は、その step の変化に数えない
- **格子と horizon**（§2.4）: `action_step_ms` の整数倍でない horizon、`action_steps × action_step_ms` が最大の
  horizon と違う spec が、それぞれ拒否される。example の action 列の長さは `action_steps` に等しい
- **target の時刻**（§2.4）: 期待時刻より後ろにしか観測が無い horizon は欠測になる（後ろの観測が前の観測より近くても
  採らない）。期待時刻以前で許容誤差の中の観測のうち最も遅いものが採られる。`label_end_ms` が `t_a + 最大の horizon`
  になる。v1 の builder の target の採り方は変わらない
- **step の番号**（§2.5）: Dataset v2 の step `k` の区間が `ActionPlan.held(..., step_ms, steps)` の `steps[k]` と
  同じ区間であることを、格子の時刻と `offset_ms − step_ms` の一致で確かめる
- **時刻が単調でない run**（§2.1）: `seq` の順で `ts_ms` が戻る run、同じ `ts_ms` の tick を2つ持つ run、
  `seq ≤ legacy_through_seq` の ControlTick を含む run からの生成が、それぞれ拒否される。単調な run は通る。
  v1 の builder の出力は変わらない
- **移行前の行の見分け方**（§2.1。migration は段 1 の実装 PR に同梱）: 行が無い DB に migration を適用すると
  `legacy_through_seq` が 0 になる。0007 の適用時に行があった DB では、`ts_ms ≤ legacy_until_ms` の行の `MAX(seq)` に
  なる。新しい DB で、`SimulatedClock` が進む前に `ts_ms = legacy_until_ms` の tick を記録した run が拒否されない。
  `legacy_until_ms` の値と 0071 の読み取り API の結果は migration の前後で変わらない
- **較正**（§2.6）: 全 example の期間の中に `calibration_changed` の宣言があると、生成が拒否される。期間の外
  なら通る。`calibration_changed` 以外の宣言では拒否しない。宣言を渡さない呼び出しは型にならない。
  `control/model` が `control/drift` を import しないことを、既存の import 走査試験の方式で確かめる
- **`prior_action`**（§2.7）: `prior_action` が `t_a` より厳密に前で直近の tick の `effective` になり、元の tick の
  時刻と `tick_id` が記録される。anchor の tick と同じ時刻の別の tick は使わない。step 0（anchor の tick 自身）と
  違う値になる tick 列で、v2 の example から作る `ObservedThermalInput` の `action` が `prior_action` と一致する。
  前に tick が無い anchor と、`prior_action` の元の tick が `action_stale_after_ms` 以上古い anchor では、example が
  できない
- **tick の連続**（§2.2）: 格子の時刻ごとの tick は新しいが、最後の step の区間の途中で tick が途切れ、最後の tick から
  最大の horizon までの差が `action_stale_after_ms` 以上になる example ができない。途中の step の区間の中で、隣り合う
  tick の差が `action_stale_after_ms` 以上になる（次の格子の時刻の前に再開する）example もできない。`prior_action` の
  元の tick と anchor の tick の差も同じく検査される。差がすべて未満なら example ができる
- **再起動**（§2.2）: `prior_action` の元の tick から最大の horizon までの間で、`seq` の順に `tick_id` が減る
  （0 に戻る）tick 列、同じ `tick_id` が続く tick 列で、時刻の差がすべて `action_stale_after_ms` 未満でも example が
  できず、除外件数の「再起動」に数えられる。最大の horizon より後の最初の tick の `tick_id` が 0 に戻る tick 列も
  同じく除外される。検査の対象の外（`prior_action` の元の tick より前、または最大の horizon より後の最初の tick より
  後）の再起動では除外されない
- **欠番**（§2.2）: `prior_action` の元の tick から最大の horizon までの間で、`seq` の順に隣り合う行の `tick_id` が
  `n` と `n + 2` になる tick 列（保存に失敗した tick がある）で、時刻の差がすべて `action_stale_after_ms` 未満で
  区間内の値も変わらなくても example ができず、除外件数の「欠番」に数えられる（「再起動」には数えない）。範囲の中で
  最後に保存された tick が `n` で、最大の horizon より後の最初の tick が `n + 2` の tick 列（終端の欠番）も同じく除外
  される。検査の対象の外の欠番では除外されない。最大の horizon より後の最初の tick まで `tick_id` がちょうど 1 ずつ
  増えるなら example ができる
- **run の末尾**（§2.2）: 最大の horizon より後に ControlTick が1つも無い example はできず、除外件数の「連続」に
  数えられる。その tick の値が step の値と違っても、区間内の変化には数えない（値を使わない）
- **除外の件数**（§2.2 / §2.3 / §2.7）: 「鮮度」「連続」「区間内の変化」「再起動」「欠番」の理由ごとの件数が manifest に記録され、
  それぞれの場合の数と一致する。複数の理由に当たる example は §2.2 の順で最初の理由だけに数えられる。除外が無ければ
  すべて 0 と記録される。理由ごとの件数を持たない manifest は型にならない
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
- trace の保存に失敗した tick が掛けたかもしれない action を学習せず、その量が「欠番」の件数として再起動と分けて見える
- 移行前の行を `seq` で見分けるので、新しい DB の mock / replay の run が、最初の tick の時刻だけで拒否されない

悪くなること（と緩和策）。

| 悪くなること | 緩和策 |
|---|---|
| tick の周期が `step_ms` より短く値がよく変わる運用では、区間内の変化で除く example が増える | 理由ごとの件数で量が見える。tick の周期を `step_ms` と同じに選べば起きない。周期と `step_ms` の選び方は実データ（0079 §5 #5）で決める |
| target を期待時刻以前に限るので、許容誤差の中の後ろ側にしか観測が無い horizon は欠測になる | 欠測は 0031 §2.1 の mask で明示される。label に入力の外の action が効くより安全側である |
| `action_stale_after_ms` の値が決まるまで、実機の Dataset v2 は作れない | 0079 §5 #5 / 0050 §5 #2 のとおり、実機の値で作るのは実データの後である。試験と合成 dataset は明示の値で進められる |
| horizon が `step_ms` の整数倍に縛られる | 0079 §2.3 が既に horizon と格子の一致を求めている。新しい制約ではない |
| 較正の検査が全 example の期間で行われ、purge される example の期間の中の変更でも拒否される | 狭める向きにだけ厳しい。窓を分けて作り直すのは人の判断（0079 §2.3 のまま） |
| trace の保存の失敗が多い運用では、欠番で除く example が増える | 「欠番」の件数で量が見える。保存の失敗は 0060 §2.6 / §2.7 のとおりログにも出る（`trace_failed` / `trace_dropped`）ので、原因を直すのが先である |
| 段 1 で store の migration が1つ増える（`legacy_through_seq`） | 列を1つ足すだけで、`control_traces` の表と `legacy_until_ms` の意味は変えない。既存の DB では安全側の上限で埋める |
| run の末尾と、最大の horizon の直後に止まった run の example が作られない | 停止が最大の horizon の前か後かを区別できないので安全側に除く。件数は「連続」「再起動」で見える |
| 0007 を適用済みの DB では、0007 の直後に `legacy_until_ms` と同じ時刻で記録した正しい行も移行前の行として数える | 拒否が増える向きにだけずれる。dataset 用の DB は run の前に新しく作る（0031 §2.3）ので、通常は 0 になる |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| step の区間の中に tick が1つも無ければ example を拒否する（as-of を使わない） | tick の周期が `step_ms` より長い運用では、ほとんどの example が作れない。鮮度は §2.2 の明示の上限で見るほうが、意図（確かでない action を入れない）を直接表す |
| 格子の時刻の後で最も近い tick（区間の終端の値）を使う | 0031 §2.2 の「その時点以前の直近」と逆向きになる。区間の後半にしか掛かっていない値を、区間全体の値として扱う |
| 鮮度に既存の `stale_after_ms` を共用する | Telemetry と ControlTick の周期は別物で、片方を変えるともう片方の判定が変わる |
| 鮮度に既定値を置く | 実データの前に数値を決めることになる（AGENTS.md ルール9、0031 §2.6） |
| 区間の途中で値が変わった example も除かない（初版の §2.3） | target が入力に無い軌道の結果になり、同じ特徴量に別の軌道の label が付く（PR #212 の Codex P1） |
| 区間の中の tick の値をすべて入力に持つ | model の入力の形が変わり、0079 の feature schema の版上げが要る |
| target を期待時刻の後ろからも採る（0031 §2.2 のまま） | 入力に無い action が label に効く（PR #212 の Codex P1） |
| 後ろの観測を採る場合は、観測時刻まで action が変わらず tick が連続していたことを求める | 規則が増え、検査の対象が horizon ごとに変わる。期待時刻以前に限るほうが単純で同じ目的を満たす |
| 格子を `label_end_ms` まで延ばして入力に入れる | 0079 §2.3 の因果の mask の定義を変える |
| 区間の中の tick の値を平均する | 一度も掛からなかった値を作る（0079 §2.3「補間しない」） |
| action 列を `label_end_ms` まで持つ（0079 の段 1 の文言のまま） | 許容誤差の分の列は因果の mask の外で、どの予測にも使えない。example の範囲が延びて件数が減る |
| `PlanStep.offset_ms` を区間の始端（`step_ms × k`）に変える | `ActionPlan` の検証・digest・既存の試験と、`offsets_ms` の docstring の「予測時刻」の意味を変える。終端と読めば矛盾しない |
| `control/model/dataset.py` が `DeclaredChange` を直接受け取る | `control/drift` が `control/model` を import しているので循環する。drift の型の変更が dataset の schema に波及する |
| v2 の anchor action を v1 のまま anchor の tick 自身の effective とし、0079 §2.5 の「anchor → 最初の step」の照合を外す | runtime の anchor（いま掛かっている effective）と学習の anchor の意味が違うままになる。0079 の照合を外すと、anchor の effective から plan の最初の step への跳びが、範囲の両端の値だけで評価されてしまう（0079 §2.5 で足した理由そのもの） |
| `prior_action` を anchor の tick と同じ時刻の tick まで含めて取る | 同じ時刻の tick は anchor の tick 自身（または同時刻の別の決定）であり、「決める前の値」にならない |
| 鮮度で除いた件数を記録しない | 除外が増えても dataset から見えず、`action_stale_after_ms` の値を選ぶ根拠が残らない |
| 較正の検査を split の後（学習の段）で行う | 0079 §2.3 は「Dataset v2 の生成は拒否する」と決めている。生成された dataset が較正をまたいでいれば、後で何に使っても同じ問題が残る |
| `tick_id` は狭義単調増加だけを求める（§5 #10 の当初の規則のまま） | 保存されなかった tick（`n` と `n + 2` の間）が掛けた action を見逃す（PR #212 の Codex P1） |
| run の末尾の example を残すために、終端（最後に保存された tick と最大の horizon の間）だけ検査を省く | 保存の失敗と停止・再起動を見逃し、最後の step の区間に掛かった別の action を学習しうる（§5 #13） |
| 欠番も「再起動」に数える | 理由が混ざり、trace の保存の失敗の量と再起動の量を分けて見られない |
| trace の版を上げて、保存に失敗した tick を後で記録する | 段 1 は trace へ何も足さない（§2.8）。保存に失敗した tick を後から書けるとは限らない。既存の `tick_id` で欠番は検出できる |
| 移行前の行を `ts_ms ≤ legacy_until_ms` で見分ける（初版で確認した読み方） | 空の DB でも `legacy_until_ms` がストアの時計で書かれるので、`SimulatedClock` で最初の tick の時刻が等しい正しい run を拒否する（PR #212 の Codex P1） |
| dataset 用の DB では `legacy_until_ms` が run の開始より前であることを求める | 時計が同じなら同じ問題が残る |
| `ts_ms < legacy_until_ms`（等しい時刻を含めない）に戻す | 移行前の行の最大の `ts_ms` と等しい時刻の移行前の行を見逃す |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | **決着**（§2.7）: v2 の anchor action は、anchor の tick より厳密に前で直近の ControlTick の effective（`prior_action`）とし、元の tick を記録し、`action_stale_after_ms` で鮮度を検査する（2026-10-01 所有者承認） | 決着（本記録 §2.7） |
| 2 | **決着**（§2.2）: 鮮度で作らなかった example の件数を manifest に記録する（2026-10-01 所有者承認） | 決着（本記録 §2.2） |
| 3 | **v2 は決着**（§2.1。2026-10-01 所有者承認）: `seq` の順の `ts_ms` が狭義単調増加でない run と、移行前の行を含む run からの Dataset v2 の生成を拒否する（移行前の行の見分け方は #12）。**v1 の builder の同じ問題は開いたまま**: v1 の builder（`ORDER BY ts_ms, tick_id`）は時刻が単調でない run でも anchor の tick を `ts_ms` の順に選ぶ。v1 にも同じ拒否を足すかは、v1 の dataset を作り直す影響（既存 artifact の再生成で bytes が変わりうる）とあわせて別に決める。以下は初版の記録。§2.1 の as-of の前提（`seq` の順に並べた ControlTick の `ts_ms` が狭義単調増加）が崩れた run をどう扱うか。**推奨案**: その run からの Dataset v2 の生成を拒否する（fail closed。黙って並べ替えない）。`seq` を持たない移行前の行（0071 §2.2a の `legacy_until_ms` より前）を含む run も、順序を確かめられないので拒否する。代替案: `seq` の順で「直近」を選ぶ（壁時計が戻った区間では、格子の時刻と action の時刻の対応自体が崩れるので推奨しない）。v1 の builder（`ORDER BY ts_ms, tick_id`）にも同じ問題があるが、v1 を変えるかは別に決める | v2 は決着（本記録 §2.1）。v1 は #83 で別の Issue コメントまたは記録として扱う |
| 4 | `action_stale_after_ms` の値 | 実機の ControlTick の周期と欠け方を見た後（#50 / #83） |
| 5 | step ごとの context（mode・Safety の状態など）を持つか。本記録は step ごとに値と元の tick だけを持ち、context は anchor の tick のものだけとする（0031 §2.1 のまま） | 必要になったら新しい記録。Shadow・評価（#90 / #91）で Guard / Safety が値を変えた step の扱いが問題になった時 |
| 6 | 0079 §5 #5（window / horizon / step 格子の実値）と 0050 §5 #2（support 軸と bin の境界）は変わらず開いたまま | 実機 dataset（#50 / #83）の後 |
| 7 | **決着**（§2.2。2026-10-01 所有者承認。PR #212 の 6d584e6 への Codex P2）: `prior_action` の元の tick から最大の horizon までの間で、`seq` の順に隣り合う ControlTick の差と、最後の tick から最大の horizon までの差が、すべて `action_stale_after_ms` 未満であることを求める。外れた example は作らず、除外件数に数える | 決着（本記録 §2.2） |
| 8 | **決着**（§2.3 / §2.2。2026-10-01 所有者承認。PR #212 の 7b2783b への Codex P1）: step の区間の中で、どれか1つの zone の effective が step の開始の値と違う example は作らない。除外件数は理由ごと（鮮度・連続・区間内の変化。後に再起動を加えた）に記録する | 決着（本記録 §2.3） |
| 9 | **決着**（§2.4。2026-10-01 所有者承認。PR #212 の 7b2783b への Codex P1）: v2 の target は `t_a + h − tol ≤ source_ts ≤ t_a + h` の観測だけから採り、`label_end_ms = t_a + 最大の horizon` とする。0031 §2.2 を v2 についてだけ部分的に置き換える | 決着（本記録 §2.4） |
| 10 | **決着**（§2.2。2026-10-01 所有者承認。PR #212 の 023e55c への Codex P2）: `prior_action` の元の tick から最大の horizon までの間で、`seq` の順に隣り合う ControlTick の `tick_id` が狭義単調増加であることを求める（減少・同値は再起動）。外れた example は作らず、除外件数の4つ目の理由「再起動」に数える。trace の版は上げない。後に #11 で「ちょうど 1 ずつ」に強めた | 決着（本記録 §2.2） |
| 11 | **決着**（§2.2。2026-10-01 所有者承認。PR #212 の 3165109 への Codex P1）: `prior_action` の元の tick から最大の horizon までの間で、`seq` の順に隣り合う ControlTick の `tick_id` が**ちょうど 1 ずつ**増えることを求める（#10 の「狭義単調増加」を強める）。差が 2 以上の欠番は除外件数の5つ目の理由「欠番」に数える（減少・同値の「再起動」とは別）。trace の版は上げない。以下は判断前の記録。`ControlLoop` は tick ごとに `tick_id` を1つ進め、trace の保存に失敗しても例外を記録して制御を続ける（`_record`。0060 §2.6）。そのため、保存された隣り合う行の `tick_id` が `n` と `n + 2` になり、時刻の差は `action_stale_after_ms` 未満でありうる。保存されなかった tick は別の demand を掛けて戻したかもしれず、§2.2 の連続の検査と §2.3 の区間内の変化の検査をどちらも通ってしまう。代替案: 欠番も「再起動」に数える（理由が混ざる） | 決着（本記録 §2.2） |
| 12 | **決着**（§2.1。2026-10-01 所有者承認。PR #212 の 3165109 への Codex P1）: 移行前の行を時刻ではなく `seq` で見分ける。新しい migration で `control_trace_prune` に `legacy_through_seq` を足す（行が無い DB では 0、0007 を適用済みの DB では `ts_ms ≤ legacy_until_ms` の行の `MAX(seq)` で埋める安全側の上限）。Dataset v2 は `seq ≤ legacy_through_seq` の行を含む run を拒否する。**migration は段 1（#83）の実装 PR に同梱する**（本記録は規則だけを決める）。初版で確認した「`ts_ms ≤ legacy_until_ms` の行を含む run を拒否」の読み方を、v2 についてこれで置き換える（§6）。以下は判断前の記録。migration `0007_control_trace_seq.sql` は、行が無い DB でも `legacy_until_ms` をストアの時計（`:now_ms`）で書く。`SimulatedClock` が進む前に最初の tick を記録すると、その tick の `ts_ms` が `legacy_until_ms` と等しくなり、正しい mock / replay の run を拒否する（AGENTS.md ルール7 の経路を壊す）。代替案: (a) dataset 用の DB では `legacy_until_ms` を run の開始より前に限ることを求める（時計が同じなら同じ問題が残る）、(b) `ts_ms < legacy_until_ms` に戻す（移行前の行の最大の `ts_ms` と等しい行を見逃す） | 決着（本記録 §2.1）。migration は段 1 の実装 PR |
| 13 | **決着**（§2.2。2026-10-01 所有者承認。本 PR で #11 を反映した際に見つけた点）: 再起動・欠番の検査を、最大の horizon より後で`seq` の順に最初の ControlTick まで1つ延ばし、その `tick_id` が範囲の中で最後に保存された tick の `n + 1` であることを求める（値は使わない）。その tick がまだ無い run の末尾の example は作らず「連続」に数える。最大の horizon の直後に止まった run も除かれる副作用は安全側として受け入れる。以下は判断前の記録。範囲の中で隣り合う2つの保存された行だけを見ると、最後に保存された tick と最大の horizon の間の保存されなかった tick や再起動が見えない。代替案: run の末尾の example を残すために、末尾だけ検査を省く（保存の失敗と停止を見逃す） | 決着（本記録 §2.2） |

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
開いたまま別に扱う（§2.1、§5 #3）。移行前の行は `ts_ms ≤ legacy_until_ms`（等しい時刻を含む）と読むことも、同日に確認した。
この読み方は、Dataset v2 については下の §5 #12 の決定（`seq ≤ legacy_through_seq`）で置き換えた。

同日、PR #212 のレビュー（6d584e6 への Codex P2）を受けて、`prior_action` の元の tick から最大の horizon までの間で、
`seq` の順に隣り合う ControlTick の差と、最後の tick から最大の horizon までの差がすべて `action_stale_after_ms` 未満で
あることを求め、外れた example は作らずに除外件数へ数えることを承認した（§2.2、§5 #7）。

同日、PR #212 のレビュー（7b2783b への Codex P1 の2件）を受けて、次を承認した。

- **step の区間の中の変化**（§5 #8）→ 区間の中で、どれか1つの zone の effective が step の開始の値と違う example は
  作らない。除外件数は理由ごと（鮮度・連続・区間内の変化。後に再起動を加えた）に記録する（§2.2 / §2.3）。初版で承認した点 3 の
  「途中で値が変わった example も除かない」を、本 PR のレビュー中に置き換えた
- **target の時刻**（§5 #9）→ v2 の target は `t_a + h − target_tolerance_ms ≤ source_ts ≤ t_a + h` の観測だけから採り、
  `label_end_ms = t_a + 最大の horizon` とする。0031 §2.2 を v2 についてだけ部分的に置き換える（§2.4。Supersedes）

同日、PR #212 のレビュー（023e55c への Codex P2）を受けて、`prior_action` の元の tick から最大の horizon までの間で
`seq` の順に隣り合う ControlTick の `tick_id` が狭義単調増加であることを求め（減少・同値は再起動とみなす）、外れた
example は作らずに除外件数の4つ目の理由「再起動」に数えることを承認した。trace の版は上げない（§2.2、§5 #10）。
「狭義単調増加」は、下の §5 #11 の決定で「ちょうど 1 ずつ」に強めた。

同日、PR #212 のレビュー（3165109 への Codex P1 の2件）を受けて、次を推奨案で承認した。

- **`tick_id` の欠番**（§5 #11）→ `prior_action` の元の tick から最大の horizon までの間で、`seq` の順に隣り合う ControlTick の
  `tick_id` がちょうど 1 ずつ増えることを求める。差が 2 以上の欠番は、除外件数の5つ目の理由「欠番」として manifest に件数を
  記録する（減少・同値の「再起動」とは別の理由）。trace の版は上げない（§2.2）
- **移行前の行の見分け方**（§5 #12）→ 時刻ではなく `seq` で見分ける。新しい migration で `control_trace_prune` に
  `legacy_through_seq` を足す（行が無い DB では 0、0007 を適用済みの DB では `ts_ms ≤ legacy_until_ms` の行の `MAX(seq)` で
  埋める安全側の上限）。Dataset v2 は `seq ≤ legacy_through_seq` の行を含む run を拒否する。**migration は段 1（#83）の
  実装 PR に同梱する**（本 PR は規則を決めるだけで migration を書かない）。これにより、上で確認した「`ts_ms ≤ legacy_until_ms`
  の行を含む run を拒否」の読み方を、Dataset v2 については置き換える（§2.1）

同日、§5 #13（本 PR で #11 を反映した際に見つけた点）を推奨案で承認した。再起動・欠番の検査を最大の horizon より後で
`seq` の順に最初の ControlTick まで1つ延ばし、その `tick_id` が範囲の中で最後に保存された tick の `n + 1` であることを
求める（値は使わない）。その tick がまだ無い run の末尾の example は作らず「連続」に数える。最大の horizon の直後に止まった
run の example も除かれる副作用は、安全側として受け入れる（§2.2）。末尾だけ検査を省く案は却下した（§4）。
