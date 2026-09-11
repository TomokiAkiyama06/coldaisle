---
title: "ベースライン測定と閾値の確定"
labels: qa, priority:must
milestone: "M4 アラート"
---

## 背景
実測なしで決めた閾値はアラートを形骸化させる（要件 R-04）。
GPUサーバー実機と自作センサーモジュールが揃ったため、現在は着手可能。

## やること
- [ ] 無負荷で2時間測定
- [ ] 軽負荷（推論）で1時間測定
- [ ] フルロード（学習 or ストレステスト）で1時間測定
- [ ] エアコンON/OFFの差分測定
- [ ] ケースファン **BIOS/Q-Fan Auto** と **100%固定** を同一負荷で比較
- [ ] 各状態での `d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta` / `d.case_delta` の分布を算出
- [ ] GPU内部メトリクス（#34完了後）と外気系メトリクスの相関を確認
- [ ] p99 + マージンで `config/rules.yaml` の閾値を確定
- [ ] `docs/baseline-YYYY-MM-DD.md` に記録

## 追加で記録すること
- GPU power / GPU core / hotspot
- Front Intake / GPU Intake / GPU Exhaust / Top Exhaust / Rear Exhaust / Room
- ケースファン RPM / PWM（#34完了後）
- T_SENSOR（12V-2x6外装、#34完了後）

## 受入基準
- 通常運用で誤警報が1週間で0件
- 意図的にフロント吸気を塞ぐと `RECIRCULATION` または `INTAKE_HIGH` が発火する
- BIOS Auto とケースファン100%の比較結果が文書化され、制御設計（#30）の根拠として使える

## 依存
#18, #34
