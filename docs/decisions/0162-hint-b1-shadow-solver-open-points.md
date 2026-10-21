# 決定記録 0162: Workload Hint の B1（Shadow solver）の前に決める点 — fand の認証領域の再検査・集計の CLI・PR の切り方

- **種別**: Decision Record
- **Status**: FINAL（2026-10-21、リポジトリ所有者の承認。§5 の 1〜8 はすべて推奨の案）
- **Date**: 2026-10-21
- **Supersedes**: なし（採った案はどれも、[0133](0133-workload-hint-stage-b-prior-rollout.md) §2.5 / §2.5a / §2.8 / §2.9・
  [0152](0152-hint-monotonic-gate-certificate.md) §2.3 / §2.6 / §2.7・[0156](0156-hint-gate-spec-schema.md) §2.4 を置き換えず、
  具体の形を補う。§5 の 1 の (b)・(d) や 2 の (b) なら置き換えが要ったが、どれも採っていない）
- **関連**: [0041](0041-supervisor-proposal-freshness.md) / [0053](0053-control-shadow-mode-and-counterfactual-logging.md) /
  [0066](0066-workload-hint-stage-b-conditions.md) §2.1 (b)〜(d) /
  [0074](0074-supervisor-shadow-wiring-and-episode-evaluation.md) /
  [0077](0077-learned-proposal-handoff.md) §2.3 / §2.6 / §2.7 /
  [0093](0093-air-balance-shadow-summary-and-baseline-arm.md) /
  [0107](0107-mpc-worker-observed-window.md) §2.1 / §2.2 / §2.5 /
  [0115](0115-learned-worker-units.md) §2.4 /
  [0133](0133-workload-hint-stage-b-prior-rollout.md) / [0149](0149-hint-skip-reason-rule-policy-not-active.md) /
  [0152](0152-hint-monotonic-gate-certificate.md) / [0156](0156-hint-gate-spec-schema.md) /
  [0157](0157-hint-runtime-launch-form.md) / [0159](0159-hint-pycache-known-limit.md) /
  [0160](0160-hint-conditions-digest-scope.md) /
  AGENTS.md「絶対に守るルール」1・2・3・4・5・7・8・9
- **対象 Issue**: #288（0152 の段 4。0133 §5 の実装 Issue の 4）

---

## 1. Context

0152 の段 1〜3 は済んだ（PR #349・#357・#361・#363）。いまの main では、fand が起動時に証明書を照合する
（`src/coldaisle/hint_certification.py`）が、`src/coldaisle/control/supervisor/workload_hint.py` の
`IMPLEMENTED_STAGE` は `OBSERVE` のままで、照合が通っても実効の段階は B0 に留まる。次は段 4（B1 `shadow_mpc` の
Shadow solver）である。

段 4 に入る前に、原文とコードを読み合わせて、記録の間で食い違う点・どこにも書かれていない点を 3 つ見つけた
（AGENTS.md「迷ったら」。実装より先に所有者の判断を要る）。

### 1.1 論点 1: fand は「その tick の window の全 frame」の認証領域の判定を、自分では行えない

記録の側:

- **0133 §2.5**（「fand が再検査する。」の項）: 「0066 §2.1 (d) の 2 条件（…）と**認証領域の判定を、worker の判定とは別に
  fand の中で決定論的に行い**、両方が通ったときだけヒント有りの plan を Gate へ渡す候補にする」
- **0152 §2.3**（「証明書の欄は照合の対象であって…」の項）: 「実行時の認証領域の判定（0066 §2.1 (d)）と `eps` / `eps_c`
  の比較（0133 §2.5 の fand の再検査を含む）に使うのは、**fand・worker が自分の設定から導いたゲートの仕様**」。
  fand も領域の判定を行う前提で書かれている
- **0156 §2.4**: 「認証領域の判定（0066 §2.1 (d)）は、**その tick の window の全 frame の値**が各軸の範囲の中にあること、
  かつ軸を持つ metric に missing / stale / suspect が無いこと」。同じ節で「fand は artifact を読まない（0077 §2.6）ので、
  この網羅の検査はランナーと worker が行う」とあるが、ここで worker に任せたのは**artifact の metric と軸の網羅**の検査で、
  領域の判定を誰が行うかは書いていない
- **0077 §2.6**: 「`coldaisle-fand` は artifact の bytes を読まず、deserialize もしない」
- **0107 §2.1**: window の格子は「artifact の feature schema のまま」作り、格子時刻は
  `range(action_ts_ms - window_ms, action_ts_ms + 1, sample_period_ms)`、各格子時刻には「**受け取った frame のうち**
  `snapshot.ts_ms <= t` で最新のもの」を当て、`tick_id` の飛びで欠けを証明できるときは missing にする
- **0133 §2.5a**: `record_json` に載せるのは「両 plan の `requested_demand`、両 plan の予測（全 offset）、fand の再検査の結果、
  ヒント有りの context（band）、artifact の識別・`config` の digest・認証のハッシュ」。**領域の判定の入力（window の値・
  使った frame）は無い**

コードの側（main の 5ff6f96）:

