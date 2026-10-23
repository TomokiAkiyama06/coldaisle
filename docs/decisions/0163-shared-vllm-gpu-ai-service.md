# 決定記録 0163: GPU AI Service は Personal AI Workspace の vLLM を共用し、Docker Compose の3層分離は行わない

- **種別**: Decision Record
- **Status**: FINAL（2026-10-21、リポジトリ所有者の承認。§5 の 1〜10 はすべて推奨の案）
- **Date**: 2026-10-21
- **Supersedes**: なし（下の記録を置き換えず、所有者の 2026-10-21 の決定を具体にして補う）
  - [0143](0143-claude-api-interim-provider.md) は置き換えない。§2.5 で 0143 §2.9 の戻す手順の値を決め、
    0143 §5 の 2 に答える（コードを残す。§5 の 1）。0143 は FINAL のまま（0143 §2.9 の 5）
  - [0002](0002-metric-naming.md) §2.6 / [0006](0006-gpu-mode-and-mixed-state.md) §2.2 の `sys.gpu_state` の5値と意味は変えない。
    §2.6 は 0002 §2.6 が `config/gpu_state.yaml` に置くとした「プロセスの識別規則」の形を決める
- **Superseded by**: [0166](0166-nfr04-request-both-call-paths.md)（§2.4 の表の「要求」の行のみ＝日次レポートの所見とアラートの説明の両方の実際の要求を経路ごとに測る。§2.4 のほかの行と他の節は有効）
- **関連**: [0001](0001-initial-project-decisions.md) D-06 / D-14 / [0005](0005-model-selection.md) /
  [0014](0014-llm-provider.md) §2.1 / §2.3 / §2.4 / §2.5 / [0019](0019-claude-escalation.md) /
  [0028](0028-fan-control-contracts.md) §5 の 7 / [0040](0040-server-health-api.md) §2.4 / §2.7 /
  [0045](0045-local-socket-write-entry.md) / [0069](0069-ubuntu-deploy-templates.md) /
  [0080](0080-fand-systemd-unit.md) §5 の 13 / [0117](0117-telemetry-unit.md) / [0128](0128-gpu-fan-curve-service.md) /
  `docs/requirements.md` D-06（Core / GPU AI の分離）/ D-07 / §7.2 / FR-501 / FR-502 / FR-504 / FR-507 / NFR-04 /
  `docs/llm-setup.md` / `config/ai.yaml` /
  `src/coldaisle/ai/provider.py` / `src/coldaisle/ai/explain.py` / `src/coldaisle/ai/health.py` /
  `src/coldaisle/internal_telemetry/nvml.py` / AGENTS.md「絶対に守るルール」1・8・9・10
- **対象 Issue**: #58 / #63 / #64

---

## 1. Context

### 1.1 所有者の決定（2026-10-21）

1. **#58**: coldaisle 専用の vLLM は立てない。同じ機械で別プロジェクト Personal AI Workspace（以下 PAW。
   `docs/requirements.md` の「Workspace」と同じもの）が動かしている vLLM を共用する。coldaisle は OpenAI 互換の窓口として
   読むだけで、起動・停止は PAW の Compute Resource Scheduler に任せる。止まっていればテンプレートへ縮退する（今の挙動）
2. **#64**: Docker Compose の3層分離は Rejected。Core はすでに systemd で本番稼働している
   （#57 / 0069 / 0080 / 0117）。fand と gpu-fan はホストで直接動かす（0080 / 0128）。
   0028 §5 の 7 と 0080 §5 の 13 もこの記録で閉じる

本記録は、この 2 つを実装と導入が参照できる具体性で書き、あわせて #63（Core と GPU AI の分離）の残り
（`sys.gpu_state` の識別規則と Server Health の `ai_layer`）の形を決める。

### 1.2 調べた事実

**PAW の vLLM**（PAW の unit。値は 2026-10-21 に確かめた）

| 項目 | 値 |
|---|---|
| 起動 | `vllm serve`。unit は `enable` されておらず、PAW の Scheduler が `systemctl start` / `stop` する |
| モデル | Qwen3.8-27B-FP8（0005 の「27B FP8 単体」と同じ選定） |
| `--served-model-name` | `main` |
| 待ち受け | `127.0.0.1:18080` |
| `--gpu-memory-utilization` | `0.51` |
| `--max-model-len` | `131072` |
| 思考 | `--reasoning-parser qwen3` |
| ツール | `--enable-auto-tool-choice --tool-call-parser qwen3_coder` |

**coldaisle の文書との食い違い**

- `docs/requirements.md` §7.2 と `docs/llm-setup.md` は、coldaisle が自分で `vllm serve Qwen/Qwen3.8-27B` を
  `--tool-call-parser hermes` / `--max-model-len 32768` / `--gpu-memory-utilization 0.4` / `--port 8001` で立てる前提で書かれている。
  `config/ai.yaml` の注釈の例も `http://127.0.0.1:8001/v1`
