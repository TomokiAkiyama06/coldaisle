# 決定記録 0119: 導入先の `safety.yaml` の値と、`fan-policy.yaml` の Baseline で効く値を確定する（0028 承認点 2）

- **種別**: Decision Record
- **Status**: Proposed（値は 2026-10-11 にリポジトリ所有者が承認）
- **Date**: 2026-10-11
- **Supersedes**: なし（0028 §5 未決 1 のうち `safety.yaml` の値を閉じる。Policy の値は範囲外）
- **関連**: [`0028`](0028-fan-control-contracts.md) §2.6〜§2.9 /
  [`0029`](0029-telemetry-loss-classes.md) /
  [`0080`](0080-fand-systemd-unit.md) §2.6 /
  [`0110`](0110-t-sensor-ceiling-and-enablement.md) /
  [`0118`](0118-hwmon-backend-label-less-headers.md) /
  `docs/fan-header-mapping.md` / `docs/critical-safety.md` / `docs/control-config.md`
- **対象 Issue**: #50（Safety 閾値の候補）/ #74（実機の確認）

---

## 1. Context

実機の hwmon backend（0118）が入り、0028 §2.9 の承認点 3（実機の Fan の制御を初めて取る）へ進める状態になった。
その前に `safety.yaml` の値を `confirmed` にする必要がある（承認点 2）。これまでの値は試験用の仮の値しかない。

導入先で 2026-10-09〜10-11 の約2日間に `coldaisle-telemetry` が集めた実測（BIOS の自動制御で運転中。
GPU の実負荷を含む）と、`docs/fan-header-mapping.md` の PWM・RPM・停止点の実測から候補を作り、所有者が承認した。

実測の要点:

| metric | 最大 | p95 | 平均 |
|---|---:|---:|---:|
| `cpu.package` | 88 °C | 72 °C | 55.6 °C |
| `gpu.0.core` | 84 °C | 78 °C | 56.6 °C |
| `gpu.0.tlimit_margin` | 最小 9 °C | — | — |
| `power.gpu.0` | 610 W | 527 W | 289 W |
| `board.connector_12v2x6` | 53 °C | 52 °C | 41.4 °C |
| `fan.top.rpm` / `fan.front.rpm` / `fan.rear.rpm` | 最小 1024 / 666 / 701 | — | — |

BIOS の曲線は Fan を約 39% より下げていなかった。

あわせて次が分かった。

- `gpu.0.hotspot` / `gpu.0.mem` は導入先の GPU driver が NVML で公開せず、期間中一度も `ok` にならなかった。
  絶対温度上限の判定に使える GPU の温度は `gpu.0.core` だけである（どちらも Critical の入力ではないので fault にはならない）。
- `power.cpu.package` を集める入力が無い。Top の safety floor は温度の曲線だけで決まり、Power 項は
  `cpu_power_unavailable` として外れる（0029 の Degraded）。
- センサー基板（`air.*`）が未接続の間は、5本の air signal がすべて欠け `AIR_TELEMETRY_STALE` になり、
  Front / Rear は `fault_demand` に固定される（`docs/critical-safety.md`）。

## 2. Decision

導入先の `safety.yaml`（`/etc/coldaisle` に置く。リポジトリには置かない）を次の値にする。
次の6つを除き、すべて `status: confirmed`、`basis` に本記録の節を書く。除く6つは値を承認したうえで `provisional` に置く。

- §2.4 の `cpu_power_cooling_floor`（入力が無く、実際の Power の振る舞いと照らしていない）
- §2.6 の `stall_window_ms`、§2.9 の `ramp_down_per_s`、§2.10 の `tick_deadline_ms` / `watchdog_timeout_ms` / `hardware_write_fail_exit_ms`（実機の試験。§2.13）

Demand 0.35 は PWM 約 89 にあたる。

### 2.1 絶対温度上限

`absolute_temp_ceiling_c` = **90.0 °C**。CPU（Ryzen 9 9950X）は 95 °C で自らクロックを下げ始める。
GPU の T.Limit は実測の core 84 °C・余裕 9 °C から約 93 °C と推定した。実測の CPU 最大 88 °C は下回る。
超えると全 zone が Max になる。