- `src/coldaisle/control/learned_handoff.py` の `LearnedFrame`（v3）は、1 tick の `snapshot`・`workload`・`supervisor`・
  `baseline`・`safety_floor`・`applied` などを持つだけで、window を持たない
- window を組み立てるのは worker だけ（`src/coldaisle/learned_worker/window.py` の `FrameHistory` /
  `build_observed_input`）。`window_ms`・`sample_period_ms`・`stale_after_ms` は artifact の `ThermalFeatureSchemaV2` から来る
- fand は frame の送り出しの枠に**待たずに置くだけ**で、lock が取れなければその frame を捨てる
  （`src/coldaisle/learned_channel/mailbox.py` の `LearnedMailbox.offer`）。どの frame が worker に届いたかを fand は知らない
- 認証ランナーの平らな window も artifact の schema で作る（`src/coldaisle/hint_certify.py` の `_windows` / `flat_window`。
  metric の網羅は `_check_window_metrics`）
- 実行時の領域の判定の関数は、まだどこにも無い（`src/coldaisle/control/hint_gate.py` はゲートの仕様を導く
  `derive_hint_gate_spec` と部品の digest だけ）
- `hint_shadow_records` の表と `HintShadowRecord` は B0 の PR で入っている（`src/coldaisle/store/models.py`）が、
  「`record_json` の中身の形（両 plan・再検査の結果など）は B1 の実装で定める」とある。理由の語彙
  `HintShadowReason` は 0133 §2.5a の 8 値だけで、0152 §2.6 が足すとした `certification_mismatch` はまだ無い

つまり、0133 §2.5 の「fand が領域を判定する」を 0156 §2.4 の定義（window の全 frame）のまま行うには、fand が
artifact の window の仕様と、worker が実際に使った frame の並びを知る必要があるが、どちらも fand の手元に無い。

### 1.2 論点 2: B1 の集計の CLI の形が決まっていない

0133 §2.5a の末尾: 「B1 の集計（ヒント有りが選ばれていたらの差分・二重解法の成功率）は DB を読むだけの CLI で行い、
その形は B1 の実装 Issue で定める」。名前・入力・出力・合否の扱い・LLM との境界がまだ無い。

あわせて、集計に要る数の**運び先**にも穴がある。

- 0133 §2.5 は `shadow_dropped_backpressure` を「数として trace のヒントの塊に載せる」とするが、捨てるのは worker で、
  fand はその数を受け取る経路を持たない。いまの `WorkloadHintRecord`（`ControlTick` v16）にも数の欄は無い
- 0133 §2.8 の語彙のうち `budget_exhausted` / `monotonicity_violated` / `out_of_certified_domain` などは、ヒント有りの
  記録が届いた**後**に分かる。元の tick の trace は既に書かれていて不変なので、その tick の `skipped` には載せられない

### 1.3 論点 3: 段 4 の PR の切り方と、`IMPLEMENTED_STAGE` を上げる PR

0152 §5 の「実装 Issue の切り方」の 4 は段 4 を 1 項目にまとめている。中身は frame の版上げ・worker の子プロセス・
fand の受信と検証と記録・`ControlTick` の版上げ・learnd の `--hint-lockfile` と unit（0152 §5 の 4）にわたり、
1 つの PR では大きすぎる。切ると途中の main の状態が生まれるので、どの状態でも B1 が半端に動かない順を決めておく。

---

## 2. Decision（2026-10-21 所有者が §5 の推奨の案で承認）

### 2.1 安全上の不変条件（どの案でも変えない）

この記録のどの案も、次を変えない。段 4 の各 PR の試験で固定する（0133 §2.5 / §2.7、0152 §2.8）。

- **ヒントの経路から Fan Demand・PWM・設定・較正を変えられない。** worker から fand へ増えるのは Shadow の記録の
  メッセージ 1 種類だけで、Demand の上書き・モード・authority を表す型を持たない（0077 §2.4 の 6・ルール 2）
- **B1 は shadow だけで、適用しない。** ヒント有りの plan は `hint_shadow_records` に書くだけで、`MpcProposal` の
  受け渡し口にも Gate にも入れない。同じ frame の列に対し `stage: off` と `stage: shadow_mpc` で各 tick の
  `requested_demand` / `effective_demand` が一致する（0133 §2.5）
- **Reactive Guard と Critical Safety を迂回しない。** `coldaisle.control.safety` / `reactive` / `fallback` / `hardware` は
  ヒントの型・frame の新しい欄・この記録で足す関数を import しない（0133 §2.7）
- **失敗の影響は片方向。** Shadow の記録・子プロセス・再検査のどの失敗でも、基準の `MpcProposal` の内容・送信時刻・
  扱いは変わらない（0133 §2.5）。ヒントの失敗で MAX にしない（0133 §2.8）
- **LLM は触れない。** Shadow の記録・再検査の結果・集計の出力を読み取り API・AI ツール・日次レポートに出さない
  （0133 §2.5a・ルール 1 / 8）

### 2.2 論点 1: fand は自分が送った frame の並びで、window を覆う区間を判定する（案 c）

比べた案:

