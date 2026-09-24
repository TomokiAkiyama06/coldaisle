# 決定記録 0064: Workload Hint の入口・形・期限と、Supervisor の prior としての扱い

- **種別**: Decision Record
- **Status**: FINAL（2026-09-21、リポジトリ所有者が承認。0045 のソケット再利用・Workload Regime 推定へは入れず SupervisorInput のみ・冷却を弱めない・Stage A は記録のみ、の 4 点。Stage A の実装は別 Issue / 別 PR）
- **Date**: 2026-09-21
- **Superseded by**: [0066](0066-workload-hint-stage-b-conditions.md)（§2.8 の「単調性（片方向）」の検証方法、§2.6 の「起動時の取り込み」の条件式と起動時の復元と稼働中の増分取り込みでの行の順序（`ts_ms` ではなく `events.id`）、§2.4 の「スキーマ変更は要らない」の記述、§2.5 の「読み取り専用の接続で読む」の記述のうち制御デーモンが DB に一切書かないと読める部分（`events` は読み取り専用のまま。0066 §2.2 の制御デーモン自身の表への書き込みの接続が加わる）、§2.10 の Stage B の前提の一覧（0066 §2.1 (b) の単調性試験の合格と (c) の照合済みハッシュを前提に加える。4 項目は有効）、§2.1 の「実装の変更点は `messages.py` の予約解除と型の追加に限る」という範囲の制約（`events` の migration と、制御デーモンの終端状態の表・run の記録・取り込みの境界の追加）のみ。他の節は有効）
- **Supersedes**: なし。[0045](0045-local-socket-write-entry.md) §2.9 と §5 の未決 #1
  （「Workload Hint のメッセージ形・Supervisor への渡し方・実装時期」）に答える記録であり、
  0045 の本文は書き換えない
- **関連**: [0009](0009-read-api.md) §3（GET-only） / [0015](0015-llm-tools.md) /
  [0018](0018-tool-exposure.md) / [0021](0021-public-repo-hygiene.md) /
  [0027](0027-fan-control-architecture.md) / [0028](0028-fan-control-contracts.md) §2.3 / §2.6 /
  [0030](0030-control-decision-trace-storage.md) / [0031](0031-thermal-dataset-contract.md) /
  [0036](0036-transient-cpu-gpu-regime.md) / [0041](0041-supervisor-proposal-freshness.md) /
  [0045](0045-local-socket-write-entry.md) / [0054](0054-offline-evaluation-attribution-and-gates.md) /
  [0058](0058-rl-supervisor-training-environment.md) §2.1 /
  AGENTS.md ルール 1 / 2 / 3 / 8 / 9 / 10
- **対象 Issue**: #107（依存 #87 / #88 / #102。関連 #67 / #82 / #89 / #105）

---

## 1. Context

#107 は、学習ジョブやベンチマークを起動する側から「いまどんな負荷を流すつもりか」を
受け取り、Supervisor（#88）の **prior** にしたいという要求である。ラベルは
`needs-decision` で、未決は2つだった。

1. 入口（ローカル Unix ソケット / ファイル / job launcher 連携）と認可
2. 実装する時期（Telemetry だけの Regime 推定で不足が見えてから）

AGENTS.md「決定記録」は、仕様に無い判断は実装より先に記録して承認を得ると定める。
**本記録は #107 の決定であって、機能の実装ではない。**

判断の前提は #107 が挙げた原則で、これは変えない。

- **Workload Hint = prior、Telemetry = reality。** 食い違えば観測を優先する
- ヒントが無くても、壊れていても、Telemetry だけで動作する
- ヒントは Supervisor の文脈にだけ入る。Fan Demand・Reactive Guard・Critical Safety へ影響させない
- ヒントの入り口を読み取り API に足さない（0009 §3）
- LLM / AI 層からヒントを書き込めない（AGENTS.md ルール1）

いまの世界の状態も前提になる。

- 書き込み専用のローカル Unix ソケット入口（`coldaisle-eventd`）は 0045 で **FINAL** として
  決まり、実装もある（`src/coldaisle/event_entry/`）。`workload_hint` は
  **名前だけ予約され、いまは拒否される**（`messages.py` の `RESERVED_TYPES`）
- Workload Regime 推定（#87）は**観測済み Power の連続履歴だけ**を使い、
  「予測 horizon や expected duration は入力にも出力にも持たない」と docstring に明記されている
