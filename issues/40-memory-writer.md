---
title: "Markdown Decision Memory への自動記録"
labels: integration, priority:should
milestone: "M5 AI"
---

## 背景
構想メモ §17-§22 の Memory 方針を運用記録へ適用する。
較正値・閾値は運用中に変わるため、現在値と履歴を1か所に保つ。

センサーモジュールの固定BOMは原則 **DS18B20 ×5 + AM2320 ×1** とする。
同じBOMを継続して使う限り、部品構成そのものを毎回Decision Memoryへ記録・履歴化しない。

## やること
- [x] `memory/projects/gpu-server.md` へ追記するWriterを実装
- [x] YAML Front Matter + Markdown形式
- [x] 記録対象:
  - 較正の実施（#13）とプローブごとのオフセット値
  - ベースライン測定結果（#19）と確定した閾値
  - DS18B20 ROM ID → 設置位置の対応
  - 同じプローブの配置変更
  - プローブ交換でROM IDが変わった場合
  - プローブ入れ替えの検出（#14）
- [x] 固定BOM（DS18B20 ×5 + AM2320 ×1）は通常のHistory対象にしない
- [x] Current FactsとHistoryを分離
- [x] 全自動保存にせず、確認を経由
- [x] Git commitを伴える

## 管理する設置位置
- Front Intake
- GPU Intake
- GPU Exhaust
- Top Exhaust
- Rear Exhaust

## 原則

管理したいのは「何個のDS18B20を使っているか」ではなく、
**どの物理プローブがどこを測っているか**と**その個体の較正値**である。

固定BOM自体を変更した場合は通常の仕様変更としてREADME / requirements / Issue等を更新し、
日々のDecision Memoryへ重複して積み上げない。

## 受入基準
- 現在の閾値・較正値・ROM ID→設置位置がMarkdown 1ファイルを見れば分かる
- 過去の値もSupersededとして残り、検索で誤って最新扱いされない
- 固定BOMを変更していないのに同じ構成情報が履歴へ繰り返し追加されない

## 依存
#13, #19

## 2026-08-26 の実装（決定記録 0020）
- `coldaisle-memory` が設定とDBから事実を集め、差分を見せる
- Current Factsは印で囲んだ区画、Historyは追記
- 古い記録に`Superseded by`を追記

較正・ベースラインの実測値は各Issue完了後に記録される。