| 案 | 形 | 評価 |
|---|---|---|
| (a) worker が判定の入力を記録に載せる | worker が window の各格子時刻に当てた frame の `tick_id` と値を `record_json` に載せ、fand が自分の設定から導いた仕様で判定する | fand は値が自分の送った frame と一致するかまでは確かめられるが、**window の長さと格子は worker の申告**のまま（fand は artifact を読まない）。短い window を申告されれば、その分だけ判定が弱くなる。記録も horizon・offset に加えて window の分だけ大きくなる（`max_shadow_record_bytes`） |
| (b) fand は自分の軸だけを判定する | 鍵（Regime, workload）・直前 Demand・safety floor・Baseline・最新の snapshot だけを fand が判定し、window の全 frame の判定は worker（子プロセス）に任せる | 単純。ただし 0133 §2.5 の「最終の判定を ML 側に置かない」から、window の判定だけが外れる。0133 §2.5 / 0152 §2.3 の一部を置き換えることになる |
| **(c 推奨) fand が window を覆う区間を自分の frame で判定する** | fand は自分が MPC worker へ送り出した frame の軸の値を、設定の長さ `domain_lookback_ms` だけ環状に持つ。`supervisor_hint` を埋めた frame を送り出す時点で、その tick から遡って `domain_lookback_ms` の区間（とその直前の 1 frame）の**すべての frame** が領域の中にあることを確かめる。worker は artifact の `window_ms` が frame で受け取った `domain_lookback_ms` 以下であることを確かめ、超えれば解かない | fand の判定は artifact にも worker の申告にも依らない。区間は window を含む（下の「覆う」の説明）ので、fand の判定は worker の判定より**厳しい側にしか**ずれない。最終の判定は fand と worker の両方が通ったときだけ、のまま |
| (d) fand が artifact の window の仕様だけを読む | 起動時に artifact の feature schema を読み、worker と同じ規則で window を組み立てる | 0077 §2.6（fand は artifact を読まない・deserialize しない）を置き換える。worker に届かなかった frame（`offer` で捨てた frame）を fand は区別できず、同じ window にはならない |

**(c) を採る。** 詳細:

- **持つもの**: fand は、MPC worker へ**実際に送り出した** frame ごとに、判定に要る値だけを環状の列に持つ:
  `tick_id`・`snapshot.ts_ms`・`monotonic_ms`、`mpc.optimizer.cost_metrics` の CPU / GPU 温度と、ゲートの仕様の
  `window_features` の metric の値と quality、その frame の (Regime, ヒントの workload) の鍵、基準とヒント有りの
  context の `target_band`、`safety_floor`、`baseline` の requested、`applied`。frame そのものや他の signal は持たない。
  列に入れるのは送り出しの側（`learned_channel` の受付スレッドが枠から取り出して送った frame）で、**tick の中では
  何も待たない**（0077 §2.2。`offer` の形を変えない）
- **判定は frame を送り出す時点で行い、結果を記録が届くまで保持する**（PR #365 の Codex P1 の 1）。Shadow の記録は
  非同期に、元の frame より後の frame を何件も送った後に届きうる。届いた時点で区間を組み立て直すと、区間の古い frame が
  列から押し出されていて、正しい記録まで拒むことになる。そこで:
  - `supervisor_hint` を埋めた frame `N` を送り出すとき、区間（下）の frame はすべて送り出し済みなので、その場で下の
    1〜5 を判定し、結果（合否と最初の外れの項目）を `N` の (`tick_id` / `snapshot.ts_ms` / `monotonic_ms`) を鍵にして
    **保留の表**に置く。列は区間の長さ（`domain_lookback_ms` と、その直前の 1 frame）だけ持てばよい
  - 保留の表の結果は、送り出しから `pending_domain_result_ms` の間だけ持ち、件数は `max_pending_domain_results` で
    抑える（どちらも `config/learned-channel.yaml`。ルール 9）。上限を超えたら古いものから捨てる
  - 記録が届いたとき、元の tick の結果が保留の表に無い（期限切れ・上限で捨てた・送り出していない）なら、**範囲の外として
    扱う**: 記録は `recorded` のまま残し、fand の領域の判定を不合格、最初の外れの項目を `source_interval_unavailable`
    とする（2 条件の再検査も行って残すが、領域の判定が不合格なので「両方が通った」にはならない）。値を推測で埋めない
  - 結果を使った鍵は保留の表から消す（同じ元の tick の記録を2回通さない。2件目は `hint_record_rejected`）
  - `pending_domain_result_ms` は、worker の Shadow の記録の期限（0133 §2.5 の「元の frame の次の基準の周期の開始
    `next_due` を過ぎたら捨てる」）より長く置く。短すぎても正しい記録が範囲の外になるだけで、危険側には倒れない
- **区間**: 送り出す frame（のちに届く記録の元の tick。記録の `source_tick_id` / `source_ts_ms` / `source_monotonic_ms` が
  保留の表の鍵と一致しなければ、上のとおり範囲の外）を `N` とし、`snapshot.ts_ms` が `[source_ts_ms - domain_lookback_ms, source_ts_ms]` に入る frame と、その区間より前で
  最新の frame 1 つ（0107 §2.1 の as-of で window の先頭の格子時刻に当たりうる frame）を区間の frame とする
