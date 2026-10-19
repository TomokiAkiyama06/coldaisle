# 決定記録 0153: 3 系統 Fan の回転数の掃引と応答の行列（風量の測定の第 1 段・測定中の安全・結果の置き場所）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-19、リポジトリ所有者の承認。§5 の 1 点は推奨の案）
- **Date**: 2026-10-16（2026-10-19 に範囲を絞って書き直し。§1）
- **Supersedes**: なし
- **関連**: [`0026`](0026-three-zone-fan-control.md) /
  [`0028`](0028-fan-control-contracts.md) §2.4 / §2.5 (a) /
  [`0033`](0033-air-balance-config-boundary.md) /
  [`0072`](0072-control-admin-entry.md) /
  [`0073`](0073-air-balance-control-config-integration.md) §2.3 / §5 /
  [`0096`](0096-calibration-binding-digest.md) / [`0099`](0099-calibration-change-log.md) /
  [`0109`](0109-dataset-declared-changes-cli.md) /
  [`0119`](0119-safety-values-first-host.md) §2.2 / §2.14 /
  [`0120`](0120-fan-hardware-first-host.md) §2.2 / §5 /
  [`0121`](0121-hardware-test-procedure.md) §2.1 / §2.6 / §2.9 /
  [`0123`](0123-cpu-power-floor-tests.md) /
  [`0125`](0125-stall-rise-criterion-tach-update.md) /
  [`0126`](0126-startup-settle-front-spinup.md) /
  [`0128`](0128-gpu-fan-curve-service.md) / [`0130`](0130-gpu-fan-quieter-curve.md) /
  `docs/airflow-model.md` / `docs/airflow-sources.md` / `docs/air-balance-model.md` /
  `docs/fan-header-mapping.md` / `docs/spec-review.md` I-01・C-05 / AGENTS.md ルール 1〜3・8・9・10
- **対象 Issue**: #75（関連 #81 / #77 / #83）

---

## 1. Context

#75 は、Front / Rear / Top の風量を共通の尺度で比べられるようにし、#81 の Air Balance Model と #83 の Thermal Dataset へ
渡すことを求める。本記録の最初の案は、回転数の掃引・熱収支による実効風量（EFU）・零位法・プローブの補正・GPU の Fan の duty の影響・
zone の間の結合までを 1 つの記録で決め、`air-balance.yaml` を `calibrated` にする条件まで書いていた。レビュー（PR #341 の Codex）の
指摘に 28 回対応した結果、測定は約 170 時間を超え、未決は 8 点に増え、それでも `calibrated` に届く手順にならず、指摘も収束しなかった。

**所有者の決定（2026-10-19）**: 本記録の範囲を、**いますぐ始められる測定**（回転数の掃引と応答の行列）に絞る。校正（風量の値・
`calibrated` にする条件）は本記録から外し、別の決定記録で決める（§2.12）。外した部分への指摘は §2.13 に既知の限界として残す。

いまの状態:

- `fan-hardware.yaml` の Fan profile は 0120 で承認済み。`pwm_to_rpm` の 0.40 以上は BIOS の運転記録の中央値（BIOS が PWM を
  動かしている途中の点を含む）、0.25 は 9月の概数の 1 点。`airflow_index` は「RPM ÷ demand 1.0 の RPM」（風量は回転数に
  おおむね比例するという近似）
- `air-balance.yaml` は `tests/fixtures/` の `uncalibrated` の例だけで、導入先では Air Balance は無効（0073 §2.3）
- 原則（`docs/airflow-model.md`・#75・#81）: ファン径 × RPM で絶対 CFM を計算しない。公称 CFM を装着時の風量とみなさない。
  `airflow_index` は zone の中だけの比で、zone をまたいで比べない
- 測れるもの: 空気の温度 `air.front_intake` / `air.gpu_intake` / `air.gpu_exhaust` / `air.top_exhaust` / `air.rear_exhaust` / `air.room`
  （DS18B20・11 bit・2.5 s 周期。位置は spec-review I-01 / C-05）、`air.room_humidity`、`cpu.package`・`gpu.0.core` / `hotspot`、
  `board.connector_12v2x6`、`power.gpu.0`（NVML）、`power.cpu.package`（RAPL。0122）、`fan.<zone>.rpm` / `pwm`、`gpu.0.fan_speed`、
  `fan.vrm.rpm`・`fan.aio_pump.rpm`、fand の decision trace（tick ごとの requested / effective / `bound_by` / `hardware.rpm` /
  `hardware.write_ok` / `readback_ok` / `applied_demand`）
- 制御の経路の制約（0121 で実機で確かめた）: manual でも最低安全 Demand 0.35・Guard の floor・Critical Safety の floor・
  `ramp_down_per_s` 0.05/s は掛かる。Top には `cpu_cooling_floor`（60 °C → 0.35・80 °C → 1.0）と `cpu_power_cooling_floor`
  （100 W → 0.35・200 W → 1.0。0123）が掛かる。`fanN_input` は約 1.5 s より短い間隔では変わらない（0125）。Front は 0.35 から
  全開まで最長約 12.8 s かかる（0126）

