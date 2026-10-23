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

決定記録 [0121](decisions/0121-hardware-test-procedure.md) の手順で行った試験の結果（§2.10）。順序は T0 → T2 → T3 → T6 → T4 → T5 → T7 → T8 → T9 → T1（T8・T9 は [0123](decisions/0123-cpu-power-floor-tests.md) で足した）。
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

### T6: stall（2026-10-14 02:56〜03:03、立ち上がりの測り直し 07:46〜07:50・15:02〜15:12）

試験専用の設定（0121 §2.8）を `/etc/coldaisle/control-config-test-<名前>/` に置き、unit の drop-in で `--config-dir` だけを差し替えた。
対象でない2つの zone は manual で 1.0。試験の後に drop-in と設定を消し、`ExecStart` が本番の `--config-dir` に戻ったことを確かめた。

**立ち上がり**（合格の条件は [0125](decisions/0125-stall-rise-criterion-tach-update.md)）

fand の trace で測った値（2026-10-14 07:46〜07:50。試験の側から基板を読まない。収集のサービスは動かしたまま）。
0.35 を書いた tick から、trace の回転数が `stall_min_rpm` 以上になった tick まで。

| zone | 5 回 | 最長 + driver の更新の閾値 1500 ms + fand の 1 tick 1000 ms | 判定 |
|---|---|---|---|
| Top（450 RPM 以上） | 2000 / 2000 / 2000 / 1999 / 2000 ms（すべて 2 tick。届いた回転数 536〜656 RPM） | 4500 ms < 5000 | 合格 |
| Front（300 RPM 以上） | 2000 / 2000 / 1999 / 2000 / 2000 ms（すべて 2 tick。届いた回転数 688〜795 RPM） | 4500 ms < 5000 | 合格 |
| Top（追加の 15 回。15:02〜15:06） | 1999〜2001 ms（15 回すべて 2 tick。届いた回転数 531〜582 RPM） | 4501 ms < 5000 | 合格 |
| Front（追加の 15 回。15:07〜15:12） | 1999〜2001 ms（15 回すべて 2 tick。届いた回転数 667〜800 RPM） | 4501 ms < 5000 | 合格 |
| Rear | 0.0 の間の最低 439 / 439 / 442 RPM（> 350）。0.35 の 90% まで 3696 / 2915 / 2685 ms（02:56 の試行。記録だけ） | — | 合格 |

- 判定に使った tick の組で、trace の `ts_ms` の差と journal の `__MONOTONIC_TIMESTAMP` の差の違いは最大 3 ms（0124 §2.2）
- trace の回転数が変わる間隔は、ほとんどが 2 tick（ときどき 1 tick。収集のサービスの読み取りが間に入ったとき）
- 02:56 の試行（試験の側が 0.1 s ごとに `fanN_input` を読んだもの）は、Top 2246 / 2136 / 2688 ms、Front 1121 / 1457 / 1008 ms。
  このときは値が約 1576 ms ごとに変わったが、読む頻度が更新を変えるので判定に使わない（0125）。Top の 2688 ms に
  同じ 2500 ms を足すと 5188 ms で窓を超える。所有者の判断（0125 §5）で、同じ手順で Top と Front を 15 回ずつ測り直した
  （15:02〜15:12。`t6c`）。合わせて 20 回ずつ、すべて 2 tick で、3 tick 以上になった回は無い。fault は出なかった。
  4501 ms の 1 ms は tick の時刻の揺れ
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
- 0125 は立ち上がりの条件に fand の 1 tick を足した。同じように足すと Front は 14341 + 1000 = 15341 ms で 15000 を超えるが、
  0126 は変えない（2026-10-14 の所有者の判断）。`startup_settle_ms` は tach を判定しない時間で、表の時間は trace の回転数が
  全開に届いた tick の時刻なので、実際に届いた時刻より遅い側に出ている（0125 の stall のように、遅れて見えることが誤判定に
  つながる量ではない）。T1 の 24 h で、起動の直後に誤った `tach_stall` が出ないことも確かめる
- 08:06 の takeover（BIOS の約 680 RPM から）でも、Front は 5.6 s で 1498 RPM（全開の 83%）だった

### T5: watchdog（2026-10-14 09:47〜11:52）