- **覆う**: worker の window の格子時刻は `[action_ts_ms - window_ms, action_ts_ms]`（`action_ts_ms` は `N` の
  `snapshot.ts_ms`）で、各格子時刻の値は `snapshot.ts_ms <= t` で最新の frame から来る（0107 §2.1）。`window_ms <=
  domain_lookback_ms` なら、window の値の出どころはすべて区間の frame の中にある。worker が受け取らなかった frame は
  worker の側で missing になり、worker の判定で外になる
- **判定**（同じ純粋な関数を worker と fand が使う。置き場所は `coldaisle.control` の `hint_gate.py` の隣。下の
  「関数」）:
  1. `N` の (Regime, workload) がゲートの仕様の `hint_keys` にある
  2. 直前 Demand: `N - 1` の frame の `applied`（0107 §2.2 の anchor の action と同じ時点）の 3 系統が `previous_demand` の範囲
  3. `N` の `safety_floor` と `baseline` の 3 系統が `safety_floor_range` / `baseline_range` の範囲（0156 §2.5）
  4. 区間のすべての frame で、CPU / GPU 温度が「`N` の基準の context の `upper_c` + 軸の範囲」の中（0156 §2.4 の
     「その tick の Regime の基準の `upper_c` からの差」。`N` の基準で全 frame を測る）、`window_features` の各 metric が
     軸の範囲の中、どの軸の metric も値があり quality が `stale` / `suspect` でない
  5. **区間に欠けが無い**: 区間の frame の `tick_id` が連続している（送り出せなかった tick があれば外）
  どれかが外なら `out_of_certified_domain`。fand の判定は、記録の中の worker の判定結果を読まない
- **worker の側**: Shadow solver の子は、束縛した artifact の `window_ms` が frame の `domain_lookback_ms` 以下であることを
  起動時（束縛のとき）に確かめ、超えれば解かずに `certification_mismatch` を返す。worker 自身の判定は 0156 §2.4 の定義
  （実際の window の全 frame・artifact の `stale_after_ms`）のまま、同じ関数に実際の window を渡して行う
- **`domain_lookback_ms` の置き場所**: `config/learned-channel.yaml`（ルール 9）。fand が読み、frame の
  `hint_certification`（0152 §2.6）に載せて worker へ渡す。worker は自分の設定ではなく frame の値で比べる
  （fand と worker で値が割れて、fand の区間が window より短くなることを作らない）
- **証明書には束ねない**（§5 の 2）。`domain_lookback_ms`（と保留の表の `pending_domain_result_ms` / `max_pending_domain_results`）は解の結果に効かず、fand の判定を厳しくする向きにしか
  働かない（短すぎれば worker が解かない。長ければ fand の外が増えるだけ）。0152 §2.3 のゲートの仕様の入力と、0156 §2.7 の
  正準な形・版の印 `hint-gate-spec-v1` は変えない
- **2 条件の再検査**は 0133 §2.5 のまま、記録の両 plan の `requested_demand` と予測の全 offset を、fand が自分の設定から
  導いた `eps` / `eps_c` で比べる（0152 §2.3）。予測そのものは worker の出力で、fand は予測を作り直さない
  （モデルを評価しない。0133 §2.5）。B2 でこれをどこまで信じるかは B2 の記録で決める（§2.6）
- **`record_json` に載せる fand の再検査の結果**: 領域の判定と 2 条件のそれぞれについて、合否と、外れた最初の項目の
  閉じた語彙（上の 1〜5 の名前・`demand` / `cpu` / `gpu`）だけ。値の写しは載せない（値は両 plan の欄にある）
- **関数**: 判定の関数は 1 つにし、worker（実際の window）・fand（区間）・試験が同じ関数を呼ぶ。入力は「時刻順の
  frame ごとの metric の値と quality の列」・`N` の鍵・直前 Demand・safety floor・Baseline・基準の `upper_c`・ゲートの仕様で、
  出力は合否と最初の外れの項目。`coldaisle.control.safety` / `reactive` / `fallback` / `hardware` を import しない
- **試験**（実機不要。ルール 7）: 区間の中の 1 frame だけ温度を範囲の外にすると fand が外にする（worker が window に
  使わない古い frame でも外になる＝厳しい側）・区間に `tick_id` の飛びがあると外・`window_ms > domain_lookback_ms` の
  artifact で子が `certification_mismatch`・証明書の `certified_domain` を書き換えても fand の判定が変わらない（0152 §2.8）・
  fand の判定が worker の記録の中の判定結果に依らない・元の frame の後に区間の長さを超えて frame を送った後に届いた
  記録でも、保留の表の結果で正しく判定される・保留の期限を過ぎて届いた記録が `source_interval_unavailable` で範囲の外に
  なる・同じ元の tick の2件目の記録が `hint_record_rejected`

### 2.3 論点 1 に付随: 記録の後に分かる理由と、worker が捨てた数の運び先

- **元の tick の trace の `skipped` には、その tick に fand が知っていることだけを載せる**（`certification_mismatch` /
  `mpc_unavailable` / `supervisor_unavailable` / `rule_policy_not_active` / ヒントの状態の失敗など）。記録が届いた後に
  分かる理由（`budget_exhausted` / `shadow_busy` / `out_of_certified_domain` / `monotonicity_violated` /
  `hint_record_rejected` / `certification_mismatch`（子の照合））は、`hint_shadow_records` の `status` / `reason` にだけ残す。
  trace は不変のまま書き直さない（0030 / 0133 §2.5a）。0133 §2.8 の表の語彙はそのまま使い、置き場所を分けるだけである
