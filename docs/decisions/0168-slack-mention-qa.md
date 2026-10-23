# 決定記録 0168: Slack でボットにメンションすると、読み取り専用のツールで集計した値から LLM が答える

- **種別**: Decision Record
- **Status**: FINAL（2026-10-23、リポジトリ所有者の承認。§5 の 1〜8 はすべて推奨の案）
- **Date**: 2026-10-23
- **Supersedes**: [`0001`](0001-initial-project-decisions.md) D-06（「AI チャット UI は実装しない」）のうち、**Slack のメンションへの返答に限って**
  coldaisle が会話の受け口を持つことを認める部分のみ（Web の チャット UI は作らないまま。ツールを使う会話の正は Workspace のまま）/
  [`0143`](0143-claude-api-interim-provider.md) §2.3 の「ツールは送らない」のうち、本記録の経路に限ってツールの定義と結果を送る部分のみ
- **関連**: [`0001`](0001-initial-project-decisions.md) D-06 / D-10 / [`0013`](0013-notifications.md) / [`0015`](0015-llm-tools.md) /
  [`0018`](0018-tool-exposure.md) / [`0131`](0131-gpu-fan-service-alert.md) §2.4a / [`0143`](0143-claude-api-interim-provider.md) /
  [`0147`](0147-slack-alert-graph.md) §2.1 / [`0163`](0163-shared-vllm-gpu-ai-service.md) §2.2 /
  `docs/requirements.md` FR-502 / FR-503 / FR-504 / FR-507 / AGENTS.md「絶対に守るルール」1・2・8・9・10
- **対象 Issue**: #374

---

## 1. Context

所有者は Slack で通知を受けている（0135 / 0147）。Claude API（0143）でアラートの説明と日次レポートの所見が作られるようになったので、
「いまの GPU の温度は？」「昨日いちばん熱かったのはいつ？」をボットにメンションして聞き、その場で答えを得たい（2026-10-23 の所有者の要望）。

いまの決まりでは作れない。

- 0001 D-06 は「coldaisle にチャット UI を作らない。会話は Workspace の側が持ち、coldaisle は読み取り専用のツールを出すだけ」とした
- 0143 §2.3 は、Claude API へツールの定義と結果を送らないとした（`AnthropicProvider.chat()` は `tools` があれば呼ばない）
- coldaisle は tool loop を持たない（0163 §2.2）。いまの Slack ボット（0147）は送るだけで、メンションを受け取らない

## 2. Decision（2026-10-23 所有者が §5 の推奨の案で承認）

### 2.1 受け取り方: Slack の Socket Mode で、新しい常駐の unit `coldaisle-slackbot`

- Slack の Events API の HTTP の受け口は使わない。**Socket Mode**（こちらから WebSocket で Slack へつなぐ）で `app_mention` だけを受ける。
  外から入る口を開けず、API の 127.0.0.1 の待ち受け（D-10）も変えない
- 新しいトークン `COLDAISLE_SLACK_APP_TOKEN`（`xapp-`、scope は `connections:write`）を `/etc/coldaisle/coldaisle.env` に置く。
  Bot トークンは今の `COLDAISLE_SLACK_BOT_TOKEN` を使い、scope に `app_mentions:read` を足す（`chat:write` は 0147 で持っている）。
  どちらもログ・例外・repr に出さない（0147 と同じ）
- 新しいプロセス `coldaisle-slackbot`（console script と `deploy/systemd/coldaisle-slackbot.service`）。uid は `coldaisle`、DB は読むだけ。
  取り込み・API・fand・telemetry のプロセスに依存させず、依存もされない。止まっても監視と通知は今のまま動く（FR-507）
- WebSocket は依存の `websockets`（`uvicorn[standard]` で既に入っている）で話す。Slack の SDK は足さない（0143 §2.10 と同じ方針）
- 受けた event はすぐ `ack` する（3 s の期限）。答えは別のスレッドで作り、**メンションのスレッドへ** `chat.postMessage` で返す
- **つなぎ直し**（PR #375 の Codex の指摘）: Slack は Socket Mode の接続を数時間ごとに入れ替え、`disconnect`（`warning` / `refresh_requested` など）を
  送る。警告が届かないこともある。そこで `disconnect` を受けたとき・WebSocket が閉じたとき・設定の時間（`slack_qa.idle_reconnect_s`）
  何も受けないときに、`apps.connections.open` から新しい URL を取り直してつなぎ直す。失敗が続けば間隔を倍にする（上限は設定）
- **再配信の重複を処理しない**（同）: ACK が届かなかった event を Slack は再配信する（`retry_attempt` / `retry_reason`）。外側の payload の
  `event_id` と、(チャンネル, メッセージの `ts`) の両方を、処理中・処理済みとして一定の件数だけ覚え、同じものには 2 回答えない

### 2.2 誰に答えるか: チャンネルとユーザーの許可リスト

- 答えるのは、許可リストのチャンネル**かつ**許可リストのユーザーのメンションだけ。どちらも ID（`C…` / `U…`）で、
  `COLDAISLE_SLACK_QA_CHANNELS` / `COLDAISLE_SLACK_QA_USERS`（カンマ区切り）に置く（リポジトリに書かない。ルール 10 の趣旨）
