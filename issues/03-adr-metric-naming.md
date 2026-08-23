---
title: "ADR: メトリクス命名規約とDBスキーマ（ロング形式）の確定"
labels: design, priority:must
milestone: "M0 基盤"
---

## 背景
後から CPU / GPU / VRM / 12V-2x6 を追加する予定（元仕様 §18-8）。
ワイド形式（列=センサー）だと追加のたびに `ALTER TABLE` が必要になる。

## やること
- [ ] `docs/adr/0001-metric-naming.md` を作成
- [ ] メトリクス名を `<domain>.<name>` に確定（要件 §5.1）
- [ ] `readings` / `readings_1m` / `alerts` / `devices` のDDLを確定
- [ ] 派生メトリクス（`d.intake_rise` 等）は保存せず計算で出すことを明記
- [ ] マルチノード拡張のため `node_id` を予約するか判断して記録

## 受入基準
- DDLがレビュー済みで、以降の実装がこのADRを参照している
- ロング形式のトレードオフ（同時刻の横断クエリが複雑）と、その緩和策（`v_latest` ビュー）が書かれている

## 依存
#1