本番の設定（drop-in なし・`startup_settle_ms` 15000）で fand を auto で動かした。systemd の `WatchdogTimestampMonotonic`（最後の
`WATCHDOG=1` を受けた時刻）が変わった直後に `SIGSTOP` し、Max（3 zone の `pwmN_enable=0`）を 20 ms ごとに読んで時間を測った。
時刻はどちらも CLOCK_MONOTONIC。Max を確かめたら負荷を止め、30 s 見てから `systemctl stop` で BIOS へ戻した。
このときは GPU 600 W で落ち着いた状態を作れなかったので、(ii) と (iii) の温めを 450 W に置き換えて行った。この置き換え（0127）は、
GPU の Fan のサービスの下で 600 W のまま合格した（下の「GPU の Fan のサービスの下でのやり直し」）ので Rejected にした。

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
- **約 11 s の経路は実機では試さない。** 0154 は取り下げ、[0158](decisions/0158-t5-11s-path-component-assurance.md)（FINAL、2026-10-19）で、
  実測済みの部品・テスト・解析で担保し、残るリスク（X1〜X3 と X5）を所有者が受け入れた。0158 §2.5 の T1〜T3 のテストは PR #355 で入った。
  `watchdog_timeout_ms` を `confirmed` にするのは承認点 2 を得てから（0158 §2.6。項目と `basis` は決定記録 [0161](decisions/0161-fand-enable-prerequisites.md) §2.2）。
  D 状態の hang は 0158 §5 の 2 のとおり調べてから決める（調べた結果と決定は 0161 §2.1）。
  以下はそれまでの経緯: `SIGABRT` で fand が終わらない hang では `TimeoutAbortSec` の 5 s も待つので、
  低い Demand のまま約 11 s 続く。上の試行はすべて約 5.2 s で終わり、Max を確かめた直後に負荷を止めたので、その後の約 6 s の
  上がりを測っていない。0119 §2.13 は `watchdog_timeout_ms` に約 11 s の間の余裕を求めるので、上の表は `SIGABRT` で終わる経路の
  合格で、`watchdog_timeout_ms` はまだ `confirmed` にしない。所有者は「試験専用の設定で再現する」方向を選んだ（`safety.yaml` の `watchdog_timeout_ms` と unit の `WatchdogSec` を試験の間だけ
  どちらも 11 s にし、(i) CPU 全スレッド・(iii) CPU・(iii) GPU を少なくとも行う）。0121 §2.1 は試験専用の設定を §2.8 の T6 にしか認めないので、
  **実施の前に、その例外と設定の中身を決める決定記録を作り、所有者の承認を得る。** それまではこの試験を行わない
- (i) / (iii) の直前の `effective` は、アイドルで記録した値（0.43）か、それ + 0.05 以内だった
- (ii) アイドルの CPU の 51 °C は、負荷の無い条件での一瞬の値（止めてから 8.9 s、Max の 3.6 s 後。約 10 s で 40 °C に戻った）。`RestartSec` 2 s で fand が自動で起動し直した時刻と重なるので、その起動（Python の起動と import）によるものと見ている
- `ii-gpu600`（10:47）は、fand が 0.96 まで上げても、負荷の開始から約 80 s で GPU コアが中止の見張り（80 °C）に届いて止めた
- 1 回目の書き出しが 2 回目で上書きされたので、DB から書き出し直した（`t5-0947-*`）。`/var/crash` の fand の記録は増えていない

#### GPU の Fan のサービスの下でのやり直し（2026-10-14 20:11〜2026-10-15 01:00）

`coldaisle-gpu-fan`（0128）を有効にした状態で、(ii) GPU 600 W と (iii) GPU 600 W で 10 分 → GPU 600 W を 0121 のとおり行った。
手順と合格の条件は上と同じ。中止の基準の `gpu.0.core` は、0130 の曲線では 0130 §2.2 の 85 °C。

| 曲線 | 条件 | 位相 | `SIGSTOP` から Max | 止める直前の `effective` | 最高（負荷の間 / 止めてから Max の後 30 s まで）CPU / GPU / 12V-2x6 °C | GPU の Fan | 判定 |
|---|---|---|---|---|---|---|---|
| 0128 | (ii) GPU 600 W | 1.9 ms | 5324 ms | 0.77 | 72 / 71 / 46 ・ 67 / 71 / 46 | 100 % | 合格 |
| 0128 | (iii) GPU 600 W | 2.2 ms | 5168 ms | 0.47 | 65 / 71 / 46 ・ 64 / 62 / 43 | 100 % → 82 % | 合格 |
| 0130 | (ii) GPU 600 W | 0.8 ms | 5227 ms | 0.91 | 71 / 78 / 49 ・ 67 / 78 / 48 | 68 % | 合格 |
| 0130 | (iii) GPU 600 W | 2.6 ms | 5164 ms | 0.45 | 77 / 78 / 49 ・ 63 / 64 / 43 | 72 % → 40 % | 合格 |

- 記録: 0128 の曲線は `t5-2011-*`、0130 の曲線は `t5-0029-*`。19:59 の 1 回目（`t5-1959-*`）は、同じ機械の別の作業の CPU 負荷
  （最大 108 W）が重なって CPU が一瞬 85 °C に届き中止した。判定に使わない
- 0130 の曲線の (ii) は 0130 §2.4 の 2 を兼ねる: GPU 600 W を 15 分以上かけ、GPU コアは 78 °C・GPU の Fan は 68 % で落ち着いた
  （筐体の Fan は GPU の温度で 0.91）。23:44 の 1 回目（`t5-2344-*`）は 30 分 78 °C で横ばいだったが、試験の側の落ち着きの判定
  （空気の排気の 3 分の幅 0.5 °C）が Fan の強いときの排気の揺れで通らず止まった。判定を「最初の 1 分と最後の 1 分の平均の差」に直して行った
