# Control Config

Fan Control の設定は `fan-hardware.yaml`、`safety.yaml`、`fan-policy.yaml`、
`air-balance.yaml` の**4ファイル**で一括検証する（#103 の3ファイルに、#81 で
`air-balance.yaml` を加えた。決定記録 0033 / 0073）。**4ファイルとも必須**である。

現時点では実機測定がないため、リポジトリに実運用用の4ファイルは置かない。
仮の hwmon 対応や温度閾値をコミットして実機を作動させないためである。
`ControlConfig.from_directory()` は4ファイルすべてが揃い、schema version・型・範囲・
相互条件を満たす場合にだけ設定を返す。欠損または不正なら、書き込み層へ設定を渡さない。
#78 はこの例外を Critical Safety の config-invalid failure semantics（書込み対象を特定できなければ
BIOS制御のまま終了、特定済みなら安全側へ引継ぎ）へ接続するconsumerである。

決定記録 0033（FINAL）により、`air-balance.yaml` を4つ目の Control Config として加え、
4ファイルを一括検証・一括採用する（0028 §2.8 の3ファイル境界を置き換える）。
#81 で4ファイルの読み込みを実装した（決定記録 0073。下の「Control Config v11」）。

`fan-hardware.yaml` の `approval.status` が `confirmed` かつ根拠 `basis` を持つまで、
`actuation_permitted` は false になる。実機の header 対応・Fan profile は #75 の測定記録を
根拠として確認する。`hwmonN`、絶対パス、個体識別子は設定に書かない。

`telemetry.t_sensor.enabled` は温度計モジュールの未設置を明示する。`false` のときは
T_SENSORのstale判定を持たず、`true` にするには `confirmed` と承認根拠、許容遅延（`stale_after_ms`）、
および T_SENSOR 専用の絶対温度上限（`absolute_ceiling_c`。決定記録 0110）が必要である。
無効のときはどちらも指定しない。有効化も再起動時にだけ反映する。手順は下の「T_SENSOR の有効化」。
`stall_check_min_demand` / `stall_min_rpm` / `stall_window_ms` と
`write_fail_emergency_after` も `safety.yaml` の承認対象とし、stallや連続書き込み失敗の
判定値をコードに埋め込まない。`stall_check_min_demand` は zone の最低安全 demand
以下でなければ設定検証で拒否し、通常の安全 floor で回っている fan も監視対象にする。
`fan-policy.yaml` は、MPCの `period_ms`・`budget_ms`・`valid_ms`・`max_source_age_ms`（v11）、Supervisorの
`period_ms`・`valid_ms` を持つ。期限は制御デーモンが受信時刻から単調時計で判定する。
MPC は受信時刻からの `valid_ms` に加えて、元 snapshot の単調時刻からの `max_source_age_ms` でも判定する（v11）。
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

#86 のv7で `mpc.optimizer` を追加する（決定記録 0052。FINAL、2026-09-20 所有者承認）。
Learned MPC の `horizon_ms` / `step_ms`、探索の `candidate_levels` / `sweeps` / `max_evaluations`、
変化幅の `max_step_up` / `max_step_down`、zone ごとの探索範囲 `zone_bounds`、
目的関数の基準量 `cost_scales`、コストに使う予測 metric 名 `cost_metrics`、
Air Balance の比を推定できない step のコスト `unknown_balance_cost` をすべて
`status` / `basis` 付きで明示する。`horizon_ms` は `step_ms` の整数倍で、control step 数は
構造上限 32、1 tick の内部モデル評価は 4,096 回までとする（資源枯渇を防ぐ境界であり、調整値ではない）。
step 数の上限 32 は #84 の `MAX_TARGET_HORIZONS` と同じ値にする。内部モデルの target schema と
plan prediction が 32 horizon までしか表現できないため、設定だけがそれより多い step を許すと、
検証に通っても決して動かない組み合わせを作れてしまう。**設定の上限を予測の契約へ合わせる**
（逆に契約を広げない）。
`cost_metrics` の metric を内部モデルの target schema が覆うことと、control step のすべての
offset が target horizon にあることは、tick ごとではなく optimizer の**生成時**に照合する。
`mpc.valid_ms` は `mpc.period_ms` 以上にする（再計算の周期より短い有効期限では、
健全な提案でも毎 tick 期限切れになる）。`mpc.budget_ms` は anchor 推論と Confidence 判定を
含めた worker の計算時間に掛かり、各モデル評価の前と後に確認する。Air Balance の目標比は `air-balance.yaml`（決定記録 0033）が
所有し、`mpc.optimizer` へ写さない。値はすべて実測前の暫定値で、horizon / step / 重みの確定値は
実測と deadline の評価で決める（`docs/requirements.md` Q-22）。

