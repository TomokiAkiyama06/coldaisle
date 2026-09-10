# ファームウェア（XIAO ESP32-S3）

`coldaisle_sensor/coldaisle_sensor.ino` が本番のスケッチです（#11）。
出力は1行1件の JSON で、契約は [`schemas/device_v1.schema.json`](../schemas/device_v1.schema.json)
と[決定記録 0003](../docs/decisions/0003-device-json-schema.md)が持ちます。

**フィールド名や型を変えるときは、決定記録 0003 を先に更新してください。**
ホスト側のパーサ（`src/coldaisle/ingest/protocol.py`）が相手です。

---

## 配線

| 用途 | XIAO | GPIO | 備考 |
|---|---|---:|---|
| DS18B20 #1 Front Intake | D0 | 1 | |
| DS18B20 #2 GPU Intake | D1 | 2 | |
| DS18B20 #3 GPU Exhaust | D2 | 3 | ストラッピングピン。2026-08-23 に実機検証済み（問題なし） |
| DS18B20 #4 Top Exhaust | D3 | 4 | |
| AM2320 SDA | D4 | 5 | XIAO の既定 SDA |
| AM2320 SCL | D5 | 6 | XIAO の既定 SCL |
| DS18B20 #5 Rear Exhaust | D8 | 7 | **D6 は GPIO43 = UART0 TXD なので使わない** |
| 予備 | D9 / D10 | 8 / 9 | GPIO3 で問題が出た場合の退避先 |

```text
        XIAO ESP32-S3
        +-----------+
  3V3 --|o         o|-- 5V
  GND --|o         o|-- GND
        |           |
   D0 --|o         o|-- D10
   D1 --|o         o|-- D9
   D2 --|o         o|-- D8
   D3 --|o         o|-- D7
   D4 --|o         o|-- D6   ← 使わない（GPIO43 = UART0 TXD）
   D5 --|o         o|-- ...
        +-----------+

  DS18B20（5本とも同じ形）        AM2320
    赤  -- 3V3                      VCC -- 3V3
    黒  -- GND                      GND -- GND
    黄  -- D0/D1/D2/D3/D8           SDA -- D4
           ＋ 4.7kΩ で 3V3 へ       SCL -- D5
```

- **4.7kΩ のプルアップは 1-Wire の線ごとに1本**ずつ必要です（5本それぞれ）
- 5本同時変換はピーク電流が集中するため、3V3 と GND の間に
  **100μF 電解 + 0.1μF セラミック**を入れると安定します（spec-review）
- プローブ本体に **#1〜#5 のラベル**を巻いてください。ROM ID と併せて二重の同定手段になります

---

## 書き込み

1. Arduino IDE のボードマネージャで **esp32（3.x）** を入れる
2. ボードは **Seeed XIAO ESP32S3** を選ぶ
3. ライブラリマネージャで以下を入れる
   - `OneWire`
   - `DallasTemperature`
   - `Adafruit AM2320`（`Adafruit BusIO` と `Adafruit Unified Sensor` が一緒に入る）
4. `coldaisle_sensor/coldaisle_sensor.ino` を開いて書き込む

動作の確認は、シリアルモニタ（115200）に JSON が1行ずつ流れることで分かります。
`firmware/sample_output.jsonl` が出力の例です。

---

## 試作（`sketch_aug21a`）から直した点

| # | 試作の問題 | 直し方 | 根拠 |
|---|---|---|---|
| 1 | 12bit 逐次変換で 750ms × 5 = 3.75 秒。2.5 秒周期を守れない | 11bit + `setWaitForConversion(false)` で5本同時変換 | spec-review C-01 |
| 2 | ちょうど `85.00` を通していた | `null` + `err` に落とす | spec-review C-02 |
| 3 | `seq` / `up` が無い | 付ける。取りこぼしと再起動をホストが検出できる | FR-105 / FR-106 |
| 4 | 起動バナーが無い | `type:"hello"` で ROM ID と分解能を送る | spec-review W-03 / FR-403 |
| 5 | I²C が高速モードのまま | `Wire.setClock(100000)` | spec-review |
| 6 | ウォッチドッグが無い | Task Watchdog Timer を有効化 | #11 |
| 7 | `delay(2500)` を処理の後に置いていた（実周期が伸びる） | 締切で刻む（ドリフトしない） | #11 受入基準 |

---

## 実機で確かめること（#11 の受入基準）

**このスケッチはコンパイル・実行の検証をしていません。** 手元に Arduino の
ツールチェーンが無いため、ここはどうしても人の手が必要です。

- [ ] **コンパイルが通る**（ライブラリの版によっては API が違う可能性があります）
- [ ] 送信周期が **2.5秒 ±10%** に収まる。`up` の差を見れば分かります

  ```bash
  # 10分ぶん記録して、間隔の分布を見る
  uv run coldaisle-daemon --source serial --port /dev/tty.usbmodem* | tee /tmp/run.jsonl
  ```
- [ ] **センサーを1本抜く**と、そのフィールドが `null` になり `err` に `<channel>:-127` が入る
- [ ] **電源投入時に `hello` が1回だけ**出る（2回出たら、リセットが挟まっています）
- [ ] **リセット後に `seq` が 0 に戻り、`up` が巻き戻る**
- [ ] D2/GPIO3 に繋いだ状態で、電源投入・リセット・書き込みのいずれも正常
      （2026-08-23 に確認済みですが、このスケッチでも再確認してください）
- [ ] AM2320 のコネクタを抜いた状態で起動すると `err` に `room:absent` が入る

### いまの作りの限界

- **起動後に挿したセンサーは認識しません。** `hello` は電源投入時の1回だけで、
  ホストはその構成を「較正が対応している構成」として記録します（FR-403）。
  配線を変えたら**再起動してください。**
- 実周期は「変換待ち 400ms + 読み取り + 送信」が 2.5 秒に収まる前提です。
  収まらない場合は `INTERVAL_MS` を上げてください（`hello` の `interval_ms` も
  自動で変わり、ホストの期待サンプル数も追従します）。
