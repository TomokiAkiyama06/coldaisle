# 決定記録 0071: decision trace の読み取り API（エンドポイント・ページング・版の扱い・registry の記録・LLM との境界）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-29
- **Supersedes**: なし。次の未決事項に答える記録であり、各記録の本文は書き換えない
  - [0046](0046-airflow-ui.md) §5 #2（判断記録を読む API の形）
  - [0062](0062-model-registry-operations.md) §5 の1項目め（registry の lifecycle event を decision trace へ載せるか）
- **関連**: [0009](0009-read-api.md)（GET-only・環境変数の設定・`truncated`）/
  [0015](0015-llm-tools.md) / [0018](0018-tool-exposure.md) /
  [0028](0028-fan-control-contracts.md) §2.2 / §2.3 /
  [0030](0030-control-decision-trace-storage.md)（**前提。§2.1 を参照**）/
  [0045](0045-local-socket-write-entry.md) / [0046](0046-airflow-ui.md) §2.2 / §2.3 / §5 /
  [0059](0059-decision-trace-model-artifact.md) / [0060](0060-control-loop-runtime.md) §2.4 / §2.7 /
  [0062](0062-model-registry-operations.md) §2.5 / §5 /
  [0064](0064-workload-hint-entry-and-supervisor-prior.md) §2.9 / §2.10 /
  [0066](0066-workload-hint-stage-b-conditions.md)（§2.9 の `age_ms` の置き換え）/
  `docs/api-contract.md` / `docs/requirements.md` FR-307 / FR-504 /
  AGENTS.md ルール 1 / 2 / 6 / 8 / 9 / 10
- **対象 Issue**: #106（関連: #104 / #107 / #82 / #66）

---

## 1. Context

decision trace（`ControlTick`）は、制御デーモンが tick ごとに SQLite の `control_traces` へ
追記している（`coldaisle.control.logging.ControlTraceLogger`、migration `0002_control_traces`）。
形は 0030 §2 のとおりで、主キー `(ts_ms, tick_id)`、`schema_version` と完全な JSON を保存する。
ところが**読む経路が HTTP に無い。** 読んでいるのは Offline Evaluation（`coldaisle.evaluate`）だけで、
これはストアを直接開く。この欠落が、3つの Issue を止めている。

| Issue | 止まっている点 |
|---|---|
| #106 | エアフロー画面は制御由来の項目をすべて「未接続」と出している（0046 §2.3）。模擬データの `page.control` を実データで埋める API の形が未決（0046 §5 #2） |
| #104 | 受入基準「promotion / rollback が decision trace へ残る」。0062 §2.5 は、tick に対応しない registry event を 0030 の trace へ載せるかを #82 側の決定に委ねた（0062 §5） |
| #107 | 0064 §2.9 は trace に載せるヒントの塊の中身を決めたが、Stage B の前提1「#82 の decision trace が保存されていること（0030）」の 0030 が `Proposed` のまま。載せた塊を**どこから読むか**も決まっていない |

加えて、trace の読み取りには、既存の読み取り API（0009）には無い論点が3つある。

1. **版が混ざる。** 保存済みの trace は v1〜v9 が混在しうる（`SCHEMA_VERSION = 9`）。
   0030 §2 は「既存 trace を新しい意味として解釈しない」と決めている。読む側がどこで版を見分けるかを決める必要がある
2. **量が多い。** tick は秒以下の周期（`safety.yaml` の `tick_ms`）で、保持は `control_trace_days`（30日）。
   `events` のように「上限を超えたら古い側を落とす」（0009 §2.4）と、証拠を黙って欠くことになる
3. **LLM との境界。** trace は制御 ML の時系列そのものであり、FR-504 は生の時系列をプロンプトへ入れることを禁じる。
   読み取り API は `coldaisle.server` で AI ツールの窓口と同じアプリに載る

仕様に書かれていない判断なので、実装より先にここへ記録して承認を求める（AGENTS.md「決定記録」）。

---

## 2. Decision（推奨案）

### 2.1 前提: 0030 の承認。承認してほしいのは §2 と §5 の1項目めだけ

本記録は 0030 の保存の形の上に API を置く。**0030 が `FINAL` になるまで本記録も `FINAL` にしない。**
所有者に承認してほしいのは次の範囲で、どれも main で実装済みである（承認は実装の追認になる）。