v6からv7へは `mpc.optimizer` を追加してから `schema_version: 7` へ上げる。
v1〜v6は自動補完せず起動前に拒否する。

#90 のv8で `shadow` を追加する（決定記録 0053。FINAL、2026-09-20 所有者承認）。
counterfactual を decision trace へ残すかどうかの `enabled`、予測時刻と実測時刻のずれの
許容幅 `outcome_match_tolerance_ms`、予測した候補 action が「実際に掛かっていた」とみなす
zone ごとの demand の幅 `applied_demand_tolerance` を `status` / `basis` 付きで明示する。
後者は 1.0 未満にする（1.0 はどんな適用値も plan どおりにしてしまい、採点の可否の判定が
意味を失う）。掛かっていた action が plan と違う区間の実測は、差を取っても制御器の違いと
モデル誤差が混ざるだけなので、`unidentifiable` として誤差を出さない（0053 §2.3）。
時刻の許容幅は
`mpc.optimizer.step_ms` 未満でなければならない。1 step に届くと、別の候補 action の効果を
「その予測が当たった証拠」に数えてしまうためで、#85 の `residual_match_tolerance_ms` が
Profile の最短 horizon に対して満たす条件と同じである。`enabled` は記録の量だけを変え、
Gate の選択にも effective demand にも影響しない。

記録側の構造上限（1 tick の counterfactual、候補 plan と予測の step 数、1 step の metric 数、
metric 名の長さ）は**写し元の契約と同じ値**にする。設定として妥当な MPC が出した解を記録側だけが
拒むと、tick の途中で記録が失敗するためである。v7 で「設定の上限を予測の契約へ合わせる」と
決めた向きと同じで、一致は試験で突き合わせる。

v7からv8へは `shadow` を追加してから `schema_version: 8` へ上げる。
v1〜v7は自動補完せず起動前に拒否する。

#92 のv9で `authority_rollout` を追加する（決定記録 0057。FINAL、2026-09-20 所有者承認）。
昇格の承認の有効期限 `approval_max_age_ms`、rollout gate の証拠の有効期限
`evidence_max_age_ms`、自動降格のために不健全な tick を数える窓 `unhealthy_window_ms`、
その窓の中で降格に至る件数 `low_confidence_after` / `ood_after` を `status` / `basis` 付きで
明示する。`approval_max_age_ms` は `evidence_max_age_ms` 以下にする（承認のほうが長生きすると、
承認だけ取って書き込みを遅らせることで期限切れの証拠での昇格が通ってしまう）。

v9 から `authority_stage` の意味が変わる。**いまの stage ではなく、設定が許す上限である。**
実際に与えている制御権は `authority.json`（`AuthorityStore` の journal）が持ち、実効 stage は
journal・設定の上限・その process が自分で下げた上限の**もっとも低いもの**になる。
設定を下げれば再起動後に実効 stage が下がり、**設定を上げても journal は上がらない。**
stage を上げられるのは、revision・遷移・設定・証拠・artifact に束縛した人の承認だけである。
Model を Production へ昇格させても authority は動かない（#104 と #92 の境界。0057 §2.3）。

v8からv9へは `authority_rollout` を追加してから `schema_version: 9` へ上げる。
v1〜v8は自動補完せず起動前に拒否する。

## Control Config v15 と `fan-hardware.yaml` v2（#74 / 決定記録 0118 §2.1）