- `SIGSTOP` から Max は、どれも上と同じく `SIGABRT` で終わる約 5.2 s の経路。約 11 s の経路は上のとおり 0158 で担保する

### T7: 高温での BIOS との比較（CPU 2026-10-14 15:50〜16:39、GPU 2026-10-15 09:10〜10:28）

0121 §2.9 のとおり、負荷ごとに BIOS → fand → fand → BIOS の順で 10 分ずつかけた。各試行は、温度と吸気との差がアイドルの記録の
±1 °C に戻ってから始めた。GPU の Fan は、どの試行も `coldaisle-gpu-fan`（0130 の曲線）が動かした。比べたのは筐体の Fan の制御だけ。
中止の基準は CPU 85・GPU 85（0130 §2.2）・12V-2x6 67 °C で、どの試行も届かなかった。

吸気との差（°C）の 2 回の平均で、fand から BIOS を引いた値。合格の条件は、最高と最後の 2 分の平均がどちらも +3 °C 以下。

| 項目 | CPU 全スレッド: 最高 | CPU: 最後の 2 分 | GPU 600 W: 最高 | GPU: 最後の 2 分 |
|---|---|---|---|---|
| `cpu.package` | +0.50 | +0.52 | −0.44 | −1.74 |
| `gpu.0.core` | −0.38 | +0.83 | −0.12 | −0.35 |
| `board.connector_12v2x6` | −0.38 | +0.83 | −1.56 | −1.74 |
| `air.gpu_exhaust` | −0.38 | +0.21 | −0.32 | −0.36 |
| `air.top_exhaust` | +0.00 | +0.03 | −2.32 | −2.30 |
| `air.rear_exhaust` | −0.49 | +0.62 | −1.12 | −0.61 |
| 判定 | 合格 | | 合格（決定記録 0136 §2.1。下の注を参照） | |

- **CPU 全スレッド**（`t7cpu-1546-*`）: 吸気は 20.0〜20.9 °C。CPU の最高は 4 回とも 72〜73 °C で、電力は 200 W に張りついた。
  GPU はアイドル（25〜26 °C）で、throttle の flag はこのときの書き出しに含めていない。14:20 の 1 回目（`t7cpu-1420-*`）は、BIOS の 1 回目（吸気 24.4 °C）の後に止めたので判定に使わない
- **GPU 600 W**（`t7gpu-0910-*`）: GPU コアの最高は 78 / 78 / 79 / 80 °C、GPU の Fan は 68 / 68 / 72 / 75 %（BIOS / fand / fand / BIOS）。
  GPU の throttle の flag は 4 回とも `sw_power_cap`（600 W の電力の上限）だけが立った。温度の flag（`sw_thermal`・`hw_thermal`・
  `hw_slowdown`）はどの試行でも立たず、「fand でだけ立つ」ことはなかった
- **GPU 600 W は手順から外れた。** 試行 3（fand）の負荷の間に部屋が暖まり、吸気が 20.4 → 23.1 °C に上がった。試行 4（BIOS）は、
  アイドルの記録（19.6 °C）より 3.5 °C 高い吸気で始まった。0121 §2.9 は、負荷の前の吸気の差が ±1 °C を超えたらやり直すと決めている。
  所有者は、やり直さずにこの結果で合格とし（2026-10-15）、[0136](decisions/0136-t7-t8-t9-acceptance-exceptions.md) §2.1 に残した。例外はこの 1 回だけ。判断の材料は次の 2 つ
  - 吸気がそろっている試行 1（BIOS）とだけ比べても、fand は全項目で 0.8〜2.6 °C 低かった
  - 3 °C の余裕はどの項目にも残っていた
- 0121 §2.9 のとおり、この比較は値の確定ではなく、`enable` の判断の材料とする

### T8: CPU Power の cooling floor（2026-10-14 13:53〜14:19、GPU 600 W は T7 と兼ねて 2026-10-15）

0123 §2.1 のとおり行った。アイドルの fand の auto から、CPU の全スレッドの負荷を 5 分ずつ 3 回、中間の負荷を 3 分保持する試行を 1 回かけた
（`t8t9-1351-*`）。GPU 600 W だけの試行は、T7 の GPU の fand の 2 回（試行 2・3）を当てた。どちらもアイドルの fand の auto から 10 分かけている。
温度の曲線（60 °C → 0.35・80 °C → 1.0）の値は、その tick の前後 2.5 s の `cpu.package` の最大値から計算した。power の曲線（100 W → 0.35・
200 W → 1.0）の値の範囲は、同じ前後 2.5 s の `power.cpu.package` の範囲から計算した。

| 試行 | 負荷の開始から power の曲線が効いた最初の tick | power の曲線が決めた tick | うち曲線の範囲 ±0.03 の外 | うち 195 W 以上 | 判定 |
|---|---|---|---|---|---|
| 全スレッド 1 | 3.5 s | 298 | 0 | 298 | 合格 |
| 全スレッド 2 | 3.8 s | 298 | 0 | 298 | 合格 |
| 全スレッド 3 | 2.1 s | 299 | 0 | 299 | 合格 |
| 中間（151〜166 W を 3 分） | — | 0 | — | — | 確定しない（下の注を参照） |
| GPU 600 W だけ（T7 の試行 2・3） | — | 0 / 0（601 tick ずつ） | — | — | 合格 |

