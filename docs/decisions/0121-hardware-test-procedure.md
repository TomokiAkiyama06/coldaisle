# 決定記録 0121: `provisional` の Safety の値を確定する実機の試験の手順（0119 §5 の 5）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-14、リポジトリ所有者の承認）
- **Date**: 2026-10-13
- **Supersedes**: なし（0119 §5 未決 5 を閉じる）
- **Superseded by**: §2.3 の `*_telemetry_stale` の行のうち `cpu_power_ms` を確定しないとした部分と、§3 の同じ趣旨の項は [`0123`](0123-cpu-power-floor-tests.md)
- **Superseded by**: §2.5 T3 と §2.8 T6 の時間の条件の境界の読み方（trace の壁時計の ±50 ms）と、T3 の落ち着きの判定は [`0124`](0124-trace-wall-clock-tolerance.md)。§2.8 T6 の立ち上がりの測り方と合格の条件（fand の trace で測り tach の更新の閾値と fand の 1 tick を足す・Rear の 90% の時間は記録だけ）は [`0125`](0125-stall-rise-criterion-tach-update.md)。§2.6 T4 の温度の判定で `cpu.package` の操作の直後 15 s を除くことは [`0126`](0126-startup-settle-front-spinup.md)
- **Superseded by**: §2.1 の中止の基準のうち `gpu.0.core` 83 °C（→ 85 °C）は [`0130`](0130-gpu-fan-quieter-curve.md)
- **Superseded by**: §2.9 の吸気の差が ±1 °C を超えたらやり直す決まりを、2026-10-15 の T7 の GPU 600 W の試行 4 にだけ当てないことは [`0136`](0136-t7-t8-t9-acceptance-exceptions.md)
- **関連**: [`0028`](0028-fan-control-contracts.md) §2.6〜§2.9 /
  [`0080`](0080-fand-systemd-unit.md) §2.3 / §2.6 / §2.10 /
  [`0118`](0118-hwmon-backend-label-less-headers.md) /
  [`0119`](0119-safety-values-first-host.md) §2.11〜§2.14 / §5 /
  [`0120`](0120-fan-hardware-first-host.md) /
  `docs/fan-header-mapping.md`「fand の実機の予備確認（2026-10-13）」
- **対象 Issue**: #74（実機の確認）/ #75

---

## 1. Context

0119 §2.13 は、時間と回数の値を実機の試験で裏付けるまで `provisional` に置き、その手順を承認点 3 の前に別の決定記録で
設計することを求めた（§5 未決 5）。2026-10-13 の予備確認（`docs/fan-header-mapping.md`）で、fand が導入先で
制御を取り、止まれば Max を書き、正常停止で BIOS へ返すことは見えた。本記録は、値を `confirmed` にするための試験を決める。

設計の前提として、コードを確かめて分かったこと:

- fand に、試験のために故障を起こす引数・環境変数・管理ソケットの命令は無い。`SimulatedFanBackend` の `fault_plan` は
  CLI に出ておらず、hwmon には効かない
- `zone_min_demand` はどのモードでも効き、manual でも下回れない。CALIBRATION は管理ソケットが受けない
- 別の `safety.yaml` で動かす方法は `--config-dir` だけ。`provisional` の値は起動を止めない（記録に残るだけ）。
  `stall_check_min_demand` は `zone_min_demand` 以下、`fan-hardware.yaml` の `minimum_stable_demand` は Demand を引き上げる
- `pwmN` / `pwmN_enable` は起動時に開いて持ち続ける。外から root が `pwmN_enable` に 2 以上（BIOS の自動制御の `5`）を
  書くと、fand は毎 tick `enable_reverted` を観測する（`EBUSY` か読み戻し）。fand は takeover の後に `1` を書き直さない
- heartbeat（`WATCHDOG=1`）は tick ごとに書き込みと読み戻しの直後に送る。heartbeat ごとのログは無く、最も近い目印は
  tick の後に出る `control tick` のログ（trace の保存の分だけ遅れる）
- 導入先では `power.cpu.package` が無いため、telemetry の健全度は常に `DEGRADED` で、Reactive Guard は
  degraded の閾値を使う（0119 §5 未決 4 の入力を足すまで）
