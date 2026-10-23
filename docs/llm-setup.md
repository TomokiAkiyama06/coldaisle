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

## Ubuntu + RTX PRO 6000（本番）: PAW の vLLM を共用する

coldaisle 専用の vLLM は**立てない**。同じ機械で Personal AI Workspace（PAW）が動かしている vLLM を、
OpenAI 互換の窓口として読むだけにする（決定記録 [0163](decisions/0163-shared-vllm-gpu-ai-service.md)）。
**coldaisle は vLLM を起動も停止もしない。** 起動・停止は PAW の Compute Resource Scheduler が持つ。

PAW の vLLM の値（0163 §1.2。**PAW が決める値**で、coldaisle の設定ではない）:

| 項目 | 値 |
|---|---|
| モデル | Qwen3.8-27B-FP8（決定記録 0005 の「27B FP8 単体」） |
| `--served-model-name` | `main` |
| 待ち受け | `127.0.0.1:18080`（ループバックだけ） |
| `--gpu-memory-utilization` | `0.51` |
| `--max-model-len` | `131072` |
| 思考 | `--reasoning-parser qwen3` |
| ツール | `--enable-auto-tool-choice --tool-call-parser qwen3_coder` |

導入先の `/etc/coldaisle/coldaisle.env`（git の外）に書くのは次だけ（0163 §2.1）。`config/ai.yaml` には書かない。

```bash
# /etc/coldaisle/coldaisle.env
# systemd の EnvironmentFile= は行末のコメントを取り除かない。説明は値の行に書かず、前の行に置く
# 書かなくてもよい（既定の経路）
COLDAISLE_LLM_PROVIDER=openai_compatible
OPENAI_BASE_URL=http://127.0.0.1:18080/v1
# PAW の別名。モデルの実体の名前は書かない
MODEL_NAME=main
# OPENAI_API_KEY は書かない（PAW の vLLM は --api-key を付けていない。PAW が鍵を付けたら、そのときに書く）
```

- `MODEL_NAME` を `main` にするのは、PAW がモデルを差し替えても coldaisle の設定を変えずに済むようにするためである
- ポートと `main` は PAW が決める窓口である。PAW が変えると coldaisle は「利用不可」になるだけで、監視は止まらない（FR-507）。
  変えたことに気づく仕組みは置かず、導入時と PAW の変更時に下の「スモークテスト」で確かめる
- 送るのは今と同じ集計値だけである（ルール 8 / FR-504）。生の時系列・decision trace・ツールの結果は送らない

### 起動しない・待たない（0163 §2.3）

- coldaisle は `systemctl` も D-Bus も呼ばない。coldaisle の unit は PAW の unit への `Wants=` / `Requires=` / `After=` を持たない
- 止まっていれば接続は拒否され、`max_attempts`（2）と `retry_backoff_s`（0.5 s）の後に「利用不可」として返る（0014 §2.5）。
  起きるまで待つ・起こしに行く・後で作り直すことはしない
- 止まっている間は、日次レポートは表だけ、アラートは素の文だけ、Server Health はテンプレート（`ai_layer: stopped`）になる。
  日次レポートのタイマーの時刻に vLLM が止まっていれば、その日の所見は付かない
- Compute Mode で VRAM を空けるのは PAW の Scheduler の責任である。coldaisle はそれを `sys.gpu_state`（0163 §2.6）で観測するだけである

### tool-call parser と reasoning の前提（0163 §2.2）

**今の実装のまま受けられる。コードは変えない。**

- coldaisle 自身の呼び出し（日次レポートの所見・アラートの説明・Server Health の要約・`probe()`）は `tools` を送らない。
  parser が効くのは要求に `tools` があるときだけなので、`qwen3_coder` か `hermes` かは結果に効かない
- vLLM の parser は `tool_calls[].function.arguments` を JSON の文字列で返し、`ToolRegistry.call` はそれを受ける。
  ただし coldaisle は tool loop を持たない。ツールを使う会話は Workspace のチャットが持つ（FR-502）
- reasoning parser は思考を `message.content` から別の欄（vLLM の版により `reasoning_content` または `reasoning`）へ移す。
  Provider は `content` だけを読むので、本文に思考は混ざらない。`<think>` の切り離し（0014 §2.4）は安全網として残す
- 思考の指示は今のまま `send_thinking_flag: true`（`chat_template_kwargs.enable_thinking=false` を送る。0014 §2.3）

