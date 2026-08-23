---
title: "NVML / lm-sensors の統合（v1スコープへ格上げ）"
labels: core, priority:must, blocked-by-hardware
milestone: "M7 拡張"
---

## 背景
**#28 を格上げして本Issueに統合する。**
構想メモ §47 の GPU パネルは VRAM監視・Process監視を持つ。これを coldaisle が一元的に担う（要件 D-07）。

Workspace が独自に `nvidia-smi` を叩く構成にしない。物理環境（`air.*`）とGPU内部（`gpu.*`）が
同じタイムラインに並んでいなければ、「GPU温度が高いのは吸気が熱いからか、負荷が高いからか、
冷却が効いていないからか」を切り分けられない。

## やること
- [ ] NVML（`pynvml`）から収集: `gpu.0.core` `gpu.0.hotspot` `gpu.0.mem` `gpu.0.vram_used` `power.gpu.0` `sys.cuda_processes`
- [ ] lm-sensors から収集: `cpu.package` `cpu.vrm` `chipset`
- [ ] 同一の `readings` テーブルへ投入（ロング形式のため**スキーマ変更不要**）
- [ ] 派生メトリクス `d.gpu_internal_delta` = `gpu.0.hotspot - gpu.0.core`
- [ ] 派生メトリクス `d.top_rise` = `air.top_exhaust - air.gpu_exhaust`
- [ ] 相関ルール: 「GPU coreは高いが吸気は正常」→ 冷却系の問題を示唆
- [ ] `nvidia-smi` をサブプロセスで叩かず NVML を直接使う（頻度が高いため）

## 検証すべき仮説
**GPUの600W排気がCPU AIOラジエーターの吸気になっていないか**（`docs/spec-review.md` C-05）。
`gpu.0.power` と `cpu.package` の相関を測定できるようにすること。

## 依存
#26, #3