- Supervisor（#88）の `SupervisorInput` は `snapshot` / `recent_history` / `workload` /
  `recent_performance` からなり、RulePolicy は Regime を**設定済みの context 表**へ写すだけである
- 0058 §2.1 は RL 学習環境の observation を `SupervisorInput` そのものと定めた。
  つまり `SupervisorInput` にフィールドを足すことは、**RL の observation を変えること**である

---

## 2. Decision

### 2.1 入口は 0045 のソケットをそのまま使う。第2の機構を作らない

**同意する。新しい入口を作らない。** #107 のヒントは 0045 §2.1〜§2.4 の
Unix ドメインソケット（`coldaisle-eventd`）へ、`type: "workload_hint"` として送る。

- ソケットは1本のまま。パス・権限・上限・タイムアウトは `config/event-entry.yaml` のまま
- クライアントは `coldaisle-event` に副コマンドを足す
  （例: `coldaisle-event workload-hint training --expected-duration 4h` /
  `coldaisle-event workload-hint end`）
- 読み取り API（`coldaisle.api` / `coldaisle.server`）には引き続き **POST を足さない**。
  0009 §3 の「OpenAPI が `get` だけ」という試験はそのまま残る
- 実装の変更点は、`messages.py` の `RESERVED_TYPES` から `workload_hint` を外し、
  `_ACCEPTED` に `WorkloadHintMessage` を足すことに限る

理由は単純で、**2つ目の入口は2つ目の認可・2つ目の検証・2つ目の failure mode を意味する**。
0045 が既に「ファイル権限 → peer credential → 版付き JSON の検証 → 追記のみの保存」という
一続きの門を作っており、ヒントはその門を通れば足りる。入口を増やす理由になるのは
「ヒントが GPU Mode と違う権限・違う信頼度を要求する」場合だが、§2.2 のとおりそれは無い。

### 2.2 認可も 0045 のまま。ソケットの意味は「注釈してよい」であって「制御してよい」ではない

- 認可は 0045 §2.3 のまま。接続ごとに `SO_PEERCRED` で uid を見て、
  同じ uid かソケットのグループのメンバーだけを受理する。root を暗黙に認めない
- **種類ごとに別のグループ・別のソケットを設けない**（§4 の却下案）。
  代わりに、このソケットが持てる意味を1文に固定する:

  > **このソケットで受理する種類は、いずれも「記録と文脈」であって「指令」ではない。**
  > 受理した内容から Fan Demand・PWM・設定ファイル・較正値へ至る経路を作らない。

  この性質は規約ではなく試験で固定する。0045 §2.6 / §2.7 の import 走査
  （`coldaisle.event_entry` が `coldaisle.control` の書き込みや `config` の書き込みを
  import しない / `coldaisle.ai` `coldaisle.api` `coldaisle.server` が
  `coldaisle.event_entry` を import しない）をそのまま `workload_hint` にも効かせる
- 書いた接続の uid は 0045 §2.5 のとおり `events.peer_uid` に残る。
  **誰が書いたヒントかは後から辿れるが、その識別子は decision trace には出さない**（§2.9）

### 2.3 メッセージの形（`type: workload_hint`）と版

1接続につき1行の要求と1行の応答（0045 §2.4）。

```json
{"v": 1, "type": "workload_hint", "hint_v": 1, "phase": "start",
 "workload": "training", "expected_duration_s": 14400,
 "source": "workspace-job-launcher", "note": "nightly run"}
```

```json
{"v": 1, "type": "workload_hint", "hint_v": 1, "phase": "end"}
```

| フィールド | 型 | 規則 |
|---|---|---|
| `v` | int | `1` のみ。**封筒（transport）の版**。0045 §2.4 のまま |
| `type` | str | `workload_hint` |
| `hint_v` | int | `1` のみ。**ヒント本体の版**。`v` と分けるのは、ヒントの形を変えるたびに `gpu_mode` の封筒を巻き込まないため |
| `phase` | str | `start` / `end` のみ。`end` はいま有効なヒントを取り消す |
| `workload` | str | `training` / `benchmark` / `inference_service` の**閉じた集合**。`phase: start` のとき必須、`end` のときは付けない |
| `expected_duration_s` | int | 任意。`1`〜`limits.max_expected_duration_s`（設定）。**期限の算出にだけ使う**（§2.6）。予測 horizon でも残り時間の countdown でもない |
| `source` | str | 任意。0045 と同じ形 `[a-z0-9][a-z0-9._-]{0,63}` |
| `note` | str | 任意。0045 と同じ規則（200文字以内・制御文字を含まない）。**人が後から読むためだけ**で、制御にも trace にも使わない（§2.9） |

