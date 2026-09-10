---
title: "SerialSource（自動検出・再接続・非JSON行の無視）"
labels: core, priority:must
milestone: "M2 実機接続"
---

## やること
- [ ] macOS `/dev/cu.usbmodem*` / Linux `/dev/ttyACM*` の自動検出
- [ ] 環境変数/設定でのポート明示指定
- [ ] 115200 baud
- [ ] **行頭が `{` でない行、JSONパース失敗行を黙って破棄**（ESP32ブートログ対策）
- [ ] 切断検出 → 指数バックオフ再接続（1s, 2s, 4s, ... 上限30s）
- [ ] 再接続中も `SENSOR_FAULT` を継続報告
- [ ] `hello` を受信したらデバイス情報を `devices` テーブルへ記録

## 受入基準
- USBを物理的に抜き差ししてもデーモンが落ちず自動復帰する（`@pytest.mark.hardware`）
- ブートログを混ぜたフィクスチャで例外が発生しない（CI実行可能）

## 依存
#8, #11

## 2026-09-10 の実装（決定記録 0023）

- `src/coldaisle/ingest/serial_source.py` を追加。`--source serial` を繋いだ
- 行の解釈は `ingest.protocol.decode_line()` へ。**非有限値は行ごと捨てる**（決定記録 0003 §2.8）
- 偽のポートで再接続とバックオフを検証（CI で回る）。**実機の抜き差しは `-m hardware`**

**実機での抜き差し確認は未了。** 手元に ESP32 はあるので、書き込み後に
`uv run pytest -m hardware` で確認できる。
