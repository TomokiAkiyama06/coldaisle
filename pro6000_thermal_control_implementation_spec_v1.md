# RTX PRO 6000 Thermal Control 実装仕様 v1

作成日: 2026-09-15

## 1. 目的

RTX PRO 6000 Blackwell Workstation Edition 搭載GPUサーバーについて、これまで取得した実機データを保存し、既存の制御契約に従う実装候補を整理する。

**本書より `docs/decisions/0026`〜`0029` と `AGENTS.md` の FINAL 契約を優先する。** 現在のアプリが操作できるアクチュエータは Front / Rear / Top の3 zoneだけである。GPU Fan と VRM Fan の直接操作は本書の実測対象ではあるが、責務境界・Safety・fault・Hardware Backend の契約を定める後続 Decision Record が承認されるまで実装しない。

現在の制御経路は以下を基本とする。

```text
Sensors / Telemetry Collector
        ↓
Supervisor
        ↓
Learned MPC（candidate）
        ↓
Confidence / OOD Gate
        ↓
Baseline / Fallback selection
        ↓
Reactive Guard
        ↓
Critical Safety
        ↓
Actuator Arbitration
        ↓
verified Front / Rear / Top Hardware Backend
```

重要方針:

- AIO Pumpは100%固定とし、アプリから通常制御しない。
- Critical Safetyはすべての上位制御より優先する。
- Learned MPCは最初から直接制御せず、Shadow Modeから開始する。Confidence/OOD不成立、モデル未ロード、optimizer timeoutではBaseline / Fallbackを選び、MPC出力をReactive Guardへ直接渡さない。
- GPUの温度・Power・UtilizationはTelemetry Collectorが取得する制御入力候補であり、`coldaisle-fand` がNVMLを直接呼び出す根拠にはしない。
- CPU / GPU / VRM / ケース温度の絶対値に加えて、温度上昇速度 `dT/dt` を利用する。
- 将来的には室温センサーを追加し、`ΔT = Component Temp - Room Temp` を主要特徴量にする。

---

## 2. 対象ハードウェア

```text
CPU: AMD Ryzen 9 9950X
GPU: NVIDIA RTX PRO 6000 Blackwell Workstation Edition / 96GB / 最大約600W
MB : ASUS ProArt X870E-CREATOR WIFI
OS : Ubuntu Desktop 26.04.1 LTS
Kernel: 7.0.0-31-generic
NVIDIA Driver: 595.91.07
```

---

## 3. ファン / PWM マッピング

Linux上では `nct6775` をロードし、`nct6799` hwmon を利用する。hwmon番号は固定せず `name == nct6799` で動的検出する。

| fan / pwm | 役割 |
|---|---|
| fan2 / pwm2 | Top Cooler Fan |
| fan4 / pwm4 | Water Block VRM Fan |
| fan5 / pwm5 | Rear Exhaust |
| fan6 / pwm6 | Front Intake |
| fan7 / pwm7 | AIO Pump |

AIO Pumpは約3370 RPM、PWM 255固定。通常制御対象から除外する。

---

## 4. 実測済み安全下限

### Top Cooler

```text
pwm16 で停止
pwm24 付近で再始動
実用下限候補 pwm32
Idleでは pwm0 / 0 RPM が成立
```

### VRM Fan

```text
pwm16 付近で停止
pwm32 付近で再始動
実運用下限 pwm40
```

CPU+GPU最大負荷でも `pwm40` でVRM温度に十分余裕があることを確認済み。

### Rear Exhaust

```text
pwm0でも完全停止せず約440〜450 RPM
```

### Front Intake

```text
pwm16で停止
pwm24付近で再始動
実用下限候補 pwm32
```

---

## 5. GPUファン制御の実測

GPUファンは手動実験ではNVMLから直接制御可能で、2基とも認識済みである。**これは実測結果であり、現在の `coldaisle-fand` の制御対象を増やすものではない。** `coldaisle-fand` はTelemetry Collectorのタイムスタンプ付き出力を消費し、NVMLを直接呼び出さない。GPU Fanを通常制御へ追加するには、専用のDecision Recordで単一owner、Safety、fault、異常終了時の扱い、Hardware Backend契約を承認する必要がある。