### 2.2 最低安全 Demand

`zone_min_demand` = Front / Rear / Top とも **0.35**。BIOS のアイドル時（約 39%）に近い保守的な値。
停止点は Top / Front が PWM 16、Rear は PWM 0 でも約 440 RPM で回る（`docs/fan-header-mapping.md`）。
下限の候補 PWM 32（約 0.13）までの余裕は大きく取り、下げるのは運転データが貯まってから別の記録で行う。

### 2.3 CPU 温度の cooling floor（Top）

`cpu_cooling_floor` = **60 °C → 0.35、80 °C → 1.0**（線形補間）。実測の p95 72 °C で約 0.74。

### 2.4 CPU Power の cooling floor（Top）

`cpu_power_cooling_floor` = **100 W → 0.35、200 W → 1.0**。200 W は 9950X の PPT。
必須の項目のため値を置くが、`power.cpu.package` の入力が無い間は効かない（§1）。入力を足すと検証していない曲線が
そのまま効き始めるため、**`provisional` に置き**、入力を足したときに実際の Power の振る舞いと照らして別の記録で確定する。

### 2.5 故障時の Demand

`fault_demand` = **1.0**。

### 2.6 stall の判定

| 項目 | Front | Rear | Top |
|---|---:|---:|---:|
| `stall_check_min_demand` | 0.35 | 0.35 | 0.35 |
| `stall_min_rpm` | 300 | 350 | 450 |

`stall_min_rpm` は Demand 0.35 で出る回転数（PWM・RPM の実測から補間して Front 約 635 / Rear 約 697 /
Top 約 968 RPM）の約半分。`stall_check_min_demand` は最低安全 Demand と同じにし、floor で回っている Fan も監視する。
`stall_window_ms` = **5000**（**provisional**。§2.13）。`docs/fan-header-mapping.md` は定常の回転数だけで、
回転数の応答時間を含まない。窓が短すぎると正常な過渡を stall と読み Max に倒し、長すぎると止まった Fan の検出が遅れる（0034 §5）。

### 2.7 書き込み失敗

`write_fail_emergency_after` = **3**。

### 2.8 許容遅延

| 項目 | 値 | 根拠 |
|---|---:|---|
| `telemetry.cpu_ms` / `cpu_power_ms` / `gpu_ms` | 5000 | `coldaisle-telemetry` の収集周期 2500 ms の2倍（0110 §2.5 と同じ考え方） |
| `telemetry.air_sensor_period_ms` | 2500 | firmware の送信周期 `INTERVAL_MS` |
| `telemetry.air_ms` | 5000 | 送信周期の2倍 |
| `telemetry.t_sensor` | 有効・5000 ms・80 °C | 0110 で承認済み（変えない） |

### 2.9 復帰と下げる速さ

`ramp_down_per_s` = **0.05**（Max から 0.35 まで約13秒。**provisional**。§2.13）。実測は BIOS の自動制御の下のもので、
fand が下げたときの温度の応答を測っていない（0028 §5 未決 1）。`startup_settle_ms` = **5000**。
`fault_clear_hold_ms` = **30000**（故障の検出と解消が短い間隔で繰り返されないようにする）。

### 2.10 周期と deadman

| 項目 | 値 | status |
|---|---:|---|
| `tick_ms` | 1000 | confirmed |
| `tick_deadline_ms` | 250 | **provisional**（下記・§2.13） |
| `overrun_consecutive_limit` | 3 | confirmed |
| `watchdog_timeout_ms` | **5000** | **provisional**（§2.12・§2.13） |
| `hardware_write_fail_exit_ms` | 5000 | **provisional**（§2.12・§2.13） |

