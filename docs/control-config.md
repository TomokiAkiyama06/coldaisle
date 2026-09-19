# Control Config

#103 は Fan Control の設定を `fan-hardware.yaml`、`safety.yaml`、
`fan-policy.yaml` の3ファイルで一括検証する。

現時点では実機測定がないため、リポジトリに実運用用の3ファイルは置かない。
仮の hwmon 対応や温度閾値をコミットして実機を作動させないためである。
`ControlConfig.from_directory()` は3ファイルすべてが揃い、schema version・型・範囲・
相互条件を満たす場合にだけ設定を返す。欠損または不正なら、書き込み層へ設定を渡さない。
#78 はこの例外を Critical Safety の config-invalid failure semantics（書込み対象を特定できなければ
BIOS制御のまま終了、特定済みなら安全側へ引継ぎ）へ接続するconsumerである。

決定記録 0033（FINAL）により、`air-balance.yaml` を4つ目の Control Config として加え、
4ファイルを一括検証・一括採用する（0028 §2.8 の3ファイル境界を置き換える）。
4ファイルの読み込みはまだ実装していないため、それまで Air Balance Model は runtime に接続しない。

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
Fallback v2 の shape に Reactive Guard の閾値バンドを追加した
`fan-policy.yaml` の schema version 3 を土台とする。
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

さらに #87 のv4で `workload_regime` を追加する。CPU / GPU Power の metric 名、idle / active の
Schmitt trigger 閾値、平滑化・履歴窓、SUSTAINED / COOLDOWN / 遷移確認の各期間、許容 sample gap、
confidence が満値になる観測期間をすべて明示する。これらは実測前にコードへ埋め込まず、検証済み
config と checksum を Replay と decision trace へ渡す。Power が欠測・stale の間と、gap 後に
連続履歴が再び揃うまでは Workload Regime を `UNKNOWN` とする。
CPU / GPU metric は別々の既知の `W` signal に限定する。履歴窓は、観測期間に加えて
SUSTAINED または COOLDOWN と遷移確認期間を Replay できる長さを必須にする。

v3からv4へは `workload_regime` の全項目を実測根拠に基づいて追加し、最後に
`schema_version: 4` へ上げる。v1 / v2 / v3をv4として自動補完すると未確認の閾値を作るため、
旧versionと `workload_regime` を欠くv4は起動前に拒否する。v1→v2→v3の既存手順を飛ばさず、
各段階のshapeを揃えてからv4へ移行する。

Workload Regime は履歴の単調時刻だけで duration と hysteresis を評価する。壁時計 `Clock` は
推定結果の `computed_at_ms` を記録するためだけに使い、未来の残り実行時間は出力しない。

#88 のv5で `supervisor` に active / shadow policy、RulePolicy version、regime別context、
RL artifact version、出力許可範囲を追加する。各contextは strategy、GPU/CPU温度・Air Balance・
Acoustic・変更量のweight、CPU/GPU temperature target bandを持つ。RL出力も同じ許可strategy、
target band、weight範囲から外れた場合は採用しない。`UNKNOWN` にも専用contextを必須とし、
通常負荷へ暗黙変換しない。

v4からv5へは上記をすべて追加してから `schema_version: 5` へ上げる。v1〜v4は自動補完せず
起動前に拒否する。RLPolicyをactiveまたはshadowにする場合は期待する `rl_version` を必須とする。
active RLの停止・期限切れ・schema不一致時はRulePolicyへfallbackし、RulePolicyも失敗した場合は
Supervisor contextなしで既存Fallback Controllerを継続する。期限はworkerの壁時計でなく、
control loopがworkerへ渡した元snapshotのローカル単調時刻から `supervisor.valid_ms` で判定する
（決定記録 0041。受信時刻もtraceに残す）。

`gate_min_confidence` は `limited` / `expanded` / `full` ごとに持ち、高いauthority stageほど
低いconfidenceで動かせないよう `limited <= expanded <= full` を検証する。ML→Fallbackの
切替回数が `demote_window_ms` 内で `demote_after` に達した場合、Gateは降格推奨をtraceへ出す。
設定上のstageを`SHADOW`へ変更・永続化する責務は #92 に残す。