| 0030 の箇所 | 内容 | main の実装 |
|---|---|---|
| §2 | 同じ SQLite に追記専用の `control_traces`。主キー `(ts_ms, tick_id)` で上書きしない | `0002_control_traces.sql`、`SqliteStore.record_control_trace`（`INSERT OR IGNORE`） |
| §2 | `schema_version` と完全な `ControlTick` JSON。JSON object を SQLite とアプリの両方で検証 | `CHECK (json_type(trace_json) = 'object')`、`ControlTraceRecord` |
| §2 | 読み出しは半開区間 `[from, to)` | `SqliteStore.control_traces` |
| §2 | 意味を変えたら schema version を上げ、既存 trace を新しい意味で読まない | `ControlTick` の版ごとの validator（v1〜v9） |
| §2 | 書くのは Control Logging だけ。AI・API・Collector・Backend は書き換えない | 書き手は `control_daemon` の `ControlTraceLogger` だけ |
| §5 の1項目め | `config/retention.yaml` の `control_trace_days` を `coldaisle-rollup` が適用。コードに既定値を持たない | `config/retention.yaml`、`store/rollup.py` |

0030 §5 の残り（SQLite 外への export は #90 / #91、actuator 固有フィールドは後続の記録）は
**開いたままでよい。** 本記録はそれに依存しない。
0030 の Status の遷移は内容の変更ではないので、README「追記のみ」の例外で足りる。

