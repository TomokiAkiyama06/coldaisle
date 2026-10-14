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
  使えない理由（閉じた語彙）は、その後に**別のメッセージ**（Shadow 用の記録。同じ `run_id`・元 snapshot・
  対になる基準の `MpcProposal` を指す識別に束縛する）として送る
- **ヒント有りの solve は基準の周期から切り離し、最初から別プロセスで解く。** いまの `PeriodicWorker.run()`
  （`learned_worker/cli.py`）は1つのスレッドで `step()` を同期に呼び、終わった時刻から次の周期を数える。ここに
  ヒント有りの solve をそのまま足すと、それが遅れた分だけ次の基準の solve がずれ、基準の提案が失効して Fallback に
  なりうる。同じプロセスの別スレッドにしても、GIL と CPU の取り合いで打ち切りの合図が効くまで基準の solve が
  待たされうる。**B1 の前に分離を構造で保証する**ため、次のようにする（「B1 で測ってから決める」はしない）:
  - **基準のプロセス**（いまの MPC worker。`coldaisle-learnd --role mpc`）の周期のループが持つのは、基準の solve と
    送信、frame の受信、Shadow solver との受け渡しだけとする。`next_due` は基準の solve と送信の終わりだけから数え、
    ヒント有りの solve の進み具合を見ない
  - **Shadow solver は基準のプロセスが起動する子プロセス 1 つ**とする（別の uid でなくてよい。同じ unit・同じ cgroup
    に入るので、unit の停止で一緒に止まる。親が死んだら子も終わる（`PR_SET_PDEATHSIG`））。子は起動の直後に
    自分の**スケジューリングを `SCHED_IDLE` に下げる**（Linux では特権なしで下げられる）。下げたことを
    `sched_getscheduler` で確かめるまで、artifact の読み込みも solve も始めない。**`SCHED_IDLE` に下げられない・
    確かめられないときの扱いは1つだけ**: 子は何もせずに終了し、親はその run の間は子を作り直さず、Shadow solver を
    `shadow_unavailable` とする（＝その run では B1 を使えない。`nice` などで代えない）。基準のプロセスの優先度は変えない。
    これで、CPU が空いていないとき子は基準のプロセスより後に回され、基準が子を待つことは無い
  - 子は fand のソケットを持たない（役割ごとの接続は1つのまま。0077）。子は親が束縛した artifact の識別
    （`expected_artifacts` と digest）を受け取り、自分で読んで digest が一致したときだけ解く。一致しなければ
    `hint_record_rejected` 相当の理由だけを返す
  - 親から子へは**1 枠**の依頼（frame・基準の plan・打ち切り時刻）をノンブロッキングで渡す。子が前の solve の途中なら
    依頼を捨てる（`hint_prior_skipped: shadow_busy`。待ち行列を作らない）。子から親への結果も非同期の pipe で返し、
    親は**基準の solve の外**（frame を待つ間の受信の多重化の中）でだけ読み、fand へ別のメッセージとして転送する。
    転送は I/O だけで、モデルの評価をしない
  - **転送は期限つき・ノンブロッキングで、詰まったら Shadow の記録を捨てる。** いまの `WorkerChannel._send()`
    （`learned_worker/client.py`）は、共有の送信 lock（heartbeat のスレッドと共有）の下で期限の無いブロッキングの
    `send` を行う。基準の `MpcProposal` と heartbeat の送信はこのまま変えず、Shadow の記録には別の送信の入口を設ける:
    - **転送するのは周期のループのスレッドだけ**（基準の送信と同じスレッド）なので、Shadow の転送と基準の送信が
      並行することは無い。heartbeat のスレッドとは lock を共有するが、Shadow の転送は lock を
      **`acquire(blocking=False)`** で取り、取れなければその回は送らない（次の待ちの間に再び試す）。lock を持つ間に
      行うのは `MSG_DONTWAIT` の `send` 1 回だけで、`EAGAIN` / `EWOULDBLOCK` なら送らずに lock を離す
      （`ChannelClosed` にしない。経路の切断として扱うのは基準・heartbeat の送信の失敗だけ）
    - **送る前に、経路の送信待ちが空であることを確かめる**（`SIOCOUTQ` で、fand がまだ読んでいないバイトが 0）。
      空でなければ送らない。これで Shadow の記録が経路のバッファに積み上がり、次の基準の `MpcProposal` の
      `send` を待たせることが無い（Shadow の記録が経路に残るのは高々 1 件）。経路の送信バッファは
      `max_message_bytes + max_shadow_record_bytes` 以上であることを起動時に確かめ、満たさなければ B1 を使わない
      （`shadow_unavailable`）
    - **期限**: 親が持てる未転送の記録は 1 件だけ（新しいものが来たら古いものを捨てる）。転送できないまま、その記録の
      元になった frame の次の基準の周期の開始（`next_due`）を過ぎたら捨てる。いずれも
      `hint_prior_skipped: shadow_dropped_backpressure` として数える（数は trace のヒントの塊に載せる）
    - **大きさの上限**: Shadow の記録 1 件の符号化後の大きさを `config/learned-channel.yaml` の
      `limits.max_shadow_record_bytes`（`max_message_bytes` 以下。ルール 9）で抑える。超えたら切り詰めずに捨てる
      （`shadow_record_too_large`）。fand の受信側も同じ上限で検査する。全 offset の予測を載せるので、上限は
      horizon と offset の数から見積もって設定に置く
  - 打ち切り時刻は `min(基準の solve の開始 + mpc.budget_ms, 次の基準の周期の開始 next_due)` を
    `CLOCK_MONOTONIC` の絶対時刻で渡す（同じホストの別プロセスで同じ時計）。子は評価の境目ごとにこれを見て
    自分で打ち切る。親は子を待たない。0066 §2.1 (d) の「予算を共有する」はこの上限として残る
  - 子が死んだ・応答しない・pipe が壊れたときは、親はその子を終わらせ、設定の間隔をあけて作り直す。その間は
    `shadow_unavailable` で、基準の周期には何も起きない。子の失敗で worker（親）を終了しない
  - **残る影響と主張の範囲**: OS のスケジューラは基準を優先するが、共有のキャッシュ・メモリ帯域・SMT の兄弟の
    コアでの干渉は残る。したがって主張は「基準の送信時刻は、Shadow solver が CPU を使い切っていても、Shadow
    solver が無いときと**分布として区別できない**（下の試験の許容幅の中）」であり、ビット単位の一致ではない
