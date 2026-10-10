# Critical Safety

#78 の Critical Safety は `coldaisle.control.safety.CriticalSafety` と
stateful な `DemandComposer` に分かれる。前者は検証済み `SafetyConfig` と
1 tick の immutable snapshot から floor / forced Max / fault / safety state を決め、
後者は決定記録 0028 §2.4 の順序で effective demand を作る。モデル、
optimizer、Supervisor はいずれにも依存しない。

## 設定と未確定値

Safety の数値にコード上の既定値はない。必須項目を追加した Safety Config は
schema version 2 とし、v1 は安全値を補完せず明示的に拒否する。
`absolute_temp_ceiling_c`、
zone floor、CPU cooling curve（温度と CPU Power の2本）、stall の demand / RPM / window、連続書き込み失敗数、
fault demand、復帰 hold、overrun 数、ramp-down はすべて `SafetyConfig` から受け取る。
`provisional` の値も保守側の制約として適用するが、判定の
`config_is_provisional` を true にし、確定値と混同しない。実運用値の設定ファイルは
#50 / #75 の実測と所有者の承認までリポジトリに置かない。
設定不正時は `DemandComposer.for_invalid_config()` を使う。この専用 instance は
`SafetyConfig` や rate を必要とせず、`config_validated=false` と `config_invalid` fault を
伴う全 zone forced Max だけを初回も継続 tick も生成できる。通常の compose と混用できず、
`config_is_provisional=false` を「確定済み」と読まないようにする。Safety / Policy の
validation が失敗して full `ControlConfig` を作れない場合は、検証済み `FanHardwareConfig` と
対応する `fan-hardware.yaml` source metadata だけから専用 emergency runtime binding を作る。
caller が hash を自己申告する入口は持たず、trusted partial loader が同じファイル bytes を1回読み、
YAML validation と SHA-256 生成を一体で行う。通常・emergency とも hardware approval が
`confirmed` でなければ capability を発行しない。
この binding は通常の `CriticalSafety` を構築できず、通常 binding も config-invalid composerへ
切り替えられないため、同一 session で Max 後に通常の低 demand へ戻せない。

T_SENSOR の metric 名は決定記録 0032（FINAL）の
`board.connector_12v2x6` とする。Critical Safety はこの名前を既定値にせず、
T_SENSOR を有効化するときに `approved_t_sensor_metric` として承認済み metric contract
の注入を必須にする。注入名は canonical metric 文法を満たし、既存の Critical / 絶対温度
入力名と衝突せず、同時に注入する検証済み Metric Catalog に単位 `C` で存在することも
起動時に検証する。
設定で無効なあいだは欠測と絶対温度上限の対象にせず、
`disabled_inputs` に理由を残す。有効化後だけ Critical とする。
T_SENSOR の扱いは下の「T_SENSOR（決定記録 0110）」にまとめる。DS18B20 は一部欠測で
`telemetry_health=DEGRADED` になっても Safety state と demand を変えず、
State Estimator が `critical_unavailable` に `air_telemetry` を出す全滅時だけ
Front / Rear へ fault demand を適用する。Critical Safety の構築時に
CPU / GPU / optional T_SENSOR の Critical signal と、5本の air signal および
`air_telemetry` group が一致する `ControlInputContract` を必須にする。さらに snapshot schema、
signal の重複・欠落、`critical_unavailable` と signal availability、air 5本全滅 marker の整合を
Safety 自身が再検証し、矛盾した snapshot を NORMAL として受理しない。`quality=OK` でも単調時計
由来の age が contract の期限を超える、future である、または申告 age と一致しない signal は
拒否する。各 signal の stale limit も同じ `SafetyConfig.telemetry` の検証済み値との一致を要求し、
別経路の緩い閾値を使わせない。
Top の safety floor は 0028 §2.4 の
`max(最低安全 demand, cpu_cooling_floor(CPU 温度, CPU Power))` とし、温度の曲線
（`cpu_cooling_floor`）と Power の曲線（`cpu_power_cooling_floor`）をそれぞれ線形補間して
大きい方を取る。Power は決定記録 0032（FINAL）の `power.cpu.package` を、
`telemetry.cpu_power_ms` を stale limit とする DEGRADED signal として契約に必須にする。
Power が使えない（`ok` 以外）ときは 0029 §2.2 / §2.3 の Degraded として、fault にも
`fault_demand` にもせず、safety state も変えずに Power 項だけを外して温度の曲線で続け、
Top の理由に `cpu_power_unavailable` を残す。
現行機は単一 GPU のため、Critical な GPU freshness と絶対温度上限は
`gpu.0.core` / `gpu.0.hotspot` / `gpu.0.mem` に限定する。複数 GPU 対応は
Collector / State Estimator と同時に contract を更新してから有効にする。
制御対象 fan の RPM 自体が読めない場合は、stall と区別できないため
決定記録 0034 に従い、`stall_check_min_demand` 以上の間は同じ stall timer を
進める。Fan readback 全体が無い場合も最後の effective demand（最初の readback 前は
STARTUP Max command）を使って timer を継続する。`stall_window_ms` 後は tach stall と
同じ安全応答にする。
Hardware Backend が `external_faults` で報告する `TACH_STALL` も直接 latch しない。
0028 §2.7 / 0034 §2 の stall は「window の間続いた」ことを含む定義のため、
その tick の zone の tach が有効な応答を返していない証拠として同じ timer に渡す
（readback の RPM が閾値以上でも timer を reset しない）。`stall_check_min_demand`
未満では数えず、window 経過後に通常の tach stall と同じ応答にする。
同じ tick は Startup の tach 応答確認にも数えない（確認は一度付くと消えないため）。