- 4 日分の運転データでの模擬（Fallback・Guard・floor・ramp の規則を写したもの）では、CPU 70 °C 以上か GPU 75 °C 以上の
  時間（全体の 8%）のうち約 4 割で、fand は BIOS より Fan を遅く回す。0119 §2.14 の曲線（82 °C で 1.0）が BIOS
  （75 °C で 100%）より緩いため。Fan を変えたときの温度は過去のデータから計算できないので、実機で比べる（§2.9）

## 2. Decision

### 2.1 共通の決まり

- **制御の経路を迂回しない**（0119 §5 の 5 (a)）。Demand は fand の自動制御か管理ソケットの manual / max / auto で決め、
  Guard と Critical Safety を通す。sysfs へ人が書くのは、§2.4 の故障の注入の `pwmN_enable=5`（その Fan を BIOS の
  自動制御へ戻す）と、§2.4 の一時的な失敗の試験で書き戻す `pwmN_enable=1`（fand が takeover の後に保つ状態へ戻す。
  所有者が試験の間に限って承認、2026-10-14）だけとし、§2.4 の条件で BIOS の出力が fand の出力を下回らないときに限る
- **試験専用の設定**は §2.8 の stall の試験だけで使う。`/etc/coldaisle/control-config-test-<名前>/` に4ファイルを置き、
  unit の drop-in で `--config-dir` を差し替える。試験の後に drop-in と設定を消し、unit の `ExecStart` が元に戻ったことを確かめる
- **所有者が現地にいて**、各試験の開始と終了を行う。操作のスクリプトは事前に内容を示し、所有者が実行する
- **記録**: 毎 tick の `control tick` のログ（`duration_ms`・`deadline_exceeded`・`faults`・`safety_state`・`effective`）と
  decision trace、`pwmN` / `pwmN_enable` / `fanN_input` と主な温度の 0.5 s ごと（§2.7 は 0.1 s ごと）の読み取りを残す
- **中止の基準**: `cpu.package` 88 °C・`gpu.0.core` 83 °C・`board.connector_12v2x6` 70 °C のどれかに達したら、すぐに負荷を
  止める。fand の挙動がおかしければ `systemctl stop coldaisle-fand`（BIOS へ返す）。それも効かなければ
  `pwmN_enable` に `5` を書く
- **負荷**は §2.7 / §2.9 で GPU 600 W（行列計算）と CPU の全スレッド（`stress-ng`）を別々に使い、同時にはかけない
  （所有者の選択。普段の最大まで）
- **Learned を配備していない**ので、`tick_deadline_ms` / `tick_ms` / `overrun_consecutive_limit` の確定は
  「Learned の経路を開かない運用」に対してのものとする。Learned を配備するときは §2.3 の soak をやり直す
- 試験の結果が今の値を支えなければ、値を変える案を別の決定記録で所有者に出す（0119 §2.13）

### 2.2 T0: 0080 段階 5 の残り

本番の DB の写しを `0640` にして、simulated backend で fand を起動し、終了コード 5（`db_not_writable`）で制御を取らずに
終わることを見る。写しを `0660`・グループ `coldaisle` に直して起動できることを見る（移行手順）。fand を数分動かした後、
`/var/lib/coldaisle` が `drwxrws---`（`2770`）のままで、fand が作った `-wal` / `-shm` のグループが `coldaisle` であることを見る。
写しは消す。

### 2.3 T1: 24 時間の連続運転（soak）

正式の unit（`--backend hwmon`）を `systemctl start` で動かし（`enable` しない）、24 時間、所有者の普段の使い方のまま
fand に制御させる。終了は `systemctl stop`。

| 測るもの | 確定する値 | 合格の条件 |
|---|---|---|
| `duration_ms` の分布（p99.9・最大） | `tick_deadline_ms` 250 | 最大が 125 ms（期限の半分）未満 |
| `deadline_exceeded` の数と連続の最長 | `overrun_consecutive_limit` 3 | 連続の最長が 1 以下。超過があれば理由を示す |
| tick の周期の乱れ（`stats.skipped_slots`、単調時計での間隔） | `tick_ms` 1000 | `skipped_slots` が 0、systemd の watchdog による再起動（`Watchdog timeout`）が 0、tick の間隔（`control tick` のログの journal の `__MONOTONIC_TIMESTAMP` の差。壁時計の `ts_ms` は時刻同期で飛ぶので使わない）の最大が 1500 ms 未満で p99.9 が 1100 ms 未満。MPC・Supervisor の期限との不変条件は設定の検証が通ること（`duration_ms` は heartbeat までしか測らないので、間隔と再起動で heartbeat の後の詰まりも捉える） |
| `*_telemetry_stale` の数 | `telemetry.cpu_ms` / `gpu_ms` / `air_ms` / `cpu_power_ms` 5000 | 0 回（収集が止まった時間を除く）。`cpu_power_ms` は入力が無いので確定しない |
| `write_failure` / `readback_mismatch` / `enable_reverted` の数と連続の最長 | `write_fail_emergency_after` 3（正常な側） | 0 回。あれば連続の最長が 2 以下 |
| `tach_stall` の数 | `stall_window_ms` 5000（正常な側） | 0 回 |

