# 決定記録 0032: Internal Telemetry のメトリクス名

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-18
- **Supersedes**: なし（決定記録 0002 §2.1 の命名規約を具体化する）
- **関連**: [`0002-metric-naming.md`](0002-metric-naming.md) / `docs/requirements.md` §5.1 /
  [`0029-telemetry-loss-classes.md`](0029-telemetry-loss-classes.md) §2.2・§2.4
- **対象 Issue**: #65

## 1. Context

要件 §5.1 は ASUS T_SENSOR で測る 12V-2x6 コネクタ外装温度と、Front / Rear /
Top の RPM・PWMについて、具体的なメトリクス名を #65 で決めるとしている。CPU
Package power と GPU utilization にも取得対象はあるが名前が無い。

T_SENSOR はマザーボード上の取得端子名であり、測定対象ではない。同じ端子へ別の場所の
プローブを接続できるため、`t_sensor` だけでは保存済みデータの物理的な意味が分からない。

## 2. Decision

次の名前を提案する。すべて決定記録 0002 の `<domain>.<name>` 文法に従う。

| 対象 | メトリクス名 | 単位 |
|---|---|---|
| 12V-2x6 コネクタ外装温度 | `board.connector_12v2x6` | C |
| CPU Package power | `power.cpu.package` | W |
| GPU utilization | `gpu.<index>.utilization` | % |
| zone fan tach | `fan.<zone>.rpm` | rpm |
| zone fan PWM readback | `fan.<zone>.pwm` | % |
| AIO Pump tach（参照のみ） | `fan.aio_pump.rpm` | rpm |
| VRM Fan tach（参照のみ） | `fan.vrm.rpm` | rpm |

`<zone>` は `front` / `rear` / `top` とする。PWM は hwmon の raw `0..255` を API と
Dataset で比較しやすい `0..100 %` へ変換して保存する。制御層内部の `demand=0.0..1.0`
とは別の、実際に読めた hardware telemetry である。

T_SENSOR は未設置なので、本記録が FINAL になっても設置・#50 の較正・妥当範囲の承認が
終わるまで本番設定を `enabled: false` に保つ。有効化前は欠測でも Critical としない
（0029 §2.4）。現在の実装と設定はこの Proposed 名を受け付けるが、本番収集は無効である。

## 3. Consequences

- 取得端子を交換しても「12V-2x6 外装」という保存値の意味が変わらない
- Fan zone と RPM / PWM を同じ規則で列挙でき、Front / Rear / Top を混ぜない
- `power.gpu.0` と `power.cpu.package` が同じ power domain に並ぶ
- T_SENSOR の測定位置を変える場合は別名が必要になり、既存系列を誤って継続しない
- `fan.*.pwm` は raw hwmon 値をそのまま必要とする低レベル診断には使えない。raw 値は
  Fan Hardware Backend の readback / decision trace が所有する

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `board.t_sensor` | 取得端子名であって測定位置が分からず、プローブ移設で系列の意味が変わる |
| `power.connector_12v2x6` | 測っているのは電力でなく外装温度であり、`power.*` の単位 W と混同する |
| `fan.front.pwm_raw` | API / Dataset の共通単位を hardware ABI の 0..255 に固定する |
| `cpu.power` | 既存の `power.gpu.0` と domain の向きが逆で、対象の Package も曖昧になる |

## 5. 未決事項

- T_SENSOR の妥当範囲、断線時の実測値、較正誤差は #50 で測定し、人が承認する
- 実機上の driver / label と物理 header の対応は #65 の実機確認として残す
- 複数の tach を持つ1 zoneを個別 fanへ分ける必要が出た場合の index 規則は、その時点で
  別の Decision Record にする