## 2. Decision

### 2.1 範囲

| 本記録で決める | 本記録で決めない（§2.12 の後続の記録） |
|---|---|
| §2.4 回転数の掃引 → `fan-hardware.yaml` の `pwm_to_rpm` と `maximum_rpm` の候補 | 風量の値（EFU・`effective_flow`）とその測り方（熱収支・零位法・風速計） |
| §2.5 応答の行列（各 zone を動かしたときの各温度の変化） | `airflow_index` を測った相対の風量にすること |
| §2.2 測定中の安全・§2.6〜§2.9 結果の置き場所・解析の設定・manifest | `air-balance.yaml` を `calibrated` にする条件 |

- **`airflow_index` は代用のまま**: 0120 と同じ定義（`pwm_to_rpm` の各点の RPM ÷ demand 1.0 の RPM）で、§2.4 の新しい表から
  計算し直す（`FanProfile` は 2 つの列の点の数をそろえることを求める）。`basis` に「RPM の比による代用。風量を測った値ではない
  （0153）」と書く。報告・画面で「測った風量」と書かない
- **`air-balance.yaml` は `uncalibrated` のまま**。本記録の結果だけでは `calibrated` にしない（Air Balance は無効のまま。0073 §2.3）
- **応答の行列は風量ではない**。各温度の直接の変化で、EFU への換算を通らない。#75 の「どの zone を上げるとどの温度がどれだけ下がるか」の
  説明と、#81 の `balance` / `thermal_limits` を決める記録の材料にする。zone の間の風量の比の根拠にはしない
- RPM の読み戻しから風量を推定する方式（0073 §5）には切り替えない

### 2.2 共通の決まり（測定中の安全）

- **制御の経路を迂回しない。** Demand は `coldaisle-control manual`（管理ソケット。人の uid）で決め、Guard と Critical Safety を通す。
  sysfs・hwmon へ試験の側から書かない。本番の設定（`/etc/coldaisle/control-config/`）のまま測り、**試験専用の設定は使わない**
  （floor を外す設定を使わない）。0.35 未満は測らない（§2.10）
- fand は 0121 と同じく `systemctl start`（`enable` しない）で動かす。0121 T1 の soak と同時には行わない
- **lease**: 各段の `manual` は、その段の最長の時間 + 5 分の `--lease` で段ごとに送り直す（`manual.max_lease_s` の 4 時間を使わない）。
  所有者が離れて送り直しが止まれば lease が切れて `AUTO` に戻る
- **CALIBRATION モードは使わない。** MANUAL で足りる。新しい入口を足さない
- **floor が掛かった段は捨てる。** 段の値を取る窓の中で、3 zone のどれかで trace の `effective` が `manual` の値と違う tick
  （`bound_by` が floor / `forced_max` など）が 1 つでもあれば、その段を解析から外し、manifest に理由を残す。floor を下げて測り直さない
- **中止の基準**は 0121 §2.1（0130 で置き換えた後）と同じ: `cpu.package` 88 °C・`gpu.0.core` 85 °C・`board.connector_12v2x6` 70 °C。
  どれかに届いたら、負荷を止めて `coldaisle-control max`。Critical Safety が `DEGRADED` / `EMERGENCY` になった・fault が出た段は
  使わず、負荷を止めて原因を見る。fand の挙動がおかしければ `systemctl stop coldaisle-fand`（BIOS へ返す）
- **負荷**（§2.5 だけ）: GPU は電力の上限を 300 W にして行列計算で上限に張りつかせる（終わったら 600 W に戻す。負荷のスクリプトは
  SIGTERM で同期してから終わる。0121 T4）。CPU は軽い固定の負荷（`stress-ng` の worker の数）で、T− の段（Top 0.35）でも
  `cpu_cooling_floor` と `cpu_power_cooling_floor` が 0.35 を超えない重さを本番の前の予備の試行で選び、manifest に残す
- **GPU の Fan は 0130 の曲線のまま**動かし、`gpu.0.fan_speed` を記録する（固定の duty にしない。§2.5）
- **所有者が在宅で**、各セッションの開始・負荷の切り替え・終了を行う。操作のスクリプトは事前に内容を示し、所有者が実行する（0121 §2.1）。
  スクリプトがするのは `coldaisle-control manual` / `auto` / `max` と負荷の開始・停止と manifest の追記だけ
- 測定の期間中は較正を変えない（`coldaisle-calibrate --apply` をしない。§2.9）
- 試験の側から `fanN_input` を高い頻度で読まない（0125）。回転数は fand の trace の `hardware.rpm` を使う
- **LLM は関わらない。** 手順の実行・解析・承認のどこにも LLM を使わない。解析の結果を LLM のプロンプトへ入れない（ルール 1 / 8）

### 2.3 窓の覆いの検査

値を取るすべての窓（§2.4 の 60 s・§2.5 の 2 分）と、落ち着きを判定する区間（§2.5 の直近 `settle.span_s`）について次を検査し、
満たさない窓は**値を計算する前に**捨てる（理由 `telemetry_coverage` / `trace_coverage` / `rpm_coverage`）。