- **worker が送らずに捨てた数**（`shadow_dropped_backpressure` / `shadow_record_too_large` と、子が作れない
  `shadow_unavailable` の回数）は、worker が**自分のプロセスの中の累積の数**として持ち、送れた Shadow の記録のメッセージに毎回
  載せる。fand は最後に受け取った値を、以後の tick の trace のヒントの塊の新しい欄 `shadow_counters`（受け取っていなければ
  `null`）に写す
- **数の epoch**（PR #365 の Codex P1 の 2）: fand の `run_id` が同じまま、worker のプロセスが再起動して接続し直すことが
  ある（0077 §2.7 / 0115 §2.5）。新しいプロセスの数は 0 から始まる。そこで:
  - worker はプロセスの起動ごとに1つ **`worker_instance_id`**（`run_id` と同じ形の乱数の識別子。個体識別子を含まない。
    ルール 10）を作り、すべての Shadow の記録のメッセージに数と組で載せる。子の作り直しでは変えない（数は親が持つ）。
    同じプロセスが接続し直したときも同じ値のまま
  - 数は `(run_id, worker_instance_id)` を1つの epoch として、その中でだけ増える向きに進む。fand は epoch ごとに最後の値を持ち、
    **同じ epoch の中で**どれかの数が減った記録だけを `hint_record_rejected` で捨てる。新しい `worker_instance_id` の記録は
    新しい epoch として受け、前の epoch の値と比べない（再起動した worker の正しい記録を拒み続けない）
  - trace の `shadow_counters` は `{worker_instance_id, 各数}` の形にし、その tick に fand が最後に受け取った epoch の値を写す。
    前の epoch の最後の値はその epoch の最後の tick までの trace に残る
  - heartbeat には載せない（基準の経路のメッセージの形を変えない。0133 §2.5）
  - 限界: 最後の記録の後に捨てた分は fand に届かない。集計はこれを「下限」として示す（§2.4）
- **語彙の追加**: `HintShadowReason` に `certification_mismatch`（0152 §2.6）を足す。`shadow_unavailable` は記録を作らず、
  上の数にだけ数える
- Shadow の記録のメッセージは、worker の生存の知らせ（`last_valid_mono_ms`。0077 §2.7）に数えない。生存は今までどおり
  基準の結果と heartbeat で決める

### 2.4 論点 2: 集計の CLI `coldaisle-hint-shadow`

- **名前と置き場所**: `coldaisle-hint-shadow`（合成の起点 `src/coldaisle/hint_shadow.py`）。`coldaisle-supervisor-shadow`・
  `coldaisle-air-balance-shadow` と並べる
- **入力**: `--evidence`（期間を名指す manifest。名前つきの区間 `start` / `end` と、任意で `run_id` の列）・`--db`・
  `--format json|markdown`・`--out`。DB は他の shadow の CLI と同じく `coldaisle-evaluate` の `EvidenceDatabase`
  （`immutable=1`）で開き、書かない（migration も走らせない）。書くのは `--out` の 1 ファイルだけ。既定値で期間を
  決めない
- **読むもの**: `hint_shadow_records` と、0133 §2.5a の join の契約（`(ts_ms, tick_id) = (source_ts_ms, source_tick_id)`
  かつ最上位の `run_id` が等しい）で対応する `control_traces` の行の、ヒントの塊（`stage` / `influence` / `skipped` /
  `certificate` / `shadow_counters`）と 0053 の台帳（基準の plan が Gate に評価されたか）
- **層**: `run_id` × 証明書（`certificate_sha256`）× (Regime, workload) の鍵ごとに数える。証明書・run・鍵の違う
  母集団を 1 つにまとめない（0133 §2.2 の帰属の規律と同じ）
- **出力**（同じ入力から同じ bytes。生成時刻を持たない）:
  - **段の数**: 候補になった tick（`stage: shadow_mpc` かつ `influence: would_override`）→ 届いた記録（`status` と
    `reason` ごと）→ `recorded` → fand の領域の判定に通った → 2 条件に通った。worker が捨てた数（下）は
    「少なくともこれだけ」として別に示す。対応する trace が無い記録は「対応なし」として数え、捨てない（0133 §2.5a）
  - **worker が捨てた数は区間ごとの差で数える**（PR #365 の Codex P1 の 3）。累積の数の最大をそのまま使うと、区間の前に
    捨てた分まで区間に入る。manifest の区間ごと・§2.3 の epoch（`(run_id, worker_instance_id)`）ごとに:
    - 基準値 = その epoch の、区間の開始より前で最後の tick の `shadow_counters` の値。区間の中の値 = 区間の終わりより前で
      最後の tick の値。区間の数 = 区間の中の値 − 基準値
    - 区間の開始より前にその epoch の値が無い（epoch の最初の値が区間の中にある）ときは基準値を 0 とし、その epoch の数に
      「最初の記録より前の分（区間の前に捨てた分）を含みうる」の印を付ける
    - 区間の数の合計は epoch の和。epoch を跨いで差を取らない（新しい epoch は 0 から始まる）
    - 印の付いた epoch がある区間では、成功率の分母が多めでありうる（率は低めに出る）ことを出力に書く
  - **二重解法の成功率**: 「2 条件まで通った記録の数」を「届いた記録の数 + worker が捨てた数」で割った値を、分子と
    分母と一緒に出す。分母の定義を出力に書く。割合だけを出さない
  - **「ヒント有りが選ばれていたら」の差分**: `recorded` の記録について、系統ごとの `requested_demand` の差
    （ヒント有り − 基準）の分布（件数・最小・中央値・p95・最大。`control.evaluation.stats` の要約）と、予測の CPU / GPU
    温度の差（全 offset での最大と、最終 offset）の分布。fand の再検査に通った記録と落ちた記録を分けて出す。
    **差は `requested_demand` の段の値と、モデルの予測の差であり、実測でも `effective_demand` でもない**ことを出力の
    見出しに書く（Gate・Guard・Safety の後の値を模擬しない）
