---
title: "読み取り専用REST API + WebSocket"
labels: api, priority:must
milestone: "M1 データ基盤"
---

## やること
- [ ] `GET /api/v1/latest`（現在値 + 派生値 + quality）
- [ ] `GET /api/v1/series?metric=&from=&to=&agg=`
- [ ] `GET /api/v1/stats?metric=&window=`
- [ ] `GET /api/v1/alerts`
- [ ] `GET /api/v1/health`（最終受信時刻、ソース種別、欠損率）
- [ ] `WS /api/v1/stream`
- [ ] 既定で `127.0.0.1` にバインド
- [ ] OpenAPI が自動生成される状態にする（後でAIツール定義に流用する）

## 受入基準
- **POST/PUT/DELETE のエンドポイントが1つも存在しない**
- `series` は最大点数を制限し、超過時は自動でダウンサンプルする

## 依存
#5, #8
