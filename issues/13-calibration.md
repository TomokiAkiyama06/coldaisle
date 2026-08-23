---
title: "センサー較正手順と calibration.json"
labels: hardware, priority:must
milestone: "M2 実機接続"
---

## 背景
DS18B20 ±0.5℃、AM2320 ±0.5℃。ΔT判定には最悪±1.0℃の系統誤差が乗る
（`docs/spec-review.md` W-02）。再循環検出は数℃の話なので無視できない。

## やること
- [ ] `uv run coldaisle-calibrate` コマンドを実装
      - 指定時間データを収集し、各メトリクスの平均を算出
      - 全体平均を基準に各センサーのオフセットを計算
      - `config/calibration.json` を出力
- [ ] `docs/calibration.md` に実行手順を記載（前提となる物理配置の条件を含む）
- [ ] 取り込み時にオフセットを適用（#8 と接続）
- [ ] 較正日時をJSONに記録し、6ヶ月以上経過したら警告

## 受入基準
- 較正後、全センサーを同一環境に置いたときのばらつきが ±0.15℃ 以内

## 依存
#11, #12
