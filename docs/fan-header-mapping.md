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

## fand の実機の予備確認（2026-10-13）

決定記録 [0080](decisions/0080-fand-systemd-unit.md) §2.10 の段階 5 と、[0028](decisions/0028-fan-control-contracts.md) §2.9 の
承認点 3 の確認項目を、[0119](decisions/0119-safety-values-first-host.md) §2.11 のとおり Top から、続けて Front / Rear で
行った。設定は 0119 と [0120](decisions/0120-fan-hardware-first-host.md) の値。fand の unit は `enable` していない。

**これは段階 5 と承認点 3 の完了ではない。** 段階 5 のうち、`0640` の DB を移行して起動できること・移行を省くと
`db_not_writable` で起動しないこと・実際の systemd で `/var/lib/coldaisle` の setgid が保たれることは、まだ確かめていない
（導入先の DB は既に `0660` で、下の確認は本番の DB の写しを使った）。また、0119 §2.13 / §5 の 5 は、承認点 3 の前に試験の手順を別の決定記録で設計し
所有者の承認を得ることを求めるが、その記録はまだ無い。ここの結果はその前の予備確認で、時間は1回ずつの観察である。
承認点 3 と `enable` は、段階 5 の残りと、その記録の承認と、記録の手順による確認の後に行う。

### 段階 5 の一部（simulated backend・本番 DB の写し）

| 操作 | 結果 |
|---|---|
| 起動 | `READY` が届き、watchdog（5 s）への heartbeat が始まる |
| `kill -STOP` | 5.05 s で watchdog が切れ SIGABRT。そこから引き継ぎ実行部の完了まで 0.86 s（apport が core を受ける。下記） |
| `kill -KILL` | 終了から引き継ぎ実行部の完了まで 44 ms。`RestartSec` の 2 s 後に再起動 |
| `systemctl stop` | 0.6 s で停止。引き継ぎ実行部が走る |
| 再起動の連続 | 毎回 `ExecStopPost` が走り、`/run/coldaisle` は残る |

simulated は引き継ぎ記録を書かないので、実行部は毎回 `record_found:false` で何も書かない（想定どおり）。

### 承認点 3 の確認項目（`--backend hwmon`・Top から）

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

### Front / Rear（同日、センサー基板の接続後）

Top を manual で 1.0 に固定し、Front / Rear を下げた。最後に3 zone を自動制御（Fallback）に任せた。

| 操作 | 結果 |
|---|---|
| manual Front / Rear 0.5 | `pwm6` / `pwm5` = 128。Front 約 905・Rear 約 940 RPM（0120 の表は 890 / 938） |
| manual Front / Rear 0.35 | 128 → 89。Front 約 630・Rear 約 670 RPM |
| manual Front / Rear 0.2 | 0.35（89）で止まる |
| auto（約 100 s） | `active_controller` は `fallback`、`faults` は空、`safety_state` は `normal` のまま（`AIR_TELEMETRY_STALE` は出ない） |
| `systemctl stop` | 3 zone を `enable=5` へ戻し、引き継ぎ記録を消した |

`kill -STOP` / `kill -KILL` で Max を書くことと BIOS への返却は、上の Top の確認で3 zone とも確かめた。

- **アイドルでの自動制御は BIOS より Fan が速い。** auto の間の平均の PWM は Top 173・Front / Rear 156（CPU の平均 55 °C）で、
  直前と直後の BIOS の制御は約 100（CPU 42〜46 °C）だった。CPU の温度の跳ねで Fallback が上がって
  ヒステリシスと保持（10 s）で残ることと、Top の Guard の floor 0.7 が重なる。`enable` の前に所有者が判断する材料

## 0121 の試験の結果

決定記録 [0121](decisions/0121-hardware-test-procedure.md) の手順で行った試験の結果（§2.10）。順序は T0 → T2 → T3 → T6 → T4 → T5 → T7 → T1。
値の `status` を `confirmed` にするのは、すべてそろってから所有者の承認を得て行う。

