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
| GET | `/api/v1/health` | デーモンの稼働状態、最終受信時刻、ソース種別（取り込み / 内部テレメトリ）、欠損率 |
| GET | `/api/v1/server-health` | **Server Health パネル1枚分。Workspace が最も多く叩く** |
| GET | `/api/v1/latest` | 全メトリクスの最新値 + 派生値 + quality |
| GET | `/api/v1/metrics` | メトリクスの表示名・単位と派生値の式（値は含まない）（決定記録 0039） |
| GET | `/api/v1/series` | 時系列。`metric` `from` `to` `agg` |
| GET | `/api/v1/stats` | min/max/mean/p95/傾き/欠測率 |
| GET | `/api/v1/alerts` | アラート一覧 |
| GET | `/api/v1/gpu/processes` | CUDA プロセス一覧と VRAM 使用量 |
| GET | `/api/v1/airflow/config` | エアフロー画面の表示設定（空気の温度の色分けの区切り）。測定値は含まない（#106 / 決定記録 0046） |
| GET | `/api/v1/devices` | 記録されたセンサー構成（チャネル / メトリクス / ROM）（#14） |
| GET | `/api/v1/events` | 記録された事象（GPU Mode の切り替え・Workload Hint）。タイムライン注釈用（#67 / #107） |
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
  "compute_mode_advisory": {
    "safe": true,
    "warnings": [],
    "blocking": false,
    "conditions": [
      {"metric": "air.room", "value": 26.4, "unit": "C", "quality": "ok", "age_seconds": 0.4,
       "usable": true, "advisory_max": 30.0, "exceeded": false, "reference_value": 26.1, "delta": 0.3},
      {"metric": "air.gpu_intake", "value": 28.3, "unit": "C", "quality": "ok", "age_seconds": 0.4,
       "usable": true, "advisory_max": 32.0, "exceeded": false, "reference_value": 28.0, "delta": 0.3},
      {"metric": "air.room_humidity", "value": 48.2, "unit": "%RH", "quality": "ok", "age_seconds": 0.4,
       "usable": true, "advisory_max": 65.0, "exceeded": false, "reference_value": 45.0, "delta": 3.2}
    ],
    "reference": {
      "started_at_ms": 1787529600000, "started_at": "2026-08-24T00:00:00+00:00",
      "ended_at_ms": 1787533200000, "ended_at": "2026-08-24T01:00:00+00:00",
      "covered_s": 3600.0, "bucket_count": 12,
      "load_metric": "power.gpu.0", "load_peak": 598.0, "load_mean": 571.0,
      "conditions": {"air.room": 26.1, "air.gpu_intake": 28.0, "air.room_humidity": 45.0},
      "peaks": {"gpu.0.core": 84.0}
    },
    "reference_count": 3,
    "reference_window_days": 30,
    "evaluated_at_ms": 1787615900000,
    "evaluated_at": "2026-08-24T23:58:20+00:00",
    "limitations": []
  }
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

Compute Mode へ切り替える**前に人が見る判断材料**です（#68 / [決定記録 0063](decisions/0063-compute-mode-advisory.md)）。
判定はすべて決定論的で、AI は関与しません。AI が停止していても同じ内容を返します。

**どんな条件でも切替をブロックしません。** `blocking` は型でも **常に false** です。
判断は人間が行います（決定 D-08）。将来もこのフィールドを true にする実装を
入れないでください。Workspace 側も、この payload を根拠に切替を拒否しないでください。

| フィールド | 意味 |
|---|---|
| `safe` | signal が green で、**かつ**すべての `conditions` が使えて助言上限の範囲内か。将来の高負荷時の安全は保証しません |
| `warnings` | 切替の判断に効く事実（情報源・アラート・条件）。`safe` が false の理由を含みます |
| `conditions` | いまの環境条件。`config/compute-mode-advisory.yaml` に並べた順で、**値が無くても件数は変わりません** |
| `reference` | 直近に**観測された**フルロード期間。無ければ `null`（理由は `limitations`） |
| `reference_count` | さかのぼり期間に見つかったフルロード期間の件数 |
| `reference_window_days` | さかのぼった日数 |
| `evaluated_at_ms` / `evaluated_at` | 履歴部分を評価した時刻。**`generated_at_ms` より古いことがあります** |
| `limitations` | **比較できなかったこと。** 空でなければ判断材料が欠けています |