- 同じ機械では Ollama も GPU を使いうる

**coldaisle の Provider の実装**（`src/coldaisle/ai/`）

- `OpenAiCompatibleProvider` は `POST {OPENAI_BASE_URL}/chat/completions` を `stream: false` で呼び、
  `choices[0].message.content` を読んで `<think>` を切り離し（0014 §2.4）、`message.tool_calls` を解釈せずに
  リストのまま `ChatResult.tool_calls` へ渡す。`reasoning_content` / `reasoning` の欄は読まない
- coldaisle 自身の呼び出しは 4 つで、**どれも `tools` を送らない**。すべて `thinking=False` で呼ぶ
  （`thinking=True` で呼ぶ所は無く、`ChatResult.thinking` を読む所も無い）

  | 呼び出し | 入口 | ツール |
  |---|---|---|
  | 日次レポートの所見 | `ai/summary.py` | 使わない |
  | アラートの説明 | `ai/explain.py` | `get_latest` / `get_stats` を **coldaisle が決定論的に** `ToolRegistry.call` で呼び、結果を集計して文に入れる。モデルにツールを選ばせない |
  | Server Health の要約 | `ai/health.py` | 使わない（閉じた候補から選ぶだけ。0040 §2.7） |
  | 疎通確認 | `Provider.probe()` | 使わない |

- モデルにツールを選ばせる会話（FR-502）は Workspace 側のチャットが持つ。coldaisle はその窓口
  `GET /api/v1/tools/{name}`（読み取り専用。`coldaisle.server:app` で起動したときだけ開く）を出すだけで、
  ツールの呼び出しの往復（tool loop）を持たない。`ToolRegistry.call` は引数を辞書でも JSON の文字列でも受ける

**本番の API と `ai_layer`**

- 導入先の `coldaisle-api.service` は `coldaisle.api:app` で起動しており、Server Health の AI 要約の経路は作られない。
  `sources.ai_layer.status` は常に `stopped`（`docs/llm-setup.md`、0143 §5 の 5）
- `ai_layer` は 0040 §2.7 で「要約が作れたら `ok`、作れなければ `stopped`」と決まっており、signal の判定から外れている（0040 §2.4）
- 0143 は Claude API を暫定の Provider にし、#58 が済んだら 0143 §2.9 の手順で戻すとしている。
  0143 §5 の 2（`AnthropicProvider` を残すか）は #58 の後に決めるとしている

**#63 の残り**

- #63 の受入基準は「GPU AI Service 停止中も監視 / API / 通知が正常」「Compute Mode 中も温度欠測なし」
  「AI 由来 CUDA process = 0 を確認可能」
- `sys.gpu_state`（`idle` / `ai` / `compute` / `mixed` / `unknown`。0002 §2.6 / 0006 §2.2・§2.3）は未実装。
  0002 §2.6 は識別規則を `config/gpu_state.yaml` に置くとしたが、ファイルが無く、規則の中身（プロセス名か、unit か）も決まっていない
- 今あるのは `sys.cuda_processes`（`internal_telemetry/nvml.py` が `nvmlDeviceGetComputeRunningProcesses` の PID の数を出す）だけ
- `coldaisle-telemetry` の unit（0117）は `ProtectProc=` を付けておらず、他のプロセスの `/proc/<pid>/cgroup` を読める

### 1.3 なぜ今決めるか

0143 の Claude API は「ローカル LLM が入るまでの間だけ」の代わりで、外へ出る経路と鍵を置いたままにしている。
共用する vLLM はすでに動いているので、向け先・戻し方・`sys.gpu_state` の規則を決めれば、文書と実装の PR へ進める。

---

## 2. Decision

### 2.1 向け先: PAW の vLLM を OpenAI 互換の窓口として読む

導入先の `/etc/coldaisle/coldaisle.env`（git の外）に次を書く。**`config/ai.yaml` には書かない**（0014 §2.1。ルール 9 の
「設定は環境変数 / config へ」はこれで満たす）。

| 環境変数 | 値 | 理由 |
|---|---|---|
| `COLDAISLE_LLM_PROVIDER` | 書かない（または `openai_compatible`） | 0143 §2.2 の既定の経路 |
| `OPENAI_BASE_URL` | `http://127.0.0.1:18080/v1` | PAW の vLLM の待ち受け。ループバックだけ |
| `MODEL_NAME` | `main` | PAW の `--served-model-name`。モデルの実体の名前ではなく別名を使う（PAW がモデルを差し替えても coldaisle の設定を変えずに済む） |
| `OPENAI_API_KEY` | 書かない | PAW の vLLM は `--api-key` を付けていない。PAW が鍵を付けたら、そのときに書く |