```text
AUTO policy   = 0
MANUAL policy = 1
```

使用API:

```text
nvmlDeviceSetFanSpeed_v2()
nvmlDeviceSetDefaultFanSpeed_v2()
nvmlDeviceGetFanSpeed_v2()
nvmlDeviceGetTargetFanSpeed()
nvmlDeviceGetFanControlPolicy_v2()
```

GPUファンは目標変更後、約10〜12秒かけて追従する。指令には15〜20秒程度のHold / Hysteresisを持たせる。

---

## 6. GPU 600Wファンカーブ実測

条件:

```text
GPU 約600W / Util 約100%
Top 100%
Rear 100%
Front 75%
VRM Fan pwm40
```

| GPU Fan | GPU Temp | GPU Clock | Thermal Slowdown |
|---:|---:|---:|---|
| 70% | 83.0〜83.5°C | 約1814〜1819 MHz | 0 |
| 80% | 81.5°C | 1818.5 MHz | 0 |
| 90% | 78.7°C | 1829.7 MHz | 0 |
| 100% | 77.0°C | 1835.8 MHz | 0 |

70%基準のドリフト補正効果:

```text
80%:  GPU Temp -1.75°C / GPU Clock +1.65 MHz
90%:  GPU Temp -4.55°C / GPU Clock +15.0 MHz
100%: GPU Temp -6.00°C / GPU Clock +20.6 MHz
```

---

## 7. GPU長時間600W試験

### GPU Fan 90% / AC OFF / 30分

```text
Last 5 min:
GPU Temp   88.00°C
GPU Power  599.99W
GPU Clock  1777.60 MHz

Last 10 min:
GPU Temp slope +0.0066°C/min

SW Thermal Slowdown 0 → 0
HW Thermal Slowdown 0 → 0
```

### GPU Fan 100% / AC OFF / 30分

```text
Last 5 min:
GPU Temp   84.91°C
GPU Power  599.99W
GPU Clock  1791.71 MHz

Last 10 min:
GPU Temp slope +0.0020°C/min

SW Thermal Slowdown 0 → 0
HW Thermal Slowdown 0 → 0
```

結論:

- 無空調・室温未計測でもThermal Slowdownは発生しなかった。
- 長時間550W超では90%より100%にする価値がある。
- 100%では90%より約3°C低い熱平衡温度になった。

---

## 8. CPU + GPU 最大負荷試験

条件:

```text
AC OFF / Room Temp unknown
CPU 100%
GPU 約600W
GPU Fan 100%
Top 100%
VRM Fan 100%
Rear 100%
Front 100%
AIO Pump 100%
```

30分後の定常状態:

```text
GPU Temp   84.96°C
GPU Power  599.98W
GPU Clock  1790.65 MHz
CPU Tctl   88.62°C
VRM        72.58°C
```

最後10分の温度傾き:

```text
GPU  -0.0097°C/min
CPU  +0.0172°C/min
VRM  -0.0447°C/min
```

```text
SW Thermal Slowdown 0 → 0
HW Thermal Slowdown 0 → 0
```

結論: AC OFF / CPU100% / GPU600W / 30minでも熱平衡まで到達し、GPU Thermal Slowdownなしで完走。

---

## 9. クールダウン実測

CPU+GPU最大負荷停止後、全ファン100%で15分冷却。

```text
開始: GPU 85.0°C / CPU 87.2°C / VRM 73.0°C
15分後: GPU 39.0°C / CPU 64.8°C / VRM 56.0°C
```

到達時間:

```text
GPU <= 60°C : 約0.23分
VRM <= 60°C : 約5.80分
CPU <= 60°C : 15分以内には未到達
```

GPUとCPUでCooldown Policyを分ける。

---

## 10. Top Cooler Zero-RPM

Idle 10分:

```text
Top        0 RPM
Rear       約50%
Front      約50%
VRM Fan    pwm40
Pump       約3366 RPM
GPU        idle
AC         OFF
Room Temp  unknown

CPU steady 47.90°C
CPU max    48.38°C
VRM steady 50.00°C
GPU steady 32.00°C
```

IdleではTop 0 RPMが成立。

