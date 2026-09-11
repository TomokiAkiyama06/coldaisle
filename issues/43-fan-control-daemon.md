---
title: "段階式ファン制御daemon（Front+Rear優先 / Top補助）"
labels: core, safety, priority:must
milestone: "M7 拡張"
---

## 背景
GPU高負荷時にケースファンがGPU発熱へ十分追従しないため、GPU/ケースTelemetryに基づいて安全にファンを制御する。

現在のFan topology:
- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top Exhaust / CPU Radiator: Cooler Master MasterLiquid Atmos II、120mm Fan ×3

**このIssueは #30 の安全設計ADRが承認されるまで実装開始しない。**

## 制御階層

### Stage 1: Front + Rear

通常時は **Front Intake ×3 + Rear Exhaust ×1** のFan Hub系統だけを主制御する。

主な入力:
- GPU power（feed-forward）
- GPU core / hotspot
- `air.gpu_intake`
- `air.gpu_exhaust`
- `air.front_intake`
- `air.rear_exhaust`
- `d.case_delta = rear_exhaust - front_intake`
- T_SENSOR（12V-2x6外装）
- Case Fan Hub PWM / RPM

### Stage 2: Top補助排気

Front + Rearを十分に上げてもケース内の熱を捌き切れない状態が一定時間継続した場合のみ、Top Radiator Fanを追加で上げる。

候補判定:
- Case Fan Hubが高Dutyでも`d.case_delta`が高止まり
- GPU Intake / Rear Exhaustが高止まり
- GPU power高負荷が継続
- Top Exhaust側へ排気能力を追加する余地がある

正確な閾値・継続時間は #19 の実測で決定する。

## Top制御の安全条件

TopはCPU AIOのラジエーターファンでもあるため、CPU冷却要求を下げない。

- coldaisleは原則Topを「追加で上げる」方向にのみ介入
- CPU/BIOS側要求を下限とする安全な仲裁方法を実機で確認する
- 安全な仲裁を確認できない場合、TopはBIOS管理のままとする
- AIO Pump / VRM Fanは初期版ではBIOS管理

## 風量モデル

絶対CFMをファン径とRPMだけから推定しない。

- メーカー公称の最大風量 / 最大RPM / 静圧は参考値として保持
- `RPM / 最大RPM`、PWM dutyを Airflow Proxy として扱う
- FrontとTopは、ケース前面・ラジエーター等の抵抗が異なるため同一RPMで同じ流量とはみなさない
- 最終判断は `d.case_delta`、GPU Intake、Rear/Top Exhaust、GPU温度・GPU powerなどの熱応答を優先する

## アーキテクチャ

```text
GUI
  |
  | Auto / Manual / Max の指示のみ
  v
coldaisle-fand  (専用daemon / 必要最小限の権限)
  |
  +--> Front + Rear Fan Hub  [主制御]
  |
  +--> Top Radiator Fan      [不足時のみ補助・安全確認済みの場合]
```

- Core read-only APIと制御経路を分離する
- GUIをrootで動かさない
- LLM/AIツールからfan daemonへ到達させない
- `hwmonX`番号をhard-codeしない

## モード

- [ ] `Auto`: 決定論的な段階式制御
- [ ] `Manual`: GUI slider。ただし安全下限を下回れない
- [ ] `Max`: 安全側の最大冷却
- [ ] fault時: 強制Safe/Max

## やること

- [ ] #30で確定したfan header / pwmX対応だけを操作
- [ ] Front + Rear Fan HubとTop Radiator Fanを別制御対象として識別
- [ ] `% -> driver値`変換を安全に扱う
- [ ] PWM control modeへ入る前にdriver semanticsを検証
- [ ] GPU powerによるFront + Rear feed-forward curve
- [ ] GPU温度 / Intake / Exhaust / Case ΔTによるfeedback補正
- [ ] Front + Rearの不足判定state machine
- [ ] Top補助開始 / 解除条件と継続時間を実装
- [ ] Top補助はCPU冷却要求を下回らないことを保証
- [ ] ramp-upは速く、ramp-downは遅くする
- [ ] ヒステリシスを入れ、回転数とStage切替のハンチングを防ぐ
- [ ] tach監視。PWM指令に対してRPMが不足したら`FAN_FAULT`
- [ ] NVML/T_SENSOR/主要センサーstale時は安全側へ
- [ ] daemon heartbeat / deadman timeout
- [ ] SIGTERM / crash / restart時の安全動作
- [ ] systemd unit (`Restart=always`) と権限設計
- [ ] 制御Stage・指令PWM・実RPM・faultをTelemetryへ記録

## フェイルセーフ

重大なfault時はFront + Rearを原則100%へ倒す。
TopについてはCPU冷却を最優先し、#30で実機確認した安全動作だけを使用する。

fault例:
- NVMLが一定時間読めない
- T_SENSORが一定時間読めない
- Front/Rear/GPU Intake等の必要センサーがstale
- hwmon write失敗
- tachが期待下限を下回る
- heartbeat期限切れ
- 設定不正

## 受入基準

- 通常時はFront + Rearだけで制御する
- Front + Rearだけでは不足する条件でのみTop補助が発動する
- Top補助がCPU冷却要求を下回らない
- 風量の絶対CFM測定を前提としない
- daemonをkillしても冷却が停止しない
- NVML停止・T_SENSOR欠測・tach異常を注入すると安全側へ移行する
- 対象外PWMを変更しないことをテスト/実機確認で保証する
- Auto/Manual/Maxのいずれでも安全下限を破れない
- AI層からPWMを書き換える経路が存在しない
- #19の実測結果を使い、Top補助が必要な条件を説明できる

## 依存
#30, #34, #19, #26
