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
  - `sensor_fail`: 指定センサーが途中から `null` + `err` になる（**契約どおりの故障**）
  - `sensor_fail_raw`: 指定センサーが途中から `-127.00` の生値を返す（契約違反のファーム）
  - `sensor_reset_85`: 指定センサーが途中から **ちょうど `85.00`** を返す（spec-review C-02）
  - `reset`: 途中で `up` が巻き戻り `seq` がリセットされる
  - `dropout`: 30秒間データが来なくなる
- [ ] 時間圧縮オプション（`--speed 60` で1分を1秒に）
- [ ] シナリオはYAMLで定義し、テストから再現可能にする

> **訂正（2026-08-24）**: 当初 `sensor_fail` を「`-127.00` を返す」と書いていたが、
> 決定記録 0003 と食い違っていた。ファーム（#11）は `-127.00` を `null` + `err` に
> 変換して送るため、**実機が送ってくるもの**は `null` + `err` である。
> MockSource の役割は実機の再現なのでそちらを既定とし、生値が届く場合
> （ファームのバグ・旧版）を `sensor_fail_raw` として分けた。要件 §5.3 が
> ホスト側の規則も定めているのは、この防御のためである。

## 受入基準
- `uv run coldaisle-daemon --source mock --scenario ramp` でDBに書き込まれる
  - **#8 完了後に検証する。** `Source → Normalizer → Store` のパイプラインと
    CLI は #8 のスコープであり、本 Issue には含めない
- 各シナリオが決定論的（seed固定で同一出力）
- この時点でダッシュボードもアラートもAIも開発可能な状態になっている

## 依存
#5
