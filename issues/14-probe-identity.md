---
title: "DS18B20 ROM IDによるプローブ同定と入れ替わり検出"
labels: core, priority:should
milestone: "M2 実機接続"
---

## 背景
現状は配線位置でのみセンサーを同定している。ケース内作業で差し替えると
較正値もラベルも静かに間違ったまま運用が続く（`docs/spec-review.md` W-03）。

## やること
- [ ] `hello` の `rom` を `devices` テーブルへ保存
- [ ] 起動ごとに前回値と比較し、不一致なら `PROBE_CHANGED` イベント
- [ ] ダッシュボードに現在のROM IDマッピングを表示
- [ ] 物理プローブへのラベル貼付を `docs/calibration.md` に手順として記載

## 依存
#11, #12

## 2026-09-10 の完了（決定記録 0025）

- `hello` の `rom` の保存と `PROBE_CHANGED` は #18 / #8 で実装済み
- **残っていた2項目**を実装した
  - `GET /api/v1/devices` とダッシュボードの「センサー構成」（ROM の一覧）
  - `docs/calibration.md` の手順0（ラベル貼付と ROM の突き合わせ）

**#11 のレビューで見つかった穴も塞いである** — 起動時に繋がっていなかった
プローブの ROM を忘れないこと（`rules.probe_mismatch()`）。