## T_SENSOR（決定記録 0110）

2026-10-08 に所有者が有効化を決めた（driver `asusec`・label `T_Sensor`。#50 の較正は未了）。

- **上限は T_SENSOR 専用。** `safety.yaml` の `telemetry.t_sensor.absolute_ceiling_c`（本番 80 °C）だけで判定し、
  共通の `absolute_temp_ceiling_c` は T_SENSOR **以外**の温度（CPU / GPU / 空気など）だけに使う。
  欄は T_SENSOR が有効なら必須、無効なら指定不可（`stale_after_ms` と同じ）
- 超えたときの扱いは既存の絶対温度上限と同じ（`値 >= 上限` で `absolute_temperature_limit`・`EMERGENCY`・全 zone Max。
  解除は下の「合成と復帰」の規則）
- fault の `detail` は metric ごとに `metric=値C (ceiling 上限C)` を残す。trace の `faults` に毎 tick 保存される
- **ありえない値（0 °C 未満）は collector が判定する。** `config/internal-telemetry.yaml` の `minimum: 0` で
  `suspect` になり、State Estimator が使えない入力とし、Critical Safety は `t_sensor_stale`
  （Front / Rear を `fault_demand`、`DEGRADED`）にする。Critical Safety 自身は下限を持たない。上限側の妥当範囲は無い
- 許容遅延は `telemetry.t_sensor.stale_after_ms`（本番 5000 ms = 収集周期 2500 ms の2倍）
- 数え方（所有者の運用: 何度も超えるなら値を直す）: `GET /api/v1/control/traces` の `faults` で `code` が
  `absolute_temperature_limit` かつ `detail` に `board.connector_12v2x6` を含む tick を拾い、直前の tick に無かったものを1回と数える
- 本番での有効化の手順は `docs/control-config.md` の「T_SENSOR の有効化」

## 合成と復帰

effective demand は zone ごとに次の順で合成する。

```text
requested
  -> Guard ceiling (AUTO only)
  -> Guard floor
  -> Critical Safety floor
  -> ramp-down floor
  -> forced Max
```

floor は ceiling に勝ち、forced Max はすべてに勝つ。上げる速さは制限しない。
Manual / Calibration で Guard ceiling は使わないが、Guard floor と Safety は外れない。
安全側の状態遷移は即時、復帰は単調時計で `fault_clear_hold_ms` の間
連続して fault が解消した後だけ行う。
絶対温度上限の fault は、上限を超えた metric の fresh（品質 `ok`）な上限未満の値を観測するまで
解消に数えない。stale / missing / 消失の tick は温度が下がった証拠ではないため、fault を
観測し続け、`fault_clear_hold_ms` の hold もやり直しにする。

`DemandComposer` は検証済み `SafetyConfig.ramp_down_per_s` と直前の effective を内部に
保持し、呼び出し側から rate / previous / elapsed を受け取らない。最初の合成は
STARTUP または EMERGENCY の全 zone forced Max だけを許し、以後は
`CriticalSafetyDecision` 自身の `tick_id` / `monotonic_ms` の前進から elapsed を計算する。
呼び出し側から時刻を注入する引数はなく、同じ裁定の再利用も拒否する。stateless な合成関数は
public API に公開しないため、#74 の loop が previous を省略したり任意の rate・未来時刻で
ramp-down や最新 Safety 判定を迂回できない。直接構築・serialize round-trip・`model_copy` で
改変した Safety decision は発行時 payload と一致しないため合成を拒否する。composer は
最初の裁定を発行した `CriticalSafety` instance と SafetyConfig に束縛し、別設定・別 evaluator の
裁定を途中へ差し込めない。