### T0: 0080 段階 5 の残り（2026-10-14 01:56、やり直し 03:09）

| 確認 | 結果 |
|---|---|
| `0640` の DB の写しで起動 | `db_not_writable`・終了コード 5 で、制御を取らずに止まった。ログに、fand の補助グループに `coldaisle` があっても `0640` では書けないことが出る |
| 写しを `0660`・グループ `coldaisle` に直して起動 | 60 s で 61 tick 動いた |
| setgid | `/var/lib/coldaisle` は `drwxrws---` のまま。fand が作った `-wal` / `-shm` はグループ `coldaisle`・`0660` |

初回（01:56）は fand を 60 s しか動かさず、0121 §2.2 の「数分」に足りなかったので、03:09 に 5 分でやり直した。

| 確認（やり直し） | 結果 |
|---|---|
| `0640` の DB の写しで起動 | `db_not_writable`・終了コード 5 |
| `0660` に直して起動 | 300 s で 301 tick 動いた（Learned は配備していないので経路を開かずに運転、の error だけ） |
| setgid | 5 分の後も `/var/lib/coldaisle` は `drwxrws---`。`-wal` / `-shm` は owner `coldaisle-fan`・グループ `coldaisle`・`0660` |

### T2: 書き込みの失敗（2026-10-14 02:26〜02:28）

アイドル（CPU 40 °C・GPU 29 °C・GPU 7 W）で、manual の全 zone 0.35 から注入した。

| 試験 | 結果 |
|---|---|
| Front に `pwm6_enable=5` | `enable_reverted` で `DEGRADED`（Front 以外は `fault_demand` 1.0）が 2 回、3 回目の評価で `EMERGENCY`。最初の失敗から `failing_ms` 5001 で終了コード 7。引き継ぎの実行部が3 zone に Max を書いた（`applied`）。再起動で takeover し、終了コード 7 は繰り返さない |
| 一時的な失敗（Front に `5`、1.2 s 後に `1`） | 失敗は 1 tick。次の tick で書き込みと読み戻しが成功した。`EMERGENCY` にも終了にもならない。fault は `fault_clear_hold_ms` の 30 s 保持の間 `DEGRADED`（3 zone 1.0）で残り、その後 `normal` |
| Top に `pwm2_enable=5` | 1 回目の評価で `EMERGENCY`。`failing_ms` 5001 で終了コード 7、引き継ぎで Max、再起動 |

気づいたこと:

- 注入から終了までの約 5 s、注入した Fan は BIOS の値（約 0.38）で回り、fand の出力（0.35）より遅くはならなかった（0121 §2.4 の想定どおり）
- Max の間、`pwmN_enable` は `0` と読める。この driver は manual の 255 を `0` と読み返す（0118 §2.6）
- 終了コード 7 のたびに `監査書き込みスレッドが時間内に止まらなかった`（error）が出る。異常終了の経路は待たずに閉じる作りなので毎回出る。監査の内容は構造化ログに残る

### T3: データの遅れと復帰（2026-10-14 02:38〜02:46）

fand の auto で行った。時間は、最後の sample の DB の時刻の直後の tick（fand が初めて観測した tick）から数えた。
判定には [0124](decisions/0124-trace-wall-clock-tolerance.md) の許容（trace の壁時計の ±50 ms）を使う。

| 試験 | stale まで | その間 | fault が消えるまで |
|---|---|---|---|
| `coldaisle-telemetry` を 30 s 停止 | 6000 ms（`cpu_telemetry_stale`・`gpu_telemetry_stale`・`t_sensor_stale`） | Top は `zone_fault_max`、Front / Rear は `critical_telemetry_fault_demand` 1.0 | 30001 ms。途中で出入りしない |
| `coldaisle-daemon` を 30 s 停止 | 6000 ms（`air_telemetry_stale`） | Front / Rear だけ 1.0。Top は自分の制御のまま（0.40〜0.59） | 31000 ms |
| 同上のやり直し | 6001 ms | 同上（Top は最大 0.59） | 30001 ms |