拒否するもの:

- **未知のフィールド**（`extra = forbid`）。型の暗黙変換もしない（strict）。0045 §2.4 のまま
- **`ts` などの時刻。** 書き手は時刻を渡せない（0045 §2.5）
- **`job_id` / `user` / `host` / `pid` / パス等の識別子。**
  public リポジトリの衛生（0021 / AGENTS.md ルール10）と、§2.9 の「際限のない同一性を作らない」ため
- **`progress` / `step` / `epoch`。** v1 では読む側が無い。
  **読む側を決めてから足す**（未決 #2）。読み手のいないフィールドは、誰も気づかないうちに意味が変わる
- **「これから暇になる」種類のヒント**（`idle_planned` 等）。理由は §2.8 のとおり、
  **冷却を弱める向きのヒントは、書き手が間違えたときにいちばん高くつく**

版の進め方:

- フィールドの追加・意味の変更は、**新しい決定記録でのみ**行う。追加時も `hint_v` を上げる
- `coldaisle-eventd` が受理する `hint_v` の集合は設定に持つ（既定 `[1]`）。
  未知の `hint_v` は固定の文言で拒否する（入力を反射しない。0045 §2.4）
- 保存する payload には `hint_v` を含める。**古い行を新しい意味で読まない**
- 読む側（§2.5 の消費者）は、知らない `hint_v` の行を「ヒント無し」として扱う。
  推測で解釈しない（fail closed）

### 2.4 保存は `events` への追記だけ。`system_state` には映さない

- `kind = 'workload_hint'`、`payload` は検証済み JSON、`ts_ms` はホスト受信時刻、
  `peer_uid` は書き手の uid。表・トリガ・索引は 0045 §2.5 のままで、**スキーマ変更は要らない**
  （`kind` の許可リストはコードが持つ、と 0045 §2.5 が既に決めている）
- **`system_state` には書かない。** `gpu_mode` が `sys.gpu_mode` を持つのは Server Health（0040）が
  読むからで、ヒントにはその読み手が無い。「いまの値」を1箇所に置くと、
  **読み取り API 越しにそれを真値として読む経路**が生まれる。ヒントは真値ではない
- 保持は `events` のまま（無期限・追記のみ）。行数は起動したジョブの数しかない
- 読み出しは既存の `GET /api/v1/events`（0045 §2.7）にそのまま現れる。
  **新しい endpoint を足さない**

### 2.5 ヒントは Workload Regime 推定に入れない。Supervisor の context にだけ入る

#107 の本文は「Supervisor が Workload Regime を推定するときの prior」と書いているが、
**推定器（`WorkloadRegimeEstimator`、#87）にはヒントを入れない。** 入る先は
`SupervisorInput` の独立したフィールド（`workload_hint`）だけとする。

理由:

1. `WorkloadRegimeEstimate` は Supervisor だけのものではない。0041 §2 の提案鮮度の判定、
   decision trace（#82）、0058 の RL observation、0054 の offline evaluation の帰属が
   すべてこれを読む。**同じ Telemetry から同じ Regime が出ることが、再生・再評価の前提**である
2. 推定器にヒントを混ぜると、記録済み Telemetry から Regime を**再現できなくなる**
   （0031 の dataset 契約と 0054 の帰属規則が壊れる）
3. 「Telemetry を優先する」ことが**試験できなくなる。** 混ぜた後で「どれくらい優先したか」を
   測るのは難しい。分けておけば試験は自明になる —
   **ヒントの有無で `WorkloadRegimeEstimate` が1ビットも変わらないことを確かめればよい**
4. #87 の推定器は expected duration を入力に持たないと明記して作られている。
   その設計を壊さずに #107 の要求を満たせる

したがって層はこうなる。

```text
Telemetry ─→ WorkloadRegimeEstimator（ヒントを見ない）─→ WorkloadRegimeEstimate
events(workload_hint) ─→ Hint Reader（合成の起点）─→ SupervisorInput.workload_hint
                                                   ↘
                                          Supervisor policy（§2.8 の範囲でだけ使う）
```

