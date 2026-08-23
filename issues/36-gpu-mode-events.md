---
title: "GPU Mode イベントの記録とタイムライン注釈"
labels: core, integration, priority:should
milestone: "M7 拡張"
---

## 背景
AI Mode（GPU 200-300W断続）と Compute Mode（600W連続）では熱的な条件が全く異なる。
Mode変化を記録しないと「なぜ14時から吸気温が3°C上がったのか」が後から分からない。

**副次的効果**: Mode切り替えは**制御された自然実験**になる。
遷移前後を比較すれば `d.gpu_delta` や `d.intake_rise` の負荷依存性を高精度で測定できる。
Issue #19（ベースライン測定）はこれを利用すると効率的。

## やること
- [ ] `events` テーブル（`ts_ms`, `kind`, `payload`）
- [ ] `POST /api/v1/events` — **唯一の書き込み系エンドポイント**。localhost限定
- [ ] Workspace の GPU Manager から Mode変更を通知させる
- [ ] `sys.gpu_mode` メトリクスとしても記録
- [ ] ダッシュボードのグラフに縦線で注釈
- [ ] `PROBE_CHANGED` `device_reset` などの既存イベントも同テーブルへ統合

## セキュリティ
書き込みエンドポイントを1つ増やすため、**localhost以外からのアクセスを拒否**すること。
受け付けるkindをホワイトリストで限定する。

## 依存
#9, #35