`conditions` の1件は次の形です。`usable` は「`quality=ok` かつ設定の `max_age_s`
以内」で、false のものを現在の条件として表示しないでください。`exceeded` は使える値が
`advisory_max` を超えた場合だけ true です。`reference_value` / `delta` は
`reference` と比較できたときだけ入ります。

```json
{"metric": "air.room", "value": 31.2, "unit": "C", "quality": "ok", "age_seconds": 0.4,
 "usable": true, "advisory_max": 30.0, "exceeded": true, "reference_value": 26.1, "delta": 5.1}
```

**`reference` は観測された電力から導きます。** GPU Mode の申告
（`POST` 相当のイベント / `sys.gpu_mode`）は根拠にしません（[決定記録 0045](decisions/0045-local-socket-write-entry.md) §2.6）。
時刻と継続時間は根拠になったバケットの時刻から決まり、現在時刻からは作りません。
`covered_s` は**観測できたバケットの合計**で、欠落した時間を含みません。

**`reference` が `null` のときに「比較の結果、問題なし」と表示しないでください。**
比較していないことを画面に出してください。同じく `limitations` が空でないときは、
どの材料が欠けたかを人が読める形で出してください。

しきい値（`advisory_max`・フルロードとみなす電力・さかのぼり日数など）はすべて
`config/compute-mode-advisory.yaml` にあり、**現時点では暫定値**です
（実測は #50 / #19）。設定は `COLDAISLE_COMPUTE_MODE_ADVISORY` で差し替えられます。

### `GET /api/v1/health`

デーモンの稼働状況（FR-305）と、**値の出どころの種類**を返します。

```json
{
  "ok": true,
  "source": "serial",
  "telemetry_source": "hardware",
  "last_sample_at": "2026-09-20T00:00:00+00:00",
  "last_sample_ts_ms": 1789516800000,
  "data_age_seconds": 1.2,
  "stale": false,
  "metrics": 24,
  "missing_ratio_1h": 0.0,
  "queue_drops_1h": 0
}
```

**出どころは経路ごとに別のフィールドです。** 片方でもう片方を説明しないでください。

| フィールド | 経路 | 値 | 書くメトリクス |
|---|---|---|---|
| `source` | 取り込み（`coldaisle-daemon`。`sys.ingest_source`） | `serial` / `mock` / `replay` / `null` | 空気の温度・湿度（`air.*`） |
| `telemetry_source` | 内部テレメトリ（`coldaisle-telemetry`。`sys.telemetry_kind`） | `hardware` / `mock` / `null` | 回転数・PWM・CPU・GPU |

`telemetry_source` は**いまの種類**で、保存済みの値ごとの出どころではありません
（決定記録 0049）。`hardware` は「実 adapter が、設定された OS の読み取り口
（NVML / hwmon / `/proc/stat`）を読んでいる」という意味で、センサーと物理部位の対応が
正しいことまでは表しません（それは `/api/v1/devices` と hwmon の `confirmation`）。
記録の無い DB（種類を書かない版のデーモンで貯めたもの）では `null` です。

**`null` や知らない値を「実機」とみなさないでください。** また、`telemetry_source` が
`hardware` でも、**古い値・更新の止まったメトリクスまで実測とは言えません**。
DB を使い回して種類を切り替えると、もう書かれないメトリクスの古い行が `/latest` に
残るためです。画面は `/api/v1/latest` の `value` が非 `null` かつ `quality` が
`ok` / `suspect` の値（＝いま届いている値）にだけ札を付けます（決定記録 0049 §2.5）。

**Server Health（`/api/v1/server-health`）の `sources.*` とは別物です。** あちらは
adapter の稼働状態（読めたか）で、出どころの種類ではありません。

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

