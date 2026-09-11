---
title: "GPU Mode イベントの記録とタイムライン注釈"
labels: core, integration, priority:should
milestone: "M7 拡張"
---

## 背景
AI ModeとCompute Modeでは熱的な条件が大きく異なる。
Mode変化を記録しないと、温度変化の原因を後から説明できない。

## 要設計判断
現行v1 APIは **GET-only** を構造的に保証しているため、Core APIへそのまま`POST /api/v1/events`を追加すると既存の安全契約を破る。
実装前に、次のどちらかをADRで確定すること。

1. **推奨:** 書き込み専用のlocalhost/Unix socket入口をread-only APIから分離する
2. v2 APIとして明示的に書き込み契約を導入する

AI/LLMがこの入口を直接呼べる構成にはしない。

## やること
- [ ] `events` テーブル（`ts_ms`, `kind`, `payload`）
- [ ] Mode変更イベントの入力経路をADRで確定
- [ ] WorkspaceのGPU ManagerからMode変更を通知
- [ ] `sys.gpu_mode` として参照可能にする
- [ ] ダッシュボードのグラフへ縦線で注釈
- [ ] `PROBE_CHANGED` / device reset等の既存イベントを統合できる設計にする
- [ ] `sys.gpu_state = mixed` を検出したら「GPUモード切替が失敗している」と警告

## セキュリティ
- localhostまたはUnix socket限定
- 受理するevent kindをホワイトリスト化
- AIツール一覧には書き込み入口を公開しない
- read-only APIのOpenAPI契約を壊さない方式を優先

## 受入基準
- Mode遷移と温度・電力の時系列を後から重ねられる
- 未認可のevent kindを拒否する
- LLMからModeイベントの書き込み経路へ到達できない
- GET-only契約を変更する場合は、破壊的変更としてapi-contractとテストを更新する

## 依存
#9, #35
