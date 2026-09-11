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
  - DS18B20 の **ROM ID → 物理配置** の割当変更
  - プローブ入れ替えの検出（#14）
- [x] Current FactsとHistoryを分離
- [x] 全自動保存にせず、確認を経由
- [x] Git commitを伴える

## 固定ハードウェア構成の扱い

本プロジェクトのセンサーモジュールは、原則として **DS18B20 ×5 + AM2320 ×1** を固定構成として扱う。
同じBOMを継続して使う限り、ハードウェア構成そのものをDecision Memoryへ毎回記録・履歴化しない。

履歴として管理するのは、運用上意味が変わる以下のみ:

- DS18B20 の ROM ID と設置位置の対応
  - Front Intake
  - GPU Intake
  - GPU Exhaust
  - Top Exhaust
  - Rear Exhaust
- 各プローブの較正オフセット
- 同じプローブを別の測定位置へ移した場合の配置変更
- プローブ交換により ROM ID が変わった場合

つまり、**固定BOMの履歴ではなく「どの個体がどこを測っているか」と「その個体の較正値」を管理する**。

## 受入基準
- 現在の閾値・較正値がMarkdown 1ファイルを見れば分かる
- 現在の ROM ID → 物理配置の対応が分かる
- 固定BOMを変更していない限り、同一構成の重複履歴を作らない
- 過去の値もSupersededとして残り、検索で誤って最新扱いされない

## 依存
#13, #19

## 2026-08-26 の実装（決定記録 0020）
- `coldaisle-memory` が設定とDBから事実を集め、差分を見せる
- Current Factsは印で囲んだ区画、Historyは追記
- 古い記録に`Superseded by`を追記

較正・ベースラインの実測値は各Issue完了後に記録される。
