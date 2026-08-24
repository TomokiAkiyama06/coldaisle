---
title: "Docker Compose による3層分離"
labels: infra, priority:should, blocked-by-hardware
milestone: "M6 移行"
---

## 背景
Workspace構想メモ §41 のサービス分離をそのまま実現する。

```text
Core Services      常時稼働   ← coldaisle-daemon / coldaisle-api
GPU AI Services    停止可能   ← vLLM / Qwen3.8-27B / coldaisle-ai
Compute Workloads  任意       ← Kaggle / PyTorch / Research
```

## やること
- [ ] `docker-compose.core.yml` / `docker-compose.gpu-ai.yml` に分割
- [ ] Core側は **GPUデバイスをマウントしない**（構造的にVRAMを掴めないようにする）
- [ ] **シリアルデバイスのパススルー**（`/dev/server-sensors`）と権限設定
- [ ] GPU AI側のみ `deploy.resources.reservations.devices` でGPUを要求
- [ ] Core側のみ `restart: always`
- [ ] SQLiteのボリューム永続化（`/var/lib/coldaisle`）

## 注意
コンテナ内からUSBシリアルを扱う場合、udevルール（#26）とデバイス権限の両方が必要。
ここでハマりやすいので、Core側だけはホスト直起動（systemd）にする選択肢も検討すること。

## 依存
#26, #32