### 2.4 T2: 書き込みの失敗（0119 §5 の 5 (c)）

アイドル（`cpu.package` 50 °C 未満・`gpu.0.core` 45 °C 未満・`power.gpu.0` 50 W 未満）のときに、manual で全 zone を 0.35 にし、
`control tick` の `effective` が3 zone とも 0.35 になった（Guard の floor が入っていない）のを確かめてから、所有者が root で
1つの zone の `pwmN_enable` に `5` を1回書く。Front・Top で1回ずつ行う。

BIOS の自動制御は全 zone を CPU の温度だけで決め、アイドルでも約 0.37 以上で回す（0119 §1・§2.14）。fand の出力を下限の 0.35 に
しておくことで、注入の後に終了と引き継ぎの Max が来るまでの約 5 s の間も、注入した Fan が fand の出力より遅くならない。
GPU の負荷の下では fand は GPU の温度で全 zone を上げるので、BIOS へ戻すと遅くなりうる。負荷の下では注入しない。

| 測るもの | 確定する値 | 合格の条件 |
|---|---|---|
| Front: `enable_reverted` → Front Max・ほかは `fault_demand` 1.0（`DEGRADED`）→ 3 回目の評価で `EMERGENCY` | `write_fail_emergency_after` 3（故障の側） | 3 回で `EMERGENCY` に上がる |
| Top: 1 回目で `EMERGENCY` | — | 1 回で上がる |
| 最初の失敗から終了コード 7 まで | `hardware_write_fail_exit_ms` 5000 | 5000〜6000 ms で終了し、引き継ぎの記録が残り、`ExecStopPost` が3 zone に Max を書く |
| 再起動の後 | 同上 | takeover が成功し（`enable=1`）、終了コード 7 の繰り返しにならない |

**一時的な失敗からの復帰**: 同じ条件（アイドル・全 zone manual 0.35・`effective` が 0.35）で、Front に `pwmN_enable=5` を書き、
1.2 s 後に `pwmN_enable=1`（fand が takeover の後に保つ manual の状態）を書き戻す。fand は takeover の後に `1` を書き直さないので、
書き戻すまでの 1〜2 tick だけ失敗する。書き戻した時点の PWM は BIOS が決めた値（アイドルで約 0.39）で、fand の 0.35 より低くならない。

| 測るもの | 確定する値 | 合格の条件 |
|---|---|---|
| 失敗の回数と、その後の tick | `write_fail_emergency_after` 3（正常な側）・`hardware_write_fail_exit_ms` | 失敗は 1〜2 回で、その後の tick で書き込みと読み戻しが成功し、`EMERGENCY` にも終了コード 7 にもならない（数え直しと終了の計時がリセットされる）。3 回に届いた試行は使わずにやり直す |

### 2.5 T3: データの遅れと復帰

fand が auto で動いている間に、`coldaisle-telemetry` を 30 s 止めて戻す。次に `coldaisle-daemon`（取り込み）を 30 s
止めて戻す。期待する動きは止めるサービスで違う。