- **metric ごとの覆い**（その窓で使う metric のすべて）: 窓の中の行が**すべて** `quality = ok`・ok の行の数が
  `ceil(min_ok_ratio × 区間の長さ ÷ その source の周期)` 以上・隣り合う行の時刻の差と、窓の両端から最初・最後の行までの差が
  `max_sample_gap_ms` 以下。値は各 metric の自分の行の時刻で使い、補間・リサンプルしない（0151 §2）。閾値は source ごと
  （基板の `air.*`・`coldaisle-telemetry`・`coldaisle-cpu-power`）に持つ
- **trace の覆い**: 窓の中の tick に、窓を挟む 2 つの tick（窓の始まり以前の最後の tick と終わり以後の最初の tick）を足した範囲で、
  (1) すべて同じ `run_id`、(2) `tick_id` が 1 ずつ連続（`seq` は並べるためだけに使い、連続を求めない）、(3) 隣り合う tick と窓の両端の
  時刻の差が `trace_max_gap_ratio × tick_period_ms` 以下（`tick_period_ms` は trace の `runtime.tick_period_ms`。範囲の中で同じ値であること）、
  (4) tick の数が `ceil(min_trace_ticks_ratio × 区間の長さ ÷ tick_period_ms)` 以上、(5) すべての tick で `operating_mode` が `manual`・
  その段の `command_id`・Critical Safety が `NORMAL`・3 zone の `effective` が manual の値、(6) すべての tick の 3 zone で
  `hardware.write_ok` と `readback_ok` が true で `applied_demand` が manual の値（`applied_demand` を持たない古い版の trace は使わない）
- **回転数の覆い**（§2.4 の窓。Codex の指摘「tach coverage」）: `write_ok` / `readback_ok` が true でも `hardware.rpm` が null の tick は
  ありうる（`hwmon.py` の `_verify`）。その zone の `hardware.rpm` が null でない tick の数が `ceil(rpm_sweep.min_rpm_ratio × 区間の長さ ÷
  tick_period_ms)` 以上で、null でない tick の間と窓の両端からの時刻の差が `rpm_sweep.max_rpm_gap_ms` 以下でない窓は捨てる

### 2.4 回転数の掃引（`pwm_to_rpm` と Fan profile）

- 条件: アイドル（`cpu.package` 50 °C 未満・`gpu.0.core` 45 °C 未満・`power.gpu.0` 50 W 未満）。対象でない 2 zone は manual 0.60
- 段: 0.35・0.40・0.45・0.50・0.60・0.70・0.80・0.90・1.00 を上りで、続けて下りで（ヒステリシスを見る）
- 落ち着き: 各段は、trace の `effective` が指示の値に届いた tick から 30 s 待つ（下りは `ramp_down_per_s` で律されるので、指示の
  時刻ではなく届いた tick から数える）。続く 60 s の trace の `hardware.rpm`（null でない値）の中央値をその段の観測にする
- 回数: zone ごとに 3 回。うち少なくとも 1 回は別の日にする
- **上りと下りから 1 つの表へ**: 段ごとの候補の値は、3 回の上りと下りのすべての観測の**中央値**。ばらつきは同じ観測の
  **範囲 ÷ 中央値**（分母は候補の値と同じ）で見て、`approval.max_rpm_spread` を超える段は値を出さない（方向ごとの表は `FanProfile` が
  持てないので作らない）。**3 回のすべてで、その段が現れる方向（上り・下り。折り返しの 1.00 は 1 つ）のすべてに有効な観測があり、
  有効な回が `rpm_sweep.min_distinct_days` 日以上にまたがる**段だけ値を出す（一方向・1 日だけの観測でヒステリシスと再現性を素通りさせない）。**9 段のどれか（特に 1.00）が値を出せない zone は、
  その zone の表を出さずに測り直す**（1.00 は `airflow_index` の分母で、欠けた表は `FanProfile` の検証を通っても全開の値を下の点へ寄せるため。
  一部の段だけを 0120 の値と混ぜない）
- 応答の時間: 上りの段の、`effective` が変わった tick から `hardware.rpm` が新しい段の値の ±5 % に入って留まった tick まで（0126 と
  同じ測り方。tach の更新 1500 ms を足して記録）。下りは記録だけ
- 出すもの: zone ごとの RPM(d)（中央値・範囲）、観測の最大 RPM、上りと下りの差（方向ごとの中央値の差）、応答の時間

### 2.5 応答の行列（Thermal Effectiveness）

- 負荷: (i) GPU 300 W だけ、(ii) CPU の軽い固定の負荷だけ、(iii) 両方。各負荷で、3 zone を R = 0.60 にした基準の段と、1 zone だけを
  0.90（+）・0.35（−）にした段、3 zone とも 0.90 の段を、**基準の段を間に挟んで**行う
  （R, F+, R, F−, R, Rr+, R, Rr−, R, T+, R, T−, R, All+, R）
