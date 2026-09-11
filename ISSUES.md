# Issue 一覧

`issues/` 配下の個別ファイルが仕様の正本です。GitHub Issue への同期は `scripts/create_issues.sh` を使用します。

GitHub の Issue / Pull Request は同じ番号空間を共有するため、**この文書の #番号は論理番号**です。同期スクリプトが本文中の依存関係を実際の GitHub Issue 番号へ変換します。

## 実機要件ラベル

Issue の「コードを書けるか」ではなく、**受入基準を完了するために何が必要か**で分類します。

| ラベル | 意味 |
|---|---|
| `requires:server` | GPUサーバー実機が完了条件に必要 |
| `requires:sensor-module` | XIAO ESP32-S3 + DS18B20×5 + AM2320 の自作センサーモジュールが必要 |
| `hardware-independent` | 上記実機なしで受入基準まで完了可能 |
| `needs-decision` | 実装前に設計判断が必要 |
| `blocked-by-design` | 安全設計/ADR承認まで実装開始しない |

`blocked-by-hardware` は旧ラベルです。サーバー到着待ちとセンサーモジュール待ちを区別できないため、新規には使いません。

## マイルストーン

| マイルストーン | 内容 |
|---|---|
| M0 基盤 | リポジトリ・CI・規約・スキーマ |
| M1 データ基盤 | Mock/Replay・Store・API |
| M2 実機接続 | ESP32ファーム・SerialSource・較正・Soak |
| M3 UI | 開発用ダッシュボード |
| M4 アラート | ルール・通知・実測閾値 |
| M5 AI | Provider・read-only tools・レポート |
| M6 移行 | Ubuntu / systemd / udev / vLLM |
| M7 拡張 | 内部Telemetry・Workspace・ファン制御 |

## 一覧

状態: ✅ completed / ⏳ open / ⛔ superseded

