// coldaisle 本番ファームウェア（#11 / JSON スキーマ v1）
//
// 出力は1行1件の JSON（決定記録 0003）。ホスト側のパーサ
// （src/coldaisle/ingest/protocol.py）が契約の相手である。
// **フィールド名と型を変えるときは決定記録 0003 を先に更新すること。**
//
// 試作（sketch_aug21a）から直した点:
//   1. 12bit 逐次変換（750ms x 5 = 3.75s）では 2.5 秒周期を守れない
//      → 11bit + 非ブロッキングで5本同時変換（spec-review C-01）
//   2. ちょうど 85.00 を異常として扱っていなかった（spec-review C-02）
//      → DS18B20 のパワーオンリセット値。「ありえない値ではない」ので危険
//   3. seq / up が無く、取りこぼしと再起動をホストが検出できなかった（FR-105 / FR-106）
//   4. 起動バナーが無く、ROM ID を記録できなかった（spec-review W-03 / FR-403）
//   5. I2C が高速モードのままだった（AM2320 は 100kHz。spec-review）
//   6. ウォッチドッグが無く、ループが止まると無言で停止していた
//
// 必要なライブラリ（Arduino IDE のライブラリマネージャ）:
//   OneWire / DallasTemperature / Adafruit AM2320（Adafruit BusIO と Unified Sensor を含む）
// ボード: Seeed XIAO ESP32S3（esp32 core 3.x）

#include <Adafruit_AM2320.h>
#include <DallasTemperature.h>
#include <OneWire.h>
#include <Wire.h>
#include <esp_task_wdt.h>
#include <esp_timer.h>
#include <math.h>
#include <stdarg.h>

// ======================================================
// 設定
// ======================================================

static const char FW_VERSION[] = "1.0.0";
static const char DEVICE_ID[] = "xiao-esp32s3";

// 送信周期。ホストはこの値を `interval_ms` として受け取り、期待サンプル数に使う
// （決定記録 0002 の expected_count）。**ここを変えたら hello も自動で変わる。**
static const uint32_t INTERVAL_MS = 2500;

// DS18B20 の分解能。11bit = 0.125C / 変換 375ms（spec-review C-01）。
// 12bit はセンサー確度 +-0.5C に対して過剰で、時間コストに見合わない
static const uint8_t DS_RESOLUTION_BITS = 11;

// 変換待ち。11bit の 375ms にマージンを足した値
static const uint32_t DS_CONVERSION_WAIT_MS = 400;

// ウォッチドッグ。1周期 2.5 秒なので、数周期ぶんの余裕を取る
static const uint32_t WDT_TIMEOUT_MS = 15000;

// AM2320 が連続でこの回数失敗したら、読み取りを間引く（spec-review C-04）。
// **異常状態のセンサーを叩き続けない。** 発熱事象が記録されている
static const uint8_t AM2320_FAIL_LIMIT = 5;
static const uint8_t AM2320_BACKOFF_CYCLES = 12;  // 約30秒

// ======================================================
// ピン割り当て（spec-review W-01 の表）
// ======================================================
//
// D6 = GPIO43 は UART0 の TXD なので 1-Wire には使わない。#5 は D8 へ退避済み。
// D2 = GPIO3 はストラッピングピンだが、2026-08-23 の実機検証で問題なしと確認済み。

#define DS1_PIN D0  // GPIO1  Front Intake
#define DS2_PIN D1  // GPIO2  GPU Intake
#define DS3_PIN D2  // GPIO3  GPU Exhaust
#define DS4_PIN D3  // GPIO4  Top Exhaust
#define DS5_PIN D8  // GPIO7  Rear Exhaust

#define I2C_SDA D4  // GPIO5
#define I2C_SCL D5  // GPIO6

static const uint8_t AM2320_I2C_ADDRESS = 0x5C;

// ======================================================
// チャネル
// ======================================================
//
// **デバイスは短い名前を送り、`air.` の付与はホストが行う**（決定記録 0003 §2.5）。
// ファームウェアがドメインの名前空間を知る必要はない。

static const uint8_t DS_COUNT = 5;

struct DsChannel {
  const char *name;
  uint8_t gpio;
};

// XIAO ESP32S3 では `D0` 等の別名の値が**そのまま GPIO 番号**である
// （D0=1 / D1=2 / D2=3 / D3=4 / D8=7。spec-review W-01 の表と一致）。
// **番号を二重に書かない。** 書くと、ピンを変えたときに hello だけ古くなる
static const DsChannel DS_CHANNELS[DS_COUNT] = {
    {"front_intake", DS1_PIN},
    {"gpu_intake", DS2_PIN},
    {"gpu_exhaust", DS3_PIN},
    {"top_exhaust", DS4_PIN},
    {"rear_exhaust", DS5_PIN},
};