- **応答の値**: 動かした段の窓の平均から、前後の基準の段の窓の平均を代表の時刻（窓の中の行の時刻の平均）へ線形に内挿した値を引く
  （部屋・冷却水の蓄熱による時間のずれを打ち消す）
- 見る指標: `air.gpu_intake`・`d.case_delta`・`gpu.0.core` / `hotspot`・`cpu.package`・`air.gpu_exhaust` / `rear_exhaust` / `top_exhaust`
  （それぞれ `air.front_intake` との差）。`air.room` / `air.room_humidity`・`board.connector_12v2x6`・`gpu.0.fan_speed`・`fan.vrm.rpm`・
  `fan.aio_pump.rpm`・電力・`hardware.rpm` も残す
- **比べる段の条件をそろえる**: 動かしていない zone の `effective` が manual の値（§2.2 の floor の決まり）・負荷の設定（電力の上限と
  worker の数）が同じ・`fan.aio_pump.rpm` の窓の中央値の差と各窓の中の幅が `max_aux_fan_delta_rpm` 以内（理由 `condition_mismatch`）。
  GPU の Fan と VRM の Fan は温度に応じて動くのが運転のままの応答の一部なので、そろえずに記録する
- **実測の熱の入力をそろえる**（Codex の指摘「Match measured power in response-matrix pairs」）: Fan を変えると boost・throttle・リークで、
  同じ設定でも実際の電力が変わりうる。動かした段と、前後の基準の段を時刻で内挿した値の `power.cpu.package` と `power.gpu.0` の
  窓の平均の差の**絶対値**の上側の境界（`|ΔP| + bound_coverage_factor × u(ΔP)`。§2.6。電力が下がった組も同じに捨てる）が `response_matrix.max_power_delta_w` 以下でない組は捨てる（`power_mismatch`）。電力の差で
  正規化はしない（指標ごとの熱抵抗とその線形性を別に測る必要があり、その誤差が Fan の効果に混ざるため）。捨てた組の電力の差と
  throttle の flag は報告に残す（Fan を下げて throttle に入ること自体は意味のある結果なので、黙って消さない）
- 回数: 負荷ごとに 2 回（別の日）
- **落ち着き**: 直近 3 分（`settle.span_s`。§2.3 の覆いを満たすこと）の**生の ok の行の**幅（最大 − 最小。`cpu.package` は 3 点の中央値の列の幅）が、
  空気の温度 0.5 °C 以内・`cpu.package` / `gpu.0.core` / `board.connector_12v2x6` 1 °C 以内になったら落ち着いたとし、最後の 2 分の平均を
  値にする。20 分で落ち着かなければ「未収束」として外す（`unsettled`）
- 1 つのセッションの中で `air.room` が 1 °C を超えて動いたら、そのセッションをやり直す（0121 §2.9 と同じ趣旨）
- 応答の行列の Front の段（F+ / F−）は、Front が吸気の側で熱の出口を持たないので、風量の代わりに `d.case_delta`・`air.gpu_intake`・
  `d.gpu_preheat` の変化で効きを見る

### 2.6 閾値との比べ方と窓の不確かさ

閾値との比較は、測った値だけで比べない。上限の検査は `値 + bound_coverage_factor × u(値)` が上限以下のときだけ通す。`u` が求められない
値は通さない。

- **窓の平均の不確かさ**: 窓を時刻で `window_blocks` 個のブロックに分け、`u_win(x̄) = sqrt(sd(ブロックの平均)^2 + u_trend^2)`
  とする（`u_trend = |b| × 窓の長さ / sqrt(12)`、b は窓の中の最小二乗の傾き）。**ブロックの数で割らない**（Codex の指摘「Preserve
  autocorrelation across window blocks」。相関の時間がブロックより長いと √k で割った値は小さすぎるため、割らない保守の側に置く）。
  `|b| × 窓の長さ` が `max_window_trend` を超える窓は捨てる（`window_trend`）
- **差の `u`**（応答の値と電力の差で同じ形）: 動かした段の窓 L と、前後の基準の窓 1・2 を内挿の重み w で
  `u(Δx) = sqrt(u_win(L)^2 + (1−w)^2 u_win(1)^2 + w^2 u_win(2)^2 + u_res^2)`。`u_res` は電力では 1 W の丸め 1/√12 × √2、
  `air.front_intake` との差の温度では `resolution.air_c`/√12 × √4（2 本のプローブ × L と基準）、チップの温度ではその metric の分解能/√12 × √2。
  較正の offset は回の中で一定（§2.9 の較正の束縛）で差で打ち消されるので足さない。2 回の応答は値・`u`・2 回の差を並べて報告し、平均の `u` は
  `max(sqrt(Σu_i^2)/2, 2 回の差/2)` とする
