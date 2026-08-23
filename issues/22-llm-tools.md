---
title: "ツール定義と実行ランタイム（読み取り専用）"
labels: ai, priority:must, safety
milestone: "M5 AI"
---

## 背景
8Bクラスのモデルは長大な数値テーブルに対する算術が不安定。
**集計はSQL側で行い、モデルには言語化だけをさせる**（要件 FR-504）。

## やること
- [ ] ツール定義（OpenAI function calling 形式）:
  - `get_latest()` — 現在値・派生値・quality
  - `query_series(metric, from, to, agg)` — **最大200点にダウンサンプル**
  - `get_stats(metric, window)` — min/max/mean/p95/傾き/欠測率
  - `list_alerts(from, to, state)`
  - `describe_system()` — センサー配置・GPIO割当・閾値の要約
- [ ] ツール実行ランタイム（引数バリデーション、実行、結果整形）
- [ ] **書き込み・実行系ツールを定義しない。任意SQLも受け付けない**
- [ ] デバイス由来文字列（`err` 等）はデータとして明示的にタグで囲む
- [ ] ツールコールのスモークテスト（パーサ設定がバージョンで変わるため必須）

## 受入基準
- 「昨日のGPU吸気温の最高値は？」に対し、`get_stats` を呼んで正しい数値を返す
- モデルが存在しないツールを呼んでもクラッシュしない
- コードベース全体を `subprocess` / `eval` で検索して `ai/` 配下にヒットが無い

## 依存
#21