| 測るもの | 確定する値 | 合格の条件 |
|---|---|---|
| fand が最後の新しい sample を初めて観測した tick（その sample の DB の時刻の直後の tick。fand の age はここから数える）から `cpu_telemetry_stale` / `gpu_telemetry_stale` まで（`coldaisle-telemetry` を止める） | `telemetry.cpu_ms` / `gpu_ms` 5000 | 5000 ms 以上・5000 ms + 1 tick 以内に上がり（早すぎても不合格）、Top が Max・Front / Rear が `fault_demand` 1.0 になる |
| fand が最後の新しい air の sample を初めて観測した tick から `air_telemetry_stale` まで（`coldaisle-daemon` を止める） | `telemetry.air_ms` 5000 | 5000 ms 以上・5000 ms + 1 tick 以内に上がり（早すぎても不合格）、Front / Rear だけが `fault_demand` 1.0 になる。Top は自分の floor と Fallback のまま（Max にならない） |
| 新しい sample が届いて stale の状態が観測されなくなった最初の tick から、fault が消えるまで（サービスを戻した時刻からではない。起動と最初の書き込みの遅れを混ぜない） | `fault_clear_hold_ms` 30000 | 30 s（+ 1 tick 以内）保持してから消え、その間と後に fault が出入りしない |
| 戻した後に温度と tach が落ち着く時間 | 同上 | 30 s より短い |

### 2.6 T4: 下げる速さと起動の落ち着き

アイドルと GPU 300 W の一定の負荷のそれぞれで、温度が落ち着いた後に `ctl max` を 2 分、続けて manual で全 zone 0.35 にし、
`effective` が実際に 0.35（最低安全 Demand。CPU の floor と Guard が上げる分はそのまま）に届いてから 60 s 見て、`ctl auto` に戻す。
auto のままでは Fallback が 0.40 以上を求めて 0.35 まで下がらないので、Max から最低安全 Demand までの全区間を manual で通す
（Guard と Critical Safety は通る）。中止の基準に近づいたら、すぐに `ctl max` に戻す。また、T1 / T2 / T3 の起動（takeover）の記録を使う。

| 測るもの | 確定する値 | 合格の条件 |
|---|---|---|
| Max から 0.35 へ下げ終えるまでと、0.35 に届いた後 60 s の温度 | `ramp_down_per_s` 0.05 | 各 tick の `effective` の下がり幅を、trace の `ramp_down_limit` の reason にある `elapsed_ms`（fand が下降量の計算に使った tick 開始の単調時刻の差）で割って求めた下降率（trace の `ts_ms` は壁時計、journal の時刻はログの出力時刻なので使わない）。0.35 に届く終端の tick は `bound_by` が `safety_floor` になり `ramp_down_limit` の reason と `elapsed_ms` が出ないので、下降率の判定には使わない（その tick の `effective` は safety floor で、ramp の許す値以上なので、下がり幅は作りの上で ramp を超えない）。下降率は `elapsed_ms` のある途中の tick（1.0 から 0.35 まで約 13 tick）の実測だけで判定し、各試行で 10 tick 以上を要するが全区間で 0.05/s 以下（+ 1 tick の丸め）で、Max から 0.35 に届く直前までの各 tick の `bound_by` が `ramp_down` であること（0.35 に届いた tick は `safety_floor` でよい。即座に下がったら不合格）。どの温度も、下げる前の定常より 3 °C 以上上がらず、上がりは 2500 ms の収集周期で捉えられ、中止の基準に近づかない |
| takeover から tach がその Demand の最終の回転数の ±5% に入るまでと、温度の落ち着き | `startup_settle_ms` 5000 | 5000 ms より短い |

### 2.7 T5: watchdog（0119 §5 の 5 (d)）

開始の状態は3種類にする。Max（`pwmN_enable=0`）を確かめたら負荷を止め、`systemctl stop` で戻す。

**止める位相は heartbeat を基準にする。** `control tick` のログは heartbeat（`WATCHDOG=1`）の後、trace の保存などを終えてから
出るので、ログからの遅れは heartbeat からの位相ではない。`SIGSTOP` の直前に systemd が最後の通知を受けた時刻
（`systemctl show -p WatchdogTimestampMonotonic coldaisle-fand`）を読み、`SIGSTOP` の単調時計の時刻との差を位相とする
（`Watchdog timeout` のログの時刻は systemd の処理の遅れを含むので使わない）。ログの直後を起点に
遅らせる量を変えて繰り返し、各条件で位相 100 ms 以内の試行を少なくとも1つ得る。各条件の最長（最も長い hang から Max まで）を使う。

