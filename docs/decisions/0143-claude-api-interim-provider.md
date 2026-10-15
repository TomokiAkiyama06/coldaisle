# 決定記録 0143: ローカル LLM が入るまでの間だけ Claude API を LLM の Provider として使う

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-15
- **Supersedes**: [`0005-model-selection.md`](0005-model-selection.md) の「ローカルモデルだけを使う」前提の一部
  （ローカル LLM が導入先に入るまでの間に限る。§2.1）と、
  [`0014-llm-provider.md`](0014-llm-provider.md) §2.1 の「接続先は OpenAI 互換だけ」の部分（Provider の選び方を足す。§2.2）。
  0005 のモデルの選定（Qwen3.8-27B 単体）と 0014 のそれ以外の規約（不達は結果・`None` を返さない・
  粘らない・`.env` を自動で読まない）は変えない。
  [`0016`](0016-evidence-alerts.md) は置き換えない（§2.11 で説明の材料と見立てを足す。Provider に依らない）
- **関連**: [`0005`](0005-model-selection.md) / [`0014`](0014-llm-provider.md) /
  [`0015`](0015-llm-tools.md) / [`0016`](0016-evidence-alerts.md) / [`0017`](0017-daily-report.md) /
  [`0018`](0018-tool-exposure.md) / [`0019`](0019-claude-escalation.md) /
  [`0013`](0013-notifications.md) §2.1 / [`0131`](0131-gpu-fan-service-alert.md) §2.4a /
  [`0138`](0138-gpu-fan-speed-llm-exclusion.md) / [`0040`](0040-server-health-api.md) /
  `docs/requirements.md` FR-501 / FR-504 / FR-505 / FR-506 / FR-507 / FR-508 /
  AGENTS.md ルール 1 / 8 / 9 / 10 / #58 / #305 / #306
- **対象 Issue**: #324

---

## 1. Context

導入先にローカル LLM（vLLM。#58）がまだ無い。`OPENAI_BASE_URL` / `MODEL_NAME` が未設定なので、
Provider は `UnavailableProvider` になり、次の 3 つが動いていない。

| 経路 | 入口 | 今の状態 |
|---|---|---|
| 日次レポートの所見（FR-506 / 0017） | `report.py` → `ai.summary.summarise` | 表だけが届く |
| アラートの自動の説明（FR-505 / FR-508 / 0016） | `daemon.py` の説明スレッド → `ai.explain.Explainer` | 素のアラートだけが届く |
| Server Health の任意要約（0040） | `server.py` → `ai.health.AiHealthSummarizer`（**`coldaisle.server:app` で起動したときだけ**。導入先の `coldaisle-api.service` は `coldaisle.api:app` で起動しており、この経路は作られない。§5 の 5） | 固定のテンプレートだけ |

所有者は 2026-10-15 に、ローカル LLM が入るまでの代わりとして Claude API の鍵を使ってよいと判断し、
導入先の `/etc/coldaisle/coldaisle.env` に鍵を登録した（#324）。

0005 は「ローカルモデル（Qwen3.8-27B 単体）」を前提にしている。0019 は「coldaisle は Claude へ送信しない」と決めている。
本記録は、その 2 つとどう両立させるかを決める。

---

## 2. Decision

### 2.1 期間: **ローカル LLM が導入先で動くまでの間だけ**

Claude API は代わりであり、置き換えではない。0005 のモデルの選定は変えない。
ローカル LLM（#58）が導入先で動いたら §2.9 の手順で戻す。

### 2.2 Provider の選び方: 環境変数 `COLDAISLE_LLM_PROVIDER`

| 値 | 使う Provider | 読む環境変数 |
|---|---|---|
| 未設定・空・`openai_compatible` | `OpenAiCompatibleProvider`（今のまま） | `OPENAI_BASE_URL` / `MODEL_NAME` / `OPENAI_API_KEY` |
| `anthropic` | `AnthropicProvider`（本記録で足す） | `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` |
| それ以外 | `UnavailableProvider`（エラーをログに出す。監視は止めない） | — |

- **既定は今の挙動を変えない。** 鍵が `coldaisle.env` にあるだけでは外へ送らない。
  外へ送るのは `COLDAISLE_LLM_PROVIDER=anthropic` と明示したときだけ（明示の opt-in）