- 全スレッドの間、CPU の最高は 76 °C。排気（Top 29.1 °C 以下）・12V-2x6（27 °C 以下）・GPU（29 °C）は、中止の基準に近づかなかった
- **GPU 600 W だけ**: `power.cpu.package` の最高は 52 W と 93 W で、100 W を超えなかった。93 W は試行 3 の負荷の 555 s 目の 1 sample だけ。
  Top の `safety_floor` は、すべての tick で温度の曲線の値と一致した（差の最大 0.000）。power の floor が Top を上げた tick は無い。
  試行 3 の floor の最高 0.64 は、CPU 69 °C の温度の曲線の値
- **中間の負荷は確定しない。** 保持した 3 分（181 tick）の間、`power.cpu.package` は 151〜166 W で、前後 2.5 s が 130〜170 W・幅 10 W 以下に
  収まる tick は 177 あった。ただ、CPU が 74〜75 °C に上がり、温度の曲線（0.80〜0.84）が power の曲線（0.70〜0.76）を上回った。
  181 tick すべてで温度の曲線が Top の floor を決め、power の曲線が決めた tick は 0 だった。この機械では、130〜170 W を保つと
  温度の曲線が先に floor を決めるので、power の曲線の途中が実機で効く場面がない。0123 §2.1 のとおり合格にせず、所有者が判断した
- **所有者の判断（2026-10-15、[0136](decisions/0136-t7-t8-t9-acceptance-exceptions.md) §2.2）**: `cpu_power_cooling_floor` は両端の 2 点（100 W → 0.35・200 W → 1.0）を全スレッドの 3 回で確定する。
  途中の傾きは実機では確かめていない。途中の値が線形の補間になることは、CI のテスト
  `test_cpu_power_jump_raises_the_top_floor_even_when_the_cpu_is_cool`（157.5 W で曲線の中間の値）で担保する
- 中間の負荷は、`stress-ng --cpu-load` の割合を最大 8 段で合わせてから 3 分保持した。0123 §2.1 は、スレッド数を試行の前に決め、
  試行の間は変えないことを求める。保持の間は変えていないが、合わせる段階は負荷の開始の後だった。そのため、この試行は 1 行目
  （負荷の開始から 5 s 以内）の判定には使っていない
- 0123 §2.1 の最後の行（T1 で power の floor が Top を決めた時間の割合）は、T1 で記録する

### T9 (b): 収集の側の古さの判定（2026-10-14 13:53〜13:54）

fand を manual 0.35（3 zone とも）で動かし、`coldaisle-cpu-power` を `systemctl kill -s SIGSTOP` で 30 s 一時停止してから `SIGCONT` で戻した
（`t8t9-1351-*`）。時刻は、専用サービスが最後に書いたファイルの `ts_ms` を基準にした。

| 測るもの | 結果 | 合格の条件 | 判定 |
|---|---|---|---|
| 最後の書き込みから、Top の reason に `cpu_power_unavailable` が出る tick まで | 7311 ms | 5000〜8500 ms（5000 ms + 収集の周期 2500 ms + 1 tick） | 合格 |
| その間の fault・Top・safety state | fault は無し。Top は `bound_by` が `safety_floor` の 0.35 のまま。`normal` | fault が上がらず、Top が 0.35 のまま | 合格 |
| その間の telemetry の健全度と Guard の閾値 | 記録に無い（下の注を参照） | `DEGRADED`・`conservative` | 合格（0136 §2.3 の CI のテストで満たした。#308 / PR #314。下の注を参照） |
| 戻した後 | `SIGCONT` から 1.8 s（最後の書き込みから 32.3 s）の tick で `cpu_power_unavailable` が消えた | 新しい sample で power の項が戻り、健全度が `NORMAL` に戻る | power の項は合格。健全度が `NORMAL` に戻ることも合格（0136 §2.3 の CI のテストで満たした。#308 / PR #314） |

- 一時停止の間に Reactive Guard は発火しなかった。Top の `bound_by` はずっと `safety_floor` で、判定できない試行にはならなかった
- **健全度と Guard の閾値は、記録から直接は確かめられなかった。** 理由は次の 2 つ
  - `telemetry_health` は decision trace にも `control tick` のログにも出ない
  - `threshold_profile` は Guard の発火と解除のログにしか出ない
- 所有者は 2026-10-15 に、この行を実機の観測の代わりに CI のテストで満たすことにした（[0136](decisions/0136-t7-t8-t9-acceptance-exceptions.md) §2.3）。
  テストは、`power.cpu.package` の時刻だけを古くした frame を、本番と同じ signal の組み立てから Reactive Guard まで通し、
  健全度が `DEGRADED`・閾値が `conservative` になり、新しい sample で戻ることを確かめる。テストは #308（PR #314）で入った:
  `tests/test_control_loop.py` の `test_cpu_power_staleness_alone_degrades_health_and_makes_the_guard_conservative`。
  実機で `cpu_power_unavailable` が出て、fault が上がらなかったことは、テストの前提（収集の側が CPU Power を使えないと判定した）を裏付ける
