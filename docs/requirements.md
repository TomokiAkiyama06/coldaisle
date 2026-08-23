# coldaisle 要件定義書

**バージョン**: 0.2 (Draft)
**作成日**: 2026-08-23
**対象**: GPUサーバー環境監視 + ローカルAI運用アシスタント
**前提ハードウェア**: NVIDIA RTX PRO 6000 Blackwell 96GB 搭載ワークステーション（導入前）

> 本書は `server_temperature_monitor_spec_2026-08-23.md`（ハードウェア仕様・アイデア段階）を
> ソフトウェア要件として再構成し、ローカルLLM活用を統合したものです。
>
> **v0.2 での重要な変更**: `personal_ai_workspace_design_memo_2026-08-12.md`（Workspace構想メモ）
> との突き合わせにより、本システムの位置づけを「独立アプリ」から
> **「Personal AI Workspace の Core Service」** へ改めました。
> Workspace との境界は `docs/api-contract.md` に定義しています。
>
> ハードウェア側の指摘・改訂提案は `docs/spec-review.md` を参照してください。

---

## 0. 用語

| 用語 | 定義 |
|---|---|
| デバイス | XIAO ESP32-S3 + DS18B20×5 + AM2320 で構成されるUSBセンサーユニット |
| サンプル | デバイスが1周期で送出する1行のJSON（全メトリクスのスナップショット） |
| メトリクス | 個々の測定値。`air.front_intake` のようにドット区切りで命名する |
| Source | サンプルの供給元。`serial` / `mock` / `replay` の3種 |
| ルールエンジン | 決定論的な閾値・継続時間判定によるアラート生成器（AI非依存） |
| AIレイヤ | ローカルLLMによる説明・診断・要約。**読み取り専用** |

---

## 1. 背景と課題

### 1.1 背景

- 高価なGPUワークステーションを導入するにあたり、熱環境の可視化が必要。
- マザーボード内蔵センサー（CPU/GPU/VRM）は「筐体内部の一点」しか見ておらず、
  **吸気温・室温・排気再循環** といった “設置環境起因の問題” を検出できない。
- ESP32-S3 + 外付けセンサーで「空気の温度」を測ることで、
  内蔵センサーだけでは分からない設置環境の問題を切り分けたい。

### 1.2 課題

1. サーバー未着のため、実機に依存する開発が進められない。
2. 現状の試作は「Pythonモニタ」と「Webダッシュボード」が**それぞれ独立にシリアルポートを開く**構成で、
   同時起動できない（仕様書 §11 に記載の制約はアーキテクチャ上の欠陥に起因）。
3. 取得したデータを人間が常時見張るのは非現実的。異常時のみ知りたい。
4. 蓄積したログを「先週と比べてGPU吸気温はどう変わった？」のように
   自然言語で問い合わせられると運用コストが下がる。

### 1.3 本プロジェクトのゴール

**「センサーデータを単一の信頼できるストアに集約し、決定論的ルールで異常を検知し、
ローカルLLMがその説明・要約・対話的な分析を担う運用アシスタントを構築する」**

---

## 2. スコープ

### 2.1 スコープ内（v1.0）

| # | 項目 |
|---|---|
| S-01 | ESP32-S3からのUSBシリアル取り込みデーモン（単一プロセスがポートを占有） |
| S-02 | Mock / Replay データソース（**ハードウェア無しで全機能を開発・テスト可能にする**） |
| S-03 | SQLiteへの時系列永続化 + 1分ロールアップ + CSVエクスポート |
| S-04 | 読み取り専用REST API + WebSocketライブ配信 |
| S-05 | Webダッシュボード（現在値・履歴グラフ・温度差・アラート一覧） |
| S-06 | 決定論的ルールエンジン（閾値 + 継続時間 + ヒステリシス） |
| S-07 | 通知（Slack / LINE。既存の個人自動化基盤へ接続） |
| S-08 | ローカルLLM（Qwen3-8B）による対話的分析・異常説明・日次レポート |
| S-09 | Mac（開発）→ Ubuntu（本番）への移行手順とsystemd/udev設定 |
| S-10 | **NVML / lm-sensors からのGPU・CPU内部センサー収集**（v1.1→v1へ格上げ） |
| S-11 | **Server Health API**（Personal AI Workspace のGPUパネルへ供給する単一エンドポイント） |
| S-12 | **GPU Mode イベントの記録とタイムライン注釈**、Compute Mode切替時の環境条件アドバイザリ |

### 2.2 スコープ外（v1.0では実装しない）