### スモークテスト（導入時と PAW の変更時。0163 §2.2）

PAW の vLLM が動いているときに行う。

| 確かめること | 期待 |
|---|---|
| `GET /v1/models` | `main` があり、`root` が Qwen3.8-27B-FP8 を指す。`root` を導入の記録に残す |
| `Provider.probe()`（`coldaisle` の uid で。Provider を明示し、導入先の env を読ませない） | `利用可`、かつ `model` が `main`（Claude のモデル ID なら Claude を試している） |
| `enable_thinking=false` を送った要求 | 400 にならず、`content` に `<think>` が無い。400 なら `send_thinking_flag: false` にする（0014 §2.3） |
| `coldaisle-report --no-send --print`（probe と同じ形で Provider を明示する） | 「所見」の節が付き、journal に Claude の行が出ない |
| （参考）`tools` を付けた要求 1 回 | `tool_calls[].function.arguments` が JSON の文字列で返る |
| PAW の vLLM が動いているときの `sys.gpu_state`（0163 §2.6 の実装の後） | `ai`（他の CUDA process があれば `mixed`）。`compute` なら `config/gpu_state.yaml` の `ai_units` が古い |

**試すときの Provider の指定（重要）**

- `provider_from_env()` は最初に `COLDAISLE_LLM_PROVIDER` を読み、`anthropic` なら `OPENAI_BASE_URL` / `MODEL_NAME` を見ずに Claude を使う。
  0143 を有効にしたままの `/etc/coldaisle/coldaisle.env` を読ませたまま `OPENAI_BASE_URL` / `MODEL_NAME` だけを足すと、**PAW ではなく Claude を試して `利用可` になる**
- systemd では `EnvironmentFile=` の値が `--setenv` を上書きする。env を読ませたまま `--setenv=COLDAISLE_LLM_PROVIDER=openai_compatible` を足しても効かない
- そこで切り替えの前の試験（probe・レポート・NFR-04 の測定）は、**`-p EnvironmentFile=/etc/coldaisle/coldaisle.env` を付けず**、
  使う値をすべて `--setenv` で渡す。`ANTHROPIC_*` は渡さない。結果の `model` が `main` であることも出して確かめる
- 切り替えの後（下の「Claude API から戻す」の手順 2〜4 の後）の確かめは env を読ませてよい。そのときも `model` が `main` であることを見る

```bash
# 1. 窓口のモデル（root を記録する）
curl -s http://127.0.0.1:18080/v1/models | python3 -m json.tool

# 2. probe（env を読ませず、Provider を明示する）
sudo systemd-run --wait --pipe --uid=coldaisle --gid=coldaisle \
  -p WorkingDirectory=/opt/coldaisle \
  --setenv=COLDAISLE_LLM_PROVIDER=openai_compatible \
  --setenv=OPENAI_BASE_URL=http://127.0.0.1:18080/v1 \
  --setenv=MODEL_NAME=main \
  /opt/coldaisle/.venv/bin/python -c "
from pathlib import Path
from coldaisle.ai import AiSettings, provider_from_env
r = provider_from_env(AiSettings.from_yaml(Path('config/ai.yaml'))).probe()
print(('利用可' if r.available else f'利用不可: {r.reason}'), 'model=' + r.model)
"

# 3. enable_thinking=false の要求（content に <think> が無いこと。400 にならないこと）
curl -s http://127.0.0.1:18080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "main", "stream": false, "max_tokens": 64,
  "messages": [{"role": "user", "content": "ping"}],
  "chat_template_kwargs": {"enable_thinking": false}
}' | python3 -m json.tool

# 4. 日次レポート（送らない。probe と同じ形で Provider を明示する）
sudo systemd-run --wait --pipe --uid=coldaisle --gid=coldaisle \
  -p WorkingDirectory=/opt/coldaisle \
  --setenv=COLDAISLE_LLM_PROVIDER=openai_compatible \
  --setenv=OPENAI_BASE_URL=http://127.0.0.1:18080/v1 \
  --setenv=MODEL_NAME=main \
  /opt/coldaisle/.venv/bin/coldaisle-report --db /var/lib/coldaisle/coldaisle.db --no-send --print
journalctl -u coldaisle-daemon -u coldaisle-api --since -10min | grep -i 'claude'   # 何も出なければ良い（上の --pipe の出力にも出ないこと）
```

### NFR-04（初回トークン 2 s）の測り方（0163 §2.4 / 0166）