**層の依存は一方向のまま。** `coldaisle.control` は DB を開かない（いまも
`coldaisle.store.models` の型しか import していない）。`events` を読むのは
制御デーモン（合成の起点）で、読み取り専用の接続で読み、検証済みの値を
`SupervisorInput` へ渡す。`coldaisle.control` が `coldaisle.store.db` を
import しないことを試験で固定する。

### 2.6 鮮度と期限は、制御ループ自身の単調時計で数える

0041 が RL 提案について決めたのと同じ規律を、ヒントにも使う。
**壁時計と単調時計を引き算しない。**

- 制御ループは、新しいヒント行を**自分が読んだ tick**で `observed_monotonic_ms` を刻む。
  これは `ControlStateSnapshot.monotonic_ms` と同じ時計であり、
  0041 の `source_monotonic_ms` と同じ役割を持つ
- 期限:

  ```text
  declared_ms  = expected_duration_s * 1000 + workload_hint.grace_ms   （無ければ default_ttl_ms）
  expires_at   = observed_monotonic_ms + min(declared_ms, workload_hint.max_age_ms)
  age_ms       = now_monotonic_ms - observed_monotonic_ms              （負にならない）
  ```

  `grace_ms` / `default_ttl_ms` / `max_age_ms` は `config/fan-policy.yaml` の
  `supervisor.workload_hint`（AGENTS.md ルール9。この設定ファイルは #103 / #74 が導入する）
- **書き手が申告した時間をそのまま信じない。** `max_age_ms` が上限であり、
  `expected_duration_s` は上限の中で短くする方向にしか効かない。
  申告で期限を伸ばせると、1回の誤った書き込みが何時間も残る
- `events.ts_ms`（壁時計）は**行の順序**と、起動時の取り込み（下記）にだけ使う。
  制御中の鮮度判定には使わない
- **起動時の取り込み**: 制御デーモンは起動時に、終了していない最新のヒントを**高々1件**だけ拾う。
  条件は `起動時の壁時計 - ts_ms <= workload_hint.startup_backfill_ms`。
  拾ったヒントの残り時間は、壁時計で見た経過ぶんだけ差し引く
  （古いヒントが起動によって満額で蘇らないため）。条件に合わなければヒント無しで始める
- **有効なヒントは高々1件。** 新しく受理された行が古いヒントを置き換える（`superseded`）。
  `phase: "end"` は取り消す（`ended`）
- **壁時計でない時計で動いているときはヒントを読まない。** `--speed` の圧縮再生や
  `replay` では `SimulatedClock` が使われる（0045 §2.1 と同じ理由）。
  シナリオ時間とホスト時間が混ざった鮮度判定をするくらいなら、ヒント無しで動かす
- **ヒントの期限切れは Safety State を変えない。** 0041 §2 の 5 と同じで、
  期限切れ・矛盾・欠落はいずれも Supervisor の context 選択に戻るだけであり、
  Critical Safety・Reactive Guard・Fallback の判断には影響しない

### 2.7 ヒントが Telemetry と食い違ったら観測を採り、その行は二度と採用しない

矛盾の判定は **Telemetry 由来の Regime だけ**から決定論的に行う。

- 判定: `workload` が計算負荷を意味する種類（v1 は3つとも該当）であるのに、
  Telemetry 由来の Regime が `IDLE` または `COOLDOWN` のまま
  `workload_hint.contradiction_hold_ms` 以上続いたとき、そのヒントを `contradicted` にする
- `contradiction_hold_ms` は設定値。**一瞬の谷で取り消さない**ための保持時間であり、
  短すぎると起動直後のジョブを殺し、長すぎると落ちたジョブを引きずる
- **一度 `contradicted` になった行は、その後 Telemetry が一致しても再採用しない。**
  ジョブは途中で落ちる（#107 の原則）。落ちたジョブのヒントが後から蘇るくらいなら、
  書き手にもう1行書かせるほうが安い。書き直しは1行で済む
- 矛盾は**構造化ログと decision trace の両方に残す**（§2.9 の `status` / `contradiction`）。
  数えられる形にしておかないと、「よく外す書き手」を直せない