| # | 項目 | 理由 |
|---|---|---|
| N-01 | **ファン制御・自動シャットダウン等のアクチュエーション** | 安全系。BIOS Q-Fanを唯一の権威として残す。v2以降で別途安全設計 |
| N-02 | **LLMによる任意のコマンド実行・設定変更** | 8Bモデルに高額ハードの制御権を渡さない |
| N-03 | クラウド送信・外部公開 | ローカル完結。LAN内のみ |
| N-04 | 複数サーバーのマルチノード監視 | 1台前提。ただしスキーマは `node_id` を予約しておく |
| N-05 | モバイルアプリ | 通知はSlack/LINEで代替 |
| N-06 | ユーザー認証・マルチテナント | 単一ユーザー、127.0.0.1バインド |

### 2.3 段階的に取り込む（v1.1〜）

- 12V-2x6 コネクタ温度（専用センサーの追加が必要）
- `power.wall`（スマートプラグ経由の壁コンセント実測電力）
- Prometheus / Grafana 連携
- RAG（仕様書・作業ログの検索）

---

## 3. ユースケース

| ID | アクター | ユースケース | 優先度 |
|---|---|---|---|
| UC-01 | 運用者 | ブラウザで現在の室温・各部温度・温度差をリアルタイムに見る | Must |
| UC-02 | システム | 異常条件を検知し、Slack/LINEへ通知する | Must |
| UC-03 | 運用者 | 「今朝のGPU吸気温のピークは？」等を自然言語で問い合わせる | Must |
| UC-04 | システム | アラート発生時、LLMが状況・推定原因・確認手順を日本語で生成する | Should |
| UC-05 | 運用者 | 過去N日分の傾向を日次レポートとして受け取る | Should |
| UC-06 | 開発者 | **実機が無い状態で** Mock/Replayソースを使い全機能を開発・回帰テストする | Must |
| UC-07 | 運用者 | センサー故障（`-127.00` / `85.00` / 無応答）を明示的に知る | Must |
| UC-08 | 運用者 | 生ログをCSVで取り出し、Excel/pandasで分析する | Should |

---

## 4. アーキテクチャ

### 4.1 レイヤ構成

```text
┌───────────────────────────────────────────────────────────┐
│ L4  Presentation                                          │
│     Web Dashboard  /  Chat UI  /  Slack・LINE 通知        │
├───────────────────────────────────────────────────────────┤
│ L3  AI Layer  ★読み取り専用・提案のみ                     │
│     LLM Provider抽象 (Ollama ⇄ vLLM)                      │
│     Tool Runtime: query_metrics / list_alerts / get_stats │
├───────────────────────────────────────────────────────────┤
│ L2  Application                                           │
│     FastAPI (REST + WebSocket)                            │
│     Rule Engine（閾値・継続時間・ヒステリシス）★決定論的  │
├───────────────────────────────────────────────────────────┤
│ L1  Storage                                               │
│     SQLite (readings / readings_1m / alerts / devices)     │
│     CSV Exporter                                          │
├───────────────────────────────────────────────────────────┤
│ L0  Ingest  ★シリアルポートを占有する唯一のプロセス       │
│     Source(serial | mock | replay) → Normalizer → Store   │
└───────────────────────────────────────────────────────────┘
                          ▲ USB CDC
                   XIAO ESP32-S3
```

### 4.2 最重要の設計判断

#### D-01: シリアルポートの単一所有者

**現状の試作の問題**: `server_sensor_monitor.py` と `server_sensor_dashboard` が
それぞれ独立に `/dev/cu.usbmodem*` を開くため排他となる。

**決定**: シリアルを開くのは **ingest daemon のみ**。
モニタもダッシュボードもAIも、すべて Storage / API 経由で読む。
これにより「Arduino IDE Serial Monitorと排他」以外の排他問題は解消する。

#### D-02: Source抽象によるハードウェア非依存

```python
class Source(Protocol):
    def stream(self) -> Iterator[RawSample]: ...
```

| 実装 | 用途 |
|---|---|
| `MockSource` | サーバー未着の現在、**全レイヤの開発とテストを可能にする**。定常/負荷上昇/センサー故障/通信断のシナリオを合成生成 |
| `ReplaySource` | 既存の `~/server_sensor_logs/*.csv` を時間圧縮再生。回帰テストのゴールデンデータ |
| `SerialSource` | 実機。自動検出・再接続・非JSON行の無視 |

**これが「サーバーが届く前にできること」を最大化する中核。**
L1〜L4 は Source が何であるかを一切知らない。

#### D-03: AIを安全系に入れない（3層の責務分離）