Provider は `stream: false` なので、測定は Provider を通さず、同じ窓口へ同じ形の要求をストリーミングで送る一回限りの手順で行う。
**測定をコードと CI に足さない。**

| 項目 | 決め |
|---|---|
| 状態 | **warm**（PAW の vLLM が起動済みでモデルを読み終えている）。起動にかかる時間は coldaisle が待たないので範囲の外 |
| 要求 | **2 つの経路のそれぞれで実際に作られた要求の本文**（0166）。(a) 日次レポートの所見は直近 7 日で最も長い日、(b) アラートの説明は直近 7 日に発火したもので最も長いもの（無ければ最後の 1 件）。保存した本文の `stream` を `true`・`max_tokens` を 1024 にして送る（`enable_thinking=false` は保存したまま） |
| 計る区間 | 要求を送ってから、最初の `content` の差分が届くまで |
| 回数 | 経路ごとに 20 回。間をあけて送る（prefix cache で 2 回目以降だけ速くならないよう、プロンプトの先頭の時刻の行を毎回変える） |
| 合格 | **経路ごとに**、PAW の他の要求が無いときに **20 回すべて 2 s 以内**（(a) と (b) の両方） |
| 参考 | PAW の要求と重なったときの値も記録する。合否には使わない（共用の窓口で、相手の負荷は coldaisle が決められない） |

**要求の本文を拾う**（0166 §2.2。手で同じ長さの文を作らない）。`/opt/coldaisle` ではなく作業用のフォルダで行う
（`--no-send` でもレポートは作業ディレクトリの `var/reports/` に書かれる）。拾った本文には DB の値が入るので、リポジトリに入れない。

```bash
mkdir -p ~/nfr04 && cd ~/nfr04 && ln -sfn /opt/coldaisle/config config
# DB の写し（導入先の DB は操作者から読めないので root で backup を取って渡す）
sudo /opt/coldaisle/.venv/bin/python -I -c "import sqlite3,sys; sqlite3.connect('file:/var/lib/coldaisle/coldaisle.db?mode=ro', uri=True).backup(sqlite3.connect(sys.argv[1]))" "$PWD/copy.db"
sudo chown "$(id -u):$(id -g)" copy.db
# 下の 3 つのスクリプトを ~/nfr04 に置く（record.py / explain.py / measure.py）
export COLDAISLE_LLM_PROVIDER=openai_compatible OPENAI_BASE_URL=http://127.0.0.1:18099/v1 MODEL_NAME=main

# (a) 日次レポート: 直近 7 日の各日の要求を拾う
python3 -I record.py req-report & rec=$!
for i in 1 2 3 4 5 6 7; do
  /opt/coldaisle/.venv/bin/coldaisle-report --db copy.db --date "$(date -d "-$i day" +%F)" --no-send --print >/dev/null
done
kill "$rec"

# (b) アラートの説明: 直近 7 日に発火したもの（無ければ最後の 1 件）の要求を拾う
python3 -I record.py req-explain & rec=$!
/opt/coldaisle/.venv/bin/python -I explain.py copy.db
kill "$rec"

ls -S req-report/*.json | head -1    # (a) で使う本文（最も長いもの）
ls -S req-explain/*.json | head -1   # (b) で使う本文
```

`record.py`（ループバックだけで待ち受け、要求の本文を保存して空の応答を返す。空なので所見と説明は捨てられる）:

```python
# 受けた chat/completions の要求の本文をそのまま保存し、空の応答を返す（ループバックだけ・測定の準備の一回だけ）
import http.server, json, sys, pathlib
out = pathlib.Path(sys.argv[1]); out.mkdir(exist_ok=True)
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        n = len(list(out.glob("*.json")))
        (out / f"{n:03d}.json").write_bytes(body)
        reply = json.dumps({"id": "r", "object": "chat.completion", "model": "main",
                            "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": ""}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply))); self.end_headers(); self.wfile.write(reply)
http.server.HTTPServer(("127.0.0.1", 18099), H).serve_forever()
```

`explain.py`（本番の説明スレッドと同じ部品で説明の要求を作る）:

