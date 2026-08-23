---
title: "Server Health API（Workspace連携の単一窓口）"
labels: api, integration, priority:must
milestone: "M7 拡張"
---

## 背景
Personal AI Workspace の GPU パネルが叩く唯一のエンドポイント。
スキーマは `docs/api-contract.md` に定義済み。

## やること
- [ ] `GET /api/v1/server-health` を実装
- [ ] `signal`（green/yellow/red）は **決定論的に**決める。AIは関与しない
- [ ] `summary` はAIが生成。**AI停止時は決定論的テンプレートへフォールバック**
- [ ] `sources` で各情報源の生死を返す（sensor_unit / nvml / lm_sensors / ai_layer）
- [ ] `compute_mode_advisory` を返す（#37 と接続）
- [ ] WebSocket でも同じペイロードをpush

## 受入基準
- **センサーが死んでいるのに `signal: green` にならない**こと
- AI停止時も全フィールドが埋まる（`summary` がテンプレート文になるだけ）
- Workspace 側は `nvidia-smi` を一切叩かずにGPUパネルを描画できる

## 依存
#9, #34