- **合否を出さない。** 閾値も B2 へ進むかの提案も持たない（0064 §2.10 の「測る前に数字を決めない」・0133 §5 の 1）。
  人の判断の材料で、昇格の証拠ではない
- **LLM へ渡さない。** AI 層・API・`server.py`・日次レポート・運用メモリはこの module を import しない（0064 §2.2 の
  import 走査に加える）。出力を AI ツールにしない（ルール 1 / 8）。制御側（`control_daemon.py` / `coldaisle.control`）も
  import しない
- 着手は fand が記録を書く PR（§2.5 の PR-C）の後。`record_json` の形がそこで決まるため

### 2.5 論点 3: 段 4 は 4 つの PR に切り、`IMPLEMENTED_STAGE` は fand の受信が揃う PR-C で上げる

どの PR の後の main でも、本番の fand は B1 を動かさない。`IMPLEMENTED_STAGE` が `OBSERVE` の間、fand は frame に
`supervisor_hint` を入れない（0133 §2.5）ので、worker の Shadow solver に依頼が届かない。

| PR | 中身 | 途中の main の状態 |
|---|---|---|
| **PR-A frame と制御側の配線** | `LearnedFrame` v4（`supervisor_hint`・`hint_certification`（0152 §2.6 の期待値と `domain_lookback_ms`））。fand は実効の段階が B1 以上の tick だけ埋め、0133 §2.5 の関係（同じ tick・同じ regime・同じ policy の版・`target_band` だけが違う）を検査し、満たさなければ両方 `None`。RL worker への frame は常に `None`（0133 §2.2）。§2.2 の判定の関数。worker は v4 を受け、ヒントの欄を読まない | fand は埋めない（B0 のまま）。frame の版だけが上がる。fand と worker は同じ venv で入れ替わる（0157）ので、v3 / v4 の混在を扱わない |
| **PR-B worker の Shadow solver の子プロセス** | 0133 §2.5 の子（`SCHED_IDLE` の確かめ・`PR_SET_PDEATHSIG`・1 枠の依頼・打ち切り時刻・作り直しの間隔）、子の照合（0152 §2.6・0157 §2.3 の `launch_form`）、残りの予算での二重解法（0160 §5 の 6 の入口）、§2.2 の worker の判定、ノンブロッキングで期限つきの転送（`SIOCOUTQ`・`MSG_DONTWAIT`・`SO_SNDBUF` の上乗せ・`max_shadow_record_bytes`）、§2.3 の累積の数、新しい受信の本文 `hint_shadow`。**learnd の `--hint-lockfile`**（0152 §5 の 4）と `deploy/systemd/coldaisle-learnd-mpc.service`・`docs/ubuntu-deploy.md` 6.8。決定論の試験と、実時間の試験（専用の marker。0133 §2.5） | 本文の型を `InboundBody` に足すと、いまの `server.py` は知らない型の本文でも末尾で生存の知らせ（`last_valid_mono_ms`）を数え直してしまう。そこで同じ PR で、fand の受信に `hint_shadow` を `unsupported_body` として捨て、生存に数えない分岐を足す（接続は切らない。§2.3）。frame に `supervisor_hint` が来ないので子は依頼を受けない |
| **PR-C fand の受信・検証・記録と `ControlTick` の版上げ** | `hint_shadow` の受信と検証（新しさ・`run_id`・artifact・対の基準・証明書と `conditions` の digest。0133 §2.5 / 0152 §2.6）、§2.2 の環状の列・送り出しの時点の判定と保留の表・再検査、§2.3 の epoch ごとの数の検査、`hint_shadow_records` への書き込み（tick の外の書き手）と `record_json` の形、`HintShadowReason` の `certification_mismatch`、`ControlTick` v17（ヒントの塊に `certificate`（0152 §2.7）と `shadow_counters`（§2.3）。`WorkloadHintRecord` の版を上げる）、`airflow-trace.js` の `KNOWN_VERSIONS` と fixture（0133 §2.9）。**最後に `IMPLEMENTED_STAGE = SHADOW_MPC`** | 設定の `stage` の既定は `off` のまま。B1 が動くのは、所有者が設定を `shadow_mpc` にし、証明書を昇格し、0133 §2.6 の前提（RulePolicy の本番値・本番のホストでの実時間の試験の合格）を揃えたときだけ |
| **PR-D 集計の CLI** | §2.4 の `coldaisle-hint-shadow` | 読むだけ。制御に届かない |