static const char ROOM_TEMP_CHANNEL[] = "room_temp";
static const char ROOM_HUMIDITY_CHANNEL[] = "room_humidity";
static const char ROOM_SENSOR_NAME[] = "room";

// ======================================================
// 異常値
// ======================================================

// センサー無応答。DallasTemperature の DEVICE_DISCONNECTED_C と同じ
static const float DS_DISCONNECTED_C = -127.0f;

// **スクラッチパッドのパワーオンリセット値**（spec-review C-02）。
// 変換完了前に読んだ場合や電源が瞬断した場合に、-127 ではなく
// 「もっともらしい 85.00」が返る。排気温度としてありえない値ではないため、
// そのまま流すと誤警報になる。
//
// 本物の 85.00 も落とすことになるが、この系は空気温度を測るものなので
// 実測で 85.00 が出る状況は既に異常である。**取り違えるより落とすほうを採る。**
static const float DS_POWER_ON_RESET_C = 85.0f;

// 11bit では 0.125C 刻みに量子化されるため、85.0 と -127.0 は厳密に表現できる。
// それでも等値比較に幅を持たせるのは、将来分解能を変えたときに黙って
// すり抜けないようにするため
static const float EXACT_EPSILON = 0.001f;

// ======================================================
// 状態
// ======================================================

OneWire oneWire[DS_COUNT] = {
    OneWire(DS1_PIN), OneWire(DS2_PIN), OneWire(DS3_PIN), OneWire(DS4_PIN), OneWire(DS5_PIN),
};
DallasTemperature ds[DS_COUNT] = {
    DallasTemperature(&oneWire[0]), DallasTemperature(&oneWire[1]), DallasTemperature(&oneWire[2]),
    DallasTemperature(&oneWire[3]), DallasTemperature(&oneWire[4]),
};
DeviceAddress ds_address[DS_COUNT];
bool ds_present[DS_COUNT];

Adafruit_AM2320 am2320;
bool am2320_present = false;
uint8_t am2320_fail_streak = 0;
uint8_t am2320_backoff_left = 0;

uint32_t seq = 0;
uint64_t next_due_ms = 0;

// USB CDC が繋がっているか。**繋がった瞬間にもう一度 hello を出す**ためだけに持つ
bool host_connected = false;

// 1行ぶんの組み立て先。**途中まで書いた行を出さない**ために一度に書き出す
static char line[768];
static size_t used = 0;
static bool overflowed = false;

// 組み立ての先頭に戻す
static void reset_line() {
  used = 0;
  overflowed = false;
  line[0] = '\0';
}

// 末尾に足す。**`snprintf` の戻り値をそのまま足し込まない。**
//
// `snprintf` は「収まっていれば書かれたはずの長さ」を返すので、切り詰めが
// 起きた時点で合計が配列の長さを超える。その値で `sizeof(line) - n` を計算すると
// 負になり、size_t では巨大な値になって**バッファを越えて書く。**
static void append(const char *fmt, ...) {
  if (overflowed) {
    return;
  }
  va_list args;
  va_start(args, fmt);
  int written = vsnprintf(line + used, sizeof(line) - used, fmt, args);
  va_end(args);
  if (written < 0 || (size_t)written >= sizeof(line) - used) {
    overflowed = true;
    return;
  }
  used += (size_t)written;
}

static void emit_log(const char *message);

// 組み立てた1行を送る。**切り詰められたら送らない。**
//
// 壊れた JSON はホスト側で捨てられるが（デーモンは落ちない）、
// 「送ったのに届かない」原因の切り分けが難しくなる。デバイス側で気づけるようにする。
static void emit() {
  if (overflowed) {
    emit_log("line_truncated");
    return;
  }
  Serial.write((const uint8_t *)line, used);
  Serial.write('\n');
}

// `log` はホストが無視する種別（決定記録 0003 §2.2）。**人が原因を追うために出す。**
//
// `println` を使わない。`\r\n` が付き、行末に `\r` が残る
static void emit_log(const char *message) {
  char buf[96];
  int n = snprintf(buf, sizeof(buf), "{\"v\":1,\"type\":\"log\",\"msg\":\"%s\"}", message);
  if (n > 0 && n < (int)sizeof(buf)) {
    Serial.write((const uint8_t *)buf, n);
    Serial.write('\n');
  }
}

// JSON の数値。測定できなければ null（決定記録 0003 §2.3）
static void append_value(float value) {
  if (isnan(value)) {
    append("null");
  } else {
    append("%.2f", value);
  }
}

static void rom_to_hex(const DeviceAddress address, char *out) {
  // **大文字16進16桁**（決定記録 0003 §2.4）。ホストはこの文字列で
  // プローブの入れ替わりを判定する（FR-403）
  for (uint8_t i = 0; i < 8; i++) {
    snprintf(out + i * 2, 3, "%02X", address[i]);
  }
}