- stale はほぼ毎回ちょうど 6000 ms に上がる。fand は 5000 ms を超えたら stale とし、tick は 1000 ms ごとなので、6 tick 後になる。
  trace の `ts_ms` は壁時計で ±1 ms 揺れるので 6001 ms も出る（0124）
- 落ち着き: 収集の停止の後、CPU は約 20 s で 44 → 40 °C に戻り、tach は保持の間 1.0 で一定。空気の温度は急な変化が無く、停止と保持の間は Fan が 1.0 なのでゆっくり下がり続けた（再開の直前から 40 s 後まで、`air.top_exhaust` 26.88 → 26.38 °C、`air.rear_exhaust` 25.50 → 25.12〜25.25 °C。どちらも 0.5 °C 以内）。取り込みの停止では、空気の温度と
  Front / Rear の tach はすぐ一定になった。CPU の温度は、試験の前から別のプロセス（別のセッションのテスト。CPU 約 40%・
  CPU Power 約 40 W）で 45〜59 °C の間を揺れており、0124 §2.3 のとおり合否に使わない
- `cpu_power_unavailable` は、どの tick の Top の reason にも出なかった。収集を止めると CPU の温度と電力が同じ tick で古くなり、
  Top が `zone_fault_max` になって safety floor の reason が trace に出ないため

### T6: stall（2026-10-14 02:56〜03:03、立ち上がりの測り直し 07:46〜07:50）

試験専用の設定（0121 §2.8）を `/etc/coldaisle/control-config-test-<名前>/` に置き、unit の drop-in で `--config-dir` だけを差し替えた。
対象でない2つの zone は manual で 1.0。試験の後に drop-in と設定を消し、`ExecStart` が本番の `--config-dir` に戻ったことを確かめた。

**立ち上がり**（合格の条件は [0125](decisions/0125-stall-rise-criterion-tach-update.md)）

fand の trace で測った値（2026-10-14 07:46〜07:50。試験の側から基板を読まない。収集のサービスは動かしたまま）。
0.35 を書いた tick から、trace の回転数が `stall_min_rpm` 以上になった tick まで。

| zone | 5 回 | 最長 + driver の更新の閾値 1500 ms | 判定 |
|---|---|---|---|
| Top（450 RPM 以上） | 2000 / 2000 / 2000 / 1999 / 2000 ms（すべて 2 tick。届いた回転数 536〜656 RPM） | 3500 ms < 5000 | 合格 |
| Front（300 RPM 以上） | 2000 / 2000 / 1999 / 2000 / 2000 ms（すべて 2 tick。届いた回転数 688〜795 RPM） | 3500 ms < 5000 | 合格 |
| Rear | 0.0 の間の最低 439 / 439 / 442 RPM（> 350）。0.35 の 90% まで 3696 / 2915 / 2685 ms（02:56 の試行。記録だけ） | — | 合格 |

- 判定に使った tick の組で、trace の `ts_ms` の差と journal の `__MONOTONIC_TIMESTAMP` の差の違いは最大 3 ms（0124 §2.2）
- trace の回転数が変わる間隔は、ほとんどが 2 tick（ときどき 1 tick。収集のサービスの読み取りが間に入ったとき）
- 02:56 の試行（試験の側が 0.1 s ごとに `fanN_input` を読んだもの）は、Top 2246 / 2136 / 2688 ms、Front 1121 / 1457 / 1008 ms。
  このときは値が約 1576 ms ごとに変わったが、読む頻度が更新を変えるので判定に使わない（0125）
- 立ち上がりの試験の間に fault は出なかった。Top の Guard の floor は起動直後の数 tick だけで、0.0 で止めるのを妨げなかった

**検出**（本番の設定のうち、対象の `stall_min_rpm` だけを Top 1200・Front 900・Rear 1000 にした設定。manual 0.35）