- ポートと `main` は **PAW が決める窓口**である。PAW が変えると coldaisle は「利用不可」になるだけで、監視は止まらない（FR-507）。
  変えたことに気づく仕組みは置かず、導入時と PAW の変更時のスモークテスト（§2.2 の表）で確かめる
- `main` が指す実体（Qwen3.8-27B-FP8）は、スモークテストで `GET /v1/models` の `root` を見て記録する。実行時には確かめない（§5 の 8）
- 共用によって coldaisle が PAW の vLLM へ渡すものは増えない。送るのは今と同じ集計値だけで（ルール 8 / FR-504）、
  生の時系列・decision trace・ツールの結果は送らない
- 返ってくる文は信頼しない入力として扱う（今のまま）。LLM の層に書き込み・実行・アクチュエーションの権限を足さない（ルール 1）。
  Server Health の要約は閉じた候補から選ぶだけ（0040 §2.7）、アラートの説明は Evidence 形式（FR-508）

### 2.2 tool-call parser と reasoning の前提: **今の実装のまま受けられる。コードは変えない**

**tool-call parser（`qwen3_coder`）**

- coldaisle 自身の呼び出しは `tools` を送らない（§1.2）。`--enable-auto-tool-choice` と parser が効くのは要求に `tools` があるときだけなので、
  **coldaisle の経路では parser の種類が結果に効かない**。`docs/requirements.md` §7.2 の `hermes` の前提は不要になる
- それでも将来 `tools` を送ったときに受けられる形かを確かめた。vLLM の parser は、モデルの出力を OpenAI の形
  （`tool_calls[].function.name` と、JSON の文字列の `tool_calls[].function.arguments`）に直して返す。
  Provider はそれを解釈せずにリストで渡し、`ToolRegistry.call` は JSON の文字列の引数を受ける。形は合う
- ただし coldaisle は tool loop を持たず、本記録でも足さない。ツールを使う会話は Workspace のチャットが持つ（FR-502 / 0001 D-06）

**reasoning（`--reasoning-parser qwen3`）**

- reasoning parser は思考を `message.content` から外し、別の欄（vLLM の版により `reasoning_content` または `reasoning`）へ移す。
  Provider は `content` だけを読むので、**本文に思考は混ざらない**。`ChatResult.thinking` は空になるが、読む所は無い（§1.2）
- `split_thinking`（0014 §2.4）は残す。parser を外した窓口へ向けたときの安全網である
- 思考の指示は今のまま `send_thinking_flag: true` で `chat_template_kwargs.enable_thinking=false` を送る（0014 §2.3）。
  coldaisle の呼び出しはすべて非思考で、思考を使うと初回トークンが遅れる（§2.4）
- `thinking=True` の呼び出しは今は無い。足すときは「思考の途中で `max_tokens` に達すると `content` が空で返る」ことを、
  その PR で「利用不可」として扱う（空の本文を所見として出さない）。本記録では足さない

**導入時と PAW の変更時のスモークテスト**（文書の PR で `docs/llm-setup.md` に書く）

| 確かめること | 期待 |
|---|---|
| `GET /v1/models` | `main` があり、`root` が Qwen3.8-27B-FP8 を指す |
| `Provider.probe()`（`coldaisle` の uid で。**Provider を明示し、導入先の env を読ませない**。下の「試すときの Provider の指定」） | `利用可`、かつ `ChatResult.model` が `main`（Claude のモデル ID なら Claude を試している） |
| `enable_thinking=false` を送った要求 | 400 にならず、`content` に `<think>` が無い。400 なら `send_thinking_flag: false` にする（0014 §2.3） |
| `coldaisle-report --no-send --print`（probe と同じ形で Provider を明示する） | 「所見」の節が付き、journal に Claude の行が出ない |
| （参考）`tools` を付けた要求 1 回 | `tool_calls[].function.arguments` が JSON の文字列で返る |
| PAW の vLLM が動いているときの `sys.gpu_state`（§2.6 の実装の後） | `ai`（他の CUDA process があれば `mixed`）。`compute` なら `ai_units` が古い（§2.6 の陽性の対照） |

**試すときの Provider の指定**（PR #364 の指摘）

- `provider_from_env()` は**最初に `COLDAISLE_LLM_PROVIDER` を読み**、`anthropic` なら `OPENAI_BASE_URL` / `MODEL_NAME` を見ずに
  `AnthropicProvider` を返す（`src/coldaisle/ai/provider.py`）。0143 を有効にしたままの導入先の `/etc/coldaisle/coldaisle.env` には
  `COLDAISLE_LLM_PROVIDER=anthropic` があるので、その env を読ませたまま `OPENAI_BASE_URL` / `MODEL_NAME` だけを足すと、**PAW ではなく Claude を試して `利用可` になる**