CPU 25%負荷では、

```text
約74.6°C → 80.0°C / 約60秒
約 +5.4°C/min
```

で上昇し、安全閾値80°Cで停止。

結論:

```text
Top 0 RPM:
  Idle / Very Low Load   = 使用可能
  CPU 25% sustained load = 不採用
```

Zero-RPM解除はCPU温度だけでなく、CPU utilization急増または `dT/dt` を使って早期に行う。

---

## 11. CPU Top Cooler 実測

CPU 100%・GPU idle:

```text
Top75 vs Top100:
CPU Tctl 約 +1.39°C
VRM      約 +1.93°C

Top50 vs Top100:
CPU Tctl 約 +4.09°C
CPU Package 約 +4.07°C
```

CPU+GPU同時最大負荷:

```text
Top75 vs Top100:
CPU Tctl +0.99°C
GPU Temp +0.23°C

Top50 vs Top100:
CPU Tctl +2.04°C
GPU Temp +0.48°C
GPU Fan  +3.2 point
GPU Clock -5.6MHz
```

初期方針:

```text
CPU High: Top75 baseline / 高温時Top100
CPU+GPU High: Top75または100 / 高温時100
Top50: 高負荷時の標準値にはしない
```

---

## 12. VRM Fan 実測

```text
50% 約2400RPM
25% 約1150RPM
pwm40 約650RPM
```

CPU+GPU最大負荷でも `pwm40` でVRM約70°C前後。

初期制御:

```text
VRM < 70°C  : pwm40
70〜75°C    : 緩やかに増速
75〜80°C    : 強めに増速
>= 80°C     : 100%
```

ファン故障監視:

```text
pwm >= 40 かつ RPM < 350 が3サンプル程度継続
→ Fan Fault → 100%
```

---

## 13. Rear / Front 実測

GPU高負荷・CPU低負荷:

```text
Rear 100%
Front 75%
```

Rear50 / Front100はGPUに悪影響が出たため避ける。

CPU+GPU高負荷:

```text
Rear 100%
Front 100%
```

CPU-onlyではケースファン速度によるCPU温度差は小さく、初期値候補はRear50 / Front50。

---

# 14. 初期Supervisor状態

```text
IDLE_LOW
CPU_DOMINANT
GPU_DOMINANT
COMBINED_HIGH
TRANSITION
CRITICAL
```

---

## 15. 状態判定 初期値

### GPU High

Enter候補:

```text
GPU Power >= 450W
OR
GPU Util >= 90%
```

Exit候補:

```text
GPU Power < 300W
AND GPU Util < 60%
を10秒継続
```

### CPU High

```text
Enter: CPU Util >= 85% を4秒継続
Exit : CPU Util < 60% を10秒継続
```

### IDLE_LOW

```text
CPU Util < 10%
GPU低負荷
CPU Tctl < 55°C
dT/dt が小さい
```

---

# 16. 初期アクチュエータポリシー

以下の Front / Rear / Top は現在の3 zone契約における候補である。VRM Fan と GPU Fan の記載は実測に基づく将来候補であり、現行daemonの出力・Safety・fault契約には含めない。AIO Pumpは通常制御対象外である。

## IDLE_LOW

```text
Top      0 RPM候補
VRM      pwm40
Rear     低速
Front    低速
GPU Fan  NVIDIA AUTO
AIO Pump 100%
```

Zero-RPM解除条件を必ず実装する。

## CPU_DOMINANT

```text
Top      75% baseline / 高温時100%
VRM      pwm40〜温度追従
Rear     50%
Front    50%
GPU Fan  AUTO
AIO Pump 100%
```

## GPU_DOMINANT

```text
Rear     100%
Front    75%
Top      CPU温度依存
VRM      pwm40〜温度依存
AIO Pump 100%
```

GPU Fan Feed-forward（将来候補。現行daemonでは実行しない）:

```text
GPU Power >= 300W → 70%
GPU Power >= 450W → 80%
GPU Power >= 550W → 90%
```

長時間高負荷:

```text
GPU Power >= 550W が数分継続
OR GPU Temp >= 83〜84°C
→ GPU Fan 100%
```