- **選択を `config/ai.yaml` に置かない。** `config/` は `/opt/coldaisle` の git 管理下で root の所有
  （`docs/ubuntu-deploy.md` §2）であり、導入先で書き換えると clone との差分になる。
  0014 §2.1 の「接続先は環境変数だけ・書く場所を 2 つにしない」とも揃う。
  `config/ai.yaml` には上限など**接続先ではない値**だけを置く（§2.5）
- **モデル ID はコードに書かない。** `ANTHROPIC_MODEL` が無ければ `UnavailableProvider`（既定のモデルをコードに持たない）。
  推奨値は **`claude-haiku-5-5`**（`.env.example` と `docs/llm-setup.md` に書く）。理由:
  入力は集計済みの短い文（数百〜数千 token）で、出力は 2〜3 文の所見か 6 項目以内の JSON、または固定の候補からの選択であり、
  出力はコードの検査（表に無い数値・確度の表現・体裁を捨てる。0016 / 0017）を通る。
  単価は Sonnet 5.5（`claude-sonnet-5-5`）の 1/20（入力 $0.10 / 出力 $0.50 per 1M token。2026-10 時点）。
  所見の質が足りないと所有者が判断したら `claude-sonnet-5-5` へ変える（環境変数だけで済む）
- 接続先 URL は Anthropic の公開の endpoint（`https://api.anthropic.com/v1/messages`）の定数とし、
  上書きの環境変数は設けない（使う場面が無い。必要になったら別の記録で足す）

### 2.3 外へ送るもの: **今の Provider に渡しているものと同じ集計値だけ**

Provider は呼び出し側が組み立てた `ChatMessage` の列をそのまま送るだけで、**送る内容を広げない。**

| 経路 | 送るもの（今と同じ） |
|---|---|
| 日次レポートの所見 | `facts_markdown()` の文（日ごとの集計の表と件数。0017 §2.2。0131 §2.4a / 0138 の除外を当てた後） |
| アラートの説明 | アラートの rule_id・重大度・対象と、コードが組み立てた Evidence の行（閾値判定・品質・直近 24 時間の統計。0016）と、負荷と温度の直近の窓の集計（§2.11）。Evidence を集めるツールにも除外を当てる（0131 §2.4a） |
| Server Health の任意要約（`coldaisle.server:app` のときだけ） | 固定のテンプレートの文 1 つ（0040） |

- 生の時系列は送らない（ルール 8 / FR-504）。今の経路がそもそも渡していない
- 0131 §2.4a / 0138 の除外（`config/report.yaml` の `llm_excluded_metrics` / `llm_excluded_rules`）はそのまま当たる。
  除外の一覧が読めなければ、今と同じく LLM を呼ばない
- **decision trace（`/api/v1/control/*`）は送らない**（0071。今も LLM へ渡す経路が無い）
- **ツールは送らない。** `AnthropicProvider.chat()` は `tools` が渡されたら呼ばずに「利用不可」を返す。
  今の 3 経路はどれもツールを渡していない。ツールの結果を外へ流す経路を、この記録の範囲で開かない
  （AI ツールの窓口 `/api/v1/tools`（0018）は Workspace が呼ぶ GET であり、本記録とは関係しない）
- **0019 の案件資料は送らない。** `coldaisle-escalate` は Provider を使わない（0019 §2.1 のまま）
- **前提: 0138 の実装（#320）が `main` に入るまで、導入先で `anthropic` を選ばない。**
  入る前は既存の `gpu.*.fan_speed` が LLM の入力に残っており（0138 §1）、外へ出ることになるため

### 2.4 鍵: 環境変数 `ANTHROPIC_API_KEY` だけ。ログ・例外の文・通知に出さない

- 置き場所は導入先の `/etc/coldaisle/coldaisle.env`（`0640 root:coldaisle`。`docs/ubuntu-deploy.md` §2）。
  リポジトリ・`config/`・コードに書かない（0013 §2.1 / ルール 10）
- 鍵は HTTP のヘッダ（`x-api-key`）にだけ載せる。**ログ・`ChatResult.reason`・例外の文・通知に出さない。**
  失敗の理由は「HTTP の状態コード」「応答の `error.type`」「例外の型の名前」だけにし、
  httpx の例外の文（URL やヘッダを含みうる）も応答の本文の `message` も載せない（#305 の `DeliveryError` と同じ考え方）