束ねた版 `CONTROL_CONFIG_VERSION` を 14 → 15 に上げ、`fan-hardware.yaml` を schema version 1 → 2 にした
（ほかのファイルの版は変えない。`safety.yaml` 4、`fan-policy.yaml` 11、`air-balance.yaml` 2）。
導入先の `nct6799` は Fan の `fanN_label` / `pwmN_label` を持たないため、v1 の「label は必須の文字列」では
header を書けなかった。

```yaml
schema_version: 2
zones:
  front:
    driver: nct6799
    label: null              # 省略は不可。null は「この driver は Fan の label を持たない」という宣言
    pwm_attribute: pwm6
    tach_attribute: fan6_input   # 番号は pwm_attribute と同じに限る
    enable_attribute: pwm6_enable
    profile: ...
```

- `label` は**必須のまま**、値に `null` を許す。省略は拒否する（書き忘れを黙って label 無しの特定に落とさない）。
  文字列のときの規則（文字の種類・`hwmon` を含めない）は v1 と同じ
- `label: null` の header は、hwmon の `name` が `driver` に一致する device が**ちょうど1つ**のときだけ特定できたとする。
  0 個・2 個以上なら header を一意に特定できないとして制御を取らない（0028 §2.7）。この照合は実機 backend が行う
- `tach_attribute` の番号を `pwm_attribute` と同じに限る（`pwm2` なら `fan2_input`）。番号が違うと、label で確かめた
  Fan とは別の Fan の回転数を読み、止まった Fan を回っている別の Fan が隠しうる
- 3 zone の重複の検査（`driver`・`label`・`pwm_attribute`）では、`null` を1つの値として同じに扱う。
  `label: null` の zone 同士は `driver` と `pwm_attribute` で区別される
- `fan-hardware.yaml` の不正なので、v1 のままのファイル・`label` を欠くファイル・番号の揃わないファイルは
  **制御を取らない**（BIOS の制御のまま終了コード 2。`config_invalid` の全 zone Max ではない）
- trace の `runtime.control_config_version` と `ControlConfigDigest` の値が 15 になる。`ControlTick` /
  `ControlTickRuntime` の形は変えない。保存済みの trace（v14 以前の版を名乗るもの）はそのまま読める。
  Offline Evaluation・昇格の証拠の突き合わせは、これまでどおり各ファイルの SHA-256 で行う
  （`fan-hardware.yaml` を書き直せば hash が変わるので、書き直す前の trace と報告は今の設定と一致しない）

**移行手順**: v1 を v2 として補完しない（読み込み時に拒否する）。いまは導入先に運用の `fan-hardware.yaml` は無い。

1. 運用の `fan-hardware.yaml` の各 zone に `label` を明示する。Fan の label を持たない driver は `label: null`、
   持つ driver は v1 の値のまま（書き換えは要らない）
2. 各 zone の `tach_attribute` の番号を `pwm_attribute` と揃える
3. 最後に `schema_version: 2` へ上げる。v1 のままのファイルも、欄を欠く v2 も拒否され、制御を取らずに終わる
4. 新しいコードへ更新して再起動する

引き継ぎの記録（`/run/coldaisle/fan-handoff.json`）も schema version 2 になり、`label: null` の header では
`label_path` / `expected_label` が `null` になる。引き継ぎ実行部は v1 の記録も読める
（`docs/critical-safety.md`「deadman / 異常停止」）。

## Control Config v14 と `fan-policy.yaml` v11（#86 / 決定記録 0077 §2.4 の4）

束ねた版 `CONTROL_CONFIG_VERSION` を 13 → 14 に上げ、`fan-policy.yaml` を schema version 10 → 11 にした
（ほかのファイルの版は変えない。`fan-hardware.yaml` 1、`safety.yaml` 4、`air-balance.yaml` 2）。
v11 は `mpc.max_source_age_ms`（`{value, status, basis}`）を**必須**にする。

```yaml
mpc:
  period_ms: 10000
  budget_ms: 2000
  valid_ms: 20000
  max_source_age_ms: {value: <下限〜valid_ms>, status: provisional}   # 実測前は provisional
```