// ======================================================
// 起動バナー（決定記録 0003 §2.4）
// ======================================================

static void send_hello() {
  reset_line();
  append("{\"v\":1,\"type\":\"hello\",\"fw\":\"%s\",\"dev\":\"%s\",\"interval_ms\":%lu,\"sensors\":{",
         FW_VERSION, DEVICE_ID, (unsigned long)INTERVAL_MS);

  bool first = true;
  for (uint8_t i = 0; i < DS_COUNT; i++) {
    if (!ds_present[i]) {
      // **見つからないセンサーを載せない。** 載せると、ホストが記録した構成が
      // 「そのとき繋がっていたもの」ではなくなる
      continue;
    }
    char rom[17] = {0};
    rom_to_hex(ds_address[i], rom);
    append("%s\"%s\":{\"kind\":\"ds18b20\",\"gpio\":%u,\"rom\":\"%s\",\"res\":%u}",
           first ? "" : ",", DS_CHANNELS[i].name, DS_CHANNELS[i].gpio, rom, DS_RESOLUTION_BITS);
    first = false;
  }

  if (am2320_present) {
    // **AM2320 に gpio を持たせない**（決定記録 0003 §2.4）。
    // I2C は SDA / SCL の2本で、単一の gpio では表せない
    append("%s\"%s\":{\"kind\":\"am2320\"}", first ? "" : ",", ROOM_SENSOR_NAME);
    first = false;
  }

  if (first) {
    // 1本も見つからなかった。**契約に違反する hello を出さない**
    // （スキーマは `sensors` に1つ以上を要求する）。
    // サンプルは出し続けるので、ホスト側では SENSOR_FAULT / SENSOR_MISSING が鳴る
    emit_log("no_sensors");
    return;
  }

  append("}}");
  emit();
}

// ======================================================
// 読み取り
// ======================================================

// 1本ぶんの温度。異常なら NAN を返し、理由を `reason` に入れる
static float read_ds(uint8_t index, const char **reason) {
  *reason = nullptr;
  if (!ds_present[index]) {
    *reason = "absent";
    return NAN;
  }
  float value = ds[index].getTempC(ds_address[index]);
  if (isnan(value) || fabsf(value - DS_DISCONNECTED_C) < EXACT_EPSILON) {
    *reason = "-127";
    return NAN;
  }
  if (fabsf(value - DS_POWER_ON_RESET_C) < EXACT_EPSILON) {
    *reason = "85";
    return NAN;
  }
  return value;
}

// ======================================================
// setup
// ======================================================

void setup() {
  Serial.begin(115200);
  // USB CDC の列挙を待つ。**待つだけでは足りない**（下記 send_hello の呼び出しを参照）
  delay(2000);

  // **100kHz にする。** AM2320 は高速モードで不安定（spec-review）
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(100000);

  // 起動時のセルフチェック（spec-review C-04）。
  // 期待するアドレスに応答するかを見る。AM2320 は最初の1回が必ず失敗する
  // （スリープからの起床のため）ので、2回目で判定する
  for (uint8_t attempt = 0; attempt < 2; attempt++) {
    Wire.beginTransmission(AM2320_I2C_ADDRESS);
    am2320_present = (Wire.endTransmission() == 0);
    if (am2320_present) {
      break;
    }
    delay(50);
  }
  if (am2320_present) {
    am2320.begin();
  }

  for (uint8_t i = 0; i < DS_COUNT; i++) {
    ds[i].begin();
    // **非ブロッキングにする**（spec-review C-01）。既定では変換完了まで待つため、
    // 5本を順に読むと 750ms x 5 = 3.75 秒かかり 2.5 秒周期が破綻する
    ds[i].setWaitForConversion(false);
    ds_present[i] = ds[i].getAddress(ds_address[i], 0);
    if (ds_present[i]) {
      ds[i].setResolution(ds_address[i], DS_RESOLUTION_BITS);
    }
  }

  // ループが止まったら自動で再起動する。**無言で停止させない**
  // 指定初期化子（`.timeout_ms = ...`）は C++17 では標準でないため、
  // 代入で埋める。**環境によってコンパイルが通らない書き方を避ける**
  esp_task_wdt_config_t wdt = {};
  wdt.timeout_ms = WDT_TIMEOUT_MS;
  wdt.idle_core_mask = 0;
  wdt.trigger_panic = true;
  if (esp_task_wdt_init(&wdt) == ESP_ERR_INVALID_STATE) {
    // Arduino core が既に初期化している場合はこちら
    esp_task_wdt_reconfigure(&wdt);
  }
  esp_task_wdt_add(NULL);

  // **電源投入時に1回**（決定記録 0003 §2.2）。
  //
  // ただし、これだけでは足りない。デバイスのほうが先に起動していると、
  // **誰も読んでいない間に hello が流れて消える。** ホストはサンプルだけを
  // 受け取り、ROM の一覧も `interval_ms` も知らないまま動き続ける（FR-403 が
  // 比べる相手を持てない）。`loop()` で接続を見て、繋がった瞬間に出し直す
  send_hello();
  host_connected = (bool)Serial;

  next_due_ms = (uint64_t)(esp_timer_get_time() / 1000) + INTERVAL_MS;
}