- 戻した後の T8 の全スレッドで、Guard の発火 4 回はすべて `threshold_profile` が `normal` だった。ただしこれは約 1 分後の値で、新しい sample を観測した tick の健全度を示さないので、合否の材料にしない
- 健全度と閾値は #308（PR #314、`ControlTick` v15・決定記録 0137）で trace に載った。0123 §2.3 の、T1 で健全度が `NORMAL` だった時間の割合は
  下の T1 の節にある（86400 tick すべて `NORMAL`）
- T9 (a)（fand の計時の `telemetry.cpu_power_ms`）は、0123 §2.2 のとおり実機では行わず、CI のテストで確かめる

### T1: 24 時間の連続運転（2026-10-15 16:36:04〜2026-10-16 16:36:05）

**判定: 合格（2026-10-16、所有者が確認）。** 下の表の判定は、0121 §2.3 の合格の条件に当てた集計の結果。

0121 §2.3 のとおり、正式の unit（`--backend hwmon`）を `systemctl start` で動かし（`enable` しない）、所有者の普段の使い方のまま
24 時間 fand に制御させて、`systemctl stop` で終えた。`/opt` の版は `d3c5404`、decision trace は v15（0137）。fand の再起動は 0 回で、
止めた後の `pwmN_enable` は 3 zone とも 5（BIOS の制御）に戻った。記録は `t1-10151636-*`（`~/coldaisle-host-work/`。DB から書き出した
reading と trace、fand の journal、`coldaisle-soak-report` の集計）。

| 測るもの（0121 §2.3） | 結果 | 合格の条件 | 判定 |
|---|---|---|---|
| `duration_ms` の分布 | 最大 11 ms・p99.9 6 ms・p99 5 ms（86400 tick） | 最大が 125 ms 未満 | 合格 |
| `deadline_exceeded` の数と連続の最長 | 0 回（連続の最長 0）。停止時の集計の `overruns` も 0 | 連続の最長が 1 以下 | 合格 |
| tick の間隔（`control tick` のログの journal の単調時計の差） | 最大 1023.96 ms・p99.9 1019.6 ms・p99 1012.0 ms・最小 977.0 ms | 最大が 1500 ms 未満・p99.9 が 1100 ms 未満 | 合格 |
| `stats.skipped_slots` | 0（停止時の集計。下の注を参照） | 0 | 合格 |
| systemd の watchdog による再起動 | 0 回（journal に `Watchdog timeout` は無い） | 0 回 | 合格 |
| `*_telemetry_stale`（`cpu_ms` / `gpu_ms` / `air_ms`）と `cpu_power_unavailable`（0123 §2.3） | 0 回 | 0 回 | 合格 |
| `write_failure` / `readback_mismatch` / `enable_reverted` | 0 回 | 0 回 | 合格 |
| `tach_stall` | 0 回（起動の直後を含む。T4 の注の確認） | 0 回 | 合格 |

- **`skipped_slots` などは、fand が正常停止のときにログに出す集計（`ControlStats`）の値。** journal の「control daemon を終了する」の行で、
  `ticks` 86400・`overruns` 0・`skipped_slots` 0・`recorded` 86400・`trace_dropped` 0・`emergency_max` false。
  `control tick` のログには `skipped_slots` の欄が無いので、この行を根拠にした。DB の trace も 86400 tick（`tick_id` 0〜86399）で、`recorded` と一致する
- fault は 0 回。`safety_state` は起動の直後の 17 tick が `startup`（`startup_max` で 3 zone とも Max）、残りの 86383 tick は `normal`。
  `mode` はずっと `auto`
- trace の reasons の内訳（zone ごとの件数の合計）: `ramp_down_limit` 7232・`reactive_guard_hold` 4826・`reactive_guard_triggered` 520・
  `cpu_cooling_floor` 150（Top だけ）・`startup_max` 51
- `bound_by`（tick の数）: `requested` が Front / Rear 82669・Top 81263、`guard_floor` が Front / Rear 1319・Top 2577、`ramp_down` が
  Front / Rear 2395・Top 2394、Top の `safety_floor` 149、`forced_max` が各 17。`effective` は 3 zone とも 0.40〜1.00、平均は約 0.585
- **起動の落ち着き（0126 §2.3）**: 全開の回転数を T4 の起動の落ち着きの 3 回で独立に測った値（`ctl max` を 30 s 保った後。Front 1790〜1795・
  Rear 1546〜1578・Top 2343〜2368 RPM）とし、どの回を全開としても ±5% に入る範囲で判定した。起動（BIOS の制御の回転数から）の `startup` の
  17 tick で、範囲に入ったのは Top が tick 4・Rear が tick 6・Front が tick 12（12001 ms）で、Demand が `ramp_down` で下がり始めた
  tick 17（17001 ms）までどれも外れなかった（試験の記録の trace から 2026-10-21 に数えた。決定記録 0161 §2.2）