合成結果は constructor を公開しない `ComposedDemands` capability として返し、Hardware Backend
はこの型だけを受理する。個別の `EffectiveZoneDemand` は decision trace の値 object として
構築できるが、それを直接 Backend へ渡して Safety / composer を省略することはできない。
command は Safety decision の tick / monotonic identity を保持する一回限りの値である。full
validated `ControlConfig`（source metadata を含む）から作る一意な runtime binding で Safety と
Backend を同じ session に束縛し、別設定および同じ設定の別 runtime が作った command を
consume/write 前に拒否する。Backend
も strictly increasing な identity だけを受理する。保持していた古い低 demand を Emergency Max
の後に replay して fan を下げることはできない。
Backend は takeover を確認するまで全 zone forced Max（STARTUP、設定不正時は EMERGENCY）の
command だけを受理し、STARTUP の command を捨てて NORMAL を渡すと consume 前に拒否する（0028 §2.7）。
Backend は全 zone の Max の書き込みと読み戻しが成功したときだけ runtime binding を通じて
takeover を確認する。失敗した zone は確認に数えず、失敗は fault として Safety へ渡るため、
Top は即 EMERGENCY、Front / Rear は `write_fail_emergency_after` 回の連続で EMERGENCY に
昇格し、STARTUP のまま黙って留まらない。Critical Safety は
この確認より前の裁定を常に STARTUP（全 zone Max）とし、確認後の最初の tick から
`startup_settle_ms` を数え、tach 応答もその次の tick 以降の snapshot だけで確認する。
STARTUP の command を遅らせても、その間に合成した command は Max のままなので、
Max を物理的に書いてから settle と tach 応答を経るまで demand を下げられない。

## deadman / 異常停止

deadman は制御プロセス外の systemd watchdog が担当する。異常停止後は
`coldaisle-safety-handoff` を `ExecStopPost` から起動する。この entry point は制御設定・
モデル・外部パッケージを読まず、固定の
`/run/coldaisle/fan-handoff.json` と `/sys/class/hwmon` だけを使う。

handoff record は schema version 2（v1 も読める）と Front / Rear / Top の3レコードを持つ。各レコードは
sysfs root からの `name_path` / `label_path` / `pwm_path` / `enable_path`、期待する
driver name / label、元の PWM / enable を持つ。`fan-hardware.yaml` で `label: null` の header は
`label_path` / `expected_label` が `null` になる（v2 だけ。決定記録 0118 §2.2）。実行部は次を検査する。

- record は symlink や FIFO を許さず、`O_NOFOLLOW` で1回だけ open した通常ファイルを
  上限サイズまで読む。欠損・不正形式・過大 record は構造化 failure と非0終了にする
- path は `hwmonN/<attribute>` 形式で、label と PWM・enable の channel 番号が一致する
- 3 zone が別の PWM / enable の組を指し、探し直しに使う組（driver・label・`pwmN`）も別である
- driver が**確かめ済みの一覧**（いまは `nct6799` だけ。`VERIFIED_HWMON_DRIVERS`）にある。無ければ
  `pwmN_enable=0` が Fan を止めうるので、その zone には何も書かず失敗とする（0118 §2.3a）

書き込み先の device は**書く時点で探し直す**（0118 §2.2）。record の `hwmonN` は監査のためだけに残し、
ドライバの再 bind で番号が変わっても Max を書ける。

- `label: null` の zone: `name` が record と一致する device がちょうど1つ
- label のある zone: `name` と、同じ番号の `fanN_label`（無ければ `pwmN_label`）が record と一致する
  device がちょうど1つ
- 0 個・2 個以上、または `name` / label を確かめられない device があれば、その zone には書かず失敗とする

探し直しは実機の class entry symlink だけを辿り、device directory FD から属性を `O_NOFOLLOW` で
先に開いて read/write FD の inode identity を検証・固定する。書く直前に、開いた FD で
driver `name` と label を読み直して照合する。

書ける値は `pwmN_enable=0`（hwmon ABI の「制御なし＝全速」）と `pwmN=255` だけで、値を引数・設定・
record から受け取らない（0118 §2.3）。

1. `pwmN_enable=0` を書く。`pwmN` を読み戻して 255 なら成功
2. 1 が拒否されたか 255 でなければ `pwmN_enable` を読む。**`1`（manual）のときだけ** `pwmN=255` を書き、
   `pwmN` が 255 かつ `pwmN_enable` が `0` か `1` なら成功（導入先の driver は manual の 255 を `0` と報告する）
3. `pwmN_enable` が 2 以上（自動）なら `pwmN` に書かず失敗とする。自動のまま 255 が読めても、
   実行部が終わった後に自動制御が下げうる。`pwmN_enable=1` は書かない

1 zone の失敗後も残り zone の Max を試み、全 zone の結果を構造化する。不一致・未確認の driver・
一意に特定できない header・I/O 失敗は phase 付きの zone別 JSON log と非0終了で通知する。
record が無いときは何もしない。

