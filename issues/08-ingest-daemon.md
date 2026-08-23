---
title: "ingest daemon 骨格（シリアルポートの単一所有者）"
labels: core, priority:must
milestone: "M1 データ基盤"
---

## 背景
元仕様ではモニタとダッシュボードがそれぞれシリアルを開くため同時起動できない
（`docs/spec-review.md` C-03）。所有者を1プロセスに集約する。

## やること
- [ ] `Source → Normalizer → Store` のパイプライン
- [ ] 較正オフセット（`config/calibration.json`）の適用（FR-107）
- [ ] `seq` 不連続の検出とカウント
- [ ] `up` 巻き戻り（デバイス再起動）の検出とイベント記録
- [ ] 構造化ログ（JSON Lines）
- [ ] SIGTERM でのグレースフルシャットダウン
- [ ] **1サンプルのパース失敗でプロセスを落とさない**

## 受入基準
- MockSourceで24時間相当（時間圧縮）を流しても停止しない
- 不正な行を混ぜても継続し、破棄件数がログに出る

## 依存
#5, #6
