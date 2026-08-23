---
title: "MockSource: 合成データ生成器（実機なし開発の要）"
labels: core, priority:must
milestone: "M1 データ基盤"
---

## 背景
**これが本プロジェクトで最優先のIssue。**
サーバーが届く前に L1〜L4 を完成させるための土台。

## やること
- [ ] `Source` プロトコル定義（`stream() -> Iterator[RawSample]`）
- [ ] `MockSource` を実装。シナリオを切り替え可能にする:
  - `idle`: 室温26℃前後、各部+1〜3℃、微小なノイズ
  - `ramp`: GPU負荷上昇。gpu_exhaust が10分で+15℃
  - `recirculation`: front_intake が room より徐々に離れる
  - `sensor_fail`: 指定センサーが途中から `-127.00` を返す
  - `reset`: 途中で `up` が巻き戻り `seq` がリセットされる
  - `dropout`: 30秒間データが来なくなる
- [ ] 時間圧縮オプション（`--speed 60` で1分を1秒に）
- [ ] シナリオはYAMLで定義し、テストから再現可能にする

## 受入基準
- `uv run coldaisle-daemon --source mock --scenario ramp` でDBに書き込まれる
- 各シナリオが決定論的（seed固定で同一出力）
- この時点でダッシュボードもアラートもAIも開発可能な状態になっている

## 依存
#5