- 落ち着きの幅の `u` はその metric の分解能の `u_res × √2`（`resolution.*`。欠ければ解析は何も出さずに失敗する）
- 回転数のばらつき: `(範囲 + bound_coverage_factor × u(範囲)) / (中央値 − bound_coverage_factor × u(中央値))` ≤ `approval.max_rpm_spread`。各観測の `u` は窓の中の
  `hardware.rpm` の `u_win` と tach の量子化から、範囲は √2 × 最大の `u`、中央値は 1.25 × 最大の `u` / √n
- ポンプの条件: 窓の中央値の差・各窓の中の幅を、各窓の `u_win` の二乗和の平方根で上側の境界にして `max_aux_fan_delta_rpm` と比べる
- 部屋: セッションの中の `air.room` のブロックの平均の最大 − 最小を `u_res × √2` で上側の境界にして `max_room_drift_c` と比べる
- 応答の値そのものには閾値を当てず、値と `u` を報告に載せる

### 2.7 結果の置き場所と承認

1. **生の記録**は今までどおり DB（telemetry と `control_traces`）にある。測定の区間と段は run の manifest（§2.9）に書き、
   `var/airflow-characterization/` に置く（`var/` は git の管理外）
2. **解析の入力は本番の DB そのものではなく、checkpoint された 1 ファイルの写し**にする。書き手が動いている DB を `immutable=1` で開くと、
   checkpoint されていない WAL を黙って無視しうる（`EvidenceDatabase` は空でない `-wal` / `-journal` を拒む。`src/coldaisle/evaluate.py`）
   - 写しは `sqlite3 <本番の DB> ".backup <写し>"`（`docs/ubuntu-deploy.md` の移行の手順 2 と同じ）で作り、`PRAGMA integrity_check;` が `ok` を確かめる
   - **権限**: 本番の DB のディレクトリは `2770`・グループ `coldaisle` で運用者の uid から読めない（0080 §2.1）。写しは所有者の許可を得て
     root で作り、運用者の所有・`0600` で `var/airflow-characterization/` へ置く。運用者を `coldaisle` グループに入れない
   - 写しは回の終了より後に作り、解析は写しの telemetry と `control_traces` の最後の時刻が manifest の終了の時刻以上であることを確かめる
   - manifest に写しの SHA-256・作った時刻・元の DB に対する相対の名前（絶対パスは書かない）を追記し、解析は照合して違えば拒否する
   - 解析は読み取りだけの道具（実装は別の PR）が manifest と写しを読み、RPM の表・応答の行列・再現性を JSON と Markdown に書く。
     設定・DB を書かない。control・AI 層・API から import しない
3. **報告**は `docs/airflow-characterization-YYYY-MM-DD.md` に値を残す（個体識別子・ホスト名・絶対パスを書かない。ルール 10）
4. **承認**は新しい決定記録で所有者が行う（0120 と同じ流儀）。承認の前に設定を変えない
   - `fan-hardware.yaml`（導入先。リポジトリに置かない）: 各 zone の `profile.pwm_to_rpm` を §2.4 の値に置き換える。0.35 未満の 0.25 の点は
     運転で届かないので 0120 の値を残し `basis` にそう書く。`airflow_index` は §2.1 の代用を新しい表から計算し直す。`maximum_rpm` は
     観測の最大に `rpm_sweep.maximum_rpm_margin` を足し `rpm_sweep.maximum_rpm_round_up` へ切り上げる。ファイルの `approval` は
     `confirmed` のままで、`basis` を承認の記録に変える
   - `air-balance.yaml` は変えない（§2.1）
   - 応答の行列は報告に載せ、#81 の決定記録の材料にする（本記録は設定へ写さない）
5. 承認の目安（承認の記録で所有者が最終に決める）: 回転数は段ごとの全観測の範囲が中央値の 3 % 以内（§2.6）。外れた段は値を出さず、
   理由を報告に残す

### 2.8 解析の設定（`config/airflow-characterization.yaml`）

解析が検査と計算に使うすべての数値を、解析の道具の設定（Control Config ではない）に置く。各値は `status: provisional` と `basis` を持ち、
コードに既定値を置かない（ルール 9）。設定が欠けた・不正なら、解析は何も出さずに失敗する。ファイルは解析の道具の PR で足す。