- しかも systemd では `EnvironmentFile=` の値が `Environment=`（`systemd-run --setenv` / `-E`）を上書きする。env を読ませたまま `--setenv=COLDAISLE_LLM_PROVIDER=openai_compatible` を足しても効かない
- そこで切り替えの前の試験（probe・レポート・NFR-04 の測定）は、**`-p EnvironmentFile=/etc/coldaisle/coldaisle.env` を付けず**、
  使う値をすべて `--setenv` で渡す: `COLDAISLE_LLM_PROVIDER=openai_compatible`・`OPENAI_BASE_URL=http://127.0.0.1:18080/v1`・`MODEL_NAME=main`
  （`ANTHROPIC_*` は渡さない）。結果の `model` が `main` であることも出して確かめる
- 切り替えの後（§2.5 の手順 2〜4 の後）の確かめは env を読ませてよい。そのときも `model` が `main` であることを見る

### 2.3 起動・停止の所有者は PAW。coldaisle は起動しない・待たない

- coldaisle は PAW の vLLM を**起動も停止もしない**。`systemctl` も D-Bus も呼ばず、PAW の unit への `Wants=` / `Requires=` / `After=` も持たない。
  coldaisle の unit（`coldaisle-daemon` / `coldaisle-api` / `coldaisle-report` ほか）は PAW の unit が無くても今のまま動く
- **待たない。** 止まっていれば接続は拒否され、`max_attempts`（2）と `retry_backoff_s`（0.5 s）の後に「利用不可」として返る（0014 §2.5）。
  起きるまで待つ・起こしに行く・後で作り直す経路は足さない
- 止まっている間の挙動は今のまま: 日次レポートは表だけ、アラートは素の文だけ（説明は通知の経路を塞がない）、
  Server Health はテンプレート（`ai_layer: stopped`）。日次レポートのタイマーの時刻に vLLM が止まっていれば、その日の所見は付かない（§3）
- Compute Mode で VRAM を空けるのは PAW の Scheduler の責任である。coldaisle はそれを `sys.gpu_state`（§2.6）で**観測するだけ**で、
  外れていれば 0006 §2.4 の警告（#36）で知らせる
- **Core は GPU AI に依存しない**（FR-507 / 0001 D-14）。fand・gpu-fan・Learned worker は LLM を読まない・呼ばない（今のまま）
- PAW の Scheduler から GPU Mode の事象（`coldaisle-event gpu-mode`。0045）を送ってもらうことは本記録では求めない（§5 の 10）

### 2.4 NFR-04（初回トークン 2 s）の測り方

NFR-04 は「vLLM 環境で LLM の初回トークン 2 秒以内」。coldaisle の Provider は `stream: false` なので、測定は Provider を通さず、
同じ窓口へ同じ形の要求をストリーミングで送る一回限りの手順で行う（コードと CI に測定を足さない）。

| 項目 | 決め |
|---|---|
| 状態 | **warm**（PAW の vLLM が起動済みでモデルを読み終えている）。起動にかかる時間は coldaisle が待たないので NFR-04 の範囲の外 |
| 要求 | coldaisle の最も長い実際のプロンプト（`explain_context` を足したアラートの説明）と同じ長さ・`enable_thinking=false`・`max_tokens` 1024・`stream: true` |
| 計る区間 | 要求を送ってから、最初の `content` の差分が届くまで |
| 回数 | 20 回。間をあけて送る（vLLM の prefix cache で 2 回目以降だけ速くならないよう、プロンプトの先頭の時刻の行を毎回変える） |
| 合格 | PAW の他の要求が無いときに **20 回すべて 2 s 以内**（§5 の 6） |
| 参考 | PAW の要求と重なったときの値も記録する。合否には使わない（共用の窓口で、相手の負荷は coldaisle が決められない） |

結果は文書の PR か導入の記録に残す。2 s を超えたら、所有者が PAW 側の設定（`--max-num-seqs` など）を含めて扱いを決める（本記録では決めない）。

### 2.5 0143 から戻す手順と `AnthropicProvider`

0143 §2.9 の手順を、§2.1 の値で行う。**本記録の承認と §2.2 のスモークテストの後**に、所有者が導入先で行う。

1. PAW の vLLM が動いているときに §2.2 のスモークテストを通す。`/etc/coldaisle/coldaisle.env` を変える前なので、**env を読ませず
   Provider を `--setenv=COLDAISLE_LLM_PROVIDER=openai_compatible` で明示する**（§2.2「試すときの Provider の指定」）。
   `利用可` と `model` が `main` の両方が出るまで手順 2 へ進まない（Claude を試して通ったまま鍵を消さない）
2. `/etc/coldaisle/coldaisle.env` の `COLDAISLE_LLM_PROVIDER=anthropic` を `COLDAISLE_LLM_PROVIDER=openai_compatible` に書き換え
   （消してもよいが、明示のほうが読み違えない）、`OPENAI_BASE_URL=http://127.0.0.1:18080/v1` と `MODEL_NAME=main` を書く
