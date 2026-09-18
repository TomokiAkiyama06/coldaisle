# 決定記録 0043: CPU die 温度（k10temp）と GPU の T.Limit margin・throttle reason・fan speed のメトリクス名

- **種別**: Decision Record
- **Status**: FINAL（2026-09-18、リポジトリ所有者が承認）
- **Date**: 2026-09-18
- **Supersedes**: なし（決定記録 0032 は FINAL のまま変えない。0002 §2.1 の文法で名前を足す）
- **関連**: [`0002-metric-naming.md`](0002-metric-naming.md) §2.1・§2.6 /
  [`0032-internal-telemetry-metric-names.md`](0032-internal-telemetry-metric-names.md) /
  [`0029-telemetry-loss-classes.md`](0029-telemetry-loss-classes.md) §2.1・§2.2 /
  [`0038-internal-telemetry-outage-counting.md`](0038-internal-telemetry-outage-counting.md) /
  [`0040-server-health-api.md`](0040-server-health-api.md)
- **対象 Issue**: #65

## 1. Context

2026-09-18、リポジトリ所有者がセッション内で、Internal Telemetry Collector（#65）へ次の入力を
加えることを承認した。

1. k10temp（AMD Ryzen の CPU 内蔵センサー）の `Tctl` / `Tccd1` / `Tccd2`
2. NVML の GPU T.Limit margin（nvidia-smi の `temperature.gpu.tlimit`。slowdown までの残り温度）
3. NVML の現在の clock event reason（throttle reason）のうち、熱と電力に関わるもの
4. NVML の GPU fan speed（%）

0032 はこれらの名前を持たない。0032 は FINAL なので書き換えず、本記録で名前を足す。
名前は一度保存すると変えられない（0002 §2.1）ため、本番の常駐 collector へ出す前に
所有者が名前を承認した（2026-09-18、セッション内で本記録の内容どおりに承認）。

2026-09-18 に実機（本サーバー）で読んだ値:

| 入力 | 取得元 | 値 |
|---|---|---|
| Tctl | hwmon `k10temp` / label `Tctl` | 44〜66 °C（負荷で変動。collector 実行時 60.0 °C） |
| Tccd1 | hwmon `k10temp` / label `Tccd1` | 33.75〜58 °C |
| Tccd2 | hwmon `k10temp` / label `Tccd2` | 30.4〜48 °C |
| T.Limit margin | `nvmlDeviceGetMarginTemperature` | 62 °C（nvidia-smi の `temperature.gpu.tlimit` と一致） |
| clock event reason | `nvmlDeviceGetCurrentClocksEventReasons` | `0x0`（supported は `0x1ff`） |
| fan speed | `nvmlDeviceGetFanSpeed` | 30 %（fan 2基とも `_v2` で 30 %） |

## 2. Decision

2026-09-18、リポジトリ所有者が本記録のメトリクス名を内容どおりに承認した（PR #139 のレビュー）。

### 2.1 名前

すべて 0002 §2.1 の文法（2〜4 セグメント）に従う。

| 対象 | メトリクス名 | 単位 | 取得元 |
|---|---|---|---|
| CPU Tctl | `cpu.tctl` | C | k10temp `Tctl` |
| CPU CCD1 温度 | `cpu.ccd1` | C | k10temp `Tccd1` |
| CPU CCD2 温度 | `cpu.ccd2` | C | k10temp `Tccd2` |
| GPU T.Limit までの余裕 | `gpu.<index>.tlimit_margin` | C | `nvmlDeviceGetMarginTemperature` |
| GPU fan speed | `gpu.<index>.fan_speed` | % | `nvmlDeviceGetFanSpeed` |
| HW Slowdown | `gpu.<index>.throttle.hw_slowdown` | flag | reason `0x08` |
| HW Thermal Slowdown | `gpu.<index>.throttle.hw_thermal` | flag | reason `0x40` |
| SW Thermal Slowdown | `gpu.<index>.throttle.sw_thermal` | flag | reason `0x20` |
| HW Power Brake Slowdown | `gpu.<index>.throttle.hw_power_brake` | flag | reason `0x80` |
| SW Power Cap | `gpu.<index>.throttle.sw_power_cap` | flag | reason `0x04` |

`<index>` は 0032 §2 と同じく「実機に1台だけ存在する GPU」の logical `0` である。

- `cpu.tctl` は**制御用の値**であり、die の実温度（Tdie）とは限らない（CPU によってはオフセットを
  含む）。そのため `cpu.die` とせず、k10temp の名前をそのまま使う。既存の `cpu.package`
  （asusec が読む「CPU Package」）とは取得元も意味も別の系列として残す
- `cpu.ccd1` / `cpu.ccd2` は k10temp の番号（1 始まり）をそのまま名前に入れる。0002 の添字
  セグメント（`cpu.ccd.1`）にすると 0 始まりの装置番号と読み違えるため、label と同じ綴りにする
- `gpu.0.fan_speed` は 0002 §2.1 の「単位を名前に入れない」に従い、`fan_pct` としない。
  NVML の fan speed は**目標値**（意図した回転数の最大比）で、tach の実測ではない。100 % を
  超えることがあり、丸めない
- `gpu.0.tlimit_margin` は slowdown 温度を超えると負になる。0 へ丸めない

### 2.2 throttle reason は reason ごとの 0/1 にする

1つの bitmask メトリクス（`gpu.0.throttle_reasons` に `0x48` などを入れる）にはしない。
reason ごとに `0.0`（立っていない）/ `1.0`（立っている）の数値メトリクスとする。単位は `flag`。

理由:

- **ロールアップが意味を持つ。** `readings_1m` の min / max / mean（0008）が、それぞれ
  「区間中ずっと立っていた」「一度でも立った」「立っていた時間の割合」になる。bitmask の平均
  （`0x40` と `0x04` の平均 `0x22`）は存在しない reason の組を指し、0002 §2.6 の理由1と同じ
  問題を起こす
- **0002 §2.6（カテゴリ値は `system_state`）に当たらない。** 0/1 は状態の列挙ではなく
  「その reason が立っているか」の指標値で、平均に意味がある。写像も名前そのもの（1 reason = 1 名前）
  なので、設定を変えて過去データの解釈が変わることもない
- 0002 §2.1 の文法の範囲に収まり、4 セグメント（`gpu.0.throttle.hw_thermal`）で足りる

記録する reason は熱と電力に関わる5つだけとする。`GpuIdle` / `ApplicationsClocksSetting` /
`SyncBoost` / `DisplayClockSetting` は冷却の判断に使わず、特に `GpuIdle` はアイドル中ずっと
立つため記録しない。`hw_slowdown` は `hw_thermal` / `hw_power_brake` を含む上位の reason で、
どちらでもない外部要因（電源など）の slowdown を拾うために残す。

ビットの値は `nvml.h` の ABI（`nvmlClocksEventReason*`）に固定する。pynvml の定数名は版で
変わってきた（`ClocksThrottle` → `ClocksEvent`）ため、名前に依存しない。

### 2.3 欠測の扱い

- API 自体が無い、または NotSupported などの例外は `missing`（値 `None`）。0 にしない
- throttle reason は `nvmlDeviceGetSupportedClocksEventReasons` を同時に読み、**supported に
  無い reason は `missing`** とする。報告しない reason のビットは常に 0 で、「起きていない」と
  区別できないため。supported を読めないときは5つとも `missing`
- 5つの reason・margin・fan speed の欠測は NVML source の状態（`ok` / `degraded`）を下げない
  （core 温度と電力だけが下げる。#65 の既存の規則のまま）
- 有効な間の停止は 0038 のとおり欠測として数える（`expected_metrics` に載せる）

### 2.4 欠測の区分（0029）

本記録の名前は、すべて **Advisory（記録のみ）** とする。0029 §2.2 の表の Advisory 行
（`gpu.0.hotspot` / `cpu.vrm` など）と同じ扱いである。

| 入力 | 区分 | 理由 |
|---|---|---|
| `cpu.tctl` / `cpu.ccd1` / `cpu.ccd2` | Advisory | CPU の Critical 入力は `cpu.package`（0029 §2.2）のまま。k10temp は補助の観測 |
| `gpu.0.tlimit_margin` | Advisory | GPU の Critical 入力は `gpu.0.core` のまま |
| `gpu.0.throttle.*` | Advisory | 記録と事後の分析に使う |
| `gpu.0.fan_speed` | Advisory | GPU 自身の fan で、本プロジェクトの制御対象外 |

そのため:

- `internal-telemetry.yaml` の k10temp 入力は `required: false`（source 状態を下げない）
- Critical Safety / Reactive Guard / Fallback Controller は、本記録の名前を入力にしない。
  制御やアラートに使う（例: `hw_thermal` が立ったら Front / Rear を上げる、margin が小さい
  ときに警告する）場合は、閾値と区分を別の決定記録で決め、人の承認を得る
- ただし Server Health API（0040）は有効な NVML / hwmon 入力をすべて見るため、これらが
  `missing` になると signal が `yellow` になる。本機はすべて公開することを確認済みなので、
  `missing_tolerated` には入れない（欠測を隠さない）

### 2.5 有効化

k10temp の3入力は、2026-09-18 に実機で driver `k10temp` と label を読み、同日に所有者が
セッション内で有効化を承認したことを `confirmation.status: confirmed` の `basis` に残す。

## 3. Consequences

- CPU の die 内の温度差（Tctl と CCD の差、CCD 間の差）を記録でき、`cpu.package` だけでは
  見えない偏りを後から分析できる
- GPU が熱・電力で clock を落とした時間の割合を、1分・1時間のロールアップからそのまま読める
- T.Limit margin は GPU ごとに異なる slowdown 温度を吸収した「あと何度」なので、機種が
  変わっても同じ意味で比べられる
- 1 poll あたりの行数が 10 増える（27 行）。行が小さいため保存量への影響は小さい

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| bitmask を1つのメトリクス（`gpu.0.throttle_reasons`）に保存 | ロールアップの平均・最小・最大が意味を持たない（0002 §2.6 の理由1） |
| reason の組を文字列で `system_state` に保存（`sys.gpu_throttle`） | 「立っていた時間の割合」を集計できない。reason が同時に立つため状態の列挙にもならない |
| `gpu.0.fan_pct` | 単位を名前に入れない（0002 §2.1） |
| fan ごとの `gpu.0.fan_speed.0` / `.1` | NVML の値は目標値で、本機の2基は同じ値。GPU ごとに fan 数が違い、設定に数を持つ必要が出る |
| `cpu.ccd.1` / `cpu.ccd.2` | 添字セグメントは 0 始まりの装置番号を想定しており（0002 §2.1）、k10temp の 1 始まりと食い違う |
| `cpu.die` | Tctl はオフセットを含みうる制御値で、die 温度と呼ぶと意味がずれる |
| `gpu.0.slowdown_margin` | nvidia-smi / NVIDIA の資料の呼び方（T.Limit）と揃えたほうが照合しやすい |

## 5. 未決事項

- throttle reason・margin をアラートや制御に使うか、その閾値（別の決定記録）
- `nvmlDeviceGetFanSpeedRPM` が本機で値（2026-09-18 に 1201 rpm）を返した。nvidia-smi は
  RPM を表示しないが、NVML からは読める。記録するかは別途決める
