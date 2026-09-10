---
title: "Markdown Decision Memory への自動記録"
labels: integration, priority:should
milestone: "M5 AI"
---

## 背景
構想メモ §17-§22 の Memory 方針を運用記録へ適用する。
**較正値・閾値は運用中に何度も変わる。** 会話履歴の中に散らばると、
3ヶ月後に「今の閾値はいくつだっけ」が分からなくなる。

## やること
- [ ] `memory/projects/gpu-server.md` へ追記する Writer を実装
- [ ] YAML Front Matter + Markdown 形式（構想メモ §19）
- [ ] 記録対象:
  - 較正の実施（#13）とオフセット値
  - ベースライン測定結果（#19）と確定した閾値
  - センサー構成の変更（#16 の SHT45換装等）
  - プローブ入れ替えの検出（#14）
- [ ] **Current Fact と History を分ける**（構想メモ §21）。
      現在の較正値・閾値は必ず1箇所に置き、過去は `Supersedes` 付きで残す
- [ ] **全自動保存にせず、確認UIを経由**する（構想メモ §23、要件 Q-12）
- [ ] Git commit を伴う（人間が差分を追える）

## 記録例
```markdown
## Decisions

### 2026-09-10
Decision: RECIRCULATION 閾値を 5.0°C → 4.2°C へ変更
Status: FINAL
Evidence: baseline-2026-09-08.md、無負荷時 p99 = 3.1°C
Supersedes: 暫定値 5.0°C（2026-08-23）
```

## 受入基準
- 現在の閾値・較正値が Markdown 1ファイルを見れば分かる
- 過去の値も Superseded として残っており、検索で誤って最新扱いされない

## 依存
#13, #19

## 2026-08-26 の実装（決定記録 0020）

- `coldaisle-memory` が設定と DB から事実を集め、**差分を見せる**（既定では書かない）
- Current Facts は印で囲んだ区画、History は積むだけ（0020 §2.3）
- 古い記録に **`Superseded by` を追記**する（0020 §2.4）

**較正の実施（#13）とベースライン（#19）は実機到着後**に入る。いまは設定ファイルの
値として記録される。**#39 から持ち越した「Claude の回答の記録」は未決**（0020 §5-2）。