- **(i) 負荷の立ち上がり（最悪の候補）**: アイドルで fand の auto の Demand が低く落ち着いた状態で、**先に `SIGSTOP` し、
  その直後に** GPU 600 W の負荷を始める（負荷を始めてから止めると、その間の tick が新しい sample で Demand を上げうる）。
  CPU の全スレッドでも同じことを行う。止める直前の tick の `effective` が、記録したアイドルの値から変わっていた試行は使わない。
  Fan はアイドルの Demand のまま固まり、熱だけが上がる（負荷の始まりから数秒でチップの温度が大きく上がる。0119 §2.12 の
  16 s で +31 °C / +23 °C）
- **(iii) 熱がこもった状態からの再開**: GPU 600 W を 10 分かけて止め、fand の auto の `effective` がアイドルの値 + 0.05 以内に
  戻った直後に（冷却水と筐体はまだ温かい）、(i) と同じく先に `SIGSTOP` してから GPU 600 W を再び始める。CPU の全スレッドでも
  同じことを行う（10 分かけて止め、Top の `effective` がアイドルの値 + 0.05 以内に戻った直後に止めて再開する。AIO の冷却水が温かい）
- **(ii) 定常**: 負荷を4段階（アイドル・GPU 300 W・GPU 600 W・CPU の全スレッド）にし、fand の auto で温度が落ち着いた後に止める。
  Fallback と floor が Demand を上げた後の状態で、(i) と比べて開始の温度が高い側を押さえる

どれが最悪かは決めつけず、条件ごとの Demand と温度を記録して測定で示す。Fallback と floor の上げる分は、止めている間は効かない（書き込みが止まるため）。

| 測るもの | 確定する値 | 合格の条件 |
|---|---|---|
| `SIGSTOP` から Max まで | `watchdog_timeout_ms` 5000（unit の `WatchdogSec` と `TimeoutAbortSec` を含む） | 約 11 s（0119 §2.12）以内 |
| その間と Max の後 30 s の温度の上昇 | 同上 | (i)〜(iii) のどの条件でも、開始の温度 + 上昇が上限（`absolute_temp_ceiling_c` 90 °C、T_SENSOR 80 °C）より 5 °C 以上低い |

### 2.8 T6: stall（0119 §5 の 5 (b)）

最低安全 Demand 0.35 の下は制御の経路の中で作れないので、所有者が承認した**試験専用の設定**を使う。対象の zone
（Top・Front・Rear）を1つずつ、アイドルのときに、ほかの zone を manual で 1.0 にして行う。`stall_window_ms` は3 zone で共通なので、
3 zone すべての結果で確定する。

- **立ち上がり**: 対象の zone だけ `zone_min_demand` / `stall_check_min_demand` / `fan-hardware.yaml` の `minimum_stable_demand`
  を 0.0 に、`stall_window_ms` を 60000 にした設定で、manual で 0.0 にして Fan が止まるのを待ち（30 s 以内）、
  Top を対象にするときは、manual でも効く `cpu_cooling_floor` の低温側の点（60 °C → 0.35）と、CPU Power の入力を有効にしていれば
  `cpu_power_cooling_floor` の低電力側の点（100 W → 0.35）も 0.0 にする（高温・高電力側の点はそのまま残し、温度か電力が上がれば
  floor は戻る。アイドルで CPU 60 °C 未満のときだけ行う）。
  manual で 0.35 にする。0.35 にしてから `fanN_input` が `stall_min_rpm` に届くまでの時間を測る。3 回行う。
  Rear は PWM 0 でも止まらない（約 440〜450 RPM）ので、同じ設定で manual 0.0 の間の最低の回転数が `stall_min_rpm`（350）より
  高いままであることと、0.0 から 0.35 にして 0.35 の回転数の 90% に届くまでの時間を測る
- **検出**: 本番と同じ設定のうち、対象の zone の `stall_min_rpm` だけを、Demand 0.35 の回転数より高く全開の回転数より低い値
  （Top 1200、Front 900、Rear 1000）にした設定で、manual で 0.35 にする。`tach_stall` が上がり、Top は `EMERGENCY`、Front と Rear は
  自分が Max・ほかが 1.0（`DEGRADED`）になることを見る（検出の側は閾値を上げるので安全側）