| zone | 計時の開始（`tach_unconfirmed_zones` に初めて出た tick）から `tach_stall` まで | その後 |
|---|---|---|
| Top | 5000 ms | `EMERGENCY`。3 zone とも `emergency_max` |
| Front | 6000 ms | Front が `zone_fault_max`、ほかは `fan_fault_demand` 1.0（`DEGRADED`） |
| Rear | 5001 ms | Rear が `zone_fault_max`、ほかは `fan_fault_demand` 1.0（`DEGRADED`） |

### T4: 下げる速さと起動の落ち着き（2026-10-14 08:06〜08:19、測り直し 08:31〜08:51）

本番の設定（drop-in なし）で fand を auto で起動した。合格の条件の一部は [0126](decisions/0126-startup-settle-front-spinup.md)。

**下げる速さ**（Max 2 分 → manual で全 zone 0.35 → 届いてから 60 s → auto）

| 試行 | 下降率の最大（`elapsed_ms` 998〜1002 ms で割る） | 0.35 に届くまで | 下げる前の定常（直前 60 s の中央値）からの上がりの最大 | 判定 |
|---|---|---|---|---|
| アイドル（08:07〜08:11） | 0.0500/s | 12〜13 tick（直前まで `ramp_down`、届いた tick は `safety_floor`） | 空気 +0.12 °C・GPU / 12V-2x6 0。CPU は操作の直後 15 s を除いて +1 °C（0126 §2.2） | 合格 |
| GPU 300 W（08:45〜08:50） | 0.0501/s | 同上。Top は CPU の floor で 0.48 で止まる | Rear の排気 +1.13 °C・CPU +2 °C（1 点）・GPU / 12V-2x6 / Top の排気 / GPU の排気 0 | 合格 |

- アイドルの `cpu.package` は、`ctl max` の 1.1 s 後に 49 °C、manual 0.35 の 3.4 s 後に 46 °C に跳ね、どちらも約 10 s で 40 °C に戻った。
  Fan が全開へ上がる途中の `ctl max` の直後にも出るので、Fan を下げたことによるものではない
- 08:11〜08:19 の GPU 300 W の試行は、GPU の排気が落ち着く前（Max の間も 42 → 48.4 °C と上がり続けた）に始めていたので判定に使わない。
  測り直しでは、10 分以上待ち、直近 3 分の幅が空気の排気 0.5 °C・CPU（3 点の中央値）/ GPU / 12V-2x6 1 °C 以内になってから始めた
- 中止の基準には近づかなかった。GPU の電力の上限は試験の後に 600 W に戻した
- 08:51 の後、GPU がプロセス無しで使用率 100%・約 120 W・P1 のまま残った（2026-10-13 20:27 の負荷試験の後と同じ）。Xid は無く、
  短い CUDA の処理を正常に終えると約 6 s で P8・約 6 W に戻った。負荷のプロセスを SIGTERM で止めると起きうるので、
  負荷のスクリプトは SIGTERM を受けたら同期してから終わるようにした

**起動の落ち着き**（アイドル。全 zone を manual 0.35 に 45 s → `ctl max` に 30 s。3 回。fand の trace の回転数が全開の ±5% に入り
そのまま留まった最初の tick まで。試験の側からは基板を読まない）

| zone | 0.35 の回転数 | 3 回 | 最長 + tach の更新の閾値 1500 ms |
|---|---|---|---|
| Top | 974〜981 RPM | 5840 / 4841 / 5844 ms | 7344 ms |
| Rear | 678〜700 RPM | 5840 / 4841 / 5844 ms | 7344 ms |
| Front | 629〜632 RPM | 11841 / 12841 / 11844 ms | 14341 ms |

- `startup_settle_ms` 5000 では Front が全開に届く前に Demand が下がり始める。0126 で 15000 にする
- 08:06 の takeover（BIOS の約 680 RPM から）でも、Front は 5.6 s で 1498 RPM（全開の 83%）だった

### T5: watchdog（2026-10-14 09:47〜11:52）