- どちらかが空・未設定なら、**どのメンションにも答えない**（起動時に 1 度だけ警告。黙って全員に答えない）
- リストの外のメンション・ボット自身や他のボットの投稿・編集・DM には答えない（返事もしない。何が許可されているかを外に見せない）

### 2.3 答え方: 読み取り専用のツールを使う短い tool loop

- LLM に渡すツールは FR-502 の読み取り専用のツールのうち **`get_latest` / `get_stats` / `list_alerts` / `describe_system` の 4 つ**。
  **`get_stats` に期間の指定（`from_ms` / `to_ms`。省けば今の `window` のまま）と、最大値・最小値をとった時刻（`max_ts_ms` / `min_ts_ms`）を足す**
  （「昨日いちばん熱かったのはいつ？」に答えるため。PR #375 の Codex の指摘・§5 の 8）。返すのは集計値とその時刻だけで、点の列は返さない。
  Workspace のチャットが使う同じツールにも同じ欄が増える（読み取り専用のまま）
  `query_series`（時系列の点の列）は渡さない（ルール 8 / FR-504。点の列は「集計済み」とは言い切れず、Claude API へ送るので狭く取る）
- ツールは今の `ToolRegistry`（`coldaisle.ai.tools`）をそのまま使い、0131 §2.4a の除外（`config/report.yaml`）を当てる。除外の一覧が読めなければ答えない
- **この経路で外へ出るツールの結果の欄を絞る**（PR #375 の Codex の指摘）。`describe_system` の `devices`（`device_id`・`fw`・センサーの
  `channel`・`gpio`・ROM の末尾・分解能）は**この経路では渡さない**（個体の識別に近く、質問に答えるのに要らない）。渡すのは
  メトリクスの名前・単位・表示名・派生値の式・閾値だけ。`list_alerts` は `rule_id`・重大度・状態・メトリクス・`started_ms` / `fired_ms` /
  `resolved_ms`・`trigger_value`・`threshold`・`detail`（ルールが作る文）を渡す。`get_latest` / `get_stats` はメトリクスの値・品質・時刻と集計値を渡す。
  これらが Claude API（のちに PAW の vLLM）へ出ることを本記録で承認の対象にする
- **decision trace・制御の状態・hint の記録・案件資料は渡さない**（0071 / ルール 8）。Fan の制御について聞かれても、ツールにある値（Fan の回転数・PWM の
  メトリクス）以上は答えない
- tool loop は 1 つの質問につき **最大 4 往復**（ツールの呼び出しを返す応答が 4 回で打ち切り、そこまでの結果で答えさせる）。上限は設定に置く（ルール 9）
- 書き込み・実行のツールは無い（ルール 1 / FR-503）。LLM の出力から Fan Demand・PWM・設定・較正・アラートの状態を変える経路を作らない（ルール 2）
- system の指示: 日本語で短く答える／数値はツールの結果にあるものだけを使う／分からなければ分からないと書く／確度を書かない（FR-508 の精神）
- **答えの数値の検査**（0143 §2.11 と同じ形）: 答えに出る数値が、その質問で得たツールの結果に無ければ、答えの末尾に
  「※ ツールの結果に無い数値を含みます（未検証）」を付ける（答えは捨てない。§5 の 5）

### 2.4 返す文の扱い

- 返す文は `mrkdwn` で送るが、`<!channel>` / `<!here>` / `<@U…>` などの通知を起こす記法と `<…|…>` のリンクを取り除く
  （質問文に書かせて全員へ通知させる経路を作らない）。`&` `<` `>` は Slack の規則でエスケープする
- 長さは 1 投稿 3000 文字までに切る
- LLM が使えない（止まっている・上限・鍵が無い）ときは、「いま AI が使えません。ダッシュボードで確かめてください」の固定の文だけを返す
  （値を自分で作らない）

### 2.5 送るもの・回数と費用

- 外（Claude API、のちに PAW の vLLM）へ送るのは、質問文・system の指示・ツールの定義・**ツールの結果（集計値）**だけ。生の時系列・trace は送らない
- 0143 §2.3 の「ツールは送らない」を、本記録の経路に限って開く。`AnthropicProvider` に Messages API の `tool_use` / `tool_result` を足す
  （OpenAI 互換の Provider は既に `tools` を送れる）。
- **ツールの結果を返せる会話の形を足す**（PR #375 の Codex の指摘）。`ChatMessage` に、assistant の `tool_calls` と、role `tool` の発話の
  `tool_call_id` を持たせる（既定は空で、今の 3 経路の要求の形は変えない）。OpenAI 互換の Provider はそのまま送り、`AnthropicProvider` は
  assistant の `tool_use` の塊と、直後の user の `tool_result` の塊（`tool_use_id` で対応）へ直す。日次レポート・アラートの説明・Server Health はツールを送らないまま
