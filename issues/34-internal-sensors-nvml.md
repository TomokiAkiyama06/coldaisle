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

Fan control v1では **Front / Rear / Topを3系統で独立制御**するため、それぞれを別zoneとしてTelemetry化する。

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
- [ ] Front zone RPM
- [ ] Front zone PWM duty / control mode
- [ ] Rear zone RPM
- [ ] Rear zone PWM duty / control mode
- [ ] Top / CPU Radiator zone RPM
- [ ] Top zone PWM duty / control mode
- [ ] AIO Pump RPM（参照のみ）
- [ ] VRM Fan RPM（取得可能な場合）
- [ ] Front / Rear / Topそれぞれの物理headerと `pwmX` / `fanX_input` 対応を特定
- [ ] RearがFront Hubから分離されていることを確認
- [ ] hwmon番号を固定せず、driver名・label・安定属性から探索

現在のFan topology:
- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top Exhaust / CPU Radiator: Cooler Master MasterLiquid Atmos II 360

### Fan metadata
#44のAirflow Model用に以下を保持できるようにする。
- zone name
- fan count
- known max RPM
- manufacturer rated airflow（存在する場合）
- manufacturer rated static pressure（存在する場合）
- measured startup PWM
- measured minimum stable PWM / RPM
- measured maximum RPM

既知の公称値:
- Front NF-A12x25 G2: 1基あたり最大1800 RPM / 63.15 CFM / 3.14 mmH2O
- Top Atmos II 360: 公称最大2500 RPM / 190 CFM / 3.61 mmH2O
- Rear: 実機型番・公開仕様を確認。無ければ測定値のみ使う

### メトリクス・派生値
- [ ] 同一`readings`テーブルへ投入（ロング形式のためスキーマ変更不要）
- [ ] `d.gpu_internal_delta = gpu.0.hotspot - gpu.0.core`
- [ ] `d.case_delta = air.rear_exhaust - air.front_intake`
- [ ] `d.gpu_preheat = air.gpu_intake - air.front_intake`
- [ ] `d.gpu_delta = air.gpu_exhaust - air.gpu_intake`
- [ ] `d.rear_rise = air.rear_exhaust - air.room`
- [ ] 12V-2x6温度はRoomとの差分も表示可能にする
- [ ] zoneごとの `rpm_ratio = rpm / measured_max_rpm`
- [ ] #44で確定後、zoneごとの `airflow_index`（0〜1）を参照可能にする

### 風量に関する原則
- [ ] ファン径とRPMだけから絶対CFMを算出しない
- [ ] メーカー公称値はprior / metadataとして扱う
- [ ] Front / Rear / Topの通気抵抗が異なるため、公称CFMやRPMを単純加算して吸排気バランスを断定しない
- [ ] PWM→RPM実測と熱応答を組み合わせた #44 Airflow Modelを使う

### 品質・障害
- [ ] NVML / lm-sensors / hwmonそれぞれのsource healthを持つ
- [ ] センサーが読めない場合に値を推測しない
- [ ] zoneごとにPWM指令と実RPMの相関を記録できるようにする
- [ ] T_SENSORのlabelが実機上で安定して識別できることを確認
- [ ] CPU telemetry lossをfan daemonへ明示できる

## 検証すべき仮説
- Frontを上げたときGPU Intakeがどの程度改善するか
- Rearを独立して上げたとき `d.case_delta` / GPU Intake / Rear Exhaustがどの程度改善するか
- FrontとRearの最適な相対関係は一定か、GPU powerで変わるか
- Front + Rearだけで不足する条件が存在するか
- Topをケース補助排気として上げた場合にどの程度追加改善するか
- TopのCPU cooling demandとcase exhaust demandが競合する条件があるか
- GPU排熱がTop radiator側へ回り込んでいるか

## 受入基準
- `air.*` と内部Telemetryを同一時系列で取得できる
- Front / Rear / TopのRPM/PWM状態を別zoneとして参照できる
- RearがFrontから独立している
- CPU temperature / powerがTop制御入力として取得できる
- #44 / #30 / #43が必要とするFan Telemetryを提供できる
- 取得不能なTelemetryがあってもCore Service全体が落ちない

## 依存
#26, #3