| 層 | 責務 | AI関与 | 停止しても安全か |
|---|---|---|---|
| Safety-0 | BIOS Q-Fan / GPU自身のサーマルスロットリング | なし | — （最終防衛線） |
| Safety-1 | ルールエンジンによる検知・通知 | **なし** | Yes（通知が止まるだけ） |
| Advisory-2 | LLMによる説明・診断・要約・対話 | あり | Yes（説明が出ないだけ） |

**LLMは一切のアクチュエーションを持たない。** 出力は常に「人間への提案」で終わる。
将来ファン制御を実装する場合も、制御ロジックは Safety-1 に置き、AIは関与させない。

#### D-04: 時系列テーブルはロング形式

将来 CPU / GPU / VRM / 12V-2x6 を追加する際にスキーマ変更（ALTER TABLE）を発生させないため、
ワイド（列＝センサー）ではなくロング（行＝メトリクス）で保持する。

```sql
CREATE TABLE readings (
    ts_ms   INTEGER NOT NULL,          -- ホスト受信時刻 (Unix ms, UTC)
    metric  TEXT    NOT NULL,          -- 'air.front_intake'
    value   REAL,                      -- NULL 可（欠測）
    quality TEXT    NOT NULL,          -- ok | missing | suspect | stale
    PRIMARY KEY (ts_ms, metric)
) WITHOUT ROWID;
```

トレードオフ: 「同一時刻の全メトリクスを1行で」取り出すクエリが複雑になる。
→ ビュー `v_latest` とロールアップ `readings_1m` で吸収する。

#### D-05: デバイス時刻は信用しない

ESP32はWi-Fi/BluetoothもRTCも使わないため壁時計を持たない。
デバイスは `uptime_ms` と `seq` のみを送り、**タイムスタンプはホスト受信時に付与**する。
`seq` の飛びで取りこぼしを検出する。

#### D-06: Core Service と GPU AI Service を分離する ★v0.2で追加

Workspace構想メモの原則「Local AIは余剰GPU資源を使うバックグラウンドサービスであり、
本来のCompute Workloadが来たら即座に退く」に従う。

| 分類 | 構成要素 | GPU | Compute Mode（Kaggle実行中） |
|---|---|---|---|
| **Core Service** | ingest / store / rules / notifier / REST API / dashboard | **不要** | **稼働し続ける** |
| **GPU AI Service** | LLMによる説明・チャット・日次レポート | 必要 | **完全停止** |

**Compute Mode こそGPUが最も熱くなる時間帯であり、そこで監視が止まる設計はあり得ない。**
逆に、その間AI説明が使えないことは一切問題にならない。

FR-507（LLM不達時もダッシュボードとルールエンジンは正常動作）が
この分割を設計として保証している。デプロイ単位を分けるだけでよい。

#### D-07: Server Health を単一の情報源にする ★v0.2で追加

Workspace の GPU パネルが独自に `nvidia-smi` を叩く構成にしない。
物理環境（`air.*`）とGPU内部（`gpu.*`）が**同じタイムラインに並んでいなければ**、
「GPU温度が高いのは吸気が熱いからか、負荷が高いからか、冷却が効いていないからか」を切り分けられない。

coldaisle が NVML / lm-sensors も収集し、`GET /api/v1/server-health` を唯一の窓口とする。

---

## 5. データ仕様

### 5.1 メトリクス命名規約

`<domain>.<name>` 形式。将来の拡張を見据えて namespace を切る。

| メトリクス | 単位 | 由来 | v1 |
|---|---|---|---|
| `air.room` | °C | AM2320 | ✅ |
| `air.room_humidity` | %RH | AM2320 | ✅ |
| `air.front_intake` | °C | DS18B20 #1 | ✅ |
| `air.gpu_intake` | °C | DS18B20 #2 | ✅ |
| `air.gpu_exhaust` | °C | DS18B20 #3 | ✅ |
| `air.top_exhaust` | °C | DS18B20 #4 | ✅ |
| `air.rear_exhaust` | °C | DS18B20 #5 | ✅ |
| `gpu.0.core` / `gpu.0.hotspot` / `gpu.0.mem` | °C | NVML | ✅ |
| `gpu.0.vram_used` | GB | NVML | ✅ |
| `cpu.package` / `cpu.vrm` / `chipset` | °C | lm-sensors | ✅ |
| `power.gpu.0` | W | NVML | ✅ |
| `sys.gpu_mode` | enum | Workspace GPU Manager | ✅ |
| `sys.cuda_processes` | count | NVML | ✅ |
| `power.wall` | W | スマートプラグ | v1.1 |

