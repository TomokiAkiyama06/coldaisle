# ローカルLLM の準備

対象は **Qwen3.8-27B**（決定記録 [0005](decisions/0005-model-selection.md)）。
接続先とモデル名は**環境変数だけ**で切り替えます（FR-501 / 決定記録 0014）。

```bash
# .env
OPENAI_BASE_URL=http://127.0.0.1:11434/v1   # Ollama
MODEL_NAME=qwen3:8b
# OPENAI_API_KEY=                           # ローカルでは不要
```

**`config/ai.yaml` に接続先を書かないでください。** 切り替えが2箇所になります。

## `.env` の読み込み方（重要）

**`.env` は自動では読まれません。** `uv run` に `--env-file` を渡すか、
シェルで export してください。

```bash
uv run --env-file .env coldaisle-daemon --source mock
# または
export UV_ENV_FILE=.env      # 以降の uv run すべてに効く
```

これは通知（`COLDAISLE_SLACK_WEBHOOK` ほか）や API の設定にも同じく必要です。
渡し忘れると、**エラーにならずに「未設定」として静かに動きます**
（LLM なら「利用不可」、通知なら stdout だけに出る）。

---

## Mac（サーバー未着の期間）

Ollama を使います。**27B が載らない場合は小さいモデルで構いません**
（要件 §7.2 の注記）。Provider 抽象が差し替えを吸収するので、
本番の選定とは独立に決められます。

```bash
brew install ollama
ollama serve                    # 既定で 127.0.0.1:11434

# 開発用の小型モデル（搭載メモリが少ない場合）
ollama pull qwen3:8b

# 27B を試す場合（Q4_K_M で約17GB。メモリに余裕が要る）
ollama pull qwen3.8:27b
```

```bash
# .env
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
MODEL_NAME=qwen3:8b
```

> **要確認**: Ollama のモデル名（タグ）は配布側の命名に従います。
> `ollama list` で実物を確認してください。

### 思考モードについて

`config/ai.yaml` の `send_thinking_flag` は、思考の有無を
`chat_template_kwargs.enable_thinking` として送るかどうかです。
**Qwen3 系 + vLLM の作法**であり、**対応しないバックエンドでは 400 になりえます。**

400 が返る場合は `send_thinking_flag: false` にしてください
（決定記録 0014 §2.3）。

---

## Ubuntu + RTX PRO 6000（本番）

vLLM を使います。**常駐させません。** Compute Mode ではプロセスごと停止して
VRAM を全解放します（決定 D-14 / 要件 §7.2）。

```bash
vllm serve Qwen/Qwen3.8-27B \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.4 \
  --port 8001
```

```bash
# .env
OPENAI_BASE_URL=http://127.0.0.1:8001/v1
MODEL_NAME=Qwen/Qwen3.8-27B
```

### `--gpu-memory-utilization 0.4` の理由

96GB のうち **4割（約38GB）** を上限にしています。27B を FP8 で載せると
重みが 30〜33GB で、残りが KV プールになります（決定記録 0005）。

**これは「常駐してよい」という意味ではありません。** 研究・学習・Kaggle が
GPU の主目的であり（#27）、Compute Mode では上限を切るのではなく
**プロセスを完全停止して VRAM を全解放**します。
停止の判定は「Docker を止めた」ではなく **NVML で CUDA プロセス数が 0 になったこと**で行います。

### 導入時に確かめること（決定記録 0005 §2.2）

- vLLM が Blackwell (sm_120) でこのモデルを動かせること
- HuggingFace のリポジトリ ID（`Qwen/Qwen3.8-27B` を想定）
- ツールコールパーサの推奨値（vLLM / モデルのバージョンで変わる）

---

## 動作確認

```bash
uv run --env-file .env python -c "
from pathlib import Path
from coldaisle.ai import AiSettings, provider_from_env
result = provider_from_env(AiSettings.from_yaml(Path('config/ai.yaml'))).probe()
print('利用可' if result.available else f'利用不可: {result.reason}')
"
```

**LLM が止まっていても、取り込み・保存・API・ダッシュボード・ルールエンジンは
そのまま動きます**（FR-507）。この確認は AI 機能を使うときだけ必要です。

---

## Claude API（ローカル LLM が入るまでの代わり。決定記録 0143）

導入先にローカル LLM（vLLM。#58）が入るまでの間だけ、日次レポートの所見・アラートの説明・
Server Health の任意要約を Claude API で作れます。**既定では使いません。**
`COLDAISLE_LLM_PROVIDER=anthropic` と明示したときだけ外へ送ります（鍵を置いただけでは送らない）。