```python
# 直近 7 日に発火したアラート（無ければ最後の 1 件）の説明の要求を、本番の説明スレッドと同じ部品で作る
import sqlite3, sys, time
from pathlib import Path
from coldaisle.ai import AiSettings, Explainer, ToolRegistry, provider_from_env
from coldaisle.clock import WallClock
from coldaisle.llm_exclusions import LlmExclusions
from coldaisle.metrics import MetricCatalog
from coldaisle.rules import RuleSet
from coldaisle.store import QualityRules, SqliteStore
db = Path(sys.argv[1])
since = int(time.time() * 1000) - 7 * 86_400_000
with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
    ids = [r[0] for r in con.execute("SELECT id FROM alerts WHERE fired_ms >= ? ORDER BY id", (since,))]
    ids = ids or [r[0] for r in con.execute("SELECT id FROM alerts WHERE fired_ms IS NOT NULL ORDER BY fired_ms DESC LIMIT 1")]
settings = AiSettings.from_yaml(Path("config/ai.yaml"))
clock = WallClock()
store = SqliteStore(db, rules=QualityRules.from_yaml(Path("config/quality.yaml")), clock=clock)
tools = ToolRegistry(store=store, catalog=MetricCatalog.from_yaml(Path("config/metrics.yaml")),
                     rules=RuleSet.from_yaml(Path("config/rules.yaml")), clock=clock,
                     exclusions=LlmExclusions.from_yaml(Path("config/report.yaml")))
explainer = Explainer(provider=provider_from_env(settings), tools=tools, context=settings.explain_context)
for alert_id in ids:
    alert = store.alert(alert_id)
    if alert is not None:
        explainer.explain(alert)
print(f"説明の要求を {len(ids)} 件作った")
```

**測る**（PAW の vLLM が warm で、PAW の他の要求が無いときに。経路ごとに 20 回）:

```bash
python3 -I measure.py "$(ls -S req-report/*.json | head -1)"
python3 -I measure.py "$(ls -S req-explain/*.json | head -1)"
```

`measure.py`:

```python
# 保存した要求を stream で 20 回送り、最初の content の差分までの秒数を出す
import json, sys, time, urllib.request
body = json.load(open(sys.argv[1], encoding="utf-8"))
url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:18080/v1/chat/completions"
body.update(stream=True, max_tokens=1024)
system = body["messages"][0]
base = system["content"]
for n in range(1, 21):
    system["content"] = f"測定 {time.time_ns()}\n{base}"  # 先頭を毎回変える（prefix cache を避ける）
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    start = time.monotonic(); first = None
    with urllib.request.urlopen(req, timeout=60) as resp:
        for line in resp:
            if first is None and line.startswith(b"data: {"):
                choices = json.loads(line[6:]).get("choices") or [{}]
                if (choices[0].get("delta") or {}).get("content"):
                    first = time.monotonic() - start
    print(f"{n:2d} {'到達せず' if first is None else f'{first:.3f} s'}", flush=True)
    time.sleep(5)
```

- 結果は文書の PR か導入の記録に残す
- 2 s を超えたら、所有者が PAW 側の設定（`--max-num-seqs` など）を含めて扱いを決める

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
導入先（本番）では、これではなく上の「スモークテスト」を使う（Provider を明示し、`model` も確かめる）。

---

## Claude API（ローカル LLM が入るまでの代わり。決定記録 0143）

導入先がローカル LLM（PAW の vLLM の共用。#58 / 決定記録 0163）へ切り替わるまでの間だけ、日次レポートの所見とアラートの説明を
Claude API で作れます。**既定では使いません。**
Server Health の任意要約は、API を `coldaisle.server:app` で起動したときだけ作られます。
導入先の `coldaisle-api.service` は `coldaisle.api:app` で起動しているので、今の導入では作られません（0143 §5 の 5）。
`COLDAISLE_LLM_PROVIDER=anthropic` と明示したときだけ外へ送ります（鍵を置いただけでは送らない）。

| 環境変数 | 値 |
|---|---|
| `COLDAISLE_LLM_PROVIDER` | `anthropic`（未設定・空・`openai_compatible` なら今のまま） |
| `ANTHROPIC_API_KEY` | Claude API の鍵。`/etc/coldaisle/coldaisle.env` にだけ書く |
| `ANTHROPIC_MODEL` | モデル ID。**推奨は `claude-haiku-5-5`**。所見の質が足りなければ `claude-sonnet-5-5`。**モデル ID は Anthropic のモデルの一覧（Console / Models API）で確かめる**（世代が替わると ID も替わる） |
| `ANTHROPIC_WORKSPACE_ID` | 任意。ワークスペースに属さない鍵で、API が `anthropic-workspace-id` ヘッダを求めて 400 を返すときだけ書く（`anthropic-workspace-id` ヘッダで送る）。空白を含むと Claude API を使わない。空なら送らない |