3. `ANTHROPIC_API_KEY`（と `ANTHROPIC_WORKSPACE_ID`・`ANTHROPIC_MODEL`）を消し、Anthropic の Console で鍵を無効にする
4. `coldaisle-daemon` と `coldaisle-api` を再起動する（`coldaisle-report` は次のタイマーの起動から効く）
5. env を読ませた形で `probe()` の `model` が `main` であることと、前日のレポートを `--no-send --print` で作り所見が付くこと・journal に Claude の行が出ないことを確かめる

**`AnthropicProvider`（0143 §5 の 2）**: **コードを残す**（§5 の 1）。

- 外へ送るのは `COLDAISLE_LLM_PROVIDER=anthropic` と鍵の両方を置いたときだけで（0143 §2.2）、手順 2・3 で両方が導入先から消える
- PAW の vLLM は Compute Mode のたびに止まり、PAW の変更で窓口が変わりうる。長く使えない期間が出たとき、所有者が明示の opt-in で
  再び選べる余地を残す
- **vLLM が止まっているときに Claude API へ自動で切り替える経路は作らない**（§4）。止まっていればテンプレートへ縮退する（§2.3）
- 0143 の Status は FINAL のまま（期間は導入先の設定で終わる。0143 §2.9 の 5 の答え）

### 2.6 `sys.gpu_state` の識別規則（#63）: NVML の PID を cgroup の systemd unit で照合する

**収集する場所**: `coldaisle-telemetry`（すでに NVML を読み `sys.cuda_processes` を出している。0117）。
fand・Learned worker・API には足さない。

**手順**（周期ごと）

1. `nvmlDeviceGetComputeRunningProcesses`（今と同じ API。全 GPU）で PID の一覧を得る
2. 各 PID の `/proc/<pid>/cgroup` を読み、cgroup v2 の行（`0::<path>`）の path のうち **`.service` で終わる最も内側の要素**を unit 名とする
   - system の unit: `/system.slice/<unit>.service` → `<unit>.service`
   - user の unit: `/user.slice/user-<uid>.slice/user@<uid>.service/app.slice/<unit>.service` → `<unit>.service`（`user@<uid>.service` ではない）
   - vLLM の EngineCore・worker など子プロセスは親と同じ unit の cgroup に入るので、プロセス名に依らず同じ unit に属する
3. unit 名を `config/gpu_state.yaml` の `ai_units`（推論サーバの unit 名の完全一致の一覧）と照合する
4. 次の表で値を決め、変わったときだけ `system_state` に `sys.gpu_state` を書く（0002 §2.6）

| 観測 | 値 |
|---|---|
| NVML が使えない・API が失敗した | `unknown` |
| いずれかの PID の cgroup が読めない（`EACCES`・`hidepid`・cgroup v2 でない・形が読めない） | `unknown`（判定不能。最も保守的。§5 の 5） |
| PID が無い（読む間に消えた PID〔`ENOENT`〕は数えない） | `idle` |
| すべての PID の unit が `ai_units` に入る | `ai` |
| どの PID の unit も `ai_units` に入らない（`.service` の要素が無い PID、たとえば端末の `session-N.scope` やコンテナの `docker-<id>.scope` を含む） | `compute` |
| 両方がある | `mixed`（0006 §2.2 のとおり異常として警告する。#36） |

**`config/gpu_state.yaml` の形**（値は実装の PR で書く）

```yaml
# sys.gpu_state の識別規則（決定記録 0002 §2.6 / 0163 §2.6）
# NVML の compute process の PID を /proc/<pid>/cgroup の最も内側の .service と照合する
ai_units:            # 推論サーバの systemd unit 名（完全一致）。ここに無い unit・scope の CUDA process は compute
  - <PAW の vLLM の unit 名>.service
  - ollama.service
```

- 一覧に入れるのは unit 名だけで、PID・uid・ホスト名・パスは書かない（ルール 10）。置き場所は §5 の 4
- 形が不正（鍵が無い・空でない文字列の一覧でない・`.service` で終わらない）なら `coldaisle-telemetry` は `sys.gpu_state` を `unknown` にして収集を続ける
  （温度の収集を止めない。#63 の「Compute Mode 中も温度欠測なし」）