| 測るもの | 確定する値 | 合格の条件 |
|---|---|---|
| 止まった状態から `stall_min_rpm` まで | `stall_window_ms` 5000 | 3 回の最長が 2500 ms（窓の半分）未満 |
| Rear の manual 0.0 の間の最低の回転数と、0.35 の 90% までの時間 | 同上 | 最低が 350 RPM より高く、時間が 2500 ms 未満 |
| stall の計時が始まった最初の tick（`effective` が `stall_check_min_demand` 以上で RPM が `stall_min_rpm` 未満になり、trace の `tach_unconfirmed_zones` にその zone が初めて出た tick。指示の時刻ではない）から `tach_stall` まで | 同上 | 5000 ms 以上・5000 ms + 1 tick 以内（早すぎても不合格） |

### 2.9 T7: 高温での BIOS との比較

同じ負荷を、BIOS の制御（fand を止めた状態）と fand の auto でそれぞれかける。負荷は GPU 600 W を 10 分、温度が戻ってから
CPU の全スレッドを 10 分。
温度は吸気（`air.front_intake`）との差で比べる。各試行は、`cpu.package` / `gpu.0.core` / `board.connector_12v2x6` / `air.*_exhaust` の吸気との差が、最初の
試行の前に記録したアイドルの値の ±1 °C 以内に戻ってから始める（冷却水と筐体の蓄熱をそろえる）。負荷の前の吸気の温度の差が
±1 °C を超えたらやり直す。順序の偏りを打ち消すため、負荷ごとに BIOS → fand → fand → BIOS の順で行い、同じ制御の2回の平均で比べる。

| 測るもの | 合格の条件 |
|---|---|
| `cpu.package` / `gpu.0.core` / `board.connector_12v2x6` / `air.*_exhaust` の最高と、最後の 2 分の平均 | 吸気との差の2回の平均で、fand の最高と最後の 2 分の平均が、どちらも BIOS の同じ値より 3 °C を超えて高くならず、中止の基準に届かない |
| GPU の throttle の flag | fand でだけ立つことがない |

条件を満たさなければ、0119 §2.14 の曲線を強める案を別の決定記録で出す。この比較は値の確定ではなく、`enable` の判断の材料とする。

### 2.10 順序と確定

T0 → T2 → T3 → T6 → T4 → T5 → T7 → T1 の順に行う（故障の注入と試験専用の設定を先に済ませ、最後に通常の設定で soak する）。
各試験の結果を `docs/fan-header-mapping.md` に残し、合格した値の `status` を `confirmed`・`basis` を本記録と結果の節にする
変更を、所有者の承認を得て `/etc/coldaisle/control-config/safety.yaml` に当てる（リポジトリには置かない）。
すべてそろってから承認点 3 を完了とし、`enable` は所有者が判断する。

## 3. Consequences

### 良くなること

- 0119 §2.13 の各値に、確定のための試験と合格の条件がそろう
- 制御の経路を迂回する試験が無い。外から書くのは BIOS へ戻す `5` だけ
- 高温で BIOS より弱い点を、`enable` の前に実機で確かめられる

### 悪くなること・その緩和

- 試験専用の設定（§2.8）は、対象の zone の下限を一時的に外す。アイドル・ほかの zone は全開・30 s 以内・所有者が現地、に限る
- 試験に半日〜2日かかる（soak が 24 時間）
- Learned を配備したら、§2.3 の時間の確定をやり直す必要がある
- `cpu_power_ms` と CPU Power の曲線は、入力が無いので確定できない（0119 §5 の 4）

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| fand を止めて sysfs へ直接書いて試す | Guard と Safety を迂回する（0119 §5 の 5 (a)） |
| 権限を外して書き込みを失敗させる | fand は起動時に開いたまま持つので失敗しない（§1） |
| Fan を物理的に止める治具で stall を作る | 回転中の Fan に触れる危険と治具の用意（所有者の選択） |
| soak を 8 時間にする | 夜間と普段の負荷の変化を取りこぼす（所有者の選択） |
| CPU と GPU を同時に最大で試す | 普段の運用より高温で、中止の基準に届きやすい（所有者の選択） |

## 5. 未決事項

| # | 内容 | 時期 |
|---|---|---|
| 1 | Learned を配備したときの §2.3 のやり直し | Learned の配備（0115） |
| 2 | `power.cpu.package` の入力と、それまで Guard が常に degraded の閾値を使うこと | 0119 §5 の 4 |
| 3 | unit の `TimeoutAbortSec` などの確定（0080 §5 の 1） | §2.7 の結果の後 |