| 区分 | 鍵 | 提案の初期値 |
|---|---|---|
| 境界の係数 | `bound_coverage_factor` | 2 |
| 回転数の掃引 | `rpm_sweep.levels` / `other_zones_demand` / `settle_s` / `record_s` / `repetitions` / `min_distinct_days` | 0.35〜1.00 の 9 段 / 0.60 / 30 / 60 / 3 / 2 |
| 回転数の覆い | `rpm_sweep.min_rpm_ratio` / `rpm_sweep.max_rpm_gap_ms` | 0.9 / 3000（tach の更新 1500 ms の 2 回分） |
| 応答の時間 | `rpm_sweep.response_band` / `tach_update_ms` | ±5 % / 1500 |
| `maximum_rpm` の余裕 | `rpm_sweep.maximum_rpm_margin` / `rpm_sweep.maximum_rpm_round_up` | 5 %（0120）/ 100 RPM |
| アイドルの条件 | `idle.cpu_package_c` / `idle.gpu_core_c` / `idle.gpu_power_w` | 50 / 45 / 50 |
| 窓 | `window.length_s` / `window.max_settle_s` / `window_blocks` | 120 / 1200 / 4 |
| 落ち着き | `settle.span_s` / `settle.max_range_air_c` / `settle.max_range_chip_c` | 180 / 0.5 / 1.0 |
| 窓の傾き | `max_window_trend.temperature_c` / `max_window_trend.power_w` | 0.2 / 5 |
| 部屋 | `max_room_drift_c` | 1.0 |
| ポンプ | `max_aux_fan_delta_rpm` | 50 |
| 分解能 | `resolution.air_c` / `resolution.power_w` / `resolution.gpu_core_c` / `resolution.cpu_package_c` / `resolution.board_connector_c` | 0.125 / 1 / 1 / 実機の値の刻みを確かめて解析の道具の PR で決める / 同左（§5 の 1 で決定） |
| 負荷 | `load.gpu_power_limit_w` | 300 |
| 応答の行列 | `response_matrix.reference_demand` / `plus_demand` / `minus_demand` / `repetitions` / `min_distinct_days` / `max_power_delta_w` | 0.60 / 0.90 / 0.35 / 2 / 2 / 5 |
| 承認の目安 | `approval.max_rpm_spread` | 3 % |
| 窓の覆い | `coverage.<source>.min_ok_ratio` / `period_ms` / `max_sample_gap_ms` / `coverage.trace_max_gap_ratio` / `coverage.min_trace_ticks_ratio` | 0.9 / 2500（基板）/ 6000 / 1.5 / 0.95 |

設定に置かないもの: 中止の基準（0121 / 0130 の値。所有者が測定中に見る判断で、解析の検査ではない）、CPU の floor の点（`safety.yaml` の値。
解析は manifest の Control Config の hash を通して同じものを読む）。

### 2.9 run の manifest

測定のセッションごとに 1 ファイル（YAML）。形は解析の道具の PR で固定する。最低限、次を持つ。

- `schema_version`・`run_id`（#83 の `source_run_id` に使える形）・`kind`（`rpm_sweep` / `response_matrix`）
- Control Config の 4 ファイルの SHA-256（trace の `runtime.config` と突き合わせ、違えば拒否）。1 つのまとめ（段の 3 回・応答の行列の 2 回）に
  入るすべての回で同じであること（違えば `config_mismatch`）。測定の期間中は 4 ファイルを変えない
- 較正の digest（区間の開始の時点で有効な `calibration_activations` の行の `offsets_sha256`。0096 / 0099）
- DB の写しの SHA-256・作った時刻（§2.7 の 2）
- 負荷の定義（GPU の電力の上限・CPU の worker の数）と予備の試行の結果
- 段ごとの指示の値（3 zone）・lease・開始と終了の時刻（ms）・各窓の代表の時刻・捨てた理由（`floor_bound` / `unsettled` / `aborted` /
  `safety_fault` / `room_drift` / `condition_mismatch` / `window_trend` / `config_mismatch` / `power_mismatch` / `calibration_changed` /
  `telemetry_coverage` / `trace_coverage` / `rpm_coverage`）

**較正との結びつけ**（`response_matrix` だけ。回転数は温度を使わない）: 区間の中に `calibration_activations` の行が 1 つでもある回・
区間の開始の時点で有効な行が無い回・有効な行の digest が manifest と違う回は拒否する（`calibration_changed`）。応答の行列は温度の水準を
比べるので、digest の違う回をまとめない。

#83 への取り込みは #83 で決める。本記録は run_id と区間を manifest に残すことだけを決める。

### 2.10 測らないもの・測り直しの条件

- 測らない: 0.35 未満の回転数・停止と再始動の点（0120 §5 の 1。試験専用の設定が要るので別の記録で）、騒音（0067）、AIO ポンプ・
  GPU の Fan の特性、風量の値（§2.12）
- 測り直す: Fan・ラジエーター・フィルター・パネルの交換・取り付けの向き、空気のセンサーの位置（応答の行列）、GPU・CPU・AIO の交換、
  筐体の置き場所。Thermal Dataset v2 の宣言された変更（0109）にも書く

### 2.11 実機が無くても確かめること（テストの方針）

解析の道具の PR に、`-m "not hardware"` で通る次のテストを入れる。

- 合成の trace から §2.4 の手順どおりの掃引を作り、候補の表が `FanProfile`（単調・`maximum_rpm`・点の数）の検証を通ること。
  上りと下りで差のある例で、範囲 ÷ 中央値が上限を超える段を出さないこと。一方向だけ・1 日だけの観測が残った段を出さないこと。1.00 の段が欠けた zone の表を出さないこと
- `hardware.rpm` が null の tick が多い・間が開いた窓を `rpm_coverage` で捨てること（`write_ok` / `readback_ok` が true でも）
- floor に縛られた段・未収束・`air.room` のずれ・ポンプの不一致・Control Config の hash の不一致・区間の中の較正の有効化と digest の
  不一致・DB の写しの SHA-256 の不一致・空でない `-wal`・写しが区間を覆わないことを正しく外す・拒否すること