`tick_deadline_ms` は、承認の時点では導入先で tick の処理時間を測っていなかった。2026-10-11 に導入先の DB の写しへ
`coldaisle-telemetry`（実機の読み取り）と mock の空気温度を書き込みながら、simulated backend の `coldaisle-fand` を
300 tick 動かし、`duration_ms` は最大 2 ms・overrun 0 だった（Learned の経路は開いていない）。ただしこれは実機の sysfs の書き込みと読み戻しを含まないので、
§2.13 で `--backend hwmon` と運用の引数（Learned の経路を含む）で測るまで `provisional` に置く。

0080 §2.6 の不変条件 `(1000 + 250) × 3 = 3750 ≤ 5000 ≤ 5000`、`watchdog_timeout_ms ≥ (1000 + 250) × 2 = 2500` を満たす。
unit の `WatchdogSec` は `watchdog_timeout_ms` と同じ **5s** にする（0080 §2.2）。

### 2.11 承認点 3 の進め方

センサー基板の接続を待たず、まず **Top だけ**を下げて確かめる（所有者の選択）。
Front / Rear は `AIR_TELEMETRY_STALE` で Max のままになる（§1）。これは安全側であり、設定で避けない。
Front / Rear は基板を挿した後に、承認点 3 の確認をもう一度行う。

### 2.12 hang から Max までの最悪時間と熱の上がり方（0080 §2.3 / §5 未決 1）

0080 §2.3 は、hang から Max までの最悪時間（`WatchdogSec` + `TimeoutAbortSec` + 引き継ぎ実行部の実行時間）を
熱の時定数と比べて承認することを求める。最初の候補（`watchdog_timeout_ms` 10000）の承認の時点ではこの比較をしていなかったため、
下の比較を所有者に示し、改めて承認を得た（2026-10-11）。

候補の 10 s では、最悪時間は `WatchdogSec`（10 s）+ `TimeoutAbortSec`（unit の暫定値 5 s）+ 実行部（1 s 未満）で
約 16 s。その間、Fan は最後に書いた値（Top は §2.3 の floor 以上、Front / Rear は §2.2 以上）のまま回る。

導入先の約2日の実測（BIOS の自動制御・2500 ms 周期）で、ある時点から窓の中で温度が最も上がった幅:

| metric | 16 s | 30 s | 60 s |
|---|---:|---:|---:|
| `cpu.package` | +31 °C（45 → 76） | +31 °C | +33 °C（52 → 85） |
| `gpu.0.core` | +23 °C（32 → 55） | +28 °C | +35 °C（29 → 64） |
| `board.connector_12v2x6` | +3 °C | +5 °C | +9 °C |

CPU と GPU のチップの温度は、負荷の立ち上がりで数秒のうちに大きく上がり、その速さは Fan の回転数ではほぼ変わらない
（Fan が効くのは冷却水と筐体の、分の単位の温度である）。数秒の間のチップの保護は、CPU（95 °C）と GPU（T.Limit）が
自ら行うクロックの制限が担う。したがって 16 s の窓で問題になるのは「Fan が floor のまま分の単位の冷却が遅れること」で、
上の表の 60 s の幅と比べて判断する。

所有者は `watchdog_timeout_ms` の値を **5000** に短くして承認した。最悪時間は **約 11 s** になる。
ただし上の表は BIOS が Fan を上げられる状態の実測で、Fan を floor に固定したときの熱の上がり方ではない。
0080 §2.3 の比較を満たすため、§2.13 の試験を記録するまで `provisional` に置く。tick の処理時間は
実測で最大 2 ms（§2.10）なので、hang ではない fand を止める余地は小さい。`hardware_write_fail_exit_ms` は不変条件の
範囲 3750〜5000 の上端の 5000 を値として承認したが、0080 §2.6 は実機の書き込み・読み戻しの失敗の実測から導くことも
求めるため、§2.13 の測定まで `provisional` に置く。`TimeoutAbortSec`（5 s）の確定は 0080 §5 未決 1 に残る。

### 2.13 実機の試験まで `provisional` に置く値

次の5つは値を承認したが、実機の試験で裏付けるまで `provisional` に置く。**試験の手順は本記録では決めず、
承認点 3 の前に別の決定記録で設計して所有者の承認を得る**（§5 未決 5）。試験の結果が今の値を支えなければ、
値を変える案を所有者に出す。