- `ChatResult.reason` は API の応答（`available: false` のとき UI に出る）にも載るため、ここも同じ規則で作る
- **任意の環境変数 `ANTHROPIC_WORKSPACE_ID`。** ワークスペースに属さない鍵では API が `anthropic-workspace-id`
  ヘッダを求め、無ければ 400 を返す（導入先の疎通で確認。所有者は鍵を作り直さず ID を足すと判断した）。
  値があればそのヘッダで送る。空文字は未設定と同じ（送らない）。値の形は起動時（Provider を作るとき）に検査し、
  空白を含めば Claude API を使わない（`UnavailableProvider`。監視は止めない）。秘匿の値ではないが、
  **鍵と同じくログ・`reason`・例外の文・repr に値を出さない**（扱いを揃え、どれを出してよいかを迷わせない）

### 2.5 費用と回数の上限（`config/ai.yaml` の `anthropic:`）

| 設定 | 推奨の値 | 意味 |
|---|---|---|
| `max_tokens` | `1024` | 1 回の応答の上限。今の `max_tokens` と同じ値。超える値は送らない |
| `daily_call_limit` | `200` | **1 プロセス・1 日（導入先のローカル時刻の日付）あたりの HTTP 要求の回数**。再試行も 1 回と数える |
| `effort` | 無し（`null`） | `output_config.effort`。**無い・`null` なら送らない**（モデルの既定のまま）。対応するモデルかどうかをコードで判定しないので、対応を確かめたモデルでだけ `low` などを設定する |

- 上限に達したら**呼ばずに**「利用不可」を返す。呼び出し側は今と同じくテンプレート（表だけ・素のアラート・固定の文）を出す
- 上限に達したことは、その日に 1 回だけ warning のログに出す（毎回出すと本物の障害が埋もれる）
- 数えるのはプロセスの中のメモリ（daemon・report・`coldaisle.server:app` で起動した API のそれぞれ）。再起動で 0 に戻る。
  上限は費用の暴走を止めるための粗い蓋であり、課金の正本は Anthropic の Console の使用量と、
  所有者が Console で設定する月の上限（spend limit）である（§5 の 1）
- 費用の見積もり（`claude-haiku-5-5`、1 回あたり入力 3,000 token・出力 1,024 token の最悪の場合）:
  約 $0.0008 / 回。3 プロセスが毎日上限まで呼んでも 600 回 × $0.0008 ≒ **$0.5 / 日**。
  実際は日次レポート 1 回 + アラートの件数（+ `coldaisle.server:app` のときの Health の文の変化の回数）で、桁が 1〜2 小さい
- `temperature` は送らない（Haiku 5.5 / Sonnet 5.5 は既定以外の値を 400 で拒む）。
  `thinking` も送らない（モデルの既定のまま。深さを抑えたいときは `effort` を設定する）

### 2.6 不達・上限超過・鍵が無いときも監視と通知は動き続ける（FR-507）

0014 §2.2 の規約をそのまま当てる。

- `chat()` は例外を投げない。接続の失敗・タイムアウト・HTTP のエラー・応答の形の違い・上限超過・`tools` の指定は
  すべて `ChatResult(available=False, reason=...)` で返す
- `ANTHROPIC_API_KEY` か `ANTHROPIC_MODEL` が無ければ `UnavailableProvider`（作って毎回失敗させない）
- 再試行は今の `max_attempts` / `retry_backoff_s` に従い、**429・5xx・接続の失敗だけ**。他の 4xx は再試行しない
- `stop_reason` が `refusal`・`max_tokens`（途中で切れた）・`model_context_window_exceeded`（文脈の長さで切れた）なら「利用不可」として捨てる
  （切れた所見や JSON を採用しない）
- 説明は付かないだけで、アラートは今と同じ時刻に今と同じ宛先へ届く（説明は後追い。0016）

### 2.7 出力は日本語

3 つの経路の system prompt は既に日本語で、日本語で書くよう指示している（`ai/summary.py` / `ai/explain.py` /
`ai/health.py`）。Provider ごとにプロンプトを変えない。出力の検査（表に無い数値・確度の表現・体裁・許可済みの文との完全一致）も
Provider に依らず同じものを通る。

### 2.8 0019（Claude へのエスカレーション）との違い

| | 0019 の案件資料 | 本記録 |
|---|---|---|
| 目的 | 初見の異常・故障の疑いを Claude に**判断**してもらう | ローカル LLM の代わりに定型の**文**を作る |
| 送り手 | **人**（coldaisle は送信経路を持たない） | coldaisle の Provider（`COLDAISLE_LLM_PROVIDER=anthropic` のときだけ） |
| 内容 | 室温・機材構成・障害の経過（5 節の資料） | §2.3 の集計値だけ |
| 判断の重さ | 故障・RMA の判断の材料 | 所見と確認手順の提案（Evidence 形式。判断はしない） |