- 回数の上限は `config/ai.yaml` に**この経路の別の上限**を置く（`slack_qa.daily_call_limit`、1 日 100 回。tool loop の 1 往復を 1 回と数える）。
  日次レポートとアラートの説明の上限を食わない。加えて 1 ユーザー 1 分 3 質問まで（超えたら「少し待ってください」と返す）
- 質問・答えの本文はログに残さない（長さ・使ったツールの名前・往復の数・所要時間だけ）。0013 と同じく秘匿情報を出さない

### 2.6 試験（実機不要。ルール 7）

- 許可リストの外・ボット自身・空のリストで答えない
- `disconnect` を受けた・WebSocket が閉じた・設定の時間何も受けないとき、新しい URL でつなぎ直す
- 同じ `event_id` / 同じ (チャンネル, `ts`) の再配信に 2 回答えない
- `describe_system` の `devices` がこの経路の結果に入らない・`get_stats` の期間の指定と最大・最小の時刻
- tool loop の 2 往復目以降に、前の往復のツールの結果が両 Provider の形で送られる
- tool loop が 4 往復で打ち切られる・`query_series` と未定義のツールを呼ばせても結果を渡さない
- 除外のメトリクスが答えの材料に入らない・除外の一覧が読めないと答えない
- 通知を起こす記法とリンクが返す文から消える
- LLM が使えないと固定の文だけを返す・上限で呼ばない
- `coldaisle.control` と `ingest` を import しない（import の走査）

## 3. Consequences

### 良くなること

- 手元のスマホの Slack から、ダッシュボードを開かずに今の状態と最近の傾向を聞ける
- 既存の読み取り専用のツールと除外の仕組みをそのまま使うので、LLM に見せる範囲は Workspace のチャットと同じに揃う

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| 新しいトークン（Socket Mode）と常駐のプロセスが増える | 別 unit で、止めても監視・通知・制御に影響しない。トークンは env にだけ置く |
| ツールの結果（集計値）が Claude API へ出る | 渡すのは Workspace のチャットと同じ読み取り専用のツールの集計値だけ。`query_series`・trace は渡さない。除外の一覧を当てる |
| 質問文による prompt injection | ツールは読み取り専用で、答えるのは許可リストの人だけ。返す文から通知の記法とリンクを消す。最悪でも許可した人に coldaisle の集計値が見えるだけ |
| LLM の答えが数値を作りうる | 数値の検査で未検証の印を付ける。ダッシュボードが正 |
| D-06 の「会話は Workspace」に例外ができる | Slack のメンションへの返答に限る。Web のチャット UI は作らない |

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| PAW（Workspace）の側に Slack ボットを作る | D-06 に沿うが、所有者が coldaisle に作ることを選んだ（2026-10-23）。既存の Slack のトークンと LLM の接続をそのまま使える |
| Events API の HTTP の受け口 | 外から入る口が要り、D-10 を変える |
| tool loop を作らず、固定の集計（現在値と直近 24 h）を毎回渡して答えさせる | 「昨日の何時」「先週のアラート」に答えられない。固定の材料が毎回長くなる |
| `query_series` も渡す | 点の列は生の時系列に近い（ルール 8）。必要になれば別の記録で |
| Slack の SDK（`slack_sdk`）を足す | 使うのは `apps.connections.open` と WebSocket の受信と `chat.postMessage` だけで、依存を増やすほどではない |

## 5. 判断点（決定済み。推奨を先頭に）

**所有者の決定（2026-10-23）**: 1〜8 のすべてを推奨の案で決めた（8 は Codex の指摘を受けて同日に足した）。所有者の用途は、メンションで使用率と異常（アラート）を確かめること。

| # | 内容 | 選択肢（推奨を先に） |
|---|---|---|
| 1 | D-06 を一部置き換えて coldaisle に作る | **(推奨) Slack のメンションへの返答に限って置き換える**（所有者は coldaisle に作ることを選んだ）/ 置き換えない（PAW に作る） |
| 2 | 渡すツール | **(推奨) `get_latest` / `get_stats` / `list_alerts` / `describe_system` の 4 つ** / `query_series` も含めて 5 つ / `get_latest` だけ |
| 3 | tool loop の往復の上限 | **(推奨) 4** / 2 / 8 |
| 4 | 回数の上限 | **(推奨) この経路だけで 1 日 100 回・1 ユーザー 1 分 3 質問** / 既存の `daily_call_limit`（200）を共有する |
| 5 | ツールの結果に無い数値 | **(推奨) 答えに「未検証」の印を付けて返す** / 答えを捨てて「答えを作れませんでした」と返す / 検査しない |
| 6 | 許可リスト | **(推奨) チャンネルとユーザーの両方の ID を env に置き、両方に当たるときだけ答える** / チャンネルだけ / ユーザーだけ |
| 7 | LLM が使えないとき | **(推奨) 固定の文だけを返す** / 返事をしない / `get_latest` の値を整形して返す（LLM を通さない） |
| 8 | 最大値・最小値の時刻を聞く質問（PR #375 の Codex の指摘） | **(推奨) `get_stats` に期間の指定と最大・最小の時刻を足す** / `query_series` を点の数を絞って許す / 答えられないままにする |
