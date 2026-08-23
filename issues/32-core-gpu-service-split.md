---
title: "Core Service と GPU AI Service の分離（Compute Mode対応）"
labels: infra, priority:must, safety
milestone: "M6 移行"
---

## 背景
Workspace構想メモ §36「Local AIは余剰GPU資源を使うバックグラウンドサービスであり、
本来のCompute Workloadが来たら即座に退く」に従う。

**Compute Mode（Kaggle実行中）こそGPUが最も熱くなる時間帯であり、そこで監視が止まる設計はあり得ない。**
逆に、その間AI説明が使えないことは一切問題にならない。

## やること
- [ ] Core Service（GPU不要・常時稼働）と GPU AI Service（停止可能）へデプロイ単位を分割
  - Core: `coldaisle-daemon` / `coldaisle-api` / rules / notifier / SQLite / dashboard
  - GPU AI: LLM説明・チャット・日次レポート
- [ ] GPU AI Service 停止時、`GET /api/v1/server-health` の `sources.ai_layer` が `stopped` になる
- [ ] `summary` フィールドが決定論的テンプレートへフォールバックする
- [ ] チャットUIは「AI停止中（Compute Mode）」と明示する
- [ ] **`--gpu-memory-utilization` による制限ではなくプロセス完全停止**でVRAMを解放する
- [ ] 停止判定は「Dockerを止めた」ではなく **NVMLでCUDAプロセス数=0** で行う（構想メモ §39）

## 受入基準
- GPU AI Service を停止した状態で、ダッシュボード・アラート・通知・API がすべて正常動作する
- Compute Mode 中も温度データが欠測しない
- AI由来のCUDAプロセスが0であることをAPIで確認できる

## 依存
#21, #26