- Learned は配備していないので、起動時に経路を開かずに運転する error が 1 件出た（T0 と同じ）。fand の journal の error はこれだけ
- systemd の記録では、fand の CPU 時間は 24 時間で 4 分 46 s、メモリの最大は 288.7 MB

**0123 §2.3・0137 §2.3 の記録（合否にしない）**

| 測るもの | 結果 |
|---|---|
| telemetry の健全度（0137 §2.3 の SQL。`(ticks, normal, conservative, guard_unknown)`） | (86400, 86400, 0, 0)。`NORMAL` の割合 100%。期間 86401 s ÷ `tick_ms` に対して記録の欠けは無い（停止時の集計の `recorded` 86400・`trace_dropped` 0） |
| Reactive Guard の発火（`reactive guard started` のログ） | 431 回（同じ数だけ `released`）。閾値の組はどの tick も `normal` |
| power の floor が Top を決めた時間（0123 §2.1 の最後の行） | 7 tick（約 0.008%）。2026-10-16 の 12:00・13:12・14:05 に 2〜3 tick ずつ |

- power の floor が Top を決めた tick は、T8 と同じ判定（Top の `safety_floor` が、前後 2.5 s の `cpu.package` の最大値から計算した温度の曲線の値より
  0.05 以上高い）で数えた。そのときの `safety_floor` は 0.83〜0.86、CPU は 71〜73 °C、前後 2.5 s の `power.cpu.package` の最大は 174〜179 W。
  24 時間の `power.cpu.package` の最高は 179 W で、100 W を超えた sample は 34561 のうち 53。そのときの CPU の使われ方（どの作業か）は記録に無い。
  音とのつり合いは所有者が判断する

**温度**（中止の基準: CPU 88・GPU 85・12V-2x6 70 °C。どれにも届かなかった）

| メトリクス | 最低〜最高（°C） |
|---|---|
| `cpu.package` | 33〜81 |
| `gpu.0.core` | 24〜79 |
| `board.connector_12v2x6` | 21〜47 |
| `air.front_intake` | 16.4〜26.1 |
| `air.gpu_exhaust` | 21.3〜54.9 |

**取り込みの soak（`coldaisle-soak-report`、#47）**: 欠測率は全 `air.*` チャネルで 0.000%、デバイスの再起動 0、`sys.dropped_samples`・
`sys.queue_drops`・`suspect` は 0。DB で判定できる項目は合格。シリアルの再接続回数と取り込みのデーモンの RSS は DB に記録経路が無く、
確かめていない（総合の判定は「判定不能」）。

- **中止の基準の見張り（`coldaisle-t1-watch`）は途中で一度止まった。** 2026-10-16 10:33 に起動し直した。止まっていた間は、
  中止の基準の監視が無かった。fand 自体はその間も動き続け（再起動 0・tick の欠け無し）、上の温度の最高は 24 時間の DB の記録から
  数えたもので、見張りの止まっていた間も含めて中止の基準に届いていない

### 残り

- 0121 の実機の試験はすべて終わった。T5 の約 11 s の経路は実機では試さず [0158](decisions/0158-t5-11s-path-component-assurance.md) で担保し、
  0158 §2.5 の T1〜T3 のテストは PR #355（890949e）で入った。T9 (b) の健全度と Guard の閾値のテストは #308（PR #314）で入った
- `enable` までに残る判断（D 状態の hang の受け入れ・承認点 2 で値の `status` を `confirmed` にすること・unit の時間の値・
  HEAD へ更新した後の確認のやり直し・順序）は、決定記録 [0161](decisions/0161-fand-enable-prerequisites.md) で決めた（2026-10-21、所有者の承認）。
  D 状態の hang の調べ（0158 §5 の 2）の結果も 0161 §2.1 にある（2026-10-13 以降の `journalctl -k` の hung_task 0 件・T1 の `duration_ms` 最大 11 ms・
  書き込みの失敗 0。予備確認から T1 の前までの trace と journal も 2026-10-22 に確かめ、試験の注入で説明できないものは 0）
- T1 は合格（2026-10-16、所有者が確認）。値の `status` を `confirmed` にするのは、所有者の承認を得てまとめて行う（項目と `basis` は 0161 §2.2）

## HEAD での確認（決定記録 0161 §2.4。2026-10-22）

**判定: R1〜R3 は 0161 §2.4 の合格の条件をすべて満たした。ただし R3 は、2 時間の本回と CPU の負荷を補う回の組で満たした（0161 §2.4 の手順からの逸脱。決定記録 [0165](decisions/0165-r3-split-cpu-load-run.md) で扱う）。所有者はこの結果で承認点 3（0121 §2.10）を完了と判断した（2026-10-22）。0165 は同日に所有者が承認した。`enable`（0161 §2.5 の手順 8〜10）は本記録のマージの後に行う。**
R3 の CPU の負荷は本回でかからず、所有者の判断で別の回で補った（下の「R3 の補い」）。

