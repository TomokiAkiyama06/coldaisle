---
title: "ルールエンジン（閾値・継続時間・ヒステリシス）"
labels: core, priority:must, safety
milestone: "M4 アラート"
---

## 背景
元仕様 §17 の「単発値ではなく、一定時間継続・上昇速度・複数センサー相関で判断する」を実装。
**AIは一切関与しない決定論的レイヤ。**

## やること
- [ ] 状態機械: `OK → PENDING → FIRING → RESOLVED`
- [ ] ルール定義を `config/rules.yaml` で外出し（閾値をコードに埋めない）
- [ ] ヒステリシス（発火閾値と解除閾値を別に持つ）
- [ ] 実装するルール（要件 FR-401〜409）:
  `SENSOR_FAULT` / `SENSOR_MISSING` / `PROBE_CHANGED` / `RECIRCULATION` /
  `INTAKE_HIGH` / `AIRFLOW_DEGRADED` / `RAPID_RISE` / `ROOM_HIGH` / `HUMIDITY_OUT_OF_RANGE`
- [ ] アラートを `alerts` テーブルへ永続化

## 受入基準
- 各ルールについて MockSource のシナリオで発火・解除するテストがある
- 閾値付近を往復するデータでフラッピングしない
- **閾値は暫定値であり実測後に確定する旨が `rules.yaml` にコメントされている**

## 依存
#6, #9