- **B1 は `requested_demand` の値にも時刻にも影響しない。** B1 では fand はヒント有りの記録を §2.5a の
  `hint_shadow` の記録へ書くだけで、受け渡し口（`MpcProposal` の slot）にも Gate にも入れない。基準の
  `MpcProposal` が受け渡し口に届く内容・その後の Gate / Guard / Safety の結果は `stage: off` の場合と同じで、
  届く時刻は上の範囲で変わらない
- 試験で固定する:
  - **決定論の試験（CI。Mock の worker・simulated backend・注入した単調時計。ルール 7）**: 複数周期の frame の列を
    流し、Shadow solver を偽物にして遅らせる（周期をまたぐ）・例外にする・死なせる・応答させない各場合で、
    各周期の基準の `MpcProposal` の内容と送信時刻（注入した時計）が、ヒントを扱わない worker と一致すること。
    依頼が溜まらない（`shadow_busy`）こと。子の死で親が終了しないこと。基準の送信が子への依頼より先に起きること
  - **fand 側の受信が詰まった場合**（試験の偽 fand が読むのを遅らせる・止める・バッファを小さくする）: Shadow の
    記録を毎周期出させた worker と、ヒントを扱わない worker とで、同じ詰まり方のもとで基準の `MpcProposal` の
    送信時刻と送信の成否が一致すること。Shadow の記録が `shadow_dropped_backpressure` で捨てられ、経路に
    2 件以上残らないこと。heartbeat のスレッドが lock を持っている間に Shadow の転送が待たないこと。
    `max_shadow_record_bytes` を超える記録が送られないこと
  - **実時間の CPU の取り合いを含む試験**: 実際の子プロセスを `SCHED_IDLE` で起動し、子に**全コアを使い切る重い計算**
    （論理 CPU の数だけの計算ループ）を走らせた状態と、子を起動しない状態とで、同じ frame の列（数百周期）を
    実時間で流す（fand 側の受信を遅らせる条件も含める）。各周期の「`next_due` から基準の `MpcProposal` を送るまでの時間」の分布を比べ、
    p50 / p99 / 最大の差が許容幅（`config/` の試験用の設定に置く。ルール 9）に収まること。
    実時間に依存するので通常の CI の `-m "not hardware"` からは分け（専用の marker）、リリースゲートと B1 の着手前に
    **本番のホスト**で走らせる。B1 の前提に「本番のホストでこの試験に合格」を加える
  - 同じ frame の列に対し、`stage: off` と `stage: shadow_mpc` で、各 tick の `requested_demand` /
    `effective_demand` が一致すること
  - B1 で、ヒント有りの記録が `MpcProposal` の slot へ入らず、`hint_shadow` の記録にだけ現れること
  - `SCHED_IDLE` に下げられない（下げたことを確かめられない）環境では、子が artifact の読み込みも solve も始めずに
    終了し、親がその run の間は作り直さず `shadow_unavailable` になること
