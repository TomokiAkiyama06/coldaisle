---
title: "NVML / lm-sensors / hwmon の内部Telemetry統合"
labels: core, priority:must
milestone: "M7 拡張"
---

## 背景
#28 を格上げして本Issueに統合する。
GPUサーバー実機が到着したため、現在は着手可能。

外付け空気センサー（`air.*`）と、GPU/CPU/VRM/T_SENSOR/ファンの内部Telemetryを同じタイムラインへ載せ、
「吸気が熱いのか」「GPU自体が熱いのか」「ケース換気が不足しているのか」「ファン指令が反映されていないのか」を切り分けられるようにする。

Fan control v1では、**Front + Rearを主制御し、Top Radiator Fanは不足時のみ補助排気**として扱うため、両系統を区別してTelemetry化する。

## やること

### NVML
- [ ] NVMLから収集:
  - `gpu.0.core`
  - `gpu.0.hotspot`（取得可能な場合）
  - `gpu.0.mem`
  - `gpu.0.vram_used`
  - `power.gpu.0`
  - `sys.cuda_processes`
- [ ] `nvidia-smi` を高頻度pollingでspawnせず、NVMLを直接利用

### lm-sensors / hwmon
- [ ] CPU Package温度
- [ ] CPU power（取得可能な場合）
- [ ] VRM温度
- [ ] chipset温度
- [ ] **ASUS T_SENSOR**（12V-2x6コネクタ外装に設置した10kΩ NTC）

### Fan Telemetry
- [ ] Front + Rear Fan HubのRPM
- [ ] Front + Rear Fan HubのPWM duty / control mode（取得可能な範囲）
- [ ] Top Radiator FanのRPM
- [ ] Top Radiator FanのPWM duty / control mode（取得可能な範囲）
- [ ] AIO Pump RPM
- [ ] VRM Fan RPM（取得可能な場合）
- [ ] Front + Rear系統とTop系統の物理header対応を特定
- [ ] hwmon番号を固定せず、driver名・label・安定属性から探索

現在のFan topology:
- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top Exhaust / CPU Radiator: Cooler Master MasterLiquid Atmos II、120mm Fan ×3

### メトリクス・派生値
- [ ] 同一`readings`テーブルへ投入（ロング形式のためスキーマ変更不要）
- [ ] `d.gpu_internal_delta = gpu.0.hotspot - gpu.0.core`
- [ ] `d.case_delta = air.rear_exhaust - air.front_intake`
- [ ] `d.gpu_preheat = air.gpu_intake - air.front_intake` を継続利用
- [ ] `d.gpu_delta = air.gpu_exhaust - air.gpu_intake` を継続利用
- [ ] `d.rear_rise = air.rear_exhaust - air.room` を継続利用
- [ ] 12V-2x6温度はRoomとの差分も表示可能にする
- [ ] Fanごとに `rpm_ratio = rpm / known_max_rpm` をAirflow Proxyとして扱えるようにする（最大RPMが確定している場合のみ）

### 風量に関する原則
- [ ] ファン径とRPMだけから絶対CFMを算出しない
- [ ] メーカー公称の最大風量 / 最大RPM / 静圧はmetadataとして保持可能にする
- [ ] FrontとTopは通気抵抗が異なるため、公称CFMやRPMを単純加算して吸排気バランスを断定しない
- [ ] Fan RPM/PWMは相対的なAirflow Proxyとして使い、実際の熱応答（`d.case_delta`、GPU Intake、Rear/Top Exhaust等）とセットで評価する

### 品質・障害
- [ ] NVML / lm-sensors / hwmonそれぞれのsource healthを持つ
- [ ] センサーが読めない場合に値を推測しない
- [ ] fan RPMが取得できる場合、PWM指令と実RPMの相関を記録できるようにする
- [ ] T_SENSORのlabelが実機上で安定して識別できることを確認

## 検証すべき仮説
- GPUの高消費電力時、ケースファンBIOS AutoではケースΔTが増えるか
- Front + Rearを高回転にすると `d.case_delta` / GPU Intake / GPU core / hotspot がどの程度改善するか
- Front + Rearを高回転にしても不足する条件が存在するか
- Top Radiator Fanを追加で上げた場合に、どの程度追加改善するか
- GPU排熱がTop radiator側へ回り込んでいるか
- GPU power上昇に対してGPU Intake / Rear Exhaust / Top Exhaustがどの順で応答するか

## 受入基準
- `air.*` と内部Telemetryを同一時系列で取得できる
- T_SENSOR、Front + Rear Fan Hub、Top Radiator FanのRPM/PWM状態を区別して参照できる
- Fan control #30/#43が必要とする入力を提供できる
- 取得不能なTelemetryがあってもCore Service全体が落ちない

## 依存
#26, #3