- **逆向きの矛盾は定義しない。** ヒントが無いのに負荷がある、は矛盾ではなく通常である。
  ヒントは prior であって、必要条件ではない

### 2.8 ヒントが変えられるのは設定済み context の選択だけ。冷却を弱める向きには動かせない

RulePolicy は Regime を `rule_policy.contexts` の context（`strategy` / `weights` /
`target_band`）へ写す。ヒントが触れるのは**この写像の選択だけ**である。

- 設定に明示の対応表 `supervisor.workload_hint.context_overrides` を置く。
  鍵は (Telemetry 由来の Regime, `workload`)、値は **同じ `contexts` 表の中の別の context** を指す
- 表に無い組み合わせでは、ヒントは**何もしない**。既定は「効かない」側
- 選ばれる context は、いずれにせよ `supervisor.output_bounds` の検証を通る
  （いまも起動時に全 Regime ぶん検証している）。**ヒントが範囲を広げることはない**
- **単調性（片方向）**: 上書き先の context は、ヒントが無いときの context より
  **冷却を弱めてはならない**（`target_band` の各端が高くならない、温度 weight が下がらない）。
  起動時に検証し、満たさなければ**起動しない**（fail closed）。
  これがあるので、**書き手が嘘をついても、嘘によって温度が上がることはない**。
  上振れの代償は騒音と消費電力であって、温度ではない
- ヒントは `requested_demand` にも `Reactive Guard` の floor / ceiling にも
  `Critical Safety` の定数にも触れない。触れられる場所が構造的に無い（AGENTS.md ルール2 / 3）
- RLPolicy（#89）については §2.10 を見ること。`SupervisorInput` にフィールドが増えることは
  observation が変わることであり、黙って渡さない

### 2.9 decision trace には閉じた語彙だけを出す（際限のない同一性を作らない）

#82 / 0030 の `control_traces` に残す。`ControlState` に任意の小さな塊を足し、
`ControlTick` の schema version を実装時点の値から1つ上げる。**古い version の trace に
この塊を持たせない**ことを検証し、「昔の tick はヒントが無かった」と
「昔の形にはヒントという概念が無かった」を取り違えないようにする。

| 項目 | 値域 |
|---|---|
| `workload` | 閉じた enum（§2.3 の3つ） |
| `status` | 閉じた enum: `active` / `expired` / `contradicted` / `superseded` / `ended` / `unsupported_version` |
| `source_label` | **設定した既知 source の許可リスト**に載っていればその値、載っていなければ `other` |
| `event_id` | `events.id`（整数）。**全文はそこにある** |
| `age_ms` | `now_monotonic_ms - observed_monotonic_ms`（0 以上） |
| `influence` | 閉じた enum: `none` / `context_override`。**その tick で実際に選択が変わったか** |

**trace に載せないもの**: `note`、生の `source` 文字列、`peer_uid`、
書き手が付けた任意の識別子。理由は2つある。

1. **際限のない同一性を作らない。** trace は集計され、export され（#90 / #91）、
   public リポジトリの例にも引かれる。自由文字列をそのまま持つと、
   値の空間が際限なく広がり、やがて**人・ジョブ・ホストの同一性そのもの**になる。
   閉じた語彙と設定済みの許可リストなら、集計の鍵として安全で、
   AGENTS.md ルール10 / 0021 に抵触しない
2. **全文は失われない。** `event_id` から `events` の行を引けば、`note` も `source` も
   `peer_uid` も残っている。trace は「判断の記録」、`events` は「届いた事実の記録」で、
   役割が違う

`influence` を必ず持つのが要点である。**「ヒントがあったか」ではなく
「ヒントで判断が変わったか」が残る。** 変わっていない tick が大半なら、
その機能は要らなかったということであり、それも証拠として使える。

LLM との境界も確認しておく。AI 向けツールは読み取りの5つだけで（0015 / 0018）、
`events` はその中に無い。ヒントの自由文はいまの経路では LLM に届かない。
将来 `events` を要約に含めるとしても、**`note` は指示ではなくデータとして扱い、
生の時系列をそのまま渡さない**（AGENTS.md ルール8）。

### 2.10 実装は2段階。Stage A は証拠集め、Stage B は所有者の承認が要る

#107 の2つ目の未決（時期）への答え。**いますぐ Stage B をやらない。**

**Stage A（記録だけ。制御は1ビットも変わらない）**