- **fand が再検査する。** 0066 §2.1 (d) の 2 条件（系統ごとに `demand_hint >= demand_base - eps`、
  かつ両 plan の予測のすべての offset で CPU / GPU が `hint <= base + eps_c`）と認証領域の判定を、
  worker の判定とは別に fand の中で決定論的に行い、両方が通ったときだけヒント有りの plan を
  Gate へ渡す候補にする。worker は ML 側のプロセスであり、Gate と同じく**最終の判定を ML 側に置かない**
  ため（ルール 3・4 の精神。判定は安価な比較だけで、モデルの評価はしない）
- **基準の解とヒント有りの記録は別々に検証し、失敗の影響を片方向にする。** 提案の鮮度・`run_id` の束縛・
  `expected_artifacts` の照合（0041 / 0077 / 0098）は、それぞれのメッセージに独立に当てる
  - 基準の `MpcProposal` を拒むのは、**基準自身の検証が失敗したときだけ**である（いまの規則のまま）。
    ヒント有りの記録が届かない・遅い・検証に落ちる・壊れている、のどれでも基準の扱いは変わらない
  - ヒント有りの記録の検証（新しさ・`run_id`・artifact・対になる基準の `MpcProposal` がいま受け渡し口にあるものと
    同じであること）が1つでも失敗したら、**その記録だけを捨てる**（`hint_prior_skipped: hint_record_rejected`）。
    §2.8 の他のヒントの失敗と同じく、基準の plan で運転を続ける
  - 基準の `MpcProposal` が拒まれた・入れ替わった場合、それと対のヒント有りの記録も使わない（対になる基準が無い
    ヒント有りの plan だけを Gate へ渡さない）。逆向き（ヒントの失敗で基準を捨てる）は作らない

### 2.5a ヒント有りの記録の保存先（`hint_shadow`。0053 の台帳とは別）

0053 の Shadow の台帳は、**不変の `ControlTick` に埋め込まれ、その tick に Gate が評価した候補**を記録する。
ヒント有りの記録は基準の `MpcProposal` より後に（次の tick 以降に）届き、Gate も評価しないので、その台帳には
書けない。**0053 は変えず**、別の追記専用の記録を定める。