Safety（将来候補。現行daemonではGPU Fanを操作しない）:

```text
GPU Temp >= 85°C → 100%
```

## COMBINED_HIGH

```text
Rear     100%
Front    100%
Top      75〜100%
VRM      pwm40〜温度依存
GPU Fan  90〜100%
AIO Pump 100%
```

高温または長時間継続時:

```text
Top      100%
GPU Fan  100%
```

---

# 17. Reactive Guard

周期候補: `2秒`

主な入力:

```text
CPU Tctl / Util / dT/dt
GPU Temp / Power / Util / dT/dt
VRM Temp
Fan RPM
Pump RPM
```

ルール例（GPU Fan / VRM Fanへの直接出力は将来候補であり、現行のReactive Guardは3 zoneにのみ出力する）:

```text
GPU Power > 550W → GPU Fan 最低90%
GPU Power > 550W が継続 → 100%
GPU Temp >= 85°C → 100%
CPU高負荷開始 → Top Zero-RPM解除
CPU dT/dt急上昇 → Topを1段階先回り
VRM >= 75°C → VRM Fan増速
```

---

# 18. Critical Safety

以下の数値は実測に基づく候補であり、Safety設定値を確定しない。正式値は既存の承認手順で設定する。

```text
GPU >= 90°C
CPU >= 92°C
VRM >= 85°C
```

現行の3 zone契約でCriticalになったときは、検証済みの Top / Rear / Front をMaxへ上げる。AIO Pumpは通常制御対象外であり、GPU Fan / VRM Fanをこのdaemonから直接操作しない。

テレメトリ欠測もCritical Safetyの入力である。品質が `ok` 以外（`missing` / `suspect` / `stale`）のとき、CPUテレメトリ欠測はTopをMaxへ、GPU・T_SENSOR・必須airテレメトリ欠測はFrontとRearを安全側へ上げる。全DS18B20が使用不能ならCritical、部分的な使用不能ならDegraded Guardとして扱う。

追加Critical条件候補:

```text
GPU SW Thermal Slowdown増加
GPU HW Thermal Slowdown増加
AIO Pump異常
Fan Fault
```

---

# 19. Cooldown Policy

### GPU

```text
GPU高負荷終了
↓
GPU Fan高回転維持
↓
GPU < 60°C かつ温度下降中
↓
NVIDIA AUTOへ復帰
```

実装上の最低Hold候補: `20〜30秒`

### CPU

```text
CPU高負荷終了
↓
Top高回転維持
↓
CPU < 65°C かつ dT/dt <= 0
↓
段階的に減速
```

GPUと同じCooldown時間を使わない。

---

# 20. 制御周期

```text
Telemetry        2秒
Critical Safety  2秒
Reactive Guard   2秒
Supervisor       5秒
MPC              5秒
```

PWM / Fan command後の最低Hold: `5〜10秒`

GPU Fan Hold: `15〜20秒`

---

# 21. Learned MPC

段階導入:

```text
Phase 1: Shadow Mode
Phase 2: Bounded Assist
Phase 3: Active Control
```

入力候補:

```text
CPU: Temp / Util / Package Power / dT/dt
GPU: Temp / Power / Util / Clock / Fan % / Thermal Slowdown / dT/dt
VRM: Temp
Case: Front Intake / GPU Intake / GPU Exhaust / Top Exhaust / Rear Exhaust
Room: Room Temp / Humidity
Actuators: Top / Rear / Front Demand（current scope）; VRM / GPU Fan %（将来候補）
```

将来追加:

```text
load duration
time since load transition
ambient ΔT
thermal history
fan response delay
```

同じ600Wでも短時間と長時間で温度が大きく異なるため、`load duration` と温度履歴は重要な特徴量とする。

MPCはcandidate proposalだけを出す。Confidence / OOD Gateで信頼できない入力・未ロード・timeoutを検出した場合は、Baseline / Fallbackを選んでからReactive Guardへ渡す。MPC出力を直接Reactive GuardやHardware Backendへ渡してはならない。

---

# 22. Actuator Arbitration

優先順位:

```text
Critical Safety
    >
Reactive Guard
    >
Supervisor Limits
    >
Learned MPC
    >
Base Profile
```

