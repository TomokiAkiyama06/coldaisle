---
title: "3系統Fan制御daemon（Front / Rear独立・Top CPU優先）"
labels: core, safety, priority:must
milestone: "M8 Fan Control"
moved_to: 74
---

> **この定義は GitHub #74 へ移管しました。** M8 以降の Issue は GitHub の本文が正本です（2026-09-13）。
> 以下は移管前の記録で、更新しません。`scripts/create_issues.sh` は作成・更新しません。

## 背景
GPU高負荷時のケース換気とCPU AIO冷却を安全に両立するため、Front / Rear / Topを3系統で独立制御する。

現在のFan topology:
- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top Exhaust / CPU Radiator: Cooler Master MasterLiquid Atmos II 360
- AIO Pump: BIOS / 固定安全設定で管理

RearはFrontのHubから分離し、別PWM headerへ接続する。
TopもcoldaisleがPWMを管理する。

**このIssueは #30 の安全設計ADRが承認されるまで実装開始しない。**

## 制御対象

### Front Intake zone
Front Intake ×3を1 zoneとして制御する。

入力例:
- GPU power（feed-forward）
- GPU core / hotspot
- `air.front_intake`
- `air.gpu_intake`
- Front PWM / RPM / Airflow Index

### Rear Exhaust zone
Rear Exhaust ×1をFrontと独立して制御する。

Rearは通常時の主排気とし、Frontを上げた際にRearだけを追加で上げられるようにする。

入力例:
- `air.rear_exhaust`
- `d.case_delta`
- GPU Intake / GPU Exhaust
- Rear PWM / RPM / Airflow Index

### Top / CPU Radiator zone
Topもcoldaisleが常時PWMを管理する。

```text
top_demand = max(cpu_cooling_demand, case_aux_exhaust_demand, safety_floor)
```

- `cpu_cooling_demand`: CPU温度 / CPU power等から決定
- `case_aux_exhaust_demand`: Front + Rearだけではケース排熱が不足する場合に上げる
- `safety_floor`: CPU AIOとして維持する最低安全値

通常時はケース側のTop要求を低く保ち、Rearを含む通常排気で不足するときだけケース補助要求を増やす。

## 風量モデル

#44で作成したzone別Airflow Modelを利用する。

- メーカー公称値はprior / metadata
- PWM→RPM curveを実測
- 起動PWM / 最低安定PWMを実測
- zoneごとに0〜1のAirflow Indexを持つ
- Front / Rear / Topの冷却効果を固定熱負荷で実測
- 絶対CFMの一致を制御目標にしない

最終判断はAirflow Indexに加えて以下の熱応答を使う。
- `d.case_delta`
- GPU Intake / GPU Exhaust
- Rear Exhaust / Top Exhaust
- GPU core / hotspot / power
- CPU temperature / power

## アーキテクチャ

```text
GUI
  |
  | Auto / Manual / Max
  v
coldaisle-fand
  |
  +--> Front PWM  [Intake]
  +--> Rear PWM   [Primary Exhaust]
  +--> Top PWM    [CPU Radiator + Auxiliary Exhaust]

AIO Pump          [outside coldaisle control]
```

- GUIをrootで動かさない
- Core read-only APIと制御経路を分離
- LLM/AIからfan daemonへ到達させない
- `hwmonX`番号をhard-codeしない

## モード

- [ ] `Auto`: 決定論的な3-zone制御
- [ ] `Manual`: zone別slider。ただし安全下限を下回れない
- [ ] `Max`: 制御可能な正常Fanを最大冷却へ
- [ ] fault時: 強制Safe/Max

ManualでもTopはCPU safety floorを下回れない。

## やること

- [ ] #30で確定したFront / Rear / Top headerだけを操作
- [ ] RearをFront Hubから分離した配線を実機確認
- [ ] `% -> driver値`変換を安全に扱う
- [ ] 各zoneのPWM control mode / semanticsを検証
- [ ] Front吸気feed-forward / feedback curve
- [ ] Rear主排気curve
- [ ] Front / Rearの協調制御
- [ ] CPU温度 / powerからTopの `cpu_cooling_demand` を生成
- [ ] ケース排熱不足から `case_aux_exhaust_demand` を生成
- [ ] Topを `max(cpu, case, floor)` で制御
- [ ] #44のAirflow Indexを制御状態へ統合
- [ ] ramp-upは速く、ramp-downは遅くする
- [ ] zoneごとにヒステリシス / slew limitを入れる
- [ ] tach監視。PWM指令に対してRPM不足ならzone別`FAN_FAULT`
- [ ] CPU telemetry loss時はTopをSafe/Max
- [ ] NVML / T_SENSOR / 必須外気センサーstale時は安全側へ
- [ ] daemon heartbeat / deadman timeout
- [ ] SIGTERM / crash / restart時の安全動作
- [ ] systemd unit (`Restart=always`) と権限設計
- [ ] zone別PWM / RPM / Airflow Index / demand / faultをTelemetryへ記録

## フェイルセーフ

重大fault時は、制御可能な正常Fanを安全側へ上げる。

特にTopはCPU冷却を担うため以下をSafe/Maxへ:
- CPU temperature telemetry loss
- Top hwmon write failure
- Top tach stall
- daemon startup / state未確定
- 設定不正

Front / Rearも以下では安全側へ:
- NVML loss
- T_SENSOR loss
- 必須外気センサーstale
- hwmon write failure
- tach stall
- heartbeat timeout

AIO Pumpはcoldaisleから制御しない。

## 受入基準

- Front / Rear / Topが独立PWMとして制御できる
- Frontを変えずRearだけ上げられる
- Rearを変えずFrontだけ上げられる
- Topはアプリ管理で `max(cpu_cooling_demand, case_aux_exhaust_demand, safety_floor)` を守る
- ManualでもCPU safety floorを破れない
- CPU telemetry lossでTopが安全側へ移行する
- #44のAirflow Modelを使う
- 絶対CFM測定を前提としない
- daemon kill / restartでも危険な冷却停止にならない
- 対象外PWMを変更しない
- AI層からPWM書換経路が存在しない
- #19 / #44の実測に基づいて各zoneの制御根拠を説明できる

## 依存
#30, #34, #44, #19, #26