- **保存先**: fand の store（`control_traces` と同じ SQLite）の新しい表 `hint_shadow_records`。書くのは fand だけで、
  0066 §2.2 の「制御デーモンが自分の表にだけ書く別の接続」を使う。`events` には書かない。書き込みは tick の外の
  書き手（trace の書き込みと同じ経路）で行い、失敗しても制御は続ける（0060 §2.6 と同じ。失敗の回数は trace の
  ヒントの塊に数える）
- **形**（列の型と migration は実装 Issue で定める。ここでは契約だけ）:

  | 列 | 内容 |
  |---|---|
  | `seq` | 追記順の番号（再利用・巻き戻りなし）。主キー |
  | `run_id` | fand の起動ごとの `run_id`（経路の封筒が運ぶもの。0077） |
  | `source_tick_id` / `source_ts_ms` | ヒント有りの solve の元になった frame の snapshot の `tick_id` と `ts_ms` |
  | `source_monotonic_ms` | 同じ snapshot の `monotonic_ms`（鮮度の判定と突き合わせの検算に使う） |
  | `received_monotonic_ms` / `received_ts_ms` | fand が受け取った時刻（単調時計と壁時計） |
  | `schema_version` | この記録の形の版 |
  | `status` | 閉じた語彙: `recorded`（plan あり）/ `skipped`（worker が解かなかった・打ち切った。理由つき）/ `rejected`（fand の検証に落ちた。§2.5 の `hint_record_rejected`） |
  | `reason` | `status` が `recorded` 以外のときの閉じた語彙（`budget_exhausted` / `shadow_busy` / `out_of_certified_domain` / `monotonicity_violated` / `stale` / `run_id_mismatch` / `artifact_mismatch` / `base_mismatch` など） |
  | `record_json` | `recorded` のときだけ: 基準とヒント有りの両 plan の `requested_demand`、両 plan の予測（`PlanPrediction.targets` の全 offset）、fand の再検査の結果、ヒント有りの context（band）、artifact の識別・`config` の digest・認証のハッシュ。`note`・生の `source`・`peer_uid` は載せない（0064 §2.9） |

- **追記専用**: UPDATE はトリガで拒む。DELETE は保持期間の適用（`coldaisle-rollup`）だけが行う（`control_traces` と
  同じ扱い。0030）
- **保持**: `config/retention.yaml` に `hint_shadow_days` を足す（ルール 9）。既定の値は `control_trace_days` と同じに
  する（突き合わせる相手の trace より長く残しても join できないため）
- **突き合わせ（join の契約）**: `hint_shadow_records` の1行は、`control_traces` の主キー
  `(ts_ms, tick_id) = (source_ts_ms, source_tick_id)` の行と、その trace の最上位の `run_id`（§2.9 で必須にする欄。
  `mode_command.run_id` ではない）が記録の `run_id` と等しいときだけ
  対応する。対応する trace の行が無い（保存に失敗した・保持で消えた）記録は集計で「対応なし」として数え、捨てない。
  基準の plan が実際に使われたかは、元 snapshot が同じ基準の `MpcProposal` を Gate が評価した tick（0053 の台帳の側）
  から引く。**`ts_ms` だけ・`tick_id` だけでは突き合わせない**（再起動で `tick_id` が戻り、壁時計は飛ぶため）
- **公開しない**: 読み取り API・AI ツールには出さない（新しい endpoint を足さない。0009 §3 / ルール 8）。
  Thermal Dataset（0031 / 0109）にも入れない。B1 の集計（ヒント有りが選ばれていたらの差分・二重解法の成功率）は
  DB を読むだけの CLI で行い、その形は B1 の実装 Issue で定める

### 2.6 段階（shadow から始める）

段階は `config/fan-policy.yaml` の `supervisor.workload_hint.stage` で持つ。値は
`off` / `observe`（B0）/ `shadow_mpc`（B1）/ `active`（B2）。既定は `off`。
**設定は上限であり、実効の段階はさらに下の条件で下がる。** 設定を下げれば次の起動から戻る。

