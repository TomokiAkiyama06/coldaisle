---
title: "ベースライン測定と閾値の確定"
labels: qa, priority:must
milestone: "M8 Fan Control"
moved_to: 50
---

> **この定義は GitHub #50 へ移管しました。** M8 以降の Issue は GitHub の本文が正本です（2026-09-13）。
> 以下は移管前の記録で、更新しません。`scripts/create_issues.sh` は作成・更新しません。

## 背景
実測なしで決めた閾値はアラート・ファン制御を形骸化させる（要件 R-04）。
GPUサーバー実機と自作センサーモジュールを使い、温度監視と3系統Fan制御の基準値を作る。

現在のFan topology:
- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1（Frontから分離）
- Top Exhaust / CPU Radiator: Cooler Master MasterLiquid Atmos II 360
- AIO Pump: BIOS / 固定安全設定

## やること

### 基本ベースライン
- [ ] 無負荷で2時間測定
- [ ] 軽負荷（推論）で1時間測定
- [ ] GPUフルロードで1時間測定
- [ ] CPUフルロードで1時間測定
- [ ] CPU + GPU同時高負荷を安全な範囲で測定
- [ ] エアコンON/OFFの差分測定
- [ ] 各状態で `d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta` / `d.case_delta` の分布を算出
- [ ] GPU/CPU内部メトリクス（#34完了後）と外気系メトリクスの相関を確認

### 3-zone Fanベースライン
#44のPWM→RPM characterization完了後に、同じ熱負荷でzone別の効果を比較する。

- [ ] Frontのみ段階的に変更し、GPU Intake / GPU温度 / Case ΔTへの効果を記録
- [ ] Rearのみ段階的に変更し、Rear Exhaust / Case ΔT / GPU Intakeへの効果を記録
- [ ] Front / Rearの組み合わせを複数測定し、通常換気の有効領域を把握
- [ ] Topのみ段階的に変更し、CPU温度とケース排気への両方の効果を記録
- [ ] GPU高負荷でFront + Rearだけで不足する条件を確認
- [ ] その状態からTopを追加したときの改善量を記録
- [ ] CPU高負荷時に必要なTop最低要求を測定
- [ ] CPU + GPU同時高負荷時のTop要求を測定

## 追加で記録すること

- GPU power / GPU core / hotspot
- CPU temperature / CPU power
- Front Intake / GPU Intake / GPU Exhaust / Top Exhaust / Rear Exhaust / Room
- Front zone RPM / PWM / Airflow Index
- Rear zone RPM / PWM / Airflow Index
- Top zone RPM / PWM / Airflow Index
- AIO Pump RPM（参照のみ）
- T_SENSOR（12V-2x6外装）

## 風量評価

絶対CFMの実測は受入条件にしない。

- メーカー公称値はpriorとして記録
- #44でPWM→RPM curve、最低安定PWM、Airflow Indexを作る
- Front / Rear / Topの効果を固定熱負荷で比較する
- 公称CFMやRPMを単純加算して吸排気バランスを断定しない
- **温度差・GPU Intake・CPU/GPU温度の改善量を最終評価にする**

## 閾値・制御値確定

- [ ] p99 + マージンで `config/rules.yaml` の監視閾値を確定
- [ ] Front control curve候補を作る
- [ ] Rear control curve候補を作る
- [ ] Front / Rearの協調ルール候補を作る
- [ ] CPU温度 / powerに対するTop `cpu_cooling_demand` 候補を作る
- [ ] ケース排熱不足に対するTop `case_aux_exhaust_demand` の開始条件 / 継続時間 / 解除条件を作る
- [ ] `docs/baseline-YYYY-MM-DD.md` に記録

## 受入基準

- 通常運用で誤警報が1週間で0件
- 意図的にフロント吸気を塞ぐと `RECIRCULATION` または `INTAKE_HIGH` が発火する
- Front / Rear / Topを独立変更した効果が文書化されている
- Front + Rearだけで熱を捌ける領域が説明できる
- Rearを追加で上げる効果が説明できる
- TopのCPU冷却要求とケース補助排気効果が別々に説明できる
- #30 / #43 の3-zone制御に使う閾値候補が得られている

## 依存
#18, #34, #44