### 2.2 エンドポイントは2つ。どちらも GET

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/api/v1/control/latest` | 最新の1 tick。エアフロー画面の「現在の状態」（0046 の `page.control`） |
| GET | `/api/v1/control/traces?from=&to=&window=&after=&limit=` | 期間内の trace を時刻の昇順で。1 tick の履歴を辿る・グラフへ重ねる |

- **GET だけ。** FR-307 / api-contract §1。OpenAPI に `get` 以外が現れないことを固定する既存の試験の範囲に入る
- 制御の状態を変える入口はここに作らない。モード変更は `coldaisle-fand` の Unix ソケットだけ（0028 §2.2）、
  イベントとヒントは `coldaisle-eventd` だけ（0045）。registry の操作は CLI だけ（0062 §2.1）
- API はストア（L1）の読み出しだけを使う。**API から `coldaisle.control` を import しない**（§2.4 の理由）
- API はシリアルポートにも、制御デーモンのソケットにも触れない（AGENTS.md ルール 6）。制御デーモンが
  止まっていても API は動き、trace が無いことをそのまま返す

#### `/api/v1/control/latest`

```json
{
  "trace": {
    "ts_ms": 1790000000000,
    "ts": "2026-09-21T14:13:20+00:00",
    "tick_id": 184390,
    "schema_version": 9,
    "age_ms": 820,
    "body": { "...": "保存した ControlTick の JSON をそのまま" }
  }
}
```

- trace が1件も無い（制御デーモンを動かしていない・保持期間で消えた）ときは **200 で `"trace": null`**。
  404 にしない。ルートが無いのか、記録が無いのかを取り違えないため。画面はこれを「未接続」と出す（0046 §2.3）
- `age_ms` はサーバの `store.clock` で数える（`/health` と同じ時計）。**古いかどうかの判定は §2.6**

#### `/api/v1/control/traces`

```json
{
  "from_ms": 1790000000000,
  "to_ms": 1790000600000,
  "traces": [ { "ts_ms": 0, "ts": "…", "tick_id": 0, "schema_version": 9, "body": { } } ],
  "has_more": true,
  "next_after": "1790000123456:184390"
}
```

- 期間は `[from, to)`（0030 §2 / 0004）。`window` は既存の `/events` と同じ `_resolve_range` で解く
- **昇順とキーセット方式のページング。** `after` は直前のページの最後の `(ts_ms, tick_id)` を
  `"<ts_ms>:<tick_id>"` で表したもので、次のページは `(ts_ms, tick_id) > after` を返す
  - `tick_id` は制御デーモンの再起動で 0 に戻る（`control/loop.py`）。**`tick_id` 単独では位置を表せない。**
    主キーそのものを cursor にすれば、再起動をまたいでも重ならない
  - offset 方式は採らない。制御デーモンが末尾へ追記し、`coldaisle-rollup` が先頭を消すあいだに、
    offset はずれて行を飛ばす・重ねる
  - `after` の行が保持期間で消えていても、比較は値で行うので続きから読める
- **上限を超えたら `has_more: true` を返し、黙って落とさない。** `/events` の「古い側を落として
  `truncated`」は採らない（§4）。trace は判断の証拠で、途中が欠けた列は事故調査に使えない
- `limit` の既定と上限は環境変数（0009 §2.9 と同じ理由。`uvicorn` に引数を渡せない）。
  名前と値は §5 #2

### 2.3 本文（`body`）は保存した JSON をそのまま返す。版の解釈は読む側で行う

- `body` は `control_traces.trace_json` を JSON として解いた object で、**API は項目を足さない・消さない・直さない**
- 外枠（`ts_ms` / `ts` / `tick_id` / `schema_version` / `age_ms`）は API の版（`/api/v1`）に属し、
  `ControlTick` の版が上がっても変えない。`ControlTick` が v10 になっても `/api/v1` の形は変わらない
- 読む側は **`schema_version` で分岐する。** 画面（`airflow.js`）の変換は1か所に置き（0046 §3 の
  「API ができた時点で変換を1つ書く」）、版ごとの欄の有無を次のように見分けて出す

| 状態 | 例 | 画面の表示 |
|---|---|---|
| その版に欄が無い | v4 の trace に `model_gate`（v5〜）が無い | 「この版の記録には無い」。**「無し」「正常」と言わない** |
| 欄はあるが値が無い | v9 で `supervisor: null`（Supervisor の出力が無い tick） | 項目ごとの既存の表示（例: 介入なし） |
| 画面が知らない版 | 画面の実装より新しい v10 | 「未対応の版」として制御由来の項目を出さない。**通常状態に見せない** |

これは 0062 §2.5 / 0064 §2.9 と同じ規律である。「記録されていない」と「起きていない」を区別する。

### 2.4 API は trace を検証し直さない・投影しない

`body` を `ControlTick.model_validate` に通さない。サーバ側で画面向けの形（`page.control`）に投影もしない。

- **版を解釈する場所を1つにする。** 投影すると、版ごとの意味の解釈がサーバとストアの読み手（Offline
  Evaluation）と画面の3か所に散る。API が `coldaisle.control.schema` を import することにもなり、
  読み取り API が制御の内部型に依存する
- 検証し直すと、**いまのコードが読めない古い trace を API が 500 で返せなくなる**、あるいは黙って
  捨てる。保存時に JSON object であることは SQLite の CHECK とアプリで検証済み（0030 §2）で、意味の検証は
  書いた時点の `ControlTick` が済ませている

### 2.5 registry の lifecycle は「tick が使っていた registry の版」として毎 tick 載せる（#104）

0062 §5 への答え。**registry event 用の表を足さない。tick に対応しない記録へ tick_id を合成しない**
（0062 §4 の却下を守る）。代わりに、**その tick の判断がどの production pointer の下で出たか**を tick 側に載せる。

- `ControlTick` に `registry` の塊を足し、schema version を**実装時点の値から1つ上げる**
  （0064 §2.9 のヒントの塊と同じ手順。どちらが先に入っても、それぞれが次の版を取る）
- 中身は、制御デーモンが起動時に読んだ registry snapshot の `revision` と、kind ごとに
  - production の `artifact_sha256`（無ければ `None`）
  - その production を成立させた pointer 変更の `RegistryAuditEvent.trace_metadata()`（0062 §2.5。path を含まず、欄は常に揃える）
- **毎 tick 載せる。** 起動した tick だけに載せると、30日の保持（`control_trace_days`）より長く動いた
  デーモンでは、その tick が消えて「どの昇格の下で動いているか」が trace から言えなくなる
- 誰が・なぜ・いつ承認したかの正本は `registry.json` の audit のまま（0062 §2.5）。trace には `revision` と
  path を含まない metadata だけを置き、全文は `coldaisle-registry audit --pointer-changes` で引く
  （0064 §2.9 の「全文は `event_id` から引く」と同じ関係）
- 読み取り API はこの塊を §2.3 のとおり `body` の一部として返すだけで、registry のファイルを読まない
- **いまの制御デーモンは起動時にしか registry を読まない。** promotion / rollback が trace に現れるのは、
  それを反映して再起動した最初の tick からである。これは正しい。trace は「判断がどの artifact で出たか」の
  記録であり、pointer を動かした瞬間の記録ではない（それは audit の役割）

### 2.6 古さの判定は読む側で、周期は trace 自身から取る

`/control/latest` は `age_ms` を返すだけで、`ok` / `stale` を決めない。

- v8 以降の trace は `runtime.tick_period_ms` を持つ（0060 §2.4）。画面は「`age_ms` が周期の何倍を超えたら古い」
  で判断し、その倍数は `config/airflow-ui.yaml` に置いて `GET /api/v1/airflow/config` で返す（AGENTS.md ルール 9）。
  **API は `safety.yaml` を読まない**（読み取り API が制御の設定に依存しない）
- v1〜v7 の trace は周期を持たないので、画面は経過時間だけを出し、「古い／新しい」を言わない
- 倍数の値は §5 #1

### 2.7 ヒントの塊（#107）は §2.3 の `body` に入るだけ。専用の口を作らない

- 塊の中身と閉じた語彙は 0064 §2.9（`age_ms` の定義は 0066）で決まっている。本記録は足さない・減らさない
- 全文（`note` / `source` / `peer_uid`）は既存の `GET /api/v1/events` にある。画面は `event_id` で突き合わせる
- **trace の API からヒントを書く・消す経路は無い**（§2.2。書く入口は 0045 のソケットだけ）
- #107 の Stage B の前提1（0064 §2.10）は、§2.1 の 0030 の承認で「保存の形が決まっている」状態になる。
  前後比較に要る読み出しは Offline Evaluation がストアから直接行うので、Stage B は本記録の API の実装を待たない

### 2.8 LLM には渡さない。AI ツールに trace を足さない（FR-504）

- `/api/v1/control/*` は**画面と人のための HTTP 読み取り口**であり、`/api/v1/tools` の一覧に載せない。
  AI 向けツールは 0015 / 0018 の読み取り専用の5つのまま
- trace の列は制御 ML と同じ粒度の時系列で、FR-504 / AGENTS.md ルール 8 がプロンプトへの直接投入を禁じる対象である。
  **制御 ML が Window を直接扱ってよいこと（ルール 8 のただし書き）と、LLM に渡すことを混同しない**
- 実装 PR で `docs/api-contract.md` の表に2行を足すとき、「LLM のプロンプトへ直接入れない。要約が要るなら
  集計済みのツールを別に定める」と注記する。Workspace 側のチャットが API の応答をそのままプロンプトへ貼る実装を防ぐため
- 将来 LLM に「なぜこの回転数か」を説明させたい場合は、期間内の `bound_by` / fault code / fallback の理由を
  **閉じた語彙で数えた集計**を返すツールを、別の決定記録で定める（§5 #5）。その場合も LLM は
  Fan Demand・PWM・registry・authority に触れない（ルール 1）

---

## 3. Consequences

### 良くなること

- #106 の画面が、模擬データと同じ `page.control` の形を実 trace から作れる。変換は画面の1か所
- 「なぜこの回転数か」を、`/control/traces` で前後の tick まで辿れる。途中が黙って欠けない（§2.2）
- `ControlTick` の版を上げても `/api/v1` の形は変わらない。API の版と trace の版が独立する（§2.3）
- #104 の「promotion / rollback が trace へ残る」が、束縛していない識別子を作らずに満たせる。
  どの tick がどの昇格の下で出たかが、保持期間の中のどの tick からも言える（§2.5）
- #107 は 0030 の承認で Stage B の前提1が満たされる（§2.1 / §2.7）

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 版の解釈を画面側（JS）で持つ。v1〜v9 の差を JS に写すことになる | 分岐を1つの変換関数に集め、`tests/fixtures/control_tick_v1.json` と版ごとの fixture を node の試験で読ませる（0044 の範囲）。知らない版は「未対応の版」と出し、通常に見せない |
| 1 tick の JSON を丸ごと返すので応答が大きい | `limit` の上限を環境変数で絞る。履歴の俯瞰は `series`（メトリクス）で行い、trace は drill-down に使う |
| `registry` の塊を毎 tick 載せると保存量が増える | 載せるのは kind の数（数個）ぶんの sha と path を含まない metadata だけ。起動した tick だけに載せる案は、保持期間で証拠が消えるので採らない（§4） |
| promotion は再起動するまで trace に現れない | trace は「判断がどの artifact で出たか」の記録。pointer を動かした時刻・理由は audit にあり、`revision` で突き合わせられる |
| API が trace を検証し直さないので、壊れた意味の trace も返す | 書き手は制御デーモンだけで、保存時に型を通っている。SQLite の CHECK が JSON object であることを保証する。読み手は `schema_version` で分岐する |
| 古さの判定が API（`/health`）と画面で別の場所にある | `/control/latest` は `age_ms` と周期（`body.runtime`）を必ず返し、判定の倍数は設定で1か所に置く |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| **A. サーバ側で画面向けの形（`page.control`）に投影して返す** | 版の解釈がサーバ・Offline Evaluation・画面に散る。API が `coldaisle.control.schema` を import する。投影を変えるたびに API の版を上げることになる（§2.4） |
| **B. `ControlTick.model_validate` で検証し直してから返す** | いまのコードが読めない古い trace（または将来の版を読む古い API）で 500 になるか、黙って捨てる。「記録されていない」と「起きていない」を区別できなくなる |
| **C. `/events` と同じく、上限を超えたら古い側を落として `truncated`** | 判断の列の途中が欠けても気付きにくい。trace は証拠であり、欠けた列で「なぜ」を辿らせない（§2.2） |
| **D. offset / page 番号でページングする** | 末尾への追記と保持期間の削除が同時に起きるので、ページがずれて行を飛ばす・重ねる |
| **E. `tick_id` 単独を cursor にする** | 再起動で 0 に戻る。主キー `(ts_ms, tick_id)` でなければ位置を表せない |
| **F. `WS /api/v1/control/stream` で押し出す** | 画面は `/control/latest` を周期的に読めば足りる。0009 §2.6 と同じ理由で、まず問い合わせで作る。要るなら後で足す（§5 #4） |
| **G. registry event 用の表（`registry_events`）を SQLite に足し、CLI に書かせる** | 書き手が2つになる（CLI と制御デーモン）。`registry.json` の audit と二重の正本になり、食い違ったときにどちらを信じるか決められない |
| **H. registry event に tick_id を合成して `control_traces` に書く** | 0062 §4 がすでに却下。束縛していない識別子を作る |
| **I. `registry` の塊を起動した tick だけに載せる** | 保持期間（30日）より長く動くと、その tick が消えて「どの昇格の下か」が trace から言えなくなる |
| **J. trace を AI ツールに足す（`get_control_traces`）** | FR-504 / ルール 8。生の時系列をプロンプトへ入れる経路になる。説明が要るなら集計済みのツールを別に決める（§2.8） |
| **K. 読み取り API が `safety.yaml` を読んで古さを判定する** | 読み取り API が制御の設定に依存する。周期は trace 自身（v8 の `runtime`）が持っている |
| **L. 0030 の承認を待たずに本記録だけを FINAL にする** | 保存の形が変わりうる前提の上に API の契約を固定することになる。承認してほしい範囲を §2.1 に絞った |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | 画面が trace を「古い」と言う倍数（`runtime.tick_period_ms` の何倍か）と、`config/airflow-ui.yaml` での項目名 | #106 の実装 PR。設定であり、記録には値を書かない |
| 2 | `limit` の既定と上限の環境変数の名前と値（0009 §2.9 の表へ足す） | #106 の実装 PR |
| 3 | 条件での絞り込み（fault のある tick だけ・Fallback の tick だけ等）。版ごとに JSON の位置が違うため、閉じた語彙の索引列を足すかを含む | 必要が出てから別 Issue。いまは期間とページングだけ |
| 4 | `WS /api/v1/control/stream` を足すか | 画面の実運用で周期読み出しが足りないと分かってから |
| 5 | LLM 向けの集計ツール（`bound_by` / fault / fallback の理由の件数など）の形 | 別の決定記録（0015 / 0018 の続き） |
| 6 | `RegistryHealthReport.trace_metadata()`（起動時検証の結果）も `registry` の塊に入れるか | #104 の実装 PR。入れるなら同じ版上げに含める |
| 7 | 保持期間の境界（いま読める最古の `ts_ms`）を応答に含めるか | #106 の実装 PR |
| 8 | `requirements.md` に FR を足す番号と文言、`api-contract.md` の表 | #106 の実装 PR（本記録は文書を書き換えない） |
| 9 | SQLite 外への export（0030 §5 の2項目め） | #90 / #91。本記録は扱わない |
| 10 | Workspace（#60）からエアフロー画面へのリンク（0046 §5 #5） | #60 |
