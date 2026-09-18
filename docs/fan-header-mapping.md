# Fan header 対応表（PRO6000 サーバー）

`config/internal-telemetry.yaml` のファン割り当ての根拠となる実機記録。
所有者が 2026-09-12 頃に実機で `nct6799` の `fanN / pwmN` と物理ファン位置を突き合わせ
（所有者の記録 `pro6000_fan_pwm_mapping_v14`）、2026-09-18 に内容を確認した。

- マザーボード: ASUS ProArt X870E-CREATOR WIFI（Super I/O `nct6799`）
- `hwmonN` の番号は再起動で変わるため、設定では driver 名 `nct6799` と channel で指定する
- マザーボード上のヘッダ名（CPU_FAN / CHA_FAN / AIO_PUMP）との対応は未記録

## 対応

| hwmon | 物理位置 / 役割 | metric | 制御 |
|---|---|---|---|
| `fan2 / pwm2` | Top Cooler Fan（上面ラジエータ） | `fan.top.rpm` / `fan.top.pwm` | Top zone |
| `fan4 / pwm4` | Water Block VRM Fan | `fan.vrm.rpm` | 参照のみ |
| `fan5 / pwm5` | Rear Exhaust（背面排気） | `fan.rear.rpm` / `fan.rear.pwm` | Rear zone |
| `fan6 / pwm6` | Front Intake（前面吸気） | `fan.front.rpm` / `fan.front.pwm` | Front zone |
| `fan7 / pwm7` | AIO Pump（約3375 RPM） | `fan.aio_pump.rpm` | 通常制御しない。100%固定 |

`fan1` / `fan3` は 0 RPM（未接続）。

## PWM と RPM（2026-09-12 頃の実測、概数）

| PWM | Top | VRM | Rear | Front |
|---:|---:|---:|---:|---:|
| 25% | 721 | 1157 | 525 | 439 |
| 50% | 1339 | 2331 | 956 | 930 |
| 75% | 1904 | 3260 | 1255 | 1304 |
| 100% | 2343 | 4078 | 1575 | 1650 |

2026-09-18 に BIOS 自動制御（PWM 約39%）のまま読んだ値は Top 1083 / VRM 1831 /
Rear 743 / Front 690 RPM で、上表と整合する。

## 停止・再始動

| Zone | 停止 | 再始動 | 下限候補 |
|---|---|---|---|
| Top | pwm 16 | pwm 24 | pwm 32 |
| VRM | pwm 16 | pwm 32 | pwm 40（実用下限） |
| Rear | pwm 0 でも停止しない（約 440〜450 RPM） | — | — |
| Front | pwm 16 | pwm 24 | pwm 32 |

下限候補は #75 / #77 の profile と `safety.yaml` の最低安全 Demand の検討材料であり、
承認済みの Safety 値ではない。
