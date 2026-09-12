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

## 2026-09-10 の実装（決定記録 0024）

- `coldaisle-calibrate` を追加。**DB から読む**（シリアルは開かない。AGENTS.md ルール6）
- **ばらつきが 2.0℃ を超えたら受け付けない**（本物の温度勾配を焼き付けない）
- `calibrated_at` / `reference` / `samples` / `revalidate_after_days` を記録に残す
- 未較正・期限切れはデーモン起動時に警告
- 手順は `docs/calibration.md`

**実機での較正は未了。** 受入基準（±0.15℃）は、模擬データで手法が成立することを
試験で固定しただけ（別区間へ当てて確認）。**実際の確認は人が行う。**