| 段階 | 制御デーモンがすること | `requested_demand` への影響 | 前提 |
|---|---|---|---|
| `off` | ヒントを読まない（Stage A のまま） | なし | — |
| **B0 `observe`** | ヒントを読み、状態（`active` / `expired` / …）を進め、終端状態を永続化し、「効かせていたら選んだ context」を trace に残す（`influence: would_override`）。frame には載せない | **なし** | 0064 前提 1（#82）・前提 4（所有者の承認）・0066 §2.2 の実装 |
| **B1 `shadow_mpc`** | B0 に加え、frame の `supervisor_hint` を埋め、worker に二重解法をさせる。基準の解は先に `MpcProposal` として届き（§2.5）、ヒント有りの plan は別プロセスの Shadow solver が解き、後から **`hint_shadow` の記録（§2.5a）へ残すだけ**で、Gate へは基準の plan だけを渡す | **なし**（値。時刻は §2.5 の範囲で変わらない） | B0 の前提 ＋ 0064 前提 2（RulePolicy の本番値）＋ 0066 §2.1 (b) / (c) の照合済みハッシュ ＋ §2.5 の実時間の試験に本番のホストで合格 |
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
| Shadow solver が前の solve の途中 | 新しい依頼を捨て、基準の plan を使う | `shadow_busy` |
| Shadow solver の子プロセスが死んだ・応答しない | 子を設定の間隔で作り直す。基準の周期と plan はそのまま | `shadow_unavailable` |
| 子が `SCHED_IDLE` に下げられない・確かめられない | 子は何もせず終了し、その run の間は作り直さない（B1 は使えない。`nice` で代えない） | `shadow_unavailable` |
| Shadow の記録を fand へ送れない（lock が取れない・送信待ちが空でない・`EAGAIN`・期限切れ） | その記録を捨てる。基準の送信は待たない | `shadow_dropped_backpressure` |
| Shadow の記録が `max_shadow_record_bytes` を超える | 切り詰めずに捨てる | `shadow_record_too_large` |
| ヒント有りの記録の検証（新しさ・`run_id`・artifact・対の基準）に落ちた | その記録だけを捨て、基準の plan を使う（基準は自分の検証が失敗したときだけ拒む） | `hint_record_rejected` |
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

**fand の起動ごとの `run_id` をすべての trace に必ず載せる。** §2.5a の突き合わせは trace の `run_id` を使うが、
いまの `ControlTick` で `run_id` を持つのは `mode_command`（`ModeCommandRecord.run_id`。v12）だけで、これは任意であり、
管理ソケットを開かない構成（`--no-admin`）では `None` になる。そこで:

- 上と同じ版上げで、`ControlTick` の最上位に **`run_id`（必須。`RUN_ID_PATTERN` の形）** を足す。値は `control_daemon.py` が
  起動ごとに1つ作る `run_id`（`new_run_id()`。管理ソケットの監査と Learned の経路の封筒が既に共有しているもの。0077 §2.3）。
  **ヒントの段階・管理ソケットの有無・Learned の経路の有無に関わらず、その版以降に fand が書くすべての tick に載せる**
  （`stage: off` でも載せる。`--no-admin` でも載せる）
- 同じ tick に `mode_command.run_id` があるときは、最上位の `run_id` と等しいことを `ControlTick` の検証で確かめる
  （食い違う trace は作らない）
- 古い版の trace には `run_id` を持たせない（足さない・推測で埋めない）。古い版の trace と対応する `hint_shadow` の記録は
  存在しない（B1 は新しい版の後にしか動かない）ので、突き合わせに穴は生じない。古い版の trace の `mode_command.run_id` を
  最上位の `run_id` として読み替えない