`/opt` の版は `5ff6f96`、decision trace は v16、Control Config は v17（`fan-policy.yaml` v13・`safety.yaml` v5、
`supervisor.workload_hint.stage: off`）。0161 §2.5 の手順 3〜5（`/opt` の更新と migration 0014〜0016・Control Config の移行と
§2.2 の 15 項目の `confirmed`・unit の `-I -m` の形への置き直し）は同じ日に済ませた。unit の差は 0157 のコメント 6 行と `ExecStart=` の
1 行だけで、`systemd-analyze verify` は何も出さず、引き継ぎ実行部は `exit=0`。どの回も `systemctl start` だけで動かし（`enable` しない）、
終わると `pwmN_enable` は 3 zone とも 5（BIOS の制御）に戻り、fand は `disabled` のまま。記録は `r1-10221203-*`・`r2-ready-0161.txt`・
`r3-10221231-*`・`r3b-10221502-*`（`~/coldaisle-host-work/`）。

起動のたびの起動ログは `control_config_version=17`・`safety_config_is_provisional=False`・`safety_disabled_inputs=[]` で、
`safety.yaml` の暫定値は 0 件（`provisional_values` 59 件はすべて `safety.yaml` の外）。最初の trace は `schema_version=16`・
`mode=auto`・`safety=normal`・`workload_hint.stage=off`・`safety_provenance.config_is_provisional=False`。動いているプロセスの引数は
どの回も `-I -m coldaisle.control_daemon`。

### R1: 0080 段階 5 の短い確認（2026-10-22 12:03〜12:05、アイドル・所有者が現地）

| 確かめたこと | 結果 | 合格の条件 | 判定 |
|---|---|---|---|
| heartbeat の直後に `kill -STOP` → Max | 5768 ms（位相 0.9 ms）。引き継ぎは 3 zone とも `applied`、`RestartSec` の後に再起動して takeover | 約 5 s（T5 のアイドル 5294 ms 程度）・0121 §2.7 の約 11 s 以内 | 合格 |
| `kill -KILL` → Max | 59 ms（`pwm_enable=0,0,0`）。引き継ぎは 3 zone とも `applied`、再起動して takeover | 引き継ぎが 3 zone に Max・再起動で takeover | 合格 |
| `systemctl stop`（2 回） | `pwm_enable` 5,5,5・`/run/coldaisle` は残る（`drwx--x--x coldaisle-fan:coldaisle-fan`）・正常停止のログあり | 3 zone とも BIOS の値・`/run/coldaisle` が残る | 合格 |
| 引数 | 4 回の起動すべてで `-I -m` | `-I` と `-m` がある | 合格 |

- **`kill -STOP` の後の終了は `status=11/SEGV` だった**（それまでの journal の 13 回はすべて `6/ABRT`）。watchdog の SIGABRT を受けた後、faulthandler
  （0080 §2.5）が traceback を書き出す途中で落ちた。メインのスレッドが止められた位置（pydantic の処理の途中）によるもので、起動の形とは関係が無い。
  プロセスは終わり、引き継ぎ実行部が Max を書き、時間も基準の内なので、安全への影響は無い（書き出しの途中で固まっても `TimeoutAbortSec` で
  SIGKILL になる）。失ったのはメインのスレッドの traceback の一部だけ
- Learned は配備していないので、起動時に経路を開かずに運転する error が各回 1 件出た（T0・T1 と同じ）

### R2: `READY=1` までの時間（0157 §5 の 4）

`ExecMainStartTimestampMonotonic` と `ActiveEnterTimestampMonotonic` の差。

| 回 | OS の起動から | `READY=1` まで |
|---|---|---|
| R1 の起動・watchdog の後の再起動・2 回目の起動・SIGKILL の後の再起動 | 約 8.4 日 | 323・318・315・322 ms |
| OS の再起動の直後（cold） | 230 s | 331 ms |
| R3 の起動・R3 の補いの起動 | 1215 s・10256 s | 317・307 ms |

7 回（OS の再起動の直後を 1 回含む）で最長 331 ms。`TimeoutStartSec` 30 s の半分の 15 s 未満で、0161 §2.3 の条件を満たす。
`stage: off` では指紋を計算しないので、B1 を設定する版で測り直す（6.9 の手順 7 の注）。

### R3: 短い soak（2026-10-22 12:31:22〜14:31:27）

所有者の普段の使い方に加えて、開始から 60 分に GPU 600 W を 10 分かけた（`gpu_burn.py`、603 s・中央値 600 W）。CPU の全スレッド 10 分は
20 分にかける予定だったが、かからなかった（下の注）。

