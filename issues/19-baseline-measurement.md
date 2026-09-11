---
title: "ベースライン測定と閾値の確定"
labels: qa, priority:must
milestone: "M4 アラート"
---

## 背景
実測なしで決めた閾値はアラート・ファン制御を形骸化させる（要件 R-04）。
GPUサーバー実機と自作センサーモジュールを使い、温度監視と段階式ファン制御の基準値を作る。

現在のFan topology:
- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top Exhaust / CPU Radiator: Cooler Master MasterLiquid Atmos II、120mm Fan ×3

## やること

### 基本ベースライン
- [ ] 無負荷で2時間測定
- [ ] 軽負荷（推論）で1時間測定
- [ ] フルロード（学習 or ストレステスト）で1時間測定
- [ ] エアコンON/OFFの差分測定
- [ ] 各状態で `d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta` / `d.case_delta` の分布を算出
- [ ] GPU内部メトリクス（#34完了後）と外気系メトリクスの相関を確認

### Fan制御用ベースライン
- [ ] BIOS/Q-Fan Autoで同一GPU負荷を測定
- [ ] **Front + Rear Fan Hubを高回転 / 100%** にして同一負荷を測定（Topは通常のCPU/BIOS管理）
- [ ] Front + Rearを上げたときの `d.case_delta` / GPU Intake / GPU core / hotspot 改善量を記録
- [ ] 安全に実施できる場合のみ、Top Radiator Fanを追加で上げた状態を測定し、**Top補助による追加改善量**を記録
- [ ] Topを上げた測定ではCPU温度 / CPU powerも同時記録し、CPU冷却との関係を確認
- [ ] Front + Rearだけで十分な領域と、Top補助が必要になる領域を整理

## 追加で記録すること

- GPU power / GPU core / hotspot
- CPU temperature / CPU power（取得可能な範囲）
- Front Intake / GPU Intake / GPU Exhaust / Top Exhaust / Rear Exhaust / Room
- Front + Rear Fan Hub RPM / PWM（#34完了後）
- Top Radiator Fan RPM / PWM / control mode（#34完了後、取得可能な範囲）
- AIO Pump RPM（参照のみ）
- T_SENSOR（12V-2x6外装、#34完了後）

## 風量評価

絶対CFMの実測は受入条件にしない。

- メーカー公称の最大風量 / 最大RPM / 静圧は参考値として記録可能
- 実運用ではPWM duty、RPM、`RPM / 最大RPM`を相対的なAirflow Proxyとして扱う
- FrontとTopは通気抵抗が異なるため、RPMや公称CFMを単純加算して吸排気バランスを断定しない
- **温度差とコンポーネント温度の改善量を最終評価にする**

## 閾値確定

- [ ] p99 + マージンで `config/rules.yaml` の監視閾値を確定
- [ ] #30向けにFront + Rear主制御のカーブ候補を作る
- [ ] #30向けにTop補助開始条件 / 継続時間 / 解除条件の候補を作る
- [ ] `docs/baseline-YYYY-MM-DD.md` に記録

## 受入基準

- 通常運用で誤警報が1週間で0件
- 意図的にフロント吸気を塞ぐと `RECIRCULATION` または `INTAKE_HIGH` が発火する
- BIOS Auto とFront + Rear高回転の比較結果が文書化されている
- Front + Rearだけで熱を捌ける条件が説明できる
- Top補助を使うなら、その追加効果と発動根拠が実測で説明できる
- #30 / #43 の段階式制御に使う閾値候補が得られている

## 依存
#18, #34
