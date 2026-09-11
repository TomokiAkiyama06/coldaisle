---
title: "【設計のみ】3系統Fan制御の安全設計検討"
labels: design, safety, priority:must
milestone: "M7 拡張"
---

## 背景
GPU高負荷時の排熱を安全に制御するため、Front / Rear / Topを最初から別系統として扱う。

現在のFan topology:
- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top Exhaust / CPU Radiator: Cooler Master MasterLiquid Atmos II 360
- AIO Pump: BIOS / 固定安全設定で管理

RearはFrontのHubから分離し、別PWM headerへ接続する前提とする。
Topもcoldaisleが制御するが、CPU AIOのラジエーターファンであるためCPU冷却要求を必ず含める。

**高額ハードウェアの冷却制御なので、実装前に安全設計を確定する。**

## 制御方針

### Zone 1: Front Intake
Front Intake ×3を独立PWMで制御する。GPU powerをfeed-forward、Front / GPU Intake / GPU温度をfeedbackとして吸気要求を決める。

### Zone 2: Rear Exhaust
Rear Exhaust ×1をFrontとは別PWMで制御する。通常時の主排気であり、Frontを上げたときRearだけ追加で上げられるようにする。

### Zone 3: Top / CPU Radiator
Topはcoldaisleが常時PWMを管理する。

Topには2つの要求を持たせる:
1. CPU cooling demand
2. case auxiliary exhaust demand

最終要求は概念上、以下とする。

```text
top_demand = max(cpu_cooling_demand, case_aux_exhaust_demand, safety_floor)
```

`case_aux_exhaust_demand` は通常は低く、Front + Rearだけで熱を捌き切れない状態が一定時間続いた場合だけ上げる。

CPU telemetryが取得不能、Top tach異常、制御ループ異常などの場合はTopを安全側の高回転へ移行する。

## 風量設計

ファン径とRPMだけから絶対CFMを推定しない。

### Manufacturer prior
- Front NF-A12x25 G2: 1基あたり最大1800 RPM / 63.15 CFM / 3.14 mmH2O
- Top Atmos II 360: 公称最大2500 RPM / 190 CFM / 3.61 mmH2O
- Rear FLUX純正Fan: 実機型番・最大RPMを確認し、公開仕様が無ければ実測中心で扱う

これらはケース装着時の実風量ではなく基礎情報としてのみ使う。

### Airflow Model
#44でFront / Rear / Topを個別にPWM sweepし、以下を決める。
- PWM → RPM curve
- 起動PWM
- 最低安定PWM / RPM
- 最大RPM
- 応答時間
- zone別Airflow Index（0〜1）
- 固定熱負荷に対するzone別の冷却効果

制御判断は `Airflow Index + 実際の熱応答` を使い、絶対CFMの一致を目標にしない。

## やること（設計のみ。コードを書かない）

- [ ] `docs/decisions/NNNN-fan-control-safety.md` を作成
- [ ] Front / Rear / Topそれぞれの物理headerと `pwmX` / `fanX_input` 対応を特定
- [ ] RearをFront Hubから分離し、独立PWM制御できることを確認
- [ ] Topをcoldaisle管理にした際のCPU冷却安全条件を定義
- [ ] CPU温度 / CPU powerから `cpu_cooling_demand` を生成する方法を定義
- [ ] `case_aux_exhaust_demand` の開始 / 解除条件を #19 / #44 の実測から定義
- [ ] Front / Rear / Topそれぞれの最低PWM / 起動PWM / 安全下限を実測で確定
- [ ] 各zoneのtach stall判定を定義
- [ ] NVML取得失敗時のフェイルセーフを定義
- [ ] CPU telemetry取得失敗時はTopを安全側へ倒す
- [ ] T_SENSOR取得失敗時のフェイルセーフを定義
- [ ] fan daemon異常終了・SIGTERM・SIGKILL相当・OS shutdown / restart時の振る舞いを確認
- [ ] 起動直後 / センサー未初期化中は安全側の高回転とする
- [ ] `hwmonX`番号を固定せず、driver名/label/安定属性から探索する方法を決める
- [ ] AIからfan daemonへの到達経路を作らないことを構造で保証する

## フェイルセーフ原則

重大なtelemetry / control faultでは、制御可能な正常Fanを安全側へ上げる。

特にTopはCPU冷却を担うため、以下を原則100%または実機で確認済みのSafe PWMとする。
- CPU temperature telemetry loss
- Top hwmon write failure
- Top tach stall
- fan daemon起動直後 / state未確定
- 設定不正

Front / RearもNVML loss、主要外気センサーstale、tach異常、heartbeat期限切れ等では安全側へ倒す。

AIO Pumpはcoldaisleから制御せず、fan daemonとは独立した安全設定を維持する。

## 受入基準

- Front / Rear / Topを独立PWMで制御する設計になっている
- RearがFront Hubから分離されている
- Topはアプリ管理で、`max(cpu_cooling_demand, case_aux_exhaust_demand, safety_floor)`の考え方を守る
- CPU telemetry loss時のTop安全動作が定義されている
- #44のAirflow Modelを使い、ファン径×RPMだけで風量を決めない
- 「ソフトウェアが停止・再起動しても危険な冷却停止にならない」ことを実機で確認する計画がある
- 対象外Fanへ誤ってPWMを書かない識別手順がある
- fault injection（NVML停止、CPU telemetry停止、センサー欠測、daemon kill、tach異常）の期待動作が決まっている
- 人間レビューでADRが承認されるまで #43 の実装を開始しない

## 依存
#34, #44, #19