- Learned MPC の提案は、**受信してから `mpc.valid_ms`**（0028 §2.6。変えない）に加えて、**元 snapshot の
  単調時刻から `mpc.max_source_age_ms`** を超えても期限切れ（`learned_proposal_expired`、detail に
  `source_age_ms` と `max_source_age_ms`）になる。worker の中で長く滞留した提案が、受信した瞬間に新しく
  見えることを塞ぐ。元 snapshot の時刻は `coldaisle-fand` が自分の出した snapshot の記録から取り、worker の名乗りは使わない
- 読み込み時に2つの不変条件を確かめ、満たさなければ Control Config の不正（`config_invalid` の全 zone Max）にする
  - 上限: `mpc.max_source_age_ms <= mpc.valid_ms`
  - 下限: `mpc.max_source_age_ms >= 2 * safety.tick_ms + mpc.period_ms + mpc.budget_ms`（`safety.yaml` をまたぐ）。
    往き（frame が worker へ届くまでの最大1 tick・worker が最後の frame を拾うまでの最大1周期・optimizer の予算）と
    還り（結果が次の tick の poll まで待つ最大1 tick）を足した値より短いと、健全な worker の提案も期限切れになり、
    Learned が黙って一度も使われない構成を許してしまう
- `provisional` の間は起動時の一覧（`provisional_values()`）に `mpc.max_source_age_ms` として出る。値は本番機での
  推論・optimizer の所要時間の実測の後に決める（0077 §5「実測を待つ値」）

**移行手順**: v10 を v11 として補完しない（読み込み時に拒否する）。

1. 運用の `fan-policy.yaml` の `mpc` に `max_source_age_ms` を足す（下限と上限の間の値。上の例の暫定値なら
   `safety.tick_ms` 1000 で下限は 14000、上限は 20000）
2. 最後に `schema_version: 11` へ上げる。欄を欠く v11 と v10 のままのファイルはどちらも拒否され、
   `config_invalid` の全 zone Max で止まる
3. 新しいコードへ更新して再起動する

## Control Config v13 と `fan-policy.yaml` v10（#81 / 決定記録 0078 §2.4）

束ねた版 `CONTROL_CONFIG_VERSION` を 12 → 13 に上げ、`fan-policy.yaml` を schema version 9 → 10 にした
（ほかのファイルの版は変えない。`fan-hardware.yaml` 1、`safety.yaml` 4、`air-balance.yaml` 2）。
v10 は Air Balance の協調を Baseline（Fallback）の requested に掛けるかの方針
`air_balance_coordination` を**必須**にする。

```yaml
air_balance_coordination:
  mode: "off"                # off | shadow | apply。雛形の既定は off
  max_raise:                 # zone ごとに raw baseline から上げてよい幅（demand、0.0..1.0）。0 は動かさない
    front: {value: <#75 の後>, status: provisional}
    rear: {value: <#75 の後>, status: provisional}
    top: {value: 0.0, status: provisional}        # 最初の apply では 0（0078 所有者の判断 9）
  release_hold_ms: {value: <#75 の RPM 応答時間の後>, status: provisional}   # 0 以上の整数
```

- `mode` は Air Balance そのものの有効・無効ではない（それは `air-balance.yaml` の `source.status`。0073 §2.2）。
  「有効な Air Balance の協調提案を Baseline に適用するか」で、`off` → `shadow` → `apply` を**人が設定と再起動で**進める
  （0078 §2.8。authority journal を経ない）
- YAML 1.1 では引用符の無い `off` が真偽値の偽になる。偽だけを `off` と読み、真（`on` / `yes` / `true`）は
  `shadow` か `apply` か決められないので拒否する。迷わないよう `"off"` と引用符で書く
- `max_raise` / `release_hold_ms` は `{value, status, basis}` で、値は #75 の実測と shadow の集計の後に決める（0078 §5）。
  `provisional` の間は起動時の一覧（`provisional_values()`）に出る
- **`mode: shadow` / `apply` で `air-balance.yaml` が `uncalibrated` なら Control Config の不正**として扱い、
  全 zone Max（`config_invalid`）で止まる（黙って `off` と読まない。0078 §2.4）。校正済みのまま協調だけを止めるときは `mode: "off"`