派生メトリクス（保存せず計算で出す）:

| 名前 | 式 | 意味 |
|---|---|---|
| `d.intake_rise` | `air.front_intake - air.room` | **排気再循環の指標**。大きい＝自分の排気を吸っている |
| `d.gpu_preheat` | `air.gpu_intake - air.front_intake` | ケース内での予熱量 |
| `d.gpu_delta` | `air.gpu_exhaust - air.gpu_intake` | GPUが空気に与えた熱。風量低下で増大 |
| `d.top_rise` | `air.top_exhaust - air.gpu_exhaust` | **GPU排気がトップラジエーターへ回り込んでいないかの指標**（spec-review I-04） |
| `d.gpu_internal_delta` | `gpu.0.hotspot - gpu.0.core` | サーマルインターフェース劣化の指標 |

### 5.2 デバイス出力 JSON スキーマ v1

元仕様（§10）から拡張。**理由は各行に記載**。

**通常サンプル**

```json
{"v":1,"type":"s","seq":1042,"up":2605250,"room_temp":26.42,"room_humidity":48.20,"front_intake":27.87,"gpu_intake":28.25,"gpu_exhaust":28.87,"top_exhaust":27.06,"rear_exhaust":null,"err":["rear_exhaust:-127"]}
```

| 追加フィールド | 理由 |
|---|---|
| `v` | スキーマバージョン。ファーム更新時にホスト側が誤解釈しない |
| `type` | `s`=sample / `hello`=起動バナー / `log`=ログ。将来の拡張余地 |
| `seq` | 単調増加。**取りこぼし検出**（USB CDCでも詰まりは起きる） |
| `up` | デバイス稼働ms。**予期せぬ再起動の検出**（値が巻き戻る＝リセット） |
| `err` | どのセンサーがなぜNULLかを機械可読で伝える |

**起動バナー（type: hello）** — 電源投入時に1回

```json
{"v":1,"type":"hello","fw":"1.0.0","dev":"xiao-esp32s3","interval_ms":2500,
 "sensors":{"front_intake":{"kind":"ds18b20","gpio":1,"rom":"28FFFFFFFFFFFF01","res":11}}}
```

`rom`（DS18B20の64bit ROM ID）を記録することで、**プローブを差し替えた／位置を入れ替えた**
ことをホスト側が検出できる。仕様書 §15 の「センサー位置の最終固定」を運用で担保する仕組み。

> 上記の `rom` はダミー値です。**実機の ROM ID はドキュメントにもコードにも書かないでください**（#41）。
> 保存先は #3 / #14 で決めます。

### 5.3 品質フラグの判定規則

| 条件 | quality | 備考 |
|---|---|---|
| 正常値 | `ok` | |
| JSONで `null` | `missing` | |
| DS18B20が `-127.00` | `suspect` | 未認識／配線不良（仕様書 §13 に記載） |
| DS18B20が **ちょうど `85.00`** | `suspect` | **DS18B20のパワーオンリセット値**。変換完了前に読むと出る典型的な罠 |
| 前サンプルから10秒以上更新なし | `stale` | |

> `85.00` の扱いは元仕様に記載がありません。実装時に必ず入れてください。

---

## 6. 機能要件

### 6.1 FR-1xx: 取り込み

| ID | 要件 | 優先度 |
|---|---|---|
| FR-101 | `serial` / `mock` / `replay` の3ソースを設定で切り替えられる | Must |
| FR-102 | シリアルポートを自動検出する（macOS: `/dev/cu.usbmodem*`, Linux: `/dev/ttyACM*` または `/dev/server-sensors`） | Must |
| FR-103 | **JSONとして解釈できない行は破棄する**（ESP32のブートログ混入対策） | Must |
| FR-104 | USB切断時、指数バックオフで再接続を試行し、その間 `SENSOR_FAULT` を継続報告する | Must |
| FR-105 | `seq` の不連続を検出し、欠損数をメトリクスとして記録する | Should |
| FR-106 | `up` の巻き戻りを検出しデバイス再起動としてイベント記録する | Should |
| FR-107 | 較正オフセット（`calibration.json`）を取り込み時に適用する | Must |

### 6.2 FR-2xx: 永続化

| ID | 要件 | 優先度 |
|---|---|---|
| FR-201 | 全サンプルを SQLite `readings` に保存する | Must |
| FR-202 | 1分粒度で min/max/mean/count を `readings_1m` にロールアップする | Must |
| FR-203 | 生データの保持期間を設定可能にし、期間超過分を削除する（既定14日） | Should |
| FR-204 | ロールアップは無期限保持する | Should |
| FR-205 | 日付単位のCSVを従来どおり `~/server_sensor_logs/sensors_YYYY-MM-DD.csv` に出力する | Should |

