---
title: "GPU / CPU / VRM 内部センサーの統合"
labels: core, priority:could, blocked-by-hardware
milestone: "M7 拡張"
---

> **⚠ v0.2で格上げ・統合**: 本Issueは **#34** に統合され、v1スコープへ格上げされました。
> #34 を参照してください。

## 背景
元仕様 §18-8。外気温だけでなく筐体内部の値と突き合わせることで、
「吸気が高いのか、冷却が効いていないのか」を切り分けられるようになる。

## やること
- [ ] `nvidia-smi` から `gpu.0.core` / `gpu.0.hotspot` / `gpu.0.mem` / `power.gpu.0`
- [ ] `lm-sensors` から `cpu.package` / `cpu.vrm` / `chipset`
- [ ] 同一の `readings` テーブルへ投入（ロング形式にした恩恵。**スキーマ変更不要**）
- [ ] 相関ルール追加: 「GPU coreは高いが吸気は正常」→ 冷却系の問題

## 依存
#26, #3
