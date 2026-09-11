---
title: "Markdown Decision Memory への自動記録"
labels: integration, priority:should
milestone: "M5 AI"
---

## 背景
構想メモ §17-§22 の Memory 方針を運用記録へ適用する。
較正値・閾値は運用中に何度も変わるため、現在値と履歴を1か所に保つ。

## やること
- [x] `memory/projects/gpu-server.md` へ追記するWriterを実装
- [x] YAML Front Matter + Markdown形式
- [x] 記録対象:
  - 較正の実施（#13）とオフセット値
  - ベースライン測定結果（#19）と確定した閾値
  - センサー構成の変更（DS18B20 ROM mapping / 配置変更など）
  - プローブ入れ替えの検出（#14）
- [x] Current FactsとHistoryを分離
- [x] 全自動保存にせず、確認を経由
- [x] Git commitを伴える

## 受入基準
- 現在の閾値・較正値がMarkdown 1ファイルを見れば分かる
- 過去の値もSupersededとして残り、検索で誤って最新扱いされない

## 依存
#13, #19

## 2026-08-26 の実装（決定記録 0020）
- `coldaisle-memory` が設定とDBから事実を集め、差分を見せる
- Current Factsは印で囲んだ区画、Historyは追記
- 古い記録に`Superseded by`を追記

較正・ベースラインの実測値は各Issue完了後に記録される。