**協調は loop へ配線済み**（0078 §2.11 の段 3 / 決定記録 0085）。`AirBalanceCoordinator`
（`control/air_balance_coordination.py`。上げるだけ・zone ごとの上限・下げる前の保持）を
Fallback の後・Critical Safety の評価の後・Gate の前に置き、毎 tick を `ControlTick` v14 の
`air_balance_coordination` に記録する。

- `mode: "off"`（雛形の既定）: 協調の部品を作らない。Fan の挙動は v9 のときと同じで、trace には `status: off` だけが残る
- `mode: "shadow"`: Fan の挙動は `off` と同じ。毎 tick `proposed` と保持込みの `counterfactual_output` を記録する
- `mode: "apply"`: Baseline の requested が `max_raise` まで上がりうる（下がらない）。協調の失敗は
  `fallback_exception`（次 tick は全 zone Max）で、その tick は Gate を迂回して raw baseline を使う

**移行手順**: v9 を v10 として補完しない（読み込み時に拒否する）。

1. 運用の `fan-policy.yaml` に `air_balance_coordination` を足す（`mode: "off"`、`max_raise` と
   `release_hold_ms` は `provisional` の仮の値）
2. 最後に `schema_version: 10` へ上げる。塊を欠く v10 と v9 のままのファイルはどちらも拒否され、
   `config_invalid` の全 zone Max で止まる
3. 新しいコードへ更新して再起動する

## T_SENSOR の有効化（決定記録 0110。`safety.yaml` は v4 のまま）

`telemetry.t_sensor` に `absolute_ceiling_c`（`{value, status, basis}`）を足した。T_SENSOR はこの上限**だけ**で
判定し、最上位の `absolute_temp_ceiling_c` は T_SENSOR 以外の温度に使う。**有効なら必須、無効なら指定不可。**
`safety.yaml` の schema version は上げない（4 のまま）。版 4 で T_SENSOR を有効にしたファイルは欄が無いので拒否され
（`config_invalid` の全 zone Max）、旧い意味（共通の上限で T_SENSOR も判定する）で黙って読まれない。無効のファイルは
意味が変わらないので、そのまま使える（0110 §2.6）。

本番の値（所有者の承認。2026-10-08）:

```yaml
telemetry:
  # ほかの欄（cpu_ms / cpu_power_ms / gpu_ms / air_ms / air_sensor_period_ms）は変えない
  t_sensor:
    enabled: {value: true, status: confirmed, basis: "決定記録 0110。2026-10-08 所有者が承認"}
    stale_after_ms: {value: 5000, status: confirmed, basis: "決定記録 0110 §2.5（収集周期 2500 ms の2倍）"}
    absolute_ceiling_c: {value: 80.0, status: confirmed, basis: "決定記録 0110 §2.2"}
```

**手順**:

1. `config/internal-telemetry.yaml` の `board.connector_12v2x6`（driver `asusec`・label `T_Sensor`・`minimum: 0`）で
   `coldaisle-telemetry` を再起動し、値が `ok` で記録されることを確かめる（0 °C 未満は `suspect`）
2. 運用の `safety.yaml` の `telemetry.t_sensor` を上のとおりにする
3. `coldaisle-fand` に `--t-sensor-metric board.connector_12v2x6` を付けて再起動する。`safety.yaml` で有効なのに
   引数が無い、または無効なのに引数がある組み合わせは、起動時に拒否される
4. 起動ログの `safety_disabled_inputs` から `t_sensor_disabled` が消えたことを確かめる

## Control Config v12 と `safety.yaml` v4（#74 / 決定記録 0080 §2.6）

束ねた版 `CONTROL_CONFIG_VERSION` を 11 → 12 に上げ、`safety.yaml` を schema version 3 → 4 にした
（ほかのファイルの版は変えない。`fan-hardware.yaml` 1、`fan-policy.yaml` 9、`air-balance.yaml` 2）。
v4 は最上位の `watchdog_timeout_ms` の隣に `hardware_write_fail_exit_ms`（ほかの Safety の値と
同じ `{value, status, basis}`）を必須にする。takeover の後、ある zone の書き込みと読み戻しが
一度も成功しないままこの時間続いたら、`coldaisle-fand` は引き継ぎ記録を消さずに終了コード 7 で
終わり、`ExecStopPost` が root で Max を書く。