- #63 の「AI 由来 CUDA process = 0 を確認可能」の確かめ方（PR #364 の指摘）
  - **`idle` だけが、NVML に CUDA process が1つも無いことの証拠になる。** `compute` は「`ai_units` に入る unit の process が無い」ことしか言わない。
    推論サーバの unit が一覧から漏れていれば（PAW の unit の改名・新しい推論サーバ）、その process も `compute` に数えられ、0006 では `compute` は
    通常の値なので警告も出ない。つまり `compute` が「AI 由来 0」を意味するのは、**`ai_units` が推論サーバの unit をすべて含むという前提の下だけ**である
  - その前提は実行時には確かめられない（数のメトリクスは足さない。§5 の 9）。そこで導入時と PAW の変更時（unit 名・推論サーバの追加）のスモークテスト
    （§2.2 の表）で**陽性の対照**を取る: PAW の vLLM（と Ollama）を動かしているときに `sys.gpu_state` が `ai`（他の CUDA process があれば `mixed`）になり、
    `compute` にならないことを確かめる。`compute` になったら `ai_units` を直すまで、`compute` を「AI 由来 0」の証拠に使わない
  - #63 の受入の試験は、PAW の Scheduler が vLLM を止めた後に `idle` になること（他の CUDA の負荷が無いとき）と、上の陽性の対照の 2 つで行う
- `sys.gpu_state` は観測の記録だけで、fand・Learned・Critical Safety の入力にしない（0045 の GPU Mode と同じ扱い）。閾値の切り替え（#18）は別に決める
- GPU Mode（PAW が申告する指令値）からは決めない。指令と実態はずれる（0002 §2.6「用語の区別」）
- `/proc` を読むことは読み取りだけで、LLM の層に権限を足さない。LLM が `sys.gpu_state` を見るのは既存の読み取り専用のツールを通るときだけで、ツールは足さない（ルール 1）
- unit に `ProtectProc=invisible` などを後で足すと cgroup が読めなくなり `unknown` になる（安全側に倒れる）。足すときはこの規則を見直す

### 2.7 Server Health の `ai_layer`: 要約の成否のまま（0040 §2.7 を変えない）

- `ai_layer.status` は今のまま「要約が作れたら `ok`、作れなければ `stopped`」とする（§5 の 2）。PAW が vLLM を止めている間は
  要約が作れず `stopped` になり、それが実態に合う。signal の判定からは外したまま（0040 §2.4）
- 窓口の疎通を別に見に行く（`GET /v1/models` を周期的に呼ぶ）ことはしない。PAW の窓口への要求を増やし、`ok` でも生成が失敗しうる
- 導入先の API は `coldaisle.api:app` なので `ai_layer` は常に `stopped` のまま。`coldaisle.server:app` へ切り替えるかは 0143 §5 の 5 のまま（本記録では決めない）

### 2.8 #64（Docker Compose の3層分離）を Rejected とし、0028 §5 の 7・0080 §5 の 13 を閉じる

**理由**

- **Core はすでにホストの systemd で本番稼働している**（#57 / 0069 / 0080 / 0117）。Compose へ移す利点がなく、移すと導入・権限・`StateDirectory` の設計
  （0080 §2.1 の setgid と `UMask`）を作り直すことになる
- **fand と gpu-fan はホストで直接動かす必要がある。** fand は hwmon へ書く唯一のプロセスで、deadman・`ExecStopPost` の引き継ぎ・watchdog を
  systemd に頼っている（0080）。gpu-fan は root で NVML の Fan を書き、止まったら既定の制御へ戻す（0128）。コンテナを挟むと、デバイスの受け渡しと
  停止時の後片付けの経路が増え、Critical Safety の前提（ルール 3）を確かめ直すことになる
- **「CoreはGPUをmountしない」は今の Core と合わない。** `coldaisle-telemetry` は監視のために NVML を読む（0117 / D-07 の単一の情報源）。
  Core が GPU を要さないのは「GPU で計算しない」という意味で、それはホストのままで満たしている
- **GPU AI の層は coldaisle に無い。** 推論サーバは PAW の unit で、起動・停止も PAW が持つ（§2.3）。Compute Workload も coldaisle の外にある。
  3 層のうち coldaisle が持つのは Core だけになる
- #63 の「Core と GPU AI の分離」は、デプロイの単位（unit）を分け、coldaisle の unit が PAW の unit に依存しないこと（§2.3）と、
  GPU AI の有無を `sys.gpu_state` で観測できること（§2.6）で満たす

**閉じる未決**（原文）

| 記録 | 原文 | 答え |
|---|---|---|
| 0028 §5 の 7 | 「コンテナ化（#64）で `coldaisle-fand` をホストで直接動かすか」 | **ホストで直接動かす**（コンテナ化しない） |
| 0080 §5 の 13 | 「コンテナ化（#64）で fand をホストで直接動かすか。本記録はホストで直接動かす前提」 | **ホストで直接動かす**（0080 の前提のまま） |

0028 / 0080 は FINAL なので本文を書き換えない。本記録が FINAL になったら #64 を not planned で閉じる。

### 2.9 文書と実装の直し方: この PR は決定記録だけ。文書と実装は承認の後

この PR には本記録と索引の行だけを入れる（§5 の 7）。FINAL の後に、次を別の Issue・ブランチで行う。