**容量見積**: 2.5秒周期 × 7メトリクス = 約 2.4M行/日。
`WITHOUT ROWID` + INTEGER/REAL で概ね 60〜80MB/日。14日保持で約1GB。
→ ロールアップ後の削除は必須。

### 6.3 FR-3xx: API

| ID | エンドポイント | 内容 |
|---|---|---|
| FR-301 | `GET /api/v1/latest` | 全メトリクスの最新値 + 派生値 + quality |
| FR-302 | `GET /api/v1/series?metric=&from=&to=&agg=` | 時系列。`agg` = raw/1m/5m/1h |
| FR-303 | `GET /api/v1/stats?metric=&window=` | min/max/mean/p95/傾き |
| FR-304 | `GET /api/v1/alerts?state=&from=&to=` | アラート一覧 |
| FR-305 | `GET /api/v1/health` | デーモン稼働・最終受信時刻・ソース種別 |
| FR-306 | `WS  /api/v1/stream` | 新サンプルをpush |
| FR-308 | `GET /api/v1/server-health` | **Workspace GPUパネル向けの統合ビュー**。signal / summary / gpu / environment / alerts / sources / compute_mode_advisory（スキーマは `docs/api-contract.md`） |
| FR-309 | `POST /api/v1/events` | **唯一の書き込み系**。Workspace の GPU Manager が Mode 変更を通知するためだけに存在する（localhost限定） |
| FR-307 | 上記 FR-309 を除き、全エンドポイントは **読み取り専用** |

### 6.4 FR-4xx: ルールエンジン

状態機械: `OK → PENDING（条件成立、継続時間未達） → FIRING → RESOLVED`
ヒステリシス必須（閾値ちょうどでのフラッピング防止）。

| ID | ルール | 条件（暫定値） | 重大度 |
|---|---|---|---|
| FR-401 | `SENSOR_FAULT` | 30秒以上サンプル無し | critical |
| FR-402 | `SENSOR_MISSING` | 特定メトリクスが5サンプル連続でnull/suspect | warning |
| FR-403 | `PROBE_CHANGED` | hello の `rom` が前回起動時と異なる | info |
| FR-404 | `RECIRCULATION` | `d.intake_rise > 5.0°C` が5分継続 | warning |
| FR-405 | `INTAKE_HIGH` | `air.gpu_intake > 40°C` が2分継続 | warning |
| FR-406 | `AIRFLOW_DEGRADED` | `d.gpu_delta > 20°C` が5分継続 | warning |
| FR-407 | `RAPID_RISE` | `air.gpu_intake` の上昇率が 5°C/分 超 | critical |
| FR-408 | `ROOM_HIGH` | `air.room > 30°C` が10分継続 | warning |
| FR-409 | `HUMIDITY_OUT_OF_RANGE` | `air.room_humidity` が 20%未満 or 70%超 が10分継続 | warning |

> **閾値はすべて暫定です。** 実機到着後、無負荷・軽負荷・フルロードの
> ベースライン測定（Issue参照）を経て確定します。設定ファイル `rules.yaml` で外出しすること。

湿度を監視する理由: 低湿は静電気、高湿は結露リスク。どちらも高額GPUの実害要因。

### 6.5 FR-5xx: AIレイヤ

| ID | 要件 | 優先度 |
|---|---|---|
| FR-501 | OpenAI互換APIを話すProvider抽象。`base_url` 差し替えで Ollama ⇄ vLLM を切替 | Must |
| FR-502 | ツール呼び出しで `get_latest` / `query_series` / `get_stats` / `list_alerts` / `describe_system` を提供 | Must |
| FR-503 | **ツールはすべて読み取り専用**。書き込み・実行系ツールをv1では定義しない | Must |
| FR-504 | **生の時系列をプロンプトに直接投入しない。** ツール側で集計済みの統計量に落として渡す | Must |
| FR-505 | アラート発生時、状況要約・推定原因・確認手順を日本語で生成する | Should |
| FR-506 | 日次レポート（最高/最低/平均、アラート件数、前日比）を生成する | Should |
| FR-507 | LLM不達時もダッシュボードとルールエンジンは正常動作する（グレースフルデグレード） | Must |