0019 §2.1 の「coldaisle は送信しない」は**案件資料について**の決定であり、本記録でも変えない。
`coldaisle-escalate` は Provider を import せず、案件資料を外へ送る経路は引き続き無い。

### 2.9 ローカル LLM が入ったら戻す手順

1. 導入先で vLLM を動かし、`docs/llm-setup.md` のスモークテストを通す
2. `/etc/coldaisle/coldaisle.env` で `COLDAISLE_LLM_PROVIDER` を消すか `openai_compatible` にし、
   `OPENAI_BASE_URL` / `MODEL_NAME` を書く
3. `ANTHROPIC_API_KEY`（と `ANTHROPIC_WORKSPACE_ID`）を消し、Anthropic の Console で鍵を無効にする（使わない鍵を置いたままにしない）
4. `coldaisle-daemon` と `coldaisle-api` を再起動する（`coldaisle-report` は次の起動から効く）
5. 本記録の Status を `Superseded` にする記録を書くか、§5 の 2 に従って `AnthropicProvider` を残すかを決める

### 2.10 実装: httpx で Messages API を直接呼ぶ（SDK を足さない）

- 使う API の面は `POST /v1/messages` の 1 つだけで、送るのは `model` / `max_tokens` / `system` / `messages` /
  （設定したときだけ）`output_config.effort`、読むのは `content` の `text` の塊と `stop_reason` だけである
- 公式の Python SDK（`anthropic`）を足すと、1.x は `httpx2` を含む別の依存の木を連れてくる。
  本プロジェクトは既に `httpx` を使い、OpenAI 互換の Provider も SDK を使わず httpx で直接呼んでいる。
  SDK の自前の再試行（既定 2 回）と待ち時間（既定 10 分）は 0014 §2.5 の「粘らない」と別に止める必要もある
- 期間を限った代わり（§2.1）のために依存を増やさない。テストは既存の Provider と同じく `httpx.MockTransport` で
  HTTP を偽物にし、実 API を呼ばない
- LLM の層に書き込み・実行の権限を足さない（ルール 1）。Provider は読み取り専用の文を返すだけである

### 2.11 アラートの説明に負荷と温度の集計と「見立て」を足す（Provider に依らない。0016 を補う）

所有者の要望（2026-10-15）: 説明（🔎）が、使用率・負荷からも原因を見立てられるようにする。
**この項は Provider に依らない**（ローカル LLM でも Claude でも同じ材料・同じ検査で効く）。
0016 の規約（VERIFIED はコードが作る・確度は丸ごと捨てる・体裁を騙らせない・通知をブロックしない）は変えずに、
材料と出力の欄を 1 つずつ足す。0016 は FINAL なので書き換えず、ここに置く。

- **材料（VERIFIED の L1 の行を足す）:** `config/ai.yaml` の `explain_context` に書いたメトリクスごとに、
  直近の窓（`window`。出荷は `15m`。説明は発火の直後に作るので発火の前後を見る）の
  **平均・最大・傾き（/分）と件数だけ**を、コードが `get_stats` で組み立てて 1 行にする。生の時系列は渡さない（ルール 8）。
  出荷の一覧は `gpu.0.utilization`・`power.gpu.0`・`gpu.0.core`・`cpu.utilization`・`power.cpu.package`・
  `cpu.package`・室温（`air.room`）と吸排気（`air.front_intake`・`air.gpu_intake`・`air.gpu_exhaust`・
  `air.top_exhaust`・`air.rear_exhaust`）。16 個まで。`explain_context` が無ければ今のまま足さない
- **除外は必ず当たる:** 集計は除外を当てたツールを通すので、0131 §2.4a / 0138 の一覧に当たるメトリクス
  （`gpu.*.fan_speed`・`gpu.*.fan_policy` など）は設定に書いても行にならない。出荷の一覧が除外に当たらないことは
  CI のテストで見張る（0138 の一覧を足した形でも確かめる）
- **出力（見立て）:** モデルに `assessment` を足して書かせる。指示は「負荷（使用率・電力）の変化と温度の関係から、
  空調・筐体内の回り込み・Fan の不足・負荷の増加のどれが近いかを見立て、根拠の値を挙げる。日本語の 3〜4 文」。
  通知では「見立て（L3 推測・未検証）」の節に `? ` を付けて出す（UNVERIFIED の後・確認手順の前）
