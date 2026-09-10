---
title: "ESP32本番ファームウェア（JSON v1 / 非同期変換 / WDT）"
labels: firmware, priority:must
milestone: "M2 実機接続"
---

## 背景
ESP32-S3は手元にあるため、**GPUサーバーが無くても着手できる。**
元仕様の重大な問題（変換時間・85℃・メタ情報欠落）をここで解消する。

## やること
- [ ] JSON v1 スキーマで出力（#4 で確定したもの）
- [ ] **DS18B20を11bit + `setWaitForConversion(false)` で5本同時変換**
      （`docs/spec-review.md` C-01。12bit逐次では2.5秒周期を守れない）
- [ ] `-127.00` および **ちょうど `85.00`** を異常として `null` + `err` に落とす
- [ ] 起動時に `type:"hello"` バナーを送出（fw版数、各chのROM ID、分解能）
- [ ] `Wire.setClock(100000)`（AM2320は高速モードで不安定）
- [ ] Task Watchdog Timer を有効化（ループ停止時に自動リセット）
- [ ] **D2/GPIO3（ストラッピングピン）での起動・書き込み検証**。
      問題があれば D9/GPIO8 へ退避（`docs/spec-review.md` W-01）
- [ ] `firmware/` にスケッチと配線図を配置

## 受入基準
- 送信周期が2.5秒 ±10% に収まる（実測ログを添付）
- センサーを1本抜くと該当フィールドが `null` になり `err` に理由が入る
- 電源投入時に必ず `hello` が1回だけ出る
- リセット後、`seq` が0に戻り `up` が巻き戻る

## 依存
#4

## 2026-09-10 の実装（決定記録 0022）

- `firmware/coldaisle_sensor/coldaisle_sensor.ino` を追加（試作 `sketch_aug21a` を土台に）
- C-01（11bit + 非ブロッキング同時変換）/ C-02（ちょうど 85.00）/ C-04（AM2320 の間引き）に対応
- `seq` / `up` / `hello`（ROM ID）/ WDT / `Wire.setClock(100000)` を追加
- `tests/test_firmware_contract.py` が形の食い違いを CI で見張る

**コンパイルと実測は未了。** このマシンに Arduino のツールチェーンが無い。
受入基準のうち「周期 ±10%」「抜線で null + err」「hello が1回」「リセットで seq が 0」は
`firmware/README.md` のチェックリストとして人に残した（決定記録 0022 §5-1）。