| 値 | 試験で確かめること |
|---|---|
| `tick_deadline_ms` | 運用の unit と同じ引数（Learned の経路を含む）・`--backend hwmon` での `duration_ms` が 250 ms に十分な余裕を持って収まる |
| `stall_window_ms` | 3 zone すべてで、正常な立ち上がりと過渡が窓より十分短い（3 zone で共通の値なので Top だけでは確定しない） |
| `watchdog_timeout_ms` | hang から Max までの実際の最悪時間（約 11 s）の間の温度の上昇が、各温度の上限（T_SENSOR は 80 °C、ほかは 90 °C）までの余裕に収まる |
| `ramp_down_per_s` | Max から最低安全 Demand へ下げる間と下げた後に、温度の上昇が 2500 ms の収集周期で捉えられ、floor と Fallback が上げ直すまでにどの温度も上限に近づかない |
| `hardware_write_fail_exit_ms` | takeover の後の書き込みの失敗から `EMERGENCY`・終了コード 7・引き継ぎの Max までが想定どおりで、再起動の後に Max の輪にならない |

### 2.14 `fan-policy.yaml` の Baseline で効く値

最初の運転は Baseline（Fallback が requested を作り、Learned は shadow）なので、実際の回転数は Fallback と
Reactive Guard が決める。ここは Safety ではないが、所有者が 2026-10-11 に承認した。

**Fallback**: 約2日の実測では、BIOS は Top / Front / Rear の3つとも CPU の温度だけで回していた（CPU 5 °C ごとの
PWM の中央値: 40 °C 39% / 50 °C 47% / 60 °C 62% / 65 °C 72% / 70 °C 95% / 75 °C 100%）。最初の候補はこの曲線を写した
もの（72 °C で 1.0）だったが、GPU の温度も入力にすると、Fallback は3 zone の Demand の**最大値を全 zone に当てる**
（`FallbackController._propose()`）ため、GPU の負荷で Top を含む全 zone が全開になる。実測の温度の推移に当てはめると
全開の時間が 58% になったため、上端を緩めた曲線を所有者が選んだ（2026-10-11）。

| 項目 | 値 |
|---|---|
| `fallback_curve` | 45 °C → 0.40、55 °C → 0.50、65 °C → 0.65、75 °C → 0.85、82 °C → 1.0 |
| `fallback_temperature_inputs` | Top: `cpu.package`。Front / Rear: `cpu.package` と `gpu.0.core`（高い方） |
| `fallback_power_feedforward` | 使わない（`null`） |
| `fallback_dynamics` | `decrease_hysteresis` 0.05・`decrease_hold_ms` 10000 |

実測の推移に当てはめた Top の Demand（Safety の CPU 下限を含む）:

| 曲線 | 平均 | 中央値 | 全開の時間 |
|---|---:|---:|---:|
| BIOS（実際の値） | 0.58 | 0.52 | 5.3% |
| 最初の候補（72 °C で 1.0） | 0.80 | 1.00 | 58% |
| 採用（82 °C で 1.0） | 0.69 | 0.79 | 1.2% |

GPU の温度は全 zone に効く（Top も上がる）。平均では BIOS より強く回るが、CPU だけが熱いときは BIOS より弱い
（CPU 72 °C で 0.79、BIOS は約 1.0）。そのときも Safety の CPU 下限（§2.3。72 °C で 0.74）と絶対温度上限（§2.1）は効く。

**Reactive Guard**: 2500 ms ごとの実測の上昇の速さの p99 付近で発火させる。

| 項目 | 値 | 実測 |
|---|---|---|
| `floor` / `hold_ms` | 0.7 / 30000 | — |
| `cpu_temperature_rate_c_per_s` | 発火 6.0・解除 1.0（Degraded 5.0 / 0.5） | p99 5.6 °C/s |
| `gpu_temperature_rate_c_per_s` | 発火 3.0・解除 0.5（Degraded 2.5 / 0.25） | p99 2.8 °C/s |
| `gpu_power_rate_w_per_s` | 発火 60・解除 10（Degraded 50 / 5） | p99 55 W/s |

