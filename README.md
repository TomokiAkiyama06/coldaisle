# coldaisle

GPUサーバーの**温度監視・時系列ログ・安全なファン制御**を行うローカル運用ツールです。

XIAO ESP32-S3 + 外付け温度センサー、NVML、lm-sensors / hwmon を同じタイムラインへ統合し、
「どこが熱いか」「ケース換気が足りているか」「ファン制御が効いているか」を判断できるようにします。

## v1 の目的

v1 の主目的は次の4点です。

1. 外気・吸気・GPU周辺・排気・12V-2x6周辺温度を継続監視する
2. GPU / CPU / Fan RPM / PWM など内部Telemetryと合わせてSQLiteへ記録する
3. Web UIで現在値・履歴・温度差・センサー異常を確認する
4. **Front / Rear / Top の3系統を独立制御し、通常はFront + Rearで換気、必要時だけTopへケース排気要求を上乗せする**

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

v1では最初から3系統を独立制御します。

- **Front Intake zone:** Noctua NF-A12x25 G2 ×3
- **Rear Exhaust zone:** Antec FLUX 純正Rear Fan ×1
- **Top / CPU Radiator zone:** Cooler Master MasterLiquid Atmos II 360
- **AIO Pump:** BIOS / 固定安全設定で管理し、coldaisleからは制御しない

RearはFrontのHubから分離し、別のPWM headerへ接続する前提です。
Front / Rear / Topはそれぞれ別の `pwmX` / `fanX_input` として識別・制御します。

## Fan control policy

### Zone 1 — Front Intake

Frontは主吸気です。GPU powerをfeed-forwardとして使い、GPU Intake / Front Intake / GPU温度などをfeedbackとして必要な吸気量を決めます。

### Zone 2 — Rear Exhaust

Rearは主排気です。Frontとは別PWMで制御し、Frontを上げた際にRearだけを追加で上げることも可能にします。

これにより、Front 3基の吸気能力に対してRear 1基の排気が不足する場合でも、すぐTopへ頼らずRear側で先に調整できます。

### Zone 3 — Top / CPU Radiator

TopもcoldaisleがPWMを管理します。ただし役割は2つあります。

1. CPU AIOの冷却
2. Front + Rearだけでは不足した場合のケース補助排気

したがってTopの最終要求値は概念上、次で決めます。

```text
top_demand = max(cpu_cooling_demand, case_aux_exhaust_demand, safety_floor)
```

通常時の `case_aux_exhaust_demand` は低く保ち、Front + Rearで熱を捌き切れない状態が一定時間続いた場合だけ上げます。
CPU温度 / CPU telemetryが取得不能になった場合は、Topを安全側の高回転へ移行します。

## 風量設計

**ファン径 × RPMから絶対CFMを直接算出して制御しません。**

同じ径・RPMでも羽根形状、静圧特性、フィルター、ケース抵抗、ラジエーターで実流量が変わるためです。

v1では次の3段階で風量モデルを作ります。

### 1. Manufacturer prior

メーカー公称の最大RPM・最大風量・静圧を基礎データとして保持します。

既知の例:
- Front NF-A12x25 G2: 1基あたり最大1800 RPM / 63.15 CFM / 3.14 mmH2O
- Top Atmos II 360: 360mmユニット公称最大2500 RPM / 190 CFM / 3.61 mmH2O
- Rear FLUX純正Fan: 実機型番・最大RPMを確認し、公開仕様が無ければ実測中心で扱う

これらは**free-air / メーカー条件の参考値**であり、ケース装着時の実風量とはみなしません。

### 2. Per-zone characterization

Front / Rear / Topを個別にPWM sweepし、次を実測します。

- PWM duty → RPM
- 起動PWM
- 最低安定PWM / RPM
- 最大RPM
- 応答時間
- tachのばらつき

この結果から各zoneに0〜1の **Airflow Index** を作ります。Airflow Indexは相対指標であり、実測CFMを名乗りません。

### 3. Installed thermal effectiveness

固定したGPU / CPU熱負荷で各zoneを個別に変化させ、以下への影響を測ります。

- `d.case_delta`
- GPU Intake / GPU Exhaust
- Rear Exhaust / Top Exhaust
- GPU core / hotspot
- CPU temperature

これにより「Rearを10%上げる方が効くのか、Topを10%上げる方が効くのか」のような**実機上の冷却効果**をモデル化します。

最終的な制御は、公称CFMではなく **Airflow Index + 実際の熱応答**を優先します。

## 主な制御入力

- GPU Power
- GPU core / hotspot
- CPU temperature / CPU power
- Front Intake
- GPU Intake / GPU Exhaust
- Rear Exhaust / Top Exhaust
- `d.case_delta = rear_exhaust - front_intake`
- T_SENSOR
- Front / Rear / TopそれぞれのPWM / RPM / Airflow Index

## 安全設計

冷却制御にAI / LLMを入れません。

| 層 | 責務 | AI |
|---|---|---|
| Safety-0 | GPUサーマル保護 / CPUハードウェア保護 | なし |
| Safety-1 | `coldaisle-fand`、CPU/GPU制御則、ヒステリシス、deadman、tach監視 | **なし** |
| Advisory | 説明・分析・将来のAI連携 | 読み取り専用のみ |

Fan daemonはGUIやAI層から分離します。

重大なtelemetry / control faultでは、制御可能な正常Fanを安全側へ上げます。特にTopをアプリ管理するため、CPU温度取得不能・daemon再起動・PWM write失敗・Top tach異常の安全動作を実機で必ず検証します。

AIO Pumpはcoldaisleから制御せず、独立した安全設定を維持します。

## ハードウェアなしでの開発

データソースは `serial` / `mock` / `replay` で抽象化しています。
そのためダッシュボード、ルール、API、fan control state machineの多くは実機なしで開発・テストできます。

```bash
uv run coldaisle-daemon --source mock --scenario ramp
```

ただし、次は実機確認が必須です。

- DS18B20 / AM2320の較正・長時間運転
- Front / Rear / Topそれぞれの物理Fan Header対応
- PWM最低値・起動値・tach特性
- PWM→RPM characterization
- CPU冷却要求を含むTop制御の安全性
- Front / Rear / Topの実機上の冷却効果
- daemon停止 / 再起動時のFan挙動

## 主な派生値

```text
d.intake_rise = air.front_intake - air.room
d.gpu_preheat = air.gpu_intake - air.front_intake
d.gpu_delta   = air.gpu_exhaust - air.gpu_intake
d.case_delta  = air.rear_exhaust - air.front_intake
```

`d.case_delta`だけで風量を決めず、GPU Intake、CPU/GPU温度、Fan状態と組み合わせて評価します。

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
NVML / lm-sensors / hwmon / T_SENSOR / 3-zone Fan Telemetry
  ↓
3-zone Fan characterization / Airflow Model
  ↓
実測ベースライン
  ↓
Fan control safety ADR
  ↓
Front / Rear / Top 独立制御
```

## 将来拡張

既存のLLM Provider、read-only tools、レポート、Workspace連携等は削除しませんが、
冷却制御v1とは分離して扱います。AI層がFan PWMを書き換える経路は作りません。

## ライセンス

Apache License 2.0

Copyright 2026 TomokiAkiyama06
