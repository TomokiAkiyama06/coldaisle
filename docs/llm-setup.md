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