| 順 | 内容 | 種類 |
|---|---|---|
| 1 | `docs/requirements.md` §7.2 の vLLM の起動例（`hermes` / 8001 / 0.4 / 32768）を「PAW の vLLM を共用する。coldaisle は起動しない」に改め、§2.1 の値と §2.2 の前提を書く。`docs/llm-setup.md` の本番の節とスモークテスト（§2.2 の表）・NFR-04 の測り方（§2.4）・戻す手順（§2.5）。`config/ai.yaml` の注釈の例 | `docs:` |
| 2 | `sys.gpu_state` の収集（§2.6）と `config/gpu_state.yaml`。cgroup の読み取りは偽の `/proc` で試験する（ルール 7） | `feat:`（#63） |
| 3 | 導入先の切り替え（§2.5 の 1〜5） | 所有者が導入先で |
| 4 | （行わない）`AnthropicProvider` の削除。§5 の 1 で残すと決めた | — |

---

## 3. Consequences

| 良くなること | 悪くなること | 緩和 |
|---|---|---|
| VRAM に 27B を 2 つ載せずに済む（PAW の 0.51 に coldaisle の 0.4 を足すと 0.91 で、Ollama の分が残らない） | PAW の Scheduler が止めている間は所見・説明が付かない（Compute Mode 以外でも止まりうる） | FR-507 のとおり監視・通知は止まらない（§2.3）。必要になれば所有者が PAW の側で扱いを決める |
| 停止・起動の判断が PAW の1か所になり、二重の調整が要らない | 窓口（ポート・`main`）が PAW の都合で変わりうる | 変わっても「利用不可」になるだけ。PAW の変更時に §2.2 のスモークテスト |
| Claude API の鍵と外への経路が導入先から消える（0143 §2.9） | `main` の実体が変わっても coldaisle は気づかない | スモークテストで `/v1/models` の `root` を記録（§5 の 8） |
| Compose を作らず、今の systemd の導入・権限・deadman の設計をそのまま使える | #64 の Issue の構想（3 層）は実現しない | Core と GPU AI の分離は unit の独立と `sys.gpu_state` で満たす（§2.8） |
| `sys.gpu_state` が子プロセスやプロセス名の違いに左右されない | PAW や Ollama の unit 名が変わると、その process を `compute` と誤り、`compute` は通常の値なので警告も出ない（実行時には気づけない） | 一覧を設定に置く（§2.6）。導入時と PAW の変更時に陽性の対照（vLLM を動かして `ai` になる）を取り、取れていない間は `compute` を「AI 由来 0」の証拠に使わない（§2.6） |
| | 共用の窓口なので、PAW の長い要求と重なると coldaisle の要求が遅れ `timeout_s` で利用不可になりうる | 監視は待たない（0014 §2.5）。NFR-04 は重ならないときで測り、重なったときは参考（§2.4） |
| | 日次レポートの時刻に vLLM が止まっていると、その日の所見が欠ける | 再試行や待ちは足さない（0014 §2.5）。表は届く |

---

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| coldaisle 専用の vLLM を立てる（要件 §7.2 の当初の案） | 所有者の決定（§1.1）。VRAM に同じ規模のモデルが 2 つ載り、停止の調整が 2 か所になる |
| coldaisle が必要なときに PAW の vLLM を `systemctl start` する | coldaisle に GPU のプロセスを動かす権限を持たせることになり、Compute Mode を優先する PAW の判断と衝突する。D-14「常駐を前提にしない」の趣旨にも反する |
| vLLM が起きるまで待つ・後で作り直す | 0014 §2.5「落ちている相手に粘らない」。日次レポートとアラートの説明が PAW の都合で遅れ、待ちの間の状態を持つことになる |
| vLLM が止まっているときに Claude API へ自動で切り替える | 外へ送るかどうかが明示の opt-in（0143 §2.2）ではなく PAW の運転状態で決まる。費用と送信の範囲が読めなくなる（0019） |
| Provider に tool loop を足して `qwen3_coder` の `tool_calls` を coldaisle の中で実行する | coldaisle の LLM の呼び出しはツールを要さない（説明の Evidence は決定論的に集める）。モデルが選んだツールを coldaisle の中で走らせる経路を増やす理由が無い（ルール 1 の読み取り専用は保てても、必要のない面を増やす） |
| `reasoning_content` / `reasoning` を読んで `ChatResult.thinking` に入れる | 読む所が無い。vLLM の版で欄の名前が変わるので、使う所ができたときにその PR で読む |
| `sys.gpu_state` をプロセス名（`comm` / `cmdline`）で識別する | vLLM の worker も学習のジョブも `python` で、名前では分けられない。子プロセスの名前は vLLM の版で変わる |
| `sys.gpu_state` を PAW の GPU Mode の事象から決める | 指令と実態はずれる（0002 §2.6）。PAW が事象を送らなければ決まらない |
| PAW の API に「いま推論サーバが動いているか」を問い合わせる | coldaisle の観測が PAW に依存する。NVML と `/proc` はホストの事実で、PAW が止まっていても読める |
| `ai_layer` を窓口の疎通（`/v1/models` の周期的な確認）で決める | PAW の窓口への要求が増え、`ok` でも生成は失敗しうる。要約の成否のほうが利用者に見える結果と一致する（§2.7） |
| `ai_layer` を `sys.gpu_state = ai` から決める | Ollama も `ai` に入り、unit が動いていても窓口が使えるとは限らない |
| Docker Compose の3層分離（#64） | §2.8 |
| この PR で文書も直す | 決定の前に文書が先へ進み、レビューで決定が変わると文書を 2 度直す。承認の後に1回で直す（§2.9） |

