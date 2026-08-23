---
title: "ベースライン測定と閾値の確定"
labels: qa, priority:must, blocked-by-hardware
milestone: "M4 アラート"
---

## 背景
実測なしで決めた閾値はアラートを形骸化させる（要件 R-04）。
**GPUサーバー到着後にのみ実施可能。**

## やること
- [ ] 無負荷で2時間測定
- [ ] 軽負荷（推論）で1時間測定
- [ ] フルロード（学習 or ストレステスト）で1時間測定
- [ ] エアコンON/OFFの差分測定
- [ ] 各状態での `d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta` の分布を算出
- [ ] p99 + マージンで `config/rules.yaml` の閾値を確定
- [ ] `docs/baseline-YYYY-MM-DD.md` に記録

## 受入基準
- 通常運用で誤警報が1週間で0件
- 意図的にフロント吸気を塞ぐと `RECIRCULATION` または `INTAKE_HIGH` が発火する

## 依存
#18
