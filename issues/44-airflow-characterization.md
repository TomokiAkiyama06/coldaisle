---
title: "3系統Fanの風量キャラクタライズとAirflow Model"
labels: design, qa, priority:must
milestone: "M8 Fan Control"
moved_to: 75
---

> **この定義は GitHub #75 へ移管しました。** M8 以降の Issue は GitHub の本文が正本です（2026-09-13）。
> 以下は移管前の記録で、更新しません。`scripts/create_issues.sh` は作成・更新しません。

## 背景
Front / Rear / Topを独立PWM制御するにあたり、ファン径やRPMだけから風量を推定すると誤差が大きい。

現在のFan topology:
- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top Exhaust / CPU Radiator: Cooler Master MasterLiquid Atmos II 360

Frontはケース前面、Topはラジエーターという異なる通気抵抗を受ける。Rearは純正Fanで公開性能が不明な可能性がある。

そのため、メーカー公称値をpriorとして使いつつ、**実機のPWM→RPM特性と固定熱負荷に対する温度応答からzone別Airflow Modelを作る。**

## 原則

- ファン径 × RPMで絶対CFMを算出しない
- メーカー公称CFMをケース装着時の実CFMとはみなさない
- 風量計が無くても成立するモデルにする
- 絶対CFMではなくzoneごとの相対 `airflow_index`（0〜1）を主に使う
- 最終評価は温度応答で行う

## Manufacturer prior

既知の公称値:

### Front — Noctua NF-A12x25 G2
1基あたり:
- max RPM: 1800
- max airflow: 63.15 CFM
- max static pressure: 3.14 mmH2O

3基合計のfree-air公称値は約189.45 CFMだが、ケース前面抵抗を含まないため実風量とはみなさない。

### Top — Cooler Master MasterLiquid Atmos II 360
360mmユニット公称:
- fan speed: 690〜2500 RPM
- max airflow: 190 CFM
- max static pressure: 3.61 mmH2O

ラジエーター装着状態の実流量は公称値から直接決めない。

### Rear — Antec FLUX純正Rear Fan
- 実機ラベル / 型番を確認
- max RPMを実測
- メーカー単体仕様が得られればmetadataに保存
- 得られない場合は実測characterizationのみで扱う

## Phase 1: PWM → RPM characterization

Front / Rear / Topを1zoneずつ個別に測定する。

- [ ] 物理headerとzoneの対応を確定
- [ ] 0 / 10 / 20 / ... / 100% PWMで安全に測定可能な範囲をsweep
- [ ] 起動できる最低PWMを測定
- [ ] 回転維持できる最低PWMを測定
- [ ] 各PWMでsteady RPM / 分散を記録
- [ ] 最大RPMを実測
- [ ] PWM stepへのRPM応答時間を測定
- [ ] 上昇時 / 下降時のヒステリシスがあるか確認

停止領域が存在する場合、通常制御では使用しない。

## Phase 2: Airflow Index

zoneごとに単調な相対指標を定義する。

```text
airflow_index(zone) ∈ [0, 1]
```

最初はPWM→RPM実測曲線を基礎にするが、`rpm / max_rpm` の単純直線に固定しない。
必要に応じてpiecewise linear / lookup tableで表現する。

例:

```text
fan_model.front:
  pwm_to_rpm: [...]
  startup_pwm: ...
  minimum_stable_pwm: ...
  measured_max_rpm: ...
  airflow_index_curve: [...]
```

## Phase 3: Installed thermal effectiveness

一定のGPU / CPU熱負荷で1zoneだけ変更し、温度応答を測る。

### Front sweepで見るもの
- GPU Intake
- `d.gpu_preheat`
- GPU core / hotspot
- `d.case_delta`

### Rear sweepで見るもの
- Rear Exhaust
- `d.case_delta`
- GPU Intake
- GPU core / hotspot

### Top sweepで見るもの
- CPU temperature
- Top Exhaust
- `d.case_delta`
- GPU Intake / GPU temperature

- [ ] GPU固定負荷でFront sweep
- [ ] GPU固定負荷でRear sweep
- [ ] GPU固定負荷でTop sweep
- [ ] CPU固定負荷でTop sweep
- [ ] CPU + GPU同時負荷でTop sweep

## Phase 4: Cross-zone effectiveness

各zoneを10%または一定Airflow Indexだけ増やしたとき、どの指標がどれだけ改善するかを比較する。

概念的に以下のresponse matrixを作る。

```text
                  GPU Intake   case ΔT   GPU temp   CPU temp
Front +Δ             ...         ...       ...        ...
Rear  +Δ             ...         ...       ...        ...
Top   +Δ             ...         ...       ...        ...
```

この結果を使い、通常はFront / Rearのうち効果の高いzoneを先に調整し、Topのcase補助要求は必要時だけ増やす。

## Air balanceの考え方

吸気CFM = 排気CFM の厳密一致を目標にしない。

圧力センサーや風量計を常設しないため、吸排気バランスは以下で評価する。
- GPU Intakeが室温から過度に上昇していない
- `d.case_delta`が許容範囲
- Rear / Topを増やしたときに熱応答が改善する
- GPU / CPU温度が許容範囲
- 不要に全Fanを高回転へしない

必要なら一時的な風速計測は検証補助として使えるが、v1の必須条件にはしない。

## 成果物

- [ ] `config/fans.yaml` または同等のFan metadata
- [ ] zone別 PWM→RPM lookup
- [ ] startup / minimum stable / max RPM
- [ ] zone別 Airflow Index curve
- [ ] thermal effectiveness response matrix
- [ ] `docs/airflow-characterization-YYYY-MM-DD.md`

## 受入基準

- Front / Rear / TopのPWM→RPM curveが実測されている
- 各zoneの起動PWM / 最低安定PWM / 最大RPMが分かる
- `rpm / max_rpm`だけに依存しないAirflow Indexが定義されている
- Front / Rear / Topそれぞれを増やしたときの熱的な効果を説明できる
- RearとTopのどちらを先に上げるべきか、固定負荷試験を根拠に説明できる
- 絶対CFMを測れなくても #30 / #43 の制御設計に必要な情報が得られる

## 依存
#34
