---
title: "vLLM + Qwen3-8B の停止可能な GPU AI Service 構成"
labels: ai, infra, priority:must, blocked-by-hardware
milestone: "M6 移行"
---

> **⚠ v0.2で方針変更**: 「`--gpu-memory-utilization 0.25` で常駐」は誤りでした。
> Workspace構想メモ §40「復帰速度より、VRAMを確実に空けることを優先」に従い、
> **Compute Mode では完全停止**します。#32 と併せて実装してください。

## 背景
RTX PRO 6000 (96GB) は本来、研究・学習・Kaggle等のCompute Workloadが主目的。
**監視アシスタントが学習・推論のVRAMを食い潰さないよう上限を切る。**

## やること
- [ ] vLLM を systemd サービス化
- [ ] `--gpu-memory-utilization` を低め（0.2〜0.3）に設定し、根拠をコメント
- [ ] ツールコールパーサの設定と**スモークテスト**（vLLM/モデルのバージョンで推奨値が変わる）
- [ ] 常駐 vs オンデマンド起動 の判断（要件 Q-04）
- [ ] 8Bで不足を感じた場合の上位モデル（30B級MoE等）への移行手順をメモ

## 受入基準
- 学習ジョブ実行中でもチャットが応答する、または明示的に縮退する
- 初回トークンまで2秒以内（NFR-04）

## 依存
#21, #26