heartbeat を出す側は `src/coldaisle/control_daemon.py`（`coldaisle-fand`）にある
（#74 / 決定記録 0060 §2.7）。

- `NOTIFY_SOCKET` と、この process 宛に有効な `WATCHDOG_USEC` がそろったときだけ
  `SystemdWatchdog` を使い、起動時に `READY=1`、tick の書き込みと検証の直後に `WATCHDOG=1`
  を送る（`control/loop.py`）。送れる datagram は固定値だけ
- deadman の有無は `NOTIFY_SOCKET` の有無ではなく sd_watchdog_enabled(3) と同じ判定
  （`WATCHDOG_USEC` / `WATCHDOG_PID`）で決める
- `WATCHDOG_USEC` が heartbeat の最悪間隔（`tick_ms + tick_deadline_ms`）の2倍に満たなければ
  起動しない。`safety.yaml` の `watchdog_timeout_ms` より**長ければ起動しない**（終了コード 4。
  決定記録 0080 §2.3）。短いときは起動して warning を残す
- 通知の I/O の失敗（通知用 socket を作れない・`READY=1` を送れない）は終了コード 6 で、
  恒久的な食い違いの 4 とは分ける（再起動で再試行する。決定記録 0080 §2.4）
- takeover の後、ある zone の書き込みと読み戻しが一度も成功しないまま `safety.yaml` の
  `hardware_write_fail_exit_ms` 続いたら、**返却も引き継ぎ記録の削除もせずに**終了コード 7 で終える。
  `ExecStopPost` の引き継ぎ実行部が root で Max を書く（決定記録 0080 §2.6。
  `write_fail_emergency_after` の `EMERGENCY` の後ろの段。1回成功すれば数え直す）
- 起動時に `faulthandler` を有効にし、watchdog の SIGABRT で全スレッドの traceback を残す（0080 §2.5）
- 制御を取る前に DB へ書けるか（ディレクトリ・DB・`-wal` / `-shm` / `-journal` の `os.access` と
  `BEGIN IMMEDIATE` → `ROLLBACK`）を確かめ、権限で書けなければ `db_not_writable` の event
  （path・mode・所有者・gid・fand の uid と補助グループ）を出して終了コード 5 で終える。
  `SQLITE_BUSY` は lock を取れないだけとして別の文言にする（0080 §2.1）
- deadman が無い環境では `UnsupervisedWatchdog` が起動時に error を残し、heartbeat の間隔が
  `watchdog_timeout_ms` を超えるたびに記録する（プロセスを止める力は無い）。
  `--require-watchdog` を付けると、deadman が無いときは制御を取らずに終了する

まだ無いものは次のとおりで、これらが揃うまで deadman は完成しない。

- `coldaisle-fand` の systemd unit（`Type=notify` / `WatchdogSec` / `Restart` /
  `ExecStopPost` と `--require-watchdog`）。中身は決定記録 0060 未決7 のまま（#57 / #64）で、
  `deploy/systemd/` にも置いていない（決定記録 0069 §2.4）
- handoff record（`/run/coldaisle/fan-handoff.json`）の producer。実機の hwmon backend が
  まだ無い（#57 / #75）

これらを接続し、startup / restart / shutdown / kill / hang を実機検証することを、
本番サービスを有効にする統合 PR の merge 条件とする。それまでは #78 を完了扱いにせず、
サービスで Fan 制御を有効化しない。

`CriticalSafetyDecision.disabled_inputs` と `config_is_provisional` は decision trace と起動ログへ
永続化した（#78）。

- decision trace: `ControlTick` を schema version 9 とし、`safety_provenance`
  （`disabled_inputs` と `config_is_provisional`。どちらも必須）を毎 tick の裁定から写す。
  v9 はこの欄を省けず、v1〜v8 は欄を持てない。保存済みの v1〜v8 は欄なしのまま読め、
  欄が無い記録は「前提が記録されていない」であって「確定値で全入力を見ていた」ではない
- 起動ログ: `coldaisle-fand` の「制御設定を読み込んだ」の構造化 field に
  `safety_config_is_provisional` と `safety_disabled_inputs`（`code` / `detail` の配列）を出す。
  tick を1つも保存しないまま止まった起動でも追えるようにするため

`config_is_provisional=false` は「暫定値を1つも含まない Safety 設定で裁定した」ことだけを表す。
`config_invalid` の経路（`DemandComposer.for_invalid_config()`）は Safety 設定を読まず、
`ControlTick` も書かない。

`ControlTick` は #78 で schema version 4 とした（fault code `absolute_temperature_limit` と Top の
`enable_reverted` の無条件 `EMERGENCY`）。保存済みの v1〜v3 は当時の規則のまま読める。
上の `safety_provenance` は同じ #78 の v9 で追加した（v5〜v8 は #85 / #90 / #159 / #74）。