---

## 5. 判断点（決定済み。推奨を先頭に）

**所有者の決定（2026-10-21）**: 1〜10 はすべて推奨の案（いずれも (a)）で決めた。§2 の本文はその案を書いたものである。

| # | 判断点 | 選択肢（推奨を先頭） | 決定 |
|---|---|---|---|
| 1 | `AnthropicProvider` を残すか（0143 §5 の 2） | **(a 推奨) コードを残し、導入先の `COLDAISLE_LLM_PROVIDER` と鍵は消す**。自動の切り替えはしない（§2.5）。0143 は FINAL のまま / (b) コードと試験を消し、0143 を `Superseded` にする記録を書く（外へ送る経路がコードからも消える。再び要るときは作り直す） | 決定済み（所有者、2026-10-21）: (a) |
| 2 | Server Health の `ai_layer` の決め方 | **(a 推奨) 要約の成否のまま**（0040 §2.7。§2.7）/ (b) 窓口の疎通（`/v1/models`）で決める / (c) `sys.gpu_state` から決める | 決定済み（所有者、2026-10-21）: (a) |
| 3 | `sys.gpu_state` の識別規則 | **(a 推奨) NVML の PID → `/proc/<pid>/cgroup` の最も内側の `.service` を `ai_units` の一覧と照合**（§2.6）/ (b) プロセス名で照合する / (c) PAW の GPU Mode の事象から決める | 決定済み（所有者、2026-10-21）: (a) |
| 4 | `config/gpu_state.yaml`（`ai_units`）の置き場所 | **(a 推奨) リポジトリの `config/gpu_state.yaml` に unit 名を書く**（0002 §2.6 のとおり。変更がレビューを通る。unit 名は個体識別子ではない）/ (b) リポジトリには形と例だけを置き、導入先の `/etc/coldaisle/` の写しを環境変数で指す（`COLDAISLE_SERVER_HEALTH` と同じ形。PAW の unit 名をリポジトリに出さない） | 決定済み（所有者、2026-10-21）: (a) |
| 5 | cgroup が読めない PID があるとき | **(a 推奨) 全体を `unknown`**（判定不能。最も保守的な閾値。§2.6）/ (b) その PID を `compute` に数える（推論サーバと示せないものは計算とみなす） | 決定済み（所有者、2026-10-21）: (a) |
| 6 | NFR-04 の合格の基準 | **(a 推奨) warm・PAW の要求が無いときに 20 回すべて 2 s 以内**。重なったときは参考（§2.4）/ (b) 20 回の 95 パーセンタイル（2 番目に遅い値）が 2 s 以内 / (c) 重なったときも含めて判定する | 決定済み（所有者、2026-10-21）: (a) |
| 7 | 文書と実装をどの PR で直すか | **(a 推奨) この PR は決定記録だけ。FINAL の後に §2.9 の順で別の PR** / (b) この PR で `docs/requirements.md` §7.2 と `docs/llm-setup.md` も直す | 決定済み（所有者、2026-10-21）: (a) |
| 8 | `main` の実体の確かめ方 | **(a 推奨) 導入時と PAW の変更時のスモークテストで `/v1/models` の `root` を確かめて記録するだけ**（実行時には確かめない）/ (b) 日次レポートに実体（`root`）を載せる（毎日 `/v1/models` を 1 回呼ぶ） | 決定済み（所有者、2026-10-21）: (a) |
| 9 | AI 由来の CUDA process の数をメトリクスにするか | **(a 推奨) 足さない**。#63 の受入基準は `sys.gpu_state` の `idle` / `compute` で確かめる / (b) `sys.cuda_processes.ai` などを `readings` に足す（`metrics.yaml` と命名の検討が要る） | 決定済み（所有者、2026-10-21）: (a) |
| 10 | PAW の Scheduler に GPU Mode の事象（`coldaisle-event gpu-mode`。0045）を送ってもらうか | **(a 推奨) 本記録では求めない**（PAW 側の判断。送られれば 0045 のとおり記録し、`sys.gpu_state` の観測と並べてタイムラインに出る）/ (b) PAW の Scheduler の start / stop のたびに送ることを求める | 決定済み（所有者、2026-10-21）: (a) |