- **見立ての検査:** UNVERIFIED と同じく、確度の表現・VERIFIED の体裁があれば応答を丸ごと捨てる。改行は潰し、
  400 文字で切る。加えて、**事実（プロンプトに渡した行）に無い数値を書いた見立ては応答を丸ごと捨てる**
  （日次レポートの所見と同じ考え方。0017。`92` と `92.00` は同じ値とみなす）。`assessment` の無い応答は今のまま使う
- 送る量はメトリクスの数ぶんの短い行が増えるだけで、§2.5 の上限（`max_tokens`・1 日の回数）はそのまま効く

---

## 3. Consequences

### 良くなること

- ローカル LLM が無くても、日次レポートの所見とアラートの説明が付く
- 既定は今のまま。`COLDAISLE_LLM_PROVIDER` を消すだけで外への送信が止まる
- 送る内容は今と同じ集計値で、除外（0131 / 0138）・出力の検査（0016 / 0017）も同じものを通る

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 監視ホストから外へ出る経路と鍵が 1 つ増える（0019 §2.1 が避けた点） | 明示の opt-in（§2.2）・送るのは集計値だけ（§2.3）・鍵を出さない（§2.4）・期間を限る（§2.1）・戻す手順（§2.9） |
| 集計値（室温・GPU 温度の日ごとの統計、アラートの名前）が Anthropic に届く | 機材の識別子・IP・ホスト名は今の入力に入っていない。所有者が 2026-10-15 にこの範囲で送ることを判断した（#324） |
| 費用がかかる | §2.5 の上限と推奨のモデル。正本は Console の spend limit |
| 回数の上限がプロセスごと・再起動で 0 に戻る | 粗い蓋と割り切る。daemon の再起動が繰り返されるときは別の問題として通知に出る。正本は Console |
| Anthropic の障害・鍵の失効で説明が付かない | FR-507 のとおり説明が付かないだけ。理由は `reason` とログに状態コードで残る |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| Provider の選択を `config/ai.yaml` の `provider:` に置く | `config/` は git 管理下で root の所有。導入先で書き換えると clone との差分になる。0014 §2.1 の「書く場所を 2 つにしない」にも反する（§2.2） |
| `ANTHROPIC_API_KEY` があれば自動で Claude を使う | 鍵を置いただけで外へ送り始める。送る判断を明示の設定にしたい（§2.2） |
| 既定のモデル ID をコードに持つ | モデルの世代が替わるたびにコードを直すことになる。ルール 9 |
| 公式 SDK（`anthropic`）を足す | 依存の木が増え、自前の再試行・待ち時間を別に止める必要がある。使う面は 1 つだけ（§2.10） |
| OpenAI 互換の endpoint 経由で Claude を呼ぶ（既存の Provider を流用） | 互換の層の振る舞い（`chat_template_kwargs` の扱いなど）が別の確認事項になる。Messages API を直接呼ぶほうが確かめる点が少ない |
| ツール（0015）も Claude に渡す | ツールの結果が外へ出る経路が増える。今の 3 経路はツールを使わない（§2.3） |
| 上限を DB やファイルに書いてプロセス間で共有する | AI の層に書き込みの経路が増える。費用の正本は Console にある（§2.5） |
| 案件資料（0019）も自動で Claude へ送る | 外に出す判断は人のもの（0019 §2.1）。本記録の目的（定型の文）と違う |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | Anthropic の Console での月の上限（spend limit）の値 | 所有者（導入先で有効にする前） |
| 2 | ローカル LLM が入った後、`AnthropicProvider` をコードに残すか消すか | ローカル LLM の導入（#58）の後 |
| 3 | `daily_call_limit` / `max_tokens` / `effort` の値の見直し | 有効にして 1〜2 週間の使用量を Console で見た後 |
| 4 | 導入先の `coldaisle.env` の鍵の変数名が `ANTHROPIC_API_KEY` と一致するか | 導入先で有効にするとき（#324 の PR の手順） |
| 5 | 導入先の API（`coldaisle-api.service`）を `coldaisle.api:app` から `coldaisle.server:app` へ切り替えるか（切り替えると Server Health の任意要約と AI ツールの窓口が開く。unit の変更は本記録の範囲外） | 別の Issue |
| 6 | モデル ID（推奨の `claude-haiku-5-5`）が鍵で使えるか | 導入先で有効にするとき（`docs/llm-setup.md` の疎通確認） |
