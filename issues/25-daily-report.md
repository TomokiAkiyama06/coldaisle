---
title: "日次レポート生成"
labels: ai, priority:should
milestone: "M5 AI"
---

## やること
- [ ] 毎朝、前日分の統計（最高/最低/平均、アラート件数、前日比、欠測率）を集計
- [ ] LLMで要約文を生成
- [ ] Slackへ投稿（既存の朝のブリーフィングに合流させるか要検討）
- [ ] Markdownで `docs/reports/` にも保存

## 依存
#22, #20
