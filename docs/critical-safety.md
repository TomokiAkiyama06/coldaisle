# Critical Safety

#78 の Critical Safety は `coldaisle.control.safety.CriticalSafety` と
`compose_effective_demands()` に分かれる。前者は検証済み `SafetyConfig` と
1 tick の immutable snapshot から floor / forced Max / fault / safety state を決め、
後者は決定記録 0028 §2.4 の順序で effective demand を作る。モデル、
optimizer、Supervisor はいずれにも依存しない。

## 設定と未確定値

Safety の数値にコード上の既定値はない。`absolute_temp_ceiling_c`、
zone floor、CPU cooling curve、stall の demand / RPM / window、連続書き込み失敗数、
fault demand、復帰 hold、overrun 数、ramp-down はすべて `SafetyConfig` から受け取る。
`provisional` の値も保守側の制約として適用するが、判定の
`config_is_provisional` を true にし、確定値と混同しない。実運用値の設定ファイルは
#50 / #75 の実測と所有者の承認までリポジトリに置かない。
設定不正時の Max 裁定は `config_validated=false` と `config_invalid` fault を残し、
`config_is_provisional=false` を「確定済み」と読まないようにする。

T_SENSOR の metric 名は決定記録 0032（#65、Proposed）の
`board.connector_12v2x6` 提案に依存する。Critical Safety はこの名前を既定値にせず、
T_SENSOR を有効化するときに `approved_t_sensor_metric` として承認済み metric contract
の注入を必須にする。
設定で無効なあいだは欠測と絶対温度上限の対象にせず、
`disabled_inputs` に理由を残す。有効化後だけ Critical とする。DS18B20 は一部欠測で
`telemetry_health=DEGRADED` になっても Safety state と demand を変えず、
State Estimator が `critical_unavailable` に `air_telemetry` を出す全滅時だけ
Front / Rear へ fault demand を適用する。
現行機は単一 GPU のため、Critical な GPU freshness と絶対温度上限は
`gpu.0.core` / `gpu.0.hotspot` / `gpu.0.mem` に限定する。複数 GPU 対応は
Collector / State Estimator と同時に contract を更新してから有効にする。
制御対象 fan の RPM 自体が読めない場合は、stall と区別できないため
決定記録 0034 に従い、`stall_check_min_demand` 以上の間は同じ stall timer を
進める。`stall_window_ms` 後は tach stall と同じ安全応答にする。

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

## deadman / 異常停止

deadman は制御プロセス外の systemd watchdog が担当する。異常停止後は
`coldaisle-safety-handoff` を `ExecStopPost` から起動する。この entry point は制御設定・
モデル・外部パッケージを読まず、固定の
`/run/coldaisle/fan-handoff.json` と `/sys/class/hwmon` だけを使う。

handoff record は schema version 1 と Front / Rear / Top の3レコードを持つ。各レコードは
sysfs root からの `name_path` / `label_path` / `pwm_path` / `enable_path`、期待する
driver name / label、元の PWM / enable を持つ。実行部は次を検査する。

- path は `hwmonN/<attribute>` 形式で、class entry の symlink 解決後も4属性が
  同一 device 内にある
- driver `name` と label が record と一致する
- 3 zone が別の PWM / enable の組を指す

一致した header には `pwmN=255` を先に書き、その後 `pwmN_enable=1` だけを
書く。1 zone の I/O 失敗後も残り zone の Max を試み、全 zone の結果を構造化して
非0終了で失敗を通知する。record が無いときは何もしない。systemd への組み込み、
正常停止時の BIOS 制御への復帰、watchdog / kill / restart の実機検証は #74 と #57 で行う。
