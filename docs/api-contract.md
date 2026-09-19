# API 契約（Workspace 連携の境界）

**バージョン**: v1
**位置づけ**: 本リポジトリと Personal AI Workspace の**唯一の接点**

> この文書が2つのリポジトリの境界を定義します。
> Workspace 側はこの契約だけに依存し、coldaisle の内部実装を知りません。
> 逆に coldaisle は Workspace の存在を知りません（起動もしないし、参照もしない）。

---

## 1. 基本原則

| 原則 | 内容 |
|---|---|
| **読み取り専用** | v1 に POST / PUT / DELETE は存在しない。Workspace から状態を変更できない |
| **一方向依存** | Workspace → coldaisle。逆向きの依存を作らない |
| **coldaisle は単独で動く** | Workspace が停止していても、取り込み・保存・アラート・通知は完全に機能する |
| **バージョンはパスに持つ** | `/api/v1/...`。破壊的変更は `/api/v2/` を並走させて移行する |
| **契約の変更は必ず両リポジトリに Issue を立てる** | 片側だけ変えない |

---

## 2. エンドポイント

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/api/v1/health` | デーモンの稼働状態、最終受信時刻、ソース種別、欠損率 |
| GET | `/api/v1/server-health` | **Server Health パネル1枚分。Workspace が最も多く叩く** |
| GET | `/api/v1/latest` | 全メトリクスの最新値 + 派生値 + quality |
| GET | `/api/v1/metrics` | メトリクスの表示名・単位と派生値の式（値は含まない）（決定記録 0039） |
| GET | `/api/v1/series` | 時系列。`metric` `from` `to` `agg` |
| GET | `/api/v1/stats` | min/max/mean/p95/傾き/欠測率 |
| GET | `/api/v1/alerts` | アラート一覧 |
| GET | `/api/v1/gpu/processes` | CUDA プロセス一覧と VRAM 使用量 |
| GET | `/api/v1/devices` | 記録されたセンサー構成（チャネル / メトリクス / ROM）（#14） |
| GET | `/api/v1/tools` | **AI 向けツールの関数定義**と注意書き（#23） |
| GET | `/api/v1/tools/{name}` | ツールを1つ実行し、結果と呼び出しの記録を返す（#23） |
| WS | `/api/v1/stream` | 新サンプルの push |
| WS | `/api/v1/server-health/stream` | GET `/server-health` と同一 payload の push |

---

## 3. Workspace が使う主要レスポンス

### `GET /api/v1/server-health`

Server Health パネル1枚を描くのに必要な情報を、**1リクエストで**返します。
Workspace 側で複数エンドポイントを叩いて組み立てさせないこと。

```json
{
  "schema_version": 1,
  "generated_at_ms": 1787616000000,
  "generated_at": "2026-08-25T00:00:00+00:00",
  "signal": "green",
  "summary": "監視対象のTelemetryと情報源は正常です。",
  "summary_source": "template",
  "gpu": {
    "mode": "ai",
    "metrics": {
      "gpu.0.core":         {"value": 54.0,  "unit": "C",     "quality": "ok", "age_seconds": 0.4},
      "gpu.0.hotspot":      {"value": null,  "unit": "C",     "quality": "missing", "age_seconds": null},
      "gpu.0.mem":          {"value": null,  "unit": "C",     "quality": "missing", "age_seconds": null},
      "gpu.0.utilization":  {"value": 42.0,  "unit": "%",     "quality": "ok", "age_seconds": 0.4},
      "gpu.0.vram_used":    {"value": 8.0,   "unit": "GB",    "quality": "ok", "age_seconds": 0.4},
      "power.gpu.0":        {"value": 180.0, "unit": "W",     "quality": "ok", "age_seconds": 0.4},
      "sys.cuda_processes": {"value": 2.0,   "unit": "count", "quality": "ok", "age_seconds": 0.4}
    }
  },
  "environment": {
    "metrics": {
      "air.room":             {"value": 26.4, "unit": "C",   "quality": "ok", "age_seconds": 0.4},
      "air.room_humidity":    {"value": 48.2, "unit": "%RH", "quality": "ok", "age_seconds": 0.4},
      "air.front_intake":     {"value": 27.9, "unit": "C",   "quality": "ok", "age_seconds": 0.4},
      "air.gpu_intake":       {"value": 28.3, "unit": "C",   "quality": "ok", "age_seconds": 0.4},
      "air.gpu_exhaust":      {"value": 35.0, "unit": "C",   "quality": "ok", "age_seconds": 0.4},
      "air.top_exhaust":      {"value": 31.0, "unit": "C",   "quality": "ok", "age_seconds": 0.4},
      "air.rear_exhaust":     {"value": 32.0, "unit": "C",   "quality": "ok", "age_seconds": 0.4},
      "cpu.package":          {"value": 49.0, "unit": "C",   "quality": "ok", "age_seconds": 0.4},
      "power.cpu.package":    {"value": 72.0, "unit": "W",   "quality": "ok", "age_seconds": 0.4},
      "cpu.vrm":              {"value": 46.0, "unit": "C",   "quality": "ok", "age_seconds": 0.4},
      "board.chipset":        {"value": 44.0, "unit": "C",   "quality": "ok", "age_seconds": 0.4},
      "board.connector_12v2x6":{"value": null, "unit": "C",   "quality": "missing", "age_seconds": null}
    }
  },
  "active_alerts": [],
  "sources": {
    "sensor_unit": {"status": "ok", "detail": "ingest_source=serial", "last_sample_ts_ms": 1787615999600, "last_sample_at": "2026-08-24T23:59:59.600000+00:00"},
    "nvml":        {"status": "ok", "detail": "collector_state=ok", "last_sample_ts_ms": 1787615999600, "last_sample_at": "2026-08-24T23:59:59.600000+00:00"},
    "lm_sensors":  {"status": "ok", "detail": "collector_state=ok", "last_sample_ts_ms": 1787615999600, "last_sample_at": "2026-08-24T23:59:59.600000+00:00"},
    "ai_layer":    {"status": "stopped", "detail": "deterministic template used", "last_sample_ts_ms": null, "last_sample_at": null}
  },
  "compute_mode_advisory": {"safe": true, "warnings": [], "blocking": false}
}
```

`gpu.metrics` と `environment.metrics` のキーは値が取得できなくても省略しません。
その場合は `value: null`、`quality: "missing"`、`age_seconds: null` です。
Workspace はこの応答だけで GPU パネルを描き、`nvidia-smi` や hwmon を直接呼びません。

`summary` だけは AI が**確定済みテンプレートと同じ signal の許可済み文面を選べます**。
自由文は公開せず、完全一致する閉じた候補以外は不正出力として捨てます。生の測定値や
Compute Mode の判断は AI へ渡しません。生成はバックグラウンドで行い、REST / WS は
完了を待ちません。AI が未設定、生成中、停止中、不達、不正出力のいずれでも、固定
テンプレートを即座に返して `summary_source: "template"`、
`sources.ai_layer.status: "stopped"` とします。成功した言い換えは同じテンプレートの
間だけ cache します。AI は `signal`、`sources`、`active_alerts`、
`compute_mode_advisory` を変更せず、安全保証・切替推奨・操作実行も文面に書けません。

**`signal` の判定規則**

判定規則の詳細（判定対象の metric、quality と source 状態の導出、アラートの数え方、
読み出しの時点）は[決定記録 0042](decisions/0042-server-health-signal-rules.md)にあります。

| 値 | 条件 |
|---|---|
| `green` | sensor_unit / nvml / lm_sensors がすべて `ok`、発生中アラート無し、監視対象の周期メトリクスがすべて `quality=ok`（`missing_tolerated` の `missing` は除く） |
| `yellow` | 情報源が `degraded`、critical 以外のアラートが発生中、または監視対象の周期メトリクスの一部が `suspect` / `missing` / `stale` / 未保存 |
| `red` | 情報源が `unavailable` / `disabled` / `stopped`、critical アラートが発生中、または監視必須データが取得不能 |

**データが古い場合に `green` を返してはいけません。**
無音で古い値を表示するのが、監視システムの最悪の失敗です。
監視必須メトリクスが**すべて** `stale` または欠測（`missing`、または一度も保存されて
いない）のときは情報源が `unavailable` となり、`red` を返します。一部だけなら
`degraded` / `yellow` であり、green にはしません。`suspect` は値が届いているため、
すべてが `suspect` でも `degraded` / `yellow` です（`red` にはしません）。
lm_sensors は有効な hwmon 入力の**いずれか1本**が届いていれば `ok` で、届かない
入力は metric 単位の規則で `yellow` になります（決定記録 0040 §5 未決1）。
発生時だけ記録する `sys.dropped_samples` 等はこの鮮度判定から
外します（決定記録 0009 §2.12）。AI 停止は監視の停止ではないため signal 判定から
外します。

`config/server-health.yaml` の `missing_tolerated` に挙げた metric（GPU / driver が
公開しない `gpu.0.hotspot` / `gpu.0.mem` など）は、`missing` だけを signal 判定から
外します（一度も保存されていない場合も同じ）。`suspect` / `stale` は外しません。監視必須 metric と監視対象の一覧も同じ
ファイルにあります（決定記録 0040）。

signal が見る周期メトリクスは**現在の監視対象**に限ります。`config/server-health.yaml`
の source ごとの監視必須 metric と、`config/internal-telemetry.yaml` で有効な入力
（NVML と hwmon）です。パネル（`panels`）は表示専用で、入力が無効な metric は値が
古くても signal に影響しません。無効化・撤去した入力の最後の行は DB に残って
`stale` になり、パネルにはそのまま `stale` と表示されます。

`active_alerts` は新しい順に `config/server-health.yaml` の `active_alerts_limit` 件
（既定 100）までです。signal の判定は件数上限に関係なく発生中の**全件**で行い、
一覧から外れたアラートは `compute_mode_advisory.warnings` に severity ごとの件数
（例: `more active alerts not listed: critical=1`）として残します。

**`GET /api/v1/server-health` と `WS /api/v1/server-health/stream` の payload は同一です。**
WebSocket 専用の封筒やフィールドは加えません。

### 時刻の表し方

すべての応答は時刻を**2つの形**で返します。

| 形 | 例 | 用途 |
|---|---|---|
| `*_ms` | `1787616000000` | 計算用。Unix ミリ秒（UTC） |
| `ts` / `last_sample_at` | `2026-08-25T00:00:00+00:00` | 人間とログ用。ISO8601 |

**サーバはタイムゾーンを持ちません。** 上の例に `+09:00` があるのは表示例で、
API が返すオフセットは `+00:00` です。同じ瞬間を指すので解釈は変わりません。
ローカル時刻への変換は Workspace 側で行ってください（決定記録 0009 §2.3）。

### `compute_mode_advisory`

```json
{"safe": false, "warnings": ["active alert: INTAKE_HIGH (warning)"], "blocking": false}
```

`safe` は**現在の決定論的 signal が green か**だけを示し、将来の高負荷時の
安全を保証しません。実測フルロード履歴との比較は #50 の測定完了後に追加します。
`blocking` は型でも **常に false** です。判断は人間が行います（決定 D-08）。
将来もこのフィールドを true にする実装を入れないでください。

### `GET /api/v1/devices`

**どの物理プローブがどのメトリクスか**を返します（#14 / FR-403）。

```json
{
  "devices": [
    {
      "device_id": "xiao-esp32s3",
      "fw": "1.0.0",
      "interval_ms": 2500,
      "last_hello_at": "2026-09-10T02:00:00+00:00",
      "sensors": [
        {"channel": "front_intake", "metric": "air.front_intake", "kind": "ds18b20",
         "gpio": 1, "rom": "28FFFFFFFFFFFF01", "observed_rom": null,
         "changed": false, "resolution": 11},
        {"channel": "rear_exhaust", "metric": "air.rear_exhaust", "kind": "ds18b20",
         "gpio": 7, "rom": "28FFFFFFFFFFFF05", "observed_rom": "28FFFFFFFFFFFF09",
         "changed": true, "resolution": 11}
      ]
    }
  ]
}
```

`rom` は**記録された**個体、`observed_rom` は**いま繋がっている**個体です。
`changed` が `true` の行は、較正のオフセットが**別のプローブに対応している**
状態です（FR-403）。

**`changed` を使ってください。** アラートの文面から読み取らないこと。文面は
最初の不一致のまま更新されないことがあり、一覧の上限で古いアラートが落ちることも
あります。

**これは「記録された構成」であって、いま繋がっている構成ではありません。**
この2つが食い違っている状態が `PROBE_CHANGED` であり、
**記録の側は人が較正をやり直すまで動きません**（決定記録 0012 §2.6）。

起動バナーを受け取る前は `devices` が空です。推測で埋めません。

### `GET /api/v1/metrics`

メトリクス名から**人間向けの表示名**を引く表です（決定記録 0039）。
`config/metrics.yaml` をそのまま返し、値は含みません（値は `/latest`）。

```json
{
  "metrics": {"air.room": {"unit": "C", "label": "室温"}},
  "derived": {
    "d.intake_rise": {
      "unit": "C", "label": "吸気上昇（再循環の指標）",
      "minuend": "air.front_intake", "subtrahend": "air.room"
    }
  }
}
```

**機械はメトリクス名で参照してください。** 表示名は変わることがあります（決定記録 0009 §2.1）。
派生値は `minuend − subtrahend` です。DB を読まないため、取り込みが止まっていても返ります。

### `GET /api/v1/tools` と `GET /api/v1/tools/{name}`

Workspace のチャットが coldaisle のデータを読むための窓口です。
**チャットUI は coldaisle 側では作りません**（決定 D-4）。ここが公開するのは
「モデルに渡す関数定義」と「それを実行する口」だけです。

```json
{
  "read_only": true,
  "advisory": true,
  "guidance": "これらのツールは読み取り専用です。…回答は人間への提案であって、実行される操作ではありません。…",
  "tools": [ { "type": "function", "function": { "name": "get_stats", "description": "…（読み取り専用。制御は行わない）", "parameters": {} } } ]
}
```

`guidance` は**呼び出し側の system prompt に入れてください。**
coldaisle にはファン制御も電源操作も存在しないため、モデルに
「実行しておきました」と書かせると、その時点で嘘になります。

実行は GET です。引数はクエリ文字列で渡します。

```text
GET /api/v1/tools/get_stats?metric=air.room&window=24h
```

```json
{
  "meta": {
    "tool": "get_stats",
    "arguments": {"metric": "air.room", "window": "24h"},
    "ok": true,
    "ts_ms": 1787616000000,
    "ts": "2026-08-25T00:00:00+00:00",
    "elapsed_ms": 3,
    "read_only": true,
    "advisory": true
  },
  "result": { "mean": 26.0, "p95": 28.4 }
}
```

`meta` は**回答の根拠を追うため**にあります（#23「どのツールを呼んだかを可視化」）。
Workspace 側でそのまま表示できます。

封筒（`meta` と一覧の外側）は OpenAPI に型として出ます。§4 の型生成でそのまま扱えます。
**`result` と `tools` の中身は型付けされません** — ツールごと・モデルの作法ごとに
形が変わるためです。

**存在しないツール名や壊れた引数でも 200 が返ります。** `meta.ok` が `false` になり、
`result.error` に理由が入ります。モデルが寄こす名前と引数は入力データであって
呼び出し側の誤りではないため、4xx にすると会話を続けるたびに例外を結果へ
翻訳することになります。

> **この2つのエンドポイントは `coldaisle.server:app` でだけ有効です。**
> `coldaisle.api:app` は L2 のみで動くため、ツールの窓口を持ちません
> （決定記録 0018 §2.1）。

---

## 4. 型の共有方法

**Python パッケージを共有しない。** 依存が双方向になり、片方のリリースがもう片方を止めます。

代わりに **OpenAPI を経由**します。

```text
coldaisle (FastAPI)
    │  自動生成
    ▼