- **0071 との関係**: 0071 §2.3 は本文（`body`）を保存した JSON のまま返し、外枠（`seq` / `ts_ms` / `tick_id` /
  `schema_version` / `age_ms`）は `/api/v1` に属して `ControlTick` の版で変わらない、と決めている。`run_id` は本文の中の
  欄なので、`/api/v1` の形は変わらない。画面の変換（`airflow-trace.js`）は新しい版を知るまで「未対応の版」と出す
  （0071 §2.3 の規律のまま）ので、版の追加と同じ PR で変換と fixture を更新する。`run_id` は起動ごとの乱数の識別子で、
  個体識別子を含まない（`learned_channel/messages.py` の形。v12 の `mode_command` で既に本文に出ている）ので、
  0071 の LLM との境界（`/api/v1/tools` に載せない）とルール 10 に触れない
- Supervisor の shadow 集計（0074）の鍵 `(ts_ms, tick_id)` は変えない。`run_id` は §2.5a の突き合わせの検算に加わるだけ

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
| 二重解法で MPC worker の計算が増える | 予算は増やさない（0066 §2.1 (d)）。基準を先に解いて先に公開し、ヒント有りは `SCHED_IDLE` の子プロセスで解いて次の周期で打ち切るので、基準の周期はずれない（§2.5 の決定論の試験と、全コアを使い切る実時間の試験で固定）。キャッシュ・メモリ帯域の干渉は残るので、主張は分布の許容幅の中に留める。成功率は B1 で測り、低ければ B2 を使わない（人が決める） |
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
| **ヒント有りの solve を周期のループの中で、基準の送信の後に同期に回す** | `PeriodicWorker.run()` は終わった時刻から次の周期を数えるので、ヒント有りが遅れた分だけ次の基準の solve がずれ、提案が失効して Fallback になりうる。別プロセスの Shadow solver に切り離し、次の周期で打ち切る（§2.5） |
| **Shadow solver を同じプロセスの別スレッドにし、重なりは B1 で測ってから決める** | GIL と CPU の取り合いで打ち切りの合図が効くまで基準の solve が待たされうる。分離を B1 の前に構造で保証するため、最初から `SCHED_IDLE` の子プロセスにする（§2.5） |
| **Shadow の記録も基準と同じ `_send()`（共有 lock・期限なしのブロッキング送信）で送る** | fand の受信が詰まると、Shadow の記録の送信が lock を持ったまま止まり、基準の `MpcProposal` と heartbeat の送信を待たせる。Shadow の記録はノンブロッキング・期限つきで、詰まったら捨てる（§2.5） |
| **`SCHED_IDLE` に下げられないとき `nice` で代える** | `nice` は CPU が混んでいるときも子に時間を配るので、§2.5 の「基準が子を待たない」の前提が成り立たない。扱いを1つにし、B1 を使わない |
| **突き合わせに `mode_command.run_id` を使う** | 任意の欄で、`--no-admin` では `None` になる。最上位の必須の `run_id` を足す（§2.9） |
| **遅れて届くヒント有りの記録を 0053 の Shadow の台帳に書く** | 0053 の台帳は不変の `ControlTick` に埋め込まれ、その tick に Gate が評価した候補を記録する。後から届き Gate が評価しない記録は入らない。0053 を変えず、別の追記専用の `hint_shadow` の記録にする（§2.5a） |
| **ヒント有りの記録の検証が落ちたら基準の提案も捨てる** | 任意の機能の失敗で基準の MPC を失い、Fallback に落ちる。基準は自分の検証でだけ拒む（§2.5） |
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
4. B1: frame の `supervisor_hint`・`SCHED_IDLE` の子プロセスの Shadow solver（基準を先に公開・ヒント有りは別のメッセージ）・fand の再検査・`hint_shadow_records` の表と保持・実時間の試験
5. B2: Gate へ渡す候補の切り替え（前提 1〜6 がすべて揃ってから）