- 窓の覆い: `stale` / `suspect` の行・`tick_id` の欠け・`run_id` の変わり目・窓の境の tick の欠け・`applied_demand` が null の tick を
  値を計算する前に捨てること。`seq` に正当な欠番があっても `tick_id` が連続する窓は捨てないこと。`tick_period_ms` が 1000 ms 以外でも
  数の下限が求まること
- 応答の行列: 動かした段で電力が上がった・下がった合成の例を、どちらも `power_mismatch` で捨て、電力の差を報告に残すこと。基準の段の時刻の内挿が
  直線のずれを取り除くこと。強く相関した合成の窓で、`u_win` がブロックの数で割った値より小さくならないこと
- 解析の道具が DB へ書かない・設定へ書かない・control と AI 層から import されないこと。報告の Markdown に個体識別子が出ないこと

### 2.12 後続の記録で決めること（番号は取らない）

次は本記録の範囲の外で、それぞれ別の決定記録で決める。本記録の結果（RPM の表・応答の行列・manifest と DB の写しの決まり）は材料に使ってよい。

1. 風量の尺度（EFU）の定義と、Top / Rear の熱収支による測り方（負荷あり − 負荷なしの差・時刻の内挿・信号 ÷ 不確かさの条件）
2. Front の流量の測り方（零位法・圧力差の向きの見方・漏れの経路の一覧と駆動される経路の扱い。漏れは「すべての開口を観測する経路にする」
   方針を所有者が 2026-10-16 に選んだ。後続の記録へ引き継ぐ）
3. 出口ごとのプローブの補正と、zone をまたぐ独立の確認（風速計などの計測器の要否と一致の上限）
4. GPU の Fan の duty の影響（EFU の窓で GPU の Fan をそろえる方法・30・60・100 % での確かめ・30 % の Rear / Front の確かめ方・測る段を減らすか）
5. zone の間の結合（Rear を変えたときの Top の風量）の測り方と、1 次元の曲線で表せる上限
6. 小さな温度上昇の扱い（段ごとに負荷を変えるか・負荷の重さによらないことの検査）
7. `airflow_index` を測った相対の風量に置き換える条件と、`air-balance.yaml` を `calibrated` にする条件の一覧
8. `balance` / `thermal_limits` / `thermal_inputs` の値（#81。応答の行列を材料に）、Thermal Dataset への run の取り込み（#83）、
   0.35 未満の測り直し（0120 §5 の 1）、解析の道具と manifest の形（実装の PR）

### 2.13 既知の限界・後続の記録で扱う指摘

PR #341 の Codex のレビューの指摘のうち、本記録の範囲の外へ出した部分にかかるものを一覧に残す（後続の記録の出発点。本記録では扱わない）。
範囲の中の指摘は本文に取り込んだ（窓の覆い・trace の覆い・`applied_demand` の確認・`seq` の欠番・tick の周期からの数・上りと下りの 1 つの表と
分母・`maximum_rpm` の余裕・落ち着きを生の行で見る・落ち着きの区間の全体の覆い・チップの温度の分解能・Control Config の hash の一致・
較正の束縛・WAL の写し・応答の行列の電力の一致・回転数の覆い・ブロックの自己相関）。

| 後続（§2.12） | 指摘（要旨） |
|---|---|
| 1 | 負荷なしの基準の熱を差し引く・基準を L の時刻へ内挿・温度上昇が分解能に届かない点を捨てる・Rear の分子の全項の不確かさ・Top の 0.60 の基準の取り方・0.80 の排気の基準点・回の平均で共通の項を √n で割らない・共通の成分をすべて合わせる |
| 1・6 | 段ごとに負荷を変えるなら、すべての段で負荷の重さによらないことを確かめる |
| 2 | 零点の推定の方法（二分法）・負荷の間の零点の確かめ直し・漏れの正味の上限・零点の区間の誤差を Front の曲線へ伝える・GPU の Fan を零点の探索の間固定する・参照の組の数 |
| 3 | 1 点のプローブの偏りを確かめてから EFU を共通の尺度にする・独立の比が無ければ `calibrated` にしない・一致の上限を先に決める・計測器の不確かさの扱いを 1 つにする・補正の係数の不確かさを伝える・係数を段ごと・duty ごと・熱源ごと（CPU と GPU）に求める・**`q_top / q_rear` の比の不確かさを元の項から伝える（共分散。4bbf7f5 への最新の P1 を含む）** |
| 4 | B と L で GPU の Fan の流れをそろえる・固定の duty の偏りの上限・duty の影響を曲線の全体で測る・30 % で Rear と Front を確かめる・窓の中の補助の Fan の揺れを捨てる |
| 5 | **zone の間の結合が大きいときは `calibrated` にしない**・結合の比を上側の境界で比べる・Rear の各段で Top のコンダクタンスを測る |
| 7 | `airflow_index` を測った風量から求める・置き換えを当てはまりの条件の後にする |

本記録の範囲に残る限界:

- 応答の行列は GPU の Fan・VRM の Fan を運転のまま動かすので、その応答には Fan の直接の効果と、温度で動く補助の Fan の効果が混ざる
  （「運転のままの系の応答」として報告し、zone ごとの純粋な効果とは書かない）
- 応答の行列は 2 回で、ばらつきの目安を置かない（値と `u` と 2 回の差を報告に載せ、#81 の記録で使えるかを判断する）
- §2.6 の窓の不確かさは保守の側に置いた単純な推定で、相関の長さの推定はしない。より厳密な推定が要れば後続の記録で扱う
- `airflow_index` は代用のままなので、trace の `airflow_index` は風量を測った値ではない（§2.1）

## 3. Consequences

### 良くなること

- 校正の議論の決着を待たずに、すぐ測定を始められる（合計 約 28 時間。下の見積もり）
- `pwm_to_rpm` が運転記録の途中の点ではなく、落ち着いた段の値になる
- 応答の行列で「どの zone を上げるとどの温度がどれだけ下がるか」を説明でき（#75 の受入基準の一部）、#81 の材料になる
- 制御の経路の中で測るので、測定の間も Critical Safety と Guard が効いたまま。lease で放置に強い

時間の見積もり（1 つの窓を落ち着きの待ちを含めて平均約 15 分とする）: 回転数の掃引 約 4〜5 時間（18 段 × 3 zone × 3 回。段ごとに約 1.5 分）、
応答の行列 約 23 時間（15 段 × 3 負荷 × 2 回）、CPU の負荷の予備の試行 約 1 時間。**合計 約 28 時間**。所有者が在宅のセッションに分ける。

### 悪くなること・その緩和

- 風量（EFU）と `calibrated` は本記録では得られず、Air Balance は無効のまま。後続の記録（§2.12）で扱う。§2.13 に指摘を残したので、
  同じ議論をやり直さずに始められる
- `airflow_index` は代用のまま。`basis` と報告に代用と明記する
- floor に縛られた段は捨てるので、負荷の重い状態での低い Demand の応答は取れない。運転で届かない領域なので外挿しない

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 1 つの記録で回転数・EFU・零位法・補正・duty・結合・`calibrated` の条件まで決める（本記録の最初の案） | 指摘が収束せず、測定が約 170 時間を超え、未決が 8 点に増えても `calibrated` に届かなかった。いますぐ始められる測定まで止まる（所有者、2026-10-19） |
| 公称 CFM × RPM の比で `q_*` を作る | 装着時の抵抗を含まず、#75 / #81 / `docs/airflow-model.md` の原則に反する |
| 0120 の運転記録の中央値のまま `pwm_to_rpm` を使う | BIOS が動かしている途中の点を含み、0.40 未満が無い |
| 応答の行列で電力の差を正規化して残す | 指標ごとの熱抵抗とその線形性を別に測る必要があり、その模型の誤差が Fan の効果に混ざる。捨てて報告に残す |
| 応答の行列で GPU の Fan を固定の duty にする | 0128 / 0130 の曲線を試験の間だけ変える承認が要る。応答の行列は運転のままの系の応答を測るので要らない |
| 試験専用の設定で floor を外して低い Demand・高い負荷を測る | Critical Safety を緩める設定を測定のために使うことになる。運転で届かない領域なので測る必要がない |
| `CALIBRATION` モードを管理ソケットに足して使う | 新しい入口と承認が要る。MANUAL と lease で同じことができる |
| fand を止めて sysfs に直接書いて掃引する | Guard と Safety を迂回する（ルール 2。0121 §2.1） |
| 3 zone を同時にランダムに動かして回帰で一度に求める | 落ち着くまで 10 分以上かかる系では段の数が足りず、floor の介入の判定も難しい |
| GPU 600 W を固定の負荷にする | 低い Demand の段で Guard の floor と中止の基準に近づき、段の多くが捨てられる |

## 5. 判断点（決定済み。推奨を先頭に）

**所有者の決定（2026-10-19）**: 1 を推奨の案で決めた。§2 の本文はその決定を書いたものである。

| # | 論点 | 選択肢（推奨を先に） | 決定 |
|---|---|---|---|
| 1 | §2.8 の初期値（承認の目安の回転数 3 %・応答の行列の電力の差 5 W・落ち着きの幅・窓の覆いの割合など）と、未決の分解能（`cpu.package`・`board.connector_12v2x6`） | **(推奨) (a)** この値で進め、分解能は実機の値の刻みを確かめて解析の道具の PR で決め、結果を承認する記録で見直す / (b) 先に予備の測定で決める | 決定済み（所有者、2026-10-19）: (a) |

範囲を絞ったことで、最初の案の §5 の 1（圧力差の向き）・2（独立の確認）・3（GPU の Fan のそろえ方）・5（小さな温度上昇）・6（結合）・
8・9（duty の影響）は §2.12 の後続の記録へ移した。最初の案の §5 の 4 の EFU の部分も後続へ移し、回転数の部分を上の 1 に含めた。
