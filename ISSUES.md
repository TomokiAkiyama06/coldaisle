# Issue 一覧

`issues/` 配下の個別ファイルが本体です。GitHubへの一括登録は `scripts/create_issues.sh` を使用してください。

各Issueのファイル冒頭にYAMLフロントマターで title / labels / milestone を記載しています。

> 本リポジトリの Issue は**ソフトウェアとファームウェアの実装**に限定します。
> 部品調達・組み立て・設置作業は管理対象外です。

## マイルストーン

| マイルストーン | 内容 | 実機依存 |
|---|---|---|
| M0 基盤 | リポジトリ・CI・規約・スキーマ確定 | なし |
| M1 データ基盤 | Mock/Replayソース・ストレージ・API | **なし** |
| M2 実機接続 | 本番ファーム・SerialSource・較正・長時間試験 | ESP32のみ |
| M3 UI | ダッシュボード | なし |
| M4 アラート | ルールエンジン・通知 | 閾値確定のみGPU必要 |
| M5 AI | Provider抽象・ツール・レポート | なし |
| M6 移行 | Ubuntu / systemd / udev / vLLM | **GPU機必要** |
| M7 拡張 | 内部センサー統合・Workspace統合 | **GPU機必要** |

## 一覧

| # | タイトル | マイルストーン | ラベル |
|---:|---|---|---|
| 1 | [リポジトリ雛形とツールチェーン整備](issues/01-repo-scaffold.md) | M0 基盤 | infra, priority:must |
| 2 | [GitHub Actions CI（実機なしで完走すること）](issues/02-ci-pipeline.md) | M0 基盤 | infra, priority:must |
| 3 | [ADR: メトリクス命名規約とDBスキーマ（ロング形式）の確定](issues/03-adr-metric-naming.md) | M0 基盤 | design, priority:must |
| 4 | [ADR: デバイス出力JSONスキーマ v1 の確定](issues/04-adr-device-json-schema.md) | M0 基盤 | design, firmware, priority:must |
| 5 | [コアデータモデルとSQLiteストレージ層](issues/05-core-models-storage.md) | M1 データ基盤 | core, priority:must |
| 6 | [MockSource: 合成データ生成器（実機なし開発の要）](issues/06-mock-source.md) | M1 データ基盤 | core, priority:must |
| 7 | [ReplaySource: 既存CSVのリプレイ](issues/07-replay-source.md) | M1 データ基盤 | core, priority:should |
| 8 | [ingest daemon 骨格（シリアルポートの単一所有者）](issues/08-ingest-daemon.md) | M1 データ基盤 | core, priority:must |
| 9 | [読み取り専用REST API + WebSocket](issues/09-read-api.md) | M1 データ基盤 | api, priority:must |
| 10 | [1分ロールアップとリテンション](issues/10-rollup-retention.md) | M1 データ基盤 | core, priority:must |
| 11 | [ESP32本番ファームウェア（JSON v1 / 非同期変換 / WDT）](issues/11-firmware-json-v1.md) | M2 実機接続 | firmware, priority:must |
| 12 | [SerialSource（自動検出・再接続・非JSON行の無視）](issues/12-serial-source.md) | M2 実機接続 | core, priority:must |
| 13 | [センサー較正手順と calibration.json](issues/13-calibration.md) | M2 実機接続 | hardware, priority:must |
| 14 | [DS18B20 ROM IDによるプローブ同定と入れ替わり検出](issues/14-probe-identity.md) | M2 実機接続 | core, priority:should |
| 15 | [24時間連続運転テストと欠測率の測定](issues/15-soak-test.md) | M2 実機接続 | qa, priority:must |
| 17 | [Webダッシュボード刷新（API経由化）](issues/17-dashboard.md) | M3 UI | ui, priority:must |
| 18 | [ルールエンジン（閾値・継続時間・ヒステリシス）](issues/18-rule-engine.md) | M4 アラート | core, priority:must, safety |
| 19 | [ベースライン測定と閾値の確定](issues/19-baseline-measurement.md) | M4 アラート | qa, priority:must, blocked-by-hardware |
| 20 | [Slack / LINE 通知](issues/20-notifications.md) | M4 アラート | integration, priority:should |
| 21 | [LLM Provider抽象（Ollama ⇄ vLLM 切替）](issues/21-llm-provider.md) | M5 AI | ai, priority:must |
| 22 | [ツール定義と実行ランタイム（読み取り専用）](issues/22-llm-tools.md) | M5 AI | ai, priority:must, safety |
| 23 | [チャットUI](issues/23-chat-ui.md) | M5 AI | ai, ui, priority:must |
| 24 | [アラート発生時のAI要約生成](issues/24-alert-explainer.md) | M5 AI | ai, priority:should |
| 25 | [日次レポート生成](issues/25-daily-report.md) | M5 AI | ai, priority:should |
| 26 | [Ubuntu移行（systemd / udev / 固定デバイス名）](issues/26-ubuntu-migration.md) | M6 移行 | infra, priority:must, blocked-by-hardware |
| 27 | [vLLM + Qwen3.8-27B の停止可能な GPU AI Service 構成](issues/27-vllm-deployment.md) | M6 移行 | ai, infra, priority:must, blocked-by-hardware |
| 28 | [GPU / CPU / VRM 内部センサーの統合](issues/28-internal-sensors.md) | M7 拡張 | core, priority:could, blocked-by-hardware |
| 29 | [Personal AI Workspace の Server Health 統合](issues/29-workspace-integration.md) | M7 拡張 | integration, priority:could |
| 30 | [【設計のみ】ファン制御の安全設計検討](issues/30-fan-control-design.md) | M7 拡張 | design, safety, priority:could |
| 31 | [ADR: ローカルモデルの役割分担を確定する](issues/31-adr-model-roles.md) **← 決定記録 0005 で解決。クローズ可** | M0 基盤 | design, ai, priority:must |
| 32 | [Core Service と GPU AI Service の分離（Compute Mode対応）](issues/32-core-gpu-service-split.md) | M6 移行 | infra, priority:must, safety |
| 33 | [Docker Compose による3層分離](issues/33-docker-compose-layers.md) | M6 移行 | infra, priority:should, blocked-by-hardware |
| 34 | [NVML / lm-sensors の統合（v1スコープへ格上げ）](issues/34-internal-sensors-nvml.md) | M7 拡張 | core, priority:must, blocked-by-hardware |
| 35 | [Server Health API（Workspace連携の単一窓口）](issues/35-server-health-api.md) | M7 拡張 | api, integration, priority:must |
| 36 | [GPU Mode イベントの記録とタイムライン注釈](issues/36-gpu-mode-events.md) | M7 拡張 | core, integration, priority:should |
| 37 | [Compute Mode 切替時の環境条件アドバイザリ](issues/37-compute-mode-advisory.md) | M7 拡張 | core, safety, priority:should |
| 38 | [アラート説明を Evidence 形式へ（#24 を全面改訂）](issues/38-evidence-based-alerts.md) | M5 AI | ai, priority:must |
| 39 | [ハードウェア故障疑い時の Claude エスカレーション](issues/39-claude-escalation.md) | M5 AI | ai, safety, priority:should |
| 40 | [Markdown Decision Memory への自動記録](issues/40-memory-writer.md) | M5 AI | integration, priority:should |
| 41 | [秘匿情報の混入防止（.env / トークン / 環境固有情報）](issues/41-public-repo-hygiene.md) | M0 基盤 | infra, priority:must, safety |

## 着手順の推奨

```text
最初に:      #41                       秘匿情報の混入防止（初回コミット前）
基盤:        #1 → #2 → #3 → #4 → #31   規約・CI・スキーマ・モデル役割
★最優先:     #5 → #6                   MockSourceが完成した瞬間に全レイヤが解禁される
データ基盤:  #8 → #9 → #10 → #17
ファーム:    #11 → #12 → #13 → #14 → #15
アラート:    #18 → #20                 （#19の閾値確定は実機到着後）
AI:          #21 → #22 → #24 → #25
実機到着後:  #26 → #27 → #19 → #34 → #35 → #36 → #37
```

**#6 (MockSource) が全体のクリティカルパスです。** これが完成すると、
GPU機どころか ESP32 すら接続せずに #8〜#25 のすべてが開発・テストできます。

## ラベル定義

| ラベル | 意味 |
|---|---|
| `priority:must` / `should` / `could` | MoSCoW |
| `blocked-by-hardware` | 実機がないと着手できない |
| `safety` | 安全性に関わる。人間のレビュー必須 |
| `infra` `core` `api` `ui` `ai` `firmware` `qa` `design` `integration` | 領域 |

