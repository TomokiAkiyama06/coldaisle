---
title: "Personal AI Workspace の Server Health 統合"
labels: integration, priority:could
milestone: "M7 拡張"
---

> **⚠ v0.2で具体化**: 統合インターフェースは **#35（Server Health API）** で定義されました。
> 本Issueは Workspace 側のパネル実装に範囲を限定します。

## 背景
元仕様 §18-9。既存の個人自動化基盤（Notion / Slack / Calendar）と同じ場所で見られるようにする。

## やること
- [ ] Server Health のサマリを返すエンドポイント（信号色 + 一行サマリ）
- [ ] Workspace側のウィジェットへ表示
- [ ] 朝のSlackブリーフィングにサーバー状態を1行追加

## 依存
#9, #25
