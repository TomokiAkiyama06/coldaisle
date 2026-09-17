# Control Config

#103 は Fan Control の設定を `fan-hardware.yaml`、`safety.yaml`、
`fan-policy.yaml` の3ファイルで一括検証する。

現時点では実機測定がないため、リポジトリに実運用用の3ファイルは置かない。
仮の hwmon 対応や温度閾値をコミットして実機を作動させないためである。
`ControlConfig.from_directory()` は3ファイルすべてが揃い、schema version・型・範囲・
相互条件を満たす場合にだけ設定を返す。欠損または不正なら、書き込み層へ設定を渡さない。
#78 はこの例外を Critical Safety の config-invalid failure semantics（書込み対象を特定できなければ
BIOS制御のまま終了、特定済みなら安全側へ引継ぎ）へ接続するconsumerである。

`fan-hardware.yaml` の `approval.status` が `confirmed` かつ根拠 `basis` を持つまで、
`actuation_permitted` は false になる。実機の header 対応・Fan profile は #75 の測定記録を
根拠として確認する。`hwmonN`、絶対パス、個体識別子は設定に書かない。

`telemetry.t_sensor.enabled` は温度計モジュールの未設置を明示する。`false` のときは
T_SENSORのstale判定を持たず、`true` にするには `confirmed` と承認根拠、および許容遅延が必要である。
有効化も再起動時にだけ反映する。
`stall_check_min_demand` / `stall_min_rpm` / `stall_window_ms` と
`write_fail_emergency_after` も `safety.yaml` の承認対象とし、stallや連続書き込み失敗の
判定値をコードに埋め込まない。`stall_check_min_demand` は zone の最低安全 demand
以下でなければ設定検証で拒否し、通常の安全 floor で回っている fan も監視対象にする。
`fan-policy.yaml` は、MPCの `period_ms`・`budget_ms`・`valid_ms`、Supervisorの
`period_ms`・`valid_ms` を持つ。期限は制御デーモンが受信時刻から単調時計で判定する。
`authority_limits` には LIMITED / EXPANDED ごとの許可zone、Fallbackからの `limit_up` /
`limit_down` を必須とし、後続のGate実装が設定外の定数に依存しないようにする。

v1 は設定の live reload を行わない。設定変更は候補全体を別オブジェクトで検証したうえで
**次回再起動時**にだけ反映する。これにより、変更後の設定も必ず `STARTUP` の Max を通る。
`trace_metadata()` は、採用されたsource名・schema version・SHA-256を #82 の decision traceへ渡す。
`provisional_values()` は起動時の構造化ログへ、暫定値そのものを露出せずに位置と根拠だけを渡す。

数値の確定や実機での書き込み許可は決定記録 0028 §2.8–2.9 に従い、所有者の承認を要する。
