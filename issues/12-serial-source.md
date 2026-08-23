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
