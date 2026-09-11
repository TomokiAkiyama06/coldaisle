---
title: "ケースFan Hub制御daemon（GPU連動・フェイルセーフ）"
labels: core, safety, priority:must
milestone: "M7 拡張"
---

## 背景
GPU高負荷時にケースファンがGPU発熱へ十分追従しないため、Front + Rearを束ねるFan HubをGPU/ケースTelemetryに基づいて制御する。

**このIssueは #30 の安全設計ADRが承認されるまで実装開始しない。**

## スコープ
初期版で制御するのは **ケースFan Hub 1系統のみ**。

- Front Intake ×3
- Rear Exhaust ×1

AIO Pump / Radiator Fan / VRM Fanは初期版ではBIOS管理を維持する。

## アーキテクチャ

```text
Workspace GUI
    |
    | Auto / Manual / Max の指示のみ
    v
coldaisle-fand  (専用daemon / 必要最小限の権限)
    |
    v
Linux hwmon/sysfs
    |
    v
CHA_FAN -> Fan Hub -> Front + Rear
```

- Core read-only APIと制御経路を分離する
- GUIをrootで動かさない
- LLM/AIツールからfan daemonへ到達させない
- `hwmonX`番号をhard-codeしない

## 制御入力
- GPU power（NVML）をfeed-forwardとして使用
- GPU core / hotspot
- `air.gpu_intake`
- `air.gpu_exhaust`
- `air.front_intake`
- `air.rear_exhaust`
- `d.case_delta = rear_exhaust - front_intake`
- T_SENSOR（12V-2x6外装）

## モード
- [ ] `Auto`: 決定論的な制御カーブ
- [ ] `Manual`: GUI slider。ただし安全下限を下回れない
- [ ] `Max`: 100%
- [ ] fault時: 強制Safe/Max

## やること
- [ ] #30で確定したfan header / pwmX対応だけを操作
- [ ] `% -> 0..255`変換をdriverごとに安全に扱う
- [ ] PWM control modeへ入る前にdriver semanticsを検証
- [ ] GPU powerによるfeed-forward curve
- [ ] GPU温度 / Intake / Exhaust / Case ΔTによる補正
- [ ] ramp-upは速く、ramp-downは遅くする
- [ ] ヒステリシスを入れ、回転数のハンチングを防ぐ
- [ ] tach監視。PWM指令に対してRPMが不足したら`FAN_FAULT`
- [ ] NVML/T_SENSOR/主要センサーstale時は100%
- [ ] daemon heartbeat / deadman timeout
- [ ] SIGTERM / crash / restart時の安全動作
- [ ] systemd unit (`Restart=always`) と権限設計
- [ ] 制御指令・実PWM・RPM・faultをTelemetryへ記録

## 初期制御の考え方
GPU powerは温度上昇より先に変化するため、GPU負荷開始直後のfeed-forwardに使う。
温度と`d.case_delta`はfeedbackとして補正する。

具体的な閾値はコードに埋めず設定へ出し、#19の実測結果で確定する。

## フェイルセーフ
以下はすべてケースFan Hubを安全側へ倒す。

- NVMLが一定時間読めない
- T_SENSORが一定時間読めない
- Front/Rear/GPU Intake等の必要センサーがstale
- hwmon write失敗
- tachが期待下限を下回る
- heartbeat期限切れ
- 設定不正

## 受入基準
- fan daemonをkillしても冷却が停止しない
- NVML停止・T_SENSOR欠測・tach異常を注入すると安全側へ移行する
- ケースFan Hub以外のPWMを変更しないことをテスト/実機確認で保証する
- Auto/Manual/Maxのいずれでも安全下限を破れない
- AI層からPWMを書き換える経路が存在しない
- #19のBIOS Auto対100%測定を再現し、Auto制御が期待どおり追従する

## 依存
#30, #34, #19, #26