openapi.json  ──→  Workspace が TypeScript 型を生成
```

- coldaisle 側: FastAPI が `/openapi.json` を自動生成する。**手書きしない**
- coldaisle 側: CI で `openapi.json` をアーティファクトとして出力する
- Workspace 側: それを取り込んで型を生成する（`openapi-typescript` 等）
- Workspace 側: 生成物をコミットする。**生成できない環境でもビルドが通るように**

---

## 5. ネットワーク

| 項目 | 値 |
|---|---|
| bind | `127.0.0.1`（既定） |
| port | `8000`（既定） |
| 認証 | なし（単一利用者・ローカル限定のため。決定 Q-13） |
| CORS | Workspace がデスクトップアプリから叩くため、必要に応じて localhost のみ許可 |

**外部公開しないでください。** LAN公開が必要になった時点で認証を設計します。

---

## 6. 契約を変更するとき

1. 破壊的変更なら `/api/v2/` を新設し、v1 を残す
2. 両リポジトリに Issue を立て、相互にリンクする
3. Workspace 側の型生成を更新する
4. v1 の廃止は、Workspace 側の移行が完了してから

**非破壊的な追加**（新しいフィールド、新しいエンドポイント）は v1 のまま行って構いません。
Workspace 側は未知のフィールドを無視する実装にしてください。