- `IMPLEMENTED_STAGE` を PR-C で上げるのは、fand の受信・検証・記録が無いまま B1 の frame を出すと、worker が解いた
  記録が fand で捨てられるだけの状態（測れない B1）になるため。PR-C の中では、受信・検証・記録の試験が通った後の
  最後のコミットで上げる
- PR-B と PR-C は frame（PR-A）だけに依るので並べて進められるが、マージは PR-B → PR-C の順にする（PR-C の試験が
  本物の worker の本文の形を使えるように）
- learnd の `--hint-lockfile` は MPC の unit にだけ渡す（Supervisor の worker はヒントを受け取らない。0133 §2.2）。
  渡さない・読めない worker の子は部品 `lockfile` が合わず `certification_mismatch`（0152 §2.4 の fand と同じ向き）
- 各 PR は Issue #288 を参照する（`Refs #288`）。1 つの Issue に複数の PR を当てるのは段 1〜3 と同じ扱い

### 2.6 B2 の未決はこの記録で決めない

次は **B2 の記録で**決める。この記録の案はどれも B1（適用しない）の範囲に閉じる。

- 0152 §5 の 7: B2 で worker の申告する digest（`certificate_sha256` / `conditions`）と予測をどこまで信じるか
  （§2.2 の 2 条件の再検査が worker の予測に依ることを含む）
- 0156 §5 の 4: B2 で `eps` / `eps_c` と上限に `confirmed` を要るか
- 0133 §5 の 1: B2 へ進む Stage A / B0 / B1 の証拠の閾値と観測期間（§2.4 の CLI は数を出すだけ）
- 0133 §5 の 4: RL artifact の「ヒントを使う」宣言の形（RL の active slot を開く記録）
- B2 で §2.2 の判定を tick の中の候補の選択にどうつなぐか（B1 では記録を書くだけなので、判定は tick の外で足りる）

---

## 3. Consequences

### 良くなること

- fand の領域の判定が、artifact にも worker の申告にも依らずに決まる（0133 §2.5 の「最終の判定を ML 側に置かない」を
  保ったまま、0077 §2.6 の「fand は artifact を読まない」も保つ）
- 証明書・ゲートの仕様の形（0152 §2.3 / 0156 §2.7）を変えないので、段 1〜3 の実装と照合はそのまま使える
- 集計に要る数の置き場所（記録・trace・累積の数）が先に決まるので、PR-C と PR-D の形が揃う
- どの PR の後の main でも B1 は動かず、`requested_demand` は B0 と同じ

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| fand の判定は window より長い区間を見るので、worker が使わない古い frame の外れでも記録を落とす | 厳しい側にしかずれない（B1 では記録が減るだけ）。`domain_lookback_ms` を artifact の `window_ms` に近づければ差は小さい。落ちた数は CLI の段の数に出る |
| fand に区間の長さ分の環状の列と、記録を待つ保留の表が要る | 持つのは軸の値と判定の結果だけ。長さ・期限・件数は設定で抑え、送り出しの側で持ち、tick は待たない。保留が切れた記録は範囲の外になるだけ |
| 2 条件の再検査は worker の予測に依る | B1 は適用しないので記録が汚れるだけ。B2 で扱う（§2.6） |
| 最後の記録の後に worker が捨てた数は fand に届かない | CLI は数を「下限」として示す。heartbeat の形は変えない（基準の経路を変えない） |
| 段 4 が 4 つの PR になる | 途中の main はどれも B0 のまま。PR ごとの差分が小さくなり、レビューで安全の不変条件（§2.1）を確かめやすい |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| worker が window の値を記録に載せ、fand がそれで判定する（§2.2 の (a)） | window の長さと格子が worker の申告のままで、短く申告されれば判定が弱くなる。記録も大きくなる |
| fand は自分の軸だけを判定し、window は worker に任せる（§2.2 の (b)） | window の判定だけが ML 側のプロセスに残る（0133 §2.5）。0133 §2.5 / 0152 §2.3 の置き換えが要る |
| fand が artifact の feature schema を読む（§2.2 の (d)） | 0077 §2.6 の置き換えが要る。読んでも worker に届いた frame を fand は知らないので、同じ window にならない |
| `domain_lookback_ms` をゲートの仕様に入れて証明書で束ねる | 0152 §2.3 の入力と 0156 §2.7 の形・版の印を変えることになる。値は解の結果に効かず、判定を厳しくする向きにしか働かない（§2.2） |
| `domain_lookback_ms` を worker も自分の設定から読む | fand と worker で値が割れると、fand の区間が window より短くなりうる。fand の値を frame で運ぶ |
| 記録の後に分かる理由を、元の tick の trace に書き足す | trace は不変（0030）。`hint_shadow_records` に置き、join で引く |
| worker が捨てた数を heartbeat で運ぶ | 基準の経路のメッセージの形が変わる。Shadow の記録に載せれば基準の経路に触れない |
| 集計の CLI が合否や B2 へ進む閾値を出す | 測る前に数字を決めない（0064 §2.10）。B2 の記録で決める |
| 集計の CLI が差分を Gate・Guard・Safety の後の値として模擬する | 模擬の規則がもう 1 つ生まれ、実際の制御と食い違いうる。`requested_demand` の段とモデルの予測の差として示す |
| 段 4 を 1 つの PR にする | 差分が大きく、安全の不変条件の確認が難しい。frame・worker・fand は独立に試せる |
| `IMPLEMENTED_STAGE` を PR-A（frame）で上げる | fand の受信が無いまま worker の記録が捨てられ続ける（測れない B1）。上げるのは受信・検証・記録が揃った PR-C |
| `IMPLEMENTED_STAGE` を、本番のホストでの実時間の試験の合格の後の別の PR で上げる | 実時間の試験は運用の前提（0133 §2.6）で、コードの上限とは別。設定の `stage` の既定は `off` なので、PR-C で上げても試験の前に B1 は動かない |