- `coldaisle-eventd` が `workload_hint` を受理し、`events` に残す。`coldaisle-event` に副コマンド
- **制御デーモンは読まない。** `SupervisorInput` も trace の形も変えない
- 目的は「Telemetry だけの Regime 推定で本当に不足しているか」を**測ること**である。
  ヒントと trace を突き合わせれば、「ヒント上は継続中の負荷なのに Regime が
  `TRANSIENT_*` / `UNKNOWN` に留まった tick」を数えられる
- Stage A 単体では、**間違ったヒントを書いても何も起きない**。これが安く始められる理由

**Stage B（prior として使う）**

前提（すべて満たすまで着手しない）:

1. **#82 の decision trace が保存されていること**（0030）。前後比較の土台が無いと効果を測れない
2. **#88 の RulePolicy が active で動き、`supervisor.output_bounds` と `contexts` が本番値であること**。
   ヒントは context の選択しか変えないので、選ぶ先が暫定値では意味がない
3. **Stage A の証拠**。上の「Regime が追いつかなかった tick」の割合が、
   運用上無視できない水準で観測されていること。**閾値と観測期間は本記録では決めない。**
   測る前に数字を決めると、その数字に合わせて読んでしまう。Stage B の実装 Issue で、
   実測を見てから決めて記録する
4. **所有者の承認**（AGENTS.md「安全系・制御系の設計変更は人間レビュー必須」）

RL（#89 / #105）との関係:

- 0058 §2.1 は環境の observation を `SupervisorInput` そのものと定めた。
  フィールドを足すことは **observation 空間を変えること**である
- したがって、ヒントを読む policy artifact は「ヒントを使う」ことを**宣言する**。
  宣言の無い artifact にはフィールドを伏せて（masked）渡す。**黙って渡さない**
- ヒントの無い期間に記録した trajectory で、ヒントを使う policy を評価しない（0054 の帰属規則）
- 既定は `supervisor.workload_hint.enabled: false`。Stage B を有効にするのは設定であり、
  **設定で戻せる**

---

## 3. Consequences

### 良くなること

- 書き込みの入口が **1本・1プロセス・1ソケット**のまま増えない。認可・検証・保存・監査が1箇所に集まる
- Workload Regime が **Telemetry の純関数のまま**残る。再生・offline evaluation・dataset の
  契約（0031 / 0054）が壊れない。「Telemetry を優先する」試験が自明になる
- ヒントが間違っていても、**温度が上がる向きには効かない**（§2.8 の単調性）。
  嘘の代償は騒音と電力であって、安全ではない
- trace から「ヒントがあったか」ではなく **「ヒントで判断が変わったか」** が分かる。
  要らなかったという結論も、証拠として出せる