| FR-508 | **アラート説明は Evidence 形式（VERIFIED / UNVERIFIED）で出力する。** confidence値を出さない | Must |
| FR-509 | ハードウェア故障が疑われる場合、圧縮した案件資料を生成し Claude へエスカレーションできる | Should |
| FR-510 | 較正値・閾値の変更を Markdown Decision Memory へ追記する（Supersedes付き） | Should |

**FR-508の理由**: 構想メモ §5.1/§8 の中核原則「小型LLMの自信度は信用しない。
Evidence over Confidence」に従います。「確度87%」には根拠がありません。
**何が測定値で何が推測かを読み手に明示する**ことが決定的に重要です。詳細は
出力例は `docs/api-contract.md` を参照してください。

**FR-504の理由**: 8Bクラスのモデルは長大な数値テーブルに対する算術・比較が不安定です。
「2万行のCSVを渡して最大値を聞く」は誤答します。
`get_stats` がSQLで min/max/mean/p95 を計算し、モデルには**言語化だけをさせる**設計にします。

---

## 7. ローカルLLM構成

### 7.1 モデルの役割分担 ★v0.2で全面改訂

Workspace構想メモは主力ローカルモデルを **GPT-OSS-120B** としており、
今回ご指示の **Qwen3-8B** と食い違っています。構想メモ §5.2 の
「小型モデルには特徴量抽出だけさせる」という設計を踏まえ、以下の分担を提案します。

| モデル | 役割 | VRAM目安 |
|---|---|---|
| **Qwen3-8B** | ①Router（タスク特徴量抽出）②**本システムの監視アシスタント**（説明・日次レポート） | 約16GB (BF16) / 約6GB (FP8) |
| **GPT-OSS-120B** | Workspace の主力Worker。本システムでは複数センサーの相関分析など重い解析のみ | 60GB前後 |
| **Claude** | エスカレーション先。ハードウェア故障疑い・高額機材の保護判断・初見の異常パターン |  — |

**温度監視の説明役に120Bは不要です。** `get_stats` が集計済みの数値を渡し、
モデルは日本語化するだけなので8Bで十分であり、Compute Mode切替時に速く降ろせる利点もあります。

> **これは提案です。** 構想メモ（2026-08-12）とご指示（2026-08-23）のどちらが最新方針かを
> 確認してください（未決事項 Q-08）。

**Qwen3-8B を採用する場合の理由:**

- Apache-2.0 ライセンスで商用利用可
- 思考モード / 非思考モードの切替が可能。
  日常のQ&Aは非思考で低レイテンシ、異常診断時は思考モードで品質重視、という使い分けができる
- ツール呼び出しに対応
- 日本語対応

> 補足: 現在は Qwen3.5 系（MoEを含む）も出ています。RTX PRO 6000 の 96GB VRAM は
> 8B には過剰なので、運用が安定したら 30B級MoE への差し替えを検討する価値があります。
> **FR-501 のProvider抽象を入れておけば、モデル差し替えは設定変更だけで済みます。**

### 7.2 実行環境（フェーズ別）

| フェーズ | ホスト | ランタイム | 形式 |
|---|---|---|---|
| 現在（サーバー未着） | Mac | **Ollama** または LM Studio / MLX | Q4_K_M 量子化（約5GB） |
| 本番 | Ubuntu + RTX PRO 6000 | **vLLM** | BF16 または FP8 |

Ollama も vLLM も OpenAI互換の `/v1/chat/completions` を提供するため、
アプリ側は `OPENAI_BASE_URL` と `MODEL_NAME` の2つを環境変数で切り替えるだけで済みます。

**vLLM起動例（本番）**

```bash
vllm serve Qwen/Qwen3-8B \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.25 \
  --port 8001
```

`--gpu-memory-utilization 0.25` で上限を切ります。ただし **v0.2 での重要な変更点** として、
これは「常駐させてよい」という意味ではありません。

**vLLM は停止可能な GPU AI Service として構成します（D-06）。**
Kaggle・研究でGPUを使う際は `--gpu-memory-utilization` による制限ではなく、
**プロセスを完全停止してVRAMを全解放**します。構想メモ §40 の
「復帰速度より、VRAMを確実に空けることを優先」に従います。

つまり:

| GPU Mode | Core Service（取り込み・アラート・API・UI） | GPU AI Service（LLM説明・チャット） |
|---|---|---|
| AI | 稼働 | 稼働 |
| **Compute（Kaggle）** | **稼働** | **完全停止** |

停止判定は「Dockerを止めた」ではなく **NVMLでCUDAプロセス数が0になったこと**で行います
（構想メモ §39）。