| 環境変数 | 値 |
|---|---|
| `COLDAISLE_LLM_PROVIDER` | `anthropic`（未設定・空・`openai_compatible` なら今のまま） |
| `ANTHROPIC_API_KEY` | Claude API の鍵。`/etc/coldaisle/coldaisle.env` にだけ書く |
| `ANTHROPIC_MODEL` | モデル ID。**推奨は `claude-haiku-5-5`**。所見の質が足りなければ `claude-sonnet-5-5` |
| `ANTHROPIC_WORKSPACE_ID` | 任意。ワークスペースに属さない鍵で、API が `anthropic-workspace-id` ヘッダを求めて 400 を返すときだけ書く（`anthropic-workspace-id` ヘッダで送る）。空白を含むと Claude API を使わない。空なら送らない |

- **送るのは今のローカル LLM に渡すのと同じ集計値だけです**（日次レポートの表・アラートの Evidence の行・
  Server Health の固定の文）。生の時系列・decision trace・ツールの結果・案件資料（`coldaisle-escalate`）は送りません
- `config/report.yaml` の除外（`llm_excluded_metrics` / `llm_excluded_rules`。0131 §2.4a / 0138）はそのまま当たります
- 1 回の長さ・1 日の回数・`effort` の上限は `config/ai.yaml` の `anthropic:` です。上限に達したら呼ばずに、
  所見・説明の無い今の形で届きます（監視と通知は止まりません。FR-507）
- 鍵はログ・応答の `reason`・通知に出ません（失敗の理由は HTTP の状態コードと `error.type` だけ）

### 導入先で有効にする

**前提: 決定記録 0143 が承認され、0138 の実装（#320）と本機能が `/opt/coldaisle` に入っていること。**
0138 の前は `gpu.*.fan_speed` が LLM の入力に残っており、それが外へ出ます。

1. Anthropic の Console で、この鍵の月の上限（spend limit）を設定する（費用の正本。`config/ai.yaml` の上限は粗い蓋）
2. 鍵の変数名が `ANTHROPIC_API_KEY` であることを、**値を表示せずに**確かめる

   ```bash
   sudo grep -c '^ANTHROPIC_API_KEY=.' /etc/coldaisle/coldaisle.env   # 1 なら良い。0 なら名前が違う
   sudo grep -o '^[A-Z_]*=' /etc/coldaisle/coldaisle.env              # 変数名だけを一覧する
   ```

   別の名前で登録していたら、`sudoedit` で `ANTHROPIC_API_KEY=` に直す

3. 残りの 2 つを足す

   ```bash
   sudoedit /etc/coldaisle/coldaisle.env
   # COLDAISLE_LLM_PROVIDER=anthropic
   # ANTHROPIC_MODEL=claude-haiku-5-5
   # ANTHROPIC_WORKSPACE_ID=<Console のワークスペースの ID>   # 鍵がワークスペースに属さないときだけ
   ```

   鍵がワークスペースに属さないと、API は「`anthropic-workspace-id` ヘッダが要る」と 400 を返します
   （journal の `reason` は `HTTP 400: invalid_request_error`）。そのときは鍵を作り直すか、
   `ANTHROPIC_WORKSPACE_ID` に Console のワークスペースの ID を書きます（値はログに出ません）

4. 環境変数は起動時に読むので、常駐する 2 つを再起動する（`coldaisle-report` は次のタイマーの起動から効く）

   ```bash
   sudo systemctl restart coldaisle-daemon coldaisle-api
   ```

5. 確かめる。前日のレポートを**送らずに**作り、所見が付くかを見る（1 回ぶんの上限を使う）

   ```bash
   sudo systemd-run --wait --pipe --uid=coldaisle --gid=coldaisle \
     -p WorkingDirectory=/opt/coldaisle -p EnvironmentFile=/etc/coldaisle/coldaisle.env \
     /opt/coldaisle/.venv/bin/coldaisle-report --db /var/lib/coldaisle/coldaisle.db --no-send --print
   journalctl -u coldaisle-daemon -u coldaisle-api --since -10min | grep -i 'claude'
   ```

   「所見」の節が出れば有効です。出なければ journal の `reason`（`HTTP 401: authentication_error` など）を見ます

### ローカル LLM が入ったら戻す（0143 §2.9）

1. vLLM を動かし、上の「動作確認」を通す
2. `/etc/coldaisle/coldaisle.env` から `COLDAISLE_LLM_PROVIDER` を消し（または `openai_compatible`）、
   `OPENAI_BASE_URL` / `MODEL_NAME` を書く
3. `ANTHROPIC_API_KEY`（と `ANTHROPIC_WORKSPACE_ID`）を消し、Console で鍵を無効にする
4. `sudo systemctl restart coldaisle-daemon coldaisle-api`