// ======================================================
// loop
// ======================================================

void loop() {
  esp_task_wdt_reset();

  // **繋がった瞬間に hello を出し直す。**
  //
  // 周期的に出すのではなく、立ち上がりだけで出す。周期的に出すと
  // 「電源投入時に1回だけ」（#11 の受入基準）が崩れ、ホストの記録が
  // 何度も書き換わる。読み手が付いたときにだけ、その読み手のために出す
  bool connected = (bool)Serial;
  if (connected && !host_connected) {
    send_hello();
  }
  host_connected = connected;

  // `millis()` ではなく 64bit のタイマを使う。`millis()` は約49.7日で巻き戻り、
  // ホストはそれを**再起動として扱う**（FR-106）。稼働し続けているのに
  // 再起動したことにされるのを避ける
  uint64_t now_ms = (uint64_t)(esp_timer_get_time() / 1000);

  if (now_ms < next_due_ms) {
    delay(10);
    return;
  }

  // 固定の刻みで進める（ドリフトしない）。ただし大きく遅れたときは追いつこうと
  // せずに再同期する。**溜まった分を一気に送っても意味が無い**
  next_due_ms += INTERVAL_MS;
  if (next_due_ms < now_ms) {
    next_due_ms = now_ms + INTERVAL_MS;
  }

  // **5本同時に変換を開始する**（spec-review C-01）
  for (uint8_t i = 0; i < DS_COUNT; i++) {
    if (ds_present[i]) {
      ds[i].requestTemperatures();
    }
  }

  // AM2320 は変換待ちの間に読む。**時間を二重に使わない**
  float room_temp = NAN;
  float room_humidity = NAN;
  const char *room_reason = nullptr;
  if (!am2320_present) {
    room_reason = "absent";
  } else if (am2320_backoff_left > 0) {
    // 連続失敗のあとは間引く（spec-review C-04）
    am2320_backoff_left--;
    room_reason = "backoff";
  } else {
    room_temp = am2320.readTemperature();
    room_humidity = am2320.readHumidity();
    if (isnan(room_temp) || isnan(room_humidity)) {
      room_reason = "read_failed";
      if (++am2320_fail_streak >= AM2320_FAIL_LIMIT) {
        am2320_fail_streak = 0;
        am2320_backoff_left = AM2320_BACKOFF_CYCLES;
      }
    } else {
      am2320_fail_streak = 0;
    }
  }

  delay(DS_CONVERSION_WAIT_MS);
  esp_task_wdt_reset();

  float ds_value[DS_COUNT];
  const char *ds_reason[DS_COUNT];
  for (uint8_t i = 0; i < DS_COUNT; i++) {
    ds_value[i] = read_ds(i, &ds_reason[i]);
  }

  // ------------------------------------------------------
  // 組み立て
  // ------------------------------------------------------

  reset_line();
  append("{\"v\":1,\"type\":\"s\",\"seq\":%lu,\"up\":%llu", (unsigned long)seq,
         (unsigned long long)now_ms);

  append(",\"%s\":", ROOM_TEMP_CHANNEL);
  append_value(room_temp);
  append(",\"%s\":", ROOM_HUMIDITY_CHANNEL);
  append_value(room_humidity);

  for (uint8_t i = 0; i < DS_COUNT; i++) {
    append(",\"%s\":", DS_CHANNELS[i].name);
    append_value(ds_value[i]);
  }

  // `err` は `<channel>:<reason>` の配列（決定記録 0003 §2.3）。
  // **理由の無い null を出さない。** 受け手が「測れなかった」と
  // 「そもそも無い」を区別できるようにする
  bool has_err = false;
  for (uint8_t i = 0; i < DS_COUNT; i++) {
    if (ds_reason[i] != nullptr) {
      append("%s\"%s:%s\"", has_err ? "," : ",\"err\":[", DS_CHANNELS[i].name, ds_reason[i]);
      has_err = true;
    }
  }
  if (room_reason != nullptr) {
    append("%s\"%s:%s\"", has_err ? "," : ",\"err\":[", ROOM_SENSOR_NAME, room_reason);
    has_err = true;
  }
  if (has_err) {
    append("]");
  }

  append("}");
  emit();

  seq++;
}