> ツールコールパーサはvLLM/モデルのバージョンで推奨値が変わります
> （Qwen3系は `hermes`、Coder系は `qwen3_xml` など）。
> 導入時に `vllm serve --help` と当該バージョンのドキュメントで確認し、
> スモークテスト（後述のIssue）を必ず通してください。

### 7.3 ツール定義（v1）

| ツール | 引数 | 戻り値 |
|---|---|---|
| `get_latest` | なし | 全メトリクスの現在値・派生値・quality |
| `query_series` | `metric, from, to, agg` | 最大200点にダウンサンプルした系列 |
| `get_stats` | `metric, window` | min/max/mean/p95/傾き/欠測率 |
| `list_alerts` | `from, to, state` | アラート履歴 |
| `describe_system` | なし | センサー配置・GPIO割当・閾値設定の要約 |

### 7.4 プロンプトインジェクション

数値データが主なので攻撃面は小さいですが、`err` フィールドやログ文字列は
デバイス由来の文字列としてモデルに渡ります。以下を守ること:

- デバイス由来文字列は「データ」として明示的にタグで囲む
- モデル出力を **eval / シェル実行 / SQL に流さない**（v1は読み取り専用ツールのみなので構造的に不可）
- ダッシュボードでのモデル出力は HTML エスケープする

---

## 8. 非機能要件

| ID | 分類 | 要件 |
|---|---|---|
| NFR-01 | 可用性 | ingest daemon は launchd(Mac) / systemd(Ubuntu) で自動再起動。クラッシュ後10秒以内に復帰 |
| NFR-02 | データ完全性 | 欠測率 1%未満（24時間連続運転で計測） |
| NFR-03 | レイテンシ | ダッシュボードの現在値表示は最新サンプルから3秒以内 |
| NFR-04 | レイテンシ | LLM初回トークン: vLLM環境で2秒以内 / Ollama環境では要件外 |
| NFR-05 | リソース | ingest daemon の常駐メモリ 100MB未満 |
| NFR-06 | セキュリティ | 既定で `127.0.0.1` バインド。LAN公開する場合は明示設定 + Basic認証（v1.1） |
| NFR-07 | 移植性 | macOS / Ubuntu 双方で動作。OS依存はシリアルデバイス検出のみに閉じる |
| NFR-08 | 保守性 | テストカバレッジ 70%以上。Mock/Replayソースにより実機なしでCIが完走する |
| NFR-09 | 可観測性 | 構造化ログ（JSON Lines）。デーモン自身のヘルスも `/api/v1/health` で公開 |

---

## 9. 技術スタック

| レイヤ | 採用 | 備考 |
|---|---|---|
| 言語 | Python 3.12+ | 既存資産（`server_sensor_monitor.py`）を活かす |
| パッケージ管理 | uv | 既存仕様書どおり |
| Web | FastAPI + Uvicorn | 既存仕様書どおり |
| シリアル | pySerial | |
| DB | SQLite（標準ライブラリ） | 単一ファイル。将来Prometheus併設可 |
| フロント | Vanilla JS + Chart.js | **開発用・フォールバック用のみ。** 本番UIは Workspace 側（Tauri + React + TS）。詳細は `docs/api-contract.md` |
| LLM | Qwen3-8B（Ollama → vLLM） | OpenAI互換API |
| Lint/Format | ruff | |
| 型 | mypy（strict） | |
| テスト | pytest | |
| CI | GitHub Actions | 実機不要で完走すること |
| ファーム | Arduino IDE / arduino-cli | 既存仕様書どおり |

---

## 10. フェーズ計画

| マイルストーン | 内容 | 実機依存 |
|---|---|---|
| **M0 基盤** | リポジトリ・CI・規約・スキーマ確定 | なし |
| **M1 データ基盤** | Mock/Replayソース・ストレージ・API | **なし** ← 今すぐ着手可 |
| **M2 実機接続** | 本番ファーム・SerialSource・較正・長時間試験 | ESP32のみ（**手元にあるので着手可**） |
| **M3 UI** | ダッシュボード刷新（API経由化） | なし |
| **M4 アラート** | ルールエンジン・通知 | なし |
| **M5 AI** | Provider抽象・ツール・チャット・レポート | なし（Mac + Ollama） |
| **M6 移行** | Ubuntu / systemd / udev / vLLM | **GPUサーバー必要** |
| **M7 拡張** | GPU/CPU/VRM統合・Workspace統合 | **GPUサーバー必要** |

**M6以外はすべてサーバー到着前に完了できます。**
到着後にやるのは「Sourceの向き先を変える」「vLLMを立てる」「閾値を実測で確定する」の3点だけ、
という状態を目指します。

