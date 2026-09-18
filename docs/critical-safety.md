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
zone floor、CPU cooling curve、stall の demand / RPM / window、連続書き込み失敗数、
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

T_SENSOR の metric 名は決定記録 0032（#65、Proposed）の
`board.connector_12v2x6` 提案に依存する。Critical Safety はこの名前を既定値にせず、
T_SENSOR を有効化するときに `approved_t_sensor_metric` として承認済み metric contract
の注入を必須にする。注入名は canonical metric 文法を満たし、既存の Critical / 絶対温度
入力名と衝突せず、同時に注入する検証済み Metric Catalog に単位 `C` で存在することも
起動時に検証する。
設定で無効なあいだは欠測と絶対温度上限の対象にせず、
`disabled_inputs` に理由を残す。有効化後だけ Critical とする。DS18B20 は一部欠測で
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

handoff record は schema version 1 と Front / Rear / Top の3レコードを持つ。各レコードは
sysfs root からの `name_path` / `label_path` / `pwm_path` / `enable_path`、期待する
driver name / label、元の PWM / enable を持つ。実行部は次を検査する。

- record は symlink や FIFO を許さず、`O_NOFOLLOW` で1回だけ open した通常ファイルを
  上限サイズまで読む。欠損・不正形式・過大 record は構造化 failure と非0終了にする
- path は `hwmonN/<attribute>` 形式で、実機の class entry symlink だけを辿る。device directory
  FD から4属性を `O_NOFOLLOW` で先に開き、read/write FD の inode identity を検証・固定する
- driver `name` と label が record と一致し、label と PWM の channel 番号も一致する
- 3 zone が別の PWM / enable の組を指す

一致した header には固定済み FD で `pwmN=255` を先に書き、その直後の PWM readback が
255 のときだけ `pwmN_enable=1` を書く。これにより PWM write が無視された場合に旧値を
manual 固定しない。enable 後にも PWM / enable を再確認する。1 zone の I/O 失敗後も残り
zone の Max を試み、全 zone の結果を構造化する。不一致・revert・I/O 失敗は phase 付きの
zone別 JSON log と非0終了で通知する。record が無いときは何もしない。

このリポジトリにはまだ `watchdog_timeout_ms` の consumer、systemd `WatchdogSec` / heartbeat、
`ExecStopPost` unit、handoff record producer が無い。この PR の standalone executor だけでは
deadman は完成しない。#74 / #57 でこれらを接続し、startup / restart / shutdown / kill / hang
を実機検証することを、本番サービスを有効にする統合 PR の merge 条件とする。この #78 PR の
merge だけでは #78 を完了扱いにせず、それまではサービスで Fan 制御を有効化しない。

同じ統合 PR では #82 の保存済み `ControlTick` v1 互換を壊さず schema migration を用意し、
`CriticalSafetyDecision.disabled_inputs` と `config_is_provisional` を decision trace と起動ログへ
永続化する。現状は Safety 裁定には両方が入るが `ControlTick` v1 には field が無いため、
この配線も本番有効化の blocker とする。