- **送るのは今のローカル LLM に渡すのと同じ集計値だけです**（日次レポートの表・アラートの Evidence の行・
  `coldaisle.server:app` のときの Server Health の固定の文）。生の時系列・decision trace・ツールの結果・案件資料（`coldaisle-escalate`）は送りません
- `config/report.yaml` の除外（`llm_excluded_metrics` / `llm_excluded_rules`。0131 §2.4a）はそのまま当たります。
  0146 で一覧は空になり、GPU の Fan の値（目標値の %）もローカル LLM と同じく送ります
- 1 回の長さ・1 日の回数の上限と `effort`（既定は送らない）は `config/ai.yaml` の `anthropic:` です。上限に達したら呼ばずに、
  所見・説明の無い今の形で届きます（監視と通知は止まりません。FR-507）
- 鍵はログ・応答の `reason`・通知に出ません（失敗の理由は HTTP の状態コードと `error.type` だけ）

### 導入先で有効にする

**前提: 決定記録 0143 が承認され、本機能が `/opt/coldaisle` に入っていること。**

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

   **モデル ID は Anthropic のモデルの一覧（Console / Models API）で確かめてから書きます。**

   再起動の前に、鍵・ワークスペース・モデル ID で 1 回だけ疎通を確かめる（1 回ぶんの上限を使う。所有者の手元の
   確かめのスクリプトがあればそれでもよい）

   ```bash
   sudo systemd-run --wait --pipe --uid=coldaisle --gid=coldaisle \
     -p WorkingDirectory=/opt/coldaisle -p EnvironmentFile=/etc/coldaisle/coldaisle.env \
     /opt/coldaisle/.venv/bin/python -c "
   from pathlib import Path
   from coldaisle.ai import AiSettings, provider_from_env
   r = provider_from_env(AiSettings.from_yaml(Path('config/ai.yaml'))).probe()
   print('利用可' if r.available else f'利用不可: {r.reason}')
   "
   ```

   `利用可` なら良い。`HTTP 404: not_found_error` はモデル ID の誤り、`HTTP 401: authentication_error` は鍵の誤り、
   `HTTP 400: invalid_request_error` はワークスペースの ID の不足か誤りを疑う（値はログに出ない）

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

### Claude API から戻す（0143 §2.9 / 0163 §2.5）

0143 §2.9 の手順を、上の「本番」の値で行う。**所有者が導入先で行う。**

1. PAW の vLLM が動いているときに、上の「スモークテスト」を通す。`/etc/coldaisle/coldaisle.env` を変える前なので、
   **env を読ませず、Provider を `--setenv=COLDAISLE_LLM_PROVIDER=openai_compatible` で明示する**（「試すときの Provider の指定」）。
   `利用可` と `model` が `main` の両方が出るまで手順 2 へ進まない（Claude を試して通ったまま鍵を消さない）
2. `/etc/coldaisle/coldaisle.env` の `COLDAISLE_LLM_PROVIDER=anthropic` を `COLDAISLE_LLM_PROVIDER=openai_compatible` に書き換え
   （消してもよいが、明示のほうが読み違えない）、`OPENAI_BASE_URL=http://127.0.0.1:18080/v1` と `MODEL_NAME=main` を書く
3. `ANTHROPIC_API_KEY`（と `ANTHROPIC_WORKSPACE_ID`・`ANTHROPIC_MODEL`）を消し、Anthropic の Console で鍵を無効にする
4. `sudo systemctl restart coldaisle-daemon coldaisle-api`（`coldaisle-report` は次のタイマーの起動から効く）
5. env を読ませた形（`-p EnvironmentFile=/etc/coldaisle/coldaisle.env`）で `probe()` の `model` が `main` であることと、
   前日のレポートを `--no-send --print` で作り所見が付くこと・journal に Claude の行が出ないことを確かめる

**`AnthropicProvider` のコードは残す**（0163 §2.5）。外へ送るのは `COLDAISLE_LLM_PROVIDER=anthropic` と鍵の両方を置いたときだけで、
手順 2・3 で両方が導入先から消える。**vLLM が止まっているときに Claude API へ自動で切り替える経路は無い。** 止まっていればテンプレートへ縮退する。
