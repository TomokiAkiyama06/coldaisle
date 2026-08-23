---
title: "Webダッシュボード刷新（API経由化）"
labels: ui, priority:must
milestone: "M3 UI"
---

> **⚠ v0.2で優先度変更**: 本番UIは Personal AI Workspace 側（Tauri + React）が担当します。
> このダッシュボードは **開発用・フォールバック用**に留め、作り込まないでください。
> 詳細: `docs/api-contract.md`。

## 背景
既存 `server_sensor_dashboard` は自前でシリアルを開いている。API経由に作り替える。

## やること
- [ ] 現在値カード（7メトリクス + quality バッジ）
- [ ] 派生値: `d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta`
- [ ] 履歴グラフ（全温度 / 湿度）。期間切替 1h / 6h / 24h / 7d
- [ ] `agg` を期間に応じて自動選択（24h以上は `1m`）
- [ ] WebSocketでのライブ更新
- [ ] アラート一覧（FIRING / RESOLVED）
- [ ] `SENSOR_FAULT` 時は画面全体に明示的な警告を出す（無音で古い値を出し続けない）
- [ ] 接続断時の再接続インジケータ

## 受入基準
- MockSourceの `sensor_fail` シナリオで、該当カードが明確に異常表示になる
- デーモン停止時に「データが古い」ことが一目でわかる

## 依存
#9