---

## 5. 判断点（決定済み。推奨を先頭に）

**所有者の決定（2026-10-21）**: 1〜8 のすべてを推奨の案で承認した。§2 の本文はその決定を書いたものである。
PR #365 の Codex のレビュー（P1 3 件）で、承認の趣旨を変えずに次を足した（判定を厳しくする向きと、数え方の誤りの修正）:
§2.2 の判定を送り出しの時点で行い記録が届くまで保持する（届く前に消えたら範囲の外）・§2.3 の数の epoch
（`worker_instance_id`）・§2.4 の区間ごとの差での数え方

| # | 判断点 | 選択肢（推奨を先頭） |
|---|---|---|
| 1 | fand の認証領域の判定の形（§2.2） | **(c 推奨) fand が自分の送った frame の並びで、`domain_lookback_ms` の区間（window を覆う）を判定する。worker は `window_ms <= domain_lookback_ms` を確かめる** / (a) worker が window の値と当てた frame を記録に載せ、fand が照らす（window の長さは worker の申告）/ (b) fand は鍵・直前 Demand・safety floor・Baseline・最新の snapshot だけを判定し、window は worker に任せる（0133 §2.5 / 0152 §2.3 の一部の置き換え）/ (d) fand が artifact の window の仕様を読む（0077 §2.6 の置き換え） |
| 2 | `domain_lookback_ms` の置き場所と束ね方（§2.2） | **(a 推奨) `config/learned-channel.yaml` に置き、fand が frame の `hint_certification` で worker へ渡す。証明書には束ねない** / (b) `fan-policy.yaml` の `monotonicity_gate` に置き、ゲートの仕様に入れて証明書で束ねる（0152 §2.3 の入力と 0156 §2.7 の形を置き換え、版の印を `hint-gate-spec-v2` に上げる。ランナーも `window_ms` を確かめる） |
| 3 | fand の区間に送り出せなかった tick があるとき（§2.2 の 5） | **(a 推奨) `out_of_certified_domain` にする**（worker もその frame を受け取っていない）/ (b) 欠けを無視して、ある frame だけで判定する（worker の判定に任せる） |
| 4 | 記録の後に分かる理由と、worker が捨てた数の運び先（§2.3） | **(a 推奨) 理由は `hint_shadow_records` にだけ、数は Shadow の記録に累積で載せ、fand が trace のヒントの塊の `shadow_counters` に写す** / (b) 数を heartbeat で運ぶ（基準の経路のメッセージを変える）/ (c) 数は worker の構造化ログにだけ残す（DB を読む CLI で数えられない） |
| 5 | 集計の CLI の名前・入力・DB の開き方（§2.4） | **(a 推奨) `coldaisle-hint-shadow`。`--evidence` の manifest・`EvidenceDatabase`（`immutable=1`）。他の shadow の CLI と同じ形** / (b) `--start` / `--end` を直接受ける（`coldaisle-load-distribution` と同じ形。`mode=ro`） |
| 6 | 集計の CLI の差分の示し方（§2.4） | **(a 推奨) `requested_demand` の段の差とモデルの予測の差だけを、fand の再検査に通った・落ちたで分けて出す。合否・閾値は出さない** / (b) Safety の floor と Guard を当てた後の値も模擬して出す（模擬の規則が増える） |
| 7 | 段 4 の PR の切り方と `IMPLEMENTED_STAGE` を上げる PR（§2.5） | **(a 推奨) PR-A frame → PR-B worker の子 → PR-C fand の受信・記録・`ControlTick` v17、PR-C の最後で上げる。PR-D に CLI** / (b) PR-B と PR-C をまとめる（差分が大きい）/ (c) 上げるのを、本番のホストでの実時間の試験の合格の後の別の PR にする |
| 8 | learnd の `--hint-lockfile` を渡す unit（§2.5） | **(a 推奨) MPC の unit だけ（PR-B で `coldaisle-learnd-mpc.service` と `docs/ubuntu-deploy.md` 6.8 を変える）** / (b) 2 つの unit の両方（Supervisor の worker は使わない） |

推奨の案だけが採られたので、旧記録の置き換えは無い（Supersedes: なし）。
