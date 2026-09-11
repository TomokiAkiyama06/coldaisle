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
- [ ] VRM温度
- [ ] chipset温度
- [ ] **ASUS T_SENSOR**（12V-2x6コネクタ外装に設置した10kΩ NTC）
- [ ] ケースFan HubのRPM
- [ ] ケースFan HubのPWM duty / control mode（取得可能な範囲）
- [ ] Radiator Fan RPM
- [ ] AIO Pump RPM
- [ ] VRM Fan RPM（取得可能な場合）
- [ ] hwmon番号を固定せず、driver名・label・安定属性から探索

### メトリクス・派生値
- [ ] 同一`readings`テーブルへ投入（ロング形式のためスキーマ変更不要）
- [ ] `d.gpu_internal_delta = gpu.0.hotspot - gpu.0.core`
- [ ] `d.case_delta = air.rear_exhaust - air.front_intake`
- [ ] `d.gpu_preheat = air.gpu_intake - air.front_intake` を継続利用
- [ ] `d.gpu_delta = air.gpu_exhaust - air.gpu_intake` を継続利用
- [ ] `d.rear_rise = air.rear_exhaust - air.room` を継続利用
- [ ] 12V-2x6温度はRoomとの差分も表示可能にする

### 品質・障害
- [ ] NVML / lm-sensors / hwmonそれぞれのsource healthを持つ
- [ ] センサーが読めない場合に値を推測しない
- [ ] fan RPMが取得できる場合、PWM指令と実RPMの相関を記録できるようにする
- [ ] T_SENSORのlabelが実機上で安定して識別できることを確認

## 検証すべき仮説
- GPUの高消費電力時、ケースファンBIOS AutoではケースΔTが増えるか
- ケースファン100%で `d.case_delta` / GPU Intake / GPU core / hotspot がどの程度改善するか
- GPU排熱がTop radiator側へ回り込んでいるか
- GPU power上昇に対してGPU Intake / Rear Exhaust / Top Exhaustがどの順で応答するか

## 受入基準
- `air.*` と内部Telemetryを同一時系列で取得できる
- Workspace側が独自に`nvidia-smi`を叩かず、coldaisleだけでGPUパネルを構成できる
- T_SENSORとFan RPM/PWMがServer Healthから参照可能
- 取得不能なTelemetryがあってもCore Service全体が落ちない

## 依存
#26, #3