| 論理# | 状態 | タイトル | 実機要件 |
|---:|:---:|---|---|
| 1 | ✅ | [リポジトリ雛形とツールチェーン整備](issues/01-repo-scaffold.md) | なし |
| 2 | ✅ | [GitHub Actions CI](issues/02-ci-pipeline.md) | なし |
| 3 | ✅ | [ADR: メトリクス命名規約とDBスキーマ](issues/03-adr-metric-naming.md) | なし |
| 4 | ✅ | [ADR: デバイス出力JSONスキーマ v1](issues/04-adr-device-json-schema.md) | なし |
| 5 | ✅ | [コアデータモデルとSQLiteストレージ層](issues/05-core-models-storage.md) | なし |
| 6 | ✅ | [MockSource](issues/06-mock-source.md) | なし |
| 7 | ✅ | [ReplaySource](issues/07-replay-source.md) | なし |
| 8 | ✅ | [ingest daemon](issues/08-ingest-daemon.md) | なし |
| 9 | ✅ | [読み取り専用REST API + WebSocket](issues/09-read-api.md) | なし |
| 10 | ✅ | [1分ロールアップとリテンション](issues/10-rollup-retention.md) | なし |
| 11 | ⏳ | [ESP32本番ファームウェア](issues/11-firmware-json-v1.md) | センサーモジュール |
| 12 | ⏳ | [SerialSource](issues/12-serial-source.md) | センサーモジュール |
| 13 | ⏳ | [センサー較正](issues/13-calibration.md) | センサーモジュール |
| 14 | ✅ | [DS18B20 ROM IDプローブ同定](issues/14-probe-identity.md) | センサーモジュール |
| 15 | ⏳ | [24時間連続運転テスト](issues/15-soak-test.md) | センサーモジュール |
| 17 | ⏳ | [Webダッシュボード刷新](issues/17-dashboard.md) | なし |
| 18 | ✅ | [ルールエンジン](issues/18-rule-engine.md) | なし |
| 19 | ⏳ | [ベースライン測定と閾値確定](issues/19-baseline-measurement.md) | サーバー + センサーモジュール |
| 20 | ⏳ | [Slack / LINE通知](issues/20-notifications.md) | なし |
| 21 | ✅ | [LLM Provider抽象](issues/21-llm-provider.md) | なし |
| 22 | ✅ | [read-only LLM tools](issues/22-llm-tools.md) | なし |
| 23 | ✅ | [チャットUI / Workspace向けツール公開](issues/23-chat-ui.md) | なし |
| 24 | ⛔ | [旧AIアラート要約](issues/24-alert-explainer.md) | #38へ置換 |
| 25 | ✅ | [日次レポート生成](issues/25-daily-report.md) | なし |
| 26 | ⏳ | [Ubuntu移行](issues/26-ubuntu-migration.md) | サーバー + センサーモジュール |
| 27 | ⏳ | [vLLM GPU AI Service](issues/27-vllm-deployment.md) | サーバー |
| 28 | ⛔ | [旧内部センサー統合](issues/28-internal-sensors.md) | #34へ統合 |
| 29 | ⏳ | [Personal AI Workspace Server Health統合](issues/29-workspace-integration.md) | なし |
| 30 | ⏳ | [ファン制御の安全設計](issues/30-fan-control-design.md) | サーバー |
| 31 | ⛔ | [旧モデル役割分担ADR](issues/31-adr-model-roles.md) | 決定記録0005で解決 |
| 32 | ⏳ | [Core / GPU AI Service分離](issues/32-core-gpu-service-split.md) | サーバー |
| 33 | ⏳ | [Docker Compose 3層分離](issues/33-docker-compose-layers.md) | サーバー + センサーモジュール |
| 34 | ⏳ | [NVML / lm-sensors / hwmon内部Telemetry](issues/34-internal-sensors-nvml.md) | サーバー |
| 35 | ⏳ | [Server Health API](issues/35-server-health-api.md) | なし |
| 36 | ⏳ | [GPU Modeイベント](issues/36-gpu-mode-events.md) | なし / `needs-decision` |
| 37 | ⏳ | [Compute Mode環境条件アドバイザリ](issues/37-compute-mode-advisory.md) | サーバー + センサーモジュール |
| 38 | ✅ | [Evidence形式アラート説明](issues/38-evidence-based-alerts.md) | なし |
| 39 | ✅ | [Claudeエスカレーション](issues/39-claude-escalation.md) | なし |
| 40 | ✅ | [Markdown Decision Memory](issues/40-memory-writer.md) | なし |
| 41 | ✅ | [秘匿情報の混入防止](issues/41-public-repo-hygiene.md) | なし |
| 42 | ✅ | [Clock抽象](issues/42-clock-injection.md) | なし |
| 43 | ⏳ | [ケースFan Hub制御daemon](issues/43-fan-control-daemon.md) | サーバー / `blocked-by-design` |

## 現在の推奨着手順

基盤実装は既に大部分が完了しています。実機運用へ進む現在は、次の順を推奨します。

```text
センサーモジュール実機確認:
  #11 → #12 → #13 → #15

Ubuntu本番化:
  #26

内部Telemetry:
  #34
    ├─ NVML
    ├─ CPU / VRM / chipset
    ├─ T_SENSOR（12V-2x6外装）
    └─ Fan RPM / PWM

実測ベースライン:
  #19
    └─ BIOS/Q-Fan Auto vs ケースFan 100% 比較

ファン制御:
  #30（安全ADR）
    ↓ 人間レビュー承認
  #43（coldaisle-fand）

Workspace / Compute Mode:
  #35 → #29
  #36（要設計判断）
  #37
  #32 → #27 / #33
```

## ファン制御に関する現在の境界

初期版でソフトウェア制御するのは **Front + Rearを束ねるケースFan Hub 1系統だけ**です。
AIO Pump / Radiator Fan / VRM FanはBIOS管理を維持します。

ファン制御daemonはAI/LLMから隔離し、NVML・T_SENSOR・主要センサー欠測・tach異常・heartbeat切れ等では安全側へ倒す設計を #30 で先に確定します。