| 測るもの（0121 §2.3） | 結果 | 合格の条件 | 判定 |
|---|---|---|---|
| `duration_ms` | 全 7205 tick で最大 8 ms（`startup` の 17 tick は最大 5 ms）。`startup` を除く 7188 tick の p99.9 は 6 ms | 最大が 125 ms 未満 | 合格 |
| `deadline_exceeded` | 0 回（連続の最長 0） | 連続の最長が 1 以下 | 合格 |
| tick の間隔（journal の単調時計） | 全 7205 tick で最大 1020.2 ms・p99.9 1019.5 ms（`startup` を含めても除いても同じ） | 最大が 1500 ms 未満・p99.9 が 1100 ms 未満 | 合格 |
| `skipped_slots`（停止時の集計） | 0（`ticks` 7205・`overruns` 0・`recorded` 7205・`trace_dropped` 0・`emergency_max` false） | 0 | 合格 |
| systemd の watchdog による終了・再起動・異常終了 | 0 回 | 0 回 | 合格 |
| `*_telemetry_stale` / `write_failure`・`readback_mismatch`・`enable_reverted` / `tach_stall` | 0 回（fault は 0） | 0 回 | 合格 |
| `cpu_power_unavailable`（0123 §2.3。Top の reason の detail で、fault にはならない） | 0 tick。DB の `power.cpu.package` の sample は 2882 件で、間隔の最大 2502 ms（古さの上限 5000 ms に届く間隔は 0） | 収集が止まった時間を除き 0 回 | 合格 |
| trace の `schema_version` と欠け | すべて 16（7205 tick）・`tick_id` の飛び 0・ts の差 1500 ms 以上 0 | 16・欠け 0 | 合格 |

- 健全度は 7205 tick すべて `normal`。`safety_state` は `startup` 17 tick・`normal` 7188 tick、`mode` はずっと `auto`
- Reactive Guard の発火 22 回（Top の `cpu_temperature_rise` 20・Front / Rear の `gpu_power_rise,gpu_temperature_rise` 各 1）。
  Top の `bound_by` は `requested` 6318・`guard_floor` 622・`ramp_down` 248・`forced_max` 17。power の floor が Top を決めた tick は 0
- 温度（°C）: `cpu.package` 38〜77・`gpu.0.core` 28〜79・`board.connector_12v2x6` 25〜50・`air.front_intake` 22.9〜24.8・
  `air.gpu_exhaust` 25.1〜55.3。中止の基準（CPU 88・GPU 85・12V-2x6 70）には届かなかった
- **CPU の負荷が 5 s で終わった。** 負荷は root の一時 unit（見張り）から所有者の uid で stress-ng を起動する作りで、その unit の作業ディレクトリが
  `/` だった。stress-ng は temp-path（既定は作業ディレクトリ）に書けないと始めずに終わる（`temp-path '.' must be readable and writeable`）。
  T7・T8 は所有者の端末から起動していたので起きなかった。試験のスクリプトの不具合で、fand とは関係が無い

### R3 の補い: CPU の全スレッド 10 分（2026-10-22 15:02:03〜15:27:07）

所有者の判断（2026-10-22）で、R3 を 2 時間やり直さず、別の回で CPU の負荷だけを補った（決定記録 0165）。正式の unit で 25 分（普段 5 分・CPU の全スレッド
10 分・普段 10 分）。stress-ng に `--temp-path /tmp` を付け、始める前に同じ形で起動できることを確かめた。負荷は 603 s、
`power.cpu.package` の中央値 200 W・最大 200 W、32 本の worker が最後まで動いた。

| 測るもの（0121 §2.3） | 結果 | 判定 |
|---|---|---|
| `duration_ms` | 全 1504 tick で最大 18 ms（`startup` の 17 tick は最大 5 ms）。`startup` を除く 1487 tick の p99.9 は 12 ms | 合格 |
| `deadline_exceeded` | 0 回 | 合格 |
| tick の間隔 | 全 1504 tick で最大 1028.8 ms・p99.9 1022.9 ms（`startup` を除くと p99.9 1023.0 ms） | 合格 |
| `skipped_slots` | 0（`ticks` 1504・`overruns` 0・`recorded` 1504・`trace_dropped` 0） | 合格 |
| watchdog による終了・再起動・異常終了 | 0 回 | 合格 |
| stale / 書き込みの失敗 / `tach_stall` | 0 回 | 合格 |
| `cpu_power_unavailable`（0123 §2.3） | 0 tick。`power.cpu.package` の sample は 602 件で、間隔の最大 2502 ms（5000 ms に届く間隔は 0） | 合格 |
| trace の `schema_version` と欠け | すべて 16（1504 tick）・欠け 0 | 合格 |

- 判定のスクリプト（`r3-verdict-0161.py`）の「期間がおよそ 2 時間」と「GPU の負荷 10 分」の行は 2 時間の本回のための行で、この回では
  想定どおり不合格・NG になる。2 時間の期間と GPU の負荷は本回で満たしている
- 健全度は 1504 tick すべて `normal`。Guard の発火 1 回（Top の `cpu_temperature_rise`）。Top の `bound_by` は `requested` 859・
  `safety_floor` 600（reason `cpu_cooling_floor`）。power の floor が Top を決めた tick は 458（CPU 電力の中央値 200 W）
- 温度（°C）: `cpu.package` 39〜84・`gpu.0.core` 29〜30・`board.connector_12v2x6` 26〜27・`air.front_intake` 24.1〜25.3。
  CPU の最高 84 °C は中止の基準 88 °C の 4 °C 下（T1 の 24 時間の最高は 81 °C）