- Stage A だけなら制御を変えずに証拠を集められる。**測る前に効かせない**

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| ヒントを書く側は「どんな負荷か」しか言えない（step / epoch / 進捗を送れない） | v1 では読む側が無い。読む側を決めてから足す（未決 #2）。閉じた語彙で始めるほうが、後から狭めるより安い |
| 矛盾した行が二度と復活しない（落ちていないジョブでも） | 書き直しは1行。復活させる仕組みより、誤検知の保持時間（`contradiction_hold_ms`）を調整するほうが理解しやすい |
| 設定項目が増える（`context_overrides` の対応表、TTL、保持時間、許可リスト） | すべて `config/fan-policy.yaml` の1つの節にまとめ、起動時に `output_bounds` と単調性を検証する。満たさなければ起動しない |
| `SupervisorInput` が変わると RL の observation が変わる | artifact に宣言を持たせ、宣言の無い artifact には伏せる（§2.10）。混ざった比較を `promotable` にしない（0058 §2.2 と同じ規律） |
| 圧縮再生・replay ではヒントを扱えない | ヒントの試験は Mock / 注入した `SupervisorInput` で行える（AGENTS.md ルール7）。時計を混ぜるくらいならヒント無しで動かす |
| `events` にヒント行が増える（`GET /api/v1/events` の応答も増える） | 行数はジョブの数しかない。期間指定は既存の読み取り契約のまま |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| **読み取り API に `POST /api/v1/workload-hint` を足す** | GET-only の契約（0009 §3 / FR-307）を破る。AI ツールの窓口（`coldaisle.server`）と同じプロセスに書き込み経路ができる。0045 §4 と同じ |
| **ヒント専用の HTTP / TCP の入口を新設する** | 同じホストの全利用者が接続でき、相手の uid が分からない。ファイル権限という門を持てない。認可を作り直すことになる |
| **spool ディレクトリ / YAML ファイルを書かせ、デーモンに監視させる** | 認可が「そのディレクトリに書けるか」だけになる。検証結果を書き手へ返せない。部分書き込み・消し込み・監視の面倒が増える。#107 の候補だが 0045 §4 と同じ理由で採らない |
| **job launcher（スケジューラ）と直接連携する** | coldaisle が外部スケジューラの API・資格情報・可用性に依存する。スケジューラが落ちたときに制御ループを待たせる経路を作りうる。launcher 側が `coldaisle-event` を1行呼べば済む |
| **制御デーモンの環境変数 / CLI 引数でヒントを渡す** | ヒントは動的に変わる。渡し直すたびに制御ループの再起動が要る。ヒントのために制御を止めるのは本末転倒 |
| **ヒントごとに別のソケット・別のグループを設ける** | 何も動かせないメッセージのために認可面を2つに増やす。守るべきものが増えず、設定の誤りだけが増える（§2.2） |
| **`WorkloadRegimeEstimator` にヒントを入れ、閾値や確認時間を動かす** | Regime が Telemetry から再現できなくなり、dataset（0031）と offline evaluation の帰属（0054）が壊れる。「Telemetry を優先する」ことを測れなくなる（§2.5） |
| **`expected_duration` を MPC の horizon や残り時間の countdown にする** | Supervisor は「負荷があと何時間続くか」を断定しない（AGENTS.md）。ジョブは途中で落ちる。申告を時間軸の真値として扱うことになる |
| **ヒントから Demand floor（先行冷却）を直接上げる** | Control Pipeline を迂回する（AGENTS.md ルール2）。上げる向きでも、Supervisor → MPC → Guard → Safety を通らない経路を作らない |
| **「これから暇になる」種類のヒントも受理し、冷却を弱める** | 書き手が間違えたときの代償が温度になる。§2.8 の単調性が成り立たなくなり、「嘘をつかれても安全側」という性質を失う |
| **書き手に `ts` や絶対時刻の期限を指定させる** | 過去の時刻で記録を作れる（0045 §2.5）。期限を壁時計の絶対時刻で持つと、時計が飛んだときに不死のヒントが作れる |
| **`job_id` / `user` / `host` を payload や trace に載せる** | 際限のない同一性になる（§2.9）。public リポジトリの衛生（0021 / ルール10）にも触れる。必要なら `peer_uid` と `event_id` から辿れる |
| **矛盾したヒントを、Telemetry が一致したら再採用する** | 落ちたジョブのヒントが蘇る。書き直しは1行で済む（§2.7） |
| **いまの値を `system_state.sys.workload_hint` に映す** | 読み取り API 越しに「いまの負荷はこれ」と読める形になる。ヒントは真値ではない。`gpu_mode` が映されるのは Server Health（0040）という読み手がいるから（§2.4） |
| **`v` を 2 に上げてヒントの形を表す** | `gpu_mode` を巻き込む。封筒の版とヒント本体の版を分ける（§2.3） |
| **いま Stage B まで実装する** | #107 の未決そのもの。「Telemetry だけの Regime 推定で不足が見えてから」という条件を、見る前に満たしたことにしてしまう（§2.10） |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | Stage B の着手条件の数値（「Regime が追いつかなかった tick」の割合の閾値と観測期間） | Stage B の実装 Issue。**Stage A の実測を見てから** |
| 2 | `progress` / `step` / `epoch` を受理するか（読む側を決めてから） | 別 Issue。新しい決定記録で `hint_v` を上げる |
| 3 | `context_overrides` の対応表の具体値、TTL・保持時間の本番値 | `config/fan-policy.yaml`（#103 / #74 の後）。設定であり、記録には書かない |
| 4 | 既知 source の許可リストの初期値 | 同上。運用で増える |
| 5 | ヒントをダッシュボード（#46 / 0046）に縦線として出すか | 別 Issue。保存と公開を同時に広げない（0030 §2） |
| 6 | RL policy artifact の「ヒントを使う」宣言の形 | #89 / 0058 の続き。Stage B の前提 |