---

## 11. リスクと対策

| ID | リスク | 影響 | 対策 |
|---|---|---|---|
| R-01 | DS18B20の12bit変換が750ms × 5本で送信周期2.5秒に間に合わない | 周期崩壊 | 非同期変換で5本同時開始 + 分解能を11bitへ（詳細は spec-review.md） |
| R-02 | センサー個体差（±0.5°C）でΔT判定が誤動作 | 誤警報 | 較正手順を設け `calibration.json` でオフセット補正 |
| R-03 | AM2320のコネクタ誤挿入による発熱（**再発の恐れあり**） | 部品破損・発火 | キー付きコネクタ必須化。手順書に「通電中の抜き差し禁止」を明記 |
| R-04 | 閾値を実測なしで決めるとアラートが機能しない | 形骸化 | ベースライン測定Issueを必須の前提タスクにする |
| R-05 | LLMが誤った診断を出し、それを信じて誤操作する | 運用事故 | 出力に必ず根拠データを併記。制御権限を与えない（D-03） |
| R-06 | 開発が実機待ちで停滞する | 納期 | Mock/Replayソースを最優先で実装（D-02） |
| R-07 | AIエージェント（Claude Code / Codex）併用でのコード衝突 | 手戻り | git worktree + Issue単位ブランチ。AGENTS.md を単一の正本に |

---

## 12. 決定事項と未決事項

決定の詳細と根拠は [`docs/decisions/`](decisions/) を参照してください。
本表は**ソフトウェア設計上の判断**のみを扱います。

### 確定済み

| ID | 論点 | 決定 | 確定日 |
|---|---|---|---|
| Q-01 | 温湿度センサー | AM2320 を継続使用 | 2026-08-23 |
| Q-02 | 通知先の振り分け | 重大度で分岐（critical / warning で別経路） | 2026-08-23 |
| Q-03 | AIチャットUIの置き場所 | **実装しない。** Workspace のチャットへ統合 | 2026-08-23 |
| Q-04 | LLMの常駐方針 | 常駐を前提にしない。Compute Mode で完全停止 | 2026-08-23 |
| Q-05 | 生データ保持期間 | **30日** + 月次閲覧用に1時間ロールアップを追加 | 2026-08-23 |
| Q-06 | リポジトリ公開範囲 | public | 2026-08-23 |
| Q-07 | Prometheus / Grafana | 不採用 | 2026-08-23 |
| Q-08 | ローカルモデルの役割分担 | Qwen3-8B を主軸 + GPT-OSS-120B を併用 | 2026-08-23 |
| Q-09 | Workspace と同一リポジトリにするか | 別リポジトリ。UI 上は Workspace へ内蔵表示 | 2026-08-23 |
| Q-10 | NVML を叩く主体 | Telemetry Collector が唯一の主体 | 2026-08-23 |
| Q-11 | Compute Mode advisory の扱い | 警告のみ。ブロックしない | 2026-08-23 |
| Q-12 | Memory への自動書き込み | 追加は自動 / 上書き・削除は承認制 | 2026-08-23 |
| Q-13 | 利用者 | 単一利用者 → 127.0.0.1 バインド、認証不要 | 2026-08-23 |
| Q-14 | 夜間通知 | 常時通知。ただし閾値確定までは critical のみに段階制限 | 2026-08-23 |
| Q-15 | 通知先 | Slack / LINE ともに専用の宛先を使用 | 2026-08-23 |

### 検証済み

| ID | 検証項目 | 結果 |
|---|---|---|
| V-01 | D2 / GPIO3（ストラッピングピン）での起動・再起動・書き込み | ✅ 問題なし。現行配線を維持 |

### 未決

| ID | 論点 | 判断の時期 |
|---|---|---|
| Q-16 | ライセンス（Apache-2.0 を推奨） | 公開前 |

| Q-18 | センサー最終配置 | 実機で構成を確認後 |

---

## 13. 完了の定義（v1.0）

- [ ] 実機なし（Mockソース）でCIが完走し、カバレッジ70%以上
- [ ] 実機で24時間連続運転し、欠測率1%未満
- [ ] 7メトリクスすべてがダッシュボードにリアルタイム表示される
- [ ] 意図的にセンサーを1本外すと `SENSOR_MISSING` が発火し通知が届く
- [ ] 「昨日のGPU吸気温の最高値は？」に対しLLMが正しい数値を根拠付きで答える
- [ ] Ubuntu上で systemd により自動起動し、`/dev/server-sensors` で固定名解決される
- [ ] 日次レポートが自動生成される