読み込み時に次の不変条件を検証し、満たさなければ Control Config の不正として扱う。

```text
(tick_ms + tick_deadline_ms) * write_fail_emergency_after <= hardware_write_fail_exit_ms <= watchdog_timeout_ms
```

下限は fand の中の再試行と `EMERGENCY` への昇格を先に試すため、上限は「書けないまま制御を
持ち続ける」時間を hang の deadman より長くしないためである。

**暫定値**は `value` = その `safety.yaml` の `watchdog_timeout_ms` と同じ値、`status: provisional`、
`basis` なし（決定記録 0080 §2.6）。新しい数を作らず承認待ちの既存の値に揃えたもので、**実機で使う前に
#50 の熱の時定数と #75 の書き込み・読み戻しの失敗の実測から導き、所有者の承認（0028 §2.9 の承認点 2）を
経て `confirmed` にする。** 承認まではほかの `provisional` の値と同じく `provisional_values()` で起動時の
一覧に出る。

**移行手順**: v3 を v4 として補完しない（読み込み時に拒否する）。

1. 運用の `safety.yaml` に `hardware_write_fail_exit_ms` を足す（暫定値なら
   `{value: <watchdog_timeout_ms と同じ値>, status: provisional}`）
2. 最後に `schema_version: 4` へ上げる。欄を欠く v4 と v3 のままのファイルはどちらも拒否され、
   `config_invalid` の全 zone Max で止まる
3. 新しいコードへ更新して再起動する

## Control Config v11 と `air-balance.yaml` v2（#81 / 決定記録 0073）

束ねた版 `CONTROL_CONFIG_VERSION` を 10 → 11 に上げ、`air-balance.yaml` を4つ目のファイルにした。
各ファイルの版は変えない（`fan-hardware.yaml` 1、`safety.yaml` 3、`fan-policy.yaml` 9）。
`air-balance.yaml` 自身は schema version 2 で、熱の指標をどの snapshot metric から取るかを
`thermal_inputs`（`gpu_intake_c` / `case_delta_c` / `cpu_package_c` / `gpu_temperature_c`。
使わない指標は `null`）に置く。各 metric は Metric Catalog にあり単位が `C` であること、
派生値（`d.*`）は材料の metric へ展開できること、材料の許容遅延を決められることを起動時に
検証し、満たさなければ Control Config の不正として扱う。束縛した metric は入力契約へ
`ADVISORY` として加わる（既に契約にある metric はその重要度のまま）。v1 は起動前に拒否し、
自動補完しない。

| `air-balance.yaml` | `coldaisle-fand` |
|---|---|
| 無い | Control Config の不正。**全 zone Max（`config_invalid`）** |
| 形が不正（schema 違反・v1・曲線の単調性違反・`thermal_inputs` の検証失敗） | 同上 |
| `source.status: uncalibrated` | 検証を通し、**Air Balance を無効**にして起動する（Safety / Guard は変わらない） |
| `source.status: calibrated` | 検証を通し、Air Balance を有効にする |

**移行手順**（決定記録 0073 §2.1）:

1. 運用の設定ディレクトリに `air-balance.yaml`（v2）を置く。#75 の前なら
   `source.status: uncalibrated` のもの。雛形は `tests/fixtures/air_balance_uncalibrated.yaml`
2. 新しいコードへ更新して再起動する

リポジトリの `config/` には、ほかの3ファイルと同じく実運用の `air-balance.yaml` を置かない。
置き忘れると全 zone Max で止まる（大きな音と `config_invalid` のログで気付ける側に倒す）。

`ControlTick` は v11 になり、runtime（`ControlTickRuntime` v2）に4ファイルの schema version と
SHA-256 を毎 tick 残す。Offline Evaluation の報告は v3 になり、`air-balance.yaml` の hash と、
消費した trace の `air-balance.yaml` / `fan-hardware.yaml` の hash の突き合わせを provenance に持つ。
昇格（`AuthorityJournal` v2）は、この2ファイルについても承認の証拠・報告・いまの設定の一致を求める
（`docs/authority-rollout.md`）。

現行の Control Config（v15）は設定の live reload を行わない。設定変更は候補全体を別オブジェクトで検証したうえで
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