例:

```text
MPCがFront demand 0.70を要求
Reactive Guardが0.90を要求
→ 0.90

MPCがTop 0を要求
CPU High
→ Supervisor下限を適用
```

---

# 23. Daemon / Fail-safe

常駐サービス化する。

```text
pro6000-thermal-control.service
```

systemd候補:

```text
Restart=always
RestartSec=2
```

Daemon異常終了時は、外部のsystemd watchdogと `ExecStopPost` のhandoffで、検証済みの Front / Rear / Top headerをMaxへ強制する。BIOS/Q-Fanへの復帰を安全handoffの代替にしない。GPU FanとVRM Fanは現行daemonの制御対象外であり、この経路でNVML操作やownerの引き継ぎを導入しない。

`ExecStopPost` / Watchdog / Heartbeat は必須の安全設計として実装する。

最重要要件:

```text
アプリが死んだことでファンが低回転固定になる状態を絶対に作らない。
```

---

# 24. ログ

最低限保存:

```text
timestamp
Supervisor state
Reactive Guard state
Critical state
CPU temp/util/power/dTdt
GPU temp/power/util/clock/fan current/fan target/thermal counters/dTdt
VRM temp
Top/VRM/Rear/Front PWM/RPM
Pump RPM
Room Temp
各ケース温度
MPC proposed action
Final applied action
Override reason
```

ログ形式候補:

```text
CSV または SQLite / Parquet
```

長期学習用にはParquetを推奨。

---

# 25. 実装フェーズ

```text
Phase A — Telemetry
Phase B — Actuator API
Phase C — Critical Safety
Phase D — Reactive Guard
Phase E — Supervisor
Phase F — GUI / Monitoring
Phase G — Learned MPC Shadow Mode
```

自動制御を常時有効化するのは、Critical SafetyとReactive Guardの両方（Phase CおよびPhase D）を完了し、3 zoneの安全経路を検証した後である。

---

# 26. 最初に実装するディレクトリ案

```text
thermal-control/
├── app/
│   ├── main.py
│   ├── telemetry/
│   │   ├── cpu.py
│   │   ├── gpu.py
│   │   ├── motherboard.py
│   │   ├── esp32.py
│   │   └── models.py
│   ├── actuators/
│   │   ├── nct6799.py
│   │   ├── nvml_fan.py
│   │   └── failsafe.py
│   ├── control/
│   │   ├── supervisor.py
│   │   ├── reactive_guard.py
│   │   ├── critical_safety.py
│   │   ├── arbitration.py
│   │   └── mpc.py
│   ├── logging/
│   │   └── recorder.py
│   └── config/
│       └── defaults.yaml
├── tests/
│   ├── test_supervisor.py
│   ├── test_guard.py
│   ├── test_safety.py
│   └── test_arbitration.py
├── scripts/
│   ├── fan_probe.py
│   ├── nvml_probe.py
│   └── restore_auto.sh
├── systemd/
│   └── pro6000-thermal-control.service
├── data/
│   └── logs/
├── pyproject.toml
└── README.md
```

---

# 27. 実装開始順

```text
1. Telemetry統合
2. Actuator API
3. Fail-safe
4. Critical Safety
5. Reactive Guard
6. Supervisor
7. Logging
8. GUI
9. MPC Shadow Mode
10. MPC Active
```

特に1〜5が完成し、Critical SafetyとReactive Guardの両方を通ることを確認するまでは、自動制御を常時有効化しない。

---

# 28. 現時点の結論

実機検証によって以下は確認済み。

```text
GPU 600W長時間運転可能
GPU Fan NVML手動制御可能
GPU Thermal Slowdownなし
CPU+GPU最大負荷30分完走
AC OFFでも熱平衡まで確認
Top Zero-RPMはIdleのみ有効
VRM Fan pwm40は実用下限として成立
GPU高負荷ではRear100が重要
CPU+GPU高負荷ではRear100/Front100が適切
```

次の段階は追加ベンチマークより、実測結果を安全な制御ソフトウェアへ落とし込むことを優先する。

空調25°C・Room Tempセンサー導入後に、制御パラメータを再キャリブレーションする。
