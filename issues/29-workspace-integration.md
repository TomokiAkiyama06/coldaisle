---
title: "Personal AI Workspace の Server Health 統合"
labels: integration, priority:could
milestone: "M7 拡張"
---

> **v0.2で具体化**: 統合インターフェースは #35（Server Health API）が担当する。
> 本Issueは **Workspace側の表示・利用** に範囲を限定する。

## 背景
coldaisleの監視結果を Personal AI Workspace の同じ画面から確認できるようにする。

## やること
- [ ] Workspace側のServer Healthパネルを実装
- [ ] `GET /api/v1/server-health` だけを参照し、Workspace側から `nvidia-smi` / lm-sensors / ESP32を直接読まない
- [ ] signal / summary / sources / compute_mode_advisory を表示
- [ ] GPU Modeイベントを送る場合は #36 の確定した契約に従う
- [ ] 朝のブリーフィングへサーバー状態を1行追加するかはWorkspace側で判断

## 受入基準
- coldaisle内部実装を知らず、`docs/api-contract.md` だけでパネルを実装できる
- coldaisle Coreが生きていれば、GPU AI Service停止中でも状態を表示できる

## 依存
#35, #25
