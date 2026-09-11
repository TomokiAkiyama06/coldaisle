# coldaisle

GPUサーバーの**温度監視・時系列ログ・安全なファン制御**を行うローカル運用ツールです。

XIAO ESP32-S3 + 外付け温度センサー、NVML、lm-sensors / hwmon を同じタイムラインへ統合し、
「どこが熱いか」「ケース換気が足りているか」「ファン制御が効いているか」を判断できるようにします。

## v1 の目的

v1 の主目的は次の4点です。

1. 外気・吸気・GPU周辺・排気・12V-2x6周辺温度を継続監視する
2. GPU / CPU / Fan RPM / PWM など内部Telemetryと合わせてSQLiteへ記録する
3. Web UIで現在値・履歴・温度差・センサー異常を確認する
4. **Front + Rearを主制御し、必要なときだけTop Radiator Fanを補助排気として上げる**

AI / LLM、Personal AI Workspace、Slack / LINE、日次AIレポート等は既存実装・将来拡張として残しますが、
**v1の冷却制御を完成させるための必須要件ではありません。**

## 現在の実機構成

### 外付けセンサーモジュール

- Seeed XIAO ESP32-S3
- DS18B20 ×5
  - Front Intake
  - GPU Intake
  - GPU Exhaust
  - Top Exhaust
  - Rear Exhaust
- AM2320 ×1
  - Room Temperature
  - Room Humidity
- ASUS `T_SENSOR`
  - 10kΩ NTCを12V-2x6コネクタ外装付近へ設置

固定BOMそのものは履歴化せず、DS18B20の **ROM ID → 設置位置**、較正値、配置変更、プローブ交換を管理します。

### Fan topology

- **Front Intake:** Noctua NF-A12x25 G2 ×3
- **Rear Exhaust:** Antec FLUX 純正Rear Fan ×1
- **Top Exhaust / CPU Radiator:** Cooler Master MasterLiquid Atmos II、120mm Fan ×3
- **AIO Pump:** BIOS / 安全設定で管理

Front + Rearは現在同一Fan Hub系統として扱います。
TopはCPU AIOのラジエーターファンでもあるため、通常のケース換気では主制御にせず、補助排気として扱います。

## Fan control policy

### Stage 1 — Front + Rear

通常時は **Front Intake ×3 + Rear Exhaust ×1** を主制御します。

主な入力:

- GPU Power（温度上昇前のfeed-forward）
- GPU core / hotspot
- Front Intake
- GPU Intake / GPU Exhaust
- Rear Exhaust
- `d.case_delta = rear_exhaust - front_intake`
- T_SENSOR
- Case Fan HubのPWM / RPM

### Stage 2 — Top auxiliary exhaust

Front + Rearを十分に上げてもケース内の熱を捌き切れない状態が一定時間続いた場合だけ、
**Top Radiator Fanを追加で上げて排気を補助**します。

TopはGPU負荷へ常時連動させません。
またTopはCPU冷却ファンでもあるため、coldaisleがCPU側の冷却要求を下げることは禁止します。
安全な仲裁方法を実機で確認できない場合、TopはBIOS管理のままとします。

AIO PumpとVRM Fanは初期版ではcoldaisleから制御しません。

## 風量の扱い

ファンの風量は**ファン径とRPMだけから絶対CFMを算出しません**。
同じ径・RPMでも羽根形状、静圧特性、フィルター、ラジエーター、ケース抵抗で実流量が変わるためです。

coldaisleでは次のように扱います。

- メーカー公称の最大風量・最大RPM・静圧は基礎情報として保持する
- 現在RPM / 最大RPM、PWM dutyを **Airflow Proxy（相対的な風量指標）** として利用する
- 最終判断は `d.case_delta`、GPU Intake、Rear / Top Exhaust、GPU温度・電力など、**実際に生じた熱応答**で行う
- Topはラジエーター抵抗があるため、Frontと同じRPM・公称風量でも同じ実流量とはみなさない

正確なCFM測定を制御の前提にはしません。

## 安全設計

冷却制御にAI / LLMを入れません。

| 層 | 責務 | AI |
|---|---|---|
| Safety-0 | BIOS Q-Fan / GPUサーマル保護 | なし |
| Safety-1 | `coldaisle-fand`、閾値、ヒステリシス、deadman、tach監視 | **なし** |
| Advisory | 説明・分析・将来のAI連携 | 読み取り専用のみ |

Fan daemonはGUIやAI層から分離し、NVML / T_SENSOR / 必須センサー欠測、tach異常、hwmon write失敗、heartbeat切れ等では安全側へ移行します。

## ハードウェアなしでの開発

データソースは `serial` / `mock` / `replay` で抽象化しています。
そのためダッシュボード、ルール、API、fan control state machineの多くは実機なしで開発・テストできます。

```bash
uv run coldaisle-daemon --source mock --scenario ramp
```

ただし、次は実機確認が必須です。

- DS18B20 / AM2320の較正・長時間運転
- hwmon上の物理Fan Header対応
- PWM最低値・起動値・tach特性
- BIOS Q-Fanとの仲裁 / 復帰挙動
- Front + Rearだけで不足する条件とTop補助開始条件

## 主な派生値

```text
d.intake_rise = air.front_intake - air.room
d.gpu_preheat = air.gpu_intake - air.front_intake
d.gpu_delta   = air.gpu_exhaust - air.gpu_intake
d.case_delta  = air.rear_exhaust - air.front_intake
```

特に `d.case_delta` は、ケース換気が熱を運び出せているかを見る主要指標として使います。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/requirements.md`](docs/requirements.md) | 詳細要件・既存設計 |
| [`docs/api-contract.md`](docs/api-contract.md) | Core API契約 |
| [`docs/spec-review.md`](docs/spec-review.md) | ハードウェア仕様レビュー |
| [`docs/decisions/`](docs/decisions/) | ADR / 決定記録 |
| [`ISSUES.md`](ISSUES.md) | Issue一覧・実機要件・着手順 |
| [`issues/`](issues/) | 個別Issue仕様の正本 |
| [`AGENTS.md`](AGENTS.md) | AIコーディングエージェント向け指示 |

## 現在の優先順

```text
ESP32 / センサー実機確認
  ↓
Ubuntu本番化
  ↓
NVML / lm-sensors / hwmon / T_SENSOR / Fan Telemetry
  ↓
実測ベースライン
  ↓
Fan control safety ADR
  ↓
Front + Rear主制御
  ↓ 必要時のみ
Top Radiator Fan補助排気
```

## 将来拡張

既存のLLM Provider、read-only tools、レポート、Workspace連携等は削除しませんが、
冷却制御v1とは分離して扱います。AI層がFan PWMを書き換える経路は作りません。

## ライセンス

Apache License 2.0

Copyright 2026 TomokiAkiyama06