### `GET /api/v1/airflow/config`

coldaisle のエアフロー画面（`/airflow.html`、#106）が使う**表示設定**です。
`config/airflow-ui.yaml` をそのまま返します（決定記録 0046）。

```json
{
  "schema_version": 1,
  "air_temperature": {"unit": "C", "thresholds_c": [27.0, 28.0, 29.0, 30.0], "provisional": true},
  "cpu_utilization": {"measured": true}
}
```

`thresholds_c` は空気の温度を5段階（青→琥珀→赤）に色分けする境目で、狭義の昇順に
4つです。境目ちょうどの値は上の段に入ります。`provisional: true` の間は区切りが
仮の値であることを凡例に出してください。

**制御・アラートの閾値ではありません。** Fan 制御やアラートの判定には使われず、
変えても色の付き方が変わるだけです。測定値は含みません（`/latest` と `/series` を使う）。

`cpu_utilization.measured` は CPU 使用率（`cpu.utilization`。決定記録 0047 / 0051）を collector が
収集しているかで、collector が保存した `sys.telemetry_source.proc_stat` の直近の状態から決まります
（#145）。`disabled` → `false`、`ok` / `degraded` → `true`、`unavailable`・状態なし → `null`（分からない）。
`false` のとき画面は CPU 使用率を「未計測」と出します。無効にする前の行が `/latest` に残りうるため、
行の有無では判断しません。API 側の設定ファイルや OS からは決めません（collector は別の設定で動きうる）。

### `GET /api/v1/events`

GPU Mode の切り替え（AI / Compute）や Workload Hint など、外から通知された事象を
時刻順に返します（#67 / #107）。
温度・電力の `series` と同じ時間軸に重ねるための注釈です。
パラメータは `from` / `to` または `window`、`kind`（複数可。例 `kind=gpu_mode`・
`kind=workload_hint`）、`limit`（既定 500）。

```json
{
  "from": 1787612400000,
  "to": 1787616000000,
  "truncated": false,
  "events": [
    {"id": 12, "ts_ms": 1787614200000, "ts": "2026-08-24T23:30:00+00:00",
     "kind": "gpu_mode",
     "payload": {"v": 1, "type": "gpu_mode", "mode": "compute", "source": "workspace-gpu-manager"}},
    {"id": 13, "ts_ms": 1787614260000, "ts": "2026-08-24T23:31:00+00:00",
     "kind": "workload_hint",
     "payload": {"v": 1, "type": "workload_hint", "hint_v": 1, "phase": "start",
                 "workload": "training", "expected_duration_s": 14400,
                 "source": "workspace-job-launcher"}}
  ]
}
```

`kind` ごとの `payload`:

| `kind` | `payload` の中身 |
|---|---|
| `gpu_mode` | `mode`（`ai` / `compute`）、任意で `source` / `note` |
| `workload_hint` | `hint_v`（ヒント本体の版。いまは `1`）、`phase`（`start` / `end`）、`start` のときだけ `workload`（`training` / `benchmark` / `inference_service`）、任意で `expected_duration_s`（`start` のときだけ）/ `source` / `note`（決定記録 0064 §2.3） |

**Workload Hint は書き手の申告であって、測定値ではありません。** いまは記録するだけで
（決定記録 0064 §2.10 の Stage A）、制御は読みません。`server-health` などの「いまの値」にも
反映されません（0064 §2.4）。知らない `hint_v` の行は、推測で解釈せず「ヒント無し」として扱ってください。

**このエンドポイントは読み取り専用です。** 事象を書き込む入口は API ではなく、
別プロセスのローカル Unix ソケット（`coldaisle-eventd`、クライアントは
`coldaisle-event gpu-mode ai|compute` と
`coldaisle-event workload-hint training|benchmark|inference_service|end`）です（決定記録 0045 / 0064）。
Workspace の GPU Manager は HTTP ではなくこのソケットへ通知してください。
受理した GPU Mode は `server-health` の `gpu.mode` にも反映されます。

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
