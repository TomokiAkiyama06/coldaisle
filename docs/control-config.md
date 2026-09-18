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
`fan-policy.yaml` は、MPCの `period_ms`・`budget_ms`・`valid_ms`、Supervisorの
`period_ms`・`valid_ms` を持つ。期限は制御デーモンが受信時刻から単調時計で判定する。
Fallback v2 の shape に Reactive Guard の閾値バンドを追加した
`fan-policy.yaml` の schema version は 3 とする。
Reactive Guard の `floor` / `hold_ms` と、温度・Power・吸気温度差の各 trigger は
通常時と Degraded 時の `activate_above` / `clear_at_or_below` を持つ。これらは
全て `status` / `basis` の追跡対象で、実測前は `provisional` のまま扱う。
CPU Power trigger の metric 名は、PR #128 で提案中の決定記録 0032
（内部Telemetryのメトリクス名。未マージ）に依存するため、
`cpu_power_metric` は未承認時は `null` にして trigger を無効にする。
その決定記録の確定後も、`confirmed` と承認根拠 `basis` が無ければ設定検証で拒否する。
`power` ドメイン（決定記録 0002 §2.1）以外の metric も拒否し、Reactive Guard の生成時に
Metric Catalog 上の単位が `W` であることを検証する。
`authority_limits` には LIMITED / EXPANDED ごとの許可zone、Fallbackからの `limit_up` /
`limit_down` を必須とし、後続のGate実装が設定外の定数に依存しないようにする。

#79 で `fan-policy.yaml` の schema version は 2 になった。Fallback の温度入力は
`fallback_temperature_inputs`、任意の Power feed-forward は
`fallback_power_feedforward`、需要を下げる前の hysteresis / hold は
`fallback_dynamics` に置く。温度・Powerのcurveを含め、実機で未確認の値をコード側の
既定値で補わない。旧versionを新しい意味で黙って解釈せず、version 1 は読み込み時に拒否する。
Power signalが未設定またはstale / missingならfeed-forward項だけを外し、温度feedbackを続ける。
Fallback Controllerの生成時に、temperature入力がMetric Catalog上の`C`、Power入力が`W`で
あることも検証し、名前が正しくても単位が異なるpolicyは起動前に拒否する。

#80 のv3への移行は自動補完しない。v2の Fallback フィールドはそのまま残し、
`reactive_guard` の旧 `ceiling` / `intake_rise_threshold_c` /
`gpu_hotspot_threshold_c` を削除して、六つの trigger それぞれに通常・Degradedの
発火/解除閾値を明示する。`cpu_power_metric` は承認まで `null` とし、
最後に `schema_version: 3` へ上げる。v2のまま、または新旧shapeが混ざった設定は拒否する。

`gate_min_confidence` は `limited` / `expanded` / `full` ごとに持ち、高いauthority stageほど
低いconfidenceで動かせないよう `limited <= expanded <= full` を検証する。ML→Fallbackの
切替回数が `demote_window_ms` 内で `demote_after` に達した場合、Gateは降格推奨をtraceへ出す。
設定上のstageを`SHADOW`へ変更・永続化する責務は #92 に残す。

v1 は設定の live reload を行わない。設定変更は候補全体を別オブジェクトで検証したうえで
**次回再起動時**にだけ反映する。これにより、変更後の設定も必ず `STARTUP` の Max を通る。
`trace_metadata()` は、採用されたsource名・schema version・SHA-256を #82 の decision traceへ渡す。
`provisional_values()` は起動時の構造化ログへ、暫定値そのものを露出せずに位置と根拠だけを渡す。

数値の確定や実機での書き込み許可は決定記録 0028 §2.8–2.9 に従い、所有者の承認を要する。