#85 のv6で `model_confidence` を追加する（決定記録 0050。FINAL）。HIGH の下限
`high_min_confidence`、MEDIUM で Learned MPC を Fallback 近傍へ閉じ込める `medium_limit`
（`limit_up` / `limit_down`。`limit_down` は最低 demand の制限を兼ねる）、OOD 判定の
`range_margin`・`min_support_count`・`full_support_count`・`min_missing_pattern_count`、
residual drift の `residual_window`・`residual_min_samples`・`residual_match_tolerance_ms`・
`residual_max_age_ms`・`residual_drift_ood_ratio`（`residual_window` / `residual_min_samples` の単位は
解決した予測（forecast）の件数で、出力の数ではない。window は照合できなかった forecast も枠に数え、
解決から `residual_max_age_ms` を過ぎた証拠は数えない。照合は品質 OK の観測だけを、期待時刻の前後
`residual_match_tolerance_ms` 以内の最も近いもので行い、同距離なら過去側）、uncertainty や residual の証拠が無い間の confidence 上限
`cap_without_uncertainty`・`cap_before_residual_evidence` をすべて `status` / `basis` 付きで明示する。
MEDIUM の下限は stage ごとの `gate_min_confidence` で、`high_min_confidence >= gate_min_confidence.full`
と、証拠が無い間に HIGH へ届かないよう `cap_before_residual_evidence < high_min_confidence`、
`residual_max_age_ms > residual_match_tolerance_ms` を検証する。`residual_match_tolerance_ms` は Profile の最短 horizon 未満でなければならず、horizon は
Profile 側にあるため residual monitor の生成時に検証する。MEDIUM 帯は stage の帯との共通部分を採り、confidence が authority を広げることはない。
`cap_without_uncertainty` にコード側の上限は置かない。uncertainty を出さないモデルを構造的に締め出すのではなく、
学習期間は保守的な暫定値に留め、評価（#90 / #91）で証拠が積み上がったら所有者が設定で広げる方針である
（決定記録 0050 §2.4 / §3）。学習期間の暫定値の目安は決定記録 0050 §2.5 に置き、`status: provisional` のまま運用する。
危険温度への対応は confidence に依存せず、Reactive Guard（#80）と Critical Safety（#78）が決定論的に行う。
Confidence / OOD が動かせるのはその前段の `requested` だけである。

v5からv6へは `model_confidence` を実データの評価根拠とともに追加してから `schema_version: 6` へ上げる。
v1〜v5は自動補完せず起動前に拒否する。

現行 Control Config v6 は設定の live reload を行わない。設定変更は候補全体を別オブジェクトで検証したうえで
**次回再起動時**にだけ反映する。これにより、変更後の設定も必ず `STARTUP` の Max を通る。
`trace_metadata()` は、採用されたsource名・schema version・SHA-256を #82 の decision traceへ渡す。
Confidence / OOD の判断（`model_gate`）には検証済み assessment の値だけを書き、裏付けの無い tick は
`attested: false` として confidence / ood も理由も残さない。`model_gate` の無い v5 の tick は
`ControlState` の ML 項目もすべて `null` にする。Gate が出さない組み合わせは schema が拒む
（決定記録 0050 §2.6）。

#78 で Safety Config に `stall_check_min_demand`・`write_fail_emergency_after`・
`cpu_power_cooling_floor`（`power_w` / `demand` の曲線）・`telemetry.cpu_power_ms` を
必須追加したため、`safety.yaml` は schema version 2 とする。
version 1 を version 2 の意味で読まず、起動時に明示的に拒否する。安全値に default を
補う migration は行わず、全項目を provisional / confirmed の根拠付きで設定してから再起動する。
`provisional_values()` は起動時の構造化ログへ、暫定値そのものを露出せずに位置と根拠だけを渡す。

数値の確定や実機での書き込み許可は決定記録 0028 §2.8–2.9 に従い、所有者の承認を要する。