入力が無く発火しない trigger（`cpu_power_rate_w_per_s`・`intake_rise_c`・`gpu_hotspot_c`）と、Baseline では Fan に
効かない塊（`mpc`・`supervisor`・`workload_regime`・`gate_min_confidence`・`model_confidence`・`authority_*`・
`shadow`・`air_balance_coordination`）は試験の値のまま `provisional` に置き、Authority を上げる前に別の記録で決める。
`authority_stage`（上限）は `shadow` にする。`air-balance.yaml` はリポジトリの未校正の雛形のまま（Air Balance は無効）。

## 3. Consequences

### 良くなること

- 承認点 3 へ進める準備が揃う（§2.13 の試験の手順を別の記録で決めた後）。Safety の暫定値は §2.13 の5つと §2.4 の CPU Power の曲線だけになる（§2.4 は入力を足すまで残るので、起動ログの `safety_config_is_provisional` はそれまで true のまま）
- 値の根拠が実測と結びつき、後で緩めるときの比較の基準になる

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| 最低 Demand 0.35 は BIOS のアイドルとほぼ同じで、騒音は下がらない | 運転データが貯まってから、停止点の実測（PWM 16 / 32）を根拠に別の記録で下げる |
| 上限 90 °C は CPU の実測最大 88 °C に近く、高負荷で全 zone Max が出うる | Max は安全側。頻発するなら trace を根拠に別の記録で見直す |
| GPU の hotspot / memory 温度が上限の判定に使えない | core で判定する。driver が公開すれば変更なしで判定に入る |
| CPU Power の曲線が効かない | 温度の曲線で floor を決める。入力を足すのは別の Issue |
| 基板が無い間は Front / Rear が Max のまま | 承認点 3 を Top と Front / Rear の2回に分ける（§2.11） |

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 最低 Demand を下限の候補 PWM 32（約 0.13）にする | 実機で制御を取るのが初めてで、停止点との余裕が小さい。まず BIOS の運転に近い値から始める |
| 許容遅延を試験用の 1000 ms にする | 収集周期 2500 ms より短く、毎 tick stale になって Max に固定される |
| 基板の欠測の間も Front / Rear を下げられるようにする | 0029 の Critical の扱いを緩めることになる。安全側の判定を設定で外さない |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | unit の `TimeoutAbortSec` / `TimeoutStopSec` / `TimeoutStartSec` / `RestartSec` の確定（`WatchdogSec` は §2.10 で 5s） | 0080 §5 未決 1。承認点 3 の kill・watchdog の確認の後 |
| 2 | 最低 Demand を下げるか | 運転データの後、別の決定記録 |
| 3 | `fan-policy.yaml` の §2.14 以外の値と `air-balance.yaml` の校正 | Authority を上げる前（0028 承認点 5）と #75 |
| 4 | `power.cpu.package` の入力 | 別の Issue |
| 5 | §2.13 の試験の手順。設計で満たすこと（PR #274 のレビューで出た点）: (a) Guard と Critical Safety を迂回しない（fand を止めて sysfs へ直接書かない。BIOS の自動制御の間は `EBUSY`。0118）。(b) stall の閾値の下からの立ち上がりは、最低安全 Demand 0.35 のもとでは制御の経路の中で作れないので、承認された故障・復帰の治具などの方法を決める。(c) 書き込みの失敗は、起動時に開いて持ち続ける `pwmN` / `pwmN_enable` の書き込み先で起こす（権限を外す方法では起きない）。(d) watchdog は、到達しうる温度と Demand の組の中で最悪の開始状態を導くか掃引し、`WATCHDOG=1` の直後に止める位相（または複数の位相の最長）で測る。Fallback の全 zone の最大値と CPU の floor が Demand を上げる分を測りに混ぜない | 別の決定記録（#74 / #75）。承認点 3 の前 |