本番の設定（drop-in なし・`startup_settle_ms` 15000）で fand を auto で動かした。systemd の `WatchdogTimestampMonotonic`（最後の
`WATCHDOG=1` を受けた時刻）が変わった直後に `SIGSTOP` し、Max（3 zone の `pwmN_enable=0`）を 20 ms ごとに読んで時間を測った。
時刻はどちらも CLOCK_MONOTONIC。Max を確かめたら負荷を止め、30 s 見てから `systemctl stop` で BIOS へ戻した。
GPU 600 W で落ち着いた状態は作れないので、(ii) と (iii) の温めは 450 W に置き換えた（[0127](decisions/0127-t5-gpu-steady-450w.md)）。

| 条件 | 位相 | `SIGSTOP` から Max | 止める直前の `effective`（F / R / T） | 開始 → 最高（Max の後 30 s まで）CPU / GPU / 12V-2x6 °C | 判定 |
|---|---|---|---|---|---|
| (ii) アイドル | 2.3 ms | 5294 ms | 0.43 / 0.43 / 0.43 | 39 → 51 / 29 → 29 / 27 → 27 | 合格 |
| (i) GPU 600 W | 1.8 ms | 5163 ms | 0.42 / 0.42 / 0.42 | 40 → 60 / 29 → 39 / 26 → 28 | 合格 |
| (i) CPU 全スレッド | 0.8 ms | 5182 ms | 0.40 / 0.40 / 0.40 | 40 → 72 / 29 → 29 / 27 → 27 | 合格 |
| (ii) GPU 300 W | 2.8 ms | 5143 ms | 0.65 / 0.65 / 0.65 | 65 → 68 / 63 → 63 / 41 → 41 | 合格 |
| (ii) GPU 450 W | 2.6 ms | 5143 ms | 0.85 / 0.85 / 0.85 | 65 → 65 / 75 → 75 / 46 → 46 | 合格 |
| (iii) 450 W で 10 分 → GPU 600 W | 1.9 ms | 5144 ms | 0.48 / 0.48 / 0.48 | 45 → 64 / 49 → 68 / 43 → 43 | 合格 |
| (ii) CPU 全スレッド | 8.0 ms | 5220 ms | 0.89 / 0.89 / 1.0 | 76 → 76 / 29 → 29 / 26 → 26 | 合格 |
| (iii) CPU 10 分 → CPU 全スレッド | 2.6 ms | 5221 ms | 0.47 / 0.47 / 0.47 | 43 → 74 / 29 → 29 / 26 → 26 | 合格 |

- 合格の条件（0121 §2.7）: `SIGSTOP` から Max まで約 11 s 以内、開始 + 上昇が上限より 5 °C 以上低い（CPU・GPU 85 °C 未満、
  T_SENSOR の 12V-2x6 75 °C 未満）。最も高かったのは (iii) CPU の 74 °C
- Max までは 5143〜5294 ms で、どれも watchdog の 5 s の直後だった。systemd は止まったプロセスにも `SIGABRT` を届け
  （`Killing process … with signal SIGABRT`）、`TimeoutAbortSec` の SIGKILL を待たずに ExecStopPost の引き継ぎ
  （`safety_handoff_completed`）が Max を書いた。最悪の約 11 s（0119 §2.12）にはならなかった
- (i) / (iii) の直前の `effective` は、アイドルで記録した値（0.43）か、それ + 0.05 以内だった
- (ii) アイドルの CPU の 51 °C は、負荷の無い条件での一瞬の値（止めてから 8.9 s、Max の 3.6 s 後。約 10 s で 40 °C に戻った）。`RestartSec` 2 s で fand が自動で起動し直した時刻と重なるので、その起動（Python の起動と import）によるものと見ている
- `ii-gpu600`（10:47）は、fand が 0.96 まで上げても、負荷の開始から約 80 s で GPU コアが中止の見張り（80 °C）に届いて止めた
- 1 回目の書き出しが 2 回目で上書きされたので、DB から書き出し直した（`t5-0947-*`）。`/var/crash` の fand の記録は増えていない
