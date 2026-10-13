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

## 温度（2026-09-18 に実機で読み取り、同日に所有者が承認）

| driver | label | metric | 状態 | 2026-09-18 の読み取り値 |
|---|---|---|---|---:|
| `asusec` | `CPU Package` | `cpu.package` | 有効 | 49 °C |
| `asusec` | `VRM` | `cpu.vrm` | 有効 | 48 °C |
| `nct6799` | `PCH_CHIP_TEMP` | `board.chipset` | **無効** | 0 °C |
| `k10temp` | `Tctl` | `cpu.tctl` | 有効（名前は決定記録 0043 で承認済み） | 44 °C |
| `k10temp` | `Tccd1` | `cpu.ccd1` | 有効（同上） | 43.6 °C |
| `k10temp` | `Tccd2` | `cpu.ccd2` | 有効（同上） | 36.5 °C |

温度は label で指定する（`hwmonN` と `tempN` の番号は使わない）。

`board.chipset` の対応は所有者が承認したが、`nct6799` の `PCH_CHIP_TEMP` は
`PCH_CHIP_CPU_MAX_TEMP` / `PCH_CPU_TEMP` と同じく常に 0 °C を返した。未配線か driver の未対応とみられる。
有効にすると 0 °C が `ok` として保存されるため（取得不能値を 0 で正常値扱いしない原則）、
実値を返す入力が確認できるまで無効にしている。

## `pwmN_enable` の挙動（2026-10-10 に Top で確認）

自動制御（`5`）の間は `pwmN` へ書けない（`EBUSY`）。`0` を書くとすぐ全速になり、manual で `pwmN=255` のときは
`pwmN_enable` が `0` と読める。`5` を書けば BIOS の曲線へ戻る。詳細と、これに合わせた Max の書き方は
決定記録 [0118](decisions/0118-hwmon-backend-label-less-headers.md) §2.3 / §2.4 / §2.6。

## fand の実機確認（2026-10-13）

決定記録 [0080](decisions/0080-fand-systemd-unit.md) §2.10 の段階 5 と、[0028](decisions/0028-fan-control-contracts.md) §2.9 の
承認点 3 を [0119](decisions/0119-safety-values-first-host.md) §2.11 のとおり Top から行った。設定は 0119 と
[0120](decisions/0120-fan-hardware-first-host.md) の値。fand の unit は `enable` していない。
ここの時間は1回ずつの観察で、0119 §2.13 の値を `confirmed` にする試験（0119 §5 の 5）ではない。

### 段階 5（simulated backend・本番 DB の写し）

| 操作 | 結果 |
|---|---|
| 起動 | `READY` が届き、watchdog（5 s）への heartbeat が始まる |
| `kill -STOP` | 5.05 s で watchdog が切れ SIGABRT。そこから引き継ぎ実行部の完了まで 0.86 s（apport が core を受ける。下記） |
| `kill -KILL` | 終了から引き継ぎ実行部の完了まで 44 ms。`RestartSec` の 2 s 後に再起動 |
| `systemctl stop` | 0.6 s で停止。引き継ぎ実行部が走る |
| 再起動の連続 | 毎回 `ExecStopPost` が走り、`/run/coldaisle` は残る |

simulated は引き継ぎ記録を書かないので、実行部は毎回 `record_found:false` で何も書かない（想定どおり）。

### 承認点 3（`--backend hwmon`・Top から）

Front / Rear は管理ソケットの manual で 1.0 に固定し、Top だけを下げた。

| 操作 | 結果 |
|---|---|
| 起動 | STARTUP の 5 s は全 zone Max。その後 takeover で `pwmN_enable=1` |
| manual Top 0.5 | Top `pwm2=128`・約 1350 RPM（0120 の表は 1376） |
| manual Top 0.35 | Top `pwm2=89`・約 920 RPM |
| manual Top 0.2 | 0.35（`pwm2=89`）で止まる。最低安全 Demand が効く |
| `kill -STOP` | 最後の heartbeat から 5.0 s で SIGABRT、その 0.1 s 後に実行部が3 zone に Max を書いた（`record_found:true`、3 zone とも `applied`）。Top は `pwm2=89` から全速 |
| 再起動 | 前回の記録の元の値（3 zone とも `enable=5`）を引き継ぐ。STARTUP の後は Fallback が全 zone に同じ値を当て、毎秒 0.05 ずつ下げた |
| `kill -KILL` | 終了から 51 ms で実行部が3 zone に Max を書いた |
| `systemctl stop` | 3 zone を `enable=5`（BIOS の曲線）へ戻し、引き継ぎ記録を消した |

気づいたこと:

- **Reactive Guard がアイドルでもよく発火する。** CPU の温度が小さな処理（管理ソケットの CLI の起動など）で数秒に 10 °C 以上跳ね、
  `cpu_temperature_rise` で Top に floor 0.7 が入る。解除まで 30〜60 s。安全側の動きだが、運用では Top が 0.7 にいる時間が長くなりうる
- **SIGABRT のとき apport が core を受ける。** unit の `LimitCORE=0` でも `kernel.core_pattern` が apport への pipe なので、
  `code=dumped` になり `/var/crash` に約 13 MB の報告が残る。実行部の開始の遅れは 0.06〜0.83 s で、0119 §2.12 の
  最悪時間（`TimeoutAbortSec` 5 s を含む約 11 s）の内側（0080 §5 の 3）
- Front / Rear を下げる確認は、これから別に行う（0119 §2.11。センサー基板は 2026-10-13 に接続済み）。Max を書くことと BIOS への返却は、
  上の操作で3 zone とも確かめた
